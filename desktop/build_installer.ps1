param(
  [switch]$SkipBuild,
  [switch]$RefreshPrereqs
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

if (-not $SkipBuild) {
  flutter build windows --release
}

$legacyExe = Join-Path $projectRoot "build\windows\x64\runner\Release\trial_desktop.exe"
if (Test-Path $legacyExe) {
  Remove-Item $legacyExe -Force
}

$prereqDir = Join-Path $projectRoot "installer\prerequisites"
if (-not (Test-Path $prereqDir)) {
  New-Item -ItemType Directory -Path $prereqDir | Out-Null
}
$vcRedist = Join-Path $prereqDir "vc_redist.x64.exe"
$vcRedistUrl = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
if ($RefreshPrereqs -or -not (Test-Path $vcRedist) -or (Get-Item $vcRedist).Length -le 0) {
  Write-Host "Downloading VC++ runtime from Microsoft..."
  Invoke-WebRequest -Uri $vcRedistUrl -OutFile $vcRedist
}

$iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) {
  throw "ISCC not found at '$iscc'. Install Inno Setup 6 first."
}

$iss = Join-Path $projectRoot "installer\FactuRoll.iss"
if (-not (Test-Path $iss)) {
  throw "Installer script not found: $iss"
}

& $iscc $iss

$setupPath = Join-Path $projectRoot "installer\output\FactuRoll-Setup.exe"
if (-not (Test-Path $setupPath)) {
  throw "Setup file was not generated."
}

Write-Host ""
Write-Host "Installer generated:"
Write-Host $setupPath -ForegroundColor Green
