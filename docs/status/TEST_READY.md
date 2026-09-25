# Verification status

Last checked: 2026-09-25. Run tests with `backend/.venv/bin/python3 -m pytest -q` from the repository root.

## Machine-role install: one host or a three-machine split (2026-09-25)

- **What changed.** The topology is now a first-class install-time choice:
  `./install.sh --role all|web|inference|data` (and the same flags through
  `--tui`), plus `--dry-run` to resolve and print the plan without touching
  anything. Validation is fail-closed before any write (a split role bound to
  loopback, or one whose peers are loopback, exits `2`; an unreachable peer
  exits `1` with nothing applied — not even a `deployment.env`). The choice is
  recorded in `backend/config/roles/deployment.env` for **every** role, reused
  when `--role` is omitted (so `./update.sh`, which calls the installer with no
  flags, cannot re-role a host mid-update), and reversible: `--role all` resets
  the peers to loopback and restores the checked-in `traefik/dynamic.yml` and
  `valkey/valkey.conf` byte-for-byte. `render_config.py` now owns the
  non-loopback Valkey `bind` entries and finds the three managed Traefik
  upstreams by service name, so a LAN-address change replaces rather than
  accumulates and an operator-chosen port is still rewritten. `platform.sh`
  loads only the secrets a role holds (the `data` tier starts with
  `valkey-password.key` alone, and without the venv it renders the runtime
  Valkey config with the system `python3`), exports `VALKEY_HOST`/`VALKEY_PORT`
  and the `SYSADMIN_*` peer hosts that ForwardAuth and the harness admin console
  need, starts only the role's services, and lists peer endpoints in `status`.
  `update.sh`'s post-update key check is role-aware. The harness console health
  panel probes the peer Valkey/SeaweedFS hosts and omits the remote-only
  inference-engine probe. Docs updated: `docs/multi-host.md` (rewritten),
  design §6/§7/§11, `configuration.md`, `backup-restore.md`, docs index, and
  roadmap tracker PR-H1 → `in flight`.
- **Focused suite (this session, macOS host, Python 3.14):**
  `pytest backend/tests/tier1_unit/test_multihost_roles.py backend/tests/tier1_unit/test_multihost_render.py -q`
  → **44 passed, 2 skipped**, 2.0 s. The two skips are the optional ShellCheck
  checks (`shellcheck` is not installed here).
- **Full tier-1 unit tier with the same interpreter** (scratch venv: pytest
  9.1.1, pyyaml, rich, pydantic, fastapi, httpx, redis, uvicorn):
  **358 passed, 30 failed, 5 skipped**. A pristine `git archive HEAD` copy run
  with the same interpreter: **329 passed, 31 failed, 5 skipped**. Diffing the
  two FAILED sets: **no failure exists only in the changed tree**, and one
  failure exists only in the baseline
  (`test_m2_readonly_slice.py::test_forwardauth_verify_alias`, which passed in
  the changed tree). The 30 failures are environment-caused and pre-existing
  (Linux-only target-adapter/config-deployment permission semantics, absent
  `litellm`, absent `nvidia-smi`). The changed tree adds 28 collected tests
  (`test_multihost_roles.py`), all passing; the +29 net gain in passing tests
  includes the one baseline-only failure that passed in the changed tree.
- **Harness package:** `make harness-test` → **60 tests, 59 pass, 1 skip, 0
  fail** (Node v24.18.1). Pristine HEAD: 57 tests, 56 pass, 1 skip. The skip is
  pre-existing (`policy.test.mjs:72` needs `backend/.venv/bin/python3`). The
  Makefile target now passes a quoted glob
  (`--test 'packages/harness-integration/tests/*.test.mjs'`): Node 24 does not
  expand a directory argument, so the previous form was a pre-existing
  portability break on this host, not a behavioural change.
- **`make harness-verify` → 4/8 checks**, identical to the pristine-HEAD result
  (profile, plugin row, LiteLLM route and default-model routing pass; the four
  checks that boot the dsh web surface fail because no launch URL is produced in
  this session). Environment limit, unchanged by this work.
