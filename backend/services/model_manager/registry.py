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
    # Versioned rollout (chantier 3): per-model version records and the
    # promotion history. Models registered before versions existed simply
    # have no `versions` key, which every version API treats as empty.
    "versions", "promotion_history",
}

# Version lifecycle stages. The rollout chain is staging -> canary -> prod,
# and any stage may move to archived. Two deliberate extensions beyond the
# strict chain, both explicit operator actions:
#   * demote (prod/canary -> staging): emergency pull-back;
#   * re-qualify (archived -> staging/canary/prod): re-test an archived
#     build, used by rollback which re-promotes the previous prod version.
STAGE_STAGING = "staging"
STAGE_CANARY = "canary"
STAGE_PROD = "prod"
STAGE_ARCHIVED = "archived"
VERSION_STAGES = frozenset({STAGE_STAGING, STAGE_CANARY, STAGE_PROD, STAGE_ARCHIVED})

_STAGE_TRANSITIONS = {
    STAGE_STAGING: {STAGE_CANARY, STAGE_ARCHIVED},
    STAGE_CANARY: {STAGE_PROD, STAGE_STAGING, STAGE_ARCHIVED},
    STAGE_PROD: {STAGE_STAGING, STAGE_ARCHIVED},
    STAGE_ARCHIVED: {STAGE_STAGING, STAGE_CANARY, STAGE_PROD},
}

# Stages with a single occupant per model: promoting a version into one of
# these archives the previous occupant (rollback reads the promotion history
# to find what to restore, never the archived stage).
_SINGLE_OCCUPANT_STAGES = frozenset({STAGE_CANARY, STAGE_PROD})

def validate_stage(stage: Any) -> str:
    if not isinstance(stage, str) or stage not in VERSION_STAGES:
        raise ValueError(f"stage must be one of {sorted(VERSION_STAGES)}")
    return stage


def validate_canary_percent(percent: Any) -> int:
    """Canary traffic share, bounded to 0-100. Out of range -> ValueError."""
    if type(percent) is bool or not isinstance(percent, int):
        raise ValueError("canary_percent must be an integer between 0 and 100")
    if not 0 <= percent <= 100:
        raise ValueError("canary_percent must be an integer between 0 and 100")
    return percent


