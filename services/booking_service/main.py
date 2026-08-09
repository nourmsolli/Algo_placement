"""
main.py - Booking Service
==========================
Rôle : "Gère les résérvations recentes (<2h)" + verrou anti-surallocation.
"""

import logging
from datetime import datetime
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from shared.models import Cluster
from database import init_db, create_booking, get_recent_bookings, get_all_bookings, Booking
from locks import build_lock_backend, acquire_blocking, RegionLockTimeout

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("booking_service")

app = FastAPI(title="Booking Service", version="1.0.0")
lock_backend = build_lock_backend()


@app.on_event("startup")
def on_startup():
    init_db()


class BookingCreate(BaseModel):
    region: str
    cluster_name: str
    host_name: str | None = None
    cpu_booked: float
    memory_booked: float
    storage_booked: float


class BookingOut(BaseModel):
    id: int
    region: str
    cluster_name: str
    host_name: str | None
    cpu_booked: float
    memory_booked: float
    storage_booked: float
    created_at: datetime


class LockOut(BaseModel):
    region: str
    token: str
    expires_at: datetime


class DeductRequest(BaseModel):
    region: str
    candidate_clusters: list[Cluster]


class DeductResponse(BaseModel):
    adjusted_clusters: list[Cluster]


class LockRequest(BaseModel):
    region: str
    ttl_seconds: int = 30
    timeout_seconds: int = 15


class LockResponse(BaseModel):
    token: str


class ReleaseRequest(BaseModel):
    region: str
    token: str


@app.get("/health")
def health():
    return {"status": "ok", "service": "booking_service"}


@app.post("/bookings", response_model=BookingOut)
def add_booking(payload: BookingCreate):
    booking = Booking(**payload.model_dump())
    saved = create_booking(booking)
    logger.info(f"Nouvelle réservation enregistrée sur {saved.cluster_name} ({saved.host_name or 'cluster entier'})")
    return BookingOut(**saved.model_dump())


@app.get("/bookings", response_model=list[BookingOut])
def list_all_bookings(limit: int = 200):
    """Historique des réservations toutes régions confondues, pour l'admin."""
    bookings = get_all_bookings(limit)
    return [BookingOut(**b.model_dump()) for b in bookings]


@app.get("/locks", response_model=list[LockOut])
def list_locks():
    """Verrous de région actuellement actifs, pour l'admin."""
    return [LockOut(**lock) for lock in lock_backend.list_active()]


@app.get("/bookings/{region}", response_model=list[BookingOut])
def list_recent_bookings(region: str):
    bookings = get_recent_bookings(region)
    return [BookingOut(**b.model_dump()) for b in bookings]


@app.post("/bookings/deduct", response_model=DeductResponse)
def deduct_bookings(payload: DeductRequest):
    recent_bookings = get_recent_bookings(payload.region)
    bookings_by_cluster: dict[str, list[Booking]] = {}
    for b in recent_bookings:
        bookings_by_cluster.setdefault(b.cluster_name, []).append(b)

    adjusted: list[Cluster] = []
    for cluster in payload.candidate_clusters:
        cluster_bookings = bookings_by_cluster.get(cluster.name, [])
        for booking in cluster_bookings:
            if booking.host_name:
                for host in cluster.hosts:
                    if host.name == booking.host_name:
                        host.cpu_allocated += booking.cpu_booked
                        host.memory_allocated += booking.memory_booked
            else:
                for rp in cluster.resource_pool:
                    rp.cpu_entitled += booking.cpu_booked
                    rp.memory_entitled += booking.memory_booked

        if cluster_bookings:
            for host in cluster.hosts:
                if host.max_allocable_cpu:
                    host.cpu_usage = host.cpu_allocated * 100 / host.max_allocable_cpu
                if host.max_allocable_memory:
                    host.memory_usage = host.memory_allocated * 100 / host.max_allocable_memory
                host.best_availability_score = host.cpu_usage + host.memory_usage
            cluster.hosts.sort(key=lambda h: h.best_availability_score)
            for rp in cluster.resource_pool:
                if rp.max_allocable_cpu:
                    rp.cpu_usage = rp.cpu_entitled * 100 / rp.max_allocable_cpu
                if rp.max_allocable_memory:
                    rp.memory_usage = rp.memory_entitled * 100 / rp.max_allocable_memory
                rp.best_availability_score = rp.cpu_usage + rp.memory_usage
                cluster.best_availability_score = rp.best_availability_score
            if not cluster.resource_pool and cluster.hosts:
                cluster.best_availability_score = cluster.hosts[0].best_availability_score
        adjusted.append(cluster)

    return DeductResponse(adjusted_clusters=adjusted)


@app.post("/locks/acquire", response_model=LockResponse)
def acquire_lock(payload: LockRequest):
    try:
        token = acquire_blocking(lock_backend, payload.region, payload.ttl_seconds, payload.timeout_seconds)
    except RegionLockTimeout as e:
        raise HTTPException(status_code=409, detail=str(e))
    logger.info(f"Verrou acquis pour la région {payload.region}")
    return LockResponse(token=token)


@app.post("/locks/release")
def release_lock_route(payload: ReleaseRequest):
    released = lock_backend.release(payload.region, payload.token)
    logger.info(f"Verrou relâché pour la région {payload.region}: {released}")
    return {"released": released}
