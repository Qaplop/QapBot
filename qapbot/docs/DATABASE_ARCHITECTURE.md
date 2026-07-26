# Database Architecture (SQLite)

**Status**: Production - Database-only mode (no feature flags)  
**Database**: `data/qapbot.db` (hot, SQLite with WAL mode) + `data/qapbot_history.db` (history, ATTACHed as schema `history`)  
**Last Updated**: 2026-07-26

---

## Why SQLite?

### Advantages for QapBot
- **Zero Configuration**: Embedded in Python, no separate server process
- **Few Files**: Easy backup, deployment, server-machine-compatible (two files since the hot/history DB split — see below)
- **ACID Compliant**: Reliable transactions and data integrity
- **Performance**: 10-100x faster for filtered queries vs CSV/JSON parsing
- **Built-in Support**: `sqlite3` module in Python standard library
- **Cross-Platform**: Identical behavior on dev (Windows) and prod (Linux server-machine)
- **Low Overhead**: Perfect for single-instance bot architecture
- **server-machine-Friendly**: WAL mode works reliably on network storage

### When to Consider PostgreSQL
- Multiple concurrent bot instances (sharding by guild)
- Remote database access needed
- Advanced JSONB analytics and indexing
- Web dashboard with concurrent query load
- Multiple developers querying DB simultaneously

**Current Decision**: SQLite is sufficient for 10x current scale. PostgreSQL migration path is straightforward if needed.

---

## Critical Database Configuration

### WAL Mode + Pragmas (MANDATORY)

**Applied on every connection in `db_manager.py`:**

```sql
PRAGMA journal_mode=WAL;          -- Write-Ahead Logging (server-machine-friendly)
PRAGMA synchronous=NORMAL;        -- Balance safety vs performance
PRAGMA busy_timeout=5000;         -- Sync connections: wait 5s for locks (server-machine latency)
                                  -- Async connection uses 30000 (30s) for bulk-write bursts
PRAGMA foreign_keys=ON;           -- Data integrity enforcement
PRAGMA temp_store=MEMORY;         -- Faster temp operations
PRAGMA cache_size=-65536;         -- 64 MB page cache (server-machine I/O reduction)
PRAGMA mmap_size=8589934592;      -- 8 GB shared kernel page cache (HDD/server-machine seek reduction)
```

**Why Critical:**
- Production environment on server-machine (`<PROD_BOT_ROOT>`; `data/` and `archive/` typically live on an SSD referenced by `${PROD_DATA_DIR}/data`)
- The SSD location is **optional** — configured via `PROD_DATA_DIR` in `.env`. Defaults to the bot root when not set. Automatically ignored in DEV mode.
- WAL eliminates reader blocking during writes
- Reduces corruption risk on network storage
- 3-10x faster concurrent reads
- Foreign keys maintain referential integrity

---

## Hot/History DB Split

`WarHistoryDB.initialize()` always `ATTACH`es a second SQLite file as schema
`history` (default path derived from the hot path, e.g. `data/qapbot.db` →
`data/qapbot_history.db`; overridable via `history_db_path`). The `history`
schema gets its own WAL/synchronous pragmas (`PRAGMA history.journal_mode=WAL`,
`PRAGMA history.synchronous=NORMAL`) since ATTACHing does not retroactively
apply the main connection's pragmas.

**Retention model**: `main` (the hot DB) always holds the current calendar
month plus the immediately preceding one in full; everything strictly older
is migrated to `history` once a month by `nightly_db_maintenance()`
(cutoff computed by `_history_cutoff()`). Only the 4 time-series tables are
mirrored on both schemas: `war_attacks`, `war_summary`, `cwl_league_groups`,
`cwl_league_rounds` — all other maindata tables (clans, users, guild_config,
etc.) stay hot-only. Migration is batched (`_migrate_table_batch_by_date()`)
rather than one giant transaction, to avoid holding the write lock for long.

Queries that need the full time range (e.g. `/whois`, full-history reports)
`UNION`/`UNION ALL` against `main.<table>` and `history.<table>` explicitly.

Code: `qapbot/db_manager.py` — `initialize()`, `_create_history_schema()`,
`attach_history_db()`, `_history_cutoff()`, `nightly_db_maintenance()`.

---

## Database Schema

### Core Tables (22 total)

**Per-Attack Tables** (Phase 8 - Complete, sole war data store since Phase 9):
- `war_attacks` - Per-attack rows: one row per attack per player per war. UNIQUE(war_id, player_tag, attack_order). Sentinel rows (attack_order=0) record missed-all-attacks players.
- `war_summary` - One row per war per tracked clan: results, stars, destruction, team size, CWL metadata, lineup JSON. UNIQUE(war_id, clan_tag).

