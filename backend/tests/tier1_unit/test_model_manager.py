"""Model registry, downloader, vLLM supervision, LiteLLM sync and admin API."""
import os
from types import SimpleNamespace

import httpx
import pytest
import yaml

from services.auth_gateway import server as auth_gateway_srv
from services.model_manager import downloader, litellm_sync, registry, vllm_server
from services.model_manager import router as model_router
from services.model_manager.registry import ModelRegistry


@pytest.fixture
def store(tmp_path):
    return ModelRegistry(path=str(tmp_path / "registry.json"), models_dir=str(tmp_path / "models"))


# --- registry ---------------------------------------------------------------

def test_validate_name_and_repo_reject_traversal():
    assert registry.validate_name("Qwen2.5-Coder-14B") == "Qwen2.5-Coder-14B"
    for bad in ("..", "../etc", "a/b", "", "-x", "x" * 70):
        with pytest.raises(ValueError):
            registry.validate_name(bad)
    assert registry.validate_hf_repo("org/model-1.0") == "org/model-1.0"
    for bad in ("org", "org/", "/model", "org/mod el", "a/b/c"):
        with pytest.raises(ValueError):
            registry.validate_hf_repo(bad)


def test_registry_roundtrip_and_path_confinement(store):
    store.upsert("m1", {"hf_repo": "org/m1", "status": registry.STATUS_REGISTERED})
    assert store.get("m1")["hf_repo"] == "org/m1"
    assert [entry["name"] for entry in store.all()] == ["m1"]
    assert store.path_for("m1") == os.path.join(store.models_dir, "m1")
    with pytest.raises(ValueError):
        store.path_for("../escape")
    assert store.delete("m1") is True
    assert store.get("m1") is None


def test_corrupt_registry_is_quarantined(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text("{not json")
    store = ModelRegistry(path=str(path), models_dir=str(tmp_path / "models"))
    assert store.all() == []
    assert any(name.startswith("registry.json.corrupt-") for name in os.listdir(tmp_path))


# --- downloader -------------------------------------------------------------

def test_parse_hf_reference_accepts_url_and_revision():
    assert downloader.parse_hf_reference("org/model") == ("org/model", None)
    assert downloader.parse_hf_reference("https://huggingface.co/org/model") == ("org/model", None)
    assert downloader.parse_hf_reference("https://huggingface.co/org/model/tree/v2.0") == ("org/model", "v2.0")
    with pytest.raises(ValueError):
        downloader.parse_hf_reference("not a repo")


def test_download_sync_success_and_failure(store):
    store.upsert("m1", {"hf_repo": "org/m1", "status": registry.STATUS_REGISTERED})

    def fake_snapshot(**kwargs):
        os.makedirs(kwargs["local_dir"], exist_ok=True)
        with open(os.path.join(kwargs["local_dir"], "model.safetensors"), "wb") as handle:
            handle.write(b"x" * 16)
        return kwargs["local_dir"]

    entry = downloader.download_sync("m1", store, snapshot_fn=fake_snapshot)
    assert entry["status"] == registry.STATUS_DOWNLOADED
    assert entry["size_bytes"] == 16

    def failing_snapshot(**kwargs):
        raise OSError("disk full")

    with pytest.raises(OSError):
        downloader.download_sync("m1", store, snapshot_fn=failing_snapshot)
    assert store.get("m1")["status"] == registry.STATUS_ERROR
    assert "disk full" in store.get("m1")["last_error"]


def test_download_preflight_blocks_before_status_change(store):
    store.upsert("m1", {"hf_repo": "org/m1", "status": registry.STATUS_REGISTERED})
    with pytest.raises(OSError):
        downloader.download_sync("m1", store, snapshot_fn=lambda **_: "x", min_free_bytes=10 ** 18)
    assert store.get("m1")["status"] == registry.STATUS_REGISTERED


def test_start_download_rejects_concurrent(store, monkeypatch):
    store.upsert("m1", {"hf_repo": "org/m1", "status": registry.STATUS_REGISTERED})
    monkeypatch.setattr(downloader, "_run_job", lambda *a, **k: None)
    downloader.start_download("m1", store)
    with pytest.raises(RuntimeError):
        downloader.start_download("m1", store)


# --- vLLM supervision -------------------------------------------------------

class FakeProc:
    def __init__(self, pid=4242, exit_after=None):
        self.pid = pid
        self._exit_after = exit_after
        self._polls = 0
        self.terminated = False

    def poll(self):
        self._polls += 1
        if self._exit_after is not None and self._polls >= self._exit_after:
            return 1
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminated = True


def _downloaded(store, name="m1", path=None):
    target = path or store.path_for(name)
    os.makedirs(target, exist_ok=True)
    store.upsert(name, {"hf_repo": "org/m1", "status": registry.STATUS_DOWNLOADED, "path": target})
    return target


def test_build_command_includes_flags(monkeypatch):
    monkeypatch.setenv("VLLM_BIN", "/fake/vllm")
    command = vllm_server.build_command(
        {"name": "m1", "path": "/models/m1", "max_model_len": 4096, "quantization": "awq",
         "tensor_parallel_size": 1, "gpu_memory_utilization": 0.9}, 8100)
    assert command[:3] == ["/fake/vllm", "serve", "/models/m1"]
    assert "--served-model-name" in command and "m1" in command
    assert "--max-model-len" in command and "4096" in command
    assert "--quantization" in command and "awq" in command


def test_vllm_start_and_stop_lifecycle(store, monkeypatch):
    monkeypatch.setattr(vllm_server, "vllm_bin", lambda: "/fake/vllm")
    monkeypatch.setattr(vllm_server, "_port_free", lambda port: True)
    _downloaded(store)
    captured = {}

    def popen_fn(command, log_file):
        captured["command"] = command
        return FakeProc(pid=4242)

    entry = vllm_server.start("m1", store, popen_fn=popen_fn, health_fn=lambda port: True, ready_timeout=5)
    assert entry["status"] == registry.STATUS_RUNNING
    assert entry["server"]["pid"] == 4242
    assert captured["command"][:2] == ["/fake/vllm", "serve"]

    stopped = vllm_server.stop("m1", store)
    assert stopped["status"] == registry.STATUS_STOPPED
    assert stopped["server"]["pid"] is None


def test_vllm_start_unhealthy_is_error(store, monkeypatch):
    monkeypatch.setattr(vllm_server, "vllm_bin", lambda: "/fake/vllm")
    monkeypatch.setattr(vllm_server, "_port_free", lambda port: True)
    _downloaded(store)
    with pytest.raises(RuntimeError):
        vllm_server.start("m1", store, popen_fn=lambda c, f: FakeProc(), health_fn=lambda port: False,
                          ready_timeout=0.2)
    assert store.get("m1")["status"] == registry.STATUS_ERROR


# --- LiteLLM sync -----------------------------------------------------------

def test_litellm_sync_adds_and_removes_managed_entries(store, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"model_list": [
        {"model_name": "fast-model", "litellm_params": {"model": "openai/x"}}
    ]}))
    _downloaded(store, "m1")
    store.update("m1", status=registry.STATUS_RUNNING, server={"pid": 1, "port": 8100, "status": "running"})
    calls = []
    result = litellm_sync.sync(store, config_path=str(cfg), restart=True,
                              run_fn=lambda command, cwd: (calls.append(command), SimpleNamespace(returncode=0, stdout="ok"))[1],
                              platform_sh="/bin/true")
    assert result["changed"] is True
    data = yaml.safe_load(cfg.read_text())
    names = [entry["model_name"] for entry in data["model_list"]]
    assert names == ["fast-model", "m1"]
    assert litellm_sync.managed_model_names(str(cfg)) == ["m1"]
    assert calls == [["/bin/true", "service", "litellm", "restart"]]

    store.update("m1", status=registry.STATUS_STOPPED, server={"pid": None, "port": None, "status": "stopped"})
    litellm_sync.sync(store, config_path=str(cfg), restart=False)
    data = yaml.safe_load(cfg.read_text())
    assert [entry["model_name"] for entry in data["model_list"]] == ["fast-model"]


