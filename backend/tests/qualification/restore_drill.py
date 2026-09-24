#!/usr/bin/env python3
"""
Backup -> clean-staging restore qualification drill for the sysadmin AI platform.

Re-runnable on the production host. Self-contained and non-destructive:
everything it writes (synthetic state, backup archives, restore targets,
negative-case archives) lives under a single mktemp root that is deleted on
exit unless --keep is given. It never starts or stops platform services and
never writes under backend/data, backend/config, backend/logs or backend/run.

Modes:
  (default)   Synthetic: builds a realistic fake platform state in the mktemp
              root (valkey dump.rdb + AOF dir, VictoriaLogs partitions +
              outbox.jsonl, SeaweedFS master/filer/volume data, config keys,
              runbooks, workspaces) and forces the snapshot endpoints to dead
              ports so the run is hermetic and exercises the documented
              filesystem-copy fallback. Restored trees are verified
              BYTE-FOR-BYTE against the pristine synthetic source.
  --real      Real state: snapshots the actual backend/data and backend/config/keys
              READ-ONLY via BackupManager (exactly the production code path,
              including live BGSAVE / /snapshot/create when reachable, and the
              filesystem fallback otherwise). Restored trees are verified
              byte-for-byte against the archive content and via the manifest
              SHA-256 chain. Key file names/contents are never printed.

Phases (both modes):
  A  Timed backup (wall clock, per-component sizes and snapshot modes).
  B  Timed clean-staging restore into an empty mktemp target; verifies the
     mandatory 7-stage sequence, manifest aggregate hashes, byte-for-byte
     fidelity, and the permission contract (0700 dirs / 0600 secret+state
     files / 0700 workspaces).
  C  The code's own DisasterRecoveryDrill (dr_drill.py) against clean staging,
     for comparison with the manual drill.
  D  Negative cases; every one must be rejected BEFORE anything is written to
     the restore target: missing component dir, missing manifest entry,
     tampered component content, '..' path-traversal member, symlink member,
     multi-root archive, truncated archive.

Output ends with an RTO_DATA line (sizes, wall times, throughput) and a small
linear extrapolation so an RTO statement for production-sized data can be
made. The restore only copies files; the inference/litellm/traefik stages are
no-ops in RestoreManager, so real service bring-up time is NOT measured here.

Usage:
  backend/.venv/bin/python3 backend/tests/qualification/restore_drill.py [--real] [--keep] [--scale N]

Exit: 0 = every check passed, 1 = at least one check failed, 2 = harness error.
"""
import argparse
import json
import os
import shutil
import stat
import sys
import tarfile
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.services.resilience import backup_manager as bm_mod
from backend.services.resilience.backup_manager import (
    BackupManager,
    compute_sha256,
    compute_dir_sha256,
)
from backend.services.resilience.restore_manager import (
    RestoreManager,
    MANDATORY_RESTORE_SEQUENCE,
)
from backend.services.resilience.dr_drill import DisasterRecoveryDrill

COMPONENTS = ("valkey", "victorialogs", "seaweedfs", "config_keys")
FAILURES = []


def report(ok: bool, name: str, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name:58s} {detail}")
    if not ok:
        FAILURES.append(name)


