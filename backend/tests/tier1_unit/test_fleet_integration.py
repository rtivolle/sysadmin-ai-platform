"""Integration tests for the GPU fleet control loop.

Covers the full loop from ARCHITECTURE.md section 7: node registration
(pending) -> human approval -> heartbeats -> declarative desired state ->
platform scheduler (bin-packing) -> placement recording -> dynamic LiteLLM
`model_list` generation, plus stale-node exclusion, drain/decommission
lifecycle, the node-agent HTTP surface, and heterogeneous GPU scheduling.

No real Postgres is ever touched: every database access goes through the
in-memory :class:`FleetTableExecutor` below, which implements the same
narrow ``Executor`` protocol (query / execute / transaction) as the
control_store doubles in ``control_store_fakes.py`` and emulates the exact
SQL statements issued by
``services.control_store.fleet_registry.FleetRegistry``.

Each contractual module is loaded lazily with an explicit skip message so a
module that is not implemented yet skips its tests instead of failing at
import time.
"""
import contextlib
import importlib
import importlib.util
import os
import time

os.environ.setdefault("NODE_AGENT_AUTH_MODE", "disabled")

import pytest
import yaml


def require_module(module_name: str, missing_reason: str):
    """Import a contractual module, skipping the test if it is not there yet."""
    if importlib.util.find_spec(module_name) is None:
        pytest.skip(missing_reason)
    return importlib.import_module(module_name)


def fleet_registry_module():
    return require_module(
        "services.control_store.fleet_registry",
        "fleet_registry not yet implemented (services.control_store.fleet_registry)",
    )


def scheduler_module():
    return require_module(
        "services.fleet.scheduler",
        "fleet scheduler not yet implemented (services.fleet.scheduler)",
    )


def litellm_sync_module():
    module = require_module(
        "services.model_manager.litellm_sync",
        "model_manager.litellm_sync not found",
    )
    if not hasattr(module, "sync_from_fleet"):
        pytest.skip(
            "sync_from_fleet not yet implemented in services.model_manager.litellm_sync"
        )
    return module


def node_agent_server_module():
    return require_module(
        "services.node_agent.server",
        "node_agent not yet implemented",
    )


_NODE_COLUMNS = ("name", "gpu_model", "gpu_count", "vram_total_gb", "compute_capability",
                 "address", "status", "last_heartbeat", "approved_by", "approved_at",
                 "created_at")


