$root = $PSScriptRoot

& (Join-Path $root 'start_backend.ps1')

# Stop existing app to ensure latest UI is loaded.
Get-Process -Name 'trial_desktop' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

$flutter = Get-Command flutter -ErrorAction SilentlyContinue
if ($flutter) {
  Start-Process -FilePath $flutter.Source -ArgumentList "run -d windows" -WorkingDirectory (Join-Path $root 'desktop')
  Write-Host "Frontend started with flutter run (latest code)."
  return
}

$appExeDebug = Join-Path $root 'desktop\build\windows\x64\runner\Debug\trial_desktop.exe'
$appExeRelease = Join-Path $root 'desktop\build\windows\x64\runner\Release\trial_desktop.exe'
if (Test-Path $appExeDebug) {
  Start-Process -FilePath $appExeDebug
  Write-Host "Flutter not found. Frontend started from Debug EXE."
  return
}
if (Test-Path $appExeRelease) {
  Start-Process -FilePath $appExeRelease
  Write-Host "Flutter not found. Frontend started from Release EXE."
  return
}

Write-Host "Flutter not found on PATH and no desktop EXE build found."
