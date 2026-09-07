"""handle_memory_profile()'s on-demand-trace startup no longer runs an unscoped gc.collect()
(tracker #0106, 2026-09-07).

qaplop's own report flagged the wrong fix first: wrapping the call in `asyncio.to_thread()` to
match `save_memtrace_snapshot()`'s existing pattern. That looked right but doesn't work —
CPython's `gc.collect()` holds the GIL for its entire sweep regardless of which OS thread calls
it (releasing it mid-collection would let another thread mutate refcounts while the collector is
tracing reachability). Proof this doesn't help was already sitting in the same night's log:
`save_memtrace_snapshot()`'s gc.collect() IS thread-offloaded and still produced an 8.852s
`[GC-SCHEDULED]` stall.

The real bug: the call was a bare `gc.collect()`, which defaults to a FULL (gen-2) sweep. This
process disables automatic collection specifically to keep full sweeps confined to the nightly
maintenance window ([GC-POLICY]) — this one on-demand call broke that, measured on PROD at
16.552s (`collected=1,854,123`) versus ~1-7s for the routine per-cycle gen(1) collect. Since this
particular collect only exists to prime a differential baseline (not to produce the actual
report — that's save_memtrace_snapshot()'s job later), gen(1) is both cheaper and the more
appropriate scope: it catches recently-created garbage, which is what "growth since baseline"
cares about, without chasing old-generation cycles that are irrelevant to a differential view.
"""
# pyright: reportPrivateUsage=false
from __future__ import annotations

import asyncio
import gc
import os
import tracemalloc
from unittest.mock import MagicMock

os.environ.setdefault("DISCORD_TOKEN", "test-token")

import QBcore
from qapbot.QBdiscocmdshelper_admin_command import handle_memory_profile


def _fake_cache() -> MagicMock:
    cache = MagicMock()
    cache.coc_clan_cache.cache = {}
    cache.coc_clan_cache.get_memory_usage_mb.return_value = 0.0
    for attr in (
        "clan_name_cache", "subscriptions", "leaderboard_messages", "user_accounts",
        "notification_state", "clan_history", "history_cache", "temp_war_stats",
        "temp_war_objects", "server_config",
    ):
        setattr(cache, attr, {})
    return cache


class TestOnDemandTraceStartupGcScope:
    def setup_method(self):
        # This branch requires tracemalloc NOT already active and no trace pending — force a
        # known-clean starting state regardless of test execution order.
        if tracemalloc.is_tracing():
            tracemalloc.stop()
        QBcore.memtrace_pending = False
        QBcore.memtrace_baseline = None

    def teardown_method(self):
        if tracemalloc.is_tracing():
            tracemalloc.stop()
        QBcore.memtrace_pending = False
        QBcore.memtrace_baseline = None

    def test_priming_collect_is_scoped_to_generation_1_not_a_full_sweep(self, monkeypatch):
        """Pins the actual fix: the argument passed to gc.collect(), not just that it runs."""
        calls: list[tuple] = []
        real_collect = gc.collect

        def _spy_collect(*args, **kwargs):
            calls.append(args)
            return real_collect(*args, **kwargs)

        monkeypatch.setattr(gc, "collect", _spy_collect)

        asyncio.run(handle_memory_profile(_fake_cache()))

        assert calls, "gc.collect was never called during trace startup"
        assert calls[0] == (1,), (
            f"expected gc.collect(1) (gen-0+1 only, matching [CYCLE-CLEANUP]'s scope) — "
            f"got gc.collect{calls[0]!r} instead, which is a full/gen-2 sweep whenever the "
            f"tuple is empty"
        )

    def test_priming_collect_still_runs_off_the_event_loop_thread(self, monkeypatch):
        """The asyncio.to_thread() wrap is kept for coroutine hygiene even though it doesn't
        fix the GIL-blocking behaviour on its own — this pins that it's still there."""
        calling_thread_names: list[str] = []
        real_collect = gc.collect

        def _spy_collect(*args, **kwargs):
            import threading
            calling_thread_names.append(threading.current_thread().name)
            return real_collect(*args, **kwargs)

        monkeypatch.setattr(gc, "collect", _spy_collect)

        asyncio.run(handle_memory_profile(_fake_cache()))

        assert calling_thread_names, "gc.collect was never called"
        assert calling_thread_names[0] != "MainThread", (
            "priming gc.collect() ran directly on the calling coroutine's thread instead of "
            "via asyncio.to_thread()"
        )

    def test_trace_actually_starts_and_a_baseline_is_recorded(self):
        """The scope/threading changes must not have broken the function's actual job."""
        asyncio.run(handle_memory_profile(_fake_cache()))

        assert tracemalloc.is_tracing()
        assert QBcore.memtrace_pending is True
        assert QBcore.memtrace_baseline is not None
