"""Semantic skill recommender: hybrid pgvector+FTS retrieval with an optional
local Ollama LLM for conversational quality.

Self-contained APIRouter so scraper.py only needs:
    from recommender import router as recommender_router
    app.include_router(recommender_router)

Endpoints:
  POST /chat  {"messages":[{"role","content"}...], "prev_options":[urls]}
              -> {"type":"recommend","skill":{...},"message":...}
               | {"type":"clarify","message":...,"options":[...]}
               | {"type":"none","message":...}
  GET  /find-semantic?q=...  raw hybrid-ranked list (debugging/eval)

Also runs a background loop that embeds any skills rows missing embeddings,
so freshly scraped skills become semantically searchable within minutes.
"""
import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from math import ceil

import httpx
from fastapi import APIRouter, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from embeddings import LibraryContent, build_embed_text, embed_text_hash, embed_texts
import local_store as store
from quality import CONFIG_VERSION, content_hash, rerank_candidates, tier_for_prompt

# Storage moved local 2026-07-05 -- recommender.py always runs embedded inside
# scraper.py's process (same app/port), which now serves local_api.py's
# Supabase-shaped REST+RPC surface backed by local_skills.db.
SUPABASE_URL = os.getenv("LOCAL_DB_URL", f"http://127.0.0.1:{os.getenv('LOCAL_DB_PORT', '8000')}").rstrip("/")
HEADERS = {
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates",
}

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
ENABLE_OLLAMA_CHAT = os.getenv("ENABLE_OLLAMA_CHAT", "").lower() in {"1", "true", "yes"}
AUTO_START_EMBEDDER = os.getenv("AUTO_START_EMBEDDER", "1").lower() not in {"0", "false", "no"}

# RRF scores cluster near 1/(rrf_k + ix), so near-ties sit ~1.0x apart; a top hit
# that both retrievers agree on lands well above 1.6x the runner-up.
RECOMMEND_GAP = 1.6
ROUTE_TTL_SECONDS = int(os.getenv("ROUTE_TTL_SECONDS", "300"))
MAX_INLINE_CONTENT_CHARS = int(os.getenv("MAX_INLINE_CONTENT_CHARS", "12000"))
ROUTE_LATENCY_WARN_MS = int(os.getenv("ROUTE_LATENCY_WARN_MS", "1500"))
ROUTE_SKILL_FIND_WARN_MS = int(os.getenv("ROUTE_SKILL_FIND_WARN_MS", "1200"))
ROUTE_INJECTED_TOKEN_WARN = int(os.getenv("ROUTE_INJECTED_TOKEN_WARN", "3000"))
ROUTE_RESPONSE_TOKEN_WARN = int(os.getenv("ROUTE_RESPONSE_TOKEN_WARN", "3500"))
CONTENT_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
EMBED_INTERVAL_SECONDS = int(os.getenv("EMBED_INTERVAL_SECONDS", "300"))
EMBED_PAGE_SIZE = 500
EMBED_BATCH = 128
# Each upserted row triggers an HNSW index update, so keep statements small
# enough to stay well under any statement_timeout.
EMBED_UPSERT_CHUNK = 50

router = APIRouter()


# --- Embedding backlog drain ---------------------------------------------

async def embed_missing_skills(client: httpx.AsyncClient) -> int:
    """Embed every skills row with no embedding yet. The is-null filter is the
    checkpoint, so this is safe to interrupt and re-run."""
    library = LibraryContent()  # fresh each drain to pick up newly saved .md files
    total = 0
    while True:
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/skills",
            params={
                "select": "id,url,name,source,description,tags",
                "embedding": "is.null",
                "url": "not.is.null",
                "quality_status": "eq.active",
                "order": "id.asc",
                "limit": str(EMBED_PAGE_SIZE),
            },
            headers=HEADERS,
            timeout=30,
        )
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        texts = [build_embed_text(row, library.get(row.get("url") or "")) for row in rows]
        vectors = await asyncio.to_thread(embed_texts, texts, EMBED_BATCH)
        now = datetime.now(timezone.utc).isoformat()
        payload = [
            {
                "url": row["url"],
                "name": row.get("name") or "",
                "source": row.get("source") or "",
                "embedding": vec,
                "embedding_text_hash": embed_text_hash(text),
                "embedded_at": now,
            }
            for row, text, vec in zip(rows, texts, vectors)
        ]
        for i in range(0, len(payload), EMBED_UPSERT_CHUNK):
            await _upsert_chunk(client, payload[i:i + EMBED_UPSERT_CHUNK])
        total += len(rows)
    return total


