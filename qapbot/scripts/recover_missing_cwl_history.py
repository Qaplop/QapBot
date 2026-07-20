"""
Recovery script: write war_attacks / war_summary rows for CWL wars that are
in archive/ but missing from the database.

Root cause (discovered 2026-04-07):
    _append_current_war_to_history() moved the temp file to archive/ immediately
    (os.rename/os.replace) while the DB write was held in a deferred batch queue.
    Bot restarts on April 5-6 (multiple version bumps) flushed the in-memory
    queue without committing, leaving 492 CWL wars in archive but absent from
    war_attacks.

What this script does:
1. Reads cwl_discrepancies.txt produced by verify_all_cwl_history.py and
   extracts archive filenames flagged as "History missing".
2. For each file, loads the JSON from archive/ and calls
   build_per_attack_rows + build_war_summary to reconstruct the DB rows.
3. Writes the rows directly to war_attacks / war_summary using the same
   INSERT OR IGNORE / INSERT OR REPLACE logic as the normal finalization path
   (idempotent: safe to re-run).

Usage:
    python -m qapbot.scripts.recover_missing_cwl_history [--dry-run]

Options:
    --dry-run   Parse and report but do NOT write anything to the database.
    --limit N   Stop after recovering N wars (for testing).

Requirements:
    • qapbot/ must be importable (run from workspace root or use -m)
    • cwl_discrepancies.txt must exist in the same directory as this script
      (produced by verify_all_cwl_history.py)
    • archive/ directory must be accessible from cwd (or set ARCHIVE_DIR env var)

Hot/history DB split (2026-07): this script writes unqualified (i.e. to the
``main`` schema) via a bare sqlite3 connection to CONFIG.db_path. This is safe
regardless of how old the recovered war is: any row inserted into ``main``
that's older than the hot retention window (current + previous calendar
month) is automatically swept into ``history`` by the next monthly migration
run (``WarHistoryDB.monthly_history_migration()``) — no special-casing needed
here. All bot-facing reads already UNION main+history, so recovered rows are
visible immediately either way.
"""
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false
import json
import logging
import os
import re
import glob
import signal
import sqlite3
import sys
from typing import List, Optional, Tuple

load_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../..'))
if load_path not in sys.path:
    sys.path.insert(0, load_path)

from dotenv import load_dotenv  # type: ignore[import]
load_dotenv()

from qapbot.config import CONFIG
import QBcsvhandling  # type: ignore[import]

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)

# ── CTRL+C handling ─────────────────────────────────────────────────────────
_stop_requested = False

def _handle_ctrl_c(signum: int, frame: object) -> None:
    global _stop_requested
    if _stop_requested:
        print("\n[CTRL+C] Forced exit.")
        sys.exit(1)
    _stop_requested = True
    print("\n[CTRL+C] Will stop after current entry (press again to force exit)...")

signal.signal(signal.SIGINT, _handle_ctrl_c)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")


def _parse_discrepancy_files(discrepancy_txt: str) -> List[str]:
    """Return a list of archive filenames that have 'History missing' in the report."""
    filenames: List[str] = []
    if not os.path.exists(discrepancy_txt):
        print(f"[ERROR] Discrepancy file not found: {discrepancy_txt}")
        return filenames
    current_file: Optional[str] = None
    with open(discrepancy_txt, encoding='utf-8') as f:
        for line in f:
            line = line.rstrip()
            # Line like:  File: 2202LPPJ8_2R090L280_202604050853_war_data.json
            m = re.match(r'^File:\s+(\S+)', line)
            if m:
                current_file = m.group(1)
            elif 'History missing' in line and current_file:
                filenames.append(current_file)
                current_file = None
    return filenames


