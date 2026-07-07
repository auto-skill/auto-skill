"""One-time (and resumable) embedding backfill for the skills table.

Pages through rows where embedding is null, builds each skill's embed text
(metadata + local .md content when we have it), embeds locally with the same
gte-small weights the Supabase edge runtime uses, and upserts vectors back.
Safe to interrupt and re-run: the `embedding is null` filter is the checkpoint.

Run:  python backfill_embeddings.py
"""
import asyncio
from datetime import datetime, timezone

import httpx

from embeddings import LibraryContent, build_embed_text, embed_text_hash, embed_texts
from scraper import HEADERS, SUPABASE_URL, supabase_post

PAGE_SIZE = 500
EMBED_BATCH = 128
UPSERT_CHUNK = 50


async def fetch_unembedded(client: httpx.AsyncClient) -> list[dict]:
    r = await client.get(
        f"{SUPABASE_URL}/rest/v1/skills",
        params={
            "select": "id,url,name,source,description,tags",
            "embedding": "is.null",
            "url": "not.is.null",
            "quality_status": "eq.active",
            # Descending: the scraper's embed loop drains ascending, so a
            # concurrent backfill works the other end instead of racing it.
            "order": "id.desc",
            "limit": str(PAGE_SIZE),
        },
        headers=HEADERS,
        timeout=90,
    )
    r.raise_for_status()
    return r.json()


async def main():
    library = LibraryContent()
    total = 0
    # Vector upserts pay per-row HNSW maintenance; the httpx default 5s read
    # timeout is far too tight for 50-row chunks.
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15)) as client:
        while True:
            rows = await fetch_unembedded(client)
            if not rows:
                break

            texts = [build_embed_text(row, library.get(row.get("url") or "")) for row in rows]
            vectors = await asyncio.to_thread(embed_texts, texts, EMBED_BATCH)

            now = datetime.now(timezone.utc).isoformat()
            payload = [
                {
                    "url": row["url"],
                    "name": row.get("name") or "",
                    "source": row.get("source") or "",
                    "embedding": vec,
                    "embedding_text_hash": embed_text_hash(text),
                    "embedded_at": now,
                }
                for row, text, vec in zip(rows, texts, vectors)
            ]
            for i in range(0, len(payload), UPSERT_CHUNK):
                chunk = payload[i:i + UPSERT_CHUNK]
                for attempt in range(4):
                    try:
                        r = await supabase_post(client, "skills", chunk, on_conflict="url")
                        if r.status_code in (200, 201):
                            break
                        detail = f"{r.status_code}: {r.text[:200]}"
                    except httpx.HTTPError as e:
                        detail = repr(e)
                    print(f"upsert attempt {attempt + 1}/4 failed ({detail})", flush=True)
                    await asyncio.sleep(2 ** attempt)
                else:
                    print("giving up after retries; re-run to resume", flush=True)
                    return

            total += len(rows)
            print(f"embedded {total} skills...", flush=True)

    print(f"done - {total} skills embedded this run")


if __name__ == "__main__":
    asyncio.run(main())
