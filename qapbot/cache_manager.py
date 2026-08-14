"""
Centralized cache management for QapBot runtime data.

This module is the single source of truth for all in-memory and persistent data, including:
- Clan name mappings and war information
- Discord message ID tracking for spam prevention and update logic
- Channel subscriptions for automatic leaderboard posting
- Clan families and user accounts
- Current war statistics and historical war records

All cache data uses write-through persistence to SQLite database and is loaded on startup. All business logic modules interact with the cache manager for data access and updates.

Performance Benefits:
- Reduces redundant data processing and network requests
- Improves responsiveness by caching expensive-to-fetch data
- Enables efficient change detection using content hashes

Business Rules:
- All cache mutations use write-through persistence (immediate DB commit)
- Message ID tracking uses a unified structure for split/merged messages
- Data integrity is prioritized over feature completeness
- All leaderboard and war info posts use unified message ID tracking

Error Handling:
- Strict validation and error logging for all database I/O
- SystemExit is raised on critical data corruption to prevent further damage
- Defensive programming patterns for external dependencies

Thread Safety:
- Designed for single-threaded async usage within Discord.py event loop

Example:
    CACHE.leaderboard_messages["L2J0C0PY_08_2025_stars_123456"] = {
        "message_ids": "987654321",
        "content_hash": "abc123..."
    }
    await CACHE.persist_user(user_id)  # Write-through to database
"""

import asyncio
import os
import json
import logging
import hashlib
import time
from collections import deque
from datetime import datetime, timedelta, timezone as _dt_timezone
from typing import Dict, Any, Tuple, List, Optional, Set, cast, TYPE_CHECKING
import discord
import coc  # type: ignore[import-untyped]
import re
import glob

if TYPE_CHECKING:
    from qapbot.db_manager import WarHistoryDB
from qapbot.constants import SECONDS_PER_HOUR, WAR_UPDATE_LEAGUES, normalize_cwl_season
from qapbot.config import CONFIG
from qapbot.coc_health import coc_retry
from qapbot.coc_cache import CoCClanCache

# Maximum number of full war payload dicts kept in temp_war_objects at any time.
# Each entry is ~61 KB (measured); 100 000 entries ≈ 6.1 GB (server-machine has 10 GB RAM, 9 GB
# reserved for the bot). This covers the full CWL working set (~100k active wars).
# Single-entry FIFO eviction is optimal: uniform polling means no cold entries exist,
# so LRU offers no advantage; bulk eviction would only create more cache misses.
# War-ended evictions still fire independently; this cap is a safety net.
MAX_TEMP_WAR_OBJECTS = 100_000
_MAX_TEMP_WAR_OBJECTS = MAX_TEMP_WAR_OBJECTS


def _de_n(v: int) -> str:
    """Format integer with European thousand separator (period)."""
    return f"{v:,}".replace(",", ".")


