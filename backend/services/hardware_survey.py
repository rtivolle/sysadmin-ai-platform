#!/usr/bin/env python3
"""
Device & Hardware Surveying Engine for Sysadmin AI Platform (DISC-01)
Discovers GPUs (VRAM, CUDA, NVLink, Architecture), CPU topology, RAM, cgroups v2,
Bubblewrap sandbox readiness, and generates recommended inference configurations.
"""
import os
import sys
import json
import shutil
import platform
import subprocess
import re
import importlib.metadata
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional


def query_pci_accelerators() -> List[Dict[str, Any]]:
    """Return PCI display and processing accelerators discovered by lspci."""
    lspci = shutil.which("lspci")
    if not lspci:
        return []
    try:
        proc = subprocess.run([lspci, "-nnk"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=8)
        if proc.returncode != 0:
            return []
        accelerators = []
        eligible_codes = {"0300", "0302", "0380", "1200"}
        eligible_text = ("vga compatible controller", "3d controller", "display controller", "processing accelerators")
        for block in re.split(r"\n(?=[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F])", proc.stdout.strip()):
            lines = block.splitlines()
            if not lines:
                continue
            header = re.match(r"^([0-9a-fA-F:.]+)\s+(.+)$", lines[0].strip())
            if not header:
                continue
            slot, description = header.groups()
            class_match = re.search(r"\[([0-9a-fA-F]{4})\]", description)
            pci_class = class_match.group(1).lower() if class_match else None
            if pci_class not in eligible_codes and not any(label in description.lower() for label in eligible_text):
                continue
            ids_match = re.search(r"\[([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\]", description)
            driver_match = next((re.match(r"\s*Kernel driver in use:\s*(.+)", line) for line in lines[1:] if "Kernel driver in use:" in line), None)
            modules_match = next((re.match(r"\s*Kernel modules:\s*(.*)", line) for line in lines[1:] if "Kernel modules:" in line), None)
            accelerators.append({
                "slot": slot,
                "description": re.sub(r"\s+\[[0-9a-fA-F]{4}:[0-9a-fA-F]{4}\]", "", description).strip(),
                "vendor_device_ids": ids_match.group(1).lower() if ids_match else None,
                "pci_class": pci_class,
                "kernel_driver": driver_match.group(1).strip() if driver_match else None,
                "kernel_modules": [item.strip() for item in modules_match.group(1).split(",") if item.strip()] if modules_match else [],
            })
        return accelerators
    except Exception:
        return []


def query_driver_stack() -> Dict[str, Any]:
    """Collect optional accelerator kernel and toolkit driver details."""
    def run_optional(command):
        try:
            proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
            return proc.stdout if proc.returncode == 0 else ""
        except Exception:
            return ""

    try:
        with open("/proc/modules", "r", encoding="utf-8") as modules_file:
            loaded = {line.split()[0] for line in modules_file if line.split()}
    except Exception:
        loaded = set()
    module_names = ("nvidia", "nvidia_uvm", "nvidia_drm", "amdgpu", "radeon", "i915", "xe")

    nvidia_smi = shutil.which("nvidia-smi")
    nvcc = shutil.which("nvcc")
    smi_version = None
    cuda_runtime = None
    if nvidia_smi:
        version_output = run_optional([nvidia_smi, "--query-gpu=driver_version", "--format=csv,noheader"])
        smi_version = next((line.strip() for line in version_output.splitlines() if line.strip()), None)
        plain_output = run_optional([nvidia_smi])
        cuda_match = re.search(r"CUDA Version:\s*([\w.]+)", plain_output)
        cuda_runtime = cuda_match.group(1) if cuda_match else None

    proc_version = None
    try:
        with open("/proc/driver/nvidia/version", "r", encoding="utf-8") as version_file:
            proc_version = next((line.strip() for line in version_file if line.strip()), None)
    except Exception:
        pass

    cuda_toolkit = None
    if nvcc:
        nvcc_output = run_optional([nvcc, "--version"])
        toolkit_match = re.search(r"release\s+([\d.]+)", nvcc_output, re.IGNORECASE)
        cuda_toolkit = toolkit_match.group(1) if toolkit_match else None
    if cuda_toolkit is None:
        try:
            with open("/usr/local/cuda/version.json", "r", encoding="utf-8") as version_file:
                version_data = json.load(version_file)
            cuda_toolkit = version_data.get("cuda", {}).get("version") or version_data.get("version")
        except Exception:
            pass

    def optional_version(tool):
        path = shutil.which(tool)
        if not path:
            return {"present": False, "version": None}
        output = run_optional([path, "--version"])
        return {"present": True, "version": next((line.strip() for line in output.splitlines() if line.strip()), None)}

    try:
        drm_entries = os.listdir("/sys/class/drm")
    except Exception:
        drm_entries = []
    intel_render_nodes = sorted(name for name in drm_entries if "renderD" in name)
    intel_cards = sorted(name for name in drm_entries if re.fullmatch(r"card\d+", name))

    return {
        "kernel_release": platform.release(),
        "loaded_modules": [name for name in module_names if name in loaded],
        "nvidia": {"present": bool(nvidia_smi), "smi_driver_version": smi_version, "proc_version": proc_version,
                   "cuda_toolkit": cuda_toolkit, "cuda_runtime": cuda_runtime},
        "amd": {"rocminfo": optional_version("rocminfo"), "rocm_smi": optional_version("rocm-smi")},
        "intel": {"render_nodes": intel_render_nodes, "card_count": len(intel_cards)},
    }


def query_software_versions() -> Dict[str, Any]:
    """Read package metadata without importing optional ML packages."""
    versions = {"python": platform.python_version()}
    for package in ("torch", "vllm", "huggingface_hub", "litellm"):
        try:
            versions[package] = importlib.metadata.version(package)
        except Exception:
            versions[package] = None
    return versions


def query_model_storage(models_dir: str) -> Dict[str, Any]:
    """Summarize capacity and direct model-directory sizes, tolerating I/O errors."""
    result = {"path": os.path.abspath(models_dir), "exists": False, "total_bytes": None, "free_bytes": None, "entries": []}
    try:
        result["exists"] = os.path.isdir(models_dir)
        stat = os.statvfs(models_dir if result["exists"] else os.path.dirname(os.path.abspath(models_dir)) or ".")
        result["total_bytes"] = stat.f_blocks * stat.f_frsize
        result["free_bytes"] = stat.f_bavail * stat.f_frsize
    except Exception:
        pass
    if not result["exists"]:
        return result
    try:
        for entry in os.scandir(models_dir):
            if not entry.is_dir(follow_symlinks=False):
                continue
            size = 0
            for root, dirs, files in os.walk(entry.path, followlinks=False):
                dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(root, name))]
                for name in files:
                    try:
                        size += os.stat(os.path.join(root, name), follow_symlinks=False).st_size
                    except Exception:
                        pass
            result["entries"].append({"name": entry.name, "size_bytes": size})
        result["entries"].sort(key=lambda item: item["name"])
    except Exception:
        pass
    return result

