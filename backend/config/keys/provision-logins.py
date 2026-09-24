#!/usr/bin/env python3
"""Provision random login passwords and their PBKDF2 hashes once.

The plaintext file is an initial delivery artifact. Move its contents into an
approved password manager, then remove it from the host. Existing credentials
are preserved on reruns; --rotate explicitly replaces them. Use --user to
provision or rotate a single login without touching the others.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile

KEYS_DIR = Path(__file__).resolve().parent
CREDENTIALS_FILE = KEYS_DIR / "login-credentials.json"
INITIAL_PASSWORDS_FILE = KEYS_DIR / "initial-passwords.txt"
USERS = [f"sysadmin-{i:02d}" for i in range(1, 11)] + ["emergency-p1-oncall"]


def write_private(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _write_private_atomic(path: Path, content: str) -> None:
    """Replace a private file atomically with a same-directory temp + os.replace."""
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _persist_private(path: Path, content: str) -> None:
    """Create the file with O_EXCL when absent, otherwise replace it atomically."""
    if path.exists():
        _write_private_atomic(path, content)
    else:
        write_private(path, content)


def _new_credential() -> tuple[str, str]:
    password = secrets.token_urlsafe(32)
    salt = secrets.token_bytes(32)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
    return password, f"{salt.hex()}${digest.hex()}"


def _read_credentials() -> dict:
    if not CREDENTIALS_FILE.exists():
        return {}
    return json.loads(CREDENTIALS_FILE.read_text())


def _update_initial_passwords(user: str, password: str) -> None:
    """Replace the user's line or append it, preserving every other line."""
    new_line = f"{user}: {password}"
    lines: list[str] = []
    if INITIAL_PASSWORDS_FILE.exists():
        lines = INITIAL_PASSWORDS_FILE.read_text().splitlines()
    updated: list[str] = []
    replaced = False
    for line in lines:
        if line.split(":", 1)[0].strip() == user:
            if not replaced:
                updated.append(new_line)
                replaced = True
        else:
            updated.append(line)
    if not replaced:
        updated.append(new_line)
    _persist_private(INITIAL_PASSWORDS_FILE, "".join(f"{line}\n" for line in updated))


def _provision_user(user: str, rotate: bool) -> None:
    credentials = _read_credentials()
    if user in credentials and not rotate:
        print(f"Existing login credential preserved for {user}: {CREDENTIALS_FILE}")
        return
    password, credential = _new_credential()
    credentials[user] = credential
    _persist_private(CREDENTIALS_FILE, json.dumps(credentials, indent=2) + "\n")
    _update_initial_passwords(user, password)
    action = "rotated" if rotate else "provisioned"
    print(f"Login credential {action} for {user}: {CREDENTIALS_FILE}")
    print(f"Deliver initial password securely: {INITIAL_PASSWORDS_FILE}")


def provision(rotate: bool = False, user: str | None = None) -> None:
    if user is not None and user not in USERS:
        raise SystemExit(f"error: unknown user {user!r}; expected one of: {', '.join(USERS)}")
    KEYS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if user is not None:
        _provision_user(user, rotate)
        return
    if CREDENTIALS_FILE.exists() and not rotate:
        print(f"Existing login credentials preserved: {CREDENTIALS_FILE}")
        return
    if not rotate and INITIAL_PASSWORDS_FILE.exists():
        raise RuntimeError("Initial password file exists without hashes; inspect before provisioning")

    passwords = {}
    credentials = {}
    for name in USERS:
        password, credential = _new_credential()
        passwords[name] = password
        credentials[name] = credential

    _persist_private(CREDENTIALS_FILE, json.dumps(credentials, indent=2) + "\n")
    _persist_private(INITIAL_PASSWORDS_FILE, "".join(f"{name}: {password}\n" for name, password in passwords.items()))
    print(f"Login credentials provisioned: {CREDENTIALS_FILE}")
    print(f"Deliver initial passwords securely: {INITIAL_PASSWORDS_FILE}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rotate", action="store_true", help="Replace existing login credentials instead of preserving them")
    parser.add_argument("--user", metavar="NAME", help=f"Provision or rotate only this login; one of: {', '.join(USERS)}")
    args = parser.parse_args()
    provision(args.rotate, args.user)
