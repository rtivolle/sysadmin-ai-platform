#!/usr/bin/env python3
"""Fleet control loop (platform side): the litellm_sync daemon.

Every ``FLEET_SYNC_INTERVAL_S`` seconds (default 10):

1. sweep stale nodes (3 missed heartbeats),
2. read declarative policies (``fleet_desired_state``) and healthy nodes,
3. schedule placements (bin-packing on free VRAM + ``gpu_class``),
4. autoscale (when ``FLEET_AUTOSCALE_ENABLED``): read serving metrics, run
   ``autoscaler.decide`` and write adjusted ``replicas`` back through
   ``registry.set_desired_state`` — the change converges on the *next* cycle —
   then compute quota-aware routing weights for the LiteLLM sync,
5. push each node's desired-state delta through its heartbeat endpoint
   (mTLS) concurrently, collecting actual state — a successful push IS the
   node's registry heartbeat (recorded so ``mark_stale`` cannot evict live
   nodes),
6. record placements (desired vs actual) in the registry,
7. regenerate LiteLLM's ``model_list`` from healthy (model × node) pairs via
   ``litellm_sync.sync_from_fleet`` — which restarts LiteLLM through
   ``platform.sh`` only when the generated block actually changed, and at
   most once per anti-flap cooldown window.

The loop is built around ``sync_once()`` with injectable ``heartbeat_fn`` /
``sync_fn`` / ``run_fn`` / ``metrics_fn`` so the whole control cycle is
testable without nodes, LiteLLM or a database. Fail-closed: with no durable
store configured nothing is synced — the outage is logged loudly, never
papered over. Autoscaling additionally fails safe: with no metrics it takes
no action, and it never scales past the quota headroom.

Graceful shutdown on SIGTERM/SIGINT.
"""
import logging
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from services.control_store import open_fleet_registry
from services.control_store.fleet_registry import FleetRegistry
from services.logging_setup import configure, get_logger, log_event
from services.model_manager import litellm_sync

from . import autoscaler
from .scheduler import compute_desired_state, shortfall, to_node_desired

_LOG = get_logger("fleet.litellm_daemon")

SYNC_INTERVAL_S = float(os.getenv("FLEET_SYNC_INTERVAL_S", "10"))
STALE_AFTER_S = 30.0
NODE_AGENT_PORT = int(os.getenv("FLEET_NODE_PORT", "8001"))


def _heartbeat_workers() -> int:
    """Concurrency for the per-node heartbeat push; override with
    ``FLEET_HEARTBEAT_WORKERS``."""
    try:
        return max(1, int(os.getenv("FLEET_HEARTBEAT_WORKERS", "8")))
    except (TypeError, ValueError):
        return 8

_shutdown = False


def _handle_signal(signum, _frame):
    global _shutdown
    _shutdown = True
    log_event(_LOG, "shutdown_requested", f"received signal {signum}; finishing the current cycle",
              fields={"signal": signum})


def _node_client() -> httpx.Client:
    ca = os.getenv("FLEET_NODE_CA")
    cert = os.getenv("FLEET_PLATFORM_CERT")
    key = os.getenv("FLEET_PLATFORM_KEY")
    missing = [name for name, value in
               (("FLEET_NODE_CA", ca), ("FLEET_PLATFORM_CERT", cert), ("FLEET_PLATFORM_KEY", key))
               if not value]
    if missing:
        raise RuntimeError(f"fleet mTLS not configured (missing: {', '.join(missing)})")
    return httpx.Client(verify=ca, cert=(cert, key), timeout=30.0)


def heartbeat_node(
    node_name: str,
    address: str,
    desired: Dict[str, Dict[str, Any]],
    version: int,
    client_fn: Callable[[], httpx.Client] = _node_client,
) -> Dict[str, Any]:
    """Push a desired-state delta to one node-agent; return its heartbeat reply."""
    url = f"https://{address}:{NODE_AGENT_PORT}/api/v1/fleet/nodes/{node_name}/heartbeat"
    with client_fn() as client:
        response = client.post(url, json={
            "node_name": node_name,
            "desired_state": desired,
            "desired_version": version,
        })
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("node-agent heartbeat reply was not a JSON object")
    return data


