"""
database.py - Booking Service
==============================
Gère les réservations récentes (<2h) pour éviter la surallocation entre le
moment du scan et la création réelle de la VM, ET un verrou par région pour
empêcher deux demandes simultanées de choisir le même serveur.
"""

import os
import uuid
from datetime import datetime, timedelta
from sqlmodel import SQLModel, Field, create_engine, Session, select
from typing import Optional

DATABASE_URL = os.getenv("BOOKING_DATABASE_URL", "sqlite:///./bookings.db")
engine = create_engine(DATABASE_URL, echo=False)


class Booking(SQLModel, table=True):
    """Une réservation temporaire de ressources sur un cluster ou un ESX précis."""
    id: Optional[int] = Field(default=None, primary_key=True)
    region: str
    cluster_name: str
    host_name: Optional[str] = None
    cpu_booked: float = 0.0
    memory_booked: float = 0.0
    storage_booked: float = 0.0
    created_at: datetime = Field(default_factory=datetime.utcnow)


class RegionLock(SQLModel, table=True):
    """
    Verrou applicatif "une seule demande de placement à la fois par région",
    avec expiration automatique (TTL) pour éviter qu'un verrou reste bloqué
    si l'Orchestrateur crashe avant de le relâcher.
    """
    region: str = Field(primary_key=True)
    token: str
    expires_at: datetime


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


def create_booking(booking: Booking) -> Booking:
    with Session(engine) as session:
        session.add(booking)
        session.commit()
        session.refresh(booking)
        return booking


def get_recent_bookings(region: str, max_age_hours: int = 2) -> list[Booking]:
    threshold = datetime.utcnow() - timedelta(hours=max_age_hours)
    with Session(engine) as session:
        statement = select(Booking).where(Booking.region == region, Booking.created_at > threshold)
        return list(session.exec(statement))


def get_all_bookings(limit: int = 200) -> list[Booking]:
    """Historique des réservations, toutes régions confondues (pour l'admin)."""
    with Session(engine) as session:
        statement = select(Booking).order_by(Booking.created_at.desc()).limit(limit)
        return list(session.exec(statement))


def list_active_locks() -> list[RegionLock]:
    """Verrous de région actuellement valides (pour l'admin)."""
    now = datetime.utcnow()
    with Session(engine) as session:
        statement = select(RegionLock).where(RegionLock.expires_at > now)
        return list(session.exec(statement))


def delete_old_bookings(max_age_hours: int = 2) -> int:
    threshold = datetime.utcnow() - timedelta(hours=max_age_hours)
    with Session(engine) as session:
        statement = select(Booking).where(Booking.created_at <= threshold)
        old_bookings = list(session.exec(statement))
        for b in old_bookings:
            session.delete(b)
        session.commit()
        return len(old_bookings)


# --- Verrou par région (backend SQLite, voir locks.py pour le backend Redis) -------

def try_acquire_lock(region: str, ttl_seconds: int = 30) -> Optional[str]:
    """Tente de prendre le verrou d'une région, en une transaction atomique."""
    now = datetime.utcnow()
    token = str(uuid.uuid4())
    with Session(engine) as session:
        existing = session.get(RegionLock, region)
        if existing and existing.expires_at > now:
            return None
        if existing:
            session.delete(existing)
            session.commit()
        lock = RegionLock(region=region, token=token, expires_at=now + timedelta(seconds=ttl_seconds))
        session.add(lock)
        session.commit()
        return token


def release_lock(region: str, token: str) -> bool:
    with Session(engine) as session:
        existing = session.get(RegionLock, region)
        if existing and existing.token == token:
            session.delete(existing)
            session.commit()
            return True
        return False
