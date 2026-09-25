"""Structured completion logs: every inference path emits a bounded event."""
import logging

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from services.inference_engine import server
from services.logging_setup import JsonFormatter

_LOGGER_NAME = "inference_engine.server"

_ALLOWED_FIELDS = {"model", "stream", "simulated", "upstream_status", "duration_ms",
                   "prompt_tokens", "completion_tokens", "bytes", "error", "error_type"}


def _request(body: bytes):
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({"type": "http", "headers": [], "method": "POST",
                    "path": "/v1/chat/completions"}, receive)


def _completion_records(caplog):
    return [r for r in caplog.records if getattr(r, "event", None) == "completion"]


def _assert_no_content(records):
    formatter = JsonFormatter("inference")
    for record in records:
        line = formatter.format(record)
        assert "hello" not in line
        assert "Reply" not in line
        assert "secret-sentence" not in line
        assert set(record.fields) <= _ALLOWED_FIELDS


@pytest.mark.asyncio
async def test_non_streamed_upstream_completion_logs_tokens(caplog, monkeypatch):
    payload = {"usage": {"prompt_tokens": 12, "completion_tokens": 7}}
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload)))
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr(server, "_local_model_port", lambda _model: 8100)
    monkeypatch.setattr(server, "_is_registry_model", lambda _model: True)
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        response = await server.chat_completions(_request(b'{"model":"real","stream":false}'))
    assert response.status_code == 200
    records = _completion_records(caplog)
    assert len(records) == 1
    assert records[0].fields["model"] == "real"
    assert records[0].fields["upstream_status"] == 200
    assert records[0].fields["prompt_tokens"] == 12
    assert records[0].fields["completion_tokens"] == 7
    assert records[0].fields["duration_ms"] >= 0
    _assert_no_content(records)


@pytest.mark.asyncio
async def test_upstream_error_completion_logs_status(caplog, monkeypatch):
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(400, json={"error": "context exceeded"})))
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr(server, "_local_model_port", lambda _model: 8100)
    monkeypatch.setattr(server, "_is_registry_model", lambda _model: True)
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        response = await server.chat_completions(_request(b'{"model":"real","stream":true}'))
    assert response.status_code == 400
    records = _completion_records(caplog)
    assert len(records) == 1
    assert records[0].fields["upstream_status"] == 400
    assert records[0].fields["error"] is True
    assert records[0].fields["stream"] is True  # request flag, not the fallback path
    assert "prompt_tokens" not in records[0].fields
    assert "completion_tokens" not in records[0].fields
    _assert_no_content(records)


@pytest.mark.asyncio
async def test_connect_failure_logs_error_event(caplog, monkeypatch):
    class FailingClient:
        def __init__(self, **kwargs):
            self.is_closed = False

        def build_request(self, method, url, **kwargs):
            return httpx.Request(method, url)

        async def send(self, request, **kwargs):
            raise httpx.ConnectError("connection refused")

        async def aclose(self):
            self.is_closed = True

    monkeypatch.setattr(server.httpx, "AsyncClient", FailingClient)
    monkeypatch.setattr(server, "_local_model_port", lambda _model: 8100)
    monkeypatch.setattr(server, "_is_registry_model", lambda _model: True)
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        with pytest.raises(HTTPException) as exc_info:
            await server.chat_completions(_request(b'{"model":"real","stream":true}'))
    assert exc_info.value.status_code == 502
    records = _completion_records(caplog)
    assert len(records) == 1
    assert records[0].fields["error_type"] == "ConnectError"
    assert records[0].levelno == logging.WARNING
    _assert_no_content(records)


@pytest.mark.asyncio
async def test_streamed_completion_logs_bytes_at_stream_end(caplog, monkeypatch):
    parts = [b'data: {"choices":[]}\n\n', b'data: [DONE]\n\n']
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"".join(parts), headers={"content-type": "text/event-stream"})))
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr(server, "_local_model_port", lambda _model: 8100)
    monkeypatch.setattr(server, "_is_registry_model", lambda _model: True)
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        response = await server.chat_completions(_request(b'{"model":"real","stream":true}'))
        received = b"".join([part async for part in response.body_iterator])
        await response.background()
    assert received == b"".join(parts)
    records = _completion_records(caplog)
    assert len(records) == 1
    assert records[0].fields["stream"] is True
    assert records[0].fields["upstream_status"] == 200
    assert records[0].fields["bytes"] == len(b"".join(parts))
    _assert_no_content(records)


@pytest.mark.asyncio
async def test_simulated_completion_logged_without_content(caplog, monkeypatch):
    monkeypatch.setattr(server, "UPSTREAM_VLLM_URL", "")
    monkeypatch.setattr(server, "_local_model_port", lambda _model: None)
    monkeypatch.setattr(server, "_is_registry_model", lambda _model: False)
    body = (b'{"model":"fast-model","stream":false,'
            b'"messages":[{"role":"user","content":"hello secret-sentence"}]}')
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        response = await server.chat_completions(_request(body))
    assert response["model"] == "fast-model"
    assert response["choices"]
    records = _completion_records(caplog)
    assert len(records) == 1
    assert records[0].fields["simulated"] is True
    assert records[0].fields["model"] == "fast-model"
    assert records[0].fields["prompt_tokens"] >= 1
    _assert_no_content(records)


@pytest.mark.asyncio
async def test_simulated_stream_completion_logged(caplog, monkeypatch):
    monkeypatch.setattr(server, "UPSTREAM_VLLM_URL", "")
    monkeypatch.setattr(server, "_local_model_port", lambda _model: None)
    monkeypatch.setattr(server, "_is_registry_model", lambda _model: False)
    body = (b'{"model":"fast-model","stream":true,'
            b'"messages":[{"role":"user","content":"hello"}]}')
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        response = await server.chat_completions(_request(body))
        parts = [part async for part in response.body_iterator]
    assert parts and parts[-1] == "data: [DONE]\n\n"
    records = _completion_records(caplog)
    assert len(records) == 1
    assert records[0].fields["simulated"] is True
    assert records[0].fields["stream"] is True
    _assert_no_content(records)


@pytest.mark.asyncio
async def test_stream_disconnect_does_not_log_completion(caplog, monkeypatch):
    class Chunks(httpx.AsyncByteStream):
        def __init__(self, parts):
            self.parts = parts

        async def __aiter__(self):
            for part in self.parts:
                yield part

        async def aclose(self):
            pass

    source = Chunks([b'data: first\n\n', b'data: second\n\n'])
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=source)))
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr(server, "_local_model_port", lambda _model: 8100)
    monkeypatch.setattr(server, "_is_registry_model", lambda _model: True)
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        response = await server.chat_completions(_request(b'{"model":"real","stream":true}'))
        await anext(response.body_iterator)
        await response.body_iterator.aclose()
    assert _completion_records(caplog) == []
