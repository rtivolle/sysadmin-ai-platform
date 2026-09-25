"""VictoriaLogs audit ingestion with a durable, replayable local outbox.

Delivery is at least once: a crash after VictoriaLogs accepts a record but before
the outbox is updated may replay that record. ``event_id`` permits deduplication.
"""
import argparse
import fcntl
import json
import logging
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

VICTORIALOGS_URL = os.getenv("VICTORIALOGS_URL", "http://127.0.0.1:9428")
OUTBOX_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../data/victorialogs/outbox.jsonl"))
LOGGER = logging.getLogger(__name__)


def _post_event(event: Dict[str, Any]) -> bool:
    url = f"{VICTORIALOGS_URL.rstrip('/')}/insert/jsonline?_stream_fields=service,user_id,priority&_time_field=timestamp"
    response = httpx.post(
        url,
        content=json.dumps(event, ensure_ascii=False) + "\n",
        headers={"Content-Type": "application/stream+json"},
        timeout=2.0,
    )
    return response.status_code in (200, 204)


@contextmanager
def _outbox_lock():
    """One stable lock file coordinates writers and replay across processes."""
    os.makedirs(os.path.dirname(OUTBOX_PATH), exist_ok=True)
    fd = os.open(OUTBOX_PATH + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _append_outbox(event: Dict[str, Any]) -> None:
    line = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
    with _outbox_lock():
        created = not os.path.exists(OUTBOX_PATH)
        fd = os.open(OUTBOX_PATH, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(line)
            while view:
                written = os.write(fd, view)
                if written == 0:
                    raise OSError("audit outbox write made no progress")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        if created:
            directory_fd = os.open(os.path.dirname(OUTBOX_PATH), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)


def _replace_outbox(lines: list[bytes]) -> None:
    directory = os.path.dirname(OUTBOX_PATH)
    fd, temp_path = tempfile.mkstemp(prefix=".outbox-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as output:
            output.writelines(lines)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, OUTBOX_PATH)
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def flush_outbox(max_events: int = 1000) -> Dict[str, Any]:
    """Replay queued records in order, retaining the first failed record onward.

    The lock stays held through delivery and checkpointing so a concurrent append
    cannot be overwritten by replay. Malformed non-JSON lines are quarantined.
    """
    if max_events < 1:
        raise ValueError("max_events must be positive")
    with _outbox_lock():
        if not os.path.exists(OUTBOX_PATH):
            return {"sent": 0, "pending": 0}
        with open(OUTBOX_PATH, "rb") as source:
            lines = source.readlines()
        sent = 0
        error = None
        retained_lines = lines[max_events:]
        corrupted_path = OUTBOX_PATH.replace(".jsonl", "_corrupted.jsonl")

        for idx, line in enumerate(lines[:max_events]):
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("audit record must be a JSON object")
            except (ValueError, json.JSONDecodeError) as exc:
                # Quarantine malformed line to prevent poison-pill replay halt
                try:
                    with open(corrupted_path, "ab") as cf:
                        cf.write(line)
                except OSError:
                    pass
                continue

            try:
                if not _post_event(event):
                    error = "collector rejected audit record"
                    retained_lines = lines[idx:]
                    break
            except (httpx.HTTPError, OSError) as exc:
                error = str(exc)
                retained_lines = lines[idx:]
                break
            sent += 1

        if not error:
            _replace_outbox(retained_lines)
        else:
            _replace_outbox(retained_lines)

        result = {"sent": sent, "pending": len(retained_lines)}
        if error:
            result["error"] = error
        return result


def log_audit_event(
    user_id: str,
    session_id: str,
    tool_name: str,
    command: Optional[str] = None,
    human_approved: bool = False,
    exit_code: int = 0,
    duration_ms: int = 0,
    tokens_prompt: int = 0,
    tokens_completion: int = 0,
    extra: Optional[Dict[str, Any]] = None,
    parameters: Optional[Dict[str, Any]] = None,
    action: Optional[str] = None,
    approval_id: Optional[str] = None,
    prompt_tokens: Optional[int] = None,
    completion_tokens: Optional[int] = None,
    priority: Optional[str] = None,
    incident_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Ingest a structured audit record conforming to PROJECT.md:124-138 or fsync it to the outbox."""
    act = action or tool_name
    p_tokens = prompt_tokens if prompt_tokens is not None else tokens_prompt
    c_tokens = completion_tokens if completion_tokens is not None else tokens_completion
    appr_id = approval_id or (extra.get("approval_id") if extra else None)

    eff_priority = priority or (extra.get("priority") if extra else None)
    eff_incident_id = incident_id or (extra.get("incident_id") if extra else None)
    if not eff_priority:
        # Check if user is P1 elevated
        try:
            from backend.services.auth_gateway.quota_manager import quota_mgr
            if quota_mgr.is_p1_elevated(user_id):
                eff_priority = "P1-CRITICAL"
            else:
                eff_priority = "standard"
        except Exception:
            eff_priority = "standard"

    safe_params = dict(parameters) if parameters else {}
    if "proposed_content" in safe_params and isinstance(safe_params["proposed_content"], str):
        if len(safe_params["proposed_content"]) > 500:
            safe_params["proposed_content"] = safe_params["proposed_content"][:500] + f"... [truncated {len(safe_params['proposed_content'])} bytes]"

    event = {
        "event_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "service": "dsh-agent",
        "user_id": user_id,
        "session_id": session_id,
        "action": act,
        "tool_name": tool_name or act,
        "parameters": safe_params,
        "command": command or "",
        "human_approved": human_approved,
        "approval_id": appr_id,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "priority": eff_priority,
        "incident_id": eff_incident_id,
        "prompt_tokens": p_tokens,
        "completion_tokens": c_tokens,
        "tokens_prompt": p_tokens,
        "tokens_completion": c_tokens,
    }
    if extra:
        event["extra"] = extra
    try:
        if _post_event(event):
            return {"logged": True, "destination": "victorialogs"}
    except (httpx.HTTPError, OSError):
        pass
    try:
        _append_outbox(event)
        return {"logged": True, "destination": "outbox"}
    except (OSError, TypeError, ValueError) as exc:
        LOGGER.error("Audit event could not be persisted: %s", exc)
        return {"logged": False, "error": str(exc)}


def run_outbox_worker(interval_seconds: float = 5.0) -> None:
    """Continuously replay buffered events; platform.sh supervises the process."""
    while True:
        try:
            result = flush_outbox()
            if result.get("error"):
                LOGGER.warning("Audit replay paused: %s", result["error"])
        except OSError:
            LOGGER.exception("Audit replay failed")
        time.sleep(interval_seconds)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VictoriaLogs audit outbox worker")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args()
    if not args.worker or args.interval <= 0:
        parser.error("--worker and a positive --interval are required")
    from services.logging_setup import configure, get_logger, log_event
    configure("audit_outbox")
    log_event(get_logger("audit_outbox.worker"), "worker_start",
              "audit outbox worker starting", fields={"interval_seconds": args.interval})
    run_outbox_worker(args.interval)
