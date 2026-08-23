# QapBot Code Structure (2026-03-01, Database-Only Architecture Edition)

## Architecture Overview

QapBot is a modular Python Discord bot for Clash of Clans, featuring:
- Main orchestration and periodic update loop
- Discord bot command handling with modular UI components
- Business logic for leaderboard generation and war prediction
- Robust cache and file management for persistent data
- Statistics calculation and formatting for Discord and terminal output
- **Account protection system** with three-tier permission hierarchy
- **CWL Clan-Config Discord Activity**: a Cloudflare Pages/Workers web app (`activity/`),
  embedded in Discord's client via `@discord/embedded-app-sdk`, backed by an in-process
  `aiohttp.web` bridge (`qapbot/web_bridge.py`) that reuses the bot's own `CACHE`/`db_manager` —
  full design in `CWL_CLAN_CONFIG_ACTIVITY_PLAN.md`

## Account Protection Architecture

### Three-Tier Permission System

**CRITICAL**: Account protection prevents unauthorized player account hijacking and maintains data integrity. Each tier trades increasing trust (Discord server role or bot-admin config) for increasing power to re-link a player account already claimed by someone else.

| Tier | Permission Level | Can Do | Confirmation Flow | Key Function / View |
|---|---|---|---|---|
| **1. Regular User** | Default for all Discord users | Link UNLINKED player accounts via registration message flow | None — direct link. Blocked with an error if the player is already linked (verified or unverified) to anyone else | registration message flow |
| **2. Guild Administrator** | Discord users with server administrator permissions | Manage clan subscriptions, access clan management interface, link players to Discord users in their guild, **re-link UNVERIFIED accounts** (cannot re-link VERIFIED accounts), **unlink any linked player from clan management — including VERIFIED accounts** | Linking: warning shows player info + current owner's display name/Discord ID → Confirm/Cancel buttons. On Confirm: player removed from previous user + DM sent to previous owner. On Cancel: no changes made. Unlinking: Confirm/Cancel dialog defaulting to Cancel; if the account is API-verified the confirmation text switches to a separate, louder warning spelling out that verified status is lost. On Confirm: player unlinked (moved to UNASSIGNED pool, no DM sent — same as self-service unlink). On Cancel: no changes made | Guild-admin linking UI, `ClanManagementUnlinkPlayerView` / `ClanManagementUnlinkPlayerConfirmView` |
| **3. Bot Server Admin** | Configured via `CONFIG.server_admin` in qapbot/config.py (specific Discord username) | All Tier 1/2 permissions, bot-wide admin commands, **re-link VERIFIED accounts** (admin override) | 2-step: "Admin Override Required" warning with verified-owner details → explicit "Admin Override Confirm" button. On Confirm: verified player removed from previous user, DM sent, action logged (audit trail). On Cancel: no changes made | `get_verified_player_owner()`, `ClanManagementAdminOverrideView` / `AdminOverrideConfirmView` |

#### Rule 4: API Token Override (Cryptographic Proof)
Cuts across all three tiers above: any user who can supply a valid CoC API token for an already-linked player (verified or not) can override the protection rules with cryptographic proof of rightful ownership — evidence treated as stronger than any admin permission. This is the intended path for legitimate account recovery when a player switches Discord accounts.
- **Flow**: user attempts to link an already-linked player → `ApiTokenOwnershipModal` explains the ownership conflict and prompts for the token → validated via `validate_api_token_for_player()`.
- **On Success**: previous link removed, player linked to new user (with verification), DM sent to previous owner.
- **On Failure**: linking rejected with error message.

### Account State Transitions

```
State Transitions:
Unlinked → Linked (Unverified): Regular user, guild admin, bot admin
Unlinked → Linked (Verified): Any user with API token
Linked (Unverified) → Linked (Unverified, User B): Guild admin (with confirmation)
Linked (Verified) → Linked (Any, User B): Bot admin only (with 2-step confirmation) OR Any user with API token
Linked (Any) → Unlinked: User removes from their own account, or guild admin removes via clan management (Confirm/Cancel, louder warning if VERIFIED)
```

### Key Functions for Account Protection

- **get_verified_player_owner()**: Check if player is verified by another user
- **get_any_player_owner()**: Check if player is linked to any user (verified or unverified)
- **validate_api_token_for_player()**: Validate CoC API token for ownership proof
- **_link_player_to_user()**: Core linking with `admin_override` and `api_token_override` parameters
- **complete_account_linking_flow()**: Unified flow with security checks and API token handling
- **ClanManagementAdminOverrideView**: Confirmation dialog for verified re-links
- **AdminOverrideConfirmView**: Generic confirmation for admin operations
- **ApiTokenOwnershipModal**: Modal for API token entry when ownership conflict detected
- **unlink_player()**: Core unlink (moves player to UNASSIGNED pool, write-through persists both accounts) — shared by self-service unlink and admin unlink
- **ClanManagementUnlinkPlayerView**: Admin's linked-player picker (paginated), opened from the clan management "Unlink Player" button
- **ClanManagementUnlinkPlayerConfirmView**: Confirm/Cancel dialog for admin unlink; renders a separate, louder warning message when the selected account is API-verified

### Security Implementation Checklist

When implementing player linking features:
- ✅ Check `get_verified_player_owner(player_tag, requesting_user_id)` for verified conflicts
- ✅ Check `get_any_player_owner(player_tag, requesting_user_id)` for any ownership conflicts
- ✅ Implement appropriate confirmation dialogs based on user type and account state
- ✅ Send DM notifications to previous owners when players are removed
- ✅ Log all admin override actions with full context
- ✅ Use `admin_override=True` flag only for bot admin operations
- ✅ Never silently remove verified accounts without explicit admin confirmation
- ✅ Implement API token validation flow for rightful ownership proof

---

## Cache Management (Single Source of Truth)

**CRITICAL**: CACHE object is the ONLY source of runtime data. Never create shadow data structures.

### Architecture: Write-Through Persistence
All cache mutations are immediately persisted to SQLite database via write-through methods.
No JSON files are used for primary data storage (only temp war files and translations remain as JSON).

### CACHE Object Properties
```python
# Core data
CACHE.clan_name_cache: Dict[str, Dict[str, Any]]  # clan_tag -> {name, has_active_subscriptions,
                                                   #   track_war_updates, last_war_update, warlog_is_public,
                                                   #   war_league, is_deleted, ...}
# has_active_subscriptions: True when clan is "tracked" — has any channel subscription,
#   OR appears in guild member_clans, OR belongs to a subscribed/member family.
# track_war_updates: three-tier model:
#   Tier 1 (actively tracked/subscribed)             → True  (full polling every cycle)
#   Tier 2 (passively tracked, Master II+)           → True  (22h threshold polling)
#   Tier 3 (passively tracked, below Master II)      → False (no war polling; stored for war_league lookup)
# is_deleted: True when CoC API confirms the clan no longer exists (NotFound 404).
#   Skipped in the Phase-1 polling loop. Automatically cleared when any get_clan() call succeeds.
CACHE.user_accounts: Dict[str, Dict[str, Any]]  # user_id -> {display_name, notification_settings, players, user_language, ...}
CACHE.notification_state: Dict[str, Dict[str, Any]]  # war_id -> {...}

# Message tracking
CACHE.leaderboard_messages: Dict[str, Dict[str, Any]]  # message_key -> {message_ids, content_hash, ...}
CACHE.subscriptions: Dict[str, Dict[str, List[Dict[str, Any]]]]  # guild_id -> channel_id -> subscription_list

# War data
CACHE.temp_war_stats: Dict[str, Dict[str, Any]]  # clan_tag -> player_tag -> stats
CACHE.temp_war_metadata: Dict[str, Dict[str, str]]  # clan_tag -> {state, start_time, end_time, filepath}
# filepath is the absolute path to the temp JSON, kept current by save_war_object().
# Used by war_notifications._get_active_wars() to open files directly without glob.glob().
CACHE.clan_history: Dict[str, List[Any]]  # clan_tag -> list of war records (loaded from DB on demand)
CACHE.history_cache: Dict[Tuple[str, Optional[int], Optional[int], Optional[str]], List[Dict[str, Any]]]  # (clan_tag, month, year, cwl_season) -> filtered history cache

# CWL in-memory caches (survive across cycles within a session)
# Evicted by evict_stale_cwl_caches() once per cycle (called after Phase 3).
CACHE._league_war_cache: Dict[str, Tuple[Any, float, str]]   # war_tag -> (ClanWar, fetch_ts, state)
                                                              # TTL: 2h for all states
CACHE._league_group_cache: Dict[str, Tuple[Any, float, int]] # clan_tag -> (LeagueGroup, fetch_ts, round_count)
                                                              # TTL: 1h; invalidated when round_count increases
# CWL round tracking: cwl_league_groups and cwl_league_rounds are DB-only tables.
# No in-memory cache — looked up via get_cwl_round_for_war_tag_sync() at finalization time.
# Populated automatically by _process_league_group_response() on every get_league_group() call.

# Clan families
CACHE.clan_families: Dict[str, Dict[str, Any]]  # family_tag -> {name, clans, owned_by_guild, ...}

# Server configuration
CACHE.server_config: Dict[str, Dict[str, Any]]  # guild_id -> {language, roles, welcome/notification config, ...}

# Database access
CACHE.db_manager: WarHistoryDB  # All database operations via this reference
```

### Write-Through Pattern
- All cache mutations persist immediately to SQLite database
- `save_all()` has been removed entirely — there is no batch-save method; all persistence is write-through
- `load_all()` loads all data from database on startup
- Per-entity persist methods: `persist_user()`, `persist_clan()`, `persist_clan_family()`, etc.
- Error handling: try/except + log + re-raise on all write-through methods
- User-facing DB writes (`save_user`, `save_guild_config`, `save_subscriptions_for_channel`,
  `save_clan_family`) are wrapped with `_retry_on_locked()` — exponential backoff on
  "database is locked" errors so Discord interactions survive concurrent finalization bursts
  (see ../qapbot/docs/DATABASE_ARCHITECTURE.md § Retry-on-Locked)

---

## Discord.py Patterns

