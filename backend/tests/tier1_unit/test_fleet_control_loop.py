"""Control-loop correctness and optimisation tests.

Covers the fleet ``sync_once`` cycle in ``services.fleet.litellm_daemon``
(heartbeat recording bug fix + concurrent heartbeat pushes) and the
LiteLLM restart anti-flap cooldown in ``services.model_manager.litellm_sync``.

A tiny stub registry is used on purpose: ``sync_once`` only needs the six
registry methods below, and the stub keeps these tests independent of the
SQL-emulating executor doubles.
"""
import os
import time

import pytest
import yaml

from services.fleet.litellm_daemon import sync_once
from services.model_manager import litellm_sync

os.environ.setdefault("FLEET_INFERENCE_SCHEME", "https")


class StubRegistry:
    """Minimal FleetRegistry double for sync_once()."""

    def __init__(self, nodes):
        # nodes: list of dicts with at least name/address/vram_total_gb
        self._nodes = {node["name"]: dict(node) for node in nodes}
        self.heartbeats = []          # names, in record order
        self.placements = []          # (model, node, state) records
        self.policies = {}

    # -- methods used by sync_once -------------------------------------
    def mark_stale(self, max_age_seconds=30.0):
        return 0

    def get_desired_state(self):
        return dict(self.policies)

    def healthy_nodes(self, max_age_seconds=30.0):
        return [dict(node) for node in self._nodes.values()]

    def list_placements(self):
        return []

    def heartbeat(self, name, state=None):
        if name not in self._nodes:
            return False
        self.heartbeats.append(name)
        return True

    def record_placement(self, model, node, state):
        self.placements.append((model, node, state))


def make_node(name, address=None, vram_gb=48.0):
    return {
        "name": name,
        "address": address or name,
        "vram_total_gb": vram_gb,
        "gpu_model": "stub-gpu",
        "gpu_count": 1,
        "compute_capability": "9.0",
        "status": "approved",
    }


def ok_reply(*models_running):
    return {
        "node_name": "stub",
        "actual": {
            "models": [
                {"name": model, "running": True} for model in models_running
            ]
        },
    }


def null_sync(placements):
    return {"changed": False, "models": {}}


# --- bug fix: the daemon's successful push IS the registry heartbeat --------

def test_sync_once_records_heartbeat_for_contacted_nodes():
    """Regression test: without registry.heartbeat() on a successful push,
    mark_stale() evicts every node 30 s after approval and the fleet drains
    itself of traffic. sync_once must record the heartbeat itself."""
    registry = StubRegistry([make_node("gpu-01")])
    registry.policies = {"llama-3-8b": {"replicas": 1, "vram_per_replica_gb": 20.0}}

    contacted = []

    def heartbeat_fn(name, address, desired, version):
        contacted.append(name)
        return ok_reply("llama-3-8b")

    summary = sync_once(registry, heartbeat_fn=heartbeat_fn, sync_fn=null_sync)

    assert contacted == ["gpu-01"]
    assert registry.heartbeats == ["gpu-01"], (
        "a successful heartbeat push must be recorded in the registry"
    )
    assert summary["node_errors"] == {}
    # placement recorded desired=running / actual=running
    assert ("llama-3-8b", "gpu-01") in [
        (model, node) for model, node, _ in registry.placements
    ]


def test_sync_once_does_not_record_heartbeat_on_push_failure():
    registry = StubRegistry([make_node("gpu-01"), make_node("gpu-02")])

    def heartbeat_fn(name, address, desired, version):
        if name == "gpu-01":
            raise ConnectionError("node down")
        return ok_reply()

    summary = sync_once(registry, heartbeat_fn=heartbeat_fn, sync_fn=null_sync)

    assert summary["node_errors"]["gpu-01"] == "node down"
    assert registry.heartbeats == ["gpu-02"]
    assert "gpu-02" not in summary["node_errors"]


def test_sync_once_flags_node_unknown_to_registry():
    """The push succeeded but the registry no longer knows the node: the
    cycle must report it instead of recording phantom placements."""
    registry = StubRegistry([make_node("gpu-01")])
    registry.heartbeat = lambda name, state=None: False

    summary = sync_once(
        registry, heartbeat_fn=lambda n, a, d, v: ok_reply(), sync_fn=null_sync
    )
    assert summary["node_errors"]["gpu-01"] == "node unknown to the registry"
    assert registry.placements == []


def test_sync_once_contacts_all_nodes_despite_slow_peer():
    """Concurrent pushes: one slow node must not starve the others."""
    registry = StubRegistry(
        [make_node("gpu-01"), make_node("gpu-02"), make_node("gpu-03")]
    )
    contacted = []

    def heartbeat_fn(name, address, desired, version):
        if name == "gpu-02":
            time.sleep(0.3)
        contacted.append(name)
        return ok_reply()

    summary = sync_once(registry, heartbeat_fn=heartbeat_fn, sync_fn=null_sync)

    assert sorted(contacted) == ["gpu-01", "gpu-02", "gpu-03"]
    assert sorted(registry.heartbeats) == ["gpu-01", "gpu-02", "gpu-03"]
    assert summary["node_errors"] == {}


