"""Tests for db_manager.get_player_attack_history_sync (leaderboard scope="all").

Uses a real in-memory-backed SQLite database with the war_attacks schema,
mirroring the pattern in test_db_search_players_by_name.py.
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportAttributeAccessIssue=false, reportReturnType=false, reportOptionalMemberAccess=false
from __future__ import annotations

import sqlite3
import threading

from qapbot.db_manager import WarHistoryDB

_CREATE_WAR_ATTACKS = """
    CREATE TABLE IF NOT EXISTS war_attacks (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        war_id                 TEXT    NOT NULL,
        clan_tag               TEXT    NOT NULL,
        date                   TEXT    NOT NULL,
        player_name            TEXT    NOT NULL,
        player_tag             TEXT    NOT NULL,
        th_level               INTEGER NOT NULL,
        map_position           INTEGER NOT NULL DEFAULT 0,
        attack_order           INTEGER NOT NULL DEFAULT 0,
        stars                  INTEGER NOT NULL,
        destruction            REAL    NOT NULL DEFAULT 0.0,
        defender_tag           TEXT    NOT NULL DEFAULT '',
        defender_th            INTEGER NOT NULL DEFAULT 0,
        defender_map_position  INTEGER NOT NULL DEFAULT 0,
        duration               INTEGER NOT NULL DEFAULT 0,
        is_fresh               INTEGER NOT NULL DEFAULT -1,
        times_defended         INTEGER NOT NULL DEFAULT 0,
        best_def_destruction   REAL    NOT NULL DEFAULT 0.0,
        max_attacks            INTEGER NOT NULL DEFAULT 2,
        missed_attacks         INTEGER NOT NULL DEFAULT 0,
        defensive_stars        INTEGER NOT NULL DEFAULT 0,
        created_at             TEXT    DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(war_id, player_tag, attack_order)
    )
"""

_CREATE_WAR_SUMMARY = """
    CREATE TABLE IF NOT EXISTS war_summary (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        war_id               TEXT    NOT NULL,
        clan_tag             TEXT    NOT NULL,
        opponent_tag         TEXT    NOT NULL,
        opponent_name        TEXT    NOT NULL DEFAULT '',
        clan_stars           INTEGER NOT NULL DEFAULT 0,
        opponent_stars       INTEGER NOT NULL DEFAULT 0,
        clan_destruction     REAL    NOT NULL DEFAULT 0.0,
        opp_destruction      REAL    NOT NULL DEFAULT 0.0,
        team_size            INTEGER NOT NULL DEFAULT 15,
        attacks_per_member   INTEGER NOT NULL DEFAULT 2,
        war_type             TEXT    NOT NULL DEFAULT 'random',
        is_cwl               INTEGER NOT NULL DEFAULT 0,
        cwl_season           TEXT    NOT NULL DEFAULT '',
        war_tag              TEXT    NOT NULL DEFAULT '',
        end_time             TEXT    NOT NULL DEFAULT '',
        state                TEXT    NOT NULL DEFAULT '',
        result               TEXT    NOT NULL DEFAULT '',
        date                 TEXT    NOT NULL,
        clan_lineup_json     TEXT    NOT NULL DEFAULT '[]',
        opp_lineup_json      TEXT    NOT NULL DEFAULT '[]',
        clan_attacks_used    INTEGER NOT NULL DEFAULT 0,
        opp_attacks_used     INTEGER NOT NULL DEFAULT 0,
        round_number         INTEGER,
        created_at           TEXT    DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(war_id, clan_tag)
    )
"""


def _make_db(tmp_path) -> WarHistoryDB:
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(_CREATE_WAR_ATTACKS)
    conn.execute(_CREATE_WAR_SUMMARY)
    conn.commit()
    conn.close()

    dm = WarHistoryDB.__new__(WarHistoryDB)
    dm.db_path = db_path
    dm._pool = None  # force fallback to direct sqlite3.connect in _sync_conn
    dm._sync_write_lock = threading.Lock()
    return dm


def _insert_attack(
    db_path: str,
    *,
    war_id: str,
    clan_tag: str,
    date: str,
    player_name: str,
    player_tag: str,
    attack_order: int = 1,
    stars: int = 3,
    max_attacks: int = 2,
    missed_attacks: int = 0,
) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT OR IGNORE INTO war_attacks
           (war_id, clan_tag, date, player_name, player_tag, th_level,
            attack_order, stars, max_attacks, missed_attacks)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (war_id, clan_tag, date, player_name, player_tag, 15, attack_order, stars,
         max_attacks, missed_attacks),
    )
    conn.commit()
    conn.close()


def _insert_summary(db_path: str, *, war_id: str, clan_tag: str, cwl_season: str, is_cwl: int = 1) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT OR IGNORE INTO war_summary (war_id, clan_tag, opponent_tag, is_cwl, cwl_season, date)
           VALUES (?,?,?,?,?,?)""",
        (war_id, clan_tag, "#OPP", is_cwl, cwl_season, "2026-06-15T10:00"),
    )
    conn.commit()
    conn.close()


