# Backup, restore and disaster recovery

The resilience package lives in `backend/services/resilience/`.

## 1. What is protected

`BackupManager.create_backup` snapshots four components and packages them into a
`tar.gz` with a manifest:

| Component | Method |
|---|---|
| `valkey` | Live `BGSAVE` (waited on, up to ~5 s) or a filesystem copy of `data/valkey`. |
| `victorialogs` | `/snapshot/create` API if available, otherwise a copy of `partitions` plus `outbox.jsonl`. |
| `seaweedfs` | Filesystem copy of `data/seaweedfs` (master `m9333`, filer `filerldb2`, volume data). |
| `config_keys` | `config/keys` with permissions preserved (`0600` files / `0700` directories). |

Each component gets an aggregate SHA-256 over its files (sorted deterministically
by path). The archive itself is hashed and recorded in the final manifest.

Backups land in `backend/data/backups/`:

```text
backend/data/backups/<backup_id>.tar.gz
backend/data/backups/<backup_id>.manifest.json
```

The manifest records `backup_id`, timestamps, platform version, per-component
metadata, `archive_sha256`, `archive_file` and `total_bytes`.

## 2. Restore

`RestoreManager.restore_from_archive` performs a clean-staging restore:

1. **Safe unpack** — rejects absolute paths, `..` components and non-file/dir
   members, and requires exactly one root directory.
2. **Manifest validation** — requires exactly the four components
   (`valkey`, `seaweedfs`, `victorialogs`, `config_keys`), each with a
   64-character aggregate hash; recomputes and compares every hash.
3. **Seven-stage bring-up sequence:**

```text
1. cgroups      (sandbox layout + 0700 workspaces)
2. valkey
3. seaweedfs
4. victorialogs
5. inference
6. litellm      (including config keys)
7. traefik
```

The result reports `backup_id`, `restore_duration_seconds`,
`restore_sequence` and `components_restored`.

## 3. Disaster-recovery drill

`DisasterRecoveryDrill.run_drill` automates an end-to-end check:

1. Create a backup.
2. Verify **RPO** — snapshot age < 24 hours (86,400 s).
3. Restore into a clean staging directory.
4. Verify **RTO** — restore duration < 4 hours (14,400 s).
5. Verify the restore sequence equals the mandatory order.
6. Post-restore health checks for Valkey (`dump.rdb` signature), VictoriaLogs
   (partitions or outbox present), SeaweedFS (data present) and config keys
   (count + `master.key`).

It returns `success`, `rpo{}`, `rto{}`, `sequence_verification{}`,
`health_checks{}`, `drill_total_seconds` and `errors[]`.

### Running it

```bash
backend/.venv/bin/python3 -m pytest backend/tests/tier4_recovery -q
```

## 4. Honest limitations

- **RPO/RTO are targets, not measured guarantees.** The drill checks fixture
  logic and local restore duration; it does not establish an RTO under four
  hours on production-sized data.
- **Single point of failure.** One host; backups are not high availability.
- **No independent integrity archive.** The design calls for hashes anchored to
  separately controlled storage; that is not implemented.
- **Retention** is a VictoriaLogs setting (90 days), not a legal retention
  policy. The data owner must approve retention.
- **Off-host copies** are the operator's responsibility; the built-in backup
  writes to a local directory.
- **Clean-staging validation** against real services is still outstanding.

Recommended operator practice: run backups on a schedule, copy archives to a
separate failure domain, record `archive_sha256`, and rehearse restore on a
disposable host.
