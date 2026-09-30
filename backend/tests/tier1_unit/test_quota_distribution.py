"""quota_weights: quota-aware distribution (pure-function unit tests)."""
import pytest

from services.fleet.distribution import (
    EPSILON,
    quota_weights,
    team_state_for_models,
)


def _assignments():
    return {
        "llama-8b": [{"node": "n1"}, {"node": "n2"}],
        "qwen-32b": [{"node": "n3"}],
    }


def test_missing_team_state_is_neutral():
    weights = quota_weights(_assignments(), {})
    assert weights["llama-8b"] == [
        {"node": "n1", "weight": 1.0}, {"node": "n2", "weight": 1.0}]
    assert weights["qwen-32b"] == [{"node": "n3", "weight": 1.0}]


def test_team_state_none_is_neutral():
    weights = quota_weights(_assignments(), None)
    assert all(entry["weight"] == 1.0
               for entries in weights.values() for entry in entries)


def test_weight_proportional_to_remaining_ratio():
    team_state = {
        "llama-8b": {"team_id": "alpha", "remaining_ratio": 0.5, "exhausted": False},
        "qwen-32b": {"team_id": "alpha", "remaining_ratio": 0.25, "exhausted": False},
    }
    weights = quota_weights(_assignments(), team_state)
    assert [e["weight"] for e in weights["llama-8b"]] == [0.5, 0.5]
    assert [e["weight"] for e in weights["qwen-32b"]] == [0.25]


def test_exhausted_team_gets_zero_weight_but_model_survives():
    # llama-8b's team is exhausted: every replica would get weight 0, so the
    # model degrades to an EPSILON trickle instead of vanishing from routing.
    team_state = {
        "llama-8b": {"team_id": "alpha", "remaining_ratio": 0.0, "exhausted": True},
    }
    weights = quota_weights(_assignments(), team_state)
    assert weights["llama-8b"] == [
        {"node": "n1", "weight": EPSILON}, {"node": "n2", "weight": EPSILON}]
    for entry in weights["llama-8b"]:
        assert entry["weight"] > 0
    # the model with no quota signal is untouched
    assert weights["qwen-32b"] == [{"node": "n3", "weight": 1.0}]


def test_single_replica_exhausted_model_degrades_not_outage():
    weights = quota_weights(
        {"qwen-32b": [{"node": "n3"}]},
        {"qwen-32b": {"team_id": "a", "remaining_ratio": 0.0, "exhausted": True}},
    )
    assert weights == {"qwen-32b": [{"node": "n3", "weight": EPSILON}]}


def test_zero_ratio_without_exhausted_flag_also_degrades():
    weights = quota_weights(
        {"qwen-32b": [{"node": "n3"}]},
        {"qwen-32b": {"team_id": "a", "remaining_ratio": 0.0, "exhausted": False}},
    )
    assert weights["qwen-32b"][0]["weight"] == EPSILON


def test_every_emitted_weight_is_positive():
    team_state = {
        "llama-8b": {"team_id": "a", "remaining_ratio": 0.0, "exhausted": True},
        "qwen-32b": {"team_id": "b", "remaining_ratio": 0.7, "exhausted": False},
    }
    weights = quota_weights(_assignments(), team_state)
    for entries in weights.values():
        assert entries, "a model must never lose all routable capacity"
        for entry in entries:
            assert entry["weight"] > 0


def test_extra_team_state_entries_are_ignored():
    weights = quota_weights(
        {"qwen-32b": [{"node": "n3"}]},
        {"ghost-model": {"team_id": "a", "remaining_ratio": 0.1, "exhausted": True}},
    )
    assert weights == {"qwen-32b": [{"node": "n3", "weight": 1.0}]}


def test_ratio_is_clamped():
    weights = quota_weights(
        {"m": [{"node": "n1"}]},
        {"m": {"team_id": "a", "remaining_ratio": 7.5, "exhausted": False}},
    )
    assert weights["m"][0]["weight"] == 1.0
    weights = quota_weights(
        {"m": [{"node": "n1"}]},
        {"m": {"team_id": "a", "remaining_ratio": -3.0, "exhausted": False}},
    )
    assert weights["m"][0]["weight"] == EPSILON  # clamped to 0 -> degraded


def test_malformed_assignments_raise():
    with pytest.raises(ValueError):
        quota_weights({"m": [{"nope": "n1"}]}, {})
    with pytest.raises(ValueError):
        quota_weights(
            {"m": [{"node": "n1"}]},
            {"m": {"team_id": "a", "remaining_ratio": "lots", "exhausted": False}},
        )


# --- team_state_for_models -------------------------------------------------

class _FakeScopes:
    def __init__(self, budgets):
        # budgets: {(scope_type, scope_id): (admitted, info)}
        self.budgets = budgets

    def check_budget(self, scope_type, scope_id, tokens=0):
        return self.budgets[(scope_type, scope_id)]


def _info(limit, used):
    return {
        "limited": True, "limit": limit, "used": used,
        "remaining": max(0, limit - used),
    }


def test_team_state_for_models_builds_ratios():
    policies = {
        "llama-8b": {"team_id": "alpha", "replicas": 2},
        "qwen-32b": {"team_id": "beta", "replicas": 1},
        "orphan": {"replicas": 1},  # no team_id -> omitted (neutral routing)
    }
    scopes = _FakeScopes({
        ("team", "alpha"): (True, _info(1000, 250)),
        ("team", "beta"): (False, _info(1000, 1000)),
    })
    state = team_state_for_models(policies, scopes)
    assert state["llama-8b"] == {
        "team_id": "alpha", "remaining_ratio": pytest.approx(0.75),
        "exhausted": False}
    assert state["qwen-32b"]["exhausted"] is True
    assert state["qwen-32b"]["remaining_ratio"] == 0.0
    assert "orphan" not in state


def test_team_state_unlimited_team_is_full_ratio():
    scopes = _FakeScopes({
        ("team", "alpha"): (True, {"limited": False, "limit": None,
                                  "used": 0, "remaining": None}),
    })
    state = team_state_for_models({"m": {"team_id": "alpha"}}, scopes)
    assert state["m"]["remaining_ratio"] == 1.0
    assert state["m"]["exhausted"] is False


def test_team_state_store_outage_fails_closed():
    class Broken:
        def check_budget(self, *a):
            raise ConnectionError("postgres down")

    with pytest.raises(ConnectionError):
        team_state_for_models({"m": {"team_id": "alpha"}}, Broken())
