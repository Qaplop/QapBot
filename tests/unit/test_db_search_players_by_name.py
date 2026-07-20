"""Tests for db_manager.search_players_by_name_sync.

Uses a real in-memory SQLite database with the war_attacks schema.
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

_CREATE_PLAYER_NAME_INDEX = """
    CREATE TABLE IF NOT EXISTS player_name_index (
        player_tag  TEXT PRIMARY KEY,
        player_name TEXT NOT NULL,
        last_seen   TEXT NOT NULL
    )
"""


def _make_db(tmp_path) -> WarHistoryDB:
    """Create a WarHistoryDB with war_attacks and player_name_index schema in a temp file."""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(_CREATE_WAR_ATTACKS)
    conn.execute(_CREATE_PLAYER_NAME_INDEX)
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
    war_id: str = "W1",
    clan_tag: str = "#CLAN",
    date: str = "2025-01-15T10:00",
    player_name: str = "PlayerOne",
    player_tag: str = "#P1",
    attack_order: int = 1,
    stars: int = 3,
) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT OR IGNORE INTO war_attacks
           (war_id, clan_tag, date, player_name, player_tag, th_level,
            attack_order, stars)
           VALUES (?,?,?,?,?,?,?,?)""",
        (war_id, clan_tag, date, player_name, player_tag, 15, attack_order, stars),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSearchPlayersByNameSync:

    def test_exact_name_match(self, tmp_path):
        dm = _make_db(tmp_path)
        _insert_attack(dm.db_path, player_name="JohnDoe", player_tag="#JD1")

        result = dm.search_players_by_name_sync("JohnDoe")

        assert len(result) == 1
        assert result[0]["player_tag"] == "#JD1"
        assert result[0]["player_name"] == "JohnDoe"

    def test_substring_match(self, tmp_path):
        dm = _make_db(tmp_path)
        _insert_attack(dm.db_path, player_name="JohnDoe", player_tag="#JD1")
        _insert_attack(dm.db_path, player_name="JohnSmith", player_tag="#JS1", war_id="W2")

        result = dm.search_players_by_name_sync("John")

        tags = {r["player_tag"] for r in result}
        assert "#JD1" in tags
        assert "#JS1" in tags

    def test_case_insensitive(self, tmp_path):
        dm = _make_db(tmp_path)
        _insert_attack(dm.db_path, player_name="JohnDoe", player_tag="#JD1")

        assert len(dm.search_players_by_name_sync("johndoe")) == 1
        assert len(dm.search_players_by_name_sync("JOHNDOE")) == 1
        assert len(dm.search_players_by_name_sync("John")) == 1

    def test_no_match_returns_empty(self, tmp_path):
        dm = _make_db(tmp_path)
        _insert_attack(dm.db_path, player_name="JohnDoe", player_tag="#JD1")

        result = dm.search_players_by_name_sync("XyzNoMatch")

        assert result == []

    def test_empty_table_returns_empty(self, tmp_path):
        dm = _make_db(tmp_path)

        result = dm.search_players_by_name_sync("John")

        assert result == []

    def test_sentinel_rows_included(self, tmp_path):
        """Rows with attack_order=0 (missed-all-attacks sentinels) ARE now indexed."""
        dm = _make_db(tmp_path)
        _insert_attack(dm.db_path, player_name="GhostPlayer", player_tag="#GP1", attack_order=0)

        result = dm.search_players_by_name_sync("Ghost")

        assert len(result) == 1
        assert result[0]["player_tag"] == "#GP1"

    def test_deduplicates_by_player_tag(self, tmp_path):
        """Same player appearing in multiple wars returns only one entry."""
        dm = _make_db(tmp_path)
        _insert_attack(dm.db_path, player_name="Alice", player_tag="#A1", war_id="W1", date="2025-01-01T10:00")
        _insert_attack(dm.db_path, player_name="Alice", player_tag="#A1", war_id="W2", date="2025-02-01T10:00")

        result = dm.search_players_by_name_sync("Alice")

        assert len(result) == 1
        assert result[0]["player_tag"] == "#A1"

    def test_returns_most_recent_name(self, tmp_path):
        """When a player's name changed, the most recent name is returned."""
        dm = _make_db(tmp_path)
        _insert_attack(dm.db_path, player_name="OldName", player_tag="#P1", war_id="W1", date="2025-01-01T10:00")
        _insert_attack(dm.db_path, player_name="NewName", player_tag="#P1", war_id="W2", date="2025-06-01T10:00")

        # Both "OldName" and "NewName" contain "Name" — should match
        result = dm.search_players_by_name_sync("Name")

        assert len(result) == 1
        assert result[0]["player_name"] == "NewName"

    def test_ordered_by_most_recently_seen(self, tmp_path):
        """Results ordered by most recent war date descending."""
        dm = _make_db(tmp_path)
        _insert_attack(dm.db_path, player_name="Beta", player_tag="#B1", war_id="W1", date="2025-01-01T10:00")
        _insert_attack(dm.db_path, player_name="Alpha", player_tag="#A1", war_id="W2", date="2025-06-01T10:00")

        result = dm.search_players_by_name_sync("a")  # matches both

        assert result[0]["player_tag"] == "#A1"  # more recent
        assert result[1]["player_tag"] == "#B1"

    def test_limit_respected(self, tmp_path):
        """Limit parameter caps the result count (and cannot exceed 25)."""
        dm = _make_db(tmp_path)
        for i in range(10):
            _insert_attack(
                dm.db_path,
                player_name=f"Player{i:02d}",
                player_tag=f"#P{i:02d}",
                war_id=f"W{i}",
                date=f"2025-0{(i % 9) + 1}-01T10:00",
            )

        result = dm.search_players_by_name_sync("Player", limit=3)

        assert len(result) <= 3

    def test_limit_capped_at_25(self, tmp_path):
        """Limit is silently capped at 25 (Discord select menu maximum)."""
        dm = _make_db(tmp_path)
        for i in range(30):
            _insert_attack(
                dm.db_path,
                player_name=f"Player{i:02d}",
                player_tag=f"#P{i:02d}",
                war_id=f"W{i}",
                date=f"2025-01-{(i % 28) + 1:02d}T10:00",
            )

        result = dm.search_players_by_name_sync("Player", limit=50)

        assert len(result) <= 25

    def test_result_keys(self, tmp_path):
        """Each result dict has exactly 'player_tag' and 'player_name' keys."""
        dm = _make_db(tmp_path)
        _insert_attack(dm.db_path, player_name="KeyTest", player_tag="#KT1")

        result = dm.search_players_by_name_sync("KeyTest")

        assert set(result[0].keys()) == {"player_tag", "player_name"}

    def test_db_not_initialized_raises(self):
        """Raises RuntimeError when db_path is not set."""
        dm = WarHistoryDB.__new__(WarHistoryDB)
        dm.db_path = None
        dm._pool = None

        with pytest.raises(RuntimeError, match="Database not initialized"):
            dm.search_players_by_name_sync("anything")


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
        conn = self._open_conn(dm.db_path)
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
        conn = self._open_conn(dm.db_path)
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
        conn = self._open_conn(dm.db_path)
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
        conn = self._open_conn(dm.db_path)
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
        conn = self._open_conn(dm.db_path)
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
        conn = self._open_conn(dm.db_path)
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
        self._seed_index(dm.db_path, {"#A1": "Alice", "#B1": "Bob"})
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
        conn = sqlite3.connect(dm.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT player_name FROM player_name_index WHERE player_tag='#A1'").fetchone()
        conn.close()
        assert row["player_name"] == "Alice"

    def test_newer_date_updates_name(self, tmp_path):
        dm = _make_db(tmp_path)
        dm.update_player_name_index_sync([("#A1", "OldName", "2024-01-01T00:00")])
        dm.update_player_name_index_sync([("#A1", "NewName", "2025-06-01T00:00")])
        conn = sqlite3.connect(dm.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT player_name FROM player_name_index WHERE player_tag='#A1'").fetchone()
        conn.close()
        assert row["player_name"] == "NewName"

    def test_older_date_does_not_overwrite(self, tmp_path):
        dm = _make_db(tmp_path)
        dm.update_player_name_index_sync([("#A1", "CurrentName", "2025-06-01T00:00")])
        dm.update_player_name_index_sync([("#A1", "StaleOldName", "2023-01-01T00:00")])
        conn = sqlite3.connect(dm.db_path)
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


# ---------------------------------------------------------------------------
# Tests: CacheManager.search_player_names (in-memory)
# ---------------------------------------------------------------------------

class TestSearchPlayerNames:
    """Tests for the synchronous in-memory search over CACHE.player_name_index."""

    def _make_cache(self, index: dict[str, str]):
        """Build a minimal stand-in for CACHE with a populated player_name_index."""
        from qapbot.cache_manager import CacheManager
        cache = CacheManager.__new__(CacheManager)
        cache.player_name_index = index
        return cache

    def test_substring_match(self):
        cache = self._make_cache({"#A1": "AliceWonder", "#B1": "Bobby"})
        result = cache.search_player_names("alice")
        assert len(result) == 1
        assert result[0]["player_tag"] == "#A1"

    def test_case_insensitive(self):
        cache = self._make_cache({"#A1": "AliceWonder"})
        assert len(cache.search_player_names("ALICE")) == 1
        assert len(cache.search_player_names("alice")) == 1
        assert len(cache.search_player_names("Wonder")) == 1

    def test_no_match_returns_empty(self):
        cache = self._make_cache({"#A1": "Alice"})
        assert cache.search_player_names("NoMatch") == []

    def test_empty_query_returns_empty(self):
        cache = self._make_cache({"#A1": "Alice"})
        assert cache.search_player_names("") == []

    def test_sorted_alphabetically(self):
        cache = self._make_cache({"#Z1": "Zebra", "#A1": "Apple", "#M1": "Mango"})
        result = cache.search_player_names("a")  # matches all three
        names = [r["player_name"] for r in result]
        assert names == sorted(names, key=str.lower)

    def test_limit_cap(self):
        """Results capped at min(limit, 25)."""
        index = {f"#{i}": f"Player{i:03d}" for i in range(30)}
        cache = self._make_cache(index)
        assert len(cache.search_player_names("Player", limit=5)) == 5
        assert len(cache.search_player_names("Player", limit=50)) == 25

    def test_result_keys(self):
        cache = self._make_cache({"#A1": "Alice"})
        result = cache.search_player_names("Alice")
        assert set(result[0].keys()) == {"player_tag", "player_name"}

    def test_empty_index_returns_empty(self):
        cache = self._make_cache({})
        assert cache.search_player_names("anything") == []

