$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot

$logPath = Join-Path $PSScriptRoot "connector_http.log"
$env:PYTHONUNBUFFERED = "1"

function Write-Log {
    param([string]$Message)
    Add-Content -Path $logPath -Value "$(Get-Date -Format o) [start_connector_http] $Message"
}

Write-Log "supervisor starting in $PSScriptRoot"

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
Write-Log "MCP_TRANSPORT=$env:MCP_TRANSPORT; MCP_PORT=$env:MCP_PORT; AUTOSKILL_URL=$env:AUTOSKILL_URL; MCP_ALLOWED_HOSTS=$env:MCP_ALLOWED_HOSTS"

$connectorCandidates = @()
if ($env:AUTO_SKILL_CONNECTOR_DIR) {
    $connectorCandidates += $env:AUTO_SKILL_CONNECTOR_DIR
}
$connectorCandidates += @(
    (Join-Path $PSScriptRoot "..\auto-skill-connector"),
    (Join-Path $PSScriptRoot "..\..\Skills"),
    (Join-Path ([Environment]::GetFolderPath("MyDocuments")) "Skills")
)

$connectorDir = $null
foreach ($candidate in $connectorCandidates) {
    if (-not $candidate) { continue }
    $serverPath = Join-Path $candidate "mcp_server.py"
    if (Test-Path -LiteralPath $serverPath) {
        $connectorDir = (Resolve-Path -LiteralPath $candidate).Path
        break
    }
}

if (-not $connectorDir) {
    Write-Log "connector checkout not found. Set AUTO_SKILL_CONNECTOR_DIR to the auto-skill-connector repo. Checked: $($connectorCandidates -join '; ')"
    throw "Connector checkout not found. Set AUTO_SKILL_CONNECTOR_DIR to the auto-skill-connector repo."
}
Write-Log "using connector checkout $connectorDir"

while ($true) {
    Write-Log "starting python mcp_server.py"
    python "$connectorDir\mcp_server.py" *>> $logPath
    Write-Log "process exited with code $LASTEXITCODE, restarting in 5s"
    Start-Sleep -Seconds 5
}
