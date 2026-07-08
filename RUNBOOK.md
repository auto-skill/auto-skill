# Auto-Skill Backend Runbook

## Alpha Launch Checklist

1. Back up the current `local_skills.db` and `skills_library/`:
   `.\deploy\backup-local.ps1 -PackContentBlobs`.
2. Run `python backfill_quality.py`.
3. Run `python -m unittest discover`.
4. Start the API.
5. Run `python reindex.py` to refresh active embeddings. This writes through
   the localhost API at `127.0.0.1:8000`, so the API must be running.
6. Verify:
   - `GET /healthz` returns `{"ok": true}`.
   - `GET https://mcp.yourdomain.com/healthz` returns `{"ok": true}` after
     the connector HTTP supervisor and Cloudflare tunnel are running.
   - `GET /readyz` returns `ok=true` with nonzero total, active, and embedded
     row counts, plus `scraper.running_stale=0` and at most one fresh running
     scrape.
   - `POST /route` for `create an excel spreadsheet report with formulas and
     charts` returns `full` or `hint`.
   - `POST /route` for `build a landing page for an AI automation agency` does
     not full-route to a Landingi-specific skill.
   - Route `score_debug.metrics.latency_ms`,
     `score_debug.metrics.skill_find_ms`, and
     `score_debug.metrics.injected_tokens` are under launch budgets.
6. Confirm public forwarded requests cannot reach admin or write endpoints:
   `/chat`, `/scrape`, `/seed-backlog`, `/rescan`, `/normalize-db`,
   `/normalize-db/progress`, `/skills`, `/library`, `/library/files/*`,
   `/route-metrics`, `/route-feedback`, and mutating or read/RPC `/rest/v1/*`
   should be blocked by the read-only guard when forwarded through Cloudflare.
7. Run the launch preflight:

```powershell
python launch_check.py --base-url https://skills.yourdomain.com --mcp-health-url https://mcp.yourdomain.com/healthz
```

For a local dry run before the API is running:

```powershell
python launch_check.py --skip-http --skip-docker --skip-env --skip-local
```

## Current Windows Host Update

The live alpha host currently runs three PowerShell restart loops behind a
Cloudflare Tunnel:

- `start_scraper.ps1`: starts `python scraper.py`, which serves the FastAPI API
  on `localhost:8000` and can also run the scraper loop.
- `start_connector_http.ps1`: starts the connector MCP HTTP service on
  `localhost:8765`. It auto-detects common connector checkout locations; set
  `AUTO_SKILL_CONNECTOR_DIR` before launching if the connector repo lives
  somewhere else.
- `start_cloudflared.ps1`: exposes `skills.avalahome.com` and
  `mcp.avalahome.com` to those local ports.

The first fix for public `403 {"error":"read-only public API"}` responses on
`/readyz` or `/route` is to pull and restart the API host. Old code allowed
public `/healthz` but blocked those newer route/readiness endpoints.

On the host:

```powershell
.\deploy\backup-local.ps1 -PackContentBlobs
git pull --ff-only origin main
.\deploy\update-host.ps1 -SkipPull -RestartTasks
```

`-RestartTasks` restarts `AutoSkill-API`, `AutoSkill-MCP`, and
`AutoSkill-Tunnel` after tests and maintenance steps, then runs the public
launch check. If legacy rows have not been quality-gated yet, run the backfill
while the API is stopped or quiet:

```powershell
python backfill_quality.py
```

Install or refresh the user-level scheduled tasks so the three restart loops
come back after host reboots and a daily local backup runs at `03:15`:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\install-windows-tasks.ps1 -StartNow
```

To inspect before changing Task Scheduler, add `-DryRun`. To schedule the daily
backup at another local time, pass `-BackupAt HH:mm`. Local backup retention
defaults to 14 days; change it with `-BackupRetentionDays N`. Add
`-UploadBackupR2` only after R2 credentials and the AWS CLI are available on
the host. To remove the tasks:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\install-windows-tasks.ps1 -Unregister
```

After the API is running on `localhost:8000`, refresh active embeddings if
needed:

```powershell
python reindex.py
```

Finally prove the public service is serving the new code:

```powershell
python launch_status.py
python launch_check.py --base-url https://skills.avalahome.com --mcp-health-url https://mcp.avalahome.com/healthz --skip-env --skip-docker
```

