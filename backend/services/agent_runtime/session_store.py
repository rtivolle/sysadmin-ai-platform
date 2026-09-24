import os
import json
import time
import uuid
import logging
from typing import Optional, List
import redis.asyncio as aioredis
from .models import SessionState, SessionMessage
from .workspace import ensure_workspace

logger = logging.getLogger("agent_runtime.session_store")

VALKEY_URL = os.getenv("VALKEY_URL", "redis://:CONFIGURE_VIA_PLATFORM_SH@127.0.0.1:6379/0")
DEFAULT_SESSION_TTL = int(os.getenv("SESSION_TTL_SECONDS", "86400"))  # 24 hours

class SessionStore:
    def __init__(self, redis_url: str = VALKEY_URL):
        self.redis_url = redis_url
        self._redis: Optional[aioredis.Redis] = None
        self._in_memory_fallback: dict = {}

    async def get_client(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = aioredis.from_url(self.redis_url, decode_responses=True, socket_timeout=3.0)
        return self._redis

    def _session_key(self, user_id: str, session_id: str) -> str:
        return f"agent:session:{user_id}:{session_id}"

    def _user_sessions_key(self, user_id: str) -> str:
        return f"agent:user_sessions:{user_id}"

    async def get_session(self, user_id: str, session_id: str) -> Optional[SessionState]:
        key = self._session_key(user_id, session_id)
        try:
            r = await self.get_client()
            data = await r.get(key)
            if data:
                await r.expire(key, DEFAULT_SESSION_TTL)
                return SessionState.model_validate_json(data)
        except Exception as e:
            logger.warning(f"Valkey read error for {key}: {e}. Checking memory fallback.")
            if key in self._in_memory_fallback:
                return self._in_memory_fallback[key]
        return None

    async def create_or_get_session(
        self,
        user_id: str,
        session_id: Optional[str] = None,
    ) -> SessionState:
        effective_workspace = ensure_workspace(user_id)

        if session_id:
            existing = await self.get_session(user_id, session_id)
            if existing:
                # Old session data may contain a client-selected path. Never reuse it.
                if existing.workspace != effective_workspace:
                    existing.workspace = effective_workspace
                    await self.save_session(existing)
                return existing

        new_sess_id = session_id or f"sess-{uuid.uuid4().hex[:12]}"
        new_session = SessionState(
            session_id=new_sess_id,
            user_id=user_id,
            workspace=effective_workspace,
            created_at=time.time(),
            updated_at=time.time(),
            turn_count=0,
            messages=[]
        )
        await self.save_session(new_session)
        return new_session

    async def save_session(self, session: SessionState, ttl: int = DEFAULT_SESSION_TTL) -> None:
        session.updated_at = time.time()
        # Sliding context window: keep first message + last 10 messages if list gets too long
        if len(session.messages) > 12:
            session.messages = [session.messages[0]] + session.messages[-10:]

        key = self._session_key(session.user_id, session.session_id)
        user_set_key = self._user_sessions_key(session.user_id)
        payload = session.model_dump_json()

        try:
            r = await self.get_client()
            await r.set(key, payload, ex=ttl)
            await r.sadd(user_set_key, session.session_id)
            await r.expire(user_set_key, ttl)
        except Exception as e:
            logger.warning(f"Valkey write error for {key}: {e}. Storing in memory fallback.")
            self._in_memory_fallback[key] = session

    async def list_user_sessions(self, user_id: str) -> List[str]:
        user_set_key = self._user_sessions_key(user_id)
        try:
            r = await self.get_client()
            sessions = await r.smembers(user_set_key)
            return list(sessions)
        except Exception as e:
            logger.warning(f"Valkey smembers error for {user_set_key}: {e}")
            prefix = f"agent:session:{user_id}:"
            return [k.replace(prefix, "") for k in self._in_memory_fallback.keys() if k.startswith(prefix)]

    async def delete_session(self, user_id: str, session_id: str) -> bool:
        key = self._session_key(user_id, session_id)
        user_set_key = self._user_sessions_key(user_id)
        try:
            r = await self.get_client()
            await r.delete(key)
            await r.srem(user_set_key, session_id)
            self._in_memory_fallback.pop(key, None)
            return True
        except Exception:
            self._in_memory_fallback.pop(key, None)
            return False

    async def close(self):
        if self._redis is not None:
            await self._redis.close()
            self._redis = None
