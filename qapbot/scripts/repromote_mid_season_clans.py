"""
One-time remediation for a side effect of `backfill_group_track_war_updates.py`
(applied to prod 2026-08-08): that backfill derived each clan's
`track_war_updates` from its MOST RECENT `cwl_league_groups.league_rank` on
record, including the CURRENTLY IN-PROGRESS season. For a clan that had
already been polled for several rounds of the current season (so some of its
CWL data was already archived) but whose confirmed league for that season is
below Master III, the backfill correctly demoted it per the intended
steady-state policy — but "correctly" mid-season, which stops polling before
the season finishes, permanently freezing an incomplete record for that clan
for the season already in progress.

`_sync_group_track_war_updates()` (qapbot/cache_manager.py) was hardened the
same day to guard against causing this going forward — it now defers a
demotion instead of applying it when the clan already has archived
war_summary rows for the season being processed, and only demotes once the
clan has zero rows for a season (i.e. at the next season's group discovery,
after this one has safely finished). That guard did not exist yet when the
one-time backfill ran, so this script performs the equivalent one-time
correction for clans it already affected: it finds every non-subscribed clan
that is currently `track_war_updates=0` but already has at least one archived
`war_summary` row for the season passed via `--season`, and re-promotes it to
`track_war_updates=1` so the bot resumes polling for the rest of that season.

No special "temporary" bookkeeping is needed — once this season ends and each
clan's NEW season group is discovered, `_sync_group_track_war_updates()` will
freshly re-derive `track_war_updates` from that new group's confirmed league
(by which point the clan has zero rows for the new season, so the guard no
longer applies) and correctly demote it again if it's still below Master III.
Clans genuinely promoted to Master III+ by then simply stay tracked.

This script does NOT touch clans that were correctly demoted by the backfill
with zero current-season data on file — those were never mid-season, so there
is nothing to finish for them.

IMPORTANT — same caveat as backfill_group_track_war_updates.py: this writes
directly to the `clans` table, bypassing CACHE.clan_name_cache (Cardinal
Rule 2). The running bot process will NOT see the correction until restarted.

Usage:
    python -m qapbot.scripts.repromote_mid_season_clans --season 2026-08
    python -m qapbot.scripts.repromote_mid_season_clans --season 2026-08 --apply

    (no --apply)  Dry run: reports how many clans would be re-promoted, with a sample.
    --apply       Actually write the corrections. Requires a typed YES confirmation.
    --season      The currently in-progress CWL season, e.g. "2026-08" or a
                  mid-month bonus season "2026-08-15". Required — deliberately
                  no default, since guessing the wrong season here could
                  re-promote clans for a season that has already fully ended.
"""
import argparse
import os
import sqlite3
import sys
from typing import List

load_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../..'))
if load_path not in sys.path:
    sys.path.insert(0, load_path)

from dotenv import load_dotenv  # type: ignore[import]
load_dotenv()

from qapbot.config import CONFIG
from qapbot.db_manager import attach_history_db


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")


def find_mid_season_demoted(conn: sqlite3.Connection, season: str) -> List[str]:
    """Return clan_tags that are track_war_updates=0, not subscribed, but
    already have at least one archived war_summary row for `season`."""
    rows = conn.execute(
        """
        SELECT DISTINCT c.clan_tag
        FROM   clans c
        JOIN (
            SELECT clan_tag FROM main.war_summary
            WHERE  cwl_season = ? AND is_cwl = 1
            UNION
            SELECT clan_tag FROM history.war_summary
            WHERE  cwl_season = ? AND is_cwl = 1
        ) ws ON ws.clan_tag = c.clan_tag
        WHERE  c.track_war_updates = 0
          AND  c.has_active_subscriptions = 0
        ORDER  BY c.clan_tag
        """,
        (season, season),
    ).fetchall()
    return [str(row["clan_tag"]) for row in rows]


def main() -> None:
    parser = argparse.ArgumentParser(
        description='One-time remediation: re-promote clans mid-season-demoted '
                    'by backfill_group_track_war_updates.py.',
    )
    parser.add_argument('--season', required=True,
                        help='Currently in-progress CWL season, e.g. 2026-08')
    parser.add_argument('--apply', action='store_true',
                        help='Write corrections (default: dry-run report only)')
    args = parser.parse_args()

    db_path = CONFIG.db_path
    mode = "DEV" if CONFIG.is_dev_mode else "PROD"
    print(f'Mode     : {mode}')
    print(f'Database : {db_path}')
    print(f'Season   : {args.season}')
    print()

    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    attach_history_db(conn, db_path, getattr(CONFIG, "history_db_path", None), read_only=True)
    try:
        clan_tags = find_mid_season_demoted(conn, args.season)
    finally:
        conn.close()

    print(f'Clans to re-promote : {len(clan_tags):,}')
    if clan_tags:
        print('Sample (up to 10):')
        for t in clan_tags[:10]:
            print(f'  {t}')
    print()

    if not clan_tags:
        print('Nothing to re-promote.')
        return

    if not args.apply:
        print('[DRY RUN] Not writing. Re-run with --apply to write these corrections.')
        return

    try:
        ans = input(
            f'\nAbout to re-promote {len(clan_tags):,} clan(s) to track_war_updates=1 '
            f'in {mode} database {db_path}.\nType YES to proceed: '
        ).strip()
    except EOFError:
        ans = ''
    if ans != 'YES':
        print('Aborted — confirmation not given.')
        return

    write_conn = sqlite3.connect(db_path)
    _apply_pragmas(write_conn)
    try:
        write_conn.executemany(
            "UPDATE clans SET track_war_updates = 1 WHERE clan_tag = ?",
            [(t,) for t in clan_tags],
        )
        write_conn.commit()
    finally:
        write_conn.close()

    print(f'Re-promoted {len(clan_tags):,} clan(s).')
    print('IMPORTANT: the running bot process must be restarted to pick up these '
          'changes — CACHE.clan_name_cache is only loaded from the DB at startup.')


if __name__ == '__main__':
    main()
