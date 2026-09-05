"""Tests for the CWL roster planning schema and sync CRUD (CWL_ROSTER_PLANNING_PLAN.md Phase 1).

Covers: cwl_events/cwl_event_clans CRUD roundtrip, UNIQUE-constraint idempotency,
cascade-delete from cwl_events down to its child tables, get_previous_cwl_event_clans_sync's
carry-over query, and the guild_config CWL columns' save/get roundtrip.
"""
from __future__ import annotations

from typing import Optional

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
    await db._conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
    await db._conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES (?, ?)", (clan_tag, "Test Clan"))
    await db._conn.commit()


async def _seed_cwl_war(
    db: WarHistoryDB, clan_tag: str, cwl_season: str, players: list, date: str = "2026-07-01T08:00", war_id: Optional[str] = None,
    attack_order: int = 1,
) -> None:
    """Seed a minimal is_cwl=1 war_summary + war_attacks pair. players: list of
    (player_tag, player_name, th_level, map_position) tuples. All attacks share `date`.
    attack_order defaults to 1 (a real attack) — pass 0 to seed a "missed attack" sentinel row."""
    war_id = war_id or f"war_{clan_tag}_{cwl_season}_{date}"
    await db._conn.execute(
        "INSERT INTO war_summary (war_id, clan_tag, opponent_tag, is_cwl, cwl_season, date) VALUES (?, ?, ?, 1, ?, ?)",
        (war_id, clan_tag, "#OPP", cwl_season, date),
    )
    for player_tag, player_name, th_level, map_position in players:
        await db._conn.execute(
            "INSERT INTO war_attacks "
            "(war_id, clan_tag, date, player_name, player_tag, th_level, map_position, attack_order, stars) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (war_id, clan_tag, date, player_name, player_tag, th_level, map_position, attack_order),
        )
    await db._conn.commit()


async def _seed_clan(db: WarHistoryDB, clan_tag: str, name: str = "Some Clan") -> None:
    """user_players.current_clan_tag has a FK to clans — a clan must exist here before a
    player_tag can be seeded as currently in it, even one this test deliberately keeps out of
    any guild's member_clans/member_families (i.e. "known to the bot, but not this guild's own
    family" — see get_all_players_for_discord_ids_sync's docstring)."""
    await db._conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES (?, ?)", (clan_tag, name))
    await db._conn.commit()