**Leaderboard data flow:**

The `get_clan_attack_history_sync()` method in `db_manager.py` aggregates `SUM(stars)`, `COUNT(attacks)`, `SUM(destruction)` per (war_id, player_tag) from `war_attacks`. Legacy data that was previously in the retired `war_history` table was migrated into `war_attacks` during Phase 9 (synthetic rows with `attack_order=1`, `destruction=0.0`).

`_merge_entries()` in `QBhelperfunctions.py` accumulates `Total_Dest_Pct` across wars and computes `Avg_Dest_Pct = Total_Dest_Pct / Attacks` for the `Ø🔥/Atk` leaderboard column.

**cwlinfo data flow:**

`_generate_cwlinfo_archive_embeds()` previously read from `archive/*.json`. It now reads from the `war_summary` table via `get_war_summaries_sync(clan_tag, is_cwl=True)`. Lineup emojis are rendered from the stored `clan_lineup_json` / `opp_lineup_json` columns (JSON arrays of TH levels). The `round_number` column (INTEGER, NULL for regular wars, 1-7 for CWL) was added 2026-05-09 and is populated at finalization time via `cwl_league_rounds` lookup; NULL for wars finalized before the CWL round tracking feature was deployed.

**Maindata** (Phase 3 - Complete):
- `clans` - Clan metadata and subscription tracking. Key columns: `has_active_subscriptions`, `track_war_updates`, `war_league`, `is_deleted` (BOOLEAN DEFAULT 0 — set when the clan no longer exists in the CoC API; cleared automatically on any successful `GET /clans/{tag}` response).
- `clan_families` - Family definitions
- `clan_family_members` - Junction: families ↔ clans
- `users` - User accounts with notification settings
- `user_players` - Junction: users ↔ player accounts
- `user_buddies` - Per-user "Save-your-Buddy" links: stores a user's buddy player tags
  so buddy war reminders / notifications can be delivered.
- `guild_config` - Discord server configuration
  Key columns: language, role IDs (newbie/member/coc roles), registration config,
  war notification config, welcome message config:
  `welcome_message_enabled` (BOOLEAN DEFAULT 0), `welcome_message_mode` (TEXT DEFAULT 'clan_link'),
  `welcome_apply_channel_id` (TEXT), `welcome_clan_tag` (TEXT, legacy single-clan column —
  superseded by `guild_welcome_clans`/`guild_welcome_families` below; kept as a read-only
  fallback in `get_guild_config()` for guilds not yet re-saved under the new multi-select UI)
- `guild_member_families` - Junction: guilds ↔ families
- `guild_member_clans` - Junction: guilds ↔ clans
- `guild_welcome_families` - Junction: guilds ↔ families selected (as a whole) for the welcome
  message's clan-link mode. Independent from `guild_member_families` (member-role granting).
- `guild_welcome_clans` - Junction: guilds ↔ individually-selected clans for the welcome
  message's clan-link mode. A clan and its owning family are mutually exclusive per guild
  (enforced by `WelcomeMessageConfigView`, not by a DB constraint) — different families are
  independent of each other.
- `guild_clan_roles` - Per-clan Discord role IDs scoped to a guild (one role id per
  clan_tag within a guild_id); backs per-clan role assignment.
- `subscriptions` - Channel subscriptions for updates
- `leaderboard_messages` - Discord message tracking for updates

**Notification State** (Phase 6 - Complete):
- `notification_state` - Player DM notification tracking (prevent duplicates)
- `channel_notification_state` - Channel notification tracking (prevent duplicates)

**Cross-Restart State**:
- `bot_metadata` - Key-value store for state that must survive bot restarts
  (e.g. one-time migration markers, last-run timestamps).

**CWL Round Tracking** (2026-05-09 - Complete):
- `cwl_league_groups` - One row per (clan × season) capturing which group each clan belongs to.
  PK: (cwl_season, clan_tag). Index on league_group_id so any clan can find its 7 group-mates.
  Columns added 2026-05-10: `league_rank` (e.g. "Master League II"), `cwl_ended` (0/1),
  `group_rank` (1-8), `total_stars` (with 10-star win bonus), `total_destruction` (sum of
  clan_destruction × team_size across all ended wars).
- `cwl_league_rounds` - One row per CWL war: maps war_tag → (cwl_season, cwl_round 1-7, league_group_id).
  PK: war_tag (globally unique). Joined to war_summary via war_tag to populate round_number.
  Populated by CacheManager._process_league_group_response() on every fresh get_league_group() call.

