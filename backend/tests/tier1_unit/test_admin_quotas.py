"""Admin authorization, shared policy updates and admission enforcement."""
import uuid
from pathlib import Path

import httpx
import pytest

from services.auth_gateway import server
from services.auth_gateway.quota_manager import QuotaManager, QuotaExceededException


class MemoryStore:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value

    def zrangebyscore(self, *args):
        return []

    def zcount(self, *args):
        return 0


@pytest.fixture
def manager(monkeypatch):
    manager = QuotaManager(redis_client=MemoryStore())
    monkeypatch.setattr(manager, "is_p1_elevated", lambda user: False)
    return manager


@pytest.mark.parametrize("limits", [{"rpm": 0}, {"rpm": True}, {"tpm": 1.5},
                                    {"concurrency": 11}, {"daily_tokens": -1},
                                    {"unknown": 10}, None, []])
def test_invalid_limits_leave_policy_unchanged(manager, limits):
    manager.set_limits("user", {"rpm": 12})
    with pytest.raises(ValueError):
        manager.set_limits("user", limits)
    assert manager.get_limits("user")["rpm"] == 12


def test_limits_shared_across_managers_and_reset_keeps_usage(manager):
    other = QuotaManager(redis_client=manager.redis)
    manager.set_limits("user", {"daily_tokens": 100, "concurrency": 1, "rpm": 5, "tpm": 250})
    assert other.get_limits("user", False) == manager.get_limits("user")
    assert other.get_limits("user", True)["rpm"] == 5
    day = manager._quota_day()
    manager.redis.set(f"daily_tokens:user:{day}", "100")
    with pytest.raises(QuotaExceededException):
        other.check_daily_token_budget("user")
    manager.set_limits("user", {})
    assert other.get_limits("user", False)["rpm"] == 60
    assert other.get_limits("user", True)["rpm"] == 200
    assert manager.quota_snapshot("user")["usage"]["daily_tokens"] == 100


def test_corrupt_policy_and_store_outage_fail_closed(manager, monkeypatch):
    manager.redis.set("quota:limits:user", '{"rpm": 0}')
    with pytest.raises(ConnectionError):
        manager.get_limits("user")

    def unavailable(*args):
        raise OSError("store down")

    monkeypatch.setattr(manager.redis, "get", unavailable)
    monkeypatch.setattr(manager.redis, "set", unavailable)
    for operation in (lambda: manager.get_limits("user"),
                      lambda: manager.set_limits("user", {"rpm": 10}),
                      lambda: manager.quota_snapshot("user")):
        with pytest.raises(ConnectionError):
            operation()


@pytest.mark.asyncio
async def test_admin_quota_api_authorization_validation_and_audit(manager, monkeypatch):
    monkeypatch.setattr(server, "quota_mgr", manager)
    monkeypatch.setattr(server, "VALID_USERS", {"sysadmin-01"})
    monkeypatch.setattr(server, "load_valid_tokens", lambda: {"admin-token": "sysadmin-admin", "user-token": "sysadmin-01"})
    events = []
    monkeypatch.setattr("services.agent_tools.audit.log_audit_event", lambda **event: events.append(event))
    path = "/api/v1/admin/quotas"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        assert (await client.get(path, headers={"X-User": "sysadmin-admin"})).status_code == 401
        assert (await client.get(path, headers={"Authorization": "Bearer user-token"})).status_code == 403
        headers = {"Authorization": "Bearer admin-token"}
        assert (await client.post(path + "/sysadmin-01", headers={"Authorization": "Bearer user-token"}, json={"limits": {}})).status_code == 403
        assert (await client.post(path + "/missing", headers=headers, json={"limits": {}})).status_code == 404
        assert (await client.post(path + "/sysadmin-01", headers=headers, json={"limits": {"rpm": 0}})).status_code == 400
        updated = await client.post(path + "/sysadmin-01", headers=headers, json={"limits": {"rpm": 20}})
        assert updated.status_code == 200
        assert events[-1]["action"] == "quota_update"
        assert events[-1]["parameters"] == {"user_id": "sysadmin-01", "limits": {"rpm": 20}}
        snapshot = (await client.get(path, headers=headers)).json()
        assert snapshot["users"][0]["limits"]["rpm"] == 20
        monkeypatch.setattr(manager, "_redis", None)
        assert (await client.get(path, headers=headers)).status_code == 503
        assert (await client.post(path + "/sysadmin-01", headers=headers, json={"limits": {}})).status_code == 503


def test_live_updated_limits_enforce_across_workers():
    password_file = Path(__file__).resolve().parents[2] / "config/keys/valkey-password.key"
    if not password_file.exists():
        pytest.skip("Live Valkey credentials unavailable")
    manager = QuotaManager(valkey_url=f"redis://:{password_file.read_text().strip()}@127.0.0.1:6379/0")
    if manager.redis is None:
        pytest.skip("Live Valkey unavailable")
    other = QuotaManager(redis_client=manager.redis)
    user = f"admin-quota-test-{uuid.uuid4().hex}"
    day = manager._quota_day()
    lease = None
    try:
        manager.set_limits(user, {"concurrency": 1, "rpm": 1, "daily_tokens": 100})
        lease = other.acquire_concurrency_slot(user)
        with pytest.raises(QuotaExceededException):
            other.acquire_concurrency_slot(user)
        other.check_and_record_rpm(user)
        with pytest.raises(QuotaExceededException):
            other.check_and_record_rpm(user)
        other.reserve_daily_token_budget(user, "first", 100)
        with pytest.raises(QuotaExceededException):
            other.reserve_daily_token_budget(user, "second", 1)
        snapshot = manager.quota_snapshot(user)
        assert snapshot["usage"]["reserved_tokens"] == 100
        assert snapshot["usage"]["concurrency"] == 1
        manager.set_limits(user, {"daily_tokens": 200})
        other.reserve_daily_token_budget(user, "second", 1)
    finally:
        if lease:
            other.release_concurrency_slot(lease)
        manager.redis.delete(f"quota:limits:{user}", f"quota:leases:user:{user}", f"inflight:{user}",
                             f"rate:rpm:{user}", f"daily_tokens:{user}:{day}",
                             f"daily_reservations:index:{user}:{day}", f"daily_reservations:active:{user}:{day}",
                             f"daily_reservations:settled:{user}:{day}")
