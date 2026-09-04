"""Tests for coc_clan_cache's protected-tag tier (exempt from size-cap eviction).

Why this exists: the Phase-1 polling loop streams tens of thousands of distinct clans
through CoCClanCache per day and re-reads none of them — its own last_checked_via_api gate
is 12h, far longer than the cache's 600s TTL, so it structurally cannot hit. Under a plain
FIFO that flood evicted the small population user commands DO ask about within seconds of
caching them, so the cache held only the clans that could never hit it.

The invariant under test: a clan that user-facing commands care about survives arbitrary
polling pressure, while the cap still bounds memory in every case.
"""
# pyright: reportPrivateUsage=false, reportMissingParameterType=false, reportUnknownMemberType=false
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Set
from unittest.mock import MagicMock

import pytest

from qapbot.coc_cache import CoCClanCache


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _fill(cache: CoCClanCache, tags: list[str]) -> None:
    for t in tags:
        cache.cache[t] = {"data": t, "timestamp": _now()}
        cache._evict_over_cap()


class TestProtectedEviction:
    def test_protected_survive_a_polling_flood(self) -> None:
        """The regression: 3000 polled clans must not displace the protected ones."""
        cache = CoCClanCache(max_entries=1500)
        protected = {f"#P{i}" for i in range(122)}
        cache.protected_tags = set(protected)
        _fill(cache, sorted(protected))
        _fill(cache, [f"#F{i}" for i in range(3000)])

        assert protected <= set(cache.cache), "a protected clan was evicted by the flood"
        assert len(cache.cache) <= 1500

    def test_cap_is_still_enforced_when_everything_is_protected(self) -> None:
        """Memory must stay bounded even if the protected set exceeds the cap."""
        cache = CoCClanCache(max_entries=50)
        protected = {f"#P{i}" for i in range(122)}
        cache.protected_tags = set(protected)
        _fill(cache, sorted(protected))

        assert len(cache.cache) == 50

    def test_no_protected_set_is_plain_fifo(self) -> None:
        """Inert until opted in: with no protected set this is the pre-change FIFO.

        Matters for bot startup (the set is only populated in main()), for a failed
        refresh_protected_clan_tags() call, and for the standalone scripts that build a
        CoCClanCache of their own and never set it.
        """
        cache = CoCClanCache(max_entries=10)
        _fill(cache, [f"#X{i}" for i in range(20)])

        assert len(cache.cache) == 10
        assert "#X0" not in cache.cache          # oldest gone
        assert "#X19" in cache.cache             # newest kept

    def test_protection_does_not_extend_ttl(self) -> None:
        """Protection changes WHICH entries survive pressure, never how stale one may get."""
        cache = CoCClanCache(soft_ttl_seconds=280, hard_ttl_seconds=600)
        cache.protected_tags = {"#P1"}
        cache.cache["#P1"] = {"data": "x", "timestamp": _now() - dt.timedelta(seconds=900)}

        assert cache.clear_expired() == 1
        assert "#P1" not in cache.cache


class TestRefreshProtectedClanTags:
    def _mgr(self) -> Any:
        from qapbot.cache_manager import CacheManager

        mgr = CacheManager.__new__(CacheManager)
        mgr.subscriptions = {"G1": {"C1": [{"clan_tag": "#SUB1"}, {"clan_tag": "#SUB2"}]}}
        mgr.clan_families = {"#FAM": {"name": "Fam", "clans": ["#MEM1", "#MEM2"]}}
        mgr.temp_war_metadata = {"#SUB1": {"opponent_tag": "#OPP1"}}
        mgr.coc_clan_cache = CoCClanCache()
        return mgr

    def test_collects_subscribed_family_opponent_and_extras(self) -> None:
        mgr = self._mgr()
        n = mgr.refresh_protected_clan_tags(["#CWL1"])

        tags: Set[str] = mgr.coc_clan_cache.protected_tags
        assert {"#SUB1", "#SUB2", "#MEM1", "#MEM2", "#CWL1", "#OPP1"} <= tags
        assert n == len(tags)

    def test_opponent_of_a_cwl_group_member_is_included(self) -> None:
        """Opponents are resolved for everything protected, extras included."""
        mgr = self._mgr()
        mgr.temp_war_metadata["#CWL1"] = {"opponent_tag": "#OPP2"}
        mgr.refresh_protected_clan_tags(["#CWL1"])

        assert "#OPP2" in mgr.coc_clan_cache.protected_tags

    def test_recompute_replaces_rather_than_accumulates(self) -> None:
        """A clan that stops being subscribed must stop being protected."""
        mgr = self._mgr()
        mgr.refresh_protected_clan_tags()
        assert "#SUB1" in mgr.coc_clan_cache.protected_tags

        mgr.subscriptions = {}
        mgr.clan_families = {}
        mgr.refresh_protected_clan_tags()
        assert "#SUB1" not in mgr.coc_clan_cache.protected_tags

    def test_degrades_on_a_partially_constructed_manager(self) -> None:
        """Test fixtures build CacheManager via __new__; this must not raise into a cycle."""
        from qapbot.cache_manager import CacheManager

        bare = CacheManager.__new__(CacheManager)
        assert bare.refresh_protected_clan_tags() == 0

    def test_no_extras_is_fine(self) -> None:
        mgr = self._mgr()
        assert mgr.refresh_protected_clan_tags(None) > 0


