from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy import compose_launch


class ComposeLaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "deploy").mkdir()
        (self.root / "deploy" / "compose_preflight.py").write_text("placeholder\n", encoding="utf-8")
        (self.root / "deploy" / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (self.root / "deploy" / ".env").write_text("GITHUB_TOKEN=token\n", encoding="utf-8")
        (self.root / "launch_check.py").write_text("placeholder\n", encoding="utf-8")

    def test_launch_runs_preflight_compose_waits_and_launch_check(self) -> None:
        commands: list[list[str]] = []

        def fake_run(cmd: list[str], cwd: Path, *, timeout: int = 300) -> None:
            commands.append(cmd)

        with (
            patch.object(compose_launch, "run", side_effect=fake_run),
            patch.object(compose_launch, "wait_json_ok") as wait_json_ok,
        ):
            code = compose_launch.main(
                [
                    "--repo-root",
                    str(self.root),
                    "--wait-seconds",
                    "9",
                ]
            )

        self.assertEqual(code, 0)
        self.assertIn("compose_preflight.py", commands[0][1])
        self.assertEqual(commands[1][:4], ["docker", "compose", "--env-file", str((self.root / "deploy" / ".env").resolve())])
        self.assertIn("--build", commands[1])
        self.assertIn("launch_check.py", commands[2][1])
        self.assertEqual(wait_json_ok.call_count, 2)
        wait_json_ok.assert_any_call("api healthz", "http://127.0.0.1:8000/healthz", 9)
        wait_json_ok.assert_any_call("api readyz", "http://127.0.0.1:8000/readyz", 9)

    def test_launch_can_skip_build_seed_checks_and_launch_check(self) -> None:
        commands: list[list[str]] = []

        def fake_run(cmd: list[str], cwd: Path, *, timeout: int = 300) -> None:
            commands.append(cmd)

        with (
            patch.object(compose_launch, "run", side_effect=fake_run),
            patch.object(compose_launch, "wait_json_ok"),
        ):
            code = compose_launch.main(
                [
                    "--repo-root",
                    str(self.root),
                    "--skip-build",
                    "--skip-seed-checks",
                    "--skip-launch-check",
                ]
            )

        self.assertEqual(code, 0)
        self.assertIn("--skip-seed-checks", commands[0])
        self.assertNotIn("--build", commands[1])
        self.assertEqual(len(commands), 2)


if __name__ == "__main__":
    unittest.main()
