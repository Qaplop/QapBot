"""
Tests for QapBot.run_nightly_maintenance_routine().

Duration tracking was the original subject (added 2026-08-03, for the /status and
/admin Check Logs nightly-maintenance duration reporting — see
qapbot/QBdiscocmdshelper_admin_command.py's format_nightly_maintenance_stats and
QBcore.nightly_maintenance_durations).

2026-09-01 added the budget-parity test below. The routine is shared by the scheduled
03:00 UTC task and /admin "Execute Nightly Maintenance", and those two paths had drifted:
/admin passed its own much shorter migration budget via a `migration_time_budget_seconds`
override. That override is gone — /admin now means "do tonight's maintenance now",
identically — and the test pins it, since the only other thing keeping the two callers in
step is that they happen to call the same function with the same arguments today.
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportMissingParameterType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

import os
from collections import deque

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
import QapBot  # noqa: E402
import QBcore  # noqa: E402


class _FakeDBManager:
    def __init__(self) -> None:
        self.migration_called = False

    async def nightly_db_maintenance(self) -> str:
        return "OK"

    async def run_history_migration(
        self, time_budget_seconds: float, row_budget: int | None = None
    ) -> None:
        self.migration_called = True
        self.migration_budgets = (time_budget_seconds, row_budget)


@pytest.mark.asyncio
async def test_run_nightly_maintenance_routine_appends_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(QapBot, "_archive_move_nightly", lambda: None)

    async def _fake_warm(force_refresh: bool = False) -> None:
        return None

    monkeypatch.setattr(QapBot, "_warm_global_db_stats_cache", _fake_warm)
    monkeypatch.setattr(QBcore, "nightly_maintenance_durations", deque(maxlen=10))

    result = await QapBot.run_nightly_maintenance_routine(_FakeDBManager(), run_migration=False)

    assert result == "OK"
    assert len(QBcore.nightly_maintenance_durations) == 1
    assert QBcore.nightly_maintenance_durations[0] >= 0.0


@pytest.mark.asyncio
async def test_run_nightly_maintenance_routine_keeps_only_last_10(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(QapBot, "_archive_move_nightly", lambda: None)

    async def _fake_warm(force_refresh: bool = False) -> None:
        return None

    monkeypatch.setattr(QapBot, "_warm_global_db_stats_cache", _fake_warm)
    monkeypatch.setattr(QBcore, "nightly_maintenance_durations", deque(maxlen=10))

    for _ in range(12):
        await QapBot.run_nightly_maintenance_routine(_FakeDBManager(), run_migration=False)

    assert len(QBcore.nightly_maintenance_durations) == 10


@pytest.mark.asyncio
async def test_run_nightly_maintenance_routine_runs_migration_when_due(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(QapBot, "_archive_move_nightly", lambda: None)

    async def _fake_warm(force_refresh: bool = False) -> None:
        return None

    monkeypatch.setattr(QapBot, "_warm_global_db_stats_cache", _fake_warm)
    monkeypatch.setattr(QBcore, "nightly_maintenance_durations", deque(maxlen=10))

    fake_db = _FakeDBManager()
    await QapBot.run_nightly_maintenance_routine(fake_db, run_migration=True)

    assert fake_db.migration_called is True
    assert len(QBcore.nightly_maintenance_durations) == 1


@pytest.mark.asyncio
async def test_migration_step_uses_the_configured_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both callers get the CONFIG budgets — there is no per-caller override any more."""
    monkeypatch.setattr(QapBot, "_archive_move_nightly", lambda: None)

    async def _fake_warm(force_refresh: bool = False) -> None:
        return None

    monkeypatch.setattr(QapBot, "_warm_global_db_stats_cache", _fake_warm)
    monkeypatch.setattr(QBcore, "nightly_maintenance_durations", deque(maxlen=10))

    fake_db = _FakeDBManager()
    await QapBot.run_nightly_maintenance_routine(fake_db, run_migration=True)

    assert fake_db.migration_called
    assert fake_db.migration_budgets == (
        QapBot.CONFIG.history_migration_time_budget_minutes * 60,
        QapBot.CONFIG.history_migration_nightly_row_budget,
    )


def test_routine_takes_no_per_caller_budget_override() -> None:
    """Structural guard: re-adding a budget parameter is how the two callers drifted
    apart before. A behavioural test cannot catch that — the signature is the contract."""
    import inspect

    params = set(inspect.signature(QapBot.run_nightly_maintenance_routine).parameters)
    assert params == {"db_mgr", "run_migration"}, (
        f"unexpected parameters {sorted(params)} — /admin and the scheduled nightly run "
        f"must stay byte-for-byte the same call"
    )
