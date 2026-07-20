"""
Harvest CWL war_tags for the 2026-04 season from the CoC API.

For each clan that has fewer than 7 CWL wars recorded in the DB this season,
call get_league_group() to retrieve all war_tags available for that group.
Each group covers 8 clans, so when one clan's group is fetched we skip the
other 7 — this means ~46K actively tracked clans collapse to roughly 6K group fetches.

War tags that are already in the DB are still written to the output file so the
recovery script has the full picture.  The output file is append-safe: re-running
the script will add any newly-discovered tags and skip duplicates at write time.

Output: qapbot/scripts/cwl_war_tags_2026-04.txt
Format (one line per war):
    <war_tag>\t<clan_tag>\t<opponent_tag>\t<round_number>\t<group_season>

Usage:
    python qapbot/scripts/harvest_cwl_war_tags.py [--season 2026-04] [--concurrency 30] [--dry-run]

Options:
    --season      CWL season to target (default: 2026-04)
    --concurrency Max parallel get_league_group() calls (default: 30)
    --dry-run     Fetch and print stats but do NOT write to file
    --tracked-only  Only process actively tracked clans (track_war_updates=1, default: False)
"""
import argparse
import asyncio
import logging
import os
import signal
import sqlite3
import sys
from datetime import datetime, timezone

import coc  # type: ignore[import-untyped]
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from qapbot.config import CONFIG

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

SEASON = "2026-04"
OUTPUT_FILENAME = "cwl_war_tags_{season}.txt"

_stop = False

def _handle_ctrl_c(sig: int, frame: object) -> None:
    global _stop
    if _stop:
        print("\n[CTRL+C] Force exit.")
        sys.exit(1)
    _stop = True
    print("\n[CTRL+C] Stopping after current batch (press again to force exit)...")

signal.signal(signal.SIGINT, _handle_ctrl_c)


# ── DB helpers ────────────────────────────────────────────────────────────────

def load_incomplete_clans(db_path: str, season: str, tracked_only: bool) -> list[str]:
    """
    Return clan_tags that have fewer than 7 wars recorded for the season.
    Ordered: actively tracked clans first (so we get the most important groups early).
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    from qapbot.db_manager import attach_history_db
    attach_history_db(conn, db_path, read_only=True)
    try:
        query = """
            WITH ws AS (
                SELECT * FROM main.war_summary UNION ALL SELECT * FROM history.war_summary
            )
            SELECT wsc.clan_tag,
                   COALESCE(c.track_war_updates, 0) AS tracked
            FROM (
                SELECT clan_tag, COUNT(war_id) AS war_count
                FROM   ws
                WHERE  is_cwl = 1 AND cwl_season = ?
                GROUP  BY clan_tag
                HAVING COUNT(war_id) < 7
            ) wsc
            LEFT JOIN clans c ON c.clan_tag = wsc.clan_tag
        """
        if tracked_only:
            query += " WHERE COALESCE(c.track_war_updates, 0) = 1"
        query += " ORDER BY tracked DESC, wsc.clan_tag"
        rows = conn.execute(query, (season,)).fetchall()
        return [row["clan_tag"] for row in rows]
    finally:
        conn.close()


def load_known_war_tags(db_path: str, season: str) -> set[str]:
    """Return all war_tags already stored in the DB for this season."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    from qapbot.db_manager import attach_history_db
    attach_history_db(conn, db_path, read_only=True)
    try:
        rows = conn.execute(
            """
            SELECT war_tag FROM main.war_summary WHERE is_cwl=1 AND cwl_season=? AND war_tag!=''
            UNION
            SELECT war_tag FROM history.war_summary WHERE is_cwl=1 AND cwl_season=? AND war_tag!=''
            """,
            (season, season),
        ).fetchall()
        return {row["war_tag"] for row in rows}
    finally:
        conn.close()


# ── API fetch ─────────────────────────────────────────────────────────────────

