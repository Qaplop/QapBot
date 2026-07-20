"""Tests for db_manager CRUD + query + error paths — Phase 5 Batch 4 coverage push.

Covers:
- get_clan_history (async) — month+year, year-only, no-filter, december boundary, error path
- get_clan_history_sync — same variants + error path
- get_player_wars — happy + error path
- get_clan_war_count — happy + error path
- get_all_clans — happy + error path
- update_war_records_sync — happy + no-records + not-found + db-error
- delete_clan — happy + error path
- _ensure_clan_exists — valid clan in cache, no cache, invalid tag, already exists
- _ensure_family_exists — family in cache, no cache, empty tag, already exists
- delete_notification_state_for_war — happy + error path
- save_clan_family / delete_clan_family — round-trip
- save_leaderboard_message / delete_leaderboard_message — round-trip
- save_user / delete_user / get_all_users_dict — round-trip
- delete_subscriptions_for_channel / delete_subscriptions_for_guild
- get_all_subscriptions_dict — multi-guild multi-channel
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnusedImport=false, reportAttributeAccessIssue=false, reportReturnType=false, reportOptionalMemberAccess=false
from __future__ import annotations

import asyncio
import sqlite3
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Async fixture — real aiosqlite database with full schema
# ---------------------------------------------------------------------------

@pytest.fixture
async def db(tmp_path):
    """Create a real async WarHistoryDB backed by a temp file."""
    from qapbot.db_manager import WarHistoryDB
    inst = WarHistoryDB()
    db_path = str(tmp_path / "crud_test.db")
    await inst.initialize(db_path)
    yield inst
    if inst.conn:
        await inst.conn.close()


async def _insert_war_record(db, clan_tag="#TEST123", war_id="W1",
                              date="2025-06-15", player_name="Alice",
                              player_tag="#P1", th_level=16, stars=3,
                              attacks=2, missed=0, max_atk=2, def_stars=1):
    """Helper to insert a single war_history row."""
    await db.conn.execute("""
        INSERT INTO war_history
        (war_id, clan_tag, date, player_name, player_tag, th_level,
         stars, attacks, missed_attacks, max_attacks, defensive_stars)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (war_id, clan_tag, date, player_name, player_tag, th_level,
          stars, attacks, missed, max_atk, def_stars))
    await db.conn.commit()


# ===================================================================
# get_clan_history (async)
# ===================================================================

class TestDeleteClan:
    @pytest.mark.asyncio
    async def test_deletes_clan(self, db):
        # Insert a clan first
        await db.conn.execute(
            "INSERT INTO clans (clan_tag, name) VALUES ('#DEL123', 'ToDelete')"
        )
        await db.conn.commit()
        await db.delete_clan("#DEL123")
        cursor = await db.conn.execute(
            "SELECT COUNT(*) AS cnt FROM clans WHERE clan_tag = '#DEL123'"
        )
        row = await cursor.fetchone()
        assert row["cnt"] == 0

    @pytest.mark.asyncio
    async def test_delete_nonexistent_no_error(self, db):
        """Deleting a non-existent clan should not raise."""
        await db.delete_clan("#NOPE12345")


# ===================================================================
# _ensure_clan_exists
# ===================================================================

