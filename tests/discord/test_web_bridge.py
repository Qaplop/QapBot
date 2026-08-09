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
