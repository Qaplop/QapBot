# War File Management - Complete Rules & Cases

## Overview

War file lifecycle management operates in three stages:

**Stage 1: Pre-Save Filter (cache_manager.py)**
- Timestamped filename uniqueness: `{CLAN}_{OPP}_{YYYYMMDDHHMM}_war_data.json`
  - Each war gets a unique filename based on war start time.
  - Prevents a second war against the same opponent from overwriting the temp file
    of an in-progress older war (old pre-2026-04-03 naming had no timestamp → silent
    overwrite caused DB entry to survive but JSON archive to be lost).
- 24-Hour Stale War Check in `CACHE.save_war_object()`
- Prevents saving wars that ended >24 hours ago
- Protects against CoC API returning same old war repeatedly
- Logs: [STALE-WAR-SKIP] for rejected saves

- Fully-Finalized War Skip (additional pre-save guard)
  - If war is already archived AND already present in history DB, skip saving again
  - Logs: [FINALIZED-WAR-SKIP]

- Friendly-War Skip (unconditional pre-save guard)
  - Wars with `type == 'friendly'` are never saved to `data/temp/` at all (regardless of state)
  - Logs: [FRIENDLY-SKIP]

**Stage 2: File Management (QBhelperfunctions.py)**
- `manage_war_files()` function handles complete lifecycle of war files:
  - Detection (current vs old wars)
  - Validation (history checks, state checks)
  - Finalization (append to history database)
  - Delegates DB write + archive handling to QBcsvhandling for new wars
  - Uses QBhelperfunctions archive refresh for late-attack updates

**Stage 3: Archive Logic (QBcsvhandling.py + QBhelperfunctions.py)**
- After successful database write, files are moved to archive with content comparison (QBcsvhandling._append_current_war_to_history):
  - **Archive doesn't exist** → Move temp file to archive
  - **Archive exists + identical content** → Delete temp file (avoid duplicates)
  - **Archive exists + different content** → Replace archive with updated file (late attacks),
    UNLESS the temp file has *fewer* total attacks than the archive (regression guard: an
    older/incomplete re-fetch can never have more attacks than an already-archived war, since
    CoC never removes attacks retroactively) — in that case the temp file is discarded and the
    archive is preserved. Logs: [REGRESSION-GUARD]
- After late-attack checks/updates, temp files are moved/refreshed in archive (QBhelperfunctions._archive_and_log_war_file)
- This ensures:
  - All war data permanently preserved
  - Late attacks properly captured
  - No duplicate identical files

## Decision Tree

```
PRE-SAVE FILTER (before temp file creation):
│
└─ Is war ended >24 hours ago?
   ├─ YES → SKIP JSON save (no file created, [STALE-WAR-SKIP] log)
  └─ NO → (If war_ended) Is war already archived AND already in history DB?
      ├─ YES → SKIP JSON save ([FINALIZED-WAR-SKIP] log)
      └─ NO  → Save to data/temp/ and continue to manage_war_files()

manage_war_files() FILE DISCOVERY:
│
├─ List files via glob: data/temp/shard_N/{SAFE_CLAN_TAG}_*_war_data.json
├─   (temp/ is sharded into 10 subdirectories, same as archive/ — shard chosen
├─    by clan-tag prefix via QBcsvhandling.get_war_shard_dir())
├─ Sort by modification time (oldest → newest)
└─ Identify CURRENT WAR FILE (only if current_opponent_tag provided):
  ├─ newest filename parts[1] (opponent segment) == current_opponent_tag (cleaned)
  ├─   (filename format: {CLAN}_{OPP}_{YYYYMMDDHHMM}_war_data.json)
  ├─ AND newest JSON state in {preparation, in_war, inWar}
  └─ If state check fails (read/JSON error) → treat as old (do not crash)

For each matching war file (excluding current war file, if any):
│
├─ Step 1: Read JSON (manage_war_files)
│  ├─ Read/parse fails? → ARCHIVE (log: [MANAGE-WAR-FILES] Error reading ... - archiving)
│  └─ Read OK → extract: state, type (war_type), file_opponent
│
├─ Step 2: Finalization decision (manage_war_files)
│  ├─ war_type == 'cwl' AND state != war_ended?
│  │   └─ SKIP here (log: [CWL-ORPHAN-SKIP])
│  │      ↳ process_orphaned_cwl_wars() may fetch via get_league_war(war_tag) and update JSON
│  ├─ state == war_ended?
│  │   └─ FINALIZE via _process_war_history()
│  ├─ war_type != 'cwl' AND is_orphaned AND state in {in_war, inWar}?
│  │   └─ FINALIZE via _process_war_history() (orphaned regular war)
│  └─ else → SKIP (not ready)
│
└─ Step 3: _process_war_history() (finalization + dedupe)
  ├─ If archive file exists AND content identical → DELETE temp ([DUPLICATE-SKIP]) and STOP
  ├─ If archive file exists AND content differs AND temp has FEWER attacks than archive
  │   → DELETE temp, preserve archive ([REGRESSION-GUARD]) and STOP
  ├─ Else load/validate JSON structure
  │  ├─ Missing opponent.tag → move to archive and STOP ([ARCHIVE] Moved incomplete ...)
  │  └─ Cannot extract start_time → FAIL (manage_war_files archives the file)
  ├─ Build war_id, then check DB (war_attacks_exist)
  │  ├─ war already in DB → check/update late attacks, then refresh/move archive
  │  └─ war not in DB → append to DB, then move/delete temp based on archive compare
  └─ End
│
└─ End
```

