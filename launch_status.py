"""Lightweight public status probe for Auto-Skill alpha hosting.

This is intentionally smaller than launch_check.py. Use it when you need a
quick answer to "is the public API/MCP live, and what should we do next?"

Run:
  python launch_status.py
  python launch_status.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict
from urllib import error, request


DEFAULT_BASE_URL = "https://skills.avalahome.com"
DEFAULT_MCP_HEALTH_URL = "https://mcp.avalahome.com/healthz"


@dataclass
class Probe:
    name: str
    method: str
    url: str
    status: int | None
    state: str
    detail: str


def _json_url_request(url: str, method: str = "GET", body: dict | None = None, timeout: int = 12) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "User-Agent": "auto-skill-launch-status/1.0",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=timeout) as response:
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


def classify(status: int | None, body: dict) -> tuple[str, str]:
    if status is None:
        return "error", "request failed"
    if status == 530 and (body.get("cloudflare_error") or body.get("error_code") == 1033):
        return "down", "Cloudflare tunnel cannot reach the origin"
    if status == 530:
        return "down", "HTTP 530 from edge"
    if status == 403:
        return "blocked", "request reached an origin but was forbidden"
    if status == 200:
        tier = body.get("tier")
        if body.get("ok") is True:
            return "ok", "ok=true"
        if tier:
            return "ok", f"route tier={tier}"
        return "ok", "HTTP 200"
    if 500 <= status <= 599:
        return "down", f"server error {status}"
    if 400 <= status <= 499:
        return "blocked", f"client/guard status {status}"
    return "unknown", f"status {status}"


def probe(name: str, method: str, url: str, body: dict | None = None) -> Probe:
    try:
        status, payload = _json_url_request(url, method, body)
        state, detail = classify(status, payload)
        if payload.get("title"):
            detail = f"{detail}; {payload.get('title')}"
        return Probe(name=name, method=method, url=url, status=status, state=state, detail=detail)
    except Exception as exc:
        return Probe(name=name, method=method, url=url, status=None, state="error", detail=str(exc))


def run_status(base_url: str, mcp_health_url: str) -> list[Probe]:
    base = base_url.rstrip("/")
    probes = [
        probe("api healthz", "GET", f"{base}/healthz"),
        probe("api readyz", "GET", f"{base}/readyz"),
        probe("api route", "POST", f"{base}/route", {"task": "create an excel spreadsheet report", "client": "launch-status"}),
    ]
    if mcp_health_url.strip():
        probes.append(probe("mcp healthz", "GET", mcp_health_url.strip()))
    return probes


def next_action(probes: list[Probe]) -> str:
    states = {item.state for item in probes}
    details = " ".join(item.detail.lower() for item in probes)
    if any(item.status == 530 for item in probes) or "cloudflare tunnel" in details:
        return (
            "Public edge is down at the tunnel/origin layer. On the host, run "
            "deploy\\recover-host.ps1; if this keeps recurring, move to the VPS compose path."
        )
    if "blocked" in states:
        return (
            "Traffic reached an origin but at least one route is blocked. Pull latest backend, "
            "restart tasks, then run launch_check.py."
        )
    if states == {"ok"}:
        return "Public API/MCP look live. Run launch_check.py and connector scripts\\live_smoke.py before launch."
    return "Status is mixed. Run deploy\\diagnose-host.ps1 on the host for local process and tunnel details."


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe public Auto-Skill alpha status.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--mcp-health-url", default=DEFAULT_MCP_HEALTH_URL)
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    probes = run_status(args.base_url, args.mcp_health_url)
    action = next_action(probes)
    status = {
        "base_url": args.base_url,
        "mcp_health_url": args.mcp_health_url,
        "probes": [asdict(item) for item in probes],
        "next_action": action,
    }
    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True))
    else:
        for item in probes:
            code = item.status if item.status is not None else "ERR"
            print(f"[{item.state.upper()}] {item.name}: {code} {item.detail}")
        print(f"\nnext action: {action}")
    return 0 if all(item.state == "ok" for item in probes) else 1


if __name__ == "__main__":
    sys.exit(main())
