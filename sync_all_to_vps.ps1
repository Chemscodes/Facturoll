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
  [switch]$SyncSecrets
)

$ErrorActionPreference = "Stop"

function Require-Command([string]$Name) {
  if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
    throw "Missing required command: $Name"
  }
}

Require-Command "powershell"
Require-Command "ssh"
Require-Command "scp"

$root = $PSScriptRoot
$backendDir = Join-Path $root "backend"
$deployScript = Join-Path $root "deploy_backend_to_vps.ps1"

if (-not (Test-Path $backendDir)) {
  throw "Backend directory not found: $backendDir"
}
if (-not (Test-Path $deployScript)) {
  throw "Deploy script not found: $deployScript"
}

$target = "$User@$VpsHost"

Write-Host "Step 1/5 - Deploying backend code..."
$deployArgs = @(
  "-ExecutionPolicy", "Bypass",
  "-File", $deployScript,
  "-VpsHost", $VpsHost,
  "-User", $User
)
if ($InstallDeps) {
  $deployArgs += "-InstallDeps"
}
if ($RunMigrations) {
  $deployArgs += "-RunMigrations"
}
if ($SetStrictSchemaMode) {
  $deployArgs += "-SetStrictSchemaMode"
}
if ($InstallProcessManager) {
  $deployArgs += "-InstallProcessManager"
  $deployArgs += "-ProcessManagerUser"
  $deployArgs += $ProcessManagerUser
}
if ($RestartServices) {
  $deployArgs += "-RestartServices"
}
& powershell @deployArgs

if ($SyncSecrets) {
  Write-Host "Step 2/5 - Syncing secrets (.env, credentials.json, google_sa.json)..."
  $secretFiles = @(".env", "credentials.json", "google_sa.json")
  foreach ($name in $secretFiles) {
    $localPath = Join-Path $backendDir $name
    if (-not (Test-Path $localPath)) {
      throw "Secret file missing: $localPath"
    }
    & scp $localPath "${target}:$RemoteBackendDir/$name"
    if ($LASTEXITCODE -ne 0) {
      throw "scp failed for $name (exit code $LASTEXITCODE)"
    }
  }

  $remoteEnvFix = @"
set -e
cd '$RemoteBackendDir'

if [ -f .env ]; then
  if grep -q '^GOOGLE_CREDENTIALS_PATH=' .env; then
    sed -i 's#^GOOGLE_CREDENTIALS_PATH=.*#GOOGLE_CREDENTIALS_PATH=/opt/saas/backend/google_sa.json#' .env
  else
    echo 'GOOGLE_CREDENTIALS_PATH=/opt/saas/backend/google_sa.json' >> .env
  fi

  if grep -q '^GMAIL_CREDENTIALS_PATH=' .env; then
    sed -i 's#^GMAIL_CREDENTIALS_PATH=.*#GMAIL_CREDENTIALS_PATH=/opt/saas/backend/credentials.json#' .env
  else
    echo 'GMAIL_CREDENTIALS_PATH=/opt/saas/backend/credentials.json' >> .env
  fi
fi

chmod 600 .env credentials.json google_sa.json || true
"@
  $remoteEnvFix | & ssh $target "bash -s"
  if ($LASTEXITCODE -ne 0) {
    throw "Failed to normalize secret paths/permissions on VPS"
  }
} else {
  Write-Host "Step 2/5 - Skipped secret sync (use -SyncSecrets to include .env + credential files)."
}

$criticalFiles = @(
  "app/routes/campaigns.py",
  "app/schemas.py",
  "scripts/gmail_fetch.py"
)

Write-Host "Step 3/5 - Verifying critical file hashes..."
$localHashes = @{}
foreach ($relPath in $criticalFiles) {
  $localPath = Join-Path $backendDir $relPath.Replace("/", "\")
  if (-not (Test-Path $localPath)) {
    throw "Missing local critical file: $localPath"
  }
  $hash = (Get-FileHash $localPath -Algorithm SHA256).Hash.ToLower()
  $localHashes[$relPath] = $hash
}

$remoteHashCmd = "cd '$RemoteBackendDir' && sha256sum " + ($criticalFiles -join " ")
$remoteHashLines = & ssh $target $remoteHashCmd
if ($LASTEXITCODE -ne 0) {
  throw "Failed to fetch remote hashes"
}

$remoteHashes = @{}
foreach ($line in $remoteHashLines) {
  $parts = ($line -split "\s+", 3)
  if ($parts.Count -ge 2) {
    $remoteHashes[$parts[1]] = $parts[0].ToLower()
  }
}

$mismatches = @()
foreach ($relPath in $criticalFiles) {
  $localHash = $localHashes[$relPath]
  $remoteHash = $remoteHashes[$relPath]
  if (-not $remoteHash) {
    $mismatches += "$relPath (missing on VPS hash output)"
    continue
  }
  if ($localHash -ne $remoteHash) {
    $mismatches += "$relPath"
  }
  Write-Host ("  {0} -> {1}" -f $relPath, $remoteHash)
}
if ($mismatches.Count -gt 0) {
  throw "Hash mismatch on: $($mismatches -join ', ')"
}

Write-Host "Step 4/5 - Restarting services and validating health..."
$remoteRestartAndHealth = @"
set -e
if command -v systemctl >/dev/null 2>&1; then
  systemctl restart saas-backend saas-email-worker saas-embedding-worker
  systemctl restart saas-gmail-sync.timer || true
  systemctl start saas-gmail-sync.service || true
fi
sleep 2
curl -fsS http://127.0.0.1:8001/health
journalctl -u saas-gmail-sync.service -n 20 --no-pager || true
"@
$remoteRestartAndHealth | & ssh $target "bash -s"
if ($LASTEXITCODE -ne 0) {
  throw "Remote restart/health check failed"
}

Write-Host "Step 5/5 - Done."
Write-Host "VPS logic is now aligned with local critical code."
