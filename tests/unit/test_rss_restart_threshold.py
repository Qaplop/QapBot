"""RSS-triggered self-restart (tracker #0106, 2026-09-08).

A STOPGAP, deliberately. The Python heap climbs ~1 GB/hour on PROD for reasons still unfound —
the SQLite retuning reduced SQLite's own footprint but did not touch the climb (RSS still went
4.0 GB -> 8.5 GB overnight on 2026-09-07/08). A process restart is the only mechanism proven to
reclaim it.

Restarting BEFORE the box is in trouble is the whole point. The observed failure sequence is:
RSS climbs -> NAS hits ~94-96% RAM -> kernel swaps the Python heap to the HDD-backed swap file
-> every GC and in-memory walk becomes seek-bound -> the 03:00 UTC nightly full GC sweep runs on
a swapped-out heap and hangs the whole NAS for hours.

These tests pin the properties that make the stopgap safe rather than a new failure mode:
the hot-loop guard, the ordering that guarantees a profile reaches disk first, and the fact
that a failure in the check can never take the update cycle down with it.
"""
# pyright: reportPrivateUsage=false
from __future__ import annotations

import dataclasses
import os

os.environ.setdefault("DISCORD_TOKEN", "test-token")

import pytest

import QBcore
from qapbot.config import CONFIG


class TestConfigDefaults:
    def test_enabled_with_a_threshold_below_the_thrashing_point(self):
        """Thrashing began around 6.5-7 GB on a 10 GB box. The threshold has to sit below that
        with room for DSM (~1.5 GB) and the mmap'd DB pages on top."""
        assert CONFIG.rss_restart_enabled is True
        assert 0 < CONFIG.rss_restart_threshold_mb <= 7000
        assert CONFIG.rss_restart_threshold_mb == 6144

    def test_has_a_hot_loop_guard(self):
        """Without a minimum uptime, an RSS already over threshold at startup would restart the
        bot forever — strictly worse than the climb this guards against."""
        assert CONFIG.rss_restart_min_uptime_minutes > 0

    def test_min_uptime_is_clear_of_the_observed_climb_rate(self):
        """PROD takes ~3 h to reach 6 GB. The guard must be well under that (or it would
        suppress real firings) and well over a restart's warm-up (or it would allow a loop)."""
        assert 10 <= CONFIG.rss_restart_min_uptime_minutes <= 120

    @pytest.mark.parametrize("env,field,expected", [
        ("RSS_RESTART_THRESHOLD_MB", "rss_restart_threshold_mb", 5000),
        ("RSS_RESTART_MIN_UPTIME_MINUTES", "rss_restart_min_uptime_minutes", 90),
    ])
    def test_env_overridable_without_a_deploy(self, monkeypatch, env, field, expected):
        from qapbot.config import load_config
        monkeypatch.setenv(env, str(expected))
        assert getattr(load_config(), field) == expected

    def test_can_be_switched_off_entirely(self, monkeypatch):
        from qapbot.config import load_config
        monkeypatch.setenv("RSS_RESTART_ENABLED", "false")
        assert load_config().rss_restart_enabled is False

    def test_unparseable_threshold_falls_back_rather_than_crashing_startup(self, monkeypatch):
        from qapbot.config import load_config
        monkeypatch.setenv("RSS_RESTART_THRESHOLD_MB", "six gigabytes")
        assert load_config().rss_restart_threshold_mb == 6144


class TestArmedFlagContract:
    def test_starts_disarmed(self):
        """A fresh process must not inherit an armed state — the flag only means 'this process
        has already seen the threshold and started a trace'."""
        assert hasattr(QBcore, "rss_restart_armed")
        assert QBcore.rss_restart_armed is False


