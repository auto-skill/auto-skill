from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import local_store
import scraper


VALID_SKILL = """---
name: spreadsheet-reporter
description: Build spreadsheet reports with formulas and charts.
---

## Workflow

Use when the user needs an Excel or spreadsheet report with formulas, charts,
tables, and repeatable formatting. Inspect the source data, create a workbook,
add formulas, verify calculations, add charts, and explain the generated file.
Always validate sheet names, formulas, and chart ranges before returning output.
"""


class ApiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_db_path = local_store.DB_PATH
        self.db_path = Path(self.tmp.name) / "local_skills.db"
        local_store.DB_PATH = self.db_path
        scraper.store.DB_PATH = self.db_path
        local_store.init_db()
        self.client = TestClient(scraper.app)

    def tearDown(self) -> None:
        local_store.DB_PATH = self.old_db_path
        scraper.store.DB_PATH = self.old_db_path

    def _insert_skill(self, *, active: bool = True, embedded: bool = True) -> None:
        status = "active" if active else "rejected"
        embedding = local_store.pack_embedding([1.0] + [0.0] * 383) if embedded else None
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO skills (
                    id, name, description, source, url, risk_score, quality_status,
                    quality_score, embedding
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "skill-1",
                    "spreadsheet-reporter",
                    "Build spreadsheet reports with formulas and charts.",
                    "github_skill_file",
                    "https://example.com/spreadsheet",
                    0,
                    status,
                    90,
                    embedding,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def test_healthz_identifies_current_api(self) -> None:
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["service"], "auto-skill-api")
        self.assertEqual(body["api_version"], scraper.API_VERSION)

    def test_readyz_requires_active_embedded_rows(self) -> None:
        response = self.client.get("/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["ok"])

        self._insert_skill(active=True, embedded=False)
        response = self.client.get("/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["embedded_skills"], 0)

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE skills SET embedding=? WHERE id='skill-1'",
                (local_store.pack_embedding([1.0] + [0.0] * 383),),
            )
            conn.commit()
        finally:
            conn.close()

        response = self.client.get("/readyz")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["active_skills"], 1)
        self.assertEqual(body["embedded_skills"], 1)

    def test_public_guard_allows_readiness_and_route_but_blocks_writes(self) -> None:
        self.assertEqual(self.client.get("/readyz", headers={"x-forwarded-for": "203.0.113.10"}).status_code, 503)
        self.assertNotEqual(
            self.client.post("/route", json={"task": ""}, headers={"x-forwarded-for": "203.0.113.10"}).status_code,
            403,
        )
        blocked = self.client.post("/scrape", headers={"x-forwarded-for": "203.0.113.10"})
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(blocked.json()["error"], "read-only public API")

    def test_route_returns_full_with_inline_content(self) -> None:
        candidate = {
            "id": "skill-1",
            "name": "spreadsheet-reporter",
            "description": "Build spreadsheet reports with formulas and charts.",
            "source": "github_skill_file",
            "url": "https://example.com/spreadsheet",
            "risk_score": 0,
            "quality_status": "active",
            "quality_score": 90,
            "rank": 1.0,
            "similarity": 0.95,
        }

        async def fake_retrieve(client, query, limit):
            del client, query, limit
            return [candidate]

        class FakeLibrary:
            def get(self, url: str) -> str:
                self_url = "https://example.com/spreadsheet"
                return VALID_SKILL if url == self_url else ""

        with patch("recommender.retrieve_skills", fake_retrieve), patch("recommender.LibraryContent", FakeLibrary):
            response = self.client.post("/route", json={"task": "create an excel report with formulas"})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["tier"], "full")
        self.assertEqual(body["skill"]["name"], "spreadsheet-reporter")
        self.assertIn("validate sheet names", body["content"])
        self.assertTrue(body["content_url"].startswith("/content/"))
        self.assertEqual(body["score_debug"]["quality_status"], "active")
        metrics = body["score_debug"]["metrics"]
        self.assertGreaterEqual(metrics["latency_ms"], 0)
        self.assertGreaterEqual(metrics["retrieval_ms"], 0)
        self.assertGreater(metrics["content_tokens"], 0)
        self.assertGreater(metrics["response_tokens"], metrics["hint_tokens"])

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            event = conn.execute("SELECT * FROM route_events ORDER BY created_at DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(event)
        self.assertEqual(event["tier"], "full")
        self.assertEqual(event["skill_name"], "spreadsheet-reporter")
        self.assertGreater(event["response_tokens"], 0)

    def test_route_caps_platform_trap_to_hint(self) -> None:
        candidate = {
            "id": "skill-1",
            "name": "sales-landingi",
            "description": "Landingi platform help for landing pages, custom domains, leads, and CRM sync.",
            "source": "github_skill_file",
            "url": "https://example.com/landingi",
            "risk_score": 0,
            "quality_status": "active",
            "quality_score": 90,
            "platforms": ["landingi"],
            "rank": 1.0,
            "similarity": 0.95,
        }

        async def fake_retrieve(client, query, limit):
            del client, query, limit
            return [candidate]

        with patch("recommender.retrieve_skills", fake_retrieve):
            response = self.client.post("/route", json={"task": "build a landing page for an AI automation agency"})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["tier"], "hint")
        self.assertIsNone(body["content"])
        self.assertTrue(body["score_debug"]["platform_mismatch"])
        self.assertEqual(body["score_debug"]["metrics"]["content_tokens"], 0)

    def test_route_metrics_summarizes_recent_events(self) -> None:
        candidate = {
            "id": "skill-1",
            "name": "spreadsheet-reporter",
            "description": "Build spreadsheet reports with formulas and charts.",
            "source": "github_skill_file",
            "url": "https://example.com/spreadsheet",
            "risk_score": 0,
            "quality_status": "metadata_only",
            "quality_score": 55,
            "rank": 1.0,
            "similarity": 0.95,
        }

        async def fake_retrieve(client, query, limit):
            del client, query, limit
            return [candidate]

        with patch("recommender.retrieve_skills", fake_retrieve):
            response = self.client.post(
                "/route",
                json={
                    "task": "create an excel report with formulas",
                    "client": "test-client",
                    "client_version": "0.1",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["tier"], "hint")

        metrics = self.client.get("/route-metrics").json()
        self.assertTrue(metrics["ok"])
        self.assertGreaterEqual(metrics["total"], 1)
        self.assertGreaterEqual(metrics["tiers"]["hint"], 1)
        self.assertGreaterEqual(metrics["avg_response_tokens"], 1)


if __name__ == "__main__":
    unittest.main()
