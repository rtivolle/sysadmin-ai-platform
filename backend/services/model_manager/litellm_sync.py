"""Keep LiteLLM's config-file model_list in step with running local models.

This deployment runs LiteLLM from `config.yaml` with no database, so the
documented way to add a model is to edit the file. We own a clearly-marked block
of `model_list` entries (tagged `model_info.managed_by`) and rewrite just those;
hand-written entries such as `fast-model`/`heavy-model` are never touched.

Hot-reload qualification (litellm 1.103.1, the version `install.sh` installs):
there is **no** reliable hot-reload of the YAML `model_list` in config-only
mode. `POST /config/update` exists but requires a connected database
(`proxy_server.py` raises "No DB Connected" when `prisma_client is None`),
there is no SIGHUP handler, and no file watcher re-reads `model_list` — the
periodic APScheduler jobs only sync from the DB (`store_model_in_db=True`).
`reload_config()` therefore attempts the hot path only when explicitly enabled
(`hot_reload=True` / `LITELLM_HOT_RELOAD=1`, for lab qualification) and falls
back to the restart through `platform.sh` otherwise. The restart is the safe,
documented path; see `docs/litellm-reload-canary.md` for the full qualification.
"""
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

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
    hot_reload: bool = False,
    litellm_url: Optional[str] = None,
    master_key: Optional[str] = None,
    http_post: Optional[Callable[..., Any]] = None,
    verify_fn: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """Rewrite the managed model_list block and optionally restart LiteLLM.

    ``hot_reload=True`` routes the restart through ``reload_config()`` (hot
    attempt first, restart fallback); the default keeps the restart-only
    behaviour.
    """
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
        if hot_reload:
            result["restart"] = reload_config(
                path, hot_reload=True, run_fn=run_fn, platform_sh=platform_sh,
                litellm_url=litellm_url, master_key=master_key,
                http_post=http_post, verify_fn=verify_fn)
        else:
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


def _fleet_managed_entry(model_name: str, node_address: str,
                         weight: Optional[float] = None,
                         canary_of: Optional[str] = None,
                         canary_version: Optional[str] = None,
                         canary_traffic_percent: Optional[float] = None) -> Dict[str, Any]:
    scheme = fleet_inference_scheme()
    litellm_params: Dict[str, Any] = {
        "model": f"openai/{model_name}",
        "api_base": f"{scheme}://{node_address}:8000/v1",
        "api_key": "none",
    }
    # Weighted deployments are a real LiteLLM router feature (litellm_params
    # `weight`, honoured by the `simple-shuffle` routing strategy in litellm
    # 1.103.1 — `least-busy` ignores it; see docs/litellm-reload-canary.md).
    if weight is not None:
        litellm_params["weight"] = weight
    model_info: Dict[str, Any] = {"managed_by": FLEET_MANAGED_BY}
    if canary_of is not None:
        model_info.update({
            "canary_of": canary_of,
            "canary_version": canary_version,
            "canary_traffic_percent": canary_traffic_percent,
        })
    return {
        "model_name": model_name,
        "litellm_params": litellm_params,
        "model_info": model_info,
    }


def _is_fleet_managed(entry: Any) -> bool:
    return isinstance(entry, dict) and (entry.get("model_info") or {}).get("managed_by") == FLEET_MANAGED_BY


def _parse_routing_weights(routing_weights: Any) -> Dict[Tuple[str, str], float]:
    """Normalize ``{model: [{"node": str, "weight": float}]}`` to (model, node)
    -> weight. ``None`` means "no weights" (current behaviour). Weights must
    be finite numbers > 0; anything else raises ValueError (fail fast: a
    silent bad weight would skew production traffic). Entries that match no
    (model x node) placement are ignored — the fleet membership changes every
    sync cycle, so a weight for a drained node must not fail the sync.
    """
    if routing_weights is None:
        return {}
    if not isinstance(routing_weights, dict):
        raise ValueError("routing_weights must be {model: [{'node': str, 'weight': float}]}")
    parsed: Dict[Tuple[str, str], float] = {}
    for model, entries in routing_weights.items():
        model = registry_module.validate_name(model)
        if not isinstance(entries, list):
            raise ValueError(f"routing_weights[{model!r}] must be a list of "
                             "{'node': ..., 'weight': ...}")
        for item in entries:
            if not isinstance(item, dict):
                raise ValueError(f"routing_weights[{model!r}] entries must be dicts")
            node = validate_node_address(item.get("node"))
            weight = item.get("weight")
            if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                raise ValueError(f"routing_weights[{model!r}][{node!r}]: weight must be a number > 0")
            weight = float(weight)
            if not (weight > 0) or weight == float("inf") or weight != weight:
                raise ValueError(f"routing_weights[{model!r}][{node!r}]: weight must be a finite number > 0")
            parsed[(model, node)] = weight
    return parsed


