"""
main.py - Scoring Service
===========================
Rôle : "Calcule le score et retourne le meilleur serveur"

Deux modes :
  - résilient : élit le meilleur cluster, host_name=null (DRS gère la
    répartition interne).
  - non résilient (par ESX) : une VM par upgrade domain, dans l'ordre fourni
    par le Filter Service ; un emplacement par VM dans `placements[]`.
"""

import logging
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from shared.models import Cluster, PlacementRequest, PlacementResult, SinglePlacement

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scoring_service")

app = FastAPI(title="Scoring Service", version="2.0.0")


class ScoringRequest(BaseModel):
    placement_request: PlacementRequest
    candidate_clusters: list[Cluster]


@app.get("/health")
def health():
    return {"status": "ok", "service": "scoring_service"}


def score_resilient(req: PlacementRequest, clusters: list[Cluster]) -> PlacementResult:
    best_cluster = min(clusters, key=lambda c: c.best_availability_score)

    if not best_cluster.datastore:
        raise HTTPException(status_code=404, detail=f"Aucun datastore disponible sur le cluster {best_cluster.name}")

    placement = SinglePlacement(
        cluster_name=best_cluster.name,
        host_name=None,
        datastore_name=best_cluster.datastore.name,
        score=best_cluster.best_availability_score,
    )
    return PlacementResult(
        cluster_name=placement.cluster_name,
        host_name=None,
        datastore_name=placement.datastore_name,
        vm_slots=best_cluster.vm_slots or req.nb_vms,
        score=placement.score,
        region=req.region,
        placements=[placement],
        logs=[f"{c.name}: score={c.best_availability_score:.2f}" for c in sorted(
            clusters, key=lambda c: c.best_availability_score
        )],
    )


def score_non_resilient(req: PlacementRequest, clusters: list[Cluster]) -> PlacementResult:
    if len(clusters) < req.nb_vms:
        raise HTTPException(
            status_code=404,
            detail=f"Pas assez d'upgrade domains pour {req.nb_vms} VM(s) ({len(clusters)} disponibles)",
        )

    placements: list[SinglePlacement] = []
    logs: list[str] = []
    for i in range(req.nb_vms):
        domain = clusters[i]
        if not domain.hosts:
            raise HTTPException(status_code=404, detail=f"Aucun ESX disponible sur le domaine {domain.name}")
        best_host = domain.hosts[0]
        datastore = best_host.datastore or domain.datastore
        if not datastore:
            raise HTTPException(status_code=404, detail=f"Aucun datastore disponible sur le domaine {domain.name}")
        placements.append(SinglePlacement(
            cluster_name=domain.name,
            host_name=best_host.name,
            datastore_name=datastore.name,
            score=best_host.best_availability_score,
        ))
        logs.append(f"VM {i + 1} -> {domain.name} / {best_host.name} (score={best_host.best_availability_score:.2f})")

    first = placements[0]
    return PlacementResult(
        cluster_name=first.cluster_name,
        host_name=first.host_name,
        datastore_name=first.datastore_name,
        vm_slots=1,
        score=first.score,
        region=req.region,
        placements=placements,
        logs=logs,
    )


@app.post("/score", response_model=PlacementResult)
def compute_best_placement(payload: ScoringRequest):
    req = payload.placement_request
    clusters = payload.candidate_clusters

    if not clusters:
        raise HTTPException(
            status_code=404,
            detail=f"Aucun cluster ne satisfait la demande de placement pour la région {req.region}",
        )

    if req.target_placement == "esx":
        result = score_non_resilient(req, clusters)
    else:
        result = score_resilient(req, clusters)

    logger.info(f"Placement calculé : {len(result.placements)} emplacement(s), meilleur score={result.score:.2f}")
    return result
