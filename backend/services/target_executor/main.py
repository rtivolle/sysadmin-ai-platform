#!/usr/bin/env python3
"""
sysadmin-target-exec — least-privilege privileged execution boundary.

This program is the ONLY component of the platform that is ever invoked with
privilege. It is installed root-owned at ``/usr/local/libexec/sysadmin-target-exec``
and invoked as::

    sudo -n /usr/local/libexec/sysadmin-target-exec <verb> <args...>

Verbs (fixed set):
    service-restart  <unit>
    service-reload   <unit>
    service-status   <unit>
    config-install   <staged-file> <dest> <sha256>
    config-rollback  <backup> <dest>

It re-validates EVERY operation independently against its own root-owned
allowlist (``/etc/sysadmin-target-exec/allowlist.json``) and refuses any side
effect that is not exactly allow-listed. It is standard-library-only and
imports nothing from the repository, so this single file can be copied to
``/usr/local/libexec/sysadmin-target-exec``.

Every rejection happens *before* any side effect (no ``systemctl`` invocation,
no filesystem write). ``config-install`` is atomic: content is written to a
temporary file in the destination directory, ``fsync``-ed, then renamed over
the destination, with a byte-for-byte backup of the previous file and a
post-install hash verification with automatic rollback.

Exit codes:
    0    success
    1    operation failed (e.g. systemctl returned non-zero)
    2    usage error (unknown verb, missing/extra arguments, malformed hash)
    3    request rejected by validation/allowlist (no side effect taken)
    4    internal error (unexpected exception / unreadable allowlist)
    124  timeout

Every invocation emits exactly one JSON object on stdout, e.g.::

    {"ok": true, "verb": "config-install", "exit_code": 0, "message": "...",
     "dest": "/etc/nginx/nginx.conf", "backup": "/etc/nginx/.nginx.conf.bak.123",
     "sha256": "..."}

The process exit status is ``result["exit_code"]``.
"""
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import uuid

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_REJECTED = 3
EXIT_ERROR = 4
EXIT_TIMEOUT = 124

DEFAULT_ALLOWLIST_PATH = "/etc/sysadmin-target-exec/allowlist.json"
DEFAULT_SYSTEMCTL = "/usr/bin/systemctl"

# Environment overrides. ``TARGET_EXEC_ALLOWLIST_PATH`` lets tests (and only
# tests) relocate the allowlist; in production sudo's env_reset (no SETENV)
# prevents the unprivileged adapter from setting it. ``TARGET_EXEC_SYSTEMCTL``
# is honoured ONLY when the allowlist sets ``test_hooks: true`` — a production
# allowlist MUST NOT set that flag (see docs/security.md).
ENV_ALLOWLIST_PATH = "TARGET_EXEC_ALLOWLIST_PATH"
ENV_SYSTEMCTL = "TARGET_EXEC_SYSTEMCTL"

SERVICE_VERBS = {
    "service-restart": "restart",
    "service-reload": "reload",
    "service-status": "is-active",
}


class _Rejected(Exception):
    """Request rejected by validation/allowlist; no side effect was taken."""


class _AllowlistError(Exception):
    """The allowlist itself is missing or invalid (internal configuration error)."""


def _result(ok, verb, exit_code, message="", **extra):
    res = {"ok": bool(ok), "verb": verb, "exit_code": int(exit_code), "message": str(message)}
    res.update(extra)
    return res


# --------------------------------------------------------------------------- #
# Allowlist loading / schema validation
# --------------------------------------------------------------------------- #

