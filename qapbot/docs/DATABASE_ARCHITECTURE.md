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

### Gotcha: pragmas are schema-scoped, not connection-scoped

`PRAGMA journal_mode=WAL` (and `synchronous`) apply only to the schema they're run against.
Running the unqualified pragma on `main` BEFORE `ATTACH DATABASE ... AS history` does **not**
affect the newly attached `history` schema — it silently stays on SQLite's default rollback
journal mode, which does an fsync-heavy journal-file create/delete per commit. Barely noticeable
on fast local NVMe, catastrophic on NAS/external-SATA storage (2026-07 incident: a migration ran
at ~2MB/s on the PROD NAS vs ~2GB/hr on a fast Windows dev box).

Fix: after every `ATTACH DATABASE ? AS history`, explicitly run
`PRAGMA history.journal_mode=WAL` and `PRAGMA history.synchronous=NORMAL`. This must be repeated
at EVERY attach site — there is no single global place. As of 2026-07 the 5 sites are:
`initialize()`, `_reconnect()`, `_SyncConnectionPool._create_conn()`, the `_sync_conn()` fallback
path, and the shared `attach_history_db()` helper (used by all `qapbot/scripts/*.py` maintenance
scripts). If a 6th attach site is ever added, it needs the same two lines.

Verification trick: after the fix, a `<historydb>.db-wal` file should appear and grow during
writes. Its ABSENCE while writes are happening is the tell-tale sign the schema is still on
rollback-journal mode.

### Incident: silent column-order divergence corrupted every migrated `war_attacks`/`war_summary` row (2026-08-14)

**Root cause.** `main.war_attacks`/`war_summary` and `history.war_attacks`/`war_summary` have
identical column *names* today, but their columns physically sit at different `cid` positions —
because each schema's `CREATE TABLE` text in `db_manager.py` looks the same now, but the two
actual on-disk tables were extended via *separate* `ALTER TABLE ADD COLUMN` sequences at
different points in the project's history, and SQLite always appends a new column at the end of
physical storage regardless of where the `CREATE TABLE` source text places it logically.
`CREATE TABLE IF NOT EXISTS` never retroactively reorders an already-existing table to match
newer code, so nothing about matching CREATE TABLE text today proves the two live tables agree.

`_migrate_table_batch_by_date()`/`_migrate_cwl_table_by_season()` moved rows with
`INSERT INTO history.<table> SELECT * FROM main.<table> WHERE ...` — a bare `SELECT *` matches
source and destination columns by **position**, not name. Every row ever migrated therefore had
its trailing columns written into the wrong destination column, silently, with no error — for the
entire lifetime of the history DB (confirmed 2026-08-14: 100% of `is_cwl=1` `war_summary` rows in
`history` had `war_tag` holding the war *result* string `'win'/'loss'/'draw'` instead of a real
CoC war tag).

**Exact shift, `war_summary`** (`cid` 0-13 — `id` through `cwl_season` — are unaffected; the two
schemas happen to agree up to there):

| cid | `main` column (true meaning) | `history` column label (what actually got written there) |
|-----|-------------------------------|-------------------------------------------------------------|
| 14  | `result`                     | `war_tag` |
| 15  | `date`                        | `end_time` |
| 16  | `clan_lineup_json`            | `state` |
| 17  | `opp_lineup_json`              | `result` |
| 18  | `created_at`                   | `date` |
| 19  | `clan_attacks_used`            | `clan_lineup_json` |
| 20  | `opp_attacks_used`             | `opp_lineup_json` |
| 21  | `war_tag`                      | `clan_attacks_used` |
| 22  | `end_time`                     | `opp_attacks_used` |
| 23  | `state`                        | `round_number` |
| 24  | `round_number`                 | `created_at` |

**Exact shift, `war_attacks`** (`cid` 0-6 — `id` through `th_level` — are unaffected):

| cid | `main` column (true meaning) | `history` column label (what actually got written there) |
|-----|-------------------------------|-------------------------------------------------------------|
| 7   | `attack_order`                | `map_position` |
| 8   | `stars`                        | `attack_order` |
| 9   | `destruction`                  | `stars` |
| 10  | `defender_tag`                 | `destruction` |
| 11  | `max_attacks`                  | `defender_tag` |
| 12  | `missed_attacks`               | `defender_th` |
| 13  | `defensive_stars`              | `defender_map_position` |
| 14  | `created_at`                    | `duration` |
| 15  | `map_position`                 | `is_fresh` |
| 16  | `defender_th`                  | `times_defended` |
| 17  | `defender_map_position`        | `best_def_destruction` |
| 18  | `duration`                      | `max_attacks` |
| 19  | `is_fresh`                      | `missed_attacks` |
| 20  | `times_defended`                | `defensive_stars` |
| 21  | `best_def_destruction`          | `created_at` |

`cwl_league_groups`/`cwl_league_rounds` (the other two migrated tables) were checked the same way
and are **not** affected — their two schemas happen to still agree column-for-column. That was
luck, not a property the code ever verified; see Cardinal Rule 1.

**Why this is recoverable, not lost.** The shift is a pure, deterministic position swap — every
value is still present in the row, just filed under the wrong column name. Nothing needs
reconstructing from an external source for a row that already migrated; each row can be repaired
in place by reading it positionally and rewriting the values under their correct names. A backup
predating this bug (if one exists) is a good independent cross-check for the repair, not a
requirement for it.

**Fix shipped 2026-08-14**: both migration functions now build an explicit, named column list at
runtime (`_explicit_column_list()`, reading `PRAGMA main.table_info(<table>)`) and use it on both
sides of the `INSERT ... SELECT`, instead of `SELECT *`. This stops any *new* row from being
corrupted on its next migration, and turns a future genuine schema divergence into a loud SQL
error (`no such column`) instead of silent misalignment.

**Repair tool: `qapbot/scripts/repair_history_schema_drift.py`** (added 2026-08-14, redesigned
same day after the first two real runs). Detects drift per table by comparing `main`'s and
`history`'s actual on-disk column order, and — only for a table where they actually differ —
builds a correctly-labeled `<table>_repaired` copy (reading every row positionally and
re-inserting it under the right names), verifies it (row counts, a random sample checked for
plausible values), then swaps it in and preserves the original as `<table>_corrupted_backup`
(never dropped). Three explicit stages, each a strictly bigger commitment than the last:
- **(no flags, default) preview** — 100% read-only: schema comparison plus a `SELECT`-only sample
  of what the repair would produce, applied live against the original table via column-alias,
  never creating or writing anything. Verified by a test that checks the history DB file's size
  is byte-for-byte unchanged (not a content hash — `WarHistoryDB.initialize()`'s own
  schema-verification pass legitimately bumps SQLite's internal change counter on every open,
  which is harmless housekeeping unrelated to this script but does mean two genuinely-idle runs
  are never hash-identical; file *size* is the meaningful, bug-catching signal).
- **`--build`** — actually builds `<table>_repaired` for real (the only stage that writes
  substantial data), still never touching the original table's name, then verifies it. Batched
  (`--batch-size`, default 20,000 rows/commit) with a periodic `PRAGMA wal_checkpoint(PASSIVE)`
  (`--checkpoint-every-batches`, default 20) — the same pattern `_migrate_table_batch_by_date`
  already uses, and for the same reason: the first real run against `history.war_attacks` (78.6M
  rows) as one giant unbatched transaction grew the WAL to 6.5GB+ in under 2 minutes with zero
  progress output and had to be killed. Resumable from `MAX(id)` already in `_repaired` if
  interrupted.
- **`--apply`** — builds if not already fully built (instant if it is), verifies, and only if
  verification passes, swaps the repaired tables in (fast, atomic, one transaction) and recreates
  the original named indexes.

`--db`/`--history-db` default to `CONFIG.db_path`/`CONFIG.history_db_path`, the same DEV/PROD
auto-resolution `run_history_migration_now.py`/`run_db_maintenance_now.py` already use. 7 tests in
`tests/unit/test_repair_history_schema_drift.py`, including one using the exact real-world 11-column
`war_summary` drift shape (not just a simplified case) end to end, and one proving resumability
after a simulated interruption.

**Status as of 2026-08-14**: fix shipped and tested; a real `--build` run against DEV's
`history.db` (`war_attacks`: 78.6M rows, `war_summary`: 4.0M rows) was in progress as of this
writing. `--apply` (the actual swap) has **not yet been run against any real `history.db`** (DEV
or PROD) — that's a deliberate, separate, explicitly-authorized step once the build+verification
output has been reviewed. Until then, don't trust any
`history.war_attacks`/`war_summary` column beyond `id`, `war_id`, `clan_tag`, `date` (war_attacks
only — `war_summary.date` IS affected), `player_name`, `player_tag`, `th_level`.

📖 Prevention rule: `.github/copilot-instructions.md` Cardinal Rule 1.

### Follow-up: the same bug existed on the READ side too (found + fixed 2026-08-16)

The 2026-08-14 fix above only covered the *write* path — the monthly migration. While
investigating why the "Manage Enrollment" hover pop-up showed a player's Skill Score but not
their Attacks/Missed-Attacks/Attack-Defense-Ratio, the identical bug turned up on the *read*
side: every sync/async method that reads BOTH `main.<table>` and `history.<table>` into one
result set via `WITH x AS (SELECT * FROM main.t UNION ALL SELECT * FROM history.t)` also
matched columns by **position**, not name — so any row actually contributed by `history` (i.e.
any war old enough to have been archived) came back with `max_attacks`/`missed_attacks`/
`defensive_stars`/`map_position`/etc. silently misaligned, for exactly the same reason as the
write-path bug: `history`'s physical column order differs from `main`'s.

