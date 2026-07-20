"""Tests for db_manager sync functions — Phase 4 coverage.

Uses real in-memory SQLite databases for accurate testing.
Covers: add_war_records_sync, get_clan_history_sync.
"""
# pyright: reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownArgumentType=false, reportUnusedImport=false
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

import pytest

from qapbot.db_manager import WarHistoryDB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_schema(conn: sqlite3.Connection) -> None:
    """Create the war_history table in an in-memory database."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS war_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            war_id TEXT NOT NULL,
            clan_tag TEXT NOT NULL,
            date TEXT NOT NULL,
            player_name TEXT NOT NULL,
            player_tag TEXT NOT NULL,
            th_level INTEGER NOT NULL,
            stars INTEGER NOT NULL,
            attacks INTEGER NOT NULL,
            missed_attacks INTEGER NOT NULL,
            max_attacks INTEGER NOT NULL,
            defensive_stars INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(war_id, player_tag)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_clan_tag ON war_history(clan_tag)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_date ON war_history(date)")
    conn.commit()


def _make_record(
    war_id: str = "W1",
    player: str = "Alice",
    player_tag: str = "#P1",
    date: str = "2025-01-15T10:00",
    th_level: int = 15,
    stars: int = 3,
    attacks: int = 1,
    missed: int = 0,
    max_attacks: int = 2,
    def_stars: int = 1,
) -> Dict[str, Any]:
    return {
        "WarID": war_id,
        "Player": player,
        "PlayerID": player_tag,
        "Date": date,
        "TH_lvl": th_level,
        "Stars": stars,
        "Attacks": attacks,
        "Missed_Attacks": missed,
        "Max_Attacks": max_attacks,
        "Defensive_Stars": def_stars,
    }


def _db_with_path(tmp_path) -> WarHistoryDB:
    """Create a WarHistoryDB pointed at a temp file with schema initialized."""
    db = WarHistoryDB()
    db_file = str(tmp_path / "test.db")
    db.db_path = db_file
    # Create schema manually
    conn = sqlite3.connect(db_file)
    _create_schema(conn)
    conn.close()
    return db


# ---------------------------------------------------------------------------
# add_war_records_sync
# ---------------------------------------------------------------------------

