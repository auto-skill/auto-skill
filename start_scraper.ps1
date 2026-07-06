$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot

try {
    $env:GITHUB_TOKEN = (gh auth token 2>$null)
} catch {
    $env:GITHUB_TOKEN = ""
}

# Storage moved local 2026-07-05 (Supabase free-tier space ran out) -- the
# scraper now writes to local_skills.db via local_api.py, no Supabase key
# needed. SUPABASE_SERVICE_KEY is unused as of this change.

# Set this to your self-hosted SearXNG instance (e.g. "http://localhost:8888")
# to enable general web search beyond GitHub/npm/registries. Leave unset to skip it.
if (-not $env:SEARXNG_URL) {
    $env:SEARXNG_URL = "http://localhost:8888"
}

while ($true) {
    python "$PSScriptRoot\scraper.py" *>> "$PSScriptRoot\scraper.log"
    Add-Content -Path "$PSScriptRoot\scraper.log" -Value "$(Get-Date -Format o) [start_scraper] process exited, restarting in 10s"
    Start-Sleep -Seconds 10
}
