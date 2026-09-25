# Backend services

The backend is a set of FastAPI applications plus supporting modules under
`backend/services/`. `backend/platform.sh` starts eight of them plus an audit
replay worker. This document is a reference for each service and its state.

| Service | Module | Default bind | Started by `platform.sh` |
|---|---|---|---|
| auth gateway | `services/auth_gateway/server.py` | `127.0.0.1:3081` | yes |
| agent tools / runtime | `services/agent_tools/server.py` | `127.0.0.1:3080` | yes |
| inference engine | `services/inference_engine/server.py` | `127.0.0.1:8000` | yes |
| LiteLLM proxy | external package (`litellm`) | `127.0.0.1:4000` | yes |
| VictoriaLogs | native binary `victoria-logs-prod` | `127.0.0.1:9428` | yes |
| SeaweedFS | native binary `weed` | `8333/9333/8888/8085` | yes |
| Valkey | native binary `valkey-server` | `127.0.0.1:6379` | yes |
| audit outbox worker | `services/agent_tools/audit.py --worker` | n/a | yes |
| Traefik | native binary `traefik` | `8080/8443/8081` | yes |

---

## Agent runtime

**Module:** `backend/services/agent_runtime/`

The runtime is a server-side ReAct loop exposed through the agent platform. It is
mounted at both `/api/v1/agent` and `/agent`.

### Files

| File | Responsibility |
|---|---|
| `models.py` | Pydantic request/response and session models. Forbids extra fields; validates that a client-supplied workspace stays under a `workspaces` directory. |
| `router.py` | FastAPI routes, authentication, concurrency-lease acquisition/renewal, SSE streaming and cancellation. |
| `react_loop.py` | The reasoning loop, system prompt, citation extraction and LiteLLM calls. |
| `parser.py` | Lenient parser for `Thought/Action/Action Input`, JSON action blocks and `<tool_call>` tags, with JSON repair heuristics. |
| `tool_registry.py` | Tool catalogue and dispatch; applies safety gating and writes audit records. |
| `session_store.py` | Valkey-backed sessions with a process-local fallback for development runs. |
| `workspace.py` | Server-owned, per-user `0700` workspace creation; rejects symlinks and path-like user IDs. |
| `cancellation.py` | In-flight request registry; only the owning identity may cancel. |

### Reasoning loop

1. Build a system prompt containing the authenticated user, the assigned
   workspace, the tool catalogue and the strict ReAct protocol.
2. Replay the session history (sliding window: first message + last ten).
3. For each step up to `max_steps` (default 5): call the model through
   LiteLLM, parse the output, and either finish with a Final Answer, execute one
   tool, or inject a recovery prompt after a parse error.
4. Tool results become an `Observation` fed back to the model.
5. Persist the session and return the answer with structured citations.

The model is called with the authenticated user's bearer key, so LiteLLM charges
quota to the correct identity. A gateway failure raises an error; the runtime
**never** falls back to calling inference directly.

### Citations

Each tool result yields a `CitationRecord` when relevant:

- `doc_runbook_reader` → source, section, line range, content hash.
- `search_log_stream` → source, pattern, min/max matching line, output hash.
- `config_lint_and_diff` → target file and proposed/original hash.

### Concurrency and cancellation

`router.py` acquires a quota lease before registering the request. While a
request runs, `_maintain_quota_lease` renews the lease every 30 s; if ownership
is lost (or renewal fails for ~110 s) the active task is cancelled. Cancellation
is identity-checked: a user may only cancel their own `request_id` or session.

---

## Agent tools

**Module:** `backend/services/agent_tools/`