class TestStoreResult:
    """get_clan(store_result=False): skip the WRITE, never the read.

    The Phase-1 polling path re-checks a given clan at most every 12h (30min for role
    clans), far beyond the 600s TTL, so it can never re-read what it caches — while
    streaming ~55K distinct clans/day through it. Storing those costs memory and eviction
    churn to serve nobody. Reads stay enabled so this can never cost an extra API call.
    """

    @staticmethod
    def _cache(monkeypatch: pytest.MonkeyPatch) -> tuple[CoCClanCache, list[int]]:
        import qapbot.coc_cache as mod
        from unittest.mock import AsyncMock

        async def _retry(f: Any, **_k: Any) -> Any:
            return await f()

        monkeypatch.setattr(mod, "coc_retry", _retry)
        c = CoCClanCache(max_entries=1500)
        c.cache_manager = MagicMock()
        c.cache_manager.coc_client = MagicMock()
        c._update_clan_metadata = AsyncMock()
        calls = [0]

        async def _api(_tag: str) -> Any:
            calls[0] += 1
            return MagicMock()

        c.cache_manager.coc_client.get_clan = _api
        return c, calls

    @pytest.mark.asyncio
    async def test_polling_fetches_are_not_stored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cache, calls = self._cache(monkeypatch)
        for i in range(300):
            await cache.get_clan(f"#F{i}", store_result=False)

        assert len(cache.cache) == 0
        assert cache.evicted_by_cap == 0, "nothing stored, so nothing should be evicted"
        assert calls[0] == 300

    @pytest.mark.asyncio
    async def test_stored_result_serves_a_second_ask(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cache, calls = self._cache(monkeypatch)
        await cache.get_clan("#P1", store_result=True)
        await cache.get_clan("#P1")

        assert calls[0] == 1, "second ask should have been served from cache"
        assert "#P1" in cache.cache

    @pytest.mark.asyncio
    async def test_store_result_false_still_reads_the_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The key safety property: skipping the write must never cost an extra API call."""
        cache, calls = self._cache(monkeypatch)
        await cache.get_clan("#P1", store_result=True)
        before = calls[0]
        await cache.get_clan("#P1", store_result=False)

        assert calls[0] == before

    @pytest.mark.asyncio
    async def test_interactive_default_still_caches_any_clan(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Commands/UI must keep caching arbitrary clans — they are why this cache exists."""
        cache, calls = self._cache(monkeypatch)
        await cache.get_clan("#RANDOM")
        await cache.get_clan("#RANDOM")

        assert calls[0] == 1
        assert "#RANDOM" in cache.cache


class TestPerPopulationCounters:
    """Hit rate must be reported per population, not blended.

    The point of instrumenting this cache is to learn WHICH clans get re-read: the ~120
    user-facing ones, or the tens of thousands the poll loop streams. A single blended
    number cannot answer that — the larger population swamps the smaller — and acting on a
    blended number is how an assumption gets mistaken for a measurement.
    """

    @pytest.mark.asyncio
    async def test_protected_and_other_are_counted_separately(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache, _calls = TestStoreResult._cache(monkeypatch)
        cache.protected_tags = {"#P1"}

        await cache.get_clan("#P1")            # miss  (protected)
        await cache.get_clan("#P1")            # hit   (protected)
        for i in range(5):
            await cache.get_clan(f"#O{i}")     # 5 misses (other)
        await cache.get_clan("#O0")            # hit   (other)

        st = cache.get_stats()
        p_served = st["hits_protected"] + st["stale_hits_protected"]
        p_total = p_served + st["misses_protected"]
        o_served = (st["hits"] + st["stale_hits"]) - p_served
        o_total = (st["hits"] + st["stale_hits"] + st["misses"]) - p_total

        assert (p_served, p_total) == (1, 2)
        assert (o_served, o_total) == (1, 6)

    @pytest.mark.asyncio
    async def test_polling_population_is_cached_again(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-protected clans must be STORED, or the counters can only confirm the guess.

        get_clan(store_result=False) exists but is deliberately unused by the poll loop
        until the measured per-population hit rate justifies it.
        """
        cache, _calls = TestStoreResult._cache(monkeypatch)
        cache.protected_tags = {"#P1"}

        await cache.get_clan("#NOT_PROTECTED")

        assert "#NOT_PROTECTED" in cache.cache