def _free_vram(nodes: List[Dict[str, Any]], policies: Dict[str, Dict[str, Any]],
               placements: List[Dict[str, Any]]) -> Dict[str, float]:
    """Free VRAM per node = total minus what is currently desired-running.

    The registry does not track live VRAM; this reconstructs it from the last
    recorded desired placements, which is exactly what the scheduler needs to
    avoid double-booking a node between cycles.
    """
    free = {node["name"]: float(node.get("vram_total_gb") or 0) for node in nodes}
    for placement in placements:
        if placement.get("desired_state") != "running":
            continue
        policy = policies.get(placement["model_name"]) or {}
        try:
            need = float(policy.get("vram_per_replica_gb", 0) or 0)
        except (TypeError, ValueError):
            need = 0.0
        node = placement["node_name"]
        if node in free:
            free[node] = max(0.0, free[node] - need)
    return free


def _autoscale_enabled() -> bool:
    """Kill-switch for desired-state autoscaling; default on.

    Read per cycle (not at import) so tests and operators can flip it
    without restarting the import machinery.
    """
    return os.getenv("FLEET_AUTOSCALE_ENABLED", "1").strip().lower() in {
        "1", "true", "yes", "on"}


def _run_autoscale(
    registry: FleetRegistry,
    policies: Dict[str, Dict[str, Any]],
    assignments: Dict[str, List[Dict[str, Any]]],
    metrics_fn: Optional[Callable[[], Dict[str, Dict[str, Any]]]] = None,
) -> Tuple[Dict[str, Any], Optional[Dict[str, list]]]:
    """One autoscale pass: metrics -> decide -> write replicas -> weights.

    Returns ``(info, routing_weights)``. ``policies`` is updated in place for
    the models whose replica count changed (the registry write is the durable
    record; the scheduler converges on the next cycle). Raises on registry
    errors — the caller logs and continues the converge loop without the
    autoscale pass.
    """
    info: Dict[str, Any] = {"enabled": True, "decisions": {},
                            "metrics_models": 0, "routing_weights": False}
    metrics = (metrics_fn or autoscaler.read_fleet_metrics)()
    if not isinstance(metrics, dict):
        metrics = {}
    info["metrics_models"] = len(metrics)

    # Durable cooldown ledger. A stub registry (tests) has no executor —
    # cooldowns are then unenforced for that cycle, which is fine because
    # the decisions are still bounded by min/max/quota.
    executor = getattr(registry, "_executor", None)
    quota_scopes = autoscaler.maybe_quota_scopes(executor)
    headroom = autoscaler.resolve_quota_headroom(policies, quota_scopes)
    if executor is not None:
        autoscaler.ensure_schema(executor)
        last_scales = autoscaler.load_last_scales(executor)
    else:
        last_scales = {}

    decisions = autoscaler.decide(policies, metrics, headroom,
                                  time.time(), last_scales)

    set_desired_state = getattr(registry, "set_desired_state", None)
    if decisions and set_desired_state is None:
        # Fail safe: never scale when the new replica count cannot be
        # persisted — an in-memory-only change would desync the fleet.
        log_event(_LOG, "autoscale_no_write_path",
                  "autoscale decisions dropped: registry has no set_desired_state",
                  fields={"decisions": decisions}, level=logging.WARNING)
        info["dropped_no_write_path"] = True
        decisions = {}

    applied: Dict[str, int] = {}
    for model in sorted(decisions):
        new_replicas = decisions[model]
        policy = dict(policies.get(model) or {})
        old_replicas = policy.get("replicas")
        policy["replicas"] = new_replicas
        set_desired_state(model, policy)  # read-modify-write, keeps other fields
        policies[model] = policy
        applied[model] = new_replicas
        if executor is not None:
            try:
                old = int(old_replicas) if old_replicas is not None else new_replicas
            except (TypeError, ValueError):
                old = new_replicas
            if new_replicas > old:
                action = "scale_up"
            elif old > headroom.get(model, old):
                action = "quota_clamp"
            else:
                action = "scale_down"
            autoscaler.record_scale_event(executor, model, action, time.time())
    info["decisions"] = applied

    # Quota-aware routing weights for the LiteLLM sync (chantier 1's
    # distribution.quota_weights; None until that module lands).
    team_state = autoscaler.build_team_state(policies, quota_scopes)
    per_model = autoscaler.invert_assignments(assignments)
    routing_weights = autoscaler.compute_routing_weights(per_model, team_state)
    info["routing_weights"] = routing_weights is not None
    return info, routing_weights


