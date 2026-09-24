# Verification status

Last checked: 2026-09-24. Run tests with `backend/.venv/bin/python3 -m pytest -q` from the repository root.

## Code-inspection repair run (2026-09-24)

- Initial `make test` on the inherited working tree: **457 passed, 2 failed,
  8 skipped**. The failures were audit-census test wiring: a keyword-only fake
  gate rejected a positional completion call, and a source-inspection test
  imported the exported FastAPI router object rather than its Python module.
  The completion call now uses the same keyword convention as the other paths;
  the census imports the module explicitly.
- Six new regression cases failed before the deployment fixes: a target created
  or deleted after approval was overwritten/recreated, line-ending-only changes
  escaped the base hash, `0600`/`0640` files became `0644`, and rejected tampered
  content still created destination directories.
- `backend/.venv/bin/python3 -m pytest backend/tests/tier1_unit backend/tests/tier3_concurrency -q`
  after those fixes: **277 passed, 1 skipped**. Additional regression coverage
  checks private new-file creation, unchanged CRLF content and exact backup
  bytes, and abort-before-replacement when permission preservation fails.
- Final repair-run `make test`, before the admin-controls feature and including
  all nine destination-integrity regression cases: **468 passed, 8 skipped,
  0 failed** (42.07 s). Skipped live checks remained unverified by that run.
- `docs/status/AUDIT_CENSUS.md` now enumerates every audited path, its canonical
  event and the five named open gaps, and `test_audit_census.py` pins them.
  `test_admin_quotas.py` covers admin authorization, bounds validation,
  cross-manager sharing, fail-closed corruption/outage and (when live Valkey is
  reachable) multi-worker enforcement. `test_config_deployment_integrity.py`
  covers the destination conflict and permission-preservation cases.
- These changes qualify execution-time destination-state validation, Unix
  permission handling and the admin quota path in temporary fixtures. They do
  not qualify the privileged boundary, ACL preservation, or external-writer
  races between validation and replacement. Real-model/GPU qualification and
  production staging deployment were not measured in this run.

## Admin controls implementation and live deployment (2026-09-24)

- `make test`: **480 passed, 8 skipped, 0 failed** (41.65 s), 488 collected.
  Final current-tree recheck: **480 passed, 8 skipped** (41.66 s).
  The skipped live-stack checks are not claimed as passing.
- Tier 1 + tier 3: **292 passed, 1 skipped** (14.77 s).
  The 12 new admin-quota tests include bounds/auth/audit checks, corrupt-store
  and outage rejection, policy sharing between managers, and live Valkey
  enforcement of changed concurrency, RPM and daily reservation limits for an
  isolated test identity. TPM values are passed to LiteLLM's custom-auth
  limits; a live TPM load test was not performed.
- `env PATH="/home/linuxbrew/.linuxbrew/bin:$PATH" make harness-test harness-verify`:
  **48 Node tests passed; 8/8 real-dsh checks passed** initially. A final run
  against the current working tree, including the additional harness-surface
  changes, passed **56 Node tests and 8/8 real-dsh checks**. The initial invocation
  could not find Node; including its installed directory in PATH resolved both
  the test runner and child-process launch failures.
- Browser checks using fixture API responses exercised quota save/reset,
  preservation of unsaved edits during refresh, simulated-model labeling,
  start controls for provisioned users without an instance, and service
  start/stop/restart controls. These were UI checks, not live service mutations.
- Restarted auth gateway, agent platform, LiteLLM and harness gateway to load
  the feature. The gateway remains on `0.0.0.0:3085`, with the agent platform
  on loopback `3090`; Traefik remained stopped.
- Live authenticated admin API checks passed: master login with backend
  verification; quota snapshots for **11 users**; quota save/read-back using
  the existing policy (no limit changes); invalid quota rejection; runtime
  inventory and backend port `3090`; logout followed by anonymous `401`.
  The live LiteLLM catalogue returned **fast-model** and **heavy-model**;
  inference health reported **simulated**. No real GPU-model qualification or
  model installation/removal is claimed.
- After clearing the browser fixtures, the authenticated live page rendered
  **11 quota forms**, **11 runtime-user rows** and both model aliases, with no
  console-page error. The live page was left on the Quotas tab.

## Device/driver survey + survey API (2026-09-24)

- `backend/services/hardware_survey.py` now also reports PCI accelerators
  (`lspci -nnk`, display/3D/processing classes only, with vendor:device IDs and
  kernel driver/modules), the accelerator driver/toolkit stack (loaded kernel
  modules; NVIDIA driver/CUDA runtime/CUDA toolkit; AMD `rocminfo`/`rocm-smi`;
  Intel render nodes), installed ML package versions (metadata only, no import),
  and model-store capacity plus recursively summed direct-subdirectory sizes.
  The Markdown export gains sections 5–8.
