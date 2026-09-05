"""Memory-report instrumentation (tracker #0009, 2026-08-21).

The /admin Memory Profile report reported entry COUNTS only for the biggest caches, so a live
7.1 GB RSS investigation couldn't tell from it which structure was responsible — it had to be
measured by hand, which is when temp_war_objects (~61 KB/entry, cap 100k ≈ 6.1 GB by design)
turned out to dwarf clan_name_cache despite ~20x fewer entries. These tests lock in the byte
sizing and the trace-window caveat so a future reader isn't misled the same way.
"""
# pyright: reportPrivateUsage=false
from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock

from qapbot.QBdiscocmdshelper_admin_command import (
    _build_cache_summary,
    _estimate_dict_size_mb,
)


class TestEstimateDictSizeMb:
    def test_empty_dict_is_zero(self):
        assert _estimate_dict_size_mb({}) == 0.0

    def test_scales_with_entry_count(self):
        def _entry(i: int) -> Dict[str, Any]:
            return {"name": f"Clan {i}", "war_league": "Master League III", "flag": True}

        small = _estimate_dict_size_mb({f"#C{i}": _entry(i) for i in range(100)})
        large = _estimate_dict_size_mb({f"#C{i}": _entry(i) for i in range(10_000)})
        assert small > 0
        # ~100x the entries should be roughly ~100x the bytes (sampling makes it approximate).
        assert large > small * 50

    def test_extrapolates_beyond_the_sample_window(self):
        """Only `sample` entries are walked, but the result must reflect the FULL dict —
        otherwise every large cache would report the same misleadingly small figure."""
        entries = {f"#C{i}": {"name": f"Clan {i}"} for i in range(5_000)}
        assert _estimate_dict_size_mb(entries, sample=50) > _estimate_dict_size_mb(
            {f"#C{i}": {"name": f"Clan {i}"} for i in range(500)}, sample=50
        )

    def test_deep_values_count_more_than_shallow_ones(self):
        shallow = {f"#C{i}": {"n": 1} for i in range(200)}
        deep = {f"#C{i}": {"n": 1, "members": [{"tag": f"#P{j}"} for j in range(30)]}
                for i in range(200)}
        assert _estimate_dict_size_mb(deep) > _estimate_dict_size_mb(shallow)

    def test_never_raises_on_hostile_input(self):
        """Best-effort: a diagnostic must never be the thing that breaks /admin."""
        class Boom:
            def __len__(self):
                raise RuntimeError("nope")

        assert _estimate_dict_size_mb(Boom()) == 0.0


class TestBuildCacheSummary:
    @staticmethod
    def _cache() -> Any:
        cache = MagicMock()
        cache.coc_clan_cache.cache = {"#A": 1}
        cache.coc_clan_cache.get_memory_usage_mb.return_value = 1.5
        cache.clan_name_cache = {f"#C{i}": {"name": f"Clan {i}"} for i in range(50)}
        cache.subscriptions = {"g1": {"c1": []}}
        cache.leaderboard_messages = {}
        cache.user_accounts = {}
        cache.notification_state = {}
        cache.clan_history = {}
        cache.history_cache = {}
        cache.temp_war_stats = {f"#C{i}": {"x": 1} for i in range(10)}
        cache.temp_war_objects = {f"#C{i}": {"clan": {"members": [1, 2, 3]}} for i in range(10)}
        cache.server_config = {}
        return cache

    def test_reports_mb_for_the_three_big_caches(self):
        lines = "\n".join(_build_cache_summary(self._cache()))
        for name in ("clan_name_cache", "temp_war_stats", "temp_war_objects"):
            row = next(ln for ln in lines.splitlines() if name in ln)
            assert "MB" in row, f"{name} still reports entry count only: {row!r}"

    def test_temp_war_objects_projects_size_at_cap(self):
        """The cap is the number that matters operationally — 100k entries is ~6.1 GB by design,
        which is invisible if only the current count is shown."""
        lines = "\n".join(_build_cache_summary(self._cache()))
        row = next(ln for ln in lines.splitlines() if "temp_war_objects" in ln)
        assert "at cap" in row and "GB" in row

    def test_survives_a_broken_cache_object(self):
        broken = MagicMock()
        broken.coc_clan_cache.cache = {}
        broken.coc_clan_cache.get_memory_usage_mb.side_effect = RuntimeError("boom")
        del broken.clan_name_cache  # attribute access raises
        lines = _build_cache_summary(broken)
        assert any("error reading CACHE sizes" in ln for ln in lines)


