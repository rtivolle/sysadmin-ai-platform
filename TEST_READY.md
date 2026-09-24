# Verification status

Last checked: 2026-09-24. Run tests with `backend/.venv/bin/python3 -m pytest -q` from the repository root.

| Check | Result | Scope |
|---|---:|---|
| Full suite in restricted workspace, before latest adversarial additions | 212 passed, 23 skipped, 5 socket checks blocked | The five failures raised `PermissionError` when the sandbox denied local socket creation |
| Focused gateway, quota, approval, P1, target adapter, and restore checks | 137 passed | Includes shared-state outages, adversarial replay, staged content, revocation, and archive checks |
| Synthetic 30-task pack | 31 pytest cases passed | Fixture-driven log, config, and runbook tasks; not an owner-run field evaluation |
| Live local auth and quota suite | 19 passed | Services started together outside the restricted test sandbox and stopped afterward |
| Live authenticated chat | HTTP 200 | Traefik → auth → agent → LiteLLM → simulated inference |
| Source checks | Passed | Shell syntax and Python compilation |

The 23 skips reflect missing delegated writable cgroups v2 and Bubblewrap namespace support in this test environment, plus services that are intentionally stopped during the normal unit run. The five socket failures are environment restrictions, so those tests still need a host that permits local loopback binding. The sandbox tests now skip only when host prerequisites are missing; a separate runner check verifies that commands fail closed instead of running without cgroup limits. The earlier live smoke check exercised authentication and quota behavior with Valkey running. It did **not** prove the 4 GiB/128-process/two-CPU limits on a production host. Run the Tier 2 sandbox suite on a host with delegated cgroups and unprivileged namespaces before deployment.

## Reproduce

```bash
backend/.venv/bin/python3 -m pytest -q
backend/.venv/bin/python3 -m pytest backend/tests/e2e/test_30_tasks.py -q
backend/.venv/bin/python3 -m pytest backend/tests/tier2_sandbox -q
```

For live tests, start the services in the same host session, wait for LiteLLM on port 4000, run `backend/.venv/bin/python3 -m pytest backend/tests/tier3_concurrency/test_empirical_challenger.py -q`, and stop services with `./platform.sh stop`. The test reads current bearer keys from ignored local files; it no longer assumes deterministic default tokens.

## Behaviors covered by focused tests

- Approved operations are bound to the exact user, session, command, workspace, and five-minute expiry; the approval is consumed once.
- Direct backend routes authenticate credentials themselves, and forwarded identity headers alone cannot impersonate an administrator.
- Server-assigned `0700` workspaces reject client-selected paths and symlinks.
- The audit outbox persists records during collector outages and the production replay worker drains them in order. Delivery is at least once and may duplicate an accepted event after a crash; `event_id` supports deduplication.
- An inference gateway rejection does not fall back to direct inference and bypass quotas.
- Shared quota outages return 503 from ForwardAuth and reject new concurrency, RPM, and daily-budget admissions.
- Target adapter routes authenticate callers, bind mutations to the requester, and derive approval reviewer authority from authenticated identity.
- Valkey-backed approvals fail closed during shared-store outages; staged configuration reads reject paths outside the assigned workspace and symlinks.
- P1 revocation invalidates every token issued to the user; restore rejects missing backup components and unsafe archive paths before writing to the target.

## Outstanding qualification work

This is not a production acceptance certificate. The scoped target adapter and shared approval store are implemented, but a least-privilege privileged boundary, multi-worker Valkey integration run, and clean staging target deployment remain to be qualified. Lease ownership and renewal, plus atomic daily token reservations and settlement, were added after the last test batch and still need runtime verification. The inference service is simulated unless an upstream vLLM endpoint is configured; NVIDIA Dynamo and DeepSeek Harness are not implemented here. A real owner-scored 30-task evaluation, a kernel-backed sandbox stress run, an audit query completeness check, and a full restore drill against clean staging remain to be done. The existing disaster recovery tests check fixture logic rather than measuring an RTO under four hours.
