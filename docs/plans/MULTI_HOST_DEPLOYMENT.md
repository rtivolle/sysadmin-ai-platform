# Multi-host deployment design — three-machine topology

Date: 24 September 2026. Status: **designed; PR-H1 implemented, not verified.**
The role plumbing (role flags, role-aware `platform.sh`, config rendering,
firewall rulesets, connectivity check) is implemented and unit-tested, but the
topology has never been run on three machines. Operator guide:
[docs/multi-host.md](../multi-host.md).

This document designs the split of the platform across three machines:

| Machine | Role | What runs there |
|---|---|---|
| **W — web delivery** | application tier | Traefik, ForwardAuth (`auth_gateway`), agent platform (`agent_runtime` + `agent_tools`, approval gate, target adapter), Bubblewrap+cgroups sandbox, harness gateway + per-user instances, user workspaces |
| **I — inference** | GPU host (8 × RTX 8000) | vLLM (14B/32B), `inference_engine` proxy/simulator, **LiteLLM proxy** |
| **D — data** | state tier | Valkey, VictoriaLogs, SeaweedFS, resilience backup/restore |

Decisions recorded with the owner (2026-09-24): LiteLLM lives on **I**;
inter-machine traffic runs on a **firewalled private LAN** for now, with TLS
on inter-machine links deferred to a hardening item (PR-H2).

## 1. Why this split

- **I** owns the only scarce hardware (GPUs). Model weights, KV cache and
  model traffic never leave it; only authenticated OpenAI-compatible calls
  cross from W.
- **D** owns durable state. Quota/lease/approval/session state (Valkey), the
  audit trail (VictoriaLogs) and artifacts (SeaweedFS) sit on one machine
  whose backup the resilience package already covers.
- **W** is the only machine reachable from user networks and the only one
  that executes sandboxed untrusted commands — keeping both the attack
  surface and the sandbox away from credentials at rest on D and model
  weights on I.

## 2. Request flows (cross-machine hops marked)

```text
user ──TLS──► Traefik (W:8443) ──► ForwardAuth (W:3081) ──► agent platform (W:3080)
                                                                  │
                    ┌─────────────────────────────────────────────┤
                    ▼                                             ▼
            LiteLLM (I:4000)  ◄══ hop W→I ══            sandbox tools (W, local)
                    │ in-process custom_auth                      │
                    ▼                                             ▼
        inference_engine (I:8000, loopback)           runbooks (W, Git-tracked)
                    │                                             │
                    ▼                                             ▼
              vLLM (I, loopback)                      SeaweedFS (D:8333) ◄══ hop W→D

W ──hop W→D──► Valkey (D:6379)        quota, leases, approvals, P1, sessions
W ──hop W→D──► VictoriaLogs (D:9428)  audit writes (durable local outbox on W)
                                      and audit queries/health from the harness
                                      admin console (gateway/admin.js:707,406)
I ──hop I→D──► Valkey (D:6379)        quota enforcement inside LiteLLM auth
I ──hop I→D──► VictoriaLogs (D:9428)  audit events from the LiteLLM callback
```

`sysadmin_cli.py` runs wherever the operator sits and targets
`GATEWAY_URL` → Traefik on W; it needs no presence on I or D.

All hops inside one machine stay on loopback, exactly as today.

## 3. The LiteLLM in-process auth consequence (read before implementing)

LiteLLM's authentication is **not** an HTTP callback: `custom_auth` in
`backend/config/litellm/config.yaml:21` names a Python function
(`backend/services/auth_gateway/litellm_auth.py`) that LiteLLM imports and
runs **in its own process**. That module:

- reads the per-user bearer keys from `backend/config/keys/` via
  `load_valid_tokens()` (`litellm_auth.py:39,44`),
- instantiates `QuotaManager()` → Valkey (`litellm_auth.py:31`),
- writes audit events → VictoriaLogs (`litellm_auth.py:28`).

Placing LiteLLM on I therefore means **I carries the full per-user key set and
the auth-gateway/quota/audit code**, and needs network access to D (Valkey +
VictoriaLogs). This is accepted: the alternative (LiteLLM on W) would send
model API traffic over the LAN instead, and I is a tightly controlled host.
Key replication to I happens at provisioning time over a secure operator
channel — never through the installer itself (§5).

The fail-closed contract is preserved: if D's Valkey is unreachable, the
in-process `QuotaManager` on I raises `ConnectionError` and LiteLLM answers
503, exactly as ForwardAuth does on W.

## 4. Network and firewall design

Bind-address changes from today's loopback-only posture (AGENTS.md §7):

