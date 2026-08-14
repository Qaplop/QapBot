"""
One-time repair for the 2026-08-14 hot/history schema-drift incident (Cardinal Rule 1,
.github/copilot-instructions.md; full writeup in qapbot/docs/DATABASE_ARCHITECTURE.md's
"Hot/History DB Split" section -> "Incident: silent column-order divergence...").

`main.war_attacks`/`war_summary` and their `history.*` counterparts had silently diverged in
physical column order (each schema's columns were added via a separate `ALTER TABLE ADD COLUMN`
history), and the monthly hot->history migration used a bare `SELECT *`, which matches columns by
POSITION, not name. Every row that ever migrated therefore had its trailing columns (war_summary:
cid 14+, war_attacks: cid 7+) written under the wrong column NAME in `history` — deterministically,
not randomly, so nothing is lost, just mislabeled. `db_manager.py`'s migration code itself was
already fixed (explicit column lists, see `_explicit_column_list()`) so no *new* row gets
corrupted this way — this script repairs the rows that migrated before that fix landed.

HOW IT WORKS: for each of the 4 tables ever mirrored to `history` (war_attacks, war_summary,
cwl_league_groups, cwl_league_rounds), this compares `main`'s and `history`'s actual on-disk
column order (`PRAGMA <schema>.table_info`) to detect drift. For an affected table, it derives
"history's column at position k currently holds the value that belongs under main's column name
at position k". A table where main/history already agree (no drift detected — true for
cwl_league_groups/cwl_league_rounds as of this writing) is left completely untouched, so this
script is also safe to run against a DB that was never affected, or one that's already repaired.

The originals are NEVER dropped — only renamed to `<table>_corrupted_backup` once a repaired copy
is built and verified, and only when --apply is given.

THREE EXPLICIT STAGES (redesigned 2026-08-14 after the first --build-by-default run confused an
operator watching `history.db` grow in size with no `--apply` given — "dry run" must mean
literally zero bytes written, not "doesn't rename the originals"):

  1. (no flags, the default) PREVIEW — 100% READ-ONLY. Detects drift per table (schema reads
     only) and, for each affected table, runs a read-only `SELECT` with the column remap applied
     as an alias directly against the ORIGINAL table (no table created, no row written anywhere)
     to print a small random sample of what the repair would produce. Genuinely writes nothing —
     verify this yourself with a file timestamp/hash on `history.db` before and after if you want
     independent proof.

  2. --build — Actually builds `<table>_repaired`, for real, as a separate, complete copy of the
     affected table. This is the only stage that writes substantial data, and it says so. Still
     never touches `history.<table>` itself (the original) — only ever inserts into the new
     `_repaired` table. Batched and checkpointed (see below) since this can be tens of millions of
     rows; safe to Ctrl+C and re-run, it resumes from `MAX(id)` already present in `_repaired`
     rather than starting over. After building, runs the same full verification (row counts +
     random-sample plausibility checks) against the real, complete `_repaired` table and reports
     it — nothing is swapped yet.

  3. --apply — Runs --build first if the repaired table(s) aren't already fully built (cheap/instant
     if step 2 already completed — resumability means it just confirms and moves on), verifies,
     and if (and only if) verification passes, renames the originals to `<table>_corrupted_backup`
     (kept, not dropped) and the repaired copies into the real table names, then recreates the
     original named indexes against the now-correctly-named table. This final swap is small, fast,
     and fully atomic (one transaction) regardless of table size. If verification fails for ANY
     table, nothing is swapped for ANY table.

BATCHED, CHECKPOINTED, AND RESUMABLE (--build/--apply only): the copy into `<table>_repaired` is
done in batches of `--batch-size` rows (default 20,000), each its own committed transaction, with
a `PRAGMA wal_checkpoint(PASSIVE)` every `--checkpoint-every-batches` batches (default 20) — the
same pattern `_migrate_table_batch_by_date` in `db_manager.py` already uses for the live monthly
migration, and for the same reason (that function's own docstring: an earlier uncheckpointed
multi-hour run once filled the disk). Progress is logged every batch.

IMPORTANT:
  - Building `<table>_repaired` needs real, new disk space — roughly the size of `<table>` itself,
    since it's a genuine second copy until the swap. Check free space first (`df`/`Get-Volume`).
    The swap step itself needs no extra space (a rename, not a copy) but the original is kept as
    `<table>_corrupted_backup`, so the *permanent* steady-state cost after --apply is the original
    table's size again, until you're confident enough to manually DROP the backup tables later.
  - The bot MUST be stopped before running with --apply — this script opens its own
    WarHistoryDB connection, separate from the running bot's, and the table renames need
    exclusive access to the `history` schema. --build alone can safely run with the bot live
    (never touches the real table names) but will compete for the same write lock as the bot's
    normal write cycle, so stopping it first is still simplest.
  - --db/--history-db default to CONFIG.db_path/CONFIG.history_db_path, which already resolve
    to the correct DEV or PROD location automatically (same convention as
    run_history_migration_now.py/run_db_maintenance_now.py) — this script does not need its own
    environment detection.
  - PROD is frozen per this project's standing convention. This script does not enforce that
    itself — running --build/--apply against a PROD-resolved path is a separate, explicit
    decision the operator must make deliberately, not something to do as a side effect of "it
    auto-detected PROD."
  - Refuses --apply for a table whose `<table>_corrupted_backup` already exists (that table has
    already been repaired) unless --force is also given.

Usage:
    python qapbot/scripts/repair_history_schema_drift.py                  # PREVIEW: read-only, writes nothing
    python qapbot/scripts/repair_history_schema_drift.py --build          # builds *_repaired for real, verifies
    python qapbot/scripts/repair_history_schema_drift.py --apply          # builds (if needed) + verifies + swaps
    python qapbot/scripts/repair_history_schema_drift.py --db ... --history-db ... --apply
    python qapbot/scripts/repair_history_schema_drift.py --build --batch-size 50000 --checkpoint-every-batches 10
    python qapbot/scripts/repair_history_schema_drift.py --sample-size 50 # more spot-check rows
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from qapbot.config import CONFIG  # noqa: E402
from qapbot.db_manager import WarHistoryDB  # noqa: E402

# The only 4 tables ever mirrored between `main` and `history` — see
# DATABASE_ARCHITECTURE.md's "Hot/History DB Split" section.
MIGRATED_TABLES = ("war_attacks", "war_summary", "cwl_league_groups", "cwl_league_rounds")

DEFAULT_BATCH_SIZE = 20_000
DEFAULT_CHECKPOINT_EVERY_BATCHES = 20
DEFAULT_SAMPLE_SIZE = 20

_TAG_RE = re.compile(r"^#[A-Z0-9]{3,}$")


def _configure_logging(log_file: Optional[str]) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        try:
            os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        except OSError as e:
            print(f"WARNING: could not open log file {log_file} ({e}) — continuing with console-only logging.")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )


@dataclass
class TablePlan:
    table: str
    needs_repair: bool
    main_cols: List[str] = field(default_factory=list)
    history_cols: List[str] = field(default_factory=list)
    create_sql: str = ""
    index_sqls: List[str] = field(default_factory=list)

    @property
    def select_list(self) -> str:
        """history_cols[k] is the CURRENT (mislabeled) column holding the value that actually
        belongs under main_cols[k] — see the module docstring's "HOW IT WORKS"."""
        return ", ".join(f"{h} AS {m}" for h, m in zip(self.history_cols, self.main_cols))