**Search Index** (2026-05-15 - Complete):
- `player_name_index` - Fast name-lookup index: one row per unique player_tag with their most
  recent known name and `last_seen` ISO timestamp. ~6.2 M rows on production (⏱ time-sensitive,
  verified 2026-07-26 — see Database Size below for the re-verify note).
  Loaded entirely into `CACHE.player_name_index: Dict[str,str]` at startup; in-memory
  O(n) search replaces a `LIKE '%substr%'` scan over the full `war_attacks` table (now ~113 M
  rows combined across the hot + history DBs — see Database Size below).
  Populated/maintained by `_upsert_player_name_index_in_conn()` inside every war write path
  (INSERT OR IGNORE ... ON CONFLICT DO UPDATE WHERE excluded.last_seen > stored).
  Also updated by `update_player_name_index_sync()` when coc_cache detects a live API name change.
  Sentinel rows (attack_order=0) ARE included so missed-war players appear in /whois searches.

### Foreign Key Relationships

**Key constraints:**
- `user_players.discord_id` → `users.discord_id` ON DELETE CASCADE
- `user_players.current_clan_tag` → `clans.clan_tag` ON DELETE SET NULL
- `clan_family_members.family_tag` → `clan_families.family_tag` ON DELETE CASCADE
- `clan_family_members.clan_tag` → `clans.clan_tag` ON DELETE CASCADE
- `guild_member_families.guild_id` → `guild_config.guild_id` ON DELETE CASCADE
- `guild_member_families.family_tag` → `clan_families.family_tag` ON DELETE CASCADE
- `guild_member_clans.guild_id` → `guild_config.guild_id` ON DELETE CASCADE
- `guild_member_clans.clan_tag` → `clans.clan_tag` ON DELETE CASCADE
- `notification_state.discord_id` → `users.discord_id` ON DELETE CASCADE

**Intentional exceptions:**
- `subscriptions.clan_tag` has **no FK constraint** because it can store both clan tags and family tags.
- `leaderboard_messages.clan_tag` is not FK-constrained (nullable and used as metadata).

**Data integrity:**
- Orphaned records automatically deleted when parent removed
- Referential integrity enforced at database level
- Validation helper: `_ensure_clan_exists()` syncs cache → database before FK inserts

---

## Migration Principles

### 1. Idempotent Operations
**All database writes must be re-runnable:**

```sql
-- Safe to run multiple times (no duplicates)
INSERT OR IGNORE INTO war_attacks (...) VALUES (...);
INSERT OR REPLACE INTO clans (...) VALUES (...);

-- Safe schema changes
CREATE TABLE IF NOT EXISTS war_attacks (...);
CREATE INDEX IF NOT EXISTS idx_war_attacks_war_id ON war_attacks(war_id);
```

**Benefits:**
- Migration scripts can be re-run to fix data issues
- Prevents duplicate records
- Safe recovery from partial failures

### 2. Validation After Migration
**Always validate data integrity:**

```python
# Validation checks
- Record count: JSON count == DB count
- Relationship integrity: No orphaned foreign keys
- Data ranges: Stars, attacks, TH levels in valid ranges
- Format validation: Tags start with #, dates in ISO format
```

### 3. Fail-Fast Behavior
**Database errors terminate bot startup:**

```python
# QapBot.py initialization
try:
    CACHE.db_manager = WarHistoryDB()
    await CACHE.db_manager.initialize(CONFIG.db_path)
except Exception as e:
    logging.error(f"Database initialization failed: {e}")
    raise RuntimeError("Bot cannot start without database")
```

### 4. Essential-Field Bypass for New Columns
**When a new column is added via `ALTER TABLE`, existing cache entries will have `NULL`/missing values for it.**

QapBot's `fetch_clan_war_data()` (in `QBhelperfunctions.py`) has an essential-field bypass:
if a required field is absent in the `clan_name_cache` entry, it forces an immediate API fetch
even though the TTL gate would otherwise suppress it. This populates the field without waiting
up to 12 h for the next scheduled refresh.

```python
# QBhelperfunctions.py — fetch_clan_war_data()
_ESSENTIAL_CLAN_FIELDS = ('war_league',)
```

**Rule: every time you add a new column to the `clans` table that is required at runtime,
also add the field name to `_ESSENTIAL_CLAN_FIELDS`.** That is the only code change needed
— the bypass will automatically trigger a backfill fetch for any clan missing it.

Example: if you add a `home_league` column:
```python
_ESSENTIAL_CLAN_FIELDS = ('war_league', 'home_league')  # add here
```

**Exception — `is_deleted`**: This column does NOT belong in `_ESSENTIAL_CLAN_FIELDS`.
It is managed entirely by the clan-deletion detection system (see below) and defaults
to `False` for pre-migration rows, which is the correct safe default.