def _resolve_archive_file(archive_dir: str, filename: str) -> str:
    """Resolve archive filename across sharded and legacy flat layouts."""
    direct_path = os.path.join(archive_dir, filename)
    if os.path.exists(direct_path):
        return direct_path

    clan_safe = filename.split('_', 1)[0].upper()
    if clan_safe:
        try:
            shard_dir = QBcsvhandling.get_war_shard_dir(clan_safe, archive_dir)
            shard_path = os.path.join(shard_dir, filename)
            if os.path.exists(shard_path):
                return shard_path
        except Exception:
            pass

    # Fallback for unexpected layouts: scan shard_* subdirs once by filename.
    for sd in glob.glob(os.path.join(archive_dir, "shard_*")):
        candidate = os.path.join(sd, filename)
        if os.path.exists(candidate):
            return candidate

    return direct_path


def _recover_one(
    archive_file: str,
    conn: sqlite3.Connection,
    dry_run: bool,
) -> Tuple[bool, str]:
    """
    Load JSON from archive/, build DB rows, write them.

    Returns (success, message).
    """
    filename = os.path.basename(archive_file)

    # Derive clan_tag from filename (first segment, no #)
    parts = filename.replace('_war_data.json', '').split('_', 2)
    if len(parts) < 2:
        return False, f"Filename format unrecognised: {filename}"
    clan_safe = parts[0].upper()
    clan_tag = f'#{clan_safe}'

    # Load JSON
    try:
        with open(archive_file, 'r', encoding='utf-8') as f:
            war_obj = json.load(f)
    except Exception as e:
        return False, f"JSON load failed: {e}"

    # Build per-attack rows and summary
    try:
        atk_rows = QBcsvhandling.build_per_attack_rows(war_obj, clan_tag, for_finalization=True)
        summary = QBcsvhandling.build_war_summary(war_obj, clan_tag)
    except Exception as e:
        return False, f"build_per_attack_rows/build_war_summary failed: {e}"

    if not atk_rows and not summary:
        return False, "No attack rows and no summary (0-attack war or parse failure)"

    war_id = atk_rows[0]['WarID'] if atk_rows else (summary.get('war_id') if summary else 'UNKNOWN')

    if dry_run:
        return True, f"[DRY-RUN] Would write {len(atk_rows)} attack rows, summary={'yes' if summary else 'no'} for war {war_id}"

    # Check if already in DB (idempotency guard)
    cur = conn.execute(
        "SELECT COUNT(*) AS cnt FROM war_attacks WHERE clan_tag = ? AND war_id = ?",
        (clan_tag, war_id),
    )
    row = cur.fetchone()
    existing = row["cnt"] if row else 0
    if existing > 0:
        return True, f"Already in DB ({existing} rows) for war {war_id} — skipped"

    # Write attack rows
    if atk_rows:
        conn.executemany("""
            INSERT OR IGNORE INTO war_attacks
            (war_id, clan_tag, date, player_name, player_tag, th_level,
             map_position, attack_order, stars, destruction, defender_tag,
             defender_th, defender_map_position, duration, is_fresh,
             times_defended, best_def_destruction,
             max_attacks, missed_attacks, defensive_stars)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            (
                r["WarID"], clan_tag, r["Date"], r["Player"], r["PlayerID"],
                r["TH_lvl"], r.get("map_position", 0), r["attack_order"],
                r["stars"], r["destruction"], r["defender_tag"],
                r.get("defender_th", 0), r.get("defender_map_position", 0),
                r.get("duration", 0), r.get("is_fresh", -1),
                r.get("times_defended", 0), r.get("best_def_destruction", 0.0),
                r["Max_Attacks"], r["Missed_Attacks"], r["Defensive_Stars"],
            )
            for r in atk_rows
        ])

    # Write summary
    if summary:
        conn.execute("""
            INSERT OR REPLACE INTO war_summary
            (war_id, clan_tag, opponent_tag, opponent_name, clan_stars,
             opponent_stars, clan_destruction, opp_destruction, team_size,
             attacks_per_member, war_type, is_cwl, cwl_season, result, date,
             clan_lineup_json, opp_lineup_json,
             clan_attacks_used, opp_attacks_used)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            summary["war_id"], clan_tag,
            summary.get("opponent_tag", ""),
            summary.get("opponent_name", ""),
            summary.get("clan_stars", 0),
            summary.get("opponent_stars", 0),
            summary.get("clan_destruction", 0.0),
            summary.get("opp_destruction", 0.0),
            summary.get("team_size", 15),
            summary.get("attacks_per_member", 2),
            summary.get("war_type", "random"),
            1 if summary.get("is_cwl") else 0,
            summary.get("cwl_season", ""),
            summary.get("result", ""),
            summary["date"],
            summary.get("clan_lineup_json", "[]"),
            summary.get("opp_lineup_json", "[]"),
            summary.get("clan_attacks_used", 0),
            summary.get("opp_attacks_used", 0),
        ))

    conn.commit()
    return True, f"Wrote {len(atk_rows)} attack rows + summary={'yes' if summary else 'no'} for war {war_id}"


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    dry_run = '--dry-run' in sys.argv
    limit: Optional[int] = None
    for i, arg in enumerate(sys.argv):
        if arg == '--limit' and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])

    print("=" * 72)
    print("CWL History Recovery — writes missing war_attacks rows from archive/")
    print("=" * 72)
    if dry_run:
        print("[DRY-RUN MODE] No DB writes will be made.\n")

    scripts_dir = os.path.dirname(os.path.abspath(__file__))
    discrepancy_txt = os.path.join(scripts_dir, "cwl_discrepancies.txt")
    archive_dir = os.environ.get("ARCHIVE_DIR", os.path.join("archive"))

    print(f"Discrepancy file : {discrepancy_txt}")
    print(f"Archive dir      : {archive_dir}")
    print(f"Database         : {CONFIG.db_path}\n")

    # Parse filenames from discrepancy report
    filenames = _parse_discrepancy_files(discrepancy_txt)
    if not filenames:
        print("No 'History missing' entries found in discrepancy report — nothing to recover.")
        return
    print(f"Found {len(filenames)} wars to recover")
    if limit:
        filenames = filenames[:limit]
        print(f"[--limit] Processing only first {limit}")

    # Open DB connection
    if not dry_run:
        conn = sqlite3.connect(CONFIG.db_path)
        conn.row_factory = sqlite3.Row
        _apply_pragmas(conn)
        print(f"Opened DB connection\n")
    else:
        conn = None  # type: ignore[assignment]

    recovered = 0
    skipped = 0
    failed = 0
    failed_list: List[str] = []

    for i, filename in enumerate(filenames, 1):
        if _stop_requested:
            print(f"\n[CTRL+C] Stopped after {i-1} entries.")
            break

        archive_file = _resolve_archive_file(archive_dir, filename)
        if not os.path.exists(archive_file):
            print(f"[{i}/{len(filenames)}] MISSING FILE: {filename}")
            failed += 1
            failed_list.append(filename)
            continue

        ok, msg = _recover_one(archive_file, conn, dry_run)  # type: ignore[arg-type]
        prefix = f"[{i}/{len(filenames)}]"
        if ok:
            if 'Already in DB' in msg or 'DRY-RUN' in msg:
                skipped += 1
                print(f"{prefix} SKIP  {filename}: {msg}")
            else:
                recovered += 1
                print(f"{prefix} OK    {filename}: {msg}")
        else:
            failed += 1
            failed_list.append(filename)
            print(f"{prefix} FAIL  {filename}: {msg}")

    if conn:
        conn.close()

    print("\n" + "=" * 72)
    print("RECOVERY SUMMARY")
    print("=" * 72)
    print(f"Recovered (new rows written) : {recovered}")
    print(f"Skipped   (already in DB)   : {skipped}")
    print(f"Failed    (error/no data)   : {failed}")
    if failed_list:
        print(f"\nFailed files ({len(failed_list)}):")
        for fn in failed_list[:20]:
            print(f"  {fn}")
        if len(failed_list) > 20:
            print(f"  ... and {len(failed_list) - 20} more")
    print("=" * 72)
    print("\nNote: Run verify_all_cwl_history.py again to confirm all recovered.")


if __name__ == '__main__':
    main()
