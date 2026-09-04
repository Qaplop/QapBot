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