def sync_once(
    registry: FleetRegistry,
    *,
    heartbeat_fn: Optional[Callable[[str, str, Dict[str, Any], int], Dict[str, Any]]] = None,
    sync_fn: Optional[Callable[[List[Tuple[str, str]]], Dict[str, Any]]] = None,
    run_fn: Optional[Callable[[list, str], Any]] = None,
    platform_sh: Optional[str] = None,
    metrics_fn: Optional[Callable[[], Dict[str, Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """Run one full control cycle. Returns a summary dict."""
    heartbeat_fn = heartbeat_fn or heartbeat_node

    stale = registry.mark_stale(STALE_AFTER_S)
    policies = registry.get_desired_state()
    nodes = registry.healthy_nodes(STALE_AFTER_S)
    placements = registry.list_placements()

    scheduler_nodes = []
    free = _free_vram(nodes, policies, placements)
    for node in nodes:
        scheduler_nodes.append({**node, "vram_free_gb": free.get(node["name"], 0.0)})
    assignments = compute_desired_state(policies, scheduler_nodes)
    missing = shortfall(policies, assignments)

    # Desired-state autoscaling: adjust policy replica counts from serving
    # signals, then hand quota-aware routing weights to the LiteLLM sync.
    # A failure here must not break the converge loop below.
    autoscale_info: Dict[str, Any] = {"enabled": _autoscale_enabled()}
    routing_weights: Optional[Dict[str, list]] = None
    if autoscale_info["enabled"]:
        try:
            autoscale_info, routing_weights = _run_autoscale(
                registry, policies, assignments, metrics_fn=metrics_fn)
        except Exception as exc:
            autoscale_info = {"enabled": True, "error": str(exc)[:200]}
            routing_weights = None
            log_event(_LOG, "autoscale_failed",
                      f"autoscale pass failed, continuing without it: {exc}",
                      fields={"error": str(exc)[:200]}, level=logging.WARNING)

    if sync_fn is None:
        # Canary policies ride on the desired-state policies (written by
        # model_manager.promotion); node_versions is the *observed* serving
        # version per node — None until node-agents report it in heartbeats
        # (lab qualification), so canary entries stay dormant meanwhile.
        canary_policies = {
            model: {"canary_version": policy.get("canary_version"),
                    "canary_traffic_percent": policy.get("canary_traffic_percent")}
            for model, policy in policies.items()
            if isinstance(policy, dict) and policy.get("canary_version")
            and (policy.get("canary_traffic_percent") or 0) > 0
        }

        def sync_fn(placements, _run_fn=run_fn, _platform_sh=platform_sh,
                    _weights=routing_weights, _canary=canary_policies):
            return litellm_sync.sync_from_fleet(
                placements, restart=True, routing_weights=_weights,
                canary_policies=_canary, run_fn=_run_fn, platform_sh=_platform_sh)

    # Models the registry still wants running on a node but the scheduler no
    # longer assigns there must be stopped explicitly.
    previously_running: Dict[str, set] = {}
    for placement in placements:
        if placement.get("desired_state") == "running":
            previously_running.setdefault(placement["node_name"], set()).add(placement["model_name"])

    version = int(time.time())
    litellm_placements: List[Tuple[str, str]] = []
    node_errors: Dict[str, str] = {}

    # Phase 1 — build each node's desired payload (pure, sequential).
    pending: List[Tuple[str, str, Dict[str, Dict[str, Any]]]] = []
    for node in nodes:
        name = node["name"]
        address = node.get("address")
        if not address:
            node_errors[name] = "no address recorded"
            continue
        node_desired = to_node_desired(assignments.get(name, []))
        for model in previously_running.get(name, set()):
            if model not in node_desired:
                node_desired[model] = {"action": "stop", "params": {}}
        pending.append((name, address, node_desired))

    # Phase 2 — push the heartbeat to every node concurrently. The old
    # sequential loop serialized the 30 s per-node timeout, so a single dead
    # node stretched a cycle into minutes; the cycle is now bounded by one
    # timeout no matter how many nodes flap.
    replies: Dict[str, Dict[str, Any]] = {}
    if pending:
        workers = min(_heartbeat_workers(), len(pending))
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="fleet-hb") as pool:
            future_to_name = {
                pool.submit(heartbeat_fn, name, address, desired, version): name
                for name, address, desired in pending
            }
            for future in as_completed(future_to_name):
                name = future_to_name[future]
                try:
                    reply = future.result()
                except Exception as exc:
                    node_errors[name] = str(exc)[:200]
                    log_event(_LOG, "node_heartbeat_failed",
                              f"heartbeat push failed for '{name}'",
                              fields={"node": name, "error": str(exc)[:200]},
                              level=logging.WARNING)
                    continue
                if not isinstance(reply, dict):
                    node_errors[name] = "node-agent heartbeat reply was not a JSON object"
                    continue
                replies[name] = reply

    # Phase 3 — process the replies sequentially so every registry write
    # stays single-threaded. A successful push IS the node's heartbeat: it
    # must be recorded, otherwise mark_stale() evicts every node 30 s after
    # approval and the fleet drains itself of traffic.
    for name, address, node_desired in pending:
        if name in node_errors:
            continue
        reply = replies.get(name)
        if reply is None:
            node_errors[name] = "no heartbeat reply recorded"
            continue
        actual_models: Dict[str, str] = {}
        for model in (reply.get("actual") or {}).get("models", []):
            if not isinstance(model, dict):
                continue
            model_name = model.get("name")
            running = bool(model.get("running"))
            actual_models[model_name] = "running" if running else "stopped"
            if running:
                litellm_placements.append((model_name, address))
        try:
            alive = registry.heartbeat(name, {"models": actual_models})
        except Exception as exc:
            node_errors[name] = f"heartbeat record failed: {exc}"[:200]
            log_event(_LOG, "node_heartbeat_record_failed",
                      f"could not record heartbeat for '{name}': {exc}",
                      fields={"node": name, "error": str(exc)[:200]},
                      level=logging.ERROR)
            continue
        if not alive:
            node_errors[name] = "node unknown to the registry"
            continue
        for model, spec in node_desired.items():
            registry.record_placement(model, name, {
                "desired": "running" if spec.get("action") == "start" else "stopped",
                "actual": actual_models.get(model),
            })

    sync_result = sync_fn(sorted(set(litellm_placements)))
    summary = {
        "nodes": len(nodes),
        "stale_marked": stale,
        "assignments": {name: [item["model"] for item in items]
                        for name, items in assignments.items()},
        "shortfall": missing,
        "autoscale": autoscale_info,
        "node_errors": node_errors,
        "litellm": {"changed": sync_result.get("changed"),
                    "models": sync_result.get("models")},
    }
    log_event(_LOG, "sync_cycle", "fleet sync cycle finished",
              fields={"nodes": len(nodes), "stale_marked": stale,
                      "shortfall": missing, "node_errors": len(node_errors),
                      "litellm_changed": sync_result.get("changed")})
    return summary


def main() -> int:
    """Poll the registry forever; exit 0 on SIGTERM/SIGINT, 2 on config error."""
    configure("fleet.litellm_daemon")
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, _handle_signal)
    log_event(_LOG, "daemon_start", "fleet litellm_sync daemon starting",
              fields={"interval_s": SYNC_INTERVAL_S})
    while not _shutdown:
        cycle_start = time.monotonic()
        try:
            registry = open_fleet_registry()
        except Exception as exc:
            registry = None
            log_event(_LOG, "registry_unavailable",
                      f"no fleet registry this cycle (fail-closed, nothing synced): {exc}",
                      level=logging.ERROR)
        if registry is not None:
            try:
                sync_once(registry)
            except Exception as exc:
                # One bad cycle must not kill the daemon; the next cycle
                # retries. The error is logged loudly.
                log_event(_LOG, "sync_cycle_failed", f"fleet sync cycle failed: {exc}",
                          fields={"error": str(exc)[:300]}, level=logging.ERROR,
                          exc_info=True)
        deadline = cycle_start + SYNC_INTERVAL_S
        while not _shutdown and time.monotonic() < deadline:
            time.sleep(min(0.5, deadline - time.monotonic()))
    log_event(_LOG, "daemon_stop", "fleet litellm_sync daemon stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
