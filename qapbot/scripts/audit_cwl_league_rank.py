"""
Report / fix script for cwl_league_groups.league_rank rows that may have been
corrupted by the post-promotion-league bug (fixed 2026-07-26 in
cache_manager.py's _process_league_group_response).

Root cause (see changelog.txt 2026-07-26 and DATABASE_ARCHITECTURE.md):
    The clan_name_cache fallback used to populate league_rank was gated on the
    DB's own `cwl_ended` column instead of the league group's live API `.state`.
    `cwl_ended` only flips once every clan's expected war count is observed in
    war_summary, which can lag days behind the real-world season end — while a
    clan's league in clan_name_cache updates almost immediately after
    promotion/demotion. Any reprocessing of a league group in that lag window
    wrote the clan's *next*-season league into the *current* season's row,
    which was then frozen there once `cwl_ended` finally flipped to 1.

    The code bug is already fixed going forward. This script helps find rows
    that were corrupted *before* that fix landed, and applies corrections.

IMPORTANT — there is no reliable automatic detector for this:
    An earlier version of this script tried to predict each clan's expected
    league from its group_rank and a hardcoded promotion/demotion-count table
    (QBhelperfunctions.py's _CWL_PROMO_RULES_*). That table does not match
    real-world CWL outcomes closely enough to use for detection — tested
    against this bot's own database it flagged >1000 clans as "wrong" even
    when restricted to just the most recent season transition per clan, almost
    all false positives. It has been removed rather than shipped as something
    that looks authoritative but isn't.

    What actually works: manual review, informed by the `report` command below
    (shows recorded league_rank next to the clan's current live league side by
    side, no judgment applied) and by the fact that this bug can only ever
    have struck in the lag window between a season's real-world end and this
    bot's own `cwl_ended` bookkeeping catching up for that group — so it can
    only affect *recently completed* seasons, never a season that was already
    frozen (cwl_ended=1) with the correct value long ago. Older, long-settled
    data is not at risk.

Commands:
    report [--clan TAG] [--seasons N]
        List every clan's recorded league_rank for the last N completed
        regular seasons (default 2) next to its current live league
        (`clans.war_league`). Differences are common and expected for
        real promotions/demotions too — this is a starting point for manual
        review, not a verdict.

    fix GROUP_ID SEASON "Correct League Name" [--yes]
        Apply a correction to one (league_group_id, cwl_season) pair, in
        whichever of the hot/history DBs currently holds it. Dry run unless
        --yes is passed.

Usage:
    python -m qapbot.scripts.audit_cwl_league_rank report
    python -m qapbot.scripts.audit_cwl_league_rank report --clan '#2J00L8YRU'
    python -m qapbot.scripts.audit_cwl_league_rank fix GROUP_ID SEASON "Champion League III"
    python -m qapbot.scripts.audit_cwl_league_rank fix GROUP_ID SEASON "Champion League III" --yes

Reads/writes CONFIG.db_path (main, "hot" DB) and CONFIG.history_db_path (older
seasons swept out by monthly_history_migration) — same split the live bot uses.
"""
import argparse
import os
import sqlite3
import sys
from typing import Dict, List, Optional

# Clan names can contain arbitrary Unicode (emoji, CJK, etc.) that the default
# Windows console codepage can't encode — replace rather than crash.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from qapbot.config import CONFIG  # noqa: E402


class Row:
    __slots__ = ("league_group_id", "cwl_season", "clan_tag", "league_rank", "cwl_ended", "group_rank")

    def __init__(self, r: sqlite3.Row) -> None:
        self.league_group_id: str = r["league_group_id"]
        self.cwl_season: str = r["cwl_season"]
        self.clan_tag: str = r["clan_tag"]
        self.league_rank: Optional[str] = r["league_rank"]
        self.cwl_ended: int = r["cwl_ended"]
        self.group_rank: Optional[int] = r["group_rank"]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(CONFIG.db_path)
    conn.row_factory = sqlite3.Row
    if os.path.exists(CONFIG.history_db_path):
        conn.execute("ATTACH DATABASE ? AS history", (CONFIG.history_db_path,))
    else:
        print(f"[WARN] history DB not found at {CONFIG.history_db_path} - "
              "using hot DB only, older seasons will be missing.")
    return conn


def _has_history(conn: sqlite3.Connection) -> bool:
    return any(r[1] == "history" for r in conn.execute("PRAGMA database_list"))


