import pytest

from backend.services.agent_tools.tools import execute_sandboxed_command


@pytest.fixture
def sandbox_available(tmp_path):
    code, _, stderr = execute_sandboxed_command(str(tmp_path), "true")
    if code == 0:
        return
    if "no delegated cgroup" in stderr or "No permissions to create a new namespace" in stderr:
        pytest.skip("This host does not provide delegated cgroups and unprivileged Bubblewrap namespaces")
    pytest.fail(f"Sandbox startup failed: {stderr}")
