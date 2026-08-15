"""Tests for cross-guild shared CWL clan ownership resolution and the sharing orchestrator
(CWL_ROSTER_PLANNING_PLAN.md, 2026-08-15): resolve_cwl_clan_owner() and
ensure_cwl_clan_sharing() in qapbot/QBdiscocmdshelper_cwl.py.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")

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


async def _seed_user_player(
    db: WarHistoryDB, discord_id: str, player_tag: str, verified: bool = True
) -> None:
    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES (?, ?)", (discord_id, discord_id))
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified) VALUES (?, ?, ?, ?)",
        (discord_id, player_tag, "Player", 1 if verified else 0),
    )
    await db.conn.commit()


def _make_role(name: str) -> MagicMock:
    role = MagicMock()
    role.name = name
    return role


def _make_clan_member(tag: str, role_name: str) -> MagicMock:
    member = MagicMock()
    member.tag = tag
    member.role = _make_role(role_name)
    return member


def _make_bot(guild_members: dict) -> MagicMock:
    """guild_members: {guild_id: {discord_id_str, ...}} — which Discord accounts are actually
    members of which guild, for the guild-presence half of ownership resolution."""
    def _get_guild(guild_id):
        members = guild_members.get(guild_id)
        if members is None:
            return None
        guild = MagicMock()

        def _get_member(discord_id):
            return MagicMock() if str(discord_id) in members else None

        async def _fetch_member(discord_id):
            if str(discord_id) in members:
                return MagicMock()
            import discord
            raise discord.NotFound(MagicMock(status=404), "not found")

        guild.get_member = MagicMock(side_effect=_get_member)
        guild.fetch_member = AsyncMock(side_effect=_fetch_member)
        return guild

    bot = MagicMock()
    bot.get_guild = MagicMock(side_effect=_get_guild)
    return bot


@pytest.mark.discord
@pytest.mark.asyncio
async def test_leader_outranks_coleader_across_guilds(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_cwl_clan_owner
    import QBcore

    await _seed_user_player(db, "111", "#LEADER_TAG", verified=False)  # guild 100's leader, unverified
    await _seed_user_player(db, "222", "#COLEADER_TAG", verified=True)  # guild 200's co-leader, verified
    monkeypatch.setattr(CACHE, "db_manager", db)

    fake_coc_cache = MagicMock()
    fake_clan = MagicMock()
    fake_clan.members = [
        _make_clan_member("#LEADER_TAG", "leader"),
        _make_clan_member("#COLEADER_TAG", "co_leader"),
    ]
    fake_coc_cache.get_clan = AsyncMock(return_value=fake_clan)
    monkeypatch.setattr(CACHE, "coc_clan_cache", fake_coc_cache)

    monkeypatch.setattr(QBcore, "bot", _make_bot({100: {"111"}, 200: {"222"}}))

    owner_guild_id, method, owner_event_id = await resolve_cwl_clan_owner("#CLAN1", "2026-09", [100, 200])

    # Leader (unverified) still beats Co-Leader (verified) — rank is primary, verified only
    # breaks ties within the same rank tier.
    assert owner_guild_id == "100"
    assert method == "leader_unverified"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_verified_breaks_tie_within_same_rank(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_cwl_clan_owner
    import QBcore

    await _seed_user_player(db, "111", "#COLEADER_A", verified=False)
    await _seed_user_player(db, "222", "#COLEADER_B", verified=True)
    monkeypatch.setattr(CACHE, "db_manager", db)

    fake_coc_cache = MagicMock()
    fake_clan = MagicMock()
    fake_clan.members = [
        _make_clan_member("#COLEADER_A", "co_leader"),
        _make_clan_member("#COLEADER_B", "co_leader"),
    ]
    fake_coc_cache.get_clan = AsyncMock(return_value=fake_clan)
    monkeypatch.setattr(CACHE, "coc_clan_cache", fake_coc_cache)

    monkeypatch.setattr(QBcore, "bot", _make_bot({100: {"111"}, 200: {"222"}}))

    owner_guild_id, method, _ = await resolve_cwl_clan_owner("#CLAN1", "2026-09", [100, 200])

    assert owner_guild_id == "200"
    assert method == "coleader_verified"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_native_family_guild_wins_tie_over_acting_guild(db, monkeypatch):
    """Regression test for a real live-testing bug (2026-08-15): the clan's real Leader is a
    Discord member of BOTH the acting guild (100 — just guest-invited the clan, doesn't track it
    natively) and the clan's actual home guild (200 — has it in member_clans) — very plausible in
    practice (a leader/admin's own account is often in more than one server: their real clan's
    server, plus a personal/test/alliance server). Before this fix, the acting guild always won
    such a tie purely because affected_guild_ids lists it first; the clan's real native-family
    guild must win instead."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_cwl_clan_owner
    import QBcore

    await _seed_user_player(db, "111", "#LEADER_TAG", verified=False)
    monkeypatch.setattr(CACHE, "db_manager", db)
    CACHE.server_config = {
        "100": {"member_clans": [], "member_families": []},  # acting guild — guest, not native
        "200": {"member_clans": ["#CLAN1"], "member_families": []},  # clan's real home
    }
    CACHE.clan_families = {}

    fake_coc_cache = MagicMock()
    fake_clan = MagicMock()
    fake_clan.members = [_make_clan_member("#LEADER_TAG", "leader")]
    fake_coc_cache.get_clan = AsyncMock(return_value=fake_clan)
    monkeypatch.setattr(CACHE, "coc_clan_cache", fake_coc_cache)

    # Same real leader account (discord_id "111") is a member of BOTH guilds' Discord servers.
    monkeypatch.setattr(QBcore, "bot", _make_bot({100: {"111"}, 200: {"111"}}))

    # affected_guild_ids lists the ACTING guild (100) first, by construction.
    owner_guild_id, method, _ = await resolve_cwl_clan_owner("#CLAN1", "2026-09", [100, 200])

    assert owner_guild_id == "200"
    assert method == "leader_unverified"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_linked_account_not_a_member_of_any_affected_guild_is_ignored(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_cwl_clan_owner
    import QBcore

    await _seed_user_player(db, "111", "#LEADER_TAG", verified=True)
    monkeypatch.setattr(CACHE, "db_manager", db)

    fake_coc_cache = MagicMock()
    fake_clan = MagicMock()
    fake_clan.members = [_make_clan_member("#LEADER_TAG", "leader")]
    fake_coc_cache.get_clan = AsyncMock(return_value=fake_clan)
    monkeypatch.setattr(CACHE, "coc_clan_cache", fake_coc_cache)

    # The real leader's Discord account isn't a member of either affected guild — e.g. they use
    # the bot somewhere else entirely, or never joined either of these two Discord servers.
    monkeypatch.setattr(QBcore, "bot", _make_bot({100: set(), 200: set()}))

    owner_guild_id, method, _ = await resolve_cwl_clan_owner("#CLAN1", "2026-09", [100, 200])

    assert method == "unresolved_first_claimer"
    assert owner_guild_id == "100"  # first in affected_guild_ids


@pytest.mark.discord
@pytest.mark.asyncio
async def test_no_leader_or_coleader_at_all_falls_back_unresolved(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_cwl_clan_owner
    import QBcore

    monkeypatch.setattr(CACHE, "db_manager", db)

    fake_coc_cache = MagicMock()
    fake_clan = MagicMock()
    fake_clan.members = [_make_clan_member("#ELDER_TAG", "elder")]  # no leader/co-leader present
    fake_coc_cache.get_clan = AsyncMock(return_value=fake_clan)
    monkeypatch.setattr(CACHE, "coc_clan_cache", fake_coc_cache)
    monkeypatch.setattr(QBcore, "bot", _make_bot({100: set(), 200: set()}))

    owner_guild_id, method, _ = await resolve_cwl_clan_owner("#CLAN1", "2026-09", [100, 200])

    assert method == "unresolved_first_claimer"
    assert owner_guild_id == "100"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_coc_api_failure_falls_back_unresolved_without_crashing(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_cwl_clan_owner
    import QBcore

    monkeypatch.setattr(CACHE, "db_manager", db)

    fake_coc_cache = MagicMock()
    fake_coc_cache.get_clan = AsyncMock(side_effect=RuntimeError("CoC API unavailable"))
    monkeypatch.setattr(CACHE, "coc_clan_cache", fake_coc_cache)
    monkeypatch.setattr(QBcore, "bot", _make_bot({100: set()}))

    owner_guild_id, method, _ = await resolve_cwl_clan_owner("#CLAN1", "2026-09", [100])

    assert method == "unresolved_first_claimer"
    assert owner_guild_id == "100"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_resolves_owner_event_id_for_the_winning_guild(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_cwl_clan_owner
    import QBcore

    await _seed_guild_and_clan(db, "100")
    await _seed_user_player(db, "111", "#LEADER_TAG", verified=True)
    event_id = db.create_cwl_event_sync("100", "2026-09", "111")
    monkeypatch.setattr(CACHE, "db_manager", db)

    fake_coc_cache = MagicMock()
    fake_clan = MagicMock()
    fake_clan.members = [_make_clan_member("#LEADER_TAG", "leader")]
    fake_coc_cache.get_clan = AsyncMock(return_value=fake_clan)
    monkeypatch.setattr(CACHE, "coc_clan_cache", fake_coc_cache)
    monkeypatch.setattr(QBcore, "bot", _make_bot({100: {"111"}}))

    owner_guild_id, method, owner_event_id = await resolve_cwl_clan_owner("#CLAN1", "2026-09", [100])

    assert owner_guild_id == "100"
    assert owner_event_id == event_id


# ---------------------------------------------------------------------------
# ensure_cwl_clan_sharing — the orchestrator
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_ensure_sharing_noop_when_clan_not_shared(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import ensure_cwl_clan_sharing

    await _seed_guild_and_clan(db, "100", "#CLAN1")
    monkeypatch.setattr(CACHE, "db_manager", db)
    event_id = db.create_cwl_event_sync("100", "2026-09", "111")

    result = await ensure_cwl_clan_sharing(100, event_id, "2026-09", "#CLAN1")

    assert result is None
    assert db.get_cwl_shared_clan_sync("#CLAN1", "2026-09") is None


@pytest.mark.discord
@pytest.mark.asyncio
async def test_ensure_sharing_creates_shared_clan_on_first_detection(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import ensure_cwl_clan_sharing
    import QBcore

    await _seed_guild_and_clan(db, "100", "#CLAN1")
    await _seed_guild_and_clan(db, "200", "#CLAN1")
    monkeypatch.setattr(CACHE, "db_manager", db)
    event_a = db.create_cwl_event_sync("100", "2026-09", "111")
    event_b = db.create_cwl_event_sync("200", "2026-09", "222")
    db.set_cwl_event_clans_sync(event_b, [{"clan_tag": "#CLAN1", "participating": True}])

    fake_coc_cache = MagicMock()
    fake_clan = MagicMock()
    fake_clan.members = []  # unresolvable — exercises the "no candidates" -> first-claimer path
    fake_coc_cache.get_clan = AsyncMock(return_value=fake_clan)
    monkeypatch.setattr(CACHE, "coc_clan_cache", fake_coc_cache)
    monkeypatch.setattr(QBcore, "bot", _make_bot({100: set(), 200: set()}))

    # Guild 100 is adding #CLAN1, which guild 200 already has as participating.
    result = await ensure_cwl_clan_sharing(100, event_a, "2026-09", "#CLAN1")

    assert result is not None
    assert result["is_new"] is True
    assert result["other_guild_ids"] == ["200"]
    shared = db.get_cwl_shared_clan_sync("#CLAN1", "2026-09")
    assert shared is not None
    assert shared["owner_guild_id"] == "100"  # first in affected list (acting guild first)
    guild_ids = {g["guild_id"] for g in db.list_cwl_shared_clan_guilds_sync(shared["id"])}
    assert guild_ids == {"100", "200"}


@pytest.mark.discord
@pytest.mark.asyncio
async def test_ensure_sharing_attaches_to_already_established_shared_clan(db, monkeypatch):
    """A third guild adding an already-shared clan just attaches — no re-resolution, owner
    stays whoever was already resolved."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import ensure_cwl_clan_sharing

    await _seed_guild_and_clan(db, "100", "#CLAN1")
    await _seed_guild_and_clan(db, "300", "#CLAN1")
    monkeypatch.setattr(CACHE, "db_manager", db)
    event_a = db.create_cwl_event_sync("100", "2026-09", "111")
    event_c = db.create_cwl_event_sync("300", "2026-09", "d3")
    shared_clan_id = db.create_cwl_shared_clan_sync("#CLAN1", "2026-09", "100", event_a, "unresolved_first_claimer")
    db.add_guild_to_shared_clan_sync(shared_clan_id, "100", event_a)

    result = await ensure_cwl_clan_sharing(300, event_c, "2026-09", "#CLAN1")

    assert result is not None
    assert result["is_new"] is False
    assert result["owner_guild_id"] == "100"
    assert result["other_guild_ids"] == ["100"]
    guild_ids = {g["guild_id"] for g in db.list_cwl_shared_clan_guilds_sync(shared_clan_id)}
    assert guild_ids == {"100", "300"}


# ---------------------------------------------------------------------------
# Delete-season guard: get_cwl_event_shared_clan_info_sync +
# prune_or_detach_shared_clans_before_deletion (2026-08-15)
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_shared_clan_info_empty_for_event_with_no_shared_clans(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import get_cwl_event_shared_clan_info_sync

    await _seed_guild_and_clan(db, "100", "#CLAN1")
    monkeypatch.setattr(CACHE, "db_manager", db)
    event_a = db.create_cwl_event_sync("100", "2026-09", "111")
    db.set_cwl_event_clans_sync(event_a, [{"clan_tag": "#CLAN1", "participating": True}])

    assert get_cwl_event_shared_clan_info_sync(event_a, 100, "2026-09") == []


@pytest.mark.discord
@pytest.mark.asyncio
async def test_shared_clan_info_reports_other_guilds(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import get_cwl_event_shared_clan_info_sync

    await _seed_guild_and_clan(db, "100", "#CLAN1")
    await _seed_guild_and_clan(db, "200", "#CLAN1")
    monkeypatch.setattr(CACHE, "db_manager", db)
    event_a = db.create_cwl_event_sync("100", "2026-09", "111")
    event_b = db.create_cwl_event_sync("200", "2026-09", "222")
    db.set_cwl_event_clans_sync(event_a, [{"clan_tag": "#CLAN1", "participating": True}])
    shared_clan_id = db.create_cwl_shared_clan_sync("#CLAN1", "2026-09", "100", event_a, "unresolved_first_claimer")
    db.add_guild_to_shared_clan_sync(shared_clan_id, "100", event_a)
    db.add_guild_to_shared_clan_sync(shared_clan_id, "200", event_b)

    info = get_cwl_event_shared_clan_info_sync(event_a, 100, "2026-09")

    assert len(info) == 1
    assert info[0]["clan_tag"] == "#CLAN1"
    assert info[0]["owner_guild_id"] == "100"
    assert info[0]["other_guild_ids"] == ["200"]


@pytest.mark.discord
@pytest.mark.asyncio
async def test_prune_or_detach_preserves_data_and_repoints_owner_when_other_guild_remains(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import prune_or_detach_shared_clans_before_deletion
    import QBcore

    await _seed_guild_and_clan(db, "100", "#CLAN1")
    await _seed_guild_and_clan(db, "200", "#CLAN1")
    monkeypatch.setattr(CACHE, "db_manager", db)
    event_a = db.create_cwl_event_sync("100", "2026-09", "111")
    event_b = db.create_cwl_event_sync("200", "2026-09", "222")
    db.set_cwl_event_clans_sync(event_a, [{"clan_tag": "#CLAN1", "participating": True}])
    db.set_cwl_event_clans_sync(event_b, [{"clan_tag": "#CLAN1", "participating": True}])
    # Guild 100 is the current owner.
    shared_clan_id = db.create_cwl_shared_clan_sync("#CLAN1", "2026-09", "100", event_a, "unresolved_first_claimer")
    db.add_guild_to_shared_clan_sync(shared_clan_id, "100", event_a)
    db.add_guild_to_shared_clan_sync(shared_clan_id, "200", event_b)
    db.upsert_cwl_shared_clan_player_sync(shared_clan_id, "#P1", "Player", "111", "confirmed", "guest_invite", "100")

    # No resolvable leader/co-leader on the live roster — resolution falls back to
    # "first of the remaining guilds," which is guild 200 (the only one left after 100 leaves).
    fake_coc_cache = MagicMock()
    fake_clan = MagicMock()
    fake_clan.members = []
    fake_coc_cache.get_clan = AsyncMock(return_value=fake_clan)
    monkeypatch.setattr(CACHE, "coc_clan_cache", fake_coc_cache)
    monkeypatch.setattr(QBcore, "bot", _make_bot({200: set()}))

    # Guild 100 (the owner) is deleting its season.
    await prune_or_detach_shared_clans_before_deletion(100, event_a, "2026-09")

    # Shared record and its roster survive — only guild 100's attachment is gone.
    shared = db.get_cwl_shared_clan_by_id_sync(shared_clan_id)
    assert shared is not None
    assert shared["owner_guild_id"] == "200"  # repointed to the only remaining guild
    remaining_guilds = {g["guild_id"] for g in db.list_cwl_shared_clan_guilds_sync(shared_clan_id)}
    assert remaining_guilds == {"200"}
    assert db.get_cwl_shared_clan_players_sync(shared_clan_id) != []  # roster preserved intact


@pytest.mark.discord
@pytest.mark.asyncio
async def test_prune_or_detach_does_not_repoint_when_deleting_guild_is_not_owner(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import prune_or_detach_shared_clans_before_deletion

    await _seed_guild_and_clan(db, "100", "#CLAN1")
    await _seed_guild_and_clan(db, "200", "#CLAN1")
    monkeypatch.setattr(CACHE, "db_manager", db)
    event_a = db.create_cwl_event_sync("100", "2026-09", "111")
    event_b = db.create_cwl_event_sync("200", "2026-09", "222")
    db.set_cwl_event_clans_sync(event_a, [{"clan_tag": "#CLAN1", "participating": True}])
    # Guild 200 is the owner; guild 100 (non-owner) is the one deleting its season.
    shared_clan_id = db.create_cwl_shared_clan_sync("#CLAN1", "2026-09", "200", event_b, "unresolved_first_claimer")
    db.add_guild_to_shared_clan_sync(shared_clan_id, "100", event_a)
    db.add_guild_to_shared_clan_sync(shared_clan_id, "200", event_b)

    await prune_or_detach_shared_clans_before_deletion(100, event_a, "2026-09")

    shared = db.get_cwl_shared_clan_by_id_sync(shared_clan_id)
    assert shared is not None
    assert shared["owner_guild_id"] == "200"  # unchanged — 100 was never the owner
    remaining_guilds = {g["guild_id"] for g in db.list_cwl_shared_clan_guilds_sync(shared_clan_id)}
    assert remaining_guilds == {"200"}


@pytest.mark.discord
@pytest.mark.asyncio
async def test_prune_or_detach_deletes_shared_record_when_last_guild_leaves(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import prune_or_detach_shared_clans_before_deletion

    await _seed_guild_and_clan(db, "100", "#CLAN1")
    monkeypatch.setattr(CACHE, "db_manager", db)
    event_a = db.create_cwl_event_sync("100", "2026-09", "111")
    db.set_cwl_event_clans_sync(event_a, [{"clan_tag": "#CLAN1", "participating": True}])
    shared_clan_id = db.create_cwl_shared_clan_sync("#CLAN1", "2026-09", "100", event_a, "unresolved_first_claimer")
    db.add_guild_to_shared_clan_sync(shared_clan_id, "100", event_a)

    await prune_or_detach_shared_clans_before_deletion(100, event_a, "2026-09")

    assert db.get_cwl_shared_clan_by_id_sync(shared_clan_id) is None


# ---------------------------------------------------------------------------
# evict_guild_from_shared_clan — owner-only eviction (2026-08-15)
# ---------------------------------------------------------------------------

async def _seed_two_shared_guilds(db: WarHistoryDB, owner_guild_id: str, target_guild_id: str, clan_tag: str = "#CLAN1"):
    await _seed_guild_and_clan(db, owner_guild_id, clan_tag)
    await _seed_guild_and_clan(db, target_guild_id, clan_tag)
    owner_event_id = db.create_cwl_event_sync(owner_guild_id, "2026-09", "111")
    target_event_id = db.create_cwl_event_sync(target_guild_id, "2026-09", "222")
    db.set_cwl_event_clans_sync(owner_event_id, [{"clan_tag": clan_tag, "participating": True}])
    db.set_cwl_event_clans_sync(target_event_id, [{"clan_tag": clan_tag, "participating": True}])
    shared_clan_id = db.create_cwl_shared_clan_sync(clan_tag, "2026-09", owner_guild_id, owner_event_id, "unresolved_first_claimer")
    db.add_guild_to_shared_clan_sync(shared_clan_id, owner_guild_id, owner_event_id)
    db.add_guild_to_shared_clan_sync(shared_clan_id, target_guild_id, target_event_id)
    return shared_clan_id, owner_event_id, target_event_id


@pytest.mark.discord
@pytest.mark.asyncio
async def test_evict_owner_removes_target_guild(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import evict_guild_from_shared_clan

    monkeypatch.setattr(CACHE, "db_manager", db)
    shared_clan_id, _, target_event_id = await _seed_two_shared_guilds(db, "100", "200")

    result = await evict_guild_from_shared_clan(100, 200, "#CLAN1", "2026-09")

    assert result == {"ok": True, "error": None}
    remaining_guilds = {g["guild_id"] for g in db.list_cwl_shared_clan_guilds_sync(shared_clan_id)}
    assert remaining_guilds == {"100"}
    # Target guild's own cwl_event_clans row deactivated, not deleted (settings preserved).
    target_clans = db.get_cwl_event_clans_sync(target_event_id)
    assert target_clans[0]["participating"] == 0


@pytest.mark.discord
@pytest.mark.asyncio
async def test_evict_rejects_non_owner(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import evict_guild_from_shared_clan

    monkeypatch.setattr(CACHE, "db_manager", db)
    shared_clan_id, _, _ = await _seed_two_shared_guilds(db, "100", "200")

    # Guild 200 (not the owner) tries to evict guild 100 (the actual owner) — must fail.
    result = await evict_guild_from_shared_clan(200, 100, "#CLAN1", "2026-09")

    assert result == {"ok": False, "error": "not_owner"}
    remaining_guilds = {g["guild_id"] for g in db.list_cwl_shared_clan_guilds_sync(shared_clan_id)}
    assert remaining_guilds == {"100", "200"}  # nothing changed


@pytest.mark.discord
@pytest.mark.asyncio
async def test_evict_rejects_evicting_self(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import evict_guild_from_shared_clan

    monkeypatch.setattr(CACHE, "db_manager", db)
    await _seed_two_shared_guilds(db, "100", "200")

    result = await evict_guild_from_shared_clan(100, 100, "#CLAN1", "2026-09")

    assert result == {"ok": False, "error": "cannot_evict_owner"}


@pytest.mark.discord
@pytest.mark.asyncio
async def test_evict_not_shared_returns_error(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import evict_guild_from_shared_clan

    monkeypatch.setattr(CACHE, "db_manager", db)

    result = await evict_guild_from_shared_clan(100, 200, "#NEVER_SHARED", "2026-09")

    assert result == {"ok": False, "error": "not_shared"}


# ---------------------------------------------------------------------------
# sync_cwl_shared_clan_roster_to_local_pools — de-sync guard (2026-08-15)
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_sync_shared_roster_mirrors_missing_players_into_every_attached_guild(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import sync_cwl_shared_clan_roster_to_local_pools

    monkeypatch.setattr(CACHE, "db_manager", db)
    shared_clan_id, owner_event_id, target_event_id = await _seed_two_shared_guilds(db, "100", "200")
    # Owner guild (100) added this player straight into the shared roster — target guild (200)
    # never independently signed them up, so its own local cwl_signups has no idea they exist.
    db.upsert_cwl_shared_clan_player_sync(shared_clan_id, "#P1", "Alpha1", "999", "confirmed", "admin_override", "100")

    sync_cwl_shared_clan_roster_to_local_pools(shared_clan_id)

    for event_id in (owner_event_id, target_event_id):
        mirrored = db.get_cwl_signup_sync(event_id, "#P1")
        assert mirrored["status"] == "confirmed"
        assert mirrored["source"] == "guest_invite"
        assert mirrored["player_name"] == "Alpha1"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_sync_shared_roster_never_overwrites_an_existing_local_signup(db, monkeypatch):
    """The duplicate/clobber guard (project owner's explicit follow-up spec): if a guild already
    has its OWN local cwl_signups row for that player_tag — e.g. they're a genuine family member
    who already responded via DM with their own preferred_league_rank — mirroring must never
    overwrite it with a placeholder guest row."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import sync_cwl_shared_clan_roster_to_local_pools

    monkeypatch.setattr(CACHE, "db_manager", db)
    shared_clan_id, owner_event_id, target_event_id = await _seed_two_shared_guilds(db, "100", "200")
    db.upsert_cwl_signup_sync(
        target_event_id, "#P1", "RealName", "111", "th16", "template_confirm", "declined", responded_at="2026-08-14T10:00Z"
    )
    db.upsert_cwl_shared_clan_player_sync(shared_clan_id, "#P1", "Alpha1", "999", "confirmed", "admin_override", "100")

    sync_cwl_shared_clan_roster_to_local_pools(shared_clan_id)

    untouched = db.get_cwl_signup_sync(target_event_id, "#P1")
    assert untouched["player_name"] == "RealName"
    assert untouched["status"] == "declined"
    assert untouched["source"] == "template_confirm"


# ---------------------------------------------------------------------------
# auto_assign_prior_cwl_members_if_empty — auto-assign-on-add (2026-08-15, live-testing
# feedback): a clan added to the roster AFTER Start Enrollment already ran gets pre-filled with
# its own current members who have prior CWL history in that exact clan, instead of sitting
# permanently empty.
# ---------------------------------------------------------------------------

async def _seed_current_clan_member(db: WarHistoryDB, discord_id: str, player_tag: str, clan_tag: str) -> None:
    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES (?, ?)", (discord_id, discord_id))
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag) VALUES (?, ?, ?, 1, ?)",
        (discord_id, player_tag, "Player", clan_tag),
    )
    await db.conn.commit()


async def _seed_real_cwl_attack(db: WarHistoryDB, war_id: str, clan_tag: str, player_tag: str, player_name: str) -> None:
    await db.conn.execute(
        "INSERT INTO war_summary (war_id, clan_tag, opponent_tag, is_cwl, cwl_season, date) "
        "VALUES (?, ?, '#OPP', 1, '2026-08', '2026-08-01T10:00')",
        (war_id, clan_tag),
    )
    await db.conn.execute(
        "INSERT INTO war_attacks (war_id, clan_tag, date, player_name, player_tag, th_level, map_position, stars, attack_order) "
        "VALUES (?, ?, '2026-08-01T10:00', ?, ?, 15, 1, 2, 1)",
        (war_id, clan_tag, player_name, player_tag),
    )
    await db.conn.commit()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_auto_assign_fills_empty_clan_with_prior_cwl_members(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import auto_assign_prior_cwl_members_if_empty

    monkeypatch.setattr(CACHE, "db_manager", db)
    await _seed_guild_and_clan(db, "300", "#CLAN1")
    event_id = db.create_cwl_event_sync("300", "2026-09", "1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True, "roster_size": 15}])
    # #P1: current member of #CLAN1 AND played CWL for #CLAN1 before — qualifies.
    await _seed_current_clan_member(db, "10", "#P1", "#CLAN1")
    await _seed_real_cwl_attack(db, "war1", "#CLAN1", "#P1", "Alpha1")
    # #P2: current member of #CLAN1 but never played CWL anywhere — doesn't qualify.
    await _seed_current_clan_member(db, "11", "#P2", "#CLAN1")
    # #P3: played CWL for #CLAN1 before but is no longer a current member — doesn't qualify.
    await _seed_guild_and_clan(db, "300", "#OTHERCLAN")
    await _seed_current_clan_member(db, "12", "#P3", "#OTHERCLAN")
    await _seed_real_cwl_attack(db, "war2", "#CLAN1", "#P3", "Gamma3")

    await auto_assign_prior_cwl_members_if_empty(300, event_id, "2026-09", "#CLAN1")

    assignments = {a["player_tag"]: a["assigned_clan_tag"] for a in db.get_cwl_assignments_sync(event_id)}
    assert assignments == {"#P1": "#CLAN1"}
    signup = db.get_cwl_signup_sync(event_id, "#P1")
    assert signup["status"] == "pending"
    assert signup["source"] == "auto_assigned"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_auto_assign_never_overwrites_a_non_empty_roster(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import auto_assign_prior_cwl_members_if_empty

    monkeypatch.setattr(CACHE, "db_manager", db)
    await _seed_guild_and_clan(db, "301", "#CLAN1")
    event_id = db.create_cwl_event_sync("301", "2026-09", "1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True, "roster_size": 15}])
    db.upsert_cwl_signup_sync(event_id, "#EXISTING", "Existing", "1", None, "admin_added", "confirmed")
    db.upsert_cwl_assignment_sync(event_id, "#EXISTING", "#CLAN1")
    await _seed_current_clan_member(db, "10", "#P1", "#CLAN1")
    await _seed_real_cwl_attack(db, "war1", "#CLAN1", "#P1", "Alpha1")

    await auto_assign_prior_cwl_members_if_empty(301, event_id, "2026-09", "#CLAN1")

    assignments = {a["player_tag"]: a["assigned_clan_tag"] for a in db.get_cwl_assignments_sync(event_id)}
    assert assignments == {"#EXISTING": "#CLAN1"}  # #P1 was never added


@pytest.mark.discord
@pytest.mark.asyncio
async def test_auto_assign_writes_to_shared_table_for_a_shared_clan(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import auto_assign_prior_cwl_members_if_empty

    monkeypatch.setattr(CACHE, "db_manager", db)
    shared_clan_id, owner_event_id, target_event_id = await _seed_two_shared_guilds(db, "302", "303")
    await _seed_current_clan_member(db, "10", "#P1", "#CLAN1")
    await _seed_real_cwl_attack(db, "war1", "#CLAN1", "#P1", "Alpha1")

    await auto_assign_prior_cwl_members_if_empty(303, target_event_id, "2026-09", "#CLAN1")

    shared_players = {p["player_tag"]: p for p in db.get_cwl_shared_clan_players_sync(shared_clan_id)}
    assert shared_players["#P1"]["status"] == "confirmed"
    assert shared_players["#P1"]["source"] == "auto_assigned"
    # De-sync guard also ran — mirrored into the OWNER's own local pool too.
    mirrored = db.get_cwl_signup_sync(owner_event_id, "#P1")
    assert mirrored["status"] == "confirmed"


# ---------------------------------------------------------------------------
# detach_guild_from_shared_clan_on_deactivation — detach-on-deactivate (2026-08-15,
# live-testing feedback: unchecking a shared clan on the Configure Participating Clans screen,
# not deleting the whole season). Isolated unit coverage for all 3 branches, complementing the
# add/remove/re-add end-to-end flow test in test_web_bridge.py (which only exercises the
# non-owner-detach branch, since the acting guild there was never the owner).
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_detach_on_deactivation_removes_non_owner_guild_leaves_record_intact(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import detach_guild_from_shared_clan_on_deactivation

    monkeypatch.setattr(CACHE, "db_manager", db)
    shared_clan_id, owner_event_id, target_event_id = await _seed_two_shared_guilds(db, "100", "200")

    # Guild 200 (not the owner) turns the clan off.
    await detach_guild_from_shared_clan_on_deactivation(200, target_event_id, "2026-09", "#CLAN1")

    remaining = {g["guild_id"] for g in db.list_cwl_shared_clan_guilds_sync(shared_clan_id)}
    assert remaining == {"100"}
    still_shared = db.get_cwl_shared_clan_sync("#CLAN1", "2026-09")
    assert still_shared is not None
    assert still_shared["owner_guild_id"] == "100"  # unaffected — 200 was never the owner


@pytest.mark.discord
@pytest.mark.asyncio
async def test_detach_on_deactivation_repoints_ownership_when_owner_deactivates(db, monkeypatch):
    """The branch the end-to-end flow test in test_web_bridge.py does NOT exercise (its acting
    guild was never the owner there): the OWNER guild itself deactivates the clan — must repoint
    ownership to a remaining guild, not leave a departed guild as the frozen owner forever."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import detach_guild_from_shared_clan_on_deactivation
    import QBcore

    await _seed_user_player(db, "222", "#COLEADER_TAG", verified=True)  # guild 200's co-leader
    monkeypatch.setattr(CACHE, "db_manager", db)
    shared_clan_id, owner_event_id, target_event_id = await _seed_two_shared_guilds(db, "100", "200")

    fake_coc_cache = MagicMock()
    fake_clan = MagicMock()
    fake_clan.members = [_make_clan_member("#COLEADER_TAG", "co_leader")]
    fake_coc_cache.get_clan = AsyncMock(return_value=fake_clan)
    monkeypatch.setattr(CACHE, "coc_clan_cache", fake_coc_cache)
    monkeypatch.setattr(QBcore, "bot", _make_bot({100: set(), 200: {"222"}}))

    # Guild 100 (the current owner) turns the clan off — only guild 200 remains attached, and its
    # linked co-leader account is resolvable there, so ownership must repoint to 200.
    await detach_guild_from_shared_clan_on_deactivation(100, owner_event_id, "2026-09", "#CLAN1")

    remaining = {g["guild_id"] for g in db.list_cwl_shared_clan_guilds_sync(shared_clan_id)}
    assert remaining == {"200"}
    repointed = db.get_cwl_shared_clan_sync("#CLAN1", "2026-09")
    assert repointed is not None
    assert repointed["owner_guild_id"] == "200"
    assert repointed["owner_event_id"] == target_event_id
    assert repointed["owner_resolution_method"] == "coleader_verified"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_detach_on_deactivation_prunes_record_when_last_guild_deactivates(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import detach_guild_from_shared_clan_on_deactivation

    await _seed_guild_and_clan(db, "100", "#CLAN1")
    monkeypatch.setattr(CACHE, "db_manager", db)
    event_a = db.create_cwl_event_sync("100", "2026-09", "111")
    db.set_cwl_event_clans_sync(event_a, [{"clan_tag": "#CLAN1", "participating": True}])
    shared_clan_id = db.create_cwl_shared_clan_sync("#CLAN1", "2026-09", "100", event_a, "unresolved_first_claimer")
    db.add_guild_to_shared_clan_sync(shared_clan_id, "100", event_a)

    await detach_guild_from_shared_clan_on_deactivation(100, event_a, "2026-09", "#CLAN1")

    assert db.get_cwl_shared_clan_by_id_sync(shared_clan_id) is None


@pytest.mark.discord
@pytest.mark.asyncio
async def test_detach_on_deactivation_is_a_no_op_when_clan_never_shared(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import detach_guild_from_shared_clan_on_deactivation

    monkeypatch.setattr(CACHE, "db_manager", db)

    # Must not raise even though no cwl_shared_clans row exists at all for this clan/season.
    await detach_guild_from_shared_clan_on_deactivation(100, 1, "2026-09", "#NEVER_SHARED")


@pytest.mark.discord
@pytest.mark.asyncio
async def test_detach_on_deactivation_is_a_no_op_when_guild_not_actually_attached(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import detach_guild_from_shared_clan_on_deactivation

    monkeypatch.setattr(CACHE, "db_manager", db)
    shared_clan_id, owner_event_id, target_event_id = await _seed_two_shared_guilds(db, "100", "200")

    # Guild 300 is unrelated to this shared clan entirely — defensive no-op, must not touch it.
    await detach_guild_from_shared_clan_on_deactivation(300, 999, "2026-09", "#CLAN1")

    remaining = {g["guild_id"] for g in db.list_cwl_shared_clan_guilds_sync(shared_clan_id)}
    assert remaining == {"100", "200"}


# ---------------------------------------------------------------------------
# Foreign-guest conversion + purge (2026-08-15, project owner's spec): a REAL member of a shared
# clan who was cross-assigned into a DIFFERENT guild's own (private) clan "becomes a guest player
# automatically" the moment that guild detaches from the shared clan — and later gets purged
# entirely once the clan's real owning guild reassigns them elsewhere.
# ---------------------------------------------------------------------------

async def _seed_cross_assigned_real_member(db: WarHistoryDB, target_event_id: str, discord_id: str, player_tag: str) -> None:
    """#CLAN1 is shared (see _seed_two_shared_guilds); this player is a REAL current member of
    #CLAN1, cross-assigned by the target guild into its own private clan #PRIVATE (which must
    already be participating in target_event_id)."""
    await _seed_current_clan_member(db, discord_id, player_tag, "#CLAN1")
    db.upsert_cwl_assignment_sync(target_event_id, player_tag, "#PRIVATE")


@pytest.mark.discord
@pytest.mark.asyncio
async def test_detach_converts_cross_assigned_real_member_into_a_guest_player(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import detach_guild_from_shared_clan_on_deactivation

    monkeypatch.setattr(CACHE, "db_manager", db)
    # Owner = 200 (so guild 100, the one cross-assigning and detaching, is never the owner —
    # keeps this test focused purely on the conversion, not the repoint branch).
    shared_clan_id, owner_event_id, target_event_id = await _seed_two_shared_guilds(db, "200", "100")
    await _seed_guild_and_clan(db, "100", "#PRIVATE")
    db.set_cwl_event_clans_sync(target_event_id, [
        {"clan_tag": "#CLAN1", "participating": True}, {"clan_tag": "#PRIVATE", "participating": True},
    ])
    # #REALMEMBER is a genuine current member of #CLAN1, never seen this guild's cwl_signups
    # before — dragged straight from the live-membership pool onto #PRIVATE, so there's no
    # pre-existing cwl_signups row at all for them yet (handle_post_cwl_enrollment_assign's
    # private-clan branch only writes cwl_assignments).
    await _seed_cross_assigned_real_member(db, target_event_id, "77", "#REALMEMBER")
    assert db.get_cwl_signup_sync(target_event_id, "#REALMEMBER") is None  # confirm the premise

    await detach_guild_from_shared_clan_on_deactivation(100, target_event_id, "2026-09", "#CLAN1")

    signup = db.get_cwl_signup_sync(target_event_id, "#REALMEMBER")
    assert signup is not None  # created, not just updated
    assert signup["source"] == "guest_invite"
    assert signup["origin_shared_clan_id"] == shared_clan_id
    # The assignment itself is untouched — they still show up under #PRIVATE, just as a guest now.
    assignments = {a["player_tag"]: a["assigned_clan_tag"] for a in db.get_cwl_assignments_sync(target_event_id)}
    assert assignments["#REALMEMBER"] == "#PRIVATE"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_detach_does_not_convert_a_real_members_own_family_assignment(db, monkeypatch):
    """A #CLAN1 member who's ALSO genuinely one of guild 100's own family clan members (assigned
    to some OTHER clan of guild 100's for unrelated reasons) must not be touched — conversion is
    specifically for a player assigned to a DIFFERENT clan than the one being detached, which is
    already the exact condition the code checks; this just confirms a member with NO cross-clan
    assignment at all (still Unassigned) is left alone too."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import detach_guild_from_shared_clan_on_deactivation

    monkeypatch.setattr(CACHE, "db_manager", db)
    shared_clan_id, owner_event_id, target_event_id = await _seed_two_shared_guilds(db, "200", "100")
    await _seed_current_clan_member(db, "77", "#REALMEMBER", "#CLAN1")  # never assigned anywhere

    await detach_guild_from_shared_clan_on_deactivation(100, target_event_id, "2026-09", "#CLAN1")

    assert db.get_cwl_signup_sync(target_event_id, "#REALMEMBER") is None  # untouched


@pytest.mark.discord
@pytest.mark.asyncio
async def test_purge_orphaned_shared_clan_guests_removes_foreign_placement_entirely(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import (
        detach_guild_from_shared_clan_on_deactivation,
        purge_orphaned_shared_clan_guests_sync,
    )

    monkeypatch.setattr(CACHE, "db_manager", db)
    shared_clan_id, owner_event_id, target_event_id = await _seed_two_shared_guilds(db, "200", "100")
    await _seed_guild_and_clan(db, "100", "#PRIVATE")
    db.set_cwl_event_clans_sync(target_event_id, [
        {"clan_tag": "#CLAN1", "participating": True}, {"clan_tag": "#PRIVATE", "participating": True},
    ])
    await _seed_cross_assigned_real_member(db, target_event_id, "77", "#REALMEMBER")
    await detach_guild_from_shared_clan_on_deactivation(100, target_event_id, "2026-09", "#CLAN1")
    assert db.get_cwl_signup_sync(target_event_id, "#REALMEMBER") is not None  # confirm the premise

    # The clan's real owning guild (200) reassigns #REALMEMBER elsewhere within the shared
    # system — this is the trigger handle_post_cwl_enrollment_assign fires on every removal from
    # a shared clan's roster, simulated directly here at the unit level.
    purge_orphaned_shared_clan_guests_sync(shared_clan_id, "#REALMEMBER")

    # Gone entirely from guild 100's roster AND pool — not just unassigned.
    assert db.get_cwl_signup_sync(target_event_id, "#REALMEMBER") is None
    assignments = {a["player_tag"] for a in db.get_cwl_assignments_sync(target_event_id)}
    assert "#REALMEMBER" not in assignments


@pytest.mark.discord
@pytest.mark.asyncio
async def test_purge_orphaned_shared_clan_guests_is_a_no_op_for_a_never_cross_assigned_player(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import purge_orphaned_shared_clan_guests_sync

    monkeypatch.setattr(CACHE, "db_manager", db)
    shared_clan_id, owner_event_id, target_event_id = await _seed_two_shared_guilds(db, "200", "100")
    db.upsert_cwl_signup_sync(target_event_id, "#UNRELATED", "Someone", "1", None, "admin_added", "confirmed")

    # Must not raise, and must never touch an unrelated player's own genuine signup.
    purge_orphaned_shared_clan_guests_sync(shared_clan_id, "#UNRELATED")

    assert db.get_cwl_signup_sync(target_event_id, "#UNRELATED") is not None
