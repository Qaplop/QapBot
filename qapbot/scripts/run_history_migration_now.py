"""
Manually advance the rolling hot->history DB migration immediately, without waiting
for the nightly maintenance window.

`WarHistoryDB.run_history_migration()` normally runs once per night as Step 0.5 of
`run_nightly_maintenance_routine()`, gated by `QapBot.py`'s `is_history_migration_due()`
("is the cutoff we reached still behind the cutoff we want"). This script calls the method
DIRECTLY, so it runs regardless of that gate and regardless of the configured budgets —
use it to advance the backlog on demand, under supervision.

How the walk works (2026-09-01 redesign): the target cutoff is
`min(today - CONFIG.history_retention_days, first day of the previous calendar month)`, and
the migration walks one calendar day at a time toward it, persisting the cutoff it reached
in `bot_metadata` after each COMPLETED day. So it is naturally resumable at day granularity
— interrupt it whenever, re-run, and it continues from where it stopped with no rescan.

IMPORTANT:
  - The bot should be STOPPED before running this against a live database
    file — this script opens its own WarHistoryDB connection pool, separate
    from the running bot's, and a long migration run competes for the same
    write lock as the bot's normal cycle writes.
  - Make sure there is enough free disk space on the volume holding both the
    hot and history DB files before running this. The migration issues a
    periodic PASSIVE WAL checkpoint every ~100K rows moved (added after the
    2026-08-01 incident where an uncheckpointed multi-hour run filled the
    disk), but that only bounds *ongoing* WAL growth — it does not help if the
    volume is already full going in. Free space first (see the incident
    writeup in changelog.txt / DATABASE_ARCHITECTURE.md) and confirm with
    `PRAGMA quick_check` or a plain `df`/`Get-Volume` before running.
  - After this completes, run `run_db_maintenance_now.py` to checkpoint/VACUUM
    and reclaim the space freed by the migration's DELETEs — same order as
    the nightly scheduler (`run_nightly_maintenance_routine()`: migration
    first, then WAL checkpoint -> VACUUM/REINDEX -> ANALYZE).

BOUNDING A RUN: --row-budget is the primary bound (the nightly path passes
CONFIG.history_migration_nightly_row_budget), --time-budget-minutes a secondary hard
stop. Either produces a "[HIST-MIGRATE] PARTIAL" result — not an error — with the reached
cutoff persisted, so the next invocation continues from there. Combine with --batch-size
and --checkpoint-every-batches to trade checkpoint overhead for throughput when you have
ample free disk space; bigger values move data faster but let more uncheckpointed WAL
accumulate between checkpoints. Note --batch-size is no longer capped by SQLite's
32766-parameter limit (the engine uses keyset id ranges, not `id IN (...)`), so pick it by
how long a single transaction should hold the write lock — ~20000 targets 1-2s.

FAST MODE (added 2026-08-01, same incident): even with a bigger --batch-size,
the normal path still maintains all of war_attacks' 5 secondary indexes on
BOTH schemas for every row moved (6 B-tree updates per row on each side) —
that per-row index-maintenance cost, not disk I/O, is what limited a live run
to ~900-1000 rows/sec despite the underlying data being only ~10 GB. Pass
--fast to instead temporarily DROP those 5 secondary indexes on both
main.war_attacks and history.war_attacks, move data in large single-commit
chunks (--fast-chunk-size, default 1,000,000 rows), then rebuild every
dropped index in one efficient bulk pass afterward. Expect an order of
magnitude (or more) throughput improvement. war_summary/CWL tables are
migrated via the normal (already fast enough at their much smaller volume)
path regardless of --fast.
  - --fast REQUIRES the bot to be stopped even more strictly than normal —
    it drops main.war_attacks' own query indexes for the duration, which
    would badly degrade any concurrent live query against it.
  - Dropped indexes are always rebuilt before the script exits, even if the
    migration itself errors out partway.
  - --row-budget / --time-budget-minutes / --batch-size / --checkpoint-every-batches
    are ignored in --fast mode (it has its own chunk-size-based safety valve,
    not a time budget — it's meant to run once, to completion).

Usage:
    python qapbot/scripts/run_history_migration_now.py                # prompts for confirmation
    python qapbot/scripts/run_history_migration_now.py --yes          # skip confirmation
    python qapbot/scripts/run_history_migration_now.py --db /path/to/qapbot.db --history-db /path/to/qapbot_history.db --yes
    python qapbot/scripts/run_history_migration_now.py --row-budget 500000 --yes
    python qapbot/scripts/run_history_migration_now.py --time-budget-minutes 30 --batch-size 20000 --yes
    python qapbot/scripts/run_history_migration_now.py --fast --yes   # bot MUST be stopped
"""
import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from qapbot.config import CONFIG  # noqa: E402
from qapbot.db_manager import WarHistoryDB  # noqa: E402


def _configure_logging(log_file: str | None) -> None:
    """Standalone script — nothing else in this process configures a logging
    handler, so without this, every logging.info() call inside
    run_history_migration()/fast_bulk_history_migration() (the per-batch/
    per-chunk progress lines) is silently dropped and the run appears to hang
    with zero output for however long it takes. Same format QapBot.py's own
    logging uses.

    Also writes to a FILE, not just stdout (added 2026-08-01 after losing
    visibility into a run twice in one incident — once from an SSH session
    dying mid-run with no way to reattach, once from the process itself being
    killed by something unlogged). A lost terminal or a killed process no
    longer means a lost record of what happened up to that point.
    """
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


