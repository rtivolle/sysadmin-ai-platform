#!/usr/bin/env python3
"""Fleet control loop (platform side): the litellm_sync daemon.

Every ``FLEET_SYNC_INTERVAL_S`` seconds (default 10):

1. sweep stale nodes (3 missed heartbeats),
2. read declarative policies (``fleet_desired_state``) and healthy nodes,
3. schedule placements (bin-packing on free VRAM + ``gpu_class``),
4. push each node's desired-state delta through its heartbeat endpoint
   (mTLS), collecting actual state,
5. record placements (desired vs actual) in the registry,
6. regenerate LiteLLM's ``model_list`` from healthy (model × node) pairs via
   ``litellm_sync.sync_from_fleet`` — which restarts LiteLLM through
   ``platform.sh`` only when the generated block actually changed.

The loop is built around ``sync_once()`` with injectable ``heartbeat_fn`` /
``sync_fn`` / ``run_fn`` so the whole control cycle is testable without
nodes, LiteLLM or a database. Fail-closed: with no durable store configured
nothing is synced — the outage is logged loudly, never papered over.

Graceful shutdown on SIGTERM/SIGINT.
"""
import logging
import os
import signal
import sys
import time
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

from .scheduler import compute_desired_state, shortfall, to_node_desired

_LOG = get_logger("fleet.litellm_daemon")

SYNC_INTERVAL_S = float(os.getenv("FLEET_SYNC_INTERVAL_S", "10"))
STALE_AFTER_S = 30.0
NODE_AGENT_PORT = int(os.getenv("FLEET_NODE_PORT", "8001"))

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


def sync_once(
    registry: FleetRegistry,
    *,
    heartbeat_fn: Optional[Callable[[str, str, Dict[str, Any], int], Dict[str, Any]]] = None,
    sync_fn: Optional[Callable[[List[Tuple[str, str]]], Dict[str, Any]]] = None,
    run_fn: Optional[Callable[[list, str], Any]] = None,
    platform_sh: Optional[str] = None,
) -> Dict[str, Any]:
    """Run one full control cycle. Returns a summary dict."""
    heartbeat_fn = heartbeat_fn or heartbeat_node
    if sync_fn is None:
        def sync_fn(placements, _run_fn=run_fn, _platform_sh=platform_sh):
            return litellm_sync.sync_from_fleet(
                placements, restart=True, run_fn=_run_fn, platform_sh=_platform_sh)

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

    # Models the registry still wants running on a node but the scheduler no
    # longer assigns there must be stopped explicitly.
    previously_running: Dict[str, set] = {}
    for placement in placements:
        if placement.get("desired_state") == "running":
            previously_running.setdefault(placement["node_name"], set()).add(placement["model_name"])

    version = int(time.time())
    litellm_placements: List[Tuple[str, str]] = []
    node_errors: Dict[str, str] = {}
    for node in nodes:
        name = node["name"]
        address = node.get("address")
        node_desired = to_node_desired(assignments.get(name, []))
        for model in previously_running.get(name, set()):
            if model not in node_desired:
                node_desired[model] = {"action": "stop", "params": {}}
        actual_models: Dict[str, str] = {}
        if not address:
            node_errors[name] = "no address recorded"
            continue
        try:
            reply = heartbeat_fn(name, address, node_desired, version)
        except Exception as exc:
            node_errors[name] = str(exc)[:200]
            log_event(_LOG, "node_heartbeat_failed", f"heartbeat push failed for '{name}'",
                      fields={"node": name, "error": str(exc)[:200]}, level=logging.WARNING)
            continue
        for model in (reply.get("actual") or {}).get("models", []):
            if not isinstance(model, dict):
                continue
            model_name = model.get("name")
            running = bool(model.get("running"))
            actual_models[model_name] = "running" if running else "stopped"
            if running:
                litellm_placements.append((model_name, address))
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
