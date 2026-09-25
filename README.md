# Sysadmin AI Platform

A zero-Docker, on-premises sysadmin agent prototype for small operations teams.
It gives authenticated users a bounded ReAct runtime, four read/validate tools,
a human-in-the-loop approval gate, a Bubblewrap + cgroups v2 sandbox, per-user
quotas, and a VictoriaLogs audit trail with a durable local outbox.

> **Status: prototype.** The target adapter and its privileged boundary are
> implemented but **not qualified for production target changes**. The benchmark
> suite and live smoke tests pass on the development host; see
> [`docs/status/TEST_READY.md`](docs/status/TEST_READY.md) for the exact scope
> and remaining qualification work.

---

## Quick start

```bash
# 1. Install dependencies, native binaries and random local credentials
./install.sh

# 2. Start the platform services
./platform.sh start
./platform.sh status

# 3. Chat with the agent CLI
./sysadmin-chat
```

Run `./install.sh --tui` for the configuration wizard, or `./install.sh --survey`
for hardware inventory only. Stop everything with `./platform.sh stop`.

The CLI defaults to `SYSADMIN_USER=sysadmin-01`. Administrator commands use
`SYSADMIN_USER=sysadmin-admin` and the master key in
`backend/config/keys/master.key`.

---

## What's implemented

| Area | State |
|---|---|
| **Identity & auth** | Bearer tokens + PBKDF2 logins via ForwardAuth; `X-User`/`X-Forwarded-*` headers are stripped and re-validated from credentials. |
| **Control store** | Optional PostgreSQL backend (`backend/services/control_store/`, ADR-0014) for durable token ledgers and API key lifecycles, surviving restarts. |
| **Inference routing** | LiteLLM gateway with per-user virtual keys; local engine simulates unless `UPSTREAM_VLLM_URL` points at vLLM. |
| **Quotas** | Per-user concurrency leases (2 in-flight, 6 during P1), RPM, TPM and daily token budgets with atomic reservation/settlement in Valkey; defaults are overridable per user by an administrator (`GET|POST /api/v1/admin/quotas`). |
| **Agent runtime** | ReAct loop, session store, tool registry and parser in `backend/services/agent_runtime/`. |
| **Bounded tools** | Streaming log search, Markdown runbook reader, JSON/YAML/systemd linter + unified diff, sandboxed shell. |
| **Approval gate** | Human-in-the-loop state machine bound to user, session, command, target, workspace, content hash and 5-minute expiry. |
| **Sandbox** | Bubblewrap namespaces, no network, dropped caps, read-only `/usr`, `memory.max=4 GiB` with `memory.swap.max=0`, `pids.max=128`, `cpu.max=200%`, 15 s deadline. |
| **Audit** | Complete census across all user/agent actions to VictoriaLogs; local outbox fsyncs on failure; HMAC-chained audit anchor (`audit_anchor.py`). |
| **Target adapter & executor** | Scoped allow-list of service actions and staged config deployment with hardened `target_executor` daemon under systemd sandboxing. |
| **Observability** | Out-of-process metrics collector and threshold evaluator (`backend/services/observability/`), Prometheus text/scrape endpoint, 8 alert runbooks. |
| **Machine roles** | Install-time role selection (`--role all|web|inference|data`) with fail-closed configuration rendering and firewall rulesets. |
| **Model management** | Admin-only HuggingFace snapshot download plus local vLLM and llama.cpp servers per model; appears in `/v1/models` and LiteLLM. |
| **Resilience & Update** | Backup/restore/DR drill, off-host backup sync (`offhost_backup.py`), and platform self-update engine (`update.sh`). |
| **Harness integration** | Optional DeepSeek Harness profile, plugin and multi-user gateway in `packages/harness-integration/`. |

---

## Latest verification (2026-09-24)

| Check | Result |
|---|---|
| Full backend suite, live stack (earlier run) | **356 passed, 0 failed, 0 skipped** |
| Full backend suite, stack up except Traefik (2026-09-24) | **480 passed, 8 skipped, 0 failed** |
| Full suite, Valkey up / services stopped (earlier run) | **423 passed, 13 skipped** |
| Tier 2 sandbox suite | **129 passed** |
| 30-task synthetic benchmark | **31 cases passed** |
| Harness integration | **45 node tests + 8/8 real-`dsh` checks + 37/37 live gateway checks** |
| Kernel-backed sandbox stress run | `systemd-run` leg passed; raw cgroup-delegation leg fail-closed verified |

Run the same checks with:

```bash
make test                    # full pytest suite
make test-live               # start services, run live stack, stop
make test-sandbox            # Bubblewrap / cgroups suite
make benchmark               # 30-task evaluation pack
make harness-test            # node --test packages/harness-integration/tests/
make harness-verify          # boot real dsh and verify wiring
make stress-sandbox          # kernel-backed sandbox stress qualification
```

See [`docs/status/TEST_READY.md`](docs/status/TEST_READY.md) for the detailed
host note on port 3080 and the full list of skipped tests.

---

## Architecture at a glance

