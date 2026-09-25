"""
Executor backend for the Scoped Target Adapter.

Selects how ``ServiceManager`` and ``ConfigDeployer`` perform privileged work:

  simulation : no side effects; log and return synthetic success (dev/tests).
  direct     : the adapter performs work in-process (current default; dev only).
  sudo       : the adapter stages content and delegates **every** privileged
               side effect to the standalone least-privilege executor via
               ``sudo -n /usr/local/libexec/sysadmin-target-exec <verb> <args>``.

Mode is selected by ``TARGET_ADAPTER_EXECUTOR`` (default ``"direct"``). A
production deployment must run with ``TARGET_ADAPTER_EXECUTOR=sudo``; in that
mode the adapter never writes to the destination itself — it only stages into
the staging directory and parses the executor's JSON result. The executor
re-validates every operation against its own root-owned allowlist, so the
adapter's own allowlists (``target_adapter/config.py``) are a fail-fast layer,
not the security boundary.

Staging-directory coordination: the adapter writes staged files into
``TARGET_ADAPTER_STAGING_DIR`` (default ``/var/lib/sysadmin-target-exec/staging``).
This must equal the ``staging_dir`` in the executor's allowlist, otherwise the
executor rejects every staged file. ``install-executor.sh`` writes both
consistently.
"""
import hashlib
import json
import logging
import os
import subprocess
import uuid

logger = logging.getLogger("target_adapter.executor")

EXECUTOR_PATH = "/usr/local/libexec/sysadmin-target-exec"
DEFAULT_STAGING_DIR = "/var/lib/sysadmin-target-exec/staging"
DEFAULT_SUDO_BIN = "sudo"

VALID_MODES = ("sudo", "direct", "simulation")

_SERVICE_VERBS = {
    "service_restart": "service-restart",
    "service_reload": "service-reload",
    "service_status": "service-status",
}


def get_executor_mode() -> str:
    """Return the executor backend mode from TARGET_ADAPTER_EXECUTOR."""
    mode = os.getenv("TARGET_ADAPTER_EXECUTOR", "direct").strip().lower()
    if mode not in VALID_MODES:
        logger.warning("Unknown TARGET_ADAPTER_EXECUTOR=%r; falling back to 'direct'", mode)
        return "direct"
    return mode


class TargetExecutorClient:
    """Unprivileged client that stages content and invokes the executor via sudo."""

    def __init__(
        self,
        executor_path: str = None,
        staging_dir: str = None,
        sudo_bin: str = None,
        timeout: int = 30,
    ):
        self.executor_path = executor_path or os.getenv("TARGET_EXEC_PATH", EXECUTOR_PATH)
        self.staging_dir = staging_dir or os.getenv("TARGET_ADAPTER_STAGING_DIR", DEFAULT_STAGING_DIR)
        self.sudo_bin = sudo_bin or os.getenv("TARGET_ADAPTER_SUDO_BIN", DEFAULT_SUDO_BIN)
        self.timeout = timeout

    # -- invocation ----------------------------------------------------------

    def _invoke(self, verb: str, args) -> dict:
        cmd = [self.sudo_bin, "-n", self.executor_path, verb, *args]
        # Minimal environment: never leak the adapter's environment (including
        # any TARGET_EXEC_* override) into the privileged process.
        env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"}
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout, shell=False, env=env
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "exit_code": 124, "message": f"executor timed out after {self.timeout}s", "stdout": "", "stderr": ""}
        except OSError as exc:
            return {"ok": False, "exit_code": 1, "message": f"failed to invoke executor: {exc}", "stdout": "", "stderr": str(exc)}
        data = self._parse_json(proc.stdout)
        if data is None:
            return {
                "ok": False,
                "exit_code": 1,
                "message": f"executor returned non-JSON output (rc={proc.returncode})",
                "stdout": (proc.stdout or "")[:500],
                "stderr": (proc.stderr or "")[:500],
            }
        data.setdefault("_returncode", proc.returncode)
        return data

    @staticmethod
    def _parse_json(text):
        text = (text or "").strip()
        if not text:
            return None
        try:
            data = json.loads(text)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    # -- staging -------------------------------------------------------------

    def stage_content(self, content: str) -> str:
        """Write content into the staging dir (mode 0600) and return its path."""
        os.makedirs(self.staging_dir, exist_ok=True)
        path = os.path.join(self.staging_dir, f"stage-{uuid.uuid4().hex}")
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        return path

    def cleanup_staged(self, path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

    # -- verbs ---------------------------------------------------------------

    def service_action(self, action: str, service: str):
        verb = _SERVICE_VERBS[action]
        result = self._invoke(verb, [service])
        code = int(result.get("exit_code", 1))
        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "") or ("" if result.get("ok") else result.get("message", ""))
        return code, stdout, stderr

    def config_install(self, staged_path: str, dest: str, sha256: str) -> dict:
        return self._invoke("config-install", [staged_path, dest, sha256])

    def config_rollback(self, backup: str, dest: str) -> dict:
        return self._invoke("config-rollback", [backup, dest])
