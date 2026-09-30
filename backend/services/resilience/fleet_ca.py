"""
fleet_ca: local fleet Certificate Authority for mTLS between platform and
inference (GPU) nodes.

Layout (all under ``backend/config/keys/fleet/``, Git-ignored, umask 077):

    fleet/
        ca.crt            # self-signed CA certificate (public)
        ca.key            # CA private key (0600) — never leaves the platform node
        ca.srl / index.txt / crlnumber   # openssl CA database files
        ca.crl            # certificate revocation list (published to nodes)
        certs/<name>.crt  # issued node / platform certificates
        private/<name>.key# private keys (0600)
        csrs/<name>.csr   # retained CSRs (audit trail)

Design notes (spec ARCHITECTURE.md §10):
- The CA lives on the **platform** node only. GPU nodes receive only their own
  ``<name>.key`` + ``<name>.crt`` + ``ca.crt`` (to verify the platform).
- Node identity = certificate subject CN = node name (e.g. ``gpu-01``).
- Issued certs carry ``serverAuth`` + ``clientAuth`` extended key usage because
  the same identity speaks both ways (node-agent server + client to platform).
- Revocation is a local CRL (``ca.crl``), regenerated on every ``revoke-node``.
- Everything goes through ``openssl`` in a subprocess; the module fails closed
  with a clear message when ``openssl`` is missing.

This module is stdlib-only so ``fleet-ca.sh`` (called by ``install.sh`` during
provisioning, before any venv exists) can use it with the system ``python3``.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_KEYS_DIR = BACKEND_DIR / "config" / "keys" / "fleet"

# Node names become certificate CNs and filesystem names: keep them strict.
NODE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Node certificate lifetime: 825 days (max broadly accepted by TLS clients).
NODE_CERT_DAYS = 825
# CA lifetime: 10 years.
CA_CERT_DAYS = 3650

PLATFORM_CN = "sysadmin-platform"


class FleetCAError(RuntimeError):
    """Fail-closed error: the requested CA operation could not be performed."""


def ensure_openssl() -> str:
    path = shutil.which("openssl")
    if not path:
        raise FleetCAError(
            "openssl is not installed or not on PATH; cannot manage the fleet CA. "
            "Install OpenSSL (e.g. 'apt install openssl') and retry."
        )
    return path


def validate_node_name(name: str) -> str:
    if not isinstance(name, str) or not NODE_NAME_RE.match(name):
        raise FleetCAError(
            f"invalid node name {name!r}: must match "
            f"{NODE_NAME_RE.pattern} (this becomes the certificate CN)"
        )
    return name


def _run(args: List[str], **kwargs) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, text=True, **kwargs)
    except OSError as e:
        raise FleetCAError(f"failed to execute {' '.join(args)}: {e}")


def _fleet_dir(keys_dir: Optional[Path] = None) -> Path:
    return Path(keys_dir) if keys_dir is not None else DEFAULT_KEYS_DIR


def _with_umask_077():
    old = os.umask(0o077)
    return old


# ---------------------------------------------------------------------------
# CA database helpers
# ---------------------------------------------------------------------------

_CA_CONFIG_TEMPLATE = """\
[ ca ]
default_ca = fleet_ca

[ fleet_ca ]
dir             = {fleet_dir}
database        = $dir/index.txt
new_certs_dir   = $dir/certs
certificate     = $dir/ca.crt
private_key     = $dir/ca.key
serial          = $dir/serial
crlnumber       = $dir/crlnumber
default_md      = sha256
default_days    = {days}
default_crl_days = 30
policy          = policy_any
x509_extensions = node_ext
copy_extensions = none
unique_subject  = no

[ policy_any ]
commonName = supplied

[ node_ext ]
basicConstraints   = CA:FALSE
keyUsage           = critical, digitalSignature, keyEncipherment
extendedKeyUsage   = serverAuth, clientAuth
subjectAltName     = @alt_names

