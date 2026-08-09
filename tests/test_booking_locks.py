"""
Tests du verrou de région : backend SQLite (par défaut, une seule instance)
et backend Redis (SET NX PX, pour plusieurs instances). Le backend Redis est
testé via fakeredis, pour ne pas dépendre d'un vrai serveur Redis en CI.
"""

import os
import time
import fakeredis
import pytest

from conftest import load_service_module


@pytest.fixture(scope="module")
def booking_modules(tmp_path_factory):
    tmp_dir = tmp_path_factory.mktemp("booking_db")
    os.environ["BOOKING_DATABASE_URL"] = f"sqlite:///{tmp_dir}/test_bookings.db"
    os.environ.pop("REDIS_URL", None)
    database = load_service_module("booking_service", "database.py")
    database.init_db()
    locks = load_service_module("booking_service", "locks.py")
    return database, locks


@pytest.fixture
def booking_locks(booking_modules):
    return booking_modules[1]


# --- Backend SQLite (une seule instance) ------------------------------------

def test_sqlite_backend_second_acquire_is_blocked(booking_modules):
    database, locks = booking_modules
    backend = locks.SQLiteLockBackend(database_module=database)
    token = backend.try_acquire("TEST-REGION-SQLITE-1", ttl_seconds=5)
    assert token is not None
    assert backend.try_acquire("TEST-REGION-SQLITE-1", ttl_seconds=5) is None


def test_sqlite_backend_release_frees_the_region(booking_modules):
    database, locks = booking_modules
    backend = locks.SQLiteLockBackend(database_module=database)
    token = backend.try_acquire("TEST-REGION-SQLITE-2", ttl_seconds=5)
    assert backend.release("TEST-REGION-SQLITE-2", token) is True
    assert backend.try_acquire("TEST-REGION-SQLITE-2", ttl_seconds=5) is not None


def test_sqlite_backend_release_wrong_token_fails(booking_modules):
    database, locks = booking_modules
    backend = locks.SQLiteLockBackend(database_module=database)
    backend.try_acquire("TEST-REGION-SQLITE-3", ttl_seconds=5)
    assert backend.release("TEST-REGION-SQLITE-3", "un-mauvais-token") is False


# --- Backend Redis (SET NX PX, plusieurs instances) --------------------------

def test_redis_backend_nx_blocks_second_acquire(booking_locks):
    backend = booking_locks.RedisLockBackend(client=fakeredis.FakeStrictRedis(decode_responses=True))
    token = backend.try_acquire("EU-WEST", ttl_seconds=5)
    assert token is not None
    assert backend.try_acquire("EU-WEST", ttl_seconds=5) is None


def test_redis_backend_release_only_with_own_token(booking_locks):
    backend = booking_locks.RedisLockBackend(client=fakeredis.FakeStrictRedis(decode_responses=True))
    token = backend.try_acquire("EU-SOUTH", ttl_seconds=5)
    assert backend.release("EU-SOUTH", "mauvais-token") is False
    assert backend.release("EU-SOUTH", token) is True
    assert backend.try_acquire("EU-SOUTH", ttl_seconds=5) is not None


def test_redis_backend_ttl_expiry_frees_the_region(booking_locks):
    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    backend = booking_locks.RedisLockBackend(client=fake)
    backend.try_acquire("EU-NORTH", ttl_seconds=1)
    fake.pexpire(f"{booking_locks.RedisLockBackend.KEY_PREFIX}EU-NORTH", 1)
    time.sleep(0.05)
    assert backend.try_acquire("EU-NORTH", ttl_seconds=5) is not None


def test_redis_backend_list_active_reflects_ttl(booking_locks):
    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    backend = booking_locks.RedisLockBackend(client=fake)
    backend.try_acquire("AP-SOUTHEAST", ttl_seconds=30)
    active = backend.list_active()
    assert any(lock["region"] == "AP-SOUTHEAST" for lock in active)


# --- Helper générique acquire_blocking (indépendant du backend) --------------

def test_acquire_blocking_raises_timeout_when_already_locked(booking_locks):
    backend = booking_locks.RedisLockBackend(client=fakeredis.FakeStrictRedis(decode_responses=True))
    backend.try_acquire("TIMEOUT-REGION", ttl_seconds=5)
    with pytest.raises(booking_locks.RegionLockTimeout):
        booking_locks.acquire_blocking(backend, "TIMEOUT-REGION", ttl_seconds=5, timeout_seconds=1)
