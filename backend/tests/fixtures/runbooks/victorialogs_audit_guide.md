# VictoriaLogs Forensic Audit Investigation Runbook

## 1. Ingestion Endpoint
Audit records are streamed via `POST /insert/jsonline?_stream_fields=service,user_id&_time_field=timestamp`.
Default retention period is configured to 90 days (`-retentionPeriod=90d`).

## 2. Forensic Audit Queries
Historical actions are queried using LogsQL on port 9428 (`/select/logsql/query`):
- To query all actions performed by user 'sysadmin-02' that required human approval and had a non-zero exit code:
  `service:dsh-agent AND user_id:sysadmin-02 AND human_approved:true AND exit_code:!0`
- Query syntax explanation:
  * `service:dsh-agent`: restricts to sysadmin agent service stream.
  * `user_id:sysadmin-02`: isolates exact operator account.
  * `human_approved:true`: filters actions that went through approval gate.
  * `exit_code:!0`: pinpoints failed commands.

## 3. Outbox Replay
If VictoriaLogs was temporarily unreachable, outbox replay worker flushes `outbox.jsonl`.