# --- anti-flap cooldown on LiteLLM restarts ---------------------------------

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


def managed_names(path):
    with open(path, encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    return [
        entry.get("model_name")
        for entry in config.get("model_list") or []
        if (entry.get("model_info") or {}).get("managed_by")
        == litellm_sync.FLEET_MANAGED_BY
    ]


class FakeRun:
    def __init__(self):
        self.calls = []

    def __call__(self, command, cwd):
        self.calls.append((command, cwd))

        class Proc:
            returncode = 0
            stdout = "ok"

        return Proc()


def test_restart_cooldown_defers_flapping_restart(tmp_path):
    """First membership change restarts immediately; a second change inside
    the cooldown window is deferred, not executed."""
    config_path = str(tmp_path / "config.yaml")
    write_seed_config(config_path)
    run = FakeRun()

    first = litellm_sync.sync_from_fleet(
        [("model-a", "gpu-01")], config_path=config_path,
        restart=True, run_fn=run, restart_cooldown_s=90.0,
    )
    assert first["changed"] is True
    assert isinstance(first["restart"], dict)
    assert len(run.calls) == 1

    second = litellm_sync.sync_from_fleet(
        [("model-a", "gpu-01"), ("model-b", "gpu-02")],
        config_path=config_path, restart=True, run_fn=run,
        restart_cooldown_s=90.0,
    )
    assert second["changed"] is True
    assert second["restart"] == "deferred"
    assert len(run.calls) == 1, "flapping change must not restart LiteLLM again"
    # the config on disk still tracks the latest membership
    assert sorted(managed_names(config_path)) == ["model-a", "model-b"]


def test_deferred_restart_fires_once_stable(tmp_path, monkeypatch):
    """A deferred restart fires on the first later call once the cooldown
    has elapsed — it is never silently dropped."""
    config_path = str(tmp_path / "config.yaml")
    write_seed_config(config_path)
    run = FakeRun()

    litellm_sync.sync_from_fleet(
        [("model-a", "gpu-01")], config_path=config_path,
        restart=True, run_fn=run, restart_cooldown_s=90.0,
    )
    assert len(run.calls) == 1
    litellm_sync.sync_from_fleet(
        [("model-a", "gpu-01"), ("model-b", "gpu-02")],
        config_path=config_path, restart=True, run_fn=run,
        restart_cooldown_s=90.0,
    )
    assert len(run.calls) == 1  # deferred

    # cooldown elapsed, config now stable: the pending restart fires
    monkeypatch.setattr(
        litellm_sync, "_last_fleet_restart_ts", time.monotonic() - 91.0)
    third = litellm_sync.sync_from_fleet(
        [("model-a", "gpu-01"), ("model-b", "gpu-02")],
        config_path=config_path, restart=True, run_fn=run,
        restart_cooldown_s=90.0,
    )
    assert third["changed"] is False
    assert isinstance(third["restart"], dict)
    assert len(run.calls) == 2


def test_restart_cooldown_zero_is_immediate(tmp_path):
    """restart_cooldown_s=0 preserves the old always-restart behaviour."""
    config_path = str(tmp_path / "config.yaml")
    write_seed_config(config_path)
    run = FakeRun()

    for i in range(3):
        result = litellm_sync.sync_from_fleet(
            [(f"model-{i}", "gpu-01")], config_path=config_path,
            restart=True, run_fn=run, restart_cooldown_s=0,
        )
        assert isinstance(result["restart"], dict)
    assert len(run.calls) == 3


def test_no_restart_requested_leaves_tracking_alone(tmp_path):
    config_path = str(tmp_path / "config.yaml")
    write_seed_config(config_path)
    run = FakeRun()

    result = litellm_sync.sync_from_fleet(
        [("model-a", "gpu-01")], config_path=config_path,
        restart=False, run_fn=run,
    )
    assert result["changed"] is True
    assert result["restart"] is None
    assert run.calls == []


# --- vLLM fleet throughput profile ------------------------------------------

def test_serve_yaml_throughput_defaults():
    """Guards the throughput-oriented fleet profile in serve.yaml."""
    path = os.path.join(
        os.path.dirname(litellm_sync.__file__), "../../config/vllm/serve.yaml")
    with open(os.path.abspath(path), encoding="utf-8") as handle:
        settings = yaml.safe_load(handle)
    assert settings["max-num-seqs"] == 128
    assert settings["max-num-batched-tokens"] == 16384
    assert settings["gpu-memory-utilization"] == 0.90
    assert settings["enable-chunked-prefill"] is True
    assert settings["enable-prefix-caching"] is True
