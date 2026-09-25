# Alert runbook: `stale_leases`

## Meaning

Expired quota lease entries are still resident in Valkey
(`observability_stale_leases_total > 0` for 5 minutes). A lease lives in the
`quota:leases:cluster` and `quota:leases:user:<id>` sorted sets with its expiry
as the score; a stale entry is one whose score has passed but was never removed.

## Impact

Under normal operation the acquire Lua script prunes expired entries
(`ZREMRANGEBYSCORE … -inf now`), so non-zero stale leases indicate requests
died without releasing their slot (crashed agent, lost cancellation, or a
renewal path that stopped running). Stale entries do **not** permanently consume
capacity — the acquire path prunes them — but they signal a release/renewal
defect and can briefly inflate the concurrency count shown by dashboards.

## Diagnosis

```bash
./platform.sh status                        # is valkey up? are agents running?
./platform.sh logs agent_tools              # crashed requests / cancellation
./platform.sh logs auth_gateway
```

Read the live sets (read-only) with `redis-cli` if available:

```bash
redis-cli ZRANGEBYSCORE quota:leases:cluster -inf +inf WITHSCORES
```

## Remediation

- Restart the affected service (`./platform.sh restart`) so requests acquire
  fresh leases.
- The next successful `acquire_concurrency_slot` prunes expired entries
  automatically; no manual deletion is required.

## Escalation

Escalate to **owner** (currently `owner-pending`) if stale leases recur in
volume, as that indicates a systematic release/renewal failure worth a code
fix in `backend/services/auth_gateway/quota_manager.py`.
