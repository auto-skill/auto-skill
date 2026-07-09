from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import scraper
import worker


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_skips_embedding_after_failed_scrape(self) -> None:
        with (
            patch.object(worker, "start_new_scrape_run", new=AsyncMock(return_value="run-1")),
            patch.object(worker, "run_scrape", new=AsyncMock(return_value=False)),
            patch.object(worker, "embed_missing_skills", new=AsyncMock()) as embed_missing,
        ):
            await worker.run_once()

        embed_missing.assert_not_awaited()

    async def test_worker_embeds_after_successful_scrape(self) -> None:
        with (
            patch.object(worker, "start_new_scrape_run", new=AsyncMock(return_value="run-1")),
            patch.object(worker, "run_scrape", new=AsyncMock(return_value=True)),
            patch.object(worker, "embed_missing_skills", new=AsyncMock(return_value=3)) as embed_missing,
        ):
            await worker.run_once()

        embed_missing.assert_awaited_once()


class ScraperRunStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_scrape_returns_false_after_marking_run_error(self) -> None:
        with (
            patch.object(scraper.CrawlState, "load", side_effect=RuntimeError("boom")),
            patch.object(scraper, "supabase_patch", new=AsyncMock()) as patch_run,
        ):
            ok = await scraper.run_scrape("run-1")

        self.assertFalse(ok)
        patch_run.assert_awaited_once()
        args = patch_run.await_args.args
        self.assertEqual(args[1], "scrape_runs")
        self.assertEqual(args[2], {"id": "run-1"})
        self.assertEqual(args[3]["status"], "error")
        self.assertEqual(args[3]["skills_found"], 0)
        self.assertIn("boom", args[3]["error"])


if __name__ == "__main__":
    unittest.main()
