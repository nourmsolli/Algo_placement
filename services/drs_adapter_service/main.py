"""
main.py - DRS Adapter Service
===============================
Rôle : "Lit la charge réelle des serveurs" — lecture temps réel (quickStats),
sans passer par le cache de l'Inventory Service.
"""

import os
import ssl
import logging
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pyVim import connect
from pyVmomi import vim

from shared.models import Cluster

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("drs_adapter_service")

app = FastAPI(title="DRS Adapter Service", version="1.0.0")


class RealLoadRequest(BaseModel):
    backend_code: str
    candidate_clusters: list[Cluster]


class RealLoadResponse(BaseModel):
    refreshed_clusters: list[Cluster]


def get_backend_credentials(backend_code: str) -> dict:
    prefix = f"VSPHERE_{backend_code.upper()}"
    host = os.getenv(f"{prefix}_HOST")
    user = os.getenv(f"{prefix}_USER")
    password = os.getenv(f"{prefix}_PASSWORD")
    if not all([host, user, password]):
        raise HTTPException(status_code=404, detail=f"Backend '{backend_code}' mal configuré")
    return {"host": host, "user": user, "password": password}


def fetch_realtime_host_stats(host_name: str, si) -> tuple[float, float] | None:
    content = si.RetrieveContent()
    container = content.viewManager.CreateContainerView(content.rootFolder, [vim.HostSystem], True)
    try:
        for host in container.view:
            if host.name == host_name:
                quick_stats = host.summary.quickStats
                total_cpu_mhz = host.summary.hardware.cpuMhz * host.summary.hardware.numCpuCores
                cpu_usage_pct = (quick_stats.overallCpuUsage / total_cpu_mhz * 100) if total_cpu_mhz else 0
                total_mem_mb = host.summary.hardware.memorySize / (1024 ** 2)
                mem_usage_pct = (quick_stats.overallMemoryUsage / total_mem_mb * 100) if total_mem_mb else 0
                return cpu_usage_pct, mem_usage_pct
        return None
    finally:
        container.Destroy()


@app.get("/health")
def health():
    return {"status": "ok", "service": "drs_adapter_service"}


@app.post("/drs/refresh-load", response_model=RealLoadResponse)
def refresh_real_load(payload: RealLoadRequest):
    credentials = get_backend_credentials(payload.backend_code)
    context = ssl._create_unverified_context()
    si = connect.SmartConnect(
        host=credentials["host"], user=credentials["user"], pwd=credentials["password"], sslContext=context
    )
    try:
        for cluster in payload.candidate_clusters:
            for host in cluster.hosts:
                stats = fetch_realtime_host_stats(host.name, si)
                if stats is None:
                    logger.warning(f"Pas de stats temps réel trouvées pour {host.name}, valeurs cache conservées")
                    continue
                cpu_usage, memory_usage = stats
                host.cpu_usage = cpu_usage
                host.memory_usage = memory_usage
                host.best_availability_score = cpu_usage + memory_usage
            if cluster.hosts:
                cluster.hosts.sort(key=lambda h: h.best_availability_score)
                cluster.best_availability_score = cluster.hosts[0].best_availability_score
    except Exception as e:
        logger.error(f"Erreur lecture charge temps réel : {e}")
        raise HTTPException(status_code=502, detail=f"Impossible de lire la charge DRS : {e}")
    finally:
        connect.Disconnect(si)

    return RealLoadResponse(refreshed_clusters=payload.candidate_clusters)
