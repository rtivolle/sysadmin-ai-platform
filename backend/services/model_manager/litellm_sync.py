"""Keep LiteLLM's config-file model_list in step with running local models.

This deployment runs LiteLLM from `config.yaml` with no database, so the
documented way to add a model is to edit the file. We own a clearly-marked block
of `model_list` entries (tagged `model_info.managed_by`) and rewrite just those;
hand-written entries such as `fast-model`/`heavy-model` are never touched. A
change requires a LiteLLM restart, which the caller triggers through
`platform.sh` — there is no config hot-reload in the config-only proxy.
"""
import os
import subprocess
import tempfile
from typing import Any, Callable, Dict, Optional

import yaml

from . import registry as registry_module

MANAGED_BY = "sysadmin-model-manager"
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
