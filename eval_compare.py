"""Compare two eval_search.py JSON snapshots.

Use this after routing or quality-gate changes to see whether relevance,
route correctness, latency, or token churn moved in the right direction.

Run:
  python eval_compare.py eval-results/before.json eval-results/after.json
  python eval_compare.py --fail-on-regression eval-results/before.json eval-results/after.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from statistics import mean
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _rate(passed: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return passed / total


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def _delta(after: float, before: float) -> str:
    diff = after - before
    sign = "+" if diff >= 0 else ""
    return f"{sign}{diff:.4f}"


def _case_values(snapshot: dict[str, Any], key: str) -> list[float]:
    cases = ((snapshot.get("route_benchmark") or {}).get("cases") or [])
    values: list[float] = []
    for case in cases:
        value = case.get(key)
        if isinstance(value, (int, float)):
            values.append(float(value))
    return values


def _mean_case_value(snapshot: dict[str, Any], key: str) -> float:
    values = _case_values(snapshot, key)
    return mean(values) if values else 0.0


def _metric_rows(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for engine in sorted(set((before.get("engines") or {}) | (after.get("engines") or {}))):
        before_engine = (before.get("engines") or {}).get(engine) or {}
        after_engine = (after.get("engines") or {}).get(engine) or {}
        for metric in ("hit1_rate", "hit3_rate"):
            rows.append(
                {
                    "name": f"{engine}.{metric}",
                    "before": float(before_engine.get(metric) or 0.0),
                    "after": float(after_engine.get(metric) or 0.0),
                    "higher_is_better": True,
                    "kind": "rate",
                }
            )

    before_gate = before.get("tier_gate") or {}
    after_gate = after.get("tier_gate") or {}
    rows.extend(
        [
            {
                "name": "tier_gate.positive_pass_rate",
                "before": _rate(int(before_gate.get("positive_pass") or 0), int(before_gate.get("positive_total") or 0)),
                "after": _rate(int(after_gate.get("positive_pass") or 0), int(after_gate.get("positive_total") or 0)),
                "higher_is_better": True,
                "kind": "rate",
            },
            {
                "name": "tier_gate.negative_reject_rate",
                "before": _rate(int(before_gate.get("negative_reject") or 0), int(before_gate.get("negative_total") or 0)),
                "after": _rate(int(after_gate.get("negative_reject") or 0), int(after_gate.get("negative_total") or 0)),
                "higher_is_better": True,
                "kind": "rate",
            },
        ]
    )

    before_content = before.get("content_quality") or {}
    after_content = after.get("content_quality") or {}
    rows.append(
        {
            "name": "content_quality.pass_rate",
            "before": _rate(int(before_content.get("passed") or 0), int(before_content.get("total") or 0)),
            "after": _rate(int(after_content.get("passed") or 0), int(after_content.get("total") or 0)),
            "higher_is_better": True,
            "kind": "rate",
        }
    )

    before_route = before.get("route_benchmark") or {}
    after_route = after.get("route_benchmark") or {}
    rows.extend(
        [
            {
                "name": "route_benchmark.pass_rate",
                "before": _rate(int(before_route.get("passed") or 0), int(before_route.get("total") or 0)),
                "after": _rate(int(after_route.get("passed") or 0), int(after_route.get("total") or 0)),
                "higher_is_better": True,
                "kind": "rate",
            },
            {
                "name": "route_benchmark.avg_latency_ms",
                "before": _mean_case_value(before, "latency_ms"),
                "after": _mean_case_value(after, "latency_ms"),
                "higher_is_better": False,
                "kind": "number",
            },
            {
                "name": "route_benchmark.avg_skill_find_ms",
                "before": _mean_case_value(before, "skill_find_ms"),
                "after": _mean_case_value(after, "skill_find_ms"),
                "higher_is_better": False,
                "kind": "number",
            },
            {
                "name": "route_benchmark.avg_injected_tokens",
                "before": _mean_case_value(before, "injected_tokens"),
                "after": _mean_case_value(after, "injected_tokens"),
                "higher_is_better": False,
                "kind": "number",
            },
            {
                "name": "route_benchmark.avg_response_tokens",
                "before": _mean_case_value(before, "response_tokens"),
                "after": _mean_case_value(after, "response_tokens"),
                "higher_is_better": False,
                "kind": "number",
            },
        ]
    )
    return rows


def _format_value(value: float, kind: str) -> str:
    if kind == "rate":
        return _percent(value)
    return f"{value:.1f}"


def _print_rows(rows: list[dict[str, Any]]) -> None:
    print(f"{'metric':<42} {'before':>10} {'after':>10} {'delta':>10}")
    for row in rows:
        before = float(row["before"])
        after = float(row["after"])
        print(
            f"{row['name']:<42} "
            f"{_format_value(before, row['kind']):>10} "
            f"{_format_value(after, row['kind']):>10} "
            f"{_delta(after, before):>10}"
        )


def _regressions(rows: list[dict[str, Any]], slowdown_tolerance: float) -> list[str]:
    failures: list[str] = []
    for row in rows:
        before = float(row["before"])
        after = float(row["after"])
        if row["higher_is_better"]:
            if after < before:
                failures.append(f"{row['name']} regressed from {_format_value(before, row['kind'])} to {_format_value(after, row['kind'])}")
        elif before > 0 and after > before * (1 + slowdown_tolerance):
            failures.append(
                f"{row['name']} increased from {_format_value(before, row['kind'])} "
                f"to {_format_value(after, row['kind'])}"
            )
    return failures


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare two eval_search.py JSON snapshots.")
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--fail-on-regression", action="store_true")
    parser.add_argument(
        "--slowdown-tolerance",
        type=float,
        default=0.25,
        help="allowed fractional increase for latency/token metrics before they count as regressions",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    before = _load(args.before)
    after = _load(args.after)
    print(f"before: {args.before} ({before.get('created_at', 'unknown')})")
    print(f"after:  {args.after} ({after.get('created_at', 'unknown')})")
    print()

    rows = _metric_rows(before, after)
    _print_rows(rows)

    failures = _regressions(rows, max(0.0, args.slowdown_tolerance))
    if failures:
        print("\nregressions:")
        for failure in failures:
            print(f"- {failure}")
    else:
        print("\nno regressions detected")

    if failures and args.fail_on_regression:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
