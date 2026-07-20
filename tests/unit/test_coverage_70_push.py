"""
Final coverage push to reach 70%.
Targets the remaining uncovered lines in db_manager and war_notifications.
"""
# pyright: reportPrivateUsage=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportReturnType=false
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import aiosqlite

from qapbot.db_manager import WarHistoryDB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_record(**overrides):
    rec = {
        "WarID": "war_1",
        "Date": "2025-01-01",
        "Player": "Alice",
        "PlayerID": "#A1",
        "TH_lvl": 15,
        "Stars": 3,
        "Attacks": 1,
        "Missed_Attacks": 0,
        "Max_Attacks": 1,
        "Defensive_Stars": 0,
    }
    rec.update(overrides)
    return rec


async def _init_db(tmp_path):
    """Create and initialize a real WarHistoryDB."""
    db = WarHistoryDB()
    db_path = str(tmp_path / "test.db")
    await db.initialize(db_path)
    return db


# 5) save_subscriptions_for_channel – year=False (isinstance bool) branch (L1995)
# ===========================================================================

class TestSaveSubsYearBoolFalse:
    """Cover the `elif isinstance(year, bool): year = None` branch."""

    async def test_year_false_becomes_none(self, tmp_path):
        db = await _init_db(tmp_path)
        try:
            # Ensure clan exists for FK
            await db._ensure_clan_exists("#C1")

            await db.save_subscriptions_for_channel(
                "g1", "ch1",
                [{"clan_tag": "#C1", "subscription_type": "attack", "year": False}],
            )

            # Verify year stored as NULL
            assert db.conn is not None
            async with db.conn.execute(
                "SELECT year FROM subscriptions WHERE guild_id = ? AND channel_id = ?",
                ("g1", "ch1"),
            ) as cur:
                row = await cur.fetchone()
                assert row is not None
                assert row["year"] is None
        finally:
            await db.close()


# ===========================================================================
# 6) save_all_subscriptions – year conversion + error branch  (L2197, L2206-2209)
# ===========================================================================

class TestSaveAllSubscriptions:
    """Cover save_all_subscriptions year conversion and error rollback."""

    async def test_year_current_and_bool(self, tmp_path):
        db = await _init_db(tmp_path)
        try:
            await db._ensure_clan_exists("#A")
            await db._ensure_clan_exists("#B")

            subs = {
                "guild_1": {
                    "ch1": [
                        {"clan_tag": "#A", "subscription_type": "attack", "year": "current"},
                        {"clan_tag": "#B", "subscription_type": "war", "year": True},
                    ],
                },
            }
            await db.save_all_subscriptions(subs)

            assert db.conn is not None
            async with db.conn.execute("SELECT year FROM subscriptions") as cur:
                rows = await cur.fetchall()
                assert all(r["year"] is None for r in rows)
        finally:
            await db.close()

    async def test_year_false_bool_branch(self, tmp_path):
        db = await _init_db(tmp_path)
        try:
            await db._ensure_clan_exists("#A")

            subs = {
                "guild_1": {
                    "ch1": [
                        {"clan_tag": "#A", "subscription_type": "attack", "year": False},
                    ],
                },
            }
            await db.save_all_subscriptions(subs)

            assert db.conn is not None
            async with db.conn.execute("SELECT year FROM subscriptions") as cur:
                row = await cur.fetchone()
                assert row is not None
                assert row["year"] is None
        finally:
            await db.close()

    async def test_error_rollback(self, tmp_path):
        db = await _init_db(tmp_path)
        try:
            # Patch at the transaction level — after _ensure_connection and _ensure_clan_exists
            # We need to make the BEGIN succeed but the DELETE fail
            assert db.conn is not None
            original_execute = db.conn.execute  # type: ignore[union-attr]
            call_count = 0

            async def execute_then_fail(sql, *args, **kwargs):
                nonlocal call_count
                # Let PRAGMA / SELECT 1 / BEGIN through, fail on DELETE
                if "DELETE" in str(sql):
                    raise Exception("boom")
                return await original_execute(sql, *args, **kwargs)

            with patch.object(db.conn, "execute", side_effect=execute_then_fail):
                with pytest.raises(Exception, match="boom"):
                    await db.save_all_subscriptions({"g1": {"ch1": [{"clan_tag": None, "subscription_type": "test"}]}})
        finally:
            await db.close()


# ===========================================================================
# 7) _get_active_wars – war filtering branches  (L181-182, L187, L193-196)
#    os/json/glob imported locally, so we test via the orchestrator path
#    that already exercises these (covered by check_wars_for_notifications tests).
#    Instead, target war_notifications._should_send_notification repeated branch.
# ===========================================================================

