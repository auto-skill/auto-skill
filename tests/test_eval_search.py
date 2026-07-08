from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from eval_search import DEFAULT_ROUTE_CASES_PATH, _evaluate_route_case, _load_route_cases, _validate_route_cases


class EvalSearchRouteCaseTests(unittest.TestCase):
    def test_default_route_cases_load(self) -> None:
        cases = _load_route_cases(DEFAULT_ROUTE_CASES_PATH)
        case_ids = {case["id"] for case in cases}
        platform_traps = [case for case in cases if "platform-trap" in case["tags"]]

        self.assertIn("trap-landingi-001", case_ids)
        self.assertGreaterEqual(len(platform_traps), 5)

    def test_validate_route_cases_rejects_duplicate_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "routes.jsonl"
            path.write_text(
                '{"id":"dup","prompt":"send slack messages","expected_tier":"full|hint",'
                '"allowed_skills":["slack"],"tags":["direct-hit"]}\n'
                '{"id":"dup","prompt":"thanks","expected_tier":"none",'
                '"forbidden_skills":["*"],"tags":["negative"]}\n'
                '{"id":"trap","prompt":"build a landing page","expected_tier":"hint|none",'
                '"forbidden_skills":["landingi"],"tags":["platform-trap"]}\n',
                encoding="utf-8",
            )

            result = _validate_route_cases(path)

        self.assertFalse(result["ok"])
        self.assertEqual(result["duplicate_ids"], ["dup"])

    def test_load_route_cases_accepts_pipe_tiers_and_jsonl_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "routes.jsonl"
            path.write_text(
                '# route cases\n'
                '{"id":"case-1","prompt":"send slack messages","expected_tier":"full|hint",'
                '"allowed_skills":["slack"],"tags":["direct"]}\n',
                encoding="utf-8",
            )

            cases = _load_route_cases(path)

        self.assertEqual(cases[0]["id"], "case-1")
        self.assertEqual(cases[0]["expected_tiers"], {"full", "hint"})
        self.assertEqual(cases[0]["allowed_skills"], ["slack"])
        self.assertEqual(cases[0]["tags"], ["direct"])

    def test_evaluate_route_case_passes_allowed_skill_and_budgets(self) -> None:
        case = {
            "id": "direct-slack",
            "label": "direct slack",
            "query": "send slack messages",
            "expected_tiers": {"hint", "full"},
            "allowed_skills": ["slack"],
            "forbidden_skills": [],
            "min_hint_candidates": 1,
            "tags": ["direct"],
        }
        body = {
            "route_id": "route-1",
            "tier": "hint",
            "skill": {"name": "slack-messenger"},
            "candidates": [{"name": "slack-messenger"}],
            "score_debug": {
                "metrics": {
                    "latency_ms": 10,
                    "skill_find_ms": 8,
                    "injected_tokens": 20,
                    "response_tokens": 30,
                }
            },
        }

        result = _evaluate_route_case(case, 200, body)

        self.assertTrue(result["ok"])
        self.assertEqual(result["failures"], [])

    def test_evaluate_route_case_rejects_forbidden_wildcard_for_negatives(self) -> None:
        case = {
            "id": "negative",
            "label": "negative",
            "query": "thanks",
            "expected_tiers": {"none"},
            "allowed_skills": [],
            "forbidden_skills": ["*"],
            "min_hint_candidates": 0,
            "tags": ["negative"],
        }
        body = {
            "route_id": "route-1",
            "tier": "hint",
            "skill": {"name": "spreadsheet-helper"},
            "candidates": [],
            "score_debug": {"metrics": {"latency_ms": 10, "skill_find_ms": 8}},
        }

        result = _evaluate_route_case(case, 200, body)

        self.assertFalse(result["ok"])
        self.assertTrue(any("wildcard forbidden" in failure for failure in result["failures"]))

    def test_evaluate_route_case_rejects_forbidden_skill_surface(self) -> None:
        case = {
            "id": "trap",
            "label": "trap",
            "query": "build a landing page",
            "expected_tiers": {"hint", "none"},
            "allowed_skills": [],
            "forbidden_skills": ["sales-landingi"],
            "min_hint_candidates": 0,
            "tags": ["platform-trap"],
        }
        body = {
            "route_id": "route-1",
            "tier": "hint",
            "skill": {"name": "sales-landingi"},
            "candidates": [{"name": "landing-page-architect"}],
            "score_debug": {"metrics": {"latency_ms": 10, "skill_find_ms": 8}},
        }

        result = _evaluate_route_case(case, 200, body)

        self.assertFalse(result["ok"])
        self.assertTrue(any("forbidden skill surfaced" in failure for failure in result["failures"]))


if __name__ == "__main__":
    unittest.main()
