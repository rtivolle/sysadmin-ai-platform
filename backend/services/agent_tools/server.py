#!/usr/bin/env python3
"""
Agent Platform Server: Tool Execution, Security Approval Gate & VictoriaLogs Audit
"""
import os
import sys
import time
import json
import asyncio
from fastapi import FastAPI, Request, HTTPException
from fastapi import Query
from fastapi.responses import JSONResponse
import httpx
import uvicorn

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)
from services.agent_tools.tools import search_log_stream, config_lint_and_diff, doc_runbook_reader, execute_sandboxed_command
from services.agent_tools.approval_gate import evaluate_command_safety, create_approval_request, consume_approval, decide_approval, PENDING_APPROVALS
from services.agent_tools.audit import log_audit_event
from services.agent_runtime.router import router as agent_router
from services.target_adapter.router import router as target_adapter_router
from services.model_manager.router import router as model_manager_router
from services.agent_runtime.workspace import ensure_workspace
from services.auth_gateway.server import authenticate_request, role_for_user
from services.hardware_survey import run_hardware_survey

app = FastAPI(title="Sysadmin Agent Platform API", version="1.0.0")

@app.exception_handler(ConnectionError)
async def approval_store_unavailable(_request: Request, _exc: ConnectionError):
    return JSONResponse(status_code=503, content={"detail": "Shared approval store unavailable"})

# Mount agent runtime router
app.include_router(agent_router, prefix="/api/v1/agent", tags=["Agent Runtime"])
app.include_router(agent_router, prefix="/agent", tags=["Agent Runtime Alias"])
# Mount target adapter router
app.include_router(target_adapter_router)
# Mount local model lifecycle router (admin-only)
app.include_router(model_manager_router)

LITELLM_URL = os.getenv("LITELLM_URL", "http://127.0.0.1:4000")
_survey_cache = None
_survey_cache_timestamp = 0.0

@app.get("/health")
async def health():
    return {"status": "healthy", "service": "agent_tools_platform", "timestamp": time.time()}


@app.get("/api/v1/survey")
async def hardware_survey(request: Request, refresh: int = Query(default=0)):
    """Return a read-only hardware survey to administrators."""
    require_approval_reviewer(request)
    global _survey_cache, _survey_cache_timestamp
    now = time.monotonic()
    if refresh != 1 and _survey_cache is not None and now - _survey_cache_timestamp < 10:
        return _survey_cache
    _survey_cache = run_hardware_survey()
    _survey_cache_timestamp = now
    return _survey_cache

@app.get("/api/tools/list")
async def list_tools(request: Request):
    authenticate_request(request)
    return {
        "tools": [
            {
                "name": "search_log_stream",
                "description": "Bounded regex search in log files or journalctl without buffering full files.",
                "parameters": ["target", "pattern", "max_matches", "context_lines"]
            },
            {
                "name": "config_lint_and_diff",
                "description": "Syntax validator for JSON, YAML, systemd units + unified diff generator.",
                "parameters": ["target_file", "proposed_content"]
            },
            {
                "name": "doc_runbook_reader",
                "description": "Extracts specific operational sections from Markdown runbooks.",
                "parameters": ["runbook_path", "section_title"]
            },
            {
                "name": "sandboxed_bash",
                "description": "Executes shell commands strictly confined in Bubblewrap sandbox.",
                "parameters": ["command", "approval_id"]
            }
        ]
    }

