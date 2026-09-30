#!/usr/bin/env python3
"""Team / project quota scopes on the durable control store (PostgreSQL leg).

Per-user quotas live in Valkey (:class:`QuotaManager <services.auth_gateway.quota_manager>`:
in-flight, RPM, TPM, daily tokens). Team and project budgets are the fleet-level
counterpart: they bound *aggregate* consumption across every user sharing a
team or a project. Two tables, created by :meth:`QuotaScopes.ensure_schema`:

* ``quota_scopes(scope_type, scope_id, limits JSONB, updated_at)`` — the
  configured budgets;
* ``quota_usage_daily(scope_type, scope_id, day, tokens, requests, cost_usd)``
  — the durable per-day usage feeding usage summaries and chargeback.

Scope types are ``"user"``, ``"team"`` and ``"project"``. A scope with no
configured limits is *unlimited* (``check_budget`` admits); an explicit limit
is required to bound anything. This keeps the feature opt-in: existing
deployments behave exactly as before until an operator sets a team or project
budget.

Fail-closed, like every other durable-leg surface: the executor is required,
and a store outage raises :class:`ConnectionError` (surfaced as HTTP 503),
never a silent zero and never an unlimited budget.

Module-level imports are stdlib-only on purpose: opening the real store
(:func:`open_quota_scopes`) imports the control-store DSN helpers lazily so
that importing this module never pulls the fleet registry or any other
unrelated dependency.
"""
import datetime
import json
import os
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union
from zoneinfo import ZoneInfo

SCOPE_TYPES = frozenset({"user", "team", "project"})

# Bounds mirror the per-user QuotaManager limits, widened for aggregates.
LIMIT_BOUNDS = {
    "daily_tokens": (1, 1_000_000_000),
    "monthly_tokens": (1, 1_000_000_000_000),
    "rpm": (1, 100_000),
    "tpm": (1, 10_000_000),
}

_SCOPE_ID_RE = None  # compiled lazily to keep module import trivial


def _scope_id_re():
    global _SCOPE_ID_RE
    if _SCOPE_ID_RE is None:
        import re

        _SCOPE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    return _SCOPE_ID_RE


def validate_scope_type(scope_type: Any) -> str:
    if scope_type not in SCOPE_TYPES:
        raise ValueError(f"scope_type must be one of {sorted(SCOPE_TYPES)}")
    return scope_type


def validate_scope_id(scope_id: Any) -> str:
    if not isinstance(scope_id, str) or not _scope_id_re().match(scope_id):
        raise ValueError(
            "scope_id must be 1-128 chars: letters, digits, '.', '_' or '-', "
            "starting with a letter or digit"
        )
    return scope_id


def validate_limits(limits: Mapping[str, Any]) -> Dict[str, int]:
    """Validate a limits mapping; at least one known field is required."""
    if not isinstance(limits, Mapping) or not limits:
        raise ValueError("limits must be a non-empty mapping")
    unknown = set(limits) - set(LIMIT_BOUNDS)
    if unknown:
        raise ValueError(f"Unknown quota fields: {sorted(unknown)}")
    cleaned: Dict[str, int] = {}
    for field, value in limits.items():
        low, high = LIMIT_BOUNDS[field]
        # `type(value) is not int` rejects bools, like QuotaManager does.
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{field} must be an integer between {low} and {high}")
        cleaned[field] = value
    return cleaned


