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
| `backend/config/roles/deployment.env` | Machine role (`all`/`web`/`inference`/`data`) and peer addresses; Git-ignored, written by `install.sh` for every role. |
| `backend/config/keys/*` | Provisioned keys and login hashes (Git-ignored). |

The Valkey password placeholder `CONFIGURE_VIA_PLATFORM_SH` is replaced at start
time by `platform.sh` into `backend/run/valkey.conf` (mode `0600`).

### Machine role (one host or three)

`backend/config/roles/deployment.env` records whether this host runs the whole
platform (`all`, the default) or one tier of the three-machine split
(`web`, `inference`, `data`) together with the peer addresses it reaches.
`platform.sh` reads it to start only that role's services and to export the
peer URLs and hosts; `install.sh` reuses it when `--role` is omitted and
rewrites it on an explicit `--role` — including `--role all`, which resets the
peers to loopback and reverts the rendered files. The role-dependent files
(`valkey/valkey.conf`, `traefik/dynamic.yml`) are produced by
`backend/config/roles/render_config.py`. See [multi-host.md](multi-host.md) for
the full operator flow.

## 2. Ports

| Service | Port(s) | Bind |
|---|---|---|
| Traefik web / websecure / dashboard | 8080 / 8443 / 8081 | `:` (all interfaces) |
| LiteLLM | 4000 | `127.0.0.1` |
| Agent platform | 3080 | `127.0.0.1` |
| Auth gateway | 3081 | `127.0.0.1` |
| Inference engine | 8000 | `127.0.0.1` |
| Valkey | 6379 | `127.0.0.1` |
| PostgreSQL (durable control store) | 5433 | `127.0.0.1` |
| VictoriaLogs | 9428 | `127.0.0.1` |
| SeaweedFS S3 / master / filer / volume | 8333 / 9333 / 8888 / 8085 | `127.0.0.1` |
| Harness gateway | 3085 | `127.0.0.1` |
| Per-user harness instances | 3180–3280 | `127.0.0.1` |

Only Traefik listens on all interfaces. Keep it that way, and terminate TLS
before exposing it beyond the host. The durable control store (PostgreSQL,
§7) binds loopback on port 5433 — deliberately not 5432, so a host PostgreSQL
is never touched or confused with the platform cluster. In a three-machine split
it belongs on the state (D) tier once the lifecycle wiring in §7 is applied.

In a three-machine split ([multi-host.md](multi-host.md)) the `LAN_BIND_IP`
address is added to LiteLLM (I), VictoriaLogs (D) and SeaweedFS (D), and Valkey
(D) additionally binds it through `valkey/valkey.conf`; every other service stays
loopback. `backend/config/firewall/{web,inference,data}.nft` restrict who may
reach those ports.

## 3. Environment variables

### Backend services

