# auto-skill backend

FastAPI scraper, local SQLite store, embedding search, and deterministic route
API for Auto-Skill.

This is alpha infrastructure. The goal is a reliable quality-gated router, not
a launch-grade distributed system.

## Run Locally

```powershell
python -m pip install -r requirements.txt
python scraper.py
```

Useful endpoints:

- `GET /healthz` - process is up.
- `GET /readyz` - SQLite is reachable with active embedded rows.
- `GET /find-semantic?q=...` - ranked search with `tier`, `score_debug`, and
  `config_version`.
- `POST /route {"task":"..."}` - backend-owned full/hint/none route contract.
- `GET /content/{content_hash}` - immutable cached SKILL.md content when known.
- `GET /route-metrics` - local-only route latency/token analytics summary.
- `POST /route-feedback` - local-only privacy-safe route outcome feedback.

`/readyz` and `/route-metrics` include `vector_index` stats so search latency
can be correlated with active corpus size, valid embeddings, and embedding
matrix cache state before moving to a new vector backend.

## Quality Gate

Fresh ingest writes deterministic quality metadata:

- `quality_status`: `active`, `metadata_only`, `rejected`, or `duplicate`.
- `quality_reasons`: machine-readable gate reasons.
- `quality_score`: 0-100.
- `content_hash`: normalized SHA-256 for dedupe.
- `platforms` and `category`: cheap tags used by routing.

Only `active` rows are eligible for vector embedding and full routes.
`metadata_only` rows can still appear as hints.

Backfill existing rows before using public routing:

```powershell
python backfill_quality.py
python reindex.py
python launch_check.py --base-url http://127.0.0.1:8000
```

## Routing Policy

Runtime routing is deterministic. It uses local embeddings for retrieval, then
reranks with lexical overlap, quality score, platform mismatch, and a capped
popularity prior. Platform-specific skills are capped to `hint` unless the
prompt names that platform. This is intended to prevent traps like a generic
landing-page prompt full-routing to a Landingi support skill.

Ollama chat selection is disabled by default. Set `ENABLE_OLLAMA_CHAT=1` only
for local experiments; production `/route` does not depend on an LLM.

Route responses include `score_debug.metrics` with cheap latency and token
estimates:

- `latency_ms`, `retrieval_ms`, and `content_ms` track skill-find time.
- `input_tokens`, `hint_tokens`, `content_tokens`, and `response_tokens` track
  token churn.
- Defaults warn above 1500 ms or 3500 response tokens.

Each `/route` call also appends a privacy-safe `route_events` row keyed by a
query hash, not raw prompt text. Use `GET /route-metrics` locally to inspect
recent tier distribution, slow routes, and average response token size. The
public read-only guard intentionally does not allow `/route-metrics` or
`/route-feedback`.

## Deploy Skeleton

`deploy/docker-compose.yml` is a small VPS-oriented skeleton:

- `api`: public/read-oriented FastAPI service.
- `worker`: scraper and embedding loop, writing through the API's local REST
  surface.
- `cloudflared`: tunnel to the API.
- `litestream`: SQLite WAL replication to Cloudflare R2.
- `library-backup`: daily R2 tarballs for `skills_library/` until content
  moves into SQLite.

The current app still stores SKILL.md files under `skills_library/`, so that
directory needs its own backup until content is moved into SQLite.

For cheaper content-addressed storage, build gzip blobs keyed by normalized
content hash:

```powershell
python pack_content_blobs.py
```

The output in `content_blobs/` can be synced to R2 later without duplicating
identical skill bodies.

See `RUNBOOK.md` for operations notes and the current Windows host update
path.
