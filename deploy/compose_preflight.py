from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


REQUIRED_ENV = {
    "GITHUB_TOKEN": "required so the scraper does not run on the 60/hr unauthenticated GitHub budget",
    "CLOUDFLARED_TOKEN": "required for the public Cloudflare Tunnel service",
    "R2_ENDPOINT": "required for Litestream and skills_library backups",
    "R2_BUCKET": "required for Litestream and skills_library backups",
    "R2_ACCESS_KEY_ID": "required for Litestream and skills_library backups",
    "R2_SECRET_ACCESS_KEY": "required for Litestream and skills_library backups",
}

POSITIVE_INT_ENV = {
    "STALE_SCRAPE_RUN_SECONDS": 7200,
    "LIBRARY_BACKUP_INTERVAL_SECONDS": 86400,
}

REQUIRED_DOCKERIGNORE_PATTERNS = {
    ".git": "git history should not be copied into production images",
    ".venv": "local virtualenvs should not be copied into production images",
    "__pycache__": "Python bytecode caches should not be copied into production images",
    "*.pyc": "Python bytecode should not be copied into production images",
    "*.log": "host logs should not be copied into production images",
    "local_skills.db": "SQLite data is mounted at runtime, not baked into the image",
    "local_skills.db-wal": "SQLite WAL files are mounted at runtime, not baked into the image",
    "local_skills.db-shm": "SQLite SHM files are mounted at runtime, not baked into the image",
    "data": "seed DBs and backups belong on the host bind mount, not in the image",
    "skills_library": "skill files are mounted and backed up separately, not baked into the image",
    "content_blobs": "generated content exports belong in backups/R2, not in the image",
    "eval-results": "local benchmark artifacts should not be copied into production images",
    "restore-drill": "restore drill output should not be copied into production images",
}


@dataclass
class Check:
    level: str
    name: str
    detail: str


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = _strip_quotes(value)
    return values


def is_placeholder(value: str) -> bool:
    normalized = value.strip().lower()
    if not normalized:
        return True
    return any(marker in normalized for marker in ("<", "todo", "changeme", "your-", "example.com"))


def add(checks: list[Check], level: str, name: str, detail: str) -> None:
    checks.append(Check(level=level, name=name, detail=detail))


def check_env(env_file: Path, checks: list[Check]) -> dict[str, str]:
    if not env_file.exists():
        add(checks, "FAIL", "env file", f"{env_file} is missing; copy deploy/.env.example and fill it in")
        return {}

    env = load_env_file(env_file)
    add(checks, "PASS", "env file", f"loaded {env_file}")

    for key, reason in REQUIRED_ENV.items():
        value = env.get(key, os.environ.get(key, ""))
        if is_placeholder(value):
            add(checks, "FAIL", f"env {key}", reason)
        else:
            add(checks, "PASS", f"env {key}", "set")

    for key, default in POSITIVE_INT_ENV.items():
        raw = env.get(key, os.environ.get(key, str(default)))
        try:
            parsed = int(raw)
        except ValueError:
            add(checks, "FAIL", f"env {key}", f"expected positive integer, got {raw!r}")
            continue
        if parsed <= 0:
            add(checks, "FAIL", f"env {key}", f"expected positive integer, got {parsed}")
        else:
            add(checks, "PASS", f"env {key}", str(parsed))

    if is_placeholder(env.get("SEARXNG_URL", os.environ.get("SEARXNG_URL", ""))):
        add(checks, "WARN", "env SEARXNG_URL", "unset; scraper can still run, but web search discovery will be thinner")

    check_dashboard_origins(env, checks)
    return env


def check_dashboard_origins(env: dict[str, str], checks: list[Check]) -> None:
    raw = env.get("AUTO_SKILL_DASHBOARD_ORIGINS", os.environ.get("AUTO_SKILL_DASHBOARD_ORIGINS", ""))
    if is_placeholder(raw):
        add(
            checks,
            "WARN",
            "env AUTO_SKILL_DASHBOARD_ORIGINS",
            "unset; compose uses the production auto-skill.com defaults, but set it explicitly on the host",
        )
        return

    origins = [value.strip() for value in raw.split(",") if value.strip()]
    if not origins:
        add(checks, "FAIL", "env AUTO_SKILL_DASHBOARD_ORIGINS", "expected one or more comma-separated origins")
        return

    invalid: list[str] = []
    for origin in origins:
        parsed = urlsplit(origin)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
            or "*" in origin
        ):
            invalid.append(origin)
            continue
        if parsed.scheme == "http" and host not in {"localhost", "127.0.0.1", "::1"}:
            invalid.append(origin)

    if invalid:
        add(
            checks,
            "FAIL",
            "env AUTO_SKILL_DASHBOARD_ORIGINS",
            "invalid origin(s): " + ", ".join(invalid),
        )
    else:
        add(checks, "PASS", "env AUTO_SKILL_DASHBOARD_ORIGINS", f"{len(origins)} allowed origin(s)")


