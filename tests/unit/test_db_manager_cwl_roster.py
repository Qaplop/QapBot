"""Tests for the CWL roster planning schema and sync CRUD (CWL_ROSTER_PLANNING_PLAN.md Phase 1).

Covers: cwl_events/cwl_event_clans CRUD roundtrip, UNIQUE-constraint idempotency,
cascade-delete from cwl_events down to its child tables, get_previous_cwl_event_clans_sync's
carry-over query, and the guild_config CWL columns' save/get roundtrip.
"""
from __future__ import annotations

import pytest

from qapbot.db_manager import WarHistoryDB


@pytest.fixture
async def db(tmp_path):
    db_path = tmp_path / "qapbot_test.db"
    manager = WarHistoryDB()
    await manager.initialize(str(db_path))
    try:
        yield manager
    finally:
        await manager.close()


async def _seed_guild_and_clan(db: WarHistoryDB, guild_id: str = "111", clan_tag: str = "#CLAN1") -> None:
    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
    await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES (?, ?)", (clan_tag, "Test Clan"))
    await db.conn.commit()


class TestCwlEventCrud:
    @pytest.mark.integration
    async def test_create_get_list_update_roundtrip(self, db):
        await _seed_guild_and_clan(db)

        event_id = db.create_cwl_event_sync("111", "2026-08", "discordid1")
        assert event_id is not None

        event = db.get_cwl_event_sync("111", "2026-08")
        assert event is not None
        assert event["status"] == "draft"
        assert event["created_by_discord_id"] == "discordid1"

        assert db.update_cwl_event_status_sync(event_id, "signup_open") is True
        updated = db.get_cwl_event_sync("111", "2026-08")
        assert updated["status"] == "signup_open"

        events = db.list_cwl_events_sync("111")
        assert len(events) == 1
        assert events[0]["id"] == event_id

        # status filter
        assert len(db.list_cwl_events_sync("111", status="signup_open")) == 1
        assert len(db.list_cwl_events_sync("111", status="draft")) == 0

    @pytest.mark.integration
    async def test_create_is_idempotent_per_guild_and_season(self, db):
        """UNIQUE(guild_id, cwl_season) — calling create twice for the same guild+season
        must return the same event id, not raise or create a duplicate."""
        await _seed_guild_and_clan(db)

        first_id = db.create_cwl_event_sync("111", "2026-08", "discordid1")
        second_id = db.create_cwl_event_sync("111", "2026-08", "discordid2")

        assert first_id == second_id
        assert len(db.list_cwl_events_sync("111")) == 1

    @pytest.mark.integration
    async def test_get_nonexistent_event_returns_none(self, db):
        await _seed_guild_and_clan(db)
        assert db.get_cwl_event_sync("111", "2099-01") is None

    @pytest.mark.integration
    async def test_get_returns_none_for_wrong_guild(self, db):
        await _seed_guild_and_clan(db, guild_id="111")
        await _seed_guild_and_clan(db, guild_id="222")
        db.create_cwl_event_sync("111", "2026-08", "discordid1")
        assert db.get_cwl_event_sync("222", "2026-08") is None


