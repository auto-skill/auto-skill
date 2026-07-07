$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot
$log = "$PSScriptRoot\skillsmp_harvest.log"

# harvest_skillsmp.py checkpoints its shard queue after every shard, so blind
# restarts are safe; loop until it prints its DONE line.
for ($i = 1; $i -le 300; $i++) {
    Add-Content -Path $log -Value "=== harvest run $i $(Get-Date -Format o) ==="
    cmd /c "python -u harvest_skillsmp.py >> `"$log`" 2>&1"
    if (Select-String -Path $log -Pattern 'HARVEST DONE' -Quiet) {
        Add-Content -Path $log -Value "harvest finished cleanly"
        exit 0
    }
    Start-Sleep -Seconds 30
}
Add-Content -Path $log -Value "harvest gave up after 300 runs"
