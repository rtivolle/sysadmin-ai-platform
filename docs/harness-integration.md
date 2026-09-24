# Harness integration

The `packages/harness-integration/` tree integrates a stock DeepSeek Harness
(`dsh`) installation with the Python platform backend. It does **not** vendor or
deploy Harness itself; it supplies a profile, a Cordis plugin, a multi-user
login gateway and a Mila-branded admin console. Node 22+ is required.

```text
packages/harness-integration/
  profile/                    Cordis profile layer (model route, bind host/port)
  dsh-plugin-sysadmin/        custom harness bundle
    index.js                  policy guard, result observer, backend tool bridge
    lib/policy.js             JavaScript port of the backend command policy
    lib/backend.js            authenticated HTTP client for the backend
    lib/audit.js              audit sink (same schema as the backend)
    lib/branding.js           Mila title/favicon via the webserver tapIndex seam
    cordis.patch.yml          plugin configuration from environment
  gateway/                    multi-user front door + Mila admin console
    server.js                 login, session, HTTP/WebSocket proxy
    admin.js                  admin console API (master-token auth)
    admin-ui.html/.js/.css    admin console UI (vanilla JS, no build step)
    instance-manager.js       one dsh process per authenticated user
    session-store.js          opaque file-persisted gateway sessions
    state-file.js             atomic 0600 state writes (temp + fsync + rename)
    assets/                   vendored Mila logo and shared brand.css
    config.js                 environment-driven configuration
  tests/                      node --test suites
```

## 1. Multi-user gateway

The shipped Harness is single-tenant: one home, one credential set, one
workspace. The gateway makes it multi-tenant by launching **one `dsh` process
per authenticated sysadmin** and routing that identity to it.

### Login and session flow

1. The browser loads `/api/gateway/login` (a Mila-branded HTML form) or posts
   credentials directly.
2. The gateway forwards `{username, password}` to the platform auth gateway
   (`POST {authUrl}/api/v1/auth/login`) and never persists the password.
3. On success it stores an opaque 256-bit gateway session cookie
   (`sysadmin_gateway`, HttpOnly, SameSite=Lax, 24 h TTL) mapping to the
   authenticated `user`. Sessions persist to `<state>/sessions.jsonl`
   (JSONL, 0600, temp + fsync + rename), are pruned at boot, and survive a
   gateway restart; `lastSeenAt` is touched on every request and flushed on a
   short debounce.
4. The user's harness instance is started eagerly so the first page load is warm.
5. Subsequent HTTP requests and WebSocket upgrades are proxied to that user's
   loopback instance; the gateway never derives identity from a client header.

### Per-user instance

`InstanceManager.ensure(userId)`:

- Validates the user id against `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`.
- Provisions a private Home: copies the profile `package.json` and
  `cordis.patch.yml`, and replaces `node_modules/dsh-plugin-sysadmin` with the
  current checkout (idempotent, so upgrades take effect).
- Creates the workspace `0700` and the audit state directory.
- Reserves a free loopback port in `SYSADMIN_INSTANCE_PORT_START..END`
  (defaults 3180–3280).
- Spawns `dsh --profile sysadmin --no-open --port <port>` with a per-user
  environment that includes `DSH_HOME`, the user's virtual key and the backend
  URLs.
- Waits until the instance answers, then exposes its launch token so the browser
  can establish the harness's own cookie.
- Persists a registry entry (`<state>/instances/<user>.json`, 0600) with
  `userId`, `port`, `pid`, `startedAt`, `home` and `workspace` — never the
  launch token or any key.
- Reuses the recorded port when it is still free, so a user's URL is stable
  across restarts.
- Appends stdout and stderr to `<state>/logs/<user>.log` (0600, rotated at
  2 MiB with one `.1` file), which the admin console tails.

The **environment is the isolation boundary**: each user gets a distinct
`DSH_HOME`, workspace, key and port. A user's LiteLLM virtual key is never
written into shared configuration.

### Persistence and supervision

Sessions and instance records are ordinary files under the gateway state root,
so a gateway restart does not log users out and does not cold-start their
harness:

- Browser sessions live in `sessions.jsonl` and admin sessions in
  `admin-sessions.jsonl` (JSONL, 0600, temp + fsync + rename); both are loaded
  and pruned at boot. `lastSeenAt` updates are debounced, not written per
  request.
