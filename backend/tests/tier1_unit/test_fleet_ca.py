"""Tier 1 unit tests: fleet CA (mTLS PKI for platform <-> GPU nodes).

Exercises init-ca + issue-node + revoke-node in a tmpdir (never the real
backend/config/keys/). Skips when openssl is absent.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.services.resilience import fleet_ca
from backend.services.resilience.fleet_ca import (
    FleetCAError,
    issue_node_cert,
    issue_platform_cert,
    init_ca,
    list_certs,
    revoke_node_cert,
    validate_node_name,
)

pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl is required for fleet CA tests"
)


@pytest.fixture()
def fleet_dir(tmp_path: Path) -> Path:
    d = tmp_path / "fleet"
    init_ca(d)
    return d


def _subject_cn(cert_path: Path) -> str:
    r = subprocess.run(
        ["openssl", "x509", "-noout", "-subject", "-in", str(cert_path)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    m = re.search(r"CN\s*=\s*([^\s/,]+)", r.stdout)
    assert m, f"no CN in subject: {r.stdout!r}"
    return m.group(1)


def test_init_ca_creates_layout_with_0600_key(fleet_dir: Path):
    assert (fleet_dir / "ca.crt").is_file()
    key = fleet_dir / "ca.key"
    assert key.is_file()
    assert oct(key.stat().st_mode & 0o777) == "0o600"
    # Refuses to overwrite an existing CA (fail closed).
    with pytest.raises(FleetCAError, match="already exists"):
        init_ca(fleet_dir)


def test_issue_node_cn_matches_name(fleet_dir: Path):
    res = issue_node_cert("gpu-01", fleet_dir)
    cert = Path(res["cert"])
    assert cert.is_file()
    assert _subject_cn(cert) == "gpu-01"
    key = Path(res["key"])
    assert oct(key.stat().st_mode & 0o777) == "0o600"


def test_issue_node_idempotent_without_force(fleet_dir: Path):
    first = issue_node_cert("gpu-01", fleet_dir)
    assert first["reused"] is False
    second = issue_node_cert("gpu-01", fleet_dir)
    assert second["reused"] is True
    assert second["cert"] == first["cert"]
    forced = issue_node_cert("gpu-01", fleet_dir, force=True)
    assert forced["reused"] is False


def test_issue_platform(fleet_dir: Path):
    res = issue_platform_cert(fleet_dir)
    assert _subject_cn(Path(res["cert"])) == "sysadmin-platform"


def test_invalid_node_name_rejected(fleet_dir: Path):
    for bad in ["bad name!", "../evil", "", "-leading", "a" * 70, "gpu/01"]:
        with pytest.raises(FleetCAError, match="invalid node name"):
            issue_node_cert(bad, fleet_dir)
        with pytest.raises(FleetCAError, match="invalid node name"):
            validate_node_name(bad)


def test_revoke_node_updates_crl_and_listing(fleet_dir: Path):
    issue_node_cert("gpu-01", fleet_dir)
    issue_node_cert("gpu-02", fleet_dir)
    res = revoke_node_cert("gpu-01", fleet_dir)
    assert res["revoked"] is True
    crl = Path(res["crl"])
    assert crl.is_file()
    by_name = {c["name"]: c for c in list_certs(fleet_dir)}
    assert by_name["gpu-01"]["status"] == "revoked"
    assert by_name["gpu-02"]["status"] == "valid"
    # Revoking an unknown node fails closed.
    with pytest.raises(FleetCAError, match="no certificate"):
        revoke_node_cert("gpu-99", fleet_dir)


def test_operations_require_ca(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(FleetCAError, match="no fleet CA"):
        issue_node_cert("gpu-01", empty)
