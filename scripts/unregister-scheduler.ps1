# Removes the hourly tourfinder collector from Windows Task Scheduler.
schtasks /Delete /TN "tourfinder-collect" /F
Write-Host "Removed 'tourfinder-collect'."
