"""
Tier 2 Security Test: Destructive Command Interception & Unconditional Rejection.
Ensures destructive commands (rm -rf, mkfs, dd, fork-bomb) are unconditionally blocked
with HTTP 403 / action BLOCKED and never receive an approval request or reach shell execution.
"""
import pytest

from backend.services.agent_tools.approval_gate import (
    evaluate_command_safety,
    PENDING_APPROVALS
)

DESTRUCTIVE_COMMAND_PAYLOADS = [
    "rm -rf /",
    "rm -rf / --no-preserve-root",
    "rm -rf /var/log",
    "rm  -rf   /tmp/data",
    "mkfs.ext4 /dev/sda1",
    "mkfs.xfs -f /dev/nvme0n1",
    "dd if=/dev/zero of=/dev/sda bs=1M",
    "dd if=/dev/urandom of=/dev/nvme0n1",
    "cat /dev/zero > /dev/sda",
    "iptables -F",
    "reboot",
    "reboot -f",
    "shutdown -h now",
    ":(){ :|:& };:"
]

@pytest.mark.parametrize("payload", DESTRUCTIVE_COMMAND_PAYLOADS)
def test_destructive_commands_unconditionally_blocked(payload):
    """Verify each destructive command is flagged as BLOCKED."""
    res = evaluate_command_safety(payload)
    assert res["action"] == "BLOCKED"
    assert "security violation" in res["reason"].lower()

def test_destructive_command_never_creates_approval():
    """Verify that a blocked destructive command NEVER generates an approval request."""
    initial_count = len(PENDING_APPROVALS)
    res = evaluate_command_safety("rm -rf /")
    assert res["action"] == "BLOCKED"
    # No new pending approval request must be registered
    assert len(PENDING_APPROVALS) == initial_count

MUTATING_COMMANDS_REQUIRING_APPROVAL = [
    "systemctl restart nginx",
    "systemctl stop traefik",
    "systemctl reload postgresql",
    "sed -i 's/foo/bar/g' /etc/hosts",
    "chmod 755 /usr/local/bin/run.sh",
    "chown root:root /tmp/file",
    "apt install -y vim",
    "docker restart my_container"
]

@pytest.mark.parametrize("cmd", MUTATING_COMMANDS_REQUIRING_APPROVAL)
def test_mutating_commands_require_approval(cmd):
    """Verify mutating commands are flagged as APPROVAL_REQUIRED, not ALLOW or BLOCKED."""
    res = evaluate_command_safety(cmd)
    assert res["action"] == "APPROVAL_REQUIRED"
    assert "mutating operation detected" in res["reason"].lower()

READ_ONLY_SAFE_COMMANDS = [
    "echo 'hello world'",
    "ls -la /backend/data",
    "cat /tmp/test.txt",
    "df -h",
    "free -m"
]

@pytest.mark.parametrize("cmd", READ_ONLY_SAFE_COMMANDS)
def test_safe_commands_allowed(cmd):
    """Verify safe read-only operations receive ALLOW action."""
    res = evaluate_command_safety(cmd)
    assert res["action"] == "ALLOW"


@pytest.mark.parametrize("cmd", [
    "touch /tmp/new-file",
    "printf x > /tmp/new-file",
    "echo x > /tmp/new-file",
    "python3 -c 'print(42)'",
    "cat /tmp/file | tee /tmp/other",
    "echo $(touch /tmp/new-file)",
])
def test_other_shell_commands_require_approval(cmd):
    assert evaluate_command_safety(cmd)["action"] == "APPROVAL_REQUIRED"
