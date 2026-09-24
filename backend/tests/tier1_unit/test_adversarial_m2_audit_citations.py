"""
Adversarial Stress Test Suite for Milestone M2: Tool Citations & Audit Logging.

Scope:
1. Citation accuracy under malformed or extreme inputs:
   - search_log_stream: zero matches, 50-match boundary, non-existent file paths, path traversal, binary/corrupted logs.
   - doc_runbook_reader: missing sections, malformed Markdown, deeply nested headings (levels 1-6), subsection preservation, code block comments.
   - Line numbers & SHA-256 hash fidelity: exact byte-level verification against disk content.
2. Audit event emission under fault conditions:
   - Outbox spooling when VictoriaLogs collector is offline (network drop, 500/503 errors).
   - Poison-pill resilience: corrupted lines (malformed JSON, primitives, empty lines) quarantined without deadlocking the replay worker.
   - Strict schema conformance to PROJECT.md:124-138 across all tools and exit code mappings.
"""
import os
import sys
import json
import time
import hashlib
import tempfile
import asyncio
from pathlib import Path
from unittest.mock import patch
import pytest
import httpx

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import services.agent_tools.audit as audit
from services.agent_tools.tools import search_log_stream, doc_runbook_reader, config_lint_and_diff
from services.agent_tools.audit import log_audit_event, flush_outbox
from services.agent_runtime.models import AgentChatRequest, CitationRecord
from services.agent_runtime.session_store import SessionStore
from services.agent_runtime.react_loop import run_react_agent, run_react_agent_stream, extract_citations_from_result
from services.agent_runtime.tool_registry import execute_tool_call


# ==============================================================================
# 1. Log Stream Search & Citation Accuracy Under Extreme Inputs
# ==============================================================================