**Why:**
- Data corruption is unacceptable
- Prevents silent failures
- Forces immediate attention to database issues

### 4b. Clan Deletion Detection (`is_deleted`)

When Supercell permanently deletes a clan, the CoC API returns `404 NotFound` on every
subsequent `GET /clans/{tag}` call. Without special handling this generates hundreds of
`[ERROR]` log lines per day (one per polling cycle).

**How the system works** (all implemented 2026-05-31):

| Step | Where | What happens |
|---|---|---|
| Detection via `get_clan` | `QBhelperfunctions.py` `fetch_clan_war_data()` | `coc.NotFound` from `CACHE.coc_clan_cache.get_clan()` → `_mark_clan_deleted()` called; function returns `None` |
| Detection via `get_current_war` | `QBhelperfunctions.py` `fetch_clan_war_data()` | `coc.NotFound` from `get_current_war_from_api()` and `get_clan` TTL gate was active → verify via `get_clan`; if also `NotFound` → `_mark_clan_deleted()` |
| Mark deleted | `QBhelperfunctions.py` `_mark_clan_deleted()` | Sets `clan_name_cache[tag]["is_deleted"] = True` and calls `persist_clan()` to write to DB; logs one `[CLAN-DELETED]` warning |
| Skip in polling loop | `QapBot.py` Phase-1 build | After the `track_war_updates` skip, any clan with `is_deleted=True` is also skipped; no API calls, no errors |
| Auto-restore | `qapbot/coc_cache.py` `_update_clan_metadata()` | Any successful `GET /clans/{tag}` response for a clan where `is_deleted=True` → clears to `False` + logs `[CLAN-RESTORED]` + persists |
| DB persistence | `qapbot/cache_manager.py` `persist_clan()` | Passes `is_deleted` from cache dict to `save_clan()` |
| DB schema | `qapbot/db_manager.py` | `is_deleted BOOLEAN NOT NULL DEFAULT 0`, baked directly into the base `CREATE TABLE IF NOT EXISTS clans` statement (no separate `ALTER TABLE` migration — `db_manager.py` has none currently; see Hot/History DB Split above) |

**Migration safety**: `get_all_clans_dict()` maps a `NULL` value to `False`, so any legacy row
predating this column is never incorrectly skipped.

