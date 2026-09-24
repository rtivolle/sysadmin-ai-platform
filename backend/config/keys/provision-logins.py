#!/usr/bin/env python3
"""Provision random login passwords and their PBKDF2 hashes once.

The plaintext file is an initial delivery artifact. Move its contents into an
approved password manager, then remove it from the host. Existing credentials
are preserved on reruns; --rotate explicitly replaces them.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets

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


def provision(rotate: bool = False) -> None:
    KEYS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if CREDENTIALS_FILE.exists() and not rotate:
        print(f"Existing login credentials preserved: {CREDENTIALS_FILE}")
        return
    if rotate:
        CREDENTIALS_FILE.unlink(missing_ok=True)
        INITIAL_PASSWORDS_FILE.unlink(missing_ok=True)
    elif INITIAL_PASSWORDS_FILE.exists():
        raise RuntimeError("Initial password file exists without hashes; inspect before provisioning")

    passwords = {user: secrets.token_urlsafe(32) for user in USERS}
    credentials = {}
    for user, password in passwords.items():
        salt = secrets.token_bytes(32)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
        credentials[user] = f"{salt.hex()}${digest.hex()}"

    write_private(CREDENTIALS_FILE, json.dumps(credentials, indent=2) + "\n")
    write_private(INITIAL_PASSWORDS_FILE, "".join(f"{user}: {password}\n" for user, password in passwords.items()))
    print(f"Login credentials provisioned: {CREDENTIALS_FILE}")
    print(f"Deliver initial passwords securely: {INITIAL_PASSWORDS_FILE}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rotate", action="store_true", help="Replace all login credentials")
    args = parser.parse_args()
    provision(args.rotate)
