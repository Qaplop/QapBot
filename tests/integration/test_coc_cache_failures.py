from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, call

import pytest


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_clan_raises_when_client_missing():
    from qapbot.coc_cache import CoCClanCache
    from qapbot.exceptions import CacheError

    cache = CoCClanCache()

    with pytest.raises(CacheError):
        await cache.get_clan("#CLAN")


@pytest.mark.integration
def test_clear_expired_removes_only_hard_expired_entries():
    from qapbot.coc_cache import CoCClanCache

    now = datetime.now(timezone.utc)
    cache = CoCClanCache(soft_ttl_seconds=5, hard_ttl_seconds=30)
    cache.cache = {
        "#OLD": {"data": object(), "timestamp": now - timedelta(seconds=120)},
        "#NEW": {"data": object(), "timestamp": now - timedelta(seconds=10)},
    }

    removed = cache.clear_expired()

    assert removed == 1
    assert "#OLD" not in cache.cache
    assert "#NEW" in cache.cache


@pytest.mark.integration
def test_get_stats_empty_and_non_empty():
    from qapbot.coc_cache import CoCClanCache

    cache = CoCClanCache()
    empty = cache.get_stats()
    assert empty["size"] == 0

    now = datetime.now(timezone.utc)
    cache.cache["#A"] = {"data": object(), "timestamp": now - timedelta(seconds=5)}
    cache.cache["#B"] = {"data": object(), "timestamp": now - timedelta(seconds=15)}

    stats = cache.get_stats()
    assert stats["size"] == 2
    assert stats["oldest_age_seconds"] >= stats["newest_age_seconds"]


@pytest.mark.integration
def test_get_memory_usage_mb_empty_is_zero():
    from qapbot.coc_cache import CoCClanCache

    cache = CoCClanCache()
    assert cache.get_memory_usage_mb() == 0.0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_warlog_status_persists_when_status_changes():
    from qapbot.coc_cache import CoCClanCache

    cache = CoCClanCache()
    persist_clan = AsyncMock()
    cache.cache_manager = cast(Any, SimpleNamespace(
        clan_name_cache={"#CLAN": {"warlog_is_public": True, "name": "Clan"}},
        persist_clan=persist_clan,
    ))

    clan_obj = SimpleNamespace(tag="#CLAN", is_war_log_public=False)

    update_warlog_status = getattr(cache, "_update_warlog_status")
    await update_warlog_status(cast(Any, clan_obj))

    cache_manager = cast(Any, cache.cache_manager)
    assert cache_manager.clan_name_cache["#CLAN"]["warlog_is_public"] is False
    persist_clan.assert_awaited_once_with("#CLAN")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_player_info_updates_and_persists_only_affected_users():
    from qapbot.coc_cache import CoCClanCache

    cache = CoCClanCache()

    clan_obj = SimpleNamespace(
        tag="#CLAN",
        members=[SimpleNamespace(tag="#P1", town_hall=16, name="PlayerOne")],
    )

    persist_user = AsyncMock()
    cache_manager = SimpleNamespace(
        user_accounts={
            "111": {
                "players": [
                    {"player_tag": "#P1", "player_name": "PlayerOne", "th_level": 14, "current_clan_tag": "#OLD"},
                    {"player_tag": "#P2", "player_name": "PlayerTwo", "th_level": 13, "current_clan_tag": "#OLD"},
                ]
            },
            "UNASSIGNED": {"players": [{"player_tag": "#P1", "player_name": "PlayerOne", "th_level": 1}]},
            "222": {"players": [{"player_tag": "#P3", "player_name": "PlayerThree", "th_level": 12, "current_clan_tag": None}]},
        },
        persist_user=persist_user,
        db_manager=None,
    )

    await cache.update_player_info_in_user_accounts(cast(Any, clan_obj), cast(Any, cache_manager))

    p1 = cache_manager.user_accounts["111"]["players"][0]
    assert p1["th_level"] == 16
    assert p1["current_clan_tag"] == "#CLAN"
    # #P1 is (inconsistently) tracked under both "111" and UNASSIGNED here — since 2026-08-14
    # the UNASSIGNED pool is no longer skipped by the periodic sync, so its stale duplicate gets
    # refreshed too rather than staying frozen forever.
    p1_unassigned = cache_manager.user_accounts["UNASSIGNED"]["players"][0]
    assert p1_unassigned["th_level"] == 16
    assert p1_unassigned["current_clan_tag"] == "#CLAN"
    assert persist_user.await_args_list == [call("111"), call("UNASSIGNED")]