(See the `_ESSENTIAL_CLAN_FIELDS` exception note above — `is_deleted` intentionally does not
belong there, since it's managed by this deletion-detection system instead.)

---

### 5. Connection Recovery
**Automatic reconnection for transient failures:**

- `_ensure_connection()` validates connection before every DB operation
- `_reconnect()` closes stale connection and opens fresh one with all pragmas
- Replaces 34 manual `if not self.conn: raise` guards with centralized check
- Explicit `BEGIN`/`ROLLBACK` transactions on 6 compound write methods:
  `save_user()`, `save_guild_config()`, `save_subscriptions_for_channel()`,
  `save_all_subscriptions()`, `save_clan_family()`, `delete_notification_state_for_war()`

### 6. Persistent Thread-Local Sync Connections
**Eliminate per-call connect/close overhead for Phase-3 sync writes:**

- `_get_sync_conn()` caches a `sqlite3.Connection` in `threading.local()` storage
- Opened once per thread, reused for all subsequent sync writes (never closed per-call)
- All pragmas applied via `_apply_sync_pragmas()` on first open (WAL, synchronous=NORMAL,
  busy_timeout=5000, foreign_keys=ON, temp_store=MEMORY, cache_size=-65536 for 64 MB page cache)
- Eliminates ~1,500+ connect/close cycles per Phase-3 run on server-machine/HDD
- `sync_batch()` context manager uses persistent connection; only manages `_batch_deferred` flag

### 7. Retry-on-Locked for User-Facing Async Writes
**Ensure Discord UI interactions survive concurrent finalization bursts:**

- `_retry_on_locked(coro_factory)` — async retry helper with exponential backoff
  (0.5 → 1.0 → 2.0 → 4.0 s, 4 retries, ~7.5 s total before giving up)
- Catches `sqlite3.OperationalError` containing "database is locked"
- Re-creates the coroutine on each attempt (consumed coroutines cannot be re-awaited)
- Wrapped methods (user-facing, must never fail):
  `save_user()`, `save_guild_config()`, `save_subscriptions_for_channel()`, `save_clan_family()`,
  `save_leaderboard_message()`, `delete_leaderboard_message()`
- Each wraps an `_impl` method that holds `_write_lock` with `finally: release()` + `rollback()`,
  so retries always get a clean connection state
- Not wrapped (periodic/background — tolerate transient failure):
  `save_all_subscriptions()`, `delete_notification_state_for_war()`

### 8. Batched War DB Writes (P0 + P1)
**Reduce hundreds of individual commits to a handful of batched transactions:**

- `war_write_batch(batch_size=50)` — context manager that activates batch collection
  on the current thread. While active, `add_war_data_sync()` and `update_war_data_sync()`
  append data to thread-local lists instead of writing immediately.
- On context exit: `_flush_pending_war_writes()` executes all collected data in batched
  transactions (50 wars per COMMIT). Each batch:
  1. `PRAGMA wal_autocheckpoint=0` (suppress random I/O mid-burst)
  2. `BEGIN` → `executemany` all attacks + summaries → `COMMIT`
  3. Restore auto-checkpoint
  4. After all batches: `PRAGMA wal_checkpoint(PASSIVE)`
- `defer_file_move(src, dst)` — defers `os.replace()` when batch active; immediate otherwise.
  Ensures archive file moves happen AFTER DB writes succeed (consistency).
- `activate_war_batch_on_thread(appends, updates, file_moves)` — sets batch state on the
  current worker thread, pointing to shared lists. Used by Phase-3 `asyncio.to_thread()`.
- Two integration points:
  1. Orphan finalization: single thread + `war_write_batch()` wrapping all `manage_war_files()`
  2. Phase-3 loop: per-clan `asyncio.to_thread()` with `activate_war_batch_on_thread()`,
     then `flush_pending_war_writes()` after the loop

---

## Database-Only Mode (Current State)

### No Feature Flags
**All data storage is database-backed:**
- No `USE_DB_FOR_*` environment variables
- No conditional logic for JSON fallbacks
- Single code path: database always

### Data Flow
```
Discord Event
    ↓
QapBot.py (commands)
    ↓
cache_manager.py (business logic)
    ↓
db_manager.py (database operations)
    ↓
SQLite database (data/qapbot.db)
```

### Cache Manager Responsibilities
- **NEVER** directly access database
- ALL database operations via `CACHE.db_manager.*`
- Maintains in-memory cache for performance
- Handles save/load coordination

### Database Manager Responsibilities  
- Runtime code: **ONLY** module that imports `aiosqlite` / `sqlite3`
- ALL SQL queries contained here
- Connection management (WAL mode, pragmas)
- Error handling and logging
- Synchronous wrappers for Discord.py event loop compatibility

**Note:** Some one-off maintenance scripts under `qapbot/scripts/` may import `sqlite3` directly for offline validation/repair.

---

## Backup & Recovery

### Backup Strategy
```bash
# Daily automated backup via nightly VACUUM INTO (prod server-machine — runs automatically)
# DB lives at: ${PROD_DATA_DIR}/data/qapbot.db  (eSATA SSD)
# Since the hot/history DB split, back up BOTH files:
#   ${PROD_DATA_DIR}/data/qapbot.db          (hot: current + previous month)
#   ${PROD_DATA_DIR}/data/qapbot_history.db  (history: everything older)

# Manual backup before a risky change (run on server-machine shell)
cp ${PROD_DATA_DIR}/data/qapbot.db \
  ${PROD_BOT_ROOT}/backups/qapbot_$(date +%Y%m%d_%H%M%S).db
cp ${PROD_DATA_DIR}/data/qapbot_history.db \
  ${PROD_BOT_ROOT}/backups/qapbot_history_$(date +%Y%m%d_%H%M%S).db
```

> ⚠️ **Production data is accessible from Windows at `${PROD_SSD_UNC}`.**
> **NEVER access this path without explicit user confirmation — it is the live production database.**

```powershell
# Dev: manual backup
Copy-Item "data\qapbot.db" "data\qapbot_backup_$(Get-Date -Format 'yyyyMMdd_HHmmss').db"
```

### Database Integrity Check
```powershell
# Verify database health
sqlite3 "data\qapbot.db" "PRAGMA integrity_check;"

# Should return: ok
```

### Recovery From Corruption
```powershell
# 1. Stop bot
# 2. Check integrity
sqlite3 "data\qapbot.db" "PRAGMA integrity_check;"

# 3. If corrupted, restore from backup
Remove-Item "data\qapbot.db" -Force
Copy-Item "data\qapbot_backup_YYYYMMDD_HHMMSS.db" "data\qapbot.db"

# 4. Restart bot
```

---

## Performance Characteristics

### Query Performance
- **War history lookup**: <10ms for single clan/month
- **Leaderboard generation**: <50ms for 50-player clan
- **Full history scan**: figures below are stale (dated from when the DB held ~47K records,
  vs. millions today — see Database Size); not re-benchmarked at current scale, don't rely on
  the old <500ms figure

### Database Size

**⏱ Time-sensitive figures — verified 2026-07-26, not code-change-triggered.** Unlike most of
this doc (kept current via the "update docs in the same change" rule — see Cardinal Rule 14 in
`.github/copilot-instructions.md`), these numbers grow purely with usage over time, with no
single code change to hang an update on. The previous version of this section (~98K attack
rows, ~500 KB maindata) was many months stale before this refresh. Don't treat the numbers
below as current without re-checking if it's been a while — re-verify directly against
`data/qapbot.db` + `data/qapbot_history.db` (see query pattern in git history / ask for a
re-check) rather than trusting them long-term.

- **Hot DB** (`data/qapbot.db`): ~21.9 GB — `war_attacks` 65.1 M rows, `war_summary` 3.0 M rows,
  `clans` ~399 K rows, `player_name_index` ~6.2 M rows, `cwl_league_groups` ~524 K rows,
  `cwl_league_rounds` ~1.83 M rows
- **History DB** (`data/qapbot_history.db`): ~16.4 GB — `war_attacks` 48.3 M rows, `war_summary`
  2.36 M rows, `cwl_league_groups` ~262 K rows, `cwl_league_rounds` ~440 K rows
- **Combined**: ~38.3 GB total, ~113.4 M `war_attacks` rows, ~5.34 M `war_summary` rows
- **Maindata** (`users`/`user_players`/config tables): tiny by comparison — low hundreds of rows

### Optimization
- Indexes on all lookup columns (clan_tag, player_tag, war_id, date)
- Composite indexes: `war_attacks(war_id, clan_tag)`, `war_attacks(clan_tag, date)`,
  plus a UNIQUE(war_id, player_tag, attack_order) index; `war_summary` has a
  UNIQUE(war_id, clan_tag) index, `war_summary(clan_tag, date)` and `war_summary(clan_tag, cwl_season)`
- `idx_ws_war_id ON war_summary(war_id)` — added 2026-03-08; required for efficient JOIN
  between `war_attacks.war_id` and `war_summary.war_id` (without it SQLite full-scanned
  `war_summary` for every matching row in `war_attacks`)
- WAL mode for concurrent read performance
- Synchronous=NORMAL (faster writes, acceptable risk on server-machine)

### Query Anti-Patterns to Avoid
- **Full-table-scan subquery as LEFT JOIN target**: using a derived table like
  `SELECT player_tag, MAX(th_level) FROM war_attacks GROUP BY player_tag` as a LEFT JOIN
  materializes the entire `war_attacks` table for every call (~500ms each). Use column
  fallbacks (`COALESCE(NULLIF(wa.defender_th, 0), wa.th_level)`) instead.
- **Missing index on JOIN target**: always verify that the right-hand side of a JOIN has
  an index on the join column; SQLite will silently full-scan the target table otherwise.

---

## Migration History

### Phase 1: Infrastructure (Complete ✅)
- Created database schema
- Implemented WAL mode + pragmas
- Built db_manager.py module
- Established connection patterns

### Phase 2: War History (Complete ✅ → Retired in Phase 9)
- Migrated 47,220 war records from CSV to `war_history` table
- Production deployment: 2026-02-14
- **Retired**: Table dropped in Phase 9; all data migrated to `war_attacks`

### Phase 3: Maindata (Complete ✅)
- Migrated 7 JSON files to 13 tables
- Implemented foreign key relationships
- Removed all feature flags
- Exclusive database mode: 2026-02-15

### Phase 9: war_history Retirement (Complete ✅)
- Migrated 5,060 legacy-only rows from `war_history` → `war_attacks` (synthetic: attack_order=1, destruction=0.0)
- Stopped dual-write in QBcsvhandling.py
- Removed hybrid fallback query from `get_clan_attack_history_sync()`
- Deleted 13 legacy methods from db_manager.py (~638 lines)
- Dropped `war_history` table from schema
- Updated admin integrity command, scripts, and 12+ test files
- All 1307 tests passing

### 2026-05-09: CWL Round Tracking (Complete ✅)
- New tables: `cwl_league_groups` (1 row/clan/season; maps clan to its 8-clan group) and
  `cwl_league_rounds` (1 row/war; maps war_tag → round 1-7 + league_group_id)
- New column: `war_summary.round_number INTEGER` (NULL = regular war or pre-feature CWL)
- New index: `idx_cwl_league_groups_id ON cwl_league_groups(league_group_id, cwl_season)`
- New db_manager methods: `upsert_cwl_league_data()`, `get_cwl_round_for_war_tag_sync()`
  (`backfill_cwl_round_numbers()` was also added here but **removed 2026-05-15** — see below)
- Processing logic: `CacheManager._process_league_group_response()` + `_make_league_group_id()` —
  called automatically from `get_league_group()` on every fresh API response; also triggered
  by Layer 2 fallback in `fetch_war_data_from_api()` at finalization time when round unknown
- All changes idempotent: `CREATE TABLE IF NOT EXISTS`, `ALTER TABLE` guarded, `INSERT OR IGNORE`
- 1317 tests passing

### 2026-05-10: CWL Group Standings (Complete ✅)
- 5 new columns on `cwl_league_groups` via idempotent `ALTER TABLE` in `_setup_tables()`:
  `league_rank TEXT`, `cwl_ended INTEGER DEFAULT 0`, `group_rank INTEGER`,
  `total_stars INTEGER`, `total_destruction REAL`
- `league_rank`: populated by `CacheManager._process_league_group_response()` on every
  fresh `get_league_group()` API call (reads `warLeague.name` from raw response).
- `total_stars`: SUM(clan_stars) + SUM(CASE WHEN result='win' THEN 10 ELSE 0 END) —
  includes the 10-star win bonus per war to match in-game display.
- `total_destruction`: SUM(clan_destruction * team_size) — sum of all individual attack
  destructions (avg% × team_size per war) to match in-game display.
- New db_manager methods: `get_cwl_group_info()`, `get_cwl_group_war_stats()`,
  `update_cwl_group_stats_batch()`, `update_cwl_league_rank()`
- Migration script (one-time use, deleted after completion): `migrate_cwl_league_groups_v2.py` — backfilled
  `league_rank` for past seasons using majority-vote from `clans.war_league`.
  Tie fallback: median-by-tier heuristic (picks middle league when votes are tied).
  Tested on 2026-04 (1715 groups updated, 10 ties resolved by heuristic) and 2026-05.
- **2026-07-26 fix**: `_process_league_group_response`'s `clan_name_cache` fallback (used
  because `get_league_group()` doesn't return `warLeague` per clan) must gate on the league
  group's **live API `.state`** (`"preparation"` / `"inWar"` = safe, anything else = season
  already ended), not on the DB's own `cwl_ended` column. `cwl_ended` only flips once
  `update_cwl_group_stats_batch` observes every clan's expected war count in `war_summary`,
  which can lag well behind the real-world season end — and a clan's league in
  `clan_name_cache` reflects promotions/demotions almost immediately after the season ends.
  A group processed again in that lag window silently overwrote (or first-populated)
  `league_rank` with the clan's *next*-season league. Symptom: `/leaderboard cwlgroup` run
  after CWL ended showed the clan's post-promotion league instead of the one it actually
  competed in.
  **Same-day follow-up**: a second, related incident (confirmed against the in-game war log)
  showed the actual flaw is broader than just `cwl_ended` lag. Both the `raw_data` lookup and
  the `clan_name_cache` fallback pick a single "representative" clan's *current* league and
  apply it to the whole group. That's only valid while the season is active — group members
  share one league only by construction (grouping happens at that shared league). Once the
  real season ends, promotions/demotions apply almost immediately and members diverge to
  *different* current leagues (some up, some down, some unchanged), so any one of them can be
  wrong. Confirmed case: a group's `league_rank` was written as "Crystal League I" — the
  post-CWL league of two *demoted* bottom-of-group clans — while the group (and every other
  member, including the clan the standings were rendered for) had actually played that season
  in "Master League III". Fix: gate **both** sources (not just the cache fallback) on the
  league group's live `.state`; the `raw_data` path previously ran unconditionally regardless
  of season state.

