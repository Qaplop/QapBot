"""
One-time retroactive backfill for the `_sync_group_track_war_updates()` gap.

Background (see changelog.txt 2026-08-08 (2)/(4), qapbot/docs/CLAN_WAR_TRACKING.md
write-path 7, and qapbot/docs/CWL_ROUND_TRACKING_PLAN.md "Why war_summary coverage
is inherently asymmetric"):

    `_sync_group_track_war_updates()` (qapbot/cache_manager.py) corrects a CWL
    group's `clans.track_war_updates` / `clans.war_league` the moment the group's
    `league_rank` is FRESHLY resolved by `_process_league_group_response()` — but
    that only ever happens for a group being seen there for the FIRST time this
    season (the `_is_new_group` gate — itself a deliberate, separate 2026-07-26
    safety fix, see qapbot/scripts/audit_cwl_league_rank.py). Every group already
    recorded in `cwl_league_groups` *before* this fix was deployed will never
    trigger that sync again this season, so its member clans stay exactly as
    mistracked as they already were — deploying the code fix alone does NOT
    retroactively correct the current backlog, only prevents new mistracking
    going forward (new groups this season, and every future season).

    This script performs the one-time equivalent of that sync for all
    already-known groups: for each clan, it takes the MOST RECENT
    `cwl_league_groups.league_rank` on record (that table's league_rank values
    are NOT affected by this bug — see audit_cwl_league_rank.py for the
    separate, already-fixed issue with league_rank itself) as ground truth for
    the clan's current league, and corrects `clans.track_war_updates` /
    `clans.war_league` to match — exactly mirroring
    `_sync_group_track_war_updates()`'s logic:
      - Subscribed clans (`has_active_subscriptions=1`) are skipped — always
        tracked regardless of league.
      - Every other clan gets `track_war_updates` set to whether its most
        recent league is Master III+ (`qapbot.constants.WAR_UPDATE_LEAGUES`),
        and `war_league` corrected to match that recorded league.

    "Most recent" is by lexicographic `cwl_season` order, which matches
    chronological order for every season string format this project uses
    ("YYYY-MM" regular seasons, "YYYY-MM-DD" mid-month bonus seasons — see
    `qapbot.constants.normalize_cwl_season`).

    Mid-season guard (added after this script was first applied to prod
    2026-08-08 and found to have this exact effect): a would-be demotion is
    DEFERRED — only `war_league` is corrected, `track_war_updates` is left
    alone — when the clan's most recent season is still active (`cwl_ended=0`)
    AND already has an archived `war_summary` row for it. Demoting a clan
    that's already partway through capturing its current season would silence
    polling for the remaining rounds and permanently freeze an incomplete
    record. Deferred clans self-correct at the next season's group discovery
    (see `_sync_group_track_war_updates()`), once they have zero rows for the
    new season. Safe to re-run this script at any time — it will never abandon
    an in-progress season.

IMPORTANT — CACHE.clan_name_cache is the live bot's single source of truth at
runtime (Cardinal Rule 2, .github/copilot-instructions.md). This script writes
directly to the `clans` table (bypassing CACHE, the same pattern the historical
one-time `promote_master_clans.py` script used for the 2026-06-24 incident this
mirrors) — the running bot process will NOT see any corrections until it is
restarted. Deploy the code fix, run this script with --apply, then restart the
bot.

Does NOT touch temp war files for newly-demoted clans (unlike the live
`_sync_group_track_war_updates()`, which cleans those up immediately via
`_cleanup_temp_war_files()`) — any stray temp file for a clan demoted by this
script is swept up by the bot's normal Phase-3B "finalize temp files for
passive clans" pass after restart, same as any other demotion.

Usage:
    python -m qapbot.scripts.backfill_group_track_war_updates
    python -m qapbot.scripts.backfill_group_track_war_updates --limit 500
    python -m qapbot.scripts.backfill_group_track_war_updates --apply

    (no --apply)  Dry run: reports how many clans would change, with a sample,
                  broken down by promotion / demotion / war_league-only fixes.
    --apply       Actually write the corrections. Requires a typed YES
                  confirmation. Auto-detects DEV vs PROD via CONFIG.db_path /
                  CONFIG.is_dev_mode, same as the bot itself.
    --limit N     Cap how many clan rows to correct (for a staged rollout).
                  Applies after sorting by clan_tag for determinism. Default:
                  no cap.
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
from qapbot.constants import WAR_UPDATE_LEAGUES
from qapbot.db_manager import attach_history_db


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")


class _Correction:
    __slots__ = ("clan_tag", "season", "league_rank", "old_war_league",
                 "old_track", "new_track", "deferred")

    def __init__(self, clan_tag: str, season: str, league_rank: str,
                 old_war_league: str, old_track: int, new_track: bool,
                 deferred: bool) -> None:
        self.clan_tag = clan_tag
        self.season = season
        self.league_rank = league_rank
        self.old_war_league = old_war_league
        self.old_track = old_track
        self.new_track = new_track
        self.deferred = deferred


def find_corrections(conn: sqlite3.Connection) -> List[_Correction]:
    """Return one _Correction per non-subscribed clan whose recorded state
    disagrees with its most recent cwl_league_groups.league_rank.

    Mid-season guard: a would-be demotion (old_track=True, new_track=False) is
    marked `deferred` — war_league still corrected, track_war_updates left
    alone — when the clan's most recent season is still active (cwl_ended=0)
    AND already has an archived war_summary row for it. Otherwise this script
    would stop polling a clan partway through its currently in-progress
    season, abandoning already-captured CWL data (see CLAN_WAR_TRACKING.md
    write-path 7's mid-season note; discovered via this exact script when
    first applied to prod 2026-08-08, remediated by
    qapbot/scripts/repromote_mid_season_clans.py). Deferred clans self-correct
    at the next season's group discovery, once they have zero rows for the new
    season.
    """
    rows = conn.execute(
        """
        WITH clg AS (
            SELECT clan_tag, cwl_season, league_rank, cwl_ended
            FROM   main.cwl_league_groups
            WHERE  league_rank IS NOT NULL AND league_rank != ''
            UNION ALL
            SELECT clan_tag, cwl_season, league_rank, cwl_ended
            FROM   history.cwl_league_groups
            WHERE  league_rank IS NOT NULL AND league_rank != ''
        ),
        latest AS (
            SELECT clan_tag, cwl_season, league_rank, cwl_ended,
                   ROW_NUMBER() OVER (
                       PARTITION BY clan_tag ORDER BY cwl_season DESC
                   ) AS rn
            FROM clg
        )
        SELECT l.clan_tag, l.cwl_season, l.league_rank, l.cwl_ended,
               c.war_league, c.track_war_updates, c.has_active_subscriptions,
               EXISTS (
                   SELECT 1 FROM main.war_summary ws
                   WHERE ws.clan_tag = l.clan_tag AND ws.cwl_season = l.cwl_season AND ws.is_cwl = 1
                   UNION ALL
                   SELECT 1 FROM history.war_summary ws
                   WHERE ws.clan_tag = l.clan_tag AND ws.cwl_season = l.cwl_season AND ws.is_cwl = 1
               ) AS has_season_data
        FROM   latest l
        JOIN   clans  c ON c.clan_tag = l.clan_tag
        WHERE  l.rn = 1
          AND  c.has_active_subscriptions = 0
        ORDER  BY l.clan_tag
        """
    ).fetchall()

    corrections: List[_Correction] = []
    for row in rows:
        league_rank = str(row["league_rank"])
        should_track = league_rank in WAR_UPDATE_LEAGUES
        old_war_league = row["war_league"]
        old_track = int(row["track_war_updates"])
        league_wrong = (old_war_league or "") != league_rank
        track_wrong = bool(old_track) != should_track
        would_demote = bool(old_track) and not should_track
        mid_season = not bool(row["cwl_ended"]) and bool(row["has_season_data"])
        deferred = would_demote and mid_season
        # Skip a no-op row: deferred + war_league already correct means nothing
        # would actually change.
        if league_wrong or (track_wrong and not deferred):
            corrections.append(_Correction(
                clan_tag=str(row["clan_tag"]),
                season=str(row["cwl_season"]),
                league_rank=league_rank,
                old_war_league=str(old_war_league or ""),
                old_track=old_track,
                new_track=bool(old_track) if deferred else should_track,
                deferred=deferred,
            ))
    return corrections


def main() -> None:
    parser = argparse.ArgumentParser(
        description='One-time backfill: sync clans.track_war_updates/war_league '
                    'from cwl_league_groups.league_rank for already-known groups.',
    )
    parser.add_argument('--apply', action='store_true',
                        help='Write corrections (default: dry-run report only)')
    parser.add_argument('--limit', type=int, default=None,
                        help='Cap number of clans corrected (staged rollout)')
    args = parser.parse_args()

    db_path = CONFIG.db_path
    mode = "DEV" if CONFIG.is_dev_mode else "PROD"
    print(f'Mode     : {mode}')
    print(f'Database : {db_path}')
    print()

    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    attach_history_db(conn, db_path, getattr(CONFIG, "history_db_path", None), read_only=True)
    try:
        corrections = find_corrections(conn)
    finally:
        conn.close()

    if args.limit is not None:
        corrections = corrections[:args.limit]

    promotions = [c for c in corrections if not c.old_track and c.new_track]
    demotions = [c for c in corrections if c.old_track and not c.new_track and not c.deferred]
    deferred = [c for c in corrections if c.deferred]
    league_only = [c for c in corrections
                   if bool(c.old_track) == c.new_track and not c.deferred]

    print(f'Clans needing correction : {len(corrections):,}')
    print(f'  -> Promote (track_war_updates 0 -> 1) : {len(promotions):,}')
    print(f'  -> Demote  (track_war_updates 1 -> 0) : {len(demotions):,}')
    print(f'  -> war_league-only fix (track unchanged): {len(league_only):,}')
    if deferred:
        print(f'  -> Demotion DEFERRED (mid-season, still has current-season data on file): {len(deferred):,}')
        print('     (war_league corrected; track_war_updates left True — will re-evaluate next season)')
    print()

    if corrections:
        print('Sample (up to 10):')
        for c in corrections[:10]:
            _tag_note = ' [DEFERRED]' if c.deferred else ''
            print(
                f'  {c.clan_tag:<12} season={c.season:<12} '
                f'league_rank={c.league_rank!r:<28} '
                f'war_league {c.old_war_league!r} -> {c.league_rank!r}  '
                f'track {bool(c.old_track)} -> {c.new_track}{_tag_note}'
            )
        print()

    if not corrections:
        print('Nothing to correct.')
        return

    if not args.apply:
        print('[DRY RUN] Not writing. Re-run with --apply to write these corrections.')
        return

    try:
        ans = input(
            f'\nAbout to correct {len(corrections):,} clan(s) in {mode} database '
            f'{db_path}.\nType YES to proceed: '
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
            "UPDATE clans SET war_league = ?, track_war_updates = ? WHERE clan_tag = ?",
            [(c.league_rank, 1 if c.new_track else 0, c.clan_tag) for c in corrections],
        )
        write_conn.commit()
    finally:
        write_conn.close()

    print(f'Corrected {len(corrections):,} clan(s).')
    print('IMPORTANT: the running bot process must be restarted to pick up these '
          'changes — CACHE.clan_name_cache is only loaded from the DB at startup.')


if __name__ == '__main__':
    main()
