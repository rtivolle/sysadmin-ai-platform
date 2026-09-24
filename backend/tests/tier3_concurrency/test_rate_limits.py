"""
Tier 3 Test: Rate Limits (60 RPM / 150k TPM) & Daily Token Budget Rollover.
Verifies rate limiting thresholds, HTTP 429 quota exhaustion,
and daily budget reset at midnight.
"""
import time
import pytest

class RateLimiter:
    """
    Simulates token bucket and daily quota tracking matching LiteLLM/Valkey parameters.
    """
    def __init__(self, rpm_limit: int = 60, tpm_limit: int = 150000, daily_token_cap: int = 2000000):
        self.rpm_limit = rpm_limit
        self.tpm_limit = tpm_limit
        self.daily_token_cap = daily_token_cap
        
        # Per-user counters
        self.user_requests = {}
        self.user_tokens = {}
        self.daily_usage = {}

    def check_and_consume(self, user_id: str, prompt_tokens: int, completion_tokens: int) -> dict:
        total_tokens = prompt_tokens + completion_tokens
        
        # Check Daily Budget
        current_daily = self.daily_usage.get(user_id, 0)
        if current_daily + total_tokens > self.daily_token_cap:
            return {"allowed": False, "status_code": 429, "error": "Daily token budget exceeded (2,000,000 tokens/day)"}

        # Check RPM
        current_reqs = self.user_requests.get(user_id, 0)
        if current_reqs >= self.rpm_limit:
            return {"allowed": False, "status_code": 429, "error": "Rate limit exceeded (60 RPM)"}

        # Check TPM
        current_tpm = self.user_tokens.get(user_id, 0)
        if current_tpm + total_tokens > self.tpm_limit:
            return {"allowed": False, "status_code": 429, "error": "Token rate limit exceeded (150,000 TPM)"}

        # Consume
        self.user_requests[user_id] = current_reqs + 1
        self.user_tokens[user_id] = current_tpm + total_tokens
        self.daily_usage[user_id] = current_daily + total_tokens

        return {"allowed": True, "status_code": 200, "remaining_daily": self.daily_token_cap - self.daily_usage[user_id]}

    def rollover_midnight(self):
        """Simulates midnight daily quota reset."""
        self.daily_usage.clear()
        self.user_requests.clear()
        self.user_tokens.clear()

def test_rpm_quota_exhaustion_429():
    """Verify rapid burst of requests returns HTTP 429 after 60 RPM limit."""
    limiter = RateLimiter(rpm_limit=60)
    user = "sysadmin-01"

    # First 60 succeed
    for _ in range(60):
        res = limiter.check_and_consume(user, prompt_tokens=10, completion_tokens=10)
        assert res["allowed"] is True

    # 61st request receives HTTP 429
    res_61 = limiter.check_and_consume(user, prompt_tokens=10, completion_tokens=10)
    assert res_61["allowed"] is False
    assert res_61["status_code"] == 429
    assert "60 RPM" in res_61["error"]

def test_tpm_rate_limit_429():
    """Verify exceeding 150,000 tokens within a minute returns HTTP 429."""
    limiter = RateLimiter(tpm_limit=150000)
    user = "sysadmin-tpm"

    # Consume 140,000 tokens (within 150k limit)
    res1 = limiter.check_and_consume(user, prompt_tokens=70000, completion_tokens=70000)
    assert res1["allowed"] is True

    # Consume another 20,000 tokens -> Exceeds 150k TPM
    res2 = limiter.check_and_consume(user, prompt_tokens=10000, completion_tokens=10000)
    assert res2["allowed"] is False
    assert res2["status_code"] == 429
    assert "150,000 TPM" in res2["error"]

def test_daily_token_budget_exhaustion_and_rollover():
    """Verify 2,000,000 daily token cap and midnight reset."""
    # Set tpm_limit high so we isolate the daily budget rollover behavior
    limiter = RateLimiter(daily_token_cap=2000000, tpm_limit=5000000)
    user = "sysadmin-02"

    # Consume 1,900,000 tokens
    res1 = limiter.check_and_consume(user, prompt_tokens=1000000, completion_tokens=900000)
    assert res1["allowed"] is True
    assert res1["remaining_daily"] == 100000

    # Consume 100,001 tokens -> Exceeds 2M cap -> 429
    res2 = limiter.check_and_consume(user, prompt_tokens=50000, completion_tokens=50001)
    assert res2["allowed"] is False
    assert res2["status_code"] == 429
    assert "Daily token budget exceeded" in res2["error"]

    # Midnight rollover
    limiter.rollover_midnight()

    # Now requests are admitted again
    res3 = limiter.check_and_consume(user, prompt_tokens=500, completion_tokens=500)
    assert res3["allowed"] is True
    assert res3["status_code"] == 200