### 2026-04-07: Index Cleanup + Partial Index (Complete ✅)
- Dropped 5 legacy duplicate indexes (exact duplicates of idx_wa_*/idx_ws_* indexes added earlier):
  `idx_war_attacks_clan`, `idx_war_attacks_player`, `idx_war_attacks_war`,
  `idx_war_summary_clan`, `idx_war_summary_date`

### 2026-05-15: player_name_index + /whois Name Search (Complete ✅)
- New table: `player_name_index(player_tag PK, player_name, last_seen)` (~125 K rows on prod)
- Replaces O(50 M) `LIKE '%substr%'` scan with an in-memory O(n) Python scan over ~125 K entries
  (<5 ms vs 50–100 s on server-machine).
- Loaded at startup into `CACHE.player_name_index` via `load_player_name_index_sync()`.
- Maintained by `_upsert_player_name_index_in_conn()` inside all war write paths.
- Sentinel rows (attack_order=0) ARE included (changed from excluded, also 2026-05-16: see below).
- Backfill script (one-time use, deleted after completion): `backfill_player_name_index.py`
  (offline, idempotent, ~60–120 s).
- Also removed `backfill_cwl_round_numbers()` from db_manager (was a post-season safety net now
  obsolete as all future CWL seasons start with round tracking active).
  Also removed `idx_ws_cwl_round_backfill` index (only used by the removed method).
  Also removed the Phase-3 call site in `QapBot.py` and a stale comment in `cache_manager.py`.
