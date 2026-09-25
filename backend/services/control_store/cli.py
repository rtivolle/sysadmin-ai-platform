#!/usr/bin/env python3
"""Operator CLI for the durable control store.

Run from the repository root:

    backend/.venv/bin/python3 -m services.control_store.cli status
    backend/.venv/bin/python3 -m services.control_store.cli apply-schema
    backend/.venv/bin/python3 -m services.control_store.cli import-file-keys
    backend/.venv/bin/python3 -m services.control_store.cli rotate --user sysadmin-01

Token material is never printed unless the operator asks for ``--stdout``;
``issue`` and ``rotate`` write to a ``0600`` file by default, matching the
platform's rule that secrets live in files.
"""
import argparse
import datetime
import os
import sys
from pathlib import Path

if __package__ in (None, ""):  # allow direct execution: python3 cli.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.control_store import (  # noqa: E402
    ControlStoreError,
    ControlStoreUnavailable,
    KeyStore,
    apply_schema,
    health,
    open_executor,
    open_key_store,
    open_token_ledger,
    password_path,
    redact,
    resolve_dsn,
    store_mode,
)

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_UNAVAILABLE = 3

KEYS_DIR = Path(__file__).resolve().parents[2] / "config" / "keys"


def _require_store(mode_expected: str = "postgres"):
    if store_mode() != mode_expected:
        print(
            "Control store is not selected. Export SYSADMIN_CONTROL_STORE=postgres "
            "(and run backend/config/postgres/postgres.sh provision first).",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_CONFIG)
    dsn = resolve_dsn()
    if not dsn:
        print("No DSN could be resolved for the control store.", file=sys.stderr)
        raise SystemExit(EXIT_CONFIG)
    print(f"target: {redact(dsn)}")
    return open_executor(dsn)


def _write_token(token: str, destination: Optional[Path], to_stdout: bool) -> None:
    if to_stdout:
        print(token)
        return
    target = destination or (KEYS_DIR / "issued.key")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    os.chmod(target, 0o600)
    print(f"wrote the new key to {target} (mode 0600)")


def cmd_status(_args) -> int:
    print(health())
    print(f"password file: {password_path()}")
    return EXIT_OK


def cmd_apply_schema(_args) -> int:
    executor = _require_store()
    with executor.transaction() as tx:
        apply_schema(tx)
    print("control-store schema is up to date")
    return EXIT_OK


def cmd_import_file_keys(args) -> int:
    executor = _require_store()
    store = KeyStore(executor)
    result = store.import_file_keys(Path(args.keys_dir or KEYS_DIR), actor=args.actor)
    print(
        "imported {imported}, already present {existing}, skipped {skipped}".format(**result)
    )
    return EXIT_OK


def cmd_issue(args) -> int:
    store = KeyStore(_require_store())
    token = store.issue(args.user, label=args.label or "", created_by=args.actor)
    _write_token(token, Path(args.out) if args.out else None, args.stdout)
    return EXIT_OK


def cmd_rotate(args) -> int:
    store = KeyStore(_require_store())
    destination = Path(args.out) if args.out else _default_key_file(args.user)
    token = store.rotate(args.user, label=args.label or "", created_by=args.actor)
    _write_token(token, destination, args.stdout)
    return EXIT_OK


def cmd_revoke(args) -> int:
    store = KeyStore(_require_store())
    token = None
    if args.token_file:
        token = Path(args.token_file).read_text().strip()
    revoked = store.revoke(args.user, token=token, actor=args.actor)
    print(f"revoked {revoked} key(s) for {args.user}")
    return EXIT_OK


def cmd_list(args) -> int:
    store = KeyStore(_require_store())
    for row in store.active_keys(args.user):
        created = row["created_at"].isoformat() if row["created_at"] else ""
        print(f"{row['user_id']}\t{row['label']}\t{created}\t{row['created_by']}")
    return EXIT_OK


def cmd_ledger(args) -> int:
    ledger = open_token_ledger()
    if ledger is None:
        print("Control store is not selected; nothing to read.", file=sys.stderr)
        return EXIT_CONFIG
    day = args.day or datetime.date.today().isoformat()
    total = ledger.durable_total(args.user, day)
    pending = ledger.pending_reservations(args.user, day)
    print(f"{args.user} {day}: durable total {total:,} tokens ({pending} unsettled)")
    return EXIT_OK


def cmd_prune(args) -> int:
    executor = _require_store()
    ledger = open_token_ledger(executor=executor)
    print(f"pruned {ledger.prune(args.retention_days)} ledger row(s)")
    return EXIT_OK


def _default_key_file(user_id: str) -> Path:
    name = "emergency-p1" if user_id == "emergency-p1-oncall" else user_id
    return KEYS_DIR / f"{name}.key"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="control-store", description=__doc__)
    parser.add_argument("--actor", default=os.getenv("USER", "operator"))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show mode, DSN (redacted) and reachability").set_defaults(func=cmd_status)
    sub.add_parser("apply-schema", help="create/refresh the platform tables").set_defaults(func=cmd_apply_schema)

    imp = sub.add_parser("import-file-keys", help="bootstrap the store from backend/config/keys")
    imp.add_argument("--keys-dir", default=None)
    imp.set_defaults(func=cmd_import_file_keys)

    issue = sub.add_parser("issue", help="issue a new key for a user")
    issue.add_argument("--user", required=True)
    issue.add_argument("--label", default="")
    issue.add_argument("--out", default=None, help="file to write the key to (mode 0600)")
    issue.add_argument("--stdout", action="store_true", help="print the key to stdout")
    issue.set_defaults(func=cmd_issue)

    rotate = sub.add_parser("rotate", help="atomically replace every live key of a user")
    rotate.add_argument("--user", required=True)
    rotate.add_argument("--label", default="")
    rotate.add_argument("--out", default=None)
    rotate.add_argument("--stdout", action="store_true")
    rotate.set_defaults(func=cmd_rotate)

    revoke = sub.add_parser("revoke", help="revoke one key file or every live key of a user")
    revoke.add_argument("--user", required=True)
    revoke.add_argument("--token-file", default=None)
    revoke.set_defaults(func=cmd_revoke)

    listing = sub.add_parser("list", help="list live keys (metadata only)")
    listing.add_argument("--user", default=None)
    listing.set_defaults(func=cmd_list)

    ledger = sub.add_parser("ledger", help="show the durable day total for a user")
    ledger.add_argument("--user", required=True)
    ledger.add_argument("--day", default=None)
    ledger.set_defaults(func=cmd_ledger)

    prune = sub.add_parser("prune", help="drop ledger rows past the retention window")
    prune.add_argument("--retention-days", type=int, default=30)
    prune.set_defaults(func=cmd_prune)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ControlStoreUnavailable as exc:
        print(f"control store unavailable: {exc}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    except ControlStoreError as exc:
        print(f"control store error: {exc}", file=sys.stderr)
        return EXIT_CONFIG


if __name__ == "__main__":
    raise SystemExit(main())
