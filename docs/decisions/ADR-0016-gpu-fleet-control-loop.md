# ADR-0016: GPU fleet control loop (declarative desired state, dynamic LiteLLM routing)

- **Date:** 2026-09-30
- **Status:** Proposed — design recorded; implementation in flight on branch
  `feature/gpu-fleet-management`, owner acceptance pending.
- **Source of divergence:** `~/workspace/your_files/architecture-inference-gpu/ARCHITECTURE.md`
  (rev. 2, 2026-09-30), §§6–7, 10. Today `model_manager` is an admin API
  **inside** `agent_tools` on a single host; adding a GPU host currently means
  hand-editing configs on the platform side. This record covers the control
  plane that makes the GPU tier frictionless to scale.

## Context

The fleet must grow from 1 to N GPU nodes **without any manual config edit on
the platform side**: provision a node → it joins → it serves traffic; drain a
node → it leaves, with zero dropped in-flight work where feasible. The
platform is the single source of truth (PostgreSQL); node-agents converge
real state toward desired state — a kubelet-lite, deliberately without an
orchestrator (Nomad/K3s alternatives rejected in ARCHITECTURE.md §12).

## Decision

### The loop

1. **Auto-registration.** At boot, `node_agent` calls
   `POST /fleet/nodes/register` (mTLS) with node name, GPU inventory (model,
   count, VRAM, compute capability), driver/CUDA/vLLM versions and disk
   space. Nodes need to know exactly one thing: the platform address
   (install-time input).
2. **`pending` + human approval.** A new node is visible in the admin but
   receives nothing until a human approves it — the natural extension of the
   project's approval-gate posture. An `auto_approve` option for trusted LANs
   may come later (open question, spec §13).
3. **Declarative desired state.** `fleet_desired_state` table (PostgreSQL),
   exportable as a versioned `fleet.yaml`, per model: replicas, engine,
   `gpu_class` constraints (VRAM min, compute capability), engine params.
4. **Scheduler (platform, simple).** Bin-packing over the free VRAM reported
   by heartbeats, filtered by `gpu_class`. Assigns each replica to a node
   and writes per-node desired state. The node never decides placement
   itself; it executes.
5. **Convergence (node-agent).** Desired state arrives in the **heartbeat
   response delta** (10 s cadence). The agent pulls missing weights (direct
   from Hugging Face, never via the platform), starts/stops engines with the
   right flags, and reports real state (loaded models, free VRAM, engine
   health, applied desired-state version) on the next heartbeat.
6. **Dynamic LiteLLM `model_list`.** `litellm_sync` becomes a daemon: on any
   state transition (approved register, applied delta, drain, stale,
   decommission) it regenerates `config.yaml` — **one deployment per (model ×
   healthy node)** under the same `model_name`, `api_base` → node `:8000`.
   The existing `least-busy` routing strategy absorbs heterogeneous GPUs
   without manual tuning. Regeneration is idempotent (hash-gated); reload is
   qualified on the pinned LiteLLM version (hot admin reload if available,
   otherwise a fast loopback restart with the retry window measured).
7. **Health and graceful drain.** Platform probes `:8000/health` (façade) +
   `:8001/healthz` (node-agent) every 15 s; 2 failures → excluded from the
   `model_list` (node stays `registered` for debugging). **3 missed
   heartbeats (30 s) → `stale`** → excluded automatically, re-included on the
   next healthy heartbeat. `POST /fleet/nodes/{name}/drain` removes the node
   from new traffic, lets in-flight requests finish (120 s grace,
   configurable), the agent confirms `drained`. `decommission` → `retired`,
   certificate revoked (local CRL), `model_list` purged.
8. **Registry tables** (`control_store`/PostgreSQL, ADR-0014): `gpu_nodes`
   (identity, GPU, status, last heartbeat), `model_placements`
   (model → node, real vs desired), `fleet_desired_state` (policies).

### Authentication of the fleet

