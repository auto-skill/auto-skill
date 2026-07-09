param(
    [Parameter(Mandatory = $false)]
    [string]$DbPath = "",

    [Parameter(Mandatory = $false)]
    [string]$LibraryDir = "",

    [Parameter(Mandatory = $false)]
    [string]$OutputDir = "",

    [Parameter(Mandatory = $false)]
    [string]$R2Endpoint = $env:R2_ENDPOINT,

    [Parameter(Mandatory = $false)]
    [string]$R2Bucket = $env:R2_BUCKET,

    [Parameter(Mandatory = $false)]
    [string]$R2Prefix = "alpha-host-backups",

    [Parameter(Mandatory = $false)]
    [int]$RetentionDays = 14,

    [switch]$SkipLibrary,
    [switch]$PackContentBlobs,
    [switch]$UploadR2
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")

if (-not $DbPath) {
    $dataDb = Join-Path $root "data\local_skills.db"
    $rootDb = Join-Path $root "local_skills.db"
    if (Test-Path -LiteralPath $dataDb) {
        $DbPath = $dataDb
    } else {
        $DbPath = $rootDb
    }
}
if (-not $LibraryDir) {
    $LibraryDir = Join-Path $root "skills_library"
}
if (-not $OutputDir) {
    $OutputDir = Join-Path $root "data\backups"
}

$backupDir = Join-Path $OutputDir $stamp
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
$backupDir = (Resolve-Path -LiteralPath $backupDir).Path

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

function Remove-ExpiredBackups {
    param(
        [string]$RootDir,
        [string]$CurrentBackupDir,
        [int]$Days
    )

    if ($Days -le 0) {
        Write-Host "Backup retention pruning disabled."
        return
    }
    if (-not (Test-Path -LiteralPath $RootDir)) {
        return
    }

    $rootItem = Get-Item -LiteralPath $RootDir
    $currentItem = Get-Item -LiteralPath $CurrentBackupDir
    $cutoff = (Get-Date).ToUniversalTime().AddDays(-$Days)
    $timestampPattern = "^\d{8}T\d{6}Z$"

    $expired = @()
    foreach ($dir in Get-ChildItem -LiteralPath $rootItem.FullName -Directory) {
        if ($dir.Name -notmatch $timestampPattern) {
            continue
        }
        if ($dir.FullName -eq $currentItem.FullName) {
            continue
        }
        if ($dir.Parent.FullName -ne $rootItem.FullName) {
            throw "Refusing to prune backup outside root: $($dir.FullName)"
        }
        if ($dir.LastWriteTimeUtc -lt $cutoff) {
            $expired += $dir
        }
    }

    foreach ($dir in $expired) {
        Write-Host "Pruning expired backup $($dir.FullName)"
        Remove-Item -LiteralPath $dir.FullName -Recurse -Force
    }
    Write-Host "Backup retention: kept backups from the last $Days day(s); pruned $($expired.Count)."
}

function Get-BackupRelativePath {
    param([string]$FullName)

    $marker = "$stamp\"
    $index = $FullName.LastIndexOf($marker, [System.StringComparison]::OrdinalIgnoreCase)
    if ($index -lt 0) {
        throw "Could not derive backup-relative path for $FullName"
    }
    return $FullName.Substring($index + $marker.Length).Replace("\", "/")
}

if (-not (Test-Path -LiteralPath $DbPath)) {
    throw "Database not found: $DbPath"
}

$dbBackup = Join-Path $backupDir "local_skills.db"
$dbSource = (Resolve-Path -LiteralPath $DbPath).Path
$dbDest = $dbBackup
Invoke-Native "SQLite online backup" {
    python -c "import sqlite3, sys; src, dst = sys.argv[1], sys.argv[2]; source = sqlite3.connect(src); dest = sqlite3.connect(dst); source.backup(dest); dest.close(); source.close()" $dbSource $dbDest
}

$libraryArchive = ""
if (-not $SkipLibrary) {
    if (Test-Path -LiteralPath $LibraryDir) {
        $libraryArchive = Join-Path $backupDir "skills_library.tgz"
        $resolvedLibrary = (Resolve-Path -LiteralPath $LibraryDir).Path
        Invoke-Native "skills_library archive" {
            tar -C $resolvedLibrary -czf $libraryArchive .
        }
    } else {
        Write-Warning "skills_library not found: $LibraryDir"
    }
}

if ($PackContentBlobs) {
    $blobDir = Join-Path $backupDir "content_blobs"
    Invoke-Native "content blob pack" {
        python (Join-Path $root "pack_content_blobs.py") --library-dir $LibraryDir --output-dir $blobDir
    }
}

$files = @()
foreach ($item in Get-ChildItem -LiteralPath $backupDir -Recurse -File) {
    $hash = Get-FileHash -Algorithm SHA256 -LiteralPath $item.FullName
    $files += [ordered]@{
        path = Get-BackupRelativePath $item.FullName
        bytes = $item.Length
        sha256 = $hash.Hash.ToLowerInvariant()
    }
}

$manifest = [ordered]@{
    created_at = $stamp
    source_root = "$root"
    db_source = "$dbSource"
    db_backup = "local_skills.db"
    library_source = "$LibraryDir"
    library_archive = $(if ($libraryArchive) { "skills_library.tgz" } else { $null })
    content_blobs_packed = [bool]$PackContentBlobs
    files = $files
}
$manifestPath = Join-Path $backupDir "manifest.json"
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $manifestPath -Encoding UTF8

if ($UploadR2) {
    if (-not $R2Endpoint -or -not $R2Bucket) {
        throw "R2Endpoint and R2Bucket are required with -UploadR2"
    }
    if (-not $env:AWS_ACCESS_KEY_ID -and $env:R2_ACCESS_KEY_ID) {
        $env:AWS_ACCESS_KEY_ID = $env:R2_ACCESS_KEY_ID
    }
    if (-not $env:AWS_SECRET_ACCESS_KEY -and $env:R2_SECRET_ACCESS_KEY) {
        $env:AWS_SECRET_ACCESS_KEY = $env:R2_SECRET_ACCESS_KEY
    }
    Invoke-Native "R2 backup sync" {
        aws --endpoint-url $R2Endpoint s3 sync $backupDir "s3://$R2Bucket/$R2Prefix/$stamp/"
    }
}

Remove-ExpiredBackups -RootDir $OutputDir -CurrentBackupDir $backupDir -Days $RetentionDays

Write-Host "Backup written to $backupDir"
Write-Host "Manifest: $manifestPath"
