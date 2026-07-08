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
            if ($process) {
                $processes += "$($process.ProcessName):$($process.Id)"
            } else {
                $processes += "pid:$($listener.OwningProcess)"
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

    if ($missing.Count -gt 0) {
        Fail "cloudflared ingress" "config $Path is missing: $($missing -join ', ')"
    } else {
        Pass "cloudflared ingress" "config maps $ApiHost->$ApiPort and $McpHost->$McpPort"
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

Test-PathPresent "skills library index" (Join-Path $RepoRoot "skills_library\index.json")

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
