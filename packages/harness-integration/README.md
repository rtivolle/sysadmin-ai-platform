# Custom DeepSeek Harness integration

This package wires a **custom DeepSeek Harness (`dsh`) profile** into the sysadmin
AI platform so the agent runtime is the real harness instead of a hand-written
reAct loop, and so **multiple sysadmins can log in** and have their data flow
through the platform backend. The gateway also ships a Mila-branded admin
console at `/admin` for the platform operator.

It is the first slice of [`docs/plans/DEVELOPMENT_PLAN.md`](../../docs/plans/DEVELOPMENT_PLAN.md)'s
"per-user Harness runtime + home + session store" architecture and of
specification document [`03 - Service Agent & Outils`](../../docs/specs/README.md)
(DeepSeek Harness & Plugins).

## Why a gateway: the harness is single-tenant

`@deepseek-ai/dsh`'s web surface authenticates **one** browser with a per-launch
token (`http://127.0.0.1:<port>/?token=…`). It has no user accounts, no login, and
one credential store, one session store and one workspace per process.

Multi-user is therefore built as **one harness process per authenticated
sysadmin**, with the process environment as the isolation boundary:

```
                       POST /api/gateway/login
  browser  ────────────────────────────────────►  harness gateway  ──►  auth gateway :3081
     │                                            (multi-user front)      (PBKDF2 + Valkey sessions)
     │  session cookie (opaque, HttpOnly)
     ▼
  gateway routes by identity ──► dsh #1  (sysadmin-01)  DSH_HOME=homes/sysadmin-01, key=sysadmin-01.key, port 3180
                             ──► dsh #2  (sysadmin-02)  DSH_HOME=homes/sysadmin-02, key=sysadmin-02.key, port 3181
                             ──► …

  operator ─── /admin (master key) ──► Mila admin console (users, sessions, instances, approvals, audit, services)
```

Each `dsh` instance gets its own `DSH_HOME`, workspace (`cwd`, the sandbox root),
loopback port, and `SYSADMIN_LITELLM_KEY`. No credential is ever written into a
shared configuration file.

## Data flow into the backend

| Data | Path | Contract |
|---|---|---|
| Model / token traffic | profile `llm-pi-ai` `litellm` route → **LiteLLM :4000** | `apiKeyEnv: SYSADMIN_LITELLM_KEY` = the user's virtual key, so concurrency, RPM/TPM and daily-token quotas are charged to the right identity |
| Tool execution | plugin tool `sysadmin_backend_tool` → **agent platform :3080** `POST /api/tools/execute` | bearer token = `backend/config/keys/<user>.key`; the backend's bounded tools, approval gate and workspace confinement stay authoritative |
| Audit | plugin `tools/result` + policy decisions → **VictoriaLogs :9428** `POST /insert/jsonline` | same event schema as `backend/services/agent_tools/audit.py`; durable local outbox on collector outage |
| Identity | gateway → **auth gateway :3081** `POST /api/v1/auth/login` | gateway stores only an opaque session id, never the password |

Command safety is enforced **twice with the same rules**: the plugin's
`tools/pre-execute` guard classifies shell commands with a port of the backend's
`backend/services/approval_gate/filter.py` (`BLOCKED` / `ALLOW` /
`APPROVAL_REQUIRED`), and a parity test asserts the two engines agree
action-for-action.

## Persistence and supervision

The gateway survives its own restarts:

- Browser sessions persist to `<SYSADMIN_HARNESS_STATE>/sessions.jsonl` (JSONL,
  0600, temp + fsync + rename) and are loaded and pruned at boot; `lastSeenAt`
  is flushed on a short debounce. Admin sessions persist the same way to
  `admin-sessions.jsonl`.
