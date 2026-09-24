import os
import json
import time
import httpx
import logging
import asyncio
import hashlib
from pathlib import Path
from fastapi import HTTPException
from typing import List, Dict, Any, Optional, AsyncGenerator

from .models import (
    AgentChatRequest,
    AgentChatResponse,
    CitationRecord,
    ToolExecutionRecord,
    SessionState,
    SessionMessage
)
from .tool_registry import AVAILABLE_TOOLS, format_tool_catalog, execute_tool_call
from .parser import parse_model_output
from .session_store import SessionStore
from services.agent_tools.audit import log_audit_event

logger = logging.getLogger("agent_runtime.react_loop")

LITELLM_URL = os.getenv("LITELLM_URL", "http://127.0.0.1:4000/v1")
KEYS_DIR = Path(__file__).resolve().parents[2] / "config" / "keys"

SYSTEM_PROMPT_TEMPLATE = """You are a production-ready Sysadmin AI Operations Assistant deployed on an on-premises enterprise Linux platform.
You operate on behalf of authenticated system administrators.
Current Authenticated User: {user_id}
Assigned User Workspace: {workspace}

OPERATIONAL CONSTRAINTS:
1. You operate under a strict least-privilege security policy. Read-only investigation is preferred.
2. Production-modifying commands (e.g. systemctl restart, service modification) must be proposed via sandboxed_bash and will be intercepted for Human-in-the-Loop review.
3. Destructive commands (e.g. rm -rf /, mkfs, dd, fork-bombs) are strictly forbidden and will be blocked immediately.
4. Always analyze logs and runbooks using the bounded tools rather than guessing file contents.

AVAILABLE TOOLS:
{tool_catalog}

REACT REASONING PROTOCOL:
You must use the following format strictly:

Thought: Your step-by-step reasoning about what diagnostic or operational step to take next.
Action: The name of the tool to invoke. Must be exactly one of: [{tool_names}].
Action Input: A valid JSON object containing the tool parameters.

Once an Action is submitted, you will receive:
Observation: The result of the tool execution.

When you have gathered sufficient information to address the user request, or if no tool is required, conclude with:
Thought: I have sufficient information to answer the request.
Final Answer: Your comprehensive, professional sysadmin response citing exact lines, error codes, and recommended procedures.
"""

def extract_citations_from_result(
    tool_name: str,
    tool_args: Dict[str, Any],
    exec_res: Dict[str, Any]
) -> List[CitationRecord]:
    """Extracts structured CitationRecord items from tool execution results."""
    citations: List[CitationRecord] = []
    res = exec_res.get("result", {})
    if not isinstance(res, dict):
        return citations

    if tool_name == "doc_runbook_reader":
        if res.get("found"):
            content = res.get("content", "")
            art_hash = hashlib.sha256(content.encode("utf-8")).hexdigest() if content else None
            citations.append(CitationRecord(
                source=res.get("source_id") or res.get("runbook_path") or tool_args.get("runbook_path", ""),
                section_or_query=res.get("section_title") or tool_args.get("section_title"),
                start_line=res.get("start_line"),
                end_line=res.get("end_line"),
                artifact_hash=art_hash
            ))
    elif tool_name == "search_log_stream":
        if res.get("matched"):
            line_nums = res.get("line_numbers", [])
            start_l = min(line_nums) if line_nums else None
            end_l = max(line_nums) if line_nums else None
            output = res.get("output", "")
            art_hash = hashlib.sha256(output.encode("utf-8")).hexdigest() if output else None
            citations.append(CitationRecord(
                source=res.get("source_id") or res.get("target") or tool_args.get("target", ""),
                section_or_query=tool_args.get("pattern"),
                start_line=start_l,
                end_line=end_l,
                artifact_hash=art_hash
            ))
    elif tool_name == "config_lint_and_diff":
        art_hash = res.get("proposed_hash") or res.get("original_hash")
        citations.append(CitationRecord(
            source=res.get("target_file") or tool_args.get("target_file", ""),
            section_or_query="config_lint_and_diff",
            start_line=None,
            end_line=None,
            artifact_hash=art_hash
        ))
    return citations

