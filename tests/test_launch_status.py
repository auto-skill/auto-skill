from __future__ import annotations

import unittest

import launch_status


class LaunchStatusTests(unittest.TestCase):
    def test_classifies_cloudflare_1033_as_tunnel_down(self) -> None:
        state, detail = launch_status.classify(530, {"cloudflare_error": True, "error_code": 1033})

        self.assertEqual(state, "down")
        self.assertIn("Cloudflare tunnel", detail)

    def test_classifies_route_response_as_ok(self) -> None:
        state, detail = launch_status.classify(200, {"tier": "hint"})

        self.assertEqual(state, "ok")
        self.assertEqual(detail, "route tier=hint")

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

    def test_next_action_requires_launch_check_when_all_ok(self) -> None:
        probes = [
            launch_status.Probe("api healthz", "GET", "https://skills.example.com/healthz", 200, "ok", "ok=true"),
            launch_status.Probe("mcp healthz", "GET", "https://mcp.example.com/healthz", 200, "ok", "ok=true"),
        ]

        action = launch_status.next_action(probes)

        self.assertIn("launch_check.py", action)
        self.assertIn("live_smoke.py", action)


if __name__ == "__main__":
    unittest.main()