- Each user's instance is recorded in `instances/<user>.json` with
  `{userId, port, pid, startedAt, home, workspace}` — never the launch token or
  any key. At boot, entries whose pid is alive **and** whose port answers are
  re-adopted on the same port; anything else is dropped. A port answering under
  a dead pid is logged as an unknown owner and left alone. Persisted sessions
  also trigger an eager, non-blocking re-ensure at boot.
- Ports are reused when still free, so a user's URL is stable.
- An unexpected child exit is restarted with exponential backoff
  (`SYSADMIN_RESTART_MAX_ATTEMPTS`, default 3; backoff base
  `SYSADMIN_RESTART_BACKOFF_MS`, default 1000 ms), then marked `failed` with the
  stderr tail.
- Per-user output goes to `logs/<user>.log`, rotated at 2 MiB (one `.1` file).
- A gateway SIGTERM deliberately leaves harness instances running so a restart
  re-adopts them; `./platform.sh stop` / `harness-stop` reaps them from the
  registry. `SYSADMIN_INSTANCE_STOP_ON_EXIT=1` makes the gateway stop them on
  exit. Optional idle eviction is `SYSADMIN_INSTANCE_IDLE_TTL_MS` (default 0 =
  keep).

## Admin console and branding

`http://127.0.0.1:3085/admin` is a Mila-branded console for the platform
operator. Auth is the master token from `backend/config/keys/master.key`
(constant-time compare plus a bearer probe of the backend, which an unreachable
backend downgrades to a warning); the session cookie is `sysadmin_admin`
(HttpOnly, SameSite=Lax, 12 h). Mutations require JSON, `X-Sysadmin-Admin: 1`
and a same-origin `Origin`. The console lists users, browser sessions (16-char
SHA-256 handles, never raw cookies), instances, approvals, audit events and
services, and can start/stop/restart a user's harness, tail its logs, rotate a
login password and restart a backend service through
`./platform.sh service <name> restart`. It never returns the master token or
user keys.

The Mila logo (`gateway/assets/mila-logo.png`, vendored from
<https://mila.quebec/>) and the shared `gateway/assets/brand.css` (accent
`#003cc5`, slate `#353641`) brand the login page and console. The harness
surface gets the title `Mila — Sysadmin AI` and the Mila favicon through the
plugin's supported webserver `tapIndex` seam
(`dsh-plugin-sysadmin/lib/branding.js`), not a UI fork; deeper in-app theming is
not supported.

## Layout

```
packages/harness-integration/
├── profile/                     # the `sysadmin` dsh profile
│   ├── package.json             # dsh.profile.bundles: dsh-base, dsh-web-app, dsh-plugin-sysadmin
│   └── cordis.patch.yml         # deployment layer: LiteLLM route, default model, bind
├── dsh-plugin-sysadmin/         # the custom harness bundle
│   ├── package.json             # dsh.bundle.patch
│   ├── cordis.patch.yml         # inserts the sysadmin-harness row (env-driven config)
│   ├── index.js                 # policy guard, audit observer, backend tool bridge, Mila title
│   └── lib/{policy,audit,backend,branding}.js
├── gateway/                     # multi-user login gateway + admin console
│   ├── server.js                # login/logout, session cookie, HTTP + WebSocket proxy
│   ├── admin.js                 # admin console API (master-token auth, CSRF, proxying)
│   ├── admin-ui.html/.js/.css   # admin console UI (vanilla JS, no build step)
│   ├── instance-manager.js      # per-user dsh process lifecycle + persistence
│   ├── session-store.js         # opaque file-persisted gateway sessions
│   ├── state-file.js            # atomic 0600 state writes (temp + fsync + rename)
│   ├── assets/                  # vendored Mila logo + shared brand.css
│   └── config.js
├── scripts/verify-harness.mjs   # real-dsh verification (compose, boot, banner, 401, Mila branding)
├── tests/                       # node:test suite (policy parity, audit, gateway, persistence, branding, admin)
└── install-harness.sh           # stage the profile into $DSH_HOME/profiles/sysadmin
```

## Install and run