Confirmed against DEV's real `data/qapbot_history.db`: a raw diagnostic `SELECT * FROM
main.war_attacks UNION ALL SELECT * FROM history.war_attacks` for a real player showed
`max_attacks` holding an opponent tag string for her older (archived) attacks — while a plain
named-column `SELECT max_attacks, ... FROM history.war_attacks` against the same rows showed
correct, sane values. This means the *stored* data itself was NOT corrupted (the 2026-08-14
write-path fix was already doing its job) — only every one of these read queries was
misinterpreting it on the way out.

**Fix**: `WarHistoryDB._explicit_column_list_sync()` (sync counterpart of the existing async
`_explicit_column_list()`) and a public `explicit_column_list_sync()` alias for other modules
that share this class's `sync_conn()`. Every affected query — ~20 call sites across
`qapbot/db_manager.py` (both sync and async), `QBhelperfunctions.py` (`/whois` reliability/
activity/skill), and `qapbot/QBdiscocmdshelper_admin_command.py` (`/backfill_cwl_groups`) —
now names its columns explicitly on both sides of every `main`/`history` `UNION ALL`, immune to
physical order regardless of which schema a row actually comes from.

A second, still-live **write**-path instance of the original bug was also found and fixed in
the same pass: `WarHistoryDB._bulk_move_chunk()` (the one-time/manual `fast_bulk_history_
migration()` path, invoked via `run_history_migration_now.py --fast` for large catch-up runs)
used a bare `INSERT INTO history.<table> SELECT * FROM main.<table> ...` — never covered by the
2026-08-14 fix, which only touched `_migrate_table_batch_by_date`/`_migrate_cwl_table_by_season`
(the normal monthly-migration path). Fixed the same way.

**Coverage confirmed complete (2026-08-16, second pass)**: after initially leaving 3 manually-
invoked diagnostic/audit scripts unfixed as "lower priority," asked directly whether coverage
was actually 100% — re-audited the whole repo rather than re-asserting confidence. Grepped every
`SELECT *` in the entire codebase (not just the files already touched) and categorized each one;
also checked for positional (non-named) row access as a separate risk category. Result: `qapbot/
scripts/analyze_cwl_rounds.py` (9 query blocks), `qapbot/scripts/audit_cwl_league_rank.py` (1 —
confirmed it genuinely reads the drifted `state`/`result` columns, not just theoretically
exposed), and `qapbot/scripts/harvest_cwl_war_tags.py` (1) all needed the same fix, now applied
via a new shared `explicit_column_list_from_conn()` module-level helper (next to
`attach_history_db`) so standalone scripts share one implementation instead of each copy-pasting
it. `qapbot/scripts/backfill_group_track_war_updates.py` and `repromote_mid_season_clans.py`
(the other two scripts touching `history.*`) were directly inspected and confirmed to already
use fully explicit column lists. Confirmed via `PRAGMA table_info`/direct file inspection that
only 4 tables are ever physically present in `history.db` at all — every other `SELECT *`
anywhere in the repo targets a main-only table and is structurally immune to this bug class,
not just unaffected by luck.

**New standing guardrail — `WarHistoryDB.check_hot_history_schema_parity_sync()`**: called once
at bot startup (`QapBot.py`'s `initialize_database()`, logged at `CRITICAL` if non-empty).
Compares `main.<table>` vs `history.<table>` column **SET** (not order — a pure reorder is now
harmless everywhere, by design, and deliberately not flagged) for all 4 mirrored tables. A
genuine column SET mismatch (one schema got a column the other didn't) is not automatically
recoverable the way a reorder is, so this exists to fail loudly the moment a future migration
introduces one, rather than surfacing as a `no such column` error or a silently-missing stat
months later. Regression-tested in `tests/unit/test_hot_history_schema_parity.py`; the read-path
fix itself is regression-tested in `tests/unit/test_hot_history_read_query_column_alignment.py`
(reproduces the exact real drift shape found on DEV and proves the two most directly-implicated
query functions — `get_player_attack_history_sync`, `get_clan_attack_history_sync` — return
correct values despite it).

📖 Prevention rule: `.github/copilot-instructions.md` Cardinal Rule 1 (extend it to cover reads,
not just the migration write path, next time it's edited).

**Verification methodology check (2026-08-16, third pass)**: asked whether limiting the grep to
the literal string `SELECT *` was itself too narrow — a fair challenge, since it leaves two
different gaps unchecked: (a) `SELECT`/`*` split across separate lines, which a plain line-based
grep can't see, and (b) an explicit-column UNION where the two sides' column lists don't
actually match each other (a hand-typo bug with the identical symptom, not a wildcard-drift bug
at all). Checked both directly: a multiline-aware regex search for `SELECT` immediately followed
by `*` (any whitespace/newlines between them) found nothing new; a script parsing every
`SELECT {var} FROM main.T ... UNION ALL SELECT {var} FROM history.T` pattern this round's fixes
introduced (51 instances, across single- and multi-line forms) confirmed 0 mismatches — every
fix reused one Python variable on both sides rather than hand-typing two lists, so a copy-paste
slip wasn't structurally possible for those. The handful of pre-existing hand-written
explicit-column UNIONs that predate this session's fixes (`clan_tag`/`war_id`/
`league_group_id`-only unions in `get_all_war_attacks_existing_sync`,
`get_all_war_summary_keys_sync`, `get_all_war_clan_tags`, `get_global_db_statistics_sync`, and
`_handle_backfill_cwl_groups_inner`) were checked by hand and also match. A final broad,
case-insensitive, `DISTINCT`-aware `SELECT * FROM` scan across every file that even mentions
`history.` turned up nothing beyond the two docstring lines that describe the incident in prose.

### On-demand ops scripts

`qapbot/scripts/run_history_migration_now.py` and `qapbot/scripts/run_db_maintenance_now.py` let
you trigger the monthly hot→history migration / nightly VACUUM+ANALYZE maintenance immediately
via CLI (bot must be stopped first) instead of waiting for the scheduled window. Both are
idempotent and safe to interrupt/rerun.

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
- `guild_clan_custodians` - Per-clan war-notification custodians scoped to a guild (multiple
  discord_user_id rows per clan_tag within a guild_id, up to 5, selected via `/clan management`
  → notifications mode); their IDs are @mentioned in the channel war notification for that clan.
- `subscriptions` - Channel subscriptions for updates
- `leaderboard_messages` - Discord message tracking for updates

**Notification State** (Phase 6 - Complete):
- `notification_state` - Player DM notification tracking (prevent duplicates)
- `channel_notification_state` - Channel notification tracking (prevent duplicates)

**Cross-Restart State**:
- `bot_metadata` - Key-value store for state that must survive bot restarts
  (e.g. one-time migration markers, last-run timestamps).
- `bot_testers` - Bot-wide (not per-guild) allowlist of Discord user IDs for DM-testing gates.

**Bug/Feature Tracker** (`BUG_FEATURE_TRACKER_PLAN.md`, Phases 1-7 — 2026-08-20; full item
lifecycle, bridge, and MCP server, see the dedicated `BUG_FEATURE_TRACKER.md` doc):
- `bot_settings` - Bot-wide key/value settings, composite `PRIMARY KEY (guild_id, key)` with
  `guild_id = ''` meaning global (the only scope wired up today). Distinct from `bot_metadata`
  above: this table is for admin-configurable settings (e.g. tracker channel IDs), not
  internal cross-restart bookkeeping.
- `tracker_items` - One row per filed bug/feature report. `item_number` (AUTOINCREMENT) is a
  single shared `#NNNN` pool across both `item_type` values ('bug'/'feature'). Hot-only, no
  `history` mirror (Rule 1 parity does not apply; Rule 14 named-column access still does).
- `tracker_attachments` - Junction: tracker items ↔ uploaded files, `ON DELETE CASCADE`.
  `local_path` is the on-disk copy under `CONFIG.tracker_data_dir` (agent-readable, no expiring
  CDN URL); `discord_url` is the bot's own re-upload, not the reporter's original attachment URL.
- `tracker_testcases` - Junction: tracker items ↔ manual test-case rows, `ON DELETE CASCADE`.
  `seq` orders rows within one `(item_number, environment)` pair.
- `tracker_items.test_channel_id` / `test_message_id` (added via `_add_column_if_missing` inside
  `_create_tracker_schema()` itself, Phase 5) - pointer to the posted `#qapbot-test` message.

**CWL Round Tracking** (2026-05-09 - Complete):
- `cwl_league_groups` - One row per (clan × season) capturing which group each clan belongs to.
  PK: (cwl_season, clan_tag). Index on league_group_id so any clan can find its 7 group-mates.
  Columns added 2026-05-10: `league_rank` (e.g. "Master League II"), `cwl_ended` (0/1),
  `group_rank` (1-8), `total_stars` (with 10-star win bonus), `total_destruction` (sum of
  clan_destruction × team_size across all ended wars).
- `cwl_league_rounds` - One row per CWL war: maps war_tag → (cwl_season, cwl_round 1-7, league_group_id).
  PK: war_tag (globally unique). Joined to war_summary via war_tag to populate round_number.
  Populated by CacheManager._process_league_group_response() on every fresh get_league_group() call.

**Search Index** (2026-05-15 - Complete; extended 2026-08-17 - see CWL_PROD_PERFORMANCE_FIX_PLAN.md
Steps 9 & 11):
- `player_name_index` - Fast name-lookup index: one row per unique player_tag with their most
  recent known name and `last_seen` ISO timestamp. ~6.6 M rows on production (per the
  2026-08-16 PROD incident log analysis, CWL_PROD_PERFORMANCE_FIX_PLAN.md — supersedes the
  2026-07-26 ~6.2M figure above, which itself superseded an earlier stale ~125K estimate found
  in two docstrings and corrected the same day).
  Populated/maintained by `_upsert_player_name_index_in_conn()` inside every war write path
  (INSERT OR IGNORE ... ON CONFLICT DO UPDATE WHERE excluded.last_seen > stored).
  Also updated by `update_player_name_index_sync()` when coc_cache detects a live API name change.
  Sentinel rows (attack_order=0) ARE included so missed-war players appear in /whois searches.
  **No longer loaded into memory** (2026-08-18, PLAYER_NAME_INDEX_RETIREMENT_PLAN.md Steps 5-6 —
  `CACHE.player_name_index`, the in-memory `Dict[str, Tuple[str, str]]` mirror and its O(n)
  Python-side search, is retired; this table now exists purely as the write-through source that
  keeps `player_name_search`/`player_name_fts` below in sync). See the 2026-08-18 dated entry
  further down for the full retirement writeup.
