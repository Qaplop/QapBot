"""
Tests for WarHistoryDB.fast_bulk_history_migration(), added 2026-08-01 after the
normal batched run_history_migration() path proved far slower than raw disk
I/O should allow (~900-1000 rows/sec on SSD for what was only ~10 GB of data) —
root-caused to per-row secondary-index maintenance (6 B-tree updates per row on
each side of the move). See qapbot/docs/DATABASE_ARCHITECTURE.md's Migration
History for the incident writeup.

Covers the properties that matter most for a "temporarily drop indexes, bulk
move, rebuild indexes" operation:
  1. Correctness across a chunk boundary (small chunk_size forces multiple
     chunks — the boundary-id/OFFSET pagination logic must not drop or
     duplicate rows at the edges).
  2. Secondary indexes are actually dropped during the run and fully rebuilt
     afterward on both schemas.
  3. Indexes are rebuilt even when the migration itself errors out partway —
     a bot must never come back with main.war_attacks missing query indexes.
  4. Re-running after a full completion is a safe no-op (idempotent, no
     duplicate rows, no error).
"""
import pytest

from qapbot.db_manager import WarHistoryDB
import qapbot.db_manager as db_manager_module

_SECONDARY_INDEX_NAMES = {
    "idx_wa_player_tag",
    "idx_wa_war_clan",
    "idx_wa_clan_date",
    "idx_wa_zero_attacks",
    "idx_wa_player_tag_date",
}


async def _index_names(db: WarHistoryDB, schema: str) -> set:
    table = f"{schema}.sqlite_master" if schema else "sqlite_master"
    cur = await db._conn.execute(
        f"SELECT name FROM {table} WHERE type='index' AND tbl_name='war_attacks'"
    )
    return {r["name"] for r in await cur.fetchall()}


@pytest.mark.asyncio
class TestFastBulkHistoryMigration:
    async def _insert_old_war_attacks(self, db: WarHistoryDB, count: int) -> None:
        for i in range(count):
            await db._conn.execute(
                "INSERT INTO main.war_attacks "
                "(war_id, clan_tag, date, player_name, player_tag, th_level, stars) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"war_{i}", "#CLAN", "2020-01-01", f"Player{i}", f"#TAG{i}", 10, 3),
            )
        await db._conn.commit()

    async def test_moves_all_rows_across_chunk_boundaries(self, tmp_path):
        db = WarHistoryDB()
        db_path = str(tmp_path / "fast_test.db")
        history_db_path = str(tmp_path / "fast_test_history.db")
        await db.initialize(db_path, history_db_path)
        try:
            await self._insert_old_war_attacks(db, 10)

            # chunk_size=3 forces 4 chunks (3,3,3,1) for 10 rows — exercises the
            # boundary-id OFFSET pagination across multiple non-even chunks.
            result = await db.fast_bulk_history_migration(chunk_size=3)

            assert result.startswith("[HIST-MIGRATE-FAST] cutoff="), result
            assert "ERROR" not in result

            cur = await db._conn.execute("SELECT COUNT(*) AS cnt FROM main.war_attacks")
            assert (await cur.fetchone())["cnt"] == 0

            cur = await db._conn.execute("SELECT COUNT(*) AS cnt FROM history.war_attacks")
            assert (await cur.fetchone())["cnt"] == 10

            # No duplicates and no gaps: every original war_id present exactly once.
            cur = await db._conn.execute("SELECT war_id FROM history.war_attacks ORDER BY war_id")
            war_ids = [r["war_id"] for r in await cur.fetchall()]
            assert war_ids == [f"war_{i}" for i in range(10)]
        finally:
            if db.conn:
                await db.conn.close()

    async def test_indexes_dropped_during_and_rebuilt_after(self, tmp_path, monkeypatch):
        db = WarHistoryDB()
        db_path = str(tmp_path / "idx_test.db")
        history_db_path = str(tmp_path / "idx_test_history.db")
        await db.initialize(db_path, history_db_path)
        try:
            await self._insert_old_war_attacks(db, 5)

            # Confirm indexes exist before the run (sanity check on the fixture).
            assert _SECONDARY_INDEX_NAMES <= await _index_names(db, "")
            assert _SECONDARY_INDEX_NAMES <= await _index_names(db, "history")

            # Snapshot mid-run by monkeypatching _bulk_move_chunk to check indexes
            # are gone on the FIRST call, then delegate to the real implementation.
            real_chunk_fn = db_manager_module.WarHistoryDB._bulk_move_chunk
            seen = {}

            async def _spy_chunk(self, table, cutoff_date, chunk_size):
                if "checked" not in seen:
                    seen["checked"] = True
                    assert not (_SECONDARY_INDEX_NAMES & await _index_names(self, "")), \
                        "main indexes should be dropped mid-run"
                    assert not (_SECONDARY_INDEX_NAMES & await _index_names(self, "history")), \
                        "history indexes should be dropped mid-run"
                return await real_chunk_fn(self, table, cutoff_date, chunk_size)

            monkeypatch.setattr(db_manager_module.WarHistoryDB, "_bulk_move_chunk", _spy_chunk)

            result = await db.fast_bulk_history_migration(chunk_size=1_000_000)
            assert "ERROR" not in result
            assert seen.get("checked"), "expected the chunk spy to run at least once"

            # Rebuilt on both schemas afterward.
            assert _SECONDARY_INDEX_NAMES <= await _index_names(db, "")
            assert _SECONDARY_INDEX_NAMES <= await _index_names(db, "history")
        finally:
            if db.conn:
                await db.conn.close()

    async def test_indexes_rebuilt_even_when_migration_errors(self, tmp_path, monkeypatch):
        db = WarHistoryDB()
        db_path = str(tmp_path / "err_test.db")
        history_db_path = str(tmp_path / "err_test_history.db")
        await db.initialize(db_path, history_db_path)
        try:
            await self._insert_old_war_attacks(db, 5)

            async def _boom(self, table, cutoff_date, chunk_size):
                raise RuntimeError("simulated failure mid-migration")

            monkeypatch.setattr(db_manager_module.WarHistoryDB, "_bulk_move_chunk", _boom)

            result = await db.fast_bulk_history_migration(chunk_size=1_000_000)
            assert result.startswith("[HIST-MIGRATE-FAST] ERROR"), result

            # Even on failure, indexes must be back — a bot restart must never see
            # main.war_attacks missing its query indexes.
            assert _SECONDARY_INDEX_NAMES <= await _index_names(db, "")
            assert _SECONDARY_INDEX_NAMES <= await _index_names(db, "history")

            # Failed run must not be marked done.
            stamp = await db.get_bot_metadata(db.HISTORY_MIGRATION_CUTOFF_KEY)
            assert stamp is None
        finally:
            if db.conn:
                await db.conn.close()

    async def test_rerun_after_completion_is_idempotent_noop(self, tmp_path):
        db = WarHistoryDB()
        db_path = str(tmp_path / "idempotent_test.db")
        history_db_path = str(tmp_path / "idempotent_test_history.db")
        await db.initialize(db_path, history_db_path)
        try:
            await self._insert_old_war_attacks(db, 5)

            first = await db.fast_bulk_history_migration(chunk_size=1_000_000)
            assert "ERROR" not in first

            second = await db.fast_bulk_history_migration(chunk_size=1_000_000)
            assert "ERROR" not in second
            assert "war_attacks=0" in second

            cur = await db._conn.execute("SELECT COUNT(*) AS cnt FROM history.war_attacks")
            assert (await cur.fetchone())["cnt"] == 5  # no duplicates from the second run
        finally:
            if db.conn:
                await db.conn.close()
