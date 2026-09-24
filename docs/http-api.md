# HTTP API reference

All services bind to `127.0.0.1`. In a normal deployment the only externally
reachable entry point is Traefik on `:8080` (HTTP) or `:8443`.
Authentication is a bearer token (`Authorization: Bearer <key>`) or a
`session_id` cookie.

Base URLs:

| Surface | Local base | Via Traefik |
|---|---|---|
| Auth gateway | `http://127.0.0.1:3081` | `http://127.0.0.1:8080` (auth routes) |
| Agent platform | `http://127.0.0.1:3080` | `http://127.0.0.1:8080` |
| LiteLLM | `http://127.0.0.1:4000` | `http://127.0.0.1:8080` |
| Inference | `http://127.0.0.1:8000` | not routed directly |
| VictoriaLogs | `http://127.0.0.1:9428` | `http://127.0.0.1:8080` (`/select`, `/insert`) |
| SeaweedFS S3 | `http://127.0.0.1:8333` | `http://127.0.0.1:8080/s3` |
| Harness gateway | `http://127.0.0.1:3085` | not routed directly |

The agent platform port is `3080` by default; `platform.sh` honors
`SYSADMIN_AGENT_PORT` on hosts where that port is already taken
(see [harness-integration.md](harness-integration.md#5-running-and-testing)).

---

## Auth gateway (:3081)

### `POST /api/v1/auth/login` (aliases `/auth/login`, `/login`)

Body: `{"username": "sysadmin-01", "password": "..."}`.

`200` sets a `session_id` cookie and returns
`{"status","user","session_id","role"}`. `401` on bad credentials. `503` when
the session store is unavailable.

### `POST /api/v1/auth/logout` (aliases `/auth/logout`, `/logout`)

Deletes the session and clears the cookie. Returns `{"status":"logged_out"}`.

### `GET|POST /verify` and `/api/v1/auth/verify`

Traefik ForwardAuth endpoint. Returns:

- `401` without valid credentials.
- `429` `{"error":"quota_exhausted","consumed_tokens","limit"}` when the daily
  budget is exhausted.
- `503` `{"error":"quota_state_unavailable"}` when quota state is unavailable.
- `200` with identity headers `X-Forwarded-User`, `X-Forwarded-Role`,
  `X-User`, `X-User-Role`, `X-User-Workspace`, `X-Auth-Method` and, during P1,
  `X-Priority`/`X-Incident-ID`.

### `POST /api/v1/auth/p1/elevate`

Requires authentication. Body:
`{"incident_id":"INC-1234","reason":"...","ttl_seconds":3600}`.
`incident_id` is mandatory and `ttl_seconds` ≤ 3600. Returns the P1 token,
priority and elevated limits. `400` on invalid input.

### `GET /api/v1/auth/p1/status`

Returns the caller's elevation status
(`elevated`, limits, incident id, remaining TTL).

### `POST /api/v1/auth/p1/revoke`

Revokes the caller's P1 elevation and every issued token.

### `GET /api/v1/admin/quotas`

Administrator only (`sysadmin-admin`; any other authenticated caller gets `403`,
unauthenticated `401`). Returns the per-user quota snapshot and the accepted
bounds:

```json
{
  "users": [{
    "user_id": "sysadmin-01",
    "limits": {"concurrency": 2, "rpm": 60, "tpm": 150000, "daily_tokens": 2000000},
    "p1_elevated": false,
    "overrides": {},
    "day": "2026-09-24",
    "timezone": "UTC",
    "usage": {"concurrency": 0, "daily_tokens": 0, "reserved_tokens": 0}
  }],
  "bounds": {"concurrency": [1, 10], "rpm": [1, 10000], "tpm": [1, 10000000], "daily_tokens": [1, 1000000000]}
}
```

`503` when the shared quota store is unavailable (fail closed).

### `POST /api/v1/admin/quotas/{user_id}`

Administrator only. Body must be exactly `{"limits": {...}}` where `limits` is a
subset of `concurrency`, `rpm`, `tpm`, `daily_tokens`; each value must be an
integer inside its bound. `{"limits":{}}` restores the standard/P1 defaults.
Returns `{"status":"updated","user_id":"sysadmin-01","overrides":{...}}`.

- `400` invalid body or value (`rpm: 0`, `true`, non-integer, unknown field);
- `404` unknown `user_id`;
- `503` shared store unavailable (the write is not confirmed).

A successful update is recorded as a `quota_update` audit event with the
reviewer as `user_id` and `parameters.{user_id,limits}`.

### `GET /health`

Service health.

---

## Agent platform (:3080)

### `GET /health`

Service health.

### `GET /api/v1/survey`

Administrator only: returns the read-only hardware survey as JSON; unauthenticated
requests receive `401` and other authenticated users receive `403`. Results are
cached for approximately 10 seconds. Add `?refresh=1` to bypass the cache and
run a fresh survey.

### `GET /api/tools/list`

Returns the four registered tools with names, descriptions and parameter lists.
Requires authentication.

### `POST /api/tools/execute`

Body: `{"name": "<tool>", "parameters": {...}, "session_id": "..."}`.

- `search_log_stream` / `config_lint_and_diff` / `doc_runbook_reader` →
  `200 {"status":"success","result":{...},"duration_ms":N}`.
- `sandboxed_bash` (aliases `bash`, `terminal`):
  - blocked → `403` with the policy reason;
  - needs approval without a valid `approval_id` → `202` with
    `{"status":"approval_required","approval_id","command"}`;
  - invalid/expired/reused approval → `403`;
  - executed → `200` with `exit_code`, `stdout`, `stderr`, `confined: true`.
- Unknown tool → `404`.

The success, blocked, approval-required, approval-denied and unknown-tool
dispatch paths emit schema-conformant audit events. Authentication and early
validation failures have separate coverage gaps. See
[status/AUDIT_CENSUS.md](status/AUDIT_CENSUS.md).

### `GET /api/approvals/pending`

Admin only. Returns `{"pending_approvals":[{approval_id,user_id,command,reason,...}]}`.

### `POST /api/approvals/decide`

Admin only. Body: `{"approval_id":"...","approved":true|false}`. Returns the
decided record; `400` if not decidable. Both outcomes emit an
`approval_decision` audit event naming the reviewer and the requester.

### Agent runtime — `/api/v1/agent` (alias `/agent`)

#### `POST /chat`

Body (`extra="forbid"`):

```json
{
  "prompt": "Search nginx logs for connect errors",
  "session_id": "sess-...",          // optional
  "model": "fast-model",              // fast-model | heavy-model
  "max_steps": 5,
  "stream": false,
  "workspace": "./backend/data/workspaces/sysadmin-01",
  "request_id": "req-..."
}
```

Response `AgentChatResponse`: `session_id`, `response`, `tools_executed[]`,
`approval_required`, `approval_id`, `command`, `user_id`, `turn_count`,
`citations[]`.

Errors: `401` unauthenticated; `409` duplicate `request_id`; `429` concurrency
ceiling; `503` shared quota state unavailable; `502` inference gateway failure.
With `Accept: text/event-stream` or `"stream": true`, the response is SSE:
`data: {"chunk":"...","citations":[...]}` … terminated by `data: [DONE]`.

#### `POST /cancel`

Body: `{"request_id":"..."}` or `{"session_id":"..."}`. Cancels only the
caller's own in-flight request. Returns `AgentCancelResponse`.

#### `GET /sessions`

Lists the caller's session ids.

#### `GET /sessions/{session_id}`

Returns the caller's session, or `404`.

#### `DELETE /sessions/{session_id}`

Deletes the caller's session.

### Target adapter

#### `POST /api/v1/approval/propose`

Body:

```json
{
  "user_id": "sysadmin-01",
  "session_id": "...",
  "action": "service_restart",
  "target": "nginx",
  "staged_path": "nginx.conf",       // config_deploy only
  "reason": "...",
  "workspace": "..."
}
```

Returns `202` with the approval token, hashes and expiry. `403` for
disallowed action/target; `400` for invalid input; `503` if the approval store
is unavailable. The body `user_id` must match the authenticated identity.

#### `POST /api/v1/approvals/decide`

Admin only. Body: `{"approval_id","approved","reason"}`. Returns `DecisionResponse`.

#### `POST /api/v1/adapter/execute`

Body: `{"approval_id","user_id","session_id","action","target","staged_path"}`.
The `user_id` must match. Executes the approved action and returns
`ExecutionResponse` (status, exit code, stdout/stderr, duration, backup path,
rollback flag). Claim failures return `403`.

#### `GET /api/v1/adapter/status/{approval_id}`

Returns the record for an admin or the owning user; others receive `404`.

### Model manager (agent platform)

Admin only (`403` for other authenticated users, `401` anonymous). Registers
HuggingFace repos, downloads snapshots or selected GGUF files, and controls a
local vLLM or llama.cpp server per model. Full lifecycle, flags and limitations are in
[model-management.md](model-management.md).

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/models` | List registered models with status/server info. |
| GET | `/api/v1/models/{name}` | One model plus its download-job status. |
| POST | `/api/v1/models` | Register `{hf_repo\|reference, name?, revision?, engine?}` → `201`. `engine="llamacpp"` requires a `gguf_file` basename; optional `ctx_size`, `n_gpu_layers`, and `flash_attn`. |
| POST | `/api/v1/models/{name}/download` | Start a background snapshot or single-GGUF download → `202`. |
| POST | `/api/v1/models/{name}/start` | Start the configured vLLM or llama.cpp server → `202`. |
| POST | `/api/v1/models/{name}/stop` | Stop the configured model server. |
| POST | `/api/v1/models/{name}/restart` | Stop then start → `202`. |
| GET | `/api/v1/models/{name}/logs?tail=` | Tail the selected engine log (`tail` clamped 1 KiB–256 KiB). |
| DELETE | `/api/v1/models/{name}?delete_files=0\|1` | Unregister (optionally delete files); `409` while running. |

`400` on invalid repo/name/revision or parameters, `404` unknown model, `409` on
a conflicting lifecycle transition. Mutations emit `model_*` audit events.

---

## LiteLLM (:4000)

Standard LiteLLM proxy surface, protected by Traefik and by custom auth:

- `GET /v1/models` — model list.
- `POST /v1/chat/completions` — chat completions (streaming supported).
- `GET /health`, `GET /health/liveliness`, `GET /health/readiness`.
- Admin/virtual-key routes (`/key/*`, `/user/*`) for the master key.

Every completion is authenticated by
`backend.services.auth_gateway.litellm_auth.sysadmin_custom_auth`, which
enforces the caller's per-user limits, and recorded by the
`QuotaLoggingHandler` callback.

---

## Inference engine (:8000)

- `GET /health`
- `GET /v1/models`
- `POST /v1/chat/completions` — simulates completion, or proxies to
  `UPSTREAM_VLLM_URL`.

---

## VictoriaLogs (:9428)

- `POST /insert/jsonline?_stream_fields=service,user_id,priority&_time_field=timestamp`
  — JSON-line ingest used by the audit module.
- LogsQL query endpoints (`/select/logsql/query`, etc.).

---

## Harness gateway (:3085)

The multi-user front door for the DeepSeek Harness integration. It also serves
the Mila-branded admin console. Sessions and instances persist under
`backend/data/harness`; see
[harness-integration.md](harness-integration.md#1-multi-user-gateway) for the
auth model, persistence and branding.

### Gateway surface

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/gateway/health` | Health, active users, session and instance counts. |
| GET | `/api/gateway/login` | Mila-branded login page. |
| POST | `/api/gateway/login` | Authenticate against the auth gateway (JSON or form). |
| POST | `/api/gateway/logout` | Clear the gateway session. |
| GET | `/admin`, `/assets/*` | Mila-branded admin console and its static assets. |
| any | everything else | Proxied to the caller's harness instance (HTTP and WebSocket). |

### Admin console API

The operator logs in with the master token from `backend/config/keys/master.key`
(constant-time compare against the key file, then a bearer probe of the backend;
an unreachable backend is allowed with a warning, an explicit `401`/`403` is
rejected). The session cookie is `sysadmin_admin` (HttpOnly, SameSite=Lax,
12 h), persisted to `admin-sessions.jsonl`. Every mutation except
`POST /api/admin/logout` requires `content-type: application/json`, the
`X-Sysadmin-Admin: 1` header and, when the browser sends one, a same-origin
`Origin` (`415`/`403` otherwise). Browser sessions are exposed only as
16-character SHA-256 handles of the cookie value; the master token and user
keys are never returned.

| Method | Path | Purpose | Backend call |
|---|---|---|---|
| GET | `/api/admin/session` | Unauthenticated login probe; reports gateway ports. | — |
| POST | `/api/admin/login` | Verify the master token, start the admin session. | `GET /api/approvals/pending` |
| POST | `/api/admin/logout` | Clear the admin session. | — |
| GET | `/api/admin/overview` | Gateway counters plus live service probes. | service health endpoints + Valkey TCP |
| GET | `/api/admin/sessions` | List browser sessions with instance status. | — |
| POST | `/api/admin/sessions/{id}/revoke` | Revoke one session (optional body `{"stopInstance":true}`); `id` is the 16-char SHA-256 handle. | — |
| GET | `/api/admin/instances` | List harness instances. | — |
| POST | `/api/admin/instances/{user}/start\|stop\|restart` | Supervise one user's instance. | — |
| GET | `/api/admin/instances/{user}/logs?tail=` | Tail the user's log (`tail` 1 KiB–200 KiB, default 32 KiB). | — |
| GET | `/api/admin/users` | Provisioned users, key presence, sessions, instance. | — |
| POST | `/api/admin/users/{user}/rotate-password` | Rotate one login password; the response names `initial-passwords.txt` and never contains the value. | `provision-logins.py --user <user> --rotate` |
| GET | `/api/admin/quotas` | Per-user quota snapshots and accepted bounds. | `GET /api/v1/admin/quotas` |
| POST | `/api/admin/quotas/{user}` | Replace a user's limit overrides (`{}` restores defaults). | `POST /api/v1/admin/quotas/{user}` |
| GET | `/api/admin/approvals` | Pending approvals. | `GET /api/approvals/pending` |
| POST | `/api/admin/approvals/decide` | Approve or reject an approval. | `POST /api/approvals/decide` |
| GET | `/api/admin/audit?query=&limit=` | Query the audit trail (LogsQL; `limit` ≤ 500, default 100). | VictoriaLogs `/select/logsql/query` |
| GET | `/api/admin/models` | Model catalogue (ids and owners) plus the inference mode. Provider configuration and credentials are never returned. | LiteLLM `GET /models` + inference `GET /health` |
| GET | `/api/admin/survey?refresh=0\|1` | Device/GPU/driver survey (cached 10 s; `refresh=1` re-runs the probes). | agent platform `GET /api/v1/survey` |
| GET/POST | `/api/admin/local-models` | List or register local models. | agent platform `/api/v1/models` |
| POST | `/api/admin/local-models/{name}/{download\|start\|stop\|restart\|delete}` | Local model lifecycle; `delete` accepts `?delete_files=1`. | agent platform `/api/v1/models/...` |
| GET | `/api/admin/local-models/{name}/logs?tail=` | Tail the model's vLLM log. | agent platform `/api/v1/models/{name}/logs` |
| GET | `/api/admin/services` | Service list with ports and pid liveness. | — |
| POST | `/api/admin/services/{name}/{start\|stop\|restart}` | Start, stop or restart one service, serialized per service, 30 s timeout. `harness_gateway` (this console) is refused with `409`. | `platform.sh service <name> <action>` |
