#!/usr/bin/env python3
"""Scan archive directories for war JSON files whose state is NOT 'war_ended'.

Usage:
    python qapbot/scripts/scan_archive_states.py [--archive-dir DIR ...] [--limit N]

Defaults to `${PROD_BOT_ROOT}/archive` and `${PROD_BOT_ROOT}/archive_old` (production),
or project-relative `archive/` and `archive_old/` when running in DEV. When using `PROD_DATA_DIR` as the
production bot root, archive locations under the SSD are typically `${PROD_DATA_DIR}/data/archive`.
Both directories are expected to contain shard_N/ sub-directories (and
archive_old may also contain unsharded files at the root level).

Per non-war_ended file the script calculates:
    time_remaining = end_time(JSON) - mtime(file)
i.e. how many minutes of war were still left when the file was archived.
Positive = war not over yet (premature archive).
Negative = war had already ended (state in JSON was not updated).

Results are bucketed:
    already_over  : time_remaining <= 0
    < 1 h         : 0 < remaining <= 60 min
    1–2 h         : 60 < remaining <= 120 min
    >= 2 h        : remaining > 120 min
    unknown       : end_time could not be parsed

Output: per-file detail + summary with time-remaining buckets.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple, cast

# Regex matching coc.py Timestamp repr stored in JSON end_time / start_time fields.
_TS_RE = re.compile(
    r'datetime\.datetime\((\d+),\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)(?:,\s*(\d+))?'
)


def _parse_ts(raw: Any) -> Optional[datetime]:
    """Parse a coc.py Timestamp repr string into a UTC-aware datetime."""
    if not raw:
        return None
    m = _TS_RE.search(str(raw))
    if not m:
        return None
    try:
        y, mo, d, h, mi = (int(x) for x in m.groups()[:5])
        sec = int(m.group(6)) if m.group(6) is not None else 0
        return datetime(y, mo, d, h, mi, sec, tzinfo=timezone.utc)
    except Exception:
        return None


def _time_bucket(remaining_minutes: Optional[float]) -> str:
    """Map minutes-remaining to a display bucket label."""
    if remaining_minutes is None:
        return "unknown"
    if remaining_minutes <= 0:
        return "already_over"
    if remaining_minutes <= 60:
        return "< 1 h"
    if remaining_minutes <= 120:
        return "1–2 h"
    return ">= 2 h"


# Result tuple: (filepath, state, attack_count, remaining_minutes_or_None)
ScanResult = Tuple[str, str, int, Optional[float]]


def _collect_files(base_dir: str) -> List[Tuple[str, float]]:
    """Recursively collect all ``*_war_data.json`` paths + cached mtime.

    Uses ``os.scandir()`` exclusively so each directory entry is stat()d once
    and the mtime is kept in memory — no second ``getmtime()`` call per file
    is needed during the processing phase.

    Returns a list of ``(absolute_path, mtime_as_float)`` pairs.
    """
    collected: List[Tuple[str, float]] = []
    stack = [base_dir]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif entry.name.endswith("_war_data.json"):
                        try:
                            mtime = entry.stat(follow_symlinks=False).st_mtime
                        except OSError:
                            mtime = 0.0
                        collected.append((entry.path, mtime))
        except OSError:
            pass
    return collected


def scan_directory(base_dir: str) -> Tuple[List[ScanResult], int]:
    """Scan *base_dir* for ``*_war_data.json`` files with state != war_ended.

    Two-phase approach:
    1. Single ``os.scandir()`` pass — collects all file paths + mtimes into
       memory (no redundant stat calls later).
    2. Sequential JSON reads from that in-memory list.

    Returns ``(bad_files, total_count)`` where *bad_files* is a list of
    ``(filepath, state, attack_count, remaining_minutes)`` tuples and
    *total_count* is the number of war JSON files found in total.
    remaining_minutes is None when end_time cannot be parsed.
    """
    if not os.path.isdir(base_dir):
        print(f"  [SKIP] {base_dir} does not exist or is not a directory")
        return [], 0

    # ── Phase 1: collect all paths + mtimes in one scandir pass ──────
    print(f"  Collecting file list ...")
    file_list = _collect_files(base_dir)
    total = len(file_list)
    print(f"  Found {total} war files — reading ...")

    # ── Phase 2: read each JSON once, using the cached mtime ──────────
    results: List[ScanResult] = []
    for idx, (fp, mtime) in enumerate(file_list):
        try:
            with open(fp, "r", encoding="utf-8") as fh:
                obj = json.load(fh)
            state = str(obj.get("state", "MISSING")).lower().strip()
            if state == "war_ended":
                pass  # counted but not stored
            else:
                # Count total attacks for context
                atk = 0
                for side in ("clan", "opponent"):
                    side_obj: Any = obj.get(side)
                    if isinstance(side_obj, dict):
                        side_typed = cast(Dict[str, Any], side_obj)
                        members: List[Dict[str, Any]] = cast(
                            List[Dict[str, Any]], side_typed.get("members") or []
                        )
                        for m in members:
                            attacks: List[Any] = cast(List[Any], m.get("attacks") or [])
                            atk += len(attacks)

                # Time-remaining = end_time(JSON) − mtime (from scandir cache)
                remaining_minutes: Optional[float] = None
                end_dt = _parse_ts(obj.get("end_time"))
                if end_dt is None:
                    start_dt = _parse_ts(obj.get("start_time"))
                    if start_dt is not None:
                        end_dt = start_dt + timedelta(hours=24)
                if end_dt is not None and mtime:
                    archive_dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
                    remaining_minutes = (end_dt - archive_dt).total_seconds() / 60.0

                results.append((fp, state, atk, remaining_minutes))
        except (json.JSONDecodeError, OSError) as exc:
            results.append((fp, f"READ_ERROR({exc})", -1, None))

        # Progress indicator every 10 000 files
        if (idx + 1) % 10_000 == 0:
            bucket_counts: Counter[str] = Counter(
                _time_bucket(rem) for _, _, _, rem in results
            )
            bucket_str = "  ".join(
                f"{b}: {bucket_counts[b]}"
                for b in ["already_over", "< 1 h", "1–2 h", ">= 2 h", "unknown"]
                if bucket_counts[b]
            )
            print(f"    ... {idx + 1}/{total} files | {len(results)} non-war_ended | {bucket_str}")

    return results, total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive-dir",
        nargs="+",
        default=[
            r"G:\Bots\QapBot\archive",
            r"G:\Bots\QapBot\archive_old",
        ],
        help="Directories to scan (default: PROD archive + archive_old)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Stop after N non-war_ended files (0 = unlimited)",
    )
    args = parser.parse_args()

    all_results: List[ScanResult] = []
    total_scanned = 0

    for d in args.archive_dir:
        print(f"Scanning {d} ...")
        found, dir_total = scan_directory(d)
        total_scanned += dir_total
        all_results.extend(found)
        print(f"  {dir_total} total files, {len(found)} non-war_ended")

        if args.limit and len(all_results) >= args.limit:
            all_results = all_results[: args.limit]
            break

    # ── Per-file detail ──────────────────────────────────────────────
    if all_results:
        print(f"\n{'='*80}")
        print(f"Non-war_ended archive files ({len(all_results)}):")
        print(f"{'='*80}")
        for fp, state, atk, rem in sorted(all_results):
            atk_str = f"{atk} attacks" if atk >= 0 else "unreadable"
            if rem is None:
                rem_str = "  unknown time"
            elif rem <= 0:
                rem_str = f"{abs(rem/60):5.1f}h AFTER end"
            else:
                rem_str = f"{rem/60:5.1f}h to end"
            print(f"  [{state:>15}]  {atk_str:>14}  {rem_str:>18}  {os.path.basename(fp)}")

    # ── Summary ──────────────────────────────────────────────────────
    state_counts: Counter[str] = Counter(s for _, s, _, _ in all_results)
    bucket_counts: Counter[str] = Counter(
        _time_bucket(rem) for _, _, _, rem in all_results
    )

    print(f"\n{'='*80}")
    print("SUMMARY")
    print(f"{'='*80}")
    print(f"Total archive files scanned : {total_scanned}")
    print(f"Non-war_ended files found   : {len(all_results)}")

    print(f"\nBy state:")
    for state, cnt in state_counts.most_common():
        print(f"  {state:>20} : {cnt}")

    print(f"\nBy time remaining when archived (positive = war still going):")
    bucket_order = ["already_over", "< 1 h", "1–2 h", ">= 2 h", "unknown"]
    for bucket in bucket_order:
        cnt = bucket_counts.get(bucket, 0)
        if cnt:
            print(f"  {bucket:>15} : {cnt}")

    if not all_results:
        print("  All archive files have state=war_ended ✓")
    print()


if __name__ == "__main__":
    main()