- **Syntax:** `bash -n` on `install.sh`, `backend/platform.sh` and `update.sh`
  is clean; `ast.parse` on every touched Python file is clean.
- **Not measured.** The topology itself has still never been run: no staged
  D → I → W bring-up, no suite pointed at a remote Valkey/VictoriaLogs, no
  firewall evidence, no key-copy or two-step revocation drill, and no check of
  the harness admin console against a live three-machine stack. Those remain the
  PR-H1 acceptance gate (checklist in `docs/multi-host.md` §7). `make test`
  with the repository venv could not run on this host at all —
  `backend/.venv/bin/python3` is a dangling symlink to a Linux path
  (`/home/linuxbrew/...`) — which is why an equivalent scratch interpreter was
  used. `make test-live`, `make benchmark` and every GPU/vLLM path were not run.

## NVIDIA setup and native vLLM configuration (2026-09-24)

- Added an Ubuntu NVIDIA PCI detection/driver installation helper with read-only
  default and explicit `--apply`, matching driver utilities, optional CUDA
  development toolkit, and TUI integration. Added native operator-owned vLLM
  YAML, installer version selection/skip, and a post-install CUDA probe.
- Session measurements: `make test` with bytecode/cache writes disabled and
  local socket access: **663 passed, 2 skipped**, one httpx cookie deprecation
  warning, 63.47s. Seven additional boundary tests were added after that run;
  the final focused GPU setup/configuration suite: **20 passed**, 0.09s.
- Required final tier 1 + tier 3 regression run (including the seven additional
  tests): **449 passed, 2 skipped**, one httpx warning, 30.50s.
  The two skips are optional ShellCheck checks (`shellcheck` is not installed),
  confirmed with a focused `-rs` run; Bash syntax checks passed separately.
- `bash -n install.sh`, installer help, driver preview, Python AST syntax, and
  whitespace checks on this change passed. The read-only preview detected
  Ubuntu 26.04.1 LTS and an NVIDIA PCI device; no package installation ran.
- The initial full-suite attempt in the restricted sandbox failed collection
  because socket creation was prohibited. An initial combined model-manager
  focused run in that sandbox was interrupted after stalling; the successful
  full-suite run included those tests outside that restriction.
- Driver retrieval/installation, reboot/MOK enrollment, optional toolkit
  installation, vLLM wheel installation/CUDA probe, and real managed GPU model
  startup were **not measured** in this session. This is implementation/unit
  evidence, not GPU-host or production qualification.
- Repository-wide `git diff --check` also reports a pre-existing extra EOF
  blank line in `docs/plans/MULTI_HOST_DEPLOYMENT.md`; left unchanged.

## Installer Valkey fix (2026-09-24)

- `install.sh` failed on hosts without Linuxbrew (`Neither valkey-server nor
  Homebrew found`). Section 3.4 now resolves Valkey in this order: `PATH`
  binary, Linuxbrew prefix, `brew install`, the official sha256-verified
  prebuilt tarball from `download.valkey.io`
  (`valkey-7.2.14-jammy-<arch>.tar.gz`, `valkey-server` + `valkey-cli`), then
  `apt-get` (Ubuntu 24.04+/Debian 13, passwordless sudo) / `apk` (Alpine).
  Fail-closed: if nothing resolves, it exits 1 with per-distro instructions.
- Verified on this host (Azure, Ubuntu 24.04 base, no Homebrew PATH entry):
  - download path: sha256 check passed, both binaries extracted to a scratch
    `BIN_DIR`, `valkey-server` runs (7.2.14, jemalloc), temp files cleaned;
  - apt fallback exercised with stubbed `curl`/`sudo`/`apt-get` (nothing real
    installed) and the total-failure path exits 1 with guidance;
  - the existing Linuxbrew-prefix branch still resolves first when present
    (observed v9.1.2 on the dev host).
- `bash -n install.sh` passes; shellcheck is not installed on this host, so
  `test_shellcheck_when_available` skipped (2 skips).
