"""Preflight checks for an Auto-Skill alpha launch.

Run this on the host after copying the DB/library, filling deploy/.env, and
starting the compose stack:

    python launch_check.py --base-url https://skills.example.com

For a local dry run without a server:

    python launch_check.py --skip-http --skip-docker --skip-env --skip-local
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib import error, request
from urllib.parse import urlsplit


REQUIRED_ENV = (
    "CLOUDFLARED_TOKEN",
    "R2_ENDPOINT",
    "R2_BUCKET",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
)


def _is_blank(value: str | None) -> bool:
    if value is None:
        return True
    stripped = value.strip()
    return not stripped or stripped.startswith("<") or stripped.endswith(">")


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _default_db_path() -> Path:
    configured = os.getenv("LOCAL_DB_PATH")
    if configured:
        return Path(configured)
    data_path = Path("data") / "local_skills.db"
    if data_path.exists():
        return data_path
    return Path("local_skills.db")


def _json_request(base_url: str, method: str, path: str, body: dict | None = None, headers: dict | None = None):
    return _json_url_request(f"{base_url.rstrip('/')}{path}", method, body, headers)


def _json_url_request(url: str, method: str = "GET", body: dict | None = None, headers: dict | None = None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "auto-skill-launch-check/1.0",
    }
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)
    req = request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with request.urlopen(req, timeout=15) as response:
            text = response.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(text) if text else {}
            except json.JSONDecodeError:
                payload = {"_text": text[:500]}
            return response.status, payload
    except error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(text) if text else {}
        except json.JSONDecodeError:
            payload = {"_text": text[:500]}
        return exc.code, payload


class Reporter:
    def __init__(self) -> None:
        self.failures = 0
        self.warnings = 0

    def pass_(self, name: str, detail: str) -> None:
        print(f"[PASS] {name}: {detail}")

    def warn(self, name: str, detail: str) -> None:
        self.warnings += 1
        print(f"[WARN] {name}: {detail}")

    def fail(self, name: str, detail: str) -> None:
        self.failures += 1
        print(f"[FAIL] {name}: {detail}")


def check_env(reporter: Reporter, env_file: Path) -> None:
    if not env_file.exists():
        reporter.fail("env", f"{env_file} is missing; copy deploy/.env.example and fill real values")
        return
    values = _read_env(env_file)
    missing = [key for key in REQUIRED_ENV if _is_blank(values.get(key))]
    if missing:
        reporter.fail("env", f"missing launch secrets: {', '.join(missing)}")
    else:
        reporter.pass_("env", f"{env_file} has Cloudflare/R2 values")
    if _is_blank(values.get("GITHUB_TOKEN")):
        reporter.warn("env", "GITHUB_TOKEN is blank; scraper discovery will be sharply limited")
    _check_dashboard_origins(reporter, values)


def _check_dashboard_origins(reporter: Reporter, values: dict[str, str]) -> None:
    raw = values.get("AUTO_SKILL_DASHBOARD_ORIGINS", os.getenv("AUTO_SKILL_DASHBOARD_ORIGINS", ""))
    if _is_blank(raw):
        reporter.warn(
            "dashboard origins",
            "AUTO_SKILL_DASHBOARD_ORIGINS is blank; dashboard OAuth will use code defaults, "
            "but production hosts should pin exact dashboard origins",
        )
        return

    origins = [value.strip() for value in raw.split(",") if value.strip()]
    if not origins:
        reporter.fail("dashboard origins", "expected one or more comma-separated origins")
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
        reporter.fail("dashboard origins", "invalid AUTO_SKILL_DASHBOARD_ORIGINS: " + ", ".join(invalid))
    else:
        reporter.pass_("dashboard origins", f"{len(origins)} allowed origin(s)")


def check_db(reporter: Reporter, db_path: Path, min_total: int, min_active: int, min_embedded: int) -> None:
    if not db_path.exists():
        reporter.fail("db", f"{db_path} does not exist")
        return
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN COALESCE(quality_status, 'active') = 'active' THEN 1 ELSE 0 END) AS active,
              SUM(CASE WHEN COALESCE(quality_status, 'active') = 'metadata_only' THEN 1 ELSE 0 END) AS metadata_only,
              SUM(CASE WHEN COALESCE(quality_status, 'active') = 'rejected' THEN 1 ELSE 0 END) AS rejected,
              SUM(CASE WHEN COALESCE(quality_status, 'active') = 'duplicate' THEN 1 ELSE 0 END) AS duplicate,
              SUM(CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END) AS embedded,
              SUM(CASE WHEN content_hash IS NOT NULL AND content_hash != '' THEN 1 ELSE 0 END) AS hashed
            FROM skills
            """
        ).fetchone()
    except Exception as exc:
        reporter.fail("db", f"could not read {db_path}: {exc}")
        return
    finally:
        try:
            conn.close()
        except Exception:
            pass

    counts = {key: int(row[key] or 0) for key in row.keys()}
    detail = ", ".join(f"{key}={value}" for key, value in counts.items())
    if counts["total"] < min_total:
        reporter.fail("db", f"not enough rows ({detail})")
    elif counts["active"] < min_active:
        reporter.fail("db", f"not enough active rows ({detail})")
    elif counts["embedded"] < min_embedded:
        reporter.fail("db", f"not enough embedded rows ({detail}); run python reindex.py or the worker")
    else:
        reporter.pass_("db", detail)


