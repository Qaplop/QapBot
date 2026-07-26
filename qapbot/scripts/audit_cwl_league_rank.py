"""
Report / fix script for cwl_league_groups.league_rank rows that may have been
corrupted by the post-promotion-league bug (fixed 2026-07-26 in
cache_manager.py's _process_league_group_response).

Root cause (see changelog.txt 2026-07-26 and DATABASE_ARCHITECTURE.md):
    cache_manager._process_league_group_response, which populates league_rank,
    had no notion of "first time seeing this group" vs. "re-processing a group
    already recorded" — it re-derived league_rank from some group member's
    *current* league on every fresh get_league_group() response. That's only
    correct at the moment a group is first discovered (every member shares one
    league purely by construction); once the real season ends, members
    promote/demote at different rates and diverge to different current
    leagues, so re-deriving from "any member" on a later call is a coin flip.
    Two earlier attempts gated this on live API `.state` and on `cwl_ended`
    respectively — both treated the symptom. The actual fix: only ever
    populate league_rank when `db.cwl_group_exists()` reports no row existed
    for this (group_id, season) before this call — the one moment every
    member is *guaranteed* to share one league. An already-known group's
    league_rank is now never touched again by that function, full stop.

    The code bug is already fixed going forward. This script helps find rows
    that were corrupted *before* that fix landed, and applies corrections.

    As of 2026-07-26, a permanent self-heal also runs automatically inside the
    live bot (QBhelperfunctions._cwl_self_heal_league_rank, wired into
    update_cwl_group_stats) — it re-checks a frozen group's league_rank every
    time its standings are served, using the same "safe rank" reasoning as this
    script's `reconstruct` command below. This script is still useful for (a) a
    one-time bulk repair right after deploying that fix (the self-heal only
    fires on-demand, one group at a time, as users query it) and (b) manual
    investigation (`report`, `fix`).

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

    reconstruct --season YYYY-MM [--fresh-after YYYY-MM-DD] [--yes] [--output PATH]
        Bulk, ZERO-API-CALL reconstruction for one season, using data this bot
        already has:
          1. Computes each group's group_rank/total_stars from war_summary —
             offline, no API calls — for every group in that season, not just
             the handful that happened to have an on-demand /leaderboard query.
          2. A clan that finished a "safe" middle rank (conservatively: not
             within the top 3 or bottom 2 of its group — no version of the
             promotion/demotion rules moves anyone outside that band) is
             guaranteed to have played the SAME league the following season.
             So if that clan's `clans.war_league` was refreshed any time after
             --fresh-after, its current live league IS this season's true
             league for the whole group (all members share one league by
             construction).
          3. Where that reconstructed league differs from what's stored,
             corrects it (with --yes); otherwise just confirms it.

        Only trustworthy for ONE season at a time, and only while its window
        hasn't closed: `clans.war_league` stops reflecting *this* season's
        outcome as soon as a LATER season's promotions apply (~a month later).
        Do not use this for older seasons — see the caveat below.

        Always writes a TSV report (see --output) before applying anything.

Why not just refresh every tracked clan via the API?
    For a season with ~140,000 participating clans, most of the resulting
    ~200,000 clan records in `clans` are for passively-discovered CWL
    groupmates nobody is watching — cwl_league_groups.group_rank is only ever
    computed (and league_rank only ever actually *displayed* to a user) for a
    clan someone queried via /leaderboard cwlinfo or cwlgroup. `reconstruct`
    exploits two things this bot already does for free: (a) it already stores
    the full war-by-war history in war_summary regardless of who queries a
    group, so group_rank can be computed for everyone offline; (b) actively
    subscribed clans refresh every cycle and passively-tracked Master III+
    clans refresh roughly every 12-22h (see QBhelperfunctions.fetch_clan_war_data
    and the CLAN_WAR_TRACKING.md tier model) — so for those leagues,
    clans.war_league is very likely already fresh with zero extra calls. Below
    that tier (`track_war_updates=False`), a clan is never routinely refreshed,
    so its war_league can be old and won't pass the --fresh-after check — those
    groups fall into the report's "no fresh safe-rank evidence" bucket instead
    of being guessed at.

Usage:
    python -m qapbot.scripts.audit_cwl_league_rank report
    python -m qapbot.scripts.audit_cwl_league_rank report --clan '#2J00L8YRU'
    python -m qapbot.scripts.audit_cwl_league_rank fix GROUP_ID SEASON "Champion League III"
    python -m qapbot.scripts.audit_cwl_league_rank fix GROUP_ID SEASON "Champion League III" --yes
    python -m qapbot.scripts.audit_cwl_league_rank reconstruct --season 2026-07 --fresh-after 2026-07-12
    python -m qapbot.scripts.audit_cwl_league_rank reconstruct --season 2026-07 --fresh-after 2026-07-12 --yes

Reads/writes CONFIG.db_path (main, "hot" DB) and CONFIG.history_db_path (older
seasons swept out by monthly_history_migration) — same split the live bot uses.
"""
import argparse
import asyncio
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

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


