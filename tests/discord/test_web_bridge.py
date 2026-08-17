"""Tests for the CWL clan-config web bridge (CWL_CLAN_CONFIG_ACTIVITY_PLAN.md Phase B):
the shared-secret gate, the guild-admin re-verification (including the configured
super-admin bypass), and the GET/POST clan-config endpoints' actual behavior.
"""
from __future__ import annotations

import asyncio
import dataclasses
import os
import threading
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


# ---------------------------------------------------------------------------
# _access_log_middleware — replaces aiohttp's default access log with one naming the
# guild/user instead of raw IDs (2026-08-14, live-testing feedback)
# ---------------------------------------------------------------------------

def test_bridge_log_label_resolves_name_from_bot_cache(monkeypatch):
    from qapbot.web_bridge import _bridge_log_label
    import QBcore

    guild = MagicMock()
    guild.name = "The QCrew"
    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)
    monkeypatch.setattr(QBcore, "bot", bot)

    assert _bridge_log_label("guild", "1145641080621109312") == "The QCrew (1145641080621109312)"


def test_bridge_log_label_falls_back_to_bare_id_when_unresolvable(monkeypatch):
    from qapbot.web_bridge import _bridge_log_label
    import QBcore

    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=None)
    monkeypatch.setattr(QBcore, "bot", bot)

    assert _bridge_log_label("guild", "12345") == "12345"


