# Testing

The project uses pytest for the backend and Node's built-in test runner for the
Harness integration. `pytest.ini` sets `pythonpath = . backend`,
`testpaths = backend/tests` and `asyncio_mode = auto`.

## 1. Test tiers

| Tier | Directory | Focus |
|---|---|---|
| Tier 1 — unit | `backend/tests/tier1_unit/` | Models, parsing, tools, auth login, quota fail-closed, admin quota policy, audit census, config-deploy integrity, P1, target adapter, config lint, runbook reader, workspace assignment, tool schemas. |
| Tier 2 — sandbox | `backend/tests/tier2_sandbox/` | Bubblewrap isolation, cgroup limits, destructive-command interception, workspace isolation. Requires host support. |
| Tier 3 — concurrency | `backend/tests/tier3_concurrency/` | Approval lifecycle/binding/HTTP, concurrency ceiling, rate limits, P1 elevation, adversarial challengers, read-only slice stress. |
| Tier 4 — recovery | `backend/tests/tier4_recovery/` | Outbox resilience, DR drill, restore, adversarial P1/DR cases. |
| End-to-end | `backend/tests/e2e/test_30_tasks.py` | Synthetic 30-task pack (log, config, runbook). |
| Stress qualification | `backend/tests/qualification/sandbox_stress.sh` | Kernel-backed stress run of the sandbox: real OOM kill at the 4 GiB ceiling, 128-task ceiling, 2-CPU throttling, 15 s deadline, live network denial, filesystem confinement. Not a pytest suite; run on the target host. |
| Restore qualification | `backend/tests/qualification/restore_drill.py` | Backup to clean-staging restore drill: byte-for-byte verification, manifest/hash chain, pre-write rejection of unsafe archives, measured backup/restore timings. Supports `--real` and `--scale N`. Not pytest-collected. |

Approximate test-function counts in the current tree (measured 2026-09-24;
parametrized cases expand to more collected tests):

| Tier | Functions |
|---|---:|
| tier1_unit | 139 |
| tier2_sandbox | 37 |
| tier3_concurrency | 72 |
| tier4_recovery | 28 |
| e2e | 2 |

`backend/tests/test_platform.py` adds the live end-to-end sections outside the
tiers. The whole suite collects **488 tests** (480 passed, 8 skipped on the
2026-09-24 run — see [status/TEST_READY.md](status/TEST_READY.md)).

The Harness integration has `packages/harness-integration/tests/`:
`policy.test.mjs`, `audit.test.mjs`, `gateway.test.mjs`, `admin.test.mjs`,
`branding.test.mjs`, `session-persistence.test.mjs`.

## 2. Running tests

```bash
# Full backend suite
backend/.venv/bin/python3 -m pytest -q

# Focused, no external services
backend/.venv/bin/python3 -m pytest \
  backend/tests/tier1_unit \
  backend/tests/tier3_concurrency/test_approval_gate_lifecycle.py \
  backend/tests/tier4_recovery/test_outbox_resilience.py -q

# Synthetic 30-task pack
backend/.venv/bin/python3 -m pytest backend/tests/e2e/test_30_tasks.py -q

# Sandbox suite (host prerequisites required)
backend/.venv/bin/python3 -m pytest backend/tests/tier2_sandbox -q

# Kernel-backed sandbox stress qualification (host prerequisites required)
make stress-sandbox

# Harness integration
cd packages/harness-integration && node --test tests/

# Platform start/stop end-to-end
./platform.sh test
```

Live tests start the services and read the current bearer keys from the ignored
local key files. Start services with `./platform.sh start`, wait for LiteLLM on
port 4000, run the test, then `./platform.sh stop`.

## 3. Recorded results

From [`status/TEST_READY.md`](status/TEST_READY.md) — the table below is the
historical restricted-workspace run; the current host results are in that file:

