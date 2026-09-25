"""Bearer identity resolution: file mode unchanged, durable store authoritative."""
import httpx
import pytest

from services.auth_gateway import server


class FakeKeyStore:
    """Stands in for services.control_store.KeyStore."""

    def __init__(self, mapping=None, failure=None):
        self.mapping = dict(mapping or {})
        self.failure = failure
        self.rotations = []
        self.revocations = []

    def _maybe_fail(self):
        if self.failure is not None:
            raise self.failure

    def resolve(self, token):
        self._maybe_fail()
        return self.mapping.get(token)

    def active_keys(self, user_id=None):
        self._maybe_fail()
        return [{"user_id": "sysadmin-01", "label": "", "created_at": "2026-09-24T00:00:00Z", "created_by": "admin"}]

    def rotate(self, user_id, label="", created_by=""):
        self._maybe_fail()
        self.rotations.append((user_id, label, created_by))
        return "sk-rotated-token"

    def revoke(self, user_id, actor=""):
        self._maybe_fail()
        self.revocations.append(user_id)
        return 3


@pytest.fixture(autouse=True)
def clean_identity_state(monkeypatch):
    monkeypatch.delenv("SYSADMIN_CONTROL_STORE", raising=False)
    monkeypatch.delenv("SYSADMIN_DATABASE_URL", raising=False)
    monkeypatch.setattr(server, "_key_store", None)
    monkeypatch.setattr(server, "MASTER_TOKEN", "")
    yield


def test_file_mode_still_resolves_from_the_provisioned_map(monkeypatch):
    monkeypatch.setattr(server, "load_valid_tokens", lambda: {"file-token": "sysadmin-01"})
    assert server.resolve_identity("file-token") == "sysadmin-01"
    assert server.resolve_identity("unknown-token") is None
    assert server.resolve_identity("") is None


def test_store_mode_uses_the_durable_store(monkeypatch):
    monkeypatch.setenv("SYSADMIN_CONTROL_STORE", "postgres")
    monkeypatch.setattr(server, "load_valid_tokens", lambda: {"file-token": "sysadmin-01"})
    monkeypatch.setattr(server, "_key_store", FakeKeyStore({"durable-token": "sysadmin-02"}))
    assert server.resolve_identity("durable-token") == "sysadmin-02"
    # The store is authoritative: a file key that was never imported does not work.
    assert server.resolve_identity("file-token") is None


def test_store_mode_never_falls_back_to_files_when_the_store_is_down(monkeypatch):
    monkeypatch.setenv("SYSADMIN_CONTROL_STORE", "postgres")
    monkeypatch.setattr(server, "load_valid_tokens", lambda: {"file-token": "sysadmin-01"})
    monkeypatch.setattr(
        server, "_key_store", FakeKeyStore(failure=ConnectionError("store down"))
    )
    with pytest.raises(ConnectionError):
        server.resolve_identity("file-token")


def test_master_credential_always_resolves_to_admin(monkeypatch):
    monkeypatch.setenv("SYSADMIN_CONTROL_STORE", "postgres")
    monkeypatch.setattr(server, "_key_store", FakeKeyStore({}))
    monkeypatch.setattr(server, "MASTER_TOKEN", "master-secret")
    assert server.resolve_identity("master-secret") == "sysadmin-admin"
    assert server.resolve_identity("not-the-master") is None


@pytest.mark.asyncio
async def test_verify_returns_503_when_the_durable_store_is_down(monkeypatch):
    monkeypatch.setenv("SYSADMIN_CONTROL_STORE", "postgres")
    monkeypatch.setattr(server, "load_valid_tokens", lambda: {"file-token": "sysadmin-01"})
    monkeypatch.setattr(server, "_key_store", FakeKeyStore(failure=ConnectionError("store down")))
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/verify", headers={"Authorization": "Bearer file-token"})
    assert response.status_code == 503
    assert response.json()["detail"] == "Shared state store unavailable"


@pytest.mark.asyncio
async def test_verify_forwards_the_store_identity(monkeypatch):
    monkeypatch.setenv("SYSADMIN_CONTROL_STORE", "postgres")
    monkeypatch.setattr(server, "_key_store", FakeKeyStore({"durable-token": "sysadmin-02"}))
    monkeypatch.setattr(server.quota_mgr, "is_p1_elevated", lambda user: False)
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/verify", headers={"Authorization": "Bearer durable-token"})
    assert response.status_code == 200
    assert response.headers["X-Forwarded-User"] == "sysadmin-02"


@pytest.mark.asyncio
async def test_admin_key_routes_require_the_durable_store(monkeypatch):
    monkeypatch.setattr(server, "load_valid_tokens", lambda: {"admin-token": "sysadmin-admin"})
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listing = await client.get("/api/v1/admin/keys", headers={"Authorization": "Bearer admin-token"})
        assert listing.status_code == 409
        rotate = await client.post("/api/v1/admin/keys/sysadmin-01/rotate", headers={"Authorization": "Bearer admin-token"})
        assert rotate.status_code == 409


@pytest.mark.asyncio
async def test_admin_key_rotation_returns_the_key_once_and_audits(monkeypatch):
    # The store is authoritative in this mode, so the operator's own key is a
    # store key too (imported at provisioning time).
    store = FakeKeyStore({"admin-token": "sysadmin-admin", "user-token": "sysadmin-01"})
    monkeypatch.setenv("SYSADMIN_CONTROL_STORE", "postgres")
    monkeypatch.setattr(server, "_key_store", store)
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        rotated = await client.post(
            "/api/v1/admin/keys/sysadmin-01/rotate",
            headers={"Authorization": "Bearer admin-token"},
            json={"label": "laptop"},
        )
        revoked = await client.post(
            "/api/v1/admin/keys/sysadmin-01/revoke",
            headers={"Authorization": "Bearer admin-token"},
        )
        unknown = await client.post(
            "/api/v1/admin/keys/nobody/revoke",
            headers={"Authorization": "Bearer admin-token"},
        )
        forbidden = await client.post(
            "/api/v1/admin/keys/sysadmin-01/revoke",
            headers={"Authorization": "Bearer user-token"},
        )
    assert rotated.status_code == 200
    assert rotated.json()["key"] == "sk-rotated-token"
    assert rotated.json()["shown_once"] is True
    assert rotated.headers["cache-control"] == "no-store"
    assert store.rotations == [("sysadmin-01", "laptop", "sysadmin-admin")]
    assert revoked.status_code == 200 and revoked.json()["revoked"] == 3
    assert store.revocations == ["sysadmin-01"]
    assert unknown.status_code == 404
    assert forbidden.status_code == 403