- `backend/tests/tier1_unit/test_egress_audit.py` +
  `test_multihost_render.py`: **40 passed, 2 skipped**. The egress census runs
  clean with the new `download.valkey.io` destination classified in
  `backend/tests/qualification/egress_allowlist.json`, and `docs/sovereignty.md`
  plus `docs/operations.md` now describe the new resolution order.
- A full `./install.sh` end-to-end on this host was not run (it would
  regenerate `backend/config/keys/` while other agents hold in-flight work);
  the Valkey section was verified standalone instead. Not measured: `make
  test-live` with the new binary.

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

## llama.cpp GGUF manager integration — Phase 1 (2026-09-24)

- Added a `llamacpp` engine beside default vLLM in the model registry, admin API,
  local inference routing and LiteLLM sync. Registration requires a strict GGUF
  basename and server-owned HuggingFace repo/model store; downloader calls
  `hf_hub_download` for that one file, then verifies it is a regular file under
  the managed directory. `llamacpp_server` binds loopback, uses the tested
  context/GPU/Flash-Attention settings and disables model thinking by default.
- Closed three cross-engine defects: re-registering an active model is
  rejected; a successful child start is stopped if LiteLLM sync fails; inference
  no longer turns a model-registry exception or registered-but-stopped model
  into a simulated completion (503 instead).
- Initial `make test` before Gate 1 remediation: **513 passed, 8 skipped,
  0 failed** (41.59 s). Gate 1 found read-path SIGTERM on transient health
  misses, premature port release after pid-only stop, and a same-model start
  race. These were fixed: liveness now validates the exact `/proc` command line
  without making health probes on request paths; reaping waits for the matching
  PID to exit before clearing state; start jobs install an atomic sentinel and
  model starts serialize during load.
- Oracle re-review identified two residual lifecycle gaps: delete could race the
  start sentinel, and read-path reap failures cleared PID/port. Delete/register/
  download now serialize under the model-operation lock and reject starting or
  downloading jobs. Reap failures retain PID/port and become a visible error
  that requires explicit stop/retry.
- After both remediation passes: `backend/.venv/bin/python3 -m pytest
  backend/tests/tier1_unit/test_model_manager.py
  backend/tests/tier1_unit/test_inference_gateway.py -q`: **39 passed**.
   Final `make test`: **509 passed, 21 skipped, 0 failed** (39.03 s; 530
   collected). All backend services were stopped. `pytest -q -rs` on the same
   stopped-service configuration attributed the 21 skips to seven live
  `test_platform.py` checks, one Valkey admin quota case, two daily-token
  Valkey cases, and eleven live auth/Traefik/Valkey challenger checks; these
   checks remain unverified, not passed. `git diff --check` passed after
   remediation. `make compile` remains blocked by an existing syntax error at
   `backend/services/target_executor/main.py:501` (`return ... from exc`); that
   unrelated file was not modified here.
- This phase validates command construction, file/path confinement, injected
  process lifecycle, admin registration and fail-closed routing in tests only.
  It has **not** yet run a manager-mediated HF file download, launched llama.cpp
  through the admin API, or routed a real completion through LiteLLM. Those are
  Phase 2 live acceptance checks. The only real model evidence remains the
  separate direct llama.cpp smoke test above.

## Model loading parameters (2026-09-25)

