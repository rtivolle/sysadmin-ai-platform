"""Shared quota outages reject admission instead of creating untracked work."""
import httpx
import pytest

from services.auth_gateway import quota_manager, server


def test_shared_quota_outage_rejects_all_admission_checks(monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("Valkey unavailable")

    monkeypatch.setattr(quota_manager.redis.Redis, "from_url", unavailable)
    manager = quota_manager.QuotaManager(valkey_url="redis://127.0.0.1:6399/0")

    with pytest.raises(ConnectionError):
        manager.acquire_concurrency_slot("sysadmin-01")
    with pytest.raises(ConnectionError):
        manager.check_and_record_rpm("sysadmin-01")
    with pytest.raises(ConnectionError):
        manager.check_daily_token_budget("sysadmin-01")
    with pytest.raises(ConnectionError):
        manager.record_token_consumption("sysadmin-01", 1, 1)


@pytest.mark.asyncio
async def test_forward_auth_returns_503_when_quota_state_is_unavailable(monkeypatch):
    class UnavailableQuota:
        def check_daily_token_budget(self, user_id):
            raise ConnectionError("Valkey unavailable")

    monkeypatch.setattr(server, "authenticate_request", lambda request: ("sysadmin-01", "bearer_token"))
    monkeypatch.setattr(server, "quota_mgr", UnavailableQuota())
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/verify")
    assert response.status_code == 503
    assert response.json()["error"] == "quota_state_unavailable"