# --- inference routing ------------------------------------------------------

def test_inference_routes_registered_running_model(monkeypatch, store):
    _downloaded(store, "m1")
    store.update("m1", status=registry.STATUS_RUNNING, server={"pid": 1, "port": 8123, "status": "running"})
    import services.inference_engine.server as inference_server
    monkeypatch.setattr(registry, "model_registry", store)
    assert inference_server._local_model_port("m1") == 8123
    assert inference_server._is_registry_model("m1") is True
    assert inference_server._local_model_port("nope") is None


# --- admin API --------------------------------------------------------------

@pytest.fixture
def api_env(monkeypatch, store):
    monkeypatch.setattr(model_router, "model_registry", store)
    monkeypatch.setattr(auth_gateway_srv, "load_valid_tokens",
                        lambda: {"admin-token": "sysadmin-admin", "user-token": "sysadmin-01"})
    return {"Authorization": "Bearer admin-token"}


async def _client():
    from services.agent_tools import server as agent_server
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=agent_server.app), base_url="http://test")


@pytest.mark.asyncio
async def test_models_api_auth_and_validation(api_env, store):
    async with await _client() as client:
        assert (await client.get("/api/v1/models")).status_code == 401
        assert (await client.get("/api/v1/models", headers={"Authorization": "Bearer user-token"})).status_code == 403
        bad = await client.post("/api/v1/models", json={"hf_repo": "not a repo"}, headers=api_env)
        assert bad.status_code == 400
        created = await client.post("/api/v1/models", json={"hf_repo": "org/model"}, headers=api_env)
        assert created.status_code == 201
        assert created.json()["model"]["name"] == "model"
        listed = await client.get("/api/v1/models", headers=api_env)
        assert [entry["name"] for entry in listed.json()["models"]] == ["model"]


@pytest.mark.asyncio
async def test_models_api_download_start_and_delete(api_env, store, monkeypatch):
    starts = []
    monkeypatch.setattr(model_router.downloader, "start_download",
                        lambda name, registry_obj: starts.append(name) or {"status": "downloading"})
    monkeypatch.setattr(model_router.vllm_server, "start", lambda name, registry_obj: {"server": {"port": 8100}})
    monkeypatch.setattr(model_router.vllm_server, "is_running", lambda entry: False)
    monkeypatch.setattr(model_router.litellm_sync, "sync", lambda registry_obj: {"changed": False})
    async with await _client() as client:
        assert (await client.post("/api/v1/models", json={"hf_repo": "org/model"}, headers=api_env)).status_code == 201
        assert (await client.post("/api/v1/models/model/download", headers=api_env)).status_code == 202
        assert starts == ["model"]
        # start is rejected while the model is not downloaded
        assert (await client.post("/api/v1/models/model/start", headers=api_env)).status_code == 409
        store.update("model", status=registry.STATUS_DOWNLOADED)
        # exercise the synchronous start job body directly (no thread timing)
        model_router._start_job("model")
        assert model_router._START_JOBS["model"]["status"] == "running"
        deleted = await client.delete("/api/v1/models/model", headers=api_env)
        assert deleted.status_code == 200
        assert store.get("model") is None
