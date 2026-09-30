"""Keep LiteLLM's config-file model_list in step with running local models.

This deployment runs LiteLLM from `config.yaml` with no database, so the
documented way to add a model is to edit the file. We own a clearly-marked block
of `model_list` entries (tagged `model_info.managed_by`) and rewrite just those;
hand-written entries such as `fast-model`/`heavy-model` are never touched. A
change requires a LiteLLM restart, which the caller triggers through
`platform.sh` — there is no config hot-reload in the config-only proxy.
"""
import os
import re
import subprocess
import tempfile
import time
from typing import Any, Callable, Dict, Optional

import yaml

from . import registry as registry_module

MANAGED_BY = "sysadmin-model-manager"
# Tag owned by the fleet manager (Phase A). Distinct from MANAGED_BY above:
# `sync()` owns local-model entries, `sync_from_fleet()` owns (model x node)
# entries; each function preserves the other's block untouched.
FLEET_MANAGED_BY = "sysadmin-fleet-manager"
INFERENCE_API_BASE = os.getenv("MODEL_INFERENCE_API_BASE", "http://127.0.0.1:8000/v1")


def default_config_path() -> str:
    return os.getenv(
        "LITELLM_CONFIG",
        os.path.abspath(os.path.join(os.path.dirname(__file__), "../../config/litellm/config.yaml")),
    )


def _default_run_fn(command: list, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, timeout=120)


def _managed_entry(name: str) -> Dict[str, Any]:
    return {
        "model_name": name,
        "litellm_params": {
            "model": f"openai/{name}",
            "api_base": INFERENCE_API_BASE,
            "api_key": "none",
        },
        "model_info": {"managed_by": MANAGED_BY},
    }


def _is_managed(entry: Any) -> bool:
    return isinstance(entry, dict) and (entry.get("model_info") or {}).get("managed_by") == MANAGED_BY


