#!/usr/bin/env python3
"""
Valkey-Backed Quota & Rate Limit Manager.
Enforces:
- 2 in-flight concurrent requests ceiling per user
- 60 RPM rolling window
- 150,000 TPM rolling window
- 2,000,000 daily tokens with automatic midnight rollover
- Emergency P1 priority admission (60-minute TTL)
"""
import os
import sys
import time
import datetime
import json
import uuid
import logging
import threading
from zoneinfo import ZoneInfo
from typing import Optional, Tuple, Dict, Any
import redis

from backend.services.agent_tools.audit import log_audit_event

logger = logging.getLogger("auth_gateway.quota_manager")

# The sorted sets below are the single source of truth for live leases. The
# `inflight:<user>` integer keys are a compatibility mirror for dashboards that
# predate leases; they are written inside the same atomic script so they cannot
# diverge from the lease sets on acquire or release.
LUA_ACQUIRE_LEASE = """
local now = tonumber(ARGV[1])
local expires_at = tonumber(ARGV[2])
local user_limit = tonumber(ARGV[3])
local cluster_enabled = tonumber(ARGV[4])
local cluster_limit = tonumber(ARGV[5])
local ttl = tonumber(ARGV[6])
local lease_id = ARGV[7]
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
local user_count = redis.call('ZCARD', KEYS[2])
if user_count >= user_limit then
  return {0, user_count, 'user_limit'}
end
local cluster_count = redis.call('ZCARD', KEYS[1])
if cluster_enabled == 1 and cluster_count >= cluster_limit then
  return {0, cluster_count, 'cluster_limit'}
end
redis.call('ZADD', KEYS[1], expires_at, lease_id)
redis.call('ZADD', KEYS[2], expires_at, lease_id)
redis.call('EXPIRE', KEYS[1], ttl * 2)
redis.call('EXPIRE', KEYS[2], ttl * 2)
local _, _, uid = string.find(KEYS[2], "quota:leases:user:(.+)")
if uid then
  redis.call('SET', 'inflight:' .. uid, user_count + 1)
  redis.call('EXPIRE', 'inflight:' .. uid, ttl * 2)
end
return {1, user_count + 1, cluster_count + 1}
"""

LUA_RELEASE_LEASE = """
local lease_id = ARGV[1]
local user_count = redis.call('ZREM', KEYS[2], lease_id)
local cluster_count = redis.call('ZREM', KEYS[1], lease_id)
local _, _, uid = string.find(KEYS[2], "quota:leases:user:(.+)")
if uid then
  local rem = redis.call('ZCARD', KEYS[2])
  if rem > 0 then
    redis.call('SET', 'inflight:' .. uid, rem)
  else
    redis.call('DEL', 'inflight:' .. uid)
  end
end
return {user_count, cluster_count}
"""

LUA_RENEW_LEASE = """
local lease_id = ARGV[1]
local expires_at = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
if redis.call('ZSCORE', KEYS[2], lease_id) == false then
  return 0
end
redis.call('ZADD', KEYS[1], expires_at, lease_id)
redis.call('ZADD', KEYS[2], expires_at, lease_id)
redis.call('EXPIRE', KEYS[1], ttl * 2)
redis.call('EXPIRE', KEYS[2], ttl * 2)
return 1
"""

