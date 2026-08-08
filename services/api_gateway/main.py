"""
main.py - API Gateway
=======================
Point d'entrée unique exposé aux clients externes. Relaie vers l'Orchestrateur.
"""

import os
import logging
import httpx
from fastapi import FastAPI, HTTPException, Query

from shared.models import PlacementRequest, PlacementResult

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api_gateway")

app = FastAPI(
    title="API Gateway - VM Placement",
    description="Point d'entrée unique pour les demandes de création de VM",
    version="1.0.0",
)

ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://orchestrator:8000")


@app.get("/health")
def health():
    return {"status": "ok", "service": "api_gateway"}


@app.post("/api/v1/placements", response_model=PlacementResult)
async def create_placement(request: PlacementRequest, backend: str = Query(..., description="Code du backend vSphere cible, ex: DC1")):
    logger.info(f"Nouvelle demande de placement reçue pour la région {request.region}, backend {backend}")
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{ORCHESTRATOR_URL}/place",
                params={"backend_code": backend},
                json=request.model_dump(),
                timeout=900.0,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            detail = e.response.json().get("detail", str(e)) if e.response.content else str(e)
            raise HTTPException(status_code=e.response.status_code, detail=detail)
        except httpx.RequestError as e:
            raise HTTPException(status_code=503, detail=f"Orchestrateur injoignable : {e}")
