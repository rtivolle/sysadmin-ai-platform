"""GPU cost tracking for the fleet (chargeback).

Pipeline:

1. **Accrual** — a periodic job (fleet control loop) calls
   :meth:`CostTracker.accrue` once per node per accounting interval with the
   node's GPU class and elapsed GPU-hours. The ledger row key is
   ``(node_name, day)``: repeated calls accumulate into the same row instead
   of exploding the table.

2. **Attribution** — :meth:`CostTracker.cost_summary` spreads each node's
   daily cost across the models it hosts:

   - token prorata when per-model token usage for the day is available
     (chantier 1's ``services.control_store.quota_scopes`` table
     ``quota_usage_daily``, via an injected ``token_usage_fn`` or a guarded
     import of that module);
   - replica-count prorata as the documented fallback.

   Model costs then roll up to teams through the ``team_id`` field of the
   ``fleet_desired_state`` policies.

3. **Serving** — ``backend/services/fleet/cost_router.py`` exposes the
   summaries over HTTP (admin only, fail-closed 503 without a store).

Cost model and its limits are documented in ``docs/fleet-cost-tracking.md``.
"""
import datetime
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from services.logging_setup import get_logger

_FLEET_LOG = get_logger("fleet.cost_tracker")

_REPO_ROOT = Path(__file__).resolve().parents[2]  # .../backend
_DEFAULT_PRICING_PATH = _REPO_ROOT / "config" / "fleet" / "gpu_pricing.yaml"
_ENV_PRICING_PATH = "FLEET_PRICING_PATH"
FALLBACK_USD_PER_HOUR = 1.50

_SCOPE_TYPES = frozenset({"model", "team", "node"})
_UNASSIGNED_TEAM = "unassigned"


def _normalize_gpu_class(value: Any) -> str:
    """'NVIDIA H100 80GB HBM3' -> 'nvidiah10080gbhbm3' for matching."""
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


class GpuPricing:
    """Lazy, tolerant loader for ``backend/config/fleet/gpu_pricing.yaml``.

    The file is read on first price lookup, never at import. A missing or
    invalid file never crashes: a warning is logged and the fallback price
    ``default`` (or :data:`FALLBACK_USD_PER_HOUR`) is returned.
    """

    def __init__(self, path: Optional[os.PathLike] = None):
        env = os.getenv(_ENV_PRICING_PATH)
        self._path = Path(path or env or _DEFAULT_PRICING_PATH)
        self._loaded = False
        self._prices: Dict[str, float] = {}
        self._aliases: Dict[str, str] = {}
        self._default = FALLBACK_USD_PER_HOUR

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            _FLEET_LOG.warning(
                "gpu pricing file not found (%s); using fallback price %.2f USD/h",
                self._path, self._default)
            return
        except OSError as exc:
            _FLEET_LOG.warning("gpu pricing file unreadable (%s: %s); using fallback",
                               self._path, exc)
            return
        try:
            import yaml  # local import: pricing must work even without PyYAML
            data = yaml.safe_load(text)
        except Exception as exc:  # ImportError, YAMLError, ...
            _FLEET_LOG.warning("gpu pricing file invalid (%s: %s); using fallback",
                               self._path, exc)
            return
        if not isinstance(data, dict):
            _FLEET_LOG.warning("gpu pricing file has no mapping (%s); using fallback",
                               self._path)
            return
        try:
            self._default = float(data.get("default", FALLBACK_USD_PER_HOUR))
        except (TypeError, ValueError):
            _FLEET_LOG.warning("gpu pricing 'default' is not a number; keeping %.2f",
                               self._default)
        prices = data.get("prices") or {}
        if isinstance(prices, dict):
            for key, value in prices.items():
                try:
                    self._prices[_normalize_gpu_class(key)] = float(value)
                except (TypeError, ValueError):
                    _FLEET_LOG.warning("gpu pricing: ignoring non-numeric price for %r", key)
        aliases = data.get("aliases") or {}
        if isinstance(aliases, dict):
            for key, names in aliases.items():
                if not isinstance(names, (list, tuple)):
                    continue
                for name in names:
                    self._aliases[_normalize_gpu_class(name)] = _normalize_gpu_class(key)

    def price_per_hour(self, gpu_class: Any) -> float:
        """USD/hour for a GPU class; fallback price when unknown."""
        self._ensure_loaded()
        norm = _normalize_gpu_class(gpu_class)
        if norm in self._prices:
            return self._prices[norm]
        if norm in self._aliases:
            return self._prices.get(self._aliases[norm], self._default)
        # Substring match on normalized names ("h10080gb" in "nvidiah10080gbhbm3").
        for key, price in self._prices.items():
            if key and key in norm:
                return price
        return self._default

    @property
    def path(self) -> Path:
        return self._path


# ---------------------------------------------------------------------------
# Token usage source (chantier 1)
# ---------------------------------------------------------------------------

