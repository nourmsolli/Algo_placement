"""
main.py - Filter Service
==========================
Rôle : "Eliminer les serveurs qui ne correspondent pas"

Porte toute la logique de ton monolithe (retrieve_candidate_clusters) :
  - Convention de nommage par offre .......... is_obj_match_naming
  - Filtre de version ESXi ................... host_have_good_version
  - Règle VM group ........................... check_cluster_vm_group_rule
  - Slots mémoire GPU ........................ check_cluster_gpu_memory
  - Profils GPU allouables ................... check_cluster_gpu_allocable_profile
  - Slots CPU du pool de ressources .......... cluster_cpu_slots
  - Slots mémoire du pool de ressources ...... cluster_memory_slots
  - Espace disque avec résilience ............ datastore_has_enough_space
  - Placement non-résilient / upgrade domains  elect_non_resilient_clusters
"""

import logging
from fastapi import FastAPI
from pydantic import BaseModel

from shared.models import PlacementRequest, VsphereInventory, Cluster
from shared.rules import get_rules_for_request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("filter_service")

app = FastAPI(title="Filter Service", version="2.0.0")


class FilterRequest(BaseModel):
    placement_request: PlacementRequest
    inventory: VsphereInventory


class FilterResponse(BaseModel):
    candidate_clusters: list[Cluster]
    eliminated: dict[str, str]


@app.get("/health")
def health():
    return {"status": "ok", "service": "filter_service"}


def cluster_matches_naming(cluster: Cluster, rules: dict) -> tuple[bool, str]:
    segments = cluster.name.split("-")
    for rule in rules["naming"]:
        if rule["pos"] >= len(segments) or segments[rule["pos"]] != rule["val"]:
            return False, (
                f"Nom du cluster hors convention {rules['code']} "
                f"(segment {rule['pos']} attendu = '{rule['val']}')"
            )
    return True, ""


def cluster_matches_esx_version(cluster: Cluster, req: PlacementRequest) -> tuple[bool, str]:
    if not req.esx_version_filter:
        return True, ""
    versions = tuple(req.esx_version_filter)
    bad = [h.name for h in cluster.hosts if h.esx_version and not h.esx_version.startswith(versions)]
    if bad:
        return False, f"Version ESXi non conforme au filtre {req.esx_version_filter} sur : {', '.join(bad[:3])}"
    return True, ""


def cluster_has_vm_group(cluster: Cluster, req: PlacementRequest) -> tuple[bool, str]:
    if not req.vm_group:
        return True, ""
    if req.vm_group in cluster.vm_group_names:
        return True, ""
    return False, f"Aucun VM group '{req.vm_group}' déclaré sur ce cluster"


def gpu_memory_slots(cluster: Cluster, req: PlacementRequest) -> int:
    allocable = int(cluster.custom_attributes.get("attr_GpuBufferAllocableGb", 0) or 0)
    allocated = 0
    slots = 0
    if (allocated + req.gpu_memory) <= allocable:
        while allocated < allocable and slots < req.nb_vms:
            allocated += req.gpu_memory
            if allocated <= allocable:
                slots += 1
    return slots


def gpu_allocable_profile_slots(cluster: Cluster, req: PlacementRequest) -> int:
    attribute_name = f"attr_GpuNbProfil{req.offer_code}Allocable"
    allocable = int(cluster.custom_attributes.get(attribute_name, 0) or 0)
    allocated = 0
    if allocable > 0:
        while allocated < allocable and allocated < req.nb_vms:
            allocated += 1
    return allocated


def cluster_cpu_slots(cluster: Cluster, req: PlacementRequest) -> int:
    if not cluster.resource_pool:
        return 0
    rp = cluster.resource_pool[0]
    cpu_allocated = rp.cpu_entitled
    slots = 0
    if (cpu_allocated + req.cpu_size) <= rp.max_allocable_cpu:
        while cpu_allocated < rp.max_allocable_cpu and slots < req.nb_vms:
            cpu_allocated += req.cpu_size
            if cpu_allocated <= rp.max_allocable_cpu:
                slots += 1
    return slots


def cluster_memory_slots(cluster: Cluster, req: PlacementRequest) -> int:
    if not cluster.resource_pool:
        return 0
    rp = cluster.resource_pool[0]
    memory_allocated = rp.memory_entitled
    slots = 0
    if (memory_allocated + req.ram_size) <= rp.max_allocable_memory:
        while memory_allocated < rp.max_allocable_memory and slots < req.nb_vms:
            memory_allocated += req.ram_size
            if memory_allocated <= rp.max_allocable_memory:
                slots += 1
    return slots


def cluster_has_enough_storage(cluster: Cluster, req: PlacementRequest, rules: dict) -> tuple[bool, str]:
    if not cluster.datastore:
        return False, "Aucun datastore utilisable trouvé sur ce cluster"
    ds = cluster.datastore
    usable_space = (ds.capacity * rules["max_usage"]) - (ds.capacity - ds.freespace)
    needed_space = req.storage_size * rules["storage_resiliency"] * req.nb_vms
    if usable_space <= needed_space:
        return False, (
            f"Stockage insuffisant pour l'offre {rules['code']} "
            f"(besoin {needed_space:.1f} Go avec résilience x{rules['storage_resiliency']}, "
            f"dispo utile {usable_space:.1f} Go)"
        )
    return True, ""