[ alt_names ]
DNS.1 = {cn}
"""


def _write_ca_config(fleet_dir: Path, cn: str) -> Path:
    cfg = fleet_dir / "openssl-ca.cnf"
    cfg.write_text(
        _CA_CONFIG_TEMPLATE.format(fleet_dir=str(fleet_dir), days=NODE_CERT_DAYS, cn=cn),
        encoding="utf-8",
    )
    os.chmod(cfg, 0o600)
    return cfg


def _require_ca(fleet_dir: Path) -> None:
    if not (fleet_dir / "ca.crt").is_file() or not (fleet_dir / "ca.key").is_file():
        raise FleetCAError(
            f"no fleet CA in {fleet_dir}; run 'fleet-ca.sh init-ca' first."
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def init_ca(keys_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Create a new self-signed fleet CA (idempotent: refuses to overwrite)."""
    ensure_openssl()
    fleet_dir = _fleet_dir(keys_dir)
    if (fleet_dir / "ca.crt").exists() or (fleet_dir / "ca.key").exists():
        raise FleetCAError(
            f"fleet CA already exists in {fleet_dir}; refusing to overwrite. "
            "Delete it explicitly if you really want a fresh CA (all node "
            "certificates become invalid)."
        )
    old_umask = _with_umask_077()
    try:
        (fleet_dir / "certs").mkdir(parents=True, exist_ok=True)
        (fleet_dir / "private").mkdir(parents=True, exist_ok=True)
        (fleet_dir / "csrs").mkdir(parents=True, exist_ok=True)
        (fleet_dir / "index.txt").write_text("", encoding="utf-8")
        (fleet_dir / "crlnumber").write_text("1000\n", encoding="utf-8")
        (fleet_dir / "serial").write_text("1000\n", encoding="utf-8")
        for f in ("index.txt", "crlnumber", "serial"):
            os.chmod(fleet_dir / f, 0o600)

        key = fleet_dir / "ca.key"
        r = _run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(key)])
        if r.returncode != 0:
            raise FleetCAError(f"openssl genpkey failed: {r.stderr.strip()}")
        os.chmod(key, 0o600)

        crt = fleet_dir / "ca.crt"
        r = _run(
            [
                "openssl", "req", "-x509", "-new",
                "-key", str(key),
                "-days", str(CA_CERT_DAYS),
                "-subj", "/CN=sysadmin-fleet-ca/O=Sysadmin AI Platform",
                "-out", str(crt),
            ]
        )
        if r.returncode != 0:
            raise FleetCAError(f"openssl req -x509 failed: {r.stderr.strip()}")
        os.chmod(crt, 0o644)
    finally:
        os.umask(old_umask)
    return {"ca_crt": str(fleet_dir / "ca.crt"), "ca_key": str(fleet_dir / "ca.key")}


def _issue(fleet_dir: Path, name: str, force: bool) -> Dict[str, Any]:
    ensure_openssl()
    validate_node_name(name)
    _require_ca(fleet_dir)

    cert_path = fleet_dir / "certs" / f"{name}.crt"
    key_path = fleet_dir / "private" / f"{name}.key"
    if cert_path.exists() and not force:
        return {"name": name, "cert": str(cert_path), "key": str(key_path), "reused": True}

    old_umask = _with_umask_077()
    try:
        cfg = _write_ca_config(fleet_dir, name)
        csr_path = fleet_dir / "csrs" / f"{name}.csr"

        r = _run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(key_path)])
        if r.returncode != 0:
            raise FleetCAError(f"openssl genpkey failed for {name}: {r.stderr.strip()}")
        os.chmod(key_path, 0o600)

        r = _run(
            [
                "openssl", "req", "-new",
                "-key", str(key_path),
                "-subj", f"/CN={name}",
                "-out", str(csr_path),
            ]
        )
        if r.returncode != 0:
            raise FleetCAError(f"openssl req failed for {name}: {r.stderr.strip()}")

        r = _run(
            [
                "openssl", "ca", "-batch",
                "-config", str(cfg),
                "-in", str(csr_path),
                "-out", str(cert_path),
                "-days", str(NODE_CERT_DAYS),
                "-notext",
            ]
        )
        if r.returncode != 0:
            raise FleetCAError(f"openssl ca failed for {name}: {r.stderr.strip()}")
        os.chmod(cert_path, 0o644)
    finally:
        os.umask(old_umask)
    return {"name": name, "cert": str(cert_path), "key": str(key_path), "reused": False}


def issue_node_cert(
    name: str, keys_dir: Optional[Path] = None, force: bool = False
) -> Dict[str, Any]:
    """Issue (or reuse) a certificate for an inference node named ``name``."""
    return _issue(_fleet_dir(keys_dir), name, force)


def issue_platform_cert(
    keys_dir: Optional[Path] = None, force: bool = False
) -> Dict[str, Any]:
    """Issue (or reuse) the platform node's own fleet certificate."""
    return _issue(_fleet_dir(keys_dir), PLATFORM_CN, force)


