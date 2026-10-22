$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$envFile = Join-Path $root '.env'

if (-not (Test-Path $python)) {
  Write-Host "Backend venv not found at $python. Run setup in $root."
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

& $python (Join-Path $PSScriptRoot 'backup_postgres.py') --output (Join-Path $root 'backups')
