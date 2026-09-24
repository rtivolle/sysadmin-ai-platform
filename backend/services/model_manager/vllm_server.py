"""Supervise one local vLLM OpenAI-compatible server per downloaded model.

Uses the current `vllm serve <path>` entry point. Readiness is `GET /health`
returning 200 *and* the child still being alive; shutdown sends SIGTERM, waits a
grace period, and only then escalates to SIGKILL. Process creation and the
health probe are injectable so the lifecycle is testable without a GPU.
"""
import os
import shutil
import signal
import socket
import subprocess
import time
from typing import Any, Callable, Dict, Optional

from . import registry as registry_module

DEFAULT_PORT_START = 8100
DEFAULT_PORT_END = 8199
DEFAULT_READY_TIMEOUT_S = 300.0
DEFAULT_STOP_GRACE_S = 15.0

_PROCS: Dict[str, Any] = {}


def vllm_bin() -> Optional[str]:
    return os.getenv("VLLM_BIN") or shutil.which("vllm")


def _port_range() -> tuple:
    start = int(os.getenv("MODEL_PORT_START", DEFAULT_PORT_START))
    end = int(os.getenv("MODEL_PORT_END", DEFAULT_PORT_END))
    if start < 1 or end < start or end > 65535:
        raise ValueError("invalid MODEL_PORT_START/END range")
    return start, end


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def allocate_port(store: registry_module.ModelRegistry) -> int:
    reserved = {
        (entry.get("server") or {}).get("port")
        for entry in store.all()
        if (entry.get("server") or {}).get("port")
    }
    start, end = _port_range()
    for port in range(start, end + 1):
        if port not in reserved and _port_free(port):
            return port
    raise RuntimeError(f"no free model port in {start}-{end}")


def log_path(name: str) -> str:
    base = os.getenv(
        "MODEL_LOG_DIR",
        os.path.abspath(os.path.join(os.path.dirname(__file__), "../../logs/models")),
    )
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"{registry_module.validate_name(name)}.log")


def build_command(entry: Dict[str, Any], port: int) -> list:
    binary = vllm_bin()
    if not binary:
        raise FileNotFoundError("vllm executable not found (set VLLM_BIN or install vllm)")
    command = [
        binary, "serve", entry["path"],
        "--host", "127.0.0.1",
        "--port", str(port),
        "--served-model-name", entry["name"],
    ]
    if entry.get("quantization"):
        command += ["--quantization", str(entry["quantization"])]
    if entry.get("max_model_len"):
        command += ["--max-model-len", str(int(entry["max_model_len"]))]
    if entry.get("tensor_parallel_size"):
        command += ["--tensor-parallel-size", str(int(entry["tensor_parallel_size"]))]
    if entry.get("gpu_memory_utilization"):
        command += ["--gpu-memory-utilization", str(entry["gpu_memory_utilization"])]
    return command


def _default_popen(command: list, log_file: Any) -> Any:
    env = dict(os.environ)
    # vLLM reads this instead of a CLI flag in several versions.
    env.setdefault("VLLM_ALLOW_RUNTIME_LORA_UPDATING", "0")
    return subprocess.Popen(command, stdout=log_file, stderr=subprocess.STDOUT, env=env)


def _default_health(port: int) -> bool:
    import httpx
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2.0)
        return response.status_code == 200
    except httpx.HTTPError:
        return False


def process_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def is_running(entry: Dict[str, Any]) -> bool:
    server = entry.get("server") or {}
    return server.get("status") == "running" and process_alive(server.get("pid"))


def wait_ready(port: int, proc: Any, health_fn: Callable[[int], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        if health_fn(port):
            return True
        time.sleep(0.5)
    return False


def start(
    name: str,
    store: registry_module.ModelRegistry,
    popen_fn: Optional[Callable[..., Any]] = None,
    health_fn: Optional[Callable[[int], bool]] = None,
    ready_timeout: float = DEFAULT_READY_TIMEOUT_S,
) -> Dict[str, Any]:
    entry = store.get(name)
    if entry is None:
        raise KeyError(f"unknown model '{name}'")
    if is_running(entry):
        return entry
    if entry.get("status") not in (registry_module.STATUS_DOWNLOADED, registry_module.STATUS_STOPPED,
                                   registry_module.STATUS_ERROR):
        raise RuntimeError(f"model '{name}' is not downloaded (status={entry.get('status')})")
    path = entry.get("path") or store.path_for(name)
    if not os.path.isdir(path):
        raise FileNotFoundError(f"model files not found at {path}")

    port = allocate_port(store)
    command = build_command(entry, port)
    store.update(name, status=registry_module.STATUS_STARTING, last_error=None, path=path)
    os.makedirs(os.path.dirname(log_path(name)), exist_ok=True)
    log_file = open(log_path(name), "ab", buffering=0)
    try:
        proc = (popen_fn or _default_popen)(command, log_file)
    except Exception as exc:
        log_file.close()
        store.update(name, status=registry_module.STATUS_ERROR, last_error=str(exc))
        raise
    _PROCS[name] = proc
    healthy = wait_ready(port, proc, health_fn or _default_health, ready_timeout)
    if not healthy:
        _terminate(proc)
        _PROCS.pop(name, None)
        store.update(
            name, status=registry_module.STATUS_ERROR,
            last_error=f"vLLM did not become healthy within {ready_timeout:.0f}s",
            server={"pid": None, "port": port, "status": "error"},
        )
        raise RuntimeError(f"vLLM for '{name}' did not become healthy")
    return store.update(
        name, status=registry_module.STATUS_RUNNING,
        server={"pid": getattr(proc, "pid", None), "port": port, "status": "running",
                "started_at": time.time()},
    )


def _terminate(proc: Any, grace: float = DEFAULT_STOP_GRACE_S) -> None:
    pid = getattr(proc, "pid", None)
    try:
        if pid:
            os.kill(int(pid), signal.SIGTERM)
        else:
            proc.terminate()
    except (OSError, ValueError):
        pass
    try:
        proc.wait(timeout=grace)
        return
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def stop(name: str, store: registry_module.ModelRegistry, grace: float = DEFAULT_STOP_GRACE_S) -> Dict[str, Any]:
    entry = store.get(name)
    if entry is None:
        raise KeyError(f"unknown model '{name}'")
    proc = _PROCS.pop(name, None)
    if proc is not None:
        _terminate(proc, grace)
    else:
        server = entry.get("server") or {}
        if server.get("pid"):
            try:
                os.kill(int(server["pid"]), signal.SIGTERM)
            except (OSError, ValueError):
                pass
    return store.update(name, status=registry_module.STATUS_STOPPED,
                        server={"pid": None, "port": None, "status": "stopped"})


def tail_log(name: str, tail_bytes: int = 32768) -> str:
    try:
        with open(log_path(name), "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - tail_bytes))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
