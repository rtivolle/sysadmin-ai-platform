# Architecture

## 1. Purpose and scope

The platform is a **local, authenticated AI operations assistant for ten system
administrators**. It performs log investigation, runbook lookup, configuration
validation and reviewed artifact creation, and can propose — under human
approval — narrowly scoped service actions and staged configuration
deployments. It is designed to run entirely on premises with no third-party
cloud egress at runtime.

The design intent comes from the six French specifications in
[`specs/`](specs/README.md). The implementation deliberately narrows or defers several of their claims;
those differences are recorded in [security.md](security.md#9-known-gaps) and
[development.md](development.md#5-deviations-from-the-design-specifications).

## 2. Design principles

1. **On-premises / sovereignty** — all services run locally. The only network
   dependency is an optional upstream vLLM endpoint, selected by the operator.
2. **Confinement by default** — agent shell commands run inside Bubblewrap with
   a cgroup v2 resource envelope, no network and a read-only host filesystem.
3. **Human in the loop** — any command that is not a simple read-only command
   requires an administrator's approval, bound to the exact request.
4. **Fair resource sharing** — each identity is limited to two in-flight model
   calls (six during P1 elevation), with RPM, TPM and daily-token ceilings.
5. **Defence in depth** — identity is verified independently at Traefik, the
   auth gateway, the agent platform and LiteLLM; no layer trusts a
   client-supplied identity header.
6. **Auditability** — every tool call and model completion produces a structured
   event delivered to VictoriaLogs through a durable local outbox.

## 3. System context

```text
                     10 sysadmin workstations
                (browser · sysadmin-chat CLI · Harness UI)
                                |
                                | HTTPS/WSS (loopback in this prototype)
                                v
+---------------------------------------------------------------+
| Traefik  (web :8080, websecure :8443, dashboard :8081)         |
|  • strips client-supplied identity headers                     |
|  • ForwardAuth -> auth gateway (/verify)                        |
|  • routes /api, /agent, /v1, /s3, /select|/insert               |
+-------------------------------+-------------------------------+
                                |
        +-----------------------+------------------------+
        |                                                |
        v                                                v
+----------------------+                     +------------------------+
| Harness gateway      |                     | Agent platform :3080   |
| :3085 (Node)         |                     |  • ReAct runtime        |
|  • login -> auth     |                     |  • bounded tools        |
|  • per-user dsh      |                     |  • approval gate        |
|    instances         |                     |  • target adapter       |
|    :3180-3280        |                     |  • audit emit           |
+----------+-----------+                     +-----------+------------+
           |                                             |
           |  model + tool calls                         |  tool calls
           v                                             v
+---------------------------------------------------------------+
| LiteLLM proxy :4000  (custom auth + callbacks)                 |
|  • per-user concurrency, RPM, TPM, daily tokens                 |
+-------------------------------+-------------------------------+
                                |
                                v
+---------------------------------------------------------------+
| Inference engine :8000                                          |
|  • OpenAI-compatible local simulator                            |
|  • transparent proxy to UPSTREAM_VLLM_URL when set              |
+---------------------------------------------------------------+

Shared state / data plane (loopback only):
  Valkey :6379            sessions, quota leases/reservations, approvals, P1
  VictoriaLogs :9428      audit query + ingest
  SeaweedFS :8333 S3      artifacts / runbooks / dumps
            :9333 master  :8888 filer  :8085 volume
  Sandbox runner          Bubblewrap + cgroup v2 (per command)
```

## 4. Layers and trust boundaries

| Layer | Component | Trust role |
|---|---|---|
| Edge | Traefik | Terminates the front door, strips spoofed identity headers, delegates authentication. |
| Identity | auth gateway (:3081) | Verifies bearer keys and session cookies; injects authoritative identity headers; enforces daily budget pre-check. |
| Agent | agent platform (:3080) | Owns the ReAct loop, tools, approval state machine, target adapter and audit emission. |
| Quota | LiteLLM (:4000) + Valkey | Independent per-user enforcement of concurrency, RPM, TPM and daily tokens. |
| Inference | inference engine (:8000) | OpenAI-compatible facade; simulator or upstream vLLM. |
| Execution | `bwrap-runner.sh` | Bubblewrap namespaces + cgroup v2 limits; fails closed if limits cannot be installed. |
| Audit | VictoriaLogs + outbox | Durable, replayable event delivery; at-least-once with `event_id` dedup. |
| Harness | Node gateway + plugin | One harness process per authenticated user; mirrors policy and audit. |

The critical trust rule is that **identity is derived only from verified
credentials**. Traefik removes inbound `X-User`/`X-Forwarded-*` headers, the auth
gateway ignores them, and the agent platform re-authenticates every request
rather than trusting upstream headers. See [security.md](security.md).

## 5. End-to-end request flows

### 5.1 Terminal chat (`sysadmin-chat`)

```text
CLI -> POST http://127.0.0.1:8080/api/v1/agent/chat   (Bearer <user>.key)
  Traefik: strip-client-headers -> ForwardAuth /verify
    auth gateway: validate bearer -> daily-budget check -> 200 + identity headers
  agent platform: authenticate_request -> acquire concurrency lease
    session store (Valkey): create/get per-user session (server-owned workspace)
    ReAct loop step:
      call LiteLLM /v1/chat/completions with the user's virtual key
        LiteLLM custom auth: identity, daily budget, per-user limits
        inference engine: simulated reply or upstream vLLM
      parse Thought/Action/Action Input
      execute bounded tool (search / lint+diff / runbook / sandbox)
      append Observation, repeat (max_steps, default 5)
    save session, release lease -> JSON or SSE response
```

Streaming requests use Server-Sent Events (`Accept: text/event-stream` or
`"stream": true`); each event carries a `chunk` and accumulated `citations`,
terminated by `data: [DONE]`.

### 5.2 Human-in-the-loop command

```text
model proposes Action: sandboxed_bash
  evaluate_command_safety(command)
    BLOCKED            -> refuse, audit blocked event
    ALLOW              -> execute immediately in sandbox
    APPROVAL_REQUIRED  -> create approval (300 s TTL), return approval_required
administrator (different identity, admin role):
  POST /api/approvals/decide {approval_id, approved}
requester:
  POST /api/tools/execute {sandboxed_bash, command, approval_id}
    claim_for_execution: atomic approved -> executing, binds user/session/
      workspace/command/content-hash, single use
    execute in Bubblewrap sandbox, then complete execution
```

### 5.3 Scoped target mutation

```text
POST /api/v1/approval/propose
  validate action + target (9 services | config roots)
  service action  -> command "systemctl <verb> <svc>.service", hash command
  config_deploy   -> read staged file inside workspace, validate syntax,
                     hash content + base content, record both
  -> approval token (pending)
POST /api/v1/approvals/decide   (admin, not the requester)
POST /api/v1/adapter/execute
  claim token -> ServiceManager (systemctl, 15 s timeout) or ConfigDeployer
  ConfigDeployer: validate -> tamper check -> syntax -> conflict check ->
                  backup -> atomic replace -> verify -> rollback on failure
```

### 5.4 Audit

Every tool outcome is sent to VictoriaLogs over
`/insert/jsonline`. If ingest fails, the event is `fsync`-ed to a local JSONL
outbox and replayed in order by a supervised worker. Delivery is at-least-once;
`event_id` allows deduplication.

## 6. Deployment topology

The whole stack runs as native processes on one Linux host — there is no Docker
requirement. `backend/platform.sh` supervises eight foreground services plus an
outbox worker using PID files in `backend/run/` and logs in `backend/logs/`.
The Harness gateway and per-user harness instances are Node processes started
separately (see [harness-integration.md](harness-integration.md)).

A single host is a single point of failure. Backups exist but do not provide high
availability; see [backup-restore.md](backup-restore.md).

## 7. Resource model

- **Sandbox**: `memory.max = 4 GiB` with `memory.swap.max = 0` (the 4 GiB
  bound covers total memory, not just RSS), `pids.max = 128`,
  `cpu.max = 200000 100000` (200 % of one CPU), 15 s wall clock then SIGKILL
  after a 5 s grace period, no network, read-only host mounts.
- **Per-user model calls**: 2 in flight (6 during P1), 60 RPM / 200 RPM,
  150,000 / 500,000 TPM, 2,000,000 / 10,000,000 tokens per day.
- **Cluster admission**: 8 slots (10 during P1) when
  `ENFORCE_CLUSTER_CONCURRENCY=1`; disabled by default.
- **Session/lease TTL**: 24 h session TTL; 120 s concurrency leases renewed every
  30 s; daily reservations carry an 8-day TTL.
