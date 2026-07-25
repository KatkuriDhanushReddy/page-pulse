"""Cache and rate-limit stores.

Two interchangeable backends:

* ``memory`` — process-local, zero dependencies. Used by tests, local dev and
  single-instance deploys.
* ``mongo``  — shared across replicas, survives restarts. Used in production.

Both implement the same small protocol so the request path never branches.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class CacheStore(Protocol):
    async def get(self, key: str) -> dict[str, Any] | None: ...
    async def set(self, key: str, value: dict[str, Any], ttl_seconds: int) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def purge_expired(self) -> int: ...


class RateLimitStore(Protocol):
    async def hit(
        self, client_id: str, limit: int, window_seconds: int, cost: int = 1
    ) -> tuple[bool, int, int]:
        """Consume ``cost`` units. Returns (allowed, remaining, reset_in_seconds).

        If the client cannot afford ``cost``, nothing is consumed.
        """
        ...

    async def reset(self, client_id: str) -> None: ...


# --------------------------------------------------------------------------- #
# In-memory backend
# --------------------------------------------------------------------------- #


class MemoryCache:
    def __init__(self) -> None:
        self._data: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> dict[str, Any] | None:
        async with self._lock:
            entry = self._data.get(key)
            if not entry:
                return None
            expires_at, value = entry
            if expires_at <= time.time():
                self._data.pop(key, None)
                return None
            return dict(value)

    async def set(self, key: str, value: dict[str, Any], ttl_seconds: int) -> None:
        async with self._lock:
            self._data[key] = (time.time() + ttl_seconds, dict(value))

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._data.pop(key, None)

    async def purge_expired(self) -> int:
        now = time.time()
        async with self._lock:
            stale = [k for k, (expires_at, _) in self._data.items() if expires_at <= now]
            for key in stale:
                self._data.pop(key, None)
            return len(stale)


class MemoryRateLimiter:
    """Fixed window counter. Simple, predictable, and cheap to reason about."""

    def __init__(self) -> None:
        self._windows: dict[str, tuple[float, int]] = {}
        self._lock = asyncio.Lock()

    async def hit(
        self, client_id: str, limit: int, window_seconds: int, cost: int = 1
    ) -> tuple[bool, int, int]:
        if cost < 1:
            raise ValueError("cost must be >= 1")

        now = time.time()
        async with self._lock:
            window_start, count = self._windows.get(client_id, (now, 0))

            if now - window_start >= window_seconds:
                window_start, count = now, 0

            reset_in = max(0, int(window_seconds - (now - window_start)))

            if count + cost > limit:
                self._windows[client_id] = (window_start, count)
                return False, max(0, limit - count), reset_in

            count += cost
            self._windows[client_id] = (window_start, count)
            return True, max(0, limit - count), reset_in

    async def reset(self, client_id: str) -> None:
        async with self._lock:
            self._windows.pop(client_id, None)


# --------------------------------------------------------------------------- #
# MongoDB backend
# --------------------------------------------------------------------------- #


class MongoCache:
    def __init__(self, db: Any) -> None:
        self._collection = db["audit_cache"]

    async def ensure_indexes(self) -> None:
        # Mongo's TTL monitor reclaims documents; the explicit expiry check in
        # get() keeps correctness even before the monitor runs.
        await self._collection.create_index("expires_at", expireAfterSeconds=0)

    async def get(self, key: str) -> dict[str, Any] | None:
        doc = await self._collection.find_one({"_id": key})
        if not doc:
            return None
        if doc.get("expires_at") and doc["expires_at"].timestamp() <= time.time():
            await self.delete(key)
            return None
        return doc.get("value")

    async def set(self, key: str, value: dict[str, Any], ttl_seconds: int) -> None:
        from datetime import datetime, timedelta

        await self._collection.update_one(
            {"_id": key},
            {
                "$set": {
                    "value": value,
                    "expires_at": datetime.now(UTC) + timedelta(seconds=ttl_seconds),
                }
            },
            upsert=True,
        )

    async def delete(self, key: str) -> None:
        await self._collection.delete_one({"_id": key})

    async def purge_expired(self) -> int:
        from datetime import datetime

        result = await self._collection.delete_many({"expires_at": {"$lte": datetime.now(UTC)}})
        return int(result.deleted_count)


class MongoRateLimiter:
    def __init__(self, db: Any) -> None:
        self._collection = db["rate_limits"]

    async def hit(
        self, client_id: str, limit: int, window_seconds: int, cost: int = 1
    ) -> tuple[bool, int, int]:
        from pymongo import ReturnDocument

        if cost < 1:
            raise ValueError("cost must be >= 1")

        now = time.time()
        doc = await self._collection.find_one({"_id": client_id})

        if doc is None or now - float(doc["window_start"]) >= window_seconds:
            if cost > limit:
                return False, limit, window_seconds
            await self._collection.update_one(
                {"_id": client_id},
                {"$set": {"window_start": now, "count": cost}},
                upsert=True,
            )
            return True, max(0, limit - cost), window_seconds

        window_start = float(doc["window_start"])
        count = int(doc["count"])
        reset_in = max(0, int(window_seconds - (now - window_start)))

        if count + cost > limit:
            return False, max(0, limit - count), reset_in

        # Atomic compare-and-increment so concurrent replicas cannot overshoot.
        updated = await self._collection.find_one_and_update(
            {"_id": client_id, "count": {"$lte": limit - cost}},
            {"$inc": {"count": cost}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is None:
            return False, max(0, limit - count), reset_in

        new_count = int(updated["count"])
        return True, max(0, limit - new_count), reset_in

    async def reset(self, client_id: str) -> None:
        await self._collection.delete_one({"_id": client_id})