_CREATE_SCOPES = (
    "CREATE TABLE IF NOT EXISTS quota_scopes ("
    "scope_type TEXT NOT NULL, "
    "scope_id TEXT NOT NULL, "
    "limits JSONB NOT NULL, "
    "updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
    "PRIMARY KEY (scope_type, scope_id))"
)
_CREATE_USAGE = (
    "CREATE TABLE IF NOT EXISTS quota_usage_daily ("
    "scope_type TEXT NOT NULL, "
    "scope_id TEXT NOT NULL, "
    "day DATE NOT NULL, "
    "tokens BIGINT NOT NULL DEFAULT 0, "
    "requests BIGINT NOT NULL DEFAULT 0, "
    "cost_usd NUMERIC(18, 6) NOT NULL DEFAULT 0, "
    "PRIMARY KEY (scope_type, scope_id, day))"
)
_UPSERT_LIMITS = (
    "INSERT INTO quota_scopes (scope_type, scope_id, limits, updated_at) "
    "VALUES (%s, %s, %s::jsonb, now()) "
    "ON CONFLICT (scope_type, scope_id) DO UPDATE SET "
    "limits = EXCLUDED.limits, updated_at = now()"
)
_SELECT_LIMITS = "SELECT limits FROM quota_scopes WHERE scope_type = %s AND scope_id = %s"
_DELETE_LIMITS = "DELETE FROM quota_scopes WHERE scope_type = %s AND scope_id = %s"
_UPSERT_USAGE = (
    "INSERT INTO quota_usage_daily (scope_type, scope_id, day, tokens, requests, cost_usd) "
    "VALUES (%s, %s, %s, %s, 1, %s) "
    "ON CONFLICT (scope_type, scope_id, day) DO UPDATE SET "
    "tokens = quota_usage_daily.tokens + EXCLUDED.tokens, "
    "requests = quota_usage_daily.requests + 1, "
    "cost_usd = quota_usage_daily.cost_usd + EXCLUDED.cost_usd"
)
_SELECT_USAGE = (
    "SELECT tokens, requests, cost_usd FROM quota_usage_daily "
    "WHERE scope_type = %s AND scope_id = %s AND day = %s"
)
_SUM_MONTH = (
    "SELECT COALESCE(SUM(tokens), 0) FROM quota_usage_daily "
    "WHERE scope_type = %s AND scope_id = %s AND day >= %s"
)
_LIST_SCOPES = "SELECT scope_type, scope_id, limits FROM quota_scopes ORDER BY scope_type, scope_id"
_LIST_SCOPES_BY_TYPE = (
    "SELECT scope_type, scope_id, limits FROM quota_scopes "
    "WHERE scope_type = %s ORDER BY scope_id"
)
_CHARGEBACK = (
    "SELECT scope_type, scope_id, tokens, requests, cost_usd FROM quota_usage_daily "
    "WHERE day = %s ORDER BY cost_usd DESC, tokens DESC"
)


def _as_date(day: Union[str, datetime.date, None]) -> datetime.date:
    if day is None:
        return _today()
    if isinstance(day, datetime.date):
        return day
    return datetime.date.fromisoformat(str(day))


def _today() -> datetime.date:
    tz = ZoneInfo(os.getenv("QUOTA_TIMEZONE", "UTC"))
    return datetime.datetime.now(tz=tz).date()