def _parse_canary_policies(canary_policies: Any) -> Dict[str, Dict[str, Any]]:
    """Normalize ``{model: {"canary_version": str, "canary_traffic_percent": n}}``.

    A policy is *active* when ``canary_version`` is a non-empty string and
    ``0 < canary_traffic_percent <= 100``. ``percent == 0`` (or a missing
    version) simply disables the canary entry; out-of-range or non-numeric
    percents raise ValueError.
    """
    if canary_policies is None:
        return {}
    if not isinstance(canary_policies, dict):
        raise ValueError("canary_policies must be {model: {'canary_version': str, "
                         "'canary_traffic_percent': 0-100}}")
    parsed: Dict[str, Dict[str, Any]] = {}
    for model, policy in canary_policies.items():
        model = registry_module.validate_name(model)
        if not isinstance(policy, dict):
            raise ValueError(f"canary_policies[{model!r}] must be a dict")
        version = policy.get("canary_version")
        percent = policy.get("canary_traffic_percent")
        if not isinstance(version, str) or not version:
            continue  # no version -> no canary entry
        if percent is None:
            continue
        if isinstance(percent, bool) or not isinstance(percent, (int, float)):
            raise ValueError(f"canary_policies[{model!r}]: canary_traffic_percent must be a number")
        percent = float(percent)
        if percent <= 0:
            continue  # 0% (or negative) -> canary disabled
        if percent > 100:
            raise ValueError(f"canary_policies[{model!r}]: canary_traffic_percent must be <= 100")
        parsed[model] = {"canary_version": version, "canary_traffic_percent": percent}
    return parsed


def _parse_node_versions(node_versions: Any) -> Dict[str, Optional[str]]:
    """Normalize ``{node: version}``; ``None`` version means "unknown"."""
    if node_versions is None:
        return {}
    if not isinstance(node_versions, dict):
        raise ValueError("node_versions must be {node: version}")
    parsed: Dict[str, Optional[str]] = {}
    for node, version in node_versions.items():
        node = validate_node_address(node)
        if version is not None and not isinstance(version, str):
            raise ValueError(f"node_versions[{node!r}]: version must be a string or None")
        parsed[node] = version
    return parsed


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


# --- config reload (hot-reload attempt, restart fallback) --------------------
# Qualified against litellm 1.103.1: no hot-reload exists for the YAML
# model_list in config-only mode (`POST /config/update` needs a database,
# raises "No DB Connected" otherwise; no SIGHUP handler; no config file
# watcher). The hot path below is therefore OFF by default and only runs when
# explicitly enabled — it exists so the lab can qualify it against a real
# proxy (possibly DB-backed) without changing this module again. The restart
# through platform.sh stays the safe, documented fallback.
LITELLM_URL_DEFAULT = "http://127.0.0.1:4000"
LITELLM_HOT_RELOAD_ENV = "LITELLM_HOT_RELOAD"          # "1" enables the hot attempt
LITELLM_MASTER_KEY_ENV = "LITELLM_MASTER_KEY"          # injected by platform.sh at startup
LITELLM_URL_ENV = "LITELLM_URL"


def _default_http_post(url: str, body: Dict[str, Any], headers: Dict[str, str],
                       timeout_s: float) -> Any:
    """Minimal stdlib HTTP POST; returns an object with ``status_code``."""
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        status = response.status

    class _Resp:
        status_code = status

    return _Resp()


