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
at position k" and builds a correctly-labeled copy (`<table>_repaired`) by reading every row
positionally and re-inserting it under the right names — same schema (types/constraints/indexes)
as the original, pulled from `history`'s own `sqlite_master`. A table where main/history already
agree (no drift detected — true for cwl_league_groups/cwl_league_rounds as of this writing) is
left completely untouched, so this script is also safe to run against a DB that was never
affected, or one that's already been repaired.

The originals are NEVER dropped — only renamed to `<table>_corrupted_backup` once the repaired
copy is verified, and only when --apply is given.

BATCHED, CHECKPOINTED, AND RESUMABLE (2026-08-14, after a first real run against a
tens-of-millions-of-rows `history.war_attacks` grew the WAL to 6.5GB+ in under 2 minutes with zero
progress visibility and had to be killed): the copy into `<table>_repaired` is done in batches of
`--batch-size` rows (default 20,000), each its own committed transaction, with a `PRAGMA
wal_checkpoint(PASSIVE)` every `--checkpoint-every-batches` batches (default 20) — the exact same
pattern `_migrate_table_batch_by_date` in `db_manager.py` already uses for the live monthly
migration, and for the same reason (see that function's own docstring: an earlier uncheckpointed
multi-hour run once filled the disk). Progress is logged every batch. If interrupted (Ctrl+C,
crash, or a deliberate stop), nothing is lost or corrupted — every completed batch is already
committed, and re-running this script picks up from `MAX(id)` already present in `<table>_repaired`
instead of starting over.

Because the build step now commits incrementally, it can no longer live inside one big
rolled-back-by-default transaction. DRY RUN vs --apply now means:
  - Without --apply (default): builds `<table>_repaired` for real (committed, batched, resumable
    — see above) and verifies it, but NEVER touches the original table names — `history.<table>`
    itself is guaranteed untouched, queryable, and unaffected the whole time, in dry run or apply
    alike. Prints a full verification report (row counts, sample rows) before you ever risk the
    swap. The `_repaired` table(s) are left on disk afterward for inspection; harmless clutter,
    not "real" data anything else reads.
  - With --apply: same build+verify, and if (and only if) verification passes, renames the
    originals to `<table>_corrupted_backup` (kept, not dropped) and the repaired copies into the
    real table names, then recreates the original named indexes against the now-correctly-named
    table (the backup table's own indexes are dropped first, to free up the names — the backup is
    a fallback reference copy, not meant to be queried hot). This swap step itself is small, fast,
    and fully atomic (one transaction) regardless of how large the table was to build. If
    verification fails for ANY table, nothing is swapped for ANY table, regardless of --apply.

IMPORTANT:
  - The bot MUST be stopped before running with --apply — this script opens its own
    WarHistoryDB connection, separate from the running bot's, and the table renames need
    exclusive access to the `history` schema. The (much longer) build phase can safely run with
    the bot live, since it never touches the real table names — but expect it to compete for the
    same write lock as the bot's normal write cycle, so stopping the bot first is still simplest.
  - Make sure there is enough free disk space for a full copy of the affected table(s) before
    starting — `<table>_repaired` is a complete second copy of `<table>` until the swap step
    (which drops nothing; the old data just gets renamed).
  - --db/--history-db default to CONFIG.db_path/CONFIG.history_db_path, which already resolve
    to the correct DEV or PROD location automatically (same convention as
    run_history_migration_now.py/run_db_maintenance_now.py) — this script does not need its own
    environment detection.
  - PROD is frozen per this project's standing convention. This script does not enforce that
    itself — running --apply against a PROD-resolved path is a separate, explicit decision the
    operator must make deliberately, not something to do as a side effect of "it auto-detected
    PROD."
  - Refuses --apply for a table whose `<table>_corrupted_backup` already exists (that table has
    already been repaired) unless --force is also given.