def _seconds_until_next_midnight() -> int:
    tz = ZoneInfo(os.getenv("QUOTA_TIMEZONE", "UTC"))
    now = datetime.datetime.now(tz=tz)
    nxt = (now + datetime.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(1, int((nxt - now).total_seconds()))


def _seconds_until_next_month() -> int:
    tz = ZoneInfo(os.getenv("QUOTA_TIMEZONE", "UTC"))
    now = datetime.datetime.now(tz=tz)
    if now.month == 12:
        nxt = now.replace(year=now.year + 1, month=1, day=1,
                          hour=0, minute=0, second=0, microsecond=0)
    else:
        nxt = now.replace(month=now.month + 1, day=1,
                          hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((nxt - now).total_seconds()))


def _decode_limits(raw: Any) -> Dict[str, int]:
    if isinstance(raw, dict):
        return {str(k): int(v) for k, v in raw.items()}
    if isinstance(raw, (str, bytes)):
        decoded = json.loads(raw)
        if isinstance(decoded, dict):
            return {str(k): int(v) for k, v in decoded.items()}
    raise ValueError(f"Unparseable limits value: {raw!r}")


class QuotaScopes:
    """Durable team/project (/user) quota budgets and usage.

    ``executor`` follows the narrow Executor protocol (query / execute /
    transaction) so the whole class is unit-testable with a fake. ``None``
    is not accepted: construct nothing when no store is configured and let
    the caller fail closed instead.
    """

    # Default price used for chargeback estimates when a model has no
    # configured price. USD per million tokens. Chargeback figures are
    # operator estimates for showback, never billing-grade: set real prices
    # with set_model_price().
    DEFAULT_PRICE_PER_MTOK_USD = 0.0

    def __init__(self, executor, clock=None):
        if executor is None:
            raise ConnectionError(
                "QuotaScopes store unavailable (no durable store configured)"
            )
        self._executor = executor
        self._clock = clock or time.time
        self._model_prices: Dict[str, float] = {}

    # -- schema ---------------------------------------------------------
    def ensure_schema(self) -> None:
        """Create both tables idempotently. Never touches schema.py."""
        self._guarded(self._executor.execute, _CREATE_SCOPES, ())
        self._guarded(self._executor.execute, _CREATE_USAGE, ())

    # -- prices (chargeback estimates, operator config) ------------------
    def set_model_price(self, model: str, usd_per_mtok: float) -> None:
        price = float(usd_per_mtok)
        if price < 0:
            raise ValueError("usd_per_mtok must be non-negative")
        self._model_prices[str(model)] = price

    def model_price(self, model: Optional[str]) -> float:
        if model is None:
            return self.DEFAULT_PRICE_PER_MTOK_USD
        return self._model_prices.get(str(model), self.DEFAULT_PRICE_PER_MTOK_USD)

    # -- limits CRUD ------------------------------------------------------
    def set_limits(
        self, scope_type: str, scope_id: str, limits: Mapping[str, Any]
    ) -> Dict[str, int]:
        """Create or replace the budget for a scope. Returns the stored limits."""
        scope_type = validate_scope_type(scope_type)
        scope_id = validate_scope_id(scope_id)
        cleaned = validate_limits(limits)
        self._guarded(
            self._executor.execute, _UPSERT_LIMITS,
            (scope_type, scope_id, json.dumps(cleaned)),
        )
        return cleaned

    def get_limits(self, scope_type: str, scope_id: str) -> Optional[Dict[str, int]]:
        """The configured budget, or None when the scope has none (unlimited)."""
        scope_type = validate_scope_type(scope_type)
        scope_id = validate_scope_id(scope_id)
        rows = self._guarded(self._executor.query, _SELECT_LIMITS, (scope_type, scope_id))
        if not rows:
            return None
        return _decode_limits(rows[0][0])

    def delete_limits(self, scope_type: str, scope_id: str) -> bool:
        """Remove the budget; the scope becomes unlimited. True when one existed."""
        scope_type = validate_scope_type(scope_type)
        scope_id = validate_scope_id(scope_id)
        affected = self._guarded(
            self._executor.execute, _DELETE_LIMITS, (scope_type, scope_id)
        )
        return int(affected) > 0

    def list_scopes(self, scope_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Every scope with configured limits, optionally filtered by type."""
        if scope_type is None:
            rows = self._guarded(self._executor.query, _LIST_SCOPES, ())
        else:
            validate_scope_type(scope_type)
            rows = self._guarded(self._executor.query, _LIST_SCOPES_BY_TYPE, (scope_type,))
        return [
            {"scope_type": r[0], "scope_id": r[1], "limits": _decode_limits(r[2])}
            for r in rows
        ]

    # -- admission ----------------------------------------------------------
    def check_budget(
        self, scope_type: str, scope_id: str, tokens: int = 0
    ) -> Tuple[bool, Dict[str, Any]]:
        """Would ``tokens`` more fit inside the scope's budget?

        Returns ``(admitted, info)``. ``info`` carries ``used``, ``limit``,
        ``remaining`` and ``reset_in_seconds`` for the binding window so HTTP
        layers can answer 429 with a meaningful ``Retry-After``. A scope with
        no configured limits admits everything (``limited`` is False).
        """
        scope_type = validate_scope_type(scope_type)
        scope_id = validate_scope_id(scope_id)
        tokens = int(tokens)
        if tokens < 0:
            raise ValueError("tokens must be non-negative")
        limits = self.get_limits(scope_type, scope_id)
        base = {
            "scope_type": scope_type,
            "scope_id": scope_id,
            "limited": limits is not None,
            "window": None,
            "limit": None,
            "used": 0,
            "remaining": None,
            "reset_in_seconds": None,
        }
        if limits is None:
            return True, dict(base, ok=True)
        day = _today()
        windows = []
        if limits.get("daily_tokens") is not None:
            windows.append(("daily", "daily_tokens",
                            self._used_on(scope_type, scope_id, day),
                            _seconds_until_next_midnight()))
        if limits.get("monthly_tokens") is not None:
            windows.append(("monthly", "monthly_tokens",
                            self._used_since(scope_type, scope_id,
                                             day.replace(day=1)),
                            _seconds_until_next_month()))
        for window, key, used, reset_in in windows:
            limit = limits[key]
            info = dict(
                base,
                ok=True,
                window=window,
                limit=limit,
                used=used,
                remaining=max(0, limit - used),
                reset_in_seconds=reset_in,
            )
            if used + tokens > limit:
                info["ok"] = False
                return False, info
        # Admitted: report the first configured window.
        window, key, used, reset_in = windows[0]
        limit = limits[key]
        return True, dict(
            base,
            ok=True,
            window=window,
            limit=limit,
            used=used,
            remaining=max(0, limit - used),
            reset_in_seconds=reset_in,
        )

    # -- usage --------------------------------------------------------------
    def record_usage(
        self,
        scope_type: str,
        scope_id: str,
        tokens: int,
        model: Optional[str] = None,
        team_id: Optional[str] = None,
        day: Union[str, datetime.date, None] = None,
    ) -> Dict[str, Any]:
        """Durably add ``tokens`` to the scope's day row (one request counted).

        ``model`` prices the tokens for chargeback via the configured
        per-model price. ``team_id`` additionally attributes the same usage
        to the team's own scope row, so team budgets bound the aggregate
        without a second call. Returns the day's totals for the scope.
        """
        scope_type = validate_scope_type(scope_type)
        scope_id = validate_scope_id(scope_id)
        tokens = int(tokens)
        if tokens < 0:
            raise ValueError("tokens must be non-negative")
        target_day = _as_date(day)
        cost = tokens / 1_000_000 * self.model_price(model)
        self._guarded(
            self._executor.execute, _UPSERT_USAGE,
            (scope_type, scope_id, target_day, tokens, cost),
        )
        if team_id is not None and scope_type != "team":
            team_id = validate_scope_id(team_id)
            self._guarded(
                self._executor.execute, _UPSERT_USAGE,
                ("team", team_id, target_day, tokens, cost),
            )
        return self.usage_summary(scope_type, scope_id, target_day)

    def usage_summary(
        self, scope_type: str, scope_id: str, day: Union[str, datetime.date, None] = None
    ) -> Dict[str, Any]:
        """Tokens, requests and estimated cost for a scope on a day."""
        scope_type = validate_scope_type(scope_type)
        scope_id = validate_scope_id(scope_id)
        target_day = _as_date(day)
        rows = self._guarded(
            self._executor.query, _SELECT_USAGE, (scope_type, scope_id, target_day)
        )
        tokens, requests, cost = (0, 0, 0.0)
        if rows:
            tokens, requests, cost = int(rows[0][0]), int(rows[0][1]), float(rows[0][2])
        return {
            "scope_type": scope_type,
            "scope_id": scope_id,
            "day": target_day.isoformat(),
            "tokens": tokens,
            "requests": requests,
            "cost_usd": round(cost, 6),
            "limits": self.get_limits(scope_type, scope_id),
        }

    def chargeback(
        self, day: Union[str, datetime.date, None] = None
    ) -> Dict[str, Any]:
        """Admin view: per-scope usage for a day, with totals. Estimates only."""
        target_day = _as_date(day)
        rows = self._guarded(self._executor.query, _CHARGEBACK, (target_day,))
        scopes = [
            {
                "scope_type": r[0],
                "scope_id": r[1],
                "tokens": int(r[2]),
                "requests": int(r[3]),
                "cost_usd": round(float(r[4]), 6),
            }
            for r in rows
        ]
        return {
            "day": target_day.isoformat(),
            "scopes": scopes,
            "totals": {
                "tokens": sum(s["tokens"] for s in scopes),
                "requests": sum(s["requests"] for s in scopes),
                "cost_usd": round(sum(s["cost_usd"] for s in scopes), 6),
            },
            "note": "cost_usd is an operator-configured estimate for showback, not billing",
        }

    # -- internals ------------------------------------------------------------
    def _used_on(self, scope_type: str, scope_id: str, day: datetime.date) -> int:
        rows = self._guarded(
            self._executor.query, _SELECT_USAGE, (scope_type, scope_id, day)
        )
        return int(rows[0][0]) if rows else 0

    def _used_since(
        self, scope_type: str, scope_id: str, since: datetime.date
    ) -> int:
        rows = self._guarded(
            self._executor.query, _SUM_MONTH, (scope_type, scope_id, since)
        )
        return int(rows[0][0] or 0) if rows else 0

    def _guarded(self, fn, *args):
        """Fail-closed: any store failure becomes ConnectionError (HTTP 503)."""
        try:
            return fn(*args)
        except ConnectionError:
            raise
        except Exception as exc:
            raise ConnectionError("QuotaScopes store unavailable") from exc


def open_quota_scopes(env: Optional[Mapping[str, str]] = None,
                       executor=None) -> Optional["QuotaScopes"]:
    """Return the quota-scope store, or None when file mode is configured.

    The control-store helpers are imported lazily so that importing this
    module never executes the control-store package ``__init__`` (which pulls
    the fleet registry and its own dependency tree).
    """
    if executor is not None:
        return QuotaScopes(executor)
    from services.control_store.dsn import resolve_dsn
    from services.control_store.executor import open_executor

    dsn = resolve_dsn(env)
    if not dsn:
        return None
    return QuotaScopes(open_executor(dsn))
