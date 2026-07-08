import unittest

from quality import PLATFORM_ALIASES, evaluate_quality, rerank_candidates, tier_for_prompt


VALID_CONTENT = """---
name: spreadsheet-reporter
description: Build spreadsheet reports with formulas and charts.
---

## Workflow

Use when the user needs an Excel or spreadsheet report with formulas, charts,
tables, and repeatable formatting. Inspect the source data, create a workbook,
add formulas, verify calculations, add charts, and explain the generated file.
Always validate sheet names, formulas, and chart ranges before returning output.
"""


class QualityGateTests(unittest.TestCase):
    def test_accepts_structured_skill_content(self):
        result = evaluate_quality(
            {
                "name": "spreadsheet-reporter",
                "description": "Build spreadsheet reports with formulas and charts.",
                "source": "github_skill_file",
                "tags": [],
                "raw": {"stars": 12},
            },
            VALID_CONTENT,
        )

        self.assertEqual(result["quality_status"], "active")
        self.assertGreaterEqual(result["quality_score"], 40)
        self.assertTrue(result["content_hash"])

    def test_rejects_path_only_stubs(self):
        result = evaluate_quality(
            {
                "name": "stub",
                "description": "A placeholder skill entry.",
                "source": "github_skill_file",
                "tags": [],
                "raw": {},
            },
            "./foo/bar/SKILL.md\n./foo/baz/SKILL.md\nhttps://example.com/a/b\n",
        )

        self.assertEqual(result["quality_status"], "rejected")
        self.assertIn("path-or-link-only", result["quality_reasons"])

    def test_trusted_registry_metadata_is_hint_only(self):
        result = evaluate_quality(
            {
                "name": "slack-mcp-server",
                "description": "Connect an agent to Slack channels, messages, and workspace search.",
                "source": "mcp_official_registry",
                "tags": ["mcp", "slack"],
                "raw": {},
            },
            "",
        )

        self.assertEqual(result["quality_status"], "metadata_only")
        self.assertIn("slack", result["platforms"])


class RoutingTierTests(unittest.TestCase):
    def test_platform_trap_caps_landingi_to_hint(self):
        prompt = "build a landing page for an AI automation agency"
        candidate = {
            "name": "sales-landingi",
            "description": "Landingi platform help for landing pages, leads, CRM sync, API keys, and publishing.",
            "tags": ["landingi", "landing-page"],
            "platforms": ["landingi"],
            "quality_status": "active",
            "quality_score": 90,
            "rank": 1.0,
            "similarity": 0.95,
        }

        ranked = rerank_candidates(prompt, [candidate])

        self.assertTrue(ranked[0]["platform_mismatch"])
        self.assertEqual(tier_for_prompt(prompt, [candidate]), "hint")

    def test_platform_explicit_can_full_route(self):
        prompt = "build a landing page on Landingi for an AI automation agency"
        candidate = {
            "name": "sales-landingi",
            "description": "Landingi platform help for landing pages, leads, CRM sync, API keys, and publishing.",
            "tags": ["landingi", "landing-page"],
            "platforms": ["landingi"],
            "quality_status": "active",
            "quality_score": 90,
            "rank": 1.0,
            "similarity": 0.95,
        }

        self.assertEqual(tier_for_prompt(prompt, [candidate]), "full")

    def test_platform_traps_cap_all_known_platforms_to_hint(self):
        generic_prompt = "build a customer dashboard and publish it"
        for platform in sorted(PLATFORM_ALIASES):
            with self.subTest(platform=platform):
                candidate = {
                    "name": f"{platform}-workflow",
                    "description": (
                        f"{platform} platform help for customer dashboards, publishing, "
                        "API keys, webhooks, and sync issues."
                    ),
                    "tags": [platform, "dashboard"],
                    "platforms": [platform],
                    "quality_status": "active",
                    "quality_score": 90,
                    "rank": 1.0,
                    "similarity": 0.95,
                }

                ranked = rerank_candidates(generic_prompt, [candidate])

                self.assertTrue(ranked[0]["platform_mismatch"])
                self.assertEqual(tier_for_prompt(generic_prompt, [candidate]), "hint")

    def test_platform_explicit_prompts_allow_known_platforms(self):
        for platform, aliases in sorted(PLATFORM_ALIASES.items()):
            with self.subTest(platform=platform):
                alias = aliases[0]
                prompt = f"build a {alias} customer dashboard and publish it"
                candidate = {
                    "name": f"{platform}-workflow",
                    "description": (
                        f"{alias} platform help for customer dashboards, publishing, "
                        "API keys, webhooks, and sync issues."
                    ),
                    "tags": [platform, "dashboard"],
                    "platforms": [platform],
                    "quality_status": "active",
                    "quality_score": 90,
                    "rank": 1.0,
                    "similarity": 0.95,
                }

                ranked = rerank_candidates(prompt, [candidate])

                self.assertFalse(ranked[0]["platform_mismatch"])
                self.assertEqual(tier_for_prompt(prompt, [candidate]), "full")

    def test_metadata_only_never_full_routes(self):
        prompt = "send slack messages from my agent"
        candidate = {
            "name": "slack-mcp-server",
            "description": "Connect an agent to Slack channels, messages, and workspace search.",
            "tags": ["slack"],
            "platforms": ["slack"],
            "quality_status": "metadata_only",
            "quality_score": 55,
            "rank": 1.0,
            "similarity": 0.96,
        }

        self.assertEqual(tier_for_prompt(prompt, [candidate]), "hint")

    def test_no_similarity_never_full_routes(self):
        prompt = "create an excel report with formulas"
        candidate = {
            "name": "spreadsheet-reporter",
            "description": "Build spreadsheet reports with formulas and charts.",
            "tags": ["spreadsheet", "excel"],
            "platforms": [],
            "quality_status": "active",
            "quality_score": 90,
            "rank": 1.0,
        }

        self.assertEqual(tier_for_prompt(prompt, [candidate]), "hint")


if __name__ == "__main__":
    unittest.main()