- On boot the manager re-adopts every registry entry whose pid is alive **and**
  whose port answers, on the same port. Entries that fail either check are
  dropped. A port that answers under a dead pid is logged as an unknown owner
  and left alone — the manager never guesses ownership of a port.
- Persisted browser sessions trigger an eager, non-blocking re-ensure at boot,
  so returning users find a warm harness.
- An unexpected child exit is restarted with exponential backoff
  (`SYSADMIN_RESTART_BACKOFF_MS` base, doubling, capped at 30 s) up to
  `SYSADMIN_RESTART_MAX_ATTEMPTS` (default 3); after that the instance is
  marked `failed` and `failureReason` carries the tail of its output.
- A gateway SIGTERM deliberately leaves harness instances running so a
  restarted gateway re-adopts them; `./platform.sh stop` and
  `./platform.sh harness-stop` reap them from the registry.
  `SYSADMIN_INSTANCE_STOP_ON_EXIT=1` makes the gateway stop them on exit
  instead.
- Optional idle eviction: instances idle for longer than
  `SYSADMIN_INSTANCE_IDLE_TTL_MS` and holding no live session are stopped
  (default `0` keeps them).

### Configuration (environment)

| Variable | Default | Meaning |
|---|---|---|
| `SYSADMIN_GATEWAY_HOST` / `_PORT` | `127.0.0.1` / `3085` | Gateway bind address. |
| `SYSADMIN_AUTH_URL` | `http://127.0.0.1:3081` | Platform auth gateway. |
| `SYSADMIN_BACKEND_URL` | `http://127.0.0.1:3080` | Agent platform. |
| `SYSADMIN_LITELLM_URL` | `http://127.0.0.1:4000/v1` | LiteLLM gateway. |
| `VICTORIALOGS_URL` | `http://127.0.0.1:9428` | Audit store. |
| `SYSADMIN_KEYS_DIR` | `backend/config/keys` | Per-user bearer keys. |
| `SYSADMIN_WORKSPACE_ROOT` | `backend/data/workspaces` | Workspace root. |
| `SYSADMIN_HARNESS_STATE` | `backend/data/harness` | Gateway state root (`<state>` below). |
| `SYSADMIN_DSH_HOME_ROOT` | `<state>/homes` | Per-user `DSH_HOME`. |
| `SYSADMIN_SESSIONS_FILE` | `<state>/sessions.jsonl` | Browser session store (JSONL). |
| `SYSADMIN_ADMIN_SESSIONS_FILE` | `<state>/admin-sessions.jsonl` | Admin session store (JSONL). |
| `SYSADMIN_INSTANCE_REGISTRY_DIR` | `<state>/instances` | Per-user instance registry. |
| `SYSADMIN_INSTANCE_LOG_DIR` | `<state>/logs` | Per-user instance logs. |
| `SYSADMIN_INSTANCE_IDLE_TTL_MS` | `0` (disabled) | Stop instances idle longer than this with no live session. |
| `SYSADMIN_RESTART_MAX_ATTEMPTS` | `3` | Supervised restart attempts before an instance is `failed`. |
| `SYSADMIN_RESTART_BACKOFF_MS` | `1000` | Base delay for exponential restart backoff. |
| `SYSADMIN_ADMIN_TTL_MS` | `43200000` (12 h) | Admin session lifetime. |
| `SYSADMIN_ADMIN_COOKIE` | `sysadmin_admin` | Admin session cookie name. |
| `SYSADMIN_MASTER_KEY_FILE` | `<keys>/master.key` | Master token the console verifies. |
| `SYSADMIN_LOGIN_CREDENTIALS_FILE` | `<keys>/login-credentials.json` | Users listed by the console. |
| `SYSADMIN_INITIAL_PASSWORDS_FILE` | `<keys>/initial-passwords.txt` | Where rotated passwords land. |
| `SYSADMIN_PROVISION_LOGINS` | `<keys>/provision-logins.py` | Rotation script used by the console. |
| `SYSADMIN_PYTHON` | `backend/.venv/bin/python3` | Python for the rotation script. |
| `SYSADMIN_PLATFORM_SH` | `./platform.sh` | Service-restart helper used by the console. |
| `SYSADMIN_TRAEFIK_PORT` | `8080` | Traefik probe port. |
| `SYSADMIN_INFERENCE_PORT` | `8000` | Inference probe port. |
| `SYSADMIN_SEAWEEDFS_PORT` | `8333` | SeaweedFS S3 port (status display). |
| `SYSADMIN_SEAWEEDFS_MASTER_PORT` | `9333` | SeaweedFS master health-probe port. |
| `SYSADMIN_VALKEY_PORT` | `6379` | Valkey probe port. |
| `DSH_BIN` | `dsh` | Harness executable. |
| `SYSADMIN_PROFILE` | `sysadmin` | Profile name. |
| `SYSADMIN_GATEWAY_COOKIE` | `sysadmin_gateway` | Cookie name. |
| `SYSADMIN_SESSION_TTL_MS` | `86400000` | Session lifetime. |
| `SYSADMIN_INSTANCE_READY_TIMEOUT_MS` | `60000` | Instance startup timeout. |