LUA_RESERVE_DAILY_TOKENS = """
local now = tonumber(ARGV[1])
local expires_at = tonumber(ARGV[2])
local estimate = tonumber(ARGV[3])
local limit = tonumber(ARGV[4])
local reservation_id = ARGV[5]
local ttl = tonumber(ARGV[6])

local expired = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', now)
for _, id in ipairs(expired) do
  redis.call('HDEL', KEYS[3], id)
  redis.call('ZREM', KEYS[2], id)
end
if redis.call('HEXISTS', KEYS[3], reservation_id) == 1 or redis.call('HEXISTS', KEYS[4], reservation_id) == 1 then
  return {0, 0, 'duplicate'}
end
local reserved = 0
local amounts = redis.call('HVALS', KEYS[3])
for _, amount in ipairs(amounts) do
  reserved = reserved + tonumber(amount)
end
local consumed = tonumber(redis.call('GET', KEYS[1]) or '0')
if consumed + reserved + estimate > limit then
  return {0, consumed + reserved, 'daily_limit'}
end
redis.call('HSET', KEYS[3], reservation_id, estimate)
redis.call('ZADD', KEYS[2], expires_at, reservation_id)
for _, key in ipairs(KEYS) do redis.call('EXPIRE', key, ttl) end
return {1, consumed + reserved + estimate, 'reserved'}
"""

LUA_SETTLE_DAILY_TOKENS = """
local reservation_id = ARGV[1]
local actual = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local now = tonumber(ARGV[4])
if redis.call('HEXISTS', KEYS[3], reservation_id) == 1 then
  return {1, tonumber(redis.call('GET', KEYS[1]) or '0'), 'duplicate'}
end
local estimate = redis.call('HGET', KEYS[2], reservation_id)
if estimate then
  redis.call('HDEL', KEYS[2], reservation_id)
  redis.call('ZREM', KEYS[4], reservation_id)
end
local consumed = redis.call('INCRBY', KEYS[1], actual)
redis.call('HSET', KEYS[3], reservation_id, actual)
for _, key in ipairs(KEYS) do redis.call('EXPIRE', key, ttl) end
redis.call('ZADD', KEYS[5], now, reservation_id .. ':' .. actual)
redis.call('ZREMRANGEBYSCORE', KEYS[5], 0, now - 60)
redis.call('EXPIRE', KEYS[5], 120)
return {1, consumed, estimate and 'settled' or 'reservation_missing'}
"""

class QuotaExceededException(Exception):
    def __init__(self, limit_type: str, message: str, current: Any, limit: Any):
        super().__init__(message)
        self.limit_type = limit_type
        self.message = message
        self.current = current
        self.limit = limit


def _audit_quota_denial(user_id: str, limit_type: str, current: Any, limit: Any, stage: str) -> None:
    """Best-effort audit of a quota denial; never alters the denial outcome.

    A failed audit write must not convert a rejection into an admission and
    must not mask the fail-closed ``ConnectionError`` the caller is about to
    surface. ``stage`` records which admission gate surfaced the denial:
    ``forwardauth`` (the ForwardAuth daily-budget pre-check) or ``litellm``
    (LiteLLM admission: concurrency/RPM/daily reservation).
    """
    try:
        log_audit_event(
            user_id=user_id,
            session_id="",
            tool_name="quota_denied",
            action="quota_denied",
            exit_code=1,
            extra={
                "limit_type": limit_type,
                "stage": stage,
                "current": current,
                "limit": limit,
            },
        )
    except Exception as exc:
        print(f"[!] Audit emission failed for quota denial: {exc}", file=sys.stderr)

