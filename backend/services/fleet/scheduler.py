"""Fleet placement scheduler (platform side, Phase A).

Pure function, no I/O: from declarative per-model policies and the current
healthy nodes, compute which model replicas run on which node. The daemon
(`litellm_daemon.py`) calls this, diffs against `model_placements`, and pushes
the deltas to the nodes through their heartbeat endpoints.

Policies (from `fleet_desired_state`) look like::

    {
        "qwen2.5-coder-32b": {
            "replicas": 2,
            "engine": "vllm",
            "vram_per_replica_gb": 22.0,
            "gpu_class": {"vram_min_gb": 40, "compute_capability_min": "8.0"},
            "params": {"gpu_memory_utilization": 0.85, "max_model_len": 8192},
        },
    }

Nodes are mappings with at least ``name``, ``vram_total_gb`` and
``compute_capability``; ``vram_free_gb`` (default: total) and ``status``
are honored when present — non placeable statuses (anything but
``approved``/``active``) are excluded, so stale/drained nodes never receive
new replicas.

Algorithm: first-fit decreasing on VRAM — models sorted by
``vram_per_replica_gb`` descending, replicas placed on distinct nodes first,
each replica going to the eligible node with the most free VRAM. Deliberately
simple (a few dozen lines, not a solver): the fleet is 1-10 nodes.
"""
from typing import Any, Dict, List, Tuple

PLACEABLE_STATUSES = frozenset({"approved", "active"})

DEFAULT_VRAM_PER_REPLICA_GB = 20.0


def _parse_capability(value: Any) -> Tuple[int, ...]:
    """'8.6' -> (8, 6); unparseable -> () which never satisfies a minimum."""
    if not isinstance(value, str):
        return ()
    parts = value.strip().split(".")
    try:
        return tuple(int(part) for part in parts if part != "")
    except ValueError:
        return ()


def _node_eligible(node: Dict[str, Any], gpu_class: Dict[str, Any]) -> bool:
    status = node.get("status")
    if status is not None and status not in PLACEABLE_STATUSES:
        return False
    vram_min = gpu_class.get("vram_min_gb")
    if vram_min is not None:
        try:
            if float(node.get("vram_total_gb") or 0) < float(vram_min):
                return False
        except (TypeError, ValueError):
            return False
    cap_min = gpu_class.get("compute_capability_min")
    if cap_min:
        node_cap = _parse_capability(node.get("compute_capability"))
        want_cap = _parse_capability(cap_min)
        if not node_cap or not want_cap or node_cap < want_cap:
            return False
    return True


def _replicas(policy: Dict[str, Any]) -> int:
    try:
        count = int(policy.get("replicas", 1))
    except (TypeError, ValueError):
        raise ValueError("policy replicas must be an integer")
    if count < 0:
        raise ValueError("policy replicas must be non-negative")
    return count


def compute_desired_state(
    policies: Dict[str, Dict[str, Any]],
    nodes: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Bin-pack model replicas onto nodes.

    Returns ``{node_name: [assignment, ...]}`` where each assignment is
    ``{"model": name, "engine": engine, "params": {...},
    "vram_per_replica_gb": float}`` — the payload a node-agent converges
    toward (``{"action": "start", "params": ...}`` per model).
    Replicas that fit nowhere are dropped (the caller compares against the
    requested replica counts to surface the shortfall).
    """
    free: Dict[str, float] = {}
    for node in nodes:
        name = node.get("name")
        if not name:
            continue
        try:
            total = float(node.get("vram_total_gb") or 0)
        except (TypeError, ValueError):
            total = 0.0
        try:
            free_vram = float(node.get("vram_free_gb", total))
        except (TypeError, ValueError):
            free_vram = total
        free[name] = max(0.0, min(free_vram, total))

    ordered = sorted(
        policies.items(),
        key=lambda item: float((item[1] or {}).get("vram_per_replica_gb", DEFAULT_VRAM_PER_REPLICA_GB) or 0),
        reverse=True,
    )
    assignments: Dict[str, List[Dict[str, Any]]] = {node["name"]: [] for node in nodes if node.get("name")}

    for model, policy in ordered:
        policy = policy if isinstance(policy, dict) else {}
        gpu_class = policy.get("gpu_class") or {}
        if not isinstance(gpu_class, dict):
            raise ValueError(f"policy for '{model}' has an invalid gpu_class")
        try:
            vram_need = float(policy.get("vram_per_replica_gb", DEFAULT_VRAM_PER_REPLICA_GB))
        except (TypeError, ValueError):
            raise ValueError(f"policy for '{model}' has an invalid vram_per_replica_gb")
        engine = policy.get("engine") or "vllm"
        params = policy.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError(f"policy for '{model}' has invalid params")

        for _ in range(_replicas(policy)):
            eligible = [node for node in nodes
                        if node.get("name") in free
                        and _node_eligible(node, gpu_class)
                        and free[node["name"]] >= vram_need]
            if not eligible:
                break  # nowhere left to place this replica: shortfall, drop it
            # Prefer nodes that do not already host this model (replica spread),
            # then the node with the most free VRAM. Sort is stable, so ties
            # break by input order — deterministic.
            already = {name for name, items in assignments.items()
                       for item in items if item["model"] == model}
            fresh = [node for node in eligible if node["name"] not in already]
            candidates = fresh or eligible
            chosen = max(candidates, key=lambda node: free[node["name"]])
            assignments[chosen["name"]].append({
                "model": model,
                "engine": engine,
                "params": dict(params),
                "vram_per_replica_gb": vram_need,
            })
            free[chosen["name"]] -= vram_need

    return {name: items for name, items in assignments.items() if items}


def to_node_desired(assignments: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Convert one node's assignments to the node-agent desired-state form::

        {model: {"action": "start", "params": {...}}}
    """
    return {item["model"]: {"action": "start", "params": item.get("params") or {}}
            for item in assignments}


def shortfall(policies: Dict[str, Dict[str, Any]],
              assignments: Dict[str, List[Dict[str, Any]]]) -> Dict[str, int]:
    """Replicas requested but not placed, per model (capacity signal)."""
    placed: Dict[str, int] = {}
    for items in assignments.values():
        for item in items:
            placed[item["model"]] = placed.get(item["model"], 0) + 1
    result = {}
    for model, policy in policies.items():
        missing = _replicas(policy if isinstance(policy, dict) else {}) - placed.get(model, 0)
        if missing > 0:
            result[model] = missing
    return result
