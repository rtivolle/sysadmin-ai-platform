# Decision records — plan/spec divergences

This directory records **every place the implemented system diverges from the
source specifications (`docs/specs/`) and the implementation baseline
(`docs/plans/DEVELOPMENT_PLAN.md` §3/§4)**. It is the deliverable for work item
PR-D5 in `docs/plans/PRODUCTION_READINESS.md`.

Each record is MADR-style: **Context**, **Decision**, **Status**, **Consequences**,
**Evidence** (with the exact test/files that qualify the contract), and
**Corrective work** when a divergence is not yet qualified.

Status values follow MADR but default to **Proposed** (owner acceptance pending)
because no source document records explicit owner acceptance of these choices;
where a divergence is already documented as intentional (e.g. in
`docs/development.md` §5 or `DEVELOPMENT_PLAN.md` §3), the record says so.

## Index

| ADR | Divergence (plan/spec → implementation) | Status | Qualified? | Evidence / corrective item |
|---|---|---|---|---|
| [ADR-0001](ADR-0001-file-based-keys-valkey-no-postgresql.md) | PostgreSQL-backed LiteLLM virtual keys → file-based bearer keys + Valkey state | Proposed — **superseded by ADR-0014** for key lifecycle and durable control state | **Partial** | File keys stay the provisioning input; the durable key store is the runtime authority (ADR-0014). LiteLLM virtual keys remain unimplemented. |
| [ADR-0002](ADR-0002-no-nvidia-dynamo.md) | NVIDIA Dynamo (KV routing, P/D disaggregation) → not implemented | Proposed | **Yes (scope reduction)** | Deferral recorded in plan §1/§3/§5; no Dynamo code. |
| [ADR-0003](ADR-0003-simulated-inference-fallback.md) | Standalone vLLM → deterministic simulator + optional upstream proxy | Proposed | **Partial** | Fail-closed routing qualified (`test_inference_gateway.py`); no real vLLM run. Corrective: PR-C1. |
| [ADR-0004](ADR-0004-llamacpp-engine-added.md) | vLLM-only → llama.cpp (GGUF) engine added | Proposed | **Partial** | Unit-qualified (`test_model_manager.py` llama.cpp tests); no live download/start. Corrective: Phase 2 live. |
| [ADR-0005](ADR-0005-zero-docker-native-processes.md) | Docker Compose → zero-Docker native processes (SeaweedFS/VictoriaLogs/Valkey) | Proposed | **Yes** | Live bring-up + backup/restore tests (`test_platform.py`, `restore_drill.py`). |
| [ADR-0006](ADR-0006-local-login-no-idp.md) | LDAP/OIDC/PAM IdP → local PBKDF2 login + bearer keys | Proposed | **Partial** | Login + identity-integrity tests; no IdP adapter. Corrective: AUTH-01. |
| [ADR-0007](ADR-0007-single-host-topology.md) | Multi-machine/cluster → single-host loopback | Proposed | **Partial** | Single-host reachability tested; 3-machine split (PR-H1) unimplemented. |
| [ADR-0008](ADR-0008-port-and-layout-choices.md) | Spec port map (dsh 3080, Traefik 80/443) → agent 3080 collision, Traefik 8080/8443, harness 3085/3180–3280 | Proposed | **Partial** | Port wiring tested live; 3080 collision documented, not tested. |
| [ADR-0009](ADR-0009-sandbox-systemd-run-no-seccomp.md) | Plain bwrap (spec 04) → systemd-run/manual cgroup v2, no seccomp, 15s+5s | Proposed | **Partial** | Fail-closed + isolation + stress tests; no seccomp; raw cgroup leg fail-closed only (PR-B2). |
| [ADR-0010](ADR-0010-harness-opt-in-vs-react-runtime.md) | dsh is THE runtime → backend ReAct runtime + opt-in dsh gateway | Proposed | **Partial** | Policy parity + harness node tests; no end-to-end dual-runtime parity. |
| [ADR-0011](ADR-0011-p1-bounded-elevation.md) | "Unlimited" P1 key → time-bounded, quota-raised elevation | Proposed | **Yes** | Lifecycle/revocation/bypass tests (`test_m3_p1_elevation.py`, `test_m3_adversarial_p1_dr.py`). |
| [ADR-0012](ADR-0012-daily-token-ledger.md) | Monetary `max_budget` 2000 → separate daily token ledger | Proposed | **Yes** | `test_daily_token_reservation.py`, `test_rate_limits.py`, `test_empirical_challenger.py`. |
| [ADR-0013](ADR-0013-audit-integrity-archive.md) | "Immutable" audit → outbox + VictoriaLogs, no integrity archive | Proposed | **Partial** | Outbox durability qualified; no tamper-evident archival (PR-D2). |
| [ADR-0014](ADR-0014-postgresql-control-store.md) | ADR-0001's file-only keys + Valkey ledger → local PostgreSQL control store (key lifecycle + durable token ledger), Valkey for counters/leases | Proposed (implemented at service level) | **Partial** | 66 unit tests qualify resolution, rotation/revocation atomicity, hashing, ledger exactly-once and fail-closed behaviour; live cluster, LiteLLM `database_url` start and platform wiring not measured (corrective work 1–2). |
| [ADR-0015](ADR-0015-two-role-platform-inference-split.md) | PR-H1 three-role `web`/`inference`/`data` → two-role `platform` (data+admin) / `inference` (GPU); LiteLLM moves to platform, Valkey loopback-only, GPU nodes secretless | Proposed (implementation in flight) | **Partial** | Role plumbing + unit tests on branch `feature/gpu-fleet-management`; real two-machine bring-up not measured. |
| [ADR-0016](ADR-0016-gpu-fleet-control-loop.md) | Manual GPU fleet config → control loop: auto-register (`pending` + human approval), declarative desired state, bin-packing scheduler, heartbeat-delta convergence, dynamic LiteLLM `model_list`, mTLS at handshake from day 1 | Proposed (implementation in flight) | **Partial** | 91 new unit/integration tests green; live multi-node behaviour, LiteLLM hot reload vs restart, and CRL distribution not measured. |

## Summary

- **Qualified by tests (contract holds):** ADR-0002 (scope reduction), ADR-0005
  (zero-Docker), ADR-0011 (P1), ADR-0012 (token ledger).
- **Partially qualified (some contract tested, live/edge cases open):**
  ADR-0003, ADR-0004, ADR-0006, ADR-0007, ADR-0008, ADR-0009, ADR-0010,
  ADR-0013.
- **Not qualified — corrective work required:** none. ADR-0001 was the only
  divergence with a fully unqualified contract; [ADR-0014](ADR-0014-postgresql-control-store.md)
  supplies the mechanism (audited issue/rotate/revoke, durable ledger with
  reconciliation) and the qualifying tests, and is itself partially qualified
  until the live-cluster and wiring items land.