class TestAdversarialLogStreamAndCitations:
    """Stress tests for search_log_stream boundary conditions and citation extraction."""

    def test_log_stream_zero_matches(self, tmp_path):
        """Zero matches: matched is False, match_count is 0, line_numbers is empty, no citations."""
        log_file = tmp_path / "zero_matches.log"
        log_file.write_text("2026-09-24 10:00:00 [INFO] System running normally\n" * 20)

        res = search_log_stream(str(log_file), "CRITICAL_FAILURE_NONEXISTENT_XYZ", max_matches=50)
        assert res["matched"] is False
        assert res["match_count"] == 0
        assert res["line_numbers"] == []
        assert res["output"] == "No occurrences found"

        # Citation extraction must produce zero records
        cits = extract_citations_from_result("search_log_stream", {"target": str(log_file), "pattern": "CRITICAL_FAILURE_NONEXISTENT_XYZ"}, {"result": res})
        assert cits == []

    def test_log_stream_max_matches_boundary_50(self, tmp_path):
        """Boundary test: 100 matching lines capped at 50, line numbers 1..50 exact, truncated=True."""
        log_file = tmp_path / "boundary_100.log"
        lines = [f"{i}: 2026-09-24 [ERROR] Disk space low code=99\n" for i in range(1, 101)]
        log_file.write_text("".join(lines))

        # 1. Requesting max_matches=50 on 100 matches
        res50 = search_log_stream(str(log_file), "Disk space low", max_matches=50, context_lines=0)
        assert res50["matched"] is True
        assert res50["match_count"] == 50
        assert len(res50["line_numbers"]) == 50
        assert res50["line_numbers"] == list(range(1, 51))
        assert res50["truncated"] is True

        # 2. Requesting max_matches=100 (must be clamped to ceiling 50)
        res_clamp = search_log_stream(str(log_file), "Disk space low", max_matches=100, context_lines=0)
        assert res_clamp["match_count"] == 50
        assert len(res_clamp["line_numbers"]) == 50

        # 3. Requesting max_matches=0 or negative (must be bounded to minimum 1)
        res_min = search_log_stream(str(log_file), "Disk space low", max_matches=0, context_lines=0)
        assert res_min["match_count"] == 1
        assert len(res_min["line_numbers"]) == 1
        assert res_min["line_numbers"] == [1]

        res_neg = search_log_stream(str(log_file), "Disk space low", max_matches=-10, context_lines=0)
        assert res_neg["match_count"] == 1

        # 4. Citation must span start_line=1 to end_line=50 and hash output
        cits = extract_citations_from_result("search_log_stream", {"target": str(log_file), "pattern": "Disk space low"}, {"result": res50})
        assert len(cits) == 1
        cit = cits[0]
        assert cit.start_line == 1
        assert cit.end_line == 50
        assert cit.artifact_hash == hashlib.sha256(res50["output"].encode("utf-8")).hexdigest()

    def test_log_stream_boundary_49_vs_50(self, tmp_path):
        """Boundary test: 49 matches -> truncated=False, 50 matches -> truncated=True."""
        log_49 = tmp_path / "log_49.log"
        log_49.write_text("".join(f"line {i} error_code_x\n" for i in range(1, 50)))

        res49 = search_log_stream(str(log_49), "error_code_x", max_matches=50)
        assert res49["match_count"] == 49
        assert res49["truncated"] is False

        log_50 = tmp_path / "log_50.log"
        log_50.write_text("".join(f"line {i} error_code_x\n" for i in range(1, 51)))

        res50 = search_log_stream(str(log_50), "error_code_x", max_matches=50)
        assert res50["match_count"] == 50
        assert res50["truncated"] is True

    def test_log_stream_pure_python_fallback_parity(self, tmp_path):
        """Ripgrep unavailable: Python streaming fallback produces identical line numbers and counts."""
        log_file = tmp_path / "stream_fallback.log"
        log_file.write_text("".join(f"line {i} marker_target\n" for i in range(1, 80)))

        # Native rg execution
        res_rg = search_log_stream(str(log_file), "marker_target", max_matches=50, context_lines=0)

        # Force pure Python fallback by mocking /usr/bin/rg nonexistence
        orig_exists = os.path.exists
        with patch("os.path.exists", side_effect=lambda p: False if str(p) == "/usr/bin/rg" else orig_exists(p)):
            res_py = search_log_stream(str(log_file), "marker_target", max_matches=50, context_lines=0)

        assert res_rg["matched"] == res_py["matched"] == True
        assert res_rg["match_count"] == res_py["match_count"] == 50
        assert res_rg["line_numbers"] == res_py["line_numbers"] == list(range(1, 51))
        assert res_rg["truncated"] == res_py["truncated"] == True

    def test_log_stream_nonexistent_and_traversal_paths(self):
        """Nonexistent paths, traversal attacks, and forbidden paths return clean error objects."""
        # 1. Nonexistent absolute path
        res_missing = search_log_stream("/tmp/nonexistent_audit_log_99999.log", "error")
        assert res_missing["matched"] is False
        assert "not found" in res_missing["error"].lower()

        # 2. Path traversal attack
        res_traversal = search_log_stream("../../../../etc/shadow", "root")
        assert res_traversal["matched"] is False
        assert "error" in res_traversal

        # 3. Forbidden system path
        if os.path.exists("/etc/shadow"):
            res_forbidden = search_log_stream("/etc/shadow", "root")
            assert res_forbidden["matched"] is False
            assert "access denied" in res_forbidden["error"].lower()

    def test_log_stream_malformed_regex_and_empty_file(self, tmp_path):
        """Invalid regex and empty files handled without unhandled exceptions."""
        empty_file = tmp_path / "empty.log"
        empty_file.write_text("")

        # Empty file
        res_empty = search_log_stream(str(empty_file), "pattern")
        assert res_empty["matched"] is False
        assert res_empty["match_count"] == 0

        # Invalid regex pattern
        res_invalid_re = search_log_stream(str(empty_file), "[unclosed-regex(")
        assert res_invalid_re["matched"] is False
        assert "Invalid regular expression" in res_invalid_re["error"]


# ==============================================================================
# 2. Runbook Reader & Deeply Nested Heading Preservation
# ==============================================================================

