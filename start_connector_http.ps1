$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot

$env:MCP_TRANSPORT = "streamable-http"
if (-not $env:MCP_PORT) { $env:MCP_PORT = "8765" }
# Exposed permanently at https://mcp.avalahome.com/mcp via the existing
# cloudflared "auto-skill" tunnel (see ~/.cloudflared/config.yml). Stable
# hostname, so Host-header DNS-rebinding protection is re-enabled here.
$env:MCP_ALLOWED_HOSTS = "mcp.avalahome.com,localhost:8765,127.0.0.1:8765"
# Skills server and connector run on the same box; the public
# skills.avalahome.com hostname doesn't resolve correctly on this machine's
# own LAN (split-horizon DNS -- see README's LAN self-hosting note), so talk
# to it directly over loopback instead.
if (-not $env:AUTOSKILL_URL) { $env:AUTOSKILL_URL = "http://localhost:8000" }

$connectorDir = "$PSScriptRoot\..\auto-skill-connector"

while ($true) {
    python "$connectorDir\mcp_server.py" *>> "$PSScriptRoot\connector_http.log"
    Add-Content -Path "$PSScriptRoot\connector_http.log" -Value "$(Get-Date -Format o) [start_connector_http] process exited, restarting in 5s"
    Start-Sleep -Seconds 5
}
