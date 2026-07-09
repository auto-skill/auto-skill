import asyncio
import hashlib
import json
import re
import tempfile
import time
import httpx
import uvicorn
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from datetime import datetime, timezone, date, timedelta
import os
from quality import content_hash as quality_content_hash, evaluate_quality

# Storage moved local 2026-07-05 (Supabase free-tier space ran out) -- new
# skills now go into local_skills.db via local_api.py's router, mounted below
# on this same app/port. That router speaks the same tiny REST+RPC surface
# Supabase did, so pointing SUPABASE_URL at our own loopback address is the
# only change the rest of this file needed. Old Supabase data is untouched;
# the public connector still reads from it separately.
SUPABASE_URL = os.getenv("LOCAL_DB_URL", f"http://127.0.0.1:{os.getenv('LOCAL_DB_PORT', '8000')}").rstrip("/")
HEADERS = {"Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=representation"}
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
# Point this at a self-hosted SearXNG instance (see README) to enable general
# web search. Left unset, scrape_web_search() is skipped entirely.
SEARXNG_URL = os.getenv("SEARXNG_URL", "").rstrip("/")
SCRAPE_INTERVAL_SECONDS = int(os.getenv("SCRAPE_INTERVAL_SECONDS", "900"))
STALE_SCRAPE_RUN_SECONDS = int(os.getenv("STALE_SCRAPE_RUN_SECONDS", "7200"))
AUTO_START_SCRAPER = os.getenv("AUTO_START_SCRAPER", "1").lower() not in {"0", "false", "no"}
API_VERSION = "quality-route-v1"

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# --- Public read-only guard ------------------------------------------------
# When this app is exposed to the internet through a tunnel (cloudflared runs
# on this machine and proxies to loopback), tunneled requests carry forwarding
# headers while genuinely local callers (scraper itself, recommender, hook,
# mcp_server) do not. Public callers get search/read endpoints, the /auth,
# /mcp-oauth, and /favorites/installs/private-skills/runs account endpoints
# (each of those enforces its own bearer-token auth in accounts_api.py -- this
# guard just decides what reaches FastAPI at all), and /route -- the local
# REST surface has no auth of its own, so every /rest/v1 path must stay
# loopback-only.
PUBLIC_GET_PATHS = frozenset(
    {"/", "/healthz", "/readyz", "/status", "/find-semantic", "/favorites", "/installs", "/private-skills", "/runs"}
)
PUBLIC_GET_PREFIXES = ("/content/", "/auth/", "/mcp-oauth/")
PUBLIC_POST_PATHS = frozenset({"/route", "/favorites", "/installs", "/private-skills"})
PUBLIC_POST_PREFIXES = ("/auth/", "/mcp-oauth/")
PUBLIC_DELETE_PREFIXES = ("/favorites/", "/private-skills/")


def public_api_allows(method: str, path: str) -> bool:
    path = path.rstrip("/") or "/"
    method = method.upper()
    if method == "GET":
        return path in PUBLIC_GET_PATHS or any(path.startswith(prefix) for prefix in PUBLIC_GET_PREFIXES)
    if method == "POST":
        return path in PUBLIC_POST_PATHS or any(path.startswith(prefix) for prefix in PUBLIC_POST_PREFIXES)
    if method == "DELETE":
        return any(path.startswith(prefix) for prefix in PUBLIC_DELETE_PREFIXES)
    return False


@app.middleware("http")
async def public_readonly_guard(request, call_next):
    from fastapi.responses import JSONResponse
    # CORS preflight has no side effects and must reach CORSMiddleware (added
    # below) to get its Access-Control-Allow-* headers -- this guard runs
    # outermost, so blocking OPTIONS here would silently break every
    # cross-origin browser call that sends a custom header (e.g. the
    # dashboard's Authorization bearer), well before the real request this
    # guard is meant to gate is ever made.
    if request.method == "OPTIONS":
        return await call_next(request)
    is_public = bool(request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for"))
    if is_public:
        if not public_api_allows(request.method, request.url.path):
            return JSONResponse({"error": "read-only public API"}, status_code=403)
    return await call_next(request)

# Local SQLite-backed store, replacing Supabase for new writes (see SUPABASE_URL
# above) -- mounted first so it's ready before the recommender's startup hook
# tries to reach it.
from local_api import router as local_db_router  # noqa: E402
import local_store as store  # noqa: E402
app.include_router(local_db_router)

# Accounts: OAuth login (Google/GitHub) and per-user favorites/installs/private
# skills, all backed by the same local SQLite store -- see auth.py.
from accounts_api import router as accounts_router  # noqa: E402
app.include_router(accounts_router)

# MCP OAuth authorization server endpoints for the hosted connector -- see
# mcp_oauth.py.
from mcp_oauth import router as mcp_oauth_router  # noqa: E402
app.include_router(mcp_oauth_router)

# Semantic recommender (hybrid pgvector search + optional Ollama chat) lives in
# its own module; it also embeds newly scraped skills in the background.
from recommender import router as recommender_router  # noqa: E402
app.include_router(recommender_router)

_library_files_dir = os.path.join(os.path.dirname(__file__), "skills_library", "files")
os.makedirs(_library_files_dir, exist_ok=True)
app.mount("/library/files", StaticFiles(directory=_library_files_dir), name="library_files")

scrape_task = None


class ScrapeAlreadyRunning(RuntimeError):
    """Raised when another fresh scrape run is already active."""


@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": "auto-skill-api", "api_version": API_VERSION}


@app.get("/readyz")
async def readyz():
    def _probe():
        conn = store.get_conn()
        try:
            total = conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
            active = conn.execute(
                "SELECT COUNT(*) FROM skills WHERE COALESCE(quality_status, 'active') = 'active'"
            ).fetchone()[0]
            embedded = conn.execute("SELECT COUNT(*) FROM skills WHERE embedding IS NOT NULL").fetchone()[0]
            vector_index = store.vector_index_stats()
            scraper_summary = store.scrape_run_summary(STALE_SCRAPE_RUN_SECONDS)
            return {
                "total_skills": total,
                "active_skills": active,
                "embedded_skills": embedded,
                "vector_index": vector_index,
                "scraper": scraper_summary,
            }
        finally:
            conn.close()

    try:
        counts = await asyncio.to_thread(_probe)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)[:200]}, status_code=503)

    ready = counts["total_skills"] > 0 and counts["active_skills"] > 0 and counts["embedded_skills"] > 0
    status_code = 200 if ready else 503
    return JSONResponse({"ok": status_code == 200, **counts}, status_code=status_code)


class RateLimiter:
    """Sliding-window limiter; also used as the fallback when an API gives no rate-limit headers."""

    def __init__(self, max_calls: int, period: float):
        self.max_calls = max_calls
        self.period = period
        self.calls: deque = deque()
        self.lock = asyncio.Lock()

    async def acquire(self):
        async with self.lock:
            now = time.monotonic()
            while self.calls and now - self.calls[0] >= self.period:
                self.calls.popleft()
            if len(self.calls) >= self.max_calls:
                await asyncio.sleep(self.period - (now - self.calls[0]))
            self.calls.append(time.monotonic())


# GitHub search endpoints have their own strict secondary rate limits:
# ~10/min unauthenticated, 30/min authenticated for repo search; code search is stricter.
github_search_limiter = RateLimiter(28 if GITHUB_TOKEN else 9, 60)
github_code_limiter = RateLimiter(14 if GITHUB_TOKEN else 4, 60)

# Self-hosted, so there's no external quota to protect — just avoid hammering it.
web_search_limiter = RateLimiter(2, 1.0)

# Core (non-search) GitHub API: tree fetches, repo metadata, topics. Generous
# because the real guard is the 5000/hr quota, enforced by RunBudget below.
github_core_limiter = RateLimiter(60, 60)

if not GITHUB_TOKEN:
    print(
        "[scraper] WARNING: GITHUB_TOKEN is not set. GitHub code search returns 401 "
        "unauthenticated and the core-API budget drops from 5000/hr to 60/hr, so "
        "most skill discovery will be skipped. Run via start_scraper.ps1 (which "
        "injects `gh auth token`) or set GITHUB_TOKEN."
    )


# --- URL normalization ---------------------------------------------------
# Dedup was exact-string, so e.g. github.com/3aKHP/... and github.com/3akhp/...
# counted as two skills. Normalize at the dedup choke points only; scrapers
# keep appending raw URLs.
TRACKING_PARAMS_RE = re.compile(r"^(utm_\w+|ref|ref_src)$", re.I)


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return url
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = parts.netloc.lower()
    path = parts.path
    if host in ("github.com", "www.github.com"):
        host = "github.com"
        # Owner/repo are case-insensitive on GitHub; deeper tree paths are not.
        segs = path.strip("/").split("/")
        segs[:2] = [s.lower() for s in segs[:2]]
        path = "/" + "/".join(segs) if segs and segs[0] else ""
    if path.endswith(".git"):
        path = path[:-4]
    path = path.rstrip("/")
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not TRACKING_PARAMS_RE.match(k)])
    return urlunsplit(("https", host, path, query, ""))