def revoke_node_cert(name: str, keys_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Revoke a node certificate and regenerate the CRL."""
    ensure_openssl()
    validate_node_name(name)
    fleet_dir = _fleet_dir(keys_dir)
    _require_ca(fleet_dir)
    cert_path = fleet_dir / "certs" / f"{name}.crt"
    if not cert_path.is_file():
        raise FleetCAError(f"no certificate for node {name!r} in {fleet_dir}/certs/")

    cfg = _write_ca_config(fleet_dir, name)
    r = _run(["openssl", "ca", "-config", str(cfg), "-revoke", str(cert_path)])
    if r.returncode != 0 and "already revoked" not in (r.stderr or "").lower():
        raise FleetCAError(f"openssl ca -revoke failed for {name}: {r.stderr.strip()}")

    crl_path = fleet_dir / "ca.crl"
    r = _run(["openssl", "ca", "-config", str(cfg), "-gencrl", "-out", str(crl_path)])
    if r.returncode != 0:
        raise FleetCAError(f"openssl ca -gencrl failed: {r.stderr.strip()}")
    os.chmod(crl_path, 0o644)
    return {"name": name, "revoked": True, "crl": str(crl_path)}


def _cert_not_after(cert_path: Path) -> Optional[str]:
    r = _run(["openssl", "x509", "-noout", "-enddate", "-in", str(cert_path)])
    if r.returncode != 0:
        return None
    return r.stdout.strip().replace("notAfter=", "")


def list_certs(keys_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """List issued certificates with CN, status (from the CA index) and expiry."""
    fleet_dir = _fleet_dir(keys_dir)
    index_path = fleet_dir / "index.txt"
    status_by_cn: Dict[str, str] = {}
    if index_path.is_file():
        for line in index_path.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            # index.txt format: <status>\t<expiry>\t<revocation date>\t<serial>\t<filename>\t<DN>
            if len(parts) >= 6:
                m = re.search(r"/CN=([^/]+)", parts[5])
                if m:
                    status_by_cn[m.group(1)] = "revoked" if parts[0] == "R" else "valid"
    out: List[Dict[str, Any]] = []
    certs_dir = fleet_dir / "certs"
    if certs_dir.is_dir():
        for cert_path in sorted(certs_dir.glob("*.crt")):
            cn = cert_path.stem
            r = _run(["openssl", "x509", "-noout", "-subject", "-in", str(cert_path)])
            subject = r.stdout.strip() if r.returncode == 0 else ""
            out.append(
                {
                    "name": cn,
                    "subject": subject,
                    "status": status_by_cn.get(cn, "unknown"),
                    "not_after": _cert_not_after(cert_path),
                }
            )
    return out


def cert_fingerprint(cert_path: Path) -> Optional[str]:
    r = _run(["openssl", "x509", "-noout", "-fingerprint", "-sha256", "-in", str(cert_path)])
    if r.returncode != 0:
        return None
    return r.stdout.strip()


# ---------------------------------------------------------------------------
# CLI (also used by fleet-ca.sh)
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fleet-ca",
        description="Fleet Certificate Authority for platform<->GPU mTLS. "
        "Keys live under backend/config/keys/fleet/ (Git-ignored, umask 077). "
        "Target RPO/RTO for the platform node: RPO <= 15 min, RTO <= 2 h (spec §8.2).",
    )
    p.add_argument(
        "--keys-dir",
        default=None,
        help="override the fleet keys directory (default: backend/config/keys/fleet)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init-ca", help="create the self-signed fleet CA (refuses if one exists)")

    issue = sub.add_parser("issue-node", help="issue (or reuse) a node certificate")
    issue.add_argument("name", help="node name, becomes the certificate CN")
    issue.add_argument("--force", action="store_true", help="regenerate even if a cert exists")

    plat = sub.add_parser("issue-platform", help="issue (or reuse) the platform certificate")
    plat.add_argument("--force", action="store_true", help="regenerate even if a cert exists")

    revoke = sub.add_parser("revoke-node", help="revoke a node certificate and regenerate the CRL")
    revoke.add_argument("name", help="node name whose certificate is revoked")

    sub.add_parser("list", help="list issued certificates with status and expiry")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    keys_dir = Path(args.keys_dir) if args.keys_dir else None
    try:
        if args.command == "init-ca":
            res = init_ca(keys_dir)
            print(f"CA initialised: {res['ca_crt']}")
        elif args.command == "issue-node":
            res = issue_node_cert(args.name, keys_dir, force=args.force)
            print(("reused existing" if res["reused"] else "issued") + f" certificate for node {res['name']!r}: {res['cert']}")
        elif args.command == "issue-platform":
            res = issue_platform_cert(keys_dir, force=args.force)
            print(("reused existing" if res["reused"] else "issued") + f" platform certificate: {res['cert']}")
        elif args.command == "revoke-node":
            res = revoke_node_cert(args.name, keys_dir)
            print(f"revoked certificate for node {res['name']!r}; CRL: {res['crl']}")
        elif args.command == "list":
            certs = list_certs(keys_dir)
            if not certs:
                print("no certificates issued yet")
            for c in certs:
                print(f"{c['name']:24} {c['status']:8} expires {c['not_after']}  {c['subject']}")
        return 0
    except FleetCAError as e:
        print(f"fleet-ca: error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
