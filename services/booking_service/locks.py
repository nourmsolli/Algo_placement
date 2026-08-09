"""
locks.py - Booking Service
============================
Verrou de région "une seule demande de placement à la fois", avec deux
implémentations :

  - SQLiteLockBackend : table RegionLock (database.py), valable pour une
    seule instance du Booking Service.
  - RedisLockBackend  : `SET region:<region> <token> NX PX <ttl_ms>`,
    partagé entre plusieurs instances (Redis déjà utilisé par l'Inventory
    Service pour son cache, voir inventory_service/cache.py — même logique
    de sélection automatique via REDIS_URL).

NX garantit que seule une instance obtient le verrou même si plusieurs
tentent de le prendre au même instant (opération atomique côté Redis).
Le relâchement compare le token via WATCH/MULTI (transaction optimiste),
pour ne jamais supprimer le verrou de quelqu'un d'autre repris entre-temps.
"""

import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional


class RegionLockTimeout(TimeoutError):
    pass


class SQLiteLockBackend:
    """Verrou via la table RegionLock (SQLite) — une seule instance du service."""

    def __init__(self, database_module=None):
        # Paramètre injectable surtout pour les tests (qui rechargent
        # database.py sous un nom dédié) ; en usage normal, un simple
        # `import database` reprend le module déjà chargé par main.py.
        if database_module is None:
            import database as database_module
        self._db = database_module

    def try_acquire(self, region: str, ttl_seconds: int) -> Optional[str]:
        return self._db.try_acquire_lock(region, ttl_seconds)

    def release(self, region: str, token: str) -> bool:
        return self._db.release_lock(region, token)

    def list_active(self) -> list[dict]:
        return [
            {"region": lock.region, "token": lock.token, "expires_at": lock.expires_at}
            for lock in self._db.list_active_locks()
        ]


class RedisLockBackend:
    """Verrou partagé via Redis (SET NX PX) — plusieurs instances du service."""

    KEY_PREFIX = "lock:region:"

    def __init__(self, redis_url: Optional[str] = None, client=None):
        if client is not None:
            self.client = client
        else:
            import redis
            self.client = redis.Redis.from_url(redis_url, decode_responses=True)

    def try_acquire(self, region: str, ttl_seconds: int) -> Optional[str]:
        token = str(uuid.uuid4())
        acquired = self.client.set(f"{self.KEY_PREFIX}{region}", token, nx=True, px=int(ttl_seconds * 1000))
        return token if acquired else None

    def release(self, region: str, token: str) -> bool:
        import redis
        key = f"{self.KEY_PREFIX}{region}"
        with self.client.pipeline() as pipe:
            try:
                pipe.watch(key)
                if pipe.get(key) != token:
                    pipe.unwatch()
                    return False
                pipe.multi()
                pipe.delete(key)
                pipe.execute()
                return True
            except redis.WatchError:
                # La clé a changé entre le WATCH et l'EXEC (quelqu'un d'autre
                # a touché ce verrou entre-temps) : on ne supprime rien.
                return False

    def list_active(self) -> list[dict]:
        locks = []
        for key in self.client.scan_iter(match=f"{self.KEY_PREFIX}*"):
            token = self.client.get(key)
            ttl_ms = self.client.pttl(key)
            if not token or ttl_ms is None or ttl_ms < 0:
                continue
            region = key[len(self.KEY_PREFIX):]
            expires_at = datetime.now(timezone.utc) + timedelta(milliseconds=ttl_ms)
            locks.append({"region": region, "token": token, "expires_at": expires_at})
        return locks


def build_lock_backend():
    """Renvoie un RedisLockBackend si REDIS_URL est défini, sinon SQLiteLockBackend."""
    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        return RedisLockBackend(redis_url=redis_url)
    return SQLiteLockBackend()


def acquire_blocking(backend, region: str, ttl_seconds: int = 30, timeout_seconds: int = 15) -> str:
    """Réessaie toutes les 200ms jusqu'à timeout_seconds, quel que soit le backend."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if token := backend.try_acquire(region, ttl_seconds):
            return token
        time.sleep(0.2)
    raise RegionLockTimeout(f"Impossible d'obtenir le verrou pour la région '{region}' après {timeout_seconds}s")
