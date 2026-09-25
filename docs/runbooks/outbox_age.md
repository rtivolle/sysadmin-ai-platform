# Alert runbook: `outbox_age`

## Meaning

The oldest pending audit event in `backend/data/victorialogs/outbox.jsonl` is
more than 1 hour old (3600 s). This means audit delivery has been blocked for
over an hour, regardless of the current backlog size.

## Impact

Audit records older than an hour have not reached VictoriaLogs. Search/audit
queries against the trail are incomplete for that window, and retention (90 days)
is measured from delivery, not from event time.

## Diagnosis

```bash
./platform.sh status
./platform.sh logs audit_outbox
./platform.sh logs victorialogs
tail -1 backend/data/victorialogs/outbox.jsonl   # inspect the oldest survivor's timestamp
curl -s http://127.0.0.1:9428/health
```

## Remediation

```bash
./platform.sh restart
```

Confirm the replay worker resumes delivery and the oldest-age metric falls.

## Escalation

Escalate to **owner** (currently `owner-pending`) if events remain undelivered
for more than 2 hours despite VictoriaLogs being healthy, or if you need to
determine the exact audit gap before reporting to the data owner.
