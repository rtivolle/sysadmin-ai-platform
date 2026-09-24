"""HuggingFace snapshot downloads with a local token and disk preflight.

`huggingface_hub.snapshot_download` handles resume through its own cache: a
re-run of the same repo/revision reuses completed files. Older options such as
`resume_download` and `local_dir_use_symlinks` were removed in huggingface_hub
1.x and are deliberately not passed.

The network call is injected (`snapshot_fn`) so the download state machine is
testable without egress or a GPU. `HF_ENDPOINT` (mirror) and `HF_HOME` are read
by huggingface_hub itself from the environment.
"""
import os
import re
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

from . import registry as registry_module

DEFAULT_MIN_FREE_BYTES = 1024 * 1024 * 1024  # 1 GiB safety floor before a download.

_JOBS_LOCK = threading.Lock()
_JOBS: Dict[str, Dict[str, Any]] = {}

_REFERENCE_RE = re.compile(
    r"^(?:https?://(?:www\.)?huggingface\.co/)?(?P<repo>[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:/tree/(?P<revision>[A-Za-z0-9][A-Za-z0-9._-]*))?/?$"
)


def token_file_path() -> str:
    return os.getenv(
        "HF_TOKEN_FILE",
        os.path.abspath(os.path.join(os.path.dirname(__file__), "../../config/keys/hf-token.key")),
    )


def parse_hf_reference(value: str) -> Tuple[str, Optional[str]]:
    """Accept `org/model` or a huggingface.co URL; return (repo, revision)."""
    if not isinstance(value, str):
        raise ValueError("a HuggingFace repo id or URL is required")
    match = _REFERENCE_RE.match(value.strip())
    if not match:
        raise ValueError("expected 'org/model' or https://huggingface.co/org/model[/tree/<rev>]")
    repo = registry_module.validate_hf_repo(match.group("repo"))
    revision = registry_module.validate_revision(match.group("revision"))
    return repo, revision


def resolve_token() -> Optional[str]:
    """Prefer HF_TOKEN, then the local 0600 key file. Never logged."""
    env_token = os.getenv("HF_TOKEN")
    if env_token:
        return env_token.strip() or None
    path = token_file_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            token = handle.read().strip()
        return token or None
    except OSError:
        return None


def dir_size(path: str) -> int:
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(root, name))]
        for name in files:
            try:
                total += os.stat(os.path.join(root, name), follow_symlinks=False).st_size
            except OSError:
                pass
    return total


def _free_bytes(path: str) -> Optional[int]:
    probe = path
    while probe and not os.path.exists(probe):
        probe = os.path.dirname(probe)
    try:
        stat = os.statvfs(probe or ".")
        return stat.f_bavail * stat.f_frsize
    except OSError:
        return None


def _default_snapshot_fn(**kwargs: Any) -> str:
    # Imported lazily: a missing dependency is an error only when a download runs.
    from huggingface_hub import snapshot_download
    return snapshot_download(**kwargs)


def download_sync(
    name: str,
    store: registry_module.ModelRegistry,
    snapshot_fn: Optional[Callable[..., str]] = None,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
) -> Dict[str, Any]:
    """Run one download to completion, updating the registry. Raises on failure."""
    entry = store.get(name)
    if entry is None:
        raise KeyError(f"unknown model '{name}'")
    target = store.path_for(name)
    free = _free_bytes(os.path.dirname(target) or target)
    if free is not None and free < min_free_bytes:
        raise OSError(
            f"insufficient free space for model store: {free} bytes free, "
            f"{min_free_bytes} required"
        )

    store.update(name, status=registry_module.STATUS_DOWNLOADING, last_error=None)
    try:
        os.makedirs(target, exist_ok=True)
        os.chmod(target, 0o700)
        downloader = snapshot_fn or _default_snapshot_fn
        downloader(
            repo_id=entry["hf_repo"],
            revision=entry.get("revision"),
            local_dir=target,
            token=resolve_token(),
        )
        size = dir_size(target)
        return store.update(
            name,
            status=registry_module.STATUS_DOWNLOADED,
            path=target,
            size_bytes=size,
            last_error=None,
        )
    except Exception as exc:  # network, auth, disk, or revision errors
        store.update(name, status=registry_module.STATUS_ERROR, last_error=str(exc))
        raise


def _run_job(name: str, store: registry_module.ModelRegistry, snapshot_fn: Optional[Callable[..., str]]) -> None:
    try:
        download_sync(name, store, snapshot_fn=snapshot_fn)
    except Exception as exc:
        with _JOBS_LOCK:
            if name in _JOBS:
                _JOBS[name].update(status="error", error=str(exc), finished_at=time.time())
    else:
        with _JOBS_LOCK:
            if name in _JOBS:
                _JOBS[name].update(status="downloaded", error=None, finished_at=time.time())


def start_download(
    name: str,
    store: registry_module.ModelRegistry,
    snapshot_fn: Optional[Callable[..., str]] = None,
) -> Dict[str, Any]:
    """Start a background download. One in-flight job per model."""
    if store.get(name) is None:
        raise KeyError(f"unknown model '{name}'")
    with _JOBS_LOCK:
        current = _JOBS.get(name)
        if current and current.get("status") == "downloading":
            raise RuntimeError(f"download already running for '{name}'")
        _JOBS[name] = {"status": "downloading", "started_at": time.time(), "error": None, "finished_at": None}
    thread = threading.Thread(target=_run_job, args=(name, store, snapshot_fn), daemon=True)
    thread.start()
    return job_status(name)


def job_status(name: str) -> Dict[str, Any]:
    with _JOBS_LOCK:
        job = _JOBS.get(name)
        return dict(job) if job else {"status": "idle"}
