"""MCP connector for the auto-skill recommender.

Exposes the Supabase-backed skill database (hybrid FTS+vector search over
~200k scraped Claude skills/MCP servers/plugins) as MCP tools so Claude
Code / Claude Desktop can look up and use a matching skill mid-conversation.

Query embedding happens server-side in a Supabase Edge Function (gte-small
via Supabase.ai.Session), so this connector only needs `mcp` + `httpx` --
no local model/runtime required, which keeps it light enough for anyone to
install with a single `uvx` command.

Tools:
  recommend_skill(task)      -> ranked candidates + the top match's full
                                 SKILL.md content (read it and follow it).
  install_skill(url, name?)  -> writes the skill's SKILL.md into
                                 ~/.claude/skills/<name>/SKILL.md so it
                                 becomes a real, permanently invocable
                                 Claude Code skill going forward.

Run directly for a stdio MCP server:
    python mcp_server.py
Register with Claude Code:
    claude mcp add auto-skill -- python "C:\\Users\\Neel\\Desktop\\auto-skill\\mcp_server.py"
"""
import json
import re
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

SUPABASE_URL = "https://kgkuoxdizynkcrbasamu.supabase.co"
# Read-only anon key -- safe to ship publicly. RLS on this project grants
# anon SELECT only; all writes require the service_role key, which never
# leaves the scraper's local machine.
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imtna3VveGRpenlua2NyYmFzYW11Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODI4NzE4NzUsImV4cCI6MjA5ODQ0Nzg3NX0.6rqfcqdVShb9fo3x5z9E6mf6f-0iUbJn9Q7hUFqZ-jw"
HEADERS = {
    "apikey": SUPABASE_ANON_KEY,
    "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
    "Content-Type": "application/json",
}

# New skills go into the local scraper's SQLite DB now (Supabase free-tier
# space ran out 2026-07-05); the ~200k already in Supabase are untouched and
# still worth searching, so recommend_skill queries both and merges results.
LOCAL_DB_URL = "http://127.0.0.1:8000"

SKILLS_HOME = Path.home() / ".claude" / "skills"
LIBRARY_DIR = Path(__file__).parent / "skills_library"
_BLOB_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.*)")
_TREE_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.*)")

mcp = FastMCP("auto-skill")


def _local_content(url: str) -> str:
    """Best-effort local cache lookup (only present on the scraper's own machine)."""
    index_path = LIBRARY_DIR / "index.json"
    if not index_path.exists():
        return ""
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        filename = (index.get(url) or {}).get("file", "")
        if not filename:
            return ""
        return (LIBRARY_DIR / "files" / filename).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _raw_candidates(url: str) -> list[str]:
    """Candidate raw.githubusercontent.com URLs for a github.com url. A
    "blob" url points at an exact file; a "tree" url points at a directory
    (the skill folder), so SKILL.md is assumed to live directly inside it."""
    m = _BLOB_RE.search(url)
    if m:
        owner, repo, ref, path = m.groups()
        return [f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"]
    m = _TREE_RE.search(url)
    if m:
        owner, repo, ref, path = m.groups()
        base = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}".rstrip("/")
        return [f"{base}/SKILL.md", f"{base}/skill.md"]
    return [url]


async def _fetch_content(client: httpx.AsyncClient, url: str) -> str:
    """Full SKILL.md content: local cache first, else fetch from GitHub raw."""
    local = _local_content(url)
    if local:
        return local
    for candidate in _raw_candidates(url):
        try:
            r = await client.get(candidate, timeout=10)
            if r.status_code == 200:
                return r.text
        except Exception:
            continue
    return ""


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "skill"


LOCAL_GAP = 1.6  # same recommend-vs-clarify gap heuristic as the Supabase side


async def _search_local(client: httpx.AsyncClient, task: str) -> list[dict]:
    """Freshly-scraped skills now live in the scraper's local SQLite DB
    instead of Supabase; only reachable if the scraper is running."""
    try:
        r = await client.post(
            f"{LOCAL_DB_URL}/rest/v1/rpc/search_skills",
            json={"query": task, "max_results": 8},
            timeout=5,
        )
        if r.status_code == 200:
            return [c for c in r.json() if (c.get("risk_score") or 0) < 3]
    except Exception:
        pass
    return []


