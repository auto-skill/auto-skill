from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


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

    return env


def check_files(repo_root: Path, compose_file: Path, checks: list[Check], skip_seed_checks: bool) -> None:
    required_files = [
        compose_file,
        repo_root / "deploy" / "Dockerfile",
        repo_root / "deploy" / "litestream.yml",
        repo_root / "deploy" / "backup-library.sh",
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
    index_path = library_dir / "index.json"

    if data_dir.exists():
        add(checks, "PASS", "data dir", str(data_dir))
    else:
        add(checks, "FAIL", "data dir", f"missing {data_dir}; create it before compose up")

    if db_path.exists():
        add(checks, "PASS", "seed db", str(db_path))
    else:
        add(checks, "FAIL", "seed db", f"missing {db_path}; copy the current local_skills.db before compose up")

    if library_dir.exists():
        add(checks, "PASS", "skills library", str(library_dir))
    else:
        add(checks, "FAIL", "skills library", f"missing {library_dir}; copy skills_library before compose up")

    if index_path.exists():
        add(checks, "PASS", "library index", str(index_path))
    else:
        add(checks, "FAIL", "library index", f"missing {index_path}; route/content recovery needs the indexed library")


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
    args = parser.parse_args(argv)

    root = args.repo_root.resolve()
    env_file = args.env_file.resolve()
    compose_file = args.compose_file.resolve()

    checks: list[Check] = []
    check_env(env_file, checks)
    check_files(root, compose_file, checks, args.skip_seed_checks)
    check_docker_compose(root, compose_file, env_file, checks, args.skip_docker)

    print_checks(checks)
    failures = [check for check in checks if check.level == "FAIL"]
    warnings = [check for check in checks if check.level == "WARN"]
    print(f"\ncompose_preflight: {len(failures)} failure(s), {len(warnings)} warning(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
