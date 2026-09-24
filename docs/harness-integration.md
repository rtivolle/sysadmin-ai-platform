# Harness integration

The `packages/harness-integration/` tree integrates a stock DeepSeek Harness
(`dsh`) installation with the Python platform backend. It does **not** vendor or
deploy Harness itself; it supplies a profile, a Cordis plugin and a multi-user
login gateway. Node 22+ is required.

```text
packages/harness-integration/
  profile/                    Cordis profile layer (model route, bind host/port)
  dsh-plugin-sysadmin/        custom harness bundle
    index.js                  policy guard, result observer, backend tool bridge
    lib/policy.js             JavaScript port of the backend command policy
    lib/backend.js            authenticated HTTP client for the backend
    lib/audit.js              audit sink (same schema as the backend)
    cordis.patch.yml          plugin configuration from environment
  gateway/                    multi-user front door
    server.js                 login, session, HTTP/WebSocket proxy
    instance-manager.js       one dsh process per authenticated user
    session-store.js          opaque in-memory gateway sessions
    config.js                 environment-driven configuration
  tests/                      node --test suites
```

## 1. Multi-user gateway

The shipped Harness is single-tenant: one home, one credential set, one
workspace. The gateway makes it multi-tenant by launching **one `dsh` process
per authenticated sysadmin** and routing that identity to it.

### Login and session flow

1. The browser loads `/api/gateway/login` (a minimal HTML form) or posts
   credentials directly.
2. The gateway forwards `{username, password}` to the platform auth gateway
   (`POST {authUrl}/api/v1/auth/login`) and never persists the password.
3. On success it stores an opaque 256-bit gateway session cookie
   (`sysadmin_gateway`, HttpOnly, SameSite=Lax, 24 h TTL) mapping to the
   authenticated `user`.
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

The **environment is the isolation boundary**: each user gets a distinct
`DSH_HOME`, workspace, key and port. A user's LiteLLM virtual key is never
written into shared configuration.

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
| `SYSADMIN_HARNESS_STATE` | `backend/data/harness` | Gateway state root. |
| `SYSADMIN_DSH_HOME_ROOT` | `<state>/homes` | Per-user `DSH_HOME`. |
| `DSH_BIN` | `dsh` | Harness executable. |
| `SYSADMIN_PROFILE` | `sysadmin` | Profile name. |
| `SYSADMIN_GATEWAY_COOKIE` | `sysadmin_gateway` | Cookie name. |
| `SYSADMIN_SESSION_TTL_MS` | `86400000` | Session lifetime. |
| `SYSADMIN_INSTANCE_READY_TIMEOUT_MS` | `60000` | Instance startup timeout. |

### Gateway endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/gateway/health` | Health, active users and session count. |
| GET | `/api/gateway/login` | Login HTML page. |
| POST | `/api/gateway/login` | Authenticate (JSON or form). |
| POST | `/api/gateway/logout` | Clear the gateway session. |
| any | everything else | Proxied to the user's harness instance (HTTP + WebSocket). |

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

## 4. Running and testing

The gateway is started separately from `platform.sh`:

```bash
cd packages/harness-integration/gateway
node server.js
```

Unit tests (Node's built-in runner) cover the policy port, the audit sink and the
gateway:

```bash
cd packages/harness-integration
node --test tests/
```

> **Caveat.** The gateway's session store is in-memory and per-process, so it is
> single-instance only. Harness and Dynamo are not deployed by this codebase; the
> integration assumes an operator-installed `dsh` on `PATH`.
