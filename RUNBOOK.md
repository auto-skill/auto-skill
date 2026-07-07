# Auto-Skill Backend Runbook

## Alpha Launch Checklist

1. Back up the current `local_skills.db` and `skills_library/`.
2. Run `python backfill_quality.py`.
3. Run `python -m unittest discover`.
4. Run `python reindex.py` to refresh active embeddings.
5. Start the API and verify:
   - `GET /healthz` returns `{"ok": true}`.
   - `GET /readyz` returns `ok=true` with nonzero total, active, and embedded
     row counts.
   - `POST /route` for `create an excel spreadsheet report with formulas and
     charts` returns `full` or `hint`.
   - `POST /route` for `build a landing page for an AI automation agency` does
     not full-route to a Landingi-specific skill.
6. Confirm public forwarded requests cannot reach write endpoints:
   `/scrape`, `/rescan`, `/normalize-db`, and mutating `/rest/v1/*` should be
   blocked by the read-only guard when forwarded through Cloudflare.
7. Run the launch preflight:

```powershell
python launch_check.py --base-url https://skills.yourdomain.com
```

For a local dry run before the API is running:

```powershell
python launch_check.py --skip-http --skip-docker --skip-env
```

## Docker Compose Skeleton

```powershell
Copy-Item deploy\.env.example deploy\.env
# Fill in Cloudflare/R2/GitHub values.
New-Item -ItemType Directory -Force -Path data, skills_library
docker compose --env-file deploy\.env -f deploy\docker-compose.yml up -d --build
```

The compose file is intentionally small. It keeps SQLite, Cloudflare Tunnel,
and Litestream. It does not introduce Postgres, Redis, queues, Kubernetes, or a
new vector server.

The compose file uses bind mounts instead of opaque Docker volumes:

- `data/local_skills.db` is mounted at `/data/local_skills.db`.
- `skills_library/` is mounted at `/app/skills_library`.

Seed a VPS by copying the current DB and library into those paths before the
first `docker compose up`.

## Backups

Litestream covers `/data/local_skills.db` in the compose setup. The
`library-backup` service also uploads a daily tarball of `skills_library/` to
Cloudflare R2 through the S3-compatible API.

Keep both. SQLite has the searchable metadata and vectors; `skills_library/`
currently has the SKILL.md content used for full routes and `/content/{hash}`.

Useful backup checks:

```bash
docker compose --env-file deploy/.env -f deploy/docker-compose.yml logs --tail=50 litestream
docker compose --env-file deploy/.env -f deploy/docker-compose.yml logs --tail=50 library-backup
```

Monthly restore drill:

1. Restore `local_skills.db` from Litestream into a scratch directory.
2. Restore a recent `skills_library` archive.
3. Copy them into `data/local_skills.db` and `skills_library/`.
4. Start the API.
5. Run `python -m unittest discover -s tests`.
6. Probe `/route` with the platform trap and a direct-hit query.

On Windows, after downloading restored artifacts:

```powershell
.\deploy\restore-local.ps1 -DbPath .\restored\local_skills.db -LibraryArchive .\restored\skills_library.tgz
```

## Health And Readiness

- `/healthz` is for uptime checks: process responding.
- `/readyz` is for serving readiness: DB reachable with at least one total,
  active, and embedded skill row.

Use `/healthz` for container health checks and `/readyz` for deployment
promotion checks.

## Scraper Supervision

The compose setup runs scraping in the `worker` service only. Keep
`AUTO_START_SCRAPER=0` on the public API so accidental API restarts do not
start extra scrapes.

Before a worker starts a new run it marks `running` rows older than
`STALE_SCRAPE_RUN_SECONDS` as `stale`, then refuses to start if a fresh
`running` row already exists. If `/status` shows several fresh running rows,
more than one scraper process is active; stop the extra process before
trusting the run counts.

After stopping the extra process, clean up stale bookkeeping rows:

```powershell
python cleanup_scrape_runs.py
python cleanup_scrape_runs.py --apply
```

## Known Alpha Limits

- The API and worker are split in compose, but the public app still includes
  local REST write routes for the internal worker. The read-only middleware is
  the public safety boundary; Cloudflare must route only to the API.
- Existing legacy rows need `backfill_quality.py` before quality metrics are
  trustworthy.
- The brute-force NumPy vector cache remains. Quality backfill should shrink
  the active set first; sqlite-vec is a later measured migration, not a P0.
- `skills_library/` should eventually move into SQLite content rows so one
  Litestream backup covers all runtime state. For alpha, daily R2 tarballs are
  acceptable and easier to operate.
