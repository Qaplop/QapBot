"""Measurement for the GC exposure tracker-0009 Stage 3 did NOT remove.

Stage 3 stopped Phase 1 retaining a `coc.ClanWar` across the cycle. What remains is
`_league_war_cache` (cap 1000) and `_league_group_cache` (cap 200): both hold coc.py graphs
created AFTER startup, so `gc.freeze()` never covers them and every full sweep walks them.
Evicted entries are cyclic (Pitfall 58) and nothing severs them — severing is only safe for
objects we own, and these are shared (Pitfall 61).

`measure_cwl_cache_gc_footprint()` turns that from a guess into a number. These tests pin the
properties that make the number trustworthy, because a measurement that silently over- or
under-reports is worse than none: it would be used to justify a change.
"""
from __future__ import annotations

import asyncio
import gc
from typing import Any

import pytest

coc = pytest.importorskip("coc")

from qapbot.cache_manager import CacheManager


def _war_payload(size: int = 15) -> dict:
    return {
        "state": "inWar", "teamSize": size, "attacksPerMember": 2,
        "preparationStartTime": "20260905T080000.000Z",
        "startTime": "20260905T090000.000Z", "endTime": "20260905T210000.000Z",
        "clan": {"tag": "#AAA", "name": "A", "clanLevel": 10, "attacks": 0, "stars": 0,
                 "destructionPercentage": 0.0, "badgeUrls": {},
                 "members": [{"tag": f"#M{i}", "name": f"m{i}", "townhallLevel": 14,
                              "mapPosition": i + 1, "attacks": []} for i in range(size)]},
        "opponent": {"tag": "#BBB", "name": "B", "clanLevel": 10, "attacks": 0, "stars": 0,
                     "destructionPercentage": 0.0, "badgeUrls": {},
                     "members": [{"tag": f"#O{i}", "name": f"o{i}", "townhallLevel": 14,
                                  "mapPosition": i + 1, "attacks": []} for i in range(size)]},
    }


@pytest.fixture
def client() -> Any:
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    return coc.Client()


@pytest.fixture
def cm(client) -> Any:
    """A CacheManager with only the two CWL caches populated — no __init__ side effects."""
    m = CacheManager.__new__(CacheManager)
    m._league_war_cache = {}
    m._league_group_cache = {}
    m.coc_client = client
    return m


def _war(client, size: int = 15) -> Any:
    return coc.ClanWar(data=_war_payload(size), client=client, clan_tag="#AAA")


class TestItMeasuresSomething:
    def test_empty_caches_report_zero(self, cm) -> None:
        fp = cm.measure_cwl_cache_gc_footprint()

        assert fp["war_entries"] == 0 and fp["war_objects"] == 0
        assert fp["pct_of_live"] == 0.0

    def test_a_cached_war_has_a_substantial_object_count(self, cm, client) -> None:
        """The premise of the whole follow-up: one war is many tracked objects, not one."""
        cm._league_war_cache["#W1"] = (_war(client), 0.0, "inwar")

        fp = cm.measure_cwl_cache_gc_footprint()

        assert fp["war_entries"] == 1
        assert fp["war_objects"] > 20, (
            f"only {fp['war_objects']} tracked objects for a 15v15 war — the walk is not "
            "reaching the graph, which would make the attribution log meaninglessly small"
        )

    def test_counts_scale_with_entries(self, cm, client) -> None:
        cm._league_war_cache["#W1"] = (_war(client), 0.0, "inwar")
        one = cm.measure_cwl_cache_gc_footprint()["war_objects"]
        for i in range(2, 5):
            cm._league_war_cache[f"#W{i}"] = (_war(client), 0.0, "inwar")
        four = cm.measure_cwl_cache_gc_footprint()["war_objects"]

        assert four > one * 2, f"4 wars ({four}) should far exceed 1 war ({one})"


