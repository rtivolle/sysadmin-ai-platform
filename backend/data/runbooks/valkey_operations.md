# Valkey Cache and Quota Store Runbook

## 1. Health Inspection
Check connectivity via `valkey-cli ping`.

## 2. Memory Saturation & Eviction
When Valkey reports `OOM command not allowed when used memory > 'maxmemory'`:
1. Inspect memory distribution:
   `valkey-cli INFO memory`
   `valkey-cli MEMORY USAGE <key>`
2. For token counters and ephemeral sessions, set eviction policy in `valkey.conf`:
   `maxmemory-policy volatile-lru`
3. Notice: Never execute `FLUSHALL` in production as it destroys active rate limiting and session keys.

## 3. Persistence Configuration
RDB snapshots are scheduled every 900 seconds.
