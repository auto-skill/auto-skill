$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot
$env:SUPABASE_SERVICE_KEY = [Environment]::GetEnvironmentVariable('SUPABASE_SERVICE_KEY', 'User')
$log = "$PSScriptRoot\migrate.log"

# migrate_from_supabase.py checkpoints to migrate_state.json after every page,
# so blind restarts are safe; loop until it prints its DONE line.
for ($i = 1; $i -le 200; $i++) {
    Add-Content -Path $log -Value "=== migration run $i $(Get-Date -Format o) ==="
    cmd /c "python -u migrate_from_supabase.py >> `"$log`" 2>&1"
    if (Select-String -Path $log -Pattern 'MIGRATION DONE' -Quiet) {
        Add-Content -Path $log -Value "migration finished cleanly"
        exit 0
    }
    Start-Sleep -Seconds 20
}
Add-Content -Path $log -Value "migration gave up after 200 runs"
