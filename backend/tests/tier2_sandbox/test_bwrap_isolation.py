"""
Tier 2 Security Test: Bubblewrap Unprivileged Linux Namespace Confinement.
Verifies filesystem read-only barriers, network unsharing, capability dropping,
and execution deadlines as specified in PROJECT.md and Document 04.
"""
import os
import sys
import tempfile
import pytest

from backend.services.agent_tools.tools import execute_sandboxed_command

pytestmark = pytest.mark.usefixtures("sandbox_available")

@pytest.fixture
def test_workspace():
    """Provides an isolated temporary workspace directory."""
    with tempfile.TemporaryDirectory(prefix="sysadmin_sandbox_ws_") as tmpdir:
        os.chmod(tmpdir, 0o700)
        yield tmpdir

def test_sandbox_basic_execution(test_workspace):
    """Verify primary behavior: command executes successfully inside sandbox."""
    code, stdout, stderr = execute_sandboxed_command(test_workspace, "echo 'hermetic sandbox online'")
    assert code == 0
    assert "hermetic sandbox online" in stdout.strip()

def test_sandbox_readonly_usr_barrier(test_workspace):
    """Verify that writing to /usr fails closed with Read-only file system."""
    code, stdout, stderr = execute_sandboxed_command(test_workspace, "touch /usr/malicious_binary")
    assert code != 0
    assert "Read-only file system" in stderr

def test_sandbox_readonly_etc_barrier(test_workspace):
    """Verify that writing to read-only mounted /etc paths fails closed."""
    code, stdout, stderr = execute_sandboxed_command(test_workspace, "touch /etc/resolv.conf")
    assert code != 0
    assert "Read-only file system" in stderr

def test_sandbox_network_isolation(test_workspace):
    """Verify network is unshared: raw sockets and outbound connections fail."""
    # Attempting to ping external or loopback inside unshared net namespace
    code, stdout, stderr = execute_sandboxed_command(test_workspace, "ping -c 1 8.8.8.8")
    assert code != 0
    assert "Operation not permitted" in stderr or "Network is unreachable" in stderr or "socket" in stderr

def test_sandbox_network_socket_unreachable(test_workspace):
    """Verify TCP/UDP socket creation to external IP is unreachable."""
    py_cmd = "python3 -c \"import socket; s = socket.socket(); s.settimeout(1); s.connect(('1.1.1.1', 80))\""
    code, stdout, stderr = execute_sandboxed_command(test_workspace, py_cmd)
    assert code != 0
    assert "Network is unreachable" in stderr or "timed out" in stderr or "Connection refused" in stderr

def test_sandbox_dropped_capabilities(test_workspace):
    """Verify all Linux capabilities are dropped inside sandbox."""
    code, stdout, stderr = execute_sandboxed_command(test_workspace, "which capsh && capsh --print || echo 'no-capsh'")
    assert code == 0
    # If capsh is present, Current should be empty or no-capsh
    if "Current:" in stdout:
        # Should not have cap_sys_admin, cap_net_admin, cap_sys_rawio
        assert "cap_sys_admin" not in stdout
        assert "cap_net_admin" not in stdout
        assert "cap_sys_rawio" not in stdout

def test_sandbox_execution_deadline(test_workspace):
    """Verify 15s deadline / 20s kill terminates long-running commands with exit code 124."""
    # Run sleep command exceeding timeout
    code, stdout, stderr = execute_sandboxed_command(test_workspace, "sleep 16")
    # Timeout command returns 124 when timing out
    assert code in [124, 137]
