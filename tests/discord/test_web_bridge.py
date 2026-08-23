"""Tests for the CWL clan-config web bridge (CWL_CLAN_CONFIG_ACTIVITY_PLAN.md Phase B):
the shared-secret gate, the guild-admin re-verification (including the configured
super-admin bypass), and the GET/POST clan-config endpoints' actual behavior.
"""
# pyright: reportPrivateUsage=false, reportOptionalMemberAccess=false, reportArgumentType=false
# pyright: reportAttributeAccessIssue=false, reportUnusedVariable=false, reportDeprecated=false
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


def _seed_player_names(db: WarHistoryDB, names: Dict[str, str]) -> None:
    """Seeds the real player_name_index/player_name_search/player_name_fts DB tables from a
    plain {tag: name} dict (2026-08-18, PLAYER_NAME_INDEX_RETIREMENT_PLAN.md Steps 5-6 —
    replaces the old in-memory CACHE.player_name_index fixture helper now that the guest
    search's `#` tag-mode and name-substring paths are unconditionally SQL-backed)."""
    db.update_player_name_index_sync([(tag, name, "2026-08-17T00:00") for tag, name in names.items()])


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
# GET /api/i18n — plans/cwl-personal-hub.md Phase 6c
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_i18n_requires_secret(bridge_config, client):
    resp = await client.get("/api/i18n", params={"guild_id": "1", "discord_user_id": "2", "ns": "cwl.player_hub"})
    assert resp.status == 403


@pytest.mark.discord
@pytest.mark.asyncio
async def test_i18n_missing_params_returns_400(bridge_config, client):
    headers = {"X-Bridge-Secret": "test-secret"}
    resp = await client.get("/api/i18n", params={"discord_user_id": "2", "ns": "cwl.player_hub"}, headers=headers)
    assert resp.status == 400
    resp2 = await client.get("/api/i18n", params={"guild_id": "1", "discord_user_id": "2"}, headers=headers)
    assert resp2.status == 400


