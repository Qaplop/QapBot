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

2026-09-01: CONFIG.history_migration_enabled was added as a master kill-switch in
front of both modes, defaulting to OFF (see qapbot/config.py for why). The
_enable_migration autouse fixture below turns it back on so the pre-existing
due-logic tests keep exercising the logic they were written for; TestKillSwitch
covers the flag itself.
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportMissingParameterType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnusedImport=false, reportAttributeAccessIssue=false
from __future__ import annotations

import dataclasses
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
import QapBot  # noqa: E402


def _config_with_migration_enabled(enabled: bool):
    """Return a real BotConfig copy with only history_migration_enabled flipped.

    dataclasses.replace() rather than monkeypatching the attribute directly:
    BotConfig is a frozen dataclass, so attribute assignment raises.
    """
    return dataclasses.replace(QapBot.CONFIG, history_migration_enabled=enabled)


@pytest.fixture(autouse=True)
def _enable_migration(monkeypatch):
    """Arm the migration kill-switch for this module's due-logic tests.

    CONFIG.history_migration_enabled defaults to False (2026-09-01), which
    short-circuits is_monthly_migration_due() before any of the day-gate /
    in-process-claim logic these tests target ever runs. TestKillSwitch opts
    back out of this fixture to assert the disabled behaviour.
    """
    monkeypatch.setattr(QapBot, "CONFIG", _config_with_migration_enabled(True))


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


@pytest.mark.asyncio
class TestKillSwitch:
    """CONFIG.history_migration_enabled=False must disarm BOTH modes.

    Added 2026-09-01 after the September migration blocked Discord commands for
    68% of a 14-hour span across 114 never-completing per-cycle chunks. This
    function is the single enforcement point — the 03:00 UTC nightly step, the
    opportunistic per-cycle chunk and /admin Execute Nightly Maintenance all gate
    on it — so these two assertions are what actually keep every automatic path off.
    """

    async def test_disabled_blocks_default_mode(self, monkeypatch):
        """Day-1, nothing ever migrated, nothing claimed — maximally "due" — and
        still False, because the kill-switch is checked before any of that."""
        monkeypatch.setattr(QapBot, "CONFIG", _config_with_migration_enabled(False))
        fake_cache = SimpleNamespace(
            db_manager=_FakeDBManager(stored_last_history_migration=None),
            last_history_migration=None,
        )
        monkeypatch.setattr(QapBot, "CACHE", fake_cache)

        assert await QapBot.is_monthly_migration_due() is False

    async def test_disabled_blocks_ignore_in_process_claim_mode(self, monkeypatch):
        """The /admin bypass must not bypass the kill-switch too."""
        monkeypatch.setattr(QapBot, "CONFIG", _config_with_migration_enabled(False))
        fake_cache = SimpleNamespace(
            db_manager=_FakeDBManager(stored_last_history_migration=None),
            last_history_migration=None,
        )
        monkeypatch.setattr(QapBot, "CACHE", fake_cache)

        assert await QapBot.is_monthly_migration_due(ignore_in_process_claim=True) is False

    async def test_disabled_does_not_claim_the_month(self, monkeypatch):
        """Returning False must NOT write CACHE.last_history_migration.

        Claiming here would persist a "done this month" marker for a run that
        never happened — the exact stale-stamp failure mode documented in
        DATABASE_ARCHITECTURE.md (2026-08-01), which silently suppressed every
        automatic retry for the rest of that month.
        """
        monkeypatch.setattr(QapBot, "CONFIG", _config_with_migration_enabled(False))
        fake_cache = SimpleNamespace(
            db_manager=_FakeDBManager(stored_last_history_migration=None),
            last_history_migration=None,
        )
        monkeypatch.setattr(QapBot, "CACHE", fake_cache)

        await QapBot.is_monthly_migration_due()

        assert fake_cache.last_history_migration is None