async def fetch_group_tags(
    client: coc.Client,
    clan_tag: str,
    sem: asyncio.Semaphore,
) -> tuple[str, list[tuple[int, str]], str] | None:
    """
    Fetch the league group for clan_tag and extract (round_number, war_tag) pairs.
    Also extracts participating clan tags so the caller can deduplicate.

    Returns (season, [(round_no, war_tag), ...], group_clan_tags_comma_sep) or None on failure.
    """
    async with sem:
        try:
            group: coc.ClanWarLeagueGroup = await client.get_league_group(clan_tag)  # type: ignore[misc]
            season = getattr(group, "season", "") or ""
            clan_tags_in_group: list[str] = [str(c.tag) for c in group.clans]  # type: ignore[union-attr]
            war_pairs: list[tuple[int, str]] = []
            for round_no, round_tags in enumerate(group.rounds, start=1):
                for tag in round_tags:
                    if tag and tag != "#0":
                        war_pairs.append((round_no, tag))
            return (season, war_pairs, ",".join(clan_tags_in_group))  # type: ignore[arg-type]
        except coc.NotFound:
            return None  # group expired or clan never in CWL
        except coc.Maintenance:
            log.warning(f"[MAINTENANCE] API under maintenance, backing off 30s")
            await asyncio.sleep(30)
            return None
        except Exception as ex:
            log.debug(f"[SKIP] {clan_tag}: {ex}")
            return None


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(args: argparse.Namespace) -> None:
    season = args.season
    db_path = CONFIG.db_path
    output_path = os.path.join(
        os.path.dirname(__file__), OUTPUT_FILENAME.format(season=season)
    )

    log.info(f"Season : {season}")
    log.info(f"DB     : {db_path}")
    log.info(f"Output : {output_path}")

    # Load state from DB
    log.info("Loading incomplete clans from DB...")
    incomplete = load_incomplete_clans(db_path, season, args.tracked_only)
    log.info(f"  → {len(incomplete):,} clans with <7 wars")

    known_tags = load_known_war_tags(db_path, season)
    log.info(f"  → {len(known_tags):,} war_tags already in DB")

    # Load already-harvested war_tags from output file (resume support)
    harvested_tags: set[str] = set()
    if os.path.exists(output_path):
        with open(output_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    parts = line.split("\t")
                    if parts:
                        harvested_tags.add(parts[0])
        log.info(f"  → {len(harvested_tags):,} war_tags already in output file (resume)")

    all_known = known_tags | harvested_tags

    # ── API login ────────────────────────────────────────────────────────────
    email = os.getenv("COC_API_EMAIL_DEV") or CONFIG.coc_email
    password = os.getenv("COC_API_PASSWORD_DEV") or CONFIG.coc_password

    client = coc.Client(
        key_count=10,
        throttler=coc.BatchThrottler,
        throttle_limit=100,
    )
    await client.login(email, password)
    log.info("Logged in to CoC API")

    sem = asyncio.Semaphore(args.concurrency)

    # Track which clans' groups we've already fetched (dedup by group)
    processed_clan_tags: set[str] = set()
    new_tags: list[tuple[str, str]] = []  # (war_tag, metadata_line)

    counters = {
        "fetched": 0,
        "expired": 0,
        "errors": 0,
        "skipped_dedup": 0,
        "new_tags": 0,
        "known_tags": 0,
    }

    total = len(incomplete)
    log.info(f"Starting group fetches (concurrency={args.concurrency})...")

    # Process in batches to give progress updates and allow early stop
    BATCH = 200
    for batch_start in range(0, total, BATCH):
        if _stop:
            log.info("[STOP] Stopping as requested.")
            break

        batch = incomplete[batch_start : batch_start + BATCH]

        # Filter out already-processed group members
        to_fetch = [t for t in batch if t not in processed_clan_tags]
        counters["skipped_dedup"] += len(batch) - len(to_fetch)

        if not to_fetch:
            continue

        tasks = [fetch_group_tags(client, tag, sem) for tag in to_fetch]
        results = await asyncio.gather(*tasks)

        for clan_tag, result in zip(to_fetch, results):
            if result is None:
                counters["expired"] += 1
                processed_clan_tags.add(clan_tag)  # don't retry
                continue

            group_season, war_pairs, group_clan_csv = result
            # Mark all 8 group members as processed so we skip them later
            for gt in group_clan_csv.split(","):
                processed_clan_tags.add(gt)

            counters["fetched"] += 1
            for round_no, war_tag in war_pairs:
                if war_tag in all_known:
                    counters["known_tags"] += 1
                    continue
                all_known.add(war_tag)
                new_tags.append((war_tag, f"{war_tag}\t{clan_tag}\t{round_no}\t{group_season}"))
                counters["new_tags"] += 1

        progress = min(batch_start + BATCH, total)
        pct = 100 * progress / total
        log.info(
            f"  [{progress:,}/{total:,} = {pct:.0f}%] "
            f"fetched={counters['fetched']:,}  expired={counters['expired']:,}  "
            f"dedup_skip={counters['skipped_dedup']:,}  new_tags={counters['new_tags']:,}"
        )

    await client.close()

    # ── Write output ─────────────────────────────────────────────────────────
    log.info(f"\nTotal new war_tags discovered: {counters['new_tags']:,}")
    log.info(f"Total expired/failed groups  : {counters['expired']:,}")

    if args.dry_run:
        log.info("[DRY RUN] Not writing to file.")
        return

    if not new_tags:
        log.info("No new tags to write.")
        return

    # Open in append mode so re-runs accumulate without losing previous results
    is_new_file = not os.path.exists(output_path)
    with open(output_path, "a", encoding="utf-8") as fh:
        if is_new_file:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            fh.write(f"# CWL war_tags harvest — season {season} — {ts}\n")
            fh.write("# Columns: war_tag  clan_tag  round_no  group_season\n")
            fh.write("# war_tags already in DB are excluded from this file\n\n")
        for _, line in new_tags:
            fh.write(line + "\n")

    log.info(f"Written {len(new_tags):,} new war_tag lines → {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Harvest CWL war_tags from API")
    parser.add_argument("--season", default=SEASON)
    parser.add_argument("--concurrency", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--tracked-only", action="store_true",
                        help="Only fetch groups for actively tracked clans (track_war_updates=1)")
    args = parser.parse_args()
    asyncio.run(main(args))