async def _upsert_chunk(client: httpx.AsyncClient, chunk: list) -> None:
    """Retry transient failures (statement timeouts, blips) before giving up on
    the whole drain; the is-null checkpoint makes re-runs safe either way."""
    last = ""
    for attempt in range(3):
        if attempt:
            await asyncio.sleep(2 ** attempt)
        pr = await client.post(
            f"{SUPABASE_URL}/rest/v1/skills?on_conflict=url",
            json=chunk,
            headers=HEADERS,
            timeout=60,
        )
        if pr.status_code in (200, 201, 204):
            return
        last = f"{pr.status_code}: {pr.text[:300]}"
    raise RuntimeError(f"embedding upsert failed after retries ({last})")


async def _embed_backlog_loop():
    delay = EMBED_INTERVAL_SECONDS
    while True:
        try:
            async with httpx.AsyncClient() as client:
                n = await embed_missing_skills(client)
                if n:
                    print(f"[recommender] embedded {n} new skills")
            delay = EMBED_INTERVAL_SECONDS
        except Exception as e:
            print(f"[recommender] embed loop error (retrying in {delay}s): {e}")
            delay = min(delay * 2, 3600)  # back off instead of spamming a down DB
        await asyncio.sleep(delay)


@router.on_event("startup")
async def _start_embed_loop():
    if AUTO_START_EMBEDDER:
        asyncio.create_task(_embed_backlog_loop())
    asyncio.create_task(_warm_embedding_model())


async def _warm_embedding_model() -> None:
    """Load the ONNX model + tokenizer and run one throwaway embed at
    startup instead of on the first real request. embeddings._load() is
    lazy, so without this the first /find-semantic call after any restart
    pays the full model-load cost inline -- the hook's UserPromptSubmit
    timeout is 3s, well under what a cold load takes, so that first prompt
    would silently get no routing at all."""
    start = time.monotonic()
    try:
        await asyncio.to_thread(embed_texts, ["warm-up query for model load"])
        print(f"[recommender] embedding model warmed in {time.monotonic() - start:.1f}s")
    except Exception as e:
        print(f"[recommender] embedding model warm-up failed (will load lazily on first request): {e}")


# --- Retrieval ------------------------------------------------------------

def _stars(row: dict) -> int:
    if row.get("stars") is not None:
        return row["stars"] or 0
    raw = row.get("raw") or {}
    try:
        return int(raw.get("stars") or 0)
    except (TypeError, ValueError):
        return 0


async def embed_query(text: str) -> list[float]:
    return (await asyncio.to_thread(embed_texts, [text]))[0]


# Minimum top-hit cosine similarity for a query to count as having a real
# match. Calibrated 2026-07-06 against gte-small on this corpus: genuine task
# queries score >= 0.885 at top-1; conversational/meta prompts ("thanks",
# "remember this is a product", "ok sounds good") land 0.82-0.88. Queries whose
# best vector hit falls below the floor return no results instead of noise.
MIN_SIMILARITY = float(os.getenv("MIN_SIMILARITY", "0.87"))


def _passes_similarity_floor(results: list[dict]) -> bool:
    """True when the results contain at least one confident vector hit. Fails
    open when no result carries a similarity (pure-FTS fallback path)."""
    sims = [r["similarity"] for r in results if r.get("similarity") is not None]
    if not sims:
        return True
    return max(sims) >= MIN_SIMILARITY


def injection_tier(query_text: str, results: list[dict]) -> str:
    """Decide how much of the top result to hand to a caller.

    The similarity floor rejects junk/meta prompts. Full vs. hint is then a
    deterministic quality/platform decision, not an RRF-gap heuristic: eval
    evidence showed the RRF gap is a smooth continuum and wrongly downgraded
    many real tasks, while platform traps need a hard cap.
    """
    if not results or not _passes_similarity_floor(results):
        return "none"
    return tier_for_prompt(query_text, results, RECOMMEND_GAP)


