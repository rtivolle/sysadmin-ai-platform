"""Local GPU inventory: the payload the node agent registers with.

Everything here is injectable (`smi_fn`) so the node agent is testable on a
host with no GPU and no `nvidia-smi` binary.
"""
import os
import re
import shutil
import subprocess
from typing import Any, Callable, Dict, List, Optional

# `nvidia-smi --query-gpu=name,memory.total,compute_cap --format=csv,noheader`
_GPU_LINE_RE = re.compile(r"^(?P<name>.+),\s*(?P<vram>[0-9]+)\s*MiB,\s*(?P<cap>[0-9]+\.[0-9]+)\s*$")


def _default_smi_fn() -> str:
    binary = shutil.which("nvidia-smi")
    if not binary:
        raise FileNotFoundError("nvidia-smi not found")
    proc = subprocess.run(
        [binary, "--query-gpu=name,memory.total,compute_cap",
         "--format=csv,noheader"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"nvidia-smi failed: {proc.stderr.strip()[:200]}")
    return proc.stdout


def gpu_inventory(smi_fn: Optional[Callable[[], str]] = None) -> Dict[str, Any]:
    """Return the GPU inventory, or a GPU-less record when unavailable."""
    try:
        output = (smi_fn or _default_smi_fn)()
    except Exception:
        return {"gpu_model": None, "gpu_count": 0, "vram_total_gb": 0.0,
                "compute_capability": None, "available": False}
    models: List[str] = []
    total_mib = 0
    caps: List[str] = []
    for line in output.splitlines():
        match = _GPU_LINE_RE.match(line.strip())
        if not match:
            continue
        models.append(match.group("name").strip())
        total_mib += int(match.group("vram"))
        caps.append(match.group("cap"))
    if not models:
        return {"gpu_model": None, "gpu_count": 0, "vram_total_gb": 0.0,
                "compute_capability": None, "available": False}
    # Homogeneous nodes are the expected case; record the first GPU as the
    # node's class and note heterogeneity in the model string.
    model = models[0] if len(set(models)) == 1 else f"heterogeneous({len(set(models))} types)"
    return {
        "gpu_model": model,
        "gpu_count": len(models),
        "vram_total_gb": round(total_mib / 1024.0, 2),
        "compute_capability": max(caps) if caps else None,
        "available": True,
    }


def disk_free_bytes(path: str) -> Optional[int]:
    try:
        stat = os.statvfs(path)
        return stat.f_bavail * stat.f_frsize
    except OSError:
        return None