- New admin-only `GET /api/v1/survey` on the agent platform (authenticated user
  `403`, anonymous `401`), cached ~10 s with `?refresh=1` to bypass.
- `backend/tests/tier1_unit/test_hardware_survey.py`: **7 passed** (0.56 s) —
  lspci classification/filtering, missing-tool non-fatality, NVIDIA version
  parsing, model-storage sizing, Markdown export sections, and endpoint
  auth plus cache behaviour.
- Current-tree full suite: **487 passed, 8 skipped, 0 failed** (39.87 s), 495
  collected; `compileall` OK; `git diff --check` OK.
- The survey parsers are exercised against fixtures, not live hardware, and the
  survey itself makes no GPU-qualification claim. A separate direct 9B
  llama.cpp/CUDA smoke test was performed after this survey run (details below);
  this does not qualify the platform's vLLM integration.

## Direct 9B model load smoke test (2026-09-24)

- Loaded cached `Qwythos-9B-Claude-Mythos-5-1M-MTP-Q6_K.gguf` directly in
  llama.cpp `0.4.1-dev`, isolated on loopback port `8010`, context `2048`, one
  slot, CUDA backend, `--n-gpu-layers all`, Flash Attention enabled. GGUF reports
  **9,197,093,888 parameters**, **Q6_K**, **7,606,849,536 bytes**. CUDA 13
  libraries were present under the local Unsloth environment but not on the
  system loader path; setting `LD_LIBRARY_PATH` for this process made the RTX
  3060 visible. The 12 GiB GPU showed **6,558 MiB** used by llama-server.
- `/health` returned `ok`; `/v1/models` confirmed the loaded GGUF and metadata.
  A short OpenAI-compatible chat request with thinking disabled returned
  **“2 + 2 equals 4.”** (9 completion tokens, 0.42 s). A short first request
  without thinking disabled exhausted its 64-token output budget in reasoning
  and had empty visible `content`; allow sufficient output tokens or disable
  thinking with `chat_template_kwargs.enable_thinking=false` for this test.
- The server timing for the successful short answer reported **40.61 tokens/s**;
  this 9-token smoke-test timing is **not a performance benchmark**. llama.cpp
  warned that additional `blk.32`/MTP tensors were unused; speculative/MTP
  decoding was not qualified. The test server was stopped, port `8010` is free,
  and GPU memory returned to idle. The platform inference service on port 8000
  still reports `local-simulated`: the model was not integrated into LiteLLM,
  the inference service, or the admin model catalog. vLLM integration remains
  untested.

## Local model manager (2026-09-24)

- New admin-only lifecycle in `backend/services/model_manager/`: JSON registry
  (0600, atomic), HuggingFace `snapshot_download` with token file + disk
  preflight, per-model `vllm serve` supervision (port allocation, `/health` +
  child-liveness readiness, SIGTERM→SIGKILL, log tail), and a managed LiteLLM
  `model_list` block with an automatic `platform.sh service litellm restart`.
  The inference engine lists/routes running local models.
- `backend/tests/tier1_unit/test_model_manager.py`: **14 passed** (1.20 s) —
  name/repo/revision validation and path confinement, corrupt-registry
  quarantine, download success/failure/preflight, concurrent-download rejection,
  vLLM command building and start/stop/unhealthy lifecycle with injected
  process + health seams, LiteLLM managed-entry add/remove, inference routing,
  and admin-only API auth plus register/download/start/delete.
- Current-tree full suite: **501 passed, 8 skipped, 0 failed** (41.13 s), 509
  collected; `compileall` OK.
- Admin console: two new tabs (*Modèles locaux*, *Matériel*) and gateway proxy
  routes (`/api/admin/local-models*`, `/api/admin/survey`). Harness node suite:
  **54 passed** (`PATH="/home/linuxbrew/.linuxbrew/bin:$PATH" node --test tests/`;
  was 52 before the two new proxy tests). The `@oracle`/specialist lanes were
  unavailable this session (account usage limit), so these changes were
  self-reviewed rather than gate-reviewed.
- **Not measured:** no real HuggingFace download and no real vLLM start
  occurred (vLLM is not installed on this host and egress was not exercised).
  The lifecycle is proven against injected process/HTTP seams only. There is no
  GPU-fit guarantee and no per-model GPU isolation. See
  [../model-management.md](../model-management.md).