def validate_version(version: Any) -> str:
    """A version id is a single path-safe token (same shape as a revision)."""
    validated = validate_revision(version)
    if validated is None:
        raise ValueError("version must be a single path-safe token")
    return validated

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

    # --- model versions (rollout lifecycle) --------------------------------
    def _versions_of(self, models: Dict[str, Dict[str, Any]], name: str) -> Dict[str, Dict[str, Any]]:
        versions = models[name].get("versions")
        if versions is None:
            versions = {}
            models[name]["versions"] = versions
        if not isinstance(versions, dict):
            raise ValueError(f"model '{name}' has a corrupt versions index")
        return versions

    def register_version(
        self,
        model: str,
        version: str,
        hf_repo: str,
        revision: Optional[str],
        engine: Optional[str] = None,
        stage: str = STAGE_STAGING,
    ) -> Dict[str, Any]:
        """Record a new build of a model, starting in `stage` (default staging).

        The model must already be registered. A version id may be registered
        only once; the model-level fields (hf_repo/revision/engine) are the
        fallback description, each version carries its own source reference.
        """
        model = validate_name(model)
        version = validate_version(version)
        if version is None:
            raise ValueError("version must be a single path-safe token")
        hf_repo = validate_hf_repo(hf_repo)
        revision = validate_revision(revision)
        engine = validate_engine(engine)
        stage = validate_stage(stage)
        with self._lock:
            models = self._read()
            if model not in models:
                raise ValueError(f"unknown model '{model}'")
            versions = self._versions_of(models, model)
            if version in versions:
                raise ValueError(f"model '{model}' already has version '{version}'")
            record = {
                "version": version,
                "hf_repo": hf_repo,
                "revision": revision,
                "engine": engine,
                "stage": STAGE_STAGING,  # registered first, then moved via _apply_stage
                "canary_percent": 0,
                "created_at": _now(),
                "updated_at": _now(),
            }
            versions[version] = record
            if stage != STAGE_STAGING:
                self._apply_stage(models, model, version, stage, record_event=False)
            models[model]["updated_at"] = _now()
            self._write(models)
            return dict(versions[version])

    def _apply_stage(
        self,
        models: Dict[str, Dict[str, Any]],
        model: str,
        version: str,
        stage: str,
        record_event: bool = True,
    ) -> Dict[str, Any]:
        """Move a version between stages, enforcing the transition machine.

        `models` is the already-loaded index; the caller holds the lock and
        writes. Versions occupying a single-occupant stage (canary/prod) are
        archived when displaced.
        """
        versions = self._versions_of(models, model)
        record = versions.get(version)
        if record is None:
            raise ValueError(f"unknown version '{version}' of model '{model}'")
        stage = validate_stage(stage)
        current = record.get("stage", STAGE_STAGING)
        if current == stage:
            return record
        if stage not in _STAGE_TRANSITIONS.get(current, frozenset()):
            raise ValueError(
                f"invalid stage transition for model '{model}' version '{version}': "
                f"{current} -> {stage}"
            )
        displaced: List[str] = []
        if stage in _SINGLE_OCCUPANT_STAGES:
            for other_version, other in versions.items():
                if other_version != version and other.get("stage") == stage:
                    other["stage"] = STAGE_ARCHIVED
                    other["updated_at"] = _now()
                    displaced.append(other_version)
        record["stage"] = stage
        record["updated_at"] = _now()
        if record_event:
            history = models[model].setdefault("promotion_history", [])
            history.append({
                "at": _now(),
                "version": version,
                "from_stage": current,
                "to_stage": stage,
                "displaced": displaced,
            })
        return record

    def set_stage(self, model: str, version: str, stage: str) -> Dict[str, Any]:
        """Move a version between stages; invalid transitions raise ValueError."""
        model = validate_name(model)
        version = validate_version(version)
        with self._lock:
            models = self._read()
            if model not in models:
                raise ValueError(f"unknown model '{model}'")
            self._apply_stage(models, model, version, stage)
            models[model]["updated_at"] = _now()
            self._write(models)
            return dict(self._versions_of(models, model)[version])

    def set_canary_percent(self, model: str, version: str, percent: int) -> Dict[str, Any]:
        """Bound the canary traffic share (0-100) of one version."""
        model = validate_name(model)
        version = validate_version(version)
        percent = validate_canary_percent(percent)
        with self._lock:
            models = self._read()
            if model not in models:
                raise ValueError(f"unknown model '{model}'")
            versions = self._versions_of(models, model)
            record = versions.get(version)
            if record is None:
                raise ValueError(f"unknown version '{version}' of model '{model}'")
            record["canary_percent"] = percent
            record["updated_at"] = _now()
            models[model]["updated_at"] = _now()
            self._write(models)
            return dict(record)

    def get_version(self, model: str, version: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            models = self._read()
            entry = models.get(validate_name(model))
            if entry is None:
                return None
            record = (entry.get("versions") or {}).get(validate_version(version))
            return dict(record) if record is not None else None

    def list_versions(self, model: str) -> List[Dict[str, Any]]:
        with self._lock:
            models = self._read()
            entry = models.get(validate_name(model))
            if entry is None:
                return []
            versions = entry.get("versions") or {}
        return [dict(versions[key]) for key in sorted(
            versions, key=lambda key: (versions[key].get("created_at", ""), key))]

    def active_version(self, model: str, stage: str = STAGE_PROD) -> Optional[Dict[str, Any]]:
        """The version currently occupying `stage` (default prod), or None.

        Models registered before versions existed report None for every
        stage — the pre-version behaviour is unchanged.
        """
        validate_stage(stage)
        with self._lock:
            models = self._read()
            entry = models.get(validate_name(model))
            if entry is None:
                return None
            versions = entry.get("versions") or {}
        for key in sorted(versions, key=lambda k: (versions[k].get("created_at", ""), k)):
            if versions[key].get("stage") == stage:
                return dict(versions[key])
        return None

    def promotion_history(self, model: str) -> List[Dict[str, Any]]:
        with self._lock:
            models = self._read()
            entry = models.get(validate_name(model))
            if entry is None:
                return []
            return [dict(event) for event in entry.get("promotion_history") or []]


# Router/import-time singleton; tests override attributes or use ModelRegistry(tmp).
model_registry = ModelRegistry()
