"""QuotaScopes: durable team/project budgets (fake-executor unit tests)."""
import contextlib
import datetime
import json

import pytest

from services.control_store.quota_scopes import (
    QuotaScopes,
    validate_limits,
    validate_scope_id,
    validate_scope_type,
)


class QuotaScopesTableExecutor:
    """In-memory emulation of quota_scopes + quota_usage_daily."""

    def __init__(self):
        self.scopes = {}  # (scope_type, scope_id) -> limits dict
        self.usage = {}  # (scope_type, scope_id, day) -> dict
        self.statements = []
        self.fail_with = None

    def query(self, sql, params=()):
        self._maybe_fail()
        self.statements.append((sql, tuple(params)))
        if sql.startswith("SELECT limits FROM quota_scopes"):
            key = (params[0], params[1])
            return [(json.dumps(self.scopes[key]) ,)] if key in self.scopes else []
        if sql.startswith("SELECT scope_type, scope_id, limits FROM quota_scopes"):
            rows = [
                (st, sid, json.dumps(limits))
                for (st, sid), limits in sorted(self.scopes.items())
            ]
            if "WHERE scope_type = %s" in sql:
                rows = [r for r in rows if r[0] == params[0]]
            return rows
        if sql.startswith("SELECT tokens, requests, cost_usd FROM quota_usage_daily"):
            key = (params[0], params[1], str(params[2]))
            row = self.usage.get(key)
            return [(row["tokens"], row["requests"], row["cost_usd"])] if row else []
        if sql.startswith("SELECT COALESCE(SUM(tokens), 0) FROM quota_usage_daily"):
            st, sid, since = params[0], params[1], str(params[2])
            total = sum(
                row["tokens"] for (a, b, day), row in self.usage.items()
                if a == st and b == sid and day >= since
            )
            return [(total,)]
        if sql.startswith("SELECT scope_type, scope_id, tokens, requests, cost_usd"):
            day = str(params[0])
            rows = [
                (st, sid, row["tokens"], row["requests"], row["cost_usd"])
                for (st, sid, d), row in self.usage.items() if d == day
            ]
            rows.sort(key=lambda r: (-r[4], -r[2]))
            return rows
        raise AssertionError(f"unexpected query: {sql}")

    def execute(self, sql, params=()):
        self._maybe_fail()
        self.statements.append((sql, tuple(params)))
        if sql.startswith("CREATE TABLE IF NOT EXISTS"):
            return 0
        if sql.startswith("INSERT INTO quota_scopes"):
            st, sid, limits_json = params
            self.scopes[(st, sid)] = json.loads(limits_json)
            return 1
        if sql.startswith("DELETE FROM quota_scopes"):
            return 1 if self.scopes.pop((params[0], params[1]), None) is not None else 0
        if sql.startswith("INSERT INTO quota_usage_daily"):
            st, sid, day, tokens, cost = params
            key = (st, sid, str(day))
            row = self.usage.setdefault(
                key, {"tokens": 0, "requests": 0, "cost_usd": 0.0})
            row["tokens"] += tokens
            row["requests"] += 1
            row["cost_usd"] += cost
            return 1
        raise AssertionError(f"unexpected statement: {sql}")

    @contextlib.contextmanager
    def transaction(self):
        yield self

    def _maybe_fail(self):
        if self.fail_with is not None:
            raise self.fail_with


@pytest.fixture()
def scopes():
    return QuotaScopes(QuotaScopesTableExecutor())


# --- validation ------------------------------------------------------------

def test_validate_scope_type_rejects_unknown():
    with pytest.raises(ValueError):
        validate_scope_type("org")
    assert validate_scope_type("team") == "team"


def test_validate_scope_id_rejects_garbage():
    for bad in ["", "../x", "a" * 129, "has space", "semi;colon"]:
        with pytest.raises(ValueError):
            validate_scope_id(bad)
    assert validate_scope_id("team-1") == "team-1"


def test_validate_limits_bounds():
    with pytest.raises(ValueError):
        validate_limits({})
    with pytest.raises(ValueError):
        validate_limits({"nope": 1})
    with pytest.raises(ValueError):
        validate_limits({"daily_tokens": 0})
    with pytest.raises(ValueError):
        validate_limits({"daily_tokens": True})
    assert validate_limits({"daily_tokens": 1000}) == {"daily_tokens": 1000}


# --- schema -----------------------------------------------------------------

def test_ensure_schema_issues_create_table_if_not_exists():
    fake = QuotaScopesTableExecutor()
    QuotaScopes(fake).ensure_schema()
    creates = [sql for sql, _ in fake.statements if sql.startswith("CREATE TABLE")]
    assert len(creates) == 2
    assert all("IF NOT EXISTS" in sql for sql in creates)
    assert any("quota_scopes" in sql for sql in creates)
    assert any("quota_usage_daily" in sql for sql in creates)


# --- limits CRUD --------------------------------------------------------------

def test_set_get_delete_limits_roundtrip(scopes):
    assert scopes.get_limits("team", "alpha") is None  # unlimited by default
    stored = scopes.set_limits("team", "alpha", {"daily_tokens": 5000})
    assert stored == {"daily_tokens": 5000}
    assert scopes.get_limits("team", "alpha") == {"daily_tokens": 5000}
    # replace
    scopes.set_limits("team", "alpha", {"daily_tokens": 9000, "rpm": 50})
    assert scopes.get_limits("team", "alpha") == {"daily_tokens": 9000, "rpm": 50}
    assert scopes.delete_limits("team", "alpha") is True
    assert scopes.get_limits("team", "alpha") is None
    assert scopes.delete_limits("team", "alpha") is False


