param([string]$Python, [string]$Script)
$ErrorActionPreference = 'Stop'
$dir = Split-Path -Parent $Script
# StartWhenAvailable: if the PC was off or asleep at the scheduled time, run as soon as it is back on
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1)

$krAction  = New-ScheduledTaskAction -Execute $Python -Argument ('"' + $Script + '" kr') -WorkingDirectory $dir
$krTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 17:50
Register-ScheduledTask -TaskName 'TrendScanner_KR' -Action $krAction -Trigger $krTrigger -Settings $settings -Description 'Minervini trend template scan - Korea (after KRX close)' -Force | Out-Null

$usAction  = New-ScheduledTaskAction -Execute $Python -Argument ('"' + $Script + '" us') -WorkingDirectory $dir
$usTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Tuesday,Wednesday,Thursday,Friday,Saturday -At 07:20
Register-ScheduledTask -TaskName 'TrendScanner_US' -Action $usAction -Trigger $usTrigger -Settings $settings -Description 'Minervini trend template scan - US (after NYSE close)' -Force | Out-Null

Write-Host '       TrendScanner_KR  Mon-Fri 17:50'
Write-Host '       TrendScanner_US  Tue-Sat 07:20'
