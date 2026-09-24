"""Model registry, downloader, vLLM supervision, LiteLLM sync and admin API."""
import os
from types import SimpleNamespace

import httpx
import pytest
import yaml
from fastapi import HTTPException, Request

from services.auth_gateway import server as auth_gateway_srv
from services.model_manager import downloader, llamacpp_server, litellm_sync, registry, vllm_server
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


def test_validate_engine_and_gguf_filename():
    assert registry.validate_engine(None) == registry.ENGINE_VLLM
    assert registry.validate_engine("llamacpp") == registry.ENGINE_LLAMACPP
    for bad in ("unknown", "../vllm", ["llamacpp"]):
        with pytest.raises((ValueError, TypeError)):
            registry.validate_engine(bad)
    for good in ("model.gguf", "Qwythos-9B.Q6_K.GGUF"):
        assert registry.validate_gguf_file(good) == good
    for bad in ("../model.gguf", "dir/model.gguf", ".gguf", "model.safetensors", "x" * 130 + ".gguf"):
        with pytest.raises(ValueError):
            registry.validate_gguf_file(bad)


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


def test_gguf_download_fetches_only_selected_file(store):
    store.upsert("q9", {"hf_repo": "org/q9", "engine": "llamacpp", "gguf_file": "q9.Q6_K.gguf",
                         "status": registry.STATUS_REGISTERED})
    calls = []

    def file_download(**kwargs):
        calls.append(kwargs)
        path = os.path.join(kwargs["local_dir"], kwargs["filename"])
        with open(path, "wb") as handle:
            handle.write(b"gguf payload")
        return path

    def never_snapshot(**_kwargs):
        pytest.fail("GGUF download must not snapshot the entire repository")

    entry = downloader.download_sync("q9", store, snapshot_fn=never_snapshot, file_download_fn=file_download)
    assert entry["status"] == registry.STATUS_DOWNLOADED
    assert entry["size_bytes"] == len(b"gguf payload")
    assert len(calls) == 1
    assert {key: value for key, value in calls[0].items() if key != "token"} == {
        "repo_id": "org/q9", "filename": "q9.Q6_K.gguf", "revision": None,
        "local_dir": store.path_for("q9"),
    }
    assert calls[0]["token"] is None or isinstance(calls[0]["token"], str)


def test_gguf_download_rejects_symlink_result_and_records_failure(store, tmp_path):
    store.upsert("q9", {"hf_repo": "org/q9", "engine": "llamacpp", "gguf_file": "q9.gguf",
                         "status": registry.STATUS_REGISTERED})
    outside = tmp_path / "outside.gguf"
    outside.write_bytes(b"outside")

    def symlink_download(**kwargs):
        path = os.path.join(kwargs["local_dir"], kwargs["filename"])
        os.symlink(outside, path)
        return path

    with pytest.raises(ValueError, match="regular file"):
        downloader.download_sync("q9", store, file_download_fn=symlink_download)
    assert store.get("q9")["status"] == registry.STATUS_ERROR


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
        if self.terminated:
            return 0
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


def test_llamacpp_command_defaults_and_child_cuda_path(store, monkeypatch):
    monkeypatch.setenv("LLAMACPP_BIN", "/opt/llama/llama-server")
    monkeypatch.setenv("LLAMACPP_LD_LIBRARY_PATH", "/cuda/lib:/cublas/lib")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/system/lib")
    command = llamacpp_server.build_command(
        {"name": "q9", "ctx_size": 2048, "n_gpu_layers": "all", "flash_attn": True},
        8110, "/managed/q9/model.gguf",
    )
    assert command == [
        "/opt/llama/llama-server", "--model", "/managed/q9/model.gguf", "--alias", "q9",
        "--host", "127.0.0.1", "--port", "8110", "--ctx-size", "2048", "--parallel", "1",
        "--n-gpu-layers", "all", "--flash-attn", "on", "--reasoning", "off",
    ]
    assert llamacpp_server._child_environment()["LD_LIBRARY_PATH"] == "/cuda/lib:/cublas/lib:/system/lib"