```text
Browser / CLI
      |
   Traefik (:8080 / :8443)
      |
ForwardAuth (:3081)  ----  auth gateway + quota manager + P1 elevation
      |
   agent runtime (:3080) ---- LiteLLM (:4000) ---- inference engine (:8000)
      |
bounded tools  ----  approval gate  ----  sandbox (bwrap + cgroups v2)
      |
   VictoriaLogs (:9428)  <-- audit outbox
   SeaweedFS   (:8333/9333/8888)
   Valkey      (:6379)
```

All services bind loopback only. DeepSeek Harness is opt-in and lives in
`packages/harness-integration/`; start it with `./platform.sh harness`.

---

## Repository map

```text
AGENTS.md                     agent/human operating manual (read this first)
Makefile                      golden commands (make help)
README.md                     this file
install.sh                    deps, binaries, random credentials
platform.sh                   service lifecycle manager
sysadmin-chat                 interactive CLI launcher

backend/
  services/
    agent_runtime/            ReAct loop, sessions, tool registry
    agent_tools/              bounded tools, audit, HTTP server
    approval_gate/            destructive filter, HITL state machine
    auth_gateway/             ForwardAuth, quota, P1, LiteLLM auth
    control_store/            PostgreSQL durable quota ledger, key store, schema & CLI (ADR-0014)
    inference_engine/         OpenAI-compatible simulator / vLLM proxy
    model_manager/            local model lifecycle, vLLM / llama.cpp servers, registry
    observability/            metrics collector, alert evaluator, Prometheus exporter
    resilience/               backup, restore, DR drill, audit anchor, offhost backup
    target_adapter/           scoped service actions and staged config
    target_executor/          hardened privileged execution daemon, systemd sandbox, sudoers
  config/                     traefik, valkey, postgres, roles, firewall, target_executor, vllm, observability, litellm, sandbox, keys (ignored)
  tests/                      tiered pytest suites + e2e benchmark
  data/                       runbooks (tracked) + runtime state (ignored)

packages/harness-integration/ DeepSeek Harness profile, plugin, gateway
docs/                         architecture, security, API, ops, testing, status, decisions
```

---

## Safety & hard limits

- Secrets stay in `backend/config/keys/*`; these files are Git-ignored and must
  never be printed, committed or copied.
- Workspaces are server-assigned `0700` directories; client-selected paths and
  symlinks are rejected.
- The sandbox aborts (exit `126`) if cgroup limits cannot be installed and read
  back.
- Quota, approval and P1 state fail closed (`503`) when Valkey is unreachable.
- Only allow-listed, approved actions reach the target adapter and executor; arbitrary
  privileged shell and production mutations are out of scope for this prototype.

---

## What's next / open qualification work

- Staging target deployment qualification (PR-A2) with rollback demonstrated.
- Live three-machine cluster bring-up and verification (PR-H1).
- Multi-worker Valkey integration test under real load (PR-B3).
- Real owner-scored 30-task field evaluation on the RTX 8000 host (PR-B5).
- Full restore drill against clean staging with measured RTO/RPO including live service bring-up (PR-B4).
- Qualify local model serving on the GPU host (real HuggingFace download + vLLM
  start, GPU-fit check, PR-C1).
- Kernel-backed sandbox stress run for the raw cgroup-delegation leg on the
  platform account's delegated subtree (PR-B2).

See [`docs/status/TEST_READY.md`](docs/status/TEST_READY.md) and
[`docs/plans/DEVELOPMENT_PLAN.md`](docs/plans/DEVELOPMENT_PLAN.md) for the full
backlog and corrections.

---

## Documentation

- Start here: [`AGENTS.md`](AGENTS.md)
- System docs: [`docs/README.md`](docs/README.md)
- Architecture: [`docs/architecture.md`](docs/architecture.md)
- Security model: [`docs/security.md`](docs/security.md)
- HTTP API: [`docs/http-api.md`](docs/http-api.md)
- Operations: [`docs/operations.md`](docs/operations.md)
- Platform self-update: [`docs/update.md`](docs/update.md)
- Multi-host topology: [`docs/multi-host.md`](docs/multi-host.md)
- Local observability: [`docs/observability.md`](docs/observability.md)
- Model management: [`docs/model-management.md`](docs/model-management.md)
- NVIDIA & vLLM setup: [`docs/nvidia-vllm.md`](docs/nvidia-vllm.md)
- Sovereignty & blocked egress: [`docs/sovereignty.md`](docs/sovereignty.md)
- Architectural Decision Records: [`docs/decisions/README.md`](docs/decisions/README.md)
- Testing: [`docs/testing.md`](docs/testing.md)
- Verification status: [`docs/status/TEST_READY.md`](docs/status/TEST_READY.md)
- Audit census: [`docs/status/AUDIT_CENSUS.md`](docs/status/AUDIT_CENSUS.md)
- Control store status: [`docs/status/CONTROL_STORE.md`](docs/status/CONTROL_STORE.md)
- Development: [`docs/development.md`](docs/development.md)
- Harness integration: [`packages/harness-integration/README.md`](packages/harness-integration/README.md)

---

## Contributing / agent notes

Read [`AGENTS.md`](AGENTS.md) before changing anything. It contains the
repository map, golden `make` commands, the guardrail list, the per-change test
matrix, and the multi-agent coordination rules.

If multiple agents are active, follow **AGENTS.md §8**: own one scoped change,
start from a clean tree, do not commit or reset history without explicit
approval, and stop to ask when conflicts touch guardrails, secrets or the
verification baseline.
