#!/usr/bin/env python3
"""Desired-state-driven autoscaler for the GPU fleet (Phase B).

The fleet control loop (``litellm_daemon.sync_once``) is register ->
heartbeat -> converge on a *declarative* desired state: per-model policies in
``fleet_desired_state`` carry a ``replicas`` count and the scheduler
bin-packs that many replicas onto healthy nodes. This module closes the loop
*one level up*: it watches serving signals (queue depth, TTFT) and adjusts the
``replicas`` field itself, so the next converge cycle places more or fewer
replicas without any operator action.

The design is deliberately split:

* :func:`decide` — pure function, no I/O. Given policies, current metrics,
  quota headroom, ``now`` and the last scale timestamps, it returns
  ``{model: new_replicas}`` for the models whose replica count should change.
  Everything in the scaling policy (signals, thresholds, cooldowns, bounds)
  lives here and is unit-testable without a database.
* :func:`ensure_schema` / :func:`load_last_scales` / :func:`record_scale_event`
  — the durable cooldown ledger. Cooldowns are persisted in the
  ``autoscale_events`` table (created here with ``CREATE TABLE IF NOT EXISTS``;
  ``services.control_store.schema`` is intentionally untouched) so a daemon
  restart cannot reset the cooldown clocks and flap the fleet.
* :func:`resolve_quota_headroom` / :func:`build_team_state` — quota guardrails.
  The quota store (chantier 1, ``QuotaScopes``) may not exist yet; every
  access is defensive and falls back to the policy's own ``max_replicas``.
  ``quota_headroom`` is a HARD ceiling: the autoscaler never scales past it.

Signals and defaults (see ``docs/fleet-autoscaling.md`` for the operator
view)::

    scale-up   queue_depth > scale_up_queue_depth (default 8)
            OR ttft_p99_s  > target_ttft_s        (default 0.5 s)
    scale-down queue_depth == 0 AND ttft_p99_s <= target_ttft_s
               (one replica per cycle, never below min_replicas)

Both directions are rate-limited per model (``scale_up_cooldown_s`` default
300, ``scale_down_cooldown_s`` default 900) and clamped to
``min_replicas <= new <= min(max_replicas, quota_headroom)``. A model with no
metrics takes no action — the autoscaler never scales on a guess.
"""
import logging
import time
from typing import Any, Dict, Optional

_LOG = logging.getLogger("fleet.autoscaler")

# --- policy field defaults -------------------------------------------------
DEFAULT_MIN_REPLICAS = 1
DEFAULT_MAX_REPLICAS = 4
DEFAULT_TARGET_TTFT_S = 0.5
DEFAULT_SCALE_UP_QUEUE_DEPTH = 8
DEFAULT_SCALE_UP_COOLDOWN_S = 300.0
DEFAULT_SCALE_DOWN_COOLDOWN_S = 900.0

