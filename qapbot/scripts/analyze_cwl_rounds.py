"""
Analyze CWL round completion status from the DB and temp war files.

For each CWL round (1-7, or fewer for short groups), shows how many wars are finalized (war_ended)
vs. still ongoing, for the clans tracked in QapBot.

Data sources:
  Archive : war_summary table (round_number from DB — no file scanning)
  Temp    : temp directory JSON files (state from JSON, round from cwl_league_rounds)
  UNKNOWN : wars where round_number is NULL in both sources

Usage:
    python analyze_cwl_rounds.py [YYYY-MM] [--no-log] [--fetch-missing] [--dry-run]

    YYYY-MM          Season to analyze (default: current month, e.g. 2026-05)
    --no-log         Print to stdout only; do not append to the log file
    --fetch-missing  Identify clans with confirmed missing wars (round_number known,
                     war count < max round), fetch their league groups via the CoC API,
                     and append the missing war_tags to data/missing_cwl_war_tags.txt
                     so the bot's recovery pipeline can fetch and archive them.
    --concurrency N  Max parallel API calls with --fetch-missing (default: 30)
    --dry-run        With --fetch-missing: report what would be written, don't write
    Round     Total    Ended   In-War     Prep      Total    Ended   In-War     Prep
    1        80,388   80,388        0        0          2        0        2        0
    UNKNOWN       0        0        0        0         41        0       38        3
"""
import argparse
import glob
import io
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional, TypedDict, cast


class _Tee(io.TextIOBase):
    """Write to both a file and the original stdout simultaneously."""

    def __init__(self, log_path: str, original_stdout: Any) -> None:
        self._log = open(log_path, 'a', encoding='utf-8')  # noqa: SIM115
        self.original_stdout = original_stdout

    def write(self, s: str) -> int:  # type: ignore[override]
        self._log.write(s)
        self._log.flush()
        try:
            return self.original_stdout.write(s)
        except UnicodeEncodeError:
            return self.original_stdout.write(s.encode(self.original_stdout.encoding or 'utf-8', errors='replace').decode(self.original_stdout.encoding or 'utf-8', errors='replace'))

    def flush(self) -> None:
        self._log.flush()
        self.original_stdout.flush()

    def close(self) -> None:
        self._log.close()
        # Don't call super().close() — it triggers a redundant flush on the
        # already-closed _log via our overridden flush(), raising ValueError.

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
from qapbot.config import CONFIG  # type: ignore[import]
from qapbot.constants import normalize_cwl_season  # noqa: E402
from qapbot.db_manager import explicit_column_list_from_conn  # noqa: E402


class _WtInfo(TypedDict):
    cwl_round: int
    league_group_id: Optional[str]
    counter: int


BUCKETS = ('ended', 'in_war', 'prep')
UNKNOWN_KEY = 'UNKNOWN'


def _cat(state: str) -> str:
    if state in ('war_ended', 'warended'):  return 'ended'
    if state == 'preparation':              return 'prep'
    return 'in_war'


def _empty_buckets() -> dict[str, int]:
    return {b: 0 for b in BUCKETS}


def season_from_filename(basename: str, season: str) -> bool:
    """
    Quick check: does this filename belong to the requested season?

    Supports both 3-part and 4-part (CWL) filenames:
      3-part: CLANTAG_OPPTAG_YYYYMMDDHHII_war_data.json           (5 tokens)
      4-part: CLANTAG_OPPTAG_YYYYMMDDHHII_WARTAG_war_data.json    (6 tokens)
    Token index [2] is always the compact timestamp; first 6 chars = YYYYMM.
    """
    parts = basename.split('_')
    if len(parts) not in (5, 6):
        return False
    return len(parts[2]) >= 6 and parts[2][:6] == season


def war_tag_from_filename(basename: str) -> Optional[str]:
    """Extract war_tag from 4-part CWL filenames; None for 3-part filenames."""
    parts = basename.split('_')
    if len(parts) == 6:  # CLAN_OPP_TS_WARTAG_war_data.json
        return f'#{parts[3]}'
    return None


# ---------------------------------------------------------------------------
# War-count distribution: how many clans played exactly N wars
# ---------------------------------------------------------------------------

def query_war_distribution(
    conn: sqlite3.Connection,
    season: str,
) -> tuple[dict[int, int], int, int]:
    """
    Return (distribution, total_wars, total_clans) for the season.

    distribution  : {war_count: clan_count}  — clans grouped by how many
                    war_summary rows they have for this season.
    total_wars    : total war_summary rows (= sum of war_count * clan_count).
    total_clans   : number of distinct clan_tags in war_summary for the season.
    """
    ws_cols = explicit_column_list_from_conn(conn, "war_summary")
    rows = conn.execute(
        f"""
        WITH ws AS (
            SELECT {ws_cols} FROM main.war_summary UNION ALL SELECT {ws_cols} FROM history.war_summary
        )
        SELECT war_count, COUNT(*) AS clan_count
        FROM (
            SELECT clan_tag, COUNT(*) AS war_count
            FROM   ws
            WHERE  is_cwl = 1
              AND  cwl_season = ?
            GROUP  BY clan_tag
        )
        GROUP  BY war_count
        ORDER  BY war_count
        """,
        (season,),
    ).fetchall()

    distribution: dict[int, int] = {}
    total_wars = 0
    total_clans = 0
    for row in rows:
        wc = int(row["war_count"])
        cc = int(row["clan_count"])
        distribution[wc] = cc
        total_wars  += wc * cc
        total_clans += cc
    return distribution, total_wars, total_clans


# ---------------------------------------------------------------------------
# Group max-rounds distribution (derived from cwl_league_rounds)
# ---------------------------------------------------------------------------