| Service | Binds today | Binds in this design | Accepts from |
|---|---|---|---|
| Traefik (W) | 8080/8443 loopback | LAN | user networks (8443; 8080 redirect only) |
| ForwardAuth (W:3081) | loopback | loopback | Traefik on W only |
| Agent platform (W:3080) | loopback | loopback | Traefik on W only |
| Harness gateway (W:3085) | loopback | loopback or LAN (admin console) | per owner decision |
| LiteLLM (I:4000) | loopback | LAN | W only |
| inference_engine / vLLM (I:8000+) | loopback | loopback | LiteLLM on I only |
| Valkey (D:6379) | loopback | LAN + `protected-mode yes` + `requirepass` | W and I only |
| VictoriaLogs (D:9428) | loopback | LAN | W and I only |
| SeaweedFS (D:8333/9333/8888) | loopback | LAN | W only |

Host firewall baseline (nftables/ufw equivalents shipped with PR-H1):

- **W:** inbound allow 8443 (+8080 redirect) from user networks; outbound
  allow to I:4000 and D:6379/8333/9428; everything else default-deny.
- **I:** inbound allow 4000 from W's address only; outbound allow to
  D:6379/9428; default-deny otherwise.
- **D:** inbound allow 6379 from W+I, 8333/8888/9333 and 9428 from W only;
  no outbound requirements beyond backup export; default-deny.
- No machine except W is reachable from user networks. SSH management is out
  of scope here and follows site policy.

**Transport is plaintext on these links** (accepted decision). Bearer tokens
and the Valkey password cross a link-restricted, default-deny private
segment. TLS on these hops is hardening item PR-H2 and must be completed
before the machines ever share a non-isolated network.

## 5. Credential and secret distribution

Secrets stay in files under `backend/config/keys/` (0600/0700), are never
printed, and are never transmitted by the installer. Per-machine footprint:

| Key file | W | I | D | Consumed by |
|---|:-:|:-:|:-:|---|
| `master.key` | ✓ | ✓ | — | ForwardAuth + harness admin (W); `LITELLM_MASTER_KEY` at LiteLLM startup (I) |
| `sysadmin-*.key`, `emergency-p1.key` | ✓ | ✓ | — | ForwardAuth, agent runtime, harness (W); LiteLLM in-process auth (I) |
| `login-credentials.json`, `initial-passwords.txt` | ✓ | — | — | ForwardAuth + harness gateway (W) |
| `valkey-password.key` | ✓ | ✓ | ✓ | Valkey clients (W, I); Valkey `requirepass` (D) |

Provisioning flow: run the key provisioners on W, then the operator copies
exactly the rows marked ✓ to I and D over a secure channel (`scp`/`rsync`
over SSH, permissions verified after copy). Rotation = re-provision on W and
re-copy; revocation on W alone is insufficient while I holds a copy — the
runbook must say so.

**Backup consequence:** today the `config_keys` backup component captures the
whole keys directory on one host. In this topology each machine's resilience
scope covers its own keys; D's backup covers Valkey/VictoriaLogs/SeaweedFS
state, and W/I key backup is a per-machine operator step. PR-H1 must encode
this in `docs/backup-restore.md`.

## 6. Initial setup — role prompts (what PR-H1 implements)

`install.sh --tui` (or `--role web|inference|data` unattended) asks, in order:

1. **Machine role**: web delivery / inference / data.
2. **Peer addresses**: W is asked for D's and I's LAN addresses; I is asked
   for D's; D needs none (it only serves).
3. Role-appropriate subsets of today's questions: ports (defaults unchanged),
   inference mode (I only: local GPU vs emulated), models (I), quotas (W),
   workspaces (W), audit retention (W).
4. A **connectivity check** before applying: from W, TCP to I:4000 and
   D:6379/8333/9428; from I, TCP to D:6379/9428. Failure aborts the apply
   step (fail closed, not a warning).
5. The wizard writes `backend/config/platform_config.json` with `role` +
   peer addresses, generates only the configs the role needs (§7), and
   prints the exact key files the operator must copy to which machine (§5).

`platform.sh start` reads the role and starts **only that machine's
services**, exporting peer URLs (`LITELLM_URL`, `VALKEY_URL`,
`VICTORIALOGS_URL`, `SYSADMIN_*`) built from the recorded addresses instead
of the loopback literals at `backend/platform.sh:131,146,154-155,176,338`.
Staged bring-up order: **D → I → W** (data first so every client starts
against a reachable store; web last so the front door never proxies to a
half-started tier).

## 7. Configuration changes required (inventory from recon)

Already env-configurable (no code change, only values): `LITELLM_URL`,
`VALKEY_URL`, `VICTORIALOGS_URL`, `VALKEY_HOST/PORT`,
`SYSADMIN_BACKEND_URL`, `SYSADMIN_AUTH_URL`, `SYSADMIN_LITELLM_URL`,
`GATEWAY_URL`.

Must become role/address-aware:

- `backend/platform.sh` — VALKEY_URL construction (:131), VictoriaLogs
  `-httpListenAddr` (:146), SeaweedFS `-ip`/`-ip.bind` (:154-155), uvicorn
  `--host` (:176), port probe (:414), harness backend default (:338); plus
  role-filtered start/stop.
