# Production-readiness roadmap

Date: 24 September 2026. Status: active program plan.

This document turns the gap lists scattered across
[DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md) (§6–§8 gates) and
[../status/TEST_READY.md](../status/TEST_READY.md) ("Outstanding qualification
work") into a single, prioritized list of work items. Each item is scoped so
**one agent or one session can own it end to end** (see AGENTS.md §8), carries
measurable acceptance criteria, and names its dependencies.

## How to use this roadmap

- **Claim one item at a time.** Record the claim in the tracking table (§4)
  before starting, so two agents never own the same item.
- **Evidence rule.** An item is *done* only when its acceptance evidence is
  reproduced in a run and recorded in [../status/TEST_READY.md](../status/TEST_READY.md).
  Say "not measured" when it was not measured — never restate a target as a
  result (AGENTS.md §4, docs conventions).
- **Guardrails apply to every item.** Fail-closed stores, credential-derived
  identity, single-use bound approvals, the sandbox abort contract, and the
  Python/JS policy parity test must not be weakened to make a check pass.
- **Priorities.** P0 items block *any production-changing action* (target
  adapter use). P1 items block *declaring the service production-ready*.
  P2 items block *release acceptance* (REL-01). Within a priority, items are
  ordered by dependency, not importance.

## 1. Verified baseline (what production-readiness builds on)

Recorded in [../status/TEST_READY.md](../status/TEST_READY.md) (2026-09-24) and
not restated here in detail: 488-test backend suite (480 passed, 8 skipped with
services stopped; green in the previously recorded service configurations) and
per-path audit census pinned by tests; tier-2 sandbox suite (129 checks) with kernel-backed stress
qualification on the `systemd-run` leg (OOM at the 4 GiB ceiling, 128-task
ceiling, 2-CPU throttle, 15 s deadline, network denial, filesystem
confinement); fail-closed quota/lease/P1 behaviour under shared-store outage;
single-use approvals bound to user/session/command/workspace/expiry; durable
audit outbox; file-level clean-staging restore drill with byte-for-byte
verification (28 checks); multi-user harness integration verified live
(45 node tests, 8/8 dsh checks, 37/37 live gateway checks).

The honest starting position: **prototype — not a production acceptance
certificate.** Inference is simulated unless a vLLM upstream is configured,
and the target adapter's privileged boundary is not qualified.

## 2. Definition of production-ready

The platform is production-ready when **all** of the following hold, each
backed by a recorded run:

1. One narrowly scoped, allow-listed target action (configuration deployment
   or service restart) executes on a registered staging target through the
   full identity → approval → least-privilege execution → audit chain, with
   rollback demonstrated (ACT-02 gate).
2. The mandatory security gates of DEVELOPMENT_PLAN §7 pass as one signed
   report, with zero unresolved critical isolation or data-integrity
   failures (QA-01).
3. Real inference serves the 14B and 32B models on the RTX 8000 with pinned
   versions, and the load/latency gates of §7 are measured — not extrapolated —
   including a 2-hour load test and a 24-hour soak (INF-02, QA-01).
4. Recovery is demonstrated end to end: restore into clean staging **including
   service bring-up**, on a production-sized data set, inside the owner-accepted
   RTO (proposed ≤ 4 h) (OPS-01).
5. Operations can see and respond: metrics, alerts with named owners and
   runbooks, off-host backups with an independent integrity anchor (OPS-01,
   AUD-01).
6. The release bundle exists: version/digest/model-revision manifest, rollback
   instructions, operator runbooks, pilot acceptance (REL-01).

## 3. Work items

### P0 — blockers for production-changing actions

#### PR-A1 — Target adapter least-privilege privileged boundary

- **Scope.** Design and implement the privilege model for
  `backend/services/target_adapter/`: which identity executes allow-listed
  actions, how privilege is obtained (dedicated systemd unit / tightly scoped
  sudoers entries pinned to exact unit names and destination paths), and how
  the adapter is prevented from acquiring anything beyond the allowlist. The
  Bubblewrap sandbox must never receive host-control privileges
  (DEVELOPMENT_PLAN §4).
- **Acceptance criteria.**
  - Written privilege model in `docs/security.md`: executor identity, every
    privilege acquisition path, and why each is minimal.
  - Executor runs as a dedicated non-root identity; the allowlist (targets,
    actions, destination paths) is enforced in the privileged component, not
    only in the unprivileged caller.
  - Negative tests: non-allow-listed target, action, and destination path are
    rejected before any side effect; symlink/traversal variants included.
  - Security reviewer sign-off; tier-1 + tier-3 suites green; results recorded
    in TEST_READY.md.
- **Dependencies.** None. **Blocks:** PR-A2, PR-E1.

#### PR-A2 — Clean staging target deployment qualification (ACT-02)

- **Scope.** Register a disposable staging target and qualify one workflow end
  to end through `target_adapter`: proposal → approval bound to exact
  target + content hash → execution → verification → rollback.
- **Acceptance criteria.**
  - Live run: approved exact change deploys; changed-content re-execution is
    rejected without a new approval; replayed approval cannot execute twice.
  - Concurrent modification between approval and execution is detected and
    rejected; failure recovery and rollback demonstrated, not just described.
  - Evidence (commands, outputs, audit trail excerpt) recorded in
    TEST_READY.md.
- **Dependencies.** PR-A1. **Blocks:** PR-E1, the production extension itself.

#### PR-B1 — Event-by-event audit completeness census *(in flight)*

- **Scope.** Finish the census pinned by
  `backend/tests/tier1_unit/test_audit_census.py`: publish
  `docs/status/AUDIT_CENSUS.md` enumerating every user- or agent-triggerable
  action/outcome path and its audit event; convert remaining gap sentinels
  into positive assertions as gaps close.
- **Acceptance criteria.**
  - Every enumerable path emits a schema-conformant event for success **and**
    failure outcomes (VictoriaLogs first, durable outbox on outage).
  - Python and JS audit writers produce the canonical field set; parity test
    green; `make test` green with zero gap sentinels left.
- **Dependencies.** None. **Blocks:** PR-E1.

#### PR-B2 — Sandbox raw cgroup-delegation leg on the production host

- **Scope.** Re-run `make stress-sandbox` under the platform account's
  delegated writable cgroup v2 subtree on the production host, qualifying the
  raw leg that so far only proved fail-closed (abort 126).
- **Acceptance criteria.** The same ceilings measured on the systemd-run leg
  (memory + `memory.swap.max=0`, pids 128, CPU 2.0, 15 s deadline, network
  denial) are measured on the raw leg; results appended to TEST_READY.md.
- **Dependencies.** Host with delegated writable subtree. **Blocks:** PR-E1.

#### PR-E1 — Consolidated security-gate report (QA-01, security half)

- **Scope.** Execute the mandatory security gates of DEVELOPMENT_PLAN §7 as a
  single reviewable run: cross-user isolation (sessions, workspaces, S3,
  approvals, keys), forged identity headers and direct backend access, tool
  confinement on every entry point, sandbox host-write/network/privilege
  denial, bounded abuse tests on a disposable host, approval
  substitution/replay/expiry, prompt-injection-in-documents cannot authorize,
  secrets absent from responses/logs/artifacts.
- **Acceptance criteria.** Signed test report (security reviewer) with zero
  unresolved critical isolation or data-integrity failures; every gate either
  maps to an existing automated check (named) or has a recorded manual run.
- **Dependencies.** PR-A1, PR-A2, PR-B1, PR-B2. **Blocks:** PR-D4.

### P1 — blockers for declaring the service production-ready

#### PR-C1 — Real vLLM inference on the RTX 8000 (INF-01/INF-02)

- **Scope.** Stand up standalone vLLM on the actual hardware: prove the 14B
  FP16 model with streaming and cancellation, then the 32B; select the TP
  layout from topology inspection; pin the entire stack (driver, CUDA, vLLM,
  model/tokenizer revisions); point `inference_engine` at it.
- **Acceptance criteria.**
  - Pinned manifest recorded; 14B and 32B load, streamed completion and
    cancellation demonstrated; per-device VRAM, KV blocks and peak runtime
    memory measured.
  - Per-model active-request limits and context bounds are set from
    measurement, and the simulator remains a clearly-labelled dev fallback.
- **Dependencies.** GPU host access. **Blocks:** PR-C2, PR-B6.

#### PR-C2 — Load and latency gates (DEVELOPMENT_PLAN §7)

- **Scope.** Run the specified matrix — 1K/8K/16K prompts (+32K exploratory),
  512-token output cap, fast-only / heavy-only / 70-30 mix, 1/5/10 users,
  twenty admitted calls with overload queued-or-rejected, cold and warm cache.
- **Acceptance criteria.** TTFT (including admission wait), decode rate,
  queue duration, per-user served work, error rate and cancellation cleanup
  reported with no hidden queue time; provisional p95 targets confirmed or
  revised with the owner; 2-hour repeatable load test passes with zero OOMs.
- **Dependencies.** PR-C1. **Blocks:** PR-B6, PR-D4.

#### PR-B3 — Multi-worker Valkey admission run

- **Scope.** Prove admission atomicity (concurrency leases, RPM, daily token
  reservation) with at least two concurrent agent-runtime workers against live
  Valkey — the single-worker assumption is the untested part.
- **Acceptance criteria.** Concurrent admission never exceeds ceilings;
  duplicate-ID rejection and exactly-once settlement hold across workers;
  recorded in TEST_READY.md.
- **Dependencies.** None (live Valkey). **Blocks:** PR-E1.

#### PR-B4 — Restore: service bring-up + production-sized data set

- **Scope.** Extend the 2026-09-24 file-level drill
  (`backend/tests/qualification/restore_drill.py`): after restoring into clean
  staging, start the services **from restored state** and pass readiness
  checks; re-run with a production-sized data set (scale mode or real volume).
- **Acceptance criteria.** Measured end-to-end RTO (copy + bring-up +
  readiness) inside the owner-accepted objective (proposed ≤ 4 h); live
  `BGSAVE` / `/snapshot/create` paths exercised or their omission explicitly
  recorded; results in TEST_READY.md.
- **Dependencies.** None. **Blocks:** PR-D4.

#### PR-B5 — Owner-scored 30-task evaluation

- **Scope.** Replace the synthetic pack claim with the real evaluation:
  30 anonymized owner-supplied tasks (10 log investigations, 10 config/script,
  10 runbook retrieval) scored against the owner's rubric.
- **Acceptance criteria.** ≥ 24/30 meet the rubric (proposed threshold,
  confirm with owner); **zero** unauthorized executions; malformed-argument
  and tool-call parsing cases included; report recorded.
- **Dependencies.** PR-C1 (meaningful scores need the real model).
  **Blocks:** PR-D4.

#### PR-D1 — Operational metrics, alerts and response runbooks (OPS-01)

- **Scope.** Add the small local collector and approved local viewer/alert
  destination required by DEVELOPMENT_PLAN §8: queue delay, errors, quota
  rejects, GPU/RAM/disk saturation, expired leases, audit outbox backlog,
  backup age.
- **Acceptance criteria.** Each actionable alert has a named owner and a
  response runbook in `docs/`; failed-service and backup-age alerts are
  demonstrated firing in a test run; component choice recorded with the
  license inventory.
- **Dependencies.** PR-C1 for GPU metrics. **Blocks:** PR-B6, PR-D4.

#### PR-D2 — Off-host backups and independent audit integrity anchor (AUD-01)

- **Scope.** Close the two recorded honest limitations in
  `docs/backup-restore.md`: operator-automated off-host archive copies, and
  chained/batched audit hashes anchored to independently controlled storage
  with restricted deletion and tested verification; obtain the data owner's
  retention decision (90 days proposed).
- **Acceptance criteria.** Tamper with a log batch → verification fails and
  identifies the batch; restore rehearsal uses an off-host copy; retention
  decision recorded; the word "immutable" is used only if the storage policy
  proves it.
- **Dependencies.** None. **Blocks:** PR-E1, PR-D4.

#### PR-H1 — Three-machine deployment implementation

- **Scope.** Implement [MULTI_HOST_DEPLOYMENT.md](MULTI_HOST_DEPLOYMENT.md):
  split the platform across web-delivery (W), inference (I) and data (D)
  machines — installer role prompts with peer addresses and a pre-apply
  connectivity check, role-aware `install.sh`/`platform.sh`, templated
  Traefik/Valkey/service bind addresses, host firewall rule sets, and the
  key-copy runbook (user keys live on W **and** I because LiteLLM's auth is
  in-process).
- **Acceptance criteria.** Staged bring-up D → I → W on real machines; the
  full backend suite passes pointed at the remote Valkey/VictoriaLogs (they
  already honor `VALKEY_URL`/`VICTORIALOGS_URL`); firewall matrices verified
  (I accepts 4000 from W only, D accepts its ports from W/I only, user
  networks reach only W:8443); fail-closed 503 semantics re-verified across a
  LAN partition; revocation runbook covers both key copies; evidence recorded
  in TEST_READY.md.
- **Dependencies.** None; can run in parallel with PR-C1 (I is where vLLM
  lands anyway). **Blocks:** nothing, but production deployment follows this
  topology once chosen.

#### PR-B6 — 24-hour pilot soak (QA-01, soak half)

- **Scope.** Run the production configuration for 24 hours with realistic
  multi-user traffic; then a controlled worker-restart drill.
- **Acceptance criteria.** No user exceeds two active calls across sessions at
  any sample point; worker restarts do not leak quota slots; stale-lease
  recovery interval set from measured cancellation behaviour; alert runbooks
  (PR-D1) exercised by at least one real alert.
- **Dependencies.** PR-C2, PR-D1. **Blocks:** PR-D4.

### P2 — release acceptance

#### PR-H2 — Inter-machine TLS hardening

- **Scope.** Replace the plaintext links accepted in
  [MULTI_HOST_DEPLOYMENT.md](MULTI_HOST_DEPLOYMENT.md) §4: Valkey TLS, HTTPS
  to LiteLLM/VictoriaLogs/SeaweedFS with upstream certificate verification,
  and certificate provisioning in `install.sh`.
- **Acceptance criteria.** Bearer tokens and the Valkey password never cross
  a link unencrypted; clients reject wrong/self-signed-substituted certs;
  suite green over TLS; mandatory before the machines share any non-isolated
  network.
- **Dependencies.** PR-H1.

#### PR-D3 — Sovereignty / blocked-egress operation

- **Scope.** Install and operate the stack with public egress blocked:
  mirrored packages/models, local DNS/NTP/PKI, telemetry and cloud fallbacks
  disabled.
- **Acceptance criteria.** Full-suite green with egress blocked; firewall
  evidence captured for the acceptance report; any runtime data egress is
  documented as a controlled import process.
- **Dependencies.** None, but cheapest after PR-C1 pins the artifacts.
  **Blocks:** PR-D4.

#### PR-D4 — Release bundle and pilot acceptance (REL-01)

- **Scope.** Assemble the release: manifest (versions, digests,
  model/tokenizer revisions, kernel/driver, migrations), rollback
  instructions, deploy/restore/rotate/upgrade/incident runbooks, operator
  training, and the sysadmin owner's pilot sign-off.
- **Acceptance criteria.** Every PR item above is *done* with evidence linked;
  the manifest installs on a clean machine; the owner accepts the pilot.
- **Dependencies.** All P0 and P1 items, PR-D3.

#### PR-D5 — Decision records for plan divergences

- **Scope.** The implementation diverged from DEVELOPMENT_PLAN §3/§4 in a few
  places (file-based keys + Valkey instead of PostgreSQL-backed LiteLLM
  virtual keys; no Dynamo; port/layout choices). Record each as an accepted
  decision with evidence, or open corrective work.
- **Acceptance criteria.** Every divergence is either qualified (tests proving
  the contract still holds — key rotation/revocation, restart durability) or
  has a written owner-accepted decision record; the release bundle references
  them.
- **Dependencies.** None. **Blocks:** PR-D4.

## 4. Tracking

| ID | Item | Priority | Depends on | Status | Evidence |
|---|---|---|---|---|---|
| PR-A1 | Adapter least-privilege boundary | P0 | — | not started | — |
| PR-A2 | Staging target deployment | P0 | A1 | not started | — |
| PR-B1 | Audit completeness census | P0 | — | **in flight** | `docs/status/AUDIT_CENSUS.md` published 2026-09-24; `test_audit_census.py` green; 5 gaps (G1, G3–G6) still open |
| PR-B2 | Sandbox raw cgroup leg | P0 | host | not started | systemd-run leg done 2026-09-24 |
| PR-E1 | Security-gate report | P0 | A1, A2, B1, B2 | not started | — |
| PR-C1 | Real vLLM inference | P1 | GPU host | not started | — |
| PR-C2 | Load/latency gates | P1 | C1 | not started | — |
| PR-B3 | Multi-worker Valkey run | P1 | — | not started | — |
| PR-B4 | Restore bring-up + scale | P1 | — | not started | file-level drill done 2026-09-24 |
| PR-B5 | Owner-scored 30-task eval | P1 | C1 | not started | synthetic pack only |
| PR-H1 | Three-machine deployment | P1 | — | not started | design: `docs/plans/MULTI_HOST_DEPLOYMENT.md` |
| PR-D1 | Metrics, alerts, runbooks | P1 | C1 | not started | — |
| PR-D2 | Off-host backup + audit anchor | P1 | — | not started | — |
| PR-B6 | 24 h pilot soak | P1 | C2, D1 | not started | — |
| PR-D3 | Blocked-egress operation | P2 | — | not started | — |
| PR-H2 | Inter-machine TLS | P2 | H1 | not started | — |
| PR-D4 | Release bundle (REL-01) | P2 | all above | not started | — |
| PR-D5 | Divergence decision records | P2 | — | not started | — |

Status values: `not started` / `claimed by <agent or session>` / `in flight` /
`done (evidence linked)`. Update this table in the same change that records
the evidence in TEST_READY.md.

## 5. Explicit non-goals (unchanged from the development plan)

High availability, Kubernetes migration, arbitrary privileged shell,
unrestricted SSH, autonomous remediation, PDF/OCR runbook extraction,
fine-tuning, vector search and a Dynamo port remain deferred and require
separate estimates. One server stays a single point of failure; backups are
not HA. Do not expand a work item to cover these while executing it.