def query_max_rounds_distribution(
    conn: sqlite3.Connection,
    season: str,
) -> dict[int, int]:
    """
    Return {max_rounds: clan_count} restricted to clans that appear in
    war_summary for *season* (same population as query_war_distribution).

    max_rounds = COUNT(DISTINCT cwl_round) for that clan's group in
    cwl_league_rounds.  Clans whose group has never been fetched from the
    API (not in cwl_league_groups) are excluded; count them separately with
    count_clans_without_group_data().
    """
    clg_cols = explicit_column_list_from_conn(conn, "cwl_league_groups")
    clr_cols = explicit_column_list_from_conn(conn, "cwl_league_rounds")
    ws_cols = explicit_column_list_from_conn(conn, "war_summary")
    rows = conn.execute(
        f"""
        WITH clg AS (
            SELECT {clg_cols} FROM main.cwl_league_groups UNION ALL SELECT {clg_cols} FROM history.cwl_league_groups
        ), clr AS (
            SELECT {clr_cols} FROM main.cwl_league_rounds UNION ALL SELECT {clr_cols} FROM history.cwl_league_rounds
        ), ws AS (
            SELECT {ws_cols} FROM main.war_summary UNION ALL SELECT {ws_cols} FROM history.war_summary
        )
        SELECT gr.max_rounds, COUNT(*) AS clan_count
        FROM   clg
        JOIN (
            SELECT league_group_id,
                   COUNT(DISTINCT cwl_round) AS max_rounds
            FROM   clr
            WHERE  cwl_season = ?
            GROUP  BY league_group_id
        ) gr ON gr.league_group_id = clg.league_group_id
        JOIN (
            SELECT DISTINCT clan_tag
            FROM   ws
            WHERE  is_cwl = 1 AND cwl_season = ?
        ) wsd ON wsd.clan_tag = clg.clan_tag
        WHERE  clg.cwl_season = ?
        GROUP  BY gr.max_rounds
        ORDER  BY gr.max_rounds
        """,
        (season, season, season),
    ).fetchall()
    return {int(row["max_rounds"]): int(row["clan_count"]) for row in rows}


def count_clans_without_group_data(conn: sqlite3.Connection, season: str) -> int:
    """
    Count clans that have war_summary rows for *season* but no entry in
    cwl_league_groups — i.e. their CWL group has never been fetched from
    the API.  Their max_rounds is unknown.
    """
    ws_cols = explicit_column_list_from_conn(conn, "war_summary")
    clg_cols = explicit_column_list_from_conn(conn, "cwl_league_groups")
    row = conn.execute(
        f"""
        WITH ws AS (
            SELECT {ws_cols} FROM main.war_summary UNION ALL SELECT {ws_cols} FROM history.war_summary
        ), clg AS (
            SELECT {clg_cols} FROM main.cwl_league_groups UNION ALL SELECT {clg_cols} FROM history.cwl_league_groups
        )
        SELECT COUNT(*) AS cnt
        FROM (
            SELECT DISTINCT clan_tag
            FROM   ws
            WHERE  is_cwl = 1 AND cwl_season = ?
        ) wsd
        WHERE NOT EXISTS (
            SELECT 1 FROM clg
            WHERE  clg.clan_tag   = wsd.clan_tag
              AND  clg.cwl_season = ?
        )
        """,
        (season, season),
    ).fetchone()
    return int(row["cnt"]) if row else 0


# ---------------------------------------------------------------------------
# League × round breakdown
# ---------------------------------------------------------------------------

_LEAGUE_ORDER: list[str] = [
    'Legend League',
    'Titan League I', 'Titan League II', 'Titan League III',
    'Champion League I', 'Champion League II', 'Champion League III',
    'Master League I', 'Master League II', 'Master League III',
    'Crystal League I', 'Crystal League II', 'Crystal League III',
    'Gold League I', 'Gold League II', 'Gold League III',
    'Silver League I', 'Silver League II', 'Silver League III',
    'Bronze League I', 'Bronze League II', 'Bronze League III',
    'Unranked',
]
_LEAGUE_RANK: dict[str, int] = {lg: i for i, lg in enumerate(_LEAGUE_ORDER)}

_LEAGUE_ABBREV: dict[str, str] = {
    'Legend League':       'Lgnd',
    'Titan League I':      'Tit1',   'Titan League II':     'Tit2',  'Titan League III':    'Tit3',
    'Champion League I':   'Chm1',   'Champion League II':  'Chm2',  'Champion League III': 'Chm3',
    'Master League I':     'Mst1',   'Master League II':    'Mst2',  'Master League III':   'Mst3',
    'Crystal League I':    'Cry1',   'Crystal League II':   'Cry2',  'Crystal League III':  'Cry3',
    'Gold League I':       'Gld1',   'Gold League II':      'Gld2',  'Gold League III':     'Gld3',
    'Silver League I':     'Slv1',   'Silver League II':    'Slv2',  'Silver League III':   'Slv3',
    'Bronze League I':     'Brz1',   'Bronze League II':    'Brz2',  'Bronze League III':   'Brz3',
    'Unranked':            'Unk',
}


def query_league_round_breakdown(
    conn: sqlite3.Connection,
    season: str,
) -> tuple[dict[tuple[Optional[int], Optional[str]], int], list[str], bool]:
    """
    Return per-round × per-league war counts from war_summary.

    Each war_summary row is joined to cwl_league_groups via (clan_tag, cwl_season)
    to resolve the clan's league rank for that season.

    Returns:
        data            : {(round_number_or_None, league_or_None): count}
        leagues_ordered : league names present in data, sorted highest → lowest
        has_null_league : True if any rows have no cwl_league_groups entry
    """
    ws_cols = explicit_column_list_from_conn(conn, "war_summary")
    clg_cols = explicit_column_list_from_conn(conn, "cwl_league_groups")
    rows = conn.execute(
        f"""
        WITH ws AS (
            SELECT {ws_cols} FROM main.war_summary UNION ALL SELECT {ws_cols} FROM history.war_summary
        ), clg AS (
            SELECT {clg_cols} FROM main.cwl_league_groups UNION ALL SELECT {clg_cols} FROM history.cwl_league_groups
        )
        SELECT ws.round_number,
               clg.league_rank,
               COUNT(*) AS cnt
        FROM   ws
        LEFT   JOIN clg
               ON  clg.clan_tag   = ws.clan_tag
               AND clg.cwl_season = ws.cwl_season
        WHERE  ws.is_cwl = 1 AND ws.cwl_season = ?
        GROUP  BY ws.round_number, clg.league_rank
        """,
        (season,),
    ).fetchall()

    data: dict[tuple[Optional[int], Optional[str]], int] = {}
    seen: set[str] = set()
    has_null = False
    for row in rows:
        rn  = row["round_number"]   # int or None
        lg  = str(row["league_rank"]) if row["league_rank"] else None
        cnt = int(row["cnt"])
        data[(rn, lg)] = cnt
        if lg is not None:
            seen.add(lg)
        else:
            has_null = True

    leagues_ordered = sorted(seen, key=lambda lg: _LEAGUE_RANK.get(lg, 999))
    return data, leagues_ordered, has_null


