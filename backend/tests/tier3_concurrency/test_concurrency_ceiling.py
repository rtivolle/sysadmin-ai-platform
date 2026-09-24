"""
Tier 3 Test: 10-User Concurrency & 2 In-Flight Calls Ceiling.
Verifies that 10 concurrent sysadmins can operate simultaneously,
and any single user exceeding 2 in-flight calls receives HTTP 429 / rejection.
"""
import asyncio
import time
import pytest

class UserConcurrencyTracker:
    """
    Simulates the Valkey / LiteLLM in-flight tracking logic:
    - Max in-flight calls per user = 2
    - Max total across 10 users = 20
    """
    def __init__(self, max_in_flight_per_user: int = 2):
        self.max_in_flight = max_in_flight_per_user
        self.in_flight_counts = {}
        self.lock = asyncio.Lock()

    async def acquire_slot(self, user_id: str) -> bool:
        async with self.lock:
            current = self.in_flight_counts.get(user_id, 0)
            if current >= self.max_in_flight:
                return False  # HTTP 429 Too Many Requests
            self.in_flight_counts[user_id] = current + 1
            return True

    async def release_slot(self, user_id: str):
        async with self.lock:
            if user_id in self.in_flight_counts and self.in_flight_counts[user_id] > 0:
                self.in_flight_counts[user_id] -= 1

@pytest.mark.asyncio
async def test_per_user_ceiling_two_inflight():
    """Verify single user cannot exceed 2 in-flight requests simultaneously."""
    tracker = UserConcurrencyTracker(max_in_flight_per_user=2)
    user = "sysadmin-01"

    # Slot 1: Admitted
    assert await tracker.acquire_slot(user) is True
    # Slot 2: Admitted
    assert await tracker.acquire_slot(user) is True
    # Slot 3: Rejected with 429
    assert await tracker.acquire_slot(user) is False

    # After slot 1 finishes, slot 3 can now be admitted
    await tracker.release_slot(user)
    assert await tracker.acquire_slot(user) is True

    # Cleanup
    await tracker.release_slot(user)
    await tracker.release_slot(user)

@pytest.mark.asyncio
async def test_ten_users_concurrent_independence():
    """Verify 10 distinct sysadmins can each run 2 concurrent calls in parallel."""
    tracker = UserConcurrencyTracker(max_in_flight_per_user=2)
    users = [f"sysadmin-{i:02d}" for i in range(1, 11)]

    # All 10 users acquire slot 1
    for u in users:
        assert await tracker.acquire_slot(u) is True

    # All 10 users acquire slot 2 (total 20 concurrent active calls)
    for u in users:
        assert await tracker.acquire_slot(u) is True

    # Any user trying for slot 3 is rejected
    for u in users:
        assert await tracker.acquire_slot(u) is False

    # Release all
    for u in users:
        await tracker.release_slot(u)
        await tracker.release_slot(u)