### Modal Pattern (REQUIRED)
```python
# Title MUST be in class definition
class MyModal(discord.ui.Modal, title="My Modal Title"):
    # TextInputs MUST be class attributes
    my_input = discord.ui.TextInput(
        label="Input Label",
        placeholder="Placeholder"
    )
    
    def __init__(self, guild_id=None):
        # NO title parameter to super().__init__()
        super().__init__()
        # Translate placeholders AFTER super init
        self.my_input.placeholder = t('key', guild_id=guild_id)
    
    async def on_submit(self, interaction):
        # ALWAYS defer first
        await interaction.response.defer(ephemeral=True)
        # ... process ...
        await interaction.followup.send("Done", ephemeral=True)
```

### Select Menus Inside Modals (discord.py 2.6+)
Plain `discord.ui.Select`/etc. can't be added to a `Modal` directly — wrap it in
`discord.ui.Label` (Components V2), still as a class attribute per Cardinal Rule 10:
```python
class MyModal(discord.ui.Modal, title="..."):
    my_select = discord.ui.Label(
        text="Choice",
        component=discord.ui.Select(
            options=[discord.SelectOption(label=v, value=v, default=(v == "A")) for v in ("A", "B")],
        ),
    )

    def _set_options(self, current: str) -> None:
        # Rebuild per-instance (translated labels, correct `default=`) same as any other
        # per-request Select — see "Select Dropdown Persistence" below.
        self.my_select.component.options = [...]

    async def on_submit(self, interaction):
        value = self.my_select.component.values[0]  # NOT .value — that's TextInput's attr
```
`Label.text` is the visible field label (max 45 chars); the wrapped component has no
`.callback` dispatch inside a modal — its submitted value(s) just land on the component
itself (`.value` for `TextInput`, `.values` for any `BaseSelect`), read after `on_submit`
fires. Real-world example: `qapbot/ui_tracker.py`'s `TrackerItemModal.environment_select`.

### Radio Groups Inside Modals (discord.py 2.7+)
When the choice must render as actual radio buttons rather than a dropdown, use
`discord.ui.RadioGroup` (still Label-wrapped, same idiom as Select above) instead of
`discord.ui.Select`:
```python
class MyModal(discord.ui.Modal, title="..."):
    my_radio = discord.ui.Label(
        text="Choice",
        component=discord.ui.RadioGroup(
            options=[discord.RadioGroupOption(label=v, value=v, default=(v == "A")) for v in ("A", "B")],
        ),
    )

    async def on_submit(self, interaction):
        value = self.my_radio.component.value  # singular .value, NOT .values — RadioGroup is
        # single-select by construction, unlike BaseSelect's .values list.
```
2-10 options (`RadioGroupOption`, not `SelectOption`). Rebuild `.options` per-instance the same
way as a Select (translated labels, correct `default=`) — see "Select Dropdown Persistence"
below; the same persistence rule applies. Real-world example: `qapbot/ui_tracker.py`'s
`TrackerItemModal.environment_select`/`priority_select` (converted from Select to RadioGroup,
tracker item request 2026-08-22, per the project owner's explicit ask for radio buttons over
dropdowns in that modal).

### Select Dropdown Persistence
```python
# Mark selected option with default=True
options = [
    discord.SelectOption(
        label=name,
        value=id,
        default=(id == selected_value)  # This makes it persist
    )
    for name, id in items
]

# After selection, rebuild dropdown
self.remove_item(self.my_select)
self._build_select(selected_value=user_selection)
await interaction.response.edit_message(view=self)
```

### Interaction Response Lifecycle
```python
# Modal submission pattern
async def on_submit(self, interaction):
    await interaction.response.defer(ephemeral=True)  # ALWAYS first
    # ... do work ...
    await interaction.followup.send("Result", ephemeral=True)  # NOT response.send_message

# Button/Select pattern
async def on_click(self, interaction):
    if interaction.response.is_done():
        await interaction.followup.send("Message")
    else:
        await interaction.response.send_message("Message")

# Slow select/button pattern (work takes >3 s)
async def on_select(self, interaction):
    await interaction.response.defer()           # extend window to 15 min
    result = await asyncio.to_thread(heavy_work)
    await interaction.edit_original_response(...)  # NOT response.edit_message
```

### Ephemeral View Delete-on-Timeout Pattern
All ephemeral views that the bot sends (and whose messages it tracks) MUST implement
delete-on-timeout so stale Discord messages are cleaned up automatically.

```python
class MyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.message: Optional[discord.Message] = None  # set after send

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.delete()
            except Exception:
                pass

# At the call site:
view = MyView()
msg = await interaction.followup.send("...", view=view, ephemeral=True)
view.message = msg
# OR for edit_message paths: view.message = interaction.message
```

Views updated with this pattern: WarNotificationPromptView, UnifiedNotificationView,
LanguageSelectionView, BuddyPlayerSelectView, RemoveBuddyView, AccountActionView,
LanguageSelectView. Multi-state views propagate `view.message` through
all rebuild/button-handler paths so the reference is never lost.

---

## Key Components

- **CACHE**: Centralized runtime and persistent data (clan names, war info, message IDs, hashes, temp/history war stats)
- **Atomic File Writes & Backups**: Ensures data integrity for all cache and history files
- **Unified Message ID Tracking**: Prevents spam and enables message updates
- **Hash-Based Content Optimization**: Reduces Discord API calls by comparing content hashes
- **Modular UI Components**: Separated Discord views and modals for better maintainability

## Main Files & Responsibilities

🟦 QapBot.py
- Main orchestration, startup/shutdown, periodic update loop
- Logging setup and error handling
- War data update and leaderboard posting
- Cleanup and cache persistence
- `_signal_shutdown_handler`: uses `loop.call_soon_threadsafe(event.set)` so SIGINT wakes the
  asyncio selector immediately (bare `event.set()` from a signal handler does NOT write to the
  self-pipe — selector stays asleep up to 300 s until the next I/O event)
- `on_member_join`: welcome message gated on `welcome_message_enabled`; for clan_link mode,
  resolves `welcome_family_tags` (all clans in each selected family) + `welcome_clan_tags`
  (individually selected clans), de-duplicates, and emits one clan-link line per clan
  (`welcome_message.clan_info` for exactly one clan, `clan_info_plural` for 2+). Zero
  selections is valid — the clan-link line is simply omitted, no fallback text shown.

🟩 QBhelperfunctions.py
- Leaderboard generation and formatting
- War info update and prediction
- Discord posting logic (unified, hash-optimized)
- Argument parsing and normalization
- `_mark_clan_deleted(clan_tag)`: async helper called when CoC API returns `NotFound` for a clan.
  Sets `is_deleted=True` in `clan_name_cache` and persists to DB. Logs one `[CLAN-DELETED]` warning.
  The clan is then skipped by the Phase-1 loop until cleared automatically (see coc_cache.py's
  `_update_clan_metadata` auto-restore below).
- `predict_war_between_clans()` — pre-war Monte Carlo prediction between any two clans; full
  steps in Function Tree § QBhelperfunctions.py.
- `generate_cwl_group_analysis_embeds()` — `/analyse leaguegroup` engine; full steps in
  Function Tree § QBhelperfunctions.py.

🟧 qapbot/coc_cache.py (~710 lines)
- CoCClanCache class with stale-while-revalidate strategy (soft_ttl=280s, stale 280-600s, hard_ttl=600s)
- Background refresh for stale entries, blocking fetch for expired/missing
- Updates clan_name_cache, warlog status, and player TH/clan info on every fetch
- **Clan deletion auto-restore**: `_update_clan_metadata()` clears `is_deleted=False` and logs
  `[CLAN-RESTORED]` whenever a successful `GET /clans/{tag}` response arrives for a previously-deleted clan
- `update_player_info_in_user_accounts`: sets player["coc_role"] from coc.Role.name with
  "co_leader" remapped to "coLeader" (str() gives title-case; .value gives "admin" for elder).
  Also detects in-game player name changes (player["player_name"] vs member.name); propagates
  updates via `update_player_name_index_sync()` in a thread (DB write-through only — no
  in-memory mirror since 2026-08-18, PLAYER_NAME_INDEX_RETIREMENT_PLAN.md Steps 5-6). Logs
  `[PLAYER-NAME-UPDATE]` per tag + `[PLAYER-NAME-INDEX]` summary per clan cycle.
- _schedule_role_sync_for_clan / _do_role_sync: fires background role sync after every real API
  fetch; guards with CONFIG.is_dev_mode check (skip non-dev guilds, matching periodic loop)

🟧 qapbot/cache_manager.py (~3230 lines)
- CacheManager class with write-through persistence to SQLite database
- All data loaded from database on startup, persisted via write-through on mutation
- Error handling with try/except + log + re-raise on all write-through methods
- Provides centralized access to db_manager for all database operations
- No JSON persistence (only temp war files in data/temp/ remain as JSON)
- `search_player_names(query, limit=25)`: unconditionally delegates to `db_manager.search_player_names_sync()` (SQLite/FTS5) since 2026-08-18 (PLAYER_NAME_INDEX_RETIREMENT_PLAN.md Steps 5-6 retired the in-memory dict and rollout flag this used to sit behind). Sorted alphabetically, capped at `limit`. Backs the CWL guest search's name-substring mode. NOT used by /whois.
- **`/whois`'s own two-step search** (2026-08-18, `PLAYER_NAME_INDEX_RETIREMENT_PLAN.md` Steps 1-3, `QBdiscordcmds.py`): `_build_guild_player_name_matches()` does an always-complete, uncapped in-memory pass over the guild's own player pool first (built from `user_accounts`/`temp_war_stats`/`coc_clan_cache`), then `db_manager.search_player_names_full_sync()` (`hard_cap=5000`) fills in everyone else, deduplicated by tag. Guild matches are concatenated first, so they can never be pushed out by the 25-result UX slice regardless of global match volume — replaces the old inline `CACHE.player_name_index` scan plus a separate post-search reorder step.

