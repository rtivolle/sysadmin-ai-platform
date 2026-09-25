# Multi-host deployment (three machines)

Status: **implemented, not verified on real machines.** The role plumbing
(`install.sh --role`, `platform.sh` role-awareness, config rendering, firewall
rulesets) is in place and unit-tested, but the topology has never been run on
three hosts. The full design lives in
[docs/plans/MULTI_HOST_DEPLOYMENT.md](plans/MULTI_HOST_DEPLOYMENT.md).

| Machine | Role | Runs |
|---|---|---|
| **W — web delivery** | `web` | Traefik, ForwardAuth, agent platform (`agent_runtime` + `agent_tools`), approval gate, target adapter, sandbox, harness gateway, workspaces |
| **I — inference** | `inference` | LiteLLM proxy, `inference_engine`, vLLM (loopback) |
| **D — data** | `data` | Valkey, VictoriaLogs, SeaweedFS, backup/restore |

A single host is the `all` role: every service, everything loopback, exactly
today's behaviour. `all` is the default whenever `deployment.env` is absent.

## 1. Prerequisite — no containers, no Docker

The runtime is the same zero-Docker stack as single-host. Nothing here adds
containers.

## 2. Staged bring-up (D → I → W)

Bring the tiers up in dependency order so every client starts against a
reachable store and the front door never proxies to a half-started tier.

1. **D (data)** — install then start Valkey, VictoriaLogs, SeaweedFS:
   ```bash
   ./install.sh --role data --lan-bind-ip <D-LAN-IP>
   ./platform.sh status   # valkey :6379, victorialogs :9428, seaweedfs :8333
   ```
   Data reaches no peers, so there is no pre-apply connectivity check.

2. **I (inference)** — install then start LiteLLM + inference engine:
   ```bash
   ./install.sh --role inference --lan-bind-ip <I-LAN-IP> --peer-data <D-LAN-IP>
   ./platform.sh status   # litellm :4000, inference :8000
   ```
   The installer TCP-checks D:6379 and D:9428 before applying; it fails closed
   (no partial apply) if D is unreachable. **Copy the key files from W before
   starting** — LiteLLM's in-process auth reads them (see §3).

3. **W (web)** — install then start the application tier:
   ```bash
   ./install.sh --role web --lan-bind-ip <W-LAN-IP> \
       --peer-inference <I-LAN-IP> --peer-data <D-LAN-IP>
   ./platform.sh status   # traefik :8443, auth_gateway :3081, agent :3080
   ```
   The installer TCP-checks I:4000 and D:6379/8333/9428 before applying.

Apply the firewall rulesets (§5) before any tier is reachable from anything
else, and start the tiers in order after the rules are in place.

## 3. Key-copy and revocation (W **and** I)

LiteLLM's authentication is **in-process** (`custom_auth` imports
`backend/services/auth_gateway/litellm_auth.py`), not an HTTP callback. So the
I host carries the full per-user key set and the quota/audit code, and needs
network access to D.

Provision keys on W only (`install.sh` role `all` or `web`), then copy exactly
these files over a **secure operator channel** (`scp`/`rsync` over SSH, verify
`0600`/`0700` permissions after copy):

| Key file | W | I | D |
|---|:-:|:-:|:-:|
| `master.key` | ✓ | ✓ | — |
| `sysadmin-*.key`, `emergency-p1.key` | ✓ | ✓ | — |
| `login-credentials.json`, `initial-passwords.txt` | ✓ | — | — |
| `valkey-password.key` | ✓ | ✓ | ✓ |

`install.sh --role inference|data` does **not** provision keys; it prints the
copy list and stops there.

**Revocation is two-step.** Rotating or revoking a key on W alone is
insufficient while I holds a copy: re-provision on W **and** re-copy to I (and
re-copy `valkey-password.key` to D when that password rotates). Until a shared
key store exists this is a manual runbook step.

## 4. What the role changes (config + env)

`deployment.env` (`backend/config/roles/deployment.env`, git-ignored) is the
only thing that makes a host stop being `all`:

- `platform.sh` starts/stops/reports **only this role's services**, forces
  loopback binds for `all`, and otherwise binds `LAN_BIND_IP`. It exports
  `VALKEY_URL`, `VICTORIALOGS_URL`, `LITELLM_URL` and `SYSADMIN_LITELLM_URL`
  from the peer addresses instead of loopback literals.
- `backend/config/roles/render_config.py` rewrites the role-dependent files:
  `valkey/valkey.conf` (Valkey `bind` gains the LAN address on `data`) and
  `traefik/dynamic.yml` (LiteLLM / SeaweedFS / VictoriaLogs upstream URLs point
  at peers on `web`). For `all` it reproduces the checked-in files byte-for-byte.
- `install.sh` accepts `--role` / `--peer-*` / `--lan-bind-ip`, installs only
  the role's binaries (W: Traefik; D: Valkey/VictoriaLogs/SeaweedFS; I: none),
  and runs the pre-apply connectivity check via
  `backend/config/roles/connectivity_check.py`.
- The TUI (`install.sh --tui`) asks the same role/peer questions first.

Interactively:
```bash
./install.sh --tui
```

## 5. Firewall rulesets

`backend/config/firewall/{web,inference,data}.nft` ship the §4 matrix from the
design doc. They are **not applied automatically** — review the `define` block
(interface name + peer IPs), then load explicitly, e.g. `nft -f
backend/config/firewall/web.nft`.

| Host | Inbound | Outbound |
|---|---|---|
| W | 8443 (+8080 redirect) from user networks | I:4000, D:6379/8333/9428 |
| I | 4000 from W only | D:6379/9428 |
| D | 6379 from W+I; 8333/8888/9333/9428 from W only | (backup export, site policy) |

Only W is reachable from user networks. SSH management is out of scope and
follows site policy.

## 6. Verification checklist

Run on real machines before considering this qualified:

- [ ] D up: `valkey`, `victorialogs`, `seaweedfs` reachable on LAN IPs.
- [ ] I up: `litellm` answers on I:4000; quota/audit reach D (no 503 when
      Valkey is up).
- [ ] W up: Traefik front door answers on W:8443; login + agent flow end-to-end.
- [ ] `./platform.sh test` (or the full `make test`) with `VALKEY_URL` /
      `VICTORIALOGS_URL` pointed at D passes.
- [ ] Firewall: from a user network, only W:8443 answers; W:4000/6379/9428
      and I/D ports are refused.
- [ ] Key revocation tested on **both** W and I (two-step, see §3).
- [ ] Backup/restore runs on D and covers Valkey/VictoriaLogs/SeaweedFS state.

Record results in `docs/status/TEST_READY.md`.

## 7. Honest limitations

- **Not verified on real machines.** The topology has never been run; §6 is
  the acceptance gate.
- **Plaintext inter-machine links.** Bearer tokens and the Valkey password
  cross an isolated, default-deny private segment **unencrypted**. TLS on these
  hops is hardening item PR-H2 and must land before the machines share any
  non-isolated network.
- **Secrets exist on two machines** (W and I both hold user keys); revocation
  is two-step until a shared key store is designed.
- **Single points of failure everywhere** — one instance of each service; HA is
  a deferred non-goal.
- **Workspaces live on W** (server-assigned 0700, ephemeral); they do not move
  to D.
- The port-3080 host note in `docs/status/TEST_READY.md` still applies on W.