def check_library(reporter: Reporter, library_dir: Path) -> None:
    index_path = library_dir / "index.json"
    files_dir = library_dir / "files"
    if not index_path.exists():
        reporter.fail("library", f"{index_path} is missing")
        return
    try:
        index = json.loads(index_path.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        reporter.fail("library", f"could not parse {index_path}: {exc}")
        return
    file_count = len(list(files_dir.glob("*.md"))) if files_dir.exists() else 0
    if not index:
        reporter.fail("library", "index.json has no entries")
    elif file_count == 0:
        reporter.fail("library", f"{files_dir} has no markdown files")
    else:
        reporter.pass_("library", f"index_entries={len(index)}, markdown_files={file_count}")


def _route_metrics(body: dict) -> dict:
    return ((body.get("score_debug") or {}).get("metrics") or {})


def _bad_gateway_detail(status: int, body: dict) -> str | None:
    if status != 502:
        return None
    title = str(body.get("title") or body.get("error") or body.get("_text") or "")
    if "bad gateway" in title.lower():
        return (
            "Cloudflare reached the tunnel but the API origin returned Bad Gateway; "
            "run deploy\\recover-host.ps1 on the host, then inspect deploy\\diagnose-host.ps1 if recovery fails"
        )
    return (
        "HTTP 502 from the public edge or API origin; run deploy\\recover-host.ps1 on the host, "
        "then inspect deploy\\diagnose-host.ps1 if recovery fails"
    )


def _empty_runtime_detail(body: dict) -> str | None:
    counts = {
        "total_skills": int(body.get("total_skills") or 0),
        "active_skills": int(body.get("active_skills") or 0),
        "embedded_skills": int(body.get("embedded_skills") or 0),
    }
    if body.get("ok") is False and all(value == 0 for value in counts.values()):
        return (
            "origin reachable but runtime DB/library is empty or not mounted "
            f"({', '.join(f'{key}={value}' for key, value in counts.items())}); "
            "seed or restore local_skills.db and skills_library with deploy\\seed_runtime.py, "
            "then run backfill_quality.py and reindex.py or the worker"
        )
    return None


def _no_candidate_route_detail(body: dict) -> str | None:
    candidates = body.get("candidates")
    if body.get("tier") == "none" and isinstance(candidates, list) and not candidates:
        reason = ((body.get("score_debug") or {}).get("reason") or "no candidates")
        return (
            f"route returned no candidates ({reason}); runtime DB/library may be empty, "
            "unembedded, or not mounted"
        )
    return None


def _check_scraper_summary(reporter: Reporter, name: str, summary: dict) -> None:
    if not summary:
        reporter.warn(name, "scraper summary missing from readiness payload")
        return
    running_recent = int(summary.get("running_recent") or 0)
    running_stale = int(summary.get("running_stale") or 0)
    last_success_at = summary.get("last_success_at") or "never"
    if running_stale:
        reporter.fail(name, f"running_stale={running_stale}, last_success_at={last_success_at}")
    elif running_recent > 1:
        reporter.fail(name, f"running_recent={running_recent}; only one scraper should run")
    else:
        reporter.pass_(
            name,
            f"running_recent={running_recent}, running_stale={running_stale}, "
            f"last_success_at={last_success_at}",
        )


def _check_route_budget(
    reporter: Reporter,
    name: str,
    body: dict,
    max_latency_ms: int,
    max_skill_find_ms: int,
    max_injected_tokens: int,
    max_response_tokens: int,
) -> None:
    metrics = _route_metrics(body)
    if not metrics:
        reporter.fail(name, "route response did not include score_debug.metrics")
        return
    latency_ms = int(metrics.get("latency_ms") or 0)
    skill_find_ms = int(metrics.get("skill_find_ms") or metrics.get("retrieval_ms") or 0)
    injected_tokens = int(metrics.get("injected_tokens") or metrics.get("content_tokens") or 0)
    response_tokens = int(metrics.get("response_tokens") or 0)
    if latency_ms > max_latency_ms:
        reporter.fail(name, f"latency_ms={latency_ms} exceeded budget {max_latency_ms}")
    elif skill_find_ms > max_skill_find_ms:
        reporter.fail(name, f"skill_find_ms={skill_find_ms} exceeded budget {max_skill_find_ms}")
    elif injected_tokens > max_injected_tokens:
        reporter.fail(name, f"injected_tokens={injected_tokens} exceeded budget {max_injected_tokens}")
    elif response_tokens > max_response_tokens:
        reporter.fail(name, f"response_tokens={response_tokens} exceeded budget {max_response_tokens}")
    else:
        reporter.pass_(
            name,
            f"latency_ms={latency_ms}, skill_find_ms={skill_find_ms}, "
            f"injected_tokens={injected_tokens}, response_tokens={response_tokens}",
        )


def _check_route_metrics_summary(
    reporter: Reporter,
    name: str,
    body: dict,
    max_route_latency_ms: int,
    max_route_skill_find_ms: int,
    max_route_injected_tokens: int,
    max_route_response_tokens: int,
) -> None:
    if not body or body.get("ok") is not True:
        reporter.warn(name, f"route metrics payload missing ok=true: {body}")
        return

    total = int(body.get("total") or 0)
    budgets = body.get("budgets") if isinstance(body.get("budgets"), dict) else {}
    expected_budgets = {
        "latency_ms": max_route_latency_ms,
        "skill_find_ms": max_route_skill_find_ms,
        "injected_tokens": max_route_injected_tokens,
        "response_tokens": max_route_response_tokens,
    }
    mismatched = [
        f"{key}={budgets.get(key)} expected={expected}"
        for key, expected in expected_budgets.items()
        if int(budgets.get(key) or 0) not in {0, expected}
    ]
    if mismatched:
        reporter.warn(name, f"route metrics used unexpected budget(s): {', '.join(mismatched)}")

    if total <= 0:
        reporter.warn(name, "no recent route analytics yet; run local route probes before launch")
        return

    breaches = body.get("budget_breaches") if isinstance(body.get("budget_breaches"), dict) else {}
    breach_count = int(breaches.get("any") or 0)
    if breach_count:
        reporter.fail(
            name,
            "recent route budget breach(es): "
            f"any={breach_count}, latency_ms={int(breaches.get('latency_ms') or 0)}, "
            f"skill_find_ms={int(breaches.get('skill_find_ms') or 0)}, "
            f"injected_tokens={int(breaches.get('injected_tokens') or 0)}, "
            f"response_tokens={int(breaches.get('response_tokens') or 0)}",
        )
        return

    reporter.pass_(
        name,
        f"total={total}, p95_latency_ms={int(body.get('p95_latency_ms') or 0)}, "
        f"p95_skill_find_ms={int(body.get('p95_skill_find_ms') or 0)}, "
        f"p95_injected_tokens={int(body.get('p95_injected_tokens') or 0)}, "
        f"p95_response_tokens={int(body.get('p95_response_tokens') or 0)}",
    )


def check_http(
    reporter: Reporter,
    base_url: str,
    direct_task: str,
    trap_task: str,
    max_route_latency_ms: int,
    max_route_skill_find_ms: int,
    max_route_injected_tokens: int,
    max_route_response_tokens: int,
) -> None:
    try:
        status, body = _json_request(base_url, "GET", "/healthz")
    except Exception as exc:
        reporter.fail("http", f"/healthz failed: {exc}")
        return
    if status == 200 and body.get("ok") is True and body.get("service") == "auto-skill-api":
        reporter.pass_("http healthz", json.dumps(body, sort_keys=True)[:220])
    elif status == 200 and body.get("ok") is True:
        reporter.fail(
            "http healthz",
            "stale or unknown health response; expected service=auto-skill-api, "
            f"body={json.dumps(body, sort_keys=True)[:220]}",
        )
    else:
        detail = _bad_gateway_detail(status, body)
        reporter.fail("http healthz", detail or f"status={status}, body={body}")

    status, body = _json_request(base_url, "GET", "/readyz")
    if status == 200 and body.get("ok") is True and body.get("active_skills", 0) > 0 and body.get("embedded_skills", 0) > 0:
        reporter.pass_("http readyz", json.dumps(body, sort_keys=True)[:220])
        _check_scraper_summary(reporter, "readyz scraper", body.get("scraper") or {})
    else:
        detail = _bad_gateway_detail(status, body) or _empty_runtime_detail(body)
        reporter.fail("http readyz", detail or f"status={status}, body={body}")

    status, body = _json_request(base_url, "GET", "/status")
    if status == 200:
        summary = body.get("scraper") or {}
        recent_runs = summary.get("recent_runs") or body.get("recent_runs") or []
        running = [run for run in recent_runs if run.get("status") == "running"]
        total = body.get("total_skills", "unknown")
        if summary:
            _check_scraper_summary(reporter, "scraper status", summary)
        elif len(running) > 1:
            reporter.fail("scraper status", f"{len(running)} recent scrape runs are still marked running")
        else:
            reporter.pass_("scraper status", f"total_skills={total}, running_recent={len(running)}")
    else:
        reporter.warn("scraper status", f"/status unavailable: status={status}, body={body}")

    status, body = _json_request(base_url, "POST", "/route", {"task": direct_task})
    if status == 200 and body.get("tier") in {"full", "hint"} and body.get("skill"):
        skill = body.get("skill") or {}
        reporter.pass_("route direct", f"tier={body.get('tier')}, skill={skill.get('name') or skill.get('slug')}")
        _check_route_budget(
            reporter,
            "route direct budget",
            body,
            max_route_latency_ms,
            max_route_skill_find_ms,
            max_route_injected_tokens,
            max_route_response_tokens,
        )
    else:
        detail = _bad_gateway_detail(status, body) or _no_candidate_route_detail(body)
        reporter.fail("route direct", detail or f"status={status}, body={json.dumps(body, sort_keys=True)[:500]}")

    status, body = _json_request(base_url, "POST", "/route", {"task": trap_task})
    skill = body.get("skill") or {}
    skill_blob = f"{skill.get('name', '')} {skill.get('url', '')} {skill.get('source_url', '')}".lower()
    if status != 200:
        detail = _bad_gateway_detail(status, body)
        reporter.fail("route trap", detail or f"status={status}, body={body}")
    elif body.get("tier") == "full" and "landingi" in skill_blob:
        reporter.fail("route trap", "generic landing-page prompt full-routed to Landingi")
    else:
        reporter.pass_("route trap", f"tier={body.get('tier')}, skill={skill.get('name') or skill.get('slug')}")
        _check_route_budget(
            reporter,
            "route trap budget",
            body,
            max_route_latency_ms,
            max_route_skill_find_ms,
            max_route_injected_tokens,
            max_route_response_tokens,
        )

    status, body = _json_request(base_url, "GET", "/route-metrics")
    if status == 200:
        _check_route_metrics_summary(
            reporter,
            "route metrics",
            body,
            max_route_latency_ms,
            max_route_skill_find_ms,
            max_route_injected_tokens,
            max_route_response_tokens,
        )
    elif status == 403:
        reporter.pass_("route metrics privacy", "/route-metrics is local-only through the public guard")
    else:
        reporter.warn("route metrics", f"/route-metrics unavailable: status={status}, body={body}")

    guarded_paths = [
        ("POST", "/scrape", {}),
        ("POST", "/chat", {"messages": [{"role": "user", "content": "find a spreadsheet skill"}]}),
        ("POST", "/seed-backlog", {}),
        ("POST", "/normalize-db", {}),
        ("GET", "/normalize-db/progress", None),
        ("POST", "/rescan", {}),
        ("GET", "/skills", None),
        ("GET", "/library", None),
        ("GET", "/library/files/example.md", None),
        ("GET", "/route-metrics", None),
        ("POST", "/route-feedback", {"route_id": "launch-check", "outcome": "used"}),
        ("GET", "/rest/v1/skills?select=id", None),
        ("POST", "/rest/v1/rpc/search_skills", {"query": "spreadsheet", "max_results": 3}),
        ("POST", "/rest/v1/rpc/vector_search_skills", {"query_embedding": [0.0] * 384, "match_count": 3}),
        ("POST", "/rest/v1/rpc/hybrid_search_skills", {"query_text": "spreadsheet", "match_count": 3}),
        ("POST", "/rest/v1/skills", {"id": "launch-check", "name": "blocked", "source": "launch_check"}),
        ("PATCH", "/rest/v1/skills?id=eq.launch-check", {"name": "blocked"}),
        ("DELETE", "/rest/v1/skills?id=eq.launch-check", None),
    ]
    guard_failures = []
    for method, path, payload in guarded_paths:
        status, body = _json_request(
            base_url,
            method,
            path,
            payload,
            headers={"x-forwarded-for": "203.0.113.10"},
        )
        if status != 403:
            guard_failures.append(f"{method} {path} -> status={status}, body={body}")
    if guard_failures:
        reporter.fail("public write/admin guard", "; ".join(guard_failures)[:1200])
    else:
        reporter.pass_("public write/admin guard", f"{len(guarded_paths)} forwarded admin/write probes returned 403")


def check_mcp_health(reporter: Reporter, health_url: str) -> None:
    if not health_url.strip():
        return
    try:
        status, body = _json_url_request(health_url.strip())
    except Exception as exc:
        reporter.fail("mcp health", f"{health_url} failed: {exc}")
        return
    if status == 200 and body.get("ok") is True:
        reporter.pass_("mcp health", json.dumps(body, sort_keys=True)[:220])
    else:
        detail = _bad_gateway_detail(status, body)
        reporter.fail("mcp health", detail or f"status={status}, body={body}")


def check_docker(reporter: Reporter, env_file: Path) -> None:
    if shutil.which("docker") is None:
        reporter.warn("docker", "docker is not installed on this machine; run compose config on the host")
        return
    env_arg = str(env_file if env_file.exists() else Path("deploy") / ".env.example")
    cmd = ["docker", "compose", "--env-file", env_arg, "-f", str(Path("deploy") / "docker-compose.yml"), "config"]
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=60)
    if proc.returncode == 0:
        reporter.pass_("docker compose", "config rendered successfully")
    else:
        reporter.fail("docker compose", (proc.stderr or proc.stdout).strip()[:800])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Auto-Skill launch preflight checks.")
    parser.add_argument("--base-url", default=os.getenv("AUTOSKILL_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--db-path", type=Path, default=_default_db_path())
    parser.add_argument("--library-dir", type=Path, default=Path("skills_library"))
    parser.add_argument("--env-file", type=Path, default=Path("deploy") / ".env")
    parser.add_argument("--direct-task", default="create an excel spreadsheet report with formulas and charts")
    parser.add_argument("--trap-task", default="build a landing page for an AI automation agency")
    parser.add_argument("--min-total", type=int, default=1)
    parser.add_argument("--min-active", type=int, default=1)
    parser.add_argument("--min-embedded", type=int, default=1)
    parser.add_argument("--max-route-latency-ms", type=int, default=1500)
    parser.add_argument("--max-route-skill-find-ms", type=int, default=1200)
    parser.add_argument("--max-route-injected-tokens", type=int, default=3000)
    parser.add_argument("--max-route-response-tokens", type=int, default=3500)
    parser.add_argument(
        "--mcp-health-url",
        default=os.getenv("AUTOSKILL_MCP_HEALTH_URL", ""),
        help="optional connector MCP health endpoint, for example https://mcp.example.com/healthz",
    )
    parser.add_argument("--skip-env", action="store_true")
    parser.add_argument("--skip-local", action="store_true")
    parser.add_argument("--skip-http", action="store_true")
    parser.add_argument("--skip-docker", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    reporter = Reporter()

    if not args.skip_env:
        check_env(reporter, args.env_file)
    if not args.skip_local:
        check_db(reporter, args.db_path, args.min_total, args.min_active, args.min_embedded)
        check_library(reporter, args.library_dir)
    if not args.skip_http:
        check_http(
            reporter,
            args.base_url,
            args.direct_task,
            args.trap_task,
            args.max_route_latency_ms,
            args.max_route_skill_find_ms,
            args.max_route_injected_tokens,
            args.max_route_response_tokens,
        )
        check_mcp_health(reporter, args.mcp_health_url)
    if not args.skip_docker:
        check_docker(reporter, args.env_file)

    print()
    if reporter.failures:
        print(f"launch_check: {reporter.failures} failure(s), {reporter.warnings} warning(s)")
        return 1
    print(f"launch_check: passed with {reporter.warnings} warning(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
