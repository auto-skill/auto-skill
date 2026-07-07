"""Backfill quality metadata for existing local skills rows.

This is intentionally non-destructive: it marks quality_status/reasons,
platforms, category, and same-run content-hash duplicates. Search and routing
then decide how conservative to be from those fields.

Run while the API is stopped or quiet:
    python backfill_quality.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile

from embeddings import LibraryContent
from local_store import DB_PATH, init_db
from quality import evaluate_quality

PAGE_SIZE = 1000


def _decode_json(value, default):
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def main() -> None:
    init_db()
    library = LibraryContent()
    index_path = library.library_dir / "index.json"
    try:
        raw_index = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        raw_index = {}
    index_dirty = False
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    seen_hashes: dict[str, str] = {}
    updated = 0
    duplicates = 0
    try:
        offset = 0
        while True:
            rows = conn.execute(
                "SELECT id,url,name,source,description,tags,raw "
                "FROM skills ORDER BY id LIMIT ? OFFSET ?",
                (PAGE_SIZE, offset),
            ).fetchall()
            if not rows:
                break

            for row in rows:
                skill = dict(row)
                skill["tags"] = _decode_json(skill.get("tags"), [])
                skill["raw"] = _decode_json(skill.get("raw"), {})
                url = skill.get("url") or ""
                content = library.get(url)
                quality = evaluate_quality(skill, content)
                chash = quality.get("content_hash")
                entry = raw_index.get(url)
                if content and chash and isinstance(entry, dict) and entry.get("content_hash") != chash:
                    entry["content_hash"] = chash
                    index_dirty = True
                if quality.get("quality_status") == "active" and chash:
                    canonical = seen_hashes.get(chash)
                    if canonical:
                        quality["quality_status"] = "duplicate"
                        reasons = set(quality.get("quality_reasons") or [])
                        reasons.add("duplicate-content")
                        quality["quality_reasons"] = sorted(reasons)
                        quality["canonical_id"] = canonical
                        duplicates += 1
                    else:
                        seen_hashes[chash] = skill["id"]

                conn.execute(
                    "UPDATE skills SET content_hash=?, canonical_id=?, quality_status=?, "
                    "quality_reasons=?, quality_score=?, platforms=?, category=? WHERE id=?",
                    (
                        quality.get("content_hash") or None,
                        quality.get("canonical_id"),
                        quality.get("quality_status"),
                        json.dumps(quality.get("quality_reasons") or []),
                        quality.get("quality_score") or 0,
                        json.dumps(quality.get("platforms") or []),
                        quality.get("category"),
                        skill["id"],
                    ),
                )
                updated += 1

            conn.commit()
            offset += PAGE_SIZE
            print(f"quality-backfill: {updated} rows, {duplicates} duplicates", flush=True)

        if index_dirty:
            fd, tmp = tempfile.mkstemp(dir=str(index_path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(raw_index, f, indent=2)
                os.replace(tmp, index_path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
