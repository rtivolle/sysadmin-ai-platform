"""Quota-aware distribution weights (platform side).

Philosophy: **quotas bound, distribution degrades gracefully**.

* *Admission* (the quota gate) decides whether a request may consume budget.
  It answers 429 when the budget is gone — see ``quota_scopes.check_budget``.
* *Distribution* decides how the remaining capacity is spread across the
  replicas the scheduler placed. It never invents capacity and never takes a
  model offline because a budget ran out.

Rules implemented by :func:`quota_weights`:

* a team whose budget is exhausted gets weight ``0`` on its replicas — no
  traffic is *preferred* there;
* if that would leave a model with no routable replica at all, the model
  degrades to an :data:`EPSILON` trickle instead of disappearing from the
  routing table. The admission gate still enforces the budget, so the trickle
  surfaces as proper 429s rather than a model-not-found outage;
* otherwise each replica's weight is proportional to its team's
  ``remaining_ratio`` (share of budget left, 0..1);
* a model with no quota signal (unknown team, no ``team_state`` entry) keeps
  the neutral weight ``1.0`` — distribution never penalises what it cannot
  see.

Zero-weight replicas are dropped from the output, so every emitted weight is
strictly positive, matching the ``routing_weights`` contract consumed by
``litellm_sync.sync_from_fleet(..., routing_weights=...)``::

    {model: [{"node": str, "weight": float}]}   # every weight > 0

:func:`team_state_for_models` builds the ``team_state`` input from the fleet
policies (each policy's ``team_id``) and a :class:`QuotaScopes` store.
"""
from typing import Any, Dict, List, Mapping, Optional

# Degraded-trickle weight: routable (strictly positive) but negligible next
# to any healthy weight. Small enough that weighted routing sends only a
# trickle; large enough to survive float rounding in consumers.
EPSILON = 1e-6

_NEUTRAL_WEIGHT = 1.0


def _clamp_ratio(value: Any) -> float:
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"remaining_ratio must be a number, got {value!r}")
    if ratio != ratio:  # NaN
        raise ValueError("remaining_ratio must not be NaN")
    return max(0.0, min(1.0, ratio))


def _replica_nodes(model: str, replicas: Any) -> List[str]:
    nodes: List[str] = []
    for entry in replicas or []:
        node = entry.get("node") if isinstance(entry, Mapping) else None
        if not isinstance(node, str) or not node:
            raise ValueError(
                f"assignment for model '{model}' has no usable 'node': {entry!r}"
            )
        nodes.append(node)
    return nodes


def quota_weights(
    assignments: Mapping[str, Any],
    team_state: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Compute per-replica routing weights from quota state.

    ``assignments`` is ``{model: [{"node": ...}, ...]}`` (the scheduler's
    per-model placement). ``team_state`` is ``{model: {"team_id": str,
    "remaining_ratio": float 0..1, "exhausted": bool}}``.

    Pure function, no I/O: every emitted weight is > 0; exhausted teams get
    weight 0 (dropped from the output) unless that would remove the model's
    last routable capacity, in which case the model degrades to ``EPSILON``.
    """
    team_state = team_state or {}
    routing: Dict[str, List[Dict[str, Any]]] = {}
    for model, replicas in (assignments or {}).items():
        nodes = _replica_nodes(model, replicas)
        if not nodes:
            continue
        state = team_state.get(model)
        if state is None:
            # No quota signal: neutral routing, never penalise the unknown.
            routing[model] = [
                {"node": node, "weight": _NEUTRAL_WEIGHT} for node in nodes
            ]
            continue
        exhausted = bool(state.get("exhausted", False))
        ratio = _clamp_ratio(state.get("remaining_ratio", 0.0))
        entries = [
            {"node": node, "weight": 0.0 if exhausted else ratio}
            for node in nodes
        ]
        entries = [entry for entry in entries if entry["weight"] > 0]
        if not entries:
            # Last remaining capacity: degrade to a trickle, never an outage.
            entries = [{"node": node, "weight": EPSILON} for node in nodes]
        routing[model] = entries
    return routing


def team_state_for_models(
    policies: Mapping[str, Mapping[str, Any]],
    quota_scopes,
) -> Dict[str, Dict[str, Any]]:
    """Build the ``team_state`` input for :func:`quota_weights`.

    ``policies`` are the ``fleet_desired_state`` policies; each model policy
    carries the owning ``team_id``. For every model with a ``team_id`` the
    scope store is asked for today's budget; the result becomes::

        {model: {"team_id": str, "remaining_ratio": float, "exhausted": bool}}

    Models without a ``team_id`` are omitted (neutral routing in
    :func:`quota_weights`). A team with no configured limits gets
    ``remaining_ratio == 1.0`` and ``exhausted == False``. Fail-closed: a
    store outage raises :class:`ConnectionError` instead of fabricating
    ratios — the caller then keeps the previous weights or answers 503,
    never a silently unlimited distribution.
    """
    state: Dict[str, Dict[str, Any]] = {}
    for model, policy in (policies or {}).items():
        if not isinstance(policy, Mapping):
            continue
        team_id = policy.get("team_id")
        if not team_id:
            continue
        admitted, info = quota_scopes.check_budget("team", str(team_id), 0)
        limit = info.get("limit")
        remaining = info.get("remaining")
        if info.get("limited") and limit:
            ratio = max(0.0, min(1.0, float(remaining or 0) / float(limit)))
        else:
            ratio = 1.0
        state[str(model)] = {
            "team_id": str(team_id),
            "remaining_ratio": ratio,
            "exhausted": not admitted,
        }
    return state
