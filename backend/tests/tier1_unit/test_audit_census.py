"""
Tier 1 Unit Test: Audit Completeness Census.

Pins the event-by-event audit census recorded in docs/status/AUDIT_CENSUS.md:
every enumerable user- or agent-triggerable action/outcome path must emit a
schema-conformant audit event (VictoriaLogs first, durable outbox on outage).

Two proof layers:
  1. Writer layer: log_audit_event() spools the canonical field set.
  2. Path layer: each dispatcher/gateway path calls the writer with the kwargs
     that populate the required schema fields, for success AND failure outcomes.

Paths documented as still-unaudited gaps carry "gap sentinel" tests that
assert NO event is emitted today. If a sentinel fails, the gap was closed:
convert the sentinel into a positive assertion and update AUDIT_CENSUS.md.
"""
import json
import re
import subprocess
import uuid
import asyncio
import importlib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

import backend.services.agent_tools.audit as audit_backend
import services.agent_tools.audit as audit_services
from backend.services.agent_runtime import react_loop, tool_registry
from backend.services.agent_runtime.models import AgentChatRequest
from backend.services.agent_runtime.session_store import SessionStore
from backend.services.agent_runtime.tool_registry import AVAILABLE_TOOLS
from backend.services.agent_tools import server as agent_server
from backend.services.auth_gateway import p1_elevation
from backend.services.auth_gateway import quota_manager as quota_module
from backend.services.auth_gateway import server as auth_server
from backend.services.target_adapter import adapter as adapter_module
from backend.services.target_adapter.adapter import TargetAdapter
from backend.services.target_adapter.models import ExecutionRequest, ProposalRequest
from services.auth_gateway import server as auth_gateway_srv

# Canonical audit schema (backend writer and JS writer must both produce it).
CANONICAL_FIELDS = {
    "event_id", "timestamp", "service", "user_id", "session_id", "action",
    "tool_name", "parameters", "command", "human_approved", "approval_id",
    "exit_code", "duration_ms", "priority", "incident_id", "prompt_tokens",
    "completion_tokens", "tokens_prompt", "tokens_completion",
}
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

REPO_ROOT = Path(__file__).resolve().parents[3]
VENV_PYTHON = REPO_ROOT / "backend" / ".venv" / "bin" / "python3"


def assert_event_schema(event):
    """The durable record carries the canonical schema (extra is optional)."""
    missing = CANONICAL_FIELDS - set(event)
    assert not missing, f"audit event missing fields: {missing}"
    unexpected = set(event) - CANONICAL_FIELDS - {"extra"}
    assert not unexpected, f"unexpected audit fields: {unexpected}"
    assert event["service"] == "dsh-agent"
    assert TIMESTAMP_RE.match(event["timestamp"]), event["timestamp"]
    uuid.UUID(event["event_id"])
    assert isinstance(event["parameters"], dict)
    assert isinstance(event["human_approved"], bool)
    assert isinstance(event["exit_code"], int)
    assert event["tokens_prompt"] == event["prompt_tokens"]
    assert event["tokens_completion"] == event["completion_tokens"]


class AuditSpool:
    """Routes audited emissions to a throwaway outbox and records call kwargs.

    Both import instances of the audit module (services.* and
    backend.services.*) exist in the test process because service modules
    import through different roots; patch both so no path can leak a real
    VictoriaLogs post or write the production outbox.
    """

    def __init__(self, monkeypatch, tmp_path):
        self.outbox = tmp_path / "census-outbox.jsonl"
        self.calls = []
        for module in {audit_backend, audit_services}:
            monkeypatch.setattr(module, "_post_event", lambda event: False)
            monkeypatch.setattr(module, "OUTBOX_PATH", str(self.outbox))
        self._real_writer = audit_backend.log_audit_event
        self._monkeypatch = monkeypatch

    def capture(self, *args, **kwargs):
        """Drop-in replacement for log_audit_event bound to the real writer."""
        self.calls.append(kwargs)
        return self._real_writer(*args, **kwargs)

    def install(self, module):
        self._monkeypatch.setattr(module, "log_audit_event", self.capture)

    def events(self):
        if not self.outbox.exists():
            return []
        return [json.loads(line) for line in self.outbox.read_text().splitlines() if line.strip()]


@pytest.fixture
def audit_spool(monkeypatch, tmp_path):
    return AuditSpool(monkeypatch, tmp_path)


# ---------------------------------------------------------------------------
# 1. Writer layer: canonical schema, outbox durability, bounded parameters
# ---------------------------------------------------------------------------

def test_writer_spools_canonical_schema(audit_spool):
    """A fully populated call produces exactly the canonical field set."""
    res = audit_backend.log_audit_event(
        user_id="sysadmin-01",
        session_id="sess-census",
        tool_name="search_log_stream",
        action="search_log_stream",
        parameters={"target": "nginx_error.log", "pattern": "502"},
        command="",
        human_approved=False,
        approval_id="appr-census",
        exit_code=0,
        duration_ms=12,
        prompt_tokens=100,
        completion_tokens=40,
        priority="standard",
        incident_id="INC-1",
        extra={"k": "v"},
    )
    assert res == {"logged": True, "destination": "outbox"}
    events = audit_spool.events()
    assert len(events) == 1
    event = events[0]
    assert set(event) == CANONICAL_FIELDS | {"extra"}
    assert_event_schema(event)
    assert event["approval_id"] == "appr-census"
    assert event["incident_id"] == "INC-1"
    assert event["extra"] == {"k": "v"}


