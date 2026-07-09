param(
    [Parameter(Mandatory = $true)]
    [string]$DbPath,

    [Parameter(Mandatory = $true)]
    [string]$LibraryDir,

    [Parameter(Mandatory = $false)]
    [string]$OutputDir = "",

    [Parameter(Mandatory = $false)]
    [int]$RetentionDays = 0,

    [switch]$PackContentBlobs,
    [switch]$NoZip
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")

if (-not $OutputDir) {
    $OutputDir = Join-Path $root "data\seed-packets"
}

function Invoke-Native {
    param(
        [string]$Label,
        [scriptblock]$Command
    )
    Write-Host "==> $Label"
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

if (-not (Test-Path -LiteralPath $DbPath)) {
    throw "Database not found: $DbPath"
}
if (-not (Test-Path -LiteralPath $LibraryDir)) {
    throw "skills_library not found: $LibraryDir"
}

$before = @()
if (Test-Path -LiteralPath $OutputDir) {
    $before = @(Get-ChildItem -LiteralPath $OutputDir -Directory | ForEach-Object { $_.FullName })
}

$backupArgs = @(
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    (Join-Path $PSScriptRoot "backup-local.ps1"),
    "-DbPath",
    $DbPath,
    "-LibraryDir",
    $LibraryDir,
    "-OutputDir",
    $OutputDir,
    "-RetentionDays",
    "$RetentionDays"
)
if ($PackContentBlobs) {
    $backupArgs += "-PackContentBlobs"
}

Invoke-Native "create seed backup" {
    powershell @backupArgs
}

$after = @(Get-ChildItem -LiteralPath $OutputDir -Directory | Sort-Object LastWriteTimeUtc -Descending)
$backupDir = $after | Where-Object { $before -notcontains $_.FullName } | Select-Object -First 1
if (-not $backupDir) {
    $backupDir = $after | Select-Object -First 1
}
if (-not $backupDir) {
    throw "Could not locate seed packet under $OutputDir"
}

Invoke-Native "verify backup manifest" {
    python (Join-Path $root "deploy\verify_backup.py") $backupDir.FullName
}

Invoke-Native "validate launch seed candidate" {
    python (Join-Path $root "launch_readiness.py") --skip-live --skip-local-seed --skip-git --seed-backup-dir $backupDir.FullName
}

if (-not $NoZip) {
    $zipPath = "$($backupDir.FullName).zip"
    if (Test-Path -LiteralPath $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force
    }
    Compress-Archive -Path (Join-Path $backupDir.FullName "*") -DestinationPath $zipPath
    Invoke-Native "validate seed packet zip" {
        python (Join-Path $root "launch_readiness.py") --skip-live --skip-local-seed --skip-git --seed-backup-zip $zipPath
    }
    Write-Host "Seed packet zip: $zipPath"
}

Write-Host "Seed packet directory: $($backupDir.FullName)"
