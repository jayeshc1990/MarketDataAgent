# Registers a Windows Task Scheduler job that starts MarketDataAgent at 09:05 every Monday-Friday.
# The agent itself waits for 09:15 IST, collects until 15:40, then exits.
# Run once:   powershell -ExecutionPolicy Bypass -File install_task.ps1
# Remove:     Unregister-ScheduledTask -TaskName MarketDataAgent -Confirm:$false

$TaskName = "MarketDataAgent"
$Bat = Join-Path $PSScriptRoot "run_agent.bat"

$action   = New-ScheduledTaskAction -Execute $Bat -WorkingDirectory $PSScriptRoot
$trigger  = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 9:05am
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -DontStopIfGoingOnBatteries `
            -AllowStartIfOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 8)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Indian F&O market data collector (09:15-15:40 IST)" -Force | Out-Null
Write-Host "Scheduled task '$TaskName' registered (Mon-Fri 09:05)."
