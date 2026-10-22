$root = $PSScriptRoot
$backendDir = Join-Path $root 'backend'
$python = Join-Path $backendDir '.venv\Scripts\python.exe'
$envFile = Join-Path $backendDir '.env'

if (-not (Test-Path $python)) {
  Write-Host "Backend venv not found at $python. Run setup in $backendDir."
  return
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

Start-Process -FilePath $python -ArgumentList "-m app.embedding_worker" -WorkingDirectory $backendDir -WindowStyle Minimized
Write-Host "Embedding worker started."
