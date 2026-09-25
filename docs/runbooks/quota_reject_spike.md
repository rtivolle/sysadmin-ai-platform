# Alert runbook: `quota_reject_spike`

> **Status: not yet measurable.** This rule is disabled in
> `backend/config/observability/alerts.json` because the auth gateway does not
> currently expose a reject counter on any loopback endpoint. See
> [observability.md](../observability.md#shipped-rules).

## Meaning

(When enabled) a spike in quota rejections — `429` responses from the auth
gateway or LiteLLM custom auth — sustained over the window.

## Impact

Users are being rate-limited or blocked: concurrency ceiling, RPM/TPM window, or
the daily token budget has been hit (possibly legitimately, possibly by a runaway
agent or an abusive client).

## Diagnosis

```bash
./platform.sh logs auth_gateway             # 429 reject reasons
./platform.sh logs litellm                  # per-completion quota decisions
./sysadmin-chat quotas --user sysadmin-admin # admin quota snapshot
```

## Remediation

- Identify the offending user/request pattern from the logs.
- Reset or raise a limit via `POST /api/v1/admin/quotas/{user_id}`, or grant a
  time-bounded P1 elevation for a genuine incident.

## Escalation

Escalate to **owner** (currently `owner-pending`). Enabling this alert first
requires a read-only reject-count metric from the auth gateway or LiteLLM custom
auth; the collector already reserves `observability_quota_rejects_total` for it.
