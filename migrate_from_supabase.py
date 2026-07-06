"""Resumable migration: frozen Supabase corpus -> local_skills.db.

The PC is becoming the single source of truth (self-hosted public DB), so the
~202k historical skills (including their finished embeddings) move down into
SQLite. Keyset pagination by id; progress checkpointed to migrate_state.json
after every page so this can be killed and re-run freely. Pages are small and
retried because the over-quota Supabase instance reads slowly.

Run:  python migrate_from_supabase.py
"""
import json
import os
import sys
import time
from pathlib import Path

import httpx

import local_store as store

SUPABASE_URL = "https://kgkuoxdizynkcrbasamu.supabase.co"
SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
HEADERS = {"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"}
PAGE_SIZE = 200
STATE_FILE = Path(__file__).parent / "migrate_state.json"

SKILL_COLS = ("id,name,description,source,url,tags,raw,discovered_at,risk_score,"
              "risk_flags,scanned_at,embedding,embedding_text_hash,embedded_at")


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_id": "", "migrated": 0, "runs_done": False}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state))


def fetch_page(client: httpx.Client, last_id: str) -> list[dict]:
    params = {"select": SKILL_COLS, "order": "id.asc", "limit": str(PAGE_SIZE)}
    if last_id:
        params["id"] = f"gt.{last_id}"
    for attempt in range(5):
        try:
            r = client.get(f"{SUPABASE_URL}/rest/v1/skills", params=params,
                           headers=HEADERS, timeout=httpx.Timeout(180, connect=15))
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            print(f"page fetch attempt {attempt + 1}/5 failed: {e!r}", flush=True)
            time.sleep(2 ** attempt)
    raise RuntimeError("page fetch failed after 5 attempts; re-run to resume")


def migrate_scrape_runs(client: httpx.Client, state: dict) -> None:
    if state.get("runs_done"):
        return
    r = client.get(f"{SUPABASE_URL}/rest/v1/scrape_runs",
                   params={"select": "*", "order": "started_at.asc", "limit": "10000"},
                   headers=HEADERS, timeout=60)
    r.raise_for_status()
    runs = r.json()
    conn = store.get_conn()
    try:
        existing = {row[0] for row in conn.execute("SELECT id FROM scrape_runs")}
    finally:
        conn.close()
    new_runs = [x for x in runs if x.get("id") not in existing]
    if new_runs:
        store.upsert_rows("scrape_runs", new_runs, None)
    state["runs_done"] = True
    save_state(state)
    print(f"migrated {len(new_runs)} scrape_runs ({len(runs) - len(new_runs)} already present)", flush=True)


def main() -> None:
    store.init_db()
    state = load_state()
    with httpx.Client() as client:
        migrate_scrape_runs(client, state)
        while True:
            rows = fetch_page(client, state["last_id"])
            if not rows:
                break
            for row in rows:
                emb = row.get("embedding")
                if isinstance(emb, str):  # PostgREST serializes vector as "[...]"
                    row["embedding"] = json.loads(emb)
            store.upsert_rows("skills", rows, "url")
            state["last_id"] = rows[-1]["id"]
            state["migrated"] += len(rows)
            save_state(state)
            if state["migrated"] % 2000 < PAGE_SIZE:
                print(f"migrated {state['migrated']} skills...", flush=True)
    print(f"MIGRATION DONE - {state['migrated']} skills total", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
