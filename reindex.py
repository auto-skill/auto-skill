"""Full-corpus reindex: ingest orphaned skills_library files and refresh
stale embeddings.

Two passes, both resumable and safe to run while the scraper/server is up
(writes go through the same localhost REST surface as the embed loop):

  1. Ingest: skills_library/index.json entries with no skills row get one
     (name/description/source/url from the index entry). They are inserted
     without an embedding, so pass 2 (or the server's own embed loop) picks
     them up.
  2. Refresh: every row whose stored embedding_text_hash differs from the
     hash of its *current* embed text (metadata + library content) is
     re-embedded. This catches rows embedded before their SKILL.md was
     harvested and any rows invalidated by build_embed_text changes.

Run:  python reindex.py
"""
import asyncio
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import httpx

from embeddings import LibraryContent, build_embed_text, embed_text_hash, embed_texts
from quality import evaluate_quality

BASE = Path(__file__).parent
DB_PATH = Path(os.getenv("LOCAL_DB_PATH", str(BASE / "local_skills.db")))
REST = "http://127.0.0.1:8000/rest/v1/skills?on_conflict=url"
HEADERS = {"Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"}
SCAN_PAGE = 5000
EMBED_BATCH = 128
UPSERT_CHUNK = 50


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _upsert(client: httpx.AsyncClient, rows: list[dict]) -> None:
    for i in range(0, len(rows), UPSERT_CHUNK):
        chunk = rows[i:i + UPSERT_CHUNK]
        for attempt in range(4):
            try:
                r = await client.post(REST, json=chunk, headers=HEADERS, timeout=120)
                if r.status_code in (200, 201, 204):
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(2 ** attempt)
        else:
            raise RuntimeError("upsert failed after retries; re-run to resume")


async def ingest_orphans(client: httpx.AsyncClient, library: LibraryContent) -> int:
    """Insert a skills row for every index.json entry the DB doesn't know."""
    index_path = BASE / "skills_library" / "index.json"
    idx = json.loads(index_path.read_text(encoding="utf-8"))
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        known = {u for (u,) in conn.execute("SELECT url FROM skills WHERE url IS NOT NULL")}
    finally:
        conn.close()

    orphans = []
    for url, e in idx.items():
        if url in known or not e.get("file"):
            continue
        row = {
            "name": (e.get("name") or url.rstrip("/").split("/")[-1])[:200],
            "description": e.get("description") or "",
            "source": e.get("source") or "library",
            "url": url,
        }
        row.update(evaluate_quality(row, library.get(url)))
        orphans.append(row)
    if orphans:
        await _upsert(client, orphans)
    return len(orphans)


def scan_stale(library: LibraryContent) -> list[str]:
    """Return ids of rows whose stored hash doesn't match their current
    embed text (or that have no embedding at all)."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    stale: list[str] = []
    try:
        offset = 0
        while True:
            rows = conn.execute(
                "SELECT id,url,name,source,description,tags,embedding_text_hash,"
                "       embedding IS NULL AS no_vec"
                "  FROM skills WHERE url IS NOT NULL "
                "   AND COALESCE(quality_status, 'active') = 'active' "
                " ORDER BY id LIMIT ? OFFSET ?",
                (SCAN_PAGE, offset),
            ).fetchall()
            if not rows:
                break
            for r in rows:
                d = dict(r)
                if d["no_vec"]:
                    stale.append(d["id"])
                    continue
                try:
                    d["tags"] = json.loads(d.get("tags") or "[]")
                except Exception:
                    d["tags"] = []
                text = build_embed_text(d, library.get(d["url"]))
                if embed_text_hash(text) != d["embedding_text_hash"]:
                    stale.append(d["id"])
            offset += SCAN_PAGE
            if offset % 50000 == 0:
                print(f"  scanned {offset} rows, {len(stale)} stale so far", flush=True)
    finally:
        conn.close()
    return stale


async def refresh(client: httpx.AsyncClient, library: LibraryContent, ids: list[str]) -> int:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    done = 0
    try:
        for i in range(0, len(ids), EMBED_BATCH):
            batch_ids = ids[i:i + EMBED_BATCH]
            marks = ",".join("?" for _ in batch_ids)
            rows = [dict(r) for r in conn.execute(
                f"SELECT id,url,name,source,description,tags FROM skills "
                f"WHERE id IN ({marks}) AND COALESCE(quality_status, 'active') = 'active'",
                batch_ids,
            )]
            for d in rows:
                try:
                    d["tags"] = json.loads(d.get("tags") or "[]")
                except Exception:
                    d["tags"] = []
            texts = [build_embed_text(d, library.get(d["url"] or "")) for d in rows]
            vectors = await asyncio.to_thread(embed_texts, texts, EMBED_BATCH)
            now = _now()
            payload = [
                {
                    "url": d["url"],
                    "name": d.get("name") or "",
                    "source": d.get("source") or "",
                    "embedding": vec,
                    "embedding_text_hash": embed_text_hash(text),
                    "embedded_at": now,
                }
                for d, text, vec in zip(rows, texts, vectors)
            ]
            await _upsert(client, payload)
            done += len(rows)
            if done % 2560 == 0:
                print(f"  re-embedded {done}/{len(ids)}", flush=True)
    finally:
        conn.close()
    return done


async def main() -> None:
    library = LibraryContent()
    async with httpx.AsyncClient() as client:
        n = await ingest_orphans(client, library)
        print(f"pass 1: ingested {n} orphaned library entries", flush=True)
        stale = scan_stale(library)
        print(f"pass 2: {len(stale)} rows need (re-)embedding", flush=True)
        done = await refresh(client, library, stale)
        print(f"done - {done} rows re-embedded", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
