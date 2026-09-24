# On-premises sysadmin AI platform

This repository contains a local sysadmin agent prototype with a Traefik front door, authentication adapter, LiteLLM quota gateway, optional upstream vLLM inference, bounded read tools, Bubblewrap sandbox, human approval gate, scoped target adapter, and VictoriaLogs audit outbox. It is **not ready for production target changes**: the target adapter can propose and execute allowlisted service actions or staged configuration deployments, but its privileged execution boundary and staging deployment have not been qualified. A `systemctl` command in the sandbox does not restart a host service.

## Documentation

The [`docs/`](docs/README.md) directory documents the implemented system:
architecture and trust boundaries, each backend service, the Harness
integration, the security model, the HTTP API and tool reference, operations,
configuration, backup/restore, testing and development conventions. Start at
[docs/README.md](docs/README.md). The source specifications live in
[`docs/specs/`](docs/specs/README.md), the plan and backlog in
[`docs/plans/`](docs/plans/DEVELOPMENT_PLAN.md), and the verification and
benchmark reports in [`docs/status/`](docs/status/TEST_READY.md).

Working in this repository as an agent or a new contributor? Start with
[AGENTS.md](AGENTS.md): repository map, golden commands, guardrails and the
verification protocol.

## Install and run

The host needs Linux, Python 3, Bubblewrap, `timeout`, and a writable delegated cgroups v2 subtree for the platform service account. Without cgroup delegation, sandbox execution fails closed before starting a command. The runner enforces `memory.max=4294967296` with `memory.swap.max=0` — so the 4 GiB ceiling bounds total memory rather than only RSS (without the swap cap the kernel swaps pages out at the ceiling instead of OOM-killing) — plus `pids.max=128`, `cpu.max=200000 100000`, and a 15-second deadline with a five-second kill grace period. It mounts only the assigned workspace at `/workspace` for writing and unshares the network.

```bash
./install.sh               # dependencies, native binaries, random credentials
./platform.sh start
./platform.sh status
./sysadmin-chat
```

`./install.sh --tui` runs the configuration wizard; run `./install.sh` afterward to install the service dependencies and binaries. `./install.sh --survey` only surveys hardware. The local inference service simulates responses unless `UPSTREAM_VLLM_URL` points at an operational vLLM endpoint. NVIDIA Dynamo is not deployed by this codebase. DeepSeek Harness is wired through the separate integration in `packages/harness-integration/`, which is opt-in: `./platform.sh harness` starts it (or `./platform.sh service harness_gateway start`), while `./platform.sh start` leaves it off; the agent API on port 3080 is still the built-in runtime.

The installer creates random bearer tokens in `backend/config/keys/*.key`, PBKDF2 login hashes in `backend/config/keys/login-credentials.json`, and one-time plaintext passwords in `backend/config/keys/initial-passwords.txt`. All are private local files ignored by Git. Move the initial passwords into an approved password manager and remove that plaintext file from the host after delivery. The installer preserves existing random credentials on rerun and replaces legacy deterministic bearer keys. To intentionally rotate login passwords, run `backend/.venv/bin/python3 backend/config/keys/provision-logins.py --rotate`; distribute the new passwords before ending active sessions. The LiteLLM master token and Valkey password are generated as `master.key` and `valkey-password.key`; `platform.sh` supplies them to local services at startup.

The CLI uses `SYSADMIN_USER=sysadmin-01` by default. Each user's bearer key stays in their own private key file. An administrator can run a separate CLI with `SYSADMIN_USER=sysadmin-admin`; that process uses `master.key` and can run `/approvals`, `/approve ID`, and `/reject ID`. When a user receives an approval ID, the administrator reviews and decides it. The user then runs `/resume ID` in the original CLI session to submit the exact command once. Approvals bind to the user, session, command, workspace, and five-minute deadline. Platform runs use transactional Valkey approval state and reject approval operations when that shared store is unavailable. Direct development runs without `VALKEY_URL` can fall back to process-local state when Valkey is unavailable.

