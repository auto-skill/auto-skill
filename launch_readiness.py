"""One-command launch readiness summary for Auto-Skill.

This is a thin orchestrator around the sharper checks:

  - local seed DB/library presence
  - alpha public host status
  - canonical production hostname status
  - dirty git working tree warning

It does not replace launch_check.py. Use it when someone asks "are we ready?"
and needs the blockers in one small report.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

import launch_status
from deploy.verify_backup import verify_backup_dir


@dataclass
class ReadinessCheck:
    name: str
    state: str
    detail: str


def _check_seed_db(db_path: Path, name: str = "local seed db") -> ReadinessCheck:
    if not db_path.exists():
        return ReadinessCheck(name, "fail", f"missing {db_path}")
    try:
        conn = sqlite3.connect(db_path)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        row = conn.execute(
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN COALESCE(quality_status, 'active') = 'active' THEN 1 ELSE 0 END) AS active,
              SUM(CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END) AS embedded
            FROM skills
            """
        ).fetchone()
    except Exception as exc:
        return ReadinessCheck(name, "fail", f"could not read {db_path}: {exc}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    total, active, embedded = [int(value or 0) for value in row]
    detail = f"path={db_path}, integrity={integrity}, total={total}, active={active}, embedded={embedded}"
    if integrity != "ok":
        return ReadinessCheck(name, "fail", detail)
    if total == 0 or active == 0 or embedded == 0:
        return ReadinessCheck(name, "fail", detail)
    return ReadinessCheck(name, "pass", detail)


def _check_seed_library(library_dir: Path, name: str = "local skills library") -> ReadinessCheck:
    index_path = library_dir / "index.json"
    files_dir = library_dir / "files"
    if not library_dir.exists():
        return ReadinessCheck(name, "fail", f"missing {library_dir}")
    if not index_path.exists():
        return ReadinessCheck(name, "fail", f"missing {index_path}")
    try:
        index = json.loads(index_path.read_text(encoding="utf-8-sig", errors="replace"))
    except Exception as exc:
        return ReadinessCheck(name, "fail", f"could not parse {index_path}: {exc}")
    if not isinstance(index, list):
        return ReadinessCheck(name, "fail", f"{index_path} must be a JSON array")
    markdown_count = len(list(files_dir.glob("*.md"))) if files_dir.exists() else 0
    detail = f"path={library_dir}, entries={len(index)}, markdown_files={markdown_count}"
    if len(index) == 0 or markdown_count == 0:
        return ReadinessCheck(name, "fail", detail)
    return ReadinessCheck(name, "pass", detail)


def _check_library_archive(archive_path: Path) -> ReadinessCheck:
    if not archive_path.exists():
        return ReadinessCheck("candidate backup library", "fail", f"missing {archive_path}")
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            names = [member.name.replace("\\", "/").lstrip("./") for member in archive.getmembers()]
    except Exception as exc:
        return ReadinessCheck("candidate backup library", "fail", f"could not read {archive_path}: {exc}")
    has_index = any(name == "index.json" or name.endswith("/index.json") for name in names)
    markdown_count = sum(1 for name in names if name.endswith(".md") and (name.startswith("files/") or "/files/" in name))
    detail = f"path={archive_path}, members={len(names)}, has_index={has_index}, markdown_files={markdown_count}"
    if not has_index or markdown_count == 0:
        return ReadinessCheck("candidate backup library", "fail", detail)
    return ReadinessCheck("candidate backup library", "pass", detail)


def _extract_backup_zip(zip_path: Path, target: Path) -> Path:
    if not zip_path.exists():
        raise FileNotFoundError(f"missing {zip_path}")
    with zipfile.ZipFile(zip_path) as archive:
        target_root = target.resolve()
        for info in archive.infolist():
            destination = (target_root / info.filename).resolve()
            try:
                destination.relative_to(target_root)
            except ValueError:
                raise RuntimeError(f"refusing unsafe zip member: {info.filename}")
        archive.extractall(target)
    manifest = target / "manifest.json"
    if manifest.exists():
        return target
    nested = list(target.glob("*/manifest.json"))
    if len(nested) == 1:
        return nested[0].parent
    raise RuntimeError(f"backup zip did not contain manifest.json: {zip_path}")


def check_local_seed(repo_root: Path) -> list[ReadinessCheck]:
    return [
        _check_seed_db(repo_root / "data" / "local_skills.db"),
        _check_seed_library(repo_root / "skills_library"),
    ]


def check_loose_seed(db_path: Path, library_dir: Path) -> list[ReadinessCheck]:
    return [
        _check_seed_db(db_path, "candidate seed db"),
        _check_seed_library(library_dir, "candidate skills library"),
    ]


def check_backup_seed(backup_dir: Path) -> list[ReadinessCheck]:
    checks = verify_backup_dir(backup_dir)
    failures = [check for check in checks if check.level == "FAIL"]
    if failures:
        detail = "; ".join(f"{check.name}: {check.detail}" for check in failures[:3])
        return [ReadinessCheck("candidate backup", "fail", detail)]
    warnings = [check for check in checks if check.level == "WARN"]
    backup_dir = backup_dir.resolve()
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8-sig", errors="replace"))
    db_rel = str(manifest.get("db_backup") or "local_skills.db")
    library_rel = str(manifest.get("library_archive") or "skills_library.tgz")
    result = [
        ReadinessCheck(
            "candidate backup",
            "warn" if warnings else "pass",
            f"verified {backup_dir}" + (f"; warnings={len(warnings)}" if warnings else ""),
        ),
        _check_seed_db(backup_dir / db_rel, "candidate backup db"),
        _check_library_archive(backup_dir / library_rel),
    ]
    return result


def check_backup_zip_seed(backup_zip: Path) -> list[ReadinessCheck]:
    try:
        with tempfile.TemporaryDirectory(prefix="autoskill-readiness-zip-") as tmp:
            backup_dir = _extract_backup_zip(backup_zip.resolve(), Path(tmp) / "backup")
            checks = check_backup_seed(backup_dir)
            checks.insert(0, ReadinessCheck("candidate backup zip", "pass", f"extracted {backup_zip}"))
            return checks
    except Exception as exc:
        return [ReadinessCheck("candidate backup zip", "fail", str(exc))]


def check_git_state(repo_root: Path) -> ReadinessCheck:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        return ReadinessCheck("git state", "warn", f"could not inspect git state: {exc}")
    if result.returncode != 0:
        return ReadinessCheck("git state", "warn", result.stderr.strip() or "git status failed")
    changed = [line for line in result.stdout.splitlines() if line.strip()]
    if changed:
        return ReadinessCheck("git state", "warn", f"{len(changed)} changed/untracked file(s); commit before launch")
    return ReadinessCheck("git state", "pass", "working tree clean")


def check_public_profile(profile: str) -> ReadinessCheck:
    base_url, mcp_health_url = launch_status.PROFILES[profile]
    probes = launch_status.run_status(base_url, mcp_health_url)
    action = launch_status.next_action(probes)
    states = ",".join(f"{item.name}={item.state}" for item in probes)
    if all(item.state == "ok" for item in probes):
        return ReadinessCheck(f"public {profile}", "pass", states)
    return ReadinessCheck(f"public {profile}", "fail", f"{states}; {action}")


def recommended_actions(checks: list[ReadinessCheck]) -> list[str]:
    names = {check.name: check for check in checks}
    details = " ".join(check.detail.lower() for check in checks)
    actions: list[str] = []

    if (
        names.get("local seed db", ReadinessCheck("", "", "")).state == "fail"
        or names.get("local skills library", ReadinessCheck("", "", "")).state == "fail"
        or "runtime db/library is empty" in details
    ):
        actions.append(
            "On the populated machine, run: "
            r".\deploy\export-seed-packet.ps1 -DbPath C:\path\to\local_skills.db "
            r"-LibraryDir C:\path\to\skills_library"
        )
        actions.append(
            "On the host, run: "
            r".\deploy\recover-host.ps1 -SeedBackupZip C:\path\to\seed-packet.zip -ForceSeedRuntime"
        )

    if "does not resolve" in details or "getaddrinfo failed" in details:
        actions.append(
            "In Cloudflare/DNS, create or fix api.auto-skill.com and mcp.auto-skill.com, "
            "then rerun: python launch_status.py --profile canonical"
        )

    if names.get("git state", ReadinessCheck("", "", "")).state == "warn":
        actions.append("Before launch, commit the verified launch-readiness changes from a clean working tree.")

    unique: list[str] = []
    for action in actions:
        if action not in unique:
            unique.append(action)
    return unique


def build_report(
    repo_root: Path,
    skip_live: bool = False,
    skip_local_seed: bool = False,
    skip_git: bool = False,
    seed_backup_dir: Path | None = None,
    seed_backup_zip: Path | None = None,
    seed_db_path: Path | None = None,
    seed_library_dir: Path | None = None,
) -> list[ReadinessCheck]:
    checks = []
    if not skip_local_seed:
        checks.extend(check_local_seed(repo_root))
    if seed_backup_dir:
        checks.extend(check_backup_seed(seed_backup_dir))
    if seed_backup_zip:
        checks.extend(check_backup_zip_seed(seed_backup_zip))
    if seed_db_path or seed_library_dir:
        if seed_db_path and seed_library_dir:
            checks.extend(check_loose_seed(seed_db_path, seed_library_dir))
        else:
            checks.append(ReadinessCheck("candidate loose seed", "fail", "provide both --seed-db-path and --seed-library-dir"))
    if not skip_live:
        checks.append(check_public_profile("alpha"))
        checks.append(check_public_profile("canonical"))
    if not skip_git:
        checks.append(check_git_state(repo_root))
    return checks


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize Auto-Skill launch readiness blockers.")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--seed-backup-dir", type=Path, help="candidate manifest-backed backup to validate before host seeding")
    parser.add_argument("--seed-backup-zip", type=Path, help="candidate zip created by deploy/export-seed-packet.ps1")
    parser.add_argument("--seed-db-path", type=Path, help="candidate loose local_skills.db to validate before host seeding")
    parser.add_argument("--seed-library-dir", type=Path, help="candidate loose skills_library directory to validate before host seeding")
    parser.add_argument("--skip-local-seed", action="store_true", help="skip mounted repo DB/library checks")
    parser.add_argument("--skip-live", action="store_true", help="skip public alpha/canonical probes")
    parser.add_argument("--skip-git", action="store_true", help="skip git working tree cleanliness check")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    checks = build_report(
        args.repo_root.resolve(),
        skip_live=args.skip_live,
        skip_local_seed=args.skip_local_seed,
        skip_git=args.skip_git,
        seed_backup_dir=args.seed_backup_dir.resolve() if args.seed_backup_dir else None,
        seed_backup_zip=args.seed_backup_zip.resolve() if args.seed_backup_zip else None,
        seed_db_path=args.seed_db_path.resolve() if args.seed_db_path else None,
        seed_library_dir=args.seed_library_dir.resolve() if args.seed_library_dir else None,
    )
    if args.json:
        print(json.dumps([asdict(check) for check in checks], indent=2, sort_keys=True))
    else:
        for check in checks:
            print(f"[{check.state.upper()}] {check.name}: {check.detail}")
        actions = recommended_actions(checks)
        if actions:
            print("\nnext actions:")
            for index, action in enumerate(actions, start=1):
                print(f"{index}. {action}")
    return 0 if all(check.state == "pass" for check in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
