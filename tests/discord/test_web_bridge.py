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
    season = resolve_current_cwl_season()
    assert body["season"] == season
    assert body["has_own_data"] is False
    assert body["carry_over_available"] is False
    assert body["previous_clans"] is None
    assert season in body["available_seasons"]
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
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "666", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha", "war_league": "Master League II"}}
    CACHE.server_config["666"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

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

    from qapbot.QBdiscocmdshelper_cwl import resolve_current_cwl_season
    event = db.get_cwl_event_sync("666", resolve_current_cwl_season())
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
async def test_deactivating_clan_via_bridge_preserves_settings(db, bridge_config, client, monkeypatch):
    """Regression guard, exercised through the actual GET/POST endpoints (not just the DB
    layer): deactivate a clan that has custom roster_size/cwl_start_at, confirm GET still
    reports those values (participating=False), then reactivate and confirm they're still
    there — the exact bug the project owner found live in the Activity."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "777", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha", "war_league": "Master League II"}}
    CACHE.server_config["777"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

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
# Season selection, carry-over detection, season-aware defaults (Phase E)
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_clan_config_get_honors_explicit_season_param(db, bridge_config, client, monkeypatch):
    """A season param picks that season's own event/clans, not whatever
    get_current_cwl_event_sync() would otherwise resolve to."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "888", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha", "war_league": "Master League II"}}
    CACHE.server_config["888"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    event_id = db.create_cwl_event_sync("888", "2026-06", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [
        {"clan_tag": "#CLAN1", "roster_size": 5, "cwl_start_at": "2026-06-01T08:00Z", "participating": True},
    ])

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(888, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/clan-config",
        params={"guild_id": "888", "discord_user_id": "42", "season": "2026-06"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    body = await resp.json()
    assert body["season"] == "2026-06"
    assert body["has_own_data"] is True
    assert body["clans"][0]["roster_size"] == 5
    assert body["clans"][0]["cwl_start_at"] == "2026-06-01T08:00Z"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_clan_config_get_lists_available_seasons(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_current_cwl_season

    await _seed_guild_and_clans(db, "999", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}}
    CACHE.server_config["999"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    db.create_cwl_event_sync("999", "2026-05", "discordid1")
    db.create_cwl_event_sync("999", "2026-06", "discordid1")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(999, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/clan-config",
        params={"guild_id": "999", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    body = await resp.json()
    assert {"2026-05", "2026-06", resolve_current_cwl_season()} <= set(body["available_seasons"])
    # Newest first.
    assert body["available_seasons"] == sorted(body["available_seasons"], reverse=True)


@pytest.mark.discord
@pytest.mark.asyncio
async def test_clan_config_get_offers_carry_over_when_new_season_has_no_data(db, bridge_config, client, monkeypatch):
    """A season with no cwl_event_clans rows of its own yet, but a previous season with
    participating clans, should surface that previous data as an explicit opt-in choice
    (carry_over_available/carry_over_season/previous_clans) — never silently pre-applied to
    `clans` itself, which must stay at the plain season-aware defaults."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "111", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha", "war_league": "Master League II"}}
    CACHE.server_config["111"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    old_event_id = db.create_cwl_event_sync("111", "2026-06", "discordid1")
    db.set_cwl_event_clans_sync(old_event_id, [
        {"clan_tag": "#CLAN1", "roster_size": 30, "cwl_start_at": "2026-06-01T08:00Z", "participating": True},
    ])

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(111, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/clan-config",
        params={"guild_id": "111", "discord_user_id": "42", "season": "2026-07"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    body = await resp.json()
    assert body["season"] == "2026-07"
    assert body["has_own_data"] is False
    assert body["carry_over_available"] is True
    assert body["carry_over_season"] == "2026-06"
    assert body["previous_clans"] == [
        {"clan_tag": "#CLAN1", "roster_size": 30, "cwl_start_at": "2026-06-01T08:00Z"}
    ]
    # The plain `clans` array stays at defaults — carry-over is opt-in, not auto-applied.
    assert body["clans"][0]["participating"] is False
    assert body["clans"][0]["roster_size"] == 15
    assert body["clans"][0]["cwl_start_at"] == "2026-07-01T08:00Z"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_clan_config_get_no_carry_over_once_season_has_own_data(db, bridge_config, client, monkeypatch):
    """Once a season has its own saved clans, it's no longer a carry-over candidate — even if
    an earlier season also has data."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "222", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}}
    CACHE.server_config["222"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    old_event_id = db.create_cwl_event_sync("222", "2026-06", "discordid1")
    db.set_cwl_event_clans_sync(old_event_id, [
        {"clan_tag": "#CLAN1", "roster_size": 30, "cwl_start_at": "2026-06-01T08:00Z", "participating": True},
    ])
    new_event_id = db.create_cwl_event_sync("222", "2026-07", "discordid1")
    db.set_cwl_event_clans_sync(new_event_id, [
        {"clan_tag": "#CLAN1", "roster_size": 5, "cwl_start_at": "2026-07-01T08:00Z", "participating": True},
    ])

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(222, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/clan-config",
        params={"guild_id": "222", "discord_user_id": "42", "season": "2026-07"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    body = await resp.json()
    assert body["has_own_data"] is True
    assert body["carry_over_available"] is False
    assert body["previous_clans"] is None
    assert body["clans"][0]["roster_size"] == 5


@pytest.mark.discord
@pytest.mark.asyncio
async def test_clan_config_post_persists_to_explicit_season(db, bridge_config, client, monkeypatch):
    """POST honors an explicit `season` in the body, so a save made while the dropdown has a
    non-default season selected lands in that season's event, not always resolve_current_cwl_season()."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "333", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha", "war_league": "Master League II"}}
    CACHE.server_config["333"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(333, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    resp = await client.post(
        "/api/cwl/clan-config",
        json={
            "guild_id": 333,
            "discord_user_id": 42,
            "season": "2026-11",
            "clans": [{"clan_tag": "#CLAN1", "participating": True, "roster_size": 30, "cwl_start_at": "2026-11-01T08:00Z"}],
        },
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200

    event = db.get_cwl_event_sync("333", "2026-11")
    assert event is not None
    clans = db.get_cwl_event_clans_sync(event["id"])
    assert clans[0]["roster_size"] == 30
