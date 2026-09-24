# Configuration reference

## 1. Configuration files

| File | Purpose |
|---|---|
| `backend/config/platform_config.json` | Human-readable defaults: user count, Traefik port, concurrency, RPM, daily budget. |
| `backend/config/litellm/config.yaml` | Model list, router settings, custom auth and callback wiring. |
| `backend/config/traefik/traefik.yml` | Static Traefik config (entry points, file provider). |
| `backend/config/traefik/dynamic.yml` | Routers, middlewares and services (ForwardAuth, header stripping, routing). |
| `backend/config/valkey/valkey.conf` | Valkey bind/port, password placeholder, AOF, 256 MB LRU. |
| `backend/config/sandbox/bwrap-runner.sh` | Bubblewrap + cgroup sandbox runner. |
| `backend/config/keys/*` | Provisioned keys and login hashes (Git-ignored). |

The Valkey password placeholder `CONFIGURE_VIA_PLATFORM_SH` is replaced at start
time by `platform.sh` into `backend/run/valkey.conf` (mode `0600`).

## 2. Ports

| Service | Port(s) | Bind |
|---|---|---|
| Traefik web / websecure / dashboard | 8080 / 8443 / 8081 | `:` (all interfaces) |
| LiteLLM | 4000 | `127.0.0.1` |
| Agent platform | 3080 | `127.0.0.1` |
| Auth gateway | 3081 | `127.0.0.1` |
| Inference engine | 8000 | `127.0.0.1` |
| Valkey | 6379 | `127.0.0.1` |
| VictoriaLogs | 9428 | `127.0.0.1` |
| SeaweedFS S3 / master / filer / volume | 8333 / 9333 / 8888 / 8085 | `127.0.0.1` |
| Harness gateway | 3085 | `127.0.0.1` |
| Per-user harness instances | 3180–3280 | `127.0.0.1` |

Only Traefik listens on all interfaces. Keep it that way, and terminate TLS
before exposing it beyond the host.

## 3. Environment variables

### Backend services

| Variable | Default | Used by | Meaning |
|---|---|---|---|
| `LITELLM_URL` | `http://127.0.0.1:4000/v1` | react loop | Gateway base URL. |
| `VALKEY_URL` | platform-injected | quota, approvals, sessions | Redis/Valkey URL. Setting it enables fail-closed mode. |
| `VALKEY_HOST` / `VALKEY_PORT` / `VALKEY_PASSWORD` | `127.0.0.1` / `6379` / placeholder | auth gateway | Cookie-session store. |
| `VICTORIALOGS_URL` | `http://127.0.0.1:9428` | audit, backup | Audit ingest/query. |
| `UPSTREAM_VLLM_URL` | empty | inference | When set, proxy completions to vLLM. |
| `QUOTA_TIMEZONE` | `UTC` | quota | Timezone for the daily rollover. |
| `ENFORCE_CLUSTER_CONCURRENCY` | `0` | quota | Enforce the 8/10-slot cluster ceiling. |
| `SESSION_TTL_SECONDS` | `86400` | session store | Session lifetime. |
| `TARGET_ADAPTER_SIMULATION` | `0` | target adapter | Simulate `systemctl` instead of running it. |
| `TARGET_CONFIG_ALLOW_TMP` | `0` | target adapter | Allow deployment into `/tmp` (tests). |
| `BWRAP_BIN` | `/usr/bin/bwrap` | sandbox runner | Bubblewrap path. |
| `SYSADMIN_LOGIN_CREDENTIALS_FILE` | `config/keys/login-credentials.json` | auth gateway | Credential file override. |

### CLI and install

| Variable | Default | Meaning |
|---|---|---|
| `GATEWAY_URL` | `http://127.0.0.1:8080` | CLI gateway base. |
| `SYSADMIN_USER` | `sysadmin-01` | CLI identity (use `sysadmin-admin` for administration). |

### Harness integration

See [harness-integration.md](harness-integration.md#configuration-environment)
for the full list (`SYSADMIN_GATEWAY_*`, `SYSADMIN_*_URL`,
`SYSADMIN_KEYS_DIR`, `SYSADMIN_WORKSPACE_ROOT`, `SYSADMIN_HARNESS_STATE`,
`DSH_BIN`, `DSH_HOME`, `SYSADMIN_LITELLM_KEY`, …).

## 4. Paths

| Path | Contents |
|---|---|
| `backend/bin/` | Downloaded native binaries (Git-ignored). |
| `backend/.venv/` | Python virtual environment (Git-ignored). |
| `backend/config/` | Templates and keys. |
| `backend/data/valkey/` | Valkey AOF/RDB. |
| `backend/data/victorialogs/` | VictoriaLogs partitions and `outbox.jsonl`. |
| `backend/data/seaweedfs/` | SeaweedFS master/filer/volume data. |
| `backend/data/workspaces/<user>/` | Per-user `0700` workspaces. |
| `backend/data/runbooks/` | Operational runbooks (Markdown). |
| `backend/data/harness/` | Harness gateway state and per-user homes. |
| `backend/logs/`, `backend/run/` | Logs and PID files. |
| `backend/tests/fixtures/` | Test fixtures (configs, logs, runbooks). |

## 5. Quota defaults

Edit `backend/config/platform_config.json` for documentation, but the enforced
defaults live in code:

| Setting | Standard | P1 |
|---|---|---|
| In-flight per user | 2 | 6 |
| RPM (rolling 60 s) | 60 | 200 |
| TPM | 150,000 | 500,000 |
| Daily tokens | 2,000,000 | 10,000,000 |
| Approval TTL | 300 s | 300 s |
| Sandbox memory / pids / CPU | 4 GiB / 128 / 200 % | same |
| Sandbox deadline | 15 s (+5 s kill grace) | same |

## 6. Changing configuration safely

1. Edit templates under `backend/config/`, not generated runtime files.
2. Restart the affected service (`./platform.sh restart` restarts everything).
3. For a configuration *deployment* through the target adapter, stage the new
   file inside the caller's workspace: the adapter validates, backups and
   atomically replaces the target, with rollback on failure.
4. Never put secrets into tracked files; secrets live in `config/keys/`.
