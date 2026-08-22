"""Short-TTL read-through cache in CACHE.get_player() (2026-08-22).

get_player() lived in cache_manager.py but was completely uncached — every call was a live API
round-trip via coc_retry, across ~10 call sites. The cache collapses bursts and repeat commands;
registration/refresh paths opt out with force_fresh=True because the values they capture become
persisted account state.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def cache(monkeypatch):
    """A CacheManager with a stubbed CoC client, and the call count it has served."""
    from qapbot.cache_manager import CacheManager

    cm = CacheManager()
    calls = {"n": 0}

    async def _get_player(tag):
        calls["n"] += 1
        p = MagicMock()
        p.tag = tag
        p.name = f"Player{calls['n']}"
        return p

    client = MagicMock()
    client.get_player = _get_player
    cm.coc_client = client
    cm._calls = calls  # type: ignore[attr-defined]
    return cm


class TestPlayerCacheHits:
    @pytest.mark.asyncio
    async def test_second_call_within_ttl_is_served_from_cache(self, cache):
        first = await cache.get_player("#ABC123")
        second = await cache.get_player("#ABC123")

        assert cache._calls["n"] == 1, "second call hit the API instead of the cache"
        assert second is first

    @pytest.mark.asyncio
    async def test_distinct_tags_are_cached_independently(self, cache):
        await cache.get_player("#AAA111")
        await cache.get_player("#BBB222")
        await cache.get_player("#AAA111")

        assert cache._calls["n"] == 2

    @pytest.mark.asyncio
    async def test_tag_is_normalized_before_lookup(self, cache):
        """Cache keys are normalized tags, so the same player written differently must hit."""
        await cache.get_player("#ABC123")
        await cache.get_player("abc123")

        assert cache._calls["n"] == 1

    @pytest.mark.asyncio
    async def test_entry_past_ttl_is_refetched(self, cache, monkeypatch):
        await cache.get_player("#ABC123")
        # Age the entry past the TTL rather than sleeping.
        obj, ts = cache._player_cache["#ABC123"]
        cache._player_cache["#ABC123"] = (obj, ts - cache._PLAYER_CACHE_TTL - 1)

        await cache.get_player("#ABC123")
        assert cache._calls["n"] == 2


class TestForceFresh:
    @pytest.mark.asyncio
    async def test_force_fresh_bypasses_a_warm_entry(self, cache):
        await cache.get_player("#ABC123")
        await cache.get_player("#ABC123", force_fresh=True)

        assert cache._calls["n"] == 2

    @pytest.mark.asyncio
    async def test_force_fresh_replaces_the_cached_entry(self, cache):
        """Not just a bypass — the refreshed value must become what later cached reads see,
        otherwise a registration would re-fetch and then hand the NEXT reader the stale object."""
        await cache.get_player("#ABC123")
        fresh = await cache.get_player("#ABC123", force_fresh=True)
        later = await cache.get_player("#ABC123")

        assert later is fresh
        assert cache._calls["n"] == 2


class TestFailuresAreNotCached:
    @pytest.mark.asyncio
    async def test_failed_fetch_is_not_cached(self, cache, monkeypatch):
        """A failure that survives coc_retry must not become a minute of sticky failure — that
        would break a registration attempt retried immediately afterwards. Patches coc_retry
        itself rather than the client, so the test exercises the give-up path directly instead
        of waiting out the real retry backoff."""
        import qapbot.cache_manager as cache_manager_module

        async def _always_fails(operation, operation_name=""):
            raise RuntimeError("exhausted")

        monkeypatch.setattr(cache_manager_module, "coc_retry", _always_fails)

        assert await cache.get_player("#ABC123") is None
        assert "#ABC123" not in cache._player_cache

    @pytest.mark.asyncio
    async def test_recovery_after_a_failure_is_not_blocked_by_the_cache(self, cache, monkeypatch):
        """The point of not caching failures: the very next call must be able to succeed."""
        import qapbot.cache_manager as cache_manager_module

        real_coc_retry = cache_manager_module.coc_retry

        async def _fails_once(operation, operation_name=""):
            raise RuntimeError("exhausted")

        monkeypatch.setattr(cache_manager_module, "coc_retry", _fails_once)
        assert await cache.get_player("#ABC123") is None

        monkeypatch.setattr(cache_manager_module, "coc_retry", real_coc_retry)
        recovered = await cache.get_player("#ABC123")
        assert recovered is not None

    @pytest.mark.asyncio
    async def test_invalid_tag_returns_none_without_touching_the_api(self, cache):
        assert await cache.get_player("not-a-tag") is None
        assert cache._calls["n"] == 0


class TestBounds:
    @pytest.mark.asyncio
    async def test_size_cap_is_enforced_on_insert(self, cache, monkeypatch):
        """The periodic sweep runs once per update cycle — far too coarse to bound a single
        command that fetches dozens of tags back to back, so the cap applies at insert time."""
        monkeypatch.setattr(type(cache), "_PLAYER_CACHE_MAX_ENTRIES", 5)

        for i in range(20):
            await cache.get_player(f"#TAG{i:04d}")

        assert len(cache._player_cache) <= 5

    @pytest.mark.asyncio
    async def test_size_cap_evicts_oldest_first(self, cache, monkeypatch):
        monkeypatch.setattr(type(cache), "_PLAYER_CACHE_MAX_ENTRIES", 3)

        for i in range(3):
            await cache.get_player(f"#TAG{i:04d}")
        await cache.get_player("#TAG0009")

        assert "#TAG0000" not in cache._player_cache, "oldest entry survived eviction"
        assert "#TAG0009" in cache._player_cache

    @pytest.mark.asyncio
    async def test_evict_stale_player_cache_purges_only_expired_entries(self, cache):
        await cache.get_player("#LDD001")
        await cache.get_player("#NEW111")
        obj, ts = cache._player_cache["#LDD001"]
        cache._player_cache["#LDD001"] = (obj, ts - cache._PLAYER_CACHE_TTL - 1)

        cache.evict_stale_player_cache()

        assert "#LDD001" not in cache._player_cache
        assert "#NEW111" in cache._player_cache

    @pytest.mark.asyncio
    async def test_evict_is_safe_on_an_empty_cache(self, cache):
        cache.evict_stale_player_cache()  # must not raise
        assert cache._player_cache == {}


class TestConcurrentBurst:
    @pytest.mark.asyncio
    async def test_concurrent_misses_both_resolve_correctly(self, cache):
        """There is deliberately no stampede lock (a redundant idempotent GET is cheaper than an
        unbounded per-tag lock dict). Both callers must still get a valid object, and the cache
        must be left in a consistent state."""
        async def _slow(tag):
            await asyncio.sleep(0.01)
            p = MagicMock()
            p.name = "Same"
            return p

        cache.coc_client.get_player = _slow

        a, b = await asyncio.gather(cache.get_player("#ABC123"), cache.get_player("#ABC123"))

        assert a is not None and b is not None
        assert len(cache._player_cache) == 1
        assert await cache.get_player("#ABC123") is cache._player_cache["#ABC123"][0]
