# stop_local_real.ps1
# =====================
# Arrete tous les processus lances par run_local_real.ps1

$pidFile = Join-Path $PSScriptRoot ".running_pids"

if (-not (Test-Path $pidFile)) {
    Write-Host "Aucun fichier .running_pids trouve. Rien a arreter." -ForegroundColor Yellow
    exit 0
}

Get-Content $pidFile | ForEach-Object {
    $procId = $_.Trim()
    if ($procId) {
        try {
            Stop-Process -Id $procId -Force -ErrorAction Stop
            Write-Host "Processus $procId arrete."
        } catch {
            Write-Host "Processus $procId deja arrete ou introuvable."
        }
    }
}

Remove-Item $pidFile -Force
Write-Host "Tous les services ont ete arretes." -ForegroundColor Green