class TestGetPlayerAttackHistorySync:
    def test_cross_clan_aggregation(self, tmp_path):
        """A player who fought for two different clans is credited for both."""
        dm = _make_db(tmp_path)
        _insert_attack(dm.db_path, war_id="W1", clan_tag="#OLD", date="2026-06-05T10:00",
                        player_name="Alice", player_tag="#P1", stars=2)
        _insert_attack(dm.db_path, war_id="W2", clan_tag="#NEW", date="2026-06-20T10:00",
                        player_name="Alice", player_tag="#P1", stars=3)

        rows = dm.get_player_attack_history_sync(["#P1"], month=6, year=2026)

        assert len(rows) == 2
        total_stars = sum(r["Stars"] for r in rows)
        assert total_stars == 5
        # WarIDs must be distinguishable even though clan_tags differ
        war_ids = {r["WarID"] for r in rows}
        assert war_ids == {"#OLD::W1", "#NEW::W2"}

    def test_month_filter_excludes_other_months(self, tmp_path):
        dm = _make_db(tmp_path)
        _insert_attack(dm.db_path, war_id="W1", clan_tag="#A", date="2026-05-05T10:00",
                        player_name="Bob", player_tag="#P2", stars=1)
        _insert_attack(dm.db_path, war_id="W2", clan_tag="#A", date="2026-06-05T10:00",
                        player_name="Bob", player_tag="#P2", stars=2)

        rows = dm.get_player_attack_history_sync(["#P2"], month=6, year=2026)

        assert len(rows) == 1
        assert rows[0]["Stars"] == 2

    def test_unrelated_player_not_included(self, tmp_path):
        dm = _make_db(tmp_path)
        _insert_attack(dm.db_path, war_id="W1", clan_tag="#A", date="2026-06-05T10:00",
                        player_name="Bob", player_tag="#P2", stars=1)

        rows = dm.get_player_attack_history_sync(["#P3"], month=6, year=2026)

        assert rows == []

    def test_empty_player_tags_returns_empty(self, tmp_path):
        dm = _make_db(tmp_path)
        assert dm.get_player_attack_history_sync([], month=6, year=2026) == []

    def test_zero_attack_rows_included(self, tmp_path):
        """A player who missed all attacks (attack_order=0) still shows up with 0 stars."""
        dm = _make_db(tmp_path)
        _insert_attack(dm.db_path, war_id="W1", clan_tag="#A", date="2026-06-05T10:00",
                        player_name="Carol", player_tag="#P4", attack_order=0, stars=0,
                        missed_attacks=2)

        rows = dm.get_player_attack_history_sync(["#P4"], month=6, year=2026)

        assert len(rows) == 1
        assert rows[0]["Stars"] == 0
        assert rows[0]["Missed_Attacks"] == 2

    def test_cwl_only_filter_via_max_attacks(self, tmp_path):
        dm = _make_db(tmp_path)
        _insert_attack(dm.db_path, war_id="W1", clan_tag="#A", date="2026-06-05T10:00",
                        player_name="Dan", player_tag="#P5", stars=3, max_attacks=2)
        _insert_attack(dm.db_path, war_id="W2", clan_tag="#B", date="2026-06-10T10:00",
                        player_name="Dan", player_tag="#P5", stars=2, max_attacks=1)

        rows = dm.get_player_attack_history_sync(["#P5"], month=6, year=2026)
        cwl_rows = [r for r in rows if r.get("Max_Attacks", 2) == 1]

        assert len(rows) == 2
        assert len(cwl_rows) == 1
        assert cwl_rows[0]["Stars"] == 2


class TestGetWarSummariesSyncCrossClan:
    def test_clan_tag_none_returns_all_clans(self, tmp_path):
        dm = _make_db(tmp_path)
        _insert_summary(dm.db_path, war_id="W1", clan_tag="#OLD", cwl_season="2026-06")
        _insert_summary(dm.db_path, war_id="W2", clan_tag="#NEW", cwl_season="2026-06")
        _insert_summary(dm.db_path, war_id="W3", clan_tag="#OTHER", cwl_season="2025-12")

        rows = dm.get_war_summaries_sync(None, season="2026-06")

        assert {(r["clan_tag"], r["war_id"]) for r in rows} == {("#OLD", "W1"), ("#NEW", "W2")}

    def test_clan_tag_given_still_filters_by_clan(self, tmp_path):
        dm = _make_db(tmp_path)
        _insert_summary(dm.db_path, war_id="W1", clan_tag="#OLD", cwl_season="2026-06")
        _insert_summary(dm.db_path, war_id="W2", clan_tag="#NEW", cwl_season="2026-06")

        rows = dm.get_war_summaries_sync("#OLD", season="2026-06")

        assert len(rows) == 1
        assert rows[0]["clan_tag"] == "#OLD"