| Variable | Default | Used by | Meaning |
|---|---|---|---|
| `LITELLM_URL` | `http://127.0.0.1:4000/v1` | react loop | Gateway base URL. |
| `VALKEY_URL` | platform-injected | quota, approvals, sessions | Redis/Valkey URL. Setting it enables fail-closed mode. |
| `VALKEY_HOST` / `VALKEY_PORT` / `VALKEY_PASSWORD` | `127.0.0.1` / `6379` / placeholder | auth gateway | Cookie-session store. |
| `VICTORIALOGS_URL` | `http://127.0.0.1:9428` | audit, backup | Audit ingest/query. |
| `UPSTREAM_VLLM_URL` | empty | inference | When set, proxy completions to a remote vLLM. |
| `MODELS_DIR` | `backend/data/models` | model manager | Local model store (Git-ignored). |
| `MODELS_REGISTRY` | `$MODELS_DIR/registry.json` | model manager | Registry file (0600, atomic). |
| `HF_TOKEN` / `HF_TOKEN_FILE` | empty / `config/keys/hf-token.key` | downloader | HuggingFace token for gated repos. |
| `HF_ENDPOINT` / `HF_HOME` / `HF_HUB_OFFLINE` | library defaults | downloader | Mirror, cache and offline mode (read by `huggingface_hub`). |
| `VLLM_BIN` | `backend/.vllm-venv/bin/vllm` when installed, otherwise `vllm` on `PATH` | model manager | vLLM executable; explicit values override the installer default. |
| `VLLM_VENV_DIR` | `backend/.vllm-venv` | installer/platform | Isolated Python environment for vLLM. |
| `MODEL_PORT_START` / `MODEL_PORT_END` | `8100` / `8199` | model manager | Per-model vLLM port range. |
| `MODEL_LOG_DIR` | `backend/logs/models` | model manager | vLLM log directory. |
| `LITELLM_CONFIG` | `config/litellm/config.yaml` | model manager | Config the managed `model_list` block is written to. |
| `MODEL_INFERENCE_API_BASE` | `http://127.0.0.1:8000/v1` | model manager | `api_base` for generated LiteLLM entries. |
| `QUOTA_TIMEZONE` | `UTC` | quota | Timezone for the daily rollover. |
| `ENFORCE_CLUSTER_CONCURRENCY` | `0` | quota | Enforce the 8/10-slot cluster ceiling. |
| `SESSION_TTL_SECONDS` | `86400` | session store | Session lifetime. |
| `TARGET_ADAPTER_SIMULATION` | `0` | target adapter | Simulate `systemctl` instead of running it. |
| `TARGET_CONFIG_ALLOW_TMP` | `0` | target adapter | Allow deployment into `/tmp` (tests). |
| `BWRAP_BIN` | `/usr/bin/bwrap` | sandbox runner | Bubblewrap path. |
| `SYSADMIN_LOGIN_CREDENTIALS_FILE` | `config/keys/login-credentials.json` | auth gateway | Credential file override. |
| `SYSADMIN_CONTROL_STORE` | `file` | auth gateway, quota manager | `postgres` makes the durable control store authoritative (key lifecycle + daily token ledger). Any other value keeps today's file mode. |
| `SYSADMIN_DATABASE_URL` | empty | control store | Explicit DSN; also selects `postgres` mode. Takes precedence over the composed local-cluster DSN. |
| `SYSADMIN_POSTGRES_HOST` / `_PORT` / `_DB` / `_USER` | `127.0.0.1` / `5433` / `sysadmin_control` / `sysadmin_control` | control store | Local-cluster coordinates used to compose the DSN. |
| `SYSADMIN_POSTGRES_PASSWORD_FILE` | `config/keys/postgres-password.key` | control store, `postgres.sh` | Cluster password (`0600`). Never printed: DSNs are only ever logged redacted. |
| `SYSADMIN_POSTGRES_DATA_DIR` / `_RUN_DIR` / `_LOG_FILE` / `_BIND` | `backend/data/postgres` / `backend/run/postgres` / `backend/logs/postgres.log` / `127.0.0.1` | `postgres.sh` | Cluster layout overrides (used by the tests, and by a split deployment). |
| `DATABASE_URL` | empty | LiteLLM | LiteLLM's own database (`database_url`): its spend logs and virtual-key tables. See §7 for the interim, unwired path. |

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
| `backend/data/postgres/` | Private PostgreSQL cluster (durable control store, Git-ignored). |
| `backend/run/postgres/` | PostgreSQL socket and PID file. |
| `backend/config/keys/postgres-password.key` | Cluster password (`0600`, Git-ignored, generated by `postgres.sh provision`). |
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

The admin console's **Quotas** tab can override concurrency, RPM, TPM and daily
tokens per user. Overrides are atomically replaced in Valkey at
`quota:limits:<user>` and read at admission time by the auth gateway, agent
runtime and LiteLLM custom authentication. They survive service restarts;
durability across a Valkey restart follows its configured persistence.

An override takes precedence over both standard and P1 defaults. **Valeurs par
défaut** removes overrides without resetting consumption or active reservations.
Lowering a limit affects new admissions, not already-running requests. Global
cluster limits still apply independently. Missing or invalid required shared
configuration fails closed. The console shows daily consumed/reserved tokens
and agent concurrency; LiteLLM's rolling RPM/TPM usage counters are not shown.

## 6. Changing configuration safely

1. Edit templates under `backend/config/`, not generated runtime files.
2. Restart the affected service (`./platform.sh restart` restarts everything).
3. For a configuration *deployment* through the target adapter, stage the new
   file inside the caller's workspace: the adapter validates, backups and
   atomically replaces the target, with rollback on failure.
4. Never put secrets into tracked files; secrets live in `config/keys/`.

## 7. Durable control store (PostgreSQL)

The platform can keep two durable things in one private, native PostgreSQL
cluster (zero Docker, port 5433, data under `backend/data/postgres`) instead of
in files and Valkey alone:

| Table | Holds | Note |
|---|---|---|
| `sysadmin_api_keys` | bearer-key lifecycle: `token_sha256`, user, label, creator, `revoked_at`, `rotated_to` | **hash only** — a dump of this table cannot be replayed as a credential |
| `sysadmin_token_ledger` | daily token reservations and settlements (`reservation_id`, day, estimate, settled, expiry) | the durable floor used to reconcile a restarted process |
| `sysadmin_schema_meta` | applied schema version | idempotent `apply-schema` |

LiteLLM owns its own tables (spend logs, virtual keys) in the same database when
`DATABASE_URL` is set for it; the platform never reads or migrates them.

### Why two stores

Valkey keeps what it is good at — atomic counters, leases, sessions, approvals —
and PostgreSQL keeps what must survive a restart. The daily-token contract is
split accordingly: Valkey admits atomically, PostgreSQL records the outcome, and
on the first admission of a day the counter is raised to the durable total in one
atomic step. An admitted-but-unreported reservation is charged at its estimate,
so a crash cannot silently make a request free.

