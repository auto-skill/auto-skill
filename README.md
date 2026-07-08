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
- `GET /readyz` - SQLite is reachable with active embedded rows and scraper
  bookkeeping summary.
- `GET /find-semantic?q=...` - ranked search with `tier`, `score_debug`, and
  `config_version`.
- `POST /route {"task":"..."}` - backend-owned full/hint/none route contract.
- `GET /content/{content_hash}` - immutable cached SKILL.md content when known.
- `GET /route-metrics` - local-only route latency/token analytics summary.
- `POST /route-feedback` - local-only privacy-safe route outcome feedback.

Legacy `/chat` recommender endpoints are local-only experiments. The public
launch route contract is `POST /route`, which is deterministic and reports
latency/token metrics.

The Supabase-shaped `/rest/v1/*` compatibility surface is also local-only. It
exists so the scraper, worker, and recommender can share the SQLite store over
loopback; public clients should use `/route`, `/find-semantic`, and
`/content/{content_hash}`.

`/readyz` and `/route-metrics` include `vector_index` stats so search latency
can be correlated with active corpus size, valid embeddings, and embedding
matrix cache state before moving to a new vector backend. `/readyz` also
includes `scraper.running_recent`, `scraper.running_stale`,
`scraper.last_success_at`, and recent run rows so launch checks can catch
duplicate or stale scraper processes.
The in-process vector matrix cache is invalidated immediately on `skills`
writes, updates, and deletes, so freshly embedded rows do not wait for the TTL
before becoming searchable.

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

To track retrieval quality, route latency, and token churn across changes:

```powershell
python eval_search.py --json-out eval-results/latest.json
python eval_compare.py eval-results/before.json eval-results/latest.json
```

Internal route benchmark cases live in `evals/routes.jsonl`. Add false
positives, direct hits, and conversation/meta negatives there so behavior
changes show up in snapshots without editing Python.

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

- `latency_ms`, `skill_find_ms`, `retrieval_ms`, `rerank_ms`, and `content_ms`
  separate the route budget from skill lookup time.
- `input_tokens`, `candidate_tokens`, `hint_tokens`, `content_tokens`,
  `injected_tokens`, and `response_tokens` track token churn.
- Defaults warn above 1500 ms total latency, 1200 ms skill-find time, 3000
  injected tokens, or 3500 response tokens.

When `/route` returns `tier: "hint"`, it also includes up to three
content-free `candidates` so clients can show a small option set without
injecting full SKILL.md instructions.

Each `/route` call also appends a privacy-safe `route_events` row keyed by a
query hash, not raw prompt text. Use `GET /route-metrics` locally to inspect
recent tier distribution, slow routes, top routed/used skills, and average
skill-find time and injected token size. The public read-only guard
intentionally does not allow `/route-metrics` or `/route-feedback`.

## Deploy Skeleton

`deploy/docker-compose.yml` is a small VPS-oriented skeleton:

- `api`: public/read-oriented FastAPI service.
- `worker`: scraper and embedding loop, writing through the API's local REST
  surface.
- `cloudflared`: tunnel to the API.
- `litestream`: SQLite WAL replication to Cloudflare R2.
- `library-backup`: daily R2 tarballs for `skills_library/` until content
  moves into SQLite.

For launch, prefer this single-host VPS path over a managed online DB rewrite.
The runbook's hosting ladder spells out when to keep SQLite/Litestream/R2 and
when Turso, Postgres/pgvector, or another hosted vector store is actually worth
the migration.

Before starting the compose stack on a host, run:

```powershell
python deploy\compose_preflight.py
```

The current app still stores SKILL.md files under `skills_library/`, so that
directory needs its own backup until content is moved into SQLite.

On the current Windows host, run `.\deploy\backup-local.ps1 -PackContentBlobs`
before deploys to create a timestamped SQLite/library/blob backup under
`data\backups\`; timestamped local backups are pruned after 14 days by
default. Add `-UploadR2` when R2 credentials are available. Use
`.\deploy\install-windows-tasks.ps1` to keep the service loops and daily local
backup registered in Task Scheduler.

For cheaper content-addressed storage, build gzip blobs keyed by normalized
content hash:

```powershell
python pack_content_blobs.py
```

The output in `content_blobs/` can be synced to R2 later without duplicating
identical skill bodies.

See `RUNBOOK.md` for operations notes and the current Windows host update
path.