If either public hostname returns Cloudflare `1033` / HTTP `530`, the tunnel
origin is unreachable. To apply the standard pull, connector pull, task
install/restart, local health waits, public launch check, and failure diagnosis
in one pass, run this on the Windows host:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\recover-host.ps1
```

`recover-host.ps1` updates both the backend checkout and the connector checkout
used by `start_connector_http.ps1`. If the connector repo lives outside the
auto-detected paths, pass `-ConnectorDir C:\path\to\auto-skill-connector`. If
the host intentionally pins connector code during an incident, pass
`-SkipConnectorPull`.

If `/healthz` still serves the stale `{"ok":true,"db_reachable":true}` body
after a normal recovery, an old Python process may still own port `8000`.
Inspect `.\deploy\diagnose-host.ps1`; if the listener command line is an
Auto-Skill scraper/MCP process, rerun recovery with scoped stale-listener
cleanup:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\recover-host.ps1 -StopStalePortOwners
```

If you only want a read-only failure packet, run:

```powershell
.\deploy\diagnose-host.ps1
```

For `AutoSkill-API`, `AutoSkill-MCP`, and `AutoSkill-Tunnel`, the diagnostic
reports Task Scheduler state, last run time, next run time, and last result. A
service task that is installed but not `Running` is a launch failure.

The diagnostic also posts two local route probes to `localhost:8000`: a
direct-hit spreadsheet task and the generic landing-page trap. Those probes
fail if local routing omits `score_debug.metrics`, exceeds the launch budgets
for `latency_ms`, `skill_find_ms`, `injected_tokens`, or `response_tokens`, or
full-routes the trap to a Landingi-specific skill.

If Windows blocks local scripts by execution policy:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\diagnose-host.ps1
```

Fix failures in this order: start or restart `python scraper.py` on
`localhost:8000`, fix local route probes and route budgets, start or restart
the connector HTTP service on `localhost:8765`, then start or restart
`cloudflared tunnel run auto-skill`. Only rerun the public `launch_check.py`
after local API, local route, and MCP checks pass.

Inspect route analytics locally on the host:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/route-metrics | ConvertTo-Json -Depth 5
```

Check `vector_index.valid_vectors`, `cache_ready`, `cache_vectors`, and
`matrix_bytes` there before deciding the brute-force NumPy index is the actual
bottleneck.

Use `p95_skill_find_ms`, `p95_injected_tokens`, `budget_breaches`,
`top_skills`, and `top_used_skills` to spot which routes are slow, expensive,
valuable, over-triggered, or need better skill content. Averages are useful
trend lines, but the p95 and breach counts decide whether a launch build is
quietly churning tokens or hanging on tail routes.

Archive internal eval snapshots before and after routing changes:

```powershell
python eval_search.py --json-out eval-results\$(Get-Date -Format yyyyMMdd-HHmmss).json
python eval_compare.py --fail-on-regression eval-results\before.json eval-results\after.json
```

Route benchmark cases come from `evals\routes.jsonl`. Add cases there for
observed false positives, obvious direct-hit tasks, and conversation/meta
prompts that should stay at `none`.

Route outcome feedback is also local-only:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/route-feedback -ContentType "application/json" -Body '{"route_id":"...","outcome":"used","source":"manual-check"}'
```

`deploy\update-host.ps1` can also run the optional maintenance steps:

```powershell
.\deploy\update-host.ps1 -RunBackfill -RunReindex -ApplyScrapeCleanup
```

After the first update, the script can do the pull itself; keep `-SkipPull`
only when you already pulled in the same session.

## Docker Compose Skeleton

```powershell
Copy-Item deploy\.env.example deploy\.env
# Fill in Cloudflare/R2/GitHub values.
New-Item -ItemType Directory -Force -Path data, skills_library
python deploy\compose_preflight.py
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

`python deploy\compose_preflight.py` fails when launch-critical values are
missing from `deploy\.env`, when the seeded DB/library files are absent, or
when `docker compose config` cannot parse the stack. Use
`--skip-seed-checks --skip-docker` only for CI or local config review before a
real host exists.

## Hosting Upgrade Ladder

Use this ladder to keep the alpha launch practical without pretending the
current laptop tunnel is production hosting:

1. Current emergency host: Windows restart loops plus Cloudflare Tunnel. This
   is acceptable for local debugging and recovery only. Any Cloudflare
   `1033`/HTTP `530` from `launch_check.py` means the public alpha is down.
2. Launch target: one cheap VPS or small VM running `deploy/docker-compose.yml`.
   Keep SQLite on the host disk, replicate it with Litestream to R2, back up
   `skills_library/` to R2, and run exactly one `worker` scraper process.
3. Managed-host fallback: if a VPS is too much operational work, use a service
   with a persistent disk and a background worker. Keep the same SQLite/R2
   model and the same `launch_check.py` gate.
4. Hosted DB migration: move to Turso/libSQL, Postgres/pgvector, or another
   online vector store only after metrics prove a reason. Valid reasons are
   repeated host reliability failures after the VPS move, SQLite write
   contention, a need for multiple live reader regions, or route metrics
   showing `skill_find_ms`/vector cache growth as the bottleneck.

