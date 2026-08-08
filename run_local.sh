#!/usr/bin/env bash
# run_local.sh — lance tous les microservices en local (Linux/Mac), mode réel.
# Équivalent de run_local_real.ps1 pour Windows.

set -e
export PYTHONPATH="$(pwd)"
set -a
[ -f .env ] && source .env
set +a

run_service () {
  local name=$1
  local port=$2
  echo "-> Démarrage de $name sur le port $port"
  (cd services/$name && PYTHONPATH="$(pwd)/../..:$(pwd)" uvicorn main:app --host 0.0.0.0 --port $port --reload) &
}

run_service inventory_service 8001
run_service drs_adapter_service 8005
run_service filter_service 8002
run_service booking_service 8003
run_service affinity_service 8004
run_service scoring_service 8006

export INVENTORY_SERVICE_URL=http://localhost:8001
export FILTER_SERVICE_URL=http://localhost:8002
export BOOKING_SERVICE_URL=http://localhost:8003
export AFFINITY_SERVICE_URL=http://localhost:8004
export DRS_ADAPTER_SERVICE_URL=http://localhost:8005
export SCORING_SERVICE_URL=http://localhost:8006
run_service orchestrator 8007

export ORCHESTRATOR_URL=http://localhost:8007
run_service api_gateway 8000

export API_GATEWAY_URL=http://localhost:8000
export API_GATEWAY_PUBLIC_URL=http://localhost:8000/docs
run_service web_ui 8090

echo ""
echo "Tous les services sont lancés."
echo "Interface web       : http://localhost:8090"
echo "Documentation Swagger : http://localhost:8000/docs"
echo "Appuie sur CTRL+C pour tout arrêter."
wait