### Enabling it

```bash
# 1. Install the server first (the script never installs packages):
#    Debian/Ubuntu  sudo apt-get install -y postgresql
#    RHEL/Fedora    sudo dnf install -y postgresql-server
#    macOS (dev)    brew install postgresql@17
backend/config/postgres/postgres.sh check        # exit 3 + the command to run when missing

# 2. Create the cluster, role, database and password file (mode 0600):
backend/config/postgres/postgres.sh provision

# 3. Create the platform tables and import the provisioned file keys once:
export SYSADMIN_CONTROL_STORE=postgres
backend/.venv/bin/python3 -m services.control_store.cli apply-schema
backend/.venv/bin/python3 -m services.control_store.cli import-file-keys

# 4. Select it for the services (see below), then restart:
./platform.sh restart
```

Selection is explicit. With `SYSADMIN_CONTROL_STORE=postgres` (or an explicit
`SYSADMIN_DATABASE_URL`) the store is **authoritative**: the auth gateway, LiteLLM
custom auth and the quota manager resolve identity and account usage through it,
and when it is unreachable they fail closed with **503** rather than falling back
to the file map or an in-memory counter. With the variable unset, behaviour is
exactly today's file mode — an installed cluster is simply not consulted.

`platform.sh` already sources and exports `backend/config/roles/deployment.env`,
so putting the selection there applies it to every service this host starts:

```bash
# backend/config/roles/deployment.env (Git-ignored)
SYSADMIN_CONTROL_STORE=postgres
```

### Operator commands

```bash
backend/config/postgres/postgres.sh status|health|start|stop|restart
backend/config/postgres/postgres.sh dsn                 # password redacted
backend/config/postgres/postgres.sh psql                # operator shell
backend/config/postgres/postgres.sh backup --out DIR/control-store.dump   # mode 0600
backend/config/postgres/postgres.sh restore --from DIR/control-store.dump --clean

export SYSADMIN_CONTROL_STORE=postgres
python3 -m services.control_store.cli status            # mode, redacted DSN, reachability
python3 -m services.control_store.cli list [--user U]   # live keys (metadata only)
python3 -m services.control_store.cli rotate --user sysadmin-01   # writes a 0600 file
python3 -m services.control_store.cli revoke --user sysadmin-01 [--token-file F]
python3 -m services.control_store.cli ledger --user sysadmin-01 [--day YYYY-MM-DD]
python3 -m services.control_store.cli prune --retention-days 30
```

Key rotation is atomic: the replacement is inserted and every other live key of
that user is revoked in one transaction, then the change is audited
(`api_key_issue` / `api_key_rotate` / `api_key_revoke` with a 12-character
non-reversible fingerprint — never a token). HTTP equivalents are
`GET /api/v1/admin/keys`, `POST /api/v1/admin/keys/{user}/rotate` and
`POST /api/v1/admin/keys/{user}/revoke` (see [http-api.md](http-api.md#auth-gateway-3081)).

### Backup

Until PostgreSQL is part of the resilience package's component list
([backup-restore.md](backup-restore.md)), back the cluster up with
`postgres.sh backup` and restore it with `postgres.sh restore --clean`; both
require a running cluster, and the dump is written `0600`.

### Pending lifecycle wiring

`backend/config/postgres/postgres.sh` is a complete, tested lifecycle, but it is
**not yet called by `platform.sh`**, and `install.sh` / `installer_tui.py` do not
provision it or write LiteLLM's `database_url` yet. That wiring is deliberately
deferred: those files carry another lane's uncommitted work
(`docs/plans/MULTI_HOST_DEPLOYMENT.md`, PR-H1), and AGENTS.md §8 forbids editing
them concurrently. The change is additive:

```diff
# backend/platform.sh
-SERVICE_START_ORDER="valkey victorialogs audit_outbox seaweedfs inference auth_gateway agent_tools litellm traefik"
+SERVICE_START_ORDER="postgres valkey victorialogs audit_outbox seaweedfs inference auth_gateway agent_tools litellm traefik"
+    postgres)  start_service "postgres" "${CONFIG_DIR}/postgres/postgres.sh" start ;;
```

plus `postgres` in the `data` role's service list, a
`backend/config/postgres/postgres.sh provision` call in `install.sh`, and (in
`installer_tui.py`) setting `general_settings.database_url` instead of popping it.

**Not yet measured on a live cluster:** provisioning, key issue/rotate/revoke
round trips, a LiteLLM start with `DATABASE_URL`, and a dump/restore cycle. The
implementation is unit-tested against doubles (66 tests); the live path, the
failing-test baseline comparison and the environment caveat are recorded in
[status/CONTROL_STORE.md](status/CONTROL_STORE.md).