- 1347 tests passing after player_name_index; 1317 after backfill removal (same session).

### 2026-05-16: player_name_index — Sentinel Inclusion + API Name Sync (Complete ✅)
- Sentinel rows (attack_order=0) now included in player_name_index upserts (changed from excluded);
  ensures players who miss all attacks still appear in /whois name searches.
- New method: `update_player_name_index_sync(updates: List[Tuple[str,str,str]])` — batch-upsert
  triggered by `coc_cache.update_player_info_in_user_accounts()` when a live API member.name
  differs from the stored player["player_name"]. Uses same ON CONFLICT / WHERE excluded.last_seen
  > stored guard so API-sourced names always supersede older war-history names.
- Logs `[PLAYER-NAME-UPDATE]` per tag and `[PLAYER-NAME-INDEX]` summary per clan cycle.
- 1352 tests passing.

### 2026-05-17: players_tracked_count in DB Statistics (Complete ✅)
- `get_global_db_statistics_sync()` now returns `players_tracked_count`:
  `SELECT COUNT(*) FROM player_name_index`.
- Surfaced in `/status` command under Database Statistics.
  These doubled INSERT overhead on `war_attacks` (5.6M rows) and `war_summary` (265K rows)
  for zero lookup benefit — covered by the newer composite indexes.