- The admin can now select more loading parameters per engine and change them on
  an existing model without re-registering:
  - **vLLM** gains `dtype` (`auto`/`half`/`float16`/`bfloat16`/`float`/`float32`),
    `kv_cache_dtype` (`auto`/`fp8`/`fp8_e5m2`/`fp8_e4m3`/`fp8_inc`/`fp8_ds`),
    `max_num_seqs`, `enforce_eager` and `enable_prefix_caching`, mapped to the
    matching `--dtype`/`--kv-cache-dtype`/`--max-num-seqs`/`--enforce-eager|--no-…`
    /`--enable-prefix-caching|--no-…` flags. Explicit booleans override the
    operator `VLLM_CONFIG` file in both directions.
  - **llama.cpp** gains `threads`, `batch_size`, `mmap` (default true, emits
    `--no-mmap` when false) and `mlock` (default false, emits `--mlock` when
    true), beside the existing `ctx_size`/`n_gpu_layers`/`flash_attn`.
  - **`PATCH /api/v1/models/{name}`** updates loading parameters of an inactive
    model (409 while starting/downloading/running), accepts only the engine's
    loading fields, and treats `null` as "clear" so the engine default applies at
    next start. Clearing is safe because the command builders treat a stored
    `null` as unset and fall back to the documented defaults. Audited as
    `model_update` (see [AUDIT_CENSUS.md](AUDIT_CENSUS.md)).
  - The admin console (*Modèles locaux*) exposes the parameters at registration
    (engine selector, GGUF filename, advanced-loading-parameters section) and a
    per-model **Paramètres** dialog that diffs against the stored values and
    PATCHes only what changed. The gateway proxies `PATCH
    /api/admin/local-models/{name}` and applies the same mutation CSRF guard as
    POST (JSON content type, `X-Sysadmin-Admin` header, same-origin `Origin`).
- Measured: `backend/tests/tier1_unit/test_model_manager.py` **59 passed**
  (1.48 s) including the new command-flag, cleared-field fallback, dtype
  validator, registration and PATCH API cases. Final `make test` (all backend
  services stopped): **729 passed, 2 skipped, 0 failed** (~70 s); the two skips
  are `shellcheck not installed` in `test_multihost_render.py`, unrelated to
  this change. `tier3_concurrency` **145 passed**, `tier2_sandbox`+`tier4_recovery`
  **183 passed**. `make compile` OK.
- **Not measured:** the harness node suite (`make harness-test`) was not run in
  this session because Node is unavailable in this environment; the gateway
  PATCH proxy and admin-UI additions were self-reviewed instead. No live
  vLLM/llama.cpp start exercised the new flags; the mapping is validated on
  built command lines only.

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
| Custom dsh harness integration (2026-09-24, re-verified after the admin fall-through fix) | 57 node tests passed; 8/8 real-`dsh` checks; 37/37 live gateway checks | Profile composes with the `dsh-plugin-sysadmin` bundle, the plugin's load banner prints, the web surface binds and refuses an unauthenticated request, and the served index carries the Mila title/favicon; the JS command policy matches the Python gate action-for-action. The live gateway run covered two concurrent sysadmins on isolated instances (the verifier used 3210/3211 in the 3210–3260 scratch range; the running gateway serves the same users in the 3180–3280 range), launch handoff, sessions surviving a gateway SIGTERM+restart with instance re-adoption on the same port, Mila-branded login/admin pages, master-key admin login with backend verification, overview probes, an instance restart that reused the port, a real approval requested with a user key and decided in the console, audit query, `platform.sh service` restart, and session revocation. The gateway preserves the browser `Host` (rewriting it to the loopback instance port made the harness Origin fence answer 403 and kept the UI stuck behind login); per-instance trusted hosts carry the host's LAN addresses, and the verifier reuses one gateway port across its restart because the harness cookie is bound to the gateway authority. A real browser on `http://192.168.14.159:3085/` renders the signed-in user (`sysadmin-01`) in the sidebar, the Mila logo in the sidebar and hero brand seats, the tab pinned to *Mila — Sysadmin AI*, a files panel listing the user's workspace (`GET /api/sysadmin/surface`, paths confined to the per-user root), and no DeepSeek brand text, with zero console errors. A live gateway crash was fixed on 2026-09-24: the admin console's page and its session probe were served but reported unhandled for browsers holding both a user session and admin access, so the request fell through to the harness proxy and the double response killed the process (`ERR_HTTP_HEADERS_SENT`); admin handlers now report handled, `proxyHttp` drops an upstream response that would rewrite a committed one, and the 57th Node test pins the fall-through. All three counts were re-measured after the fix. |
| Admin console UI redesign (2026-09-25) | 57 node tests passed; DOM + interaction QA passed at 390/768/1024/1440 px | `packages/harness-integration/gateway/admin-ui.{html,css,js}` and `assets/brand.css` were reworked into a sidebar console with a status topbar, hash deep links, arrow/Home/End roving-tabindex navigation, deduplicated toasts, a labelled `<dialog>` for destructive confirms, first-load skeletons, and per-panel feedback improvements (quota usage meters, approval countdown, audit LogsQL chips with expandable raw events, log wrap toggle, storage bar, state-dependent action buttons). Every `/api/admin/*` call, guard (quota dirty, local-models busy, 7 s/5 s polling) and DOM id is unchanged; `tests/admin.test.mjs` plus the other node suites stay green. Browser QA against a fabricated-data preview mock: all nine panels render at 390 and 1440 px with 0 px horizontal overflow and no console errors; nav badges come from live data; quota edits survive polling and save with read-back; instance logs tail and wrap; a service restart confirms in the dialog and writes the output drawer; logout→login renders cleanly after a dirty edit. Oracle gate 2 returned PASS-WITH-CONCERNS with no blockers; its one material finding (dirty/busy flags surviving logout) was fixed and re-verified. Screenshots were not captured — the automation's desktop window was not visible and the operator chose to skip them — so the visual claims above rest on DOM/computed-style measurements, not images. |
| Full backend suite, current worktree (2026-09-25) | 702 passed, 2 skipped | `backend/.venv/bin/python3 -m pytest -q`, 65 s, run as the AGENTS §6 regression check for the JS-only admin-console redesign. The worktree includes other lanes' uncommitted changes and their tests, so this is a worktree measurement, not a release baseline; the two skips are live-service checks. The UI change itself touches no Python import path. |
| Model loading parameters + PATCH endpoint (2026-09-25) | 729 passed, 2 skipped | Final `make test` after the model-loading-parameters lane: `test_model_manager.py` 59 passed; tier3 145 passed; tier2+tier4 183 passed. The two skips are `shellcheck not installed` in `test_multihost_render.py`. Harness node suite not run (no Node in this session). |

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

