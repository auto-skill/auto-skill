param(
    [string]$BaseUrl = "https://skills.avalahome.com",
    [string]$McpHealthUrl = "https://mcp.avalahome.com/healthz",
    [string]$LocalApiUrl = "http://127.0.0.1:8000",
    [string]$LocalMcpHealthUrl = "http://127.0.0.1:8765/healthz",
    [string]$TaskPrefix = "AutoSkill",
    [int]$MaxBackupAgeHours = 30
)

$ErrorActionPreference = "Continue"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location -Path $RepoRoot

$failures = 0
$warnings = 0

function Pass {
    param([string]$Name, [string]$Detail)
    Write-Host "[PASS] $Name`: $Detail"
}

function Warn {
    param([string]$Name, [string]$Detail)
    $script:warnings += 1
    Write-Host "[WARN] $Name`: $Detail"
}

function Fail {
    param([string]$Name, [string]$Detail)
    $script:failures += 1
    Write-Host "[FAIL] $Name`: $Detail"
}

function Test-JsonEndpoint {
    param(
        [string]$Name,
        [string]$Url,
        [switch]$RequireOk,
        [switch]$RequireService
    )

    try {
        $response = Invoke-RestMethod -Uri $Url -Headers @{ Accept = "application/json" } -TimeoutSec 10
        $json = $response | ConvertTo-Json -Depth 8 -Compress
        if ($RequireOk -and $response.ok -ne $true) {
            Fail $Name "response did not include ok=true: $json"
            return
        }
        if ($RequireService -and $response.service -ne "auto-skill-api") {
            Fail $Name "stale API response; expected service=auto-skill-api: $json"
            return
        }
        Pass $Name $json
    } catch {
        Fail $Name $_.Exception.Message
    }
}

function Test-PathPresent {
    param([string]$Name, [string]$Path)
    if (Test-Path -LiteralPath $Path) {
        $item = Get-Item -LiteralPath $Path
        Pass $Name $item.FullName
    } else {
        Fail $Name "missing $Path"
    }
}

Write-Host "Auto-Skill host diagnosis"
Write-Host "Repo: $RepoRoot"
try {
    $head = (& git rev-parse --short HEAD).Trim()
    Pass "git head" $head
} catch {
    Warn "git head" $_.Exception.Message
}

$dbCandidates = @()
if ($env:LOCAL_DB_PATH) {
    $dbCandidates += $env:LOCAL_DB_PATH
}
$dbCandidates += @(
    (Join-Path $RepoRoot "data\local_skills.db"),
    (Join-Path $RepoRoot "local_skills.db")
)
$dbPath = $dbCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if ($dbPath) {
    Pass "local db" $dbPath
} else {
    Fail "local db" "no local_skills.db found; checked $($dbCandidates -join ', ')"
}

Test-PathPresent "skills library index" (Join-Path $RepoRoot "skills_library\index.json")

$cloudflaredExe = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
$cloudflaredConfig = Join-Path $env:USERPROFILE ".cloudflared\config.yml"
Test-PathPresent "cloudflared exe" $cloudflaredExe
Test-PathPresent "cloudflared config" $cloudflaredConfig

$cloudflaredProcesses = Get-Process -Name cloudflared -ErrorAction SilentlyContinue
if ($cloudflaredProcesses) {
    $ids = ($cloudflaredProcesses | Select-Object -ExpandProperty Id) -join ", "
    Pass "cloudflared process" "running pid(s): $ids"
} else {
    Fail "cloudflared process" "not running; public Cloudflare Tunnel will return 1033/HTTP 530"
}

foreach ($taskName in @("$TaskPrefix-API", "$TaskPrefix-MCP", "$TaskPrefix-Tunnel", "$TaskPrefix-Backup")) {
    try {
        $task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
        Pass "scheduled task $taskName" "state=$($task.State)"
    } catch {
        Warn "scheduled task $taskName" "not installed; run deploy\install-windows-tasks.ps1 on the host"
    }
}

$backupRoot = Join-Path $RepoRoot "data\backups"
if (-not (Test-Path -LiteralPath $backupRoot)) {
    Warn "backup freshness" "backup directory missing: $backupRoot"
} else {
    $latestManifest = Get-ChildItem -LiteralPath $backupRoot -Recurse -Filter manifest.json -File |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1
    if (-not $latestManifest) {
        Warn "backup freshness" "no manifest.json found under $backupRoot"
    } else {
        $ageHours = ((Get-Date).ToUniversalTime() - $latestManifest.LastWriteTimeUtc).TotalHours
        $detail = "latest=$($latestManifest.FullName), age_hours=$([math]::Round($ageHours, 1))"
        if ($ageHours -gt $MaxBackupAgeHours) {
            Warn "backup freshness" "$detail, max_hours=$MaxBackupAgeHours"
        } else {
            Pass "backup freshness" $detail
        }
    }
}

Test-JsonEndpoint "local API healthz" "$($LocalApiUrl.TrimEnd('/'))/healthz" -RequireOk -RequireService
Test-JsonEndpoint "local API readyz" "$($LocalApiUrl.TrimEnd('/'))/readyz" -RequireOk
Test-JsonEndpoint "local MCP healthz" $LocalMcpHealthUrl -RequireOk
Test-JsonEndpoint "public API healthz" "$($BaseUrl.TrimEnd('/'))/healthz" -RequireOk -RequireService
Test-JsonEndpoint "public MCP healthz" $McpHealthUrl -RequireOk

Write-Host ""
if ($failures -gt 0) {
    Write-Host "diagnose-host: $failures failure(s), $warnings warning(s)"
    exit 1
}
Write-Host "diagnose-host: passed with $warnings warning(s)"
