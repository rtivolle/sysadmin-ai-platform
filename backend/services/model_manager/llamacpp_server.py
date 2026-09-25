"""Supervise one local llama.cpp OpenAI-compatible server per GGUF model."""
import os
import shutil
import stat
import subprocess
import time
from typing import Any, Callable, Dict, Optional

from . import registry as registry_module
from . import vllm_server as process_support


def llamacpp_bin() -> Optional[str]:
    """Resolve the operator-provisioned llama-server binary."""
    configured = os.getenv("LLAMACPP_BIN")
    return configured or shutil.which("llama-server")


def _managed_model_path(name: str, filename: str, store: registry_module.ModelRegistry) -> str:
    filename = registry_module.validate_gguf_file(filename)
    managed_root = os.path.realpath(store.models_dir)
    model_root = os.path.realpath(store.path_for(name))
    if os.path.commonpath((model_root, managed_root)) != managed_root:
        raise ValueError("model directory escapes the managed model store")
    return os.path.join(model_root, filename)


def model_file_path(name: str, store: registry_module.ModelRegistry) -> str:
    """Resolve the regular GGUF from the server-assigned model directory."""
    entry = store.get(name)
    if entry is None:
        raise KeyError(f"unknown model '{name}'")
    path = _managed_model_path(name, entry.get("gguf_file"), store)
    model_root = os.path.realpath(store.path_for(name))
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise FileNotFoundError(f"GGUF model file not found: {filename}") from exc
    if not stat.S_ISREG(metadata.st_mode) or os.path.realpath(path) != path:
        raise ValueError("GGUF model must be a regular file inside its managed directory")
    if os.path.commonpath((os.path.realpath(path), model_root)) != model_root:
        raise ValueError("GGUF model file escapes its managed directory")
    return path


def build_command(entry: Dict[str, Any], port: int, path: str) -> list:
    binary = llamacpp_bin()
    if not binary:
        raise FileNotFoundError("llama-server executable not found (set LLAMACPP_BIN or add it to PATH)")
    # A field explicitly cleared by an admin (JSON null) falls back to its default.
    raw_ctx = entry.get("ctx_size")
    ctx_size = int(raw_ctx) if raw_ctx is not None else 2048
    if not 512 <= ctx_size <= 131072:
        raise ValueError("ctx_size must be between 512 and 131072")
    raw_layers = entry.get("n_gpu_layers")
    n_gpu_layers = "all" if raw_layers is None else raw_layers
    if n_gpu_layers != "all" and (type(n_gpu_layers) is not int or n_gpu_layers < 0):
        raise ValueError("n_gpu_layers must be 'all' or a non-negative integer")
    raw_flash = entry.get("flash_attn")
    flash_attn = True if raw_flash is None else raw_flash
    if type(flash_attn) is not bool:
        raise ValueError("flash_attn must be a boolean")
    threads = entry.get("threads")
    if threads is not None:
        threads = int(threads)
        if threads < 1:
            raise ValueError("threads must be positive")
    batch_size = entry.get("batch_size")
    if batch_size is not None:
        batch_size = int(batch_size)
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
    command = [
        binary, "--model", path, "--alias", entry["name"],
        "--host", "127.0.0.1", "--port", str(port),
        "--ctx-size", str(ctx_size), "--parallel", "1",
        "--n-gpu-layers", str(n_gpu_layers),
        "--flash-attn", "on" if flash_attn else "off",
        "--reasoning", "off",
    ]
    if threads is not None:
        command += ["--threads", str(threads)]
    if batch_size is not None:
        command += ["--batch-size", str(batch_size)]
    if entry.get("mmap") is False:
        command += ["--no-mmap"]
    if entry.get("mlock") is True:
        command += ["--mlock"]
    return command


def _child_environment() -> Dict[str, str]:
    env = dict(os.environ)
    extra_libraries = [item for item in os.getenv("LLAMACPP_LD_LIBRARY_PATH", "").split(os.pathsep) if item]
    if extra_libraries:
        existing = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = os.pathsep.join(extra_libraries + ([existing] if existing else []))
    return env


def _default_popen(command: list, log_file: Any) -> Any:
    return subprocess.Popen(command, stdout=log_file, stderr=subprocess.STDOUT, env=_child_environment())


def _default_health(port: int) -> bool:
    return process_support._default_health(port)


def _process_matches(entry: Dict[str, Any], store: registry_module.ModelRegistry) -> bool:
    server = entry.get("server") or {}
    pid = server.get("pid")
    if not process_support.process_alive(pid):
        return False
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as cmdline_file:
            args = [value.decode("utf-8", errors="replace") for value in cmdline_file.read().split(b"\0") if value]
        expected_model = _managed_model_path(entry["name"], entry.get("gguf_file"), store)
        expected_port = str(int(server.get("port") or 0))
    except FileNotFoundError:
        return False
    except (OSError, ValueError, KeyError, TypeError):
        # If process identity cannot be read, callers must fail closed without
        # signalling or clearing a potentially live model record.
        raise
    if not args:
        return False
    return (
        os.path.basename(args[0]) == "llama-server"
        and _has_argument(args, "--model", expected_model)
        and _has_argument(args, "--alias", entry["name"])
        and _has_argument(args, "--port", expected_port)
    )


def _has_argument(args: list, option: str, expected: str) -> bool:
    try:
        return args[args.index(option) + 1] == expected
    except (ValueError, IndexError):
        return False