async def _search(client: httpx.AsyncClient, task: str) -> dict:
    """Checks the local scraper DB first (fresh skills added since Supabase
    ran out of space); if it has a clear single winner, use it. Otherwise
    falls back to the Supabase-backed edge function (embeds + hybrid search +
    recommend/clarify/none decision over the ~200k historical skills), with
    a plain keyword RPC as a last resort if the edge function is down."""
    local = await _search_local(client, task)
    if local:
        top, runner_up = local[0], (local[1] if len(local) > 1 else None)
        if runner_up is None or top.get("rank", 0) >= runner_up.get("rank", 0) * LOCAL_GAP:
            return {"type": "recommend", "skill": top, "message": f"Best match: {top.get('name')}."}

    try:
        r = await client.post(
            f"{SUPABASE_URL}/functions/v1/recommend-skill",
            json={"messages": [{"role": "user", "content": task}]},
            headers=HEADERS,
            timeout=20,
        )
        if r.status_code == 200:
            result = r.json()
            if local and result.get("type") == "clarify":
                result["options"] = (result.get("options") or [])[:2] + local[:1]
            return result
    except Exception:
        pass

    r = await client.post(
        f"{SUPABASE_URL}/rest/v1/rpc/search_skills",
        json={"query": task, "max_results": 8},
        headers=HEADERS,
        timeout=15,
    )
    r.raise_for_status()
    candidates = [c for c in r.json() if (c.get("risk_score") or 0) < 3] + local
    if not candidates:
        return {"type": "none", "message": "No matching skill found in the database."}
    if len(candidates) == 1:
        return {"type": "recommend", "skill": candidates[0], "message": f"Best match: {candidates[0].get('name')}."}
    return {
        "type": "clarify",
        "message": "A few skills fit that about equally well — which is closest to what you're doing?",
        "options": candidates[:3],
    }


@mcp.tool()
async def recommend_skill(task: str) -> dict:
    """Search the auto-skill database (~200k scraped Claude skills, MCP
    servers, and plugins) for the one that best matches a task, and return
    its full SKILL.md content so it can be read and followed immediately.

    Call this whenever the user's request might already be covered by an
    existing packaged skill/MCP server, before building something from
    scratch. Pass a short, keyword-rich description of the task.
    """
    async with httpx.AsyncClient() as client:
        try:
            result = await _search(client, task)
        except Exception as e:
            return {"found": False, "message": f"Skill database is unavailable right now ({e}). Try again shortly."}

        if result.get("type") == "none":
            return {"found": False, "message": result.get("message", "No matching skill found.")}

        if result.get("type") == "clarify":
            options = result.get("options") or []
            return {
                "found": False,
                "message": result.get("message"),
                "candidates": [
                    {"name": o.get("name"), "description": (o.get("description") or "")[:150], "url": o.get("url")}
                    for o in options
                ],
                "instructions": "Ask the user to pick one of these, or call recommend_skill again with a more specific task.",
            }

        top = result.get("skill") or {}
        content = await _fetch_content(client, top.get("url", ""))
        return {
            "found": True,
            "best_match": {
                "name": top.get("name"),
                "description": top.get("description"),
                "url": top.get("url"),
                "source": top.get("source"),
                "stars": top.get("stars"),
                "risk_score": top.get("risk_score"),
            },
            "skill_content": content or "(content unavailable — fetch the url directly)",
            "instructions": (
                "Follow skill_content as if it were the active skill's instructions. "
                "If it genuinely fits, you can also call install_skill to save it permanently."
            ),
        }


@mcp.tool()
async def install_skill(url: str, name: str = "") -> str:
    """Download a skill's SKILL.md (by url, as returned from recommend_skill)
    and install it into ~/.claude/skills/<name>/SKILL.md so Claude Code can
    invoke it as a normal /skill from now on, in any project."""
    async with httpx.AsyncClient() as client:
        content = await _fetch_content(client, url)
    if not content:
        return f"Could not fetch content for {url}"

    m = re.search(r"^name:\s*(.+)$", content, re.MULTILINE)
    slug = _slugify(name or (m.group(1).strip() if m else url.rstrip("/").split("/")[-1]))

    dest_dir = SKILLS_HOME / slug
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_file = dest_dir / "SKILL.md"
    dest_file.write_text(content, encoding="utf-8")

    return f"Installed as '{slug}' at {dest_file}. Invoke it with the Skill tool (skill: \"{slug}\")."


if __name__ == "__main__":
    mcp.run()
