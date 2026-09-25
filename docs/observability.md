# Local observability (PR-D1)

Stdlib-only metrics collector and alert evaluator that scrape the platform from
*outside* the running services — no service code is modified, and nothing inside
the platform depends on these tools. They can run from a bare `python3` (no
`httpx`, no `redis`, no third-party runtime imports).

```text
┌──────────────────────────────────────────────────────────────────────┐
│ collector.py                                                          │
│  service health probes ──► loopback /health + TCP + RESP PING         │
│  audit outbox            ──► backend/data/victorialogs/outbox.jsonl   │
│  backup freshness        ──► backend/data/backups/*.manifest.json     │
│  host disk/RAM/load/GPU  ──► shutil.disk_usage, /proc/meminfo,        │
│                              os.getloadavg, nvidia-smi (if present)   │
│  Valkey                  ──► TCP + PING + SCAN/ZRANGEBYSCORE for       │
│                              stale lease counts (read-only)           │
└───────────────┬──────────────────────────────────────────────────────┘
                │ Prometheus text (stdout / --textfile / --serve :9464)
                │ JSON summary → VictoriaLogs stream service:observability
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│ alerts.py                                                             │
│  rules: backend/config/observability/alerts.json                      │
│  state: backend/data/observability/state.json                         │
│  sinks: alerts.log  +  VictoriaLogs event  +  optional command hook   │
└──────────────────────────────────────────────────────────────────────┘
```

## 1. Collector

```bash
# one-shot to stdout
python3 -m backend.services.observability.collector

# repeat N times (default 30 s apart)
python3 -m backend.services.observability.collector --loop 60

# also write a node_exporter-style textfile
python3 -m backend.services.observability.collector --textfile /run/observability.prom

# serve /metrics for a Prometheus scrape
python3 -m backend.services.observability.collector --serve 127.0.0.1:9464
```

Flags: `--loop N` (default 0 = once), `--interval SECONDS` (default 30),
`--textfile PATH` (atomic write), `--serve HOST:PORT`, `--timeout SECONDS`
(default 2, per probe), `--no-victorialogs` (skip the summary send).

The collector is **read-only**: the only file it writes is the optional
`--textfile` destination. It never reads `backend/config/keys/*`; it only
parses `VALKEY_URL` for host/port/password and never logs or emits the password.

### Service catalogue

| Service | Probe | Endpoint |
|---|---|---|
| valkey | RESP | `127.0.0.1:6379` (TCP + `PING`) |
| victorialogs | HTTP | `http://127.0.0.1:9428/health` |
| seaweedfs | TCP | `127.0.0.1:8333` |
| inference | HTTP | `http://127.0.0.1:8000/health` |
| auth_gateway | HTTP | `http://127.0.0.1:3081/health` |
| agent_tools | HTTP | `http://127.0.0.1:<SYSADMIN_AGENT_PORT or 3080>/health` |
| litellm | HTTP | `http://127.0.0.1:4000/health/readiness` |
| traefik | TCP | `127.0.0.1:8080` |
| harness_gateway | HTTP | `http://127.0.0.1:3085/api/gateway/health` |
| audit_outbox | pidfile | process liveness via `backend/run/audit_outbox.pid` |

Ports and endpoints are taken from [services.md](services.md) and
[http-api.md](http-api.md). The `agent_tools` port honours
`SYSADMIN_AGENT_PORT` exactly as `platform.sh` does.

## 2. Metric catalogue

| Metric | Type | Meaning |
|---|---|---|
| `observability_up` | gauge | Collector liveness (always 1). |
| `observability_scrape_duration_seconds` | gauge | Last scrape duration. |
| `observability_service_up{service=…}` | gauge | 1 when the probe succeeds, else 0. |
| `observability_outbox_backlog` | gauge | Pending events in `outbox.jsonl`. |
| `observability_outbox_oldest_age_seconds` | gauge | Age of the oldest pending event. |
| `observability_backup_present` | gauge | 1 when a backup exists. |
| `observability_backup_newest_age_seconds` | gauge | Age of the newest backup (prefers `.manifest.json`). |
| `observability_disk_total_bytes` | gauge | Filesystem total hosting `backend/data`. |
| `observability_disk_used_bytes` | gauge | Filesystem used hosting `backend/data`. |
| `observability_disk_used_percent` | gauge | Filesystem usage percent. |
| `observability_ram_total_bytes` | gauge | Host total RAM. |
| `observability_ram_available_bytes` | gauge | Host available RAM. |
| `observability_ram_used_percent` | gauge | Host RAM usage percent. |
| `observability_load1/5/15` | gauge | Load averages. |
| `observability_gpu_present` | gauge | 1 when `nvidia-smi` reported GPUs. |
| `observability_gpu_memory_total_mb{gpu=…}` | gauge | GPU VRAM total. |
| `observability_gpu_memory_used_mb{gpu=…}` | gauge | GPU VRAM used. |
| `observability_gpu_memory_used_percent{gpu=…}` | gauge | GPU VRAM usage percent. |
| `observability_valkey_up` | gauge | 1 when Valkey TCP/PING succeeds. |
| `observability_stale_leases_total` | gauge | Expired lease entries resident in Valkey. |
| `observability_stale_leases{scope=cluster\|user}` | gauge | Expired leases per scope. |

