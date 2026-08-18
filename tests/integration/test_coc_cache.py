from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


@pytest.mark.integration
@pytest.mark.asyncio
async def test_coc_cache_fresh_hit_does_not_fetch(monkeypatch: pytest.MonkeyPatch):
    from qapbot.coc_cache import CoCClanCache

    cache = CoCClanCache(soft_ttl_seconds=100, hard_ttl_seconds=200)
    cache.cache_manager = object()  # minimal truthy placeholder

    now = datetime.now(timezone.utc)
    cache.cache["#CLAN"] = {"data": "CACHED", "timestamp": now - timedelta(seconds=10)}

    fetch_called = False

    async def _fetch_and_cache(_tag: str):
        nonlocal fetch_called
        fetch_called = True
        return "FETCHED"

    monkeypatch.setattr(cache, "_fetch_and_cache", _fetch_and_cache)
    # Also avoid the 'client not initialized' check by providing a minimal coc_client
    cache.cache_manager = type("M", (), {"coc_client": object()})()

    out = await cache.get_clan("#CLAN")
    assert out == "CACHED"
    assert fetch_called is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_coc_cache_stale_schedules_background_refresh(monkeypatch: pytest.MonkeyPatch):
    from qapbot.coc_cache import CoCClanCache

    cache = CoCClanCache(soft_ttl_seconds=5, hard_ttl_seconds=50)
    cache.cache_manager = type("M", (), {"coc_client": object()})()

    now = datetime.now(timezone.utc)
    cache.cache["#CLAN"] = {"data": "CACHED", "timestamp": now - timedelta(seconds=10)}

    scheduled: list[str] = []

    def _schedule(tag: str) -> None:
        scheduled.append(tag)

    monkeypatch.setattr(cache, "_schedule_background_refresh", _schedule)

    out = await cache.get_clan("#CLAN")
    assert out == "CACHED"
    assert scheduled == ["#CLAN"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_coc_cache_expired_forces_blocking_fetch(monkeypatch: pytest.MonkeyPatch):
    from qapbot.coc_cache import CoCClanCache

    cache = CoCClanCache(soft_ttl_seconds=5, hard_ttl_seconds=8)
    cache.cache_manager = type("M", (), {"coc_client": object()})()

    now = datetime.now(timezone.utc)
    cache.cache["#CLAN"] = {"data": "CACHED", "timestamp": now - timedelta(seconds=30)}

    async def _fetch_and_cache(_tag: str):
        return "FETCHED"

    monkeypatch.setattr(cache, "_fetch_and_cache", _fetch_and_cache)

    out = await cache.get_clan("#CLAN")
    assert out == "FETCHED"

@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_player_info_maps_coc_role_name_correctly():
    """
    Verify update_player_info_in_user_accounts produces the correct coc_role strings.

    coc.Role.name values are:
        "member", "elder", "co_leader", "leader"
    We must store:
        "member", "elder", "coLeader", "leader"
    i.e. co_leader is remapped to coLeader to match COC_ROLE_PRIORITY keys.
    """
    from unittest.mock import AsyncMock, MagicMock
    from qapbot.coc_cache import CoCClanCache

    def _make_member(tag: str, role_name: str) -> MagicMock:
        m = MagicMock()
        m.tag = tag
        m.town_hall = 14
        r = MagicMock()
        r.name = role_name
        m.role = r
        return m

    clan_obj = MagicMock()
    clan_obj.tag = "#CLAN"
    clan_obj.members = [
        _make_member("#P1", "member"),
        _make_member("#P2", "elder"),
        _make_member("#P3", "co_leader"),  # must be remapped to "coLeader"
        _make_member("#P4", "leader"),
    ]

    def _player(tag: str) -> dict:
        return {"player_tag": tag, "coc_role": None, "current_clan_tag": None, "th_level": None}

    cache_manager = MagicMock()
    cache_manager.user_accounts = {
        "1": {"players": [_player("#P1")]},
        "2": {"players": [_player("#P2")]},
        "3": {"players": [_player("#P3")]},
        "4": {"players": [_player("#P4")]},
    }
    cache_manager.persist_user = AsyncMock()
    cache_manager.db_manager = None  # exercise the full-scan fallback

    cache = CoCClanCache()
    await cache.update_player_info_in_user_accounts(clan_obj, cache_manager)

    assert cache_manager.user_accounts["1"]["players"][0]["coc_role"] == "member"
    assert cache_manager.user_accounts["2"]["players"][0]["coc_role"] == "elder"
    assert cache_manager.user_accounts["3"]["players"][0]["coc_role"] == "coLeader", \
        "co_leader (role.name) must be remapped to coLeader to match COC_ROLE_PRIORITY"
    assert cache_manager.user_accounts["4"]["players"][0]["coc_role"] == "leader"