def _load_allowlist(env):
    path = env.get(ENV_ALLOWLIST_PATH) or DEFAULT_ALLOWLIST_PATH
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise _AllowlistError(f"allowlist not readable ({path}): {exc}") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise _AllowlistError(f"allowlist must be a regular file, not a symlink: {path}")
    if st.st_mode & 0o022:
        raise _AllowlistError(f"allowlist must not be group/world writable: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise _AllowlistError(f"allowlist unreadable or not JSON ({path}): {exc}") from exc
    if not isinstance(data, dict):
        raise _AllowlistError(f"allowlist must be a JSON object: {path}")

    staging_dir = data.get("staging_dir")
    if not isinstance(staging_dir, str) or not staging_dir.startswith("/"):
        raise _AllowlistError("allowlist 'staging_dir' must be an absolute path")
    services = data.get("services")
    if not isinstance(services, list) or not all(isinstance(s, str) for s in services):
        raise _AllowlistError("allowlist 'services' must be a list of unit names")
    destinations = data.get("destinations")
    if not isinstance(destinations, list) or len(destinations) == 0:
        raise _AllowlistError("allowlist 'destinations' must be a non-empty list")
    for entry in destinations:
        if isinstance(entry, str):
            if not entry.startswith("/"):
                raise _AllowlistError(f"allowlist destination must be absolute: {entry!r}")
        elif isinstance(entry, dict):
            p = entry.get("path")
            if not isinstance(p, str) or not p.startswith("/"):
                raise _AllowlistError(f"allowlist destination 'path' must be absolute: {entry!r}")
            if entry.get("type", "file") not in ("file", "dir"):
                raise _AllowlistError(f"allowlist destination 'type' must be 'file' or 'dir': {entry!r}")
        else:
            raise _AllowlistError(f"allowlist destination must be a string or object: {entry!r}")
    return data


def _timeout(allowlist):
    t = allowlist.get("timeout_seconds", 30)
    try:
        t = int(t)
    except (TypeError, ValueError):
        t = 30
    return t if t > 0 else 30


def _resolve_systemctl(allowlist, env):
    path = allowlist.get("systemctl") or DEFAULT_SYSTEMCTL
    if bool(allowlist.get("test_hooks")) and env.get(ENV_SYSTEMCTL):
        # Test-only hook: a fake systemctl. Never set test_hooks in production.
        path = env[ENV_SYSTEMCTL]
    if not isinstance(path, str) or not path.startswith("/"):
        raise _AllowlistError("'systemctl' must be an absolute path")
    return path


# --------------------------------------------------------------------------- #
# Path validation helpers (no symlinks, no traversal)
# --------------------------------------------------------------------------- #

def _has_traversal(path):
    if "\x00" in path:
        return True
    return ".." in path.split("/")


def _symlink_ancestor(path):
    """Return the first symlink ancestor of an absolute path, or None.

    Checks every existing ancestor directory; the final component is checked
    separately by the caller.
    """
    comps = [c for c in path.split("/") if c]
    cur = ""
    for c in comps[:-1]:
        cur += "/" + c
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            break
        if stat.S_ISLNK(st.st_mode):
            return cur
    return None


def _resolve_dest(dest):
    if not isinstance(dest, str) or not dest:
        raise _Rejected("destination path is required")
    if not dest.startswith("/"):
        raise _Rejected(f"destination must be an absolute path: {dest!r}")
    if _has_traversal(dest):
        raise _Rejected(f"destination contains path traversal: {dest!r}")
    link = _symlink_ancestor(dest)
    if link:
        raise _Rejected(f"destination has a symlink in its path: {link}")
    if os.path.islink(dest):
        raise _Rejected(f"destination is itself a symlink: {dest!r}")
    return os.path.realpath(dest)


def _allowlist_match(allowlist, real_dest):
    for entry in allowlist.get("destinations", []):
        if isinstance(entry, str):
            entry = {"path": entry, "type": "file"}
        path = entry.get("path")
        etype = entry.get("type", "file")
        real_entry = os.path.realpath(path)
        if etype == "dir":
            if real_dest == real_entry or real_dest.startswith(real_entry + "/"):
                return entry
        else:  # file
            if real_dest == real_entry:
                return entry
    return None


# --------------------------------------------------------------------------- #
# Mode / owner resolution
# --------------------------------------------------------------------------- #

def _parse_mode(value):
    if isinstance(value, int):
        return value & 0o7777
    if isinstance(value, str):
        try:
            return int(value, 8) & 0o7777
        except ValueError:
            return None
    return None


def _resolve_uid(value):
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        if value.isdigit():
            return int(value)
        try:
            import pwd
            return pwd.getpwnam(value).pw_uid
        except (ImportError, KeyError):
            return None
    return None


def _resolve_gid(value):
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        if value.isdigit():
            return int(value)
        try:
            import grp
            return grp.getgrnam(value).gr_gid
        except (ImportError, KeyError):
            return None
    return None


def _resolve_owner(value):
    if not value:
        return None, None
    if isinstance(value, str):
        if ":" in value:
            u, g = value.split(":", 1)
            return _resolve_uid(u), _resolve_gid(g)
        return _resolve_uid(value), _resolve_gid(value)
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return _resolve_uid(value[0]), _resolve_gid(value[1])
    return None, None


# --------------------------------------------------------------------------- #
# Verbs
# --------------------------------------------------------------------------- #

def _run_service(verb, unit, allowlist, env):
    services = allowlist.get("services", [])
    normalized = unit
    if isinstance(normalized, str) and normalized.endswith(".service"):
        normalized = normalized[: -len(".service")]
    if normalized not in services:
        return _result(False, verb, EXIT_REJECTED, f"unit {unit!r} is not allow-listed")
    systemctl = _resolve_systemctl(allowlist, env)
    sub = SERVICE_VERBS[verb]
    cmd = [systemctl, sub, f"{normalized}.service"]
    timeout = _timeout(allowlist)
    child_env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=False, env=child_env)
    except subprocess.TimeoutExpired:
        return _result(False, verb, EXIT_TIMEOUT, f"systemctl timed out after {timeout}s", unit=normalized, stdout="", stderr="timeout")
    except OSError as exc:
        return _result(False, verb, EXIT_FAILED, f"failed to invoke systemctl: {exc}", unit=normalized, stdout="", stderr=str(exc))
    ok = proc.returncode == 0
    return _result(
        ok, verb, proc.returncode,
        f"systemctl {sub} {normalized}.service exited {proc.returncode}",
        unit=normalized, stdout=proc.stdout or "", stderr=proc.stderr or "",
    )


