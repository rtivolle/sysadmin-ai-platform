# GPU fleet (Phase A — model/fleet management)

Date: 2026-09-30. Status: prototype (Phase A of `ARCHITECTURE.md` §7).

The GPU tier is a set of secretless inference nodes. The platform node is the
placement authority (PostgreSQL fleet registry); each GPU node runs a
**node-agent** (`:8001`) that only *executes* — it never decides placement.

```
platform (:3080 fleet API, :5432 registry)
   │  mTLS heartbeat poll ──► desired-state ──► node-agent :8001
   │  ◄── actual state + applied version ──────
   ▼
litellm_sync daemon ──► config.yaml (one entry per model × healthy node)
```

## Components

| Piece | Where | What |
|---|---|---|
| `services/node_agent/` | GPU node | FastAPI `:8001`: register, heartbeat, drain, healthz, node info, local model lifecycle |
| `services/node_agent/converge.py` | GPU node | desired-state execution: pull weights → start/stop engines → update registry |
| `services/fleet/scheduler.py` | platform | pure bin-packing: policies + healthy nodes → `{node: [assignments]}` |
| `services/fleet/router.py` | platform | admin Fleet API inside `agent_tools` (`:3080`) |
| `services/fleet/litellm_daemon.py` | platform | control loop: schedule → push → record → regenerate LiteLLM config |
| `services/control_store/fleet_registry.py` | platform | durable registry (`gpu_nodes`, `model_placements`, `fleet_desired_state`) |
| `model_manager/litellm_sync.sync_from_fleet` | platform | generates the fleet `model_list` block (tag `sysadmin-fleet-manager`) |

## Endpoints

Node-agent (`:8001`, LAN, mTLS):

- `POST /api/v1/fleet/register` — self-enrollment at boot (`node_name`, inventory). Status `pending`.
- `POST /api/v1/fleet/nodes/{name}/heartbeat` — state exchange. The platform polls; the request carries `desired_state` + `desired_version`, the response carries actual state + `applied_desired_version`. Convergence runs in a background thread.
- `GET /healthz` — engines alive, `nvidia-smi` responsive.
- `POST /api/v1/fleet/nodes/{name}/drain` — local drain: refuse new models, stop running ones, confirm `drained` (202 while draining).
- `GET /api/v1/node/info` — inventory + state.
- `GET/POST/PATCH/DELETE /api/v1/models…` — local model lifecycle (register/download/start/stop/params), gated by node identity, **no user credentials**.

Fleet API (`:3080`, admin role):

- `POST /api/v1/fleet/nodes/{name}/approve` — human approval gate (pending → approved).
- `POST /api/v1/fleet/nodes/{name}/drain` — calls the node-agent over mTLS, marks `drained`.
- `POST /api/v1/fleet/nodes/{name}/decommission` — retires the node.
- `GET /api/v1/fleet/nodes`, `GET /api/v1/fleet/health` — list + summary (by status, VRAM totals).

Node lifecycle: `pending` → `approved` → `active` → `stale` (3 missed heartbeats, auto-excluded from LiteLLM) → `drained` → `retired`. A `stale` node that heartbeats again returns to `active` automatically.

## Auth model

1. **mTLS at the handshake.** The node-agent's `ssl_context` uses `verify_mode=CERT_REQUIRED` with the fleet CA: without a fleet-signed client certificate the TLS handshake fails and the request never reaches the app. The platform calls nodes with a client certificate (`FLEET_PLATFORM_CERT/KEY`) pinned to the fleet CA (`FLEET_NODE_CA`).
2. **Node identity in the body.** The asserted `node_name` (regex `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`) must match the resource it acts on (`require_self`), and is bound again at human approval time.
3. **No secrets on GPU nodes.** No API keys, no Valkey password, no audit signing keys. The node-agent never imports `services.agent_tools` or `services.auth_gateway`.
4. `NODE_AGENT_AUTH_MODE=disabled` bypasses the identity gate — tests/dev only.