def _quota_scopes_token_usage(
    day: datetime.date, model_names: List[str]
) -> Optional[Dict[str, int]]:
    """Per-model token usage from chantier 1's quota scopes, if available.

    Expected contract (documented for chantier 1): module
    ``services.control_store.quota_scopes`` exposing a callable
    ``daily_model_tokens(day, model_names)`` returning a mapping
    ``model_name -> tokens`` for the day. The import is guarded: when the
    module (or the callable) is absent, ``None`` is returned and the tracker
    falls back to replica-count prorata — never a crash.
    """
    try:
        from services.control_store import quota_scopes  # type: ignore
    except ImportError:
        return None
    fn = getattr(quota_scopes, "daily_model_tokens", None)
    if not callable(fn):
        _FLEET_LOG.warning(
            "quota_scopes present but has no daily_model_tokens(); "
            "falling back to replica prorata")
        return None
    try:
        result = fn(day, model_names)
    except Exception as exc:
        _FLEET_LOG.warning("quota_scopes.daily_model_tokens failed (%s); "
                           "falling back to replica prorata", exc)
        return None
    if not isinstance(result, Mapping):
        return None
    return {str(k): int(v) for k, v in result.items()}


_CREATE_LEDGER = """
CREATE TABLE IF NOT EXISTS gpu_cost_ledger (
    node_name TEXT NOT NULL,
    gpu_class TEXT NOT NULL DEFAULT '',
    day DATE NOT NULL,
    gpu_hours DOUBLE PRECISION NOT NULL DEFAULT 0,
    usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    PRIMARY KEY (node_name, day)
)
"""

_ACCRUE = """
INSERT INTO gpu_cost_ledger (node_name, gpu_class, day, gpu_hours, usd)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (node_name, day) DO UPDATE SET
    gpu_hours = gpu_cost_ledger.gpu_hours + EXCLUDED.gpu_hours,
    usd = gpu_cost_ledger.usd + EXCLUDED.usd,
    gpu_class = EXCLUDED.gpu_class
"""

_LIST_DAY = """
SELECT node_name, gpu_class, gpu_hours, usd FROM gpu_cost_ledger WHERE day = %s
"""


