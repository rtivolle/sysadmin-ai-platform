"""quota_router: admin quota-scope API (router unit tests)."""
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from services.control_store.quota_scopes import QuotaScopes
from services.fleet import quota_router
from test_quota_scopes import QuotaScopesTableExecutor


@pytest.fixture()
def client(monkeypatch):
    fake = QuotaScopesTableExecutor()
    monkeypatch.setattr(quota_router, "quota_scopes", QuotaScopes(fake))
    monkeypatch.setattr(quota_router, "require_admin", lambda request: "admin")
    monkeypatch.setattr(quota_router, "_audit", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(quota_router.router)
    return TestClient(app)


def test_crud_limits_roundtrip(client):
    put = client.put("/api/v1/quotas/team/alpha",
                     json={"limits": {"daily_tokens": 5000}})
    assert put.status_code == 200, put.text
    assert put.json()["limits"] == {"daily_tokens": 5000}

    get = client.get("/api/v1/quotas/team/alpha")
    assert get.status_code == 200
    body = get.json()
    assert body["limits"] == {"daily_tokens": 5000}
    assert body["usage"]["tokens"] == 0

    delete = client.delete("/api/v1/quotas/team/alpha")
    assert delete.status_code == 200
    assert delete.json()["deleted"] is True
    assert client.get("/api/v1/quotas/team/alpha").status_code == 404
    assert client.delete("/api/v1/quotas/team/alpha").status_code == 404


def test_invalid_scope_type_is_400(client):
    response = client.put("/api/v1/quotas/org/alpha",
                          json={"limits": {"daily_tokens": 5}})
    assert response.status_code == 400


def test_invalid_limits_are_400(client):
    response = client.put("/api/v1/quotas/team/alpha",
                          json={"limits": {"daily_tokens": 0}})
    assert response.status_code == 400
    response = client.put("/api/v1/quotas/team/alpha",
                          json={"limits": {"bogus": 5}})
    assert response.status_code == 400


def test_usage_and_chargeback(client):
    client.put("/api/v1/quotas/team/alpha", json={"limits": {"daily_tokens": 1000}})
    usage = client.get("/api/v1/quotas/team/alpha/usage")
    assert usage.status_code == 200
    assert usage.json()["tokens"] == 0

    chargeback = client.get("/api/v1/quotas/chargeback")
    assert chargeback.status_code == 200
    assert chargeback.json()["totals"]["tokens"] == 0


def test_check_admitted_is_200(client):
    client.put("/api/v1/quotas/team/alpha", json={"limits": {"daily_tokens": 1000}})
    response = client.post("/api/v1/quotas/check",
                           json={"scope_type": "team", "scope_id": "alpha",
                                 "tokens": 100})
    assert response.status_code == 200
    assert response.json()["admitted"] is True


def test_check_exhausted_is_429_with_retry_after(client):
    client.put("/api/v1/quotas/team/alpha", json={"limits": {"daily_tokens": 100}})
    fake_scopes = quota_router.quota_scopes
    fake_scopes.record_usage("team", "alpha", 100)
    response = client.post("/api/v1/quotas/check",
                           json={"scope_type": "team", "scope_id": "alpha",
                                 "tokens": 1})
    assert response.status_code == 429
    assert "retry-after" in response.headers
    assert int(response.headers["retry-after"]) > 0
    body = response.json()
    assert body["admitted"] is False
    assert body["used"] == 100


def test_no_store_is_503(monkeypatch):
    monkeypatch.setattr(quota_router, "quota_scopes", None)
    monkeypatch.setattr(quota_router, "open_quota_scopes", lambda *a, **k: None)
    monkeypatch.setattr(quota_router, "require_admin", lambda request: "admin")
    app = FastAPI()
    app.include_router(quota_router.router)
    client = TestClient(app)
    assert client.get("/api/v1/quotas/team/alpha").status_code == 503
    response = client.post("/api/v1/quotas/check",
                           json={"scope_type": "team", "scope_id": "a"})
    assert response.status_code == 503


def test_store_outage_is_503_not_500(client, monkeypatch):
    fake = QuotaScopesTableExecutor()
    fake.fail_with = OSError("postgres down")
    monkeypatch.setattr(quota_router, "quota_scopes", QuotaScopes(fake))
    assert client.get("/api/v1/quotas/team/alpha").status_code == 503
    response = client.post("/api/v1/quotas/check",
                           json={"scope_type": "team", "scope_id": "a"})
    assert response.status_code == 503


def test_non_admin_is_403(monkeypatch):
    def deny(request):
        raise HTTPException(status_code=403, detail="Administrator required")

    monkeypatch.setattr(quota_router, "require_admin", deny)
    monkeypatch.setattr(quota_router, "quota_scopes",
                        QuotaScopes(QuotaScopesTableExecutor()))
    app = FastAPI()
    app.include_router(quota_router.router)
    client = TestClient(app)
    assert client.get("/api/v1/quotas/team/alpha").status_code == 403