def _validate_staged(staged, allowlist):
    if not isinstance(staged, str) or not staged:
        raise _Rejected("staged file path is required")
    if not staged.startswith("/"):
        raise _Rejected(f"staged file must be an absolute path: {staged!r}")
    if _has_traversal(staged):
        raise _Rejected(f"staged file contains path traversal: {staged!r}")
    link = _symlink_ancestor(staged)
    if link:
        raise _Rejected(f"staged file has a symlink in its path: {link}")
    try:
        st = os.lstat(staged)
    except OSError as exc:
        raise _Rejected(f"staged file not found: {staged!r}") from exc
    if stat.S_ISLNK(st.st_mode):
        raise _Rejected(f"staged file is a symlink: {staged!r}")
    if not stat.S_ISREG(st.st_mode):
        raise _Rejected(f"staged file is not a regular file: {staged!r}")
    if st.st_mode & 0o022:
        raise _Rejected(f"staged file must not be group/world writable: {staged!r}")

    staging = allowlist.get("staging") or {}
    owner = staging.get("owner")
    if owner:
        if owner == "nonroot":
            if st.st_uid == 0:
                raise _Rejected(f"staged file must not be root-owned: {staged!r}")
        elif owner == "root":
            if st.st_uid != 0:
                raise _Rejected(f"staged file must be root-owned: {staged!r}")
        else:
            uid = _resolve_uid(owner)
            if uid is not None and st.st_uid != uid:
                raise _Rejected(f"staged file owner mismatch: expected {owner!r}, got uid {st.st_uid}")
    mode = staging.get("mode")
    if mode is not None:
        want = _parse_mode(mode)
        if want is not None and stat.S_IMODE(st.st_mode) != want:
            raise _Rejected(f"staged file mode mismatch: expected {oct(want)}, got {oct(stat.S_IMODE(st.st_mode))}")

    staging_dir = allowlist.get("staging_dir")
    real_staged = os.path.realpath(staged)
    real_staging = os.path.realpath(staging_dir)
    if os.path.dirname(real_staged) != real_staging:
        raise _Rejected(f"staged file must live directly inside the staging dir {staging_dir}")
    return real_staged


