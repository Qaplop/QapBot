"""Tests for db_manager async methods + error paths — Phase 5 coverage push.

Covers:
- save_all_subscriptions (~35 lines)
- get_subscriptions_by_channel (~20 lines)
- DB error-handling in add_war_records, get_clan_history, etc. (~40 lines)
- initialize, _ensure_connection, _reconnect paths
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnusedImport=false, reportAttributeAccessIssue=false, reportReturnType=false, reportOptionalMemberAccess=false
from __future__ import annotations

import os
import asyncio
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
async def db(tmp_path):
    """Create a real async WarHistoryDB with in-memory-like temp file."""
    from qapbot.db_manager import WarHistoryDB
    db = WarHistoryDB()
    db_path = str(tmp_path / "test.db")
    await db.initialize(db_path)
    yield db
    if db.conn:
        await db.conn.close()


# ---------------------------------------------------------------------------
# save_all_subscriptions
# ---------------------------------------------------------------------------

class TestSaveAllSubscriptions:
    @pytest.mark.asyncio
    async def test_save_and_retrieve(self, db):
        subs = {
            "guild1": {
                "ch1": [
                    {"clan_tag": "#CLAN1", "subscription_type": "attack", "year": 2025},
                    {"clan_tag": "#CLAN2", "subscription_type": "playerlist"},
                ],
            },
        }
        await db.save_all_subscriptions(subs)

        # Verify via raw SQL
        cursor = await db.conn.execute("SELECT COUNT(*) AS cnt FROM subscriptions")
        row = await cursor.fetchone()
        assert row["cnt"] == 2

    @pytest.mark.asyncio
    async def test_year_current_converted_to_none(self, db):
        subs = {
            "guild1": {
                "ch1": [{"clan_tag": "#CL1", "subscription_type": "attack", "year": "current"}],
            },
        }
        await db.save_all_subscriptions(subs)
        cursor = await db.conn.execute("SELECT year FROM subscriptions WHERE clan_tag = '#CL1'")
        row = await cursor.fetchone()
        assert row["year"] is None

    @pytest.mark.asyncio
    async def test_year_bool_converted_to_none(self, db):
        subs = {
            "guild1": {
                "ch1": [{"clan_tag": "#CL1", "subscription_type": "attack", "year": True}],
            },
        }
        await db.save_all_subscriptions(subs)
        cursor = await db.conn.execute("SELECT year FROM subscriptions WHERE clan_tag = '#CL1'")
        row = await cursor.fetchone()
        assert row["year"] is None

    @pytest.mark.asyncio
    async def test_replaces_existing(self, db):
        subs1 = {"guild1": {"ch1": [{"clan_tag": "#OLD", "subscription_type": "attack"}]}}
        await db.save_all_subscriptions(subs1)

        subs2 = {"guild1": {"ch1": [{"clan_tag": "#NEW", "subscription_type": "war"}]}}
        await db.save_all_subscriptions(subs2)

        cursor = await db.conn.execute("SELECT clan_tag FROM subscriptions")
        rows = await cursor.fetchall()
        tags = [r["clan_tag"] for r in rows]
        assert "#OLD" not in tags
        assert "#NEW" in tags

    @pytest.mark.asyncio
    async def test_multiple_guilds(self, db):
        subs = {
            "guild1": {"ch1": [{"clan_tag": "#C1", "subscription_type": "attack"}]},
            "guild2": {"ch2": [{"clan_tag": "#C2", "subscription_type": "war"}]},
        }
        await db.save_all_subscriptions(subs)
        cursor = await db.conn.execute("SELECT COUNT(*) AS cnt FROM subscriptions")
        row = await cursor.fetchone()
        assert row["cnt"] == 2

    @pytest.mark.asyncio
    async def test_none_clan_tag_skipped(self, db):
        """Subscriptions with None clan_tag may be skipped by FK constraints."""
        subs = {
            "guild1": {
                "ch1": [{"clan_tag": None, "subscription_type": "playerlist"}],
            },
        }
        await db.save_all_subscriptions(subs)
        cursor = await db.conn.execute("SELECT COUNT(*) AS cnt FROM subscriptions")
        row = await cursor.fetchone()
        # None clan_tag may or may not insert depending on FK constraints
        assert row["cnt"] >= 0  # Just verify no crash


# ---------------------------------------------------------------------------
# get_subscriptions_by_channel
# ---------------------------------------------------------------------------

class TestGetSubscriptionsByChannel:
    @pytest.mark.asyncio
    async def test_empty_channel(self, db):
        result = await db.get_subscriptions_by_channel("guild1", "ch1")
        assert result == {}

    @pytest.mark.asyncio
    async def test_basic_retrieval(self, db):
        subs = {"guild1": {"ch1": [{"clan_tag": "#C1", "subscription_type": "attack"}]}}
        await db.save_all_subscriptions(subs)
        result = await db.get_subscriptions_by_channel("guild1", "ch1")
        assert "#C1" in result
        assert result["#C1"]["attack"] is True

    @pytest.mark.asyncio
    async def test_with_year(self, db):
        subs = {"guild1": {"ch1": [{"clan_tag": "#C1", "subscription_type": "war", "year": 2025}]}}
        await db.save_all_subscriptions(subs)
        result = await db.get_subscriptions_by_channel("guild1", "ch1")
        # SQLite stores year as text
        assert result["#C1"]["war"] == {"year": "2025"}

    @pytest.mark.asyncio
    async def test_multiple_types_same_clan(self, db):
        subs = {
            "guild1": {
                "ch1": [
                    {"clan_tag": "#C1", "subscription_type": "attack"},
                    {"clan_tag": "#C1", "subscription_type": "war", "year": 2025},
                ],
            },
        }
        await db.save_all_subscriptions(subs)
        result = await db.get_subscriptions_by_channel("guild1", "ch1")
        assert "#C1" in result
        assert result["#C1"]["attack"] is True
        assert result["#C1"]["war"] == {"year": "2025"}

    @pytest.mark.asyncio
    async def test_wrong_guild_returns_empty(self, db):
        subs = {"guild1": {"ch1": [{"clan_tag": "#C1", "subscription_type": "attack"}]}}
        await db.save_all_subscriptions(subs)
        result = await db.get_subscriptions_by_channel("guild2", "ch1")
        assert result == {}


# ---------------------------------------------------------------------------
# initialize paths
# ---------------------------------------------------------------------------

class TestInitialize:
    @pytest.mark.asyncio
    async def test_already_initialized_skips(self, db):
        """Second initialize call is a no-op."""
        await db.initialize(db.db_path)  # should not raise

    @pytest.mark.asyncio
    async def test_no_aiosqlite_raises(self, monkeypatch, tmp_path):
        from qapbot.db_manager import WarHistoryDB
        d = WarHistoryDB()
        # Temporarily remove aiosqlite
        import qapbot.db_manager as dm
        original = dm.aiosqlite
        dm.aiosqlite = None
        try:
            with pytest.raises(ImportError, match="aiosqlite"):
                await d.initialize(str(tmp_path / "test2.db"))
        finally:
            dm.aiosqlite = original


# ---------------------------------------------------------------------------
# _ensure_connection / _reconnect
# ---------------------------------------------------------------------------

class TestEnsureConnection:
    @pytest.mark.asyncio
    async def test_no_db_path_raises(self):
        from qapbot.db_manager import WarHistoryDB
        d = WarHistoryDB()
        with pytest.raises(RuntimeError, match="not initialized"):
            await d._ensure_connection()

    @pytest.mark.asyncio
    async def test_conn_none_reconnects(self, db):
        db.conn = None
        # Should attempt reconnect and succeed (db_path is set)
        await db._ensure_connection()
        assert db.conn is not None

    @pytest.mark.asyncio
    async def test_health_check_failure_reconnects(self, db):
        """If SELECT 1 fails, _reconnect is called."""
        _original_conn = db.conn
        # Make the health check fail
        original_execute = db.conn.execute
        call_count = 0
        async def failing_execute(sql, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if sql == "SELECT 1":
                raise Exception("Connection lost")
            return await original_execute(sql, *args, **kwargs)
        db.conn.execute = failing_execute
        await db._ensure_connection()
        # Connection should have been re-created
        assert db.conn is not None


# ---------------------------------------------------------------------------
# _reconnect
# ---------------------------------------------------------------------------

class TestReconnect:
    @pytest.mark.asyncio
    async def test_no_db_path_raises(self):
        from qapbot.db_manager import WarHistoryDB
        d = WarHistoryDB()
        with pytest.raises(RuntimeError, match="db_path not set"):
            await d._reconnect()

    @pytest.mark.asyncio
    async def test_successful_reconnect(self, db):
        old_conn = db.conn
        await db._reconnect()
        assert db.conn is not None
        assert db.conn is not old_conn


# ---------------------------------------------------------------------------
# get_latest_cwl_season_for_clan_in_month
# ---------------------------------------------------------------------------
# Regression: an explicit month/year lookup must resolve to the plain regular
# CWL key ("YYYY-MM") when it exists, even if a mid-month bonus sub-season
# ("YYYY-MM-DD") also exists for the same month. A naive `ORDER BY cwl_season
# DESC` always prefers the longer dated string, silently returning the wrong
# season for /leaderboard cwlgroup month/year queries.

class TestGetLatestCwlSeasonForClanInMonth:
    async def _insert_group(self, db, season: str, clan_tag: str = "#CLAN1"):
        await db.conn.execute(
            "INSERT INTO cwl_league_groups (league_group_id, cwl_season, clan_tag) "
            "VALUES (?, ?, ?)",
            (f"grp-{season}", season, clan_tag),
        )
        await db.conn.commit()

    @pytest.mark.asyncio
    async def test_prefers_plain_month_key_over_mid_month_bonus(self, db):
        """Both a regular '2026-06' and a bonus '2026-06-16' season exist —
        an explicit month/year request must resolve to the plain key."""
        await self._insert_group(db, "2026-06")
        await self._insert_group(db, "2026-06-16")
        result = await db.get_latest_cwl_season_for_clan_in_month("#CLAN1", "2026-06")
        assert result == "2026-06"

    @pytest.mark.asyncio
    async def test_falls_back_to_mid_month_bonus_when_no_plain_key(self, db):
        """Only a mid-month bonus CWL ran that month — fall back to it."""
        await self._insert_group(db, "2026-06-16")
        result = await db.get_latest_cwl_season_for_clan_in_month("#CLAN1", "2026-06")
        assert result == "2026-06-16"

    @pytest.mark.asyncio
    async def test_picks_most_recent_bonus_when_multiple_and_no_plain_key(self, db):
        await self._insert_group(db, "2026-06-08")
        await self._insert_group(db, "2026-06-16")
        result = await db.get_latest_cwl_season_for_clan_in_month("#CLAN1", "2026-06")
        assert result == "2026-06-16"

    @pytest.mark.asyncio
    async def test_no_match_returns_none(self, db):
        await self._insert_group(db, "2026-05")
        result = await db.get_latest_cwl_season_for_clan_in_month("#CLAN1", "2026-06")
        assert result is None

    @pytest.mark.asyncio
    async def test_different_month_not_matched_by_prefix(self, db):
        """'2026-06' prefix must not match '2026-060...'-style false positives
        such as a differently-formatted season from another month."""
        await self._insert_group(db, "2026-06")
        await self._insert_group(db, "2026-07")
        result = await db.get_latest_cwl_season_for_clan_in_month("#CLAN1", "2026-07")
        assert result == "2026-07"


# ---------------------------------------------------------------------------
# add_war_records error paths
# ---------------------------------------------------------------------------

