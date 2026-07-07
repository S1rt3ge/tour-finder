# Registers the hourly tourfinder collector in Windows Task Scheduler.
# Run once from an elevated-or-normal PowerShell:  .\scripts\register-scheduler.ps1
# Removes with: .\scripts\unregister-scheduler.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$exe  = Join-Path $root ".venv\Scripts\pythonw.exe"
$db   = Join-Path $root "data\tourfinder.db"

if (-not (Test-Path $exe)) { throw "venv not found at $exe — create it first (see README)" }

# pythonw = no console window; collect logs to data\collect.log
$tr = "`"$exe`" -m tourfinder.cli --db `"$db`" collect"

# First automated run 60 min out, then every hour. The collect command
# itself skips tiers that aren't due and refuses to overlap a running one,
# so an hourly trigger is safe and cheap.
$startAt = (Get-Date).AddMinutes(60).ToString("HH:mm")

schtasks /Create /TN "tourfinder-collect" /TR $tr /SC HOURLY /ST $startAt /F

Write-Host "Registered 'tourfinder-collect', first run at $startAt, hourly after."
Write-Host "Watch it:   Get-Content '$root\data\collect.log' -Wait"
Write-Host "Run now:    schtasks /Run /TN tourfinder-collect"
