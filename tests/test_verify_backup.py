from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path

from deploy.verify_backup import verify_backup_dir


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class VerifyBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.backup = Path(self.tmp.name) / "20260708T200000Z"
        self.backup.mkdir()

        db_path = self.backup / "local_skills.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE skills (id TEXT PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO skills VALUES ('skill-1', 'spreadsheet')")
        conn.commit()
        conn.close()

        library_root = Path(self.tmp.name) / "library"
        library_root.mkdir()
        (library_root / "index.json").write_text("{}\n", encoding="utf-8")
        archive_path = self.backup / "skills_library.tgz"
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(library_root / "index.json", arcname="index.json")

        blob_dir = self.backup / "content_blobs" / "aa"
        blob_dir.mkdir(parents=True)
        blob_path = blob_dir / ("a" * 64 + ".md.gz")
        blob_path.write_bytes(gzip.compress(b"skill body", mtime=0))
        blob_manifest = self.backup / "content_blobs" / "manifest.json"
        blob_manifest.write_text(
            json.dumps(
                {
                    "entries": 1,
                    "unique_blobs": 1,
                    "missing_files": 0,
                    "blobs": {
                        "a" * 64: {
                            "path": "aa/" + "a" * 64 + ".md.gz",
                            "bytes": 10,
                            "compressed_bytes": blob_path.stat().st_size,
                            "urls": ["https://example.com/skill"],
                            "count": 1,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        files = []
        for path in [db_path, archive_path, blob_path, blob_manifest]:
            files.append(
                {
                    "path": path.relative_to(self.backup).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
            )
        (self.backup / "manifest.json").write_text(
            json.dumps(
                {
                    "created_at": self.backup.name,
                    "db_backup": "local_skills.db",
                    "library_archive": "skills_library.tgz",
                    "content_blobs_packed": True,
                    "files": files,
                }
            ),
            encoding="utf-8",
        )

    def test_verifies_complete_backup(self) -> None:
        checks = verify_backup_dir(self.backup)

        failures = [check for check in checks if check.level == "FAIL"]
        self.assertEqual(failures, [])
        self.assertTrue(any(check.name == "sqlite integrity" and check.level == "PASS" for check in checks))
        self.assertTrue(any(check.name == "content blobs" and check.level == "PASS" for check in checks))

    def test_fails_when_file_hash_changes(self) -> None:
        (self.backup / "local_skills.db").write_bytes(b"not sqlite anymore")

        checks = verify_backup_dir(self.backup)

        failures = [check for check in checks if check.level == "FAIL"]
        self.assertTrue(any(check.name == "file local_skills.db" for check in failures))

    def test_accepts_bom_prefixed_manifest(self) -> None:
        manifest_path = self.backup / "manifest.json"
        original = manifest_path.read_bytes()
        manifest_path.write_bytes(b"\xef\xbb\xbf" + original)

        checks = verify_backup_dir(self.backup)

        failures = [check for check in checks if check.level == "FAIL"]
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
