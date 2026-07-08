from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from eval_compare import _load, _metric_rows, _regressions


def _snapshot(
    *,
    hit1: float = 0.8,
    positive_pass: int = 10,
    latency_ms: int = 100,
    skill_find_ms: int = 80,
    injected_tokens: int = 700,
    response_tokens: int = 1000,
) -> dict:
    return {
        "engines": {"hybrid": {"hit1_rate": hit1, "hit3_rate": 0.9}},
        "tier_gate": {
            "positive_pass": positive_pass,
            "positive_total": 10,
            "negative_reject": 7,
            "negative_total": 7,
        },
        "content_quality": {"passed": 4, "total": 4},
        "route_benchmark": {
            "passed": 3,
            "total": 3,
            "cases": [
                {
                    "latency_ms": latency_ms,
                    "skill_find_ms": skill_find_ms,
                    "injected_tokens": injected_tokens,
                    "response_tokens": response_tokens,
                },
                {
                    "latency_ms": latency_ms,
                    "skill_find_ms": skill_find_ms,
                    "injected_tokens": injected_tokens,
                    "response_tokens": response_tokens,
                },
            ],
        },
    }


class EvalCompareTests(unittest.TestCase):
    def test_load_accepts_bom_prefixed_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            path.write_text(json.dumps(_snapshot()), encoding="utf-8-sig")

            self.assertEqual(_load(path)["engines"]["hybrid"]["hit1_rate"], 0.8)

    def test_metric_rows_include_quality_and_cost_signals(self) -> None:
        rows = _metric_rows(_snapshot(), _snapshot(hit1=0.9, positive_pass=9, latency_ms=120))
        by_name = {row["name"]: row for row in rows}

        self.assertEqual(by_name["hybrid.hit1_rate"]["after"], 0.9)
        self.assertEqual(by_name["tier_gate.positive_pass_rate"]["after"], 0.9)
        self.assertEqual(by_name["route_benchmark.avg_latency_ms"]["after"], 120)
        self.assertEqual(by_name["route_benchmark.avg_skill_find_ms"]["after"], 80)
        self.assertEqual(by_name["route_benchmark.avg_injected_tokens"]["after"], 700)
        self.assertFalse(by_name["route_benchmark.avg_latency_ms"]["higher_is_better"])

    def test_regressions_catch_relevance_drop(self) -> None:
        rows = _metric_rows(_snapshot(hit1=0.8), _snapshot(hit1=0.7))

        failures = _regressions(rows, slowdown_tolerance=0.25)

        self.assertTrue(any("hybrid.hit1_rate regressed" in failure for failure in failures))

    def test_regressions_honor_slowdown_tolerance(self) -> None:
        within_tolerance = _metric_rows(_snapshot(latency_ms=100), _snapshot(latency_ms=120))
        over_tolerance = _metric_rows(_snapshot(latency_ms=100), _snapshot(latency_ms=140))

        self.assertFalse(_regressions(within_tolerance, slowdown_tolerance=0.25))
        self.assertTrue(any("avg_latency_ms increased" in failure for failure in _regressions(over_tolerance, 0.25)))


if __name__ == "__main__":
    unittest.main()