class TestDeepEstimationFix:
    """2026-08-22: the original _estimate_dict_size_mb stopped recursing at depth 2, silently
    undercounting anything nested deeper than "dict of dict of list" -- exactly the shape of a
    real war payload (payload -> clan -> members[] -> attacks[]). Verified live against a
    realistic payload: the depth-capped version undercounted a pickle-size lower bound by ~5x.
    """

    @staticmethod
    def _war_payload(i: int) -> Dict[str, Any]:
        def member(j: int) -> Dict[str, Any]:
            return {
                "tag": f"#P{i}_{j}", "name": f"Player{j}", "townhallLevel": 15,
                "attacks": [
                    {"attackerTag": f"#P{i}_{j}", "defenderTag": f"#D{j}", "stars": 3,
                     "destructionPercentage": 100, "order": k}
                    for k in range(2)
                ],
            }
        return {
            "state": "inWar", "teamSize": 50,
            "clan": {"tag": f"#C{i}", "members": [member(j) for j in range(50)]},
        }

    def test_deeply_nested_payload_exceeds_pickle_lower_bound(self):
        """Real Python heap usage (sys.getsizeof summed over every object) must always be AT
        LEAST as large as pickle's compact serialized size -- pickle strips almost all per-object
        overhead. The old depth-capped estimator reported LESS than pickle size, which is
        structurally impossible for genuine heap cost and proved it was undercounting."""
        import pickle

        d = {f"#C{i}": self._war_payload(i) for i in range(20)}
        estimated_mb = _estimate_dict_size_mb(d, sample=20)
        pickle_mb = len(pickle.dumps(d)) / (1024 * 1024)

        assert estimated_mb >= pickle_mb

    def test_handles_reference_cycles_without_hanging(self):
        """A cycle guard (id()-based `seen` set) must stop infinite recursion — this test
        completing at all (not timing out / RecursionError) is the assertion."""
        cyclical: Dict[str, Any] = {}
        cyclical["self"] = cyclical
        result = _estimate_dict_size_mb({"a": cyclical})
        assert result >= 0.0


class TestCocClanCacheMemoryUsage:
    """2026-08-22: get_memory_usage_mb() was never wired up to _build_cache_summary() (which
    read a key get_stats() never sets, always defaulting to 0.0) — fixed alongside deepening
    get_memory_usage_mb() itself, since its own old version only measured
    sys.getsizeof(member) per member and missed every nested League/Icon/Badge object a real
    coc.py ClanMember points to."""

    def test_nonzero_for_a_populated_cache(self):
        from datetime import datetime, timezone
        from qapbot.coc_cache import CoCClanCache

        class _Icon:
            def __init__(self):
                self.small = "http://example/small.png"

        class _League:
            def __init__(self):
                self.name = "Master League III"
                self.icon = _Icon()

        class _Member:
            def __init__(self, i: int):
                self.tag = f"#P{i}"
                self.name = f"Player{i}"
                self.league = _League()

        class _Clan:
            def __init__(self):
                self.tag = "#C1"
                self.members = [_Member(i) for i in range(30)]

        cache = CoCClanCache()
        cache.cache["#C1"] = {"data": _Clan(), "timestamp": datetime.now(timezone.utc)}

        assert cache.get_memory_usage_mb() > 0.0

    def test_empty_cache_is_zero(self):
        from qapbot.coc_cache import CoCClanCache
        assert CoCClanCache().get_memory_usage_mb() == 0.0

    def test_build_cache_summary_uses_the_real_estimator(self):
        """_build_cache_summary must call get_memory_usage_mb(), not read a nonexistent key off
        get_stats() (the original bug — silently always 0.0 MB)."""
        cache = MagicMock()
        cache.coc_clan_cache.cache = {"#A": 1}
        cache.coc_clan_cache.get_memory_usage_mb.return_value = 42.5
        cache.clan_name_cache = {}
        cache.subscriptions = {}
        cache.leaderboard_messages = {}
        cache.user_accounts = {}
        cache.notification_state = {}
        cache.clan_history = {}
        cache.history_cache = {}
        cache.temp_war_stats = {}
        cache.temp_war_objects = {}
        cache.server_config = {}

        lines = "\n".join(_build_cache_summary(cache))
        cache.coc_clan_cache.get_memory_usage_mb.assert_called_once()
        assert "42.5 MB" in lines


