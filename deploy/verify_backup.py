from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import tarfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Check:
    level: str
    name: str
    detail: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _add(checks: list[Check], level: str, name: str, detail: str) -> None:
    checks.append(Check(level, name, detail))


def _check_sqlite(path: Path, checks: list[Check]) -> None:
    conn = None
    try:
        conn = sqlite3.connect(path)
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    except Exception as exc:
        _add(checks, "FAIL", "sqlite integrity", str(exc))
        return
    finally:
        if conn is not None:
            conn.close()
    if result == "ok":
        _add(checks, "PASS", "sqlite integrity", "ok")
    else:
        _add(checks, "FAIL", "sqlite integrity", str(result)[:400])


def _check_tar(path: Path, checks: list[Check]) -> None:
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
    except Exception as exc:
        _add(checks, "FAIL", "library archive", str(exc))
        return
    if members:
        _add(checks, "PASS", "library archive", f"{len(members)} member(s)")
    else:
        _add(checks, "WARN", "library archive", "archive is readable but empty")


def _check_content_blobs(backup_dir: Path, checks: list[Check]) -> None:
    manifest_path = backup_dir / "content_blobs" / "manifest.json"
    if not manifest_path.exists():
        _add(checks, "FAIL", "content blobs", f"missing {manifest_path}")
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig", errors="replace"))
    except Exception as exc:
        _add(checks, "FAIL", "content blobs", f"could not parse manifest: {exc}")
        return
    blobs = manifest.get("blobs") if isinstance(manifest.get("blobs"), dict) else {}
    missing = []
    for hash_value, blob in blobs.items():
        relpath = blob.get("path") if isinstance(blob, dict) else ""
        if not relpath or not (backup_dir / "content_blobs" / relpath).exists():
            missing.append(hash_value)
    if missing:
        _add(checks, "FAIL", "content blobs", f"missing {len(missing)} blob file(s)")
    else:
        _add(
            checks,
            "PASS",
            "content blobs",
            f"unique_blobs={int(manifest.get('unique_blobs') or len(blobs))}, entries={int(manifest.get('entries') or 0)}",
        )


def verify_backup_dir(backup_dir: Path) -> list[Check]:
    backup_dir = backup_dir.resolve()
    checks: list[Check] = []
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.exists():
        return [Check("FAIL", "manifest", f"missing {manifest_path}")]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig", errors="replace"))
    except Exception as exc:
        return [Check("FAIL", "manifest", f"could not parse {manifest_path}: {exc}")]
    _add(checks, "PASS", "manifest", str(manifest_path))

    files = manifest.get("files") if isinstance(manifest.get("files"), list) else []
    if not files:
        _add(checks, "FAIL", "files", "manifest did not list backup files")
    for entry in files:
        if not isinstance(entry, dict):
            _add(checks, "FAIL", "files", f"invalid manifest file entry: {entry!r}")
            continue
        relpath = str(entry.get("path") or "")
        if not relpath:
            _add(checks, "FAIL", "files", "file entry missing path")
            continue
        path = backup_dir / relpath
        if not path.exists():
            _add(checks, "FAIL", f"file {relpath}", "missing")
            continue
        expected_bytes = entry.get("bytes")
        if expected_bytes is not None and path.stat().st_size != int(expected_bytes):
            _add(checks, "FAIL", f"file {relpath}", f"bytes={path.stat().st_size}, expected={expected_bytes}")
            continue
        expected_hash = str(entry.get("sha256") or "").lower()
        if expected_hash:
            actual_hash = _sha256(path)
            if actual_hash != expected_hash:
                _add(checks, "FAIL", f"file {relpath}", f"sha256={actual_hash}, expected={expected_hash}")
                continue
        _add(checks, "PASS", f"file {relpath}", f"bytes={path.stat().st_size}")

    db_relpath = str(manifest.get("db_backup") or "local_skills.db")
    db_path = backup_dir / db_relpath
    if db_path.exists():
        _check_sqlite(db_path, checks)
    else:
        _add(checks, "FAIL", "sqlite integrity", f"missing {db_path}")

    archive_relpath = manifest.get("library_archive")
    if archive_relpath:
        archive_path = backup_dir / str(archive_relpath)
        if archive_path.exists():
            _check_tar(archive_path, checks)
        else:
            _add(checks, "FAIL", "library archive", f"missing {archive_path}")

    if manifest.get("content_blobs_packed") is True:
        _check_content_blobs(backup_dir, checks)

    return checks


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify an Auto-Skill local/R2 backup directory.")
    parser.add_argument("backup_dir", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    checks = verify_backup_dir(args.backup_dir)
    for check in checks:
        print(f"[{check.level}] {check.name}: {check.detail}")
    failures = [check for check in checks if check.level == "FAIL"]
    warnings = [check for check in checks if check.level == "WARN"]
    print(f"\nverify_backup: {len(failures)} failure(s), {len(warnings)} warning(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
