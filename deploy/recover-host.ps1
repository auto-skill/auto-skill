param(
    [string]$Branch = "main",
    [string]$ConnectorBranch = "master",
    [string]$ConnectorDir = "",
    [string]$BaseUrl = "https://skills.avalahome.com",
    [string]$McpHealthUrl = "https://mcp.avalahome.com/healthz",
    [string]$LocalApiUrl = "http://127.0.0.1:8000",
    [string]$LocalMcpHealthUrl = "http://127.0.0.1:8765/healthz",
    [string]$TaskPrefix = "AutoSkill",
    [int]$WaitSeconds = 45,
    [switch]$AllowDirty,
    [switch]$SkipPull,
    [switch]$SkipInstall,
    [switch]$SkipTests,
    [switch]$SkipTaskInstall,
    [switch]$SkipBackupTask,
    [switch]$SkipLaunchCheck,
    [switch]$SkipConnectorPull,
    [switch]$StopStalePortOwners
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location -Path $RepoRoot

function Invoke-Step {
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

function Test-TaskMissing {
    param([string]$Name)
    return $null -eq (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue)
}

function Get-UriValue {
    param([string]$Url)
    try {
        return [Uri]$Url
    } catch {
        throw "Could not parse URL '$Url': $($_.Exception.Message)"
    }
}

function Get-EndpointPort {
    param($Uri)
    if ($Uri.Port -gt 0) {
        return $Uri.Port
    }
    if ($Uri.Scheme -eq "https") {
        return 443
    }
    return 80
}

function Stop-ScopedPortOwners {
    param(
        [string]$Name,
        [int]$Port,
        [string[]]$AllowedCommandPatterns
    )

    $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if (-not $listeners) {
        Write-Host "[PASS] $Name`: no existing listener on port $Port"
        return
    }

    foreach ($listener in $listeners) {
        $pidValue = [int]$listener.OwningProcess
        $process = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
        $cim = Get-CimInstance Win32_Process -Filter "ProcessId = $pidValue" -ErrorAction SilentlyContinue
        $commandLine = if ($cim -and $cim.CommandLine) { [string]$cim.CommandLine } else { "" }
        $identity = "$(if ($process) { $process.ProcessName } else { 'pid' }):$pidValue $commandLine"

        $matched = $false
        foreach ($pattern in $AllowedCommandPatterns) {
            if ($identity -match $pattern) {
                $matched = $true
                break
            }
        }
        if (-not $matched) {
            Write-Host "[WARN] $Name`: not stopping non-Auto-Skill listener on port ${Port}: $identity"
            continue
        }

        Write-Host "[WARN] $Name`: stopping stale Auto-Skill listener on port ${Port}: $identity"
        Stop-Process -Id $pidValue -Force -ErrorAction Stop
    }
}

function Find-ConnectorDir {
    $candidates = @()
    if ($ConnectorDir) {
        $candidates += $ConnectorDir
    }
    if ($env:AUTO_SKILL_CONNECTOR_DIR) {
        $candidates += $env:AUTO_SKILL_CONNECTOR_DIR
    }
    $candidates += @(
        (Join-Path $RepoRoot "..\auto-skill-connector"),
        (Join-Path $RepoRoot "..\..\Skills"),
        (Join-Path ([Environment]::GetFolderPath("MyDocuments")) "Skills")
    )

    foreach ($candidate in $candidates) {
        if (-not $candidate) { continue }
        $serverPath = Join-Path $candidate "mcp_server.py"
        if (Test-Path -LiteralPath $serverPath) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    throw "Connector checkout not found. Set -ConnectorDir or AUTO_SKILL_CONNECTOR_DIR."
}

function Update-ConnectorCheckout {
    param([string]$Path)

    Push-Location -LiteralPath $Path
    try {
        $branch = (& git rev-parse --abbrev-ref HEAD).Trim()
        if ($LASTEXITCODE -ne 0) {
            throw "Could not determine connector git branch in $Path"
        }
        if ($branch -ne $ConnectorBranch) {
            throw "Connector checkout is on '$branch', expected '$ConnectorBranch'."
        }
        $dirty = (& git status --porcelain)
        if ($dirty) {
            if (-not $AllowDirty) {
                throw "Connector checkout has uncommitted changes. Commit/stash them, or pass -AllowDirty after reviewing them."
            }
            Write-Host "Connector dirty check skipped because -AllowDirty was passed."
        }
        Invoke-Step "git fetch connector $ConnectorBranch" { git fetch origin $ConnectorBranch }
        Invoke-Step "git pull connector --ff-only origin $ConnectorBranch" { git pull --ff-only origin $ConnectorBranch }
    } finally {
        Pop-Location
    }
}

function Wait-JsonOk {
    param(
        [string]$Name,
        [string]$Url,
        [int]$TimeoutSeconds,
        [switch]$RequireOk,
        [switch]$RequireService
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastError = ""
    while ((Get-Date) -lt $deadline) {
        try {
            $body = Invoke-RestMethod -Uri $Url -Headers @{ Accept = "application/json" } -TimeoutSec 5
            if ($RequireOk -and $body.ok -ne $true) {
                $lastError = "response ok was not true: $($body | ConvertTo-Json -Depth 6 -Compress)"
            } elseif ($RequireService -and $body.service -ne "auto-skill-api") {
                $lastError = "expected service=auto-skill-api: $($body | ConvertTo-Json -Depth 6 -Compress)"
            } else {
                Write-Host "[PASS] $Name`: $($body | ConvertTo-Json -Depth 6 -Compress)"
                return
            }
        } catch {
            $lastError = $_.Exception.Message
        }
        Start-Sleep -Seconds 2
    }

    throw "$Name did not become healthy within $TimeoutSeconds second(s): $lastError"
}

Write-Host "Auto-Skill host recovery"
Write-Host "Repo: $RepoRoot"
Write-Host "Branch: $Branch"
Write-Host "Connector branch: $ConnectorBranch"
Write-Host "Public URL: $BaseUrl"
Write-Host "MCP health URL: $McpHealthUrl"

try {
    $apiPort = Get-EndpointPort (Get-UriValue $LocalApiUrl)
    $mcpPort = Get-EndpointPort (Get-UriValue $LocalMcpHealthUrl)

    $serviceTasks = @("$TaskPrefix-API", "$TaskPrefix-MCP", "$TaskPrefix-Tunnel")
    $missingTasks = @($serviceTasks | Where-Object { Test-TaskMissing $_ })
    if ($missingTasks.Count -gt 0) {
        if ($SkipTaskInstall) {
            throw "Missing scheduled tasks: $($missingTasks -join ', '). Rerun without -SkipTaskInstall."
        }

        Invoke-Step "install/start scheduled tasks" {
            $args = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ".\deploy\install-windows-tasks.ps1", "-StartNow")
            if ($SkipBackupTask) {
                $args += "-SkipBackupTask"
            }
            powershell @args
        }
    } else {
        Write-Host "[PASS] scheduled tasks installed: $($serviceTasks -join ', ')"
    }

    $resolvedConnectorDir = Find-ConnectorDir
    Write-Host "[PASS] connector checkout: $resolvedConnectorDir"
    if (-not $SkipConnectorPull -and -not $SkipPull) {
        Update-ConnectorCheckout $resolvedConnectorDir
    } elseif ($SkipConnectorPull) {
        Write-Host "Skipping connector pull because -SkipConnectorPull was passed."
    } else {
        Write-Host "Skipping connector pull because -SkipPull was passed."
    }

    if ($StopStalePortOwners) {
        Invoke-Step "stop scoped stale local listeners" {
            Stop-ScopedPortOwners "local API" $apiPort @("scraper\.py", "start_scraper\.ps1")
            Stop-ScopedPortOwners "local MCP" $mcpPort @("mcp_server\.py", "start_connector_http\.ps1")
        }
    }

    $updateArgs = @(
        "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", ".\deploy\update-host.ps1",
        "-Branch", $Branch,
        "-BaseUrl", $BaseUrl,
        "-McpHealthUrl", $McpHealthUrl,
        "-TaskPrefix", $TaskPrefix,
        "-RestartTasks",
        "-RestartWaitSeconds", "5",
        "-SkipLaunchCheck"
    )
    if ($AllowDirty) { $updateArgs += "-AllowDirty" }
    if ($SkipPull) { $updateArgs += "-SkipPull" }
    if ($SkipInstall) { $updateArgs += "-SkipInstall" }
    if ($SkipTests) { $updateArgs += "-SkipTests" }

    Invoke-Step "update host and restart service tasks" {
        powershell @updateArgs
    }

    Invoke-Step "wait for local API health" {
        Wait-JsonOk "local API healthz" "$($LocalApiUrl.TrimEnd('/'))/healthz" $WaitSeconds -RequireOk -RequireService
    }
    Invoke-Step "wait for local API readiness" {
        Wait-JsonOk "local API readyz" "$($LocalApiUrl.TrimEnd('/'))/readyz" $WaitSeconds -RequireOk
    }
    Invoke-Step "wait for local MCP health" {
        Wait-JsonOk "local MCP healthz" $LocalMcpHealthUrl $WaitSeconds -RequireOk
    }

    if (-not $SkipLaunchCheck) {
        Invoke-Step "public launch check" {
            python launch_check.py --base-url $BaseUrl --mcp-health-url $McpHealthUrl --skip-env --skip-docker
        }
    }

    Write-Host ""
    Write-Host "Host recovery completed."
} catch {
    Write-Host ""
    Write-Host "[FAIL] host recovery failed: $($_.Exception.Message)"
    Write-Host ""
    Write-Host "Running diagnose-host.ps1 for the failure packet..."
    powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\diagnose-host.ps1 `
        -BaseUrl $BaseUrl `
        -McpHealthUrl $McpHealthUrl `
        -LocalApiUrl $LocalApiUrl `
        -LocalMcpHealthUrl $LocalMcpHealthUrl `
        -TaskPrefix $TaskPrefix
    exit 1
}