```bash
# 1. Backend services (auth gateway, agent platform, LiteLLM, VictoriaLogs, Valkey)
./platform.sh start

# 2. Stage the harness profile into $DSH_HOME (~/.dsh)
packages/harness-integration/install-harness.sh

# 3. Verify against the real dsh binary
node packages/harness-integration/scripts/verify-harness.mjs

# 4. Start the multi-user gateway on :3085
./platform.sh service harness_gateway start
# equivalent direct start: node packages/harness-integration/gateway/server.js
```

Then browse to the gateway, sign in as `sysadmin-01` with the password from
`backend/config/keys/initial-passwords.txt`, and the gateway starts that user's
harness instance. The operator console is at `http://127.0.0.1:3085/admin` and
takes the master token from `backend/config/keys/master.key`.
`./platform.sh harness-stop` stops the gateway and reaps the instances it left
running.

Environment knobs (all optional): `SYSADMIN_GATEWAY_PORT` (3085),
`SYSADMIN_AUTH_URL` (3081), `SYSADMIN_BACKEND_URL` (3080), `SYSADMIN_LITELLM_URL`
(4000), `SYSADMIN_HARNESS_STATE`, `SYSADMIN_INSTANCE_PORT_START/END`
(3180–3280), `DSH_BIN`, `DSH_HOME`. Persistence, restart and admin-console
settings (`SYSADMIN_SESSIONS_FILE`, `SYSADMIN_ADMIN_SESSIONS_FILE`,
`SYSADMIN_INSTANCE_REGISTRY_DIR`, `SYSADMIN_INSTANCE_LOG_DIR`,
`SYSADMIN_INSTANCE_IDLE_TTL_MS`, `SYSADMIN_RESTART_MAX_ATTEMPTS`,
`SYSADMIN_RESTART_BACKOFF_MS`, `SYSADMIN_INSTANCE_STOP_ON_EXIT`,
`SYSADMIN_ADMIN_TTL_MS`, `SYSADMIN_ADMIN_COOKIE`, `SYSADMIN_MASTER_KEY_FILE`,
`SYSADMIN_LOGIN_CREDENTIALS_FILE`, `SYSADMIN_INITIAL_PASSWORDS_FILE`,
`SYSADMIN_PROVISION_LOGINS`, `SYSADMIN_PYTHON`, `SYSADMIN_PLATFORM_SH` and the
`SYSADMIN_TRAEFIK_PORT` / `SYSADMIN_INFERENCE_PORT` / `SYSADMIN_SEAWEEDFS_PORT` /
`SYSADMIN_VALKEY_PORT` probes) are listed in
[`../../docs/harness-integration.md`](../../docs/harness-integration.md#configuration-environment).

## Tests

```bash
node --test packages/harness-integration/tests/
```

The suite covers command-policy classification and **Python parity**, audit
schema + outbox fallback, session/cookie handling, per-user provisioning, the
full login → identity-scoped instance → proxy → logout path, session persistence
across a gateway restart (the `session-persistence` suite), the Mila branding
rewrite (the `branding` suite) and the admin console (the `admin` suite):
master-token login and CSRF checks, sessions, instances, users, approvals, audit
and service restart.

## Status and limits

- Verified here: profile composition (`--dump-config`), plugin load on a real
  `dsh` boot, web surface binding and unauthenticated refusal, plus the Node test
  suite and Python policy parity.
- The gateway starts real `dsh` processes; that path is exercised by
  `scripts/verify-harness.mjs`, not by the unit suite (which injects a stub
  instance manager).
- Sandbox enforcement (`bwrap` + cgroups v2) is inherited from the profile and
  host; it is **not** qualified by this package. See
  [`../../docs/status/TEST_READY.md`](../../docs/status/TEST_READY.md).
- On the GPU host the `litellm` route should point at the real vLLM-backed
  LiteLLM deployment; locally the backend's inference engine is a simulator.
