"""
Transactional Valkey-backed Approval Store and Backward-Compatible Dict Proxy.
Supports full 8-state machine:
proposed -> pending -> (approved | rejected | expired) -> executing -> (succeeded | failed)
with atomic Lua CAS scripts, parameter binding, and single-use anti-replay.
"""
import collections.abc
import json
import logging
import os
import shlex
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple, Iterator

import redis

from .models import ApprovalRecord, ApprovalStatus

logger = logging.getLogger("approval_gate.store")

# Lua Scripts
LUA_CLAIM_FOR_EXECUTION = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local exp_user = ARGV[2]
local exp_sess = ARGV[3]
local exp_ws = ARGV[4]
local exp_cmd = ARGV[5]
local exp_target = ARGV[6]
local exp_hash = ARGV[7]

if redis.call("EXISTS", key) == 0 then
    return cjson.encode({success = false, code = "NOT_FOUND", error = "Approval token not found"})
end

local raw = redis.call("GET", key)
if not raw then
    return cjson.encode({success = false, code = "NOT_FOUND", error = "Approval token not found"})
end
local data = cjson.decode(raw)

if data.status == "executing" then
    return cjson.encode({success = false, code = "ALREADY_EXECUTING", error = "Approval token already executing (concurrent execution blocked)"})
end
if data.status == "consumed" or data.status == "succeeded" or data.status == "failed" then
    return cjson.encode({success = false, code = "ALREADY_CONSUMED", error = "Approval token already consumed (replay attack blocked)"})
end
if data.status ~= "approved" then
    return cjson.encode({success = false, code = "INVALID_STATUS", error = "Approval token not approved (current status: " .. tostring(data.status) .. ")"})
end

local expires_at = tonumber(data.expires_at or 0)
if now >= expires_at then
    data.status = "expired"
    redis.call("SET", key, cjson.encode(data))
    return cjson.encode({success = false, code = "EXPIRED", error = "Approval token expired"})
end

if exp_user ~= "" and data.user_id ~= exp_user then
    return cjson.encode({success = false, code = "USER_MISMATCH", error = "Approval token bound to different user"})
end

if exp_sess ~= "" and data.session_id ~= "" and data.session_id ~= exp_sess then
    return cjson.encode({success = false, code = "SESSION_MISMATCH", error = "Approval token bound to different session"})
end

if exp_ws ~= "" and data.workspace ~= "" and data.workspace ~= exp_ws then
    return cjson.encode({success = false, code = "WORKSPACE_MISMATCH", error = "Approval token bound to different workspace"})
end

if exp_cmd ~= "" and data.command ~= "" and data.command ~= exp_cmd then
    return cjson.encode({success = false, code = "COMMAND_MISMATCH", error = "Approval command does not match"})
end

if exp_target ~= "" and data.target ~= "" and data.target ~= exp_target then
    return cjson.encode({success = false, code = "TARGET_MISMATCH", error = "Approval target does not match"})
end

if exp_hash ~= "" and data.content_hash ~= "" and data.content_hash ~= exp_hash then
    return cjson.encode({success = false, code = "HASH_MISMATCH", error = "Approval content hash mismatch: payload tampered"})
end

data.status = "executing"
data.executed_by = exp_user
data.executed_at = now
redis.call("SET", key, cjson.encode(data))
return cjson.encode({success = true, code = "CLAIMED", status = "executing", approval = data})
"""

LUA_DECIDE_APPROVAL = """
local key = KEYS[1]
local pending_zset = KEYS[2]
local now = tonumber(ARGV[1])
local decision = ARGV[2]
local reviewer = ARGV[3]
local role = ARGV[4]
local reason = ARGV[5]

if role ~= "admin" then
    return cjson.encode({success = false, error = "Administrator role required"})
end

local raw = redis.call("GET", key)
if not raw then
    return cjson.encode({success = false, error = "Approval request not found"})
end
local data = cjson.decode(raw)

