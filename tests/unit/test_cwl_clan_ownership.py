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
    db.set_cwl_shared_clan_player_status_sync(shared_clan_id, "#P1", "Player", "111", "confirmed", "guest_invite", "100")

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
    db.set_cwl_shared_clan_player_status_sync(shared_clan_id, "#P1", "Alpha1", "999", "confirmed", "admin_override", "100")

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
    db.set_cwl_shared_clan_player_status_sync(shared_clan_id, "#P1", "Alpha1", "999", "confirmed", "admin_override", "100")

    sync_cwl_shared_clan_roster_to_local_pools(shared_clan_id)

    untouched = db.get_cwl_signup_sync(target_event_id, "#P1")
    assert untouched["player_name"] == "RealName"
    assert untouched["status"] == "declined"
    assert untouched["source"] == "template_confirm"


# ---------------------------------------------------------------------------
# auto_assign_prior_cwl_members — auto-assign-on-add (2026-08-15, live-testing feedback): a clan
# added to the roster AFTER Start Enrollment already ran gets pre-filled with its own current
# members who have prior CWL history in that exact clan, instead of sitting permanently empty.
# Gated per PLAYER (2026-08-16 follow-up), not on the whole roster being empty, so a re-add after
# removal still seeds newly-qualifying members even when a couple of deliberately locked
# placements survived the removal.
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
    from qapbot.QBdiscocmdshelper_cwl import auto_assign_prior_cwl_members

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

    await auto_assign_prior_cwl_members(300, event_id, "2026-09", "#CLAN1")

    assignments = {a["player_tag"]: a["assigned_clan_tag"] for a in db.get_cwl_assignments_sync(event_id)}
    assert assignments == {"#P1": "#CLAN1"}
    signup = db.get_cwl_signup_sync(event_id, "#P1")
    assert signup["status"] == "pending"
    assert signup["source"] == "auto_assigned"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_auto_assign_seeds_other_qualifying_members_even_when_some_already_placed(db, monkeypatch):
    """2026-08-16, live-testing feedback, project owner's spec, verbatim: "after removing
    staycalm as guest clan and then re-adding it only the two players that were assigned manually
    are in staycalm's roster. The re-add should also have done a re-auto-assign in case the guest
    clan is not controlled by its own guild." A re-added clan can already carry a couple of
    deliberately locked survivors from before its removal — those must NOT block every other
    genuinely-qualifying player from getting auto-assigned too."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import auto_assign_prior_cwl_members

    monkeypatch.setattr(CACHE, "db_manager", db)
    await _seed_guild_and_clan(db, "301", "#CLAN1")
    event_id = db.create_cwl_event_sync("301", "2026-09", "1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True, "roster_size": 15}])
    db.upsert_cwl_signup_sync(event_id, "#EXISTING", "Existing", "1", None, "admin_added", "confirmed")
    db.upsert_cwl_assignment_sync(event_id, "#EXISTING", "#CLAN1", assignment_source="admin_override", locked=True)
    await _seed_current_clan_member(db, "10", "#P1", "#CLAN1")
    await _seed_real_cwl_attack(db, "war1", "#CLAN1", "#P1", "Alpha1")

    await auto_assign_prior_cwl_members(301, event_id, "2026-09", "#CLAN1")

    assignments = {a["player_tag"]: a for a in db.get_cwl_assignments_sync(event_id)}
    assert assignments["#EXISTING"]["assigned_clan_tag"] == "#CLAN1"
    assert assignments["#EXISTING"]["assignment_source"] == "admin_override"  # untouched, still locked
    assert assignments["#EXISTING"]["locked"] == 1
    assert assignments["#P1"]["assigned_clan_tag"] == "#CLAN1"  # newly seeded despite #EXISTING already there


@pytest.mark.discord
@pytest.mark.asyncio
async def test_auto_assign_never_re_places_a_player_already_placed_in_this_clan(db, monkeypatch):
    """The exclusion is per-player, keyed on THIS clan specifically — a player already assigned
    to clan_tag (even if they'd otherwise also qualify via prior CWL history there) is left
    completely alone, so a locked/admin_override placement is never silently downgraded to a
    plain 'suggested' one by a later re-add."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import auto_assign_prior_cwl_members

    monkeypatch.setattr(CACHE, "db_manager", db)
    await _seed_guild_and_clan(db, "307", "#CLAN1")
    event_id = db.create_cwl_event_sync("307", "2026-09", "1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True, "roster_size": 15}])
    await _seed_current_clan_member(db, "10", "#P1", "#CLAN1")
    await _seed_real_cwl_attack(db, "war1", "#CLAN1", "#P1", "Alpha1")
    db.upsert_cwl_signup_sync(event_id, "#P1", "Alpha1", "10", None, "admin_added", "confirmed")
    db.upsert_cwl_assignment_sync(event_id, "#P1", "#CLAN1", assignment_source="admin_override", locked=True)

    await auto_assign_prior_cwl_members(307, event_id, "2026-09", "#CLAN1")

    assignment = next(a for a in db.get_cwl_assignments_sync(event_id) if a["player_tag"] == "#P1")
    assert assignment["assignment_source"] == "admin_override"
    assert assignment["locked"] == 1


@pytest.mark.discord
@pytest.mark.asyncio
async def test_auto_assign_writes_to_shared_table_for_a_shared_clan(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import auto_assign_prior_cwl_members

    monkeypatch.setattr(CACHE, "db_manager", db)
    shared_clan_id, owner_event_id, target_event_id = await _seed_two_shared_guilds(db, "302", "303")
    await _seed_current_clan_member(db, "10", "#P1", "#CLAN1")
    await _seed_real_cwl_attack(db, "war1", "#CLAN1", "#P1", "Alpha1")

    await auto_assign_prior_cwl_members(303, target_event_id, "2026-09", "#CLAN1")

    shared_players = {p["player_tag"]: p for p in db.get_cwl_shared_clan_players_sync(shared_clan_id)}
    assert shared_players["#P1"]["assigned"] == 1
    # A placement write never touches status (2026-08-16: status/assigned are deliberately
    # separate columns) — #P1 never actually responded, so it stays at the honest default.
    assert shared_players["#P1"]["status"] == "pending"
    assert shared_players["#P1"]["source"] == "auto_assigned"
    # De-sync guard also ran — mirrored into the OWNER's own local pool too, carrying forward
    # the same honest 'pending' status (mirroring copies the shared row's real status, and it was
    # never touched by this placement write either).
    mirrored = db.get_cwl_signup_sync(owner_event_id, "#P1")
    assert mirrored["status"] == "pending"


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


# ---------------------------------------------------------------------------
# Orphaned-assignment preservation (2026-08-16, live-testing feedback, project owner's spec): a
# player assigned INTO the shared clan itself (the opposite direction of the guest-conversion
# tests above) must not simply vanish from the board once this guild detaches — their assignment
# is mirrored into a local cwl_assignments row so the frontend's new "Assigned to other Guild"
# column can surface them, and it self-cleans via the same purge mechanism once the clan's real
# owning guild reassigns them elsewhere.
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_detach_mirrors_confirmed_shared_roster_into_local_orphaned_assignment(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import detach_guild_from_shared_clan_on_deactivation

    monkeypatch.setattr(CACHE, "db_manager", db)
    shared_clan_id, owner_event_id, target_event_id = await _seed_two_shared_guilds(db, "200", "100")
    # #STUCK was drag-assigned INTO the shared clan itself from guild 100's own board — lives
    # only in cwl_shared_clan_players (handle_post_cwl_enrollment_assign's shared-destination
    # branch never touches cwl_assignments), confirmed, added by guild 100.
    db.set_cwl_shared_clan_player_assignment_sync(shared_clan_id, "#STUCK", "Stuck", "77", True, "admin_override", "100")

    await detach_guild_from_shared_clan_on_deactivation(100, target_event_id, "2026-09", "#CLAN1")

    # Mirrored locally: still shows as assigned to #CLAN1 (now a non-existent column for guild
    # 100), instead of vanishing from the board entirely.
    assignments_by_tag = {a["player_tag"]: a for a in db.get_cwl_assignments_sync(target_event_id)}
    assert assignments_by_tag["#STUCK"]["assigned_clan_tag"] == "#CLAN1"
    # Mirrored as assignment_source="admin_override"/locked=True — the SAME live values a real
    # drag-and-drop placement carries (2026-08-19 fix, project owner's spec: this placement must
    # survive a later Remove too, not just Uncheck — only these exact values are what
    # _cleanup_local_pool_for_plain_clan_deactivation_sync preserves).
    assert assignments_by_tag["#STUCK"]["assignment_source"] == "admin_override"
    assert assignments_by_tag["#STUCK"]["locked"] == 1
    signup = db.get_cwl_signup_sync(target_event_id, "#STUCK")
    assert signup is not None
    assert signup["origin_shared_clan_id"] == shared_clan_id
    # The shared roster itself is untouched — still there for the owner's own board.
    shared_players = {p["player_tag"] for p in db.get_cwl_shared_clan_players_sync(shared_clan_id)}
    assert "#STUCK" in shared_players


@pytest.mark.discord
@pytest.mark.asyncio
async def test_detach_does_not_mirror_a_non_confirmed_shared_roster_player(db, monkeypatch):
    """A pending/withdrawn/declined shared-roster row was never actually "assigned" to the clan
    (only status='confirmed' counts as an assignment for a shared clan — see
    _build_enrollment_payload's merge) — must not be mirrored as an orphaned assignment."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import detach_guild_from_shared_clan_on_deactivation

    monkeypatch.setattr(CACHE, "db_manager", db)
    shared_clan_id, owner_event_id, target_event_id = await _seed_two_shared_guilds(db, "200", "100")
    db.set_cwl_shared_clan_player_status_sync(shared_clan_id, "#PENDING_PLAYER", "Pending", "77", "pending", "auto_seeded", "100")

    await detach_guild_from_shared_clan_on_deactivation(100, target_event_id, "2026-09", "#CLAN1")

    assert db.get_cwl_signup_sync(target_event_id, "#PENDING_PLAYER") is None
    assignments = {a["player_tag"] for a in db.get_cwl_assignments_sync(target_event_id)}
    assert "#PENDING_PLAYER" not in assignments


@pytest.mark.discord
@pytest.mark.asyncio
async def test_detach_does_not_mirror_a_real_members_own_deliberate_self_assignment(db, monkeypatch):
    """2026-08-19 live bug report, project owner — the write-time counterpart of
    test_shared_guest_clan_remove_purges_own_member_deliberately_placed_into_own_column below:
    "STY - Basement," a genuine current member of the shared clan itself, deliberately
    drag-assigned into that same clan's OWN column, still got mirrored into a local "Assigned to
    other Guild" placement — and since this mirror is the ONLY thing detach_guild_from_shared_
    clan_on_deactivation does on a plain UNCHECK (the destructive cleanup that would otherwise
    catch this only runs from the explicit Remove button, per rule f), this showed up even before
    Remove was ever clicked. Contrast with test_detach_mirrors_confirmed_shared_roster_into_local_
    orphaned_assignment above (#STUCK, no known live current clan — the genuinely preservable
    case). Project owner's correction of the framing, verbatim: "the assigned to other guild case
    serves a different purpose, namely a player that is rightfully member of the current player
    pool... but is assigned to another guild's roster" — a real direct member of the clan itself
    was never "rightfully in this guild's pool" to begin with."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import detach_guild_from_shared_clan_on_deactivation

    monkeypatch.setattr(CACHE, "db_manager", db)
    shared_clan_id, owner_event_id, target_event_id = await _seed_two_shared_guilds(db, "200", "100")
    # #BASEMENT is a genuine CURRENT member of #CLAN1 itself (the shared clan), deliberately
    # drag-assigned into its own column.
    await _seed_current_clan_member(db, "77", "#BASEMENT", "#CLAN1")
    db.set_cwl_shared_clan_player_assignment_sync(shared_clan_id, "#BASEMENT", "Basement", "77", True, "admin_override", "100")

    await detach_guild_from_shared_clan_on_deactivation(100, target_event_id, "2026-09", "#CLAN1")

    # No local mirror written at all — nothing to preserve here, this is a mere Uncheck, not
    # even a Remove yet.
    assert db.get_cwl_signup_sync(target_event_id, "#BASEMENT") is None
    assignments = {a["player_tag"] for a in db.get_cwl_assignments_sync(target_event_id)}
    assert "#BASEMENT" not in assignments
    # The shared roster itself is untouched either way (this function never writes to it).
    shared_players = {p["player_tag"] for p in db.get_cwl_shared_clan_players_sync(shared_clan_id)}
    assert "#BASEMENT" in shared_players


@pytest.mark.discord
@pytest.mark.asyncio
async def test_detach_does_not_mirror_an_auto_seeded_or_auto_assigned_confirmed_player(db, monkeypatch):
    """Live-testing feedback (2026-08-16, project owner's spec, verbatim): "all players from
    that guest clan that are not already assigned to a member clan player roster should be
    removed from the player pool completely... only [the one deliberately drag-assigned player]
    should have stayed." A confirmed-but-auto_assigned player (auto_assign_prior_cwl_members_
    if_empty's "prior CWL history" seed — a passive side effect of the clan being added, not a
    deliberate cross-guild placement) must NOT be mirrored, unlike a genuine admin_override
    drag-and-drop (covered by the sibling test above)."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import detach_guild_from_shared_clan_on_deactivation

    monkeypatch.setattr(CACHE, "db_manager", db)
    shared_clan_id, owner_event_id, target_event_id = await _seed_two_shared_guilds(db, "200", "100")
    db.set_cwl_shared_clan_player_assignment_sync(shared_clan_id, "#AUTO_PLAYER", "Auto", "77", True, "auto_assigned", "100")

    await detach_guild_from_shared_clan_on_deactivation(100, target_event_id, "2026-09", "#CLAN1")

    assert db.get_cwl_signup_sync(target_event_id, "#AUTO_PLAYER") is None
    assignments = {a["player_tag"] for a in db.get_cwl_assignments_sync(target_event_id)}
    assert "#AUTO_PLAYER" not in assignments
    # The shared roster itself is untouched regardless — still visible to the real owning guild.
    shared_players = {p["player_tag"] for p in db.get_cwl_shared_clan_players_sync(shared_clan_id)}
    assert "#AUTO_PLAYER" in shared_players


@pytest.mark.discord
@pytest.mark.asyncio
async def test_detach_no_longer_deletes_a_players_existing_local_mirror(db, monkeypatch):
    """SUPERSEDED 2026-08-19 (rule f, live bug report: "Members vanished from player pool after
    uncheck for StayCalm" — a genuinely cross-guild-shared clan). This test used to assert the
    OPPOSITE — that detaching actively deletes a player's existing local cwl_signups/
    cwl_assignments mirror (from while the clan was still an active, participating column) —
    which was correct behavior BEFORE rule f existed (2026-08-16 fix, project owner's spec at the
    time, verbatim: "The STY members still show up in both pools..."). Rule f (2026-08-18)
    requires a mere uncheck+Save to be purely cosmetic for the player pool here too, exactly like
    the plain-clan branch (shared is None) — this test was never revisited when rule f shipped,
    so it kept locking in the pre-rule-f (now wrong) behavior. Deletion now only ever happens via
    the explicit "Remove" button (remove_cwl_guest_clan)."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import detach_guild_from_shared_clan_on_deactivation

    monkeypatch.setattr(CACHE, "db_manager", db)
    shared_clan_id, owner_event_id, target_event_id = await _seed_two_shared_guilds(db, "200", "100")
    db.set_cwl_shared_clan_player_assignment_sync(shared_clan_id, "#AUTO_PLAYER", "Auto", "77", True, "auto_assigned", "100")
    # Existing local mirror, as if sync_cwl_shared_clan_roster_to_local_pools() already wrote it
    # while the clan was still active/participating.
    db.upsert_cwl_signup_sync(target_event_id, "#AUTO_PLAYER", "Auto", "77", None, "guest_invite", "pending")
    db.upsert_cwl_assignment_sync(target_event_id, "#AUTO_PLAYER", "#CLAN1", assignment_source="orphaned_on_detach", locked=False)

    await detach_guild_from_shared_clan_on_deactivation(100, target_event_id, "2026-09", "#CLAN1")

    assert db.get_cwl_signup_sync(target_event_id, "#AUTO_PLAYER") is not None
    assignments = {a["player_tag"] for a in db.get_cwl_assignments_sync(target_event_id)}
    assert "#AUTO_PLAYER" in assignments


@pytest.mark.discord
@pytest.mark.asyncio
async def test_detach_never_deletes_a_genuine_local_signup_sharing_a_shared_roster_tag(db, monkeypatch):
    """A player who is a CURRENT member of one of this guild's own family clans must never have
    their local signup deleted by the stale-mirror cleanup, even if they also happen to carry old
    auto_assigned/auto_seeded history on the shared roster (e.g. from before they moved into the
    family clan) — live current-clan membership, not the local row's source field, is what the
    cleanup gates on (2026-08-16 follow-up: the source-field check turned out too narrow, since
    auto_assign_prior_cwl_members_if_empty() itself writes local rows with source='auto_assigned'/
    'auto_seeded' whenever it runs before a clan is detected as shared)."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import detach_guild_from_shared_clan_on_deactivation

    monkeypatch.setattr(CACHE, "db_manager", db)
    shared_clan_id, owner_event_id, target_event_id = await _seed_two_shared_guilds(db, "200", "100")
    monkeypatch.setattr(CACHE, "server_config", {"100": {"member_clans": ["#FAMILY_CLAN"], "member_families": []}})
    monkeypatch.setattr(CACHE, "clan_families", {})
    await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES ('#FAMILY_CLAN', 'Family Clan')")
    await db.conn.commit()
    await _seed_current_clan_member(db, "77", "#REAL_SIGNUP", "#FAMILY_CLAN")
    db.set_cwl_shared_clan_player_assignment_sync(shared_clan_id, "#REAL_SIGNUP", "Real", "77", True, "auto_assigned", "100")
    db.upsert_cwl_signup_sync(target_event_id, "#REAL_SIGNUP", "Real", "77", None, "template_confirm", "confirmed")

    await detach_guild_from_shared_clan_on_deactivation(100, target_event_id, "2026-09", "#CLAN1")

    signup = db.get_cwl_signup_sync(target_event_id, "#REAL_SIGNUP")
    assert signup is not None
    assert signup["source"] == "template_confirm"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_orphaned_assignment_gets_purged_when_owning_guild_reassigns_elsewhere(db, monkeypatch):
    """Closes the loop: once mirrored as an orphaned local assignment, the SAME purge mechanism
    that already cleans up foreign guests (test_purge_orphaned_shared_clan_guests_removes_
    foreign_placement_entirely) must also clean up this guild's own now-stale local copy the
    moment the clan's real owning guild reassigns that exact player away from the shared clan."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import detach_guild_from_shared_clan_on_deactivation, purge_orphaned_shared_clan_guests_sync

    monkeypatch.setattr(CACHE, "db_manager", db)
    shared_clan_id, owner_event_id, target_event_id = await _seed_two_shared_guilds(db, "200", "100")
    db.set_cwl_shared_clan_player_assignment_sync(shared_clan_id, "#STUCK", "Stuck", "77", True, "admin_override", "100")
    await detach_guild_from_shared_clan_on_deactivation(100, target_event_id, "2026-09", "#CLAN1")
    assert db.get_cwl_signup_sync(target_event_id, "#STUCK") is not None  # confirm the premise

    purge_orphaned_shared_clan_guests_sync(shared_clan_id, "#STUCK")

    assert db.get_cwl_signup_sync(target_event_id, "#STUCK") is None
    assignments = {a["player_tag"] for a in db.get_cwl_assignments_sync(target_event_id)}
    assert "#STUCK" not in assignments


@pytest.mark.discord
@pytest.mark.asyncio
async def test_remove_guest_clan_purges_local_pool_even_when_clan_is_shared(db, monkeypatch):
    """Bug fixed 2026-08-18 (live-tested in DEV, project owner's report, verbatim: "Remove guest
    clan didn't remove the players from the player pool!"). remove_cwl_guest_clan() used to only
    run the destructive local-pool cleanup (_cleanup_local_pool_for_plain_clan_deactivation_sync)
    for a clan that was NEVER cross-guild shared — a shared one only got
    detach_guild_from_shared_clan_on_deactivation's cross-guild bookkeeping, which never touches
    THIS guild's own local cwl_signups/cwl_assignments rows for the clan's real current members
    (only whatever happens to already be registered in cwl_shared_clan_players). Fixed by running
    the local cleanup unconditionally, after the shared-detach step."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import remove_cwl_guest_clan

    monkeypatch.setattr(CACHE, "db_manager", db)
    monkeypatch.setattr(CACHE, "server_config", {"100": {"member_clans": [], "member_families": []}})
    monkeypatch.setattr(CACHE, "clan_families", {})
    shared_clan_id, owner_event_id, target_event_id = await _seed_two_shared_guilds(db, "100", "200")

    # A real current member of the shared clan, with a genuine LOCAL signup/assignment on the
    # owner guild's own board — never registered in cwl_shared_clan_players at all (the exact gap
    # the shared-detach branch alone can't see, since it only iterates that table).
    await _seed_current_clan_member(db, "10", "#P1", "#CLAN1")
    db.upsert_cwl_signup_sync(owner_event_id, "#P1", "P1", "10", None, "template_confirm", "pending")
    db.upsert_cwl_assignment_sync(owner_event_id, "#P1", "#CLAN1", assignment_source="suggested", locked=False)

    await remove_cwl_guest_clan(100, owner_event_id, "2026-09", "#CLAN1")

    assert db.get_cwl_signup_sync(owner_event_id, "#P1") is None  # the fix
    assert db.get_cwl_assignments_sync(owner_event_id) == []
    assert db.get_cwl_event_clans_sync(owner_event_id) == []  # the clan row itself is gone too


@pytest.mark.discord
@pytest.mark.asyncio
async def test_remove_guest_clan_still_preserves_a_deliberate_admin_override_placement(db, monkeypatch):
    """2026-08-19 fix, project owner's spec, verbatim, confirmed explicitly: "'assigned players
    remain in their rosters... becomes a guest player automatically' even when the clan is
    removed" — NOT just on a mere Uncheck. Before this fix, detach_guild_from_shared_clan_on_
    deactivation's shared-roster mirror-write used a softer assignment_source ("orphaned_on_
    detach"/locked=False) that _cleanup_local_pool_for_plain_clan_deactivation_sync's own
    preservation check didn't recognize — so remove_cwl_guest_clan silently deleted the very
    placement it was supposed to preserve, the instant after the shared-detach step wrote it.
    This is the sibling of test_remove_guest_clan_purges_local_pool_even_when_clan_is_shared
    above: that test proves ordinary (non-deliberate) members DO get purged by Remove; this one
    proves a genuine deliberate placement still does NOT, exactly like it survives a mere
    Uncheck (test_detach_mirrors_confirmed_shared_roster_into_local_orphaned_assignment)."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import remove_cwl_guest_clan

    monkeypatch.setattr(CACHE, "db_manager", db)
    monkeypatch.setattr(CACHE, "server_config", {"100": {"member_clans": [], "member_families": []}})
    monkeypatch.setattr(CACHE, "clan_families", {})
    shared_clan_id, owner_event_id, target_event_id = await _seed_two_shared_guilds(db, "200", "100")
    # #STUCK was drag-assigned INTO the shared clan itself from guild 100's own board — a genuine
    # deliberate placement, exactly like the mere-Uncheck sibling test's setup.
    db.set_cwl_shared_clan_player_assignment_sync(shared_clan_id, "#STUCK", "Stuck", "77", True, "admin_override", "100")

    await remove_cwl_guest_clan(100, target_event_id, "2026-09", "#CLAN1")

    signup = db.get_cwl_signup_sync(target_event_id, "#STUCK")
    assert signup is not None  # NOT purged, unlike an ordinary member
    assignments = {a["player_tag"]: a["assigned_clan_tag"] for a in db.get_cwl_assignments_sync(target_event_id)}
    assert assignments.get("#STUCK") == "#CLAN1"
    # The clan itself is still fully removed — only the deliberate placement survives.
    assert db.get_cwl_event_clans_sync(target_event_id) == []


@pytest.mark.discord
@pytest.mark.asyncio
async def test_remove_guest_clan_preserves_a_member_drag_assigned_into_a_family_clan(db, monkeypatch):
    """The OTHER direction of the same rule (2026-08-19 fix, live bug report, project owner,
    verbatim: "I dragged a StayCalm member to TheQCrew's roster and then I removed StayCalm.
    According to our rule that member should have stayed but is gone now"). Distinct from the
    sibling test above (which drags a player INTO the shared clan's own column) — here a real
    CURRENT member of the shared clan being removed is manually drag-assigned the OTHER way,
    into one of the guild's own family clans instead. Their local cwl_assignments row points at
    the FAMILY clan, not the shared clan being removed — _cleanup_local_pool_for_plain_clan_
    deactivation_sync's preservation check used to only look at an assignment scoped to the clan
    being removed, so it never found this one and deleted it (and the player's signup) anyway."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import remove_cwl_guest_clan

    monkeypatch.setattr(CACHE, "db_manager", db)
    monkeypatch.setattr(CACHE, "server_config", {"100": {"member_clans": ["#FAMILY_CLAN"], "member_families": []}})
    monkeypatch.setattr(CACHE, "clan_families", {})
    shared_clan_id, owner_event_id, target_event_id = await _seed_two_shared_guilds(db, "200", "100")
    await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES ('#FAMILY_CLAN', 'Family Clan')")
    await db.conn.commit()
    # #DRAGGED is a real CURRENT member of #CLAN1 (the shared clan about to be removed)...
    await _seed_current_clan_member(db, "77", "#DRAGGED", "#CLAN1")
    db.upsert_cwl_signup_sync(target_event_id, "#DRAGGED", "Dragged", "77", None, "auto_assigned", "pending")
    # ...but was manually drag-assigned into the guild's own family clan instead — assign_cwl_
    # player_sync's real values for a Manage Enrollment drag (handle_post_cwl_enrollment_assign,
    # web_bridge.py: source="admin_override", locked=True).
    db.upsert_cwl_assignment_sync(target_event_id, "#DRAGGED", "#FAMILY_CLAN", assignment_source="admin_override", locked=True)

    await remove_cwl_guest_clan(100, target_event_id, "2026-09", "#CLAN1")

    signup = db.get_cwl_signup_sync(target_event_id, "#DRAGGED")
    assert signup is not None  # NOT purged
    assignments = {a["player_tag"]: a["assigned_clan_tag"] for a in db.get_cwl_assignments_sync(target_event_id)}
    assert assignments.get("#DRAGGED") == "#FAMILY_CLAN"  # placement survives, fully untouched


@pytest.mark.discord
@pytest.mark.asyncio
async def test_remove_guest_clan_purges_local_mirror_for_a_player_who_since_left_the_shared_clan(db, monkeypatch):
    """2026-08-19 fix, live bug report, project owner, verbatim: "I NEVER ADDED THOSE GUESTS
    MANUALLY THROUGH THE PLAYER GUEST INVITE FEATURE!!!" — followed by tracing the real sequence:
    a shared clan's roster member gets mirrored into this guild's own local cwl_signups by
    sync_cwl_shared_clan_roster_to_local_pools as a source='guest_invite' placeholder the moment
    the shared clan is added (never a manual invite), with no cwl_assignments row and no
    origin_shared_clan_id written. If that player has since left the shared clan in real life,
    they were invisible to BOTH of this cleanup's other candidate sources (not a live clan_tag
    member, no local assignment pointing at clan_tag) — their mirror row lingered forever, later
    showing up as an oddly "individually removable" guest player the admin never actually
    invited."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import remove_cwl_guest_clan, sync_cwl_shared_clan_roster_to_local_pools

    monkeypatch.setattr(CACHE, "db_manager", db)
    monkeypatch.setattr(CACHE, "server_config", {"100": {"member_clans": [], "member_families": []}})
    monkeypatch.setattr(CACHE, "clan_families", {})
    shared_clan_id, owner_event_id, target_event_id = await _seed_two_shared_guilds(db, "200", "100")
    # #FORMER_MEMBER was a genuine participant of #CLAN1 (tracked on the SHARED roster, not this
    # guild's local tables) while #CLAN1 was active on guild 100's board.
    db.set_cwl_shared_clan_player_status_sync(
        shared_clan_id, "#FORMER_MEMBER", "Former", "77", "pending", "auto_seeded", "100",
    )
    # sync_cwl_shared_clan_roster_to_local_pools mirrors it into guild 100's own local cwl_signups
    # — exactly what the real seeding flow does (auto_assign_prior_cwl_members_sync's Step 2, via
    # this same function), never anything the admin clicked themselves.
    sync_cwl_shared_clan_roster_to_local_pools(shared_clan_id)
    assert db.get_cwl_signup_sync(target_event_id, "#FORMER_MEMBER")["source"] == "guest_invite"

    # ...but #FORMER_MEMBER has SINCE left #CLAN1 in real life — now a member of some unrelated
    # clan the guild has never heard of.
    await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES ('#UNRELATED', 'Unrelated Clan')")
    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES ('77', '77')")
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag) "
        "VALUES ('77', '#FORMER_MEMBER', 'Former', 1, '#UNRELATED')"
    )
    await db.conn.commit()

    await remove_cwl_guest_clan(100, target_event_id, "2026-09", "#CLAN1")

    assert db.get_cwl_signup_sync(target_event_id, "#FORMER_MEMBER") is None  # the fix


@pytest.mark.discord
@pytest.mark.asyncio
async def test_remove_shared_guest_clan_clears_stale_pointer_for_a_real_family_member(db, monkeypatch):
    """The exact live scenario, reproduced (2026-08-19, project owner, verbatim: "the qcrew
    members were falsely auto-assigned to staycalm. On the other hand... after removing staycalm
    the error becomes obvious"). A player who is a genuine CURRENT member of the guild's own
    family clan (TheQCrew) somehow ended up with a non-deliberate local assignment pointing at
    #CLAN1 (a shared guest clan, e.g. from a prior-CWL-history seed that predates them joining
    the family clan). Confirmed via the live dev log/DB: their signup must survive (they're a
    real pool member) but the stale assignment must be cleared — left alone, the board renders
    them stuck in "Assigned to other Guild" forever once #CLAN1 is removed, even though they're
    just an ordinary family-clan member who should show as Unassigned."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import remove_cwl_guest_clan

    monkeypatch.setattr(CACHE, "db_manager", db)
    monkeypatch.setattr(CACHE, "server_config", {"100": {"member_clans": ["#FAMILY_CLAN"], "member_families": []}})
    monkeypatch.setattr(CACHE, "clan_families", {})
    shared_clan_id, owner_event_id, target_event_id = await _seed_two_shared_guilds(db, "200", "100")
    await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES ('#FAMILY_CLAN', 'Family Clan')")
    await db.conn.commit()
    await _seed_current_clan_member(db, "77", "#QCREW_MEMBER", "#FAMILY_CLAN")
    db.upsert_cwl_signup_sync(target_event_id, "#QCREW_MEMBER", "QMember", "77", None, "template_confirm", "confirmed")
    # Non-deliberate — e.g. resolve_prior_cwl_assignments seeded them into #CLAN1 based on stale
    # prior-CWL-attack history, before real-world data caught up to them now being a #FAMILY_CLAN
    # member. NOT admin_override/locked (that's the sibling test's scenario, correctly preserved
    # as-is regardless).
    db.upsert_cwl_assignment_sync(target_event_id, "#QCREW_MEMBER", "#CLAN1", assignment_source="suggested", locked=False)

    await remove_cwl_guest_clan(100, target_event_id, "2026-09", "#CLAN1")

    signup = db.get_cwl_signup_sync(target_event_id, "#QCREW_MEMBER")
    assert signup is not None  # pool membership survives
    assert signup["status"] == "confirmed"  # their real response is untouched
    assignments = {a["player_tag"]: a["assigned_clan_tag"] for a in db.get_cwl_assignments_sync(target_event_id)}
    assert "#QCREW_MEMBER" not in assignments  # stale pointer cleared — falls back to Unassigned


# ---------------------------------------------------------------------------
# detach_guild_from_shared_clan_on_deactivation — the `shared is None` branch: a PLAIN guest clan
# (never cross-guild shared with anyone else). 2026-08-16 follow-up, live-testing feedback,
# project owner's spec, verbatim: "I removed staycalm but their players were not removed from
# the qcrew's player pool as it should have. we fixed this earlier this day and now it's back."
# Not actually a regression of that earlier fix — every round of it only ever operated on
# cwl_shared_clan_players, gated behind this exact function's `if shared is None: return` a few
# lines up top — a clan that was NEVER cross-guild shared hit that early return and got zero
# cleanup the entire time. Same rule, applied to the local tables directly instead.
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_plain_guest_clan_detach_removes_auto_assigned_and_auto_seeded_players(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import remove_cwl_guest_clan

    monkeypatch.setattr(CACHE, "db_manager", db)
    monkeypatch.setattr(CACHE, "server_config", {"300": {"member_clans": [], "member_families": []}})
    monkeypatch.setattr(CACHE, "clan_families", {})
    await _seed_guild_and_clan(db, "300", "#GUESTCLAN")
    event_id = db.create_cwl_event_sync("300", "2026-09", "111")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#GUESTCLAN", "participating": True}])
    await _seed_current_clan_member(db, "10", "#AUTO_ASSIGNED", "#GUESTCLAN")
    await _seed_current_clan_member(db, "20", "#AUTO_SEEDED", "#GUESTCLAN")
    # #AUTO_ASSIGNED: auto_assign_prior_cwl_members_if_empty's step-1 seed (real prior history) —
    # a passive machine placement, not a deliberate admin action.
    db.upsert_cwl_signup_sync(event_id, "#AUTO_ASSIGNED", "Auto", "10", None, "auto_assigned", "pending")
    db.upsert_cwl_assignment_sync(event_id, "#AUTO_ASSIGNED", "#GUESTCLAN", assignment_source="suggested", locked=False)
    # #AUTO_SEEDED: step-2's pure visibility placeholder — pending, no assignment at all.
    db.upsert_cwl_signup_sync(event_id, "#AUTO_SEEDED", "Seeded", "20", None, "auto_seeded", "pending")

    await remove_cwl_guest_clan(300, event_id, "2026-09", "#GUESTCLAN")

    assert db.get_cwl_signup_sync(event_id, "#AUTO_ASSIGNED") is None
    assert db.get_cwl_signup_sync(event_id, "#AUTO_SEEDED") is None
    assignments = {a["player_tag"] for a in db.get_cwl_assignments_sync(event_id)}
    assert "#AUTO_ASSIGNED" not in assignments


@pytest.mark.discord
@pytest.mark.asyncio
async def test_plain_guest_clan_remove_preserves_deliberate_cross_assignment_to_a_different_clan(db, monkeypatch):
    """A genuine human drag-and-drop placement into a DIFFERENT clan than the one being removed
    must survive — same "Assigned to other Guild" treatment the shared-clan branch gives its own
    admin_override players (see test_detach_converts_cross_assigned_real_member_into_a_guest_player
    above), except here the local cwl_assignments row already IS the real assignment (no shared
    table to mirror from), so preserving it is simply not deleting it."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import remove_cwl_guest_clan

    monkeypatch.setattr(CACHE, "db_manager", db)
    monkeypatch.setattr(CACHE, "server_config", {"301": {"member_clans": [], "member_families": []}})
    monkeypatch.setattr(CACHE, "clan_families", {})
    await _seed_guild_and_clan(db, "301", "#GUESTCLAN")
    await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES ('#OTHERCLAN', 'Other Clan')")
    await db.conn.commit()
    event_id = db.create_cwl_event_sync("301", "2026-09", "111")
    db.set_cwl_event_clans_sync(event_id, [
        {"clan_tag": "#GUESTCLAN", "participating": True}, {"clan_tag": "#OTHERCLAN", "participating": True},
    ])
    await _seed_current_clan_member(db, "10", "#DRAGGED", "#GUESTCLAN")
    db.upsert_cwl_signup_sync(event_id, "#DRAGGED", "Dragged", "10", None, "admin_added", "pending")
    db.upsert_cwl_assignment_sync(event_id, "#DRAGGED", "#OTHERCLAN", assignment_source="admin_override", locked=True)

    await remove_cwl_guest_clan(301, event_id, "2026-09", "#GUESTCLAN")

    signup = db.get_cwl_signup_sync(event_id, "#DRAGGED")
    assert signup is not None
    assignments = {a["player_tag"]: a["assigned_clan_tag"] for a in db.get_cwl_assignments_sync(event_id)}
    assert assignments.get("#DRAGGED") == "#OTHERCLAN"  # untouched, still a real placement


@pytest.mark.discord
@pytest.mark.asyncio
async def test_plain_guest_clan_remove_purges_deliberate_assignment_into_the_removed_clan_itself(db, monkeypatch):
    """2026-08-19 live bug report, project owner: he added Hohenloher Land as a guest clan, then
    manually drag-assigned its own real member Akaza INTO Hohenloher Land's own column (not a
    cross-assignment elsewhere) — then clicked Remove on Hohenloher Land. Akaza still lingered in
    the pool (visible in "Remove Guest Players…") instead of being purged like every other member.

    Root cause: the deliberate-placement preservation check preserved an admin_override/locked
    assignment "wherever it currently points," with no exception for pointing AT clan_tag itself.
    The whole point of preservation is "a cross-assignment into a DIFFERENT clan survives its
    origin clan's removal" (see the sibling test above) — there's nothing to preserve when the
    placement points at the clan being fully removed: that column/roster is gone entirely, so a
    player manually placed into their own clan's column must be purged exactly like any other
    ordinary (non-deliberate) direct member of that clan."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import remove_cwl_guest_clan

    monkeypatch.setattr(CACHE, "db_manager", db)
    monkeypatch.setattr(CACHE, "server_config", {"307": {"member_clans": [], "member_families": []}})
    monkeypatch.setattr(CACHE, "clan_families", {})
    await _seed_guild_and_clan(db, "307", "#GUESTCLAN")
    event_id = db.create_cwl_event_sync("307", "2026-09", "111")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#GUESTCLAN", "participating": True}])
    await _seed_current_clan_member(db, "10", "#AKAZA", "#GUESTCLAN")
    db.upsert_cwl_signup_sync(event_id, "#AKAZA", "Akaza", "10", None, "auto_seeded", "pending")
    db.upsert_cwl_assignment_sync(event_id, "#AKAZA", "#GUESTCLAN", assignment_source="admin_override", locked=True)

    await remove_cwl_guest_clan(307, event_id, "2026-09", "#GUESTCLAN")

    assert db.get_cwl_signup_sync(event_id, "#AKAZA") is None
    assignments = {a["player_tag"] for a in db.get_cwl_assignments_sync(event_id)}
    assert "#AKAZA" not in assignments


@pytest.mark.discord
@pytest.mark.asyncio
async def test_shared_guest_clan_remove_purges_own_member_deliberately_placed_into_own_column(db, monkeypatch):
    """2026-08-19 live bug report, project owner — the SAME symptom as the Akaza/Hohenloher-Land
    test above recurred for a genuinely cross-guild-SHARED clan (StayCalm), which the first fix's
    `shared_clan_id is not None` carve-out didn't cover: "when adding a cross-linked clan from
    another guild, then assigning one of its own members to itself and then removing that clan
    again should not leave that manually assigned player in the guild's pool... the 'assigned to
    other guild' case serves a different purpose, namely a player that is rightfully a member of
    the current player pool (e.g. because he is a member of this guild) but is assigned to another
    guild's roster — those are the players that should appear in that category."

    #BASEMENT here is a genuine CURRENT member of the shared clan itself (#CLAN1) — not a member
    of guild 100's own pool by any other measure — drag-assigned into #CLAN1's OWN column while it
    was a guest clan on guild 100's board. #CLAN1 being genuinely shared (owned by guild 200) is
    not enough on its own to preserve this: #BASEMENT was never "one of guild 100's own players
    assigned to another guild," they're simply a foreign player whose guest invitation just ended,
    same as every other ordinary member of the removed clan."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import remove_cwl_guest_clan

    monkeypatch.setattr(CACHE, "db_manager", db)
    monkeypatch.setattr(CACHE, "server_config", {"100": {"member_clans": [], "member_families": []}})
    monkeypatch.setattr(CACHE, "clan_families", {})
    shared_clan_id, owner_event_id, target_event_id = await _seed_two_shared_guilds(db, "200", "100")
    # A genuine CURRENT member of #CLAN1 (the shared clan itself), deliberately drag-assigned
    # into its own column from guild 100's own board — mirrors what a real admin drag actually
    # produces (assignment_source="admin_override", locked=True — see web_bridge.py:
    # handle_post_cwl_enrollment_assign).
    await _seed_current_clan_member(db, "10", "#BASEMENT", "#CLAN1")
    db.upsert_cwl_signup_sync(target_event_id, "#BASEMENT", "Basement", "10", None, "admin_added", "pending")
    db.upsert_cwl_assignment_sync(target_event_id, "#BASEMENT", "#CLAN1", assignment_source="admin_override", locked=True)

    await remove_cwl_guest_clan(100, target_event_id, "2026-09", "#CLAN1")

    assert db.get_cwl_signup_sync(target_event_id, "#BASEMENT") is None
    assignments = {a["player_tag"] for a in db.get_cwl_assignments_sync(target_event_id)}
    assert "#BASEMENT" not in assignments


@pytest.mark.discord
@pytest.mark.asyncio
async def test_plain_guest_clan_detach_never_deletes_a_genuine_family_members_signup(db, monkeypatch):
    """A player who is a CURRENT member of one of this guild's own family clans must never have
    their local signup deleted, even if they also happen to carry old auto_assigned/auto_seeded
    history from the guest clan (e.g. from before they moved into the family clan) — but their
    now-stale ASSIGNMENT pointing at the just-removed guest clan IS cleared (2026-08-19 fix, live
    bug report, project owner: "the qcrew members were falsely auto-assigned to staycalm... after
    removing staycalm the error becomes obvious" — real family-clan members were stuck rendering
    as "Assigned to other Guild" forever otherwise, since this cleanup already correctly refused
    to delete their pool membership but never cleared the resulting dangling pointer)."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import remove_cwl_guest_clan

    monkeypatch.setattr(CACHE, "db_manager", db)
    monkeypatch.setattr(CACHE, "server_config", {"302": {"member_clans": ["#FAMILY_CLAN"], "member_families": []}})
    monkeypatch.setattr(CACHE, "clan_families", {})
    await _seed_guild_and_clan(db, "302", "#GUESTCLAN")
    await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES ('#FAMILY_CLAN', 'Family Clan')")
    await db.conn.commit()
    event_id = db.create_cwl_event_sync("302", "2026-09", "111")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#GUESTCLAN", "participating": True}])
    # Currently a real family-clan member, but they still carry a stale, non-deliberate local
    # assignment pointing at the guest clan from before they moved into the family clan — this is
    # what makes them a candidate at all (get_current_clan_members_sync([GUESTCLAN]) alone
    # wouldn't find them, since they're no longer really a GUESTCLAN member).
    await _seed_current_clan_member(db, "10", "#REAL_SIGNUP", "#FAMILY_CLAN")
    db.upsert_cwl_signup_sync(event_id, "#REAL_SIGNUP", "Real", "10", None, "template_confirm", "confirmed")
    db.upsert_cwl_assignment_sync(event_id, "#REAL_SIGNUP", "#GUESTCLAN", assignment_source="suggested", locked=False)

    await remove_cwl_guest_clan(302, event_id, "2026-09", "#GUESTCLAN")

    signup = db.get_cwl_signup_sync(event_id, "#REAL_SIGNUP")
    assert signup is not None  # pool membership survives
    assert signup["source"] == "template_confirm"
    assert signup["status"] == "confirmed"  # their real response is untouched too
    assignments = {a["player_tag"]: a["assigned_clan_tag"] for a in db.get_cwl_assignments_sync(event_id)}
    assert "#REAL_SIGNUP" not in assignments  # the stale pointer is cleared — falls back to Unassigned


# ---------------------------------------------------------------------------
# Discord-linked-account sweep on plain-clan detach (2026-08-16 follow-up, live-testing
# feedback, project owner's spec, verbatim): "not only the staycalm members were added to The
# QCrew's player pool but also [alt accounts from completely unrelated clans, linked via the
# same Discord user]... when removing a guest clan... we always remove all the players that are
# linked to the removed clan's discord users. There is one exception... When a discord user has
# linked players either in one of the guild's member clans or in another guest clan of the guild
# then these players should remain." Follow-up refinement: protection via a FAMILY clan is
# unconditional; protection via ANOTHER active GUEST clan only holds while
# cwl_enrollment_include_all_linked_accounts is currently True (mirrors what a fresh Start
# Enrollment run would produce for that other clan right now).
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_plain_guest_clan_detach_removes_discord_linked_alt_in_unrelated_clan(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import remove_cwl_guest_clan

    monkeypatch.setattr(CACHE, "db_manager", db)
    monkeypatch.setattr(CACHE, "server_config", {"303": {"member_clans": [], "member_families": []}})
    monkeypatch.setattr(CACHE, "clan_families", {})
    await _seed_guild_and_clan(db, "303", "#GUESTCLAN")
    await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES ('#UNRELATED', 'Unrelated Clan')")
    await db.conn.commit()
    event_id = db.create_cwl_event_sync("303", "2026-09", "111")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#GUESTCLAN", "participating": True}])
    # #P1 is a direct current member of the guest clan; #P2 is the SAME discord user's alt
    # account, currently in a totally unrelated clan — neither family nor another guest clan.
    await _seed_current_clan_member(db, "50", "#P1", "#GUESTCLAN")
    await _seed_current_clan_member(db, "50", "#P2", "#UNRELATED")
    db.upsert_cwl_signup_sync(event_id, "#P1", "P1", "50", None, "template_confirm", "pending")
    db.upsert_cwl_signup_sync(event_id, "#P2", "P2", "50", None, "template_confirm", "pending")

    await remove_cwl_guest_clan(303, event_id, "2026-09", "#GUESTCLAN")

    assert db.get_cwl_signup_sync(event_id, "#P1") is None
    assert db.get_cwl_signup_sync(event_id, "#P2") is None  # the alt gets swept too


@pytest.mark.discord
@pytest.mark.asyncio
async def test_plain_guest_clan_detach_keeps_discord_linked_alt_in_family_clan_unconditionally(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import remove_cwl_guest_clan

    monkeypatch.setattr(CACHE, "db_manager", db)
    # No cwl_enrollment_include_all_linked_accounts set at all — family protection must not
    # depend on it.
    monkeypatch.setattr(CACHE, "server_config", {"304": {"member_clans": ["#FAMILY_CLAN"], "member_families": []}})
    monkeypatch.setattr(CACHE, "clan_families", {})
    await _seed_guild_and_clan(db, "304", "#GUESTCLAN")
    await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES ('#FAMILY_CLAN', 'Family Clan')")
    await db.conn.commit()
    event_id = db.create_cwl_event_sync("304", "2026-09", "111")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#GUESTCLAN", "participating": True}])
    # #P1 is a direct guest-clan member; #P2 is the same discord user's OTHER account, currently
    # a genuine family-clan member.
    await _seed_current_clan_member(db, "60", "#P1", "#GUESTCLAN")
    await _seed_current_clan_member(db, "60", "#P2", "#FAMILY_CLAN")
    db.upsert_cwl_signup_sync(event_id, "#P1", "P1", "60", None, "template_confirm", "pending")
    db.upsert_cwl_signup_sync(event_id, "#P2", "P2", "60", None, "template_confirm", "pending")

    await remove_cwl_guest_clan(304, event_id, "2026-09", "#GUESTCLAN")

    # BOTH survive — the whole linked account is protected once any of its players is a genuine
    # family-clan member, unconditionally.
    assert db.get_cwl_signup_sync(event_id, "#P1") is not None
    assert db.get_cwl_signup_sync(event_id, "#P2") is not None


@pytest.mark.discord
@pytest.mark.asyncio
async def test_plain_guest_clan_detach_keeps_discord_linked_alt_in_other_guest_clan_when_toggle_on(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import remove_cwl_guest_clan

    monkeypatch.setattr(CACHE, "db_manager", db)
    monkeypatch.setattr(CACHE, "server_config", {
        "305": {"member_clans": [], "member_families": [], "cwl_enrollment_include_all_linked_accounts": True},
    })
    monkeypatch.setattr(CACHE, "clan_families", {})
    await _seed_guild_and_clan(db, "305", "#GUESTCLAN")
    await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES ('#OTHERGUEST', 'Other Guest Clan')")
    await db.conn.commit()
    event_id = db.create_cwl_event_sync("305", "2026-09", "111")
    # #OTHERGUEST stays active; #GUESTCLAN is the one being deactivated (matches the real
    # handle_post_clan_config ordering — cwl_event_clans is persisted before detach runs).
    db.set_cwl_event_clans_sync(event_id, [
        {"clan_tag": "#GUESTCLAN", "participating": False},
        {"clan_tag": "#OTHERGUEST", "participating": True},
    ])
    await _seed_current_clan_member(db, "70", "#P1", "#GUESTCLAN")
    await _seed_current_clan_member(db, "70", "#P2", "#OTHERGUEST")
    db.upsert_cwl_signup_sync(event_id, "#P1", "P1", "70", None, "template_confirm", "pending")
    db.upsert_cwl_signup_sync(event_id, "#P2", "P2", "70", None, "template_confirm", "pending")

    await remove_cwl_guest_clan(305, event_id, "2026-09", "#GUESTCLAN")

    # BOTH survive: #P2 is a direct #OTHERGUEST member (unconditional); #P1 is only linked, but
    # the expansion toggle is currently on, so it's protected too.
    assert db.get_cwl_signup_sync(event_id, "#P1") is not None
    assert db.get_cwl_signup_sync(event_id, "#P2") is not None


@pytest.mark.discord
@pytest.mark.asyncio
async def test_plain_guest_clan_detach_removes_discord_linked_alt_in_other_guest_clan_when_toggle_off(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import remove_cwl_guest_clan

    monkeypatch.setattr(CACHE, "db_manager", db)
    # cwl_enrollment_include_all_linked_accounts left off (default False).
    monkeypatch.setattr(CACHE, "server_config", {"306": {"member_clans": [], "member_families": []}})
    monkeypatch.setattr(CACHE, "clan_families", {})
    await _seed_guild_and_clan(db, "306", "#GUESTCLAN")
    await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES ('#OTHERGUEST', 'Other Guest Clan')")
    await db.conn.commit()
    event_id = db.create_cwl_event_sync("306", "2026-09", "111")
    db.set_cwl_event_clans_sync(event_id, [
        {"clan_tag": "#GUESTCLAN", "participating": False},
        {"clan_tag": "#OTHERGUEST", "participating": True},
    ])
    await _seed_current_clan_member(db, "80", "#P1", "#GUESTCLAN")
    await _seed_current_clan_member(db, "80", "#P2", "#OTHERGUEST")
    db.upsert_cwl_signup_sync(event_id, "#P1", "P1", "80", None, "template_confirm", "pending")
    db.upsert_cwl_signup_sync(event_id, "#P2", "P2", "80", None, "template_confirm", "pending")

    await remove_cwl_guest_clan(306, event_id, "2026-09", "#GUESTCLAN")

    # #P2 still survives (direct #OTHERGUEST membership is unconditional), but #P1 — only ever
    # linked, never a direct #OTHERGUEST member — is removed since the expansion toggle is off.
    assert db.get_cwl_signup_sync(event_id, "#P1") is None
    assert db.get_cwl_signup_sync(event_id, "#P2") is not None
