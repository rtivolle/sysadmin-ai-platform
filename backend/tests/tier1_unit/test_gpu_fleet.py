"""Phase A GPU fleet: registry, scheduler, fleet litellm_sync, node agent, fleet API, daemon."""
import contextlib
import os
from datetime import datetime, timedelta, timezone

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.control_store.errors import ControlStoreIntegrityError, ControlStoreUnavailable
from services.control_store.fleet_registry import FleetRegistry, validate_node_name
from services.fleet.scheduler import compute_desired_state, shortfall, to_node_desired
from services.model_manager import litellm_sync, registry as registry_module
from services.model_manager.registry import ModelRegistry


def _now():
    return datetime.now(timezone.utc)


_NODE_COLS = ("name", "gpu_model", "gpu_count", "vram_total_gb", "compute_capability",
              "address", "status", "last_heartbeat", "approved_by", "approved_at",
              "created_at")


class FleetTableExecutor:
    """In-memory emulation of the three fleet tables (pattern: control_store_fakes)."""

    def __init__(self):
        self.nodes = {}
        self.placements = {}
        self.policies = {}
        self.statements = []
        self.fail_with = None

    def _row(self, name):
        return tuple(self.nodes[name][column] for column in _NODE_COLS)

    # -- Executor protocol -------------------------------------------------
    def query(self, sql, params=()):
        self._maybe_fail()
        self.statements.append((sql, tuple(params)))
        if "FROM gpu_nodes" in sql:
            if "WHERE name = %s" in sql:
                name = params[0]
                return [self._row(name)] if name in self.nodes else []
            if "WHERE status = %s" in sql:
                return [self._row(n) for n in sorted(self.nodes)
                        if self.nodes[n]["status"] == params[0]]
            if "status IN ('approved', 'active')" in sql:
                cutoff = _now() - timedelta(seconds=params[0])
                return [self._row(n) for n in sorted(self.nodes)
                        if self.nodes[n]["status"] in ("approved", "active")
                        and self.nodes[n]["last_heartbeat"] > cutoff]
            return [self._row(n) for n in sorted(self.nodes)]
        if "FROM fleet_desired_state" in sql:
            return [(model, policy) for model, policy in sorted(self.policies.items())]
        if "FROM model_placements" in sql:
            return [(model, node, value["desired_state"], value["actual_state"], value["updated_at"])
                    for (model, node), value in sorted(self.placements.items())]
        raise AssertionError(f"unexpected query: {sql}")

    def execute(self, sql, params=()):
        self._maybe_fail()
        self.statements.append((sql, tuple(params)))
        if "INSERT INTO gpu_nodes" in sql:
            name, gpu_model, gpu_count, vram, capability, address = params
            if name in self.nodes:
                self.nodes[name].update(
                    gpu_model=gpu_model, gpu_count=gpu_count, vram_total_gb=vram,
                    compute_capability=capability, address=address, last_heartbeat=_now())
            else:
                row = {column: None for column in _NODE_COLS}
                row.update(name=name, gpu_model=gpu_model, gpu_count=gpu_count,
                           vram_total_gb=vram, compute_capability=capability,
                           address=address, status="pending",
                           last_heartbeat=_now(), created_at=_now())
                self.nodes[name] = row
            return 1
        if "SET last_heartbeat = now()" in sql:
            (name,) = params
            node = self.nodes.get(name)
            if node is None:
                return 0
            node["last_heartbeat"] = _now()
            if node["status"] == "stale":
                node["status"] = "active"
            return 1
        if "SET status = 'approved'" in sql:
            approved_by, name = params
            node = self.nodes.get(name)
            if node and node["status"] == "pending":
                node.update(status="approved", approved_by=approved_by,
                            approved_at=_now(), last_heartbeat=_now())
                return 1
            return 0
        if "SET status = 'drained'" in sql:
            (name,) = params
            node = self.nodes.get(name)
            if node and node["status"] in ("approved", "active", "stale"):
                node["status"] = "drained"
                return 1
            return 0
        if "SET status = 'retired'" in sql:
            (name,) = params
            node = self.nodes.get(name)
            if node and node["status"] != "retired":
                node["status"] = "retired"
                return 1
            return 0
        if "SET status = 'stale'" in sql:
            cutoff = _now() - timedelta(seconds=params[0])
            marked = 0
            for node in self.nodes.values():
                if node["status"] == "active" and node["last_heartbeat"] < cutoff:
                    node["status"] = "stale"
                    marked += 1
            return marked
        if sql.strip().startswith("UPDATE gpu_nodes SET status = %s"):
            status, name = params
            if name not in self.nodes:
                return 0
            self.nodes[name]["status"] = status
            return 1
        if "INSERT INTO fleet_desired_state" in sql:
            model, policy = params
            self.policies[model] = policy
            return 1
        if "INSERT INTO model_placements" in sql:
            model, node, desired, actual = params
            self.placements[(model, node)] = {
                "desired_state": desired, "actual_state": actual, "updated_at": _now()}
            return 1
        raise AssertionError(f"unexpected statement: {sql}")

    @contextlib.contextmanager
    def transaction(self):
        yield self

    def _maybe_fail(self):
        if self.fail_with is not None:
            raise self.fail_with


@pytest.fixture
def fleet_exec():
    return FleetTableExecutor()


@pytest.fixture
def fleet_registry(fleet_exec):
    return FleetRegistry(fleet_exec)


# --- node name validation ---------------------------------------------------

def test_validate_node_name_rejects_everywhere(fleet_registry):
    for bad in ("../evil", "a/b", "", "-x", "x" * 65, "has space", None, 42):
        with pytest.raises((ValueError, TypeError)):
            validate_node_name(bad)
        with pytest.raises((ValueError, TypeError)):
            fleet_registry.get_node(bad) if isinstance(bad, str) else validate_node_name(bad)
    assert validate_node_name("gpu-01") == "gpu-01"
    assert validate_node_name("GPU_02.x") == "GPU_02.x"


