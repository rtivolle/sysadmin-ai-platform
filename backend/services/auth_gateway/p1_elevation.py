"""
Emergency P1 On-Call Elevation Manager.
Provides dynamic, time-bounded (60-minute automatic Valkey TTL) privilege elevation
for named on-call administrators tied to ticketing incident IDs.
Enforces:
- Dynamic named on-call sysadmin identity + mandatory Incident ID (INC-...).
- 60-minute TTL automatic expiration via Valkey key expiration.
- Elevated limits: 6 in-flight calls, 200 RPM, 500k TPM, 10M daily tokens.
- Cluster concurrency fairness: 2 reserved slots for P1 without GPU preemption.
- Distinct VictoriaLogs audit stream tagging (priority: P1-CRITICAL, incident_id).
- Invariants: Never bypasses Bubblewrap, command interceptor, or human approval.
"""
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Dict, Optional

import redis

from .quota_manager import QuotaManager, quota_mgr
from backend.services.agent_tools.audit import log_audit_event

logger = logging.getLogger("auth_gateway.p1_elevation")

INCIDENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,32}$")


class P1EmergencyGate:
    """
    Production Valkey-backed P1 emergency elevation manager.
    """
    def __init__(self, quota_manager: Optional[QuotaManager] = None):
        self.qm = quota_manager or quota_mgr
        self.require_shared = bool(os.getenv("VALKEY_URL"))
        self.active_p1_tokens: Dict[str, Dict[str, Any]] = {}
        self._local_user_elevations: Dict[str, Dict[str, Any]] = {}

    def issue_p1_token(
        self,
        on_call_user: str,
        incident_id: str,
        ttl_seconds: int = 3600,
        reason: str = "On-call emergency incident response"
    ) -> str:
        """
        Issues a P1 elevation token and registers elevation in Valkey and local state.
        Binds to named user and mandatory incident ID.
        """
        if not on_call_user or not incident_id:
            raise ValueError("on_call_user and incident_id are mandatory for P1 elevation")

        inc_clean = incident_id.strip()
        if not INCIDENT_ID_PATTERN.match(inc_clean):
            raise ValueError(f"Invalid incident ID format: '{incident_id}'. Must be 3-32 alphanumeric/hyphen characters.")
        if ttl_seconds > 3600:
            raise ValueError("P1 elevation cannot exceed 3600 seconds")

        token = f"p1-token-{inc_clean}-{uuid.uuid4().hex[:8]}"
        now = time.time()
        expires_at = now + ttl_seconds

        metadata = {
            "token": token,
            "user": on_call_user,
            "user_id": on_call_user,
            "incident_id": inc_clean,
            "reason": reason,
            "issued_at": now,
            "expires_at": expires_at,
            "ttl_seconds": ttl_seconds,
            "max_in_flight": 6,
            "rpm_limit": 200,
            "tpm_limit": 500_000,
            "daily_tokens": 10_000_000,
            "priority": "P1-CRITICAL"
        }

        # Persist before exposing a new elevation to callers.
        r = self.qm.redis
        if r:
            try:
                token_set = f"p1_tokens:{on_call_user}"
                with r.pipeline(transaction=True) as pipe:
                    pipe.setex(f"p1_token:{token}", max(1, int(ttl_seconds)), json.dumps(metadata))
                    pipe.setex(f"p1_elevation:{on_call_user}", max(1, int(ttl_seconds)), json.dumps(metadata))
                    pipe.sadd(token_set, token)
                    # Every token expires within one hour, so the index must live
                    # for at least one hour after the newest issue.
                    pipe.expire(token_set, 3600)
                    pipe.execute()
            except Exception as exc:
                if self.require_shared:
                    raise ConnectionError("Shared P1 elevation store unavailable") from exc
                logger.warning("Valkey P1 token write failed: %s", exc)
        elif self.require_shared:
            raise ConnectionError("Shared P1 elevation store unavailable")

        self.active_p1_tokens[token] = metadata
        self._local_user_elevations[on_call_user] = metadata

        # Audit event
        try:
            log_audit_event(
                user_id=on_call_user,
                session_id="",
                tool_name="p1_elevation_issued",
                action="p1_elevation_issued",
                parameters={"incident_id": inc_clean, "ttl_seconds": ttl_seconds, "reason": reason},
                exit_code=0,
                duration_ms=0,
                priority="P1-CRITICAL",
                incident_id=inc_clean,
            )
        except Exception as audit_err:
            logger.warning("Failed to emit P1 elevation audit event: %s", audit_err)

        return token

    def validate_p1_request(self, token: str) -> Dict[str, Any]:
        """
        Validates token existence and expiry.
        Returns: {valid: bool, priority: str, max_in_flight: int, ...}
        """
        now = time.time()
        metadata = None

        # Check Valkey first
        r = self.qm.redis
        if r:
            try:
                raw = r.get(f"p1_token:{token}")
                if raw:
                    metadata = json.loads(raw)
            except Exception as e:
                if self.require_shared:
                    return {"valid": False, "error": "Shared P1 elevation store unavailable"}
                logger.warning("Valkey P1 token lookup error: %s", e)
        elif self.require_shared:
            return {"valid": False, "error": "Shared P1 elevation store unavailable"}

        if not metadata and r is None and not self.require_shared:
            metadata = self.active_p1_tokens.get(token)

        if not metadata:
            return {"valid": False, "error": "Invalid P1 token"}

        if now >= metadata.get("expires_at", 0):
            return {"valid": False, "error": "P1 emergency token expired"}

        return {
            "valid": True,
            "user": metadata.get("user") or metadata.get("user_id"),
            "user_id": metadata.get("user_id") or metadata.get("user"),
            "incident_id": metadata.get("incident_id"),
            "priority": metadata.get("priority", "P1-CRITICAL"),
            "max_in_flight": metadata.get("max_in_flight", 6),
            "rpm_limit": metadata.get("rpm_limit", 200),
            "expires_at": metadata.get("expires_at"),
            "ttl_remaining": max(0, int(metadata.get("expires_at", 0) - now)),
        }

    def get_p1_status(self, user_id: str) -> Dict[str, Any]:
        """Queries whether a given user is currently P1-elevated."""
        now = time.time()
        metadata = None

        r = self.qm.redis
        if r:
            try:
                raw = r.get(f"p1_elevation:{user_id}")
                if raw:
                    metadata = json.loads(raw)
            except Exception as e:
                if self.require_shared:
                    return {"elevated": False, "user_id": user_id, "priority": "standard", "max_in_flight": 2, "rpm_limit": 60}
                logger.warning("Valkey p1_elevation lookup error: %s", e)

        if not metadata and r is None and not self.require_shared:
            metadata = self._local_user_elevations.get(user_id)

        if metadata and now < metadata.get("expires_at", 0):
            ttl_remaining = max(0, int(metadata.get("expires_at", 0) - now))
            return {
                "elevated": True,
                "user_id": user_id,
                "incident_id": metadata.get("incident_id"),
                "priority": "P1-CRITICAL",
                "max_in_flight": metadata.get("max_in_flight", 6),
                "rpm_limit": metadata.get("rpm_limit", 200),
                "expires_at": metadata.get("expires_at"),
                "ttl_remaining_seconds": ttl_remaining,
            }

        return {
            "elevated": False,
            "user_id": user_id,
            "priority": "standard",
            "max_in_flight": 2,
            "rpm_limit": 60,
        }

    def revoke_p1_elevation(self, user_id: str, reason: str = "Manual revocation") -> bool:
        """Revokes active P1 elevation for a user."""
        r = self.qm.redis
        token = None

        local_tokens = [tok for tok, meta in self.active_p1_tokens.items() if meta.get("user_id") == user_id]

        if r:
            try:
                tokens = set(r.smembers(f"p1_tokens:{user_id}")) | set(local_tokens)
                raw = r.get(f"p1_elevation:{user_id}")
                if raw:
                    try:
                        data = json.loads(raw)
                        token = data.get("token")
                        if token:
                            tokens.add(token)
                    except Exception:
                        pass
                with r.pipeline(transaction=True) as pipe:
                    pipe.delete(f"p1_elevation:{user_id}", f"p1_tokens:{user_id}")
                    for old_token in tokens:
                        pipe.delete(f"p1_token:{old_token}")
                    pipe.execute()
            except Exception as e:
                if self.require_shared:
                    raise ConnectionError("Shared P1 elevation store unavailable") from e
                logger.warning("Valkey revoke_p1_elevation error: %s", e)
        elif self.require_shared:
            raise ConnectionError("Shared P1 elevation store unavailable")

        self._local_user_elevations.pop(user_id, None)
        for old_token in local_tokens:
            self.active_p1_tokens.pop(old_token, None)

        # Audit event
        try:
            log_audit_event(
                user_id=user_id,
                session_id="",
                tool_name="p1_elevation_revoked",
                action="p1_elevation_revoked",
                parameters={"reason": reason},
                exit_code=0,
                duration_ms=0,
                priority="standard",
            )
        except Exception:
            pass

        return True


_GLOBAL_P1_GATE: Optional[P1EmergencyGate] = None


def get_p1_gate() -> P1EmergencyGate:
    global _GLOBAL_P1_GATE
    if _GLOBAL_P1_GATE is None:
        _GLOBAL_P1_GATE = P1EmergencyGate()
    return _GLOBAL_P1_GATE