# --- SKILL.md frontmatter parsing ----------------------------------------
# Flat YAML only (name/description/allowed-tools etc.) — regex-based to avoid a
# PyYAML dependency; this function is the single swap point if that changes.
FRONTMATTER_RE = re.compile(r"\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
FRONTMATTER_KEY_RE = re.compile(r"^([A-Za-z0-9_-]+):\s*(.*)$")


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Returns (fields, body_after_frontmatter). Nested maps/lists are ignored."""
    match = FRONTMATTER_RE.match(text or "")
    if not match:
        return {}, text or ""
    block, body = match.group(1), (text or "")[match.end():]
    fields: dict = {}
    lines = block.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        km = FRONTMATTER_KEY_RE.match(line)
        if not km:
            continue
        key, value = km.group(1), km.group(2).strip()
        if value in (">", "|", ">-", "|-"):
            folded = []
            while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                folded.append(lines[i].strip())
                i += 1
            fields[key] = " ".join(f for f in folded if f)
        elif value.startswith("[") and value.endswith("]"):
            fields[key] = [_strip_quotes(v) for v in value[1:-1].split(",") if v.strip()]
        elif value:
            fields[key] = _strip_quotes(value)
        else:
            # block list ("key:" followed by "- item" lines); nested maps ignored
            items = []
            while i < len(lines) and lines[i].lstrip().startswith("- "):
                items.append(_strip_quotes(lines[i].lstrip()[2:]))
                i += 1
            if items:
                fields[key] = items
    return fields, body


# --- Persistent crawl state ----------------------------------------------
# Local JSON (not Supabase) tracking which repos were tree-crawled at which
# commit/ETag, deep-sweep rotation cursors, and the backlog of known-but-not-
# yet-crawled repos. Lets every run skip unchanged repos for free (304s).
CRAWL_STATE_PATH = Path(__file__).parent / "skills_library" / "crawl_state.json"


class CrawlState:
    def __init__(self, data: dict):
        self.repos: dict = data.get("repos", {})
        self.deep_sweep: dict = data.get("deep_sweep", {"repo_query_cursor": 0, "code_query_cursor": 0, "last_sweep_at": {}})
        self.backlog: list = data.get("backlog", [])
        self.topics_expanded_at: str = data.get("topics_expanded_at", "")
        self.extra_topic_queries: list = data.get("extra_topic_queries", [])

    @classmethod
    def load(cls) -> "CrawlState":
        try:
            return cls(json.loads(CRAWL_STATE_PATH.read_text(encoding="utf-8")))
        except Exception:
            return cls({})

    def save(self):
        # Merge the on-disk backlog first: /seed-backlog may have queued repos
        # while a long scrape held this state in memory.
        try:
            disk = json.loads(CRAWL_STATE_PATH.read_text(encoding="utf-8"))
            ours = set(self.backlog) | set(self.repos)
            self.backlog += [b for b in disk.get("backlog", []) if b not in ours]
        except Exception:
            pass
        data = {
            "repos": self.repos,
            "deep_sweep": self.deep_sweep,
            "backlog": self.backlog,
            "topics_expanded_at": self.topics_expanded_at,
            "extra_topic_queries": self.extra_topic_queries,
        }
        CRAWL_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(CRAWL_STATE_PATH.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, CRAWL_STATE_PATH)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass


class RunBudget:
    """Per-run API call caps. The RateLimiters pace requests; these caps bound how
    much of the hourly GitHub quota one run may consume (loop is gap-based, so
    worst case ~4 runs/hr: 4x700 core + search stays under the 5000/30-per-min limits)."""

    CAPS = {"core": 700, "search": 185, "code_search": 100}

    def __init__(self):
        self.used = {k: 0 for k in self.CAPS}

    def take(self, kind: str) -> bool:
        if self.used[kind] >= self.CAPS[kind]:
            return False
        self.used[kind] += 1
        return True


SKILL_COLUMNS = (
    "name",
    "description",
    "source",
    "url",
    "tags",
    "raw",
    "risk_score",
    "risk_flags",
    "scanned_at",
    "content_hash",
    "canonical_id",
    "quality_status",
    "quality_reasons",
    "quality_score",
    "platforms",
    "category",
)


def skill_to_row(skill: dict) -> dict:
    """Skills carry transient keys (_content, _repo_key) during a run; the Supabase
    upsert 400s on unknown columns, so whitelist exactly the table's columns."""
    return {k: skill[k] for k in SKILL_COLUMNS if k in skill}


async def github_get(client: httpx.AsyncClient, url: str, params: dict, headers: dict, limiter: RateLimiter, max_retries: int = 4):
    """GET with rate limiting plus 403/429 backoff honoring Retry-After / X-RateLimit-Reset."""
    for _ in range(max_retries):
        await limiter.acquire()
        try:
            r = await client.get(url, params=params, headers=headers, timeout=15)
        except Exception:
            return None

        if r.status_code in (403, 429):
            retry_after = r.headers.get("retry-after")
            reset = r.headers.get("x-ratelimit-reset")
            if retry_after:
                wait = float(retry_after)
            elif reset:
                wait = max(float(reset) - time.time(), 1)
            else:
                wait = 30
            await asyncio.sleep(min(wait, 120))
            continue

        remaining = r.headers.get("x-ratelimit-remaining")
        if remaining == "0":
            reset = r.headers.get("x-ratelimit-reset")
            if reset:
                await asyncio.sleep(max(float(reset) - time.time(), 0) + 1)
        return r
    return None


async def supabase_post(client: httpx.AsyncClient, table: str, data: dict | list, on_conflict: str = ""):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    if on_conflict:
        url += f"?on_conflict={on_conflict}"
    r = await client.post(url, json=data, headers=HEADERS)
    return r

async def supabase_patch(client: httpx.AsyncClient, table: str, match: dict, data: dict):
    params = "&".join(f"{k}=eq.{v}" for k, v in match.items())
    r = await client.patch(f"{SUPABASE_URL}/rest/v1/{table}?{params}", json=data, headers=HEADERS)
    return r

async def supabase_get(client: httpx.AsyncClient, table: str, params: str = ""):
    r = await client.get(f"{SUPABASE_URL}/rest/v1/{table}?{params}", headers=HEADERS)
    return r.json()

async def supabase_delete(client: httpx.AsyncClient, table: str, match: dict):
    params = "&".join(f"{k}=eq.{v}" for k, v in match.items())
    return await client.delete(f"{SUPABASE_URL}/rest/v1/{table}?{params}", headers=HEADERS)


GITHUB_REPO_QUERIES = [
    "claude skill in:name,description,readme",
    "claude-code skill in:name,description",
    "mcp server claude in:name,description",
    "topic:claude-skill",
    "topic:claude-skills",
    "topic:claude-code",
    "topic:claude-code-skill",
    "topic:claude-code-skills",
    "topic:agent-skills",
    "topic:agent-skill",
    "topic:mcp-server claude",
    "topic:model-context-protocol claude",
    "topic:awesome-claude-code",
    "anthropic skill in:name,description",
    "claude plugin in:name,description",
    "claude marketplace skill in:name,description,readme",
    "claude subagent in:name,description,readme",
    "claude slash command in:name,description,readme",
    "\"SKILL.md\" claude in:readme",
    "\"claude skills\" in:readme",
    "\"claude code\" skills marketplace in:readme",
]

# GitHub code search finds actual skill files living inside larger repos, which
# repo/description search misses entirely. Ordered precise → broad; the bare
# "filename:SKILL.md" query has ~3.9M hits that are overwhelmingly NOT Claude
# skills, so it is deliberately absent — coverage of real skills comes from the
# qualified queries plus the tree crawl of every repo these hits reveal.
GITHUB_CODE_QUERIES = [
    "path:.claude/skills filename:SKILL.md",
    "\"allowed-tools\" filename:SKILL.md",
    "\"description:\" \"name:\" filename:SKILL.md path:skills",
    "path:.claude/agents extension:md",
    "path:.claude/commands extension:md",
    "filename:.mcp.json path:/",
    "filename:SKILL.md claude",
    "filename:CLAUDE.md skill",
    "path:.claude/skills",
    "path:skills filename:SKILL.md",
]

SEARCH_EPOCH = date(2023, 1, 1)  # claude-code/SKILL.md ecosystem postdates 2023
DEEP_SWEEP_QUERIES_PER_RUN = 2
DEEP_SWEEP_MIN_HOURS = 24  # each query gets at most one full sweep per day
MAX_SEARCH_PAGES = 10      # GitHub search hard-caps at 1000 results = 10x100


def _repo_item_to_skill(repo: dict) -> dict:
    return {
        "name": repo.get("name", ""),
        "description": repo.get("description") or "",
        "source": "github",
        "url": repo.get("html_url", ""),
        "tags": repo.get("topics", []),
        "raw": {
            "stars": repo.get("stargazers_count", 0),
            "owner": repo.get("owner", {}).get("login"),
            "updated_at": repo.get("updated_at"),
            "language": repo.get("language"),
        },
    }


def _code_item_to_skill(item: dict) -> dict:
    repo = item.get("repository", {})
    owner_repo = repo.get("full_name") or f"{repo.get('owner', {}).get('login', '')}/{repo.get('name', '')}"
    path = item.get("path", "")
    if SKILL_MD_PATH_RE.search(path):
        # The hit IS a skill: point at its directory; the tree crawl of the parent
        # repo (Tier A via the skill-file tag) fills in frontmatter + siblings.
        dir_path = path[: -len("SKILL.md")].rstrip("/")
        url = f"https://github.com/{owner_repo}/tree/HEAD/{dir_path}" if dir_path else repo.get("html_url", "")
        return {
            "name": dir_path.split("/")[-1] if dir_path else repo.get("name", ""),
            "description": f"Skill at {path} in {owner_repo}",
            "source": "github_skill_file",
            "url": url,
            "tags": ["skill-file"],
            "raw": {"parent_repo": owner_repo, "path": path, "valid_skill": False},
        }
    kind = "subagent" if "/agents/" in f"/{path}" else "slash-command" if "/commands/" in f"/{path}" else "skill-file"
    return {
        "name": repo.get("name", ""),
        "description": f"Contains {path}",
        "source": "github",
        "url": repo.get("html_url", ""),
        "tags": [kind],
        "raw": {"owner": repo.get("owner", {}).get("login"), "path": path},
    }


async def github_search_count(client: httpx.AsyncClient, endpoint: str, q: str, headers: dict, limiter: RateLimiter) -> int:
    r = await github_get(client, f"https://api.github.com/search/{endpoint}", {"q": q, "per_page": 1}, headers, limiter)
    if r is None or r.status_code != 200:
        return -1
    return r.json().get("total_count", 0)


async def _paginate_search(client, endpoint, q, headers, limiter, budget, budget_kind, emit, sort=""):
    for page in range(1, MAX_SEARCH_PAGES + 1):
        if not budget.take(budget_kind):
            return
        params = {"q": q, "per_page": 100, "page": page}
        if sort:
            params["sort"] = sort
        r = await github_get(client, f"https://api.github.com/search/{endpoint}", params, headers, limiter)
        if r is None or r.status_code != 200:
            return
        items = r.json().get("items", [])
        for item in items:
            emit(item)
        if len(items) < 100:
            return


async def sliced_repo_search(client, q, headers, budget, emit):
    """Full sweep of a repo query past GitHub's 1000-result ceiling: adaptively
    bisect created:-date ranges until each slice fits in 1000 results."""
    stack = [(SEARCH_EPOCH, date.today())]
    while stack:
        lo, hi = stack.pop()
        if not budget.take("search"):
            return False
        total = await github_search_count(client, "repositories", f"{q} created:{lo}..{hi}", headers, github_search_limiter)
        if total == 0:
            continue
        if total < 0:
            return False
        if total <= 1000 or lo == hi:
            await _paginate_search(client, "repositories", f"{q} created:{lo}..{hi}", headers,
                                   github_search_limiter, budget, "search", emit)
        else:
            mid = lo + (hi - lo) // 2
            stack.append((lo, mid))
            stack.append((mid + timedelta(days=1), hi))
    return True


CODE_SEARCH_MAX_SIZE = 393216  # code search only indexes files < 384 KB


async def sliced_code_search(client, q, headers, budget, emit):
    """Same adaptive slicing for code search, over size: byte ranges (legacy code
    search has no created: qualifier)."""
    stack = [(0, CODE_SEARCH_MAX_SIZE)]
    while stack:
        lo, hi = stack.pop()
        if not budget.take("code_search"):
            return False
        total = await github_search_count(client, "code", f"{q} size:{lo}..{hi}", headers, github_code_limiter)
        if total == 0:
            continue
        if total < 0:
            return False
        if total <= 1000 or lo >= hi:
            await _paginate_search(client, "code", f"{q} size:{lo}..{hi}", headers,
                                   github_code_limiter, budget, "code_search", emit)
        else:
            mid = (lo + hi) // 2
            stack.append((lo, mid))
            stack.append((mid + 1, hi))
    return True


async def expand_topic_queries(client, gh_headers, state: "CrawlState"):
    """Once a day, discover new skill-ish GitHub topics (claude-skills-marketplace
    etc.) so new naming conventions are picked up without code changes."""
    try:
        last = datetime.fromisoformat(state.topics_expanded_at) if state.topics_expanded_at else None
        if last and (datetime.now(timezone.utc) - last).total_seconds() < 86400:
            return
    except ValueError:
        pass
    found = []
    for tq in ("claude", "mcp"):
        r = await github_get(client, "https://api.github.com/search/topics",
                             {"q": tq, "per_page": 100}, gh_headers, github_search_limiter)
        if r is None or r.status_code != 200:
            continue
        for topic in r.json().get("items", []):
            name = topic.get("name", "")
            if SKILLISH_RE.search(name) and (topic.get("repository_count") or 0) >= 3:
                found.append(f"topic:{name}")
    state.extra_topic_queries = sorted(set(found))[:15]
    state.topics_expanded_at = datetime.now(timezone.utc).isoformat()


def _pick_deep_sweep_queries(queries: list, cursor_key: str, state: "CrawlState", count: int) -> list:
    """Round-robin: next `count` queries from the rotation whose last full sweep
    is older than DEEP_SWEEP_MIN_HOURS."""
    picked = []
    last_sweep = state.deep_sweep.setdefault("last_sweep_at", {})
    cursor = state.deep_sweep.get(cursor_key, 0)
    for _ in range(len(queries)):
        if len(picked) >= count:
            break
        q = queries[cursor % len(queries)]
        cursor += 1
        stamp = last_sweep.get(q)
        try:
            if stamp and (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds() < DEEP_SWEEP_MIN_HOURS * 3600:
                continue
        except ValueError:
            pass
        picked.append(q)
    state.deep_sweep[cursor_key] = cursor % len(queries)
    return picked


async def scrape_github(client: httpx.AsyncClient, skills: list, state: "CrawlState", budget: RunBudget):
    gh_headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        gh_headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    await expand_topic_queries(client, gh_headers, state)
    repo_queries = GITHUB_REPO_QUERIES + [q for q in state.extra_topic_queries if q not in GITHUB_REPO_QUERIES]

    seen = set()

    def emit_repo(repo):
        url = repo.get("html_url", "")
        if url and url not in seen:
            seen.add(url)
            skills.append(_repo_item_to_skill(repo))

    def emit_code(item):
        skill = _code_item_to_skill(item)
        if skill["url"] and skill["url"] not in seen:
            seen.add(skill["url"])
            skills.append(skill)

    # Cheap incremental pass every run: recently-updated results of every query.
    for q in repo_queries:
        for page in (1, 2, 3):
            if not budget.take("search"):
                break
            try:
                r = await github_get(
                    client,
                    "https://api.github.com/search/repositories",
                    {"q": q, "per_page": 100, "page": page, "sort": "updated"},
                    gh_headers,
                    github_search_limiter,
                )
                if r is None or r.status_code != 200:
                    break
                items = r.json().get("items", [])
                for repo in items:
                    emit_repo(repo)
                if len(items) < 100:
                    break
            except Exception:
                break

    # Deep sweeps: a rotating few queries per run get a full, ceiling-breaking
    # sweep, so every query is exhaustively covered every ~day of uptime.
    last_sweep = state.deep_sweep.setdefault("last_sweep_at", {})
    for q in _pick_deep_sweep_queries(repo_queries, "repo_query_cursor", state, DEEP_SWEEP_QUERIES_PER_RUN):
        try:
            if await sliced_repo_search(client, q, gh_headers, budget, emit_repo):
                last_sweep[q] = datetime.now(timezone.utc).isoformat()
        except Exception:
            pass

    # Code search (auth required): rotate through queries under the code budget.
    if GITHUB_TOKEN:
        for q in _pick_deep_sweep_queries(GITHUB_CODE_QUERIES, "code_query_cursor", state, len(GITHUB_CODE_QUERIES)):
            try:
                if await sliced_code_search(client, q, gh_headers, budget, emit_code):
                    last_sweep[q] = datetime.now(timezone.utc).isoformat()
                else:
                    break  # budget exhausted
            except Exception:
                pass


async def scrape_npm(client: httpx.AsyncClient, skills: list):
    queries = [
        "claude skill", "claude-code skill", "mcp server claude", "anthropic claude skill",
        "claude subagent", "claude plugin", "claude marketplace", "keywords:claude",
        "keywords:claude-code", "keywords:claude-skill", "keywords:mcp-server",
        "model context protocol claude", "claude agent skill",
    ]
    seen = set()
    for q in queries:
        try:
            r = await client.get(
                "https://registry.npmjs.org/-/v1/search",
                params={"text": q, "size": 50},
                timeout=15,
            )
            if r.status_code != 200:
                continue
            for obj in r.json().get("objects", []):
                pkg = obj["package"]
                url = pkg.get("links", {}).get("npm") or f"https://www.npmjs.com/package/{pkg['name']}"
                if url in seen:
                    continue
                seen.add(url)
                skills.append({
                    "name": pkg["name"],
                    "description": pkg.get("description") or "",
                    "source": "npm",
                    "url": url,
                    "tags": pkg.get("keywords", []),
                    "raw": {
                        "version": pkg.get("version"),
                        "publisher": pkg.get("publisher", {}).get("username"),
                        "date": pkg.get("date"),
                    },
                })
        except Exception:
            pass
        await asyncio.sleep(0.3)


# --- Monorepo tree crawl ---------------------------------------------------
# A repo holding 100 skills used to be stored as ONE row. For every skill-ish
# repo we discover, walk its git tree, find each */SKILL.md, and register it as
# an individual skill with metadata parsed from its frontmatter. Only entries
# whose frontmatter has both name and description are flagged valid_skill —
# that's Claude's own bar for an uploadable skill.

SEED_TREE_CRAWL_REPOS = [
    "anthropics/skills",
    "anthropics/claude-code",
    "davila7/claude-code-templates",
    "obra/superpowers",
    "VoltAgent/awesome-claude-code-subagents",
    "wshobson/agents",
]

SKILLISH_RE = re.compile(r"skill|plugin|subagent|agent|\.claude|claude-code|mcp", re.I)
SKILLISH_TOPICS = {"claude-skill", "claude-skills", "agent-skill", "agent-skills"}
SKILL_MD_PATH_RE = re.compile(r"(?:^|/)SKILL\.md$", re.I)
TREE_CRAWL_PER_REPO_CAP = 500  # bound pathological repos
BACKLOG_DRAIN_PER_RUN = 150    # ~26k backlog fully swept in ~2 days at 4 runs/hr
RAW_FETCH_SEMAPHORE = asyncio.Semaphore(20)


def _github_owner_repo(url: str):
    match = GITHUB_OWNER_REPO_RE.search(url or "")
    if not match:
        return None
    return match.group(1).lower(), match.group(2).lower()


def collect_repo_candidates(skills: list, state: "CrawlState") -> list:
    """This run's tree-crawl worklist, ordered by priority tier:
    A) seeds + repos with a code-search SKILL.md hit + skill-topic repos,
    B) repos discovered this run that look skill-ish,
    C) backlog drain (the pre-existing corpus, crawled gradually)."""
    tier_a, tier_b, seen = [], [], set()

    def add(tier, owner_repo, meta=None):
        if owner_repo in seen:
            return
        seen.add(owner_repo)
        tier.append((owner_repo, meta or {}))

    for seed in SEED_TREE_CRAWL_REPOS:
        add(tier_a, seed.lower())

    for s in skills:
        if s.get("source") not in ("github", "github_skill_file"):
            continue
        parsed = _github_owner_repo(s.get("url") or "")
        if not parsed:
            continue
        owner_repo = "/".join(parsed)
        meta = {"stars": s.get("raw", {}).get("stars"), "updated_at": s.get("raw", {}).get("updated_at")}
        tags = set(s.get("tags") or [])
        if "skill-file" in tags or tags & SKILLISH_TOPICS:
            add(tier_a, owner_repo, meta)
        elif SKILLISH_RE.search(f"{s.get('name', '')} {s.get('description', '')} {' '.join(tags)}"):
            add(tier_b, owner_repo, meta)
        else:
            # Not obviously skill-ish now, but cheap to sweep eventually.
            if owner_repo not in state.repos and owner_repo not in state.backlog:
                state.backlog.append(owner_repo)

    tier_c = []
    while state.backlog and len(tier_c) < BACKLOG_DRAIN_PER_RUN:
        owner_repo = state.backlog.pop(0)
        if owner_repo not in seen:
            seen.add(owner_repo)
            tier_c.append((owner_repo, {}))

    return tier_a + tier_b + tier_c


def _should_skip_crawl(owner_repo: str, meta: dict, state: "CrawlState") -> bool:
    stored = state.repos.get(owner_repo)
    if not stored:
        return False
    # Missing/errored repos: don't re-poke for 30 days.
    if stored.get("missing_at"):
        try:
            missing = datetime.fromisoformat(stored["missing_at"])
            if (datetime.now(timezone.utc) - missing).days < 30:
                return True
        except ValueError:
            pass
        return False
    # Unchanged since last crawl (updated_at from repo search is free); repos
    # without that signal still get an ETag-conditional request (304 = free).
    if meta.get("updated_at") and stored.get("pushed_at") == meta["updated_at"]:
        return True
    return False


async def _fetch_raw_file(client: httpx.AsyncClient, owner: str, repo: str, path: str) -> str:
    async with RAW_FETCH_SEMAPHORE:
        try:
            r = await client.get(f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{path}", timeout=10)
            if r.status_code == 200:
                return r.text[:20000]
        except Exception:
            pass
    return ""


def _skill_from_skill_md(owner: str, repo: str, path: str, content: str, meta: dict) -> dict:
    fields, body = parse_frontmatter(content)
    dir_path = path[: -len("SKILL.md")].rstrip("/")
    name = str(fields.get("name") or "").strip()
    description = str(fields.get("description") or "").strip()
    if not name:
        name = dir_path.split("/")[-1] if dir_path else repo
    if not description:
        # first prose paragraph of the body
        for para in re.split(r"\r?\n\s*\r?\n", body):
            para = para.strip()
            if para and not para.startswith(("#", "<", "!", "|", "```")):
                description = re.sub(r"\s+", " ", para)[:300]
                break
    tags = fields.get("tags") if isinstance(fields.get("tags"), list) else []
    if dir_path:
        url = f"https://github.com/{owner}/{repo}/tree/HEAD/{dir_path}"
    else:
        url = f"https://github.com/{owner}/{repo}"
    return {
        "name": name[:200],
        "description": description[:1000],
        "source": "github_skill_file",
        "url": url,
        "tags": list(tags),
        "raw": {
            "parent_repo": f"{owner}/{repo}",
            "path": path,
            "frontmatter": {k: v for k, v in fields.items() if isinstance(v, (str, int, float, bool, list))},
            "stars": meta.get("stars"),
            "updated_at": meta.get("updated_at"),
            # Claude accepts a skill iff SKILL.md frontmatter carries name+description.
            "valid_skill": bool(fields.get("name") and fields.get("description")),
        },
        "_content": content,
    }


async def tree_crawl_repo(client: httpx.AsyncClient, gh_headers: dict, owner_repo: str, meta: dict,
                          state: "CrawlState", budget: RunBudget) -> list:
    """Returns individual-skill rows found in the repo tree, or None if the core
    budget ran out (caller re-queues the repo)."""
    owner, repo = owner_repo.split("/", 1)
    if not budget.take("core"):
        return None
    stored = state.repos.get(owner_repo, {})
    headers = dict(gh_headers)
    if stored.get("etag"):
        headers["If-None-Match"] = stored["etag"]
    r = await github_get(
        client,
        f"https://api.github.com/repos/{owner}/{repo}/git/trees/HEAD",
        {"recursive": "1"},
        headers,
        github_core_limiter,
    )
    now = datetime.now(timezone.utc).isoformat()
    if r is None:
        return []
    if r.status_code == 304:
        state.repos[owner_repo] = {**stored, "pushed_at": meta.get("updated_at") or stored.get("pushed_at"), "last_crawled_at": now}
        return []
    if r.status_code != 200:
        state.repos[owner_repo] = {"missing_at": now, "status": r.status_code}
        return []

    data = r.json()
    paths = [t.get("path", "") for t in data.get("tree", []) if t.get("type") == "blob"]
    skill_paths = [p for p in paths if SKILL_MD_PATH_RE.search(p)][:TREE_CRAWL_PER_REPO_CAP]

    contents = await asyncio.gather(*(_fetch_raw_file(client, owner, repo, p) for p in skill_paths))
    found = [
        _skill_from_skill_md(owner, repo, path, content, meta)
        for path, content in zip(skill_paths, contents)
        if content
    ]
    state.repos[owner_repo] = {
        "tree_sha": data.get("sha", ""),
        "etag": r.headers.get("etag", ""),
        "pushed_at": meta.get("updated_at") or "",
        "last_crawled_at": now,
        "skill_count": len(found),
        "truncated": bool(data.get("truncated")),
    }
    return found


async def expand_repos_to_skills(client: httpx.AsyncClient, skills: list, state: "CrawlState", budget: RunBudget):
    """Tree-crawls this run's candidate repos and appends each SKILL.md found as
    its own skill row. Budget overflow goes back on the backlog for next run."""
    candidates = [
        (owner_repo, meta)
        for owner_repo, meta in collect_repo_candidates(skills, state)
        if not _should_skip_crawl(owner_repo, meta, state)
    ]
    crawl_sem = asyncio.Semaphore(8)
    gh_headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        gh_headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    async def crawl_one(owner_repo, meta):
        async with crawl_sem:
            found = await tree_crawl_repo(client, gh_headers, owner_repo, meta, state, budget)
            if found is None:
                if owner_repo not in state.backlog:
                    state.backlog.append(owner_repo)
                return
            skills.extend(found)

    await asyncio.gather(*(crawl_one(o, m) for o, m in candidates))


MAX_REGISTRY_PAGES = 50  # safety cap so a runaway cursor can't loop forever


async def scrape_mcp_registry(client: httpx.AsyncClient, skills: list):
    # Smithery MCP registry — it's entirely an MCP/skill directory, so crawl every
    # page rather than filtering by keyword, to guarantee full coverage.
    try:
        page = 1
        while page <= MAX_REGISTRY_PAGES:
            r = await client.get(
                "https://registry.smithery.ai/servers",
                params={"pageSize": 100, "page": page},
                timeout=15,
            )
            if r.status_code != 200:
                break
            data = r.json()
            servers = data.get("servers", [])
            if not servers:
                break
            for server in servers:
                qualified_name = server.get("qualifiedName", "")
                if not qualified_name:
                    continue
                skills.append({
                    "name": server.get("displayName") or qualified_name,
                    "description": server.get("description") or "",
                    "source": "smithery_registry",
                    "url": f"https://smithery.ai/server/{qualified_name}",
                    "tags": [],
                    "raw": server,
                })
            pagination = data.get("pagination", {})
            total_pages = pagination.get("totalPages")
            if (total_pages and page >= total_pages) or len(servers) < 100:
                break
            page += 1
    except Exception:
        pass

    # glama.ai MCP registry — cursor-paginated; walk it to the end.
    try:
        cursor = None
        for _ in range(MAX_REGISTRY_PAGES):
            params = {"limit": 100}
            if cursor:
                params["after"] = cursor
            r = await client.get("https://glama.ai/api/mcp/v1/servers", params=params, timeout=15)
            if r.status_code != 200:
                break
            data = r.json()
            servers = data.get("servers", data if isinstance(data, list) else [])
            if not servers:
                break
            for server in servers:
                if not isinstance(server, dict):
                    continue
                server_id = server.get("id", "")
                url = server.get("url") or (f"https://glama.ai/mcp/servers/{server_id}" if server_id else "")
                if not url:
                    continue
                skills.append({
                    "name": server.get("name") or server_id,
                    "description": server.get("description") or "",
                    "source": "glama_registry",
                    "url": url,
                    "tags": server.get("tags", []),
                    "raw": server,
                })
            page_info = data.get("pageInfo", {})
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                break
    except Exception:
        pass

    # Official MCP registry (registry.modelcontextprotocol.io) — cursor-paginated.
    try:
        cursor = None
        for _ in range(MAX_REGISTRY_PAGES):
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            r = await client.get("https://registry.modelcontextprotocol.io/v0/servers", params=params, timeout=15)
            if r.status_code != 200:
                break
            data = r.json()
            servers = data.get("servers", [])
            if not servers:
                break
            for entry in servers:
                server = entry.get("server", entry) if isinstance(entry, dict) else {}
                name = server.get("name", "")
                if not name:
                    continue
                repo_url = (server.get("repository") or {}).get("url") or ""
                skills.append({
                    "name": name,
                    "description": server.get("description") or "",
                    "source": "mcp_official_registry",
                    "url": repo_url or f"https://registry.modelcontextprotocol.io/v0/servers?search={name}",
                    "tags": [],
                    "raw": server,
                })
            cursor = (data.get("metadata") or {}).get("next_cursor")
            if not cursor:
                break
    except Exception:
        pass

    # PulseMCP directory — offset-paginated; walk it to the end.
    try:
        offset = 0
        count_per_page = 100
        for _ in range(MAX_REGISTRY_PAGES):
            r = await client.get(
                "https://api.pulsemcp.com/v0beta/servers",
                params={"count_per_page": count_per_page, "offset": offset},
                timeout=15,
            )
            if r.status_code != 200:
                break
            data = r.json()
            servers = data.get("servers", [])
            if not servers:
                break
            for server in servers:
                url = server.get("source_code_url") or server.get("url") or ""
                if not url:
                    continue
                skills.append({
                    "name": server.get("name", ""),
                    "description": server.get("short_description") or server.get("description") or "",
                    "source": "pulsemcp_registry",
                    "url": url,
                    "tags": [],
                    "raw": server,
                })
            if len(servers) < count_per_page:
                break
            offset += count_per_page
    except Exception:
        pass


# Curated lists that hand-track Claude skills/plugins/MCP servers; scraping
# their markdown catches long-tail entries that never show up in API search.
AWESOME_LIST_URLS = [
    "https://raw.githubusercontent.com/hesreallyhim/awesome-claude-code/main/README.md",
    "https://raw.githubusercontent.com/hesreallyhim/awesome-claude-prompts/main/README.md",
    "https://raw.githubusercontent.com/punkpeye/awesome-mcp-servers/main/README.md",
    "https://raw.githubusercontent.com/wong2/awesome-mcp-servers/main/README.md",
    "https://raw.githubusercontent.com/appcypher/awesome-mcp-servers/main/README.md",
    "https://raw.githubusercontent.com/VoltAgent/awesome-claude-code-subagents/main/README.md",
]

MAX_DISCOVERED_AWESOME_LISTS = 25

MARKDOWN_LINK_RE = re.compile(r"\[([^\]\[]+)\]\((https?://[^\s)]+)\)")

# Boilerplate that shows up in every awesome-list (badges, license links, socials)
# but is never itself a skill/tool being listed.
AWESOME_LIST_LINK_EXCLUDE = (
    "shields.io", "opensource.org", "choosealicense.com", "creativecommons.org",
    "twitter.com", "x.com/", "github.com/sponsors", "buymeacoffee.com",
    "codecov.io", "travis-ci", "github.com/actions",
)


async def _scrape_one_awesome_list(client: httpx.AsyncClient, list_url: str, seen: set, skills: list):
    try:
        r = await client.get(list_url, timeout=15)
        if r.status_code != 200:
            return
        list_name = list_url.split("/")[3] + "/" + list_url.split("/")[4]
        for name, link in MARKDOWN_LINK_RE.findall(r.text):
            if link in seen:
                continue
            if any(bad in link for bad in AWESOME_LIST_LINK_EXCLUDE):
                continue
            # keep only links with a real path (not bare domains/anchors)
            if link.rstrip("/").count("/") < 3:
                continue
            seen.add(link)
            skills.append({
                "name": name.strip() or link,
                "description": f"Listed in {list_name}",
                "source": "awesome_list",
                "url": link,
                "tags": [list_name.split("/")[-1]],
                "raw": {"list": list_url},
            })
    except Exception:
        pass
    await asyncio.sleep(0.3)


async def scrape_awesome_lists(client: httpx.AsyncClient, skills: list, budget: RunBudget):
    seen = set()
    for list_url in AWESOME_LIST_URLS:
        await _scrape_one_awesome_list(client, list_url, seen, skills)

    # Dynamic discovery: any reasonably-starred awesome-* list about claude/mcp
    # gets fed through the same extractor, so new community lists are picked up
    # without hardcoding them.
    gh_headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        gh_headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    known = {u.split("/")[3].lower() + "/" + u.split("/")[4].lower() for u in AWESOME_LIST_URLS}
    discovered = 0
    for q in ("awesome claude in:name", "awesome mcp in:name"):
        if not budget.take("search"):
            break
        r = await github_get(client, "https://api.github.com/search/repositories",
                             {"q": q, "per_page": 100, "sort": "stars"}, gh_headers, github_search_limiter)
        if r is None or r.status_code != 200:
            continue
        for repo in r.json().get("items", []):
            if discovered >= MAX_DISCOVERED_AWESOME_LISTS:
                break
            full = repo.get("full_name", "").lower()
            if (not repo.get("name", "").lower().startswith("awesome-")
                    or repo.get("stargazers_count", 0) < 20 or full in known):
                continue
            known.add(full)
            discovered += 1
            await _scrape_one_awesome_list(
                client, f"https://raw.githubusercontent.com/{repo['full_name']}/HEAD/README.md", seen, skills
            )


# General web search queries — these aren't restricted to any one host, so this
# is what catches blog posts, personal repos, forum threads, gists, etc. that
# the GitHub/npm/registry/awesome-list scrapers above would never see.
WEB_SEARCH_QUERIES = [
    "claude code skill",
    "claude agent skill SKILL.md",
    "claude code subagent",
    "claude code slash command",
    "claude code plugin marketplace",
    "mcp server for claude",
    "model context protocol server claude",
    "anthropic claude skill directory",
    "\"claude skills\" repository",
    "claude code custom skill tutorial",
]

# Domains already covered exhaustively by the dedicated scrapers above — skip
# them here so web search results don't just duplicate what we already have.
WEB_SEARCH_EXCLUDE_DOMAINS = ("github.com", "npmjs.com", "smithery.ai", "glama.ai", "pulsemcp.com")


SKILLSMP_QUERIES = [
    "claude", "claude code", "claude skill", "claude subagent", "claude plugin",
    "mcp", "mcp server", "model context protocol", "agent skill", "anthropic",
    "slash command", "SKILL.md",
]
SKILLSMP_LIMIT = 50
SKILLSMP_MAX_PAGES = 20  # API caps results at ~1000/query (20 pages x 50) regardless of query breadth
skillsmp_limiter = RateLimiter(5, 1.0)


async def scrape_skillsmp(client: httpx.AsyncClient, skills: list):
    # skillsmp.com indexes GitHub-hosted skills specifically (SkillsMP claims
    # ~2M) and exposes an open, unauthenticated JSON search API pointing
    # straight at each skill's GitHub path — broader coverage of long-tail
    # skills than our own targeted GitHub queries above.
    seen = set()
    for q in SKILLSMP_QUERIES:
        for page in range(1, SKILLSMP_MAX_PAGES + 1):
            await skillsmp_limiter.acquire()
            try:
                r = await client.get(
                    "https://skillsmp.com/api/v1/skills/search",
                    params={"q": q, "page": page, "limit": SKILLSMP_LIMIT},
                    headers={"Accept": "application/json"},
                    timeout=15,
                )
                if r.status_code != 200:
                    break
                data = r.json().get("data", {})
                items = data.get("skills", [])
                if not items:
                    break
                for item in items:
                    url = item.get("githubUrl") or item.get("skillUrl") or ""
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    skills.append({
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
                    })
                if not data.get("pagination", {}).get("hasNext"):
                    break
            except Exception:
                break


def _emit_web_result(skills: list, seen: set, q: str, url: str, title: str, snippet: str, engine: str):
    if not url or url in seen:
        return
    if any(domain in url for domain in WEB_SEARCH_EXCLUDE_DOMAINS):
        return
    seen.add(url)
    skills.append({
        "name": title or url,
        "description": snippet or "",
        "source": "web_search",
        "url": url,
        "tags": [],
        "raw": {"query": q, "engine": engine},
    })


async def scrape_web_search(client: httpx.AsyncClient, skills: list):
    # Preferred engine: a self-hosted SearXNG instance (SEARXNG_URL) — public
    # SearXNG instances and hosted search APIs are bot-walled or need signup.
    # Fallback engine: the ddgs package (DuckDuckGo scraper) — no key and no
    # infrastructure, just occasionally rate-limited, which we absorb with a
    # pause per query and by treating failures as skips.
    seen = set()
    if SEARXNG_URL:
        for q in WEB_SEARCH_QUERIES:
            await web_search_limiter.acquire()
            try:
                r = await client.get(
                    f"{SEARXNG_URL}/search",
                    params={"q": q, "format": "json"},
                    timeout=15,
                )
                if r.status_code != 200:
                    continue
                for item in r.json().get("results", []):
                    _emit_web_result(skills, seen, q, item.get("url", ""),
                                     item.get("title", ""), item.get("content", ""), item.get("engine", "searxng"))
            except Exception:
                pass
        return

    try:
        from ddgs import DDGS
    except ImportError:
        return

    def _ddg_query(query: str):
        try:
            with DDGS() as ddgs:
                return list(ddgs.text(query, max_results=50))
        except Exception:
            return []

    for q in WEB_SEARCH_QUERIES:
        results = await asyncio.to_thread(_ddg_query, q)
        for item in results:
            _emit_web_result(skills, seen, q, item.get("href", ""),
                             item.get("title", ""), item.get("body", ""), "ddgs")
        await asyncio.sleep(3)  # be gentle; DDG rate-limits aggressive clients


# --- Risk scanning -----------------------------------------------------
# Static heuristics over each skill's readable content (SKILL.md/README/package
# metadata): dangerous shell/exec patterns, reverse shells, obfuscated payloads,
# credential exfiltration, and prompt-injection phrasing aimed at Claude itself
# (since these are Claude skills, a hidden "ignore your instructions and..."
# in a SKILL.md is exactly the kind of malicious content we're looking for).
RISK_PATTERNS = [
    ("curl-pipe-shell", re.compile(r"curl\s+[^\n|]*\|\s*(sudo\s+)?(sh|bash|zsh)\b"), 3),
    ("wget-pipe-shell", re.compile(r"wget\s+[^\n|]*\|\s*(sudo\s+)?(sh|bash|zsh)\b"), 3),
    ("rm-rf-root", re.compile(r"rm\s+-rf\s+/(?!\S)"), 3),
    ("base64-decode-exec", re.compile(r"base64\s+(-d|--decode)[^\n]*\|\s*(sh|bash)"), 3),
    ("eval-atob", re.compile(r"eval\s*\(\s*atob\s*\("), 3),
    ("reverse-shell-devtcp", re.compile(r"/dev/tcp/\d"), 3),
    ("reverse-shell-nc", re.compile(r"\bnc(at)?\s+-e\s"), 3),
    ("reverse-shell-bash-i", re.compile(r"bash\s+-i\s*>&\s*/dev/tcp"), 3),
    ("powershell-encoded", re.compile(r"powershell[^\n]*-(enc|EncodedCommand)\b", re.I), 3),
    ("onion-address", re.compile(r"\b[a-z2-7]{16,56}\.onion\b"), 2),
    ("crypto-miner", re.compile(r"stratum\+tcp://|xmrig|minergate", re.I), 3),
    ("exfiltration-language", re.compile(r"exfiltrat(e|ion)", re.I), 2),
    ("credential-to-remote", re.compile(r"(api[_ -]?key|secret|credential|\.env)\b[^\n]{0,60}(https?://)", re.I), 2),
    ("eval-generic", re.compile(r"\beval\s*\("), 1),
    ("exec-generic", re.compile(r"\bexec\s*\("), 1),
    ("os-system", re.compile(r"os\.system\s*\("), 1),
    ("shell-true", re.compile(r"shell\s*=\s*True"), 1),
    ("dynamic-function-eval", re.compile(r"new\s+Function\s*\(|Function\s*\(\s*atob"), 2),
    ("prompt-injection-ignore", re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.I), 3),
    ("prompt-injection-disregard", re.compile(r"disregard\s+(all\s+)?(previous|prior|your)\s+instructions", re.I), 3),
    ("prompt-injection-secret", re.compile(r"(secretly|without (telling|informing) the user|do not (tell|inform) the user)", re.I), 3),
]

RISK_SCAN_CONCURRENCY = asyncio.Semaphore(25)
GITHUB_OWNER_REPO_RE = re.compile(r"github\.com/([^/]+)/([^/#?]+)")


def heuristic_scan(text: str):
    if not text:
        return 0, []
    score = 0
    flags = []
    for label, pattern, weight in RISK_PATTERNS:
        if pattern.search(text):
            score += weight
            flags.append(label)
    return score, flags


GITHUB_TREE_URL_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.+)")


async def fetch_github_raw_content(client: httpx.AsyncClient, owner: str, repo: str, url: str = "") -> str:
    # If the URL points at a specific path (e.g. skillsmp's githubUrl =
    # .../tree/main/skills/foo), fetch SKILL.md from that exact directory
    # first — it's precise, whereas guessing top-level paths on a big repo
    # would usually miss a skill that lives in a subdirectory.
    tree_match = GITHUB_TREE_URL_RE.search(url) if url else None
    candidates = []
    if tree_match:
        t_owner, t_repo, branch, subpath = tree_match.groups()
        candidates.append((t_owner, t_repo, branch, f"{subpath.rstrip('/')}/SKILL.md"))
    candidates += [
        (owner, repo, "HEAD", "SKILL.md"),
        (owner, repo, "HEAD", ".claude/skills/SKILL.md"),
        (owner, repo, "HEAD", "README.md"),
        (owner, repo, "HEAD", "package.json"),
    ]
    for c_owner, c_repo, branch, path in candidates:
        try:
            r = await client.get(f"https://raw.githubusercontent.com/{c_owner}/{c_repo}/{branch}/{path}", timeout=6)
            if r.status_code == 200 and r.text:
                return r.text[:20000]
        except Exception:
            continue
    return ""


async def fetch_npm_readme(client: httpx.AsyncClient, package_name: str) -> str:
    try:
        r = await client.get(f"https://registry.npmjs.org/{package_name}", timeout=6)
        if r.status_code == 200:
            return (r.json().get("readme") or "")[:20000]
    except Exception:
        pass
    return ""


# --- Local skill library ------------------------------------------------
# Persists every scanned skill's actual markdown/readme content to disk, so
# it's browsable/greppable at all times without the scraper running or
# Supabase reachable — the DB only ever stores metadata + links, never the
# content itself, so this is the only durable local copy of the real files.
LIBRARY_DIR = Path(__file__).parent / "skills_library"
LIBRARY_FILES_DIR = LIBRARY_DIR / "files"
LIBRARY_INDEX_PATH = LIBRARY_DIR / "index.json"
library_lock = asyncio.Lock()

# The index used to be re-parsed and re-serialized from disk on EVERY save. With
# per-skill extraction now emitting tens of thousands of skills per run against a
# 20MB+ index, that was O(n^2) and dominated run time. Keep the index in memory,
# authoritative once loaded, and flush to disk on a debounce + a final flush.
_library_index: dict | None = None
_library_dirty = 0
_library_last_flush = 0.0
LIBRARY_FLUSH_INTERVAL = 5.0     # seconds
LIBRARY_FLUSH_EVERY = 2000       # saves


def _library_filename(skill: dict) -> str:
    name = re.sub(r"[^a-zA-Z0-9_-]+", "-", skill.get("name") or "skill").strip("-")[:80] or "skill"
    url_hash = hashlib.sha1((skill.get("url") or "").encode("utf-8")).hexdigest()[:10]
    return f"{skill.get('source', 'unknown')}__{name}__{url_hash}.md"


def _write_library_index_atomic():
    LIBRARY_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(LIBRARY_INDEX_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(_library_index, f, indent=2)
        os.replace(tmp, LIBRARY_INDEX_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


async def flush_library_index():
    """Force-write the in-memory index. Call at the end of a run so nothing is lost."""
    global _library_dirty, _library_last_flush
    async with library_lock:
        if _library_index is not None and _library_dirty:
            _write_library_index_atomic()
            _library_dirty = 0
            _library_last_flush = time.monotonic()


async def save_to_library(skill: dict, content: str):
    global _library_index, _library_dirty, _library_last_flush
    filename = _library_filename(skill)
    async with library_lock:
        LIBRARY_FILES_DIR.mkdir(parents=True, exist_ok=True)
        (LIBRARY_FILES_DIR / filename).write_text(content, encoding="utf-8")

        if _library_index is None:
            try:
                _library_index = json.loads(LIBRARY_INDEX_PATH.read_text(encoding="utf-8"))
            except Exception:
                _library_index = {}
        _library_index[skill.get("url", "")] = {
            "name": skill.get("name"),
            "source": skill.get("source"),
            "url": skill.get("url"),
            "description": skill.get("description"),
            "content_hash": skill.get("content_hash") or quality_content_hash(content),
            "file": filename,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        _library_dirty += 1
        now = time.monotonic()
        if _library_dirty >= LIBRARY_FLUSH_EVERY or (now - _library_last_flush) >= LIBRARY_FLUSH_INTERVAL:
            _write_library_index_atomic()
            _library_dirty = 0
            _library_last_flush = now


async def scan_skill(client: httpx.AsyncClient, skill: dict):
    async with RISK_SCAN_CONCURRENCY:
        # Tree-crawled skills arrive with their SKILL.md already fetched — scan
        # that exact content instead of re-guessing paths.
        content = skill.pop("_content", "")
        try:
            url = skill.get("url") or ""
            match = GITHUB_OWNER_REPO_RE.search(url)
            if content:
                pass
            elif match:
                content = await fetch_github_raw_content(client, match.group(1), match.group(2), url)
            elif skill.get("source") == "npm":
                content = await fetch_npm_readme(client, skill.get("name", ""))
        except Exception:
            content = ""

        blob = f"{skill.get('name', '')} {skill.get('description', '')} {content}"
        score, flags = heuristic_scan(blob)
        skill["risk_score"] = score
        skill["risk_flags"] = flags
        skill["scanned_at"] = datetime.now(timezone.utc).isoformat()
        skill.update(evaluate_quality(skill, content))

        if content:
            skill["_library_content"] = content


async def get_scanned_urls(client: httpx.AsyncClient) -> set:
    """URLs already scanned in a previous run — skip rescanning them on routine
    discovery passes; /rescan-all covers periodic full re-verification instead."""
    urls = set()
    offset = 0
    page_size = 1000
    while True:
        try:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/skills",
                params={"select": "url", "scanned_at": "not.is.null", "order": "id.asc"},
                headers={**HEADERS, "Range": f"{offset}-{offset + page_size - 1}"},
                timeout=15,
            )
            rows = r.json()
            if not isinstance(rows, list) or not rows:
                break
            urls.update(row["url"] for row in rows if row.get("url"))
            if len(rows) < page_size:
                break
            offset += page_size
        except Exception:
            break
    return urls


async def count_skills(client: httpx.AsyncClient) -> int:
    """Exact row count of the skills table. Comparing this before/after the upsert
    is the ground truth for how many genuinely new skills a run added — unlike
    diffing URL sets, it can't be silently skewed by a failed pagination request."""
    r = await client.get(
        f"{SUPABASE_URL}/rest/v1/skills",
        params={"select": "id"},
        headers={**HEADERS, "Prefer": "count=exact", "Range": "0-0"},
        timeout=15,
    )
    return int(r.headers.get("content-range", "0/0").split("/")[-1])


def dedup_skills(skills: list) -> list:
    """Dedup by normalized url — a single Postgres upsert statement errors if the
    same conflict key (url) appears twice in one batch, and un-normalized URLs
    (case, trailing /, .git) hide dupes. On collision prefer the richer
    github_skill_file row (frontmatter-parsed name/description) over a bare
    repo/search/directory pointer for the same URL."""
    deduped: dict = {}
    for s in skills:
        url = normalize_url(s.get("url") or "")
        if not url:
            continue
        s["url"] = url
        existing = deduped.get(url)
        if existing is None or (
            s.get("source") == "github_skill_file" and existing.get("source") != "github_skill_file"
        ):
            deduped[url] = s
    return list(deduped.values())


def mark_content_duplicates(skills: list) -> None:
    """Mark same-run duplicate content by normalized content hash."""
    canonical_by_hash: dict[str, str] = {}
    for skill in skills:
        chash = skill.get("content_hash")
        if not chash or skill.get("quality_status") != "active":
            continue
        canonical = canonical_by_hash.get(chash)
        if canonical:
            reasons = set(skill.get("quality_reasons") or [])
            reasons.add("duplicate-content")
            skill["quality_status"] = "duplicate"
            skill["quality_reasons"] = sorted(reasons)
            skill["canonical_id"] = canonical
            skill["embedding"] = None
        else:
            canonical_by_hash[chash] = skill.get("url") or skill.get("id") or chash


async def run_scrape(run_id: str) -> bool:
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            state = CrawlState.load()
            budget = RunBudget()
            skills = []
            await asyncio.gather(
                scrape_github(client, skills, state, budget),
                scrape_npm(client, skills),
                scrape_mcp_registry(client, skills),
                scrape_awesome_lists(client, skills, budget),
                scrape_web_search(client, skills),
                scrape_skillsmp(client, skills),
            )
            skills = dedup_skills(skills)

            # Walk skill-ish repos' git trees so every individual SKILL.md inside
            # them becomes its own row, then dedup again (tree-crawl rows can
            # collide with skillsmp/code-search rows for the same directory).
            await expand_repos_to_skills(client, skills, state, budget)
            skills = dedup_skills(skills)

            already_scanned = await get_scanned_urls(client)
            unscanned = [s for s in skills if s["url"] not in already_scanned or s.get("_content")]
            await asyncio.gather(*(scan_skill(client, s) for s in unscanned))
            mark_content_duplicates(skills)
            await asyncio.gather(*(
                save_to_library(s, s.pop("_library_content"))
                for s in skills
                if s.get("_library_content") and s.get("quality_status") not in {"rejected", "duplicate"}
            ))
            await flush_library_index()
            state.save()

            count_before = await count_skills(client)
            # PostgREST bulk upsert requires uniform keys per request; scanned rows
            # carry risk fields that unscanned ones lack, so group by key set.
            rows_by_shape: dict = {}
            for s in skills:
                row = skill_to_row(s)
                rows_by_shape.setdefault(frozenset(row.keys()), []).append(row)
            chunk_size = 50
            for rows in rows_by_shape.values():
                for i in range(0, len(rows), chunk_size):
                    await supabase_post(client, "skills", rows[i:i+chunk_size], on_conflict="url")
            count_after = await count_skills(client)

            await supabase_patch(client, "scrape_runs", {"id": run_id}, {
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "status": "done",
                "skills_found": len(skills),
                "new_skills_found": count_after - count_before,
            })
            return True
        except Exception as e:
            await supabase_patch(client, "scrape_runs", {"id": run_id}, {
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "status": "error",
                "skills_found": 0,
                "error": str(e)[:500],
            })
            return False


async def start_new_scrape_run() -> str:
    async with httpx.AsyncClient() as client:
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(seconds=STALE_SCRAPE_RUN_SECONDS)).isoformat()
        await client.patch(
            f"{SUPABASE_URL}/rest/v1/scrape_runs",
            params={"status": "eq.running", "started_at": f"lt.{cutoff}"},
            json={
                "finished_at": now.isoformat(),
                "status": "stale",
                "error": f"Marked stale before starting a new run after {STALE_SCRAPE_RUN_SECONDS}s.",
            },
            headers=HEADERS,
        )
        active = await supabase_get(
            client,
            "scrape_runs",
            f"status=eq.running&started_at=gt.{cutoff}&order=started_at.desc&limit=1",
        )
        if isinstance(active, list) and active:
            run = active[0]
            raise ScrapeAlreadyRunning(
                f"Scrape already running: {run.get('id')} started_at={run.get('started_at')}"
            )
        r = await supabase_post(client, "scrape_runs", {"status": "running"})
        if r.status_code == 409:
            raise ScrapeAlreadyRunning("Scrape already running: database lease is held")
        r.raise_for_status()
        run = r.json()
        return run[0]["id"] if isinstance(run, list) else run.get("id")


@app.post("/scrape")
async def start_scrape():
    global scrape_task
    if scrape_task and not scrape_task.done():
        return {"error": "Scrape already running"}

    try:
        run_id = await start_new_scrape_run()
    except ScrapeAlreadyRunning as exc:
        return {"error": str(exc), "status": "already_running"}
    scrape_task = asyncio.create_task(run_scrape(run_id))
    return {"run_id": run_id, "status": "started"}


async def continuous_scrape_loop():
    """Keeps the scraper running forever: one run, then wait, repeat. Never blocks
    the event loop for long and never dies from a single bad run."""
    global scrape_task
    while True:
        try:
            if not (scrape_task and not scrape_task.done()):
                run_id = await start_new_scrape_run()
                scrape_task = asyncio.create_task(run_scrape(run_id))
            await scrape_task
        except Exception as e:
            print(f"[continuous_scrape_loop] run failed: {e}")
        await asyncio.sleep(SCRAPE_INTERVAL_SECONDS)


@app.on_event("startup")
async def on_startup():
    if AUTO_START_SCRAPER:
        asyncio.create_task(continuous_scrape_loop())


rescan_task = None
rescan_progress = {"scanned": 0, "running": False}


async def run_rescan():
    """Backfills risk_score/risk_flags for every row already in the table — walks the
    whole skills table regardless of scanned_at, so it also re-verifies rows whose
    upstream content may have changed since the last scan."""
    global rescan_progress
    rescan_progress = {"scanned": 0, "running": True}
    async with httpx.AsyncClient(follow_redirects=True) as client:
        offset = 0
        page_size = 200
        while True:
            try:
                r = await client.get(
                    f"{SUPABASE_URL}/rest/v1/skills",
                    params={"select": "id,url,name,description,source"},
                    headers={**HEADERS, "Range": f"{offset}-{offset + page_size - 1}"},
                    timeout=15,
                )
                rows = r.json()
            except Exception:
                break
            if not isinstance(rows, list) or not rows:
                break
            await asyncio.gather(*(scan_skill(client, row) for row in rows))
            await asyncio.gather(*(
                supabase_patch(client, "skills", {"id": row["id"]}, {
                    "risk_score": row.get("risk_score", 0),
                    "risk_flags": row.get("risk_flags", []),
                    "scanned_at": row.get("scanned_at"),
                })
                for row in rows
            ))
            rescan_progress["scanned"] += len(rows)
            if len(rows) < page_size:
                break
            offset += page_size
    await flush_library_index()
    rescan_progress["running"] = False


normalize_task = None
normalize_progress = {"rows_seen": 0, "duplicates_deleted": 0, "urls_rewritten": 0, "running": False}


async def run_normalize_db():
    """One-time backfill: collapse rows whose URLs normalize to the same string
    (keep lowest id), rewrite survivors' URLs to normalized form, and rewrite
    skills_library/index.json keys to match."""
    global normalize_progress
    normalize_progress = {"rows_seen": 0, "duplicates_deleted": 0, "urls_rewritten": 0, "running": True}
    async with httpx.AsyncClient() as client:
        groups: dict = {}
        offset, page_size = 0, 1000
        while True:
            try:
                r = await client.get(
                    f"{SUPABASE_URL}/rest/v1/skills",
                    params={"select": "id,url", "order": "id.asc"},
                    headers={**HEADERS, "Range": f"{offset}-{offset + page_size - 1}"},
                    timeout=30,
                )
                rows = r.json()
            except Exception:
                break
            if not isinstance(rows, list) or not rows:
                break
            for row in rows:
                normalize_progress["rows_seen"] += 1
                norm = normalize_url(row.get("url") or "")
                if norm:
                    groups.setdefault(norm, []).append((row["id"], row["url"]))
            if len(rows) < page_size:
                break
            offset += page_size

        for norm, members in groups.items():
            members.sort()
            keep_id, keep_url = members[0]
            # delete dupes first so patching the survivor can't hit the unique constraint
            for dupe_id, _ in members[1:]:
                await supabase_delete(client, "skills", {"id": dupe_id})
                normalize_progress["duplicates_deleted"] += 1
            if keep_url != norm:
                await supabase_patch(client, "skills", {"id": keep_id}, {"url": norm})
                normalize_progress["urls_rewritten"] += 1

    # Rewrite local library index keys the same way (first writer wins per key).
    global _library_index
    async with library_lock:
        try:
            index = json.loads(LIBRARY_INDEX_PATH.read_text(encoding="utf-8"))
            new_index = {}
            for url, entry in index.items():
                norm = normalize_url(url) or url
                if norm not in new_index:
                    entry["url"] = norm
                    new_index[norm] = entry
            _library_index = new_index  # keep the in-memory cache consistent
            _write_library_index_atomic()
        except Exception:
            pass
    normalize_progress["running"] = False


@app.post("/seed-backlog")
async def seed_backlog():
    """One-time migration: queue every GitHub repo already known to the DB for a
    tree crawl, so the existing corpus gets per-skill extraction over the next
    couple of days of backlog draining."""
    state = CrawlState.load()
    known = set(state.backlog) | set(state.repos)
    added = 0
    async with httpx.AsyncClient() as client:
        offset, page_size = 0, 1000
        while True:
            try:
                r = await client.get(
                    f"{SUPABASE_URL}/rest/v1/skills",
                    params={"select": "url", "order": "id.asc"},
                    headers={**HEADERS, "Range": f"{offset}-{offset + page_size - 1}"},
                    timeout=30,
                )
                rows = r.json()
            except Exception:
                break
            if not isinstance(rows, list) or not rows:
                break
            for row in rows:
                parsed = _github_owner_repo(row.get("url") or "")
                if parsed:
                    owner_repo = "/".join(parsed)
                    if owner_repo not in known:
                        known.add(owner_repo)
                        state.backlog.append(owner_repo)
                        added += 1
            if len(rows) < page_size:
                break
            offset += page_size
    state.save()
    return {"queued": added, "backlog_size": len(state.backlog)}


@app.post("/normalize-db")
async def start_normalize_db():
    global normalize_task
    if normalize_task and not normalize_task.done():
        return {"error": "Normalize already running", "progress": normalize_progress}
    normalize_task = asyncio.create_task(run_normalize_db())
    return {"status": "started"}


@app.get("/normalize-db/progress")
async def get_normalize_progress():
    return normalize_progress


@app.post("/rescan")
async def start_rescan():
    global rescan_task
    if rescan_task and not rescan_task.done():
        return {"error": "Rescan already running", "progress": rescan_progress}
    rescan_task = asyncio.create_task(run_rescan())
    return {"status": "started"}


@app.get("/status")
async def status():
    async with httpx.AsyncClient() as client:
        runs = await supabase_get(client, "scrape_runs", "order=started_at.desc&limit=5")
        count_r = await client.get(f"{SUPABASE_URL}/rest/v1/skills?select=id", headers={**HEADERS, "Prefer": "count=exact", "Range": "0-0"})
        total = count_r.headers.get("content-range", "0/0").split("/")[-1]
        flagged_r = await client.get(
            f"{SUPABASE_URL}/rest/v1/skills?select=id&risk_score=gt.0",
            headers={**HEADERS, "Prefer": "count=exact", "Range": "0-0"},
        )
        flagged = flagged_r.headers.get("content-range", "0/0").split("/")[-1]
    running = scrape_task and not scrape_task.done()
    return {
        "running": running,
        "total_skills": total,
        "flagged_skills": flagged,
        "scraper": store.scrape_run_summary(STALE_SCRAPE_RUN_SECONDS),
        "recent_runs": runs,
        "continuous_mode": True,
        "interval_seconds": SCRAPE_INTERVAL_SECONDS,
        "rescan": rescan_progress,
    }


@app.get("/find")
async def find_skill(q: str, limit: int = 8):
    """Chat-style skill finder: full-text search ranked by relevance (then stars),
    backed by the search_skills Postgres function."""
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{SUPABASE_URL}/rest/v1/rpc/search_skills",
            json={"query": q, "max_results": limit},
            headers=HEADERS,
            timeout=15,
        )
        if r.status_code != 200:
            return {"error": r.text[:300], "results": []}
        return {"query": q, "results": r.json()}


@app.get("/chat")
async def chat_find_skill(q: str):
    """Conversational single-skill recommender. The whole conversation's user text
    is passed as one combined query. Returns exactly one of:
      - {"type": "recommend", "skill": {...}, "message": ...}  - one clear winner
      - {"type": "clarify", "message": ..., "options": [...]}  - ambiguous; the top
        candidates are offered so the user can pick one or add detail
      - {"type": "none", "message": ...}                        - nothing matched
    """
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{SUPABASE_URL}/rest/v1/rpc/search_skills",
            json={"query": q, "max_results": 10},
            headers=HEADERS,
            timeout=15,
        )
        if r.status_code != 200:
            return {"type": "none", "message": "Search failed - try again in a moment."}
        results = r.json()

    if not results:
        return {
            "type": "none",
            "message": "I couldn't find anything matching that. Try describing the task with different words - e.g. the tool, file type, or service involved.",
        }

    # Never recommend a skill the malware scan flagged as high risk; surface safer
    # alternatives instead. (Low scores stay eligible but the warning is shown.)
    safe = [s for s in results if (s.get("risk_score") or 0) < 3]
    if not safe:
        return {
            "type": "none",
            "message": "The only matches I found were flagged as potentially unsafe by the malware scan, so I won't recommend them. Try a different description.",
        }

    top = safe[0]
    runner_up = safe[1] if len(safe) > 1 else None

    # Clear winner: only one match, or top rank dominates the runner-up. Ranks come
    # back sorted desc, so a big relative gap means the extra words in the query
    # matched the top hit and not the rest.
    # 1.25x: near-ties in the OR-based ranking sit at ~1.0x, while a query whose
    # extra terms all matched one skill lands 1.3x+ over the runner-up.
    if runner_up is None or top["rank"] >= runner_up["rank"] * 1.25:
        return {"type": "recommend", "skill": top, "message": _recommend_blurb(top)}

    # Ambiguous: several close matches. Ask a clarifying question and offer the top
    # few as options so one more turn resolves it.
    options = safe[:3]
    return {
        "type": "clarify",
        "message": "A few skills fit that about equally well - which of these is closest to what you're doing? Pick one, or describe your task in a bit more detail.",
        "options": options,
    }


