import os
import sys
import time
import logging
from typing import Dict, Any, Tuple, Optional

# Add backend directory to sys.path to allow absolute imports
BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from services.agent_tools.tools import (
    search_log_stream,
    config_lint_and_diff,
    doc_runbook_reader,
    execute_sandboxed_command
)
from services.agent_tools.approval_gate import (
    evaluate_command_safety,
    create_approval_request,
    consume_approval,
)
from services.agent_tools.audit import log_audit_event
from services.agent_runtime.workspace import ensure_workspace

logger = logging.getLogger("agent_runtime.tool_registry")

AVAILABLE_TOOLS = [
    "search_log_stream",
    "config_lint_and_diff",
    "doc_runbook_reader",
    "sandboxed_bash"
]

TOOL_DESCRIPTIONS = {
    "search_log_stream": {
        "description": "Bounded regex search in log files or journalctl without buffering full files in memory.",
        "parameters": {
            "target": "string (file path or unit name, e.g. ./backend/data/logs/nginx_error.log)",
            "pattern": "string (regex search pattern)",
            "max_matches": "integer (optional, default 50)",
            "context_lines": "integer (optional, default 2)"
        }
    },
    "config_lint_and_diff": {
        "description": "Syntax validator for JSON, YAML, systemd units and unified diff generator.",
        "parameters": {
            "target_file": "string (file path to compare/lint against)",
            "proposed_content": "string (full proposed configuration content)"
        }
    },
    "doc_runbook_reader": {
        "description": "Extracts specific operational sections from Markdown runbooks.",
        "parameters": {
            "runbook_path": "string (path to markdown file, e.g. ./backend/data/runbooks/nginx_recovery.md)",
            "section_title": "string (header title or keyword of section to extract)"
        }
    },
    "sandboxed_bash": {
        "description": "Executes shell commands strictly confined in Bubblewrap sandbox.",
        "parameters": {
            "command": "string (shell command)",
            "approval_id": "string (optional, if previously approved by HITL gate)"
        }
    }
}

def format_tool_catalog() -> str:
    lines = []
    for name, info in TOOL_DESCRIPTIONS.items():
        lines.append(f"- {name}: {info['description']}")
        lines.append(f"  Parameters: {info['parameters']}")
    return "\n".join(lines)

def execute_tool_call(
    tool_name: str,
    parameters: Dict[str, Any],
    user_id: str,
    session_id: str,
    workspace: str
) -> Dict[str, Any]:
    """Dispatches tool execution, handles HITL safety gating, and logs audit record."""
    start_time = time.time()
    exit_code = 0
    human_approved = False
    result = {}

    try:
        if tool_name == "search_log_stream":
            target = parameters.get("target", "")
            pattern = parameters.get("pattern", "")
            max_matches = int(parameters.get("max_matches", 50))
            context_lines = int(parameters.get("context_lines", 2))
            result = search_log_stream(target, pattern, max_matches=max_matches, context_lines=context_lines, user_id=user_id)
            if result.get("error"):
                exit_code = 2 if "not found" in str(result.get("error", "")).lower() else 1
            else:
                exit_code = 0

        elif tool_name == "config_lint_and_diff":
            target_file = parameters.get("target_file", "")
            proposed_content = parameters.get("proposed_content", "")
            result = config_lint_and_diff(target_file, proposed_content, user_id=user_id)
            if not result.get("valid", True) or result.get("error"):
                exit_code = 1
            else:
                exit_code = 0

        elif tool_name == "doc_runbook_reader":
            runbook_path = parameters.get("runbook_path", "")
            section_title = parameters.get("section_title", "")
            result = doc_runbook_reader(runbook_path, section_title, user_id=user_id)
            if not result.get("found", True):
                exit_code = 2 if "not found" in str(result.get("error", "")).lower() else 1
            else:
                exit_code = 0

        elif tool_name in ["sandboxed_bash", "bash", "terminal"]:
            cmd = parameters.get("command", "")
            approval_id = parameters.get("approval_id")
            assigned_workspace = str(ensure_workspace(user_id))
            if os.path.realpath(workspace) != os.path.realpath(assigned_workspace):
                return {"status": "error", "error": "Workspace does not match authenticated user"}

            # 1. Safety check
            safety = evaluate_command_safety(cmd)
            if safety["action"] == "BLOCKED":
                duration_ms = int((time.time() - start_time) * 1000)
                log_audit_event(user_id, session_id, tool_name, cmd, False, 126, duration_ms, action=tool_name, parameters=parameters, extra={"blocked": True, "reason": safety["reason"]})
                return {
                    "status": "blocked",
                    "blocked": True,
                    "reason": safety["reason"],
                    "duration_ms": duration_ms
                }

            # 2. Mutating check
            if safety["action"] == "APPROVAL_REQUIRED":
                if not consume_approval(approval_id, user_id, session_id, cmd, assigned_workspace):
                    if approval_id:
                        return {"status": "blocked", "blocked": True, "reason": "Approval invalid, expired, mismatched, or already used"}
                    new_appr_id = create_approval_request(user_id, session_id, cmd, safety["reason"], assigned_workspace)
                    duration_ms = int((time.time() - start_time) * 1000)
                    log_audit_event(user_id, session_id, tool_name, cmd, False, 0, duration_ms, action=tool_name, parameters=parameters, approval_id=new_appr_id, extra={"approval_required": True, "approval_id": new_appr_id})
                    return {
                        "status": "approval_required",
                        "approval_id": new_appr_id,
                        "command": cmd,
                        "message": safety["reason"],
                        "duration_ms": duration_ms
                    }
                human_approved = True

            # 3. Sandbox execution
            exit_code, stdout, stderr = execute_sandboxed_command(assigned_workspace, cmd)
            result = {
                "exit_code": exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "confined": True
            }

        else:
            return {"status": "error", "error": f"Unknown tool: {tool_name}"}

        duration_ms = int((time.time() - start_time) * 1000)
        log_audit_event(
            user_id=user_id,
            session_id=session_id,
            tool_name=tool_name,
            action=tool_name,
            parameters=parameters,
            command=parameters.get("command"),
            human_approved=human_approved,
            exit_code=exit_code,
            duration_ms=duration_ms
        )
        return {"status": "success", "result": result, "duration_ms": duration_ms}

    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        log_audit_event(
            user_id=user_id,
            session_id=session_id,
            tool_name=tool_name,
            action=tool_name,
            parameters=parameters if isinstance(parameters, dict) else {},
            command=parameters.get("command") if isinstance(parameters, dict) else "",
            human_approved=False,
            exit_code=1,
            duration_ms=duration_ms,
            extra={"error": str(e)}
        )
        return {"status": "error", "error": str(e), "duration_ms": duration_ms}