class TestRestartPrimitives:
    """The restart reuses /admin Maintenance Start + End rather than inventing a shutdown.
    These pin that the pieces it calls still exist and still mean what the code assumes."""

    def test_maintenance_exit_code_is_what_the_wrapper_restarts_on(self):
        """start_qapbot.sh restarts on exit 42 and stops on anything else — if this constant
        ever changed, the 'restart' would silently become a permanent shutdown."""
        assert QBcore.EXIT_CODE_MAINTENANCE == 42

    def test_shutdown_helper_exists_and_is_awaitable(self):
        """do_maintenance_shutdown() is what makes the restart safe: it closes the CoC client
        and the DB with a FULL WAL checkpoint before the process exits."""
        import inspect
        from QBdiscordcmds import do_maintenance_shutdown
        assert inspect.iscoroutinefunction(do_maintenance_shutdown)

    def test_shutdown_helper_marks_cleanup_done(self):
        """Guards the double-close interlock: after do_maintenance_shutdown(), the normal
        async_cleanup() on the way out must be a no-op, or exit would close an already-closed
        DB. Verified by source rather than execution — running it would tear down real
        resources."""
        import inspect
        from QBdiscordcmds import do_maintenance_shutdown
        assert "cleaned_up = True" in inspect.getsource(do_maintenance_shutdown)

    def test_trace_starter_is_shared_with_the_admin_command(self):
        """Both the interactive /admin Memory Profile and the automatic restart start their
        trace through one function. Two hand-maintained copies is exactly how the gen(1)
        scoping fix would silently regress on one path only."""
        import inspect
        from qapbot.QBdiscocmdshelper_admin_command import (
            handle_memory_profile,
            start_memtrace_baseline,
        )
        assert inspect.iscoroutinefunction(start_memtrace_baseline)
        assert "start_memtrace_baseline()" in inspect.getsource(handle_memory_profile)

    def test_priming_collect_stays_scoped_to_gen1(self):
        """The 16.5s Discord freeze came from an unscoped gc.collect() here. Pinned in the
        shared helper so neither caller can reintroduce a full gen-2 sweep."""
        import inspect
        from qapbot.QBdiscocmdshelper_admin_command import start_memtrace_baseline
        src = inspect.getsource(start_memtrace_baseline)
        assert "gc.collect, 1" in src, "priming collect is no longer scoped to gen(1)"


class TestCycleIntegration:
    """Source-level checks on periodic_main's block. The real behaviour needs a running bot,
    a real /proc and an actual process exit, so these pin the properties that would silently
    break the mechanism rather than trying to execute it."""

    @staticmethod
    def _block() -> str:
        import inspect
        import QapBot
        src = inspect.getsource(QapBot.periodic_main)
        start = src.index("[RSS-RESTART]")
        return src[start - 2000:start + 4000]

    def test_fires_only_after_the_profile_is_written(self):
        """The ordering that makes the profile worth having: phase 2 gates on memtrace_pending
        having gone back to False, which is periodic_main's own signal that the report is on
        disk. Without this the process would exit mid-trace and the profile would be lost —
        which is the entire reason for restarting at a known-high RSS in the first place."""
        assert "rss_restart_armed and not QBcore.memtrace_pending" in self._block()

    def test_does_not_fire_during_db_maintenance(self):
        """Restarting mid-REINDEX/VACUUM would abandon an exclusive-locked operation."""
        block = self._block()
        assert "not QBcore.db_maintenance_mode" in block
        assert "not QBcore.maintenance_mode" in block

    def test_closes_resources_before_setting_the_exit_code(self):
        """Order matters: the DB must be closed with its full checkpoint BEFORE the process is
        allowed to exit, or the restart drops an un-checkpointed WAL."""
        block = self._block()
        assert block.index("do_maintenance_shutdown()") < block.index("QBcore.exit_code =")

    def test_check_failure_cannot_break_the_cycle(self):
        """A stopgap must never become the outage. The whole block is wrapped so any failure
        degrades to 'no restart' rather than killing the update loop."""
        assert "[RSS-RESTART] Check failed (non-fatal)" in self._block()
