# Sysadmin AI Platform

An on-premises, zero-Docker sysadmin AI platform for authenticated multi-user operations.

It provides:

- a bounded ReAct runtime,
- four constrained tools for logs/runbooks/config/sandboxed shell,
- a human-in-the-loop approval gate for non-read-only commands,
- per-user quota enforcement via LiteLLM + Valkey,
- a Bubblewrap + cgroups v2 execution sandbox,
- durable audit delivery to VictoriaLogs.

> **Status: prototype.** The target adapter privileged boundary and staging deployment are not yet production-qualified. See `/home/runner/work/sysadmin-ai-platform/sysadmin-ai-platform/docs/status/TEST_READY.md` for current verification scope and limits.

## Quick start

```bash
# from repository root
./install.sh
./platform.sh start
./platform.sh status
./sysadmin-chat
```

Stop services:

```bash
./platform.sh stop
```

Optional install modes:

- `./install.sh --tui` (interactive setup wizard)
- `./install.sh --survey` (hardware survey only)

## What runs

`./platform.sh start` brings up this local stack:

- Valkey (`127.0.0.1:6379`)
- VictoriaLogs (`127.0.0.1:9428`)
- audit outbox worker
- SeaweedFS (`8333/9333/8888/8085`)
- inference engine (`127.0.0.1:8000`)
- auth gateway / ForwardAuth (`127.0.0.1:3081`)
- agent platform (default `127.0.0.1:3080`, configurable via `SYSADMIN_AGENT_PORT`)
- LiteLLM (`127.0.0.1:4000`)
- Traefik (`:8080/:8443`)

Harness gateway is opt-in (`./platform.sh harness`) and binds `:3085`.

## Golden commands

```bash
make help
make install
make start
make stop
make status
make logs SERVICE=agent_tools
make test
make test-live
make benchmark
make harness-test
make harness-verify
make compile
```

## Core components

- **Auth + quotas**: `backend/services/auth_gateway/`
- **Runtime**: `backend/services/agent_runtime/`
- **Tools + audit**: `backend/services/agent_tools/`
- **Approval gate**: `backend/services/approval_gate/`
- **Target adapter**: `backend/services/target_adapter/`
- **Inference engine**: `backend/services/inference_engine/`
- **Resilience**: `backend/services/resilience/`
- **Harness integration**: `packages/harness-integration/`

## Safety guardrails (high level)

- Identity comes from verified credentials, never client-supplied forwarded headers.
- Workspaces are server-assigned `0700`; client-selected paths/symlinks are rejected.
- Quota/approval/P1 state fails closed when required shared state is unavailable.
- Sandbox execution aborts if cgroup limits cannot be installed/read back.
- Secrets remain in `backend/config/keys/*` and are Git-ignored.

## Documentation map

- `/home/runner/work/sysadmin-ai-platform/sysadmin-ai-platform/AGENTS.md` — repository map, guardrails, test matrix
- `/home/runner/work/sysadmin-ai-platform/sysadmin-ai-platform/docs/README.md` — documentation index
- `/home/runner/work/sysadmin-ai-platform/sysadmin-ai-platform/docs/architecture.md` — architecture and trust boundaries
- `/home/runner/work/sysadmin-ai-platform/sysadmin-ai-platform/docs/services.md` — backend service reference
- `/home/runner/work/sysadmin-ai-platform/sysadmin-ai-platform/docs/security.md` — security model and open gaps
- `/home/runner/work/sysadmin-ai-platform/sysadmin-ai-platform/docs/http-api.md` — HTTP endpoints
- `/home/runner/work/sysadmin-ai-platform/sysadmin-ai-platform/docs/operations.md` — install/run/troubleshooting
- `/home/runner/work/sysadmin-ai-platform/sysadmin-ai-platform/docs/testing.md` — test tiers and execution guidance
- `/home/runner/work/sysadmin-ai-platform/sysadmin-ai-platform/docs/status/TEST_READY.md` — latest measured verification
