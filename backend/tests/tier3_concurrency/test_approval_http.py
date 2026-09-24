"""The API enforces reviewer identity and consumes approved commands."""
import httpx
import pytest

from services.agent_tools import server
from services.agent_tools.approval_gate import PENDING_APPROVALS
from services.auth_gateway import server as auth_gateway


@pytest.mark.asyncio
async def test_reviewer_role_and_exact_execution(monkeypatch):
    executions = []
    monkeypatch.setattr(server, "execute_sandboxed_command", lambda workspace, command: (executions.append(command) or (0, "ok", "")))
    monkeypatch.setattr(server, "log_audit_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(auth_gateway, "load_valid_tokens", lambda: {"user-token": "sysadmin-01", "admin-token": "sysadmin-admin"})
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        user_headers = {"Authorization": "Bearer user-token", "X-Forwarded-User": "sysadmin-admin", "X-Forwarded-Role": "admin"}
        params = {"name": "sandboxed_bash", "session_id": "session-a", "parameters": {"command": "touch /tmp/test"}}
        spoofed = await client.post("/api/tools/execute", json=params, headers={"X-Forwarded-User": "sysadmin-admin", "X-Forwarded-Role": "admin"})
        assert spoofed.status_code == 401
        request = await client.post("/api/tools/execute", json=params, headers=user_headers)
        assert request.status_code == 202
        approval_id = request.json()["approval_id"]
        assert not executions

        decision = {"approval_id": approval_id, "approved": True}
        assert (await client.post("/api/approvals/decide", json=decision, headers=user_headers)).status_code == 403
        admin_headers = {"Authorization": "Bearer admin-token", "X-Forwarded-User": "sysadmin-01", "X-Forwarded-Role": "sysadmin"}
        assert (await client.post("/api/approvals/decide", json=decision, headers=admin_headers)).status_code == 200

        wrong_command = {**params, "parameters": {"command": "touch /tmp/other", "approval_id": approval_id}}
        assert (await client.post("/api/tools/execute", json=wrong_command, headers=user_headers)).status_code == 403
        approved = {**params, "parameters": {"command": "touch /tmp/test", "approval_id": approval_id}}
        assert (await client.post("/api/tools/execute", json=approved, headers=user_headers)).status_code == 200
        assert executions == ["touch /tmp/test"]
        assert PENDING_APPROVALS[approval_id]["status"] == "consumed"
        assert (await client.post("/api/tools/execute", json=approved, headers=user_headers)).status_code == 403


@pytest.mark.asyncio
async def test_agent_router_ignores_forwarded_identity(monkeypatch):
    monkeypatch.setattr(auth_gateway, "load_valid_tokens", lambda: {"user-token": "sysadmin-01"})
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        spoofed = await client.get("/api/v1/agent/sessions", headers={"X-Forwarded-User": "sysadmin-admin"})
        assert spoofed.status_code == 401
        authenticated = await client.get(
            "/api/v1/agent/sessions",
            headers={"Authorization": "Bearer user-token", "X-Forwarded-User": "sysadmin-admin"},
        )
        assert authenticated.status_code == 200
        assert authenticated.json()["user_id"] == "sysadmin-01"


@pytest.mark.asyncio
async def test_agent_router_accepts_valid_session_cookie(monkeypatch):
    class SessionStore:
        def get(self, key):
            return "sysadmin-02" if key == "session:valid-cookie" else None

    monkeypatch.setattr(auth_gateway, "get_valkey", lambda: SessionStore())
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set("session_id", "valid-cookie")
        response = await client.get(
            "/api/v1/agent/sessions",
            headers={"X-Forwarded-User": "sysadmin-admin"},
        )
        assert response.status_code == 200
        assert response.json()["user_id"] == "sysadmin-02"


@pytest.mark.asyncio
async def test_target_adapter_routes_bind_identity_and_reviewer(monkeypatch, tmp_path):
    from services.agent_runtime import workspace as workspace_module
    from services.target_adapter import router as target_router_module
    from services.target_adapter.models import ProposalResponse, ExecutionResponse, AdapterStatusResponse

    monkeypatch.setattr(workspace_module, "WORKSPACES_DIR", tmp_path / "workspaces")
    monkeypatch.setattr(auth_gateway, "load_valid_tokens", lambda: {
        "user-token": "sysadmin-01", "other-token": "sysadmin-02", "admin-token": "sysadmin-admin"
    })

    calls = []

    class FakeGate:
        def decide(self, **kwargs):
            calls.append(("decide", kwargs))
            return {"success": True, "status": "approved", "approval": {"decided_by": kwargs["reviewer"], "decided_at": 1.0}}

    class FakeAdapter:
        gate = FakeGate()

        def propose(self, req):
            calls.append(("propose", req))
            return ProposalResponse(approval_id="appr-one", status="pending", action=req.action,
                                    target=req.target, content_hash="hash", created_at=1.0,
                                    expires_at=2.0, message="pending")

        def execute(self, req):
            calls.append(("execute", req))
            return ExecutionResponse(approval_id=req.approval_id, status="succeeded", exit_code=0, message="ok")

        def get_status(self, approval_id):
            return AdapterStatusResponse(approval_id=approval_id, status="pending",
                                         record={"user_id": "sysadmin-01", "status": "pending"})

    monkeypatch.setattr(target_router_module, "get_target_adapter", lambda: FakeAdapter())
    transport = httpx.ASGITransport(app=server.app)
    proposal = {"user_id": "sysadmin-01", "action": "service_restart", "target": "nginx", "workspace": "/tmp/forged"}
    decision = {"approval_id": "appr-one", "approved": True, "reviewer": "sysadmin-admin", "reviewer_role": "admin"}
    execution = {"approval_id": "appr-one", "user_id": "sysadmin-01"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post("/api/v1/approval/propose", json=proposal)).status_code == 401
        assert (await client.post("/api/v1/approval/propose", json=proposal,
                                  headers={"Authorization": "Bearer other-token"})).status_code == 403
        good = await client.post("/api/v1/approval/propose", json=proposal,
                                 headers={"Authorization": "Bearer user-token"})
        assert good.status_code == 202
        assert calls[-1][1].workspace == str(tmp_path / "workspaces" / "sysadmin-01")
        assert (await client.post("/api/v1/approvals/decide", json=decision,
                                  headers={"Authorization": "Bearer user-token"})).status_code == 403
        assert (await client.post("/api/v1/approvals/decide", json=decision,
                                  headers={"Authorization": "Bearer admin-token"})).status_code == 200
        assert calls[-1][1]["reviewer"] == "sysadmin-admin"
        assert (await client.post("/api/v1/adapter/execute", json=execution,
                                  headers={"Authorization": "Bearer other-token"})).status_code == 403
        assert (await client.post("/api/v1/adapter/execute", json=execution,
                                  headers={"Authorization": "Bearer user-token"})).status_code == 200
        assert (await client.get("/api/v1/adapter/status/appr-one",
                                 headers={"Authorization": "Bearer other-token"})).status_code == 404
        assert (await client.get("/api/v1/adapter/status/appr-one",
                                 headers={"Authorization": "Bearer user-token"})).status_code == 200

    assert [name for name, _ in calls] == ["propose", "decide", "execute"]