@pytest.mark.discord
@pytest.mark.asyncio
async def test_i18n_defaults_to_english_with_no_preference_set(bridge_config, client):
    from qapbot.cache_manager import CACHE

    CACHE.server_config["901"] = {}
    CACHE.user_accounts.pop("902", None)

    resp = await client.get(
        "/api/i18n",
        params={"guild_id": "901", "discord_user_id": "902", "ns": "cwl.player_hub"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["lang"] == "en"
    assert body["strings"]["title"] == "🛡️ Your CWL Preferences"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_i18n_honours_guild_language_over_default(bridge_config, client):
    from qapbot.cache_manager import CACHE

    CACHE.server_config["903"] = {"language": "de"}
    CACHE.user_accounts.pop("904", None)

    resp = await client.get(
        "/api/i18n",
        params={"guild_id": "903", "discord_user_id": "904", "ns": "cwl.player_hub"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    body = await resp.json()
    assert body["lang"] == "de"
    assert body["strings"]["title"] == "🛡️ Deine CWL-Einstellungen"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_i18n_user_language_wins_over_guild_language(bridge_config, client):
    """The exact chain t() itself uses — a member's own preference beats the guild's, matching
    what a DM to the same member would show them."""
    from qapbot.cache_manager import CACHE

    CACHE.server_config["905"] = {"language": "en"}
    CACHE.user_accounts["906"] = {"user_language": "de"}

    resp = await client.get(
        "/api/i18n",
        params={"guild_id": "905", "discord_user_id": "906", "ns": "cwl.player_hub"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    body = await resp.json()
    assert body["lang"] == "de"
    assert body["strings"]["title"] == "🛡️ Deine CWL-Einstellungen"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_i18n_unknown_namespace_returns_empty_strings_not_an_error(bridge_config, client):
    from qapbot.cache_manager import CACHE

    CACHE.server_config["907"] = {}
    CACHE.user_accounts.pop("908", None)

    resp = await client.get(
        "/api/i18n",
        params={"guild_id": "907", "discord_user_id": "908", "ns": "this.does.not.exist"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["strings"] == {}


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
        "is_guest": False,  # #CLAN1 is in this guild's member_clans
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
# Rule b/d (2026-08-18, CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md): a clan newly added to the
# roster must seed the pool regardless of whether it's checked (participating) or not — pool
# membership must never depend on the checkbox, only the auto-assignment TARGET does.
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_adding_a_guest_clan_unchecked_still_seeds_the_player_pool(db, bridge_config, client, monkeypatch):
    """Bug fixed 2026-08-18 (live-tested in DEV, project owner's report, verbatim: "the AKATSUKI
    guest clan being in the list but unchecked for that clan roster the members of that clan
    should be in the player pool but ... they aren't"). Adding a brand-new guest clan to the
    roster while leaving it UNCHECKED (participating=False), on an event that's already past
    draft, must still seed its current members into the pool — just without actually assigning
    anyone into its (non-existent) column."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "840", {"#CLAN1": "Alpha", "#AKATSUKI": "Akatsuki"})
    CACHE.db_manager = db
    CACHE.server_config["840"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    event_id = db.create_cwl_event_sync("840", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    db.update_cwl_event_status_sync(event_id, "signup_open")  # already past draft

    await _seed_current_clan_member(db, "10", "#P1", "#AKATSUKI")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(840, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    resp = await client.post(
        "/api/cwl/clan-config",
        json={"guild_id": 840, "discord_user_id": 42, "clans": [
            {"clan_tag": "#CLAN1", "participating": True},
            {"clan_tag": "#AKATSUKI", "participating": False},  # added, left UNCHECKED
        ]},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200

    signup = db.get_cwl_signup_sync(event_id, "#P1")
    assert signup is not None  # the fix — pooled despite staying unchecked
    assert signup["status"] == "pending"
    assignments = {a["player_tag"]: a["assigned_clan_tag"] for a in db.get_cwl_assignments_sync(event_id)}
    assert "#P1" not in assignments  # never assigned into a non-participating clan's column


@pytest.mark.discord
@pytest.mark.asyncio
async def test_adding_unchecked_guest_clan_never_auto_assigns_even_a_qualifying_player(db, bridge_config, client, monkeypatch):
    """Even a player whose last real CWL attack was in this exact clan (the normal auto-assign
    qualifying condition) must NOT get assigned into a non-participating clan's column — only
    visibility-seeded as pending/unassigned, since participating_clan_tags alone still defines
    valid assignment targets (unchanged by rule b's pool-membership broadening)."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "841", {"#CLAN1": "Alpha", "#AKATSUKI": "Akatsuki"})
    CACHE.db_manager = db
    CACHE.server_config["841"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    event_id = db.create_cwl_event_sync("841", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    db.update_cwl_event_status_sync(event_id, "signup_open")

    await _seed_current_clan_member(db, "10", "#P1", "#AKATSUKI")
    await db.conn.execute(
        "INSERT INTO war_summary (war_id, clan_tag, opponent_tag, is_cwl, cwl_season, date) "
        "VALUES ('war1', '#AKATSUKI', '#OPP', 1, '2026-08', '2026-08-01T10:00')"
    )
    await db.conn.execute(
        "INSERT INTO war_attacks (war_id, clan_tag, date, player_name, player_tag, th_level, map_position, stars, attack_order) "
        "VALUES ('war1', '#AKATSUKI', '2026-08-01T10:00', 'P1', '#P1', 15, 1, 2, 1)"
    )
    await db.conn.commit()

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(841, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    resp = await client.post(
        "/api/cwl/clan-config",
        json={"guild_id": 841, "discord_user_id": 42, "clans": [
            {"clan_tag": "#CLAN1", "participating": True},
            {"clan_tag": "#AKATSUKI", "participating": False},
        ]},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200

    signup = db.get_cwl_signup_sync(event_id, "#P1")
    assert signup is not None
    assert signup["status"] == "pending"
    assert db.get_cwl_assignments_sync(event_id) == []  # never assigned despite qualifying history


# ---------------------------------------------------------------------------
# Rule f (2026-08-18, CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md): unchecking a guest clan is
# now purely cosmetic — the destructive local-pool purge that used to run automatically on every
# uncheck+Save moved to the explicit new "Remove" endpoint, POST /api/cwl/enrollment/guest-clan/
# remove.
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_unchecking_a_guest_clan_via_bridge_no_longer_purges_the_player_pool(db, bridge_config, client, monkeypatch):
    """The exact behavior rule f reverses: a plain uncheck+Save of a guest (non-family) clan used
    to delete its members' cwl_signups/cwl_assignments rows automatically. It must not anymore —
    the clan just drops off the active roster, its players stay in the pool untouched."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "820", {"#CLAN1": "Alpha", "#GUESTCLAN": "Guest"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {
        "#CLAN1": {"name": "Alpha", "war_league": "Master League II"},
        "#GUESTCLAN": {"name": "Guest", "war_league": "Master League II"},
    }
    CACHE.server_config["820"] = {"member_clans": ["#CLAN1"], "member_families": []}  # #GUESTCLAN not in family
    CACHE.subscriptions = {}
    CACHE.clan_families = {}
    await db.conn.execute(
        "INSERT OR IGNORE INTO users (discord_id, display_name) VALUES ('10', '10')"
    )
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag) "
        "VALUES ('10', '#GUESTMEMBER', 'GuestMember', 1, '#GUESTCLAN')"
    )
    await db.conn.commit()

    event_id = db.create_cwl_event_sync("820", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [
        {"clan_tag": "#CLAN1", "participating": True},
        {"clan_tag": "#GUESTCLAN", "participating": True},
    ])
    db.upsert_cwl_signup_sync(event_id, "#GUESTMEMBER", "GuestMember", "10", None, "template_confirm", "pending")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(820, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    resp = await client.post(
        "/api/cwl/clan-config",
        json={"guild_id": 820, "discord_user_id": 42, "clans": [
            {"clan_tag": "#CLAN1", "participating": True},
            {"clan_tag": "#GUESTCLAN", "participating": False},
        ]},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200

    signup = db.get_cwl_signup_sync(event_id, "#GUESTMEMBER")
    assert signup is not None  # NOT purged — the fix
    clan_tags = {c["clan_tag"] for c in db.get_cwl_event_clans_sync(event_id)}
    assert "#GUESTCLAN" in clan_tags  # row survives too, just participating=False


@pytest.mark.discord
@pytest.mark.asyncio
async def test_unchecking_a_shared_guest_clan_via_bridge_no_longer_purges_the_player_pool(db, bridge_config, client, monkeypatch):
    """The cross-guild-shared counterpart to the sibling test above (2026-08-19 live bug report:
    "Members vanished from player pool after uncheck for StayCalm" — a genuinely cross-guild-
    shared clan, not the plain never-shared case the sibling test covers). Unchecking a shared
    guest clan must be just as purely cosmetic as unchecking a plain one — the destructive purge
    lived in a SEPARATE code path (detach_guild_from_shared_clan_on_deactivation's shared branch)
    that rule f's original fix never touched, and this is the regression it left behind."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "832", {"#CLAN1": "Alpha", "#STAYCALM": "StayCalm"})
    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES ('833')")
    await db.conn.commit()
    CACHE.db_manager = db
    CACHE.clan_name_cache = {
        "#CLAN1": {"name": "Alpha", "war_league": "Master League II"},
        "#STAYCALM": {"name": "StayCalm", "war_league": "Champion League II"},
    }
    CACHE.server_config["832"] = {"member_clans": ["#CLAN1"], "member_families": []}  # #STAYCALM not in family
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES ('10', '10')")
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag) "
        "VALUES ('10', '#STAYMEMBER', 'StayMember', 1, '#STAYCALM')"
    )
    await db.conn.commit()

    event_id = db.create_cwl_event_sync("832", "2026-09", "discordid1")
    other_event_id = db.create_cwl_event_sync("833", "2026-09", "otherdiscordid")
    db.set_cwl_event_clans_sync(event_id, [
        {"clan_tag": "#CLAN1", "participating": True},
        {"clan_tag": "#STAYCALM", "participating": True},
    ])
    shared_clan_id = db.create_cwl_shared_clan_sync("#STAYCALM", "2026-09", "833", other_event_id, "unresolved_first_claimer")
    db.add_guild_to_shared_clan_sync(shared_clan_id, "832", event_id)
    db.add_guild_to_shared_clan_sync(shared_clan_id, "833", other_event_id)
    # #STAYMEMBER is on the shared roster itself (not admin_override — a passive auto-seed/
    # template-confirm entry, same as any ordinary real signed-up member) — the deletion loop
    # this bug came from only ever iterates get_cwl_shared_clan_players_sync(), so this row is
    # what makes the reproduction real, not just the local mirror below on its own.
    db.set_cwl_shared_clan_player_status_sync(
        shared_clan_id, "#STAYMEMBER", "StayMember", "10", "pending", "template_confirm", "832",
    )

    # #STAYMEMBER already has a real, local signup — as if written while #STAYCALM was still an
    # active, participating column (template DM confirm, or the shared-roster local mirror sync)
    # — exactly what "real signed-up members" means in the bug report.
    db.upsert_cwl_signup_sync(event_id, "#STAYMEMBER", "StayMember", "10", None, "template_confirm", "pending")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(832, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    resp = await client.post(
        "/api/cwl/clan-config",
        json={"guild_id": 832, "discord_user_id": 42, "clans": [
            {"clan_tag": "#CLAN1", "participating": True},
            {"clan_tag": "#STAYCALM", "participating": False},
        ]},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200

    signup = db.get_cwl_signup_sync(event_id, "#STAYMEMBER")
    assert signup is not None  # NOT purged — the fix
    clan_tags = {c["clan_tag"] for c in db.get_cwl_event_clans_sync(event_id)}
    assert "#STAYCALM" in clan_tags  # row survives too, just participating=False


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_clan_remove_endpoint_purges_pool_and_deletes_the_clan_row(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "821", {"#CLAN1": "Alpha", "#GUESTCLAN": "Guest"})
    CACHE.db_manager = db
    CACHE.server_config["821"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}
    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES ('10', '10')")
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag) "
        "VALUES ('10', '#GUESTMEMBER', 'GuestMember', 1, '#GUESTCLAN')"
    )
    await db.conn.commit()

    event_id = db.create_cwl_event_sync("821", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [
        {"clan_tag": "#CLAN1", "participating": True},
        {"clan_tag": "#GUESTCLAN", "participating": True},
    ])
    db.upsert_cwl_signup_sync(event_id, "#GUESTMEMBER", "GuestMember", "10", None, "template_confirm", "pending")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(821, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    resp = await client.post(
        "/api/cwl/enrollment/guest-clan/remove",
        json={"guild_id": 821, "discord_user_id": 42, "clan_tag": "#GUESTCLAN"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body == {"ok": True}

    assert db.get_cwl_signup_sync(event_id, "#GUESTMEMBER") is None  # purged
    clan_tags = {c["clan_tag"] for c in db.get_cwl_event_clans_sync(event_id)}
    assert clan_tags == {"#CLAN1"}  # #GUESTCLAN's row is gone entirely


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_clan_remove_endpoint_rejects_a_family_clan(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "822", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["822"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}
    event_id = db.create_cwl_event_sync("822", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(822, 42, is_admin=True))

    resp = await client.post(
        "/api/cwl/enrollment/guest-clan/remove",
        json={"guild_id": 822, "discord_user_id": 42, "clan_tag": "#CLAN1"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 400
    clan_tags = {c["clan_tag"] for c in db.get_cwl_event_clans_sync(event_id)}
    assert clan_tags == {"#CLAN1"}  # untouched


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_clan_remove_endpoint_404_when_clan_not_on_roster(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "823", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["823"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}
    db.create_cwl_event_sync("823", "2026-09", "discordid1")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(823, 42, is_admin=True))

    resp = await client.post(
        "/api/cwl/enrollment/guest-clan/remove",
        json={"guild_id": 823, "discord_user_id": 42, "clan_tag": "#NEVERADDED"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 404


# ---------------------------------------------------------------------------
# Rule g (2026-08-18, CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md): list/remove guest players.
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_players_list_includes_invited_and_orphaned_but_not_family(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "824", {"#CLAN1": "Alpha", "#GUESTCLAN": "Guest"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {
        "#CLAN1": {"name": "Alpha", "war_league": "Master League II"},
        "#GUESTCLAN": {"name": "Guest", "war_league": "Master League II"},
    }
    CACHE.server_config["824"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    # #FAMILYMEMBER: real family-clan member — must never appear in the guest list.
    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES ('10', '10')")
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag) "
        "VALUES ('10', '#FAMILYMEMBER', 'FamilyMember', 1, '#CLAN1')"
    )
    # #ORPHAN: still a live current member of #GUESTCLAN, whose cwl_event_clans row is about to
    # be removed entirely (rule f) — its signup row survives, so it's an "orphaned" guest.
    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES ('20', '20')")
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag) "
        "VALUES ('20', '#ORPHAN', 'Orphan', 1, '#GUESTCLAN')"
    )
    await db.conn.commit()

    event_id = db.create_cwl_event_sync("824", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    db.upsert_cwl_signup_sync(event_id, "#ORPHAN", "Orphan", "20", None, "template_confirm", "pending")
    # #INVITEE: an individually-invited guest player, not tied to any clan roster at all.
    db.upsert_cwl_signup_sync(event_id, "#INVITEE", "Invitee", "30", None, "guest_invite", "pending")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(824, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/enrollment/guest-players",
        params={"guild_id": "824", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    tags = {p["player_tag"] for p in body["players"]}
    assert tags == {"#ORPHAN", "#INVITEE"}
    assert "#FAMILYMEMBER" not in tags


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_players_remove_purges_pool_but_not_global_status(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "825", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["825"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    event_id = db.create_cwl_event_sync("825", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    db.upsert_cwl_signup_sync(event_id, "#INVITEE", "Invitee", "30", None, "guest_invite", "pending")
    db.upsert_cwl_assignment_sync(event_id, "#INVITEE", "#CLAN1", assignment_source="admin_override", locked=True)
    # Global rule-h record (Phase 2 schema, not yet wired to any caller) — removal must not
    # touch this, so a later re-add is recognized as already-contacted.
    db.mark_cwl_player_dm_sent_sync("#INVITEE", "2026-09", "Invitee", "30", event_id, 825, "2026-08-18T09:00Z")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(825, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    resp = await client.post(
        "/api/cwl/enrollment/guest-players/remove",
        json={"guild_id": 825, "discord_user_id": 42, "player_tags": ["#INVITEE"]},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body == {"ok": True, "removed": ["#INVITEE"], "rejected": []}

    assert db.get_cwl_signup_sync(event_id, "#INVITEE") is None
    assert db.get_cwl_assignments_sync(event_id) == []
    # Global dm_sent record survives — rule h's "must survive remove-then-re-add."
    status = db.get_cwl_player_season_status_sync("#INVITEE", "2026-09")
    assert status is not None
    assert status["dm_sent"] == 1


# ---------------------------------------------------------------------------
# Guest-player provenance (2026-08-19, CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md): clan-derived
# guest players can only be removed by removing the whole guest clan (rule f); individually
# invited ones can be removed on their own. Two race conditions from the project owner's spec:
# 1) adding a player individually who is already a member of a guest clan already on the roster
#    must be rejected; 2) a later guest-clan invite must reclassify a previously individually
#    invited member of that clan as clan-derived ("clan invitation beats individual invitation").
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_players_list_excludes_guest_clan_derived_players(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "826", {"#CLAN1": "Alpha", "#GUESTCLAN": "Guest"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {
        "#CLAN1": {"name": "Alpha", "war_league": "Master League II"},
        "#GUESTCLAN": {"name": "Guest", "war_league": "Master League II"},
    }
    CACHE.server_config["826"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    # #CLANMEMBER: a live current member of #GUESTCLAN, which IS on this event's roster — clan-
    # derived, must be excluded from the removable list.
    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES ('20', '20')")
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag) "
        "VALUES ('20', '#CLANMEMBER', 'ClanMember', 1, '#GUESTCLAN')"
    )
    await db.conn.commit()

    event_id = db.create_cwl_event_sync("826", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(
        event_id, [{"clan_tag": "#CLAN1", "participating": True}, {"clan_tag": "#GUESTCLAN", "participating": True}],
    )
    db.upsert_cwl_signup_sync(event_id, "#CLANMEMBER", "ClanMember", "20", None, "template_confirm", "pending")
    db.upsert_cwl_signup_sync(event_id, "#INVITEE", "Invitee", "30", None, "guest_invite", "pending")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(826, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/enrollment/guest-players",
        params={"guild_id": "826", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    tags = {p["player_tag"] for p in body["players"]}
    assert tags == {"#INVITEE"}
    assert "#CLANMEMBER" not in tags


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_players_list_excludes_a_genuine_deliberate_cross_assignment_to_other_guild(
    db, bridge_config, client, monkeypatch,
):
    """The legitimate "Assigned to other Guild" case must still be excluded from "Remove Guest
    Players" after Remove: an individually-invited guest player (project owner's spec, verbatim:
    "a player that is rightfully member of the current player pool... but is assigned to another
    guild's roster" — an individual guest invite is exactly how a player who isn't part of any
    family clan becomes "rightfully" pooled here), deliberately drag-assigned INTO the shared clan
    #CLAN1. Untracked current clan (never a real member of #CLAN1 or anywhere else) matches
    test_remove_guest_clan_still_preserves_a_deliberate_admin_override_placement's own premise
    (tests/unit/test_cwl_clan_ownership.py) — the backend-level test this end-to-end check builds
    on. A player whose current clan IS a family clan would have is_guest=False and never even
    reach the candidate list this filter operates on, so wouldn't actually exercise it."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import remove_cwl_guest_clan

    shared_clan_id, owner_event_id, follower_event_id = await _seed_shared_clan_pair(db, "829", "828")
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}}
    CACHE.server_config["828"] = {"member_clans": [], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    # #INVITEE: an individually-invited guest player (source='guest_invite', no live current
    # clan on record), then deliberately drag-assigned INTO the shared clan #CLAN1.
    db.upsert_cwl_signup_sync(follower_event_id, "#INVITEE", "Invitee", "10", None, "guest_invite", "pending")
    db.upsert_cwl_assignment_sync(follower_event_id, "#INVITEE", "#CLAN1", assignment_source="admin_override", locked=True)

    await remove_cwl_guest_clan(828, follower_event_id, "2026-09", "#CLAN1")

    # Survives, correctly, as "Assigned to other Guild" — not individually removable.
    assert db.get_cwl_signup_sync(follower_event_id, "#INVITEE") is not None

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(828, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/enrollment/guest-players",
        params={"guild_id": "828", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    tags = {p["player_tag"] for p in body["players"]}
    assert "#INVITEE" not in tags


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_players_list_excludes_a_purged_own_member_self_assignment(db, bridge_config, client, monkeypatch):
    """2026-08-19 live bug report, project owner — spotted while re-checking the s3-7c manual QA
    step against the live board: a real member of a cross-guild-shared clan, deliberately
    drag-assigned into that SAME clan's own column, then that clan Removed from guild 828's
    roster — "STY - Basement" stayed behind in the pool, visible in "Remove Guest Players", rather
    than being purged like every other real member of the removed clan (project owner's spec,
    verbatim: "assigning one of its own members to itself and then removing that clan again should
    not leave that manually assigned player in the guild's pool"). Fixed upstream in
    _cleanup_local_pool_for_plain_clan_deactivation_sync (QBdiscocmdshelper_cwl.py) — see
    test_shared_guest_clan_remove_purges_own_member_deliberately_placed_into_own_column,
    tests/unit/test_cwl_clan_ownership.py, for the direct backend-level test. This is the
    end-to-end confirmation that the fixed player is gone from the "Remove Guest Players" list too
    (nothing to filter — their local rows no longer exist at all after Remove)."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import remove_cwl_guest_clan

    shared_clan_id, owner_event_id, follower_event_id = await _seed_shared_clan_pair(db, "829", "828")
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}}
    CACHE.server_config["828"] = {"member_clans": [], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    # #BASEMENT: a genuine current member of #CLAN1 itself, deliberately drag-assigned into its
    # OWN column — not a cross-assignment to anywhere else.
    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES ('20', '20')")
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag) "
        "VALUES ('20', '#BASEMENT', 'Basement', 1, '#CLAN1')"
    )
    await db.conn.commit()
    db.upsert_cwl_signup_sync(follower_event_id, "#BASEMENT", "Basement", "20", None, "admin_added", "pending")
    db.upsert_cwl_assignment_sync(follower_event_id, "#BASEMENT", "#CLAN1", assignment_source="admin_override", locked=True)

    await remove_cwl_guest_clan(828, follower_event_id, "2026-09", "#CLAN1")

    assert db.get_cwl_signup_sync(follower_event_id, "#BASEMENT") is None  # fully purged, not preserved

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(828, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/enrollment/guest-players",
        params={"guild_id": "828", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    tags = {p["player_tag"] for p in body["players"]}
    assert "#BASEMENT" not in tags


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_players_remove_rejects_guest_clan_derived_player(db, bridge_config, client, monkeypatch):
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "827", {"#CLAN1": "Alpha", "#GUESTCLAN": "Guest"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {
        "#CLAN1": {"name": "Alpha", "war_league": "Master League II"},
        "#GUESTCLAN": {"name": "Guest", "war_league": "Master League II"},
    }
    CACHE.server_config["827"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES ('20', '20')")
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag) "
        "VALUES ('20', '#CLANMEMBER', 'ClanMember', 1, '#GUESTCLAN')"
    )
    await db.conn.commit()

    event_id = db.create_cwl_event_sync("827", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(
        event_id, [{"clan_tag": "#CLAN1", "participating": True}, {"clan_tag": "#GUESTCLAN", "participating": True}],
    )
    db.upsert_cwl_signup_sync(event_id, "#CLANMEMBER", "ClanMember", "20", None, "template_confirm", "pending")
    db.upsert_cwl_signup_sync(event_id, "#INVITEE", "Invitee", "30", None, "guest_invite", "pending")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(827, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    resp = await client.post(
        "/api/cwl/enrollment/guest-players/remove",
        json={"guild_id": 827, "discord_user_id": 42, "player_tags": ["#CLANMEMBER", "#INVITEE"]},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["removed"] == ["#INVITEE"]
    assert len(body["rejected"]) == 1
    assert body["rejected"][0]["player_tag"] == "#CLANMEMBER"
    assert body["rejected"][0]["clan_tag"] == "#GUESTCLAN"
    assert body["rejected"][0]["clan_name"] == "Guest"

    # The individually invited one is gone; the clan-derived one survives untouched.
    assert db.get_cwl_signup_sync(event_id, "#INVITEE") is None
    assert db.get_cwl_signup_sync(event_id, "#CLANMEMBER") is not None


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_players_remove_all_rejected_skips_hub_refresh(db, bridge_config, client, monkeypatch):
    """When every requested tag is rejected, nothing changed — the Hub message refresh and
    enrollment-version bump (which drive the live board's auto-update) must not fire for a no-op
    request; only a genuine removal should trigger either."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "828", {"#CLAN1": "Alpha", "#GUESTCLAN": "Guest"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {
        "#CLAN1": {"name": "Alpha", "war_league": "Master League II"},
        "#GUESTCLAN": {"name": "Guest", "war_league": "Master League II"},
    }
    CACHE.server_config["828"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES ('20', '20')")
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag) "
        "VALUES ('20', '#CLANMEMBER', 'ClanMember', 1, '#GUESTCLAN')"
    )
    await db.conn.commit()

    event_id = db.create_cwl_event_sync("828", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(
        event_id, [{"clan_tag": "#CLAN1", "participating": True}, {"clan_tag": "#GUESTCLAN", "participating": True}],
    )
    db.upsert_cwl_signup_sync(event_id, "#CLANMEMBER", "ClanMember", "20", None, "template_confirm", "pending")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(828, 42, is_admin=True))
    refresh_mock = AsyncMock()
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", refresh_mock)

    resp = await client.post(
        "/api/cwl/enrollment/guest-players/remove",
        json={"guild_id": 828, "discord_user_id": 42, "player_tags": ["#CLANMEMBER"]},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["removed"] == []
    assert len(body["rejected"]) == 1
    refresh_mock.assert_not_awaited()
    assert db.get_cwl_signup_sync(event_id, "#CLANMEMBER") is not None


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_guest_add_rejects_when_player_already_in_guest_clan(db, bridge_config, client, monkeypatch):
    """Race condition 1 (project owner's spec, verbatim): "When a guest clan was added all
    players of that clan are already in the player pool. Trying to add an individual player that
    is member of the guest clan should lead to an appropriate error message to the user.\""""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "829", {"#CLAN1": "Alpha", "#GUESTCLAN": "Guest"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {
        "#CLAN1": {"name": "Alpha", "war_league": "Master League II"},
        "#GUESTCLAN": {"name": "Guest", "war_league": "Master League II"},
    }
    CACHE.server_config["829"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES ('20', '20')")
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag) "
        "VALUES ('20', '#CLANMEMBER', 'ClanMember', 1, '#GUESTCLAN')"
    )
    await db.conn.commit()

    event_id = db.create_cwl_event_sync("829", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(
        event_id, [{"clan_tag": "#CLAN1", "participating": True}, {"clan_tag": "#GUESTCLAN", "participating": True}],
    )

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(829, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    resp = await client.post(
        "/api/cwl/enrollment/guest",
        json={"guild_id": 829, "discord_user_id": 42, "player_tag": "#CLANMEMBER", "player_name": "ClanMember", "discord_id": "20"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 409
    body = await resp.json()
    assert "Guest" in body["error"]
    # Never added a second, individually-sourced signup row.
    assert db.get_cwl_signup_sync(event_id, "#CLANMEMBER") is None


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_guest_add_rejects_when_player_already_placed_in_another_guild(
    db, bridge_config, client, monkeypatch
):
    """2026-08-20 live bug report: a player already deliberately placed in a DIFFERENT guild's
    own (private, non-shared) CWL clan this season must not be individually guest-invitable here
    — unlike a guest CLAN, an individual guest-player invite has no cross-guild conflict handling
    at all, so this used to silently succeed and leave the player one drag away from a genuine
    double-booking across two guilds' rosters for the same season."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "830", {"#HOME": "Home"})
    await _seed_guild_and_clans(db, "831", {"#OTHERCLAN": "Other"})
    CACHE.db_manager = db
    CACHE.server_config["830"] = {"member_clans": ["#HOME"], "member_families": []}
    CACHE.server_config["831"] = {"member_clans": ["#OTHERCLAN"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    other_event_id = db.create_cwl_event_sync("831", "2026-09", "discordid1")
    db.upsert_cwl_assignment_sync(other_event_id, "#ELSEWHERE", "#OTHERCLAN", assignment_source="admin_override", locked=True)

    event_id = db.create_cwl_event_sync("830", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#HOME", "participating": True}])

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(830, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    resp = await client.post(
        "/api/cwl/enrollment/guest",
        json={"guild_id": 830, "discord_user_id": 42, "player_tag": "#ELSEWHERE", "player_name": "Elsewhere"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 409
    body = await resp.json()
    assert "already placed" in body["error"]
    assert db.get_cwl_signup_sync(event_id, "#ELSEWHERE") is None


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_player_reclassified_when_clan_later_invited_as_guest(db, bridge_config, client, monkeypatch):
    """Race condition 2 (project owner's spec, verbatim): "When a guest player is invited
    individually and then his clan is invited as a guest clan then the status of that player
    should be switched from individual invitation to guest clan invitation. i.e. clan invitation
    beats individual invitation." The classification is derived from live current-clan membership
    on every call (get_cwl_guest_clan_tags_sync), so this "switch" needs no write-time flag flip —
    it falls out automatically once #GUESTCLAN is added to the roster."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "830", {"#CLAN1": "Alpha", "#GUESTCLAN": "Guest"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {
        "#CLAN1": {"name": "Alpha", "war_league": "Master League II"},
        "#GUESTCLAN": {"name": "Guest", "war_league": "Master League II"},
    }
    CACHE.server_config["830"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES ('20', '20')")
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag) "
        "VALUES ('20', '#CLANMEMBER', 'ClanMember', 1, '#GUESTCLAN')"
    )
    await db.conn.commit()

    event_id = db.create_cwl_event_sync("830", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    # Individually invited BEFORE #GUESTCLAN is ever on the roster — at this point it's a plain,
    # individually-removable guest.
    db.upsert_cwl_signup_sync(event_id, "#CLANMEMBER", "ClanMember", "20", None, "guest_invite", "pending")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(830, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    resp = await client.get(
        "/api/cwl/enrollment/guest-players",
        params={"guild_id": "830", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    body = await resp.json()
    assert {p["player_tag"] for p in body["players"]} == {"#CLANMEMBER"}

    # #GUESTCLAN now joins the roster — #CLANMEMBER's provenance flips to clan-derived with no
    # write to their own cwl_signups row at all.
    db.set_cwl_event_clans_sync(
        event_id, [{"clan_tag": "#CLAN1", "participating": True}, {"clan_tag": "#GUESTCLAN", "participating": True}],
    )

    resp = await client.get(
        "/api/cwl/enrollment/guest-players",
        params={"guild_id": "830", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    body = await resp.json()
    assert body["players"] == []

    resp = await client.post(
        "/api/cwl/enrollment/guest-players/remove",
        json={"guild_id": 830, "discord_user_id": 42, "player_tags": ["#CLANMEMBER"]},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    body = await resp.json()
    assert body["removed"] == []
    assert body["rejected"][0]["player_tag"] == "#CLANMEMBER"
    assert db.get_cwl_signup_sync(event_id, "#CLANMEMBER") is not None


# ---------------------------------------------------------------------------
# Rule h (2026-08-18, CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md): notify_new_cwl_pool_members.
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_notify_new_cwl_pool_members_only_dms_not_yet_contacted(db, monkeypatch):
    from qapbot import config as config_module
    from qapbot.cache_manager import CACHE
    from qapbot.web_bridge import notify_new_cwl_pool_members

    monkeypatch.setattr(
        config_module, "CONFIG",
        dataclasses.replace(config_module.CONFIG, is_dev_mode=False, cwl_dm_restrict_to_admin=False),
    )

    await _seed_guild_and_clans(db, "830", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha", "war_league": "Master League II"}}
    CACHE.server_config["830"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    event_id = db.create_cwl_event_sync("830", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    db.update_cwl_event_status_sync(event_id, "signup_open")

    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES ('10', '10')")
    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES ('20', '20')")
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag) "
        "VALUES ('10', '#P1', 'PlayerOne', 1, '#CLAN1')"
    )
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag) "
        "VALUES ('20', '#P2', 'PlayerTwo', 1, '#CLAN1')"
    )
    await db.conn.commit()
    db.mark_cwl_player_dm_sent_sync("#P1", "2026-09", "PlayerOne", "10", event_id, 830, "2026-08-17T09:00Z")
    # #P2 (discord_id '20') never contacted by anyone — the one this action should reach.

    contacted = []

    async def fake_send_user_dm_detailed(user_id, message, view=None, embed=None, sent_message_out=None):
        contacted.append(user_id)
        return True, "sent"

    monkeypatch.setattr(CACHE, "send_user_dm_detailed", fake_send_user_dm_detailed)

    result = await notify_new_cwl_pool_members(830, "2026-09")

    assert result["ok"] is True
    assert result["contacted"] == 1
    assert contacted == ["20"]
    assert db.get_cwl_player_season_status_sync("#P2", "2026-09")["dm_sent"] == 1


@pytest.mark.discord
@pytest.mark.asyncio
async def test_notify_new_cwl_pool_members_seeds_declined_for_an_optout_no_dm_new_member(db, monkeypatch):
    """plans/cwl-personal-hub.md Phase 4b-bis: a permanently-opted-out member who joins the pool
    AFTER enrollment already started — this button's whole purpose — is skipped from the DM by
    _send_cwl_enrollment_dm_batch's own seed-before-DM step (that step only ever seeds who it's
    ABOUT to DM), so without the second seed pass they would never get a cwl_signups row at all,
    and would be invisible on the board instead of showing as Declined."""
    from qapbot import config as config_module
    from qapbot.cache_manager import CACHE
    from qapbot.web_bridge import notify_new_cwl_pool_members

    monkeypatch.setattr(
        config_module, "CONFIG",
        dataclasses.replace(config_module.CONFIG, is_dev_mode=False, cwl_dm_restrict_to_admin=False),
    )

    await _seed_guild_and_clans(db, "832", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha", "war_league": "Master League II"}}
    CACHE.server_config["832"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    event_id = db.create_cwl_event_sync("832", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    db.update_cwl_event_status_sync(event_id, "signup_open")

    # Joined the clan (and thus the pool) after Start Enrollment already ran — no cwl_signups
    # row exists for them yet, and they're permanently opted out with no DM-anyway override.
    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES ('30', '30')")
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag, cwl_permanent_optout) "
        "VALUES ('30', '#P3', 'PlayerThree', 1, '#CLAN1', 1)"
    )
    await db.conn.commit()

    monkeypatch.setattr(CACHE, "send_user_dm_detailed", AsyncMock(return_value=(True, "sent")))

    result = await notify_new_cwl_pool_members(832, "2026-09")

    assert result["ok"] is True
    assert result["contacted"] == 0
    signup = db.get_cwl_signup_sync(event_id, "#P3")
    assert signup is not None
    assert signup["status"] == "declined"
    assert signup["source"] == "auto_optout"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_notify_new_cwl_pool_members_rejects_draft_event(db):
    from qapbot.cache_manager import CACHE
    from qapbot.web_bridge import notify_new_cwl_pool_members

    await _seed_guild_and_clans(db, "831", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["831"] = {"member_clans": ["#CLAN1"], "member_families": []}
    db.create_cwl_event_sync("831", "2026-09", "discordid1")  # stays draft

    result = await notify_new_cwl_pool_members(831, "2026-09")

    assert result == {"ok": False, "error": "not_open"}


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
async def test_enrollment_get_unchecked_plain_guest_clan_members_fall_back_to_unassigned(
    db, bridge_config, client, monkeypatch
):
    """2026-08-19 live bug report, project owner: unchecking Hohenloher Land — a plain guest clan
    tracked by no other guild — while leaving it on the roster, showed its (previously seeded and
    assigned) members in the "Assigned to other Guild" pseudo-column, even though no other guild
    is involved at all: "Hohenloher Land ist not tracked on another guild so after unchecking it,
    Hohenloher Land has no other cwl ongoing. Hence the players should have been moved back to the
    Unassigned pool."

    Root cause: unchecking a plain (never cross-guild-shared) guest clan is deliberately cosmetic
    — cwl_assignments rows pointing at it are left untouched (rule f,
    CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md) — but the payload builder surfaced that dormant
    assigned_clan_tag unconditionally, and the frontend's orphaned-column heuristic
    (enrollmentBoard.ts, `assigned_clan_tag not in knownClanTags`) can't tell "this points at a
    real cross-guild shared clan" apart from "this points at my own clan I just unchecked" — both
    produce a tag absent from `clans` (which only lists PARTICIPATING clans). Fixed by having the
    payload builder itself drop an assignment pointing at a non-participating clan unless that
    clan is a genuine cwl_shared_clans entry."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "794", {"#CLAN1": "Alpha", "#GUESTCLAN": "Guest Clan"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}, "#GUESTCLAN": {"name": "Guest Clan"}}
    CACHE.server_config["794"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    event_id = db.create_cwl_event_sync("794", "2026-09", "discordid1")
    # #GUESTCLAN present on the roster but UNCHECKED (participating=False) — matches "unchecked it
    # but kept it as a guest clan" from the live report, not a full Remove.
    db.set_cwl_event_clans_sync(event_id, [
        {"clan_tag": "#CLAN1", "participating": True},
        {"clan_tag": "#GUESTCLAN", "participating": False},
    ])
    await _seed_current_clan_member(db, "20", "#PGUEST", "#GUESTCLAN")
    # A dormant local assignment row pointing at the now-unchecked clan — exactly what a prior
    # auto-assign seed (while #GUESTCLAN was still checked) would have left behind, and what rule
    # f's "purely cosmetic" uncheck deliberately never cleans up.
    db.upsert_cwl_signup_sync(event_id, "#PGUEST", "GuestPlayer", "20", None, "auto_assigned", "pending")
    db.upsert_cwl_assignment_sync(event_id, "#PGUEST", "#GUESTCLAN", assignment_source="suggested", locked=False)

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(794, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/enrollment",
        params={"guild_id": "794", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert {c["clan_tag"] for c in body["clans"]} == {"#CLAN1"}  # unchecked clan has no column
    players_by_tag = {p["player_tag"]: p for p in body["players"]}
    assert players_by_tag["#PGUEST"]["assigned_clan_tag"] is None  # falls back to Unassigned


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


@pytest.mark.asyncio
async def test_enrollment_get_mirrors_a_private_placement_in_another_guild(db, bridge_config, client, monkeypatch):
    """2026-08-20 live bug report, project owner: player Killer, already deliberately placed in
    "The Marines" guild's own (private, non-shared) CWL roster this season, got pooled into
    "The QCrew" guild too (via cwl_enrollment_include_all_linked_accounts' account-wide
    expansion) and showed up as plain Unassigned there — no hint they already have a real home
    elsewhere, unlike the identical situation for a cross-guild SHARED clan (which already gets
    the "Assigned to other Guild" pseudo-column via the shared-clan merge just above this fix in
    _build_enrollment_payload). The Marines/QCrew clans here are NOT shared with each other at
    all — Killer's real placement lives purely in Marines' own private cwl_assignments, which
    QCrew's board has no local record of until this fix's cross-guild mirror runs."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "832", {"#MARINES2": "The Marines II"})
    await _seed_guild_and_clans(db, "833", {"#QCREW2": "The QCrew"})
    CACHE.db_manager = db
    CACHE.server_config["832"] = {"member_clans": ["#MARINES2"], "member_families": []}
    CACHE.server_config["833"] = {"member_clans": ["#QCREW2"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    marines_event_id = db.create_cwl_event_sync("832", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(marines_event_id, [{"clan_tag": "#MARINES2", "participating": True}])
    db.upsert_cwl_assignment_sync(
        marines_event_id, "#KILLER", "#MARINES2", assignment_source="suggested", locked=False
    )

    qcrew_event_id = db.create_cwl_event_sync("833", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(qcrew_event_id, [{"clan_tag": "#QCREW2", "participating": True}])
    # Pooled in QCrew's own event (e.g. via account-wide expansion) but never locally assigned
    # anywhere here — this is the exact shape that showed as plain Unassigned before the fix.
    db.upsert_cwl_signup_sync(qcrew_event_id, "#KILLER", "Killer", None, None, "template_confirm", "confirmed")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(833, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/enrollment",
        params={"guild_id": "833", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    killer = next(p for p in body["players"] if p["player_tag"] == "#KILLER")
    assert killer["assigned_clan_tag"] == "#MARINES2"


@pytest.mark.asyncio
async def test_enrollment_get_uses_cached_th_level_for_clanless_pooled_player(db, bridge_config, client, monkeypatch):
    """A pooled player (cwl_signups row) who has since left every family/participating clan —
    current_clan_tag NULL in user_players, so get_current_clan_members_sync structurally can't
    return them — must still show their real, cached th_level rather than falling all the way
    through to (possibly much older, or entirely absent) war_attacks history (2026-08-20, live bug
    report: players who left their clan after being pooled showed a blank TH badge despite the
    bot already having their TH cached from before they left)."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "791", {"#CLAN2": "Beta"})
    CACHE.db_manager = db
    CACHE.server_config["791"] = {"member_clans": ["#CLAN2"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    event_id = db.create_cwl_event_sync("791", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN2", "participating": True}])
    # Stale war_attacks history (TH10) predating a cached, more recent user_players.th_level (12).
    await db.conn.execute(
        "INSERT INTO war_summary (war_id, clan_tag, opponent_tag, is_cwl, cwl_season, date) "
        "VALUES ('war2', '#CLAN2', '#OPP', 0, '', '2026-01-01T10:00')"
    )
    await db.conn.execute(
        "INSERT INTO war_attacks (war_id, clan_tag, date, player_name, player_tag, th_level, map_position, stars) "
        "VALUES ('war2', '#CLAN2', '2026-01-01T10:00', 'Departed', '#P2', 10, 1, 0)"
    )
    # current_clan_tag left NULL: this player has left every clan the bot tracks.
    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES ('11', '11')")
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag, th_level) "
        "VALUES ('11', '#P2', 'Departed', 1, NULL, 12)"
    )
    await db.conn.commit()
    db.upsert_cwl_signup_sync(event_id, "#P2", "Departed", "11", None, "template_confirm", "pending")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(791, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/enrollment",
        params={"guild_id": "791", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    players_by_tag = {p["player_tag"]: p for p in body["players"]}
    assert players_by_tag["#P2"]["th_level"] == 12


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_get_resolves_link_live_not_from_signup_snapshot(db, bridge_config, client, monkeypatch):
    """2026-08-22 live bug report: "B.A.B.A is still shown as unlinked in the teams management
    view while it is clearly linked to user odin". cwl_signups is an enrollment-time SNAPSHOT
    written once by Start Enrollment and never refreshed, and _build_enrollment_payload seeds
    players_by_tag from it BEFORE the live get_current_clan_members_sync pass (which then skips
    any tag it already has). So an account linked AFTER enrollment ran kept the snapshot's NULL
    discord_id forever and rendered grey/"Not Linked" (17 players on one live board), while an
    account re-linked to a different Discord user kept the OLD owner forever (8 more).

    Covers all three shapes in one payload:
      #LATE   — signup snapshot has NULL, user_players has a real owner  (the B.A.B.A case)
      #MOVED  — signup snapshot names an owner who no longer owns it     (the re-link case)
      #NEVER  — signup snapshot names an owner, no user_players row at all (guest tag added by
                search, never linked) — must keep the snapshot value rather than being blanked.
    """
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "834", {"#CLANL": "LinkClan"})
    CACHE.db_manager = db
    CACHE.server_config["834"] = {"member_clans": ["#CLANL"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    event_id = db.create_cwl_event_sync("834", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLANL", "participating": True}])

    # The snapshot, as Start Enrollment left it.
    db.upsert_cwl_signup_sync(event_id, "#LATE", "Late", None, None, "template_confirm", "pending")
    db.upsert_cwl_signup_sync(event_id, "#MOVED", "Moved", "oldowner", None, "template_confirm", "pending")
    db.upsert_cwl_signup_sync(event_id, "#NEVER", "Never", "guestowner", None, "guest_invite", "pending")

    # Reality now, per user_players.
    await _seed_current_clan_member(db, "realowner", "#LATE", "#CLANL")
    await _seed_current_clan_member(db, "newowner", "#MOVED", "#CLANL")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(834, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/enrollment",
        params={"guild_id": "834", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    players_by_tag = {p["player_tag"]: p for p in body["players"]}

    assert players_by_tag["#LATE"]["discord_id"] == "realowner"
    assert players_by_tag["#MOVED"]["discord_id"] == "newowner"
    assert players_by_tag["#NEVER"]["discord_id"] == "guestowner"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_get_clears_link_for_account_returned_to_unassigned_pool(db, bridge_config, client, monkeypatch):
    """The other direction of the same staleness (2026-08-22): a signup snapshot naming an owner
    for an account that has since been unlinked (its only user_players row is the UNASSIGNED
    sentinel). user_players is the authority for "who owns this now", and the answer is nobody —
    the board must render it unlinked rather than keeping the old owner. get_player_links_sync
    maps 'UNASSIGNED' to None, so the live override has to apply that None rather than treating
    it as "no information"."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "835", {"#CLANU": "UnlinkClan"})
    CACHE.db_manager = db
    CACHE.server_config["835"] = {"member_clans": ["#CLANU"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    event_id = db.create_cwl_event_sync("835", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLANU", "participating": True}])
    db.upsert_cwl_signup_sync(event_id, "#GONE", "Gone", "exowner", None, "template_confirm", "pending")
    await _seed_current_clan_member(db, "UNASSIGNED", "#GONE", "#CLANU")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(835, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/enrollment",
        params={"guild_id": "835", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    gone = next(p for p in body["players"] if p["player_tag"] == "#GONE")
    assert gone["discord_id"] is None


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_get_includes_discord_display_name_for_the_tooltip(db, bridge_config, client, monkeypatch):
    """2026-08-22 live-testing feedback: "instead of showing 'Linked' the tooltip could as well
    show the name of the linked discord user" — the board already resolves discord_id live (see
    test_enrollment_get_resolves_link_live_not_from_signup_snapshot above), it just never carried
    a human-readable name with it. Sourced from CACHE.user_accounts, the same in-memory gateway
    cache the @-prefixed guest search already reads (_discord_display_name_player_hits) — no
    extra DB query. Three shapes: linked + present in the cache, linked but absent from it (left
    the server — falls back to null, the frontend then shows the older "Linked" text), and not
    linked at all."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "836", {"#CLAND": "NameClan"})
    CACHE.db_manager = db
    CACHE.server_config["836"] = {"member_clans": ["#CLAND"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}
    CACHE.user_accounts = {
        "realowner": {"display_name": "Qaplop", "players": []},
    }

    event_id = db.create_cwl_event_sync("836", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAND", "participating": True}])

    await _seed_current_clan_member(db, "realowner", "#NAMED", "#CLAND")
    await _seed_current_clan_member(db, "staleowner", "#GHOST", "#CLAND")
    await _seed_current_clan_member(db, "UNASSIGNED", "#SOLO", "#CLAND")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(836, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/enrollment",
        params={"guild_id": "836", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    players_by_tag = {p["player_tag"]: p for p in body["players"]}

    assert players_by_tag["#NAMED"]["discord_display_name"] == "Qaplop"
    assert players_by_tag["#GHOST"]["discord_display_name"] is None
    assert players_by_tag["#SOLO"]["discord_display_name"] is None


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_get_resolves_optout_for_a_clanless_pooled_player(db, bridge_config, client, monkeypatch):
    """optout_by_tag was populated ONLY from get_current_clan_members_sync, which is clan-scoped
    — so a pooled player who is in no member/participating clan silently defaulted to
    cwl_permanent_optout=False no matter what user_players actually said, hiding a real opt-out
    from the board. The same get_player_links_sync call that fixes discord_id (2026-08-22) also
    carries cwl_permanent_optout, so it closes this identical blind spot."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "836", {"#CLANO": "OptClan"})
    CACHE.db_manager = db
    CACHE.server_config["836"] = {"member_clans": ["#CLANO"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    event_id = db.create_cwl_event_sync("836", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLANO", "participating": True}])
    db.upsert_cwl_signup_sync(event_id, "#OPTED", "Opted", "owner1", None, "template_confirm", "pending")
    # Linked and opted out, but in NO clan — the clan-scoped source can't see them.
    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES ('owner1', 'Owner')")
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag, "
        "cwl_permanent_optout) VALUES ('owner1', '#OPTED', 'Opted', 0, NULL, 1)"
    )
    await db.conn.commit()

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(836, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/enrollment",
        params={"guild_id": "836", "discord_user_id": "42"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    opted = next(p for p in body["players"] if p["player_tag"] == "#OPTED")
    assert opted["cwl_permanent_optout"] is True


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
async def test_enrollment_assign_rejects_player_already_placed_in_another_guild(db, bridge_config, client, monkeypatch):
    """2026-08-20 live bug report companion to test_enrollment_guest_add_rejects_when_player_
    already_placed_in_another_guild: even if a conflicting signup somehow already exists locally
    (e.g. seeded before this fix shipped), the actual placement write must still refuse rather
    than silently double-booking the player into two guilds' rosters for the same season."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "788", {"#HOME2": "Home2"})
    await _seed_guild_and_clans(db, "789", {"#OTHERCLAN2": "Other2"})
    CACHE.db_manager = db
    CACHE.server_config["788"] = {"member_clans": ["#HOME2"], "member_families": []}
    CACHE.server_config["789"] = {"member_clans": ["#OTHERCLAN2"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    other_event_id = db.create_cwl_event_sync("789", "2026-09", "discordid1")
    db.upsert_cwl_assignment_sync(
        other_event_id, "#ELSEWHERE2", "#OTHERCLAN2", assignment_source="admin_override", locked=True
    )

    event_id = db.create_cwl_event_sync("788", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#HOME2", "participating": True}])
    db.upsert_cwl_signup_sync(event_id, "#ELSEWHERE2", "Elsewhere2", None, None, "guest_invite", "pending")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(788, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    resp = await client.post(
        "/api/cwl/enrollment/assign",
        json={"guild_id": 788, "discord_user_id": 42, "player_tag": "#ELSEWHERE2", "clan_tag": "#HOME2"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 409
    body = await resp.json()
    assert "already placed" in body["error"].lower()
    assert db.get_cwl_assignments_sync(event_id) == []


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
async def test_guest_search_excludes_family_clan_not_yet_configured_for_event(db, bridge_config, client, monkeypatch):
    """2026-08-20 fix, live bug report: a family clan with no cwl_event_clans row yet (never
    explicitly checked/configured for this event) still showed up as an addable "guest" —
    excluding only already-*participating* clans missed this case. A guild's own family clan is
    never a guest candidate regardless of whether it's been configured for the event at all."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "856", {"#CLAN1": "Marines"})
    CACHE.db_manager = db
    CACHE.server_config["856"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Marines"}}
    CACHE.user_accounts = {}

    db.create_cwl_event_sync("856", "2026-09", "discordid1")
    # No set_cwl_event_clans_sync call — #CLAN1 has no cwl_event_clans row at all yet.

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(856, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/guest-search?guild_id=856&discord_user_id=42&q=marine",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["results"] == []


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_search_excludes_current_member_of_family_clan(db, bridge_config, client, monkeypatch):
    """2026-08-20 fix, live bug report: a current member of the guild's own family clan kept
    showing up as an addable "guest" whenever they had no cwl_signups row of their own yet (e.g.
    before Start Enrollment has run) — excluding only already-invited signups missed this case.
    A current member of any clan in this guild's own lineup is never a guest candidate."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "857", {"#CLAN1": "Marines"})
    CACHE.db_manager = db
    CACHE.server_config["857"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.clan_name_cache = {}
    _seed_player_names(db, {"#FAM1": "FamilyPlayer"})
    CACHE.user_accounts = {}
    await _seed_current_clan_member(db, "555", "#FAM1", "#CLAN1")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(857, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/guest-search?guild_id=857&discord_user_id=42&q=family",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["results"] == []


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
    _seed_player_names(db, {"#GUEST1": "GuestPlayer"})
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
async def test_guest_search_excludes_already_invited_guest_player(db, bridge_config, client, monkeypatch):
    """2026-08-20 fix, live bug report: a player already invited as a guest kept reappearing in
    later searches. A player_tag with an existing cwl_signups row for this event must be
    excluded from the result list, same as an already-participating clan already is."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "805", {"#OUTSIDE_CLAN": "Outside"})
    CACHE.db_manager = db
    CACHE.server_config["805"] = {"member_clans": [], "member_families": []}
    CACHE.clan_name_cache = {}
    _seed_player_names(db, {"#GUEST1": "GuestPlayer"})
    CACHE.user_accounts = {}
    await _seed_current_clan_member(db, "555", "#GUEST1", "#OUTSIDE_CLAN")

    event_id = db.create_cwl_event_sync("805", "2026-09", "discordid1")
    db.upsert_cwl_signup_sync(event_id, "#GUEST1", "GuestPlayer", "555", None, "guest_invite", "pending")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(805, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/guest-search?guild_id=805&discord_user_id=42&q=guest",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["results"] == []


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


# ---------------------------------------------------------------------------
# CoC API fallback for an unknown-but-well-formed tag (2026-08-20) — see
# qapbot/web_bridge.py's _resolve_guest_tag_via_coc_api().
# ---------------------------------------------------------------------------

async def _setup_api_fallback_guild(db, guild_id: str, monkeypatch) -> None:
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, guild_id, {})
    CACHE.db_manager = db
    CACHE.server_config[guild_id] = {"member_clans": [], "member_families": []}
    CACHE.clan_name_cache = {}
    CACHE.user_accounts = {}

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(int(guild_id), 42, is_admin=True))


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_search_unknown_tag_resolved_as_clan_via_coc_api(db, bridge_config, client, monkeypatch):
    """A well-formed tag the DB has never seen triggers a live clan lookup; the hit comes back
    with the real name/tier the API-populated clan_name_cache now holds, replacing the raw
    unverified placeholder entirely."""
    from types import SimpleNamespace

    from qapbot.cache_manager import CACHE

    await _setup_api_fallback_guild(db, "850", monkeypatch)

    async def _fake_get_clan(tag):
        # Mirrors coc_clan_cache._update_clan_metadata()'s real side effect: a never-before-seen
        # clan lands in clan_name_cache (and the DB) as part of the fetch itself.
        CACHE.clan_name_cache[tag] = {"name": "Api Clan", "war_league": "Master League III"}
        return SimpleNamespace(tag=tag, name="Api Clan")

    monkeypatch.setattr(CACHE.coc_clan_cache, "get_clan", AsyncMock(side_effect=_fake_get_clan))
    monkeypatch.setattr(CACHE, "get_player", AsyncMock(return_value=None))

    resp = await client.get(
        "/api/cwl/guest-search?guild_id=850&discord_user_id=42&q=%23APICLAN1",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["results"] == [{
        "type": "clan", "clan_tag": "#APICLAN1", "clan_name": "Api Clan",
        "clan_tier": "Master League III", "already_shared_with": None,
    }]
    CACHE.get_player.assert_not_awaited()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_search_unknown_tag_resolved_as_player_via_coc_api(db, bridge_config, client, monkeypatch):
    """No clan by that tag, but a player — the hit carries the API's real name and is written
    into player_name_index (and its mirrors), so the next keystroke finds it without an API
    call."""
    import coc  # type: ignore[import-untyped]
    from types import SimpleNamespace

    from qapbot.cache_manager import CACHE

    await _setup_api_fallback_guild(db, "851", monkeypatch)
    monkeypatch.setattr(
        CACHE.coc_clan_cache, "get_clan", AsyncMock(side_effect=coc.NotFound(MagicMock(), "gone"))
    )
    monkeypatch.setattr(
        CACHE, "get_player", AsyncMock(return_value=SimpleNamespace(tag="#APIPLAY1", name="ApiPlayer"))
    )

    resp = await client.get(
        "/api/cwl/guest-search?guild_id=851&discord_user_id=42&q=%23APIPLAY1",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["results"] == [
        {"type": "player", "player_tag": "#APIPLAY1", "player_name": "ApiPlayer", "discord_id": None}
    ]
    assert db.search_player_tags_by_prefix_sync("#APIPLAY1") == [
        {"player_tag": "#APIPLAY1", "player_name": "ApiPlayer"}
    ]


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_search_coc_api_fallback_excludes_already_invited_player(db, bridge_config, client, monkeypatch):
    """2026-08-20 fix, live bug report: a guest invited by typing an exact tag (no
    player_name_index entry) reappeared on a later search of that same tag, since the DB search
    found nothing "real" and fell through to the CoC API fallback, which had no equivalent
    exclusion — mirrors _build_api_clan_hit_sync's already-participating clan check."""
    import coc  # type: ignore[import-untyped]
    from types import SimpleNamespace

    from qapbot.cache_manager import CACHE

    await _setup_api_fallback_guild(db, "854", monkeypatch)
    event_id = db.create_cwl_event_sync("854", "2026-09", "discordid1")
    db.upsert_cwl_signup_sync(event_id, "#APIPLAY2", "ApiPlayer", "555", None, "guest_invite", "pending")
    monkeypatch.setattr(
        CACHE.coc_clan_cache, "get_clan", AsyncMock(side_effect=coc.NotFound(MagicMock(), "gone"))
    )
    get_player_mock = AsyncMock(return_value=SimpleNamespace(tag="#APIPLAY2", name="ApiPlayer"))
    monkeypatch.setattr(CACHE, "get_player", get_player_mock)

    resp = await client.get(
        "/api/cwl/guest-search?guild_id=854&discord_user_id=42&q=%23APIPLAY2",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["results"] == []


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_search_coc_api_fallback_excludes_current_family_clan_member(
    db, bridge_config, client, monkeypatch
):
    """Same exclusion, reached via the family-membership half rather than the signup half — a
    current member of the guild's own family clan with no player_name_index entry must not be
    resurfaced by the CoC API fallback either."""
    import coc  # type: ignore[import-untyped]
    from types import SimpleNamespace

    from qapbot.cache_manager import CACHE

    await _setup_api_fallback_guild(db, "855", monkeypatch)
    await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES ('#CLAN1', 'Marines')")
    await db.conn.commit()
    CACHE.server_config["855"] = {"member_clans": ["#CLAN1"], "member_families": []}
    await _seed_current_clan_member(db, "555", "#APIPLAY3", "#CLAN1")
    monkeypatch.setattr(
        CACHE.coc_clan_cache, "get_clan", AsyncMock(side_effect=coc.NotFound(MagicMock(), "gone"))
    )
    get_player_mock = AsyncMock(return_value=SimpleNamespace(tag="#APIPLAY3", name="ApiPlayer"))
    monkeypatch.setattr(CACHE, "get_player", get_player_mock)

    resp = await client.get(
        "/api/cwl/guest-search?guild_id=855&discord_user_id=42&q=%23APIPLAY3",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["results"] == []


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_search_unknown_tag_absent_from_coc_api_keeps_raw_placeholder(
    db, bridge_config, client, monkeypatch
):
    """Neither a clan nor a player — the pre-fallback behavior is preserved unchanged (raw hit,
    name = the tag), and the internal `unverified` marker never reaches the frontend."""
    import coc  # type: ignore[import-untyped]

    from qapbot.cache_manager import CACHE

    await _setup_api_fallback_guild(db, "852", monkeypatch)
    monkeypatch.setattr(
        CACHE.coc_clan_cache, "get_clan", AsyncMock(side_effect=coc.NotFound(MagicMock(), "gone"))
    )
    monkeypatch.setattr(CACHE, "get_player", AsyncMock(return_value=None))

    resp = await client.get(
        "/api/cwl/guest-search?guild_id=852&discord_user_id=42&q=%23APIGH0ST",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["results"] == [
        {"type": "player", "player_tag": "#APIGH0ST", "player_name": "#APIGH0ST", "discord_id": None}
    ]


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_search_db_hit_never_triggers_coc_api(db, bridge_config, client, monkeypatch):
    """The fallback is a last resort: any real DB hit must short-circuit it, so a normal search
    never costs a CoC API call."""
    from qapbot.cache_manager import CACHE

    await _setup_api_fallback_guild(db, "853", monkeypatch)
    CACHE.clan_name_cache = {"#KN0WNCLAN": {"name": "Known Clan"}}
    monkeypatch.setattr(CACHE.coc_clan_cache, "get_clan", AsyncMock())
    monkeypatch.setattr(CACHE, "get_player", AsyncMock())

    resp = await client.get(
        "/api/cwl/guest-search?guild_id=853&discord_user_id=42&q=known",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert [r["clan_tag"] for r in body["results"] if r["type"] == "clan"] == ["#KN0WNCLAN"]
    CACHE.coc_clan_cache.get_clan.assert_not_awaited()
    CACHE.get_player.assert_not_awaited()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guest_search_api_miss_is_negative_cached(db, bridge_config, client, monkeypatch):
    """A definitive double-NotFound is remembered briefly so a run of keystrokes over the same
    non-existent tag doesn't re-issue two API calls each time."""
    import coc  # type: ignore[import-untyped]

    from qapbot.cache_manager import CACHE
    from qapbot import web_bridge

    await _setup_api_fallback_guild(db, "854", monkeypatch)
    web_bridge._guest_tag_api_misses.clear()
    get_clan = AsyncMock(side_effect=coc.NotFound(MagicMock(), "gone"))
    monkeypatch.setattr(CACHE.coc_clan_cache, "get_clan", get_clan)
    monkeypatch.setattr(CACHE, "get_player", AsyncMock(return_value=None))

    for _ in range(3):
        resp = await client.get(
            "/api/cwl/guest-search?guild_id=854&discord_user_id=42&q=%23APIMISS1",
            headers={"X-Bridge-Secret": "test-secret"},
        )
        assert resp.status == 200
    assert get_clan.await_count == 1


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
    _seed_player_names(db, {"#LONER": "Qaplike"})  # matches "qap" by name, but not linked
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
    _seed_player_names(db, {f"#PLAYER{i:02d}": f"Test Player {i:02d}" for i in range(15)})
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
    _seed_player_names(db, {"#2ABCDEFGH": "SomePlayer"})
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
    _seed_player_names(db, {f"#QAP{i:03d}": f"Player {i:03d}" for i in range(20)})
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
async def test_guest_search_hash_prefix_uses_sqlite(db, bridge_config, client, monkeypatch):
    """2026-08-17 (CWL_PROD_PERFORMANCE_FIX_PLAN.md P2 Step 11), unconditional since 2026-08-18
    (PLAYER_NAME_INDEX_RETIREMENT_PLAN.md Steps 5-6, the in-memory fallback and its rollout flag
    both retired): the # tag-mode path delegates to db.search_player_tags_by_prefix_sync()
    (SQLite, PK-prefix-indexed) — proven here by seeding ONLY the real DB (via
    update_player_name_index_sync, which also populates player_name_search); a correct result
    is only possible if the SQLite path actually ran."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "835", {})
    CACHE.db_manager = db
    CACHE.server_config["835"] = {"member_clans": [], "member_families": []}
    CACHE.clan_name_cache = {}
    CACHE.user_accounts = {}
    db.update_player_name_index_sync([("#QAPFTS1", "FtsPlayer", "2026-08-17T00:00")])

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(835, 42, is_admin=True))

    resp = await client.get(
        "/api/cwl/guest-search?guild_id=835&discord_user_id=42&q=%23QAPFTS",
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    player_hits = [r for r in body["results"] if r["type"] == "player"]
    # Includes the pre-existing "raw unindexed tag" fallback (query len >= 5, not itself an
    # exact key in the hits) alongside the real SQLite-backed match — unrelated to this test's
    # own point, just the existing behavior at this query length.
    assert {"type": "player", "player_tag": "#QAPFTS1", "player_name": "FtsPlayer", "discord_id": None} in player_hits
    assert len(player_hits) == 2


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
    which other guild(s) it touched. Triggered via a drag-and-drop assign into the shared clan
    itself (2026-08-19: the board's 1-click admin confirm control this test used to trigger the
    write with was removed entirely — deprecated, dead code — so the write is now made the same
    way a real admin actually makes one)."""
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

    # Guild 8605 (the owner) drags the shared player into #CLAN1's own column — the write that
    # must release 8606's wait.
    assign_resp = await client.post(
        "/api/cwl/enrollment/assign",
        json={"guild_id": 8605, "discord_user_id": 42, "player_tag": "#P1", "clan_tag": "#CLAN1"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert assign_resp.status == 200

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

    resp = await client.post(
        "/api/cwl/enrollment/guest",
        json={
            "guild_id": 806, "discord_user_id": 42, "player_tag": "#guest1",
            "player_name": "GuestOne", "discord_id": None,
        },
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body == {"ok": True}
    signup = db.get_cwl_signup_sync(event_id, "#GUEST1")
    assert signup is not None
    assert signup["player_name"] == "GuestOne"
    assert signup["source"] == "guest_invite"
    assert signup["status"] == "pending"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_guest_never_sends_an_immediate_dm(db, bridge_config, client, monkeypatch):
    """2026-08-18, CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md rules c/e: guest player invites
    only add to the pool now — the "Send enrollment DM immediately" checkbox and its backend
    branch are gone entirely (even with a linked discord_id, which used to be enough to trigger
    an immediate send). The DM only ever goes out later, via Start Enrollment or the rule-h
    "notify new pool members" flow."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "807", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["807"] = {"member_clans": ["#CLAN1"], "member_families": []}

    event_id = db.create_cwl_event_sync("807", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(807, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())
    send_dm_mock = AsyncMock(return_value=(True, "sent"))
    monkeypatch.setattr("qapbot.QBdiscocmdshelper_cwl.send_cwl_signup_template_dm", send_dm_mock)

    resp = await client.post(
        "/api/cwl/enrollment/guest",
        json={
            "guild_id": 807, "discord_user_id": 42, "player_tag": "#GUEST2",
            "player_name": "GuestTwo", "discord_id": "999",
        },
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body == {"ok": True}
    send_dm_mock.assert_not_awaited()
    signup = db.get_cwl_signup_sync(event_id, "#GUEST2")
    assert signup["dmed_discord_id"] == "999"  # linked and pooled, just not DMed yet


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_guest_no_event_returns_409(bridge_config, client, monkeypatch):
    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(808, 42, is_admin=True))

    resp = await client.post(
        "/api/cwl/enrollment/guest",
        json={
            "guild_id": 808, "discord_user_id": 42, "player_tag": "#GUEST3",
            "player_name": "GuestThree", "discord_id": None,
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
            "player_name": "GuestFour", "discord_id": None,
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


# ---------------------------------------------------------------------------
# POST /api/cwl/enrollment/status — admin enrollment-status override from the Manage Enrollment
# board's right-click menu (2026-08-22, tracker #0014). Confirmed/Declined deliberately leave the
# player's DM live (that's what keeps "last action wins" symmetric); only Pending retracts it,
# clears the global dm_sent dedup, and sends a fresh one.
# ---------------------------------------------------------------------------

def _fake_dm_bot(guild_id: int, discord_user_id: int, deleted_message_ids: list):
    """_fake_admin_bot plus a working fetch_user -> dm_channel -> fetch_message -> delete chain,
    so cleanup_stale_cwl_enrollment_dms() can actually retract a DM and record which one."""
    bot = _fake_admin_bot(guild_id, discord_user_id, is_admin=True)

    message = MagicMock()

    async def _delete():
        deleted_message_ids.append(message.id)

    message.delete = _delete

    dm_channel = MagicMock()
    dm_channel.fetch_message = AsyncMock(side_effect=lambda mid: (setattr(message, "id", mid), message)[1])

    user = MagicMock()
    user.dm_channel = dm_channel
    bot.fetch_user = AsyncMock(return_value=user)
    return bot


async def _seed_status_event(db, guild_id: str, season: str = "2026-09"):
    """One signup_open event with a single participating clan and one linked, pooled player."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, guild_id, {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha", "war_league": "Master League II"}}
    CACHE.server_config[guild_id] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    event_id = db.create_cwl_event_sync(guild_id, season, "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    db.update_cwl_event_status_sync(event_id, "signup_open")

    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES ('70', 'Owner')")
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag) "
        "VALUES ('70', '#P1', 'PlayerOne', 1, '#CLAN1')"
    )
    await db.conn.commit()
    return event_id


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_status_confirmed_writes_local_and_global_and_sends_no_dm(
    db, bridge_config, client, monkeypatch,
):
    from qapbot.cache_manager import CACHE

    event_id = await _seed_status_event(db, "840")
    db.upsert_cwl_signup_sync(event_id, "#P1", "PlayerOne", "70", None, "template_confirm", "pending")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(840, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())
    sent = []
    monkeypatch.setattr(
        CACHE, "send_user_dm_detailed",
        AsyncMock(side_effect=lambda *a, **kw: (sent.append(a), (True, "sent"))[1]),
    )

    resp = await client.post(
        "/api/cwl/enrollment/status",
        json={"guild_id": 840, "discord_user_id": 42, "player_tag": "#P1", "status": "confirmed"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True and body["status"] == "confirmed"
    # dm is null for confirmed/declined — those never touch the player's DM at all.
    assert body["dm"] is None

    signup = db.get_cwl_signup_sync(event_id, "#P1")
    assert signup["status"] == "confirmed"
    assert signup["responded_at"] is not None
    global_row = db.get_cwl_player_season_status_sync("#P1", "2026-09")
    assert global_row["status"] == "confirmed"
    # The player's own DM stays live so they can still overwrite this — that IS "last action wins".
    assert sent == []


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_status_declined_creates_a_signup_row_when_none_existed(
    db, bridge_config, client, monkeypatch,
):
    """A pooled family member the board shows but Start Enrollment never seeded a cwl_signups row
    for still needs somewhere to hold the admin's decision."""
    event_id = await _seed_status_event(db, "841")
    assert db.get_cwl_signup_sync(event_id, "#P1") is None

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(841, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    resp = await client.post(
        "/api/cwl/enrollment/status",
        json={"guild_id": 841, "discord_user_id": 42, "player_tag": "#P1", "status": "declined"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200

    signup = db.get_cwl_signup_sync(event_id, "#P1")
    assert signup is not None
    assert signup["status"] == "declined"
    # Live ownership (user_players), never a stale snapshot — Pitfall 37.
    assert signup["dmed_discord_id"] == "70"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_status_pending_retracts_old_dm_clears_dm_sent_and_resends(
    db, bridge_config, client, monkeypatch,
):
    from qapbot import config as config_module
    from qapbot.cache_manager import CACHE

    monkeypatch.setattr(
        config_module, "CONFIG",
        dataclasses.replace(config_module.CONFIG, is_dev_mode=False, cwl_dm_restrict_to_admin=False),
    )

    event_id = await _seed_status_event(db, "842")
    db.upsert_cwl_signup_sync(event_id, "#P1", "PlayerOne", "70", None, "template_confirm", "confirmed")
    db.mark_cwl_player_dm_sent_sync(
        "#P1", "2026-09", "PlayerOne", "70", event_id, 842, "2026-08-20T09:00Z",
        message_id="55501", channel_id="99901",
    )

    deleted: list = []
    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_dm_bot(842, 42, deleted))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    contacted = []

    async def fake_send_user_dm_detailed(user_id, message, view=None, embed=None, sent_message_out=None):
        contacted.append(user_id)
        dm_message = MagicMock()
        dm_message.id = 77702
        dm_message.channel.id = 99901
        if sent_message_out is not None:
            sent_message_out.append(dm_message)
        return True, "sent"

    monkeypatch.setattr(CACHE, "send_user_dm_detailed", fake_send_user_dm_detailed)

    resp = await client.post(
        "/api/cwl/enrollment/status",
        json={"guild_id": 842, "discord_user_id": 42, "player_tag": "#P1", "status": "pending"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["dm"] == {"sent": True, "reason": None}

    # The stale DM was retracted...
    assert deleted == [55501]
    # ...and a fresh one went to the account's current owner.
    assert contacted == ["70"]
    assert db.get_cwl_signup_sync(event_id, "#P1")["status"] == "pending"
    assert db.get_cwl_signup_sync(event_id, "#P1")["responded_at"] is None
    global_row = db.get_cwl_player_season_status_sync("#P1", "2026-09")
    assert global_row["status"] == "pending"
    # Re-marked by the fresh send — the point is that clear_cwl_player_dm_sent_sync ran in
    # between, so the batch's global dedup didn't skip it (it would otherwise still be 55501).
    assert global_row["dm_sent"] == 1
    assert global_row["dm_sent_via_message_id"] == "77702"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_status_pending_for_unlinked_player_still_resets_but_reports_unlinked(
    db, bridge_config, client, monkeypatch,
):
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "843", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["843"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}
    event_id = db.create_cwl_event_sync("843", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    db.update_cwl_event_status_sync(event_id, "signup_open")
    # A guest tag added by search that was never linked to any Discord account.
    db.upsert_cwl_signup_sync(event_id, "#GUEST", "Guesty", None, None, "guest_invite", "confirmed")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(843, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())
    contacted = []
    monkeypatch.setattr(
        CACHE, "send_user_dm_detailed",
        AsyncMock(side_effect=lambda uid, *a, **kw: (contacted.append(uid), (True, "sent"))[1]),
    )

    resp = await client.post(
        "/api/cwl/enrollment/status",
        json={"guild_id": 843, "discord_user_id": 42, "player_tag": "#GUEST", "status": "pending"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["dm"] == {"sent": False, "reason": "unlinked"}
    # The status reset still stands — there is simply nobody to ask.
    assert db.get_cwl_signup_sync(event_id, "#GUEST")["status"] == "pending"
    assert contacted == []


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_status_propagates_to_another_guild_pooling_the_same_player(
    db, bridge_config, client, monkeypatch,
):
    """rule h: the admin's decision is the season's global truth, so every OTHER guild that has
    this player pooled sees it on their own board too — same fan-out the DM button already does."""
    event_id = await _seed_status_event(db, "844")
    db.upsert_cwl_signup_sync(event_id, "#P1", "PlayerOne", "70", None, "template_confirm", "pending")

    await _seed_guild_and_clans(db, "845", {"#CLAN2": "Beta"})
    other_event_id = db.create_cwl_event_sync("845", "2026-09", "discordid2")
    db.set_cwl_event_clans_sync(other_event_id, [{"clan_tag": "#CLAN2", "participating": True}])
    db.upsert_cwl_signup_sync(other_event_id, "#P1", "PlayerOne", "70", None, "template_confirm", "pending")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(844, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    resp = await client.post(
        "/api/cwl/enrollment/status",
        json={"guild_id": 844, "discord_user_id": 42, "player_tag": "#P1", "status": "confirmed"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    assert db.get_cwl_signup_sync(other_event_id, "#P1")["status"] == "confirmed"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_player_dm_button_overrides_a_later_admin_status_last_action_wins(
    db, bridge_config, client, monkeypatch,
):
    """The spec's conflict rule, in the direction that matters most: the admin sets Confirmed
    while the player's DM is still sitting unanswered, then the player clicks Opt Out. The
    player's answer is the later action, so it must win — which it does precisely because
    Confirmed never retracted their DM."""
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import CwlSignupResponseButton

    event_id = await _seed_status_event(db, "846")
    db.upsert_cwl_signup_sync(event_id, "#P1", "PlayerOne", "70", None, "template_confirm", "pending")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(846, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    resp = await client.post(
        "/api/cwl/enrollment/status",
        json={"guild_id": 846, "discord_user_id": 42, "player_tag": "#P1", "status": "confirmed"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    assert db.get_cwl_player_season_status_sync("#P1", "2026-09")["status"] == "confirmed"

    interaction = MagicMock()
    interaction.user.id = 70
    interaction.response.edit_message = AsyncMock()
    interaction.response.send_message = AsyncMock()
    CACHE.db_manager = db

    await CwlSignupResponseButton("optout", event_id, "#P1").callback(interaction)

    assert db.get_cwl_signup_sync(event_id, "#P1")["status"] == "declined"
    assert db.get_cwl_player_season_status_sync("#P1", "2026-09")["status"] == "declined"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_status_rejects_bad_status_non_admin_and_missing_event(
    db, bridge_config, client, monkeypatch,
):
    await _seed_status_event(db, "847")
    import QBcore

    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(847, 42, is_admin=True))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())

    # 400 — 'withdrawn' is legacy-only and deliberately not admin-settable.
    resp = await client.post(
        "/api/cwl/enrollment/status",
        json={"guild_id": 847, "discord_user_id": 42, "player_tag": "#P1", "status": "withdrawn"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 400

    # 403 — neither admin nor leader.
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(847, 42, is_admin=False))
    resp = await client.post(
        "/api/cwl/enrollment/status",
        json={"guild_id": 847, "discord_user_id": 42, "player_tag": "#P1", "status": "confirmed"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 403

    # 403 — the shared-secret gate, ahead of everything else.
    resp = await client.post(
        "/api/cwl/enrollment/status",
        json={"guild_id": 847, "discord_user_id": 42, "player_tag": "#P1", "status": "confirmed"},
        headers={"X-Bridge-Secret": "wrong"},
    )
    assert resp.status == 403

    # 409 — the guild has no event for the selected season at all.
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(848, 42, is_admin=True))
    resp = await client.post(
        "/api/cwl/enrollment/status",
        json={"guild_id": 848, "discord_user_id": 42, "player_tag": "#P1", "status": "confirmed"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 409


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_status_rejects_a_draft_event(db, bridge_config, client, monkeypatch):
    """Nothing has been enrolled yet in a draft event, so there is no status to override — the
    same guard notify_new_cwl_pool_members() already applies before DMing anyone."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "849", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["849"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}
    event_id = db.create_cwl_event_sync("849", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    # left in its default 'draft' status

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(849, 42, is_admin=True))

    resp = await client.post(
        "/api/cwl/enrollment/status",
        json={"guild_id": 849, "discord_user_id": 42, "player_tag": "#P1", "status": "confirmed"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 409


@pytest.mark.discord
@pytest.mark.asyncio
async def test_enrollment_status_pending_never_redms_a_stale_owner_of_an_unassigned_account(
    db, bridge_config, client, monkeypatch,
):
    """Pitfall 37: an account moved to the UNASSIGNED pool has no owner at all right now. Both
    snapshots (cwl_signups.dmed_discord_id and cwl_player_season_status.dmed_discord_id) still
    name whoever was DMed months ago — falling back to them would re-stamp that stale owner AND
    aim a fresh DM at someone who no longer owns the account."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "850", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["850"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}
    event_id = db.create_cwl_event_sync("850", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    db.update_cwl_event_status_sync(event_id, "signup_open")

    # The account is in the UNASSIGNED pool — a real user_players row exists, but nobody owns it.
    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES ('UNASSIGNED', '-')")
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag) "
        "VALUES ('UNASSIGNED', '#ORPHAN', 'Orphan', 0, '#CLAN1')"
    )
    await db.conn.commit()
    # ...while both snapshots still name the previous owner.
    db.upsert_cwl_signup_sync(event_id, "#ORPHAN", "Orphan", "999", None, "template_confirm", "confirmed")
    db.mark_cwl_player_dm_sent_sync(
        "#ORPHAN", "2026-09", "Orphan", "999", event_id, 850, "2026-08-20T09:00Z",
        message_id="55502", channel_id="99902",
    )

    deleted: list = []
    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_dm_bot(850, 42, deleted))
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", AsyncMock())
    contacted = []
    monkeypatch.setattr(
        CACHE, "send_user_dm_detailed",
        AsyncMock(side_effect=lambda uid, *a, **kw: (contacted.append(uid), (True, "sent"))[1]),
    )

    resp = await client.post(
        "/api/cwl/enrollment/status",
        json={"guild_id": 850, "discord_user_id": 42, "player_tag": "#ORPHAN", "status": "pending"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    assert (await resp.json())["dm"] == {"sent": False, "reason": "unlinked"}

    # The old owner's DM is still retracted (it points at an account they no longer own)...
    assert deleted == [55502]
    # ...but no new DM was aimed at them, and the row no longer re-stamps them as the recipient.
    assert contacted == []
    signup = db.get_cwl_signup_sync(event_id, "#ORPHAN")
    assert signup["status"] == "pending"
    assert signup["dmed_discord_id"] is None


# ---------------------------------------------------------------------------
# GET/POST /api/cwl/player-prefs, POST /api/cwl/player-prefs/status —
# plans/cwl-personal-hub.md Phase 5c, the Player CWL Settings Hub's Activity screen.
# ---------------------------------------------------------------------------

async def _link_player_prefs_account(
    db, discord_id: str, player_tag: str, player_name: str = "Player",
    clan_tag=None, cwl_permanent_optout: bool = False, cwl_permanent_optin: bool = False,
    cwl_optout_send_dm_anyway: bool = False, cwl_default_preferred_league_rank=None,
) -> None:
    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES (?, ?)", (discord_id, discord_id))
    await db.conn.execute(
        "INSERT INTO user_players "
        "(discord_id, player_tag, player_name, verified, current_clan_tag, cwl_permanent_optout, "
        " cwl_permanent_optin, cwl_optout_send_dm_anyway, cwl_default_preferred_league_rank) "
        "VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)",
        (
            discord_id, player_tag, player_name, clan_tag,
            1 if cwl_permanent_optout else 0, 1 if cwl_permanent_optin else 0,
            1 if cwl_optout_send_dm_anyway else 0, cwl_default_preferred_league_rank,
        ),
    )
    await db.conn.commit()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_player_prefs_get_no_linked_accounts_returns_empty(db, bridge_config, client):
    from qapbot.cache_manager import CACHE

    CACHE.db_manager = db
    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES ('950')")
    await db.conn.commit()

    resp = await client.get(
        "/api/cwl/player-prefs",
        params={"guild_id": "950", "discord_user_id": "999999"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body == {"season": None, "event_status": None, "accounts": [], "season_rows": []}


@pytest.mark.discord
@pytest.mark.asyncio
async def test_player_prefs_get_one_account_no_event(db, bridge_config, client):
    from qapbot.cache_manager import CACHE

    CACHE.db_manager = db
    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES ('951')")
    await db.conn.commit()
    await _link_player_prefs_account(
        db, "60", "#SOLO", player_name="Solo", cwl_permanent_optout=True,
        cwl_optout_send_dm_anyway=True, cwl_default_preferred_league_rank="Gold League I",
    )

    resp = await client.get(
        "/api/cwl/player-prefs",
        params={"guild_id": "951", "discord_user_id": "60"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    body = await resp.json()
    assert body["season"] is None
    assert body["season_rows"] == []
    assert len(body["accounts"]) == 1
    account = body["accounts"][0]
    assert account["player_tag"] == "#SOLO"
    assert account["mode"] == "optout"
    assert account["send_dm_anyway"] is True
    assert account["preferred_league_rank"] == "Gold League I"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_player_prefs_get_three_accounts_sorted_by_name(db, bridge_config, client):
    from qapbot.cache_manager import CACHE

    CACHE.db_manager = db
    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES ('952')")
    await db.conn.commit()
    await _link_player_prefs_account(db, "61", "#C", player_name="Charlie")
    await _link_player_prefs_account(db, "61", "#A", player_name="Alpha")
    await _link_player_prefs_account(db, "61", "#B", player_name="Bravo")

    resp = await client.get(
        "/api/cwl/player-prefs",
        params={"guild_id": "952", "discord_user_id": "61"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    body = await resp.json()
    assert [a["player_tag"] for a in body["accounts"]] == ["#A", "#B", "#C"]


@pytest.mark.discord
@pytest.mark.asyncio
async def test_player_prefs_get_season_rows_assigned_and_unassigned(db, bridge_config, client):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import assign_cwl_player_sync

    await _seed_guild_and_clans(db, "953", {"#CLANX": "Xylo"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLANX": {"name": "Xylo", "war_league": "Crystal League I"}}
    CACHE.server_config["953"] = {"member_clans": ["#CLANX"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    event_id = db.create_cwl_event_sync("953", "2026-09", "admin1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLANX", "participating": True}])
    db.update_cwl_event_status_sync(event_id, "signup_open")

    await _link_player_prefs_account(db, "62", "#ASSIGNED", player_name="Assigned", clan_tag="#CLANX")
    await _link_player_prefs_account(db, "62", "#FREE", player_name="Free")
    db.upsert_cwl_signup_sync(event_id, "#ASSIGNED", "Assigned", "62", None, "template_confirm", "confirmed")
    db.upsert_cwl_signup_sync(event_id, "#FREE", "Free", "62", None, "template_confirm", "pending")
    assign_cwl_player_sync(953, event_id, "2026-09", "#ASSIGNED", "#CLANX", source="admin")

    resp = await client.get(
        "/api/cwl/player-prefs",
        params={"guild_id": "953", "discord_user_id": "62"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    body = await resp.json()
    assert body["season"] == "2026-09"
    assert body["event_status"] == "signup_open"
    rows = {r["player_tag"]: r for r in body["season_rows"]}
    assert rows["#ASSIGNED"]["signup_status"] == "confirmed"
    assert rows["#ASSIGNED"]["assigned_clan_tag"] == "#CLANX"
    assert rows["#ASSIGNED"]["assigned_clan_name"] == "Xylo"
    assert rows["#ASSIGNED"]["assigned_clan_tier"] == "Crystal League I"
    assert rows["#FREE"]["signup_status"] == "pending"
    assert rows["#FREE"]["assigned_clan_tag"] is None


@pytest.mark.discord
@pytest.mark.asyncio
async def test_player_prefs_post_applies_change_and_returns_rebuilt_payload(db, bridge_config, client):
    from qapbot.cache_manager import CACHE

    CACHE.db_manager = db
    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES ('954')")
    await db.conn.commit()
    await _link_player_prefs_account(db, "63", "#MINE", player_name="Mine")

    resp = await client.post(
        "/api/cwl/player-prefs",
        json={
            "guild_id": "954", "discord_user_id": "63",
            "changes": [{"player_tag": "#MINE", "mode": "optin"}],
        },
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["accounts"][0]["mode"] == "optin"
    links = db.get_player_links_sync(["#MINE"])
    assert links["#MINE"]["cwl_permanent_optin"] is True


@pytest.mark.discord
@pytest.mark.asyncio
async def test_player_prefs_post_null_player_tag_applies_to_all_owned_accounts(db, bridge_config, client):
    from qapbot.cache_manager import CACHE

    CACHE.db_manager = db
    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES ('955')")
    await db.conn.commit()
    await _link_player_prefs_account(db, "64", "#ONE", player_name="One")
    await _link_player_prefs_account(db, "64", "#TWO", player_name="Two")
    await _link_player_prefs_account(db, "65", "#OTHER", player_name="Other")

    resp = await client.post(
        "/api/cwl/player-prefs",
        json={"guild_id": "955", "discord_user_id": "64", "changes": [{"player_tag": None, "mode": "optout"}]},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    links = db.get_player_links_sync(["#ONE", "#TWO", "#OTHER"])
    assert links["#ONE"]["cwl_permanent_optout"] is True
    assert links["#TWO"]["cwl_permanent_optout"] is True
    assert links["#OTHER"]["cwl_permanent_optout"] is False


@pytest.mark.discord
@pytest.mark.asyncio
async def test_player_prefs_post_rejects_a_player_tag_owned_by_someone_else(db, bridge_config, client):
    from qapbot.cache_manager import CACHE

    CACHE.db_manager = db
    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES ('956')")
    await db.conn.commit()
    await _link_player_prefs_account(db, "66", "#MINE2", player_name="Mine2")
    await _link_player_prefs_account(db, "67", "#NOTMINE", player_name="NotMine")

    resp = await client.post(
        "/api/cwl/player-prefs",
        json={
            "guild_id": "956", "discord_user_id": "66",
            "changes": [
                {"player_tag": "#MINE2", "mode": "optin"},
                {"player_tag": "#NOTMINE", "mode": "optin"},
            ],
        },
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 403

    links = db.get_player_links_sync(["#MINE2", "#NOTMINE"])
    assert links["#MINE2"]["cwl_permanent_optin"] is False
    assert links["#NOTMINE"]["cwl_permanent_optin"] is False


@pytest.mark.discord
@pytest.mark.asyncio
async def test_player_prefs_status_confirm_updates_signup_and_reconciles_dm(db, bridge_config, client, monkeypatch):
    event_id = await _seed_status_event(db, "957")
    db.upsert_cwl_signup_sync(event_id, "#P1", "PlayerOne", "70", None, "template_confirm", "pending")
    db.mark_cwl_player_dm_sent_sync(
        "#P1", "2026-09", "PlayerOne", "70", event_id, 957, "2026-09-01T09:00Z", "600001", "700001",
    )

    import QBcore
    deleted: list = []
    edited: list = []
    bot = _fake_dm_bot(957, 70, deleted)

    async def _capture_edit(**kwargs):
        edited.append(kwargs)

    async def _fetch_message(mid):
        msg = MagicMock()
        msg.id = mid
        msg.edit = _capture_edit
        return msg

    bot.fetch_user.return_value.dm_channel.fetch_message = AsyncMock(side_effect=_fetch_message)
    monkeypatch.setattr(QBcore, "bot", bot)

    resp = await client.post(
        "/api/cwl/player-prefs/status",
        json={"guild_id": "957", "discord_user_id": "70", "player_tag": "#P1", "action": "confirm"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 200
    assert db.get_cwl_signup_sync(event_id, "#P1")["status"] == "confirmed"
    assert len(edited) == 1
    assert edited[0]["view"] is None


@pytest.mark.discord
@pytest.mark.asyncio
async def test_player_prefs_status_rejects_a_different_accounts_signup(db, bridge_config, client, monkeypatch):
    event_id = await _seed_status_event(db, "958")
    db.upsert_cwl_signup_sync(event_id, "#P1", "PlayerOne", "70", None, "template_confirm", "pending")

    import QBcore
    monkeypatch.setattr(QBcore, "bot", _fake_admin_bot(958, 999, is_admin=False))

    resp = await client.post(
        "/api/cwl/player-prefs/status",
        json={"guild_id": "958", "discord_user_id": "999", "player_tag": "#P1", "action": "confirm"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 403
    body = await resp.json()
    assert body["error"] == "not_your_signup"
    assert db.get_cwl_signup_sync(event_id, "#P1")["status"] == "pending"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_player_prefs_status_rejects_when_enrollment_not_open(db, bridge_config, client):
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clans(db, "959", {"#CLANY": "Yankee"})
    CACHE.db_manager = db
    CACHE.server_config["959"] = {"member_clans": ["#CLANY"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}
    event_id = db.create_cwl_event_sync("959", "2026-09", "admin1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLANY", "participating": True}])
    db.upsert_cwl_signup_sync(event_id, "#P1", "PlayerOne", "70", None, "template_confirm", "pending")
    await _link_player_prefs_account(db, "70", "#P1", player_name="PlayerOne")

    resp = await client.post(
        "/api/cwl/player-prefs/status",
        json={"guild_id": "959", "discord_user_id": "70", "player_tag": "#P1", "action": "confirm"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 409
    body = await resp.json()
    assert body["error"] == "signup_closed"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_player_prefs_status_no_event_returns_409(db, bridge_config, client):
    from qapbot.cache_manager import CACHE

    CACHE.db_manager = db
    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES ('960')")
    await db.conn.commit()

    resp = await client.post(
        "/api/cwl/player-prefs/status",
        json={"guild_id": "960", "discord_user_id": "70", "player_tag": "#GHOST", "action": "confirm"},
        headers={"X-Bridge-Secret": "test-secret"},
    )
    assert resp.status == 409
    body = await resp.json()
    assert body["error"] == "no_longer_valid"
