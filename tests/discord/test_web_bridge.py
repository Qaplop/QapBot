"""Tests for the CWL clan-config web bridge (CWL_CLAN_CONFIG_ACTIVITY_PLAN.md Phase B):
the shared-secret gate, the guild-admin re-verification (including the configured
super-admin bypass), and the GET/POST clan-config endpoints' actual behavior.
"""
from __future__ import annotations

import dataclasses
import os
from typing import Dict
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

os.environ.setdefault("DISCORD_TOKEN", "test-token")

from qapbot.config import CONFIG
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


async def _seed_guild_and_clans(db: WarHistoryDB, guild_id: str, clan_tags: Dict[str, str]) -> None:
    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
    for tag, name in clan_tags.items():
        await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES (?, ?)", (tag, name))
    await db.conn.commit()


@pytest.fixture
def bridge_config(monkeypatch):
    config = dataclasses.replace(CONFIG, web_bridge_secret="test-secret", web_bridge_port=1)
    monkeypatch.setattr("qapbot.config.CONFIG", config)
    return config


@pytest.fixture
async def client():
    from qapbot.web_bridge import create_app

    async with TestClient(TestServer(create_app())) as c:
        yield c


def _fake_admin_bot(guild_id: int, discord_user_id: int, is_admin: bool = True):
    """Fake QBcore.bot whose guild has exactly one resolvable member (discord_user_id) with
    guild_permissions.administrator set as requested."""
    member = MagicMock()
    member.guild_permissions.administrator = is_admin

    guild = MagicMock()
    guild.get_member = MagicMock(return_value=member)
    guild.fetch_member = AsyncMock(return_value=member)

    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)
    return bot


def _fake_role_bot(guild_id: int, discord_user_id: int, role_ids):
    """Fake QBcore.bot whose guild has exactly one resolvable, non-admin member holding
    the given Discord role IDs — for testing the Leader/Co-Leader path independent of
    _resolve_admin()'s own guild-admin/super-admin checks."""
    member = MagicMock()
    member.guild_permissions.administrator = False
    member.roles = [MagicMock(id=rid) for rid in role_ids]

    guild = MagicMock()
    guild.get_member = MagicMock(return_value=member)
    guild.fetch_member = AsyncMock(return_value=member)

    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)
    return bot


@pytest.mark.discord
@pytest.mark.asyncio
async def test_resolve_admin_or_leader_allows_guild_admin(monkeypatch):
    from qapbot.web_bridge import _resolve_admin_or_leader
    import QBcore

    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(1, 2, is_admin=True))
    assert await _resolve_admin_or_leader(1, 2) is True


