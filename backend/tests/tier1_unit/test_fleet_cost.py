"""Tier-1 unit tests for the fleet GPU cost tracker (chantier 6).

All runnable without a GPU, a database or the network: the Executor protocol
is doubled over in-memory SQLite (``%s`` -> ``?`` placeholder translation),
placements come from a stub registry, and token usage is injected.
"""
import contextlib
import datetime
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.fleet import cost_router
from services.fleet.cost_tracker import CostTracker, GpuPricing

DAY = datetime.date(2026, 9, 30)


class SqliteExecutor:
    """Executor double: runs the real SQL against in-memory SQLite."""

    def __init__(self):
        # check_same_thread=False: TestClient serves requests on another thread.
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)

    def query(self, sql, params=()):
        return self.conn.execute(sql.replace("%s", "?"), params).fetchall()

    def execute(self, sql, params=()):
        cur = self.conn.execute(sql.replace("%s", "?"), params)
        self.conn.commit()
        return cur.rowcount

    @contextlib.contextmanager
    def transaction(self):
        yield self


class FakeRegistry:
    def __init__(self, placements, policies):
        self._placements = placements  # [(model_name, node_name), ...]
        self._policies = policies  # {model_name: policy}

    def list_placements(self):
        return [
            {"model_name": m, "node_name": n, "desired_state": None,
             "actual_state": "running", "updated_at": None}
            for m, n in self._placements
        ]

    def get_desired_state(self):
        return self._policies


def _pricing(tmp_path):
    path = tmp_path / "gpu_pricing.yaml"
    path.write_text(
        "default: 2.00\n"
        "prices:\n"
        "  h100-80gb: 4.00\n"
        "  rtx4090: 0.75\n"
        "aliases:\n"
        "  h100-80gb: [h100]\n"
    )
    return GpuPricing(path=path)


@pytest.fixture
def tracker(tmp_path):
    ex = SqliteExecutor()
    registry = FakeRegistry(
        placements=[("model-a", "gpu-01"), ("model-a", "gpu-01"),
                    ("model-b", "gpu-01")],
        policies={"model-a": {"team_id": "team-red", "replicas": 2},
                  "model-b": {"team_id": "team-blue", "replicas": 1}},
    )
    t = CostTracker(ex, pricing=_pricing(tmp_path), fleet_registry=registry)
    t.ensure_schema()
    return t


def _accrue(tracker):
    # 8 GPU-hours on an H100-class node: 8 * 4.00 = 32.00 USD.
    tracker.accrue("gpu-01", "h100-80gb", 8.0, DAY)


# -- accrual -----------------------------------------------------------------


def test_accrue_creates_and_accumulates_per_node_day(tracker):
    tracker.accrue("gpu-01", "h100-80gb", 2.0, DAY)
    tracker.accrue("gpu-01", "h100-80gb", 1.0, DAY)  # same (node, day): adds up
    rows = tracker._executor.query(
        "SELECT node_name, gpu_hours, usd FROM gpu_cost_ledger", ())
    assert rows == [("gpu-01", 3.0, 12.0)]


def test_accrue_is_keyed_by_day(tracker):
    tracker.accrue("gpu-01", "h100-80gb", 1.0, DAY)
    tracker.accrue("gpu-01", "h100-80gb", 1.0, DAY - datetime.timedelta(days=1))
    rows = tracker._executor.query("SELECT COUNT(*) FROM gpu_cost_ledger", ())
    assert rows == [(2,)]


def test_accrue_defaults_to_today(tracker):
    tracker.accrue("gpu-01", "rtx4090", 1.0)
    rows = tracker._executor.query("SELECT day FROM gpu_cost_ledger", ())
    assert rows == [(datetime.date.today().isoformat(),)]


def test_accrue_validates_inputs(tracker):
    with pytest.raises(ValueError):
        tracker.accrue("", "h100-80gb", 1.0, DAY)
    with pytest.raises(ValueError):
        tracker.accrue("gpu-01", "h100-80gb", -1.0, DAY)


def test_accrue_unknown_gpu_class_uses_default(tracker):
    usd = tracker.accrue("gpu-01", "mystery-gpu-9000", 2.0, DAY)
    assert usd == pytest.approx(2.0 * 2.00)  # tmp pricing default


def test_ensure_schema_is_idempotent(tracker):
    tracker.ensure_schema()
    tracker.ensure_schema()


# -- attribution --------------------------------------------------------------


def test_attribution_token_prorata(tracker):
    _accrue(tracker)
    tracker._token_usage_fn = lambda day, models: {"model-a": 1000,
                                                   "model-b": 3000}
    got = tracker.cost_summary("model", "model-a", DAY)
    assert got["attribution"] == "token_prorata"
    assert got["tokens"] == 1000
    assert got["gpu_hours"] == pytest.approx(2.0)  # 8h * 1000/4000
    assert got["usd"] == pytest.approx(8.0)  # 32 * 1000/4000


def test_attribution_replica_fallback_when_no_token_source(tracker):
    _accrue(tracker)
    tracker._token_usage_fn = lambda day, models: None  # quota_scopes absent
    got = tracker.cost_summary("model", "model-a", DAY)
    assert got["attribution"] == "replica_prorata"
    assert got["gpu_hours"] == pytest.approx(8.0 * 2 / 3)  # 2 replicas of 3
    assert got["usd"] == pytest.approx(32.0 * 2 / 3)