class TestAdversarialRunbookReaderAndCitations:
    """Stress tests for doc_runbook_reader Markdown handling, headings, and line slices."""

    def test_runbook_missing_section(self, tmp_path):
        """Missing section returns found=False, line bounds=None, and lists available sections."""
        rb_file = tmp_path / "runbook_sample.md"
        rb_file.write_text("# Overview\nSystem notes.\n## Diagnostics\nCheck logs.\n")

        res = doc_runbook_reader(str(rb_file), "Nonexistent Disaster Procedure")
        assert res["found"] is False
        assert res["start_line"] is None
        assert res["end_line"] is None
        assert "available_sections" in res
        assert "Overview" in res["available_sections"]
        assert "Diagnostics" in res["available_sections"]

        # No citations emitted
        cits = extract_citations_from_result("doc_runbook_reader", {"runbook_path": str(rb_file), "section_title": "Nonexistent Disaster Procedure"}, {"result": res})
        assert cits == []

    def test_runbook_deeply_nested_headings_and_subsection_preservation(self, tmp_path):
        """Deeply nested headings (levels 2 -> 3 -> 4 -> 5 -> 6) fully preserved within parent section."""
        md_content = """# Platform Runbook
Introduction.

## Network Troubleshooting
Initial network instructions.

### DNS Resolution
Check resolving status.

#### resolv.conf Validation
Verify nameserver entries.

##### Local Stub Listener
Systemd-resolved status.

###### Low-level socket check
ss -tulpn | grep 53

## Storage Troubleshooting
Storage instructions.
"""
        rb_file = tmp_path / "nested_runbook.md"
        rb_file.write_text(md_content)

        # Extract Level 2 section "Network Troubleshooting"
        res = doc_runbook_reader(str(rb_file), "Network Troubleshooting")
        assert res["found"] is True
        assert res["header_level"] == 2

        # Verify all child subsections (levels 3, 4, 5, 6) preserved inside content
        assert "### DNS Resolution" in res["content"]
        assert "#### resolv.conf Validation" in res["content"]
        assert "##### Local Stub Listener" in res["content"]
        assert "###### Low-level socket check" in res["content"]
        assert "ss -tulpn | grep 53" in res["content"]

        # Verify it terminated before sibling level 2 section "Storage Troubleshooting"
        assert "## Storage Troubleshooting" not in res["content"]
        assert "Storage instructions." not in res["content"]

    def test_runbook_line_numbers_and_content_hash_fidelity(self, tmp_path):
        """Line numbers (start_line, end_line) match exact disk byte slice, SHA-256 hash matches."""
        md_content = """# Title Section
Line 2 text.
Line 3 text.

## Target Section
Line 6 of file.
Line 7 of file.
Line 8 of file.

## Final Section
Line 11 of file.
Line 12 of file."""
        rb_file = tmp_path / "slice_verify.md"
        rb_file.write_text(md_content)

        with open(str(rb_file), "r", encoding="utf-8") as f:
            all_lines = f.readlines()

        # 1. Intermediate section "Target Section"
        res_target = doc_runbook_reader(str(rb_file), "Target Section")
        sl, el = res_target["start_line"], res_target["end_line"]
        assert sl == 5
        assert el == 9
        disk_slice = "".join(all_lines[sl - 1 : el])
        assert disk_slice == res_target["content"]

        # Citation validation
        cits = extract_citations_from_result("doc_runbook_reader", {"runbook_path": str(rb_file), "section_title": "Target Section"}, {"result": res_target})
        assert len(cits) == 1
        assert cits[0].start_line == 5
        assert cits[0].end_line == 9
        assert cits[0].artifact_hash == hashlib.sha256(res_target["content"].encode("utf-8")).hexdigest()

        # 2. Terminal section "Final Section" (all the way to EOF)
        res_final = doc_runbook_reader(str(rb_file), "Final Section")
        sl_f, el_f = res_final["start_line"], res_final["end_line"]
        assert sl_f == 10
        assert el_f == 12
        disk_slice_f = "".join(all_lines[sl_f - 1 : el_f])
        assert disk_slice_f == res_final["content"]

    def test_runbook_malformed_markdown_and_edge_cases(self, tmp_path):
        """Malformed headers (#NoSpace, ####### 7 hashes, empty files, comments in code blocks)."""
        md_content = """Plain text without header.
#NoSpaceHeader
####### SevenHashesNotMarkdown
# 
   ## Indented Section
Valid indented content.
```bash
# This is a comment in code
echo "hello"
```
"""
        rb_file = tmp_path / "malformed.md"
        rb_file.write_text(md_content)

        # 1. NoSpace is not recognized as a header
        res_no_space = doc_runbook_reader(str(rb_file), "NoSpaceHeader")
        assert res_no_space["found"] is False

        # 2. 7 Hashes is not standard CommonMark header
        res_7 = doc_runbook_reader(str(rb_file), "SevenHashesNotMarkdown")
        assert res_7["found"] is False

        # 3. Indented header is correctly parsed
        res_indented = doc_runbook_reader(str(rb_file), "Indented Section")
        assert res_indented["found"] is True
        assert "Valid indented content." in res_indented["content"]

        # 4. Empty file handled cleanly
        empty_file = tmp_path / "empty_runbook.md"
        empty_file.write_text("")
        res_empty = doc_runbook_reader(str(empty_file), "Any Section")
        assert res_empty["found"] is False
        assert res_empty["available_sections"] == []

    def test_runbook_case_insensitivity_and_prefix_stripping(self, tmp_path):
        """Case insensitivity and stripping of numeric prefixes (e.g. '1. Section Title')."""
        md_content = "# 1. Incident Remediation Procedure\nStep 1.\n## 2.1) Service Restart\nRestart nginx.\n"
        rb_file = tmp_path / "prefixed.md"
        rb_file.write_text(md_content)

        # Matched by normalized title without prefix
        res1 = doc_runbook_reader(str(rb_file), "Incident Remediation Procedure")
        assert res1["found"] is True

        res2 = doc_runbook_reader(str(rb_file), "service restart")
        assert res2["found"] is True


