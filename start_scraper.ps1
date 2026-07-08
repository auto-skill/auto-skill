$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot

$logPath = Join-Path $PSScriptRoot "scraper.log"
$env:PYTHONUNBUFFERED = "1"

function Write-Log {
    param([string]$Message)
    Add-Content -Path $logPath -Value "$(Get-Date -Format o) [start_scraper] $Message"
}

Write-Log "supervisor starting in $PSScriptRoot"

try {
    $env:GITHUB_TOKEN = (gh auth token 2>$null)
} catch {
    $env:GITHUB_TOKEN = ""
}
if ($env:GITHUB_TOKEN) {
    Write-Log "GitHub token loaded from gh auth"
} else {
    Write-Log "GitHub token unavailable; scraper will use unauthenticated GitHub limits"
}

# Storage moved local 2026-07-05 (Supabase free-tier space ran out) -- the
# scraper now writes to local_skills.db via local_api.py, no Supabase key
# needed. SUPABASE_SERVICE_KEY is unused as of this change.

# Set this to your self-hosted SearXNG instance (e.g. "http://localhost:8888")
# to enable general web search beyond GitHub/npm/registries. Leave unset to skip it.
if (-not $env:SEARXNG_URL) {
    $env:SEARXNG_URL = "http://localhost:8888"
}
Write-Log "SEARXNG_URL=$env:SEARXNG_URL; AUTO_START_SCRAPER=$env:AUTO_START_SCRAPER; LOCAL_DB_PATH=$env:LOCAL_DB_PATH"

while ($true) {
    Write-Log "starting python scraper.py"
    python "$PSScriptRoot\scraper.py" *>> $logPath
    Write-Log "process exited with code $LASTEXITCODE, restarting in 10s"
    Start-Sleep -Seconds 10
}
