"""GPU setup boundary tests; no package manager or GPU is required."""
import importlib.util
from pathlib import Path

import pytest

from services.model_manager import vllm_server

spec = importlib.util.spec_from_file_location(
    "nvidia_setup", Path(__file__).parents[2] / "scripts/nvidia_setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def test_plan_never_executes_packages(monkeypatch):
    monkeypatch.setattr(setup, "has_nvidia", lambda: False)
    monkeypatch.setattr(setup.subprocess, "run", lambda *a, **k: pytest.fail("mutation in plan"))
    assert setup.main([]) == 0


def test_install_rejects_unsupported_os(monkeypatch):
    monkeypatch.setattr(setup.platform, "freedesktop_os_release", lambda: {"ID": "debian"})
    monkeypatch.setattr(setup, "has_nvidia", lambda: True)
    monkeypatch.setattr(setup.subprocess, "run", lambda *a, **k: pytest.fail("mutation"))
    with pytest.raises(SystemExit):
        setup.main(["--apply"])


@pytest.mark.parametrize("driver,toolkit", [("--evil", None), ("570;id", None), ("auto", "../12")])
def test_invalid_package_selection(driver, toolkit):
    with pytest.raises(ValueError):
        setup.commands(driver, toolkit)


def test_explicit_driver_and_optional_toolkit():
    plan = setup.commands("570-server-open", "12-8")
    assert plan[3] == ["ubuntu-drivers", "install", "--gpgpu", "nvidia:570-server-open"]
    assert plan[-1][-1] == "cuda-toolkit-12-8"
    assert not any("cuda-toolkit" in arg for cmd in setup.commands() for arg in cmd)


@pytest.mark.parametrize("key", ["host", "port", "model", "served_model_name", "uds", "config"])
def test_config_cannot_change_managed_endpoint(tmp_path, monkeypatch, key):
    config = tmp_path / "serve.yaml"
    config.write_text(f"{key}: changed\n")
    monkeypatch.setenv("VLLM_BIN", "/fake/vllm")
    monkeypatch.setenv("VLLM_CONFIG", str(config))
    with pytest.raises(ValueError, match="platform"):
        vllm_server.build_command({"path": "/models/m", "name": "m"}, 8100)


def test_native_config_and_registry_precedence(tmp_path, monkeypatch):
    config = tmp_path / "serve.yaml"
    config.write_text("dtype: bfloat16\nmax-model-len: 4096\n")
    monkeypatch.setenv("VLLM_BIN", "/fake/vllm")
    monkeypatch.setenv("VLLM_CONFIG", str(config))
    command = vllm_server.build_command({"path": "/models/m", "name": "m", "max_model_len": 8192}, 8100)
    assert command[command.index("--config") + 1] == str(config)
    assert command[command.index("--max-model-len") + 1] == "8192"
    assert command[command.index("--host") + 1] == "127.0.0.1"


def test_driver_apply_installs_matching_utilities(monkeypatch):
    monkeypatch.setattr(setup.platform, "freedesktop_os_release", lambda: {"ID": "ubuntu"})
    monkeypatch.setattr(setup, "has_nvidia", lambda: True)
    monkeypatch.setattr(setup.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(setup.shutil, "which", lambda _: None)
    monkeypatch.setattr(setup.subprocess, "check_output", lambda *a, **k:
                        "nvidia-driver-570-server-open install ok installed\n"
                        "nvidia-driver-550 deinstall ok config-files\n")
    calls = []
    monkeypatch.setattr(setup.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    assert setup.main(["--apply"]) == 0
    assert calls[-1] == ["sudo", "apt-get", "install", "-y", "nvidia-utils-570-server"]
    assert all(cmd[0] == "sudo" for cmd in calls)


def test_driver_apply_stops_on_package_failure(monkeypatch):
    monkeypatch.setattr(setup.platform, "freedesktop_os_release", lambda: {"ID": "ubuntu"})
    monkeypatch.setattr(setup, "has_nvidia", lambda: True)
    calls = []
    def fail(cmd, **kwargs):
        calls.append(cmd)
        raise setup.subprocess.CalledProcessError(100, cmd)
    monkeypatch.setattr(setup.subprocess, "run", fail)
    with pytest.raises(setup.subprocess.CalledProcessError):
        setup.main(["--apply"])
    assert len(calls) == 1


def test_driver_apply_requires_hardware(monkeypatch):
    monkeypatch.setattr(setup.platform, "freedesktop_os_release", lambda: {"ID": "ubuntu"})
    monkeypatch.setattr(setup, "has_nvidia", lambda: False)
    monkeypatch.setattr(setup.subprocess, "run", lambda *a, **k: pytest.fail("mutation"))
    with pytest.raises(SystemExit):
        setup.main(["--apply"])


@pytest.mark.parametrize("content", ["[]", "null", "1: invalid"])
def test_bad_yaml_shape_fails_before_spawn(tmp_path, monkeypatch, content):
    config = tmp_path / "serve.yaml"
    config.write_text(content)
    monkeypatch.setenv("VLLM_BIN", "/fake/vllm")
    monkeypatch.setenv("VLLM_CONFIG", str(config))
    with pytest.raises(ValueError, match="mapping"):
        vllm_server.build_command({"path": "/models/m", "name": "m"}, 8100)


def test_missing_config_fails_and_empty_env_disables(monkeypatch, tmp_path):
    monkeypatch.setenv("VLLM_BIN", "/fake/vllm")
    monkeypatch.setenv("VLLM_CONFIG", str(tmp_path / "missing.yaml"))
    with pytest.raises(FileNotFoundError):
        vllm_server.build_command({"path": "/models/m", "name": "m"}, 8100)
    monkeypatch.setenv("VLLM_CONFIG", "")
    assert "--config" not in vllm_server.build_command({"path": "/models/m", "name": "m"}, 8100)