- `player_name_search(player_tag PK, name, name_lower)` / `player_name_fts` (FTS5, trigram
  tokenizer) — 2026-08-17, Step 11, **unconditional since 2026-08-18** (the
  `CONFIG.cwl_use_fts_player_search` rollout flag and the in-memory fallback it gated were both
  retired once DEV+PROD burn-in confirmed parity — see below). `player_name_search` is a plain
  table (PK-indexed, backs the CWL guest search's `#` tag-PREFIX mode — `player_tag LIKE
  ?||'%'`); `player_name_fts` is FTS5 with the trigram tokenizer (backs actual name-SUBSTRING
  search) — feasibility confirmed live via SSH on both DEV (SQLite 3.50.4) and PROD (SQLite
  3.45.2), both fully support FTS5 + trigram. Kept in sync by the SAME two writers as
  `player_name_index` above (`_upsert_player_name_index_in_conn` / `update_player_name_index_
  sync`), re-reading the just-upserted `player_name_index` row rather than trusting the write
  batch's own value directly, so a WHERE-guarded "not newer, skip" outcome there can never leave
  these two tables holding a stale name `player_name_index` itself rejected. One-time idempotent
  backfill on every startup (`_backfill_player_name_search_if_needed()`), guarded by a row-count
  comparison against `player_name_index` so it's a no-op once already in sync. `/whois`'s own
  search (`QBdiscordcmds.py`) uses a different, two-step design — see the 2026-08-18 dated entry
  further down — rather than calling `search_player_names_sync` directly, since it needs
  guild-member completeness a plain `LIMIT`-based query can't guarantee on its own.
  - **2026-08-17 follow-up fix — `player_name_fts` rowid**: `player_tag` is declared `UNINDEXED`
    in the FTS5 schema, which only excludes it from full-text `MATCH` — it does NOT give SQLite
    any index for a plain equality lookup, so `DELETE FROM player_name_fts WHERE player_tag = ?`
    (the incremental writer's original pattern) falls back to a full table scan. That was
    invisible in tests but stalled every PROD update cycle right after the startup backfill
    populated the table to its real ~6.6M rows — one flush batch's worth of changed players
    turned into hundreds of full scans back to back. Fixed by giving `player_name_fts` an
    explicit `rowid` — the one column FTS5 genuinely indexes — mirrored from
    `player_name_search`'s own SQLite-assigned rowid (looked up, never computed): one bulk
    `SELECT ... rowid FROM player_name_search` for the backfill, one small chunked lookup for the
    incremental writer. Stable across repeated upserts since `ON CONFLICT DO UPDATE` never
    changes a row's rowid. A `bot_metadata` marker (`player_name_fts_rowid_scheme` =
    `player_name_search_rowid_v2`) forces one full rebuild of any table populated under an older
    scheme, since the row-count guard alone can't detect a stale rowid scheme.
    - **v1 → v2**: the first version of this fix (`tag_hash_v1`) derived the rowid from a
      per-tag `hashlib.blake2b()` call instead, specifically to avoid the DB round-trip above.
      That was the wrong trade-off: measured at 355.25s for ~6.6M rows on DEV (fast hardware),
      and PROD's much weaker CPU never finished it at all — it hit `QapBot.py`'s 30-minute DB-init
      safety timeout and failed to start. v2's lookup approach measured 82.42s for the same
      dataset on the same DEV hardware (4.3x faster), since it replaces 6.6M individual Python
      hash calls with a single bulk SQL query.
  - **2026-08-18 — `/whois` migrated too, via a two-step search, not a bigger cap**
    (`PLAYER_NAME_INDEX_RETIREMENT_PLAN.md` Steps 1-3): the note above ("out of this step's
    scope") is now out of date. A naive migration — just pointing `/whois` at a higher-`LIMIT`
    FTS5 reader — would have reintroduced the exact same completeness problem one order of
    magnitude later (a guild member sorting past a 5000-match global cap, instead of past 25).
    Fixed instead with a two-step design: `QBdiscordcmds._build_guild_player_name_matches()`
    does an always-complete, uncapped Python substring match over the guild's OWN player pool
    (built fresh from the same 3 in-memory CACHE sources the old post-search reorder step used —
    `user_accounts`, `temp_war_stats`, `coc_clan_cache` — small enough, a few hundred entries at
    most, to run directly on the event loop with no threading needed), and only THEN does
    `db_manager.search_player_names_full_sync()` (new, `hard_cap=5000`, shares its FTS5 MATCH
    core with `search_player_names_sync` via `_search_player_names_fts_sync`) fill in the
    "everyone else" fallback, deduplicated by tag. Guild matches are structurally first in the
    concatenation, so they can never be pushed out by the `[:25]` UX slice regardless of how
    many unrelated global matches exist — the ordering guarantee is positional, not a sort key.
    Replaces the old separate `tag_to_clan`-based reorder step entirely (redundant once the
    guild pass already produces that same split by construction). New i18n key
    `commands.whois.player_search_too_short` — the 3-character FTS5 trigram floor only gates
    the global fallback, not the guild pass, which has none.
  - **2026-08-18 — `CACHE.player_name_index` (the in-memory dict) and
    `CONFIG.cwl_use_fts_player_search` (the rollout flag) both retired**
    (`PLAYER_NAME_INDEX_RETIREMENT_PLAN.md` Steps 5-6), once the Step 4 DEV/PROD checkpoint and
    live PROD guest-search burn-in both confirmed the SQL path fully replaces the in-memory
    scan. `CACHE.search_player_names()` now unconditionally delegates to
    `db_manager.search_player_names_sync()`, with no fallback branch — the flag's `False` branch
    and its in-memory implementation are both gone, not just disabled. Removed:
    `CACHE.player_name_index` itself, `load_player_name_index()`/`_load_player_name_index_sync()`
    (the startup loader), `set_player_name()` (the writer — `coc_cache.py`'s
    `update_player_info_in_user_accounts()` keeps its `update_player_name_index_sync()` DB
    write-through, just drops the now-pointless in-memory mirror call alongside it),
    `_player_name_tuple()` and `SEARCH_PLAYER_NAMES_MAX_COLLECT` (both now-unused helpers),
    `web_bridge.py`'s in-memory fallback branch in the guest search's `#` tag mode, and
    `cwl_use_fts_player_search` from `config.py`/`.env` entirely. One residual behavior change:
    `QBdiscordcmds._build_guild_player_name_matches()`'s source-2-only fallback (a tag known only
    via `temp_war_stats`, with no name from `user_accounts`/`coc_clan_cache`) used to resolve a
    display name via `CACHE.player_name_index`; with that gone, such a tag is now silently
    skipped rather than adding a DB round-trip (and the `asyncio.to_thread()` wrapping it would
    require) to what's meant to stay a fast, in-memory-only, event-loop-safe pass — accepted as a
    narrow edge case (e.g. a player who left their clan mid-war). `player_name_index` the TABLE
    is untouched and still the write-through source of truth for `player_name_search`/
    `player_name_fts` above — only the in-memory `CACHE` mirror and its own O(n) scan are gone.

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
- `guild_clan_custodians.guild_id` → `guild_config.guild_id` ON DELETE CASCADE
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
this doc (kept current via the "update docs in the same change" rule — see Cardinal Rule 15 in
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
- **Never `LEFT JOIN` a `UNION ALL` CTE** (e.g. `ws AS (SELECT * FROM main.war_summary UNION ALL
  SELECT * FROM history.war_summary)` joined via `FROM wa LEFT JOIN ws ON ...`): SQLite cannot use
  a co-routine for the right-hand side of a LEFT JOIN, so it MATERIALIZEs the compound subquery in
  full BEFORE the join — scanning every row of both underlying tables (multi-million rows on prod)
  on every call, regardless of how selective the outer query is. This caused `/whois player` to
  take 59s+ (and OOM under load) after the 2026-07-11 hot/history split introduced the pattern.
  Confirm via `EXPLAIN QUERY PLAN` ("MATERIALIZE ws" + "SCAN main.X"/"SCAN history.X"). Fix:
  `LEFT JOIN` each physical table directly (`LEFT JOIN main.war_summary ws_h ... LEFT JOIN
  history.war_summary ws_a ...`) and `COALESCE(ws_h.col, ws_a.col, ...)` the columns — each table
  keeps its own index usable for a cheap per-row SEARCH instead of a full scan (verified: 59s →
  0.02s on the same DB). `INNER JOIN`s and scalar subqueries against a `UNION ALL` CTE do **not**
  have this problem — only `LEFT JOIN` triggers forced materialization. Fixed functions
  (2026-07-16): `get_player_war_history_sync`, `get_player_attack_summary_sync`,
  `get_player_monthly_star_dist_sync`. Check `EXPLAIN QUERY PLAN` for "MATERIALIZE" before
  shipping any NEW query joining hot+history data.
- **Ambiguous column name after splitting one UNION-ALL CTE into two direct joins**: when
  rewriting `LEFT JOIN ws` (single CTE) into `LEFT JOIN main.X ws_h ... LEFT JOIN history.X ws_a`
  (the fix above), watch for any `GROUP BY`/`ORDER BY` that reuses a column name which is a REAL
  column on the underlying table (e.g. `is_cwl`, `cwl_season`). With one merged CTE only one
  source exposed that column name, so SQLite resolved it unambiguously. With two separately
  joined tables BOTH now expose it, so a bare `GROUP BY is_cwl` becomes genuinely ambiguous even
  though the SELECT list defines an `is_cwl` alias via `COALESCE`. Fix: `GROUP BY`/`ORDER BY` the
  full expression (`COALESCE(ws_h.is_cwl, ws_a.is_cwl, ...)`), not the bare alias. Bit
  `get_player_monthly_star_dist_sync` and `compute_roster_stats_sync` after the 2026-07-16 fix —
  check every `GROUP BY`/`ORDER BY`, not just the SELECT list, when doing this kind of rewrite.
- **`COUNT(*)`/`COUNT(DISTINCT)` over hot+history has no O(1) shortcut**: `wars_count`/
  `attacks_count` in `get_global_db_statistics_sync()` require a full index scan across BOTH hot
  and history `war_summary`/`war_attacks` (5M+ and 94M+ rows) — SQLite cannot know these counts
  without scanning, even across a `UNION ALL` of two attached schemas. Direct cause of `/status`
  being slow (~3s fast local SSD, 20+s prod NAS). Since it's a reporting stat with no
  business-logic dependency, fixed with a TTL cache on the `WarHistoryDB` instance
  (`_global_stats_cache` / `_GLOBAL_STATS_TTL`, raised to 25h on 2026-08-03) rather than
  incremental counters (which would require touching every insert/delete site — much higher risk
  for a cosmetic stat). 2026-08-18: the 20+s cost itself was found live on PROD to matter even
  with the cache, because the fire-and-forget startup warm-up call shared `WarHistoryDB`'s
  8-connection sync pool with the periodic clan-fetch cycle's Phase-1 (also starting at bot
  startup), starving it for the scan's whole duration. Two independent fixes, both in
  `get_global_db_statistics_sync()`: (1) the 5 sub-queries now run concurrently instead of
  sequentially on one connection, cutting wall time to roughly the slowest single query; (2) every
  exact computation persists its result to `bot_metadata` (JSON, with a wall-clock
  `computed_at_utc`), and the plain startup warm-up (`QapBot._warm_global_db_stats_cache()`,
  `force_refresh=False`) now restores that snapshot via `preload_global_db_statistics_from_
  snapshot()` — a single tiny row read — instead of ever re-running the scan cold on a restart.
  Only a brand-new DB with no snapshot yet, or an explicit `force_refresh=True` (nightly
  maintenance / manual `/status` refresh), still pays the real (now-parallelized) scan cost.

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
  **Bulk repair + permanent self-heal**: the write-time fixes above don't repair rows already
  corrupted before they landed, and a frozen (`cwl_ended=1`) row's `league_rank` was never
  re-examined afterwards. Two additions close that: (1) `qapbot/scripts/audit_cwl_league_rank.py
  reconstruct` — a one-time, mostly-zero-API-call bulk repair per season (see the script's
  docstring); ran against season 2026-07, corrected 7,404 of 26,467 groups. (2)
  `QBhelperfunctions._cwl_self_heal_league_rank`, wired into `update_cwl_group_stats`'s
  `cwl_ended=1` short-circuit — every time an ended group's standings are served, it
  cross-checks the frozen `league_rank` against the current live league of any group member
  ranked outside the top-3/bottom-2 band (a rank no promotion/demotion rule version can move),
  using `clan_name_cache` only (no API calls). Only trusted within a bounded window after the
  season ends (until roughly when the *next* season's own promotions would apply — past that,
  "current league" may reflect a later season, not this one). Corrections bypass the freeze via
  `db_manager.update_cwl_league_rank(..., force=True)` — a new parameter that drops the
  `cwl_ended` guard specifically for this already-verified corrective path, never for the
  original derivation path the guard was built to constrain.
  **Root-cause fix, replacing the `.state` gate above**: the `.state`-based gate treated a
  symptom, not the disease. The actual defect was that `_process_league_group_response` had no
  concept of "first time seeing this group" vs. "re-processing a group already recorded" — the
  same "trust any member's current league" logic ran on *every* call, gated only on whether the
  API currently reported the season as active. The real fix: only ever populate `league_rank`
  when `db.cwl_group_exists(group_id, season)` reports no row existed before this call. That is
  the one moment every member is *guaranteed* to share one league — a freshly-discovered group
  is necessarily still within its own active season, no `.state` inspection needed. An
  already-known group's `league_rank` is now never touched again by this function, regardless
  of what the API reports; `update_cwl_group_stats`'s self-heal remains the only other path that
  can touch it later, and only via the verified safe-rank cross-check. This also simplified the
  fallback: raw_data → fresh `clan_name_cache` entry (8-day staleness check, kept — a cached
  value can be stale from long before this brand-new group even without any season-timing
  concern) → exactly one `get_clan()` call if nothing fresh is cached. New
  `db_manager.cwl_group_exists()` method. Tests replaced:
  `tests/unit/test_cwl_league_rank_season_gate.py` (tested the now-removed `.state` gate) →
  `tests/unit/test_cwl_league_rank_new_group_gate.py` (9 tests for the new-group gate).

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