# ---------------------------------------------------------------------------
# Missing-war recovery helpers
# ---------------------------------------------------------------------------

def find_missing_war_tags(
    conn: sqlite3.Connection,
    season: str,
    temp_dir: str,
    ended_season: bool = True,
) -> tuple[list[str], int, int]:
    """
    Return war_tags that need recovery for this CWL season.

    Algorithm (single pass per table, all in memory):

    Step 1 — Load cwl_league_rounds for the season.
      Build war_tag_info: {war_tag → {cwl_round, league_group_id, counter=2}}.
      counter=2 because each CWL war involves two clans; each fully archived
      war should produce exactly 2 rows in war_summary (one per participant).

    Step 2 — Load cwl_league_groups + clans for the season.
      Build clan_to_group: {clan_tag → league_group_id} for O(1) lookup.
      Build highest_round: {league_group_id → 0} — updated in Step 4.
      Build group_is_master_plus: set of group_ids whose league_rank is M3+.
      Build group_has_subscribed: set of group_ids that contain at least one
        clan with has_active_subscriptions = 1 in the clans table.

    Step 3 — Tiered filter:
      Master III+ groups  → always include (full global recovery).
      Crystal I and lower → include only if the group has a subscribed clan.
      Groups absent from cwl_league_groups → skip (opponent-only; no tracked
        clan is a primary member, so recovery would inflate the DB needlessly).

    Step 4 — Walk war_summary for the season once.
      For every row: decrement war_tag_info[war_tag].counter by 1.
      Also update highest_round[league_group_id] from the row's round_number.

    Step 5 — Exclude temp files (wars still in progress).

    Step 6 — Collect result.
      counter == 0  → fully archived, skip.
      counter == 1  → one participant's row missing (asymmetric archive).
      counter == 2  → neither participant archived.
      If ended_season=False, additionally require cwl_round < highest_round
      for the group (war must be finished because the group progressed further).

    Returns:
        (sorted_war_tags, missing_both, missing_one)
        missing_both : tags where counter == 2 (no war_summary entries at all)
        missing_one  : tags where counter == 1 (one side archived, one missing)
    """

    def _n(wt: str) -> str:
        return f'#{wt.lstrip("#").upper()}'

    season_compact = season.replace('-', '')[:6]  # '2026-06-15' → '202606'

    # ── Step 1: war_tag_info from cwl_league_rounds ──────────────────────────
    war_tag_info: dict[str, _WtInfo] = {}
    for row in conn.execute(
        "SELECT war_tag, cwl_round, league_group_id"
        " FROM cwl_league_rounds"
        " WHERE cwl_season = ? AND war_tag IS NOT NULL AND war_tag != ''",
        (season,),
    ):
        wt = _n(str(row["war_tag"]))
        war_tag_info[wt] = {
            "cwl_round":       int(row["cwl_round"]) if row["cwl_round"] is not None else 0,
            "league_group_id": str(row["league_group_id"]) if row["league_group_id"] else None,
            "counter":         2,
        }

    # ── Step 2: clan_to_group / highest_round / subscription / league tier ───
    _MASTER_PLUS = frozenset((
        'Master League III', 'Master League II', 'Master League I',
        'Champion League III', 'Champion League II', 'Champion League I',
        'Titan League III', 'Titan League II', 'Titan League I', 'Legend League',
    ))

    # Clans with active subscriptions (users explicitly tracking them).
    subscribed_clans: set[str] = set()
    for row in conn.execute("SELECT clan_tag FROM clans WHERE has_active_subscriptions = 1"):
        subscribed_clans.add(str(row["clan_tag"]))

    clan_to_group: dict[str, str] = {}   # clan_tag → league_group_id
    highest_round: dict[str, int] = {}   # league_group_id → max archived round
    group_is_master_plus: set[str] = set()  # group_ids with Master III+ league_rank
    group_has_subscribed: set[str] = set()  # group_ids with ≥1 subscribed clan
    all_known_groups: set[str] = set()      # all group_ids in cwl_league_groups

    for row in conn.execute(
        "SELECT league_group_id, clan_tag, league_rank"
        " FROM main.cwl_league_groups WHERE cwl_season = ?"
        " UNION ALL "
        "SELECT league_group_id, clan_tag, league_rank"
        " FROM history.cwl_league_groups WHERE cwl_season = ?",
        (season, season),
    ):
        lgid = str(row["league_group_id"]) if row["league_group_id"] else None
        if not lgid:
            continue
        all_known_groups.add(lgid)
        highest_round.setdefault(lgid, 0)
        if str(row["league_rank"] or "") in _MASTER_PLUS:
            group_is_master_plus.add(lgid)
        if row["clan_tag"]:
            ct = str(row["clan_tag"])
            clan_to_group[ct] = lgid
            if ct in subscribed_clans:
                group_has_subscribed.add(lgid)

    # ── Step 3: tiered filter ─────────────────────────────────────────────────
    # Master III+: recover all missing wars globally.
    # Crystal I and lower: recover only groups that contain a subscribed clan.
    # Groups absent from cwl_league_groups: skip — these are opponent-only
    #   groups; no primary tracked clan is a member, so archiving them would
    #   inflate the DB without serving any subscription.
    def _keep(info: _WtInfo) -> bool:
        lgid = info["league_group_id"]
        if not lgid or lgid not in all_known_groups:
            return False
        if lgid in group_is_master_plus:
            return True
        return lgid in group_has_subscribed

    war_tag_info = {wt: info for wt, info in war_tag_info.items() if _keep(info)}

    # ── Step 4: walk war_summary — decrement counters + track highest_round ──
    for row in conn.execute(
        "SELECT war_tag, clan_tag, round_number"
        " FROM main.war_summary WHERE cwl_season = ? AND is_cwl = 1"
        " UNION ALL "
        "SELECT war_tag, clan_tag, round_number"
        " FROM history.war_summary WHERE cwl_season = ? AND is_cwl = 1",
        (season, season),
    ):
        wt = _n(str(row["war_tag"])) if row["war_tag"] else None
        rn = row["round_number"]
        if wt and wt in war_tag_info:
            war_tag_info[wt]["counter"] -= 1
            # Update highest_round via the war_tag's own group — works even
            # for groups absent from cwl_league_groups.
            if rn is not None:
                lgid = war_tag_info[wt]["league_group_id"]
                if lgid is not None and int(rn) > highest_round.get(lgid, 0):
                    highest_round[lgid] = int(rn)

        if rn is not None and row["clan_tag"]:
            lgid = clan_to_group.get(str(row["clan_tag"]))
            if lgid and int(rn) > highest_round.get(lgid, 0):
                highest_round[lgid] = int(rn)

    # ── Step 5: exclude ongoing temp-file wars ────────────────────────────────
    in_temp: set[str] = set()
    for sd in glob.glob(os.path.join(temp_dir, 'shard_*')):
        for fp in glob.glob(os.path.join(sd, '*_war_data.json')):
            if not season_from_filename(os.path.basename(fp), season_compact):
                continue
            wt = war_tag_from_filename(os.path.basename(fp))
            if wt:
                in_temp.add(_n(wt))

    # ── Step 6: collect result ────────────────────────────────────────────────
    result: list[str] = []
    missing_both = 0  # counter == 2: no war_summary entries at all
    missing_one  = 0  # counter == 1: one side archived, one missing
    for wt, info in war_tag_info.items():
        if info["counter"] <= 0:
            continue  # fully archived — both perspectives present
        if wt in in_temp:
            continue  # war is in progress, not missing
        if not ended_season:
            # Only include wars that are provably finished: the group has
            # progressed to a later round, so this round must be done.
            lgid = info["league_group_id"]
            hr = highest_round.get(lgid, 0) if lgid else 0
            if hr == 0 or info["cwl_round"] >= hr:
                continue
        if info["counter"] == 2:
            missing_both += 1
        else:
            missing_one += 1
        result.append(wt)

    return sorted(result), missing_both, missing_one


