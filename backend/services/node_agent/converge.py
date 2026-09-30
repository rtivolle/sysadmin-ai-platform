"""Desired-state convergence for the node agent (kubelet-lite).

The platform decides placement; this node only executes. `converge()` takes a
desired-state mapping of the form::

    {
        "qwen2.5-coder:32b": {"action": "start", "params": {"gpu_memory_utilization": 0.85}},
        "old-model": {"action": "stop"},
    }

For each model it: pulls missing weights via `downloader`, starts/stops via
`vllm_server`/`llamacpp_server`, and updates the local `registry`. Failures
are recorded per model — a failing model never blocks the others — and the
full result map is returned to the caller (heartbeat reporter).

Engine functions are module-level and replaceable so tests can converge
without a GPU, a network or real weights:

- `download_fn(name, store, snapshot_fn=None, file_download_fn=None)`
- `start_fn(name, store)`
- `stop_fn(name, store)`

Params handling: only registry load parameters for the model's engine may be
set through the desired state, and they go through `registry.update` (which
strips anything outside `ALLOWED_FIELDS`). The engine binary remains the final
authority and reports errors at start.
"""
import logging
from typing import Any, Callable, Dict, Optional

from services.model_manager import downloader, llamacpp_server, registry as registry_module, vllm_server
from services.logging_setup import get_logger, log_event

_LOG = get_logger("node_agent.converge")

ACTION_START = "start"
ACTION_STOP = "stop"

# Identity/source fields may never arrive through the desired state; they are
# set once at registration by the local admin API.
_IDENTITY_FIELDS = frozenset({"name", "hf_repo", "revision", "engine", "gguf_file", "path"})


def _engine_for(entry: Dict[str, Any]):
    engine = registry_module.validate_engine(entry.get("engine", registry_module.ENGINE_VLLM))
    return llamacpp_server if engine == registry_module.ENGINE_LLAMACPP else vllm_server


def _is_running(entry: Dict[str, Any], store: registry_module.ModelRegistry) -> bool:
    server = _engine_for(entry)
    if server is llamacpp_server:
        return server.process_matches(entry, store)
    return server.is_running(entry)


def download_fn(
    name: str,
    store: registry_module.ModelRegistry,
    snapshot_fn: Optional[Callable[..., str]] = None,
    file_download_fn: Optional[Callable[..., str]] = None,
) -> Dict[str, Any]:
    return downloader.download_sync(name, store, snapshot_fn=snapshot_fn, file_download_fn=file_download_fn)


def start_fn(name: str, store: registry_module.ModelRegistry) -> Dict[str, Any]:
    entry = store.get(name) or {}
    return _engine_for(entry).start(name, store)


def stop_fn(name: str, store: registry_module.ModelRegistry) -> Dict[str, Any]:
    entry = store.get(name) or {}
    return _engine_for(entry).stop(name, store)


def _apply_params(name: str, store: registry_module.ModelRegistry, params: Any) -> Dict[str, Any]:
    """Store bounded load parameters for the next start. Unknown or identity
    fields are dropped (never trusted from a remote desired state)."""
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    safe = {
        key: value for key, value in params.items()
        if key in registry_module.ALLOWED_FIELDS and key not in _IDENTITY_FIELDS
        and type(value) in (str, int, float, bool)
    }
    if not safe:
        return store.get(name) or {}
    return store.update(name, **safe) or {}