def test_writer_minimal_call_has_full_schema(audit_spool):
    """Even a minimal call (no optional kwargs) leaves no outbox field missing."""
    audit_backend.log_audit_event("sysadmin-02", "sess-min", "doc_runbook_reader", priority="standard")
    (event,) = audit_spool.events()
    assert set(event) == CANONICAL_FIELDS  # no "extra" when none was given
    assert_event_schema(event)
    assert event["action"] == "doc_runbook_reader"
    assert event["tool_name"] == "doc_runbook_reader"
    assert event["command"] == ""
    assert event["approval_id"] is None


def test_writer_bounds_proposed_content(audit_spool):
    """The writer truncates oversized proposed_content exactly like the JS writer."""
    big = "x" * 900
    audit_backend.log_audit_event(
        "sysadmin-01", "sess-trunc", "config_lint_and_diff",
        parameters={"target_file": "a.json", "proposed_content": big},
        priority="standard",
    )
    (event,) = audit_spool.events()
    truncated = event["parameters"]["proposed_content"]
    assert len(truncated) < len(big)
    assert truncated.endswith("... [truncated 900 bytes]")


# ---------------------------------------------------------------------------
# 2. Registry dispatcher census: every registered tool, success AND failure
# ---------------------------------------------------------------------------

@pytest.fixture
def registry_env(monkeypatch, audit_spool, tmp_path):
    """Capture registry audit emissions; sandbox and workspace are faked."""
    audit_spool.install(tool_registry)
    monkeypatch.setattr(
        tool_registry, "execute_sandboxed_command",
        lambda workspace, command: (0, "ok", ""),
    )
    workspace = tmp_path / "ws" / "sysadmin-01"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(tool_registry, "ensure_workspace", lambda user_id: str(workspace))
    return str(workspace)


