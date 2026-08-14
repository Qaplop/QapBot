"""End-to-end tests for qapbot/scripts/repair_history_schema_drift.py — the repair tool for the
2026-08-14 hot/history schema-drift incident (Cardinal Rule 1). See
tests/unit/test_history_migration_column_alignment.py for the migration-code-level regression
test; this file tests the standalone repair script itself against a synthetically drifted DB.
"""
import importlib.util
import os
import sys

import pytest

from qapbot.db_manager import WarHistoryDB

_SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "qapbot", "scripts", "repair_history_schema_drift.py")
_spec = importlib.util.spec_from_file_location("repair_history_schema_drift", _SCRIPT_PATH)
repair_module = importlib.util.module_from_spec(_spec)
sys.modules["repair_history_schema_drift"] = repair_module
_spec.loader.exec_module(repair_module)


async def _rebuild_history_war_summary_with_swapped_columns(db: WarHistoryDB) -> None:
    """Same synthetic drift as test_history_migration_column_alignment.py: war_tag/end_time
    swapped relative to main's physical column order."""
    await db._conn.execute("DROP TABLE history.war_summary")
    await db._conn.execute("""
        CREATE TABLE history.war_summary (
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
            end_time             TEXT    NOT NULL DEFAULT '',
            war_tag              TEXT    NOT NULL DEFAULT '',
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
    """)
    await db._conn.execute("CREATE INDEX IF NOT EXISTS history.idx_ws_clan_tag ON war_summary(clan_tag)")
    await db._conn.commit()


async def _seed_corrupted_row(
    db: WarHistoryDB, war_id: str, real_war_tag: str, real_end_time: str,
    result: str = "win", cwl_season: str = "2020-01",
) -> None:
    """Insert directly into history.war_summary using the CURRENT (drifted) schema's own column
    labels, simulating exactly what the old buggy positional SELECT * would have produced: the
    value that truly belongs under `war_tag` lands under history's `end_time` label (since that's
    what physically sits at war_tag's position in main), and vice versa. `result`/`date` are NOT
    swapped by this test's simplified 2-column drift (see
    _rebuild_history_war_summary_with_swapped_columns), so they're written under their own,
    correct, unshifted labels directly."""
    await db._conn.execute(
        "INSERT INTO history.war_summary "
        "(war_id, clan_tag, opponent_tag, date, is_cwl, cwl_season, end_time, war_tag, result) "
        "VALUES (?, '#CLAN', '#OPP', '2020-01-01 00:00:00', 1, ?, ?, ?, ?)",
        (war_id, cwl_season, real_war_tag, real_end_time, result),
    )
    await db._conn.commit()


