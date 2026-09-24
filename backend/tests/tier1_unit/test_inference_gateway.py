import httpx
import pytest

from backend.services.agent_runtime import react_loop


@pytest.mark.asyncio
async def test_quota_rejection_does_not_bypass_gateway(monkeypatch, tmp_path):
    calls = []
    (tmp_path / "sysadmin-01.key").write_text("test-token")
    monkeypatch.setattr(react_loop, "KEYS_DIR", tmp_path)

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(429, json={"error": "quota exhausted"})

    transport = httpx.MockTransport(respond)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        react_loop.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=transport, **kwargs),
    )

    with pytest.raises(httpx.HTTPStatusError) as error:
        await react_loop.call_llm([{"role": "user", "content": "hello"}], "fast-model", "sysadmin-01")

    assert error.value.response.status_code == 429
    assert calls == ["http://127.0.0.1:4000/v1/chat/completions"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["gateway_502", "network_error", "malformed_response"])
async def test_gateway_failure_never_uses_direct_inference(monkeypatch, tmp_path, failure):
    calls = []
    (tmp_path / "sysadmin-01.key").write_text("test-token")
    monkeypatch.setattr(react_loop, "KEYS_DIR", tmp_path)

    def respond(request):
        calls.append(str(request.url))
        if failure == "network_error":
            raise httpx.ConnectError("gateway down", request=request)
        if failure == "gateway_502":
            return httpx.Response(502, json={"error": "upstream down"})
        return httpx.Response(200, json={"choices": []})

    transport = httpx.MockTransport(respond)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        react_loop.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=transport, **kwargs),
    )

    with pytest.raises((httpx.HTTPStatusError, httpx.ConnectError, IndexError)):
        await react_loop.call_llm([{"role": "user", "content": "hello"}], "fast-model", "sysadmin-01")

    assert calls == ["http://127.0.0.1:4000/v1/chat/completions"]