async def retrieve_skills(client: httpx.AsyncClient, query_text: str, limit: int = 10) -> list[dict]:
    """Hybrid FTS+vector retrieval against the local DB. The frozen Supabase
    corpus was fully migrated into local_skills.db (migrate_state.json:
    202,367 rows on 2026-07-05), so local is the single source of truth.
    Falls back to pure FTS if embedding fails."""
    fetch_limit = max(limit, 20)
    body = {"query_text": query_text, "match_count": fetch_limit}
    try:
        body["query_embedding"] = await embed_query(query_text)
        rpc = "hybrid_search_skills"
    except Exception:
        rpc = "search_skills"
        body = {"query": query_text, "max_results": fetch_limit}

    r = await client.post(f"{SUPABASE_URL}/rest/v1/rpc/{rpc}", json=body, headers=HEADERS, timeout=20)
    if r.status_code != 200:
        return []
    results = list(r.json())
    results = rerank_candidates(query_text, results)
    return results[:limit]


async def fetch_skills_by_urls(client: httpx.AsyncClient, urls: list[str]) -> list[dict]:
    if not urls:
        return []
    quoted = ",".join('"' + u.replace('"', "") + '"' for u in urls[:10])
    r = await client.get(
        f"{SUPABASE_URL}/rest/v1/skills",
        params={
            "select": (
                "id,name,description,source,url,tags,risk_score,risk_flags,raw,"
                "content_hash,quality_status,quality_score,platforms,category"
            ),
            "url": f"in.({quoted})",
        },
        headers=HEADERS,
        timeout=15,
    )
    if r.status_code != 200:
        return []
    rows = r.json()
    for row in rows:
        row["stars"] = _stars(row)
        row.pop("raw", None)
        row.setdefault("rank", 0.0)
    return rows


# --- Ollama (optional local LLM) -------------------------------------------

_ollama_probe = {"at": 0.0, "up": False}


async def ollama_available(client: httpx.AsyncClient) -> bool:
    if not ENABLE_OLLAMA_CHAT:
        return False
    now = time.monotonic()
    if now - _ollama_probe["at"] < 60:
        return _ollama_probe["up"]
    up = False
    try:
        r = await client.get(f"{OLLAMA_URL}/api/tags", timeout=1.5)
        up = r.status_code == 200
    except Exception:
        up = False
    _ollama_probe.update(at=now, up=up)
    return up


async def ollama_json(client: httpx.AsyncClient, messages: list[dict], schema: dict, timeout: float = 45) -> dict | None:
    """One structured-output chat call; one retry on malformed JSON; None on failure."""
    for _ in range(2):
        try:
            r = await client.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": OLLAMA_MODEL,
                    "messages": messages,
                    "stream": False,
                    "format": schema,
                    "options": {"temperature": 0},
                },
                timeout=timeout,
            )
            if r.status_code != 200:
                return None
            content = r.json().get("message", {}).get("content", "")
            return json.loads(content)
        except json.JSONDecodeError:
            continue
        except Exception:
            return None
    return None


QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "search_query": {"type": "string"},
        "intent": {"type": "string", "enum": ["new_search", "refine", "correction", "pick_option"]},
        "excluded": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["search_query", "intent"],
}

QUERY_SYSTEM = """You turn a conversation into a search query for a directory of Claude Code skills, MCP servers, and plugins.
Output JSON:
- search_query: a short, keyword-rich description of the task the user wants a skill for (their latest need, incorporating earlier context). No filler words.
- intent: "new_search" for a fresh request; "refine" if the latest message adds constraints to the same request; "correction" if the user rejected what was suggested; "pick_option" if the user is choosing one of the options they were offered.
- excluded: names or URLs of skills the user rejected, if any."""

CHOOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["recommend", "clarify", "none"]},
        "chosen_url": {"type": "string"},
        "reply": {"type": "string"},
        "clarify_question": {"type": "string"},
        "option_urls": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["action", "reply"],
}

CHOOSE_SYSTEM = """You are a recommender for Claude Code skills / MCP servers. Given the user's task and a JSON list of candidate skills, pick a usable fit only when one is clearly relevant.
Output JSON:
- action: "recommend" when one candidate clearly fits the task; "clarify" when several fit about equally; "none" when nothing genuinely fits.
- chosen_url: the url of the recommended candidate (required for recommend).
- reply: 1-3 friendly sentences. For recommend: say why this skill fits their task. For clarify: ask ONE short question that would disambiguate. For none: suggest how to rephrase.
- clarify_question / option_urls: for clarify, the question plus 2-3 candidate urls to offer.
Rules: only ever use urls that appear in the candidate list. Prefer well-described, higher-star candidates when quality seems equal. Never pick a candidate whose risk_score is 3 or more."""


