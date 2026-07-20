"""Tests for db_manager async operations using real aiosqlite — Phase 4 coverage.

Covers: get_user, save_user, war_exists_sync, get_all_leaderboard_messages,
save_leaderboard_message, delete_leaderboard_message, load_notification_state.
All tests use a real in-memory database for accuracy.
"""
# pyright: reportPrivateUsage=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportOptionalMemberAccess=false, reportIndexIssue=false, reportUnusedImport=false, reportOptionalSubscript=false
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import aiosqlite
import pytest

from qapbot.db_manager import WarHistoryDB


async def _create_db() -> WarHistoryDB:
    """Create an initialized WarHistoryDB with in-memory database."""
    import threading
    db = WarHistoryDB.__new__(WarHistoryDB)
    db.db_path = ":memory:"
    db._initialized = False
    db._write_lock = asyncio.Lock()
    db._sync_write_lock = threading.Lock()
    db.conn = await aiosqlite.connect(":memory:")
    db.conn.row_factory = aiosqlite.Row
    db._initialized = True
    await db._create_schema()
    return db


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# save_user + get_user roundtrip
# ---------------------------------------------------------------------------

class TestUserRoundtrip:
    @pytest.mark.asyncio
    async def test_save_and_get_user(self):
        db = await _create_db()
        try:
            user_data = {
                "display_name": "TestUser",
                "notification_settings": {
                    "notification_mode": "repeated",
                    "notification_type": "all_wars",
                    "hours_before_end": 4,
                    "war_reminders": True,
                },
                "user_language": "de",
                "user_language_locked": False,
                "players": [
                    {
                        "player_tag": "#P1",
                        "player_name": "Alice",
                        "verified": True,
                        "th_level": 16,
                        "current_clan_tag": None,
                        "is_primary": True,
                    }
                ],
                "watched_players": [
                    {"player_tag": "#B1", "player_name": "BuddyBob"}
                ],
            }
            await db.save_user("U1", user_data)
            result = await db.get_user("U1")

            assert result is not None
            assert result["display_name"] == "TestUser"
            assert result["user_language"] == "de"
            assert len(result["players"]) == 1
            assert result["players"][0]["player_tag"] == "#P1"
            assert result["players"][0]["verified"] is True
            assert result["players"][0]["is_primary"] is True
            assert len(result["watched_players"]) == 1
            assert result["watched_players"][0]["player_tag"] == "#B1"
            assert result["notification_settings"]["war_reminders"] is True
        finally:
            await db.conn.close()

    @pytest.mark.asyncio
    async def test_get_nonexistent_user(self):
        db = await _create_db()
        try:
            result = await db.get_user("NONEXISTENT")
            assert result is None
        finally:
            await db.conn.close()

    @pytest.mark.asyncio
    async def test_update_user_overwrites(self):
        db = await _create_db()
        try:
            await db.save_user("U1", {
                "display_name": "Old",
                "players": [{"player_tag": "#P1", "player_name": "Old"}],
                "watched_players": [],
            })
            await db.save_user("U1", {
                "display_name": "New",
                "players": [{"player_tag": "#P2", "player_name": "New"}],
                "watched_players": [],
            })
            result = await db.get_user("U1")
            assert result["display_name"] == "New"
            assert len(result["players"]) == 1
            assert result["players"][0]["player_tag"] == "#P2"
        finally:
            await db.conn.close()

    @pytest.mark.asyncio
    async def test_multiple_players(self):
        db = await _create_db()
        try:
            await db.save_user("U1", {
                "display_name": "Multi",
                "players": [
                    {"player_tag": "#P1", "player_name": "A", "is_primary": True},
                    {"player_tag": "#P2", "player_name": "B", "is_primary": False},
                    {"player_tag": "#P3", "player_name": "C", "is_primary": False},
                ],
                "watched_players": [],
            })
            result = await db.get_user("U1")
            assert len(result["players"]) == 3
            # Primary should be first (ORDER BY is_primary DESC)
            assert result["players"][0]["is_primary"] is True
        finally:
            await db.conn.close()


# ---------------------------------------------------------------------------
# leaderboard_messages roundtrip
# ---------------------------------------------------------------------------

