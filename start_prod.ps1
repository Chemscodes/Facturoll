param(
  [int]$EmailParallel = 3
)

$ErrorActionPreference = "Stop"

$root = $PSScriptRoot
$backendDir = Join-Path $root "backend"
$python = Join-Path $backendDir ".venv\\Scripts\\python.exe"
$envFile = Join-Path $backendDir ".env"
$backendScript = Join-Path $root "start_backend.ps1"

function Get-PythonWorkers([string]$pattern) {
  @(
    Get-CimInstance Win32_Process |
      Where-Object {
        $_.Name -eq "python.exe" -and
        ([string]$_.CommandLine -match $pattern)
      } |
      Sort-Object CreationDate
  )
}

function Get-WorkerRoots([string]$pattern) {
  $workers = Get-PythonWorkers $pattern
  if ($workers.Count -eq 0) {
    return @()
  }
  $ids = @{}
  foreach ($proc in $workers) {
    $ids[[int]$proc.ProcessId] = $true
  }
  @(
    $workers | Where-Object {
      $parentId = [int]$_.ParentProcessId
      -not $ids.ContainsKey($parentId)
    }
  )
}

function Import-DotEnv([string]$path) {
  if (-not (Test-Path $path)) {
    return
  }
  Get-Content $path | ForEach-Object {
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

function Ensure-SingleWorker([string]$name, [string]$pattern, [scriptblock]$startAction) {
  $roots = @(Get-WorkerRoots $pattern)

  if ($roots.Count -gt 1) {
    $extra = $roots | Select-Object -Skip 1
    foreach ($proc in $extra) {
      Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 1200
  }

  $roots = @(Get-WorkerRoots $pattern)
  if ($roots.Count -eq 0) {
    & $startAction
    for ($i = 0; $i -lt 8; $i++) {
      Start-Sleep -Seconds 1
      $roots = @(Get-WorkerRoots $pattern)
      if ($roots.Count -gt 0) {
        break
      }
    }
  }

  # Final dedupe pass to avoid race conditions.
  if ($roots.Count -gt 1) {
    $extra = $roots | Select-Object -Skip 1
    foreach ($proc in $extra) {
      Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 1200
    $roots = @(Get-WorkerRoots $pattern)
  }

  if ($roots.Count -eq 0) {
    throw "$name failed to start."
  }

  $pidList = ($roots | ForEach-Object { $_.ProcessId }) -join ", "
  $procCount = @(Get-PythonWorkers $pattern).Count
  Write-Host "$name running (instances: $($roots.Count), processes: $procCount, root PID: $pidList)"
}

if (-not (Test-Path $backendScript)) {
  throw "Missing script: $backendScript"
}
if (-not (Test-Path $python)) {
  throw "Missing Python venv: $python"
}

Write-Host "Starting backend..."
& $backendScript -StrictRouteCheck
Import-DotEnv $envFile

$healthy = $false
for ($i = 0; $i -lt 25; $i++) {
  try {
    $resp = Invoke-WebRequest -Uri "http://127.0.0.1:8001/health" -UseBasicParsing -TimeoutSec 2
    if ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 300) {
      $healthy = $true
      break
    }
  } catch {
    # keep waiting
  }
  Start-Sleep -Seconds 1
}

if (-not $healthy) {
  throw "Backend health check failed on http://127.0.0.1:8001/health"
}
Write-Host "Backend healthy."

Ensure-SingleWorker "Email worker" "app.email_worker" {
  Start-Process -FilePath $python -ArgumentList "-m app.email_worker --parallel $EmailParallel" -WorkingDirectory $backendDir -WindowStyle Minimized | Out-Null
}

Ensure-SingleWorker "Embedding worker" "app.embedding_worker" {
  Start-Process -FilePath $python -ArgumentList "-m app.embedding_worker" -WorkingDirectory $backendDir -WindowStyle Minimized | Out-Null
}

$emailInstances = @(Get-WorkerRoots "app.email_worker").Count
$embeddingInstances = @(Get-WorkerRoots "app.embedding_worker").Count
$listening8001 = @(
  Get-NetTCPConnection -LocalPort 8001 -State Listen -ErrorAction SilentlyContinue
).Count

Write-Host ""
Write-Host "Ready:"
Write-Host " - Backend listen 8001: $listening8001"
Write-Host " - Email worker instances: $emailInstances"
Write-Host " - Embedding worker instances: $embeddingInstances"
