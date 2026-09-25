"""Model registry: a validated, atomically written JSON index of local models.

The registry is the single source of truth for which HuggingFace models the
platform knows about, where their files live, and whether a vLLM server is
running for them. It is a local file (no shared store): every write is atomic
(temp file + fsync + rename, mode 0600) so a crash cannot leave a half-written
index, and a corrupt index is quarantined rather than trusted.
"""
import json
import os
import re
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional

# A served name becomes a directory name and a LiteLLM model_name, so it must be
# a single path-safe token. No slashes, no traversal, bounded length.
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# HuggingFace repo ids look like `org/name`; both sides are path-safe tokens.
HF_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
GGUF_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.gguf$", re.IGNORECASE)

ENGINE_VLLM = "vllm"
ENGINE_LLAMACPP = "llamacpp"
ENGINES = frozenset({ENGINE_VLLM, ENGINE_LLAMACPP})

# Registry status state machine (coarse). `error` carries `last_error`.
STATUS_REGISTERED = "registered"
STATUS_DOWNLOADING = "downloading"
STATUS_DOWNLOADED = "downloaded"
STATUS_STARTING = "starting"
STATUS_RUNNING = "running"
STATUS_STOPPED = "stopped"
STATUS_ERROR = "error"

ALLOWED_FIELDS = {
    "name", "hf_repo", "revision", "path", "status", "size_bytes", "created_at",
    "updated_at", "last_error", "quantization", "max_model_len",
    "tensor_parallel_size", "gpu_memory_utilization", "server", "engine",
    "gguf_file", "ctx_size", "n_gpu_layers", "flash_attn",
    "dtype", "kv_cache_dtype", "enforce_eager", "max_num_seqs",
    "enable_prefix_caching", "threads", "batch_size", "mmap", "mlock",
}

# Bounded admin-selectable choices. The installed engine binary remains the
# final authority on what it accepts; these sets keep the registry predictable
# and prevent arbitrary CLI-argument injection through the API.
VLLM_DTYPES = frozenset({"auto", "half", "float16", "bfloat16", "float", "float32"})
VLLM_KV_CACHE_DTYPES = frozenset({"auto", "fp8", "fp8_e5m2", "fp8_e4m3", "fp8_inc", "fp8_ds"})


def default_models_dir() -> str:
    return os.getenv(
        "MODELS_DIR",
        os.path.abspath(os.path.join(os.path.dirname(__file__), "../../data/models")),
    )


def default_registry_path(models_dir: Optional[str] = None) -> str:
    return os.getenv("MODELS_REGISTRY", os.path.join(models_dir or default_models_dir(), "registry.json"))


def validate_name(name: Any) -> str:
    if not isinstance(name, str) or not MODEL_ID_RE.match(name):
        raise ValueError("name must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    return name


def validate_hf_repo(repo: Any) -> str:
    if not isinstance(repo, str) or not HF_REPO_RE.match(repo):
        raise ValueError("hf_repo must look like 'org/model'")
    return repo


def validate_revision(revision: Any) -> Optional[str]:
    if revision is None or revision == "":
        return None
    if not isinstance(revision, str) or not REVISION_RE.match(revision):
        raise ValueError("revision must be a single path-safe token")
    return revision


def validate_engine(engine: Any) -> str:
    if engine is None:
        return ENGINE_VLLM
    if engine not in ENGINES:
        raise ValueError(f"engine must be one of {sorted(ENGINES)}")
    return engine


def validate_gguf_file(filename: Any) -> str:
    if (not isinstance(filename, str) or os.path.basename(filename) != filename
            or not GGUF_FILE_RE.fullmatch(filename)):
        raise ValueError("gguf_file must be a GGUF filename without a directory path")
    return filename


def validate_dtype(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or value not in VLLM_DTYPES:
        raise ValueError(f"dtype must be one of {sorted(VLLM_DTYPES)}")
    return value


def validate_kv_cache_dtype(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or value not in VLLM_KV_CACHE_DTYPES:
        raise ValueError(f"kv_cache_dtype must be one of {sorted(VLLM_KV_CACHE_DTYPES)}")
    return value


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".registry-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


class ModelRegistry:
    """Thread-safe accessor for the JSON registry."""

    def __init__(self, path: Optional[str] = None, models_dir: Optional[str] = None):
        self.models_dir = models_dir or default_models_dir()
        self.path = path or default_registry_path(self.models_dir)
        self._lock = threading.RLock()

    # --- persistence ---
    def _read(self) -> Dict[str, Dict[str, Any]]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            # Quarantine a corrupt index so it cannot be silently trusted.
            corrupted = f"{self.path}.corrupt-{int(time.time())}.bak"
            try:
                os.replace(self.path, corrupted)
            except OSError:
                pass
            return {}
        if not isinstance(data, dict):
            return {}
        return {key: value for key, value in data.items() if isinstance(value, dict)}

    def _write(self, models: Dict[str, Dict[str, Any]]) -> None:
        _atomic_write_json(self.path, models)

    # --- queries ---
    def all(self) -> List[Dict[str, Any]]:
        with self._lock:
            models = self._read()
        return [models[name] for name in sorted(models)]

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._read().get(name)

    def path_for(self, name: str) -> str:
        """Filesystem path for a model, guaranteed inside the models directory."""
        safe = validate_name(name)
        target = os.path.abspath(os.path.join(self.models_dir, safe))
        if os.path.commonpath([target, os.path.abspath(self.models_dir)]) != os.path.abspath(self.models_dir):
            raise ValueError("model path escapes the models directory")
        return target

    # --- mutations ---
    def upsert(self, name: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        name = validate_name(name)
        with self._lock:
            models = self._read()
            existing = models.get(name, {})
            entry = {key: value for key, value in fields.items() if key in ALLOWED_FIELDS}
            merged = {**existing, **entry, "name": name, "updated_at": _now()}
            merged.setdefault("created_at", _now())
            models[name] = merged
            self._write(models)
            return merged

    def update(self, name: str, **fields: Any) -> Optional[Dict[str, Any]]:
        name = validate_name(name)
        with self._lock:
            models = self._read()
            if name not in models:
                return None
            entry = {key: value for key, value in fields.items() if key in ALLOWED_FIELDS}
            models[name].update(entry)
            models[name]["updated_at"] = _now()
            self._write(models)
            return models[name]

    def delete(self, name: str) -> bool:
        name = validate_name(name)
        with self._lock:
            models = self._read()
            if name not in models:
                return False
            del models[name]
            self._write(models)
            return True


# Router/import-time singleton; tests override attributes or use ModelRegistry(tmp).
model_registry = ModelRegistry()