class TestSharedReferenceAccounting:
    """2026-08-29 (tracker #0009): `_estimate_dict_size_mb` reset its id()-based `seen` set
    once per sampled entry. Anything shared BETWEEN entries — above all the dict KEYS, which
    are the same interned string objects in every entry — was therefore counted once per
    entry and then scaled by the full entry count. On PROD's clan_name_cache that reported
    462 MB against a true 263 MB, a 76% over-count that made the cache look like the single
    biggest RSS consumer when it is ~4%."""

    @staticmethod
    def _shared_key_dict(n: int) -> Dict[str, Any]:
        # One big key string object, reused as the key of every value dict — the same
        # aliasing that real cache entries have with their (few, fixed) field names.
        shared_key = "K" * 4000
        return {f"#C{i}": {shared_key: f"v{i}"} for i in range(n)}

    def test_shared_content_is_not_multiplied_by_entry_count(self):
        d = self._shared_key_dict(1_000)

        # Reference implementation of the OLD behaviour (seen reset per entry), so the
        # contrast this test protects is visible in the test itself rather than implied.
        import sys as _sys

        def _deep(value: Any, seen: set) -> int:
            if id(value) in seen:
                return 0
            seen.add(id(value))
            size = _sys.getsizeof(value)
            if isinstance(value, dict):
                for k, v in value.items():
                    size += _deep(k, seen) + _deep(v, seen)
            return size

        per_entry_seen_bytes = 0
        for i, (k, v) in enumerate(d.items()):
            if i >= 200:
                break
            seen: set = set()
            per_entry_seen_bytes += _deep(k, seen) + _deep(v, seen)
        old_mb = (per_entry_seen_bytes / 200) * len(d) / (1024 * 1024)

        new_mb = _estimate_dict_size_mb(d)

        assert old_mb > 3.0, "reference 'old' implementation should blow up on shared keys"
        assert new_mb < old_mb / 10, (
            f"shared key string is still being charged per entry: {new_mb:.2f} MB vs "
            f"{old_mb:.2f} MB under the old per-entry `seen` reset"
        )

    def test_unique_per_entry_content_still_scales(self):
        """The shared-reference fix must not flatten genuinely distinct per-entry data."""
        def _unique(n: int) -> Dict[str, Any]:
            return {f"#C{i}": {"blob": f"{i}" + "x" * 2000} for i in range(n)}

        small = _estimate_dict_size_mb(_unique(300))
        large = _estimate_dict_size_mb(_unique(3_000))
        assert large > small * 5


