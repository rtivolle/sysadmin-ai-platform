# Glossary

| Term | Meaning |
|---|---|
| **Agent platform** | The FastAPI app at `:3080` (`agent_tools/server.py`) that hosts the ReAct runtime, tools, approval gate, target adapter and audit emission. |
| **Approval gate** | The transactional state machine that authorises mutating commands and target actions. |
| **Bubblewrap / bwrap** | The unprivileged sandbox used to confine agent shell commands. |
| **Citation** | A structured reference (source, section/query, line range, hash) returned with an answer. |
| **cgroup v2** | Linux control-group hierarchy used to enforce memory, PID and CPU limits on a sandbox. |
| **Consumed** | Legacy approval status marking a token that has been used. |
| **Daily budget** | The 2,000,000-token (standard) or 10,000,000-token (P1) per-user daily token ceiling, with an explicit timezone rollover. |
| **DR drill** | The automated disaster-recovery check that validates RPO/RTO and restore integrity. |
| **ForwardAuth** | Traefik's delegated-authentication middleware, backed here by `/verify` on the auth gateway. |
| **Harness / dsh** | DeepSeek Harness, the upstream agent host the integration packages target. |
| **HITL** | Human in the loop; an administrator must approve before execution. |
| **In-flight** | A model call currently executing; at most 2 per user (6 during P1). |
| **Lease** | An expiring ownership token for a concurrency slot, renewed while a request runs. |
| **LiteLLM** | The OpenAI-compatible proxy that enforces per-user quota and virtual keys. |
| **LogsQL** | VictoriaLogs' query language for audit investigation. |
| **Outbox** | The `fsync`-ed JSONL spool that holds audit events until VictoriaLogs accepts them. |
| **P1** | Emergency incident priority; time-bounded, incident-bound elevation. |
| **Pilot** | The intended first production-like use, after qualification. |
| **ReAct** | The Reason + Act loop: Thought → Action → Action Input → Observation. |
| **Reservation** | A pre-dispatch daily-token placeholder replaced by actual usage on settlement. |
| **RPO / RTO** | Recovery Point Objective (≤ 24 h target) / Recovery Time Objective (≤ 4 h target). |
| **Scoped target adapter** | The narrow mutation surface (four actions, nine services, fixed config roots). |
| **SSE** | Server-Sent Events, used for streaming agent responses. |
| **Tenant / user id** | One of `sysadmin-01`…`10`, `emergency-p1-oncall`, or the administrative identity `sysadmin-admin`. |
| **TPM** | Tokens per minute. |
| **Valkey** | The BSD-licensed Redis fork used as the shared state store. |
| **Virtual key** | A per-user LiteLLM key with attached limits; here the same value as the platform bearer key. |
| **VM / Workspace** | Server-owned `backend/data/workspaces/<user>` directory, mode `0700`, bound into the sandbox at `/workspace`. |
