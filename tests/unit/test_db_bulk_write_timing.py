"""#0113 re-measurement (2026-09-12): pin the [DB-BULK-WRITE] elapsed= field.

The pre-#0106 "1.08s -> 3.18s" write-batch figure that #0113 was filed against was
derived from timestamps BETWEEN log lines, not a measured duration — noisy, since
that also counts whatever ran between batches (API fetches, Phase-1/2 work, etc.).
This test pins that `_flush_pending_war_writes` now logs a real duration covering
just the params-build + executemany + commit critical section, so the PROD
re-measurement's correlation analysis has a field to parse.
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportAttributeAccessIssue=false
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
    """Same fallback-connection pattern as test_db_player_attack_history.py's
    _make_db(): _pool=None forces _sync_conn() through its bare-connection path,
    which ATTACHes a history schema automatically."""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute(_CREATE_WAR_ATTACKS)
    conn.execute(_CREATE_WAR_SUMMARY)
    conn.commit()
    conn.close()

    dm = WarHistoryDB.__new__(WarHistoryDB)
    dm.db_path = db_path
    dm._pool = None  # force fallback to direct sqlite3.connect in _sync_conn
    dm._sync_write_lock = threading.Lock()
    return dm


class TestBulkWriteTimingInstrumentation:
    """Appends use empty attack_rows (summary only) deliberately — a non-empty
    attack_rows list also exercises _upsert_player_name_index_in_conn, which in
    turn touches player_name_search/player_name_fts (fts5). Replicating that whole
    schema here would test the fts5 mirror, not the timer; the timer wraps the same
    try block regardless of which of the two INSERTs inside it actually ran, so a
    summary-only append is sufficient to pin the elapsed= field."""

    def test_logs_elapsed_alongside_existing_counts(self, tmp_path, caplog):
        dm = _make_db(tmp_path)
        appends = [
            ("#CLAN1", [], {"war_id": "W1", "date": "2026-09-12T10:00"}),
        ]

        with caplog.at_level("INFO"):
            dm.flush_pending_war_writes(appends, [], batch_size=50)

        [line] = [r.message for r in caplog.records if "[DB-BULK-WRITE]" in r.message]
        # Pre-existing fields must survive unchanged — this is additive instrumentation.
        assert "Flushed batch of 1 war appends" in line
        assert "0 attack rows" in line
        assert "1 summaries" in line
        # The new field the #0113 re-measurement needs.
        assert "elapsed=" in line
        elapsed = float(line.split("elapsed=")[1].rstrip("s"))
        assert elapsed >= 0.0

    def test_elapsed_logged_per_batch_not_once_for_the_whole_call(self, tmp_path, caplog):
        """Two batches (batch_size=1, two appends) must each get their own elapsed=
        value — a single aggregate figure would hide which specific batch was slow,
        which is exactly the resolution the correlation analysis needs."""
        dm = _make_db(tmp_path)
        appends = [
            ("#CLAN1", [], {"war_id": "W1", "date": "2026-09-12T10:00"}),
            ("#CLAN2", [], {"war_id": "W2", "date": "2026-09-12T10:05"}),
        ]

        with caplog.at_level("INFO"):
            dm.flush_pending_war_writes(appends, [], batch_size=1)

        lines = [r.message for r in caplog.records if "[DB-BULK-WRITE]" in r.message]
        assert len(lines) == 2
        for line in lines:
            assert "elapsed=" in line