- **mTLS at the handshake, day 1.** Every platform↔node link (inference
  `:8000`, fleet control `:8001`, audit replay `:9428`) requires mutual TLS
  (`CERT_REQUIRED`) against a fleet CA provisioned by `install.sh`. Per-node
  client certificate, CN = node name, provisioned at install, revocable at
  decommission. This replaces PR-H1's accepted plaintext LAN.
- **Node identity is asserted in the request body and bound to approval.**
  The CN presented at the TLS handshake must match the node name in the
  registration payload and the approved record; an unknown or unapproved node
  gets no desired state.
- **Honest limitation — uvicorn and the ASGI scope.** uvicorn does **not**
  expose the client certificate to the ASGI scope, so the application cannot
  verify the CN in-app today. Residual risk: **impersonation between fleet
  members** — a compromised or rogue fleet member holding a valid fleet cert
  could assert another node's name in the request body. Accepted because:
  all fleet members are platform-provisioned hosts (the fleet is not a hostile
  network); the firewall is default-deny with inference ports accepting only
  the platform address and the platform accepting fleet ports only from
  registered node addresses; every lifecycle transition (register / approve /
  drain / decommission) emits an audit event, so impersonation leaves a trace.
  Follow-up: terminate node TLS at a layer that can verify the DN (Traefik
  with `clientAuth` forwarding the verified identity, or a small
  reverse-proxy shim) before trusting the in-body name for anything beyond
  telemetry.

## Consequences

- **Positive.** Adding/removing a GPU node touches no platform config: the
  full add workflow (provision → cert → auto-register → approve → converge →
  `model_list` regen → traffic) and the remove workflow (drain → drained →
  decommission → revoke) are mechanical. Heterogeneous GPUs are absorbed by
  per-node engine flags and `least-busy` routing. Model weights never
  transit the platform node (HF-direct per node); the registry (which points
  at which weights) is backed up with the platform.
- **Negative / accepted.** New control-plane surface: a service, three
  tables, a scheduler and a sync daemon for a prototype. The uvicorn CN gap
  (above) is accepted with the stated mitigations. Heartbeat staleness is a
  30-second detection window — fast enough for a serving tier, slow enough
  to avoid flapping on GC pauses; tune from measurement.
- **Negative / accepted.** No autoscaling: desired state is set by a human
  (via `fleet.yaml`/admin) or computed by the scheduler from policy — it is
  declarative, not load-driven. Beyond ~10 GPU nodes, revisit Nomad; the
  node-agent REST surface is already what a scheduler would call, so the
  work is not lost (spec §7.5/§12).
- **Not yet qualified.** Hot LiteLLM reload on the pinned version, drain
  grace behaviour under load, stale/flap timing on real machines, scheduler
  behaviour under VRAM pressure — all open corrective items.

## Evidence

Design source: `ARCHITECTURE.md` §§6 (weight distribution), 7.1–7.6
(registration, desired state, dynamic `model_list`, health/drain, scale
limits, add-node workflow), 10 (security). Implementation in flight on this
branch by parallel lanes carries the unit tests (heartbeat state machine,
scheduler bin-packing, idempotent `model_list` regeneration, mTLS
handshake) and its own TEST_READY.md entries. **Not measured:** real
multi-host bring-up, hot LiteLLM reload, failover — recorded as
"not measured" in `docs/status/TEST_READY.md` per AGENTS.md §4.

## Corrective work

1. Qualify the LiteLLM reload path (hot admin reload vs fast restart) on the
   pinned LiteLLM version; measure the interruption window.
2. Measure drain/stale/decommission transitions on real machines, including
   in-flight request completion during the 120 s grace period.
3. Decide the spec §13 open questions: identity bootstrap (install-time cert
   vs one-time bootstrap token), `auto_approve` policy, weight distribution
   (HF-direct vs LAN mirror), TTFT/timeout budget for LAN-crossing prompts,
   alerting channel.
