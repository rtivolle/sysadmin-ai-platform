"""Integration tests: autoscaling inside ``sync_once()`` (litellm_daemon).

Uses the same stub-registry style as ``test_fleet_control_loop.py``: the
whole register -> heartbeat -> converge -> autoscale cycle runs with fakes,
so no database, nodes, LiteLLM or GPU are needed.

Covers: autoscale disabled via ``FLEET_AUTOSCALE_ENABLED=0`` (no policy
writes, rest of the cycle untouched), autoscale enabled with a queue spike
(policy ``replicas`` bumped through ``set_desired_state`` with all other
policy fields preserved), cooldown persistence across two cycles via the
``autoscale_events`` ledger, and the ``routing_weights`` handoff to
``sync_from_fleet``.
"""
import os

import pytest

from services.fleet import litellm_daemon
from services.fleet.litellm_daemon import sync_once
from services.model_manager import litellm_sync

os.environ.setdefault("FLEET_INFERENCE_SCHEME", "https")

MODEL = "llama-3-8b"


class LedgerExecutor:
    """Fake Executor that actually persists autoscale_events in memory."""

    def __init__(self):
        self.statements = []
        self.events = []  # (model, action, at)

    def execute(self, sql, params=()):
        self.statements.append((sql, tuple(params)))
        if "INSERT INTO autoscale_events" in sql:
            self.events.append((params[0], params[1], float(params[2])))
        return 1

    def query(self, sql, params=()):
        self.statements.append((sql, tuple(params)))
        if "MAX(at)" in sql:
            latest = {}
            for model, _action, at in self.events:
                latest[model] = max(latest.get(model, 0.0), at)
            return [(model, at) for model, at in latest.items()]
        return []


class AutoscaleStubRegistry:
    """StubRegistry + set_desired_state + a fake _executor for the ledger."""

    def __init__(self, nodes):
        self._nodes = {node["name"]: dict(node) for node in nodes}
        self._executor = LedgerExecutor()
        self.policies = {}
        self.writes = []  # (model, policy) passed to set_desired_state

    def mark_stale(self, max_age_seconds=30.0):
        return 0

    def get_desired_state(self):
        return {model: dict(policy) for model, policy in self.policies.items()}

    def healthy_nodes(self, max_age_seconds=30.0):
        return [dict(node) for node in self._nodes.values()]

    def list_placements(self):
        return []

    def heartbeat(self, name, state=None):
        return name in self._nodes

    def record_placement(self, model, node, state):
        pass

    def set_desired_state(self, model, policy):
        self.writes.append((model, dict(policy)))
        self.policies[model] = dict(policy)


def make_node(name="n1"):
    return {"name": name, "address": name, "vram_total_gb": 48.0,
            "gpu_model": "stub-gpu", "gpu_count": 1,
            "compute_capability": "9.0", "status": "approved"}


def make_policy(**overrides):
    policy = {"replicas": 1, "engine": "vllm", "vram_per_replica_gb": 4.0,
              "min_replicas": 1, "max_replicas": 4,
              "params": {"gpu_memory_utilization": 0.9}}
    policy.update(overrides)
    return policy


def ok_reply(*models_running):
    return {"node_name": "stub",
            "actual": {"models": [{"name": m, "running": True}
                                  for m in models_running]}}


def heartbeat_fn(name, address, desired, version):
    return ok_reply()


def null_sync(placements):
    return {"changed": False, "models": {}}


def spike_metrics(queue_depth=50):
    return {MODEL: {"queue_depth": queue_depth, "ttft_p99_s": 0.2,
                    "latency_p99_s": 0.3}}


def idle_metrics():
    return {MODEL: {"queue_depth": 0, "ttft_p99_s": 0.1,
                    "latency_p99_s": 0.15}}


@pytest.fixture()
def registry():
    reg = AutoscaleStubRegistry([make_node()])
    reg.policies = {MODEL: make_policy()}
    return reg


def run_cycle(registry, monkeypatch, **kwargs):
    monkeypatch.delenv("FLEET_AUTOSCALE_ENABLED", raising=False)
    return sync_once(registry, heartbeat_fn=heartbeat_fn,
                     sync_fn=null_sync, **kwargs)


# --- kill switch ---------------------------------------------------------------
def test_autoscale_disabled_leaves_policies_alone(registry, monkeypatch):
    monkeypatch.setenv("FLEET_AUTOSCALE_ENABLED", "0")
    summary = sync_once(registry, heartbeat_fn=heartbeat_fn,
                        sync_fn=null_sync,
                        metrics_fn=lambda: spike_metrics())
    assert registry.writes == []
    assert registry.policies[MODEL]["replicas"] == 1
    assert summary["autoscale"] == {"enabled": False}
    # the rest of the cycle still ran
    assert summary["nodes"] == 1
    assert summary["litellm"] == {"changed": False, "models": {}}


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_autoscale_disabled_values(registry, monkeypatch, value):
    monkeypatch.setenv("FLEET_AUTOSCALE_ENABLED", value)
    summary = sync_once(registry, heartbeat_fn=heartbeat_fn,
                        sync_fn=null_sync,
                        metrics_fn=lambda: spike_metrics())
    assert registry.writes == []
    assert summary["autoscale"]["enabled"] is False


