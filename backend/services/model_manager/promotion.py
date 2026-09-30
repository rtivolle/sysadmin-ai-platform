"""Model promotion: staging -> canary -> prod rollouts, canary traffic, rollback.

This module is the only writer of the canary fields in the fleet desired
state (`canary_version`, `canary_traffic_percent`) and of the `version`
(prod) field. The fleet policy is always updated read-modify-write: every
field the scheduler or another agent wrote (replicas, engine, gpu_class,
...) survives a promotion.

Canary contract (shared with chantiers 2 and 4):

* promote to canary  -> policy gains ``canary_version`` (the version id) and
  ``canary_traffic_percent`` (0-100);
* promote to prod    -> the canary fields are cleared and ``version`` is set
  to the prod version; chantier 4 (`litellm_sync`) derives the
  ``{model}-canary`` LiteLLM entry from the canary fields while present;
* demote/rollback   -> the canary fields are cleared when they point at the
  moved version.

Rollback restores the version prod *before* the current one, found in the
registry promotion history. The history is append-only: every promote,
demote and rollback records an event, so a double rollback walks back
through earlier prod versions.
"""
import logging
from typing import Any, Dict, List, Optional

from services.control_store import open_fleet_registry
from services.control_store.fleet_registry import FleetRegistry
from services.logging_setup import get_logger, log_event

from . import registry as registry_module

_LOG = get_logger("model_manager")
CANARY_VERSION_FIELD = "canary_version"
CANARY_PERCENT_FIELD = "canary_traffic_percent"
PROD_VERSION_FIELD = "version"


def _registry(registry: Optional[registry_module.ModelRegistry]) -> registry_module.ModelRegistry:
    return registry if registry is not None else registry_module.model_registry


def _fleet(fleet: Optional[FleetRegistry]) -> FleetRegistry:
    """Resolve the fleet registry; raise when the control store is unavailable.

    Routers translate this to HTTP 503 (fail closed — never an in-memory
    fallback, per the control_store contract).
    """
    resolved = fleet if fleet is not None else open_fleet_registry()
    if resolved is None:
        raise RuntimeError("fleet registry unavailable (control store not configured)")
    return resolved


def _write_policy(
    fleet: FleetRegistry,
    model: str,
    mutate,
) -> Dict[str, Any]:
    """Read-modify-write of one model's fleet policy; no field is ever lost."""
    policies = fleet.get_desired_state()
    policy = dict(policies.get(model) or {})
    mutate(policy)
    fleet.set_desired_state(model, policy)
    return policy


def _clear_canary(policy: Dict[str, Any], version: Optional[str] = None) -> None:
    """Drop canary fields when they describe `version` (or unconditionally)."""
    if version is None or policy.get(CANARY_VERSION_FIELD) == version:
        policy.pop(CANARY_VERSION_FIELD, None)
        policy.pop(CANARY_PERCENT_FIELD, None)


def _record_history_event(
    store: registry_module.ModelRegistry,
    model: str,
    version: str,
    from_stage: str,
    to_stage: str,
    action: str,
) -> None:
    # set_stage already appended the transition event; tag the intent by
    # rewriting the last event (it is ours: we hold no lock here, but the
    # event was just written by our own set_stage call above).
    history = store.promotion_history(model)
    if history and history[-1].get("version") == version and history[-1].get("to_stage") == to_stage:
        history[-1]["action"] = action
        # Persist the tagged event through a raw entry update.
        entry = store.get(model) or {}
        stored = list(entry.get("promotion_history") or [])
        if stored:
            stored[-1] = history[-1]
            store.update(model, promotion_history=stored)


def promote(
    model: str,
    version: str,
    target_stage: str,
    canary_percent: int = 10,
    registry: Optional[registry_module.ModelRegistry] = None,
    fleet: Optional[FleetRegistry] = None,
) -> Dict[str, Any]:
    """Promote a version to `target_stage` and sync the fleet policy.

    `canary_percent` applies when `target_stage` is ``canary``; promoting to
    ``prod`` clears the canary fields and records the prod version.
    Invalid transitions raise ValueError (from the registry).
    """
    store = _registry(registry)
    fleet = _fleet(fleet)
    target_stage = registry_module.validate_stage(target_stage)
    if target_stage == registry_module.STAGE_CANARY:
        canary_percent = registry_module.validate_canary_percent(canary_percent)
    current = store.get_version(model, version)
    if current is None:
        raise ValueError(f"unknown version '{version}' of model '{model}'")
    from_stage = current.get("stage", registry_module.STAGE_STAGING)

    record = store.set_stage(model, version, target_stage)
    if target_stage == registry_module.STAGE_CANARY:
        # Keep the registry record consistent with the policy contract.
        record = store.set_canary_percent(model, version, canary_percent)

    def mutate(policy: Dict[str, Any]) -> None:
        if target_stage == registry_module.STAGE_CANARY:
            _clear_canary(policy)
            policy[CANARY_VERSION_FIELD] = version
            policy[CANARY_PERCENT_FIELD] = canary_percent
        elif target_stage == registry_module.STAGE_PROD:
            _clear_canary(policy)
            policy[PROD_VERSION_FIELD] = version
        else:  # archived: never leave a stale canary pointer behind
            _clear_canary(policy, version)

    _write_policy(fleet, model, mutate)
    _record_history_event(store, model, version, from_stage, target_stage, "promote")
    log_event(_LOG, "model_promoted", f"model '{model}' version '{version}' -> {target_stage}",
              fields={"model": model, "version": version, "from_stage": from_stage,
                      "to_stage": target_stage,
                      "canary_percent": canary_percent if target_stage == registry_module.STAGE_CANARY else None})
    return record


