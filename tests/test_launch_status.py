from __future__ import annotations

import unittest

import launch_status


class LaunchStatusTests(unittest.TestCase):
    def test_classifies_cloudflare_1033_as_tunnel_down(self) -> None:
        state, detail = launch_status.classify(530, {"cloudflare_error": True, "error_code": 1033})

        self.assertEqual(state, "down")
        self.assertIn("Cloudflare tunnel", detail)

    def test_classifies_cloudflare_502_bad_gateway_as_origin_down(self) -> None:
        state, detail = launch_status.classify(502, {"title": "Error 502: Bad gateway"})

        self.assertEqual(state, "down")
        self.assertIn("API origin", detail)

    def test_classifies_route_response_as_ok(self) -> None:
        state, detail = launch_status.classify(200, {"tier": "hint"})

        self.assertEqual(state, "ok")
        self.assertEqual(detail, "route tier=hint")

    def test_classifies_empty_runtime_as_not_ready(self) -> None:
        state, detail = launch_status.classify(
            503,
            {"ok": False, "total_skills": 0, "active_skills": 0, "embedded_skills": 0},
        )

        self.assertEqual(state, "not_ready")
        self.assertIn("skill DB is empty", detail)

    def test_classifies_no_result_route_as_not_ready(self) -> None:
        state, detail = launch_status.classify(200, {"tier": "none", "candidates": []})

        self.assertEqual(state, "not_ready")
        self.assertIn("no candidates", detail)

    def test_next_action_points_530_to_recovery(self) -> None:
        probes = [
            launch_status.Probe(
                name="api healthz",
                method="GET",
                url="https://skills.example.com/healthz",
                status=530,
                state="down",
                detail="Cloudflare tunnel cannot reach the origin",
            )
        ]

        self.assertIn("recover-host.ps1", launch_status.next_action(probes))

    def test_next_action_points_502_to_recovery_and_diagnostics(self) -> None:
        probes = [
            launch_status.Probe(
                name="api healthz",
                method="GET",
                url="https://skills.example.com/healthz",
                status=502,
                state="down",
                detail="Cloudflare reached the tunnel but the API origin returned Bad Gateway",
            ),
            launch_status.Probe("mcp healthz", "GET", "https://mcp.example.com/healthz", 200, "ok", "ok=true"),
        ]

        action = launch_status.next_action(probes)

        self.assertIn("recover-host.ps1", action)
        self.assertIn("diagnose-host.ps1", action)

    def test_next_action_requires_launch_check_when_all_ok(self) -> None:
        probes = [
            launch_status.Probe("api healthz", "GET", "https://skills.example.com/healthz", 200, "ok", "ok=true"),
            launch_status.Probe("mcp healthz", "GET", "https://mcp.example.com/healthz", 200, "ok", "ok=true"),
        ]

        action = launch_status.next_action(probes)

        self.assertIn("launch_check.py", action)
        self.assertIn("live_smoke.py", action)

    def test_next_action_points_empty_runtime_to_seed_restore(self) -> None:
        probes = [
            launch_status.Probe("api healthz", "GET", "https://skills.example.com/healthz", 200, "ok", "ok=true"),
            launch_status.Probe(
                "api readyz",
                "GET",
                "https://skills.example.com/readyz",
                503,
                "not_ready",
                "origin is reachable but the skill DB is empty or not mounted",
            ),
            launch_status.Probe("api route", "POST", "https://skills.example.com/route", 200, "not_ready", "route found no candidates"),
        ]

        action = launch_status.next_action(probes)

        self.assertIn("Seed or restore local_skills.db", action)

    def test_next_action_points_dns_failures_to_domain_setup(self) -> None:
        probes = [
            launch_status.Probe(
                "api healthz",
                "GET",
                "https://api.auto-skill.com/healthz",
                None,
                "error",
                "<urlopen error [Errno 11001] getaddrinfo failed>",
            )
        ]

        action = launch_status.next_action(probes)

        self.assertIn("does not resolve", action)
        self.assertIn("DNS", action)

    def test_profiles_define_alpha_and_canonical_hosts(self) -> None:
        self.assertEqual(
            launch_status.PROFILES["alpha"],
            ("https://skills.avalahome.com", "https://mcp.avalahome.com/healthz"),
        )
        self.assertEqual(
            launch_status.PROFILES["canonical"],
            ("https://api.auto-skill.com", "https://mcp.auto-skill.com/healthz"),
        )


if __name__ == "__main__":
    unittest.main()