## Vast.ai dual enterprise A100 installation smoke test (2026-09-24)

- Installed the current worktree installer on temporary Vast.ai instance
  `52486654`: Ubuntu 24.04.5, **2 × NVIDIA A100-PCIE-40GB**, driver
  `595.71.05`, 100 GB rented disk. Quoted compute plus disk: **$2.616963/hour**.
- Uploaded only ten installer/lifecycle/provisioning scripts and configuration
  templates (15,498-byte archive), excluding local credentials, application
  Python/JS source, runtime data, models and environments. Provisioning helpers
  came from their versioned Git objects; fresh credentials stayed on the host.
- Installed host prerequisites (including `python3-venv`, Bubblewrap, curl,
  sudo and libnuma1), then ran `bash ./install.sh` with the default `all` role.
  Installer **exit 0 in 99 seconds**. Traefik 3.7.13, SeaweedFS 4.47,
  VictoriaLogs and checksum-verified Valkey 7.2.14 were obtained successfully.
- Installed **vLLM 0.30.0**, **PyTorch 2.13.0+cu132 / CUDA 13.2**,
  FastAPI 0.141.1 and LiteLLM 1.102.1. The installer's CUDA probe passed;
  separate allocation/copy assertions passed on **both GPUs**. `vllm --version`
  returned 0.30.0 with exit 0. No model download or inference was performed.
- Instance created at 21:28:34 UTC, remote test finished at 21:32:42 UTC,
  destruction requested immediately afterwards. An independent
  `vastai show instances` query confirmed **zero remaining instances**.
  The billing API subsequently reported **$0.126** for this instance
  ($0.122 GPU, $0.004 disk); this is the currently reported charge, not a
  final invoice or assurance that delayed bandwidth accounting is complete.
- Scope limit: this was an explicitly authorized container-hosted installation
  test, not bare-metal/no-container deployment or full-stack qualification.
  cgroups v2 was mounted **read-only**; sandbox execution was not qualified.
  Host-driver installation, multi-GPU inference/NCCL, service startup,
  model-manager lifecycle, runtime integration and production readiness were
  **not measured**. No application code was changed for this test.
