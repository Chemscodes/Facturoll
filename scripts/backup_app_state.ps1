$ErrorActionPreference = "Stop"

$root = "d:\SAAS"
$ts = Get-Date -Format "yyyyMMdd_HHmmss"
$dest = Join-Path $root ("backup_state_{0}.zip" -f $ts)

Add-Type -AssemblyName System.IO.Compression.FileSystem
if (Test-Path $dest) { Remove-Item $dest -Force }

$zip = [System.IO.Compression.ZipFile]::Open($dest, 'Create')
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
Write-Output $dest
