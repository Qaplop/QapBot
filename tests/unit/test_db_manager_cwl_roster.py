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


async def _seed_cwl_war(
    db: WarHistoryDB, clan_tag: str, cwl_season: str, players: list, date: str = "2026-07-01T08:00", war_id: str = None,
) -> None:
    """Seed a minimal is_cwl=1 war_summary + war_attacks pair. players: list of
    (player_tag, player_name, th_level, map_position) tuples. All attacks share `date`."""
    war_id = war_id or f"war_{clan_tag}_{cwl_season}_{date}"
    await db.conn.execute(
        "INSERT INTO war_summary (war_id, clan_tag, opponent_tag, is_cwl, cwl_season, date) VALUES (?, ?, ?, 1, ?, ?)",
        (war_id, clan_tag, "#OPP", cwl_season, date),
    )
    for player_tag, player_name, th_level, map_position in players:
        await db.conn.execute(
            "INSERT INTO war_attacks (war_id, clan_tag, date, player_name, player_tag, th_level, map_position, stars) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (war_id, clan_tag, date, player_name, player_tag, th_level, map_position),
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
    current_clan_tag: str = None,
) -> None:
    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES (?, ?)", (discord_id, discord_id))
    await db.conn.execute(
        """
        INSERT INTO user_players
            (discord_id, player_tag, player_name, verified, cwl_permanent_optout, cwl_default_preferred_league_rank, current_clan_tag)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (discord_id, player_tag, player_name, 1 if verified else 0, 1 if cwl_permanent_optout else 0, cwl_default_preferred_league_rank, current_clan_tag),
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
    async def test_deactivate_cwl_event_clan_sync_flips_participating_only(self, db):
        """Narrower than set_cwl_event_clans_sync (which replaces the whole set) — must leave
        roster_size/tier_order/cwl_start_at untouched, only participating flips."""
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        event_id = db.create_cwl_event_sync("111", "2026-08", "discordid1")
        db.set_cwl_event_clans_sync(event_id, [
            {"clan_tag": "#CLAN1", "roster_size": 30, "tier_order": 2, "cwl_start_at": "2026-08-01T08:00Z", "participating": True},
        ])

        assert db.deactivate_cwl_event_clan_sync(event_id, "#CLAN1") is True

        clan = db.get_cwl_event_clans_sync(event_id)[0]
        assert clan["participating"] == 0
        assert clan["roster_size"] == 30
        assert clan["tier_order"] == 2
        assert clan["cwl_start_at"] == "2026-08-01T08:00Z"

    @pytest.mark.integration
    async def test_deactivate_cwl_event_clan_sync_nonexistent_row_is_a_noop(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        event_id = db.create_cwl_event_sync("111", "2026-08", "discordid1")
        assert db.deactivate_cwl_event_clan_sync(event_id, "#NEVER") is True
        assert db.get_cwl_event_clans_sync(event_id) == []

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
            "timezone_name": "Europe/Berlin",
        })

        cfg = await db.get_guild_config("111")
        assert cfg["cwl_hub_channel_id"] == "555"
        assert cfg["cwl_hub_message_enabled"] is True
        assert cfg["cwl_management_channel_id"] == "666"
        assert cfg["cwl_management_message_enabled"] is True
        assert cfg["cwl_retention_months"] == 12
        assert cfg["cwl_selected_season"] == "2026-09"
        assert cfg["timezone_name"] == "Europe/Berlin"
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
        assert cfg["timezone_name"] == "UTC"


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


class TestGetCurrentClanMembers:
    """get_current_clan_members_sync() replaced get_previous_cwl_participants_sync() on
    2026-08-10 after a live DEV test showed Start Enrollment seeding zero signups for a clan
    with no tracked CWL war history — current membership (user_players.current_clan_tag) is the
    right source, not last season's CWL attacker list."""

    @pytest.mark.integration
    async def test_returns_empty_for_no_clans(self, db):
        assert db.get_current_clan_members_sync([]) == []

    @pytest.mark.integration
    async def test_returns_empty_when_no_members_tracked(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        assert db.get_current_clan_members_sync(["#CLAN1"]) == []

    @pytest.mark.integration
    async def test_resolves_linked_discord_id_and_preferences(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        await _seed_user_player(
            db, "d1", "#P1", cwl_default_preferred_league_rank="Champion League I",
            current_clan_tag="#CLAN1",
        )

        members = db.get_current_clan_members_sync(["#CLAN1"])
        assert len(members) == 1
        m = members[0]
        assert m["player_tag"] == "#P1"
        assert m["clan_tag"] == "#CLAN1"
        assert m["discord_id"] == "d1"
        assert m["verified"] is True
        assert m["cwl_permanent_optout"] is False
        assert m["preferred_league_rank"] == "Champion League I"

    @pytest.mark.integration
    async def test_unassigned_sentinel_has_none_discord_id(self, db):
        """The 'UNASSIGNED' discord_id is the bot's own placeholder for a tracked-but-unlinked
        account (QBdiscocmdshelper.py) — not a real Discord user, must resolve to None so
        start_cwl_enrollment() correctly counts it as skipped_unlinked rather than trying to DM
        the literal string 'UNASSIGNED'."""
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        await _seed_user_player(db, "UNASSIGNED", "#P1", verified=False, current_clan_tag="#CLAN1")

        members = db.get_current_clan_members_sync(["#CLAN1"])
        assert len(members) == 1
        assert members[0]["discord_id"] is None

    @pytest.mark.integration
    async def test_departed_member_is_excluded(self, db):
        """A player whose current_clan_tag no longer matches (they left, or the bot's tracking
        moved them elsewhere) must not show up — unlike a "last season's roster" source, which
        would have kept including them after they left."""
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        await _seed_guild_and_clan(db, guild_id="111", clan_tag="#CLAN2")
        await _seed_user_player(db, "d1", "#P1", current_clan_tag="#CLAN2")  # now in a different clan

        assert db.get_current_clan_members_sync(["#CLAN1"]) == []
        assert len(db.get_current_clan_members_sync(["#CLAN2"])) == 1

    @pytest.mark.integration
    async def test_prefers_verified_link_when_multiple_accounts_claim_the_same_tag(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        await _seed_user_player(db, "d_unverified", "#P1", verified=False, current_clan_tag="#CLAN1")
        await _seed_user_player(db, "d_verified", "#P1", verified=True, current_clan_tag="#CLAN1")

        members = db.get_current_clan_members_sync(["#CLAN1"])
        assert len(members) == 1
        assert members[0]["discord_id"] == "d_verified"
        assert members[0]["verified"] is True

    @pytest.mark.integration
    async def test_permanent_optout_is_surfaced_not_filtered(self, db):
        """This query only resolves data — filtering opted-out accounts out of the seed pool
        is Start Enrollment's responsibility, not this query's."""
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        await _seed_user_player(db, "d1", "#P1", cwl_permanent_optout=True, current_clan_tag="#CLAN1")

        members = db.get_current_clan_members_sync(["#CLAN1"])
        assert len(members) == 1
        assert members[0]["cwl_permanent_optout"] is True

    @pytest.mark.integration
    async def test_multiple_members_across_multiple_clans(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        await _seed_guild_and_clan(db, guild_id="111", clan_tag="#CLAN2")
        await _seed_user_player(db, "d1", "#P1", current_clan_tag="#CLAN1")
        await _seed_user_player(db, "d2", "#P2", current_clan_tag="#CLAN2")

        members = db.get_current_clan_members_sync(["#CLAN1", "#CLAN2"])
        assert {m["player_tag"] for m in members} == {"#P1", "#P2"}


class TestGetMostRecentCwlWarRoster:
    """get_most_recent_cwl_war_roster_sync() — the "Manage Enrollment" auto-assignment seed's
    per-clan data source: this clan's own most recent CWL war, regardless of season."""

    @pytest.mark.integration
    async def test_returns_empty_when_no_cwl_war_exists(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        assert db.get_most_recent_cwl_war_roster_sync("#CLAN1") == []

    @pytest.mark.integration
    async def test_ignores_non_cwl_wars(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        await db.conn.execute(
            "INSERT INTO war_summary (war_id, clan_tag, opponent_tag, is_cwl, cwl_season, date) "
            "VALUES ('regular_war', '#CLAN1', '#OPP', 0, '', '2026-07-15T10:00')"
        )
        await db.conn.execute(
            "INSERT INTO war_attacks (war_id, clan_tag, date, player_name, player_tag, th_level, map_position, stars) "
            "VALUES ('regular_war', '#CLAN1', '2026-07-15T10:00', 'Alpha', '#P1', 15, 1, 0)"
        )
        await db.conn.commit()
        assert db.get_most_recent_cwl_war_roster_sync("#CLAN1") == []

    @pytest.mark.integration
    async def test_finds_most_recent_cwl_war_regardless_of_season(self, db):
        """The whole point: no cwl_season filter — a clan that skipped last month resolves
        against whichever season it last actually played."""
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        await _seed_cwl_war(db, "#CLAN1", "2026-05", [("#OLD", "Old", 14, 1)], date="2026-05-01T08:00")
        await _seed_cwl_war(db, "#CLAN1", "2026-07", [("#NEW", "New", 15, 1)], date="2026-07-01T08:00")

        roster = db.get_most_recent_cwl_war_roster_sync("#CLAN1")
        assert [r["player_tag"] for r in roster] == ["#NEW"]
        assert roster[0]["date"] == "2026-07-01T08:00"

    @pytest.mark.integration
    async def test_returns_date_for_every_player(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        await _seed_cwl_war(
            db, "#CLAN1", "2026-07",
            [("#P1", "Alpha", 15, 1), ("#P2", "Bravo", 14, 2)],
            date="2026-07-01T08:00",
        )
        roster = db.get_most_recent_cwl_war_roster_sync("#CLAN1")
        assert {r["player_tag"]: r["date"] for r in roster} == {
            "#P1": "2026-07-01T08:00", "#P2": "2026-07-01T08:00",
        }


class TestCwlAssignmentsCrud:
    @pytest.mark.integration
    async def test_upsert_creates_then_overwrites(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        await _seed_guild_and_clan(db, guild_id="111", clan_tag="#CLAN2")
        event_id = db.create_cwl_event_sync("111", "2026-08", "discordid1")

        assert db.upsert_cwl_assignment_sync(event_id, "#P1", "#CLAN1", assignment_source="suggested") is True
        assignments = db.get_cwl_assignments_sync(event_id)
        assert len(assignments) == 1
        assert assignments[0]["assigned_clan_tag"] == "#CLAN1"
        assert assignments[0]["assignment_source"] == "suggested"
        assert assignments[0]["locked"] == 0

        # Drag-and-drop move: overwrite, not a second row.
        db.upsert_cwl_assignment_sync(event_id, "#P1", "#CLAN2", assignment_source="admin_override", locked=True)
        assignments = db.get_cwl_assignments_sync(event_id)
        assert len(assignments) == 1
        assert assignments[0]["assigned_clan_tag"] == "#CLAN2"
        assert assignments[0]["assignment_source"] == "admin_override"
        assert assignments[0]["locked"] == 1

    @pytest.mark.integration
    async def test_bulk_create_is_idempotent_and_preserves_manual_overrides(self, db):
        """Re-running the seed (defensively) must never clobber a drag-and-drop move that
        happened since — same ON CONFLICT DO NOTHING discipline as bulk_create_cwl_signups_sync."""
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        await _seed_guild_and_clan(db, guild_id="111", clan_tag="#CLAN2")
        event_id = db.create_cwl_event_sync("111", "2026-08", "discordid1")

        db.bulk_create_cwl_assignments_sync(event_id, [{"player_tag": "#P1", "assigned_clan_tag": "#CLAN1"}])
        db.upsert_cwl_assignment_sync(event_id, "#P1", "#CLAN2", assignment_source="admin_override", locked=True)

        # Re-running the seed must not revert the manual override back to #CLAN1.
        db.bulk_create_cwl_assignments_sync(event_id, [{"player_tag": "#P1", "assigned_clan_tag": "#CLAN1"}])

        assignments = db.get_cwl_assignments_sync(event_id)
        assert len(assignments) == 1
        assert assignments[0]["assigned_clan_tag"] == "#CLAN2"
        assert assignments[0]["locked"] == 1

    @pytest.mark.integration
    async def test_bulk_create_defaults_suggested_clan_tag_to_assigned(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        event_id = db.create_cwl_event_sync("111", "2026-08", "discordid1")
        db.bulk_create_cwl_assignments_sync(event_id, [{"player_tag": "#P1", "assigned_clan_tag": "#CLAN1"}])
        assignments = db.get_cwl_assignments_sync(event_id)
        assert assignments[0]["suggested_clan_tag"] == "#CLAN1"

    @pytest.mark.integration
    async def test_get_cwl_assignments_sync_empty_event(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        event_id = db.create_cwl_event_sync("111", "2026-08", "discordid1")
        assert db.get_cwl_assignments_sync(event_id) == []

    @pytest.mark.integration
    async def test_delete_cwl_assignment_sync_removes_the_row(self, db):
        """Unassigned = no row (assigned_clan_tag is NOT NULL) — dragging to the Unassigned
        pool must delete, not null out, the row."""
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        event_id = db.create_cwl_event_sync("111", "2026-08", "discordid1")
        db.upsert_cwl_assignment_sync(event_id, "#P1", "#CLAN1")

        assert db.delete_cwl_assignment_sync(event_id, "#P1") is True
        assert db.get_cwl_assignments_sync(event_id) == []

    @pytest.mark.integration
    async def test_delete_cwl_assignment_sync_nonexistent_row_is_a_noop(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        event_id = db.create_cwl_event_sync("111", "2026-08", "discordid1")
        assert db.delete_cwl_assignment_sync(event_id, "#NEVER") is True