def set_canary_traffic(
    model: str,
    version: str,
    percent: int,
    registry: Optional[registry_module.ModelRegistry] = None,
    fleet: Optional[FleetRegistry] = None,
) -> Dict[str, Any]:
    """Adjust the traffic share of the canary version (0-100).

    The version must currently sit in the ``canary`` stage; the policy is
    updated read-modify-write and any other policy field is preserved.
    """
    store = _registry(registry)
    fleet = _fleet(fleet)
    percent = registry_module.validate_canary_percent(percent)
    record = store.set_canary_percent(model, version, percent)
    if record.get("stage") != registry_module.STAGE_CANARY:
        raise ValueError(
            f"version '{version}' of model '{model}' is not in canary stage "
            f"(stage={record.get('stage')})"
        )

    def mutate(policy: Dict[str, Any]) -> None:
        policy[CANARY_VERSION_FIELD] = version
        policy[CANARY_PERCENT_FIELD] = percent

    _write_policy(fleet, model, mutate)
    log_event(_LOG, "canary_traffic_updated",
              f"model '{model}' canary '{version}' traffic -> {percent}%",
              fields={"model": model, "version": version, "canary_percent": percent})
    return record


def _previous_prod_version(store: registry_module.ModelRegistry, model: str) -> Optional[str]:
    """Newest prod version in history that is not the current prod occupant."""
    current = store.active_version(model, registry_module.STAGE_PROD)
    current_version = current.get("version") if current else None
    for event in reversed(store.promotion_history(model)):
        if event.get("to_stage") == registry_module.STAGE_PROD \
                and event.get("version") != current_version:
            return event["version"]
    return None


def rollback(
    model: str,
    registry: Optional[registry_module.ModelRegistry] = None,
    fleet: Optional[FleetRegistry] = None,
) -> Dict[str, Any]:
    """Restore the previous prod version of a model.

    The current prod occupant is archived by the registry's single-occupant
    rule; the canary fields are cleared from the policy and the prod
    ``version`` field points at the restored version. A second rollback
    walks one step further back through the append-only history.
    """
    store = _registry(registry)
    fleet = _fleet(fleet)
    model = registry_module.validate_name(model)
    target = _previous_prod_version(store, model)
    if target is None:
        raise ValueError(f"model '{model}' has no previous production version to roll back to")
    current = store.active_version(model, registry_module.STAGE_PROD)
    from_stage = (current or {}).get("stage")
    record = store.set_stage(model, target, registry_module.STAGE_PROD)

    def mutate(policy: Dict[str, Any]) -> None:
        _clear_canary(policy)
        policy[PROD_VERSION_FIELD] = target

    _write_policy(fleet, model, mutate)
    _record_history_event(store, model, target, from_stage or "unknown",
                          registry_module.STAGE_PROD, "rollback")
    log_event(_LOG, "model_rollback", f"model '{model}' rolled back to '{target}'",
              fields={"model": model, "version": target,
                      "previous_prod": (current or {}).get("version")})
    return record


def demote_to_staging(
    model: str,
    version: str,
    registry: Optional[registry_module.ModelRegistry] = None,
    fleet: Optional[FleetRegistry] = None,
) -> Dict[str, Any]:
    """Emergency pull-back: move a version back to staging.

    A stale canary pointer is cleared from the policy; the prod ``version``
    field is left untouched unless the demoted version was the canary.
    """
    store = _registry(registry)
    fleet = _fleet(fleet)
    current = store.get_version(model, version)
    if current is None:
        raise ValueError(f"unknown version '{version}' of model '{model}'")
    from_stage = current.get("stage", registry_module.STAGE_STAGING)
    record = store.set_stage(model, version, registry_module.STAGE_STAGING)

    def mutate(policy: Dict[str, Any]) -> None:
        _clear_canary(policy, version)

    _write_policy(fleet, model, mutate)
    _record_history_event(store, model, version, from_stage,
                          registry_module.STAGE_STAGING, "demote")
    log_event(_LOG, "model_demoted", f"model '{model}' version '{version}' -> staging",
              fields={"model": model, "version": version, "from_stage": from_stage})
    return record


def promotion_summary(
    model: str,
    registry: Optional[registry_module.ModelRegistry] = None,
) -> Dict[str, Any]:
    """Operator view: versions per stage, current prod/canary, history tail."""
    store = _registry(registry)
    versions = store.list_versions(model)
    by_stage: Dict[str, List[str]] = {}
    for record in versions:
        by_stage.setdefault(record.get("stage", "?"), []).append(record["version"])
    prod = store.active_version(model, registry_module.STAGE_PROD)
    canary = store.active_version(model, registry_module.STAGE_CANARY)
    return {
        "model": model,
        "versions": versions,
        "by_stage": by_stage,
        "prod": (prod or {}).get("version"),
        "canary": (canary or {}).get("version"),
        "canary_percent": (canary or {}).get("canary_percent"),
        "history": store.promotion_history(model)[-20:],
    }
