"""Tests for the player_name_index DB-layer maintenance helpers: the incremental writer
(_upsert_player_name_index_in_conn), the startup bulk loader (load_player_name_index_sync), and
the API-detected-name-change batch upsert (update_player_name_index_sync).

Renamed from test_db_search_players_by_name.py (2026-08-18,
PLAYER_NAME_INDEX_RETIREMENT_PLAN.md Step 7) after search_players_by_name_sync — the sole reason
for that file's original name and its war_attacks-table test fixtures — was deleted as dead code
(superseded by the in-memory index long before this session, then by SQLite/FTS5; zero
production callers). This file's actual remaining content was always about player_name_index
maintenance, not name search, so the name now matches what it tests.
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportAttributeAccessIssue=false, reportReturnType=false, reportOptionalMemberAccess=false
from __future__ import annotations

import sqlite3
import threading

import pytest

from qapbot.db_manager import WarHistoryDB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CREATE_PLAYER_NAME_INDEX = """
    CREATE TABLE IF NOT EXISTS player_name_index (
        player_tag  TEXT PRIMARY KEY,
        player_name TEXT NOT NULL,
        last_seen   TEXT NOT NULL
    )
"""

# player_name_search / player_name_fts (2026-08-17, CWL_PROD_PERFORMANCE_FIX_PLAN.md Step 11) —
# _upsert_player_name_index_in_conn/update_player_name_index_sync now also write these, so this
# file's hand-built minimal schema (deliberately NOT the full WarHistoryDB.initialize(), see
# _make_db's own docstring) needs them too, same as the real schema-creation code does.
_CREATE_PLAYER_NAME_SEARCH = """
    CREATE TABLE IF NOT EXISTS player_name_search (
        player_tag  TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        name_lower  TEXT NOT NULL
    )
"""
_CREATE_PLAYER_NAME_FTS = """
    CREATE VIRTUAL TABLE IF NOT EXISTS player_name_fts USING fts5(
        player_tag UNINDEXED, name, tokenize='trigram'
    )
