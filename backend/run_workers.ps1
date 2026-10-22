param(
  [int]$Parallel = 2,
  [int]$EmailStuckTimeout = 600,
  [int]$EmbeddingStuckTimeout = 900
)

Set-Location "d:\SAAS\backend"
$emailArgs = "-m app.email_worker --parallel $Parallel --stuck-timeout $EmailStuckTimeout"
$embeddingArgs = "-m app.embedding_worker --stuck-timeout $EmbeddingStuckTimeout"
$email = Start-Process .\.venv\Scripts\python -ArgumentList $emailArgs -PassThru
$emb = Start-Process .\.venv\Scripts\python -ArgumentList $embeddingArgs -PassThru
Write-Host "Workers started -> email PID: $($email.Id), embedding PID: $($emb.Id)"
while ($true) {
  Start-Sleep -Seconds 5
  if ($email.HasExited -or $emb.HasExited) {
    Write-Host "One worker stopped."
    break
  }
}
