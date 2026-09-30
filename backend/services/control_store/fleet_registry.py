#!/usr/bin/env python3
"""Durable fleet registry: the platform's PostgreSQL source of truth for GPU nodes.

Tables (see ``schema.py``):

* ``gpu_nodes`` - identity, GPU inventory, lifecycle status, last heartbeat;
* ``model_placements`` - model -> node assignments, desired vs actual state;
* ``fleet_desired_state`` - the declarative per-model policies the scheduler
  writes and the convergence loop executes.

Like :class:`KeyStore` and the ledger, this store is written against the
narrow ``Executor`` protocol (query / execute / transaction): every statement
is parameterized, and an unreachable store raises
:class:`ControlStoreUnavailable` (a ``ConnectionError``) which callers
translate to HTTP 503 — never a silent in-memory fallback.

Node lifecycle: ``pending`` (registered, awaiting a human) -> ``approved`` ->
``active`` (healthy heartbeats) -> ``stale`` (3 missed heartbeats) ->
``drained`` (graceful removal confirmed) -> ``retired`` (decommissioned).
A ``stale`` node that heartbeats again returns to ``active`` automatically.
"""
import json
import re
from typing import Any, Dict, List, Optional

from services.control_store.errors import ControlStoreIntegrityError
from services.control_store.executor import Executor
from services.model_manager.registry import validate_name as validate_model_name

# Same contract as node_agent.auth.NODE_NAME_RE: a single path-safe token,
# re-declared here so the platform side never imports the node agent.
_NODE_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
_NODE_NAME_RE = re.compile(_NODE_NAME_PATTERN)

STATUSES = frozenset({"pending", "approved", "active", "stale", "drained", "retired"})
# Nodes in these statuses may receive model placements.
_PLACEABLE = frozenset({"approved", "active"})

_REGISTER_NODE = """
INSERT INTO gpu_nodes
    (name, gpu_model, gpu_count, vram_total_gb, compute_capability, address,
     status, last_heartbeat)
VALUES (%s, %s, %s, %s, %s, %s, 'pending', now())
ON CONFLICT (name) DO UPDATE SET
    gpu_model = EXCLUDED.gpu_model,
    gpu_count = EXCLUDED.gpu_count,
    vram_total_gb = EXCLUDED.vram_total_gb,
    compute_capability = EXCLUDED.compute_capability,
    address = EXCLUDED.address,
    last_heartbeat = now()
"""

_GET_NODE = """
SELECT name, gpu_model, gpu_count, vram_total_gb, compute_capability, address,
       status, last_heartbeat, approved_by, approved_at, created_at
FROM gpu_nodes WHERE name = %s
"""

_LIST_NODES = """
SELECT name, gpu_model, gpu_count, vram_total_gb, compute_capability, address,
       status, last_heartbeat, approved_by, approved_at, created_at
FROM gpu_nodes
"""

_HEARTBEAT = """
UPDATE gpu_nodes
SET last_heartbeat = now(),
    status = CASE WHEN status = 'stale' THEN 'active' ELSE status END
WHERE name = %s
"""

_SET_STATUS = "UPDATE gpu_nodes SET status = %s WHERE name = %s"

_APPROVE_NODE = """
UPDATE gpu_nodes
SET status = 'approved', approved_by = %s, approved_at = now(), last_heartbeat = now()
WHERE name = %s AND status = 'pending'
"""

_DRAIN_NODE = """
UPDATE gpu_nodes SET status = 'drained'
WHERE name = %s AND status IN ('approved', 'active', 'stale')
"""

_DECOMMISSION_NODE = """
UPDATE gpu_nodes SET status = 'retired'
WHERE name = %s AND status <> 'retired'
"""

_MARK_STALE = """
UPDATE gpu_nodes SET status = 'stale'
WHERE status = 'active' AND last_heartbeat < now() - make_interval(secs => %s)
"""

