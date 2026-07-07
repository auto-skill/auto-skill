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
- `GET /readyz` - SQLite is reachable and has rows.
- `GET /find-semantic?q=...` - ranked search with `tier`, `score_debug`, and
  `config_version`.
- `POST /route {"task":"..."}` - backend-owned full/hint/none route contract.
- `GET /content/{content_hash}` - immutable cached SKILL.md content when known.

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
```

## Routing Policy

Runtime routing is deterministic. It uses local embeddings for retrieval, then
reranks with lexical overlap, quality score, platform mismatch, and a capped
popularity prior. Platform-specific skills are capped to `hint` unless the
prompt names that platform. This is intended to prevent traps like a generic
landing-page prompt full-routing to a Landingi support skill.

Ollama chat selection is disabled by default. Set `ENABLE_OLLAMA_CHAT=1` only
for local experiments; production `/route` does not depend on an LLM.

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

See `RUNBOOK.md` for operations notes.
