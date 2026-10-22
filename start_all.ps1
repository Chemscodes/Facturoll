$ErrorActionPreference = 'Stop'

$root = $PSScriptRoot
$frontendDir = Join-Path $root 'desktop'
$backendScript = Join-Path $root 'start_backend.ps1'

# Start backend (prints a clear message if PostgreSQL is stopped)
powershell -ExecutionPolicy Bypass -File $backendScript

# Start frontend in the same window
Set-Location $frontendDir
Write-Host "Starting frontend..."
flutter run -d windows