## Previously recorded checks

| Check | Result | Scope |
|---|---:|---|
| Backup / clean-staging restore drill (2026-09-24) | 28 checks passed, 0 failed, synthetic + real + scale modes | `backend/tests/qualification/restore_drill.py`: real host state (2,375,664 B / 823 paths, 4 components) backed up in 0.335 s and restored to clean staging in 0.249 s (~9.6 MB/s, many-small-file dominated); archive sha256 == manifest; per-component aggregate SHA-256 == independently recomputed trees; restored trees byte-for-byte identical; 7-stage restore sequence exact; permission contract holds (0700 roots, 0600 direct secret/state files). Linear extrapolation: ~2.2 GiB ≈ 250 s for the file-copy phase (service bring-up not included). Built-in `dr_drill.py` against clean staging: success=true, RPO/RTO passed, all 4 health checks healthy (0.256 s, matches within noise). Negative cases (missing component, missing manifest entry, tampered content, `..` traversal, symlink, multi-root, truncated archive) all rejected before any target write (`target_paths_written=0`). Live `BGSAVE` / `/snapshot/create` paths not exercised (credentials / running API version); filesystem-copy fallbacks verified. Workspace contents and runbooks are not backup-covered (runbooks are Git-tracked); see docs/backup-restore.md |
| Kernel-backed sandbox stress qualification (2026-09-24) | systemd-run leg: all scenarios passed; raw cgroup-delegation leg: fail-closed verified, enforcement not measurable in this session | `make stress-sandbox` (`backend/tests/qualification/sandbox_stress.sh`): 2 GiB under-ceiling allocation completed; 6 GiB over-ceiling allocation OOM-killed at ~3.75 GiB after the `memory.swap.max=0` fix (it survived before the fix — see below); of 400 spawn attempts exactly 122 ran concurrently (126 namespace tasks, ceiling 128) with 278 fork failures; 4 CPU spinners throttled to user/real ratio 2.0; 60 s sleep killed at 15.1 s; live host loopback listener unreachable from the sandbox; `/usr` read-only, host `/etc` absent, mount denied; 2 processes visible in `/proc` |
| Full backend suite against the live local stack (2026-09-24) | 356 passed, 0 failed, 0 skipped | `backend/.venv/bin/python3 -m pytest -q` with Valkey, VictoriaLogs, SeaweedFS, inference, ForwardAuth, Traefik, and the agent platform reachable |
| Full backend suite, Valkey reachable and other backend services stopped (2026-09-24) | 423 passed, 13 skipped | 436 tests collected. The 13 skips are the eleven live auth/Traefik/Valkey challenger checks plus the seven `test_platform.py` sections, which skip when the stack is down and fail on a recorded error instead of passing silently; `tier2_sandbox/test_m4_empirical_challenger.py` contributes 80 independent challenge cases |
| Full backend suite, every backend service stopped (2026-09-24) | 416 passed, 20 skipped | Same 436 tests: the eleven live challenger checks now skip too because Valkey is down. Both configurations are honest; the difference is exactly the live-Valkey coverage |
| Full backend suite with the stack up except Traefik (2026-09-24) | 428 passed, 8 skipped | The harness live run left Traefik stopped on purpose (see the host note); only the seven `test_platform.py` sections and one live auth/Traefik challenger check skip |
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
| Custom dsh harness integration (2026-09-24) | 56 node tests passed; 8/8 real-`dsh` checks; 37/37 live gateway checks | Profile composes with the `dsh-plugin-sysadmin` bundle, the plugin's load banner prints, the web surface binds and refuses an unauthenticated request, and the served index carries the Mila title/favicon; the JS command policy matches the Python gate action-for-action. The live gateway run covered two concurrent sysadmins on isolated instances (the verifier used 3210/3211 in the 3210–3260 scratch range; the running gateway serves the same users in the 3180–3280 range), launch handoff, sessions surviving a gateway SIGTERM+restart with instance re-adoption on the same port, Mila-branded login/admin pages, master-key admin login with backend verification, overview probes, an instance restart that reused the port, a real approval requested with a user key and decided in the console, audit query, `platform.sh service` restart, and session revocation. The gateway preserves the browser `Host` (rewriting it to the loopback instance port made the harness Origin fence answer 403 and kept the UI stuck behind login); per-instance trusted hosts carry the host's LAN addresses, and the verifier reuses one gateway port across its restart because the harness cookie is bound to the gateway authority. A real browser on `http://192.168.14.159:3085/` renders the signed-in user (`sysadmin-01`) in the sidebar, the Mila logo in the sidebar and hero brand seats, the tab pinned to *Mila — Sysadmin AI*, a files panel listing the user's workspace (`GET /api/sysadmin/surface`, paths confined to the per-user root), and no DeepSeek brand text, with zero console errors |