# --- FleetRegistry ------------------------------------------------------------

def test_register_node_is_pending_and_reregister_keeps_status(fleet_registry):
    node = fleet_registry.register_node(
        "gpu-01", gpu_model="H100", gpu_count=8, vram_total_gb=640.0,
        compute_capability="9.0", address="10.0.0.11")
    assert node["status"] == "pending"
    assert node["gpu_count"] == 8
    assert node["vram_total_gb"] == 640.0
    assert node["approved_by"] is None

    assert fleet_registry.approve_node("gpu-01", "romaric") is True
    # Re-registration refreshes inventory and heartbeat but never the status.
    node = fleet_registry.register_node("gpu-01", gpu_count=8, vram_total_gb=640.0,
                                        address="10.0.0.12")
    assert node["status"] == "approved"
    assert node["address"] == "10.0.0.12"

    with pytest.raises(ControlStoreIntegrityError):
        fleet_registry.register_node("gpu-02", gpu_count=-1)


def test_get_node_unknown_returns_none(fleet_registry):
    assert fleet_registry.get_node("nope") is None


def test_list_nodes_with_status_filter(fleet_registry):
    fleet_registry.register_node("gpu-01")
    fleet_registry.register_node("gpu-02")
    fleet_registry.approve_node("gpu-01", "romaric")
    assert [n["name"] for n in fleet_registry.list_nodes()] == ["gpu-01", "gpu-02"]
    assert [n["name"] for n in fleet_registry.list_nodes(status="pending")] == ["gpu-02"]
    with pytest.raises(ValueError):
        fleet_registry.list_nodes(status="bogus")


def test_heartbeat_updates_timestamp_and_revives_stale(fleet_registry, fleet_exec):
    fleet_registry.register_node("gpu-01")
    fleet_registry.set_node_status("gpu-01", "stale")
    assert fleet_registry.heartbeat("gpu-01", {"vram_free_gb": 10}) is True
    assert fleet_registry.get_node("gpu-01")["status"] == "active"
    assert fleet_registry.heartbeat("unknown") is False
    with pytest.raises(ValueError):
        fleet_registry.heartbeat("gpu-01", state="not-a-dict")


def test_approve_node_is_a_human_gate(fleet_registry):
    fleet_registry.register_node("gpu-01")
    assert fleet_registry.approve_node("gpu-01", "romaric") is True
    assert fleet_registry.get_node("gpu-01")["status"] == "approved"
    assert fleet_registry.get_node("gpu-01")["approved_by"] == "romaric"
    # Not pending any more: second approval fails closed (returns False).
    assert fleet_registry.approve_node("gpu-01", "romaric") is False
    assert fleet_registry.approve_node("unknown", "romaric") is False
    fleet_registry.register_node("gpu-02")
    with pytest.raises(ControlStoreIntegrityError):
        fleet_registry.approve_node("gpu-02", "")


def test_set_node_status_validates(fleet_registry):
    fleet_registry.register_node("gpu-01")
    assert fleet_registry.set_node_status("gpu-01", "active") is True
    assert fleet_registry.get_node("gpu-01")["status"] == "active"
    assert fleet_registry.set_node_status("unknown", "active") is False
    with pytest.raises(ValueError):
        fleet_registry.set_node_status("gpu-01", "exploded")


def test_drain_and_decommission_transitions(fleet_registry):
    fleet_registry.register_node("gpu-01")
    # A pending node cannot be drained.
    assert fleet_registry.drain_node("gpu-01") is False
    fleet_registry.approve_node("gpu-01", "romaric")
    fleet_registry.set_node_status("gpu-01", "active")
    assert fleet_registry.drain_node("gpu-01") is True
    assert fleet_registry.get_node("gpu-01")["status"] == "drained"
    # Draining twice is a no-op failure, not an error.
    assert fleet_registry.drain_node("gpu-01") is False
    assert fleet_registry.decommission_node("gpu-01") is True
    assert fleet_registry.get_node("gpu-01")["status"] == "retired"
    assert fleet_registry.decommission_node("gpu-01") is False


def test_mark_stale_and_healthy_nodes(fleet_registry, fleet_exec):
    fleet_registry.register_node("gpu-01", address="10.0.0.11")
    fleet_registry.register_node("gpu-02", address="10.0.0.12")
    fleet_registry.set_node_status("gpu-01", "active")
    fleet_registry.set_node_status("gpu-02", "active")
    # gpu-02's heartbeat is old.
    fleet_exec.nodes["gpu-02"]["last_heartbeat"] = _now() - timedelta(seconds=120)
    assert fleet_registry.mark_stale(30) == 1
    assert fleet_registry.get_node("gpu-02")["status"] == "stale"
    healthy = fleet_registry.healthy_nodes(30)
    assert [n["name"] for n in healthy] == ["gpu-01"]
    assert fleet_registry.placeable_nodes(30) == healthy


def test_desired_state_roundtrip(fleet_registry):
    policy = {"replicas": 2, "engine": "vllm", "gpu_class": {"vram_min_gb": 40}}
    fleet_registry.set_desired_state("model-a", policy)
    assert fleet_registry.get_desired_state() == {"model-a": policy}
    fleet_registry.set_desired_state("model-a", {"replicas": 1})
    assert fleet_registry.get_desired_state() == {"model-a": {"replicas": 1}}
    with pytest.raises(ValueError):
        fleet_registry.set_desired_state("../evil", {})
    with pytest.raises(ValueError):
        fleet_registry.set_desired_state("model-a", ["not", "a", "dict"])


