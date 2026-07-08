param(
    [string]$Branch = "main",
    [string]$BaseUrl = "https://skills.avalahome.com",
    [string]$McpHealthUrl = "https://mcp.avalahome.com/healthz",
    [switch]$AllowDirty,
    [switch]$SkipPull,
    [switch]$SkipInstall,
    [switch]$SkipTests,
    [switch]$RunBackfill,
    [switch]$RunReindex,
    [switch]$ApplyScrapeCleanup,
    [switch]$RestartTasks,
    [string]$TaskPrefix = "AutoSkill",
    [int]$RestartWaitSeconds = 10,
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

function Restart-HostTask {
    param([string]$TaskName)

    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        throw "Scheduled task '$TaskName' is not installed. Run deploy\install-windows-tasks.ps1 -StartNow first."
    }
    if ($task.State -eq "Running") {
        Write-Host "Stopping $TaskName"
        Stop-ScheduledTask -TaskName $TaskName
    }
    Write-Host "Starting $TaskName"
    Start-ScheduledTask -TaskName $TaskName
}

Write-Host "Auto-Skill host update"
Write-Host "Repo: $RepoRoot"
Write-Host "Target branch: $Branch"
Write-Host "Public URL: $BaseUrl"
Write-Host "MCP health URL: $McpHealthUrl"

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
        $oldPrefix = $env:PYTHONPYCACHEPREFIX
        $env:PYTHONPYCACHEPREFIX = Join-Path $env:TEMP "autoskill-pycache-$([guid]::NewGuid().ToString('N'))"
        try {
            python -m compileall -q -x "(\.git|__pycache__|\.venv|venv|data|skills_library|content_blobs|eval-results)" .
        } finally {
            Remove-Item -LiteralPath $env:PYTHONPYCACHEPREFIX -Recurse -Force -ErrorAction SilentlyContinue
            $env:PYTHONPYCACHEPREFIX = $oldPrefix
        }
    }
    Invoke-Native "PowerShell script parse check" {
        [scriptblock]::Create((Get-Content -Raw deploy\backup-local.ps1)) | Out-Null
        [scriptblock]::Create((Get-Content -Raw deploy\diagnose-host.ps1)) | Out-Null
        [scriptblock]::Create((Get-Content -Raw deploy\install-windows-tasks.ps1)) | Out-Null
        [scriptblock]::Create((Get-Content -Raw deploy\recover-host.ps1)) | Out-Null
        [scriptblock]::Create((Get-Content -Raw deploy\restore-local.ps1)) | Out-Null
        [scriptblock]::Create((Get-Content -Raw deploy\update-host.ps1)) | Out-Null
        [scriptblock]::Create((Get-Content -Raw start_cloudflared.ps1)) | Out-Null
        [scriptblock]::Create((Get-Content -Raw start_connector_http.ps1)) | Out-Null
        [scriptblock]::Create((Get-Content -Raw start_scraper.ps1)) | Out-Null
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

if ($RestartTasks) {
    Write-Host ""
    Write-Host "Restarting host scheduled tasks..."
    foreach ($taskName in @("$TaskPrefix-API", "$TaskPrefix-MCP", "$TaskPrefix-Tunnel")) {
        Restart-HostTask $taskName
    }
    if ($RestartWaitSeconds -gt 0) {
        Write-Host "Waiting $RestartWaitSeconds second(s) for restarted services to bind..."
        Start-Sleep -Seconds $RestartWaitSeconds
    }
} else {
    Write-Host ""
    Write-Host "Restart the host supervisors now if they are still running old Python processes:"
    Write-Host "  - API/scraper: start_scraper.ps1 (python scraper.py on localhost:8000)"
    Write-Host "  - Connector HTTP: start_connector_http.ps1"
    Write-Host "  - Cloudflare tunnel: start_cloudflared.ps1, only if the tunnel process changed"
    Write-Host "Or rerun this script with -RestartTasks after installing scheduled tasks."
}

if (-not $SkipLaunchCheck) {
    Invoke-Native "public launch preflight" {
        python launch_check.py --base-url $BaseUrl --mcp-health-url $McpHealthUrl --skip-env --skip-docker
    }
}

Write-Host ""
Write-Host "Host update script completed."
