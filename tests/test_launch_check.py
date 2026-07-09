import tempfile
import unittest
from pathlib import Path

import launch_check


class LaunchCheckDiagnosticsTests(unittest.TestCase):
    def test_bad_gateway_detail_points_to_recovery(self) -> None:
        detail = launch_check._bad_gateway_detail(502, {"title": "Error 502: Bad gateway"})

        self.assertIsNotNone(detail)
        self.assertIn("recover-host.ps1", detail)
        self.assertIn("diagnose-host.ps1", detail)

    def test_bad_gateway_detail_ignores_non_502(self) -> None:
        self.assertIsNone(launch_check._bad_gateway_detail(503, {"title": "Bad gateway"}))

    def test_empty_runtime_detail_points_to_seed_runtime(self) -> None:
        detail = launch_check._empty_runtime_detail(
            {"ok": False, "total_skills": 0, "active_skills": 0, "embedded_skills": 0}
        )

        self.assertIsNotNone(detail)
        self.assertIn("deploy\\seed_runtime.py", detail)
        self.assertIn("total_skills=0", detail)

    def test_empty_runtime_detail_ignores_partial_readiness_failures(self) -> None:
        detail = launch_check._empty_runtime_detail(
            {"ok": False, "total_skills": 12, "active_skills": 12, "embedded_skills": 0}
        )

        self.assertIsNone(detail)

    def test_no_candidate_route_detail_names_unseeded_runtime(self) -> None:
        detail = launch_check._no_candidate_route_detail(
            {"tier": "none", "candidates": [], "score_debug": {"reason": "no-results"}}
        )

        self.assertIsNotNone(detail)
        self.assertIn("no candidates", detail)
        self.assertIn("runtime DB/library", detail)


class LaunchCheckEnvTests(unittest.TestCase):
    def write_env(self, text: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / ".env"
        path.write_text(text, encoding="utf-8")
        return path

    def test_env_check_accepts_dashboard_origin_allowlist(self) -> None:
        reporter = launch_check.Reporter()
        env_file = self.write_env(
            "\n".join(
                [
                    "CLOUDFLARED_TOKEN=cloudflare-token",
                    "R2_ENDPOINT=https://abc.r2.cloudflarestorage.com",
                    "R2_BUCKET=autoskill-backups",
                    "R2_ACCESS_KEY_ID=access",
                    "R2_SECRET_ACCESS_KEY=secret",
                    "GITHUB_TOKEN=ghp_test",
                    "AUTO_SKILL_DASHBOARD_ORIGINS=https://auto-skill.com,https://www.auto-skill.com",
                    "",
                ]
            )
        )

        launch_check.check_env(reporter, env_file)

        self.assertEqual(reporter.failures, 0)

    def test_env_check_rejects_unsafe_dashboard_origins(self) -> None:
        reporter = launch_check.Reporter()
        env_file = self.write_env(
            "\n".join(
                [
                    "CLOUDFLARED_TOKEN=cloudflare-token",
                    "R2_ENDPOINT=https://abc.r2.cloudflarestorage.com",
                    "R2_BUCKET=autoskill-backups",
                    "R2_ACCESS_KEY_ID=access",
                    "R2_SECRET_ACCESS_KEY=secret",
                    "GITHUB_TOKEN=ghp_test",
                    "AUTO_SKILL_DASHBOARD_ORIGINS=https://*.bad.test,https://auto-skill.com/dashboard",
                    "",
                ]
            )
        )

        launch_check.check_env(reporter, env_file)

        self.assertEqual(reporter.failures, 1)

    def test_env_check_warns_when_dashboard_origins_are_unset(self) -> None:
        reporter = launch_check.Reporter()
        env_file = self.write_env(
            "\n".join(
                [
                    "CLOUDFLARED_TOKEN=cloudflare-token",
                    "R2_ENDPOINT=https://abc.r2.cloudflarestorage.com",
                    "R2_BUCKET=autoskill-backups",
                    "R2_ACCESS_KEY_ID=access",
                    "R2_SECRET_ACCESS_KEY=secret",
                    "GITHUB_TOKEN=ghp_test",
                    "",
                ]
            )
        )

        launch_check.check_env(reporter, env_file)

        self.assertEqual(reporter.failures, 0)
        self.assertEqual(reporter.warnings, 1)


class LaunchCheckRouteMetricsTests(unittest.TestCase):
    def test_route_metrics_summary_passes_when_recent_routes_are_under_budget(self) -> None:
        reporter = launch_check.Reporter()

        launch_check._check_route_metrics_summary(
            reporter,
            "route metrics",
            {
                "ok": True,
                "total": 4,
                "budgets": {
                    "latency_ms": 1500,
                    "skill_find_ms": 1200,
                    "injected_tokens": 3000,
                    "response_tokens": 3500,
                },
                "budget_breaches": {
                    "any": 0,
                    "latency_ms": 0,
                    "skill_find_ms": 0,
                    "injected_tokens": 0,
                    "response_tokens": 0,
                },
                "p95_latency_ms": 240,
                "p95_skill_find_ms": 110,
                "p95_injected_tokens": 900,
                "p95_response_tokens": 1400,
            },
            1500,
            1200,
            3000,
            3500,
        )

        self.assertEqual(reporter.failures, 0)
        self.assertEqual(reporter.warnings, 0)

    def test_route_metrics_summary_fails_on_budget_breaches(self) -> None:
        reporter = launch_check.Reporter()

        launch_check._check_route_metrics_summary(
            reporter,
            "route metrics",
            {
                "ok": True,
                "total": 2,
                "budget_breaches": {
                    "any": 1,
                    "latency_ms": 0,
                    "skill_find_ms": 1,
                    "injected_tokens": 0,
                    "response_tokens": 0,
                },
            },
            1500,
            1200,
            3000,
            3500,
        )

        self.assertEqual(reporter.failures, 1)
        self.assertEqual(reporter.warnings, 0)

    def test_route_metrics_summary_warns_when_no_recent_routes_exist(self) -> None:
        reporter = launch_check.Reporter()

        launch_check._check_route_metrics_summary(
            reporter,
            "route metrics",
            {"ok": True, "total": 0, "budget_breaches": {"any": 0}},
            1500,
            1200,
            3000,
            3500,
        )

        self.assertEqual(reporter.failures, 0)
        self.assertEqual(reporter.warnings, 1)


if __name__ == "__main__":
    unittest.main()