# ==============================================================================
# 3. Audit Event Emission Under Fault Conditions & Poison-Pill Quarantine
# ==============================================================================

class TestAdversarialAuditEventEmissionAndFaultTolerance:
    """Stress tests for outbox spooling, collector failures, malformed line quarantine, and schema."""

    def test_outbox_spooling_on_collector_offline(self, tmp_path, monkeypatch):
        """When VictoriaLogs is offline (exceptions or HTTP errors), events spool to outbox.jsonl."""
        test_outbox = str(tmp_path / "spool_outbox.jsonl")
        monkeypatch.setattr(audit, "OUTBOX_PATH", test_outbox)

        # Simulate connection error
        def offline_post(event):
            raise httpx.ConnectError("Connection to 127.0.0.1:9428 refused")

        monkeypatch.setattr(audit, "_post_event", offline_post)

        res = log_audit_event(
            user_id="sysadmin-01",
            session_id="sess-offline-01",
            tool_name="search_log_stream",
            action="search_log_stream",
            parameters={"target": "nginx_error.log", "pattern": "502 Bad Gateway"},
            exit_code=0,
            duration_ms=30,
            prompt_tokens=100,
            completion_tokens=50
        )
        assert res["logged"] is True
        assert res["destination"] == "outbox"

        assert os.path.exists(test_outbox)
        with open(test_outbox, "r", encoding="utf-8") as f:
            lines = [json.loads(l) for l in f if l.strip()]

        assert len(lines) == 1
        rec = lines[0]
        assert rec["user_id"] == "sysadmin-01"
        assert rec["action"] == "search_log_stream"
        assert rec["parameters"] == {"target": "nginx_error.log", "pattern": "502 Bad Gateway"}
        assert rec["exit_code"] == 0

    def test_outbox_replay_malformed_records_no_deadlock(self, tmp_path, monkeypatch):
        """Corrupted records (non-JSON, primitives, truncated, blank) are quarantined without deadlock."""
        test_outbox = str(tmp_path / "outbox.jsonl")
        corrupted_outbox = str(tmp_path / "outbox_corrupted.jsonl")
        monkeypatch.setattr(audit, "OUTBOX_PATH", test_outbox)

        # Inject 5 diverse malformed payloads interleaved with 2 valid records
        bad_syntax = b"UNPARSABLE_GARBAGE_LINE_%%%$$$\n"
        valid_1 = json.dumps({"timestamp": "2026-09-24T03:00:00Z", "action": "tool_1", "user_id": "u1"}).encode() + b"\n"
        bad_truncated = b'{"timestamp": "2026-09-24T03:00:00Z", "action": \n'
        bad_string = b'"just a valid json string, not a dict"\n'
        bad_array = b'[{"event_in_array": 1}]\n'
        bad_null = b'null\n'
        bad_empty = b'\n'
        valid_2 = json.dumps({"timestamp": "2026-09-24T03:01:00Z", "action": "tool_2", "user_id": "u2"}).encode() + b"\n"

        with open(test_outbox, "wb") as f:
            f.writelines([bad_syntax, valid_1, bad_truncated, bad_string, bad_array, bad_null, bad_empty, valid_2])

        sent_records = []
        monkeypatch.setattr(audit, "_post_event", lambda ev: sent_records.append(ev) or True)

        flush_result = flush_outbox()
        # Assert replay sent both valid records and skipped/quarantined all corrupted lines
        assert flush_result["sent"] == 2
        assert flush_result["pending"] == 0
        assert len(sent_records) == 2
        assert sent_records[0]["action"] == "tool_1"
        assert sent_records[1]["action"] == "tool_2"

        # Outbox must be completely cleared
        assert os.path.getsize(test_outbox) == 0

        # Corrupted outbox file must contain the quarantined garbage
        assert os.path.exists(corrupted_outbox)
        with open(corrupted_outbox, "rb") as cf:
            corrupted_content = cf.read()
        assert b"UNPARSABLE_GARBAGE_LINE" in corrupted_content
        assert b'{"timestamp": "2026-09-24T03:00:00Z", "action": ' in corrupted_content
        assert b"just a valid json string" in corrupted_content
        assert b'[{"event_in_array": 1}]' in corrupted_content
        assert b"null" in corrupted_content

    def test_outbox_partial_replay_retains_failed_record_and_tail(self, tmp_path, monkeypatch):
        """During transient outage mid-flush, failed record and subsequent records are preserved."""
        test_outbox = str(tmp_path / "outbox.jsonl")
        monkeypatch.setattr(audit, "OUTBOX_PATH", test_outbox)

        rec1 = {"event_id": "rec-1", "action": "act-1"}
        rec2 = {"event_id": "rec-2", "action": "act-2"}
        rec3 = {"event_id": "rec-3", "action": "act-3"}

        with open(test_outbox, "wb") as f:
            for r in [rec1, rec2, rec3]:
                f.write((json.dumps(r) + "\n").encode())

        sent = []
        def fail_on_rec2(event):
            if event["event_id"] == "rec-2":
                raise httpx.RequestError("Network partition occurred")
            sent.append(event)
            return True

        monkeypatch.setattr(audit, "_post_event", fail_on_rec2)

        # First flush attempts to send; rec-1 succeeds, rec-2 fails
        res1 = flush_outbox()
        assert res1["sent"] == 1
        assert res1["pending"] == 2
        assert "Network partition" in res1["error"]

        # Outbox now contains rec-2 and rec-3; rec-1 was safely checkpointed
        with open(test_outbox, "r") as f:
            remaining = [json.loads(l) for l in f if l.strip()]
        assert len(remaining) == 2
        assert remaining[0]["event_id"] == "rec-2"
        assert remaining[1]["event_id"] == "rec-3"

        # Restoration: network recovered
        monkeypatch.setattr(audit, "_post_event", lambda ev: sent.append(ev) or True)
        res2 = flush_outbox()
        assert res2["sent"] == 2
        assert res2["pending"] == 0
        assert os.path.getsize(test_outbox) == 0

    def test_strict_schema_conformance_project_md(self, tmp_path, monkeypatch):
        """All emitted audit records strictly satisfy schema required by PROJECT.md:124-138."""
        test_outbox = str(tmp_path / "outbox.jsonl")
        monkeypatch.setattr(audit, "OUTBOX_PATH", test_outbox)
        monkeypatch.setattr(audit, "_post_event", lambda ev: False)  # Force outbox spool

        # 1. search_log_stream success
        execute_tool_call(
            "search_log_stream",
            {"target": "backend/data/logs/nginx_error.log", "pattern": "Connection refused"},
            user_id="sysadmin-01",
            session_id="sess-01",
            workspace="/tmp"
        )
        # 2. doc_runbook_reader success
        execute_tool_call(
            "doc_runbook_reader",
            {"runbook_path": "backend/data/runbooks/nginx_recovery.md", "section_title": "Diagnostic Rapide"},
            user_id="sysadmin-01",
            session_id="sess-02",
            workspace="/tmp"
        )
        # 3. config_lint_and_diff success
        execute_tool_call(
            "config_lint_and_diff",
            {"target_file": "config.yaml", "proposed_content": "port: 8080\n"},
            user_id="sysadmin-01",
            session_id="sess-03",
            workspace="/tmp"
        )

        with open(test_outbox, "r", encoding="utf-8") as f:
            records = [json.loads(l) for l in f if l.strip()]

        assert len(records) == 3

        REQUIRED_FIELDS = [
            "timestamp", "user_id", "session_id", "action", "parameters",
            "duration_ms", "exit_code", "prompt_tokens", "completion_tokens", "approval_id"
        ]

        for i, rec in enumerate(records):
            for field in REQUIRED_FIELDS:
                assert field in rec, f"Record {i} missing field '{field}' from PROJECT.md:124-138"

            # Type and format validations
            assert isinstance(rec["timestamp"], str) and rec["timestamp"].endswith("Z")
            assert isinstance(rec["user_id"], str) and len(rec["user_id"]) > 0
            assert isinstance(rec["session_id"], str) and len(rec["session_id"]) > 0
            assert isinstance(rec["action"], str) and len(rec["action"]) > 0
            assert isinstance(rec["parameters"], dict)
            assert isinstance(rec["duration_ms"], int) and rec["duration_ms"] >= 0
            assert isinstance(rec["exit_code"], int)
            assert isinstance(rec["prompt_tokens"], int) and rec["prompt_tokens"] >= 0
            assert isinstance(rec["completion_tokens"], int) and rec["completion_tokens"] >= 0
            assert rec["approval_id"] is None or isinstance(rec["approval_id"], str)

    def test_parameter_truncation_safety(self, tmp_path, monkeypatch):
        """Proposed config content > 500 bytes is truncated in audit parameters to avoid outbox bloat."""
        test_outbox = str(tmp_path / "outbox.jsonl")
        monkeypatch.setattr(audit, "OUTBOX_PATH", test_outbox)
        monkeypatch.setattr(audit, "_post_event", lambda ev: False)

        huge_payload = "A" * 10000
        execute_tool_call(
            "config_lint_and_diff",
            {"target_file": "big.yaml", "proposed_content": huge_payload},
            user_id="sysadmin-01",
            session_id="sess-trunc-01",
            workspace="/tmp"
        )

        with open(test_outbox, "r", encoding="utf-8") as f:
            records = [json.loads(l) for l in f if l.strip()]

        assert len(records) == 1
        logged_content = records[0]["parameters"]["proposed_content"]
        assert len(logged_content) < 600
        assert "[truncated 10000 bytes]" in logged_content


