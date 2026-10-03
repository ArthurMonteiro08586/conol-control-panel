$action = New-ScheduledTaskAction -Execute "C:\Users\User\AppData\Local\hermes\hermes-agent-0.18.2-latest\venv\Scripts\python.exe" -Argument "C:\Users\User\Desktop\conol_autoreg\tunnel.py"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
Register-ScheduledTask -TaskName "ConolTunnel" -Action $action -Trigger $trigger -Settings $settings -Force
Start-ScheduledTask -TaskName "ConolTunnel"