🟨 qapbot/db_manager.py (Database Layer - ~6200 lines)
- WarHistoryDB class for ALL SQLite database operations (22 tables)
- WAL mode enabled for server-machine reliability and concurrent reads
- Idempotent operations (INSERT OR IGNORE, CREATE TABLE IF NOT EXISTS)
- Batch transaction support for performance (1000+ rows per commit)
- Automatic connection recovery: `_ensure_connection()` / `_reconnect()`
- Explicit BEGIN/ROLLBACK transactions on 6 compound write methods
- Hot/history DB split: `war_attacks`, `war_summary`, `cwl_league_groups`, `cwl_league_rounds`
  are mirrored into a second attached database (`data/qapbot_history.db`, schema alias
  `history`) via `ATTACH DATABASE`; the other tables stay hot-only
- Tables: war_attacks, war_summary, clans, clan_families, clan_family_members, users,
  user_players, user_buddies, guild_config, guild_member_families, guild_member_clans,
  guild_welcome_families, guild_welcome_clans, guild_clan_roles, subscriptions,
  notification_state, channel_notification_state, leaderboard_messages,
  cwl_league_groups, cwl_league_rounds, player_name_index, bot_metadata
- Key new methods (Phase 8):
  · `add_war_attack_records_sync()`: INSERT OR IGNORE per-attack rows into war_attacks
  · `add_war_summary_sync()`: INSERT OR REPLACE a war_summary row
  · `update_war_attack_records_sync()`: atomic DELETE + INSERT for late-attack updates
  · `war_attacks_exist_sync()`: existence check for a war_id
  · `get_clan_attack_history_sync()`: aggregates war_attacks per (war_id, player_tag); includes Total_Dest_Pct field
  · `get_war_summaries_sync()`: reads war_summary rows with optional CWL/season filter
- CWL round tracking methods (2026-05-09):
  · `upsert_cwl_league_data(league_group_id, cwl_season, clan_tags, rounds)`: upserts cwl_league_groups (8 rows) + cwl_league_rounds (one per war_tag), INSERT OR IGNORE
  · `get_cwl_round_for_war_tag_sync(war_tag)`: sync lookup; used at finalization time before the DB INSERT
  · `get_cwl_max_rounds_sync(season_clan_pairs)`: returns {cwl_season: max_rounds} for /whois CWL activity denominator
- Player name index methods (2026-05-15+):
  · `_upsert_player_name_index_in_conn(conn, attack_params)`: writes to player_name_index within any open conn/transaction; called by all war write paths
  · `load_player_name_index_sync()`: bulk-loads entire table → {player_tag: player_name} dict at startup
  · `update_player_name_index_sync(updates)`: batch-upsert for API-detected player name changes (from coc_cache)
  · `search_players_by_name_sync` deleted 2026-08-18 (PLAYER_NAME_INDEX_RETIREMENT_PLAN.md Step 7) — a `war_attacks` full-table LIKE fallback, already dead (zero production callers) before this session's FTS5/two-step search work even started
- Chart data methods (2026-05-17+):
  · `get_player_monthly_star_dist_sync(player_tag)`: per-month, per-type (CW/CWL) star distribution for Skill/Reliability /whois charts
  · `get_global_db_statistics_sync(force_refresh=False)`: includes `players_tracked_count` (COUNT(*) FROM player_name_index).
    Full scan across multi-GB hot+history war_summary/war_attacks tables — cached 25h (`_GLOBAL_STATS_TTL`, 2026-08-03) since
    `/status` is only called a few times a week; cache is warmed at bot startup and refreshed (`force_refresh=True`) at the
    end of `run_nightly_maintenance_routine()` in QapBot.py (after REINDEX/VACUUM/ANALYZE), so `/status` should never pay
    the cold-scan cost during normal operation. See `QapBot._warm_global_db_stats_cache()`.
    2026-08-18: the 5 sub-queries run concurrently (one `_sync_conn()` connection each via a small
    local `ThreadPoolExecutor`) instead of sequentially on one connection — cuts wall time from
    ~20s to roughly the single slowest query, since this was found live on PROD to be starving
    the sync connection pool that the periodic clan-fetch cycle's Phase-1 also needs at startup.
    Every exact computation also persists its result to `bot_metadata` (key
    `DatabaseManager._GLOBAL_STATS_METADATA_KEY`) as JSON with a wall-clock `computed_at_utc`.
    `preload_global_db_statistics_from_snapshot()` (async — uses the aiosqlite connection, not
    the sync pool) restores that snapshot into the in-memory cache at startup, converting the
    persisted wall-clock age back into an equivalent `_global_stats_cache_ts` so the normal
    `_GLOBAL_STATS_TTL` check still expires it at the right time. `_warm_global_db_stats_cache()`'s
    plain startup call (`force_refresh=False`) now tries this restore first — a single tiny row
    read — and only falls through to the real (now-parallelized) scan if no snapshot exists yet
    (fresh DB). The `force_refresh=True` call at the end of nightly maintenance always does the
    real scan and refreshes the persisted snapshot for the next restart.
- Never called directly from business logic - only via CACHE.db_manager
- **Production Status**: Database-only mode (no feature flags), all data stored in SQLite
📖 See: DATABASE_ARCHITECTURE.md for schema and architecture details

🟪 QBdiscordcmds.py (~4320 lines)
- Discord command handlers (/leaderboard, /subscribe, /admin, /clan management, /whois, /list, /help, etc.)
- /analyse command group: analyse_group containing `/analyse leaguegroup` to rank all CWL group players
- /whois slash command + two right-click context menus (user and message) sharing _whois_logic()
- Unified message ID logic for all commands
- Imports UI components from split UI modules (ui_common/ui_registration/ui_notifications/ui_clan_management)
- Imports helpers from qapbot/QBdiscocmdshelper.py
- leaderboard_clan_autocomplete: passes guild_id to restrict fallback to guild-subscribed clans only (cross-server isolation)
- Note: Account management (/myaccounts) and notifications (/notify) deprecated - functionality moved to registration message UI

🟦 QBcore.py
- Core bot and CoC client initialization
- Global bot instance and singleton pattern
- Shutdown coordination and cleanup management- `backfill_idle_event` (asyncio.Event, starts set): cleared while `/admin Backfill CWL Groups`
  runs, set again in a finally block on completion. Mirrors `db_maintenance_idle_event` pattern.
  Phase-1 in `periodic_main` waits for it before starting a new update cycle (prevents a cycle
  from running concurrently with in-flight backfill API calls).
- `/admin Backfill CWL Groups` command (bot-admin only): fetches CWL league groups from the
  CoC API for all clans with <7 rounds in war_summary for a season entered via modal;
  deduplicates API calls per group; writes via existing `get_league_group()` / `upsert_cwl_league_data()` pipeline
🟩 QBwarsim.py
- War simulation and win probability calculations
- Attack pattern analysis and outcome prediction
- monte_carlo_war_prediction(): uses an EPHEMERAL ProcessPoolExecutor with
  mp_context=multiprocessing.get_context('spawn'). spawn is critical: the function
  runs inside asyncio.to_thread() so the parent has multiple threads active; the
  Linux default fork() would inherit any locked mutexes (e.g. logging lock) and
  deadlock workers permanently. spawn starts a fresh interpreter with no inherited
  lock state. A 30-second concurrent.futures.wait() timeout provides a safety net
  that falls back to sequential execution rather than hanging forever.
  Workers exist only during the computation, never idle in the background.
- _sim_n_workers / _sim_min_chunk: configured at startup by init_sim_pool(); runs
  in-process below the chunk threshold to keep test monkeypatching compatible
- shutdown_sim_pool(): no-op kept for API compatibility with cleanup callers

🟩 qapbot/ui_common.py (~220 lines)
- TrackedView: Base class for the "tracked temporary message" pattern — self.message
  attribute + on_timeout() deletes it + on_error() suppresses 10062 (expired interaction
  token). Use for any new View that posts a message it should clean up on timeout;
  override on_timeout()/on_error() and call super() if extra behavior is needed. Adopted
  by GenericSelectView, LanguageSelectView, RegistrationView, AccountActionView, and 5
  views in ui_notifications.py — use it for new views (e.g. CWL roster) instead of
  copy-pasting the pattern again.
- GenericSelectView: Reusable dropdown selection view
- LanguageSelectView: Standalone language selection for /language command
- update_user_metadata_from_interaction: Utility for user metadata updates

🟩 qapbot/ui_registration.py (~1920 lines)
- RegistrationView (4 buttons: Link Account, War Notifications, API Verification, My Accounts)
- AccountActionView: Verify existing or link new player
- AccountManagementView: Single-message account overview with player selection dropdown
- UnlinkConfirmView: Inline confirmation dialog for player unlinking
- ApiVerificationPromptView, ApiTokenEntryModal: API token verification flow
- PlayerSubstringModal, VerifyAccountModal, ApiTokenOwnershipModal: Registration modals
- Callback functions: _clan_filter_callback, _user_player_select_callback, _show_player_search_modal

🟩 qapbot/ui_notifications.py (~1430 lines)
- WarNotificationPromptView: Post-registration notification opt-in
- UnifiedNotificationView: Complete notification management (enable/disable, type, mode)
- LanguageSelectionView: Language picker within notification flow
- NotificationSettingsView: Clan-wide and user-specific notification configuration

🟩 qapbot/ui_clan_management.py (~6550 lines)
- ClanManagementView: Main hub with mode switching (config, roles, families, registrations, notifications)
- Configuration views: ChannelConfigurationView, LanguageConfigurationView, NotificationThresholdConfigurationView
- RoleConfigurationView: Newbie/member role assignment configuration
- WelcomeMessageConfigView: Pending-state welcome config dialog (no DB writes until Save)
  · clan_link mode: toggle buttons for families (🏰/🏯) + individual clans (✅/➕), filling rows 0-3
    (max 20 slots: 5 families + up to 15 clans); row 4 always has mode toggle + Save/Cancel
  · Per-family mutual exclusion: selecting a family deselects any individually-picked clans
    belonging to it; individually picking a clan deselects its owning family (if selected).
    Families are independent of each other (Family A whole-selected, Family B partial clans OK)
  · apply_channel mode: row 0 has ChannelSelect instead of family/clan buttons (`_build_items()`
    fully rebuilds items from pending state on every mode switch/toggle)
  · Persists to `welcome_clan_tags`/`welcome_family_tags` (multi-select, see DATABASE_ARCHITECTURE.md);
    legacy single `welcome_clan_tag` column is read-only fallback for un-migrated guilds
  · Save allows zero clan-link selections (welcome message just omits the clan-link line);
    only apply_channel mode with no channel is blocked