class TestCwlEventClans:
    @pytest.mark.integration
    async def test_set_and_get_event_clans_roundtrip(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        await _seed_guild_and_clan(db, guild_id="111", clan_tag="#CLAN2")
        event_id = db.create_cwl_event_sync("111", "2026-08", "discordid1")

        ok = db.set_cwl_event_clans_sync(event_id, [
            {"clan_tag": "#CLAN2", "roster_size": 30, "tier_order": 1},
            {"clan_tag": "#CLAN1", "roster_size": 15, "tier_order": 0},
        ])
        assert ok is True

        clans = db.get_cwl_event_clans_sync(event_id)
        assert [c["clan_tag"] for c in clans] == ["#CLAN1", "#CLAN2"]  # ordered by tier_order
        assert clans[0]["roster_size"] == 15
        assert clans[1]["roster_size"] == 30

    @pytest.mark.integration
    async def test_set_event_clans_replaces_not_appends(self, db):
        """Calling set_cwl_event_clans_sync a second time must fully replace the prior set
        (atomic DELETE + INSERT), not append to it — re-opening "Configure Participating
        Clans" and applying a smaller selection must actually shrink the roster."""
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        await _seed_guild_and_clan(db, guild_id="111", clan_tag="#CLAN2")
        event_id = db.create_cwl_event_sync("111", "2026-08", "discordid1")

        db.set_cwl_event_clans_sync(event_id, [
            {"clan_tag": "#CLAN1"}, {"clan_tag": "#CLAN2"},
        ])
        assert len(db.get_cwl_event_clans_sync(event_id)) == 2

        db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1"}])
        clans = db.get_cwl_event_clans_sync(event_id)
        assert len(clans) == 1
        assert clans[0]["clan_tag"] == "#CLAN1"

    @pytest.mark.integration
    async def test_get_previous_cwl_event_clans_sync_carries_over_prior_season(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        old_event_id = db.create_cwl_event_sync("111", "2026-07", "discordid1")
        db.set_cwl_event_clans_sync(old_event_id, [
            {"clan_tag": "#CLAN1", "roster_size": 15, "tier_order": 0},
        ])

        new_event_id = db.create_cwl_event_sync("111", "2026-08", "discordid1")
        prev_clans = db.get_previous_cwl_event_clans_sync("111", exclude_event_id=new_event_id)

        assert len(prev_clans) == 1
        assert prev_clans[0]["clan_tag"] == "#CLAN1"
        assert prev_clans[0]["event_id"] == old_event_id

    @pytest.mark.integration
    async def test_get_previous_cwl_event_clans_sync_no_prior_event_returns_empty(self, db):
        await _seed_guild_and_clan(db)
        event_id = db.create_cwl_event_sync("111", "2026-08", "discordid1")
        assert db.get_previous_cwl_event_clans_sync("111", exclude_event_id=event_id) == []

    @pytest.mark.integration
    async def test_get_previous_cwl_event_clans_sync_no_events_at_all(self, db):
        await _seed_guild_and_clan(db)
        assert db.get_previous_cwl_event_clans_sync("111") == []


class TestCwlCascadeDelete:
    @pytest.mark.integration
    async def test_deleting_cwl_event_cascades_to_child_tables(self, db):
        """cwl_event_clans/cwl_signups/cwl_assignments all FK -> cwl_events ON DELETE CASCADE."""
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        event_id = db.create_cwl_event_sync("111", "2026-08", "discordid1")
        db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1"}])
        await db.conn.execute(
            "INSERT INTO cwl_signups (event_id, player_tag, source, status) VALUES (?, ?, ?, ?)",
            (event_id, "#PLAYER1", "self_signup", "confirmed"),
        )
        await db.conn.execute(
            "INSERT INTO cwl_assignments (event_id, player_tag, assigned_clan_tag) VALUES (?, ?, ?)",
            (event_id, "#PLAYER1", "#CLAN1"),
        )
        await db.conn.commit()

        assert len(db.get_cwl_event_clans_sync(event_id)) == 1

        await db.conn.execute("DELETE FROM cwl_events WHERE id = ?", (event_id,))
        await db.conn.commit()

        assert db.get_cwl_event_clans_sync(event_id) == []
        cursor = await db.conn.execute("SELECT COUNT(*) AS n FROM cwl_signups WHERE event_id = ?", (event_id,))
        row = await cursor.fetchone()
        assert row["n"] == 0
        cursor2 = await db.conn.execute("SELECT COUNT(*) AS n FROM cwl_assignments WHERE event_id = ?", (event_id,))
        row2 = await cursor2.fetchone()
        assert row2["n"] == 0


class TestGuildConfigCwlColumns:
    @pytest.mark.integration
    async def test_save_and_get_cwl_guild_config_columns(self, db):
        await db.save_guild_config("111", {
            "cwl_hub_channel_id": "555",
            "cwl_hub_message_enabled": True,
            "cwl_management_channel_id": "666",
            "cwl_management_message_enabled": True,
            "cwl_retention_months": 12,
        })

        cfg = await db.get_guild_config("111")
        assert cfg["cwl_hub_channel_id"] == "555"
        assert cfg["cwl_hub_message_enabled"] is True
        assert cfg["cwl_management_channel_id"] == "666"
        assert cfg["cwl_management_message_enabled"] is True
        assert cfg["cwl_retention_months"] == 12
        # Untouched fields default sanely
        assert cfg["cwl_hub_message_id"] is None
        assert cfg["cwl_management_message_id"] is None

    @pytest.mark.integration
    async def test_cwl_guild_config_defaults_for_fresh_guild(self, db):
        await db.save_guild_config("222", {})
        cfg = await db.get_guild_config("222")
        assert cfg["cwl_hub_message_enabled"] is False
        assert cfg["cwl_management_message_enabled"] is False
        assert cfg["cwl_retention_months"] == 0
