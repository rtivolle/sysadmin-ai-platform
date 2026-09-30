# Mila roadmap — from prototype to Mila-scale inference & data management

Date: 2026-09-30. Status: plan, not a commitment. Nothing here is implemented;
it is the honest gap analysis between what this repo *has* (branch
`feature/gpu-fleet-management`) and what running inference-fleet + data
management at Mila's scale requires.

## 1. What already exists

**Inference fleet (Phase A, implemented and unit-tested, never run on real
machines):**

- Secretless GPU nodes running a **node-agent** (`:8001`, mTLS with the fleet
  CA): self-enrollment at boot, heartbeat/desired-state exchange, drain,
  local model lifecycle — it executes, never decides placement.
- **Fleet control loop** on the platform host: pure bin-packing scheduler
  (`services/fleet/scheduler.py`), admin fleet API in `agent_tools` (`:3080`:
  approve / drain / decommission / list / health), and the `litellm_sync`
  daemon regenerating LiteLLM's `model_list` (one entry per model × healthy
  node, least-busy routing). Requires the PostgreSQL control store.
- **Fleet registry** in PostgreSQL (`gpu_nodes`, `model_placements`,
  `fleet_desired_state`); declarative per-model policy (replicas, engine,
  VRAM per replica, GPU class, engine params); node lifecycle
  `pending → approved → active → stale → drained → retired` with a
  human-in-the-loop approval gate on enrollment.
- `--peer-inference-hosts` bootstrap; add/remove a node = provision it and
  approve it — no platform config edit. Firewall rulesets ship for the
  platform and inference hosts.

**Data tier:**

- SeaweedFS (object/file store), VictoriaLogs (logs + audit), Valkey
  (quota/lease state), optional PostgreSQL control store (durable quota
  ledger, key store, fleet registry; self-provisioned cluster via
  `backend/config/postgres/postgres.sh`, loopback `:5433`).
- Backup / restore / DR drill, off-host backup sync, HMAC-chained audit
  anchor, local audit outbox with replay.

**Access control & serving:**

- ForwardAuth identity (Bearer tokens + PBKDF2 logins), LiteLLM gateway with
  per-user virtual keys, **per-user** concurrency leases / RPM / TPM / daily
  token budgets with atomic reservation in Valkey, HITL approval gate for
  destructive actions, metrics collector + alert evaluator + Prometheus
  exporter with 8 alert runbooks, admin-only model lifecycle (HuggingFace
  download, vLLM / llama.cpp servers, `/v1/models`).

## 2. What is missing for Mila

| Gap | Today | Mila needs |
|---|---|---|
| Dataset management | SeaweedFS is raw object storage; no dataset concept | versioned datasets, ingestion, lineage |
| Data lifecycle / retention | retention policy explicitly **pending the data owner's decision** (docs/operations.md §9); unbounded growth | lifecycle policies for audit, logs, metrics, models |
| Quotas per team/project + chargeback | quotas are **per-user only** | team/project scopes, budgets, cost attribution |
| Advanced scheduling | pure bin-packing; desired state written by hand; **no priorities, no preemption, no fair-share** | arbitration between teams under contention |
| Multi-tenancy / isolation | nodes shared by everyone; GPU sharing unguarded | team-scoped pools, isolation guarantees |
| Model registry + versioning + promotion | registry exists, no versioning, no staging→prod pipeline | versioned registry, promotion gates |
| Eval harness for served models | `make benchmark` is a 30-task **agent** eval; nothing measures served-model quality | quality gates before promotion |
| Autoscaling | static scheduling only | queue/latency-driven scale of the fleet |
| GPU cost tracking | no GPU-hour accounting per model/team | cost model feeding chargeback |
| Enterprise platform | plaintext inter-machine links (PR-H2 open); documented mTLS CN-binding gap; two-step manual key revocation; single points of failure everywhere | TLS everywhere, CRL, shared secret store, HA |

## 3. Prioritized roadmap

Each item: **why** it matters, **prerequisites**, and **order of magnitude**
(agent-weeks of focused implementation; tests and docs included).

### P0 — trust + multi-team foundations (blockers before any real team onboarding)

**P0.1 — TLS and mTLS hardening on all inter-machine hops.**
Bearer tokens and the Valkey password cross the fleet LAN unencrypted today
(PR-H2); the mTLS handshake has a documented gap (the app cannot bind the
asserted node name to the certificate CN). *Why:* nothing else is trustworthy
on a shared network until this lands. *Prereqs:* PR-H2 design. *Size:*
~1–2 weeks (TLS termination, fix the CN-binding gap via a terminating layer,
CRL distribution on decommission).

