$ErrorActionPreference = "Stop"

$BackendDir = "D:\SAAS\backend"
$PythonExe = Join-Path $BackendDir ".venv\Scripts\python.exe"
$LogDir = Join-Path $BackendDir "logs"
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$ReportPath = Join-Path $LogDir ("cleanup_report_" + $Stamp + ".json")

if (!(Test-Path $PythonExe)) {
  throw "Python venv not found: $PythonExe"
}

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
Set-Location $BackendDir

& $PythonExe "scripts\scheduled_cleanup.py" `
  --logs-days 30 `
  --outbox-days 30 `
  --email-jobs-days 90 `
  --json-out $ReportPath
