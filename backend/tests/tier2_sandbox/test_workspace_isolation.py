"""
Tier 2 Security Test: Multi-User 0700 Workspace Isolation.
Verifies that User A cannot read, modify, or list files in User B's workspace directory,
nor perform path traversal attacks out of their assigned workspace.
"""
import os
import stat
import tempfile
import pytest

from backend.services.agent_tools.tools import execute_sandboxed_command

@pytest.fixture
def multi_user_workspaces():
    """Sets up two separate user workspaces with 0700 permissions and secret files."""
    with tempfile.TemporaryDirectory(prefix="sysadmin_workspaces_") as root_dir:
        ws_user_a = os.path.join(root_dir, "sysadmin-01")
        ws_user_b = os.path.join(root_dir, "sysadmin-02")
        
        os.makedirs(ws_user_a, mode=0o700, exist_ok=True)
        os.makedirs(ws_user_b, mode=0o700, exist_ok=True)
        os.chmod(ws_user_a, 0o700)
        os.chmod(ws_user_b, 0o700)
        
        # Plant secret file in user B's workspace
        secret_b = os.path.join(ws_user_b, "credentials.key")
        with open(secret_b, "w") as f:
            f.write("SECRET_KEY_USER_B_12345")
        os.chmod(secret_b, 0o600)
        
        yield ws_user_a, ws_user_b

def test_workspace_mode_0700(multi_user_workspaces):
    """Verify workspace directories are created with strict 0700 permission bits."""
    ws_user_a, ws_user_b = multi_user_workspaces
    
    st_a = os.stat(ws_user_a)
    st_b = os.stat(ws_user_b)
    
    # 0o700: Owner rwx, Group none, Others none
    assert stat.S_IMODE(st_a.st_mode) == 0o700
    assert stat.S_IMODE(st_b.st_mode) == 0o700

def test_cross_user_workspace_access_blocked_in_sandbox(multi_user_workspaces, sandbox_available):
    """Verify sandboxed User A process cannot view or read User B's directory."""
    ws_user_a, ws_user_b = multi_user_workspaces
    
    # Bubblewrap binds ONLY ws_user_a to the sandbox. ws_user_b is not mounted at all.
    code, stdout, stderr = execute_sandboxed_command(ws_user_a, f"cat {ws_user_b}/credentials.key")
    
    assert code != 0
    assert "No such file or directory" in stderr or "Permission denied" in stderr
    assert "SECRET_KEY_USER_B_12345" not in stdout

def test_workspace_path_traversal_blocked(multi_user_workspaces, sandbox_available):
    """Verify directory traversal '../' from workspace fails to escape sandbox mount."""
    ws_user_a, ws_user_b = multi_user_workspaces
    
    # Attempt relative traversal out of workspace
    code, stdout, stderr = execute_sandboxed_command(ws_user_a, "ls -la ../")
    # Inside bwrap, parent directory is either non-existent or root/empty
    assert "sysadmin-02" not in stdout

def test_workspace_internal_write_permitted(multi_user_workspaces, sandbox_available):
    """Verify user CAN read and write inside their own assigned workspace."""
    ws_user_a, _ = multi_user_workspaces
    
    code, stdout, stderr = execute_sandboxed_command(ws_user_a, "echo 'data' > my_script.sh && cat my_script.sh")
    assert code == 0
    assert "data" in stdout
    assert os.path.exists(os.path.join(ws_user_a, "my_script.sh"))
