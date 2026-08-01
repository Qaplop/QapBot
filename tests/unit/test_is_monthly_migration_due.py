"""
Tests for QapBot.is_monthly_migration_due()'s ignore_in_process_claim mode,
added 2026-08-01 alongside monthly_history_migration()'s time-budget/chunking
support (see qapbot/docs/DATABASE_ARCHITECTURE.md's Migration History).

Background: the default behavior (used by the scheduled nightly task and the
standalone safety-net path) claims the migration as "due" in-memory the moment
it returns True, and gates on day == 1 — both exist to stop the two automatic
paths from double-firing or looping unsupervised. But that means once a bot
process has auto-triggered a chunk once this month, it won't fire again
automatically for the rest of that process's uptime, even though the real
backlog (tracked in bot_metadata) is still incomplete. ignore_in_process_claim
exists so a human explicitly invoking /admin Execute Nightly Maintenance can
always get "one more chunk" regardless of that in-memory state or the day of
the month — these tests pin down exactly that bypass.
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportMissingParameterType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnusedImport=false, reportAttributeAccessIssue=false
from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
import QapBot  # noqa: E402


class _FakeDBManager:
    def __init__(self, stored_last_history_migration: str | None):
        self._stored = stored_last_history_migration

    async def get_bot_metadata(self, key: str):
        assert key == "last_history_migration"
        return self._stored


@pytest.mark.asyncio
class TestIgnoreInProcessClaim:
    async def test_bypasses_prior_in_process_claim_when_still_not_done(self, monkeypatch):
        """A prior automatic trigger already claimed this month in-memory, but
        bot_metadata shows no completion yet — ignore_in_process_claim=True
        must still report due, since the real backlog isn't finished."""
        now = datetime.now(timezone.utc)
        fake_cache = SimpleNamespace(
            db_manager=_FakeDBManager(stored_last_history_migration=None),
            last_history_migration=now,  # already "claimed" in-memory this month
        )
        monkeypatch.setattr(QapBot, "CACHE", fake_cache)

        assert await QapBot.is_monthly_migration_due(ignore_in_process_claim=True) is True

    async def test_bypasses_day_gate(self, monkeypatch):
        """Default mode only fires on day==1; ignore_in_process_claim must not
        care what day it is — an operator can trigger a chunk any day."""
        fake_cache = SimpleNamespace(
            db_manager=_FakeDBManager(stored_last_history_migration=None),
            last_history_migration=None,
        )
        monkeypatch.setattr(QapBot, "CACHE", fake_cache)

        # Regardless of real "today", the persisted state (not done) is what matters.
        assert await QapBot.is_monthly_migration_due(ignore_in_process_claim=True) is True

    async def test_respects_real_completion_for_current_month(self, monkeypatch):
        """bot_metadata shows a completion already persisted for this exact
        year/month — ignore_in_process_claim=True must report NOT due."""
        now = datetime.now(timezone.utc)
        fake_cache = SimpleNamespace(
            db_manager=_FakeDBManager(stored_last_history_migration=now.isoformat()),
            last_history_migration=None,
        )
        monkeypatch.setattr(QapBot, "CACHE", fake_cache)

        assert await QapBot.is_monthly_migration_due(ignore_in_process_claim=True) is False

    async def test_default_mode_still_respects_in_process_claim(self, monkeypatch):
        """Sanity check the default (automatic-path) behavior is unchanged:
        already claimed this month in-memory → not due again, even though
        bot_metadata shows no real completion — same as before this feature."""
        now = datetime.now(timezone.utc)
        fake_cache = SimpleNamespace(
            db_manager=_FakeDBManager(stored_last_history_migration=None),
            last_history_migration=now,
        )
        monkeypatch.setattr(QapBot, "CACHE", fake_cache)

        assert await QapBot.is_monthly_migration_due() is False
