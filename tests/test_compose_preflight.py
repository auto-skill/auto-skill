from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from deploy import compose_preflight


class ComposePreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "deploy").mkdir()
        (self.root / "data").mkdir()
        (self.root / "skills_library").mkdir()

        for path in [
            "deploy/docker-compose.yml",
            "deploy/Dockerfile",
            "deploy/litestream.yml",
            "deploy/backup-library.sh",
            "requirements.txt",
            "scraper.py",
            "worker.py",
        ]:
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("placeholder\n", encoding="utf-8")

    def write_env(self, content: str) -> Path:
        env_file = self.root / "deploy" / ".env"
        env_file.write_text(content, encoding="utf-8")
        return env_file

    def run_preflight(self, *extra_args: str) -> tuple[int, str]:
        stdout = io.StringIO()
        args = [
            "--repo-root",
            str(self.root),
            "--env-file",
            str(self.root / "deploy" / ".env"),
            "--compose-file",
            str(self.root / "deploy" / "docker-compose.yml"),
            "--skip-docker",
            *extra_args,
        ]
        with contextlib.redirect_stdout(stdout):
            exit_code = compose_preflight.main(args)
        return exit_code, stdout.getvalue()

    def test_passes_with_seeded_launch_inputs(self) -> None:
        (self.root / "data" / "local_skills.db").write_bytes(b"sqlite")
        (self.root / "skills_library" / "index.json").write_text("[]\n", encoding="utf-8")
        self.write_env(
            "\n".join(
                [
                    "GITHUB_TOKEN=ghp_test",
                    "CLOUDFLARED_TOKEN=cloudflare-token",
                    "R2_ENDPOINT=https://abc.r2.cloudflarestorage.com",
                    "R2_BUCKET=autoskill-backups",
                    "R2_ACCESS_KEY_ID=access",
                    "R2_SECRET_ACCESS_KEY=secret",
                    "STALE_SCRAPE_RUN_SECONDS=7200",
                    "LIBRARY_BACKUP_INTERVAL_SECONDS=86400",
                    "",
                ]
            )
        )

        exit_code, output = self.run_preflight()

        self.assertEqual(exit_code, 0, output)
        self.assertIn("[PASS] seed db", output)
        self.assertIn("compose_preflight: 0 failure(s)", output)

    def test_rejects_placeholders_and_missing_seed_data(self) -> None:
        self.write_env(
            "\n".join(
                [
                    "GITHUB_TOKEN=",
                    "CLOUDFLARED_TOKEN=<token>",
                    "R2_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com",
                    "R2_BUCKET=autoskill-backups",
                    "R2_ACCESS_KEY_ID=",
                    "R2_SECRET_ACCESS_KEY=",
                    "",
                ]
            )
        )

        exit_code, output = self.run_preflight()

        self.assertEqual(exit_code, 1, output)
        self.assertIn("[FAIL] env GITHUB_TOKEN", output)
        self.assertIn("[FAIL] seed db", output)
        self.assertIn("[FAIL] library index", output)

    def test_skip_seed_checks_allows_config_only_validation(self) -> None:
        self.write_env(
            "\n".join(
                [
                    "GITHUB_TOKEN=ghp_test",
                    "CLOUDFLARED_TOKEN=cloudflare-token",
                    "R2_ENDPOINT=https://abc.r2.cloudflarestorage.com",
                    "R2_BUCKET=autoskill-backups",
                    "R2_ACCESS_KEY_ID=access",
                    "R2_SECRET_ACCESS_KEY=secret",
                    "",
                ]
            )
        )

        exit_code, output = self.run_preflight("--skip-seed-checks")

        self.assertEqual(exit_code, 0, output)
        self.assertIn("[WARN] seed data: skipped", output)


if __name__ == "__main__":
    unittest.main()
