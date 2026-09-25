# Alert runbook: `backup_age`

## Meaning

The newest backup in `backend/data/backups/` is older than 26 hours
(`observability_backup_newest_age_seconds > 93600`). This implies the scheduled
backup has not completed on time — RPO is drifting past the documented 24 h
target. Note this rule fires only when at least one backup exists; a separate
`observability_backup_present == 0` condition (no backup at all) is the more
severe case and appears in the same metric set.

## Impact

The last good restore point is stale. On a host loss you could lose up to the
gap between the last backup and the failure — more than 24 h of Valkey,
VictoriaLogs, SeaweedFS and config-key state.

## Diagnosis

```bash
ls -lat backend/data/backups/ | head          # newest archive + manifest
./platform.sh status                           # is the platform healthy enough to back up?
```

## Remediation

```bash
backend/.venv/bin/python3 -c \
  "from backend.services.resilience.backup_manager import get_backup_manager; print(get_backup_manager().create_backup()['backup_id'])"
```

Verify the archive and manifest landed in `backend/data/backups/`, copy the
archive to a separate failure domain, and record `archive_sha256`. Re-run a
restore drill periodically (`make test` tier 4 recovery suite).

## Escalation

Escalate to **owner** (currently `owner-pending`) if a fresh backup cannot be
produced (disk full, Valkey/VictoriaLogs snapshot failures) or if backup
scheduling itself is missing — treat stale/missing backups as a data-loss risk
until a fresh archive is confirmed off-host.
