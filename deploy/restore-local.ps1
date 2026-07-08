param(
    [Parameter(Mandatory = $true)]
    [string]$DbPath,

    [Parameter(Mandatory = $false)]
    [string]$LibraryArchive = "",

    [Parameter(Mandatory = $false)]
    [string]$TargetDataDir = "",

    [Parameter(Mandatory = $false)]
    [string]$TargetLibraryDir = ""
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
if (-not $TargetDataDir) {
    $TargetDataDir = Join-Path $root "data"
}
if (-not $TargetLibraryDir) {
    $TargetLibraryDir = Join-Path $root "skills_library"
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