def test_record_placement_forms(fleet_registry):
    fleet_registry.register_node("gpu-01")
    fleet_registry.record_placement("model-a", "gpu-01",
                                   {"desired": "running", "actual": "running"})
    fleet_registry.record_placement("model-b", "gpu-01", "starting")
    placements = fleet_registry.list_placements()
    by_model = {p["model_name"]: p for p in placements}
    assert by_model["model-a"]["desired_state"] == "running"
    assert by_model["model-a"]["actual_state"] == "running"
    assert by_model["model-b"]["desired_state"] is None
    assert by_model["model-b"]["actual_state"] == "starting"
    with pytest.raises(ValueError):
        fleet_registry.record_placement("model-a", "gpu-01", 42)


def test_registry_fails_closed_when_store_is_down(fleet_registry, fleet_exec):
    fleet_exec.fail_with = ControlStoreUnavailable("postgres down")
    with pytest.raises(ConnectionError):
        fleet_registry.register_node("gpu-01")
    with pytest.raises(ConnectionError):
        fleet_registry.get_node("gpu-01")
    with pytest.raises(ConnectionError):
        fleet_registry.list_nodes()
    with pytest.raises(ConnectionError):
        fleet_registry.heartbeat("gpu-01")
    with pytest.raises(ConnectionError):
        fleet_registry.approve_node("gpu-01", "romaric")
    with pytest.raises(ConnectionError):
        fleet_registry.set_desired_state("model-a", {})
    with pytest.raises(ConnectionError):
        fleet_registry.record_placement("model-a", "gpu-01", "running")


# --- scheduler ------------------------------------------------------------------

def _fleet_fixture_nodes():
    return [
        {"name": "gpu-01", "vram_total_gb": 80, "compute_capability": "8.0", "status": "active"},
        {"name": "gpu-02", "vram_total_gb": 48, "compute_capability": "8.6", "status": "active"},
        {"name": "gpu-03", "vram_total_gb": 24, "compute_capability": "7.5", "status": "active"},
    ]


def _fleet_fixture_policies():
    return {
        "model-big": {
            "replicas": 2, "engine": "vllm", "vram_per_replica_gb": 40,
            "gpu_class": {"vram_min_gb": 40, "compute_capability_min": "8.0"},
            "params": {"gpu_memory_utilization": 0.85},
        },
        "model-small": {
            "replicas": 1, "engine": "vllm", "vram_per_replica_gb": 12,
            "gpu_class": {"vram_min_gb": 16}, "params": {},
        },
    }


def test_scheduler_bin_packs_two_models_on_three_heterogeneous_nodes():
    assignments = compute_desired_state(_fleet_fixture_policies(), _fleet_fixture_nodes())
    # model-big needs 40 GB and cap >= 8.0: only gpu-01 and gpu-02 qualify
    # (gpu-03 is too small and too old). Replicas spread across distinct nodes.
    big_hosts = sorted(name for name, items in assignments.items()
                       for item in items if item["model"] == "model-big")
    assert big_hosts == ["gpu-01", "gpu-02"]
    # model-small fits anywhere; it lands on the node with the most free VRAM.
    small_hosts = [name for name, items in assignments.items()
                   for item in items if item["model"] == "model-small"]
    assert small_hosts == ["gpu-01"]
    assert shortfall(_fleet_fixture_policies(), assignments) == {}
    desired = to_node_desired(assignments["gpu-01"])
    assert desired["model-big"] == {"action": "start",
                                    "params": {"gpu_memory_utilization": 0.85}}


def test_scheduler_excludes_stale_nodes():
    nodes = _fleet_fixture_nodes()
    nodes[0]["status"] = "stale"
    nodes[1]["status"] = "stale"
    assignments = compute_desired_state(_fleet_fixture_policies(), nodes)
    # Only gpu-03 remains: model-big fits nowhere (vram_min + capability),
    # model-small lands on gpu-03.
    assert shortfall(_fleet_fixture_policies(), assignments) == {"model-big": 2}
    assert [item["model"] for item in assignments["gpu-03"]] == ["model-small"]


def test_scheduler_honors_free_vram_and_spreads_replicas():
    nodes = [
        {"name": "gpu-01", "vram_total_gb": 80, "vram_free_gb": 45,
         "compute_capability": "9.0", "status": "active"},
        {"name": "gpu-02", "vram_total_gb": 80, "vram_free_gb": 80,
         "compute_capability": "9.0", "status": "active"},
    ]
    policies = {"model-a": {"replicas": 2, "vram_per_replica_gb": 40, "params": {}}}
    assignments = compute_desired_state(policies, nodes)
    # First replica -> gpu-02 (most free VRAM); the second prefers a node
    # that does not already host the model, so it lands on gpu-01.
    hosts = sorted(name for name, items in assignments.items()
                   for item in items if item["model"] == "model-a")
    assert hosts == ["gpu-01", "gpu-02"]


def test_scheduler_rejects_invalid_policies():
    nodes = _fleet_fixture_nodes()
    with pytest.raises(ValueError):
        compute_desired_state({"m": {"replicas": -1}}, nodes)
    with pytest.raises(ValueError):
        compute_desired_state({"m": {"vram_per_replica_gb": "lots"}}, nodes)
    with pytest.raises(ValueError):
        compute_desired_state({"m": {"params": ["x"]}}, nodes)
    assert compute_desired_state({}, nodes) == {}


def test_scheduler_capability_comparison():
    nodes = [{"name": "gpu-01", "vram_total_gb": 80, "compute_capability": "8.6",
              "status": "active"}]
    ok = {"m": {"replicas": 1, "vram_per_replica_gb": 10,
                "gpu_class": {"compute_capability_min": "8.0"}}}
    too_new = {"m": {"replicas": 1, "vram_per_replica_gb": 10,
                     "gpu_class": {"compute_capability_min": "9.0"}}}
    assert compute_desired_state(ok, nodes)["gpu-01"]
    assert shortfall(too_new, compute_desired_state(too_new, nodes)) == {"m": 1}


# --- sync_from_fleet ------------------------------------------------------------

