"""FastAPI router exposing local_store.py through the same tiny slice of the
PostgREST REST + RPC surface that scraper.py / recommender.py already speak
(GET/POST/PATCH/DELETE on /rest/v1/{skills,scrape_runs}, plus the three
/rest/v1/rpc/* search functions). Mounted into scraper.py's own FastAPI app
so scraper.py can just point SUPABASE_URL at its own loopback address.
"""
import asyncio

from fastapi import APIRouter, Request, Response

import local_store as store

router = APIRouter()

_KNOWN_PARAMS = {"select", "order", "limit", "offset", "on_conflict"}


@router.on_event("startup")
async def _init():
    store.init_db()


def _parse_filters(query_params) -> dict:
    return {k: v for k, v in query_params.items() if k not in _KNOWN_PARAMS}


@router.get("/rest/v1/{table}")
async def rest_get(table: str, request: Request):
    if table not in store.TABLES:
        return Response(status_code=404)
    params = dict(request.query_params)
    filters = _parse_filters(params)
    select = params.get("select")
    order = params.get("order")
    limit = int(params["limit"]) if "limit" in params else None

    range_start = range_end = None
    range_header = request.headers.get("range")
    if range_header and "-" in range_header:
        a, b = range_header.split("-", 1)
        range_start, range_end = int(a), int(b)
    elif "offset" in params:
        offset = int(params["offset"])
        range_start, range_end = offset, offset + (limit or 100) - 1

    count_exact = "count=exact" in (request.headers.get("prefer") or "")
    rows, total = await asyncio.to_thread(store.select_rows, table, select, filters, order, limit, range_start, range_end, count_exact)

    headers = {}
    if count_exact:
        end = (range_start or 0) + len(rows) - 1
        headers["Content-Range"] = f"{range_start or 0}-{max(end, 0)}/{total}"
    return Response(content=_dumps(rows), media_type="application/json", headers=headers)


@router.post("/rest/v1/{table}")
async def rest_post(table: str, request: Request):
    if table not in store.TABLES:
        return Response(status_code=404)
    body = await request.json()
    rows = body if isinstance(body, list) else [body]
    on_conflict = request.query_params.get("on_conflict")
    out = await asyncio.to_thread(store.upsert_rows, table, rows, on_conflict)
    return Response(content=_dumps(out), media_type="application/json")


@router.patch("/rest/v1/{table}")
async def rest_patch(table: str, request: Request):
    if table not in store.TABLES:
        return Response(status_code=404)
    filters = _parse_filters(dict(request.query_params))
    data = await request.json()
    await asyncio.to_thread(store.update_rows, table, filters, data)
    return Response(status_code=204)


@router.delete("/rest/v1/{table}")
async def rest_delete(table: str, request: Request):
    if table not in store.TABLES:
        return Response(status_code=404)
    filters = _parse_filters(dict(request.query_params))
    await asyncio.to_thread(store.delete_rows, table, filters)
    return Response(status_code=204)


@router.post("/rest/v1/rpc/search_skills")
async def rpc_search_skills(request: Request):
    body = await request.json()
    rows = await asyncio.to_thread(store.search_skills_fts, body.get("query", ""), body.get("max_results", 10))
    return Response(content=_dumps(rows), media_type="application/json")


@router.post("/rest/v1/rpc/vector_search_skills")
async def rpc_vector_search_skills(request: Request):
    body = await request.json()
    emb = _parse_embedding(body.get("query_embedding"))
    rows = await asyncio.to_thread(store.vector_search_skills, emb, body.get("match_count", 10))
    return Response(content=_dumps(rows), media_type="application/json")


@router.post("/rest/v1/rpc/hybrid_search_skills")
async def rpc_hybrid_search_skills(request: Request):
    body = await request.json()
    emb = _parse_embedding(body.get("query_embedding"))
    rows = await asyncio.to_thread(
        store.hybrid_search_skills,
        body.get("query_text", ""),
        emb,
        body.get("match_count", 10),
        body.get("fts_weight", 1.0),
        body.get("vec_weight", 0.6),
        body.get("rrf_k", 20),
    )
    return Response(content=_dumps(rows), media_type="application/json")


def _parse_embedding(val):
    if val is None:
        return None
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        import json as _json
        return _json.loads(val)
    return None


def _dumps(obj) -> str:
    import json as _json
    return _json.dumps(obj)