The 23 skips in the table's historical row reflect a restricted workspace without delegated writable cgroups v2 or Bubblewrap namespaces, plus services intentionally stopped during a unit run; the five socket failures there were environment restrictions. On the current host those prerequisites exist, so the sandbox suite executes and passes, and the live stack run skips nothing. Sandbox tests still skip only when host prerequisites are missing, and a separate runner check verifies that commands fail closed instead of running without cgroup limits. A kernel-backed stress run on 2026-09-24 (`backend/tests/qualification/sandbox_stress.sh`, via `make stress-sandbox`) proves the ceilings on the runner's `systemd-run` leg: a 6 GiB allocation is OOM-killed at the 4 GiB ceiling, 400 spawn attempts are bounded to the 128-task ceiling with fork failures, four CPU spinners are throttled to a 2-CPU ratio, a 60 s sleep is killed at the 15 s deadline, a sandboxed client cannot reach a live listener on the host loopback, and the filesystem view is confined to the workspace. That run caught a real gap first: `memory.max` alone did not bound total memory — RSS pinned at 4 GiB while swap usage climbed without bound and the 6 GiB allocation survived. The runner now also enforces `memory.swap.max=0` (`MemorySwapMax=0` on the systemd leg; read-back-verified on the raw leg, fail-closed if it cannot be installed), and the OOM kill at the ceiling was re-measured after the fix. The raw cgroup-delegation leg could not be measured in the qualifying session (no delegated writable subtree in that cgroup); it was verified to fail closed instead — abort 126 before executing the command. Re-run `make stress-sandbox` under the platform account's delegated subtree on the production host to qualify the raw leg.

### Host note: port 3080

The agent platform defaults to port 3080, which is also the default port of the DeepSeek Harness web surface. On a host where the harness already owns 3080, `agent_tools` cannot bind its port and the Traefik `agent-service` route fails. The live stack run above was performed with the identical agent-platform code on port 3090 and a Traefik instance whose `agent-service` URL pointed at 3090; every other service ran on its default port. Keep 3080 free for the platform, or point both `platform.sh` and `backend/config/traefik/dynamic.yml` at the same alternative port.

`platform.sh` now takes that workaround into account: `SYSADMIN_AGENT_PORT=3090 ./platform.sh service agent_tools start` binds the agent platform on 3090, and `SYSADMIN_AGENT_PORT=3090 ./platform.sh harness` starts the harness gateway with `SYSADMIN_BACKEND_URL=http://127.0.0.1:3090` so every per-user plugin instance talks to the right backend. The 2026-09-24 harness live run used exactly this setup and left Traefik stopped on purpose (its `agent-service` route still points at 3080), which is why the `test_platform.py` sections and one challenger check skip above. Full-stack runs with Traefik require pointing the route at the same alternative port. The gateway also supports per-service control: `./platform.sh service <name> {start|stop|restart|status}`.

## Reproduce