| Check | Result | Scope |
|---|---:|---|
| Full suite (before latest adversarial additions) | 212 passed, 23 skipped, 5 socket checks blocked | The five failures were `PermissionError` on local socket creation. |
| Focused gateway, quota, approval, P1, target adapter, restore | 137 passed | Shared-state outages, replay, staged content, revocation, archive checks. |
| Synthetic 30-task pack | 31 cases passed | Fixture-driven, not an owner-run field evaluation. |
| Live local auth + quota suite | 19 passed | Services started together outside the restricted sandbox. |
| Live authenticated chat | HTTP 200 | Traefik → auth → agent → LiteLLM → simulated inference. |
| Source checks | Passed | Shell syntax and Python compilation. |

## 4. Behaviours covered

- Approvals bind the exact user, session, command, workspace and 5-minute
  expiry, and are consumed once.
- Direct backend routes authenticate credentials; forwarded identity headers
  alone cannot impersonate an administrator.
- Server-assigned `0700` workspaces reject client-selected paths and symlinks.
- The audit outbox survives a collector outage and drains in order; delivery is
  at-least-once with `event_id` dedup.
- An inference-gateway rejection does not fall back to direct inference.
- Shared quota outages return `503` and reject new concurrency/RPM/daily
  admissions.
- Target-adapter routes authenticate callers, bind mutations to the requester,
  and derive reviewer authority from authenticated identity.
- Valkey-backed approvals fail closed during shared-store outages.
- P1 revocation invalidates every token issued to the user.
- Restore rejects missing components and unsafe archive paths before writing.
- Per-user quota overrides are validated against fixed bounds, shared across
  managers/workers, applied to concurrency, RPM and daily admissions, and fail
  closed on corruption or a store outage.
- Every enumerated tool, approval, target-adapter and admin path emits a
  canonical audit event for success **and** failure; unaudited paths are pinned
  by sentinel tests. See [status/AUDIT_CENSUS.md](status/AUDIT_CENSUS.md).
- Config deployment rejects a destination created, deleted or changed after
  approval (including line-ending-only changes), preserves the existing Unix
  mode and owner, creates new files `0600`, and aborts before replacement if
  those attributes cannot be applied.

## 5. Environment limits (current)

- **23 skipped** tests reflect missing delegated writable cgroups v2 and
  Bubblewrap namespace support, plus services intentionally stopped during a
  normal unit run.
- **5 blocked** socket tests need a host that permits loopback binding.
- Tier 2 sandbox tests skip when host prerequisites are missing; a separate
  runner check verifies commands fail closed instead of running unbounded.
- The kernel-backed stress run (`make stress-sandbox`) now proves the
  4 GiB total-memory ceiling (with `memory.swap.max=0`), the 128-task
  ceiling, 2-CPU throttling, the 15 s deadline, live network denial and
  filesystem confinement on the runner's `systemd-run` leg. The raw
  cgroup-delegation leg is verified fail-closed (abort 126 before executing)
  when the session has no delegated writable subtree; its enforcement path
  still needs a run under the platform account's delegated subtree on the
  production host.

## 6. Outstanding qualification work

1. Run Tier 2 on a host with delegated cgroups and unprivileged namespaces.
2. Measure and verify quota-lease ownership/renewal at runtime under load.
3. Run a real, owner-scored 30-task evaluation.
4. ~~Perform a kernel-backed sandbox stress run.~~ Done on the
   `systemd-run` leg (see §5); re-run on the production host to qualify the
   raw cgroup-delegation leg under the platform account's delegated subtree.
5. ~~Verify audit query completeness.~~ The per-path census is published in
   [status/AUDIT_CENSUS.md](status/AUDIT_CENSUS.md) and pinned by
   `test_audit_census.py`; the open gaps (runtime chat turns, LiteLLM failures,
   quota denials, auth login/logout/`401`s, cancellation) and live-store query
   completeness remain open.
6. ~~Run a full restore drill against clean staging and measure RTO.~~ Done at the file level (2026-09-24, `backend/tests/qualification/restore_drill.py`, all modes pass). Post-restore service bring-up from restored state and production-sized data remain open.
7. Qualify the target adapter's privileged execution boundary and a clean
   staging target deployment.

See [security.md](security.md#9-known-gaps) for the security framing.
