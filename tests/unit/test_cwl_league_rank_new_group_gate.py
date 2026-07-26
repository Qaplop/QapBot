"""Regression tests for _process_league_group_response's league_rank gating.

Bug (2026-07-26, two confirmed production incidents): league_rank for a past
CWL season could get locked in as some group member's *current*
(post-promotion/demotion) league instead of the league the group actually
competed in that season.

Root cause: the function that populates league_rank ran on *every* fresh
get_league_group() response, with no notion of "first time seeing this group"
versus "re-processing a group already recorded". Both the raw_data lookup and
the clan_name_cache fallback pick a single "representative" clan's *current*
league for the whole group — safe only at the moment the group is discovered,
since every member shares one league purely by construction (grouping happens
at that shared league). Once the real season ends, promotions/demotions apply
almost immediately and members diverge to *different* current leagues, so
re-deriving from "any member's current league" on a later re-processing call
is a coin flip, not a fact. Confirmed in production: a group's league_rank got
overwritten with a demoted member's *post*-season league while the group had
actually played that season in a different, higher league, per the in-game
war log.

An earlier fix gated this on the league group's live API `.state` field
(active vs. ended). That worked but treated a symptom — it still re-ran the
same "trust any member" logic on every subsequent call, just conditionally.
The actual fix: only ever populate league_rank when db.cwl_group_exists()
reports this (group_id, season) had no row before this call. That is the one
moment every member is *guaranteed* to share one league; an already-known
group's league_rank is never touched again by this function afterwards,
regardless of API state. QBhelperfunctions.update_cwl_group_stats's self-heal
remains the only other path that can ever touch it later, and only via a
verified safe-rank cross-check, never a blind "any member" guess.
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

    def __init__(self, clan_tags: list[str]) -> None:
        self.clans = [_FakeClan(t) for t in clan_tags]
        self.state = "inWar"  # no longer read by the production code
        self.season = "2026-07"
        self.rounds: list[list[str]] = []
        self._raw_data = None  # forces the clan_name_cache fallback path


def _make_cm(clan_name_cache: dict, *, group_exists: bool) -> CacheManager:
    cm = CacheManager()
    cm.clan_name_cache = clan_name_cache
    cm.db_manager = AsyncMock()
    cm.db_manager.cwl_group_exists = AsyncMock(return_value=group_exists)
    cm.db_manager.upsert_cwl_league_data = AsyncMock(return_value=0)
    cm.db_manager.update_cwl_league_rank = AsyncMock()
    cm.coc_clan_cache = AsyncMock()
    return cm


def _fresh_cache_entry(war_league: str) -> dict:
    return {
        "war_league": war_league,
        "last_checked_via_api": datetime.now(timezone.utc).isoformat(),
    }


def _stale_cache_entry(war_league: str) -> dict:
    from datetime import timedelta
    return {
        "war_league": war_league,
        "last_checked_via_api": (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
    }


CLAN_TAGS = ["#AAA", "#BBB"]


class TestNewGroupGating:
    @pytest.mark.asyncio
    async def test_new_group_uses_fresh_cache_entry(self):
        cm = _make_cm({t: _fresh_cache_entry("Champion League III") for t in CLAN_TAGS}, group_exists=False)
        lg = _FakeLeagueGroup(CLAN_TAGS)

        await cm._process_league_group_response(lg, "2026-07")

        cm.db_manager.update_cwl_league_rank.assert_awaited_once()
        args = cm.db_manager.update_cwl_league_rank.await_args.args
        assert args[2] == "Champion League III"
        cm.coc_clan_cache.get_clan.assert_not_awaited()  # cache was fresh — no API call needed

    @pytest.mark.asyncio
    async def test_new_group_calls_api_once_when_cache_stale(self):
        cm = _make_cm({t: _stale_cache_entry("Old League") for t in CLAN_TAGS}, group_exists=False)

        async def _fake_get_clan(tag: str):
            cm.clan_name_cache[tag] = _fresh_cache_entry("Titan League I")

        cm.coc_clan_cache.get_clan = AsyncMock(side_effect=_fake_get_clan)
        lg = _FakeLeagueGroup(CLAN_TAGS)

        await cm._process_league_group_response(lg, "2026-07")

        cm.coc_clan_cache.get_clan.assert_awaited_once()
        cm.db_manager.update_cwl_league_rank.assert_awaited_once()
        args = cm.db_manager.update_cwl_league_rank.await_args.args
        assert args[2] == "Titan League I"

    @pytest.mark.asyncio
    async def test_new_group_calls_api_once_when_cache_absent(self):
        cm = _make_cm({}, group_exists=False)

        async def _fake_get_clan(tag: str):
            cm.clan_name_cache[tag] = _fresh_cache_entry("Gold League II")

        cm.coc_clan_cache.get_clan = AsyncMock(side_effect=_fake_get_clan)
        lg = _FakeLeagueGroup(CLAN_TAGS)

        await cm._process_league_group_response(lg, "2026-07")

        cm.coc_clan_cache.get_clan.assert_awaited_once()
        args = cm.db_manager.update_cwl_league_rank.await_args.args
        assert args[2] == "Gold League II"

    @pytest.mark.asyncio
    async def test_new_group_uses_raw_data_if_present(self):
        cm = _make_cm({}, group_exists=False)
        lg = _FakeLeagueGroup(CLAN_TAGS)
        lg._raw_data = {
            "clans": [{"tag": t, "warLeague": {"name": "Champion League I"}} for t in CLAN_TAGS]
        }

        await cm._process_league_group_response(lg, "2026-07")

        cm.db_manager.update_cwl_league_rank.assert_awaited_once()
        args = cm.db_manager.update_cwl_league_rank.await_args.args
        assert args[2] == "Champion League I"
        cm.coc_clan_cache.get_clan.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_existing_group_never_repopulates_even_with_fresh_cache(self):
        """The core regression test: an already-known group's league_rank must
        never be touched again by this function, no matter how fresh or
        confident the cache data looks — this is exactly the write path that
        corrupted production data after a season ended."""
        cm = _make_cm({t: _fresh_cache_entry("Champion League II") for t in CLAN_TAGS}, group_exists=True)
        lg = _FakeLeagueGroup(CLAN_TAGS)

        await cm._process_league_group_response(lg, "2026-07")

        cm.db_manager.update_cwl_league_rank.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_existing_group_skips_api_call_too(self):
        """No wasted API budget on a group we already know about."""
        cm = _make_cm({}, group_exists=True)
        lg = _FakeLeagueGroup(CLAN_TAGS)

        await cm._process_league_group_response(lg, "2026-07")

        cm.coc_clan_cache.get_clan.assert_not_awaited()
        cm.db_manager.update_cwl_league_rank.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_existing_group_ignores_raw_data_too(self):
        cm = _make_cm({}, group_exists=True)
        lg = _FakeLeagueGroup(CLAN_TAGS)
        lg._raw_data = {
            "clans": [{"tag": t, "warLeague": {"name": "Champion League I"}} for t in CLAN_TAGS]
        }

        await cm._process_league_group_response(lg, "2026-07")

        cm.db_manager.update_cwl_league_rank.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_existing_group_with_divergent_members_still_not_touched(self):
        """Reproduces the exact production incident shape (members diverged to
        different current leagues after the season ended) — for an EXISTING
        group, regardless of which "representative" member's data looks most
        confident, nothing must be written."""
        tags = ["#PROMOTED", "#UNCHANGED", "#DEMOTED"]
        cache = {
            "#PROMOTED": _fresh_cache_entry("Master League II"),
            "#UNCHANGED": _fresh_cache_entry("Master League III"),
            "#DEMOTED": _fresh_cache_entry("Crystal League I"),
        }
        cm = _make_cm(cache, group_exists=True)
        lg = _FakeLeagueGroup(tags)

        await cm._process_league_group_response(lg, "2026-07")

        cm.db_manager.update_cwl_league_rank.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cwl_group_exists_checked_before_upsert(self):
        """cwl_group_exists must be called (and therefore reflect pre-upsert
        state) before upsert_cwl_league_data — checking after would always
        see the row as existing, since INSERT OR IGNORE has already run."""
        cm = _make_cm({t: _fresh_cache_entry("Champion League III") for t in CLAN_TAGS}, group_exists=False)
        call_order: list[str] = []
        cm.db_manager.cwl_group_exists.side_effect = lambda *a, **k: call_order.append("exists") or False
        cm.db_manager.upsert_cwl_league_data.side_effect = lambda **k: call_order.append("upsert") or 0
        lg = _FakeLeagueGroup(CLAN_TAGS)

        await cm._process_league_group_response(lg, "2026-07")

        assert call_order == ["exists", "upsert"]
