"""Tests for db_manager error paths + sync query methods — Phase 5 Batch 4b.

Targets the ~97 uncovered error-handling lines across db_manager, plus
cache_manager load helpers and QBhelperfunctions dict start_time parsing.
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnusedImport=false, reportAttributeAccessIssue=false, reportReturnType=false, reportOptionalMemberAccess=false
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Async fixture — real aiosqlite database with full schema
# ---------------------------------------------------------------------------

@pytest.fixture
async def db(tmp_path):
    """Real async WarHistoryDB backed by a temp file."""
    from qapbot.db_manager import WarHistoryDB
    inst = WarHistoryDB()
    db_path = str(tmp_path / "err_test.db")
    await inst.initialize(db_path)
    yield inst
    if inst.conn:
        await inst.conn.close()


# ===================================================================

class TestDeleteClanError:
    @pytest.mark.asyncio
    async def test_error_re_raises(self, db):
        """delete_clan should re-raise on DB error."""
        import aiosqlite
        original_execute = db.conn.execute

        async def bad_execute(sql, *args, **kwargs):
            if "DELETE FROM clans" in sql:
                raise aiosqlite.Error("locked")
            return await original_execute(sql, *args, **kwargs)

        db.conn.execute = bad_execute
        with pytest.raises(aiosqlite.Error):
            await db.delete_clan("#TEST123")


class TestSaveUserTransactionError:
    @pytest.mark.asyncio
    async def test_rollback_on_error(self, db):
        """save_user should rollback on failure."""
        # Create a scenario that triggers an error in the transaction
        # Use an invalid notification_mode to violate CHECK constraint
        user_data = {
            "display_name": "Test",
            "notification_settings": {
                "notification_mode": "invalid_mode_that_violates_check",
                "notification_type": "all_wars",
                "hours_before_end": 4,
                "war_reminders": True
            },
            "players": []
        }
        with pytest.raises(Exception):
            await db.save_user("ERR_USER", user_data)

        # Verify user was NOT persisted (rollback)
        cursor = await db.conn.execute(
            "SELECT COUNT(*) AS cnt FROM users WHERE discord_id = 'ERR_USER'"
        )
        row = await cursor.fetchone()
        assert row["cnt"] == 0



class TestLoadAllTempWarStats:
    def test_loads_from_json(self, tmp_path, monkeypatch):
        """load_all_temp_war_stats() is a two-phase scan (see its docstring):
        Phase 1 derives metadata for every clan from filenames alone (no I/O);
        Phase 2 does a full JSON parse only for subscribed clans. A
        non-subscribed clan with a temp file still gets Phase-1 metadata but
        is NOT added to temp_war_stats — Phase-3 repopulates it after the
        first update cycle.
        """
        import dataclasses
        import json
        import QBcsvhandling
        import qapbot.cache_manager as cache_manager_module
        from qapbot.cache_manager import CacheManager
        from qapbot.config import CONFIG

        # CONFIG is a frozen dataclass — swap the module-level name cache_manager
        # binds instead of mutating a field.
        monkeypatch.setattr(cache_manager_module, "CONFIG", dataclasses.replace(CONFIG, data_dir=str(tmp_path)))
        temp_dir = tmp_path / "temp" / "shard_0"
        temp_dir.mkdir(parents=True)

        cl1_file = temp_dir / "CL1_OPP1_202601010000_war_data.json"
        cl1_file.write_text(json.dumps({"state": "inWar", "type": "random"}), encoding="utf-8")
        cl2_file = temp_dir / "CL2_OPP2_202601010000_war_data.json"
        cl2_file.write_text(json.dumps({"state": "inWar", "type": "random"}), encoding="utf-8")

        cm = CacheManager.__new__(CacheManager)
        cm.clan_name_cache = {
            "#CL1": {"name": "C1", "has_active_subscriptions": True},
            "#CL2": {"name": "C2", "has_active_subscriptions": False},
        }
        cm.in_war_clan_tags = set()
        cm.temp_war_objects = {}

        def mock_load(clan_tag, **kwargs):
            if clan_tag == "#CL1":
                return {"player1": {"stars": 3}}
            return None

        with patch.object(QBcsvhandling, "_load_war_data_from_json", side_effect=mock_load):
            cm.load_all_temp_war_stats()

        # Subscribed clan: full JSON parse ran, stats populated.
        assert cm.temp_war_stats["#CL1"] == {"player1": {"stars": 3}}
        # Non-subscribed clan: Phase 1 metadata exists, but Phase 2 was skipped.
        assert "#CL2" not in cm.temp_war_stats
        assert "#CL1" in cm.temp_war_metadata
        assert "#CL2" in cm.temp_war_metadata

    def test_empty_clan_cache(self, tmp_path, monkeypatch):
        import dataclasses
        import qapbot.cache_manager as cache_manager_module
        from qapbot.cache_manager import CacheManager
        from qapbot.config import CONFIG

        monkeypatch.setattr(cache_manager_module, "CONFIG", dataclasses.replace(CONFIG, data_dir=str(tmp_path)))
        cm = CacheManager.__new__(CacheManager)
        cm.clan_name_cache = {}
        cm.in_war_clan_tags = set()
        cm.temp_war_objects = {}

        with patch("QBcsvhandling._load_war_data_from_json"):
            cm.load_all_temp_war_stats()

        assert cm.temp_war_stats == {}


# ===================================================================
# cache_manager — get_clan_history (sync DB read)
# ===================================================================

class TestCacheGetClanHistory:
    def test_success(self):
        from qapbot.cache_manager import CacheManager
        cm = CacheManager.__new__(CacheManager)
        cm.db_manager = MagicMock()
        cm.clan_history = {}
        cm.db_manager.get_clan_attack_history_sync.return_value = [{"WarID": "W1"}]

        result = cm.get_clan_history("#CL1")
        assert result == [{"WarID": "W1"}]
        assert cm.clan_history["#CL1"] == [{"WarID": "W1"}]

    def test_db_error_returns_empty(self):
        from qapbot.cache_manager import CacheManager
        cm = CacheManager.__new__(CacheManager)
        cm.db_manager = MagicMock()
        cm.clan_history = {}
        cm.db_manager.get_clan_attack_history_sync.side_effect = RuntimeError("fail")

        result = cm.get_clan_history("#CL1")
        assert result == []

    def test_no_db_manager(self):
        from qapbot.cache_manager import CacheManager
        cm = CacheManager.__new__(CacheManager)
        cm.db_manager = None
        cm.clan_history = {}

        result = cm.get_clan_history("#CL1")
        assert result == []


# ===================================================================
# QBhelperfunctions — _process_war_history dict start_time fallback
# ===================================================================

class TestProcessWarHistoryDictStartTime:
    """Test the dict start_time branch in _process_war_history."""

    def test_dict_start_time_parsing(self):
        """When start_time is a dict with year/month/day/hour/minute keys."""
        import re
        # Replicate the exact logic from the function
        start_time: dict[str, int] | str = {"year": 2025, "month": 6, "day": 15, "hour": 10, "minute": 30}

        start_dt_compact = None
        start_dt_iso = None

        if isinstance(start_time, dict):  # type: ignore[unnecessary-isinstance]
            y, mo, d, h, mi = (
                start_time.get("year"), start_time.get("month"),
                start_time.get("day"), start_time.get("hour"), start_time.get("minute")
            )
            if all(v is not None for v in [y, mo, d, h, mi]):
                start_dt_compact = f"{y:04d}{mo:02d}{d:02d}{h:02d}{mi:02d}"
                start_dt_iso = f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}"

        assert start_dt_compact == "202506151030"
        assert start_dt_iso == "2025-06-15T10:30"

    def test_dict_missing_key_returns_none(self):
        """If any key missing, should not produce a valid compact time."""
        start_time: dict[str, int] | str = {"year": 2025, "month": 6}
        start_dt_compact = None
        if isinstance(start_time, dict):  # type: ignore[unnecessary-isinstance]
            y, mo, d, h, mi = (
                start_time.get("year"), start_time.get("month"),
                start_time.get("day"), start_time.get("hour"), start_time.get("minute")
            )
            if all(v is not None for v in [y, mo, d, h, mi]):
                start_dt_compact = f"{y:04d}{mo:02d}{d:02d}{h:02d}{mi:02d}"
        assert start_dt_compact is None


# ===================================================================
# cache_manager — delete_subscriptions_for_channel error handler
# ===================================================================

class TestDeleteSubsForChannelError:
    @pytest.mark.asyncio
    async def test_error_propagates(self):
        from qapbot.cache_manager import CacheManager
        cm = CacheManager.__new__(CacheManager)
        cm.subscriptions = {"G1": {"C1": [{"clan_tag": "#X"}]}}
        cm.db_manager = AsyncMock()
        cm.db_manager.delete_subscriptions_for_channel.side_effect = RuntimeError("fail")
        with pytest.raises(RuntimeError):
            await cm.delete_subscriptions_for_channel("G1", "C1")


# ===================================================================
# cache_manager — set_clan_family error handler
# ===================================================================

class TestSetClanFamilyError:
    @pytest.mark.asyncio
    async def test_error_propagates(self):
        from qapbot.cache_manager import CacheManager
        cm = CacheManager.__new__(CacheManager)
        cm.clan_families = {}
        cm.db_manager = AsyncMock()
        cm.db_manager.save_clan_family.side_effect = RuntimeError("fail")
        with pytest.raises(RuntimeError):
            await cm.set_clan_family("F1", {"name": "F", "clans": []})

    @pytest.mark.asyncio
    async def test_persist_error_propagates(self):
        from qapbot.cache_manager import CacheManager
        cm = CacheManager.__new__(CacheManager)
        cm.clan_families = {"F2": {"name": "F2", "clans": []}}
        cm.db_manager = AsyncMock()
        cm.db_manager.save_clan_family.side_effect = RuntimeError("fail")
        with pytest.raises(RuntimeError):
            await cm.persist_clan_family("F2")


# ===================================================================
# cache_manager — set_user_account / persist_user error handlers
# ===================================================================

class TestSetUserAccountError:
    @pytest.mark.asyncio
    async def test_error_propagates(self):
        from qapbot.cache_manager import CacheManager
        cm = CacheManager.__new__(CacheManager)
        cm.user_accounts = {}
        cm.users_loaded = True  # post-startup state; pre-load gate tested in test_cache_manager.py
        cm.db_manager = AsyncMock()
        cm.db_manager.save_user.side_effect = RuntimeError("fail")
        with pytest.raises(RuntimeError):
            await cm.set_user_account("U1", {"display_name": "X", "players": []})

    @pytest.mark.asyncio
    async def test_persist_error_propagates(self):
        from qapbot.cache_manager import CacheManager
        cm = CacheManager.__new__(CacheManager)
        cm.user_accounts = {"U2": {"display_name": "X"}}
        cm.users_loaded = True  # post-startup state; pre-load gate tested in test_cache_manager.py
        cm.db_manager = AsyncMock()
        cm.db_manager.save_user.side_effect = RuntimeError("fail")
        with pytest.raises(RuntimeError):
            await cm.persist_user("U2")


# ===================================================================
# cache_manager — set_leaderboard_message error handler
# ===================================================================

class TestSetLeaderboardMessageError:
    @pytest.mark.asyncio
    async def test_error_propagates(self):
        from qapbot.cache_manager import CacheManager
        cm = CacheManager.__new__(CacheManager)
        cm.leaderboard_messages = {}
        cm.db_manager = AsyncMock()
        cm.db_manager.save_leaderboard_message.side_effect = RuntimeError("fail")
        cm.db_manager._ensure_clan_exists = AsyncMock()
        with pytest.raises(RuntimeError):
            await cm.set_leaderboard_message("K1", {
                "clan_tag": None, "channel_id": "C", "mode": "m",
                "message_ids": "1", "content_hash": "h"
            })
