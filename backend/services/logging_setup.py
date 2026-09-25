"""Always-on structured service logging.

Every Python service emits one JSON line per platform event on stdout/stderr;
``platform.sh`` redirects those streams to ``backend/logs/<service>.log``, so a
service gets a full, structured, always-on trail without a debug gate.
uvicorn's server-lifecycle lines (startup/shutdown/errors) are interleaved in
the same file; its plain-text access lines are disabled — every HTTP request is
logged as a JSON ``request`` event by ``RequestLoggingMiddleware``, which
deliberately excludes the query string.

Line shape::

    {"ts": "2026-09-24T12:00:00.000Z", "service": "agent_tools", "level": "INFO",
     "logger": "agent_tools.requests", "event": "request", "message": "request",
     "fields": {"method": "POST", "path": "/api/tools/execute",
                "status": 200, "duration_ms": 12.34}}

Redaction policy: this module never formats headers, bodies, query strings or
credentials. Call sites log bounded scalar fields only (identifiers, counts,
durations, error types); secrets stay in ``backend/config/keys/`` and never
appear in any log line. Content (prompts, completions, file contents) is logged
as counts/hashes only.
"""
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

__all__ = ["JsonFormatter", "RequestLoggingMiddleware", "configure", "get_logger", "log_event"]


class JsonFormatter(logging.Formatter):
    """One JSON object per record; exc info appended under ``exc``."""

    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        now = datetime.now(timezone.utc)
        payload: Dict[str, Any] = {
            "ts": now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z",
            "service": self.service,
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", record.getMessage()),
            "message": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if fields:
            payload["fields"] = fields
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure(service: str, level: int = logging.INFO) -> None:
    """Install the JSON handler on the root logger (idempotent per process)."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if (isinstance(handler, logging.StreamHandler) and isinstance(handler.formatter, JsonFormatter)
                and handler.formatter.service == service):
            return  # already configured for this service in this process
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(service))
    root.handlers = [handler]
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, event: str, message: str = "",
              fields: Optional[Dict[str, Any]] = None, level: int = logging.INFO,
              exc_info: Any = None) -> None:
    """Emit a structured event; ``fields`` carries bounded scalars only."""
    logger.log(level, message or event, extra={"event": event, "fields": fields}, exc_info=exc_info)


class RequestLoggingMiddleware:
    """Structured per-request access log: method, path, status, duration_ms.

    Never logs headers, bodies or query strings — bearer tokens and credentials
    travel in headers, and a request body can contain user content. Paths on
    this platform never carry credentials.
    """

    def __init__(self, app, service: str):
        self.app = app
        self.service = service
        self.logger = logging.getLogger(f"{service}.requests")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = time.monotonic()
        status = {"code": None}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            duration_ms = round((time.monotonic() - started) * 1000, 2)
            log_event(self.logger, "request", "request",
                      fields={"method": scope.get("method"), "path": scope.get("path"),
                              "status": status["code"] or 500, "duration_ms": duration_ms,
                              "error": True}, level=logging.ERROR, exc_info=True)
            raise
        duration_ms = round((time.monotonic() - started) * 1000, 2)
        log_event(self.logger, "request", "request",
                  fields={"method": scope.get("method"), "path": scope.get("path"),
                          "status": status["code"], "duration_ms": duration_ms})
