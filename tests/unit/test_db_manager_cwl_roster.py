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


async def _seed_cwl_roster_war(
    db: WarHistoryDB, clan_tag: str, cwl_season: str, players: list, war_id: str = None,
) -> None:
    """Seed a minimal war_summary + war_attacks pair so get_cwl_roster_sync (and therefore
    get_previous_cwl_participants_sync) has something to read. players: list of
    (player_tag, player_name, th_level, map_position) tuples."""
    war_id = war_id or f"war_{clan_tag}_{cwl_season}"
    await db.conn.execute(
        """
        INSERT INTO war_summary (war_id, clan_tag, opponent_tag, is_cwl, cwl_season, date)
        VALUES (?, ?, ?, 1, ?, ?)
        """,
        (war_id, clan_tag, "#OPP", cwl_season, "2026-08-01 08:00:00"),
    )
    for player_tag, player_name, th_level, map_position in players:
        await db.conn.execute(
            """
            INSERT INTO war_attacks (war_id, clan_tag, date, player_name, player_tag, th_level, map_position, stars)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (war_id, clan_tag, "2026-08-01 08:00:00", player_name, player_tag, th_level, map_position),
        )
    await db.conn.commit()


async def _seed_user_player(
    db: WarHistoryDB,
    discord_id: str,
    player_tag: str,
    player_name: str = "Player",
    verified: bool = True,
    cwl_permanent_optout: bool = False,
    cwl_default_preferred_league_rank: str = None,
) -> None:
    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES (?, ?)", (discord_id, discord_id))
    await db.conn.execute(
        """
        INSERT INTO user_players
            (discord_id, player_tag, player_name, verified, cwl_permanent_optout, cwl_default_preferred_league_rank)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (discord_id, player_tag, player_name, 1 if verified else 0, 1 if cwl_permanent_optout else 0, cwl_default_preferred_league_rank),
    )
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
    async def test_deactivating_a_clan_preserves_its_settings_not_deletes_row(self, db):
        """Regression guard for the data-loss bug fixed in CWL_CLAN_CONFIG_ACTIVITY_PLAN.md:
        a clan with participating=False must keep its row (and roster_size/cwl_start_at), not
        get dropped entirely — the caller (the web bridge) is responsible for passing the clan
        through at all; this only guards that when it does, nothing is lost."""
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        event_id = db.create_cwl_event_sync("111", "2026-08", "discordid1")

        db.set_cwl_event_clans_sync(event_id, [
            {"clan_tag": "#CLAN1", "roster_size": 30, "cwl_start_at": "2026-09-01T08:00Z", "participating": True},
        ])
        assert db.get_cwl_event_clans_sync(event_id)[0]["participating"] == 1

        # Deactivate — settings must be passed through unchanged, not dropped.
        db.set_cwl_event_clans_sync(event_id, [
            {"clan_tag": "#CLAN1", "roster_size": 30, "cwl_start_at": "2026-09-01T08:00Z", "participating": False},
        ])
        clans = db.get_cwl_event_clans_sync(event_id)
        assert len(clans) == 1  # row kept, not deleted
        assert clans[0]["participating"] == 0
        assert clans[0]["roster_size"] == 30
        assert clans[0]["cwl_start_at"] == "2026-09-01T08:00Z"

        # Reactivate — same settings still there.
        db.set_cwl_event_clans_sync(event_id, [
            {"clan_tag": "#CLAN1", "roster_size": 30, "cwl_start_at": "2026-09-01T08:00Z", "participating": True},
        ])
        clans = db.get_cwl_event_clans_sync(event_id)
        assert clans[0]["participating"] == 1
        assert clans[0]["roster_size"] == 30
        assert clans[0]["cwl_start_at"] == "2026-09-01T08:00Z"

    @pytest.mark.integration
    async def test_get_previous_cwl_event_clans_sync_excludes_non_participating(self, db):
        """Carry-over must only offer clans that were actually participating last season — a
        clan deactivated then (row kept for its settings) shouldn't silently reappear."""
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        await _seed_guild_and_clan(db, guild_id="111", clan_tag="#CLAN2")
        old_event_id = db.create_cwl_event_sync("111", "2026-07", "discordid1")
        db.set_cwl_event_clans_sync(old_event_id, [
            {"clan_tag": "#CLAN1", "participating": True},
            {"clan_tag": "#CLAN2", "participating": False},
        ])

        new_event_id = db.create_cwl_event_sync("111", "2026-08", "discordid1")
        prev_clans = db.get_previous_cwl_event_clans_sync("111", exclude_event_id=new_event_id)

        assert [c["clan_tag"] for c in prev_clans] == ["#CLAN1"]

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

    async def test_delete_cwl_event_sync_cascades_and_returns_true(self, db):
        """delete_cwl_event_sync() (the "Delete Season" admin action's backing call, distinct
        from update_cwl_event_status_sync()'s normal lifecycle transitions) uses a separate
        sync sqlite3 connection — confirm cascade delete still applies there too, not just on
        the async connection exercised by the raw-SQL test above."""
        await _seed_guild_and_clan(db, guild_id="222", clan_tag="#CLAN1")
        event_id = db.create_cwl_event_sync("222", "2026-09", "discordid1")
        db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1"}])

        assert db.delete_cwl_event_sync(event_id) is True
        assert db.get_cwl_event_sync("222", "2026-09") is None
        assert db.get_cwl_event_clans_sync(event_id) == []


