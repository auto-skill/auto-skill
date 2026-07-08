from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

from pack_content_blobs import pack_content_blobs
from quality import content_hash


class ContentBlobPackTests(unittest.TestCase):
    def test_pack_deduplicates_and_compresses_by_content_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = root / "skills_library"
            files = library / "files"
            output = root / "content_blobs"
            files.mkdir(parents=True)

            content = "---\nname: spreadsheet\n---\n\n## Workflow\n\nUse when building spreadsheet reports.\n"
            duplicate = "---\nname: spreadsheet\n---\n\n## Workflow\n\nUse when building spreadsheet reports.\n"
            other = "---\nname: docs\n---\n\n## Workflow\n\nUse when writing documentation.\n"
            (files / "one.md").write_text(content, encoding="utf-8")
            (files / "two.md").write_text(duplicate, encoding="utf-8")
            (files / "three.md").write_text(other, encoding="utf-8")

            first_hash = content_hash(content)
            second_hash = content_hash(other)
            (library / "index.json").write_text(
                json.dumps(
                    {
                        "https://example.com/one": {"file": "one.md", "content_hash": first_hash},
                        "https://example.com/two": {"file": "two.md", "content_hash": first_hash},
                        "https://example.com/three": {"file": "three.md", "content_hash": second_hash},
                    }
                ),
                encoding="utf-8",
            )

            manifest = pack_content_blobs(library, output)

            self.assertEqual(manifest["entries"], 3)
            self.assertEqual(manifest["unique_blobs"], 2)
            self.assertEqual(manifest["missing_files"], 0)
            self.assertEqual(manifest["blobs"][first_hash]["count"], 2)

            blob_path = output / manifest["blobs"][first_hash]["path"]
            self.assertTrue(blob_path.exists())
            self.assertEqual(gzip.decompress(blob_path.read_bytes()).decode("utf-8"), content)

    def test_pack_accepts_bom_prefixed_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = root / "skills_library"
            files = library / "files"
            output = root / "content_blobs"
            files.mkdir(parents=True)

            content = "---\nname: demo\n---\n\n## Workflow\n\nUse when smoke testing backups.\n"
            (files / "demo.md").write_text(content, encoding="utf-8")
            (library / "index.json").write_text(
                json.dumps({"https://example.com/demo": {"file": "demo.md"}}),
                encoding="utf-8-sig",
            )

            manifest = pack_content_blobs(library, output)

            self.assertEqual(manifest["entries"], 1)
            self.assertEqual(manifest["unique_blobs"], 1)
            self.assertEqual(manifest["missing_files"], 0)


if __name__ == "__main__":
    unittest.main()
