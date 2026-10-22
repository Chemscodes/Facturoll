param(
  [string]$VpsHost = "46.202.128.235",
  [string]$User = "root",
  [string]$RemoteBackendDir = "/opt/saas/backend",
  [switch]$InstallDeps,
  [switch]$RunMigrations,
  [switch]$SetStrictSchemaMode,
  [switch]$InstallProcessManager,
  [string]$ProcessManagerUser = "root",
  [switch]$RestartServices,
  [string]$BackendService = "saas-backend",
  [string]$EmailWorkerService = "saas-email-worker",
  [string]$EmbeddingWorkerService = "saas-embedding-worker",
  [string]$HealthUrl = "http://127.0.0.1:8001/health"
)

$ErrorActionPreference = "Stop"

$root = $PSScriptRoot
$backendDir = Join-Path $root "backend"
if (-not (Test-Path $backendDir)) {
  throw "Backend directory not found: $backendDir"
}

foreach ($cmd in @("tar", "scp", "ssh", "robocopy")) {
  if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
    throw "Missing required command: $cmd"
  }
}

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$tempRoot = Join-Path $env:TEMP "saas_backend_deploy_$stamp"
$stagingDir = Join-Path $tempRoot "backend"
$archivePath = Join-Path $tempRoot "backend_$stamp.tar.gz"

New-Item -ItemType Directory -Path $stagingDir -Force | Out-Null

$excludeDirs = @(
  (Join-Path $backendDir ".venv"),
  (Join-Path $backendDir "__pycache__"),
  (Join-Path $backendDir ".pytest_cache"),
  (Join-Path $backendDir ".mypy_cache"),
  (Join-Path $backendDir "logs"),
  (Join-Path $backendDir "outbox"),
  (Join-Path $backendDir "backups")
)
$excludeFiles = @(".env", "app.db")

Write-Host "Preparing backend package..."
$robocopyArgs = @(
  $backendDir,
  $stagingDir,
  "/MIR",
  "/NFL",
  "/NDL",
  "/NJH",
  "/NJS",
  "/NP",
  "/XD"
) + $excludeDirs + @(
  "/XF"
) + $excludeFiles

$null = & robocopy @robocopyArgs
if ($LASTEXITCODE -ge 8) {
  throw "robocopy failed with exit code $LASTEXITCODE"
}

Push-Location $tempRoot
try {
  & tar -czf $archivePath "backend"
  if ($LASTEXITCODE -ne 0) {
    throw "tar failed with exit code $LASTEXITCODE"
  }
} finally {
  Pop-Location
}

$target = "$User@$VpsHost"
$remoteArchive = "/tmp/backend_$stamp.tar.gz"

Write-Host "Uploading archive to $target..."
& scp $archivePath "${target}:$remoteArchive"
if ($LASTEXITCODE -ne 0) {
  throw "scp failed with exit code $LASTEXITCODE"
}

$remoteCommands = @(
  "set -e",
  "mkdir -p '$RemoteBackendDir'",
  "tar -xzf '$remoteArchive' -C '$RemoteBackendDir' --strip-components=1",
  "rm -f '$remoteArchive'"
)

if ($InstallDeps) {
  $remoteCommands += "if [ -x '$RemoteBackendDir/.venv/bin/pip' ]; then '$RemoteBackendDir/.venv/bin/pip' install -r '$RemoteBackendDir/requirements.txt'; else echo 'skip pip (venv missing)'; fi"
}

if ($SetStrictSchemaMode) {
  $remoteCommands += "if [ -f '$RemoteBackendDir/.env' ]; then if grep -q '^APP_DB_SCHEMA_MODE=' '$RemoteBackendDir/.env'; then sed -i 's#^APP_DB_SCHEMA_MODE=.*#APP_DB_SCHEMA_MODE=alembic_strict#' '$RemoteBackendDir/.env'; else echo 'APP_DB_SCHEMA_MODE=alembic_strict' >> '$RemoteBackendDir/.env'; fi; else echo 'skip strict schema mode (.env missing)'; fi"
}

if ($RunMigrations) {
  $remoteCommands += "if [ -x '$RemoteBackendDir/.venv/bin/python' ]; then cd '$RemoteBackendDir' && '$RemoteBackendDir/.venv/bin/python' -m alembic upgrade head; else echo 'skip alembic upgrade (venv missing)'; fi"
}

if ($InstallProcessManager) {
  $remoteCommands += "if [ -f '$RemoteBackendDir/scripts/install_systemd_units.sh' ]; then bash '$RemoteBackendDir/scripts/install_systemd_units.sh' --backend-dir '$RemoteBackendDir' --run-user '$ProcessManagerUser' --restart --validate-health --health-url '$HealthUrl'; else echo 'skip process manager install (installer missing)'; fi"
}

if ($RestartServices) {
  $remoteCommands += "if command -v systemctl >/dev/null 2>&1; then systemctl restart '$BackendService' || true; systemctl restart '$EmailWorkerService' || true; systemctl restart '$EmbeddingWorkerService' || true; fi"
  $remoteCommands += "if command -v curl >/dev/null 2>&1; then curl -fsS '$HealthUrl' >/dev/null || true; fi"
}

$remoteScript = $remoteCommands -join "; "

Write-Host "Applying backend update on VPS..."
& ssh $target $remoteScript
if ($LASTEXITCODE -ne 0) {
  throw "ssh failed with exit code $LASTEXITCODE"
}

Remove-Item $tempRoot -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Deploy finished."
Write-Host "Host: $target"
Write-Host "Remote dir: $RemoteBackendDir"
Write-Host "Archive: $archivePath"
Write-Host "Notes: .env and app.db were excluded from upload."
if ($RunMigrations) {
  Write-Host "Migrations: alembic upgrade head executed."
}
if ($SetStrictSchemaMode) {
  Write-Host "Schema mode: APP_DB_SCHEMA_MODE set to alembic_strict in remote .env."
}
if ($InstallProcessManager) {
  Write-Host "Process manager: systemd units installed/validated via install_systemd_units.sh."
}
