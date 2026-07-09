param(
    [Parameter(Mandatory = $false)]
    [string]$BackupDir = "",

    [Parameter(Mandatory = $false)]
    [string]$DbPath,

    [Parameter(Mandatory = $false)]
    [string]$LibraryArchive = "",

    [Parameter(Mandatory = $false)]
    [string]$TargetDataDir = "",

    [Parameter(Mandatory = $false)]
    [string]$TargetLibraryDir = "",

    [switch]$SkipVerify
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
if (-not $TargetDataDir) {
    $TargetDataDir = Join-Path $root "data"
}
if (-not $TargetLibraryDir) {
    $TargetLibraryDir = Join-Path $root "skills_library"
}

if ($BackupDir) {
    $resolvedBackupDir = (Resolve-Path -LiteralPath $BackupDir).Path
    $manifestPath = Join-Path $resolvedBackupDir "manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath)) {
        throw "Backup manifest not found: $manifestPath"
    }
    if (-not $SkipVerify) {
        python (Join-Path $root "deploy\verify_backup.py") $resolvedBackupDir
        if ($LASTEXITCODE -ne 0) {
            throw "Backup verification failed for $resolvedBackupDir"
        }
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    $dbBackupName = if ($manifest.db_backup) { [string]$manifest.db_backup } else { "local_skills.db" }
    $DbPath = Join-Path $resolvedBackupDir $dbBackupName
    if ($manifest.library_archive) {
        $LibraryArchive = Join-Path $resolvedBackupDir ([string]$manifest.library_archive)
    }
}

if (-not $DbPath) {
    throw "DbPath is required unless -BackupDir is provided."
}
if (-not (Test-Path -LiteralPath $DbPath)) {
    throw "Database backup not found: $DbPath"
}
if ($LibraryArchive -and -not (Test-Path -LiteralPath $LibraryArchive)) {
    throw "Library archive not found: $LibraryArchive"
}

New-Item -ItemType Directory -Force -Path $TargetDataDir | Out-Null
Copy-Item -LiteralPath $DbPath -Destination (Join-Path $TargetDataDir "local_skills.db") -Force

if ($LibraryArchive) {
    if (Test-Path -LiteralPath $TargetLibraryDir) {
        $stamp = Get-Date -Format "yyyyMMddTHHmmssZ"
        Rename-Item -LiteralPath $TargetLibraryDir -NewName "skills_library.before_restore.$stamp"
    }
    New-Item -ItemType Directory -Force -Path $TargetLibraryDir | Out-Null
    tar -xzf $LibraryArchive -C $TargetLibraryDir
}

Write-Host "Restored DB to $TargetDataDir\local_skills.db"
if ($LibraryArchive) {
    Write-Host "Restored library to $TargetLibraryDir"
}