def test_llamacpp_path_is_confined_and_rejects_symlink(store, tmp_path):
    store.upsert("q9", {"hf_repo": "org/q9", "engine": "llamacpp", "gguf_file": "q9.gguf"})
    model_dir = store.path_for("q9")
    os.makedirs(model_dir, exist_ok=True)
    model = os.path.join(model_dir, "q9.gguf")
    with open(model, "wb") as handle:
        handle.write(b"gguf")
    assert llamacpp_server.model_file_path("q9", store) == model
    os.unlink(model)
    outside = tmp_path / "outside.gguf"
    outside.write_bytes(b"outside")
    os.symlink(outside, model)
    with pytest.raises(ValueError, match="regular file"):
        llamacpp_server.model_file_path("q9", store)

    store.update("q9", path=str(tmp_path))
    os.unlink(model)
    with open(model, "wb") as handle:
        handle.write(b"gguf")
    # Registry path fields do not redirect the supervisor away from path_for(name).
    assert llamacpp_server.model_file_path("q9", store) == model


def test_llamacpp_liveness_does_not_probe_or_signal_on_transient_health_failure(store, monkeypatch):
    store.upsert("q9", {"hf_repo": "org/q9", "engine": "llamacpp", "gguf_file": "q9.gguf",
                         "status": registry.STATUS_RUNNING,
                         "server": {"pid": 4343, "port": 8124, "status": "running"}})
    probes = []
    monkeypatch.setattr(vllm_server, "process_alive", lambda _pid: True)
    monkeypatch.setattr(llamacpp_server, "_process_matches", lambda _entry, _store: True)
    monkeypatch.setattr(llamacpp_server, "_default_health", lambda _port: probes.append(_port) or False)
    assert llamacpp_server.is_running(store.get("q9"), store)
    assert probes == []


