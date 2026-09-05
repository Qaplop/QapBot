"""Regression tests for the orphaned-CWL battle-day gate in process_orphaned_cwl_wars.

CWL rounds overlap: round N+1's preparation file is written while round N is still
inside its 24h battle day, which demotes round N's file to "not the newest file for
this clan".  That demotion used to be the *only* condition for flagging a multi-file
clan's war as an orphan, so wars that were still being fought were re-fetched via
get_league_war() and rewritten by save_war_object() on every cycle — never finalizing,
because the API correctly kept reporting them as inWar.

Observed on PROD 2026-09-03: 1,719 of 2,063 non-newest entries (83%) were wars still in
progress, the orphan pass logged 0/2,156 successful for 24 hours straight, and update
cycles grew from ~40 s to 250-526 s.

These tests pin the gate: a non-newest CWL file is only handed to the orphan fetcher once
its war ended more than an hour ago.
"""
# pyright: reportPrivateUsage=false, reportMissingParameterType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _ts(dt: datetime) -> str:
    """Filename timestamp format: YYYYMMDDHHMM."""
    return dt.strftime("%Y%m%d%H%M")


def _stamp(dt: datetime) -> str:
    """coc.py Timestamp repr, as stored in the temp JSON."""
    return (
        f"<Timestamp time=datetime.datetime({dt.year}, {dt.month}, {dt.day}, "
        f"{dt.hour}, {dt.minute}, {dt.second}) seconds_until=0>"
    )


def _write_war(
    shard_dir: Any, clan: str, opp: str, war_tag: str, start: datetime, end: datetime, state: str
) -> str:
    """Write a 4-part CWL temp war file and return its path."""
    path = shard_dir / f"{clan}_{opp}_{_ts(start)}_{war_tag}_war_data.json"
    path.write_text(
        json.dumps(
            {
                "state": state,
                "type": "cwl",
                "is_cwl": True,
                "war_tag": f"#{war_tag}",
                "start_time": _stamp(start),
                "end_time": _stamp(end),
                "clan": {"tag": f"#{clan}", "members": []},
                "opponent": {"tag": f"#{opp}", "members": []},
            }
        ),
        encoding="utf-8",
    )
    return str(path)


async def _run_scan(tmp_path: Any, files: List[str], clan: str) -> List[str]:
    """Run process_orphaned_cwl_wars and return the war tags it actually fetched."""
    from QBhelperfunctions import process_orphaned_cwl_wars

    fetched: List[str] = []

    async def _fake_get_league_war(war_tag: str, **_kw: Any) -> Any:
        fetched.append(war_tag)
        # Report the war as still running so nothing is finalized; we only care
        # about *which* wars the scanner decided were worth an API call.
        war = MagicMock()
        war.state = "inWar"
        war.clan = MagicMock()
        war.opponent = MagicMock()
        return war

    prescan: Dict[str, List[str]] = {clan: files}

    # war_write_batch() is a context manager yielding an object with pending_file_moves;
    # process_orphaned_cwl_wars enters it unconditionally once any orphan is found.
    batch = MagicMock()
    batch.pending_file_moves = []
    db = MagicMock()
    db.war_write_batch.return_value.__enter__ = MagicMock(return_value=batch)
    db.war_write_batch.return_value.__exit__ = MagicMock(return_value=False)
    db.get_cwl_round_for_war_tag_sync = MagicMock(return_value=("round", 1))

    with patch("QBhelperfunctions.CACHE") as cache, patch(
        "QBhelperfunctions.CONFIG"
    ) as config, patch("QBhelperfunctions.manage_war_files") as mwf:
        config.data_dir = str(tmp_path / "data")
        cache.temp_war_metadata = {}
        cache.get_league_war = AsyncMock(side_effect=_fake_get_league_war)
        cache.get_league_group = AsyncMock(return_value=MagicMock())
        cache.save_war_object = MagicMock(return_value=True)
        cache.db_manager = db
        mwf.return_value = None
        await process_orphaned_cwl_wars(set(), prescan, set())

    return fetched