# --- scale-up writes ------------------------------------------------------------
def test_autoscale_scales_up_and_preserves_policy_fields(registry, monkeypatch):
    summary = run_cycle(registry, monkeypatch,
                        metrics_fn=lambda: spike_metrics())
    assert summary["autoscale"]["decisions"] == {MODEL: 2}
    assert len(registry.writes) == 1
    model, written = registry.writes[0]
    assert model == MODEL
    assert written["replicas"] == 2
    # read-modify-write: every other field survives
    assert written["engine"] == "vllm"
    assert written["vram_per_replica_gb"] == 4.0
    assert written["params"] == {"gpu_memory_utilization": 0.9}
    assert written["min_replicas"] == 1
    assert registry.policies[MODEL]["replicas"] == 2


def test_autoscale_idle_takes_no_action(registry, monkeypatch):
    summary = run_cycle(registry, monkeypatch,
                        metrics_fn=lambda: idle_metrics())
    assert summary["autoscale"]["decisions"] == {}
    assert registry.writes == []


def test_autoscale_no_metrics_takes_no_action(registry, monkeypatch):
    summary = run_cycle(registry, monkeypatch, metrics_fn=lambda: {})
    assert summary["autoscale"]["decisions"] == {}
    assert registry.writes == []


# --- cooldown persistence --------------------------------------------------------
def test_cooldown_survives_across_cycles(registry, monkeypatch):
    first = run_cycle(registry, monkeypatch,
                      metrics_fn=lambda: spike_metrics())
    assert first["autoscale"]["decisions"] == {MODEL: 2}
    # The scale event was persisted to the ledger ...
    assert registry._executor.events != []
    assert registry._executor.events[0][1] == "scale_up"
    # ... so the very next cycle (seconds later) does not scale again,
    # even though the queue is still spiking.
    second = run_cycle(registry, monkeypatch,
                       metrics_fn=lambda: spike_metrics())
    assert second["autoscale"]["decisions"] == {}
    assert len(registry.writes) == 1


def test_quota_headroom_caps_replicas_in_daemon(registry, monkeypatch):
    registry.policies = {MODEL: make_policy(replicas=1, max_replicas=8)}
    summary = run_cycle(registry, monkeypatch,
                        metrics_fn=lambda: spike_metrics())
    # No QuotaScopes module in this tree -> falls back to policy max_replicas.
    assert summary["autoscale"]["decisions"] == {MODEL: 2}


# --- routing weights handoff ------------------------------------------------------
def test_default_sync_fn_receives_routing_weights_kwarg(registry, monkeypatch):
    captured = {}

    def fake_sync_from_fleet(placements, **kwargs):
        captured["placements"] = placements
        captured["kwargs"] = kwargs
        return {"changed": False, "models": {}}

    monkeypatch.setattr(litellm_sync, "sync_from_fleet", fake_sync_from_fleet)
    monkeypatch.delenv("FLEET_AUTOSCALE_ENABLED", raising=False)
    sync_once(registry, heartbeat_fn=heartbeat_fn,
              metrics_fn=lambda: spike_metrics())
    assert "routing_weights" in captured["kwargs"]
    # distribution.quota_weights is live: one healthy node, no quota
    # signal -> neutral weight 1.0.
    assert captured["kwargs"]["routing_weights"] == {
        MODEL: [{"node": "n1", "weight": 1.0}]}
    assert captured["kwargs"]["restart"] is True


def test_autoscale_failure_does_not_break_converge(registry, monkeypatch):
    def boom():
        raise RuntimeError("metrics exploded")

    summary = run_cycle(registry, monkeypatch, metrics_fn=boom)
    assert summary["autoscale"]["error"] == "metrics exploded"
    assert registry.writes == []
    # the converge loop still completed
    assert summary["nodes"] == 1
    assert summary["litellm"] == {"changed": False, "models": {}}


def test_summary_reports_autoscale_state(registry, monkeypatch):
    summary = run_cycle(registry, monkeypatch,
                        metrics_fn=lambda: spike_metrics())
    info = summary["autoscale"]
    assert info["enabled"] is True
    assert info["metrics_models"] == 1
    assert info["routing_weights"] is True  # neutral 1.0, no quota signal
