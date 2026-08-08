"""
main.py - Inventory Service
============================
Rôle : "Scanner VSphere une fois, partage le resultat (cache)"

Le scan utilise PAR DÉFAUT la méthode optimisée (PropertyCollector, voir
vsphere_client.py), configurable via INVENTORY_SCAN_OPTIMIZED ou le
paramètre `optimized` de la requête.
"""

import os
import time
import logging
from fastapi import FastAPI, HTTPException, Query

from shared.models import VsphereInventory
from vsphere_client import VSphereClient
from cache import build_cache

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("inventory_service")

app = FastAPI(title="Inventory Service", version="3.0.0")
cache = build_cache()

DEFAULT_OPTIMIZED = os.getenv("INVENTORY_SCAN_OPTIMIZED", "true").lower() == "true"


def get_backend_credentials(backend_code: str) -> dict:
    prefix = f"VSPHERE_{backend_code.upper()}"
    host = os.getenv(f"{prefix}_HOST")
    user = os.getenv(f"{prefix}_USER")
    password = os.getenv(f"{prefix}_PASSWORD")
    if not all([host, user, password]):
        raise HTTPException(
            status_code=404,
            detail=f"Backend vSphere '{backend_code}' inconnu ou mal configuré (variables {prefix}_* manquantes)",
        )
    return {"host": host, "user": user, "password": password}


@app.get("/health")
def health():
    return {"status": "ok", "service": "inventory_service"}


@app.get("/inventory/{backend_code}", response_model=VsphereInventory)
def get_inventory(
    backend_code: str,
    force_refresh: bool = Query(False, description="Ignore le cache et relance un scan complet"),
    cpu_to_vcpu_ratio: float = Query(8.0, description="Ratio de surallocation CPU (dépend de l'offre)"),
    optimized: bool | None = Query(None, description="true = PropertyCollector (rapide), false = méthode d'origine"),
):
    use_optimized = DEFAULT_OPTIMIZED if optimized is None else optimized
    cache_key = f"inventory:{backend_code}:ratio{cpu_to_vcpu_ratio}:opt{use_optimized}"

    if not force_refresh:
        if cached := cache.get(cache_key):
            logger.info(f"Inventaire '{backend_code}' servi depuis le cache (mode {'optimisé' if use_optimized else 'classique'})")
            return cached

    mode_label = "optimisé (PropertyCollector)" if use_optimized else "classique (appels individuels)"
    logger.info(f"Scan vSphere en cours pour le backend '{backend_code}' [mode {mode_label}]...")

    credentials = get_backend_credentials(backend_code)
    client = VSphereClient(**credentials)
    t0 = time.time()
    try:
        client.connect()
        if use_optimized:
            inventory = client.scan_optimized(cpu_to_vcpu_ratio=cpu_to_vcpu_ratio)
        else:
            inventory = client.scan(cpu_to_vcpu_ratio=cpu_to_vcpu_ratio)
    except Exception as e:
        logger.error(f"Échec du scan vSphere pour '{backend_code}': {e}")
        raise HTTPException(status_code=502, detail=f"Impossible de scanner vSphere : {e}")
    finally:
        client.disconnect()
    elapsed = time.time() - t0

    cache.set(cache_key, inventory)
    logger.info(
        f"Inventaire '{backend_code}' scanné en {elapsed:.2f}s [mode {mode_label}] "
        f"({len(inventory.clusters)} clusters trouvés) et mis en cache"
    )
    return inventory


@app.get("/inventory/{backend_code}/compare-scan-speed")
def compare_scan_speed(backend_code: str, cpu_to_vcpu_ratio: float = Query(8.0)):
    """Lance les DEUX méthodes de scan à la suite (sans cache) et renvoie le
    temps de chacune, pour comparer concrètement (utile pour ton rapport)."""
    credentials = get_backend_credentials(backend_code)
    client = VSphereClient(**credentials)
    client.connect()
    try:
        t0 = time.time()
        inv_classic = client.scan(cpu_to_vcpu_ratio=cpu_to_vcpu_ratio)
        classic_time = time.time() - t0

        t0 = time.time()
        inv_optimized = client.scan_optimized(cpu_to_vcpu_ratio=cpu_to_vcpu_ratio)
        optimized_time = time.time() - t0
    finally:
        client.disconnect()

    return {
        "backend": backend_code,
        "classic_scan_seconds": round(classic_time, 2),
        "optimized_scan_seconds": round(optimized_time, 2),
        "speedup_factor": round(classic_time / optimized_time, 1) if optimized_time > 0 else None,
        "clusters_found_classic": len(inv_classic.clusters),
        "clusters_found_optimized": len(inv_optimized.clusters),
        "results_match": len(inv_classic.clusters) == len(inv_optimized.clusters),
    }


@app.delete("/inventory/{backend_code}/cache")
def invalidate_cache(backend_code: str):
    for opt in (True, False):
        for ratio in (1, 2, 4, 8):
            cache_key = f"inventory:{backend_code}:ratio{ratio}:opt{opt}"
            if hasattr(cache, "_store"):
                cache._store.pop(cache_key, None)
            else:
                cache.client.delete(cache_key)
    return {"status": "cache invalidated", "backend": backend_code}
