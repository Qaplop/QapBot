"""Tests for the cross-guild shared CWL clans DB layer (CWL_ROSTER_PLANNING_PLAN.md, 2026-08-15):
cwl_shared_clans / cwl_shared_clan_guilds / cwl_shared_clan_players CRUD, and
find_cwl_clan_participation_across_guilds_sync — the "is this clan already claimed elsewhere"
check both trigger points (guest-clan add, Start Enrollment) use.
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


async def _seed_guild_and_clan(db: WarHistoryDB, guild_id: str, clan_tag: str = "#CLAN1") -> None:
    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
    await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES (?, ?)", (clan_tag, "Test Clan"))
    await db.conn.commit()


class TestFindCwlClanParticipationAcrossGuilds:
    @pytest.mark.integration
    async def test_finds_other_guilds_participating_same_clan_and_season(self, db):
        await _seed_guild_and_clan(db, "100", "#CLAN1")
        await _seed_guild_and_clan(db, "200", "#CLAN1")
        event_a = db.create_cwl_event_sync("100", "2026-09", "d1")
        event_b = db.create_cwl_event_sync("200", "2026-09", "d2")
        db.set_cwl_event_clans_sync(event_a, [{"clan_tag": "#CLAN1", "participating": True}])
        db.set_cwl_event_clans_sync(event_b, [{"clan_tag": "#CLAN1", "participating": True}])

        matches = db.find_cwl_clan_participation_across_guilds_sync("#CLAN1", "2026-09", exclude_guild_id="100")

        assert len(matches) == 1
        assert matches[0]["guild_id"] == "200"
        assert matches[0]["event_id"] == event_b

    @pytest.mark.integration
    async def test_excludes_non_participating_rows(self, db):
        await _seed_guild_and_clan(db, "100", "#CLAN1")
        event_a = db.create_cwl_event_sync("100", "2026-09", "d1")
        db.set_cwl_event_clans_sync(event_a, [{"clan_tag": "#CLAN1", "participating": False}])

        matches = db.find_cwl_clan_participation_across_guilds_sync("#CLAN1", "2026-09")

        assert matches == []

    @pytest.mark.integration
    async def test_excludes_different_season(self, db):
        await _seed_guild_and_clan(db, "100", "#CLAN1")
        event_a = db.create_cwl_event_sync("100", "2026-08", "d1")
        db.set_cwl_event_clans_sync(event_a, [{"clan_tag": "#CLAN1", "participating": True}])

        assert db.find_cwl_clan_participation_across_guilds_sync("#CLAN1", "2026-09") == []

    @pytest.mark.integration
    async def test_no_exclude_guild_id_includes_everyone(self, db):
        await _seed_guild_and_clan(db, "100", "#CLAN1")
        event_a = db.create_cwl_event_sync("100", "2026-09", "d1")
        db.set_cwl_event_clans_sync(event_a, [{"clan_tag": "#CLAN1", "participating": True}])

        matches = db.find_cwl_clan_participation_across_guilds_sync("#CLAN1", "2026-09")
        assert [m["guild_id"] for m in matches] == ["100"]


class TestCwlSharedClanCrud:
    @pytest.mark.integration
    async def test_create_and_get_roundtrip(self, db):
        await _seed_guild_and_clan(db, "100", "#CLAN1")
        event_id = db.create_cwl_event_sync("100", "2026-09", "d1")

        shared_clan_id = db.create_cwl_shared_clan_sync("#CLAN1", "2026-09", "100", event_id, "leader_verified")

        assert shared_clan_id is not None
        row = db.get_cwl_shared_clan_sync("#CLAN1", "2026-09")
        assert row["owner_guild_id"] == "100"
        assert row["owner_event_id"] == event_id
        assert row["owner_resolution_method"] == "leader_verified"
        assert row["owner_resolved_at"] is not None

        by_id = db.get_cwl_shared_clan_by_id_sync(shared_clan_id)
        assert by_id["id"] == shared_clan_id

    @pytest.mark.integration
    async def test_get_returns_none_when_not_shared(self, db):
        assert db.get_cwl_shared_clan_sync("#NEVER", "2026-09") is None
        assert db.get_cwl_shared_clan_by_id_sync(999) is None

    @pytest.mark.integration
    async def test_repoint_owner(self, db):
        await _seed_guild_and_clan(db, "100", "#CLAN1")
        await _seed_guild_and_clan(db, "200", "#CLAN1")
        event_a = db.create_cwl_event_sync("100", "2026-09", "d1")
        event_b = db.create_cwl_event_sync("200", "2026-09", "d2")
        shared_clan_id = db.create_cwl_shared_clan_sync("#CLAN1", "2026-09", "100", event_a, "unresolved_first_claimer")

        assert db.repoint_cwl_shared_clan_owner_sync(shared_clan_id, "200", event_b, "leader_verified") is True

        row = db.get_cwl_shared_clan_by_id_sync(shared_clan_id)
        assert row["owner_guild_id"] == "200"
        assert row["owner_event_id"] == event_b
        assert row["owner_resolution_method"] == "leader_verified"

    @pytest.mark.integration
    async def test_delete_shared_clan_cascades(self, db):
        await _seed_guild_and_clan(db, "100", "#CLAN1")
        event_a = db.create_cwl_event_sync("100", "2026-09", "d1")
        shared_clan_id = db.create_cwl_shared_clan_sync("#CLAN1", "2026-09", "100", event_a, "unresolved_first_claimer")
        db.add_guild_to_shared_clan_sync(shared_clan_id, "100", event_a)
        db.set_cwl_shared_clan_player_status_sync(shared_clan_id, "#P1", "Player", "d1", "pending", "guest_invite", "100")

        assert db.delete_cwl_shared_clan_sync(shared_clan_id) is True

        assert db.get_cwl_shared_clan_by_id_sync(shared_clan_id) is None
        assert db.list_cwl_shared_clan_guilds_sync(shared_clan_id) == []
        assert db.get_cwl_shared_clan_players_sync(shared_clan_id) == []


class TestCwlSharedClanGuilds:
    @pytest.mark.integration
    async def test_add_and_list(self, db):
        await _seed_guild_and_clan(db, "100", "#CLAN1")
        await _seed_guild_and_clan(db, "200", "#CLAN1")
        event_a = db.create_cwl_event_sync("100", "2026-09", "d1")
        event_b = db.create_cwl_event_sync("200", "2026-09", "d2")
        shared_clan_id = db.create_cwl_shared_clan_sync("#CLAN1", "2026-09", "100", event_a, "unresolved_first_claimer")

        assert db.add_guild_to_shared_clan_sync(shared_clan_id, "100", event_a) is True
        assert db.add_guild_to_shared_clan_sync(shared_clan_id, "200", event_b) is True

        guilds = db.list_cwl_shared_clan_guilds_sync(shared_clan_id)
        assert {g["guild_id"] for g in guilds} == {"100", "200"}

    @pytest.mark.integration
    async def test_add_is_idempotent(self, db):
        await _seed_guild_and_clan(db, "100", "#CLAN1")
        event_a = db.create_cwl_event_sync("100", "2026-09", "d1")
        shared_clan_id = db.create_cwl_shared_clan_sync("#CLAN1", "2026-09", "100", event_a, "unresolved_first_claimer")

        assert db.add_guild_to_shared_clan_sync(shared_clan_id, "100", event_a) is True
        assert db.add_guild_to_shared_clan_sync(shared_clan_id, "100", event_a) is True  # no error, no duplicate

        assert len(db.list_cwl_shared_clan_guilds_sync(shared_clan_id)) == 1

    @pytest.mark.integration
    async def test_remove_guild(self, db):
        await _seed_guild_and_clan(db, "100", "#CLAN1")
        await _seed_guild_and_clan(db, "200", "#CLAN1")
        event_a = db.create_cwl_event_sync("100", "2026-09", "d1")
        event_b = db.create_cwl_event_sync("200", "2026-09", "d2")
        shared_clan_id = db.create_cwl_shared_clan_sync("#CLAN1", "2026-09", "100", event_a, "unresolved_first_claimer")
        db.add_guild_to_shared_clan_sync(shared_clan_id, "100", event_a)
        db.add_guild_to_shared_clan_sync(shared_clan_id, "200", event_b)

        assert db.remove_guild_from_shared_clan_sync(shared_clan_id, "200") is True

        guilds = db.list_cwl_shared_clan_guilds_sync(shared_clan_id)
        assert [g["guild_id"] for g in guilds] == ["100"]
        # The shared clan record + its player roster survive — removal only touches membership.
        assert db.get_cwl_shared_clan_by_id_sync(shared_clan_id) is not None


class TestCwlSharedClanPlayers:
    """status (confirmation) and assigned (placement) are deliberately separate columns
    (2026-08-16, live-testing feedback, project owner's spec, verbatim: "Confirmation status and
    assignment status should be treated completely separate. the one has a totally different
    meaning logically than the other!") — set_cwl_shared_clan_player_status_sync and
    set_cwl_shared_clan_player_assignment_sync each own exactly one of the two columns and must
    never touch the other, see cwl_shared_clan_players' own CREATE TABLE comment (db_manager.py)."""

    @pytest.mark.integration
    async def test_set_status_and_get(self, db):
        await _seed_guild_and_clan(db, "100", "#CLAN1")
        event_a = db.create_cwl_event_sync("100", "2026-09", "d1")
        shared_clan_id = db.create_cwl_shared_clan_sync("#CLAN1", "2026-09", "100", event_a, "unresolved_first_claimer")

        assert db.set_cwl_shared_clan_player_status_sync(
            shared_clan_id, "#P1", "Alpha", "d1", "pending", "guest_invite", "100"
        ) is True

        players = db.get_cwl_shared_clan_players_sync(shared_clan_id)
        assert len(players) == 1
        assert players[0]["player_tag"] == "#P1"
        assert players[0]["status"] == "pending"
        assert players[0]["added_by_guild_id"] == "100"
        assert players[0]["assigned"] == 0  # a status-only write never places anyone

    @pytest.mark.integration
    async def test_set_assignment_and_get(self, db):
        await _seed_guild_and_clan(db, "100", "#CLAN1")
        event_a = db.create_cwl_event_sync("100", "2026-09", "d1")
        shared_clan_id = db.create_cwl_shared_clan_sync("#CLAN1", "2026-09", "100", event_a, "unresolved_first_claimer")

        assert db.set_cwl_shared_clan_player_assignment_sync(
            shared_clan_id, "#P1", "Alpha", "d1", True, "admin_override", "100"
        ) is True

        players = db.get_cwl_shared_clan_players_sync(shared_clan_id)
        assert len(players) == 1
        assert players[0]["assigned"] == 1
        assert players[0]["status"] == "pending"  # column DEFAULT — never a real confirmation

    @pytest.mark.integration
    async def test_set_status_overwrites_status_but_never_touches_assigned(self, db):
        await _seed_guild_and_clan(db, "100", "#CLAN1")
        event_a = db.create_cwl_event_sync("100", "2026-09", "d1")
        shared_clan_id = db.create_cwl_shared_clan_sync("#CLAN1", "2026-09", "100", event_a, "unresolved_first_claimer")
        db.set_cwl_shared_clan_player_assignment_sync(shared_clan_id, "#P1", "Alpha", "d1", True, "admin_override", "100")

        # A player confirms/declines after already being placed — must not un-place them.
        db.set_cwl_shared_clan_player_status_sync(shared_clan_id, "#P1", "Alpha", "d1", "confirmed", "admin_added", "200")

        players = db.get_cwl_shared_clan_players_sync(shared_clan_id)
        assert len(players) == 1
        assert players[0]["status"] == "confirmed"
        assert players[0]["added_by_guild_id"] == "200"
        assert players[0]["assigned"] == 1  # untouched by the status-only write

    @pytest.mark.integration
    async def test_set_assignment_overwrites_assigned_but_never_touches_status(self, db):
        await _seed_guild_and_clan(db, "100", "#CLAN1")
        event_a = db.create_cwl_event_sync("100", "2026-09", "d1")
        shared_clan_id = db.create_cwl_shared_clan_sync("#CLAN1", "2026-09", "100", event_a, "unresolved_first_claimer")
        db.set_cwl_shared_clan_player_status_sync(shared_clan_id, "#P1", "Alpha", "d1", "confirmed", "template_confirm", "100")

        # An admin drags this already-confirmed player into a different clan's column — their
        # genuine confirmation must survive the move untouched (2026-08-16, project owner's spec:
        # "It must not be altered by any assignment process across clans and guilds!").
        db.set_cwl_shared_clan_player_assignment_sync(shared_clan_id, "#P1", "Alpha", "d1", True, "admin_override", "200")

        players = db.get_cwl_shared_clan_players_sync(shared_clan_id)
        assert len(players) == 1
        assert players[0]["assigned"] == 1
        assert players[0]["added_by_guild_id"] == "200"
        assert players[0]["status"] == "confirmed"  # untouched by the assignment-only write

    @pytest.mark.integration
    async def test_delete_player(self, db):
        await _seed_guild_and_clan(db, "100", "#CLAN1")
        event_a = db.create_cwl_event_sync("100", "2026-09", "d1")
        shared_clan_id = db.create_cwl_shared_clan_sync("#CLAN1", "2026-09", "100", event_a, "unresolved_first_claimer")
        db.set_cwl_shared_clan_player_status_sync(shared_clan_id, "#P1", "Alpha", "d1", "confirmed", "guest_invite", "100")

        assert db.delete_cwl_shared_clan_player_sync(shared_clan_id, "#P1") is True
        assert db.get_cwl_shared_clan_players_sync(shared_clan_id) == []


class TestCwlSharedClanPlayersAssignedMigration:
    """The `assigned` column (2026-08-16, live-testing feedback) was added to an already-shipped
    table — a pre-existing DB needs the one-time backfill (assigned=1 WHERE status='confirmed')
    so every player the OLD combined status='confirmed' meaning had already placed into a column
    doesn't silently vanish from it the moment this ships. See db_manager.py's migration block and
    cwl_shared_clan_players' own CREATE TABLE comment."""

    @pytest.mark.integration
    async def test_pre_existing_confirmed_rows_get_backfilled_as_assigned(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        manager = WarHistoryDB()
        await manager.initialize(str(db_path))
        try:
            await _seed_guild_and_clan(manager, "100", "#CLAN1")
            event_a = manager.create_cwl_event_sync("100", "2026-09", "d1")
            shared_clan_id = manager.create_cwl_shared_clan_sync(
                "#CLAN1", "2026-09", "100", event_a, "unresolved_first_claimer"
            )
            # Simulate a pre-migration DB: drop the new column and write rows the OLD way, where
            # status='confirmed' alone meant "placed," and status='pending' meant "not placed."
            await manager.conn.execute("ALTER TABLE cwl_shared_clan_players DROP COLUMN assigned")
            await manager.conn.execute(
                "INSERT INTO cwl_shared_clan_players "
                "(shared_clan_id, player_tag, player_name, dmed_discord_id, status, source, added_by_guild_id) "
                "VALUES (?, '#PLACED', 'Placed', 'd1', 'confirmed', 'admin_override', '100')",
                (shared_clan_id,),
            )
            await manager.conn.execute(
                "INSERT INTO cwl_shared_clan_players "
                "(shared_clan_id, player_tag, player_name, dmed_discord_id, status, source, added_by_guild_id) "
                "VALUES (?, '#NOT_PLACED', 'NotPlaced', 'd2', 'pending', 'auto_seeded', '100')",
                (shared_clan_id,),
            )
            await manager.conn.commit()
        finally:
            await manager.close()

        # Re-open the same DB file — this re-runs the migration/backfill against real
        # pre-existing data, exactly like a bot restart against an already-shipped database.
        migrated = WarHistoryDB()
        await migrated.initialize(str(db_path))
        try:
            players = {p["player_tag"]: p for p in migrated.get_cwl_shared_clan_players_sync(shared_clan_id)}
            assert players["#PLACED"]["assigned"] == 1
            assert players["#PLACED"]["status"] == "confirmed"  # untouched — still a real fact
            assert players["#NOT_PLACED"]["assigned"] == 0
        finally:
            await migrated.close()
