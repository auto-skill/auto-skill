param(
    [string]$Branch = "main",
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
    [switch]$SkipLaunchCheck
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
Write-Host "Public URL: $BaseUrl"
Write-Host "MCP health URL: $McpHealthUrl"

try {
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
