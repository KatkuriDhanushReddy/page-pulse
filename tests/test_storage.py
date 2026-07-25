"""Cache TTL and rate-limit window semantics."""

from __future__ import annotations

import asyncio

import pytest

from app.storage import MemoryCache, MemoryRateLimiter

pytestmark = pytest.mark.asyncio


async def test_cache_miss_returns_none():
    cache = MemoryCache()
    assert await cache.get("absent") is None


async def test_cache_roundtrip_returns_a_copy():
    cache = MemoryCache()
    payload = {"status_code": 200}
    await cache.set("k", payload, ttl_seconds=60)

    stored = await cache.get("k")
    assert stored == {"status_code": 200}

    stored["status_code"] = 500  # mutating the caller's copy must not poison the cache
    assert (await cache.get("k"))["status_code"] == 200


async def test_cache_entry_expires():
    cache = MemoryCache()
    await cache.set("k", {"v": 1}, ttl_seconds=0)
    await asyncio.sleep(0.01)
    assert await cache.get("k") is None


async def test_cache_overwrite_and_delete():
    cache = MemoryCache()
    await cache.set("k", {"v": 1}, ttl_seconds=60)
    await cache.set("k", {"v": 2}, ttl_seconds=60)
    assert (await cache.get("k"))["v"] == 2

    await cache.delete("k")
    assert await cache.get("k") is None


async def test_purge_expired_only_removes_stale_entries():
    cache = MemoryCache()
    await cache.set("fresh", {"v": 1}, ttl_seconds=60)
    for i in range(3):
        await cache.set(f"stale-{i}", {"v": i}, ttl_seconds=0)
    await asyncio.sleep(0.01)

    assert await cache.purge_expired() == 3
    assert await cache.get("fresh") is not None


async def test_rate_limiter_allows_up_to_the_limit():
    limiter = MemoryRateLimiter()
    for expected_remaining in (4, 3, 2, 1, 0):
        allowed, remaining, _ = await limiter.hit("client", limit=5, window_seconds=60)
        assert allowed is True
        assert remaining == expected_remaining


async def test_rate_limiter_blocks_beyond_the_limit():
    limiter = MemoryRateLimiter()
    for _ in range(5):
        await limiter.hit("client", limit=5, window_seconds=60)

    allowed, remaining, reset_in = await limiter.hit("client", limit=5, window_seconds=60)
    assert allowed is False
    assert remaining == 0
    assert 0 <= reset_in <= 60


async def test_rate_limiter_rejected_cost_consumes_nothing():
    """A batch that does not fit must not burn the remaining budget."""
    limiter = MemoryRateLimiter()
    await limiter.hit("client", limit=5, window_seconds=60, cost=3)

    allowed, remaining, _ = await limiter.hit("client", limit=5, window_seconds=60, cost=3)
    assert allowed is False
    assert remaining == 2

    # The 2 remaining units are still usable.
    allowed, remaining, _ = await limiter.hit("client", limit=5, window_seconds=60, cost=2)
    assert allowed is True
    assert remaining == 0


async def test_rate_limiter_window_rolls_over():
    limiter = MemoryRateLimiter()
    await limiter.hit("client", limit=1, window_seconds=0)
    allowed, _, _ = await limiter.hit("client", limit=1, window_seconds=0)
    assert allowed is True


async def test_rate_limiter_is_per_client():
    limiter = MemoryRateLimiter()
    await limiter.hit("noisy", limit=1, window_seconds=60)
    blocked, _, _ = await limiter.hit("noisy", limit=1, window_seconds=60)
    allowed, _, _ = await limiter.hit("quiet", limit=1, window_seconds=60)

    assert blocked is False
    assert allowed is True


async def test_rate_limiter_reset_clears_the_window():
    limiter = MemoryRateLimiter()
    await limiter.hit("client", limit=1, window_seconds=60)
    await limiter.reset("client")

    allowed, remaining, _ = await limiter.hit("client", limit=1, window_seconds=60)
    assert allowed is True
    assert remaining == 0


async def test_rate_limiter_is_safe_under_concurrency():
    limiter = MemoryRateLimiter()
    results = await asyncio.gather(*(limiter.hit("burst", limit=10, window_seconds=60) for _ in range(50)))
    assert sum(1 for allowed, _, _ in results if allowed) == 10
