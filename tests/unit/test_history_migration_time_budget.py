"""
Tests for monthly_history_migration()'s time-budget/chunking support, added
2026-08-01 after a first-ever migration against a large backlog needed 10+
hours — far too long to block a live bot's Discord commands on in one
sitting. See qapbot/docs/DATABASE_ARCHITECTURE.md's Migration History for the
incident writeup.

Covers the two behaviors that matter most for safe chunking:
  1. A time-budgeted run that doesn't finish reports PARTIAL (not an error)
     and does NOT persist last_history_migration as done — this is the exact
     bug fixed alongside the time-budget feature (a naive `not
     result.startswith("ERROR")` check would have wrongly marked a PARTIAL
     run as done, since PARTIAL isn't an error string).
  2. Re-running without a deadline picks up exactly where the partial run
     left off and completes, at which point the timestamp IS persisted.
"""
import pytest

from qapbot.db_manager import WarHistoryDB
import qapbot.db_manager as db_manager_module


@pytest.mark.asyncio
class TestHistoryMigrationTimeBudget:
    async def _insert_old_war_attacks(self, db: WarHistoryDB, count: int) -> None:
        """Insert `count` main.war_attacks rows dated before any realistic cutoff."""
        for i in range(count):
            await db._conn.execute(
                "INSERT INTO main.war_attacks "
                "(war_id, clan_tag, date, player_name, player_tag, th_level, stars) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"war_{i}", "#CLAN", "2020-01-01", f"Player{i}", f"#TAG{i}", 10, 3),
            )
        await db._conn.commit()

    async def test_partial_run_not_marked_done(self, tmp_path, monkeypatch):
        db = WarHistoryDB()
        db_path = str(tmp_path / "budget_test.db")
        history_db_path = str(tmp_path / "budget_test_history.db")
        await db.initialize(db_path, history_db_path)
        try:
            await self._insert_old_war_attacks(db, 10)

            # Fake clock: each call to monotonic() advances by 1.0s. With
            # time_budget_seconds=2.5 and batch_size=3, two 3-row batches fit
            # before the deadline trips on the third loop iteration's check —
            # deterministic regardless of real execution speed.
            clock = {"t": 0.0}

            def fake_monotonic():
                clock["t"] += 1.0
                return clock["t"]

            monkeypatch.setattr(db_manager_module, "_monotonic", fake_monotonic)

            result = await db.monthly_history_migration(batch_size=3, time_budget_seconds=2.5)

            assert result.startswith("[HIST-MIGRATE] PARTIAL"), result
            assert "ERROR" not in result

            # The core bug this feature could have reintroduced: a partial run
            # must NOT be persisted as done, or is_monthly_migration_due()
            # would silently skip retrying for the rest of the month.
            stamp = await db.get_bot_metadata("last_history_migration")
            assert stamp is None, "partial run must not mark the migration as done"

            # Real progress happened (some rows moved), but not all of them.
            cur = await db._conn.execute("SELECT COUNT(*) AS cnt FROM main.war_attacks")
            remaining = (await cur.fetchone())["cnt"]
            assert 0 < remaining < 10, f"expected partial progress, got {remaining} remaining"
        finally:
            if db.conn:
                await db.conn.close()

    async def test_resumed_run_completes_and_marks_done(self, tmp_path, monkeypatch):
        db = WarHistoryDB()
        db_path = str(tmp_path / "resume_test.db")
        history_db_path = str(tmp_path / "resume_test_history.db")
        await db.initialize(db_path, history_db_path)
        try:
            await self._insert_old_war_attacks(db, 10)

            clock = {"t": 0.0}

            def fake_monotonic():
                clock["t"] += 1.0
                return clock["t"]

            monkeypatch.setattr(db_manager_module, "_monotonic", fake_monotonic)
            partial_result = await db.monthly_history_migration(batch_size=3, time_budget_seconds=2.5)
            assert partial_result.startswith("[HIST-MIGRATE] PARTIAL")

            monkeypatch.undo()  # restore real time.monotonic for the completing run

            final_result = await db.monthly_history_migration(batch_size=3)
            assert final_result.startswith("[HIST-MIGRATE] cutoff="), final_result
            assert "PARTIAL" not in final_result
            assert "ERROR" not in final_result

            stamp = await db.get_bot_metadata("last_history_migration")
            assert stamp is not None, "a fully-completed run must persist the done marker"

            cur = await db._conn.execute("SELECT COUNT(*) AS cnt FROM main.war_attacks")
            assert (await cur.fetchone())["cnt"] == 0

            cur = await db._conn.execute("SELECT COUNT(*) AS cnt FROM history.war_attacks")
            assert (await cur.fetchone())["cnt"] == 10
        finally:
            if db.conn:
                await db.conn.close()

    async def test_checkpoint_every_batches_override_accepted(self, tmp_path):
        """checkpoint_every_batches is a plain pass-through — just confirm it
        doesn't break a normal full run when overridden (e.g. a recovery run
        raising it well above the class default to cut checkpoint overhead)."""
        db = WarHistoryDB()
        db_path = str(tmp_path / "ckpt_override_test.db")
        history_db_path = str(tmp_path / "ckpt_override_test_history.db")
        await db.initialize(db_path, history_db_path)
        try:
            await self._insert_old_war_attacks(db, 5)
            result = await db.monthly_history_migration(batch_size=2, checkpoint_every_batches=1000)
            assert result.startswith("[HIST-MIGRATE] cutoff="), result
            cur = await db._conn.execute("SELECT COUNT(*) AS cnt FROM history.war_attacks")
            assert (await cur.fetchone())["cnt"] == 5
        finally:
            if db.conn:
                await db.conn.close()
