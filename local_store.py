"""SQLite-backed replacement for the Supabase skills DB, used once the
scraper stops writing to Supabase (running out of free-tier space). Exposes
just enough of the PostgREST REST + RPC surface that scraper.py and
recommender.py already speak, so those files only need a base-URL swap.

Schema mirrors the Supabase `skills` / `scrape_runs` tables closely enough
that skill_to_row() output drops in unchanged. Embeddings are stored as
packed float32 BLOBs; vector search is brute-force numpy (fine at the scale
a single scraper accumulates going forward).
"""
import json
import os
import re
import sqlite3
import struct
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

DB_PATH = Path(os.getenv("LOCAL_DB_PATH", str(Path(__file__).parent / "local_skills.db")))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    source TEXT NOT NULL,
    url TEXT UNIQUE,
    tags TEXT DEFAULT '[]',
    raw TEXT DEFAULT '{}',
    discovered_at TEXT,
    risk_score INTEGER DEFAULT 0,
    risk_flags TEXT DEFAULT '[]',
    scanned_at TEXT,
    content_hash TEXT,
    canonical_id TEXT,
    quality_status TEXT DEFAULT 'active',
    quality_reasons TEXT DEFAULT '[]',
    quality_score INTEGER DEFAULT 0,
    platforms TEXT DEFAULT '[]',
    category TEXT,
    embedding BLOB,
    embedding_text_hash TEXT,
    embedded_at TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts5(
    name, description, tags, content='skills', content_rowid='rowid', tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS skills_ai AFTER INSERT ON skills BEGIN
    INSERT INTO skills_fts(rowid, name, description, tags)
    VALUES (new.rowid, new.name, new.description, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS skills_ad AFTER DELETE ON skills BEGIN
    INSERT INTO skills_fts(skills_fts, rowid, name, description, tags)
    VALUES ('delete', old.rowid, old.name, old.description, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS skills_au AFTER UPDATE ON skills BEGIN
    INSERT INTO skills_fts(skills_fts, rowid, name, description, tags)
    VALUES ('delete', old.rowid, old.name, old.description, old.tags);
    INSERT INTO skills_fts(rowid, name, description, tags)
    VALUES (new.rowid, new.name, new.description, new.tags);
END;

CREATE TABLE IF NOT EXISTS scrape_runs (
    id TEXT PRIMARY KEY,
    started_at TEXT,
    finished_at TEXT,
    status TEXT DEFAULT 'running',
    skills_found INTEGER DEFAULT 0,
    error TEXT,
    new_skills_found INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS route_events (
    id TEXT PRIMARY KEY,
    created_at TEXT,
    client TEXT,
    client_version TEXT,
    query_hash TEXT,
    query_chars INTEGER,
    tier TEXT,
    skill_id TEXT,
    skill_name TEXT,
    skill_url TEXT,
    latency_ms INTEGER,
    skill_find_ms INTEGER,
    retrieval_ms INTEGER,
    rerank_ms INTEGER,
    content_ms INTEGER,
    result_count INTEGER,
    input_tokens INTEGER,
    hint_tokens INTEGER,
    candidate_tokens INTEGER,
    content_tokens INTEGER,
    injected_tokens INTEGER,
    response_tokens INTEGER,
    config_version TEXT,
    outcome TEXT,
    outcome_at TEXT,
    feedback_source TEXT,
    feedback_note TEXT,
    warnings TEXT DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS route_events_created_at_idx ON route_events(created_at);
CREATE INDEX IF NOT EXISTS route_events_tier_idx ON route_events(tier);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    name TEXT,
    avatar_url TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS oauth_identities (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_user_id TEXT NOT NULL,
    created_at TEXT,
    UNIQUE(provider, provider_user_id)
);
CREATE INDEX IF NOT EXISTS oauth_identities_user_idx ON oauth_identities(user_id);

CREATE TABLE IF NOT EXISTS cli_tokens (
    id TEXT PRIMARY KEY,
    token_hash TEXT UNIQUE NOT NULL,
    user_id TEXT NOT NULL,
    created_at TEXT,
    last_used_at TEXT,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS cli_tokens_user_idx ON cli_tokens(user_id);

CREATE TABLE IF NOT EXISTS favorites (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    created_at TEXT,
    UNIQUE(user_id, skill_id)
);
CREATE INDEX IF NOT EXISTS favorites_user_idx ON favorites(user_id);

CREATE TABLE IF NOT EXISTS installs (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    skill_id TEXT,
    skill_url TEXT,
    target TEXT,
    installed_at TEXT
);
CREATE INDEX IF NOT EXISTS installs_user_idx ON installs(user_id);

CREATE TABLE IF NOT EXISTS private_skills (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    content TEXT NOT NULL,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS private_skills_owner_idx ON private_skills(owner_user_id);

CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id TEXT PRIMARY KEY,
    client_info TEXT NOT NULL,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS mcp_auth_codes (
    code TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    scopes TEXT DEFAULT '[]',
    user_id TEXT NOT NULL,
    created_at TEXT,
    expires_at TEXT,
    consumed_at TEXT
);
"""

TABLES = {
    "skills": {"unique": "url", "json_cols": {"tags", "raw", "risk_flags", "quality_reasons", "platforms"}},
    "scrape_runs": {"unique": None, "json_cols": set()},
    "route_events": {"unique": None, "json_cols": {"warnings"}},
    "users": {"unique": "email", "json_cols": set()},
    "oauth_identities": {"unique": None, "json_cols": set()},
    "cli_tokens": {"unique": "token_hash", "json_cols": set()},
    "favorites": {"unique": None, "json_cols": set()},
    "installs": {"unique": None, "json_cols": set()},
    "private_skills": {"unique": None, "json_cols": set()},
    "oauth_clients": {"unique": None, "json_cols": {"client_info"}},
    "mcp_auth_codes": {"unique": None, "json_cols": {"scopes"}},
}

SKILL_COLUMN_DEFAULTS = {
    "content_hash": "TEXT",
    "canonical_id": "TEXT",
    "quality_status": "TEXT DEFAULT 'active'",
    "quality_reasons": "TEXT DEFAULT '[]'",
    "quality_score": "INTEGER DEFAULT 0",
    "platforms": "TEXT DEFAULT '[]'",
    "category": "TEXT",
}

ROUTE_EVENT_COLUMN_DEFAULTS = {
    "outcome": "TEXT",
    "outcome_at": "TEXT",
    "feedback_source": "TEXT",
    "feedback_note": "TEXT",
    "skill_find_ms": "INTEGER",
    "rerank_ms": "INTEGER",
    "candidate_tokens": "INTEGER",
    "injected_tokens": "INTEGER",
    "user_id": "TEXT",
}


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)  # ride out concurrent write bursts (migration, embed loop)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _stale_duplicate_running_scrapes(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT id FROM scrape_runs WHERE status='running' "
        "ORDER BY COALESCE(started_at, '') DESC, id DESC"
    ).fetchall()
    if len(rows) <= 1:
        return
    now = _now()
    for row in rows[1:]:
        conn.execute(
            "UPDATE scrape_runs SET status=?, finished_at=?, error=? WHERE id=?",
            (
                "stale",
                now,
                "Marked stale by init_db before creating the single-running scrape guard.",
                row["id"],
            ),
        )


def init_db() -> None:
    conn = get_conn()
    try:
        conn.executescript(_SCHEMA)
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(skills)").fetchall()}
        for col, spec in SKILL_COLUMN_DEFAULTS.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE skills ADD COLUMN {col} {spec}")
        route_existing = {row["name"] for row in conn.execute("PRAGMA table_info(route_events)").fetchall()}
        for col, spec in ROUTE_EVENT_COLUMN_DEFAULTS.items():
            if col not in route_existing:
                conn.execute(f"ALTER TABLE route_events ADD COLUMN {col} {spec}")
        _stale_duplicate_running_scrapes(conn)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS scrape_runs_one_running_idx "
            "ON scrape_runs(status) WHERE status='running'"
        )
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def pack_embedding(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def unpack_embedding(blob: bytes) -> np.ndarray:
    n = len(blob) // 4
    return np.array(struct.unpack(f"{n}f", blob), dtype=np.float32)


def _row_to_dict(row: sqlite3.Row, table: str, select_cols: list[str] | None) -> dict:
    d = dict(row)
    d.pop("embedding", None)
    for col in TABLES[table]["json_cols"]:
        if col in d and isinstance(d[col], str):
            try:
                d[col] = json.loads(d[col])
            except Exception:
                pass
    if select_cols:
        d = {k: d[k] for k in select_cols if k in d}
    return d


def upsert_rows(table: str, rows: list[dict], on_conflict: str | None) -> list[dict]:
    conn = get_conn()
    try:
        cur = conn.cursor()
        out = []
        for row in rows:
            row = dict(row)
            if "id" not in row:
                row["id"] = str(uuid.uuid4())
            if table == "skills" and "discovered_at" not in row:
                row["discovered_at"] = _now()
            if table == "scrape_runs" and "started_at" not in row:
                row["started_at"] = _now()
            for col in TABLES[table]["json_cols"]:
                if col in row and not isinstance(row[col], str):
                    row[col] = json.dumps(row[col])
            if table == "skills" and isinstance(row.get("embedding"), list):
                row["embedding"] = pack_embedding(row["embedding"])

            cols = list(row.keys())
            placeholders = ",".join("?" for _ in cols)
            col_list = ",".join(cols)

            unique_col = TABLES[table]["unique"]
            if on_conflict and unique_col:
                update_cols = [c for c in cols if c != unique_col]
                set_clause = ",".join(f"{c}=excluded.{c}" for c in update_cols)
                sql = (
                    f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
                    f"ON CONFLICT({unique_col}) DO UPDATE SET {set_clause}"
                )
            else:
                sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"
            cur.execute(sql, list(row.values()))

            if unique_col and unique_col in row:
                r = cur.execute(f"SELECT * FROM {table} WHERE {unique_col}=?", (row[unique_col],)).fetchone()
            else:
                r = cur.execute(f"SELECT * FROM {table} WHERE id=?", (row["id"],)).fetchone()
            out.append(_row_to_dict(r, table, None))
        conn.commit()
        if table == "skills":
            invalidate_vector_cache()
        return out
    finally:
        conn.close()


_FILTER_RE = re.compile(r"^(not\.)?([a-z]+)\.(.*)$")


def _apply_filter(col: str, raw_value: str) -> tuple[str, list]:
    m = _FILTER_RE.match(raw_value)
    if not m:
        return f"{col} = ?", [raw_value]
    negate, op, val = m.groups()
    if op == "is" and val == "null":
        clause = f"{col} IS NULL"
        return (f"NOT ({clause})", []) if negate else (clause, [])
    if op == "eq":
        return f"{col} {'!=' if negate else '='} ?", [val]
    if op == "gt":
        return f"{col} {'<=' if negate else '>'} ?", [val]
    if op == "lt":
        return f"{col} {'>=' if negate else '<'} ?", [val]
    if op == "in":
        items = [v.strip().strip('"') for v in val.strip("()").split(",") if v.strip()]
        placeholders = ",".join("?" for _ in items)
        clause = f"{col} IN ({placeholders})"
        return (f"NOT ({clause})", items) if negate else (clause, items)
    return "1=1", []


def select_rows(
    table: str,
    select: str | None = None,
    filters: dict | None = None,
    order: str | None = None,
    limit: int | None = None,
    range_start: int | None = None,
    range_end: int | None = None,
    count_exact: bool = False,
) -> tuple[list[dict], int | None]:
    conn = get_conn()
    try:
        where_sql = []
        params = []
        for col, val in (filters or {}).items():
            clause, p = _apply_filter(col, val)
            where_sql.append(clause)
            params.extend(p)
        where = f"WHERE {' AND '.join(where_sql)}" if where_sql else ""

        total = None
        if count_exact:
            total = conn.execute(f"SELECT COUNT(*) FROM {table} {where}", params).fetchone()[0]

        order_sql = ""
        if order:
            col, _, direction = order.partition(".")
            direction = "DESC" if direction.lower() == "desc" else "ASC"
            order_sql = f"ORDER BY {col} {direction}"

        limit_sql, offset_sql = "", ""
        if range_start is not None and range_end is not None:
            offset_sql = f"OFFSET {range_start}"
            limit_sql = f"LIMIT {range_end - range_start + 1}"
        elif limit is not None:
            limit_sql = f"LIMIT {limit}"

        sql = f"SELECT * FROM {table} {where} {order_sql} {limit_sql} {offset_sql}"
        rows = conn.execute(sql, params).fetchall()
        select_cols = [c.strip() for c in select.split(",")] if select else None
        return [_row_to_dict(r, table, select_cols) for r in rows], total
    finally:
        conn.close()


def update_rows(table: str, filters: dict, data: dict) -> int:
    conn = get_conn()
    try:
        for col in TABLES[table]["json_cols"]:
            if col in data and not isinstance(data[col], str):
                data[col] = json.dumps(data[col])
        if table == "skills" and isinstance(data.get("embedding"), list):
            data["embedding"] = pack_embedding(data["embedding"])
        where_sql, params = [], []
        for col, val in filters.items():
            clause, p = _apply_filter(col, val)
            where_sql.append(clause)
            params.extend(p)
        set_clause = ",".join(f"{k}=?" for k in data.keys())
        sql = f"UPDATE {table} SET {set_clause} WHERE {' AND '.join(where_sql)}"
        cur = conn.execute(sql, list(data.values()) + params)
        conn.commit()
        if table == "skills" and cur.rowcount:
            invalidate_vector_cache()
        return cur.rowcount
    finally:
        conn.close()


def delete_rows(table: str, filters: dict) -> int:
    conn = get_conn()
    try:
        where_sql, params = [], []
        for col, val in filters.items():
            clause, p = _apply_filter(col, val)
            where_sql.append(clause)
            params.extend(p)
        sql = f"DELETE FROM {table} WHERE {' AND '.join(where_sql)}"
        cur = conn.execute(sql, params)
        conn.commit()
        if table == "skills" and cur.rowcount:
            invalidate_vector_cache()
        return cur.rowcount
    finally:
        conn.close()


def insert_route_event(event: dict) -> None:
    """Best-effort append-only analytics for route latency and token churn."""
    conn = get_conn()
    try:
        row = dict(event)
        row.setdefault("id", str(uuid.uuid4()))
        row.setdefault("created_at", _now())
        if not isinstance(row.get("warnings"), str):
            row["warnings"] = json.dumps(row.get("warnings") or [])
        cols = list(row.keys())
        placeholders = ",".join("?" for _ in cols)
        conn.execute(
            f"INSERT INTO route_events ({','.join(cols)}) VALUES ({placeholders})",
            [row[col] for col in cols],
        )
        conn.commit()
    finally:
        conn.close()


def _percentile(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * percentile))))
    return int(ordered[index] or 0)


def route_event_summary(
    hours: int = 24,
    *,
    max_latency_ms: int = 1500,
    max_skill_find_ms: int = 1200,
    max_injected_tokens: int = 3000,
    max_response_tokens: int = 3500,
) -> dict:
    """Aggregate recent route events for local ops/product checks."""
    conn = get_conn()
    try:
        cutoff = time.time() - (max(1, hours) * 3600)
        cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
        total = conn.execute("SELECT COUNT(*) FROM route_events WHERE created_at >= ?", (cutoff_iso,)).fetchone()[0]
        tiers = {
            row["tier"]: row["count"]
            for row in conn.execute(
                "SELECT tier, COUNT(*) AS count FROM route_events "
                "WHERE created_at >= ? GROUP BY tier",
                (cutoff_iso,),
            ).fetchall()
        }
        outcomes = {
            row["outcome"] or "pending": row["count"]
            for row in conn.execute(
                "SELECT outcome, COUNT(*) AS count FROM route_events "
                "WHERE created_at >= ? GROUP BY outcome",
                (cutoff_iso,),
            ).fetchall()
        }
        row = conn.execute(
            """
            SELECT
              AVG(latency_ms) AS avg_latency_ms,
              AVG(skill_find_ms) AS avg_skill_find_ms,
              AVG(retrieval_ms) AS avg_retrieval_ms,
              AVG(rerank_ms) AS avg_rerank_ms,
              AVG(content_ms) AS avg_content_ms,
              AVG(injected_tokens) AS avg_injected_tokens,
              AVG(response_tokens) AS avg_response_tokens,
              MAX(skill_find_ms) AS max_skill_find_ms,
              MAX(latency_ms) AS max_latency_ms,
              MAX(injected_tokens) AS max_injected_tokens,
              MAX(response_tokens) AS max_response_tokens
            FROM route_events
            WHERE created_at >= ?
            """,
            (cutoff_iso,),
        ).fetchone()
        metric_rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT latency_ms, skill_find_ms, injected_tokens, response_tokens
                FROM route_events
                WHERE created_at >= ?
                """,
                (cutoff_iso,),
            ).fetchall()
        ]
        latency_values = [int(r["latency_ms"] or 0) for r in metric_rows]
        skill_find_values = [int(r["skill_find_ms"] or 0) for r in metric_rows]
        injected_values = [int(r["injected_tokens"] or 0) for r in metric_rows]
        response_values = [int(r["response_tokens"] or 0) for r in metric_rows]
        budget_breaches = {
            "latency_ms": sum(1 for value in latency_values if value > max_latency_ms),
            "skill_find_ms": sum(1 for value in skill_find_values if value > max_skill_find_ms),
            "injected_tokens": sum(1 for value in injected_values if value > max_injected_tokens),
            "response_tokens": sum(1 for value in response_values if value > max_response_tokens),
            "any": sum(
                1
                for r in metric_rows
                if int(r["latency_ms"] or 0) > max_latency_ms
                or int(r["skill_find_ms"] or 0) > max_skill_find_ms
                or int(r["injected_tokens"] or 0) > max_injected_tokens
                or int(r["response_tokens"] or 0) > max_response_tokens
            ),
        }
        slowest = [
            dict(r)
            for r in conn.execute(
                """
                SELECT created_at, client, tier, skill_name, latency_ms, skill_find_ms,
                       retrieval_ms, rerank_ms, content_ms, injected_tokens,
                       response_tokens, warnings
                FROM route_events
                WHERE created_at >= ?
                ORDER BY latency_ms DESC
                LIMIT 5
                """,
                (cutoff_iso,),
            ).fetchall()
        ]
        for event in slowest:
            try:
                event["warnings"] = json.loads(event.get("warnings") or "[]")
            except Exception:
                event["warnings"] = []
        top_skills = [
            dict(r)
            for r in conn.execute(
                """
                SELECT
                  skill_name,
                  skill_url,
                  COUNT(*) AS count,
                  SUM(CASE WHEN tier='full' THEN 1 ELSE 0 END) AS full_count,
                  SUM(CASE WHEN tier='hint' THEN 1 ELSE 0 END) AS hint_count,
                  SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END) AS feedback_count,
                  SUM(CASE WHEN outcome IN ('used', 'installed') THEN 1 ELSE 0 END) AS positive_count,
                  AVG(latency_ms) AS avg_latency_ms,
                  AVG(skill_find_ms) AS avg_skill_find_ms,
                  AVG(injected_tokens) AS avg_injected_tokens,
                  AVG(response_tokens) AS avg_response_tokens
                FROM route_events
                WHERE created_at >= ?
                  AND skill_name IS NOT NULL
                  AND skill_name != ''
                GROUP BY skill_name, skill_url
                ORDER BY count DESC, positive_count DESC, skill_name ASC
                LIMIT 10
                """,
                (cutoff_iso,),
            ).fetchall()
        ]
        for skill in top_skills:
            for key in ("count", "full_count", "hint_count", "feedback_count", "positive_count"):
                skill[key] = int(skill[key] or 0)
            skill["avg_latency_ms"] = int(skill["avg_latency_ms"] or 0)
            skill["avg_skill_find_ms"] = int(skill["avg_skill_find_ms"] or 0)
            skill["avg_injected_tokens"] = int(skill["avg_injected_tokens"] or 0)
            skill["avg_response_tokens"] = int(skill["avg_response_tokens"] or 0)
        top_used_skills = [skill for skill in top_skills if skill["positive_count"] > 0]
        top_used_skills.sort(key=lambda s: (-s["positive_count"], -s["count"], s["skill_name"] or ""))
        return {
            "window_hours": hours,
            "total": total,
            "tiers": tiers,
            "outcomes": outcomes,
            "top_skills": top_skills,
            "top_used_skills": top_used_skills[:10],
            "vector_index": vector_index_stats(),
            "avg_latency_ms": int(row["avg_latency_ms"] or 0),
            "avg_skill_find_ms": int(row["avg_skill_find_ms"] or 0),
            "avg_retrieval_ms": int(row["avg_retrieval_ms"] or 0),
            "avg_rerank_ms": int(row["avg_rerank_ms"] or 0),
            "avg_content_ms": int(row["avg_content_ms"] or 0),
            "avg_injected_tokens": int(row["avg_injected_tokens"] or 0),
            "avg_response_tokens": int(row["avg_response_tokens"] or 0),
            "p95_latency_ms": _percentile(latency_values, 0.95),
            "p95_skill_find_ms": _percentile(skill_find_values, 0.95),
            "p95_injected_tokens": _percentile(injected_values, 0.95),
            "p95_response_tokens": _percentile(response_values, 0.95),
            "max_skill_find_ms": int(row["max_skill_find_ms"] or 0),
            "max_latency_ms": int(row["max_latency_ms"] or 0),
            "max_injected_tokens": int(row["max_injected_tokens"] or 0),
            "max_response_tokens": int(row["max_response_tokens"] or 0),
            "budgets": {
                "latency_ms": int(max_latency_ms),
                "skill_find_ms": int(max_skill_find_ms),
                "injected_tokens": int(max_injected_tokens),
                "response_tokens": int(max_response_tokens),
            },
            "budget_breaches": budget_breaches,
            "slowest": slowest,
        }
    finally:
        conn.close()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def scrape_run_summary(stale_after_seconds: int = 7200, limit: int = 5) -> dict:
    """Return recent scraper bookkeeping for readiness and launch checks."""
    stale_after_seconds = max(1, int(stale_after_seconds or 7200))
    limit = max(1, min(int(limit or 5), 20))
    now = datetime.now(timezone.utc)
    conn = get_conn()
    try:
        recent = [
            dict(r)
            for r in conn.execute(
                """
                SELECT id, started_at, finished_at, status, skills_found,
                       new_skills_found, error
                FROM scrape_runs
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        ]
        row = conn.execute(
            """
            SELECT MAX(finished_at) AS last_success_at
            FROM scrape_runs
            WHERE status='done'
            """
        ).fetchone()
    finally:
        conn.close()

    running_recent = 0
    running_stale = 0
    for run in recent:
        started_at = _parse_iso(run.get("started_at"))
        age_seconds = int((now - started_at).total_seconds()) if started_at else None
        run["age_seconds"] = age_seconds
        if run.get("status") == "running":
            if age_seconds is not None and age_seconds > stale_after_seconds:
                running_stale += 1
            else:
                running_recent += 1
    return {
        "stale_after_seconds": stale_after_seconds,
        "running_recent": running_recent,
        "running_stale": running_stale,
        "last_success_at": row["last_success_at"] if row else None,
        "recent_runs": recent,
    }


def update_route_event_feedback(route_id: str, outcome: str, source: str = "", note: str = "") -> bool:
    """Attach privacy-safe outcome feedback to a route event."""
    conn = get_conn()
    try:
        cur = conn.execute(
            """
            UPDATE route_events
            SET outcome=?, outcome_at=?, feedback_source=?, feedback_note=?
            WHERE id=?
            """,
            (
                outcome,
                _now(),
                source[:80],
                note[:300],
                route_id,
            ),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


_WORD_RE = re.compile(r"\w+")


def _fts_query(text: str) -> str:
    words = _WORD_RE.findall(text)
    return " ".join(f'"{w}"' for w in words) if words else ""


def _stars(raw_json: str) -> int:
    try:
        return int(json.loads(raw_json or "{}").get("stars") or 0)
    except Exception:
        return 0


def search_skills_fts(query: str, max_results: int = 10) -> list[dict]:
    conn = get_conn()
    try:
        fts_q = _fts_query(query)
        if not fts_q:
            return []
        rows = conn.execute(
            """
            SELECT s.*, bm25(skills_fts) AS bm25
            FROM skills_fts
            JOIN skills s ON s.rowid = skills_fts.rowid
            WHERE skills_fts MATCH ?
              AND s.risk_score < 3
              AND COALESCE(s.quality_status, 'active') IN ('active', 'metadata_only')
            ORDER BY bm25(skills_fts) ASC
            LIMIT ?
            """,
            (fts_q, max_results),
        ).fetchall()
        out = []
        for r in rows:
            d = _row_to_dict(r, "skills", None)
            d["stars"] = _stars(dict(r).get("raw", "{}"))
            d["rank"] = -r["bm25"]
            out.append(d)
        return out
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


# In-memory embedding-matrix cache. Rebuilding the matrix from blobs is
# O(corpus) per query and dominates latency once the corpus is large; with the
# cache a search is a single matvec.
# Short TTL so freshly embedded skills become searchable within a minute.
EMBEDDING_DIM = 384
_EMB_DIM = EMBEDDING_DIM
_EMB_BLOB_LEN = _EMB_DIM * 4
_EMB_CACHE_TTL_SECONDS = 60.0
_emb_cache: dict = {"at": 0.0, "ids": [], "mat": None}


def invalidate_vector_cache() -> None:
    """Clear the in-process vector matrix after skills writes."""
    _emb_cache.update(at=0.0, ids=[], mat=None)


def vector_index_stats() -> dict:
    """Return cheap search-index stats for latency/debug dashboards."""
    conn = get_conn()
    try:
        row = conn.execute(
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN COALESCE(quality_status, 'active') = 'active' THEN 1 ELSE 0 END) AS active,
              SUM(CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END) AS embedded,
              SUM(CASE WHEN embedding IS NOT NULL AND LENGTH(embedding) = ? THEN 1 ELSE 0 END) AS valid_vectors
            FROM skills
            """,
            (_EMB_BLOB_LEN,),
        ).fetchone()
    finally:
        conn.close()

    mat = _emb_cache.get("mat")
    cache_at = float(_emb_cache.get("at") or 0.0)
    age_ms = int((time.monotonic() - cache_at) * 1000) if mat is not None and cache_at else None
    return {
        "total_skills": int(row["total"] or 0),
        "active_skills": int(row["active"] or 0),
        "embedded_skills": int(row["embedded"] or 0),
        "valid_vectors": int(row["valid_vectors"] or 0),
        "cache_ready": mat is not None,
        "cache_vectors": len(_emb_cache.get("ids") or []),
        "cache_age_ms": age_ms,
        "cache_ttl_seconds": int(_EMB_CACHE_TTL_SECONDS),
        "matrix_bytes": int(mat.nbytes) if mat is not None else 0,
        "vector_dim": _EMB_DIM,
    }


def _embedding_matrix(conn: sqlite3.Connection) -> tuple[list[str], np.ndarray]:
    now = time.monotonic()
    if _emb_cache["mat"] is not None and now - _emb_cache["at"] < _EMB_CACHE_TTL_SECONDS:
        return _emb_cache["ids"], _emb_cache["mat"]
    rows = conn.execute(
        "SELECT id, embedding FROM skills "
        "WHERE embedding IS NOT NULL "
        "AND risk_score < 3 "
        "AND COALESCE(quality_status, 'active') = 'active'"
    ).fetchall()
    ids = [r["id"] for r in rows if len(r["embedding"]) == _EMB_BLOB_LEN]
    blobs = [bytes(r["embedding"]) for r in rows if len(r["embedding"]) == _EMB_BLOB_LEN]
    if blobs:
        mat = np.frombuffer(b"".join(blobs), dtype=np.float32).reshape(len(blobs), _EMB_DIM)
    else:
        mat = np.zeros((0, _EMB_DIM), dtype=np.float32)
    _emb_cache.update(at=now, ids=ids, mat=mat)
    return ids, mat


def vector_search_skills(query_embedding: list[float], match_count: int = 10) -> list[dict]:
    conn = get_conn()
    try:
        ids, mat = _embedding_matrix(conn)
        if not ids:
            return []
        q = np.array(query_embedding, dtype=np.float32)
        sims = mat @ q  # both L2-normalized -> cosine similarity
        k = min(match_count, len(ids))
        top = np.argpartition(-sims, k - 1)[:k]
        top = top[np.argsort(-sims[top])]
        top_ids = [ids[i] for i in top]
        placeholders = ",".join("?" for _ in top_ids)
        fetched = conn.execute(
            f"SELECT * FROM skills WHERE id IN ({placeholders})", top_ids
        ).fetchall()
        by_id: dict[str, dict] = {}
        for r in fetched:
            d = _row_to_dict(r, "skills", None)
            d["stars"] = _stars(dict(r).get("raw", "{}"))
            by_id[d["id"]] = d
        out = []
        for i in top:
            d = by_id.get(ids[i])
            if d is not None:
                d["rank"] = float(sims[i])
                out.append(d)
        return out
    finally:
        conn.close()


def hybrid_search_skills(
    query_text: str,
    query_embedding: list[float] | None,
    match_count: int = 10,
    fts_weight: float = 1.0,
    vec_weight: float = 0.6,
    rrf_k: int = 20,
) -> list[dict]:
    fts = search_skills_fts(query_text, 60)
    vec = vector_search_skills(query_embedding, 60) if query_embedding else []

    by_id: dict[str, dict] = {}
    scores: dict[str, float] = {}
    for ix, row in enumerate(fts, start=1):
        by_id[row["id"]] = row
        scores[row["id"]] = scores.get(row["id"], 0.0) + fts_weight / (rrf_k + ix)
    for ix, row in enumerate(vec, start=1):
        row["similarity"] = row["rank"]  # cosine, before rank is overwritten with the fused score
        existing = by_id.get(row["id"])
        if existing is not None:
            existing["similarity"] = row["similarity"]
        else:
            by_id[row["id"]] = row
        scores[row["id"]] = scores.get(row["id"], 0.0) + vec_weight / (rrf_k + ix)

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:match_count]
    out = []
    for skill_id, score in ranked:
        row = dict(by_id[skill_id])
        stars = row.get("stars") or 0
        risk = row.get("risk_score") or 0
        quality = max(0, min(int(row.get("quality_score") or 50), 100)) / 100
        star_bonus = 0.003 * min(np.log1p(max(stars, 0)), 6) / 6
        risk_penalty = 0.01 * min(risk, 2)
        row["rank"] = float(score + star_bonus + (0.004 * quality) - risk_penalty)
        out.append(row)
    out.sort(key=lambda r: r["rank"], reverse=True)
    return out


# --- Accounts: users, OAuth identities, CLI tokens, and per-user data ------

def get_or_create_user(email: str, name: str | None, avatar_url: str | None) -> dict:
    """Find a user by email, or create one. Existing name/avatar_url are
    refreshed from the identity provider's latest profile on every login."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if row is None:
            user_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO users (id, email, name, avatar_url, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, email, name, avatar_url, _now()),
            )
        else:
            user_id = row["id"]
            conn.execute(
                "UPDATE users SET name=?, avatar_url=? WHERE id=?",
                (name, avatar_url, user_id),
            )
        conn.commit()
        return dict(conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())
    finally:
        conn.close()


def link_oauth_identity(user_id: str, provider: str, provider_user_id: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO oauth_identities (id, user_id, provider, provider_user_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), user_id, provider, provider_user_id, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def create_cli_token(user_id: str, token_hash: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO cli_tokens (id, token_hash, user_id, created_at) VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), token_hash, user_id, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def get_user_by_token_hash(token_hash: str) -> dict | None:
    """Resolve a live (non-revoked) CLI token to its user, bumping last_used_at."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT u.* FROM cli_tokens t JOIN users u ON u.id = t.user_id "
            "WHERE t.token_hash=? AND t.revoked_at IS NULL",
            (token_hash,),
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE cli_tokens SET last_used_at=? WHERE token_hash=?", (_now(), token_hash))
        conn.commit()
        return dict(row)
    finally:
        conn.close()


def revoke_cli_token(token_hash: str) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE cli_tokens SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
            (_now(), token_hash),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def create_oauth_client(client_id: str, client_info: dict) -> None:
    """Store a dynamically-registered MCP OAuth client verbatim (client_secret,
    token_endpoint_auth_method, redirect_uris, etc. -- whatever the `mcp` SDK's
    registration handler assigned) so get_oauth_client can hand it back
    unchanged for the SDK's own client authentication checks."""
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO oauth_clients (client_id, client_info, created_at) VALUES (?, ?, ?)",
            (client_id, json.dumps(client_info), _now()),
        )
        conn.commit()
    finally:
        conn.close()


def get_oauth_client(client_id: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT client_info FROM oauth_clients WHERE client_id=?", (client_id,)).fetchone()
        return json.loads(row["client_info"]) if row else None
    finally:
        conn.close()


def create_mcp_auth_code(
    code: str, client_id: str, code_challenge: str, redirect_uri: str, scopes: list[str], user_id: str, expires_at: str
) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO mcp_auth_codes "
            "(code, client_id, code_challenge, redirect_uri, scopes, user_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (code, client_id, code_challenge, redirect_uri, json.dumps(scopes), user_id, _now(), expires_at),
        )
        conn.commit()
    finally:
        conn.close()


def _mcp_auth_code_row(row: sqlite3.Row) -> dict:
    entry = dict(row)
    entry["scopes"] = json.loads(entry["scopes"])
    return entry


def peek_mcp_auth_code(code: str) -> dict | None:
    """Non-destructive lookup -- the `mcp` SDK validates expiry/redirect_uri/
    PKCE itself against this before ever calling exchange, so this must not
    consume the code (see mcp_oauth.py's /codes/{code} and /token split)."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM mcp_auth_codes WHERE code=?", (code,)).fetchone()
        return _mcp_auth_code_row(row) if row else None
    finally:
        conn.close()


def consume_mcp_auth_code(code: str, client_id: str) -> dict | None:
    """Atomically load and consume a not-yet-used, not-expired auth code
    belonging to `client_id`, guarding against replay."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM mcp_auth_codes WHERE code=? AND client_id=? AND consumed_at IS NULL AND expires_at > ?",
            (code, client_id, _now()),
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE mcp_auth_codes SET consumed_at=? WHERE code=?", (_now(), code))
        conn.commit()
        return _mcp_auth_code_row(row)
    finally:
        conn.close()


def list_route_events_for_user(user_id: str, limit: int = 100) -> list[dict]:
    """Every route_events column for this user -- all privacy-safe by
    construction (query_hash/query_chars only, never raw prompt text)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM route_events WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            event["warnings"] = json.loads(event["warnings"]) if event.get("warnings") else []
            events.append(event)
        return events
    finally:
        conn.close()


def add_favorite(user_id: str, skill_id: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO favorites (id, user_id, skill_id, created_at) VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), user_id, skill_id, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def remove_favorite(user_id: str, skill_id: str) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM favorites WHERE user_id=? AND skill_id=?", (user_id, skill_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_favorites(user_id: str) -> list[dict]:
    """Favorited skills joined with their current skill row, newest first."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT s.*, f.created_at AS favorited_at FROM favorites f "
            "JOIN skills s ON s.id = f.skill_id WHERE f.user_id=? ORDER BY f.created_at DESC",
            (user_id,),
        ).fetchall()
        return [_row_to_dict(r, "skills", None) | {"favorited_at": r["favorited_at"]} for r in rows]
    finally:
        conn.close()


def record_install(user_id: str, skill_id: str | None, skill_url: str | None, target: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO installs (id, user_id, skill_id, skill_url, target, installed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), user_id, skill_id, skill_url, target, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def list_installs(user_id: str) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM installs WHERE user_id=? ORDER BY installed_at DESC", (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def add_private_skill(owner_user_id: str, name: str, description: str | None, content: str) -> dict:
    conn = get_conn()
    try:
        skill_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO private_skills (id, owner_user_id, name, description, content, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (skill_id, owner_user_id, name, description, content, _now()),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM private_skills WHERE id=?", (skill_id,)).fetchone())
    finally:
        conn.close()


def list_private_skills(owner_user_id: str) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM private_skills WHERE owner_user_id=? ORDER BY created_at DESC", (owner_user_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def remove_private_skill(owner_user_id: str, skill_id: str) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM private_skills WHERE id=? AND owner_user_id=?", (skill_id, owner_user_id)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
