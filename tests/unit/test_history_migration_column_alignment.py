"""Regression test for the 2026-08-14 hot/history schema-drift incident (Cardinal Rule 1,
.github/copilot-instructions.md; full writeup in DATABASE_ARCHITECTURE.md's "Hot/History DB
Split" section).

`main.war_attacks`/`war_summary` and their `history.*` counterparts had silently diverged in
physical column order over time (separate `ALTER TABLE ADD COLUMN` histories), and the migration
code used a bare `SELECT *`, which matches columns by position, not name — so every migrated row
landed its trailing ~11/~15 columns in the wrong destination column, with no error, for the
entire lifetime of the history DB.

The fix (`_explicit_column_list()`, used by both `_migrate_date_window_batched` and
`_migrate_cwl_table_by_season`) reads column names from `main`'s own schema and lists them
explicitly on both sides of `INSERT ... SELECT`. This test proves that fix actually survives a
genuine column-order divergence between the two schemas — a `SELECT *` implementation would fail
it (silently write `war_tag`'s value into `end_time` and vice versa).
"""
import pytest

from qapbot.db_manager import WarHistoryDB


@pytest.mark.asyncio
class TestHistoryMigrationSurvivesColumnDrift:
    async def _rebuild_history_war_summary_with_swapped_columns(self, db: WarHistoryDB) -> None:
        """Recreates history.war_summary with war_tag/end_time swapped relative to main's
        physical column order — simulating the exact class of drift the real incident had
        (columns added via divergent ALTER TABLE sequences), without needing years of migration
        history to reproduce it."""
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
        await db._conn.commit()

    async def test_migration_preserves_correct_columns_despite_schema_drift(self, tmp_path):
        db = WarHistoryDB()
        db_path = str(tmp_path / "drift_test.db")
        history_db_path = str(tmp_path / "drift_test_history.db")
        await db.initialize(db_path, history_db_path)
        try:
            await self._rebuild_history_war_summary_with_swapped_columns(db)

            await db._conn.execute(
                "INSERT INTO main.war_summary "
                "(war_id, clan_tag, opponent_tag, date, war_tag, end_time, is_cwl, cwl_season) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("war_1", "#CLAN", "#OPP", "2020-01-01", "#REALWARTAG1", "2020-01-01T20:00", 1, "2020-01"),
            )
            await db._conn.commit()

            result = await db.run_history_migration(batch_size=10)
            assert "ERROR" not in result

            cur = await db._conn.execute(
                "SELECT war_tag, end_time FROM history.war_summary WHERE war_id = 'war_1'"
            )
            row = await cur.fetchone()
            assert row is not None, "row did not migrate at all"
            # The whole point: even though history.war_summary's physical layout has war_tag
            # and end_time swapped relative to main, the values must land under the correct
            # NAME, not the correct position.
            assert row["war_tag"] == "#REALWARTAG1"
            assert row["end_time"] == "2020-01-01T20:00"
        finally:
            if db.conn:
                await db.conn.close()

    async def test_cwl_season_migration_preserves_correct_columns_despite_schema_drift(self, tmp_path):
        """Same proof for _migrate_cwl_table_by_season's path (cwl_league_groups/rounds today,
        but the helper is generic — this exercises war_summary's season-keyed sibling path via
        the same rebuilt-with-drift table to keep the test self-contained)."""
        db = WarHistoryDB()
        db_path = str(tmp_path / "drift_season_test.db")
        history_db_path = str(tmp_path / "drift_season_test_history.db")
        await db.initialize(db_path, history_db_path)
        try:
            await self._rebuild_history_war_summary_with_swapped_columns(db)

            await db._conn.execute(
                "INSERT INTO main.war_summary "
                "(war_id, clan_tag, opponent_tag, date, war_tag, end_time, is_cwl, cwl_season) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("war_2", "#CLAN", "#OPP", "2020-02-01", "#REALWARTAG2", "2020-02-01T20:00", 1, "2020-02"),
            )
            await db._conn.commit()

            moved, completed = await db._migrate_cwl_table_by_season("war_summary", cutoff_month="2099-01")
            assert completed is True
            assert moved == 1

            cur = await db._conn.execute(
                "SELECT war_tag, end_time FROM history.war_summary WHERE war_id = 'war_2'"
            )
            row = await cur.fetchone()
            assert row is not None
            assert row["war_tag"] == "#REALWARTAG2"
            assert row["end_time"] == "2020-02-01T20:00"
        finally:
            if db.conn:
                await db.conn.close()