### 2026-07-30: Leaderboard "scope=all" — Cross-Clan Player History (Complete ✅)
- New method `get_player_attack_history_sync(player_tags, month, year)`: same aggregation as
  `get_clan_attack_history_sync()` but filtered by `WHERE player_tag IN (...)` instead of
  `clan_tag = ?`. Uses `idx_wa_player_tag_date(player_tag, date)` (added 2026-07-30, see below)
  — one index range scan per player tag scoped to the requested month, not a full-table scan.
- `WarID` is returned as the composite `"{clan_tag}::{war_id}"`. Reason: `war_id` is only unique
  per `(war_id, clan_tag)` pair — it's derived from the *opponent* tag + date, from that clan's
  own point of view (see `war_id = f"{opponent_tag}_{start_dt_compact}"` in
  `QBhelperfunctions.py`) — so two different home clans can coincidentally collide on the same
  war_id string (e.g. both fought the same opponent on the same day). The composite key avoids
  merging two unrelated wars into one `Wars_Count` entry when aggregating across clan_tags.
- `get_war_summaries_sync(clan_tag, ...)` now accepts `clan_tag=None` to query CWL-season summary
  rows across every clan_tag (needed to resolve `cwl_season` filtering for the cross-clan query
  above, since `war_summary` rows also carry their own `clan_tag`).
- Purpose: the `/leaderboard` command's new `scope="all"` option (default) credits a player
  currently rostered in a tracked clan for wars fought while registered to a clan that is no
  longer tracked/subscribed (e.g. they switched clans mid-month). `scope="own"` restores the
  previous per-clan-only behavior. See `QBhelperfunctions._load_history_rows()` /
  `calculate_leaderboard(scope=...)` for the dispatch, and `QBdiscordcmds.leaderboard()` for how
  the current roster (`CACHE.coc_clan_cache.get_clan()`) is resolved per target clan/family.
- Chunks the player-tag `IN (...)` clause at 400 tags per query to stay under SQLite's default
  host-parameter limit even for large clan families.
- Not cached across calls (unlike `_load_history_filtered`'s per-clan history_cache) — this path
  is only reached from the manual `/leaderboard` command, not the automatic per-subscription
  posting loop, so the extra per-invocation DB round trip is not a concern.
- 1457 tests passing.

### 2026-07-30: Leaderboard scope="all" Perf Fix — Composite Index + Parallel Roster Fetch (Complete ✅)
- Reported symptom: `/leaderboard` (scope="all", the new default from the entry above) took
  *minutes* to respond on prod, not the sub-second DB query expected from an indexed lookup.
  Root-caused to two separate issues, both fixed:
  1. **DB query**: `get_player_attack_history_sync()`'s `WHERE player_tag IN (...) AND date
     BETWEEN ...` only had `idx_wa_player_tag(player_tag)` to work with — a single-column index.
     SQLite narrowed to the right player_tag via the index, but then had to rowid-fetch *every*
     row that player ever has in `war_attacks`, across all time, just to discard everything
     outside the requested month. For a long-tenured player with a large history, multiplied by
     every player_tag in a family's roster, that's a lot of avoidable I/O.
     Fix: added composite index `idx_wa_player_tag_date(player_tag, date)` to both the main and
     history `war_attacks` schemas. **Note (2026-07-30, superseded below):** this was originally
     built inline inside `initialize()`'s awaited schema-creation path — that caused a startup
     hang on prod; see the entry below for the corrected background-build approach.
     The old single-column `idx_wa_player_tag` is deliberately **kept, not dropped** — `war_attacks`
     has ~5.6M rows, and `DROP INDEX` on a table that size is slow enough that it belongs in
     nightly maintenance, not something to run unconditionally on every connection open/startup
     the way `CREATE INDEX IF NOT EXISTS` safely can. Added `idx_wa_player_tag_date` to
     `major_indexes` in `run_nightly_maintenance_routine()`'s REINDEX list.
  2. **Roster fetch (the dominant cost in practice)**: `QBdiscordcmds.leaderboard()`'s scope="all"
     roster resolution awaited `CACHE.coc_clan_cache.get_clan()` **one clan at a time in a plain
     for-loop**. A cache miss/expiry there is a live CoC API call — with no clan/family filter
     given (the common case: a channel's full set of subscribed clans/families), that's a
     sequential chain of live API calls, one per constituent clan, which is exactly what turned
     into minutes. Fixed by deduplicating every distinct constituent clan across all requested
     tags up front and fetching them all in a single `asyncio.gather(..., return_exceptions=True)`
     batch — the same pattern already used by `ui_clan_management.py`'s guild-clan refresh.
  Files: qapbot/db_manager.py (composite index ×3 creation sites + major_indexes),
  QBdiscordcmds.py (parallel roster gather).
  Tests: 1464 passed (up from 1462; +2 new tests — composite index exists + is chosen by the
  query planner via EXPLAIN QUERY PLAN). pyright: 0 errors.

### 2026-07-30: Startup Hang Fix — Composite Index Build Moved to Background Task (SUPERSEDED — see next entry ⚠️)
- Reported symptom: after the perf fix above shipped, the bot failed to start on prod at all —
  `❌ Startup login timed out after 60 seconds (includes DB init + CoC API authentication)`. Logs
  showed the hang starting right at `[DB-SCHEMA] Verifying war_attacks table + indexes...` with
  nothing further for the full 60s.
- Root cause: `idx_wa_player_tag_date` (added above) was created via `await self._conn.execute(...)`
  inside `_create_schema()`/`_create_history_schema()`, which are awaited synchronously inside
  `WarHistoryDB.initialize()` — itself awaited inside `startup_login()`, which
  `QapBot.py`'s `on_ready()` wraps in `asyncio.wait_for(..., timeout=60.0)`. Building this index
  for the FIRST time on `history.war_attacks` (~5.6M rows) is a full table scan + sort that takes
  minutes on server-machine/SATA storage — far more than the 60s login budget. Every subsequent
  restart would have been instant (`CREATE INDEX IF NOT EXISTS` is a cheap metadata check once the
  index exists) — it was purely the one-time build that broke startup.
- Fix:
  1. Removed the inline `CREATE INDEX ... idx_wa_player_tag_date` calls from `_create_schema()`
     (main) and `_create_history_schema()` (history) entirely — these async methods no longer
     touch this index at all.
  2. Added `WarHistoryDB._build_composite_indexes_background()`: checks for the index on both
     `main.*` and `history.*` (cheap `sqlite_master`/`history.sqlite_master` lookup) and builds
     whichever is missing, using a dedicated plain `sqlite3` connection opened via
     `asyncio.to_thread` — same pattern as `run_nightly_maintenance()`'s `_run()`. Uses the same
     server-machine-safe pragmas as maintenance (`temp_store=FILE`, 128 MB `cache_size`) rather
     than the hot-path pragmas (`temp_store=MEMORY`, 8 GB `mmap_size`) — building a composite
     index over millions of rows needs an external sort, the same RAM profile as `VACUUM`, and the
     hot-path pragmas risk the OOM-kill scenario already documented for `VACUUM` above.
  3. `initialize()` schedules this via `asyncio.create_task(...)` (fire-and-forget, NOT awaited)
     as its very last step, after the sync connection pool is built. `initialize()` therefore
     returns immediately regardless of table size — Discord login, cache loading, and the rest of
     `on_ready()` all proceed without waiting on the index build.
  4. `_create_history_schema_sync()` (the sync/pool-fill helper, also used by tests and migration
     scripts) gained a `build_expensive_indexes: bool = True` parameter.
     `_SyncConnectionPool._create_conn()` passes `False`: pool-fill (`_fill()`) runs *synchronously
     on the event-loop thread* inside `initialize()` (it's plain blocking Python, not awaited) — if
     it tried to build this index too, the entire bot process would freeze for the full build
     duration (worse than the original bug, which only broke one coroutine's timeout). Tests and
     standalone migration/diagnostic scripts keep the default `True` — their DBs are small/fresh,
     so building inline there is fine and expected.
  - Correctness is unaffected either way: absence of the index just means queries fall back to the
    slower `idx_wa_player_tag` single-column index (rowid-fetch + filter) until the background
    build finishes — a temporary performance cost, never wrong results.
  Files: qapbot/db_manager.py.
  Tests: 1466 passed (up from 1464; +2 new — `build_expensive_indexes=False` skips only the
  composite index, and the background task actually builds it on both schemas after `initialize()`
  returns). pyright: 0 errors.

### 2026-07-30: Startup Hang Fix, Take 2 — Composite Index Build Moved to `nightly_db_maintenance()` (SUPERSEDED — see next entry ⚠️)
- The background-task fix above (fire-and-forget `asyncio.create_task`) shipped, and the bot
  started correctly — but immediately afterward, live production logs showed PHASE-1's clan-fetch
  writes failing en masse: `[DB-WRITE-THROUGH] Failed to persist clan #...: database is locked`,
  cascading into `WarDataFetchError` for every affected clan.
- Root cause: moving the build off the event loop via `asyncio.to_thread` only solved *that*
  problem. `CREATE INDEX` is a write transaction that holds SQLite's single writer lock (even in
  WAL mode, only one writer at a time) for its *entire* build — which thread runs it is irrelevant.
  The background task opened its own raw connection with zero coordination with the rest of the
  app: no pool drain, no `db_maintenance_mode` flag, nothing. It started building
  `idx_wa_player_tag_date` on `history.war_attacks` at the exact moment `periodic_main()`'s
  PHASE-1 was doing parallel live-clan-data writes for ~1555 clans — every one of those writes
  hit the lock and failed once its (short, hot-path) `busy_timeout` expired.
  **General lesson: `asyncio.to_thread`/a background task only keeps a blocking DB operation off
  the event loop. It does nothing to stop that operation from locking out every other writer in
  the app for its full duration — for anything that takes longer than a moment, that coordination
  has to be explicit (pool drain / maintenance-mode flag / scheduled low-traffic window), not
  assumed away by "it's a background task now".**
