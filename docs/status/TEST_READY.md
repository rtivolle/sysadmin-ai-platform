# Verification status

Last checked: 2026-09-24. Run tests with `backend/.venv/bin/python3 -m pytest -q` from the repository root.

| Check | Result | Scope |
|---|---:|---|
| Full backend suite against the live local stack (2026-09-24) | 356 passed, 0 failed, 0 skipped | `backend/.venv/bin/python3 -m pytest -q` with Valkey, VictoriaLogs, SeaweedFS, inference, ForwardAuth, Traefik, and the agent platform reachable |
| Full backend suite with no live stack (2026-09-24) | 423 passed, 13 skipped | 436 tests collected. The 13 skips are the six live ForwardAuth/Traefik checks plus the seven `test_platform.py` sections, which now skip when the stack is down and fail on a recorded error instead of passing silently; `tier2_sandbox/test_m4_empirical_challenger.py` contributes 80 independent challenge cases |
| Tier 2 sandbox suite on this host | 129 passed | Bubblewrap and delegated cgroups v2 are available here, so isolation, deadline, and cgroup-ceiling checks executed instead of skipping: 49 core checks plus the 80-case M4 challenger pack |
| Live concurrency-lease lifecycle against Valkey | Passed | Two leases admitted, a third rejected with the 2/2 ceiling, lease renewal accepted, both releases returned the user and cluster lease sets to zero, and re-admission succeeded immediately |
| Daily token reservation and settlement | 8 passed (6 process-local, 2 against live Valkey) | Atomic admission, duplicate-ID rejection, exactly-once settlement, capacity release, and fail-closed behaviour when the shared store is required but unreachable |
| Live stack end-to-end (`backend/tests/test_platform.py` checks) | All sections passed | Valkey, VictoriaLogs ingestion, SeaweedFS master + S3, inference models + SSE, ForwardAuth, bounded tools, sandboxed execution, destructive-command 403, approval-gate approval, and Traefik routing/ForwardAuth. See the host note on port 3080 |
| Full suite in restricted workspace, earlier run | 212 passed, 23 skipped, 5 socket checks blocked | The five failures raised `PermissionError` when that restricted sandbox denied local socket creation |
| Focused gateway, quota, approval, P1, target adapter, and restore checks | 137 passed | Includes shared-state outages, adversarial replay, staged content, revocation, and archive checks |
| Synthetic 30-task pack | 31 pytest cases passed | Fixture-driven log, config, and runbook tasks; not an owner-run field evaluation |
| Live local auth and quota suite | 19 passed | Services started together outside the restricted test sandbox and stopped afterward |
| Live authenticated chat | HTTP 200 | Traefik → auth → agent → LiteLLM → simulated inference |
| Source checks | Passed | Shell syntax and Python compilation |
| Custom dsh harness integration | 19 node tests passed; 7/7 real-`dsh` checks passed | Profile composes with the `dsh-plugin-sysadmin` bundle, the plugin's load banner prints, the web surface binds and refuses an unauthenticated request; the JS command policy matches the Python gate action-for-action |

The 23 skips in the table's historical row reflect a restricted workspace without delegated writable cgroups v2 or Bubblewrap namespaces, plus services intentionally stopped during a unit run; the five socket failures there were environment restrictions. On the current host those prerequisites exist, so the sandbox suite executes and passes, and the live stack run skips nothing. Sandbox tests still skip only when host prerequisites are missing, and a separate runner check verifies that commands fail closed instead of running without cgroup limits. The live runs exercised authentication, quota behaviour, and sandboxing with the services running; they did **not** prove the 4 GiB/128-process/two-CPU limits under a sustained kernel-backed stress run on a production host. Run that stress qualification on the deployment host before relying on the ceilings.

### Host note: port 3080

The agent platform defaults to port 3080, which is also the default port of the DeepSeek Harness web surface. On a host where the harness already owns 3080, `agent_tools` cannot bind its port and the Traefik `agent-service` route fails. The live stack run above was performed with the identical agent-platform code on port 3090 and a Traefik instance whose `agent-service` URL pointed at 3090; every other service ran on its default port. Keep 3080 free for the platform, or point both `platform.sh` and `backend/config/traefik/dynamic.yml` at the same alternative port.

## Reproduce

```bash
backend/.venv/bin/python3 -m pytest -q
backend/.venv/bin/python3 -m pytest backend/tests/e2e/test_30_tasks.py -q
backend/.venv/bin/python3 -m pytest backend/tests/tier2_sandbox -q
node --test packages/harness-integration/tests/
node packages/harness-integration/scripts/verify-harness.mjs
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
- A shared-store client is published only after it authenticates, so a concurrent caller can no longer borrow an unverified client and silently drop a lease release.
- Releasing a concurrency lease falls back to process-local bookkeeping when the shared store is momentarily unusable; in-flight leases cannot strand until their TTL expires.
- Cleanup for a disconnected or cancelled request reclaims the lease and the registry entry before its first await, because cancellation is re-delivered at that await.
- Daily token admission reserves capacity atomically and settlement replaces the estimate with actual usage exactly once, attributed to the admission day.
- `backend/tests/test_platform.py` skips under pytest when the live stack is down and raises when a section records a failure, so the live end-to-end sections cannot pass vacuously; as a script it still reports through PASS/FAIL lines and its exit code.
- The M4 challenger pack asserts a specification divergence rather than hiding it: the sandbox binds `/usr` read-only plus `/etc/resolv.conf` and `/etc/ssl` only, so a sandboxed process can still write inside its private `/etc`. The benchmark report's earlier whole-`/etc` read-only claim was corrected, and the runner's deadline is 15 s with a 5 s kill grace, not 20 s.

## Outstanding qualification work

This is not a production acceptance certificate. The scoped target adapter and shared approval store are implemented, but a least-privilege privileged boundary, multi-worker Valkey integration run, and clean staging target deployment remain to be qualified. Lease ownership and renewal, plus atomic daily token reservations and settlement, were verified on 2026-09-24: the release path no longer strands a lease when the shared store is momentarily unusable, a cancelled or disconnected request reclaims its lease during cleanup, and the reservation lifecycle is covered by process-local and live-Valkey tests. The inference service is simulated unless an upstream vLLM endpoint is configured; NVIDIA Dynamo is not implemented here, and the DeepSeek Harness integration lives in `packages/harness-integration/` outside the `platform.sh` stack. That integration's multi-user path has been verified with real harness boot and a stubbed instance manager, but not yet end-to-end with two concurrent users against a running auth gateway and LiteLLM. A real owner-scored 30-task evaluation, a kernel-backed sandbox stress run, an event-by-event audit completeness census, and a full restore drill against clean staging remain to be done. The existing disaster recovery tests check fixture logic rather than measuring an RTO under four hours.
