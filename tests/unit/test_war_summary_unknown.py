"""WAR_SUMMARY_UNKNOWN (-1) = "data not available" in a numeric war_summary column (2026-09-26).

Reconstructed legacy wars (2025-07-26 .. 2026-01, backfill_legacy_war_summaries.py) store -1 in
clan_stars, clan_destruction, opp_destruction, opp_attacks_used (and opponent_stars for the bot's
first week). These tests pin the reader rules from clashcontrol/constants.py: aggregates exclude
it, renderers show "?", and no result is ever derived from it.
"""
# pyright: reportPrivateUsage=false
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")

from clashcontrol.constants import WAR_SUMMARY_UNKNOWN  # noqa: E402
from clashcontrol.db_manager import WarHistoryDB  # noqa: E402

U = WAR_SUMMARY_UNKNOWN
_ROOT = Path(__file__).resolve().parents[2]
_UNKNOWN_CAPABLE = ("clan_stars", "opponent_stars", "clan_destruction", "opp_destruction", "opp_attacks_used")


def test_marker_value():
    assert WAR_SUMMARY_UNKNOWN == -1  # the value the backfill script and every reader assume


def test_classify_war_result_never_guesses_from_unknown():
    from QBhelperfunctions import classify_war_result

    assert classify_war_result(U, 30, U, U) == "❔ Unknown"
    assert classify_war_result(30, U, 90.0, 80.0) == "❔ Unknown"
    assert classify_war_result(30, 30, U, 80.0) == "❔ Unknown"      # tie decided by unknown destruction
    assert classify_war_result(31, 30, U, U) == "✅ Win"              # stars decide, destruction not needed
    assert classify_war_result(29, 30, U, U) == "❌ Loss"
    assert classify_war_result(30, 30, 90.0, 80.0) == "✅ Win"        # normal wars unchanged
    assert classify_war_result(30, 30, 80.0, 80.0) == "🤝 Draw"


def test_display_helpers_show_question_mark():
    from QBhelperfunctions import _fmt_ws_destruction, _fmt_ws_stars

    assert _fmt_ws_stars(U) == "?" and _fmt_ws_stars(0) == "0" and _fmt_ws_stars(27) == "27"
    assert _fmt_ws_destruction(float(U)) == "?" and _fmt_ws_destruction(83.456) == "83.5"


def test_no_unguarded_sql_aggregate_over_unknown_capable_columns():
    """A bare SUM(clan_stars) silently subtracts 1 per legacy war. Every aggregate over these
    columns must go through CASE WHEN col >= 0 (see constants.WAR_SUMMARY_UNKNOWN)."""
    pattern = re.compile(
        r"\b(SUM|AVG|MIN|MAX|TOTAL)\(\s*(\w+\.)?(" + "|".join(_UNKNOWN_CAPABLE) + r")\b"
    )
    offenders = []
    for path in _ROOT.rglob("*.py"):
        rel = path.relative_to(_ROOT).as_posix()
        if rel.startswith(("venv/", "tests/", "activity/")) or "/node_modules/" in rel:
            continue
        for no, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{rel}:{no}: {line.strip()}")
    assert not offenders, "unguarded aggregate over a WAR_SUMMARY_UNKNOWN column:\n" + "\n".join(offenders)


@pytest.fixture
async def db(tmp_path):
    manager = WarHistoryDB()
    await manager.initialize(str(tmp_path / "unknown.db"))
    try:
        yield manager
    finally:
        await manager.close()


def _insert(conn, schema: str, war_id: str, clan: str, stars: int, dest: float, result: str, season: str = "2025-09") -> None:
    conn.execute(
        f"INSERT INTO {schema}.war_summary (war_id, clan_tag, opponent_tag, clan_stars, clan_destruction,"
        " team_size, is_cwl, cwl_season, state, result, date)"
        " VALUES (?, ?, '#OPP', ?, ?, 15, 1, ?, 'war_ended', ?, '2025-09-02T10:00')",
        (war_id, clan, stars, dest, season, result),
    )


@pytest.mark.asyncio
async def test_cwl_group_stats_exclude_unknown_values(db):
    """Both twins (async + sync) skip -1 and still count the war as ended."""
    with db._sync_conn() as conn:
        _insert(conn, "history", "w1", "#A", 20, 80.0, "win")
        _insert(conn, "history", "w2", "#A", U, U, "")           # reconstructed legacy war
        _insert(conn, "main", "w3", "#A", 25, 90.0, "loss")
        conn.commit()

    stars_async, ended_async = await db.get_cwl_group_war_stats("2025-09", ["#A"])
    stars_sync, ended_sync = db.get_cwl_group_war_stats_sync("2025-09", ["#A"])
    expected_stars = (20 + 25 + 10, (80.0 + 90.0) * 15)   # +10 = win bonus; -1 rows contribute nothing
    assert stars_async["#A"] == expected_stars == stars_sync["#A"]
    assert ended_async["#A"] == 3 == ended_sync["#A"]


@pytest.mark.asyncio
async def test_whois_clan_stats_count_wars_without_result(db):
    with db._sync_conn() as conn:
        _insert(conn, "history", "w1", "#A", 20, 80.0, "win")
        _insert(conn, "history", "w2", "#A", U, U, "")
        conn.execute(
            "INSERT INTO main.war_summary (war_id, clan_tag, opponent_tag, is_cwl, state, result, date)"
            " VALUES ('live', '#A', '#OPP', 1, 'in_war', '', '2026-09-26T10:00')"
        )
        conn.commit()

    stats = db.get_clan_whois_stats_sync("#A")
    assert stats["cwl"]["wars"] == 3
    assert stats["cwl"]["wins"] == 1
    assert stats["cwl"]["unknown"] == 1   # the running war is not "without result"
