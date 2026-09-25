# Audit completeness census

Date: 24 September 2026. Status: active census, pinned by
`backend/tests/tier1_unit/test_audit_census.py`.

This document enumerates every user- or agent-triggerable action/outcome path,
states which audit event it emits, and records the paths that are **not** yet
audited. It is the human-readable half of PR-B1
([`../plans/PRODUCTION_READINESS.md`](../plans/PRODUCTION_READINESS.md#pr-b1--event-by-event-audit-completeness-census-in-flight));
the test file is the machine half. When the two disagree, the test wins and
this file is wrong.

The census is deliberately narrower than "everything the platform logs". A path
is *audited* only when it calls `log_audit_event` (Python) or
`AuditSink.emit` (JavaScript) with a schema-conformant event that reaches
VictoriaLogs or the durable outbox. Server `print`/`logger` output is not audit.

## 1. Canonical event schema

Both writers emit the same field set. The Python writer is
`backend/services/agent_tools/audit.py::log_audit_event`; the JavaScript writer
is `packages/harness-integration/dsh-plugin-sysadmin/lib/audit.js::buildAuditEvent`.

| Field | Notes |
|---|---|
| `event_id` | UUIDv4; the deduplication key for at-least-once delivery. |
| `timestamp` | UTC, second resolution, `YYYY-MM-DDTHH:MM:SSZ`. |
| `service` | Fixed `dsh-agent` in both writers. |
| `user_id`, `session_id` | Authenticated identity (never a client header). |
| `action`, `tool_name` | `action` defaults to `tool_name` when omitted. |
| `parameters` | Bounded: `proposed_content` is truncated past 500 bytes. |
| `command` | The exact command where one applies, else `""`. |
| `human_approved` | `true` only after an approval was actually consumed. |
| `approval_id` | Binding token where one applies, else `null`. |
| `exit_code`, `duration_ms` | Integer outcome code and wall time. |
| `priority`, `incident_id` | Python derives `P1-CRITICAL` from live elevation when not supplied. |
| `prompt_tokens`, `completion_tokens` | Aliased as `tokens_prompt`, `tokens_completion`. |
| `extra` | Optional, path-specific context (reasons, flags). |

`test_writer_spools_canonical_schema` and `test_writer_minimal_call_has_full_schema`
assert the field set; the harness `audit.test.mjs` suite asserts the JS parity.

## 2. Delivery semantics

`log_audit_event` posts to VictoriaLogs first
(`/insert/jsonline?_stream_fields=service,user_id,priority&_time_field=timestamp`)
and, on any HTTP/OS error, appends the event to a `fsync`-ed JSONL outbox
(`backend/data/victorialogs/outbox.jsonl`, mode `0600`). A supervised worker
(`audit.py --worker`) replays the outbox in order. Delivery is **at-least-once**:
a crash between VictoriaLogs acceptance and outbox removal can replay an event,
which is why `event_id` exists. Malformed outbox lines are quarantined so they
cannot halt replay.

## 3. Census — audited paths

### 3.1 Agent runtime dispatcher — `tool_registry.execute_tool_call`

Used by the ReAct loop for every model-issued tool call.

| Trigger | `tool_name`/`action` | `exit_code` | Salient `extra` |
|---|---|---|---|
| Tool success | tool name | tool result code (`0` on success) | — |
| Tool returns a non-zero result (e.g. runbook section not found) | tool name | result code (`1`/`2`) | — |
| Workspace does not match authenticated user | tool name | `126` | `blocked: true`, `reason` |
| Command classified `BLOCKED` | tool name | `126` | `blocked: true`, `reason` |
| `APPROVAL_REQUIRED`, no approval id | tool name | `0` | `approval_required: true`, `approval_id` |
| `APPROVAL_REQUIRED`, invalid/expired/reused approval id | tool name | `126` | `approval_denied: true`, `reason` |
| Unknown tool | requested name | `127` | `unknown_tool: true` |
| Dispatcher raised an exception | tool name | `1` | `error` |

### 3.2 Agent platform HTTP — `POST /api/tools/execute` (`agent_tools/server.py`)

The direct HTTP surface for a single tool call. It mirrors the runtime
dispatcher outcome-for-outcome, so a call cannot be audited through one entry
point and silently missed through the other.

| Trigger | HTTP | `tool_name`/`action` | `exit_code` | Salient `extra` |
|---|---|---|---|---|
| Command classified `BLOCKED` | `403` | tool name | `126` | `blocked: true`, `reason` |
| `APPROVAL_REQUIRED`, no approval id | `202` | tool name | `0` | `approval_required: true`, `approval_id` |
| `APPROVAL_REQUIRED`, invalid/expired/reused approval id | `403` | tool name | `126` | `approval_denied: true`, `reason` |
| Unknown tool | `404` | `unknown` | `127` | `unknown_tool: true` |
| Executed | `200` | tool name | command exit code | — |

### 3.3 Approval decisions

Both decision endpoints record who decided what, for **approved and rejected**
outcomes:

| Path | `tool_name` | `action` | Fields |
|---|---|---|---|
| `POST /api/approvals/decide` (agent platform) | `approval_decision` | `approval_decision` | `human_approved`, `approval_id`, session/command from the record; `extra.requester`, `extra.approval_decision` |
| `POST /api/v1/approvals/decide` (target adapter) | `approval_decision` | `approval_decision` | same, resolved from the adapter approval record |

### 3.4 Target adapter (`target_adapter/`)

| Trigger | `tool_name` | `action` | Outcome |
|---|---|---|---|
| Proposal accepted | `adapter_<action>` | requested action | `exit_code 0`, `extra.approval_required`, `approval_id`, `reason` |
| Service action executed | `adapter_<action>` | action | `human_approved: true`, service exit code, `duration_ms` |
| Config deployment executed | `adapter_config_deploy` | `config_deploy` | `human_approved: true`, exit code, `parameters.backup_path`, `rollback_performed` |
| Approval token not found | `adapter_execute` | `execute` | `exit_code 1`, `extra.error` |
| Claim denied (consumed/expired/mismatched) | `adapter_execute` | record action | `exit_code 1`, `extra.blocked`, `extra.error` |
| Staged file unavailable at execution | `adapter_execute` | `config_deploy` | `exit_code 1`, `extra.error` |
| Unknown action | `adapter_execute` | action | `exit_code 1`, `extra.error` |

### 3.5 Auth gateway administration

| Trigger | `tool_name`/`action` | Fields |
|---|---|---|
| `POST /api/v1/admin/quotas/{user}` | `quota_update` | `parameters.user_id`, `parameters.limits`; only reachable by `sysadmin-admin` |

### 3.6 P1 emergency elevation (`auth_gateway/p1_elevation.py`)

| Trigger | `action` | Fields |
|---|---|---|
| `issue_p1_token` | `p1_elevation_issued` | `priority: P1-CRITICAL`, `incident_id`, `parameters.ttl_seconds`, `parameters.reason` |
| `revoke_p1_elevation` | `p1_elevation_revoked` | `parameters.reason` |

Audit emission here is best-effort and is caught so it cannot abort an
elevation; a failure is logged to stderr, not retried.

### 3.7 LiteLLM completions (`auth_gateway/litellm_auth.py`)

| Trigger | `tool_name`/`action` | Fields |
|---|---|---|
| `QuotaLoggingHandler.async_log_success_event` | `litellm_completion` | resolved `user_id`, `session_id`, `prompt_tokens`/`completion_tokens`, `extra.response_id` |
| `QuotaLoggingHandler.async_log_failure_event` | `litellm_completion_failure` | resolved `user_id`, `session_id`, `extra.error_type`, `extra.response_id`; `exit_code 1` |

This event is also how token settlement becomes traceable to an identity. The
failure handler mirrors the success path: an admitted completion that then
fails still settles its daily-token reservation exactly once (normally with
zero tokens) so a failed request cannot leak reserved capacity or double-settle.

### 3.8 Model manager (`model_manager/router.py`)

The reviewer is the authenticated `sysadmin-admin`. Every rejected or failed
lifecycle branch emits a best-effort event with `exit_code 1` and a
`reason`/`error` in `extra`, so no mutation outcome goes unrecorded. Any
lifecycle endpoint given an unknown model emits its own action with
`reason: unknown_model`.

| Trigger | `tool_name`/`action` | `exit_code` | Salient `extra` |
|---|---|---|---|
| Register a HuggingFace model | `model_register` | `0` | `parameters.name`, `hf_repo`, `revision` |
| Registration rejected (invalid body/fields) | `model_register` | `1` | `reason: invalid_body` / `reason: validation`, `error` |
| Registration rejected (model active) | `model_register` | `1` | `reason: active_operation` |
| Update loading parameters | `model_update` | `0` | `parameters.name`, `fields` (validated loading parameters) |
| Update rejected (invalid body) | `model_update` | `1` | `reason: invalid_body` |
| Update rejected (unknown/immutable fields, no fields) | `model_update` | `1` | `reason: validation`, `error` |
| Update rejected (parameter value invalid) | `model_update` | `1` | `reason: validation`, `error` |
| Update rejected (model active) | `model_update` | `1` | `reason: active_operation` |
| Start a download | `model_download` | `0` | `parameters.name` |
| Download rejected (active operation) | `model_download` | `1` | `reason: active_operation` |
| Downloader refused to start | `model_download` | `1` | `reason: start_failed`, `error` |
| Start a vLLM/llama.cpp server | `model_start` | `0` | `parameters.name` |
| Start rejected (already running) | `model_start` | `1` | `reason: already_running` |
| Start rejected (not downloaded) | `model_start` | `1` | `reason: not_downloaded` |
| Start rejected (active lifecycle operation) | `model_start` | `1` | `reason: active_operation` |
| Start job thread could not spawn | `model_start` | `1` | `reason: spawn_failed` |
| Start job failed asynchronously | `model_start` | `1` | `reason: start_failed`, `error`, `cleanup_not_confirmed` |
| Stop a server | `model_stop` | `0` | `parameters.name` |
| Stop rejected (still starting) | `model_stop` | `1` | `reason: starting` |
| Stop failed (`server.stop`/sync raised) | `model_stop` | `1` | `reason: stop_failed`, `error` |
| Restart rejected (still starting) | `model_restart` | `1` | `reason: starting` |
| Restart stop failed | `model_restart` | `1` | `reason: stop_failed`, `error` |
| Delete a model | `model_delete` | `0` | `parameters.name`, `delete_files` |
| Delete rejected (active operation) | `model_delete` | `1` | `reason: active_operation` |
| Delete rejected (still running) | `model_delete` | `1` | `reason: running` |
| Delete rejected (path escape) | `model_delete` | `1` | `reason: path_escape` |

Rejection/failure emission is best-effort: a failed audit write never changes
the rejection outcome.

### 3.9 Agent runtime turns (`agent_runtime/react_loop.py`)

| Trigger | `tool_name`/`action` | `exit_code` | Salient `extra` |
|---|---|---|---|
| Turn completes (final answer, synthesized, or approval interception) | `agent_turn` | `0` | `model`; `approval_required: true` when the turn ended in a HITL request |
| LLM gateway rejects or errors the turn | `agent_turn` | non-zero | `model`, `error`, `http_status` |

Token counts are not known at this layer (they are settled downstream by the
LiteLLM handler), so `prompt_tokens`/`completion_tokens` stay `0` here.
Emission is best-effort and never changes the turn outcome.

### 3.10 Quota denials (`auth_gateway/quota_manager.py`)

| Denial | `tool_name`/`action` | `exit_code` | Salient `extra` |
|---|---|---|---|
| Concurrency ceiling (user or cluster) | `quota_denied` | non-zero | `limit_type: concurrency`, `stage`, `current`, `limit` |
| RPM rolling window | `quota_denied` | non-zero | `limit_type: rpm`, `stage`, `current`, `limit` |
| Daily budget pre-check (ForwardAuth `/verify`) | `quota_denied` | non-zero | `limit_type: daily_tokens`, `stage: forwardauth`, `current`, `limit` |
| Daily reservation admission (LiteLLM `sysadmin_custom_auth`) | `quota_denied` | non-zero | `limit_type: daily_tokens`, `stage: litellm`, `current`, `limit` |

`stage` records which admission gate surfaced the denial — `forwardauth` for the
ForwardAuth daily-budget pre-check, `litellm` for LiteLLM admission
(concurrency/RPM/daily reservation). Emission is best-effort: a failed audit
write never converts a denial into an admission and never masks the fail-closed
`ConnectionError` (503) path.

### 3.11 Auth gateway (`auth_gateway/server.py`)

| Trigger | `tool_name`/`action` | Fields |
|---|---|---|
| ForwardAuth `401` | `auth_denied` | `user_id: anonymous`, `extra.reason` (`missing_credentials` / `invalid_credentials`); never the presented token |
| Login success | `login_success` | `user_id` (the authenticated user), `session_id` (the issued session) |
| Login failure | `login_failure` | `user_id: anonymous`, `parameters.attempted_user` |
| Logout | `logout` | `user_id` (resolved from the session, else `anonymous`) |

The presented bearer token and password are never written to an event.
Emission is offloaded to a worker thread (`asyncio.to_thread`) so a flood of
`401`s cannot stall the event loop; a failure is logged to stderr and does not
change the response.

### 3.12 Cancellation (`agent_runtime/router.py`)

| Trigger | `tool_name`/`action` | Salient `extra` |
|---|---|---|
| `POST /agent/cancel` | `agent_cancel` | `cause: user`, `request_id` |
| SSE client disconnect | `agent_cancel` | `cause: disconnect`, `request_id` |
| Quota lease ownership lost | `agent_cancel` | `cause: lease_lost`, `request_id` |

Emission is best-effort and never alters the cancellation outcome.

## 4. Open gaps

No open gaps. Every enumerable user- or agent-triggerable path in §3 emits a
schema-conformant event on both success and failure, and the sentinel tests
that previously pinned G1/G3/G4/G5/G6 as unaudited have been replaced with
positive assertions.

## 5. Maintaining the census

- `test_every_registered_tool_audits_success_and_failure` enumerates
  `AVAILABLE_TOOLS` and fails if a registered tool has no success/failure
  scenario, so adding a tool without updating the census fails the suite.
- Add a row to §3 and a positive test whenever a new
  `log_audit_event`/`AuditSink.emit` call site is introduced.
- Keep the Python and JS writers in lockstep — a divergent field breaks the
  VictoriaLogs stream fields and the harness parity suite.

## 6. Honest status

This census is complete for the paths it enumerates. It was derived by code
inspection and is pinned by tests that run without live services; it is **not**
evidence that a production VictoriaLogs instance received every event
(query-completeness against a live store remains open — see
[`TEST_READY.md`](TEST_READY.md)). No claim here is a substitute for the
event-by-event live audit verification.