### Gateway endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/gateway/health` | Health, active users and session count. |
| GET | `/api/gateway/login` | Mila-branded login page. |
| POST | `/api/gateway/login` | Authenticate (JSON or form). |
| POST | `/api/gateway/logout` | Clear the gateway session. |
| GET | `/admin` | Mila-branded admin console (separate master-token auth). |
| any | `/api/admin/*` | Admin console API (table below). |
| any | everything else | Proxied to the user's harness instance (HTTP + WebSocket). |

### Admin console

`http://127.0.0.1:3085/admin` serves a Mila-branded console for the platform
operator. Authorization stays local: the operator holds
`backend/config/keys/master.key`, the same secret the backend accepts as the
`sysadmin-admin` bearer.

- Login verifies the token with a constant-time compare against the key file and
  then probes the backend (`GET /api/approvals/pending` with the bearer token).
  An explicit `401`/`403` rejects the login; an unreachable backend is accepted
  with a warning, because the local key file is the authority and the console
  must stay usable when services are down.
- On success the console issues `sysadmin_admin` (HttpOnly, SameSite=Lax, 12 h),
  persisted to `admin-sessions.jsonl` like the browser sessions.
- Every `/api/admin/*` POST except `/api/admin/logout` requires a JSON content
  type (`415` otherwise), the `X-Sysadmin-Admin: 1` header (`403`) and, when the
  browser sends one, a same-origin `Origin` (`403`).
- The console never returns the master token or user keys; browser sessions are
  exposed only as 16-character SHA-256 handles of the cookie value.

| Method | Path | Purpose | Backend call |
|---|---|---|---|
| GET | `/api/admin/session` | Unauthenticated login probe; reports gateway ports. | — |
| POST | `/api/admin/login` | Verify the master token, start the admin session. | `GET /api/approvals/pending` |
| POST | `/api/admin/logout` | Clear the admin session. | — |
| GET | `/api/admin/overview` | Gateway counters plus live service probes. | service health endpoints + Valkey TCP |
| GET | `/api/admin/sessions` | List browser sessions with instance status. | — |
| POST | `/api/admin/sessions/{id}/revoke` | Revoke one session; body `{"stopInstance":true}` also stops its harness. `id` is the 16-char SHA-256 handle. | — |
| GET | `/api/admin/instances` | List harness instances. | — |
| POST | `/api/admin/instances/{user}/start\|stop\|restart` | Supervise one user's instance. | — |
| GET | `/api/admin/instances/{user}/logs?tail=` | Tail the user's log (`tail` 1 KiB–200 KiB, default 32 KiB). | — |
| GET | `/api/admin/users` | Provisioned users, key presence, sessions, instance. | — |
| POST | `/api/admin/users/{user}/rotate-password` | Rotate one login password; the response names `initial-passwords.txt` and never contains the value. | `provision-logins.py --user <user> --rotate` |
| GET | `/api/admin/approvals` | Pending approvals. | `GET /api/approvals/pending` |
| POST | `/api/admin/approvals/decide` | Approve or reject an approval. | `POST /api/approvals/decide` |
| GET | `/api/admin/audit?query=&limit=` | Query the audit trail (LogsQL; `limit` ≤ 500, default 100). | VictoriaLogs `/select/logsql/query` |
| GET | `/api/admin/services` | Service list with ports and pid liveness. | — |
| POST | `/api/admin/services/{name}/restart` | Restart one service, serialized, 30 s timeout. | `platform.sh service <name> restart` |

## 2. Profile layer

`profile/cordis.patch.yml` is the deployment layer applied after the base and
web-app layers. It:

- Points the `litellm` provider at `SYSADMIN_LITELLM_URL` and reads the per-user
  virtual key from `SYSADMIN_LITELLM_KEY`.
