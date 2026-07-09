param(
    [string]$BaseUrl = "https://skills.avalahome.com",
    [string]$McpHealthUrl = "https://mcp.avalahome.com/healthz",
    [string]$LocalApiUrl = "http://127.0.0.1:8000",
    [string]$LocalMcpHealthUrl = "http://127.0.0.1:8765/healthz",
    [string]$TaskPrefix = "AutoSkill",
    [int]$MaxBackupAgeHours = 30,
    [int]$MinFreeDiskGb = 5,
    [string]$DirectTask = "create an excel spreadsheet report with formulas and charts",
    [string]$TrapTask = "build a landing page for an AI automation agency",
    [int]$MaxRouteLatencyMs = 1500,
    [int]$MaxRouteSkillFindMs = 1200,
    [int]$MaxRouteInjectedTokens = 3000,
    [int]$MaxRouteResponseTokens = 3500
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

function Get-JsonValue {
    param(
        $Object,
        [string]$Name
    )

    if ($null -eq $Object) {
        return $null
    }
    if ($Object -is [System.Collections.IDictionary] -and $Object.Contains($Name)) {
        return $Object[$Name]
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($property) {
        return $property.Value
    }
    return $null
}

function Get-MetricInt {
    param(
        $Metrics,
        [string[]]$Names
    )

    foreach ($name in $Names) {
        $value = Get-JsonValue $Metrics $name
        if ($null -ne $value) {
            return [int]$value
        }
    }
    return 0
}

function Test-RouteBudget {
    param(
        [string]$Name,
        $Body
    )

    $scoreDebug = Get-JsonValue $Body "score_debug"
    $metrics = Get-JsonValue $scoreDebug "metrics"
    if ($null -eq $metrics) {
        Fail $Name "route response did not include score_debug.metrics"
        return
    }

    $latencyMs = Get-MetricInt $metrics @("latency_ms")
    $skillFindMs = Get-MetricInt $metrics @("skill_find_ms", "retrieval_ms")
    $injectedTokens = Get-MetricInt $metrics @("injected_tokens", "content_tokens")
    $responseTokens = Get-MetricInt $metrics @("response_tokens")

    if ($latencyMs -gt $MaxRouteLatencyMs) {
        Fail $Name "latency_ms=$latencyMs exceeded budget $MaxRouteLatencyMs"
    } elseif ($skillFindMs -gt $MaxRouteSkillFindMs) {
        Fail $Name "skill_find_ms=$skillFindMs exceeded budget $MaxRouteSkillFindMs"
    } elseif ($injectedTokens -gt $MaxRouteInjectedTokens) {
        Fail $Name "injected_tokens=$injectedTokens exceeded budget $MaxRouteInjectedTokens"
    } elseif ($responseTokens -gt $MaxRouteResponseTokens) {
        Fail $Name "response_tokens=$responseTokens exceeded budget $MaxRouteResponseTokens"
    } else {
        Pass $Name "latency_ms=$latencyMs, skill_find_ms=$skillFindMs, injected_tokens=$injectedTokens, response_tokens=$responseTokens"
    }
}

function Test-LocalRouteMetrics {
    $metricsUrl = "$($LocalApiUrl.TrimEnd('/'))/route-metrics"
    try {
        $body = Invoke-RestMethod -Uri $metricsUrl -Headers @{ Accept = "application/json" } -TimeoutSec 10
    } catch {
        Warn "local route metrics" $_.Exception.Message
        return
    }

    $total = Get-MetricInt $body @("total")
    $breaches = Get-JsonValue $body "budget_breaches"
    $breachAny = Get-MetricInt $breaches @("any")
    $latencyBreaches = Get-MetricInt $breaches @("latency_ms")
    $skillFindBreaches = Get-MetricInt $breaches @("skill_find_ms")
    $injectedBreaches = Get-MetricInt $breaches @("injected_tokens")
    $responseBreaches = Get-MetricInt $breaches @("response_tokens")

    if ($total -le 0) {
        Warn "local route metrics" "no recent route events yet; route probes should populate this before launch"
        return
    }

    if ($breachAny -gt 0) {
        Fail "local route metrics" "recent budget breaches: any=$breachAny, latency_ms=$latencyBreaches, skill_find_ms=$skillFindBreaches, injected_tokens=$injectedBreaches, response_tokens=$responseBreaches"
        return
    }

    $p95Latency = Get-MetricInt $body @("p95_latency_ms")
    $p95SkillFind = Get-MetricInt $body @("p95_skill_find_ms")
    $p95Injected = Get-MetricInt $body @("p95_injected_tokens")
    $p95Response = Get-MetricInt $body @("p95_response_tokens")
    Pass "local route metrics" "total=$total, p95_latency_ms=$p95Latency, p95_skill_find_ms=$p95SkillFind, p95_injected_tokens=$p95Injected, p95_response_tokens=$p95Response"
}

function Invoke-RouteProbe {
    param(
        [string]$Name,
        [string]$Task
    )

    $routeUrl = "$($LocalApiUrl.TrimEnd('/'))/route"
    $payload = @{
        task = $Task
        client = "diagnose-host"
        client_version = "local"
    } | ConvertTo-Json -Depth 4

    try {
        return Invoke-RestMethod -Method Post -Uri $routeUrl -Headers @{ Accept = "application/json" } -ContentType "application/json" -Body $payload -TimeoutSec 20
    } catch {
        Fail $Name $_.Exception.Message
        return $null
    }
}

function Test-LocalRouteDirect {
    $response = Invoke-RouteProbe "local route direct" $DirectTask
    if ($null -eq $response) {
        return
    }

    $tier = [string](Get-JsonValue $response "tier")
    $skill = Get-JsonValue $response "skill"
    $skillName = Get-JsonValue $skill "name"
    if (-not $skillName) {
        $skillName = Get-JsonValue $skill "slug"
    }

    if (($tier -eq "full" -or $tier -eq "hint") -and $skill) {
        Pass "local route direct" "tier=$tier, skill=$skillName"
        Test-RouteBudget "local route direct budget" $response
    } else {
        $json = $response | ConvertTo-Json -Depth 8 -Compress
        Fail "local route direct" "expected tier full|hint with skill: $json"
    }
}

function Test-LocalRouteTrap {
    $response = Invoke-RouteProbe "local route trap" $TrapTask
    if ($null -eq $response) {
        return
    }

    $tier = [string](Get-JsonValue $response "tier")
    $skill = Get-JsonValue $response "skill"
    $skillName = Get-JsonValue $skill "name"
    if (-not $skillName) {
        $skillName = Get-JsonValue $skill "slug"
    }
    $skillBlob = "$skillName $(Get-JsonValue $skill "url") $(Get-JsonValue $skill "source_url")".ToLowerInvariant()

    if ($tier -eq "full" -and $skillBlob.Contains("landingi")) {
        Fail "local route trap" "generic landing-page prompt full-routed to Landingi"
        return
    }

    Pass "local route trap" "tier=$tier, skill=$skillName"
    Test-RouteBudget "local route trap budget" $response
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

function Test-SeedRuntime {
    param(
        [string]$DbPath,
        [string]$LibraryDir
    )

    if (-not $DbPath -or -not (Test-Path -LiteralPath $DbPath)) {
        Fail "seed runtime" "local_skills.db is missing; seed or restore with deploy\seed_runtime.py"
        return
    }
    if (-not (Test-Path -LiteralPath $LibraryDir)) {
        Fail "seed runtime" "skills_library is missing at $LibraryDir; seed or restore with deploy\seed_runtime.py"
        return
    }

    $code = @'
import json
import sqlite3
import sys
from pathlib import Path

db_path = Path(sys.argv[1])
library_dir = Path(sys.argv[2])
summary = {
    "db_path": str(db_path),
    "library_dir": str(library_dir),
    "total": 0,
    "active": 0,
    "embedded": 0,
    "index_entries": 0,
    "markdown_files": 0,
}
conn = sqlite3.connect(db_path)
try:
    row = conn.execute(
        """
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN COALESCE(quality_status, 'active') = 'active' THEN 1 ELSE 0 END) AS active,
          SUM(CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END) AS embedded
        FROM skills
        """
    ).fetchone()
    summary["total"] = int(row[0] or 0)
    summary["active"] = int(row[1] or 0)
    summary["embedded"] = int(row[2] or 0)
finally:
    conn.close()

index_path = library_dir / "index.json"
if index_path.exists():
    index = json.loads(index_path.read_text(encoding="utf-8-sig"))
    if isinstance(index, list):
        summary["index_entries"] = len(index)
files_dir = library_dir / "files"
if files_dir.exists():
    summary["markdown_files"] = len(list(files_dir.glob("*.md")))
print(json.dumps(summary, sort_keys=True))
'@

    try {
        $raw = & python -c $code $DbPath $LibraryDir 2>&1
        if ($LASTEXITCODE -ne 0) {
            Fail "seed runtime" "could not inspect DB/library: $($raw -join ' ')"
            return
        }
        $summary = ($raw | Select-Object -Last 1) | ConvertFrom-Json
    } catch {
        Fail "seed runtime" "could not inspect DB/library: $($_.Exception.Message)"
        return
    }

    $detail = "total=$($summary.total), active=$($summary.active), embedded=$($summary.embedded), index_entries=$($summary.index_entries), markdown_files=$($summary.markdown_files)"
    if ([int]$summary.total -lt 1 -or [int]$summary.active -lt 1 -or [int]$summary.index_entries -lt 1 -or [int]$summary.markdown_files -lt 1) {
        Fail "seed runtime" "$detail; runtime DB/library is empty or not mounted. Seed or restore with python deploy\seed_runtime.py"
    } elseif ([int]$summary.embedded -lt 1) {
        Fail "seed runtime" "$detail; DB has skills but no embeddings. Run python reindex.py or start the worker"
    } else {
        Pass "seed runtime" $detail
    }
}

function Get-UriValue {
    param([string]$Url)
    try {
        return [Uri]$Url
    } catch {
        Warn "parse URL" "could not parse $Url`: $($_.Exception.Message)"
        return $null
    }
}

function Get-EndpointPort {
    param($Uri)
    if ($null -eq $Uri) {
        return 0
    }
    if ($Uri.Port -gt 0) {
        return $Uri.Port
    }
    if ($Uri.Scheme -eq "https") {
        return 443
    }
    return 80
}

function Test-ListeningPort {
    param(
        [string]$Name,
        [int]$Port
    )

    if ($Port -le 0) {
        Warn $Name "could not determine port"
        return
    }

    try {
        $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
        if (-not $listeners) {
            Fail $Name "nothing is listening on localhost port $Port"
            return
        }
        $processes = @()
        foreach ($listener in $listeners) {
            $process = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
            $cim = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)" -ErrorAction SilentlyContinue
            $commandLine = ""
            if ($cim -and $cim.CommandLine) {
                $commandLine = " cmd=$($cim.CommandLine)"
            }
            if ($process) {
                $processes += "$($process.ProcessName):$($process.Id)$commandLine"
            } else {
                $processes += "pid:$($listener.OwningProcess)$commandLine"
            }
        }
        $uniqueProcesses = $processes | Sort-Object -Unique
        Pass $Name "port=$Port, listeners=$($uniqueProcesses -join ', ')"
    } catch {
        Warn $Name "could not inspect listening port $Port`: $($_.Exception.Message)"
    }
}

function Test-CloudflaredConfigIngress {
    param(
        [string]$Path,
        [string]$ApiHost,
        [int]$ApiPort,
        [string]$McpHost,
        [int]$McpPort
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }

    try {
        $raw = Get-Content -LiteralPath $Path -Raw
        $lines = @(Get-Content -LiteralPath $Path)
    } catch {
        Warn "cloudflared ingress" "could not read $Path`: $($_.Exception.Message)"
        return
    }

    $missing = @()
    if ($ApiHost -and -not $raw.Contains($ApiHost)) {
        $missing += "hostname $ApiHost"
    }
    if ($McpHost -and -not $raw.Contains($McpHost)) {
        $missing += "hostname $McpHost"
    }
    if ($ApiPort -gt 0 -and $raw -notmatch "https?://(localhost|127\.0\.0\.1):$ApiPort\b") {
        $missing += "loopback API service port $ApiPort"
    }
    if ($McpPort -gt 0 -and $raw -notmatch "https?://(localhost|127\.0\.0\.1):$McpPort\b") {
        $missing += "loopback MCP service port $McpPort"
    }

    function Select-IngressMatchLines {
        param(
            [string[]]$ConfigLines,
            [string]$Host,
            [int]$Port
        )

        $matches = @()
        if ($Host) {
            $matches += @($ConfigLines | Where-Object { $_ -match [regex]::Escape($Host) })
        }
        if ($Port -gt 0) {
            $matches += @($ConfigLines | Where-Object { $_ -match "https?://(localhost|127\.0\.0\.1):$Port\b" })
        }
        return @($matches | Select-Object -Unique)
    }

    $apiMatches = @(Select-IngressMatchLines -ConfigLines $lines -Host $ApiHost -Port $ApiPort)
    $mcpMatches = @(Select-IngressMatchLines -ConfigLines $lines -Host $McpHost -Port $McpPort)
    $hostLines = @($lines | Where-Object { $_ -match "^\s*hostname\s*:" })
    $serviceLines = @($lines | Where-Object { $_ -match "^\s*service\s*:" })

    if ($ApiHost) {
        $apiHostCount = @($lines | Where-Object { $_ -match [regex]::Escape($ApiHost) }).Count
        if ($apiHostCount -gt 1) {
            Warn "cloudflared ingress" "hostname $ApiHost appears $apiHostCount times; check for duplicate ingress rules"
        }
    }
    if ($McpHost) {
        $mcpHostCount = @($lines | Where-Object { $_ -match [regex]::Escape($McpHost) }).Count
        if ($mcpHostCount -gt 1) {
            Warn "cloudflared ingress" "hostname $McpHost appears $mcpHostCount times; check for duplicate ingress rules"
        }
    }

    if ($missing.Count -gt 0) {
        Fail "cloudflared ingress" "config $Path is missing: $($missing -join ', '); host_lines=$($hostLines -join ' | '); service_lines=$($serviceLines -join ' | ')"
    } else {
        Pass "cloudflared ingress" "config maps $ApiHost->$ApiPort and $McpHost->$McpPort; api_lines=$($apiMatches -join ' | '); mcp_lines=$($mcpMatches -join ' | ')"
    }
}

function Show-LogTail {
    param(
        [string]$Name,
        [string]$Path,
        [int]$Lines = 12
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        Warn "$Name log" "missing $Path"
        return
    }
    Write-Host ""
    Write-Host "Recent $Name log ($Path):"
    Get-Content -LiteralPath $Path -Tail $Lines
}

function Format-TaskResult {
    param($Result)
    if ($null -eq $Result) {
        return "unknown"
    }
    return "$Result (0x$([Convert]::ToString([int64]$Result, 16)))"
}

function Test-ScheduledTaskState {
    param(
        [string]$TaskName,
        [switch]$RequireRunning
    )

    try {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
        $detailParts = @("state=$($task.State)")
        if ($info) {
            $detailParts += "last_run=$($info.LastRunTime)"
            $detailParts += "next_run=$($info.NextRunTime)"
            $detailParts += "last_result=$(Format-TaskResult $info.LastTaskResult)"
        }
        $detail = $detailParts -join ", "
        if ($RequireRunning -and $task.State -ne "Running") {
            Fail "scheduled task $TaskName" "$detail; expected long-running service task to be Running"
        } else {
            Pass "scheduled task $TaskName" $detail
        }
    } catch {
        Warn "scheduled task $TaskName" "not installed; run deploy\install-windows-tasks.ps1 on the host"
    }
}

function Format-Bytes {
    param([double]$Bytes)
    if ($Bytes -ge 1GB) {
        return "$([math]::Round($Bytes / 1GB, 2)) GB"
    }
    if ($Bytes -ge 1MB) {
        return "$([math]::Round($Bytes / 1MB, 2)) MB"
    }
    return "$([math]::Round($Bytes, 0)) bytes"
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

$repoDrive = Get-PSDrive -Name ((Get-Item -LiteralPath $RepoRoot).PSDrive.Name)
$freeGb = [math]::Round($repoDrive.Free / 1GB, 2)
if ($freeGb -lt $MinFreeDiskGb) {
    Warn "disk free" "drive=$($repoDrive.Name), free_gb=$freeGb, min_gb=$MinFreeDiskGb"
} else {
    Pass "disk free" "drive=$($repoDrive.Name), free_gb=$freeGb"
}

$libraryDir = Join-Path $RepoRoot "skills_library"
Test-PathPresent "skills library index" (Join-Path $libraryDir "index.json")
Test-SeedRuntime -DbPath $dbPath -LibraryDir $libraryDir

$cloudflaredExe = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
$cloudflaredConfig = Join-Path $env:USERPROFILE ".cloudflared\config.yml"
Test-PathPresent "cloudflared exe" $cloudflaredExe
Test-PathPresent "cloudflared config" $cloudflaredConfig

$baseUri = Get-UriValue $BaseUrl
$mcpUri = Get-UriValue $McpHealthUrl
$localApiUri = Get-UriValue $LocalApiUrl
$localMcpUri = Get-UriValue $LocalMcpHealthUrl
$apiPort = Get-EndpointPort $localApiUri
$mcpPort = Get-EndpointPort $localMcpUri
Test-CloudflaredConfigIngress `
    -Path $cloudflaredConfig `
    -ApiHost $(if ($baseUri) { $baseUri.Host } else { "" }) `
    -ApiPort $apiPort `
    -McpHost $(if ($mcpUri) { $mcpUri.Host } else { "" }) `
    -McpPort $mcpPort

$cloudflaredProcesses = Get-Process -Name cloudflared -ErrorAction SilentlyContinue
if ($cloudflaredProcesses) {
    $ids = ($cloudflaredProcesses | Select-Object -ExpandProperty Id) -join ", "
    Pass "cloudflared process" "running pid(s): $ids"
} else {
    Fail "cloudflared process" "not running; public Cloudflare Tunnel will return 1033/HTTP 530"
}

Test-ScheduledTaskState "$TaskPrefix-API" -RequireRunning
Test-ScheduledTaskState "$TaskPrefix-MCP" -RequireRunning
Test-ScheduledTaskState "$TaskPrefix-Tunnel" -RequireRunning
Test-ScheduledTaskState "$TaskPrefix-Backup"

Test-ListeningPort "local API listener" $apiPort
Test-ListeningPort "local MCP listener" $mcpPort

$backupRoot = Join-Path $RepoRoot "data\backups"
if (-not (Test-Path -LiteralPath $backupRoot)) {
    Warn "backup freshness" "backup directory missing: $backupRoot"
} else {
    $backupFiles = @(Get-ChildItem -LiteralPath $backupRoot -Recurse -File)
    $backupBytes = ($backupFiles | Measure-Object -Property Length -Sum).Sum
    $backupDirs = @(Get-ChildItem -LiteralPath $backupRoot -Directory | Where-Object { $_.Name -match "^\d{8}T\d{6}Z$" })
    Pass "backup footprint" "sets=$($backupDirs.Count), files=$($backupFiles.Count), bytes=$(Format-Bytes $backupBytes)"
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
Test-LocalRouteDirect
Test-LocalRouteTrap
Test-LocalRouteMetrics
Test-JsonEndpoint "local MCP healthz" $LocalMcpHealthUrl -RequireOk
Test-JsonEndpoint "public API healthz" "$($BaseUrl.TrimEnd('/'))/healthz" -RequireOk -RequireService
Test-JsonEndpoint "public MCP healthz" $McpHealthUrl -RequireOk

Write-Host ""
if ($failures -gt 0) {
    Show-LogTail "API/scraper" (Join-Path $RepoRoot "scraper.log")
    Show-LogTail "MCP" (Join-Path $RepoRoot "connector_http.log")
    Show-LogTail "cloudflared" (Join-Path $RepoRoot "cloudflared_tunnel.log")
    Write-Host ""
    Write-Host "diagnose-host: $failures failure(s), $warnings warning(s)"
    exit 1
}
Write-Host "diagnose-host: passed with $warnings warning(s)"
