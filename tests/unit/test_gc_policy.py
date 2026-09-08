"""Tests for the GC scheduling policy (2026-09-04).

Every Discord-responsiveness stall this bot has had is an automatic gen-2 sweep: on PROD
build 11, 11 of 11 `[LOOP-LAG]` stalls matched a `[GC-AUTO]` pause within the watchdog's
100ms probe resolution, several past Discord's 3s interaction ACK deadline.

coc.py's war graph is cyclic by construction (`WarClan._war`, `ClanWarMember.war`/`.clan`,
`WarAttack.war`/`.member`), so refcounting cannot free a war *as coc.py hands it to us* — it
survives until a sweep walks it. The policy is therefore about WHICH sweep runs WHEN.

⚠️ SUPERSEDED 2026-09-08 (tracker #0106). This file was written for the 2026-09-04 policy —
"automatic collection off, a young-generation collect per cycle, one full sweep nightly" —
and that policy was REVERSED because it caused a ~1 GB/hour heap climb. `gc.collect(1)`
promotes every survivor to the next generation, so firing it each cycle while that cycle's
war population was still live pumped ~20k objects/cycle into generation 2, which automatic
collection being off then left undrained until 03:00 UTC.

The current policy is the established pattern for latency-sensitive Python:

  - automatic collection ON, with threshold0 raised well above CPython's default so most
    objects die by refcounting before any collection sees them (promotion attacked at source)
  - gc.freeze() after startup, keeping the large static caches out of every scan
  - the nightly full sweep kept as a backstop, plus an RSS-triggered restart as a safety net

See qapbot/docs/PERFORMANCE_TUNING.md and tests/unit/test_gc_policy_promotion.py.

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
    """Build and drop coc.py-shaped war graphs: cyclic, so refcounting cannot free them.

    NOTE the graphs are dropped immediately here, so nothing is ever live at a subsequent
    collection. That is fine for tests about whether the garbage EXISTS, but it cannot model
    promotion — use `_war_graph_into()` for anything about accumulation across cycles. See
    the note in test_repeated_cycles_do_not_accumulate.
    """
    _war_graph_into([], wars, members)


def _war_graph_into(sink: List[Any], wars: int = 40, members: int = 20) -> None:
    """Same graphs, but retained by `sink` — models the bot holding a cycle's war population
    in temp_war_objects/temp_war_stats across the per-cycle cleanup point."""
    for _ in range(wars):
        war: Dict[str, Any] = {"tag": "w", "members": []}
        for j in range(members):
            m: Dict[str, Any] = {"tag": j, "war": war, "attacks": []}
            m["attacks"].append({"attacker": m, "war": war})  # back-refs, exactly like coc.py
            war["members"].append(m)
        sink.append(war)


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
        """No pile-up: every cycle must reclaim its own garbage, not defer it.

        REWRITTEN 2026-09-08 (tracker #0106). The original version of this test asserted
        exactly the right property and still passed all the way through the incident that
        proved it false in production — PROD accumulated 10-11.5M objects per day while this
        was green. The reason is in the setup, not the assertion:

            for _ in range(6):
                _war_graph()            # <- result never bound; garbage immediately
                freed.append(gc.collect(1))

        With nothing live at the moment of the collect, nothing SURVIVES it, so nothing is
        promoted — and promotion is the entire failure mode. The real bot holds each cycle's
        war population live across that point (temp_war_objects / temp_war_stats keep it
        until the next cycle replaces it), so its survivors were promoted into generation 2,
        where a gen-1 collect can never reach them again.

        The fix here is to hold the graph across the collect, exactly as the bot does, and to
        model the CURRENT policy (automatic collection on, threshold0 raised) rather than the
        withdrawn one. Verified 2026-09-08: with the old policy substituted back in, this same
        body strands ~14,880 objects in generation 2 and the assertion fires.

        The explicit old-vs-new comparison lives in test_gc_policy_promotion.py.
        """
        gc.collect(2)
        gc.enable()                      # the policy since 2026-09-08: automatic, not manual
        _, t1, t2 = gc.get_threshold()
        gc.set_threshold(50_000, t1, t2)

        held: List[Any] = []
        for _ in range(6):
            graph: List[Any] = []
            _war_graph_into(graph)
            held = graph                 # LIVE across the cleanup point, as temp_war_* is
        del held, graph

        gc.collect(1)
        assert gc.collect(2) == 0, (
            "backlog accumulated across cycles: objects live at a collection got promoted to "
            "generation 2, where a gen-1 collect can no longer reach them. That is tracker "
            "#0106's root cause and it must not come back."
        )


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
