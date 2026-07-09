from __future__ import annotations

import json
import hashlib
import sqlite3
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

import launch_readiness


class LaunchReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "data").mkdir()
        (self.root / "skills_library" / "files").mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_db(self, *, active: bool = True, embedded: bool = True) -> None:
        self._write_db_at(self.root / "data" / "local_skills.db", active=active, embedded=embedded)

    def _write_db_at(self, path: Path, *, active: bool = True, embedded: bool = True) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute(
            """
            CREATE TABLE skills (
                id TEXT PRIMARY KEY,
                quality_status TEXT,
                embedding BLOB
            )
            """
        )
        conn.execute(
            "INSERT INTO skills (id, quality_status, embedding) VALUES (?, ?, ?)",
            ("skill-1", "active" if active else "rejected", b"x" if embedded else None),
        )
        conn.commit()
        conn.close()

    def _write_library(self) -> None:
        (self.root / "skills_library" / "index.json").write_text(json.dumps([{"id": "skill-1"}]), encoding="utf-8")
        (self.root / "skills_library" / "files" / "skill-1.md").write_text("# Skill\n", encoding="utf-8")

    def _write_backup(self, *, embedded: bool = True, include_markdown: bool = True) -> Path:
        backup_dir = self.root / "backup"
        backup_dir.mkdir()
        db_path = backup_dir / "local_skills.db"
        self._write_db_at(db_path, embedded=embedded)
        archive_path = backup_dir / "skills_library.tgz"
        source_library = self.root / "archive_source"
        (source_library / "files").mkdir(parents=True)
        (source_library / "index.json").write_text(json.dumps([{"id": "skill-1"}]), encoding="utf-8")
        if include_markdown:
            (source_library / "files" / "skill-1.md").write_text("# Skill\n", encoding="utf-8")
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(source_library / "index.json", arcname="index.json")
            archive.add(source_library / "files", arcname="files")

        files = []
        for path in (db_path, archive_path):
            files.append(
                {
                    "path": path.name,
                    "bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
        (backup_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "db_backup": "local_skills.db",
                    "library_archive": "skills_library.tgz",
                    "files": files,
                }
            ),
            encoding="utf-8",
        )
        return backup_dir

    def _write_backup_zip(self) -> Path:
        backup_dir = self._write_backup()
        zip_path = self.root / "seed.zip"
        with zipfile.ZipFile(zip_path, "w") as archive:
            for item in backup_dir.rglob("*"):
                if item.is_file():
                    archive.write(item, item.relative_to(backup_dir).as_posix())
        return zip_path

    def test_local_seed_passes_with_non_empty_embedded_db_and_library(self) -> None:
        self._write_db()
        self._write_library()

        checks = launch_readiness.check_local_seed(self.root)

        self.assertEqual([check.state for check in checks], ["pass", "pass"])

    def test_local_seed_fails_when_db_missing(self) -> None:
        self._write_library()

        checks = launch_readiness.check_local_seed(self.root)

        self.assertEqual(checks[0].state, "fail")
        self.assertIn("missing", checks[0].detail)

    def test_local_seed_fails_when_embeddings_missing(self) -> None:
        self._write_db(embedded=False)
        self._write_library()

        checks = launch_readiness.check_local_seed(self.root)

        self.assertEqual(checks[0].state, "fail")
        self.assertIn("embedded=0", checks[0].detail)

    def test_local_seed_fails_when_library_has_no_markdown(self) -> None:
        self._write_db()
        (self.root / "skills_library" / "index.json").write_text(json.dumps([{"id": "skill-1"}]), encoding="utf-8")

        checks = launch_readiness.check_local_seed(self.root)

        self.assertEqual(checks[1].state, "fail")
        self.assertIn("markdown_files=0", checks[1].detail)

    def test_loose_candidate_seed_passes_with_db_and_library(self) -> None:
        self._write_db()
        self._write_library()

        checks = launch_readiness.check_loose_seed(
            self.root / "data" / "local_skills.db",
            self.root / "skills_library",
        )

        self.assertEqual([check.state for check in checks], ["pass", "pass"])

    def test_backup_candidate_seed_passes_when_backup_is_non_empty(self) -> None:
        backup_dir = self._write_backup()

        checks = launch_readiness.check_backup_seed(backup_dir)

        self.assertEqual([check.state for check in checks], ["pass", "pass", "pass"])

    def test_backup_candidate_seed_fails_when_embeddings_missing(self) -> None:
        backup_dir = self._write_backup(embedded=False)

        checks = launch_readiness.check_backup_seed(backup_dir)

        self.assertEqual(checks[1].state, "fail")
        self.assertIn("embedded=0", checks[1].detail)

    def test_backup_candidate_seed_fails_when_archive_has_no_markdown(self) -> None:
        backup_dir = self._write_backup(include_markdown=False)

        checks = launch_readiness.check_backup_seed(backup_dir)

        self.assertEqual(checks[2].state, "fail")
        self.assertIn("markdown_files=0", checks[2].detail)

    def test_build_report_can_skip_local_seed_for_candidate_validation(self) -> None:
        self._write_backup()

        checks = launch_readiness.build_report(
            self.root,
            skip_live=True,
            skip_local_seed=True,
            skip_git=True,
            seed_backup_dir=self.root / "backup",
        )

        self.assertFalse(any(check.name == "local seed db" for check in checks))
        self.assertEqual(checks[0].name, "candidate backup")
        self.assertFalse(any(check.name == "git state" for check in checks))

    def test_backup_zip_candidate_seed_passes_when_zip_is_non_empty(self) -> None:
        zip_path = self._write_backup_zip()

        checks = launch_readiness.check_backup_zip_seed(zip_path)

        self.assertEqual([check.state for check in checks], ["pass", "pass", "pass", "pass"])
        self.assertEqual(checks[0].name, "candidate backup zip")

    def test_backup_zip_candidate_seed_rejects_unsafe_member(self) -> None:
        zip_path = self.root / "unsafe.zip"
        with zipfile.ZipFile(zip_path, "w") as archive:
            archive.writestr("../manifest.json", "{}")

        checks = launch_readiness.check_backup_zip_seed(zip_path)

        self.assertEqual(checks[0].state, "fail")
        self.assertIn("unsafe zip member", checks[0].detail)

    def test_recommended_actions_include_seed_and_dns_commands(self) -> None:
        checks = [
            launch_readiness.ReadinessCheck("local seed db", "fail", "missing data/local_skills.db"),
            launch_readiness.ReadinessCheck("public alpha", "fail", "runtime DB/library is empty"),
            launch_readiness.ReadinessCheck("public canonical", "fail", "getaddrinfo failed"),
            launch_readiness.ReadinessCheck("git state", "warn", "3 changed/untracked file(s)"),
        ]

        actions = launch_readiness.recommended_actions(checks)

        self.assertTrue(any("export-seed-packet.ps1" in action for action in actions))
        self.assertTrue(any("recover-host.ps1 -SeedBackupZip" in action for action in actions))
        self.assertTrue(any("api.auto-skill.com" in action for action in actions))
        self.assertTrue(any("commit" in action for action in actions))


if __name__ == "__main__":
    unittest.main()