async def call_llm(
    messages: List[Dict[str, Any]],
    model: str,
    user_id: str,
    session_id: Optional[str] = None
) -> str:
    """Call the quota-enforcing gateway. A gateway failure must stop generation."""
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": 512,
        "stream": False,
        "metadata": {
            "session_id": session_id or "",
            "user_id": user_id
        }
    }
    key_name = "master" if user_id == "sysadmin-admin" else ("emergency-p1" if user_id == "emergency-p1-oncall" else user_id)
    key_file = KEYS_DIR / f"{key_name}.key"
    bearer_token = key_file.read_text().strip() if key_file.is_file() else ""
    headers = {"Authorization": f"Bearer {bearer_token}", "Content-Type": "application/json"}

    endpoint = f"{LITELLM_URL.rstrip('/')}/chat/completions"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(endpoint, json=payload, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    if not isinstance(content, str):
        raise ValueError("Inference gateway returned invalid message content")
    return content

async def run_react_agent(
    request: AgentChatRequest,
    user_id: str,
    session: SessionState,
    session_store: SessionStore
) -> AgentChatResponse:
    """Executes the multi-turn ReAct reasoning loop."""
    tools_executed: List[ToolExecutionRecord] = []
    thought_steps: List[Dict[str, Any]] = []
    accumulated_citations: List[CitationRecord] = []

    # 1. Build System Prompt
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        user_id=user_id,
        workspace=session.workspace,
        tool_catalog=format_tool_catalog(),
        tool_names=", ".join(AVAILABLE_TOOLS)
    )

    # 2. Build Conversational History
    llm_messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    for msg in session.messages:
        llm_messages.append({"role": msg.role, "content": msg.content})

    # Add current prompt
    llm_messages.append({"role": "user", "content": request.prompt})

    # Record user message in session
    session.messages.append(SessionMessage(role="user", content=request.prompt, timestamp=time.time()))
    session.turn_count += 1

    max_steps = request.max_steps or 5
    final_response_text = ""

    # 3. Execution Loop
    for step in range(max_steps):
        try:
            try:
                raw_output = await call_llm(llm_messages, request.model or "fast-model", user_id, session_id=session.session_id)
            except TypeError:
                raw_output = await call_llm(llm_messages, request.model or "fast-model", user_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403, 429):
                raise HTTPException(status_code=exc.response.status_code, detail="Inference gateway rejected the request") from exc
            raise HTTPException(status_code=502, detail="Inference gateway failed") from exc
        except (httpx.RequestError, OSError, ValueError, KeyError, IndexError) as exc:
            raise HTTPException(status_code=502, detail="Inference gateway unavailable or returned an invalid response") from exc
        parsed = parse_model_output(raw_output, AVAILABLE_TOOLS)

        # Case A: Final Answer reached
        if parsed.type == "final_answer":
            final_response_text = parsed.final_answer or ""
            break

        # Case B: Tool Action proposed
        elif parsed.type == "action":
            tool_name = parsed.tool_name
            tool_args = parsed.tool_args or {}

            # Append assistant step
            step_text = f"Thought: {parsed.thought or 'Executing ' + tool_name}\nAction: {tool_name}\nAction Input: {json.dumps(tool_args)}"
            llm_messages.append({"role": "assistant", "content": step_text})

            # Execute tool
            exec_res = execute_tool_call(
                tool_name=tool_name,
                parameters=tool_args,
                user_id=user_id,
                session_id=session.session_id,
                workspace=session.workspace
            )

            # Extract citations
            new_cits = extract_citations_from_result(tool_name, tool_args, exec_res)
            accumulated_citations.extend(new_cits)

            # Check for HITL approval interception
            if exec_res.get("status") == "approval_required":
                appr_id = exec_res["approval_id"]
                cmd = exec_res["command"]
                rec = ToolExecutionRecord(
                    tool=tool_name,
                    status="approval_required",
                    parameters=tool_args,
                    approval_id=appr_id,
                    command=cmd,
                    duration_ms=exec_res.get("duration_ms", 0)
                )
                tools_executed.append(rec)

                approval_explanation = (
                    f"The proposed operation requires Human-in-the-Loop authorization before execution:\n\n"
                    f"**Command**: `{cmd}`\n"
                    f"**Approval ID**: `{appr_id}`\n"
                    f"**Policy**: {exec_res.get('message', 'Mutating operation')}\n\n"
                    f"Please review and approve via `/api/approvals/decide`."
                )

                session.messages.append(SessionMessage(
                    role="assistant",
                    content=approval_explanation,
                    thought_steps=thought_steps,
                    tools_executed=[t.model_dump() for t in tools_executed],
                    timestamp=time.time()
                ))
                await session_store.save_session(session)

                return AgentChatResponse(
                    session_id=session.session_id,
                    user_id=user_id,
                    response=approval_explanation,
                    tools_executed=tools_executed,
                    approval_required=True,
                    approval_id=appr_id,
                    command=cmd,
                    turn_count=session.turn_count,
                    citations=accumulated_citations
                )

            # Check for security blockage
            elif exec_res.get("status") == "blocked":
                rec = ToolExecutionRecord(
                    tool=tool_name,
                    status="blocked",
                    parameters=tool_args,
                    error=exec_res.get("reason"),
                    duration_ms=exec_res.get("duration_ms", 0)
                )
                tools_executed.append(rec)
                obs_content = f"SECURITY POLICY VIOLATION: Command blocked by interceptor. Reason: {exec_res.get('reason')}"

            # Check for execution error
            elif exec_res.get("status") == "error":
                rec = ToolExecutionRecord(
                    tool=tool_name,
                    status="error",
                    parameters=tool_args,
                    error=exec_res.get("error"),
                    duration_ms=exec_res.get("duration_ms", 0)
                )
                tools_executed.append(rec)
                obs_content = f"Tool execution error: {exec_res.get('error')}"

            # Success
            else:
                rec = ToolExecutionRecord(
                    tool=tool_name,
                    status="success",
                    parameters=tool_args,
                    duration_ms=exec_res.get("duration_ms", 0)
                )
                tools_executed.append(rec)
                obs_content = json.dumps(exec_res.get("result", {}))

            # Store step and inject Observation
            thought_steps.append({
                "thought": parsed.thought,
                "action": tool_name,
                "action_input": tool_args,
                "observation": obs_content
            })
            llm_messages.append({"role": "user", "content": f"Observation: {obs_content}"})

        # Case C: Parse error (Self-Healing Recovery)
        elif parsed.type == "error":
            recovery_prompt = (
                f"Observation: Error parsing tool call. Details: {parsed.error_message}. "
                f"Please correct your formatting and emit strictly:\n"
                f"Thought: <reasoning>\nAction: <tool_name>\nAction Input: <json_object>\n"
                f"OR\nThought: <reasoning>\nFinal Answer: <answer>"
            )
            llm_messages.append({"role": "user", "content": recovery_prompt})

    # If loop ended without explicit Final Answer, synthesize observations
    if not final_response_text:
        final_response_text = (
            "Investigation complete. Gathered diagnostic observations across bounded tools:\n\n" +
            "\n".join([f"- **{s['action']}**: {s['observation'][:180]}..." for s in thought_steps])
        )

    # 4. Save Session
    session.messages.append(SessionMessage(
        role="assistant",
        content=final_response_text,
        thought_steps=thought_steps,
        tools_executed=[t.model_dump() for t in tools_executed],
        timestamp=time.time()
    ))
    await session_store.save_session(session)

    return AgentChatResponse(
        session_id=session.session_id,
        user_id=user_id,
        response=final_response_text,
        tools_executed=tools_executed,
        approval_required=False,
        turn_count=session.turn_count,
        citations=accumulated_citations
    )

