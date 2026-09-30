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
            "priority": 10,                     # optional, default 0 (higher first)
            "node_affinity": {"gpu": "h100"},    # optional, default: all nodes
            "preemption": "lower-priority",     # optional, default "never"
        },
    }

Nodes are mappings with at least ``name``, ``vram_total_gb`` and
``compute_capability``; ``vram_free_gb`` (default: total), ``status`` and
``labels`` (dict, default ``{}``) are honored when present — non placeable
statuses (anything but ``approved``/``active``) are excluded, so stale/drained
nodes never receive new replicas.

Algorithm: first-fit decreasing — models sorted by ``priority`` descending,
then by ``vram_per_replica_gb`` descending; replicas placed on distinct nodes
first, each replica going to the eligible node with the most free VRAM.
Deliberately simple (a few dozen lines, not a solver): the fleet is 1-10 nodes.

Preemption is deliberately conservative: a model with
``preemption == "lower-priority"`` may only evict already-placed replicas of
STRICTLY lower priority when it cannot be placed otherwise, a single level
(the evicted replica is dropped and never re-placed, so there is no cascade),
and only from nodes that are otherwise eligible for it (placeable status,
``gpu_class``, ``node_affinity``). Models with the default
``preemption == "never"`` keep the historical behavior: unplaceable replicas
are dropped and surface through ``shortfall()``.

