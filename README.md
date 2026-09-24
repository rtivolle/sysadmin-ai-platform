# On-premises sysadmin AI platform

This repository contains a local sysadmin agent prototype with a Traefik front door, authentication adapter, LiteLLM quota gateway, optional upstream vLLM inference, bounded read tools, Bubblewrap sandbox, human approval gate, scoped target adapter, and VictoriaLogs audit outbox. It is **not ready for production target changes**: the target adapter can propose and execute allowlisted service actions or staged configuration deployments, but its privileged execution boundary and staging deployment have not been qualified. A `systemctl` command in the sandbox does not restart a host service.

## Documentation

The [`docs/`](docs/README.md) directory documents the implemented system:
architecture and trust boundaries, each backend service, the Harness
integration, the security model, the HTTP API and tool reference, operations,
configuration, backup/restore, testing and development conventions. Start at
[docs/README.md](docs/README.md).

## Install and run

The host needs Linux, Python 3, Bubblewrap, `timeout`, and a writable delegated cgroups v2 subtree for the platform service account. Without cgroup delegation, sandbox execution fails closed before starting a command. The runner enforces `memory.max=4294967296`, `pids.max=128`, and `cpu.max=200000 100000`, plus a 15-second deadline with a five-second kill grace period. It mounts only the assigned workspace at `/workspace` for writing and unshares the network.

```bash
./install.sh               # dependencies, native binaries, random credentials
./platform.sh start
./platform.sh status
./sysadmin-chat
```

`./install.sh --tui` runs the configuration wizard; run `./install.sh` afterward to install the service dependencies and binaries. `./install.sh --survey` only surveys hardware. The local inference service simulates responses unless `UPSTREAM_VLLM_URL` points at an operational vLLM endpoint. NVIDIA Dynamo and DeepSeek Harness are not deployed by this codebase.

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

Tests that need real Bubblewrap namespaces, writable cgroups, Valkey, or loopback services require a host configured for them. See [TEST_READY.md](TEST_READY.md) for current verification results and limits. The 30-task pack exercises synthetic fixtures and does not establish production readiness or a real recovery time objective.

## Repository scope

Git tracks source, configuration templates, documentation, and small test fixtures. It ignores downloaded binaries, the Python virtual environment, generated multi-gigabyte fixtures, local service state, logs, workspaces, and secrets. `backend/tests/fixtures/fixture_generator.py` can regenerate large fixtures locally.