def _recommend_blurb(skill: dict) -> str:
    parts = [f"Best match: {skill['name']}."]
    if skill.get("description"):
        parts.append(skill["description"][:200])
    if skill.get("stars"):
        parts.append(f"({skill['stars']} GitHub stars)")
    if (skill.get("risk_score") or 0) > 0:
        parts.append(f"Note: the malware scan gave this a low-level risk score of {skill['risk_score']} - review it before installing.")
    return " ".join(parts)


@app.get("/library")
async def get_library():
    """Metadata for every locally-saved skill file (name/source/url/description
    plus which file under /library/files/ holds its content). The .md content
    itself lives on disk in skills_library/files and is fetchable directly at
    /library/files/<file>, or read straight off disk without the server running."""
    if not LIBRARY_INDEX_PATH.exists():
        return {"count": 0, "skills": []}
    try:
        index = json.loads(LIBRARY_INDEX_PATH.read_text(encoding="utf-8"))
    except Exception:
        index = {}
    entries = list(index.values())
    entries.sort(key=lambda e: e.get("saved_at", ""), reverse=True)
    return {"count": len(entries), "skills": entries}


@app.get("/skills")
async def get_skills(limit: int = 100, offset: int = 0, source: str = "", min_risk: int = 0):
    async with httpx.AsyncClient() as client:
        params = f"order=discovered_at.desc&limit={limit}&offset={offset}"
        if source:
            params += f"&source=eq.{source}"
        if min_risk > 0:
            params += f"&risk_score=gte.{min_risk}"
        return await supabase_get(client, "skills", params)


@app.get("/")
async def serve_index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