def esx_respects_ratios(cluster: Cluster, req: PlacementRequest) -> tuple[bool, str]:
    for host in cluster.hosts:
        cpu_ok = (host.max_allocable_cpu - host.cpu_allocated) >= req.cpu_size
        mem_ok = (host.max_allocable_memory - host.memory_allocated) >= req.ram_size
        if cpu_ok and mem_ok:
            return True, ""
    return False, "Aucun ESX du cluster n'a assez de CPU/RAM libres pour une VM"


def filter_resilient(req: PlacementRequest, inventory: VsphereInventory, rules: dict) -> FilterResponse:
    candidates: list[Cluster] = []
    eliminated: dict[str, str] = {}
    filled_slots = 0

    for cluster in inventory.clusters:
        checks = [
            cluster_matches_naming(cluster, rules),
            cluster_matches_esx_version(cluster, req),
            cluster_has_vm_group(cluster, req),
            cluster_has_enough_storage(cluster, req, rules),
        ]
        failed = next((reason for ok, reason in checks if not ok), None)
        if failed:
            eliminated[cluster.name] = failed
            continue

        if req.gpu_memory:
            if gpu_memory_slots(cluster, req) < req.nb_vms:
                eliminated[cluster.name] = "Pas assez de slots mémoire GPU sur ce cluster"
                continue
            if gpu_allocable_profile_slots(cluster, req) < req.nb_vms:
                eliminated[cluster.name] = f"Le profil GPU '{req.offer_code}' n'est pas allouable sur ce cluster"
                continue

        cpu_slots = cluster_cpu_slots(cluster, req)
        if not cpu_slots:
            eliminated[cluster.name] = "Pas assez de slots CPU sur ce cluster"
            continue
        memory_slots = cluster_memory_slots(cluster, req)
        if not memory_slots:
            eliminated[cluster.name] = "Pas assez de slots mémoire sur ce cluster"
            continue

        cluster.vm_slots = min(cpu_slots, memory_slots, req.nb_vms)
        filled_slots += cluster.vm_slots
        candidates.append(cluster)

    if filled_slots < req.nb_vms:
        return FilterResponse(candidate_clusters=[], eliminated=eliminated)

    candidates.sort(key=lambda c: c.best_availability_score)
    return FilterResponse(candidate_clusters=candidates, eliminated=eliminated)


def filter_non_resilient(req: PlacementRequest, inventory: VsphereInventory, rules: dict) -> FilterResponse:
    eliminated: dict[str, str] = {}
    candidates: list[Cluster] = []

    for cluster in inventory.clusters:
        ok, reason = cluster_matches_naming(cluster, rules)
        if not ok:
            eliminated[cluster.name] = reason
            continue
        ok, reason = cluster_matches_esx_version(cluster, req)
        if not ok:
            eliminated[cluster.name] = reason
            continue
        ok, reason = esx_respects_ratios(cluster, req)
        if not ok:
            eliminated[cluster.name] = reason
            continue
        ok, reason = cluster_has_enough_storage(cluster, req, rules)
        if not ok:
            eliminated[cluster.name] = reason
            continue
        if not cluster.hosts:
            eliminated[cluster.name] = "Aucun ESX sain sur ce cluster"
            continue
        cluster.vm_slots = 1
        cluster.best_availability_score = cluster.hosts[0].best_availability_score
        candidates.append(cluster)

    if req.upgrade_domains:
        if len(req.upgrade_domains) != req.nb_vms:
            return FilterResponse(
                candidate_clusters=[],
                eliminated={"__request__": "Il faut exactement autant d'upgrade domains que de VMs"},
            )
        available_names = {c.name for c in candidates}
        if not set(req.upgrade_domains).issubset(available_names):
            missing = set(req.upgrade_domains) - available_names
            return FilterResponse(
                candidate_clusters=[],
                eliminated={"__request__": f"Upgrade domains indisponibles pour cette demande : {missing}"},
            )
        candidates = [c for c in candidates if c.name in req.upgrade_domains]
        candidates.sort(key=lambda c: req.upgrade_domains.index(c.name))
    else:
        if req.nb_vms > len(candidates):
            return FilterResponse(
                candidate_clusters=[],
                eliminated={**eliminated, "__request__": "Pas assez d'upgrade domains disponibles pour la demande"},
            )
        candidates.sort(key=lambda c: c.best_availability_score)

    return FilterResponse(candidate_clusters=candidates, eliminated=eliminated)


@app.post("/filter", response_model=FilterResponse)
def filter_clusters(payload: FilterRequest):
    req = payload.placement_request
    rules = get_rules_for_request(req)

    if req.target_placement == "esx":
        result = filter_non_resilient(req, payload.inventory, rules)
    else:
        result = filter_resilient(req, payload.inventory, rules)

    logger.info(
        f"Règle {rules['code']} | mode {req.target_placement} | "
        f"{len(result.candidate_clusters)} cluster(s) retenu(s) sur {len(payload.inventory.clusters)}"
    )
    return result