"""


def _make_db(tmp_path) -> WarHistoryDB:
    """Create a WarHistoryDB with just the player_name_index/player_name_search/player_name_fts
    schema in a temp file (deliberately NOT the full WarHistoryDB.initialize())."""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(_CREATE_PLAYER_NAME_INDEX)
    conn.execute(_CREATE_PLAYER_NAME_SEARCH)
    conn.execute(_CREATE_PLAYER_NAME_FTS)
    conn.commit()
    conn.close()

    dm = WarHistoryDB.__new__(WarHistoryDB)
    dm.db_path = db_path
    dm._pool = None  # force fallback to direct sqlite3.connect in _sync_conn
    dm._sync_write_lock = threading.Lock()
    return dm


def _path(dm: WarHistoryDB) -> str:
    """Non-Optional view of dm.db_path -- always set by _make_db() above."""
    assert dm.db_path is not None
    return dm.db_path


# ---------------------------------------------------------------------------
# Tests: _upsert_player_name_index_in_conn (private helper)
# ---------------------------------------------------------------------------

class TestUpsertPlayerNameIndexInConn:

    def _open_conn(self, db_path: str) -> sqlite3.Connection:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def test_dict_rows_upserted(self, tmp_path):
        """Dict attack rows (from add_war_data_sync path) are upserted correctly."""
        dm = _make_db(tmp_path)
        rows = [
            {"PlayerID": "#A1", "Player": "Alice", "Date": "2025-01-01T10:00", "attack_order": 1},
            {"PlayerID": "#B1", "Player": "Bob",   "Date": "2025-01-02T10:00", "attack_order": 2},
        ]
        conn = self._open_conn(_path(dm))
        dm._upsert_player_name_index_in_conn(conn, rows)
        conn.commit()
        all_rows = conn.execute("SELECT player_tag, player_name FROM player_name_index").fetchall()
        conn.close()
        index = {r["player_tag"]: r["player_name"] for r in all_rows}
        assert index == {"#A1": "Alice", "#B1": "Bob"}

    def test_tuple_rows_upserted(self, tmp_path):
        """Tuple attack rows (from batch flush path) are upserted correctly."""
        dm = _make_db(tmp_path)
        # Tuple layout: (war_id[0], clan_tag[1], date[2], player_name[3], player_tag[4],
        #                th[5], map_pos[6], attack_order[7], ...)
        rows = [
            ("W1", "#CLAN", "2025-03-01T10:00", "Charlie", "#C1", 15, 1, 1, 3, 100, "#DEF", 15, 1, 30, 1, 0, 0, 2, 0, 0),
        ]
        conn = self._open_conn(_path(dm))
        dm._upsert_player_name_index_in_conn(conn, rows)
        conn.commit()
        row = conn.execute("SELECT player_name FROM player_name_index WHERE player_tag='#C1'").fetchone()
        conn.close()
        assert row["player_name"] == "Charlie"

    def test_sentinel_rows_included(self, tmp_path):
        """attack_order=0 rows (missed-all-attacks sentinels) ARE indexed for /whois."""
        dm = _make_db(tmp_path)
        rows = [
            {"PlayerID": "#G1", "Player": "Ghost", "Date": "2025-01-01T10:00", "attack_order": 0},
        ]
        conn = self._open_conn(_path(dm))
        dm._upsert_player_name_index_in_conn(conn, rows)
        conn.commit()
        count = conn.execute("SELECT COUNT(*) AS cnt FROM player_name_index").fetchone()["cnt"]
        conn.close()
        assert count == 1

    def test_newer_date_wins(self, tmp_path):
        """When the same player_tag appears twice, the row with the later date wins."""
        dm = _make_db(tmp_path)
        rows_old = [{"PlayerID": "#P1", "Player": "OldName", "Date": "2024-01-01T00:00", "attack_order": 1}]
        rows_new = [{"PlayerID": "#P1", "Player": "NewName", "Date": "2025-06-01T00:00", "attack_order": 1}]
        conn = self._open_conn(_path(dm))
        dm._upsert_player_name_index_in_conn(conn, rows_old)
        conn.commit()
        dm._upsert_player_name_index_in_conn(conn, rows_new)
        conn.commit()
        row = conn.execute("SELECT player_name FROM player_name_index WHERE player_tag='#P1'").fetchone()
        conn.close()
        assert row["player_name"] == "NewName"

    def test_older_date_does_not_overwrite(self, tmp_path):
        """An older date for the same player_tag must not overwrite the stored name."""
        dm = _make_db(tmp_path)
        rows_new = [{"PlayerID": "#P1", "Player": "CurrentName", "Date": "2025-06-01T00:00", "attack_order": 1}]
        rows_old = [{"PlayerID": "#P1", "Player": "StaleOldName", "Date": "2023-01-01T00:00", "attack_order": 1}]
        conn = self._open_conn(_path(dm))
        dm._upsert_player_name_index_in_conn(conn, rows_new)
        conn.commit()
        dm._upsert_player_name_index_in_conn(conn, rows_old)
        conn.commit()
        row = conn.execute("SELECT player_name FROM player_name_index WHERE player_tag='#P1'").fetchone()
        conn.close()
        assert row["player_name"] == "CurrentName"

    def test_empty_rows_no_error(self, tmp_path):
        """Calling with an empty iterable is a no-op (no error, no rows inserted)."""
        dm = _make_db(tmp_path)
        conn = self._open_conn(_path(dm))
        dm._upsert_player_name_index_in_conn(conn, [])  # must not raise
        conn.commit()
        count = conn.execute("SELECT COUNT(*) AS cnt FROM player_name_index").fetchone()["cnt"]
        conn.close()
        assert count == 0


# ---------------------------------------------------------------------------
# Tests: load_player_name_index_sync
# ---------------------------------------------------------------------------

class TestLoadPlayerNameIndexSync:

    def _seed_index(self, db_path: str, entries: dict[str, str]) -> None:
        conn = sqlite3.connect(db_path)
        conn.executemany(
            "INSERT INTO player_name_index (player_tag, player_name, last_seen) VALUES (?, ?, '2025-01-01')",
            list(entries.items()),
        )
        conn.commit()
        conn.close()

    def test_returns_dict(self, tmp_path):
        dm = _make_db(tmp_path)
        self._seed_index(_path(dm), {"#A1": "Alice", "#B1": "Bob"})
        result = dm.load_player_name_index_sync()
        assert result == {"#A1": "Alice", "#B1": "Bob"}

    def test_empty_table_returns_empty_dict(self, tmp_path):
        dm = _make_db(tmp_path)
        result = dm.load_player_name_index_sync()
        assert result == {}

    def test_db_not_initialized_raises(self):
        dm = WarHistoryDB.__new__(WarHistoryDB)
        dm.db_path = None
        dm._pool = None
        with pytest.raises(RuntimeError, match="Database not initialized"):
            dm.load_player_name_index_sync()


# ---------------------------------------------------------------------------
# Tests: update_player_name_index_sync
# ---------------------------------------------------------------------------

class TestUpdatePlayerNameIndexSync:

    def test_upserts_new_entry(self, tmp_path):
        dm = _make_db(tmp_path)
        dm.update_player_name_index_sync([("#A1", "Alice", "2025-01-01T10:00")])
        conn = sqlite3.connect(_path(dm))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT player_name FROM player_name_index WHERE player_tag='#A1'").fetchone()
        conn.close()
        assert row["player_name"] == "Alice"

    def test_newer_date_updates_name(self, tmp_path):
        dm = _make_db(tmp_path)
        dm.update_player_name_index_sync([("#A1", "OldName", "2024-01-01T00:00")])
        dm.update_player_name_index_sync([("#A1", "NewName", "2025-06-01T00:00")])
        conn = sqlite3.connect(_path(dm))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT player_name FROM player_name_index WHERE player_tag='#A1'").fetchone()
        conn.close()
        assert row["player_name"] == "NewName"

    def test_older_date_does_not_overwrite(self, tmp_path):
        dm = _make_db(tmp_path)
        dm.update_player_name_index_sync([("#A1", "CurrentName", "2025-06-01T00:00")])
        dm.update_player_name_index_sync([("#A1", "StaleOldName", "2023-01-01T00:00")])
        conn = sqlite3.connect(_path(dm))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT player_name FROM player_name_index WHERE player_tag='#A1'").fetchone()
        conn.close()
        assert row["player_name"] == "CurrentName"

    def test_empty_updates_no_error(self, tmp_path):
        dm = _make_db(tmp_path)
        dm.update_player_name_index_sync([])  # must not raise

    def test_db_not_initialized_raises(self):
        dm = WarHistoryDB.__new__(WarHistoryDB)
        dm.db_path = None
        dm._pool = None
        with pytest.raises(RuntimeError, match="Database not initialized"):
            dm.update_player_name_index_sync([("#A1", "Alice", "2025-01-01")])
