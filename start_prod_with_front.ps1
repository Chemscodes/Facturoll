param(
  [int]$EmailParallel = 3
)

$ErrorActionPreference = "Stop"

$root = $PSScriptRoot
$desktopDir = Join-Path $root "desktop"
$startProdScript = Join-Path $root "start_prod.ps1"

if (-not (Test-Path $startProdScript)) {
  throw "Missing script: $startProdScript"
}

if (-not (Test-Path $desktopDir)) {
  throw "Desktop directory not found: $desktopDir"
}

Write-Host "Starting backend + workers..."
& $startProdScript -EmailParallel $EmailParallel

# Restart desktop app to ensure latest UI/process.
Get-Process -Name "trial_desktop" -ErrorAction SilentlyContinue |
  Stop-Process -Force -ErrorAction SilentlyContinue

$flutter = Get-Command flutter -ErrorAction SilentlyContinue
if ($flutter) {
  Start-Process -FilePath $flutter.Source -ArgumentList "run -d windows" -WorkingDirectory $desktopDir
  Write-Host "Frontend started with flutter run (latest code)."
  return
}

$appExeDebug = Join-Path $root "desktop\build\windows\x64\runner\Debug\trial_desktop.exe"
$appExeRelease = Join-Path $root "desktop\build\windows\x64\runner\Release\trial_desktop.exe"

if (Test-Path $appExeDebug) {
  Start-Process -FilePath $appExeDebug
  Write-Host "Frontend started from Debug EXE."
  return
}

if (Test-Path $appExeRelease) {
  Start-Process -FilePath $appExeRelease
  Write-Host "Frontend started from Release EXE."
  return
}

throw "Flutter not found on PATH and no desktop EXE build found."