async def _column_order(conn: Any, schema: str, table: str) -> List[str]:
    cur = await conn.execute(f"PRAGMA {schema}.table_info({table})")
    rows = await cur.fetchall()
    return [r["name"] for r in sorted(rows, key=lambda r: r["cid"])]


async def _plan_for_table(conn: Any, table: str) -> TablePlan:
    main_cols = await _column_order(conn, "main", table)
    history_cols = await _column_order(conn, "history", table)

    if set(main_cols) != set(history_cols):
        raise RuntimeError(
            f"{table}: main and history have genuinely different column SETS "
            f"(not just reordered) — main={main_cols} history={history_cols}. "
            "This script only handles pure reordering; stopping rather than guessing."
        )

    needs_repair = main_cols != history_cols
    if not needs_repair:
        return TablePlan(table=table, needs_repair=False)

    cur = await conn.execute(
        "SELECT sql FROM history.sqlite_master WHERE type='table' AND name=?", (table,)
    )
    row = await cur.fetchone()
    if row is None or not row["sql"]:
        raise RuntimeError(f"{table}: could not read CREATE TABLE sql from history.sqlite_master")
    create_sql = row["sql"]

    cur = await conn.execute(
        "SELECT sql FROM history.sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
        (table,),
    )
    index_sqls = [r["sql"] for r in await cur.fetchall()]

    return TablePlan(
        table=table,
        needs_repair=True,
        main_cols=main_cols,
        history_cols=history_cols,
        create_sql=create_sql,
        index_sqls=index_sqls,
    )