class TestSlotsAndGeneratorWalking:
    """2026-08-29 (tracker #0009): `get_memory_usage_mb()` recursed via
    `hasattr(value, "__dict__")`. Every coc.py model class declares `__slots__` and so has NO
    `__dict__` — the walk stopped at the top-level object and returned a bare
    `sys.getsizeof(clan)` (360 bytes for a full 50-member clan), which is why the report
    printed ~0.1 MB for a cache actually holding ~69-90 KB per entry.

    The pre-existing tests above did not catch it because their fixtures were ordinary
    `__dict__` classes. These use `__slots__` (and a generator-pinned payload) deliberately."""

    @staticmethod
    def _populated_cache(data: Any) -> Any:
        from datetime import datetime, timezone
        from qapbot.coc_cache import CoCClanCache

        cache = CoCClanCache()
        cache.cache["#C1"] = {"data": data, "timestamp": datetime.now(timezone.utc)}
        return cache

    def test_slots_only_object_is_walked(self):
        import sys as _sys

        class _SlotMember:
            __slots__ = ("tag", "blob")

            def __init__(self, i: int):
                self.tag = f"#P{i}"
                # Built per-instance rather than as a `"x" * 1000` literal: CPython folds
                # that at compile time into ONE shared constant, so every member would
                # alias it and the id()-based `seen` set would (correctly) count ~1 KB
                # total instead of ~50 KB — the fixture, not the walk, would be wrong.
                self.blob = "x" * 1000 + str(i)

        class _SlotClan:
            __slots__ = ("tag", "members")

            def __init__(self):
                self.tag = "#C1"
                self.members = [_SlotMember(i) for i in range(50)]

        clan = _SlotClan()
        assert not hasattr(clan, "__dict__"), "fixture must reproduce the __slots__ layout"

        reported = self._populated_cache(clan).get_memory_usage_mb() * 1024 * 1024
        shallow = _sys.getsizeof(clan)
        assert reported > 40_000, (
            f"__slots__ payload not walked: reported {reported:.0f} B for ~50 KB of members"
        )
        assert reported > shallow * 100

    def test_generator_pinned_payload_is_counted(self):
        """coc.py keeps the raw API `memberList` alive through an un-exhausted
        `_iter_members` generator expression — not through any attribute — which is why
        attribute-walking alone still under-reported this cache by ~3x."""
        # Distinct blob per entry — see the note in the __slots__ test above.
        payload = [{"tag": f"#P{i}", "blob": "y" * 1000 + str(i)} for i in range(50)]

        class _LazyClan:
            __slots__ = ("tag", "_iter_members")

            def __init__(self, members: Any):
                self.tag = "#C1"
                self._iter_members = (m for m in members)

        clan = _LazyClan(payload)
        del payload  # only the un-exhausted generator keeps it alive now

        reported = self._populated_cache(clan).get_memory_usage_mb() * 1024 * 1024
        assert reported > 40_000, (
            f"generator-pinned payload not counted: reported {reported:.0f} B"
        )

    def test_does_not_wander_into_the_coc_client(self):
        """Every coc.py model holds a `_client` back-reference. Recursing through it would
        charge the client's whole graph (aiohttp session, throttler, CacheManager) once per
        cached clan — measured as a 44% over-report before coc.Client was excluded."""
        import asyncio as _asyncio

        import coc as _coc  # type: ignore[import-untyped]

        try:
            _asyncio.get_event_loop()
        except RuntimeError:
            _asyncio.set_event_loop(_asyncio.new_event_loop())
        client = _coc.Client()
        # coc.Client is itself __slots__-based, so hang the marker off one of its own
        # container slots instead of setting a new attribute (~1 MB reachable from client).
        client._clans.update({f"#M{i}": "z" * 5000 + str(i) for i in range(200)})

        class _ClanWithClient:
            __slots__ = ("tag", "_client")

            def __init__(self):
                self.tag = "#C1"
                self._client = client

        reported = self._populated_cache(_ClanWithClient()).get_memory_usage_mb() * 1024 * 1024
        assert reported < 100_000, (
            f"walk followed _client into the shared client graph: reported {reported:.0f} B"
        )