async def run_react_agent_stream(
    request: AgentChatRequest,
    user_id: str,
    session: SessionState,
    session_store: SessionStore,
    cancel_event: Optional[asyncio.Event] = None,
    http_request: Optional[Any] = None
) -> AsyncGenerator[str, None]:
    """
    Executes the multi-turn ReAct reasoning loop with Server-Sent Events (SSE) streaming.
    Yields chunks formatted as: data: {"chunk": "...", "citations": [...]}\n\n
    Ends with: data: [DONE]\n\n
    Monitors cancellation and client disconnects.
    """
    tools_executed: List[ToolExecutionRecord] = []
    thought_steps: List[Dict[str, Any]] = []
    accumulated_citations: List[CitationRecord] = []

    # 1. Build System Prompt
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        user_id=user_id,
        workspace=session.workspace,
        tool_catalog=format_tool_catalog(),
        tool_names=", ".join(AVAILABLE_TOOLS)
    )

    # 2. Build Conversational History
    llm_messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    for msg in session.messages:
        llm_messages.append({"role": msg.role, "content": msg.content})

    # Add current prompt
    llm_messages.append({"role": "user", "content": request.prompt})

    # Record user message in session
    session.messages.append(SessionMessage(role="user", content=request.prompt, timestamp=time.time()))
    session.turn_count += 1

    max_steps = request.max_steps or 5
    final_response_text = ""

    # 3. Execution Loop
    for step in range(max_steps):
        # Check cancellation or client disconnect
        if cancel_event and cancel_event.is_set():
            logger.info(f"Stream cancelled before step {step} for user {user_id}")
            break
        if http_request and await http_request.is_disconnected():
            logger.info(f"Client disconnected before step {step} for user {user_id}")
            break

        try:
            try:
                raw_output = await call_llm(llm_messages, request.model or "fast-model", user_id, session_id=session.session_id)
            except TypeError:
                raw_output = await call_llm(llm_messages, request.model or "fast-model", user_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403, 429):
                raise HTTPException(status_code=exc.response.status_code, detail="Inference gateway rejected the request") from exc
            raise HTTPException(status_code=502, detail="Inference gateway failed") from exc
        except (httpx.RequestError, OSError, ValueError, KeyError, IndexError) as exc:
            raise HTTPException(status_code=502, detail="Inference gateway unavailable or returned an invalid response") from exc

        if cancel_event and cancel_event.is_set():
            logger.info(f"Stream cancelled during step {step} for user {user_id}")
            break
        if http_request and await http_request.is_disconnected():
            logger.info(f"Client disconnected during step {step} for user {user_id}")
            break

        parsed = parse_model_output(raw_output, AVAILABLE_TOOLS)

        # Case A: Final Answer reached
        if parsed.type == "final_answer":
            final_response_text = parsed.final_answer or ""
            break

        # Case B: Tool Action proposed
        elif parsed.type == "action":
            tool_name = parsed.tool_name
            tool_args = parsed.tool_args or {}

            # Yield action notification chunk
            action_desc = f"Thinking: {parsed.thought or 'Executing ' + tool_name}\nAction: {tool_name}\n"
            cits_payload = [c.model_dump() for c in accumulated_citations]
            yield f"data: {json.dumps({'chunk': action_desc, 'citations': cits_payload})}\n\n"

            # Append assistant step
            step_text = f"Thought: {parsed.thought or 'Executing ' + tool_name}\nAction: {tool_name}\nAction Input: {json.dumps(tool_args)}"
            llm_messages.append({"role": "assistant", "content": step_text})

            # Execute tool
            exec_res = execute_tool_call(
                tool_name=tool_name,
                parameters=tool_args,
                user_id=user_id,
                session_id=session.session_id,
                workspace=session.workspace
            )

            # Extract citations from tool execution
            new_cits = extract_citations_from_result(tool_name, tool_args, exec_res)
            accumulated_citations.extend(new_cits)

            # Check for HITL approval interception
            if exec_res.get("status") == "approval_required":
                appr_id = exec_res["approval_id"]
                cmd = exec_res["command"]
                rec = ToolExecutionRecord(
                    tool=tool_name,
                    status="approval_required",
                    parameters=tool_args,
                    approval_id=appr_id,
                    command=cmd,
                    duration_ms=exec_res.get("duration_ms", 0)
                )
                tools_executed.append(rec)

                approval_explanation = (
                    f"The proposed operation requires Human-in-the-Loop authorization before execution:\n\n"
                    f"**Command**: `{cmd}`\n"
                    f"**Approval ID**: `{appr_id}`\n"
                    f"**Policy**: {exec_res.get('message', 'Mutating operation')}\n\n"
                    f"Please review and approve via `/api/approvals/decide`."
                )

                session.messages.append(SessionMessage(
                    role="assistant",
                    content=approval_explanation,
                    thought_steps=thought_steps,
                    tools_executed=[t.model_dump() for t in tools_executed],
                    timestamp=time.time()
                ))
                await session_store.save_session(session)

                cits_payload = [c.model_dump() for c in accumulated_citations]
                yield f"data: {json.dumps({'chunk': approval_explanation, 'citations': cits_payload, 'approval_required': True, 'approval_id': appr_id, 'command': cmd})}\n\n"
                yield "data: [DONE]\n\n"
                return

            # Check for security blockage
            elif exec_res.get("status") == "blocked":
                rec = ToolExecutionRecord(
                    tool=tool_name,
                    status="blocked",
                    parameters=tool_args,
                    error=exec_res.get("reason"),
                    duration_ms=exec_res.get("duration_ms", 0)
                )
                tools_executed.append(rec)
                obs_content = f"SECURITY POLICY VIOLATION: Command blocked by interceptor. Reason: {exec_res.get('reason')}"

            # Check for execution error
            elif exec_res.get("status") == "error":
                rec = ToolExecutionRecord(
                    tool=tool_name,
                    status="error",
                    parameters=tool_args,
                    error=exec_res.get("error"),
                    duration_ms=exec_res.get("duration_ms", 0)
                )
                tools_executed.append(rec)
                obs_content = f"Tool execution error: {exec_res.get('error')}"

            # Success
            else:
                rec = ToolExecutionRecord(
                    tool=tool_name,
                    status="success",
                    parameters=tool_args,
                    duration_ms=exec_res.get("duration_ms", 0)
                )
                tools_executed.append(rec)
                obs_content = json.dumps(exec_res.get("result", {}))

            # Store step and inject Observation
            thought_steps.append({
                "thought": parsed.thought,
                "action": tool_name,
                "action_input": tool_args,
                "observation": obs_content
            })
            llm_messages.append({"role": "user", "content": f"Observation: {obs_content}"})

            # Stream citation update if any new citation was added
            if new_cits:
                cits_payload = [c.model_dump() for c in accumulated_citations]
                yield f"data: {json.dumps({'chunk': '', 'citations': cits_payload})}\n\n"

        # Case C: Parse error (Self-Healing Recovery)
        elif parsed.type == "error":
            recovery_prompt = (
                f"Observation: Error parsing tool call. Details: {parsed.error_message}. "
                f"Please correct your formatting and emit strictly:\n"
                f"Thought: <reasoning>\nAction: <tool_name>\nAction Input: <json_object>\n"
                f"OR\nThought: <reasoning>\nFinal Answer: <answer>"
            )
            llm_messages.append({"role": "user", "content": recovery_prompt})

    # If loop ended without explicit Final Answer, synthesize observations
    if not final_response_text:
        final_response_text = (
            "Investigation complete. Gathered diagnostic observations across bounded tools:\n\n" +
            "\n".join([f"- **{s['action']}**: {s['observation'][:180]}..." for s in thought_steps])
        )

    # Yield final answer chunk with full citations
    cits_payload = [c.model_dump() for c in accumulated_citations]
    yield f"data: {json.dumps({'chunk': final_response_text, 'citations': cits_payload})}\n\n"

    # 4. Save Session
    session.messages.append(SessionMessage(
        role="assistant",
        content=final_response_text,
        thought_steps=thought_steps,
        tools_executed=[t.model_dump() for t in tools_executed],
        timestamp=time.time()
    ))
    await session_store.save_session(session)

    # 5. Terminal SSE event
    yield "data: [DONE]\n\n"
