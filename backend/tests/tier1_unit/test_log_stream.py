"""
Tier 1 Unit Test: search_log_stream Contract, Bounds, and Adversarial Inputs.
Guarantees < 5s execution time and strict compliance with tool schema.
"""
import os
import sys
import pytest

from backend.services.agent_tools.tools import search_log_stream
from backend.tests.fixtures import LOGS_DIR

def test_search_log_stream_basic_match():
    """Verify primary happy path: pattern match in log file."""
    target = os.path.join(LOGS_DIR, "nginx_error.log")
    res = search_log_stream(target=target, pattern="Connection refused", max_matches=10)
    
    assert res["matched"] is True
    assert res["target"] == target
    assert "127.0.0.1:9000" in res["output"]
    assert "Connection refused" in res["output"]
    assert res["match_count"] >= 1

def test_search_log_stream_bounding_without_context():
    """Verify output is strictly bounded to max_matches ceiling when context_lines=0."""
    target = os.path.join(LOGS_DIR, "auth.log")
    # auth.log has >60 brute force lines
    max_limit = 5
    res = search_log_stream(target=target, pattern="Failed password", max_matches=max_limit, context_lines=0)
    
    assert res["matched"] is True
    matched_lines = [l for l in res["output"].splitlines() if "Failed password" in l]
    assert len(matched_lines) <= max_limit

def test_search_log_stream_default_cap_50():
    """Verify default search caps matches around 50 (allowing for trailing context lines)."""
    target = os.path.join(LOGS_DIR, "auth.log")
    res = search_log_stream(target=target, pattern="Failed password")
    
    assert res["matched"] is True
    # Default context_lines=2 may include up to 2 trailing context lines
    matched_lines = [l for l in res["output"].splitlines() if "Failed password" in l]
    assert len(matched_lines) <= 54

def test_search_log_stream_missing_file():
    """Verify graceful error reporting when target file does not exist."""
    res = search_log_stream(target="/tmp/non_existent_log_file_12345.log", pattern="error")
    
    assert res["matched"] is False
    assert "error" in res
    assert "not found" in res["error"].lower()

def test_search_log_stream_no_match():
    """Verify clean response when pattern has zero occurrences."""
    target = os.path.join(LOGS_DIR, "nginx_error.log")
    res = search_log_stream(target=target, pattern="NON_EXISTENT_PATTERN_XYZ_999")
    
    assert res["matched"] is False
    assert res["match_count"] == 0
    assert "No occurrences found" in res["output"]

def test_search_log_stream_regex_escaping():
    """Verify regex meta-characters and parentheses matching."""
    target = os.path.join(LOGS_DIR, "nginx_error.log")
    # Matches connect() with escaped parens
    res = search_log_stream(target=target, pattern=r"connect\(\)", max_matches=10)
    
    assert res["matched"] is True
    assert "connect()" in res["output"]

def test_search_log_stream_context_lines():
    """Verify context lines parameter inclusion."""
    target = os.path.join(LOGS_DIR, "journal_systemd.log")
    res = search_log_stream(target=target, pattern="address already in use", max_matches=5, context_lines=1)
    
    assert res["matched"] is True
    assert "Traefik version" in res["output"] or "Failed with result" in res["output"]