def _repaired_create_sql(create_sql: str, table: str) -> str:
    """Rewrites a CREATE TABLE statement's name to `history.<table>_repaired`, preserving
    everything else (columns, types, defaults, constraints) exactly as history's own schema
    already has it — the schema itself isn't wrong, only which row-values landed under which
    column.

    Always forces `IF NOT EXISTS` onto the rewritten statement, regardless of whether the
    original had it — this is what makes resuming an interrupted build safe: the build step
    re-issues this CREATE every call, and a plain `CREATE TABLE` (no `IF NOT EXISTS`) would fail
    outright the second time the table already exists from a prior partial run.

    `sqlite_master.sql` does NOT retain a schema-qualifying prefix on the table name (schema
    membership comes from which schema's own sqlite_master the row is listed in, not from text
    in the SQL itself) — so the rewritten name must be explicitly qualified with `history.`
    here. Without that, `CREATE TABLE war_summary_repaired (...)` on a connection with both
    `main` and `history` attached silently creates the table in `main`, not `history`.
    """
    pattern = re.compile(
        rf"CREATE TABLE\s+(IF NOT EXISTS\s+)?(history\.)?{re.escape(table)}\b", re.IGNORECASE
    )
    new_sql, count = pattern.subn(
        lambda m: f"CREATE TABLE IF NOT EXISTS history.{table}_repaired", create_sql, count=1
    )
    if count != 1:
        raise RuntimeError(f"Could not locate table name in CREATE TABLE sql for {table}: {create_sql!r}")
    return new_sql


@dataclass
class SampleResult:
    ok: bool
    problems: List[str]
    samples: List[str]


def _check_war_summary_row(r: Any) -> Optional[str]:
    if r["war_tag"] and not _TAG_RE.match(str(r["war_tag"])):
        return f"war_id={r['war_id']}: war_tag {r['war_tag']!r} doesn't look like a real CoC tag"
    if r["result"] not in ("win", "loss", "draw", "", None):
        return f"war_id={r['war_id']}: result {r['result']!r} isn't a recognized war result"
    return None


def _check_war_attacks_row(r: Any) -> Optional[str]:
    if r["stars"] is None or not (0 <= int(r["stars"]) <= 3):
        return f"war_id={r['war_id']} player_tag={r['player_tag']}: stars {r['stars']!r} out of 0-3 range"
    if r["destruction"] is not None and not (0 <= float(r["destruction"]) <= 100):
        return f"war_id={r['war_id']} player_tag={r['player_tag']}: destruction {r['destruction']!r} out of 0-100 range"
    return None


async def _preview_table(conn: Any, plan: TablePlan, sample_size: int) -> SampleResult:
    """100% READ-ONLY: samples what the repair would produce by applying the column remap as a
    plain SELECT alias directly against the ORIGINAL table — no table created, no row written
    anywhere, on any schema. Safe to run at any time, against a live bot, with zero risk."""
    table = plan.table
    problems: List[str] = []
    samples: List[str] = []

    if table == "war_summary":
        cur = await conn.execute(
            f"SELECT war_id, {plan.select_list} FROM history.{table} "
            f"WHERE is_cwl = 1 ORDER BY RANDOM() LIMIT ?",
            (sample_size,),
        )
        rows = await cur.fetchall()
        if not rows:
            problems.append("no is_cwl=1 rows found to preview against")
        for r in rows:
            samples.append(
                f"war_id={r['war_id']} war_tag={r['war_tag']!r} result={r['result']!r} "
                f"date={r['date']!r} created_at={r['created_at']!r}"
            )
            problem = _check_war_summary_row(r)
            if problem:
                problems.append(problem)
    elif table == "war_attacks":
        cur = await conn.execute(
            f"SELECT war_id, {plan.select_list} FROM history.{table} ORDER BY RANDOM() LIMIT ?",
            (sample_size,),
        )
        rows = await cur.fetchall()
        if not rows:
            problems.append("no rows found to preview against")
        for r in rows:
            samples.append(
                f"war_id={r['war_id']} player_tag={r['player_tag']} stars={r['stars']} "
                f"destruction={r['destruction']} attack_order={r['attack_order']}"
            )
            problem = _check_war_attacks_row(r)
            if problem:
                problems.append(problem)

    return SampleResult(ok=not problems, problems=problems, samples=samples)


