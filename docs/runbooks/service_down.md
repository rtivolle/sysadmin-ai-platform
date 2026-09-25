# Alert runbook: `service_down`

## Meaning

A platform service stopped answering its loopback health probe
(`observability_service_up{service=…} == 0` for 60 s). The collector checks each
service's `/health` endpoint, a TCP port, a Valkey `PING`, or the worker's PID
file for `audit_outbox` (see
[observability.md](../observability.md#service-catalogue)).

## Impact

- `valkey` — the platform **fails closed**: auth, quota, approval and session
  state raise `503`. The whole platform stops serving.
- `victorialogs` — audit events queue in the outbox; the replay worker drains
  them on recovery. No audit ingest while down.
- `seaweedfs` / `inference` / `litellm` — chat/inference returns `502`.
- `auth_gateway` — Traefik ForwardAuth cannot authorise; every routed request
  fails.
- `agent_tools` — the agent runtime and tool API are unavailable.
- `traefik` — the single external entry point is down.
- `harness_gateway` — the DeepSeek Harness front door is down.

## Diagnosis

```bash
./platform.sh status                      # PID, port and RSS per service
./platform.sh logs <service>              # tail the failing service log
curl -s http://127.0.0.1:3081/health      # auth gateway
curl -s http://127.0.0.1:3080/health      # agent platform
curl -s http://127.0.0.1:8000/health      # inference
curl -s http://127.0.0.1:9428/health      # VictoriaLogs
curl -s http://127.0.0.1:4000/health/readiness
```

Check the PID files under `backend/run/*.pid` and the logs under
`backend/logs/*.log` for a crash trace.

## Remediation

```bash
./platform.sh start                       # start everything
./platform.sh restart                     # or bounce the whole stack
./platform.sh test                        # end-to-end verification
```

If a single service refuses to start, restart just that one from the TUI
(`./platform.sh dashboard`) or check its log for the root cause (port conflict
— note port 3080 must be free — missing secret, bad config).

## Escalation

Escalate to the on-call **owner** (currently `owner-pending`) when the service
does not recover after `./platform.sh restart`, or when `valkey`,
`victorialogs` or `traefik` are down for more than a few minutes (they are
single points of failure for the whole platform).