@pytest.mark.asyncio
class TestRepairHistorySchemaDrift:
    async def test_dry_run_reports_but_does_not_persist(self, tmp_path):
        db = WarHistoryDB()
        db_path = str(tmp_path / "dry.db")
        history_db_path = str(tmp_path / "dry_history.db")
        await db.initialize(db_path, history_db_path)
        try:
            await _rebuild_history_war_summary_with_swapped_columns(db)
            await _seed_corrupted_row(db, "war_1", real_war_tag="#REALTAG1", real_end_time="2020-01-01T20:00")
            await db.close()

            ok = await repair_module._run(db_path, history_db_path, apply=False, sample_size=5, force=False)
            assert ok is True

            # Dry run must leave the file completely unchanged — no _repaired/_corrupted_backup
            # tables, and the original row still shows the corrupted (swapped) raw values.
            db2 = WarHistoryDB()
            await db2.initialize(db_path, history_db_path)
            try:
                cur = await db2._conn.execute(
                    "SELECT name FROM history.sqlite_master WHERE type='table' AND name LIKE 'war_summary%'"
                )
                names = {r["name"] for r in await cur.fetchall()}
                assert names == {"war_summary"}, f"dry run left extra tables behind: {names}"

                cur = await db2._conn.execute("SELECT war_tag, end_time FROM history.war_summary WHERE war_id='war_1'")
                row = await cur.fetchone()
                # Still corrupted — dry run changed nothing: war_tag holds the end_time value
                # and vice versa, exactly as seeded.
                assert row["war_tag"] == "2020-01-01T20:00"
                assert row["end_time"] == "#REALTAG1"
            finally:
                await db2.close()
        finally:
            if db.conn:
                await db.conn.close()

    async def test_apply_repairs_and_preserves_backup(self, tmp_path):
        db = WarHistoryDB()
        db_path = str(tmp_path / "apply.db")
        history_db_path = str(tmp_path / "apply_history.db")
        await db.initialize(db_path, history_db_path)
        try:
            await _rebuild_history_war_summary_with_swapped_columns(db)
            await _seed_corrupted_row(db, "war_1", real_war_tag="#REALTAG1", real_end_time="2020-01-01T20:00")
            await _seed_corrupted_row(db, "war_2", real_war_tag="#REALTAG2", real_end_time="2020-02-01T20:00")
            await db.close()

            ok = await repair_module._run(db_path, history_db_path, apply=True, sample_size=5, force=False)
            assert ok is True

            db2 = WarHistoryDB()
            await db2.initialize(db_path, history_db_path)
            try:
                # Repaired: war_summary now has the CORRECT values under the correct names.
                cur = await db2._conn.execute("SELECT war_tag, end_time FROM history.war_summary WHERE war_id='war_1'")
                row = await cur.fetchone()
                assert row["war_tag"] == "#REALTAG1"
                assert row["end_time"] == "2020-01-01T20:00"

                cur = await db2._conn.execute("SELECT war_tag, end_time FROM history.war_summary WHERE war_id='war_2'")
                row = await cur.fetchone()
                assert row["war_tag"] == "#REALTAG2"
                assert row["end_time"] == "2020-02-01T20:00"

                # Original preserved (still corrupted, as a fallback reference) under _corrupted_backup.
                cur = await db2._conn.execute(
                    "SELECT war_tag, end_time FROM history.war_summary_corrupted_backup WHERE war_id='war_1'"
                )
                backup_row = await cur.fetchone()
                assert backup_row["war_tag"] == "2020-01-01T20:00"
                assert backup_row["end_time"] == "#REALTAG1"

                # No leftover _repaired table.
                cur = await db2._conn.execute(
                    "SELECT name FROM history.sqlite_master WHERE type='table' AND name LIKE 'war_summary%'"
                )
                names = {r["name"] for r in await cur.fetchall()}
                assert names == {"war_summary", "war_summary_corrupted_backup"}

                # The original named index survived the swap, now on the real (repaired) table.
                cur = await db2._conn.execute(
                    "SELECT name, tbl_name FROM history.sqlite_master WHERE type='index' AND name='idx_ws_clan_tag'"
                )
                idx_row = await cur.fetchone()
                assert idx_row is not None
                assert idx_row["tbl_name"] == "war_summary"
            finally:
                await db2.close()
        finally:
            if db.conn:
                await db.conn.close()

    async def test_apply_refuses_to_redo_without_force(self, tmp_path):
        db = WarHistoryDB()
        db_path = str(tmp_path / "redo.db")
        history_db_path = str(tmp_path / "redo_history.db")
        await db.initialize(db_path, history_db_path)
        try:
            await _rebuild_history_war_summary_with_swapped_columns(db)
            await _seed_corrupted_row(db, "war_1", real_war_tag="#REALTAG1", real_end_time="2020-01-01T20:00")
            await db.close()

            ok = await repair_module._run(db_path, history_db_path, apply=True, sample_size=5, force=False)
            assert ok is True

            with pytest.raises(RuntimeError, match="already exists"):
                await repair_module._run(db_path, history_db_path, apply=True, sample_size=5, force=False)
        finally:
            if db.conn:
                await db.conn.close()

    async def test_apply_repairs_full_real_world_war_summary_drift(self, tmp_path):
        """Uses the EXACT column order found in the real 2026-08-14 incident (all 11 shifted
        columns, not the simplified 2-column swap the other tests use) to remove any doubt that
        the generic positional-remap algorithm handles the real shape of corruption, not just a
        toy case."""
        db = WarHistoryDB()
        db_path = str(tmp_path / "real_shape.db")
        history_db_path = str(tmp_path / "real_shape_history.db")
        await db.initialize(db_path, history_db_path)
        try:
            await db._conn.execute("DROP TABLE history.war_summary")
            # Real incident's history.war_summary column order, verbatim (DATABASE_ARCHITECTURE.md
            # "Incident: silent column-order divergence..." table).
            await db._conn.execute("""
                CREATE TABLE history.war_summary (
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
            """)
            await db._conn.commit()

            # Insert a row the way the OLD buggy migration actually would have: main's row
            # (in main's true column order) SELECT *'d positionally into history's (differently
            # ordered) columns. This mirrors main's real column order exactly.
            main_true_values = {
                "war_id": "war_real_1", "clan_tag": "#CLAN", "opponent_tag": "#OPP",
                "opponent_name": "Enemy", "clan_stars": 40, "opponent_stars": 35,
                "clan_destruction": 94.5, "opp_destruction": 88.0, "team_size": 15,
                "attacks_per_member": 1, "war_type": "cwl", "is_cwl": 1, "cwl_season": "2026-01",
                "result": "win", "date": "2026-01-03T21:27", "clan_lineup_json": "[18,18,18]",
                "opp_lineup_json": "[17,17,17]", "created_at": "2026-01-04T09:00",
                "clan_attacks_used": 15, "opp_attacks_used": 14, "war_tag": "#8LQPRYJ20",
                "end_time": "2026-01-04T21:27", "state": "war_ended", "round_number": 3,
            }
            main_cols = await repair_module._column_order(db._conn, "main", "war_summary")
            main_cols_no_id = [c for c in main_cols if c != "id"]
            history_cols = await repair_module._column_order(db._conn, "history", "war_summary")
            history_cols_no_id = [c for c in history_cols if c != "id"]
            # Positional SELECT * simulation: value at main position k lands at history position k.
            placeholders = ", ".join("?" for _ in history_cols_no_id)
            await db._conn.execute(
                f"INSERT INTO history.war_summary ({', '.join(history_cols_no_id)}) VALUES ({placeholders})",
                [main_true_values[c] for c in main_cols_no_id],
            )
            await db._conn.commit()
            await db.close()

            ok = await repair_module._run(db_path, history_db_path, apply=True, sample_size=5, force=False)
            assert ok is True

            db2 = WarHistoryDB()
            await db2.initialize(db_path, history_db_path)
            try:
                cur = await db2._conn.execute(
                    "SELECT " + ", ".join(main_cols_no_id) + " FROM history.war_summary WHERE war_id='war_real_1'"
                )
                row = await cur.fetchone()
                for col, expected in main_true_values.items():
                    assert row[col] == expected, f"column {col}: expected {expected!r}, got {row[col]!r}"
            finally:
                await db2.close()
        finally:
            if db.conn:
                await db.conn.close()

    async def test_no_drift_table_is_a_clean_noop(self, tmp_path):
        """A freshly-initialized DB has identical main/history schemas — nothing to repair,
        must not create or touch anything."""
        db = WarHistoryDB()
        db_path = str(tmp_path / "clean.db")
        history_db_path = str(tmp_path / "clean_history.db")
        await db.initialize(db_path, history_db_path)
        try:
            await db.close()

            ok = await repair_module._run(db_path, history_db_path, apply=True, sample_size=5, force=False)
            assert ok is True

            db2 = WarHistoryDB()
            await db2.initialize(db_path, history_db_path)
            try:
                cur = await db2._conn.execute(
                    "SELECT name FROM history.sqlite_master WHERE type='table' AND "
                    "(name LIKE '%_repaired' OR name LIKE '%_corrupted_backup')"
                )
                assert await cur.fetchall() == []
            finally:
                await db2.close()
        finally:
            if db.conn:
                await db.conn.close()