# ---------------------------------------------------------------------------
# Archive: query war_summary table (no file I/O for archive at all)
# ---------------------------------------------------------------------------

def query_archive(
    conn: sqlite3.Connection,
    season: str,
) -> dict[Any, dict[str, int]]:
    """
    Return archive round stats from war_summary.

    Returns: {round_number_or_None: {bucket: count}}
    round_number=None means round_number IS NULL in the DB.
    """
    ws_cols = explicit_column_list_from_conn(conn, "war_summary")
    cur = conn.execute(
        f"""
        WITH ws AS (
            SELECT {ws_cols} FROM main.war_summary UNION ALL SELECT {ws_cols} FROM history.war_summary
        )
        SELECT round_number, state, COUNT(*) AS cnt
        FROM   ws
        WHERE  is_cwl = 1
          AND  cwl_season = ?
        GROUP  BY round_number, state
        """,
        (season,),
    )
    result: dict[Any, dict[str, int]] = {}
    for row in cur.fetchall():
        rn   = row["round_number"]  # may be None
        st   = str(row["state"] or '').lower()
        cnt  = row["cnt"]
        if rn not in result:
            result[rn] = _empty_buckets()
        result[rn][_cat(st)] += cnt
    return result


# ---------------------------------------------------------------------------
# Temp: scan temp dir; look up round from cwl_league_rounds
# ---------------------------------------------------------------------------

