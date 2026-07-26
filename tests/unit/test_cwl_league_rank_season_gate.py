"""Regression tests for _process_league_group_response's season-end gating.

Bug (2026-07-26, two confirmed production incidents): league_rank for a past
CWL season could get locked in as some group member's *current*
(post-promotion/demotion) league instead of the league the group actually
competed in that season.

Two contributing causes, both closed by the same fix:
1. The clan_name_cache fallback was gated on our own `cwl_ended` DB column,
   which can lag well behind the real-world end of the season (it only flips
   once every clan's expected war count is observed in war_summary).
2. Both the raw_data lookup AND the clan_name_cache fallback pick a single
   "representative" clan's *current* league for the whole group. That's only
   safe while the season is still active — every clan in a group shares the
   same league by construction, since group membership is determined by the
   clans' league at formation time. Once the real season ends, promotions/
   demotions apply almost immediately, and group members can diverge to
   *different* current leagues (some promoted, some demoted, some unchanged),
   so picking any one of them is a coin flip. Confirmed in production: a
   group's league_rank got written as "Crystal League I" — the CURRENT league
   of two demoted bottom-of-group members — when the group (and every other
   member) had actually played that season in "Master League III", per the
   in-game war log.

Fix: gate both sources on the league group's own live API `.state` field (the
authoritative, real-time signal for season completion) instead of the DB's
`cwl_ended` column, and instead of trusting any single clan's current data.
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false
# pyright: reportOptionalMemberAccess=false, reportAttributeAccessIssue=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from qapbot.cache_manager import CacheManager


class _FakeClan:
    def __init__(self, tag: str) -> None:
        self.tag = tag


class _FakeLeagueGroup:
    """Minimal stand-in for coc.ClanWarLeagueGroup."""

    def __init__(self, clan_tags: list[str], state: str) -> None:
        self.clans = [_FakeClan(t) for t in clan_tags]
        self.state = state
        self.season = "2026-07"
        self.rounds: list[list[str]] = []
        self._raw_data = None  # forces the clan_name_cache fallback path


def _make_cm(clan_name_cache: dict) -> CacheManager:
    cm = CacheManager()
    cm.clan_name_cache = clan_name_cache
    cm.db_manager = AsyncMock()
    cm.db_manager.upsert_cwl_league_data = AsyncMock(return_value=0)
    cm.db_manager.update_cwl_league_rank = AsyncMock()
    return cm


def _fresh_cache_entry(war_league: str) -> dict:
    return {
        "war_league": war_league,
        "last_checked_via_api": datetime.now(timezone.utc).isoformat(),
    }


CLAN_TAGS = ["#AAA", "#BBB"]


class TestSeasonEndGating:
    @pytest.mark.asyncio
    async def test_active_season_uses_clan_name_cache_fallback(self):
        """While the group is still in preparation/inWar, the clan's current
        league IS this season's league — the fallback must run normally."""
        cm = _make_cm({t: _fresh_cache_entry("Champion League III") for t in CLAN_TAGS})
        lg = _FakeLeagueGroup(CLAN_TAGS, state="inWar")

        await cm._process_league_group_response(lg, "2026-07")

        cm.db_manager.update_cwl_league_rank.assert_awaited_once()
        args = cm.db_manager.update_cwl_league_rank.await_args.args
        assert args[2] == "Champion League III"

    @pytest.mark.asyncio
    async def test_ended_season_skips_fallback_even_if_league_rank_unknown(self):
        """Once the league group reports warEnded, clan_name_cache may already
        reflect a post-promotion league — must NOT be used to populate
        league_rank for the just-finished season."""
        cm = _make_cm({t: _fresh_cache_entry("Champion League II") for t in CLAN_TAGS})
        lg = _FakeLeagueGroup(CLAN_TAGS, state="warEnded")

        await cm._process_league_group_response(lg, "2026-07")

        cm.db_manager.update_cwl_league_rank.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_preparation_state_also_treated_as_active(self):
        cm = _make_cm({t: _fresh_cache_entry("Master League I") for t in CLAN_TAGS})
        lg = _FakeLeagueGroup(CLAN_TAGS, state="preparation")

        await cm._process_league_group_response(lg, "2026-07")

        cm.db_manager.update_cwl_league_rank.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ended_season_skips_fallback_when_group_members_diverge(self):
        """Reproduces the exact production incident: after a warEnded group's
        promotions/demotions apply, group members no longer share one current
        league (some promoted, some demoted, some unchanged). Regardless of
        which clan the fallback would have picked as "representative", no
        write must happen once the season has ended."""
        tags = ["#PROMOTED", "#UNCHANGED", "#DEMOTED"]
        cache = {
            "#PROMOTED": _fresh_cache_entry("Master League II"),
            "#UNCHANGED": _fresh_cache_entry("Master League III"),
            "#DEMOTED": _fresh_cache_entry("Crystal League I"),
        }
        cm = _make_cm(cache)
        lg = _FakeLeagueGroup(tags, state="warEnded")

        await cm._process_league_group_response(lg, "2026-07")

        cm.db_manager.update_cwl_league_rank.assert_not_awaited()


class TestRawDataSeasonGate:
    """The raw_data (get_league_group raw API response) lookup is tried before
    the clan_name_cache fallback and previously ran unconditionally — it must
    be gated on season state exactly like the cache fallback."""

    @pytest.mark.asyncio
    async def test_active_season_uses_raw_data(self):
        cm = _make_cm({})
        lg = _FakeLeagueGroup(CLAN_TAGS, state="inWar")
        lg._raw_data = {
            "clans": [{"tag": t, "warLeague": {"name": "Champion League I"}} for t in CLAN_TAGS]
        }

        await cm._process_league_group_response(lg, "2026-07")

        cm.db_manager.update_cwl_league_rank.assert_awaited_once()
        args = cm.db_manager.update_cwl_league_rank.await_args.args
        assert args[2] == "Champion League I"

    @pytest.mark.asyncio
    async def test_ended_season_skips_raw_data_too(self):
        cm = _make_cm({})
        lg = _FakeLeagueGroup(CLAN_TAGS, state="warEnded")
        lg._raw_data = {
            "clans": [{"tag": t, "warLeague": {"name": "Champion League I"}} for t in CLAN_TAGS]
        }

        await cm._process_league_group_response(lg, "2026-07")

        cm.db_manager.update_cwl_league_rank.assert_not_awaited()
