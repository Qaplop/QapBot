"""
Manually trigger DB maintenance (WAL checkpoint + VACUUM/REINDEX + ANALYZE) immediately.

Normally `WarHistoryDB.nightly_db_maintenance()` only runs automatically once a
night (see QapBot.py's maintenance scheduler). This script lets an operator run
the exact same maintenance on demand, e.g. right after a large one-off DELETE
burst (like the monthly hot->history migration) instead of waiting for the next
nightly window for the freed space to be reclaimed.

What it does (see `WarHistoryDB.nightly_db_maintenance()` docstring for full detail):
  1. WAL checkpoint (TRUNCATE) — covers both the hot DB and the attached 'history' DB.
  2. VACUUM (if the hot DB's freelist > 500 pages or a page-size migration is pending)
     or REINDEX otherwise. Main/hot-DB only — 'history' only ever receives bulk INSERTs
     (never DELETEs) in normal operation, so it doesn't accumulate freelist bloat.
  3. ANALYZE + PRAGMA optimize — refreshes query-planner statistics for BOTH the hot DB
     and 'history' (important after the history DB just received a large bulk insert
     from a migration run, since it starts with zero statistics otherwise).

Safe to re-run: every step is idempotent. VACUUM is skipped automatically if the
freelist is already small; REINDEX/ANALYZE are cheap and safe to repeat.

IMPORTANT:
  - The bot should be STOPPED before running this against a live database file —
    this script opens its own WarHistoryDB connection pool, separate from the
    running bot's, and VACUUM needs an EXCLUSIVE lock to run efficiently.
    - VACUUM rewrites the entire hot DB file (via VACUUM INTO — see the docstring
    for why that's preferred over in-place VACUUM on HDD/server-machine storage). This can
    take a while on a multi-GB database; progress is logged every 2 minutes.

Usage:
    python qapbot/scripts/run_db_maintenance_now.py                # prompts for confirmation
    python qapbot/scripts/run_db_maintenance_now.py --yes          # skip confirmation
    python qapbot/scripts/run_db_maintenance_now.py --db /path/to/qapbot.db --history-db /path/to/qapbot_history.db --yes
"""
import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from qapbot.config import CONFIG  # noqa: E402
from qapbot.db_manager import WarHistoryDB  # noqa: E402

# Standalone script — nothing else in this process configures a logging handler,
# so without this, every logging.info() call inside nightly_db_maintenance()
# (checkpoint/VACUUM/REINDEX/ANALYZE progress) is silently dropped. Same format
# QapBot.py's own logging uses.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)


async def _run(db_path: str, history_db_path: str) -> None:
    print(f"Hot DB     : {db_path}")
    print(f"History DB : {history_db_path}")
    print()
    db = WarHistoryDB()
    await db.initialize(db_path, history_db_path)
    try:
        print("Running nightly_db_maintenance() (WAL checkpoint -> VACUUM/REINDEX -> ANALYZE)...")
        result = await db.nightly_db_maintenance()
        print()
        print(result)
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
        print("This will run VACUUM/REINDEX/ANALYZE against the hot DB (and ANALYZE against")
        print("the history DB). Make sure the bot is stopped first.")
        ans = input("Type YES to proceed: ").strip()
        if ans != "YES":
            print("Aborted. Nothing was changed.")
            sys.exit(1)

    asyncio.run(_run(args.db, args.history_db))


if __name__ == "__main__":
    main()
