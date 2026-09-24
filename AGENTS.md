# Agent guide

Audience: coding agents (and humans) working in this repository. Read this
first — it is the map, the golden commands and the guardrails.

## 1. What this repository is

An on-premises, **zero-Docker** sysadmin AI platform: Traefik front door,
ForwardAuth identity + quota gateway (LiteLLM + Valkey), a multi-user agent
runtime with four bounded tools, a Human-in-the-Loop approval gate, a scoped
target adapter, a Bubblewrap + cgroups v2 sandbox, and a VictoriaLogs audit
trail with a durable local outbox. Python/FastAPI services, a Node DeepSeek
Harness integration, and static Go/C binaries — no containers.

**Status: prototype.** The scoped target adapter, its privileged boundary and
staging deployment are not qualified for production target changes. Current
measured results and limits live in [docs/status/TEST_READY.md](docs/status/TEST_READY.md).
Never restate a claim as verified unless a run in this session produced it.

## 2. Repository map

```text
AGENTS.md                     this file
README.md                     orientation and quick start
Makefile                      golden commands (make help)
install.sh                    dependency/binary install, credential provisioning
platform.sh -> backend/platform.sh   service lifecycle (start/stop/test/harness)
sysadmin-chat                 CLI launcher -> platform.sh chat
pytest.ini                    pytest rootdir, pythonpath, asyncio mode

backend/
  platform.sh                 service lifecycle manager (the real file)
  sysadmin_cli.py             interactive terminal client
  installer_tui.py            configuration wizard (install.sh --tui)
  platform_tui.py             live dashboard
  services/
    agent_runtime/            ReAct loop, parser, sessions, tool registry, HTTP router
    agent_tools/              bounded tools, audit, HTTP server
    approval_gate/            destructive filter, HITL state machine, Valkey store
    auth_gateway/             ForwardAuth, quota/lease manager, P1 elevation, LiteLLM auth
    inference_engine/         OpenAI-compatible simulator / vLLM proxy
    target_adapter/           scoped service actions and staged config deployment
    resilience/               backup, restore, DR drill
    hardware_survey.py        host inventory
  config/                     traefik, valkey, litellm, sandbox, seaweedfs, victorialogs, keys (local)
  tests/                      tiered pytest suites (see §6)
  data/                       runbooks (tracked) + runtime state (Git-ignored)
  bin/                        downloaded static binaries (Git-ignored)

packages/harness-integration/ DeepSeek Harness profile, plugin, multi-user gateway
docs/                         documentation set (start at docs/README.md)
  specs/                      the six source specifications (French, .docx + .txt)
  plans/DEVELOPMENT_PLAN.md   implementation baseline and backlog
  plans/PRODUCTION_READINESS.md  prioritized production-readiness work items and tracking
  plans/MULTI_HOST_DEPLOYMENT.md  three-machine topology design (unimplemented; PR-H1)
  status/TEST_READY.md        current verification results and limits
  status/BENCHMARK_REPORT.md  M4 qualification report + re-verification addendum
```

## 3. Golden commands

```bash
make help                    # list every target
make install                 # install.sh: deps, binaries, random credentials
make start / stop / status   # platform.sh lifecycle
make test                    # full pytest suite (no live services required)
make test-live               # platform.sh test: starts services, end-to-end, stops
make benchmark               # 30-task evaluation pack
make harness-test            # node --test packages/harness-integration/tests
make compile                 # Python byte-compile check
backend/.venv/bin/python3 -m pytest backend/tests/tier2_sandbox -q
./install.sh --survey        # hardware inventory only
./platform.sh logs [service] # tail service logs
```

Secrets are generated into `backend/config/keys/` (Git-ignored). `platform.sh`
injects the LiteLLM master key and Valkey password at startup; tests read the
per-user bearer keys from those files.

## 4. Guardrails — do not break these

- **Secrets stay in files.** Never print, commit or copy `backend/config/keys/*`.
- **Fail closed.** Quota, approval, P1 and session state raise `ConnectionError`
  when the shared store is unavailable and surface as `503`; do not add a
  silent fallback for a store that is *required*.
- **Identity comes from credentials.** Never trust `X-User`/`X-Forwarded-*`
  from a client; ForwardAuth strips them.
- **No client-selected paths.** Workspaces are server-assigned `0700`
  directories; reject traversal and symlinks.
- **Approvals are single-use and bound** to user, session, command, target,
  workspace, content hash and a five-minute expiry.
- **The sandbox runner aborts** (`126`) if a cgroup limit cannot be installed
  and read back. Do not weaken it to make a test pass.
- **The JS policy port must match** `backend/services/approval_gate/filter.py`
  action-for-action; the parity test enforces it.
- **Generated directories are off limits**: `backend/bin`, `backend/logs`,
  `backend/run`, `backend/.venv`, `.pytest_cache`, and everything under
  `backend/data/` except the tracked runbooks.
- **Never fabricate evidence.** Report skips and environment limits explicitly;
  say "not measured" when it was not measured.
- **Port 3080 must be free** for the agent platform. It is also the DeepSeek
  Harness web default, so a host running the harness UI there cannot start
  `agent_tools`; see the host note in `docs/status/TEST_READY.md`.

## 5. Where to change what

