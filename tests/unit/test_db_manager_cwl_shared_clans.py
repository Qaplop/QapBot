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
        db.upsert_cwl_shared_clan_player_sync(shared_clan_id, "#P1", "Player", "d1", "pending", "guest_invite", "100")

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
    @pytest.mark.integration
    async def test_upsert_and_get(self, db):
        await _seed_guild_and_clan(db, "100", "#CLAN1")
        event_a = db.create_cwl_event_sync("100", "2026-09", "d1")
        shared_clan_id = db.create_cwl_shared_clan_sync("#CLAN1", "2026-09", "100", event_a, "unresolved_first_claimer")

        assert db.upsert_cwl_shared_clan_player_sync(
            shared_clan_id, "#P1", "Alpha", "d1", "pending", "guest_invite", "100"
        ) is True

        players = db.get_cwl_shared_clan_players_sync(shared_clan_id)
        assert len(players) == 1
        assert players[0]["player_tag"] == "#P1"
        assert players[0]["status"] == "pending"
        assert players[0]["added_by_guild_id"] == "100"

    @pytest.mark.integration
    async def test_upsert_overwrites_on_conflict(self, db):
        await _seed_guild_and_clan(db, "100", "#CLAN1")
        event_a = db.create_cwl_event_sync("100", "2026-09", "d1")
        shared_clan_id = db.create_cwl_shared_clan_sync("#CLAN1", "2026-09", "100", event_a, "unresolved_first_claimer")
        db.upsert_cwl_shared_clan_player_sync(shared_clan_id, "#P1", "Alpha", "d1", "pending", "guest_invite", "100")

        # A different guild's admin confirms the same player — latest action wins, audit trail updates.
        db.upsert_cwl_shared_clan_player_sync(shared_clan_id, "#P1", "Alpha", "d1", "confirmed", "admin_added", "200")

        players = db.get_cwl_shared_clan_players_sync(shared_clan_id)
        assert len(players) == 1
        assert players[0]["status"] == "confirmed"
        assert players[0]["added_by_guild_id"] == "200"

    @pytest.mark.integration
    async def test_delete_player(self, db):
        await _seed_guild_and_clan(db, "100", "#CLAN1")
        event_a = db.create_cwl_event_sync("100", "2026-09", "d1")
        shared_clan_id = db.create_cwl_shared_clan_sync("#CLAN1", "2026-09", "100", event_a, "unresolved_first_claimer")
        db.upsert_cwl_shared_clan_player_sync(shared_clan_id, "#P1", "Alpha", "d1", "confirmed", "guest_invite", "100")

        assert db.delete_cwl_shared_clan_player_sync(shared_clan_id, "#P1") is True

        assert db.get_cwl_shared_clan_players_sync(shared_clan_id) == []