# --- Chat orchestration -----------------------------------------------------

class ChatRequest(BaseModel):
    messages: list[dict]
    prev_options: list[str] = []


class RouteRequest(BaseModel):
    prompt: str | None = None
    task: str | None = None
    client: str = ""
    client_version: str = ""
    limit: int = 8


class RouteFeedbackRequest(BaseModel):
    route_id: str
    outcome: str
    source: str = ""
    note: str = ""


NONE_MESSAGE = ("I couldn't find anything matching that. Try describing the task with "
                "different words - e.g. the tool, file type, or service involved.")


def _blurb(skill: dict) -> str:
    parts = [f"Best match: {skill['name']}."]
    if skill.get("description"):
        parts.append(skill["description"][:200])
    if skill.get("stars"):
        parts.append(f"({skill['stars']} GitHub stars)")
    if (skill.get("risk_score") or 0) > 0:
        parts.append(f"Note: the malware scan gave this a low-level risk score of {skill['risk_score']} - review it before installing.")
    return " ".join(parts)


def _heuristic_response(candidates: list[dict]) -> dict:
    top = candidates[0]
    runner_up = candidates[1] if len(candidates) > 1 else None
    top_score = top.get("route_score", top.get("rank", 0))
    runner_score = runner_up.get("route_score", runner_up.get("rank", 0)) if runner_up else 0
    if runner_up is None or top_score >= runner_score * RECOMMEND_GAP:
        return {"type": "recommend", "skill": top, "message": _blurb(top)}
    return {
        "type": "clarify",
        "message": "A few skills fit that about equally well - which of these is closest to what you're doing? Pick one, or describe your task in a bit more detail.",
        "options": candidates[:3],
    }


def _compact(c: dict) -> dict:
    return {
        "name": c.get("name"),
        "description": (c.get("description") or "")[:200],
        "source": c.get("source"),
        "url": c.get("url"),
        "tags": (c.get("tags") or [])[:6],
        "stars": c.get("stars") or 0,
        "risk_score": c.get("risk_score") or 0,
    }


@router.post("/chat")
async def chat_recommend(body: ChatRequest):
    messages = [m for m in body.messages if m.get("role") in ("user", "assistant") and m.get("content")]
    user_texts = [m["content"] for m in messages if m["role"] == "user"]
    if not user_texts:
        return {"type": "none", "message": "Tell me what you're trying to do and I'll find a skill for it."}

    query = " ".join(user_texts)[-500:]
    intent, excluded = "new_search", []

    async with httpx.AsyncClient() as client:
        use_llm = await ollama_available(client)

        if use_llm:
            convo = json.dumps(messages[-8:], ensure_ascii=False)
            parsed = await ollama_json(
                client,
                [{"role": "system", "content": QUERY_SYSTEM},
                 {"role": "user", "content": f"Conversation:\n{convo}"}],
                QUERY_SCHEMA,
            )
            if parsed and (parsed.get("search_query") or "").strip():
                query = parsed["search_query"].strip()
                intent = parsed.get("intent", "new_search")
                excluded = [str(x).lower() for x in (parsed.get("excluded") or [])]

        candidates = await retrieve_skills(client, query, 10)

        # Follow-ups keep the previously offered options in play so the LLM can
        # rerank the union rather than starting from scratch.
        if body.prev_options and intent in ("refine", "correction", "pick_option"):
            prev = await fetch_skills_by_urls(client, body.prev_options)
            seen = {c.get("url") for c in candidates}
            candidates.extend(p for p in prev if p.get("url") not in seen)

        def _excluded(c: dict) -> bool:
            name = (c.get("name") or "").lower()
            url = (c.get("url") or "").lower()
            return any(x == name or (x and x in url) for x in excluded)

        candidates = [c for c in candidates if (c.get("risk_score") or 0) < 3 and not _excluded(c)]
        if not candidates or not _passes_similarity_floor(candidates):
            return {"type": "none", "message": NONE_MESSAGE}

        if use_llm:
            by_url = {c["url"]: c for c in candidates if c.get("url")}
            choice = await ollama_json(
                client,
                [{"role": "system", "content": CHOOSE_SYSTEM},
                 {"role": "user", "content": json.dumps({
                     "task": query,
                     "latest_user_message": user_texts[-1],
                     "candidates": [_compact(c) for c in candidates[:8]],
                 }, ensure_ascii=False)}],
                CHOOSE_SCHEMA,
            )
            if choice:
                action = choice.get("action")
                reply = (choice.get("reply") or "").strip()
                if action == "recommend" and choice.get("chosen_url") in by_url:
                    skill = by_url[choice["chosen_url"]]
                    return {"type": "recommend", "skill": skill, "message": reply or _blurb(skill)}
                if action == "clarify":
                    options = [by_url[u] for u in (choice.get("option_urls") or []) if u in by_url][:3]
                    if not options:
                        options = candidates[:3]
                    return {"type": "clarify",
                            "message": reply or choice.get("clarify_question") or "Which of these is closest?",
                            "options": options}
                if action == "none":
                    return {"type": "none", "message": reply or NONE_MESSAGE}
            # malformed/hallucinated LLM output -> deterministic path

        return _heuristic_response(candidates)