async def _build_repaired_table_batched(
    conn: Any, plan: TablePlan, batch_size: int, checkpoint_every_batches: int
) -> int:
    """Populates `<table>_repaired` in committed batches with periodic WAL checkpoints — see the
    module docstring's "BATCHED, CHECKPOINTED, AND RESUMABLE" section for why. Resumes from
    `MAX(id)` already present in `_repaired` rather than starting over, so an interrupted run
    just needs the same command run again. This is the ONLY function in this script that writes
    substantial data — everything else is either a schema read or this table's own small,
    already-committed metadata.

    Returns the number of rows inserted THIS run (not the table's total)."""
    repaired_name = f"{plan.table}_repaired"
    await conn.execute(_repaired_create_sql(plan.create_sql, plan.table))
    await conn.commit()

    cur = await conn.execute(f"SELECT COALESCE(MAX(id), 0) AS m FROM history.{repaired_name}")
    resume_from = (await cur.fetchone())["m"]
    if resume_from:
        print(f"  [{plan.table}] resuming — {repaired_name} already has rows up to id={resume_from}")

    insert_cols = ", ".join(plan.main_cols)

    total_moved = 0
    batches_since_checkpoint = 0
    cursor = resume_from
    t_start = time.monotonic()
    while True:
        await conn.execute("BEGIN")
        cur = await conn.execute(
            f"INSERT INTO history.{repaired_name} ({insert_cols}) "
            f"SELECT {plan.select_list} FROM history.{plan.table} "
            f"WHERE id > ? ORDER BY id LIMIT ?",
            (cursor, batch_size),
        )
        moved_this_batch = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        if moved_this_batch == 0:
            await conn.commit()
            break

        cur2 = await conn.execute(f"SELECT MAX(id) AS m FROM history.{repaired_name}")
        cursor = (await cur2.fetchone())["m"]
        await conn.commit()

        total_moved += moved_this_batch
        batches_since_checkpoint += 1
        elapsed = time.monotonic() - t_start
        rate = total_moved / elapsed if elapsed > 0 else 0
        print(f"  [{plan.table}] moved batch of {moved_this_batch} rows (total this run: {total_moved:,}, "
              f"~{rate:,.0f} rows/sec, up to id={cursor})")

        if batches_since_checkpoint >= checkpoint_every_batches:
            batches_since_checkpoint = 0
            try:
                await conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except Exception as e:
                print(f"  [{plan.table}] WARNING: periodic checkpoint failed: {e}")

    return total_moved


@dataclass
class VerificationResult:
    table: str
    ok: bool
    original_count: int
    repaired_count: int
    problems: List[str]
    samples: List[str]


async def _verify_repaired_table(conn: Any, plan: TablePlan, sample_size: int) -> VerificationResult:
    """Verifies the REAL, fully-built `<table>_repaired` — row counts against the original, plus
    the same plausibility spot-checks _preview_table does, applied to the actually-persisted
    data this time rather than a live-remapped read."""
    table = plan.table
    repaired = f"{table}_repaired"

    cur = await conn.execute(f"SELECT COUNT(*) AS n FROM history.{table}")
    original_count = (await cur.fetchone())["n"]
    cur = await conn.execute(f"SELECT COUNT(*) AS n FROM history.{repaired}")
    repaired_count = (await cur.fetchone())["n"]

    problems: List[str] = []
    if original_count != repaired_count:
        problems.append(f"row count mismatch: original={original_count} repaired={repaired_count}")

    samples: List[str] = []
    if table == "war_summary":
        cur = await conn.execute(
            f"SELECT war_id, war_tag, result, date, created_at FROM history.{repaired} "
            f"WHERE is_cwl = 1 ORDER BY RANDOM() LIMIT ?",
            (sample_size,),
        )
        rows = await cur.fetchall()
        if not rows:
            problems.append("no is_cwl=1 sample rows found to verify against")
        for r in rows:
            samples.append(
                f"war_id={r['war_id']} war_tag={r['war_tag']!r} result={r['result']!r} "
                f"date={r['date']!r} created_at={r['created_at']!r}"
            )
            problem = _check_war_summary_row(r)
            if problem:
                problems.append(problem)
    elif table == "war_attacks":
        cur = await conn.execute(
            f"SELECT war_id, player_tag, stars, destruction, attack_order FROM history.{repaired} "
            f"ORDER BY RANDOM() LIMIT ?",
            (sample_size,),
        )
        rows = await cur.fetchall()
        if not rows:
            problems.append("no sample rows found to verify against")
        for r in rows:
            samples.append(
                f"war_id={r['war_id']} player_tag={r['player_tag']} stars={r['stars']} "
                f"destruction={r['destruction']} attack_order={r['attack_order']}"
            )
            problem = _check_war_attacks_row(r)
            if problem:
                problems.append(problem)

    return VerificationResult(
        table=table, ok=not problems, original_count=original_count,
        repaired_count=repaired_count, problems=problems, samples=samples,
    )