def _fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_install(dest, content, allowlist):
    """Write content to dest atomically; returns (backup_path_or_None, installed_sha256)."""
    dest_dir = os.path.dirname(dest)
    dest_name = os.path.basename(dest)
    existed = os.path.lexists(dest)

    if existed:
        st = os.lstat(dest)
        mode = stat.S_IMODE(st.st_mode)
        uid, gid = st.st_uid, st.st_gid
    else:
        policy = allowlist.get("file_policy") or {}
        mode = _parse_mode(policy.get("default_mode", "0600"))
        if mode is None:
            mode = 0o600
        uid, gid = _resolve_owner(policy.get("default_owner"))

    backup = None
    if existed:
        backup = os.path.join(dest_dir, f".{dest_name}.bak.{int(time.time())}_{uuid.uuid4().hex}")
        with open(dest, "rb") as src:
            with open(backup, "xb") as dst:
                dst.write(src.read())
                dst.flush()
                os.fsync(dst.fileno())
        os.chmod(backup, mode)
        try:
            os.chown(backup, uid, gid)
        except (OSError, PermissionError):
            pass

    fd, tmp = tempfile.mkstemp(prefix=f".{dest_name}.tmp.", dir=dest_dir)
    try:
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
                os.fchmod(f.fileno(), mode)
                euid, egid = os.geteuid(), os.getegid()
                if uid is not None and gid is not None and (uid, gid) != (euid, egid):
                    os.fchown(f.fileno(), uid, gid)
        except BaseException:
            raise
        os.replace(tmp, dest)
        _fsync_dir(dest_dir)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    with open(dest, "rb") as f:
        installed = f.read()
    return backup, hashlib.sha256(installed).hexdigest()


def _run_config_install(staged, dest, sha256, allowlist, env):
    verb = "config-install"
    if not _is_sha256(sha256):
        return _result(False, verb, EXIT_USAGE, "sha256 argument is not a 64-character hex digest")
    sha256 = sha256.lower()

    try:
        real_dest = _resolve_dest(dest)
    except _Rejected as exc:
        return _result(False, verb, EXIT_REJECTED, str(exc), dest=dest)
    if _allowlist_match(allowlist, real_dest) is None:
        return _result(False, verb, EXIT_REJECTED, f"destination {dest!r} is not allow-listed", dest=dest)

    try:
        real_staged = _validate_staged(staged, allowlist)
    except _Rejected as exc:
        return _result(False, verb, EXIT_REJECTED, str(exc), staged=staged)

    try:
        with open(real_staged, "rb") as f:
            content = f.read()
    except OSError as exc:
        return _result(False, verb, EXIT_ERROR, f"cannot read staged file: {exc}", staged=staged)

    actual = hashlib.sha256(content).hexdigest()
    if actual != sha256:
        return _result(
            False, verb, EXIT_REJECTED,
            f"staged file sha256 mismatch: expected {sha256}, computed {actual}",
            staged=staged, sha256=actual,
        )

    try:
        backup, installed_sha = _atomic_install(real_dest, content, allowlist)
    except _Rejected as exc:
        return _result(False, verb, EXIT_REJECTED, str(exc), dest=dest)
    except OSError as exc:
        return _result(False, verb, EXIT_ERROR, f"install failed: {exc}", dest=dest)

    # The rename is atomic and the content was fsync-ed, so a mismatch here
    # means the destination was modified by a concurrent writer after our
    # rename. Report it rather than roll back (rolling back could clobber that
    # writer). The adapter's approval-bound base-hash check guards the normal
    # single-writer case.
    if installed_sha != sha256:
        return _result(
            False, verb, EXIT_ERROR,
            f"post-install hash mismatch: installed {installed_sha}, expected {sha256}",
            dest=real_dest, backup=backup, sha256=installed_sha,
        )

    return _result(True, verb, EXIT_OK, f"installed {real_dest}", dest=real_dest, backup=backup, sha256=installed_sha)