Reachability note: models are processed in ``priority`` descending order, so
when a replica finds no room, every replica placed earlier in the same pass
has priority greater than or equal to its own — there is never a
strictly-lower-priority in-pass victim. In other words, priority-descending
placement already yields exactly the outcome preemption aims for (the
higher-priority model gets capacity first); the in-pass eviction below is
kept as a specified safeguard and its audit trail (``return_preemptions``)
is real. True cross-loop preemption — evicting *running* lower-priority
replicas that occupy VRAM from a previous control loop — needs the caller
to expose current placements; that contract belongs to the daemon side.
"""
from typing import Any, Dict, List, Optional, Tuple, Union

PLACEABLE_STATUSES = frozenset({"approved", "active"})

DEFAULT_VRAM_PER_REPLICA_GB = 20.0
DEFAULT_PRIORITY = 0
DEFAULT_PREEMPTION = "never"
ALLOWED_PREEMPTION = frozenset({"never", "lower-priority"})


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


def _affinity_matches(node: Dict[str, Any], node_affinity: Dict[str, Any]) -> bool:
    """True when the node's labels contain every affinity key/value pair.

    A policy without affinity (``{}``/missing) matches every node, preserving
    the historical behavior. Non-dict node labels are treated as empty.
    """
    if not node_affinity:
        return True
    labels = node.get("labels") or {}
    if not isinstance(labels, dict):
        labels = {}
    return all(labels.get(key) == value for key, value in node_affinity.items())


def _replicas(policy: Dict[str, Any]) -> int:
    try:
        count = int(policy.get("replicas", 1))
    except (TypeError, ValueError):
        raise ValueError("policy replicas must be an integer")
    if count < 0:
        raise ValueError("policy replicas must be non-negative")
    return count


def _priority(policy: Dict[str, Any], model: str) -> int:
    try:
        value = policy.get("priority", DEFAULT_PRIORITY)
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"policy for '{model}' has an invalid priority")


def _preemption(policy: Dict[str, Any], model: str) -> str:
    value = policy.get("preemption", DEFAULT_PREEMPTION)
    if value not in ALLOWED_PREEMPTION:
        raise ValueError(
            f"policy for '{model}' has an invalid preemption {value!r}: "
            f"expected one of {sorted(ALLOWED_PREEMPTION)}"
        )
    return value


def _node_affinity(policy: Dict[str, Any], model: str) -> Dict[str, Any]:
    affinity = policy.get("node_affinity") or {}
    if not isinstance(affinity, dict):
        raise ValueError(f"policy for '{model}' has an invalid node_affinity")
    return affinity


def _candidate_nodes(nodes: List[Dict[str, Any]],
                     free: Dict[str, float],
                     gpu_class: Dict[str, Any],
                     node_affinity: Dict[str, Any],
                     vram_need: float) -> List[Dict[str, Any]]:
    """Nodes eligible for the model that have enough free VRAM."""
    return [node for node in nodes
            if node.get("name") in free
            and _node_eligible(node, gpu_class)
            and _affinity_matches(node, node_affinity)
            and free[node["name"]] >= vram_need]


def _preemption_target(
    nodes: List[Dict[str, Any]],
    free: Dict[str, float],
    gpu_class: Dict[str, Any],
    node_affinity: Dict[str, Any],
    vram_need: float,
    priority: int,
    placed: Dict[str, List[Dict[str, Any]]],
    placed_priorities: Dict[str, List[int]],
) -> Tuple[Optional[str], List[int]]:
    """Find the cheapest single-level eviction that makes room.

    Returns ``(node_name, indices)`` — the already-placed replicas to evict —
    or ``(None, [])`` when no such eviction exists. Only replicas of STRICTLY
    lower priority are evictable; among feasible nodes the one requiring the
    fewest evictions wins (ties: most free VRAM, then input order, both
    deterministic). Evicted replicas are dropped, never re-placed: no cascade.

    Note: with priority-descending processing order this finds no victim in
    a stateless from-scratch pass (see the module docstring); it is kept as
    the specified safeguard with its audit trail.
    """
    best: Tuple[Optional[str], List[int]] = (None, [])
    best_evictions: Optional[int] = None
    for node in nodes:
        name = node.get("name")
        if not name or name not in free:
            continue
        if not _node_eligible(node, gpu_class):
            continue
        if not _affinity_matches(node, node_affinity):
            continue
        evictable = sorted(
            (idx for idx, prio in enumerate(placed_priorities.get(name, []))
             if prio < priority),
            key=lambda idx: placed_priorities[name][idx],
        )
        gathered = 0.0
        chosen: List[int] = []
        for idx in evictable:
            chosen.append(idx)
            gathered += placed[name][idx]["vram_per_replica_gb"]
            if free[name] + gathered >= vram_need:
                break
        if chosen and free[name] + gathered >= vram_need:
            if (best_evictions is None
                    or len(chosen) < best_evictions
                    or (len(chosen) == best_evictions
                        and free[name] > free[best[0]])):
                best = (name, chosen)
                best_evictions = len(chosen)
    return best


def compute_desired_state(
    policies: Dict[str, Dict[str, Any]],
    nodes: List[Dict[str, Any]],
    return_preemptions: bool = False,
) -> Union[Dict[str, List[Dict[str, Any]]],
            Tuple[Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]]]]:
    """Bin-pack model replicas onto nodes.

    Returns ``{node_name: [assignment, ...]}`` where each assignment is
    ``{"model": name, "engine": engine, "params": {...},
    "vram_per_replica_gb": float}`` — the payload a node-agent converges
    toward (``{"action": "start", "params": ...}`` per model).
    Replicas that fit nowhere are dropped (the caller compares against the
    requested replica counts to surface the shortfall); with
    ``preemption == "lower-priority"`` a blocked replica may first evict
    already-placed replicas of strictly lower priority (single level, no
    cascade — see the module docstring for when this can actually trigger),
    which then show up as shortfall for their own model.

    When ``return_preemptions`` is true, returns ``(assignments,
    preemptions)`` where ``preemptions`` is a list of
    ``{"node", "evicted_model", "evicted_priority", "for_model",
    "for_priority", "vram_freed_gb"}`` entries for audit. The default
    ``return_preemptions=False`` keeps the historical return shape.

    Models are processed by ``priority`` descending, then
    ``vram_per_replica_gb`` descending. Policies without the new fields
    (``priority`` default 0, no ``node_affinity``, ``preemption == "never"``)
    reproduce exactly the pre-chantier-5 behavior.
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

    normalized: Dict[str, Dict[str, Any]] = {
        model: policy if isinstance(policy, dict) else {}
        for model, policy in policies.items()
    }
    ordered = sorted(
        normalized.items(),
        key=lambda item: (
            -_priority(item[1], item[0]),
            -float((item[1] or {}).get("vram_per_replica_gb", DEFAULT_VRAM_PER_REPLICA_GB) or 0),
        ),
    )
    assignments: Dict[str, List[Dict[str, Any]]] = {
        node["name"]: [] for node in nodes if node.get("name")
    }
    # Priorities of the placed assignments, parallel to `assignments` lists.
    placed_priorities: Dict[str, List[int]] = {name: [] for name in assignments}
    preemptions: List[Dict[str, Any]] = []

    for model, policy in ordered:
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
        priority = _priority(policy, model)
        node_affinity = _node_affinity(policy, model)
        preemption = _preemption(policy, model)

        def place_replica(node_name: str) -> None:
            assignments[node_name].append({
                "model": model,
                "engine": engine,
                "params": dict(params),
                "vram_per_replica_gb": vram_need,
            })
            placed_priorities[node_name].append(priority)
            free[node_name] -= vram_need

        for _ in range(_replicas(policy)):
            eligible = _candidate_nodes(nodes, free, gpu_class, node_affinity, vram_need)
            if eligible:
                # Prefer nodes that do not already host this model (replica
                # spread), then the node with the most free VRAM. Sort is
                # stable, so ties break by input order — deterministic.
                already = {name for name, items in assignments.items()
                           for item in items if item["model"] == model}
                fresh = [node for node in eligible if node["name"] not in already]
                candidates = fresh or eligible
                chosen = max(candidates, key=lambda node: free[node["name"]])
                place_replica(chosen["name"])
                continue
            if preemption != "lower-priority":
                break  # nowhere left to place this replica: shortfall, drop it
            target, evict_indices = _preemption_target(
                nodes, free, gpu_class, node_affinity, vram_need,
                priority, assignments, placed_priorities,
            )
            if target is None:
                break  # nothing evictable: shortfall, drop it
            for idx in sorted(evict_indices, reverse=True):
                evicted = assignments[target].pop(idx)
                evicted_priority = placed_priorities[target].pop(idx)
                free[target] += evicted["vram_per_replica_gb"]
                preemptions.append({
                    "node": target,
                    "evicted_model": evicted["model"],
                    "evicted_priority": evicted_priority,
                    "for_model": model,
                    "for_priority": priority,
                    "vram_freed_gb": evicted["vram_per_replica_gb"],
                })
            place_replica(target)

    result = {name: items for name, items in assignments.items() if items}
    if return_preemptions:
        return result, preemptions
    return result


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