_HEALTHY_NODES = """
SELECT name, gpu_model, gpu_count, vram_total_gb, compute_capability, address,
       status, last_heartbeat, approved_by, approved_at, created_at
FROM gpu_nodes
WHERE status IN ('approved', 'active')
  AND last_heartbeat > now() - make_interval(secs => %s)
ORDER BY name
"""

_SET_DESIRED_STATE = """
INSERT INTO fleet_desired_state (model_name, policy, updated_at)
VALUES (%s, %s, now())
ON CONFLICT (model_name) DO UPDATE SET policy = EXCLUDED.policy, updated_at = now()
"""

_GET_DESIRED_STATE = "SELECT model_name, policy FROM fleet_desired_state ORDER BY model_name"

_RECORD_PLACEMENT = """
INSERT INTO model_placements (model_name, node_name, desired_state, actual_state, updated_at)
VALUES (%s, %s, %s, %s, now())
ON CONFLICT (model_name, node_name) DO UPDATE SET
    desired_state = EXCLUDED.desired_state,
    actual_state = EXCLUDED.actual_state,
    updated_at = now()
"""

_LIST_PLACEMENTS = """
SELECT model_name, node_name, desired_state, actual_state, updated_at
FROM model_placements ORDER BY model_name, node_name
"""

_NODE_COLUMNS = ("name", "gpu_model", "gpu_count", "vram_total_gb", "compute_capability",
                 "address", "status", "last_heartbeat", "approved_by", "approved_at",
                 "created_at")


def validate_node_name(name: Any) -> str:
    if not isinstance(name, str) or not _NODE_NAME_RE.match(name):
        raise ValueError(f"node name must match {_NODE_NAME_PATTERN}")
    return name


def _node_row(row: tuple) -> Dict[str, Any]:
    return dict(zip(_NODE_COLUMNS, row))