# ==============================================================================
# 4. End-to-End ReAct Chat & SSE Streaming Citation Delivery
# ==============================================================================

@pytest.mark.asyncio
class TestReActChatAndStreamingCitations:
    """Verifies that citations reach the client intact over both JSON and SSE endpoints."""

    async def test_end_to_end_chat_response_citations(self, monkeypatch):
        """AgentChatResponse.citations contains valid source, section_or_query, lines, and hash."""
        step = 0
        async def mock_call(messages, model, user_id, session_id=None):
            nonlocal step
            step += 1
            if step == 1:
                return 'Thought: Read runbook.\nAction: doc_runbook_reader\nAction Input: {"runbook_path": "backend/data/runbooks/nginx_recovery.md", "section_title": "Diagnostic Rapide"}'
            return "Thought: Done.\nFinal Answer: Verified runbook procedure."

        monkeypatch.setattr("services.agent_runtime.react_loop.call_llm", mock_call)

        store = SessionStore()
        sess = await store.create_or_get_session("sysadmin-01", "sess-cit-e2e")
        req = AgentChatRequest(prompt="How to recover nginx?")

        resp = await run_react_agent(req, "sysadmin-01", sess, store)
        assert resp.citations is not None
        assert len(resp.citations) == 1
        cit = resp.citations[0]
        assert cit.source.endswith("nginx_recovery.md")
        assert cit.section_or_query == "Diagnostic Rapide"
        assert cit.start_line == 6
        assert cit.end_line == 12
        assert len(cit.artifact_hash) == 64

    async def test_end_to_end_sse_streaming_citations(self, monkeypatch):
        """SSE stream yields citations in data event payloads and terminates cleanly with [DONE]."""
        step = 0
        async def mock_call(messages, model, user_id, session_id=None):
            nonlocal step
            step += 1
            if step == 1:
                return 'Thought: Read runbook.\nAction: doc_runbook_reader\nAction Input: {"runbook_path": "backend/data/runbooks/nginx_recovery.md", "section_title": "Diagnostic Rapide"}'
            return "Thought: Done.\nFinal Answer: Procedure confirmed."

        monkeypatch.setattr("services.agent_runtime.react_loop.call_llm", mock_call)

        store = SessionStore()
        sess = await store.create_or_get_session("sysadmin-01", "sess-cit-sse")
        req = AgentChatRequest(prompt="How to recover nginx?", stream=True)

        events = []
        async for chunk in run_react_agent_stream(req, "sysadmin-01", sess, store):
            events.append(chunk)

        assert len(events) >= 3
        assert events[-1] == "data: [DONE]\n\n"

        # Check citation in final answer data event
        final_data = json.loads(events[-2].replace("data: ", "").strip())
        assert "citations" in final_data
        assert len(final_data["citations"]) == 1
        cit = final_data["citations"][0]
        assert cit["source"].endswith("nginx_recovery.md")
        assert cit["start_line"] == 6
        assert cit["end_line"] == 12
        assert len(cit["artifact_hash"]) == 64
