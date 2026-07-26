"""Tests for the CWL league_rank self-heal (QBhelperfunctions._cwl_self_heal_league_rank
and its wiring into update_cwl_group_stats).

Context: cwl_league_groups.league_rank could get written wrong and then frozen
forever once cwl_ended=1 (see changelog.txt 2026-07-26). The write-time bug is
fixed, but a group's *existing* frozen value was never re-examined — this
self-heal re-checks it every time an ended group's standings are served, using
the same "safe middle rank" reasoning validated in
qapbot/scripts/audit_cwl_league_rank.py: a clan ranked outside the top 3 /
bottom 2 of its group is guaranteed to have stayed in the same league the
following season, regardless of league or rule-version, so its current live
league (if freshly checked) is trustworthy evidence for what the group's
league_rank should have been.
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownParameterType=false
# pyright: reportMissingParameterType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from QBhelperfunctions import _cwl_self_heal_league_rank


def _rows(n: int = 8) -> List[Dict[str, Any]]:
    return [{"clan_tag": f"#C{i}", "group_rank": i} for i in range(1, n + 1)]


def _cache_entry(war_league: str, last_checked: str) -> Dict[str, Any]:
    return {"war_league": war_league, "last_checked_via_api": last_checked}


# Season "2026-07": window_start=2026-07-12, window_end=2026-08-12 (11-day margin).
IN_WINDOW = datetime(2026, 7, 20, tzinfo=timezone.utc)
FRESH_TS = "2026-07-15T00:00:00+00:00"
STALE_TS = "2026-07-01T00:00:00+00:00"  # before window_start


class TestCwlSelfHealLeagueRank:
    def test_corrects_when_safe_rank_member_disagrees(self, monkeypatch):
        cache = MagicMock()
        # Ranks 4-6 are the safe band for an 8-clan group (MAX_PROMOTED=3, MAX_DEMOTED=2).
        cache.clan_name_cache = {
            "#C4": _cache_entry("Master League III", FRESH_TS),
            "#C5": _cache_entry("Master League III", FRESH_TS),
        }
        monkeypatch.setattr("QBhelperfunctions.CACHE", cache)

        result = _cwl_self_heal_league_rank(_rows(), "2026-07", "Crystal League I", now=IN_WINDOW)
        assert result == "Master League III"

    def test_no_correction_when_already_correct(self, monkeypatch):
        cache = MagicMock()
        cache.clan_name_cache = {"#C4": _cache_entry("Master League III", FRESH_TS)}
        monkeypatch.setattr("QBhelperfunctions.CACHE", cache)

        result = _cwl_self_heal_league_rank(_rows(), "2026-07", "Master League III", now=IN_WINDOW)
        assert result is None

    def test_too_early_in_season_returns_none(self, monkeypatch):
        cache = MagicMock()
        cache.clan_name_cache = {"#C4": _cache_entry("Master League III", FRESH_TS)}
        monkeypatch.setattr("QBhelperfunctions.CACHE", cache)

        too_early = datetime(2026, 7, 5, tzinfo=timezone.utc)  # before window_start
        result = _cwl_self_heal_league_rank(_rows(), "2026-07", "Crystal League I", now=too_early)
        assert result is None

    def test_too_late_after_next_season_returns_none(self, monkeypatch):
        cache = MagicMock()
        cache.clan_name_cache = {"#C4": _cache_entry("Master League III", FRESH_TS)}
        monkeypatch.setattr("QBhelperfunctions.CACHE", cache)

        too_late = datetime(2026, 8, 15, tzinfo=timezone.utc)  # after window_end
        result = _cwl_self_heal_league_rank(_rows(), "2026-07", "Crystal League I", now=too_late)
        assert result is None

    def test_stale_cache_entry_is_not_trusted(self, monkeypatch):
        """now is inside the window, but the cache entry itself predates window_start."""
        cache = MagicMock()
        cache.clan_name_cache = {"#C4": _cache_entry("Master League III", STALE_TS)}
        monkeypatch.setattr("QBhelperfunctions.CACHE", cache)

        result = _cwl_self_heal_league_rank(_rows(), "2026-07", "Crystal League I", now=IN_WINDOW)
        assert result is None

    def test_disagreement_among_safe_rank_members_returns_none(self, monkeypatch):
        cache = MagicMock()
        cache.clan_name_cache = {
            "#C4": _cache_entry("Master League III", FRESH_TS),
            "#C5": _cache_entry("Crystal League I", FRESH_TS),
        }
        monkeypatch.setattr("QBhelperfunctions.CACHE", cache)

        result = _cwl_self_heal_league_rank(_rows(), "2026-07", "Master League II", now=IN_WINDOW)
        assert result is None

    def test_group_too_small_for_safe_band_returns_none(self, monkeypatch):
        cache = MagicMock()
        cache.clan_name_cache = {"#C1": _cache_entry("Master League III", FRESH_TS)}
        monkeypatch.setattr("QBhelperfunctions.CACHE", cache)

        result = _cwl_self_heal_league_rank(_rows(n=5), "2026-07", "Crystal League I", now=IN_WINDOW)
        assert result is None

    def test_bonus_mid_month_season_returns_none(self, monkeypatch):
        cache = MagicMock()
        cache.clan_name_cache = {"#C4": _cache_entry("Master League III", FRESH_TS)}
        monkeypatch.setattr("QBhelperfunctions.CACHE", cache)

        result = _cwl_self_heal_league_rank(_rows(), "2026-07-16", "Crystal League I", now=IN_WINDOW)
        assert result is None

    def test_no_safe_rank_member_in_cache_returns_none(self, monkeypatch):
        cache = MagicMock()
        cache.clan_name_cache = {}
        monkeypatch.setattr("QBhelperfunctions.CACHE", cache)

        result = _cwl_self_heal_league_rank(_rows(), "2026-07", "Crystal League I", now=IN_WINDOW)
        assert result is None


# ---------------------------------------------------------------------------
# Wiring into update_cwl_group_stats's cwl_ended short-circuit
# ---------------------------------------------------------------------------

class TestUpdateCwlGroupStatsSelfHealWiring:
    def _clear_ttl_cache(self):
        import QBhelperfunctions
        QBhelperfunctions._cwl_group_stats_cache.clear()

    def _make_cache(self, rows: List[Dict[str, Any]], league_rank: str):
        cache = MagicMock()
        cache.get_current_war_data = MagicMock(return_value=None)
        cache.clan_name_cache = {}

        db = MagicMock()
        db.get_cwl_group_info = AsyncMock(return_value={
            "league_group_id": "grp1",
            "clan_tags": [r["clan_tag"] for r in rows],
            "league_rank": league_rank,
            "cwl_ended": True,
            "rows": rows,
        })
        db.update_cwl_league_rank = AsyncMock()
        cache.db_manager = db
        return cache, db

    @pytest.mark.asyncio
    async def test_self_heal_correction_is_persisted_with_force(self, monkeypatch):
        self._clear_ttl_cache()
        rows = [
            {"clan_tag": "#A", "group_rank": 1, "total_stars": 500, "total_destruction": 100.0,
             "cwl_ended": True, "league_rank": "Crystal League I"},
        ]
        cache, db = self._make_cache(rows, "Crystal League I")
        monkeypatch.setattr("QBhelperfunctions.CACHE", cache)
        monkeypatch.setattr("QBhelperfunctions._cwl_self_heal_league_rank",
                             lambda rows, season, recorded: "Master League III")

        import QBhelperfunctions
        result = await QBhelperfunctions.update_cwl_group_stats("#A", "2026-07")

        db.update_cwl_league_rank.assert_awaited_once_with(
            "2026-07", "grp1", "Master League III", force=True
        )
        assert result[0]["league_rank"] == "Master League III"

    @pytest.mark.asyncio
    async def test_no_correction_leaves_league_rank_untouched(self, monkeypatch):
        self._clear_ttl_cache()
        rows = [
            {"clan_tag": "#A", "group_rank": 1, "total_stars": 500, "total_destruction": 100.0,
             "cwl_ended": True, "league_rank": "Master League III"},
        ]
        cache, db = self._make_cache(rows, "Master League III")
        monkeypatch.setattr("QBhelperfunctions.CACHE", cache)
        monkeypatch.setattr("QBhelperfunctions._cwl_self_heal_league_rank",
                             lambda rows, season, recorded: None)

        import QBhelperfunctions
        result = await QBhelperfunctions.update_cwl_group_stats("#A", "2026-07")

        db.update_cwl_league_rank.assert_not_awaited()
        assert result[0]["league_rank"] == "Master League III"

    @pytest.mark.asyncio
    async def test_self_heal_also_fires_on_first_ever_computation(self, monkeypatch):
        """Reproduces the gap this closes: a group whose league_rank never got
        populated during its active season (still empty) reaches all_ended=True
        for the very first time via the "compute fresh" branch, not the
        cwl_ended=1 short-circuit. Without this fix, league_rank would stay
        empty forever for any clan only ever reached via the auto-post
        background loop, since that loop skips update_cwl_group_stats entirely
        once cwl_ended is observed True — it would never get a second chance."""
        self._clear_ttl_cache()
        clan_tags = ["#A", "#B", "#C", "#D"]  # 4 clans -> expected_rounds = 3
        cache = MagicMock()
        cache.get_current_war_data = MagicMock(return_value=None)
        cache.clan_name_cache = {}

        db = MagicMock()
        db.get_cwl_group_info = AsyncMock(return_value={
            "league_group_id": "grp1",
            "clan_tags": clan_tags,
            "league_rank": "",  # never populated
            "cwl_ended": False,  # not yet marked ended going into this call
            "rows": [],
        })
        db_stars = {ct: (10, 100.0) for ct in clan_tags}
        ended = {ct: 3 for ct in clan_tags}  # all rounds complete -> all_ended=True
        db.get_cwl_group_war_stats = AsyncMock(return_value=(db_stars, ended))
        db.update_cwl_group_stats_batch = AsyncMock(return_value=len(clan_tags))
        db.update_cwl_league_rank = AsyncMock()
        cache.db_manager = db
        monkeypatch.setattr("QBhelperfunctions.CACHE", cache)
        monkeypatch.setattr("QBhelperfunctions._cwl_self_heal_league_rank",
                             lambda rows, season, recorded: "Master League III" if not recorded else None)

        import QBhelperfunctions
        result = await QBhelperfunctions.update_cwl_group_stats("#A", "2026-07")

        db.update_cwl_league_rank.assert_awaited_once_with(
            "2026-07", "grp1", "Master League III", force=True
        )
        assert all(r["league_rank"] == "Master League III" for r in result)

    @pytest.mark.asyncio
    async def test_no_self_heal_attempt_when_season_not_yet_fully_ended(self, monkeypatch):
        """all_ended=False (still mid-season) must not invoke the self-heal at all —
        group_rank isn't final yet, so "safe rank" reasoning doesn't apply."""
        self._clear_ttl_cache()
        clan_tags = ["#A", "#B", "#C", "#D"]
        cache = MagicMock()
        cache.get_current_war_data = MagicMock(return_value=None)
        cache.clan_name_cache = {}

        db = MagicMock()
        db.get_cwl_group_info = AsyncMock(return_value={
            "league_group_id": "grp1",
            "clan_tags": clan_tags,
            "league_rank": "",
            "cwl_ended": False,
            "rows": [],
        })
        db_stars = {ct: (10, 100.0) for ct in clan_tags}
        ended = {"#A": 3, "#B": 3, "#C": 2, "#D": 3}  # #C short one round -> all_ended=False
        db.get_cwl_group_war_stats = AsyncMock(return_value=(db_stars, ended))
        db.update_cwl_group_stats_batch = AsyncMock(return_value=len(clan_tags))
        db.update_cwl_league_rank = AsyncMock()
        cache.db_manager = db
        monkeypatch.setattr("QBhelperfunctions.CACHE", cache)
        self_heal_called = MagicMock()
        monkeypatch.setattr("QBhelperfunctions._cwl_self_heal_league_rank", self_heal_called)

        import QBhelperfunctions
        await QBhelperfunctions.update_cwl_group_stats("#A", "2026-07")

        self_heal_called.assert_not_called()
        db.update_cwl_league_rank.assert_not_awaited()