async def _run(
    db_path: str,
    history_db_path: str,
    batch_size: int,
    checkpoint_every_batches: int | None,
    time_budget_minutes: float | None,
    fast: bool,
    fast_chunk_size: int,
    row_budget: int | None = None,
) -> None:
    print(f"Hot DB     : {db_path}")
    print(f"History DB : {history_db_path}")
    db = WarHistoryDB()
    await db.initialize(db_path, history_db_path)
    try:
        if fast:
            print(f"Mode       : FAST (chunk size {fast_chunk_size:,} rows, indexes dropped+rebuilt)")
            print()
            print("Running fast_bulk_history_migration()...")
            result = await db.fast_bulk_history_migration(chunk_size=fast_chunk_size)
            print()
            print(result)
            if result.startswith("[HIST-MIGRATE-FAST] ERROR"):
                sys.exit(1)
        else:
            print(f"Batch size : {batch_size}")
            if checkpoint_every_batches:
                print(f"Checkpoint every: {checkpoint_every_batches} batches")
            if time_budget_minutes:
                print(f"Time budget: {time_budget_minutes} minutes")
            if row_budget:
                print(f"Row budget : {row_budget:,} rows")
            print()
            print("Running run_history_migration() (rolling day-by-day hot->history walk)...")
            result = await db.run_history_migration(
                batch_size=batch_size,
                checkpoint_every_batches=checkpoint_every_batches,
                time_budget_seconds=(time_budget_minutes * 60) if time_budget_minutes else None,
                row_budget=row_budget,
            )
            print()
            print(result)
            if result.startswith("[HIST-MIGRATE] ERROR"):
                sys.exit(1)
            if result.startswith("[HIST-MIGRATE] PARTIAL"):
                print("Time budget reached — not done yet. Re-run this same command to continue.")
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=CONFIG.db_path, help="Path to the hot qapbot.db (default: CONFIG.db_path)")
    parser.add_argument("--history-db", default=getattr(CONFIG, "history_db_path", None),
                         help="Path to qapbot_history.db (default: CONFIG.history_db_path)")
    parser.add_argument("--batch-size", type=int, default=5000,
                         help="Rows per batch/transaction (default: 5000). Larger values reduce "
                              "commit overhead (faster) at the cost of more WAL accumulated per batch — "
                              "only raise this when you have ample free disk space.")
    parser.add_argument("--checkpoint-every-batches", type=int, default=None,
                         help="Override how many batches run between periodic WAL checkpoints "
                              "(default: WarHistoryDB._MIGRATION_CHECKPOINT_INTERVAL_BATCHES, currently 20). "
                              "Raise this (e.g. 100+) on a one-off recovery run with plenty of free disk "
                              "space to cut checkpoint overhead; keep it low/default for routine runs.")
    parser.add_argument("--time-budget-minutes", type=float, default=None,
                         help="Stop cleanly after this many minutes, leaving the rest for a follow-up run "
                              "(result starts with '[HIST-MIGRATE] PARTIAL', not treated as done — safe to "
                              "just re-run this same command later to continue). Use this to chunk a large "
                              "backlog into windows that don't block the live bot for hours at a time. "
                              "Ignored in --fast mode.")
    parser.add_argument("--row-budget", type=int, default=None,
                         help="Stop cleanly once this many rows have moved, leaving the rest for a "
                              "follow-up run (the automatic nightly path uses "
                              "CONFIG.history_migration_nightly_row_budget). Primary bound; combine "
                              "with --time-budget-minutes as a secondary hard stop. Ignored in --fast mode.")
    parser.add_argument("--fast", action="store_true",
                         help="Temporarily drop war_attacks' secondary indexes on both schemas, move data "
                              "in large single-commit chunks, then rebuild the indexes — an order of "
                              "magnitude or more faster than the default batched path, since it avoids "
                              "per-row index-maintenance overhead. REQUIRES the bot to be stopped (drops "
                              "main.war_attacks' own query indexes for the duration). Ignores "
                              "--batch-size/--checkpoint-every-batches/--time-budget-minutes.")
    parser.add_argument("--fast-chunk-size", type=int, default=1_000_000,
                         help="Rows per commit in --fast mode (default: 1,000,000). Each chunk is still "
                              "checkpointed, bounding how much uncheckpointed WAL a single chunk can grow "
                              "to — this is --fast mode's safety valve in place of a time budget.")
    parser.add_argument("--log-file", default=os.path.join(CONFIG.data_dir, "logs", "history_migration_now.log"),
                         help="Also write progress to this file, not just stdout (default: "
                              "<data_dir>/logs/history_migration_now.log) — so a lost SSH session or an "
                              "unexpectedly killed process still leaves a record of what happened up to "
                              "that point. Pass an empty string to disable file logging.")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    args = parser.parse_args()

    _configure_logging(args.log_file or None)
    if args.log_file:
        print(f"Logging to stdout AND {args.log_file}")

    if not args.yes:
        if args.fast:
            print("FAST MODE: this will temporarily DROP war_attacks' secondary indexes on BOTH the")
            print("hot and history DB, move data in large chunks, then rebuild the indexes. The bot")
            print("MUST be stopped — dropping main.war_attacks' indexes while it's live would badly")
            print("degrade any concurrent query against that table.")
        else:
            print("This will move data older than the retention window from the hot DB to the")
            print("history DB (batched DELETE+INSERT). Make sure the bot is stopped and there is")
            print("enough free disk space first.")
        ans = input("Type YES to proceed: ").strip()
        if ans != "YES":
            print("Aborted. Nothing was changed.")
            sys.exit(1)

    asyncio.run(_run(
        args.db, args.history_db, args.batch_size, args.checkpoint_every_batches,
        args.time_budget_minutes, args.fast, args.fast_chunk_size, args.row_budget,
    ))


if __name__ == "__main__":
    main()