- Family management: CreateFamilyModal, EditFamilyView, RenameFamilyModal, AddClanModal
- MemberClansConfigurationView: Member clan/family selection for role system
- ClanManagementLinkAccountView: Admin account linking with notification settings
- AdminOverrideConfirmView, ClanManagementAdminOverrideView: Verified player override dialogs
- ImportDataConfirmView, SwitchViewContinueView: ClashPerk data import

🟩 qapbot/guild_role_manager.py
- Discord role management for CoC In-Game Roles (Member/Elder/Co-Leader/Leader) and Per-Clan
  Roles — full function inventory + sync/bootstrap call graph in Function Tree § guild_role_manager.py
- Data model: `_get_highest_coc_role_for_user` reads `player["coc_role"]` (memory-only, set by
  coc_cache); values are `"member"`, `"elder"`, `"coLeader"`, `"leader"` (match COC_ROLE_PRIORITY)

🟫 qapbot/QBdiscocmdshelper.py (~4713 lines)
- Command helper functions (admin checks, display formatting, etc.)
- Player registration helpers (process_player_registration, complete_account_linking_flow)
- Account management helpers (set_primary_account, unlink_player) - used by AccountManagementView
- Notification formatting (format_notification_settings) - used by RegistrationView and UnifiedNotificationView
- Autocomplete factory functions (create_clan_autocomplete, create_family_autocomplete)
- Playerregistration command helpers (setup_playerregistration_message, update_playerregistration_subscription)
- get_clan_family_autocomplete_choices(current, channel_id, guild_id, mode, max_choices):
  `guild_id` restricts the fallback in "subscribed_first" mode to clans/families subscribed
  in the requesting guild — prevents cross-server clan leakage in autocomplete dropdowns
- Note: get_player_autocomplete_choices() removed (was exclusive to deprecated /myaccounts command)

🟫 QBcsvhandling.py
- JSON war data loading from temp/archive directories
- File path helpers and War ID extraction
- Late attack updates and duplicate detection
- Note: CSV history operations deprecated — war history now in SQLite database
- **Phase 8 additions**:
  · `_parse_start_time()`: parses `datetime.datetime(...)` Timestamp string from CoC API objects
  · `build_per_attack_rows()`: converts raw war JSON dict → flat list of per-attack row dicts
    (sentinel row with attack_order=0 for 0-attack players)
  · `build_war_summary()`: extracts clan-level war summary dict from raw war JSON
  · Both called from `_append_current_war_to_history()` (finalization) and
    `_process_war_history()` (late-attack update) so war_attacks/war_summary stay in sync

🟫 qapbot/config.py
- CONFIG object for all configuration values
- Configuration validation with fail-fast startup
- Uses ConfigurationError for invalid/missing settings
- load_dotenv(override=False): OS/patched env vars take priority over .env (safe for tests)
- SLEEP_INTERVAL minimum: 10 s in DEV mode, 60 s in PROD mode

🟫 qapbot/constants.py
- Centralized constants module (200+ lines)
- Discord API limits (message length 2000, embed length 4096)
- CoC API configuration (rate limits, cache TTL)
- War processing (inactive interval 22h, stale threshold 24h)
- Time conversions (seconds per minute/hour/day)
- All numeric thresholds and magic numbers
- Used by 6+ modules to eliminate hardcoded values

🟫 qapbot/exceptions.py
- Custom exception hierarchy (21 exception classes)
- Base: QapBotError with message and context support
- Configuration: ConfigurationError
- Cache: CacheError, CacheSaveError, CacheLoadError, CacheCorruptionError
- War: WarProcessingError, WarDataFetchError, WarStateError, WarHistoryError
- Leaderboard: LeaderboardError, LeaderboardGenerationError, LeaderboardPostingError
- Account: AccountProtectionError, VerifiedAccountError, OwnershipConflictError, ApiTokenValidationError
- Notification: NotificationError, NotificationSendError, NotificationConfigError
- Validation: ValidationError
- Context-aware error reporting for better debugging

🟫 qapbot/formatting.py
- Leaderboard rendering and column alignment
- Unicode/special character handling
- MODE_REGISTRY for all leaderboard modes
- **Phase 8 addition**: `Ø🔥/Atk` (width=9) column added as index 6 to all 6 modes
  (currentwar, attack, avgstars, avgstarsbyth, attack_cwl, avgstars_cwl).
  `render_leaderboard()` guards with `if len(cols) > 6` for backward compatibility.
  Values formatted as `f"{avg_dest:.1f}%"` (e.g. `52.3%`).
- text_display_width_float() calibration table extended with BrawlerBaller special chars:
  IPA Extensions (ʜ ʙ ʟ ɴ ꜱ), Greek block (Λ ω Ξ σ ϟ), Bullet (•),
  Dingbats block (❂ ❆ ✞ ⚝ ➓), APL block (⍭); all entries include Unicode code point+block comments

🟫 qapbot/discord_health.py
- Discord API retry wrapper with exponential backoff
- Rate limit handling (HTTP 429)
- Basic statistics tracking for API calls
- `bulk_sync_global_commands()`: raw-HTTP global command bulk-upsert that always re-includes
  any existing Activities `PRIMARY_ENTRY_POINT` command (discord.py's `CommandTree.sync()` has
  no model of it and would otherwise submit a payload that omits it — Discord rejects that
  outright, HTTP 400/50240, instead of deleting it). Used in `QapBot.py` in place of
  `tree.sync(guild=None)`/`tree.clear_commands(guild=None)` wherever a *global* sync happens;
  guild-scoped syncs are unaffected (Entry Point commands are inherently global-only).
  See CWL_CLAN_CONFIG_ACTIVITY_PLAN.md Phase D.

🟫 qapbot/web_bridge.py
- `aiohttp.web` app for the CWL Clan-Config Discord Activity (CWL_CLAN_CONFIG_ACTIVITY_PLAN.md
  Phase B), started from `QapBot.py`'s `_setup_hook()` / stopped from `async_cleanup()` —
  no-ops entirely unless `WEB_BRIDGE_PORT`/`WEB_BRIDGE_SECRET` (or the `_DEV` pair) are both set
  in `.env`. Bound to `127.0.0.1` only; a `cloudflared` tunnel (quick tunnel for DEV, named
  tunnel for PROD) is what makes it reachable from the Cloudflare Worker.
- `GET/POST /api/cwl/clan-config`: re-derives admin status itself via the same
  `check_admin_permissions()` logic the Discord-side UI uses (never trusts the Worker's OAuth
  check alone) before reading/writing `cwl_events`/`cwl_event_clans` through the normal
  `db_manager.py` functions — a second UI in front of the same data, not a second data store.
- `GET /api/health`: unauthenticated liveness check (used by the tunnel setup to confirm
  reachability end-to-end).
- `/api/tracker/*` (BUG_FEATURE_TRACKER_PLAN.md Phase 6): list/get items, download attachment
  bytes, and the three write endpoints (status/comment/testcases) — all gated by the same
  `X-Bridge-Secret` as the CWL routes above (deliberate, single-admin trade-off, plan §6.4/§8.7);
  `X-Tracker-Admin` is attribution-only, never authentication. Handlers delegate the actual
  DB+Discord work to `qapbot/ui_tracker.py` (`apply_status_change()`, `post_comment()`,
  `post_test_cases()`) rather than duplicating it.

