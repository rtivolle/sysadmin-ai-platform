"""Unit tests for the desired-state autoscaler (``services.fleet.autoscaler``).

Covers ``decide()`` — scale-up on queue depth, scale-up on TTFT, scale-down
on idle + low latency, cooldown enforcement, the quota-headroom hard ceiling,
min/max bounds and quota clamping — plus the ``autoscale_events`` cooldown
ledger helpers against a fake executor. No database, no GPU, no network.
"""
import pytest

from services.fleet import autoscaler


def policy(**overrides):
    base = {"replicas": 2, "engine": "vllm", "vram_per_replica_gb": 22.0}
    base.update(overrides)
    return base


def metrics(queue_depth=0, ttft_p99_s=0.1, latency_p99_s=0.2):
    return {"queue_depth": queue_depth, "ttft_p99_s": ttft_p99_s,
            "latency_p99_s": latency_p99_s}


NOW = 1_700_000_000.0


# --- scale-up ---------------------------------------------------------------
def test_scale_up_on_queue_depth():
    decisions = autoscaler.decide(
        {"m": policy(replicas=2)},
        {"m": metrics(queue_depth=20)},
        {"m": 4}, NOW, {})
    assert decisions == {"m": 3}


def test_scale_up_on_ttft_even_with_empty_queue():
    decisions = autoscaler.decide(
        {"m": policy(replicas=2, target_ttft_s=0.5)},
        {"m": metrics(queue_depth=0, ttft_p99_s=2.5)},
        {"m": 4}, NOW, {})
    assert decisions == {"m": 3}


def test_no_scale_up_at_max_replicas():
    decisions = autoscaler.decide(
        {"m": policy(replicas=4, max_replicas=4)},
        {"m": metrics(queue_depth=99, ttft_p99_s=9.9)},
        {"m": 4}, NOW, {})
    assert decisions == {}


def test_scale_up_defaults_when_policy_fields_missing():
    # queue_depth 9 > default threshold 8, max defaults to 4
    decisions = autoscaler.decide(
        {"m": {"replicas": 1}},
        {"m": metrics(queue_depth=9)},
        {}, NOW, {})
    assert decisions == {"m": 2}


# --- scale-down --------------------------------------------------------------
def test_scale_down_on_idle_and_low_latency():
    decisions = autoscaler.decide(
        {"m": policy(replicas=3, target_ttft_s=0.5)},
        {"m": metrics(queue_depth=0, ttft_p99_s=0.2)},
        {"m": 4}, NOW, {})
    assert decisions == {"m": 2}


def test_scale_down_when_latency_exactly_at_target():
    # ttft == target is healthy: an idle model still scales down.
    decisions = autoscaler.decide(
        {"m": policy(replicas=3, target_ttft_s=0.5)},
        {"m": metrics(queue_depth=0, ttft_p99_s=0.5)},
        {"m": 4}, NOW, {})
    assert decisions == {"m": 2}


def test_no_scale_down_when_latency_above_target():
    # Above target the pressure goes the other way (scale-up), never down.
    decisions = autoscaler.decide(
        {"m": policy(replicas=3, target_ttft_s=0.5)},
        {"m": metrics(queue_depth=0, ttft_p99_s=0.8)},
        {"m": 4}, NOW, {})
    assert decisions == {"m": 4}


def test_no_scale_down_without_ttft_signal():
    decisions = autoscaler.decide(
        {"m": policy(replicas=3)},
        {"m": {"queue_depth": 0}},  # no ttft_p99_s at all
        {"m": 4}, NOW, {})
    assert decisions == {}


def test_no_scale_down_below_min_replicas():
    decisions = autoscaler.decide(
        {"m": policy(replicas=1, min_replicas=1)},
        {"m": metrics(queue_depth=0, ttft_p99_s=0.05)},
        {"m": 4}, NOW, {})
    assert decisions == {}


def test_no_action_without_metrics():
    decisions = autoscaler.decide(
        {"m": policy(replicas=2)},
        {},  # model absent: never scale on a guess
        {"m": 4}, NOW, {})
    assert decisions == {}


# --- cooldowns ---------------------------------------------------------------
def test_scale_up_cooldown_blocks_repeat():
    last = {"m": NOW - 60.0}  # 60 s ago, cooldown is 300 s
    decisions = autoscaler.decide(
        {"m": policy(replicas=2)},
        {"m": metrics(queue_depth=50)},
        {"m": 4}, NOW, last)
    assert decisions == {}


def test_scale_up_cooldown_expired_allows():
    last = {"m": NOW - 301.0}
    decisions = autoscaler.decide(
        {"m": policy(replicas=2)},
        {"m": metrics(queue_depth=50)},
        {"m": 4}, NOW, last)
    assert decisions == {"m": 3}