def _public_skill(row: dict | None) -> dict | None:
    if not row:
        return None
    return {
        "id": row.get("id"),
        "slug": row.get("name"),
        "name": row.get("name"),
        "summary": row.get("description"),
        "description": row.get("description"),
        "source": row.get("source"),
        "source_url": row.get("url"),
        "url": row.get("url"),
        "content_hash": row.get("content_hash"),
        "quality_status": row.get("quality_status"),
        "quality_score": row.get("quality_score"),
        "platforms": row.get("platforms") or [],
        "category": row.get("category"),
        "risk_score": row.get("risk_score"),
        "rank": row.get("rank"),
        "route_score": row.get("route_score"),
        "similarity": row.get("similarity"),
    }


def _hint_candidates(results: list[dict], limit: int = 3) -> list[dict]:
    """Return a small content-free option set for medium-confidence routes."""
    candidates: list[dict] = []
    seen: set[str] = set()
    for row in results:
        public = _public_skill(row)
        if not public:
            continue
        key = (public.get("source_url") or public.get("url") or public.get("name") or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        candidates.append(public)
        if len(candidates) >= limit:
            break
    return candidates


def _score_debug(results: list[dict], tier: str) -> dict:
    if not results:
        return {"tier": tier, "reason": "no-results"}
    top = results[0]
    runner = results[1] if len(results) > 1 else None
    top_score = float(top.get("route_score") or top.get("rank") or 0.0)
    runner_score = float(runner.get("route_score") or runner.get("rank") or 0.0) if runner else 0.0
    return {
        "tier": tier,
        "top_route_score": top_score,
        "runner_route_score": runner_score,
        "margin": round(top_score - runner_score, 6),
        "lexical_overlap": top.get("lexical_overlap"),
        "platform_mismatch": bool(top.get("platform_mismatch")),
        "similarity": top.get("similarity"),
        "quality_status": top.get("quality_status"),
        "quality_score": top.get("quality_score"),
        "recommend_gap": RECOMMEND_GAP,
        "min_similarity": MIN_SIMILARITY,
    }


def _estimate_tokens(value) -> int:
    """Cheap, deterministic token estimate for budgets and trend tracking."""
    if value is None:
        return 0
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if not value:
        return 0
    return max(1, ceil(len(value) / 4))


def _query_hash(query: str) -> str:
    return hashlib.sha256(query.strip().lower().encode("utf-8")).hexdigest()


async def _record_route_event(event: dict) -> None:
    try:
        await asyncio.to_thread(store.insert_route_event, event)
    except Exception as exc:
        print(f"[recommender] route event logging failed: {exc}")


def _library_content_by_hash(target_hash: str) -> str:
    if not target_hash or not CONTENT_HASH_RE.match(target_hash):
        return ""
    return LibraryContent().get_by_hash(target_hash)


@router.get("/content/{hash_value}")
async def get_content(hash_value: str):
    text = await asyncio.to_thread(_library_content_by_hash, hash_value)
    if not text:
        return Response(status_code=404)
    return PlainTextResponse(
        text,
        media_type="text/markdown; charset=utf-8",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.post("/route")
async def route(body: RouteRequest):
    """Deterministic backend-owned route contract for connectors."""
    start = time.monotonic()
    query = (body.task or body.prompt or "").strip()
    if not query:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        metrics = {
            "latency_ms": elapsed_ms,
            "skill_find_ms": 0,
            "retrieval_ms": 0,
            "rerank_ms": 0,
            "content_ms": 0,
            "result_count": 0,
            "input_tokens": 0,
            "hint_tokens": 0,
            "candidate_tokens": 0,
            "content_tokens": 0,
            "injected_tokens": 0,
            "response_tokens": 0,
            "latency_warn_ms": ROUTE_LATENCY_WARN_MS,
            "response_token_warn": ROUTE_RESPONSE_TOKEN_WARN,
        }
        return {
            "tier": "none",
            "skill": None,
            "content": None,
            "content_url": None,
            "score_debug": {"tier": "none", "reason": "empty-query", "metrics": metrics},
            "config_version": CONFIG_VERSION,
            "ttl": ROUTE_TTL_SECONDS,
        }

    limit = max(2, min(int(body.limit or 8), 20))
    retrieval_start = time.monotonic()
    async with httpx.AsyncClient() as client:
        results = await retrieve_skills(client, query, limit)
    retrieval_ms = int((time.monotonic() - retrieval_start) * 1000)
    rerank_start = time.monotonic()
    results = rerank_candidates(query, results)
    tier = injection_tier(query, results)
    rerank_ms = int((time.monotonic() - rerank_start) * 1000)
    skill_find_ms = int((time.monotonic() - retrieval_start) * 1000)
    warnings: list[str] = []
    content = None
    content_url = None
    skill = _public_skill(results[0]) if results else None
    content_ms = 0
    route_id = str(uuid.uuid4())

    if tier == "full" and skill:
        content_start = time.monotonic()
        library = LibraryContent()
        text = library.get(skill.get("url") or "")
        content_ms = int((time.monotonic() - content_start) * 1000)
        if not text:
            tier = "hint"
            warnings.append("Matched skill has no locally stored SKILL.md content; downgraded to hint.")
        elif len(text) > MAX_INLINE_CONTENT_CHARS:
            tier = "hint"
            chash = skill.get("content_hash") or content_hash(text)
            content_url = f"/content/{chash}" if chash else None
            warnings.append("Matched skill content exceeds inline size cap; downgraded to hint.")
        else:
            content = text
            chash = skill.get("content_hash") or content_hash(text)
            if chash:
                skill["content_hash"] = chash
                content_url = f"/content/{chash}"

    debug = _score_debug(results, tier)
    candidates = _hint_candidates(results) if tier == "hint" else []
    input_tokens = _estimate_tokens(query)
    hint_tokens = _estimate_tokens(skill)
    candidate_tokens = _estimate_tokens(candidates)
    content_tokens = _estimate_tokens(content)
    injected_tokens = 0
    if tier == "full":
        injected_tokens = hint_tokens + content_tokens
    elif tier == "hint":
        injected_tokens = candidate_tokens
    response_preview = {
        "tier": tier,
        "skill": skill,
        "candidates": candidates,
        "content": content,
        "content_url": content_url,
        "config_version": CONFIG_VERSION,
    }
    metrics = {
        "latency_ms": int((time.monotonic() - start) * 1000),
        "skill_find_ms": skill_find_ms,
        "retrieval_ms": retrieval_ms,
        "rerank_ms": rerank_ms,
        "content_ms": content_ms,
        "result_count": len(results),
        "input_tokens": input_tokens,
        "hint_tokens": hint_tokens,
        "candidate_tokens": candidate_tokens,
        "content_tokens": content_tokens,
        "injected_tokens": injected_tokens,
        "response_tokens": _estimate_tokens(response_preview),
        "latency_warn_ms": ROUTE_LATENCY_WARN_MS,
        "response_token_warn": ROUTE_RESPONSE_TOKEN_WARN,
    }
    if metrics["latency_ms"] > ROUTE_LATENCY_WARN_MS:
        warnings.append(f"Route latency exceeded {ROUTE_LATENCY_WARN_MS}ms budget.")
    if metrics["skill_find_ms"] > ROUTE_SKILL_FIND_WARN_MS:
        warnings.append(f"Skill find exceeded {ROUTE_SKILL_FIND_WARN_MS}ms budget.")
    if metrics["injected_tokens"] > ROUTE_INJECTED_TOKEN_WARN:
        warnings.append(f"Injected content exceeded {ROUTE_INJECTED_TOKEN_WARN} token budget.")
    if metrics["response_tokens"] > ROUTE_RESPONSE_TOKEN_WARN:
        warnings.append(f"Route response exceeded {ROUTE_RESPONSE_TOKEN_WARN} token budget.")
    debug["metrics"] = metrics
    if warnings:
        debug["warnings"] = warnings
    await _record_route_event(
        {
            "client": body.client[:80],
            "client_version": body.client_version[:80],
            "id": route_id,
            "query_hash": _query_hash(query),
            "query_chars": len(query),
            "tier": tier,
            "skill_id": skill.get("id") if skill else None,
            "skill_name": skill.get("name") if skill else None,
            "skill_url": skill.get("source_url") if skill else None,
            "latency_ms": metrics["latency_ms"],
            "skill_find_ms": skill_find_ms,
            "retrieval_ms": retrieval_ms,
            "rerank_ms": rerank_ms,
            "content_ms": content_ms,
            "result_count": len(results),
            "input_tokens": metrics["input_tokens"],
            "hint_tokens": metrics["hint_tokens"],
            "candidate_tokens": metrics["candidate_tokens"],
            "content_tokens": metrics["content_tokens"],
            "injected_tokens": metrics["injected_tokens"],
            "response_tokens": metrics["response_tokens"],
            "config_version": CONFIG_VERSION,
            "warnings": warnings,
        }
    )
    return {
        "tier": tier,
        "skill": skill,
        "candidates": candidates,
        "content": content,
        "content_url": content_url,
        "route_id": route_id,
        "score_debug": debug,
        "config_version": CONFIG_VERSION,
        "ttl": ROUTE_TTL_SECONDS,
    }


@router.get("/find-semantic")
async def find_semantic(q: str, limit: int = 8, gate: bool = True):
    """Hybrid-ranked results, plus a `tier` a caller can act on directly:
      "full" -> inject the top result's whole skill content
      "hint" -> surface just its name/url, several candidates are plausible
      "none" -> nothing cleared the bar; do not inject anything
    With gate=true (default), a "none" tier also empties `results` - pass
    gate=false for debugging/eval of raw rankings regardless of tier."""
    async with httpx.AsyncClient() as client:
        results = await retrieve_skills(client, q, limit)
    tier = injection_tier(q, results)
    if gate and tier == "none":
        return {"query": q, "results": [], "tier": tier, "gated": True,
                "message": f"No result cleared the similarity floor ({MIN_SIMILARITY}).",
                "score_debug": _score_debug(results, tier),
            "config_version": CONFIG_VERSION}
    return {"query": q, "results": results, "tier": tier,
            "score_debug": _score_debug(results, tier),
            "config_version": CONFIG_VERSION}


@router.get("/route-metrics")
async def route_metrics(hours: int = 24):
    hours = max(1, min(int(hours or 24), 24 * 30))
    summary = await asyncio.to_thread(
        store.route_event_summary,
        hours,
        max_latency_ms=ROUTE_LATENCY_WARN_MS,
        max_skill_find_ms=ROUTE_SKILL_FIND_WARN_MS,
        max_injected_tokens=ROUTE_INJECTED_TOKEN_WARN,
        max_response_tokens=ROUTE_RESPONSE_TOKEN_WARN,
    )
    return {"ok": True, **summary, "config_version": CONFIG_VERSION}


@router.post("/route-feedback")
async def route_feedback(body: RouteFeedbackRequest):
    """Local-only outcome feedback for route analytics.

    The public read-only guard intentionally blocks this endpoint for forwarded
    traffic. Keep payloads privacy-safe: route_id plus a small enum-style
    outcome, never raw prompts.
    """
    outcome = (body.outcome or "").strip().lower()
    allowed = {"used", "skipped", "installed", "failed", "dismissed"}
    if outcome not in allowed:
        return Response(status_code=400)
    route_id = (body.route_id or "").strip()
    if not route_id:
        return Response(status_code=400)
    updated = await asyncio.to_thread(
        store.update_route_event_feedback,
        route_id,
        outcome,
        body.source,
        body.note,
    )
    if not updated:
        return Response(status_code=404)
    return {"ok": True, "route_id": route_id, "outcome": outcome}
