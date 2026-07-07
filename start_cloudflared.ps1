$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot

# Runs the named "auto-skill" tunnel that exposes skills.avalahome.com ->
# localhost:8000 and mcp.avalahome.com -> localhost:8765 (see
# ~/.cloudflared/config.yml ingress rules). Restart loop mirrors
# start_scraper.ps1/start_connector_http.ps1 so all three services survive
# a crash the same way.
$cloudflared = "C:\Program Files (x86)\cloudflared\cloudflared.exe"

while ($true) {
    & $cloudflared tunnel run auto-skill *>> "$PSScriptRoot\cloudflared_tunnel.log"
    Add-Content -Path "$PSScriptRoot\cloudflared_tunnel.log" -Value "$(Get-Date -Format o) [start_cloudflared] process exited, restarting in 5s"
    Start-Sleep -Seconds 5
}