def test_scale_down_cooldown_is_independent_and_longer():
    last = {"m": NOW - 400.0}  # past the 300 s up-cooldown, inside 900 s down
    decisions = autoscaler.decide(
        {"m": policy(replicas=3, target_ttft_s=0.5)},
        {"m": metrics(queue_depth=0, ttft_p99_s=0.1)},
        {"m": 4}, NOW, last)
    assert decisions == {}


def test_custom_cooldowns_from_policy():
    last = {"m": NOW - 120.0}
    decisions = autoscaler.decide(
        {"m": policy(replicas=2, scale_up_cooldown_s=60)},
        {"m": metrics(queue_depth=50)},
        {"m": 4}, NOW, last)
    assert decisions == {"m": 3}


# --- quota headroom: the hard ceiling -----------------------------------------
def test_quota_headroom_caps_scale_up():
    decisions = autoscaler.decide(
        {"m": policy(replicas=2, max_replicas=4)},
        {"m": metrics(queue_depth=50)},
        {"m": 2},  # quota allows no more than 2
        NOW, {})
    assert decisions == {}


def test_quota_headroom_clamps_existing_replicas_immediately():
    # Replicas already above the quota ceiling are clamped at once,
    # bypassing the scale-down cooldown.
    last = {"m": NOW - 10.0}
    decisions = autoscaler.decide(
        {"m": policy(replicas=4, max_replicas=4)},
        {"m": metrics(queue_depth=0, ttft_p99_s=0.1)},
        {"m": 1}, NOW, last)
    assert decisions == {"m": 1}


def test_quota_headroom_missing_falls_back_to_policy_max():
    decisions = autoscaler.decide(
        {"m": policy(replicas=3, max_replicas=4)},
        {"m": metrics(queue_depth=50)},
        {},  # no headroom entry -> policy max
        NOW, {})
    assert decisions == {"m": 4}


# --- bounds -------------------------------------------------------------------
def test_min_replicas_above_one():
    decisions = autoscaler.decide(
        {"m": policy(replicas=2, min_replicas=2)},
        {"m": metrics(queue_depth=0, ttft_p99_s=0.05)},
        {"m": 4}, NOW, {})
    assert decisions == {}


def test_multiple_models_decided_independently():
    decisions = autoscaler.decide(
        {"a": policy(replicas=1),
         "b": policy(replicas=3, target_ttft_s=0.5)},
        {"a": metrics(queue_depth=30),
         "b": metrics(queue_depth=0, ttft_p99_s=0.1)},
        {"a": 4, "b": 4}, NOW, {})
    assert decisions == {"a": 2, "b": 2}


def test_invalid_policy_replicas_rejected():
    with pytest.raises(ValueError):
        autoscaler.decide({"m": policy(replicas=-1)}, {}, {"m": 4}, NOW, {})


def test_invalid_min_max_rejected():
    with pytest.raises(ValueError):
        autoscaler.decide(
            {"m": policy(min_replicas=5, max_replicas=2)}, {}, {"m": 4}, NOW, {})


# --- cooldown ledger -----------------------------------------------------------
class FakeExecutor:
    """Minimal Executor double: records statements, serves canned rows."""

    def __init__(self, rows=()):
        self.statements = []  # (sql, params)
        self._rows = list(rows)

    def execute(self, sql, params=()):
        self.statements.append((sql, tuple(params)))
        return 1

    def query(self, sql, params=()):
        self.statements.append((sql, tuple(params)))
        return list(self._rows)


def test_ensure_schema_creates_table_if_not_exists():
    ex = FakeExecutor()
    autoscaler.ensure_schema(ex)
    assert len(ex.statements) == 1
    sql, params = ex.statements[0]
    assert "CREATE TABLE IF NOT EXISTS autoscale_events" in sql
    assert params == ()


def test_load_last_scales_returns_max_per_model():
    ex = FakeExecutor(rows=[("a", 100.0), ("b", 200.0), ("a", 150.0)])
    assert autoscaler.load_last_scales(ex) == {"a": 150.0, "b": 200.0}


def test_load_last_scales_empty_table():
    assert autoscaler.load_last_scales(FakeExecutor()) == {}


def test_record_scale_event_inserts_row():
    ex = FakeExecutor()
    autoscaler.record_scale_event(ex, "m", "scale_up", NOW)
    sql, params = ex.statements[0]
    assert "INSERT INTO autoscale_events" in sql
    assert params == ("m", "scale_up", NOW)