# ── reconstruct ──────────────────────────────────────────────────────────────
# Conservative, league-independent promotion/demotion bounds: no rule table
# version seen in QBhelperfunctions.py promotes more than 3 or demotes more
# than 2. A clan ranked outside [MAX_PROMOTED+1, n_clans-MAX_DEMOTED] is
# therefore GUARANTEED to have stayed in the same league the following season,
# regardless of which league it was actually in or which rule version applied.
MAX_PROMOTED = 3
MAX_DEMOTED = 2


def _safe_rank_band(n_clans: int) -> Optional[Tuple[int, int]]:
    lo, hi = MAX_PROMOTED + 1, n_clans - MAX_DEMOTED
    return (lo, hi) if lo <= hi else None


def _bulk_group_war_stats(conn: sqlite3.Connection, season: str) -> Dict[str, Tuple[int, float, int]]:
    """Return {clan_tag: (total_stars, total_destruction, ended_wars)} for every
    clan with at least one archived CWL war in *season* — computed offline from
    war_summary (main+history), no API calls."""
    has_hist = _has_history(conn)
    ws_sql = "SELECT * FROM main.war_summary"
    if has_hist:
        ws_sql += " UNION ALL SELECT * FROM history.war_summary"
    rows = conn.execute(
        f"WITH ws AS ({ws_sql}) "
        "SELECT clan_tag, "
        "  SUM(clan_stars) + SUM(CASE WHEN result = 'win' THEN 10 ELSE 0 END) AS tot_stars, "
        "  SUM(clan_destruction * team_size) AS tot_destr, "
        "  COUNT(*) AS ended_wars "
        "FROM ws WHERE cwl_season = ? AND is_cwl = 1 AND state = 'war_ended' "
        "GROUP BY clan_tag",
        (season,),
    ).fetchall()
    return {r["clan_tag"]: (int(r["tot_stars"] or 0), float(r["tot_destr"] or 0.0), int(r["ended_wars"] or 0))
            for r in rows}


async def _refresh_groups_live(
    candidates: List[Tuple[str, List[str], str]], concurrency: int
) -> Dict[str, str]:
    """Fetch one clan per group live via get_clan(), trying each safe-rank
    candidate in order until one succeeds. Returns {group_id: war_league_name}
    for groups where a fetch succeeded.

    Uses CONFIG.coc_email/coc_password, which already resolve to the correct
    DEV or PROD credentials automatically based on this machine's .env
    (DISCORD_GUILD_ID) — see qapbot/config.py. There is no separate "which
    environment am I on" check here; CONFIG *is* that check.
    """
    import coc  # type: ignore[import-untyped]

    mode = "DEV" if CONFIG.is_dev_mode else "PROD"
    print(f"[API] Logging in with {mode}-mode credentials...")
    client = coc.Client(key_count=10, throttler=coc.BatchThrottler, throttle_limit=100)
    await client.login(CONFIG.coc_email, CONFIG.coc_password)

    results: Dict[str, str] = {}
    sem = asyncio.Semaphore(concurrency)
    done = 0

    async def _one(gid: str, safe_tags: List[str]) -> None:
        nonlocal done
        async with sem:
            for tag in safe_tags:
                try:
                    clan_obj = await client.get_clan(tag)
                    wl = getattr(clan_obj, "war_league", None)
                    wl_name = getattr(wl, "name", None) if wl else None
                    if wl_name:
                        results[gid] = wl_name
                        break
                except Exception:
                    continue
            done += 1
            if done % 100 == 0:
                print(f"[API] refreshed {done}/{len(candidates)} groups...")

    await asyncio.gather(*[_one(gid, tags) for gid, tags, _rec in candidates])
    await client.close()
    return results