def test_bridge_log_label_returns_dash_for_missing_id():
    from qapbot.web_bridge import _bridge_log_label

    assert _bridge_log_label("guild", None) == "-"
    assert _bridge_log_label("user", "") == "-"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_access_log_middleware_logs_resolved_names_not_raw_ids(client, monkeypatch, caplog):
    import QBcore

    guild = MagicMock()
    guild.name = "The QCrew"
    user = MagicMock()
    user.name = "Qaplop"
    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)
    bot.get_user = MagicMock(return_value=user)
    monkeypatch.setattr(QBcore, "bot", bot)

    with caplog.at_level("INFO"):
        await client.get("/api/cwl/screen", params={"guild_id": "1", "discord_user_id": "2"})

    log_line = next(r.message for r in caplog.records if "GET /api/cwl/screen" in r.message)
    assert "The QCrew (1)" in log_line
    assert "Qaplop (2)" in log_line
    assert "127.0.0.1" not in log_line
    assert "guild_id=1" not in log_line  # the raw id alone must not appear unresolved


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
        "shared_with": None,
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
async def test_clan_config_post_triggers_sharing_for_newly_participating_shared_clan(db, bridge_config, client, monkeypatch):
    """2026-08-15 (cross-guild shared CWL clans) — the first of the two trigger points. #CLAN1
    is newly turned on to participating=True in this save, and another guild (9999) already has
    it participating for the same season — must establish the shared-clan record and notify."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_current_cwl_season

    await _seed_guild_and_clans(db, "667", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}}
    CACHE.server_config["667"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    season = resolve_current_cwl_season()
    db.create_cwl_event_sync("667", season, "discordid1")  # not participating yet

    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES ('9999')")
    await db.conn.commit()
    other_event_id = db.create_cwl_event_sync("9999", season, "otherdiscordid")
    db.set_cwl_event_clans_sync(other_event_id, [{"clan_tag": "#CLAN1", "participating": True}])

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(667, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())
    notify_mock = AsyncMock()
    monkeypatch.setattr("qapbot.ui_cwl_roster.notify_cwl_clan_shared", notify_mock)

    resp = await client.post(
        "/api/cwl/clan-config",
        json={
            "guild_id": 667,
            "discord_user_id": 42,
            "clans": [{"clan_tag": "#CLAN1", "participating": True, "roster_size": 15}],
        },
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200

    shared = db.get_cwl_shared_clan_sync("#CLAN1", season)
    assert shared is not None
    guild_ids = {g["guild_id"] for g in db.list_cwl_shared_clan_guilds_sync(shared["id"])}
    assert guild_ids == {"667", "9999"}
    notify_mock.assert_awaited_once()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_clan_config_post_does_not_trigger_sharing_for_already_participating_clan(db, bridge_config, client, monkeypatch):
    """Re-saving a clan that was ALREADY participating (e.g. just editing roster_size) must not
    re-run the sharing check — only a clan newly turning on this save qualifies."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_current_cwl_season

    await _seed_guild_and_clans(db, "668", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}}
    CACHE.server_config["668"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    season = resolve_current_cwl_season()
    event_id = db.create_cwl_event_sync("668", season, "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True, "roster_size": 15}])

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(668, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())
    notify_mock = AsyncMock()
    monkeypatch.setattr("qapbot.ui_cwl_roster.notify_cwl_clan_shared", notify_mock)

    resp = await client.post(
        "/api/cwl/clan-config",
        json={
            "guild_id": 668,
            "discord_user_id": 42,
            "clans": [{"clan_tag": "#CLAN1", "participating": True, "roster_size": 30}],
        },
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    notify_mock.assert_not_awaited()
    assert db.get_cwl_shared_clan_sync("#CLAN1", season) is None


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


async def _seed_current_clan_member(
    db: WarHistoryDB, discord_id: str, player_tag: str, clan_tag: str, verified: bool = True, th_level: int = None
) -> None:
    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES (?, ?)", (discord_id, discord_id))
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag, th_level) VALUES (?, ?, ?, ?, ?, ?)",
        (discord_id, player_tag, "Player", 1 if verified else 0, clan_tag, th_level),
    )
    await db.conn.commit()


# ---------------------------------------------------------------------------
# GET /api/cwl/screen — "Manage Enrollment" pending-screen picker (2026-08-10, made
# non-destructive 2026-08-14 — see handle_get_cwl_screen's docstring)
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_screen_get_returns_recorded_value_and_does_not_clear_it(bridge_config, client):
    """Must NOT pop — Discord's "pop out" button re-runs main.ts's initial fetch sequence
    (including this call) a second time for the same logical launch. A destructive read
    left that second call with nothing recorded, silently falling back to clan_config even
    though the user never clicked a different button — this is the regression test for
    that live bug."""
    from qapbot.cache_manager import CACHE

    CACHE.pending_cwl_activity_screen[("1", "2")] = "enrollment"

    for _ in range(2):  # simulates the original launch's fetch, then the popped-out window's
        resp = await client.get(
            "/api/cwl/screen",
            params={"guild_id": "1", "discord_user_id": "2"},
            headers={"X-Bridge-Secret": "test-secret"},
        )
        assert resp.status == 200
        assert (await resp.json())["screen"] == "enrollment"
    assert CACHE.pending_cwl_activity_screen[("1", "2")] == "enrollment"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_screen_get_reflects_a_new_click_overwriting_an_older_one(bridge_config, client):
    from qapbot.cache_manager import CACHE

    CACHE.pending_cwl_activity_screen[("1", "2")] = "enrollment"
    await client.get(
        "/api/cwl/screen",
        params={"guild_id": "1", "discord_user_id": "2"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    # A later, different button click overwrites the old recorded value...
    CACHE.pending_cwl_activity_screen[("1", "2")] = "clan_config"

    resp = await client.get(
        "/api/cwl/screen",
        params={"guild_id": "1", "discord_user_id": "2"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    # ...and the next fetch must reflect that, not the earlier value.
    assert (await resp.json())["screen"] == "clan_config"


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
# 2026-08-17 (CWL_PROD_PERFORMANCE_FIX_PLAN.md P1 Step 6): the enrollment and clan-config payload
# builders are now plain sync functions run via asyncio.to_thread from their GET handlers, same
# as every write handler already does (Pitfall 26, COPILOT_PITFALLS_COOKBOOK.md — now covers
# READ paths too). Regression guard: nobody accidentally makes the sync core `async def` again,
# which would silently put its DB work back on the event loop.
# ---------------------------------------------------------------------------

def test_build_enrollment_payload_sync_is_not_a_coroutine_function():
    from qapbot.web_bridge import _build_enrollment_payload_sync
    assert not asyncio.iscoroutinefunction(_build_enrollment_payload_sync)


def test_build_clan_config_payload_sync_is_not_a_coroutine_function():
    from qapbot.web_bridge import _build_clan_config_payload_sync
    assert not asyncio.iscoroutinefunction(_build_clan_config_payload_sync)


def test_search_cwl_guests_sync_is_not_a_coroutine_function():
    from qapbot.web_bridge import _search_cwl_guests_sync
    assert not asyncio.iscoroutinefunction(_search_cwl_guests_sync)


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
    # #P1 has a tracked war attack (TH15) to show up in the board; #P2 has none at all.
    await db.conn.execute(
        "INSERT INTO war_summary (war_id, clan_tag, opponent_tag, is_cwl, cwl_season, date) "
        "VALUES ('war1', '#CLAN1', '#OPP', 0, '', '2026-07-15T10:00')"
    )
    await db.conn.execute(
        "INSERT INTO war_attacks (war_id, clan_tag, date, player_name, player_tag, th_level, map_position, stars) "
        "VALUES ('war1', '#CLAN1', '2026-07-15T10:00', 'Alpha1', '#P1', 15, 1, 0)"
    )
    await db.conn.commit()

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
    assert body["clans"] == [{"clan_tag": "#CLAN1", "name": "Alpha", "tier": "Crystal League I", "roster_size": 15}]

    players_by_tag = {p["player_tag"]: p for p in body["players"]}
    assert players_by_tag["#P1"]["signup_status"] == "pending"
    assert players_by_tag["#P1"]["assigned_clan_tag"] == "#CLAN1"
    assert players_by_tag["#P1"]["th_level"] == 15
    assert players_by_tag["#P1"]["th_icon_url"] == "https://cdn.discordapp.com/emojis/1470128241271640075.png"
    assert players_by_tag["#P2"]["signup_status"] is None
    assert players_by_tag["#P2"]["assigned_clan_tag"] is None
    assert players_by_tag["#P2"]["th_level"] is None
    assert players_by_tag["#P2"]["th_icon_url"] is None
    # Neither player has any CWL-league-tracked attack history seeded in this test — skill_score
    # and avg_stars must both be absent-as-None, never a fabricated 0 (see
    # compute_league_adjusted_skill_scores / compute_avg_stars_per_attack).
    assert players_by_tag["#P1"]["skill_score"] is None
    assert players_by_tag["#P2"]["skill_score"] is None
    assert players_by_tag["#P1"]["avg_stars"] is None
    assert players_by_tag["#P2"]["avg_stars"] is None


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_get_includes_members_of_non_participating_member_clans(db, bridge_config, client, monkeypatch):
    """2026-08-14 (project owner's spec): the pool is every current member of every guild member
    clan, not just clans participating in CWL this season — so an admin can drag in a player
    from a clan that opted out, or that just never opted in. #CLAN2 is a guild member clan but
    NOT participating this event; #P2 (a #CLAN2 member) must still show up in the payload,
    unassigned (no column exists for #CLAN2 to be assigned into)."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "791", {"#CLAN1": "Alpha", "#CLAN2": "Bravo"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}, "#CLAN2": {"name": "Bravo"}}
    CACHE.server_config["791"] = {"member_clans": ["#CLAN1", "#CLAN2"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    event_id = db.create_cwl_event_sync("791", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    await _seed_current_clan_member(db, "10", "#P1", "#CLAN1")
    await _seed_current_clan_member(db, "11", "#P2", "#CLAN2")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(791, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/enrollment",
        params={"guild_id": "791", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    # Only #CLAN1 gets a column — #CLAN2 isn't participating this season.
    assert [c["clan_tag"] for c in body["clans"]] == ["#CLAN1"]
    players_by_tag = {p["player_tag"]: p for p in body["players"]}
    assert "#P2" in players_by_tag
    assert players_by_tag["#P2"]["assigned_clan_tag"] is None


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_get_includes_guest_clan_roster(db, bridge_config, client, monkeypatch):
    """2026-08-15 (Guests search, project owner's spec): a clan added to cwl_event_clans that
    ISN'T in the guild's own member_clans/member_families (a "guest clan", added via the
    Configure Participating Clans Guests search — see _search_cwl_guests's docstring) still gets
    both a board column AND its current members pulled into the pool, exactly like a real family
    clan — that's the whole "full participating clan treatment" the project owner asked for."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "792", {"#CLAN1": "Alpha", "#GUESTCLAN": "Guest Clan"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}, "#GUESTCLAN": {"name": "Guest Clan"}}
    # #GUESTCLAN deliberately absent from member_clans/member_families — that's what makes it a
    # guest rather than a normal family clan.
    CACHE.server_config["792"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    event_id = db.create_cwl_event_sync("792", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [
        {"clan_tag": "#CLAN1", "participating": True},
        {"clan_tag": "#GUESTCLAN", "participating": True},
    ])
    await _seed_current_clan_member(db, "10", "#P1", "#CLAN1")
    await _seed_current_clan_member(db, "20", "#PGUEST", "#GUESTCLAN")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(792, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/enrollment",
        params={"guild_id": "792", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert {c["clan_tag"] for c in body["clans"]} == {"#CLAN1", "#GUESTCLAN"}
    players_by_tag = {p["player_tag"]: p for p in body["players"]}
    assert "#PGUEST" in players_by_tag  # pulled in via the union, not just resolve_guild_member_clan_tags
    assert players_by_tag["#PGUEST"]["current_clan_tag"] == "#GUESTCLAN"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_get_marks_guest_invited_players(db, bridge_config, client, monkeypatch):
    """A guest PLAYER (cwl_signups.source='guest_invite', from POST /api/cwl/enrollment/guest)
    carries is_guest=True; a normal member/signup carries is_guest=False."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "793", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}}
    CACHE.server_config["793"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    event_id = db.create_cwl_event_sync("793", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    await _seed_current_clan_member(db, "10", "#P1", "#CLAN1")
    db.upsert_cwl_signup_sync(event_id, "#P1", "Alpha1", "10", None, "template_confirm", "pending")
    db.upsert_cwl_signup_sync(event_id, "#GUEST1", "GuestOne", "99", None, "guest_invite", "pending")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(793, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/enrollment",
        params={"guild_id": "793", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    players_by_tag = {p["player_tag"]: p for p in body["players"]}
    assert players_by_tag["#P1"]["is_guest"] is False
    assert players_by_tag["#GUEST1"]["is_guest"] is True


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_get_is_guest_follows_live_current_clan_not_assignment_or_source(
    db, bridge_config, client, monkeypatch
):
    """2026-08-16 follow-up, live-testing feedback, project owner's spec, verbatim: "the yellow
    marker is defined as being a GUEST player for this guild... a member is a member regardless
    of assignment status, a guest is a guest regardless of assignment status." is_guest must be
    driven by live current-clan membership, not by an assignment or by whichever write path
    happened to stamp the local row's source:
    - #FAMILY_LEGACY_GUEST is a CURRENT member of this guild's own family clan #CLAN1, but its
      local signup carries source='guest_invite' from an earlier, unrelated guest invite — must
      show is_guest=False now that it's a genuine family member, not the stale marker.
    - #FOREIGN_ASSIGNED is a current member of #GUESTCLAN (outside the family) who has been
      deliberately drag-assigned INTO #CLAN1 (this guild's own clan) — must still show
      is_guest=True despite being assigned into a member clan's roster, since the badge tracks
      who they ARE, not where they've been placed."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "794", {"#CLAN1": "Alpha", "#GUESTCLAN": "Beta"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}, "#GUESTCLAN": {"name": "Beta"}}
    CACHE.server_config["794"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    event_id = db.create_cwl_event_sync("794", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [
        {"clan_tag": "#CLAN1", "participating": True},
        {"clan_tag": "#GUESTCLAN", "participating": True},
    ])
    await _seed_current_clan_member(db, "10", "#FAMILY_LEGACY_GUEST", "#CLAN1")
    db.upsert_cwl_signup_sync(event_id, "#FAMILY_LEGACY_GUEST", "FamilyMember", "10", None, "guest_invite", "pending")
    await _seed_current_clan_member(db, "20", "#FOREIGN_ASSIGNED", "#GUESTCLAN")
    db.upsert_cwl_assignment_sync(event_id, "#FOREIGN_ASSIGNED", "#CLAN1", assignment_source="admin_override", locked=True)

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(794, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/enrollment",
        params={"guild_id": "794", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    players_by_tag = {p["player_tag"]: p for p in body["players"]}
    assert players_by_tag["#FAMILY_LEGACY_GUEST"]["is_guest"] is False
    assert players_by_tag["#FOREIGN_ASSIGNED"]["is_guest"] is True
    assert players_by_tag["#FOREIGN_ASSIGNED"]["assigned_clan_tag"] == "#CLAN1"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_get_resolves_current_clan_tag_outside_family_and_participating(db, bridge_config, client, monkeypatch):
    """2026-08-15 bugfix (live-testing feedback): a guest/account-wide-expanded player's real
    current clan can be outside BOTH the guild's own family AND every clan participating this
    season — get_current_clan_members_sync(all_member_clan_tags) structurally can't see them,
    which left current_clan_tag permanently null and their card stuck plain/default instead of
    green/amber. #PGUEST is assigned to #CLAN1 (participating) but really still sits in
    #OUTSIDE_CLAN (neither family nor participating) — must resolve to "different-clan" data,
    i.e. current_clan_tag == #OUTSIDE_CLAN, not null."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "794", {"#CLAN1": "Alpha", "#OUTSIDE_CLAN": "Outside"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}}
    # #OUTSIDE_CLAN deliberately absent from member_clans/member_families AND never configured
    # as participating for this event — exactly the gap the fallback closes.
    CACHE.server_config["794"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    event_id = db.create_cwl_event_sync("794", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    await _seed_current_clan_member(db, "20", "#PGUEST", "#OUTSIDE_CLAN")
    db.upsert_cwl_signup_sync(event_id, "#PGUEST", "GuestPlayer", "20", None, "guest_invite", "pending")
    db.upsert_cwl_assignment_sync(event_id, "#PGUEST", "#CLAN1")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(794, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/enrollment",
        params={"guild_id": "794", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    players_by_tag = {p["player_tag"]: p for p in body["players"]}
    assert players_by_tag["#PGUEST"]["current_clan_tag"] == "#OUTSIDE_CLAN"
    assert players_by_tag["#PGUEST"]["assigned_clan_tag"] == "#CLAN1"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_get_includes_cwl_permanent_optout_flag(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "792", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}}
    CACHE.server_config["792"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    event_id = db.create_cwl_event_sync("792", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES ('10', '10')")
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag, cwl_permanent_optout) "
        "VALUES ('10', '#P1', 'Player', 1, '#CLAN1', 1)"
    )
    await db.conn.commit()

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(792, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/enrollment",
        params={"guild_id": "792", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    players_by_tag = {p["player_tag"]: p for p in body["players"]}
    assert players_by_tag["#P1"]["cwl_permanent_optout"] is True


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_get_includes_current_clan_tag(db, bridge_config, client, monkeypatch):
    """current_clan_tag (2026-08-14, same-clan/different-clan card highlighting) is
    user_players.current_clan_tag — distinct from assigned_clan_tag, which is the CWL
    assignment. Absent (null) for a player only known via an old signup who's since left every
    guild clan, so get_current_clan_members_sync() no longer covers them."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "793", {"#CLAN1": "Alpha", "#CLAN2": "Bravo"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}, "#CLAN2": {"name": "Bravo"}}
    CACHE.server_config["793"] = {"member_clans": ["#CLAN1", "#CLAN2"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    event_id = db.create_cwl_event_sync("793", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    # #P1 is currently in #CLAN2 but assigned to #CLAN1 — a real transfer-pending case.
    await _seed_current_clan_member(db, "10", "#P1", "#CLAN2")
    db.upsert_cwl_assignment_sync(event_id, "#P1", "#CLAN1")
    # #P2 has a signup row but has since left every guild clan entirely.
    db.upsert_cwl_signup_sync(event_id, "#P2", "Bravo2", "11", None, "template_confirm", "pending")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(793, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/enrollment",
        params={"guild_id": "793", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    players_by_tag = {p["player_tag"]: p for p in body["players"]}
    assert players_by_tag["#P1"]["current_clan_tag"] == "#CLAN2"
    assert players_by_tag["#P1"]["assigned_clan_tag"] == "#CLAN1"
    assert players_by_tag["#P2"]["current_clan_tag"] is None


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_get_prefers_live_th_level_over_war_attacks_fallback(db, bridge_config, client, monkeypatch):
    """user_players.th_level (kept fresh by coc_cache.py's per-clan poll, 2026-08-14) must win
    over the war_attacks-derived fallback when both exist — the live value is always at least as
    fresh, and doesn't require the player to have ever made a tracked war attack."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "790", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["790"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    event_id = db.create_cwl_event_sync("790", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    # Stale war_attacks history (TH14) vs a fresher live user_players.th_level (TH16, e.g. the
    # player upgraded since that war).
    await db.conn.execute(
        "INSERT INTO war_summary (war_id, clan_tag, opponent_tag, is_cwl, cwl_season, date) "
        "VALUES ('war1', '#CLAN1', '#OPP', 0, '', '2026-06-01T10:00')"
    )
    await db.conn.execute(
        "INSERT INTO war_attacks (war_id, clan_tag, date, player_name, player_tag, th_level, map_position, stars) "
        "VALUES ('war1', '#CLAN1', '2026-06-01T10:00', 'Alpha1', '#P1', 14, 1, 0)"
    )
    await db.conn.commit()
    await _seed_current_clan_member(db, "10", "#P1", "#CLAN1", th_level=16)

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(790, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/enrollment",
        params={"guild_id": "790", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    players_by_tag = {p["player_tag"]: p for p in body["players"]}
    assert players_by_tag["#P1"]["th_level"] == 16


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


# ---------------------------------------------------------------------------
# Guests (2026-08-15) — GET /api/cwl/guest-search, POST /api/cwl/enrollment/guest. See
# qapbot/web_bridge.py's _search_cwl_guests docstring: a guest CLAN hit is added via the
# existing POST /api/cwl/clan-config (no separate endpoint), so only the search itself and the
# guest-PLAYER add need coverage here.
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_search_get_rejects_missing_secret(bridge_config, client):
    resp = await client.get("/api/cwl/guest-search?guild_id=800&discord_user_id=42&q=foo")
    assert resp.status == 403


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_search_get_rejects_non_admin(bridge_config, client, monkeypatch):
    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(800, 42, is_admin=False))

    resp = await client.get(
        "/api/cwl/guest-search?guild_id=800&discord_user_id=42&q=foo",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 403


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_search_returns_clan_hits_excluding_already_participating(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "801", {"#CLAN1": "Marines", "#CLAN2": "QCrew"})
    CACHE.db_manager = db
    CACHE.server_config["801"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.clan_name_cache = {
        "#CLAN1": {"name": "Marines"},
        "#CLAN2": {"name": "QCrew"},
        "#CLAN3": {"name": "Not A Match"},
    }
    CACHE.player_name_index = {}
    CACHE.user_accounts = {}

    event_id = db.create_cwl_event_sync("801", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(801, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/guest-search?guild_id=801&discord_user_id=42&q=cre",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    # #CLAN1 (Marines) already participates -> excluded even though "cre" isn't in its name;
    # #CLAN2 (QCrew) matches "cre" and isn't participating -> included; #CLAN3 doesn't match "cre".
    assert body["results"] == [
        {"type": "clan", "clan_tag": "#CLAN2", "clan_name": "QCrew", "clan_tier": None, "already_shared_with": None}
    ]


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_search_highlights_clan_already_on_another_guilds_roster(db, bridge_config, client, monkeypatch):
    """2026-08-15 (cross-guild shared CWL clans, project owner's spec): a clan already
    participating in ANOTHER guild's event for the same season is still shown (never hidden),
    annotated with already_shared_with so the frontend can highlight it and confirm before add."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_current_cwl_season

    await _seed_guild_and_clans(db, "811", {"#CLAN1": "Marines"})
    CACHE.db_manager = db
    CACHE.server_config["811"] = {"member_clans": [], "member_families": []}
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Marines"}}
    CACHE.player_name_index = {}
    CACHE.user_accounts = {}

    season = resolve_current_cwl_season()
    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES ('9999')")
    await db.conn.commit()
    other_event_id = db.create_cwl_event_sync("9999", season, "otherdiscordid")
    db.set_cwl_event_clans_sync(other_event_id, [{"clan_tag": "#CLAN1", "participating": True}])

    import QBcore
    admin_member = MagicMock()
    admin_member.guild_permissions.administrator = True
    acting_guild = MagicMock()
    acting_guild.get_member = MagicMock(return_value=admin_member)
    acting_guild.fetch_member = AsyncMock(return_value=admin_member)
    other_guild = MagicMock()
    other_guild.name = "The Other Guild"

    def _get_guild(gid):
        return acting_guild if gid == 811 else (other_guild if gid == 9999 else None)

    bot = MagicMock()
    bot.get_guild = MagicMock(side_effect=_get_guild)
    monkeypatch.setattr(QBcore, "bot", bot)

    resp = await client.get(
        "/api/cwl/guest-search?guild_id=811&discord_user_id=42&q=marines",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["results"] == [
        {
            "type": "clan", "clan_tag": "#CLAN1", "clan_name": "Marines", "clan_tier": None,
            "already_shared_with": "The Other Guild",
        }
    ]


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_search_clan_hit_includes_live_tier(db, bridge_config, client, monkeypatch):
    """2026-08-15 bugfix (live-testing feedback): a guest clan added via the search previously
    always showed tier "—" until the next full page reload, because the search result never
    carried CACHE's live war_league at all — the frontend just hardcoded tier: null when
    constructing the new row."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "810", {"#CLAN1": "Marines"})
    CACHE.db_manager = db
    CACHE.server_config["810"] = {"member_clans": [], "member_families": []}
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Marines", "war_league": "Master League I"}}
    CACHE.player_name_index = {}
    CACHE.user_accounts = {}

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(810, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/guest-search?guild_id=810&discord_user_id=42&q=marines",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["results"] == [
        {
            "type": "clan", "clan_tag": "#CLAN1", "clan_name": "Marines", "clan_tier": "Master League I",
            "already_shared_with": None,
        }
    ]


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_search_returns_player_hit_by_name(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "802", {"#OUTSIDE_CLAN": "Outside"})
    CACHE.db_manager = db
    CACHE.server_config["802"] = {"member_clans": [], "member_families": []}
    CACHE.clan_name_cache = {}
    CACHE.player_name_index = {"#GUEST1": "GuestPlayer"}
    CACHE.user_accounts = {}
    await _seed_current_clan_member(db, "555", "#GUEST1", "#OUTSIDE_CLAN")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(802, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/guest-search?guild_id=802&discord_user_id=42&q=guest",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["results"] == [
        {"type": "player", "player_tag": "#GUEST1", "player_name": "Player", "discord_id": "555"}
    ]


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_search_returns_player_hit_via_discord_account_name(db, bridge_config, client, monkeypatch):
    """Searching a Discord display name surfaces that account's linked players directly,
    flattened — not a nested 'discord_user' result type (see _search_cwl_guests docstring)."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "803", {})
    CACHE.db_manager = db
    CACHE.server_config["803"] = {"member_clans": [], "member_families": []}
    CACHE.clan_name_cache = {}
    CACHE.player_name_index = {}
    CACHE.user_accounts = {
        "777": {"display_name": "Qaplop", "players": [{"player_tag": "#Q1", "player_name": "Qaplop"}]},
    }

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(803, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/guest-search?guild_id=803&discord_user_id=42&q=qap",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["results"] == [
        {"type": "player", "player_tag": "#Q1", "player_name": "Qaplop", "discord_id": None}
    ]


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_search_raw_unindexed_tag_still_returned(db, bridge_config, client, monkeypatch):
    """A tag the bot has genuinely never seen still comes back as a hit (name = the tag itself)
    so the admin can add it directly — it just can't be DMed until it's real."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "804", {})
    CACHE.db_manager = db
    CACHE.server_config["804"] = {"member_clans": [], "member_families": []}
    CACHE.clan_name_cache = {}
    CACHE.player_name_index = {}
    CACHE.user_accounts = {}

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(804, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/guest-search?guild_id=804&discord_user_id=42&q=%23NEVERSEEN",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["results"] == [
        {"type": "player", "player_tag": "#NEVERSEEN", "player_name": "#NEVERSEEN", "discord_id": None}
    ]


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_search_at_prefix_restricts_to_discord_display_name_only(db, bridge_config, client, monkeypatch):
    """2026-08-16, live-testing feedback, project owner's spec, verbatim: "when a given
    expression starts with @ assume it is a discord user and only search in that name space."
    A clan name and a plain (non-linked-account) player name that both also match the needle must
    be excluded — only players reached via a matching Discord display name come back."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "820", {})
    CACHE.db_manager = db
    CACHE.server_config["820"] = {"member_clans": [], "member_families": []}
    CACHE.clan_name_cache = {"#CLANQAP": {"name": "Qap Clan"}}
    CACHE.player_name_index = {"#LONER": "Qaplike"}  # matches "qap" by name, but not linked
    CACHE.user_accounts = {
        "777": {"display_name": "Qaplop", "players": [{"player_tag": "#Q1", "player_name": "Qaplop"}]},
    }

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(820, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/guest-search?guild_id=820&discord_user_id=42&q=%40qap",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["results"] == [
        {"type": "player", "player_tag": "#Q1", "player_name": "Qaplop", "discord_id": None}
    ]


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_search_hash_prefix_restricts_to_tag_matching_only(db, bridge_config, client, monkeypatch):
    """2026-08-16, live-testing feedback, project owner's spec, verbatim: "when the expression
    starts with a # assume that we are talking about a clan, player or family tag." A clan whose
    NAME matches the needle but whose tag doesn't must be excluded once # restricts to tag-only
    matching."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "821", {})
    CACHE.db_manager = db
    CACHE.server_config["821"] = {"member_clans": [], "member_families": []}
    CACHE.clan_name_cache = {
        "#QAPXYZ": {"name": "Something Else"},  # tag starts with #QAP -> matches
        "#OTHERTAG": {"name": "Qap Warriors"},  # name contains Qap, tag doesn't -> excluded
    }
    CACHE.player_name_index = {}
    CACHE.user_accounts = {}

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(821, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/guest-search?guild_id=821&discord_user_id=42&q=%23qap",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["results"] == [
        {"type": "clan", "clan_tag": "#QAPXYZ", "clan_name": "Something Else", "clan_tier": None, "already_shared_with": None}
    ]


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_search_interleaves_and_caps_clan_and_player_hits(db, bridge_config, client, monkeypatch):
    """2026-08-16, live-testing feedback, project owner's spec, verbatim: "Do the interleave, cap
    each type 12 / 12" — a broad query matching more than 12 of each type must not let one type
    bury the other; the result alternates clan/player and stops at 12 of each (24 total here)."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "822", {})
    CACHE.db_manager = db
    CACHE.server_config["822"] = {"member_clans": [], "member_families": []}
    CACHE.clan_name_cache = {f"#CLAN{i:02d}": {"name": f"Test Clan {i:02d}"} for i in range(15)}
    CACHE.player_name_index = {f"#PLAYER{i:02d}": f"Test Player {i:02d}" for i in range(15)}
    CACHE.user_accounts = {}

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(822, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/guest-search?guild_id=822&discord_user_id=42&q=test",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    results = body["results"]
    assert len(results) == 24
    types = [r["type"] for r in results]
    assert types == ["clan", "player"] * 12
    clan_tags = {r["clan_tag"] for r in results if r["type"] == "clan"}
    player_tags = {r["player_tag"] for r in results if r["type"] == "player"}
    assert len(clan_tags) == 12
    assert len(player_tags) == 12


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_search_empty_query_returns_empty(bridge_config, client, monkeypatch):
    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(805, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/guest-search?guild_id=805&discord_user_id=42&q=",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["results"] == []


# ---------------------------------------------------------------------------
# 2026-08-17 PROD meltdown fix (CWL_PROD_PERFORMANCE_FIX_PLAN.md P0 Step 1/2): server-side
# minimum query length (reject before any scan) and per-scan caps (break during collection,
# not after) for _search_cwl_guests. See that function's module-level GUEST_SEARCH_MIN_NEEDLE_*/
# GUEST_SEARCH_CAP constants.
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_search_below_minimum_text_length_returns_empty(db, bridge_config, client, monkeypatch):
    """A plain-text query under GUEST_SEARCH_MIN_NEEDLE_TEXT (3) must reject before any scan,
    even though the CACHE contents would otherwise match — and must never reach
    get_player_links_sync (the call whose unbounded input caused "too many SQL variables")."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "830", {"#CLAN1": "QCrew"})
    CACHE.db_manager = db
    CACHE.server_config["830"] = {"member_clans": [], "member_families": []}
    CACHE.clan_name_cache = {"#CLAN1": {"name": "QCrew"}}
    CACHE.player_name_index = {}
    CACHE.user_accounts = {}

    link_spy = MagicMock(wraps=db.get_player_links_sync)
    monkeypatch.setattr(db, "get_player_links_sync", link_spy)

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(830, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/guest-search?guild_id=830&discord_user_id=42&q=cr",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["results"] == []
    link_spy.assert_not_called()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_search_below_minimum_at_prefix_needle_returns_empty(db, bridge_config, client, monkeypatch):
    """@needle under GUEST_SEARCH_MIN_NEEDLE_TAG (2) must reject before scanning user_accounts."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "831", {})
    CACHE.db_manager = db
    CACHE.server_config["831"] = {"member_clans": [], "member_families": []}
    CACHE.clan_name_cache = {}
    CACHE.player_name_index = {}
    CACHE.user_accounts = {
        "777": {"display_name": "Qaplop", "players": [{"player_tag": "#Q1", "player_name": "Qaplop"}]},
    }

    link_spy = MagicMock(wraps=db.get_player_links_sync)
    monkeypatch.setattr(db, "get_player_links_sync", link_spy)

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(831, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/guest-search?guild_id=831&discord_user_id=42&q=%40q",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["results"] == []
    link_spy.assert_not_called()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_search_below_minimum_hash_prefix_needle_returns_empty(db, bridge_config, client, monkeypatch):
    """#2 -> needle "2" (length 1) must reject; this is the exact PROD incident pattern (a short
    '#'-prefixed query matching millions of player_name_index entries)."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "832", {})
    CACHE.db_manager = db
    CACHE.server_config["832"] = {"member_clans": [], "member_families": []}
    CACHE.clan_name_cache = {}
    CACHE.player_name_index = {"#2ABCDEFGH": "SomePlayer"}
    CACHE.user_accounts = {}

    link_spy = MagicMock(wraps=db.get_player_links_sync)
    monkeypatch.setattr(db, "get_player_links_sync", link_spy)

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(832, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/guest-search?guild_id=832&discord_user_id=42&q=%232",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["results"] == []
    link_spy.assert_not_called()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_search_hash_prefix_caps_player_hits_at_twelve(db, bridge_config, client, monkeypatch):
    """The # tag-mode scan over player_name_index must stop collecting once GUEST_SEARCH_CAP
    (12) hits exist, not after scanning the whole index."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "833", {})
    CACHE.db_manager = db
    CACHE.server_config["833"] = {"member_clans": [], "member_families": []}
    CACHE.clan_name_cache = {}
    CACHE.player_name_index = {f"#QAP{i:03d}": f"Player {i:03d}" for i in range(20)}
    CACHE.user_accounts = {}

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(833, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/guest-search?guild_id=833&discord_user_id=42&q=%23qap",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    player_hits = [r for r in body["results"] if r["type"] == "player"]
    assert len(player_hits) == 12


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_search_text_query_caps_clan_hits_and_db_check_calls(db, bridge_config, client, monkeypatch):
    """The clan scan breaks at GUEST_SEARCH_CAP (12) and the cross-guild DB check
    (find_cwl_clan_participation_across_guilds_sync) only ever runs for the capped set, not once
    per matching clan found during the scan — this is Step 2's "≤12 DB queries per search, ever"
    guarantee."""
    from qapbot.cache_manager import CACHE

    clan_tags = {f"#CLAN{i:02d}": f"Test Clan {i:02d}" for i in range(20)}
    await _seed_guild_and_clans(db, "834", clan_tags)
    CACHE.db_manager = db
    CACHE.server_config["834"] = {"member_clans": [], "member_families": []}
    CACHE.clan_name_cache = {tag: {"name": name} for tag, name in clan_tags.items()}
    CACHE.player_name_index = {}
    CACHE.user_accounts = {}

    claim_spy = MagicMock(wraps=db.find_cwl_clan_participation_across_guilds_sync)
    monkeypatch.setattr(db, "find_cwl_clan_participation_across_guilds_sync", claim_spy)

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(834, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/guest-search?guild_id=834&discord_user_id=42&q=test",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    clan_hits = [r for r in body["results"] if r["type"] == "clan"]
    assert len(clan_hits) == 12
    assert claim_spy.call_count <= 12


# ---------------------------------------------------------------------------
# 2026-08-17 PROD meltdown fix (CWL_PROD_PERFORMANCE_FIX_PLAN.md P0 Step 3): guest search now
# runs off the event loop via asyncio.to_thread, plus a single-flight/newest-wins guard per
# (guild_id, discord_user_id) so a burst of debounced keystrokes can't pile up concurrent DB
# scans for the same admin.
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_search_still_returns_normal_results_when_threaded(db, bridge_config, client, monkeypatch):
    """Behavioral no-op check: routing the real _search_cwl_guests_sync through
    asyncio.to_thread must not change what a normal (non-concurrent) query returns."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "840", {"#CLAN1": "Marines"})
    CACHE.db_manager = db
    CACHE.server_config["840"] = {"member_clans": [], "member_families": []}
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Marines"}}
    CACHE.player_name_index = {}
    CACHE.user_accounts = {}

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(840, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/guest-search?guild_id=840&discord_user_id=42&q=marines",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["results"] == [
        {"type": "clan", "clan_tag": "#CLAN1", "clan_name": "Marines", "clan_tier": None, "already_shared_with": None}
    ]
    assert "stale" not in body


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_search_coalesces_a_queued_keystroke_superseded_by_a_newer_one(
    bridge_config, client, monkeypatch,
):
    """Fires three overlapping searches for the SAME admin to prove the single-flight guard
    actually coalesces pile-up rather than just serializing every keystroke:
      1. "first" acquires the per-key semaphore and blocks (a real worker thread, released on
         cue) — simulates a search already in flight when the next keystroke lands.
      2. "second" arrives while "first" is still running; it bumps the generation counter, then
         queues behind the semaphore.
      3. "third" arrives before "second" gets a turn; it bumps the generation counter past
         "second"'s, then also queues.
    When "first" finally releases the semaphore, "second" is next in line (FIFO) — it discovers
    its generation is stale (superseded by "third") and returns {"stale": True} WITHOUT ever
    calling the underlying search function. "third" then runs for real. So the underlying search
    function must be called exactly twice ("first", "third") — never for "second" — which is
    the actual pile-up-prevention property Step 3 exists for."""
    import qapbot.web_bridge as web_bridge_module

    started = threading.Event()
    release = threading.Event()
    calls: list = []

    def fake_search(guild_id, query):
        calls.append(query)
        if query == "first":
            started.set()
            release.wait(timeout=5)
        return []

    monkeypatch.setattr(web_bridge_module, "_search_cwl_guests_sync", fake_search)

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(841, 42, is_admin=True))

    async def _get(query: str):
        return await client.get(
            f"/api/cwl/guest-search?guild_id=841&discord_user_id=42&q={query}",
            headers={"X-Bridge-Secret": "test-secret"},
        )

    task_first = asyncio.create_task(_get("first"))
    for _ in range(500):
        if started.is_set():
            break
        await asyncio.sleep(0.01)
    assert started.is_set(), "the 'first' search never started"

    task_second = asyncio.create_task(_get("second"))
    await asyncio.sleep(0.05)  # let "second" bump its generation and queue on the semaphore

    task_third = asyncio.create_task(_get("third"))
    await asyncio.sleep(0.05)  # let "third" bump its generation and queue on the semaphore

    release.set()  # let "first" finish and release the semaphore

    resp_first, resp_second, resp_third = await asyncio.gather(task_first, task_second, task_third)

    body_first = await resp_first.json()
    body_second = await resp_second.json()
    body_third = await resp_third.json()

    assert resp_second.status == 200
    assert body_second == {"results": [], "stale": True}
    assert body_first["results"] == [] and "stale" not in body_first
    assert body_third["results"] == [] and "stale" not in body_third

    assert calls == ["first", "third"]


# ---------------------------------------------------------------------------
# Clan-name lookup (2026-08-16) — GET /api/cwl/clan-names. Backs the Manage Enrollment board's
# hover pop-up: it renders instantly with whatever's already loaded, then fetches a display name
# for any current_clan_tag that isn't one of this event's own columns (project owner's spec:
# "show up the pop-up as soon as possible... then fetch more data... starting with the clan
# name"). Same admin-or-leader gate as the enrollment screen itself.
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_clan_names_get_rejects_missing_secret(bridge_config, client):
    resp = await client.get("/api/cwl/clan-names?guild_id=830&discord_user_id=42&tags=%23CLAN1")
    assert resp.status == 403


@pytest.mark.discord
@pytest.mark.asyncio
async def test_clan_names_get_rejects_non_admin_non_leader(bridge_config, client, monkeypatch):
    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(830, 42, is_admin=False))

    resp = await client.get(
        "/api/cwl/clan-names?guild_id=830&discord_user_id=42&tags=%23CLAN1",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 403


@pytest.mark.discord
@pytest.mark.asyncio
async def test_clan_names_resolves_known_tags_and_omits_unknown_ones(bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    CACHE.clan_name_cache = {"#CLAN1": {"name": "Marines"}, "#CLAN2": {"name": "QCrew"}}

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(831, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/clan-names?guild_id=831&discord_user_id=42&tags=%23CLAN1,%23CLAN2,%23NEVERSEEN",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    # #NEVERSEEN silently omitted — the client already falls back to the raw tag for those.
    assert body["names"] == {"#CLAN1": "Marines", "#CLAN2": "QCrew"}


@pytest.mark.discord
@pytest.mark.asyncio
async def test_clan_names_empty_tags_returns_empty(bridge_config, client, monkeypatch):
    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(832, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/clan-names?guild_id=832&discord_user_id=42&tags=",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["names"] == {}


# ---------------------------------------------------------------------------
# 2026-08-17 (CWL_PROD_PERFORMANCE_FIX_PLAN.md P1 Step 8) — GET /api/cwl/enrollment/wait, the
# event-driven long-poll replacing the client's old fixed 12s setInterval. guild_ids in the
# 860x range are dedicated to this section (never reused elsewhere in this file) since
# _enrollment_version/_enrollment_changed are module-level state that persists across tests.
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_get_includes_version_field(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "8600", {})
    CACHE.db_manager = db
    CACHE.server_config["8600"] = {"member_clans": [], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(8600, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/enrollment",
        params={"guild_id": "8600", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["version"] == 0


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_wait_rejects_missing_secret(bridge_config, client):
    resp = await client.get("/api/cwl/enrollment/wait?guild_id=8601&discord_user_id=42&known_version=0")
    assert resp.status == 403


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_wait_stale_known_version_returns_immediately(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "8601", {})
    CACHE.db_manager = db
    CACHE.server_config["8601"] = {"member_clans": [], "member_families": []}

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(8601, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/enrollment/wait?guild_id=8601&discord_user_id=42&known_version=999",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body == {"changed": True, "version": 0}


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_wait_times_out_with_no_write(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE
    import qapbot.web_bridge as web_bridge_module

    await _seed_guild_and_clans(db, "8602", {})
    CACHE.db_manager = db
    CACHE.server_config["8602"] = {"member_clans": [], "member_families": []}

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(8602, 42, is_admin=True))
    monkeypatch.setattr(web_bridge_module, "_ENROLLMENT_WAIT_TIMEOUT_SECONDS", 0.1)

    resp = await client.get(
        "/api/cwl/enrollment/wait?guild_id=8602&discord_user_id=42&known_version=0",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body == {"changed": False, "version": 0}


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_wait_resolves_on_concurrent_write(db, bridge_config, client, monkeypatch):
    """A parked wait must be released promptly by a real write through the normal POST endpoint —
    not just by calling bump_enrollment_version() directly — proving the actual handler wiring,
    not just the primitive."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_current_cwl_season

    await _seed_guild_and_clans(db, "8603", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}}
    CACHE.server_config["8603"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    season = resolve_current_cwl_season()
    event_id = db.create_cwl_event_sync("8603", season, "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    await _seed_current_clan_member(db, "10", "#P1", "#CLAN1")
    await db.conn.commit()

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(8603, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    wait_task = asyncio.create_task(client.get(
        "/api/cwl/enrollment/wait?guild_id=8603&discord_user_id=42&known_version=0",
        headers={"X-Bridge-Secret": "test-secret"},
    ))
    await asyncio.sleep(0.05)  # let the wait task actually park on the guild's Condition
    assert not wait_task.done()

    assign_resp = await client.post(
        "/api/cwl/enrollment/assign",
        json={"guild_id": 8603, "discord_user_id": 42, "player_tag": "#P1", "clan_tag": "#CLAN1"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert assign_resp.status == 200

    wait_resp = await asyncio.wait_for(wait_task, timeout=5)
    assert wait_resp.status == 200
    body = await wait_resp.json()
    assert body == {"changed": True, "version": 1}


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_wait_waiter_cap_overflow_returns_changed_immediately(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE
    import qapbot.web_bridge as web_bridge_module

    await _seed_guild_and_clans(db, "8604", {})
    CACHE.db_manager = db
    CACHE.server_config["8604"] = {"member_clans": [], "member_families": []}

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(8604, 42, is_admin=True))
    monkeypatch.setattr(web_bridge_module, "_ENROLLMENT_WAIT_MAX_WAITERS_PER_GUILD", 1)
    monkeypatch.setattr(web_bridge_module, "_ENROLLMENT_WAIT_TIMEOUT_SECONDS", 0.2)

    first_task = asyncio.create_task(client.get(
        "/api/cwl/enrollment/wait?guild_id=8604&discord_user_id=42&known_version=0",
        headers={"X-Bridge-Secret": "test-secret"},
    ))
    await asyncio.sleep(0.05)  # let it register as the one parked waiter allowed by the cap
    assert not first_task.done()

    overflow_resp = await client.get(
        "/api/cwl/enrollment/wait?guild_id=8604&discord_user_id=42&known_version=0",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert overflow_resp.status == 200
    overflow_body = await overflow_resp.json()
    assert overflow_body == {"changed": True, "version": 0}

    # The first waiter is still just an ordinary timeout — degrade-gracefully behavior, not a
    # crash — confirming the cap didn't corrupt its own outcome.
    first_resp = await asyncio.wait_for(first_task, timeout=5)
    assert first_resp.status == 200
    first_body = await first_resp.json()
    assert first_body == {"changed": False, "version": 0}


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_wait_cross_guild_shared_clan_write_releases_other_guild(db, bridge_config, client, monkeypatch):
    """A shared-clan write made through guild A (the owner) must release guild B's (the
    follower's) parked waiter too — the global-bump fallback (bump_enrollment_version's own
    docstring) for the case sync_cwl_shared_clan_roster_to_local_pools() doesn't report exactly
    which other guild(s) it touched."""
    from qapbot.cache_manager import CACHE

    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}
    CACHE.server_config["8605"] = {"member_clans": [], "member_families": []}
    CACHE.server_config["8606"] = {"member_clans": [], "member_families": []}
    shared_clan_id, owner_event_id, follower_event_id = await _seed_shared_clan_pair(db, "8605", "8606")
    db.set_cwl_shared_clan_player_status_sync(
        shared_clan_id, "#P1", "Alpha1", "999", "pending", "guest_invite", "8605",
    )

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(8605, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    # Guild 8606 (the follower) is the one waiting.
    wait_task = asyncio.create_task(client.get(
        "/api/cwl/enrollment/wait?guild_id=8606&discord_user_id=42&known_version=0",
        headers={"X-Bridge-Secret": "test-secret"},
    ))
    await asyncio.sleep(0.05)
    assert not wait_task.done()

    # Guild 8605 (the owner) confirms the shared player — the write that must release 8606's wait.
    signup_resp = await client.post(
        "/api/cwl/enrollment/signup",
        json={"guild_id": 8605, "discord_user_id": 42, "player_tag": "#P1", "action": "confirm"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert signup_resp.status == 200

    wait_resp = await asyncio.wait_for(wait_task, timeout=5)
    assert wait_resp.status == 200
    body = await wait_resp.json()
    assert body["changed"] is True


@pytest.mark.discord
@pytest.mark.asyncio
async def test_activity_closed_does_not_bump_enrollment_version(bridge_config, client, monkeypatch):
    """handle_post_cwl_activity_closed fires refresh_cwl_management_hub_message() unconditionally
    on every close, even when nothing changed — deliberately excluded from bump_enrollment_version
    (see that handler's own docstring) so a plain Cancel/back-gesture close doesn't wake every
    parked waiter for no reason."""
    import qapbot.web_bridge as web_bridge_module

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(8609, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    before = web_bridge_module._enrollment_version.get("8609", 0)

    resp = await client.post(
        "/api/cwl/activity-closed",
        json={"guild_id": 8609, "discord_user_id": 42},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200

    after = web_bridge_module._enrollment_version.get("8609", 0)
    assert after == before


# ---------------------------------------------------------------------------
# Player-stats lookup (2026-08-16) — GET /api/cwl/player-stats. The other half of the Manage
# Enrollment board's hover pop-up progressive fetch: missed CWL attacks + attack/defense star
# ratio, computed exactly as /leaderboard mode=missedattacks|attackdefratio cwl_only=true
# month=-3 scope=all would (project owner's spec, verbatim: "They should be calculated exactly as
# the /leaderboard command would do it..." / "the option scope=ALL is also important"). Same
# admin-or-leader gate as the enrollment screen itself; see
# tests/unit/test_leaderboard_scope_and_month_parsing.py's TestGetRecentCwlPlayerStats for the
# underlying computation's own coverage (including a fixed `now`, so it's deterministic) — these
# tests only check the HTTP wiring around it, so the seeded war's date is anchored to "today" to
# stay inside the real, un-overridable trailing-3-month window the endpoint actually computes
# against.
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_player_stats_get_rejects_missing_secret(bridge_config, client):
    resp = await client.get("/api/cwl/player-stats?guild_id=840&discord_user_id=42&player_tag=%23P1")
    assert resp.status == 403


@pytest.mark.discord
@pytest.mark.asyncio
async def test_player_stats_get_rejects_non_admin_non_leader(bridge_config, client, monkeypatch):
    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(840, 42, is_admin=False))

    resp = await client.get(
        "/api/cwl/player-stats?guild_id=840&discord_user_id=42&player_tag=%23P1",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 403


@pytest.mark.discord
@pytest.mark.asyncio
async def test_player_stats_no_history_returns_null_fields(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "841", {})
    CACHE.db_manager = db

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(841, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/player-stats?guild_id=841&discord_user_id=42&player_tag=%23NEVERSEEN",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body == {"seasons": [], "attacks": None, "missed_attacks": None, "attack_defense_ratio": None}


@pytest.mark.discord
@pytest.mark.asyncio
async def test_player_stats_returns_recent_cwl_stats(db, bridge_config, client, monkeypatch):
    from datetime import datetime, timezone

    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "842", {"#CLAN1": "Marines"})
    CACHE.db_manager = db
    now = datetime.now(timezone.utc)
    date = now.strftime("%Y-%m-01T08:00")
    # max_attacks=1 is what marks this a CWL war for the endpoint's own cwl_only filter — NOT
    # war_summary.is_cwl (see get_recent_cwl_player_stats' own docstring for why: it mirrors
    # calculate_leaderboard()'s cwl_only filter exactly, which reads Max_Attacks==1).
    await db.conn.execute(
        "INSERT INTO war_summary (war_id, clan_tag, opponent_tag, is_cwl, cwl_season, date) "
        "VALUES ('w1', '#CLAN1', '#OPP', 1, ?, ?)",
        (now.strftime("%Y-%m"), date),
    )
    await db.conn.execute(
        "INSERT INTO war_attacks "
        "(war_id, clan_tag, date, player_name, player_tag, th_level, map_position, attack_order, "
        " stars, missed_attacks, defensive_stars, max_attacks) "
        "VALUES ('w1', '#CLAN1', ?, 'Alpha', '#P1', 15, 1, 1, 3, 0, 1, 1)",
        (date,),
    )
    await db.conn.commit()

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(842, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/player-stats?guild_id=842&discord_user_id=42&player_tag=%23P1",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["attacks"] == 1
    assert body["missed_attacks"] == 0
    assert body["attack_defense_ratio"] == 3.0
    assert len(body["seasons"]) == 3
    assert body["seasons"][-1] == now.strftime("%Y-%m")


# ---------------------------------------------------------------------------
# 2026-08-17 (CWL_PROD_PERFORMANCE_FIX_PLAN.md P1 Step 7): _player_stats_cache TTL cache — the
# incident log showed get_recent_cwl_player_stats running ~100+ times in one session, almost
# always for the same handful of players a lead was scanning back and forth across. Each test
# calls clear_player_stats_cache() first and uses a player_tag not used by any other test in this
# file, since the cache is module-level (persists across tests) and keyed by player_tag alone.
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_player_stats_caches_repeat_lookups_for_same_tag(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE
    import qapbot.web_bridge as web_bridge_module
    import QBhelperfunctions

    web_bridge_module.clear_player_stats_cache()
    await _seed_guild_and_clans(db, "850", {})
    CACHE.db_manager = db

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(850, 42, is_admin=True))

    call_log: list = []
    real_fn = QBhelperfunctions.get_recent_cwl_player_stats

    def spy(player_tag, *args, **kwargs):
        call_log.append(player_tag)
        return real_fn(player_tag, *args, **kwargs)

    monkeypatch.setattr(QBhelperfunctions, "get_recent_cwl_player_stats", spy)

    resp1 = await client.get(
        "/api/cwl/player-stats?guild_id=850&discord_user_id=42&player_tag=%23CACHETEST1",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    resp2 = await client.get(
        "/api/cwl/player-stats?guild_id=850&discord_user_id=42&player_tag=%23CACHETEST1",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp1.status == 200 and resp2.status == 200
    assert (await resp1.json()) == (await resp2.json())
    assert call_log == ["#CACHETEST1"]


@pytest.mark.discord
@pytest.mark.asyncio
async def test_player_stats_cache_expires_after_ttl(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE
    import qapbot.web_bridge as web_bridge_module
    import QBhelperfunctions

    web_bridge_module.clear_player_stats_cache()
    await _seed_guild_and_clans(db, "851", {})
    CACHE.db_manager = db

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(851, 42, is_admin=True))

    call_log: list = []
    real_fn = QBhelperfunctions.get_recent_cwl_player_stats

    def spy(player_tag, *args, **kwargs):
        call_log.append(player_tag)
        return real_fn(player_tag, *args, **kwargs)

    monkeypatch.setattr(QBhelperfunctions, "get_recent_cwl_player_stats", spy)

    fake_now = [1_000.0]
    monkeypatch.setattr(web_bridge_module.time, "monotonic", lambda: fake_now[0])

    resp1 = await client.get(
        "/api/cwl/player-stats?guild_id=851&discord_user_id=42&player_tag=%23CACHETEST2",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp1.status == 200
    assert call_log == ["#CACHETEST2"]

    # Still within the TTL -> served from cache, no second underlying call.
    fake_now[0] += 60
    resp2 = await client.get(
        "/api/cwl/player-stats?guild_id=851&discord_user_id=42&player_tag=%23CACHETEST2",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp2.status == 200
    assert call_log == ["#CACHETEST2"]

    # Past the TTL -> real call again.
    fake_now[0] += web_bridge_module._PLAYER_STATS_CACHE_TTL_SECONDS + 1
    resp3 = await client.get(
        "/api/cwl/player-stats?guild_id=851&discord_user_id=42&player_tag=%23CACHETEST2",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp3.status == 200
    assert call_log == ["#CACHETEST2", "#CACHETEST2"]


@pytest.mark.discord
@pytest.mark.asyncio
async def test_player_stats_cache_cleared_by_clear_player_stats_cache(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE
    import qapbot.web_bridge as web_bridge_module
    import QBhelperfunctions

    web_bridge_module.clear_player_stats_cache()
    await _seed_guild_and_clans(db, "852", {})
    CACHE.db_manager = db

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(852, 42, is_admin=True))

    call_log: list = []
    real_fn = QBhelperfunctions.get_recent_cwl_player_stats

    def spy(player_tag, *args, **kwargs):
        call_log.append(player_tag)
        return real_fn(player_tag, *args, **kwargs)

    monkeypatch.setattr(QBhelperfunctions, "get_recent_cwl_player_stats", spy)

    resp1 = await client.get(
        "/api/cwl/player-stats?guild_id=852&discord_user_id=42&player_tag=%23CACHETEST3",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp1.status == 200
    assert call_log == ["#CACHETEST3"]

    web_bridge_module.clear_player_stats_cache()

    resp2 = await client.get(
        "/api/cwl/player-stats?guild_id=852&discord_user_id=42&player_tag=%23CACHETEST3",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp2.status == 200
    assert call_log == ["#CACHETEST3", "#CACHETEST3"]


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_guest_creates_signup_row(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "806", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["806"] = {"member_clans": ["#CLAN1"], "member_families": []}

    event_id = db.create_cwl_event_sync("806", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(806, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())
    send_dm_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("qapbot.QBdiscocmdshelper_cwl.send_cwl_signup_template_dm", send_dm_mock)

    resp = await client.post(
        "/api/cwl/enrollment/guest",
        json={
            "guild_id": 806, "discord_user_id": 42, "player_tag": "#guest1",
            "player_name": "GuestOne", "discord_id": None, "send_dm_on_save": False,
        },
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body == {"ok": True, "dm_sent": False}
    signup = db.get_cwl_signup_sync(event_id, "#GUEST1")
    assert signup is not None
    assert signup["player_name"] == "GuestOne"
    assert signup["source"] == "guest_invite"
    assert signup["status"] == "pending"
    send_dm_mock.assert_not_awaited()  # no discord_id given, and send_dm_on_save was False anyway


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_guest_sends_dm_when_requested_and_linked(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "807", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["807"] = {"member_clans": ["#CLAN1"], "member_families": []}

    event_id = db.create_cwl_event_sync("807", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(807, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())
    send_dm_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("qapbot.QBdiscocmdshelper_cwl.send_cwl_signup_template_dm", send_dm_mock)

    resp = await client.post(
        "/api/cwl/enrollment/guest",
        json={
            "guild_id": 807, "discord_user_id": 42, "player_tag": "#GUEST2",
            "player_name": "GuestTwo", "discord_id": "999", "send_dm_on_save": True,
        },
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body == {"ok": True, "dm_sent": True}
    send_dm_mock.assert_awaited_once()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_guest_no_event_returns_409(bridge_config, client, monkeypatch):
    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(808, 42, is_admin=True))

    resp = await client.post(
        "/api/cwl/enrollment/guest",
        json={
            "guild_id": 808, "discord_user_id": 42, "player_tag": "#GUEST3",
            "player_name": "GuestThree", "discord_id": None, "send_dm_on_save": False,
        },
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 409


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_guest_rejects_non_admin(bridge_config, client, monkeypatch):
    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(809, 42, is_admin=False))

    resp = await client.post(
        "/api/cwl/enrollment/guest",
        json={
            "guild_id": 809, "discord_user_id": 42, "player_tag": "#GUEST4",
            "player_name": "GuestFour", "discord_id": None, "send_dm_on_save": False,
        },
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 403


# ---------------------------------------------------------------------------
# Shared-clan owner-only eviction: POST /api/cwl/shared-clan/evict (2026-08-15)
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_shared_clan_evict_owner_removes_target(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "820", {"#CLAN1": "Alpha"})
    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES ('821')")
    await db.conn.commit()
    CACHE.db_manager = db
    CACHE.server_config["820"] = {}

    owner_event_id = db.create_cwl_event_sync("820", "2026-09", "111")
    target_event_id = db.create_cwl_event_sync("821", "2026-09", "222")
    db.set_cwl_event_clans_sync(owner_event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    db.set_cwl_event_clans_sync(target_event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    shared_clan_id = db.create_cwl_shared_clan_sync("#CLAN1", "2026-09", "820", owner_event_id, "unresolved_first_claimer")
    db.add_guild_to_shared_clan_sync(shared_clan_id, "820", owner_event_id)
    db.add_guild_to_shared_clan_sync(shared_clan_id, "821", target_event_id)

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(820, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    resp = await client.post(
        "/api/cwl/shared-clan/evict",
        json={"guild_id": 820, "discord_user_id": 42, "clan_tag": "#CLAN1", "target_guild_id": 821},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    remaining_guilds = {g["guild_id"] for g in db.list_cwl_shared_clan_guilds_sync(shared_clan_id)}
    assert remaining_guilds == {"820"}


@pytest.mark.discord
@pytest.mark.asyncio
async def test_shared_clan_evict_rejects_non_owner_guild(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "822", {"#CLAN1": "Alpha"})
    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES ('823')")
    await db.conn.commit()
    CACHE.db_manager = db
    CACHE.server_config["822"] = {}

    # Guild 823 is the owner; guild 822 (a mere admin of a different, non-owner attached guild)
    # tries to evict 823 — must be rejected even though 822's own admin check passes.
    owner_event_id = db.create_cwl_event_sync("823", "2026-09", "111")
    acting_event_id = db.create_cwl_event_sync("822", "2026-09", "222")
    db.set_cwl_event_clans_sync(acting_event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    shared_clan_id = db.create_cwl_shared_clan_sync("#CLAN1", "2026-09", "823", owner_event_id, "unresolved_first_claimer")
    db.add_guild_to_shared_clan_sync(shared_clan_id, "822", acting_event_id)
    db.add_guild_to_shared_clan_sync(shared_clan_id, "823", owner_event_id)

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(822, 42, is_admin=True))

    resp = await client.post(
        "/api/cwl/shared-clan/evict",
        json={"guild_id": 822, "discord_user_id": 42, "clan_tag": "#CLAN1", "target_guild_id": 823},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 403
    remaining_guilds = {g["guild_id"] for g in db.list_cwl_shared_clan_guilds_sync(shared_clan_id)}
    assert remaining_guilds == {"822", "823"}


@pytest.mark.discord
@pytest.mark.asyncio
async def test_shared_clan_evict_rejects_non_admin(bridge_config, client, monkeypatch):
    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(824, 42, is_admin=False))

    resp = await client.post(
        "/api/cwl/shared-clan/evict",
        json={"guild_id": 824, "discord_user_id": 42, "clan_tag": "#CLAN1", "target_guild_id": 825},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 403


@pytest.mark.discord
@pytest.mark.asyncio
async def test_clan_config_get_reports_shared_with_info(db, bridge_config, client, monkeypatch):
    """The Configure Participating Clans GET payload's new shared_with field (2026-08-15) — used
    by the frontend to show the shared badge and, for the owner, the evict affordance."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_current_cwl_season

    await _seed_guild_and_clans(db, "826", {"#CLAN1": "Alpha"})
    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES ('827')")
    await db.conn.commit()
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}}
    CACHE.server_config["826"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    season = resolve_current_cwl_season()
    event_id = db.create_cwl_event_sync("826", season, "111")
    other_event_id = db.create_cwl_event_sync("827", season, "222")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    db.set_cwl_event_clans_sync(other_event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    shared_clan_id = db.create_cwl_shared_clan_sync("#CLAN1", season, "826", event_id, "unresolved_first_claimer")
    db.add_guild_to_shared_clan_sync(shared_clan_id, "826", event_id)
    db.add_guild_to_shared_clan_sync(shared_clan_id, "827", other_event_id)

    import QBcore
    other_guild = MagicMock()
    other_guild.name = "Sister Guild"
    admin_member = MagicMock()
    admin_member.guild_permissions.administrator = True
    acting_guild = MagicMock()
    acting_guild.get_member = MagicMock(return_value=admin_member)
    acting_guild.fetch_member = AsyncMock(return_value=admin_member)
    bot = MagicMock()
    bot.get_guild = MagicMock(side_effect=lambda gid: acting_guild if gid == 826 else (other_guild if gid == 827 else None))
    monkeypatch.setattr(QBcore, "bot", bot)

    resp = await client.get(
        "/api/cwl/clan-config",
        params={"guild_id": "826", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["clans"][0]["shared_with"] == {
        "is_owner": True, "other_guild_ids": ["827"], "other_guild_names": ["Sister Guild"],
    }


@pytest.mark.discord
@pytest.mark.asyncio
async def test_clan_config_get_reports_shared_with_even_when_currently_unchecked(db, bridge_config, client, monkeypatch):
    """Live-testing feedback (2026-08-16): re-checking a previously-shared-but-deactivated
    clan's checkbox client-side never re-fetches anything — it only flips `clan.participating`
    locally — so shared_with must already be correct in the payload the moment it's fetched,
    regardless of whether `participating` happens to be true or false at that instant. Was
    previously gated on `participating`, so a guild that added a guest clan, saved, then
    unchecked and saved again, got a payload with shared_with=null for that clan on the very
    next load — even though the clan's real cwl_shared_clans record was untouched by any of that
    and it was still, in fact, shared."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "828", {"#CLAN1": "Alpha"})
    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES ('829')")
    await db.conn.commit()
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}}
    CACHE.server_config["828"] = {"member_clans": [], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    from qapbot.QBdiscocmdshelper_cwl import resolve_current_cwl_season
    season = resolve_current_cwl_season()
    event_id = db.create_cwl_event_sync("828", season, "111")
    other_event_id = db.create_cwl_event_sync("829", season, "222")
    # #CLAN1 exists as a row for guild 828 but is currently UNCHECKED — the exact state after
    # add -> save -> disable -> save.
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": False}])
    db.set_cwl_event_clans_sync(other_event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    shared_clan_id = db.create_cwl_shared_clan_sync("#CLAN1", season, "829", other_event_id, "unresolved_first_claimer")
    db.add_guild_to_shared_clan_sync(shared_clan_id, "828", event_id)
    db.add_guild_to_shared_clan_sync(shared_clan_id, "829", other_event_id)

    import QBcore
    other_guild = MagicMock()
    other_guild.name = "Owner Guild"
    admin_member = MagicMock()
    admin_member.guild_permissions.administrator = True
    acting_guild = MagicMock()
    acting_guild.get_member = MagicMock(return_value=admin_member)
    acting_guild.fetch_member = AsyncMock(return_value=admin_member)
    bot = MagicMock()
    bot.get_guild = MagicMock(side_effect=lambda gid: acting_guild if gid == 828 else (other_guild if gid == 829 else None))
    monkeypatch.setattr(QBcore, "bot", bot)

    resp = await client.get(
        "/api/cwl/clan-config",
        params={"guild_id": "828", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    clan = next(c for c in body["clans"] if c["clan_tag"] == "#CLAN1")
    assert clan["participating"] is False
    assert clan["shared_with"] == {
        "is_owner": False, "other_guild_ids": ["829"], "other_guild_names": ["Owner Guild"],
    }


@pytest.mark.discord
@pytest.mark.asyncio
async def test_clan_config_get_shows_owners_roster_size_for_follower(db, bridge_config, client, monkeypatch):
    """De-sync guard follow-up (2026-08-15, live-testing feedback): the Configure Participating
    Clans screen used to read each guild's own local roster_size/cwl_start_at even for an
    already-shared clan — unlike the Manage Enrollment board, which already deferred to the
    owner. A follower must see the OWNER's canonical settings here too, never its own stale
    local copy."""
    from qapbot.cache_manager import CACHE

    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}
    CACHE.server_config["840"] = {"member_clans": [], "member_families": []}
    CACHE.server_config["841"] = {"member_clans": [], "member_families": []}
    await _seed_shared_clan_pair(db, "840", "841")  # owner=840 (roster_size 30), follower=841 (15)

    import QBcore
    admin_member = MagicMock()
    admin_member.guild_permissions.administrator = True
    follower_guild = MagicMock()
    follower_guild.get_member = MagicMock(return_value=admin_member)
    follower_guild.fetch_member = AsyncMock(return_value=admin_member)
    owner_guild = MagicMock()
    owner_guild.name = "Owner Guild"
    bot = MagicMock()
    bot.get_guild = MagicMock(side_effect=lambda gid: follower_guild if gid == 841 else (owner_guild if gid == 840 else None))
    monkeypatch.setattr(QBcore, "bot", bot)

    resp = await client.get(
        "/api/cwl/clan-config", params={"guild_id": "841", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    clan = next(c for c in body["clans"] if c["clan_tag"] == "#CLAN1")
    assert clan["roster_size"] == 30


@pytest.mark.discord
@pytest.mark.asyncio
async def test_clan_config_post_from_follower_cannot_diverge_shared_settings(db, bridge_config, client, monkeypatch):
    """The write-side half of the same guard: even if a follower's form somehow still submits a
    different roster_size/cwl_start_at for an already-shared clan, the save must silently keep
    the owner's canonical values — never persist a divergent local copy."""
    from qapbot.cache_manager import CACHE

    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}
    CACHE.server_config["842"] = {"member_clans": [], "member_families": []}
    CACHE.server_config["843"] = {"member_clans": [], "member_families": []}
    shared_clan_id, owner_event_id, follower_event_id = await _seed_shared_clan_pair(db, "842", "843")

    import QBcore
    admin_member = MagicMock()
    admin_member.guild_permissions.administrator = True
    follower_guild = MagicMock()
    follower_guild.get_member = MagicMock(return_value=admin_member)
    follower_guild.fetch_member = AsyncMock(return_value=admin_member)
    owner_guild = MagicMock()
    owner_guild.name = "Owner Guild"
    bot = MagicMock()
    bot.get_guild = MagicMock(side_effect=lambda gid: follower_guild if gid == 843 else (owner_guild if gid == 842 else None))
    monkeypatch.setattr(QBcore, "bot", bot)
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    resp = await client.post(
        "/api/cwl/clan-config",
        json={
            "guild_id": 843, "discord_user_id": 42,
            "clans": [{"clan_tag": "#CLAN1", "participating": True, "roster_size": 99, "cwl_start_at": "2026-09-05T12:00Z"}],
        },
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    follower_row = db.get_cwl_event_clans_sync(follower_event_id)[0]
    assert follower_row["roster_size"] == 30  # owner's value, not the submitted 99
    assert follower_row["cwl_start_at"] != "2026-09-05T12:00Z"
    # The owner's own row is untouched by the follower's save.
    owner_row = db.get_cwl_event_clans_sync(owner_event_id)[0]
    assert owner_row["roster_size"] == 30


# ---------------------------------------------------------------------------
# Live shared roster (slice 4, 2026-08-15) — both guilds' boards render the identical live
# roster for a shared clan, and edits from either side apply to the same underlying data.
# ---------------------------------------------------------------------------

async def _seed_shared_clan_pair(db: WarHistoryDB, owner_guild_id: str, follower_guild_id: str, clan_tag: str = "#CLAN1"):
    await _seed_guild_and_clans(db, owner_guild_id, {clan_tag: "Alpha"})
    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (follower_guild_id,))
    await db.conn.commit()
    owner_event_id = db.create_cwl_event_sync(owner_guild_id, "2026-09", "111")
    follower_event_id = db.create_cwl_event_sync(follower_guild_id, "2026-09", "222")
    db.set_cwl_event_clans_sync(owner_event_id, [{"clan_tag": clan_tag, "participating": True, "roster_size": 30}])
    db.set_cwl_event_clans_sync(follower_event_id, [{"clan_tag": clan_tag, "participating": True, "roster_size": 15}])
    shared_clan_id = db.create_cwl_shared_clan_sync(clan_tag, "2026-09", owner_guild_id, owner_event_id, "unresolved_first_claimer")
    db.add_guild_to_shared_clan_sync(shared_clan_id, owner_guild_id, owner_event_id)
    db.add_guild_to_shared_clan_sync(shared_clan_id, follower_guild_id, follower_event_id)
    return shared_clan_id, owner_event_id, follower_event_id


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_get_uses_owner_roster_size_for_shared_clan(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}
    CACHE.server_config["830"] = {"member_clans": [], "member_families": []}
    CACHE.server_config["831"] = {"member_clans": [], "member_families": []}
    await _seed_shared_clan_pair(db, "830", "831")  # owner=830 (roster_size 30), follower=831 (15)

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(831, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/enrollment", params={"guild_id": "831", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    # Follower's board shows the OWNER's roster_size (30), not its own local row's (15).
    assert body["clans"][0]["roster_size"] == 30


@pytest.mark.discord
@pytest.mark.asyncio
async def test_shared_clan_add_remove_readd_flow_stays_consistent(db, bridge_config, client, monkeypatch):
    """End-to-end regression test for the exact live-testing sequence that surfaced the ownership
    bug (2026-08-15, project owner's spec: "check this flow that all statuses in each edge case...
    get updated correctly"): guild 851 (1) adds a clan that's already guild 850's real home clan
    and saves, (2) removes it and saves again, (3) starts its own enrollment (moving its event
    past 'draft'), then (4) re-adds the same clan and saves once more. Ownership must stay with
    850 (the real home guild) throughout, 851 must be cleanly detached after step 2 (not left
    dangling as "still sharing" something it turned off), and the clan's current members must end
    up visible with a real status after step 4 even though most of them have no prior CWL history
    in this exact clan (the auto-assign-on-add visibility-seed fix)."""
    from qapbot.cache_manager import CACHE

    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}
    CACHE.server_config["850"] = {"member_clans": ["#CLAN1"], "member_families": []}  # real home
    CACHE.server_config["851"] = {"member_clans": [], "member_families": []}  # acting/guest guild

    await _seed_guild_and_clans(db, "850", {"#CLAN1": "Alpha"})
    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES ('851')")
    await db.conn.commit()
    home_event_id = db.create_cwl_event_sync("850", "2026-09", "1")
    acting_event_id = db.create_cwl_event_sync("851", "2026-09", "2")
    db.set_cwl_event_clans_sync(home_event_id, [{"clan_tag": "#CLAN1", "participating": True, "roster_size": 30}])

    # A handful of current #CLAN1 members: #P1 has prior CWL history in this exact clan
    # (qualifies for auto-assignment once the roster's empty); #P2/#P3 don't (visibility-seed
    # only).
    for tag, discord_id in (("#P1", "10"), ("#P2", "11"), ("#P3", "12")):
        await _seed_current_clan_member(db, discord_id, tag, "#CLAN1")
    await db.conn.execute(
        "INSERT INTO war_summary (war_id, clan_tag, opponent_tag, is_cwl, cwl_season, date) "
        "VALUES ('war1', '#CLAN1', '#OPP', 1, '2026-08', '2026-08-01T10:00')"
    )
    await db.conn.execute(
        "INSERT INTO war_attacks (war_id, clan_tag, date, player_name, player_tag, th_level, map_position, stars, attack_order) "
        "VALUES ('war1', '#CLAN1', '2026-08-01T10:00', 'P1', '#P1', 15, 1, 2, 1)"
    )
    await db.conn.commit()

    # #LEADER is #CLAN1's real in-game Leader, linked, a Discord member of the HOME guild (850)
    # only — never joins 851 at all, so ownership must resolve to 850 unambiguously.
    await _seed_current_clan_member(db, "999", "#LEADER", "#CLAN1")
    await db.conn.execute("UPDATE user_players SET verified = 1 WHERE player_tag = '#LEADER'")
    await db.conn.commit()

    import QBcore
    fake_coc_cache = MagicMock()
    fake_clan = MagicMock()
    leader_role = MagicMock()
    leader_role.name = "leader"
    leader_member = MagicMock()
    leader_member.tag = "#LEADER"
    leader_member.role = leader_role
    fake_clan.members = [leader_member]
    fake_coc_cache.get_clan = AsyncMock(return_value=fake_clan)
    monkeypatch.setattr(CACHE, "coc_clan_cache", fake_coc_cache)

    admin_member = MagicMock()
    admin_member.guild_permissions.administrator = True
    acting_guild = MagicMock()
    acting_guild.get_member = MagicMock(return_value=admin_member)
    acting_guild.fetch_member = AsyncMock(return_value=admin_member)

    def _home_get_member(discord_id):
        return MagicMock() if str(discord_id) == "999" else None

    home_guild = MagicMock()
    home_guild.name = "Home Guild"
    home_guild.get_member = MagicMock(side_effect=_home_get_member)
    home_guild.fetch_member = AsyncMock(side_effect=_home_get_member)

    bot = MagicMock()
    bot.get_guild = MagicMock(side_effect=lambda gid: acting_guild if gid == 851 else (home_guild if gid == 850 else None))
    monkeypatch.setattr(QBcore, "bot", bot)
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())
    monkeypatch.setattr("qapbot.ui_cwl_roster.notify_cwl_clan_shared", AsyncMock())

    async def _save(participating: bool) -> None:
        resp = await client.post(
            "/api/cwl/clan-config",
            json={
                "guild_id": 851, "discord_user_id": 42,
                "clans": [{"clan_tag": "#CLAN1", "participating": participating, "roster_size": 15}],
            },
            headers={"X-Bridge-Secret": "test-secret"},
        )
        assert resp.status == 200

    # Step 1: add #CLAN1 (still 'draft' — auto-assign doesn't run yet) and save.
    await _save(True)
    shared = db.get_cwl_shared_clan_sync("#CLAN1", "2026-09")
    assert shared is not None
    assert shared["owner_guild_id"] == "850"  # real home guild, not the acting one
    guild_ids = {g["guild_id"] for g in db.list_cwl_shared_clan_guilds_sync(shared["id"])}
    assert guild_ids == {"850", "851"}

    # Step 2: remove (uncheck) #CLAN1 and save — 851 must cleanly detach.
    await _save(False)
    guild_ids = {g["guild_id"] for g in db.list_cwl_shared_clan_guilds_sync(shared["id"])}
    assert guild_ids == {"850"}
    still_shared = db.get_cwl_shared_clan_sync("#CLAN1", "2026-09")
    assert still_shared is not None  # home guild still attached — record survives
    assert still_shared["owner_guild_id"] == "850"  # unaffected, 851 was never the owner

    # Step 3: 851 starts its own enrollment (moves its event past 'draft').
    db.update_cwl_event_status_sync(acting_event_id, "signup_open")

    # Step 4: re-add #CLAN1 and save — must re-attach, keep the SAME owner, apply the roster_size
    # guard, and seed a status for every current member (auto-assigned or visibility-seeded).
    await _save(True)
    guild_ids = {g["guild_id"] for g in db.list_cwl_shared_clan_guilds_sync(shared["id"])}
    assert guild_ids == {"850", "851"}
    reattached = db.get_cwl_shared_clan_sync("#CLAN1", "2026-09")
    assert reattached["owner_guild_id"] == "850"  # still the real home guild

    # roster_size guard: 851 submitted 15, but 850 (owner) has 30 — 850's value wins.
    acting_row = db.get_cwl_event_clans_sync(acting_event_id)[0]
    assert acting_row["roster_size"] == 30

    shared_players = {p["player_tag"]: p for p in db.get_cwl_shared_clan_players_sync(shared["id"])}
    assert shared_players["#P1"]["assigned"] == 1  # prior CWL history in this exact clan -> placed
    # A placement write never touches status (2026-08-16: status/assigned are deliberately
    # separate columns) — #P1 never actually responded, so it stays at the honest default.
    assert shared_players["#P1"]["status"] == "pending"
    assert shared_players["#P1"]["source"] == "auto_assigned"
    assert shared_players["#P2"]["status"] == "pending"  # visibility-seeded, no prior history
    assert shared_players["#P2"]["assigned"] == 0
    assert shared_players["#P2"]["source"] == "auto_seeded"
    assert shared_players["#P3"]["status"] == "pending"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_foreign_guest_conversion_and_purge_end_to_end_through_real_endpoints(db, bridge_config, client, monkeypatch):
    """Full HTTP-level regression test for the exact scenario the project owner described
    (2026-08-15): guild 853 adds guild 852's real clan #CLAN1 as a guest, drags one of #CLAN1's
    REAL members into its own private clan #PRIVATE, then detaches #CLAN1 — that player should
    "become a guest player automatically" and linger in #PRIVATE (still visible, still assigned).
    Only once #CLAN1's real owning guild (852) later reassigns that exact player elsewhere should
    they disappear from guild 853's roster AND pool entirely — verified through the actual
    POST /api/cwl/clan-config and POST /api/cwl/enrollment/assign endpoints, not by calling the
    underlying helpers directly, to prove the wiring itself works end to end."""
    from qapbot.cache_manager import CACHE

    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}, "#PRIVATE": {"name": "Beta"}}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}
    CACHE.server_config["852"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.server_config["853"] = {"member_clans": ["#PRIVATE"], "member_families": []}
    shared_clan_id, owner_event_id, follower_event_id = await _seed_shared_clan_pair(db, "852", "853")
    await _seed_guild_and_clans(db, "853", {"#PRIVATE": "Beta"})
    db.set_cwl_event_clans_sync(follower_event_id, [
        {"clan_tag": "#CLAN1", "participating": True}, {"clan_tag": "#PRIVATE", "participating": True},
    ])
    await _seed_current_clan_member(db, "77", "#REALMEMBER", "#CLAN1")  # real #CLAN1 member

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(853, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    # Step 1: guild 853 drags #REALMEMBER (a real #CLAN1 member) into its own private #PRIVATE.
    resp = await client.post(
        "/api/cwl/enrollment/assign",
        json={"guild_id": 853, "discord_user_id": 42, "player_tag": "#REALMEMBER", "clan_tag": "#PRIVATE"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    assignments = {a["player_tag"]: a["assigned_clan_tag"] for a in db.get_cwl_assignments_sync(follower_event_id)}
    assert assignments["#REALMEMBER"] == "#PRIVATE"

    # Step 2: guild 853 detaches #CLAN1 (unchecks it and saves).
    resp = await client.post(
        "/api/cwl/clan-config",
        json={"guild_id": 853, "discord_user_id": 42, "clans": [
            {"clan_tag": "#CLAN1", "participating": False, "roster_size": 15},
            {"clan_tag": "#PRIVATE", "participating": True, "roster_size": 15},
        ]},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    # Lingers, now as a guest — matches "that is ok from my view so far."
    signup = db.get_cwl_signup_sync(follower_event_id, "#REALMEMBER")
    assert signup is not None
    assert signup["source"] == "guest_invite"
    assert signup["origin_shared_clan_id"] == shared_clan_id
    assignments = {a["player_tag"]: a["assigned_clan_tag"] for a in db.get_cwl_assignments_sync(follower_event_id)}
    assert assignments["#REALMEMBER"] == "#PRIVATE"  # still there

    # Step 3: #CLAN1's real owning guild (852) reassigns #REALMEMBER elsewhere on ITS OWN board.
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(852, 43, is_admin=True))
    resp = await client.post(
        "/api/cwl/enrollment/assign",
        json={"guild_id": 852, "discord_user_id": 43, "player_tag": "#REALMEMBER", "clan_tag": None},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200

    # Guild 853's foreign placement is now gone entirely — not just unassigned.
    assert db.get_cwl_signup_sync(follower_event_id, "#REALMEMBER") is None
    assignments = {a["player_tag"] for a in db.get_cwl_assignments_sync(follower_event_id)}
    assert "#REALMEMBER" not in assignments


@pytest.mark.discord
@pytest.mark.asyncio
async def test_drag_out_of_orphaned_column_survives_clan_reactivation(db, bridge_config, client, monkeypatch):
    """Regression test for a real live-testing bug (2026-08-16): a player confirmed in a shared
    clan's roster (e.g. dragged there by an admin) who then gets dragged OUT of the "Assigned to
    other Guild" pseudo-column into one of this guild's own private clans, while that shared
    clan is currently deactivated (not a participating column here at all) — must have their
    cwl_shared_clan_players row actually cleared, not just their local assignment overwritten.
    Before the fix, the removal loop only ever checked CURRENTLY-PARTICIPATING shared clans, so a
    deactivated origin clan's row silently survived — and reactivating that clan later
    (sync_cwl_shared_clan_roster_to_local_pools) snapped the player right back to it, undoing the
    reassignment."""
    from qapbot.cache_manager import CACHE

    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "StayCalm"}, "#QCREW": {"name": "The QCrew"}}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}
    CACHE.server_config["860"] = {"member_clans": ["#QCREW"], "member_families": []}
    CACHE.server_config["861"] = {"member_clans": ["#CLAN1"], "member_families": []}
    shared_clan_id, owner_event_id, follower_event_id = await _seed_shared_clan_pair(db, "861", "860")
    await _seed_guild_and_clans(db, "860", {"#QCREW": "The QCrew"})
    db.set_cwl_event_clans_sync(follower_event_id, [
        {"clan_tag": "#CLAN1", "participating": True}, {"clan_tag": "#QCREW", "participating": True},
    ])
    # QManiac was deliberately drag-assigned into the shared clan at some point (admin_override).
    db.set_cwl_shared_clan_player_assignment_sync(shared_clan_id, "#QMANIAC", "QManiac", "55", True, "admin_override", "860")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(860, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    # Step 1: guild 860 detaches #CLAN1 — QManiac becomes an orphaned local assignment there.
    resp = await client.post(
        "/api/cwl/clan-config",
        json={"guild_id": 860, "discord_user_id": 42, "clans": [
            {"clan_tag": "#CLAN1", "participating": False, "roster_size": 15},
            {"clan_tag": "#QCREW", "participating": True, "roster_size": 15},
        ]},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    assignments = {a["player_tag"]: a["assigned_clan_tag"] for a in db.get_cwl_assignments_sync(follower_event_id)}
    assert assignments["#QMANIAC"] == "#CLAN1"  # orphaned, "Assigned to other Guild"

    # Step 2: drag QManiac OUT of the orphaned column into #QCREW — #CLAN1 is NOT participating
    # for guild 860 right now, so only the player-scoped lookup can find their shared-roster row.
    resp = await client.post(
        "/api/cwl/enrollment/assign",
        json={"guild_id": 860, "discord_user_id": 42, "player_tag": "#QMANIAC", "clan_tag": "#QCREW"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    assignments = {a["player_tag"]: a["assigned_clan_tag"] for a in db.get_cwl_assignments_sync(follower_event_id)}
    assert assignments["#QMANIAC"] == "#QCREW"
    shared_players = {p["player_tag"] for p in db.get_cwl_shared_clan_players_sync(shared_clan_id)}
    assert "#QMANIAC" not in shared_players  # actually cleared, not just shadowed

    # Step 3: re-activate #CLAN1 — must NOT snap QManiac back to it.
    resp = await client.post(
        "/api/cwl/clan-config",
        json={"guild_id": 860, "discord_user_id": 42, "clans": [
            {"clan_tag": "#CLAN1", "participating": True, "roster_size": 15},
            {"clan_tag": "#QCREW", "participating": True, "roster_size": 15},
        ]},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200

    resp = await client.get(
        "/api/cwl/enrollment", params={"guild_id": "860", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    qmaniac = next(p for p in body["players"] if p["player_tag"] == "#QMANIAC")
    assert qmaniac["assigned_clan_tag"] == "#QCREW"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_get_shows_owners_signup_status_for_unassigned_shared_clan_member(
    db, bridge_config, client, monkeypatch,
):
    """De-sync guard follow-up (2026-08-15, project owner's spec: "the enrollment status per
    player is global... no harm to show the enrollment status also for players from other
    clans/guilds") — a current member of a shared clan who isn't assigned into the shared roster
    yet (so the roster-merge override below doesn't touch them) still sits in the general pool via
    plain current-membership; if the OWNER guild's own cwl_signups already recorded a real status
    for that same real player, show it instead of a hardcoded blank."""
    from qapbot.cache_manager import CACHE

    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}
    CACHE.server_config["844"] = {"member_clans": [], "member_families": []}
    CACHE.server_config["845"] = {"member_clans": [], "member_families": []}
    shared_clan_id, owner_event_id, _ = await _seed_shared_clan_pair(db, "844", "845")
    # #P1 is a current member of the shared clan and already confirmed via the OWNER guild's own
    # Start Enrollment — but was never assigned into cwl_shared_clan_players (still unassigned).
    db.upsert_cwl_signup_sync(owner_event_id, "#P1", "Alpha1", "999", None, "template_confirm", "confirmed")
    await _seed_current_clan_member(db, "999", "#P1", "#CLAN1")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(845, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/enrollment", params={"guild_id": "845", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    player = next(p for p in body["players"] if p["player_tag"] == "#P1")
    assert player["signup_status"] == "confirmed"
    assert player["assigned_clan_tag"] is None  # still genuinely unassigned, just status-enriched


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_get_shows_identical_roster_from_either_guild(db, bridge_config, client, monkeypatch):
    """The core claim of this feature: a player assigned to the shared clan via one guild's
    board shows up identically when the OTHER guild builds its own payload."""
    from qapbot.cache_manager import CACHE

    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}
    CACHE.server_config["832"] = {"member_clans": [], "member_families": []}
    CACHE.server_config["833"] = {"member_clans": [], "member_families": []}
    shared_clan_id, _, _ = await _seed_shared_clan_pair(db, "832", "833")
    # Genuinely placed AND genuinely confirmed — two separate writes, matching how these two
    # facts are actually recorded in production (2026-08-16: status and assigned are deliberately
    # separate columns, see cwl_shared_clan_players' own CREATE TABLE comment).
    db.set_cwl_shared_clan_player_assignment_sync(shared_clan_id, "#P1", "Alpha1", "999", True, "guest_invite", "832")
    db.set_cwl_shared_clan_player_status_sync(shared_clan_id, "#P1", "Alpha1", "999", "confirmed", "guest_invite", "832")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(833, 42, is_admin=True))

    # Fetching guild 833's (the follower's) own board.
    resp = await client.get(
        "/api/cwl/enrollment", params={"guild_id": "833", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    body = await resp.json()
    players_by_tag = {p["player_tag"]: p for p in body["players"]}
    assert players_by_tag["#P1"]["assigned_clan_tag"] == "#CLAN1"
    assert players_by_tag["#P1"]["signup_status"] == "confirmed"
    assert players_by_tag["#P1"]["is_guest"] is True


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_get_shows_auto_assigned_shared_guest_as_pending_not_confirmed(
    db, bridge_config, client, monkeypatch
):
    """2026-08-16, live-testing feedback, project owner's spec, verbatim: "i just added staycalm
    as a guest clan to the qcrew's clan roster. all the players in staycalm's player roster have
    the 'Confirmed' status set. This is a bug. Since I'm only testing in dev no one of those
    players could possibly have confirmed their participation." Follow-up, verbatim: "Confirmation
    status and assignment status should be treated completely separate." A player placed via
    assign_cwl_player_sync's non-deliberate auto-assign path (source='auto_assigned') is written
    with set_cwl_shared_clan_player_assignment_sync, which never touches `status` at all — the
    board must show the honest 'pending' default (nobody actually responded), while the player
    still lands correctly in the column via the separate `assigned` column."""
    from qapbot.cache_manager import CACHE

    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}
    CACHE.server_config["834"] = {"member_clans": [], "member_families": []}
    CACHE.server_config["835"] = {"member_clans": [], "member_families": []}
    shared_clan_id, _, _ = await _seed_shared_clan_pair(db, "834", "835")
    db.set_cwl_shared_clan_player_assignment_sync(shared_clan_id, "#P1", "Alpha1", "999", True, "auto_assigned", "834")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(835, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/enrollment", params={"guild_id": "835", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    body = await resp.json()
    players_by_tag = {p["player_tag"]: p for p in body["players"]}
    assert players_by_tag["#P1"]["assigned_clan_tag"] == "#CLAN1"  # still correctly placed
    assert players_by_tag["#P1"]["signup_status"] == "pending"  # never actually confirmed


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_signup_confirm_writes_to_shared_table_for_shared_clan_player(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    CACHE.db_manager = db
    CACHE.server_config["834"] = {"member_clans": [], "member_families": []}
    CACHE.server_config["835"] = {"member_clans": [], "member_families": []}
    shared_clan_id, owner_event_id, follower_event_id = await _seed_shared_clan_pair(db, "834", "835")
    db.set_cwl_shared_clan_player_status_sync(shared_clan_id, "#P1", "Alpha1", "999", "pending", "guest_invite", "834")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(835, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    # The FOLLOWER guild (835) confirms the player — must write to the shared table, not its
    # own (empty) local cwl_signups.
    resp = await client.post(
        "/api/cwl/enrollment/signup",
        json={"guild_id": 835, "discord_user_id": 42, "player_tag": "#P1", "action": "confirm"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    shared_players = {p["player_tag"]: p for p in db.get_cwl_shared_clan_players_sync(shared_clan_id)}
    assert shared_players["#P1"]["status"] == "confirmed"
    assert shared_players["#P1"]["added_by_guild_id"] == "835"
    # De-sync guard (2026-08-15): the follower's own local cwl_signups now gets a mirrored
    # placeholder row too, so any local-only feature that reads its cwl_signups directly (not via
    # the shared-table merge) still knows this player exists — see
    # sync_cwl_shared_clan_roster_to_local_pools's docstring.
    mirrored = db.get_cwl_signup_sync(follower_event_id, "#P1")
    assert mirrored["status"] == "confirmed"
    assert mirrored["source"] == "guest_invite"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_assign_to_shared_clan_writes_to_shared_table(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    CACHE.db_manager = db
    CACHE.server_config["836"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.server_config["837"] = {"member_clans": [], "member_families": []}
    shared_clan_id, owner_event_id, follower_event_id = await _seed_shared_clan_pair(db, "836", "837")
    await _seed_current_clan_member(db, "999", "#P1", "#CLAN1")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(836, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    resp = await client.post(
        "/api/cwl/enrollment/assign",
        json={"guild_id": 836, "discord_user_id": 42, "player_tag": "#P1", "clan_tag": "#CLAN1"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    shared_players = {p["player_tag"]: p for p in db.get_cwl_shared_clan_players_sync(shared_clan_id)}
    assert shared_players["#P1"]["assigned"] == 1  # placed via drag-and-drop
    # A placement write never touches status (2026-08-16: the two are deliberately separate
    # columns) — this player never actually responded, so it stays at the honest default.
    assert shared_players["#P1"]["status"] == "pending"
    assert shared_players["#P1"]["player_name"] == "Player"
    # Never created a stale LOCAL assignment row for the owner's own event.
    assert db.get_cwl_assignments_sync(owner_event_id) == []
    # De-sync guard (2026-08-15): the FOLLOWER guild's own local cwl_signups gets a mirrored
    # placeholder too, even though it never independently signed this player up.
    mirrored = db.get_cwl_signup_sync(follower_event_id, "#P1")
    assert mirrored["status"] == "pending"
    assert mirrored["source"] == "guest_invite"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_assign_away_from_shared_clan_removes_shared_row(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    CACHE.db_manager = db
    CACHE.server_config["838"] = {"member_clans": [], "member_families": []}
    CACHE.server_config["839"] = {"member_clans": [], "member_families": []}
    shared_clan_id, owner_event_id, _ = await _seed_shared_clan_pair(db, "838", "839")
    db.set_cwl_shared_clan_player_assignment_sync(shared_clan_id, "#P1", "Alpha1", "999", True, "guest_invite", "838")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(838, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    # Drag to Unassigned (clan_tag: null).
    resp = await client.post(
        "/api/cwl/enrollment/assign",
        json={"guild_id": 838, "discord_user_id": 42, "player_tag": "#P1", "clan_tag": None},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    assert db.get_cwl_shared_clan_players_sync(shared_clan_id) == []


# ---------------------------------------------------------------------------
# POST /api/cwl/activity-closed (2026-08-16, live-testing feedback: on iPad, the Hub message's
# launch buttons stayed visibly disabled after closing the Activity — Discord's own client-side
# "an Activity was launched from this message" state, only ever incidentally cleared before by
# the save flow's existing Hub-message refresh). Fired on every close, not just after a save.
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_activity_closed_refreshes_hub_message(db, bridge_config, client, monkeypatch):
    import QBcore

    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(870, 42, is_admin=True))
    refresh_mock = AsyncMock()
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", refresh_mock)

    resp = await client.post(
        "/api/cwl/activity-closed",
        json={"guild_id": 870, "discord_user_id": 42},
        headers={"X-Bridge-Secret": "test-secret"},
    )

    assert resp.status == 200
    body = await resp.json()
    assert body == {"ok": True}
    refresh_mock.assert_awaited_once_with(870, "cwl_management")


@pytest.mark.discord
@pytest.mark.asyncio
async def test_activity_closed_rejects_missing_secret(bridge_config, client):
    resp = await client.post(
        "/api/cwl/activity-closed",
        json={"guild_id": 870, "discord_user_id": 42},
    )
    assert resp.status == 403


@pytest.mark.discord
@pytest.mark.asyncio
async def test_activity_closed_rejects_non_admin_non_leader(db, bridge_config, client, monkeypatch):
    import QBcore

    non_admin_member = MagicMock()
    non_admin_member.guild_permissions.administrator = False
    non_admin_member.roles = []
    guild = MagicMock()
    guild.get_member = MagicMock(return_value=non_admin_member)
    guild.fetch_member = AsyncMock(return_value=non_admin_member)
    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)
    monkeypatch.setattr(QBcore, "bot", bot)

    resp = await client.post(
        "/api/cwl/activity-closed",
        json={"guild_id": 870, "discord_user_id": 42},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 403


@pytest.mark.discord
@pytest.mark.asyncio
async def test_activity_closed_never_fails_when_hub_refresh_errors(db, bridge_config, client, monkeypatch):
    """Best-effort — a Hub-refresh failure (e.g. no Hub message configured) must not turn into
    an error response; the Activity is already closing by the time this fires regardless."""
    import QBcore

    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(870, 42, is_admin=True))
    monkeypatch.setattr(
        "qapbot.ui_cwl_roster.refresh_cwl_management_hub_message",
        AsyncMock(side_effect=RuntimeError("boom")),
    )

    resp = await client.post(
        "/api/cwl/activity-closed",
        json={"guild_id": 870, "discord_user_id": 42},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
