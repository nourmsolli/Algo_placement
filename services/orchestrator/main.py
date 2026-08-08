"""
main.py - Orchestrateur (Placement Adapter Engine)
=====================================================
Coordonne tous les microservices dans l'ordre :
  0. Sélection des règles selon l'offre (offer_code / GPU / esx)
  1. Inventory Service   -> scanner/récupérer l'inventaire vSphere
  2. Filter Service      -> éliminer les clusters ne correspondant pas
  3. Verrou de région + Booking Service -> déduire les réservations récentes
  4. Affinity Service    -> vérifier les règles d'anti-affinité
  5. DRS Adapter Service -> rafraîchir la charge réelle des serveurs restants
  6. Scoring Service     -> calculer le score et élire le meilleur candidat
  7. Booking Service     -> enregistrer la/les nouvelle(s) réservation(s)

Toute la séquence 3->7 est protégée par un verrou par région (pris au début,
relâché dans un finally), pour empêcher deux demandes simultanées sur la
même région de choisir le même serveur.
"""

import os
import logging
import httpx
from fastapi import FastAPI, HTTPException

from shared.models import PlacementRequest, PlacementResult
from shared.rules import get_rules_for_request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("orchestrator")

app = FastAPI(title="Orchestrator - Placement Adapter Engine", version="1.0.0")

INVENTORY_URL = os.getenv("INVENTORY_SERVICE_URL", "http://inventory_service:8000")
FILTER_URL = os.getenv("FILTER_SERVICE_URL", "http://filter_service:8000")
BOOKING_URL = os.getenv("BOOKING_SERVICE_URL", "http://booking_service:8000")
AFFINITY_URL = os.getenv("AFFINITY_SERVICE_URL", "http://affinity_service:8000")
DRS_ADAPTER_URL = os.getenv("DRS_ADAPTER_SERVICE_URL", "http://drs_adapter_service:8000")
SCORING_URL = os.getenv("SCORING_SERVICE_URL", "http://scoring_service:8000")

# Long, car le premier scan vSphere (sans cache chaud) peut prendre plusieurs
# minutes sur une grosse infra. Le scan optimisé réduit ça énormément, mais
# on garde une marge de sécurité généreuse.
HTTP_TIMEOUT = 900.0


@app.get("/health")
def health():
    return {"status": "ok", "service": "orchestrator"}


async def call(client: httpx.AsyncClient, method: str, url: str, **kwargs):
    try:
        response = await client.request(method, url, timeout=HTTP_TIMEOUT, **kwargs)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as e:
        detail = e.response.json().get("detail", str(e)) if e.response.content else str(e)
        raise HTTPException(status_code=e.response.status_code, detail=detail)
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"Service injoignable ({url}) : {e}")


@app.post("/place", response_model=PlacementResult)
async def place_vm(req: PlacementRequest, backend_code: str):
    async with httpx.AsyncClient() as client:

        # --- Étape 0 : sélection des règles selon l'offre ---
        rules = get_rules_for_request(req)
        logger.info(f"Règle sélectionnée pour cette demande : {rules['code']}")

        # --- Étape 1 : Inventaire (scan ou cache) ---
        logger.info(f"[1/6] Récupération de l'inventaire vSphere pour backend={backend_code}")
        inventory = await call(
            client, "GET", f"{INVENTORY_URL}/inventory/{backend_code}",
            params={"cpu_to_vcpu_ratio": rules["cpu_to_vcpu_ratio"]},
        )

        # --- Étape 2 : Filtrage des clusters candidats ---
        logger.info("[2/6] Filtrage des clusters candidats")
        filter_result = await call(
            client, "POST", f"{FILTER_URL}/filter",
            json={"placement_request": req.model_dump(), "inventory": inventory},
        )
        candidates = filter_result["candidate_clusters"]
        if not candidates:
            raise HTTPException(status_code=404, detail=f"Aucun cluster ne passe le filtrage : {filter_result['eliminated']}")

        # --- Étape 3 : Verrou de région + déduction des réservations récentes ---
        logger.info("[3/6] Acquisition du verrou de région et déduction des réservations récentes")
        lock = await call(client, "POST", f"{BOOKING_URL}/locks/acquire", json={"region": req.region})
        lock_token = lock["token"]

        try:
            deduct_result = await call(
                client, "POST", f"{BOOKING_URL}/bookings/deduct",
                json={"region": req.region, "candidate_clusters": candidates},
            )
            candidates = deduct_result["adjusted_clusters"]

            # --- Étape 4 : Règles d'anti-affinité ---
            logger.info("[4/6] Vérification des règles d'anti-affinité")
            affinity_result = await call(
                client, "POST", f"{AFFINITY_URL}/affinity/check",
                json={"placement_request": req.model_dump(), "candidate_clusters": candidates},
            )
            candidates = affinity_result["valid_clusters"]
            if not candidates:
                raise HTTPException(status_code=404, detail=f"Aucun cluster ne respecte l'anti-affinité : {affinity_result['eliminated']}")

            # --- Étape 5 : Charge réelle (DRS) ---
            logger.info("[5/6] Lecture de la charge réelle des serveurs restants")
            drs_result = await call(
                client, "POST", f"{DRS_ADAPTER_URL}/drs/refresh-load",
                json={"backend_code": backend_code, "candidate_clusters": candidates},
            )
            candidates = drs_result["refreshed_clusters"]

            # --- Étape 6 : Scoring et élection du meilleur candidat ---
            logger.info("[6/6] Calcul du score et élection du meilleur candidat")
            result = await call(
                client, "POST", f"{SCORING_URL}/score",
                json={"placement_request": req.model_dump(), "candidate_clusters": candidates},
            )

            # --- Étape 7 : enregistrement des réservations, toujours sous verrou ---
            logger.info("Enregistrement des réservations pour éviter la surallocation")
            placements = result.get("placements") or [result]
            if req.target_placement == "esx":
                for p in placements:
                    await call(
                        client, "POST", f"{BOOKING_URL}/bookings",
                        json={
                            "region": req.region,
                            "cluster_name": p["cluster_name"],
                            "host_name": p.get("host_name"),
                            "cpu_booked": req.cpu_size,
                            "memory_booked": req.ram_size,
                            "storage_booked": req.storage_size,
                        },
                    )
            else:
                await call(
                    client, "POST", f"{BOOKING_URL}/bookings",
                    json={
                        "region": req.region,
                        "cluster_name": result["cluster_name"],
                        "host_name": result.get("host_name"),
                        "cpu_booked": req.cpu_size * req.nb_vms,
                        "memory_booked": req.ram_size * req.nb_vms,
                        "storage_booked": req.storage_size * req.nb_vms,
                    },
                )
        finally:
            await call(client, "POST", f"{BOOKING_URL}/locks/release", json={"region": req.region, "token": lock_token})

        return result
