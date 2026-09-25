# Alert runbook: `outbox_backlog`

## Meaning

The audit outbox (`backend/data/victorialogs/outbox.jsonl`) holds more than 100
pending events for longer than 5 minutes. Audit events are written to the outbox
when VictoriaLogs is unreachable, and the replay worker drains them on recovery.

## Impact

The audit trail is buffered, not lost (the outbox is durable and fsync'd). If
the backlog keeps growing it means VictoriaLogs is still down or the replay
worker is stalled, and audit coverage is degraded.

## Diagnosis

```bash
./platform.sh status                          # is audit_outbox running? is victorialogs up?
./platform.sh logs audit_outbox               # replay worker errors
./platform.sh logs victorialogs               # ingest errors
wc -l backend/data/victorialogs/outbox.jsonl  # current backlog size
tail -1 backend/data/victorialogs/outbox.jsonl
curl -s http://127.0.0.1:9428/health          # VictoriaLogs reachable?
```

## Remediation

```bash
./platform.sh restart                         # bring victorialogs + audit_outbox back
```

The worker replays up to 1000 events per flush and retains anything it cannot
deliver. Once VictoriaLogs answers, the backlog drains automatically.

## Escalation

Escalate to **owner** (currently `owner-pending`) if the backlog is still
growing 15 minutes after VictoriaLogs is confirmed reachable, or if events are
being quarantined to `outbox_corrupted.jsonl` (a poison-pill record).