class TestBattleDayGate:
    """Non-newest CWL files are only fetched once their war is genuinely over."""

    @pytest.mark.asyncio
    async def test_war_still_in_battle_day_is_not_fetched(self, tmp_path: Any) -> None:
        """The regression: round N is non-newest but still being fought."""
        now = datetime.now(timezone.utc)
        shard = tmp_path / "data" / "temp" / "shard_0"
        shard.mkdir(parents=True)
        clan = "AAA0000"

        # Round N: started 2h ago, ends in 22h — demoted to non-newest by round N+1,
        # but the war is still live.  Must NOT be fetched.
        older = _write_war(
            shard, clan, "OPP1111", "8WWWWWWW1", now - timedelta(hours=2), now + timedelta(hours=22), "in_war"
        )
        # Round N+1: preparation file written moments ago — this is the newest file.
        newer = _write_war(
            shard, clan, "OPP2222", "8WWWWWWW2", now - timedelta(minutes=5), now + timedelta(hours=24), "preparation"
        )

        fetched = await _run_scan(tmp_path, [older, newer], clan)

        assert fetched == [], f"still-in-battle war was re-fetched: {fetched}"

    @pytest.mark.asyncio
    async def test_maintenance_extended_war_is_not_fetched(self, tmp_path: Any) -> None:
        """Filename is >25h old, but end_time says the war is still running.

        CoC extends wars by the length of a maintenance outage, so the filename
        timestamp (start) is not a reliable upper bound — Tier 2 must catch this.
        """
        now = datetime.now(timezone.utc)
        shard = tmp_path / "data" / "temp" / "shard_0"
        shard.mkdir(parents=True)
        clan = "BBB0000"

        older = _write_war(
            shard, clan, "OPP1111", "8XXXXXXX1", now - timedelta(hours=30), now + timedelta(hours=1), "in_war"
        )
        newer = _write_war(
            shard, clan, "OPP2222", "8XXXXXXX2", now - timedelta(hours=2), now + timedelta(hours=22), "preparation"
        )

        fetched = await _run_scan(tmp_path, [older, newer], clan)

        assert fetched == [], f"maintenance-extended war was re-fetched: {fetched}"

    @pytest.mark.asyncio
    async def test_genuinely_ended_war_is_still_fetched(self, tmp_path: Any) -> None:
        """The gate must not block real orphans — this is the whole point of the pass."""
        now = datetime.now(timezone.utc)
        shard = tmp_path / "data" / "temp" / "shard_0"
        shard.mkdir(parents=True)
        clan = "CCC0000"

        # Ended 2h ago: past end_time + 1h, so it is a real orphan.
        older = _write_war(
            shard, clan, "OPP1111", "8YYYYYYY1", now - timedelta(hours=26), now - timedelta(hours=2), "in_war"
        )
        newer = _write_war(
            shard, clan, "OPP2222", "8YYYYYYY2", now - timedelta(hours=1), now + timedelta(hours=23), "preparation"
        )

        fetched = await _run_scan(tmp_path, [older, newer], clan)

        assert fetched == ["#8YYYYYYY1"], f"real orphan was not fetched: {fetched}"

    @pytest.mark.asyncio
    async def test_already_war_ended_file_is_not_fetched(self, tmp_path: Any) -> None:
        """A file already in war_ended state is finalized by manage_war_files, not here."""
        now = datetime.now(timezone.utc)
        shard = tmp_path / "data" / "temp" / "shard_0"
        shard.mkdir(parents=True)
        clan = "DDD0000"

        older = _write_war(
            shard, clan, "OPP1111", "8ZZZZZZZ1", now - timedelta(hours=26), now - timedelta(hours=2), "war_ended"
        )
        newer = _write_war(
            shard, clan, "OPP2222", "8ZZZZZZZ2", now - timedelta(hours=1), now + timedelta(hours=23), "preparation"
        )

        fetched = await _run_scan(tmp_path, [older, newer], clan)

        assert fetched == [], f"already-finalized war was re-fetched: {fetched}"
