"""
Tests for run_history_migration()'s budget/resume behaviour.

Successor to test_history_migration_time_budget.py (2026-08-01), rewritten for the
2026-09-01 rolling redesign. The old monthly job moved "everything below the first of
last month" in one campaign; this walks the cutoff forward one calendar day at a time
under a row budget (primary) and a time budget (secondary hard stop).

What matters here, and why:

  1. A bounded run reports PARTIAL and must NOT persist a cutoff it did not actually
     reach. The monthly version had exactly this bug class — a naive
     `not result.startswith("ERROR")` check treated a PARTIAL run as done and silently
     suppressed retries for the rest of the month (2026-08-01 incident).
  2. Re-running resumes and completes, at which point the reached cutoff IS persisted.
  3. The reached-cutoff marker is what makes resume cheap — it is written after each
     COMPLETED day, never mid-day.
  4. Empty calendar days are skipped via an indexed MIN() probe rather than iterated.
     Without that, one stray ancient row makes every nightly run crawl day by day from
     that row to today.
"""
import pytest

from qapbot.db_manager import WarHistoryDB
import qapbot.db_manager as db_manager_module


@pytest.mark.asyncio
class TestHistoryMigrationBudgets:
    async def _insert_old_war_attacks(
        self, db: WarHistoryDB, count: int, date: str = "2020-01-01", offset: int = 0
    ) -> None:
        """Insert `count` main.war_attacks rows dated before any realistic cutoff."""
        for i in range(offset, offset + count):
            await db._conn.execute(
                "INSERT INTO main.war_attacks "
                "(war_id, clan_tag, date, player_name, player_tag, th_level, stars) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"war_{i}", "#CLAN", date, f"Player{i}", f"#TAG{i}", 10, 3),
            )
        await db._conn.commit()

    async def _open(self, tmp_path, name: str) -> WarHistoryDB:
        db = WarHistoryDB()
        await db.initialize(str(tmp_path / f"{name}.db"), str(tmp_path / f"{name}_history.db"))
        return db

    async def test_row_budget_stops_run_and_does_not_persist_target_cutoff(self, tmp_path):
        db = await self._open(tmp_path, "rowbudget")
        try:
            # Two days of data, so the budget can stop the walk cleanly between days.
            await self._insert_old_war_attacks(db, 6, date="2020-01-01")
            await self._insert_old_war_attacks(db, 6, date="2020-01-02", offset=100)

            result = await db.run_history_migration(batch_size=3, row_budget=6)

            assert result.startswith("[HIST-MIGRATE] PARTIAL"), result
            assert "ERROR" not in result

            # Day 1 completed, so its cutoff (the NEXT day) is persisted — but the target
            # cutoff must not be, or the next run would skip the remaining work.
            reached = await db.get_history_migration_cutoff_reached()
            target, _ = db._history_cutoff()
            assert reached == "2020-01-02"
            assert reached < target

            cur = await db._conn.execute("SELECT COUNT(*) AS cnt FROM main.war_attacks")
            assert (await cur.fetchone())["cnt"] == 6
        finally:
            if db.conn:
                await db.conn.close()

    async def test_time_budget_stops_run(self, tmp_path, monkeypatch):
        db = await self._open(tmp_path, "timebudget")
        try:
            await self._insert_old_war_attacks(db, 6, date="2020-01-01")
            await self._insert_old_war_attacks(db, 6, date="2020-01-02", offset=100)

            # Fake clock: every monotonic() call advances 1s, so the deadline trips
            # deterministically regardless of real execution speed.
            clock = {"t": 0.0}

            def fake_monotonic():
                clock["t"] += 1.0
                return clock["t"]

            monkeypatch.setattr(db_manager_module, "_monotonic", fake_monotonic)

            result = await db.run_history_migration(batch_size=3, time_budget_seconds=2.5)

            assert result.startswith("[HIST-MIGRATE] PARTIAL"), result
            assert "ERROR" not in result
            cur = await db._conn.execute("SELECT COUNT(*) AS cnt FROM main.war_attacks")
            assert (await cur.fetchone())["cnt"] > 0, "expected the deadline to stop it early"
        finally:
            if db.conn:
                await db.conn.close()

    async def test_resumed_run_completes_and_persists_target_cutoff(self, tmp_path):
        db = await self._open(tmp_path, "resume")
        try:
            await self._insert_old_war_attacks(db, 6, date="2020-01-01")
            await self._insert_old_war_attacks(db, 6, date="2020-01-02", offset=100)

            partial = await db.run_history_migration(batch_size=3, row_budget=6)
            assert partial.startswith("[HIST-MIGRATE] PARTIAL")

            final = await db.run_history_migration(batch_size=3)
            assert final.startswith("[HIST-MIGRATE] Caught up"), final
            assert "PARTIAL" not in final
            assert "ERROR" not in final

            target, _ = db._history_cutoff()
            assert await db.get_history_migration_cutoff_reached() == target

            cur = await db._conn.execute("SELECT COUNT(*) AS cnt FROM main.war_attacks")
            assert (await cur.fetchone())["cnt"] == 0
            cur = await db._conn.execute("SELECT COUNT(*) AS cnt FROM history.war_attacks")
            assert (await cur.fetchone())["cnt"] == 12
        finally:
            if db.conn:
                await db.conn.close()

    async def test_empty_database_is_a_no_op_and_marks_target_reached(self, tmp_path):
        """Steady state: nothing has aged out. Must not report PARTIAL forever."""
        db = await self._open(tmp_path, "empty")
        try:
            result = await db.run_history_migration()
            assert "Nothing to migrate" in result, result
            target, _ = db._history_cutoff()
            assert await db.get_history_migration_cutoff_reached() == target
        finally:
            if db.conn:
                await db.conn.close()

    async def test_far_past_row_does_not_cause_a_day_by_day_crawl(self, tmp_path):
        """One row dated 2020 must not make the walk iterate ~2400 calendar days.

        Guards the indexed MIN() lookahead in _oldest_migratable_date(since=...). If it
        regresses, this test still passes functionally but the walk visits every empty
        day — so assert on the reported day count, which is the observable proxy.
        """
        db = await self._open(tmp_path, "sparse")
        try:
            await self._insert_old_war_attacks(db, 2, date="2020-01-01")
            await self._insert_old_war_attacks(db, 2, date="2024-06-15", offset=100)

            result = await db.run_history_migration(batch_size=10)

            assert result.startswith("[HIST-MIGRATE] Caught up"), result
            # Exactly the two days that hold data — not the ~1,600 between them.
            assert "across 2 day(s)" in result, result
            cur = await db._conn.execute("SELECT COUNT(*) AS cnt FROM history.war_attacks")
            assert (await cur.fetchone())["cnt"] == 4
        finally:
            if db.conn:
                await db.conn.close()

    async def test_checkpoint_every_batches_override_accepted(self, tmp_path):
        """Plain pass-through — confirm an override doesn't break a normal full run."""
        db = await self._open(tmp_path, "ckpt")
        try:
            await self._insert_old_war_attacks(db, 5)
            result = await db.run_history_migration(batch_size=2, checkpoint_every_batches=1000)
            assert result.startswith("[HIST-MIGRATE] Caught up"), result
            cur = await db._conn.execute("SELECT COUNT(*) AS cnt FROM history.war_attacks")
            assert (await cur.fetchone())["cnt"] == 5
        finally:
            if db.conn:
                await db.conn.close()

    async def test_migration_sets_advisory_flag_not_the_hard_block(self, tmp_path):
        """The whole point of the redesign: the walk must never set db_maintenance_mode.

        Setting it is what refused 9.6 hours of Discord commands on 2026-09-01 for a
        run that holds no exclusive lock and never needed to refuse any.
        """
        import QBcore

        db = await self._open(tmp_path, "flags")
        seen = {"maintenance": False, "migration": False}
        try:
            await self._insert_old_war_attacks(db, 4)

            original = db._migrate_date_window_batched

            async def _spy(*args, **kwargs):
                seen["maintenance"] = seen["maintenance"] or QBcore.db_maintenance_mode
                seen["migration"] = seen["migration"] or QBcore.db_migration_active
                return await original(*args, **kwargs)

            db._migrate_date_window_batched = _spy  # type: ignore[method-assign]
            await db.run_history_migration(batch_size=2)

            assert seen["migration"] is True, "advisory flag should be set during the walk"
            assert seen["maintenance"] is False, "the walk must NOT take the hard block"
            assert QBcore.db_migration_active is False, "flag must be cleared afterwards"
        finally:
            if db.conn:
                await db.conn.close()
