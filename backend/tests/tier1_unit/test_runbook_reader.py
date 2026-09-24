"""
Tier 1 Unit Test: doc_runbook_reader Section Extraction & Boundary Isolation.
Verifies precise section extraction without leaking following sections or full document.
"""
import os
import pytest

from backend.services.agent_tools.tools import doc_runbook_reader
from backend.tests.fixtures import RUNBOOKS_DIR

def test_runbook_reader_extract_section():
    """Verify primary extraction of 'Diagnostic Rapide' from nginx_recovery.md."""
    target = os.path.join(RUNBOOKS_DIR, "nginx_recovery.md")
    res = doc_runbook_reader(runbook_path=target, section_title="Diagnostic Rapide")
    
    assert res["found"] is True
    assert res["section_title"] == "Diagnostic Rapide"
    assert "systemctl status nginx" in res["content"]
    assert "grep -i error /var/log/nginx/error.log" in res["content"]
    
    # Boundary check: verify subsequent sections are NOT included
    assert "Procédure de Redémarrage" not in res["content"]
    assert "Escalade Incident P1" not in res["content"]

def test_runbook_reader_case_insensitive():
    """Verify section lookup is case-insensitive."""
    target = os.path.join(RUNBOOKS_DIR, "nginx_recovery.md")
    res = doc_runbook_reader(runbook_path=target, section_title="diagnostic rapide")
    
    assert res["found"] is True
    assert "systemctl status nginx" in res["content"]

def test_runbook_reader_pitr_extraction():
    """Verify extraction of 'Point-in-Time Recovery' from postgresql_recovery.md."""
    target = os.path.join(RUNBOOKS_DIR, "postgresql_recovery.md")
    res = doc_runbook_reader(runbook_path=target, section_title="Point-in-Time Recovery")
    
    assert res["found"] is True
    assert "restore_command" in res["content"]
    assert "recovery_target_time" in res["content"]
    assert "recovery.signal" in res["content"]
    # Check boundary
    assert "Replication Failover" not in res["content"]

def test_runbook_reader_missing_file():
    """Verify graceful handling when runbook file does not exist."""
    res = doc_runbook_reader(runbook_path="/tmp/non_existent_runbook_xyz.md", section_title="Test")
    
    assert res["found"] is False
    assert "error" in res
    assert "not found" in res["error"].lower()

def test_runbook_reader_missing_section():
    """Verify response when requested section does not exist in runbook."""
    target = os.path.join(RUNBOOKS_DIR, "nginx_recovery.md")
    res = doc_runbook_reader(runbook_path=target, section_title="Non Existent Section Title")
    
    assert res["found"] is False
    assert res["content"] == "Section not found"
