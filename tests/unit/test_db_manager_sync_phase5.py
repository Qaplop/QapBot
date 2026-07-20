"""Tests for db_manager.py synchronous methods — Phase 5 coverage push.

Covers check_integrity_sync, get_war_statistics_sync, get_all_wars_sync,
check_war_exists_sync, get_recent_wars_sync using real in-memory SQLite.
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnusedImport=false, reportAttributeAccessIssue=false, reportReturnType=false, reportOptionalMemberAccess=false
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest


def _make_db(tmp_path):
    """Create a real SQLite DB with war_history table and return (db_manager, db_path)."""
    from qapbot.db_manager import WarHistoryDB
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS war_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clan_tag TEXT NOT NULL,
            war_id TEXT NOT NULL,
            date TEXT NOT NULL,
            player_tag TEXT,
            player_name TEXT,
            th_level INTEGER,
            map_position INTEGER,
            stars INTEGER,
            destruction REAL,
            attacks_used INTEGER
        )
    """)
    conn.commit()
    conn.close()

    dm = WarHistoryDB.__new__(WarHistoryDB)
    dm.db_path = db_path
    dm.conn = None
    return dm, db_path


def _insert_war(db_path, clan_tag, war_id, date_str, player_tag="#P1"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO war_history (clan_tag, war_id, date, player_tag, player_name, th_level, map_position, stars, destruction, attacks_used) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (clan_tag, war_id, date_str, player_tag, "Player1", 16, 1, 3, 100.0, 2),
    )
    conn.commit()
    conn.close()


# ===========================================================================
# check_integrity_sync
# ===========================================================================

class TestCheckIntegritySync:
    def test_healthy_db(self, tmp_path):
        dm, _ = _make_db(tmp_path)
        ok, errors = dm.check_integrity_sync()
        assert ok is True
        assert errors == []

    def test_no_db_path_raises(self):
        from qapbot.db_manager import WarHistoryDB
        dm = WarHistoryDB.__new__(WarHistoryDB)
        dm.db_path = None
        with pytest.raises(RuntimeError, match="not initialized"):
            dm.check_integrity_sync()

    def test_corrupt_db(self, tmp_path):
        from qapbot.db_manager import WarHistoryDB
        bad_path = str(tmp_path / "corrupt.db")
        with open(bad_path, "w") as f:
            f.write("not a sqlite database")
        dm = WarHistoryDB.__new__(WarHistoryDB)
        dm.db_path = bad_path
        ok, errors = dm.check_integrity_sync()
        assert ok is False
        assert len(errors) >= 1


# ===========================================================================
# get_war_statistics_sync
# ===========================================================================