def query_temp(
    conn: sqlite3.Connection,
    temp_dir: str,
    season: str,
) -> dict[Any, dict[str, int]]:
    """
    Scan the temp directory for CWL files of this season.
    State is read from JSON.  Round is looked up in cwl_league_rounds
    (or extracted from the 4-part filename war_tag, same table).
    Returns: {round_number_or_None: {bucket: count}}
    """
    all_files = [
        fp
        for sd in glob.glob(os.path.join(temp_dir, 'shard_*'))
        for fp in glob.glob(os.path.join(sd, '*_war_data.json'))
    ]

    # Build war_tag → cwl_round lookup from DB (one query, all rounds this season)
    cur = conn.execute(
        "SELECT war_tag, cwl_round FROM main.cwl_league_rounds WHERE cwl_season = ? "
        "UNION ALL "
        "SELECT war_tag, cwl_round FROM history.cwl_league_rounds WHERE cwl_season = ?",
        (season, season),
    )
    round_lookup: dict[str, int] = {}
    for row in cur.fetchall():
        wt = str(row["war_tag"] or '').upper().lstrip('#')
        if wt:
            round_lookup[wt] = row["cwl_round"]
            round_lookup[f'#{wt}'] = row["cwl_round"]

    result: dict[Any, dict[str, int]] = {}
    scanned = cwl_matches = wrong_season = not_cwl = errors = 0

    for fp in all_files:
        scanned += 1
        basename = os.path.basename(fp)
        # Use only the 6-char YYYYMM month prefix for the quick filename check;
        # a full date season like "2026-06-16" would otherwise never match.
        if not season_from_filename(basename, season.replace('-', '')[:6]):
            wrong_season += 1
            continue

        # Fast path: 4-part CWL filename has war_tag encoded; still need JSON for state.
        fn_war_tag = war_tag_from_filename(basename)

        try:
            with open(fp, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
        except Exception:
            errors += 1
            continue

        if data.get('type') != 'cwl':
            not_cwl += 1
            continue

        # Secondary season filter: use league_group_data.season from the JSON to
        # distinguish sub-seasons within the same month (e.g. "2026-06" regular CWL
        # vs "2026-06-16" mid-month bonus CWL). If the field is absent the war_tag
        # lookup below resolves the round (or leaves it UNKNOWN) instead.
        _lg_season: str = normalize_cwl_season(
            cast(Dict[str, Any], data.get('league_group') or {}).get('season') or ''
        )
        if _lg_season and _lg_season != season:
            wrong_season += 1
            continue

        cwl_matches += 1
        state = str(data.get('state', 'unknown') or 'unknown').lower()

        # Resolve war_tag: prefer filename (no string allocation), fall back to JSON
        war_tag = fn_war_tag or str(data.get('war_tag', '') or '')
        wt_key  = war_tag.upper().lstrip('#')
        rn: Optional[int] = round_lookup.get(wt_key) or round_lookup.get(f'#{wt_key}')

        if rn not in result:
            result[rn] = _empty_buckets()
        result[rn][_cat(state)] += 1

    print(
        f"  [temp] {scanned:,} files scanned -> {cwl_matches:,} CWL matches "
        f"(skipped: {wrong_season:,} wrong season, {not_cwl:,} not-cwl, "
        f"{errors:,} read-err)"
    )
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description='Analyze CWL round completion and optionally fetch missing war_tags.',
    )
    parser.add_argument('season', nargs='?', default=None,
                        help='Season as YYYY-MM or YYYYMM (default: current month)')
    parser.add_argument('--no-log', action='store_true',
                        help='Print to stdout only; do not append to log file')
    parser.add_argument('--fetch-missing', action='store_true',
                        help='Write missing war_tags from cwl_league_rounds to the bot recovery file')
    parser.add_argument('--dry-run', action='store_true',
                        help='With --fetch-missing: show what would be written, do not write')
    args = parser.parse_args()

    if args.no_log:
        _main_inner(args)
        return

    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cwl_rounds_analysis.log')
    tee = _Tee(log_path, sys.stdout)
    sys.stdout = tee  # type: ignore[assignment]
    try:
        _main_inner(args)
    finally:
        sys.stdout = tee.original_stdout
        tee.close()