class FleetRegistry:
    """Durable GPU-node and placement registry over an ``Executor``.

    Every method fails closed: when the executor cannot reach the store,
    :class:`ControlStoreUnavailable` (a ``ConnectionError``) propagates to the
    caller, which must surface it as 503.
    """

    def __init__(self, executor: Executor):
        self._executor = executor

    # --- nodes ------------------------------------------------------------
    def register_node(
        self,
        name: str,
        *,
        gpu_model: Optional[str] = None,
        gpu_count: int = 0,
        vram_total_gb: float = 0.0,
        compute_capability: Optional[str] = None,
        address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Enroll a node as ``pending``; re-registration refreshes inventory.

        A re-register never changes lifecycle status — only a human (approve)
        or the drain/decommission flow may move a node.
        """
        name = validate_node_name(name)
        if gpu_count is not None and (not isinstance(gpu_count, int) or gpu_count < 0):
            raise ControlStoreIntegrityError("gpu_count must be a non-negative integer")
        self._executor.execute(
            _REGISTER_NODE,
            (name, gpu_model, gpu_count or 0, vram_total_gb or 0.0,
             compute_capability, address),
        )
        node = self.get_node(name)
        if node is None:  # pragma: no cover - the upsert above just wrote it
            raise ControlStoreIntegrityError(f"node '{name}' was not persisted")
        return node

    def get_node(self, name: str) -> Optional[Dict[str, Any]]:
        name = validate_node_name(name)
        rows = self._executor.query(_GET_NODE, (name,))
        return _node_row(rows[0]) if rows else None

    def list_nodes(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if status is not None:
            if status not in STATUSES:
                raise ValueError(f"unknown node status '{status}'")
            rows = self._executor.query(_LIST_NODES + " WHERE status = %s ORDER BY name", (status,))
        else:
            rows = self._executor.query(_LIST_NODES + " ORDER BY name", ())
        return [_node_row(row) for row in rows]

    def heartbeat(self, name: str, state: Optional[Dict[str, Any]] = None) -> bool:
        """Record a heartbeat; a ``stale`` node that heartbeats returns to ``active``.

        `state` is accepted for forward compatibility (the reported payload is
        logged by the caller); the durable record is the heartbeat timestamp.
        Returns True when the node exists.
        """
        name = validate_node_name(name)
        if state is not None and not isinstance(state, dict):
            raise ValueError("heartbeat state must be an object")
        return self._executor.execute(_HEARTBEAT, (name,)) == 1

    def set_node_status(self, name: str, status: str) -> bool:
        name = validate_node_name(name)
        if status not in STATUSES:
            raise ValueError(f"unknown node status '{status}'")
        return self._executor.execute(_SET_STATUS, (status, name)) == 1

    def approve_node(self, name: str, approved_by: str) -> bool:
        """Human approval gate: only a ``pending`` node may be approved.

        Returns False when the node is unknown or not pending — the caller
        must not treat that as an approval.
        """
        name = validate_node_name(name)
        if not approved_by or not isinstance(approved_by, str):
            raise ControlStoreIntegrityError("approved_by must name the approving human")
        return self._executor.execute(_APPROVE_NODE, (approved_by, name)) == 1

    def drain_node(self, name: str) -> bool:
        """Mark a node ``drained`` after its node-agent confirmed the drain.

        Only a placeable/stale node may be drained; retired nodes stay retired.
        """
        name = validate_node_name(name)
        return self._executor.execute(_DRAIN_NODE, (name,)) == 1

    def decommission_node(self, name: str) -> bool:
        """Retire a node: it leaves the fleet permanently (certificate
        revocation happens out of band at the platform)."""
        name = validate_node_name(name)
        return self._executor.execute(_DECOMMISSION_NODE, (name,)) == 1

    def mark_stale(self, max_age_seconds: float = 30.0) -> int:
        """Sweep: mark ``active`` nodes with no recent heartbeat ``stale``."""
        return self._executor.execute(_MARK_STALE, (float(max_age_seconds),))

    def healthy_nodes(self, max_age_seconds: float = 30.0) -> List[Dict[str, Any]]:
        """Nodes eligible for placement: approved/active with a fresh heartbeat."""
        rows = self._executor.query(_HEALTHY_NODES, (float(max_age_seconds),))
        return [_node_row(row) for row in rows]

    def placeable_nodes(self, max_age_seconds: float = 30.0) -> List[Dict[str, Any]]:
        return [node for node in self.healthy_nodes(max_age_seconds)
                if node["status"] in _PLACEABLE]

    # --- desired state ------------------------------------------------------
    def set_desired_state(self, model: str, policy: Dict[str, Any]) -> None:
        """Write the declarative policy for one model (scheduler input)."""
        model = validate_model_name(model)
        if not isinstance(policy, dict):
            raise ValueError("policy must be an object")
        self._executor.execute(_SET_DESIRED_STATE, (model, json.dumps(policy, sort_keys=True)))

    def get_desired_state(self) -> Dict[str, Dict[str, Any]]:
        rows = self._executor.query(_GET_DESIRED_STATE, ())
        return {model: json.loads(policy) for model, policy in rows}

    # --- placements -----------------------------------------------------------
    def record_placement(
        self,
        model: str,
        node: str,
        state: Any,
    ) -> None:
        """Record desired vs actual placement for one (model, node) pair.

        `state` is either a mapping with optional ``desired``/``actual`` keys
        or a single actual-state string (desired left unchanged is not
        expressible — pass the mapping form for that).
        """
        model = validate_model_name(model)
        node = validate_node_name(node)
        if isinstance(state, dict):
            desired = state.get("desired")
            actual = state.get("actual")
        elif isinstance(state, str):
            desired, actual = None, state
        else:
            raise ValueError("placement state must be an object or an actual-state string")
        self._executor.execute(_RECORD_PLACEMENT, (model, node, desired, actual))

    def list_placements(self) -> List[Dict[str, Any]]:
        rows = self._executor.query(_LIST_PLACEMENTS, ())
        return [
            {"model_name": r[0], "node_name": r[1], "desired_state": r[2],
             "actual_state": r[3], "updated_at": r[4]}
            for r in rows
        ]