class TestGuildConfigCwlColumns:
    @pytest.mark.integration
    async def test_save_and_get_cwl_guild_config_columns(self, db):
        await db.save_guild_config("111", {
            "cwl_hub_channel_id": "555",
            "cwl_hub_message_enabled": True,
            "cwl_management_channel_id": "666",
            "cwl_management_message_enabled": True,
            "cwl_retention_months": 12,
            "cwl_selected_season": "2026-09",
            "timezone_offset_minutes": 330,
        })

        cfg = await db.get_guild_config("111")
        assert cfg["cwl_hub_channel_id"] == "555"
        assert cfg["cwl_hub_message_enabled"] is True
        assert cfg["cwl_management_channel_id"] == "666"
        assert cfg["cwl_management_message_enabled"] is True
        assert cfg["cwl_retention_months"] == 12
        assert cfg["cwl_selected_season"] == "2026-09"
        assert cfg["timezone_offset_minutes"] == 330
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
        assert cfg["cwl_selected_season"] is None
        assert cfg["timezone_offset_minutes"] == 0


class TestCwlSignupsCrud:
    @pytest.mark.integration
    async def test_bulk_create_and_get_signups_roundtrip(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        event_id = db.create_cwl_event_sync("111", "2026-08", "discordid1")

        ok = db.bulk_create_cwl_signups_sync(event_id, [
            {"player_tag": "#P2", "player_name": "Bravo", "discord_id": "d2", "source": "template_confirm"},
            {"player_tag": "#P1", "player_name": "Alpha", "discord_id": "d1", "source": "template_confirm"},
        ])
        assert ok is True

        signups = db.get_cwl_signups_for_event_sync(event_id)
        assert [s["player_tag"] for s in signups] == ["#P1", "#P2"]  # ORDER BY player_name
        assert signups[0]["status"] == "pending"
        assert signups[0]["source"] == "template_confirm"

    @pytest.mark.integration
    async def test_bulk_create_is_idempotent_and_does_not_clobber_existing_rows(self, db):
        """ON CONFLICT DO NOTHING — a re-run of the template copy must never overwrite a
        row that's already there (e.g. one already responded to)."""
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        event_id = db.create_cwl_event_sync("111", "2026-08", "discordid1")

        db.bulk_create_cwl_signups_sync(event_id, [
            {"player_tag": "#P1", "player_name": "Alpha", "source": "template_confirm", "status": "pending"},
        ])
        db.upsert_cwl_signup_sync(event_id, "#P1", "Alpha", "d1", None, "template_confirm", "confirmed", responded_at="2026-08-10T00:00Z")

        # Re-running the bulk seed must not revert the already-confirmed row back to pending.
        db.bulk_create_cwl_signups_sync(event_id, [
            {"player_tag": "#P1", "player_name": "Alpha", "source": "template_confirm", "status": "pending"},
        ])

        signup = db.get_cwl_signup_sync(event_id, "#P1")
        assert signup["status"] == "confirmed"
        assert signup["responded_at"] == "2026-08-10T00:00Z"

    @pytest.mark.integration
    async def test_upsert_creates_then_overwrites_on_conflict(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        event_id = db.create_cwl_event_sync("111", "2026-08", "discordid1")

        db.upsert_cwl_signup_sync(event_id, "#P1", "Alpha", "d1", "Champion League I", "self_signup", "confirmed")
        first = db.get_cwl_signup_sync(event_id, "#P1")
        assert first["status"] == "confirmed"
        assert first["preferred_league_rank"] == "Champion League I"

        # Re-signing up with a different preference must overwrite, not create a second row.
        db.upsert_cwl_signup_sync(event_id, "#P1", "Alpha", "d1", "Master League I", "self_signup", "confirmed")
        rows = db.get_cwl_signups_for_event_sync(event_id)
        assert len(rows) == 1
        assert rows[0]["preferred_league_rank"] == "Master League I"

    @pytest.mark.integration
    async def test_get_cwl_signups_for_event_sync_filters_by_status(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        event_id = db.create_cwl_event_sync("111", "2026-08", "discordid1")
        db.upsert_cwl_signup_sync(event_id, "#P1", "Alpha", "d1", None, "self_signup", "confirmed")
        db.upsert_cwl_signup_sync(event_id, "#P2", "Bravo", "d2", None, "template_confirm", "pending")

        assert len(db.get_cwl_signups_for_event_sync(event_id, status="confirmed")) == 1
        assert len(db.get_cwl_signups_for_event_sync(event_id, status="declined")) == 0
        assert len(db.get_cwl_signups_for_event_sync(event_id)) == 2

    @pytest.mark.integration
    async def test_get_cwl_signup_sync_returns_none_for_missing(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        event_id = db.create_cwl_event_sync("111", "2026-08", "discordid1")
        assert db.get_cwl_signup_sync(event_id, "#NOPE") is None


class TestGetPreviousCwlParticipants:
    @pytest.mark.integration
    async def test_returns_empty_for_no_clans(self, db):
        assert db.get_previous_cwl_participants_sync([], "2026-07") == []

    @pytest.mark.integration
    async def test_returns_empty_when_no_roster_data_exists(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        assert db.get_previous_cwl_participants_sync(["#CLAN1"], "2026-07") == []

    @pytest.mark.integration
    async def test_resolves_linked_discord_id_and_preferences(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        await _seed_cwl_roster_war(db, "#CLAN1", "2026-07", [("#P1", "Alpha", 15, 1)])
        await _seed_user_player(db, "d1", "#P1", cwl_default_preferred_league_rank="Champion League I")

        participants = db.get_previous_cwl_participants_sync(["#CLAN1"], "2026-07")
        assert len(participants) == 1
        p = participants[0]
        assert p["player_tag"] == "#P1"
        assert p["clan_tag"] == "#CLAN1"
        assert p["discord_id"] == "d1"
        assert p["verified"] is True
        assert p["cwl_permanent_optout"] is False
        assert p["preferred_league_rank"] == "Champion League I"

    @pytest.mark.integration
    async def test_unlinked_player_has_none_discord_id(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        await _seed_cwl_roster_war(db, "#CLAN1", "2026-07", [("#P1", "Alpha", 15, 1)])

        participants = db.get_previous_cwl_participants_sync(["#CLAN1"], "2026-07")
        assert len(participants) == 1
        assert participants[0]["discord_id"] is None
        assert participants[0]["verified"] is False
        assert participants[0]["cwl_permanent_optout"] is False

    @pytest.mark.integration
    async def test_dedups_player_appearing_in_multiple_clans_rosters(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        await _seed_guild_and_clan(db, guild_id="111", clan_tag="#CLAN2")
        await _seed_cwl_roster_war(db, "#CLAN1", "2026-07", [("#P1", "Alpha", 15, 1)])
        await _seed_cwl_roster_war(db, "#CLAN2", "2026-07", [("#P1", "Alpha", 15, 1)])

        participants = db.get_previous_cwl_participants_sync(["#CLAN1", "#CLAN2"], "2026-07")
        assert len(participants) == 1
        assert participants[0]["clan_tag"] == "#CLAN1"  # first clan in the list wins

    @pytest.mark.integration
    async def test_prefers_verified_link_when_multiple_accounts_claim_the_same_tag(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        await _seed_cwl_roster_war(db, "#CLAN1", "2026-07", [("#P1", "Alpha", 15, 1)])
        await _seed_user_player(db, "d_unverified", "#P1", verified=False)
        await _seed_user_player(db, "d_verified", "#P1", verified=True)

        participants = db.get_previous_cwl_participants_sync(["#CLAN1"], "2026-07")
        assert participants[0]["discord_id"] == "d_verified"
        assert participants[0]["verified"] is True

    @pytest.mark.integration
    async def test_permanent_optout_is_surfaced_not_filtered(self, db):
        """This query only resolves data — filtering opted-out accounts out of the
        template copy is Start Enrollment's responsibility, not this query's."""
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        await _seed_cwl_roster_war(db, "#CLAN1", "2026-07", [("#P1", "Alpha", 15, 1)])
        await _seed_user_player(db, "d1", "#P1", cwl_permanent_optout=True)

        participants = db.get_previous_cwl_participants_sync(["#CLAN1"], "2026-07")
        assert len(participants) == 1
        assert participants[0]["cwl_permanent_optout"] is True