if data.user_id == reviewer then
    return cjson.encode({success = false, error = "Requester cannot decide own approval"})
end

if data.status ~= "pending" then
    return cjson.encode({success = false, error = "Request already decided (" .. tostring(data.status) .. ")"})
end

local expires_at = tonumber(data.expires_at or 0)
if now >= expires_at then
    data.status = "expired"
    redis.call("SET", key, cjson.encode(data))
    redis.call("ZREM", pending_zset, KEYS[1])
    return cjson.encode({success = false, error = "Approval request expired"})
end

local new_status = (decision == "approved") and "approved" or "rejected"
data.status = new_status
data.decided_by = reviewer
data.decided_at = now
data.decision_reason = reason
redis.call("SET", key, cjson.encode(data))
redis.call("ZREM", pending_zset, KEYS[1])

return cjson.encode({success = true, status = new_status, approval = data})
"""

LUA_COMPLETE_EXECUTION = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local is_success = (ARGV[2] == "true")
local exit_code = tonumber(ARGV[3]) or 0
local summary = ARGV[4]

local raw = redis.call("GET", key)
if not raw then
    return cjson.encode({success = false, error = "Approval token not found"})
end
local data = cjson.decode(raw)

local final_status = is_success and "succeeded" or "failed"
data.status = final_status
data.completed_at = now
data.exit_code = exit_code
data.result_summary = summary
redis.call("SET", key, cjson.encode(data))

return cjson.encode({success = true, status = final_status, approval = data})
"""


