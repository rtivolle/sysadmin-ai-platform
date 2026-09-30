"""GPU fleet control plane (platform side).

The platform decides *where* models run; the node agents on the GPU nodes
execute. This package holds the decision side:

* :mod:`services.fleet.scheduler` — pure bin-packing: desired model policies
  plus node inventory become a per-node desired state, with shortfall
  reporting when the fleet cannot satisfy a policy.
* :mod:`services.fleet.router` — the human-facing Fleet API mounted in
  ``agent_tools`` (approve / drain / decommission / list / health). Approval
  is the human gate; drain and decommission are irreversible fleet operations.
* :mod:`services.fleet.litellm_daemon` — the control loop: poll node agents
  with heartbeats, push desired state, record actual state, and regenerate
  the LiteLLM config so the gateway routes to healthy replicas.

The durable state (node registry, placements, desired policies) lives in
:mod:`services.control_store.fleet_registry`; see ``docs/gpu-fleet.md`` for
the architecture and the add-node workflow.
"""

from services.fleet import litellm_daemon, router, scheduler

__all__ = ["litellm_daemon", "router", "scheduler"]
