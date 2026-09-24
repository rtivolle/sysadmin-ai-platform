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

This event is also how token settlement becomes traceable to an identity.

### 3.8 Model manager (`model_manager/router.py`)

| Trigger | `tool_name`/`action` | Fields |
|---|---|---|
| Register a HuggingFace model | `model_register` | `parameters.name`, `hf_repo`, `revision` |
| Start a download | `model_download` | `parameters.name` |
| Start a vLLM server | `model_start` | `parameters.name` |
| Stop a vLLM server | `model_stop` | `parameters.name` |
| Delete a model | `model_delete` | `parameters.name`, `delete_files` |

The reviewer is the authenticated `sysadmin-admin`. These are admin-only paths;
the download/start failure branches raise before the audit call, so they are not
yet events (a follow-up consistent with the gaps below).

## 4. Open gaps (pinned by sentinel tests)

The following paths are **not** audited today. Each has a "gap sentinel" test
that asserts the absence, so the census cannot drift silently. When a gap is
closed, the sentinel fails: convert it to a positive assertion and update this
section.

| Gap | Path | Missing event |
|---|---|---|
| G1 | ReAct runtime turns (`agent_runtime/react_loop.py`) | No runtime-level completion event; LLM-gateway rejections seen by the runtime are unaudited. Completions are only audited downstream by the LiteLLM handler (3.7). Cancellation is G6. |
| G3 | LiteLLM failures (`QuotaLoggingHandler`) | `async_log_failure_event` is not implemented, so upstream errors and rejected streams emit no audit event. |
| G4 | Quota denials (`auth_gateway/quota_manager.py`) | Concurrency/RPM/daily-budget rejections return `429`/`QuotaExceededException` without an audit event, at ForwardAuth and LiteLLM admission. |
| G5 | Auth gateway (`auth_gateway/server.py`) | ForwardAuth `401`s, login success/failure and logout emit no audit event. |
| G6 | Cancellation (`agent_runtime/cancellation.py`, `agent_runtime/router.py`) | `/agent/cancel`, SSE client disconnects and lease-loss cancellations only reach the server log. |

These gaps are tracked as part of PR-B1 and are not production-acceptable for a
final audit-completeness claim; see
[`../plans/PRODUCTION_READINESS.md`](../plans/PRODUCTION_READINESS.md).

## 5. Maintaining the census

- `test_every_registered_tool_audits_success_and_failure` enumerates
  `AVAILABLE_TOOLS` and fails if a registered tool has no success/failure
  scenario, so adding a tool without updating the census fails the suite.
- Add a row to §3 and a positive test whenever a new
  `log_audit_event`/`AuditSink.emit` call site is introduced.
- Do not delete a gap sentinel to make a change pass; close the gap first.
- Keep the Python and JS writers in lockstep — a divergent field breaks the
  VictoriaLogs stream fields and the harness parity suite.

## 6. Honest status

This census is complete for the paths it enumerates and incomplete by design
for §4. It was derived by code inspection and is pinned by tests that run
without live services; it is **not** evidence that a production VictoriaLogs
instance received every event (query-completeness against a live store remains
open — see [`TEST_READY.md`](TEST_READY.md)). No claim here is a substitute for
the event-by-event live audit verification.