def reconstruct(
    season: str,
    fresh_after: str,
    apply: bool,
    output: Optional[str],
    refresh_missing: bool = False,
    concurrency: int = 20,
) -> None:
    if len(season) != 7:
        print(f"[ERROR] --season must be a plain 'YYYY-MM' regular season, got {season!r}. "
              "Bonus mid-month CWLs aren't supported by this command.")
        return
    try:
        fresh_after_dt = datetime.fromisoformat(fresh_after).replace(tzinfo=timezone.utc)
    except ValueError:
        print(f"[ERROR] --fresh-after must be YYYY-MM-DD, got {fresh_after!r}.")
        return

    conn = _connect()
    group_rows = _fetch_all_rows(conn, None)
    group_rows = [r for r in group_rows if r.cwl_season == season]
    if not group_rows:
        print(f"No cwl_league_groups rows found for season {season!r}.")
        conn.close()
        return

    war_stats = _bulk_group_war_stats(conn, season)
    clan_info: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
    for r in conn.execute("SELECT clan_tag, war_league, last_checked_via_api FROM clans"):
        clan_info[r["clan_tag"]] = (r["war_league"], r["last_checked_via_api"])
    conn.close()

    groups: Dict[str, List[Row]] = {}
    for r in group_rows:
        groups.setdefault(r.league_group_id, []).append(r)

    already_correct: List[Tuple[str, str]] = []       # (group_id, league)
    would_fix: List[Tuple[str, str, str, str]] = []    # (group_id, recorded, reconstructed, source_clan_tag)
    incomplete_war_data: List[str] = []
    no_safe_rank_band: List[str] = []
    no_fresh_evidence: List[Tuple[str, List[str], str]] = []  # (group_id, safe_tags, recorded)
    disagreement: List[Tuple[str, List[str]]] = []

    for gid, members in groups.items():
        n = len(members)
        band = _safe_rank_band(n)
        if band is None:
            no_safe_rank_band.append(gid)
            continue
        lo, hi = band

        ranked: List[Tuple[str, int, float]] = []
        missing_data = False
        for m in members:
            stats = war_stats.get(m.clan_tag)
            if stats is None:
                missing_data = True
                break
            ranked.append((m.clan_tag, stats[0], stats[1]))
        if missing_data or len(ranked) != n:
            incomplete_war_data.append(gid)
            continue

        ranked.sort(key=lambda t: (-t[1], -t[2]))  # stars desc, destruction desc
        safe_tags = [tag for i, (tag, _, _) in enumerate(ranked, start=1) if lo <= i <= hi]

        fresh_leagues: Dict[str, List[str]] = {}
        for tag in safe_tags:
            info = clan_info.get(tag)
            if not info or not info[0] or not info[1]:
                continue
            try:
                lc = datetime.fromisoformat(info[1])
                if lc.tzinfo is None:
                    lc = lc.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
            if lc >= fresh_after_dt:
                fresh_leagues.setdefault(info[0], []).append(tag)

        if not fresh_leagues:
            no_fresh_evidence.append((gid, safe_tags, members[0].league_rank or ""))
            continue
        if len(fresh_leagues) > 1:
            disagreement.append((gid, [t for tags in fresh_leagues.values() for t in tags]))
            continue

        reconstructed_league, source_tags = next(iter(fresh_leagues.items()))
        recorded = members[0].league_rank or ""
        if reconstructed_league == recorded:
            already_correct.append((gid, recorded))
        else:
            would_fix.append((gid, recorded, reconstructed_league, source_tags[0]))

    # ── Optional live refresh for the "no fresh evidence" bucket ───────────────
    still_no_evidence: List[str] = []
    if refresh_missing and no_fresh_evidence:
        print(f"Refreshing {len(no_fresh_evidence)} group(s) live (concurrency={concurrency})...")
        refreshed = asyncio.run(_refresh_groups_live(no_fresh_evidence, concurrency))
        for gid, _safe_tags, recorded in no_fresh_evidence:
            league = refreshed.get(gid)
            if league is None:
                still_no_evidence.append(gid)
            elif league == recorded:
                already_correct.append((gid, recorded))
            else:
                would_fix.append((gid, recorded, league, "(live refresh)"))
        print(f"  {len(refreshed)}/{len(no_fresh_evidence)} resolved via live refresh.")
    else:
        still_no_evidence = [gid for gid, _tags, _rec in no_fresh_evidence]

    # ── Report ────────────────────────────────────────────────────────────────
    out_path = output or f"cwl_reconstruct_{season}.tsv"
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("league_group_id\tstatus\trecorded_league\treconstructed_league\tsource_clan_tag\n")
        for gid, league in already_correct:
            fh.write(f"{gid}\tcorrect\t{league}\t{league}\t\n")
        for gid, recorded, reconstructed, src in would_fix:
            fh.write(f"{gid}\tmismatch\t{recorded}\t{reconstructed}\t{src}\n")
        for gid in incomplete_war_data:
            fh.write(f"{gid}\tincomplete_war_data\t\t\t\n")
        for gid in no_safe_rank_band:
            fh.write(f"{gid}\tno_safe_rank_band\t\t\t\n")
        for gid in still_no_evidence:
            fh.write(f"{gid}\tno_fresh_evidence\t\t\t\n")
        for gid, tags in disagreement:
            fh.write(f"{gid}\tdisagreement\t\t\t{','.join(tags)}\n")

    print(f"Season {season}: {len(groups)} group(s) total.")
    print(f"  already correct              : {len(already_correct)}")
    print(f"  would be fixed                : {len(would_fix)}")
    print(f"  incomplete war_summary data   : {len(incomplete_war_data)}  (can't rank — no API call will fix this, needs harvested war data)")
    print(f"  group too small for safe rank : {len(no_safe_rank_band)}")
    print(f"  no fresh safe-rank evidence   : {len(still_no_evidence)}"
          + ("" if refresh_missing else "  (stale/never-refreshed clans.war_league — pass --refresh-missing to resolve via live API)"))
    print(f"  disagreement among safe ranks : {len(disagreement)}  (anomaly — needs manual review, not auto-fixed)")
    print(f"Full detail written to {out_path}")

    if not would_fix:
        return

    if not apply:
        print(f"\nDry run — {len(would_fix)} correction(s) NOT written. Re-run with --yes to apply.")
        return

    conn = _connect()
    schemas = ["main", "history"] if _has_history(conn) else ["main"]
    n_applied = 0
    for gid, recorded, reconstructed, _src in would_fix:
        for schema in schemas:
            cur = conn.execute(
                f"UPDATE {schema}.cwl_league_groups SET league_rank = ? "
                "WHERE league_group_id = ? AND cwl_season = ?",
                (reconstructed, gid, season),
            )
            n_applied += cur.rowcount
    conn.commit()
    conn.close()
    print(f"\nApplied {len(would_fix)} group correction(s), {n_applied} row(s) updated.")


