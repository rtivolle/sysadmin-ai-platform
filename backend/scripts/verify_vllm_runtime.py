#!/usr/bin/env python3
"""Import the serving stack and exercise every visible GPU; no model download."""
import argparse
import importlib
import os
from pathlib import Path
import shutil


def check_jit_tools():
    cuda = Path(os.environ.get("CUDA_HOME", "/usr/local/cuda"))
    missing = []
    if not shutil.which("ninja"):
        missing.append("ninja (Ubuntu package ninja-build)")
    if not (cuda / "bin/nvcc").is_file():
        missing.append(f"{cuda}/bin/nvcc")
    for name in ("cuda_runtime.h", "curand.h"):
        if not (cuda / "include" / name).is_file():
            missing.append(f"{cuda}/include/{name}")
    if missing:
        raise RuntimeError("Missing JIT prerequisites: " + ", ".join(missing))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jit", action="store_true", help="Check CUDA compiler, cuRAND headers and Ninja")
    parser.add_argument("--architecture", help="Require a registered vLLM model architecture")
    args = parser.parse_args()
    # A torch allocation alone misses torchvision/torchaudio and vLLM ABI errors.
    for name in ("torch", "torchvision", "torchaudio", "vllm"):
        module = importlib.import_module(name)
        print(name, getattr(module, "__version__", "unknown"))
    if args.architecture:
        from vllm.model_executor.models.registry import ModelRegistry
        if args.architecture not in ModelRegistry.get_supported_archs():
            raise RuntimeError(f"Unsupported architecture: {args.architecture}")
    import torch
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA unavailable")
    for index in range(torch.cuda.device_count()):
        value = torch.ones(4, device=f"cuda:{index}").cpu().sum().item()
        if value != 4:
            raise RuntimeError(f"GPU {index} allocation/copy check failed: expected 4, got {value}")
        print(f"GPU {index}: {torch.cuda.get_device_name(index)} PASS")
    if args.jit:
        check_jit_tools()
    print("Runtime preflight PASS; model loading and inference still require live validation")


if __name__ == "__main__":
    main()
