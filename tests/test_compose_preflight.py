from __future__ import annotations

import contextlib
import io
import json
import sqlite3
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
            ".dockerignore",
            "deploy/Dockerfile",
            "deploy/litestream.yml",
            "deploy/backup-library.sh",
            "deploy/seed_runtime.py",
            "deploy/export-seed-packet.ps1",
            "requirements.txt",
            "scraper.py",
            "worker.py",
        ]:
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if path == ".dockerignore":
                target.write_text(
                    "\n".join(
                        [
                            ".git",
                            ".venv",
                            "__pycache__",
                            "*.pyc",
                            "*.log",
                            "local_skills.db",
                            "local_skills.db-wal",
                            "local_skills.db-shm",
                            "data",
                            "skills_library",
                            "content_blobs",
                            "eval-results",
                            "restore-drill",
                            "",
                        ]
                    ),
                    encoding="utf-8",
                )
            else:
                target.write_text("placeholder\n", encoding="utf-8")
        (self.root / "skills_library" / "files").mkdir()
        (self.root / "deploy" / "docker-compose.yml").write_text(
            """
services:
  api:
    environment:
      LOCAL_DB_PATH: /data/local_skills.db
      AUTO_START_SCRAPER: "0"
      AUTO_START_EMBEDDER: "0"
    command: uvicorn scraper:app --host 0.0.0.0 --port 8000
  worker:
    environment:
      LOCAL_DB_URL: http://api:8000
      AUTO_START_SCRAPER: "0"
      AUTO_START_EMBEDDER: "0"
    command: python worker.py
  cloudflared:
    command: tunnel run
  litestream:
    command: replicate
  library-backup:
    command: backup
""",
            encoding="utf-8",
        )

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

    def write_seed_db(self, *, rows: int = 1, embedded: int = 1) -> None:
        conn = sqlite3.connect(self.root / "data" / "local_skills.db")
        try:
            conn.execute(
                """
                CREATE TABLE skills (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    quality_status TEXT,
                    embedding TEXT
                )
                """
            )
            for i in range(rows):
                conn.execute(
                    "INSERT INTO skills (id, name, quality_status, embedding) VALUES (?, ?, ?, ?)",
                    (f"skill-{i}", f"Skill {i}", "active", "[0.1]" if i < embedded else None),
                )
            conn.commit()
        finally:
            conn.close()

    def write_seed_library(self, *, entries: int = 1, files: int = 1) -> None:
        index = [
            {"id": f"skill-{i}", "name": f"Skill {i}", "path": f"files/skill-{i}.md"}
            for i in range(entries)
        ]
        (self.root / "skills_library" / "index.json").write_text(json.dumps(index), encoding="utf-8")
        files_dir = self.root / "skills_library" / "files"
        files_dir.mkdir(exist_ok=True)
        for i in range(files):
            (files_dir / f"skill-{i}.md").write_text("## Workflow\nUse this skill.\n", encoding="utf-8")

    def test_passes_with_seeded_launch_inputs(self) -> None:
        self.write_seed_db()
        self.write_seed_library()
        self.write_env(
            "\n".join(
                [
                    "GITHUB_TOKEN=ghp_test",
                    "CLOUDFLARED_TOKEN=cloudflare-token",
                    "R2_ENDPOINT=https://abc.r2.cloudflarestorage.com",
                    "R2_BUCKET=autoskill-backups",
                    "R2_ACCESS_KEY_ID=access",
                    "R2_SECRET_ACCESS_KEY=secret",
                    "AUTO_SKILL_DASHBOARD_ORIGINS=https://auto-skill.com,https://www.auto-skill.com",
                    "STALE_SCRAPE_RUN_SECONDS=7200",
                    "LIBRARY_BACKUP_INTERVAL_SECONDS=86400",
                    "",
                ]
            )
        )

        exit_code, output = self.run_preflight()

        self.assertEqual(exit_code, 0, output)
        self.assertIn("[PASS] seed db", output)
        self.assertIn("total=1", output)
        self.assertIn("embedded=1", output)
        self.assertIn("[PASS] library index", output)
        self.assertIn("[PASS] dockerignore", output)
        self.assertIn("[PASS] env AUTO_SKILL_DASHBOARD_ORIGINS", output)
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

    def test_rejects_empty_seed_runtime(self) -> None:
        self.write_seed_db(rows=0, embedded=0)
        self.write_seed_library(entries=0, files=0)
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

        exit_code, output = self.run_preflight()

        self.assertEqual(exit_code, 1, output)
        self.assertIn("[FAIL] seed db", output)
        self.assertIn("total=0", output)
        self.assertIn("[FAIL] library index", output)
        self.assertIn("entries=0", output)

    def test_rejects_seed_db_without_embeddings(self) -> None:
        self.write_seed_db(rows=1, embedded=0)
        self.write_seed_library()
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

        exit_code, output = self.run_preflight()

        self.assertEqual(exit_code, 1, output)
        self.assertIn("embedded=0", output)
        self.assertIn("expected embedded >= 1", output)

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

    def test_rejects_unsafe_dashboard_origin_config(self) -> None:
        self.write_env(
            "\n".join(
                [
                    "GITHUB_TOKEN=ghp_test",
                    "CLOUDFLARED_TOKEN=cloudflare-token",
                    "R2_ENDPOINT=https://abc.r2.cloudflarestorage.com",
                    "R2_BUCKET=autoskill-backups",
                    "R2_ACCESS_KEY_ID=access",
                    "R2_SECRET_ACCESS_KEY=secret",
                    "AUTO_SKILL_DASHBOARD_ORIGINS=https://*.bad.test,https://auto-skill.com/dashboard",
                    "",
                ]
            )
        )

        exit_code, output = self.run_preflight("--skip-seed-checks")

        self.assertEqual(exit_code, 1, output)
        self.assertIn("[FAIL] env AUTO_SKILL_DASHBOARD_ORIGINS", output)
        self.assertIn("https://*.bad.test", output)
        self.assertIn("https://auto-skill.com/dashboard", output)

    def test_rejects_compose_that_starts_scraper_in_api(self) -> None:
        (self.root / "deploy" / "docker-compose.yml").write_text(
            """
services:
  api:
    environment:
      LOCAL_DB_PATH: /data/local_skills.db
      AUTO_START_SCRAPER: "1"
      AUTO_START_EMBEDDER: "0"
    command: uvicorn scraper:app --host 0.0.0.0 --port 8000
  worker:
    environment:
      LOCAL_DB_URL: http://api:8000
      AUTO_START_SCRAPER: "0"
      AUTO_START_EMBEDDER: "0"
    command: python worker.py
  cloudflared:
    command: tunnel run
  litestream:
    command: replicate
  library-backup:
    command: backup
""",
            encoding="utf-8",
        )
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

        self.assertEqual(exit_code, 1, output)
        self.assertIn("[FAIL] compose api no scraper loop", output)

    def test_rejects_dockerignore_that_would_bake_runtime_state_into_image(self) -> None:
        (self.root / ".dockerignore").write_text(
            "\n".join(
                [
                    ".git",
                    ".venv",
                    "__pycache__",
                    "*.pyc",
                    "*.log",
                    "local_skills.db",
                    "skills_library",
                    "",
                ]
            ),
            encoding="utf-8",
        )
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

        self.assertEqual(exit_code, 1, output)
        self.assertIn("[FAIL] dockerignore", output)
        self.assertIn("data", output)
        self.assertIn("content_blobs", output)


if __name__ == "__main__":
    unittest.main()
