"""
Move old archived war JSON files from archive/ to archive_old/ (SSD staging).

Behavior:
- Scans archive/ recursively for *_war_data.json files.
- Moves files older than the configured age threshold.
- Preserves the relative shard directory structure (for example,
  archive/shard_3/foo.json -> archive_old/shard_3/foo.json).
- Uses os.replace() for atomic same-filesystem moves (source and dest must share a mount point).
- archive_old/ sits on the same SSD volume as archive/ so os.replace() is a pure
  metadata rename — no data is copied, no SSD wear incurred.
- ArchiveOld.bat then compresses archive_old/ into archive_compressed/ on the HDD.

Default paths are workspace-local:
- source: archive
- destination: archive_old

Example:
    python -m qapbot.move_old_archive_files
    python -m qapbot.move_old_archive_files --days 3 --dry-run
    python -m qapbot.move_old_archive_files --source "${PROD_DATA_DIR}/archive" --dest "${PROD_DATA_DIR}/archive_old"
"""
from __future__ import annotations

import argparse
import os
import sys
import time


def _iter_war_files(source_dir: str):
    for root, _, files in os.walk(source_dir):
        for name in files:
            if name.endswith("_war_data.json"):
                yield os.path.join(root, name)


def run(source_dir: str, dest_dir: str, days: float = 7.0) -> dict[str, int]:
    """
    Programmatic entry point for the nightly maintenance pipeline.

    Moves war JSON files older than *days* from *source_dir* to *dest_dir*,
    preserving the relative shard sub-directory structure.  Safe to call from
    a background thread; returns a result dict instead of printing.

    Returns:
        dict with keys:
            moved       (int)  — number of files successfully moved
            skipped     (int)  — number of files that could not be moved
            total_bytes (int)  — combined size of moved files
    """
    if not os.path.isdir(source_dir):
        raise FileNotFoundError(f"[ARCHIVE-MOVE] Source directory not found: {source_dir}")

    os.makedirs(dest_dir, exist_ok=True)
    cutoff_ts = time.time() - (days * 86400)
    moved = 0
    skipped = 0
    total_bytes = 0

    for src_path in _iter_war_files(source_dir):
        try:
            stat_result = os.stat(src_path)
        except OSError:
            skipped += 1
            continue

        if stat_result.st_mtime >= cutoff_ts:
            continue

        rel_path = os.path.relpath(src_path, source_dir)
        dst_path = os.path.join(dest_dir, rel_path)
        dst_parent = os.path.dirname(dst_path)

        try:
            os.makedirs(dst_parent, exist_ok=True)
            os.replace(src_path, dst_path)
            moved += 1
            total_bytes += stat_result.st_size
        except OSError:
            skipped += 1

    return {"moved": moved, "skipped": skipped, "total_bytes": total_bytes}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        default=os.path.join(os.path.dirname(__file__), "..", "..", "archive"),
        help="Source archive directory (default: ./archive)",
    )
    parser.add_argument(
        "--dest",
        default=os.path.join(os.path.dirname(__file__), "..", "..", "archive_old"),
        help="Destination archive_old directory (default: ./archive_old)",
    )
    parser.add_argument(
        "--days",
        type=float,
        default=7.0,
        help="Move files older than this many days based on mtime (default: 7)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List files that would be moved without changing anything.",
    )
    args = parser.parse_args()

    source_dir = os.path.abspath(args.source)
    dest_dir = os.path.abspath(args.dest)

    if not os.path.isdir(source_dir):
        print(f"[ERROR] Source directory not found: {source_dir}")
        sys.exit(1)

    os.makedirs(dest_dir, exist_ok=True)

    cutoff_ts = time.time() - (args.days * 86400)
    moved = 0
    skipped = 0
    total_bytes = 0

    print(f"Source:      {source_dir}")
    print(f"Destination: {dest_dir}")
    print(f"Threshold:   older than {args.days:g} day(s)")
    if args.dry_run:
        print("Mode:        DRY RUN")

    for src_path in _iter_war_files(source_dir):
        try:
            stat_result = os.stat(src_path)
        except OSError as exc:
            print(f"[WARN] Cannot stat {src_path}: {exc}")
            skipped += 1
            continue

        if stat_result.st_mtime >= cutoff_ts:
            continue

        rel_path = os.path.relpath(src_path, source_dir)
        dst_path = os.path.join(dest_dir, rel_path)
        dst_parent = os.path.dirname(dst_path)

        if args.dry_run:
            print(f"[DRY-RUN] MOVE {src_path} -> {dst_path}")
            moved += 1
            total_bytes += stat_result.st_size
            continue

        try:
            os.makedirs(dst_parent, exist_ok=True)
            os.replace(src_path, dst_path)
            moved += 1
            total_bytes += stat_result.st_size
        except OSError as exc:
            print(f"[WARN] Failed to move {src_path} -> {dst_path}: {exc}")
            skipped += 1

    print()
    print("=== Summary ===")
    print(f"Moved files:   {moved:,}")
    print(f"Skipped files: {skipped:,}")
    print(f"Moved bytes:   {total_bytes:,}")


if __name__ == "__main__":
    main()