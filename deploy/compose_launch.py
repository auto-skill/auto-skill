from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib import error, request


def run(cmd: list[str], cwd: Path, *, timeout: int = 300) -> None:
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=cwd, check=True, timeout=timeout)


def json_get(url: str, timeout: int = 5) -> tuple[int, dict]:
    req = request.Request(url, headers={"Accept": "application/json", "User-Agent": "auto-skill-compose-launch/1.0"})
    try:
        with request.urlopen(req, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(text) if text else {}
    except error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(text) if text else {}
        except json.JSONDecodeError:
            body = {"_text": text[:500]}
        return exc.code, body


def wait_json_ok(name: str, url: str, wait_seconds: int) -> None:
    deadline = time.monotonic() + wait_seconds
    last = ""
    while time.monotonic() < deadline:
        try:
            status, body = json_get(url)
            last = f"status={status}, body={body}"
            if status == 200 and body.get("ok") is True:
                print(f"[PASS] {name}: {last}", flush=True)
                return
        except Exception as exc:
            last = str(exc)
        time.sleep(3)
    raise RuntimeError(f"{name} did not become ready within {wait_seconds}s: {last}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Preflight and launch the Auto-Skill docker compose stack.")
    parser.add_argument("--repo-root", type=Path, default=root)
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--compose-file", type=Path, default=None)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--wait-seconds", type=int, default=120)
    parser.add_argument("--skip-build", action="store_true", help="run compose up without --build")
    parser.add_argument("--skip-launch-check", action="store_true", help="skip launch_check.py after local readiness")
    parser.add_argument("--skip-seed-checks", action="store_true", help="pass through to compose_preflight.py")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.repo_root.resolve()
    env_file = (args.env_file or root / "deploy" / ".env").resolve()
    compose_file = (args.compose_file or root / "deploy" / "docker-compose.yml").resolve()
    deploy_dir = root / "deploy"

    preflight = [
        sys.executable,
        str(deploy_dir / "compose_preflight.py"),
        "--repo-root",
        str(root),
        "--env-file",
        str(env_file),
        "--compose-file",
        str(compose_file),
    ]
    if args.skip_seed_checks:
        preflight.append("--skip-seed-checks")
    run(preflight, root, timeout=120)

    compose = ["docker", "compose", "--env-file", str(env_file), "-f", str(compose_file), "up", "-d"]
    if not args.skip_build:
        compose.append("--build")
    run(compose, root, timeout=600)

    base = args.base_url.rstrip("/")
    wait_json_ok("api healthz", f"{base}/healthz", args.wait_seconds)
    wait_json_ok("api readyz", f"{base}/readyz", args.wait_seconds)

    if not args.skip_launch_check:
        run(
            [
                sys.executable,
                str(root / "launch_check.py"),
                "--base-url",
                base,
                "--env-file",
                str(env_file),
                "--skip-docker",
            ],
            root,
            timeout=180,
        )
    print("compose launch completed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
