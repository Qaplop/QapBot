"""Tests for the GC scheduling policy (2026-09-04).

Every Discord-responsiveness stall this bot has had is an automatic gen-2 sweep: on PROD
build 11, 11 of 11 `[LOOP-LAG]` stalls matched a `[GC-AUTO]` pause within the watchdog's
100ms probe resolution, several past Discord's 3s interaction ACK deadline.

coc.py's war graph is cyclic by construction (`WarClan._war`, `ClanWarMember.war`/`.clan`,
`WarAttack.war`/`.member`), so refcounting cannot free a war *as coc.py hands it to us* — it
survives until a sweep walks it. The policy is therefore about WHICH sweep runs WHEN:

  - automatic collection off, so nothing fires mid-cycle or during the sleep window
  - a young-generation collect per cycle, which reclaims that whole population cheaply
    because nothing has been promoted
  - the one full sweep per day in the nightly maintenance window, where commands are blocked

NOTE this is only half the story. An earlier version of this docstring called the garbage
"unavoidable"; that was wrong. `release_war_object()` severs those back-references after the
graph's last consumer so wars never reach the collector at all — see
`test_release_war_object.py`, which is what actually removed the pause. This policy remains
load-bearing underneath it: the young-gen collect is only sufficient *because* automatic
collection is off, and it still catches whatever cyclic garbage the rest of the process makes.

These tests pin the properties that make that safe. They are deliberately about GC
*semantics* rather than about QapBot's startup path, which cannot be imported in a unit test.
"""
from __future__ import annotations

import gc
from typing import Any, Dict, List

import pytest


def _war_graph(wars: int = 40, members: int = 20) -> None:
    """Build and drop coc.py-shaped war graphs: cyclic, so refcounting cannot free them."""
    for _ in range(wars):
        war: Dict[str, Any] = {"tag": "w", "members": []}
        for j in range(members):
            m: Dict[str, Any] = {"tag": j, "war": war, "attacks": []}
            m["attacks"].append({"attacker": m, "war": war})  # back-refs, exactly like coc.py
            war["members"].append(m)


@pytest.fixture
def isolated_gc():
    """Restore the ambient GC state — pytest itself depends on it."""
    was_enabled = gc.isenabled()
    thresholds = gc.get_threshold()
    try:
        yield
    finally:
        gc.set_threshold(*thresholds)
        gc.enable() if was_enabled else gc.disable()
        gc.collect()


class TestTheGarbageIsReal:
    def test_war_graphs_are_cyclic_and_survive_refcounting(self, isolated_gc) -> None:
        """The premise. If these were acyclic, refcounting would free them and no GC policy
        would be needed at all — so this test is what justifies the whole design."""
        gc.disable()
        gc.collect()

        _war_graph()
        leaked = gc.collect(1)

        assert leaked > 0, (
            "war graphs were freed without a sweep — if coc.py ever stops back-referencing "
            "the ClanWar, revisit the GC policy, it may no longer be needed"
        )


class TestYoungCollectSuffices:
    def test_young_collect_reclaims_everything_when_automatic_gc_is_off(self, isolated_gc) -> None:
        """The load-bearing claim: with nothing promoted on CPython's schedule, the whole
        per-cycle population is still young, so a young-only collect gets all of it."""
        gc.disable()
        gc.collect()

        _war_graph()
        young = gc.collect(1)
        leftover_for_full_sweep = gc.collect(2)

        assert young > 0
        assert leftover_for_full_sweep == 0, (
            f"{leftover_for_full_sweep} objects escaped the young collect and would wait for "
            "the nightly full sweep"
        )

    def test_repeated_cycles_do_not_accumulate(self, isolated_gc) -> None:
        """No pile-up: every cycle must reclaim its own garbage, not defer it."""
        gc.disable()
        gc.collect()

        freed = []
        for _ in range(6):
            _war_graph()
            freed.append(gc.collect(1))

        assert all(f > 0 for f in freed), f"a cycle reclaimed nothing: {freed}"
        assert gc.collect(2) == 0, "backlog accumulated across cycles despite per-cycle collects"


class TestFreezeProtectsTheStaticCaches:
    def test_frozen_objects_are_not_walked_by_a_full_sweep(self, isolated_gc) -> None:
        """Why the startup freeze exists: it takes the big static caches out of every sweep."""
        gc.disable()
        gc.collect()
        static: List[Dict[str, Any]] = [{"n": i, "ref": None} for i in range(500)]
        for d in static:
            d["ref"] = static  # make it cyclic so it would otherwise be sweep-visible
        gc.collect()

        before = gc.get_freeze_count()
        gc.freeze()
        try:
            assert gc.get_freeze_count() > before
            assert gc.collect(2) == 0, "a full sweep still found work in the frozen set"
        finally:
            gc.unfreeze()

        assert len(static) == 500, "freezing must not collect live objects"

    def test_unfreeze_restores_collectability_for_the_nightly_sweep(self, isolated_gc) -> None:
        """The nightly unfreeze -> collect -> freeze is the only release valve for cyclic
        garbage that got frozen; if unfreeze stopped working it would leak silently."""
        gc.disable()
        gc.collect()
        gc.freeze()
        try:
            _war_graph()
            gc.freeze()  # freeze the garbage, as the nightly re-freeze would
            assert gc.collect(2) == 0, "frozen garbage should be invisible to a sweep"
        finally:
            gc.unfreeze()

        assert gc.collect(2) > 0, "unfreeze did not return the frozen garbage to the collector"


