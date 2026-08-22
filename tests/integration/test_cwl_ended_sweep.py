"""Tests for the cwl_ended sweep (tracker #0017, 2026-08-22).

`cwl_league_groups.cwl_ended` gates `is_latest_cwl_season_ended_sync()`, which is what stops
`_find_active_cwl_war_for_clan()` re-walking a finished league group's war tags on every notInWar
clan. The flag had exactly one writer -- `update_cwl_group_stats()`, reached only from a /cwlinfo
render or a cwlgroup subscription -- and no periodic pass, so on 2026-08-22 only 8 of 25,049
August groups were marked ended while 136,707 polled clans kept re-downloading finished wars.

The sweep marks a group on EITHER condition:
  1. every clan played all its rounds (the pre-existing test), or
  2. the season's war window is definitively over.
(2) is load-bearing: measured on real data only ~55% of groups can ever satisfy (1), because the
rest contain clans whose wars are never fetched at all.

The property the project owner cared about -- that this must not freeze any standings -- is
pinned down by test_sweep_never_writes_standings below.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import List, Tuple

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")

from qapbot.constants import cwl_season_window_closed
from qapbot.db_manager import WarHistoryDB
from QBhelperfunctions import cwl_group_all_rounds_ended


NOW = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# cwl_season_window_closed -- the time-based half of the verdict
# ---------------------------------------------------------------------------

class TestSeasonWindowClosed:
    @pytest.mark.parametrize("season,expected", [
        ("2026-08", True),      # window 08-01 .. 08-15; wars really ended 08-11
        ("2026-07", True),      # long over
        ("2026-06-15", True),   # bonus CWL, window 06-15 .. 06-29; wars ended 06-26
        ("2026-08-15", False),  # a bonus CWL that started a week ago -- still live
        ("2026-09", False),     # future season
    ])
    def test_window_verdicts(self, season, expected):
        assert cwl_season_window_closed(season, NOW) is expected

    @pytest.mark.parametrize("bad", ["", "garbage", "2026-13", "2026", "2026-02-30"])
    def test_unparseable_keys_are_never_treated_as_closed(self, bad):
        """Marking a season ended suppresses live CWL detection for every clan in it, so a key we
        do not understand must always fall back to 'still open'."""
        assert cwl_season_window_closed(bad, NOW) is False

    def test_boundary_is_inclusive_at_exactly_the_window_length(self):
        # 2026-08-01 + 14 days == 2026-08-15
        assert cwl_season_window_closed("2026-08", datetime(2026, 8, 14, 23, 59, tzinfo=timezone.utc)) is False
        assert cwl_season_window_closed("2026-08", datetime(2026, 8, 15, 0, 1, tzinfo=timezone.utc)) is True


# ---------------------------------------------------------------------------
# cwl_group_all_rounds_ended -- the data-completeness half, now shared
# ---------------------------------------------------------------------------

class TestAllRoundsEnded:
    def test_complete_group(self):
        tags = [f"#C{i}" for i in range(8)]
        assert cwl_group_all_rounds_ended(tags, {t: 7 for t in tags}) is True

    def test_one_clan_short_blocks_the_whole_group(self):
        tags = [f"#C{i}" for i in range(8)]
        ended = {t: 7 for t in tags}
        ended["#C3"] = 6
        assert cwl_group_all_rounds_ended(tags, ended) is False

    def test_untracked_clans_count_as_zero(self):
        """The real-world ~45% case: most of the group has no war data at all."""
        tags = [f"#C{i}" for i in range(8)]
        assert cwl_group_all_rounds_ended(tags, {"#C0": 7}) is False

    def test_short_group_uses_n_minus_1_rounds(self):
        assert cwl_group_all_rounds_ended(["#A", "#B", "#C"], {"#A": 2, "#B": 2, "#C": 2}) is True
        assert cwl_group_all_rounds_ended(["#A", "#B", "#C"], {"#A": 2, "#B": 2, "#C": 1}) is False

    def test_empty_group_is_never_ended(self):
        assert cwl_group_all_rounds_ended([], {}) is False


# ---------------------------------------------------------------------------
# DB layer + the sweep itself
# ---------------------------------------------------------------------------

@pytest.fixture
async def db(tmp_path):
    manager = WarHistoryDB()
    await manager.initialize(str(tmp_path / "sweep.db"), str(tmp_path / "sweep_history.db"))
    try:
        yield manager
    finally:
        await manager.close()


async def _add_group(db: WarHistoryDB, group_id: str, season: str, clan_tags: List[str]) -> None:
    """cwl_league_groups has UNIQUE(cwl_season, clan_tag) -- a clan belongs to exactly one group
    per season -- so every group in a season needs its own tags. Callers pass bare suffixes and
    this namespaces them by group_id."""
    for tag in [f"#{group_id}{t.lstrip('#')}" for t in clan_tags]:
        await db._conn.execute(
            "INSERT INTO cwl_league_groups (league_group_id, cwl_season, clan_tag, cwl_ended) "
            "VALUES (?, ?, ?, 0)",
            (group_id, season, tag),
        )
    await db._conn.commit()


async def _add_ended_wars(db: WarHistoryDB, clan_tag: str, season: str, count: int) -> None:
    for i in range(count):
        await db._conn.execute(
            "INSERT INTO main.war_summary "
            "(war_id, clan_tag, opponent_tag, date, is_cwl, cwl_season, state, "
            " clan_stars, clan_destruction, team_size, result) "
            "VALUES (?, ?, '#OPP', ?, 1, ?, 'war_ended', 5, 50.0, 15, 'win')",
            (f"{clan_tag}-{season}-{i}", clan_tag, f"{season[:7]}-0{(i % 9) + 1}T08:00", season),
        )
    await db._conn.commit()


def _ended_flags(db: WarHistoryDB, group_id: str) -> List[int]:
    with db._sync_conn() as conn:
        return [
            r["cwl_ended"]
            for r in conn.execute(
                "SELECT cwl_ended FROM cwl_league_groups WHERE league_group_id = ?", (group_id,)
            )
        ]


@pytest.mark.asyncio
async def test_page_query_is_keyset_paginated_and_excludes_ended_groups(db):
    for i in range(5):
        await _add_group(db, f"g{i:02d}", "2026-08", ["#A", "#B"])
    db.mark_cwl_groups_ended_sync([("g02", "2026-08")])

    page1 = db.find_unended_cwl_groups_page_sync("", "", 2)
    assert [g["league_group_id"] for g in page1] == ["g00", "g01"]
    assert page1[0]["clan_tags"] == ["#g00A", "#g00B"]

    cursor = (page1[-1]["league_group_id"], page1[-1]["cwl_season"])
    page2 = db.find_unended_cwl_groups_page_sync(cursor[0], cursor[1], 2)
    # g02 is already ended -- it must not reappear.
    assert [g["league_group_id"] for g in page2] == ["g03", "g04"]

    cursor = (page2[-1]["league_group_id"], page2[-1]["cwl_season"])
    assert db.find_unended_cwl_groups_page_sync(cursor[0], cursor[1], 2) == []


@pytest.mark.asyncio
async def test_mark_groups_ended_sets_only_the_flag(db):
    """The project owner's concern about option A: marking must not freeze standings. The sweep
    writes cwl_ended and nothing else, so total_stars stays NULL and update_cwl_group_stats()'s
    freeze short-circuit (which requires non-NULL stats) never engages."""
    await _add_group(db, "gX", "2026-08", ["#A", "#B", "#C"])
    updated = db.mark_cwl_groups_ended_sync([("gX", "2026-08")])
    assert updated == 3

    with db._sync_conn() as conn:
        rows = list(conn.execute(
            "SELECT cwl_ended, group_rank, total_stars, total_destruction "
            "FROM cwl_league_groups WHERE league_group_id = 'gX'"
        ))
    assert all(r["cwl_ended"] == 1 for r in rows)
    assert all(r["total_stars"] is None for r in rows)
    assert all(r["group_rank"] is None for r in rows)
    assert all(r["total_destruction"] is None for r in rows)

    # Idempotent: a second call finds nothing left at cwl_ended=0.
    assert db.mark_cwl_groups_ended_sync([("gX", "2026-08")]) == 0


@pytest.mark.asyncio
async def test_sync_war_stats_twin_matches_the_async_one(db):
    """The sweep and the on-demand path must reach the same completion verdict, so their two
    aggregate queries have to stay in step."""
    await _add_ended_wars(db, "#A", "2026-08", 7)
    await _add_ended_wars(db, "#B", "2026-08", 3)

    async_stars, async_ended = await db.get_cwl_group_war_stats("2026-08", ["#A", "#B"])
    sync_stars, sync_ended = db.get_cwl_group_war_stats_sync("2026-08", ["#A", "#B"])

    assert async_ended == sync_ended == {"#A": 7, "#B": 3}
    assert async_stars == sync_stars


@pytest.mark.asyncio
async def test_sweep_marks_a_group_whose_season_window_has_closed(db, monkeypatch):
    """The ~45% case: no war data for anyone in the group, so all_ended can never fire -- the
    time-based condition is the only thing that resolves it."""
    from qapbot.cache_manager import CACHE
    import QBhelperfunctions as H

    monkeypatch.setattr(CACHE, "db_manager", db)
    monkeypatch.setattr(H, "_cwl_ended_sweep_cursor", ("", ""), raising=False)
    await _add_group(db, "gOld", "2026-07", [f"#C{i}" for i in range(8)])

    result = await H.sweep_cwl_ended_flags(batches=1, batch_size=10)

    assert result["marked"] == 1
    assert _ended_flags(db, "gOld") == [1] * 8


@pytest.mark.asyncio
async def test_sweep_marks_an_in_window_group_that_completed_every_round(db, monkeypatch):
    """all_ended still fires early, without waiting for the window to close -- which is why both
    conditions are kept rather than replacing one with the other."""
    from qapbot.cache_manager import CACHE
    import QBhelperfunctions as H

    monkeypatch.setattr(CACHE, "db_manager", db)
    monkeypatch.setattr(H, "_cwl_ended_sweep_cursor", ("", ""), raising=False)
    monkeypatch.setattr(H, "cwl_season_window_closed", lambda season, now=None: False, raising=False)

    tags = [f"#C{i}" for i in range(4)]
    await _add_group(db, "gLive", "2026-09", tags)
    for tag in tags:
        # _add_group namespaces tags by group_id -- the war rows must use the same names.
        await _add_ended_wars(db, f"#gLive{tag.lstrip('#')}", "2026-09", 3)  # n-1 == 3 rounds

    result = await H.sweep_cwl_ended_flags(batches=1, batch_size=10)

    assert result["marked"] == 1
    assert _ended_flags(db, "gLive") == [1] * 4


@pytest.mark.asyncio
async def test_sweep_leaves_an_in_window_incomplete_group_alone(db, monkeypatch):
    """A CWL still in progress must never be marked -- that is what would freeze a live season."""
    from qapbot.cache_manager import CACHE
    import QBhelperfunctions as H

    monkeypatch.setattr(CACHE, "db_manager", db)
    monkeypatch.setattr(H, "_cwl_ended_sweep_cursor", ("", ""), raising=False)

    tags = [f"#C{i}" for i in range(4)]
    await _add_group(db, "gRunning", "2026-09", tags)  # 2026-09 window not closed at real "now"
    for tag in tags[:2]:
        await _add_ended_wars(db, f"#gRunning{tag.lstrip('#')}", "2026-09", 3)

    result = await H.sweep_cwl_ended_flags(batches=1, batch_size=10)

    assert result["marked"] == 0
    assert _ended_flags(db, "gRunning") == [0] * 4


@pytest.mark.asyncio
async def test_sweep_runs_five_batches_per_call_and_rotates_the_cursor(db, monkeypatch):
    """Project owner's spec: 5 batches per update cycle. Each is its own to_thread hop, run
    sequentially so the event loop breathes between them."""
    from qapbot.cache_manager import CACHE
    import QBhelperfunctions as H

    monkeypatch.setattr(CACHE, "db_manager", db)
    monkeypatch.setattr(H, "_cwl_ended_sweep_cursor", ("", ""), raising=False)
    for i in range(12):
        await _add_group(db, f"g{i:02d}", "2026-07", ["#A", "#B"])

    seen: List[Tuple[str, str, int]] = []
    real = H._sweep_cwl_ended_batch_sync

    def spy(after_group_id, after_season, limit):
        seen.append((after_group_id, after_season, limit))
        return real(after_group_id, after_season, limit)

    monkeypatch.setattr(H, "_sweep_cwl_ended_batch_sync", spy)

    result = await H.sweep_cwl_ended_flags(batches=5, batch_size=2)

    assert result["batches_run"] == 5
    assert result["checked"] == 10
    assert result["marked"] == 10
    # The cursor advanced across batches instead of re-reading the same page.
    assert [s[0] for s in seen] == ["", "g01", "g03", "g05", "g07"]
    assert all(s[2] == 2 for s in seen)


@pytest.mark.asyncio
async def test_sweep_wraps_the_cursor_at_the_end_of_the_rotation(db, monkeypatch):
    """A short page means the rotation finished. Without the wrap, the cursor would sit past the
    last group forever and the ~45% that can never complete would stop being re-checked."""
    from qapbot.cache_manager import CACHE
    import QBhelperfunctions as H

    monkeypatch.setattr(CACHE, "db_manager", db)
    monkeypatch.setattr(H, "_cwl_ended_sweep_cursor", ("", ""), raising=False)
    await _add_group(db, "g00", "2026-07", ["#A"])

    result = await H.sweep_cwl_ended_flags(batches=5, batch_size=10)

    assert result["wrapped"] == 1
    assert result["batches_run"] == 1  # stops on the short page rather than spinning
    assert H._cwl_ended_sweep_cursor == ("", "")


@pytest.mark.asyncio
async def test_sweep_never_writes_standings(db, monkeypatch):
    """The whole reason option A was safe to ship: the sweep sets the flag only, so
    update_cwl_group_stats()'s freeze short-circuit (cwl_ended AND non-NULL stats) stays disarmed
    and a swept group still recomputes its standings live on the next render."""
    from qapbot.cache_manager import CACHE
    import QBhelperfunctions as H

    monkeypatch.setattr(CACHE, "db_manager", db)
    monkeypatch.setattr(H, "_cwl_ended_sweep_cursor", ("", ""), raising=False)
    tags = [f"#C{i}" for i in range(8)]
    await _add_group(db, "gNoStats", "2026-07", tags)
    for tag in tags:
        await _add_ended_wars(db, f"#gNoStats{tag.lstrip('#')}", "2026-07", 7)

    await H.sweep_cwl_ended_flags(batches=1, batch_size=10)

    with db._sync_conn() as conn:
        rows = list(conn.execute(
            "SELECT cwl_ended, total_stars, group_rank FROM cwl_league_groups "
            "WHERE league_group_id = 'gNoStats'"
        ))
    assert all(r["cwl_ended"] == 1 for r in rows)
    assert all(r["total_stars"] is None for r in rows), "sweep must not persist standings"
    assert all(r["group_rank"] is None for r in rows)


@pytest.mark.asyncio
async def test_sweep_is_a_no_op_once_everything_is_marked(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    import QBhelperfunctions as H

    monkeypatch.setattr(CACHE, "db_manager", db)
    monkeypatch.setattr(H, "_cwl_ended_sweep_cursor", ("", ""), raising=False)
    await _add_group(db, "g00", "2026-07", ["#A", "#B"])

    first = await H.sweep_cwl_ended_flags(batches=5, batch_size=10)
    second = await H.sweep_cwl_ended_flags(batches=5, batch_size=10)

    assert first["marked"] == 1
    assert second["marked"] == 0
    assert second["checked"] == 0