Do not split storage just because raw skill files feel awkward. For alpha,
SQLite plus `skills_library/` backups are cheaper to operate than a new online
DB, object store read path, and migration surface. R2 is useful now for
backups and content-addressed blob exports; it should become runtime storage
only after `/content/{hash}` has a local cache and restore drill.

## Hosted Storage Decision

For alpha, keep runtime state boring:

- `local_skills.db` remains the source of truth on one backend host.
- Litestream replicates SQLite WAL/backups to Cloudflare R2.
- `skills_library/` stays as recoverable SKILL.md files, backed up as tarballs
  and optionally exported as deduped compressed content blobs.
- Only one supervised scraper/worker should write at a time.

Do not move the live route path to Turso/libSQL, Postgres, or a hosted vector
database just to look production-grade. Revisit that when one of these becomes
true: the host is unreliable after scheduled tasks/tunnel fixes, SQLite write
contention shows up in metrics, multiple regions need live reads, or
`skill_find_ms`/vector cache size proves local search is the bottleneck.

## Backups

Litestream covers `/data/local_skills.db` in the compose setup. The
`library-backup` service also uploads a daily tarball of `skills_library/` to
Cloudflare R2 through the S3-compatible API.

Keep both. SQLite has the searchable metadata and vectors; `skills_library/`
currently has the SKILL.md content used for full routes and `/content/{hash}`.

On the current Windows tunnel host, take a one-shot backup before deploys:

```powershell
.\deploy\backup-local.ps1 -PackContentBlobs
```

That writes a timestamped folder under `data\backups\` containing a SQLite
online backup, a `skills_library.tgz` archive, optional compressed content
blobs, and `manifest.json`. Timestamp-named local backup folders older than 14
days are pruned by default; pass `-RetentionDays 0` to disable pruning for a
manual run. To upload the same artifact to R2:

```powershell
.\deploy\backup-local.ps1 -PackContentBlobs -UploadR2 -R2Prefix alpha-host-backups
```

If Windows blocks local scripts by execution policy, run the same command as:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\backup-local.ps1 -PackContentBlobs
```

The Windows scheduled-task installer registers `AutoSkill-Backup` to run the
same local backup daily:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\install-windows-tasks.ps1 -BackupAt 03:15
```

`deploy\diagnose-host.ps1` warns when no backup manifest exists or the newest
manifest under `data\backups\` is older than 30 hours. It also reports backup
footprint and warns when the repo drive has less than 5 GB free; override with
`-MinFreeDiskGb N`.

To prepare deduped compressed content blobs for R2:

```powershell
python pack_content_blobs.py
aws --endpoint-url $env:R2_ENDPOINT s3 sync content_blobs "s3://$env:R2_BUCKET/skills-content/"
```

This is a content-addressed export, not yet the runtime source of truth. Keep
the normal `skills_library/` backup until `/content/{hash}` reads from R2 or a
local cache.

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

For a scratch restore drill that does not replace the live repo paths:

```powershell
.\deploy\restore-local.ps1 -DbPath .\restored\local_skills.db -LibraryArchive .\restored\skills_library.tgz -TargetDataDir .\restore-drill\data -TargetLibraryDir .\restore-drill\skills_library
```

## Health And Readiness

- `/healthz` is for uptime checks: process responding.
- `/readyz` is for serving readiness: DB reachable with at least one total,
  active, and embedded skill row. It also includes `scraper.running_recent`,
  `scraper.running_stale`, `scraper.last_success_at`, and recent run rows.

Use `/healthz` for container health checks and `/readyz` for deployment
promotion checks. Treat `scraper.running_stale > 0` or `running_recent > 1` as
an alpha launch blocker even if search can still serve from the existing DB.

## Scraper Supervision

The compose setup runs scraping in the `worker` service only. Keep
`AUTO_START_SCRAPER=0` on the public API so accidental API restarts do not
start extra scrapes.

Before a worker starts a new run it marks `running` rows older than
`STALE_SCRAPE_RUN_SECONDS` as `stale`, then refuses to start if a fresh
`running` row already exists. If `/status` shows several fresh running rows,
more than one scraper process is active; stop the extra process before
trusting the run counts.

SQLite also enforces a single `status='running'` scrape row, so a duplicate
worker startup should fail fast instead of creating a second active scrape.

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
  the active set first. Use `/route-metrics`, `eval_search.py`, and
  `launch_check.py` latency budgets before migrating to sqlite-vec, libSQL, or
  a hosted vector service.
- `skills_library/` should eventually move into SQLite content rows so one
  Litestream backup covers all runtime state. For alpha, daily R2 tarballs are
  acceptable and easier to operate.
