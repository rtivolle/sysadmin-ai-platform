# Alert runbook: `ram_usage`

## Meaning

Host RAM usage is above 90 % (`observability_ram_used_percent > 90` for
5 minutes), as reported by `/proc/meminfo`.

## Impact

Memory pressure causes swapping, OOM kills of platform services, sandbox
failures, and general latency. If a cgroup limit cannot be installed, the
sandbox runner aborts (`126`) and execution fails closed.

## Diagnosis

```bash
free -h                                   # RAM and swap
./platform.sh status                      # per-service RSS
./platform.sh logs <service>              # OOM or swap storms
ps aux --sort=-%mem | head                # top memory consumers
```

## Remediation

- Restart the largest offender with `./platform.sh restart` (or a single service
  via `./platform.sh dashboard`).
- Stop unused local model servers (see `model-management.md`).
- If the host is undersized for the workload, schedule a capacity review.

## Escalation

Escalate to **owner** (currently `owner-pending`) when the platform is swapping
heavily or processes are being OOM-killed repeatedly — this indicates a
capacity or memory-leak problem that needs a fix, not just a restart.