def query_nvidia_smi() -> List[Dict[str, Any]]:
    """Query nvidia-smi for all installed GPUs."""
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return []

    fields = [
        "index", "name", "uuid", "pci.bus_id",
        "driver_version", "temperature.gpu", "power.draw", "power.limit",
        "memory.total", "memory.free", "memory.used",
        "utilization.gpu", "utilization.memory"
    ]
    cmd = [
        nvidia_smi,
        f"--query-gpu={','.join(fields)}",
        "--format=csv,noheader,nounits"
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
        if proc.returncode != 0:
            return []

        gpus = []
        for line in proc.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= len(fields):
                idx = int(parts[0])
                name = parts[1]
                uuid = parts[2]
                bus_id = parts[3]
                driver = parts[4]
                temp = float(parts[5]) if parts[5] != "[N/A]" else 0.0
                power_draw = float(parts[6]) if parts[6] != "[N/A]" else 0.0
                power_limit = float(parts[7]) if parts[7] != "[N/A]" else 0.0
                mem_total = int(parts[8]) if parts[8] != "[N/A]" else 0
                mem_free = int(parts[9]) if parts[9] != "[N/A]" else 0
                mem_used = int(parts[10]) if parts[10] != "[N/A]" else 0
                util_gpu = int(parts[11]) if parts[11] != "[N/A]" else 0
                util_mem = int(parts[12]) if parts[12] != "[N/A]" else 0

                # Determine Architecture & Compute Capability
                arch = "Unknown"
                compute_cap = "N/A"
                if "rtx 8000" in name.lower() or "t4" in name.lower() or "turing" in name.lower():
                    arch = "Turing"
                    compute_cap = "7.5"
                elif "rtx 30" in name.lower() or "a100" in name.lower() or "a10" in name.lower() or "a30" in name.lower():
                    arch = "Ampere"
                    compute_cap = "8.0" if "a100" in name.lower() else "8.6"
                elif "h100" in name.lower() or "h200" in name.lower() or "hopper" in name.lower():
                    arch = "Hopper"
                    compute_cap = "9.0"
                elif "b100" in name.lower() or "b200" in name.lower() or "blackwell" in name.lower():
                    arch = "Blackwell"
                    compute_cap = "10.0"

                gpus.append({
                    "index": idx,
                    "name": name,
                    "uuid": uuid,
                    "bus_id": bus_id,
                    "driver_version": driver,
                    "architecture": arch,
                    "compute_capability": compute_cap,
                    "vram_total_mb": mem_total,
                    "vram_free_mb": mem_free,
                    "vram_used_mb": mem_used,
                    "temperature_c": temp,
                    "power_draw_w": power_draw,
                    "power_limit_w": power_limit,
                    "utilization_gpu_pct": util_gpu,
                    "utilization_mem_pct": util_mem
                })
        return gpus
    except Exception:
        return []

def query_gpu_topology() -> str:
    """Query nvidia-smi topo -m."""
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return "No nvidia-smi available"
    try:
        proc = subprocess.run([nvidia_smi, "topo", "-m"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
        return proc.stdout.strip() if proc.returncode == 0 else "Topology matrix not available"
    except Exception as e:
        return f"Error querying topology: {str(e)}"

def query_cpu_info() -> Dict[str, Any]:
    """Inspect CPU information from /proc/cpuinfo."""
    cpu_info = {
        "model": platform.processor() or "Unknown",
        "cores_physical": os.cpu_count() or 1,
        "cores_logical": os.cpu_count() or 1,
        "flags": []
    }
    if os.path.exists("/proc/cpuinfo"):
        models = []
        flags = set()
        physical_ids = set()
        core_ids = set()
        with open("/proc/cpuinfo", "r", encoding="utf-8") as f:
            for line in f:
                if ":" in line:
                    k, v = [x.strip() for x in line.split(":", 1)]
                    if k == "model name":
                        models.append(v)
                    elif k == "flags":
                        flags.update(v.split())
                    elif k == "physical id":
                        physical_ids.add(v)
                    elif k == "core id":
                        core_ids.add(v)

        if models:
            cpu_info["model"] = models[0]
        cpu_info["sockets"] = max(1, len(physical_ids))
        cpu_info["cores_physical"] = max(1, len(core_ids) * max(1, len(physical_ids)))
        cpu_info["cores_logical"] = len(models)
        cpu_info["flags"] = sorted([f for f in flags if f in ["avx", "avx2", "avx512f", "fma", "sse4_2", "aes"]])
    return cpu_info

def query_ram_info() -> Dict[str, Any]:
    """Inspect system RAM and Swap from /proc/meminfo."""
    ram = {"total_mb": 0, "available_mb": 0, "swap_total_mb": 0, "swap_free_mb": 0}
    if os.path.exists("/proc/meminfo"):
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if ":" in line:
                    k, v = [x.strip() for x in line.split(":", 1)]
                    val_kb = int(v.split()[0]) if v.split() else 0
                    if k == "MemTotal":
                        ram["total_mb"] = val_kb // 1024
                    elif k == "MemAvailable":
                        ram["available_mb"] = val_kb // 1024
                    elif k == "SwapTotal":
                        ram["swap_total_mb"] = val_kb // 1024
                    elif k == "SwapFree":
                        ram["swap_free_mb"] = val_kb // 1024
    return ram

def query_cgroups_v2() -> Dict[str, Any]:
    """Verify cgroups v2 controller availability."""
    controllers_file = "/sys/fs/cgroup/cgroup.controllers"
    if os.path.exists(controllers_file):
        try:
            with open(controllers_file, "r", encoding="utf-8") as f:
                controllers = f.read().strip().split()
            return {
                "version": 2,
                "supported": True,
                "controllers": controllers,
                "has_memory": "memory" in controllers,
                "has_pids": "pids" in controllers,
                "has_cpu": "cpu" in controllers
            }
        except Exception as e:
            return {"version": 2, "supported": False, "error": str(e)}
    return {"version": 1, "supported": False, "controllers": []}

def query_sandbox_readiness() -> Dict[str, Any]:
    """Inspect Bubblewrap and Linux namespace isolation readiness."""
    bwrap_path = shutil.which("bwrap") or ("/usr/bin/bwrap" if os.path.exists("/usr/bin/bwrap") else None)
    works = False
    details = ""
    if bwrap_path:
        try:
            cmd = [
                bwrap_path,
                "--ro-bind", "/usr", "/usr",
                "--symlink", "usr/bin", "/bin",
                "--symlink", "usr/lib", "/lib",
                "--symlink", "usr/lib64", "/lib64",
                "--proc", "/proc",
                "--unshare-all",
                "--", "/usr/bin/echo", "OK"
            ]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=3)
            if res.returncode == 0 and "OK" in res.stdout:
                works = True
                details = "Unprivileged user namespaces active and functional."
            else:
                details = f"bwrap test failed with code {res.returncode}: {res.stderr}"
        except Exception as e:
            details = f"bwrap execution error: {str(e)}"
    else:
        details = "Bubblewrap executable not found on system."

    return {
        "installed": bool(bwrap_path),
        "path": bwrap_path,
        "functional": works,
        "details": details
    }

def query_storage_mounts() -> List[Dict[str, Any]]:
    """Inspect disk space and filesystem mounts."""
    mounts = []
    try:
        proc = subprocess.run(["df", "-hT", "/"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=3)
        if proc.returncode == 0:
            lines = proc.stdout.strip().splitlines()
            if len(lines) > 1:
                parts = lines[1].split()
                if len(parts) >= 7:
                    mounts.append({
                        "filesystem": parts[0],
                        "type": parts[1],
                        "total": parts[2],
                        "used": parts[3],
                        "available": parts[4],
                        "use_pct": parts[5],
                        "mountpoint": parts[6]
                    })
    except Exception:
        pass
    return mounts

def calculate_inference_recommendation(gpus: List[Dict[str, Any]], ram: Dict[str, Any]) -> Dict[str, Any]:
    """Calculates model topology recommendations based on surveyed hardware."""
    gpu_count = len(gpus)
    total_vram_mb = sum(g["vram_total_mb"] for g in gpus)
    total_vram_gb = total_vram_mb / 1024

    rec = {
        "status": "ready",
        "recommended_mode": "local_vllm",
        "fast_model": "Qwen/Qwen2.5-Coder-14B-Instruct",
        "heavy_model": "Qwen/Qwen2.5-Coder-32B-Instruct",
        "tp_fast": 1,
        "tp_heavy": 1,
        "allocated_gpu_ids": [],
        "max_context": 8192,
        "notes": []
    }

    if gpu_count >= 8:
        # Full production cluster (e.g. 8x RTX 8000 with 384 GB VRAM)
        rec["recommended_mode"] = "local_vllm_cluster"
        rec["tp_heavy"] = 4
        rec["tp_fast"] = 2
        rec["allocated_gpu_ids"] = list(range(gpu_count))
        rec["max_context"] = 32768
        rec["notes"].append("Sufficient VRAM for full FP16 fast (TP=2) and heavy (TP=4) with 2 GPUs reserved for KV benchmarks.")
    elif gpu_count >= 2:
        rec["recommended_mode"] = "local_vllm"
        rec["tp_heavy"] = min(gpu_count, 2)
        rec["tp_fast"] = 1
        rec["allocated_gpu_ids"] = list(range(gpu_count))
        rec["max_context"] = 16384
        rec["notes"].append(f"Detected {gpu_count} GPUs ({total_vram_gb:.1f} GB total). Recommended TP=2 for heavy-model.")
    elif gpu_count == 1:
        vram_gb = gpus[0]["vram_total_mb"] / 1024
        rec["allocated_gpu_ids"] = [0]
        if vram_gb >= 24:
            rec["recommended_mode"] = "local_vllm_single"
            rec["tp_heavy"] = 1
            rec["tp_fast"] = 1
            rec["max_context"] = 8192
            rec["notes"].append(f"Single GPU with {vram_gb:.1f} GB VRAM. Can run 14B model locally in FP16 or 32B in 4-bit quantization.")
        elif vram_gb >= 10:
            rec["recommended_mode"] = "hybrid_local_or_remote"
            rec["max_context"] = 4096
            rec["notes"].append(f"Single GPU with {vram_gb:.1f} GB VRAM. Ideal for 7B/14B Q4_K_M or proxying to remote vLLM cluster.")
        else:
            rec["recommended_mode"] = "remote_vllm"
            rec["notes"].append("VRAM is under 10 GB. Recommended to point to remote vLLM cluster for 14B/32B models.")
    else:
        rec["recommended_mode"] = "remote_vllm_or_emulated"
        rec["notes"].append("No dedicated NVIDIA GPU found. Recommended mode: Remote vLLM cluster or local emulated mode.")

    return rec

def run_hardware_survey() -> Dict[str, Any]:
    """Executes the complete hardware survey and returns structured dictionary."""
    gpus = query_nvidia_smi()
    topology = query_gpu_topology()
    cpu = query_cpu_info()
    ram = query_ram_info()
    cgroups = query_cgroups_v2()
    sandbox = query_sandbox_readiness()
    storage = query_storage_mounts()
    recommendation = calculate_inference_recommendation(gpus, ram)
    pci_accelerators = query_pci_accelerators()
    driver_stack = query_driver_stack()
    software_versions = query_software_versions()
    models_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../data/models"))
    model_storage = query_model_storage(models_dir)

    return {
        "survey_timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hostname": platform.node(),
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "distro": "Ubuntu" if "ubuntu" in platform.version().lower() or "ubuntu" in platform.release().lower() else "Linux"
        },
        "gpus": gpus,
        "gpu_topology": topology,
        "cpu": cpu,
        "ram": ram,
        "cgroups_v2": cgroups,
        "sandbox_confinement": sandbox,
        "storage": storage,
        "pci_accelerators": pci_accelerators,
        "driver_stack": driver_stack,
        "software_versions": software_versions,
        "model_storage": model_storage,
        "inference_recommendation": recommendation
    }

def export_survey_reports(survey: Dict[str, Any], output_dir: str):
    """Exports survey to hardware_inventory.json and hardware_inventory.md."""
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, "hardware_inventory.json")
    md_path = os.path.join(output_dir, "hardware_inventory.md")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(survey, f, indent=2)

    # Generate Markdown Report
    gpu_lines = []
    if survey["gpus"]:
        gpu_lines.append("| Index | Name | Architecture | Compute Cap | VRAM Total | Free | Temp | Driver |")
        gpu_lines.append("|---|---|---|---|---|---|---|---|")
        for g in survey["gpus"]:
            gpu_lines.append(f"| {g['index']} | {g['name']} | {g['architecture']} | {g['compute_capability']} | {g['vram_total_mb']} MB | {g['vram_free_mb']} MB | {g['temperature_c']}°C | {g['driver_version']} |")
    else:
        gpu_lines.append("*No NVIDIA GPUs detected via nvidia-smi.*")

    mount_lines = []
    for m in survey["storage"]:
        mount_lines.append(f"| `{m['mountpoint']}` | {m['type']} | {m['total']} | {m['used']} | {m['available']} ({m['use_pct']}) |")

    pci_lines = [f"| `{device.get('slot')}` | {device.get('description')} | {device.get('vendor_device_ids')} | {device.get('pci_class')} | {device.get('kernel_driver')} | {', '.join(device.get('kernel_modules', []))} |" for device in survey.get("pci_accelerators", [])]
    if not pci_lines:
        pci_lines = ["*No PCI accelerators detected.*"]
    driver_stack = survey.get("driver_stack", {})
    software_versions = survey.get("software_versions", {})
    model_storage = survey.get("model_storage", {})
    model_lines = [f"| `{entry.get('name')}` | {entry.get('size_bytes')} |" for entry in model_storage.get("entries", [])]
    if not model_lines:
        model_lines = ["*No model directories found.*"]

    md_content = f"""# Sysadmin AI Platform — Hardware & Device Survey Report
**Timestamp:** {survey['survey_timestamp']}  
**Host:** `{survey['hostname']}` ({survey['os']['distro']} {survey['os']['release']} {survey['os']['machine']})  

---

## 1. GPU & Accelerator Inventory
{chr(10).join(gpu_lines)}

### Recommended Topology & Concurrency:
* **Recommended Mode:** `{survey['inference_recommendation']['recommended_mode']}`
* **Fast Model:** `{survey['inference_recommendation']['fast_model']}` (TP={survey['inference_recommendation']['tp_fast']})
* **Heavy Model:** `{survey['inference_recommendation']['heavy_model']}` (TP={survey['inference_recommendation']['tp_heavy']})
* **Maximum Context Window:** `{survey['inference_recommendation']['max_context']} tokens`
* **Notes:** {", ".join(survey['inference_recommendation']['notes'])}

---

## 2. CPU & Host Memory
* **CPU Model:** {survey['cpu']['model']}
* **Logical Cores:** {survey['cpu']['cores_logical']} (Physical: {survey['cpu']['cores_physical']})
* **Vector Extensions:** `{", ".join(survey['cpu']['flags']) or "None detected"}`
* **Host RAM:** {survey['ram']['total_mb']} MB Total ({survey['ram']['available_mb']} MB Available)
* **Swap:** {survey['ram']['swap_total_mb']} MB Total

---

## 3. Sandboxing & Isolation Readiness
* **Bubblewrap Binary:** `{survey['sandbox_confinement']['path'] or "Missing"}`
* **Namespace Isolation Status:** {"Functional" if survey['sandbox_confinement']['functional'] else "Inactive"}
* **cgroups v2 Controllers:** `{", ".join(survey['cgroups_v2'].get('controllers', []))}`
  * Memory Controller: {"Active" if survey['cgroups_v2'].get('has_memory') else "Missing"}
  * PIDs Controller (Anti Fork-Bomb): {"Active" if survey['cgroups_v2'].get('has_pids') else "Missing"}
  * CPU Quota Controller: {"Active" if survey['cgroups_v2'].get('has_cpu') else "Missing"}

---

## 4. Storage & Partitions
| Mountpoint | Filesystem | Total Size | Used | Available |
|---|---|---|---|---|
{chr(10).join(mount_lines)}

---

## 5. PCI Accelerators & Devices
| Slot | Description | Vendor:Device IDs | PCI Class | Kernel Driver | Kernel Modules |
|---|---|---|---|---|---|
{chr(10).join(pci_lines)}

---

## 6. Driver & Toolkit Stack
```json
{json.dumps(driver_stack, indent=2)}
```

---

## 7. Software Versions
```json
{json.dumps(software_versions, indent=2)}
```

---

## 8. Model Storage
* **Path:** `{model_storage.get('path')}`
* **Exists:** {model_storage.get('exists')}
* **Total bytes:** {model_storage.get('total_bytes')}
* **Free bytes:** {model_storage.get('free_bytes')}

| Model directory | Size (bytes) |
|---|---:|
{chr(10).join(model_lines)}

---
*Report automatically generated by `hardware_survey.py`.*
"""
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    return json_path, md_path

if __name__ == "__main__":
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel

    console = Console()
    survey = run_hardware_survey()

    console.print("\n[bold cyan]╔══════════════════════════════════════════════════════════════════════╗[/bold cyan]")
    console.print("[bold cyan]║          Sysadmin AI Platform — Automated Device Survey              ║[/bold cyan]")
    console.print("[bold cyan]╚══════════════════════════════════════════════════════════════════════╝[/bold cyan]\n")

    # GPU Table
    if survey["gpus"]:
        table = Table(title="[bold green]Detected NVIDIA Accelerators[/bold green]", show_header=True, header_style="bold magenta")
        table.add_column("Index", style="dim", width=6)
        table.add_column("Device Name", style="bold")
        table.add_column("Arch / CC")
        table.add_column("VRAM Total")
        table.add_column("VRAM Free")
        table.add_column("Temp")
        table.add_column("Driver")

        for g in survey["gpus"]:
            table.add_row(
                str(g["index"]),
                g["name"],
                f"{g['architecture']} (v{g['compute_capability']})",
                f"{g['vram_total_mb']} MB",
                f"[green]{g['vram_free_mb']} MB[/green]",
                f"{g['temperature_c']}°C",
                g["driver_version"]
            )
        console.print(table)
    else:
        console.print("[yellow][!] No NVIDIA GPUs detected via nvidia-smi.[/yellow]")

    # System Panel
    cpu = survey["cpu"]
    ram = survey["ram"]
    sb = survey["sandbox_confinement"]
    cg = survey["cgroups_v2"]

    sys_text = (
        f"[bold]Host:[/bold] {survey['hostname']} ({survey['os']['distro']} {survey['os']['release']})\n"
        f"[bold]CPU:[/bold] {cpu['model']} ({cpu['cores_logical']} cores) | Vector flags: {', '.join(cpu['flags'])}\n"
        f"[bold]Memory:[/bold] {ram['total_mb']} MB Total ({ram['available_mb']} MB Available)\n"
        f"[bold]Sandbox:[/bold] Bubblewrap ({'Ready' if sb['functional'] else 'Not Functional'}) | "
        f"cgroups v2 ({'Active: ' + ', '.join(cg.get('controllers', [])) if cg.get('supported') else 'Inactive'})"
    )
    console.print(Panel(sys_text, title="[bold blue]Host & Security Environment[/bold blue]", expand=False))

    rec = survey["inference_recommendation"]
    rec_text = (
        f"[bold]Recommended Mode:[/bold] [green]{rec['recommended_mode']}[/green]\n"
        f"[bold]Fast Model:[/bold] {rec['fast_model']} (TP={rec['tp_fast']})\n"
        f"[bold]Heavy Model:[/bold] {rec['heavy_model']} (TP={rec['tp_heavy']})\n"
        f"[bold]Max Context Window:[/bold] {rec['max_context']} tokens\n"
        f"[bold]Notes:[/bold] {' '.join(rec['notes'])}"
    )
    console.print(Panel(rec_text, title="[bold green]Inference Engine Sizing Recommendation[/bold green]", expand=False))

    out_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../data"))
    j_p, m_p = export_survey_reports(survey, out_dir)
    console.print(f"\n[green]✔ Reports generated:[/green] [dim]{j_p}[/dim] & [dim]{m_p}[/dim]\n")