def _run_config_rollback(backup, dest, allowlist, env):
    verb = "config-rollback"
    try:
        real_dest = _resolve_dest(dest)
    except _Rejected as exc:
        return _result(False, verb, EXIT_REJECTED, str(exc), dest=dest)
    if _allowlist_match(allowlist, real_dest) is None:
        return _result(False, verb, EXIT_REJECTED, f"destination {dest!r} is not allow-listed", dest=dest)

    if not isinstance(backup, str) or not backup.startswith("/"):
        return _result(False, verb, EXIT_REJECTED, "backup must be an absolute path")
    if _has_traversal(backup):
        return _result(False, verb, EXIT_REJECTED, "backup contains path traversal")
    if os.path.dirname(os.path.realpath(backup)) != os.path.dirname(real_dest):
        return _result(False, verb, EXIT_REJECTED, "backup must live in the same directory as dest")
    dest_name = os.path.basename(real_dest)
    if not os.path.basename(backup).startswith(f".{dest_name}.bak."):
        return _result(False, verb, EXIT_REJECTED, "backup filename does not match the executor backup pattern")
    try:
        st = os.lstat(backup)
    except OSError:
        return _result(False, verb, EXIT_REJECTED, f"backup not found: {backup!r}")
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return _result(False, verb, EXIT_REJECTED, "backup must be a regular file (not a symlink)")

    try:
        os.replace(backup, real_dest)
        _fsync_dir(os.path.dirname(real_dest))
    except OSError as exc:
        return _result(False, verb, EXIT_ERROR, f"rollback failed: {exc}")

    return _result(True, verb, EXIT_OK, f"rolled back {real_dest} from {backup}", dest=real_dest, backup=backup)


# --------------------------------------------------------------------------- #
# Dispatcher
# --------------------------------------------------------------------------- #

def _is_sha256(value):
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
        return True
    except ValueError:
        return False


def run(argv, env=None):
    """Execute the executor with args ``argv`` (list, excluding program name).

    Returns a JSON-serialisable result dict with at least ``ok`` and ``exit_code``.
    ``env`` is a mapping used instead of ``os.environ`` when supplied (tests).
    """
    env = dict(os.environ if env is None else env)
    if not argv:
        return _result(False, None, EXIT_USAGE, "usage: sysadmin-target-exec <verb> <args...>")
    verb = argv[0]
    args = argv[1:]

    try:
        allowlist = _load_allowlist(env)
    except _AllowlistError as exc:
        return _result(False, verb, EXIT_ERROR, str(exc))

    if verb in SERVICE_VERBS:
        if len(args) != 1:
            return _result(False, verb, EXIT_USAGE, f"{verb} expects exactly one <unit> argument")
        return _run_service(verb, args[0], allowlist, env)
    if verb == "config-install":
        if len(args) != 3:
            return _result(False, verb, EXIT_USAGE, "config-install expects <staged-file> <dest> <sha256>")
        return _run_config_install(args[0], args[1], args[2], allowlist, env)
    if verb == "config-rollback":
        if len(args) != 2:
            return _result(False, verb, EXIT_USAGE, "config-rollback expects <backup> <dest>")
        return _run_config_rollback(args[0], args[1], allowlist, env)
    return _result(False, verb, EXIT_USAGE, f"unknown verb {verb!r}")


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    try:
        result = run(argv, os.environ)
    except Exception as exc:  # noqa: BLE001 - last-resort, no side effect taken
        result = _result(False, argv[0] if argv else None, EXIT_ERROR, f"internal error: {exc}")
    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()
    return int(result.get("exit_code", EXIT_ERROR))


if __name__ == "__main__":
    sys.exit(main())
