param(
  [Parameter(Mandatory = $true)]
  [string]$ArchivePath
)

$ErrorActionPreference = "Stop"

$root = "d:\SAAS"
$ts = Get-Date -Format "yyyyMMdd_HHmmss"

if (-not (Test-Path $ArchivePath)) {
  throw "Archive not found: $ArchivePath"
}

# Safety backup of current state
$safety = Join-Path $root ("backup_before_restore_{0}.zip" -f $ts)
Add-Type -AssemblyName System.IO.Compression.FileSystem
if (Test-Path $safety) { Remove-Item $safety -Force }

$zip = [System.IO.Compression.ZipFile]::Open($safety, 'Create')
$rootLen = $root.Length + 1
Get-ChildItem -Path $root -Recurse -File -Force | ForEach-Object {
  $full = $_.FullName
  $rel = $full.Substring($rootLen)
  try {
    $entry = $zip.CreateEntry($rel)
    $entryStream = $entry.Open()
    $fs = [System.IO.File]::Open(
      $full,
      [System.IO.FileMode]::Open,
      [System.IO.FileAccess]::Read,
      [System.IO.FileShare]::ReadWrite
    )
    $fs.CopyTo($entryStream)
    $fs.Close()
    $entryStream.Close()
  } catch {
    if ($entryStream) { $entryStream.Dispose() }
  }
}
$zip.Dispose()

Write-Host "Safety backup created: $safety"

# Restore archive contents over current tree via temp dir to avoid locking the zip itself.
$tmp = Join-Path $root ("_restore_tmp_{0}" -f $ts)
if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
Expand-Archive -Path $ArchivePath -DestinationPath $tmp -Force

# Copy over (do not delete extras).
robocopy $tmp $root /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
Remove-Item -Recurse -Force $tmp

Write-Host "Restore complete from: $ArchivePath"