def _fetch_all_rows(conn: sqlite3.Connection, clan_filter: Optional[str]) -> List[Row]:
    sql = ("SELECT league_group_id, cwl_season, clan_tag, league_rank, cwl_ended, group_rank "
           "FROM main.cwl_league_groups")
    if _has_history(conn):
        sql += (" UNION ALL SELECT league_group_id, cwl_season, clan_tag, league_rank, "
                "cwl_ended, group_rank FROM history.cwl_league_groups")
    params: tuple = ()
    if clan_filter:
        sql = f"SELECT * FROM ({sql}) WHERE clan_tag = ?"
        params = (clan_filter,)
    return [Row(r) for r in conn.execute(sql, params).fetchall()]


def _clan_names_and_leagues(conn: sqlite3.Connection) -> Dict[str, tuple]:
    try:
        return {r["clan_tag"]: (r["name"], r["war_league"])
                for r in conn.execute("SELECT clan_tag, name, war_league FROM clans")}
    except sqlite3.OperationalError:
        return {}


def report(clan_filter: Optional[str], n_seasons: int) -> None:
    conn = _connect()
    rows = _fetch_all_rows(conn, clan_filter)
    info = _clan_names_and_leagues(conn)
    conn.close()

    regular_seasons = sorted({r.cwl_season for r in rows if len(r.cwl_season) == 7 and r.cwl_ended})
    target_seasons = set(regular_seasons[-n_seasons:])
    if not target_seasons:
        print("No completed regular CWL seasons found." + (f" clan filter: {clan_filter}" if clan_filter else ""))
        return

    rows = [r for r in rows if r.cwl_season in target_seasons]
    rows.sort(key=lambda r: (r.cwl_season, r.clan_tag))

    print(f"Showing completed seasons: {', '.join(sorted(target_seasons))}\n")
    n_diff = 0
    for r in rows:
        name, current_league = info.get(r.clan_tag, ("", None))
        diff = current_league is not None and current_league != r.league_rank
        marker = "  <-- differs from current live league" if diff else ""
        if diff:
            n_diff += 1
        print(f"  {r.cwl_season}  {r.clan_tag} {name!r}")
        print(f"    recorded league_rank : {r.league_rank!r}  (group {r.league_group_id}, group_rank {r.group_rank})")
        print(f"    current live league  : {current_league!r}{marker}")
        print()

    print(f"{len(rows)} row(s) shown, {n_diff} differ from the clan's current live league.")
    print("A difference is expected and normal for a clan that has since been promoted/demoted for")
    print("real reasons — it is NOT proof of the bug. Cross-check against what you know about that")
    print("clan's actual CWL history before using --fix.")
    print()
    print("Fix a specific (group, season) with:")
    print('  python -m qapbot.scripts.audit_cwl_league_rank fix GROUP_ID SEASON "Correct League Name" --yes')


def fix(group_id: str, season: str, correct_league: str, apply: bool) -> None:
    conn = _connect()
    schemas = ["main", "history"] if _has_history(conn) else ["main"]

    found_any = False
    for schema in schemas:
        rows = conn.execute(
            f"SELECT clan_tag, league_rank, cwl_ended FROM {schema}.cwl_league_groups "
            "WHERE league_group_id = ? AND cwl_season = ?",
            (group_id, season),
        ).fetchall()
        if not rows:
            continue
        found_any = True

        print(f"[{schema}] {len(rows)} row(s) for group {group_id} / season {season}:")
        for r in rows:
            marker = "  (already correct)" if r["league_rank"] == correct_league else ""
            print(f"    {r['clan_tag']}: {r['league_rank']!r} -> {correct_league!r}{marker}")

        if not apply:
            print("  Dry run - no changes written. Re-run with --yes to apply.")
            continue

        conn.execute(
            f"UPDATE {schema}.cwl_league_groups SET league_rank = ? "
            "WHERE league_group_id = ? AND cwl_season = ?",
            (correct_league, group_id, season),
        )
        conn.commit()
        print(f"  Updated {len(rows)} row(s) in {schema}.cwl_league_groups.")

    if not found_any:
        print(f"No rows found for group {group_id} / season {season} in any schema.")
    conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    p_report = sub.add_parser("report", help="List recent seasons' recorded league vs current live league.")
    p_report.add_argument("--clan", help="Restrict to one clan tag (e.g. '#2J00L8YRU').")
    p_report.add_argument("--seasons", type=int, default=2, help="How many recent completed seasons to show (default 2).")

    p_fix = sub.add_parser("fix", help="Correct league_rank for one (group, season) pair.")
    p_fix.add_argument("group_id")
    p_fix.add_argument("season")
    p_fix.add_argument("correct_league")
    p_fix.add_argument("--yes", action="store_true", help="Actually write the correction (default: dry run).")

    args = p.parse_args()
    if args.command == "report":
        report(args.clan, args.seasons)
    elif args.command == "fix":
        fix(args.group_id, args.season, args.correct_league, apply=args.yes)


if __name__ == "__main__":
    main()
