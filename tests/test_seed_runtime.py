from __future__ import annotations

import json
import hashlib
import sqlite3
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from deploy import seed_runtime


class SeedRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.target = self.root / "target"
        self.source.mkdir()
        self.target.mkdir()

    def write_seed_db(self, path: Path, *, rows: int = 1, embedded: int = 1) -> None:
        conn = sqlite3.connect(path)
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

    def write_library(self, path: Path, *, entries: int = 1, files: int = 1) -> None:
        files_dir = path / "files"
        files_dir.mkdir(parents=True)
        index = [
            {"id": f"skill-{i}", "name": f"Skill {i}", "path": f"files/skill-{i}.md"}
            for i in range(entries)
        ]
        (path / "index.json").write_text(json.dumps(index), encoding="utf-8")
        for i in range(files):
            (files_dir / f"skill-{i}.md").write_text("## Workflow\nUse it.\n", encoding="utf-8")

    def write_backup_dir(self, path: Path) -> Path:
        path.mkdir(parents=True)
        db = path / "local_skills.db"
        library_source = self.source / "library_archive_source"
        archive = path / "skills_library.tgz"
        self.write_seed_db(db)
        self.write_library(library_source)
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(library_source / "index.json", arcname="index.json")
            tar.add(library_source / "files", arcname="files")
        files = []
        for item in (db, archive):
            files.append(
                {
                    "path": item.name,
                    "bytes": item.stat().st_size,
                    "sha256": hashlib.sha256(item.read_bytes()).hexdigest(),
                }
            )
        (path / "manifest.json").write_text(
            json.dumps({"db_backup": db.name, "library_archive": archive.name, "files": files}),
            encoding="utf-8",
        )
        return path

    def write_backup_zip(self, path: Path) -> Path:
        backup_dir = self.write_backup_dir(self.source / "backup")
        with zipfile.ZipFile(path, "w") as archive:
            for item in backup_dir.rglob("*"):
                if item.is_file():
                    archive.write(item, item.relative_to(backup_dir).as_posix())
        return path

    def test_seeds_runtime_from_loose_db_and_library(self) -> None:
        db = self.source / "local_skills.db"
        library = self.source / "skills_library"
        self.write_seed_db(db)
        self.write_library(library)

        code = seed_runtime.main(
            [
                "--repo-root",
                str(self.target),
                "--db-path",
                str(db),
                "--library-dir",
                str(library),
            ]
        )

        self.assertEqual(code, 0)
        self.assertTrue((self.target / "data" / "local_skills.db").exists())
        self.assertTrue((self.target / "skills_library" / "index.json").exists())
        self.assertTrue((self.target / "skills_library" / "files" / "skill-0.md").exists())

    def test_refuses_to_replace_existing_runtime_without_force(self) -> None:
        db = self.source / "local_skills.db"
        library = self.source / "skills_library"
        self.write_seed_db(db)
        self.write_library(library)
        (self.target / "data").mkdir()
        (self.target / "data" / "local_skills.db").write_text("existing", encoding="utf-8")

        code = seed_runtime.main(
            [
                "--repo-root",
                str(self.target),
                "--db-path",
                str(db),
                "--library-dir",
                str(library),
            ]
        )

        self.assertEqual(code, 1)

    def test_rejects_empty_seed_runtime(self) -> None:
        db = self.source / "local_skills.db"
        library = self.source / "skills_library"
        self.write_seed_db(db, rows=0, embedded=0)
        self.write_library(library, entries=0, files=0)

        code = seed_runtime.main(
            [
                "--repo-root",
                str(self.target),
                "--db-path",
                str(db),
                "--library-dir",
                str(library),
            ]
        )

        self.assertEqual(code, 1)
        self.assertFalse((self.target / "data" / "local_skills.db").exists())

    def test_seeds_runtime_from_backup_zip(self) -> None:
        backup_zip = self.write_backup_zip(self.source / "seed.zip")

        code = seed_runtime.main(
            [
                "--repo-root",
                str(self.target),
                "--backup-zip",
                str(backup_zip),
            ]
        )

        self.assertEqual(code, 0)
        self.assertTrue((self.target / "data" / "local_skills.db").exists())
        self.assertTrue((self.target / "skills_library" / "index.json").exists())
        self.assertTrue((self.target / "skills_library" / "files" / "skill-0.md").exists())

    def test_rejects_unsafe_backup_zip_members(self) -> None:
        backup_zip = self.source / "unsafe.zip"
        with zipfile.ZipFile(backup_zip, "w") as archive:
            archive.writestr("../manifest.json", "{}")

        code = seed_runtime.main(
            [
                "--repo-root",
                str(self.target),
                "--backup-zip",
                str(backup_zip),
            ]
        )

        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