class TestEscapeHatch:
    def test_disable_and_enable_round_trip(self, isolated_gc) -> None:
        """GC_AUTOMATIC=1 must genuinely restore CPython scheduling."""
        gc.disable()
        assert not gc.isenabled()
        gc.enable()
        assert gc.isenabled()

    def test_explicit_collect_works_while_automatic_is_disabled(self, isolated_gc) -> None:
        """The entire policy rests on this: disabling automatic collection must not disable
        explicit collection, or the per-cycle and nightly sweeps would silently do nothing."""
        gc.disable()
        gc.collect()

        _war_graph()

        assert not gc.isenabled()
        assert gc.collect(1) > 0


class TestChunkedCollection:
    """`maybe_chunk_collect()` slices the per-cycle collection so no single pause is visible.

    Even after `release_war_object()` removed 78% of the garbage, one end-of-cycle
    `gc.collect(1)` still cost 1.135s on PROD: a sweep's cost tracks what it WALKS, and most of
    that is the live young generation rather than the garbage found. Measured at cycle shape,
    slicing every 500 units gives a **5.8x lower max pause** with *slightly less* total GC time
    (dead objects stop being re-walked by later passes). every-100 measured worse than
    every-500, so the interval is a tuned value, not an arbitrary one.

    This is also what makes the remaining `coc.Clan` garbage a non-problem without lifetime
    tracking: cached clans are handed to callers and live up to 600s, so severing their
    back-references would risk breaking a holder. Making collection invisible needs no
    ownership analysis at all.
    """

    def test_collects_once_per_interval_and_only_gen0(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import QapBot

        seen: List[Any] = []
        monkeypatch.setattr(QapBot, "_CHUNK_COLLECT_EVERY", 5)
        monkeypatch.setattr(QapBot, "_chunk_collect_counter", 0)
        monkeypatch.setattr(QapBot.gc, "collect", lambda *a, **k: seen.append(a[0] if a else None) or 0)

        for _ in range(12):
            QapBot.maybe_chunk_collect()

        assert len(seen) == 2, f"expected 2 slices in 12 calls at interval 5, got {len(seen)}"
        assert set(seen) == {0}, "slices must be generation-0 only — a deeper sweep is the pause we are avoiding"

    def test_disabled_by_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GC_CHUNK_EVERY=0 must fully disable slicing, for A/B and for debugging."""
        import QapBot

        seen: List[Any] = []
        monkeypatch.setattr(QapBot, "_CHUNK_COLLECT_EVERY", 0)
        monkeypatch.setattr(QapBot, "_chunk_collect_counter", 0)
        monkeypatch.setattr(QapBot.gc, "collect", lambda *a, **k: seen.append(1) or 0)

        for _ in range(50):
            QapBot.maybe_chunk_collect()

        assert seen == []

    def test_never_raises_into_a_cycle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failed teardown must not fail an update cycle."""
        import QapBot

        def _boom(*_a: Any, **_k: Any) -> int:
            raise RuntimeError("gc exploded")

        monkeypatch.setattr(QapBot, "_CHUNK_COLLECT_EVERY", 1)
        monkeypatch.setattr(QapBot, "_chunk_collect_counter", 0)
        monkeypatch.setattr(QapBot.gc, "collect", _boom)

        QapBot.maybe_chunk_collect()  # must not propagate

    def test_deliberate_flag_is_always_cleared(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A stuck flag would mislabel every later automatic pause as [GC-SCHEDULED] — the one
        signal that tells us the GC policy has lapsed."""
        import QapBot

        def _boom(*_a: Any, **_k: Any) -> int:
            raise RuntimeError("gc exploded")

        monkeypatch.setattr(QapBot, "_CHUNK_COLLECT_EVERY", 1)
        monkeypatch.setattr(QapBot, "_chunk_collect_counter", 0)
        monkeypatch.setattr(QapBot.gc, "collect", _boom)

        QapBot.maybe_chunk_collect()

        assert QapBot._gc_deliberate is False

    def test_slicing_lowers_the_worst_pause(self, isolated_gc) -> None:
        """The actual claim, measured rather than asserted structurally."""
        import time

        gc.disable()
        gc.collect()

        def run(interval: int | None) -> float:
            pauses = []
            live: List[Any] = []
            for i in range(1200):
                _war_graph(wars=1, members=8)
                live.append(object())
                if len(live) > 50:
                    live.pop(0)
                if interval and i % interval == interval - 1:
                    t = time.perf_counter(); gc.collect(0); pauses.append(time.perf_counter() - t)
            t = time.perf_counter(); gc.collect(1); pauses.append(time.perf_counter() - t)
            return max(pauses)

        one_big = min(run(None) for _ in range(3))
        sliced = min(run(300) for _ in range(3))

        assert sliced < one_big, (
            f"slicing did not lower the worst pause ({sliced:.4f}s vs {one_big:.4f}s) — "
            "re-tune _CHUNK_COLLECT_EVERY before trusting it on PROD"
        )
