param(
  [switch]$ForceRestart = $false,
  [switch]$StrictRouteCheck = $true
)

$root = $PSScriptRoot
$backendDir = Join-Path $root 'backend'
$python = Join-Path $backendDir '.venv\Scripts\python.exe'
$envFile = Join-Path $backendDir '.env'

function Test-BackendRoutes([int]$port) {
  try {
    $openapi = Invoke-RestMethod -Uri "http://127.0.0.1:$port/openapi.json" -TimeoutSec 4
    if (-not $openapi -or -not $openapi.paths) {
      return $false
    }
    $paths = @($openapi.paths.PSObject.Properties.Name)
    $required = @(
      '/campaigns/{campaign_id}/close',
      '/campaigns/{campaign_id}/export/xlsx',
      '/campaigns/{campaign_id}/purge'
    )
    foreach ($route in $required) {
      if (-not ($paths -contains $route)) {
        return $false
      }
    }
    return $true
  } catch {
    return $false
  }
}

function Stop-PortListeners([int]$port) {
  $listeners = @(
    Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
      Select-Object -ExpandProperty OwningProcess -Unique
  )
  foreach ($procId in $listeners) {
    if (-not $procId) { continue }
    try {
      Stop-Process -Id $procId -Force -ErrorAction Stop
      Start-Sleep -Milliseconds 700
      continue
    } catch {
      # fallback for protected process contexts
    }
    try {
      & taskkill /PID $procId /F | Out-Null
      Start-Sleep -Milliseconds 700
    } catch {
      # no-op
    }
  }
}

if (-not (Test-Path $python)) {
  throw "Backend venv not found at $python. Run setup in $backendDir."
}

if (Test-Path $envFile) {
  Get-Content $envFile | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#")) {
      return
    }
    $parts = $line -split "=", 2
    if ($parts.Length -ge 2) {
      $name = $parts[0].Trim()
      $value = $parts[1].Trim()
      if ($name) {
        Set-Item -Path "Env:$name" -Value $value
      }
    }
  }
}

$pgService = Get-Service -Name "postgresql-x64-18" -ErrorAction SilentlyContinue
if ($pgService) {
  if ($pgService.Status -ne 'Running') {
    try {
      Start-Service $pgService.Name -ErrorAction Stop
      Start-Sleep -Seconds 2
      $pgService.Refresh()
    } catch {
      $pgListening = $null
      try {
        $pgListening = Get-NetTCPConnection -LocalPort 5432 -State Listen -ErrorAction SilentlyContinue
      } catch {
        $pgListening = $null
      }
      if (-not $pgListening) {
        throw "PostgreSQL is stopped. Open PowerShell as ADMIN and run: Start-Service $($pgService.Name)"
      }
      Write-Host "PostgreSQL service could not be started without admin, but port 5432 is active: continuing."
    }
  }
}

$backendPort = 8001

$listening = $null
try {
  $listening = Get-NetTCPConnection -LocalPort $backendPort -State Listen -ErrorAction SilentlyContinue
} catch {
  $listening = $null
}

if ($listening) {
  $compatible = $true
  if ($StrictRouteCheck) {
    $compatible = Test-BackendRoutes -port $backendPort
  }
  if (-not $ForceRestart -and $compatible) {
    Write-Host "Backend already running on 127.0.0.1:$backendPort"
    return
  }

  Write-Host "Existing backend detected on $backendPort. Restarting..."
  Stop-PortListeners -port $backendPort
  Start-Sleep -Seconds 1

  $stillListening = Get-NetTCPConnection -LocalPort $backendPort -State Listen -ErrorAction SilentlyContinue
  if ($stillListening) {
    throw "Cannot stop existing process on port $backendPort. Re-run PowerShell as Administrator and retry: .\\start_backend.ps1 -ForceRestart"
  }
}

Start-Process -FilePath $python -ArgumentList "-m uvicorn main:app --reload --host 127.0.0.1 --port $backendPort" -WorkingDirectory $backendDir -WindowStyle Minimized
Write-Host "Backend started."