# --- cooldown ledger --------------------------------------------------------
_AUTOSCALE_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS autoscale_events (
    model TEXT NOT NULL,
    action TEXT NOT NULL,
    at DOUBLE PRECISION NOT NULL
)
"""

_LAST_SCALES_QUERY = "SELECT model, MAX(at) FROM autoscale_events GROUP BY model"

_RECORD_SCALE_EVENT = (
    "INSERT INTO autoscale_events (model, action, at) VALUES (%s, %s, %s)"
)


def ensure_schema(executor: Any) -> None:
    """Create the ``autoscale_events`` cooldown ledger if it is missing.

    Takes any object with an ``execute(sql, params)`` method (the
    ``Executor`` protocol from ``services.control_store.executor``).
    """
    executor.execute(_AUTOSCALE_EVENTS_DDL, ())


def load_last_scales(executor: Any) -> Dict[str, float]:
    """Return ``{model: epoch_seconds}`` of the most recent scale event."""
    last: Dict[str, float] = {}
    for row in executor.query(_LAST_SCALES_QUERY, ()):
        model, at = row[0], row[1]
        if at is None:
            continue
        try:
            last[str(model)] = float(at)
        except (TypeError, ValueError):
            continue
    return last


def record_scale_event(executor: Any, model: str, action: str, at: float) -> None:
    """Persist one scaling decision so cooldowns survive daemon restarts."""
    executor.execute(_RECORD_SCALE_EVENT, (model, action, float(at)))


# --- policy parsing ---------------------------------------------------------
def _int_field(policy: Dict[str, Any], name: str, default: int) -> int:
    try:
        value = int(policy.get(name, default))
    except (TypeError, ValueError):
        raise ValueError(f"autoscale policy field '{name}' must be an integer")
    if value < 0:
        raise ValueError(f"autoscale policy field '{name}' must be non-negative")
    return value


def _float_field(policy: Dict[str, Any], name: str, default: float) -> float:
    try:
        value = float(policy.get(name, default))
    except (TypeError, ValueError):
        raise ValueError(f"autoscale policy field '{name}' must be a number")
    if value < 0:
        raise ValueError(f"autoscale policy field '{name}' must be non-negative")
    return value


def _bounds(policy: Dict[str, Any], quota_headroom: Dict[str, int],
            model: str) -> tuple:
    """(current, lo, hi): replica count bounds for one model."""
    min_r = _int_field(policy, "min_replicas", DEFAULT_MIN_REPLICAS)
    max_r = _int_field(policy, "max_replicas", DEFAULT_MAX_REPLICAS)
    if min_r > max_r:
        raise ValueError(
            f"autoscale policy for '{model}': min_replicas > max_replicas")
    current = _int_field(policy, "replicas", min_r)
    ceiling = quota_headroom.get(model, max_r)
    try:
        ceiling = int(ceiling)
    except (TypeError, ValueError):
        ceiling = max_r
    hi = min(max_r, max(0, ceiling))
    lo = min(min_r, hi)  # a zero quota ceiling beats min_replicas (fail-safe)
    return current, lo, hi


# --- the decision function ----------------------------------------------------
def decide(
    policies: Dict[str, Dict[str, Any]],
    metrics: Dict[str, Dict[str, Any]],
    quota_headroom: Dict[str, int],
    now: float,
    last_scales: Dict[str, float],
) -> Dict[str, int]:
    """Compute new replica counts. Pure: no I/O, no clock reads.

    :param policies: ``{model: policy}`` from ``fleet_desired_state``.
    :param metrics: ``{model: {"queue_depth": int, "ttft_p99_s": float,
        "latency_p99_s": float}}``; a model absent here takes no action.
    :param quota_headroom: ``{model: max replicas the quota allows}`` — a hard
        ceiling the result never exceeds.
    :param now: current epoch seconds (injected for testability).
    :param last_scales: ``{model: epoch of last scale event}`` from the
        ``autoscale_events`` ledger (see :func:`load_last_scales`).
    :returns: ``{model: new_replicas}`` — only models whose count changes.
    """
    decisions: Dict[str, int] = {}
    for model in sorted(policies):
        policy = policies[model]
        if not isinstance(policy, dict):
            continue
        current, lo, hi = _bounds(policy, quota_headroom, model)

        # Quota ceiling enforcement is not a "decision": it applies
        # immediately, bypassing cooldowns, because overshooting the quota is
        # worse than flapping.
        if current > hi:
            decisions[model] = hi
            continue

        sample = metrics.get(model)
        if not isinstance(sample, dict):
            continue  # no signal -> no action, never scale on a guess

        queue_depth = sample.get("queue_depth", 0)
        try:
            queue_depth = int(queue_depth)
        except (TypeError, ValueError):
            queue_depth = 0
        ttft = sample.get("ttft_p99_s")
        try:
            ttft = float(ttft) if ttft is not None else None
        except (TypeError, ValueError):
            ttft = None

        target_ttft = _float_field(policy, "target_ttft_s", DEFAULT_TARGET_TTFT_S)
        queue_threshold = _int_field(
            policy, "scale_up_queue_depth", DEFAULT_SCALE_UP_QUEUE_DEPTH)

        last = last_scales.get(model)
        try:
            last = float(last) if last is not None else None
        except (TypeError, ValueError):
            last = None

        new = current
        if queue_depth > queue_threshold or (ttft is not None and ttft > target_ttft):
            cooldown = _float_field(
                policy, "scale_up_cooldown_s", DEFAULT_SCALE_UP_COOLDOWN_S)
            if current < hi and (last is None or now - last >= cooldown):
                new = min(current + 1, hi)
        elif (queue_depth == 0 and ttft is not None and ttft <= target_ttft
                and current > lo):
            cooldown = _float_field(
                policy, "scale_down_cooldown_s", DEFAULT_SCALE_DOWN_COOLDOWN_S)
            if last is None or now - last >= cooldown:
                new = current - 1

        if new != current:
            decisions[model] = new
    return decisions


# --- quota guardrails (chantier 1 integration) -----------------------------------
def maybe_quota_scopes(executor: Any = None) -> Optional[Any]:
    """Best-effort access to chantier 1's ``QuotaScopes``.

    Uses ``open_quota_scopes`` so file-mode deployments (no control store)
    yield ``None`` instead of a broken handle. Returns ``None`` when the
    module is unavailable or the store cannot be opened — callers then fall
    back to the policy's own ``max_replicas`` and unweighted routing.
    """
    try:
        from services.control_store.quota_scopes import open_quota_scopes
    except ImportError:
        return None
    try:
        return open_quota_scopes(executor=executor)
    except Exception as exc:
        _LOG.warning("quota_scopes_unavailable",
                     extra={"error": str(exc)[:200]})
        return None


def resolve_quota_headroom(
    policies: Dict[str, Dict[str, Any]],
    quota_scopes: Optional[Any] = None,
) -> Dict[str, int]:
    """``{model: hard replica ceiling}`` from team quota, else policy max.

    Chantier 1's quota model is token/budget based (``daily_tokens``,
    ``monthly_tokens``, ``rpm``, ``tpm``) — it carries no replica count, so
    the ceiling is derived from admission: a team whose budget is exhausted
    (``check_budget("team", team_id, 0)`` denies) gets its headroom frozen at
    the *current* replica count — the autoscaler may not add replicas for a
    team that cannot pay for them, and the quota-aware routing weights drain
    traffic away from them. Every other case falls back to the policy's own
    ``max_replicas`` (itself a bound).

    Never raises for quota-store problems: an unreadable quota falls back to
    the policy max and the failure is logged. ``decide`` additionally clamps
    every result to this ceiling, so the autoscaler can never overshoot the
    quota it could read.
    """
    headroom: Dict[str, int] = {}
    for model, policy in policies.items():
        if not isinstance(policy, dict):
            continue
        policy_max = _int_field(policy, "max_replicas", DEFAULT_MAX_REPLICAS)
        ceiling = policy_max
        team_id = policy.get("team_id")
        if quota_scopes is not None and team_id:
            try:
                admitted, _info = quota_scopes.check_budget(
                    "team", str(team_id), 0)
                if not admitted:
                    # Budget exhausted: freeze, do not grow.
                    ceiling = _int_field(policy, "replicas", policy_max)
            except Exception as exc:
                _LOG.warning("quota_headroom_fallback",
                             extra={"model": model, "team_id": team_id,
                                    "error": str(exc)[:200]})
                ceiling = policy_max
        headroom[model] = max(0, ceiling)
    return headroom


def build_team_state(
    policies: Dict[str, Dict[str, Any]],
    quota_scopes: Optional[Any] = None,
) -> Dict[str, Dict[str, Any]]:
    """``{model: {"team_id", "remaining_ratio", "exhausted"}}`` per policy team.

    Delegates to chantier 1's ``distribution.team_state_for_models`` — the
    exact input ``distribution.quota_weights`` expects. Empty when the quota
    store (or the distribution module) is unavailable: routing then stays
    neutral instead of penalising what it cannot see.
    """
    if quota_scopes is None:
        return {}
    try:
        from services.fleet.distribution import team_state_for_models
    except ImportError:
        return {}
    try:
        state = team_state_for_models(policies, quota_scopes)
    except Exception as exc:
        _LOG.warning("team_state_unavailable",
                     extra={"error": str(exc)[:200]})
        return {}
    return state if isinstance(state, dict) else {}


# --- metrics intake (observability collector, best-effort) -----------------------
def read_fleet_metrics() -> Dict[str, Dict[str, Any]]:
    """Best-effort read of per-model serving metrics.

    Returns ``{model: {"queue_depth": int, "ttft_p99_s": float,
    "latency_p99_s": float}}``. The observability collector is expected to
    expose ``latest_fleet_metrics()`` (see ``docs/fleet-autoscaling.md`` for
    the required ``observability_fleet_*`` series); until it does, or when the
    collector is unreachable, this returns ``{}`` and the autoscaler takes no
    action — it never scales on stale or guessed signals.
    """
    try:
        from services.observability import collector as _collector
    except Exception:
        return {}
    accessor = getattr(_collector, "latest_fleet_metrics", None)
    if accessor is None:
        return {}
    try:
        metrics = accessor()
    except Exception as exc:
        _LOG.warning("fleet_metrics_unavailable",
                     extra={"error": str(exc)[:200]})
        return {}
    if not isinstance(metrics, dict):
        return {}
    return {model: sample for model, sample in metrics.items()
            if isinstance(sample, dict)}


def invert_assignments(
    assignments: Dict[str, Any],
) -> Dict[str, list]:
    """``{node: [assignment, ...]}`` -> ``{model: [{"node": node}, ...]}``.

    The per-model form is exactly what ``distribution.quota_weights``
    consumes for routing weights.
    """
    per_model: Dict[str, list] = {}
    for node, items in (assignments or {}).items():
        for item in items or []:
            model = item.get("model") if isinstance(item, dict) else None
            if model:
                per_model.setdefault(model, []).append({"node": node})
    return per_model


def compute_routing_weights(
    assignments_per_model: Dict[str, list],
    team_state: Dict[str, Dict[str, Any]],
) -> Optional[Dict[str, list]]:
    """Quota-aware routing weights via chantier 1's ``distribution.quota_weights``.

    Returns ``None`` when the ``distribution`` module is not available —
    the daemon then calls ``sync_from_fleet`` without ``routing_weights``.
    """
    if not assignments_per_model:
        return None
    try:
        from services.fleet.distribution import quota_weights
    except ImportError:
        return None
    try:
        weights = quota_weights(assignments_per_model, team_state)
    except Exception as exc:
        _LOG.warning("routing_weights_failed",
                     extra={"error": str(exc)[:200]})
        return None
    return weights if isinstance(weights, dict) else None


def current_epoch() -> float:
    return time.time()
