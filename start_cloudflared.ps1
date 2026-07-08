$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot

$logPath = Join-Path $PSScriptRoot "cloudflared_tunnel.log"

function Write-Log {
    param([string]$Message)
    Add-Content -Path $logPath -Value "$(Get-Date -Format o) [start_cloudflared] $Message"
}

# Runs the named "auto-skill" tunnel that exposes skills.avalahome.com ->
# localhost:8000 and mcp.avalahome.com -> localhost:8765 (see
# ~/.cloudflared/config.yml ingress rules). Restart loop mirrors
# start_scraper.ps1/start_connector_http.ps1 so all three services survive
# a crash the same way.
$cloudflared = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
$configPath = Join-Path $env:USERPROFILE ".cloudflared\config.yml"

Write-Log "supervisor starting in $PSScriptRoot"
Write-Log "cloudflared=$cloudflared; config=$configPath"

while ($true) {
    if (-not (Test-Path -LiteralPath $cloudflared)) {
        Write-Log "cloudflared executable missing: $cloudflared"
        Start-Sleep -Seconds 15
        continue
    }
    if (-not (Test-Path -LiteralPath $configPath)) {
        Write-Log "cloudflared config missing: $configPath"
        Start-Sleep -Seconds 15
        continue
    }

    Write-Log "starting tunnel auto-skill"
    & $cloudflared tunnel run auto-skill *>> $logPath
    Write-Log "process exited with code $LASTEXITCODE, restarting in 5s"
    Start-Sleep -Seconds 5
}
