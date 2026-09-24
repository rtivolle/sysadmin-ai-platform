"""
Fixtures package for Sysadmin AI Platform tests.
Provides path helpers and automatic fixture verification.
"""
import os
from .fixture_generator import (
    FIXTURES_DIR, LOGS_DIR, CONFIG_DIR, RUNBOOKS_DIR, generate_all
)

def ensure_fixtures():
    """Ensures all required test fixtures are present on disk."""
    required = [
        os.path.join(LOGS_DIR, "nginx_error.log"),
        os.path.join(LOGS_DIR, "journal_oom.log"),
        os.path.join(LOGS_DIR, "massive_access_5gb.log"),
        os.path.join(CONFIG_DIR, "platform_config.json"),
        os.path.join(RUNBOOKS_DIR, "nginx_recovery.md"),
        os.path.join(RUNBOOKS_DIR, "disaster_recovery_plan.md")
    ]
    if not all(os.path.exists(p) for p in required):
        generate_all()

# Ensure fixtures on import
ensure_fixtures()
