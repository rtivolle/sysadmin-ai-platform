# Development guide

## 1. Repository layout

```text
AGENTS.md                     agent/human operating manual (map, commands, guardrails)
Makefile                      golden commands (make help)
backend/
  platform.sh                 service lifecycle manager
  sysadmin_cli.py             interactive terminal client
  installer_tui.py            configuration wizard
  platform_tui.py             live dashboard
  services/
    agent_runtime/            ReAct loop, parser, sessions, tools dispatch
    agent_tools/              tool implementations, audit, HTTP server
    approval_gate/            filter, models, gate, Valkey store
    auth_gateway/             auth/ForwardAuth, quota, P1, LiteLLM custom auth
    inference_engine/         OpenAI-compatible simulator/proxy
    target_adapter/           scoped mutations and config deployment
    resilience/               backup, restore, DR drill
    hardware_survey.py        host inventory and inference recommendation
  config/                     templates, sandbox runner, keys
  tests/                      tiered pytest suites
  data/                       runbooks and runtime state (mostly Git-ignored)
packages/
  harness-integration/        profile, plugin, multi-user gateway, JS tests
docs/                         this documentation set
  specs/                      the six source specifications (.docx + .txt)
  plans/DEVELOPMENT_PLAN.md   implementation baseline and backlog
  status/TEST_READY.md        verification results and environment limits
  status/BENCHMARK_REPORT.md  M4 qualification report and addendum
install.sh                    one-command installer
platform.sh -> backend/platform.sh
sysadmin-chat                 CLI launcher
```

## 2. Conventions

- **Python** — FastAPI + Pydantic v2; services are standalone modules run by
  `platform.sh`. Shared modules are imported through the `backend` package root.
- **Fail closed** — privileged state (quota, approval, P1) raises
  `ConnectionError` when the shared store is unavailable, surfaced as `503`.
- **No client-selected identity or paths** — always derive the user from
  credentials and the workspace from `ensure_workspace`.
- **Secrets are files** — `backend/config/keys/*`; never tracked, never logged.
- **Bounded everything** — matches, context, output bytes, file size, command
  runtime, session history.
- **JavaScript** — ES modules, Node 22+, no build step; tests use `node --test`.
- **Documentation** — update this `docs/` set when behaviour changes, and record
  verification results in `docs/status/TEST_READY.md`.
- **Agents** — read [`../AGENTS.md`](../AGENTS.md) before changing anything; it
  carries the golden commands, the guardrail list and the per-change test matrix.
- **Multiple agents** — when more than one agent is active, follow
  [`../AGENTS.md`](../AGENTS.md) §8: own one scoped change, start from a clean
  tree, do not commit or reset history without explicit approval, and stop to
  ask when conflicts involve guardrails, secrets or the verification baseline.

## 3. Common tasks

### Add a backend tool

1. Implement the function in `backend/services/agent_tools/tools.py` with
   bounded inputs and path confinement.
2. Register it in `tool_registry.py` (`AVAILABLE_TOOLS`, `TOOL_DESCRIPTIONS`)
   and add dispatch in `execute_tool_call`.
3. Add it to `/api/tools/list` and `/api/tools/execute` in
   `agent_tools/server.py`.
4. Mirror any shell-command classification in the JS policy port if relevant.
5. Add unit tests and update [tools.md](tools.md).

### Add a target-adapter action

1. Add the action to `ALLOWED_ACTIONS` in `target_adapter/config.py`.
2. Extend `TargetAdapter.propose` and `execute`.
3. Add service-manager/config-deployer support.
4. Add tests in the tier-1 and tier-3 suites; update [services.md](services.md).

### Change a quota or policy default

1. Update the code (source of truth) and
   `backend/config/platform_config.json` for documentation.
2. Update the tables in [configuration.md](configuration.md) and
   [security.md](security.md).
3. Add/adjust tests in `tier1_unit/test_quota_fail_closed.py` and the tier-3
   suites.

### Change the sandbox

Edit `backend/config/sandbox/bwrap-runner.sh` and re-run `platform.sh test` on
a host with cgroups v2 delegation. The runner must abort (`126`) if a limit
cannot be installed and read back.

## 4. Extending the Harness integration

- **Policy** → `packages/harness-integration/dsh-plugin-sysadmin/lib/policy.js`
  (keep it in lock-step with the Python filter).
- **Audit** → `lib/audit.js` (same field set as `log_audit_event`).
- **Backend calls** → `lib/backend.js`.
- **Profile/model route** → `profile/cordis.patch.yml`.
- **Per-user isolation** → `gateway/instance-manager.js`.

## 5. Deviations from the design specifications

The implementation is narrower than the original documents in several places.
This is intentional and must be preserved rather than papered over:

| Spec claim | Implementation reality |
|---|---|
| NVIDIA Dynamo with KV-aware routing and P/D disaggregation | Not deployed. LiteLLM → inference engine; the engine proxies to vLLM when configured. |
| DeepSeek Harness as the runtime | The backend implements its own ReAct loop and tools; the Harness package supplies a profile, plugin and multi-user gateway for an operator-installed `dsh`. |
| SeaweedFS and VictoriaLogs via Docker Compose | Native static binaries and processes, no Docker. |
| `systemctl` restarts host services from the sandbox | A sandboxed `systemctl` does not affect the host. Host mutations go through the target adapter, which is not yet qualified. |
| "Unlimited" P1 priority | P1 is time-bounded admission priority with higher finite limits; it never bypasses approval, sandbox or audit. |
| 50 % TTFT / sub-10 ms sandbox startup etc. | Hypotheses, not measured acceptance evidence. |
| "100 % FOSS" | An application-level claim; GPU drivers, model weights and runtime dependencies need a separate licence inventory. |

See [`plans/DEVELOPMENT_PLAN.md`](plans/DEVELOPMENT_PLAN.md) §3 for the original
corrections list and [`status/TEST_READY.md`](status/TEST_READY.md) for the
current verification status.