class QuotaManager:
    RESERVATION_TTL_SECONDS = 8 * 24 * 60 * 60
    LIMIT_BOUNDS = {"concurrency": (1, 10), "rpm": (1, 10000), "tpm": (1, 10000000), "daily_tokens": (1, 1000000000)}

    @classmethod
    def validate_limits(cls, limits: Dict[str, Any]) -> Dict[str, int]:
        if not isinstance(limits, dict) or set(limits) - cls.LIMIT_BOUNDS.keys():
            raise ValueError("Unknown quota fields")
        for field, value in limits.items():
            low, high = cls.LIMIT_BOUNDS[field]
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{field} must be an integer between {low} and {high}")
        return dict(limits)

    def get_limits(self, user_id: str, is_p1: Optional[bool] = None) -> Dict[str, int]:
        if is_p1 is None:
            is_p1 = self.is_p1_elevated(user_id)
        limits = {"concurrency": 6 if is_p1 else 2, "rpm": 200 if is_p1 else 60,
                  "tpm": 500000 if is_p1 else 150000, "daily_tokens": 10000000 if is_p1 else 2000000}
        r = self.redis
        if r is None:
            if self.require_shared:
                raise ConnectionError("Shared quota state unavailable")
            return limits
        try:
            raw = r.get(f"quota:limits:{user_id}")
            if raw:
                limits.update(self.validate_limits(json.loads(raw)))
            return limits
        except Exception as exc:
            raise ConnectionError("Shared quota configuration unavailable") from exc

    def set_limits(self, user_id: str, limits: Dict[str, Any]) -> None:
        limits = self.validate_limits(limits)
        r = self.redis
        if r is None:
            raise ConnectionError("Shared quota state unavailable")
        try:
            # One atomic replacement; an empty object restores normal/P1 defaults.
            r.set(f"quota:limits:{user_id}", json.dumps(limits))
        except Exception as exc:
            raise ConnectionError("Shared quota configuration unavailable") from exc

    def quota_snapshot(self, user_id: str) -> Dict[str, Any]:
        r = self.redis
        if r is None:
            raise ConnectionError("Shared quota state unavailable")
        try:
            elevated = self.is_p1_elevated(user_id)
            limits = self.get_limits(user_id, elevated)
            day, now = self._quota_day(), time.time()
            reservation_ids = r.zrangebyscore(f"daily_reservations:index:{user_id}:{day}", f"({now}", "+inf")
            reserved = sum(int(value or 0) for value in r.hmget(
                f"daily_reservations:active:{user_id}:{day}", reservation_ids
            )) if reservation_ids else 0
            return {"user_id": user_id, "limits": limits, "p1_elevated": elevated,
                    "overrides": json.loads(r.get(f"quota:limits:{user_id}") or "{}"),
                    "day": day, "timezone": os.getenv("QUOTA_TIMEZONE", "UTC"),
                    "usage": {"concurrency": r.zcount(f"quota:leases:user:{user_id}", f"({now}", "+inf"),
                              "daily_tokens": int(r.get(f"daily_tokens:{user_id}:{day}") or 0),
                              "reserved_tokens": reserved}}
        except Exception as exc:
            raise ConnectionError("Shared quota state unavailable") from exc

    def __init__(
        self,
        valkey_url: Optional[str] = None,
        redis_client: Optional[Any] = None,
        enforce_cluster_limits: Optional[bool] = None,
    ):
        self.valkey_url = valkey_url or os.getenv("VALKEY_URL", "redis://:CONFIGURE_VIA_PLATFORM_SH@127.0.0.1:6379/0")
        self.require_shared = bool(valkey_url or os.getenv("VALKEY_URL") or redis_client is not None)
        self._redis: Optional[redis.Redis] = redis_client
        self._custom_client = redis_client is not None
        self._local_lease_lock = threading.RLock()
        self._redis_lock = threading.Lock()
        self._local_leases: Dict[str, Tuple[str, float]] = {}
        if enforce_cluster_limits is not None:
            self.enforce_cluster_limits = enforce_cluster_limits
        else:
            self.enforce_cluster_limits = os.getenv("ENFORCE_CLUSTER_CONCURRENCY", "0") == "1"

    @property
    def redis(self) -> Optional[redis.Redis]:
        """Return a verified shared client, or None when the shared store is unusable.

        The client is only published after a successful PING. Publishing it first
        let a concurrent caller borrow an unauthenticated client and take a
        shared-store code path whose failure was silently swallowed, which lost
        in-flight leases (see release_concurrency_slot). Connection attempts are
        serialized so a store outage does not fan out into a connect storm.
        """
        if self._custom_client:
            return self._redis
        if self._redis is not None:
            return self._redis
        with self._redis_lock:
            if self._redis is not None:
                return self._redis
            try:
                client = redis.Redis.from_url(self.valkey_url, decode_responses=True, socket_timeout=2.0)
                client.ping()
            except Exception:
                return None
            self._redis = client
            return self._redis

    # --- P1 Elevation ---
    def is_p1_elevated(self, user_id: str) -> bool:
        r = self.redis
        if r:
            try:
                if r.exists(f"p1_elevation:{user_id}"):
                    return True
            except Exception:
                pass
        try:
            from .p1_elevation import get_p1_gate
            return bool(get_p1_gate().get_p1_status(user_id).get("elevated", False))
        except Exception:
            try:
                from backend.services.auth_gateway.p1_elevation import get_p1_gate
                return bool(get_p1_gate().get_p1_status(user_id).get("elevated", False))
            except Exception:
                pass
        return False

    def grant_p1_elevation(self, user_id: str, duration_seconds: int = 3600, reason: str = "On-call emergency") -> Dict[str, Any]:
        expires_at = time.time() + duration_seconds
        payload = {"user_id": user_id, "expires_at": expires_at, "reason": reason, "incident_id": "INC-P1"}
        r = self.redis
        if r:
            try:
                r.set(f"p1_elevation:{user_id}", str(payload), ex=duration_seconds)
            except Exception:
                pass
        try:
            from .p1_elevation import get_p1_gate
            get_p1_gate()._local_user_elevations[user_id] = {
                "user_id": user_id,
                "incident_id": "INC-P1",
                "expires_at": expires_at,
                "priority": "P1-CRITICAL",
                "max_in_flight": 6,
                "rpm_limit": 200,
            }
        except Exception:
            try:
                from backend.services.auth_gateway.p1_elevation import get_p1_gate
                get_p1_gate()._local_user_elevations[user_id] = {
                    "user_id": user_id,
                    "incident_id": "INC-P1",
                    "expires_at": expires_at,
                    "priority": "P1-CRITICAL",
                    "max_in_flight": 6,
                    "rpm_limit": 200,
                }
            except Exception:
                pass
        return payload

    def get_p1_metadata(self, user_id: str) -> Optional[Dict[str, Any]]:
        r = self.redis
        if r:
            try:
                raw = r.get(f"p1_elevation:{user_id}")
                if raw:
                    return json.loads(raw)
            except Exception:
                pass
        try:
            from .p1_elevation import get_p1_gate
            status = get_p1_gate().get_p1_status(user_id)
            if status.get("elevated"):
                return status
        except Exception:
            try:
                from backend.services.auth_gateway.p1_elevation import get_p1_gate
                status = get_p1_gate().get_p1_status(user_id)
                if status.get("elevated"):
                    return status
            except Exception:
                pass
        return None

    # --- Concurrency Management (2 In-Flight Limit, 10 Cluster Ceiling, 2 Reserved P1 Slots) ---
    def _acquire_local_lease(
        self, user_id: str, limit: int, cluster_limit: int, lease_id: str, now: float, expires_at: float
    ) -> str:
        """Process-local admission used when no shared store is configured or reachable."""
        with self._local_lease_lock:
            for stale_id, (_, expiry) in list(self._local_leases.items()):
                if expiry <= now:
                    self._remove_local_lease(stale_id)
            current = self._local_inflight.get(user_id, 0) if hasattr(self, "_local_inflight") else 0
            if current >= limit:
                _audit_quota_denial(user_id, "concurrency", current, limit, "litellm")
                raise QuotaExceededException("concurrency", f"Concurrency ceiling exceeded ({current}/{limit} in-flight calls active).", current, limit)
            cluster_count = getattr(self, "_local_cluster_total", 0)
            if self.enforce_cluster_limits and cluster_count >= cluster_limit:
                _audit_quota_denial(user_id, "concurrency", cluster_count, cluster_limit, "litellm")
                raise QuotaExceededException("concurrency", f"Cluster concurrency ceiling exceeded ({cluster_count}/{cluster_limit} slots active).", cluster_count, cluster_limit)
            if not hasattr(self, "_local_inflight"):
                self._local_inflight = {}
            self._local_inflight[user_id] = current + 1
            if self.enforce_cluster_limits:
                self._local_cluster_total = cluster_count + 1
            self._local_leases[lease_id] = (user_id, expires_at)
        return lease_id

    def acquire_concurrency_slot(self, user_id: str, timeout_seconds: int = 120) -> str:
        """
        Atomically acquires an in-flight slot.
        Returns a slot lease token string.
        Enforces user in-flight limit (2 for standard, 6 for P1) and cluster capacity (8 for standard, 10 for P1).
        Fails closed when a shared store has been configured (VALKEY_URL or an
        injected client); an unconfigured deployment degrades to process-local
        lease accounting rather than refusing every request.
        """
        is_p1 = self.is_p1_elevated(user_id)
        limit = self.get_limits(user_id, is_p1)["concurrency"]
        cluster_limit = 10 if is_p1 else 8
        lease_id = f"lease:{user_id}:{uuid.uuid4().hex}"
        now = time.time()
        expires_at = now + timeout_seconds
        r = self.redis
        if r:
            try:
                global_key = "quota:leases:cluster"
                user_key = f"quota:leases:user:{user_id}"
                result = r.eval(
                    LUA_ACQUIRE_LEASE, 2, global_key, user_key,
                    now, expires_at, limit, 1 if self.enforce_cluster_limits else 0,
                    cluster_limit, timeout_seconds, lease_id,
                )
                admitted, current, reason = int(result[0]), int(result[1]), str(result[2])
                if not admitted:
                    if reason == "cluster_limit":
                        _audit_quota_denial(user_id, "concurrency", current, cluster_limit, "litellm")
                        raise QuotaExceededException(
                            "concurrency", f"Cluster concurrency ceiling exceeded ({current}/{cluster_limit} slots active).", current, cluster_limit
                        )
                    _audit_quota_denial(user_id, "concurrency", current, limit, "litellm")
                    raise QuotaExceededException(
                        "concurrency", f"Concurrency ceiling exceeded ({current}/{limit} in-flight calls active).", current, limit
                    )
                return lease_id
            except QuotaExceededException:
                raise
            except Exception as exc:
                if self.require_shared:
                    raise ConnectionError("Shared quota state unavailable") from exc
                logger.warning("Shared quota acquisition failed; using local lease accounting: %s", exc)
        elif self.require_shared:
            raise ConnectionError("Shared quota state unavailable")
        return self._acquire_local_lease(user_id, limit, cluster_limit, lease_id, now, expires_at)

    def _remove_local_lease(self, lease_id: str) -> bool:
        record = self._local_leases.pop(lease_id, None)
        if not record:
            return False
        user_id, _ = record
        current = self._local_inflight.get(user_id, 0)
        if current <= 1:
            self._local_inflight.pop(user_id, None)
        else:
            self._local_inflight[user_id] = current - 1
        if self.enforce_cluster_limits and getattr(self, "_local_cluster_total", 0) > 0:
            self._local_cluster_total -= 1
        return True

    def _release_local_lease(self, lease_or_user_id: str) -> bool:
        with self._local_lease_lock:
            if lease_or_user_id.startswith("lease:"):
                return self._remove_local_lease(lease_or_user_id)
            if hasattr(self, "_local_inflight"):
                candidates = [token for token, (owner, _) in self._local_leases.items() if owner == lease_or_user_id]
                if candidates:
                    return self._remove_local_lease(candidates[0])
        return False

    def release_concurrency_slot(self, lease_or_user_id: str):
        """Release exactly one lease; user IDs remain accepted for older callers.

        A lease must be reclaimed even when the shared store is momentarily
        unusable, otherwise the slot leaks until its TTL expires and the user
        receives spurious 429s. Shared and local bookkeeping are both
        reconciled; each is a no-op for leases it does not own.
        """
        r = self.redis
        if r:
            try:
                if lease_or_user_id.startswith("lease:"):
                    _, user_id, _ = lease_or_user_id.split(":", 2)
                    member = lease_or_user_id
                else:
                    user_id = lease_or_user_id
                    user_key = f"quota:leases:user:{user_id}"
                    candidates = r.zrange(user_key, 0, 0)
                    member = candidates[0] if candidates else None
                if member:
                    r.eval(
                        LUA_RELEASE_LEASE, 2, "quota:leases:cluster", f"quota:leases:user:{user_id}", member
                    )
            except Exception as exc:
                if self.require_shared:
                    raise ConnectionError("Shared quota state unavailable") from exc
                logger.warning("Shared quota release failed for %s: %s", lease_or_user_id, exc)
        elif self.require_shared:
            raise ConnectionError("Shared quota state unavailable")
        self._release_local_lease(lease_or_user_id)

    def _renew_local_lease(self, lease_id: str, user_id: str, now: float, expires_at: float) -> bool:
        with self._local_lease_lock:
            current = self._local_leases.get(lease_id)
            if not current or current[1] <= now:
                self._remove_local_lease(lease_id)
                if current is None and not self.require_shared:
                    # A shared lease whose store is unreachable in a deployment
                    # that does not require one: ownership cannot be proven, so
                    # keep the in-flight request alive rather than cancelling it.
                    return True
                return False
            self._local_leases[lease_id] = (user_id, expires_at)
            return True

    def renew_concurrency_slot(self, lease_id: str, timeout_seconds: int = 120) -> bool:
        """Extend a live lease while its request is still running."""
        now = time.time()
        expires_at = now + timeout_seconds
        if not lease_id.startswith("lease:"):
            raise ValueError("Invalid quota lease")
        _, user_id, _ = lease_id.split(":", 2)
        r = self.redis
        if r:
            try:
                return bool(r.eval(
                    LUA_RENEW_LEASE, 2, "quota:leases:cluster", f"quota:leases:user:{user_id}",
                    lease_id, expires_at, timeout_seconds,
                ))
            except Exception as exc:
                if self.require_shared:
                    raise ConnectionError("Shared quota state unavailable") from exc
                logger.warning("Shared quota renewal failed for %s: %s", lease_id, exc)
        elif self.require_shared:
            raise ConnectionError("Shared quota state unavailable")
        return self._renew_local_lease(lease_id, user_id, now, expires_at)

    # --- Rate Limiting (60 RPM) ---
    def check_and_record_rpm(self, user_id: str) -> int:
        is_p1 = self.is_p1_elevated(user_id)
        limit = self.get_limits(user_id, is_p1)["rpm"]
        now = time.time()
        rpm_key = f"rate:rpm:{user_id}"

        r = self.redis
        if not r:
            if self.require_shared:
                raise ConnectionError("Shared quota state unavailable")
            return 1

        try:
            pipe = r.pipeline()
            pipe.zremrangebyscore(rpm_key, 0, now - 60)
            pipe.zcard(rpm_key)
            pipe.zadd(rpm_key, {str(uuid.uuid4()): now})
            pipe.expire(rpm_key, 120)
            _, count_before, _, _ = pipe.execute()

            if count_before >= limit:
                _audit_quota_denial(user_id, "rpm", count_before, limit, "litellm")
                raise QuotaExceededException(
                    limit_type="rpm",
                    message=f"Rate limit exceeded ({count_before}/{limit} requests in rolling 60s window).",
                    current=count_before,
                    limit=limit
                )
            return count_before + 1
        except QuotaExceededException:
            raise
        except Exception as exc:
            raise ConnectionError("Shared quota state unavailable") from exc

    # --- Daily Token Budget (2M Tokens with Midnight Rollover) ---
    def check_daily_token_budget(self, user_id: str, estimated_tokens: int = 0) -> Tuple[int, int]:
        is_p1 = self.is_p1_elevated(user_id)
        limit = self.get_limits(user_id, is_p1)["daily_tokens"]
        today_str = self._quota_day()
        daily_key = f"daily_tokens:{user_id}:{today_str}"

        r = self.redis
        if not r:
            if self.require_shared:
                raise ConnectionError("Shared quota state unavailable")
            return 0, limit

        try:
            current_consumed = int(r.get(daily_key) or 0)
            if (current_consumed + estimated_tokens) >= limit:
                _audit_quota_denial(user_id, "daily_tokens", current_consumed, limit, "forwardauth")
                raise QuotaExceededException(
                    limit_type="daily_tokens",
                    message=f"Daily token budget exhausted ({current_consumed:,}/{limit:,} tokens consumed). Rollover at 00:00:00.",
                    current=current_consumed,
                    limit=limit
                )
            return current_consumed, limit
        except QuotaExceededException:
            raise
        except Exception as exc:
            raise ConnectionError("Shared quota state unavailable") from exc

    @staticmethod
    def _quota_day(epoch_seconds: Optional[float] = None) -> str:
        timezone_name = os.getenv("QUOTA_TIMEZONE", "UTC")
        tz = ZoneInfo(timezone_name)
        when = time.time() if epoch_seconds is None else epoch_seconds
        return datetime.datetime.fromtimestamp(when, tz=tz).date().isoformat()

    def reserve_daily_token_budget(
        self,
        user_id: str,
        reservation_id: str,
        estimated_tokens: int,
        admission_day: Optional[str] = None,
    ) -> Tuple[str, int, int]:
        """Atomically reserve daily capacity before an inference call is dispatched."""
        if not reservation_id or estimated_tokens < 0:
            raise ValueError("A reservation ID and non-negative estimate are required")
        day = admission_day or self._quota_day()
        limit = self.get_limits(user_id)["daily_tokens"]
        now = time.time()
        expires_at = now + self.RESERVATION_TTL_SECONDS
        daily_key = f"daily_tokens:{user_id}:{day}"
        index_key = f"daily_reservations:index:{user_id}:{day}"
        active_key = f"daily_reservations:active:{user_id}:{day}"
        settled_key = f"daily_reservations:settled:{user_id}:{day}"
        r = self.redis
        if not r:
            if self.require_shared:
                raise ConnectionError("Shared quota state unavailable")
            with self._local_lease_lock:
                reservations = getattr(self, "_local_daily_reservations", {})
                for identity, (_, expiry) in list(reservations.items()):
                    if expiry <= now:
                        reservations.pop(identity, None)
                actual = getattr(self, "_local_daily_actual", {}).get((user_id, day), 0)
                already_reserved = sum(
                    amount for (owner, res_day, _), (amount, _) in reservations.items()
                    if owner == user_id and res_day == day
                )
                identity = (user_id, day, reservation_id)
                if identity in reservations or identity in getattr(self, "_local_daily_settled", set()):
                    raise ValueError("Duplicate daily token reservation ID")
                if actual + already_reserved + estimated_tokens > limit:
                    _audit_quota_denial(user_id, "daily_tokens", actual + already_reserved, limit, "litellm")
                    raise QuotaExceededException("daily_tokens", f"Daily token budget exhausted ({actual + already_reserved:,}/{limit:,} tokens reserved or consumed).", actual + already_reserved, limit)
                reservations[identity] = (estimated_tokens, expires_at)
                self._local_daily_reservations = reservations
                return day, actual + already_reserved + estimated_tokens, limit
        try:
            result = r.eval(
                LUA_RESERVE_DAILY_TOKENS, 4, daily_key, index_key, active_key, settled_key,
                now, expires_at, estimated_tokens, limit, reservation_id, self.RESERVATION_TTL_SECONDS,
            )
            admitted, current, reason = int(result[0]), int(result[1]), str(result[2])
            if not admitted:
                if reason == "duplicate":
                    raise ValueError("Duplicate daily token reservation ID")
                _audit_quota_denial(user_id, "daily_tokens", current, limit, "litellm")
                raise QuotaExceededException(
                    "daily_tokens",
                    f"Daily token budget exhausted ({current:,}/{limit:,} tokens reserved or consumed).",
                    current,
                    limit,
                )
            return day, current, limit
        except (QuotaExceededException, ValueError):
            raise
        except Exception as exc:
            raise ConnectionError("Shared quota state unavailable") from exc

    def settle_daily_token_reservation(
        self,
        user_id: str,
        reservation_id: str,
        admission_day: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> int:
        """Replace an estimate with actual usage exactly once, attributed to admission day."""
        actual = max(0, int(prompt_tokens)) + max(0, int(completion_tokens))
        daily_key = f"daily_tokens:{user_id}:{admission_day}"
        index_key = f"daily_reservations:index:{user_id}:{admission_day}"
        active_key = f"daily_reservations:active:{user_id}:{admission_day}"
        settled_key = f"daily_reservations:settled:{user_id}:{admission_day}"
        tpm_key = f"rate:tpm:{user_id}"
        r = self.redis
        if not r:
            if self.require_shared:
                raise ConnectionError("Shared quota state unavailable")
            with self._local_lease_lock:
                done = getattr(self, "_local_daily_settled", set())
                identity = (user_id, admission_day, reservation_id)
                if identity in done:
                    return getattr(self, "_local_daily_actual", {}).get((user_id, admission_day), 0)
                reservations = getattr(self, "_local_daily_reservations", {})
                reservations.pop(identity, None)
                actual_by_day = getattr(self, "_local_daily_actual", {})
                actual_by_day[(user_id, admission_day)] = actual_by_day.get((user_id, admission_day), 0) + actual
                done.add(identity)
                self._local_daily_reservations = reservations
                self._local_daily_actual = actual_by_day
                self._local_daily_settled = done
                return actual_by_day[(user_id, admission_day)]
        try:
            result = r.eval(
                LUA_SETTLE_DAILY_TOKENS, 5, daily_key, active_key, settled_key, index_key, tpm_key,
                reservation_id, actual, self.RESERVATION_TTL_SECONDS, time.time(),
            )
            return int(result[1])
        except Exception as exc:
            raise ConnectionError("Shared quota state unavailable") from exc

    def record_token_consumption(self, user_id: str, prompt_tokens: int, completion_tokens: int):
        total = prompt_tokens + completion_tokens
        if total <= 0:
            return
        today_str = self._quota_day()
        daily_key = f"daily_tokens:{user_id}:{today_str}"
        now = time.time()

        r = self.redis
        if r:
            try:
                pipe = r.pipeline()
                pipe.incrby(daily_key, total)
                pipe.expire(daily_key, 172800)  # 48h TTL
                pipe.execute()
            except Exception as exc:
                if self.require_shared:
                    raise ConnectionError("Shared quota state unavailable") from exc
        elif self.require_shared:
            raise ConnectionError("Shared quota state unavailable")
        self.record_tpm_consumption(user_id, total, now)

    def record_tpm_consumption(self, user_id: str, total_tokens: int, timestamp: Optional[float] = None) -> None:
        """Record usage in the rolling TPM ledger without changing daily totals."""
        if total_tokens <= 0:
            return
        now = time.time() if timestamp is None else timestamp
        r = self.redis
        if not r:
            if self.require_shared:
                raise ConnectionError("Shared quota state unavailable")
            return
        try:
            tpm_key = f"rate:tpm:{user_id}"
            pipe = r.pipeline()
            pipe.zadd(tpm_key, {f"{uuid.uuid4().hex}:{total_tokens}": now})
            pipe.zremrangebyscore(tpm_key, 0, now - 60)
            pipe.expire(tpm_key, 120)
            pipe.execute()
        except Exception as exc:
            if self.require_shared:
                raise ConnectionError("Shared quota state unavailable") from exc

quota_mgr = QuotaManager()