## Rule Matrix

### Rule 0: 24-Hour Stale War Check (Pre-Save Filter)
**Location**: `CACHE.save_war_object()` in cache_manager.py  
**Condition**: War state is 'war_ended' AND end_time is >24 hours in the past  
**Action**: SKIP JSON save (file never created in data/temp/)  
**Reason**: Prevent accumulating deteriorated data from CoC API  
**Why**: CoC API returns same ended war repeatedly if clan hasn't started new war  
**Threshold**: 24 hours - after this, data is considered stale and not worth persisting  
**Log**: `[STALE-WAR-SKIP] Skipping JSON save for {clan} vs {opponent}: war ended X hours ago (>24h threshold)`  
**Example**: War ended 599.5 hours ago (~25 days) - skipped, no temp file created  
**Integration**: Complements Rule 6 (stale war deletion) which removes duplicates already in history

### Rule 1: Current War Protection
**Condition**: Newest file + Same opponent + Active state (preparation/in_war/inWar)  
**Action**: SKIP (never process current war)  
**Reason**: War still in progress, data incomplete  
**Logs**:
- `[MANAGE-WAR-FILES] Identified current active war: {filename} (state={state})` (DEBUG)
- `[MANAGE-WAR-FILES] Skipping current war: {filename}` (DEBUG)

### Rule 2: Non-Matching Filenames (Ignored)
**Condition**: Filename does not match the glob used by `manage_war_files()` (`{SAFE_CLAN_TAG}_*_war_data.json`)  
**Action**: IGNORE (not processed)  
**Reason**: File discovery is glob-based; there is no explicit “invalid filename” validation step in the implementation.

### Rule 3: Corrupted JSON
**Condition**: JSON file cannot be parsed / read  
**Action**: ARCHIVE  
**Reason**: Prevent crashes and prevent retry loops; keep the file for investigation  
**Log**: `[MANAGE-WAR-FILES] Error reading {filename}: {error} - archiving`

### Rule 4: Missing Opponent Data
**Condition**: JSON has no opponent object or opponent.tag is null  
**Action**: ARCHIVE  
**Reason**: Incomplete war data, cannot build war_id  
**Logs**:
- `[PROCESS-WAR-HISTORY] No valid opponent data in {filename} - skipping (likely corrupted or incomplete war data)`
- `[ARCHIVE] Moved incomplete war data {filename} to archive`

### Rule 5: Invalid Timestamp
**Condition**: Cannot parse start_time from JSON  
**Action**: ARCHIVE  
**Reason**: Cannot build war_id without timestamp  
**Logs**:
- `[PROCESS-WAR-HISTORY] Could not extract start time from {filename}`
- `[MANAGE-WAR-FILES] Failed to finalize {filename} - archiving`

### Rule 6: Stale War (Already in History + Archive)
**Condition**: Archive file exists AND temp file content is identical to archive  
**Action**: DELETE temp file (exact duplicate; no reprocessing)  
**Reason**: API (or a prior save) produced an identical JSON file again; avoid repeated processing/I/O  
**Log**: `[DUPLICATE-SKIP] Deleted duplicate temp file {filename} - identical to archive`