@pytest.fixture
def litellm_cfg(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"model_list": [
        {"model_name": "fast-model", "litellm_params": {"model": "openai/x"}},
        {"model_name": "legacy-local", "litellm_params": {"model": "openai/y"},
         "model_info": {"managed_by": "sysadmin-model-manager"}},
    ]}))
    return str(cfg)


def _entries(cfg_path):
    return yaml.safe_load(open(cfg_path, encoding="utf-8").read())["model_list"]


def test_sync_from_fleet_generates_one_entry_per_model_x_node(litellm_cfg):
    placements = [("model-a", "10.0.0.11"), ("model-a", "10.0.0.12"), ("model-b", "10.0.0.11")]
    result = litellm_sync.sync_from_fleet(placements, config_path=litellm_cfg, restart=False)
    assert result["changed"] is True
    entries = _entries(litellm_cfg)
    fleet = [e for e in entries
             if (e.get("model_info") or {}).get("managed_by") == "sysadmin-fleet-manager"]
    assert len(fleet) == 3
    by_base = {(e["model_name"], e["litellm_params"]["api_base"]) for e in fleet}
    assert by_base == {
        ("model-a", "https://10.0.0.11:8000/v1"),
        ("model-a", "https://10.0.0.12:8000/v1"),
        ("model-b", "https://10.0.0.11:8000/v1"),
    }
    assert result["models"] == {"model-a": ["10.0.0.11", "10.0.0.12"], "model-b": ["10.0.0.11"]}


def test_sync_from_fleet_preserves_handwritten_and_local_manager_entries(litellm_cfg):
    litellm_sync.sync_from_fleet([("model-a", "10.0.0.11")], config_path=litellm_cfg,
                                 restart=False)
    entries = _entries(litellm_cfg)
    by_name = {e["model_name"]: e for e in entries}
    # Hand-written entry untouched.
    assert by_name["fast-model"]["litellm_params"]["model"] == "openai/x"
    # The local model manager's own block untouched.
    assert (by_name["legacy-local"].get("model_info") or {}).get("managed_by") \
        == "sysadmin-model-manager"
    assert litellm_sync.managed_model_names(litellm_cfg) == ["legacy-local"]


def test_sync_from_fleet_is_idempotent_and_removes_departed_nodes(litellm_cfg):
    placements = [("model-a", "10.0.0.11"), ("model-a", "10.0.0.12")]
    assert litellm_sync.sync_from_fleet(placements, config_path=litellm_cfg,
                                       restart=False)["changed"] is True
    assert litellm_sync.sync_from_fleet(placements, config_path=litellm_cfg,
                                       restart=False)["changed"] is False
    # A duplicate placement does not create a duplicate entry.
    assert litellm_sync.sync_from_fleet(placements + [("model-a", "10.0.0.11")],
                                       config_path=litellm_cfg,
                                       restart=False)["changed"] is False
    # A departed node is removed from the generated block.
    result = litellm_sync.sync_from_fleet([("model-a", "10.0.0.11")],
                                         config_path=litellm_cfg, restart=False)
    assert result["changed"] is True
    fleet = [e for e in _entries(litellm_cfg)
             if (e.get("model_info") or {}).get("managed_by") == "sysadmin-fleet-manager"]
    assert [(e["model_name"], e["litellm_params"]["api_base"]) for e in fleet] == [
        ("model-a", "https://10.0.0.11:8000/v1")]


def test_sync_from_fleet_validates_inputs_and_scheme(litellm_cfg, monkeypatch):
    with pytest.raises(ValueError):
        litellm_sync.sync_from_fleet([("model-a", "not a host!")],
                                     config_path=litellm_cfg, restart=False)
    with pytest.raises(ValueError):
        litellm_sync.sync_from_fleet([("../evil", "10.0.0.11")],
                                     config_path=litellm_cfg, restart=False)
    monkeypatch.setenv("FLEET_INFERENCE_SCHEME", "http")
    litellm_sync.sync_from_fleet([("model-a", "10.0.0.11")], config_path=litellm_cfg,
                                 restart=False)
    fleet = [e for e in _entries(litellm_cfg)
             if (e.get("model_info") or {}).get("managed_by") == "sysadmin-fleet-manager"]
    assert fleet[0]["litellm_params"]["api_base"] == "http://10.0.0.11:8000/v1"
    monkeypatch.setenv("FLEET_INFERENCE_SCHEME", "gopher")
    with pytest.raises(ValueError):
        litellm_sync.sync_from_fleet([("model-a", "10.0.0.11")], config_path=litellm_cfg,
                                     restart=False)


def test_sync_from_fleet_restart_uses_platform_sh(litellm_cfg):
    calls = []
    result = litellm_sync.sync_from_fleet(
        [("model-a", "10.0.0.11")], config_path=litellm_cfg, restart=True,
        run_fn=lambda command, cwd: (calls.append(command), _Completed(0))[1],
        platform_sh="/bin/true")
    assert result["changed"] is True
    assert result["restart"]["exit_code"] == 0
    assert calls == [["/bin/true", "service", "litellm", "restart"]]


class _Completed:
    def __init__(self, returncode):
        self.returncode = returncode
        self.stdout = "ok"


def test_sync_does_not_touch_fleet_block(tmp_path):
    """The existing sync() keeps managing only its own tag (behaviour unchanged)."""
    from types import SimpleNamespace
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"model_list": [
        {"model_name": "fleet-a", "litellm_params": {"model": "openai/fleet-a"},
         "model_info": {"managed_by": "sysadmin-fleet-manager"}},
    ]}))
    store = ModelRegistry(path=str(tmp_path / "registry.json"),
                          models_dir=str(tmp_path / "models"))
    store.upsert("local-1", {"hf_repo": "org/local-1", "status": registry_module.STATUS_RUNNING})
    litellm_sync.sync(store, config_path=str(cfg), restart=False)
    entries = _entries(str(cfg))
    tags = {(e.get("model_info") or {}).get("managed_by") for e in entries}
    assert "sysadmin-fleet-manager" in tags
    assert "sysadmin-model-manager" in tags


