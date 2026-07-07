"""Scheduled scraper/embed worker for deploys where the API is read-oriented.

The worker talks to the API's local PostgREST-compatible surface via
LOCAL_DB_URL, so SQLite writes are serialized through one service while the
public API can run without scraper/embed background loops.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import httpx

from recommender import embed_missing_skills
from scraper import SCRAPE_INTERVAL_SECONDS, run_scrape, start_new_scrape_run


async def run_once() -> None:
    run_id = await start_new_scrape_run()
    print(f"{datetime.now(timezone.utc).isoformat()} worker scrape {run_id} started", flush=True)
    await run_scrape(run_id)
    async with httpx.AsyncClient() as client:
        embedded = await embed_missing_skills(client)
    print(
        f"{datetime.now(timezone.utc).isoformat()} worker scrape {run_id} complete; embedded={embedded}",
        flush=True,
    )


async def main() -> None:
    once = os.getenv("WORKER_ONCE", "").lower() in {"1", "true", "yes"}
    while True:
        try:
            await run_once()
        except Exception as exc:
            print(f"{datetime.now(timezone.utc).isoformat()} worker error: {exc}", flush=True)
        if once:
            return
        await asyncio.sleep(SCRAPE_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