- Declares the `fast-model` and `heavy-model` aliases (32,768-token context).
- Selects the default model (`SYSADMIN_DEFAULT_MODEL`, default `heavy-model`).
- Binds the harness web server to `SYSADMIN_HARNESS_HOST:PORT`.

`profile/package.json` lists the bundle order:
`@deepseek-ai/dsh-base`, `@deepseek-ai/dsh-web-app`, `dsh-plugin-sysadmin`.

## 3. The `dsh-plugin-sysadmin` bundle

The plugin wires the harness to the backend in three directions. All
configuration is read from the process environment (per-user), via
`dsh-plugin-sysadmin/cordis.patch.yml`.

### 3.1 Command policy guard

`lib/policy.js` is a faithful JavaScript port of the backend's
`backend/services/approval_gate/filter.py`. On `tools/pre-execute` it classifies
shell tool arguments as `BLOCKED`, `ALLOW` or `APPROVAL_REQUIRED`:

- `BLOCKED` → the tool call is denied immediately and audited.
- `APPROVAL_REQUIRED` → the request is audited and handed to the harness's own
  approval seam; the **backend gate remains authoritative** for real execution.
- `ALLOW` → passed through.

Keeping one classification is intentional: the harness must not become a weaker
second policy engine than the backend.

### 3.2 Result observer

On `tools/result` the plugin emits one audit record per tool outcome, using the
same event schema as the Python backend, so harness activity can be correlated
with backend events by `user_id`/`session_id`.

### 3.3 Backend tool bridge

It registers `sysadmin_backend_tool`, which forwards a named bounded tool and a
JSON parameter object to `POST {backendBaseUrl}/api/tools/execute` with the
caller's bearer token. This is the only path by which harness-originated tool
requests reach the backend, and it is identity-checked, quota-controlled,
approval-gated and audited there.

### Audit sink

`lib/audit.js` posts the same JSON event to VictoriaLogs and falls back to
`fsync`-ed JSONL spooling, mirroring the backend's at-least-once semantics.

## 4. Mila branding

- The Mila logo is vendored at `gateway/assets/mila-logo.png` (from
  <https://mila.quebec/>) and served locally; `gateway/assets/brand.css` is the
  shared stylesheet (accent `#003cc5`, slate `#353641`).
- The gateway login page, the admin console and the favicon are Mila-branded and
  link back to <https://mila.quebec/>.
- The harness surface itself is not forked.
  `dsh-plugin-sysadmin/lib/branding.js` hooks the webserver's supported
  `tapIndex` seam to set the title `Mila — Sysadmin AI` and the Mila favicon
  (proxied through the gateway). Deeper in-app theming is not supported by the
  harness seams.

## 5. Running and testing

`platform.sh` starts the gateway, and the `service` subcommand restarts any
individual service the admin console offers:

```bash
./platform.sh harness                          # start the gateway on :3085
./platform.sh service harness_gateway status   # start|stop|restart|status
./platform.sh service agent_tools restart
./platform.sh harness-stop                     # stop the gateway and reap instances
```

`start_harness` prefers the stable `dsh` on `PATH` (a global
`@deepseek-ai/dsh` install) over an npx cache and honors `DSH_BIN`. The agent
platform port is `SYSADMIN_AGENT_PORT` (default `3080`), and `start_harness`
points the gateway's `SYSADMIN_BACKEND_URL` at it by default.

On a host where a running DeepSeek Harness GUI already owns `:3080`, run the
agent platform on another port and leave the gateway pointed at it:

```bash
SYSADMIN_AGENT_PORT=3090 ./platform.sh service agent_tools restart
SYSADMIN_AGENT_PORT=3090 SYSADMIN_BACKEND_URL=http://127.0.0.1:3090 ./platform.sh harness
```

A full-stack run must also point the Traefik `agent-service` route at the same
port. See [`status/TEST_READY.md`](status/TEST_READY.md) for the current host
note.

Unit tests (Node's built-in runner) cover the policy port, the audit sink, the
gateway, session persistence, Mila branding and the admin console:

```bash
cd packages/harness-integration
node --test tests/
```

> **Caveat.** The gateway is a single process: session and instance state are
> files, so a restart preserves logins and re-adopts instances, but two gateways
> must not share one state root. Harness and Dynamo are not deployed by this
> codebase; the integration assumes an operator-installed `dsh` on `PATH`.