class TestLeaderboardMessagesRoundtrip:
    @pytest.mark.asyncio
    async def test_save_and_get(self):
        db = await _create_db()
        try:
            await db.save_leaderboard_message(
                "key1", None, "chan1", "stars", "111,222", "abc123"
            )
            result = await db.get_all_leaderboard_messages()
            assert "key1" in result
            assert result["key1"]["channel_id"] == "chan1"
            assert result["key1"]["mode"] == "stars"
            assert result["key1"]["message_ids"] == "111,222"
            assert result["key1"]["content_hash"] == "abc123"
        finally:
            await db.conn.close()

    @pytest.mark.asyncio
    async def test_delete(self):
        db = await _create_db()
        try:
            await db.save_leaderboard_message("key1", None, "c", "m", "1", "h")
            await db.delete_leaderboard_message("key1")
            result = await db.get_all_leaderboard_messages()
            assert "key1" not in result
        finally:
            await db.conn.close()

    @pytest.mark.asyncio
    async def test_upsert(self):
        db = await _create_db()
        try:
            await db.save_leaderboard_message("key1", None, "c", "m", "1", "old")
            await db.save_leaderboard_message("key1", None, "c", "m", "1", "new")
            result = await db.get_all_leaderboard_messages()
            assert result["key1"]["content_hash"] == "new"
        finally:
            await db.conn.close()

    @pytest.mark.asyncio
    async def test_multiple_messages(self):
        db = await _create_db()
        try:
            await db.save_leaderboard_message("k1", None, "c1", "stars", "1", "h1")
            await db.save_leaderboard_message("k2", None, "c2", "attacks", "2", "h2")
            result = await db.get_all_leaderboard_messages()
            assert len(result) == 2
        finally:
            await db.conn.close()


# ---------------------------------------------------------------------------
# load_notification_state
# ---------------------------------------------------------------------------

class TestLoadNotificationState:
    @pytest.mark.asyncio
    async def test_empty_state(self):
        db = await _create_db()
        try:
            result = await db.load_notification_state()
            assert result == {}
        finally:
            await db.conn.close()

    @pytest.mark.asyncio
    async def test_with_player_notifications(self):
        db = await _create_db()
        try:
            # Insert directly
            await db.conn.execute("""
                INSERT INTO notification_state
                (war_key, player_tag, player_name, discord_id, notification_time, attacks_remaining)
                VALUES ('war1', '#P1', 'Alice', 'D1', '2025-01-01T12:00', 1)
            """)
            await db.conn.commit()

            result = await db.load_notification_state()
            assert "war1" in result
            assert "#P1" in result["war1"]["notified_players"]
            p = result["war1"]["notified_players"]["#P1"]
            assert p["player_name"] == "Alice"
            assert p["discord_id"] == "D1"
            assert p["attacks_remaining"] == 1
        finally:
            await db.conn.close()

    @pytest.mark.asyncio
    async def test_with_channel_notifications(self):
        db = await _create_db()
        try:
            await db.conn.execute("""
                INSERT INTO channel_notification_state
                (war_key, guild_id, notification_time, clan_name, opponent_name)
                VALUES ('war1', 'G1', '2025-01-01T12:00', 'MyClan', 'Enemy')
            """)
            await db.conn.commit()

            result = await db.load_notification_state()
            assert "war1" in result
            cn = result["war1"]["channel_notifications"]
            assert "G1" in cn
            assert cn["G1"]["clan_name"] == "MyClan"
        finally:
            await db.conn.close()

    @pytest.mark.asyncio
    async def test_combined_state(self):
        db = await _create_db()
        try:
            await db.conn.execute("""
                INSERT INTO notification_state
                (war_key, player_tag, player_name, discord_id, notification_time, attacks_remaining)
                VALUES ('war1', '#P1', 'Alice', 'D1', '2025-01-01', 2)
            """)
            await db.conn.execute("""
                INSERT INTO channel_notification_state
                (war_key, guild_id, notification_time, clan_name, opponent_name)
                VALUES ('war1', 'G1', '2025-01-01', 'Clan', 'Opp')
            """)
            await db.conn.commit()

            result = await db.load_notification_state()
            assert "war1" in result
            assert "#P1" in result["war1"]["notified_players"]
            assert "G1" in result["war1"]["channel_notifications"]
        finally:
            await db.conn.close()
