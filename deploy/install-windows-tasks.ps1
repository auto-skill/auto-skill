param(
    [string]$TaskPrefix = "AutoSkill",
    [switch]$StartNow,
    [switch]$Unregister,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")

$Tasks = @(
    @{
        Name = "$TaskPrefix-API"
        Script = Join-Path $RepoRoot "start_scraper.ps1"
        Description = "Auto-Skill API and scraper restart loop"
    },
    @{
        Name = "$TaskPrefix-MCP"
        Script = Join-Path $RepoRoot "start_connector_http.ps1"
        Description = "Auto-Skill connector MCP HTTP restart loop"
    },
    @{
        Name = "$TaskPrefix-Tunnel"
        Script = Join-Path $RepoRoot "start_cloudflared.ps1"
        Description = "Auto-Skill Cloudflare Tunnel restart loop"
    }
)

function Write-Step {
    param([string]$Message)
    Write-Host "==> $Message"
}

function Assert-ScriptExists {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Required script is missing: $Path"
    }
}

function Remove-Task {
    param([string]$Name)
    $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if (-not $existing) {
        Write-Host "Task not present: $Name"
        return
    }
    if ($DryRun) {
        Write-Host "[dry-run] unregister task $Name"
        return
    }
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    Write-Host "Unregistered task $Name"
}

if ($Unregister) {
    foreach ($task in $Tasks) {
        Remove-Task $task.Name
    }
    exit 0
}

$powerShellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
foreach ($task in $Tasks) {
    Assert-ScriptExists $task.Script
}

$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Days 999) `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

foreach ($task in $Tasks) {
    $scriptPath = [string]$task.Script
    $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`""
    $action = New-ScheduledTaskAction -Execute $powerShellExe -Argument $arguments -WorkingDirectory $RepoRoot
    $definition = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description $task.Description

    if ($DryRun) {
        Write-Step "[dry-run] register $($task.Name)"
        Write-Host "  Execute: $powerShellExe"
        Write-Host "  Arguments: $arguments"
        Write-Host "  WorkingDirectory: $RepoRoot"
        continue
    }

    Write-Step "register $($task.Name)"
    Register-ScheduledTask -TaskName $task.Name -InputObject $definition -Force | Out-Null
    if ($StartNow) {
        Start-ScheduledTask -TaskName $task.Name
        Write-Host "Started $($task.Name)"
    }
}

Write-Host ""
if ($DryRun) {
    Write-Host "Dry run complete. No scheduled tasks were changed."
} else {
    Write-Host "Installed Auto-Skill scheduled tasks for user $user."
}
Write-Host "Inspect with: Get-ScheduledTask -TaskName '$TaskPrefix-*'"
