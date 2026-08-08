"""
cache.py
========
"Scanner VSphere une fois, partage le resultat (cache)". Deux implémentations :
InMemoryCache (dev, simple dict Python) et RedisCache (partagé, prod). Le
service choisit automatiquement Redis si REDIS_URL est configuré.
"""

import os
import time
from typing import Optional
from shared.models import VsphereInventory

DEFAULT_TTL_SECONDS = int(os.getenv("INVENTORY_CACHE_TTL", "60"))


class InMemoryCache:
    """Cache basique en RAM. Ne fonctionne que pour une seule instance du service."""

    def __init__(self):
        self._store: dict[str, tuple[float, str]] = {}

    def get(self, key: str) -> Optional[VsphereInventory]:
        entry = self._store.get(key)
        if not entry:
            return None
        expires_at, data = entry
        if time.time() > expires_at:
            del self._store[key]
            return None
        return VsphereInventory.model_validate_json(data)

    def set(self, key: str, inventory: VsphereInventory, ttl: int = DEFAULT_TTL_SECONDS) -> None:
        self._store[key] = (time.time() + ttl, inventory.model_dump_json())


class RedisCache:
    """Cache partagé via Redis : plusieurs instances voient le même inventaire."""

    def __init__(self, redis_url: str):
        import redis
        self.client = redis.Redis.from_url(redis_url, decode_responses=True)

    def get(self, key: str) -> Optional[VsphereInventory]:
        data = self.client.get(key)
        if not data:
            return None
        return VsphereInventory.model_validate_json(data)

    def set(self, key: str, inventory: VsphereInventory, ttl: int = DEFAULT_TTL_SECONDS) -> None:
        self.client.set(key, inventory.model_dump_json(), ex=ttl)


def build_cache():
    """Renvoie une RedisCache si REDIS_URL est défini, sinon une InMemoryCache."""
    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        return RedisCache(redis_url)
    return InMemoryCache()