# --- node agent -----------------------------------------------------------------

from services.node_agent import auth as node_auth
from services.node_agent import converge as converge_module
from services.node_agent import hwinfo as hwinfo_module
from services.node_agent import models_admin as models_admin_module
from services.node_agent import server as node_server


@pytest.fixture
def node_store(tmp_path):
    return ModelRegistry(path=str(tmp_path / "registry.json"),
                         models_dir=str(tmp_path / "models"))


@pytest.fixture
def node_client(monkeypatch, node_store):
    monkeypatch.setenv("NODE_AGENT_AUTH_MODE", "disabled")
    monkeypatch.setattr(models_admin_module, "model_registry", node_store)
    monkeypatch.setattr(node_server, "model_registry", node_store)
    node_server._reset_state()
    return TestClient(node_server.app)


def _wait_for(predicate, timeout=5.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_node_register_and_heartbeat(node_client):
    response = node_client.post("/api/v1/fleet/register",
                               json={"node_name": "gpu-01",
                                     "inventory": {"gpu_count": 2, "vram_total_gb": 160.0}})
    assert response.status_code == 201
    assert response.json()["status"] == "pending"

    response = node_client.post("/api/v1/fleet/nodes/gpu-01/heartbeat",
                               json={"node_name": "gpu-01", "desired_state": {},
                                     "desired_version": 0})
    assert response.status_code == 200
    body = response.json()
    assert body["registered"] is True
    assert body["applied_desired_version"] == 0


def test_node_register_rejects_invalid_names(node_client):
    for bad in ("../evil", "a/b", "", "-bad", "x" * 65, "has space"):
        response = node_client.post("/api/v1/fleet/register", json={"node_name": bad})
        assert response.status_code == 400, bad


def test_node_heartbeat_rejects_identity_mismatch_and_unregistered(node_client):
    # A heartbeat from a node that never registered is a conflict.
    response = node_client.post("/api/v1/fleet/nodes/gpu-01/heartbeat",
                               json={"node_name": "gpu-01"})
    assert response.status_code == 409
    node_client.post("/api/v1/fleet/register", json={"node_name": "gpu-01"})
    # Path/body identity mismatch is rejected.
    response = node_client.post("/api/v1/fleet/nodes/gpu-01/heartbeat",
                               json={"node_name": "gpu-02"})
    assert response.status_code == 403
    # An invalid name in the path is rejected (a space survives client
    # and server path normalization as a single segment).
    response = node_client.post("/api/v1/fleet/nodes/bad%20name/heartbeat",
                               json={"node_name": "gpu-01"})
    assert response.status_code == 404


def test_node_auth_fails_closed_on_plain_http_in_mtls_mode(monkeypatch, node_store):
    monkeypatch.setenv("NODE_AGENT_AUTH_MODE", "mtls")
    monkeypatch.setattr(models_admin_module, "model_registry", node_store)
    monkeypatch.setattr(node_server, "model_registry", node_store)
    node_server._reset_state()
    client = TestClient(node_server.app)  # TestClient speaks plain HTTP
    response = client.post("/api/v1/fleet/register", json={"node_name": "gpu-01"})
    assert response.status_code == 403
    assert "mTLS" in response.json()["detail"]


def test_node_heartbeat_applies_desired_state_in_background(node_client, monkeypatch):
    calls = []

    def fake_converge(desired, store, **kwargs):
        calls.append(desired)
        return {name: {"status": "started"} for name in desired}

    monkeypatch.setattr(converge_module, "converge", fake_converge)
    node_client.post("/api/v1/fleet/register", json={"node_name": "gpu-01"})
    desired = {"model-a": {"action": "start", "params": {"gpu_memory_utilization": 0.8}}}
    response = node_client.post("/api/v1/fleet/nodes/gpu-01/heartbeat",
                               json={"node_name": "gpu-01", "desired_state": desired,
                                     "desired_version": 7})
    assert response.status_code == 200
    assert response.json()["converging"] == 7
    assert _wait_for(lambda: node_server._STATE["applied_version"] == 7)
    assert calls == [desired]
    # Re-sending the same version does not reconverge.
    response = node_client.post("/api/v1/fleet/nodes/gpu-01/heartbeat",
                               json={"node_name": "gpu-01", "desired_state": desired,
                                     "desired_version": 7})
    assert "converging" not in response.json()
    assert len(calls) == 1


def test_node_drain_stops_models_and_confirms(node_client, monkeypatch):
    monkeypatch.setattr(converge_module, "converge",
                        lambda desired, store, **kwargs: {
                            name: {"status": "stopped"} for name in desired})
    node_client.post("/api/v1/fleet/register", json={"node_name": "gpu-01"})
    response = node_client.post("/api/v1/fleet/nodes/gpu-01/drain",
                               json={"node_name": "gpu-01"})
    assert response.status_code == 202
    assert response.json()["status"] == "draining"
    assert _wait_for(lambda: node_server._STATE["drain_status"] == "drained")
    # A second drain reports the confirmed state.
    response = node_client.post("/api/v1/fleet/nodes/gpu-01/drain",
                               json={"node_name": "gpu-01"})
    assert response.json()["status"] == "drained"
    # While draining, heartbeat refuses new start actions.
    response = node_client.post(
        "/api/v1/fleet/nodes/gpu-01/heartbeat",
        json={"node_name": "gpu-01",
              "desired_state": {"model-a": {"action": "start", "params": {}}},
              "desired_version": 9})
    assert "drain" in response.json().get("note", "")


def test_node_healthz_and_info(node_client):
    response = node_client.get("/healthz")
    # No GPU on the test host: degraded (503) but well-formed.
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert "gpu" in response.json()["checks"]

    node_client.post("/api/v1/fleet/register", json={"node_name": "gpu-01"})
    response = node_client.get("/api/v1/node/info", params={"node_name": "gpu-01"})
    assert response.status_code == 200
    assert response.json()["node"] == "gpu-01"
    assert response.json()["registered"] is True
    # Missing identity on a GET is rejected.
    assert node_client.get("/api/v1/node/info").status_code in (400, 403)


def test_node_models_admin_lifecycle(node_client, node_store):
    # Register a model through the secretless local admin API.
    created = node_client.post("/api/v1/models", params={"node_name": "gpu-01"},
                               json={"node_name": "gpu-01", "hf_repo": "org/model-a",
                                     "engine": "vllm"})
    assert created.status_code == 201, created.text
    assert created.json()["model"]["name"] == "model-a"

    listed = node_client.get("/api/v1/models", params={"node_name": "gpu-01"})
    assert [m["name"] for m in listed.json()["models"]] == ["model-a"]

    # Unknown engine rejected; unknown model is 404.
    bad = node_client.post("/api/v1/models", params={"node_name": "gpu-01"},
                           json={"node_name": "gpu-01", "hf_repo": "org/x",
                                 "engine": "bogus"})
    assert bad.status_code == 400
    assert node_client.get("/api/v1/models/nope",
                           params={"node_name": "gpu-01"}).status_code == 404

    # Start requires a downloaded model.
    assert node_client.post("/api/v1/models/model-a/start",
                            params={"node_name": "gpu-01"}).status_code == 409
    node_store.update("model-a", status=registry_module.STATUS_DOWNLOADED,
                      path=str(node_store.models_dir))
    started = node_client.post("/api/v1/models/model-a/start",
                               params={"node_name": "gpu-01"})
    assert started.status_code == 202

    # Params update is bounded to the engine's load fields.
    patched = node_client.patch("/api/v1/models/model-a", params={"node_name": "gpu-01"},
                               json={"node_name": "gpu-01",
                                     "gpu_memory_utilization": 0.8})
    assert patched.status_code == 200
    assert node_client.patch(
        "/api/v1/models/model-a", params={"node_name": "gpu-01"},
        json={"node_name": "gpu-01", "hf_repo": "org/other"}).status_code == 400

    # Identity gate: no node_name anywhere -> rejected.
    assert node_client.get("/api/v1/models").status_code in (400, 403)


def test_hwinfo_without_gpu_is_explicit():
    inventory = hwinfo_module.gpu_inventory(smi_fn=lambda: (_ for _ in ()).throw(
        FileNotFoundError("no nvidia-smi")))
    assert inventory == {"gpu_model": None, "gpu_count": 0, "vram_total_gb": 0.0,
                         "compute_capability": None, "available": False}
    inventory = hwinfo_module.gpu_inventory(
        smi_fn=lambda: "NVIDIA H100 80GB HBM3, 81920 MiB, 9.0\n"
                       "NVIDIA H100 80GB HBM3, 81920 MiB, 9.0\n")
    assert inventory["gpu_count"] == 2
    assert inventory["vram_total_gb"] == 160.0
    assert inventory["compute_capability"] == "9.0"
    assert inventory["available"] is True


# --- converge -------------------------------------------------------------------

def _converge_store(tmp_path):
    store = ModelRegistry(path=str(tmp_path / "registry.json"),
                          models_dir=str(tmp_path / "models"))
    store.upsert("model-a", {"hf_repo": "org/model-a", "engine": "vllm",
                             "status": registry_module.STATUS_DOWNLOADED,
                             "path": str(tmp_path / "models" / "model-a")})
    return store


def test_converge_starts_downloaded_model_with_injected_engine(tmp_path):
    store = _converge_store(tmp_path)
    calls = []

    def fake_start(name, _store):
        calls.append(name)
        return _store.update(name, status=registry_module.STATUS_RUNNING,
                             server={"pid": 1234, "port": 8100, "status": "running"})

    def fail_download(name, _store, **kwargs):
        raise AssertionError("download must not run for a downloaded model")

    results = converge_module.converge(
        {"model-a": {"action": "start",
                     "params": {"gpu_memory_utilization": 0.85,
                                "hf_repo": "evil/override", "name": "evil"}}},
        store, download=fail_download, start=fake_start,
        stop=lambda name, _store: (_ for _ in ()).throw(AssertionError("no stop expected")))
    assert results["model-a"]["status"] == "started"
    assert results["model-a"]["port"] == 8100
    assert calls == ["model-a"]
    entry = store.get("model-a")
    # Bounded params applied; identity/source fields never overwritten by desired state.
    assert entry["gpu_memory_utilization"] == 0.85
    assert entry["hf_repo"] == "org/model-a"


def test_converge_downloads_before_start_when_needed(tmp_path):
    store = ModelRegistry(path=str(tmp_path / "registry.json"),
                          models_dir=str(tmp_path / "models"))
    store.upsert("model-a", {"hf_repo": "org/model-a", "engine": "vllm",
                             "status": registry_module.STATUS_REGISTERED})
    order = []

    def fake_download(name, _store, **kwargs):
        order.append("download")
        return _store.update(name, status=registry_module.STATUS_DOWNLOADED)

    def fake_start(name, _store):
        order.append("start")
        return {"server": {"port": 8100}}

    results = converge_module.converge({"model-a": {"action": "start", "params": {}}},
                                       store, download=fake_download, start=fake_start,
                                       stop=lambda n, s: {})
    assert order == ["download", "start"]
    assert results["model-a"]["status"] == "started"


def test_converge_stop_and_noops(tmp_path):
    store = _converge_store(tmp_path)
    stopped = []

    def fake_stop(name, _store):
        stopped.append(name)
        return _store.update(name, status=registry_module.STATUS_STOPPED)

    # Not running -> already_stopped; unknown model stop -> noop.
    results = converge_module.converge(
        {"model-a": {"action": "stop"}, "ghost": {"action": "stop"}},
        store, stop=fake_stop)
    assert results["model-a"]["status"] == "already_stopped"
    assert results["ghost"]["status"] == "noop"
    assert stopped == []


def test_converge_reports_errors_per_model(tmp_path):
    store = _converge_store(tmp_path)

    def bad_start(name, _store):
        raise RuntimeError("CUDA OOM")

    results = converge_module.converge(
        {"model-a": {"action": "start", "params": {}},
         "../evil": {"action": "start"},
         "model-a2": {"action": "explode"}},
        store, start=bad_start, stop=lambda n, s: {})
    assert results["model-a"]["status"] == "error"
    assert "CUDA OOM" in results["model-a"]["error"]
    assert results["../evil"]["status"] == "error"  # invalid name, never acted on
    assert results["model-a2"]["status"] == "error"  # unknown action
    assert "explode" in results["model-a2"]["error"]


# --- fleet router (platform side) -------------------------------------------------

from services.fleet import router as fleet_router


class _FakeNodeResponse:
    def __init__(self, status_code=202, payload=None):
        self.status_code = status_code
        self._payload = payload or {"status": "draining"}

    def json(self):
        return self._payload


class _FakeNodeClient:
    """httpx.Client double: records calls, supports `with`."""

    def __init__(self, response=None, error=None):
        self.response = response or _FakeNodeResponse()
        self.error = error
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None):
        self.calls.append((url, json))
        if self.error is not None:
            raise self.error
        return self.response


