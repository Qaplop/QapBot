"""Tests for QBhelperfunctions.refresh_stale_passive_clans() — the monthly
get_clan() ping for passively-tracked (track_war_updates=False) clans whose
CWL group is never rediscovered by the normal discovery graph (see
CLAN_WAR_TRACKING.md write-path 8).
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownParameterType=false
# pyright: reportMissingParameterType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import coc  # type: ignore[import-untyped]
import pytest

import QBhelperfunctions
from QBhelperfunctions import refresh_stale_passive_clans


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _passive(last_checked: str | None) -> Dict[str, Any]:
    return {
        "name": "Clan", "war_league": "Crystal League I",
        "track_war_updates": False, "has_active_subscriptions": False,
        "is_deleted": False, "last_checked_via_api": last_checked,
    }


def _make_cache() -> MagicMock:
    cache = MagicMock()
    cache.clan_name_cache = {}
    cache.coc_clan_cache = AsyncMock()
    return cache


class TestRefreshStalePassiveClans:
    @pytest.mark.asyncio
    async def test_no_candidates_returns_zero(self, monkeypatch):
        cache = _make_cache()
        cache.clan_name_cache = {"#FRESH": _passive(_iso(1))}  # within interval
        monkeypatch.setattr(QBhelperfunctions, "CACHE", cache)

        result = await refresh_stale_passive_clans()

        assert result == 0
        cache.coc_clan_cache.get_clan.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_never_checked_and_overdue_clans_are_refreshed(self, monkeypatch):
        cache = _make_cache()
        cache.clan_name_cache = {
            "#NEVER": _passive(None),
            "#OVERDUE": _passive(_iso(45)),
            "#FRESH": _passive(_iso(2)),
        }
        monkeypatch.setattr(QBhelperfunctions, "CACHE", cache)

        result = await refresh_stale_passive_clans()

        assert result == 2
        called_tags = {c.args[0] for c in cache.coc_clan_cache.get_clan.await_args_list}
        assert called_tags == {"#NEVER", "#OVERDUE"}

    @pytest.mark.asyncio
    async def test_skips_subscribed_tracked_and_deleted_clans(self, monkeypatch):
        cache = _make_cache()
        cache.clan_name_cache = {
            "#SUB": {**_passive(None), "has_active_subscriptions": True},
            "#TRACKED": {**_passive(None), "track_war_updates": True},
            "#DELETED": {**_passive(None), "is_deleted": True},
        }
        monkeypatch.setattr(QBhelperfunctions, "CACHE", cache)

        result = await refresh_stale_passive_clans()

        assert result == 0
        cache.coc_clan_cache.get_clan.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_respects_batch_size_cap_most_overdue_first(self, monkeypatch):
        cache = _make_cache()
        monkeypatch.setattr(QBhelperfunctions, "_PASSIVE_REFRESH_BATCH_SIZE", 2)
        cache.clan_name_cache = {
            "#OLDEST": _passive(_iso(100)),
            "#OLDER": _passive(_iso(60)),
            "#OVERDUE": _passive(_iso(31)),
        }
        monkeypatch.setattr(QBhelperfunctions, "CACHE", cache)

        result = await refresh_stale_passive_clans()

        assert result == 2
        called_tags = {c.args[0] for c in cache.coc_clan_cache.get_clan.await_args_list}
        assert called_tags == {"#OLDEST", "#OLDER"}  # most overdue two, not #OVERDUE

    @pytest.mark.asyncio
    async def test_detects_promotion_after_get_clan(self, monkeypatch):
        """get_clan() -> _update_clan_metadata() flips track_war_updates to True
        in-place when the clan is now Master III+; the function must notice."""
        cache = _make_cache()
        cache.clan_name_cache = {"#PROMOTED": _passive(None)}

        async def _fake_get_clan(tag: str) -> None:
            cache.clan_name_cache[tag]["track_war_updates"] = True
            cache.clan_name_cache[tag]["war_league"] = "Master League III"

        cache.coc_clan_cache.get_clan = AsyncMock(side_effect=_fake_get_clan)
        monkeypatch.setattr(QBhelperfunctions, "CACHE", cache)

        result = await refresh_stale_passive_clans()

        assert result == 1
        assert cache.clan_name_cache["#PROMOTED"]["track_war_updates"] is True

    @pytest.mark.asyncio
    async def test_not_found_marks_clan_deleted(self, monkeypatch):
        cache = _make_cache()
        cache.clan_name_cache = {"#GONE": _passive(None)}
        cache.coc_clan_cache.get_clan = AsyncMock(side_effect=coc.NotFound(MagicMock(), "gone"))
        monkeypatch.setattr(QBhelperfunctions, "CACHE", cache)

        mark_deleted = AsyncMock()
        monkeypatch.setattr(QBhelperfunctions, "_mark_clan_deleted", mark_deleted)

        result = await refresh_stale_passive_clans()

        assert result == 1  # still counted as "queried this cycle"
        mark_deleted.assert_awaited_once_with("#GONE")

    @pytest.mark.asyncio
    async def test_generic_fetch_error_does_not_crash_batch(self, monkeypatch):
        cache = _make_cache()
        cache.clan_name_cache = {"#A": _passive(None), "#B": _passive(None)}

        async def _flaky(tag: str) -> None:
            if tag == "#A":
                raise RuntimeError("transient network error")
            cache.clan_name_cache[tag]["last_checked_via_api"] = datetime.now(timezone.utc).isoformat()

        cache.coc_clan_cache.get_clan = AsyncMock(side_effect=_flaky)
        monkeypatch.setattr(QBhelperfunctions, "CACHE", cache)

        result = await refresh_stale_passive_clans()

        assert result == 2  # both attempted despite #A's failure