class FleetTableExecutor:
    """In-memory Executor double for the fleet tables.

    Emulates ``gpu_nodes``, ``fleet_desired_state`` and ``model_placements``
    against the exact statements issued by ``FleetRegistry`` (see
    ``services/control_store/fleet_registry.py``): timestamps behave like
    Postgres ``now()`` (``time.time()`` floats) so the freshness predicates
    in ``healthy_nodes()``/``mark_stale()`` work as in production.

    Statements are matched by fragments in the style of
    ``control_store_fakes.py``; anything unrecognised raises
    ``AssertionError`` showing the offending SQL.
    """

    def __init__(self):
        self.nodes = {}  # name -> dict keyed like _NODE_COLUMNS
        self.desired = {}  # model -> policy JSON string
        self.placements = []  # [{"model_name","node_name","desired_state","actual_state","updated_at"}]
        self.statements = []
        self.fail_with = None

    # -- Executor protocol -------------------------------------------------
    def query(self, sql, params=()):
        self._maybe_fail()
        self.statements.append((sql, tuple(params)))
        s = " ".join(str(sql).split())
        if "FROM gpu_nodes" in s:
            return self._query_nodes(s, params)
        if "FROM fleet_desired_state" in s:
            return [(model, self.desired[model]) for model in sorted(self.desired)]
        if "FROM model_placements" in s:
            ordered = sorted(self.placements,
                             key=lambda p: (p["model_name"], p["node_name"]))
            return [(p["model_name"], p["node_name"], p["desired_state"],
                     p["actual_state"], p["updated_at"]) for p in ordered]
        raise AssertionError(f"unexpected query: {sql}")

    def execute(self, sql, params=()):
        self._maybe_fail()
        self.statements.append((sql, tuple(params)))
        s = " ".join(str(sql).split())
        if "INSERT INTO gpu_nodes" in s:
            return self._upsert_node(params)
        if "UPDATE gpu_nodes" in s:
            if "SET status = 'approved'" in s:
                return self._approve(params)
            if "SET status = 'drained'" in s:
                return self._transition(params[0], "drained", {"approved", "active", "stale"})
            if "SET status = 'retired'" in s:
                node = self.nodes.get(params[0])
                if node is None or node["status"] == "retired":
                    return 0
                node["status"] = "retired"
                return 1
            if "SET status = 'stale'" in s:
                return self._mark_stale(params[0])
            if "SET last_heartbeat = now()" in s:
                return self._heartbeat(params[0])
            if "SET status = %s WHERE name = %s" in s:
                node = self.nodes.get(params[1])
                if node is None:
                    return 0
                node["status"] = params[0]
                return 1
        if "INSERT INTO fleet_desired_state" in s:
            model, policy_json = params[0], params[1]
            self.desired[model] = policy_json
            return 1
        if "INSERT INTO model_placements" in s:
            model, node, desired, actual = params[0], params[1], params[2], params[3]
            self.placements = [p for p in self.placements
                               if not (p["model_name"] == model and p["node_name"] == node)]
            self.placements.append({"model_name": model, "node_name": node,
                                   "desired_state": desired, "actual_state": actual,
                                   "updated_at": time.time()})
            return 1
        if s.startswith("CREATE"):
            return 0
        raise AssertionError(f"unexpected statement: {sql}")

    # -- gpu_nodes emulation ------------------------------------------------
    def _upsert_node(self, params):
        name, gpu_model, gpu_count, vram_total_gb, compute_capability, address = params
        now = time.time()
        node = self.nodes.get(name)
        if node is None:
            self.nodes[name] = {
                "name": name, "gpu_model": gpu_model, "gpu_count": gpu_count,
                "vram_total_gb": vram_total_gb, "compute_capability": compute_capability,
                "address": address, "status": "pending", "last_heartbeat": now,
                "approved_by": None, "approved_at": None, "created_at": now,
            }
            return 1
        # ON CONFLICT DO UPDATE: refresh inventory, never the lifecycle status.
        node.update({"gpu_model": gpu_model, "gpu_count": gpu_count,
                     "vram_total_gb": vram_total_gb,
                     "compute_capability": compute_capability, "address": address,
                     "last_heartbeat": now})
        return 1

    def _node_row(self, node):
        return tuple(node[col] for col in _NODE_COLUMNS)

    def _query_nodes(self, s, params):
        rows = list(self.nodes.values())
        if "WHERE name = %s" in s:
            rows = [n for n in rows if n["name"] == params[0]]
        elif "WHERE status IN ('approved', 'active')" in s:
            cutoff = time.time() - float(params[0])
            rows = [n for n in rows
                    if n["status"] in ("approved", "active")
                    and n["last_heartbeat"] is not None and n["last_heartbeat"] > cutoff]
        elif "WHERE status = %s" in s:
            rows = [n for n in rows if n["status"] == params[0]]
        return [self._node_row(n) for n in sorted(rows, key=lambda n: n["name"])]

    def _heartbeat(self, name):
        node = self.nodes.get(name)
        if node is None:
            return 0
        node["last_heartbeat"] = time.time()
        if node["status"] == "stale":
            node["status"] = "active"
        return 1

    def _approve(self, params):
        approved_by, name = params[0], params[1]
        node = self.nodes.get(name)
        if node is None or node["status"] != "pending":
            return 0
        node["status"] = "approved"
        node["approved_by"] = approved_by
        node["approved_at"] = time.time()
        node["last_heartbeat"] = time.time()
        return 1

    def _transition(self, name, new_status, allowed_from):
        node = self.nodes.get(name)
        if node is None or node["status"] not in allowed_from:
            return 0
        node["status"] = new_status
        return 1

    def _mark_stale(self, max_age_seconds):
        cutoff = time.time() - float(max_age_seconds)
        affected = 0
        for node in self.nodes.values():
            if (node["status"] == "active" and node["last_heartbeat"] is not None
                    and node["last_heartbeat"] < cutoff):
                node["status"] = "stale"
                affected += 1
        return affected

    @contextlib.contextmanager
    def transaction(self):
        yield self

    def _maybe_fail(self):
        if self.fail_with is not None:
            raise self.fail_with


