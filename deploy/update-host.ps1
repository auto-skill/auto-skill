param(
    [string]$Branch = "main",
    [string]$BaseUrl = "https://skills.avalahome.com",
    [switch]$AllowDirty,
    [switch]$SkipPull,
    [switch]$SkipInstall,
    [switch]$SkipTests,
    [switch]$RunBackfill,
    [switch]$RunReindex,
    [switch]$ApplyScrapeCleanup,
    [switch]$SkipLaunchCheck
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location -Path $RepoRoot

function Invoke-Native {
    param(
        [string]$Label,
        [scriptblock]$Command
    )

    Write-Host ""
    Write-Host "==> $Label"
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

function Assert-CleanTree {
    if ($AllowDirty) {
        Write-Host "Working tree dirty check skipped because -AllowDirty was passed."
        return
    }

    $dirty = (& git status --porcelain)
    if ($dirty) {
        throw "Working tree has uncommitted changes. Commit/stash them, or pass -AllowDirty after reviewing them."
    }
}

Write-Host "Auto-Skill host update"
Write-Host "Repo: $RepoRoot"
Write-Host "Target branch: $Branch"
Write-Host "Public URL: $BaseUrl"

Assert-CleanTree

$currentBranch = (& git rev-parse --abbrev-ref HEAD).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Could not determine current git branch."
}
if ($currentBranch -ne $Branch) {
    throw "Host checkout is on '$currentBranch', expected '$Branch'. Switch branches manually before running this script."
}

if (-not $SkipPull) {
    Invoke-Native "git fetch origin $Branch" { git fetch origin $Branch }
    Invoke-Native "git pull --ff-only origin $Branch" { git pull --ff-only origin $Branch }
}

if (-not $SkipInstall) {
    Invoke-Native "install Python dependencies" { python -m pip install -r requirements.txt }
}

if (-not $SkipTests) {
    Invoke-Native "unit tests" { python -m unittest discover -s tests -v }
    Invoke-Native "syntax check" {
        python -m py_compile quality.py local_store.py local_api.py recommender.py scraper.py backfill_quality.py cleanup_scrape_runs.py worker.py reindex.py backfill_embeddings.py embeddings.py mcp_server.py eval_search.py launch_check.py tests\test_api_contract.py tests\test_quality.py tests\test_quality_routing.py
    }
}

Invoke-Native "scrape run cleanup dry run" { python cleanup_scrape_runs.py }
if ($ApplyScrapeCleanup) {
    Invoke-Native "scrape run cleanup apply" { python cleanup_scrape_runs.py --apply }
}

if ($RunBackfill) {
    Invoke-Native "quality backfill" { python backfill_quality.py }
} else {
    Write-Host ""
    Write-Host "Skipping quality backfill. Run with -RunBackfill after stopping or quieting the API if legacy rows need refreshed quality metadata."
}

if ($RunReindex) {
    Invoke-Native "embedding reindex" { python reindex.py }
} else {
    Write-Host ""
    Write-Host "Skipping embedding reindex. Run with -RunReindex after the localhost API is running if active rows need refreshed embeddings."
}

Write-Host ""
Write-Host "Restart the host supervisors now if they are still running old Python processes:"
Write-Host "  - API/scraper: start_scraper.ps1 (python scraper.py on localhost:8000)"
Write-Host "  - Connector HTTP: start_connector_http.ps1"
Write-Host "  - Cloudflare tunnel: start_cloudflared.ps1, only if the tunnel process changed"

if (-not $SkipLaunchCheck) {
    Invoke-Native "public launch preflight" {
        python launch_check.py --base-url $BaseUrl --skip-env --skip-docker
    }
}

Write-Host ""
Write-Host "Host update script completed."
