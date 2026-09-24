# PostgreSQL Operations & Disaster Recovery Runbook

## 1. Architecture Overview
PostgreSQL runs as primary on port 5432 with WAL streaming replication.

## 2. Point-in-Time Recovery
To perform Point-in-Time Recovery (PITR) to a specific target time:
1. Stop the PostgreSQL service: `systemctl stop postgresql`
2. Restore the physical base backup to data directory `/var/lib/postgresql/data`.
3. Configure PITR parameters in `postgresql.conf`:
   - `restore_command = 'cp /mnt/server/archivedir/%f %p'`
   - `recovery_target_time = '2026-09-23 12:00:00 UTC'`
4. Create the required trigger signal file in the data directory:
   `touch /var/lib/postgresql/data/recovery.signal`
5. Start PostgreSQL service to begin WAL replay: `systemctl start postgresql`

## 3. Replication Failover
Promote standby node using `pg_ctl promote`.
