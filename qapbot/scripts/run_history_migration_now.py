"""
Manually trigger the monthly hot->history DB migration immediately, bypassing
the day == 1 gate in QapBot.py's is_monthly_migration_due().

Normally `WarHistoryDB.monthly_history_migration()` only runs automatically
once a month (day 1 of the calendar month, 03:00 UTC window) — see
`QapBot.py`'s `is_monthly_migration_due()` / `run_nightly_maintenance_routine()`.
This script lets an operator (re-)run it on demand, e.g. to resume a run that
errored out partway (disk full, crash, etc.) without waiting for day 1 of next
month, and without needing day == 1 to still be true.

The migration is naturally resumable/idempotent: each batch re-selects
whatever rows are still below the retention cutoff (see
`_migrate_table_batch_by_date` / `_migrate_cwl_table_by_season`), so re-running
this after a partial failure picks up exactly where the previous run stopped —
no special "resume" flag needed.

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

Usage:
    python qapbot/scripts/run_history_migration_now.py                # prompts for confirmation
    python qapbot/scripts/run_history_migration_now.py --yes          # skip confirmation
    python qapbot/scripts/run_history_migration_now.py --db /path/to/qapbot.db --history-db /path/to/qapbot_history.db --yes
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from qapbot.config import CONFIG  # noqa: E402
from qapbot.db_manager import WarHistoryDB  # noqa: E402


async def _run(db_path: str, history_db_path: str) -> None:
    print(f"Hot DB     : {db_path}")
    print(f"History DB : {history_db_path}")
    print()
    db = WarHistoryDB()
    await db.initialize(db_path, history_db_path)
    try:
        print("Running monthly_history_migration() (batched hot->history move)...")
        result = await db.monthly_history_migration()
        print()
        print(result)
        if result.startswith("[HIST-MIGRATE] ERROR"):
            sys.exit(1)
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=CONFIG.db_path, help="Path to the hot qapbot.db (default: CONFIG.db_path)")
    parser.add_argument("--history-db", default=getattr(CONFIG, "history_db_path", None),
                         help="Path to qapbot_history.db (default: CONFIG.history_db_path)")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    args = parser.parse_args()

    if not args.yes:
        print("This will move data older than the retention window from the hot DB to the")
        print("history DB (batched DELETE+INSERT). Make sure the bot is stopped and there is")
        print("enough free disk space first.")
        ans = input("Type YES to proceed: ").strip()
        if ans != "YES":
            print("Aborted. Nothing was changed.")
            sys.exit(1)

    asyncio.run(_run(args.db, args.history_db))


if __name__ == "__main__":
    main()
