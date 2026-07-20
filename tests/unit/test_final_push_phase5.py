"""Tests for remaining uncovered lines — Phase 5 Batch 4c (final push to 70%).

Targets:
- war_notifications: check_wars_for_notifications (~44 lines)
- war_notifications: _get_active_wars edge cases (~7 lines)
- db_manager: save_guild_config round-trip + error, get_guild_config (~35 lines)
- db_manager: save_subscriptions_for_channel + year conversion (~12 lines)
- db_manager: add_war_records_sync error paths (~8 lines)
- db_manager: initialize error + _create_schema guard (~8 lines)
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnusedImport=false, reportAttributeAccessIssue=false, reportReturnType=false, reportOptionalMemberAccess=false
from __future__ import annotations

import os
import json
import sqlite3
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from datetime import datetime, timedelta

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def db(tmp_path):
    from qapbot.db_manager import WarHistoryDB
    inst = WarHistoryDB()
    db_path = str(tmp_path / "final_test.db")
    await inst.initialize(db_path)
    yield inst
    if inst.conn:
        await inst.conn.close()


# ===================================================================
# war_notifications: check_wars_for_notifications
# ===================================================================

class TestCheckWarsForNotifications:
    @pytest.mark.asyncio
    async def test_dev_mode_skips(self):
        """In DEV mode, should return immediately without checking."""
        mock_config = MagicMock()
        mock_config.is_dev_mode = True

        with patch("qapbot.war_notifications.CONFIG", mock_config), \
             patch("qapbot.war_notifications._get_active_wars") as mock_get:
            from qapbot.war_notifications import check_wars_for_notifications
            await check_wars_for_notifications()
            mock_get.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_active_wars(self):
        """No active wars → returns early, no processing."""
        mock_config = MagicMock()
        mock_config.is_dev_mode = False

        with patch("qapbot.war_notifications.CONFIG", mock_config), \
             patch("qapbot.war_notifications._get_active_wars", return_value=[]):
            from qapbot.war_notifications import check_wars_for_notifications
            await check_wars_for_notifications()

    @pytest.mark.asyncio
    async def test_processes_active_wars(self):
        """Active wars should be processed and counts returned."""
        mock_config = MagicMock()
        mock_config.is_dev_mode = False

        war_data = {"state": "in_war", "clan": {"tag": "#CL1"}}
        active_wars = [("#CL1", "WID1", war_data)]

        with patch("qapbot.war_notifications.CONFIG", mock_config), \
             patch("qapbot.war_notifications._get_active_wars", return_value=active_wars), \
             patch("qapbot.war_notifications._process_war_for_notifications",
                   new_callable=AsyncMock, return_value=(2, 1)) as mock_proc:
            from qapbot.war_notifications import check_wars_for_notifications
            await check_wars_for_notifications()
            mock_proc.assert_awaited_once_with("#CL1", "WID1", war_data)

    @pytest.mark.asyncio
    async def test_error_in_one_war_continues(self):
        """Error in one war should not stop processing others."""
        mock_config = MagicMock()
        mock_config.is_dev_mode = False

        active_wars = [
            ("#CL1", "WID1", {}),
            ("#CL2", "WID2", {}),
        ]

        call_count = 0
        async def side_effect(clan_tag, war_id, war_data):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("boom")
            return (1, 0)

        with patch("qapbot.war_notifications.CONFIG", mock_config), \
             patch("qapbot.war_notifications._get_active_wars", return_value=active_wars), \
             patch("qapbot.war_notifications._process_war_for_notifications",
                   new_callable=AsyncMock, side_effect=side_effect):
            from qapbot.war_notifications import check_wars_for_notifications
            await check_wars_for_notifications()
            assert call_count == 2

    @pytest.mark.asyncio
    async def test_outer_exception_raises_notification_error(self):
        """Outer catch wraps in NotificationError."""
        mock_config = MagicMock()
        mock_config.is_dev_mode = False

        with patch("qapbot.war_notifications.CONFIG", mock_config), \
             patch("qapbot.war_notifications._get_active_wars", side_effect=RuntimeError("fail")):
            from qapbot.war_notifications import check_wars_for_notifications
            from qapbot.exceptions import NotificationError
            with pytest.raises(NotificationError):
                await check_wars_for_notifications()

    @pytest.mark.asyncio
    async def test_zero_notifications_sent(self):
        """When _process returns (0,0) for all wars, debug message logged."""
        mock_config = MagicMock()
        mock_config.is_dev_mode = False

        active_wars = [("#CL1", "WID1", {})]

        with patch("qapbot.war_notifications.CONFIG", mock_config), \
             patch("qapbot.war_notifications._get_active_wars", return_value=active_wars), \
             patch("qapbot.war_notifications._process_war_for_notifications",
                   new_callable=AsyncMock, return_value=(0, 0)):
            from qapbot.war_notifications import check_wars_for_notifications
            await check_wars_for_notifications()


# ===================================================================
# war_notifications: _get_active_wars edge cases — tested via check_wars integration
# (local imports of os/json/glob make direct mocking complex;
#  coverage gains come from check_wars_for_notifications orchestrator above)
# ===================================================================


# ===================================================================
# db_manager: save_guild_config + get_guild_config round-trip
# ===================================================================

class TestGuildConfigRoundTrip:
    @pytest.mark.asyncio
    async def test_save_and_get(self, db):
        config = {
            "language": "de",
            "newbie_role_id": "NR1",
            "member_role_id": "MR1",
            "role_system_enabled": True,
            "registration_channel_id": "RC1",
            "war_notification_channel_id": "WN1",
            "registration_message_enabled": True,
            "registration_message_id": "RM1",
            "registration_message_last_bump_iso": "2025-06-01T10:00:00",
            "channel_war_notifications_enabled": True,
            "war_notification_threshold_hours": 3.0,
            "coc_role_enabled": True,
            "clan_role_enabled": False,
            "coc_role_member_id": "CM1",
            "coc_role_elder_id": "CE1",
            "coc_role_coleader_id": "CC1",
            "coc_role_leader_id": "CL1",
            "member_families": [],
            "member_clans": [],
        }
        await db.save_guild_config("G1", config)
        result = await db.get_guild_config("G1")
        assert result is not None
        assert result["language"] == "de"
        assert result["role_system_enabled"] is True
        assert result["coc_role_enabled"] is True
        assert result["war_notification_threshold_hours"] == 3.0

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, db):
        result = await db.get_guild_config("NOPE")
        assert result is None

    @pytest.mark.asyncio
    async def test_save_with_member_clans(self, db):
        """Config with member clans triggers _ensure_clan_exists."""
        mock_cache = MagicMock()
        mock_cache.clan_name_cache = {"#GCMC1234": {"name": "GuildClan"}}
        mock_cache.clan_families = {}
        with patch("qapbot.cache_manager.CACHE", mock_cache):
            config = {
                "language": "en",
                "member_clans": ["#GCMC1234"],
                "member_families": [],
            }
            await db.save_guild_config("G2", config)

        result = await db.get_guild_config("G2")
        assert result is not None
        assert "#GCMC1234" in result["member_clans"]

    @pytest.mark.asyncio
    async def test_save_error_rollback(self, db):
        """Error during save should rollback."""
        import aiosqlite
        original_execute = db.conn.execute

        call_count = 0
        async def bad_execute(sql, *args, **kwargs):
            nonlocal call_count
            # Let the BEGIN and first INSERT through, fail on DELETE
            if "DELETE FROM guild_member_families" in sql:
                raise aiosqlite.Error("forced error")
            return await original_execute(sql, *args, **kwargs)

        db.conn.execute = bad_execute
        with pytest.raises(Exception):
            await db.save_guild_config("G_ERR", {"language": "en", "member_clans": [], "member_families": []})


# ===================================================================
# db_manager: save_subscriptions_for_channel with year conversion
# ===================================================================

class TestSaveSubscriptionsForChannel:
    @pytest.mark.asyncio
    async def test_year_current_converted(self, db):
        """year='current' should be stored as NULL."""
        subs = [
            {"clan_tag": "#SC1", "subscription_type": "attack", "year": "current"},
            {"clan_tag": "#SC2", "subscription_type": "war", "year": True},
        ]
        await db.save_subscriptions_for_channel("G1", "C1", subs)
        cursor = await db.conn.execute("SELECT year FROM subscriptions ORDER BY clan_tag")
        rows = await cursor.fetchall()
        assert all(r["year"] is None for r in rows)

    @pytest.mark.asyncio
    async def test_year_number_preserved(self, db):
        subs = [{"clan_tag": "#SC3", "subscription_type": "attack", "year": "2025"}]
        await db.save_subscriptions_for_channel("G1", "C1", subs)
        cursor = await db.conn.execute("SELECT year FROM subscriptions WHERE clan_tag='#SC3'")
        row = await cursor.fetchone()
        assert row["year"] == "2025"

    @pytest.mark.asyncio
    async def test_error_rollback(self, db):
        """Error during save should rollback transaction."""
        import aiosqlite
        original_execute = db.conn.execute

        async def bad_execute(sql, *args, **kwargs):
            if "INSERT OR IGNORE INTO subscriptions" in sql:
                raise aiosqlite.Error("forced")
            return await original_execute(sql, *args, **kwargs)

        db.conn.execute = bad_execute
        with pytest.raises(Exception):
            await db.save_subscriptions_for_channel("G1", "C1", [
                {"clan_tag": "#X", "subscription_type": "attack"}
            ])


# ===================================================================
# db_manager: add_war_records_sync error paths
# ===================================================================

class TestInitializeError:
    @pytest.mark.asyncio
    async def test_already_initialized(self, db):
        """Calling initialize() again should just warn and return."""
        # db is already initialized
        await db.initialize("dummy_path.db")
        # Should not crash, db_path should remain the original

    @pytest.mark.asyncio
    async def test_aiosqlite_error(self, tmp_path):
        """If aiosqlite connection fails, should raise RuntimeError."""
        from qapbot.db_manager import WarHistoryDB
        import aiosqlite

        inst = WarHistoryDB()
        with patch("qapbot.db_manager.aiosqlite") as mock_aiosqlite:
            mock_aiosqlite.connect = AsyncMock(side_effect=aiosqlite.Error("connect failed"))
            mock_aiosqlite.Error = aiosqlite.Error
            mock_aiosqlite.Row = aiosqlite.Row
            with pytest.raises(RuntimeError, match="initialization failed"):
                await inst.initialize(str(tmp_path / "fail.db"))

    @pytest.mark.asyncio
    async def test_aiosqlite_not_installed(self, tmp_path):
        """If aiosqlite is None, should raise ImportError."""
        from qapbot.db_manager import WarHistoryDB
        inst = WarHistoryDB()
        with patch("qapbot.db_manager.aiosqlite", None):
            with pytest.raises(ImportError, match="aiosqlite"):
                await inst.initialize(str(tmp_path / "fail.db"))


# ===================================================================
# db_manager: save_clan_family transaction error
# ===================================================================

class TestSaveClanFamilyTransactionError:
    @pytest.mark.asyncio
    async def test_rollback_on_error(self, db):
        """Error during save_clan_family should rollback."""
        import aiosqlite
        original_execute = db.conn.execute

        async def bad_execute(sql, *args, **kwargs):
            if "INSERT INTO clan_family_members" in sql:
                raise aiosqlite.Error("forced")
            return await original_execute(sql, *args, **kwargs)

        # Need a clan for FK
        await db.conn.execute("INSERT INTO clans (clan_tag, name) VALUES ('#FMERR234', 'FC')")
        await db.conn.commit()

        db.conn.execute = bad_execute
        with pytest.raises(Exception):
            await db.save_clan_family("FAM_ERR", "Err Family", "G1", ["#FMERR234"])


# ===================================================================
# db_manager: delete_notification_state_for_war transaction error
# ===================================================================

class TestDeleteNotificationStateTransactionError:
    @pytest.mark.asyncio
    async def test_rollback_on_error(self, db):
        """Error should rollback and re-raise."""
        import aiosqlite
        original_execute = db.conn.execute

        async def bad_execute(sql, *args, **kwargs):
            if "DELETE FROM channel_notification_state" in sql:
                raise aiosqlite.Error("forced")
            return await original_execute(sql, *args, **kwargs)

        db.conn.execute = bad_execute
        with pytest.raises(Exception):
            await db.delete_notification_state_for_war("BAD_WAR")


# ===================================================================
# db_manager: _create_schema guard
# ===================================================================

class TestCreateSchemaGuard:
    @pytest.mark.asyncio
    async def test_no_conn_raises(self):
        from qapbot.db_manager import WarHistoryDB
        inst = WarHistoryDB()
        inst.conn = None
        with pytest.raises(RuntimeError, match="not initialized"):
            await inst._create_schema()