def _default_verify_litellm(url: str, master_key: str, timeout_s: float) -> bool:
    """Best-effort liveness check: the proxy answers /v1/models."""
    request = urllib.request.Request(
        f"{url.rstrip('/')}/v1/models",
        headers={"Authorization": f"Bearer {master_key}"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
        return response.status == 200 and isinstance(payload.get("data"), list)
    except Exception:
        return False


def _try_hot_reload(
    model_list: List[Dict[str, Any]],
    *,
    litellm_url: str,
    master_key: str,
    http_post=None,
    verify_fn=None,
    http_timeout_s: float = 10.0,
) -> Optional[Dict[str, Any]]:
    """Attempt `POST /config/update`; return the hot report or None.

    Returns None (caller falls back to restart) when the endpoint is
    unreachable, answers non-2xx, or the post-reload verification fails.
    Requires the proxy master key — never logged, only sent as a Bearer
    header on loopback. A missing key also returns None (no hot attempt).
    """
    if not master_key:
        return None
    post = http_post or _default_http_post
    try:
        response = post(
            f"{litellm_url.rstrip('/')}/config/update",
            {"model_list": model_list},
            {"Authorization": f"Bearer {master_key}",
             "Content-Type": "application/json"},
            http_timeout_s,
        )
        status = getattr(response, "status_code", None)
        if not isinstance(status, int) or not 200 <= status < 300:
            return None
    except Exception:
        return None
    verify = verify_fn or (lambda: _default_verify_litellm(litellm_url, master_key, http_timeout_s))
    try:
        if not verify():
            return None
    except Exception:
        return None
    return {"path": "hot", "endpoint": "/config/update", "status_code": status}


def reload_config(
    config_path: Optional[str] = None,
    *,
    hot_reload: Optional[bool] = None,
    run_fn: Optional[Callable[[list, str], Any]] = None,
    platform_sh: Optional[str] = None,
    litellm_url: Optional[str] = None,
    master_key: Optional[str] = None,
    http_post: Optional[Callable[..., Any]] = None,
    verify_fn: Optional[Callable[[], bool]] = None,
    http_timeout_s: float = 10.0,
) -> Dict[str, Any]:
    """Reload LiteLLM's running config, hot path first, restart as fallback.

    1. When hot reload is enabled (``hot_reload=True`` or
       ``LITELLM_HOT_RELOAD=1``) and a master key is available, POST the
       current on-disk ``model_list`` to ``/config/update`` and verify the
       proxy serves it. On success no restart happens.
    2. Otherwise — or when the hot attempt fails for any reason — restart
       LiteLLM through ``platform.sh`` (the existing, qualified path).

    Idempotent: posting an unchanged model_list is a no-op for the proxy and
    restarting twice in a row is harmless. Every dependency is injectable
    (``http_post``, ``verify_fn``, ``run_fn``) so the policy is unit-testable
    without a proxy.
    """
    path = config_path or default_config_path()
    with open(path, "r", encoding="utf-8") as handle:
        model_list = (yaml.safe_load(handle) or {}).get("model_list") or []

    hot_enabled = (hot_reload if hot_reload is not None
                   else os.getenv(LITELLM_HOT_RELOAD_ENV, "").strip() == "1")
    key = master_key if master_key is not None else os.getenv(LITELLM_MASTER_KEY_ENV)
    url = litellm_url or os.getenv(LITELLM_URL_ENV, LITELLM_URL_DEFAULT)

    hot_report: Optional[Dict[str, Any]] = None
    if hot_enabled and key:
        hot_report = _try_hot_reload(
            model_list, litellm_url=url, master_key=key,
            http_post=http_post, verify_fn=verify_fn, http_timeout_s=http_timeout_s)
    if hot_report is not None:
        return {"path": "hot", "hot": hot_report, "restart": None,
                "reason": "hot reload via /config/update succeeded"}
    restart_report = _restart_litellm(run_fn=run_fn, platform_sh=platform_sh)
    reason = ("hot reload disabled or unavailable (config-only proxy has no "
              "hot-reload; see module docstring)" if not hot_enabled or not key
              else "hot reload attempt failed; fell back to restart")
    return {"path": "restart", "hot": None, "restart": restart_report, "reason": reason}


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


def _maybe_hot_reload_litellm(
    changed: bool,
    *,
    cooldown_s: float,
    run_fn=None,
    platform_sh=None,
    litellm_url: Optional[str] = None,
    master_key: Optional[str] = None,
    http_post=None,
    verify_fn=None,
    http_timeout_s: float = 10.0,
) -> Any:
    """Hot-reload-aware variant of `_maybe_restart_litellm`.

    On a change, the hot path is attempted first (cheap, no downtime); the
    restart fallback — and only the fallback — honours the anti-flap
    cooldown. Semantics otherwise identical: restart report dict, the string
    ``"deferred"``, or None.
    """
    global _last_fleet_restart_ts, _fleet_restart_pending
    now = time.monotonic()
    if changed:
        hot_report = _try_hot_reload(
            _last_generated_model_list or [],
            litellm_url=litellm_url or os.getenv(LITELLM_URL_ENV, LITELLM_URL_DEFAULT),
            master_key=master_key if master_key is not None else os.getenv(LITELLM_MASTER_KEY_ENV),
            http_post=http_post, verify_fn=verify_fn, http_timeout_s=http_timeout_s)
        if hot_report is not None:
            return hot_report
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


# Last model_list generated by sync_from_fleet (module-level so
# _maybe_hot_reload_litellm can POST it without re-reading the file).
_last_generated_model_list: List[Dict[str, Any]] = []


def _canonical_entry(entry: Dict[str, Any]) -> str:
    return json.dumps(entry, sort_keys=True, default=str)


def sync_from_fleet(
    placements,
    config_path: Optional[str] = None,
    restart: bool = True,
    run_fn: Optional[Callable[[list, str], Any]] = None,
    platform_sh: Optional[str] = None,
    restart_cooldown_s: float = RESTART_COOLDOWN_DEFAULT_S,
    routing_weights: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    node_versions: Optional[Dict[str, Optional[str]]] = None,
    canary_policies: Optional[Dict[str, Dict[str, Any]]] = None,
    hot_reload: bool = False,
    litellm_url: Optional[str] = None,
    master_key: Optional[str] = None,
    http_post: Optional[Callable[..., Any]] = None,
    verify_fn: Optional[Callable[[], bool]] = None,
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

    ``routing_weights`` (``{model: [{"node": str, "weight": float}]}``) writes
    ``litellm_params.weight`` on the matching (model × node) deployments.
    ``None`` keeps the current behaviour (no ``weight`` key). Weights must be
    finite numbers > 0; entries matching no placement are ignored. NOTE: in
    litellm 1.103.1 the router only honours ``weight`` under the
    ``simple-shuffle`` routing strategy — the shipped config uses
    ``least-busy``, where weights are accepted but ignored. See
    ``docs/litellm-reload-canary.md``.

    ``canary_policies`` (``{model: {"canary_version": str,
    "canary_traffic_percent": 0-100}}``, from ``fleet_desired_state``) plus
    ``node_versions`` (``{node: version}``) generate, for each active policy,
    extra entries under ``model_name = "{model}-canary"`` pointing at the
    placed nodes that carry ``canary_version``. Those nodes are *excluded*
    from the plain ``{model}`` pool while the canary is active, so stable
    traffic never lands on the canary version by accident. The
    ``canary_traffic_percent`` itself is enforced upstream (the gateway/agent
    sends that share of requests to the ``{model}-canary`` name); LiteLLM only
    exposes both names. Canary metadata is recorded in
    ``model_info`` (``canary_of`` / ``canary_version`` /
    ``canary_traffic_percent``).

    ``hot_reload`` routes the restart through the hot-reload attempt first
    (see ``reload_config``); the restart fallback keeps the anti-flap
    cooldown. Off by default: the restart is the qualified path.
    """
    path = config_path or default_config_path()
    with open(path, "r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}

    weights = _parse_routing_weights(routing_weights)
    versions = _parse_node_versions(node_versions)
    canaries = _parse_canary_policies(canary_policies)

    seen = set()
    deduped: List[Tuple[str, str]] = []
    for model_name, node_address in placements or []:
        model_name = registry_module.validate_name(model_name)
        node_address = validate_node_address(node_address)
        key = (model_name, node_address)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(key)

    # Which (model, node) pairs are canary-only: the node carries the model's
    # canary version. Those pairs leave the stable pool for that model.
    canary_pairs = set()
    for model, policy in canaries.items():
        version = policy["canary_version"]
        for (model_name, node_address) in deduped:
            if model_name == model and versions.get(node_address) == version:
                canary_pairs.add((model_name, node_address))

    fleet_entries = []
    by_model: Dict[str, list] = {}
    canary_info: Dict[str, Dict[str, Any]] = {}
    for model_name, node_address in deduped:
        if (model_name, node_address) in canary_pairs:
            entry_model = f"{model_name}-canary"
            policy = canaries[model_name]
            entry = _fleet_managed_entry(
                entry_model, node_address,
                weight=weights.get((entry_model, node_address)),
                canary_of=model_name,
                canary_version=policy["canary_version"],
                canary_traffic_percent=policy["canary_traffic_percent"],
            )
            canary_info.setdefault(model_name, {
                "canary_version": policy["canary_version"],
                "canary_traffic_percent": policy["canary_traffic_percent"],
                "nodes": [],
            })["nodes"].append(node_address)
        else:
            entry_model = model_name
            entry = _fleet_managed_entry(
                model_name, node_address,
                weight=weights.get((model_name, node_address)),
            )
        fleet_entries.append(entry)
        by_model.setdefault(entry_model, []).append(node_address)
    fleet_entries.sort(key=lambda entry: (
        entry["model_name"], entry["litellm_params"]["api_base"]))

    entries = config.get("model_list") or []
    base = [entry for entry in entries if not _is_fleet_managed(entry)]
    new_entries = base + fleet_entries

    changed = ([_canonical_entry(entry) for entry in entries if _is_fleet_managed(entry)]
               != [_canonical_entry(entry) for entry in fleet_entries])
    if changed:
        config["model_list"] = new_entries
        _atomic_write_yaml(path, config)

    global _last_generated_model_list
    _last_generated_model_list = [dict(entry) for entry in fleet_entries]

    result: Dict[str, Any] = {
        "changed": changed, "models": by_model, "canary": canary_info, "restart": None}
    if restart:
        if hot_reload:
            result["restart"] = _maybe_hot_reload_litellm(
                changed, cooldown_s=restart_cooldown_s, run_fn=run_fn,
                platform_sh=platform_sh, litellm_url=litellm_url,
                master_key=master_key, http_post=http_post, verify_fn=verify_fn)
        else:
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