- Fix: removed the background task entirely (and `_composite_index_build_task`). `initialize()`
  now calls `_warn_if_composite_index_missing()` — a cheap **read-only** `sqlite_master` check
  that only logs a warning if the index is missing, never writes anything. The actual
  `CREATE INDEX` moved into `nightly_db_maintenance()`'s `_run()`, as a new "Step 1.5" right after
  the WAL checkpoint and before REINDEX/VACUUM — the one place in this codebase that already:
  1. `self._pool.drain(timeout=120)`s first — waits for in-flight sync workers to finish, then
     makes new `pool.acquire()` calls raise a clean `RuntimeError` ("draining for maintenance")
     instead of racing for the lock and failing with a raw `OperationalError`.
  2. Sets `QBcore.db_maintenance_mode = True` — blocks Discord commands with a friendly message
     for the duration.
  This runs automatically on the next scheduled nightly maintenance. To build the index sooner
  without waiting for that schedule, trigger maintenance manually via the `/admin` "Optimize DB"
  action or `qapbot/scripts/run_db_maintenance_now.py` — both call the same
  `nightly_db_maintenance()` method, during a time of your choosing rather than immediately after
  a live restart.
  Files: qapbot/db_manager.py.
  Tests: 1467 passed (up from 1464 net; replaced the background-task test with
  `test_initialize_never_builds_composite_index_itself` and added
  `test_nightly_maintenance_builds_missing_composite_index`). pyright: 0 errors.

### 2026-07-30: Startup Ordering Fix (Final) — DB Init Sequenced Strictly Before CoC Login (Complete ✅)
- The maintenance-deferred fix above worked, but had a real usability cost: a fresh deploy
  wouldn't get the leaderboard scope="all" perf benefit until the next scheduled nightly run (or
  a manually-triggered one) — and it meant "ensure schema is fully up to date" was split across
  two different code paths (`_create_schema()` for everything except this one index, and
  `nightly_db_maintenance()` for this index alone) for no reason a future maintainer could infer
  from either file in isolation.
- **Root insight**: the actual danger was never "building this index inline" — it was that other
  code (`periodic_main()`'s PHASE-1) was already writing to the DB concurrently by the time the
  build ran. Fix the ordering, not the index.
- Fix: added `QapBot.initialize_database()`, called as a new "Step 1.5" in `on_ready()` — strictly
  BEFORE Step 2 (CoC login) and, since `periodic_main()` only starts later in the same
  initialization sequence, strictly before any other code path in the app could possibly write to
  the DB. It:
  1. Calls `WarHistoryDB.initialize()` under its own `asyncio.wait_for(timeout=1800.0)` — 30
     minutes, generous enough for a legitimately slow first-run migration, but bounded so a
     genuinely stuck migration doesn't hang forever silently. This is now fully decoupled from the
     CoC-login timeout, which stays a tight 60s in `on_ready()`'s Step 2 (pure API auth has no
     legitimate reason to be slow).
  2. Sets `QBcore.db_maintenance_mode = True` for the duration — the same flag
     `nightly_db_maintenance()` already uses, so Discord commands get the existing friendly
     "maintenance in progress" message instead of erroring. Discord's gateway connection is already
     established by the time `on_ready()` runs (that's what triggers `on_ready()` in the first
     place), so the bot still shows "online" throughout — only command *execution* is gated.
  `startup_login()` had its DB-init block removed entirely — it's now purely CoC client
  creation + login, decoupled from database concerns.
- `idx_wa_player_tag_date` moved back inline into `_create_schema()` (main) and
  `_create_history_schema()` (history) — built exactly the same way as every other index in this
  codebase, with no special-casing. It's safe now purely because of *when* `initialize()` runs,
  not because of anything clever inside `_create_schema()` itself. Logs the elapsed time only when
  a build exceeds 5 seconds, so a normal (post-first-run) restart stays quiet.
- Removed `nightly_db_maintenance()`'s "Step 1.5" create-if-missing block from the entry above —
  redundant now that `initialize()` always guarantees the index exists before maintenance could
  ever run. Maintenance's existing REINDEX loop (`major_indexes`, which already lists
  `idx_wa_player_tag_date`) still periodically rebuilds it for fragmentation, same as any other
  index — that's a distinct, ongoing concern from one-time creation.
- `_create_history_schema_sync()`'s `build_expensive_indexes` flag (used by
  `_SyncConnectionPool._create_conn()`, passed `False`) stays as a defensive measure: pool-fill
  runs synchronously on the event-loop thread inside `initialize()`, and must never attempt this
  build itself even though, in the current ordering, `_create_schema()`/`_create_history_schema()`
  (which run first) always beat it to it.
- **General lesson (kept from the previous entry, still the core takeaway)**: a background task
  or `asyncio.to_thread` only keeps a blocking DB operation off the event loop — it does nothing to
  stop that operation from locking out every other writer in the app for its full duration. The
  only real fix for a rare-but-slow exclusive DB operation is controlling *when* it can run
  relative to everything else that might write concurrently: either a dedicated, coordinated
  maintenance window (drained pool + blocked commands), or — better when the trigger is "this
  needs to exist before the app is fully useful anyway" — sequencing it before any concurrent
  writer can exist in the first place.
  Files: QapBot.py, qapbot/db_manager.py.
  Tests: 1467 passed. pyright: 0 errors (QapBot.py, qapbot/db_manager.py).

### 2026-08-01: Disk-Full Incident During First-Ever Monthly Migration — WAL Growth Bounded (Complete ✅)
- **Incident**: The 2026-08-01 03:00 UTC nightly window ran `monthly_history_migration()` for the
  first time against the full accumulated `war_attacks` backlog (~8.3M+ rows past the July cutoff,
  never migrated before this feature existed at this scale). The migration sets
  `PRAGMA wal_autocheckpoint=0` on both `main` and `history` for the *entire* run (by design, to
  avoid random-I/O stalls from checkpointing every 5000-row batch), restoring it + running one
  `wal_checkpoint(PASSIVE)` only in the `finally` block at the very end. For a run that finishes in
  minutes this is fine; for the first run against an 8M+-row backlog it took ~4h45m
  (02:00:29→06:44:54) and never checkpointed once in that window — both WAL files grew unbounded
  the entire time until the volume hit 0 bytes free (`qapbot.db-wal` reached 287.9 GB,
  `qapbot_history.db-wal` 103.8 GB, on a 457 GB volume otherwise holding only ~48 GB of hot+history
  DB + archive data). The migration errored with `database or disk is full`; the `finally` block's
  own recovery checkpoint then *also* failed for the same reason (`Could not restore
  autocheckpoint/checkpoint: database or disk is full`); `db_maintenance_mode` was still cleared
  and the normal update cycle resumed, immediately cascading into `OSError: [Errno 28] No space
  left on device` on every subsequent DB write and war-file save for the rest of the run.
  A second, independent bug compounded recovery: `monthly_history_migration()` persisted
  `bot_metadata["last_history_migration"] = now` **unconditionally in a bare `try` block outside**
  the main `try/except/finally`, regardless of whether `result` indicated success or `ERROR`. This
  wrongly marked the (failed, partial) migration as done-for-the-month, so
  `QapBot.is_monthly_migration_due()` would have skipped retrying for the rest of August even
  after the underlying disk-full condition was fixed — no automatic recovery path.
- **Fix 1 (root cause)**: `_migrate_table_batch_by_date()` now runs an unqualified
  `PRAGMA wal_checkpoint(PASSIVE)` (covers both `main` and `history`, non-blocking) every
  `_MIGRATION_CHECKPOINT_INTERVAL_BATCHES` (20) batches — bounds WAL growth to ~100K rows' worth of
  changes regardless of total migration size, while still avoiding a checkpoint on every single
  batch commit.
- **Fix 2**: the final `set_bot_metadata("last_history_migration", ...)` call is now gated on
  `result` not starting with `"[HIST-MIGRATE] ERROR"` — a partial/failed run is never marked done,
  so the next due-check (or a manual re-run) retries automatically. The migration was already
  naturally resumable/idempotent (`_migrate_table_batch_by_date` re-selects whatever rows are still
  below the cutoff on each call) — this fix just stops the false "done" marker from suppressing
  that retry.
