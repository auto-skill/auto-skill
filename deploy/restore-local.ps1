param(
    [Parameter(Mandatory = $true)]
    [string]$DbPath,

    [Parameter(Mandatory = $false)]
    [string]$LibraryArchive = ""
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$dataDir = Join-Path $root "data"
$libraryDir = Join-Path $root "skills_library"

New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
Copy-Item -LiteralPath $DbPath -Destination (Join-Path $dataDir "local_skills.db") -Force

if ($LibraryArchive) {
    if (Test-Path -LiteralPath $libraryDir) {
        $stamp = Get-Date -Format "yyyyMMddTHHmmssZ"
        Rename-Item -LiteralPath $libraryDir -NewName "skills_library.before_restore.$stamp"
    }
    New-Item -ItemType Directory -Force -Path $libraryDir | Out-Null
    tar -xzf $LibraryArchive -C $libraryDir
}

Write-Host "Restored DB to $dataDir\local_skills.db"
if ($LibraryArchive) {
    Write-Host "Restored library to $libraryDir"
}