The tool implementations and their HTTP surface. See [tools.md](tools.md) for the
tool reference and [security.md](security.md#path-confinement-backend-tools) for path rules.

- `tools.py` — `search_log_stream`, `config_lint_and_diff`,
  `doc_runbook_reader`, `execute_sandboxed_command`, plus path confinement.
- `approval_gate.py` — thin façade over the approval package used by tools.
- `audit.py` — VictoriaLogs ingestion, durable outbox, replay worker and the
  `log_audit_event` schema.
- `server.py` — FastAPI app exposing `/health`, `/api/tools/*` and
  `/api/approvals/*`, and mounting the agent-runtime and target-adapter routers.

Only authenticated callers reach the tool endpoints. The server re-derives the
user's workspace with `ensure_workspace`; it ignores any client-supplied path.

---

## Auth gateway

**Module:** `backend/services/auth_gateway/server.py`

A FastAPI service that acts as Traefik's ForwardAuth provider and as the login
endpoint.

- **Authentication** — accepts a bearer token found in `backend/config/keys/*.key`
  (or the `LITELLM_MASTER_KEY`), or a `session_id` cookie whose value maps to a
  user in Valkey. Forwarded identity headers are ignored.
- **Login** — verifies PBKDF2-SHA256 (600,000 iterations) credentials from
  `login-credentials.json` with constant-time comparison, then stores a
  256-bit session id in Valkey for 24 h.
- **ForwardAuth** — returns `401` without credentials, `429` when the daily
  token budget is exhausted, `503` when quota state is unavailable, otherwise
  `200` with authoritative `X-Forwarded-User`, `X-Forwarded-Role`,
  `X-User-Workspace` (and `X-Priority`/`X-Incident-ID` during P1).
- **Roles** — `admin` for `sysadmin-admin`, `p1-operator` for an elevated
  on-call user, otherwise `sysadmin`.
- **P1** — `/api/v1/auth/p1/elevate|status|revoke` manage time-bounded emergency
  elevation (see below).

Valid users are `sysadmin-01` … `sysadmin-10` plus `emergency-p1-oncall`.

---

## Quota manager

**Module:** `backend/services/auth_gateway/quota_manager.py`

A Valkey-backed manager used both by the auth gateway (pre-admission) and LiteLLM
custom auth (per completion). Enforcement uses atomic Lua scripts.

| Limit | Standard | P1 elevated |
|---|---|---|
| In-flight calls per user | 2 | 6 |
| Cluster slots (when enforced) | 8 | 10 |
| Requests / minute (rolling 60 s) | 60 | 200 |
| Tokens / minute | 150,000 | 500,000 |
| Tokens / day | 2,000,000 | 10,000,000 |

These values are defaults. A per-user override is stored at
`quota:limits:<user_id>` and read on every admission, so a change takes effect
for new leases, RPM windows and daily reservations without a restart; running
requests and already-counted usage are unaffected. `get_limits`/`set_limits`
validate an override against `LIMIT_BOUNDS` — concurrency 1–10, rpm 1–10000,
tpm 1–10,000,000, daily tokens 1–1,000,000,000 — and reject unknown fields,
non-integers and out-of-range values. An empty object restores the defaults.
An override applies during P1 elevation too. The admin API is
`GET|POST /api/v1/admin/quotas` (see
[http-api.md](http-api.md#get-apiv1adminquotas)); changes are audited.

Additional behaviour:

- **Leases** carry an owner, expiry and id; they are acquired atomically,
  renewed while a request runs and released by id. Stale entries are pruned.
- **Daily accounting** uses an explicit timezone (`QUOTA_TIMEZONE`, default
  `UTC`) so rollover is well defined. `reserve_daily_token_budget` reserves an
  estimate before dispatch and `settle_daily_token_reservation` replaces it with
  actual usage exactly once, attributed to the admission day.
- **Daily reservations** have a `RESERVATION_TTL_SECONDS` of eight days to
  survive a restart.
- **Fail closed**: when `VALKEY_URL` is configured (platform runs) and the
  shared store is unreachable, calls raise `ConnectionError`, surfaced as
  `503`. A stored override that fails validation (corrupt or out-of-range) is
  treated the same way, so a bad policy entry cannot silently fall back to
  permissive defaults. Process-local fallback is only for development runs
  without `VALKEY_URL`.

---

## Approval gate

**Package:** `backend/services/approval_gate/`

A transactional state machine shared by tool commands and the target adapter.

```text
proposed -> pending -> approved | rejected | expired
                          |
                          v
                      executing -> succeeded | failed
                                 (consumed = legacy marker)
```

- **Filter** (`filter.py`) classifies a command as `BLOCKED`,
  `ALLOW` or `APPROVAL_REQUIRED` (see [tools.md](tools.md#command-policy) and
  the identical JS port in [harness-integration.md](harness-integration.md)).
- **Binding** (`gate.py`): every approval records user, session, workspace,
  target, action, command, normalised command, content hash and base hash, with
  a 300 s expiry.
- **Store** (`store.py`): Valkey with Lua compare-and-swap for decide, claim
  and complete. Claiming requires status `approved` and matching bindings;
  concurrent execution, replay and post-consumption reuse are rejected.
- **Authority**: only an authenticated `admin` may decide, and the reviewer must
  differ from the requester.
- **Fail closed**: when `VALKEY_URL` is set, an unavailable store raises
  `ConnectionError`; the in-memory store is a development-only fallback.

---

## Target adapter

**Package:** `backend/services/target_adapter/`

A deliberately narrow mutation surface: four actions against nine services and a
small set of configuration roots.

| Action | Effect |
|---|---|
| `service_status` | `systemctl is-active <svc>.service` |
| `service_restart` | `systemctl restart <svc>.service` |
| `service_reload` | `systemctl reload <svc>.service` |
| `config_deploy` | staged atomic configuration replacement with backup/rollback |

Whitelisted services: `nginx`, `traefik`, `valkey`, `victorialogs`,
`seaweedfs`, `postgresql`, `dsh-agent`, `dsh-sysadmin`, `litellm`
(plus aliases `valkey-server`, `victoria-logs`, `weed`).

Allowed configuration roots include `/etc/nginx`, `/etc/traefik`,
`/etc/systemd/system`, `backend/config` and the fixture directories; sensitive
paths such as `/etc/shadow`, `/proc`, `/sys`, `/boot` and `/root` are
rejected. Staged content must be a regular file directly inside the caller's
workspace, no larger than 1 MiB.

The deployer runs a ten-phase pipeline: destination validation → SHA-256 tamper
check → syntax validation → out-of-band conflict detection → same-directory
backup with verification → `os.replace` atomic swap → post-deploy verification →
automated rollback → success/audit retention.

The conflict check hashes exact file bytes and binds the destination's
existence at proposal time (a base hash implies the target existed; an approved
creation implies it did not), so a target created, deleted or changed between
approval and execution is rejected before replacement. Replacement preserves the
destination's Unix mode and UID/GID; new configurations are created `0600`.
Proposals, approval decisions and executions — including failed executions and
denied claims — each emit an audit event (see
[status/AUDIT_CENSUS.md](status/AUDIT_CENSUS.md)).

`TARGET_ADAPTER_SIMULATION=1` makes `ServiceManager` return simulated results
without invoking `systemctl`. See [security.md](security.md#6-target-adapter) for
the qualifications this component still needs.

---

## Model manager

**Package:** `backend/services/model_manager/`

Admin-only local model lifecycle: register a HuggingFace repo, download its
snapshot, run a local vLLM server, and publish the model through the inference
engine and LiteLLM.

| File | Responsibility |
|---|---|
| `registry.py` | Validated JSON registry (`backend/data/models/registry.json`, 0600, atomic writes); name/repo/revision/engine validation; bounded dtype/KV-cache choices; path confinement. |
| `downloader.py` | `huggingface_hub.snapshot_download` with HF token, disk preflight, resume via the Hub cache, background jobs. |
| `vllm_server.py` | Per-model `vllm serve` supervision: port allocation, readiness (`/health` + child liveness), SIGTERM/SIGKILL shutdown, log tail. Maps registry loading parameters to CLI flags. |
| `llamacpp_server.py` | Per-model llama.cpp supervision for GGUF files: identity-checked liveness/stop, path confinement, loading parameters to CLI flags. |
| `litellm_sync.py` | Rewrites the managed `model_list` block in `backend/config/litellm/config.yaml` and restarts LiteLLM. |
| `router.py` | Admin-only `/api/v1/models*` endpoints incl. `PATCH` loading-parameter updates; audit events. |

State machine: `registered → downloading → downloaded → starting → running →
stopped`, with `error` carrying `last_error`. The vLLM process, its port and the
download job are recorded in the registry; the model becomes selectable only
when `running`. Admin-selectable loading parameters per engine are listed in
[model-management.md](model-management.md).

---

## Inference engine

**Module:** `backend/services/inference_engine/server.py`

An OpenAI-compatible facade on `:8000`. It always answers `/v1/models` with the
`fast-model` and `heavy-model` aliases plus any locally running models from the
model registry.

- If the requested `model` is a running local model, the request is routed to
  that model's own vLLM port; a registered-but-stopped model returns `503`
  rather than a simulated answer.
- Else if `UPSTREAM_VLLM_URL` is set, requests are forwarded to
  `<url>/v1/chat/completions` (streaming and non-streaming), with the original
  headers minus `host` and `content-length`.
- Otherwise it returns deterministic simulated completions that emit valid ReAct
  traces for common prompts (search, runbook, diff, restart). This is a
  development and test aid, **not** a model.

---

## Resilience

**Package:** `backend/services/resilience/`

- `backup_manager.py` — snapshots Valkey, VictoriaLogs, SeaweedFS and
  `config/keys`, computes per-component and aggregate SHA-256 hashes, writes a
  `manifest.json`, and produces a `.tar.gz` plus a final manifest.
- `restore_manager.py` — unpacks safely (rejects absolute paths, `..` and
  non-file/dir members), verifies the mandatory components and their hashes, then
  restores in a fixed seven-stage order.
- `dr_drill.py` — an end-to-end drill that takes a backup, validates RPO < 24 h,
  restores to a clean staging directory, validates RTO < 4 h, checks the restore
  sequence and performs post-restore health checks.

See [backup-restore.md](backup-restore.md).

---

## Hardware survey

**Module:** `backend/services/hardware_survey.py`

Collects OS, GPU, PCI accelerator/device IDs and kernel drivers, topology, CPU,
RAM, cgroup v2, sandbox-readiness and storage facts. It also reports the loaded
accelerator modules, NVIDIA/AMD driver and toolkit versions, Python and ML
package versions, and capacity plus recursively summed sizes of direct model
store subdirectories. It derives an inference recommendation (TP layout,
context length, local vs remote mode) from detected GPU count/VRAM.
`install.sh --survey` runs it; `platform.sh survey` and `platform.sh dashboard`
expose it through the TUI. Results are exported to
`backend/data/hardware_inventory.{json,md}`; admins can also read the current
survey from `GET /api/v1/survey` on the agent platform.