def dir_bytes(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def tree_hashes(root: Path):
    """{relative path: content hash} for every file/symlink/dir below root."""
    out = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_symlink():
            out[rel] = ("symlink", os.readlink(p))
        elif p.is_file():
            out[rel] = ("file", compute_sha256(p))
        elif p.is_dir():
            out[rel] = ("dir",)
    return out


def mode_of(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


# --------------------------------------------------------------------------
# Synthetic realistic platform state
# --------------------------------------------------------------------------
def build_synthetic_state(root: Path, scale: int = 1) -> None:
    """Create a fake but realistic backend/{data,config} tree under root."""
    import random
    import secrets as _sec

    rng = random.Random(20260924)  # deterministic content
    data = root / "data"
    cfg = root / "config"

    # --- valkey: dump.rdb with a valid signature + AOF dir ---
    vdir = data / "valkey"
    (vdir / "appendonlydir").mkdir(parents=True)
    payload = rng.randbytes(512 * 1024 * scale)
    (vdir / "dump.rdb").write_bytes(b"REDIS0011" + payload + b"\xff")
    (vdir / "appendonlydir" / "manifest").write_text(
        "file appendonly.aof.1.base.rdb seq 1 type b\n"
        "file appendonly.aof.1.incr.aof seq 1 type i\n")
    aof = b"".join(
        f"*3\r\n$3\r\nSET\r\n$8\r\nquota:u{i}\r\n$4\r\n{1000 + i}\r\n".encode()
        for i in range(200 * scale))
    (vdir / "appendonlydir" / "appendonly.aof.1.incr.aof").write_bytes(aof)
    (vdir / "appendonlydir" / "appendonly.aof.1.base.rdb").write_bytes(
        b"REDIS0011" + rng.randbytes(64 * 1024))

    # --- victorialogs: partitions + outbox.jsonl ---
    vlog = data / "victorialogs"
    for part in range(2 * scale):
        pid = f"18D837B4BDAC{part:04X}"
        ddb = vlog / "partitions" / "20260924" / "datadb" / pid
        idb = vlog / "partitions" / "20260924" / "indexdb" / pid
        ddb.mkdir(parents=True, exist_ok=True)
        idb.mkdir(parents=True, exist_ok=True)
        for name in ("bloom.bin0", "values.bin0", "index.bin", "timestamps.bin",
                     "columns_header.bin", "metaindex.bin", "message_bloom.bin"):
            (ddb / name).write_bytes(rng.randbytes(16 * 1024))
        (ddb / "metadata.json").write_text(json.dumps({"rows": 1234, "blocks": 12}))
        for name in ("index.bin", "items.bin", "lens.bin", "metaindex.bin"):
            (idb / name).write_bytes(rng.randbytes(8 * 1024))
        (idb / "metadata.json").write_text(json.dumps({"items": 5678}))
    with open(vlog / "outbox.jsonl", "w", encoding="utf-8") as f:
        for i in range(200 * scale):
            f.write(json.dumps({
                "timestamp": f"2026-09-24T09:{i % 60:02d}:00Z", "service": "dsh-agent",
                "user_id": f"sysadmin-{(i % 2) + 1:02d}", "session_id": f"sess-{i:04d}",
                "tool_name": "execute_scoped_command", "command": f"systemctl status svc{i}",
                "human_approved": i % 3 == 0, "exit_code": 0, "duration_ms": 20 + i,
                "tokens_prompt": 100 + i, "tokens_completion": 40,
            }) + "\n")

    # --- seaweedfs: master dir, filer leveldb, volume data ---
    sw = data / "seaweedfs"
    (sw / "m9333").mkdir(parents=True)
    (sw / "m9333" / "raft.log").write_bytes(rng.randbytes(32 * 1024))
    (sw / "vol_dir.uuid").write_text("b7f3c1e0-4a52-4f2e-9c2d-2d6f1a9e44aa\n")
    filer = sw / "filerldb2"
    filer.mkdir()
    for name, size in (("MANIFEST-000001", 1024), ("000002.ldb", 64 * 1024 * scale),
                       ("000003.log", 16 * 1024), ("CURRENT", 16), ("LOCK", 0), ("LOG", 4096)):
        (filer / name).write_bytes(rng.randbytes(size))
    (sw / "vol_1.dat").write_bytes(rng.randbytes(256 * 1024 * scale))
    (sw / "vol_1.idx").write_bytes(rng.randbytes(32 * 1024))
    (sw / "vol_1.vif").write_bytes(rng.randbytes(256))

    # --- config keys (synthetic random secrets, 0600) ---
    keys = cfg / "keys"
    keys.mkdir(parents=True)
    os.chmod(keys, 0o700)
    for name in ("master.key", "valkey-password", "jwt-secret.key",
                 "sysadmin-01.key", "sysadmin-02.key", "auditor-01.key"):
        kf = keys / name
        kf.write_text(_sec.token_hex(24) + "\n")
        os.chmod(kf, 0o600)

    # --- state the backup deliberately does NOT cover (coverage-gap probes) ---
    ws = data / "workspaces" / "ws-sysadmin-01"
    ws.mkdir(parents=True)
    (ws / "notes.txt").write_text("scratch data that backup must NOT contain\n")
    rb = data / "runbooks"
    rb.mkdir()
    (rb / "disaster_recovery_plan.md").write_text(
        "# DR plan\nPhase 2: Service Restoration Sequence\n")


# --------------------------------------------------------------------------
# Drill phases
# --------------------------------------------------------------------------
def phase_backup(mgr: BackupManager, root: Path):
    print("\n=== PHASE A: BACKUP ===")
    t0 = time.monotonic()
    manifest = mgr.create_backup()
    wall = time.monotonic() - t0
    archive = Path(manifest["archive_path"])
    print(f"backup_id={manifest['backup_id']}  created_at={manifest['created_at']}")
    for comp in COMPONENTS:
        meta = manifest["components"][comp]
        n = len(meta["files"])
        names = ", ".join(f["name"] for f in meta["files"]) if comp != "config_keys" else "(names redacted)"
        print(f"  {comp:14s} snapshot_mode={meta['mode']:16s} entries={n:3d} {names}")
    print(f"  archive_bytes={manifest['total_bytes']}  archive_sha256={manifest['archive_sha256'][:24]}...")
    print(f"  BACKUP_WALL_SECONDS={wall:.3f}")
    report(archive.exists() and compute_sha256(archive) == manifest["archive_sha256"],
           "archive exists; recorded archive_sha256 matches file")

    # Independent extraction for verification
    extract = root / "verify-extract"
    extract.mkdir()
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(extract)
    ex_root = next(p for p in extract.iterdir() if p.is_dir())
    comp_bytes = {}
    for comp in COMPONENTS:
        comp_bytes[comp] = dir_bytes(ex_root / comp)
        report(compute_dir_sha256(ex_root / comp) == manifest["components"][comp]["aggregate_sha256"],
               f"manifest aggregate hash == extracted tree [{comp}]")
    print(f"  component_bytes={json.dumps(comp_bytes)}  total_uncompressed={sum(comp_bytes.values())}")
    return manifest, archive, ex_root, comp_bytes, wall


def phase_restore(archive: Path, ex_root: Path, root: Path, reference_root):
    """reference_root: pristine synthetic source tree (synthetic mode) or None
    (real mode; the archive extraction is then the byte-for-byte reference)."""
    print("\n=== PHASE B: CLEAN-STAGING RESTORE ===")
    target = root / "restore-target"
    rm = RestoreManager(data_dir=root / "must-not-be-used-data",
                        config_dir=root / "must-not-be-used-config")
    t0 = time.monotonic()
    result = rm.restore_from_archive(archive_path=archive, target_staging_base=target / "data")
    wall = time.monotonic() - t0
    print(f"  restore_sequence={result['restore_sequence']}")
    print(f"  components_restored={sorted(result['components_restored'])}")
    print(f"  internal restore_duration_seconds={result['restore_duration_seconds']:.3f}")
    print(f"  RESTORE_WALL_SECONDS={wall:.3f}")
    report(result["restore_sequence"] == MANDATORY_RESTORE_SEQUENCE,
           "restore sequence == mandatory 7-stage order")

    if reference_root is not None:
        ref = {
            "valkey": reference_root / "data" / "valkey",
            "victorialogs": reference_root / "data" / "victorialogs",
            "seaweedfs": reference_root / "data" / "seaweedfs",
            "config_keys": reference_root / "config" / "keys",
        }
        label = "pristine synthetic source"
    else:
        ref = {
            "valkey": ex_root / "valkey",
            "victorialogs": ex_root / "victorialogs",
            "seaweedfs": ex_root / "seaweedfs",
            "config_keys": ex_root / "config_keys",
        }
        label = "archive content"
    dst = {
        "valkey": target / "data" / "valkey",
        "victorialogs": target / "data" / "victorialogs",
        "seaweedfs": target / "data" / "seaweedfs",
        "config_keys": target / "config" / "keys",
    }
    for comp in COMPONENTS:
        same = tree_hashes(ref[comp]) == tree_hashes(dst[comp])
        report(same, f"restored tree == {label} byte-for-byte [{comp}]",
               f"{len(tree_hashes(dst[comp]))} paths compared")

    # Permission contract as implemented by RestoreManager:
    # component roots 0700; direct file children of valkey/victorialogs/keys 0600;
    # direct subdirs 0700; workspaces 0700. Nested content keeps source modes.
    violations = []
    if mode_of(target / "data" / "workspaces") != 0o700:
        violations.append("workspaces root")
    for cdir in ("data/valkey", "data/victorialogs", "data/seaweedfs", "config/keys"):
        if mode_of(target / cdir) != 0o700:
            violations.append(f"{cdir} root is {oct(mode_of(target / cdir))}")
    for cdir in ("data/valkey", "data/victorialogs", "config/keys"):
        for item in (target / cdir).iterdir():
            if item.is_file() and mode_of(item) != 0o600:
                violations.append(f"{cdir}/{item.name} is {oct(mode_of(item))}")
            if item.is_dir() and mode_of(item) != 0o700:
                violations.append(f"{cdir}/{item.name}/ is {oct(mode_of(item))}")
    for item in (target / "data" / "seaweedfs").iterdir():
        if item.is_dir() and mode_of(item) != 0o700:
            violations.append(f"seaweedfs/{item.name}/ is {oct(mode_of(item))}")
    report(not violations,
           "permission contract: roots 0700, secret/state files 0600, workspaces 0700",
           "; ".join(violations[:5]))
    nested = sum(1 for p in (target / "data").rglob("*") if p.is_file() and mode_of(p) != 0o600)
    print(f"  INFO: {nested} nested files keep source modes below 0700 parents (implementation behavior)")

    report((target / "data" / "workspaces").is_dir() and not any((target / "data" / "workspaces").iterdir()),
           "workspaces recreated empty 0700 (workspace CONTENT is not backup-covered)")
    report(not (target / "data" / "runbooks").exists(),
           "runbooks not restored (not backup-covered; tracked in git)")
    return result, wall, target


def phase_builtin_drill(mgr: BackupManager, root: Path, manual_restore_wall: float):
    print("\n=== PHASE C: BUILT-IN DisasterRecoveryDrill (dr_drill.py) ===")
    drill = DisasterRecoveryDrill(
        backup_mgr=mgr,
        restore_mgr=RestoreManager(data_dir=root / "drill-unused-data",
                                   config_dir=root / "drill-unused-config"),
    )
    rep = drill.run_drill(target_staging_dir=root / "builtin-drill-target")
    print(json.dumps(rep, indent=2, default=str))
    report(rep.get("success") is True, "built-in drill reports success")
    report(rep["rpo"]["passed"] is True, "built-in drill RPO (<24h) passed",
           f"snapshot age {rep['rpo']['actual_seconds']:.2f}s")
    report(rep["rto"]["passed"] is True, "built-in drill RTO (<4h) passed",
           f"restore {rep['rto']['actual_seconds']:.3f}s")
    report(rep["sequence_verification"]["passed"] is True,
           "built-in drill sequence verification passed")
    for name, hc in rep["health_checks"].items():
        report(hc.get("status") in ("healthy", "unverified"), f"built-in drill health [{name}]",
               json.dumps(hc))
    print(f"  COMPARISON: manual restore wall={manual_restore_wall:.3f}s "
          f"vs built-in drill rto.actual={rep['rto']['actual_seconds']:.3f}s (same payload class)")
    return rep


def phase_negative(archive: Path, ex_root: Path, root: Path):
    print("\n=== PHASE D: NEGATIVE CASES (rejection must happen BEFORE target writes) ===")

    def repack(src_root: Path, dest: Path, arcname: str):
        with tarfile.open(dest, "w:gz") as tar:
            tar.add(src_root, arcname=arcname)

    def expect_reject(label: str, bad_archive: Path, needle: str, neg_target: Path):
        rm = RestoreManager(data_dir=root / "neg-unused-data", config_dir=root / "neg-unused-config")
        try:
            rm.restore_from_archive(archive_path=bad_archive, target_staging_base=neg_target / "data")
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            written = list(neg_target.rglob("*")) if neg_target.exists() else []
            report(needle in msg and not written, label,
                   f"[{msg[:88]}] target_paths_written={len(written)}")
            return
        report(False, label, "NO EXCEPTION RAISED")

    # D1: component directory removed, manifest still lists it
    t1 = root / "neg1-tree"
    shutil.copytree(ex_root, t1)
    shutil.rmtree(t1 / "seaweedfs")
    a1 = root / "neg1-missing-component-dir.tar.gz"
    repack(t1, a1, ex_root.name)
    expect_reject("missing component dir rejected pre-write", a1,
                  "Backup component missing: seaweedfs", root / "neg1-target")

    # D2: component dropped from the manifest itself
    t2 = root / "neg2-tree"
    shutil.copytree(ex_root, t2)
    mfile = t2 / "manifest.json"
    mdata = json.loads(mfile.read_text())
    del mdata["components"]["seaweedfs"]
    mfile.write_text(json.dumps(mdata, indent=2))
    a2 = root / "neg2-missing-manifest-entry.tar.gz"
    repack(t2, a2, ex_root.name)
    expect_reject("missing manifest entry rejected pre-write", a2,
                  "missing required components", root / "neg2-target")

    # D3: tampered component content (manifest hash no longer matches)
    t3 = root / "neg3-tree"
    shutil.copytree(ex_root, t3)
    victim = t3 / "victorialogs" / "outbox.jsonl"
    comp = "victorialogs"
    if not victim.exists():
        victim = next(p for p in (t3 / "valkey").rglob("*") if p.is_file())
        comp = "valkey"
    with open(victim, "ab") as f:
        f.write(b"TAMPERED")
    a3 = root / "neg3-tampered-content.tar.gz"
    repack(t3, a3, ex_root.name)
    expect_reject("tampered component content rejected pre-write", a3,
                  f"Integrity violation on component '{comp}'", root / "neg3-target")

    # D4: '..' path-traversal member
    payload = root / "payload.bin"
    payload.write_bytes(b"x")
    a4 = root / "neg4-traversal.tar.gz"
    with tarfile.open(a4, "w:gz") as tar:
        tar.add(payload, arcname=f"{ex_root.name}/../../evil.sh")
    expect_reject("'..' traversal member rejected pre-write", a4,
                  "Unsafe backup archive member", root / "neg4-target")

    # D5: symlink member (only plain files/dirs are allowed)
    a5 = root / "neg5-symlink.tar.gz"
    link_src = root / "link-target"
    link_src.write_text("y")
    link = root / "the-link"
    os.symlink(link_src, link)
    with tarfile.open(a5, "w:gz") as tar:
        tar.add(ex_root, arcname=ex_root.name)
        tar.add(link, arcname=f"{ex_root.name}/valkey/evil-link")
    expect_reject("symlink member rejected pre-write", a5,
                  "Unsafe backup archive member", root / "neg5-target")

    # D6: multiple roots
    a6 = root / "neg6-multiroot.tar.gz"
    with tarfile.open(a6, "w:gz") as tar:
        tar.add(payload, arcname="rootA/file.txt")
        tar.add(payload, arcname="rootB/file.txt")
    expect_reject("multi-root archive rejected pre-write", a6,
                  "one root directory", root / "neg6-target")

    # D7: truncated archive
    a7 = root / "neg7-truncated.tar.gz"
    a7.write_bytes(archive.read_bytes()[: max(100, archive.stat().st_size // 3)])
    expect_reject("truncated archive rejected pre-write", a7, "", root / "neg7-target")


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Backup/restore clean-staging qualification drill")
    ap.add_argument("--real", action="store_true",
                    help="snapshot the real backend/data + backend/config/keys (read-only) "
                         "instead of synthetic state")
    ap.add_argument("--keep", action="store_true", help="keep the mktemp drill root for inspection")
    ap.add_argument("--scale", type=int, default=1,
                    help="multiply synthetic payload size (default 1)")
    args = ap.parse_args()

    drill_root = Path(tempfile.mkdtemp(prefix="restore-drill-"))
    os.chmod(drill_root, 0o700)
    print(f"restore_drill: mode={'REAL host state' if args.real else 'SYNTHETIC (hermetic)'}  "
          f"drill_root={drill_root} (mode 700){'  [KEPT]' if args.keep else ''}")
    reference_root = None
    try:
        if args.real:
            # Production code path against real state; writes stay in drill_root.
            mgr = BackupManager(backup_dir=drill_root / "backups")
            print("real mode: reading backend/data + backend/config/keys read-only; "
                  "snapshot mode per component depends on live service reachability")
        else:
            state_root = drill_root / "synthetic-state"
            build_synthetic_state(state_root, scale=max(1, args.scale))
            reference_root = state_root
            # Redirect the module-level source dirs and force dead endpoints so the
            # run is hermetic and exercises the documented filesystem fallback.
            bm_mod.DATA_DIR = state_root / "data"
            bm_mod.CONFIG_DIR = state_root / "config"
            mgr = BackupManager(backup_dir=drill_root / "backups",
                                valkey_url="redis://127.0.0.1:1/0",
                                victorialogs_url="http://127.0.0.1:1")
            sb = dir_bytes(state_root / "data") + dir_bytes(state_root / "config")
            print(f"synthetic state built: {sb} bytes under {state_root}")

        manifest, archive, ex_root, comp_bytes, backup_wall = phase_backup(mgr, drill_root)
        result, restore_wall, target = phase_restore(archive, ex_root, drill_root, reference_root)
        phase_builtin_drill(mgr, drill_root, restore_wall)
        phase_negative(archive, ex_root, drill_root)

        total = sum(comp_bytes.values())
        n_paths = sum(len(tree_hashes(ex_root / c)) for c in COMPONENTS)
        mbps = total / 1e6 / max(restore_wall, 1e-9)
        pps = n_paths / max(restore_wall, 1e-9)
        print("\n=== RTO DATA ===")
        print(f"RTO_DATA: uncompressed_bytes={total} archive_bytes={manifest['total_bytes']} "
              f"paths={n_paths} backup_wall={backup_wall:.3f}s restore_wall={restore_wall:.3f}s "
              f"restore_throughput={mbps:.1f}MB/s {pps:.0f}paths/s")
        print("linear extrapolation of the file-copy phase (service bring-up NOT included):")
        for factor in (100, 1000):
            gib = total * factor / 2**30
            est = restore_wall * factor
            verdict = "<< 4h RTO" if est < 14400 else "EXCEEDS 4h RTO"
            print(f"  x{factor:<5d} (~{gib:6.2f} GiB uncompressed): ~{est:8.1f}s ({verdict})")

        print(f"\n=== DRILL SUMMARY: {'ALL CHECKS PASSED' if not FAILURES else f'{len(FAILURES)} FAILURES: {FAILURES}'} ===")
        return 0 if not FAILURES else 1
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"FATAL: {type(e).__name__}: {e}")
        return 2
    finally:
        bm_mod.DATA_DIR = REPO_ROOT / "backend" / "data"
        bm_mod.CONFIG_DIR = REPO_ROOT / "backend" / "config"
        if args.keep:
            print(f"drill root kept at {drill_root}")
            if args.real:
                print("WARNING: kept artifacts include backup archives containing copies of "
                      "config/keys; handle and delete them as secret material.")
        else:
            shutil.rmtree(drill_root, ignore_errors=True)
            print(f"cleanup: drill root removed: {not drill_root.exists()}")


if __name__ == "__main__":
    sys.exit(main())