# --- quota headroom resolution --------------------------------------------------
class FakeQuotaScopes:
    """Minimal chantier-1 QuotaScopes double: check_budget(scope_type, scope_id, tokens)."""

    def __init__(self, admitted=True):
        self.admitted = admitted
        self.calls = []

    def check_budget(self, scope_type, scope_id, tokens=0):
        self.calls.append((scope_type, scope_id, tokens))
        return self.admitted, {"limited": True, "limit": 1000,
                               "remaining": 1000 if self.admitted else 0}


def test_resolve_quota_headroom_without_quota_store():
    headroom = autoscaler.resolve_quota_headroom(
        {"m": policy(max_replicas=4, team_id="t1")}, None)
    assert headroom == {"m": 4}


def test_resolve_quota_headroom_admitted_team_uses_policy_max():
    headroom = autoscaler.resolve_quota_headroom(
        {"m": policy(replicas=2, max_replicas=4, team_id="t1")},
        FakeQuotaScopes(admitted=True))
    assert headroom == {"m": 4}


def test_resolve_quota_headroom_exhausted_team_freezes_at_current():
    # Budget exhausted: the autoscaler may not ADD replicas for that team.
    headroom = autoscaler.resolve_quota_headroom(
        {"m": policy(replicas=2, max_replicas=4, team_id="t1")},
        FakeQuotaScopes(admitted=False))
    assert headroom == {"m": 2}


def test_resolve_quota_headroom_exhausted_team_still_scales_up_to_current():
    # A model below its frozen count may still converge upward to it.
    decisions = autoscaler.decide(
        {"m": policy(replicas=1, max_replicas=4, team_id="t1")},
        {"m": metrics(queue_depth=50)},
        {"m": 2}, NOW, {})
    assert decisions == {"m": 2}


def test_resolve_quota_headroom_unreadable_store_falls_back():
    class Broken:
        def check_budget(self, scope_type, scope_id, tokens=0):
            raise RuntimeError("store down")

    headroom = autoscaler.resolve_quota_headroom(
        {"m": policy(max_replicas=3, team_id="t1")}, Broken())
    assert headroom == {"m": 3}


def test_resolve_quota_headroom_no_team_id_uses_policy_max():
    qs = FakeQuotaScopes(admitted=False)
    headroom = autoscaler.resolve_quota_headroom({"m": policy(max_replicas=3)}, qs)
    assert headroom == {"m": 3}
    assert qs.calls == []  # store not even consulted


def test_build_team_state_empty_without_quota_store():
    assert autoscaler.build_team_state({"m": policy(team_id="t1")}, None) == {}


def test_build_team_state_delegates_to_distribution():
    qs = FakeQuotaScopes(admitted=True)
    state = autoscaler.build_team_state(
        {"a": policy(team_id="t1"), "b": policy()}, qs)
    assert state["a"]["team_id"] == "t1"
    assert state["a"]["remaining_ratio"] == 1.0
    assert state["a"]["exhausted"] is False
    assert "b" not in state  # no team_id -> omitted (neutral routing)


def test_build_team_state_marks_exhausted():
    qs = FakeQuotaScopes(admitted=False)
    state = autoscaler.build_team_state({"a": policy(team_id="t1")}, qs)
    assert state["a"]["exhausted"] is True
    assert state["a"]["remaining_ratio"] == 0.0


# --- metrics intake --------------------------------------------------------------
def test_read_fleet_metrics_empty_without_collector_accessor():
    # The observability collector has no latest_fleet_metrics() yet.
    assert autoscaler.read_fleet_metrics() == {}


def test_invert_assignments():
    assignments = {
        "n1": [{"model": "a"}, {"model": "b"}],
        "n2": [{"model": "a"}],
    }
    assert autoscaler.invert_assignments(assignments) == {
        "a": [{"node": "n1"}, {"node": "n2"}],
        "b": [{"node": "n1"}],
    }


def test_compute_routing_weights_neutral_without_team_state():
    # Chantier 1's distribution module is present: no quota signal means
    # neutral weight 1.0 per replica — never penalise the unknown.
    weights = autoscaler.compute_routing_weights(
        {"a": [{"node": "n1"}, {"node": "n2"}]}, {})
    assert weights == {"a": [{"node": "n1", "weight": 1.0},
                             {"node": "n2", "weight": 1.0}]}


def test_compute_routing_weights_honours_exhaustion():
    weights = autoscaler.compute_routing_weights(
        {"a": [{"node": "n1"}]},
        {"a": {"team_id": "t1", "remaining_ratio": 0.0, "exhausted": True}})
    # Degraded trickle, never an outage: strictly positive weights.
    assert list(weights) == ["a"]
    assert weights["a"][0]["node"] == "n1"
    assert weights["a"][0]["weight"] > 0


def test_compute_routing_weights_empty_assignments():
    assert autoscaler.compute_routing_weights({}, {}) is None
