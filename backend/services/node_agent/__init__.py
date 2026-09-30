"""Node agent: the per-GPU-node control plane service (port 8001).

The node agent runs on inference nodes and holds **no user secrets** — it
authenticates to the platform by mTLS client certificate (provisioned at
install, CN = node name) and never sees API keys, Valkey or the audit trail.

Surface:
- POST /api/v1/fleet/register — register this node with the platform
- POST /api/v1/fleet/nodes/{name}/heartbeat — report local state, receive desired state
- GET /healthz — local liveness of engines + GPU
- POST /api/v1/fleet/nodes/{name}/drain — execute the local drain sequence
- GET /api/v1/node/info — local inventory (GPU, versions)

It reuses `services.model_manager` (registry, downloader, vllm_server,
llamacpp_server) but is **secretless**: it must never import
`services.agent_tools` or `services.auth_gateway`. The local model lifecycle is
gated by node identity (`auth.require_node_identity`), not by user credentials.

The convergence loop (`converge.py`) executes desired-state deltas received in
heartbeat responses: pull missing weights, start/stop engines, update the
registry. The platform decides placement; this node only executes.
"""
