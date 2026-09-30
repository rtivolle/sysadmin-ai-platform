# ADR-0015: Two-role split (platform / inference) replaces the PR-H1 three-role topology

- **Date:** 2026-09-30
- **Status:** Proposed — design recorded; implementation in flight on branch
  `feature/gpu-fleet-management`, owner acceptance pending;
  **supersedes/extends** the three-role `web` / `inference` / `data` design of
  [`docs/plans/MULTI_HOST_DEPLOYMENT.md`](../plans/MULTI_HOST_DEPLOYMENT.md)
  for the inference-GPU / enterprise-platform roadmap (PR-H1).
- **Source of divergence:** `~/workspace/your_files/architecture-inference-gpu/ARCHITECTURE.md`
  (rev. 2, 2026-09-30), §§1–5, 9–10. PR-H1's design (LiteLLM on the inference
  node, plaintext inter-machine links, user keys copied to two machines) is
  kept in the repo for compatibility but is no longer the target topology.

## Context

PR-H1 designed a three-machine split: **W** (web delivery: Traefik,
ForwardAuth, agent platform, sandbox), **I** (inference: vLLM, LiteLLM), **D**
(data: Valkey, VictoriaLogs, SeaweedFS). Two structural problems emerged:

1. **LiteLLM's auth is in-process, not a callback.** `custom_auth`
   (`backend/config/litellm/config.yaml`) runs inside the LiteLLM process and
   reads per-user bearer keys, Valkey and VictoriaLogs directly. Placing
   LiteLLM on **I** therefore forced the **GPU host to hold the full per-user
   key set** and to join the data network (I→D:6379 in plaintext), with a
   two-step key revocation across W and I (MULTI_HOST_DEPLOYMENT.md §3/§5).
2. **Valkey leaves the host.** W and I both crossed the LAN to D:6379, with
   the Valkey password on the wire in plaintext (TLS deferred to PR-H2). That
   is the worst possible place to accept plaintext: the credential store for
   quotas, leases, approvals and sessions.

Three roles also means three machines to provision, firewall and back up for
a project whose GPU tier must scale 1→N — while the data tier stays small and
centrally managed.

## Decision

Split into **two roles**, with LiteLLM relocated:

| Role | Hosts | Runs |
|---|---|---|
| `platform` | 1 (data + admin) | Traefik, ForwardAuth, agent platform (runtime + tools + approval gate), sandbox, **LiteLLM :4000 (loopback)**, Valkey :6379 (**loopback**), PostgreSQL :5432 (loopback), VictoriaLogs, SeaweedFS, `fleet_registry`, `litellm_sync` daemon, harness gateway, observability |
| `inference` | 1 to N (GPU) | `inference_engine` :8000 (LAN), `node_agent` :8001 (LAN), vLLM / llama.cpp per model (loopback) |

Why this is strictly better than W/I/D:

- **Auth becomes local again.** LiteLLM's `custom_auth` (keys + Valkey +
  VictoriaLogs) runs on the same host as everything it reads. GPU nodes carry
  **no user secrets** — no `master.key`, no `sysadmin-*.key`, no Valkey
  password. The node-agent authenticates by client certificate only.
- **Valkey never leaves loopback.** No more remote Valkey clients, no more
  password on the wire, no PR-H2 dependency for the data path. The only
  cross-node links are inference traffic (:8000), fleet control (:8001) and
  audit replay (:9428) — all mTLS from day 1 (ADR-0016).
- **Simpler fleet.** Adding a GPU node is register → approve → converge
  (ADR-0016); the three-machine install wizard, key-copy runbook and two-step
  revocation go away.

## Implementation

| Path | Change |
|---|---|
| `backend/platform.sh` | `role_services()`: `platform` ← valkey victorialogs audit_outbox seaweedfs auth_gateway agent_tools litellm traefik harness_gateway fleet_registry litellm_sync ; `inference` ← inference audit_outbox node_agent. `PEER_INFERENCE_HOSTS` becomes a **list**; `LITELLM_URL` is no longer exported to a peer (LiteLLM is platform-local). Bring-up order: platform first. |
| `install.sh` | `--role platform\|inference` replaces/extends `web\|inference\|data`; `--unattended` for frictionless GPU provisioning. `inference` installs drivers (`--nvidia`) and the vLLM venv, and **nothing** of Traefik/Valkey/VictoriaLogs/SeaweedFS/Postgres. Client-certificate provisioning against the fleet CA. |
| `backend/config/firewall/` | `platform.nft` + `inference.nft` (inference: inbound :8000/:8001 from the platform address only, default-deny); `web.nft`/`data.nft` retained for compatibility. |
| `backend/config/roles/render_config.py` | templates for the two roles; `deployment.env` carries `PLATFORM_URL` on inference nodes; the static peer list is superseded by the fleet registry. |
| `backend/services/inference_engine/` | configurable bind host (today `127.0.0.1` is hard-coded); accepts only the platform address, enforced by firewall. |

**Roles `web` / `data` are kept for compatibility** in `install.sh` and
`platform.sh`; they are deprecated in docs but still installable. `--role all`
remains the reversible single-host dev mode.

## Consequences

- **Positive.** No key duplication across machines; single-step revocation;
  Valkey and the audit/key material share one trust boundary (the platform
  node); GPU nodes are disposable secretless workers; firewall matrix shrinks
  to three links.
- **Negative / accepted.** The platform node concentrates Traefik, agent,
  LiteLLM, Valkey, Postgres, VictoriaLogs and SeaweedFS — a single point of
  failure. This is accepted: HA is already a deferred non-goal (PRODUCTION_READINESS.md
  §5), and the enterprise-level-1 target is backup/PITR-tested restore (RTO
  ≤ 2 h), not failover. HA levels 2/3 stay available as a later decision.
- **Negative / accepted.** `inference_engine` binding LAN is a new accepted
  network surface (previously loopback-only, ADR-0007); exposure is limited to
  the platform address by firewall default-deny.
- **Migration cost.** The in-flight PR-H1 role plumbing (`web`/`data` roles,
  key-copy runbook, `docs/multi-host.md`) must be updated to the two-role
  model; the roles stay installable so nothing breaks mid-migration.
- **Not yet wired.** PR-H1's topology was already unverified on real
  machines; this change does not fix that — real-machine bring-up remains the
  acceptance gate (PR-F1–PR-F5).

## Evidence

Design source: `ARCHITECTURE.md` §§1–5 (topology), §9 (P0 items 1, 5, 6, 8),
§10 (security). Implementation is in flight on this branch by parallel lanes
(node-agent, roles, mTLS, backups) and carries its own unit tests and
TEST_READY.md entries. This record's qualification is **proposed** until the
two-node bring-up, firewall matrix and fail-closed-503 semantics are measured
on real machines.

## Corrective work

1. Staged bring-up platform → inference on real machines with the full suite
   pointed at the platform's remote stores; firewall matrix verified
   (inference accepts :8000/:8001 from platform only; nothing else reachable).
2. Update `docs/multi-host.md` to the two-role model (PR-H1's operator guide).
3. Decide whether `web`/`data` roles are removed in a later release or kept
   indefinitely (spec §13 open question).