- Added partial index `idx_wa_zero_attacks ON war_attacks(attack_order) WHERE attack_order = 0`
  Used by `get_global_db_statistics_sync()` to count real attacks via subtraction
  (total_rows − missed-attack rows) instead of a 5M-row filtered scan.
- All changes are idempotent: `DROP INDEX IF EXISTS` / `CREATE INDEX IF NOT EXISTS`

### Future Phases
**Not currently planned:**
- Phase 4: Temp war stats (JSON → DB)
- Phase 5: War archives (JSON → DB)

**Reason**: Current JSON-based approach works well for temporary/archive data. Database migration adds complexity with minimal benefit.

---

## Troubleshooting

### Bot won't start: "Database not initialized"
- Check database file exists: `Test-Path "data\qapbot.db"`
- Check file permissions (read/write)
- Review startup logs for specific error

### "Foreign key constraint failed"
- Indicates orphaned record (referencing non-existent parent)
- Use `/admin integrity` command for database consistency checks
- Or restore from backup

### "Database is locked"
- Common during heavy Phase-3 finalization: sync threads hold WAL writer lock
  during rapid sequential writes; async connection's busy_timeout may be exceeded
- Defense layers (in order):
  1. `PRAGMA busy_timeout=5000` on all connections (wait up to 5s for lock release)
  2. `synchronous=NORMAL` on sync connections (sub-ms commits, lock held briefly)
  3. `deferred_commit=False` in `sync_batch()` (WAL lock released between writes)
  4. `_retry_on_locked()` on user-facing async methods (exponential backoff 0.5→4s)
- If still occurring: check for zombie sqlite3 sessions or external DB browsers
- Restart terminal/IDE if needed

### Slow queries
- Run: `sqlite3 "data\qapbot.db" "PRAGMA optimize;"`
- Check indexes exist: `.schema` in sqlite3
- Review query patterns in logs

### Database corruption
- Stop bot immediately
- Run integrity check
- Restore from most recent backup
- Review server-machine stability (if on network storage)

---

## References

**Code:**
- Database manager: `qapbot/db_manager.py`
- Cache manager: `qapbot/cache_manager.py`
- Consistency check: `/admin integrity` command (replaces deprecated check_database_consistency.py)
- On-demand nightly maintenance runner (still present): `qapbot/scripts/run_db_maintenance_now.py`
- Maindata migration (completed, script deleted): migrate_maindata_to_db.py
- Maindata migration validation (completed, script deleted): validate_maindata_migration.py
- war_history migration (completed 2026-03-08, script deleted): migrate_war_history_to_war_attacks.py
- war_attacks backfill migration (completed 2026-03-08, script deleted): backfill_map_positions.py
- player_name_index backfill (completed, script deleted): backfill_player_name_index.py
- CWL league-group v2 migration (completed, script deleted): migrate_cwl_league_groups_v2.py

**Documentation:**
- Architecture: `qapbot/docs/CODE_STRUCTURE.md`
- Migration checklist: `MIGRATION_CHECKLIST.md`
