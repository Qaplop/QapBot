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
from datetime import datetime
from pathlib import Path

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
    return parser.parse_args()


def find_gaps(log_path: Path, top_n: int) -> None:
    gaps: list[tuple[float, str, str, str, str]] = []  # (seconds, ts_a, ts_b, line_a, line_b)

    prev_ts: datetime | None = None
    prev_line: str = ""
    total_lines = 0
    matched_lines = 0

    with log_path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            total_lines += 1
            m = TIMESTAMP_RE.match(line)
            if not m:
                continue
            matched_lines += 1
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
    print(f"Lines    : {total_lines:,} total, {matched_lines:,} with timestamps")
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
    find_gaps(log_path, args.top)
