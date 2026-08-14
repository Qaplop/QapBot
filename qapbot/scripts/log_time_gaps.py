"""log_time_gaps.py — Find the N biggest time gaps between consecutive log lines.

Usage:
    python qapbot/scripts/log_time_gaps.py [--log PATH] [--top N]

Defaults:
    --log data/logs/qapbot_PROD.log
    --top 20
"""

import argparse
import heapq
import re
import sys
from datetime import datetime
from pathlib import Path

# Windows consoles often default to a legacy codepage (cp1252 etc.) that can't encode
# every byte a log line might contain (emoji, non-ASCII clan/player names) — without this,
# print() crashes with UnicodeEncodeError partway through a report instead of finishing it.
# reconfigure() only touches error handling, not the chosen encoding, so this is a no-op on
# a stream that's already UTF-8.
try:
    sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
except (AttributeError, ValueError):
    pass  # stdout isn't a reconfigurable text stream (e.g. redirected to certain pipes)

TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})")
TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S,%f"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find biggest time gaps in a QapBot log file.")
    parser.add_argument(
        "--log",
        default="data/logs/qapbot.log",
        help="Path to the log file (default: data/logs/qapbot.log)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=20,
        help="Number of gaps to report (default: 20)",
    )
    parser.add_argument(
        "--since-marker",
        default=None,
        help="Only consider gaps at/after the LAST line containing this substring (e.g. "
             "'QapBot started.' to scope a report to the most recent restart, ignoring "
             "everything from earlier restarts/days in the same log file).",
    )
    return parser.parse_args()


def find_gaps(log_path: Path, top_n: int, since_marker: str | None = None) -> None:
    gaps: list[tuple[float, str, str, str, str]] = []  # (seconds, ts_a, ts_b, line_a, line_b)

    prev_ts: datetime | None = None
    prev_line: str = ""
    total_lines = 0
    matched_lines = 0
    scanned_lines = 0

    # Two-pass when scoping to a marker: heapq's "top N" bound only works with a strictly
    # forward scan, so find the LAST matching line number first, then re-scan from there —
    # cheap relative to the file read itself, and keeps find_gaps a single straightforward
    # forward pass otherwise.
    start_at_line = 0
    if since_marker:
        last_match_line = None
        with log_path.open(encoding="utf-8", errors="replace") as fh:
            for i, raw in enumerate(fh):
                if since_marker in raw:
                    last_match_line = i
        if last_match_line is None:
            print(f"WARNING: --since-marker {since_marker!r} not found in {log_path} — reporting the whole file.")
        else:
            start_at_line = last_match_line

    with log_path.open(encoding="utf-8", errors="replace") as fh:
        for i, raw in enumerate(fh):
            if i < start_at_line:
                continue
            line = raw.rstrip("\n")
            total_lines += 1
            m = TIMESTAMP_RE.match(line)
            if not m:
                continue
            matched_lines += 1
            scanned_lines += 1
            ts = datetime.strptime(m.group(1), TIMESTAMP_FMT)
            if prev_ts is not None:
                delta = (ts - prev_ts).total_seconds()
                entry = (delta, prev_ts.isoformat(sep=" "), ts.isoformat(sep=" "), prev_line, line)
                if len(gaps) < top_n:
                    heapq.heappush(gaps, entry)
                elif delta > gaps[0][0]:
                    heapq.heapreplace(gaps, entry)
            prev_ts = ts
            prev_line = line

    if not gaps:
        print("No timestamped lines found.")
        return

    # Sort largest-first
    gaps.sort(key=lambda x: x[0], reverse=True)

    print(f"Log file : {log_path}")
    scope = f" (scoped to lines after {'{:,}'.format(start_at_line)}, marker {since_marker!r})" if start_at_line else ""
    print(f"Lines    : {total_lines:,} scanned{scope}, {matched_lines:,} with timestamps")
    print(f"Top {top_n} time gaps (largest first)")
    print("=" * 72)
    for rank, (secs, ts_a, ts_b, line_a, line_b) in enumerate(gaps, 1):
        h = int(secs // 3600)
        m_ = int((secs % 3600) // 60)
        s_ = secs % 60
        if h:
            gap_str = f"{h}h {m_:02d}m {s_:05.2f}s"
        elif m_:
            gap_str = f"{m_}m {s_:05.2f}s"
        else:
            gap_str = f"{s_:.3f}s"
        print(f"\n#{rank:>2}  GAP: {gap_str}")
        print(f"      FROM: {ts_a}  {_snippet(line_a)}")
        print(f"        TO: {ts_b}  {_snippet(line_b)}")
    print()


def _snippet(line: str, max_len: int = 90) -> str:
    """Return the non-timestamp portion of a log line, truncated."""
    # Strip the leading timestamp (29 chars: "YYYY-MM-DD HH:MM:SS,mmm ")
    body = line[29:].strip() if len(line) > 29 else line
    if len(body) > max_len:
        return body[:max_len] + "…"
    return body


if __name__ == "__main__":
    args = parse_args()
    log_path = Path(args.log)
    if not log_path.is_absolute():
        log_path = Path(__file__).resolve().parents[2] / args.log
    if not log_path.exists():
        print(f"ERROR: log file not found: {log_path}")
        raise SystemExit(1)
    find_gaps(log_path, args.top, args.since_marker)