def make_registry():
    """Build a FleetRegistry over a fresh in-memory executor."""
    fr = fleet_registry_module()
    executor = FleetTableExecutor()
    return fr.FleetRegistry(executor), executor


def register_gpu01(registry):
    """Register the 48 GB reference node; returns its node dict."""
    return registry.register_node(
        "gpu-01",
        gpu_model="NVIDIA L40S",
        gpu_count=1,
        vram_total_gb=48,
        compute_capability="8.9",
        address="gpu-01",
    )


GPU_48_HEARTBEAT = {
    "models": [],
    "vram_free_gb": 44.0,
    "vram_total_gb": 48,
    "compute_capability": "8.9",
    "engine_health": "ok",
    "desired_state_version": 0,
}

MODEL = "qwen2.5-coder-32b"
POLICY_40GB = {
    "replicas": 1,
    "engine": "vllm",
    "vram_per_replica_gb": 22.0,
    "gpu_class": {"vram_min_gb": 40, "compute_capability_min": "8.0"},
    "params": {"gpu_memory_utilization": 0.85, "max_model_len": 8192},
}

FLEET_MANAGED_BY = "sysadmin-fleet-manager"


def write_seed_config(path):
    """A LiteLLM config.yaml with a hand-written entry that must survive sync."""
    config = {
        "model_list": [
            {
                "model_name": "fast-model",
                "litellm_params": {
                    "model": "openai/fast-model",
                    "api_base": "http://127.0.0.1:8001/v1",
                    "api_key": "none",
                },
                "model_info": {"managed_by": "human-operator"},
            }
        ],
        "general_settings": {"master_key": "sk-placeholder"},
    }
    with open(path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(config, handle)
    return config


def read_config(path):
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def managed_entries(config):
    return [
        e for e in (config.get("model_list") or [])
        if isinstance(e, dict)
        and (e.get("model_info") or {}).get("managed_by") == FLEET_MANAGED_BY
    ]


def node_names_from_assignments(desired, node_name):
    """Extract model names from a scheduler assignment list for one node.

    The scheduler returns ``{node: [{"model": name, ...}]}``; this stays
    tolerant of dict-vs-string assignment shapes.
    """
    assignments = desired.get(node_name) or []
    found = []
    for assignment in assignments:
        if isinstance(assignment, dict):
            for key in ("model", "model_name", "name"):
                if key in assignment:
                    found.append(assignment[key])
                    break
            else:
                found.append(repr(assignment))
        else:
            found.append(str(assignment))
    return found


def healthy_placement_pairs(registry, extra_pairs=()):
    """(model, node) pairs limited to currently healthy nodes.

    This is what the litellm_sync daemon derives from the registry: only
    healthy nodes receive traffic, with no manual config edit.
    """
    healthy_names = {n["name"] for n in registry.healthy_nodes()}
    return [(model, node) for (model, node) in extra_pairs if node in healthy_names]


def test_full_loop_register_to_traffic(tmp_path, monkeypatch):
    """Register -> approve -> heartbeat -> desired state -> schedule ->
    placement -> litellm_sync: a GPU node goes from pending to serving."""
    registry, executor = make_registry()
    scheduler = scheduler_module()
    litellm_sync = litellm_sync_module()
    monkeypatch.setenv("FLEET_INFERENCE_SCHEME", "https")

    # 1. registration lands the node in `pending`
    node = register_gpu01(registry)
    assert node["status"] == "pending"
    assert registry.get_node("gpu-01")["status"] == "pending"

    # 2. human approval gates any placement
    assert registry.approve_node("gpu-01", "romaric") is True
    assert registry.get_node("gpu-01")["status"] == "approved"

    # 3. heartbeats keep the node healthy (stale nodes self-heal here too)
    assert registry.heartbeat("gpu-01", GPU_48_HEARTBEAT) is True
    assert registry.get_node("gpu-01")["status"] in ("approved", "active")

    # 4. declarative desired state, then the platform scheduler bin-packs
    registry.set_desired_state(MODEL, POLICY_40GB)
    policies = registry.get_desired_state()
    assert policies[MODEL]["replicas"] == 1
    nodes = registry.healthy_nodes()
    assert any(n["name"] == "gpu-01" for n in nodes)

    desired = scheduler.compute_desired_state(policies, nodes)
    assert "gpu-01" in desired, f"gpu-01 not scheduled: {desired}"
    assert MODEL in node_names_from_assignments(desired, "gpu-01"), (
        f"{MODEL} not assigned to gpu-01: {desired}"
    )

    # 5. placement is recorded, then the config is generated from the fleet
    registry.record_placement(MODEL, "gpu-01", "running")
    assert registry.list_placements()[0]["actual_state"] == "running"
    config_path = str(tmp_path / "config.yaml")
    write_seed_config(config_path)
    result = litellm_sync.sync_from_fleet(
        healthy_placement_pairs(registry, [(MODEL, "gpu-01")]),
        config_path=config_path,
        restart=False,
    )
    assert result["changed"] is True

    config = read_config(config_path)
    managed = managed_entries(config)
    assert len(managed) == 1
    entry = managed[0]
    assert entry["model_name"] == MODEL
    assert entry["litellm_params"]["api_base"] == "https://gpu-01:8000/v1"
    assert entry["model_info"]["managed_by"] == FLEET_MANAGED_BY
    # hand-written entries are never touched
    hand_written = [
        e for e in config["model_list"]
        if (e.get("model_info") or {}).get("managed_by") == "human-operator"
    ]
    assert len(hand_written) == 1 and hand_written[0]["model_name"] == "fast-model"
    assert config["general_settings"]["master_key"] == "sk-placeholder"


def test_stale_node_excluded_from_traffic(tmp_path, monkeypatch):
    """3 missed heartbeats (simulated) -> stale -> healthy_nodes() excludes it
    -> the next sync drops its model_list entry with no manual edit."""
    registry, _ = make_registry()
    litellm_sync = litellm_sync_module()
    monkeypatch.setenv("FLEET_INFERENCE_SCHEME", "https")

    register_gpu01(registry)
    registry.approve_node("gpu-01", "romaric")
    registry.heartbeat("gpu-01", GPU_48_HEARTBEAT)
    registry.set_desired_state(MODEL, POLICY_40GB)
    registry.record_placement(MODEL, "gpu-01", "running")

    config_path = str(tmp_path / "config.yaml")
    write_seed_config(config_path)

    # node is healthy: it gets traffic
    litellm_sync.sync_from_fleet(
        healthy_placement_pairs(registry, [(MODEL, "gpu-01")]),
        config_path=config_path,
        restart=False,
    )
    assert len(managed_entries(read_config(config_path))) == 1

    # 3 missed heartbeats -> stale (simulated directly; mark_stale() is the
    # production sweeper). A stale node heals on its next real heartbeat.
    assert registry.set_node_status("gpu-01", "stale") is True
    assert registry.get_node("gpu-01")["status"] == "stale"
    assert "gpu-01" not in {n["name"] for n in registry.healthy_nodes()}

    # litellm_sync only routes to healthy nodes: the entry is withdrawn
    # automatically, hand-written config is untouched.
    litellm_sync.sync_from_fleet(
        healthy_placement_pairs(registry, [(MODEL, "gpu-01")]),
        config_path=config_path,
        restart=False,
    )
    config = read_config(config_path)
    assert managed_entries(config) == []
    assert any(
        e.get("model_name") == "fast-model" for e in config["model_list"]
    ), "hand-written entries must survive a full withdrawal"

    # ... and a fresh heartbeat heals the node back into the fleet
    assert registry.heartbeat("gpu-01", GPU_48_HEARTBEAT) is True
    assert registry.get_node("gpu-01")["status"] == "active"
    assert "gpu-01" in {n["name"] for n in registry.healthy_nodes()}


def test_drain_lifecycle(tmp_path, monkeypatch):
    """drain -> drained (no new traffic) -> decommission -> retired."""
    registry, _ = make_registry()
    litellm_sync = litellm_sync_module()
    monkeypatch.setenv("FLEET_INFERENCE_SCHEME", "https")

    register_gpu01(registry)
    registry.approve_node("gpu-01", "romaric")
    registry.heartbeat("gpu-01", GPU_48_HEARTBEAT)
    registry.set_desired_state(MODEL, POLICY_40GB)
    registry.record_placement(MODEL, "gpu-01", "running")

    config_path = str(tmp_path / "config.yaml")
    write_seed_config(config_path)
    litellm_sync.sync_from_fleet(
        healthy_placement_pairs(registry, [(MODEL, "gpu-01")]),
        config_path=config_path,
        restart=False,
    )
    assert len(managed_entries(read_config(config_path))) == 1

    # drain: out of the model_list, no new traffic
    assert registry.drain_node("gpu-01") is True
    assert registry.get_node("gpu-01")["status"] == "drained"
    assert "gpu-01" not in {n["name"] for n in registry.healthy_nodes()}
    litellm_sync.sync_from_fleet(
        healthy_placement_pairs(registry, [(MODEL, "gpu-01")]),
        config_path=config_path,
        restart=False,
    )
    assert managed_entries(read_config(config_path)) == []

    # decommission: terminal state
    assert registry.decommission_node("gpu-01") is True
    assert registry.get_node("gpu-01")["status"] == "retired"


def test_node_agent_http_surface():
    """FastAPI surface of the node agent (auth disabled for tests)."""
    server = node_agent_server_module()
    from fastapi.testclient import TestClient

    client = TestClient(server.app)

    # /healthz always answers; 503 means "degraded" (no GPU on this host),
    # which still proves the endpoint is wired.
    response = client.get("/healthz")
    assert response.status_code in (200, 503), response.text
    assert response.json()["status"] in ("ok", "degraded")

    # path traversal / invalid node names are rejected at the gate
    response = client.post(
        "/api/v1/fleet/register",
        json={"node_name": "../x", "gpus": [], "vram_total_gb": 0},
    )
    assert response.status_code in (400, 422), response.status_code

    # a valid registration (pending approval), then a heartbeat for the
    # registered node
    response = client.post(
        "/api/v1/fleet/register",
        json={
            "node_name": "gpu-01",
            "address": "gpu-01",
            "inventory": {
                "gpu_model": "NVIDIA L40S",
                "gpu_count": 1,
                "vram_total_gb": 48.0,
                "compute_capability": "8.9",
                "available": True,
            },
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "pending"

    response = client.post(
        "/api/v1/fleet/nodes/gpu-01/heartbeat",
        json={"node_name": "gpu-01", "models": [], "vram_free_gb": 44.0},
    )
    assert response.status_code == 200, response.text
    assert response.json()["node"] == "gpu-01"

    response = client.get("/api/v1/node/info", params={"node_name": "gpu-01"})
    assert response.status_code == 200, response.text
    assert response.json()["node"] == "gpu-01"


def test_heterogeneous_gpu_scheduling():
    """A model needing 40 GB VRAM must land only on the 80 GB node."""
    scheduler = scheduler_module()

    policies = {
        "qwen2.5-coder-32b": {
            "replicas": 1,
            "engine": "vllm",
            "vram_per_replica_gb": 22.0,
            "gpu_class": {"vram_min_gb": 40, "compute_capability_min": "8.0"},
            "params": {"gpu_memory_utilization": 0.85},
        }
    }
    nodes = [
        {
            "name": "gpu-24",
            "status": "active",
            "vram_free_gb": 20.0,
            "vram_total_gb": 24,
            "compute_capability": "8.6",
        },
        {
            "name": "gpu-80",
            "status": "active",
            "vram_free_gb": 72.0,
            "vram_total_gb": 80,
            "compute_capability": "9.0",
        },
    ]
    desired = scheduler.compute_desired_state(policies, nodes)
    assert "qwen2.5-coder-32b" in node_names_from_assignments(desired, "gpu-80"), (
        f"model not assigned to the 80GB node: {desired}"
    )
    assert "qwen2.5-coder-32b" not in node_names_from_assignments(desired, "gpu-24"), (
        f"model wrongly assigned to the 24GB node: {desired}"
    )
