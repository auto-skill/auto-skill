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
"""

TABLES = {
    "skills": {"unique": "url", "json_cols": {"tags", "raw", "risk_flags", "quality_reasons", "platforms"}},
    "scrape_runs": {"unique": None, "json_cols": set()},
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


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)  # ride out concurrent write bursts (migration, embed loop)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    conn = get_conn()
    try:
        conn.executescript(_SCHEMA)
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(skills)").fetchall()}
        for col, spec in SKILL_COLUMN_DEFAULTS.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE skills ADD COLUMN {col} {spec}")
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
        where_sql, params = [], []
        for col, val in filters.items():
            clause, p = _apply_filter(col, val)
            where_sql.append(clause)
            params.extend(p)
        set_clause = ",".join(f"{k}=?" for k in data.keys())
        sql = f"UPDATE {table} SET {set_clause} WHERE {' AND '.join(where_sql)}"
        cur = conn.execute(sql, list(data.values()) + params)
        conn.commit()
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
        return cur.rowcount
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
_EMB_DIM = 384
_EMB_BLOB_LEN = _EMB_DIM * 4
_EMB_CACHE_TTL_SECONDS = 60.0
_emb_cache: dict = {"at": 0.0, "ids": [], "mat": None}


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