def _atomic_write_yaml(path: str, data: Dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    fd, temp_path = tempfile.mkstemp(prefix=".litellm-", suffix=".yaml", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(data, handle, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o644)
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def sync(
    store: registry_module.ModelRegistry,
    config_path: Optional[str] = None,
    restart: bool = True,
    run_fn: Optional[Callable[[list, str], Any]] = None,
    platform_sh: Optional[str] = None,
) -> Dict[str, Any]:
    """Rewrite the managed model_list block and optionally restart LiteLLM."""
    path = config_path or default_config_path()
    with open(path, "r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}

    entries = config.get("model_list") or []
    base = [entry for entry in entries if not _is_managed(entry)]
    running = [
        entry["name"] for entry in store.all()
        if entry.get("status") == registry_module.STATUS_RUNNING
    ]
    managed = [_managed_entry(name) for name in running]
    new_entries = base + managed

    changed = [entry.get("model_name") for entry in entries if _is_managed(entry)] != running
    if changed:
        config["model_list"] = new_entries
        _atomic_write_yaml(path, config)

    result: Dict[str, Any] = {"changed": changed, "models": running, "restart": None}
    if changed and restart:
        script = platform_sh or os.getenv(
            "SYSADMIN_PLATFORM_SH",
            os.path.abspath(os.path.join(os.path.dirname(__file__), "../../platform.sh")),
        )
        command = [script, "service", "litellm", "restart"]
        cwd = os.path.dirname(script)
        try:
            proc = (run_fn or _default_run_fn)(command, cwd)
            result["restart"] = {
                "exit_code": getattr(proc, "returncode", None),
                "output": (getattr(proc, "stdout", "") or "")[-4000:],
            }
        except Exception as exc:
            result["restart"] = {"exit_code": None, "error": str(exc)}
    return result


def managed_model_names(config_path: Optional[str] = None) -> list:
    path = config_path or default_config_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
    except OSError:
        return []
    return [entry.get("model_name") for entry in (config.get("model_list") or []) if _is_managed(entry)]


# --- fleet sync (Phase A: model_list generated from the fleet registry) -----

_NODE_ADDRESS_RE = re.compile(r"^[A-Za-z0-9._:-]{1,253}$")


def fleet_inference_scheme() -> str:
    """Scheme for LiteLLM -> node :8000 traffic; `https` (mTLS) by default."""
    scheme = os.getenv("FLEET_INFERENCE_SCHEME", "https").strip().lower()
    if scheme not in ("http", "https"):
        raise ValueError("FLEET_INFERENCE_SCHEME must be 'http' or 'https'")
    return scheme


def validate_node_address(address: Any) -> str:
    if not isinstance(address, str) or not _NODE_ADDRESS_RE.match(address):
        raise ValueError("node address must be a hostname or IP literal")
    return address


def _fleet_managed_entry(model_name: str, node_address: str) -> Dict[str, Any]:
    scheme = fleet_inference_scheme()
    return {
        "model_name": model_name,
        "litellm_params": {
            "model": f"openai/{model_name}",
            "api_base": f"{scheme}://{node_address}:8000/v1",
            "api_key": "none",
        },
        "model_info": {"managed_by": FLEET_MANAGED_BY},
    }


def _is_fleet_managed(entry: Any) -> bool:
    return isinstance(entry, dict) and (entry.get("model_info") or {}).get("managed_by") == FLEET_MANAGED_BY


def _restart_litellm(run_fn=None, platform_sh=None) -> Dict[str, Any]:
    script = platform_sh or os.getenv(
        "SYSADMIN_PLATFORM_SH",
        os.path.abspath(os.path.join(os.path.dirname(__file__), "../../platform.sh")),
    )
    command = [script, "service", "litellm", "restart"]
    cwd = os.path.dirname(script)
    try:
        proc = (run_fn or _default_run_fn)(command, cwd)
        return {
            "exit_code": getattr(proc, "returncode", None),
            "output": (getattr(proc, "stdout", "") or "")[-4000:],
        }
    except Exception as exc:
        return {"exit_code": None, "error": str(exc)}


# --- fleet restart anti-flap -------------------------------------------------
# A flapping node must not restart the LiteLLM proxy on every sync cycle
# (each restart is seconds of proxy downtime). Restarts are therefore
# rate-limited: at most one per ``restart_cooldown_s``. A restart that arrives
# inside the window is deferred, not dropped — it fires on the first later
# call once the window has elapsed and the config is stable.
#
# Module-level on purpose: the litellm_sync daemon is long-lived. A process
# restart conservatively resets the window and allows one immediate restart.
RESTART_COOLDOWN_DEFAULT_S = 90.0
_last_fleet_restart_ts: float = 0.0
_fleet_restart_pending: bool = False


def _reset_restart_tracking() -> None:
    """Test helper: clear the anti-flap restart state."""
    global _last_fleet_restart_ts, _fleet_restart_pending
    _last_fleet_restart_ts = 0.0
    _fleet_restart_pending = False


def _maybe_restart_litellm(
    changed: bool,
    *,
    cooldown_s: float,
    run_fn=None,
    platform_sh=None,
) -> Any:
    """Restart LiteLLM honouring the anti-flap cooldown.

    Returns the restart report dict, the string ``"deferred"`` when a needed
    restart is held back by the cooldown, or None when no restart was due.
    """
    global _last_fleet_restart_ts, _fleet_restart_pending
    now = time.monotonic()
    if changed:
        if now - _last_fleet_restart_ts >= cooldown_s:
            _last_fleet_restart_ts = now
            _fleet_restart_pending = False
            return _restart_litellm(run_fn=run_fn, platform_sh=platform_sh)
        _fleet_restart_pending = True
        return "deferred"
    if _fleet_restart_pending and now - _last_fleet_restart_ts >= cooldown_s:
        _last_fleet_restart_ts = now
        _fleet_restart_pending = False
        return _restart_litellm(run_fn=run_fn, platform_sh=platform_sh)
    return None


def sync_from_fleet(
    placements,
    config_path: Optional[str] = None,
    restart: bool = True,
    run_fn: Optional[Callable[[list, str], Any]] = None,
    platform_sh: Optional[str] = None,
    restart_cooldown_s: float = RESTART_COOLDOWN_DEFAULT_S,
) -> Dict[str, Any]:
    """Rewrite the fleet-managed model_list block from healthy placements.

    `placements` is an iterable of ``(model_name, node_address)`` pairs for
    healthy nodes (typically from ``FleetRegistry.healthy_nodes()`` joined
    with the scheduler's assignments). One LiteLLM entry is generated per
    (model × node) under the same ``model_name`` so the existing
    ``least-busy`` routing strategy spreads load across nodes.

    Only entries tagged ``model_info.managed_by = "sysadmin-fleet-manager"``
    are rewritten; hand-written entries and the local model manager's own
    block (``"sysadmin-model-manager"``) are preserved byte-for-byte.

    ``restart_cooldown_s`` rate-limits LiteLLM restarts (anti-flap): at most
    one restart per window; a restart that arrives inside the window is
    deferred until the config is stable, never dropped.
    """
    path = config_path or default_config_path()
    with open(path, "r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}

    seen = set()
    fleet_entries = []
    by_model: Dict[str, list] = {}
    for model_name, node_address in placements or []:
        model_name = registry_module.validate_name(model_name)
        node_address = validate_node_address(node_address)
        key = (model_name, node_address)
        if key in seen:
            continue
        seen.add(key)
        fleet_entries.append(_fleet_managed_entry(model_name, node_address))
        by_model.setdefault(model_name, []).append(node_address)
    fleet_entries.sort(key=lambda entry: (
        entry["model_name"], entry["litellm_params"]["api_base"]))

    entries = config.get("model_list") or []
    base = [entry for entry in entries if not _is_fleet_managed(entry)]
    new_entries = base + fleet_entries

    changed = ([(entry.get("model_name"), (entry.get("litellm_params") or {}).get("api_base"))
                for entry in entries if _is_fleet_managed(entry)]
               != [(entry["model_name"], entry["litellm_params"]["api_base"])
                   for entry in fleet_entries])
    if changed:
        config["model_list"] = new_entries
        _atomic_write_yaml(path, config)

    result: Dict[str, Any] = {"changed": changed, "models": by_model, "restart": None}
    if restart:
        result["restart"] = _maybe_restart_litellm(
            changed, cooldown_s=restart_cooldown_s, run_fn=run_fn, platform_sh=platform_sh)
    return result


def fleet_managed_model_names(config_path: Optional[str] = None) -> list:
    path = config_path or default_config_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
    except OSError:
        return []
    return [entry.get("model_name") for entry in (config.get("model_list") or []) if _is_fleet_managed(entry)]
