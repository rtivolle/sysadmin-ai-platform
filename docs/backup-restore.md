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
| `config_keys` | `config/keys`; component roots restore `0700` and direct secret/state files `0600` (nested files keep their source modes, shielded by the `0700` parents). |

Each component gets an aggregate SHA-256 over its files (sorted deterministically
by path). The archive itself is hashed and recorded in the final manifest.

**In a three-machine split** ([multi-host.md](multi-host.md)) each machine backs
up its own scope: D's `config_keys` holds `valkey-password.key` only (the state
tier has no LiteLLM key by design), while W and I each back up the key set they
hold — a per-machine operator step, not something one backup on one host
captures. `update.sh`'s post-update key check is role-aware for the same reason:
on a `data` host it requires `valkey-password.key` and does not demand
`master.key`.

**Not yet a component:** the PostgreSQL control store
([ADR-0014](decisions/ADR-0014-postgresql-control-store.md)) is not in the
package's component list, so a backup taken today protects the keys and the
durable token ledger only if the operator dumps the cluster separately:
`backend/config/postgres/postgres.sh backup --out <dir>/control-store.dump`
(restore with `postgres.sh restore --from <dump> --clean`). Adding a fifth
component to `BackupManager`/`RestoreManager` — and to the restore drill's
expectations — is outstanding work, not a claim.

Out of scope: user workspace contents (`data/workspaces`) are **not** backed up —
restore recreates an empty `0700` workspaces directory. Treat workspace data as
ephemeral, or add it to the backup scope before relying on it. Runbooks
(`data/runbooks`) are Git-tracked and restored from the repository, not from
backups.

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
`restore_sequence` and `components_restored`. Stages 1 and 5–7 currently perform
no service bring-up beyond recreating the workspaces directory, so
`restore_duration_seconds` covers the file-copy and verification phase only —
not service start or readiness time.

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

## 4. Audit anchor (tamper-evident integrity)

`backend/services/resilience/audit_anchor.py` seals fixed time windows of the
`service:dsh-agent` VictoriaLogs stream into a hash-chained JSONL ledger meant
to live on independently controlled storage:

```text
batch_hash = sha256(prev_hash || window || count || sha256(canonical events))
```

* **`seal`** queries VictoriaLogs (`VICTORIALOGS_URL`, default
  `http://127.0.0.1:9428`, `/select/logsql/query`) for `service:="dsh-agent"`
  events in fixed windows (default 1 h). Only windows that are fully in the past
  plus a grace delay (default 15 min, for outbox lag) are sealed. Events are
  canonicalized (keys sorted, VictoriaLogs-internal `_stream`/`_stream_id`/
  `_msg`/`_time` dropped), deduplicated by `event_id`, and sorted by `event_id`. Each
  batch appends `{index, window_start, window_end, count, events_sha256,
  prev_hash, hash}` to `AUDIT_ANCHOR_DIR/audit_anchor_ledger.jsonl` (`0600`,
  `O_APPEND`). It is idempotent and fail-closed: an unreachable store leaves the
  ledger untouched and exits non-zero. By default the anchor directory must live
  outside the repository (independently controlled storage); `--allow-local`
  exists only for tests.
* **`verify`** re-queries every sealed window and recomputes the chain, naming
  the exact failing batch and reason (event added / removed / modified, chain
  broken, ledger truncated or edited). `--ledger-only` skips re-querying the
  store; `--expect-tip`/`--expect-min-index` detect tail truncation against an
  externally recorded tip hash.

Run it:

```bash
backend/.venv/bin/python3 -m backend.services.resilience.audit_anchor seal \
    --anchor-dir /mnt/worm-anchor
backend/.venv/bin/python3 -m backend.services.resilience.audit_anchor verify \
    --anchor-dir /mnt/worm-anchor
```

**Honest property: the anchor is tamper-EVIDENT, not immutable.** Any interior
edit breaks the chain, but an attacker who can rewrite the *whole* ledger *and*
the store can recompute a self-consistent one. Immutability depends entirely on
the anchor storage policy: put `AUDIT_ANCHOR_DIR` on a WORM/append-only
filesystem with separate credentials (`chattr +a` plus a dedicated host account
is the cheap local approximation), and record the tip hash printed by `verify`
in independent monitoring.

## 5. Off-host backup copies

After `create_backup` produces a local archive, it can be pushed to
independently controlled storage (`OFFHOST_BACKUP_DEST`):

* **Local mount** — `OFFHOST_BACKUP_DEST=/mnt/worm-backups` (the required form
  for production). The archive, manifest and a `<archive>.sha256` sidecar are
  copied and then **re-read from the destination and re-hashed** to verify the
  copy.
* **`ssh://`** — `OFFHOST_BACKUP_DEST=ssh://user@host:/path` (optional) copies
  via `rsync -a` and verifies by pulling the archive back and re-hashing it.

`create_backup(offhost_dest=..., offhost_retention_days=...)` records the copy
in the manifest and prunes archives older than the retention (default 90 days —
a configurable placeholder **pending the data owner's retention decision**).
`RestoreManager.restore_from_offhost(offhost_dest, backup_id)` pulls the archive
from off-host storage, verifies its SHA-256 against the sidecar, and only then
restores. Every copy/checksum/prune failure raises `OffhostBackupError` — there
is no silent, best-effort path.

## 6. Honest limitations

- **RPO/RTO**: the file-copy restore phase is measured (2026-09-24,
  `backend/tests/qualification/restore_drill.py --real`): 2.4 MB / 823 paths of
  real host state backed up in 0.335 s and restored to clean staging in 0.249 s
  with byte-for-byte verification; linear extrapolation puts a ~2.2 GiB data set
  at ≈250 s. Service bring-up/readiness is not included in that figure, so an
  end-to-end RTO under four hours is supported, not fully measured. Production-
  sized data and off-host copies remain operator responsibilities.
- **Single point of failure.** One host; backups are not high availability.
- **Tamper-evidence, not immutability.** The audit anchor (§4) and off-host
  SHA-256 sidecars (§5) detect tampering; they do not prevent it. Immutability
  requires WORM/append-only anchor storage with separate credentials, and
  off-host storage the platform host cannot write to.
- **Retention** is a VictoriaLogs setting (90 days) and a configurable off-host
  placeholder (90 days), not a legal retention policy. The data owner must
  approve retention.
- **Off-host copies** are only verified for integrity, not for restorability
  *until* a `restore_from_offhost` is actually run; rehearse it on a disposable
  host. `ssh://` retention pruning is deliberately left to the operator rather
  than guessed at.
- **Post-restore service bring-up** from restored state is still outstanding:
  the 2026-09-24 drill verified file-level restore, integrity and permission
  contracts against real host state, not service start from restored state. The
  live Valkey `BGSAVE` and VictoriaLogs `/snapshot/create` paths were not
  exercised in that run (credentials / running API version); the documented
  filesystem-copy fallbacks were used and verified instead.

Recommended operator practice: run backups on a schedule, copy archives to a
separate failure domain, record `archive_sha256`, and rehearse restore on a
disposable host.