Usage:
    python qapbot/scripts/repair_history_schema_drift.py                  # dry run, prints report
    python qapbot/scripts/repair_history_schema_drift.py --apply          # actually repairs
    python qapbot/scripts/repair_history_schema_drift.py --db ... --history-db ... --apply
    python qapbot/scripts/repair_history_schema_drift.py --batch-size 50000 --checkpoint-every-batches 10
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


async def _build_repaired_table_batched(
    conn: Any, plan: TablePlan, batch_size: int, checkpoint_every_batches: int
) -> int:
    """Populates `<table>_repaired` in committed batches with periodic WAL checkpoints — see the
    module docstring's "BATCHED, CHECKPOINTED, AND RESUMABLE" section for why. Resumes from
    `MAX(id)` already present in `_repaired` rather than starting over, so an interrupted run
    just needs the same command run again.

    Returns the number of rows inserted THIS run (not the table's total)."""
    repaired_name = f"{plan.table}_repaired"
    await conn.execute(_repaired_create_sql(plan.create_sql, plan.table))
    await conn.commit()

    cur = await conn.execute(f"SELECT COALESCE(MAX(id), 0) AS m FROM history.{repaired_name}")
    resume_from = (await cur.fetchone())["m"]
    if resume_from:
        print(f"  [{plan.table}] resuming — {repaired_name} already has rows up to id={resume_from}")

    # history_cols[k] is the CURRENT (mislabeled) column holding the value that actually
    # belongs under main_cols[k] — see the module docstring's "HOW IT WORKS".
    select_list = ", ".join(f"{h} AS {m}" for h, m in zip(plan.history_cols, plan.main_cols))
    insert_cols = ", ".join(plan.main_cols)

    total_moved = 0
    batches_since_checkpoint = 0
    cursor = resume_from
    t_start = time.monotonic()
    while True:
        await conn.execute("BEGIN")
        cur = await conn.execute(
            f"INSERT INTO history.{repaired_name} ({insert_cols}) "
            f"SELECT {select_list} FROM history.{plan.table} "
            f"WHERE id > ? ORDER BY id LIMIT ?",
            (cursor, batch_size),
        )
        moved_this_batch = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        if moved_this_batch == 0:
            await conn.commit()
            break

        cur2 = await conn.execute(
            f"SELECT MAX(id) AS m FROM history.{repaired_name}"
        )
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


_TAG_RE = re.compile(r"^#[A-Z0-9]{3,}$")


async def _verify_table(conn: Any, plan: TablePlan, sample_size: int) -> VerificationResult:
    table = plan.table
    repaired = f"{table}_repaired"
    problems: List[str] = []

    cur = await conn.execute(f"SELECT COUNT(*) AS n FROM history.{table}")
    original_count = (await cur.fetchone())["n"]
    cur = await conn.execute(f"SELECT COUNT(*) AS n FROM history.{repaired}")
    repaired_count = (await cur.fetchone())["n"]
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
            if r["war_tag"] and not _TAG_RE.match(str(r["war_tag"])):
                problems.append(f"war_id={r['war_id']}: repaired war_tag {r['war_tag']!r} doesn't look like a real CoC tag")
            if r["result"] not in ("win", "loss", "draw", "", None):
                problems.append(f"war_id={r['war_id']}: repaired result {r['result']!r} isn't a recognized war result")
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
            if r["stars"] is None or not (0 <= int(r["stars"]) <= 3):
                problems.append(f"war_id={r['war_id']} player_tag={r['player_tag']}: repaired stars {r['stars']!r} out of 0-3 range")
            if r["destruction"] is not None and not (0 <= float(r["destruction"]) <= 100):
                problems.append(f"war_id={r['war_id']} player_tag={r['player_tag']}: repaired destruction {r['destruction']!r} out of 0-100 range")

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


