"""
Active Request Registry and In-Flight Cancellation Tracker for Agent Runtime.
Tracks active tasks by user_id, session_id, and request_id.
Enforces that users can only cancel their own in-flight operations.
"""
import asyncio
import logging
from typing import Dict, Optional, Tuple, Callable, Any

logger = logging.getLogger("agent_runtime.cancellation")

class ActiveRequestRecord:
    def __init__(
        self,
        request_id: str,
        user_id: str,
        session_id: Optional[str] = None,
        task: Optional[asyncio.Task] = None,
        cancel_event: Optional[asyncio.Event] = None
    ):
        self.request_id = request_id
        self.user_id = user_id
        self.session_id = session_id
        self.task = task
        self.cancel_event = cancel_event or asyncio.Event()
        self.abort_callbacks: list[Callable[[], Any]] = []

    def add_abort_callback(self, cb: Callable[[], Any]):
        self.abort_callbacks.append(cb)

    def trigger_abort(self):
        for cb in self.abort_callbacks:
            try:
                cb()
            except Exception as e:
                logger.warning(f"Error in abort callback for request {self.request_id}: {e}")

class RequestRegistry:
    def __init__(self):
        self._requests: Dict[str, ActiveRequestRecord] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        request_id: str,
        user_id: str,
        session_id: Optional[str] = None,
        task: Optional[asyncio.Task] = None
    ) -> ActiveRequestRecord:
        async with self._lock:
            if request_id in self._requests:
                raise ValueError("An active request already uses this request_id")
            record = ActiveRequestRecord(
                request_id=request_id,
                user_id=user_id,
                session_id=session_id,
                task=task
            )
            self._requests[request_id] = record
            return record

    async def update_task(self, request_id: str, task: asyncio.Task) -> None:
        async with self._lock:
            if request_id in self._requests:
                self._requests[request_id].task = task

    async def unregister(self, request_id: str) -> None:
        async with self._lock:
            self._requests.pop(request_id, None)

    def unregister_nowait(self, request_id: str) -> None:
        """Drop a request record without awaiting.

        Cancelled cleanup paths cannot await, so this is the synchronous
        counterpart of unregister(). A dict pop is atomic under the GIL; the
        readers below iterate over snapshots so an unlocked pop cannot raise
        "dictionary changed size during iteration".
        """
        self._requests.pop(request_id, None)

    async def get_active_count(self, user_id: Optional[str] = None) -> int:
        async with self._lock:
            if user_id is None:
                return len(self._requests)
            return sum(1 for r in list(self._requests.values()) if r.user_id == user_id)

    async def cancel(
        self,
        user_id: str,
        request_id: Optional[str] = None,
        session_id: Optional[str] = None
    ) -> Tuple[bool, Optional[str], str]:
        """
        Cancels an active request.
        Only allows cancellation if request belongs to the authenticated user_id.
        Returns: (success: bool, cancelled_request_id: Optional[str], message: str)
        """
        async with self._lock:
            target_record: Optional[ActiveRequestRecord] = None
            if request_id:
                record = self._requests.get(request_id)
                if record:
                    if record.user_id != user_id:
                        return False, None, f"Unauthorized: User {user_id} cannot cancel request owned by {record.user_id}"
                    target_record = record
            elif session_id:
                for record in list(self._requests.values()):
                    if record.session_id == session_id and record.user_id == user_id:
                        target_record = record
                        break
            else:
                return False, None, "Either request_id or session_id must be provided"

            if not target_record:
                return False, None, "No active in-flight request found matching criteria"

            target_record.cancel_event.set()
            target_record.trigger_abort()
            if target_record.task and not target_record.task.done():
                target_record.task.cancel()

            return True, target_record.request_id, f"Request {target_record.request_id} cancelled"

registry = RequestRegistry()