- **Recovery tooling**: re-added `qapbot/scripts/run_history_migration_now.py` (a prior version of
  this doc's cousin, `CLAN_AND_WAR_CYCLE_ARCHITECTURE.md`, said this had been deleted in favor of
  `run_db_maintenance_now.py` "including" the migration step — that claim was wrong;
  `nightly_db_maintenance()` has never had a migration step, only
  `QapBot.run_nightly_maintenance_routine()` calls both in sequence, gated by `day == 1`. Restored
  the standalone script so an operator can force-run just `monthly_history_migration()` on demand
  (bypassing the day-of-month gate) to resume an interrupted run without waiting for next month.
- **Recovery procedure for an interrupted migration** (this incident or any future one): 1) stop
  the bot, 2) free enough disk space on the volume holding both DB files for a checkpoint to
  succeed (checkpointing writes the pending WAL content back into the main DB files, so the volume
  needs room for both files to grow by roughly the pending WAL size), 3)
  `python qapbot/scripts/run_history_migration_now.py --yes` to resume/finish the migration
  (naturally idempotent — safe to re-run), 4) `python qapbot/scripts/run_db_maintenance_now.py --yes`
  to checkpoint + VACUUM/REINDEX + ANALYZE and reclaim the space freed by the migration's DELETEs,
  5) restart the bot.
- Files: `qapbot/db_manager.py` (`_migrate_table_batch_by_date`, `monthly_history_migration`),
  `qapbot/scripts/run_history_migration_now.py` (new), `qapbot/docs/CLAN_AND_WAR_CYCLE_ARCHITECTURE.md`
  (corrected stale claim). All existing tests pass (see changelog.txt for the count).

### 2026-08-01 (same-day follow-up): Time-Budgeted Chunking — Migration No Longer Has To Finish In One Sitting
- **Why**: the disk-full incident above got fixed, but recovery surfaced a second real problem —
  once WAL growth stopped being the blocker, the sheer backlog size was: a live progress check
  showed ~597 rows/sec sustained even on SSD, and ~22.9M `war_attacks` rows still remained after
  the first 8.3M, projecting to ~10h40m to finish in one uninterrupted run. `monthly_history_migration()`
  sets `db_maintenance_mode=True` for its entire duration, blocking all Discord commands — nobody
  can hold a live production bot offline for half a day.
