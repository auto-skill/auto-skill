from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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


def _registered_routes(app):
    for route in app.routes:
        router = getattr(route, "original_router", None)
        if router:
            yield from router.routes
        else:
            yield route


def _route_example(path_format: str) -> str:
    examples = {
        "/content/{hash_value}": "/content/" + "0" * 64,
        "/rest/v1/{table}": "/rest/v1/skills",
        "/library/files/{path}": "/library/files/example.md",
    }
    return examples.get(path_format, path_format)


def _route_json_body(method: str, path_format: str) -> dict | None:
    if method not in {"POST", "PATCH", "PUT"}:
        return None
    if path_format == "/chat":
        return {"messages": [{"role": "user", "content": "find a spreadsheet skill"}]}
    if path_format == "/route":
        return {"task": "create an excel spreadsheet report"}
    if path_format == "/route-feedback":
        return {"route_id": "route-1", "outcome": "used"}
    if path_format.startswith("/rest/v1/rpc/"):
        return {"query": "spreadsheet", "max_results": 3}
    if path_format == "/rest/v1/{table}":
        return {"id": "public-guard", "name": "blocked", "source": "test"}
    return {}


class ApiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_db_path = local_store.DB_PATH
        self.db_path = Path(self.tmp.name) / "local_skills.db"
        local_store.DB_PATH = self.db_path
        scraper.store.DB_PATH = self.db_path
        local_store.init_db()
        local_store.invalidate_vector_cache()
        self.client = TestClient(scraper.app)

    def tearDown(self) -> None:
        local_store.invalidate_vector_cache()
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
        self.assertEqual(body["vector_index"]["valid_vectors"], 1)
        self.assertEqual(body["vector_index"]["vector_dim"], 384)
        self.assertEqual(body["scraper"]["running_recent"], 0)
        self.assertEqual(body["scraper"]["running_stale"], 0)

    def test_readyz_includes_scraper_bookkeeping(self) -> None:
        self._insert_skill()
        now = datetime.now(timezone.utc)
        last_success_at = (now - timedelta(minutes=10)).isoformat()
        stale_started_at = (now - timedelta(seconds=scraper.STALE_SCRAPE_RUN_SECONDS + 30)).isoformat()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO scrape_runs (
                    id, started_at, finished_at, status, skills_found, new_skills_found
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("run-done", last_success_at, last_success_at, "done", 10, 2),
            )
            conn.execute(
                "INSERT INTO scrape_runs (id, started_at, status) VALUES (?, ?, ?)",
                ("run-stale", stale_started_at, "running"),
            )
            conn.commit()
        finally:
            conn.close()

        response = self.client.get("/readyz")
        self.assertEqual(response.status_code, 200)
        scraper_summary = response.json()["scraper"]
        self.assertEqual(scraper_summary["running_recent"], 0)
        self.assertEqual(scraper_summary["running_stale"], 1)
        self.assertEqual(scraper_summary["last_success_at"], last_success_at)
        self.assertEqual(scraper_summary["recent_runs"][0]["id"], "run-done")
        self.assertGreater(scraper_summary["recent_runs"][1]["age_seconds"], scraper.STALE_SCRAPE_RUN_SECONDS)

    def test_public_guard_allows_readiness_and_route_but_blocks_writes(self) -> None:
        self.assertEqual(self.client.get("/readyz", headers={"x-forwarded-for": "203.0.113.10"}).status_code, 503)
        self.assertNotEqual(
            self.client.post("/route", json={"task": ""}, headers={"x-forwarded-for": "203.0.113.10"}).status_code,
            403,
        )
        probes = [
            ("post", "/scrape", None),
            ("post", "/chat", {"messages": [{"role": "user", "content": "find a spreadsheet skill"}]}),
            ("post", "/seed-backlog", None),
            ("post", "/normalize-db", None),
            ("get", "/normalize-db/progress", None),
            ("post", "/rescan", None),
            ("get", "/skills", None),
            ("get", "/library", None),
            ("get", "/library/files/example.md", None),
            ("get", "/route-metrics", None),
            ("post", "/route-feedback", {"route_id": "route-1", "outcome": "used"}),
            ("get", "/rest/v1/skills?select=id", None),
            ("post", "/rest/v1/rpc/search_skills", {"query": "spreadsheet", "max_results": 3}),
            ("post", "/rest/v1/rpc/vector_search_skills", {"query_embedding": [0.0] * 384, "match_count": 3}),
            ("post", "/rest/v1/rpc/hybrid_search_skills", {"query_text": "spreadsheet", "match_count": 3}),
            ("post", "/rest/v1/skills", {"id": "public-guard", "name": "blocked", "source": "test"}),
            ("patch", "/rest/v1/skills?id=eq.public-guard", {"name": "blocked"}),
            ("delete", "/rest/v1/skills?id=eq.public-guard", None),
        ]
        for method, path, json_body in probes:
            request = getattr(self.client, method)
            kwargs = {"headers": {"x-forwarded-for": "203.0.113.10"}}
            if json_body is not None:
                kwargs["json"] = json_body
            blocked = request(path, **kwargs)
            self.assertEqual(blocked.status_code, 403, path)
            self.assertEqual(blocked.json()["error"], "read-only public API", path)

    def test_public_guard_contract_covers_registered_routes(self) -> None:
        expected_public_routes = {
            ("GET", "/"),
            ("GET", "/healthz"),
            ("GET", "/readyz"),
            ("GET", "/status"),
            ("GET", "/find-semantic"),
            ("GET", "/content/{hash_value}"),
            ("POST", "/route"),
        }
        discovered_public_routes = set()

        for route in _registered_routes(scraper.app):
            path_format = getattr(route, "path_format", None) or getattr(route, "path", "")
            methods = set(getattr(route, "methods", set()) or set()) - {"HEAD", "OPTIONS"}
            for method in methods:
                public_path = _route_example(path_format)
                if scraper.public_api_allows(method, public_path):
                    discovered_public_routes.add((method, path_format))
                    continue

                request = getattr(self.client, method.lower())
                kwargs = {"headers": {"x-forwarded-for": "203.0.113.10"}}
                json_body = _route_json_body(method, path_format)
                if json_body is not None:
                    kwargs["json"] = json_body
                blocked = request(public_path, **kwargs)
                self.assertEqual(blocked.status_code, 403, (method, path_format, public_path))
                self.assertEqual(blocked.json()["error"], "read-only public API", (method, path_format))

        self.assertEqual(discovered_public_routes, expected_public_routes)
        self.assertFalse(scraper.public_api_allows("GET", "/skills"))
        self.assertFalse(scraper.public_api_allows("GET", "/library"))
        self.assertFalse(scraper.public_api_allows("GET", "/library/files/example.md"))
        self.assertFalse(scraper.public_api_allows("GET", "/library/files"))

    def test_scrape_run_lease_allows_only_one_running_row(self) -> None:
        first = self.client.post("/rest/v1/scrape_runs", json={"id": "run-1", "status": "running"})
        self.assertEqual(first.status_code, 200)

        second = self.client.post("/rest/v1/scrape_runs", json={"id": "run-2", "status": "running"})
        self.assertEqual(second.status_code, 409)
        self.assertIn("UNIQUE", second.json()["error"].upper())

        done = self.client.patch(
            "/rest/v1/scrape_runs?id=eq.run-1",
            json={"status": "done", "finished_at": datetime.now(timezone.utc).isoformat()},
        )
        self.assertEqual(done.status_code, 204)

        third = self.client.post("/rest/v1/scrape_runs", json={"id": "run-3", "status": "running"})
        self.assertEqual(third.status_code, 200)

    def test_skills_writes_invalidate_vector_cache(self) -> None:
        first = {
            "id": "skill-1",
            "name": "spreadsheet-reporter",
            "description": "Build spreadsheet reports with formulas and charts.",
            "source": "test",
            "url": "https://example.com/spreadsheet",
            "risk_score": 0,
            "quality_status": "active",
            "embedding": [1.0] + [0.0] * 383,
        }
        second = {
            "id": "skill-2",
            "name": "browser-qa",
            "description": "Test browser interfaces and capture screenshots.",
            "source": "test",
            "url": "https://example.com/browser",
            "risk_score": 0,
            "quality_status": "active",
            "embedding": [0.0, 1.0] + [0.0] * 382,
        }

        self.client.post("/rest/v1/skills?on_conflict=url", json=first)
        initial = self.client.post(
            "/rest/v1/rpc/vector_search_skills",
            json={"query_embedding": [1.0] + [0.0] * 383, "match_count": 5},
        )
        self.assertEqual([row["id"] for row in initial.json()], ["skill-1"])

        self.client.post("/rest/v1/skills?on_conflict=url", json=second)
        refreshed = self.client.post(
            "/rest/v1/rpc/vector_search_skills",
            json={"query_embedding": [0.0, 1.0] + [0.0] * 382, "match_count": 5},
        )
        self.assertEqual(refreshed.json()[0]["id"], "skill-2")

        third = {
            "id": "skill-3",
            "name": "presentation-builder",
            "description": "Build polished slide decks.",
            "source": "test",
            "url": "https://example.com/slides",
            "risk_score": 0,
            "quality_status": "active",
        }
        self.client.post("/rest/v1/skills?on_conflict=url", json=third)
        patch = self.client.patch(
            "/rest/v1/skills?id=eq.skill-3",
            json={"embedding": [0.0, 0.0, 1.0] + [0.0] * 381},
        )
        self.assertEqual(patch.status_code, 204)
        patched = self.client.post(
            "/rest/v1/rpc/vector_search_skills",
            json={"query_embedding": [0.0, 0.0, 1.0] + [0.0] * 381, "match_count": 5},
        )
        self.assertEqual(patched.json()[0]["id"], "skill-3")

    def test_vector_rpc_rejects_invalid_embeddings(self) -> None:
        missing = self.client.post("/rest/v1/rpc/vector_search_skills", json={})
        self.assertEqual(missing.status_code, 400)
        self.assertIn("required", missing.json()["error"])

        short = self.client.post(
            "/rest/v1/rpc/vector_search_skills",
            json={"query_embedding": [0.0, 1.0], "match_count": 5},
        )
        self.assertEqual(short.status_code, 400)
        self.assertIn("384", short.json()["error"])

        bad_value = self.client.post(
            "/rest/v1/rpc/vector_search_skills",
            json={"query_embedding": ["nope"] * local_store.EMBEDDING_DIM, "match_count": 5},
        )
        self.assertEqual(bad_value.status_code, 400)
        self.assertIn("numbers", bad_value.json()["error"])

    def test_hybrid_rpc_allows_fts_without_embedding(self) -> None:
        self._insert_skill(active=True, embedded=False)
        response = self.client.post(
            "/rest/v1/rpc/hybrid_search_skills",
            json={"query_text": "spreadsheet formulas", "match_count": 5},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["id"], "skill-1")

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
        self.assertTrue(body["route_id"])
        self.assertIn("validate sheet names", body["content"])
        self.assertTrue(body["content_url"].startswith("/content/"))
        self.assertEqual(body["score_debug"]["quality_status"], "active")
        metrics = body["score_debug"]["metrics"]
        self.assertGreaterEqual(metrics["latency_ms"], 0)
        self.assertGreaterEqual(metrics["skill_find_ms"], 0)
        self.assertGreaterEqual(metrics["retrieval_ms"], 0)
        self.assertGreaterEqual(metrics["rerank_ms"], 0)
        self.assertGreater(metrics["content_tokens"], 0)
        self.assertEqual(metrics["injected_tokens"], metrics["hint_tokens"] + metrics["content_tokens"])
        self.assertGreater(metrics["response_tokens"], metrics["hint_tokens"])

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            event = conn.execute("SELECT * FROM route_events ORDER BY created_at DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(event)
        self.assertEqual(event["id"], body["route_id"])
        self.assertEqual(event["tier"], "full")
        self.assertEqual(event["skill_name"], "spreadsheet-reporter")
        self.assertGreaterEqual(event["skill_find_ms"], 0)
        self.assertGreaterEqual(event["injected_tokens"], event["content_tokens"])
        self.assertGreater(event["response_tokens"], 0)

    def test_route_caps_platform_trap_to_hint(self) -> None:
        landingi = {
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
        landing_page = {
            "id": "skill-2",
            "name": "landing-page-architect",
            "description": "Create landing pages with positioning, proof, sections, and CTA copy.",
            "source": "github_skill_file",
            "url": "https://example.com/landing-page",
            "risk_score": 0,
            "quality_status": "active",
            "quality_score": 88,
            "platforms": [],
            "rank": 0.9,
            "similarity": 0.9,
        }

        async def fake_retrieve(client, query, limit):
            del client, query, limit
            return [landingi, landing_page]

        with patch("recommender.retrieve_skills", fake_retrieve):
            response = self.client.post("/route", json={"task": "build a landing page for an AI automation agency"})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["route_id"])
        self.assertEqual(body["tier"], "hint")
        self.assertIsNone(body["content"])
        self.assertEqual(body["score_debug"]["metrics"]["content_tokens"], 0)
        self.assertGreater(body["score_debug"]["metrics"]["candidate_tokens"], 0)
        self.assertEqual(
            body["score_debug"]["metrics"]["injected_tokens"],
            body["score_debug"]["metrics"]["candidate_tokens"],
        )
        self.assertEqual(
            {c["name"] for c in body["candidates"]},
            {"sales-landingi", "landing-page-architect"},
        )
        self.assertNotIn("content", body["candidates"][0])
        self.assertLessEqual(len(body["candidates"]), 3)

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
        self.assertGreaterEqual(metrics["outcomes"]["pending"], 1)
        self.assertEqual(metrics["top_skills"][0]["skill_name"], "spreadsheet-reporter")
        self.assertGreaterEqual(metrics["top_skills"][0]["hint_count"], 1)
        self.assertIn("vector_index", metrics)
        self.assertEqual(metrics["vector_index"]["vector_dim"], 384)
        self.assertIn("avg_skill_find_ms", metrics)
        self.assertIn("avg_injected_tokens", metrics)
        self.assertGreaterEqual(metrics["top_skills"][0]["avg_skill_find_ms"], 0)
        self.assertGreaterEqual(metrics["avg_response_tokens"], 1)
        self.assertIn("p95_skill_find_ms", metrics)
        self.assertIn("p95_injected_tokens", metrics)
        self.assertEqual(metrics["budgets"]["skill_find_ms"], 1200)
        self.assertEqual(metrics["budgets"]["injected_tokens"], 3000)
        self.assertEqual(metrics["budget_breaches"]["any"], 0)

        local_store.insert_route_event(
            {
                "client": "test-client",
                "client_version": "0.1",
                "query_hash": "synthetic-slow-route",
                "query_chars": 20,
                "tier": "hint",
                "skill_id": "skill-1",
                "skill_name": "spreadsheet-reporter",
                "skill_url": "https://example.com/spreadsheet",
                "latency_ms": 2500,
                "skill_find_ms": 1800,
                "retrieval_ms": 1700,
                "rerank_ms": 100,
                "content_ms": 0,
                "result_count": 3,
                "input_tokens": 8,
                "hint_tokens": 40,
                "candidate_tokens": 4500,
                "content_tokens": 0,
                "injected_tokens": 4500,
                "response_tokens": 5200,
                "config_version": "test",
                "warnings": ["synthetic breach"],
            }
        )
        metrics = self.client.get("/route-metrics").json()
        self.assertGreaterEqual(metrics["p95_latency_ms"], metrics["avg_latency_ms"])
        self.assertEqual(metrics["budget_breaches"]["latency_ms"], 1)
        self.assertEqual(metrics["budget_breaches"]["skill_find_ms"], 1)
        self.assertEqual(metrics["budget_breaches"]["injected_tokens"], 1)
        self.assertEqual(metrics["budget_breaches"]["response_tokens"], 1)
        self.assertEqual(metrics["budget_breaches"]["any"], 1)

    def test_route_feedback_updates_existing_route_event(self) -> None:
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
            route = self.client.post("/route", json={"task": "create an excel report with formulas"}).json()

        feedback = self.client.post(
            "/route-feedback",
            json={
                "route_id": route["route_id"],
                "outcome": "used",
                "source": "unit-test",
                "note": "applied",
            },
        )
        self.assertEqual(feedback.status_code, 200)
        self.assertEqual(feedback.json()["outcome"], "used")

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            event = conn.execute("SELECT * FROM route_events WHERE id=?", (route["route_id"],)).fetchone()
        finally:
            conn.close()
        self.assertEqual(event["outcome"], "used")
        self.assertEqual(event["feedback_source"], "unit-test")

        metrics = self.client.get("/route-metrics").json()
        self.assertEqual(metrics["top_used_skills"][0]["skill_name"], "spreadsheet-reporter")
        self.assertGreaterEqual(metrics["top_used_skills"][0]["positive_count"], 1)

    def test_route_feedback_rejects_unknown_outcome(self) -> None:
        response = self.client.post(
            "/route-feedback",
            json={"route_id": "route-1", "outcome": "raw prompt was great"},
        )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
