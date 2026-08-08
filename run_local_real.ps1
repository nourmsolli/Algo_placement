# run_local_real.ps1
# ====================
# Lance TOUT le pipeline en local (sans Docker), en mode REEL : les 8
# microservices, dont le VRAI Inventory Service et le VRAI DRS Adapter
# Service connectes a vSphere.
#
# A placer A LA RACINE du projet vm-placement-microservices\ (a cote de
# services\ et shared\).
#
# Pre-requis :
#   1. Un venv cree a la racine du projet, avec toutes les dependances
#      installees (les 8 requirements.txt).
#   2. Un fichier .env a la racine avec tes identifiants vSphere.
#
# Lancement :  .\run_local_real.ps1
# Arret     :  .\stop_local_real.ps1

$ErrorActionPreference = "Stop"

$ProjectRoot = $PSScriptRoot

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "Erreur : environnement virtuel introuvable ($VenvPython)." -ForegroundColor Red
    Write-Host "Cree-le d'abord a la racine du projet : py -m venv .venv ; puis installe les requirements." -ForegroundColor Red
    exit 1
}

$EnvFile = Join-Path $ProjectRoot ".env"
if (-not (Test-Path $EnvFile)) {
    Write-Host "Erreur : fichier .env introuvable a la racine du projet." -ForegroundColor Red
    Write-Host "Copie .env.example en .env et remplis tes identifiants vSphere." -ForegroundColor Red
    exit 1
}

$VsphereEnv = @{}
Get-Content $EnvFile | ForEach-Object {
    if ($_ -match '^\s*([^#][^=]*)=(.*)$') {
        $VsphereEnv[$matches[1].Trim()] = $matches[2].Trim()
    }
}
Write-Host "Variables chargees depuis .env : $($VsphereEnv.Keys -join ', ')" -ForegroundColor DarkGray

$global:ChildProcesses = @()

function Start-MicroService {
    param(
        [string]$Dir,
        [string]$Name,
        [int]$Port,
        [hashtable]$ExtraEnv = @{}
    )
    Write-Host "-> Demarrage de $Name sur le port $Port"

    $envVars = @{ "PYTHONPATH" = "$ProjectRoot;$Dir" }
    foreach ($key in $ExtraEnv.Keys) { $envVars[$key] = $ExtraEnv[$key] }

    $envSetters = ($envVars.GetEnumerator() | ForEach-Object { "`$env:$($_.Key) = '$($_.Value)'" }) -join "; "
    $command = "$envSetters; Set-Location '$Dir'; & '$VenvPython' -m uvicorn main:app --host 0.0.0.0 --port $Port --reload"

    $proc = Start-Process powershell -ArgumentList "-NoExit", "-Command", $command -PassThru
    $global:ChildProcesses += $proc
}

Write-Host "=== 1. Services connectes a vSphere (REELS) ===" -ForegroundColor Cyan
Start-MicroService -Dir (Join-Path $ProjectRoot "services\inventory_service") -Name "inventory_service" -Port 8001 -ExtraEnv $VsphereEnv
Start-MicroService -Dir (Join-Path $ProjectRoot "services\drs_adapter_service") -Name "drs_adapter_service" -Port 8005 -ExtraEnv $VsphereEnv

Write-Host ""
Write-Host "=== 2. Services metier (pas d'acces vSphere necessaire) ===" -ForegroundColor Cyan
Start-MicroService -Dir (Join-Path $ProjectRoot "services\filter_service") -Name "filter_service" -Port 8002
# Note : si le port 8003 est reserve par Windows chez toi (netsh interface
# ipv4 show excludedportrange protocol=tcp), change-le ici ET dans
# BOOKING_SERVICE_URL juste en dessous.
Start-MicroService -Dir (Join-Path $ProjectRoot "services\booking_service") -Name "booking_service" -Port 8003
Start-MicroService -Dir (Join-Path $ProjectRoot "services\affinity_service") -Name "affinity_service" -Port 8004
Start-MicroService -Dir (Join-Path $ProjectRoot "services\scoring_service") -Name "scoring_service" -Port 8006

Write-Host ""
Write-Host "=== 3. Orchestrateur ===" -ForegroundColor Cyan
$orchestratorEnv = @{
    "INVENTORY_SERVICE_URL"   = "http://127.0.0.1:8001"
    "FILTER_SERVICE_URL"      = "http://127.0.0.1:8002"
    "BOOKING_SERVICE_URL"     = "http://127.0.0.1:8003"
    "AFFINITY_SERVICE_URL"    = "http://127.0.0.1:8004"
    "DRS_ADAPTER_SERVICE_URL" = "http://127.0.0.1:8005"
    "SCORING_SERVICE_URL"     = "http://127.0.0.1:8006"
}
Start-MicroService -Dir (Join-Path $ProjectRoot "services\orchestrator") -Name "orchestrator" -Port 8007 -ExtraEnv $orchestratorEnv

Write-Host ""
Write-Host "=== 4. API Gateway ===" -ForegroundColor Cyan
Start-MicroService -Dir (Join-Path $ProjectRoot "services\api_gateway") -Name "api_gateway" -Port 8000 -ExtraEnv @{ "ORCHESTRATOR_URL" = "http://127.0.0.1:8007" }

Write-Host ""
Write-Host "Pipeline complet lance en mode REEL (connecte a vSphere)." -ForegroundColor Green
Write-Host "Documentation Swagger : http://localhost:8000/docs" -ForegroundColor Green
Write-Host "Attention : le premier placement peut etre plus long (premier scan vSphere)," -ForegroundColor Yellow
Write-Host "les suivants seront rapides grace au cache (30 min par defaut)." -ForegroundColor Yellow
Write-Host "Pour tout arreter : .\stop_local_real.ps1"

$global:ChildProcesses | ForEach-Object { $_.Id } | Out-File -FilePath (Join-Path $ProjectRoot ".running_pids") -Encoding ascii