Only simple read-only shell commands run without approval. Shell expressions and other commands need review; the destructive-command filter rejects known destructive forms. The shell tool runs inside the isolated workspace. It is not a production target execution mechanism.

Agent completions always go through LiteLLM. If that gateway rejects or cannot serve a request, the agent returns an error rather than calling inference directly. Concurrent requests use expiring per-request Valkey leases, renewed during long generations and released by lease ID when requests finish. The target adapter API authenticates each caller, derives reviewer authority from that identity, and reads staged configuration files only from the caller's assigned workspace.

## Audit and tests

Audit events are sent to VictoriaLogs. When the collector is down, events are fsynced to a local outbox; `platform.sh` runs a replay worker when services start. Delivery is at least once: an event may be replayed after a crash, and `event_id` allows deduplication. A malformed outbox record pauses replay until repaired. Stop all services with `./platform.sh stop`.

```bash
backend/.venv/bin/python3 -m pytest backend/tests/tier1_unit backend/tests/tier3_concurrency/test_approval_gate_lifecycle.py backend/tests/tier4_recovery/test_outbox_resilience.py -q
backend/.venv/bin/python3 -m pytest -q
```

Tests that need real Bubblewrap namespaces, writable cgroups, Valkey, or loopback services require a host configured for them. See [docs/status/TEST_READY.md](docs/status/TEST_READY.md) for current verification results and limits. The 30-task pack exercises synthetic fixtures and does not establish production readiness or a real recovery time objective.

## Custom DeepSeek Harness integration

`packages/harness-integration/` wires the real DeepSeek Harness (`@deepseek-ai/dsh`) to this backend as an alternative agent runtime. It ships a custom `sysadmin` dsh profile, a harness bundle plugin, and a multi-user login gateway.

The harness web surface is single-tenant: one launch token, one credential store and one workspace per process. Multi-user is therefore one `dsh --profile sysadmin` process per authenticated sysadmin, each with its own `DSH_HOME`, workspace, loopback port and LiteLLM virtual key. The gateway authenticates users against the auth gateway and routes each to their own instance, so the process environment is the isolation boundary and no key is written to shared configuration. Sessions and instance records persist under `backend/data/harness`, so a gateway restart keeps users logged in, re-adopts live instances on the same port, and restarts unexpected exits with backoff.

The gateway also serves a Mila-branded admin console at `http://127.0.0.1:3085/admin` (master token from `backend/config/keys/master.key`) covering users, sessions, instances, approvals, audit and services; the console restarts backend services through `./platform.sh service <name> restart`. The harness tab and favicon are branded through the plugin, not a UI fork.

Harness data reaches the backend three ways: model traffic goes through the profile's `litellm` route to LiteLLM on port 4000, the `sysadmin_backend_tool` tool forwards bounded tool calls to the agent platform on port 3080, and every tool outcome and policy decision is written to VictoriaLogs using the same event schema as `backend/services/agent_tools/audit.py`. Command safety is enforced by a port of `backend/services/approval_gate/filter.py`, with a test asserting the two engines agree action-for-action.

```bash
packages/harness-integration/install-harness.sh                 # stage the profile into $DSH_HOME
node packages/harness-integration/scripts/verify-harness.mjs    # compose + boot + plugin load + 401 + Mila branding
node --test packages/harness-integration/tests/                 # unit tests (policy, audit, gateway, persistence, branding, admin)
./platform.sh service harness_gateway start                     # multi-user gateway on :3085, persistent sessions/instances
```

See [packages/harness-integration/README.md](packages/harness-integration/README.md) for architecture, contracts and limits.

## Repository scope

Git tracks source, configuration templates, documentation, and small test fixtures. It ignores downloaded binaries, the Python virtual environment, generated multi-gigabyte fixtures, local service state, logs, workspaces, and secrets. `backend/tests/fixtures/fixture_generator.py` can regenerate large fixtures locally.
