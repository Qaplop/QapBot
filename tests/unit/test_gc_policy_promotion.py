"""GC policy: the per-cycle collect was a promotion pump (tracker #0106, 2026-09-08).

ROOT CAUSE of the ~1 GB/hour heap climb. The 2026-09-04 policy — `gc.disable()` plus a
per-cycle `gc.collect(1)` — was not merely failing to reclaim generation-2 garbage. It was
CREATING it.

CPython promotes every surviving object to the next generation on each collection. The
per-cycle collect fired at cycle end, while that cycle's war population was still live, so
every survivor went straight into generation 2 — which, with automatic collection disabled,
nothing drained until the 03:00 UTC nightly sweep.

Evidence from PROD that this was real, not theoretical:
  - RSS climb went ~1.4 MB/min -> ~19 MB/min at the run starting 11 min after the deploy.
  - The nightly sweep's own reclaim count jumped 211,262 -> 11,457,899 on the first night.
  - Arithmetic matches: ~480 cycles/day x ~20k promoted/collect ~= 9.6M/day vs 10.1-11.5M
    actually reclaimed nightly.

These tests pin both halves: that promotion is what strands objects (so nobody reintroduces
the pump), and that the replacement policy leaves nothing stranded.

All tests restore global GC state in `finally` — a leaked `gc.disable()` or threshold change
would silently corrupt every later test in the session.
"""
# pyright: reportPrivateUsage=false
from __future__ import annotations

import gc
import os

os.environ.setdefault("DISCORD_TOKEN", "test-token")

import pytest


class _Node:
    """Cyclic by construction, like coc.py's war graph (WarAttack.war / WarClan._war point
    back at the ClanWar). Refcounting can never free these; only the cyclic collector can."""
    __slots__ = ("peer", "payload")

    def __init__(self) -> None:
        self.peer = None
        self.payload = None


def _make_cycles(n: int) -> list:
    out = []
    for _ in range(n):
        a, b = _Node(), _Node()
        a.peer = b
        b.peer = a           # the cycle
        a.payload = ("x" * 40,)
        out.append(a)
    return out


@pytest.fixture(autouse=True)
def _restore_gc_state():
    """Global GC state is process-wide; a leak here breaks unrelated tests."""
    was_enabled = gc.isenabled()
    thresholds = gc.get_threshold()
    try:
        yield
    finally:
        gc.set_threshold(*thresholds)
        gc.enable() if was_enabled else gc.disable()
        gc.collect(2)


class TestPromotionIsWhatStrandsObjects:
    """The mechanism itself, independent of QapBot. If CPython ever changes this, the whole
    root-cause analysis needs revisiting — so pin it."""

    def test_surviving_one_gen1_collect_makes_an_object_unreachable_to_gen1(self):
        gc.disable()
        objs = _make_cycles(20_000)
        gc.collect(1)          # survives -> promoted out of the young generations
        del objs               # NOW garbage

        freed_by_gen1 = gc.collect(1)
        freed_by_gen2 = gc.collect(2)

        assert freed_by_gen1 == 0, (
            "gen-1 reached promoted objects — CPython's promotion behaviour changed and the "
            "tracker #0106 analysis needs re-deriving"
        )
        assert freed_by_gen2 == 40_000, "only a full collection should reclaim promoted cycles"

    def test_without_the_pump_the_same_objects_stay_reachable_to_gen1(self):
        """The control: identical objects, never subjected to a surviving collection."""
        gc.disable()
        objs = _make_cycles(20_000)
        del objs

        assert gc.collect(1) == 40_000, "young cyclic garbage should be reclaimable by gen-1"


class TestPolicyOutcome:
    """End-to-end on a simulation of the bot's shape: each cycle builds a war population that
    is still LIVE at the cleanup point (as temp_war_* is), then is replaced next cycle."""

    @staticmethod
    def _simulate(per_cycle_collect: bool, automatic: bool, threshold0: int, cycles: int = 20):
        gc.collect(2)
        gc.set_threshold(2000, 10, 10)
        gc.enable()
        if not automatic:
            gc.disable()
        elif threshold0:
            _, t1, t2 = gc.get_threshold()
            gc.set_threshold(threshold0, t1, t2)

        held = None
        for _ in range(cycles):
            fresh = _make_cycles(2_000)
            held = fresh                      # still referenced at the cleanup point
            if per_cycle_collect:
                gc.collect(1)                 # the promotion pump
        del held, fresh

        gc.collect(1)
        return gc.collect(2)                  # what gen-1 could NOT reach

    def test_old_policy_strands_objects_in_generation_2(self):
        stranded = self._simulate(per_cycle_collect=True, automatic=False, threshold0=0)
        assert stranded > 10_000, (
            "the 2026-09-04 policy should strand promoted cycles in gen-2 — if it no longer "
            "does, this test's simulation has drifted from the real failure"
        )

    def test_new_policy_strands_nothing(self):
        stranded = self._simulate(per_cycle_collect=False, automatic=True, threshold0=50_000)
        assert stranded == 0, f"{stranded} objects stranded in gen-2 under the new policy"