🟩 qapbot/ui_tracker.py (BUG_FEATURE_TRACKER_PLAN.md, Phases 2-5)
- Discord UI layer for the bug/feature tracker — kept separate from `ui_clan_management.py`
  (plan §5.1). `BotSetupView` (`/admin` → Bot Setup); `TrackerItemModal`/`TrackerDraftView`/
  `start_tracker_item()` (the `/bug`/`/feature` modal → draft-preview → post flow);
  `build_tracker_embed()`/`_post_tracker_item()` (posted item + discussion thread);
  `TrackerItemButton` (restart-safe `DynamicItem`: Edit/Add files/Status/Test cases);
  the upload-window mechanism (`_upload_windows`, `handle_tracker_upload_message()` — called
  from `QapBot.py`'s `on_message`); `apply_status_change()`/`TrackerStatusSelectView` (status
  lifecycle); `post_test_cases()`/`TrackerTestPassButton`/`TrackerTestFailButton`/
  `handle_tracker_test_reaction()` (test-case sign-off loop + the 👍-reaction shortcut, called
  from `QapBot.py`'s `on_raw_reaction_add`). CACHE.tracker_settings caches `bot_settings`
  (Rule 3); everything else (items/attachments/testcases) reads fresh via `CACHE.db_manager.*`
  each time, not cached — matches how other low-frequency admin-facing rows are handled.

🟫 qapbot/mcp/ (BUG_FEATURE_TRACKER_PLAN.md Phase 7)
- `tracker_mcp.py`: stdio MCP server (`python -m qapbot.mcp.tracker_mcp`), hand-rolled
  JSON-RPC 2.0 (no `mcp` PyPI dependency — the wire surface needed is small). Exposes 5 tools
  (`tracker_list_items`/`tracker_get_item`/`tracker_set_status`/`tracker_comment`/
  `tracker_add_testcases`) to VS Code Copilot Chat (`.vscode/mcp.json`) and Claude Code
  (`.mcp.json`) against the web bridge's `/api/tracker/*` endpoints.
- `tracker_bridge_client.py`: thin async HTTP client (reuses `aiohttp`, already a dependency).
- `tracker_envelope.py`: the plan §6.6 untrusted-data mitigations — `sanitize_field()` (strips
  control chars, neutralizes ``` fences, caps length) and `wrap_untrusted()` (labelled
  `<user_report trust="untrusted">` envelope). Every reporter-supplied field goes through both
  before it reaches a tool result — bug/feature reports are arbitrary Discord-user text fed
  straight into an agent's context, a textbook prompt-injection surface.

🟫 qapbot/coc_health.py
- CoC API retry wrapper: `coc_retry(operation, operation_name, max_retries=2)`
- Exception routing (no-retry vs retry):
  · coc.NotFound — immediate raise, no retry
  · coc.PrivateWarLog — immediate raise, counted as success (definitive 403)
  · coc.Maintenance — immediate raise, no retry (see maintenance detection below)
  · coc.HTTPException 429 — sleep Retry-After then retry
  · other HTTP / generic — exponential backoff retry
- Per-cycle CoC maintenance fast-fail:
  · `_maintenance_detected` (bool) — cycle-wide flag; set on first coc.Maintenance hit
  · First detection: log one [WARNING], set flag, raise immediately
  · Subsequent concurrent hits: log at DEBUG, raise immediately — no sleep, no retry
  · CoC maintenance lasts 10–30+ min; retrying per-clan within the same cycle is
    pointless and caused 3200s+ cycles (1889 clans × 30s sleep / 20 slots ≈ 2800s)
  · `clear_maintenance_detection()` — called at start of each cycle (alongside
    `reset_cycle_stats()`) so every cycle re-probes the API fresh
  · `is_maintenance_detected()` — checked after Phase-1 gather() to log a single
    [PHASE-1] WARNING summary with the count of affected clans
- DEV-mode throttle: `_DEV_API_THROTTLE_S = 0.022` (22 ms global gap between calls)
- Reconnect callback: `set_reconnect_callback()` — registered by startup_login() so
  coc_retry can re-auth the client on unexpected session close
- Per-cycle and lifetime statistics tracking (`reset_cycle_stats`, `get_coc_stats`)

## Refactoring Summary (2025-12-20)

### Changes Made
1. **Created qapbot/ui_components.py** (~260 lines)
  - Note: This was later split into `qapbot/ui_common.py`, `qapbot/ui_registration.py`, `qapbot/ui_notifications.py`, `qapbot/ui_clan_management.py` and `qapbot/ui_components.py` was deleted (2026-02-17)
  - Note: `qapbot/ui_welcome.py` was renamed to `qapbot/ui_registration.py` and `WelcomeView` → `RegistrationView` as part of the registration-message refactor
  - Originally moved UI classes from QBdiscordcmds.py (e.g. WelcomeView, PlayerSubstringModal)
   
2. **Enhanced qapbot/QBdiscocmdshelper.py** (+400 lines)
   - Added player registration helpers (build_player_list_for_clan, filter_out_verified_registered_players_for_user, process_player_registration)
   - Created autocomplete factory functions (create_clan_autocomplete, create_family_autocomplete)
   - Added playerregistration command helpers (setup_playerregistration_message, update_playerregistration_subscription)
   
3. **Refactored QBdiscordcmds.py** (-536 lines, 25% reduction)
   - Removed duplicate player registration logic (~130 lines consolidated)
   - Removed UI classes (~260 lines moved)
   - Simplified playerregistration command (~127 lines extracted)
   - Updated imports to use new modules

### Benefits
- **Improved Modularity**: UI components separated from command logic
- **Reduced Duplication**: Consolidated 98% identical modal processing methods
- **Better Testability**: Helper functions can be tested independently
- **Cleaner Architecture**: Clear separation of concerns
- **Easier Maintenance**: Smaller, focused files

## Main Flows

### 🟦 Regular Update Cycle (QapBot.py) - Multi-Phase Pipeline

**Note**: CoC API calls are parallelized; file/DB operations are intentionally processed sequentially to avoid race conditions. The phases below match the `[PHASE-*]` log tags emitted by `periodic_main()`: PHASE-1, PHASE-1.5, PHASE-2, PHASE-3, PHASE-3B (leaderboard posting runs after, concurrently).

**Phase 1: Parallel API Fetches (async)**
```
└── main()
    ├── startup_login()
    └── periodic_main()
    ├── (build clans_to_update list — active clans + capped passive shard)
    ├── PHASE 1: fetch_clan_war_data() for all active clans (async gather)
    │   ├── CoC API calls go through CACHE
    │   │   ├── get_clan() via CACHE.coc_clan_cache.get_clan()
    │   │   └── get_current_war() via CACHE.get_current_war_from_api()
    │   ├── CACHE.save_war_object(war, tracked_clan_tag=clan_tag)
    │   │   └── Saves JSON war data to data/temp/
    │   └── Returns war_data dict (or None)
        │
    ├── PHASE 1.5: CWL war-tag recovery batch (async)
    │   └── Re-fetches active CWL wars whose war tags were discovered during
    │       Phase 1, writing their temp JSON.
        │
    ├── PHASE 2: process_orphaned_cwl_wars(failed_clans) (async)
    │   ├── Identifies orphaned CWL wars across all clans
    │   ├── Fetches final state via CACHE.get_league_war(war_tag) (parallel)
    │   ├── On success: updates JSON and triggers manage_war_files() to finalize
    │   │  (on failure: file may remain in temp for retry)
    │   └── Dispatches a background WAL checkpoint task
    │
    ├── PHASE 3: sequential file processing for active clans (sync)
    │   ├── process_clan_war_data(clan_tag, war_data) for each successful fetch
    │   └── manage_war_files(clan_tag, "") for failed fetches (finalize any orphans)
        │
    ├── PHASE 3B: finalize temp files for passive (inactive) clans (sync)
    │   └── mtime-based skip-until optimization: unchanged passive war files on
    │       HDD/server-machine are not re-read every cycle (minimizes server-machine I/O).
        │
        ├── generate_leaderboard_text(TERMINAL) 🟩
        │   ├── calculate_leaderboard()
        │   │   ├── _merge_entries()
        │   │   │   └── uses MODE_REGISTRY from qapbot/formatting.py 🟫
        │   │   ├── _load_history_filtered()
        │   │   │   └── CACHE.get_clan_history(clan_tag) 🟧
        │   │   │       └── db_manager.get_clan_attack_history_sync() (war_attacks only)
        │   │   └── CACHE.load_temp_war_stats(clan_tag) 🟧
        │   │       └── _load_war_data_from_json() 🟫
        │   └── render_leaderboard() 🟫
        │
        └── post_leaderboards_to_subscribed_channels()  [channels run concurrently via asyncio.gather + Semaphore(5)]
            ├── [cwlinfo subscriptions — embed path]
            │   ├── generate_cwlinfo_embeds() 🟩  (async, DB-backed)
            │   │   ├── CACHE.db_manager.get_war_summaries_sync(is_cwl=True)
            │   │   ├── [or CACHE.get_league_group() for live/current season]
            │   │   ├── _cwl_compute_skill_factors()  ← unified skill loader (all modes)
            │   │   │   ├── _load_skill_factors_for_clan() × all group clans  ← universal base
            │   │   │   │   └── db_manager.get_player_cwl_attacks_multi_season_sync()  ← per-player best season (clan-agnostic)
            │   │   │   └── [comp_mode only] compute_player_skill_factors_from_attacks()  ← current-season overlay
            │   │   └── calculate_win_probability(player_skill_factors=...)  ← skill-adjusted Monte Carlo
            │   ├── calculate_content_hash() from embed descriptions
            │   └── post_cwlinfo_embeds_to_discord(content_hash=...) 🟩
            └── [all other modes — text/leaderboard path]
                ├── await asyncio.to_thread(generate_leaderboard_text, ...)  ← offloads CPU+sync chain to thread
                ├── generate_war_info_text()  [sync; uses skill-adjusted calculate_win_probability()]
                │   ├── calculate_leaderboard() (see above)
                │   └── render_leaderboard() 🟫
                ├── calculate_content_hash() 🟦
                ├── delete_leaderboard_messages_for_context() 🟩
                └── post_leaderboard_to_discord() 🟩
                    ├── calculate_content_hash() 🟦
                    └── _split_and_post_leaderboard_helper() 🟩
```

**Multi-Phase Architecture Rationale**:
- **Phase 1 (Parallel API)**: Network-bound CoC calls in parallel + lightweight JSON save
- **Phase 1.5 (CWL war-tag recovery)**: Batches the follow-up fetches of active CWL wars whose war tags were only discovered during Phase 1
- **Phase 2 (Parallel Orphans)**: CWL-only recovery via `get_league_war()` for orphaned wars; also dispatches a background WAL checkpoint
- **Phase 3 (Sequential Processing)**: Deterministic file/DB work for active clans to prevent race conditions
- **Phase 3B (Passive Finalization)**: Sequentially finalizes temp files for passive clans, skipping unchanged files via an mtime / skip-until cache to minimize server-machine I/O
- **Leaderboard Posting (Parallel)**: `post_leaderboards_to_subscribed_channels()` runs all channels concurrently via `asyncio.gather` + `Semaphore(5)`; `generate_leaderboard_text()` and `calculate_win_probability()` both offloaded to thread pool via `asyncio.to_thread()` to keep the event loop responsive
- **Orphaned War Types**:
  - CWL orphans: Skipped in Phase 2, fetched in Phase 3, finalized next cycle
  - Non-CWL orphans: Finalized immediately in Phase 2 (no API to refetch final state)

### War Info Text — Sentinel Pattern (currentwar)

Discord does not render custom emojis (`<:NAME:ID>`) inside ` ``` ` code blocks.  
The war info text (roster lineups, win probability, status line) uses byte-sequence sentinels to mix plain text and code blocks in a single Discord message.

**How it works:**
```
generate_war_info_text()
  → emits: {_PLAIN_SENTINEL_START}{status line + rosters + prob}{_PLAIN_SENTINEL_END}
           (followed by ``` code block with clan stats table)

_split_and_post_leaderboard_helper()
  → detects PLAIN_SENTINEL_START in text
  → pairs each plain segment with its following code segment
  → if combined length ≤ 2000 chars: posts ONE Discord message:
      plain text (emojis render) + "```" code block (monospace table)
  → if combined > 2000 chars: falls back to separate plain + code messages
```

**Key design rules:**
- `preparation` state: sentinel block only (no stats table; attacks haven't started)
- `in_war` state: sentinel block + clan stats table (Stars, Attacks, Stars Max, 🔥%)
- Stars Max shows `?` instead of crashing when `calculate_max_possible_stars()` fails

### CWL War League — Background Fetch Pattern

`_resolve_war_league(tag, opponent_tag=None)`:
1. Fast path: reads `clan_name_cache` entry (DB-backed, no API call)
2. If missing: schedules a background `create_task(_bg_fetch_league(tag))` so the next
   call will have the data. Protected by `_league_refresh_in_progress` dedup set.
3. `opponent_tag` is threaded so the background fetch can use the opponent's league as
   a fallback when the primary clan has no `warLeague` in the CoC API.
4. Suppresses the "no league resolved" warning when the actual league equals the default string.


### 🟪 Discord Bot Commands (QBdiscordcmds.py)

└── Discord command handlers
    ├── /leaderboard
    │   ├── Argument parsing (clan, mode, time, scope)
    │   │   ├── `month` accepts a single value ("6"), a range ("6-7"), a semicolon list
    │   │   │   ("1;3;5"), or a trailing count ("-2" = last 2 months, may cross a year
    │   │   │   boundary) — parsed by `parse_month_argument()` into (month, year) pairs
    │   │   └── `scope`: "all" (default) credits a current clan member's stats even for wars
    │   │       fought while registered to a clan no longer tracked/subscribed here; "own"
    │   │       restores the previous per-clan-only behavior. Resolves the current roster via
    │   │       `CACHE.coc_clan_cache.get_clan()` per target clan/family, then threads
    │   │       `member_player_tags` through to `calculate_leaderboard(scope=...)` — see
    │   │       DATABASE_ARCHITECTURE.md § 2026-07-30 for the cross-clan DB query.
    │   ├── Resolves the invoking user's own registered player tag(s) from
    │   │   `CACHE.user_accounts` and passes them as `highlight_player_ids` so their
    │   │   row is auto-bolded in the rendered table (see render_leaderboard() below)
    │   ├── await asyncio.to_thread(generate_leaderboard_text, ...)  ← offloads CPU chain to thread
    │   │   ├── calculate_leaderboard() (see above)
    │   │   └── render_leaderboard() 🟫
    │   │       └── wraps a highlighted row in ANSI bold/color escape codes (`_ANSI_HIGHLIGHT`
    │   │           / `_ANSI_RESET` in formatting.py) — style="discord" only; requires the
    │   │           message be posted in a ` ```ansi ` code block to actually render as color
    │   │           (not every mobile Discord client renders it — falls back to raw escape text)
    │   ├── post_leaderboard_to_discord() 🟩
    │   │   ├── delete_leaderboard_messages_for_context() 🟩
    │   │   ├── calculate_content_hash() 🟦
    │   │   └── _split_and_post_leaderboard_helper() 🟩
    │   │       └── all leaderboard code-block fences use the ` ```ansi ` language tag (not
    │   │           plain ` ``` `) so the highlight above can render; harmless no-op when no
    │   │           row is highlighted
    │   └── cwlinfo mode:
    │       ├── generate_cwlinfo_embeds() 🟩  (async, single embed per season; skill-factor call
    │       │   chain — _cwl_compute_skill_factors() → _load_skill_factors_for_clan() →
    │       │   calculate_win_probability() — is the same as in Regular Update Cycle above;
    │       │   per-player fallback logs: "Skill-data source for clan X: 5 players CWL 03/26, ...")
    │       │   ├── CACHE.get_league_group(clan_tag) → live CWL group (ongoing season)
    │       │   ├── _generate_cwlinfo_archive_embeds()  (sync, reads war_summary DB table)
    │       │   │   └── CACHE.db_manager.get_war_summaries_sync(clan_tag, is_cwl=True)
    │       │   └── asyncio.ensure_future(update_cwl_group_stats())  ← free piggyback
    │       └── post_cwlinfo_embeds_to_discord(content_hash=...) 🟩
    │           ├── delete_leaderboard_messages_for_context() 🟩
    │           └── discord_retry(channel.send(embeds=[embed]))
    │           NOTE: content_hash stored in leaderboard_messages for change detection
    └── cwlgroup mode:
        ├── update_cwl_group_stats(clan_tag, season) 🟩  (async; TTL=600s)
        │   ├── db.get_cwl_group_info() → group_id, clan_tags, cwl_ended, league_rank
        │   ├── db.get_cwl_group_war_stats() → stars_map (incl. 10-star win bonus) + ended_map
        │   ├── Folds in live in_war round from CACHE.get_current_war_data()
        │   ├── Computes ranks + db.update_cwl_group_stats_batch()
        │   └── Returns list[dict] sorted by group_rank
        └── asyncio.to_thread(generate_cwl_group_image, standings, season) 🟫
            └── Renders dark-themed PNG (matplotlib Agg); returns bytes → discord.File
    ├── /highlightme
    │   ├── Iterates this channel's subscriptions (excludes cwlinfo/cwlinfo_comp/cwlgroup —
    │   │   embed/image modes with no per-row table to highlight)
    │   ├── resolve_subscription_period(sub) 🟩  (QBhelperfunctions.py — shared with the
    │   │   automatic posting loop in QapBot.py so the rendered period always matches
    │   │   whatever is currently posted for that subscription)
    │   ├── generate_leaderboard_text(..., highlight_player_ids={invoking user's own tags})
    │   ├── leaderboard_text_has_highlight(text) 🟫  (formatting.py — checks for
    │   │   `_ANSI_HIGHLIGHT` in the rendered text) → skip this subscription (no repost)
    │   │   if the invoking user isn't actually listed in it
    │   └── post_leaderboard_to_discord() 🟩  for each subscription the user IS listed in
    │       NOTE: one-time by construction, no extra state tracked — the highlighted
    │       text's content_hash never matches the plain text the next auto-post cycle
    │       renders (see post_leaderboards_to_subscribed_channels() above, under
    │       Regular Update Cycle), so that cycle always reposts — and thereby clears
    │       the highlight — even if the underlying stats didn't change.
    │       NOTE: a rolling (no explicit month/year) subscription accumulates a separate
    │       tracked message per calendar month over time (05/2026, 06/2026, 07/2026, ...) —
    │       resolve_subscription_period() always resolves it to "now", so only the latest
    │       (current month's) message is ever targeted here; older months' messages are
    │       left untouched. The "no wars yet this month" fallback below only swaps the
    │       rendered TEXT for the previous month's data — the tracking key (month_range/year
    │       passed to post_leaderboard_to_discord) is deliberately left at the current
    │       period, matching QapBot.py's loop, so the fallback still updates the latest
    │       message instead of creating a stray duplicate on a past month's message.
    ├── /subscribe / /unsubscribe
    │   ├── Subscription management
    │   └── Cache update and persistence
    ├── /removeclan
    │   ├── Clan removal from cache
    │   └── Message cleanup
    ├── /cleanup_messages
    │   └── CACHE.cleanup_stale_message_ids() 🟧
    ├── /help / /subscriptions / /status / /ping / /whois / /list
    │   └── Info, status, and account lookup reporting
    └── /analyse leaguegroup
        ├── Fetches CWL group via CACHE.get_league_group() (live) or DB fallback
        └── generate_cwl_group_analysis_embeds() 🟩  → top 10 attackers + defenders across all group clans

## Function Tree (Key Functions Only)

Complete per-file inventory of key functions and their call relationships. Where a function's
behavior is already fully explained in **Main Flows** above, its entry here is trimmed to a
one-line pointer ("see Main Flows § ...") instead of repeating the description — full detail is
kept here only for functions not narrated elsewhere.

🟦 QapBot.py — full call graph in Main Flows § Regular Update Cycle; inventory:
├── main() → startup_login(), periodic_main()
├── fetch_clan_war_data() 🟩 (Phase 1, parallel gather)
├── process_orphaned_cwl_wars() 🟩 (Phase 2)
├── process_clan_war_data() 🟩 (Phase 3, sequential)
├── post_leaderboards_to_subscribed_channels() 🟦
├── generate_leaderboard_text() 🟩
├── calculate_content_hash() 🟦
├── delete_leaderboard_messages_for_context() 🟩
├── post_leaderboard_to_discord() 🟩
└── _split_and_post_leaderboard_helper() 🟩

🟩 QBhelperfunctions.py
├── generate_leaderboard_text() — full call graph in Main Flows § Regular Update Cycle (Phase 3B)
│   ├── generate_war_info_text() — see Main Flows § War Info Text Sentinel Pattern
│   ├── calculate_leaderboard()
│   │   ├── _merge_entries() — player name change handling via PlayerID; uses MODE_REGISTRY (qapbot/formatting.py 🟫)
│   │   ├── _load_history_filtered() → CACHE.get_clan_history(clan_tag) 🟧 → db_manager.get_clan_attack_history_sync() (SQLite)
│   │   └── CACHE.load_temp_war_stats(clan_tag) 🟧 → _load_war_data_from_json() 🟫
│   └── render_leaderboard() 🟫
├── post_leaderboard_to_discord() — see Main Flows § Regular Update Cycle
│   ├── normalize_leaderboard_text() — content normalization for change detection
│   ├── Discord API calls (channel.send/edit)
│   └── CACHE.leaderboard_messages management 🟧
├── generate_cwlinfo_embeds()  [async] — call graph in Main Flows § Regular Update Cycle & § Discord Bot Commands (cwlinfo mode); detail not covered there:
│   ├── _generate_cwlinfo_archive_embeds()  [sync; reads war_summary DB via get_war_summaries_sync]
│   │   └── _lineup_from_json(ascending=True for my clan, False for opponent)
│   │       · My clan sorted low→high TH so highest THs meet at centre "vs"
│   │       · Score box: `my⭐ – opp⭐ · dest% – dest%` (warended/inwar)
│   │       · Bidi fix: \u200e LRM before tag code blocks on vs. lines
│   ├── _cwl_compute_skill_factors() internals: fallback chain current CWL → latest past CWL → base rates;
│   │   searches across ALL clans so player history survives clan transfers between CWLs
│   │   (comp_mode overlay: get_cwl_attack_records_sync() + live cache attacks → compute_player_skill_factors_from_attacks())
│   └── _cwl_append_prediction()  ← single-line display; 📊 prefix for non-comp
├── post_cwlinfo_embeds_to_discord() — see Main Flows § Regular Update Cycle
│   ├── delete_leaderboard_messages_for_context()
│   └── CACHE.set_leaderboard_message()
├── update_cwl_group_stats(clan_tag, cwl_season)  [async; module-level TTL cache 600s] — see Main Flows § Discord Bot Commands (cwlgroup mode); detail not covered there:
│   ├── Short-circuit: cwl_ended=1 + all total_stars non-null → return cached DB data
│   ├── SQL: SUM(clan_stars) + SUM(CASE WHEN result='win' THEN 10 ELSE 0 END); SUM(clan_destruction * team_size) [matches in-game display]
│   └── db.update_cwl_group_stats_batch()  [skips unchanged rows]
├── generate_cwl_group_image(standings, cwl_season)  [sync; run in asyncio.to_thread]
│   · matplotlib Agg backend (no display); dark CoC theme (#3B2A1F background)
│   · Rank 1-2: green ▲ (promotion zone); rank 7-8: red ▼ (demotion zone)
│   · Returns raw PNG bytes → discord.File
├── _build_standings_result(rows, all_clan_tags, league_rank)
│   └── Merges DB rows with clan_name_cache; returns list sorted by group_rank
├── generate_cwl_group_analysis_embeds(clan_tag)  [/analyse leaguegroup engine]
│   ├── Live path: CACHE.get_league_group() + CACHE.get_league_war() (all group wars in parallel)
│   ├── DB fallback: _load_cwl_analysis_from_db_sync() via asyncio.to_thread (after CWL ends)
│   │   · reads latest CWL season from war_summary + war_attacks tables
│   │   · derives group clans from opponent_tag field; only tracked clans included
│   ├── Ranks top 10 attackers: most total stars, tiebreak lowest avg opponent map position
│   ├── Ranks top 10 defenders: fewest avg stars conceded per defense (min 2 defs)
│   └── Returns 2 Discord embeds (⚔️ attackers gold, 🛡️ defenders green)
├── predict_war_between_clans(clan1_tag, clan2_tag, n_players, apm)  [/admin WAR_PREDICT]
│   ├── Fetches both clans in parallel via CACHE.coc_clan_cache.get_clan()
│   ├── Selects top-N members by TH level descending per clan
│   ├── Resolves CWL league per clan via _ensure_clan_war_league()
│   ├── Loads per-player skill factors from DB via _load_skill_factors_for_clan()
│   ├── Builds synthetic war_data dict with all attacks still remaining
│   └── calculate_win_probability() → Win/Lose/Draw % formatted for Discord
├── update_clan_war_info_and_stats()
│   ├── CoC API calls: 2 per clan_tag — ALWAYS get_clan() (CACHE.coc_clan_cache.get_clan()) + ALWAYS get_current_war() (CACHE.get_current_war_from_api())
│   ├── CACHE.save_war_object(clan_tag, war) 🟧 — saves JSON war data file to data/temp/
│   ├── CACHE.get_temp_war_stats(clan_tag) 🟧 / CACHE.set_temp_war_stats(clan_tag, stats) 🟧
│   ├── QBwarsim.calculate_win_probability()
│   └── CACHE updates (clan_name_cache, war object)
├── manage_war_files() - synchronous war file lifecycle management
│   ├── Scans data/temp/ for all war files; identifies current war (newest file + opponent match + active state)
│   ├── Processes old/stale wars: validate → check history → finalize → archive
│   ├── Deletes duplicate temp file when archive exists and JSON is identical
│   └── Result: data/temp/ cleaned, completed wars in archive/ (side-effect driven, returns None)
├── process_orphaned_cwl_wars() — see Main Flows § Regular Update Cycle (Phase 2) for the overall flow; detail not covered there:
│   ├── Identifies orphaned wars: files with state != "war_ended" not newest in clan
│   ├── Groups by clan_tag to determine which war is current
│   └── Runs AFTER Phase 1 completes (separate async phase)
├── _process_war_history() - consolidated finalization logic (module-level function)
│   ├── Load war data from JSON → check if already in history → update with late attacks if present
│   ├── Append to history if new war (based on war type)
│   └── Move JSON to archive after processing; invalidate history cache
├── generate_message_key()
├── _normalize_api_datetime()
├── _build_war_id_from_dt()
├── _current_war_temp_csv()
├── get_effective_leaderboard_month_year()
└── _split_and_post_leaderboard_helper() — see Main Flows § War Info Text Sentinel Pattern
    └── Discord message splitting for long leaderboards

🟧 qapbot/coc_cache.py
├── CoCClanCache (class) - Stale-while-revalidate API cache
│   ├── get_clan() - Returns cached clan, background refresh if stale
│   ├── soft_ttl=280s, hard_ttl=600s
│   ├── _fetch_and_cache() / _schedule_background_refresh()
│   └── update_player_info_in_user_accounts()
│       ├── Sets player["th_level"] and player["current_clan_tag"] from API data
│       └── Sets player["coc_role"] via role.name + "co_leader"→"coLeader" remap

🟩 qapbot/guild_role_manager.py
├── _coc_role_refreshed_clans: set[str]  (module-level; one bootstrap per process lifetime)
├── _ROLE_SYNC_CONCURRENCY = 5  (module-level; caps concurrent sync_roles_for_user() calls
│   below, per asyncio.Semaphore built fresh inside each sync_all_roles_for_guild()/
│   sync_roles_for_clan_members() call — see RATE_LIMITING_IMPLEMENTATION.md "Discord
│   role-edit concurrency")
├── sync_all_roles_for_guild(guild, guild_id)
│   ├── Bootstrap: calls coc_clan_cache.get_clan() for each uncached member clan
│   │   └── Ensures coc_role is populated after restart (coc_role is memory-only)
│   └── Calls sync_roles_for_user() for every registered guild member, bounded-concurrent
│       via _ROLE_SYNC_CONCURRENCY (asyncio.gather over a semaphore-guarded worker, both
│       for the chunked-member pass and the capped missing-member verification pass)
├── sync_roles_for_user(guild, guild_id, discord_user_id)
│   ├── CoC role sync: _get_highest_coc_role_for_user() → assign/remove 4 CoC roles
│   └── Clan role sync: _get_clan_tags_for_user() → assign/remove per-clan roles
├── sync_roles_for_clan_members(guild, guild_id, clan_tag, coc_members)
│   └── Fast path from coc_cache.py's per-clan fetch trigger — calls sync_roles_for_user()
│       for just this clan's registered members, bounded-concurrent via _ROLE_SYNC_CONCURRENCY
├── create_coc_ingame_roles / delete_all_coc_ingame_roles
├── create_clan_role / create_all_clan_roles / delete_clan_role_from_guild
└── get_or_create_discord_role / normalize_discord_role_name

🟧 qapbot/cache_manager.py
├── CacheManager (class)
│   ├── clan_name_cache, user_accounts, subscriptions, etc. (dicts)
│   ├── db_manager (WarHistoryDB reference)
│   ├── load_all() - Loads all data from SQLite database
│   ├── Write-through methods:
│   │   ├── persist_user(user_id) - User accounts
│   │   ├── persist_clan(clan_tag) - Clan name cache
│   │   ├── delete_clan_from_cache(clan_tag) - Remove clan from cache + DB
│   │   ├── set_clan_family() / persist_clan_family() / delete_clan_family()
│   │   ├── set_server_config() / persist_server_config()
│   │   ├── set_subscriptions_for_channel() / delete_subscriptions_for_channel()
│   │   ├── set_leaderboard_message() / delete_leaderboard_message()
│   │   └── save_player_notification() / save_channel_notification()
│   ├── get_league_group(clan_tag, max_age) → ClanWarLeagueGroup
│   │   └── After every fresh API response calls _process_league_group_response()
│   │       (upserts cwl_league_groups + cwl_league_rounds; backfills war_summary.round_number)
│   ├── _make_league_group_id(clan_tags, cwl_season) → str  [static]
│   │   └── SHA-256(season:sorted_tags)[:16] — stable, season-scoped group ID
│   ├── _process_league_group_response(lg, cwl_season)
│   │   └── Upserts CWL group/round data + triggers backfill; idempotent (INSERT OR IGNORE)
│   │       When league_rank is freshly resolved, also calls _sync_group_track_war_updates()
│   ├── _sync_group_track_war_updates(clan_objs, league_rank)
│   │   └── Group-wide track_war_updates gate: corrects/creates every non-subscribed member's
│   │       war_league + track_war_updates to match the group's confirmed league (see
│   │       CLAN_WAR_TRACKING.md write-path 7); inserts never-before-seen members too
│   ├── get_league_war(war_tag, max_age)
│   ├── evict_stale_cwl_caches()
│   ├── save_war_object(clan_tag, war)
│   │   └── Saves JSON war data to data/temp/
│   ├── load_temp_war_stats(clan_tag)
│   │   └── _load_war_data_from_json() 🟫
│   ├── get_clan_history(clan_tag)
│   │   └── db_manager.get_clan_attack_history_sync() (from SQLite)
│   └── cleanup_stale_message_ids()
└── CACHE (global instance)

🟪 QBdiscordcmds.py
├── Discord command handlers (functions)
│   ├── /leaderboard — full call graph in Main Flows § Discord Bot Commands
│   ├── /subscribe / /unsubscribe
│   ├── /removeclan
│   ├── /admin (multiple actions)
│   │   ├── CLEANUP_MESSAGES      — clean up this guild's channels (guild admin)
│   │   ├── CLEANUP_MESSAGES_ALL  — clean up all guilds/channels (bot admin)
│   │   ├── CHECK_LOGS            — scan and summarise log files (bot admin)
│   │   ├── CHECK_DATA            — validate DB/data consistency (bot admin)
│   │   ├── LIST_ALL_SUBSCRIPTIONS — list every subscription (bot admin)
│   │   ├── TEST_NOTIFY           — test war notification for a clan (bot admin)
│   │   ├── REMOVE_CLAN           — remove a clan from tracking (guild admin)
│   │   ├── LIST_CLANS            — list all tracked clans (guild admin)
│   │   ├── IMPORT_DATA           — import ClashPerk player list embed (bot admin)
│   │   ├── DEBUG_MESSAGE         — analyse a Discord message structure (guild admin)
│   │   ├── REFRESH_DATA          — force refresh all clan CoC API data across all
│   │   │   guilds (subscribed + families + member_clans); bot admin only;
│   │   │   invalidates coc_clan_cache for every tag then parallel-fetches all
│   │   ├── RETRIEVE_CWL          — backfill CWL history for a clan (bot admin)
│   │   ├── BACKFILL_CWL_GROUPS   — fetch CWL league groups for clans with <7 rounds
│   │   │                           in war_summary for a season (bot admin)
│   │   ├── WAR_PREDICT           — predict outcome between two clans via
│   │   │                           predict_war_between_clans() (bot admin)
│   │   ├── START_UPDATE_CYCLE    — interrupt sleep and start next update cycle immediately
│   │   │                           (bot admin); queues cycle if one is already running
│   │   ├── OPTIMIZE_DB           — run nightly maintenance (archive move + DB
│   │   │                           ANALYZE/REINDEX/VACUUM) immediately (bot admin)
│   │   ├── MEMORY_PROFILE        — dump memory allocation stats to log file (bot admin)
│   │   ├── MAINTENANCE_START     — suspend updates and close DB for safe data access
│   │   └── MAINTENANCE_END       — restart bot and resume normal operation
│   ├── /clan_management (families, notifications, player registrations, roles)
│   ├── /list_accounts / /list_players / /list_families
│   ├── /help / /clans / /subscriptions / /status / /ping
│   └── Unified message ID logic for all commands
├── Note: /myaccounts and /notify commands removed (2026-01-02)
│   └── Functionality moved to registration message UI (RegistrationView → AccountManagementView / UnifiedNotificationView)

🟫 QBcsvhandling.py
├── _load_war_data_from_json()
│   └── Loads war stats from JSON files in data/temp/ (private)
└── Note: CSV history functions deprecated, war history stored in SQLite database

🟫 qapbot/config.py
├── CONFIG (class/instance)
│   ├── sleep_interval (min 10 s DEV / 60 s PROD)
│   ├── is_dev_mode (True when DISCORD_GUILD_ID > 0)
│   └── other config values
├── load_dotenv(override=False) — OS/patched env vars take priority over .env
└── _validate_config() — fail-fast; SLEEP_INTERVAL minimum is mode-aware

🟫 qapbot/formatting.py
├── MODE_REGISTRY (dict)  — all leaderboard modes including cwlgroup (sort_key=group_rank)
├── DEFAULT_MODE (str)
├── render_leaderboard()
├── pad_player_cell_leaderboard()
├── pad_player_cell_terminal()
├── normalize_player_name()
│   └── RTL names (Arabic/Hebrew/etc.) are wrapped in plain LRM (U+200E)
│       marks: "{LRM}{name}{LRM}". Unicode bidi ISOLATES (FSI U+2068 / PDI
│       U+2069) were tried instead (theoretically stronger, and don't need
│       help from the caller) but Discord's client does not appear to respect
│       them — live retesting on both desktop and iOS showed zero effect.
│       Plain LRM does work on Discord, but bracketing the name alone is only
│       half the fix: any caller that builds a line mixing an RTL name with
│       OTHER fields (separators, arrows, more text after the name) must ALSO
│       place a bare, non-bracketed LRM at each transition out of the name
│       back into the rest of the line — otherwise Discord merges the name's
│       RTL run with everything up to the next unambiguous Latin-text run and
│       mirrors that whole span (this is what caused CWL group-analyse embed
│       rows to scramble — see changelog). See QBhelperfunctions.py's CWL
│       group analyse row builder, and the "vs. [LRM name LRM](url)  LRM TAG"
│       pattern in its CWL round-lines builder, for the reference
│       implementation of both halves of this fix.
│       LRM is zero-width (Cf category) so text_display_width() ignores it.

🟦 QBcore.py
├── bot (discord.commands.Bot)
├── coc_client (coc.Client)
├── shutdown_event (asyncio.Event)
├── cleaned_up (bool)
├── maintenance_mode (bool)          # True while /admin Maintenance Start is active
├── maintenance_pending (bool)       # True when Maintenance Start was issued mid-cycle; periodic_main
│                                    # calls do_maintenance_shutdown() at natural cycle end
├── force_cycle_event (asyncio.Event) # Set by /admin Start Update Cycle to interrupt sleep phase
├── force_cycle_pending (bool)       # True when cycle was running at the time of Start Update Cycle;
│                                    # periodic_main skips next sleep when this is set
├── in_main_cycle (bool)             # True only while main() is executing (not during sleep)
├── exit_code (int)                  # Set to EXIT_CODE_MAINTENANCE (42) for restart
├── EXIT_CODE_MAINTENANCE (int = 42) # Recognised by start.sh to trigger immediate restart
├── BOT_VERSION (str)                # Semantic version string, shown in /status and startup log
├── nightly_maintenance_durations (deque[float], maxlen=10) # Rolling history of completed
│                                    # run_nightly_maintenance_routine() durations (seconds), fed
│                                    # by the scheduled 03:00 UTC task, /admin Execute Nightly
│                                    # Maintenance, and the deferred-optimize path alike. Read by
│                                    # /status and /admin Check Logs (min/avg/max) via
│                                    # qapbot/QBdiscocmdshelper_admin_command.py's
│                                    # format_nightly_maintenance_stats() — empty until this
│                                    # process's own first run, before which those commands fall
│                                    # back to the last run's duration parsed from the log file
│                                    # (find_last_nightly_maintenance_duration()).
└── _maintenance_interaction_check() # CommandTree.interaction_check — blocks commands during
                                     # startup (fully_initialized=False) and maintenance;
                                     # registered via direct assignment (NOT @decorator)

🟩 QBwarsim.py
├── calculate_win_probability()
├── simulate_remaining_attacks()

🟫 qapbot/discord_health.py
├── discord_retry()
├── get_simple_discord_stats() (returns retry statistics)
└── bulk_sync_global_commands() # preserves the Activities Entry Point command across a
                                 # global bulk command sync — see Main Files entry above

🟫 qapbot/web_bridge.py
├── start_web_bridge() / stop_web_bridge()  # no-op unless WEB_BRIDGE_PORT/SECRET configured
├── create_app()                            # builds the aiohttp.web.Application + routes
├── handle_get_clan_config() / handle_post_clan_config()  # admin-reverified, same db_manager
│                                                          # calls the Discord-side UI uses
├── handle_health()                         # unauthenticated liveness check
└── handle_{get,post}_tracker_*()           # /api/tracker/* — see Main Files entry above

🟩 qapbot/ui_tracker.py
├── start_tracker_item() / TrackerItemModal / TrackerDraftView   # /bug, /feature flow
├── build_tracker_embed() / _post_tracker_item() / _refresh_item_message()
├── TrackerItemButton                       # DynamicItem: Edit/Add files/Status/Test cases
├── handle_tracker_upload_message()         # called from QapBot.py's on_message
├── apply_status_change() / TrackerStatusSelectView
├── post_test_cases() / TrackerTestPassButton / TrackerTestFailButton
└── handle_tracker_test_reaction()          # called from QapBot.py's on_raw_reaction_add

🟫 qapbot/mcp/tracker_mcp.py
├── handle_request() / run_stdio_server()   # JSON-RPC 2.0 stdio loop
├── call_tool() / TOOLS                     # 5 tools, dispatched to tracker_bridge_client.py
└── render_item_markdown() / cache_path_for_item()  # .tracker-cache/NNNN/ mirror (plan §6.2)

🟫 qapbot/coc_health.py
├── coc_retry()                    # main wrapper — routes all CoC API exceptions
├── clear_maintenance_detection()  # called at cycle start; resets _maintenance_detected
├── is_maintenance_detected()      # True if coc.Maintenance was seen this cycle
├── set_reconnect_callback()       # registers re-auth hook for session-close recovery
├── reset_cycle_stats()            # clears per-cycle rate-limit counters
└── get_coc_stats()                # returns dict of lifetime + cycle API statistics

## Data & Cache Files

- data/qapbot.db - SQLite database (WAL mode) - PRIMARY data store for all persistent data
  * War history, user accounts, subscriptions, clan families, server config, notification state, leaderboard messages, clan name cache, CWL round tracking
  * data/qapbot_history.db is ATTACHed to the same connection as schema `history`; the 4
    time-series tables (war_attacks, war_summary, cwl_league_groups, cwl_league_rounds) are
    mirrored there as part of the hot/history DB split
  * All tables (22): war_attacks, war_summary, clans, clan_families, clan_family_members, users, user_players, user_buddies, guild_config, guild_member_families, guild_member_clans, guild_welcome_families, guild_welcome_clans, guild_clan_roles, subscriptions, notification_state, channel_notification_state, leaderboard_messages, cwl_league_groups, cwl_league_rounds, player_name_index, bot_metadata
- data/logs/ - Rotating log files (daily)
- data/temp/ - JSON war data files for active wars (format: {CLAN_TAG}_{OPPONENT_TAG}_{YYYYMMDDHHMM}_war_data.json)
  * Managed by unified war file lifecycle (manage_war_files)
  * Current war protected from premature finalization
  * Old wars automatically finalized and moved to archive
  * Duplicate temp files deleted when archive exists and JSON content is identical
  * Wars ended >24h ago are skipped at save-time (preventing endless stale temp creation)
- archive/ - JSON war data files for completed wars (archived after finalization)
  * Archive file format: {CLAN_TAG}_{OPPONENT_TAG}_{YYYYMMDDHHMM}_war_data.json
  * Duplicate archives prevented via existence check
  * Serves as backup for late attack verification
- data/maindata/ - Legacy JSON files (kept for reference, DB is source of truth)
  * These files are NO LONGER actively read/written by the bot
  * All data persisted via write-through to SQLite database
- data/history/ - Empty (war history migrated to SQLite database)

---

This updated structure reflects the database-only, write-through, cache-centric architecture after the migration from JSON/CSV to SQLite. All persistent data is stored in `data/qapbot.db`, with in-memory caches providing fast access. Business logic never directly accesses the database — all operations go through CACHE methods which handle write-through persistence.