async def _swap_in(conn: Any, plan: TablePlan) -> None:
    table = plan.table
    repaired = f"{table}_repaired"
    backup = f"{table}_corrupted_backup"

    await conn.execute(f"ALTER TABLE history.{table} RENAME TO {backup}")
    # Free up the original index names — the backup is a fallback reference copy, not meant
    # to be queried hot, so it doesn't need its indexes.
    cur = await conn.execute(
        "SELECT name FROM history.sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
        (backup,),
    )
    for row in await cur.fetchall():
        await conn.execute(f"DROP INDEX history.{row['name']}")

    await conn.execute(f"ALTER TABLE history.{repaired} RENAME TO {table}")
    for index_sql in plan.index_sqls:
        # index_sqls were captured from the ORIGINAL table before any renaming; SQLite's
        # ALTER TABLE RENAME TO already updated the CREATE TABLE statement's internal
        # references, but CREATE INDEX statements are independent, so re-issue them verbatim
        # against the now-correctly-named table using their original names.
        await conn.execute(index_sql.replace("CREATE INDEX", "CREATE INDEX IF NOT EXISTS", 1)
                            if "IF NOT EXISTS" not in index_sql else index_sql)


async def _collect_plans(conn: Any, require_no_backup: bool, force: bool) -> List[TablePlan]:
    plans = []
    for table in MIGRATED_TABLES:
        plan = await _plan_for_table(conn, table)
        if not plan.needs_repair:
            print(f"[{table}] no drift detected — main/history column order already agree. Skipping.")
            continue
        plans.append(plan)
        print(f"[{table}] DRIFT DETECTED:")
        print(f"  main    order: {plan.main_cols}")
        print(f"  history order: {plan.history_cols}")

        if require_no_backup and not force:
            cur = await conn.execute(
                "SELECT 1 FROM history.sqlite_master WHERE type='table' AND name=?",
                (f"{table}_corrupted_backup",),
            )
            if await cur.fetchone():
                raise RuntimeError(
                    f"{table}_corrupted_backup already exists — this table looks already "
                    f"repaired. Pass --force to re-run anyway (it will resume/rebuild "
                    f"{table}_repaired; the existing backup is left alone)."
                )
    return plans


def _print_sample_result(table: str, result: SampleResult) -> None:
    print(f"[{table}] {len(result.samples)} sample row(s):")
    for line in result.samples:
        print(f"    {line}")
    if result.problems:
        print(f"[{table}] *** PROBLEMS ***")
        for p in result.problems:
            print(f"    - {p}")
    else:
        print(f"[{table}] looks OK.")
    print()