class RecordProxy(dict):
    """
    Dict proxy that writes changes back to store when modified in-place,
    e.g. `PENDING_APPROVALS[appr_id]["expires_at"] = time.time() - 10`.
    """
    def __init__(self, store: "ValkeyApprovalStore", approval_id: str, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._store = store
        self._approval_id = approval_id

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if self._store:
            self._store.update_raw(self._approval_id, {key: value})


class ValkeyApprovalStore:
    """
    Shared Approval Store backed by Valkey with atomic Lua CAS scripts.
    Local fallback is only for development runs without VALKEY_URL. Platform runs
    configure VALKEY_URL and fail closed if the shared store is unavailable.
    """
    def __init__(self, valkey_url: Optional[str] = None):
        self.valkey_url = valkey_url or os.getenv("VALKEY_URL", "redis://127.0.0.1:6379/0")
        self.require_shared = bool(valkey_url or os.getenv("VALKEY_URL"))
        self._redis: Optional[redis.Redis] = None
        self._lock = threading.RLock()
        self._local_records: Dict[str, Dict[str, Any]] = {}
        self._scripts_loaded = False
        self._sha_claim = None
        self._sha_decide = None
        self._sha_complete = None

    @property
    def redis(self) -> Optional[redis.Redis]:
        if self._redis is None:
            try:
                r = redis.Redis.from_url(self.valkey_url, decode_responses=True, socket_timeout=2.0)
                r.ping()
                self._redis = r
                self._load_scripts()
            except Exception as e:
                if self.require_shared:
                    raise ConnectionError("Shared approval store unavailable") from e
                logger.debug("Valkey not available (%s), using in-memory store", e)
                self._redis = None
        return self._redis

    def _load_scripts(self):
        if not self._redis or self._scripts_loaded:
            return
        try:
            self._sha_claim = self._redis.script_load(LUA_CLAIM_FOR_EXECUTION)
            self._sha_decide = self._redis.script_load(LUA_DECIDE_APPROVAL)
            self._sha_complete = self._redis.script_load(LUA_COMPLETE_EXECUTION)
            self._scripts_loaded = True
        except Exception as e:
            logger.warning("Failed to preload Lua scripts in Valkey: %s", e)

    def create_approval(self, record: ApprovalRecord) -> str:
        """Stores a new approval record with 300s expiration and 24h audit TTL."""
        data = record.to_dict()
        appr_id = record.approval_id
        now = time.time()
        if not data.get("created_at"):
            data["created_at"] = now
        if not data.get("expires_at"):
            data["expires_at"] = now + 300.0

        r = self.redis
        if r:
            try:
                key = f"approval:{appr_id}"
                with r.pipeline(transaction=True) as pipe:
                    pipe.setex(key, 86400, json.dumps(data))
                    if data["status"] == "pending":
                        pipe.zadd("approvals:pending", {key: data["expires_at"]})
                    if data.get("user_id"):
                        pipe.sadd(f"approvals:user:{data['user_id']}", appr_id)
                    pipe.execute()
            except Exception as e:
                if self.require_shared:
                    raise ConnectionError("Shared approval store unavailable") from e
                logger.warning("Valkey write error in create_approval: %s", e)

        with self._lock:
            self._local_records[appr_id] = dict(data)

        return appr_id

    def get_approval(self, approval_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves approval data by ID."""
        r = self.redis
        if r:
            try:
                raw = r.get(f"approval:{approval_id}")
                if raw:
                    data = json.loads(raw)
                    with self._lock:
                        self._local_records[approval_id] = data
                    return data
            except Exception as e:
                if self.require_shared:
                    raise ConnectionError("Shared approval store unavailable") from e
                logger.warning("Valkey read error in get_approval: %s", e)

        if self.require_shared:
            return None

        with self._lock:
            data = self._local_records.get(approval_id)
            return dict(data) if data else None

    def decide_approval(
        self,
        approval_id: str,
        approved: bool,
        reviewer: str,
        reviewer_role: str = "",
        reason: str = ""
    ) -> Dict[str, Any]:
        """Admin decides approval request (approve or reject)."""
        now = time.time()
        decision_str = "approved" if approved else "rejected"

        if not reviewer or reviewer_role != "admin":
            return {"success": False, "error": "Administrator role required"}

        r = self.redis
        if r:
            try:
                key = f"approval:{approval_id}"
                pending_zset = "approvals:pending"
                if self._sha_decide:
                    res_raw = r.evalsha(
                        self._sha_decide, 2, key, pending_zset,
                        now, decision_str, reviewer, reviewer_role, reason
                    )
                else:
                    res_raw = r.eval(
                        LUA_DECIDE_APPROVAL, 2, key, pending_zset,
                        now, decision_str, reviewer, reviewer_role, reason
                    )
                result = json.loads(res_raw)
                if result.get("success") and "approval" in result:
                    with self._lock:
                        self._local_records[approval_id] = result["approval"]
                return result
            except Exception as e:
                if self.require_shared:
                    raise ConnectionError("Shared approval store unavailable") from e
                logger.warning("Valkey decide_approval error, falling back to local: %s", e)

        # In-memory fallback
        with self._lock:
            req = self._local_records.get(approval_id)
            if not req:
                return {"success": False, "error": "Approval request not found"}
            if reviewer == req.get("user_id"):
                return {"success": False, "error": "Requester cannot decide own approval"}
            if req.get("status") != "pending":
                return {"success": False, "error": f"Request already decided ({req.get('status')})"}
            if now >= req.get("expires_at", 0):
                req["status"] = "expired"
                return {"success": False, "error": "Approval request expired"}

            req["status"] = decision_str
            req["decided_by"] = reviewer
            req["decided_at"] = now
            req["decision_reason"] = reason
            return {"success": True, "status": decision_str, "approval": dict(req)}

    def claim_for_execution(
        self,
        approval_id: str,
        user_id: str,
        session_id: str = "",
        workspace: str = "",
        command: str = "",
        target: str = "",
        content_hash: str = ""
    ) -> Dict[str, Any]:
        """
        Atomic Compare-And-Swap (CAS) claiming token for execution.
        Transitions approved -> executing.
        Guarantees single-use execution and validates cryptographic bindings.
        """
        now = time.time()
        r = self.redis
        if r:
            try:
                key = f"approval:{approval_id}"
                if self._sha_claim:
                    res_raw = r.evalsha(
                        self._sha_claim, 1, key,
                        now, user_id, session_id, workspace, command, target, content_hash
                    )
                else:
                    res_raw = r.eval(
                        LUA_CLAIM_FOR_EXECUTION, 1, key,
                        now, user_id, session_id, workspace, command, target, content_hash
                    )
                result = json.loads(res_raw)
                if result.get("success") and "approval" in result:
                    with self._lock:
                        self._local_records[approval_id] = result["approval"]
                return result
            except Exception as e:
                if self.require_shared:
                    raise ConnectionError("Shared approval store unavailable") from e
                logger.warning("Valkey claim_for_execution error, falling back to local: %s", e)

        # In-memory fallback
        with self._lock:
            req = self._local_records.get(approval_id)
            if not req:
                return {"success": False, "code": "NOT_FOUND", "error": "Approval token not found"}
            status = req.get("status")
            if status == "executing":
                return {"success": False, "code": "ALREADY_EXECUTING", "error": "Approval token already executing (concurrent execution blocked)"}
            if status in ("consumed", "succeeded", "failed"):
                return {"success": False, "code": "ALREADY_CONSUMED", "error": "Approval token already consumed (replay attack blocked)"}
            if status != "approved":
                return {"success": False, "code": "INVALID_STATUS", "error": f"Approval token not approved (current status: {status})"}
            if now >= req.get("expires_at", 0):
                req["status"] = "expired"
                return {"success": False, "code": "EXPIRED", "error": "Approval token expired"}

            # Binding checks
            if user_id and req.get("user_id") != user_id:
                return {"success": False, "code": "USER_MISMATCH", "error": "Approval token bound to different user"}
            if session_id and req.get("session_id") and req.get("session_id") != session_id:
                return {"success": False, "code": "SESSION_MISMATCH", "error": "Approval token bound to different session"}
            if workspace and req.get("workspace") and req.get("workspace") != workspace:
                return {"success": False, "code": "WORKSPACE_MISMATCH", "error": "Approval token bound to different workspace"}
            if command and req.get("command") and req.get("command") != command:
                return {"success": False, "code": "COMMAND_MISMATCH", "error": "Approval command does not match"}
            if target and req.get("target") and req.get("target") != target:
                return {"success": False, "code": "TARGET_MISMATCH", "error": "Approval target does not match"}
            if content_hash and req.get("content_hash") and req.get("content_hash") != content_hash:
                return {"success": False, "code": "HASH_MISMATCH", "error": "Approval content hash mismatch: payload tampered"}

            req["status"] = "executing"
            req["executed_by"] = user_id
            req["executed_at"] = now
            return {"success": True, "code": "CLAIMED", "status": "executing", "approval": dict(req)}

    def complete_execution(
        self,
        approval_id: str,
        is_success: bool,
        exit_code: int = 0,
        result_summary: str = ""
    ) -> Dict[str, Any]:
        """Transitions executing -> succeeded or failed."""
        now = time.time()
        final_status = "succeeded" if is_success else "failed"

        r = self.redis
        if r:
            try:
                key = f"approval:{approval_id}"
                success_str = "true" if is_success else "false"
                if self._sha_complete:
                    res_raw = r.evalsha(
                        self._sha_complete, 1, key,
                        now, success_str, exit_code, result_summary
                    )
                else:
                    res_raw = r.eval(
                        LUA_COMPLETE_EXECUTION, 1, key,
                        now, success_str, exit_code, result_summary
                    )
                result = json.loads(res_raw)
                if result.get("success") and "approval" in result:
                    with self._lock:
                        self._local_records[approval_id] = result["approval"]
                return result
            except Exception as e:
                if self.require_shared:
                    raise ConnectionError("Shared approval store unavailable") from e
                logger.warning("Valkey complete_execution error, falling back to local: %s", e)

        if self.require_shared:
            raise ConnectionError("Shared approval store unavailable")

        with self._lock:
            req = self._local_records.get(approval_id)
            if not req:
                return {"success": False, "error": "Approval token not found"}
            req["status"] = final_status
            req["completed_at"] = now
            req["exit_code"] = exit_code
            req["result_summary"] = result_summary
            return {"success": True, "status": final_status, "approval": dict(req)}

    def update_raw(self, approval_id: str, updates: Dict[str, Any]) -> bool:
        """Directly updates fields in local state and Valkey (for test manipulations)."""
        with self._lock:
            req = self._local_records.get(approval_id)
            if req is not None:
                req.update(updates)
            else:
                self._local_records[approval_id] = dict(updates)
            current_data = dict(self._local_records[approval_id])

        r = self.redis
        if r:
            try:
                r.setex(f"approval:{approval_id}", 86400, json.dumps(current_data))
                if current_data.get("status") == "pending":
                    r.zadd("approvals:pending", {f"approval:{approval_id}": current_data.get("expires_at", 0)})
                else:
                    r.zrem("approvals:pending", f"approval:{approval_id}")
            except Exception as e:
                if self.require_shared:
                    raise ConnectionError("Shared approval store unavailable") from e
                logger.warning("Valkey update_raw error: %s", e)
        return True

    def delete(self, approval_id: str):
        with self._lock:
            self._local_records.pop(approval_id, None)
        r = self.redis
        if r:
            try:
                r.delete(f"approval:{approval_id}")
                r.zrem("approvals:pending", f"approval:{approval_id}")
            except Exception:
                if self.require_shared:
                    raise ConnectionError("Shared approval store unavailable")
                pass


class ApprovalStoreMappingProxy(collections.abc.MutableMapping):
    """
    MutableMapping proxy ensuring `PENDING_APPROVALS` remains 100% backward compatible
    with existing tests while delegating to ValkeyApprovalStore.
    """
    def __init__(self, store: ValkeyApprovalStore):
        self._store = store

    def __getitem__(self, approval_id: str) -> RecordProxy:
        data = self._store.get_approval(approval_id)
        if data is None:
            raise KeyError(approval_id)
        return RecordProxy(self._store, approval_id, data)

    def __setitem__(self, approval_id: str, value: Dict[str, Any]):
        rec_data = dict(value)
        if "approval_id" not in rec_data:
            rec_data["approval_id"] = approval_id
        # Instantiate model if possible or store directly
        try:
            record = ApprovalRecord(**rec_data)
            self._store.create_approval(record)
        except Exception:
            self._store.update_raw(approval_id, rec_data)

    def __delitem__(self, approval_id: str):
        self._store.delete(approval_id)

    def __iter__(self) -> Iterator[str]:
        # Return all active keys from local cache and Valkey
        keys = set() if self._store.require_shared else set(self._store._local_records.keys())
        r = self._store.redis
        if r:
            try:
                for k in r.keys("approval:*"):
                    keys.add(k.split(":", 1)[1])
            except Exception as exc:
                if self._store.require_shared:
                    raise ConnectionError("Shared approval store unavailable") from exc
        return iter(keys)

    def __len__(self) -> int:
        return sum(1 for _ in self.__iter__())

    def __contains__(self, approval_id: object) -> bool:
        if not isinstance(approval_id, str):
            return False
        return self._store.get_approval(approval_id) is not None

    def get(self, approval_id: str, default: Any = None) -> Any:
        try:
            return self[approval_id]
        except KeyError:
            return default

    def values(self):
        return [self[k] for k in self]

    def items(self):
        return [(k, self[k]) for k in self]

    def keys(self):
        return [k for k in self]


# Singleton instance
_GLOBAL_STORE: Optional[ValkeyApprovalStore] = None


def get_approval_store() -> ValkeyApprovalStore:
    global _GLOBAL_STORE
    if _GLOBAL_STORE is None:
        _GLOBAL_STORE = ValkeyApprovalStore()
    return _GLOBAL_STORE
