@echo off
powershell -NoProfile -Command "Unregister-ScheduledTask -TaskName 'TrendScanner_KR' -Confirm:$false -ErrorAction SilentlyContinue; Unregister-ScheduledTask -TaskName 'TrendScanner_US' -Confirm:$false -ErrorAction SilentlyContinue; Write-Host ' Auto scan removed (TrendScanner_KR, TrendScanner_US).'"
pause
