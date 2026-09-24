"""
Tier 1 Unit Test: Tool Input/Output Contract Schemas.
Verifies return types and schema integrity against PROJECT.md § Interface Contracts.
"""
import os
import pytest

from backend.services.agent_tools.tools import (
    search_log_stream,
    config_lint_and_diff,
    doc_runbook_reader
)
from backend.services.agent_tools.approval_gate import evaluate_command_safety
from backend.tests.fixtures import LOGS_DIR, CONFIG_DIR, RUNBOOKS_DIR

def test_search_log_stream_schema():
    """Verify search_log_stream returns all mandatory schema fields."""
    target = os.path.join(LOGS_DIR, "nginx_error.log")
    res = search_log_stream(target, "nginx")
    
    assert isinstance(res, dict)
    assert "matched" in res and isinstance(res["matched"], bool)
    assert "target" in res and isinstance(res["target"], str)
    assert "pattern" in res and isinstance(res["pattern"], str)
    assert "match_count" in res and isinstance(res["match_count"], int)
    assert "output" in res and isinstance(res["output"], str)

def test_config_lint_and_diff_schema():
    """Verify config_lint_and_diff returns all mandatory schema fields."""
    target = os.path.join(CONFIG_DIR, "platform_config.json")
    res = config_lint_and_diff(target, '{"valid": true}')
    
    assert isinstance(res, dict)
    assert "valid" in res and isinstance(res["valid"], bool)
    assert "error" in res
    assert "original_size" in res and isinstance(res["original_size"], int)
    assert "proposed_size" in res and isinstance(res["proposed_size"], int)
    assert "diff" in res and isinstance(res["diff"], str)

def test_doc_runbook_reader_schema():
    """Verify doc_runbook_reader returns all mandatory schema fields."""
    target = os.path.join(RUNBOOKS_DIR, "nginx_recovery.md")
    res = doc_runbook_reader(target, "Diagnostic Rapide")
    
    assert isinstance(res, dict)
    assert "found" in res and isinstance(res["found"], bool)
    assert "section_title" in res and isinstance(res["section_title"], str)
    assert "content" in res and isinstance(res["content"], str)

def test_evaluate_command_safety_schema():
    """Verify evaluate_command_safety returns action and reason."""
    res = evaluate_command_safety("echo hello")
    assert isinstance(res, dict)
    assert "action" in res and res["action"] in ["ALLOW", "APPROVAL_REQUIRED", "BLOCKED"]
    assert "reason" in res and isinstance(res["reason"], str)
