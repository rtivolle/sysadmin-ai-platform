"""GPU-free unit tests for the installer's vLLM runtime preflight."""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "verify_vllm_runtime", Path(__file__).parents[2] / "scripts/verify_vllm_runtime.py")
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class FakeTensor:
    def cpu(self):
        return self

    def sum(self):
        return self

    def item(self):
        return 4


def seed_runtime(monkeypatch, cuda_available=True, device_count=0):
    """Fake the whole serving stack so main() runs without torch/vllm installed."""
    registry = types.ModuleType("registry")
    registry.ModelRegistry = types.SimpleNamespace(get_supported_archs=lambda: {"FakeArch"})
    monkeypatch.setitem(sys.modules, "vllm.model_executor.models.registry", registry)
    cuda = types.SimpleNamespace(
        is_available=lambda: cuda_available,
        device_count=lambda: device_count,
        get_device_name=lambda index: f"GPU{index}",
    )
    torch = types.ModuleType("torch")
    torch.__version__ = "2.13.0"
    torch.cuda = cuda
    torch.ones = lambda *args, **kwargs: FakeTensor()
    monkeypatch.setitem(sys.modules, "torch", torch)

    def fake_import(name):
        module = types.ModuleType(name)
        module.__version__ = "0.0.0"
        return module

    monkeypatch.setattr(verify.importlib, "import_module", fake_import)


def test_jit_tools_missing_reports_every_gap(monkeypatch, tmp_path):
    monkeypatch.setattr(verify.shutil, "which", lambda name: None)
    cuda = tmp_path / "cuda"
    monkeypatch.setenv("CUDA_HOME", str(cuda))
    with pytest.raises(RuntimeError, match="Missing JIT prerequisites") as exc_info:
        verify.check_jit_tools()
    message = str(exc_info.value)
    for expected in ("ninja", "nvcc", "cuda_runtime.h", "curand.h"):
        assert expected in message


def test_jit_tools_present_passes(monkeypatch, tmp_path):
    monkeypatch.setattr(verify.shutil, "which", lambda name: "/usr/bin/ninja")
    cuda = tmp_path / "cuda"
    (cuda / "bin").mkdir(parents=True)
    (cuda / "include").mkdir()
    (cuda / "bin" / "nvcc").write_text("")
    (cuda / "include" / "cuda_runtime.h").write_text("")
    (cuda / "include" / "curand.h").write_text("")
    monkeypatch.setenv("CUDA_HOME", str(cuda))
    verify.check_jit_tools()


def test_main_rejects_unknown_architecture(monkeypatch):
    seed_runtime(monkeypatch, cuda_available=True, device_count=0)
    monkeypatch.setattr(sys, "argv", ["verify_vllm_runtime.py", "--architecture", "MissingArch"])
    with pytest.raises(RuntimeError, match="Unsupported architecture: MissingArch"):
        verify.main()


def test_main_requires_cuda(monkeypatch):
    seed_runtime(monkeypatch, cuda_available=False, device_count=0)
    monkeypatch.setattr(sys, "argv", ["verify_vllm_runtime.py"])
    with pytest.raises(RuntimeError, match="CUDA unavailable"):
        verify.main()


def test_main_exercises_every_visible_gpu(monkeypatch, capsys):
    seed_runtime(monkeypatch, cuda_available=True, device_count=2)
    monkeypatch.setattr(sys, "argv", ["verify_vllm_runtime.py"])
    verify.main()
    output = capsys.readouterr().out
    assert "GPU 0: GPU0 PASS" in output
    assert "GPU 1: GPU1 PASS" in output
    assert "Runtime preflight PASS" in output
