"""
Tests for QapBot.run_nightly_maintenance_routine()'s duration tracking,
added 2026-08-03 for the /status and /admin Check Logs nightly-maintenance
duration reporting (see qapbot/QBdiscocmdshelper_admin_command.py's
format_nightly_maintenance_stats and QBcore.nightly_maintenance_durations).
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

    async def monthly_history_migration(self, time_budget_seconds: float) -> None:
        self.migration_called = True


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
    await QapBot.run_nightly_maintenance_routine(fake_db, run_migration=True, migration_time_budget_seconds=5.0)

    assert fake_db.migration_called is True
    assert len(QBcore.nightly_maintenance_durations) == 1