def test_attribution_replica_fallback_when_no_tokens_that_day(tracker):
    _accrue(tracker)
    tracker._token_usage_fn = lambda day, models: {"model-a": 0, "model-b": 0}
    got = tracker.cost_summary("model", "model-b", DAY)
    assert got["attribution"] == "replica_prorata"
    assert got["usd"] == pytest.approx(32.0 / 3)


def test_team_summary_rolls_models_up(tracker):
    _accrue(tracker)
    tracker._token_usage_fn = lambda day, models: None
    red = tracker.cost_summary("team", "team-red", DAY)
    blue = tracker.cost_summary("team", "team-blue", DAY)
    assert red["usd"] == pytest.approx(32.0 * 2 / 3)
    assert blue["usd"] == pytest.approx(32.0 / 3)
    assert red["usd"] + blue["usd"] == pytest.approx(32.0)  # fully attributed


def test_node_scope_reports_direct_cost(tracker):
    _accrue(tracker)
    got = tracker.cost_summary("node", "gpu-01", DAY)
    assert got["attribution"] == "direct"
    assert got["gpu_hours"] == pytest.approx(8.0)
    assert got["usd"] == pytest.approx(32.0)


def test_model_without_registry_warns_and_returns_zeros():
    ex = SqliteExecutor()
    t = CostTracker(ex, pricing=GpuPricing(path="/nonexistent/pricing.yaml"))
    t.ensure_schema()
    t.accrue("gpu-01", "h100-80gb", 1.0, DAY)
    got = t.cost_summary("model", "model-a", DAY)
    assert got["usd"] == 0.0
    assert "warning" in got


def test_summary_rejects_unknown_scope(tracker):
    with pytest.raises(ValueError):
        tracker.cost_summary("datacenter", "x", DAY)


# -- pricing ------------------------------------------------------------------


def test_pricing_loads_yaml_prices(tmp_path):
    p = _pricing(tmp_path)
    assert p.price_per_hour("h100-80gb") == 4.00
    assert p.price_per_hour("H100") == 4.00  # alias
    assert p.price_per_hour("NVIDIA H100 80GB HBM3") == 4.00  # normalization
    assert p.price_per_hour("rtx4090") == 0.75
    assert p.price_per_hour("unknown-class") == 2.00  # default from file


def test_pricing_missing_file_falls_back_without_crash(tmp_path):
    p = GpuPricing(path=tmp_path / "does-not-exist.yaml")
    assert p.price_per_hour("h100-80gb") == 1.50  # hard fallback


def test_pricing_invalid_yaml_falls_back_without_crash(tmp_path):
    bad = tmp_path / "gpu_pricing.yaml"
    bad.write_text("{{{ not: [valid yaml")
    p = GpuPricing(path=bad)
    assert p.price_per_hour("h100-80gb") == 1.50


def test_repo_pricing_file_loads():
    from pathlib import Path
    repo_file = (Path(__file__).resolve().parents[3]
                 / "backend" / "config" / "fleet" / "gpu_pricing.yaml")
    p = GpuPricing(path=repo_file)
    assert p.price_per_hour("h100-80gb") == 4.00
    assert p.price_per_hour("a100-80gb") > 0


# -- router -------------------------------------------------------------------


@pytest.fixture
def cost_api(monkeypatch, tracker, tmp_path):
    monkeypatch.setattr(cost_router, "cost_tracker", tracker)
    monkeypatch.setattr(cost_router, "authenticate_request",
                        lambda request: ("admin", None))
    monkeypatch.setattr(cost_router, "role_for_user", lambda user_id: "admin")
    app = FastAPI()
    app.include_router(cost_router.router)
    return TestClient(app)


def test_router_summary_ok(cost_api, tracker):
    _accrue(tracker)
    tracker._token_usage_fn = lambda day, models: None
    r = cost_api.get("/api/v1/fleet/costs/summary",
                     params={"scope_type": "team", "scope_id": "team-red",
                             "day": "2026-09-30"})
    assert r.status_code == 200
    body = r.json()
    assert body["usd"] == pytest.approx(32.0 * 2 / 3)
    assert body["day"] == "2026-09-30"


def test_router_fails_closed_without_store(monkeypatch):
    monkeypatch.setattr(cost_router, "cost_tracker", None)
    monkeypatch.setattr(cost_router, "authenticate_request",
                        lambda request: ("admin", None))
    monkeypatch.setattr(cost_router, "role_for_user", lambda user_id: "admin")
    app = FastAPI()
    app.include_router(cost_router.router)
    r = TestClient(app).get("/api/v1/fleet/costs/summary",
                            params={"scope_type": "node", "scope_id": "gpu-01"})
    assert r.status_code == 503


def test_router_rejects_non_admin(cost_api, monkeypatch):
    monkeypatch.setattr(cost_router, "role_for_user", lambda user_id: "user")
    r = cost_api.get("/api/v1/fleet/costs/summary",
                     params={"scope_type": "node", "scope_id": "gpu-01"})
    assert r.status_code == 403


def test_router_rejects_bad_params(cost_api):
    r = cost_api.get("/api/v1/fleet/costs/summary",
                     params={"scope_type": "nope", "scope_id": "x",
                             "day": "2026-09-30"})
    assert r.status_code == 400
    r = cost_api.get("/api/v1/fleet/costs/summary",
                     params={"scope_type": "node", "scope_id": "gpu-01",
                             "day": "not-a-date"})
    assert r.status_code == 400