- **Fix**: `monthly_history_migration()` gained a `time_budget_seconds` parameter (threaded through
  `_migrate_table_batch_by_date()` / `_migrate_cwl_table_by_season()` as a `deadline` checked between
  batches/seasons). When the deadline passes, the run stops cleanly — not an error — and returns a
  `"[HIST-MIGRATE] PARTIAL"` result. Also added `batch_size` and `checkpoint_every_batches` as
  first-class tunables (batch_size already existed; checkpoint_every_batches is new) so a one-off
  recovery run with ample free disk can trade checkpoint overhead for throughput.
  **Correctness note**: the "only mark done on success" fix from the entry above used to check
  `not result.startswith("[HIST-MIGRATE] ERROR")` — that would have wrongly treated a PARTIAL result
  as success (PARTIAL isn't an ERROR string either), silently disabling retry exactly like the
  original bug. Replaced with a real `fully_completed: bool` threaded through from each table's
  `(moved, completed)` return tuple — the `bot_metadata` "done" stamp is now gated on that boolean,
  not on string-sniffing the result message. Covered by
  `tests/unit/test_history_migration_time_budget.py`.
- **Automatic-path safety**: without a bounded automatic path, simply restarting the bot mid-backlog
  would immediately re-trigger an *unbounded* `monthly_history_migration()` via `QapBot.py`'s
  standalone safety-net task (`is_monthly_migration_due()` re-hydrates as "not done" from
  `bot_metadata` on every fresh process, and that check isn't gated by hour==3) — trading a manual
  multi-hour block for an automatic one on every restart. New config
  `CONFIG.history_migration_time_budget_minutes` (env `HISTORY_MIGRATION_TIME_BUDGET_MINUTES`,
  default 90) is now passed to both automatic call sites in `QapBot.py`
  (`run_nightly_maintenance_routine()`'s Step 0.5 and the standalone safety-net task), so any
  automatic trigger is bounded the same way a manual one would be.
- **Implementation gotcha worth remembering**: the deadline-check code initially used `_time.monotonic()`
  (`_time` being `import time as _time` — the *same object identity* as the real `time` module, not a
  copy). A test that monkeypatched `_time.monotonic` to a fake deterministic clock also silently
  hijacked `asyncio`'s own event-loop scheduler (`base_events.py`'s `_run_once()` calls
  `time.monotonic()` every iteration), producing wildly unpredictable extra ticks and a test that
  measured "0 rows moved" instead of the expected partial progress. Fixed by adding a separate
  module-level `_monotonic = _time.monotonic` reference used only by the migration deadline checks —
  rebinding that name in a test affects only this code, not the shared `time` module. General lesson:
  never monkeypatch an attribute on a module that's also relied on by the async runtime itself
  (`time`, `asyncio`, etc.) — bind a local reference to patch instead.
- **Not done automatically across nights yet**: `is_monthly_migration_due()`'s "claim" (setting
  `CACHE.last_history_migration` the moment it returns due) still means the automatic path only
  fires once per bot-process lifetime per month, even with `time_budget_seconds` now bounding each
  individual firing. **Correction, same day (see next entry)**: this note originally said
  `/admin Execute Nightly Maintenance` could be used to "advance to the next chunk" — that turned
  out to be wrong as written: `/admin` called the same `is_monthly_migration_due()` with the same
  claim behavior, so if an automatic trigger had already claimed the month in-process (e.g. right
  after a restart), `/admin` would silently skip the migration step too, with no error. Fixed below.
- Files: `qapbot/db_manager.py`, `qapbot/scripts/run_history_migration_now.py`, `qapbot/config.py`,
  `QapBot.py`, `README.md`, `tests/unit/test_history_migration_time_budget.py` (new, 3 tests).
  1470 tests pass.

### 2026-08-01 (second same-day follow-up): `/admin` Can Now Force Another Migration Chunk On Demand
- **Why**: after the mid-recovery VACUUM (this entry's predecessor), the operator asked whether
  `/admin Execute Nightly Maintenance` could be used instead of restarting the bot to advance the
  migration further while it kept serving Discord normally in between. It's wired to do exactly
  that (`QBdiscordcmds.py` already called `run_nightly_maintenance_routine()` with
  `is_monthly_migration_due()`'s result) — but tracing it through surfaced the bug in the entry
  above: if the automatic safety-net had already claimed the month in-memory earlier in the same
  bot process (very likely, since it fires within the first cycle after any restart while the
  backlog remains), a subsequent `/admin` invocation would find `is_monthly_migration_due()` already
  False and silently run archive-move + VACUUM/REINDEX/ANALYZE only, skipping the migration step
  with no indication anything was skipped.
- **Fix**: `is_monthly_migration_due()` gained an `ignore_in_process_claim: bool = False` parameter.
  When True, it bypasses both the `day == 1` gate and the in-memory claim, re-deriving purely from
  persisted `bot_metadata` — i.e. "is there NOT yet a recorded full completion for this calendar
  month", full stop. `QBdiscordcmds.py`'s `/admin` handler now passes
  `ignore_in_process_claim=True`. At the time this entry was first written, the two automatic paths
  (scheduled nightly window, standalone safety-net in `periodic_main()`) were left unchanged — see
  the next entry for why the scheduled path also needed this.
  Tests: `tests/unit/test_is_monthly_migration_due.py` (new, 4 tests) — pins the bypass behavior and
  confirms the automatic-path default is unchanged.
- **Net result**: an operator can now run `/admin Execute Nightly Maintenance` repeatedly (any day,
  any time) to advance a large migration in bounded (`CONFIG.history_migration_time_budget_minutes`)
  chunks while the bot keeps running and serving other commands in between — no SSH/script/restart
  needed for this path specifically, though the CLI script remains available for larger
  batch-size/checkpoint-interval tuning on a dedicated recovery run.
- Files: `QapBot.py` (`is_monthly_migration_due`), `QBdiscordcmds.py` (admin call site),
  `tests/unit/test_is_monthly_migration_due.py` (new). 1474 tests pass.

### 2026-08-01 (third same-day follow-up): Scheduled Nightly Path Now Auto-Advances the Migration Every Night
- **Why**: the operator asked directly — "if I let the bot run from here, will it do one chunk per
  night automatically until done?" The honest answer at that point was no. The scheduled 03:00 UTC
  nightly task (`_maint_due`, gated on hour==3 + >20h since the last maintenance run — no day
  restriction) already runs every single night regardless of day-of-month. But the migration step
  within it used the DEFAULT `is_monthly_migration_due()` — gated on `day == 1` AND claimed
  in-memory the first time it fires. So on a continuously-running bot process (no restarts), the
  migration would only ever be triggered automatically ONCE per calendar month (whenever first
  claimed — likely immediately after any restart via the standalone safety net), then silently never
  again until day 1 of the following month, regardless of how much backlog remained.
- **Fix**: the scheduled path's migration-due check now uses
  `is_monthly_migration_due(ignore_in_process_claim=True)` — the same bypass added for `/admin` in
  the entry above, reused here. This is safe specifically because the surrounding `if _maint_due`
  block already has its own independent throttle (hour==3 AND >20h since last run), so re-deriving
  "still not done this month" fresh from `bot_metadata` every night naturally caps at once per
  night — no separate spam-guard needed. The standalone safety-net `elif` branch deliberately keeps
  calling the DEFAULT (claiming) `is_monthly_migration_due()` as its own condition, unchanged — that
  branch has no other throttle, and bypassing its claim too would make it re-fire on every ~5-minute
  cycle for as long as backlog remains, reintroducing exactly the "block Discord for hours
  continuously" problem the time-budget feature exists to prevent.
- **Net result**: a bot left running continuously now genuinely advances the migration by one
  bounded (`CONFIG.history_migration_time_budget_minutes`, default 90 min) chunk every night at
  03:00 UTC, with no manual intervention required, until `bot_metadata` shows a full completion for
  the current month — at which point it correctly stops firing on its own. Manual `/admin` triggers
  (previous entry) remain available to advance faster than one chunk/night if desired.
  **Superseded same-day (see two entries below)**: the standalone safety-net branch described above
  as deliberately unchanged was replaced entirely by a much better mechanism — turning "re-firing
  every ~5-minute cycle" from a problem to avoid into the actual design.
- Files: `QapBot.py` (`periodic_main()`'s migration-due call sites). 1474 tests pass (no new tests —
  covered indirectly by the existing `is_monthly_migration_due()` bypass tests; the branching logic
  itself is exercised by the existing `test_periodic_main_control.py` control-flow suite's pattern,
  not duplicated here since `periodic_main()`'s full loop isn't unit-tested end-to-end — see that
  file's module docstring).

### 2026-08-01 (operational note, no code change): Stale Pre-Fix `bot_metadata` Stamp Blocked Every Automatic Path
- After deploying the `ignore_in_process_claim` fix (two entries above) and restarting the bot
  twice, the operator found the migration still wasn't auto-triggering via any path — scheduled
  window, safety-net, or (had it been tried) `/admin`. Root cause: `bot_metadata["last_history_migration"]`
  still held the value written by the *original pre-fix* failed run from earlier that night
  (`2026-08-01T05:25:59Z`, written by the old unconditional-stamp code before any of today's fixes
  existed). Every variant of `is_monthly_migration_due()` compares this stamp's year/month against
  "now" — both August 2026 — so every path correctly-per-its-own-logic concluded "already done this
  month" even though the real backlog was nowhere near finished.
  **Lesson**: fixing the code that produces a bad value does not retroactively fix a bad value that
  code already wrote before the fix was deployed. This exact stamp had been read and flagged as
  evidence of the original bug much earlier the same night — the follow-up step of clearing it once
  the fix landed was missed until the operator noticed the symptom (automatic paths silently doing
  nothing) and asked directly.
- **Manual fix applied**: `DELETE FROM bot_metadata WHERE key = 'last_history_migration';` run
  directly against the live DB via `sqlite3` CLI (safe with the bot running — single statement,
  well within `busy_timeout`). No code change — this is a one-time data correction, documented here
  so a future incident with a similar shape (persisted "done" state written before a fix, not
  cleared after) is recognized faster.

### 2026-08-01 (fourth same-day follow-up): Opportunistic Per-Cycle Migration Chunking
- **Why**: operator proposal — the update cycle already sleeps for the unused remainder of
  `SLEEP_INTERVAL` after each cycle (~4-4.5 min idle out of a 5-min default interval, cycles
  typically taking under a minute). Rather than only advancing the migration in one large chunk a
  night (or relying on a rare safety net), spend that already-idle time on migration too, using the
  exact same time-budget/chunking machinery already built for the other paths.
- **Fix**: the old standalone safety-net `elif` branch (default `is_monthly_migration_due()`, fires
  rarely, only once per process per month) was replaced with an opportunistic chunk branch: fires on
  *every* cycle where `_migration_due` (the `ignore_in_process_claim=True` check computed once per
  cycle, shared with the `if _maint_due` branch above it) is true, bounded to
  `CONFIG.history_migration_cycle_chunk_minutes` (default 4 min) via the same
  `asyncio.create_task()` + `QBcore.db_maintenance_idle_event` clear/set pattern the nightly-window
  branch already uses — reuses 100% existing infrastructure, no new synchronization primitives.
  Since the sleep-wait already runs concurrently with whatever background task was launched, and the
  chunk is deliberately sized to usually finish within the idle window, the existing
  `db_maintenance_idle_event` gate at the end of the sleep-wait mostly finds it already set (little
  to no actual blocking of the *next* cycle) — restoring the validity of that gate's original "normally
  already set" comment, which the once-a-night 90-minute chunks had been violating.
  This branch is a strict superset of the safety net it replaced (fires on any day, any time, not
  just as a rare once-a-month fallback), so nothing was lost by removing it.
- **Trade-off, stated plainly**: while a real backlog remains, this means Discord commands
  (`db_maintenance_mode`) are blocked for roughly `chunk_minutes` out of every `SLEEP_INTERVAL`
  cycle — i.e. *mostly* blocked with brief ~cycle-duration windows of availability, rather than
  *mostly* available with one longer nightly block. In exchange, total backlog completion time drops
  roughly in proportion to how much of the idle window gets used (e.g. ~4 of ~5 idle minutes per
  cycle vs. ~90 of ~1440 minutes per night is roughly an order of magnitude more throughput).
  Self-limiting: once `is_monthly_migration_due()` reports done, the branch's condition is false and
  it costs nothing — for routine future months (one month's worth of newly-aged-out data, not a
  multi-year backlog), this will likely complete within the first cycle or two, making the
  "mostly blocked" state last minutes, not days. Set `HISTORY_MIGRATION_CYCLE_CHUNK_MINUTES=0` to
  disable and fall back to the once-a-night-only cadence if this trade-off isn't wanted.
- Files: `QapBot.py` (`periodic_main()`), `qapbot/config.py` (new
  `history_migration_cycle_chunk_minutes` + env var), `README.md`. 1474 tests pass (same rationale
  as the entry above for no new dedicated test — `periodic_main()`'s loop isn't unit-tested
  end-to-end).

### 2026-08-01 (fifth same-day follow-up): `/admin` Migration Budget Decoupled From the Scheduled Nightly One
- **Why**: operator noticed `run_nightly_maintenance_routine()` is shared verbatim between the
  scheduled 03:00 UTC task and the `/admin` "Execute Nightly Maintenance" command — both were using
  the same `CONFIG.history_migration_time_budget_minutes` (90 min) for the migration step. That's
  fine for the fire-and-forget scheduled task, but wrong for `/admin`: it's an interactive,
  user-awaited command whose actual purpose is the maintenance steps (WAL checkpoint / VACUUM /
  REINDEX / ANALYZE), not migration progress — the opportunistic per-cycle chunk (entry above)
  already carries the bulk of that automatically. A 90-minute wait on an interactive command is also
  a functional risk: `QBdiscordcmds.py`'s own comment already notes the Discord interaction token
  expires after ~15 minutes, so a long migration chunk risks the reply silently failing to send.
- **Fix**: `run_nightly_maintenance_routine()` gained an optional `migration_time_budget_seconds`
  parameter (defaults to the existing scheduled-nightly value when not given, so the scheduled
  caller in `periodic_main()` is unaffected). The `/admin` handler now passes a new, separate,
  much shorter budget: `CONFIG.history_migration_admin_budget_minutes` (env
  `HISTORY_MIGRATION_ADMIN_BUDGET_MINUTES`, default 1 min).
- **Net result**: `/admin Execute Nightly Maintenance` now stays fast and focused on its actual
  job — a quick 1-minute migration nibble (if due) followed by the real maintenance steps — instead
  of potentially hanging for up to 90 minutes on migration before the interaction can even be
  replied to. The scheduled nightly task and the opportunistic per-cycle chunk are both unaffected.
- Files: `QapBot.py` (`run_nightly_maintenance_routine`), `QBdiscordcmds.py` (admin call site),
  `qapbot/config.py` (new `history_migration_admin_budget_minutes` + env var), `README.md`.
  1474 tests pass.

### 2026-08-01 (sixth same-day follow-up): Fast Bulk Migration Mode — Drop Indexes, Bulk Move, Rebuild
- **Why**: with WAL growth and chunking both fixed, the operator asked why the batched path still
  only moved ~900-1000 rows/sec on SSD when the underlying backlog was only ~10 GB — data that size
  should copy in minutes on SSD, not the ~5-7 hours the current rate implied for the remaining ~19M
  rows. Root cause: `war_attacks` carries 5 secondary indexes on EACH schema (`idx_wa_player_tag`,
  `idx_wa_war_clan`, `idx_wa_clan_date`, `idx_wa_zero_attacks`, `idx_wa_player_tag_date`) plus the
  UNIQUE(war_id, player_tag, attack_order) constraint index. Every row moved was paying 6 B-tree
  updates inserting into `history` and 6 more deleting from `main` — 12 index updates per row is
  what actually limited throughput, not disk I/O; raw sequential I/O for ~10 GB on SSD would indeed
  take minutes, not hours.
- **Fix**: new `WarHistoryDB.fast_bulk_history_migration(chunk_size=1_000_000)` — temporarily drops
  the 5 secondary indexes on BOTH `main.war_attacks` and `history.war_attacks` (never the UNIQUE
  constraint — `INSERT OR IGNORE`'s idempotency depends on it), migrates `war_attacks` in large
  single-commit chunks (default 1M rows, each still individually checkpointed) instead of 5000-row
  batches, then rebuilds every dropped index in one efficient sorted `CREATE INDEX` pass each —
  dramatically cheaper than millions of incremental per-row updates. `war_summary`/CWL tables keep
  using the existing batched methods (much smaller volume, not the bottleneck).
  New helper `_bulk_move_chunk()` finds each chunk's upper `id` bound via `OFFSET`-based keyset
  pagination (`SELECT id ... ORDER BY id LIMIT 1 OFFSET chunk_size-1`) rather than either (a) an
  `IN (...)` list of a million ids — would exceed SQLite's default bound-parameter limit
  (~32,766) — or (b) `LIMIT`/`ORDER BY` on `DELETE`, a non-default SQLite compile-time option not
  guaranteed available.
- **Safety**: this mode strictly REQUIRES the bot to be stopped — dropping `main.war_attacks`'
  indexes, even briefly, would badly degrade any concurrent live query against it (unlike the
  routine batched path, which never touches `main`'s indexes for exactly this reason). Dropped
  indexes are rebuilt in a `finally` block regardless of success or failure, so an interrupted or
  errored run never leaves a restarted bot with degraded query performance. Each chunk remains its
  own committed transaction (same resumability as the batched path). No `time_budget_seconds` —
  this mode is meant to run once, to completion, with chunk size (not a time budget) as the safety
  valve bounding uncheckpointed WAL growth.
- **CLI**: `run_history_migration_now.py --fast [--fast-chunk-size N]` — ignores
  `--batch-size`/`--checkpoint-every-batches`/`--time-budget-minutes` in this mode. Confirmation
  prompt (without `--yes`) is fast-mode-specific, emphasizing the bot-stopped requirement.
- Files: `qapbot/db_manager.py` (`fast_bulk_history_migration`, `_bulk_move_chunk`,
  `_drop_war_attacks_secondary_indexes`, `_rebuild_war_attacks_secondary_indexes`),
  `qapbot/scripts/run_history_migration_now.py` (`--fast`/`--fast-chunk-size`),
  `tests/unit/test_fast_bulk_history_migration.py` (new, 4 tests — correctness across chunk
  boundaries, indexes dropped mid-run and rebuilt after, indexes rebuilt even on a simulated
  mid-run error, idempotent re-run). 1478 tests pass.

### 2026-08-02: `fast_bulk_history_migration()` Silent Death Root-Caused — Small tmpfs `/tmp`, Not OOM
- **Why**: the `--fast` run from the entries above died silently partway through recovery (`ps`
  showed the process alive, then gone, with the 5 secondary indexes on both schemas never
  rebuilt). OOM was the leading suspect (the NAS became briefly unresponsive around the same
  time) but was cleanly ruled out: `free -h` showed only 4% memory used, and `dmesg` had no
  OOM-killer signature at all (a real OOM kill produces a very distinctive multi-line block that
  would dominate a `tail -50`, not be absent).
  The actual cause surfaced while manually rebuilding the missing indexes via the `sqlite3` CLI as
  a stopgap: the second `CREATE INDEX` failed outright with `Error: stepping, database or disk is
  full (13)` — even though the data volume itself had hundreds of GB free. Root cause: `CREATE
  INDEX` over a table this large needs real external-sort temp space, and SQLite's default temp
  location (typically `/tmp`) is unrelated to the data volume — on this NAS, `/tmp` is a tmpfs
  (RAM-backed) capped at 4.8 GB. `fast_bulk_history_migration()` never set `SQLITE_TMPDIR` /
  `PRAGMA temp_store_directory`, so it was silently subject to the same tiny limit — almost
  certainly what killed the original run too (SQLITE_FULL raised inside `_rebuild_war_attacks_secondary_indexes()`'s
  per-index `try/except`... except that only explains a logged-and-skipped single index, not the
  total silence observed, so the exact mechanism of the *first* death is still not 100% certain —
  but the tmpfs constraint is confirmed real and reproducible, and is fixed regardless).
  **This was already a known class of problem in this codebase**: `nightly_db_maintenance()`
  already works around it for VACUUM, with a comment documenting a *different* historical incident
  with the same "mystery SIGKILL, no log entry" symptom (an overly large `cache_size` triggering a
  real OOM there) — `fast_bulk_history_migration()` just didn't replicate that existing fix when it
  was added.
- **Fix**: `fast_bulk_history_migration()` now sets `PRAGMA temp_store=FILE` +
  `PRAGMA temp_store_directory=<the DB's own directory>` before touching any index — same pattern,
  same target directory (the data volume itself, effectively unlimited headroom by comparison to a
  4.8 GB tmpfs) as `nightly_db_maintenance()` already uses for VACUUM.
- **Operational workaround** (for anyone hitting `SQLITE_FULL` on a manual `CREATE INDEX`/`VACUUM`
  via the `sqlite3` CLI directly, independent of this code fix): `export SQLITE_TMPDIR=<dir on the
  data volume>` before running `sqlite3` — read directly by the CLI, takes priority over the OS
  default.
- Files: `qapbot/db_manager.py` (`fast_bulk_history_migration`).

### 2026-08-09: Manual Maintenance Run Silently Suppressed That Night's Automatic Run
- **Symptom**: nightly maintenance didn't run at all overnight — `qapbot_PROD.log` had zero
  `[NIGHTLY-MAINTENANCE]` lines for the whole day (not a failed/crashed run — the task was never
  spawned in the first place).
- **Root cause**: `periodic_main()`'s scheduled trigger (`_maint_due` in `QapBot.py`) gated firing on
  `hour == 3 and (now - CACHE.last_db_maintenance) > 20h`. `last_db_maintenance` is shared with
  manual `/admin` Optimize DB / Execute Nightly Maintenance runs — `nightly_db_maintenance()`
  persists it to `bot_metadata` unconditionally, regardless of caller. On 2026-08-08, a manual run
  (testing that day's maintenance-code changes) completed at 17:16 UTC. A later same-evening
  redeploy restarted the bot process; on restart, `CACHE.last_db_maintenance` is `None` in memory,
  so the startup hydration block re-loaded that fresh 17:16 UTC value from `bot_metadata`. At the
  03:00 UTC check that night, only ~9h46m had elapsed since 17:16 UTC — under the 20h threshold —
  so `_maint_due` was `False` and the automatic run silently never fired. Confirmed by directly
  reading `data/qapbot.db`'s `bot_metadata` table (`last_db_maintenance` = `2026-08-08T17:16:11Z`)
  and cross-referencing the commit timeline (a maintenance-code commit at 18:42 CEST, final same-day
  redeploy at 19:46 CEST).
  This is a latent design gap, not new breakage: any manual/test maintenance run after roughly
  07:00 UTC, followed by a same-day restart, can suppress that night's scheduled run for up to 20h.
  It's self-healing after 20h pass with no further manual runs near the window, which is why it
  hadn't been noticed before — most manual runs during dev aren't followed by a restart within the
  same night.
- **Fix**: `_maint_due`'s guard now compares UTC calendar dates
  (`CACHE.last_db_maintenance.date() != _now_utc.date()`) instead of elapsed hours — "has
  maintenance of any origin already run today?" This still prevents the original double-fire case
  (a restart within the same 03:00 window recomputes the same date, correctly skips), but is no
  longer sensitive to what time of day an unrelated manual run happened.
- Files: `QapBot.py` (`periodic_main()`'s `_maint_due` block). 1519 tests pass.

### 2026-08-17: `player_name_index` Scan-Cost Fix + SQLite/FTS5 Search Alternative (Complete ✅)
- **Context**: `CWL_PROD_PERFORMANCE_FIX_PLAN.md` P1 Step 9 + P2 Step 11, following the
  2026-08-16 PROD meltdown (see the CWL Guests-search incident referenced throughout that plan
  doc — a different code path, but the same `player_name_index` table was one of the two
  million-plus-entry structures the incident's uncapped scans walked).
- **Step 9**: `CACHE.player_name_index` values changed from a bare `player_name: str` to
  `(name, name_lower)` tuples, built once at load/write time (`_player_name_tuple()`) instead of
  every reader calling `.lower()` on every one of the ~6.6M entries per search.
  `CACHE.search_player_names()` now also bounds collection to 200 matches before sorting/capping
  instead of scanning to completion first. Found and fixed a matching cost in `/whois`'s OWN
  separate inline scan (`QBdiscordcmds.py` — doesn't call `search_player_names()` at all, needs
  the full match set for its guild-membership reorder) — extracted into
  `_search_player_name_index_sync()`, `asyncio.to_thread()`-wrapped (was running unwrapped on
  the event loop).
- **Step 11**: added a SQLite-backed alternative — `player_name_search(player_tag PK, name,
  name_lower)` (plain table, tag-prefix search) + `player_name_fts` (FTS5, `tokenize='trigram'`,
  name-substring search), gated behind `CONFIG.cwl_use_fts_player_search` (default `False`).
  Feasibility (FTS5 + trigram tokenizer support) confirmed live via SSH on both DEV (SQLite
  3.50.4) and PROD (SQLite 3.45.2) before implementing — both fully support it. Kept in sync by
  the same two writers as `player_name_index` (`_upsert_player_name_index_in_conn` /
  `update_player_name_index_sync`), re-reading the just-upserted `player_name_index` row (not
  trusting the write batch's own value) so a "not newer, skip" outcome there can never leave the
  new tables holding a stale name. One-time idempotent backfill on every startup, guarded by a
  row-count comparison. `CACHE.player_name_index` stays fully loaded/dual-written regardless of
  the flag — explicit safety-net retention for at least one release; dropping it from RAM (the
  actual ~1GB RSS payoff) is a later, separate decision once DEV+PROD burn-in confirms parity.
- **Corrections made to the plan's own draft during implementation** (verified empirically,
  2026-08-17): the plan's `fts5(player_tag UNPREFIXED, name)` doesn't parse — FTS5 has no
  `UNPREFIXED` column option; the real keyword is `UNINDEXED`. FTS5 virtual tables don't support
  `INSERT ... ON CONFLICT DO UPDATE` ("UPSERT not implemented for virtual table") — used
  DELETE-then-INSERT per row instead (`UPDATE ... WHERE <unindexed column> = ?` also works
  against FTS5 directly, confirmed, but delete+insert is simpler when a row may not exist yet).
  Trigram tokenization needs >=3 characters to form even one trigram, so a shorter query
  structurally cannot match anything — `search_player_names_sync()` returns `[]` immediately for
  those. An unquoted needle is parsed as FTS5 query syntax, not literal text (a raw hyphen raises
  "no such column", an unescaped `"` raises "unterminated string") — every query is wrapped in
  FTS5's own literal-string quoting (`"..."`, embedded `"` doubled) before use.
- Files: `qapbot/db_manager.py` (schema, backfill, 2 writers extended, 2 new readers:
  `search_player_names_sync`/`search_player_tags_by_prefix_sync`), `qapbot/cache_manager.py`
  (tuple shape, `set_player_name()`, rollout-flag delegation), `qapbot/coc_cache.py` (writer
  switched to `set_player_name()`), `qapbot/web_bridge.py` (guest-search tag-prefix mode
  delegation), `QBdiscordcmds.py` (`/whois` threading fix), `qapbot/config.py`
  (`cwl_use_fts_player_search`). 26 new tests (20 in a new `tests/unit/
  test_player_name_search_fts.py`, plus the Step 9 tests and one guest-search delegation test).
  2046 tests pass.

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

### `VACUUM INTO` swap: Windows vs Linux file-handle semantics
`nightly_db_maintenance()`'s VACUUM path does `VACUUM INTO 'x.vacuumed'` then swaps it into place
with `os.remove(-wal/-shm)` + `os.replace()`. Closing the sqlite3 connection AFTER this swap works
on Linux (unlink/rename on an open file just detaches the directory entry) but fails on Windows
with WinError 5/32 (can't delete/rename a file with an open handle). Fix: close the connection
FIRST, then remove/replace, then reopen. Bit DEV testing on Windows; PROD (Linux) was unaffected,
but the ordering is now correct on both.

### Random pre-existing index corruption — always run a quick_check after big DB ops
After a migration+VACUUM on the DEV DB (2026-07-12), `PRAGMA integrity_check` found
"wrong # of entries in index sqlite_autoindex_clans_1" — not caused by the migration itself
(confirmed: no actual duplicate `clan_tag` rows; `COUNT(*)` was correct, only
`COUNT(DISTINCT clan_tag)` was inflated by stale index entries). Fixed instantly with
`REINDEX clans`. Run `PRAGMA integrity_check`/`quick_check` after any big VACUUM/migration op on a
DB whose provenance includes raw file copies (vs. proper backup/restore tooling) — cheap
insurance, and `REINDEX` is a safe, non-destructive fix for this class of issue.

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
