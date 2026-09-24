"""Regression: buffered httpx responses cannot be replayed using aiter_raw()."""
import httpx
import pytest
from starlette.requests import Request
from services.inference_engine import server


class Chunks(httpx.AsyncByteStream):
    def __init__(self, parts):
        self.parts = parts
        self.closed = False

    async def __aiter__(self):
        for part in self.parts:
            assert not self.closed
            yield part

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 400])
async def test_proxy_stream_lifetime_and_error_status(monkeypatch, status):
    parts = [b'data: {"choices":[]}\n\n', b'data: [DONE]\n\n'] if status == 200 else [b'{"error":"context exceeded"}']
    source = Chunks(parts)
    real_client = httpx.AsyncClient
    client = real_client(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, stream=source, headers={"content-type": "application/json"})))
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr(server, "_local_model_port", lambda model: 8100)
    monkeypatch.setattr(server, "_is_registry_model", lambda model: True)
    async def receive():
        return {"type": "http.request", "body": b'{"model":"real","stream":true}', "more_body": False}
    request = Request({"type": "http", "headers": [], "method": "POST", "path": "/v1/chat/completions"}, receive)
    response = await server.chat_completions(request)
    assert response.status_code == status
    if status == 200:
        assert not client.is_closed and not source.closed
        received = b"".join([part async for part in response.body_iterator])
        await response.background()
    else:
        received = response.body
    assert received == b"".join(parts)
    assert client.is_closed and source.closed


@pytest.mark.asyncio
async def test_downstream_early_close_releases_upstream(monkeypatch):
    source = Chunks([b'data: first\n\n', b'data: second\n\n'])
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=source)))
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr(server, "_local_model_port", lambda _: 8100)
    monkeypatch.setattr(server, "_is_registry_model", lambda _: True)
    async def receive():
        return {"type": "http.request", "body": b'{"stream":true}'}
    response = await server.chat_completions(Request({"type": "http", "headers": []}, receive))
    await anext(response.body_iterator)
    await response.body_iterator.aclose()
    assert client.is_closed and source.closed


@pytest.mark.asyncio
async def test_proxy_non_streaming_success_preserves_body_and_content_type(monkeypatch):
    payload = {"id": "chatcmpl-1", "choices": [{"message": {"content": "ok"}}]}
    upstream = None

    def handler(request):
        nonlocal upstream
        upstream = httpx.Response(200, json=payload,
                                  headers={"content-type": "application/json; charset=utf-8"})
        return upstream

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr(server, "_local_model_port", lambda _: 8100)
    monkeypatch.setattr(server, "_is_registry_model", lambda _: True)
    async def receive():
        return {"type": "http.request", "body": b'{"model":"real","stream":false}', "more_body": False}
    response = await server.chat_completions(
        Request({"type": "http", "headers": [], "method": "POST", "path": "/v1/chat/completions"}, receive))
    assert response.status_code == 200
    assert response.body == upstream.content
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    assert client.is_closed


@pytest.mark.asyncio
async def test_proxy_upstream_connect_failure_returns_502(monkeypatch):
    from fastapi import HTTPException

    class FailingClient:
        def __init__(self, **kwargs):
            self.is_closed = False

        def build_request(self, method, url, **kwargs):
            return httpx.Request(method, url)

        async def send(self, request, **kwargs):
            raise httpx.ConnectError("connection refused by backend")

        async def aclose(self):
            self.is_closed = True

    stub = FailingClient()
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kwargs: stub)
    monkeypatch.setattr(server, "_local_model_port", lambda _: 8100)
    monkeypatch.setattr(server, "_is_registry_model", lambda _: True)
    async def receive():
        return {"type": "http.request", "body": b'{"model":"real","stream":true}', "more_body": False}
    with pytest.raises(HTTPException) as exc_info:
        await server.chat_completions(
            Request({"type": "http", "headers": [], "method": "POST", "path": "/v1/chat/completions"}, receive))
    assert exc_info.value.status_code == 502
    assert "connection refused by backend" in exc_info.value.detail
    assert stub.is_closed
