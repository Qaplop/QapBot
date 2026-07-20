# Archive-Directory Full-Rescan Performance — Analysis & Deferred Optimization

Date: 2026-07-18
Status: **Deferred** — full rescan kept intentionally. Revisit if archive_scan cost
grows significantly above the current ~1s baseline (see "When to revisit" below).

## Background

As part of a broader update-cycle performance investigation (2026-07-18, see
changelog.txt entries for that date for the full story: gc.collect() fix,
clan-categorization-loop yield fix, PHASE-2 strptime fix), the PRE-SCAN step in
`main()` (QapBot.py) was instrumented with `[PRE-SCAN-TIMING]`:

```
[PRE-SCAN-TIMING] temp_scan=0.152-0.157s (~26K files, ~26K clans)
                   archive_scan=0.976-0.993s (~320K files)
                   total=1.13-1.15s
```

`archive_scan` (an `os.scandir()` walk over the entire `archive/` directory tree,
all shards) is the dominant cost — confirmed consistent across 8 captured cycles
(2 different PROD deployments) on 2026-07-18. Both scans already run inside
`asyncio.to_thread()`, so **this is not a blocking/event-loop-freeze bug** — it's
real disk-I/O wall-clock time on server-machine/eSATA storage, correctly backgrounded.

## Why a persistent/incremental archive-filename index was considered

Instead of a full `os.scandir()` rescan every cycle, the idea was to maintain a
`CACHE`-level `set[str]` of archive filenames incrementally (add on
finalization, remove on nightly archive→archive_old move), similar in spirit to
the clan-categorization-loop and PHASE-2 fixes already applied that day.

## Risk analysis: what happens when archive files are removed / added / changed?

### Removed (nightly `_archive_move_nightly()` moves archive/ → archive_old/)
**Low risk — already self-healing.** The only consumer that reads the set,
`_process_war_history()` (QBhelperfunctions.py ~line 4374), never trusts a
"found" result blindly:
```python
_archive_exists = (_basename in archive_set) if archive_set is not None else os.path.exists(archive_file)
if _archive_exists and not os.path.exists(archive_file):
    # falls back to a 10-shard search; if truly gone, _archive_exists = False (treat as new)
```
A stale/leftover "exists" entry gets double-checked via `os.path.exists()` before
being trusted, with a full shard-search fallback. This direction is safe today
and would remain safe with a persistent index.

### Changed (late-attack content replace, same filename)
**Non-issue.** The set only tracks filenames, not content/hashes. A content-only
replace (same filename) never needs to add or remove a set entry.

### Added (war finalization moves temp/ → archive/) — **the real problem**
Traced every `archive_set.add(...)` call site. Found the **common/normal**
finalization path does NOT update the set:
- `QapBot.py` `_do_deferred_moves()` (~line 1455) — Phase-3's batched/deferred
  file moves (the standard path per the P0-batch design) calls
  `os.replace(src, dst)` for every deferred move and **never calls
  `archive_set.add()`**. Harmless *today* only because the set is thrown away
  and rebuilt from a fresh disk scan every single cycle.
- Bonus wrinkle: `QBhelperfunctions.py` `_archive_and_log_war_file()`
  (~line 4993) calls `archive_set.add(_basename)` **immediately**, even though
  the actual move may be deferred via `CACHE.db_manager.defer_file_move(...)`
  and only executes later *if* the batch DB flush succeeds. If that flush
  fails, the set would (transiently, harmlessly today) claim a file is archived
  when it's still sitting in `temp/`.

If the set became **persistent across cycles**, gap #1 means it would
permanently and silently miss every batch-finalized war file — accumulating
false negatives forever, with no periodic rescan to self-correct.

A false negative here is dangerous: unlike the "removed" direction, there is
**no defensive check**. If `_archive_exists` is wrongly `False`,
`_process_war_history()` skips its content-compare step entirely — including
the **regression guard** that exists specifically to prevent overwriting a
more-complete archived war with a less-complete one
(`if _temp_atk < _arch_atk: discard temp, preserve archive`).
`os.replace()`/`os.rename()` silently overwrite an existing destination on
POSIX with no error, so a false negative could theoretically let a stale/
incomplete re-fetch silently clobber a correctly-archived war file, bypassing
that exact safeguard.

## Decision: keep the full rescan for now

The persistent-index approach is **not meaningfully safer** than the earlier
clan-categorization "pollable tags set" idea (also deferred that day for
similar reasons) — it needs the same class of careful, cross-cutting
verification work:
1. Fix `_do_deferred_moves()` to also `archive_set.add()` after each successful move.
2. Fix the optimistic-add-before-confirmed-move timing in `_archive_and_log_war_file()`.
3. Add a removal hook in `_archive_move_nightly()`.
4. Add a periodic full-reconciliation safety net (no defensive check exists
   today for the false-negative direction, unlike false-positives).

Given the scan is already non-blocking and costs only ~1s out of a ~15s
steady-state cycle, this was deferred rather than implemented.

## When to revisit

Archive size grows significantly during CWL season — potentially up to ~4x the
current baseline (~320K files → up to ~1.3M files). If `archive_scan` cost
scales roughly linearly with file count (consistent with the `os.scandir()`
measurements above), that could push `archive_scan` from ~1s to ~4s+.

**Monitor `[PRE-SCAN-TIMING]` in PROD logs through the next CWL season.** If
`archive_scan` grows significantly above the ~1s baseline (e.g. sustained
2-3s+), revisit implementing the incremental index using the 4-point plan
above, plus the periodic reconciliation safety net.

## References
- changelog.txt, 2026-07-18 entries (gc.collect fix, categorize-loop yield fix,
  PRE-SCAN/PHASE-1.5/PHASE-2 timing instrumentation, PHASE-2 strptime fix)
- backlog.txt: "Monitor archive_scan cost through CWL season" item
- Repo memory `/memories/repo/qapbot_sqlite_attach_pragma_gotcha.md` — GIL /
  blocking-vs-non-blocking lessons from the same investigation
