"""Chantier 4 — LiteLLM reload, routing_weights, canary entries.

Covers ``services.model_manager.litellm_sync``:
- ``routing_weights`` in ``sync_from_fleet`` (weights applied per
  (model x node); ``None`` = current behaviour, no ``weight`` key),
- ``{model}-canary`` entries from ``canary_policies`` + ``node_versions``,
- ``reload_config()``: hot path OK -> no restart; hot path KO ->
  fallback restart; hot disabled -> direct restart.
"""
import os

import pytest
import yaml

from services.model_manager import litellm_sync

os.environ.setdefault("FLEET_INFERENCE_SCHEME", "https")


@pytest.fixture(autouse=True)
def clean_restart_tracking():
    litellm_sync._reset_restart_tracking()
    yield
    litellm_sync._reset_restart_tracking()


def write_seed_config(path):
    config = {
        "general_settings": {"master_key": "sk-placeholder"},
        "model_list": [
            {
                "model_name": "fast-model",
                "litellm_params": {
                    "model": "openai/fast-model",
                    "api_base": "http://127.0.0.1:8000/v1",
                    "api_key": "none",
                },
                "model_info": {"managed_by": "human-operator"},
            }
        ],
    }
    with open(path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(config, handle)


def read_config(path):
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def fleet_entries(path):
    return [
        entry for entry in read_config(path).get("model_list") or []
        if (entry.get("model_info") or {}).get("managed_by")
        == litellm_sync.FLEET_MANAGED_BY
    ]


def entry_by_name(entries, model_name):
    return [e for e in entries if e.get("model_name") == model_name]


class FakeRun:
    def __init__(self):
        self.calls = []

    def __call__(self, command, cwd):
        self.calls.append((command, cwd))

        class Proc:
            returncode = 0
            stdout = "ok"

        return Proc()


class FakeHttp:
    """Injectable http_post double for /config/update."""

    def __init__(self, status_code=200, explode=False):
        self.status_code = status_code
        self.explode = explode
        self.calls = []

    def __call__(self, url, body, headers, timeout):
        self.calls.append((url, body, headers))
        if self.explode:
            raise ConnectionError("proxy down")
        assert "Authorization" in headers
        assert "Bearer" in headers["Authorization"]

        class Resp:
            status_code = self.status_code

        return Resp()


# --- routing_weights ---------------------------------------------------------

def test_routing_weights_applied_per_node(tmp_path):
    config_path = str(tmp_path / "config.yaml")
    write_seed_config(config_path)

    result = litellm_sync.sync_from_fleet(
        [("model-a", "gpu-01"), ("model-a", "gpu-02")],
        config_path=config_path, restart=False,
        routing_weights={"model-a": [
            {"node": "gpu-01", "weight": 3.0},
            {"node": "gpu-02", "weight": 1.0},
        ]},
    )
    assert result["changed"] is True
    params = {e["litellm_params"]["api_base"]: e["litellm_params"]
              for e in fleet_entries(config_path)}
    assert params["https://gpu-01:8000/v1"]["weight"] == 3.0
    assert params["https://gpu-02:8000/v1"]["weight"] == 1.0


def test_routing_weights_none_is_unchanged(tmp_path):
    """None -> no 'weight' key at all (byte-compatible with old output)."""
    config_path = str(tmp_path / "config.yaml")
    write_seed_config(config_path)

    litellm_sync.sync_from_fleet(
        [("model-a", "gpu-01")], config_path=config_path, restart=False)
    params = fleet_entries(config_path)[0]["litellm_params"]
    assert "weight" not in params


def test_routing_weights_unknown_entries_ignored(tmp_path):
    """A weight for a drained/unknown (model, node) must not fail the sync."""
    config_path = str(tmp_path / "config.yaml")
    write_seed_config(config_path)

    result = litellm_sync.sync_from_fleet(
        [("model-a", "gpu-01")],
        config_path=config_path, restart=False,
        routing_weights={
            "model-a": [{"node": "gpu-01", "weight": 2.0}],
            "model-gone": [{"node": "gpu-99", "weight": 5.0}],
        },
    )
    assert result["changed"] is True
    params = fleet_entries(config_path)[0]["litellm_params"]
    assert params["weight"] == 2.0


@pytest.mark.parametrize("weights", [
    {"model-a": [{"node": "gpu-01", "weight": 0}]},
    {"model-a": [{"node": "gpu-01", "weight": -1.5}]},
    {"model-a": [{"node": "gpu-01", "weight": "lots"}]},
    {"model-a": [{"node": "gpu-01"}]},
    {"model-a": "not-a-list"},
    "not-a-dict",
])
def test_routing_weights_invalid_rejected(tmp_path, weights):
    config_path = str(tmp_path / "config.yaml")
    write_seed_config(config_path)
    with pytest.raises(ValueError):
        litellm_sync.sync_from_fleet(
            [("model-a", "gpu-01")], config_path=config_path,
            restart=False, routing_weights=weights)


def test_routing_weights_change_triggers_rewrite(tmp_path):
    """A weight change alone (same membership) counts as a change."""
    config_path = str(tmp_path / "config.yaml")
    write_seed_config(config_path)
    run = FakeRun()
    placements = [("model-a", "gpu-01")]

    first = litellm_sync.sync_from_fleet(
        placements, config_path=config_path, restart=False,
        routing_weights={"model-a": [{"node": "gpu-01", "weight": 1.0}]})
    second = litellm_sync.sync_from_fleet(
        placements, config_path=config_path, restart=False, run_fn=run,
        routing_weights={"model-a": [{"node": "gpu-01", "weight": 9.0}]})
    assert first["changed"] is True
    assert second["changed"] is True
    params = fleet_entries(config_path)[0]["litellm_params"]
    assert params["weight"] == 9.0


# --- canary entries ----------------------------------------------------------

CANARY = {"model-a": {"canary_version": "v2", "canary_traffic_percent": 10}}


def test_canary_entry_generated_for_version_nodes(tmp_path):
    config_path = str(tmp_path / "config.yaml")
    write_seed_config(config_path)

    result = litellm_sync.sync_from_fleet(
        [("model-a", "gpu-01"), ("model-a", "gpu-02")],
        config_path=config_path, restart=False,
        node_versions={"gpu-01": "v1", "gpu-02": "v2"},
        canary_policies=CANARY,
    )
    assert result["changed"] is True
    entries = fleet_entries(config_path)

    canary = entry_by_name(entries, "model-a-canary")
    assert len(canary) == 1
    assert canary[0]["litellm_params"]["api_base"] == "https://gpu-02:8000/v1"
    info = canary[0]["model_info"]
    assert info["canary_of"] == "model-a"
    assert info["canary_version"] == "v2"
    assert info["canary_traffic_percent"] == 10

    # canary nodes are excluded from the stable pool
    stable = entry_by_name(entries, "model-a")
    assert [e["litellm_params"]["api_base"] for e in stable] == ["https://gpu-01:8000/v1"]

    assert result["canary"]["model-a"]["nodes"] == ["gpu-02"]
    assert result["canary"]["model-a"]["canary_traffic_percent"] == 10


def test_canary_absent_when_percent_zero_or_policy_missing(tmp_path):
    config_path = str(tmp_path / "config.yaml")
    write_seed_config(config_path)

    for policies in (None, {},
                     {"model-a": {"canary_version": "v2", "canary_traffic_percent": 0}},
                     {"model-a": {"canary_traffic_percent": 10}}):
        litellm_sync._reset_restart_tracking()
        result = litellm_sync.sync_from_fleet(
            [("model-a", "gpu-01")], config_path=config_path, restart=False,
            node_versions={"gpu-01": "v2"}, canary_policies=policies)
        names = [e["model_name"] for e in fleet_entries(config_path)]
        assert "model-a-canary" not in names, policies
        assert result["canary"] == {}


def test_canary_absent_when_no_node_carries_version(tmp_path):
    """Policy active but no placed node reports the canary version: no
    canary entry, stable pool untouched."""
    config_path = str(tmp_path / "config.yaml")
    write_seed_config(config_path)

    result = litellm_sync.sync_from_fleet(
        [("model-a", "gpu-01")], config_path=config_path, restart=False,
        node_versions={"gpu-01": "v1"}, canary_policies=CANARY)
    names = [e["model_name"] for e in fleet_entries(config_path)]
    assert names == ["model-a"]
    assert result["canary"] == {}


@pytest.mark.parametrize("policies", [
    {"model-a": {"canary_version": "v2", "canary_traffic_percent": 101}},
    {"model-a": {"canary_version": "v2", "canary_traffic_percent": "half"}},
])
def test_canary_invalid_percent_rejected(tmp_path, policies):
    config_path = str(tmp_path / "config.yaml")
    write_seed_config(config_path)
    with pytest.raises(ValueError):
        litellm_sync.sync_from_fleet(
            [("model-a", "gpu-01")], config_path=config_path,
            restart=False, canary_policies=policies)


def test_canary_weights_use_canary_model_key(tmp_path):
    """Weights for canary deployments are keyed by the generated
    '{model}-canary' name."""
    config_path = str(tmp_path / "config.yaml")
    write_seed_config(config_path)

    litellm_sync.sync_from_fleet(
        [("model-a", "gpu-01"), ("model-a", "gpu-02")],
        config_path=config_path, restart=False,
        node_versions={"gpu-01": "v1", "gpu-02": "v2"},
        canary_policies=CANARY,
        routing_weights={"model-a-canary": [{"node": "gpu-02", "weight": 4.0}]},
    )
    canary = entry_by_name(fleet_entries(config_path), "model-a-canary")[0]
    assert canary["litellm_params"]["weight"] == 4.0
    stable = entry_by_name(fleet_entries(config_path), "model-a")[0]
    assert "weight" not in stable["litellm_params"]


# --- reload_config -----------------------------------------------------------

def test_reload_config_hot_success_skips_restart(tmp_path):
    config_path = str(tmp_path / "config.yaml")
    write_seed_config(config_path)
    http = FakeHttp(status_code=200)
    run = FakeRun()

    result = litellm_sync.reload_config(
        config_path, hot_reload=True, run_fn=run,
        master_key="sk-test", http_post=http, verify_fn=lambda: True)

    assert result["path"] == "hot"
    assert result["restart"] is None
    assert run.calls == [], "a successful hot reload must not restart"
    url, body, _ = http.calls[0]
    assert url.endswith("/config/update")
    assert [e["model_name"] for e in body["model_list"]] == ["fast-model"]


def test_reload_config_hot_failure_falls_back_to_restart(tmp_path):
    config_path = str(tmp_path / "config.yaml")
    write_seed_config(config_path)
    run = FakeRun()

    for http in (FakeHttp(status_code=500),
                 FakeHttp(explode=True),
                 FakeHttp(status_code=200)):  # 200 but verify fails
        verify = (lambda: False) if http.status_code == 200 else (lambda: True)
        result = litellm_sync.reload_config(
            config_path, hot_reload=True, run_fn=run,
            master_key="sk-test", http_post=http, verify_fn=verify)

        assert result["path"] == "restart", http.status_code
        assert result["restart"]["exit_code"] == 0
    assert len(run.calls) == 3


def test_reload_config_disabled_goes_straight_to_restart(tmp_path):
    config_path = str(tmp_path / "config.yaml")
    write_seed_config(config_path)
    http = FakeHttp(status_code=200)
    run = FakeRun()

    result = litellm_sync.reload_config(
        config_path, run_fn=run, master_key="sk-test", http_post=http)

    assert result["path"] == "restart"
    assert http.calls == [], "hot path must not be attempted when disabled"
    assert len(run.calls) == 1


def test_reload_config_hot_without_key_falls_back(tmp_path, monkeypatch):
    """No master key available -> no hot attempt possible -> restart."""
    config_path = str(tmp_path / "config.yaml")
    write_seed_config(config_path)
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)
    http = FakeHttp(status_code=200)
    run = FakeRun()

    result = litellm_sync.reload_config(
        config_path, hot_reload=True, run_fn=run, http_post=http)

    assert result["path"] == "restart"
    assert http.calls == []
    assert len(run.calls) == 1


