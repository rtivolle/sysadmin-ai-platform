"""Login aliases and provisioned credential behavior."""
import hashlib
import json

import httpx
import pytest

from backend.services.auth_gateway import server


class FakeValkey:
    def __init__(self):
        self.sessions = {}

    def setex(self, key, ttl, value):
        self.sessions[key] = value


@pytest.mark.asyncio
async def test_login_routes_use_provisioned_hashes(monkeypatch, tmp_path):
    password = "random-test-password"
    salt = b"0123456789abcdef0123456789abcdef"
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
    credential_file = tmp_path / "login-credentials.json"
    credential_file.write_text(json.dumps({"sysadmin-01": f"{salt.hex()}${digest.hex()}"}))
    monkeypatch.setattr(server, "LOGIN_CREDENTIALS_FILE", credential_file)
    fake_valkey = FakeValkey()
    monkeypatch.setattr(server, "get_valkey", lambda: fake_valkey)
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for path in ("/login", "/auth/login", "/api/v1/auth/login"):
            response = await client.post(path, json={"username": "sysadmin-01", "password": password})
            assert response.status_code == 200
            assert response.json()["session_id"]
        rejected = await client.post("/auth/login", json={"username": "sysadmin-01", "password": "pass-01"})
        assert rejected.status_code == 401