class CacheManager:
    """
    Centralized cache manager providing single-source-of-truth for all QapBot runtime and persistent data.
    
    This class manages all cached data including Discord message IDs, content hashes,
    clan names, war information, subscriptions, families, user accounts, and war stats/history.
    It provides automatic persistence, atomic update detection, and unified message tracking.
    
    Cache Categories:
        - leaderboard_messages: Unified Discord message IDs and content hashes for all posts
        - clan_name_cache: Mapping of clan tags to human-readable names
        - subscriptions: Channel subscriptions for automatic leaderboard posting
        - clan_families: Family tags and member clans
        - user_accounts: Discord user accounts and linked player tags with verification status
        - temp_war_stats: Current war stats for all clans
        - clan_history: Historical war records for all clans
    
    Performance Optimization:
        - Unified message structure combines message IDs and content hashes for atomic updates
        - Content hashes eliminate unnecessary Discord API calls by comparing SHA-256 hashes
        - All leaderboard and war info posts use unified message ID tracking
        - Token bucket rate limiting for CoC API calls (10 req/sec per key)
    
    Persistence:
        - All cache data uses write-through persistence to SQLite database
        - Every mutation immediately commits to DB via set_*/persist_*/delete_* methods
        - Data is loaded from database on initialization for continuity across bot restarts
        - Temp war stats are the only data using JSON files (data/temp/)
    
    Lazy-Loading Strategy (2026-02-07):
        READ-ONLY Caches (lazy-loading enabled):
        - clan_history: Loaded on-demand from database via get_clan_history()
        
        READ-WRITE Caches (write-through persistence):
        - subscriptions, user_accounts, notification_state, server_config, clan_families,
          leaderboard_messages, clan_name_cache
        - Every mutation writes through to database immediately
        - Empty cache on startup = FATAL ERROR (prevents data loss)
    
    Thread Safety:
        - Designed for single-threaded async usage within Discord.py event loop
    
    Rate Limiting:
        - Uses coc.Client's BatchThrottler (key_count=10, throttle_limit=100)
        - 10 API keys @ 10 req/sec = 100 total req/sec
    
    Example:
        CACHE.leaderboard_messages["L2J0C0PY_08_2025_stars_123456"] = {
            "message_ids": "987654321",
            "content_hash": "abc123..."
        }
        await CACHE.persist_user(user_id)  # Write-through to database
    """

    # Class-level default for the data-loss guard (see __init__) — ensures even
    # instances created without __init__ (e.g. test fixtures using __new__) fail
    # safe (writes refused) instead of raising AttributeError.
    users_loaded: bool = False

    def __init__(self) -> None:
        """
        Initialize cache manager with empty data structures.
        
        Note:
            Data must be explicitly loaded using load_all() after initialization.
            This design allows for controlled loading during bot startup.
        """
        # Core clan data (tag and full clan metadata dict)
        self.clan_name_cache: Dict[str, Dict[str, Any]] = {}
        # Channel subscriptions for automatic leaderboard posting (guild_id -> channel_id -> subscription_list)
        self.subscriptions: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        # Discord message tracking with timestamp-based unique key and metadata
        self.leaderboard_messages: Dict[str, Dict[str, Any]] = {}
        # Clan families
        self.clan_families: Dict[str, Dict[str, Any]] = {}  # family_tag -> {"name": str, "clans": List[str]}
        # User accounts with discord user_id to dict with display_name and players list
        self.user_accounts: Dict[str, Dict[str, Any]] = {}  # user_id -> {"display_name": str, "notification_settings": {...}, "players": List[{"player_tag": str, "player_name": str, "verified": bool}]}
        # Data-loss guard: False until load_user_accounts() has successfully populated
        # user_accounts from the DB. All user write-through paths refuse to run while
        # False — a write based on a not-yet-loaded cache would persist an empty/partial
        # entry over real DB rows (save_user() replaces the whole players list, so a
        # skeleton entry with players=[] hard-deletes every linked account — exactly the
        # 2026-08-08 prod incident where a button click during startup wiped 5 links).
        self.users_loaded: bool = False
        # Notification state tracking to prevent duplicate war reminders
        self.notification_state: Dict[str, Dict[str, Any]] = {}  # war_id -> {"notified_players": {player_tag: {...}}}
        # Server configuration for role management, welcome message, and war notifications (guild_id -> config)
        self.server_config: Dict[str, Dict[str, Any]] = {}  # guild_id -> {"role_system_enabled": bool, "newbie_role_id": str, "member_role_id": str, "member_clans": List[str], "member_families": List[str], "registration_channel_id": str, "registration_message_enabled": bool, "registration_message_id": str, "war_notification_channel_id": str, "channel_war_notifications_enabled": bool}
        # Temporary war stats and clan history for in-memory management
        self.temp_war_stats: Dict[str, Dict[str, Any]] = {}  # clan_tag -> player_tag -> stats
        # Lightweight war metadata cached alongside temp_war_stats (state, start_time, end_time, type, is_cwl, war_tag, opponent_tag)
        # Populated at startup by load_all_temp_war_stats() and updated by set_temp_war_stats() / save_war_object().
        # Used by manage_war_files() and _scan_orphans() to avoid re-reading JSON files from disk.
        self.temp_war_metadata: Dict[str, Dict[str, Any]] = {}  # clan_tag -> {"state", "start_time", "end_time", "filepath", "type", "is_cwl", "war_tag", "opponent_tag"}
        # In-memory cache of the full war JSON payload (the dict written to temp files).
        # Written by save_war_object(), read by get_current_war_data().
        # Eliminates re-reading JSON from disk for active war lookups.
        # Cleared when a clan's war ends (set_temp_war_stats with empty stats).
        self.temp_war_objects: Dict[str, Dict[str, Any]] = {}  # clan_tag -> full war payload dict
        # Fast O(1) set of clan_tags currently in "in_war" state.
        # Maintained by load_all_temp_war_stats(), save_war_object(), and set_temp_war_stats().
        # Allows _get_active_wars() to skip iterating all 18k+ temp_war_metadata entries.
        self.in_war_clan_tags: Set[str] = set()
        # Raw temp file count from the most recent disk scan (startup or cycle pre-scan).
        # Updated by load_all_temp_war_stats() and the QapBot.py cycle pre-scan.
        # Used by get_war_file_stats() so "total" matches actual disk files rather than
        # the deduplicated per-clan metadata count (one entry per clan, not per file).
        self.temp_total_file_count: int = 0
        self.clan_history: Dict[str, List[Any]] = {}  # clan_tag -> list of war records
        # Filtered history cache for performance optimization (in-memory only)
        self.history_cache: Dict[Tuple[str, Optional[int], Optional[int], Optional[str]], List[Dict[str, Any]]] = {}  # (clan_tag, month, year, cwl_season) -> filtered records
        # CoC API cache for reducing redundant clan fetches (in-memory only, no persistence)
        self.coc_clan_cache = CoCClanCache(soft_ttl_seconds=280, hard_ttl_seconds=600)  # 4m40s soft, 10m hard
        # Set reference for player info updates
        self.coc_clan_cache.cache_manager = self  # type: ignore[assignment]
        # CoC API client (will be initialized in startup_login with rate limiting)
        self.coc_client: Optional[coc.Client] = None
        # Database manager for persistent storage (initialized in startup_login if enabled)
        self.db_manager: Optional['WarHistoryDB'] = None  # type: ignore[name-defined]
        # ── CWL caches (in-memory only, survive across cycles within session) ──
        # League war cache: war_tag → (war_obj, fetch_unix_timestamp, state_str)
        # war_ended entries expire after _LEAGUE_WAR_CACHE_TTL seconds to prevent
        # unbounded memory growth (each ClanWar holds 30-100 ClanWarMember objects).
        # Active states expire via max_age at call site.
        self._league_war_cache: Dict[str, Tuple[Any, float, str]] = {}
        # League group cache: clan_tag → (league_group_obj, fetch_unix_timestamp, active_round_count)
        # Invalidated when active_round_count increases (new round started).
        self._league_group_cache: Dict[str, Tuple[Any, float, int]] = {}
        # Per-clan shortcut: clan_tag → war_tag of the last known active CWL war.
        # Allows 1-call refresh instead of iterating all round war tags.
        self.clan_active_cwl_war: Dict[str, str] = {}
        # CWL war tag recovery queue: populated from data/missing_cwl_war_tags.txt on startup.
        # Each entry is a war_tag string (e.g. '#ABC123').  Consumed in batches per update cycle.
        self.pending_cwl_recovery: deque[str] = deque()
        self.last_cwl_recovery_summary: str = ""  # Summary of last completed recovery batch
        # "Manage Enrollment" web-Activity screen picker (CWL_ROSTER_PLANNING_PLAN.md, 2026-08-10):
        # (guild_id_str, discord_user_id_str) -> "clan_config" | "enrollment", set by whichever
        # Discord button most recently fired LAUNCH_ACTIVITY, read (non-destructively, since
        # 2026-08-14 — see web_bridge.py's handler docstring for why a plain pop broke Discord's
        # "pop out" button) by the bridge's GET /api/cwl/screen on the Activity's initial fetch.
        # Deliberately ephemeral/unpersisted (an in-memory launch hint, not real state) — a bot
        # restart just falls back to the "clan_config" default, same as an entry that was never
        # recorded at all.
        self.pending_cwl_activity_screen: Dict[Tuple[str, str], str] = {}
        self.last_db_maintenance: Optional[datetime] = None  # UTC timestamp of last nightly DB maintenance run
        self.last_history_migration: Optional[datetime] = None  # UTC timestamp of last monthly hot->history DB migration
        # Cross-cycle datetime parse cache for the clan categorization loop.
        # Maps clan_tag -> (raw_iso_string, parsed_datetime). Avoids 12K fromisoformat()
        # calls per cycle by only re-parsing when the stored ISO string changes.
        self.clan_dt_cache: Dict[str, Tuple[str, datetime]] = {}
        # Fast lookup for /whois player name searches.  Populated at startup from
        # player_name_index table; updated incrementally by each war write cycle.
        # Maps player_tag -> most recently seen player_name (~125 K entries, ~20 MB).
        self.player_name_index: Dict[str, str] = {}

        # Per-cycle counters.  Reset at the start of each update cycle by
        # QapBot.main() and read at the end for the [CYCLE-SUMMARY] log line.
        # Keys are added on first use — no fixed schema needed.
        self.cycle_stats: Dict[str, int] = {}

        # Lifetime counters — same keys as cycle_stats, never reset, not persisted.
        # Accumulates from bot start until shutdown.  Read by /status command.
        self.lifetime_stats: Dict[str, int] = {}

    def record_cycle_stat(self, key: str, n: int = 1) -> None:
        """Increment a named counter in both the current cycle's and lifetime stats dicts."""
        self.cycle_stats[key] = self.cycle_stats.get(key, 0) + n
        self.lifetime_stats[key] = self.lifetime_stats.get(key, 0) + n

    def reset_cycle_stats(self) -> None:
        """Clear per-cycle counters.  Lifetime counters are unaffected.
        Gauge values (throttle_backlog) are preserved so /status and LIFETIME
        summary always reflect the last computed value rather than defaulting to 0."""
        _preserved = {
            k: self.cycle_stats[k]
            for k in ("throttle_backlog",)
            if k in self.cycle_stats
        }
        self.cycle_stats.clear()
        self.cycle_stats.update(_preserved)

    def format_lifetime_summary(self) -> str:
        """Return lifetime totals for /status — same order as log lines, with split lines for readability."""
        s = self.lifetime_stats

        api_ok  = s.get("api_fetched", 0)
        api_no_war = s.get("api_no_war", 0)
        fail_buckets = {k[len("api_fail:"):]: v for k, v in s.items() if k.startswith("api_fail:")}
        api_err = sum(fail_buckets.values())
        if fail_buckets and api_err > 0:
            bucket_str = "  ".join(f"{r}={_de_n(n)}" for r, n in sorted(fail_buckets.items()))
            api_line = f"API fetches    : ok={_de_n(api_ok)}  no_war={_de_n(api_no_war)}  failed={_de_n(api_err)}  ({bucket_str})"
        else:
            api_line = f"API fetches    : ok={_de_n(api_ok)}  no_war={_de_n(api_no_war)}  failed={_de_n(api_err)}"

        written  = s.get("temp_written",               0)
        new_f    = s.get("temp_new",                   0)
        upd_f    = s.get("temp_updated",               0)
        skip_f   = s.get("save_skip_finalized",        0)
        skip_s   = s.get("save_skip_stale",            0)
        skip_fa  = s.get("save_skip_friendly",         0)
        skip_nc  = s.get("save_skip_no_clan",          0)
        written_detail = f"written={_de_n(written)} (new={_de_n(new_f)}, upd={_de_n(upd_f)})" if new_f or upd_f else f"written={_de_n(written)}"
        skip_nc_str = f"  skip_no_clan={_de_n(skip_nc)}" if skip_nc else ""
        tmp_line = f"Temp writes    : {written_detail}  skip_finalized={_de_n(skip_f)}  skip_stale={_de_n(skip_s)}  skip_friendly={_de_n(skip_fa)}{skip_nc_str}"

        fo_war     = s.get("mwf_finalize_regular_war_ended",  0)
        fo_orphan  = s.get("mwf_finalize_orphan",   0)
        cwl_skip   = s.get("mwf_cwl_orphan_skip",   0)
        guard_skip = s.get("mwf_in_war_guard_skip", 0)
        prep_rm    = s.get("mwf_stale_prep_removed",0)
        not_ready  = s.get("mwf_skip_not_ready",    0)
        fin_line   = (
            f"Finalization   : regular_war_ended={_de_n(fo_war)}  orphan={_de_n(fo_orphan)}  cwl_skip={_de_n(cwl_skip)}  in_war_guard_skip={_de_n(guard_skip)}\n"
            f"Finalization   : stale_prep_rm={_de_n(prep_rm)}  not_ready={_de_n(not_ready)}"
        )

        new_war    = s.get("pwh_new_war_finalized",    0)
        late_upd   = s.get("pwh_late_attacks_updated_DB", 0)
        late_none  = s.get("pwh_late_check_no_change", 0)
        dup_skip   = s.get("pwh_duplicate_skip",       0)
        arch_diff  = s.get("pwh_archive_differs",      0)
        regress    = s.get("pwh_regression_guard",     0)
        incomplete = s.get("pwh_incomplete_archived",  0)
        hist_line  = (
            f"History        : new={_de_n(new_war)}  (late_updated_DB={_de_n(late_upd)}  late_no_change={_de_n(late_none)}  duplicate_skip={_de_n(dup_skip)}\n"
            f"History        : archive_differs={_de_n(arch_diff)}  regression_guard={_de_n(regress)}  incomplete={_de_n(incomplete)})"
        )

        db_app  = s.get("db_appends", 0)
        db_upd  = s.get("db_updates", 0)
        db_line = f"DB flush       : appends={_de_n(db_app)}  updates={_de_n(db_upd)}"

        p3b_done = s.get("p3b_passive_processed", 0)
        p3b_skip = s.get("p3b_passive_skipped",   0)
        p3b_line = f"Phase-3B       : passive_processed={_de_n(p3b_done)}  passive_skipped={_de_n(p3b_skip)}"

        # throttle_backlog is a gauge (set per-cycle, not accumulated), so read from cycle_stats.
        backlog = self.cycle_stats.get("throttle_backlog", 0)
        throttle_line = f"Throttle       : backlog={_de_n(backlog)}"

        return "\n".join([api_line, tmp_line, fin_line, hist_line, db_line, p3b_line, throttle_line])

    def _format_stats_log_lines(self, stats: Dict[str, int], prefix: str) -> list[str]:
        """Shared log formatter for both CYCLE-SUMMARY and LIFETIME log output.

        Args:
            stats:  Either self.cycle_stats or self.lifetime_stats.
            prefix: Label prepended to every line, e.g. '[CYCLE-SUMMARY]' or '[LIFETIME]'.
        """
        divider = f"{prefix} " + "\u2500" * 57
        p = f"{prefix}  "
        lines: list[str] = [divider]

        api_ok  = stats.get("api_fetched", 0)
        api_no_war = stats.get("api_no_war", 0)
        fail_buckets = {k[len("api_fail:"):]: v for k, v in stats.items() if k.startswith("api_fail:")}
        api_err = sum(fail_buckets.values())
        if fail_buckets and api_err > 1:
            bucket_str = "  ".join(f"{r}={n}" for r, n in sorted(fail_buckets.items()))
            lines.append(f"{p} API fetches    : ok={api_ok}  no_war={api_no_war}  failed={api_err}  ({bucket_str})")
        else:
            lines.append(f"{p} API fetches    : ok={api_ok}  no_war={api_no_war}  failed={api_err}")

        written  = stats.get("temp_written",               0)
        new_f    = stats.get("temp_new",                   0)
        upd_f    = stats.get("temp_updated",               0)
        skip_f   = stats.get("save_skip_finalized",        0)
        skip_s   = stats.get("save_skip_stale",            0)
        skip_fa  = stats.get("save_skip_friendly",         0)
        skip_nc  = stats.get("save_skip_no_clan",          0)
        written_detail = f"written={written} (new={new_f}, upd={upd_f})" if new_f or upd_f else f"written={written}"
        skip_nc_str = f"  skip_no_clan={skip_nc}" if skip_nc else ""
        lines.append(f"{p} Temp writes    : {written_detail}  skip_finalized={skip_f}  skip_stale={skip_s}  skip_friendly={skip_fa}{skip_nc_str}")

        fo_war     = stats.get("mwf_finalize_regular_war_ended",  0)
        fo_orphan  = stats.get("mwf_finalize_orphan",   0)
        cwl_skip   = stats.get("mwf_cwl_orphan_skip",   0)
        guard_skip = stats.get("mwf_in_war_guard_skip", 0)
        prep_rm    = stats.get("mwf_stale_prep_removed",0)
        not_ready  = stats.get("mwf_skip_not_ready",    0)
        lines.append(f"{p} Finalization   : regular_war_ended={fo_war}  orphan={fo_orphan}  cwl_skip={cwl_skip}  in_war_guard_skip={guard_skip}  stale_prep_rm={prep_rm}  not_ready={not_ready}")

        new_war    = stats.get("pwh_new_war_finalized",    0)
        late_upd   = stats.get("pwh_late_attacks_updated_DB", 0)
        late_none  = stats.get("pwh_late_check_no_change", 0)
        dup_skip   = stats.get("pwh_duplicate_skip",       0)
        arch_diff  = stats.get("pwh_archive_differs",      0)
        regress    = stats.get("pwh_regression_guard",     0)
        incomplete = stats.get("pwh_incomplete_archived",  0)
        lines.append(f"{p} History        : new={new_war}  late_updated_DB={late_upd}  late_no_change={late_none}  duplicate_skip={dup_skip}  archive_differs={arch_diff}  regression_guard={regress}  incomplete={incomplete}")

        db_app = stats.get("db_appends", 0)
        db_upd = stats.get("db_updates", 0)
        lines.append(f"{p} DB flush       : appends={db_app}  updates={db_upd}")

        p3b_done = stats.get("p3b_passive_processed", 0)
        p3b_skip = stats.get("p3b_passive_skipped",   0)
        lines.append(f"{p} Phase-3B       : passive_processed={p3b_done}  passive_skipped={p3b_skip}")

        throttle_backlog = stats.get("throttle_backlog") if "throttle_backlog" in stats else self.cycle_stats.get("throttle_backlog", 0)
        lines.append(f"{p} Throttle       : backlog={throttle_backlog}")

        lines.append(divider)
        return lines

    def format_lifetime_log(self) -> list[str]:
        """Return lifetime totals as log-ready lines identical in structure to format_cycle_summary()."""
        return self._format_stats_log_lines(self.lifetime_stats, "[LIFETIME]")

    def format_cycle_summary(self) -> list[str]:
        """Return the cycle summary as a list of log-ready strings (one per line)."""
        return self._format_stats_log_lines(self.cycle_stats, "[CYCLE-SUMMARY]")

    async def load_clan_name_cache(self) -> None:
        """
        Load clan name cache from database.
        
        Raises:
            RuntimeError: If database manager not initialized
            SystemExit: On database loading error
        """
        if not self.db_manager:
            raise RuntimeError("Database manager not initialized")
        
        try:
            # Load full clan metadata dict (clan_tag -> {name, has_active_subscriptions, ...})
            self.clan_name_cache = await self.db_manager.get_all_clans_dict()
            logging.info(f"[DB-READ] Loaded {len(self.clan_name_cache)} clans from database")
        except Exception as e:
            logging.error(f"[DB-READ] Failed to load clans from database: {e}")
            logging.error("FATAL: Cannot load clan data - terminating")
            raise SystemExit(1)

    async def load_player_name_index(self) -> None:
        """Load the player_name_index table into CACHE.player_name_index at startup.

        With ~125 K rows this runs in < 100 ms and replaces slow per-query
        ``LIKE '%substring%'`` full table scans for /whois name searches.
        """
        if not self.db_manager:
            raise RuntimeError("Database manager not initialized")
        try:
            self.player_name_index = await asyncio.to_thread(
                self.db_manager.load_player_name_index_sync
            )
            logging.info(
                f"[DB-READ] Loaded {len(self.player_name_index):,} entries into player_name_index"
            )
        except Exception as e:
            logging.warning(f"[DB-READ] Failed to load player_name_index: {e} — /whois name search unavailable")
            self.player_name_index = {}

    def search_player_names(
        self, query: str, limit: int = 25
    ) -> List[Dict[str, str]]:
        """In-memory O(n) search over player_name_index for /whois.

        Returns up to ``limit`` (max 25) dicts with 'player_tag' / 'player_name',
        sorted alphabetically by name.  Runs synchronously in the Discord
        coroutine — no asyncio.to_thread needed.
        """
        if not query:
            return []
        needle = query.lower()
        cap = min(limit, 25)
        matches = [
            {"player_tag": tag, "player_name": name}
            for tag, name in self.player_name_index.items()
            if needle in name.lower()
        ]
        matches.sort(key=lambda x: x["player_name"].lower())
        return matches[:cap]

    async def load_subscriptions(self) -> None:
        try:
            subs_from_db = await self.db_manager.get_all_subscriptions_for_cache()  # type: ignore[union-attr]
            self.subscriptions = subs_from_db
            total = sum(len(channels) for channels in subs_from_db.values())
            logging.info(f"[DB-READ] Loaded subscriptions for {total} channels across {len(subs_from_db)} guilds from database")
        except Exception as e:
            logging.error(f"Failed to load subscriptions from database: {e}")
            self.subscriptions = {}
            logging.info("Initialized empty subscriptions (will create on first save)")

    async def set_subscriptions_for_channel(
        self,
        guild_id: str,
        channel_id: str,
        subscriptions: List[Dict[str, Any]]
    ) -> None:
        """
        Set subscriptions for a channel with immediate database persistence (write-through).
        
        Args:
            guild_id: Discord guild ID
            channel_id: Discord channel ID
            subscriptions: List of subscription dicts
        
        Raises:
            Exception: If database write fails
        """
        try:
            # Update in-memory cache
            if guild_id not in self.subscriptions:
                self.subscriptions[guild_id] = {}
            self.subscriptions[guild_id][channel_id] = subscriptions
            
            # Immediately persist to database
            await self.db_manager.save_subscriptions_for_channel(  # type: ignore[union-attr]
                guild_id, channel_id, subscriptions
            )
            logging.debug(f"[DB-WRITE-THROUGH] Persisted {len(subscriptions)} subscriptions for guild {guild_id}, channel {channel_id}")
        except Exception as e:
            logging.error(f"[DB-WRITE-THROUGH] Failed to persist subscriptions for guild {guild_id}, channel {channel_id}: {e}")
            raise

    async def delete_subscriptions_for_channel(
        self,
        guild_id: str,
        channel_id: str
    ) -> None:
        """
        Delete all subscriptions for a channel with immediate database persistence (write-through).
        
        Args:
            guild_id: Discord guild ID
            channel_id: Discord channel ID
        
        Raises:
            Exception: If database delete fails
        """
        try:
            # Remove from in-memory cache
            if guild_id in self.subscriptions:
                if channel_id in self.subscriptions[guild_id]:
                    del self.subscriptions[guild_id][channel_id]
                
                # Clean up empty guild entries
                if not self.subscriptions[guild_id]:
                    del self.subscriptions[guild_id]
            
            # Immediately delete from database
            await self.db_manager.delete_subscriptions_for_channel(guild_id, channel_id)  # type: ignore[union-attr]
            logging.debug(f"[DB-DELETE-THROUGH] Removed subscriptions for guild {guild_id}, channel {channel_id}")
        except Exception as e:
            logging.error(f"[DB-DELETE-THROUGH] Failed to delete subscriptions for guild {guild_id}, channel {channel_id}: {e}")
            raise

    async def delete_subscriptions_for_guild(self, guild_id: str) -> None:
        """
        Delete all subscriptions for a guild with immediate database persistence (write-through).
        
        Args:
            guild_id: Discord guild ID
        
        Raises:
            Exception: If database delete fails
        """
        try:
            # Remove from in-memory cache
            if guild_id in self.subscriptions:
                del self.subscriptions[guild_id]
            
            # Immediately delete from database
            await self.db_manager.delete_subscriptions_for_guild(guild_id)  # type: ignore[union-attr]
            logging.debug(f"[DB-DELETE-THROUGH] Removed all subscriptions for guild {guild_id}")
        except Exception as e:
            logging.error(f"[DB-DELETE-THROUGH] Failed to delete subscriptions for guild {guild_id}: {e}")
            raise

    def _calculate_subscription_status(self, clan_tag: str) -> bool:
        """
        Check if a clan is "tracked" — has any active relationship to a guild.
        
        A clan is tracked (has_active_subscriptions=True) when ANY of:
        - Direct channel subscription
        - Member of a family that is subscribed to a channel
        - Listed in a guild's member_clans
        - Member of a family listed in a guild's member_families
        
        Tracked clans receive war updates every polling cycle (Tier 1).
        
        Args:
            clan_tag: Clan tag to check (e.g., "#2C9UR9GJY")
        
        Returns:
            True if clan is tracked, False otherwise
        """
        # Check direct subscriptions
        for guild_data in self.subscriptions.values():
            for channel_subs in guild_data.values():
                for sub in channel_subs:
                    if sub.get('clan_tag') == clan_tag:
                        return True
        
        # Check family subscriptions
        for family_tag, family_data in self.clan_families.items():
            if clan_tag in family_data.get('clans', []):
                # This clan is in a family - check if family is subscribed
                for guild_data in self.subscriptions.values():
                    for channel_subs in guild_data.values():
                        for sub in channel_subs:
                            if sub.get('clan_tag') == family_tag:
                                return True
        
        # Check guild member_clans
        for config in getattr(self, 'server_config', {}).values():
            if clan_tag in config.get('member_clans', []):
                return True
        
        # Check guild member_families → expand to member clans
        for config in getattr(self, 'server_config', {}).values():
            for fam_tag in config.get('member_families', []):
                fam_data = self.clan_families.get(fam_tag)
                if fam_data and clan_tag in fam_data.get('clans', []):
                    return True
        
        return False

    # TTL for CWL caches — prevents unbounded memory growth from
    # accumulated coc.ClanWar and ClanWarLeagueGroup objects.
    _LEAGUE_WAR_CACHE_TTL: float = 7200.0   # 2 hours — enough for all CWL processing
    _LEAGUE_GROUP_CACHE_TTL: float = 3600.0  # 1 hour

    # Hard size caps — prevent memory explosion during CWL season when hundreds of
    # orphaned wars are fetched per cycle.  TTL eviction alone cannot bound the PEAK
    # size within a single cycle; these caps guarantee a fixed upper bound.
    # At ~200 KB per coc.ClanWar object: 1000 entries ≈ 200 MB max.
    _LEAGUE_WAR_CACHE_MAX_ENTRIES: int = 1000
    _LEAGUE_GROUP_CACHE_MAX_ENTRIES: int = 200

    @staticmethod
    def _calculate_track_war_updates(clan_data: Dict[str, Any], is_tracked: bool) -> bool:
        """
        Determine whether a clan should receive any war updates.

        Three-tier model:
          Tier 1 (actively tracked / has_active_subscriptions=True): always True
          Tier 2 (passively tracked, Master III through Legend): True → 22h polling
          Tier 3 (passively tracked, below Master III or unknown league): False → no polling
        """
        if is_tracked:
            return True
        league = clan_data.get("war_league")
        if league and league in WAR_UPDATE_LEAGUES:
            return True
        return False

    async def update_clan_subscription_status(self, clan_tag: str) -> None:
        """
        Update has_active_subscriptions flag for a single clan.
        
        Call this after subscribe/unsubscribe operations.
        
        Args:
            clan_tag: Clan tag to update (e.g., "#2C9UR9GJY")
        """
        clan_data = self.clan_name_cache.get(clan_tag)
        if not clan_data:
            return
        
        # Clan data must be in new format by now
        if not isinstance(clan_data, dict):  # type: ignore[misc]
            logging.error(f"[SUBSCRIPTION-STATUS] Clan {clan_tag} still in old format - skipping")
            return
        
        new_status = self._calculate_subscription_status(clan_tag)
        dirty = False
        if clan_data.get('has_active_subscriptions') != new_status:  # type: ignore[union-attr]
            clan_data['has_active_subscriptions'] = new_status
            dirty = True
            logging.info(f"[SUBSCRIPTION-STATUS] {clan_tag} subscription status: {new_status}")
        
        # Recalculate track_war_updates (one-way ratchet: once True, never reverts to False).
        # Promotions: subscribed/member clan (Tier 1) or league entered Master III+ (Tier 2).
        # Unsubscribing, channel deletion, or league demotion never clears the flag.
        new_track = self._calculate_track_war_updates(clan_data, new_status)
        if new_track and not clan_data.get('track_war_updates'):
            clan_data['track_war_updates'] = True
            dirty = True
            logging.info(f"[SUBSCRIPTION-STATUS] {clan_tag} track_war_updates → True")
        
        if dirty:
            await self.persist_clan(clan_tag)

    async def update_all_clan_subscription_statuses(self) -> None:
        """
        Recalculate subscription status and track_war_updates for ALL clans.
        
        Use after:
        - Loading cache on startup
        - Family membership changes
        - Bulk subscription operations
        - Guild member_clans / member_families changes
        
        Performance: builds a tracked-tag set in O(subscriptions + guild_configs) once,
        then checks each clan in O(1).  Changed clans are persisted in bulk DB
        transactions.
        """
        # --- Step 1: build tracked tag set in one pass ---
        tracked_tags: set[str] = set()

        # 1a. Channel subscriptions (direct)
        for guild_data in self.subscriptions.values():
            for channel_subs in guild_data.values():
                for sub in channel_subs:
                    tag = sub.get('clan_tag')
                    if tag:
                        tracked_tags.add(tag)

        # 1b. Guild member_clans
        for config in getattr(self, 'server_config', {}).values():
            for clan_tag in config.get('member_clans', []):
                tracked_tags.add(clan_tag)
            # 1c. Guild member_families → expand to member clans
            for fam_tag in config.get('member_families', []):
                tracked_tags.add(fam_tag)  # family tag itself (subscription matching)
                fam_data = self.clan_families.get(fam_tag)
                if fam_data:
                    for member_tag in fam_data.get('clans', []):
                        tracked_tags.add(member_tag)

        # 1d. Expand family subscriptions: if a family tag is tracked, all its member clans are too
        for family_tag, family_data in self.clan_families.items():
            if family_tag in tracked_tags:
                for member_tag in family_data.get('clans', []):
                    tracked_tags.add(member_tag)

        # --- Step 2: update in-memory cache, collect changes ---
        sub_changed: list[tuple[bool, str]] = []      # (new_status, clan_tag) for bulk DB write
        track_changed: list[tuple[str, Dict[str, Any]]] = []     # (clan_tag, clan_data) for individual persist
        for clan_tag, clan_data in self.clan_name_cache.items():
            if not isinstance(clan_data, dict):  # type: ignore[misc]
                logging.warning(f"[SUBSCRIPTION-STATUS] Clan {clan_tag} in old format - skipping")
                continue
            new_status = clan_tag in tracked_tags
            old_status = clan_data.get('has_active_subscriptions', False)  # type: ignore[union-attr]
            if old_status != new_status:
                clan_data['has_active_subscriptions'] = new_status  # type: ignore[index]
                sub_changed.append((new_status, clan_tag))

            # Recalculate track_war_updates (one-way ratchet: only upgrade, never downgrade).
            new_track = self._calculate_track_war_updates(clan_data, new_status)
            if new_track and not clan_data.get('track_war_updates'):
                clan_data['track_war_updates'] = True
                track_changed.append((clan_tag, clan_data))

        # --- Step 3: persist all changes in bulk DB transactions ---
        if sub_changed:
            await self.db_manager.bulk_update_clan_subscription_statuses(sub_changed)  # type: ignore[union-attr]
            logging.info(f"[SUBSCRIPTION-STATUS] Updated {len(sub_changed)} clan subscription statuses")
        else:
            logging.debug("[SUBSCRIPTION-STATUS] All clan subscription statuses up-to-date")

        if track_changed:
            track_tags = [clan_tag for clan_tag, _ in track_changed]
            await self.db_manager.bulk_update_clan_track_war_updates(track_tags)  # type: ignore[union-attr]
            logging.info(f"[TRACK-WAR-UPDATES] Updated {len(track_changed)} clan track_war_updates flags")

    def get_clan_name(self, clan_tag: str, default: Optional[str] = "Unknown") -> Optional[str]:
        """
        Safely get clan name from cache with lazy-loading, handling both old (string) and new (dict) formats.
        
        Defensive lazy-loading: If entire cache is empty (cache invalidation bug),
        automatically reload from file. This prevents the same issue that affected
        clan_history during CWL war finalization.
        
        Args:
            clan_tag: Clan tag (e.g., "#L2J0C0PY")
            default: Default value if clan not found (can be None)
            
        Returns:
            Clan name string or None if not found and default is None
            
        Note:
            This is safe for clan_name_cache because it's read-mostly (rarely modified).
            NOT applied to write-heavy caches (user_accounts, subscriptions, etc.) where
            empty cache should fail loudly to prevent data loss.
        """
        # Defensive: If cache completely empty, reload from file
        if not self.clan_name_cache:
            logging.error("[CACHE-RELOAD] clan_name_cache empty, cannot reload synchronously. This should never happen if load_all() was called at startup.")
            # NOTE: Cannot call async load_clan_name_cache() from sync context
            # This defensive reload is disabled. If cache is empty, it means load_all() wasn't called properly.
        
        clan_data = self.clan_name_cache.get(clan_tag)
        if clan_data is None:
            return default
        if isinstance(clan_data, dict):  # type: ignore[misc]
            return clan_data.get('name', default)  # type: ignore[return-value]
        # Defensive: handle unexpected non-dict format gracefully
        return clan_data if isinstance(clan_data, str) else default  # type: ignore[misc, return-value]

    def get_clan_war_league(self, clan_tag: str, default: Optional[str] = None) -> Optional[str]:
        """Safely get a clan's current CWL league tier (e.g. "Crystal League I") from cache.

        This is CoC-API-sourced (clans.war_league, synced via coc_cache.py) and reflects the
        game's own promotion/demotion state — it is not something admins configure, so any UI
        showing a clan's CWL tier should read it from here rather than treating it as an
        editable field.
        """
        clan_data = self.clan_name_cache.get(clan_tag)
        if not isinstance(clan_data, dict):  # type: ignore[misc]
            return default
        war_league = clan_data.get('war_league')  # type: ignore[union-attr]
        return war_league if isinstance(war_league, str) and war_league else default

    async def load_leaderboard_messages(self) -> None:
        """
        Load leaderboard messages (IDs + content hashes + metadata) from database.
        Format:
            {
                "timestamp_key": {
                    "clan_tag": "#L2J0C0PY",
                    "channel_id": "123456789012345678",
                    "mode": "stars_08_2025",
                    "message_ids": "123456789",
                    "content_hash": "abc123def456..."
                }
            }
        """
        try:
            messages_from_db = await self.db_manager.get_all_leaderboard_messages()  # type: ignore[union-attr]
            self.leaderboard_messages = messages_from_db
            logging.info(f"[DB-READ] Loaded {len(self.leaderboard_messages)} leaderboard messages from database")
        except Exception as e:
            logging.error(f"Failed to load leaderboard messages from database: {e}")
            self.leaderboard_messages = {}
            logging.info("Initialized empty leaderboard messages (will create on first save)")

    async def set_leaderboard_message(self, message_key: str, message_data: Dict[str, Any]) -> None:
        """
        Set leaderboard message with immediate database persistence (write-through).
        
        Args:
            message_key: Unique key for the message (e.g., "L2J0C0PY_08_2025_stars_123456")
            message_data: Message data dict containing clan_tag, channel_id, mode, message_ids, content_hash
        
        Raises:
            Exception: If database write fails
        """
        try:
            # Update in-memory cache
            self.leaderboard_messages[message_key] = message_data
            
            # Prepare clan_tag (convert old "channel_" format to None)
            clan_tag = message_data.get('clan_tag')
            if clan_tag and clan_tag.startswith('channel_'):
                clan_tag = None
            
            # Guard: db_manager may be None during early startup (before on_ready finishes)
            if self.db_manager is None:
                logging.warning(f"[DB-WRITE-THROUGH] db_manager not yet initialized, skipping DB write for {message_key}")
                return
            
            # Ensure clan exists in database if clan_tag is set
            if clan_tag:
                await self.db_manager._ensure_clan_exists(clan_tag)  # type: ignore[union-attr]
            
            # Immediately persist to database
            await self.db_manager.save_leaderboard_message(  # type: ignore[union-attr]
                message_key=message_key,
                clan_tag=clan_tag,
                channel_id=message_data['channel_id'],
                mode=message_data['mode'],
                message_ids=message_data['message_ids'],
                content_hash=message_data['content_hash']
            )
            logging.debug(f"[DB-WRITE-THROUGH] Persisted leaderboard message: {message_key}")
        except Exception as e:
            logging.error(f"[DB-WRITE-THROUGH] Failed to persist leaderboard message {message_key}: {e}")
            raise

    async def delete_leaderboard_message(self, message_key: str) -> None:
        """
        Delete leaderboard message with immediate database persistence (write-through).
        
        Args:
            message_key: Unique key for the message to delete
        
        Raises:
            Exception: If database delete fails
        """
        try:
            # Delete from database first to avoid cache/DB divergence on failure
            await self.db_manager.delete_leaderboard_message(message_key)  # type: ignore[union-attr]

            # Remove from in-memory cache
            if message_key in self.leaderboard_messages:
                del self.leaderboard_messages[message_key]

            logging.debug(f"[DB-DELETE-THROUGH] Removed leaderboard message: {message_key}")
        except Exception as e:
            logging.error(f"[DB-DELETE-THROUGH] Failed to delete leaderboard message {message_key}: {e}")
            raise

    async def load_clan_families(self) -> None:
        """
        Load clan families mapping from database.
        Structure: family_tag -> {"name": str, "clans": List[str], "owned_by_guild": str}
        """
        try:
            families_from_db = await self.db_manager.get_all_clan_families()  # type: ignore[union-attr]
            self.clan_families = families_from_db
            logging.info(f"[DB-READ] Loaded {len(self.clan_families)} clan families from database")
        except Exception as e:
            logging.error(f"Failed to load clan families from database: {e}")
            self.clan_families = {}
            logging.info("Initialized empty clan families (will create on first save)")

    async def set_clan_family(self, family_tag: str, family_data: Dict[str, Any]) -> None:
        """Set clan family with immediate DB persistence (write-through).
        
        Raises:
            Exception: If database write fails (cache is still updated)
        """
        try:
            # Update in-memory cache
            self.clan_families[family_tag] = family_data

            # Immediately persist to database
            await self.db_manager.save_clan_family(  # type: ignore[union-attr]
                family_tag=family_tag,
                name=family_data.get("name", "Unknown Family"),
                owned_by_guild=family_data.get("owned_by_guild", ""),
                member_clans=family_data.get("clans", [])
            )
            logging.debug(f"[DB-WRITE-THROUGH] Persisted clan family: {family_tag}")
            # Family membership changed → recalculate subscription/tracking for affected clans
            await self.update_all_clan_subscription_statuses()
        except Exception as e:
            logging.error(f"[DB-WRITE-THROUGH] Failed to persist clan family {family_tag}: {e}")
            raise

    async def persist_clan_family(self, family_tag: str) -> None:
        """Persist an existing in-memory clan family to DB after in-place modification.
        
        Raises:
            Exception: If database write fails
        """
        if family_tag not in self.clan_families:
            logging.warning(f"[DB-WRITE] Cannot persist clan family {family_tag}: not found in cache")
            return
        try:
            family_data = self.clan_families[family_tag]
            await self.db_manager.save_clan_family(  # type: ignore[union-attr]
                family_tag=family_tag,
                name=family_data.get("name", "Unknown Family"),
                owned_by_guild=family_data.get("owned_by_guild", ""),
                member_clans=family_data.get("clans", [])
            )
            logging.debug(f"[DB-WRITE-THROUGH] Persisted clan family (in-place): {family_tag}")
            # Family membership may have changed → recalculate subscription/tracking
            await self.update_all_clan_subscription_statuses()
        except Exception as e:
            logging.error(f"[DB-WRITE-THROUGH] Failed to persist clan family {family_tag}: {e}")
            raise

    async def delete_clan_family(self, family_tag: str) -> None:
        """Delete clan family with immediate DB persistence (write-through).
        
        Raises:
            Exception: If database delete fails (cache entry already removed)
        """
        try:
            # Remove from in-memory cache
            if family_tag in self.clan_families:
                del self.clan_families[family_tag]

            # Immediately delete from database (CASCADE deletes members)
            await self.db_manager.delete_clan_family(family_tag)  # type: ignore[union-attr]
            logging.debug(f"[DB-DELETE-THROUGH] Removed clan family: {family_tag}")
            # Family removed → recalculate subscription/tracking for former member clans
            await self.update_all_clan_subscription_statuses()
        except Exception as e:
            logging.error(f"[DB-DELETE-THROUGH] Failed to delete clan family {family_tag}: {e}")
            raise

    async def load_user_accounts(self) -> None:
        """
        Load user accounts mapping from database.
        
        Validation approach:
        - Validates REQUIRED fields exist with correct types (display_name, players list)
        - Sets defaults for OPTIONAL fields if missing (verified, is_primary, notification_settings)
        - Preserves ALL other fields automatically (user_language, future additions)
        
        Structure: user_id -> {
            "display_name": str (required),
            "user_language": str (optional, preserved),
            "notification_settings": dict (optional, initialized if missing),
            "players": List[{
                "player_tag": str (required),
                "player_name": str (required),
                "verified": bool (optional, default False),
                "is_primary": bool (optional, default False),
                ... (all other fields preserved)
            }]
        }
        """
        if not self.db_manager:
            raise RuntimeError("Database manager not initialized")
        
        try:
            self.user_accounts = await self.db_manager.get_all_users_dict()
            self.users_loaded = True
            logging.info(f"[DB-READ] Loaded {len(self.user_accounts)} user accounts from database")
            return
        except Exception as e:
            logging.error(f"[DB-READ] Failed to load users from database: {e}")
            raise SystemExit(1)

    async def set_user_account(self, discord_id: str, user_data: Dict[str, Any]) -> None:
        """
        Set user account with immediate database persistence (write-through).
        
        Replaces the entire user entry in cache and persists to DB immediately.
        Preserves unknown fields from existing data to prevent data loss.
        
        Args:
            discord_id: Discord user ID (string)
            user_data: Complete user data dict
        
        Raises:
            Exception: If database write fails
        """
        if not self.users_loaded:
            # Writing before the DB load completes would persist this (necessarily
            # cache-blind) entry over the user's real DB rows — see users_loaded.
            raise RuntimeError(
                f"Refusing set_user_account({discord_id}) before user accounts are loaded from DB "
                "(write-through on an unloaded cache would overwrite real data)"
            )
        try:
            # Preserve unknown fields from existing data (Pitfall 7)
            if discord_id in self.user_accounts:
                existing_data = self.user_accounts[discord_id]
                for key, value in existing_data.items():
                    if key not in user_data:
                        user_data[key] = value
            
            # Update in-memory cache
            self.user_accounts[discord_id] = user_data
            
            # Immediately persist to database
            await self.db_manager.save_user(discord_id, user_data)  # type: ignore[union-attr]
            logging.debug(f"[DB-WRITE-THROUGH] Persisted user account: {discord_id}")
        except Exception as e:
            logging.error(f"[DB-WRITE-THROUGH] Failed to persist user account {discord_id}: {e}")
            raise

    async def persist_user(self, discord_id: str) -> None:
        """
        Persist current in-memory user data to database (write-through).
        
        Use after in-place modifications to user_accounts[discord_id].
        This saves the current state of the user in cache to the database.
        
        Args:
            discord_id: Discord user ID (string)
        
        Raises:
            Exception: If database write fails or user not found in cache
        """
        if not self.users_loaded:
            raise RuntimeError(
                f"Refusing persist_user({discord_id}) before user accounts are loaded from DB "
                "(write-through on an unloaded cache would overwrite real data)"
            )
        try:
            user_data = self.user_accounts.get(discord_id)
            if user_data is None:
                logging.warning(f"[DB-WRITE-THROUGH] User {discord_id} not found in cache, skipping persist")
                return
            
            # Immediately persist to database
            await self.db_manager.save_user(discord_id, user_data)  # type: ignore[union-attr]
            logging.debug(f"[DB-WRITE-THROUGH] Persisted user account: {discord_id}")
        except Exception as e:
            logging.error(f"[DB-WRITE-THROUGH] Failed to persist user account {discord_id}: {e}")
            raise

    async def delete_user_account(self, discord_id: str) -> None:
        """
        Delete user account with immediate database persistence (write-through).
        
        Removes user from in-memory cache and database. CASCADE will delete
        associated player entries in the database.
        
        Args:
            discord_id: Discord user ID (string)
        
        Raises:
            Exception: If database delete fails
        """
        if not self.users_loaded:
            raise RuntimeError(
                f"Refusing delete_user_account({discord_id}) before user accounts are loaded from DB"
            )
        try:
            # Remove from in-memory cache
            if discord_id in self.user_accounts:
                del self.user_accounts[discord_id]
            
            # Immediately delete from database
            await self.db_manager.delete_user(discord_id)  # type: ignore[union-attr]
            logging.debug(f"[DB-DELETE-THROUGH] Removed user account: {discord_id}")
        except Exception as e:
            logging.error(f"[DB-DELETE-THROUGH] Failed to delete user account {discord_id}: {e}")
            raise

    async def update_user_metadata(self, user_id: str, user_obj: Optional['discord.User'] = None, interaction: Optional['discord.Interaction'] = None) -> bool:
        """
        Update user metadata (display_name, user_language) from Discord API.
        
        Automatically called whenever user data is accessed or user interacts with bot.
        Compares cached values with fresh API data and updates if changed.
        
        Args:
            user_id: Discord user ID (string)
            user_obj: Optional discord.User object (if already fetched)
            interaction: Optional discord.Interaction (provides locale information)
        
        Returns:
            True if any data was updated, False otherwise
        
        Example:
            # During interaction
            await CACHE.update_user_metadata(str(interaction.user.id), interaction=interaction)
            
            # When fetching user
            user = await bot.fetch_user(user_id)
            await CACHE.update_user_metadata(str(user_id), user_obj=user)
        """
        import QBcore
        
        try:
            # Ensure user_id is string
            user_id = str(user_id)

            if not self.users_loaded:
                # Before the DB load completes the cache is empty — the skeleton-creation
                # below would fabricate a players=[] entry for an existing user and the
                # write-through would hard-delete their real user_players rows (2026-08-08
                # prod incident). Metadata refresh is cosmetic; skip it entirely pre-load.
                logging.warning(
                    f"[STARTUP-GUARD] Skipping update_user_metadata({user_id}) — user accounts not yet loaded from DB"
                )
                return False

            updated = False
            
            # Fetch user object if not provided
            if user_obj is None and interaction is None:
                user_obj = await QBcore.bot.fetch_user(int(user_id))
            elif interaction is not None and user_obj is None:
                user_obj = interaction.user  # type: ignore[assignment]

            # Get or create user account entry (after user resolution succeeds)
            if user_id not in self.user_accounts:
                self.user_accounts[user_id] = {
                    "display_name": "",
                    "user_language": "en",
                    "notification_settings": {
                        "notification_mode": "repeated",
                        "notification_type": "all_wars",
                        "hours_before_end": 4,
                        "war_reminders": True
                    },
                    "players": []
                }
                updated = True

            user_data = self.user_accounts[user_id]
            
            # Update display_name if changed
            if user_obj:
                new_display_name = user_obj.display_name
                if user_data.get("display_name") != new_display_name:
                    logging.debug(f"Updated display_name for {user_id}: {user_data.get('display_name')} -> {new_display_name}")
                    user_data["display_name"] = new_display_name
                    updated = True
            
            # Update user_language from interaction locale
            if interaction:
                locale = getattr(interaction, 'locale', None)
                if locale:
                    # Map Discord locale to language code
                    new_language = 'de' if str(locale).startswith('de') else 'en'
                    
                    # Only update if user hasn't manually locked their language preference
                    user_language_locked = user_data.get("user_language_locked", False)
                    
                    if not user_language_locked and user_data.get("user_language") != new_language:
                        logging.info(f"Updated user_language for {user_id} ({user_data.get('display_name')}): {user_data.get('user_language')} -> {new_language} (locale: {locale})")
                        user_data["user_language"] = new_language
                        updated = True
                    elif user_language_locked:
                        logging.debug(f"Skipped language update for {user_id} - language is locked by user preference")
            
            # Save if updated (write-through: persist only the affected user)
            if updated:
                await self.persist_user(user_id)
            
            return updated
            
        except discord.NotFound:
            # Re-raise NotFound so callers can catch it (e.g., invalid user ID)
            raise
        except Exception as e:
            logging.error(f"Error updating user metadata for {user_id}: {e}", exc_info=True)
            return False

    async def ensure_user_metadata(self, user_id: str) -> Dict[str, Any]:
        """
        Ensure user metadata exists and is current, fetching from API if missing.
        
        Called when reading user data from cache. If display_name or user_language
        is missing, fetches from Discord API and updates cache.
        
        Args:
            user_id: Discord user ID (string)
        
        Returns:
            User data dictionary with guaranteed display_name and user_language
        
        Example:
            user_data = await CACHE.ensure_user_metadata(str(user_id))
            language = user_data.get("user_language", "en")
        """
        # import QBcore  # Not used in this function
        
        try:
            user_id = str(user_id)
            
            # Get or create user data
            if user_id not in self.user_accounts:
                await self.update_user_metadata(user_id)
                return self.user_accounts.get(user_id, {})
            
            user_data = self.user_accounts[user_id]
            
            # Check if metadata is missing
            needs_update = False
            if not user_data.get("display_name"):
                needs_update = True
                logging.debug(f"User {user_id} missing display_name, fetching from API")
            if "user_language" not in user_data:
                needs_update = True
                logging.debug(f"User {user_id} missing user_language, fetching from API")
            
            # Fetch from API if needed
            if needs_update:
                await self.update_user_metadata(user_id)
            
            return self.user_accounts.get(user_id, {})
            
        except discord.NotFound:
            # Re-raise NotFound so callers can catch it (e.g., invalid user ID)
            raise
        except Exception as e:
            logging.error(f"Error ensuring user metadata for {user_id}: {e}", exc_info=True)
            return self.user_accounts.get(user_id, {})

    async def get_user_for_dm(self, user_id: str) -> Optional['discord.User']:
        """
        Get Discord User object for sending DMs, with metadata update.
        
        Centralizes user fetching for DM operations. Automatically updates
        cached metadata (display_name) while fetching.
        
        Args:
            user_id: Discord user ID (string or int)
        
        Returns:
            discord.User object or None if fetch fails
        
        Example:
            user = await CACHE.get_user_for_dm(str(user_id))
            if user:
                await user.send("Hello!")
        """
        import QBcore
        
        try:
            user_id = str(user_id)
            user = await QBcore.bot.fetch_user(int(user_id))
            
            # Update metadata while we have the user object
            await self.update_user_metadata(user_id, user_obj=user)
            
            return user
            
        except discord.HTTPException as e:
            # 503/5xx = Discord outage; 429 code 40062 = "service resource rate limited"
            # (Discord-side overload signal that often precedes full outages).
            # Both are temporary and should not produce alarming ERROR tracebacks.
            if e.status >= 500 or (e.status == 429 and getattr(e, 'code', 0) == 40062):
                logging.warning(f"Discord temporarily unavailable when fetching user {user_id}: {e}")
            else:
                logging.error(f"Failed to fetch user {user_id} for DM: {e}", exc_info=True)
            return None
        except Exception as e:
            logging.error(f"Failed to fetch user {user_id} for DM: {e}", exc_info=True)
            return None

    async def send_user_dm(
        self,
        user_id: str,
        message: str,
        view: Optional['discord.ui.View'] = None,
        embed: Optional['discord.Embed'] = None,
    ) -> bool:
        """
        Send DM to user, handling fetch and metadata update internally.

        Centralizes DM sending with automatic user fetching and metadata updates.
        Handles common exceptions (Forbidden, NotFound) gracefully.

        Args:
            user_id: Discord user ID (string or int)
            message: Message text to send
            view: Optional interactive view to attach (e.g. CWL confirm/opt-out buttons)
            embed: Optional embed to attach

        Returns:
            True if sent successfully, False otherwise

        Example:
            success = await CACHE.send_user_dm(str(user_id), "War reminder!")
            if not success:
                logging.warning(f"Could not DM user {user_id}")
        """
        import discord

        try:
            user = await self.get_user_for_dm(user_id)
            if not user:
                return False

            await user.send(message, view=view, embed=embed)
            return True

        except discord.Forbidden:
            logging.info(f"Cannot send DM to user {user_id}: DMs disabled or bot blocked")
            return False
        except discord.NotFound:
            logging.info(f"Cannot send DM to user {user_id}: User not found")
            return False
        except discord.DiscordServerError as e:
            logging.warning(f"Transient Discord server error sending DM to {user_id}: HTTP {e.status}")
            raise
        except Exception as e:
            logging.error(f"Failed to send DM to user {user_id}: {e}", exc_info=True)
            return False

    async def get_player(self, player_tag: str) -> Optional['coc.Player']:
        """
        Fetch player data from CoC API.
        
        Centralized method for all player data fetching. Provides consistent
        error handling and logging.
        
        Args:
            player_tag: Player tag (will be normalized automatically)
        
        Returns:
            coc.Player object or None if fetch fails
        
        Example:
            player = await CACHE.get_player("#ABC123")
            if player:
                print(f"Player: {player.name}, TH{player.town_hall}")
        """
        # import QBcore  # Not used in this function
        from qapbot.QBdiscocmdshelper import normalize_clan_tag
        
        try:
            normalized_tag = normalize_clan_tag(player_tag)
            if not normalized_tag:
                logging.warning(f"Invalid player tag format: {player_tag}")
                return None
            
            if not self.coc_client:
                raise RuntimeError("CoC API client not initialized. Call startup_login() first.")
            
            async def _fetch_player() -> coc.Player:
                return await self.coc_client.get_player(normalized_tag)  # type: ignore[union-attr]
            
            player: coc.Player = await coc_retry(  # type: ignore[assignment]
                _fetch_player,
                operation_name=f"get_player({normalized_tag})"
            )
            return player
            
        except Exception as e:
            logging.warning(f"Failed to fetch player {player_tag} from CoC API: {e}")
            return None

    async def verify_api_token(self, player_tag: str, api_token: str) -> tuple[bool, str]:
        """
        Verify that an API token belongs to a specific player.
        
        Uses the CoC API's verify_player_token() method which is the proper way to validate
        ownership. Returns True only if the token belongs to the specified player.
        
        Args:
            player_tag: Player tag to verify (will be normalized)
            api_token: CoC API token to validate
        
        Returns:
            Tuple of (is_valid: bool, message: str)
            - (True, "success message") if token is valid
            - (False, "error message") if token is invalid or verification fails
        
        Example:
            valid, msg = await CACHE.verify_api_token("#ABC123", "token123")
            if valid:
                # Mark player as verified
                CACHE.set_player_verification(user_id, player_tag, True)
        """
        # import QBcore  # Not used in this function
        from qapbot.QBdiscocmdshelper import normalize_clan_tag
        
        normalized_tag = ""  # Initialize for error handling scope
        try:
            normalized_tag = normalize_clan_tag(player_tag)
            if not normalized_tag:
                return False, "Invalid player tag format"
            
            if not self.coc_client:
                raise RuntimeError("CoC API client not initialized. Call startup_login() first.")
            
            # Use the proper CoC API method for token verification
            verification_result: bool = await coc_retry(
                lambda: self.coc_client.verify_player_token(normalized_tag, api_token),  # type: ignore[union-attr]
                operation_name=f"verify_player_token({normalized_tag})"
            )
            
            if verification_result is True:
                logging.info(f"API token validation SUCCESS for {normalized_tag}")
                return True, "API token validated successfully"
            else:
                logging.warning(f"API token validation FAILED for {normalized_tag}")
                return False, "API token is invalid or doesn't belong to this player"
                
        except Exception as e:
            error_msg = str(e).lower()
            if "forbidden" in error_msg or "403" in error_msg:
                logging.warning(f"API token validation FAILED for {normalized_tag}: forbidden/403")
                return False, "API token is invalid or doesn't belong to this player"
            elif "not found" in error_msg or "404" in error_msg:
                logging.warning(f"API token validation FAILED for {normalized_tag}: player not found")
                return False, "Player not found"
            # Removed unreachable generic Exception clause
            logging.error(f"API token validation ERROR for {normalized_tag}: {e}")
            return False, f"API token validation error: {str(e)}"

    async def set_player_verification(self, user_id: str, player_tag: str, verified: bool) -> bool:
        """
        Set the verification status for a specific player account.
        
        Args:
            user_id (str): Discord user ID
            player_tag (str): Clash of Clans player tag
            verified (bool): Verification status to set
            
        Returns:
            bool: True if the player was found and updated, False otherwise
            
        Example:
            success = CACHE.set_player_verification("123456789", "#ABC123", True)
        """
        if not isinstance(verified, bool):  # type: ignore[misc]
            logging.error(f"Invalid verification status type: {type(verified)}, expected bool")
            return False
            
        user_entry = self.user_accounts.get(user_id)
        if not user_entry:
            logging.warning(f"User {user_id} not found in user accounts")
            return False
            
        players = user_entry.get("players", [])
        for player in players:
            current_tag = player.get("player_tag", "")
            if current_tag == player_tag:
                player["verified"] = verified
                await self.persist_user(user_id)
                logging.info(f"Updated verification status for player {player_tag} in user {user_id}: {verified}")
                return True
                
        logging.warning(f"Player {player_tag} not found in user {user_id} accounts")
        return False

    def is_player_verified(self, user_id: str, player_tag: str) -> bool:
        """
        Check if a player account is marked as verified for a given user.

        Args:
            user_id (str): Discord user ID
            player_tag (str): Clash of Clans player tag
        Returns:
            bool: True if the player is verified, False otherwise
        """
        user_entry = self.user_accounts.get(user_id)
        if not user_entry:
            return False
        for player in user_entry.get("players", []):
            current_tag = player.get("player_tag", "")
            if current_tag == player_tag:
                return bool(player.get("verified", False))
        return False

    async def fetch_and_update_player_info(self, player_tag: str) -> Dict[str, Any]:
        """
        Fetch player info from CoC API and update user accounts and clan name cache.
        
        This function is called when player TH level or clan information is missing/unknown.
        It makes a direct API call to retrieve current player data and immediately updates
        the cache to prevent future lookups.
        
        Args:
            player_tag: Normalized player tag (e.g., "#ABC123")
            
        Returns:
            Dict with player info: {"th_level": int, "clan_tag": str or None, "clan_name": str or None}
            Returns empty dict on API errors.
            
        Side Effects:
            - Updates user_accounts with th_level and current_clan_tag (write-through to DB)
            - Updates clan_name_cache with clan name if player is in a clan (write-through to DB)
            - Persists changes to database immediately
            
        Example:
            player_info = await CACHE.fetch_and_update_player_info("#ABC123")
            # Returns: {"th_level": 15, "clan_tag": "#2CLANTAG01", "clan_name": "Our Clan"}
        """
        if not self.coc_client:
            raise RuntimeError("CoC API client not initialized. Call startup_login() first.")
        
        try:
            logging.debug(f"[PLAYER-API-CALL] Fetching {player_tag} from CoC API (missing info)")
            
            async def _fetch_player() -> coc.Player:
                return await self.coc_client.get_player(player_tag)  # type: ignore[union-attr]
            
            player_obj: coc.Player = await coc_retry(  # type: ignore[assignment]
                _fetch_player,
                operation_name=f"get_player({player_tag})"
            )
            
            # Extract player info
            th_level = player_obj.town_hall
            clan_tag = player_obj.clan.tag if player_obj.clan else None
            clan_name = player_obj.clan.name if player_obj.clan else None
            
            # Update user_accounts for all users who have this player.
            # Collect the actually-changed user IDs here instead of a bool flag, so the
            # write-through persist below doesn't need its own second full scan of
            # user_accounts (previously O(users×players) twice per fetched player).
            changed_uids: set[str] = set()
            for user_id, user_data in self.user_accounts.items():
                # Skip UNASSIGNED pseudo-user
                if user_id == "UNASSIGNED":
                    continue
                # Skip invalid entries (defensive programming)
                if not isinstance(user_data, dict):  # type: ignore[misc]
                    continue

                players = user_data.get("players", [])
                for player in players:
                    if not isinstance(player, dict):
                        continue

                    current_tag = player.get("player_tag")  # type: ignore[union-attr]
                    if current_tag == player_tag:
                        # Update TH level
                        old_th = player.get("th_level")  # type: ignore[union-attr]
                        if old_th != th_level:
                            player["th_level"] = th_level
                            changed_uids.add(user_id)
                            logging.info(f"[PLAYER-INFO-UPDATE] {player_tag}: TH {old_th} -> {th_level}")

                        # Update current clan tag
                        old_clan_tag = player.get("current_clan_tag")  # type: ignore[union-attr]
                        if old_clan_tag != clan_tag:
                            player["current_clan_tag"] = clan_tag
                            changed_uids.add(user_id)
                            logging.info(f"[PLAYER-INFO-UPDATE] {player_tag}: Clan {old_clan_tag} -> {clan_tag}")

            # Save affected user accounts (write-through: persist only changed users)
            if changed_uids:
                for uid in changed_uids:
                    await self.persist_user(uid)
                logging.debug(f"[USER-ACCOUNTS-SAVE] Saved player info updates for {player_tag}")
            
            # Update clan_name_cache if player is in a clan
            if clan_tag and clan_name:
                clan_data = self.clan_name_cache.get(clan_tag)
                if isinstance(clan_data, dict):
                    # New format - update name if changed
                    if clan_data.get('name') != clan_name:
                        clan_data['name'] = clan_name
                        await self.persist_clan(clan_tag)
                        logging.debug(f"[CLAN-NAME-UPDATE] {clan_tag}: {clan_name}")
                elif clan_data is not None:
                    # Old format (string) - convert to dict
                    self.clan_name_cache[clan_tag] = {
                        "name": clan_name,
                        "has_active_subscriptions": False,
                        "last_war_update": None,
                        "warlog_is_public": None,
                        "last_checked_via_api": None
                    }
                    await self.persist_clan(clan_tag)
                    logging.debug(f"[CLAN-NAME-UPDATE] {clan_tag}: {clan_name} (converted to dict)")
                else:
                    # New clan - create dict entry
                    self.clan_name_cache[clan_tag] = {
                        "name": clan_name,
                        "has_active_subscriptions": False,
                        "last_war_update": None,
                        "warlog_is_public": None,
                        "last_checked_via_api": None
                    }
                    await self.persist_clan(clan_tag)
                    logging.debug(f"[CLAN-NAME-ADD] {clan_tag}: {clan_name}")
            
            return {
                "th_level": th_level,
                "clan_tag": clan_tag,
                "clan_name": clan_name
            }
            
        except Exception as e:
            logging.error(f"[PLAYER-API-ERROR] Failed to fetch {player_tag}: {e}")
            return {}

    def load_all_temp_war_stats(self) -> None:
        """
        Load all current war temp stats for all clans into memory.

        Two-phase design for server-machine performance (called via asyncio.to_thread — never on the event loop):

        Phase 1 — Metadata from filenames (ZERO file reads):
            Derives state/start_time/end_time directly from the filename timestamp
            (CLANTAG_OPPONENTTAG_YYYYMMDDHHMM_war_data.json). The YYYYMMDDHHMM is the
            battle start time, so end_time ≈ start_time + 24 h. This replaces reading
            76k JSON files just for metadata and cuts startup from 60-95 s to < 1 s.

            WHY state is always hardcoded to "in_war" (never synthesized as "war_ended"):
            When CoC servers go into maintenance, ALL ongoing wars are automatically
            extended by the duration of the downtime. This means the filename timestamp
            (start_time + 24h) is NOT a reliable upper bound for war end time. A war
            that appears "ended" from the filename may still be running post-maintenance.
            Prematurely marking it "war_ended" in metadata would cause Phase-3B's
            skip-until logic to make wrong decisions and /status to over-report
            war_ended counts. We leave state = "in_war" universally; save_war_object()
            corrects it to the real state after the first API poll.

        Phase 2 — Selective full stats parse (JSON reads for subscribed clans only):
            Reads JSON and calls _load_war_data_from_json() ONLY for:
            - Subscribed clans (has_active_subscriptions=True) — needed for channel/DM
              notifications and leaderboards immediately after restart.
            All other clans get temp_war_stats = {} and temp_war_objects = {} until
            Phase-3 repopulates them after the first update cycle (~5-10 min).
            This is safe because:
            - War notifications (_get_active_wars) already skip clans with empty stats
              and notifications are never sent for non-subscribed clans anyway.
            - manage_war_files, Phase-3B skip-until, and throttle-exemption all use
              temp_war_metadata only (populated in Phase 1 for all clans).
            - /currentwar player search has a ~5-min cold-start gap for non-subscribed
              clans — acceptable trade-off vs. parsing 44K-100K JSON files at startup.
            The JSON-sourced metadata overrides the filename estimate for Phase-2 clans.

            WHY the old "within-6h window" condition was removed:
            The time window was intended to pre-load stats for clans about to end their
            war (for notifications). But notifications only go to subscribed clans, so
            pre-loading non-subscribed clans' stats for that window was wasted work.
            During CWL season nearly all 44K-100K temp files are for genuinely ongoing
            wars, so the time window matched almost everything — defeating its purpose.

        Also:
            - Keeps the LATEST file per clan (lexicographic timestamp comparison) so CWL
              multi-round scenarios always load the current round.
            - Runs inside asyncio.to_thread() so the Discord event loop stays free.
        """
        import json as _json_mod
        import QBcsvhandling
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from datetime import datetime, timedelta, timezone as _tz

        self.temp_war_stats = {}
        self.temp_war_metadata = {}

        # ── Phase 1: scan directory, derive metadata from filenames ──────────────────

        temp_dir = os.path.join(CONFIG.data_dir, "temp")

        # {safe_tag: (filepath, ts_str)} — keep the LATEST file per clan tag
        temp_file_map: Dict[str, Tuple[str, str]] = {}
        _all_shard_files = [
            fp
            for sd in glob.glob(os.path.join(temp_dir, "shard_*"))
            for fp in glob.glob(os.path.join(sd, "*_war_data.json"))
        ]
        for filepath in _all_shard_files:
            fname = os.path.basename(filepath)
            # Filename: CLANTAG_OPPONENTTAG_YYYYMMDDHHMM_war_data.json
            parts = fname.split("_")
            if len(parts) < 4:
                continue
            safe_tag = parts[0].upper()
            ts_str = parts[2]  # YYYYMMDDHHMM
            existing = temp_file_map.get(safe_tag)
            if existing is None or ts_str > existing[1]:   # lexicographic == chronological
                temp_file_map[safe_tag] = (filepath, ts_str)

        # Reverse lookup: safe-normalised clan tag → original tag from name cache
        safe_to_clan: Dict[str, str] = {
            re.sub(r'[^A-Z0-9]', '', tag.upper()): tag
            for tag in self.clan_name_cache
        }

        # Subscribed clans always receive full stats
        subscribed_tags: Set[str] = {
            tag for tag, data in self.clan_name_cache.items()
            if bool(data.get('has_active_subscriptions', False))
        }

        clans_needing_full_parse: List[Tuple[str, str]] = []  # (clan_tag, filepath)
        loaded_meta_count = 0

        for safe_tag, (filepath, ts_str) in temp_file_map.items():
            clan_tag = safe_to_clan.get(safe_tag)
            if not clan_tag:
                continue

            # Derive battle start/end from the filename timestamp
            start_str = ""
            end_str = ""
            state = "in_war"
            end_dt: Optional[datetime] = None

            if len(ts_str) == 12 and ts_str.isdigit():
                try:
                    y, mo, d, h, mi = (
                        int(ts_str[:4]), int(ts_str[4:6]), int(ts_str[6:8]),
                        int(ts_str[8:10]), int(ts_str[10:12])
                    )
                    s_dt = datetime(y, mo, d, h, mi, tzinfo=_tz.utc)
                    end_dt = s_dt + timedelta(hours=24)
                    # CoC Timestamp format — compatible with _parse_dt_from_raw()
                    start_str = (
                        f"<Timestamp time=datetime.datetime({y}, {mo}, {d}, {h}, {mi})"
                        f" seconds_until=0>"
                    )
                    ey, emo, ed, eh, emi = (
                        end_dt.year, end_dt.month, end_dt.day,
                        end_dt.hour, end_dt.minute
                    )
                    end_str = (
                        f"<Timestamp time=datetime.datetime({ey}, {emo}, {ed}, {eh}, {emi})"
                        f" seconds_until=0>"
                    )
                    # Do NOT synthesize "war_ended" from the filename estimate.
                    # CoC maintenance extends all ongoing wars by the downtime duration,
                    # so start_time + 24h is NOT a reliable end-time upper bound.
                    # A war that looks finished from the filename may still be running.
                    # Leave state = "in_war" (the default); save_war_object() will correct
                    # it to the real API-sourced state after the first poll.
                    # The end_str is still useful for Phase-3B end-time guards.
                    pass
                except (ValueError, OverflowError):
                    pass

            # Extract opponent tag and CWL fields from filename.
            # 3-part: CLAN_OPP_TS_war_data.json        → parts has 5 tokens
            # 4-part: CLAN_OPP_TS_WARTAG_war_data.json → parts has 6 tokens
            _fname_parts = os.path.basename(filepath).split("_")
            _opp_safe = _fname_parts[1].upper() if len(_fname_parts) >= 3 else None
            _opp_tag = f"#{_opp_safe}" if _opp_safe else None
            # 4-part CWL filename: war_tag is parts[3], raw form without '#'
            _is_cwl_fn = len(_fname_parts) == 6
            _war_tag_fn = f"#{_fname_parts[3]}" if _is_cwl_fn else None
            self.temp_war_metadata[clan_tag] = {
                "state": state,
                "start_time": start_str,
                "end_time": end_str,
                "filepath": filepath,
                "type": "cwl" if _is_cwl_fn else None,
                "is_cwl": _is_cwl_fn,
                "war_tag": _war_tag_fn,
                "opponent_tag": _opp_tag,
            }
            loaded_meta_count += 1

            if state == "in_war":
                self.in_war_clan_tags.add(clan_tag)

            # Phase 2: only subscribed clans need full JSON parse at startup.
            # Non-subscribed clans get temp_war_stats={} until Phase-3 repopulates
            # them after the first update cycle (~5-10 min). War notifications and
            # leaderboards only apply to subscribed clans, so nothing is missed.
            if clan_tag in subscribed_tags:
                clans_needing_full_parse.append((clan_tag, filepath))

        # ── Phase 2: selective JSON parse ────────────────────────────────────────────

        def _load_one(clan_tag: str, filepath: str) -> Tuple[str, Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
            """Read JSON and parse stats; returns (clan_tag, stats, updated_metadata, raw_war_obj)."""
            updated_meta: Dict[str, Any] = {}
            raw: Dict[str, Any] = {}
            try:
                with open(filepath, 'r', encoding='utf-8') as _f:
                    raw = _json_mod.load(_f)
                _opp_obj = raw.get("opponent")
                updated_meta = {
                    "state": str(raw.get("state", "")).lower(),
                    "start_time": str(raw.get("start_time", "")),
                    "end_time": str(raw.get("end_time", "")),
                    "filepath": filepath,
                    "type": raw.get("type"),
                    "is_cwl": bool(raw.get("is_cwl", False)),
                    "war_tag": raw.get("war_tag"),
                    "opponent_tag": _opp_obj.get("tag") if isinstance(_opp_obj, dict) else None,  # type: ignore[union-attr]
                }
            except Exception as exc:
                logging.warning(f"[TEMP-WAR-LOAD] Failed to read {filepath} for {clan_tag}: {exc}")
                return clan_tag, {}, {}, {}
            try:
                stats = QBcsvhandling._load_war_data_from_json(  # type: ignore[attr-defined]
                    clan_tag, json_file_path=filepath, preloaded_raw_data=raw
                )
            except Exception as exc:
                logging.warning(f"[TEMP-WAR-LOAD] Failed to parse stats for {clan_tag}: {exc}")
                return clan_tag, {}, updated_meta, raw
            return clan_tag, stats, updated_meta, raw

        loaded_stats_count = 0
        if clans_needing_full_parse:
            with ThreadPoolExecutor(max_workers=16) as executor:
                futures = {
                    executor.submit(_load_one, ct, fp): ct
                    for ct, fp in clans_needing_full_parse
                }
                for future in as_completed(futures):
                    clan_tag, stats, updated_meta, raw_obj = future.result()
                    if stats:
                        self.temp_war_stats[clan_tag] = stats
                        loaded_stats_count += 1
                        logging.debug(
                            f"[TEMP-WAR-LOAD] {clan_tag}: {len(stats)} player stats"
                        )
                    # Cache full war object for get_current_war_data()
                    if raw_obj:
                        self.temp_war_objects[clan_tag] = raw_obj
                    # Override filename-derived metadata with exact JSON values
                    if updated_meta:
                        self.temp_war_metadata[clan_tag] = updated_meta
                        if updated_meta.get("state") == "in_war":
                            self.in_war_clan_tags.add(clan_tag)
                        else:
                            self.in_war_clan_tags.discard(clan_tag)

        # Store raw file count so get_war_file_stats() can report it as "total"
        # rather than the deduplicated per-clan count (temp_file_map has one entry
        # per clan; clans with multiple temp files are under-counted otherwise).
        self.temp_total_file_count = len(_all_shard_files)

        logging.info(
            f"[TEMP-WAR-LOAD] Total temp war files: {loaded_meta_count} clans (metadata read from filenames). "
            f"Full JSON parse: {loaded_stats_count}/{len(clans_needing_full_parse)} clans "
            f"({len(subscribed_tags)} subscribed — non-subscribed clans repopulated by first update cycle)."
        )

    def get_temp_war_stats(self, clan_tag: str) -> Dict[str, Any]:
        """
        Get current war temp stats for a clan from in-memory cache.
        """
        return self.temp_war_stats.get(clan_tag, {})

    def get_war_file_stats(self) -> Dict[str, int]:
        """
        Return temp file counts by state and type from in-memory cache. Zero I/O.

        Note: is_cwl is only accurate for clans that have been API-polled or had a
        full JSON parse in the current session (subscribed clans / within-6h window at
        startup). Filename-only loaded entries (Phase 1 of load_all_temp_war_stats)
        default to is_cwl=False. The cwl_known count improves as the bot polls clans.
        """
        prep = 0
        in_war = 0
        war_ended = 0
        cwl_known = 0
        for m in self.temp_war_metadata.values():
            state = m.get("state", "")
            if state == "preparation":
                prep += 1
            elif state == "in_war":
                in_war += 1
            elif state == "war_ended":
                war_ended += 1
            if m.get("is_cwl"):
                cwl_known += 1
        # Use the raw file count from the last disk scan if available so the total
        # matches `countprodfiles`.  Metadata has one entry per clan (deduped), so
        # clans with multiple temp files (e.g. old CWL + new CWL) would otherwise
        # be under-counted by exactly the number of extra files.
        raw_total = self.temp_total_file_count or (prep + in_war + war_ended)
        # Files on disk not yet reflected in any state counter.
        # Two sources: (a) older CWL round files still present while a newer round is
        # already tracked (the newer round wins in temp_war_metadata), and (b) files
        # for clans that were removed from tracking and are therefore absent from
        # clan_name_cache / temp_war_metadata.
        other = max(0, raw_total - (prep + in_war + war_ended))
        return {
            "total": raw_total,
            "prep": prep,
            "in_war": in_war,
            "war_ended": war_ended,
            "other": other,
            "cwl_known": cwl_known,
        }

    def set_temp_war_stats(self, clan_tag: str, stats: Dict[str, Any], metadata: Optional[Dict[str, str]] = None) -> None:
        """
        Set current war temp stats for a clan in in-memory cache.
        Note: JSON persistence handled by save_war_object() during war updates.
        
        Args:
            clan_tag: Clan tag
            stats: Player stats dict (empty dict clears stats)
            metadata: Optional war metadata dict with keys "state", "start_time", "end_time".
                      If None and stats is empty, metadata is also cleared.
        """
        if stats:
            self.temp_war_stats[clan_tag] = stats
        else:
            # Remove key entirely instead of storing empty dict to prevent
            # unbounded growth as new clans are discovered (81K → empty entries).
            self.temp_war_stats.pop(clan_tag, None)
        if metadata is not None:
            self.temp_war_metadata[clan_tag] = metadata
            if metadata.get("state") == "in_war":
                self.in_war_clan_tags.add(clan_tag)
            else:
                self.in_war_clan_tags.discard(clan_tag)
        elif not stats:
            # Clearing stats → also clear stale metadata, war object cache, and active-war set
            self.temp_war_metadata.pop(clan_tag, None)
            self.temp_war_objects.pop(clan_tag, None)
            self.in_war_clan_tags.discard(clan_tag)

    def get_clan_history(self, clan_tag: str) -> List[Any]:
        """
        Get clan history data for a clan from database.
        
        Queries database via db_manager.get_clan_history_sync().
        Implements lazy-loading behavior to ensure history is available even
        after cache invalidation (e.g., after CWL war finalization).
        
        Args:
            clan_tag: Clan tag to query
        
        Returns:
            List of war history records (dicts with WarID, Date, Player, PlayerID, etc.)
        
        Note:
            This method is synchronous for compatibility with existing code.
            Database operations use synchronous sqlite3 wrapper for event loop safety.
        """
        # Verify database is initialized
        if not self.db_manager:
            logging.error(f"[CACHE] Database manager not initialized for {clan_tag}")
            return []
        
        try:
            # Return in-memory cache if already loaded.
            # Callers invalidate the cache (del CACHE.clan_history[clan_tag]) after war finalisation
            # so that the next call here triggers a fresh DB read automatically.
            if clan_tag in self.clan_history:
                logging.debug(f"[CACHE] Returning cached history for {clan_tag} ({len(self.clan_history[clan_tag])} records)")  # type: ignore
                return self.clan_history[clan_tag]  # type: ignore

            # Cache miss — fetch from DB (blocking, but only happens once per clan per update cycle)
            history = self.db_manager.get_clan_attack_history_sync(clan_tag)  # type: ignore
            
            logging.debug(f"[CACHE] Loaded {len(history)} records from DB for {clan_tag}")  # type: ignore
            self.clan_history[clan_tag] = history  # type: ignore
            return history  # type: ignore
            
        except Exception as e:
            logging.error(f"[CACHE] DB read failed for {clan_tag}: {e}")
            return []

    async def load_notification_state(self) -> None:
        """
        Load notification state from database.
        
        Structure: war_id -> {"notified_players": {player_tag: {player_name, discord_id, notification_time, attacks_remaining}},
                              "channel_notifications": {guild_id: {notification_time, clan_name, opponent_name}}}
        
        This tracks which players/channels have been notified for which wars to prevent duplicate notifications.
        """
        if not self.db_manager:
            raise RuntimeError("Database manager not initialized")
        
        try:
            self.notification_state = await self.db_manager.load_notification_state()
            logging.info(f"[DB-READ] Loaded notification state for {len(self.notification_state)} wars from database")
        except Exception as e:
            logging.error(f"[DB-READ] Failed to load notification state from database: {e}")
            raise SystemExit(1)

    async def load_server_config(self) -> None:
        """
        Load server configuration from database.
        
        Structure: guild_id -> {"newbie_role_id": str, "member_role_id": str, "member_clans": List[str], "member_families": List[str]}
        
        Raises:
            SystemExit: On critical loading errors
        """
        if not self.db_manager:
            raise RuntimeError("Database manager not initialized")
        
        try:
            self.server_config = await self.db_manager.get_all_guild_configs_dict()
            logging.info(f"[DB-READ] Loaded config for {len(self.server_config)} guilds from database")
        except Exception as e:
            logging.error(f"[DB-READ] Failed to load guild config from database: {e}")
            raise SystemExit(1)
    
    async def validate_clan_cache_consistency(self) -> None:
        """
        Validate consistency between server_config and clan_name_cache.
        
        Ensures that all clans referenced in server_config.member_clans also exist
        in clan_name_cache. If any are missing, adds them with placeholder data.
        
        Also evicts any family tags that accidentally ended up in clan_name_cache.
        Family tags are not real CoC clans and must not be polled in PHASE-1.
        
        This handles edge cases where:
        - Manual data manipulation removes clans from clan_name_cache
        - Data corruption or incomplete updates
        
        Missing clans are added with minimal data and logged as warnings for investigation.
        """
        # Step 0: Evict any family tags that accidentally ended up in clan_name_cache.
        # subscriptions.clan_tag can hold family tags (intentional), but _ensure_clan_exists
        # should never insert them into the clans table. If one slips through (e.g. leftover
        # from a previous bug), remove it from both in-memory cache and the clans table so
        # PHASE-1 does not try to poll it via the CoC API.
        family_tags_in_cache = [
            tag for tag in list(self.clan_name_cache.keys())
            if tag in self.clan_families
        ]
        if family_tags_in_cache:
            logging.warning(
                f"[CONSISTENCY-CHECK] Found {len(family_tags_in_cache)} family tag(s) in clan_name_cache "
                f"(should never be there): {family_tags_in_cache}. Evicting."
            )
            for tag in family_tags_in_cache:
                self.clan_name_cache.pop(tag, None)
                # Also remove from clans table so it does not re-appear after restart
                if self.db_manager:
                    try:
                        await self.db_manager.delete_clan(tag)
                        logging.info(f"[CONSISTENCY-CHECK] Evicted family tag {tag} from clan_name_cache and clans table")
                    except Exception as e:
                        logging.error(f"[CONSISTENCY-CHECK] Failed to delete family tag {tag} from clans table: {e}")

        missing_clans = []
        
        for guild_id, config_data in self.server_config.items():
            member_clans = config_data.get('member_clans', [])
            
            for clan_tag in member_clans:  # type: ignore[union-attr]
                if clan_tag not in self.clan_name_cache:
                    missing_clans.append((guild_id, clan_tag))  # type: ignore[arg-type]
        
        if missing_clans:
            logging.warning(f"[CONSISTENCY-CHECK] Found {len(missing_clans)} clan(s) in server_config but not in clan_name_cache")  # type: ignore[arg-type]
            
            for guild_id, clan_tag in missing_clans:  # type: ignore[misc]
                logging.warning(f"[CONSISTENCY-CHECK] Adding missing clan {clan_tag} from guild {guild_id} to clan_name_cache")
                
                # Add placeholder entry with unknown name
                self.clan_name_cache[clan_tag] = {  # type: ignore[index]
                    "name": "Unknown (auto-added)",
                    "has_active_subscriptions": False,
                    "last_war_update": None,
                    "warlog_is_public": True
                }
            
            # Save updated clan_name_cache
            for _, clan_tag in missing_clans:  # type: ignore[misc]
                await self.persist_clan(clan_tag)  # type: ignore[arg-type]
            logging.info(f"[CONSISTENCY-CHECK] Added {len(missing_clans)} missing clan(s) to clan_name_cache")  # type: ignore[arg-type]
        else:
            logging.debug("[CONSISTENCY-CHECK] All clans in server_config exist in clan_name_cache")
    
    async def set_server_config(self, guild_id: str, config_data: Dict[str, Any]) -> None:
        """Set server config with immediate DB persistence (write-through).
        
        Raises:
            Exception: If database write fails (cache is still updated)
        """
        try:
            # Update in-memory cache
            self.server_config[guild_id] = config_data

            # Immediately persist to database
            await self.db_manager.save_guild_config(guild_id, config_data)  # type: ignore[union-attr]
            logging.debug(f"[DB-WRITE-THROUGH] Persisted server config for guild: {guild_id}")
            # Guild member_clans/families may have changed → recalculate tracking
            await self.update_all_clan_subscription_statuses()
        except Exception as e:
            logging.error(f"[DB-WRITE-THROUGH] Failed to persist server config for guild {guild_id}: {e}")
            raise

    async def persist_server_config(self, guild_id: str) -> None:
        """Persist an existing in-memory server config to DB after in-place modification.
        
        Raises:
            Exception: If database write fails
        """
        if guild_id not in self.server_config:
            logging.warning(f"[DB-WRITE] Cannot persist server config for guild {guild_id}: not found in cache")
            return
        try:
            await self.db_manager.save_guild_config(guild_id, self.server_config[guild_id])  # type: ignore[union-attr]
            logging.debug(f"[DB-WRITE-THROUGH] Persisted server config (in-place): guild {guild_id}")
            # Guild member_clans/families may have changed → recalculate tracking
            await self.update_all_clan_subscription_statuses()
        except Exception as e:
            logging.error(f"[DB-WRITE-THROUGH] Failed to persist server config for guild {guild_id}: {e}")
            raise

    # ─── Notification State Write-Through Methods ─────────────────────

    async def persist_player_notification(self, war_id: str, player_tag: str) -> None:
        """Persist a single player notification record to DB (write-through).
        
        Called after updating CACHE.notification_state[war_id]["notified_players"][player_tag] in-place.
        """
        war_state = self.notification_state.get(war_id, {})
        player_data = war_state.get("notified_players", {}).get(player_tag)
        if not player_data:
            logging.warning(f"[DB-WRITE] Cannot persist player notification for war {war_id}, player {player_tag}: not found in cache")
            return
        await self.db_manager.save_player_notification(  # type: ignore[union-attr]
            war_key=war_id,
            player_tag=player_tag,
            player_name=player_data["player_name"],
            discord_id=player_data["discord_id"],
            notification_time=player_data["notification_time"],
            attacks_remaining=player_data["attacks_remaining"],
        )
        logging.debug(f"[DB-WRITE] Persisted player notification: war={war_id}, player={player_tag}")

    async def persist_channel_notification(self, war_id: str, guild_id: str) -> None:
        """Persist a single channel notification record to DB (write-through).
        
        Called after updating CACHE.notification_state[war_id]["channel_notifications"][guild_id] in-place.
        """
        war_state = self.notification_state.get(war_id, {})
        channel_data = war_state.get("channel_notifications", {}).get(guild_id)
        if not channel_data:
            logging.warning(f"[DB-WRITE] Cannot persist channel notification for war {war_id}, guild {guild_id}: not found in cache")
            return
        await self.db_manager.save_channel_notification(  # type: ignore[union-attr]
            war_key=war_id,
            guild_id=guild_id,
            notification_time=channel_data["notification_time"],
            clan_name=channel_data["clan_name"],
            opponent_name=channel_data["opponent_name"],
        )
        logging.debug(f"[DB-WRITE] Persisted channel notification: war={war_id}, guild={guild_id}")

    # ─── Clan Name Cache Write-Through Methods ───────────────────────

    async def persist_clan(self, clan_tag: str) -> None:
        """Persist a single clan entry to DB after in-place modification (write-through).
        
        Called after modifying self.clan_name_cache[clan_tag] in-place (name, warlog status, etc.).
        
        Args:
            clan_tag: The clan tag to persist (e.g., "#2C9UR9GJY")
        
        Raises:
            Exception: If database write fails
        """
        clan_data = self.clan_name_cache.get(clan_tag)
        if not clan_data:
            logging.warning(f"[DB-WRITE] Cannot persist clan {clan_tag}: not found in cache")
            return
        if not isinstance(clan_data, dict):  # type: ignore[misc]
            logging.warning(f"[DB-WRITE] Cannot persist clan {clan_tag}: unexpected non-dict format")
            return
        try:
            await self.db_manager.save_clan(  # type: ignore[union-attr]
                clan_tag=clan_tag,
                name=str(clan_data.get("name", "Unknown")),
                has_active_subscriptions=bool(clan_data.get("has_active_subscriptions", False)),
                last_war_update=clan_data.get("last_war_update") if isinstance(clan_data.get("last_war_update"), str) else None,
                warlog_is_public=bool(clan_data.get("warlog_is_public", True)),
                last_checked_via_api=clan_data.get("last_checked_via_api") if isinstance(clan_data.get("last_checked_via_api"), str) else None,
                war_league=clan_data.get("war_league") if isinstance(clan_data.get("war_league"), str) else None,
                track_war_updates=clan_data.get("track_war_updates"),  # None → DB default 1
                is_deleted=clan_data.get("is_deleted"),  # None → DB preserves existing (default 0)
            )
            logging.debug(f"[DB-WRITE-THROUGH] Persisted clan: {clan_tag}")
        except Exception as e:
            logging.error(f"[DB-WRITE-THROUGH] Failed to persist clan {clan_tag}: {e}")
            raise

    async def delete_clan_from_cache(self, clan_tag: str) -> None:
        """Delete a clan from both in-memory cache and DB (write-through).
        
        Args:
            clan_tag: The clan tag to delete
        
        Raises:
            Exception: If database delete fails (cache entry already removed)
        """
        try:
            if clan_tag in self.clan_name_cache:
                del self.clan_name_cache[clan_tag]
            await self.db_manager.delete_clan(clan_tag)  # type: ignore[union-attr]
            logging.debug(f"[DB-DELETE-THROUGH] Removed clan: {clan_tag}")
        except Exception as e:
            logging.error(f"[DB-DELETE-THROUGH] Failed to delete clan {clan_tag}: {e}")
            raise

    async def purge_clan_if_orphaned(self, clan_tag: str) -> bool:
        """Self-healing cleanup: hard-delete *clan_tag* from cache + DB, but only
        if nothing else in the database still refers to it.

        Intended to be called once a clan has been confirmed gone via the CoC
        API (404 NotFound) — see `_mark_clan_deleted()` in QBhelperfunctions.py.
        No-ops safely if the clan is still referenced elsewhere (active
        subscription, family membership, guild member clan, guild clan role,
        a registered player's current clan, or historical war data) or if the
        row is already gone.

        Args:
            clan_tag: The clan tag to conditionally purge

        Returns:
            True if the row was actually deleted, False otherwise.
        """
        try:
            deleted = await self.db_manager.delete_clan_if_unreferenced(clan_tag)  # type: ignore[union-attr]
            if deleted and clan_tag in self.clan_name_cache:
                del self.clan_name_cache[clan_tag]
            return deleted
        except Exception as e:
            logging.error(f"[DB-CLEANUP] Failed to purge orphaned clan {clan_tag}: {e}")
            return False

    async def load_all(self) -> None:
        """Load all cache data from persistent storage."""
        self._current_load_operation = "load_clan_name_cache"
        logging.info("Starting cache load_all() - loading clan names...")
        await self.load_clan_name_cache()
        self._current_load_operation = "load_subscriptions"
        logging.info("Starting cache load_all() - loading subscriptions...")
        await self.load_subscriptions()
        self._current_load_operation = "load_leaderboard_messages"
        logging.info("Starting cache load_all() - loading leaderboard messages...")
        await self.load_leaderboard_messages()
        self._current_load_operation = "load_clan_families"
        logging.info("Starting cache load_all() - loading clan families...")
        await self.load_clan_families()
        self._current_load_operation = "load_user_accounts"
        logging.info("Starting cache load_all() - loading user accounts...")
        await self.load_user_accounts()
        self._current_load_operation = "load_notification_state"
        logging.info("Starting cache load_all() - loading notification state...")
        await self.load_notification_state()
        self._current_load_operation = "load_server_config"
        logging.info("Starting cache load_all() - loading server config...")
        await self.load_server_config()
        self._current_load_operation = "validate_clan_cache_consistency"
        # Validate consistency between server_config and clan_name_cache
        logging.info("Starting cache load_all() - validating clan cache consistency...")
        await self.validate_clan_cache_consistency()
        self._current_load_operation = "load_all_temp_war_stats"
        # Load temp war stats off the event-loop thread (76k+ server-machine file reads would block Discord heartbeats)
        await asyncio.to_thread(self.load_all_temp_war_stats)
        # Initialize empty clan_history dict (loaded on-demand from database)
        self.clan_history = {}
        self._current_load_operation = "load_player_name_index"
        logging.info("Starting cache load_all() - loading player name index...")
        await self.load_player_name_index()
        self._current_load_operation = "update_all_clan_subscription_statuses"
        logging.info("Cache load_all() completed successfully")
        
        # Update subscription statuses after all data is loaded
        logging.info("Updating clan subscription statuses...")
        await self.update_all_clan_subscription_statuses()
        self._current_load_operation = None
        logging.info("Clan subscription status update complete")

    def save_war_object(self, coc_war_obj: 'coc.War', tracked_clan_tag: Optional[str] = None, force_save: bool = False) -> bool:  # type: ignore[name-defined]
        """
        Serialize and save a Clash of Clans war object to disk with comprehensive data capture.
        
        24-Hour Stale War Protection:
            Wars that ended more than 24 hours ago are NOT saved to prevent accumulating
            deteriorated data. The CoC API can return the same ended war repeatedly if a
            clan hasn't started a new war, and after 24 hours this data is considered stale.
            
            When skipped:
            - State is 'war_ended'
            - War end_time is more than 24 hours in the past
            - Logs: [STALE-WAR-SKIP] with hours since war ended
            
            This complements the manage_war_files() stale war deletion logic, which removes
            temp files for wars already in history AND archive (duplicate detection).

        Args:
            coc_war_obj (coc.War): The war object to serialize (must have clan/opponent attributes).
            tracked_clan_tag (str): Optional clan tag being tracked (e.g., '#L2J0C0PY'). 
                                    If provided, ensures this clan is first in filename regardless of API position.
            force_save (bool): If True, skip the 24-hour stale war check and the "already finalized"
                                check. Used by backfill_last_cwl_for_clan() to write historical CWL
                                wars that are older than 24 hours. Default: False.

        Returns:
            bool: True if the war object was successfully dumped, False if skipped or failed.
                  Returns False for stale wars (>24h), invalid data, or I/O errors.

        Side Effects:
            - Writes to 'data/temp/{CLAN_TAG}_{OPPONENT_TAG}_{YYYYMMDDHHMM}_war_data.json'
            - Skips save and logs [STALE-WAR-SKIP] for wars ended >24 hours ago
            - Logs warnings for invalid clan/opponent data

        Example:
            success = save_war_object(coc_war_obj, tracked_clan_tag='#L2J0C0PY')
            # Ensures L2J0C0PY is always first in filename
        """
        # Enhanced logging for debugging war object issues
        war_state_obj = getattr(coc_war_obj, "state", None)  # type: ignore[arg-type]
        war_state = str(war_state_obj.name if war_state_obj and hasattr(war_state_obj, "name") else "unknown")
        my_clan = getattr(coc_war_obj, "clan", None)  # type: ignore[arg-type]
        enemy_clan = getattr(coc_war_obj, "opponent", None)  # type: ignore[arg-type]
        
        # CRITICAL: Normalize data structure for tracked clan
        # If tracked_clan_tag provided and API returned our clan in opponent position,
        # swap my_clan/enemy_clan so our tracked clan is ALWAYS treated as "my_clan"
        # This ensures consistent filename AND JSON structure
        if tracked_clan_tag and enemy_clan and getattr(enemy_clan, "tag", None) == tracked_clan_tag:
            logging.debug(f"[SAVE-WAR] Tracked clan {tracked_clan_tag} in opponent position - swapping for consistency")
            my_clan, enemy_clan = enemy_clan, my_clan
        
        my_clan_tag = getattr(my_clan, 'tag', None) if my_clan else None
        enemy_clan_tag = getattr(enemy_clan, 'tag', None) if enemy_clan else None
        my_clan_name = getattr(my_clan, 'name', 'Unknown') if my_clan else 'None'
        enemy_clan_name = getattr(enemy_clan, 'name', 'Unknown') if enemy_clan else 'None'
        
        if not my_clan or not enemy_clan or not my_clan_tag or not enemy_clan_tag:
            logging.warning(
                f"Invalid clan or opponent data in war object: "
                f"state={war_state}, "
                f"clan_tag={my_clan_tag}, clan_name='{my_clan_name}', "
                f"opponent_tag={enemy_clan_tag}, opponent_name='{enemy_clan_name}', "
                f"war_obj_type={type(coc_war_obj).__name__}"  # type: ignore[arg-type]
            )
            return False

        # Friendly wars are never tracked — skip unconditionally regardless of state
        _war_type_raw = getattr(coc_war_obj, "type", None)  # type: ignore[arg-type]
        if str(_war_type_raw) == "friendly":
            logging.info(
                f"[FRIENDLY-SKIP] Skipping JSON save for {my_clan_tag} vs {enemy_clan_tag}: "
                f"friendly wars are never tracked"
            )
            self.record_cycle_stat("save_skip_friendly")
            return False

        # Calculate file tags early (needed for checks below)
        my_file_tag = re.sub(r'[^A-Z0-9]', '', my_clan.tag.upper())
        enemy_file_tag = re.sub(r'[^A-Z0-9]', '', enemy_clan.tag.upper())

        # Extract war start timestamp for filename uniqueness.
        # Filename format: {MY_TAG}_{OPP_TAG}_{YYYYMMDDHHMM}_war_data.json
        # Falls back to UTC-now so the file is at least unique (extremely rare).
        _war_start_ts: str = ""
        try:
            _wst = getattr(coc_war_obj, 'start_time', None)  # type: ignore[arg-type]
            if _wst:
                _wst_dt = _wst.time if hasattr(_wst, 'time') else _wst
                _war_start_ts = _wst_dt.strftime("%Y%m%d%H%M")
        except Exception:
            pass
        if not _war_start_ts:
            from datetime import datetime, timezone as _tz_fallback
            _war_start_ts = datetime.now(_tz_fallback.utc).strftime("%Y%m%d%H%M")

        # Edge case: end_time is >24h in the past but state is NOT war_ended.
        # The API is returning a stale/corrupt snapshot — the war should have ended
        # but the state never transitioned. Do not write; a proper war_ended snapshot
        # will follow once the API corrects itself.
        # force_save=True bypasses this check for historical CWL backfills.
        if war_state != 'war_ended' and not force_save:
            war_end_time = getattr(coc_war_obj, 'end_time', None)  # type: ignore[arg-type]
            if war_end_time:
                try:
                    from datetime import datetime, timezone, timedelta
                    if hasattr(war_end_time, 'time'):
                        end_dt = war_end_time.time
                    else:
                        end_dt = war_end_time

                    now = datetime.now(timezone.utc)
                    time_since_end = now - end_dt.replace(tzinfo=timezone.utc)

                    if time_since_end > timedelta(hours=24):
                        hours_past = time_since_end.total_seconds() / SECONDS_PER_HOUR
                        logging.warning(
                            f"[STALE-INVALID-SKIP] Skipping JSON save for {my_clan_tag} vs {enemy_clan_tag}: "
                            f"end_time is {hours_past:.1f}h in the past "
                            f"but state={war_state!r} — corrupt/stale API response"
                        )
                        self.record_cycle_stat("save_skip_stale")
                        try:
                            import json as _json
                            _inv_dir = CONFIG.investigate_dir
                            os.makedirs(_inv_dir, exist_ok=True)
                            _inv_file = os.path.join(
                                _inv_dir,
                                f"{my_file_tag}_{enemy_file_tag}_{_war_start_ts}_stale_invalid.json"
                            )
                            with open(_inv_file, 'w', encoding='utf-8') as _f:
                                _json.dump({
                                    "reason": "stale_invalid",
                                    "clan_tag": my_clan_tag,
                                    "opponent_tag": enemy_clan_tag,
                                    "state": war_state,
                                    "hours_past_end": round(hours_past, 1),
                                    "war_start_ts": _war_start_ts,
                                }, _f, indent=2)
                        except Exception as _inv_ex:
                            logging.warning(f"[INVESTIGATE-WRITE] Could not write investigate file: {_inv_ex}")
                        return False
                except Exception as time_ex:
                    logging.warning(f"[STALE-INVALID-CHECK] Could not parse war end time: {time_ex} - proceeding with save")

        # CRITICAL: Skip saving only when all three conditions are true:
        #   1. war file already exists in archive
        #   2. war already recorded in history (DB)
        #   3. DB state is war_ended
        # If any condition is unmet, write the temp file so the next cycle can
        # update history and/or archive with the latest data.
        # force_save=True bypasses this check for historical CWL backfills.
        # Edge case: fully finalized but API returns state != war_ended → WARNING logged.
        # Pre-compute CWL war tag — needed by both the FINALIZED-WAR-SKIP check
        # below and the filename build section that follows, so it must be resolved
        # before either block runs.
        _cwl_war_tag_raw = getattr(coc_war_obj, "war_tag", None)  # type: ignore[arg-type]
        _cwl_war_tag_clean = re.sub(r'[^A-Z0-9]', '', str(_cwl_war_tag_raw).upper()) if _cwl_war_tag_raw else None
        _is_cwl_war = bool(getattr(coc_war_obj, "is_cwl", False)) and bool(_cwl_war_tag_clean)  # type: ignore[arg-type]

        if not force_save:
            try:
                import QBhelperfunctions
                # Build war_id: OPPONENT_TAG_YYYYMMDDHHMM (uses _war_start_ts computed above)
                war_id = f"{enemy_file_tag}_{_war_start_ts}"

                # Check archive file with timestamped naming convention.
                # Regular wars use a 3-part filename; CWL wars use a 4-part filename
                # that appends the war_tag.  Try 3-part first then fall back to 4-part
                # so both war types hit the skip guard correctly.
                import QBcsvhandling as _qbc_shard
                archive_file = os.path.join(
                    _qbc_shard.get_war_shard_dir(my_file_tag, CONFIG.archive_dir),
                    f"{my_file_tag}_{enemy_file_tag}_{_war_start_ts}_war_data.json"
                )

                _in_archive  = os.path.exists(archive_file)
                # CWL wars are stored with a 4-part filename — also check that.
                if not _in_archive and _is_cwl_war:
                    _archive_file_cwl = os.path.join(
                        _qbc_shard.get_war_shard_dir(my_file_tag, CONFIG.archive_dir),
                        f"{my_file_tag}_{enemy_file_tag}_{_war_start_ts}_{_cwl_war_tag_clean}_war_data.json"
                    )
                    if os.path.exists(_archive_file_cwl):
                        archive_file = _archive_file_cwl
                        _in_archive = True
                _in_history  = _in_archive and QBhelperfunctions._is_war_in_history(my_clan_tag, war_id)  # type: ignore[attr-defined]
                _db_state    = self.db_manager.get_war_summary_state_sync(my_clan_tag, war_id) if _in_history else None  # type: ignore[union-attr]

                if _in_archive and _in_history and _db_state == "war_ended":
                    if war_state != "war_ended":
                        logging.warning(
                            f"[FINALIZED-WAR-UNEXPECTED] War {war_id} for {my_clan_tag} is fully finalized "
                            f"(archive+history+db=war_ended) but API returns state={war_state!r} — skipping save"
                        )
                    else:
                        logging.info(
                            f"[FINALIZED-WAR-SKIP] Skipping JSON save for {my_clan_tag} vs {enemy_clan_tag}: "
                            f"war {war_id} fully finalized (archive+history+db=war_ended)"
                        )
                    self.record_cycle_stat("save_skip_finalized")
                    return False
            except Exception as check_ex:
                logging.warning(f"[FINALIZED-CHECK] Could not check if war already finalized: {check_ex} - proceeding with save")

        # Build filename:
        #   Regular war: {OUR_CLAN}_{OPPONENT}_{YYYYMMDDHHMM}_war_data.json  (3-part)
        #   CWL war:     {OUR_CLAN}_{OPPONENT}_{YYYYMMDDHHMM}_{WAR_TAG}_war_data.json  (4-part)
        # 4-part lets process_orphaned_cwl_wars() extract the war_tag from the filename
        # directly, avoiding a JSON open for every file during the orphan scan.
        import QBcsvhandling as _qbc_shard
        _temp_base = os.path.join(CONFIG.data_dir, "temp")
        _temp_shard_dir = _qbc_shard.get_war_shard_dir(my_file_tag, _temp_base)
        if _is_cwl_war:
            temp_file_name = f"{my_file_tag}_{enemy_file_tag}_{_war_start_ts}_{_cwl_war_tag_clean}_war_data.json"
        else:
            temp_file_name = f"{my_file_tag}_{enemy_file_tag}_{_war_start_ts}_war_data.json"
        temp_file = os.path.join(_temp_shard_dir, temp_file_name)
        archive_path = _qbc_shard.get_war_shard_dir(my_file_tag, CONFIG.archive_dir)
        
        try:
            os.makedirs(_temp_shard_dir, exist_ok=True)
            os.makedirs(archive_path, exist_ok=True)
        except Exception as dir_ex:
            logging.error(f"Could not create directories: {dir_ex}")
            return False
        
        # Always write to temp directory (archives should be immutable)
        # Later, war_ended files will be moved to archive after finalization
        target_file = temp_file
        
        try:
            def simple_attack(a):  # type: ignore[no-untyped-def]
                # Defensive: handle WarAttack objects and dicts
                if not a:
                    return None
                if hasattr(a, "attacker_tag") or hasattr(a, "defender_tag"):  # type: ignore[arg-type]
                    return {
                        "attackerTag": getattr(a, "attacker_tag", ""),  # type: ignore[arg-type]
                        "defenderTag": getattr(a, "defender_tag", ""),  # type: ignore[arg-type]
                        "stars": getattr(a, "stars", 0),  # type: ignore[arg-type]
                        "destruction": getattr(a, "destruction", 0),  # type: ignore[arg-type]
                        "order": getattr(a, "order", 0),  # type: ignore[arg-type]
                        "duration": getattr(a, "duration", None),  # type: ignore[arg-type]
                        "fresh": getattr(a, "fresh", None)  # type: ignore[arg-type]
                    }
                elif isinstance(a, dict):
                    return {
                        "attackerTag": a.get("attacker_tag", ""),  # type: ignore[union-attr]
                        "defenderTag": a.get("defender_tag", ""),  # type: ignore[union-attr]
                        "stars": a.get("stars", 0),  # type: ignore[union-attr]
                        "destruction": a.get("destruction", 0),  # type: ignore[union-attr]
                        "order": a.get("order", 0),  # type: ignore[union-attr]
                        "duration": a.get("duration", None),  # type: ignore[union-attr]
                        "fresh": a.get("fresh", None)  # type: ignore[union-attr]
                    }
                else:
                    return str(a)  # type: ignore[arg-type]

            def find_best_opponent_attack(member_tag, opponent_members):  # type: ignore[no-untyped-def]
                """Find the best (highest stars) opponent attack against a member."""
                best_attack = None
                best_stars = -1
                
                for opp_member in opponent_members:  # type: ignore[attr-defined]
                    for attack in (getattr(opp_member, "attacks", []) or []):  # type: ignore[arg-type]
                        # attacker_tag = getattr(attack, "attacker_tag", None)  # Unused variable
                        defender_tag = getattr(attack, "defender_tag", None)
                        stars = getattr(attack, "stars", 0)
                        
                        if defender_tag == member_tag:
                            if stars > best_stars:
                                best_stars = stars
                                best_attack = attack
                
                return simple_attack(best_attack) if best_attack else None  # type: ignore[return-value]
            
            def calculate_defensive_stars(member_tag, opponent_members):  # type: ignore[no-untyped-def]
                """Calculate defensive stars for a member by counting ALL opponent attacks."""
                defensive_stars = 0
                for opp_member in opponent_members:  # type: ignore[attr-defined]
                    for attack in (getattr(opp_member, "attacks", []) or []):  # type: ignore[arg-type]
                        defender_tag = getattr(attack, "defender_tag", None)
                        stars = getattr(attack, "stars", 0)
                        
                        if defender_tag == member_tag:
                            defensive_stars += stars
                return defensive_stars

            def simple_member(m, opponent_members):  # type: ignore[no-untyped-def]
                if not m:
                    return None
                attacks_list = getattr(m, "attacks", [])  # type: ignore[arg-type]
                member_tag = getattr(m, "tag", "")  # type: ignore[arg-type]
                
                # Calculate defensive stats by iterating through opponent attacks
                # This ensures we capture ALL attacks including late attacks in CWL
                best_opp_attack = find_best_opponent_attack(member_tag, opponent_members)  # type: ignore[arg-type]
                total_defensive_stars = calculate_defensive_stars(member_tag, opponent_members)  # type: ignore[arg-type]
                
                # Safely get previous_best_opponent_attack with exception handling
                previous_best_attack = None
                try:
                    previous_best_attack = getattr(m, "previous_best_opponent_attack", None)  # type: ignore[arg-type]
                except ValueError:
                    # Handle case where coc.py throws ValueError: max() iterable argument is empty
                    previous_best_attack = None
                
                return {  # type: ignore[return-value]
                    "tag": member_tag,
                    "name": getattr(m, "name", ""),  # type: ignore[arg-type]
                    "townhall": getattr(m, "town_hall", getattr(m, "townhallLevel", 0)),  # type: ignore[arg-type]
                    "map_position": getattr(m, "map_position", 0),  # type: ignore[arg-type]
                    "role": getattr(m, "role", None),  # type: ignore[arg-type]
                    "donated": getattr(m, "donated", None),  # type: ignore[arg-type]
                    "received": getattr(m, "received", None),  # type: ignore[arg-type]
                    "attacks": [simple_attack(a) for a in attacks_list if a is not None],
                    "opponentAttacks": total_defensive_stars,
                    "bestOpponentAttack": best_opp_attack,
                    "previousBestOpponentAttack": simple_attack(previous_best_attack)
                }

            def simple_badge(badge):  # type: ignore[no-untyped-def]
                """Handle Badge objects by extracting serializable data."""
                if badge is None:
                    return None
                if hasattr(badge, 'name') or hasattr(badge, 'url'):  # type: ignore[arg-type]
                    return {
                        "name": getattr(badge, "name", ""),  # type: ignore[arg-type]
                        "url": getattr(badge, "url", ""),  # type: ignore[arg-type]
                        "small": getattr(badge, "small", ""),  # type: ignore[arg-type]
                        "medium": getattr(badge, "medium", ""),  # type: ignore[arg-type]
                        "large": getattr(badge, "large", "")  # type: ignore[arg-type]
                    }
                else:
                    return str(badge)  # type: ignore[arg-type]

            def simple_clan(c, opponent_members):  # type: ignore[no-untyped-def]
                """Serialize clan with proper defensive stars calculation."""
                if not c:
                    return None
                badge_data = simple_badge(getattr(c, "badge", None))  # type: ignore[arg-type]
                members_list = getattr(c, "members", [])  # type: ignore[arg-type]
                
                return {  # type: ignore[return-value]
                    "tag": getattr(c, "tag", ""),  # type: ignore[arg-type]
                    "name": getattr(c, "name", ""),  # type: ignore[arg-type]
                    "level": getattr(c, "level", getattr(c, "clanLevel", 0)),  # type: ignore[arg-type]
                    "badge": badge_data,
                    "stars": getattr(c, "stars", 0),  # type: ignore[arg-type]
                    "destruction": getattr(c, "destruction", getattr(c, "destructionPercentage", 0)),  # type: ignore[arg-type]
                    "attack_wins": getattr(c, "attack_wins", None),  # type: ignore[arg-type]
                    "attacks_used": getattr(c, "attacks_used", None),  # type: ignore[arg-type]
                    "wins": getattr(c, "wins", None),  # type: ignore[arg-type]
                    "members": [simple_member(m, opponent_members) for m in members_list if m is not None]  # type: ignore[arg-type, misc]
                }

            # Extract league group information if available
            league_group = getattr(coc_war_obj, "league_group", None)  # type: ignore[arg-type]
            league_group_data = None
            if league_group:
                league_group_data = {  # type: ignore[var-annotated]
                    "tag": getattr(league_group, "tag", ""),  # type: ignore[arg-type]
                    "state": str(getattr(league_group, "state", "")),  # type: ignore[arg-type]
                    # Normalise season so mid-month CWLs that span Mon–Wed share one key.
                    "season": normalize_cwl_season(str(getattr(league_group, "season", "") or ""))  # type: ignore[arg-type]
                }

            # Extract member lists for cross-clan defensive star calculation
            # After swap above, my_clan/enemy_clan are already in correct positions
            # my_clan = our tracked clan, enemy_clan = opponent
            clan_obj = my_clan
            opponent_obj = enemy_clan
            clan_members = getattr(clan_obj, "members", []) if clan_obj else []
            opponent_members = getattr(opponent_obj, "members", []) if opponent_obj else []

            war_state_obj = getattr(coc_war_obj, "state", None)  # type: ignore[arg-type]
            payload = {  # type: ignore[var-annotated]
                "state": str(war_state_obj.name if war_state_obj and hasattr(war_state_obj, "name") else "unknown"),
                "team_size": getattr(coc_war_obj, "team_size", 0),  # type: ignore[arg-type]
                "attacks_per_member": getattr(coc_war_obj, "attacks_per_member", 0),  # type: ignore[arg-type]
                "type": str(getattr(coc_war_obj, "type", None)) if getattr(coc_war_obj, "type", None) else None,  # type: ignore[arg-type]
                "is_cwl": getattr(coc_war_obj, "is_cwl", None),  # type: ignore[arg-type]
                "war_tag": getattr(coc_war_obj, "war_tag", None),  # type: ignore[arg-type]
                "start_time": str(getattr(coc_war_obj, "start_time", "")),  # type: ignore[arg-type]
                "end_time": str(getattr(coc_war_obj, "end_time", "")),  # type: ignore[arg-type]
                "preparation_start_time": str(getattr(coc_war_obj, "preparation_start_time", "")),  # type: ignore[arg-type]
                "league_group": league_group_data,
                "clan": simple_clan(clan_obj, opponent_members),  # type: ignore[arg-type]
                "opponent": simple_clan(opponent_obj, clan_members),  # type: ignore[arg-type]
                "attacks": [simple_attack(a) for a in getattr(coc_war_obj, "attacks", []) if a is not None]  # type: ignore[arg-type, misc]
            }
            
            # Write to target file (temp or archive, based on earlier determination).
            # Atomic write-to-tmp + os.replace() (same pattern as
            # persist_cwl_recovery_file() in QBhelperfunctions.py) — added 2026-08-01
            # after the disk-full incident corrupted several temp war files.
            # open(target_file, "w") truncates the file to 0 bytes BEFORE writing a
            # single byte of new content; if the write then fails partway (disk full,
            # crash, etc.), the previously-good content is already destroyed with
            # nothing valid left in its place. Writing to a sibling .tmp file first
            # means a failed write leaves the existing good file completely untouched;
            # os.replace() only swaps it in once the new content is fully and
            # successfully written, and is atomic on the same filesystem/directory.
            _file_is_new = not os.path.exists(target_file)
            _tmp_target_file = target_file + ".tmp"
            try:
                with open(_tmp_target_file, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2, ensure_ascii=False)
                os.replace(_tmp_target_file, target_file)
            except Exception:
                try:
                    os.remove(_tmp_target_file)
                except OSError:
                    pass
                raise
            
            # Cache the full war object payload so get_current_war_data() can
            # return it without re-reading the JSON from disk.
            # Skip war_ended — those wars are immediately queued for finalization
            # and the JSON is read from disk anyway.  Caching them inflates
            # temp_war_objects by ~40 KB × 10k entries = 400 MB during CWL season.
            # NOTE: coc.WarState.war_ended.name == "war_ended" (snake_case), so we
            # normalise by stripping underscores before comparing.
            if my_clan_tag and war_state.lower().replace('_', '') != 'warended':
                self.temp_war_objects[my_clan_tag] = payload
                # Evict the oldest entry (FIFO) if the cap is exceeded.
                # Single-entry eviction is optimal: polling is uniform across all clans,
                # so no eviction strategy can outperform keeping the most-recently-written
                # N entries. Disk fallback in get_current_war_data() handles any cache miss.
                if len(self.temp_war_objects) > _MAX_TEMP_WAR_OBJECTS:
                    try:
                        self.temp_war_objects.pop(next(iter(self.temp_war_objects)))
                    except StopIteration:
                        pass
            elif my_clan_tag:
                # Evict any stale in_war entry so get_current_war_data() falls back to disk.
                self.temp_war_objects.pop(my_clan_tag, None)
            
            # Keep filepath, state and end_time current in temp_war_metadata so
            # war_notifications can build war_data entirely from in-memory cache
            # without touching the filesystem.
            if my_clan_tag:
                _state_lower = war_state.lower() if war_state else "unknown"
                _war_type_raw = getattr(coc_war_obj, "type", None)  # type: ignore[arg-type]

                # Guard: don't clobber metadata that already tracks a NEWER war.
                # Phase-2 orphan processing writes a war_ended update for an old CWL
                # file while Phase-1 may have already written a newer in_war/prep file
                # for the same clan.  If we allowed the overwrite the metadata would
                # point to the old file; the subsequent _archive_and_log_war_file call
                # would see state=="war_ended" and evict the metadata, causing the clan
                # to vanish from the in_war / prep counters until the next restart.
                # Filename layout: SAFE_OPP_YYYYMMDDHHMM[_WARTAG]_war_data.json
                # Timestamp is at index [2] — lexicographic == chronological order.
                _existing_meta = self.temp_war_metadata.get(my_clan_tag)
                _existing_fp = _existing_meta.get("filepath", "") if _existing_meta else ""
                _should_update_meta = True
                if _existing_fp and _existing_fp != temp_file:
                    try:
                        _ex_parts = os.path.basename(_existing_fp).replace("_war_data.json", "").split("_")
                        _nw_parts = os.path.basename(temp_file).replace("_war_data.json", "").split("_")
                        _ex_ts = _ex_parts[2] if len(_ex_parts) >= 3 and _ex_parts[2].isdigit() else ""
                        _nw_ts = _nw_parts[2] if len(_nw_parts) >= 3 and _nw_parts[2].isdigit() else ""
                        if _ex_ts and _nw_ts and _ex_ts > _nw_ts:
                            _should_update_meta = False
                            logging.debug(
                                "[SAVE-WAR-META-SKIP] %s: skipping metadata update for older "
                                "file %s (current tracked: %s)",
                                my_clan_tag,
                                os.path.basename(temp_file),
                                os.path.basename(_existing_fp),
                            )
                    except Exception:
                        pass  # Any parse error → safe fallback: proceed with update

                if _should_update_meta:
                    self.temp_war_metadata.setdefault(my_clan_tag, {}).update({
                        "filepath": temp_file,
                        "state": _state_lower,
                        "end_time": str(getattr(coc_war_obj, "end_time", "")),  # type: ignore[arg-type]
                        "start_time": str(getattr(coc_war_obj, "start_time", "")),  # type: ignore[arg-type]
                        "type": str(_war_type_raw) if _war_type_raw else None,
                        "is_cwl": bool(getattr(coc_war_obj, "is_cwl", False)),  # type: ignore[arg-type]
                        "war_tag": getattr(coc_war_obj, "war_tag", None),  # type: ignore[arg-type]
                        "opponent_tag": getattr(getattr(coc_war_obj, "opponent", None), "tag", None) if hasattr(coc_war_obj, "opponent") else None,  # type: ignore[arg-type]
                    })
                    # Maintain fast active-war set used by war_notifications._get_active_wars()
                    if _state_lower == "in_war":
                        self.in_war_clan_tags.add(my_clan_tag)
                    else:
                        self.in_war_clan_tags.discard(my_clan_tag)
            
            # Note: File is kept in temp/ for war_ended state to allow finalization to read it
            # File will be moved to archive after finalization completes successfully
            
            # NOTE: War file cleanup and finalization is now handled by manage_war_files()
            # in QBhelperfunctions.py. This ensures atomic processing and prevents race conditions
            # where files could be archived before finalization completes.
            # See: QBhelperfunctions.manage_war_files() for unified war lifecycle management.
            self.record_cycle_stat("temp_written")
            self.record_cycle_stat("temp_new" if _file_is_new else "temp_updated")
            return True
            
        except Exception as ex:
            logging.error(f"Failed to save war object to {target_file}: {ex}")
            import traceback
            logging.error(f"Stack trace: {traceback.format_exc()}")
            return False

    def get_current_war_data(self, clan_tag: str) -> Any:
        """
        Return the latest war object for a given clan_tag.

        Prefers the in-memory ``temp_war_objects`` cache (populated by
        ``save_war_object()``).  Falls back to reading from disk only when
        the cache is cold (e.g., first access after restart for a clan
        whose JSON was not loaded during startup).
        """
        # Fast-path: in-memory cache (populated by save_war_object)
        cached = self.temp_war_objects.get(clan_tag)
        if cached is not None:
            return cached

        # Slow-path: read from disk (cold cache / startup edge case)
        temp_path = os.path.join(CONFIG.data_dir, "temp")
        file_tag = re.sub(r'[^A-Z0-9]', '', clan_tag.upper())

        # First try the exact filepath tracked in metadata (fast-path, sharding-safe).
        md = self.temp_war_metadata.get(clan_tag)
        md_path = md.get("filepath") if md else None
        if isinstance(md_path, str) and md_path:
            try:
                if os.path.exists(md_path):
                    with open(md_path, "r", encoding="utf-8") as f:
                        return json.load(f)
            except Exception as e:
                logging.error(f"Error loading war data from metadata path {md_path}: {e}")

        # Fallback search: prefer the computed shard path, then legacy flat temp/ layout.
        import QBcsvhandling as _qbc_shard
        shard_dir = _qbc_shard.get_war_shard_dir(file_tag, temp_path)
        pattern_shard = os.path.join(shard_dir, f"{file_tag}_*_war_data.json")
        pattern_flat = os.path.join(temp_path, f"{file_tag}_*_war_data.json")

        war_files = glob.glob(pattern_shard)
        if not war_files:
            war_files = glob.glob(pattern_flat)

        if not war_files:
            logging.debug(f"No war data files found for clan {clan_tag} in temp directory.")
            return None

        # Sort files by modification time, newest first.
        war_files.sort(key=lambda x: os.path.getmtime(x), reverse=True)
        latest_file = war_files[0]
        try:
            with open(latest_file, "r", encoding="utf-8") as f:
                war_data = json.load(f)
            return war_data
        except Exception as e:
            logging.error(f"Error loading war data from {latest_file}: {e}")
            return None

    def get_channel_subscriptions(self, channel_id: str) -> List[Dict[str, Any]]:
        """
        Get subscriptions for a specific channel using the new guild-based structure.
        
        Args:
            channel_id (str): Discord channel ID
            
        Returns:
            List[Dict[str, Any]]: List of subscription entries for the channel
        """
        for guild_data in self.subscriptions.values():
            if channel_id in guild_data:
                return guild_data[channel_id]
        return []
    
    async def set_channel_subscriptions(self, guild_id: str, channel_id: str, subscriptions: List[Dict[str, Any]]) -> None:
        """
        Set subscriptions for a specific channel using the new guild-based structure (write-through).
        
        Args:
            guild_id (str): Discord guild ID
            channel_id (str): Discord channel ID
            subscriptions (List[Dict[str, Any]]): List of subscription entries
        """
        # Use write-through method
        await self.set_subscriptions_for_channel(guild_id, channel_id, subscriptions)
    
    async def remove_channel_subscriptions(self, channel_id: str) -> bool:
        """
        Remove all subscriptions for a specific channel from the guild-based structure (write-through).
        
        Args:
            channel_id (str): Discord channel ID
            
        Returns:
            bool: True if channel was found and removed, False otherwise
        """
        # Find the guild that owns this channel
        for guild_id, guild_data in list(self.subscriptions.items()):
            if channel_id in guild_data:
                await self.delete_subscriptions_for_channel(guild_id, channel_id)
                return True
        return False
    
    def get_all_subscriptions_flat(self) -> Dict[str, List[Dict[str, Any]]]:
        """
        Get all subscriptions as a flat channel-to-subscription mapping.
        
        Returns:
            Dict[str, List[Dict[str, Any]]]: Channel ID -> subscription list mapping
        """
        flat_subs = {}  # type: ignore[var-annotated]
        for guild_data in self.subscriptions.values():
            for channel_id, sub_list in guild_data.items():
                flat_subs[channel_id] = sub_list
        return flat_subs  # type: ignore[return-value]
    
    async def get_current_war_from_api(self, clan_tag: str) -> Optional['coc.ClanWar']:
        """
        Get current war data from CoC API (no caching).
        
        This is a centralized wrapper for coc_client.get_current_war() that ensures
        all CoC API calls go through the cache manager for rate limiting.
        
        War data is NOT cached because it's always single-use and needs fresh data
        on each call for real-time war statistics.
        
        Args:
            clan_tag: Normalized clan tag (e.g., "#2C9UR9GJY")
        
        Returns:
            coc.ClanWar object or None if not in war
        
        Raises:
            coc.PrivateWarLog: If war log is private
            coc.NotFound: If clan doesn't exist
            coc.Maintenance: If CoC API is under maintenance
        
        Note:
            Rate limiting is handled automatically by coc.Client's BatchThrottler.
        """
        if not self.coc_client:
            raise RuntimeError("CoC API client not initialized. Call startup_login() first.")
        
        logging.debug(f"[COC-API-CALL] Fetching current war for clan {clan_tag} from API")
        return await coc_retry(
            lambda: self.coc_client.get_current_war(clan_tag),  # type: ignore[union-attr]
            operation_name=f"get_current_war({clan_tag})"
        )

    async def get_league_war(
        self,
        war_tag: str,
        *,
        max_age: float = 0,
        cache_result: bool = True,
        force_refresh: bool = False,
    ) -> 'coc.ClanWar':
        """
        Fetch CWL war data by war tag, with in-memory caching.

            Caching behaviour:
        - ``war_ended`` results are **immutable** and cached permanently
          (within the bot session).
        - Active states (``preparation``, ``in_war``) are returned from
          cache only when *max_age* > 0 **and** the cached entry is
          younger than *max_age* seconds.
        - ``max_age=0`` (default) always fetches from the API for
          non-ended wars, which is the safe default for callers that
          need fresh attack data.

        Args:
            war_tag:  War tag (e.g. ``"#12345ABC"``).
            max_age:  Maximum acceptable cache age in seconds for
                      non-ended wars.  ``0`` = always fetch fresh.
            cache_result: When ``False``, skip both cache read and cache write.
                          Use this for one-shot bulk backfills (for example CWL
                          recovery) so large historical batches do not inflate
                          in-memory cache usage across cycles.
            force_refresh: When ``True``, skip the cache read entirely (even
                          for an already-cached ``warended`` entry) and evict
                          any existing entry before fetching. Use this for a
                          deliberate retry of a war that came back with no
                          attack data — the CoC API is occasionally seen to
                          mark a CWL war ``warEnded`` slightly before its
                          attacks have fully replicated, and since ended wars
                          are normally cached as immutable, a plain retry
                          would otherwise just be served that same incomplete
                          snapshot again.

        Returns:
            ``coc.ClanWar`` object for the specific CWL war.

        Raises:
            RuntimeError: If CoC API client not initialised.
            coc.NotFound / coc.Maintenance: Propagated from CoC API.
        """
        if not self.coc_client:
            raise RuntimeError("CoC API client not initialized. Call startup_login() first.")

        # ── Check cache ──────────────────────────────────────────────────
        _now = time.time()
        if cache_result and force_refresh:
            self._league_war_cache.pop(war_tag, None)
        if cache_result and not force_refresh:
            cached = self._league_war_cache.get(war_tag)
            if cached is not None:
                war_obj, fetch_ts, cached_state = cached
                _age = _now - fetch_ts
                # war_ended is immutable → serve from cache if within TTL
                if cached_state == 'warended':
                    if _age < self._LEAGUE_WAR_CACHE_TTL:
                        return war_obj
                    # Expired — evict and re-fetch below
                    del self._league_war_cache[war_tag]
                else:
                    # For active states, serve from cache only if within max_age
                    if max_age > 0 and _age < max_age:
                        return war_obj

        # ── Fetch from API ───────────────────────────────────────────────
        logging.debug(f"[COC-API-CALL] Fetching CWL war {war_tag} from API")
        result = await coc_retry(
            lambda: self.coc_client.get_league_war(war_tag),  # type: ignore[union-attr]
            operation_name=f"get_league_war({war_tag})"
        )

        # ── Store in cache ───────────────────────────────────────────────
        _state = str(getattr(result, 'state', '') or '').lower().replace(' ', '')
        if cache_result:
            self._league_war_cache[war_tag] = (result, time.time(), _state)
        return result  # type: ignore[return-value]

    async def get_league_group(self, clan_tag: str, *, max_age: float = 0) -> 'coc.ClanWarLeagueGroup':  # type: ignore[name-defined]
        """
        Fetch a clan's current CWL league group from the CoC API, with caching.

        Caching behaviour:
        - The response is cached per *clan_tag*.
        - If ``max_age > 0`` and the cached entry is younger than
          *max_age* seconds, the cached value is returned.
        - ``max_age=0`` (default) always fetches fresh from the API.
        - Round-transition detection: after a fresh fetch the number
          of active rounds (those containing at least one real war tag)
          is stored.  Callers can compare this to the previous count to
          detect that a new CWL round has started.

        Args:
            clan_tag: Clan tag (e.g. ``'#L2J0C0PY'``).
            max_age:  Maximum acceptable cache age in seconds.

        Returns:
            ``coc.ClanWarLeagueGroup`` with ``.clans``, ``.season``,
            ``.state``, ``.rounds``, etc.

        Raises:
            RuntimeError: If CoC client not initialised.
            coc.NotFound / coc.Maintenance: Propagated from CoC API.
        """
        if not self.coc_client:
            raise RuntimeError("CoC API client not initialized. Call startup_login() first.")

        # ── Check cache ──────────────────────────────────────────────────
        cached = self._league_group_cache.get(clan_tag)
        if cached is not None and max_age > 0:
            lg_obj, fetch_ts, _round_cnt = cached
            if (time.time() - fetch_ts) < max_age:
                return lg_obj

        # ── Fetch from API ───────────────────────────────────────────────
        logging.debug(f"[COC-API-CALL] Fetching CWL league group for {clan_tag}")
        result = await coc_retry(
            lambda: self.coc_client.get_league_group(clan_tag),  # type: ignore[union-attr]
            operation_name=f"get_league_group({clan_tag})"
        )

        # ── Count active rounds (rounds with at least one real war tag) ─
        active_rounds = 0
        for rnd in getattr(result, 'rounds', []):
            war_tags = getattr(rnd, 'war_tags', []) or []
            if any(t and t != '#0' for t in war_tags):
                active_rounds += 1

        self._league_group_cache[clan_tag] = (result, time.time(), active_rounds)

        # Upsert cwl_league_rounds and backfill war_summary.round_number.
        # Normalise so mid-month CWLs whose start dates differ by a day still get
        # the same season key (snapped to the ISO Monday of that week).
        _lg_season = normalize_cwl_season(str(getattr(result, 'season', '') or ''))
        if _lg_season:
            try:
                await self._process_league_group_response(result, _lg_season)
            except Exception as _cb_ex:
                logging.warning(f"[CWL-ROUNDS] _process_league_group_response error: {_cb_ex}")

        return result  # type: ignore[return-value]

    @staticmethod
    def _make_league_group_id(clan_tags: List[str], cwl_season: str) -> str:
        """Return a stable 16-char hex ID for a CWL group, unique per season + clan set.

        Deterministic and order-independent: sorts tags before hashing so any
        ordering of the 8 clan_tags always yields the same result.  Season is
        included so the same 8 clans meeting again in a different season get a
        distinct ID.
        """
        sorted_tags = "|".join(sorted(t.lstrip("#").upper() for t in clan_tags))
        key = f"{cwl_season}:{sorted_tags}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    async def _process_league_group_response(self, lg: Any, cwl_season: str) -> None:
        """Upsert cwl_league_groups and cwl_league_rounds from a league group response.

        Called automatically after every fresh get_league_group() API response.
        Idempotent — INSERT OR IGNORE throughout.  After writing the new rows,
        runs a backfill UPDATE on war_summary to populate round_number for any
        already-finalised CWL wars in the same group.
        """
        try:
            _clan_objs = list(getattr(lg, "clans", []) or [])
            _clan_tags: List[str] = [
                str(getattr(c, "tag", "") or "") for c in _clan_objs
                if getattr(c, "tag", None)
            ]
            if len(_clan_tags) < 2:
                return  # Not a real group response

            _group_id = self._make_league_group_id(_clan_tags, cwl_season)

            # Collect (round_number, war_tag) for all revealed rounds.
            # Prefer _raw_data (available when coc client has raw_attribute=True).
            # Fall back to lg.rounds (List[List[str]]) — coc.py builds this by
            # filtering out all-#0 rounds, and CWL always reveals rounds
            # sequentially (1, 2, …) so enumerate index + 1 == round number.
            _revealed: List[Tuple[int, str]] = []
            _raw_data = getattr(lg, "_raw_data", None)
            if _raw_data and isinstance(_raw_data, dict):
                _raw_dict = cast(Dict[str, Any], _raw_data)
                for _r_idx, _rnd in enumerate(_raw_dict.get("rounds", []), start=1):
                    _rnd_dict = cast(Dict[str, Any], _rnd) if isinstance(_rnd, dict) else {}
                    for _wt in cast(List[Any], _rnd_dict.get("warTags") or []):
                        if _wt and str(_wt) != "#0":
                            _revealed.append((_r_idx, str(_wt)))
            else:
                # raw_attribute=False (default coc.Client) — use parsed rounds list.
                _rounds: List[Any] = list(getattr(lg, "rounds", None) or [])
                for _r_idx, _wt_list in enumerate(_rounds, start=1):
                    for _wt in (list(_wt_list) if _wt_list else []):
                        if _wt and str(_wt) != "#0":
                            _revealed.append((_r_idx, str(_wt)))

            if not _clan_tags:
                return

            db = self.db_manager
            if db is None:
                return

            # league_rank is populated ONLY at the moment this exact group is
            # discovered for the first time — checked BEFORE the upsert below,
            # so it reflects whether a row for this (group_id, season) existed
            # coming into this call. That moment is the only one where every
            # member is *guaranteed* to share one league: a freshly-discovered
            # group is necessarily still within its own active season. Any
            # later re-processing of an already-known group (new rounds
            # revealed, repeat polls, etc.) never touches league_rank again —
            # once a season ends, promotions/demotions apply almost
            # immediately and members can diverge to *different* current
            # leagues, so re-deriving from "any member's current league" at
            # that point is a coin flip, not a fact. This bit QapBot in
            # production twice (see changelog.txt 2026-07-26): a group's
            # league_rank got overwritten with a promoted/demoted member's
            # *post*-season league instead of the league the group actually
            # played. QBhelperfunctions.update_cwl_group_stats's self-heal is
            # the backstop for a group whose league_rank is still empty after
            # this (e.g. the one API call below failed) or, in the rare case
            # something else still gets it wrong — it only ever corrects using
            # a verified safe-rank cross-check, never a blind "any member" guess.
            _is_new_group = not await db.cwl_group_exists(_group_id, cwl_season)

            _new_rows = await db.upsert_cwl_league_data(
                league_group_id=_group_id,
                cwl_season=cwl_season,
                clan_tags=_clan_tags,
                rounds=_revealed,
            )

            _league_rank: str = ""
            if _is_new_group:
                # Try raw_data first — free if present. get_league_group() is
                # not currently known to include per-clan warLeague, but this
                # costs nothing to check in case that ever changes.
                if _raw_data and isinstance(_raw_data, dict):
                    _raw_data_d: Dict[str, Any] = cast(Dict[str, Any], _raw_data)
                    _clans_raw = cast(List[Any], _raw_data_d.get("clans") or [])
                    for _cr in _clans_raw:
                        _cr_dict = cast(Dict[str, Any], _cr) if isinstance(_cr, dict) else {}
                        _wl_raw = _cr_dict.get("warLeague")
                        _wl: Dict[str, Any] = cast(Dict[str, Any], _wl_raw) if isinstance(_wl_raw, dict) else {}
                        _wl_name: str = cast(str, _wl.get("name") or "")
                        if _wl_name:
                            _league_rank = _wl_name
                            break

                if not _league_rank:
                    # Fallback: clan_name_cache — but only trust reasonably fresh
                    # entries. A clan's cached war_league can be stale from long
                    # before this month's group even though the group itself is
                    # brand new (e.g. a passive Tier-3 clan not routinely
                    # re-polled). Accept a cached value only when
                    # last_checked_via_api is within 8 days; otherwise fire one
                    # get_clan() call for the first candidate clan — cheap in
                    # both compute and API budget, since every member of a
                    # brand-new group shares the same answer.
                    _STALE_DAYS = 8
                    _stale_cutoff = datetime.now(_dt_timezone.utc) - timedelta(days=_STALE_DAYS)
                    _refresh_candidate: str = ""
                    for _ct in _clan_tags:
                        _entry = self.clan_name_cache.get(_ct)
                        if isinstance(_entry, dict):
                            _lr = _entry.get("war_league") or ""
                            if _lr:
                                _last_chk: str = _entry.get("last_checked_via_api") or ""
                                _is_stale = True
                                if _last_chk:
                                    try:
                                        _lc_dt = datetime.fromisoformat(_last_chk)
                                        if _lc_dt.tzinfo is None:
                                            _lc_dt = _lc_dt.replace(tzinfo=_dt_timezone.utc)
                                        _is_stale = _lc_dt < _stale_cutoff
                                    except (ValueError, TypeError):
                                        _is_stale = True
                                if not _is_stale:
                                    _league_rank = str(_lr)  # fresh — use directly
                                    break
                                elif not _refresh_candidate:
                                    _refresh_candidate = _ct  # stale — remember for API call
                        elif not _refresh_candidate and _ct:
                            _refresh_candidate = _ct  # no entry at all → candidate

                    if not _league_rank:
                        # No fresh cache entry anywhere in the group — one
                        # targeted API call is enough.
                        _fetch_tag = _refresh_candidate or (_clan_tags[0] if _clan_tags else "")
                        if _fetch_tag:
                            try:
                                logging.info(
                                    f"[CWL-ROUNDS] group {_group_id}: new group, no "
                                    f"fresh cached war_league in the group — "
                                    f"refreshing via get_clan({_fetch_tag})"
                                )
                                await self.coc_clan_cache.get_clan(_fetch_tag)
                                # _update_clan_metadata (called by get_clan) writes the
                                # fresh war_league into clan_name_cache in-place.
                                _fresh = self.clan_name_cache.get(_fetch_tag)
                                if isinstance(_fresh, dict):
                                    _league_rank = str(_fresh.get("war_league") or "")
                            except Exception as _ge:
                                logging.warning(
                                    f"[CWL-ROUNDS] get_clan({_fetch_tag}) for "
                                    f"league_rank failed: {_ge}"
                                )

            if _league_rank:
                await db.update_cwl_league_rank(cwl_season, _group_id, _league_rank)
                await self._sync_group_track_war_updates(_clan_objs, _league_rank, cwl_season)

            _log_msg = (
                f"[CWL-ROUNDS] group {_group_id} season={cwl_season}: "
                f"{len(_clan_tags)} clans, {len(_revealed)} war_tags"
                + (f", league_rank='{_league_rank}'" if _league_rank else "")
            )
            if _new_rows > 0:
                logging.info(_log_msg + f" ({_new_rows} new DB rows written)")
            else:
                logging.debug(_log_msg + " (no-op, all rows already present)")
        except Exception as _ex:
            logging.warning(f"[CWL-ROUNDS] _process_league_group_response error: {_ex}")

    async def _sync_group_track_war_updates(self, clan_objs: List[Any], league_rank: str, cwl_season: str) -> None:
        """Enforce the Master III+ tracking gate across a CWL group whose league is confirmed.

        This is the group-wide counterpart to ``_update_clan_metadata()``'s per-clan
        promotion/demotion (qapbot/coc_cache.py) and ``_harvest_cwl_group_clans()``'s
        subscription-gated harvest (QBhelperfunctions.py). Those two paths only reach
        a clan when it is already being actively polled, or when it is freshly
        discovered as an enemy of a *subscribed* clan — so an already-known,
        non-subscribed clan in a group with no subscribed member (or no "new enemy"
        event this season) can sit indefinitely with a stale league and the wrong
        track_war_updates value.  This method fires for every group resolved by
        ``_process_league_group_response()`` (Layer 1 organic + Layer 2 finalization
        fallback get_league_group() calls), independent of subscription, closing
        that gap — see the 2026-06-24 changelog entry ("~40,000 Master III+ clans
        silently stopped being polled") for the failure mode this exists to prevent
        from recurring in a new blind spot.

        Also inserts group members that aren't in ``clan_name_cache`` at all yet —
        without this, only the single clan ``_process_league_group_response`` may
        have queried live via ``get_clan()`` (to resolve ``league_rank`` itself) gets
        seeded, and the other (up to 7) never-before-seen members are silently
        skipped.  No extra API call is needed for the insert: ``clan_objs`` is the
        already-fetched ``ClanWarLeagueGroup.clans`` list, which carries tag + name
        for every member, and every member of a CWL group is guaranteed to share
        the season's one ``league_rank`` (CWL rule) — no per-clan war_league lookup
        required.

        Subscribed clans are always tracked regardless of league movement and are
        left untouched.

        Demotion is deferred (war_league still corrected, track_war_updates left
        alone) for a clan that already has archived war_summary rows for
        ``cwl_season`` — i.e. the bot already captured some of this clan's rounds
        for the season in progress. Demoting mid-season would silence polling for
        the clan's remaining rounds, leaving a permanently incomplete season on
        record for no benefit (the demotion isn't lost — it naturally applies at
        the next season's group discovery, by which point this clan has zero rows
        for that new season, so the guard no longer applies). This mirrors the
        one-time backfill remediation in
        qapbot/scripts/repromote_mid_season_clans.py, written after discovering
        the same gap in the retroactive backfill for this fix (2026-08-08).

        Self-heal (2026-08-09): the guard above only fires *at the moment of a
        demotion transition*. A clan that was already, correctly, demoted while
        it had zero season data can still pick up archived war_summary rows
        later in the same season — ``CWL-GROUP-EXPAND`` (``QapBot.py``)
        force-fetches every member of a group containing an actively-tracked
        clan each cycle, independent of the fetched clan's own
        ``track_war_updates`` — landing it right back in the "demoted mid-season
        with partial data" bucket as a brand-new instance, not a leftover (see
        ``qapbot/docs/CWL_ROUND_TRACKING_PLAN.md`` 2026-08-09 finding:
        ``repromote_mid_season_clans.py`` found 2,332 fresh occurrences one day
        after being run to zero). Since that case has no league mismatch (the
        clan is still correctly below Master III), it never reached the
        transition branch below at all. Now checked unconditionally for any
        non-subscribed clan currently ``track_war_updates=False``: if it already
        has season data, re-promote it for the rest of this season using the
        same guard, proactively rather than only reactively.
        """
        should_track = league_rank in WAR_UPDATE_LEAGUES
        now_iso = datetime.now(_dt_timezone.utc).isoformat()
        for clan_obj in clan_objs:
            tag = str(getattr(clan_obj, "tag", "") or "")
            if not tag:
                continue
            clan_data = self.clan_name_cache.get(tag)
            if not isinstance(clan_data, dict):
                # Never-before-seen group member — insert it now (zero extra API
                # calls: name comes from the already-fetched league group response).
                name = str(getattr(clan_obj, "name", "") or "Unknown")
                self.clan_name_cache[tag] = {
                    "name": name,
                    "has_active_subscriptions": False,
                    "last_war_update": now_iso,
                    "warlog_is_public": True,
                    "last_checked_via_api": now_iso,
                    "war_league": league_rank,
                    "track_war_updates": should_track,
                }
                await self.persist_clan(tag)
                logging.info(
                    "[CWL-GROUP-SYNC] %s (%s): new clan, track_war_updates=%s (group league=%s)",
                    tag, name, should_track, league_rank,
                )
                continue
            if clan_data.get("has_active_subscriptions"):
                continue  # subscribed clans stay tracked regardless of league

            dirty = False
            if clan_data.get("war_league") != league_rank:
                clan_data["war_league"] = league_rank
                dirty = True
            _currently_tracked = bool(clan_data.get("track_war_updates"))
            if _currently_tracked != should_track:
                if not should_track:
                    # Demotion candidate — defer if the clan already has
                    # in-progress data for THIS season (see docstring).
                    _has_season_data = await self._clan_has_cwl_data_for_season_safe(
                        tag, cwl_season, "demotion guard"
                    )
                    if _has_season_data:
                        logging.info(
                            "[CWL-GROUP-SYNC] %s: demotion deferred (group league=%s) — "
                            "already has %s CWL data; will re-evaluate at next season's "
                            "group discovery", tag, league_rank, cwl_season,
                        )
                    else:
                        # Remove any ongoing temp war file first to prevent orphans,
                        # mirroring _update_clan_metadata()'s demotion path.
                        _removed = self.coc_clan_cache._cleanup_temp_war_files(tag)  # type: ignore[attr-defined]
                        logging.info(
                            "[CWL-GROUP-SYNC] %s: track_war_updates -> False (group league=%s, no subscriptions%s)",
                            tag, league_rank,
                            f", removed {_removed} temp war file(s)" if _removed else "",
                        )
                        clan_data["track_war_updates"] = False
                        dirty = True
                else:
                    logging.info(
                        "[CWL-GROUP-SYNC] %s: track_war_updates -> True (group league=%s)",
                        tag, league_rank,
                    )
                    clan_data["track_war_updates"] = True
                    dirty = True
            elif not should_track and not _currently_tracked:
                # No league mismatch (still correctly below M3) but already
                # demoted — self-heal: it may have picked up season data since
                # the demotion via CWL-GROUP-EXPAND (see docstring). Re-promote
                # for the rest of THIS season if so.
                _has_season_data = await self._clan_has_cwl_data_for_season_safe(
                    tag, cwl_season, "self-heal check"
                )
                if _has_season_data:
                    logging.info(
                        "[CWL-GROUP-SYNC] %s: re-promoted (group league=%s) — picked up "
                        "%s CWL data while demoted (self-heal); will re-evaluate at next "
                        "season's group discovery", tag, league_rank, cwl_season,
                    )
                    clan_data["track_war_updates"] = True
                    dirty = True

            if dirty:
                await self.persist_clan(tag)

    async def _clan_has_cwl_data_for_season_safe(
        self, tag: str, cwl_season: str, context: str
    ) -> bool:
        """``db_manager.clan_has_cwl_data_for_season()`` with a safe False default
        and consistent logging on error — shared by ``_sync_group_track_war_updates()``'s
        demotion guard and self-heal checks. False is always the safe fallback for
        both callers: the demotion guard proceeds with demotion, and the self-heal
        check leaves track_war_updates unchanged."""
        if self.db_manager is None:
            return False
        try:
            return await self.db_manager.clan_has_cwl_data_for_season(tag, cwl_season)
        except Exception as _hd_ex:
            logging.warning(
                "[CWL-GROUP-SYNC] %s: clan_has_cwl_data_for_season check failed during "
                "%s (%s)", tag, context, _hd_ex,
            )
            return False

    def evict_stale_cwl_caches(self) -> None:
        """Evict expired entries from CWL caches to prevent unbounded memory growth.

        Called once per update cycle.  Removes:
        - ``_league_war_cache``  entries older than ``_LEAGUE_WAR_CACHE_TTL``
        - ``_league_group_cache`` entries older than ``_LEAGUE_GROUP_CACHE_TTL``
        """
        _now = time.time()
        _war_exp = [
            tag for tag, (_, ts, _st) in self._league_war_cache.items()
            if _now - ts > self._LEAGUE_WAR_CACHE_TTL
        ]
        for tag in _war_exp:
            del self._league_war_cache[tag]

        _grp_exp = [
            tag for tag, (_, ts, _) in self._league_group_cache.items()
            if _now - ts > self._LEAGUE_GROUP_CACHE_TTL
        ]
        for tag in _grp_exp:
            del self._league_group_cache[tag]

        if _war_exp or _grp_exp:
            logging.info(
                f"[CWL-CACHE-EVICT] Purged {len(_war_exp)} league-war "
                f"and {len(_grp_exp)} league-group stale entries"
            )

        # Hard size caps — evict oldest entries when over limit.
        # Protects against memory explosion during CWL season (hundreds of orphaned
        # wars fetched per cycle, each holding a large coc.ClanWar object).
        _war_over = len(self._league_war_cache) - self._LEAGUE_WAR_CACHE_MAX_ENTRIES
        if _war_over > 0:
            _oldest = sorted(self._league_war_cache.items(), key=lambda x: x[1][1])[:_war_over]
            for _tag, _ in _oldest:
                del self._league_war_cache[_tag]
            logging.info(
                f"[CWL-CACHE-EVICT] Size cap: evicted {_war_over} league-war entries "
                f"(limit={self._LEAGUE_WAR_CACHE_MAX_ENTRIES})"
            )

        _grp_over = len(self._league_group_cache) - self._LEAGUE_GROUP_CACHE_MAX_ENTRIES
        if _grp_over > 0:
            _oldest_g = sorted(self._league_group_cache.items(), key=lambda x: x[1][1])[:_grp_over]
            for _tag, _ in _oldest_g:
                del self._league_group_cache[_tag]
            logging.info(
                f"[CWL-CACHE-EVICT] Size cap: evicted {_grp_over} league-group entries "
                f"(limit={self._LEAGUE_GROUP_CACHE_MAX_ENTRIES})"
            )

def count_archive_files_sync(archive_dir: str) -> int:
    """
    Count total archive war files across all shard directories.

    Uses os.scandir (10 directory reads, no per-file stat call). Safe to call
    via asyncio.to_thread() — does not touch any in-memory state.
    """
    total = 0
    for shard_dir in glob.glob(os.path.join(archive_dir, "shard_*")):
        try:
            with os.scandir(shard_dir) as it:
                total += sum(1 for _ in it)
        except OSError:
            pass
    return total


# Global cache instance
CACHE = CacheManager()