async def _seed_user_player(
    db: WarHistoryDB,
    discord_id: str,
    player_tag: str,
    player_name: str = "Player",
    verified: bool = True,
    cwl_permanent_optout: bool = False,
    cwl_default_preferred_league_rank: Optional[str] = None,
    current_clan_tag: Optional[str] = None,
    th_level: Optional[int] = None,
) -> None:
    await db._conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES (?, ?)", (discord_id, discord_id))
    await db._conn.execute(
        """
        INSERT INTO user_players
            (discord_id, player_tag, player_name, verified, cwl_permanent_optout, cwl_default_preferred_league_rank, current_clan_tag, th_level)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (discord_id, player_tag, player_name, 1 if verified else 0, 1 if cwl_permanent_optout else 0, cwl_default_preferred_league_rank, current_clan_tag, th_level),
    )
    await db._conn.commit()


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

    @pytest.mark.integration
    async def test_list_cwl_events_for_season_across_guilds(self, db):
        """list_cwl_events_for_season_across_guilds_sync (2026-08-16, /list "Managed CWLs"):
        deliberately guild-agnostic, unlike every other cwl_events query in this file."""
        await _seed_guild_and_clan(db, guild_id="111")
        await _seed_guild_and_clan(db, guild_id="222")
        await _seed_guild_and_clan(db, guild_id="333")
        db.create_cwl_event_sync("111", "2026-09", "discordid1")
        event_222 = db.create_cwl_event_sync("222", "2026-09", "discordid2")
        db.update_cwl_event_status_sync(event_222, "signup_open")
        db.create_cwl_event_sync("333", "2026-08", "discordid3")  # different season — excluded

        rows = db.list_cwl_events_for_season_across_guilds_sync("2026-09")

        by_guild = {r["guild_id"]: r for r in rows}
        assert set(by_guild.keys()) == {"111", "222"}
        assert by_guild["111"]["status"] == "draft"
        assert by_guild["222"]["status"] == "signup_open"

    @pytest.mark.integration
    async def test_list_cwl_events_for_season_across_guilds_empty_for_no_matches(self, db):
        await _seed_guild_and_clan(db, guild_id="111")
        db.create_cwl_event_sync("111", "2026-09", "discordid1")

        assert db.list_cwl_events_for_season_across_guilds_sync("2099-01") == []


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

    @pytest.mark.integration
    async def test_get_previous_cwl_season_sync_returns_most_recent_prior_season(self, db):
        await _seed_guild_and_clan(db)
        db.create_cwl_event_sync("111", "2026-06", "discordid1")
        db.create_cwl_event_sync("111", "2026-07", "discordid1")
        new_event_id = db.create_cwl_event_sync("111", "2026-08", "discordid1")

        assert db.get_previous_cwl_season_sync("111", exclude_event_id=new_event_id) == "2026-07"

    @pytest.mark.integration
    async def test_get_previous_cwl_season_sync_no_prior_event_returns_none(self, db):
        await _seed_guild_and_clan(db)
        event_id = db.create_cwl_event_sync("111", "2026-08", "discordid1")
        assert db.get_previous_cwl_season_sync("111", exclude_event_id=event_id) is None

    @pytest.mark.integration
    async def test_get_clans_with_cwl_data_for_season_sync_splits_played_and_did_not(self, db):
        """The real-war-history pre-set source for CwlCarryOverPromptView._create_season
        (ui_cwl_roster.py) — a batched version of has_cwl_season_data_sync."""
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        await _seed_guild_and_clan(db, guild_id="111", clan_tag="#CLAN2")
        await _seed_cwl_war(db, "#CLAN1", "2026-07", [("#P1", "Player One", 15, 1)])

        played = db.get_clans_with_cwl_data_for_season_sync(["#CLAN1", "#CLAN2"], "2026-07")

        assert played == {"#CLAN1"}

    @pytest.mark.integration
    async def test_get_clans_with_cwl_data_for_season_sync_ignores_other_seasons(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        await _seed_cwl_war(db, "#CLAN1", "2026-06", [("#P1", "Player One", 15, 1)])

        assert db.get_clans_with_cwl_data_for_season_sync(["#CLAN1"], "2026-07") == set()

    @pytest.mark.integration
    async def test_get_clans_with_cwl_data_for_season_sync_empty_clan_list(self, db):
        assert db.get_clans_with_cwl_data_for_season_sync([], "2026-07") == set()


class TestCwlCascadeDelete:
    @pytest.mark.integration
    async def test_deleting_cwl_event_cascades_to_child_tables(self, db):
        """cwl_event_clans/cwl_signups/cwl_assignments all FK -> cwl_events ON DELETE CASCADE."""
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        event_id = db.create_cwl_event_sync("111", "2026-08", "discordid1")
        db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1"}])
        await db._conn.execute(
            "INSERT INTO cwl_signups (event_id, player_tag, source, status) VALUES (?, ?, ?, ?)",
            (event_id, "#PLAYER1", "self_signup", "confirmed"),
        )
        await db._conn.execute(
            "INSERT INTO cwl_assignments (event_id, player_tag, assigned_clan_tag) VALUES (?, ?, ?)",
            (event_id, "#PLAYER1", "#CLAN1"),
        )
        await db._conn.commit()

        assert len(db.get_cwl_event_clans_sync(event_id)) == 1

        await db._conn.execute("DELETE FROM cwl_events WHERE id = ?", (event_id,))
        await db._conn.commit()

        assert db.get_cwl_event_clans_sync(event_id) == []
        cursor = await db._conn.execute("SELECT COUNT(*) AS n FROM cwl_signups WHERE event_id = ?", (event_id,))
        row = await cursor.fetchone()
        assert row["n"] == 0
        cursor2 = await db._conn.execute("SELECT COUNT(*) AS n FROM cwl_assignments WHERE event_id = ?", (event_id,))
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

    async def test_delete_cwl_event_sync_clears_dm_sent_for_this_event(self, db):
        """2026-08-19 live bug report: deleting a season and immediately starting a new one for
        the SAME season string silently DMed nobody, because cwl_player_season_status.dm_sent
        (deliberately not cascaded from cwl_events — see its own CREATE TABLE comment) was still
        standing from the deleted event. delete_cwl_event_sync() must now clear it for any row
        entirely attributable to the event being deleted."""
        await _seed_guild_and_clan(db, guild_id="223", clan_tag="#CLAN1")
        event_id = db.create_cwl_event_sync("223", "2026-09", "discordid1")
        db.mark_cwl_player_dm_sent_sync(
            "#P1", "2026-09", "PlayerOne", "10", event_id, 223, "2026-08-19T09:00Z", "msg1", "chan1",
        )

        assert db.get_cwl_player_season_status_sync("#P1", "2026-09")["dm_sent"] == 1

        assert db.delete_cwl_event_sync(event_id) is True

        assert db.get_cwl_player_season_status_sync("#P1", "2026-09") is None

    async def test_delete_cwl_event_sync_preserves_dm_sent_responded_via_another_event(self, db):
        """A player who DID respond via a DIFFERENT, still-existing event (the cross-guild
        shared-clan case) must keep their dm_sent record — deleting THIS event must not make
        them eligible for a duplicate DM through the other, still-live event."""
        await _seed_guild_and_clan(db, guild_id="224", clan_tag="#CLAN1")
        await db._conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES ('225')")
        await db._conn.commit()
        event_id = db.create_cwl_event_sync("224", "2026-09", "discordid1")
        other_event_id = db.create_cwl_event_sync("225", "2026-09", "discordid1")
        db.mark_cwl_player_dm_sent_sync(
            "#P1", "2026-09", "PlayerOne", "10", event_id, 224, "2026-08-19T09:00Z", "msg1", "chan1",
        )
        db.set_cwl_player_response_status_sync(
            "#P1", "2026-09", "PlayerOne", "10", "confirmed", "2026-08-19T09:05Z", other_event_id, 225,
        )

        assert db.delete_cwl_event_sync(event_id) is True

        status = db.get_cwl_player_season_status_sync("#P1", "2026-09")
        assert status is not None
        assert status["dm_sent"] == 1
        assert status["status"] == "confirmed"

    async def test_get_cwl_player_season_status_dm_refs_for_event_sync(self, db):
        await _seed_guild_and_clan(db, guild_id="226", clan_tag="#CLAN1")
        await db._conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES ('227')")
        await db._conn.commit()
        event_id = db.create_cwl_event_sync("226", "2026-09", "discordid1")
        other_event_id = db.create_cwl_event_sync("227", "2026-09", "discordid1")
        db.mark_cwl_player_dm_sent_sync(
            "#P1", "2026-09", "PlayerOne", "10", event_id, 226, "2026-08-19T09:00Z", "msg1", "chan1",
        )
        # Responded via a DIFFERENT still-existing event — must NOT be offered up for retraction,
        # matching delete_cwl_event_sync()'s own scoping (it won't be deleted either).
        db.mark_cwl_player_dm_sent_sync(
            "#P2", "2026-09", "PlayerTwo", "20", event_id, 226, "2026-08-19T09:00Z", "msg2", "chan2",
        )
        db.set_cwl_player_response_status_sync(
            "#P2", "2026-09", "PlayerTwo", "20", "confirmed", "2026-08-19T09:05Z", other_event_id, 227,
        )

        refs = db.get_cwl_player_season_status_dm_refs_for_event_sync(event_id)

        assert refs == [{"player_tag": "#P1", "dmed_discord_id": "10", "message_id": "msg1", "channel_id": "chan1"}]


class TestGuildConfigCwlColumns:
    @pytest.mark.integration
    async def test_save_and_get_cwl_guild_config_columns(self, db):
        await db.save_guild_config("111", {
            "cwl_player_hub_channel_id": "555",
            "cwl_player_hub_message_enabled": True,
            "cwl_management_channel_id": "666",
            "cwl_management_message_enabled": True,
            "cwl_retention_months": 12,
            "cwl_selected_season": "2026-09",
            "timezone_name": "Europe/Berlin",
        })

        cfg = await db.get_guild_config("111")
        assert cfg["cwl_player_hub_channel_id"] == "555"
        assert cfg["cwl_player_hub_message_enabled"] is True
        assert cfg["cwl_management_channel_id"] == "666"
        assert cfg["cwl_management_message_enabled"] is True
        assert cfg["cwl_retention_months"] == 12
        assert cfg["cwl_selected_season"] == "2026-09"
        assert cfg["timezone_name"] == "Europe/Berlin"
        # Untouched fields default sanely
        assert cfg["cwl_player_hub_message_id"] is None
        assert cfg["cwl_management_message_id"] is None

    @pytest.mark.integration
    async def test_cwl_guild_config_defaults_for_fresh_guild(self, db):
        await db.save_guild_config("222", {})
        cfg = await db.get_guild_config("222")
        assert cfg["cwl_player_hub_message_enabled"] is False
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
    async def test_returns_th_level(self, db):
        """th_level is kept fresh for every current member (linked or not) by coc_cache.py's
        per-clan poll cycle (2026-08-14) — the Manage Enrollment board's primary TH source."""
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        await _seed_user_player(db, "d1", "#P1", current_clan_tag="#CLAN1", th_level=15)
        await _seed_user_player(db, "d2", "#P2", current_clan_tag="#CLAN1")  # never synced yet

        members_by_tag = {m["player_tag"]: m for m in db.get_current_clan_members_sync(["#CLAN1"])}
        assert members_by_tag["#P1"]["th_level"] == 15
        assert members_by_tag["#P2"]["th_level"] is None

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
        await db._conn.execute(
            "INSERT INTO war_summary (war_id, clan_tag, opponent_tag, is_cwl, cwl_season, date) "
            "VALUES ('regular_war', '#CLAN1', '#OPP', 0, '', '2026-07-15T10:00')"
        )
        await db._conn.execute(
            "INSERT INTO war_attacks (war_id, clan_tag, date, player_name, player_tag, th_level, map_position, stars) "
            "VALUES ('regular_war', '#CLAN1', '2026-07-15T10:00', 'Alpha', '#P1', 15, 1, 0)"
        )
        await db._conn.commit()
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


class TestGetLastRealCwlAttackClan:
    """get_last_real_cwl_attack_clan_sync() — the player-centric auto-assignment source
    (2026-08-14 redesign): each player's own most recent REAL CWL attack, any clan, any season."""

    @pytest.mark.integration
    async def test_empty_input_returns_empty_without_querying(self, db):
        assert db.get_last_real_cwl_attack_clan_sync([]) == {}

    @pytest.mark.integration
    async def test_player_with_no_history_absent_from_result(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        assert db.get_last_real_cwl_attack_clan_sync(["#NEVERSEEN"]) == {}

    @pytest.mark.integration
    async def test_excludes_zero_attack_sentinel_rows(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        await _seed_cwl_war(db, "#CLAN1", "2026-07", [("#P1", "Alpha", 15, 1)], attack_order=0)
        assert db.get_last_real_cwl_attack_clan_sync(["#P1"]) == {}

    @pytest.mark.integration
    async def test_excludes_non_cwl_wars(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        await db._conn.execute(
            "INSERT INTO war_summary (war_id, clan_tag, opponent_tag, is_cwl, cwl_season, date) "
            "VALUES ('regular_war', '#CLAN1', '#OPP', 0, '', '2026-07-15T10:00')"
        )
        await db._conn.execute(
            "INSERT INTO war_attacks "
            "(war_id, clan_tag, date, player_name, player_tag, th_level, map_position, attack_order, stars) "
            "VALUES ('regular_war', '#CLAN1', '2026-07-15T10:00', 'Alpha', '#P1', 15, 1, 1, 3)"
        )
        await db._conn.commit()
        assert db.get_last_real_cwl_attack_clan_sync(["#P1"]) == {}

    @pytest.mark.integration
    async def test_picks_most_recent_across_clans(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        await _seed_guild_and_clan(db, guild_id="111", clan_tag="#CLAN2")
        await _seed_cwl_war(db, "#CLAN1", "2026-05", [("#P1", "Alpha", 15, 1)], date="2026-05-01T08:00")
        await _seed_cwl_war(db, "#CLAN2", "2026-07", [("#P1", "Alpha", 15, 1)], date="2026-07-01T08:00")

        result = db.get_last_real_cwl_attack_clan_sync(["#P1"])
        assert result["#P1"] == ("#CLAN2", "2026-07-01T08:00")

    @pytest.mark.integration
    async def test_resolves_multiple_players_independently(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        await _seed_cwl_war(
            db, "#CLAN1", "2026-07",
            [("#P1", "Alpha", 15, 1), ("#P2", "Bravo", 14, 2)],
            date="2026-07-01T08:00",
        )
        result = db.get_last_real_cwl_attack_clan_sync(["#P1", "#P2", "#NEVERSEEN"])
        assert result == {
            "#P1": ("#CLAN1", "2026-07-01T08:00"),
            "#P2": ("#CLAN1", "2026-07-01T08:00"),
        }


class TestGetMostRecentThLevels:
    """get_most_recent_th_levels_sync() — the "Manage Enrollment" board's per-player TH lookup
    (live-testing feedback, 2026-08-14). Unlike get_most_recent_cwl_war_roster_sync, this reads
    *any* war type, not just CWL — a player's TH is the same fact regardless of which war it was
    last observed in."""

    @pytest.mark.integration
    async def test_empty_input_returns_empty_without_querying(self, db):
        assert db.get_most_recent_th_levels_sync([]) == {}

    @pytest.mark.integration
    async def test_missing_player_absent_from_result(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        assert db.get_most_recent_th_levels_sync(["#NEVERSEEN"]) == {}

    @pytest.mark.integration
    async def test_returns_th_level_from_regular_war_not_just_cwl(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        await db._conn.execute(
            "INSERT INTO war_summary (war_id, clan_tag, opponent_tag, is_cwl, cwl_season, date) "
            "VALUES ('regular_war', '#CLAN1', '#OPP', 0, '', '2026-07-15T10:00')"
        )
        await db._conn.execute(
            "INSERT INTO war_attacks (war_id, clan_tag, date, player_name, player_tag, th_level, map_position, stars) "
            "VALUES ('regular_war', '#CLAN1', '2026-07-15T10:00', 'Alpha', '#P1', 15, 1, 0)"
        )
        await db._conn.commit()
        assert db.get_most_recent_th_levels_sync(["#P1"]) == {"#P1": 15}

    @pytest.mark.integration
    async def test_picks_the_most_recent_attack_across_multiple_wars(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        # Same player, two wars, TH upgraded in between — the later date must win.
        await _seed_cwl_war(db, "#CLAN1", "2026-05", [("#P1", "Alpha", 14, 1)], date="2026-05-01T08:00", war_id="war_old")
        await _seed_cwl_war(db, "#CLAN1", "2026-07", [("#P1", "Alpha", 15, 1)], date="2026-07-01T08:00", war_id="war_new")
        assert db.get_most_recent_th_levels_sync(["#P1"]) == {"#P1": 15}

    @pytest.mark.integration
    async def test_scoped_to_only_the_requested_player_tags(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        await _seed_cwl_war(
            db, "#CLAN1", "2026-07",
            [("#P1", "Alpha", 15, 1), ("#P2", "Bravo", 12, 2)],
            date="2026-07-01T08:00",
        )
        assert db.get_most_recent_th_levels_sync(["#P1"]) == {"#P1": 15}


async def _seed_cwl_attack_with_league(
    db, clan_tag: str, cwl_season: str, league_rank: str, player_tag: str, stars: int,
    date: str = "2026-07-01T08:00", war_id: Optional[str] = None, league_group_id: Optional[str] = None,
) -> None:
    """Seeds one CWL war_attacks row plus the league-reconstruction chain
    (cwl_league_rounds -> cwl_league_groups) get_recent_cwl_attacks_with_league_sync() needs.
    war_summary.war_tag must be set explicitly — it's a distinct column from war_id, defaults to
    '' if omitted, and is exactly what the league-tier join keys off."""
    war_id = war_id or f"war_{clan_tag}_{date}"
    league_group_id = league_group_id or f"group_{cwl_season}_{clan_tag}"
    await db._conn.execute(
        "INSERT INTO war_summary (war_id, clan_tag, opponent_tag, is_cwl, cwl_season, date, war_tag) VALUES (?, ?, ?, 1, ?, ?, ?)",
        (war_id, clan_tag, "#OPP", cwl_season, date, war_id),
    )
    await db._conn.execute(
        "INSERT INTO war_attacks (war_id, clan_tag, date, player_name, player_tag, th_level, map_position, stars) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (war_id, clan_tag, date, player_tag, player_tag, 15, 1, stars),
    )
    await db._conn.execute(
        "INSERT INTO cwl_league_rounds (war_tag, cwl_season, cwl_round, league_group_id) VALUES (?, ?, 1, ?)",
        (war_id, cwl_season, league_group_id),
    )
    await db._conn.execute(
        "INSERT OR IGNORE INTO cwl_league_groups (league_group_id, cwl_season, clan_tag, league_rank) VALUES (?, ?, ?, ?)",
        (league_group_id, cwl_season, clan_tag, league_rank),
    )
    await db._conn.commit()


class TestGetRecentCwlAttacksWithLeague:
    """get_recent_cwl_attacks_with_league_sync() — the raw per-attack data source behind
    compute_league_adjusted_skill_scores() (QBdiscocmdshelper_cwl.py). Its own tests there cover
    the weighting/averaging/trailing-3-month-window behavior end to end; these focus on this
    function's own contract: shape, ordering, the league_group_id join, and the since_date filter
    specifically."""

    @pytest.mark.integration
    async def test_returns_empty_for_no_player_tags(self, db):
        assert db.get_recent_cwl_attacks_with_league_sync([]) == {}

    @pytest.mark.integration
    async def test_returns_empty_when_no_cwl_attacks_exist(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        assert db.get_recent_cwl_attacks_with_league_sync(["#P1"]) == {}

    @pytest.mark.integration
    async def test_returns_stars_and_league_rank_most_recent_first(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        await _seed_cwl_attack_with_league(db, "#CLAN1", "2026-05", "Bronze League III", "#P1", stars=1, date="2026-05-01T08:00", war_id="w1")
        await _seed_cwl_attack_with_league(db, "#CLAN1", "2026-07", "Master League II", "#P1", stars=3, date="2026-07-01T08:00", war_id="w2")

        attacks = db.get_recent_cwl_attacks_with_league_sync(["#P1"])["#P1"]
        assert [a["stars"] for a in attacks] == [3, 1]  # most recent first
        assert attacks[0]["league_rank"] == "Master League II"
        assert attacks[1]["league_rank"] == "Bronze League III"

    @pytest.mark.integration
    async def test_resolves_league_via_shared_group_not_just_one_clan_row(self, db):
        """Two clans sharing one league_group_id (the normal case — a CWL league group is
        several clans) must both resolve to the same league_rank, joined via league_group_id
        rather than requiring a cwl_league_groups row for the specific attacking clan."""
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        await _seed_guild_and_clan(db, guild_id="111", clan_tag="#CLAN2")
        # Both clans in the same group_id/season; #CLAN1's own row is the one carrying
        # league_rank, but #CLAN2's attack must resolve it too via the shared group.
        await db._conn.execute(
            "INSERT INTO war_summary (war_id, clan_tag, opponent_tag, is_cwl, cwl_season, date, war_tag) "
            "VALUES ('w1', '#CLAN1', '#OPP', 1, '2026-07', '2026-07-01T08:00', 'w1')"
        )
        await db._conn.execute(
            "INSERT INTO war_attacks (war_id, clan_tag, date, player_name, player_tag, th_level, map_position, stars) "
            "VALUES ('w1', '#CLAN1', '2026-07-01T08:00', '#P1', '#P1', 15, 1, 2)"
        )
        await db._conn.execute(
            "INSERT INTO war_summary (war_id, clan_tag, opponent_tag, is_cwl, cwl_season, date, war_tag) "
            "VALUES ('w2', '#CLAN2', '#OPP', 1, '2026-07', '2026-07-01T08:00', 'w2')"
        )
        await db._conn.execute(
            "INSERT INTO war_attacks (war_id, clan_tag, date, player_name, player_tag, th_level, map_position, stars) "
            "VALUES ('w2', '#CLAN2', '2026-07-01T08:00', '#P2', '#P2', 15, 1, 2)"
        )
        await db._conn.execute(
            "INSERT INTO cwl_league_rounds (war_tag, cwl_season, cwl_round, league_group_id) VALUES ('w1', '2026-07', 1, 'grp')"
        )
        await db._conn.execute(
            "INSERT INTO cwl_league_rounds (war_tag, cwl_season, cwl_round, league_group_id) VALUES ('w2', '2026-07', 1, 'grp')"
        )
        await db._conn.execute(
            "INSERT INTO cwl_league_groups (league_group_id, cwl_season, clan_tag, league_rank) VALUES ('grp', '2026-07', '#CLAN1', 'Titan League I')"
        )
        await db._conn.execute(
            "INSERT INTO cwl_league_groups (league_group_id, cwl_season, clan_tag, league_rank) VALUES ('grp', '2026-07', '#CLAN2', 'Titan League I')"
        )
        await db._conn.commit()

        result = db.get_recent_cwl_attacks_with_league_sync(["#P1", "#P2"])
        assert result["#P1"][0]["league_rank"] == "Titan League I"
        assert result["#P2"][0]["league_rank"] == "Titan League I"

    @pytest.mark.integration
    async def test_since_date_excludes_older_attacks(self, db):
        """2026-08-16 (replaces the old attack_limit count-based cap with a calendar-date
        filter, for consistency with the hover pop-up's own trailing-3-month window — see
        compute_league_adjusted_skill_scores' own docstring)."""
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        for i in range(3):
            await _seed_cwl_attack_with_league(
                db, "#CLAN1", f"2026-{i + 1:02d}", "Bronze League III", "#P1", stars=2,
                date=f"2026-{i + 1:02d}-01T08:00", war_id=f"w{i}",
            )
        attacks = db.get_recent_cwl_attacks_with_league_sync(["#P1"], since_date="2026-02-01")["#P1"]
        assert len(attacks) == 2  # February and March; January excluded
        assert all(a["date"] >= "2026-02-01" for a in attacks)

    @pytest.mark.integration
    async def test_since_date_none_returns_every_attack(self, db):
        await _seed_guild_and_clan(db, clan_tag="#CLAN1")
        for i in range(3):
            await _seed_cwl_attack_with_league(
                db, "#CLAN1", f"2026-{i + 1:02d}", "Bronze League III", "#P1", stars=2,
                date=f"2026-{i + 1:02d}-01T08:00", war_id=f"w{i}",
            )
        attacks = db.get_recent_cwl_attacks_with_league_sync(["#P1"])["#P1"]
        assert len(attacks) == 3


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


class TestGetAllPlayersForDiscordIds:
    """get_all_players_for_discord_ids_sync — the account-wide expansion source for
    guild_config.cwl_enrollment_include_all_linked_accounts (2026-08-15)."""

    @pytest.mark.integration
    async def test_returns_every_linked_player_regardless_of_clan(self, db):
        await _seed_clan(db, "#MARINES")
        await _seed_clan(db, "#QCREW")
        await _seed_user_player(db, "d1", "#P1", current_clan_tag="#MARINES")
        await _seed_user_player(db, "d1", "#P2", current_clan_tag="#QCREW")  # out-of-family clan
        await _seed_user_player(db, "d2", "#P3", current_clan_tag="#MARINES")  # different account

        players = db.get_all_players_for_discord_ids_sync(["d1"])

        assert {p["player_tag"] for p in players} == {"#P1", "#P2"}
        assert {p["clan_tag"] for p in players} == {"#MARINES", "#QCREW"}

    @pytest.mark.integration
    async def test_works_for_a_clan_this_guild_has_never_added_to_its_family(self, db):
        """The whole point of this function — a clan the bot knows about (tracked for some
        other guild, or just a subscription) but this guild never added to its own
        member_clans/member_families still comes back fine."""
        await _seed_clan(db, "#OUT_OF_FAMILY_CLAN")
        await _seed_user_player(db, "d1", "#P1", current_clan_tag="#OUT_OF_FAMILY_CLAN")

        players = db.get_all_players_for_discord_ids_sync(["d1"])

        assert players == [{
            "player_tag": "#P1", "player_name": "Player", "clan_tag": "#OUT_OF_FAMILY_CLAN",
            "discord_id": "d1", "verified": True, "cwl_permanent_optout": False,
            "preferred_league_rank": None, "th_level": None,
            "cwl_permanent_optin": False, "cwl_optout_send_dm_anyway": False,
        }]

    @pytest.mark.integration
    async def test_empty_discord_ids_returns_empty(self, db):
        assert db.get_all_players_for_discord_ids_sync([]) == []

    @pytest.mark.integration
    async def test_verified_wins_on_disputed_player_tag(self, db):
        await _seed_clan(db, "#A")
        await _seed_clan(db, "#B")
        await _seed_user_player(db, "d1", "#P1", current_clan_tag="#A", verified=False)
        await _seed_user_player(db, "d2", "#P1", current_clan_tag="#B", verified=True)

        players = db.get_all_players_for_discord_ids_sync(["d1", "d2"])

        assert len(players) == 1
        assert players[0]["discord_id"] == "d2"
        assert players[0]["verified"] is True


class TestGuildConfigIncludeAllLinkedAccounts:
    """guild_config.cwl_enrollment_include_all_linked_accounts save/get roundtrip — the
    persistent per-guild toggle for the account-wide expansion above (2026-08-15)."""

    @pytest.mark.integration
    async def test_defaults_to_false_for_a_guild_that_never_set_it(self, db):
        await db._conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES ('111')")
        await db._conn.commit()
        config = await db.get_guild_config("111")
        assert config["cwl_enrollment_include_all_linked_accounts"] is False

    @pytest.mark.integration
    async def test_save_and_get_roundtrip_true(self, db):
        await db.save_guild_config("111", {"cwl_enrollment_include_all_linked_accounts": True})
        config = await db.get_guild_config("111")
        assert config["cwl_enrollment_include_all_linked_accounts"] is True

    @pytest.mark.integration
    async def test_save_and_get_roundtrip_false_after_true(self, db):
        await db.save_guild_config("111", {"cwl_enrollment_include_all_linked_accounts": True})
        await db.save_guild_config("111", {"cwl_enrollment_include_all_linked_accounts": False})
        config = await db.get_guild_config("111")
        assert config["cwl_enrollment_include_all_linked_accounts"] is False


class TestGetPlayerLinks:
    """get_player_links_sync — the CWL Guests search's "is this hit DMable" lookup (2026-08-15).
    Unlike get_current_clan_members_sync/get_all_players_for_discord_ids_sync, this looks up by
    player_tag with no clan or discord_id context at all."""

    @pytest.mark.integration
    async def test_returns_link_info_for_known_tags_only(self, db):
        await _seed_clan(db, "#A")
        await _seed_user_player(db, "d1", "#P1", current_clan_tag="#A")

        links = db.get_player_links_sync(["#P1", "#NEVER_LINKED"])

        assert list(links.keys()) == ["#P1"]
        assert links["#P1"] == {
            "player_name": "Player", "discord_id": "d1", "verified": True,
            "cwl_permanent_optout": False,
            "cwl_permanent_optin": False, "cwl_optout_send_dm_anyway": False,
            "preferred_league_rank": None,
        }

    @pytest.mark.integration
    async def test_empty_tags_returns_empty(self, db):
        assert db.get_player_links_sync([]) == {}

    @pytest.mark.integration
    async def test_verified_wins_on_disputed_player_tag(self, db):
        await _seed_clan(db, "#A")
        await _seed_clan(db, "#B")
        await _seed_user_player(db, "d1", "#P1", current_clan_tag="#A", verified=False)
        await _seed_user_player(db, "d2", "#P1", current_clan_tag="#B", verified=True)

        links = db.get_player_links_sync(["#P1"])

        assert links["#P1"]["discord_id"] == "d2"
        assert links["#P1"]["verified"] is True


class TestGetCurrentClanTagsForPlayers:
    """get_current_clan_tags_for_players_sync — the player-scoped (not clan-scoped) fallback
    _build_enrollment_payload uses so a guest/account-wide-expanded player's real current clan
    still resolves even when that clan is outside every filter set the board otherwise uses
    (2026-08-15 bugfix — these players' cards were stuck plain/default forever, never green or
    amber)."""

    @pytest.mark.integration
    async def test_resolves_a_clan_outside_any_filter_set(self, db):
        await _seed_clan(db, "#OUT_OF_FAMILY_CLAN")
        await _seed_user_player(db, "d1", "#P1", current_clan_tag="#OUT_OF_FAMILY_CLAN")

        clan_tags = db.get_current_clan_tags_for_players_sync(["#P1"])

        assert clan_tags == {"#P1": "#OUT_OF_FAMILY_CLAN"}

    @pytest.mark.integration
    async def test_unknown_player_tag_absent_not_none(self, db):
        assert db.get_current_clan_tags_for_players_sync(["#NEVER"]) == {}

    @pytest.mark.integration
    async def test_player_with_no_current_clan_absent(self, db):
        await _seed_user_player(db, "d1", "#P1", current_clan_tag=None)
        assert db.get_current_clan_tags_for_players_sync(["#P1"]) == {}

    @pytest.mark.integration
    async def test_empty_tags_returns_empty(self, db):
        assert db.get_current_clan_tags_for_players_sync([]) == {}

    @pytest.mark.integration
    async def test_verified_wins_on_disputed_player_tag(self, db):
        await _seed_clan(db, "#A")
        await _seed_clan(db, "#B")
        await _seed_user_player(db, "d1", "#P1", current_clan_tag="#A", verified=False)
        await _seed_user_player(db, "d2", "#P1", current_clan_tag="#B", verified=True)

        clan_tags = db.get_current_clan_tags_for_players_sync(["#P1"])

        assert clan_tags == {"#P1": "#B"}


class TestChunkedInQuery:
    """_chunked_in_query_sync (2026-08-17, CWL_PROD_PERFORMANCE_FIX_PLAN.md P0 Step 4) — splits
    any IN (...) query's values into <=900-sized chunks so a large input (e.g. the CWL guest
    search's player_hits, previously unbounded — see web_bridge.py's GUEST_SEARCH_CAP) can never
    again produce SQLite's "too many SQL variables" error, one of the two confirmed causes of the
    2026-08-16 PROD meltdowns. get_player_links_sync, get_current_clan_tags_for_players_sync, and
    get_current_clan_members_sync all route their IN (...) query through it."""

    @pytest.mark.integration
    async def test_get_player_links_sync_handles_2000_tags_no_error(self, db):
        """2,000 tags (well past the ~900 chunk boundary) — mix of linked/unlinked — must not
        raise and must return exactly the linked subset, correctly merged across chunks."""
        tags = [f"#P{i:05d}" for i in range(2000)]
        linked_indices = list(range(0, 2000, 3))
        await db._conn.executemany(
            "INSERT OR IGNORE INTO clans (clan_tag, name) VALUES (?, ?)",
            [(f"#C{i:05d}", "Clan") for i in linked_indices],
        )
        await db._conn.executemany(
            "INSERT OR IGNORE INTO users (discord_id, display_name) VALUES (?, ?)",
            [(f"d{i}", f"d{i}") for i in linked_indices],
        )
        await db._conn.executemany(
            """
            INSERT INTO user_players
                (discord_id, player_tag, player_name, verified, current_clan_tag)
            VALUES (?, ?, ?, 1, ?)
            """,
            [(f"d{i}", tags[i], f"Player{i}", f"#C{i:05d}") for i in linked_indices],
        )
        await db._conn.commit()

        links = db.get_player_links_sync(tags)

        assert len(links) == len(linked_indices)
        assert links[tags[0]] == {
        "player_name": "Player0", "discord_id": "d0", "verified": True,
        "cwl_permanent_optout": False,
        "cwl_permanent_optin": False, "cwl_optout_send_dm_anyway": False,
        "preferred_league_rank": None,
    }
        assert tags[1] not in links

    @pytest.mark.integration
    async def test_get_current_clan_members_sync_verified_wins_across_chunk_boundary(self, db):
        """Forces a real chunk split: 901 distinct clan_tags means the query chunks on clan_tag
        (900 + 1), while the dedup key is player_tag — so a disputed player_tag whose two linked
        rows carry DIFFERENT current_clan_tag values can genuinely have one row land in chunk 1
        and the other in chunk 2. The merged result must still resolve to the verified row
        regardless of which chunk it came from (do NOT dedup per chunk)."""
        chunk_a_tag = "#CHUNKA"
        chunk_b_tag = "#CHUNKB"
        filler_tags = [f"#F{i:04d}" for i in range(899)]
        clan_tags = [chunk_a_tag] + filler_tags + [chunk_b_tag]  # 901 total: chunk_b lands in chunk 2
        assert len(clan_tags) == 901

        await db._conn.executemany(
            "INSERT OR IGNORE INTO clans (clan_tag, name) VALUES (?, ?)",
            [(t, "Clan") for t in clan_tags],
        )
        await db._conn.commit()
        # Disputed player_tag: an unverified link claims it's in the FIRST-chunk clan, a verified
        # link claims it's in the SECOND-chunk clan.
        await _seed_user_player(db, "d1", "#P1", current_clan_tag=chunk_a_tag, verified=False)
        await _seed_user_player(db, "d2", "#P1", current_clan_tag=chunk_b_tag, verified=True)

        members = db.get_current_clan_members_sync(clan_tags)

        members_by_tag = {m["player_tag"]: m for m in members}
        assert list(members_by_tag.keys()) == ["#P1"]
        assert members_by_tag["#P1"]["discord_id"] == "d2"
        assert members_by_tag["#P1"]["verified"] is True
        assert members_by_tag["#P1"]["clan_tag"] == chunk_b_tag

    @pytest.mark.integration
    async def test_empty_values_short_circuits_before_chunking(self, db):
        """All three callers already early-return [] / {} for empty input before ever reaching
        _chunked_in_query_sync — regression guard for that short-circuit."""
        assert db.get_player_links_sync([]) == {}
        assert db.get_current_clan_tags_for_players_sync([]) == {}
        assert db.get_current_clan_members_sync([]) == []


class TestCwlPlayerSeasonStatus:
    """cwl_player_season_status (2026-08-18, CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md Phase 2,
    rule h) — the global, cross-guild per-(player_tag, cwl_season) DM/response record. Schema-only
    phase: these methods aren't wired into any caller yet, so these tests exercise the CRUD
    surface directly rather than through start_cwl_enrollment()."""

    async def test_dm_sent_round_trip_and_bulk_lookup(self, db):
        # dm_sent_via_event_id must point to a REAL cwl_events row for the bulk dedup lookup to
        # trust it (2026-08-19 follow-up fix — see get_cwl_player_season_dm_status_bulk_sync's own
        # docstring: a dm_sent=1 row whose event no longer exists self-heals to "never DMed").
        await _seed_guild_and_clan(db, guild_id="100", clan_tag="#CLAN1")
        event_id = db.create_cwl_event_sync("100", "2026-08", "discordid1")

        assert db.get_cwl_player_season_status_sync("#P1", "2026-08") is None
        assert db.get_cwl_player_season_dm_status_bulk_sync(["#P1", "#P2"], "2026-08") == {}

        ok = db.mark_cwl_player_dm_sent_sync("#P1", "2026-08", "Player One", "d1", event_id, 100, "2026-08-18T09:00Z")
        assert ok is True

        row = db.get_cwl_player_season_status_sync("#P1", "2026-08")
        assert row is not None
        assert row["dm_sent"] == 1
        assert row["dm_sent_at"] == "2026-08-18T09:00Z"
        assert row["dm_sent_via_event_id"] == event_id
        assert row["dm_sent_via_guild_id"] == "100"
        assert row["status"] == "pending"  # untouched default — dm_sent and status are independent
        assert row["responded_at"] is None

        # #P2 was never marked — bulk lookup omits it entirely, caller uses .get(tag, False).
        bulk = db.get_cwl_player_season_dm_status_bulk_sync(["#P1", "#P2"], "2026-08")
        assert bulk == {"#P1": True}
        assert bulk.get("#P2", False) is False

        # A different season for the same player is a completely separate record.
        assert db.get_cwl_player_season_status_sync("#P1", "2026-09") is None

    async def test_dm_sent_bulk_lookup_self_heals_when_event_no_longer_exists(self, db):
        """2026-08-19 follow-up fix, live bug report: delete_cwl_event_sync() only cleans up
        cwl_player_season_status rows at the MOMENT of a delete going forward — this covers rows
        already left orphaned by an OLDER delete (from before that fix shipped, or any other way
        the referenced event stopped existing) without needing a one-time backfill migration."""
        await _seed_guild_and_clan(db, guild_id="101", clan_tag="#CLAN1")
        event_id = db.create_cwl_event_sync("101", "2026-08", "discordid1")
        db.mark_cwl_player_dm_sent_sync("#P1", "2026-08", "Player One", "d1", event_id, 101, "2026-08-18T09:00Z")
        assert db.get_cwl_player_season_dm_status_bulk_sync(["#P1"], "2026-08") == {"#P1": True}

        # Simulates the pre-fix delete_cwl_event_sync(): removes the event WITHOUT touching
        # cwl_player_season_status at all, leaving dm_sent=1 pointing at a now-nonexistent event.
        await db._conn.execute("DELETE FROM cwl_events WHERE id = ?", (event_id,))
        await db._conn.commit()

        assert db.get_cwl_player_season_dm_status_bulk_sync(["#P1"], "2026-08") == {}

    async def test_re_marking_dm_sent_updates_in_place_without_touching_status(self, db):
        db.mark_cwl_player_dm_sent_sync("#P1", "2026-08", "Player One", "d1", 1, 100, "2026-08-18T09:00Z")
        db.set_cwl_player_response_status_sync("#P1", "2026-08", "Player One", "d1", "confirmed", "2026-08-18T09:05Z", 1, 100)

        # A second guild also "sends" (re-marks) the same player+season — dm_sent_via_* updates
        # to the new source, but the earlier confirmed response must survive untouched.
        db.mark_cwl_player_dm_sent_sync("#P1", "2026-08", "Player One", "d1", 2, 200, "2026-08-18T10:00Z")

        row = db.get_cwl_player_season_status_sync("#P1", "2026-08")
        assert row["dm_sent_via_event_id"] == 2
        assert row["dm_sent_via_guild_id"] == "200"
        assert row["status"] == "confirmed"  # untouched by the DM re-mark
        assert row["responded_at"] == "2026-08-18T09:05Z"

    async def test_response_status_round_trip_and_bulk_lookup(self, db):
        ok = db.set_cwl_player_response_status_sync(
            "#P1", "2026-08", "Player One", "d1", "confirmed", "2026-08-18T09:05Z", 1, 100,
        )
        assert ok is True

        row = db.get_cwl_player_season_status_sync("#P1", "2026-08")
        assert row["status"] == "confirmed"
        assert row["responded_via_event_id"] == 1
        assert row["responded_via_guild_id"] == "100"
        assert row["dm_sent"] == 0  # untouched — a response can arrive without a tracked DM send

        bulk = db.get_cwl_player_season_status_bulk_sync(["#P1", "#P2"], "2026-08")
        assert set(bulk.keys()) == {"#P1"}
        assert bulk["#P1"]["status"] == "confirmed"

    async def test_fan_out_target_lookups_are_season_scoped(self, db):
        await db._conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES ('501')")
        await db._conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES ('502')")
        await db._conn.commit()

        event_a = db.create_cwl_event_sync("501", "2026-08", "creator")
        event_b = db.create_cwl_event_sync("502", "2026-08", "creator")
        event_other_season = db.create_cwl_event_sync("501", "2026-09", "creator")

        db.upsert_cwl_signup_sync(event_a, "#P1", "Player One", "d1", None, "template_confirm", "pending")
        db.upsert_cwl_signup_sync(event_b, "#P1", "Player One", "d1", None, "guest_invite", "pending")
        db.upsert_cwl_signup_sync(event_other_season, "#P1", "Player One", "d1", None, "template_confirm", "pending")
        db.upsert_cwl_signup_sync(event_a, "#P2", "Player Two", "d2", None, "template_confirm", "pending")

        events = sorted(db.find_cwl_signup_events_for_player_and_season_sync("#P1", "2026-08"))
        assert events == sorted([event_a, event_b])  # both guilds' 2026-08 events, not the 2026-09 one

        shared_id = db.create_cwl_shared_clan_sync("#SHAREDCLAN", "2026-08", "501", event_a, "unresolved_first_claimer")
        db.set_cwl_shared_clan_player_status_sync(shared_id, "#P1", "Player One", "d1", "pending", "admin_added", "501", None)

        shared_ids = db.find_cwl_shared_clan_ids_for_player_and_season_sync("#P1", "2026-08")
        assert shared_ids == [shared_id]
        assert db.find_cwl_shared_clan_ids_for_player_and_season_sync("#P2", "2026-08") == []

    async def test_update_cwl_signup_status_only_fills_a_gap_never_creates(self, db):
        await db._conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES ('503')")
        await db._conn.commit()
        event_id = db.create_cwl_event_sync("503", "2026-08", "creator")
        db.upsert_cwl_signup_sync(event_id, "#P1", "Player One", "d1", None, "template_confirm", "pending")

        ok = db.update_cwl_signup_status_sync(event_id, "#P1", "confirmed", "2026-08-18T09:05Z")
        assert ok is True
        assert db.get_cwl_signup_sync(event_id, "#P1")["status"] == "confirmed"

        # #P2 was never seeded in this event — must NOT create a row.
        db.update_cwl_signup_status_sync(event_id, "#P2", "confirmed", "2026-08-18T09:05Z")
        assert db.get_cwl_signup_sync(event_id, "#P2") is None

    async def test_delete_cwl_event_clan_sync_removes_only_the_targeted_row(self, db):
        await _seed_guild_and_clan(db, "504", clan_tag="#CLAN1")
        await _seed_clan(db, "#GUESTCLAN", "Guest Clan")
        event_id = db.create_cwl_event_sync("504", "2026-08", "creator")
        db.set_cwl_event_clans_sync(event_id, [
            {"clan_tag": "#CLAN1", "participating": True},
            {"clan_tag": "#GUESTCLAN", "participating": True},
        ])

        ok = db.delete_cwl_event_clan_sync(event_id, "#GUESTCLAN")
        assert ok is True

        remaining = {c["clan_tag"] for c in db.get_cwl_event_clans_sync(event_id)}
        assert remaining == {"#CLAN1"}  # #GUESTCLAN's row is gone entirely, #CLAN1 untouched


class TestUserPlayersCwlPreferenceSurvival:
    """Phase 0 (plans/cwl-personal-hub.md) — get_user()'s player dict and
    _replace_user_players_rows()'s INSERT both used to omit cwl_permanent_optout/
    cwl_default_preferred_league_rank, so ANY save_user() round-trip (what
    set_primary_account()/unlink_player()/a fresh /link ultimately call via
    CACHE.persist_user()) did a full user_players delete+reinsert that silently reset both
    columns back to their defaults for every linked account of that discord_id — not just the
    one account the caller actually meant to change."""

    async def test_preference_survives_a_save_user_roundtrip_for_a_different_account(self, db):
        discord_id = "601"
        await db._conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES (?, ?)", (discord_id, "Tester"))
        await _seed_user_player(
            db, discord_id, "#PREF1", player_name="Preferred One",
            cwl_permanent_optout=True, cwl_default_preferred_league_rank="Master League II",
        )
        await _seed_user_player(db, discord_id, "#PREF2", player_name="Preferred Two")

        user_data = await db.get_user(discord_id)
        assert user_data is not None
        by_tag = {p["player_tag"]: p for p in user_data["players"]}
        assert by_tag["#PREF1"]["cwl_permanent_optout"] is True
        assert by_tag["#PREF1"]["cwl_default_preferred_league_rank"] == "Master League II"
        assert by_tag["#PREF2"]["cwl_permanent_optout"] is False

        # Simulate set_primary_account(discord_id, "#PREF2") — swap which player is_primary,
        # then round-trip the WHOLE players list back through save_user(), exactly as
        # CACHE.persist_user() does. This is the exact write path that previously wiped every
        # CWL preference: _replace_user_players_rows() deletes every user_players row for this
        # discord_id and reinserts from this list.
        for p in user_data["players"]:
            p["is_primary"] = (p["player_tag"] == "#PREF2")
        await db.save_user(discord_id, user_data)

        reread = await db.get_user(discord_id)
        by_tag_after = {p["player_tag"]: p for p in reread["players"]}
        assert by_tag_after["#PREF1"]["is_primary"] is False
        assert by_tag_after["#PREF2"]["is_primary"] is True
        # The actual regression: #PREF1's preferences must have survived a save_user() call
        # that only intentionally changed #PREF2's is_primary flag.
        assert by_tag_after["#PREF1"]["cwl_permanent_optout"] is True
        assert by_tag_after["#PREF1"]["cwl_default_preferred_league_rank"] == "Master League II"

    async def test_unlink_player_roundtrip_preserves_remaining_accounts_preferences(self, db):
        """Simulates unlink_player() removing one account: the caller drops it from the
        players list before calling save_user(), which still triggers the same
        delete-everything+reinsert-the-rest path for the accounts that remain."""
        discord_id = "602"
        await db._conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES (?, ?)", (discord_id, "Tester"))
        await _seed_user_player(
            db, discord_id, "#KEEP", player_name="Keep Me",
            cwl_permanent_optout=True, cwl_default_preferred_league_rank="Crystal League I",
        )
        await _seed_user_player(db, discord_id, "#DROP", player_name="Drop Me")

        user_data = await db.get_user(discord_id)
        user_data["players"] = [p for p in user_data["players"] if p["player_tag"] != "#DROP"]
        await db.save_user(discord_id, user_data)

        reread = await db.get_user(discord_id)
        assert {p["player_tag"] for p in reread["players"]} == {"#KEEP"}
        kept = reread["players"][0]
        assert kept["cwl_permanent_optout"] is True
        assert kept["cwl_default_preferred_league_rank"] == "Crystal League I"


async def _read_prefs(db: "WarHistoryDB", discord_id: str, player_tag: str) -> dict:
    """Raw read of the four preference columns for one row, bypassing the higher-level read
    methods under test elsewhere in this file — used so set_cwl_preferences_sync's own tests
    don't depend on get_player_links_sync also being correct."""
    cursor = await db._conn.execute(
        "SELECT cwl_permanent_optout, cwl_permanent_optin, cwl_optout_send_dm_anyway, "
        "cwl_default_preferred_league_rank FROM user_players WHERE discord_id = ? AND player_tag = ?",
        (discord_id, player_tag),
    )
    row = await cursor.fetchone()
    return {
        "cwl_permanent_optout": bool(row["cwl_permanent_optout"]),
        "cwl_permanent_optin": bool(row["cwl_permanent_optin"]),
        "cwl_optout_send_dm_anyway": bool(row["cwl_optout_send_dm_anyway"]),
        "cwl_default_preferred_league_rank": row["cwl_default_preferred_league_rank"],
    }


class TestSetCwlPreferencesSync:
    """Phase 1c (plans/cwl-personal-hub.md) — set_cwl_preferences_sync()'s write path."""

    async def test_mode_optout_sets_both_booleans(self, db):
        await _seed_user_player(db, "701", "#A1")
        rowcount = db.set_cwl_preferences_sync("701", "#A1", mode="optout")
        assert rowcount == 1
        prefs = await _read_prefs(db, "701", "#A1")
        assert prefs["cwl_permanent_optout"] is True
        assert prefs["cwl_permanent_optin"] is False

    async def test_mode_optin_sets_both_booleans(self, db):
        await _seed_user_player(db, "701", "#A1")
        db.set_cwl_preferences_sync("701", "#A1", mode="optin")
        prefs = await _read_prefs(db, "701", "#A1")
        assert prefs["cwl_permanent_optout"] is False
        assert prefs["cwl_permanent_optin"] is True

    async def test_mode_none_clears_both_booleans(self, db):
        await _seed_user_player(db, "701", "#A1", cwl_permanent_optout=True)
        db.set_cwl_preferences_sync("701", "#A1", mode="none")
        prefs = await _read_prefs(db, "701", "#A1")
        assert prefs["cwl_permanent_optout"] is False
        assert prefs["cwl_permanent_optin"] is False

    async def test_switching_away_from_optout_clears_send_dm_anyway(self, db):
        await _seed_user_player(db, "701", "#A1")
        db.set_cwl_preferences_sync("701", "#A1", mode="optout", send_dm_anyway=True)
        assert (await _read_prefs(db, "701", "#A1"))["cwl_optout_send_dm_anyway"] is True

        db.set_cwl_preferences_sync("701", "#A1", mode="optin")
        prefs = await _read_prefs(db, "701", "#A1")
        assert prefs["cwl_permanent_optin"] is True
        assert prefs["cwl_optout_send_dm_anyway"] is False  # force-cleared, not left stale

    async def test_send_dm_anyway_standalone_toggle_without_mode_change(self, db):
        await _seed_user_player(db, "701", "#A1", cwl_permanent_optout=True)
        rowcount = db.set_cwl_preferences_sync("701", "#A1", send_dm_anyway=True)
        assert rowcount == 1
        prefs = await _read_prefs(db, "701", "#A1")
        assert prefs["cwl_permanent_optout"] is True  # untouched — mode was None
        assert prefs["cwl_optout_send_dm_anyway"] is True

    async def test_rank_provided_true_with_none_clears_to_null(self, db):
        await _seed_user_player(db, "701", "#A1", cwl_default_preferred_league_rank="Gold League I")
        db.set_cwl_preferences_sync("701", "#A1", league_rank=None, rank_provided=True)
        assert (await _read_prefs(db, "701", "#A1"))["cwl_default_preferred_league_rank"] is None

    async def test_rank_provided_false_leaves_existing_rank_untouched(self, db):
        await _seed_user_player(db, "701", "#A1", cwl_default_preferred_league_rank="Gold League I")
        db.set_cwl_preferences_sync("701", "#A1", mode="optout")  # rank_provided defaults False
        assert (await _read_prefs(db, "701", "#A1"))["cwl_default_preferred_league_rank"] == "Gold League I"

    async def test_rank_provided_true_sets_a_new_value(self, db):
        await _seed_user_player(db, "701", "#A1")
        db.set_cwl_preferences_sync("701", "#A1", league_rank="Champion League III", rank_provided=True)
        assert (await _read_prefs(db, "701", "#A1"))["cwl_default_preferred_league_rank"] == "Champion League III"

    async def test_player_tag_none_applies_to_every_linked_account_of_that_discord_id_only(self, db):
        await _seed_user_player(db, "701", "#A1")
        await _seed_user_player(db, "701", "#A2")
        await _seed_user_player(db, "702", "#B1")  # different discord_id — must NOT be touched

        rowcount = db.set_cwl_preferences_sync("701", None, mode="optout")
        assert rowcount == 2
        assert (await _read_prefs(db, "701", "#A1"))["cwl_permanent_optout"] is True
        assert (await _read_prefs(db, "701", "#A2"))["cwl_permanent_optout"] is True
        assert (await _read_prefs(db, "702", "#B1"))["cwl_permanent_optout"] is False

    async def test_nonexistent_pair_returns_zero_without_raising(self, db):
        rowcount = db.set_cwl_preferences_sync("999999", "#NOPE", mode="optout")
        assert rowcount == 0

    async def test_no_arguments_is_a_pure_noop_returning_zero(self, db):
        await _seed_user_player(db, "701", "#A1", cwl_permanent_optout=True)
        rowcount = db.set_cwl_preferences_sync("701", "#A1")
        assert rowcount == 0
        # Confirm it genuinely touched nothing.
        assert (await _read_prefs(db, "701", "#A1"))["cwl_permanent_optout"] is True

    async def test_invalid_mode_raises_value_error(self, db):
        await _seed_user_player(db, "701", "#A1")
        with pytest.raises(ValueError):
            db.set_cwl_preferences_sync("701", "#A1", mode="bogus")

    async def test_write_immediately_visible_to_get_player_links_sync(self, db):
        await _seed_user_player(db, "701", "#A1")
        db.set_cwl_preferences_sync("701", "#A1", mode="optin", league_rank="Silver League II", rank_provided=True)
        links = db.get_player_links_sync(["#A1"])
        assert links["#A1"]["cwl_permanent_optin"] is True
        assert links["#A1"]["cwl_permanent_optout"] is False

    async def test_write_immediately_visible_to_get_current_clan_members_sync(self, db):
        await _seed_clan(db, "#CLANX")
        await _seed_user_player(db, "701", "#A1", current_clan_tag="#CLANX")
        db.set_cwl_preferences_sync("701", "#A1", mode="optout", send_dm_anyway=True)

        members = db.get_current_clan_members_sync(["#CLANX"])
        assert len(members) == 1
        assert members[0]["cwl_permanent_optout"] is True
        assert members[0]["cwl_optout_send_dm_anyway"] is True
        assert members[0]["cwl_permanent_optin"] is False

    async def test_write_immediately_visible_to_get_all_players_for_discord_ids_sync(self, db):
        await _seed_user_player(db, "701", "#A1")
        db.set_cwl_preferences_sync("701", "#A1", mode="optin")

        players = db.get_all_players_for_discord_ids_sync(["701"])
        assert len(players) == 1
        assert players[0]["cwl_permanent_optin"] is True


# ---------------------------------------------------------------------------
# Retention purge (purge_expired_cwl_events) — the consumer guild_config.
# cwl_retention_months never had until 2026-08-30.
# ---------------------------------------------------------------------------

def test_retention_cutoff_season_arithmetic():
    """Seasons are 'YYYY-MM' strings compared lexically, so the cutoff must be zero-padded and
    must roll the year over correctly when subtracting past January."""
    from datetime import datetime, timezone

    cut = WarHistoryDB._cwl_retention_cutoff_season
    assert cut(12, datetime(2026, 8, 15, tzinfo=timezone.utc)) == "2025-08"
    assert cut(1, datetime(2026, 1, 15, tzinfo=timezone.utc)) == "2025-12"   # year rollover
    assert cut(8, datetime(2026, 8, 15, tzinfo=timezone.utc)) == "2025-12"
    assert cut(0, datetime(2026, 8, 15, tzinfo=timezone.utc)) == "2026-08"


@pytest.mark.asyncio
async def test_purge_removes_only_seasons_past_retention(db):
    await _seed_guild_and_clan(db, "801")
    await db._conn.execute("UPDATE guild_config SET cwl_retention_months = 12 WHERE guild_id = ?", ("801",))
    await db._conn.commit()

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    old_season = f"{now.year - 3}-01"          # comfortably past a 12-month window
    recent_season = f"{now.year}-{now.month:02d}"

    old_id = db.create_cwl_event_sync("801", old_season, "u1")
    recent_id = db.create_cwl_event_sync("801", recent_season, "u1")

    result = await db.purge_expired_cwl_events()

    assert result["events"] == 1
    assert db.get_cwl_event_sync("801", old_season) is None
    assert db.get_cwl_event_sync("801", recent_season) is not None
    assert recent_id != old_id


@pytest.mark.asyncio
async def test_purge_skips_guilds_keeping_data_indefinitely(db):
    """retention_months = 0 is "keep indefinitely" and is the default for every guild — it must
    never purge anything, however old the season is."""
    await _seed_guild_and_clan(db, "802")  # cwl_retention_months defaults to 0

    from datetime import datetime, timezone
    ancient = f"{datetime.now(timezone.utc).year - 5}-01"
    db.create_cwl_event_sync("802", ancient, "u1")

    result = await db.purge_expired_cwl_events()

    assert result["events"] == 0
    assert db.get_cwl_event_sync("802", ancient) is not None


@pytest.mark.asyncio
async def test_purge_cascades_to_child_tables(db):
    await _seed_guild_and_clan(db, "803")
    await db._conn.execute("UPDATE guild_config SET cwl_retention_months = 6 WHERE guild_id = ?", ("803",))
    await db._conn.commit()

    from datetime import datetime, timezone
    ancient = f"{datetime.now(timezone.utc).year - 3}-01"
    event_id = db.create_cwl_event_sync("803", ancient, "u1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1"}])
    db.upsert_cwl_signup_sync(event_id, "#P1", "Alpha", "1", None, "tpl", "pending")

    await db.purge_expired_cwl_events()

    cur = await db._conn.execute("SELECT COUNT(*) FROM cwl_event_clans WHERE event_id = ?", (event_id,))
    assert (await cur.fetchone())[0] == 0
    cur = await db._conn.execute("SELECT COUNT(*) FROM cwl_signups WHERE event_id = ?", (event_id,))
    assert (await cur.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_purge_keeps_cross_guild_rows_another_guild_still_retains(db):
    """cwl_locked_clan_members/cwl_player_season_status are shared across guilds with no FK to
    cwl_events. One guild's retention expiring must NOT destroy a season another guild is still
    keeping — they are swept referentially, only once no event anywhere references the season."""
    await _seed_guild_and_clan(db, "804")
    await db._conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", ("805",))
    # 804 purges aggressively; 805 keeps everything.
    await db._conn.execute("UPDATE guild_config SET cwl_retention_months = 1 WHERE guild_id = ?", ("804",))
    await db._conn.commit()

    from datetime import datetime, timezone
    shared_season = f"{datetime.now(timezone.utc).year - 3}-01"
    db.create_cwl_event_sync("804", shared_season, "u1")
    db.create_cwl_event_sync("805", shared_season, "u1")   # 805 still holds this season

    await db._conn.execute(
        "INSERT INTO cwl_locked_clan_members (cwl_season, clan_tag, player_tag, source) VALUES (?, ?, ?, ?)",
        (shared_season, "#CLAN1", "#P1", "league_group"),
    )
    await db._conn.commit()

    result = await db.purge_expired_cwl_events()

    assert result["events"] == 1               # only 804's event went
    assert result["locked_members"] == 0       # 805 still references the season
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM cwl_locked_clan_members WHERE cwl_season = ?", (shared_season,)
    )
    assert (await cur.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_purge_sweeps_cross_guild_rows_once_no_event_references_the_season(db):
    await _seed_guild_and_clan(db, "806")
    await db._conn.execute("UPDATE guild_config SET cwl_retention_months = 1 WHERE guild_id = ?", ("806",))
    await db._conn.commit()

    from datetime import datetime, timezone
    season = f"{datetime.now(timezone.utc).year - 3}-01"
    db.create_cwl_event_sync("806", season, "u1")
    await db._conn.execute(
        "INSERT INTO cwl_locked_clan_members (cwl_season, clan_tag, player_tag, source) VALUES (?, ?, ?, ?)",
        (season, "#CLAN1", "#P1", "league_group"),
    )
    await db._conn.execute(
        "INSERT INTO cwl_player_season_status (player_tag, cwl_season) VALUES (?, ?)", ("#P1", season)
    )
    await db._conn.commit()

    result = await db.purge_expired_cwl_events()

    assert result["events"] == 1
    assert result["locked_members"] == 1
    assert result["player_season_status"] == 1