class CostTracker:
    """Accrues per-node GPU cost and attributes it to models and teams.

    ``executor`` is the narrow control-store Executor protocol (query /
    execute / transaction) — the tracker never touches ``schema.py`` and owns
    only the ``gpu_cost_ledger`` table. ``fleet_registry`` is optional: without
    it, ``node`` scopes still work but model/team attribution has no placement
    data and returns zeros with an explicit warning.
    """

    def __init__(
        self,
        executor: Any,
        pricing: Optional[GpuPricing] = None,
        fleet_registry: Any = None,
        token_usage_fn: Optional[Callable[[datetime.date, List[str]], Optional[Dict[str, int]]]] = None,
    ):
        self._executor = executor
        self._pricing = pricing or GpuPricing()
        self._registry = fleet_registry
        self._token_usage_fn = token_usage_fn or _quota_scopes_token_usage

    # -- schema --------------------------------------------------------------
    def ensure_schema(self) -> None:
        """Create ``gpu_cost_ledger`` if it does not exist. Safe to rerun."""
        self._executor.execute(_CREATE_LEDGER, ())

    # -- accrual --------------------------------------------------------------
    def accrue(
        self,
        node_name: str,
        gpu_class: str,
        hours: float,
        day: Optional[datetime.date] = None,
    ) -> float:
        """Add ``hours`` of GPU time for a node to the daily ledger.

        Idempotent by ``(node_name, day)``: the same row accumulates across
        calls (upsert-add). The caller owns interval discipline — re-accruing
        the same interval twice counts it twice; the key only prevents row
        explosion. Returns the USD accrued for this call.
        """
        node_name = str(node_name or "").strip()
        if not node_name:
            raise ValueError("node_name is required")
        hours = float(hours)
        if hours < 0:
            raise ValueError("hours must be non-negative")
        day = day or datetime.date.today()
        usd = hours * self._pricing.price_per_hour(gpu_class)
        self._executor.execute(
            _ACCRUE, (node_name, str(gpu_class or ""), day, hours, usd))
        return usd

    # -- attribution ----------------------------------------------------------
    def _day_rows(self, day: datetime.date) -> List[Tuple[str, str, float, float]]:
        return [
            (str(r[0]), str(r[1] or ""), float(r[2] or 0.0), float(r[3] or 0.0))
            for r in self._executor.query(_LIST_DAY, (day,))
        ]

    def _placements(self) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
        """Return (placements, model -> team_id). Empty when no registry."""
        if self._registry is None:
            return [], {}
        try:
            placements = self._registry.list_placements()
        except Exception as exc:
            _FLEET_LOG.warning("fleet registry placements unreadable (%s)", exc)
            placements = []
        try:
            policies = self._registry.get_desired_state() or {}
        except Exception as exc:
            _FLEET_LOG.warning("fleet desired state unreadable (%s)", exc)
            policies = {}
        teams = {}
        for model, policy in policies.items():
            team = (policy or {}).get("team_id") if isinstance(policy, dict) else None
            teams[str(model)] = str(team) if team else _UNASSIGNED_TEAM
        return placements, teams

    def cost_summary(
        self,
        scope_type: str,
        scope_id: str,
        day: Optional[datetime.date] = None,
    ) -> Dict[str, Any]:
        """Attribute the day's GPU cost to one model, team or node.

        Returns a dict with ``tokens``, ``gpu_hours`` and ``usd`` plus a
        ``breakdown`` of per-node contributions and the ``attribution``
        method used. Raises ``ValueError`` on an unknown scope type.
        """
        if scope_type not in _SCOPE_TYPES:
            raise ValueError(f"scope_type must be one of {sorted(_SCOPE_TYPES)}")
        scope_id = str(scope_id or "").strip()
        if not scope_id:
            raise ValueError("scope_id is required")
        day = day or datetime.date.today()

        rows = self._day_rows(day)
        placements, teams = self._placements()

        # node -> {model -> replica count}
        node_models: Dict[str, Dict[str, int]] = {}
        for placement in placements:
            node = str(placement.get("node_name") or "")
            model = str(placement.get("model_name") or "")
            if not node or not model:
                continue
            node_models.setdefault(node, {}).setdefault(model, 0)
            node_models[node][model] += 1

        all_models = sorted({m for models in node_models.values() for m in models})
        tokens = self._token_usage_fn(day, all_models) if all_models else None
        if tokens is not None:
            tokens = {m: max(0, int(tokens.get(m, 0))) for m in all_models}
        token_source = (
            "injected-or-quota_scopes" if tokens is not None else "none")

        summary: Dict[str, Any] = {
            "scope_type": scope_type,
            "scope_id": scope_id,
            "day": day.isoformat(),
            "tokens": 0,
            "gpu_hours": 0.0,
            "usd": 0.0,
            "attribution": "direct",
            "token_source": token_source,
            "breakdown": [],
        }
        if self._registry is None and scope_type in ("model", "team"):
            summary["warning"] = ("no fleet registry attached: model/team "
                                  "attribution needs placement data")
        for node_name, gpu_class, gpu_hours, node_usd in rows:
            models = node_models.get(node_name, {})
            node_entry = {
                "node_name": node_name,
                "gpu_class": gpu_class,
                "gpu_hours": gpu_hours,
                "usd": node_usd,
                "models": [],
            }
            if not models:
                # Node accrued cost but hosts nothing we know about: its cost
                # stays unattributed (visible at node scope only).
                shares: Dict[str, float] = {}
                method = "direct"
            else:
                shares, method = self._shares(models, tokens)
            for model, share in sorted(shares.items()):
                model_usd = node_usd * share
                model_tokens = (tokens or {}).get(model, 0)
                in_scope = (
                    (scope_type == "model" and model == scope_id)
                    or (scope_type == "team" and teams.get(model, _UNASSIGNED_TEAM) == scope_id)
                )
                node_entry["models"].append({
                    "model_name": model,
                    "team_id": teams.get(model, _UNASSIGNED_TEAM),
                    "share": share,
                    "tokens": model_tokens,
                    "replicas": models[model],
                    "usd": model_usd,
                })
                if scope_type == "node" and node_name == scope_id:
                    summary["gpu_hours"] += gpu_hours * share
                    summary["usd"] += model_usd
                    summary["tokens"] += model_tokens
                elif in_scope:
                    summary["gpu_hours"] += gpu_hours * share
                    summary["usd"] += model_usd
                    summary["tokens"] += model_tokens
                    if summary["attribution"] == "direct":
                        summary["attribution"] = method
            if scope_type == "node" and node_name == scope_id and not models:
                summary["gpu_hours"] += gpu_hours
                summary["usd"] += node_usd
            summary["breakdown"].append(node_entry)

        summary["gpu_hours"] = round(summary["gpu_hours"], 6)
        summary["usd"] = round(summary["usd"], 6)
        return summary

    @staticmethod
    def _shares(
        models: Dict[str, int],
        tokens: Optional[Dict[str, int]],
    ) -> Tuple[Dict[str, float], str]:
        """Split a node's cost across its models.

        Token prorata when at least one hosted model consumed tokens that day;
        otherwise replica-count prorata. Shares always sum to 1.
        """
        if tokens is not None:
            total_tokens = sum(tokens.get(m, 0) for m in models)
            if total_tokens > 0:
                return ({m: tokens.get(m, 0) / total_tokens for m in models},
                        "token_prorata")
        total_replicas = sum(models.values()) or 1
        return ({m: count / total_replicas for m, count in models.items()},
                "replica_prorata")