**Documented limit (honest):** uvicorn does not expose the peer client
certificate to the ASGI scope, so the app cannot cryptographically bind the
asserted `node_name` to the certificate CN — a holder of *any* valid fleet
certificate could claim another node's name in its heartbeat. Mitigations:
approval is human-in-the-loop and binds the approved name; heartbeats for
unknown/unapproved names are rejected by the platform; drain and placement
only act on approved nodes. Accepted for the prototype threat model (LAN,
operator-provisioned hosts); a future step is terminating TLS in a layer
that forwards the verified CN (e.g. a small reverse proxy or Traefik with
`clientAuth` + header injection on the platform side).

## Environment variables

| Variable | Default | Used by |
|---|---|---|
| `NODE_AGENT_HOST` / `NODE_AGENT_PORT` | `0.0.0.0` / `8001` | node-agent bind |
| `NODE_AGENT_TLS_CERT` / `NODE_AGENT_TLS_KEY` / `NODE_AGENT_TLS_CA` | — | node-agent mTLS server context |
| `NODE_AGENT_AUTH_MODE` | `mtls` | `mtls` (fail closed on plain HTTP) or `disabled` (dev/tests) |
| `SYSADMIN_ROLE` | `all` | `agent_tools`: mounts fleet API for `all`/`platform`, model lifecycle only for `all` |
| `FLEET_NODE_CA` / `FLEET_PLATFORM_CERT` / `FLEET_PLATFORM_KEY` | — | platform → node-agent mTLS client |
| `FLEET_NODE_PORT` | `8001` | platform → node-agent port |
| `FLEET_INFERENCE_SCHEME` | `https` | scheme in generated LiteLLM `api_base` URLs |
| `FLEET_SYNC_INTERVAL_S` | `10` | daemon poll interval |

## Desired-state policy (per model)

Stored in `fleet_desired_state` (JSON), written by the operator:

```yaml
qwen2.5-coder-32b:
  replicas: 2
  engine: vllm
  vram_per_replica_gb: 40
  gpu_class: { vram_min_gb: 40, compute_capability_min: "8.0" }
  params: { gpu_memory_utilization: 0.85, max_model_len: 8192 }
```

The scheduler bin-packs replicas on free VRAM honoring `gpu_class`; the
daemon pushes `{model: {action: start|stop, params}}` deltas to the nodes.

## Adding a node (workflow)

1. **Provision**: OS + NVIDIA drivers, `install.sh --role inference` (P0.6 of the architecture).
2. **Identity**: client certificate `gpu-0N` signed by the fleet CA, deployed with `NODE_AGENT_TLS_*`.
3. **Start**: launch the node-agent → it self-registers (`pending`, visible in `GET /api/v1/fleet/nodes`).
4. **Approve**: a human calls `POST /api/v1/fleet/nodes/gpu-0N/approve`.
5. **Converge**: the daemon schedules, pushes desired state; the node pulls weights from HF and starts engines.
6. **Serve**: `sync_from_fleet` regenerates the LiteLLM `model_list` (one entry per model × healthy node, `least-busy` routing), restarts LiteLLM if changed.

**Removing**: `POST …/drain` (node stops new models, finishes running ones, confirms `drained`) → `POST …/decommission` (status `retired`, revoke the certificate) → power off. No manual config edit on the platform at any point.

## Cost tracking (chargeback)

GPU cost accrues per node into `gpu_cost_ledger` (`CostTracker.accrue`), is
attributed to models at token prorata (replica-count fallback) and rolls up
to teams via the `team_id` of the `fleet_desired_state` policies; served at
`GET /api/v1/fleet/costs/summary` (admin only, fail-closed 503).
Cost model, limits and price calibration: `docs/fleet-cost-tracking.md`.

## Open / not in Phase A

- Platform-side `register`/`heartbeat` receiver (node → platform push) — currently the platform polls the node-agent; see the design note in `services/node_agent/server.py`.
- Hot reload of LiteLLM (today: restart via `platform.sh`, a few seconds).
- `auto_approve` for trusted LANs; CRL distribution on decommission.
- Per-node free-VRAM in the registry (today reconstructed from placements by the daemon).