| Task | Files |
|---|---|
| Add or change a bounded tool | `backend/services/agent_tools/tools.py`, `backend/services/agent_runtime/tool_registry.py`, `backend/services/agent_tools/server.py`; tests in `tier1_unit`; doc in `docs/tools.md` |
| Quota, lease, RPM/TPM or daily budget | `backend/services/auth_gateway/quota_manager.py` + `litellm_auth.py`; tests in `tier1_unit/test_quota_fail_closed.py`, `tier1_unit/test_daily_token_reservation.py`, `tier3_concurrency/` |
| Approval flow or destructive filter | `backend/services/approval_gate/` (Python) **and** `packages/harness-integration/dsh-plugin-sysadmin/lib/policy.js` |
| Target adapter action | `backend/services/target_adapter/config.py`, `adapter.py`, then `service_manager.py`/`config_deployer.py`; tests in `tier1_unit` + `tier3_concurrency` |
| Sandbox limits or mounts | `backend/config/sandbox/bwrap-runner.sh`; verify with `tier2_sandbox` on a host with cgroups v2 |
| Audit schema or outbox | `backend/services/agent_tools/audit.py`; keep the JS audit writer in sync; tests in `tier4_recovery` |
| Backup / restore / DR | `backend/services/resilience/`; tests in `tier4_recovery` |
| Traefik routes or middleware | `backend/config/traefik/dynamic.yml`; doc in `docs/http-api.md` |
| Harness profile or gateway | `packages/harness-integration/`; doc in `docs/harness-integration.md` |
| Documentation | `docs/` (index: `docs/README.md`); verification results: `docs/status/TEST_READY.md` |

## 6. Verification protocol

| After changing | Run |
|---|---|
| Anything | `make test` |
| Quota, approval, runtime, adapter | `backend/.venv/bin/python3 -m pytest backend/tests/tier1_unit backend/tests/tier3_concurrency -q` |
| Sandbox, workspaces | `backend/.venv/bin/python3 -m pytest backend/tests/tier2_sandbox -q` |
| Audit, outbox, backup/restore | `backend/.venv/bin/python3 -m pytest backend/tests/tier4_recovery -q` |
| Tools, prompts, rubrics | `make benchmark` |
| Service wiring, ports, Traefik | `make start && make test-live` (needs a free port 3080) |
| Harness package | `make harness-test && make harness-verify` |
| Anything user-visible | update `docs/` and record results in `docs/status/TEST_READY.md` |

Baseline on the development host (2026-09-24): the suite collects **436 tests**.
With Valkey reachable and the other backend services stopped it reports
**423 passed, 13 skipped** — eleven live auth/Traefik/Valkey challenger checks
plus the seven `test_platform.py` sections, which skip when the stack is down
and fail on any recorded error instead of passing silently. With every backend
service stopped: **416 passed, 20 skipped**. With the stack up except Traefik
(the harness live-run configuration): **428 passed, 8 skipped**. Tier 2 sandbox
**129 passed** (including the 80-case M4 challenger pack); harness **45 node
tests + 8/8 dsh checks + 37/37 live gateway checks** (`make harness-test`,
`make harness-verify`, `packages/harness-integration/scripts/verify-live-gateway.mjs`).
Treat these as a regression baseline, not a production acceptance certificate,
and record new measurements in `docs/status/TEST_READY.md`.

## 7. Environment notes

- Python: `backend/.venv/bin/python3` (created by `install.sh`). Node 22+ for
  the harness package; no build step.
- Host prerequisites for the sandbox: `bwrap`, `timeout`, and delegated
  writable cgroups v2. Without them the sandbox tests skip and execution fails
  closed.
- Services bind loopback only: Traefik 8080/8443, LiteLLM 4000, agent platform
  3080, ForwardAuth 3081, harness gateway 3085, inference 8000, SeaweedFS
  8333/9333/8888, VictoriaLogs 9428, Valkey 6379.
- There is no Docker anywhere in the runtime; do not introduce containers.

## 8. Multiple agents working on the same repository

Several coding agents may edit this project at the same time. Treat every agent
as an independent contributor with no shared memory beyond the files in the
repository.

- **One task per agent.** Each agent should own a single, well-scoped change.
  If two agents need the same file, split the work or serialize it.
- **Start from a clean working tree.** Before making changes, check the current
  branch and uncommitted modifications; do not overwrite another agent's in-flight
  work.
- **Never commit, push or reset Git history** unless the user explicitly asks for
  it. Ask for confirmation before any `git commit`, `git push`, `git reset`,
  `git rebase` or merge.
- **Do not touch generated or secret directories** (`backend/config/keys/*`,
  `backend/bin`, `backend/logs`, `backend/run`, `backend/data/` runtime state,
  `.pytest_cache`).
- **Communicate through the files.** Write clear commit-sized changes, update the
  relevant `docs/` pages, and record new measurements in
  `docs/status/TEST_READY.md` when behaviour changes.
- **Run the right tests for your change.** See §6. Do not rely on another agent
  to verify your edits.
- **If you see conflicts, stop and ask.** Do not silently resolve a conflict that
  involves another agent's work, guardrails, secrets, or the verification
  baseline. Surface the collision and wait for direction.
- **Respect the guardrails in §4.** They apply no matter how many agents are
  active.
