# QapBot Clan and War Cycle Architecture

> Moved here from Copilot repo-scoped memory on 2026-07-11 (was never committed to
> the repo before — lived only in local VS Code workspace storage). This file is
> now the single source of truth for these notes; do not duplicate back into memory.

## Database Schema

### Clans Table (db_manager.py)
**Location**: [qapbot/db_manager.py](../db_manager.py#L1453)

```sql
CREATE TABLE IF NOT EXISTS clans (
    clan_tag TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    has_active_subscriptions BOOLEAN NOT NULL DEFAULT 0,
    last_war_update TEXT,
    warlog_is_public BOOLEAN NOT NULL DEFAULT 1,
    last_checked_via_api TEXT,
    war_league TEXT,
    track_war_updates BOOLEAN NOT NULL DEFAULT 1,
    is_deleted BOOLEAN NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
)
```

**Indexes**:
- idx_clans_has_subs ON clans(has_active_subscriptions)
- idx_clans_last_war_update ON clans(last_war_update)

**Migrations**: war_league, track_war_updates and is_deleted are now baked directly into the
`CREATE TABLE IF NOT EXISTS` above (no separate `ALTER TABLE clans` migration statements remain
in `_create_maindata_schema` — the earlier idempotent ALTER-TABLE approach was superseded once
the base schema was updated to include these columns from the start).

## Clan Fetching Pipeline

### 1. CoCClanCache (qapbot/coc_cache.py)
- In-memory cache with stale-while-revalidate strategy
- Soft TTL: 280s (4m40s), Hard TTL: 600s (10m)
- Stores: `{clan_tag: {"data": clan_obj, "timestamp": datetime}}`

**Key Methods**:
- `get_clan(clan_tag)`: Returns cached or fresh clan from API
- `_fetch_and_cache(clan_tag)`: Fetches from API and updates cache + clan_name_cache
- `_update_clan_metadata()`: Updates clan_name_cache, war_league, warlog status
- `clear_expired()`: Removes stale entries

### 2. CACHE.get_current_war_from_api(clan_tag)
**Location**: [qapbot/cache_manager.py](../cache_manager.py#L2832)

Wrapper around `coc_client.get_current_war()` with retry logic. Does NOT cache war data (fresh on every call).

## Phase-1 Update Cycle

**Location**: [QapBot.py](../../QapBot.py#L543-L949) (categorization starts in `async def main()`
at L543; parallel fetch loop/semaphore at L850-L949)

### Step 1: Categorize Clans to Update
- Active clans (has_active_subscriptions=True): update every cycle
- Inactive clans: update if >22h since last_war_update
- Passive clans (track_war_updates=False): skip entirely
- CWL group expansion: add group-mate clans if main clan is active

### Step 2: Parallel API Fetches
- Concurrency: 20 concurrent requests (bounded by asyncio.Semaphore)
- For each clan: call `fetch_clan_war_data(clan_tag)`
- Record cycle stats: api_fetched, api_no_war, api_fail:*

### Step 3: fetch_clan_war_data()
**Location**: [QBhelperfunctions.py](../../QBhelperfunctions.py#L6230-L6597)

**Phase 1a - Optional API fetch for clan metadata**:
- Throttle: 30min for role-enabled clans, 12h for others
- Essential field bypass: forces fetch if required fields missing (e.g., war_league NULL after migration)
- Calls: `await CACHE.coc_clan_cache.get_clan(clan_tag)`

**Phase 1b - Mandatory API fetch for current war**:
- Calls: `await CACHE.get_current_war_from_api(clan_tag)`
- **Exception Handling**:
  - `coc.PrivateWarLog` / `coc.NotFound`: Mark warlog as private, try CWL fallback
  - Other exceptions: Raise `WarDataFetchError`
- **CWL Fallback**: For private warlogs, try `_find_active_cwl_war_for_clan()`
- **War save**: Call `CACHE.save_war_object(coc_war_obj, clan_tag)`
- **Enemy discovery**: For new opponent clans, call `_upsert_enemy_clan_on_war_start()`

**Returns**: `{"clan_tag": str, "war_obj": coc.War, "opponent_tag": str, "state": str}` or None

## Error Handling

### Exception Hierarchy
**Location**: [qapbot/exceptions.py](../exceptions.py#L19)

- `WarDataFetchError`: Failed API call (wrapped around coc.NotFound, coc.PrivateWarLog, etc.)
- `WarProcessingError`: War processing failures
- Caught in Phase-1 loop (QapBot.py#L904-L929, inside `fetch_single_clan()`) and bucketed into cycle stats

### Specific Cases
- **NotFound (404)**: Clan deleted or tag invalid → Record as api_fail:NotFound
- **PrivateWarLog**: War log is private → Try CWL fallback
- **RateLimit (429)**: Bucket as api_fail:RateLimit
- **Maintenance (503)**: Bucket as api_fail:Maintenance

## Skip Clan Logic

**Field**: `clans.track_war_updates` (Boolean, default=1)

**Values**:
- 1 (True): Active polling every cycle (role-enabled + M3+ leagues)
- 0 (False): Passive tracking (no regular polling, only on enemy discovery)

**Decision Logic** (coc_cache.py):
```python
_track = _wl_name in _WAR_UPDATE_LEAGUES  # Master III+ → True
# Defined: Legend, Titan I-III, Champion I-III, Master I-III
```

**One-way Ratchet**: track_war_updates never reverts to False on demotion.

## Clan Data Persistence

### CACHE.persist_clan(clan_tag)
**Location**: [qapbot/cache_manager.py](../cache_manager.py#L2107)

Writes modified clan_name_cache entry to DB via `db_manager.save_clan()`.

### db_manager.save_clan() / _save_clan_unlocked()
**Location**: [qapbot/db_manager.py](../db_manager.py#L3699) / [../db_manager.py#L3655](../db_manager.py#L3655)

UPSERT (INSERT ... ON CONFLICT):
- Inserts new clan or updates existing
- `war_league`: COALESCE(excluded, existing) — preserves if None passed
- `track_war_updates`: COALESCE(excluded, existing, 1) — defaults to 1

## DB Startup and Migrations

### WarHistoryDB.initialize()
**Location**: [qapbot/db_manager.py](../db_manager.py#L962)

1. Create async connection with aiosqlite
2. Enable WAL mode (journal_mode=WAL)
3. Set pragmas: synchronous=NORMAL, busy_timeout=30000, foreign_keys=ON, mmap_size=8GB
4. Call `_create_schema()` → all migrations

### _create_schema() / _create_maindata_schema()
**Location**: [qapbot/db_manager.py](../db_manager.py#L1144) / [../db_manager.py#L1446](../db_manager.py#L1446)

Idempotent (CREATE TABLE IF NOT EXISTS for every table). No `ALTER TABLE` migration
statements remain anywhere in db_manager.py — new columns (e.g. `war_league`,
`track_war_updates`, `is_deleted` on `clans`) are now added directly to the base
`CREATE TABLE IF NOT EXISTS` definitions rather than via separate idempotent ALTER-TABLE
migration steps.

## CWL Season Naming (normalize_cwl_season)

**Location**: [qapbot/constants.py](../constants.py#L206)

CoC API returns `league_group.season` as `"YYYY-MM"` normally, but as a literal
`"YYYY-MM-DD"` creation-date (always a Monday) while the group is still being
assembled. Since matchmaking always starts on Monday, this can land in the
last days of the prior month (e.g. `"2026-06-29"` for the July CWL) or first
days of the target month — NOT a real mid-month bonus CWL.

`normalize_cwl_season()` disambiguates via day-of-month on `YYYY-MM-DD` input:
- day <= 6  → fold into that month's `YYYY-MM` (regular CWL, month-start snapshot)
- day >= 25 → fold into NEXT month's `YYYY-MM` (regular CWL, month-end snapshot)
- day 7-24  → genuine mid-month bonus CWL → snap to ISO Monday of that week

Affected tables (all have a `cwl_season` column): `war_summary`,
`cwl_league_groups`, `cwl_league_rounds`. `cwl_league_groups.league_group_id`
and `cwl_league_rounds.league_group_id` are `sha256(f"{season}:{sorted_tags}")[:16]`
— must be recomputed whenever season is corrected.

**Repair script**: `fix_cwl_season_labels.py` was a one-time repair tool for rows mislabeled
before `normalize_cwl_season()` existed — deleted after use, no longer in `qapbot/scripts/`.
If a similar repair is ever needed again, re-derive it from the disambiguation rules above and
the `(cwl_season, clan_tag)` PK-merge requirement on `cwl_league_groups`.

## coc_client Initialization

**Location**: [QapBot.py](../../QapBot.py#L237), inside `startup_login()`.

The `coc.Client(key_count=10, throttler=coc.BatchThrottler, throttle_limit=100)` construction
itself is documented in `../qapbot/docs/RATE_LIMITING_IMPLEMENTATION.md` (canonical source —
don't duplicate its detail here). Once the client exists, it's stored in both `QBcore.coc_client`
(back-compat) and `CACHE.coc_client` — all guarded by `if QBcore.coc_client is None` so
`startup_login()` is idempotent across reconnects.

**Database init is a separate, earlier step (2026-07-30)**: `startup_login()` no longer touches
the database at all. `on_ready()` calls `QapBot.initialize_database()` as its own "Step 1.5",
strictly BEFORE the CoC-login step (`startup_login()`, wrapped in a 60s timeout) and before
`periodic_main()` can start. This constructs `CACHE.db_manager = WarHistoryDB()` and awaits
`initialize()` under its own generous 30-minute timeout, with `QBcore.db_maintenance_mode = True`
for the duration (blocks Discord commands with the existing maintenance message; the gateway
connection is already up, so the bot still shows online). See
[DATABASE_ARCHITECTURE.md](DATABASE_ARCHITECTURE.md)'s "2026-07-30: Startup Ordering Fix (Final)"
entry for why this ordering matters: it's what makes it safe for a rare, first-run-only slow
schema migration (e.g. building a new index on a multi-million-row table) to run inline in
`_create_schema()` without racing any concurrent DB writer or sharing a timeout budget with CoC
login.

**Location**: [qapbot/cache_manager.py](../cache_manager.py#L176)

`self.coc_clan_cache = CoCClanCache(soft_ttl_seconds=280, hard_ttl_seconds=600)`

## Performance Investigation (2026-07-11)

**Tooling**: [qapbot/scripts/log_time_gaps.py](../scripts/log_time_gaps.py) is functional but
only accepts ONE log file at a time (no multi-file/rotated-log aggregation) and its top-N gaps
are dominated by the intentional inter-cycle `Sleeping for Ns` pauses — filter those out
(`FROM: ... Sleeping for`) to see real in-cycle stalls.

**Root causes found (confirmed via data/logs/qapbot.log 2026-07-11)**:
1. **Nightly full VACUUM took 2688s (44.8 min)** on the 33 GB `data/qapbot.db` (freed 2.4 GB).
   `nightly_db_maintenance()` in [qapbot/db_manager.py](../db_manager.py#L5717) triggers VACUUM
   whenever `freelist_count > 500 pages` (~2-8 MB) — a fixed threshold that does NOT scale with
   DB size, so as the DB grows, nightly VACUUM duration grows too (self-reinforcing). Runs daily
   at 03:00 UTC (`QapBot.py` ~line 2060, the `hour == 3, minute == 0` scheduling gate). Blocks DB-backed Discord commands via
   `QBcore.db_maintenance_mode` for the whole VACUUM duration (event loop / gateway heartbeat NOT
   blocked — only DB commands are gated).
2. **Backlog catch-up cycles after the stall**: normal cycles process ~600-800 clans in 40-100s;
   the two cycles immediately after the 44.8-min VACUUM processed 6885 and then 4534 clans, taking
   1468s (24.5 min) and 688s (11.5 min) respectively — this is what makes the bot feel "unresponsive"
   well beyond the VACUUM window itself.
3. **File-count scale** (2026-07-11 measurement): `data/temp/` = 30,522 files across 10 shards;
   `archive/` = 809,394 files across 10 shards. `archive/` keeps only the last 7 days
   (`qapbot/move_old_archive_files.py`, `days=7.0`, called nightly via `_archive_move_nightly()`
   in QapBot.py before DB maintenance) — the large count reflects genuine scale (thousands of
   tracked clans, e.g. cycle logs show up to ~8000 total clans/6885 fetched in one cycle), not an
   obvious bug. One nightly archive-move run moved 77,603 files (3.3 GB) in 56.4s.
4. Pre-scan optimization already exists (`QapBot.py` ~line 952: single `glob.glob()` for temp
   shards + `os.scandir()` for archive shards once per cycle, shared across Phase 2/3) — do NOT
   re-introduce per-clan directory scans.

**Fix ideas (not yet implemented)**:
- Make VACUUM threshold size-aware (e.g. skip if DB > N GB and freelist ratio < X%, or switch to
  `PRAGMA auto_vacuum=INCREMENTAL` + periodic `PRAGMA incremental_vacuum(N)` to avoid full rewrites).
- Consider pruning/rolling off old `war_attacks`/`war_summary` rows (or archiving to a separate
  cold DB) to keep the live DB smaller — needs product decision on retention. **See the
  Hot/History DB Redesign section below — this is now an active proposal for that decision.**
- After a stall (VACUUM or restart), consider capping the first catch-up cycle's clan batch size
  instead of processing the full backlog in one giant cycle.
- Investigate whether `calculate_win_probability` / `calculate_max_possible_stars`
  (QBwarsim.py, Monte Carlo) run synchronously on the event loop during leaderboard posting —
  worth confirming they're offloaded (worker pool exists per QBwarsim.py "Parallel Monte Carlo
  helpers") especially during backlog cycles with 1000+ clans to post.

## Hot/History DB Redesign (IMPLEMENTED 2026-07-11)

**Goal**: stop `data/qapbot.db` from growing unboundedly (33 GB and counting), which was the root
cause of the ever-slower nightly VACUUM described above. The DB is now split into `data/qapbot.db`
(hot: current + previous calendar month of war data) and `data/qapbot_history.db` (everything
older). See `changelog.txt` (2026-07-11 entry) for the full file-by-file list of changes.

### Mechanism: `ATTACH DATABASE`

`WarHistoryDB` uses **two connection types**: one async `aiosqlite` connection, and an
`_SyncConnectionPool` of 8 plain `sqlite3` connections used by all `*_sync` methods
(`qapbot/db_manager.py`). Rather than querying two separate connections and merging in Python,
`qapbot_history.db` is ATTACHed as schema `history` on **every** connection (the async one, all 8
pooled sync connections, and the bare-connection fallback used by tests/pre-initialize callers)
right after it's opened, in `WarHistoryDB.initialize()`:
- Queries already bounded to "current/recent" data needed **zero changes**.
- Queries that must see all-time data became `WITH wa AS (SELECT * FROM main.war_attacks UNION ALL
  SELECT * FROM history.war_attacks) SELECT ... FROM wa ...` (CTE wrapping the raw table so the
  rest of each query's join/aggregation/window-function logic is untouched) — or a plain `UNION`
  for simple `SELECT DISTINCT`-style queries.
- Writes stay targeted at `main.*` (unqualified) in the normal hot write path; a handful of
  maintenance/backfill scripts that write historical data were updated to attach + qualify their
  writes (or, where it's provably safe, left writing to `main` since the monthly migration job
  will sweep any old-dated row into `history` on its next run regardless — see Group 4 below).

**Tables split**: `war_attacks`, `war_summary`, `cwl_league_groups`, `cwl_league_rounds` — dated
by `date` for the first two, by `cwl_season` (via the existing `normalize_cwl_season()`, truncated
to its `YYYY-MM` prefix for comparison — comparing raw `YYYY-MM` vs `YYYY-MM-DD` strings directly
would be wrong since they have different lengths) for the CWL tables. Everything else (clans,
subscriptions, users, guild_config, `player_name_index`, `bot_metadata`, etc.) stays hot-only,
unsplit — `player_name_index` in particular continues to be upserted on every write regardless of
which physical DB the underlying attack rows land in.

**Monthly migration** (day 1 of the month, inside the existing nightly maintenance window,
`WarHistoryDB.monthly_history_migration()`): cutoff = start of `current_month − 1`. For
date-keyed tables: batched `INSERT INTO history.X SELECT * FROM main.X WHERE id IN (...)` /
`DELETE FROM main.X WHERE id IN (...)` in chunks of 5000 rows (never one giant transaction — see
the 44-minute VACUUM lesson above). For CWL tables: one transaction per affected season (each
season naturally has few rows). Does NOT run VACUUM itself — the pre-existing
`nightly_db_maintenance()` freelist-based VACUUM trigger picks up the space freed by the
migration's DELETEs on the same nightly run (migration runs first). This is also the actual fix
for "DB constantly growing": once `main.db` is bounded to ~2 months of data, nightly VACUUM stops
growing more expensive every night.

**VACUUM on attached databases — confirmed via spike (open question #1, now answered)**:
`VACUUM <schema> INTO '<file>'` (e.g. `VACUUM history INTO 'history.db.vacuumed'`) works correctly
and safely in isolation on an attached, non-main SQLite schema (verified empirically on SQLite
3.50.4 — inserted distinguishable data into both `main` and an attached `history` schema, ran
`VACUUM main INTO` and `VACUUM history INTO` into separate files, confirmed each output file
contained ONLY its own schema's data with correct row counts). This feature has existed since
SQLite 3.27 (2019), so it's expected to be available in production too. Not yet wired into a
periodic history-DB compaction routine — the history DB currently only grows via the monthly
migration's bulk insert, so a dedicated VACUUM schedule for it is a possible future refinement,
not required for correctness.

### Complete function inventory (verified against qapbot/db_manager.py; ALL items now DONE)

**Group 1 — bounded to hot window already, no change needed**: `war_attacks_exist_sync`,
`get_war_summary_state_sync`, `get_recent_war_summaries_sync` (28d), `get_active_cwl_group_member_tags`,
`war_write_batch`/`_flush_pending_war_writes`/`add_war_attack_records_sync`/`add_war_summary_sync`/
`update_war_data_sync`/`update_war_attack_records_sync` (always write current data).

**Group 2 — hot+history UNION — ✅ ALL 20 DONE** (verified end-to-end with real hot+history test
data, plus the full 1401-test suite passing throughout):
`search_players_by_name_sync`, `get_player_war_history_sync`, `get_player_attack_summary_sync`,
`get_player_monthly_star_dist_sync`, `get_all_war_attacks_existing_sync`, `get_all_war_clan_tags`,
`get_all_war_summaries_brief_sync`, `get_all_war_summary_keys_sync`, `get_global_db_statistics_sync`,
`get_direct_cwl_attacks_sync`, `get_cwl_roster_sync`, `get_cwl_attack_records_sync`,
`get_cwl_group_info`, `get_latest_cwl_season_for_clan`, `get_latest_cwl_season_for_clan_in_month`,
`get_cwl_group_war_stats`, `get_war_tag_leagues`, `get_cwl_max_rounds_sync`, `get_war_summaries_sync`,
`has_cwl_season_data_sync`. Also fixed `get_clan_attack_history_sync` (not in the original
inventory, but used by `verify_all_cwl_history.py` for arbitrary historical months, so it needed
the same treatment).

**Group 3 — Discord commands affected** (via Group 2 functions, now fixed): `/status` (all-time),
`/whois` + both context-menu entries (all-time player history), `/list bybrowser` (all-time),
`/leaderboard cwlinfo` / `cwlinfo_comp` / `cwlgroup`, `/analyse` both subcommands (leaguegroup,
cwlopponent) — all now correctly see historical data regardless of age.

**Group 4 — maintenance scripts — ✅ DONE** (mix of full rewrites and documented-safe-as-is;
historical record — several of these were one-time-use and have since been deleted, marked
below):
`fix_cwl_season_labels.py` (deleted), `check_cwl_completeness.py` (deleted),
`harvest_cwl_war_tags.py`, `backfill_cwl_groups_from_war_summary.py` (deleted),
`backfill_player_name_index.py` (deleted), `db_analysis.py` (deleted),
`verify_all_cwl_history.py`, `analyze_cwl_rounds.py` were rewritten to be hot/history-aware.
`fix_cwl_season_2026_06.py` (deleted), `backfill_cwl_rounds_via_api.py` (deleted),
`recover_missing_cwl_history.py` (still present)
were deliberately left unchanged with a documented rationale (see `changelog.txt` 2026-07-11
entry and each script's docstring) — either because they can only ever target current/live data,
or because their writes to `main` self-heal via the next monthly migration.
Also fixed a previously-undocumented instance: the `/admin` CWL-group-backfill command handler
(`handle_backfill_cwl_groups` / `_handle_backfill_cwl_groups_inner` in
`qapbot/QBdiscocmdshelper_admin_command.py`) had the same raw-sqlite3/main-only gap — fixed.

`analyze_cwl_rounds.py` (11 raw `sqlite3.connect()` calls, one per report section) was fixed by
attaching `history` (read-only) on every connection and CTE-wrapping `war_summary` /
`cwl_league_groups` / `cwl_league_rounds` references with `UNION ALL main.<table> + history.<table>`
in all 7 query helper functions plus the inline diagnostic/completeness queries in `main()`.
The season auto-detect query (`MAX(cwl_season)`) was deliberately left `main`-only — the most
recent season can never live in `history` by construction of the monthly migration. Verified with
a synthetic hot+history DB where an entire CWL season's data lived only in `history`: before the
fix the script reported 0 archive rows for that season; after the fix it correctly reports all 7
war_summary rows, round/league breakdowns, and max-rounds distribution.

**Group 5 — operational tooling — ✅ DONE**: `BackupProd.bat` and `RestoreDevFromBackup.bat` now
lock-check `qapbot_history.db` alongside `qapbot.db`. `RestoreProdFromBackup.bat` needed no code
changes — both DB files live in the same `data/` directory and are already backed up/restored as
a unit. The `/admin` "Check Data" command was
already hot/history-aware for free (it goes through the now-fixed Group 2 functions); "Import Data"
in `QBdiscordcmds.py` turned out to be an unrelated feature (player registration import from
ClashPerk embeds, not DB backup).

### Helper reference

- `WarHistoryDB._history_cutoff()` — computes the monthly migration cutoff.
- `WarHistoryDB.monthly_history_migration()` — orchestrates the monthly migration.
- `qapbot/scripts/run_db_maintenance_now.py` — CLI wrapper to trigger
  `WarHistoryDB.nightly_db_maintenance()` on demand (outside its normal once-a-day schedule),
  which includes the monthly hot→history migration as one of its steps. (The old dedicated
  `run_history_migration_now.py` one-time script has been deleted; this is the current
  equivalent.)
- `attach_history_db(conn, db_path, history_db_path=None, read_only=False)` (module-level in
  `qapbot/db_manager.py`) — convenience helper for standalone scripts using a bare `sqlite3`
  connection instead of `WarHistoryDB`; ATTACHes history + creates its schema (skipped when
  `read_only=True`, since a read-only-attached schema can't `CREATE TABLE`).
- `_create_history_schema_sync(conn)` (module-level) — sync counterpart of
  `WarHistoryDB._create_history_schema()`, used by both the sync connection pool and
  `attach_history_db()`.