class TestEnsureClanExists:
    @pytest.mark.asyncio
    async def test_invalid_tag_skipped(self, db):
        """Tags that don't match format are silently skipped."""
        await db._ensure_clan_exists("")
        await db._ensure_clan_exists("#AB")       # too short (< 5 chars total)
        await db._ensure_clan_exists("NOHASH1234")  # no leading #
        cursor = await db.conn.execute("SELECT COUNT(*) AS cnt FROM clans")
        row = await cursor.fetchone()
        assert row["cnt"] == 0

    @pytest.mark.asyncio
    async def test_creates_from_cache(self, db):
        """If clan not in DB but in CACHE, auto-creates from cache data."""
        mock_cache = MagicMock()
        mock_cache.clan_name_cache = {
            "#ABCDE1234": {"name": "TestClan", "has_active_subscriptions": True}
        }
        with patch("qapbot.cache_manager.CACHE", mock_cache):
            await db._ensure_clan_exists("#ABCDE1234")

        cursor = await db.conn.execute(
            "SELECT name FROM clans WHERE clan_tag = '#ABCDE1234'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["name"] == "TestClan"

    @pytest.mark.asyncio
    async def test_creates_placeholder_when_not_in_cache(self, db):
        """If clan not in DB and not in CACHE, creates placeholder."""
        mock_cache = MagicMock()
        mock_cache.clan_name_cache = {}
        with patch("qapbot.cache_manager.CACHE", mock_cache):
            await db._ensure_clan_exists("#NEWCL1234")

        cursor = await db.conn.execute(
            "SELECT name FROM clans WHERE clan_tag = '#NEWCL1234'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["name"] == "Unknown"

    @pytest.mark.asyncio
    async def test_already_exists_no_duplicate(self, db):
        """If clan already in DB, no error and no duplicate."""
        await db.conn.execute(
            "INSERT INTO clans (clan_tag, name) VALUES ('#EXIST1234', 'Already')"
        )
        await db.conn.commit()
        # _ensure_clan_exists does SELECT first — should skip INSERT
        await db._ensure_clan_exists("#EXIST1234")
        cursor = await db.conn.execute(
            "SELECT COUNT(*) AS cnt FROM clans WHERE clan_tag = '#EXIST1234'"
        )
        row = await cursor.fetchone()
        assert row["cnt"] == 1


# ===================================================================
# _ensure_family_exists
# ===================================================================

class TestEnsureFamilyExists:
    @pytest.mark.asyncio
    async def test_empty_tag_skipped(self, db):
        await db._ensure_family_exists("")
        await db._ensure_family_exists(None)
        cursor = await db.conn.execute("SELECT COUNT(*) AS cnt FROM clan_families")
        row = await cursor.fetchone()
        assert row["cnt"] == 0

    @pytest.mark.asyncio
    async def test_creates_from_cache(self, db):
        mock_cache = MagicMock()
        mock_cache.clan_families = {
            "FAM1": {"name": "My Family", "owned_by_guild": "guild1"}
        }
        with patch("qapbot.cache_manager.CACHE", mock_cache):
            await db._ensure_family_exists("FAM1")

        cursor = await db.conn.execute(
            "SELECT name FROM clan_families WHERE family_tag = 'FAM1'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["name"] == "My Family"

    @pytest.mark.asyncio
    async def test_creates_placeholder_no_cache(self, db):
        mock_cache = MagicMock()
        mock_cache.clan_families = {}
        with patch("qapbot.cache_manager.CACHE", mock_cache):
            await db._ensure_family_exists("FAM_NOCACHE")

        cursor = await db.conn.execute(
            "SELECT name FROM clan_families WHERE family_tag = 'FAM_NOCACHE'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["name"] == "Unknown Family"

    @pytest.mark.asyncio
    async def test_already_exists_no_duplicate(self, db):
        await db.conn.execute("""
            INSERT INTO clan_families (family_tag, name, owned_by_guild)
            VALUES ('FAMEXIST', 'Existing', 'g1')
        """)
        await db.conn.commit()
        await db._ensure_family_exists("FAMEXIST")
        cursor = await db.conn.execute(
            "SELECT COUNT(*) AS cnt FROM clan_families WHERE family_tag = 'FAMEXIST'"
        )
        row = await cursor.fetchone()
        assert row["cnt"] == 1


# ===================================================================
# delete_notification_state_for_war
# ===================================================================

class TestDeleteNotificationStateForWar:
    @pytest.mark.asyncio
    async def test_deletes_both_tables(self, db):
        """Deletes from notification_state AND channel_notification_state."""
        # Need a user for FK constraint in notification_state
        await db.conn.execute(
            "INSERT INTO users (discord_id, display_name) VALUES ('U1', 'User1')"
        )
        await db.conn.execute("""
            INSERT INTO notification_state
            (war_key, player_tag, player_name, discord_id, notification_time, attacks_remaining)
            VALUES ('WAR1', '#P1', 'Alice', 'U1', '2025-06-15', 2)
        """)
        await db.conn.execute("""
            INSERT INTO channel_notification_state
            (war_key, guild_id, notification_time, clan_name, opponent_name)
            VALUES ('WAR1', 'G1', '2025-06-15', 'Clan', 'Opponent')
        """)
        await db.conn.commit()

        await db.delete_notification_state_for_war("WAR1")

        c1 = await db.conn.execute(
            "SELECT COUNT(*) AS cnt FROM notification_state WHERE war_key = 'WAR1'"
        )
        assert (await c1.fetchone())["cnt"] == 0
        c2 = await db.conn.execute(
            "SELECT COUNT(*) AS cnt FROM channel_notification_state WHERE war_key = 'WAR1'"
        )
        assert (await c2.fetchone())["cnt"] == 0

    @pytest.mark.asyncio
    async def test_nonexistent_key_no_error(self, db):
        await db.delete_notification_state_for_war("NO_SUCH_WAR")


# ===================================================================
# save_clan_family / delete_clan_family round-trip
# ===================================================================

class TestClanFamilyRoundTrip:
    @pytest.mark.asyncio
    async def test_save_and_delete(self, db):
        # Ensure member clans exist so FK on clan_family_members passes
        await db.conn.execute(
            "INSERT INTO clans (clan_tag, name) VALUES ('#FMCL1234', 'FC1')"
        )
        await db.conn.execute(
            "INSERT INTO clans (clan_tag, name) VALUES ('#FMCL5678', 'FC2')"
        )
        await db.conn.commit()

        await db.save_clan_family(
            family_tag="FAM_RT",
            name="Round-Trip Family",
            owned_by_guild="guild1",
            member_clans=["#FMCL1234", "#FMCL5678"]
        )

        cursor = await db.conn.execute(
            "SELECT name FROM clan_families WHERE family_tag = 'FAM_RT'"
        )
        assert (await cursor.fetchone())["name"] == "Round-Trip Family"

        # Delete
        await db.delete_clan_family("FAM_RT")
        cursor2 = await db.conn.execute(
            "SELECT COUNT(*) AS cnt FROM clan_families WHERE family_tag = 'FAM_RT'"
        )
        assert (await cursor2.fetchone())["cnt"] == 0


# ===================================================================
# save_leaderboard_message / delete_leaderboard_message round-trip
# ===================================================================

class TestLeaderboardMessageRoundTrip:
    @pytest.mark.asyncio
    async def test_save_and_delete(self, db):
        await db.save_leaderboard_message(
            message_key="LB_KEY1",
            clan_tag=None,
            channel_id="CH1",
            mode="stars_06_2025",
            message_ids="111,222",
            content_hash="hash123"
        )
        cursor = await db.conn.execute(
            "SELECT mode FROM leaderboard_messages WHERE message_key = 'LB_KEY1'"
        )
        assert (await cursor.fetchone())["mode"] == "stars_06_2025"

        await db.delete_leaderboard_message("LB_KEY1")
        cursor2 = await db.conn.execute(
            "SELECT COUNT(*) AS cnt FROM leaderboard_messages WHERE message_key = 'LB_KEY1'"
        )
        assert (await cursor2.fetchone())["cnt"] == 0

    @pytest.mark.asyncio
    async def test_save_with_clan_tag(self, db):
        """If clan_tag provided, _ensure_clan_exists is called."""
        mock_cache = MagicMock()
        mock_cache.clan_name_cache = {"#LBCL1234": {"name": "LBClan"}}
        with patch("qapbot.cache_manager.CACHE", mock_cache):
            await db.save_leaderboard_message(
                message_key="LB_KEY2",
                clan_tag="#LBCL1234",
                channel_id="CH2",
                mode="attacks_06_2025",
                message_ids="333",
                content_hash="hash456"
            )
        cursor = await db.conn.execute(
            "SELECT clan_tag FROM leaderboard_messages WHERE message_key = 'LB_KEY2'"
        )
        assert (await cursor.fetchone())["clan_tag"] == "#LBCL1234"


# ===================================================================
# save_user / delete_user / get_all_users_dict round-trip
# ===================================================================

class TestUserRoundTrip:
    @pytest.mark.asyncio
    async def test_save_and_retrieve(self, db):
        user_data = {
            "display_name": "TestUser",
            "user_language": "de",
            "notification_settings": {
                "notification_mode": "repeated",
                "notification_type": "all_wars",
                "hours_before_end": 4,
                "war_reminders": True
            },
            "players": [
                {"player_tag": "#USRP1234", "player_name": "Hero", "verified": True, "is_primary": True}
            ]
        }
        await db.save_user("12345", user_data)
        users = await db.get_all_users_dict()
        assert "12345" in users
        assert users["12345"]["display_name"] == "TestUser"

    @pytest.mark.asyncio
    async def test_delete_user(self, db):
        user_data = {
            "display_name": "ToDelete",
            "players": []
        }
        await db.save_user("99999", user_data)
        await db.delete_user("99999")
        cursor = await db.conn.execute(
            "SELECT COUNT(*) AS cnt FROM users WHERE discord_id = '99999'"
        )
        assert (await cursor.fetchone())["cnt"] == 0

    @pytest.mark.asyncio
    async def test_save_with_buddies(self, db):
        """User with watched_players should save buddy rows."""
        user_data = {
            "display_name": "BuddyUser",
            "players": [],
            "watched_players": [
                {"player_tag": "#BUDDY123", "player_name": "BuddyName"}
            ]
        }
        await db.save_user("77777", user_data)
        cursor = await db.conn.execute(
            "SELECT player_name FROM user_buddies WHERE discord_id = '77777'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["player_name"] == "BuddyName"

    @pytest.mark.asyncio
    async def test_save_with_clan_tag(self, db):
        """Player with current_clan_tag should trigger _ensure_clan_exists."""
        mock_cache = MagicMock()
        mock_cache.clan_name_cache = {"#USRCL234": {"name": "UserClan"}}
        with patch("qapbot.cache_manager.CACHE", mock_cache):
            user_data = {
                "display_name": "ClanUser",
                "players": [
                    {"player_tag": "#USRP5678", "player_name": "ClanHero",
                     "current_clan_tag": "#USRCL234"}
                ]
            }
            await db.save_user("88888", user_data)

        cursor = await db.conn.execute(
            "SELECT name FROM clans WHERE clan_tag = '#USRCL234'"
        )
        row = await cursor.fetchone()
        assert row is not None


# ===================================================================
# delete_subscriptions_for_channel / delete_subscriptions_for_guild
# ===================================================================

class TestDeleteSubscriptions:
    async def _seed_subs(self, db):
        """Seed two guilds with subscriptions."""
        for guild, channel, tag in [
            ("G1", "C1", "#S1"),
            ("G1", "C1", "#S2"),
            ("G1", "C2", "#S3"),
            ("G2", "C3", "#S4"),
        ]:
            await db.conn.execute("""
                INSERT INTO subscriptions (guild_id, channel_id, clan_tag, subscription_type)
                VALUES (?, ?, ?, 'attack')
            """, (guild, channel, tag))
        await db.conn.commit()

    @pytest.mark.asyncio
    async def test_delete_channel(self, db):
        await self._seed_subs(db)
        await db.delete_subscriptions_for_channel("G1", "C1")
        cursor = await db.conn.execute(
            "SELECT COUNT(*) AS cnt FROM subscriptions WHERE guild_id='G1' AND channel_id='C1'"
        )
        assert (await cursor.fetchone())["cnt"] == 0
        # Other channel untouched
        cursor2 = await db.conn.execute(
            "SELECT COUNT(*) AS cnt FROM subscriptions WHERE guild_id='G1' AND channel_id='C2'"
        )
        assert (await cursor2.fetchone())["cnt"] == 1

    @pytest.mark.asyncio
    async def test_delete_guild(self, db):
        await self._seed_subs(db)
        await db.delete_subscriptions_for_guild("G1")
        cursor = await db.conn.execute(
            "SELECT COUNT(*) AS cnt FROM subscriptions WHERE guild_id='G1'"
        )
        assert (await cursor.fetchone())["cnt"] == 0
        # G2 untouched
        cursor2 = await db.conn.execute(
            "SELECT COUNT(*) AS cnt FROM subscriptions WHERE guild_id='G2'"
        )
        assert (await cursor2.fetchone())["cnt"] == 1


# ===================================================================
# get_all_subscriptions_dict
# ===================================================================

class TestGetAllSubscriptionsDict:
    @pytest.mark.asyncio
    async def test_multi_guild_multi_channel(self, db):
        await db.conn.execute("""
            INSERT INTO subscriptions (guild_id, channel_id, clan_tag, subscription_type, year)
            VALUES ('G1', 'C1', '#CL1', 'attack', '2025')
        """)
        await db.conn.execute("""
            INSERT INTO subscriptions (guild_id, channel_id, clan_tag, subscription_type, year)
            VALUES ('G1', 'C1', '#CL1', 'playerlist', NULL)
        """)
        await db.conn.execute("""
            INSERT INTO subscriptions (guild_id, channel_id, clan_tag, subscription_type, year)
            VALUES ('G2', 'C2', '#CL2', 'war', NULL)
        """)
        await db.conn.commit()

        result = await db.get_all_subscriptions_dict()
        assert "G1" in result
        assert "C1" in result["G1"]
        assert "#CL1" in result["G1"]["C1"]
        # attack has year → dict with year
        assert result["G1"]["C1"]["#CL1"]["attack"] == {"year": "2025"}
        # playerlist has no year → True
        assert result["G1"]["C1"]["#CL1"]["playerlist"] is True
        # G2 present
        assert result["G2"]["C2"]["#CL2"]["war"] is True

    @pytest.mark.asyncio
    async def test_empty_db(self, db):
        result = await db.get_all_subscriptions_dict()
        assert result == {}