# ── evaluate-promo-rules ─────────────────────────────────────────────────────
# Supercell has never published exact CWL promotion/demotion counts per league.
# This empirically measures them: for a season whose cwl_league_groups.league_rank
# is already known-correct (verified by `reconstruct` above), compare each
# clan's final rank in its group against its CURRENT live league (clans.war_league,
# gated to entries refreshed after --fresh-after so "current" reliably means
# "the outcome of THIS season", not some later one). Aggregated across every
# 8-clan group in the season, this gives a real sample size per (league, rank)
# instead of guessing from 1-2 examples.
LEAGUE_LADDER: List[str] = [
    "Bronze League III", "Bronze League II", "Bronze League I",
    "Silver League III", "Silver League II", "Silver League I",
    "Gold League III", "Gold League II", "Gold League I",
    "Crystal League III", "Crystal League II", "Crystal League I",
    "Master League III", "Master League II", "Master League I",
    "Champion League III", "Champion League II", "Champion League I",
    "Titan League III", "Titan League II", "Titan League I",
    "Legend League",
]
_LADDER_INDEX: Dict[str, int] = {name: i for i, name in enumerate(LEAGUE_LADDER)}


def evaluate_promo_rules(season: str, fresh_after: str) -> None:
    if len(season) != 7:
        print(f"[ERROR] --season must be a plain 'YYYY-MM' regular season, got {season!r}.")
        return
    try:
        fresh_after_dt = datetime.fromisoformat(fresh_after).replace(tzinfo=timezone.utc)
    except ValueError:
        print(f"[ERROR] --fresh-after must be YYYY-MM-DD, got {fresh_after!r}.")
        return

    conn = _connect()
    group_rows = [r for r in _fetch_all_rows(conn, None) if r.cwl_season == season]
    war_stats = _bulk_group_war_stats(conn, season)
    clan_info: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
    for r in conn.execute("SELECT clan_tag, war_league, last_checked_via_api FROM clans"):
        clan_info[r["clan_tag"]] = (r["war_league"], r["last_checked_via_api"])
    conn.close()

    groups: Dict[str, List[Row]] = {}
    for r in group_rows:
        groups.setdefault(r.league_group_id, []).append(r)

    # (league, rank) -> counts
    tally: Dict[Tuple[str, int], Dict[str, int]] = {}
    non_standard_size = 0
    incomplete = 0
    unknown_league = 0

    for members in groups.values():
        n = len(members)
        recorded_league = members[0].league_rank or ""
        if not recorded_league or recorded_league not in _LADDER_INDEX:
            unknown_league += 1
            continue
        if n != 8:
            non_standard_size += 1
            continue

        ranked: List[Tuple[str, int, float]] = []
        missing = False
        for m in members:
            stats = war_stats.get(m.clan_tag)
            if stats is None:
                missing = True
                break
            ranked.append((m.clan_tag, stats[0], stats[1]))
        if missing:
            incomplete += 1
            continue
        ranked.sort(key=lambda t: (-t[1], -t[2]))

        rec_idx = _LADDER_INDEX[recorded_league]
        for rank, (tag, _s, _d) in enumerate(ranked, start=1):
            key = (recorded_league, rank)
            bucket = tally.setdefault(key, {"promoted": 0, "unchanged": 0, "demoted": 0, "no_evidence": 0})
            info = clan_info.get(tag)
            if not info or not info[0] or not info[1] or info[0] not in _LADDER_INDEX:
                bucket["no_evidence"] += 1
                continue
            try:
                lc = datetime.fromisoformat(info[1])
                if lc.tzinfo is None:
                    lc = lc.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                bucket["no_evidence"] += 1
                continue
            if lc < fresh_after_dt:
                bucket["no_evidence"] += 1
                continue
            cur_idx = _LADDER_INDEX[info[0]]
            if cur_idx > rec_idx:
                bucket["promoted"] += 1
            elif cur_idx < rec_idx:
                bucket["demoted"] += 1
            else:
                bucket["unchanged"] += 1

    print(f"Season {season}: {len(groups)} group(s) total "
          f"({non_standard_size} non-8-clan excluded, {incomplete} incomplete war data excluded, "
          f"{unknown_league} unknown recorded league excluded)")
    print(f"Only counts clans whose clans.war_league was refreshed on/after {fresh_after}\n")

    leagues_seen = sorted({lg for (lg, _r) in tally.keys()}, key=lambda lg: _LADDER_INDEX.get(lg, -1))
    suggested: Dict[str, Tuple[int, int]] = {}
    for lg in leagues_seen:
        print(f"=== {lg} ===")
        n_promoted = 0
        n_demoted = 0
        rank_rows = []
        for rank in range(1, 9):
            c = tally.get((lg, rank))
            if not c:
                continue
            total = c["promoted"] + c["unchanged"] + c["demoted"]
            rank_rows.append((rank, c, total))
        # Suggest n_promoted: contiguous top ranks where promoted is the majority outcome.
        for rank, c, total in rank_rows:
            if total > 0 and c["promoted"] * 2 > total:
                n_promoted = rank
            else:
                break
        # Suggest n_demoted: contiguous bottom ranks where demoted is the majority outcome.
        for rank, c, total in reversed(rank_rows):
            if total > 0 and c["demoted"] * 2 > total:
                n_demoted = 9 - rank
            else:
                break
        suggested[lg] = (n_promoted, n_demoted)

        for rank, c, total in rank_rows:
            if total == 0:
                print(f"  rank {rank}: no fresh evidence ({c['no_evidence']} sampled, all stale/unknown)")
                continue
            pct_p = 100 * c["promoted"] / total
            pct_d = 100 * c["demoted"] / total
            print(f"  rank {rank}: promoted={c['promoted']:4d} ({pct_p:5.1f}%)  "
                  f"unchanged={c['unchanged']:4d}  demoted={c['demoted']:4d} ({pct_d:5.1f}%)  "
                  f"[n={total}, no_evidence={c['no_evidence']}]")
        print(f"  suggested: (n_promoted={n_promoted}, n_demoted={n_demoted})\n")

    print("Suggested _CWL_PROMO_RULES entries (review the per-rank breakdown above before trusting —")
    print("this is majority-vote inference from one season, not a verified rule):")
    for lg in leagues_seen:
        p, d = suggested[lg]
        print(f'    "{lg}": ({p}, {d}),')


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

    p_recon = sub.add_parser("reconstruct", help="Bulk, offline (zero API calls) reconstruction for one season.")
    p_recon.add_argument("--season", required=True, help="Regular season, e.g. '2026-07'.")
    p_recon.add_argument("--fresh-after", required=True,
                          help="YYYY-MM-DD: only trust clans.war_league if last_checked_via_api is on/after "
                               "this date (i.e. after this season's promotions/demotions applied).")
    p_recon.add_argument("--yes", action="store_true", help="Actually write corrections (default: dry run).")
    p_recon.add_argument("--output", help="TSV report path (default: cwl_reconstruct_<season>.tsv in cwd).")
    p_recon.add_argument("--refresh-missing", action="store_true",
                          help="For groups with no fresh safe-rank evidence in clans.war_league, fetch one "
                               "safe-rank clan per group live via the CoC API (uses CONFIG.coc_email/"
                               "coc_password, which auto-select DEV/PROD credentials for this machine).")
    p_recon.add_argument("--concurrency", type=int, default=20, help="Max parallel API calls for --refresh-missing (default 20).")

    p_eval = sub.add_parser("evaluate-promo-rules",
                             help="Empirically measure promotion/demotion counts per league from real season data.")
    p_eval.add_argument("--season", required=True, help="Regular season with already-correct league_rank, e.g. '2026-07'.")
    p_eval.add_argument("--fresh-after", required=True,
                         help="YYYY-MM-DD: only trust clans.war_league if last_checked_via_api is on/after this date.")

    args = p.parse_args()
    if args.command == "report":
        report(args.clan, args.seasons)
    elif args.command == "fix":
        fix(args.group_id, args.season, args.correct_league, apply=args.yes)
    elif args.command == "reconstruct":
        reconstruct(args.season, args.fresh_after, apply=args.yes, output=args.output,
                    refresh_missing=args.refresh_missing, concurrency=args.concurrency)
    elif args.command == "evaluate-promo-rules":
        evaluate_promo_rules(args.season, args.fresh_after)


if __name__ == "__main__":
    main()