class TestTheNumberIsTrustworthy:
    def test_the_walk_does_not_escape_into_the_client(self, cm, client) -> None:
        """The failure that would make this useless: coc.py models all back-reference
        coc.Client, so a walk that follows it charges the session, throttler and the whole
        CacheManager to the cache — once per entry. coc_cache.get_memory_usage_mb() measured
        that exact mistake as 96.8 KB/entry against 67.1 KB actual."""
        cm._league_war_cache["#W1"] = (_war(client), 0.0, "inwar")

        one_war = cm.measure_cwl_cache_gc_footprint()["war_objects"]

        assert one_war < 5000, (
            f"{one_war} tracked objects for ONE war — the walk escaped into coc.Client or "
            "another shared root and is charging the whole process to this cache"
        )

    def test_only_tracked_objects_are_counted(self, cm) -> None:
        """A sweep walks GC-tracked containers; strings and ints cost it nothing. Counting
        them would inflate the attribution and overstate the saving on offer."""
        cm._league_war_cache["#S"] = (["x" * 50, 12345, 3.14, None], 0.0, "inwar")

        fp = cm.measure_cwl_cache_gc_footprint()

        assert fp["war_objects"] == 1, (
            f"expected only the list itself to be tracked, got {fp['war_objects']} — "
            "untracked leaves are being counted"
        )

    def test_shared_objects_are_not_double_counted_within_an_entry(self, cm, client) -> None:
        """Per-entry accounting is deliberate: the log's objects-per-entry is what predicts
        eviction garbage, so each entry must be charged its own full graph."""
        shared = _war(client)
        cm._league_war_cache["#W1"] = (shared, 0.0, "inwar")
        single = cm.measure_cwl_cache_gc_footprint()["war_objects"]

        cm._league_war_cache["#W2"] = (shared, 0.0, "inwar")
        double = cm.measure_cwl_cache_gc_footprint()["war_objects"]

        assert double == single * 2, (
            "per-entry accounting changed; objects-per-entry would no longer predict what an "
            "eviction orphans"
        )

    def test_it_never_raises(self, cm) -> None:
        """A measurement must not be able to break nightly maintenance."""
        class Hostile:
            def __getattribute__(self, name: str) -> Any:
                raise RuntimeError("boom")

        cm._league_war_cache["#BAD"] = (Hostile(), 0.0, "inwar")

        fp = cm.measure_cwl_cache_gc_footprint()

        assert "war_objects" in fp


class TestFrozenObjectsDontInflateThePercentage:
    """Bug #0107: PROD's first-ever [GC-ATTRIBUTION] reading said CWL caches held 53300.8% of
    live tracked objects -- impossible on its face. Root cause: gc.get_objects() excludes frozen
    objects by design, but _walk() (this module's own graph walk) counts them via
    gc.get_referents() regardless of frozen state. This codebase freezes millions of objects
    nightly (a GC-pause-avoidance strategy), so once a meaningful share of a cached war's own
    graph has itself been frozen by a PREVIOUS nightly run, the frozen-exclusive denominator
    undercounts against the frozen-inclusive numerator and the ratio can exceed 100%.
    """

    def test_pct_of_live_stays_bounded_when_the_cached_war_is_frozen(self, cm, client) -> None:
        war = _war(client)
        cm._league_war_cache["#W1"] = (war, 0.0, "inwar")
        try:
            gc.freeze()  # simulates a war that survived to a previous nightly freeze
            fp = cm.measure_cwl_cache_gc_footprint()
        finally:
            gc.unfreeze()

        assert fp["war_objects"] > 0, "the walk must still find the frozen graph's objects"
        assert fp["pct_of_live"] <= 100.0, (
            f"pct_of_live={fp['pct_of_live']} — a frozen-inclusive numerator was compared "
            "against a frozen-exclusive denominator again"
        )

    def test_total_tracked_accounts_for_the_frozen_population(self, cm) -> None:
        """Directly pins the fix: total_tracked = get_objects() + get_freeze_count(), not
        get_objects() alone."""
        before = gc.get_freeze_count()
        try:
            gc.freeze()
            fp = cm.measure_cwl_cache_gc_footprint()
            assert fp["total_tracked"] >= gc.get_freeze_count() - before, (
                "total_tracked did not grow with the frozen population — get_freeze_count() "
                "is not being added back in"
            )
        finally:
            gc.unfreeze()


class TestSampling:
    def test_sampling_extrapolates_and_is_flagged(self, cm, client) -> None:
        """The caches run at their caps during CWL; the nightly window must not be stretched by
        a full 1000-war walk, so a sampled result has to be both scaled and labelled."""
        for i in range(10):
            cm._league_war_cache[f"#W{i}"] = (_war(client), 0.0, "inwar")

        full = cm.measure_cwl_cache_gc_footprint()
        sampled = cm.measure_cwl_cache_gc_footprint(sample=2)

        assert full["sampled"] is False
        assert sampled["sampled"] is True
        assert sampled["war_objects"] == pytest.approx(full["war_objects"], rel=0.15)


class TestEvictionNote:
    def test_silent_until_a_measurement_has_run(self, cm) -> None:
        """Never guess. Before the first nightly measurement there is no objects-per-entry, and
        inventing one would put a fabricated number in a PROD log."""
        CacheManager._cwl_objs_per_war = None
        CacheManager._cwl_objs_per_group = None

        assert cm._evicted_objects_note(208, 12) == ""

    def test_reports_orphaned_objects_once_measured(self, cm, client) -> None:
        cm._league_war_cache["#W1"] = (_war(client), 0.0, "inwar")
        cm.measure_cwl_cache_gc_footprint()

        note = cm._evicted_objects_note(208, 0)

        assert "cyclic garbage" in note
        assert "208 object" not in note, (
            "the note must report OBJECTS, not echo the entry count the log line already has"
        )

    def test_note_never_raises(self, cm) -> None:
        CacheManager._cwl_objs_per_war = "not-a-number"  # type: ignore[assignment]
        try:
            assert cm._evicted_objects_note(5, 0) == ""
        finally:
            CacheManager._cwl_objs_per_war = None
