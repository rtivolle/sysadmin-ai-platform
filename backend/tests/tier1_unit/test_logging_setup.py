"""Structured logging: JSON shape, middleware behaviour, redaction policy."""
import json
import logging

import pytest

from services.logging_setup import (
    JsonFormatter,
    RequestLoggingMiddleware,
    configure,
    get_logger,
    log_event,
)


def test_formatter_emits_json_line_with_fields():
    formatter = JsonFormatter("svc")
    record = logging.LogRecord(
        name="svc.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="hello", args=(), exc_info=None)
    record.event = "started"
    record.fields = {"port": 8000}
    line = formatter.format(record)
    payload = json.loads(line)
    assert payload["service"] == "svc"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "svc.test"
    assert payload["event"] == "started"
    assert payload["message"] == "hello"
    assert payload["fields"] == {"port": 8000}
    assert payload["ts"].endswith("Z")


def test_formatter_serializes_exc_info():
    formatter = JsonFormatter("svc")
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        record = logging.LogRecord(
            name="svc.test", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="failed", args=(), exc_info=sys.exc_info())
    payload = json.loads(formatter.format(record))
    assert "boom" in payload["exc"]


def test_configure_is_idempotent():
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        configure("svc-a")
        count = sum(1 for h in root.handlers if isinstance(h.formatter, JsonFormatter))
        configure("svc-a")
        assert sum(1 for h in root.handlers if isinstance(h.formatter, JsonFormatter)) == count
        assert root.level == logging.INFO
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)


def test_configure_relabels_when_service_changes():
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        configure("svc-a")
        configure("svc-b")
        formatters = [h.formatter for h in root.handlers if isinstance(h.formatter, JsonFormatter)]
        assert [f.service for f in formatters] == ["svc-b"]
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)


@pytest.mark.asyncio
async def test_middleware_logs_status_and_duration(caplog):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = RequestLoggingMiddleware(app, service="testsvc")
    scope = {"type": "http", "method": "POST", "path": "/api/v1/agent/chat",
             "headers": [(b"authorization", b"Bearer top-secret-token")]}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b'{"secret":"prompt-payload"}', "more_body": False}

    async def send(message):
        sent.append(message)

    with caplog.at_level(logging.INFO, logger="testsvc.requests"):
        await middleware(scope, receive, send)

    assert [m["type"] for m in sent[:2]] == ["http.response.start", "http.response.body"]
    records = [r for r in caplog.records if getattr(r, "event", None) == "request"]
    assert len(records) == 1
    fields = records[0].fields
    assert fields["method"] == "POST"
    assert fields["path"] == "/api/v1/agent/chat"
    assert fields["status"] == 200
    assert fields["duration_ms"] >= 0
    # Redaction policy: headers and bodies are never present in the serialized
    # line — assert against the formatted JSON, the strongest surface.
    formatter = JsonFormatter("testsvc")
    for record in caplog.records:
        line = formatter.format(record)
        assert "top-secret-token" not in line
        assert "prompt-payload" not in line


@pytest.mark.asyncio
async def test_middleware_logs_exception_and_reraises(caplog):
    async def app(scope, receive, send):
        raise RuntimeError("upstream dead")

    middleware = RequestLoggingMiddleware(app, service="testsvc")
    scope = {"type": "http", "method": "GET", "path": "/health", "headers": []}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    with caplog.at_level(logging.ERROR, logger="testsvc.requests"):
        with pytest.raises(RuntimeError, match="upstream dead"):
            await middleware(scope, receive, lambda message: None)

    records = [r for r in caplog.records if getattr(r, "event", None) == "request"]
    assert len(records) == 1
    assert records[0].fields["status"] == 500
    assert records[0].fields["error"] is True
    assert records[0].exc_info


@pytest.mark.asyncio
async def test_middleware_passes_non_http_scopes_through(caplog):
    calls = []

    async def app(scope, receive, send):
        calls.append(scope["type"])

    middleware = RequestLoggingMiddleware(app, service="testsvc")
    with caplog.at_level(logging.INFO, logger="testsvc.requests"):
        await middleware({"type": "lifespan"}, None, lambda message: None)
    assert calls == ["lifespan"]
    assert not [r for r in caplog.records if getattr(r, "event", None) == "request"]


def test_log_event_sets_event_and_fields(caplog):
    logger = get_logger("testsvc.events")
    with caplog.at_level(logging.INFO, logger="testsvc.events"):
        log_event(logger, "lifecycle", "model started", fields={"model": "m1", "port": 8100})
    record = caplog.records[-1]
    assert record.event == "lifecycle"
    assert record.getMessage() == "model started"
    assert record.fields == {"model": "m1", "port": 8100}