@app.post("/api/tools/execute")
async def execute_tool(request: Request):
    data = await request.json()
    tool_name = data.get("name")
    params = data.get("parameters", {})
    user_id, _ = authenticate_request(request)
    session_id = data.get("session_id", "sess-default")

    try:
        workspace = str(ensure_workspace(user_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    start_time = time.time()
    result = {}
    exit_code = 0
    human_approved = False

    if tool_name == "search_log_stream":
        target = params.get("target")
        pattern = params.get("pattern")
        max_matches = int(params.get("max_matches", 50))
        result = search_log_stream(target, pattern, max_matches=max_matches, user_id=user_id)
        if result.get("error"):
            exit_code = 2 if "not found" in str(result.get("error", "")).lower() else 1
        else:
            exit_code = 0

    elif tool_name == "config_lint_and_diff":
        target_file = params.get("target_file")
        proposed_content = params.get("proposed_content", "")
        result = config_lint_and_diff(target_file, proposed_content, user_id=user_id)
        if not result.get("valid", True) or result.get("error"):
            exit_code = 1
        else:
            exit_code = 0

    elif tool_name == "doc_runbook_reader":
        runbook_path = params.get("runbook_path")
        section_title = params.get("section_title")
        result = doc_runbook_reader(runbook_path, section_title, user_id=user_id)
        if not result.get("found", True):
            exit_code = 2 if "not found" in str(result.get("error", "")).lower() else 1
        else:
            exit_code = 0

    elif tool_name in ["sandboxed_bash", "bash", "terminal"]:
        cmd = params.get("command", "")
        approval_id = params.get("approval_id")

        safety = evaluate_command_safety(cmd)
        if safety["action"] == "BLOCKED":
            duration_ms = int((time.time() - start_time) * 1000)
            log_audit_event(user_id, session_id, tool_name, cmd, False, 126, duration_ms, action=tool_name, parameters=params, extra={"blocked": True, "reason": safety["reason"]})
            raise HTTPException(status_code=403, detail=safety["reason"])

        if safety["action"] == "APPROVAL_REQUIRED":
            if not consume_approval(approval_id, user_id, session_id, cmd, workspace):
                if approval_id:
                    duration_ms = int((time.time() - start_time) * 1000)
                    log_audit_event(user_id, session_id, tool_name, cmd, False, 126, duration_ms, action=tool_name, parameters=params, approval_id=approval_id, extra={"approval_denied": True, "reason": "Approval invalid, expired, mismatched, or already used"})
                    raise HTTPException(status_code=403, detail="Approval invalid, expired, mismatched, or already used")
                new_appr_id = create_approval_request(user_id, session_id, cmd, safety["reason"], workspace)
                duration_ms = int((time.time() - start_time) * 1000)
                log_audit_event(user_id, session_id, tool_name, cmd, False, 0, duration_ms, action=tool_name, parameters=params, approval_id=new_appr_id, extra={"approval_required": True, "approval_id": new_appr_id})
                return JSONResponse(
                    status_code=202,
                    content={
                        "status": "approval_required",
                        "approval_id": new_appr_id,
                        "command": cmd,
                        "message": f"Operation '{cmd}' requires human-in-the-loop approval before execution."
                    }
                )
            human_approved = True

        exit_code, stdout, stderr = execute_sandboxed_command(workspace, cmd)
        result = {
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "confined": True
        }

    else:
        duration_ms = int((time.time() - start_time) * 1000)
        log_audit_event(
            user_id=user_id,
            session_id=session_id,
            tool_name=tool_name or "unknown",
            action=tool_name or "unknown",
            parameters=params,
            command=params.get("command"),
            human_approved=False,
            exit_code=127,
            duration_ms=duration_ms,
            extra={"unknown_tool": True},
        )
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")

    duration_ms = int((time.time() - start_time) * 1000)
    log_audit_event(
        user_id=user_id,
        session_id=session_id,
        tool_name=tool_name,
        action=tool_name,
        parameters=params,
        command=params.get("command"),
        human_approved=human_approved,
        exit_code=exit_code,
        duration_ms=duration_ms
    )

    return {"status": "success", "result": result, "duration_ms": duration_ms}

def require_approval_reviewer(request: Request) -> str:
    reviewer, _ = authenticate_request(request)
    if role_for_user(reviewer) != "admin":
        raise HTTPException(status_code=403, detail="Authenticated administrator required")
    return reviewer


@app.get("/api/approvals/pending")
async def list_pending_approvals(request: Request):
    require_approval_reviewer(request)
    now = time.time()
    return {"pending_approvals": [
        approval for approval in PENDING_APPROVALS.values()
        if approval.get("status") == "pending" and approval.get("expires_at", 0) > now
    ]}

@app.post("/api/approvals/decide")
async def decide_approval_endpoint(request: Request):
    data = await request.json()
    approval_id = data.get("approval_id")
    approved = bool(data.get("approved"))
    reviewer = require_approval_reviewer(request)
    res = decide_approval(approval_id, approved, reviewer, role_for_user(reviewer))
    if not res["success"]:
        raise HTTPException(status_code=400, detail=res["error"])
    approval = res["approval"]
    log_audit_event(
        user_id=reviewer,
        session_id=approval["session_id"],
        tool_name="approval_decision",
        command=approval["command"],
        human_approved=approved,
        exit_code=0,
        duration_ms=0,
        extra={"approval_decision": approval["status"], "approval_id": approval_id, "requester": approval["user_id"]},
    )
    return res

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 3080
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