```bash
backend/.venv/bin/python3 -m pytest -q
backend/.venv/bin/python3 -m pytest backend/tests/e2e/test_30_tasks.py -q
backend/.venv/bin/python3 -m pytest backend/tests/tier2_sandbox -q
make harness-test          # node --test packages/harness-integration/tests/
make harness-verify        # composes + boots the real dsh profile
# Live harness gateway, with auth and the agent platform running:
node packages/harness-integration/scripts/verify-live-gateway.mjs \
  --auth-url http://127.0.0.1:3081 --backend-url http://127.0.0.1:3090
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
- Admin quota endpoints require `sysadmin-admin`, validate an override against fixed bounds, store it in Valkey so it is shared across managers/workers, restore defaults on `{}`, and fail closed on a corrupt policy or a store outage; each update emits a `quota_update` audit event and a rejected write does not mutate the stored policy.
- The audit census pins a canonical field set for the Python and JS writers and asserts an event for every enumerated tool, approval, target-adapter and admin path for success and failure; the five still-unaudited paths are asserted absent so the census cannot drift.
- A destination created, deleted or changed (including line-ending-only changes) between approval and execution is rejected; replacements preserve the target's Unix mode and owner, new configurations are created `0600`, and a failure to apply those attributes aborts before replacement.
- Model registration validates the served name, HuggingFace repo and revision and confines paths to the model store; a corrupt registry is quarantined; a download is preflighted for disk space and reuses the Hub cache; a model must be downloaded before its vLLM server starts and only becomes selectable while running. Every model mutation is admin-only and audited.
- Valkey-backed approvals fail closed during shared-store outages; staged configuration reads reject paths outside the assigned workspace and symlinks.
- P1 revocation invalidates every token issued to the user; restore rejects missing backup components and unsafe archive paths before writing to the target.
- A shared-store client is published only after it authenticates, so a concurrent caller can no longer borrow an unverified client and silently drop a lease release.
- Releasing a concurrency lease falls back to process-local bookkeeping when the shared store is momentarily unusable; in-flight leases cannot strand until their TTL expires.
- Cleanup for a disconnected or cancelled request reclaims the lease and the registry entry before its first await, because cancellation is re-delivered at that await.
- Daily token admission reserves capacity atomically and settlement replaces the estimate with actual usage exactly once, attributed to the admission day.
- `backend/tests/test_platform.py` skips under pytest when the live stack is down and raises when a section records a failure, so the live end-to-end sections cannot pass vacuously; as a script it still reports through PASS/FAIL lines and its exit code.
- Harness browser sessions and admin sessions persist to JSONL (0600, temp+fsync+rename) and survive a gateway restart; expired records are pruned and rewritten at load, and a corrupt file is quarantined as `.corrupt-<timestamp>.bak` instead of crashing the gateway.
- A harness instance is re-adopted after a gateway restart only when its recorded pid is alive **and** its port answers; a port answering under a dead pid is logged as `unknown owner` and left alone. An admin restart keeps the user's port when it is still free, supervised crashes restart with exponential backoff, and per-user logs rotate at 2 MiB.
- The admin console authenticates with the master key (constant-time compare plus a backend bearer probe), rejects cross-origin mutations (JSON content type, `X-Sysadmin-Admin: 1`, same-origin `Origin`), exposes browser sessions only as SHA-256 handles, never returns the master token or user keys, and proxies approvals/audit/service restarts with the master bearer; an unreachable VictoriaLogs surfaces as 503 with the outbox note.
- Effective two-user isolation is asserted live: distinct sessions, distinct ports, each session reaching only its own harness surface, and each user's LiteLLM key authenticating alone.
- The M4 challenger pack asserts a specification divergence rather than hiding it: the sandbox binds `/usr` read-only plus `/etc/resolv.conf` and `/etc/ssl` only, so a sandboxed process can still write inside its private `/etc`. The benchmark report's earlier whole-`/etc` read-only claim was corrected, and the runner's deadline is 15 s with a 5 s kill grace, not 20 s.

## Outstanding qualification work

The items below are tracked as a prioritized, one-agent-per-item program in [../plans/PRODUCTION_READINESS.md](../plans/PRODUCTION_READINESS.md).

This is not a production acceptance certificate. The scoped target adapter and shared approval store are implemented, but a least-privilege privileged boundary, multi-worker Valkey integration run, and clean staging target deployment remain to be qualified. Lease ownership and renewal, plus atomic daily token reservations and settlement, were verified on 2026-09-24: the release path no longer strands a lease when the shared store is momentarily unusable, a cancelled or disconnected request reclaims its lease during cleanup, and the reservation lifecycle is covered by process-local and live-Valkey tests. The inference service is simulated unless an upstream vLLM endpoint is configured; NVIDIA Dynamo is not implemented here, and the DeepSeek Harness integration lives in `packages/harness-integration/` outside the `platform.sh` stack (started with `./platform.sh harness`, stopped with `./platform.sh harness-stop`). That integration's multi-user path is now verified end-to-end: two concurrent sysadmins against the running auth gateway, isolated harness processes, a real approval requested with a user key and decided in the Mila-branded admin console, per-user model traffic routed to LiteLLM, and browser sessions plus instances surviving a gateway restart (45 node tests, 8/8 real-`dsh` checks, 37/37 live gateway checks on 2026-09-24). The live harness run left Traefik stopped and used the agent platform on port 3090 as described in the host note, so the Traefik-routed platform sections skipped rather than passing. A real owner-scored 30-task evaluation remains to be done, and the event-by-event audit census is now published in [AUDIT_CENSUS.md](AUDIT_CENSUS.md) with five named paths still unaudited. The kernel-backed sandbox stress run is done on the systemd-run leg (above); only the raw cgroup-delegation leg still needs a run on a host with a delegated writable subtree. The clean-staging restore drill is done at the file level with measured timings (above); what remains open is post-restore service bring-up from restored state and a production-sized data set, so the end-to-end RTO under four hours is supported by extrapolation, not fully measured.