def _ensure_started(
    name: str,
    store: registry_module.ModelRegistry,
    params: Any,
    download: Callable[..., Dict[str, Any]],
    start: Callable[[str, registry_module.ModelRegistry], Dict[str, Any]],
    snapshot_fn: Optional[Callable[..., str]],
    file_download_fn: Optional[Callable[..., str]],
) -> Dict[str, Any]:
    entry = store.get(name)
    if entry is None:
        return {"model": name, "action": ACTION_START, "status": "error",
                "error": f"unknown model '{name}' (register it first)"}
    _apply_params(name, store, params)
    entry = store.get(name) or {}
    if entry.get("status") not in (registry_module.STATUS_DOWNLOADED,
                                   registry_module.STATUS_STOPPED,
                                   registry_module.STATUS_RUNNING,
                                   registry_module.STATUS_STARTING):
        try:
            download(name, store, snapshot_fn=snapshot_fn, file_download_fn=file_download_fn)
        except Exception as exc:
            log_event(_LOG, "converge_download_failed", f"download failed for '{name}'",
                      fields={"model": name, "error": str(exc)[:200]}, level=logging.WARNING)
            return {"model": name, "action": ACTION_START, "status": "error",
                    "error": f"download failed: {exc}"}
        entry = store.get(name) or {}
    if _is_running(entry, store):
        return {"model": name, "action": ACTION_START, "status": "already_running",
                "port": (entry.get("server") or {}).get("port")}
    try:
        entry = start(name, store)
    except Exception as exc:
        log_event(_LOG, "converge_start_failed", f"start failed for '{name}'",
                  fields={"model": name, "error": str(exc)[:200]}, level=logging.WARNING)
        return {"model": name, "action": ACTION_START, "status": "error",
                "error": f"start failed: {exc}"}
    log_event(_LOG, "converge_started", f"model '{name}' started",
              fields={"model": name, "port": (entry.get("server") or {}).get("port")})
    return {"model": name, "action": ACTION_START, "status": "started",
            "port": (entry.get("server") or {}).get("port")}


def _ensure_stopped(
    name: str,
    store: registry_module.ModelRegistry,
    stop: Callable[[str, registry_module.ModelRegistry], Dict[str, Any]],
) -> Dict[str, Any]:
    entry = store.get(name)
    if entry is None:
        # Converging a stop for an unknown model is already the desired state.
        return {"model": name, "action": ACTION_STOP, "status": "noop"}
    if not _is_running(entry, store):
        return {"model": name, "action": ACTION_STOP, "status": "already_stopped"}
    try:
        stop(name, store)
    except Exception as exc:
        log_event(_LOG, "converge_stop_failed", f"stop failed for '{name}'",
                  fields={"model": name, "error": str(exc)[:200]}, level=logging.WARNING)
        return {"model": name, "action": ACTION_STOP, "status": "error",
                "error": f"stop failed: {exc}"}
    log_event(_LOG, "converge_stopped", f"model '{name}' stopped", fields={"model": name})
    return {"model": name, "action": ACTION_STOP, "status": "stopped"}


def converge(
    desired: Dict[str, Dict[str, Any]],
    store: registry_module.ModelRegistry,
    download: Optional[Callable[..., Dict[str, Any]]] = None,
    start: Optional[Callable[[str, registry_module.ModelRegistry], Dict[str, Any]]] = None,
    stop: Optional[Callable[[str, registry_module.ModelRegistry], Dict[str, Any]]] = None,
    snapshot_fn: Optional[Callable[..., str]] = None,
    file_download_fn: Optional[Callable[..., str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Execute one desired-state delta. Returns a per-model result map.

    `desired` maps model name -> {"action": "start"|"stop", "params": {...}}.
    Every model is validated as a path-safe registry name; invalid names are
    reported as errors, never acted on.
    """
    download = download or download_fn
    start = start or start_fn
    stop = stop or stop_fn
    results: Dict[str, Dict[str, Any]] = {}
    for raw_name, spec in (desired or {}).items():
        try:
            name = registry_module.validate_name(raw_name)
        except (ValueError, TypeError) as exc:
            results[str(raw_name)] = {"model": str(raw_name), "action": None,
                                      "status": "error", "error": str(exc)}
            continue
        spec = spec if isinstance(spec, dict) else {}
        action = spec.get("action")
        if action == ACTION_START:
            results[name] = _ensure_started(name, store, spec.get("params"), download,
                                            start, snapshot_fn, file_download_fn)
        elif action == ACTION_STOP:
            results[name] = _ensure_stopped(name, store, stop)
        else:
            results[name] = {"model": name, "action": action, "status": "error",
                             "error": f"unknown action {action!r} (want 'start' or 'stop')"}
    return results
