# Sysadmin AI Platform

An on-premises, zero-Docker sysadmin AI platform for authenticated multi-user operations.

It provides:

- a bounded ReAct runtime,
- four constrained tools for logs/runbooks/config/sandboxed shell,
- a human-in-the-loop approval gate for non-read-only commands,
- per-user quota enforcement via LiteLLM + Valkey,
- a Bubblewrap + cgroups v2 execution sandbox,
- durable audit delivery to VictoriaLogs.

> **Status: prototype.** The target adapter privileged boundary and staging deployment are not yet production-qualified. See [`docs/status/TEST_READY.md`](docs/status/TEST_READY.md) for current verification scope and limits.

**Contributors and agents:** read [`AGENTS.md`](AGENTS.md) first for repository guardrails, required verification matrix, and coordination rules.

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

Equivalent Make workflow:

```bash
make install
make start
make status
make stop
```

Optional install modes:

- `./install.sh --tui` (interactive setup wizard)
- `./install.sh --survey` (hardware survey only)

## What runs

`./platform.sh start` brings up this local stack:

- Valkey (`127.0.0.1:6379`)
- VictoriaLogs (`127.0.0.1:9428`)
- audit outbox worker
- SeaweedFS (`127.0.0.1:8333/9333/8888`)
- inference engine (`127.0.0.1:8000`)
- auth gateway / ForwardAuth (`127.0.0.1:3081`)
- agent platform (`127.0.0.1:3080`)
- LiteLLM (`127.0.0.1:4000`)
- Traefik (`127.0.0.1:8080/8443`)

This is the intended default stack; ensure the agent port (`3080` by default, override with `SYSADMIN_AGENT_PORT`) is free before startup.

Harness integration is optional; see [`docs/harness-integration.md`](docs/harness-integration.md).

## Golden commands

```bash
make help
make install
make start
make stop
make status
make test
make test-live
make benchmark
make harness-test
make harness-verify
make compile
```

Harness package tests run via `make harness-test`; live harness wiring verification runs via `make harness-verify`. See [`docs/testing.md`](docs/testing.md) for details.

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
- Arbitrary privileged shell access and direct production mutations are out of scope for this prototype.
- Secrets remain in `backend/config/keys/*` and are Git-ignored.

## Documentation map

- [`AGENTS.md`](AGENTS.md) — repository map, guardrails, test matrix
- [`docs/README.md`](docs/README.md) — documentation index
- [`docs/architecture.md`](docs/architecture.md) — architecture and trust boundaries
- [`docs/services.md`](docs/services.md) — backend service reference
- [`docs/security.md`](docs/security.md) — security model and open gaps
- [`docs/http-api.md`](docs/http-api.md) — HTTP endpoints
- [`docs/operations.md`](docs/operations.md) — install/run/troubleshooting
- [`docs/testing.md`](docs/testing.md) — test tiers and execution guidance
- [`docs/status/TEST_READY.md`](docs/status/TEST_READY.md) — latest measured verification