### Rule 7: War in History but Archive Missing
**Condition**: War_id exists in history database  
**Action**: Check for late attacks → refresh archive handling  
**Reason**: Keep DB and archive consistent; late attacks can arrive after initial finalization  
**Log**: `[LATE-CHECK] War {war_id} already in history - checking for late attacks` (DEBUG)

### Rule 8: Orphaned CWL War (In Progress)
**Condition**: Different opponent + CWL + State NOT war_ended  
**Action**: SKIP finalization in manage_war_files(); handled by process_orphaned_cwl_wars() which fetches final data via API, updates JSON, then triggers finalization  
**Reason**: CWL wars can be re-fetched by war_tag; this path recovers complete final results when the bot was offline  
**API Call**: CACHE.get_league_war(war_tag) (parallel in process_orphaned_cwl_wars)  
**Logs**: `[CWL-ORPHAN-SKIP] ... will be fetched via get_league_war() in process_orphaned_cwl_wars()` and `[ORPHANED-CWL] ...`

### Rule 9: Orphaned CWL War (Already Ended)
**Condition**: Different opponent + CWL + State = war_ended  
**Action**: FINALIZE with existing JSON → ARCHIVE  
**Reason**: War already ended in JSON, no late attacks possible  
**Log**: `[FINALIZE-OLD-WAR] Processing {filename} - state=war_ended, is_cwl=True`

### Rule 10: Orphaned Regular War
**Condition**: Different opponent + NOT CWL (no war_tag)  
**Action**: FINALIZE with existing JSON → ARCHIVE  
**Reason**: Cannot fetch from API without war_tag  
**Log**: `[FINALIZE-ORPHAN-REGULAR] Processing orphaned regular war {filename} - state={state} (can't refetch from API)`

### Rule 11: Same Opponent, War Ended
**Condition**: Same opponent + State = war_ended  
**Action**: FINALIZE → APPEND TO HISTORY → ARCHIVE  
**Reason**: War ended naturally, bot was online  
**Log**: `[FINALIZE-OLD-WAR] Processing {filename} - state=war_ended, is_cwl={is_cwl}`

### Rule 12: Same Opponent, Still Active
**Condition**: Same opponent + State = preparation/in_war  
**Action**: SKIP  
**Reason**: Should not happen (protected by Rule 1), defensive check  
**Log**: `[MANAGE-WAR-FILES] Skipping {filename} - state={state}, is_orphaned={is_orphaned} (not ready for finalization)` (DEBUG)

## War Type Handling

### CWL vs Non-CWL
- `type='cwl'` is treated specially: wars with `state != 'war_ended'` are *not* finalized in `manage_war_files()` and are handled by `process_orphaned_cwl_wars()`.
- All other `type` values (including `random`, `friendly`, `unknown`) are treated as “regular” wars for file lifecycle purposes.

## State Definitions

### Active States (Don't Finalize)
- `preparation` - War scheduled, not started yet
- `in_war` / `inWar` - War in progress
- **Action**: Protected by Rule 1 (current war detection)

### Ended States (Finalize)
- `war_ended` - War completed
- **Action**: Finalize and append to history

### Special States
- `not_in_war` / `notInWar` - No active war
- **Action**: API shouldn't return war objects in this state

## Edge Cases Covered

### Case 1: Rapid CWL Wars (Multiple Wars Per Day)
**Scenario**: New CWL war starts before bot processes old war  
**Detection**: Different opponent tag  
**Handling**: Orphaned war logic (Rule 8/9/10)  
**Result**: ✅ Old war finalized, new war becomes current

### Case 2: Bot Offline During War End
**Scenario**: War ends while bot is down, comes back after new war started  
**Detection**: Orphaned war with state still "in_war"  
**Handling**: CWL enhancement fetches final data (Rule 8)  
**Result**: ✅ Complete data with late attacks captured

### Case 3: Clan Doesn't Start New War (Stale War)
**Scenario**: War ended weeks ago, API keeps returning same war  
**Detection**: Two-stage protection:
  1. 24-hour check: If war ended >24h ago, JSON never saved (Rule 0)
  2. If saved: War_id already in history database AND archive file exists