def _registry_fixtures(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("info boot\nerror boom\n")
    rb = tmp_path / "runbook.md"
    rb.write_text("# Recovery\nRestart the service.\n")
    return log, rb


def test_every_registered_tool_audits_success_and_failure(registry_env, audit_spool, tmp_path):
    """Census driver: AVAILABLE_TOOLS is fully enumerated, two outcomes each."""
    log, rb = _registry_fixtures(tmp_path)
    scenarios = {
        "search_log_stream": (
            {"target": str(log), "pattern": "error"},
            {"target": str(tmp_path / "missing.log"), "pattern": "error"},
        ),
        "config_lint_and_diff": (
            {"target_file": str(tmp_path / "cfg.json"), "proposed_content": "{\"a\": 1}"},
            {"target_file": str(tmp_path / "cfg.json"), "proposed_content": "{\"a\": "},
        ),
        "doc_runbook_reader": (
            {"runbook_path": str(rb), "section_title": "Recovery"},
            {"runbook_path": str(tmp_path / "missing.md"), "section_title": "Recovery"},
        ),
        "sandboxed_bash": (
            {"command": "ls"},
            {"command": "rm -rf /"},
        ),
    }
    assert set(scenarios) == set(AVAILABLE_TOOLS), "registry census drift: a tool lacks a scenario"

    for tool_name in AVAILABLE_TOOLS:
        ok_params, bad_params = scenarios[tool_name]
        before = len(audit_spool.events())

        ok_res = tool_registry.execute_tool_call(
            tool_name, ok_params, "sysadmin-01", "sess-census", registry_env,
        )
        assert ok_res["status"] in {"success", "blocked"}  # rm -rf / is the bounded "failure" for bash
        bad_res = tool_registry.execute_tool_call(
            tool_name, bad_params, "sysadmin-01", "sess-census", registry_env,
        )

        new_events = audit_spool.events()[before:]
        assert len(new_events) == 2, f"{tool_name}: expected 2 audit events, got {len(new_events)}"
        for event in new_events:
            assert_event_schema(event)
            assert event["action"] == tool_name
            assert event["tool_name"] == tool_name
            assert event["user_id"] == "sysadmin-01"
            assert event["session_id"] == "sess-census"
        ok_event, bad_event = new_events
        if tool_name == "sandboxed_bash":
            assert ok_event["exit_code"] == 0
            assert bad_event["exit_code"] == 126 and bad_event["extra"]["blocked"] is True
            assert bad_res["status"] == "blocked"
        else:
            assert ok_event["exit_code"] == 0, f"{tool_name} success event: {ok_event}"
            assert bad_event["exit_code"] != 0, f"{tool_name} failure must be non-zero: {bad_event}"


def test_registry_approval_request_is_audited(registry_env, audit_spool):
    res = tool_registry.execute_tool_call(
        "sandboxed_bash", {"command": "systemctl restart nginx"},
        "sysadmin-01", "sess-hitl", registry_env,
    )
    assert res["status"] == "approval_required"
    (event,) = audit_spool.events()
    assert_event_schema(event)
    assert event["extra"]["approval_required"] is True
    assert event["approval_id"] == res["approval_id"]
    assert event["command"] == "systemctl restart nginx"


def test_registry_invalid_approval_denial_is_audited(registry_env, audit_spool):
    """Replayed/expired/mismatched approval IDs are security denials: they must audit."""
    res = tool_registry.execute_tool_call(
        "sandboxed_bash", {"command": "systemctl restart nginx", "approval_id": "appr-forged"},
        "sysadmin-01", "sess-hitl", registry_env,
    )
    assert res["status"] == "blocked"
    (event,) = audit_spool.events()
    assert_event_schema(event)
    assert event["exit_code"] == 126
    assert event["approval_id"] == "appr-forged"
    assert event["extra"]["approval_denied"] is True


def test_registry_workspace_mismatch_is_audited(registry_env, audit_spool, tmp_path):
    res = tool_registry.execute_tool_call(
        "sandboxed_bash", {"command": "ls"}, "sysadmin-01", "sess-ws", str(tmp_path / "elsewhere"),
    )
    assert res["status"] == "error"
    (event,) = audit_spool.events()
    assert_event_schema(event)
    assert event["exit_code"] == 126
    assert event["extra"]["blocked"] is True
    assert "Workspace" in event["extra"]["reason"]


def test_registry_unknown_tool_is_audited(registry_env, audit_spool):
    res = tool_registry.execute_tool_call(
        "nmap", {"command": "nmap -sS target"}, "sysadmin-01", "sess-unk", registry_env,
    )
    assert res["status"] == "error"
    (event,) = audit_spool.events()
    assert_event_schema(event)
    assert event["exit_code"] == 127
    assert event["extra"]["unknown_tool"] is True


def test_registry_dispatcher_exception_is_audited(registry_env, audit_spool, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("census fault injection")

    monkeypatch.setattr(tool_registry, "search_log_stream", boom)
    res = tool_registry.execute_tool_call(
        "search_log_stream", {"target": "x", "pattern": "y"}, "sysadmin-01", "sess-exc", registry_env,
    )
    assert res["status"] == "error"
    (event,) = audit_spool.events()
    assert_event_schema(event)
    assert event["exit_code"] == 1
    assert "census fault injection" in event["extra"]["error"]



# ---------------------------------------------------------------------------
# 3. HTTP dispatcher census: /api/tools/execute (direct tool calls)
# ---------------------------------------------------------------------------

@pytest.fixture
def http_env(monkeypatch, audit_spool, tmp_path):
    audit_spool.install(agent_server)
    monkeypatch.setattr(
        auth_gateway_srv, "load_valid_tokens",
        lambda: {"user-token": "sysadmin-01", "admin-token": "sysadmin-admin"},
    )
    monkeypatch.setattr(
        agent_server, "execute_sandboxed_command",
        lambda workspace, command: (0, "ok", ""),
    )
    workspace = tmp_path / "ws" / "sysadmin-01"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(agent_server, "ensure_workspace", lambda user_id: str(workspace))
    return {"Authorization": "Bearer user-token"}


async def _post_execute(client, headers, name, parameters):
    return await client.post(
        "/api/tools/execute",
        json={"name": name, "session_id": "sess-http", "parameters": parameters},
        headers=headers,
    )


@pytest.mark.asyncio
async def test_http_tool_success_is_audited(http_env, audit_spool, tmp_path):
    log = tmp_path / "http.log"
    log.write_text("error here\n")
    transport = httpx.ASGITransport(app=agent_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await _post_execute(client, http_env, "search_log_stream", {"target": str(log), "pattern": "error"})
    assert resp.status_code == 200
    (event,) = audit_spool.events()
    assert_event_schema(event)
    assert event["action"] == "search_log_stream"
    assert event["exit_code"] == 0


@pytest.mark.asyncio
async def test_http_blocked_command_is_audited(http_env, audit_spool):
    transport = httpx.ASGITransport(app=agent_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await _post_execute(client, http_env, "sandboxed_bash", {"command": "rm -rf /"})
    assert resp.status_code == 403
    (event,) = audit_spool.events()
    assert_event_schema(event)
    assert event["exit_code"] == 126
    assert event["extra"]["blocked"] is True


@pytest.mark.asyncio
async def test_http_approval_request_is_audited(http_env, audit_spool):
    """The 202 approval interception must audit like the runtime dispatcher does."""
    transport = httpx.ASGITransport(app=agent_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await _post_execute(client, http_env, "sandboxed_bash", {"command": "systemctl restart nginx"})
    assert resp.status_code == 202
    approval_id = resp.json()["approval_id"]
    (event,) = audit_spool.events()
    assert_event_schema(event)
    assert event["extra"]["approval_required"] is True
    assert event["approval_id"] == approval_id


@pytest.mark.asyncio
async def test_http_invalid_approval_denial_is_audited(http_env, audit_spool):
    transport = httpx.ASGITransport(app=agent_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await _post_execute(
            client, http_env, "sandboxed_bash",
            {"command": "systemctl restart nginx", "approval_id": "appr-forged"},
        )
    assert resp.status_code == 403
    (event,) = audit_spool.events()
    assert_event_schema(event)
    assert event["exit_code"] == 126
    assert event["approval_id"] == "appr-forged"
    assert event["extra"]["approval_denied"] is True


@pytest.mark.asyncio
async def test_http_approved_execution_is_audited(http_env, audit_spool, monkeypatch):
    monkeypatch.setattr(agent_server, "consume_approval", lambda *args, **kwargs: True)
    transport = httpx.ASGITransport(app=agent_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await _post_execute(
            client, http_env, "sandboxed_bash",
            {"command": "systemctl restart nginx", "approval_id": "appr-ok"},
        )
    assert resp.status_code == 200
    (event,) = audit_spool.events()
    assert_event_schema(event)
    assert event["human_approved"] is True
    assert event["exit_code"] == 0


@pytest.mark.asyncio
async def test_http_unknown_tool_is_audited(http_env, audit_spool):
    transport = httpx.ASGITransport(app=agent_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await _post_execute(client, http_env, "nmap", {"command": "nmap -sS target"})
    assert resp.status_code == 404
    (event,) = audit_spool.events()
    assert_event_schema(event)
    assert event["exit_code"] == 127
    assert event["extra"]["unknown_tool"] is True


@pytest.mark.asyncio
async def test_http_approval_decision_is_audited(http_env, audit_spool):
    """The agent-platform decide endpoint audits approve AND reject outcomes."""
    transport = httpx.ASGITransport(app=agent_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        request = await _post_execute(client, http_env, "sandboxed_bash", {"command": "systemctl restart nginx"})
        approval_id = request.json()["approval_id"]
        for approved in (True, False):
            if not approved:
                # Re-deciding the same record fails, so mint a second request.
                request = await _post_execute(client, http_env, "sandboxed_bash", {"command": "systemctl reload nginx"})
                approval_id = request.json()["approval_id"]
            resp = await client.post(
                "/api/approvals/decide",
                json={"approval_id": approval_id, "approved": approved},
                headers={"Authorization": "Bearer admin-token"},
            )
            assert resp.status_code == 200
    decision_events = [e for e in audit_spool.events() if e["tool_name"] == "approval_decision"]
    assert len(decision_events) == 2
    for event, expected in zip(decision_events, (True, False)):
        assert_event_schema(event)
        assert event["user_id"] == "sysadmin-admin"
        assert event["human_approved"] is expected
        assert event["extra"]["requester"] == "sysadmin-01"
    assert decision_events[0]["extra"]["approval_decision"] == "approved"
    assert decision_events[1]["extra"]["approval_decision"] == "rejected"


# ---------------------------------------------------------------------------
# 4. Target adapter census: proposals, decisions, executions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_adapter_router_decision_is_audited(monkeypatch, audit_spool, tmp_path):
    """The /api/v1/approvals/decide route must audit like /api/approvals/decide."""
    from services.agent_runtime import workspace as workspace_module
    from services.target_adapter import router as target_router_services
    from services.target_adapter.models import AdapterStatusResponse

    audit_spool.install(target_router_services)
    monkeypatch.setattr(workspace_module, "WORKSPACES_DIR", tmp_path / "workspaces")
    monkeypatch.setattr(
        auth_gateway_srv, "load_valid_tokens",
        lambda: {"user-token": "sysadmin-01", "admin-token": "sysadmin-admin"},
    )

    class FakeGate:
        def decide(self, **kwargs):
            return {
                "success": True,
                "status": "approved" if kwargs["approved"] else "rejected",
                "approval": {
                    "decided_by": kwargs["reviewer"],
                    "decided_at": 1.0,
                    "session_id": "sess-adapter",
                    "command": "systemctl restart nginx.service",
                    "user_id": "sysadmin-01",
                },
            }

    class FakeAdapter:
        gate = FakeGate()

        def get_status(self, approval_id):
            return AdapterStatusResponse(approval_id=approval_id, status="pending", record={})

    monkeypatch.setattr(target_router_services, "get_target_adapter", lambda: FakeAdapter())
    transport = httpx.ASGITransport(app=agent_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for approved in (True, False):
            resp = await client.post(
                "/api/v1/approvals/decide",
                json={"approval_id": "appr-v1", "approved": approved, "reviewer": "sysadmin-admin", "reviewer_role": "admin"},
                headers={"Authorization": "Bearer admin-token"},
            )
            assert resp.status_code == 200

    events = audit_spool.events()
    assert len(events) == 2
    for event, expected in zip(events, ("approved", "rejected")):
        assert_event_schema(event)
        assert event["tool_name"] == "approval_decision"
        assert event["user_id"] == "sysadmin-admin"
        assert event["session_id"] == "sess-adapter"
        assert event["command"] == "systemctl restart nginx.service"
        assert event["extra"]["approval_decision"] == expected
        assert event["extra"]["requester"] == "sysadmin-01"
        assert event["approval_id"] == "appr-v1"



class _FakeGate:
    """Minimal approval-gate double for adapter unit census tests."""

    def __init__(self):
        self.records = {}
        self.claim_result = {"success": True, "approval": {}}

    def propose(self, **kwargs):
        return {
            "success": True,
            "approval_id": "appr-fake",
            "status": "pending",
            "target": kwargs["target"],
            "action": kwargs["action"],
            "command": kwargs.get("command", ""),
            "content_hash": "h" * 64,
            "base_hash": None,
            "created_at": 1.0,
            "expires_at": 2.0,
            "message": "pending",
        }

    def get_status(self, approval_id):
        return self.records.get(approval_id)

    def claim_execution(self, **kwargs):
        return self.claim_result

    def complete_execution(self, **kwargs):
        return {"success": True}


class _FakeServiceManager:
    def execute_action(self, action, target):
        return 0, f"{action} {target} ok", ""


class _FakeDeployer:
    def deploy(self, **kwargs):
        return {"success": True, "message": "deployed", "backup_path": "/tmp/bak", "rollback_performed": False}


@pytest.fixture
def adapter_env(monkeypatch, audit_spool, tmp_path):
    audit_spool.install(adapter_module)
    gate = _FakeGate()
    adapter = TargetAdapter(
        approval_gate=gate,
        service_manager=_FakeServiceManager(),
        config_deployer=_FakeDeployer(),
    )
    return adapter, gate


def test_adapter_proposal_is_audited(adapter_env, audit_spool, tmp_path):
    adapter, _ = adapter_env
    res = adapter.propose(ProposalRequest(
        user_id="sysadmin-01", action="service_restart", target="nginx",
        session_id="sess-adapter", workspace=str(tmp_path), reason="census",
    ))
    assert res.status == "pending"
    (event,) = audit_spool.events()
    assert_event_schema(event)
    assert event["action"] == "service_restart"
    assert event["tool_name"] == "adapter_service_restart"
    assert event["approval_id"] == "appr-fake"
    assert event["extra"]["approval_required"] is True
    assert event["command"] == "systemctl restart nginx.service"


def test_adapter_service_execution_success_is_audited(adapter_env, audit_spool):
    adapter, gate = adapter_env
    gate.records["appr-1"] = {
        "action": "service_restart", "target": "nginx",
        "command": "systemctl restart nginx.service", "workspace": "",
    }
    res = adapter.execute(ExecutionRequest(approval_id="appr-1", user_id="sysadmin-01"))
    assert res.status == "succeeded"
    (event,) = audit_spool.events()
    assert_event_schema(event)
    assert event["tool_name"] == "adapter_service_restart"
    assert event["action"] == "service_restart"
    assert event["human_approved"] is True
    assert event["exit_code"] == 0


def test_adapter_config_deploy_success_is_audited(adapter_env, audit_spool, tmp_path):
    adapter, gate = adapter_env
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "cfg.json").write_text("{\"v\": 2}")
    target = tmp_path / "cfg.json"
    gate.records["appr-2"] = {
        "action": "config_deploy", "target": str(target),
        "command": f"deploy {target}", "workspace": str(workspace),
        "content_hash": "h", "base_hash": None,
    }
    res = adapter.execute(ExecutionRequest(approval_id="appr-2", user_id="sysadmin-01"))
    assert res.status == "succeeded"
    (event,) = audit_spool.events()
    assert_event_schema(event)
    assert event["tool_name"] == "adapter_config_deploy"
    assert event["action"] == "config_deploy"
    assert event["human_approved"] is True
    assert event["exit_code"] == 0
    assert event["parameters"]["backup_path"] == "/tmp/bak"


def test_adapter_execute_unknown_approval_is_audited(adapter_env, audit_spool):
    adapter, _ = adapter_env
    res = adapter.execute(ExecutionRequest(approval_id="appr-ghost", user_id="sysadmin-01"))
    assert res.status == "failed"
    (event,) = audit_spool.events()
    assert_event_schema(event)
    assert event["tool_name"] == "adapter_execute"
    assert event["exit_code"] == 1
    assert "not found" in event["extra"]["error"]


def test_adapter_execute_claim_failure_is_audited(adapter_env, audit_spool):
    """Consumed/expired/mismatched approval claims are execution denials."""
    adapter, gate = adapter_env
    gate.records["appr-3"] = {"action": "service_restart", "target": "nginx", "command": "c"}
    gate.claim_result = {"success": False, "error": "already consumed"}
    res = adapter.execute(ExecutionRequest(approval_id="appr-3", user_id="sysadmin-01"))
    assert res.status == "failed"
    (event,) = audit_spool.events()
    assert_event_schema(event)
    assert event["action"] == "service_restart"
    assert event["exit_code"] == 1
    assert event["extra"]["blocked"] is True
    assert "already consumed" in event["extra"]["error"]


def test_adapter_execute_missing_staged_file_is_audited(adapter_env, audit_spool, tmp_path):
    adapter, gate = adapter_env
    workspace = tmp_path / "ws-empty"
    workspace.mkdir()
    gate.records["appr-4"] = {
        "action": "config_deploy", "target": str(tmp_path / "gone.json"),
        "command": "deploy gone", "workspace": str(workspace),
    }
    res = adapter.execute(ExecutionRequest(approval_id="appr-4", user_id="sysadmin-01"))
    assert res.status == "failed"
    (event,) = audit_spool.events()
    assert_event_schema(event)
    assert event["action"] == "config_deploy"
    assert event["exit_code"] == 1
    assert "Staged file unavailable" in event["extra"]["error"]


def test_adapter_execute_unknown_action_is_audited(adapter_env, audit_spool):
    adapter, gate = adapter_env
    gate.records["appr-5"] = {"action": "firmware_flash", "target": "bios", "command": "c"}
    res = adapter.execute(ExecutionRequest(approval_id="appr-5", user_id="sysadmin-01"))
    assert res.status == "failed"
    (event,) = audit_spool.events()
    assert_event_schema(event)
    assert event["action"] == "firmware_flash"
    assert event["exit_code"] == 1
    assert "Unknown action" in event["extra"]["error"]


# ---------------------------------------------------------------------------
# 5. P1 elevation census
# ---------------------------------------------------------------------------

def test_p1_elevation_issue_and_revoke_are_audited(monkeypatch, audit_spool):
    audit_spool.install(p1_elevation)
    gate = p1_elevation.P1EmergencyGate()
    token = gate.issue_p1_token(
        on_call_user="sysadmin-03", incident_id="INC-census",
        ttl_seconds=600, reason="census",
    )
    assert token.startswith("p1-token-INC-census-")
    gate.revoke_p1_elevation("sysadmin-03", reason="census done")

    events = audit_spool.events()
    assert len(events) == 2
    issued, revoked = events
    for event in (issued, revoked):
        assert_event_schema(event)
    assert issued["action"] == "p1_elevation_issued"
    assert issued["priority"] == "P1-CRITICAL"
    assert issued["incident_id"] == "INC-census"
    assert issued["parameters"]["ttl_seconds"] == 600
    assert revoked["action"] == "p1_elevation_revoked"
    assert revoked["parameters"]["reason"] == "census done"


# ---------------------------------------------------------------------------
# 6. Chat completion census (LiteLLM gateway logging handler)
# ---------------------------------------------------------------------------

def test_litellm_completion_success_is_audited(tmp_path):
    """The LiteLLM gateway success handler audits completions with token counts.

    Runs in a subprocess: importing litellm under pytest segfaults on this
    host (Python 3.14 + pydantic native crash), so the handler is exercised
    by a plain interpreter, matching the JS parity-test convention.
    """
    outbox = tmp_path / "litellm-outbox.jsonl"
    script = f"""
import asyncio
from types import SimpleNamespace
import backend.services.agent_tools.audit as audit
audit._post_event = lambda event: False
audit.OUTBOX_PATH = {str(outbox)!r}
from backend.services.auth_gateway import litellm_auth
litellm_auth.quota_mgr = SimpleNamespace(
    record_token_consumption=lambda *args: None,
    settle_daily_token_reservation=lambda *args: 0,
)
handler = litellm_auth.QuotaLoggingHandler()
kwargs = {{"user": "sysadmin-01", "litellm_params": {{"metadata": {{"session_id": "sess-llm"}}}}}}
resp = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=12, completion_tokens=7), id="resp-census")
asyncio.run(handler.async_log_success_event(kwargs, resp, None, None))
"""
    proc = subprocess.run(
        [str(VENV_PYTHON), "-c", script],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, f"subprocess failed:\n{proc.stderr[-2000:]}"

    (event,) = [json.loads(line) for line in outbox.read_text().splitlines() if line.strip()]
    assert_event_schema(event)
    assert event["tool_name"] == "litellm_completion"
    assert event["action"] == "litellm_completion"
    assert event["user_id"] == "sysadmin-01"
    assert event["session_id"] == "sess-llm"
    assert event["prompt_tokens"] == 12
    assert event["completion_tokens"] == 7
    assert event["extra"]["response_id"] == "resp-census"



# ---------------------------------------------------------------------------
# 7. Former gap sentinels — now positive assertions.
# G1/G3/G4/G5/G6 were closed (see docs/status/AUDIT_CENSUS.md §3). Each former
# "absence" test is replaced by a positive success/failure assertion below.
# ---------------------------------------------------------------------------


# --- G1: runtime agent_turn -------------------------------------------------

@pytest.mark.asyncio
async def test_g1_agent_turn_success_is_audited(monkeypatch, audit_spool):
    """GAP G1 (closed): a completed ReAct turn emits an agent_turn event."""
    audit_spool.install(react_loop)

    async def fake_llm(messages, model, user_id, session_id=None):
        return "Thought: I have enough.\nFinal Answer: nothing to do."

    monkeypatch.setattr(react_loop, "call_llm", fake_llm)
    store = SessionStore()
    session = await store.create_or_get_session(user_id="sysadmin-01", session_id="sess-g1-ok")
    resp = await react_loop.run_react_agent(
        AgentChatRequest(prompt="status?", model="fast-model"), "sysadmin-01", session, store,
    )
    assert "nothing to do" in resp.response

    (event,) = audit_spool.events()
    assert_event_schema(event)
    assert event["tool_name"] == "agent_turn"
    assert event["action"] == "agent_turn"
    assert event["user_id"] == "sysadmin-01"
    assert event["session_id"] == "sess-g1-ok"
    assert event["exit_code"] == 0
    assert event["extra"]["model"] == "fast-model"


@pytest.mark.asyncio
async def test_g1_agent_turn_failure_is_audited(monkeypatch, audit_spool):
    """GAP G1 (closed): a gateway rejection emits a non-zero agent_turn."""
    audit_spool.install(react_loop)

    async def failing_llm(messages, model, user_id, session_id=None):
        req = httpx.Request("POST", "http://litellm/v1/chat/completions")
        resp = httpx.Response(429, request=req)
        raise httpx.HTTPStatusError("rejected", request=req, response=resp)

    monkeypatch.setattr(react_loop, "call_llm", failing_llm)
    store = SessionStore()
    session = await store.create_or_get_session(user_id="sysadmin-01", session_id="sess-g1-fail")
    with pytest.raises(HTTPException) as exc:
        await react_loop.run_react_agent(
            AgentChatRequest(prompt="status?", model="fast-model"), "sysadmin-01", session, store,
        )
    assert exc.value.status_code == 429

    (event,) = audit_spool.events()
    assert_event_schema(event)
    assert event["tool_name"] == "agent_turn"
    assert event["user_id"] == "sysadmin-01"
    assert event["exit_code"] != 0
    assert event["extra"]["http_status"] == 429
    assert event["extra"]["error"]
    assert event["extra"]["model"] == "fast-model"


# --- G3: LiteLLM failure event ----------------------------------------------

def test_litellm_completion_failure_is_audited(tmp_path):
    """GAP G3 (closed): the failure handler audits rejected/errored completions.

    Runs in a subprocess because importing litellm under pytest segfaults on
    this host; mirrors the success-path convention above.
    """
    outbox = tmp_path / "litellm-failure-outbox.jsonl"
    settle_log = tmp_path / "settle-calls.jsonl"
    script = f"""
import asyncio, json
from types import SimpleNamespace
import backend.services.agent_tools.audit as audit
audit._post_event = lambda event: False
audit.OUTBOX_PATH = {str(outbox)!r}
from backend.services.auth_gateway import litellm_auth

def record_settle(*args):
    with open({str(settle_log)!r}, "a") as f:
        f.write(json.dumps(list(args)) + "\\n")
    return 0

litellm_auth.quota_mgr = SimpleNamespace(
    record_token_consumption=lambda *args: None,
    settle_daily_token_reservation=record_settle,
)
handler = litellm_auth.QuotaLoggingHandler()
kwargs = {{"user": "sysadmin-01", "litellm_params": {{"metadata": {{"session_id": "sess-llm-fail", "quota_reservation_id": "res-fail", "quota_admission_day": "2026-09-24"}}}}}}
resp = SimpleNamespace(status_code=429, id="resp-fail")
asyncio.run(handler.async_log_failure_event(kwargs, resp, None, None))
"""
    proc = subprocess.run(
        [str(VENV_PYTHON), "-c", script],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, f"subprocess failed:\n{proc.stderr[-2000:]}"

    (event,) = [json.loads(line) for line in outbox.read_text().splitlines() if line.strip()]
    assert_event_schema(event)
    assert event["tool_name"] == "litellm_completion_failure"
    assert event["action"] == "litellm_completion_failure"
    assert event["user_id"] == "sysadmin-01"
    assert event["session_id"] == "sess-llm-fail"
    assert event["exit_code"] != 0
    assert event["extra"]["error_type"] == "http_429"
    assert event["extra"]["response_id"] == "resp-fail"

    settle_calls = [json.loads(line) for line in settle_log.read_text().splitlines() if line.strip()]
    assert len(settle_calls) == 1, "failure path must settle the reservation exactly once (no leaked slot)"
    assert settle_calls[0][:3] == ["sysadmin-01", "res-fail", "2026-09-24"]
    assert settle_calls[0][3] == 0 and settle_calls[0][4] == 0


# --- G4: quota denials ------------------------------------------------------

def test_g4_quota_denial_litellm_is_audited(monkeypatch, audit_spool):
    """GAP G4 (closed): concurrency + daily-reservation denials audit quota_denied."""
    audit_spool.install(quota_module)
    monkeypatch.delenv("VALKEY_URL", raising=False)
    manager = quota_module.QuotaManager()
    monkeypatch.setattr(manager, "is_p1_elevated", lambda user: False)
    user = f"quota-deny-{uuid.uuid4().hex[:8]}"

    # Concurrency ceiling (2 for standard).
    manager.acquire_concurrency_slot(user)
    manager.acquire_concurrency_slot(user)
    with pytest.raises(quota_module.QuotaExceededException):
        manager.acquire_concurrency_slot(user)

    # Daily reservation exhaustion.
    manager.reserve_daily_token_budget(user, "res-full", 2_000_000)
    with pytest.raises(quota_module.QuotaExceededException):
        manager.reserve_daily_token_budget(user, "res-over", 1)

    events = audit_spool.events()
    assert len(events) == 2
    concurrency_event, daily_event = events
    for event in events:
        assert_event_schema(event)
        assert event["tool_name"] == "quota_denied"
        assert event["action"] == "quota_denied"
        assert event["user_id"] == user
        assert event["exit_code"] != 0
    assert concurrency_event["extra"]["limit_type"] == "concurrency"
    assert concurrency_event["extra"]["stage"] == "litellm"
    assert daily_event["extra"]["limit_type"] == "daily_tokens"
    assert daily_event["extra"]["stage"] == "litellm"


def test_g4_quota_denial_forwardauth_is_audited(monkeypatch, audit_spool):
    """GAP G4 (closed): the ForwardAuth daily-budget denial audits quota_denied."""
    audit_spool.install(quota_module)

    class _DailyStore:
        def __init__(self, consumed):
            self.consumed = str(consumed)

        def get(self, key):
            if key.startswith("daily_tokens:"):
                return self.consumed
            return None

    manager = quota_module.QuotaManager(redis_client=_DailyStore(2_000_000))
    monkeypatch.setattr(manager, "is_p1_elevated", lambda user: False)
    with pytest.raises(quota_module.QuotaExceededException):
        manager.check_daily_token_budget("sysadmin-01")

    (event,) = audit_spool.events()
    assert_event_schema(event)
    assert event["tool_name"] == "quota_denied"
    assert event["extra"]["limit_type"] == "daily_tokens"
    assert event["extra"]["stage"] == "forwardauth"
    assert event["exit_code"] != 0


# --- G5: auth gateway -------------------------------------------------------

class _FakeSessionStore:
    def __init__(self):
        self.sessions = {}

    def setex(self, key, ttl, value):
        self.sessions[key] = value

    def get(self, key):
        return self.sessions.get(key)

    def delete(self, key):
        self.sessions.pop(key, None)


@pytest.mark.asyncio
async def test_g5_forwardauth_401_emits_auth_denied(monkeypatch, audit_spool):
    audit_spool.install(auth_server)
    monkeypatch.setattr(auth_server, "load_valid_tokens", lambda: {"known-token": "sysadmin-01"})
    transport = httpx.ASGITransport(app=auth_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/verify", headers={"Authorization": "Bearer forged-token"})
    assert resp.status_code == 401
    (event,) = audit_spool.events()
    assert_event_schema(event)
    assert event["tool_name"] == "auth_denied"
    assert event["action"] == "auth_denied"
    assert event["user_id"] == "anonymous"
    assert event["exit_code"] != 0
    assert event["extra"]["reason"] == "invalid_credentials"
    assert "forged-token" not in json.dumps(event)


@pytest.mark.asyncio
async def test_g5_login_success_emits_login_success(monkeypatch, audit_spool):
    audit_spool.install(auth_server)
    store = _FakeSessionStore()
    monkeypatch.setattr(auth_server, "valid_login_password", lambda u, p: True)
    monkeypatch.setattr(auth_server, "get_valkey", lambda: store)
    transport = httpx.ASGITransport(app=auth_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/login", json={"username": "sysadmin-01", "password": "hunter2"})
    assert resp.status_code == 200
    (event,) = audit_spool.events()
    assert_event_schema(event)
    assert event["tool_name"] == "login_success"
    assert event["action"] == "login_success"
    assert event["user_id"] == "sysadmin-01"
    assert event["exit_code"] == 0
    assert "hunter2" not in json.dumps(event)


@pytest.mark.asyncio
async def test_g5_login_failure_emits_login_failure(monkeypatch, audit_spool):
    audit_spool.install(auth_server)
    monkeypatch.setattr(auth_server, "valid_login_password", lambda u, p: False)
    transport = httpx.ASGITransport(app=auth_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/login", json={"username": "sysadmin-01", "password": "wrong-password-xyz"})
    assert resp.status_code == 401
    (event,) = audit_spool.events()
    assert_event_schema(event)
    assert event["tool_name"] == "login_failure"
    assert event["action"] == "login_failure"
    assert event["user_id"] == "anonymous"
    assert event["exit_code"] != 0
    assert event["parameters"]["attempted_user"] == "sysadmin-01"
    assert "wrong-password-xyz" not in json.dumps(event)


@pytest.mark.asyncio
async def test_g5_logout_emits_logout(monkeypatch, audit_spool):
    audit_spool.install(auth_server)
    store = _FakeSessionStore()
    store.sessions["session:tok-123"] = "sysadmin-01"
    monkeypatch.setattr(auth_server, "get_valkey", lambda: store)
    transport = httpx.ASGITransport(app=auth_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/logout", cookies={"session_id": "tok-123"})
    assert resp.status_code == 200
    (event,) = audit_spool.events()
    assert_event_schema(event)
    assert event["tool_name"] == "logout"
    assert event["action"] == "logout"
    assert event["user_id"] == "sysadmin-01"
    assert event["exit_code"] == 0


@pytest.mark.asyncio
async def test_g5_events_never_contain_token_or_password(monkeypatch, audit_spool):
    audit_spool.install(auth_server)
    monkeypatch.setattr(auth_server, "load_valid_tokens", lambda: {"known-token": "sysadmin-01"})
    monkeypatch.setattr(auth_server, "valid_login_password", lambda u, p: False)
    leak_token = "BEARER-LEAK-TOKEN-abc123"
    leak_password = "SUPER-SECRET-PASSWORD-xyz789"
    transport = httpx.ASGITransport(app=auth_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/verify", headers={"Authorization": f"Bearer {leak_token}"})
        await client.post("/login", json={"username": "sysadmin-01", "password": leak_password})

    events = audit_spool.events()
    assert len(events) == 2
    for event in events:
        serialized = json.dumps(event)
        assert leak_token not in serialized
        assert leak_password not in serialized


# --- G6: cancellation -------------------------------------------------------

@pytest.mark.asyncio
async def test_g6_cancel_endpoint_emits_user_cause(monkeypatch, audit_spool):
    services_router = importlib.import_module("services.agent_runtime.router")
    audit_spool.install(services_router)
    monkeypatch.setattr(services_router, "authenticate_request", lambda request: ("sysadmin-01", "bearer"))
    await services_router.registry.register("req-1", "sysadmin-01", session_id="sess-1")
    try:
        transport = httpx.ASGITransport(app=agent_server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/agent/cancel", json={"request_id": "req-1"})
        assert resp.status_code == 200
        assert resp.json()["success"] is True
    finally:
        await services_router.registry.unregister("req-1")

    (event,) = audit_spool.events()
    assert_event_schema(event)
    assert event["tool_name"] == "agent_cancel"
    assert event["action"] == "agent_cancel"
    assert event["user_id"] == "sysadmin-01"
    assert event["extra"]["cause"] == "user"
    assert event["extra"]["request_id"] == "req-1"


@pytest.mark.asyncio
async def test_g6_sse_disconnect_emits_disconnect_cause(monkeypatch, audit_spool):
    services_router = importlib.import_module("services.agent_runtime.router")
    audit_spool.install(services_router)

    class _DisconnectedRequest:
        async def is_disconnected(self):
            return True

    record = SimpleNamespace(cancel_event=asyncio.Event())
    stopped = await services_router._audit_disconnect_if_needed(
        record, _DisconnectedRequest(), "sysadmin-01", "sess-1", "req-1",
    )
    assert stopped is True

    (event,) = audit_spool.events()
    assert_event_schema(event)
    assert event["tool_name"] == "agent_cancel"
    assert event["extra"]["cause"] == "disconnect"
    assert event["extra"]["request_id"] == "req-1"


@pytest.mark.asyncio
async def test_g6_lease_loss_emits_lease_lost_cause(monkeypatch, audit_spool):
    services_router = importlib.import_module("services.agent_runtime.router")
    audit_spool.install(services_router)
    monkeypatch.setattr(services_router.quota_mgr, "renew_concurrency_slot", lambda lease_id, timeout: False)

    await services_router._maintain_quota_lease(
        "lease:sysadmin-01:abc", asyncio.Event(), SimpleNamespace(cancel=lambda: None),
        user_id="sysadmin-01", session_id="sess-1", request_id="req-1",
        renew_interval=0.01,
    )

    (event,) = audit_spool.events()
    assert_event_schema(event)
    assert event["tool_name"] == "agent_cancel"
    assert event["user_id"] == "sysadmin-01"
    assert event["extra"]["cause"] == "lease_lost"
    assert event["extra"]["request_id"] == "req-1"