- CLI orchestration issues: SSH attachment `--raw` printed a Python-style dict;
  destruction required `--yes` and produced empty `--raw` output. The controller
  incorrectly reported cleanup failure, but independent instance listing
  confirmed teardown. Billing lookup required both start and end dates to
  avoid the CLI's default-end-date TypeError.
- Local raw log: `/tmp/vast-install-test-20260924/install.log`; upload manifest:
  `/tmp/vast-install-test-20260924/installer-only-manifest.json` (temporary
  local evidence; not a portable repository artifact).
- Local repository-required regression after recording this evidence: `make test`
  **670 passed, 2 skipped, 1 warning in 58.23s**. The initial restricted-sandbox
  attempt failed collection because socket creation was prohibited; the completed
  run used normal host socket access. This does not expand the remote test scope.

## Vast.ai 4×H200 DeepSeek deployment → durable fixes (2026-09-24)

- Temporary Vast.ai instance `52490639` (offer 20654525): Ubuntu 24.04
  container, **4 × NVIDIA H200**, driver `570.148.08`, ~1 TiB host RAM, NVLink
  fabric, **575 084 MiB** advertised VRAM, **$18.599/hour**. The instance is
  operator-owned and **left running**; the admin console was exposed at
  `https://208.64.254.182:30321/admin` behind a temporary self-signed
  certificate. Bubblewrap cannot run inside the provider container, so
  sandboxed execution remains **unqualified** on this host.
- The repository survey detected all four GPUs. The official
  `deepseek-ai/DeepSeek-V4.1-Flash` checkpoint (48 shards) was downloaded
  through the model manager and served with tensor parallel 4
  (`system_fingerprint: vllm-0.30.0-tp4`). Real inference verified: direct
  `2+2` → `4` (~0.30 s), an authenticated HTTPS chat completion answered a
  French SSH question (43 completion tokens), and after the streaming fix a
  ~9000-token prompt streamed to completion with usage reported.
  `max-model-len` 32768 was the measured serving setting.
- This remote test is the first manager-mediated HuggingFace download and real
  vLLM start through the platform path; the earlier "not measured" notes for
  the model manager above refer to the development host, not to this rental.
- Four field defects were converted into repository fixes in this worktree:
  1. Streamed proxy requests replayed a consumed httpx stream, so DSH turns
     died with `TransferEncodingError`. The proxy now keeps client and
     response alive until the downstream stream closes and propagates upstream
     error statuses; regression tests in
     `backend/tests/tier1_unit/test_inference_streaming.py`.
  2. The installer picked torch from the host driver's CUDA level, mixing
     CUDA 12 torch with the CUDA 13 vLLM wheel (ABI mismatch). The vLLM wheel
     now resolves torch, and `install.sh` fails closed on the new preflight
     `backend/scripts/verify_vllm_runtime.py` (imports + per-GPU allocation;
     `--jit` adds nvcc/cuRAND/ninja checks).
  3. DeepSeek JIT kernels needed nvcc, cuRAND headers and ninja, and the CUDA
     torchaudio wheel rejected torch CUDA 13.2 (CPU wheel used). Operator
     recipe: [../runbooks/vast-deepseek.md](../runbooks/vast-deepseek.md).
  4. The DSH profile hardcoded the two simulator model IDs and an 8192-token
     context, so the deployed model never appeared and the DSH system prompt
     overflowed. The profile now reads `SYSADMIN_DEFAULT_MODEL`,
     `SYSADMIN_MODEL_DISPLAY_NAME`, `SYSADMIN_MODEL_CONTEXT_WINDOW` (simulator
     fallback clearly labelled); example config
     `backend/config/vllm/deepseek-v41-h200.example.yaml`.
- Local regression (development host, full stack up): `make test`
  **680 passed, 2 skipped** (one pre-existing httpx cookies deprecation
  warning) in 60.92 s; harness **57/57 node tests**. `make harness-verify`
  was not run — no local `dsh` binary. The count includes this lane's ten new
  tier-1 tests (5 streaming, 5 preflight) plus other lanes' in-flight
  additions.
