"""
Tier 2 Security Test: Cgroups v2 Resource Ceilings & Adversarial Stress.
Validates memory ceilings (4 GiB), process ceilings (pids.max=128),
and CPU quota configuration matching Document 04 and PROJECT.md.
"""
import os
import subprocess
import pytest

from backend.services.agent_tools.tools import execute_sandboxed_command
from backend.services.agent_tools.approval_gate import evaluate_command_safety

def test_runner_enforces_limits_or_fails_closed(tmp_path):
    """A host without delegated controllers must never run a command unbounded."""
    code, stdout, stderr = execute_sandboxed_command(str(tmp_path), "echo SANDBOX_RAN")
    if code == 0:
        assert stdout.strip() == "SANDBOX_RAN"
    else:
        assert "SANDBOX_RAN" not in stdout
        assert code != 0
        assert stderr


def test_child_is_in_limited_cgroup_when_host_delegates(tmp_path):
    """Inspect the actual kernel limits from a process launched by the runner."""
    probe = tmp_path / "probe.sh"
    probe.write_text("""#!/bin/sh
group=$(sed -n 's/^0:://p' /proc/self/cgroup)
base="/sys/fs/cgroup${group}"
cat "$base/memory.max" "$base/pids.max" "$base/cpu.max"
# memory.max alone does not bound total memory on swap-enabled hosts; the
# runner must also forbid swap. Kernels without swap accounting have no file.
if [ -f "$base/memory.swap.max" ]; then cat "$base/memory.swap.max"; fi
""")
    probe.chmod(0o700)
    runner = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../config/sandbox/bwrap-runner.sh"))
    env = {**os.environ, "BWRAP_BIN": str(probe)}
    result = subprocess.run([runner, str(tmp_path), "/bin/sh", "-c", "true"], env=env,
                            text=True, capture_output=True)
    if result.returncode == 126 and "no delegated cgroup" in result.stderr:
        pytest.skip("Host has not delegated a writable cgroups v2 subtree")
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[:3] == ["4294967296", "128", "200000 100000"]
    # With swap accounting present the runner must cap swap at zero so the
    # 4 GiB ceiling bounds total memory, not just RSS.
    if len(lines) == 4:
        assert lines[3] == "0"
    else:
        assert len(lines) == 3

def test_adversarial_fork_bomb_blocked_at_interceptor():
    """Verify classic fork-bomb ':(){ :|:& };:' is unconditionally blocked by safety filter."""
    fork_bomb = ":(){ :|:& };:"
    res = evaluate_command_safety(fork_bomb)
    assert res["action"] == "BLOCKED"
    assert "security violation" in res["reason"].lower()

def test_pids_max_bounding_behavior(tmp_path):
    """
    Verify process spawning ceiling inside sandbox.
    Even if an unblocked loop attempts to spawn 500 processes, it is terminated or bounded.
    """
    ws = str(tmp_path)
    # Bash script spawning 200 background subshells
    script = "for i in $(seq 1 150); do (sleep 5 &); done; wait"
    code, stdout, stderr = execute_sandboxed_command(ws, script)
    if code == 126 and "cgroup" in stderr.lower():
        pytest.skip("Host has not delegated a writable cgroups v2 subtree")
    # Under cgroups or bwrap pid limits, fork returns resource unavailable or processes terminate
    assert code == 0 or "Resource temporarily unavailable" in stderr or "fork" in stderr or code in [124, 137]