The stale-lease keys (`quota:leases:cluster`, `quota:leases:user:*`) mirror the
constants in `backend/services/auth_gateway/quota_manager.py`; keep the two in
sync if the quota key scheme changes.

## 3. Alert evaluator

```bash
python3 -m backend.services.observability.alerts            # collect + evaluate once
python3 -m backend.services.observability.alerts --loop 60  # repeat
python3 -m backend.services.observability.alerts --no-victorialogs
```

Rules live in `backend/config/observability/alerts.json`. Each rule has `name`,
`metric`, `op` (`lt|le|gt|ge|eq|ne`), `threshold`, `for_seconds`, `severity`,
`owner` (currently the placeholder `owner-pending`), `runbook` and a `summary`
template. A `labels` filter whose value is `"*"` expands the rule per distinct
label value (used by `service_down` and `gpu_memory`).

Lifecycle: `inactive → pending → firing → resolved`. A rule becomes `pending`
the moment its condition is true, and only `firing` after the condition has held
continuously for `for_seconds`; it returns to `resolved` the moment the
condition clears. State is persisted to `backend/data/observability/state.json`.

Sinks, in order:

1. **Log file** — `backend/data/observability/alerts.log` (JSON lines).
2. **VictoriaLogs event** — best-effort POST under `service:observability`.
3. **Optional command hook** — per-rule `command` string run without a shell,
   with `ALERT_NAME`, `ALERT_STATE`, `ALERT_VALUE`, `ALERT_SEVERITY` and
   `ALERT_LABELS` (JSON) in the environment.

### Shipped rules

| Rule | Condition | `for` | Severity |
|---|---|---|---|
| `service_down` | any service `observability_service_up < 1` | 60 s | critical |
| `outbox_backlog` | backlog `> 100` | 300 s | warning |
| `outbox_age` | oldest event `> 3600 s` | 300 s | warning |
| `backup_age` | newest backup `> 93600 s` (26 h) | 3600 s | critical |
| `disk_usage` | `> 85 %` | 300 s | critical |
| `ram_usage` | `> 90 %` | 300 s | critical |
| `stale_leases` | `> 0` expired leases | 300 s | warning |
| `gpu_memory` | `> 90 %` per GPU (only when a GPU is present) | 300 s | warning |
| `quota_reject_spike` | **disabled** — see note below | — | warning |

`quota_reject_spike` is intentionally disabled: the auth gateway does not
currently expose a reject counter on any loopback endpoint, so the metric is not
measurable from outside. Enabling it requires a read-only reject-count metric
from the auth gateway or LiteLLM custom auth; the collector already reserves the
`observability_quota_rejects_total` name and will emit it once such a source
exists.

## 4. Scheduled collection

The tools are runnable under a systemd **user** timer (no root needed):

```ini
# ~/.config/systemd/user/sysadmin-observability.service
[Unit]
Description=Sysadmin platform observability collector

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -m backend.services.observability.collector --textfile %t/sysadmin-observability.prom
WorkingDirectory=/path/to/platform
```

```ini
# ~/.config/systemd/user/sysadmin-observability.timer
[Unit]
Description=Run the observability collector every minute

[Timer]
OnCalendar=*-*-* *:*:00
Persistent=true

[Install]
WantedBy=timers.target
```

Then `systemctl --user enable --now sysadmin-observability.timer`. Run the alert
evaluator from the same timer (or a second, longer timer) to keep alerts firing.

## 5. Components and license

Both modules are original, standard-library-only Python added for this platform
(no bundled third-party code, no new runtime dependencies). They import `socket`,
`urllib`, `subprocess`, `http.server`, `json`, `os`, `shutil`, `datetime`,
`argparse`, `re`/`pathlib` — all part of the Python standard library. There are
no separate license obligations.

## 6. Proposed platform.sh / Makefile wiring (not yet applied)

This is the integration I would want, but `platform.sh`, the `Makefile`,
`install.sh` and `docs/README.md` were out of scope for this task and were not
edited:

- `make metrics` → `backend/.venv/bin/python3 -m backend.services.observability.collector`
- `make alerts` → `backend/.venv/bin/python3 -m backend.services.observability.alerts`
- A `systemd` (or `platform.sh` `cron`-style) timer running the collector with
  `--textfile` and the evaluator once a minute.
- An optional `--serve 127.0.0.1:9464` daemon managed by `platform.sh start`.