- Scope limits: container-hosted test, not bare metal; sandbox execution,
  multi-user runtime against the real model, NCCL stress and long-run
  stability are **not measured**. No final invoice: the instance remains
  running at ~$18.60/hour under operator control. Temporary local evidence:
  `/tmp/vast-deepseek-deploy-20260924/`.

## Full logging of everything, always (2026-09-25)

- User directive: full logging of everything, always. Delivered in four parts.
- **Audit completeness.** Every model-manager lifecycle outcome now emits a
  schema-conformant audit event, including all rejected/failed branches
  (register/download/start/stop/restart/delete with `reason`/`error` extras,
  the async start failure with `cleanup_not_confirmed`, and the async reviewer
  identity preserved through the start job). `AUDIT_CENSUS.md` §3.8 enumerates
  the rows; the earlier §3.8/§4 contradiction is resolved. 15 new pinning
  tests in `backend/tests/tier1_unit/test_model_manager.py`.
- **Always-on structured service logs.** New `backend/services/logging_setup.py`
  (stdlib-only JSON-lines formatter, idempotent `configure()`, ASGI request
  middleware). Wired into the agent platform, auth gateway and inference
  engine (method/path/status/duration_ms per request), the audit outbox
  worker, the approval gate (propose/block/decide/claim/complete; commands
  logged as length + sha256 prefix only) and the model manager (lifecycle and
  rejection events). Documented in `operations.md` ("Logging policy").
- **Inference traffic.** One `completion` event per request on the inference
  proxy with model/stream/status/duration/tokens/bytes/error — never content.
  LiteLLM's identity-bound completion audit (census §3.7) remains the
  canonical record and is not duplicated. 7 new tests in
  `backend/tests/tier1_unit/test_inference_logging.py`.
- **Redaction, enforced and tested.** No keys, tokens, authorization headers,
  request bodies, query strings or prompt/completion content in any log line;
  tests assert against the serialized JSON output. uvicorn's plain-text access
  lines (which would include query strings) are disabled; every request is
  logged as a bounded JSON `request` event instead.
- Deliberate exclusions: `target_executor/main.py` (standalone stdlib-only
  privileged file), the observability collector (its stdout is the Prometheus
  output contract), and the harness JS gateway (another lane's active
  surface). The mixed uvicorn/JSON log-file format is documented.
- Measured (development host, full stack up): `make test`
  **729 passed, 2 skipped** in 70.72 s; harness **57/57 node tests**.

## Platform self-update — update.sh (2026-09-25)

- New root-level `update.sh` + `make update` / `make update-check`: updates the
  platform's own modules (backend services, harness package, dependencies,
  optionally the pinned static binaries) from the git tracking branch via
  fast-forward only, or from a directory checkout via `--source` (tar overlay
  with `keys`/`data`/`logs`/`run`/`bin`/`node_modules` excluded). It re-runs
  `install.sh` (which keeps existing keys and binaries), refreshes the harness
  profile, then restarts exactly the services whose PID files were alive, in
  dependency order. Every outcome is one JSON line in `backend/logs/update.log`.
  Documented in `docs/update.md`; golden commands in `AGENTS.md`/`Makefile`.
- Fail-closed properties, pinned by tests: refuses a dirty working tree,
  a diverged or non-fast-forwardable history, a checkout without provisioned
  secrets, and a confirmation without a terminal or `--yes`; never resets,
  rebases or rewrites history; secrets are preserved byte-for-byte across both
  modes; the `--source` overlay never copies keys, state, logs, run, binaries
  or harness dependency closures from the source tree.
- Measured (development host, 2026-09-25): `backend/tests/tier1_unit/
  test_update_script.py` **10 passed** (local bare-repo fixtures, no network;
  covers check/apply/refusal/overlay/confirmation paths); `make compile` OK.
  Full suite: `make test` **729 passed, 2 skipped** in 70.0 s (the 2 skips are
  the existing environment-dependent checks, not update coverage).
- Not measured here: a live `./update.sh` run against the real platform stack
  with running services (the restart path is covered by fixtures only), and
  `--binaries` re-download on this host (network download of pinned binaries).
