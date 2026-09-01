"""
Tests for QapBot.is_history_migration_due().

Successor to test_is_monthly_migration_due.py, rewritten for the 2026-09-01 rolling
redesign. The old function had two modes (`ignore_in_process_claim`), a `day == 1` gate,
and an in-memory "claim" — all of which existed only to stop a MONTHLY job re-firing
within the month it had already run. A nightly walk needs none of that: the question is
simply "is the cutoff we reached still behind the cutoff we want", derived fresh from
persisted state every call and naturally idempotent.

There is deliberately no on/off switch to test: the 2026-09-01 stopgap
CONFIG.history_migration_enabled was removed once the redesign made it pointless. A short,
non-blocking, budgeted, resumable nightly job has no state in which you would want it
permanently off, and an "off" setting would silently stop hot-DB retention with nothing to
alarm on. HISTORY_MIGRATION_NIGHTLY_ROW_BUDGET=0 remains as an immediate no-op if one is
ever genuinely needed.
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportMissingParameterType=false, reportUnknownVariableType=false, reportAttributeAccessIssue=false
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
import QapBot  # noqa: E402


class _FakeDBManager:
    """Models only what is_history_migration_due() touches."""

    def __init__(self, reached: str | None, oldest: str | None, target: str = "2026-07-03"):
        self._reached = reached
        self._oldest = oldest
        self._target = target
        self.oldest_probe_calls = 0

    def _history_cutoff(self):
        return self._target, self._target[:7]

    async def get_history_migration_cutoff_reached(self):
        return self._reached

    async def _oldest_migratable_date(self, cutoff_date, since=None):
        self.oldest_probe_calls += 1
        return self._oldest

    async def has_history_migration_work(self) -> bool:
        """Mirrors WarHistoryDB.has_history_migration_work() so these tests exercise the
        real decision logic rather than a stub that could drift from it."""
        target, _ = self._history_cutoff()
        reached = await self.get_history_migration_cutoff_reached()
        if reached is None:
            return await self._oldest_migratable_date(target) is not None
        return reached < target


@pytest.mark.asyncio
class TestDueLogic:
    async def test_due_when_reached_cutoff_is_behind_target(self, monkeypatch):
        monkeypatch.setattr(
            QapBot, "CACHE",
            SimpleNamespace(db_manager=_FakeDBManager(reached="2026-07-01", oldest=None)),
        )
        assert await QapBot.is_history_migration_due() is True

    async def test_not_due_when_reached_cutoff_equals_target(self, monkeypatch):
        """Steady state on a night when nothing new has aged out."""
        monkeypatch.setattr(
            QapBot, "CACHE",
            SimpleNamespace(db_manager=_FakeDBManager(reached="2026-07-03", oldest=None)),
        )
        assert await QapBot.is_history_migration_due() is False

    async def test_not_due_when_reached_cutoff_is_ahead_of_target(self, monkeypatch):
        """Retention was lengthened, or the clock moved back. Never migrate backwards."""
        monkeypatch.setattr(
            QapBot, "CACHE",
            SimpleNamespace(db_manager=_FakeDBManager(reached="2026-08-01", oldest=None)),
        )
        assert await QapBot.is_history_migration_due() is False

    async def test_no_marker_and_no_old_rows_is_not_due(self, monkeypatch):
        """First run against a fresh/empty DB must not report due forever."""
        db = _FakeDBManager(reached=None, oldest=None)
        monkeypatch.setattr(QapBot, "CACHE", SimpleNamespace(db_manager=db))
        assert await QapBot.is_history_migration_due() is False
        assert db.oldest_probe_calls == 1

    async def test_no_marker_but_old_rows_present_is_due(self, monkeypatch):
        """First run after deploy, with real data below the cutoff."""
        db = _FakeDBManager(reached=None, oldest="2026-06-10")
        monkeypatch.setattr(QapBot, "CACHE", SimpleNamespace(db_manager=db))
        assert await QapBot.is_history_migration_due() is True
        assert db.oldest_probe_calls == 1

    async def test_no_db_manager_is_not_due(self, monkeypatch):
        monkeypatch.setattr(QapBot, "CACHE", SimpleNamespace(db_manager=None))
        assert await QapBot.is_history_migration_due() is False
