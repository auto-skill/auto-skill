"""UserPromptSubmit hook: surface a matching auto-skill for every chat message.

Reads the hook payload from stdin, asks the recommend-skill edge function
(hybrid FTS+vector search — handles natural-language prompts, unlike raw
FTS) for a match, and prints a one-line suggestion that Claude Code injects
into context. Claude then calls the auto-skill MCP tool recommend_skill for
the full SKILL.md and applies it.

Fail-open by design: any error, timeout, or non-match prints nothing and
exits 0, so a DB blip or edge cold start can never block the chat — it just
costs that one message its suggestion.
"""
import json
import sys
import urllib.request

SUPABASE_URL = "https://kgkuoxdizynkcrbasamu.supabase.co"
ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imtna3VveGRpenlua2NyYmFzYW11Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODI4NzE4NzUsImV4cCI6MjA5ODQ0Nzg3NX0."
    "6rqfcqdVShb9fo3x5z9E6mf6f-0iUbJn9Q7hUFqZ-jw"
)
LOCAL_URL = "http://localhost:8000"
TIMEOUT_SECONDS = 3.0


def _compact(skill: dict) -> str:
    name = skill.get("name") or "?"
    desc = (skill.get("description") or "").replace("\n", " ")[:120]
    return f"\"{name}\" — {desc} ({skill.get('url') or ''})"


def _local_matches(prompt: str) -> list | None:
    """Hybrid search via the local scraper (model already resident, no cold
    start). Returns None only when the scraper isn't running, so the caller
    knows to try the edge; a timeout returns [] (edge would be slower still)."""
    from urllib.parse import quote

    try:
        with urllib.request.urlopen(
            f"{LOCAL_URL}/find-semantic?q={quote(prompt[:500])}&limit=3",
            timeout=TIMEOUT_SECONDS,
        ) as r:
            return (json.load(r).get("results") or [])[:2]
    except (ConnectionError, OSError) as e:
        if isinstance(e, TimeoutError) or "timed out" in str(e).lower():
            return []
        return None
    except Exception:
        return []


def _edge_matches(prompt: str) -> list:
    req = urllib.request.Request(
        f"{SUPABASE_URL}/functions/v1/recommend-skill",
        data=json.dumps({"messages": [{"role": "user", "content": prompt[:500]}]}).encode(),
        headers={
            "apikey": ANON_KEY,
            "Authorization": f"Bearer {ANON_KEY}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as r:
        result = json.load(r)
    kind = result.get("type")
    if kind == "recommend" and result.get("skill"):
        return [result["skill"]]
    if kind == "clarify" and result.get("options"):
        return result["options"][:3]
    return []


def main() -> None:
    payload = json.load(sys.stdin)
    prompt = (payload.get("prompt") or "").strip()
    # Slash commands, tiny follow-ups ("yes", "ok do that"), and pasted
    # walls of text are not skill-shaped requests.
    if not prompt or prompt.startswith(("/", "!")) or len(prompt) < 12 or len(prompt) > 2000:
        return

    matches = _local_matches(prompt)
    if matches is None:  # scraper not running locally -> edge fallback
        matches = _edge_matches(prompt)
    matches = [m for m in matches if (m.get("risk_score") or 0) < 3]
    if not matches:
        return

    lines = "; ".join(_compact(m) for m in matches)
    print(
        f"[auto-skill] Existing skill(s) may cover this request: {lines}. "
        f"If the user's message is a task one of these could handle, call the auto-skill "
        f"MCP tool recommend_skill with a short task description, follow the returned "
        f"skill_content, and offer install_skill if it's worth keeping. If none fit, "
        f"ignore this note silently."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # fail open: never block the prompt on errors