- `backend/config/valkey/valkey.conf` — `bind` (:2) becomes
  `bind 127.0.0.1 <D-LAN-address>` (loopback kept for D-local access such as
  the resilience manager's BGSAVE; LAN address added for W/I clients);
  `protected-mode yes` and `requirepass` stay mandatory.
- `backend/config/traefik/dynamic.yml` — backend URLs for LiteLLM (:114),
  SeaweedFS (:119) and VictoriaLogs (:124) point at I/D addresses; the rest
  stay loopback on W.
- `backend/installer_tui.py` — role/address prompts (:97-177), config
  generation (:195-305) builds routes from recorded addresses instead of
  `127.0.0.1` (:232-243).
- `install.sh` — `--role` flag; per-role binary downloads (W: Traefik; D:
  Valkey/VictoriaLogs/SeaweedFS; I: none) and directory creation.
- Sandbox config on W is untouched: the Bubblewrap runner and its cgroup
  abort contract are per-machine local.

## 8. Failure-mode semantics (unchanged contracts, new distances)

- **D unreachable from W or I** → quota/approval/P1/session paths raise
  `ConnectionError` → 503. No silent fallback (AGENTS.md §4). A LAN partition
  is indistinguishable from a store outage, which the code already handles.
- **VictoriaLogs unreachable** → audit events spool to the durable local
  outbox on the writing machine (W or I) and replay in order; outbox
  exhaustion blocks new privileged actions.
- **I unreachable from W** → inference gateway rejection; no fallback that
  bypasses quotas (covered by existing tests).
- **W down** → no user service; D and I keep running and stay consistent.

## 9. Honest limitations

- **Unverified.** This topology has never been run. PR-H1's acceptance
  requires a staged bring-up on real machines with the full suite pointed at
  remote Valkey/VictoriaLogs, recorded in `docs/status/TEST_READY.md`.
- **Plaintext inter-machine links** until PR-H2 (TLS) lands; acceptable only
  on an isolated segment with the §4 firewall rules verified.
- **Secrets exist on two machines** (W and I both hold user keys); revocation
  is two-step until a shared key store is designed.
- **Single points of failure everywhere.** Three machines, one instance of
  each service; HA remains a deferred non-goal.
- **Workspaces live on W** (server-assigned 0700, ephemeral per
  `docs/backup-restore.md`); they do not move to D in this design.
- The port-3080 host note in TEST_READY.md still applies on W.

## 10. Implementation tracking

| Item | Roadmap entry | Contents |
|---|---|---|
| Three-machine implementation | PR-H1 (P1) | Role prompts (§6), role-aware `platform.sh`/`install.sh`, config templating (§7), firewall rule sets (§4), key-copy runbook (§5), staged bring-up + remote-store suite on real machines |
| Inter-machine TLS | PR-H2 (P2) | Valkey TLS, HTTPS upstreams with verification, cert provisioning in `install.sh`, required before any non-isolated network |

## 11. PR-H1 implementation notes

What landed (see [docs/multi-host.md](../multi-host.md) for the operator flow):

- **`backend/config/roles/deployment.env.example`** — role + peer addresses +
  `LAN_BIND_IP` template. `deployment.env` is git-ignored (`*.env`).
- **`backend/platform.sh`** — loads `deployment.env`; `role_services()` gates
  start/stop/status to the role's services; forces loopback for `all`; exports
  `VALKEY_URL`/`VICTORIALOGS_URL`/`LITELLM_URL`/`SYSADMIN_LITELLM_URL` from
  peers; binds VictoriaLogs/SeaweedFS/LiteLLM to `LAN_BIND_IP`.
- **`backend/config/roles/render_config.py`** — renders `valkey/valkey.conf`
  (`data` adds LAN `bind`) and `traefik/dynamic.yml` (`web` points LiteLLM /
  SeaweedFS / VictoriaLogs upstreams at peers). `all` reproduces the checked-in
  files byte-for-byte.
- **`backend/config/roles/connectivity_check.py`** — fail-closed TCP check to
  required peer ports; `install.sh` runs it before applying.
- **`backend/config/firewall/{web,inference,data}.nft`** — per-role rulesets
  implementing the §4 matrix; not applied automatically.
- **`install.sh`** — `--role` / `--peer-*` / `--lan-bind-ip` flags, role-scoped
  binary downloads and directory creation, role-aware key provisioning (W
  provisions; I/D print the copy list).
- **`backend/installer_tui.py`** — role/peer prompt step, deployment.env write,
  connectivity check, and post-apply render.

Open follow-ups encoded in this design (not yet done): `docs/backup-restore.md`
still describes single-host key backup and must be updated to state that W/I key
backup is a per-machine operator step and D's backup covers the state tier.
`SYSADMIN_LITELLM_URL` and `VICTORIALOGS_URL` are exported by `platform.sh` so
the harness admin console (W) sees the peer URLs; verify the console's
`gateway/admin.js` audit/health paths against remote D before relying on them.