async def _run(
    db_path: str, history_db_path: str, apply: bool, sample_size: int, force: bool,
    batch_size: int = DEFAULT_BATCH_SIZE, checkpoint_every_batches: int = DEFAULT_CHECKPOINT_EVERY_BATCHES,
) -> bool:
    print(f"Hot DB     : {db_path}")
    print(f"History DB : {history_db_path}")
    print(f"Mode       : {'APPLY (will write)' if apply else 'DRY RUN (build+verify only, originals untouched)'}")
    print(f"Batching   : {batch_size:,} rows/batch, checkpoint every {checkpoint_every_batches} batches")
    print()

    db = WarHistoryDB()
    await db.initialize(db_path, history_db_path)
    conn = db._conn
    try:
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

            if apply and not force:
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

        if not plans:
            print()
            print("Nothing to repair. Exiting.")
            return True

        print()
        print("Building repaired copies (batched, resumable — safe to interrupt and re-run)...")
        for plan in plans:
            moved = await _build_repaired_table_batched(conn, plan, batch_size, checkpoint_every_batches)
            print(f"  [{plan.table}] done — {moved:,} row(s) moved this run.")

        print()
        print("Verifying...")
        all_ok = True
        for plan in plans:
            result = await _verify_table(conn, plan, sample_size)
            print(f"[{plan.table}] rows: original={result.original_count} repaired={result.repaired_count}")
            print(f"[{plan.table}] {len(result.samples)} sample row(s) after repair:")
            for line in result.samples:
                print(f"    {line}")
            if result.problems:
                all_ok = False
                print(f"[{plan.table}] *** VERIFICATION PROBLEMS ***")
                for p in result.problems:
                    print(f"    - {p}")
            else:
                print(f"[{plan.table}] verification OK.")
            print()

        if not all_ok:
            print("Verification FAILED for at least one table. The *_repaired table(s) are left")
            print("on disk for inspection, but nothing has been swapped — history.<table> is")
            print("completely unaffected either way.")
            return False

        if not apply:
            print("Dry run complete, verification passed.")
            print("history.<table> is unaffected — *_repaired table(s) left on disk for inspection.")
            print("Re-run with --apply to swap the repaired copies in for real.")
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
    parser.add_argument("--apply", action="store_true",
                         help="Actually swap the repaired tables in (default: dry run — builds and "
                              "verifies but never touches the original table names).")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                         help=f"Rows per committed batch while building *_repaired (default: {DEFAULT_BATCH_SIZE:,}). "
                              "Larger = faster but more WAL accumulated per batch.")
    parser.add_argument("--checkpoint-every-batches", type=int, default=DEFAULT_CHECKPOINT_EVERY_BATCHES,
                         help=f"PRAGMA wal_checkpoint(PASSIVE) every N batches (default: {DEFAULT_CHECKPOINT_EVERY_BATCHES}). "
                              "Lower this if the WAL file is still growing too large between checkpoints.")
    parser.add_argument("--sample-size", type=int, default=20,
                         help="Number of random rows per table to spot-check during verification (default: 20).")
    parser.add_argument("--force", action="store_true",
                         help="Allow --apply even if <table>_corrupted_backup already exists for a table "
                              "(i.e. it looks already repaired).")
    parser.add_argument("--log-file", default=os.path.join(CONFIG.data_dir, "logs", "repair_history_schema_drift.log"),
                         help="Also write progress to this file, not just stdout. Pass an empty string to disable.")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt (only asked for --apply).")
    args = parser.parse_args()

    _configure_logging(args.log_file or None)
    if args.log_file:
        print(f"Logging to stdout AND {args.log_file}")

    if args.apply and not args.yes:
        print("This will PERMANENTLY rename history.war_attacks/war_summary (if drifted) to")
        print("*_corrupted_backup and swap in repaired copies. Make sure the bot is stopped first.")
        ans = input("Type YES to proceed: ").strip()
        if ans != "YES":
            print("Aborted. Nothing was changed.")
            sys.exit(1)

    ok = asyncio.run(_run(
        args.db, args.history_db, args.apply, args.sample_size, args.force,
        args.batch_size, args.checkpoint_every_batches,
    ))
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
