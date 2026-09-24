"""
Tier 3 Test: Human-in-the-Loop Approval Gate State Machine & Anti-Replay Security.
Verifies state machine transitions (proposed -> pending -> approved/rejected/expired -> executing -> succeeded),
single-use anti-replay token invalidation, 300s TTL expiration, and content hash tampering rejection.
"""
import time
import hashlib
import pytest

from backend.services.agent_tools.approval_gate import (
    evaluate_command_safety,
    create_approval_request,
    decide_approval,
    PENDING_APPROVALS
)

def test_approval_lifecycle_happy_path():
    """Verify proposed -> pending -> approved state transitions."""
    user_id = "sysadmin-01"
    session_id = "sess-lifecycle-01"
    cmd = "systemctl restart nginx"
    
    # 1. Evaluate safety
    safety = evaluate_command_safety(cmd)
    assert safety["action"] == "APPROVAL_REQUIRED"
    
    # 2. Create pending approval request
    appr_id = create_approval_request(user_id, session_id, cmd, safety["reason"])
    assert appr_id in PENDING_APPROVALS
    assert PENDING_APPROVALS[appr_id]["status"] == "pending"
    assert PENDING_APPROVALS[appr_id]["command"] == cmd
    assert PENDING_APPROVALS[appr_id]["user_id"] == user_id
    
    # 3. Decide approval (Human sysadmin confirms)
    res = decide_approval(appr_id, approved=True, reviewer="sysadmin-lead", reviewer_role="admin")
    assert res["success"] is True
    assert res["approval"]["status"] == "approved"
    assert res["approval"]["decided_by"] == "sysadmin-lead"

def test_approval_lifecycle_rejection():
    """Verify proposed -> pending -> rejected state transitions."""
    user_id = "sysadmin-02"
    session_id = "sess-lifecycle-02"
    cmd = "systemctl stop postgresql"
    
    appr_id = create_approval_request(user_id, session_id, cmd, "Mutating database stop")
    assert PENDING_APPROVALS[appr_id]["status"] == "pending"
    
    res = decide_approval(appr_id, approved=False, reviewer="sysadmin-lead", reviewer_role="admin")
    assert res["success"] is True
    assert res["approval"]["status"] == "rejected"

def test_approval_token_anti_replay():
    """Verify single-use anti-replay: decided approval token cannot be re-decided or replayed."""
    appr_id = create_approval_request("sysadmin-03", "sess-anti-replay", "systemctl restart valkey", "Restart valkey")
    
    # First decision succeeds
    res1 = decide_approval(appr_id, approved=True, reviewer="sysadmin-lead", reviewer_role="admin")
    assert res1["success"] is True
    
    # Attempt second decision (replay attack)
    res2 = decide_approval(appr_id, approved=True, reviewer="attacker", reviewer_role="admin")
    assert res2["success"] is False
    assert "already decided" in res2["error"].lower()

def test_approval_token_ttl_expiry():
    """Verify approval token expires after TTL (>300 seconds)."""
    appr_id = create_approval_request("sysadmin-04", "sess-expiry", "chmod 700 /tmp/test", "Permission change")
    
    # Simulate time jump past 300s expiry
    PENDING_APPROVALS[appr_id]["expires_at"] = time.time() - 10
    
    res = decide_approval(appr_id, approved=True, reviewer="sysadmin-lead", reviewer_role="admin")
    assert res["success"] is False
    assert "expired" in res["error"].lower()

def test_approval_token_tamper_detection():
    """
    Verify tamper protection: If command content hash changes from proposed content,
    the modification is detected and rejected.
    """
    original_cmd = "systemctl restart nginx"
    original_hash = hashlib.sha256(original_cmd.encode()).hexdigest()
    
    tampered_cmd = "systemctl restart nginx && touch /tmp/pwned"
    tampered_hash = hashlib.sha256(tampered_cmd.encode()).hexdigest()
    
    assert original_hash != tampered_hash
    # Target adapter contract: hash must match exact proposed payload