def test_llamacpp_stop_waits_for_pid_before_releasing_port(store, monkeypatch):
    store.upsert("q9", {"hf_repo": "org/q9", "engine": "llamacpp", "gguf_file": "q9.gguf",
                         "status": registry.STATUS_RUNNING,
                         "server": {"pid": 4343, "port": 8124, "status": "running"}})
    alive_checks = iter([True, True, False])
    clock = {"now": 0.0}
    signals = []
    monkeypatch.setattr(vllm_server, "process_alive", lambda _pid: True)
    monkeypatch.setattr(llamacpp_server, "_process_matches", lambda _entry, _store: next(alive_checks, False))
    monkeypatch.setattr(llamacpp_server.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(llamacpp_server.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(llamacpp_server.time, "sleep", lambda seconds: clock.update(now=clock["now"] + seconds))
    stopped = llamacpp_server.stop("q9", store, grace=1)
    assert signals == [(4343, 15)]
    assert stopped["server"] == {"pid": None, "port": None, "status": "stopped"}


def test_llamacpp_stop_keeps_port_reserved_if_process_cannot_be_reaped(store, monkeypatch):
    store.upsert("q9", {"hf_repo": "org/q9", "engine": "llamacpp", "gguf_file": "q9.gguf",
                         "status": registry.STATUS_RUNNING,
                         "server": {"pid": 4343, "port": 8124, "status": "running"}})
    monkeypatch.setattr(vllm_server, "process_alive", lambda _pid: True)
    monkeypatch.setattr(llamacpp_server, "_process_matches", lambda _entry, _store: True)
    signals = []
    clock = {"now": 0.0}
    monkeypatch.setattr(llamacpp_server.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(llamacpp_server.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(llamacpp_server.time, "sleep", lambda seconds: clock.update(now=clock["now"] + seconds))
    with pytest.raises(RuntimeError, match="state and port remain reserved"):
        llamacpp_server.stop("q9", store, grace=0)
    assert store.get("q9")["server"]["port"] == 8124
    assert signals == [(4343, 15), (4343, 9)]


def test_llamacpp_stop_does_not_signal_pid_reused_by_another_process(store, monkeypatch):
    store.upsert("q9", {"hf_repo": "org/q9", "engine": "llamacpp", "gguf_file": "q9.gguf",
                         "status": registry.STATUS_RUNNING,
                         "server": {"pid": 4343, "port": 8124, "status": "running"}})
    signals = []
    monkeypatch.setattr(vllm_server, "process_alive", lambda _pid: True)
    monkeypatch.setattr(llamacpp_server, "_process_matches", lambda _entry, _store: False)
    monkeypatch.setattr(llamacpp_server.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    stopped = llamacpp_server.stop("q9", store)
    assert signals == []
    assert stopped["status"] == registry.STATUS_STOPPED


def test_llamacpp_start_and_stop_lifecycle(store, monkeypatch, tmp_path):
    monkeypatch.setattr(llamacpp_server, "llamacpp_bin", lambda: "/fake/llama-server")
    monkeypatch.setattr(vllm_server, "_port_free", lambda port: True)
    monkeypatch.setattr(vllm_server, "log_path", lambda name: str(tmp_path / f"{name}.test.log"))
    model_dir = store.path_for("q9")
    os.makedirs(model_dir, exist_ok=True)
    with open(os.path.join(model_dir, "q9.gguf"), "wb") as handle:
        handle.write(b"gguf")
    store.upsert("q9", {"hf_repo": "org/q9", "engine": "llamacpp", "gguf_file": "q9.gguf",
                         "status": registry.STATUS_DOWNLOADED})
    captured = {}

    def popen_fn(command, log_file):
        captured["command"] = command
        return FakeProc(pid=4343)

    entry = llamacpp_server.start("q9", store, popen_fn=popen_fn, health_fn=lambda _port: True, ready_timeout=5)
    assert entry["status"] == registry.STATUS_RUNNING
    assert entry["server"]["pid"] == 4343
    assert "--alias" in captured["command"] and "q9" in captured["command"]
    assert llamacpp_server.stop("q9", store)["status"] == registry.STATUS_STOPPED
    assert entry["server"]["port"] in range(8100, 8200)


def test_llamacpp_start_refuses_external_file_before_spawning(store, tmp_path):
    outside = tmp_path / "outside.gguf"
    outside.write_bytes(b"gguf")
    store.upsert("q9", {"hf_repo": "org/q9", "engine": "llamacpp", "gguf_file": "q9.gguf",
                         "status": registry.STATUS_DOWNLOADED})
    os.makedirs(store.path_for("q9"), exist_ok=True)
    os.symlink(outside, os.path.join(store.path_for("q9"), "q9.gguf"))
    calls = []
    with pytest.raises(ValueError, match="regular file"):
        llamacpp_server.start("q9", store, popen_fn=lambda *args: calls.append(args))
    assert calls == []


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
    monkeypatch.setattr(vllm_server, "is_running", lambda _entry: True)
    assert inference_server._local_model_port("m1") == 8123
    assert inference_server._is_registry_model("m1") is True
    assert inference_server._local_model_port("nope") is None


def test_inference_routes_running_gguf_and_fails_closed_when_registry_is_unavailable(monkeypatch, store):
    store.upsert("q9", {"hf_repo": "org/q9", "engine": "llamacpp", "gguf_file": "q9.gguf",
                         "status": registry.STATUS_RUNNING,
                         "server": {"pid": 4343, "port": 8124, "status": "running"}})
    import services.inference_engine.server as inference_server
    monkeypatch.setattr(registry, "model_registry", store)
    monkeypatch.setattr(llamacpp_server, "process_matches", lambda _entry, _store: True)
    assert inference_server._local_model_port("q9") == 8124
    assert inference_server._is_registry_model("q9") is True

    class BrokenRegistry:
        def all(self):
            raise OSError("registry unreadable")

        def get(self, _name):
            raise OSError("registry unreadable")

    monkeypatch.setattr(registry, "model_registry", BrokenRegistry())
    with pytest.raises(OSError, match="registry unreadable"):
        inference_server._local_model_port("q9")
    with pytest.raises(OSError, match="registry unreadable"):
        inference_server._is_registry_model("q9")


@pytest.mark.asyncio
async def test_registered_gguf_unavailable_returns_503_instead_of_simulation(monkeypatch, store):
    import services.inference_engine.server as inference_server
    monkeypatch.setattr(registry, "model_registry", store)
    store.upsert("q9", {"hf_repo": "org/q9", "engine": "llamacpp", "gguf_file": "q9.gguf",
                         "status": registry.STATUS_STOPPED})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=inference_server.app),
                                 base_url="http://test") as client:
        stopped = await client.post("/v1/chat/completions", json={
            "model": "q9", "messages": [{"role": "user", "content": "hello"}],
        })
        assert stopped.status_code == 503
        assert "registered but not running" in stopped.json()["detail"]

        class BrokenRegistry:
            def all(self):
                raise OSError("registry unreadable")

            def get(self, _name):
                raise OSError("registry unreadable")

        monkeypatch.setattr(registry, "model_registry", BrokenRegistry())
        broken = await client.post("/v1/chat/completions", json={
            "model": "fast-model", "messages": [{"role": "user", "content": "hello"}],
        })
        assert broken.status_code == 503
        assert "registry unavailable" in broken.json()["detail"]


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
async def test_gguf_registration_requires_a_filename_and_keeps_engine_settings(api_env, store):
    async with await _client() as client:
        missing = await client.post("/api/v1/models", headers=api_env,
                                    json={"hf_repo": "org/q9", "engine": "llamacpp"})
        assert missing.status_code == 400
        traversal = await client.post("/api/v1/models", headers=api_env,
                                      json={"hf_repo": "org/q9", "engine": "llamacpp",
                                            "gguf_file": "../outside.gguf"})
        assert traversal.status_code == 400
        bad_engine = await client.post("/api/v1/models", headers=api_env,
                                        json={"hf_repo": "org/q9", "engine": "other"})
        assert bad_engine.status_code == 400
        created = await client.post("/api/v1/models", headers=api_env,
                                    json={"hf_repo": "org/q9", "name": "q9", "engine": "llamacpp",
                                          "gguf_file": "q9.Q6_K.gguf", "ctx_size": 4096,
                                          "n_gpu_layers": "all", "flash_attn": True})
        assert created.status_code == 201
        model = created.json()["model"]
        assert model["engine"] == "llamacpp"
        assert model["gguf_file"] == "q9.Q6_K.gguf"
        assert model["ctx_size"] == 4096
        assert model["path"] == store.path_for("q9")


@pytest.mark.asyncio
async def test_running_model_cannot_be_reregistered_and_orphaned(api_env, store, monkeypatch):
    store.upsert("q9", {"hf_repo": "org/q9", "engine": "llamacpp", "gguf_file": "q9.gguf",
                         "status": registry.STATUS_RUNNING,
                         "server": {"pid": 1234, "port": 8100, "status": "running"}})
    monkeypatch.setattr(llamacpp_server, "process_matches", lambda _entry, _store: True)
    async with await _client() as client:
        response = await client.post("/api/v1/models", headers=api_env,
                                     json={"hf_repo": "org/other", "name": "q9", "engine": "llamacpp",
                                           "gguf_file": "other.gguf"})
    assert response.status_code == 409
    assert store.get("q9")["gguf_file"] == "q9.gguf"


def test_concurrent_start_requests_share_one_start_sentinel(store, monkeypatch):
    store.upsert("q9", {"hf_repo": "org/q9", "engine": "llamacpp", "gguf_file": "q9.gguf",
                         "status": registry.STATUS_DOWNLOADED,
                         "server": {"pid": None, "port": None, "status": "stopped"}})
    monkeypatch.setattr(model_router, "model_registry", store)
    monkeypatch.setattr(model_router.llamacpp_server, "process_matches", lambda _entry, _store: False)
    monkeypatch.setattr(model_router, "_START_JOBS", {})
    monkeypatch.setattr(model_router, "_audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(model_router, "require_admin", lambda _request: "sysadmin-admin")
    monkeypatch.setattr(model_router.threading.Thread, "start", lambda _thread: None)
    request = Request({"type": "http", "method": "POST", "path": "/api/v1/models/q9/start", "headers": []})
    first = model_router.start_model("q9", request)
    assert first["status"] == "starting"
    with pytest.raises(HTTPException) as exc:
        model_router.start_model("q9", request)
    assert exc.value.status_code == 409
    assert model_router._START_JOBS["q9"]["status"] == "starting"


def test_failed_litellm_sync_stops_started_local_model(store, monkeypatch):
    store.upsert("q9", {"hf_repo": "org/q9", "engine": "llamacpp", "gguf_file": "q9.gguf",
                         "status": registry.STATUS_DOWNLOADED})
    starts, stops, sync_calls = [], [], []

    def start(name, registry_obj):
        starts.append(name)
        return registry_obj.update(name, status=registry.STATUS_RUNNING,
                                   server={"pid": 4242, "port": 8100, "status": "running"})

    def stop(name, registry_obj):
        stops.append(name)
        return registry_obj.update(name, status=registry.STATUS_STOPPED,
                                   server={"pid": None, "port": None, "status": "stopped"})

    def sync(_store, restart=True):
        sync_calls.append(restart)
        if restart:
            raise OSError("LiteLLM unavailable")
        return {"changed": False}

    monkeypatch.setattr(llamacpp_server, "start", start)
    monkeypatch.setattr(llamacpp_server, "stop", stop)
    monkeypatch.setattr(model_router, "model_registry", store)
    monkeypatch.setattr(litellm_sync, "sync", sync)
    model_router._start_job("q9")
    assert starts == ["q9"]
    assert stops == ["q9"]
    assert sync_calls == [True, False]
    assert store.get("q9")["status"] == registry.STATUS_ERROR
    assert model_router._START_JOBS["q9"]["status"] == "error"


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