def test_set_limits_validates(scopes):
    with pytest.raises(ValueError):
        scopes.set_limits("org", "alpha", {"daily_tokens": 1})
    with pytest.raises(ValueError):
        scopes.set_limits("team", "alpha", {"daily_tokens": 0})


def test_list_scopes(scopes):
    scopes.set_limits("team", "alpha", {"daily_tokens": 100})
    scopes.set_limits("project", "beta", {"daily_tokens": 200})
    all_scopes = scopes.list_scopes()
    assert [(s["scope_type"], s["scope_id"]) for s in all_scopes] == [
        ("project", "beta"), ("team", "alpha")]
    assert [s["scope_id"] for s in scopes.list_scopes("team")] == ["alpha"]


# --- admission ------------------------------------------------------------------

def test_check_budget_unlimited_when_no_limits(scopes):
    admitted, info = scopes.check_budget("team", "alpha", 10 ** 9)
    assert admitted is True
    assert info["limited"] is False
    assert info["limit"] is None


def test_check_budget_daily_window(scopes):
    scopes.set_limits("team", "alpha", {"daily_tokens": 1000})
    admitted, info = scopes.check_budget("team", "alpha", 400)
    assert admitted is True
    assert info["window"] == "daily"
    assert info["used"] == 0
    assert info["remaining"] == 1000
    assert info["reset_in_seconds"] > 0
    scopes.record_usage("team", "alpha", 700)
    admitted, info = scopes.check_budget("team", "alpha", 400)
    assert admitted is False
    assert info["used"] == 700
    assert info["remaining"] == 300
    assert info["reset_in_seconds"] > 0
    admitted, _ = scopes.check_budget("team", "alpha", 300)
    assert admitted is True  # exactly at the limit still fits


def test_check_budget_monthly_window(scopes):
    scopes.set_limits("team", "alpha", {"monthly_tokens": 1000})
    scopes.record_usage("team", "alpha", 900)
    admitted, info = scopes.check_budget("team", "alpha", 200)
    assert admitted is False
    assert info["window"] == "monthly"
    assert info["limit"] == 1000
    assert info["used"] == 900


def test_check_budget_monthly_only_reports_monthly_window(scopes):
    scopes.set_limits("team", "alpha", {"monthly_tokens": 1000})
    admitted, info = scopes.check_budget("team", "alpha", 100)
    assert admitted is True
    assert info["window"] == "monthly"
    assert info["limit"] == 1000
    assert info["remaining"] == 1000


def test_check_budget_rejects_negative(scopes):
    with pytest.raises(ValueError):
        scopes.check_budget("team", "alpha", -1)


# --- usage ----------------------------------------------------------------------

def test_record_usage_accumulates_and_prices(scopes):
    scopes.set_model_price("llama-8b", 2.0)  # USD per Mtok
    summary = scopes.record_usage("team", "alpha", 500_000, model="llama-8b")
    assert summary["tokens"] == 500_000
    assert summary["requests"] == 1
    assert summary["cost_usd"] == pytest.approx(1.0)
    summary = scopes.record_usage("team", "alpha", 500_000, model="llama-8b")
    assert summary["tokens"] == 1_000_000
    assert summary["requests"] == 2
    assert summary["cost_usd"] == pytest.approx(2.0)


def test_record_usage_unpriced_model_costs_zero(scopes):
    summary = scopes.record_usage("team", "alpha", 1000, model="unknown-model")
    assert summary["cost_usd"] == 0.0


def test_record_usage_attributes_team(scopes):
    scopes.record_usage("project", "beta", 100, team_id="alpha")
    assert scopes.usage_summary("project", "beta")["tokens"] == 100
    assert scopes.usage_summary("team", "alpha")["tokens"] == 100


def test_record_usage_rejects_negative(scopes):
    with pytest.raises(ValueError):
        scopes.record_usage("team", "alpha", -5)


def test_usage_summary_unknown_scope_is_zeros(scopes):
    summary = scopes.usage_summary("team", "ghost")
    assert summary["tokens"] == 0
    assert summary["requests"] == 0
    datetime.date.fromisoformat(summary["day"])  # parses as a date
    assert summary["limits"] is None


def test_chargeback_aggregates(scopes):
    scopes.record_usage("team", "alpha", 100)
    scopes.record_usage("team", "alpha", 200)
    scopes.record_usage("project", "beta", 50)
    report = scopes.chargeback()
    by_id = {s["scope_id"]: s for s in report["scopes"]}
    assert by_id["alpha"]["tokens"] == 300
    assert by_id["beta"]["tokens"] == 50
    assert report["totals"]["tokens"] == 350
    assert report["totals"]["requests"] == 3
    assert "not billing" in report["note"]


# --- fail-closed ------------------------------------------------------------------

def test_constructor_rejects_missing_executor():
    with pytest.raises(ConnectionError):
        QuotaScopes(None)


def test_store_outage_raises_connection_error():
    fake = QuotaScopesTableExecutor()
    fake.fail_with = OSError("postgres down")
    scopes = QuotaScopes(fake)
    with pytest.raises(ConnectionError):
        scopes.get_limits("team", "alpha")
    with pytest.raises(ConnectionError):
        scopes.set_limits("team", "alpha", {"daily_tokens": 1})
    with pytest.raises(ConnectionError):
        scopes.check_budget("team", "alpha", 1)
    with pytest.raises(ConnectionError):
        scopes.record_usage("team", "alpha", 1)
    with pytest.raises(ConnectionError):
        scopes.usage_summary("team", "alpha")
    with pytest.raises(ConnectionError):
        scopes.chargeback()
    with pytest.raises(ConnectionError):
        scopes.ensure_schema()