**Handling**: 
  - Stage 1: Skip JSON save with [STALE-WAR-SKIP] log (no temp file created)
  - Stage 2: If temp file exists, delete it (don't duplicate in archive, Rule 6)
**Result**: ✅ No duplicate archives, clean temp directory, reduced I/O for very old wars  
**Example**: War ended 599.5 hours ago - caught by 24-hour check, never saved to temp/  
**Result**: ✅ No duplicate archives, clean temp directory

### Case 4: Archive File Manually Deleted
**Scenario**: War in history but archive missing  
**Detection**: War in history but no archive file  
**Handling**: Process for late attacks, re-archive (Rule 7)  
**Result**: ✅ Archive consistency restored

### Case 5: Corrupted JSON File
**Scenario**: File system error, incomplete save, encoding issue  
**Detection**: JSON parse fails  
**Handling**: Archive corrupted file (Rule 3)  
**Result**: ✅ No retry loops, file archived for investigation

### Case 6: Missing Opponent Data
**Scenario**: API returned partial data, network error during save  
**Detection**: opponent object missing or opponent.tag is null  
**Handling**: Archive incomplete file (Rule 4)  
**Result**: ✅ No crashes, graceful handling

### Case 7: Multiple Files in Temp (>2)
**Scenario**: Bot offline for days, multiple wars happened  
**Detection**: Multiple files with different opponents  
**Handling**: Process each file sequentially (oldest first)  
**Result**: ✅ All wars finalized in chronological order

### Case 8: CWL API Expired (Old War)
**Scenario**: Fetching final data for CWL war older than 1-2 months  
**Detection**: get_league_war() returns None or incomplete data  
**Handling**: Log warning and keep file in temp for retry (no fallback finalization with incomplete CWL data)  
**Result**: ⚠️ War may remain in temp until a successful API fetch updates it to `war_ended` (or manual cleanup)

## Process Flow Summary

```
CURRENT WAR (newest + same opponent + active)
└─> SKIP (protected)

ARCHIVE EXISTS + IDENTICAL JSON (same filename)
└─> DELETE temp file ([DUPLICATE-SKIP])

WAR ALREADY IN DB (by war_id)
└─> Check/update late attacks → ARCHIVE/REFRESH

ORPHANED CWL WAR (not ended)
└─> Skip in manage_war_files → process_orphaned_cwl_wars tries API fetch
  ├─ success → JSON updated → manage_war_files finalizes → ARCHIVE
  └─ failure → file may remain in temp for retry

ORPHANED CWL WAR (ended) / REGULAR WAR
└─> FINALIZE with existing JSON → ARCHIVE

ENDED WAR (same opponent)
└─> FINALIZE → ARCHIVE

CORRUPTED/INVALID FILE
└─> ARCHIVE (error recovery)
```

## Testing Checklist

### Functional Tests
- [ ] 24-hour stale war check: wars ended >24h ago not saved to temp/
- [ ] Current war not touched (newest + same opponent + active state)
- [ ] Orphaned CWL war (in_war) fetches final data from API
- [ ] Orphaned CWL war (ended) uses existing JSON
- [ ] Orphaned regular war finalizes with existing JSON
- [ ] Ended war (same opponent) finalizes normally
- [ ] Duplicate temp file (archive exists + identical) deletes temp file
- [ ] War already in history (DB) triggers late-attack check and archive refresh
- [ ] Corrupted JSON archived without crash
- [ ] Missing opponent archived gracefully
- [ ] Invalid timestamp archived gracefully
- [ ] Multiple rapid wars processed in order
- [ ] State check prevents ended war from being treated as current

### Edge Case Tests
- [ ] Bot offline during war end → data complete after restart
- [ ] Clan doesn't start new war for weeks → 24-hour check prevents saves
- [ ] War ended 25+ days ago → [STALE-WAR-SKIP] logged, no temp file
- [ ] Archive manually deleted → consistency restored
- [ ] Multiple files (3+) → all processed correctly
- [ ] CWL API expired → graceful fallback to existing JSON
- [ ] File system error during JSON save → corrupted file archived
- [ ] Network error during API call → existing JSON used
- [ ] State check fails → safe fallback (treat as current)

### Integration Tests
- [ ] No race conditions under concurrent calls
- [ ] History database never has duplicates
- [ ] Archive directory never has duplicates
- [ ] Temp directory cleaned up properly
- [ ] Cache invalidation works correctly
- [ ] Late attack detection works
- [ ] War type handling works (CWL vs non-CWL)

### Performance Tests
- [ ] Single war file: <100ms
- [ ] 10 war files: <1s
- [ ] Corrupted file doesn't slow down processing
- [ ] History check optimized (single query per file)
- [ ] Archive check optimized (single file existence check)
- [ ] API calls: 2 per clan_tag (normal), 3 for orphaned CWL wars
- [ ] No redundant API calls (clan info cached, war data fetched once)
- [ ] CWL enhancement only when needed (state check prevents unnecessary calls)

## Logging Patterns

### Success Patterns
```
[STALE-WAR-SKIP] Skipping JSON save for {clan} vs {opponent}: war ended X hours ago (>24h threshold)
[FINALIZED-WAR-SKIP] Skipping JSON save for {clan} vs {opponent}: war {war_id} already in history and archive (fully finalized)
[FRIENDLY-SKIP] Skipping JSON save for {clan} vs {opponent}: friendly wars are never tracked
[MANAGE-WAR-FILES] Processing old war: {filename}
[FINALIZE-OLD-WAR] Processing {filename} - state=war_ended, is_cwl={is_cwl}
[FINALIZE-ORPHAN-REGULAR] Processing orphaned regular war {filename} - state={state} (can't refetch from API)
[DUPLICATE-SKIP] Deleted duplicate temp file {filename} - identical to archive
[REGRESSION-GUARD] Discarded temp {filename}: temp has N attacks < archive N attacks — archive preserved
[ARCHIVE] Moved {filename} to archive
```

### Warning Patterns
```
[MANAGE-WAR-FILES] Error reading {filename}: {error} - archiving
[PROCESS-WAR-HISTORY] No valid opponent data in {filename} - skipping (likely corrupted or incomplete war data)
[PROCESS-WAR-HISTORY] Could not extract start time from {filename}
[ORPHANED-CWL] API returned incomplete data for {war_tag} (expired)
```

### Error Patterns
```
[MANAGE-WAR-FILES] Error processing {filename}: {error} - archiving
[MANAGE-WAR-FILES] Failed to finalize {filename} - archiving
Failed to archive temp file {filename}: {error}
```

## Monitoring Recommendations

### Key Metrics
1. **Files Processed**: Track count per cycle
2. **Stale War Skips**: Monitor [STALE-WAR-SKIP] frequency (high = many inactive clans)
3. **Duplicate Temp Deletes**: Monitor [DUPLICATE-SKIP] frequency (duplicate temp files)
4. **API Failures**: Track CWL API errors (indicates expiration)
5. **Archive Errors**: Alert on repeated failures
6. **Processing Time**: Alert if >1s per cycle

### Alert Conditions
- ℹ️ High [STALE-WAR-SKIP] count (>50/day): Many inactive clans (informational)
- ⚠️ More than 5 corrupted files in 24h
- ⚠️ More than 10 archive errors in 24h
- ⚠️ Same war file processed repeatedly (processing failure)
- ⚠️ Many [DUPLICATE-SKIP] deletes but no [STALE-WAR-SKIP]/[FINALIZED-WAR-SKIP] (pre-save guards not triggering)
- 🔴 Archive directory grows unbounded (deletion not working)
- 🔴 Temp directory never empties (processing blocked)

## Maintenance Notes

### When to Review This Logic
- CoC API changes war data structure
- New war types added (e.g., "special event wars")
- State names change (e.g., "battleDay" instead of "in_war")
- CWL format changes (e.g., different rounds, team sizes)
- History data format changes

### Known Limitations
1. Cannot fetch final data for regular wars (no war_tag)
2. CWL API expires after 1-2 months (use archived JSON)
3. Relies on file modification time for sorting (filesystem dependent)
4. State check requires valid JSON (falls back to treating as current)
5. Late attack detection only for wars already in history

### Future Enhancements
- ✅ ~~Database instead of CSV for better querying~~ (COMPLETED - SQLite database since 2026-02-14)
- Periodic archive integrity checks
- Automated recovery from corrupted archives
- War data versioning for format migrations
- Enhanced CWL prediction with historical data
