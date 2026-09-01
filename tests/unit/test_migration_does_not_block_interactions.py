"""
The single most important regression guard of the 2026-09-01 redesign.

On 2026-09-01 the hot->history migration held QBcore.db_maintenance_mode for 9.6 hours
of a 14.1-hour span (68% duty cycle), refusing every slash command, button and modal —
including ones that touch no database at all, like /ping. It never needed to: the batched
migration holds no exclusive lock. It runs short transactions on the async writer
connection while user commands read through the separate 8-connection sync pool in WAL
mode, and WAL readers never block on a writer. The flag was simply inherited from
nightly_db_maintenance(), which genuinely does need it (VACUUM takes EXCLUSIVE and drains
the pool).

The fix splits the two states:
  - db_maintenance_mode  -> HARD block (VACUUM/REINDEX, schema+index builds, fast bulk)
  - db_migration_active  -> ADVISORY only, never blocks an interaction

These tests pin that separation from both guard entry points. If either starts consulting
db_migration_active again, the outage class comes straight back, and nothing else in the
suite would notice.
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportMissingParameterType=false, reportAttributeAccessIssue=false
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")

import QBcore  # noqa: E402
from qapbot.ui_common import check_maintenance_block  # noqa: E402


def _interaction():
    it = MagicMock()
    it.guild = MagicMock()
    it.guild.id = 123
    it.command = MagicMock()
    it.command.name = "ping"
    it.data = {}
    it.response = MagicMock()
    it.response.is_done = MagicMock(return_value=False)
    it.response.send_message = AsyncMock()
    it.followup = MagicMock()
    it.followup.send = AsyncMock()
    return it


@pytest.fixture(autouse=True)
def _clean_flags(monkeypatch):
    monkeypatch.setattr(QBcore, "maintenance_mode", False)
    monkeypatch.setattr(QBcore, "db_maintenance_mode", False)
    monkeypatch.setattr(QBcore, "db_migration_active", False)
    monkeypatch.setattr(QBcore, "_guard_reject_reason", None, raising=False)
    monkeypatch.setattr(QBcore, "_guard_reject_count", 0, raising=False)
    monkeypatch.setattr(QBcore, "_guard_reject_last_ts", 0.0, raising=False)


@pytest.mark.asyncio
class TestComponentGuard:
    """qapbot.ui_common.check_maintenance_block — buttons, selects, modals."""

    async def test_migration_alone_does_not_block(self):
        QBcore.db_migration_active = True
        it = _interaction()

        assert await check_maintenance_block(it) is False
        it.response.send_message.assert_not_awaited()

    async def test_db_maintenance_still_blocks(self):
        QBcore.db_maintenance_mode = True
        it = _interaction()

        assert await check_maintenance_block(it) is True
        it.response.send_message.assert_awaited_once()

    async def test_full_maintenance_still_blocks(self):
        QBcore.maintenance_mode = True
        it = _interaction()

        assert await check_maintenance_block(it) is True

    async def test_migration_plus_maintenance_blocks_for_the_maintenance(self):
        """Both flags set (a nightly window that migrates then VACUUMs): the hard block
        wins, and the advisory flag neither adds nor removes anything."""
        QBcore.db_migration_active = True
        QBcore.db_maintenance_mode = True

        assert await check_maintenance_block(_interaction()) is True

    async def test_nothing_set_does_not_block(self):
        assert await check_maintenance_block(_interaction()) is False


@pytest.mark.asyncio
class TestSlashCommandGuard:
    """QBcore._maintenance_interaction_check — slash commands."""

    @pytest.fixture(autouse=True)
    def _initialized(self, monkeypatch):
        # The guard refuses everything before on_ready finishes; bypass that here so the
        # maintenance branches are what is actually under test.
        monkeypatch.setattr(QBcore.bot, "fully_initialized", True, raising=False)

    async def test_migration_alone_does_not_block(self):
        QBcore.db_migration_active = True
        it = _interaction()

        assert await QBcore._maintenance_interaction_check(it) is True
        it.response.send_message.assert_not_awaited()

    async def test_db_maintenance_still_blocks(self):
        QBcore.db_maintenance_mode = True
        it = _interaction()

        assert await QBcore._maintenance_interaction_check(it) is False
        it.response.send_message.assert_awaited_once()


class TestRejectionAccounting:
    """The forensic trail that was missing on 2026-09-01 — a 14-hour outage produced
    zero [MAINTENANCE-GUARD] lines in a 596k-line log."""

    def test_first_rejection_logs_at_info(self, caplog):
        with caplog.at_level("INFO"):
            QBcore.record_interaction_rejection("db_maintenance", "/ping")
        assert any("MAINTENANCE-GUARD" in r.message for r in caplog.records)

    def test_repeat_rejections_are_rate_limited(self, caplog):
        QBcore.record_interaction_rejection("db_maintenance", "/ping")
        with caplog.at_level("INFO"):
            for _ in range(5):
                QBcore.record_interaction_rejection("db_maintenance", "/ping")
        info = [r for r in caplog.records if r.levelname == "INFO"]
        assert not info, "repeat rejections inside an episode must not log at INFO"

    def test_every_nth_rejection_logs_at_info(self, caplog):
        QBcore.record_interaction_rejection("db_maintenance", "/ping")
        with caplog.at_level("INFO"):
            for _ in range(QBcore._GUARD_REJECT_LOG_EVERY):
                QBcore.record_interaction_rejection("db_maintenance", "/ping")
        info = [r for r in caplog.records if r.levelname == "INFO"]
        assert len(info) == 1, f"expected one milestone log, got {len(info)}"

    def test_changed_reason_starts_a_new_episode(self, caplog):
        QBcore.record_interaction_rejection("db_maintenance", "/ping")
        with caplog.at_level("INFO"):
            QBcore.record_interaction_rejection("maintenance_mode", "/status")
        assert any("maintenance_mode" in r.message for r in caplog.records)
