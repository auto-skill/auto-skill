"""Full SkillsMP harvest via adaptive query sharding.

skillsmp.com's search API returns GitHub URLs directly but caps every query
at 49 pages x 50 = 2,450 results, and there is no browse-all endpoint (the
sitemap only covers the top 50k). So: start with single-character queries
and, whenever a shard comes back capped, split it into finer prefixes
(a -> aa, ab, ...) until every shard fits under the cap. Rows are upserted
straight into local_skills.db (dedup on url); the recommender's embed loop
picks them up automatically.

Resumable: the shard queue is checkpointed to skillsmp_state.json after every
shard, so kill/re-run continues where it left off.

Run:  python harvest_skillsmp.py
"""
import json
import sys
import time
from pathlib import Path

import httpx

import local_store as store
from scraper import normalize_url

API = "https://skillsmp.com/api/v1/skills/search"
CHARSET = "abcdefghijklmnopqrstuvwxyz0123456789"
PAGE_LIMIT = 50
MAX_PAGES = 49
CAP = MAX_PAGES * PAGE_LIMIT  # a shard reporting this many results is capped
MAX_SHARD_LEN = 4
REQUEST_GAP_SECONDS = 0.35
STATE_FILE = Path(__file__).parent / "skillsmp_state.json"


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"queue": list(CHARSET), "harvested": 0, "requests": 0}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state))


def fetch_page(client: httpx.Client, q: str, page: int) -> dict | None:
    for attempt in range(3):
        time.sleep(REQUEST_GAP_SECONDS)
        try:
            r = client.get(
                API,
                params={"q": q, "page": page, "limit": PAGE_LIMIT},
                headers={"Accept": "application/json"},
                timeout=30,
            )
            if r.status_code == 200:
                return r.json().get("data", {})
            if r.status_code in (400, 404):
                return None
        except httpx.HTTPError:
            pass
        time.sleep(2 ** attempt)
    return None


def item_to_row(item: dict) -> dict | None:
    url = normalize_url(item.get("githubUrl") or item.get("skillUrl") or "")
    if not url:
        return None
    return {
        "name": item.get("name") or url,
        "description": item.get("description") or "",
        "source": "skillsmp",
        "url": url,
        "tags": [],
        "raw": {
            "author": item.get("author"),
            "stars": item.get("stars"),
            "skill_page": item.get("skillUrl"),
            "updated_at": item.get("updatedAt"),
        },
    }


def harvest_shard(client: httpx.Client, q: str, seen: set, state: dict) -> bool:
    """Harvest one query shard. Returns True when the shard hit the cap and
    should be split into finer prefixes."""
    capped = False
    for page in range(1, MAX_PAGES + 1):
        data = fetch_page(client, q, page)
        state["requests"] += 1
        if data is None:
            return False
        items = data.get("skills") or []
        pagination = data.get("pagination") or {}
        if (pagination.get("total") or 0) >= CAP:
            capped = True
        rows = []
        for item in items:
            row = item_to_row(item)
            if row and row["url"] not in seen:
                seen.add(row["url"])
                rows.append(row)
        if rows:
            store.upsert_rows("skills", rows, "url")
            state["harvested"] += len(rows)
        if not items or not pagination.get("hasNext"):
            break
    return capped


def main() -> None:
    store.init_db()
    state = load_state()
    seen: set = set()
    with httpx.Client() as client:
        while state["queue"]:
            q = state["queue"].pop(0)
            capped = harvest_shard(client, q, seen, state)
            if capped and len(q) < MAX_SHARD_LEN:
                state["queue"].extend(q + c for c in CHARSET)
            save_state(state)
            print(
                f"shard '{q}' done (capped={capped}) — {state['harvested']:,} new urls, "
                f"{state['requests']:,} requests, {len(state['queue']):,} shards left",
                flush=True,
            )
    print(f"HARVEST DONE — {state['harvested']:,} new skills from skillsmp", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