**P0.2 — Team/project quota scopes + chargeback ledger.**
Quotas are per-user; Mila works in teams and projects. *Why:* without team
budgets and cost attribution there is no way to run this as a service.
*Prereqs:* PostgreSQL control store (durable ledger; already exists).
*Size:* ~2–3 weeks (teams/projects schema, quota scopes, LiteLLM key →
team mapping, admin API).

**P0.3 — Advanced scheduling: priorities, preemption, fair-share.**
The scheduler bin-packs blindly and cannot arbitrate between teams under
contention. *Why:* contention is the first thing that breaks at scale.
*Prereqs:* P0.2 (team identity on every request). *Size:* ~2–4 weeks.

**P0.4 — Secret management: shared store, two-step revocation fix.**
Revocation is a manual two-step runbook while keys live on two hosts.
*Why:* key rotation must be atomic and auditable at Mila's scale. *Prereqs:*
P0.1. *Size:* ~1–2 weeks.

**P0.5 — GPU cost tracking MVP.**
GPU-hours per model and per team, from the existing observability metrics.
*Why:* chargeback (P0.2) needs a cost signal; autoscaling needs one too.
*Prereqs:* metrics collector (exists). *Size:* ~1–2 weeks.

### P1 — operate at scale

**P1.1 — Autoscaling control loop.**
Queue depth / latency driven desired-state updates (scale replicas, request
more nodes). *Why:* static desired state cannot follow load. *Prereqs:* P0.5,
observability (exists). *Size:* ~3–4 weeks.

**P1.2 — Model registry v2: versioning + promotion pipeline.**
Versioned models with staging → production promotion and rollback. *Why:*
no safe model rollout exists today. *Prereqs:* P0.2. *Size:* ~2–3 weeks.

**P1.3 — Eval harness for served models.**
Quality gates (accuracy/latency/throughput benchmarks) before promotion.
*Why:* P1.2 without quality gates is just a faster way to ship regressions.
*Prereqs:* P1.2. *Size:* ~2–3 weeks.

**P1.4 — Dataset management MVP.**
Dataset registry on SeaweedFS: versioned snapshots, metadata, lineage hooks.
*Why:* raw object storage is not a data platform. *Prereqs:* none (parallelizable).
*Size:* ~3–4 weeks.

**P1.5 — Data lifecycle / retention policies.**
Lifecycle rules for audit logs, metrics, SeaweedFS objects and model
artifacts — the retention decision the docs flag as pending must be made
first. *Why:* unbounded growth of audit and logs is a cost and compliance
risk. *Prereqs:* data-owner retention decision. *Size:* ~1–2 weeks
(policy engine + enforcement jobs).

**P1.6 — Multi-tenancy / isolation: team-scoped node pools.**
Labels/constraints on nodes, team-pinned placements, network policy between
pools. *Why:* teams currently share GPUs with no guardrails. *Prereqs:*
P0.2, P0.3. *Size:* ~2–4 weeks.

### P2 — enterprise

**P2.1 — Platform HA.**
Active-passive control plane; Valkey/Postgres replication; documented
failover. *Why:* single points of failure everywhere today. *Prereqs:* P0.1.
*Size:* ~4–6 weeks.

**P2.2 — Operational ergonomics at node count.**
Hot LiteLLM reload (no restart on routing updates), `auto_approve` for
trusted LANs, per-node live VRAM in the registry. *Why:* manual approval and
restarts do not survive hundreds of nodes. *Prereqs:* fleet CA trust model
(exists). *Size:* ~1–2 weeks.

**P2.3 — Audit archival tier + compliance reporting.**
WORM archival of the audit trail and team-level usage reports. *Why:* the
audit anchor proves integrity; it does not solve long-term retention or
reporting. *Prereqs:* P1.5. *Size:* ~2–3 weeks.

## 4. Suggested order

P0.1 → P0.2 → P0.3 → P0.5 → P0.4, then P1.2 → P1.3 → P1.1 in parallel with
P1.4/P1.5, then P1.6, then P2. The critical path to a Mila pilot (one team,
a handful of nodes, real models) is **P0.1 + P0.2 + P1.2 + P1.3** — security
and the model rollout story — before any team depends on the fleet.