def test_sync_from_fleet_hot_reload_opt_in(tmp_path):
    """hot_reload=True: hot OK -> anti-flap untouched, no restart call."""
    config_path = str(tmp_path / "config.yaml")
    write_seed_config(config_path)
    http = FakeHttp(status_code=200)
    run = FakeRun()

    result = litellm_sync.sync_from_fleet(
        [("model-a", "gpu-01")], config_path=config_path,
        restart=True, run_fn=run, hot_reload=True,
        master_key="sk-test", http_post=http, verify_fn=lambda: True,
        restart_cooldown_s=90.0)

    assert result["changed"] is True
    assert result["restart"]["path"] == "hot"
    assert run.calls == []


def test_sync_from_fleet_hot_failure_uses_cooldown_restart(tmp_path):
    """Hot KO -> restart goes through the anti-flap cooldown."""
    config_path = str(tmp_path / "config.yaml")
    write_seed_config(config_path)
    run = FakeRun()

    first = litellm_sync.sync_from_fleet(
        [("model-a", "gpu-01")], config_path=config_path,
        restart=True, run_fn=run, hot_reload=True,
        master_key="sk-test", http_post=FakeHttp(status_code=500),
        restart_cooldown_s=90.0)
    assert first["restart"]["exit_code"] == 0
    assert len(run.calls) == 1

    second = litellm_sync.sync_from_fleet(
        [("model-a", "gpu-01"), ("model-b", "gpu-02")],
        config_path=config_path, restart=True, run_fn=run, hot_reload=True,
        master_key="sk-test", http_post=FakeHttp(status_code=500),
        restart_cooldown_s=90.0)
    assert second["restart"] == "deferred"
    assert len(run.calls) == 1


def test_sync_hot_reload_kwarg_defaults_to_restart(tmp_path):
    """sync() keeps restart-only behaviour unless hot_reload=True."""
    from services.model_manager import registry as registry_module

    config_path = str(tmp_path / "config.yaml")
    write_seed_config(config_path)

    class Store:
        def all(self):
            return [{"name": "model-a", "status": registry_module.STATUS_RUNNING}]

    run = FakeRun()
    result = litellm_sync.sync(Store(), config_path=config_path, run_fn=run)
    assert result["changed"] is True
    assert result["restart"]["exit_code"] == 0
    assert len(run.calls) == 1