@pytest.mark.discord
@pytest.mark.asyncio
async def test_resolve_admin_or_leader_allows_leader_role_holder(monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.web_bridge import _resolve_admin_or_leader
    import QBcore

    monkeypatch.setattr(QBcore, "bot", _fake_role_bot(1, 2, role_ids=[1001]))
    CACHE.server_config["1"] = {"coc_role_leader_id": "1001"}

    assert await _resolve_admin_or_leader(1, 2) is True


@pytest.mark.discord
@pytest.mark.asyncio
async def test_resolve_admin_or_leader_allows_coleader_role_holder(monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.web_bridge import _resolve_admin_or_leader
    import QBcore

    monkeypatch.setattr(QBcore, "bot", _fake_role_bot(1, 2, role_ids=[1002]))
    CACHE.server_config["1"] = {"coc_role_coleader_id": "1002"}

    assert await _resolve_admin_or_leader(1, 2) is True


@pytest.mark.discord
@pytest.mark.asyncio
async def test_resolve_admin_or_leader_rejects_regular_member(monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.web_bridge import _resolve_admin_or_leader
    import QBcore

    monkeypatch.setattr(QBcore, "bot", _fake_role_bot(1, 2, role_ids=[9999]))  # unrelated role
    CACHE.server_config["1"] = {"coc_role_leader_id": "1001", "coc_role_coleader_id": "1002"}

    assert await _resolve_admin_or_leader(1, 2) is False


@pytest.mark.discord
@pytest.mark.asyncio
async def test_resolve_admin_or_leader_rejects_when_guild_unresolvable(monkeypatch):
    from qapbot.web_bridge import _resolve_admin_or_leader
    import QBcore

    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=None)
    monkeypatch.setattr(QBcore, "bot", bot)

    assert await _resolve_admin_or_leader(1, 2) is False


@pytest.mark.discord
@pytest.mark.asyncio
async def test_health_does_not_require_secret(client):
    resp = await client.get("/api/health")
    assert resp.status == 200
    assert (await resp.json())["ok"] is True


@pytest.mark.discord
@pytest.mark.asyncio
async def test_clan_config_get_rejects_missing_secret(bridge_config, client):
    resp = await client.get("/api/cwl/clan-config", params={"guild_id": "1", "discord_user_id": "2"})
    assert resp.status == 403


@pytest.mark.discord
@pytest.mark.asyncio
async def test_clan_config_get_rejects_wrong_secret(bridge_config, client):
    resp = await client.get(
        "/api/cwl/clan-config",
        params={"guild_id": "1", "discord_user_id": "2"},
        headers={"X-Bridge-Secret": "wrong"},
    )
    assert resp.status == 403


@pytest.mark.discord
@pytest.mark.asyncio
async def test_clan_config_get_rejects_non_admin(bridge_config, client, monkeypatch):
    import QBcore

    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(1, 2, is_admin=False))

    resp = await client.get(
        "/api/cwl/clan-config",
        params={"guild_id": "1", "discord_user_id": "2"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 403


@pytest.mark.discord
@pytest.mark.asyncio
async def test_clan_config_get_super_admin_bypasses_guild_lookup(monkeypatch):
    """The configured single super-admin (CONFIG.server_admin) is treated as admin of every
    guild without ever needing a resolvable guild/member — mirrors
    QBdiscocmdshelper._is_configured_admin()'s numeric-ID fast path."""
    from qapbot.web_bridge import create_app

    config = dataclasses.replace(CONFIG, web_bridge_secret="test-secret", web_bridge_port=1, server_admin="999")
    monkeypatch.setattr("qapbot.config.CONFIG", config)

    import QBcore
    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=None)  # guild not even resolvable
    monkeypatch.setattr(QBcore, "bot", bot)

    from qapbot.cache_manager import CACHE
    CACHE.server_config["1"] = {"member_clans": [], "member_families": []}
    CACHE.db_manager = None

    async with TestClient(TestServer(create_app())) as client:
        resp = await client.get(
            "/api/cwl/clan-config",
            params={"guild_id": "1", "discord_user_id": "999"},
            headers={"X-Bridge-Secret": "test-secret"},
        )
    assert resp.status == 200


@pytest.mark.discord
@pytest.mark.asyncio
async def test_clan_config_get_returns_payload_for_admin(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_current_cwl_season

    await _seed_guild_and_clans(db, "555", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha", "war_league": "Crystal League I"}}
    CACHE.server_config["555"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(555, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/clan-config",
        params={"guild_id": "555", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    # No persisted cwl_selected_season and no event yet -> resolve_selected_cwl_season() falls
    # all the way back to the calendar default (Phase E.3's resolution chain).
    season = resolve_current_cwl_season()
    assert body["season"] == season
    # No cwl_event_clans row yet for this clan -> season-aware defaults, not nulls (Phase E.2):
    # roster_size 15, start time the 1st of the season's month at 08:00 UTC.
    assert body["clans"] == [{
        "clan_tag": "#CLAN1",
        "name": "Alpha",
        "tier": "Crystal League I",
        "participating": False,
        "roster_size": 15,
        "cwl_start_at": f"{season}-01T08:00Z",
    }]


@pytest.mark.discord
@pytest.mark.asyncio
async def test_clan_config_post_persists_and_refreshes_hub(db, bridge_config, client, monkeypatch):
    """POST never creates a season itself (Phase E.3 — that's exclusively "Add New Season"'s
    job in Discord), so the test seeds the event first, simulating that having already run."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_current_cwl_season

    await _seed_guild_and_clans(db, "666", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha", "war_league": "Master League II"}}
    CACHE.server_config["666"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    season = resolve_current_cwl_season()
    db.create_cwl_event_sync("666", season, "discordid1")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(666, 42, is_admin=True))

    refresh_mock = AsyncMock()
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", refresh_mock)

    resp = await client.post(
        "/api/cwl/clan-config",
        json={
            "guild_id": 666,
            "discord_user_id": 42,
            "clans": [{"clan_tag": "#CLAN1", "participating": True, "roster_size": 30, "cwl_start_at": "2026-09-01T08:00Z"}],
        },
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True

    event = db.get_cwl_event_sync("666", season)
    assert event is not None
    clans = db.get_cwl_event_clans_sync(event["id"])
    assert len(clans) == 1
    assert clans[0]["clan_tag"] == "#CLAN1"
    assert clans[0]["roster_size"] == 30
    assert clans[0]["cwl_start_at"] == "2026-09-01T08:00Z"
    assert clans[0]["target_league_rank"] == "Master League II"

    refresh_mock.assert_awaited_once_with(666, "cwl_management")


@pytest.mark.discord
@pytest.mark.asyncio
async def test_clan_config_post_rejects_when_no_season_exists_yet(db, bridge_config, client, monkeypatch):
    """Without a prior "Add New Season" (i.e. no cwl_events row for the resolved season), POST
    must refuse rather than silently creating one — Phase E.3's explicit instruction that only
    "Add New Season" ever creates a season."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "1010", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}}
    CACHE.server_config["1010"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(1010, 42, is_admin=True))

    resp = await client.post(
        "/api/cwl/clan-config",
        json={
            "guild_id": 1010,
            "discord_user_id": 42,
            "clans": [{"clan_tag": "#CLAN1", "participating": True, "roster_size": 30, "cwl_start_at": "2026-09-01T08:00Z"}],
        },
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 409
    assert db.list_cwl_events_sync("1010") == []


@pytest.mark.discord
@pytest.mark.asyncio
async def test_deactivating_clan_via_bridge_preserves_settings(db, bridge_config, client, monkeypatch):
    """Regression guard, exercised through the actual GET/POST endpoints (not just the DB
    layer): deactivate a clan that has custom roster_size/cwl_start_at, confirm GET still
    reports those values (participating=False), then reactivate and confirm they're still
    there — the exact bug the project owner found live in the Activity."""
    from qapbot.cache_manager import CACHE

    from qapbot.QBdiscocmdshelper_cwl import resolve_current_cwl_season

    await _seed_guild_and_clans(db, "777", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha", "war_league": "Master League II"}}
    CACHE.server_config["777"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}
    db.create_cwl_event_sync("777", resolve_current_cwl_season(), "discordid1")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(777, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    headers = {"X-Bridge-Secret": "test-secret"}

    # 1. Activate with custom settings.
    await client.post(
        "/api/cwl/clan-config",
        json={"guild_id": 777, "discord_user_id": 42, "clans": [
            {"clan_tag": "#CLAN1", "participating": True, "roster_size": 30, "cwl_start_at": "2026-09-01T08:00Z"},
        ]},
        headers=headers,
    )

    # 2. Deactivate — settings must be echoed back, not reset to defaults.
    await client.post(
        "/api/cwl/clan-config",
        json={"guild_id": 777, "discord_user_id": 42, "clans": [
            {"clan_tag": "#CLAN1", "participating": False, "roster_size": 30, "cwl_start_at": "2026-09-01T08:00Z"},
        ]},
        headers=headers,
    )
    resp = await client.get("/api/cwl/clan-config", params={"guild_id": "777", "discord_user_id": "42"}, headers=headers)
    body = await resp.json()
    clan = body["clans"][0]
    assert clan["participating"] is False
    assert clan["roster_size"] == 30
    assert clan["cwl_start_at"] == "2026-09-01T08:00Z"

    # 3. Reactivate — same settings, not reset to the 15/None defaults.
    await client.post(
        "/api/cwl/clan-config",
        json={"guild_id": 777, "discord_user_id": 42, "clans": [
            {"clan_tag": "#CLAN1", "participating": True, "roster_size": 30, "cwl_start_at": "2026-09-01T08:00Z"},
        ]},
        headers=headers,
    )
    resp = await client.get("/api/cwl/clan-config", params={"guild_id": "777", "discord_user_id": "42"}, headers=headers)
    body = await resp.json()
    clan = body["clans"][0]
    assert clan["participating"] is True
    assert clan["roster_size"] == 30
    assert clan["cwl_start_at"] == "2026-09-01T08:00Z"


# ---------------------------------------------------------------------------
# Season resolution and tier sorting (Phase E) — season selection itself now lives entirely on
# the Discord-side CWL Management screen; the bridge just reads the persisted selection.
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_clan_config_get_honors_persisted_selected_season(db, bridge_config, client, monkeypatch):
    """GET reflects guild_config.cwl_selected_season (set by the Discord-side season select),
    not whatever get_current_cwl_event_sync() would otherwise resolve to."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "888", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha", "war_league": "Master League II"}}
    CACHE.server_config["888"] = {
        "member_clans": ["#CLAN1"], "member_families": [], "cwl_selected_season": "2026-06",
    }
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    # A newer/"current" event also exists — the persisted selection must still win.
    db.create_cwl_event_sync("888", "2026-08", "discordid1")
    event_id = db.create_cwl_event_sync("888", "2026-06", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [
        {"clan_tag": "#CLAN1", "roster_size": 5, "cwl_start_at": "2026-06-01T08:00Z", "participating": True},
    ])

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(888, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/clan-config",
        params={"guild_id": "888", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    body = await resp.json()
    assert body["season"] == "2026-06"
    assert body["clans"][0]["roster_size"] == 5
    assert body["clans"][0]["cwl_start_at"] == "2026-06-01T08:00Z"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_clan_config_get_sorts_clans_by_tier_highest_first(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "1212", {"#CLAN1": "Bronze Clan", "#CLAN2": "Champion Clan", "#CLAN3": "No Tier Clan"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {
        "#CLAN1": {"name": "Bronze Clan", "war_league": "Bronze League III"},
        "#CLAN2": {"name": "Champion Clan", "war_league": "Champion League I"},
        "#CLAN3": {"name": "No Tier Clan"},  # never synced -> unknown, sorts last
    }
    CACHE.server_config["1212"] = {"member_clans": ["#CLAN1", "#CLAN2", "#CLAN3"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(1212, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/clan-config",
        params={"guild_id": "1212", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    body = await resp.json()
    assert [c["clan_tag"] for c in body["clans"]] == ["#CLAN2", "#CLAN1", "#CLAN3"]


@pytest.mark.discord
@pytest.mark.asyncio
async def test_clan_config_post_targets_the_selected_season(db, bridge_config, client, monkeypatch):
    """POST must write to the season currently selected in guild_config, not whichever event
    happens to be "current" — mirrors the GET-side persisted-selection test above."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "333", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha", "war_league": "Master League II"}}
    CACHE.server_config["333"] = {
        "member_clans": ["#CLAN1"], "member_families": [], "cwl_selected_season": "2026-11",
    }
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    db.create_cwl_event_sync("333", "2026-09", "discordid1")  # a different, "current" event
    db.create_cwl_event_sync("333", "2026-11", "discordid1")  # the selected one

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(333, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    resp = await client.post(
        "/api/cwl/clan-config",
        json={
            "guild_id": 333,
            "discord_user_id": 42,
            "clans": [{"clan_tag": "#CLAN1", "participating": True, "roster_size": 30, "cwl_start_at": "2026-11-01T08:00Z"}],
        },
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200

    selected_event = db.get_cwl_event_sync("333", "2026-11")
    assert db.get_cwl_event_clans_sync(selected_event["id"])[0]["roster_size"] == 30

    other_event = db.get_cwl_event_sync("333", "2026-09")
    assert db.get_cwl_event_clans_sync(other_event["id"]) == []


async def _seed_current_clan_member(db: WarHistoryDB, discord_id: str, player_tag: str, clan_tag: str, verified: bool = True) -> None:
    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES (?, ?)", (discord_id, discord_id))
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag) VALUES (?, ?, ?, ?, ?)",
        (discord_id, player_tag, "Player", 1 if verified else 0, clan_tag),
    )
    await db.conn.commit()


# ---------------------------------------------------------------------------
# GET /api/cwl/screen — "Manage Enrollment" pending-screen picker (2026-08-10)
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_screen_get_pops_recorded_value(bridge_config, client):
    from qapbot.cache_manager import CACHE

    CACHE.pending_cwl_activity_screen[("1", "2")] = "enrollment"

    resp = await client.get(
        "/api/cwl/screen",
        params={"guild_id": "1", "discord_user_id": "2"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    assert (await resp.json())["screen"] == "enrollment"
    # Popped — gone after being read once.
    assert ("1", "2") not in CACHE.pending_cwl_activity_screen


@pytest.mark.discord
@pytest.mark.asyncio
async def test_screen_get_defaults_to_clan_config_when_nothing_recorded(bridge_config, client):
    from qapbot.cache_manager import CACHE

    CACHE.pending_cwl_activity_screen.pop(("3", "4"), None)

    resp = await client.get(
        "/api/cwl/screen",
        params={"guild_id": "3", "discord_user_id": "4"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    assert (await resp.json())["screen"] == "clan_config"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_screen_get_rejects_wrong_secret(bridge_config, client):
    resp = await client.get(
        "/api/cwl/screen",
        params={"guild_id": "1", "discord_user_id": "2"},
        headers={"X-Bridge-Secret": "wrong"},
    )
    assert resp.status == 403


@pytest.mark.discord
@pytest.mark.asyncio
async def test_screen_get_rejects_missing_params(bridge_config, client):
    resp = await client.get(
        "/api/cwl/screen",
        params={"guild_id": "1"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 400


# ---------------------------------------------------------------------------
# GET /api/cwl/enrollment — the Manage Enrollment board payload
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_get_returns_merged_players_and_clans(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_current_cwl_season

    await _seed_guild_and_clans(db, "777", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha", "war_league": "Crystal League I"}}
    CACHE.server_config["777"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    season = resolve_current_cwl_season()
    event_id = db.create_cwl_event_sync("777", season, "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])

    # #P1 already has a signup row (pending) and an assignment; #P2 is a current member with
    # neither yet — must still show up, ready for a 1-click sign-up.
    db.upsert_cwl_signup_sync(event_id, "#P1", "Alpha1", "10", None, "template_confirm", "pending")
    await _seed_current_clan_member(db, "10", "#P1", "#CLAN1")
    await _seed_current_clan_member(db, "11", "#P2", "#CLAN1")
    db.upsert_cwl_assignment_sync(event_id, "#P1", "#CLAN1")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(777, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/enrollment",
        params={"guild_id": "777", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["season"] == season
    assert body["event_status"] == "draft"
    assert body["clans"] == [{"clan_tag": "#CLAN1", "name": "Alpha", "tier": "Crystal League I"}]

    players_by_tag = {p["player_tag"]: p for p in body["players"]}
    assert players_by_tag["#P1"]["signup_status"] == "pending"
    assert players_by_tag["#P1"]["assigned_clan_tag"] == "#CLAN1"
    assert players_by_tag["#P2"]["signup_status"] is None
    assert players_by_tag["#P2"]["assigned_clan_tag"] is None


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_get_no_event_returns_empty(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "778", {})
    CACHE.db_manager = db
    CACHE.server_config["778"] = {"member_clans": [], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(778, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/enrollment",
        params={"guild_id": "778", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["event_status"] is None
    assert body["clans"] == []
    assert body["players"] == []


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_get_allows_leader_role_holder(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "779", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["779"] = {"member_clans": ["#CLAN1"], "member_families": [], "coc_role_leader_id": "1001"}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}
    db.create_cwl_event_sync("779", "2026-09", "discordid1")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_role_bot(779, 50, role_ids=[1001]))

    resp = await client.get(
        "/api/cwl/enrollment",
        params={"guild_id": "779", "discord_user_id": "50"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_get_rejects_regular_member(bridge_config, client, monkeypatch):
    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(1, 2, is_admin=False))

    resp = await client.get(
        "/api/cwl/enrollment",
        params={"guild_id": "1", "discord_user_id": "2"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 403


# ---------------------------------------------------------------------------
# POST /api/cwl/enrollment/signup — 1-click confirm/withdraw
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_signup_confirms_existing_row(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "780", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["780"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    event_id = db.create_cwl_event_sync("780", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    db.upsert_cwl_signup_sync(event_id, "#P1", "Alpha1", "10", None, "template_confirm", "pending")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(780, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    resp = await client.post(
        "/api/cwl/enrollment/signup",
        json={"guild_id": 780, "discord_user_id": 42, "player_tag": "#P1", "action": "confirm"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    signup = db.get_cwl_signup_sync(event_id, "#P1")
    assert signup["status"] == "confirmed"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_signup_creates_row_for_new_current_member(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "781", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["781"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    event_id = db.create_cwl_event_sync("781", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    await _seed_current_clan_member(db, "20", "#P2", "#CLAN1")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(781, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    resp = await client.post(
        "/api/cwl/enrollment/signup",
        json={"guild_id": 781, "discord_user_id": 42, "player_tag": "#P2", "action": "confirm"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    signup = db.get_cwl_signup_sync(event_id, "#P2")
    assert signup is not None
    assert signup["status"] == "confirmed"
    assert signup["discord_id"] == "20"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_signup_unknown_player_returns_404(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "782", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["782"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    event_id = db.create_cwl_event_sync("782", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(782, 42, is_admin=True))

    resp = await client.post(
        "/api/cwl/enrollment/signup",
        json={"guild_id": 782, "discord_user_id": 42, "player_tag": "#NEVER", "action": "confirm"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 404


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_signup_withdraw_cascades_unassign(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "783", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["783"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    event_id = db.create_cwl_event_sync("783", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    db.upsert_cwl_signup_sync(event_id, "#P1", "Alpha1", "10", None, "template_confirm", "confirmed")
    db.upsert_cwl_assignment_sync(event_id, "#P1", "#CLAN1")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(783, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    resp = await client.post(
        "/api/cwl/enrollment/signup",
        json={"guild_id": 783, "discord_user_id": 42, "player_tag": "#P1", "action": "withdraw"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    signup = db.get_cwl_signup_sync(event_id, "#P1")
    assert signup["status"] == "withdrawn"
    assert db.get_cwl_assignments_sync(event_id) == []


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_signup_no_event_returns_409(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "784", {})
    CACHE.db_manager = db
    CACHE.server_config["784"] = {"member_clans": [], "member_families": []}

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(784, 42, is_admin=True))

    resp = await client.post(
        "/api/cwl/enrollment/signup",
        json={"guild_id": 784, "discord_user_id": 42, "player_tag": "#P1", "action": "confirm"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 409


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_signup_rejects_non_admin_non_leader(bridge_config, client, monkeypatch):
    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(1, 2, is_admin=False))

    resp = await client.post(
        "/api/cwl/enrollment/signup",
        json={"guild_id": 1, "discord_user_id": 2, "player_tag": "#P1", "action": "confirm"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 403


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_signup_rejects_bad_action(bridge_config, client, monkeypatch):
    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(1, 2, is_admin=True))

    resp = await client.post(
        "/api/cwl/enrollment/signup",
        json={"guild_id": 1, "discord_user_id": 2, "player_tag": "#P1", "action": "bogus"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 400


# ---------------------------------------------------------------------------
# POST /api/cwl/enrollment/assign — drag-and-drop move
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_assign_upserts_assignment(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "785", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["785"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    event_id = db.create_cwl_event_sync("785", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(785, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    resp = await client.post(
        "/api/cwl/enrollment/assign",
        json={"guild_id": 785, "discord_user_id": 42, "player_tag": "#P1", "clan_tag": "#CLAN1"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    assignments = db.get_cwl_assignments_sync(event_id)
    assert len(assignments) == 1
    assert assignments[0]["assigned_clan_tag"] == "#CLAN1"
    assert assignments[0]["assignment_source"] == "admin_override"
    assert assignments[0]["locked"] == 1


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_assign_null_clan_tag_unassigns(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "786", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["786"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    event_id = db.create_cwl_event_sync("786", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    db.upsert_cwl_assignment_sync(event_id, "#P1", "#CLAN1")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(786, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    resp = await client.post(
        "/api/cwl/enrollment/assign",
        json={"guild_id": 786, "discord_user_id": 42, "player_tag": "#P1", "clan_tag": None},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    assert db.get_cwl_assignments_sync(event_id) == []


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_assign_no_event_returns_409(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "787", {})
    CACHE.db_manager = db
    CACHE.server_config["787"] = {"member_clans": [], "member_families": []}

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(787, 42, is_admin=True))

    resp = await client.post(
        "/api/cwl/enrollment/assign",
        json={"guild_id": 787, "discord_user_id": 42, "player_tag": "#P1", "clan_tag": "#CLAN1"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 409


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_assign_rejects_non_admin_non_leader(bridge_config, client, monkeypatch):
    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(1, 2, is_admin=False))

    resp = await client.post(
        "/api/cwl/enrollment/assign",
        json={"guild_id": 1, "discord_user_id": 2, "player_tag": "#P1", "clan_tag": "#CLAN1"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 403
