"""
ServiceManager: Scoped systemd service lifecycle manager.
Executes restart, reload, and status queries strictly against whitelisted services.
Supports TARGET_ADAPTER_SIMULATION=1 for containerized or test environments.
"""
import logging
import os
import shutil
import subprocess
from typing import Tuple, Optional

from .config import validate_target_service
from .executor import TargetExecutorClient, get_executor_mode

logger = logging.getLogger("target_adapter.service_manager")


class ServiceManager:
    def __init__(
        self,
        simulation: Optional[bool] = None,
        mode: Optional[str] = None,
        executor: Optional[TargetExecutorClient] = None,
    ):
        if simulation is not None:
            self.simulation = simulation
        else:
            self.simulation = os.getenv("TARGET_ADAPTER_SIMULATION", "0") == "1"
        self.mode = mode if mode is not None else get_executor_mode()
        self.executor = (
            executor if executor is not None
            else (TargetExecutorClient() if self.mode == "sudo" else None)
        )

    def execute_action(self, action: str, service: str) -> Tuple[int, str, str]:
        """
        Executes an action against an authorized service.
        Returns: (exit_code, stdout, stderr)
        """
        canonical_service = validate_target_service(service)

        if action not in ("service_restart", "service_reload", "service_status"):
            raise ValueError(f"Unsupported service action: {action}")

        if self.simulation or self.mode == "simulation":
            logger.info("[SIMULATION] service action %s on %s", action, canonical_service)
            if action == "service_status":
                return 0, "active\n", ""
            return 0, f"Service {canonical_service} {action.split('_')[1]}ed successfully (simulated)\n", ""

        if self.mode == "sudo":
            return self.executor.service_action(action, canonical_service)

        systemctl_bin = shutil.which("systemctl") or "/usr/bin/systemctl"

        if action == "service_restart":
            cmd = [systemctl_bin, "restart", f"{canonical_service}.service"]
        elif action == "service_reload":
            cmd = [systemctl_bin, "reload", f"{canonical_service}.service"]
        elif action == "service_status":
            cmd = [systemctl_bin, "is-active", f"{canonical_service}.service"]
        else:
            raise ValueError(f"Unknown action: {action}")

        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15,
                shell=False
            )
            return res.returncode, res.stdout, res.stderr
        except subprocess.TimeoutExpired:
            return 124, "", f"Operation timed out after 15s: {' '.join(cmd)}"
        except Exception as e:
            return 1, "", f"Failed to invoke systemctl: {e}"


_GLOBAL_SERVICE_MGR: Optional[ServiceManager] = None


def get_service_manager() -> ServiceManager:
    global _GLOBAL_SERVICE_MGR
    if _GLOBAL_SERVICE_MGR is None:
        _GLOBAL_SERVICE_MGR = ServiceManager()
    return _GLOBAL_SERVICE_MGR