def is_running(entry: Dict[str, Any], store: Optional[registry_module.ModelRegistry] = None) -> bool:
    """Cheap process-identity check; never send a signal based on HTTP health."""
    return ((entry.get("server") or {}).get("status") == "running"
            and process_matches(entry, store or registry_module.model_registry))


def process_matches(entry: Dict[str, Any], store: registry_module.ModelRegistry) -> bool:
    """Whether the recorded PID still belongs to this model server."""
    return _process_matches(entry, store)


def _terminate_owned_process(proc: Any, grace: float) -> None:
    """Stop a child through its Popen handle and confirm exit before untracking it."""
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=grace)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
    if proc.poll() is None:
        raise RuntimeError("llama.cpp child has not exited; model state and port remain reserved")


def start(
    name: str,
    store: registry_module.ModelRegistry,
    popen_fn: Optional[Callable[..., Any]] = None,
    health_fn: Optional[Callable[[int], bool]] = None,
    ready_timeout: float = process_support.DEFAULT_READY_TIMEOUT_S,
) -> Dict[str, Any]:
    entry = store.get(name)
    if entry is None:
        raise KeyError(f"unknown model '{name}'")
    if entry.get("engine", registry_module.ENGINE_VLLM) != registry_module.ENGINE_LLAMACPP:
        raise ValueError(f"model '{name}' is not configured for llama.cpp")
    if is_running(entry, store):
        return entry
    recorded_pid = (entry.get("server") or {}).get("pid")
    if recorded_pid and process_support.process_alive(recorded_pid):
        # A recorded process that is still ours must be reaped before another
        # model server can compete for its port or GPU memory. A recycled PID
        # is not ours and must never be signalled.
        stop(name, store)
        entry = store.get(name) or entry
    if entry.get("status") not in (registry_module.STATUS_DOWNLOADED, registry_module.STATUS_STOPPED,
                                    registry_module.STATUS_ERROR):
        raise RuntimeError(f"model '{name}' is not downloaded (status={entry.get('status')})")

    path = model_file_path(name, store)
    port = process_support.allocate_port(store)
    command = build_command(entry, port, path)
    store.update(name, status=registry_module.STATUS_STARTING, last_error=None)
    log_name = registry_module.validate_name(name)
    os.makedirs(os.path.dirname(process_support.log_path(log_name)), exist_ok=True)
    log_file = open(process_support.log_path(log_name), "ab", buffering=0)
    try:
        proc = (popen_fn or _default_popen)(command, log_file)
    except Exception as exc:
        log_file.close()
        store.update(name, status=registry_module.STATUS_ERROR, last_error=str(exc))
        raise
    finally:
        # The child inherits its own descriptor; the supervisor does not need one.
        if not log_file.closed:
            log_file.close()
    process_support._PROCS[name] = proc
    healthy = process_support.wait_ready(port, proc, health_fn or _default_health, ready_timeout)
    if not healthy:
        try:
            _terminate_owned_process(proc, process_support.DEFAULT_STOP_GRACE_S)
        except Exception as terminate_error:
            store.update(
                name,
                status=registry_module.STATUS_ERROR,
                last_error=f"llama.cpp readiness failed and shutdown was not confirmed: {terminate_error}",
                server={"pid": getattr(proc, "pid", None), "port": port, "status": "error"},
            )
            raise RuntimeError(f"llama.cpp for '{name}' failed readiness and could not be reaped") from terminate_error
        process_support._PROCS.pop(name, None)
        store.update(
            name,
            status=registry_module.STATUS_ERROR,
            last_error=f"llama.cpp did not become healthy within {ready_timeout:.0f}s",
            server={"pid": None, "port": port, "status": "error"},
        )
        raise RuntimeError(f"llama.cpp for '{name}' did not become healthy")
    return store.update(
        name,
        status=registry_module.STATUS_RUNNING,
        last_error=None,
        server={"pid": getattr(proc, "pid", None), "port": port, "status": "running",
                "started_at": time.time()},
    )


def stop(name: str, store: registry_module.ModelRegistry,
         grace: float = process_support.DEFAULT_STOP_GRACE_S) -> Dict[str, Any]:
    entry = store.get(name)
    if entry is None:
        raise KeyError(f"unknown model '{name}'")
    if entry.get("engine", registry_module.ENGINE_VLLM) != registry_module.ENGINE_LLAMACPP:
        raise ValueError(f"model '{name}' is not configured for llama.cpp")
    proc = process_support._PROCS.get(name)
    if proc is not None:
        _terminate_owned_process(proc, grace)
    else:
        server = entry.get("server") or {}
        pid = server.get("pid")
        if pid and process_support.process_alive(pid) and _process_matches(entry, store):
            try:
                os.kill(int(pid), 15)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline and _process_matches(entry, store):
                time.sleep(0.1)
            if _process_matches(entry, store):
                # Recheck identity immediately before escalating; never kill a
                # process that reused the recorded PID after the original exited.
                try:
                    os.kill(int(pid), 9)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and _process_matches(entry, store):
                    time.sleep(0.1)
            if _process_matches(entry, store):
                raise RuntimeError(f"llama.cpp for '{name}' could not be reaped; model state and port remain reserved")
    process_support._PROCS.pop(name, None)
    return store.update(name, status=registry_module.STATUS_STOPPED,
                        server={"pid": None, "port": None, "status": "stopped"})


def tail_log(name: str, tail_bytes: int = 32768) -> str:
    return process_support.tail_log(name, tail_bytes)