class TestFreezeMustNotCaptureTransientData:
    """Second finding of 2026-09-08: the NIGHTLY re-freeze was over-capturing.

    `gc.freeze()` moves everything currently tracked into the permanent generation, so running
    it at 03:00 froze whatever happened to be live at that instant — the in-flight war
    population, coc_clan_cache, the CWL caches — not just the permanent baseline. Measured on
    PROD: the startup freeze covers ~616k objects; the nightly re-freeze covered
    1,282,713-2,903,579, i.e. 2-4.7x as many.

    That is not harmless, because frozen objects are exempt from cyclic collection. These tests
    pin why, and pin that the nightly path no longer does it.
    """

    def test_frozen_cyclic_garbage_is_never_collected(self):
        """The hazard itself. Acyclic frozen garbage still dies by refcounting, but coc.py's
        graphs are cyclic by construction — so a coc.Clan frozen at 03:00 and TTL-evicted at
        03:10 was garbage the collector could not see until the next night's unfreeze."""
        gc.collect(2)
        objs = _make_cycles(5_000)
        try:
            gc.freeze()                 # as the nightly re-freeze did, while this was live
            del objs                    # now unreachable cyclic garbage
            assert gc.collect(2) == 0, (
                "frozen cyclic garbage was collected — if CPython changed this, the reason "
                "for dropping the nightly re-freeze needs re-deriving"
            )
        finally:
            gc.unfreeze()
        # Once unfrozen it is reclaimable again — which is exactly what the old nightly
        # unfreeze did, 24 hours late.
        assert gc.collect(2) == 10_000

    def test_acyclic_frozen_garbage_still_dies_by_refcounting(self):
        """The control, and why this was survivable rather than catastrophic: only the cyclic
        share leaked. War payload dicts (build_war_payload output) are acyclic."""
        gc.collect(2)
        payloads = [{"tag": i, "members": [{"n": j} for j in range(10)]} for i in range(2_000)]
        try:
            gc.freeze()
            frozen_before = gc.get_freeze_count()
            del payloads                # refcounting frees these even though frozen
            assert gc.get_freeze_count() < frozen_before
        finally:
            gc.unfreeze()

    @staticmethod
    def _code_only(src: str) -> str:
        """Strip comment lines. These assertions are about what EXECUTES — the surrounding
        comments deliberately discuss gc.freeze()/unfreeze() at length, and matching those
        would make the test fire on its own documentation."""
        return "\n".join(
            line for line in src.splitlines() if not line.lstrip().startswith("#")
        )

    def test_nightly_path_no_longer_unfreezes_or_refreezes(self):
        import inspect
        import QapBot
        code = self._code_only(inspect.getsource(QapBot.run_nightly_maintenance_routine))
        assert "gc.unfreeze()" not in code, "the nightly unfreeze is back"
        assert "gc.freeze()" not in code, (
            "the nightly re-freeze is back — it captures whatever is live at 03:00, and the "
            "cyclic share of that becomes uncollectable for 24 hours"
        )

    def test_startup_freeze_is_the_only_freeze(self):
        """One freeze, at startup, covering only the genuinely-permanent caches."""
        import inspect
        import QapBot
        code = self._code_only(inspect.getsource(QapBot))
        assert code.count("gc.freeze()") == 1, (
            "expected exactly one gc.freeze() call (the startup one); found "
            f"{code.count('gc.freeze()')}"
        )
        assert "gc.unfreeze()" not in code, "no unfreeze should remain anywhere"


class TestConfigDefaults:
    def test_automatic_collection_is_on_by_default(self):
        """This is the reversal. Disabling it is what caused #0106."""
        from qapbot.config import CONFIG
        assert CONFIG.gc_automatic is True

    def test_per_cycle_collect_is_off_by_default(self):
        from qapbot.config import CONFIG
        assert CONFIG.gc_per_cycle_collect is False

    def test_threshold0_is_raised_well_above_the_cpython_default(self):
        """Raising threshold0 is what attacks promotion at source: objects are promoted only
        by surviving a collection, so collecting less often in gen-0 means most die by
        refcounting first. CPython's own default is 2000 since 3.12."""
        from qapbot.config import CONFIG
        assert CONFIG.gc_threshold0 >= 20_000

    @pytest.mark.parametrize("env,expected", [("0", False), ("false", False), ("1", True)])
    def test_gc_automatic_env_override(self, monkeypatch, env, expected):
        """GC_AUTOMATIC keeps its historical name; only the default flipped."""
        from qapbot.config import load_config
        monkeypatch.setenv("GC_AUTOMATIC", env)
        assert load_config().gc_automatic is expected

    def test_threshold_override_and_bad_value_fallback(self, monkeypatch):
        from qapbot.config import load_config
        monkeypatch.setenv("GC_THRESHOLD0", "12345")
        assert load_config().gc_threshold0 == 12345
        monkeypatch.setenv("GC_THRESHOLD0", "lots")
        assert load_config().gc_threshold0 == 50_000


class TestWiring:
    """Source-level: the real paths need a live bot, so pin the properties that would
    silently reintroduce the pump."""

    def test_per_cycle_collect_is_gated_on_the_config_flag(self):
        import inspect
        import QapBot
        src = inspect.getsource(QapBot.periodic_main)
        assert "CONFIG.gc_per_cycle_collect" in src, (
            "the per-cycle gc.collect(1) is no longer gated — it would run unconditionally "
            "again, which is exactly the promotion pump"
        )

    def test_gc_stats_delta_reports_per_generation_counts(self):
        """The tuning instrument: [GC-AUTO] only logs collections >=0.5s, so without this we
        cannot see how OFTEN CPython collects, which is what threshold0 tuning needs."""
        import QapBot
        QapBot._gc_stats_prev = None
        assert "baseline" in QapBot._gc_stats_delta()      # first call has no baseline
        gc.collect(0)
        out = QapBot._gc_stats_delta()
        assert "gen0=" in out and "gen2=" in out, out

    def test_gc_stats_delta_never_raises(self, monkeypatch):
        import QapBot
        monkeypatch.setattr(gc, "get_stats", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert QapBot._gc_stats_delta() == "n/a"
