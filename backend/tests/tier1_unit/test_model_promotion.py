"""Versioned model rollout: registry versions, promotion, canary, rollback (chantier 3)."""
import pytest
from fastapi import HTTPException

from services.model_manager import promotion, registry
from services.model_manager import router as model_router
from services.model_manager.registry import ModelRegistry


class FakeFleet:
    """Minimal FleetRegistry double: only the desired-state surface promotion uses."""

    def __init__(self):
        self.policies = {}

    def get_desired_state(self):
        return {name: dict(policy) for name, policy in self.policies.items()}

    def set_desired_state(self, model, policy):
        self.policies[model] = dict(policy)


@pytest.fixture
def store(tmp_path):
    return ModelRegistry(path=str(tmp_path / "registry.json"), models_dir=str(tmp_path / "models"))


@pytest.fixture
def registered(store):
    store.upsert("llama", {"hf_repo": "org/llama", "status": registry.STATUS_REGISTERED,
                           "engine": registry.ENGINE_VLLM})
    return store


@pytest.fixture
def fleet():
    return FakeFleet()


class StubRequest:
    """Enough of a Request for the router endpoints: async .json()."""

    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


@pytest.fixture
def admin_api(store, fleet, monkeypatch):
    """Router endpoints with auth/audit/fleet stubbed; registry is a tmp store."""
    monkeypatch.setattr(model_router, "require_admin", lambda request: "tester")
    monkeypatch.setattr(model_router, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(model_router, "_audit_failure", lambda *a, **k: None)
    monkeypatch.setattr(model_router, "model_registry", store)
    monkeypatch.setattr(model_router, "open_fleet_registry", lambda: fleet)
    store.upsert("llama", {"hf_repo": "org/llama", "status": registry.STATUS_REGISTERED})
    return model_router


# --- version registration ---------------------------------------------------

def test_register_version_defaults_and_record_shape(registered):
    record = registered.register_version("llama", "v1", "org/llama", "abc123", "vllm")
    assert record["version"] == "v1"
    assert record["stage"] == registry.STAGE_STAGING
    assert record["engine"] == "vllm"
    assert record["canary_percent"] == 0
    assert record["revision"] == "abc123"
    assert record["created_at"] and record["updated_at"]
    assert registered.get_version("llama", "v1") == record


def test_register_version_validation(registered):
    with pytest.raises(ValueError):
        registered.register_version("unknown-model", "v1", "org/x", None, "vllm")
    for bad_version in (None, "", "a/b", "../x", "v 1", 42):
        with pytest.raises((ValueError, TypeError)):
            registered.register_version("llama", bad_version, "org/llama", None, "vllm")
    for bad in ("org", "org/", "a/b/c"):
        with pytest.raises(ValueError):
            registered.register_version("llama", "v1", bad, None, "vllm")
    with pytest.raises(ValueError):
        registered.register_version("llama", "v1", "org/llama", None, "tensorrt")
    with pytest.raises(ValueError):
        registered.register_version("llama", "v1", "org/llama", None, "vllm", stage="moon")
    registered.register_version("llama", "v1", "org/llama", None, "vllm")
    with pytest.raises(ValueError):
        registered.register_version("llama", "v1", "org/llama", None, "vllm")


def test_versions_are_retrocompatible(store):
    store.upsert("legacy", {"hf_repo": "org/legacy", "status": registry.STATUS_REGISTERED})
    assert store.list_versions("legacy") == []
    assert store.get_version("legacy", "v1") is None
    assert store.active_version("legacy") is None
    assert store.promotion_history("legacy") == []
    assert store.list_versions("nope") == []
    assert store.active_version("nope") is None
    # The pre-version fields survive a version write untouched.
    store.upsert("legacy", {"quantization": "awq"})
    store.register_version("legacy", "v1", "org/legacy", None, "vllm")
    assert store.get("legacy")["quantization"] == "awq"


def test_list_versions_sorted_and_active_version(registered):
    registered.register_version("llama", "v2", "org/llama", None, "vllm")
    registered.register_version("llama", "v1", "org/llama", None, "vllm")
    # created_at has 1s resolution: order within the same second is by id.
    assert sorted(v["version"] for v in registered.list_versions("llama")) == ["v1", "v2"]
    assert registered.active_version("llama", "prod") is None
    registered.set_stage("llama", "v1", registry.STAGE_CANARY)
    assert registered.active_version("llama", "canary")["version"] == "v1"


# --- stage machine ----------------------------------------------------------

def test_valid_rollout_chain(registered):
    registered.register_version("llama", "v1", "org/llama", None, "vllm")
    assert registered.set_stage("llama", "v1", "canary")["stage"] == "canary"
    assert registered.set_stage("llama", "v1", "prod")["stage"] == "prod"


def test_invalid_transitions_raise(registered):
    registered.register_version("llama", "v1", "org/llama", None, "vllm")
    # Skipping canary is invalid.
    with pytest.raises(ValueError, match="invalid stage transition"):
        registered.set_stage("llama", "v1", "prod")
    registered.set_stage("llama", "v1", "canary")
    registered.set_stage("llama", "v1", "prod")
    # Rolling back to canary is invalid.
    with pytest.raises(ValueError, match="invalid stage transition"):
        registered.set_stage("llama", "v1", "canary")
    # Unknown model/version/stage.
    with pytest.raises(ValueError):
        registered.set_stage("nope", "v1", "canary")
    with pytest.raises(ValueError):
        registered.set_stage("llama", "nope", "canary")
    with pytest.raises(ValueError):
        registered.set_stage("llama", "v1", "moon")


def test_any_stage_can_archive(registered):
    registered.register_version("llama", "v1", "org/llama", None, "vllm")
    registered.register_version("llama", "v2", "org/llama", None, "vllm")
    registered.set_stage("llama", "v1", "canary")
    registered.set_stage("llama", "v2", "archived")
    assert registered.get_version("llama", "v2")["stage"] == "archived"
    registered.set_stage("llama", "v1", "archived")
    assert registered.get_version("llama", "v1")["stage"] == "archived"


def test_demote_and_requalify_are_explicit_operator_actions(registered):
    registered.register_version("llama", "v1", "org/llama", None, "vllm")
    registered.set_stage("llama", "v1", "canary")
    registered.set_stage("llama", "v1", "prod")
    # Emergency pull-back.
    assert registered.set_stage("llama", "v1", "staging")["stage"] == "staging"
    # Re-qualify an archived build.
    registered.set_stage("llama", "v1", "archived")
    assert registered.set_stage("llama", "v1", "prod")["stage"] == "prod"


def test_single_occupant_stages_archive_the_displaced(registered):
    registered.register_version("llama", "v1", "org/llama", None, "vllm")
    registered.register_version("llama", "v2", "org/llama", None, "vllm")
    registered.set_stage("llama", "v1", "canary")
    registered.set_stage("llama", "v2", "canary")
    assert registered.get_version("llama", "v1")["stage"] == "archived"
    assert registered.active_version("llama", "canary")["version"] == "v2"
    registered.set_stage("llama", "v1", "prod")
    registered.set_stage("llama", "v2", "prod")
    assert registered.get_version("llama", "v1")["stage"] == "archived"
    assert registered.active_version("llama", "prod")["version"] == "v2"
    # staging is not single-occupant.
    registered.register_version("llama", "v3", "org/llama", None, "vllm")
    assert registered.get_version("llama", "v3")["stage"] == "staging"


def test_canary_percent_bounds(registered):
    registered.register_version("llama", "v1", "org/llama", None, "vllm")
    assert registered.set_canary_percent("llama", "v1", 0)["canary_percent"] == 0
    assert registered.set_canary_percent("llama", "v1", 100)["canary_percent"] == 100
    for bad in (-1, 101, "50", 50.5, True, None):
        with pytest.raises((ValueError, TypeError)):
            registered.set_canary_percent("llama", "v1", bad)
    with pytest.raises(ValueError):
        registered.set_canary_percent("llama", "nope", 10)
    with pytest.raises(ValueError):
        registered.set_canary_percent("nope", "v1", 10)


# --- promotion ----------------------------------------------------------------

def _roll_to_prod(store, fleet, version, canary_percent=10):
    store.register_version("llama", version, "org/llama", None, "vllm")
    promotion.promote("llama", version, "canary", canary_percent=canary_percent,
                      registry=store, fleet=fleet)
    return promotion.promote("llama", version, "prod", registry=store, fleet=fleet)


def test_promote_to_canary_writes_policy_read_modify_write(registered, fleet):
    fleet.set_desired_state("llama", {"replicas": 2, "engine": "vllm",
                                      "gpu_class": {"vram_min_gb": 40}})
    registered.register_version("llama", "v2", "org/llama", None, "vllm")
    record = promotion.promote("llama", "v2", "canary", canary_percent=25,
                               registry=registered, fleet=fleet)
    assert record["stage"] == "canary"
    policy = fleet.get_desired_state()["llama"]
    # Canary contract fields are present...
    assert policy["canary_version"] == "v2"
    assert policy["canary_traffic_percent"] == 25
    # ...and nothing the scheduler wrote was lost.
    assert policy["replicas"] == 2
    assert policy["engine"] == "vllm"
    assert policy["gpu_class"] == {"vram_min_gb": 40}
    assert registered.get_version("llama", "v2")["canary_percent"] == 25


def test_promote_to_prod_clears_canary_and_sets_version(registered, fleet):
    fleet.set_desired_state("llama", {"replicas": 1})
    _roll_to_prod(registered, fleet, "v1", canary_percent=10)
    policy = fleet.get_desired_state()["llama"]
    assert "canary_version" not in policy
    assert "canary_traffic_percent" not in policy
    assert policy["version"] == "v1"
    assert policy["replicas"] == 1  # preserved
    assert registered.active_version("llama", "prod")["version"] == "v1"


def test_promote_rejects_bad_transition_and_unknown(registered, fleet):
    registered.register_version("llama", "v1", "org/llama", None, "vllm")
    with pytest.raises(ValueError, match="invalid stage transition"):
        promotion.promote("llama", "v1", "prod", registry=registered, fleet=fleet)
    with pytest.raises(ValueError, match="unknown"):
        promotion.promote("llama", "nope", "canary", registry=registered, fleet=fleet)
    with pytest.raises(ValueError, match="unknown"):
        promotion.promote("nope", "v1", "canary", registry=registered, fleet=fleet)


def test_promote_fails_closed_without_fleet(registered, monkeypatch):
    registered.register_version("llama", "v1", "org/llama", None, "vllm")
    monkeypatch.setattr(promotion, "open_fleet_registry", lambda: None)
    with pytest.raises(RuntimeError, match="fleet registry unavailable"):
        promotion.promote("llama", "v1", "canary", registry=registered)


def test_set_canary_traffic_updates_policy(registered, fleet):
    fleet.set_desired_state("llama", {"replicas": 3, "notes": "keep me"})
    _roll_to_prod(registered, fleet, "v1")
    registered.register_version("llama", "v2", "org/llama", None, "vllm")
    promotion.promote("llama", "v2", "canary", canary_percent=10,
                      registry=registered, fleet=fleet)
    record = promotion.set_canary_traffic("llama", "v2", 40, registry=registered, fleet=fleet)
    assert record["canary_percent"] == 40
    policy = fleet.get_desired_state()["llama"]
    assert policy["canary_version"] == "v2"
    assert policy["canary_traffic_percent"] == 40
    assert policy["replicas"] == 3 and policy["notes"] == "keep me"


def test_set_canary_traffic_rejects_non_canary(registered, fleet):
    registered.register_version("llama", "v1", "org/llama", None, "vllm")
    with pytest.raises(ValueError, match="not in canary stage"):
        promotion.set_canary_traffic("llama", "v1", 10, registry=registered, fleet=fleet)
    promotion.promote("llama", "v1", "canary", registry=registered, fleet=fleet)
    promotion.promote("llama", "v1", "prod", registry=registered, fleet=fleet)
    with pytest.raises(ValueError, match="not in canary stage"):
        promotion.set_canary_traffic("llama", "v1", 10, registry=registered, fleet=fleet)


# --- rollback / demote --------------------------------------------------------

def test_rollback_restores_previous_prod(registered, fleet):
    _roll_to_prod(registered, fleet, "v1")
    _roll_to_prod(registered, fleet, "v2")
    assert registered.active_version("llama", "prod")["version"] == "v2"
    record = promotion.rollback("llama", registry=registered, fleet=fleet)
    assert record["version"] == "v1"
    assert record["stage"] == "prod"
    assert registered.get_version("llama", "v2")["stage"] == "archived"
    policy = fleet.get_desired_state()["llama"]
    assert policy["version"] == "v1"
    assert "canary_version" not in policy
    # A second rollback walks one step further back through history.
    record = promotion.rollback("llama", registry=registered, fleet=fleet)
    assert record["version"] == "v2"
    assert registered.active_version("llama", "prod")["version"] == "v2"


def test_rollback_without_history_raises(registered, fleet):
    registered.register_version("llama", "v1", "org/llama", None, "vllm")
    with pytest.raises(ValueError, match="no previous production version"):
        promotion.rollback("llama", registry=registered, fleet=fleet)


def test_rollback_clears_canary_pointer(registered, fleet):
    _roll_to_prod(registered, fleet, "v1")
    _roll_to_prod(registered, fleet, "v2")
    registered.register_version("llama", "v3", "org/llama", None, "vllm")
    promotion.promote("llama", "v3", "canary", registry=registered, fleet=fleet)
    assert fleet.get_desired_state()["llama"]["canary_version"] == "v3"
    promotion.rollback("llama", registry=registered, fleet=fleet)
    policy = fleet.get_desired_state()["llama"]
    assert "canary_version" not in policy
    assert policy["version"] == "v1"


def test_demote_to_staging(registered, fleet):
    fleet.set_desired_state("llama", {"replicas": 2})
    _roll_to_prod(registered, fleet, "v1")
    registered.register_version("llama", "v2", "org/llama", None, "vllm")
    promotion.promote("llama", "v2", "canary", canary_percent=30,
                      registry=registered, fleet=fleet)
    record = promotion.demote_to_staging("llama", "v2", registry=registered, fleet=fleet)
    assert record["stage"] == "staging"
    policy = fleet.get_desired_state()["llama"]
    assert "canary_version" not in policy
    assert policy["version"] == "v1"  # prod pointer untouched
    assert policy["replicas"] == 2
    history = registered.promotion_history("llama")
    assert history[-1]["to_stage"] == "staging"
    assert history[-1]["action"] == "demote"


def test_promotion_history_is_append_only(registered, fleet):
    _roll_to_prod(registered, fleet, "v1")
    _roll_to_prod(registered, fleet, "v2")
    promotion.rollback("llama", registry=registered, fleet=fleet)
    stages = [(event["version"], event["to_stage"]) for event in registered.promotion_history("llama")]
    assert ("v1", "prod") in stages and ("v2", "prod") in stages
    assert stages[-1] == ("v1", "prod")


def test_promotion_summary(registered, fleet):
    _roll_to_prod(registered, fleet, "v1")
    summary = promotion.promotion_summary("llama", registry=registered)
    assert summary["prod"] == "v1"
    assert summary["canary"] is None
    assert summary["by_stage"]["prod"] == ["v1"]
    assert len(summary["versions"]) == 1


# --- endpoints ----------------------------------------------------------------

async def test_endpoint_register_and_list_versions(admin_api):
    response = await admin_api.register_model_version("llama", StubRequest({
        "version": "v1", "hf_repo": "org/llama", "revision": "abc", "engine": "vllm"}))
    assert response["model"] == "llama"
    assert response["version"]["version"] == "v1"
    assert response["version"]["stage"] == "staging"
    listed = admin_api.list_model_versions("llama", StubRequest({}))
    assert [v["version"] for v in listed["versions"]] == ["v1"]
    assert listed["history"] == []


async def test_endpoint_register_version_validation(admin_api):
    with pytest.raises(HTTPException) as exc_info:
        await admin_api.register_model_version("llama", StubRequest({"version": "v1"}))
    assert exc_info.value.status_code == 400
    with pytest.raises(HTTPException) as exc_info:
        await admin_api.register_model_version("../evil", StubRequest({"version": "v1", "hf_repo": "o/m"}))
    assert exc_info.value.status_code == 400
    with pytest.raises(HTTPException) as exc_info:
        admin_api.list_model_versions("unknown-model", StubRequest({}))
    assert exc_info.value.status_code == 404


async def test_endpoint_promote_canary_then_prod(admin_api, fleet):
    fleet.set_desired_state("llama", {"replicas": 2})
    await admin_api.register_model_version("llama", StubRequest({"version": "v1", "hf_repo": "org/llama"}))
    response = await admin_api.promote_model_version(
        "llama", StubRequest({"version": "v1", "target_stage": "canary", "canary_percent": 15}))
    assert response["version"]["stage"] == "canary"
    policy = fleet.get_desired_state()["llama"]
    assert policy["canary_version"] == "v1" and policy["canary_traffic_percent"] == 15
    assert policy["replicas"] == 2
    response = await admin_api.promote_model_version(
        "llama", StubRequest({"version": "v1", "target_stage": "prod"}))
    assert response["version"]["stage"] == "prod"
    policy = fleet.get_desired_state()["llama"]
    assert policy["version"] == "v1" and "canary_version" not in policy


async def test_endpoint_promote_rejects_bad_transition_and_bad_percent(admin_api):
    await admin_api.register_model_version("llama", StubRequest({"version": "v1", "hf_repo": "org/llama"}))
    with pytest.raises(HTTPException) as exc_info:
        await admin_api.promote_model_version(
            "llama", StubRequest({"version": "v1", "target_stage": "prod"}))
    assert exc_info.value.status_code == 409
    with pytest.raises(HTTPException) as exc_info:
        await admin_api.promote_model_version(
            "llama", StubRequest({"version": "v1", "target_stage": "canary", "canary_percent": 150}))
    assert exc_info.value.status_code == 400
    with pytest.raises(HTTPException) as exc_info:
        await admin_api.promote_model_version(
            "llama", StubRequest({"version": "nope", "target_stage": "canary"}))
    assert exc_info.value.status_code == 404


async def test_endpoint_rollback(admin_api, fleet):
    for version in ("v1", "v2"):
        await admin_api.register_model_version("llama", StubRequest({"version": version, "hf_repo": "org/llama"}))
        await admin_api.promote_model_version(
            "llama", StubRequest({"version": version, "target_stage": "canary"}))
        await admin_api.promote_model_version(
            "llama", StubRequest({"version": version, "target_stage": "prod"}))
    response = await admin_api.rollback_model("llama", StubRequest({}))
    assert response["version"]["version"] == "v1"
    assert response["version"]["stage"] == "prod"
    assert fleet.get_desired_state()["llama"]["version"] == "v1"


async def test_endpoint_rollback_without_history_is_409(admin_api):
    await admin_api.register_model_version("llama", StubRequest({"version": "v1", "hf_repo": "org/llama"}))
    with pytest.raises(HTTPException) as exc_info:
        await admin_api.rollback_model("llama", StubRequest({}))
    assert exc_info.value.status_code == 409


async def test_endpoint_set_canary_traffic(admin_api, fleet):
    await admin_api.register_model_version("llama", StubRequest({"version": "v1", "hf_repo": "org/llama"}))
    await admin_api.promote_model_version(
        "llama", StubRequest({"version": "v1", "target_stage": "canary", "canary_percent": 10}))
    response = await admin_api.set_canary_traffic(
        "llama", StubRequest({"version": "v1", "percent": 35}))
    assert response["version"]["canary_percent"] == 35
    assert fleet.get_desired_state()["llama"]["canary_traffic_percent"] == 35
    with pytest.raises(HTTPException) as exc_info:
        await admin_api.set_canary_traffic("llama", StubRequest({"version": "v1", "percent": 101}))
    assert exc_info.value.status_code == 400


async def test_endpoint_set_canary_traffic_rejects_non_canary(admin_api):
    await admin_api.register_model_version("llama", StubRequest({"version": "v1", "hf_repo": "org/llama"}))
    with pytest.raises(HTTPException) as exc_info:
        await admin_api.set_canary_traffic("llama", StubRequest({"version": "v1", "percent": 10}))
    assert exc_info.value.status_code == 409


async def test_endpoint_fleet_unavailable_is_503(admin_api, monkeypatch):
    await admin_api.register_model_version("llama", StubRequest({"version": "v1", "hf_repo": "org/llama"}))
    monkeypatch.setattr(admin_api, "open_fleet_registry", lambda: None)
    with pytest.raises(HTTPException) as exc_info:
        await admin_api.promote_model_version(
            "llama", StubRequest({"version": "v1", "target_stage": "canary"}))
    assert exc_info.value.status_code == 503
    with pytest.raises(HTTPException) as exc_info:
        await admin_api.rollback_model("llama", StubRequest({}))
    assert exc_info.value.status_code == 503
