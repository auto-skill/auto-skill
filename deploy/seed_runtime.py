from __future__ import annotations

import argparse
import json
import shutil
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deploy import compose_preflight  # noqa: E402
from deploy.verify_backup import verify_backup_dir  # noqa: E402


def _fail(message: str) -> int:
    print(f"[FAIL] {message}", file=sys.stderr)
    return 1


def _print_checks(checks: list[compose_preflight.Check]) -> bool:
    compose_preflight.print_checks(checks)
    return not any(check.level == "FAIL" for check in checks)


def validate_seed(db_path: Path, library_dir: Path, args: argparse.Namespace) -> bool:
    checks: list[compose_preflight.Check] = []
    compose_preflight.check_seed_db(db_path, checks, args.min_total, args.min_active, args.min_embedded)
    compose_preflight.check_seed_library(library_dir, checks, args.min_library_entries)
    return _print_checks(checks)


def ensure_replaceable(path: Path, force: bool) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists; pass --force to replace it")


def replace_db(source: Path, target: Path, force: bool) -> None:
    ensure_replaceable(target, force)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    shutil.copy2(source, target)


def replace_library(source: Path, target: Path, force: bool) -> None:
    ensure_replaceable(target, force)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)


def extract_library_archive(archive_path: Path, target: Path, force: bool) -> None:
    ensure_replaceable(target, force)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:gz") as archive:
        target_root = target.resolve()
        for member in archive.getmembers():
            destination = (target_root / member.name).resolve()
            try:
                destination.relative_to(target_root)
            except ValueError:
                raise RuntimeError(f"refusing unsafe archive member: {member.name}")
        archive.extractall(target)


def extract_backup_zip(zip_path: Path, target: Path) -> Path:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        target_root = target.resolve()
        for info in archive.infolist():
            destination = (target_root / info.filename).resolve()
            try:
                destination.relative_to(target_root)
            except ValueError:
                raise RuntimeError(f"refusing unsafe zip member: {info.filename}")
        archive.extractall(target)

    manifest_candidates = list(target.glob("manifest.json"))
    if not manifest_candidates:
        nested = list(target.glob("*/manifest.json"))
        if len(nested) == 1:
            return nested[0].parent
        raise RuntimeError(f"backup zip did not contain manifest.json: {zip_path}")
    return manifest_candidates[0].parent


def seed_from_backup(backup_dir: Path, target_data_dir: Path, target_library_dir: Path, force: bool) -> None:
    checks = verify_backup_dir(backup_dir)
    if not _print_checks(checks):
        raise RuntimeError(f"backup verification failed: {backup_dir}")

    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8-sig"))
    db_rel = str(manifest.get("db_backup") or "local_skills.db")
    library_rel = str(manifest.get("library_archive") or "skills_library.tgz")
    replace_db(backup_dir / db_rel, target_data_dir / "local_skills.db", force)
    extract_library_archive(backup_dir / library_rel, target_library_dir, force)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed Auto-Skill runtime data for compose/host launch.")
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--db-path", type=Path, help="source local_skills.db")
    parser.add_argument("--library-dir", type=Path, help="source skills_library directory")
    parser.add_argument("--backup-dir", type=Path, help="verified backup directory containing manifest.json")
    parser.add_argument("--backup-zip", type=Path, help="zip created by deploy/export-seed-packet.ps1")
    parser.add_argument("--target-data-dir", type=Path, help="target data directory; defaults to <repo-root>/data")
    parser.add_argument("--target-library-dir", type=Path, help="target skills_library directory; defaults to <repo-root>/skills_library")
    parser.add_argument("--force", action="store_true", help="replace existing target DB/library")
    parser.add_argument("--min-total", type=int, default=1)
    parser.add_argument("--min-active", type=int, default=1)
    parser.add_argument("--min-embedded", type=int, default=1)
    parser.add_argument("--min-library-entries", type=int, default=1)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.repo_root.resolve()
    target_data_dir = (args.target_data_dir or root / "data").resolve()
    target_library_dir = (args.target_library_dir or root / "skills_library").resolve()

    seed_modes = sum(
        [
            bool(args.backup_dir),
            bool(args.backup_zip),
            bool(args.db_path or args.library_dir),
        ]
    )
    if seed_modes > 1:
        return _fail("use exactly one seed mode: --backup-dir, --backup-zip, or --db-path/--library-dir")
    if seed_modes == 0:
        return _fail("provide --backup-dir, --backup-zip, or both --db-path and --library-dir")
    if (args.db_path or args.library_dir) and not (args.db_path and args.library_dir):
        return _fail("provide both --db-path and --library-dir")

    try:
        if args.backup_dir:
            seed_from_backup(args.backup_dir.resolve(), target_data_dir, target_library_dir, args.force)
        elif args.backup_zip:
            with tempfile.TemporaryDirectory(prefix="autoskill-seed-zip-") as tmp:
                backup_dir = extract_backup_zip(args.backup_zip.resolve(), Path(tmp) / "backup")
                seed_from_backup(backup_dir, target_data_dir, target_library_dir, args.force)
        else:
            source_db = args.db_path.resolve()
            source_library = args.library_dir.resolve()
            if not validate_seed(source_db, source_library, args):
                return 1
            replace_db(source_db, target_data_dir / "local_skills.db", args.force)
            replace_library(source_library, target_library_dir, args.force)

        print(f"[PASS] copied DB to {target_data_dir / 'local_skills.db'}")
        print(f"[PASS] copied library to {target_library_dir}")
        print("[INFO] validating seeded runtime")
        return 0 if validate_seed(target_data_dir / "local_skills.db", target_library_dir, args) else 1
    except Exception as exc:
        return _fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
