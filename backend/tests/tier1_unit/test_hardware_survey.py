import json
from types import SimpleNamespace

import httpx
import pytest

from services import hardware_survey
from backend.services.agent_tools import server as agent_server
from services.auth_gateway import server as auth_gateway_srv


LSPCI_FIXTURE = """01:00.0 VGA compatible controller [0300]: NVIDIA Corporation Device [10de:2684]
\tKernel driver in use: nvidia
\tKernel modules: nvidia, nvidia_drm
02:00.0 3D controller [0302]: Advanced Micro Devices, Inc. Device [1002:744c]
\tKernel driver in use: amdgpu
\tKernel modules: amdgpu
03:00.0 Ethernet controller [0200]: Intel Corporation Ethernet [8086:1234]
\tKernel driver in use: e1000e
\tKernel modules: e1000e
"""


def test_query_pci_accelerators_parses_and_filters(monkeypatch):
    monkeypatch.setattr(hardware_survey.shutil, "which", lambda name: "/usr/bin/lspci" if name == "lspci" else None)
    monkeypatch.setattr(hardware_survey.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=LSPCI_FIXTURE))
    devices = hardware_survey.query_pci_accelerators()
    assert len(devices) == 2
    assert devices[0]["slot"] == "01:00.0"
    assert devices[0]["vendor_device_ids"] == "10de:2684"
    assert devices[0]["kernel_driver"] == "nvidia"
    assert devices[0]["kernel_modules"] == ["nvidia", "nvidia_drm"]
    assert devices[1]["kernel_driver"] == "amdgpu"
    assert all("Ethernet" not in device["description"] for device in devices)


def test_query_driver_stack_without_optional_tools_is_nonfatal(monkeypatch):
    monkeypatch.setattr(hardware_survey.shutil, "which", lambda _name: None)
    stack = hardware_survey.query_driver_stack()
    assert stack["nvidia"]["present"] is False
    assert stack["amd"]["rocminfo"]["present"] is False
    assert stack["amd"]["rocm_smi"]["present"] is False


def test_query_driver_stack_parses_nvidia_versions(monkeypatch):
    def which(name):
        return {"nvidia-smi": "/fake/nvidia-smi", "nvcc": "/fake/nvcc"}.get(name)

    def run(command, **kwargs):
        if command[-1] == "--version" and command[0] == "/fake/nvcc":
            output = "Cuda compilation tools, release 12.4, V12.4.99\n"
        elif "--query-gpu=driver_version" in command:
            output = "555.42.02\n"
        elif command == ["/fake/nvidia-smi"]:
            output = "NVIDIA-SMI 555.42.02\n| CUDA Version: 12.5 |\n"
        else:
            output = ""
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(hardware_survey.shutil, "which", which)
    monkeypatch.setattr(hardware_survey.subprocess, "run", run)
    stack = hardware_survey.query_driver_stack()
    assert stack["nvidia"]["smi_driver_version"] == "555.42.02"
    assert stack["nvidia"]["cuda_toolkit"] == "12.4"
    assert stack["nvidia"]["cuda_runtime"] == "12.5"


def test_query_software_versions_survives_missing_packages(monkeypatch):
    monkeypatch.setattr(hardware_survey.importlib.metadata, "version", lambda _name: (_ for _ in ()).throw(PackageNotFoundError()))
    result = hardware_survey.query_software_versions()
    assert result["python"]
    assert all(result[name] is None for name in ("torch", "vllm", "huggingface_hub", "litellm"))


class PackageNotFoundError(Exception):
    pass


def test_query_model_storage_counts_direct_subdirectories(tmp_path):
    models = tmp_path / "models"
    first = models / "model-a"
    second = models / "model-b"
    first.mkdir(parents=True)
    second.mkdir()
    (first / "weights.bin").write_bytes(b"12345")
    (first / "config.json").write_bytes(b"abc")
    (second / "weights.bin").write_bytes(b"1234567")
    result = hardware_survey.query_model_storage(str(models))
    assert result["exists"] is True
    assert result["path"] == str(models)
    assert result["entries"] == [{"name": "model-a", "size_bytes": 8}, {"name": "model-b", "size_bytes": 7}]


def test_export_survey_reports_includes_added_sections(tmp_path):
    survey = {
        "survey_timestamp": "2026-09-24T00:00:00Z", "hostname": "test", "os": {"distro": "Linux", "release": "x", "machine": "x"},
        "gpus": [], "storage": [], "inference_recommendation": {"recommended_mode": "remote", "fast_model": "f", "tp_fast": 1, "heavy_model": "h", "tp_heavy": 1, "max_context": 1, "notes": []},
        "cpu": {"model": "cpu", "cores_logical": 1, "cores_physical": 1, "flags": []}, "ram": {"total_mb": 1, "available_mb": 1, "swap_total_mb": 0},
        "sandbox_confinement": {"path": None, "functional": False}, "cgroups_v2": {},
        "pci_accelerators": [], "driver_stack": {}, "software_versions": {}, "model_storage": {"path": "models", "exists": False, "entries": []},
    }
    _, markdown = hardware_survey.export_survey_reports(survey, str(tmp_path))
    contents = (tmp_path / "hardware_inventory.md").read_text()
    for heading in ("5. PCI Accelerators & Devices", "6. Driver & Toolkit Stack", "7. Software Versions", "8. Model Storage"):
        assert heading in contents
    assert "automatically generated" in contents


@pytest.mark.asyncio
async def test_survey_endpoint_admin_only_and_cached(monkeypatch):
    monkeypatch.setattr(auth_gateway_srv, "load_valid_tokens", lambda: {"user-token": "sysadmin-01", "admin-token": "sysadmin-admin"})
    calls = []
    expected = {"survey": "fixture"}
    monkeypatch.setattr(agent_server, "run_hardware_survey", lambda: (calls.append(True), expected)[1])
    monkeypatch.setattr(agent_server, "_survey_cache", None)
    monkeypatch.setattr(agent_server, "_survey_cache_timestamp", 0.0)
    transport = httpx.ASGITransport(app=agent_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/v1/survey")).status_code == 401
        assert (await client.get("/api/v1/survey", headers={"Authorization": "Bearer user-token"})).status_code == 403
        headers = {"Authorization": "Bearer admin-token"}
        response = await client.get("/api/v1/survey", headers=headers)
        assert response.status_code == 200
        assert response.json() == expected
        cached_response = await client.get("/api/v1/survey", headers=headers)
        assert cached_response.json() == expected
    assert len(calls) == 1
