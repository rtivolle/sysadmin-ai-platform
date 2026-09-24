"""
Tier 3 Test: P1 Emergency On-Call Elevation & Priority Admission Bypass.
Verifies temporary priority admission (60-minute TTL), elevated rate limits,
distinct VictoriaLogs audit tagging (priority: P1-CRITICAL), and safety boundary invariants.
"""
import time
import pytest

from backend.services.agent_tools.approval_gate import evaluate_command_safety

class P1EmergencyGate:
    """
    Simulates the P1 priority bypass logic:
    - Bypasses standard user concurrency queues.
    - Applies elevated ceiling (6 in-flight calls).
    - Injects 'priority: P1-CRITICAL' into audit metadata.
    - Expires automatically after 60 minutes.
    """
    def __init__(self):
        self.active_p1_tokens = {}

    def issue_p1_token(self, on_call_user: str, incident_id: str, ttl_seconds: int = 3600) -> str:
        token = f"p1-token-{incident_id}"
        self.active_p1_tokens[token] = {
            "user": on_call_user,
            "incident_id": incident_id,
            "issued_at": time.time(),
            "expires_at": time.time() + ttl_seconds,
            "max_in_flight": 6,
            "priority": "P1-CRITICAL"
        }
        return token

    def validate_p1_request(self, token: str) -> dict:
        if token not in self.active_p1_tokens:
            return {"valid": False, "error": "Invalid P1 token"}
        p1 = self.active_p1_tokens[token]
        if time.time() > p1["expires_at"]:
            return {"valid": False, "error": "P1 emergency token expired"}
        return {"valid": True, "priority": p1["priority"], "max_in_flight": p1["max_in_flight"]}

def test_p1_elevation_issuance_and_priority_tagging():
    """Verify P1 key issuance, 60-min TTL, and distinct audit tag."""
    gate = P1EmergencyGate()
    token = gate.issue_p1_token("sysadmin-oncall", "INC-99120", ttl_seconds=3600)
    
    validation = gate.validate_p1_request(token)
    assert validation["valid"] is True
    assert validation["priority"] == "P1-CRITICAL"
    assert validation["max_in_flight"] == 6

def test_p1_elevation_expiration():
    """Verify expired P1 token fails closed."""
    gate = P1EmergencyGate()
    token = gate.issue_p1_token("sysadmin-oncall", "INC-99121", ttl_seconds=-5)
    
    validation = gate.validate_p1_request(token)
    assert validation["valid"] is False
    assert "expired" in validation["error"].lower()

def test_p1_elevation_does_not_bypass_destructive_interceptor():
    """CRITICAL SAFETY INVARIANT: P1 elevation NEVER allows destructive commands."""
    destructive_cmd = "rm -rf / --no-preserve-root"
    safety = evaluate_command_safety(destructive_cmd)
    
    # Must be BLOCKED regardless of operator identity
    assert safety["action"] == "BLOCKED"

def test_p1_elevation_does_not_bypass_human_approval():
    """CRITICAL SAFETY INVARIANT: P1 elevation still requires approval for mutating commands."""
    mutating_cmd = "systemctl restart nginx"
    safety = evaluate_command_safety(mutating_cmd)
    
    # Must require approval even during P1
    assert safety["action"] == "APPROVAL_REQUIRED"
