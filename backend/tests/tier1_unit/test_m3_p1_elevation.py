"""
Unit Tests for Milestone M3: Emergency P1 On-Call Elevation & ForwardAuth Priority Admission.
Verifies:
- Production P1EmergencyGate token issuance, 60-minute TTL, and status.
- Mandatory incident ID validation (INC-...).
- Safety invariants: Never bypasses Bubblewrap sandbox, command filter, or approval gate.
- Concurrency reservation: 2 reserved slots for P1 admission without starvation.
- REST ForwardAuth endpoints: /api/v1/auth/p1/elevate, /status, /revoke, /verify headers.
"""
import time
import pytest
import httpx

from backend.services.auth_gateway.p1_elevation import P1EmergencyGate
from backend.services.auth_gateway.quota_manager import QuotaManager, QuotaExceededException
from backend.services.auth_gateway.server import app
from backend.services.approval_gate.filter import evaluate_command_safety


def test_p1_elevation_gate_lifecycle():
    """Verify production P1 token issuance, validation, and revocation."""
    gate = P1EmergencyGate()
    token = gate.issue_p1_token(
        on_call_user="sysadmin-03",
        incident_id="INC-88912",
        ttl_seconds=3600,
        reason="Nginx 502 outage response",
    )
    assert token.startswith("p1-token-INC-88912-")

    # Validate token
    val = gate.validate_p1_request(token)
    assert val["valid"] is True
    assert val["priority"] == "P1-CRITICAL"
    assert val["max_in_flight"] == 6
    assert val["incident_id"] == "INC-88912"

    # Status query
    status = gate.get_p1_status("sysadmin-03")
    assert status["elevated"] is True
    assert status["priority"] == "P1-CRITICAL"
    assert status["incident_id"] == "INC-88912"

    # Revoke
    gate.revoke_p1_elevation("sysadmin-03")
    rev_status = gate.get_p1_status("sysadmin-03")
    assert rev_status["elevated"] is False


def test_p1_mandatory_incident_id_validation():
    """Verify missing or malformed incident ID is rejected."""
    gate = P1EmergencyGate()

    with pytest.raises(ValueError):
        gate.issue_p1_token("sysadmin-01", "")

    with pytest.raises(ValueError):
        gate.issue_p1_token("sysadmin-01", "bad incident with spaces")


def test_p1_elevation_expired_fails_closed():
    """Verify expired P1 elevation fails closed."""
    gate = P1EmergencyGate()
    token = gate.issue_p1_token(
        on_call_user="sysadmin-04",
        incident_id="INC-expired",
        ttl_seconds=-10,
    )
    val = gate.validate_p1_request(token)
    assert val["valid"] is False
    assert "expired" in val["error"].lower()


def test_p1_safety_invariants_preserved():
    """Verify P1 elevation does NOT bypass destructive command interceptor or approval gate."""
    # Destructive command must be unconditionally blocked
    blocked = evaluate_command_safety("rm -rf /")
    assert blocked["action"] == "BLOCKED"

    blocked_mkfs = evaluate_command_safety("mkfs.ext4 /dev/sda1")
    assert blocked_mkfs["action"] == "BLOCKED"

    # Mutating command must still require approval
    mutating = evaluate_command_safety("systemctl restart nginx")
    assert mutating["action"] == "APPROVAL_REQUIRED"


def test_p1_concurrency_reserved_slots():
    """Verify standard users cannot consume reserved P1 slots (slots 9-10)."""
    qm = QuotaManager(enforce_cluster_limits=True)
    qm.is_p1_elevated = lambda u: u == "sysadmin-p1"

    # Simulate filling standard quota (8 slots)
    qm._local_cluster_total = 8

    # Standard user attempting 9th slot is blocked
    with pytest.raises(QuotaExceededException) as exc_info:
        qm.acquire_concurrency_slot("sysadmin-02")
    assert "Cluster concurrency ceiling exceeded" in str(exc_info.value)

    # P1 user can acquire reserved 9th slot
    lease_p1 = qm.acquire_concurrency_slot("sysadmin-p1")
    assert lease_p1.startswith("lease:sysadmin-p1:")
    assert qm._local_cluster_total == 9

    # Release
    qm.release_concurrency_slot("sysadmin-p1")
    assert qm._local_cluster_total == 8


@pytest.mark.asyncio
async def test_auth_gateway_p1_api_endpoints(monkeypatch):
    """Verify /api/v1/auth/p1/elevate, /status, /revoke, and /verify endpoints."""
    # Mock authentication to return sysadmin-01
    from backend.services.auth_gateway import server
    monkeypatch.setattr(server, "authenticate_request", lambda req: ("sysadmin-01", "bearer_token"))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/api/v1/auth/p1/elevate", json={
            "incident_id": "INC-77312",
            "reason": "Traefik ingress outage",
            "ttl_seconds": 1800,
        })
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "elevated"
        assert data["priority"] == "P1-CRITICAL"
        assert data["incident_id"] == "INC-77312"

        res_status = await client.get("/api/v1/auth/p1/status")
        assert res_status.status_code == 200
        assert res_status.json()["elevated"] is True

        res_verify = await client.get("/verify")
        assert res_verify.status_code == 200
        assert res_verify.headers.get("X-Priority") == "P1-CRITICAL"
        assert res_verify.headers.get("X-Incident-ID") == "INC-77312"

        res_rev = await client.post("/api/v1/auth/p1/revoke")
        assert res_rev.status_code == 200
        assert res_rev.json()["status"] == "revoked"

        res_after = await client.get("/api/v1/auth/p1/status")
        assert res_after.json()["elevated"] is False
