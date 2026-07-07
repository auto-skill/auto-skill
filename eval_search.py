"""Retrieval quality eval: old keyword search vs pure vector vs hybrid.

Each case is a natural-language task description plus accept-substrings; a hit
is any result whose name/description/url contains one of them. Reports hit@1
and hit@3 per engine so ranking weights can be tuned with evidence instead of
guesswork.

Run:  python eval_search.py
"""
import asyncio

import httpx

from embeddings import embed_texts
from recommender import HEADERS, SUPABASE_URL

# (query, accept-substrings). Substrings are matched case-insensitively against
# each result's name + description + url.
CASES = [
    ("turn a pdf into an excel spreadsheet", ["xlsx", "pdf", "spreadsheet", "excel"]),
    ("take screenshots of a webpage automatically", ["screenshot", "browser", "playwright", "puppeteer"]),
    ("help me write git commit messages", ["commit", "git"]),
    ("review my pull requests on github", ["review", "pull request", "pr "]),
    ("generate documentation from my codebase", ["doc", "readme"]),
    ("connect claude to my postgres database", ["postgres", "sql", "database"]),
    ("search the web from claude", ["search", "brave", "duckduckgo", "google"]),
    ("manage my kubernetes cluster", ["kubernetes", "k8s", "kubectl"]),
    ("create presentation slides", ["slide", "presentation", "pptx", "powerpoint"]),
    ("scrape data from websites", ["scrape", "crawl", "firecrawl"]),
    ("automate sending emails", ["email", "gmail", "smtp"]),
    ("work with jira tickets", ["jira"]),
    ("query my mongodb collections", ["mongo"]),
    ("draw diagrams from text descriptions", ["diagram", "mermaid", "excalidraw", "graphviz"]),
    ("transcribe audio files to text", ["audio", "transcribe", "whisper", "speech"]),
    ("interact with aws services", ["aws", "amazon"]),
    ("run security audits on my dependencies", ["security", "audit", "vulnerab"]),
    ("translate text between languages", ["translat"]),
    ("track my notion pages", ["notion"]),
    ("control docker containers", ["docker", "container"]),
    ("get stock prices and financial data", ["stock", "financ", "market", "yahoo"]),
    ("edit videos programmatically", ["video", "ffmpeg"]),
    ("send slack messages from claude", ["slack"]),
    ("work with google sheets", ["google sheet", "gsheet", "sheets"]),
    ("memory that persists across claude sessions", ["memory", "remember", "persist"]),
    ("convert markdown to word documents", ["docx", "word", "markdown", "pandoc"]),
    ("monitor errors in production with sentry", ["sentry", "error"]),
    ("browse and query github repositories", ["github", "repo"]),
    ("weather forecasts inside claude", ["weather"]),
    ("generate images with ai", ["image", "dall", "stable diffusion", "flux"]),
]

# Prompts that are NOT delegable tasks: the gated /find-semantic endpoint
# should return nothing for these. Each one routed junk in production before
# the similarity floor existed.
NEGATIVE_CASES = [
    "remember this is a product whatever works for me has to work for everyone else also",
    "ok sounds good lets do it",
    "thanks that worked great",
    "why is the server down right now",
    "can you explain what you just did",
    "hmm let me think about that for a bit",
    "that doesnt look right to me",
]

TOP_K = 3


def is_hit(result: dict, accepts: list[str]) -> bool:
    blob = " ".join([
        result.get("name") or "", result.get("description") or "", result.get("url") or "",
    ]).lower()
    return any(a in blob for a in accepts)


async def run_engine(client: httpx.AsyncClient, engine: str, query: str, vec: list[float]) -> list[dict]:
    if engine == "keyword":
        body, rpc = {"query": query, "max_results": TOP_K}, "search_skills"
    elif engine == "vector":
        body, rpc = {"query_embedding": str(vec), "match_count": TOP_K}, "vector_search_skills"
    else:
        body, rpc = {"query_text": query, "query_embedding": str(vec), "match_count": TOP_K}, "hybrid_search_skills"
    r = await client.post(f"{SUPABASE_URL}/rest/v1/rpc/{rpc}", json=body, headers=HEADERS, timeout=30)
    if r.status_code != 200:
        print(f"  [{engine}] error {r.status_code}: {r.text[:120]}")
        return []
    return r.json()


async def main():
    queries = [q for q, _ in CASES]
    vectors = await asyncio.to_thread(embed_texts, queries)

    scores = {e: {"hit1": 0, "hit3": 0} for e in ("keyword", "vector", "hybrid")}
    async with httpx.AsyncClient() as client:
        for (query, accepts), vec in zip(CASES, vectors):
            line = [query[:44].ljust(46)]
            for engine in scores:
                results = await run_engine(client, engine, query, vec)
                hit1 = bool(results) and is_hit(results[0], accepts)
                hit3 = any(is_hit(r, accepts) for r in results[:TOP_K])
                scores[engine]["hit1"] += hit1
                scores[engine]["hit3"] += hit3
                line.append(f"{engine[:3]}:{'Y' if hit1 else 'y' if hit3 else '.'}")
            print("  ".join(line))

    n = len(CASES)
    print(f"\n{'engine':<10}{'hit@1':>8}{'hit@3':>8}   (n={n};  Y = hit@1, y = hit@3 only, . = miss)")
    for engine, s in scores.items():
        print(f"{engine:<10}{s['hit1']/n:>8.0%}{s['hit3']/n:>8.0%}")

    # Gate check: positives must pass the similarity floor, negatives must not.
    async with httpx.AsyncClient() as client:
        pos_pass = neg_reject = 0
        for query, _ in CASES:
            r = await client.get(f"{SUPABASE_URL}/find-semantic", params={"q": query}, timeout=30)
            if r.status_code == 200 and r.json().get("results"):
                pos_pass += 1
        for query in NEGATIVE_CASES:
            r = await client.get(f"{SUPABASE_URL}/find-semantic", params={"q": query}, timeout=30)
            if r.status_code == 200 and not r.json().get("results"):
                neg_reject += 1
            else:
                print(f"  gate MISS (junk passed): {query[:60]!r}")
    print(f"\ngate: positives passed {pos_pass}/{n}, negatives rejected {neg_reject}/{len(NEGATIVE_CASES)}")


if __name__ == "__main__":
    asyncio.run(main())