class TestShouldSendNotificationRepeated:
    """Cover the repeated interval notification branches (L820, L830-831)."""

    def test_repeated_due(self):
        from qapbot.war_notifications import _should_send_notification
        from datetime import datetime, timedelta

        # Set up CACHE so _should_send_notification finds the user and previous notification
        last_notif = (datetime.now() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
        mock_cache = MagicMock()
        mock_cache.user_accounts = {
            "disc1": {
                "notification_settings": {
                    "notification_mode": "repeated",
                    "hours_before_end": 10,
                },
            },
        }
        mock_cache.notification_state = {
            "war_1": {
                "notified_players": {
                    "A1": {"notification_time": last_notif},
                },
            },
        }
        with patch("qapbot.war_notifications.CACHE", mock_cache):
            result = _should_send_notification("war_1", "#A1", "disc1", 3.5)
        assert result is True

    def test_repeated_too_recent(self):
        from qapbot.war_notifications import _should_send_notification
        from datetime import datetime, timedelta

        # Last notification only 10 min ago with >2h remaining → need 2h interval
        last_notif = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        mock_cache = MagicMock()
        mock_cache.user_accounts = {
            "disc1": {
                "notification_settings": {
                    "notification_mode": "repeated",
                    "hours_before_end": 10,
                },
            },
        }
        mock_cache.notification_state = {
            "war_1": {
                "notified_players": {
                    "A1": {"notification_time": last_notif},
                },
            },
        }
        with patch("qapbot.war_notifications.CACHE", mock_cache):
            result = _should_send_notification("war_1", "#A1", "disc1", 3.5)
        assert result is False


# ===========================================================================
# 8) db_manager reconnect – error branch (L192-195)
# ===========================================================================

class TestReconnectError:
    """Cover the reconnect error branch."""

    async def test_reconnect_failure(self, tmp_path):
        db = await _init_db(tmp_path)
        try:
            with patch("aiosqlite.connect", side_effect=aiosqlite.Error("connection refused")):
                with pytest.raises(RuntimeError, match="reconnection failed"):
                    await db._reconnect()

            # conn should be None after failed reconnect
            assert db.conn is None
        finally:
            if db.conn:
                await db.close()

    async def test_reconnect_closes_stale_connection(self, tmp_path):
        """Cover L179-180: reconnect closes existing stale handle."""
        db = await _init_db(tmp_path)
        try:
            old_conn = db.conn  # Save reference to existing conn
            # Reconnect should close the existing connection and open a new one
            await db._reconnect()

            assert db.conn is not None
            assert db.conn is not old_conn  # Should be a new connection
        finally:
            await db.close()

    async def test_reconnect_stale_close_raises(self, tmp_path):
        """Cover L179-180: stale conn.close() raises, still continues."""
        db = await _init_db(tmp_path)
        real_conn = db.conn  # Save reference to close later
        try:
            # Replace conn with a mock that raises on close
            mock_conn = AsyncMock()
            mock_conn.close.side_effect = Exception("stale close error")
            db.conn = mock_conn

            # Reconnect should swallow the close error and create new connection
            await db._reconnect()
            assert db.conn is not None
            assert db.conn is not mock_conn
        finally:
            await db.close()
            # Close the original connection that was replaced by the mock
            if real_conn:
                try:
                    await real_conn.close()
                except Exception:
                    pass


# ===========================================================================
# More db_manager edge cases to push past 70%
# ===========================================================================

# Initialize error closes conn (L132-133)
# ===========================================================================

class TestInitializeErrorClosesConn:
    """Cover L132-133: when initialize fails after conn is set, it closes conn."""

    async def test_schema_error_closes_conn(self, tmp_path):
        db = WarHistoryDB()
        db_path = str(tmp_path / "test_init_err.db")

        # Patch _create_schema to raise aiosqlite.Error AFTER conn is established
        with patch.object(
            WarHistoryDB, "_create_schema",
            side_effect=aiosqlite.Error("schema boom"),
        ):
            with pytest.raises(RuntimeError, match="Database initialization failed"):
                await db.initialize(db_path)

        # conn should be cleaned up
        assert db.conn is None


# ===========================================================================
# _create_schema guard (L266) — no conn
# ===========================================================================

class TestCreateSchemaNoConn:
    """Cover L266: _create_schema raises if conn is None."""

    async def test_no_conn_raises(self):
        db = WarHistoryDB()
        db.conn = None
        with pytest.raises(RuntimeError, match="not initialized"):
            await db._create_schema()


# ===========================================================================
# add_war_records_sync real sqlite3.Error in executemany (L834-835)
# ===========================================================================

# ===========================================================================

class TestFormattingWidthBranches:
    """Cover formatting.py L204 (emoji width) and L212 (Latin small capital)."""

    def test_emoji_width(self):
        from qapbot.formatting import text_display_width_float
        # U+1F600 GRINNING FACE — should trigger the emoji branch returning 2.25
        w = text_display_width_float("\U0001F600")
        assert w == 2.25

    def test_latin_small_capital(self):
        from qapbot.formatting import text_display_width_float
        # U+1D04 LATIN SMALL CAPITAL C — in range 0x1D00-0x1D7F, category Ll
        w = text_display_width_float("\u1D04")
        assert w == 1.00


class TestExceptionStrNoContext:
    """Cover exceptions.py L111: __str__ without context."""

    def test_str_no_context(self):
        from qapbot.exceptions import QapBotError
        err = QapBotError("test error")
        assert str(err) == "test error"