def check_seed_db(db_path: Path, checks: list[Check], min_total: int, min_active: int, min_embedded: int) -> None:
    if not db_path.exists():
        add(checks, "FAIL", "seed db", f"missing {db_path}; copy the current local_skills.db before compose up")
        return
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
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
        add(checks, "FAIL", "seed db", f"could not read launch seed DB {db_path}: {exc}")
        return
    finally:
        try:
            conn.close()
        except Exception:
            pass

    total = int(row["total"] or 0)
    active = int(row["active"] or 0)
    embedded = int(row["embedded"] or 0)
    detail = f"path={db_path}, total={total}, active={active}, embedded={embedded}, integrity={integrity}"
    if integrity != "ok":
        add(checks, "FAIL", "seed db", detail)
    elif total < min_total:
        add(checks, "FAIL", "seed db", f"{detail}; expected total >= {min_total}")
    elif active < min_active:
        add(checks, "FAIL", "seed db", f"{detail}; expected active >= {min_active}")
    elif embedded < min_embedded:
        add(checks, "FAIL", "seed db", f"{detail}; expected embedded >= {min_embedded}")
    else:
        add(checks, "PASS", "seed db", detail)


def check_seed_library(library_dir: Path, checks: list[Check], min_index_entries: int) -> None:
    index_path = library_dir / "index.json"
    files_dir = library_dir / "files"
    if not library_dir.exists():
        add(checks, "FAIL", "skills library", f"missing {library_dir}; copy skills_library before compose up")
        return
    add(checks, "PASS", "skills library", str(library_dir))

    if not index_path.exists():
        add(checks, "FAIL", "library index", f"missing {index_path}; route/content recovery needs the indexed library")
        return
    try:
        index = json.loads(index_path.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        add(checks, "FAIL", "library index", f"could not parse {index_path}: {exc}")
        return
    if not isinstance(index, list):
        add(checks, "FAIL", "library index", f"{index_path} must be a JSON array")
        return

    markdown_files = list(files_dir.glob("*.md")) if files_dir.exists() else []
    detail = f"path={index_path}, entries={len(index)}, markdown_files={len(markdown_files)}"
    if len(index) < min_index_entries:
        add(checks, "FAIL", "library index", f"{detail}; expected entries >= {min_index_entries}")
    elif not markdown_files:
        add(checks, "FAIL", "library files", f"missing markdown files under {files_dir}")
    else:
        add(checks, "PASS", "library index", detail)


def check_files(
    repo_root: Path,
    compose_file: Path,
    checks: list[Check],
    skip_seed_checks: bool,
    min_total: int,
    min_active: int,
    min_embedded: int,
    min_library_entries: int,
) -> None:
    required_files = [
        compose_file,
        repo_root / "deploy" / "Dockerfile",
        repo_root / "deploy" / "litestream.yml",
        repo_root / "deploy" / "backup-library.sh",
        repo_root / "deploy" / "seed_runtime.py",
        repo_root / "deploy" / "export-seed-packet.ps1",
        repo_root / "requirements.txt",
        repo_root / "scraper.py",
        repo_root / "worker.py",
    ]
    for path in required_files:
        if path.exists():
            add(checks, "PASS", f"file {path.name}", str(path))
        else:
            add(checks, "FAIL", f"file {path.name}", f"missing {path}")

    if skip_seed_checks:
        add(checks, "WARN", "seed data", "skipped DB/library seed checks")
        return

    data_dir = repo_root / "data"
    db_path = data_dir / "local_skills.db"
    library_dir = repo_root / "skills_library"

    if data_dir.exists():
        add(checks, "PASS", "data dir", str(data_dir))
    else:
        add(checks, "FAIL", "data dir", f"missing {data_dir}; create it before compose up")

    check_seed_db(db_path, checks, min_total, min_active, min_embedded)
    check_seed_library(library_dir, checks, min_library_entries)


def check_compose_runtime_contract(compose_file: Path, checks: list[Check]) -> None:
    if not compose_file.exists():
        return
    text = compose_file.read_text(encoding="utf-8", errors="replace")
    service_blocks = {
        match.group("name"): match.group(0)
        for match in re.finditer(
            r"(?ms)^  (?P<name>[A-Za-z0-9_-]+):\n.*?(?=^  [A-Za-z0-9_-]+:\n|\Z)",
            text,
        )
    }

    def require(name: str, service: str | None, needles: tuple[str, ...]) -> None:
        haystack = text if service is None else service_blocks.get(service, "")
        missing = [needle for needle in needles if needle not in haystack]
        if missing:
            target = compose_file if service is None else f"{compose_file} service {service!r}"
            add(checks, "FAIL", name, f"{target} missing {', '.join(missing)}")
        else:
            add(checks, "PASS", name, "runtime contract present")

    if not service_blocks:
        add(checks, "FAIL", "compose services", f"{compose_file} did not contain parseable services")
        return

    require("compose api service", "api", ("command: uvicorn scraper:app", "LOCAL_DB_PATH:"))
    require("compose api no scraper loop", "api", ("AUTO_START_SCRAPER: \"0\"", "AUTO_START_EMBEDDER: \"0\""))
    require("compose worker service", "worker", ("command: python worker.py", "LOCAL_DB_URL: http://api:8000"))
    require("compose worker no embedded app loops", "worker", ("AUTO_START_SCRAPER: \"0\"", "AUTO_START_EMBEDDER: \"0\""))
    require("compose tunnel and backups", None, ("cloudflared:", "litestream:", "library-backup:"))


def check_dockerignore(repo_root: Path, checks: list[Check]) -> None:
    path = repo_root / ".dockerignore"
    if not path.exists():
        add(checks, "FAIL", "dockerignore", f"missing {path}; image builds could include DBs, backups, or local artifacts")
        return

    patterns = {
        line.strip().rstrip("/")
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    missing = [
        f"{pattern} ({reason})"
        for pattern, reason in REQUIRED_DOCKERIGNORE_PATTERNS.items()
        if pattern.rstrip("/") not in patterns
    ]
    if missing:
        add(checks, "FAIL", "dockerignore", "missing launch-safety excludes: " + "; ".join(missing))
    else:
        add(checks, "PASS", "dockerignore", "excludes DBs, backups, libraries, logs, caches, and local artifacts")


def check_docker_compose(repo_root: Path, compose_file: Path, env_file: Path, checks: list[Check], skip_docker: bool) -> None:
    if skip_docker:
        add(checks, "WARN", "docker compose config", "skipped")
        return

    docker = shutil.which("docker")
    if not docker:
        add(checks, "FAIL", "docker", "docker executable not found")
        return

    cmd = [
        docker,
        "compose",
        "--env-file",
        str(env_file),
        "-f",
        str(compose_file),
        "config",
        "-q",
    ]
    try:
        proc = subprocess.run(cmd, cwd=repo_root, text=True, capture_output=True, timeout=45)
    except subprocess.TimeoutExpired:
        add(checks, "FAIL", "docker compose config", "timed out after 45 seconds")
        return

    if proc.returncode == 0:
        add(checks, "PASS", "docker compose config", "valid")
    else:
        detail = (proc.stderr or proc.stdout or "docker compose config failed").strip()
        add(checks, "FAIL", "docker compose config", detail)


def print_checks(checks: list[Check]) -> None:
    for check in checks:
        print(f"[{check.level}] {check.name}: {check.detail}")


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Preflight the Auto-Skill docker compose launch path.")
    parser.add_argument("--repo-root", type=Path, default=repo_root)
    parser.add_argument("--env-file", type=Path, default=repo_root / "deploy" / ".env")
    parser.add_argument("--compose-file", type=Path, default=repo_root / "deploy" / "docker-compose.yml")
    parser.add_argument("--skip-docker", action="store_true", help="skip docker compose config validation")
    parser.add_argument("--skip-seed-checks", action="store_true", help="skip data/local_skills.db and skills_library checks")
    parser.add_argument("--min-total", type=int, default=1, help="minimum total skills required in the seed DB")
    parser.add_argument("--min-active", type=int, default=1, help="minimum active skills required in the seed DB")
    parser.add_argument("--min-embedded", type=int, default=1, help="minimum embedded skills required in the seed DB")
    parser.add_argument("--min-library-entries", type=int, default=1, help="minimum index entries required in skills_library")
    args = parser.parse_args(argv)

    root = args.repo_root.resolve()
    env_file = args.env_file.resolve()
    compose_file = args.compose_file.resolve()

    checks: list[Check] = []
    check_env(env_file, checks)
    check_files(
        root,
        compose_file,
        checks,
        args.skip_seed_checks,
        args.min_total,
        args.min_active,
        args.min_embedded,
        args.min_library_entries,
    )
    check_dockerignore(root, checks)
    check_compose_runtime_contract(compose_file, checks)
    check_docker_compose(root, compose_file, env_file, checks, args.skip_docker)

    print_checks(checks)
    failures = [check for check in checks if check.level == "FAIL"]
    warnings = [check for check in checks if check.level == "WARN"]
    print(f"\ncompose_preflight: {len(failures)} failure(s), {len(warnings)} warning(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
