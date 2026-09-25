# Alert runbook: `disk_usage`

## Meaning

The filesystem hosting `backend/data` is more than 85 % full
(`observability_disk_used_percent > 85` for 5 minutes).

## Impact

A full disk stalls Valkey persistence, VictoriaLogs ingest, SeaweedFS volume
writes and backup archives. Writes can fail or corrupt; the audit outbox may be
unable to fsync.

## Diagnosis

```bash
df -h backend/data                        # filesystem usage
du -sh backend/data/*                     # which subtree is largest
./platform.sh status                      # any service writing abnormally?
```

## Remediation

- Remove or rotate oversized runtime state (old logs under `backend/logs`,
  orphaned model downloads under `backend/data/models`, stale backups under
  `backend/data/backups`).
- Confirm backups are being copied off-host so older archives can be pruned.
- If the filesystem is genuinely full, stop write-heavy services first
  (`./platform.sh stop`) before freeing space, then `./platform.sh start`.

## Escalation

Escalate to **owner** (currently `owner-pending`) if usage stays above 85 % and
no safe deletions are identified — do not delete state you cannot explain.