@pytest.fixture
def fleet_api(monkeypatch, fleet_registry):
    monkeypatch.setattr(fleet_router, "fleet_registry", fleet_registry)
    monkeypatch.setattr(fleet_router, "authenticate_request", lambda request: ("admin", None))
    monkeypatch.setattr(fleet_router, "role_for_user", lambda user_id: "admin")
    monkeypatch.setattr(fleet_router, "_audit", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(fleet_router.router)
    return TestClient(app)


def _enroll(fleet_registry, name="gpu-01", address="10.0.0.11"):
    fleet_registry.register_node(name, gpu_model="H100", gpu_count=2,
                                 vram_total_gb=160.0, compute_capability="9.0",
                                 address=address)
    fleet_registry.approve_node(name, "romaric")
    fleet_registry.set_node_status(name, "active")


def test_fleet_approve_gate(fleet_api, fleet_registry):
    fleet_registry.register_node("gpu-01", address="10.0.0.11")
    response = fleet_api.post("/api/v1/fleet/nodes/gpu-01/approve")
    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    # Approving twice is a conflict, not an approval.
    assert fleet_api.post("/api/v1/fleet/nodes/gpu-01/approve").status_code == 409
    assert fleet_api.post("/api/v1/fleet/nodes/unknown/approve").status_code == 404
    # A space survives client/server path normalization as one segment.
    assert fleet_api.post(
        "/api/v1/fleet/nodes/bad%20name/approve").status_code == 400


def test_fleet_drain_calls_node_agent_over_mtls(fleet_api, fleet_registry, monkeypatch):
    _enroll(fleet_registry)
    fake = _FakeNodeClient()
    monkeypatch.setattr(fleet_router, "node_http_client", lambda: fake)
    response = fleet_api.post("/api/v1/fleet/nodes/gpu-01/drain")
    assert response.status_code == 200
    assert response.json()["status"] == "drained"
    assert fleet_registry.get_node("gpu-01")["status"] == "drained"
    url, payload = fake.calls[0]
    assert url == "https://10.0.0.11:8001/api/v1/fleet/nodes/gpu-01/drain"
    assert payload == {"node_name": "gpu-01"}


def test_fleet_drain_fails_closed_when_node_unreachable(fleet_api, fleet_registry,
                                                       monkeypatch):
    _enroll(fleet_registry)
    import httpx as _httpx
    fake = _FakeNodeClient(error=_httpx.ConnectError("no route"))
    monkeypatch.setattr(fleet_router, "node_http_client", lambda: fake)
    response = fleet_api.post("/api/v1/fleet/nodes/gpu-01/drain")
    assert response.status_code == 502
    # The registry was not marked drained on a failed call.
    assert fleet_registry.get_node("gpu-01")["status"] == "active"


def test_fleet_drain_rejects_bad_states(fleet_api, fleet_registry):
    fleet_registry.register_node("gpu-01", address="10.0.0.11")
    assert fleet_api.post("/api/v1/fleet/nodes/gpu-01/drain").status_code == 409
    fleet_registry.register_node("gpu-02")  # no address recorded
    fleet_registry.approve_node("gpu-02", "romaric")
    assert fleet_api.post("/api/v1/fleet/nodes/gpu-02/drain").status_code == 409


def test_fleet_decommission(fleet_api, fleet_registry):
    _enroll(fleet_registry)
    response = fleet_api.post("/api/v1/fleet/nodes/gpu-01/decommission")
    assert response.status_code == 200
    assert response.json()["status"] == "retired"
    assert fleet_api.post("/api/v1/fleet/nodes/gpu-01/decommission").status_code == 409


def test_fleet_list_and_health(fleet_api, fleet_registry):
    _enroll(fleet_registry, "gpu-01", "10.0.0.11")
    _enroll(fleet_registry, "gpu-02", "10.0.0.12")
    nodes = fleet_api.get("/api/v1/fleet/nodes").json()["nodes"]
    assert [n["name"] for n in nodes] == ["gpu-01", "gpu-02"]
    health = fleet_api.get("/api/v1/fleet/health").json()
    assert health["nodes"] == 2
    assert health["by_status"] == {"active": 2}
    assert health["vram_total_gb"] == 320.0
    assert health["healthy_nodes"] == 2


def test_fleet_api_fails_closed_without_registry(fleet_api, monkeypatch):
    monkeypatch.setattr(fleet_router, "fleet_registry", None)
    assert fleet_api.get("/api/v1/fleet/nodes").status_code == 503
    assert fleet_api.get("/api/v1/fleet/health").status_code == 503
    assert fleet_api.post("/api/v1/fleet/nodes/gpu-01/approve").status_code == 503


def test_fleet_api_requires_admin(fleet_api, monkeypatch):
    monkeypatch.setattr(fleet_router, "role_for_user", lambda user_id: "user")
    assert fleet_api.get("/api/v1/fleet/nodes").status_code == 403


# --- litellm daemon ----------------------------------------------------------------

from services.fleet import litellm_daemon


def test_daemon_sync_once_full_cycle(fleet_registry):
    fleet_registry.register_node("gpu-01", gpu_model="H100", gpu_count=2,
                                 vram_total_gb=160.0, compute_capability="9.0",
                                 address="10.0.0.11")
    fleet_registry.register_node("gpu-02", gpu_model="A100", gpu_count=1,
                                 vram_total_gb=40.0, compute_capability="8.0",
                                 address="10.0.0.12")
    for name in ("gpu-01", "gpu-02"):
        fleet_registry.approve_node(name, "romaric")
        fleet_registry.set_node_status(name, "active")
    fleet_registry.set_desired_state("model-a", {
        "replicas": 2, "engine": "vllm", "vram_per_replica_gb": 30,
        "gpu_class": {"vram_min_gb": 30}, "params": {"gpu_memory_utilization": 0.85}})

    pushes = []
    synced = []

    def fake_heartbeat(name, address, desired, version):
        pushes.append((name, desired, version))
        return {"node": name, "status": "ok", "applied_desired_version": version,
                "actual": {"models": [
                    {"name": model, "running": spec["action"] == "start"}
                    for model, spec in desired.items()]}}

    def fake_sync(placements):
        synced.append(placements)
        return {"changed": True, "models": {}}

    summary = litellm_daemon.sync_once(fleet_registry, heartbeat_fn=fake_heartbeat,
                                       sync_fn=fake_sync)
    # Both replicas placed on distinct nodes (bin-packing), both pushed.
    assert sorted(name for name, _, _ in pushes) == ["gpu-01", "gpu-02"]
    assert summary["shortfall"] == {}
    assert summary["stale_marked"] == 0
    # The daemon pushed start actions with the policy params.
    for _, desired, _ in pushes:
        assert desired["model-a"]["action"] == "start"
        assert desired["model-a"]["params"] == {"gpu_memory_utilization": 0.85}
    # LiteLLM got one entry per (model x node address).
    assert synced == [[("model-a", "10.0.0.11"), ("model-a", "10.0.0.12")]]
    # Placements were recorded desired vs actual.
    placements = {p["node_name"]: p for p in fleet_registry.list_placements()}
    assert placements["gpu-01"]["desired_state"] == "running"
    assert placements["gpu-01"]["actual_state"] == "running"


def test_daemon_sync_once_records_node_errors_and_stops(fleet_registry):
    fleet_registry.register_node("gpu-01", vram_total_gb=160.0, address="10.0.0.11")
    fleet_registry.approve_node("gpu-01", "romaric")
    fleet_registry.set_node_status("gpu-01", "active")
    fleet_registry.set_desired_state("model-a", {"replicas": 1, "vram_per_replica_gb": 10})
    # Previously desired on this node, now unassigned -> stop pushed.
    fleet_registry.record_placement("model-old", "gpu-01",
                                   {"desired": "running", "actual": "running"})

    def boom(name, address, desired, version):
        raise RuntimeError("node down")

    summary = litellm_daemon.sync_once(fleet_registry, heartbeat_fn=boom,
                                       sync_fn=lambda placements: {"changed": False})
    assert summary["node_errors"] == {"gpu-01": "node down"}


def test_daemon_stop_action_for_removed_model(fleet_registry):
    fleet_registry.register_node("gpu-01", vram_total_gb=160.0, address="10.0.0.11")
    fleet_registry.approve_node("gpu-01", "romaric")
    fleet_registry.set_node_status("gpu-01", "active")
    fleet_registry.record_placement("model-old", "gpu-01",
                                   {"desired": "running", "actual": "running"})
    pushes = []

    def fake_heartbeat(name, address, desired, version):
        pushes.append(desired)
        return {"node": name, "actual": {"models": []}}

    litellm_daemon.sync_once(fleet_registry, heartbeat_fn=fake_heartbeat,
                             sync_fn=lambda placements: {"changed": False})
    assert pushes[0]["model-old"]["action"] == "stop"
    placements = {p["model_name"]: p for p in fleet_registry.list_placements()}
    assert placements["model-old"]["desired_state"] == "stopped"


# --- agent_tools role gating ----------------------------------------------------------

def test_agent_tools_mounts_fleet_and_models_by_default():
    # FastAPI >= 0.142 defers include_router into _IncludedRouter markers;
    # the mount decision (role gating) is what this test pins down.
    from fastapi.routing import _IncludedRouter
    from services.agent_tools import server as agent_server
    from services.fleet.router import router as fleet_router
    from services.model_manager.router import router as model_manager_router
    included = [route.original_router for route in agent_server.app.routes
                if isinstance(route, _IncludedRouter)]
    assert any(r is fleet_router for r in included)
    assert any(r is model_manager_router for r in included)


# --- schema -----------------------------------------------------------------------

def test_schema_declares_fleet_tables():
    from services.control_store.schema import STATEMENTS
    joined = "\n".join(STATEMENTS)
    for table in ("gpu_nodes", "model_placements", "fleet_desired_state"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in joined
    assert "CREATE INDEX IF NOT EXISTS idx_gpu_nodes_status" in joined
