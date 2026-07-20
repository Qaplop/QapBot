"""Unit tests for CWL helper logic in QBhelperfunctions.py.

Covers two areas the wider suite previously left untested:
  1. ``_find_active_cwl_war_for_clan`` — the cwl_ended short-circuit
     (must skip the league-group API fetch when the latest season is ended).
  2. ``update_cwl_group_stats`` — the ``all_ended`` season-completion computation
     that drives the ``cwl_ended`` flag written via ``update_cwl_group_stats_batch``.
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnusedImport=false
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# _find_active_cwl_war_for_clan — season-over guards
# ---------------------------------------------------------------------------

class TestFindActiveCwlWarGuards:
    def _fn(self):
        from QBhelperfunctions import _find_active_cwl_war_for_clan
        return _find_active_cwl_war_for_clan

    def _make_cache(self):
        cache = MagicMock()
        cache.clan_active_cwl_war = {}  # no shortcut → reach the ended guard
        cache.get_league_group = AsyncMock(return_value=None)
        db = MagicMock()
        db.is_latest_cwl_season_ended_sync = MagicMock(return_value=False)
        cache.db_manager = db
        return cache, db

    @pytest.mark.asyncio
    async def test_season_ended_skips_league_group_fetch(self, monkeypatch):
        """is_latest_cwl_season_ended_sync=True → return None without any API call."""
        cache, db = self._make_cache()
        db.is_latest_cwl_season_ended_sync.return_value = True
        monkeypatch.setattr("QBhelperfunctions.CACHE", cache)

        result = await self._fn()("#CLAN")
        assert result is None
        db.is_latest_cwl_season_ended_sync.assert_called_once()
        cache.get_league_group.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_season_not_ended_proceeds_to_fetch(self, monkeypatch):
        """is_latest_cwl_season_ended_sync=False → the guard allows the fetch."""
        cache, db = self._make_cache()
        db.is_latest_cwl_season_ended_sync.return_value = False
        monkeypatch.setattr("QBhelperfunctions.CACHE", cache)

        result = await self._fn()("#CLAN")
        assert result is None  # get_league_group returned None
        db.is_latest_cwl_season_ended_sync.assert_called_once()
        cache.get_league_group.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_db_proceeds_to_fetch(self, monkeypatch):
        """No db_manager → skip the ended check and proceed to fetch."""
        cache, _ = self._make_cache()
        cache.db_manager = None
        monkeypatch.setattr("QBhelperfunctions.CACHE", cache)

        result = await self._fn()("#CLAN")
        assert result is None  # get_league_group returned None
        cache.get_league_group.assert_awaited_once()


# ---------------------------------------------------------------------------
# update_cwl_group_stats — all_ended (season completion) computation
# ---------------------------------------------------------------------------

class TestUpdateCwlGroupStatsAllEnded:
    def _fn(self):
        from QBhelperfunctions import update_cwl_group_stats
        return update_cwl_group_stats

    def _clear_ttl_cache(self):
        import QBhelperfunctions
        QBhelperfunctions._cwl_group_stats_cache.clear()

    def _make_cache(self, clan_tags: List[str], ended_wars: Dict[str, int]):
        cache = MagicMock()
        # No in_war temp data → step 5 contributes nothing.
        cache.get_current_war_data = MagicMock(return_value=None)
        cache.clan_name_cache = {ct: {"name": ct} for ct in clan_tags}

        db = MagicMock()
        db.get_cwl_group_info = AsyncMock(return_value={
            "league_group_id": "grp1",
            "clan_tags": clan_tags,
            "league_rank": "Master League III",
            "cwl_ended": False,
            "rows": [],
        })
        # db_stars: every clan has some completed stars; ended_wars drives all_ended.
        db_stars: Dict[str, Tuple[int, float]] = {ct: (10, 100.0) for ct in clan_tags}
        db.get_cwl_group_war_stats = AsyncMock(return_value=(db_stars, ended_wars))
        db.update_cwl_group_stats_batch = AsyncMock(return_value=len(clan_tags))
        cache.db_manager = db
        return cache, db

    @pytest.mark.asyncio
    async def test_all_rounds_complete_sets_ended_true(self, monkeypatch):
        self._clear_ttl_cache()
        clan_tags = ["#A", "#B", "#C", "#D"]  # 4 clans → expected_rounds = 3
        ended = {ct: 3 for ct in clan_tags}
        cache, db = self._make_cache(clan_tags, ended)
        monkeypatch.setattr("QBhelperfunctions.CACHE", cache)

        await self._fn()("#A", "2026-06")

        # set_cwl_ended is the 4th positional arg.
        call = db.update_cwl_group_stats_batch.call_args
        assert call.args[3] is True

    @pytest.mark.asyncio
    async def test_one_clan_incomplete_sets_ended_false(self, monkeypatch):
        self._clear_ttl_cache()
        clan_tags = ["#A", "#B", "#C", "#D"]  # expected_rounds = 3
        ended = {"#A": 3, "#B": 3, "#C": 2, "#D": 3}  # #C short one round
        cache, db = self._make_cache(clan_tags, ended)
        monkeypatch.setattr("QBhelperfunctions.CACHE", cache)

        await self._fn()("#A", "2026-06")

        call = db.update_cwl_group_stats_batch.call_args
        assert call.args[3] is False

    @pytest.mark.asyncio
    async def test_empty_group_not_ended(self, monkeypatch):
        self._clear_ttl_cache()
        clan_tags: List[str] = []
        cache, db = self._make_cache(clan_tags, {})
        monkeypatch.setattr("QBhelperfunctions.CACHE", cache)

        result = await self._fn()("#A", "2026-06")
        # No clans → batch still invoked with all_ended False (len==0 guard).
        call = db.update_cwl_group_stats_batch.call_args
        assert call.args[3] is False
        assert result == []


# ---------------------------------------------------------------------------
# fetch_clan_war_data notInWar CWL fallback
# ---------------------------------------------------------------------------

class TestFetchClanWarDataNotInWarCwlFallback:
    """
    When get_current_war() returns None (notInWar) for a public-warlog clan,
    fetch_clan_war_data must attempt the CWL fallback iff
    is_latest_cwl_season_ended_sync() returns False (season active or unknown).
    The regular-war exclusivity guard in _find_active_cwl_war_for_clan must also
    suppress the fallback when temp_war_metadata shows a regular war in progress.
    """

    def _make_cache(self, cwl_ended: bool = False):
        cache = MagicMock()
        cache.clan_active_cwl_war = {}
        cache.clan_name_cache = {}
        cache.server_config = {}
        cache.clan_families = {}
        cache.temp_war_metadata = {}
        db = MagicMock()
        db.is_latest_cwl_season_ended_sync = MagicMock(return_value=cwl_ended)
        cache.db_manager = db
        cache.get_current_war_from_api = AsyncMock(return_value=None)
        cache.coc_clan_cache = MagicMock()
        cache.coc_clan_cache.get_clan = AsyncMock(return_value=MagicMock())
        cache.get_league_group = AsyncMock(return_value=None)
        return cache, db

    @pytest.mark.asyncio
    async def test_cwl_season_ended_returns_none_without_fallback(self, monkeypatch):
        """is_latest_cwl_season_ended_sync=True → no fallback API call, returns None."""
        cache, db = self._make_cache(cwl_ended=True)
        monkeypatch.setattr("QBhelperfunctions.CACHE", cache)

        from QBhelperfunctions import fetch_clan_war_data
        result = await fetch_clan_war_data("#CLAN1")

        assert result is None
        db.is_latest_cwl_season_ended_sync.assert_called_once_with("#CLAN1")
        cache.get_league_group.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cwl_active_triggers_fallback(self, monkeypatch):
        """is_latest_cwl_season_ended_sync=False → _find_active_cwl_war_for_clan is called."""
        cache, db = self._make_cache(cwl_ended=False)
        monkeypatch.setattr("QBhelperfunctions.CACHE", cache)

        # _find_active_cwl_war_for_clan calls get_league_group then returns None
        # (no matching war found) — just confirm the fallback was attempted.
        from QBhelperfunctions import fetch_clan_war_data
        result = await fetch_clan_war_data("#CLAN2")

        assert result is None
        db.is_latest_cwl_season_ended_sync.assert_called()
        cache.get_league_group.assert_awaited()  # fallback reached the API

    @pytest.mark.asyncio
    async def test_no_db_triggers_fallback(self, monkeypatch):
        """No db_manager → conservative (treat as season active) → fallback attempted."""
        cache, _ = self._make_cache(cwl_ended=False)
        cache.db_manager = None
        monkeypatch.setattr("QBhelperfunctions.CACHE", cache)

        from QBhelperfunctions import fetch_clan_war_data
        result = await fetch_clan_war_data("#CLAN3")

        assert result is None
        cache.get_league_group.assert_awaited()

    @pytest.mark.asyncio
    async def test_regular_war_in_metadata_suppresses_fallback(self, monkeypatch):
        """Regular war in temp_war_metadata → CWL check skipped (mutual exclusivity)."""
        cache, db = self._make_cache(cwl_ended=False)
        # Simulate an active regular (non-CWL) war in temp_war_metadata
        cache.temp_war_metadata = {"#CLAN4": {"state": "inWar", "is_cwl": False}}
        monkeypatch.setattr("QBhelperfunctions.CACHE", cache)

        from QBhelperfunctions import fetch_clan_war_data
        result = await fetch_clan_war_data("#CLAN4")

        assert result is None
        # is_latest_cwl_season_ended_sync fires (in fetch_clan_war_data) but
        # get_league_group must NOT be called because the regular-war guard fires first.
        cache.get_league_group.assert_not_awaited()