async def _run(
    db_path: str, history_db_path: str, mode: str, sample_size: int, force: bool,
    batch_size: int = DEFAULT_BATCH_SIZE, checkpoint_every_batches: int = DEFAULT_CHECKPOINT_EVERY_BATCHES,
) -> bool:
    """`mode` is one of "preview" (read-only), "build" (writes *_repaired, no swap), or "apply"
    (build if needed, verify, swap)."""
    print(f"Hot DB     : {db_path}")
    print(f"History DB : {history_db_path}")
    print(f"Mode       : {mode.upper()}"
          + (" — read-only, writes nothing" if mode == "preview" else ""))
    if mode != "preview":
        print(f"Batching   : {batch_size:,} rows/batch, checkpoint every {checkpoint_every_batches} batches")
    print()

    db = WarHistoryDB()
    await db.initialize(db_path, history_db_path)
    conn = db._conn
    try:
        plans = await _collect_plans(conn, require_no_backup=(mode == "apply"), force=force)
        if not plans:
            print()
            print("Nothing to repair. Exiting.")
            return True

        if mode == "preview":
            print()
            print("Read-only preview — sampling what the repair would produce (nothing written):")
            all_ok = True
            for plan in plans:
                result = await _preview_table(conn, plan, sample_size)
                _print_sample_result(plan.table, result)
                all_ok = all_ok and result.ok
            print("This was a preview only — history.<table> was not touched, nothing was written.")
            print("Run with --build to actually construct the repaired copies for real.")
            return all_ok

        print()
        print("Building repaired copies (batched, resumable — safe to interrupt and re-run)...")
        for plan in plans:
            moved = await _build_repaired_table_batched(conn, plan, batch_size, checkpoint_every_batches)
            print(f"  [{plan.table}] done — {moved:,} row(s) moved this run.")

        print()
        print("Verifying the built copies...")
        all_ok = True
        for plan in plans:
            result = await _verify_repaired_table(conn, plan, sample_size)
            print(f"[{plan.table}] rows: original={result.original_count} repaired={result.repaired_count}")
            _print_sample_result(plan.table, SampleResult(ok=result.ok, problems=result.problems, samples=result.samples))
            all_ok = all_ok and result.ok

        if not all_ok:
            print("Verification FAILED for at least one table. The *_repaired table(s) are left")
            print("on disk for inspection, but nothing has been swapped — history.<table> is")
            print("completely unaffected either way.")
            return False

        if mode == "build":
            print("Build complete, verification passed. history.<table> is unaffected —")
            print("*_repaired table(s) left on disk. Run with --apply to swap them in for real.")
            return True

        print("Verification passed — swapping repaired tables in (fast, atomic)...")
        await conn.execute("BEGIN")
        try:
            for plan in plans:
                await _swap_in(conn, plan)
                print(f"  [{plan.table}] swapped in. Original preserved as {plan.table}_corrupted_backup.")
            await conn.commit()
        except Exception:
            await conn.execute("ROLLBACK")
            raise
        print()
        print("APPLY complete. Repair committed.")
        return True
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=CONFIG.db_path, help="Path to the hot qapbot.db (default: CONFIG.db_path)")
    parser.add_argument("--history-db", default=getattr(CONFIG, "history_db_path", None),
                         help="Path to qapbot_history.db (default: CONFIG.history_db_path)")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--build", action="store_true",
                             help="Actually build *_repaired tables for real (writes data — the only stage "
                                  "that does). Verifies afterward. Does NOT swap anything in. Default "
                                  "(no flag) is a 100%% read-only preview that writes nothing at all.")
    mode_group.add_argument("--apply", action="store_true",
                             help="Build (if not already fully built — instant if it is, via resumability), "
                                  "verify, and if verification passes, swap the repaired tables in for real. "
                                  "This is the only mode that renames anything.")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                         help=f"Rows per committed batch while building *_repaired (default: {DEFAULT_BATCH_SIZE:,}). "
                              "Larger = faster but more WAL accumulated per batch. Ignored in preview mode.")
    parser.add_argument("--checkpoint-every-batches", type=int, default=DEFAULT_CHECKPOINT_EVERY_BATCHES,
                         help=f"PRAGMA wal_checkpoint(PASSIVE) every N batches (default: {DEFAULT_CHECKPOINT_EVERY_BATCHES}). "
                              "Lower this if the WAL file grows too large between checkpoints. Ignored in preview mode.")
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE,
                         help=f"Number of random rows per table to spot-check (default: {DEFAULT_SAMPLE_SIZE}).")
    parser.add_argument("--force", action="store_true",
                         help="Allow --apply even if <table>_corrupted_backup already exists for a table "
                              "(i.e. it looks already repaired).")
    parser.add_argument("--log-file", default=os.path.join(CONFIG.data_dir, "logs", "repair_history_schema_drift.log"),
                         help="Also write progress to this file, not just stdout. Pass an empty string to disable.")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt (only asked for --apply).")
    args = parser.parse_args()

    mode = "apply" if args.apply else ("build" if args.build else "preview")

    _configure_logging(args.log_file or None)
    if args.log_file:
        print(f"Logging to stdout AND {args.log_file}")

    if mode == "apply" and not args.yes:
        print("This will PERMANENTLY rename history.war_attacks/war_summary (if drifted) to")
        print("*_corrupted_backup and swap in repaired copies. Make sure the bot is stopped first.")
        ans = input("Type YES to proceed: ").strip()
        if ans != "YES":
            print("Aborted. Nothing was changed.")
            sys.exit(1)

    ok = asyncio.run(_run(
        args.db, args.history_db, mode, args.sample_size, args.force,
        args.batch_size, args.checkpoint_every_batches,
    ))
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