def _main_inner(args: argparse.Namespace) -> None:
    # --- Season selection ---
    # Accepts YYYY-MM, YYYYMM (6 digits), YYYY-MM-DD, or YYYYMMDD (8 digits).
    # Mid-month bonus CWL seasons are stored as full date strings (e.g. "2026-06-16")
    # in the DB, so we must support them here to avoid mixing them with the regular
    # same-month CWL stored as "2026-06" or "2026-06-01".
    if args.season is not None:
        raw = args.season.replace('-', '')
        if re.fullmatch(r'\d{6}', raw):
            season = f"{raw[:4]}-{raw[4:]}"          # YYYYMM     → YYYY-MM
        elif re.fullmatch(r'\d{8}', raw):
            season = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"  # YYYYMMDD   → YYYY-MM-DD
        else:
            print(f"ERROR: Invalid season '{args.season}'. "
                  "Expected YYYY-MM, YYYYMM, YYYY-MM-DD, or YYYYMMDD.")
            sys.exit(1)
    else:
        # Default: query DB for the most recently recorded CWL season.
        # This returns e.g. "2026-06-16" instead of strftime's "2026-06" when
        # a mid-month bonus CWL is the most recently active season.
        _db_path = CONFIG.db_path
        try:
            _c = sqlite3.connect(f'file:{_db_path}?mode=ro', uri=True)
            _c.row_factory = sqlite3.Row
            _row = _c.execute(
                "SELECT MAX(cwl_season) AS latest FROM war_summary WHERE is_cwl = 1"
            ).fetchone()
            season = (_row["latest"] if _row and _row["latest"] else None) \
                or datetime.now().strftime('%Y-%m')
            _c.close()
        except Exception:
            season = datetime.now().strftime('%Y-%m')

    season_display = season  # already YYYY-MM
    run_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    print()
    print('=' * 60)
    print(f'CWL Round Status Analysis  --  Season {season_display}')
    print(f'Generated: {run_ts}')
    print('=' * 60)

    db_path  = CONFIG.db_path
    temp_dir = os.path.join(CONFIG.data_dir, 'temp')

    print(f'\nDatabase : {db_path}')
    print(f'Temp     : {temp_dir}')
    print()

    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    from qapbot.db_manager import attach_history_db
    attach_history_db(conn, db_path, getattr(CONFIG, "history_db_path", None), read_only=True)
    try:
        # --- Archive data from DB (fast — no file I/O) ---
        print('Querying war_summary for archive data...')
        archive_stats = query_archive(conn, season)
        archive_total = sum(sum(v.values()) for v in archive_stats.values())
        print(f'  [archive] {archive_total:,} war_summary rows for season {season_display}')

        # --- Temp data from files + cwl_league_rounds lookup ---
        print('Scanning temp files...')
        temp_stats = query_temp(conn, temp_dir, season)
    finally:
        conn.close()

    # --- Merge into per-round table (round=None → UNKNOWN) ---
    all_rounds = sorted(
        {r for r in list(archive_stats) + list(temp_stats) if r is not None}
    )

    # --- Print table ---
    CW  = 8
    RW  = 7
    GAP = '   '
    grp_w = CW * 4 + 3
    sep   = '-' * (RW + 2 + grp_w + len(GAP) + grp_w)

    def _n(v: int) -> str:
        return f'{v:>{CW},}'

    print()
    print(f"{'':>{RW}}  {'Archive':^{grp_w}}{GAP}{'Temp':^{grp_w}}")
    print(
        f"{'Round':<{RW}}"
        f"  {'Total':>{CW}} {'Ended':>{CW}} {'In-War':>{CW}} {'Prep':>{CW}}"
        f"{GAP}"
        f"{'Total':>{CW}} {'Ended':>{CW}} {'In-War':>{CW}} {'Prep':>{CW}}"
    )
    print(sep)

    sum_a: dict[str, int] = _empty_buckets()
    sum_t: dict[str, int] = _empty_buckets()

    def _print_row(label: str, a: dict[str, int], t: dict[str, int]) -> None:
        at = sum(a.values())
        tt = sum(t.values())
        print(
            f"{label:<{RW}}"
            f"  {_n(at)} {_n(a['ended'])} {_n(a['in_war'])} {_n(a['prep'])}"
            f"{GAP}"
            f"{_n(tt)} {_n(t['ended'])} {_n(t['in_war'])} {_n(t['prep'])}"
        )

    for r in all_rounds:
        a = archive_stats.get(r, _empty_buckets())
        t = temp_stats.get(r, _empty_buckets())
        if sum(a.values()) == 0 and sum(t.values()) == 0:
            continue
        for b in BUCKETS:
            sum_a[b] += a[b]
            sum_t[b] += t[b]
        _print_row(str(r), a, t)

    # UNKNOWN row (round_number IS NULL)
    a_unk = archive_stats.get(None, _empty_buckets())
    t_unk = temp_stats.get(None, _empty_buckets())
    for b in BUCKETS:
        sum_a[b] += a_unk[b]
        sum_t[b] += t_unk[b]
    _print_row(UNKNOWN_KEY, a_unk, t_unk)

    print(sep)
    _print_row('Total', sum_a, sum_t)
    print()

    # --- Coverage summary ---
    total_archive = sum(sum_a.values())
    known_archive = total_archive - sum(a_unk.values())
    total_temp    = sum(sum_t.values())
    known_temp    = total_temp - sum(t_unk.values())
    if total_archive:
        print(f'Archive round coverage : {known_archive:,} / {total_archive:,} ({100*known_archive/total_archive:.1f}%)')
    if total_temp:
        print(f'Temp round coverage    : {known_temp:,} / {total_temp:,} ({100*known_temp/total_temp:.1f}%)')

    # --- Round-count consistency check ---
    # Game rule: a clan that has round N must also have rounds 1…N-1 (no opt-out).
    # So archive counts can only decrease (or stay flat) from round 1 → round 7.
    # Any round with fewer wars than the maximum-archive count has confirmed
    # missing wars — a lower bound independent of the per-clan cwl_league_rounds
    # analysis.  Rounds still in progress may show an apparent deficit that is
    # NOT missing data; treat those rows as informational.
    if all_rounds:
        arc_totals = {r: sum(archive_stats.get(r, _empty_buckets()).values()) for r in all_rounds}
        ref_cc = max(arc_totals.values()) if arc_totals else 0
        lb_missing = sum(max(0, ref_cc - c) for c in arc_totals.values())
        DW = 9
        sep_cc = '-' * (RW + 2 + CW + 1 + DW)
        print()
        print(f"Round-count consistency  (ref = {ref_cc:,}; all rounds should reach this count)")
        print(f"{'Round':<{RW}}  {'Archive':>{CW}} {'Deficit':>{DW}}")
        print(sep_cc)
        for r in all_rounds:
            cnt = arc_totals[r]
            deficit = ref_cc - cnt
            d_str = f'-{deficit:,}' if deficit else '--'
            print(f"{r!s:<{RW}}  {_n(cnt)} {d_str:>{DW}}")
        print(sep_cc)
        if lb_missing:
            print(f'  Confirmed missing >= {lb_missing:,} wars (round-count gap; late rounds still in progress may show false deficits)')
        else:
            print('  All rounds match -- no gaps detected by round-count comparison.')

    # --- Wars-per-clan distribution table ---
    conn2 = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    conn2.row_factory = sqlite3.Row
    attach_history_db(conn2, db_path, getattr(CONFIG, "history_db_path", None), read_only=True)
    try:
        dist, dist_total_wars, dist_total_clans = query_war_distribution(conn2, season)
    finally:
        conn2.close()

    LW  = 16   # label column width
    CW2 = 12   # count column width
    sep2 = '-' * (LW + CW2)
    print()
    print(f"{'Rounds':<{LW}} {'Clans':>{CW2}}")
    print(sep2)
    for n in range(1, 8):
        count = dist.get(n, 0)
        if count or n <= max(dist, default=0):
            print(f"{n:<{LW}} {count:>{CW2},}")
    # Also print any rows beyond 7 (unexpected but makes script self-documenting)
    over7 = sorted(k for k in dist if k > 7)
    for n in over7:
        print(f"{n:<{LW}} {dist[n]:>{CW2},}")
    print(sep2)
    print(f"{'Total_wars':<{LW}} {dist_total_wars:>{CW2},}")
    print(f"{'Total_clans':<{LW}} {dist_total_clans:>{CW2},}")

    # --- Diagnostic: inspect clans with >7 CWL wars (should be impossible) ---
    if over7:
        conn_diag = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
        conn_diag.row_factory = sqlite3.Row
        attach_history_db(conn_diag, db_path, getattr(CONFIG, "history_db_path", None), read_only=True)
        try:
            ws_cols = explicit_column_list_from_conn(conn_diag, "war_summary")
            outliers = conn_diag.execute(
                f"""
                WITH ws AS (
                    SELECT {ws_cols} FROM main.war_summary UNION ALL SELECT {ws_cols} FROM history.war_summary
                )
                SELECT clan_tag, COUNT(*) AS war_count
                FROM   ws
                WHERE  is_cwl = 1 AND cwl_season = ?
                GROUP  BY clan_tag
                HAVING COUNT(*) > 7
                ORDER  BY war_count DESC
                LIMIT  10
                """,
                (season,),
            ).fetchall()
            print()
            print(f"Diagnostic -- sample clans with >7 CWL wars (max 10):")
            for row in outliers:
                ct = row["clan_tag"]
                wc = row["war_count"]
                detail = conn_diag.execute(
                    f"""
                    WITH ws AS (
                        SELECT {ws_cols} FROM main.war_summary UNION ALL SELECT {ws_cols} FROM history.war_summary
                    )
                    SELECT war_id, war_type, war_tag, round_number,
                           opponent_tag, date, state
                    FROM   ws
                    WHERE  is_cwl = 1 AND cwl_season = ? AND clan_tag = ?
                    ORDER  BY date
                    """,
                    (season, ct),
                ).fetchall()
                print(f"  {ct}  ({wc} wars):")
                for d in detail:
                    rn  = d["round_number"] if d["round_number"] is not None else "?"
                    wt  = d["war_tag"] or "(no war_tag)"
                    wid = d["war_id"]
                    opp = d["opponent_tag"] or "?"
                    dt  = (d["date"] or "")[:10]
                    st  = d["state"] or "?"
                    wtp = d["war_type"] or "?"
                    print(f"    r{rn:>2}  {dt}  state={st:<10}  type={wtp:<8}  opp={opp:<12}  war_id={wid}  war_tag={wt}")
        finally:
            conn_diag.close()
    print()

    # --- Group max-rounds distribution (from cwl_league_rounds) ---
    conn_mr = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    conn_mr.row_factory = sqlite3.Row
    attach_history_db(conn_mr, db_path, getattr(CONFIG, "history_db_path", None), read_only=True)
    try:
        max_rounds_dist = query_max_rounds_distribution(conn_mr, season)
        unknown_grp = count_clans_without_group_data(conn_mr, season)
    finally:
        conn_mr.close()

    if max_rounds_dist:
        print(f"{'Max rounds':<{LW}} {'Clans':>{CW2}}")
        print(sep2)
        for mr in sorted(max_rounds_dist):
            print(f"{mr:<{LW}} {max_rounds_dist[mr]:>{CW2},}")
        if unknown_grp:
            print(f"{'? (no group)':<{LW}} {unknown_grp:>{CW2},}")
        print(sep2)
        print(f"{'Total_clans':<{LW}} {sum(max_rounds_dist.values()) + unknown_grp:>{CW2},}")
        print()

    # --- League × round breakdown ---
    conn_lb = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    conn_lb.row_factory = sqlite3.Row
    attach_history_db(conn_lb, db_path, getattr(CONFIG, "history_db_path", None), read_only=True)
    try:
        lb_data, lb_leagues, lb_has_null = query_league_round_breakdown(conn_lb, season)
    finally:
        conn_lb.close()

    if lb_leagues or lb_has_null:
        all_lb_cols: list[str | None] = list(lb_leagues) + ([None] if lb_has_null else [])

        # Pre-compute per-round totals and per-league totals for the Total row.
        round_totals_lb: dict[Any, int] = {}
        league_totals_lb: dict[str | None, int] = {lg: 0 for lg in all_lb_cols}
        for (rn, lg), cnt in lb_data.items():
            round_totals_lb[rn] = round_totals_lb.get(rn, 0) + cnt
            if lg in league_totals_lb:
                league_totals_lb[lg] = league_totals_lb.get(lg, 0) + cnt
        grand_total_lb = sum(round_totals_lb.values())

        # Column widths: wide enough for the largest value and the header abbrev.
        _RW_LB = 7
        _TW_LB = max(len(f'{grand_total_lb:,}'), len('Total')) + 1

        def _abbrev(lg: str | None) -> str:
            if lg is None:
                return '?'
            return _LEAGUE_ABBREV.get(lg, lg[:6])

        col_w: dict[str | None, int] = {}
        for lg in all_lb_cols:
            max_cnt = max(
                (lb_data.get((rn, lg), 0) for rn in list(round_totals_lb)),
                default=0,
            )
            max_cnt = max(max_cnt, league_totals_lb.get(lg, 0))
            col_w[lg] = max(len(_abbrev(lg)), len(f'{max_cnt:,}')) + 1

        # Header
        hdr = f"{'Round':<{_RW_LB}}  {'Total':>{_TW_LB}}"
        for lg in all_lb_cols:
            hdr += f"  {_abbrev(lg):>{col_w[lg]}}"
        sep_lb = '-' * len(hdr)

        print()
        print('League breakdown per round  (war_summary rows per league tier)')
        print(hdr)
        print(sep_lb)

        all_lb_rounds = sorted(rn for rn in round_totals_lb if rn is not None)
        for rn in all_lb_rounds:
            line = f"{rn!s:<{_RW_LB}}  {round_totals_lb.get(rn, 0):>{_TW_LB},}"
            for lg in all_lb_cols:
                line += f"  {lb_data.get((rn, lg), 0):>{col_w[lg]},}"
            print(line)

        unk_rn_total = round_totals_lb.get(None, 0)
        if unk_rn_total:
            line = f"{'UNKNOWN':<{_RW_LB}}  {unk_rn_total:>{_TW_LB},}"
            for lg in all_lb_cols:
                line += f"  {lb_data.get((None, lg), 0):>{col_w[lg]},}"
            print(line)

        print(sep_lb)
        line = f"{'Total':<{_RW_LB}}  {grand_total_lb:>{_TW_LB},}"
        for lg in all_lb_cols:
            line += f"  {league_totals_lb.get(lg, 0):>{col_w[lg]},}"
        print(line)
        print()

    # --- Missing-war completeness summary (for clans with round_number set) ---
    conn3 = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    conn3.row_factory = sqlite3.Row
    attach_history_db(conn3, db_path, getattr(CONFIG, "history_db_path", None), read_only=True)
    try:
        ws_cols = explicit_column_list_from_conn(conn3, "war_summary")
        clg_cols = explicit_column_list_from_conn(conn3, "cwl_league_groups")
        clr_cols = explicit_column_list_from_conn(conn3, "cwl_league_rounds")
        row = conn3.execute(
            f"""
            -- true_max = MAX(cwl_league_rounds rounds, war_summary max round).
            -- cwl_league_rounds may be stale (e.g. fetched when only 5 rounds existed
            -- for the group) while war_summary already contains round 7.  Using plain
            -- COALESCE would return the stale lr.max_rounds (e.g. 5) and produce a
            -- negative "missing" value for clans that have 7 wars (5 - 7 = -2).
            -- Taking MAX ensures the floor is always the highest round already
            -- confirmed in war_summary, so the count can never go negative.
            -- The WHERE uses < (not !=) so clans that somehow exceed the stale
            -- max_rounds are treated as complete, not missing.
            WITH ws_all AS (
                SELECT {ws_cols} FROM main.war_summary UNION ALL SELECT {ws_cols} FROM history.war_summary
            ), clg_all AS (
                SELECT {clg_cols} FROM main.cwl_league_groups UNION ALL SELECT {clg_cols} FROM history.cwl_league_groups
            ), clr_all AS (
                SELECT {clr_cols} FROM main.cwl_league_rounds UNION ALL SELECT {clr_cols} FROM history.cwl_league_rounds
            )
            SELECT
                COUNT(*) AS incomplete_clans,
                SUM(true_max - war_count) AS missing_wars
            FROM (
                SELECT ws.clan_tag, ws.war_count,
                       MAX(COALESCE(lr.max_rounds, 0), ws.max_round) AS true_max
                FROM (
                    SELECT clan_tag,
                           COUNT(*) AS war_count,
                           MAX(round_number) AS max_round
                    FROM   ws_all
                    WHERE  war_type = 'cwl' AND cwl_season = ?
                      AND  round_number IS NOT NULL
                    GROUP  BY clan_tag
                ) ws
                LEFT JOIN (
                    SELECT clg.clan_tag, MAX(clr.cwl_round) AS max_rounds
                    FROM   clg_all  clg
                    JOIN   clr_all  clr
                           ON clr.league_group_id = clg.league_group_id
                          AND clr.cwl_season      = clg.cwl_season
                    WHERE  clg.cwl_season = ?
                    GROUP  BY clg.clan_tag
                ) lr ON lr.clan_tag = ws.clan_tag
                WHERE ws.war_count < MAX(COALESCE(lr.max_rounds, 0), ws.max_round)
            )
            """,
            (season, season),
        ).fetchone()
        incomplete_clans = row["incomplete_clans"] or 0
        missing_wars     = row["missing_wars"] or 0
    finally:
        conn3.close()

    print()
    print(f'Confirmed incomplete clans (round known, wars missing): {incomplete_clans:,}')
    print(f'Total missing war records                             : {missing_wars:,}')
    if incomplete_clans and not args.fetch_missing:
        print('  -> Run with --fetch-missing to write them to the bot recovery file (DB-only, no API).')
    print()

    # --- Optional: write missing war_tags to bot recovery file (pure DB, no API) ---
    if args.fetch_missing:
        print('-' * 60)
        print('Writing missing war_tags to recovery file')
        print('-' * 60)
        recovery_path = os.path.join(CONFIG.data_dir, 'missing_cwl_war_tags.txt')
        print(f'Recovery file : {recovery_path}')

        # Ask whether to use the strict "completed rounds only" filter
        # (useful while the season is still running) or write everything missing
        # (correct for a finished season).
        ended_season = True
        try:
            ans = input(
                '\n  Season status?\n'
                '  [A] All missing war_tags    (use when season is ENDED)\n'
                '  [C] Completed rounds only   (use when season is ONGOING)\n'
                '  Choice [A/C, default=A]: '
            ).strip().upper()
            ended_season = (ans != 'C')
        except EOFError:
            pass  # non-interactive (piped input / --no-log redirect); default to all
        print(f'  Mode: {"ALL missing" if ended_season else "completed rounds only (cwl_round < highest_round per group)"}')

        # cwl_league_rounds has every war_tag the bot has seen for this season.
        # Counter-based cross-reference with war_summary gives exactly the wars
        # that are absent (counter=2) or only partially archived (counter=1).
        conn4 = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
        conn4.row_factory = sqlite3.Row
        attach_history_db(conn4, db_path, getattr(CONFIG, "history_db_path", None), read_only=True)
        try:
            missing, missing_both, missing_one = find_missing_war_tags(
                conn4, season, temp_dir, ended_season=ended_season
            )
        finally:
            conn4.close()

        print(f'  Missing war_tags identified (absent or asymmetric in war_summary): {len(missing):,}')
        print(f'    -> Missing both entries (0 rows in war_summary) : {missing_both:,}')
        print(f'    -> Missing one entry   (1 row in war_summary)   : {missing_one:,}')
        if not missing:
            print('  Nothing to write -- already fully covered.')
        elif args.dry_run:
            print('  [DRY RUN] Not writing to recovery file.')
        else:
            # Dedup against what is already in the recovery file (safe to re-run)
            existing_in_file: set[str] = set()
            if os.path.exists(recovery_path):
                with open(recovery_path, 'r', encoding='utf-8') as fh:
                    for line in fh:
                        line = line.strip()
                        if line and not line.startswith('#'):
                            existing_in_file.add(line.split('\t')[0])

            to_write = [t for t in missing if t not in existing_in_file]
            print(f'  After dedup with existing file: {len(to_write):,} tags to append')
            if not to_write:
                print('  Recovery file already up to date.')
            else:
                is_new_file = not os.path.exists(recovery_path)
                ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
                with open(recovery_path, 'a', encoding='utf-8') as fh:
                    if is_new_file:
                        fh.write(f'# CWL war_tag recovery — season {season[:4]}-{season[4:]} — {ts}\n')
                        fh.write('# Format: war_tag (one per line, first tab-column used)\n\n')
                    for tag in to_write:
                        fh.write(tag + '\n')
                print(f'  Written {len(to_write):,} tags -> {recovery_path}')
                print('  Bot processes this file via load_cwl_recovery_file() at startup')
                print('  and process_cwl_recovery_batch() each cycle (up to 5,000 tags/cycle).')


if __name__ == '__main__':
    main()
