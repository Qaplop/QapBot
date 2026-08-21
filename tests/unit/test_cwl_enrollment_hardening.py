"""Tracker #0011 hardening (2026-08-21): concurrency + DM-retraction gaps around CWL enrollment.

Neither gap was proven to be the exact trigger of #0011 (the PROD log had no success-path
logging for either action, which is itself fixed here), but both are real, confirmed holes of
the same bug class as the already-fixed coc_cache.py UNASSIGNED race (Pitfall 35).
"""
# pyright: reportPrivateUsage=false
from __future__ import annotations

import asyncio
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# start_cwl_enrollment — per-(guild, season) serialization
# ---------------------------------------------------------------------------

class TestStartEnrollmentConcurrency:
    """The only server-side guard was `event["status"] != "draft"`, and that status isn't
    written until AFTER the whole DM batch — minutes, for a ~120-recipient blast under load. A
    second trigger in that window sailed straight past it and started a duplicate run."""

    @staticmethod
    def _slow_impl(events: List[str], delay: float = 0.05):
        async def _impl(guild_id: int, season: str) -> Dict[str, Any]:
            events.append(f"start:{guild_id}:{season}")
            await asyncio.sleep(delay)
            events.append(f"end:{guild_id}:{season}")
            return {"ok": True}
        return _impl

    @pytest.mark.asyncio
    async def test_same_guild_and_season_is_serialized(self, monkeypatch):
        import qapbot.QBdiscocmdshelper_cwl as cwl

        events: List[str] = []
        monkeypatch.setattr(cwl, "_start_cwl_enrollment_locked", self._slow_impl(events))
        monkeypatch.setattr(cwl, "_enrollment_locks", {})

        await asyncio.gather(
            cwl.start_cwl_enrollment(1, "2026-09"),
            cwl.start_cwl_enrollment(1, "2026-09"),
        )

        # The second run must not begin before the first has finished.
        assert events == [
            "start:1:2026-09", "end:1:2026-09",
            "start:1:2026-09", "end:1:2026-09",
        ]

    @pytest.mark.asyncio
    async def test_different_guilds_are_not_serialized(self, monkeypatch):
        import qapbot.QBdiscocmdshelper_cwl as cwl

        events: List[str] = []
        monkeypatch.setattr(cwl, "_start_cwl_enrollment_locked", self._slow_impl(events))
        monkeypatch.setattr(cwl, "_enrollment_locks", {})

        await asyncio.gather(
            cwl.start_cwl_enrollment(1, "2026-09"),
            cwl.start_cwl_enrollment(2, "2026-09"),
        )

        assert set(events[:2]) == {"start:1:2026-09", "start:2:2026-09"}

    @pytest.mark.asyncio
    async def test_different_seasons_same_guild_are_not_serialized(self, monkeypatch):
        import qapbot.QBdiscocmdshelper_cwl as cwl

        events: List[str] = []
        monkeypatch.setattr(cwl, "_start_cwl_enrollment_locked", self._slow_impl(events))
        monkeypatch.setattr(cwl, "_enrollment_locks", {})

        await asyncio.gather(
            cwl.start_cwl_enrollment(1, "2026-09"),
            cwl.start_cwl_enrollment(1, "2026-10"),
        )

        assert set(events[:2]) == {"start:1:2026-09", "start:1:2026-10"}


# ---------------------------------------------------------------------------
# _retract_enrollment_dms_for_tags — scoped DM retraction
# ---------------------------------------------------------------------------

class TestRetractEnrollmentDms:
    ALL_REFS = [
        {"player_tag": "#PURGED", "discord_id": "1", "dm_sent_via_message_id": "10",
         "dm_sent_via_channel_id": "20"},
        {"player_tag": "#KEPT", "discord_id": "2", "dm_sent_via_message_id": "11",
         "dm_sent_via_channel_id": "21"},
    ]

    def _wire(self, monkeypatch, cwl):
        db = MagicMock()
        db.get_cwl_player_season_status_dm_refs_for_event_sync = MagicMock(
            return_value=list(self.ALL_REFS)
        )
        cache = MagicMock()
        cache.db_manager = db
        monkeypatch.setattr(cwl, "CACHE", cache)

        import QBcore
        monkeypatch.setattr(QBcore, "bot", MagicMock())

        retract = AsyncMock(return_value={"deleted": 1, "failed": 0})
        monkeypatch.setattr(cwl, "cleanup_stale_cwl_enrollment_dms", retract)
        return retract

    @pytest.mark.asyncio
    async def test_retracts_only_the_purged_players_dms(self, monkeypatch):
        """A preserved player keeps their pool membership, so their DM is still live and must
        NOT be retracted — only the purged ones."""
        import qapbot.QBdiscocmdshelper_cwl as cwl
        retract = self._wire(monkeypatch, cwl)

        await cwl._retract_enrollment_dms_for_tags(7, ["#PURGED"], context="test")

        retract.assert_awaited_once()
        _, kwargs = retract.call_args
        sent_refs = kwargs.get("dm_refs") or retract.call_args[0][1]
        assert [r["player_tag"] for r in sent_refs] == ["#PURGED"]

    @pytest.mark.asyncio
    async def test_no_matching_refs_is_a_noop(self, monkeypatch):
        import qapbot.QBdiscocmdshelper_cwl as cwl
        retract = self._wire(monkeypatch, cwl)

        await cwl._retract_enrollment_dms_for_tags(7, ["#NOBODY"], context="test")

        retract.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_bot_is_a_noop(self, monkeypatch):
        """Best-effort: no client available (e.g. during shutdown) must not raise."""
        import qapbot.QBdiscocmdshelper_cwl as cwl
        retract = self._wire(monkeypatch, cwl)

        import QBcore
        monkeypatch.setattr(QBcore, "bot", None)

        await cwl._retract_enrollment_dms_for_tags(7, ["#PURGED"], context="test")

        retract.assert_not_awaited()
