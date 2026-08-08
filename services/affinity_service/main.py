"""
main.py - Affinity Service
============================
Rôle : "Vérifie les règles d'écartement entre les VMs" (anti-affinité DRS).
"""

import logging
from fastapi import FastAPI
from pydantic import BaseModel

from shared.models import PlacementRequest, Cluster

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("affinity_service")

app = FastAPI(title="Affinity Service", version="1.0.0")


class AffinityCheckRequest(BaseModel):
    placement_request: PlacementRequest
    candidate_clusters: list[Cluster]


class AffinityCheckResponse(BaseModel):
    valid_clusters: list[Cluster]
    eliminated: dict[str, str]


@app.get("/health")
def health():
    return {"status": "ok", "service": "affinity_service"}


def compute_available_slots(cluster: Cluster, rule_name: str, nb_vms: int) -> int | None:
    rule = next((r for r in cluster.affinity_rules if r.name == rule_name), None)
    if rule is None:
        return None
    occupied_hosts = set(rule.member_hosts)
    free_hosts = [h for h in cluster.hosts if h.name not in occupied_hosts]
    return len(free_hosts)


@app.post("/affinity/check", response_model=AffinityCheckResponse)
def check_affinity(payload: AffinityCheckRequest):
    req = payload.placement_request
    valid: list[Cluster] = []
    eliminated: dict[str, str] = {}

    if not req.anti_affinity:
        return AffinityCheckResponse(valid_clusters=payload.candidate_clusters, eliminated={})

    for cluster in payload.candidate_clusters:
        available = compute_available_slots(cluster, req.anti_affinity, req.nb_vms)

        if available is None:
            valid.append(cluster)
            continue

        if available <= 0:
            eliminated[cluster.name] = (
                f"Règle d'anti-affinité '{req.anti_affinity}' : plus aucun ESX libre sur ce cluster"
            )
            continue

        cluster.vm_slots = min(cluster.vm_slots or req.nb_vms, available, req.nb_vms)
        valid.append(cluster)

    return AffinityCheckResponse(valid_clusters=valid, eliminated=eliminated)
