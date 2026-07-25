import os
from dotenv import load_dotenv
load_dotenv(override=True)  # Force reload .env to get fresh values

# --- Logging Setup (MUST be before discord imports) ---
import logging
from logging.handlers import TimedRotatingFileHandler

_PROD_BASE = os.getenv("PROD_DATA_DIR", "")
try:
    _is_dev_mode = int(os.getenv("DISCORD_GUILD_ID", "0")) > 0
except ValueError:
    _is_dev_mode = False
DATA_DIR = os.path.join(_PROD_BASE, "data") if (_PROD_BASE and not _is_dev_mode) else "data"
TEMP_DIR = os.path.join(DATA_DIR, "temp")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

# Load log level from environment variable with fallback to INFO
log_level_str = os.getenv("QAPBOT_LOG_LEVEL", "INFO").upper()
LOG_LEVEL = getattr(logging, log_level_str, logging.INFO)
log_formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
log_file = os.path.join(LOGS_DIR, "qapbot.log")
try:
    file_handler = TimedRotatingFileHandler(log_file, when="midnight", backupCount=13, encoding="utf-8", delay=True)
    file_handler.setFormatter(log_formatter)
    file_handler.setLevel(LOG_LEVEL)
    handlers: list[logging.Handler] = [file_handler]
except Exception as e:
    # Fallback to console only if file handler fails
    handlers: list[logging.Handler] = []
    print(f"[WARN] Could not set up file logging: {e}")
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
console_handler.setLevel(LOG_LEVEL)
handlers.append(console_handler)
logging.basicConfig(level=LOG_LEVEL, handlers=handlers)

# tracemalloc: disabled for now (re-enable when investigating memory leaks).
# Was DEV-only (DISCORD_GUILD_ID > 0) to avoid 10-30% CPU overhead in PROD.
# The /admin Memory Profile command gracefully handles tracemalloc being off.
# import tracemalloc  # Uncomment to re-enable memory profiling in DEV
# tracemalloc.start(25)  # Uncomment to re-enable memory profiling in DEV

# Suppress PyNaCl warning from discord.py (we don't use voice features)
# Must be set BEFORE importing discord
logging.getLogger('discord').setLevel(logging.ERROR)

# Suppress the benign "Attempting a reconnect in Xs" ERROR that discord.py emits
# when aiohttp raises ClientConnectionResetError during heartbeat send.
# This happens when Discord closes the gateway server-side (transient drop) or
# when our intentional shutdown closes the transport while the heartbeat loop
# is still running. Discord.py handles the reconnect automatically — the message
# is noise, not a real error in our code.
class _DiscordReconnectFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not (record.levelno == logging.ERROR and "Attempting a reconnect" in record.getMessage())

logging.getLogger('discord').addFilter(_DiscordReconnectFilter())

import platform
import sys
import asyncio
import aiohttp
import coc  # type: ignore[import-untyped]
from coc import BatchThrottler  # type: ignore[import-untyped] # Enables true parallelization of API calls

# Patch coc.HERO_ORDER to include heroes not yet known by the coc.py library.
# coc.Player.heroes filters its output through this list, silently dropping unknowns.
# "Dragon Duke" was added in CoC update 2026-05 but is not yet in coc.py's constants.
# Guard: no-op if a future coc.py version adds Dragon Duke natively.
# Fallback: if "Battle Machine" is removed in a future version, append instead of insert.
if "Dragon Duke" not in coc.HERO_ORDER:
    try:
        coc.HERO_ORDER.insert(coc.HERO_ORDER.index("Battle Machine"), "Dragon Duke")
    except ValueError:
        coc.HERO_ORDER.append("Dragon Duke")

import discord  # Added for Discord exception handling
from discord.ext import commands  # Added for Bot/commands support
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, List, Optional, Tuple
import signal  # added for graceful shutdown
import time
import QBcore
from qapbot.config import CONFIG
from qapbot.cache_manager import CACHE
from qapbot.constants import (
    SECONDS_PER_HOUR,
    MAX_RETRY_BACKOFF_SECONDS,
    PLAYERREGISTRATION_BUMP_COOLDOWN_SECONDS
)
from qapbot.discord_health import discord_retry
from qapbot.coc_health import reset_cycle_stats, get_coc_stats, clear_maintenance_detection, is_maintenance_detected, clear_dns_detection
from QBhelperfunctions import (
    generate_leaderboard_text, generate_cwlinfo_embeds, generate_cwlinfo_comp_embeds, post_discord_content_with_tracking,
    post_leaderboard_to_discord, calculate_content_hash,
    fetch_clan_war_data, process_clan_war_data,
    generate_cwl_group_image, update_cwl_group_stats,
)

# Sentinel returned by fetch_single_clan when the CoC API responded successfully
# but the clan has no active war (notInWar).  Distinct from None (API failure) so
# Phase 3 can update last_war_update for the no-war case (removing the clan from
# the overdue backlog for 22h) while leaving genuine failures for retry next cycle.
_NOT_IN_WAR: dict[str, Any] = {"__not_in_war__": True}

# --- Constants (already defined above with logging setup) ---
# DATA_DIR, TEMP_DIR, LOGS_DIR, LOG_LEVEL already configured

# ---------------------------------------------------------------------------
# Feature Toggles — central index
# All toggles that change bot behaviour are listed here for easy maintenance.
# Toggles that live in other modules are noted with their file location.
# ---------------------------------------------------------------------------
#
# 1) NO_DEV_MODE_UPDATES  (this file, QapBot.py)
#    When True, update cycles are skipped entirely in DEV mode.
#    The bot still sleeps normally between cycles — only main() is bypassed.
#    Set to False to restore normal update behaviour in dev mode.
NO_DEV_MODE_UPDATES: bool = True
#
# 2) _sim_enabled  (QBwarsim.py, line ~750)
#    When True, Monte Carlo war simulation uses a process pool for parallelism.
#    Set to False via init_sim_pool(enabled=False) or directly to run in-process.
#    Controlled at startup; False is also forced when < 2 workers are available.
#
# 3) _DEV_API_THROTTLE_S  (qapbot/coc_health.py, line ~49)
#    Minimum seconds between consecutive CoC API calls in DEV mode.
#    Set to 0.0 to disable throttling entirely.
#    Only applies when CONFIG.is_dev_mode is True.
# ---------------------------------------------------------------------------

# --- Global Guild ID Override ---
# Set this variable to override the Discord guild ID globally for all code (testing/dev convenience)
GLOBAL_GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0"))
#GLOBAL_GUILD_ID = 0 # Uncomment to force global mode for testing/dev, also uncomment correpsonding line in config.py!!!

QBcore.cleaned_up = False  # flag to avoid double cleanup
QBcore.lifetime_logged = False  # flag to log lifetime stats exactly once
QBcore.shutdown_initiated = False

def _signal_shutdown_handler(signum: int, frame: Optional[Any]) -> None:
    """
    Handle shutdown signals for graceful bot termination.
    
    Args:
        signum (int): Signal number received (e.g., signal.SIGINT, signal.SIGTERM)
        frame (Optional[Any]): Current stack frame object (can be None)
    
    Behavior:
        - Sets shutdown event and schedules bot closure if event loop is running
        - Prevents duplicate shutdowns using _shutdown_initiated flag
        - Handles Windows CTRL+C gracefully
    """
    if QBcore.shutdown_initiated:
        return
    QBcore.shutdown_initiated = True
    logging.info(f"Signal {signum} received; initiating graceful shutdown...")
    # IMPORTANT: asyncio.Event.set() called directly from a signal handler uses
    # loop.call_soon() internally, which queues the wakeup but does NOT write to
    # the loop's self-pipe.  The sleeping selector therefore won't wake until the
    # next natural I/O event (Discord heartbeat ~40 s) or the 300 s timeout fires.
    # call_soon_threadsafe() writes to the self-pipe and wakes the selector immediately.
    try:
        loop = QBcore.bot.loop
        if loop.is_running():
            loop.call_soon_threadsafe(QBcore.shutdown_event.set)
            loop.call_soon_threadsafe(lambda: asyncio.create_task(_close_bot_after_signal()))
            return
    except Exception:
        pass
    # Fallback: loop not available or not running yet
    if QBcore.shutdown_event and not QBcore.shutdown_event.is_set():
        QBcore.shutdown_event.set()

async def _close_bot_after_signal() -> None:
    """
    Fallback bot close for cases where periodic_main cannot close the bot itself
    (e.g. very early startup shutdown, or periodic_main stuck in a long cycle).

    DO NOT call bot.close() immediately here — that would race with
    periodic_main's async_cleanup() sequence:

        periodic_main: async_cleanup() → bot.close()  ← correct order
        _close_bot_after_signal (old): sleep(0) → bot.close()  ← too early!

    Calling bot.close() before async_cleanup() finishes causes asyncio.run() to
    cancel all remaining tasks (including periodic_main mid-cleanup), after which
    the aiosqlite background thread finds the event loop closed and crashes with
    RuntimeError: Event loop is closed.

    Instead: poll for QBcore.cleaned_up (set at the END of async_cleanup()) and
    only act as a fallback if periodic_main hasn't handled cleanup within 60 s.

    IMPORTANT: Always call bot.close() after cleanup so bot.run() / asyncio.run()
    can actually return.  In maintenance mode cleaned_up=True immediately (resources
    were closed by do_maintenance_shutdown), but bot.close() was never called —
    without it bot.start() blocks forever on the Discord WebSocket, preventing exit.
    """
    for _ in range(600):  # 60 s in 0.1 s steps
        await asyncio.sleep(0.1)
        if QBcore.cleaned_up or QBcore.bot.is_closed():
            break  # resources clean — fall through to close the Discord connection
    else:
        # Fallback: periodic_main did not finish in 60 s (not started or stuck)
        if not QBcore.cleaned_up:
            try:
                await async_cleanup()
            except Exception:
                pass
    # Always ensure the Discord WebSocket is closed so bot.run() can return.
    # bot.close() is idempotent — safe even if periodic_main already called it.
    try:
        if not QBcore.bot.is_closed():
            await asyncio.wait_for(QBcore.bot.close(), timeout=10.0)
    except (asyncio.TimeoutError, Exception):
        pass
    if not QBcore.bot.is_closed():
        try:
            await QBcore.bot.close()
        except Exception:
            pass

# Register handlers (best-effort; may not be available on some platforms)
for _sig in (getattr(signal, 'SIGINT', None), getattr(signal, 'SIGTERM', None)):
    if _sig is not None:
        try:
            signal.signal(_sig, _signal_shutdown_handler)
        except Exception:
            pass

# --- Unique functions kept locally ---

async def startup_login() -> None:
    """
    Initialize and authenticate the Clash of Clans API client with BatchThrottler.

    Behavior:
        - Creates coc.Client with BatchThrottler for true parallelization (10 req/sec)
        - Loads credentials from CONFIG (auto-selects DEV/PROD based on DISCORD_GUILD_ID)
        - Authenticates with CoC API
        - Stores client in CACHE for centralized API access
        - Ensures QBcore.coc_client is ready for backward compatibility

    Rate Limiting:
        - Uses coc.py's BatchThrottler for efficient parallel request handling
        - Limit: 10 requests per second per API key
        - Enables true concurrent API calls to multiple clans simultaneously

    Raises:
        Exception: If API credentials are missing or authentication fails
        coc.InvalidCredentials: If provided credentials are invalid
        coc.Maintenance: If CoC API is under maintenance

    Environment Variables Required:
        DEV Mode (DISCORD_GUILD_ID > 0):
        - COC_API_EMAIL_DEV: Clash of Clans API account email
        - COC_API_PASSWORD_DEV: Clash of Clans API account password
        
        PROD Mode (DISCORD_GUILD_ID == 0):
        - COC_API_EMAIL: Clash of Clans API account email
        - COC_API_PASSWORD: Clash of Clans API account password

    Example:
        await startup_login()  # Must be called before any CoC API operations
    """
    if QBcore.coc_client is None:
        if CONFIG.no_coc_api:
            # NO_COC_API mode: skip CoC client creation entirely
            logging.info("[NO_COC_API] Skipping CoC API client creation (NO_COC_API=true)")
        else:
            # Create client with rate limiting
            # CoC API official limit: 10 requests/second (per key)
            # BatchThrottler enables true parallelization of concurrent API calls
            # rate_limit=10 requests per 1.0 second (official CoC API limit)
            # QBcore.coc_client = coc.Client(throttle_limit=30) # Old single-threaded throttler

            QBcore.coc_client = coc.Client(key_count=10, throttler=coc.BatchThrottler, throttle_limit=100) # throttle_limit can be up to 100 (10 keys * 10 req/sec)


            # Store in CACHE for centralized access
            CACHE.coc_client = QBcore.coc_client
        
        # Initialize database manager (always required, even without CoC API)
        try:
            from qapbot.db_manager import WarHistoryDB
            CACHE.db_manager = WarHistoryDB()
            if CACHE.db_manager:  # type: ignore
                await CACHE.db_manager.initialize(CONFIG.db_path, CONFIG.history_db_path)  # type: ignore
                logging.info(f"[DB] Database initialized at {CONFIG.db_path} (history: {CONFIG.history_db_path})")
        except Exception as e:
            logging.error(f"[DB] Failed to initialize database: {e}")
            raise RuntimeError("Database initialization failed - bot cannot start")
        
        # Suppress verbose HTTP logging from coc.py library (403 Forbidden responses for private war logs)
        logging.getLogger('coc.http').setLevel(logging.WARNING) # set back to DEBUG/INFO for debugging if needed
        # Suppress PyNaCl voice warning from discord.py (we don't use voice features)
        logging.getLogger('discord.voice_client').setLevel(logging.ERROR)
        logging.getLogger('discord.player').setLevel(logging.ERROR)
    
    # Login to CoC API (skip when NO_COC_API is set)
    if not CONFIG.no_coc_api:
        await QBcore.coc_client.login(CONFIG.coc_email, CONFIG.coc_password)  # type: ignore[union-attr]
        mode_str = "DEV" if CONFIG.is_dev_mode else "PROD"
        logging.info(f"Logged in to Clash of Clans API ({mode_str} mode) with rate limiting 100 (10 keys *10 req/sec).")
        # Register ourselves as the reconnect callback so coc_retry can re-authenticate
        # the client if the aiohttp session is unexpectedly closed.
        from qapbot.coc_health import set_reconnect_callback
        set_reconnect_callback(startup_login)
    else:
        logging.info("[NO_COC_API] Skipping CoC API login (NO_COC_API=true)")

sleep_interval = CONFIG.sleep_interval

async def post_leaderboards_to_subscribed_channels() -> None:
    """
    Post leaderboards to all subscribed Discord channels automatically.

    Behavior:
        - Iterates through all channel subscriptions using guild-aware cache access
        - In DEV mode: Only posts to DEV_PLAYERREGISTRATION_CHANNEL_ID
        - In PROD mode: Posts to all subscribed channels
        - For each subscription, generates and posts the leaderboard for the specified clan and mode
        - Uses content hashes to avoid duplicate posts and minimize Discord API calls
        - Skips channels that can't be found or are inaccessible
        - Uses cache-centric data access for all war stats and leaderboards
        - Logs posting statistics for monitoring

    Note:
        Called by the main() loop during regular synchronization cycles.
        All data is accessed via the cache manager; no direct file I/O.
    """
    # DEV-MODE SAFETY: auto-post is allowed only for the explicitly configured
    # DEV_PLAYERREGISTRATION_CHANNEL_ID. All other channels remain suppressed.
    if CONFIG.is_dev_mode and CONFIG.dev_playerregistration_channel_id <= 0:
        logging.warning("DEV mode: automatic leaderboard posting is disabled because DEV_PLAYERREGISTRATION_CHANNEL_ID is not configured.")
        return

    subs = CACHE.get_all_subscriptions_flat()
    total_posts = 0
    skipped_posts = 0
    filtered_channels = 0

    # Pre-filter channels into work list
    channel_work: list[tuple[str, discord.TextChannel | discord.Thread, list[dict[str, Any]]]] = []
    for channel_id, sub_list in subs.items():
        channel = QBcore.bot.get_channel(int(channel_id))
        if not channel:
            continue
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            logging.debug(f"Skipping non-text channel {channel_id}")
            continue
        if CONFIG.is_dev_mode and int(channel_id) != CONFIG.dev_playerregistration_channel_id:
            filtered_channels += 1
            logging.debug(
                f"Skipping channel {channel_id} in guild {channel.guild.id} "
                f"(DEV mode - only processing channel {CONFIG.dev_playerregistration_channel_id})"
            )
            continue
        channel_work.append((channel_id, channel, sub_list))

    # Process channels concurrently (subscriptions within a channel stay sequential
    # to respect per-channel Discord rate limits).
    _post_sem = asyncio.Semaphore(5)

    async def _process_channel(channel_id: str, channel: discord.TextChannel | discord.Thread, sub_list: list[dict[str, Any]]) -> tuple[int, int]:
        """Process all subscriptions for a single channel. Returns (posted, skipped)."""
        posted, skipped = 0, 0
        async with _post_sem:
            for sub in sub_list:
                # Yield to event loop between subscriptions so Discord
                # interactions (commands, buttons) are not starved.
                await asyncio.sleep(0)
                clan_name = None
                clan_tag = sub.get('clan_tag')
                subscription_type = sub.get('subscription_type', 'attack')
                sub_month = sub.get('month')
                sub_year = sub.get('year')

                try:
                    # Skip playerregistration subscriptions - they are handled only on startup
                    if subscription_type == 'playerregistration':
                        continue

                    if not clan_tag:
                        continue
                    clan_name = CACHE.get_clan_name(clan_tag, None)
                    if not clan_name:
                        clan_name = CACHE.clan_families.get(clan_tag, {}).get("name")
                    now = datetime.now()
                    # Default: current month/year
                    month = now.month
                    year = now.year
                    month_range = month
                    # YTD scope when year == "current"
                    if sub_year == "current":
                        year = now.year
                        month_range = list(range(1, now.month + 1))
                    elif isinstance(sub_year, int):
                        year = sub_year
                    if isinstance(sub_month, int):
                        month = sub_month
                        month_range = month
                    logging.info(f"Preparing leaderboard for clan_tag: {clan_tag}, month: {month_range}, year: {year}, mode: {subscription_type}")

                    # --- cwlinfo: embed path (round-by-round CWL overview) ---
                    if subscription_type == 'cwlinfo':
                        cwl_embeds = await generate_cwlinfo_embeds(clan_tag)
                        # Hash all embed descriptions + field values for change detection
                        cwl_hash_text = ''.join(
                            (e.description or '') + ''.join(f.value or '' for f in (e.fields or []))
                            for e in cwl_embeds
                        )
                        content_hash = calculate_content_hash(cwl_hash_text)
                        found_existing = any(
                            v.get('channel_id') == str(channel_id)
                            and v.get('clan_tag') == clan_tag
                            and v.get('mode', '') == 'cwlinfo'
                            and v.get('content_hash', '') == content_hash
                            for v in CACHE.leaderboard_messages.values()
                        )
                        if found_existing:
                            skipped += 1
                            logging.info(f"Skipping {clan_tag} cwlinfo - content unchanged (hash: {content_hash[:8]}...)")
                        else:
                            posted += 1
                            logging.info(f"Posting {clan_tag} cwlinfo - content changed (hash: {content_hash[:8]}...)")
                            await post_discord_content_with_tracking(clan_tag, channel, embeds=cwl_embeds, content_hash=content_hash)
                        continue

                    # --- cwlinfo_comp: embed path with dual predictions ---
                    elif subscription_type == 'cwlinfo_comp':
                        cwl_comp_embeds, cwl_comp_debug = await generate_cwlinfo_comp_embeds(clan_tag)
                        cwl_hash_text = ''.join(
                            (e.description or '') + ''.join(f.value or '' for f in (e.fields or []))
                            for e in cwl_comp_embeds
                        )
                        content_hash = calculate_content_hash(cwl_hash_text)
                        found_existing = any(
                            v.get('channel_id') == str(channel_id)
                            and v.get('clan_tag') == clan_tag
                            and v.get('mode', '') == 'cwlinfo_comp'
                            and v.get('content_hash', '') == content_hash
                            for v in CACHE.leaderboard_messages.values()
                        )
                        if found_existing:
                            skipped += 1
                            logging.info(f"Skipping {clan_tag} cwlinfo_comp - content unchanged (hash: {content_hash[:8]}...)")
                        else:
                            posted += 1
                            logging.info(f"Posting {clan_tag} cwlinfo_comp - content changed (hash: {content_hash[:8]}...)")
                            await post_discord_content_with_tracking(clan_tag, channel, 'cwlinfo_comp', embeds=cwl_comp_embeds, content_hash=content_hash, debug_content=cwl_comp_debug)
                        continue

                    # --- cwlgroup: PNG image of CWL league-group standings ---
                    elif subscription_type == 'cwlgroup':
                        # Find the most recent season that has data for this clan.
                        if CACHE.db_manager is None:
                            skipped += 1
                            continue
                        _season = await CACHE.db_manager.get_latest_cwl_season_for_clan(clan_tag)
                        if not _season:
                            logging.info(f"[CWLGROUP] No CWL season found for {clan_tag} — skipping.")
                            skipped += 1
                            continue
                        if await CACHE.db_manager.is_cwl_ended_for_clan(clan_tag, _season):
                            skipped += 1
                            logging.info(f"Skipping {clan_tag}, type {subscription_type} - CWL ended for season {_season}")
                            continue
                        standings = await update_cwl_group_stats(clan_tag, _season)
                        if not standings:
                            logging.info(f"[CWLGROUP] No standings for {clan_tag} / {_season} — skipping.")
                            skipped += 1
                            continue
                        img_bytes = await asyncio.to_thread(generate_cwl_group_image, standings, _season, clan_tag)
                        # update_existing=True: edits the same-season message in-place when
                        # content changed, or posts a new message when the season is new.
                        # The old season's message is left untouched (history preserved).
                        await post_discord_content_with_tracking(
                            clan_tag, channel, f"cwlgroup_{_season}",
                            file_bytes=img_bytes, file_name="cwlgroup.png",
                            update_existing=True,
                        )
                        posted += 1
                        logging.info(f"[CWLGROUP] Processed {clan_tag} / {_season}.")
                        continue

                    # --- all other modes: text/leaderboard path ---
                    leaderboard_text = await asyncio.to_thread(generate_leaderboard_text, clan_tag, month=month_range, year=year, mode=subscription_type)
                    # Only auto-fallback when no explicit month/year is set
                    if sub_month is None and sub_year is None and "no wars recorded for" in leaderboard_text.lower() and subscription_type != "currentwar":
                        prev_month = month - 1
                        prev_year = year
                        if prev_month == 0:
                            prev_month = 12
                            prev_year -= 1
                        leaderboard_text = await asyncio.to_thread(generate_leaderboard_text, clan_tag, month=prev_month, year=prev_year, mode=subscription_type)
                    content_hash = calculate_content_hash(leaderboard_text)
                    # Search for existing message with matching context and hash
                    found_existing = False
                    logging.debug(f"Testing leaderboard post: channel_id={channel_id}, clan_tag={clan_tag}, mode={subscription_type}, content_hash={content_hash[:8]}...")
                    for _, v in CACHE.leaderboard_messages.items():
                        if (
                            v.get('channel_id') == str(channel_id)
                            and v.get('clan_tag') == clan_tag
                            and subscription_type in v.get('mode', '')
                            and v.get('content_hash', '') == content_hash
                        ):
                            found_existing = True
                            break
                    if found_existing:
                        skipped += 1
                        logging.info(f"Skipping {clan_tag}, type {subscription_type} - content unchanged (hash: {content_hash[:8]}...)")
                    else:
                        posted += 1
                        logging.info(f"Posting {clan_tag} leaderboard - content changed (hash: {content_hash[:8]}...)")
                        await post_leaderboard_to_discord(leaderboard_text, clan_tag, month_range, year, channel, mode=subscription_type)
                except Exception as e:
                    failed_count_label = f"{clan_tag}/{subscription_type}" if clan_tag else f"unknown/{subscription_type}"
                    logging.error(f"[LEADERBOARD-POST] Error processing subscription {failed_count_label}: {e}", exc_info=True)
        return posted, skipped

    results = await asyncio.gather(
        *[_process_channel(cid, ch, sl) for cid, ch, sl in channel_work],
        return_exceptions=True,
    )
    for r in results:
        if isinstance(r, tuple):
            total_posts += r[0]
            skipped_posts += r[1]
        elif isinstance(r, Exception):
            logging.error(f"[LEADERBOARD-POST] Channel task failed: {r}", exc_info=r)

    # Update logging to include filtered count in DEV mode
    if CONFIG.is_dev_mode and filtered_channels > 0:
        logging.info(f"Discord posting complete: {total_posts} posted/updated, {skipped_posts} skipped (unchanged), {filtered_channels} channels filtered (DEV mode)")
    else:
        logging.info(f"Discord posting complete: {total_posts} posted/updated, {skipped_posts} skipped (unchanged)")

async def main() -> None:
    """
    Main synchronization loop that updates clan war info and posts leaderboards.

    Behavior:
        - Updates war information and statistics for all actively tracked clans via CACHE and QBhelperfunctions
        - Generates terminal-style leaderboards for logging
        - Posts Discord leaderboards to all subscribed channels using cache-centric logic
        - Records sync timestamp for status tracking
        - Handles errors per clan without stopping the loop

    Note:
        This is the main business logic entry point, called by periodic_main().
        All persistent data is managed via cache_manager.py.
    """
    QBcore.bot.last_sync = datetime.now()

    # Skip all CoC API operations when NO_COC_API is set
    if CONFIG.no_coc_api:
        logging.info("[NO_COC_API] Skipping clan war updates and leaderboard posts (NO_COC_API=true)")
        await repost_playerregistration_messages(only_if_not_bottom=True, bump_cooldown_seconds=PLAYERREGISTRATION_BUMP_COOLDOWN_SECONDS)
        return

    # Reset CoC API rate limit tracking for this cycle
    reset_cycle_stats()
    # Reset per-cycle CoC maintenance detection (new probe at each cycle start)
    clear_maintenance_detection()
    # Reset per-cycle DNS failure detection (new probe at each cycle start)
    clear_dns_detection()
    # Reset per-cycle war counters
    CACHE.reset_cycle_stats()

    # INACTIVE_CLAN_UPDATE_INTERVAL_HOURS: Update interval for clans without active subscriptions
    # This constant controls:
    # 1. Minimum time between updates for inactive clans (hours_since_update >= THRESHOLD)
    # 2. Smart timestamp calculation for ongoing wars: war_end_time - (THRESHOLD + 8min)
    # The 8-minute offset ensures updates happen ~8min before war ends to capture late attacks,
    # then continue every 5min while inWar (due to constant backdated timestamp), and finally
    # one update after war ends before resuming normal interval.
    INACTIVE_CLAN_UPDATE_INTERVAL_HOURS = 22

    logging.info("Starting clan war info updates...")
    _categorize_t0 = time.monotonic()
    now = datetime.now(timezone.utc)
    clans_to_update: list[tuple[str, bool]] = []

    # ---- helper: parse "datetime.datetime(Y, M, D, H, Mi)" from raw timestamp strings ----
    import re as _re_mod
    _DT_RE = _re_mod.compile(r'datetime\.datetime\(([^)]+)\)')

    def _parse_dt_from_raw(raw: str) -> Optional[datetime]:
        """Extract a datetime from the CoC Timestamp string stored in JSON."""
        m = _DT_RE.search(raw)
        if not m:
            return None
        parts = [int(x.strip()) for x in m.group(1).split(',')]
        if len(parts) >= 5:
            return datetime(parts[0], parts[1], parts[2], parts[3], parts[4], tzinfo=timezone.utc)
        return None

    # Batch-collect (timestamp, clan_tag) updates instead of await persist_clan per clan
    _timestamp_batch: list[tuple[str, str]] = []  # (iso_timestamp, clan_tag)

    # Precompute cutoff datetime and reuse cross-cycle datetime parse cache.
    # Avoids 12K × datetime.fromisoformat() calls per cycle; only re-parses
    # when the raw ISO string changes (i.e. clan was updated this cycle).
    cutoff_dt = now - timedelta(hours=INACTIVE_CLAN_UPDATE_INTERVAL_HOURS)
    _dt_cache: dict[str, tuple[str, datetime]] = CACHE.clan_dt_cache

    # Categorize clans based on subscription status and last update time.
    #
    # PERFORMANCE/BLOCKING NOTE: CACHE.clan_name_cache holds ~380K entries in
    # PROD (every clan ever encountered via CWL family harvesting, not just
    # this cycle's active/inactive counts). This loop is 100% synchronous
    # (dict lookups + cached datetime parsing, no I/O) and previously had NO
    # yield points, so it ran as one uninterrupted ~4s block every single
    # cycle — freezing Discord heartbeat/gateway processing and slash-command
    # handling for that whole duration, every ~5 minutes, day-round. Confirmed
    # via qapbot/scripts/log_time_gaps.py: a consistent ~4.1-4.2s silent gap
    # between "Starting clan war info updates..." and the next log line, every
    # cycle, regardless of how many clans were actually due for update.
    # Fix: yield every 2000 clans so the event loop can interleave heartbeat/
    # interaction handling; ~380K/2000 = ~190 yields/cycle at negligible
    # overhead (asyncio.sleep(0) is a few µs), bounding any single blocking
    # stretch to a small fraction of the original ~4s.
    #
    # MICRO-OPTIMIZATION (2026-07-18): measured >99.9% of the ~380K entries
    # are passively-tracked enemy clans (track_war_updates=False) that get
    # rejected on the very first check — the cost is pure CPython per-
    # iteration loop overhead, not wasted per-clan computation. Two cheap
    # wins that don't change any polling-eligibility semantics:
    #   1. Snapshot `.items()` instead of `.keys()` + a second `[clan_tag]`
    #      dict lookup per clan — halves the dict-lookup count. Still a full
    #      `list(...)` snapshot (not a live view) because this loop yields
    #      via asyncio.sleep(0), and other coroutines (e.g. WAR-DISCOVERY
    #      upserting a newly scouted enemy clan) can mutate
    #      CACHE.clan_name_cache between yields — iterating a live view
    #      would risk "dictionary changed size during iteration".
    #   2. Replace the per-clan isinstance() check with EAFP (try/except
    #      AttributeError) around the two most common fast-reject lookups.
    #      isinstance() has real per-call overhead at this iteration count;
    #      exception handling costs ~nothing when no exception is raised,
    #      which is true for >99.9% of entries.
    # NOT changed: this remains an O(n_all_clans) scan. A real fix (an
    # incrementally-maintained "pollable clan tags" set/index) was
    # considered but rejected for now — it requires touching every one of
    # the ~6-8 call sites that create/mutate track_war_updates/is_deleted,
    # and this exact class of stale-derived-state bug has bitten this
    # codebase before (see changelog: ~40K Master III+ clans silently
    # stopped being polled). Not worth the correctness risk for a step
    # that's already non-blocking.
    _categorize_clan_count = 0
    for clan_tag, clan_data in list(CACHE.clan_name_cache.items()):
        _categorize_clan_count += 1
        if _categorize_clan_count % 2000 == 0:
            await asyncio.sleep(0)

        try:
            # Fast-reject path first: passively-tracked / deleted clans are
            # >99.9% of all entries and never need anything below this.
            if not clan_data.get('track_war_updates', True) or clan_data.get('is_deleted'):
                continue
        except AttributeError:
            # Treat old-format (non-dict) entries as active
            logging.warning(f"[MAIN-LOOP] Clan {clan_tag} not in new format - treating as active")
            clans_to_update.append((clan_tag, True))
            continue

        has_subs: bool = bool(clan_data.get('has_active_subscriptions', False))  # type: ignore[arg-type]
        last_update: str | None = clan_data.get('last_war_update')  # type: ignore[assignment]
        
        # Active clans: update every cycle
        if has_subs:
            clans_to_update.append((clan_tag, True))
            continue
        
        # Inactive clans: update if >22h since last update
        if last_update is None:
            clans_to_update.append((clan_tag, False))  # Never updated - do it now
            continue
        
        try:
            # Use cached parsed datetime if available and raw string unchanged
            _cached = _dt_cache.get(clan_tag)
            if _cached is not None and _cached[0] == last_update:
                last_update_dt = _cached[1]
            else:
                last_update_dt = datetime.fromisoformat(last_update)  # type: ignore[arg-type]
                if last_update_dt.tzinfo is None:
                    last_update_dt = last_update_dt.replace(tzinfo=timezone.utc)
                _dt_cache[clan_tag] = (last_update, last_update_dt)

            if last_update_dt <= cutoff_dt:
                clans_to_update.append((clan_tag, False))
            else:
                hours_since_update = (now - last_update_dt).total_seconds() / SECONDS_PER_HOUR
                # Check if clan has ongoing war using IN-MEMORY metadata (no disk I/O)
                meta = CACHE.temp_war_metadata.get(clan_tag)
                if meta:
                    war_state = meta.get('state', '')

                    if war_state == 'preparation':
                        start_dt = _parse_dt_from_raw(meta.get('start_time', ''))
                        if start_dt:
                            update_target_time = start_dt + timedelta(minutes=30)
                            backdated_timestamp = update_target_time - timedelta(hours=INACTIVE_CLAN_UPDATE_INTERVAL_HOURS)
                            _bd_iso = backdated_timestamp.isoformat()
                            clan_data['last_war_update'] = _bd_iso
                            _dt_cache[clan_tag] = (_bd_iso, backdated_timestamp)
                            # Dirty-check: only write to DB if the value actually changed.
                            # The in-memory update above is always applied.
                            if _bd_iso != last_update:
                                _timestamp_batch.append((_bd_iso, clan_tag))
                            logging.debug(f"[INACTIVE-PREP] {clan_tag} - war in preparation, backdated to update 30min after start ({update_target_time.isoformat()})")
                            continue
                        logging.debug(f"[SKIP] {clan_tag} - war in preparation, couldn't parse start time")
                    elif war_state in ('in_war', 'inwar'):
                        end_dt = _parse_dt_from_raw(meta.get('end_time', ''))
                        if end_dt:
                            update_target_time = end_dt - timedelta(minutes=8)
                            backdated_timestamp = update_target_time - timedelta(hours=INACTIVE_CLAN_UPDATE_INTERVAL_HOURS)
                            _bd_iso = backdated_timestamp.isoformat()
                            clan_data['last_war_update'] = _bd_iso
                            _dt_cache[clan_tag] = (_bd_iso, backdated_timestamp)
                            # Dirty-check: only write to DB if the value actually changed.
                            # The in-memory update above is always applied.
                            if _bd_iso != last_update:
                                _timestamp_batch.append((_bd_iso, clan_tag))
                            logging.debug(f"[INACTIVE-INWAR] {clan_tag} - war in progress, backdated to update 8min before end ({update_target_time.isoformat()})")
                            continue
                        logging.debug(f"[SKIP] {clan_tag} - war in progress, couldn't parse end time")
                    elif war_state == 'war_ended':
                        clans_to_update.append((clan_tag, False))
                        logging.debug(f"[INACTIVE-WAR] {clan_tag} - war ended, forcing update despite {hours_since_update:.1f}h")
                    else:
                        logging.debug(f"[SKIP] {clan_tag} - inactive, unknown war state '{war_state}'")
                else:
                    logging.debug(f"[SKIP] {clan_tag} - inactive, last update {hours_since_update:.1f}h ago")
        except Exception as e:
            logging.warning(f"[PARSE-ERROR] {clan_tag} last_update='{last_update}': {e} - forcing update")
            clans_to_update.append((clan_tag, False))

    logging.info(
        "[CATEGORIZE-TIMING] Categorized %d clan(s) in %.3fs",
        _categorize_clan_count, time.monotonic() - _categorize_t0,
    )

    # Flush all backdated timestamp updates in a single DB transaction
    if _timestamp_batch:
        try:
            await CACHE.db_manager.bulk_update_clan_timestamps(_timestamp_batch)  # type: ignore[union-attr]
            logging.debug(f"[CATEGORIZE] Batch-persisted {len(_timestamp_batch)} backdated timestamps")
        except Exception as e:
            logging.error(f"[CATEGORIZE] Failed to batch-persist timestamps: {e}")

    # Count active and inactive for logging
    active_clans = [(ct, ia) for ct, ia in clans_to_update if ia]
    inactive_clans = [(ct, ia) for ct, ia in clans_to_update if not ia]

    # ---- Thundering-herd guard ----
    # After a restart or mass-expiry, thousands of inactive clans can expire
    # their 22h check simultaneously.  Cap per-cycle to keep cycles short.
    # Stalest clans (longest since last update) get priority.
    #
    # War-critical clans (temp_war_metadata.state preparation/in_war) are EXEMPT
    # from the cap: their poll must capture the in_war snapshot before the battle
    # day ends, otherwise the preparation temp file is never overwritten and later
    # gets removed as stale.  Smart-backdating schedules these polls at a precise
    # time, but a staleness-only cap would defer them behind the huge warless
    # backlog (starvation), defeating the schedule.  Active-war clans are extremely
    # rare in the inactive pool (~2 per 5000), so exempting them is near-zero cost.
    _MAX_INACTIVE_PER_CYCLE = 5000
    _overdue_total = len(inactive_clans)  # snapshot before capping, for CYCLE-SUMMARY
    if len(inactive_clans) > _MAX_INACTIVE_PER_CYCLE:
        # Partition into war-critical (exempt) and generic (cappable) clans.
        _war_critical: list[tuple[str, bool]] = []
        _generic: list[tuple[str, bool]] = []
        for _item in inactive_clans:
            _meta = CACHE.temp_war_metadata.get(_item[0])
            if _meta and _meta.get('state', '') in ('preparation', 'in_war', 'inwar'):
                _war_critical.append(_item)
            else:
                _generic.append(_item)

        if len(_generic) > _MAX_INACTIVE_PER_CYCLE:
            # Sort generic clans by staleness: no last_war_update first, then oldest first
            def _staleness(item: tuple[str, bool]) -> float:
                cd = CACHE.clan_name_cache.get(item[0])
                if not isinstance(cd, dict):
                    return float('inf')  # unknown → highest priority
                lu = cd.get('last_war_update')
                if lu is None:
                    return float('inf')
                try:
                    lu_dt = datetime.fromisoformat(lu)  # type: ignore[arg-type]
                    if lu_dt.tzinfo is None:
                        lu_dt = lu_dt.replace(tzinfo=timezone.utc)
                    return (now - lu_dt).total_seconds()
                except Exception:
                    return float('inf')

            _generic.sort(key=_staleness, reverse=True)
            deferred = len(_generic) - _MAX_INACTIVE_PER_CYCLE
            _generic = _generic[:_MAX_INACTIVE_PER_CYCLE]
            logging.info(
                f"[THROTTLE] Capped generic inactive clans from {deferred + _MAX_INACTIVE_PER_CYCLE} "
                f"to {_MAX_INACTIVE_PER_CYCLE} + {len(_war_critical)} war-critical exempt "
                f"(deferred {deferred} to next cycle)"
            )

        inactive_clans = _war_critical + _generic

    # Record as a gauge (not a counter): overwrite directly so reset_cycle_stats()
    # clears it and the next cycle's assignment sets the fresh value.
    CACHE.cycle_stats["throttle_backlog"] = _overdue_total

    clans_to_update = active_clans + inactive_clans

    # ── CWL group expansion: during CWL fetch all 8 group members every cycle ──
    # Stars for non-tracked group-mates are only accurate when their war data is
    # fetched directly.  Query cwl_league_groups for active (cwl_ended=0) groups
    # of all subscribed clans and inject their group-mates into this cycle.
    # Deduplicates against the already-scheduled set; falls back silently on error.
    _cwl_season_prefix = now.strftime("%Y-%m")
    _tracked_active_tags = [ct for ct, _ in active_clans]
    if _tracked_active_tags and CACHE.db_manager is not None:
        try:
            _group_member_tags = await CACHE.db_manager.get_active_cwl_group_member_tags(
                _cwl_season_prefix, _tracked_active_tags
            )
            _already_scheduled = {ct for ct, _ in clans_to_update}
            _cwl_bonus_tags = [t for t in _group_member_tags if t not in _already_scheduled]
            if _cwl_bonus_tags:
                logging.info(
                    f"[CWL-GROUP-EXPAND] Prefix {_cwl_season_prefix}: adding "
                    f"{len(_cwl_bonus_tags)} group-member clan(s) to this cycle's fetch list"
                )
                clans_to_update.extend((t, False) for t in _cwl_bonus_tags)
        except Exception as _cgx:
            logging.warning(f"[CWL-GROUP-EXPAND] Failed to expand group members: {_cgx}")

    active_count = len(active_clans)
    inactive_count = len(inactive_clans)
    logging.info(f"War updates: {active_count} active clans, {inactive_count} inactive clans ({INACTIVE_CLAN_UPDATE_INTERVAL_HOURS}h check)")

    # ============================================================================
    # PHASE 1: PARALLEL API FETCHES (async, all clans simultaneously)
    # ============================================================================
    # Semaphore to cap concurrent CoC API fetches.
    # Without this, asyncio.gather launches ALL clans simultaneously (potentially
    # thousands of tasks), creating hundreds of concurrent outgoing connections.
    # 50 balances throughput vs API/SSL connection pressure. ==> 20 for more safety margin.
    _FETCH_CONCURRENCY = 20
    _fetch_semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)

    # Pre-group inactive clans into chunks of 10.  Each clan carries a reference
    # to its group so the first clan in the group to acquire the semaphore can log
    # the whole chunk header — right before the API calls for that batch fire.
    # This keeps errors/warnings adjacent to the fetch info in the log.
    _inactive_tags_ordered = [t for t, active in clans_to_update if not active]
    _INACTIVE_BATCH = 10
    _inactive_group: dict[str, tuple[int, list[str]]] = {}
    for _gi in range(0, len(_inactive_tags_ordered), _INACTIVE_BATCH):
        _chunk = _inactive_tags_ordered[_gi : _gi + _INACTIVE_BATCH]
        _gidx  = _gi // _INACTIVE_BATCH
        for _t in _chunk:
            _inactive_group[_t] = (_gidx, _chunk)
    _logged_groups: set[int] = set()
    _group_log_lock = asyncio.Lock()

    async def fetch_single_clan(clan_tag: str, is_active: bool) -> Tuple[str, bool, Optional[dict[str, Any]]]:
        """
        Phase 1: Fetch war data from CoC API in parallel (semaphore-bounded).
        
        Returns:
            (clan_tag, is_active, war_data_dict or None)
        """
        async with _fetch_semaphore:
            try:
                # Log based on active/inactive status
                if is_active:
                    logging.info(f"[ACTIVE] Fetching {clan_tag}...")
                else:
                    # Log the group header once, just before the first clan in
                    # the group starts its API call.  Subsequent clans in the
                    # same group are already listed in that line.
                    _gidx, _gtags = _inactive_group[clan_tag]
                    if _gidx not in _logged_groups:
                        async with _group_log_lock:
                            if _gidx not in _logged_groups:   # double-check after acquiring lock
                                _logged_groups.add(_gidx)
                                logging.info(
                                    f"[INACTIVE {INACTIVE_CLAN_UPDATE_INTERVAL_HOURS}H FETCHING] "
                                    f"{', '.join(_gtags)}"
                                )
                
                war_data = await fetch_clan_war_data(clan_tag)
                if war_data is None:
                    CACHE.record_cycle_stat("api_no_war")
                    return (clan_tag, is_active, _NOT_IN_WAR)  # sentinel: confirmed no active war
                return (clan_tag, is_active, war_data)
            except Exception as e:
                _ename = type(e).__name__
                _emsg  = str(e).lower()
                _is_maint = "maintenance" in _emsg or "503" in _emsg
                if _is_maint and is_maintenance_detected():
                    # Maintenance already flagged globally; suppress per-clan ERROR spam.
                    # coc_health.py already logged one WARNING on first detection.
                    logging.debug(f"[PHASE-1] Maintenance fast-fail: {clan_tag}")
                else:
                    logging.error(f"[PHASE-1] Exception fetching clan {clan_tag}: {e}")
                # Bucket the failure reason for the cycle summary
                if "ratelimit" in _ename.lower() or "ratelimit" in _emsg or "429" in _emsg:
                    _bucket = "RateLimit"
                elif "timeout" in _ename.lower() or "timeout" in _emsg:
                    _bucket = "Timeout"
                elif "privatewarlog" in _ename.lower():
                    _bucket = "PrivateWarLog"
                elif "notfound" in _ename.lower() or "404" in _emsg:
                    _bucket = "NotFound"
                elif "maintenance" in _emsg or "503" in _emsg:
                    _bucket = "Maintenance"
                elif "network" in _emsg or "connection" in _emsg or "ssl" in _emsg:
                    _bucket = "Network"
                else:
                    _bucket = _ename
                CACHE.record_cycle_stat(f"api_fail:{_bucket}")
                return (clan_tag, is_active, None)

    logging.info(f"[PHASE-1] Starting parallel API fetches for {len(clans_to_update)} clans (max {_FETCH_CONCURRENCY} concurrent)...")
    fetch_tasks = [fetch_single_clan(clan_tag, is_active) for clan_tag, is_active in clans_to_update]
    fetch_results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
    logging.info(f"[PHASE-1] All API fetches completed")
    if is_maintenance_detected():
        _maint_n = CACHE.cycle_stats.get("api_fail:Maintenance", 0)
        logging.warning(
            f"[PHASE-1] CoC API in maintenance this cycle — {_maint_n} clan fetch(es) failed fast. "
            f"Phases 2-4 will still run on existing temp files. Next cycle will re-probe."
        )

    # Record Phase-1 API counts in cycle stats
    for _r in fetch_results:
        if isinstance(_r, Exception):
            CACHE.record_cycle_stat("api_fail:Exception")
        elif isinstance(_r, tuple) and len(_r) == 3 and _r[2] is not None:
            CACHE.record_cycle_stat("api_fetched")
        # None result: either api_no_war (recorded in fetch_single_clan) or api_fail:* (recorded in except block)

    # ============================================================================
    # PRE-SCAN: Shared temp/archive directory scans for Phase 2 + Phase 3
    # ============================================================================
    # Single glob.glob() + os.scandir() replaces per-clan scans that previously
    # re-read the entire temp/ and archive/ directory trees once per clan per
    # cycle. Directory sizes grow over time (tens of thousands of files in
    # temp/, hundreds of thousands in archive/ as of 2026-07 — see CACHE
    # counters / /admin status for current totals); re-scanning per clan was
    # the original bottleneck this pre-scan eliminates.
    # Wrapped in to_thread() to avoid blocking the event loop on slow I/O.
    import glob as _glob_mod
    _temp_dir = os.path.join(DATA_DIR, "temp")

    def _scan_temp_shards() -> list[str]:
        return [
            fp
            for sd in _glob_mod.glob(os.path.join(_temp_dir, "shard_*"))
            for fp in _glob_mod.glob(os.path.join(sd, "*_war_data.json"))
        ]

    _prescan_t0 = time.monotonic()
    _all_temp_files = await asyncio.to_thread(_scan_temp_shards)
    _temp_scan_elapsed = time.monotonic() - _prescan_t0
    CACHE.temp_total_file_count = len(_all_temp_files)  # keep /status total in sync with disk
    _temp_files_by_clan: dict[str, list[str]] = {}
    for _fp in _all_temp_files:
        _safe = os.path.basename(_fp).split("_")[0]
        _temp_files_by_clan.setdefault(_safe, []).append(_fp)
    _archive_dir = CONFIG.archive_dir
    _archive_scan_t0 = time.monotonic()
    try:
        def _scan_archive_names() -> set[str]:
            return {
                e.name
                for sd in os.scandir(_archive_dir)
                if sd.is_dir()
                for e in os.scandir(sd.path)
                if e.name.endswith("_war_data.json")
            }
        _archive_filenames: set[str] = await asyncio.to_thread(_scan_archive_names)
    except FileNotFoundError:
        _archive_filenames = set()
    _archive_scan_elapsed = time.monotonic() - _archive_scan_t0
    # INFO (not debug) so PROD logs show where the PHASE-1 → PHASE-3 gap goes
    # without needing to re-enable DEBUG. Both scans run in asyncio.to_thread()
    # so they never block the event loop — this is real disk-I/O wall-clock
    # time (server-machine/data directory listing over tens/hundreds of thousands of
    # files), not a blocking bug. See qapbot/scripts/log_time_gaps.py findings.
    logging.info(
        "[PRE-SCAN-TIMING] temp_scan=%.3fs (%d files, %d clans) archive_scan=%.3fs (%d files) total=%.3fs",
        _temp_scan_elapsed, len(_all_temp_files), len(_temp_files_by_clan),
        _archive_scan_elapsed, len(_archive_filenames),
        _temp_scan_elapsed + _archive_scan_elapsed,
    )

    # ============================================================================
    # PHASE 1.5: CWL WAR TAG RECOVERY BATCH (API fetch + temp file write only)
    # ============================================================================
    # Fetches up to _CWL_RECOVERY_BATCH_SIZE missing historical CWL war_tags per
    # cycle via get_league_war() and writes temp JSON files via save_war_object().
    # Phase 2 (orphan scanner) picks up the temp files and finalises them via
    # war_write_batch() — no per-write SQLite stalls in this phase.
    # Active only while data/missing_cwl_war_tags.txt exists (auto-deleted when done).
    if CACHE.pending_cwl_recovery:
        _phase15_t0 = time.monotonic()
        try:
            from QBhelperfunctions import process_cwl_recovery_batch  # type: ignore[attr-defined]
            await process_cwl_recovery_batch(_archive_filenames)
        except Exception as e:
            logging.error(f"[PHASE-1.5] CWL recovery batch error: {e}")
        logging.info(f"[PHASE-1.5-TIMING] Completed in {time.monotonic() - _phase15_t0:.3f}s")

    # ============================================================================
    # PHASE 2: PROCESS ORPHANED CWL WARS (async, before file processing)
    # ============================================================================
    # Collect clans that failed to fetch (None result)
    # These might have orphaned CWL wars that need processing
    failed_clans: set[str] = set()
    for result in fetch_results:
        # Skip exceptions from gather
        if isinstance(result, Exception):
            continue
        # Type guard - ensure result is tuple before unpacking
        if isinstance(result, tuple) and len(result) == 3:
            clan_tag, _, war_data = result
            if war_data is None:
                failed_clans.add(clan_tag)
    
    # Process orphaned CWL wars now so they can be finalized in Phase 3
    # Pass pre-scanned files so orphan scan avoids redundant glob/stat calls
    _phase2_finalized: set[str] = set()
    _phase2_t0 = time.monotonic()
    try:
        from QBhelperfunctions import process_orphaned_cwl_wars  # type: ignore[attr-defined]
        _phase2_finalized = await process_orphaned_cwl_wars(
            failed_clans, _temp_files_by_clan, _archive_filenames
        ) or set()
    except Exception as e:
        logging.error(f"[PHASE-2] Error processing orphaned CWL wars: {e}")
    logging.info(
        "[PHASE-2-TIMING] Completed in %.3fs (failed_clans=%d, finalized=%d)",
        time.monotonic() - _phase2_t0, len(failed_clans), len(_phase2_finalized),
    )

    # Fire WAL checkpoint as a background task so Phase 3 starts immediately.
    # war_write_batch() skips the checkpoint (skip_checkpoint=True) to avoid
    # blocking here; this task runs it concurrently with Phase 3 processing.
    if CACHE.db_manager is not None:
        asyncio.create_task(
            asyncio.to_thread(CACHE.db_manager.run_passive_checkpoint),
            name="phase2-wal-checkpoint",
        )
        logging.debug("[PHASE-2] WAL checkpoint dispatched as background task")

    # Refresh prescan for clans finalized in Phase 2 (files have moved to archive).
    # O(n) in-memory dict pops — NO filesystem I/O.
    # Previously used per-clan glob.glob() calls which caused 5225 × full-directory
    # scans on HDD/server-machine (11-minute stall for large orphan batches).
    # Popping is safe: finalized clan temp files were moved to archive; Phase 3's
    # manage_war_files() gets war_files_prescan=None → own glob → empty → quick no-op.
    if _phase2_finalized:
        for _fc_tag in _phase2_finalized:
            _safe_fc = _fc_tag.lstrip('#').upper()
            _temp_files_by_clan.pop(_safe_fc, None)
        logging.debug(f"[PRE-SCAN] Removed {len(_phase2_finalized)} finalized clan(s) from prescan cache (no I/O)")

    # ============================================================================
    # PHASE 3: SEQUENTIAL FILE PROCESSING (sync, after all API operations)
    # ============================================================================
    logging.info(f"[PHASE-3] Starting file processing for {len(fetch_results)} clans...")
    
    success_count = 0
    failed_count = 0

    # Collect timestamp updates for batch DB write at end of Phase-3
    _phase3_ts_batch: list[tuple[str, str]] = []  # (iso_timestamp, clan_tag)

    # ── P0 batch: collect all war DB writes across clans ─────────────
    # Instead of each process_clan_war_data/manage_war_files call committing
    # individually (causing HDD writeback stalls every ~12 writes), we collect
    # all war data in shared lists and flush in batched transactions of 50
    # wars per COMMIT at the end of Phase-3.
    _war_batch_appends: list[Any] = []   # (clan_tag, attack_rows, summary)
    _war_batch_updates: list[Any] = []   # (clan_tag, war_id, attack_rows, summary)
    _war_batch_file_moves: list[tuple[str, str]] = []  # (src, dst)

    def _with_war_batch(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Activate war batch mode on the worker thread, then call fn."""
        db = CACHE.db_manager
        assert db is not None
        db.activate_war_batch_on_thread(
            _war_batch_appends, _war_batch_updates, _war_batch_file_moves,
        )
        try:
            return fn(*args, **kwargs)
        finally:
            db.deactivate_war_batch_on_thread()

    # NOTE: Phase 3 offloads file/DB work to thread-pool workers via
    # asyncio.to_thread().  Each worker thread opens its own sqlite3
    # connection inside _get_sync_conn() — no shared connection needed.
    # (A previous shared-conn optimisation caused thread-safety errors
    # because the main thread's connection was reused by worker threads.)

    # Yield to the event loop after each clan so Discord can process heartbeats,
    # gateway messages, and user interactions (slash commands, buttons, modals).
    # Each process_clan_war_data() call blocks for ~10-50ms (file I/O), so yielding
    # every iteration keeps the event loop responsive with negligible overhead
    # (~3K * ~1µs = ~3ms total).  Previously yielding every 200 clans caused
    # 2-10s blocking gaps → WebSocket disconnects and unresponsive interactions.

    for _idx, result in enumerate(fetch_results):
        # Yield to event loop regularly for interaction responsiveness.
        # Each process_clan_war_data() blocks ~10-50ms (file I/O), so
        # yielding every 10 iterations caps blocking at ~100-500ms.
        if _idx % 10 == 0:
            await asyncio.sleep(0)

        # Handle exceptions from asyncio.gather
        if isinstance(result, Exception):
            logging.error(f"[PHASE-3] Unhandled exception in fetch: {result}")
            failed_count += 1
            continue
        
        # Type guard - ensure result is tuple before unpacking
        if not isinstance(result, tuple) or len(result) != 3:
            logging.error(f"[PHASE-3] Unexpected result type: {type(result)}")
            failed_count += 1
            continue
        
        clan_tag, is_active, war_data = result
        
        # Even if fetch failed or clan has no active war, still call manage_war_files()
        # to finalize any orphaned wars.  Distinguish two None-like outcomes:
        #   _NOT_IN_WAR: API responded successfully → clan not in war → update timestamp
        #   None:        API call failed (exception) → do NOT update timestamp (retry next cycle)
        if war_data is _NOT_IN_WAR or war_data is None:
            _confirmed_no_war = war_data is _NOT_IN_WAR
            failed_count += 1
            # Only call manage_war_files when this clan actually HAS temp files
            _safe_tag = clan_tag.lstrip('#').upper()
            _prescan = _temp_files_by_clan.get(_safe_tag)
            if _prescan:
                try:
                    from QBhelperfunctions import manage_war_files
                    await asyncio.to_thread(
                        _with_war_batch, manage_war_files, clan_tag, "",
                        _prescan, _archive_filenames
                    )
                    _reason = "no active war" if _confirmed_no_war else "fetch failed"
                    logging.debug(f"[PHASE-3] Finalized orphaned wars for {clan_tag} ({_reason})")
                except Exception as e:
                    logging.error(f"[PHASE-3] Error finalizing orphaned wars for {clan_tag}: {e}")
            
            # BUGFIX (2026-02-05): Clear temp_war_stats when no active war
            # Prevents stale data in currentwar leaderboards during month transitions
            # Root cause: When API returns None (notInWar), process_clan_war_data() isn't called,
            # leaving old temp stats in memory. This caused leaderboards to freeze during CWL
            # month transitions (Jan→Feb) when wars ended before new ones started.
            CACHE.set_temp_war_stats(clan_tag, {})
            logging.debug(f"[PHASE-3-TEMP-CLEAR] Cleared temp_war_stats for {clan_tag} (no active war)")
            
            if _confirmed_no_war:
                # API confirmed no active war: update last_war_update so this clan leaves
                # the overdue backlog for 22h instead of being re-polled every cycle.
                # Without this, no_war clans permanently occupy polling capacity (observed:
                # 12K+ no_war clans polled each cycle, taking 81% of the 15K/cycle budget).
                _clan_data_niw = CACHE.clan_name_cache.get(clan_tag)
                if _clan_data_niw:
                    _ts_niw = now.isoformat()
                    _clan_data_niw['last_war_update'] = _ts_niw
                    _phase3_ts_batch.append((_ts_niw, clan_tag))
            continue
        
        try:
            # Process war data (file operations) - offloaded to thread to keep
            # the event loop responsive during sync file I/O.
            _safe_tag = clan_tag.lstrip('#').upper()
            process_success = await asyncio.to_thread(
                _with_war_batch, process_clan_war_data, clan_tag, war_data,
                _temp_files_by_clan.get(_safe_tag, []),
                _archive_filenames
            )
            
            if not process_success:
                failed_count += 1
                continue
            
            # Update timestamp on success (batch DB write at end of loop)
            clan_data = CACHE.clan_name_cache.get(clan_tag)
            if clan_data and isinstance(clan_data, dict):  # type: ignore[misc]
                update_timestamp = now.isoformat()
                
                # For inactive clans: smart timestamp for ongoing wars
                if not is_active:
                    war_state = war_data.get('state')
                    logging.debug(f"[INACTIVE-BACKDATE-CHECK] {clan_tag}: war_state='{war_state}' (type: {type(war_state).__name__})")
                    
                    # Check for both 'inWar' (camelCase from old API), 'in_war' (underscore), and 'In War' (with space)
                    # war_state can be a string or a WarState enum object - convert to string first
                    war_state_str = str(war_state).lower().replace(' ', '') if war_state else ''
                    if war_state_str == 'inwar':
                        logging.debug(f"[INACTIVE-BACKDATE-CHECK] {clan_tag}: War state matches, attempting to parse end_time...")
                        try:
                            war_obj = war_data.get('war_obj')
                            raw_end = getattr(war_obj, 'end_time', None) if war_obj else None
                            logging.debug(f"[INACTIVE-BACKDATE-CHECK] {clan_tag}: raw_end='{raw_end}'")
                            if raw_end:
                                m = _DT_RE.search(str(raw_end))
                                if m:
                                    dt_args = [int(x.strip()) for x in m.group(1).split(',')]
                                    if len(dt_args) >= 5:
                                        y, mo, d, h, mi = dt_args[:5]
                                        war_end_time = datetime(y, mo, d, h, mi, tzinfo=timezone.utc)
                                        backdated_time = war_end_time - timedelta(hours=INACTIVE_CLAN_UPDATE_INTERVAL_HOURS, minutes=8)
                                        update_timestamp = backdated_time.isoformat()
                                        next_update = backdated_time + timedelta(hours=INACTIVE_CLAN_UPDATE_INTERVAL_HOURS)
                                        logging.info(f"[INACTIVE] {clan_tag}: Smart timestamp set - War ends {war_end_time.isoformat()}, next update {next_update.isoformat()}")
                                    else:
                                        logging.debug(f"[INACTIVE-BACKDATE-CHECK] {clan_tag}: Not enough datetime args: {dt_args}")
                                else:
                                    logging.debug(f"[INACTIVE-BACKDATE-CHECK] {clan_tag}: Regex did not match raw_end")
                            else:
                                logging.debug(f"[INACTIVE-BACKDATE-CHECK] {clan_tag}: raw_end is None/empty")
                        except Exception as parse_ex:
                            logging.debug(f"[INACTIVE] {clan_tag}: Could not parse war end time: {parse_ex}")
                    else:
                        logging.debug(f"[INACTIVE-BACKDATE-CHECK] {clan_tag}: War state '{war_state}' does not match, using normal timestamp")
                
                # Update in-memory immediately; batch the DB write
                clan_data['last_war_update'] = update_timestamp
                _phase3_ts_batch.append((update_timestamp, clan_tag))
                if is_active:
                    logging.debug(f"[ACTIVE] {clan_tag} - war data processed successfully")
                else:
                    logging.debug(f"[INACTIVE] {clan_tag} - war data processed successfully")
            
            success_count += 1
        except Exception as e:
            logging.error(f"[PHASE-3] Exception processing clan {clan_tag}: {e}")
            failed_count += 1

    # ============================================================================
    # PHASE 3B: FINALIZE TEMP FILES FOR PASSIVE CLANS (track_war_updates=False)
    # ============================================================================
    # Passive clans are skipped in Phase 1 (no polling) and never appear in
    # fetch_results, so manage_war_files() is never called for them.  Their
    # war_ended (and stale in_war) temp files accumulate indefinitely.
    # Fix: iterate the pre-scan for any clan NOT polled this cycle and call
    # manage_war_files() with current_opponent_tag="" (→ None) so ALL their
    # files are treated as old wars and finalized normally.
    # CWL files still in in_war state are left for the Phase 2 orphan scanner.
    #
    # OPTIMISATION: Only process clans whose temp file was modified since the
    # last cycle.  A passive clan's file can only change when Phase 1 polls it
    # (22h threshold).  If the file hasn't changed, its state hasn't changed,
    # so re-reading the JSON is pure waste (~12K HDD reads every 5 min).
    # Threshold: files modified within the last 10 min (2× cycle interval)
    # to catch polls near the boundary.  First cycle after restart processes
    # everything because _cycle_mtime_cutoff is 0.
    import time as _time_mod
    _now_ts = _time_mod.time()
    _cycle_mtime_cutoff = getattr(QBcore.bot, '_last_phase3b_time', 0.0)
    # Use a generous window: anything modified after (last_phase3b - 60s)
    # to avoid missing files written during the overlap between Phase 1
    # completing and prescan running.
    _mtime_threshold = _cycle_mtime_cutoff - 60.0 if _cycle_mtime_cutoff > 0 else 0.0
    _polled_safe_tags: set[str] = {ct.lstrip('#').upper() for ct, _ in clans_to_update}
    # Skip-until dict: {safe_tag: unix_timestamp} — clans to skip until their war
    # ends or their state changes.  Zero server-machine I/O per skipped clan.  Persists across
    # cycles via QBcore.bot._p3b_skip_until.  Pre-seeded from CACHE.temp_war_metadata
    # so even cycle 1 after restart skips clans whose wars are far from ending.
    _p3b_skip_until: dict[str, float] = getattr(QBcore.bot, '_p3b_skip_until', {})
    _passive_shard_tags: list[str] = []
    _passive_skipped = 0
    # PERFORMANCE/BLOCKING NOTE: _temp_files_by_clan holds ~26K entries in PROD
    # (every clan with at least one temp war file). This loop previously had
    # ZERO yield points, so it ran as one uninterrupted synchronous block every
    # cycle. Tier 1/2 (skip-until dict, in-memory metadata) are cheap, but any
    # clan reaching Tier 3 costs a real os.path.getmtime() syscall per file —
    # clans with no temp_war_metadata entry (or a state Tier 2 doesn't cover)
    # fall straight through to Tier 3 every single cycle. Fix: yield every 2000
    # clans (same pattern as the clan-categorization loop fix) so the event loop
    # can interleave heartbeat/interaction handling throughout, and add timing +
    # a Tier-3 hit counter to measure the real cost going forward.
    _p3b_scan_t0 = time.monotonic()
    _p3b_tier3_hits = 0
    _p3b_scan_count = 0
    for safe_tag in _temp_files_by_clan:
        _p3b_scan_count += 1
        if _p3b_scan_count % 2000 == 0:
            await asyncio.sleep(0)
        if safe_tag in _polled_safe_tags:
            continue
        # ── Tier 1: skip-until dict check (zero server-machine I/O) ───────────────
        _su_ts = _p3b_skip_until.get(safe_tag, 0.0)
        if _su_ts > _now_ts:
            _passive_skipped += 1
            continue
        # ── Tier 2: metadata pre-seed (zero server-machine I/O, in-memory only) ───
        # For clans not yet covered by skip-until (or whose entry just
        # expired), check CACHE.temp_war_metadata.  If the war is still
        # active and won't end soon, add a skip-until entry and skip.
        _meta_ps = CACHE.temp_war_metadata.get(f"#{safe_tag}")
        if _meta_ps:
            _ps_state = _meta_ps.get('state', '')
            if _ps_state in ('in_war', 'inwar'):
                _ps_end = _parse_dt_from_raw(_meta_ps.get('end_time', ''))
                if _ps_end:
                    _secs_to_end = (_ps_end - now).total_seconds()
                    if _secs_to_end > 300:  # war ends in more than 5 min
                        # Retry 4.5 min before the expected end
                        _p3b_skip_until[safe_tag] = _ps_end.timestamp() - 270
                        _passive_skipped += 1
                        continue
                    elif _secs_to_end > 0:
                        # 0–300 s remaining: war is almost over but Phase-1's
                        # 22h re-poll should be imminent (smart backdating fires
                        # just before war end).  Fall through to Tier-3 mtime:
                        # if Phase-1 already wrote a fresh file, mtime is recent
                        # and we'll process it; otherwise skip for one more cycle.
                        pass  # fall through to Tier-3
                    else:
                        # end_time is in the past: bypass Tier-3 mtime so stale
                        # in_war files (old mtime, never re-polled) are not
                        # permanently stuck.  manage_war_files' own end_time guard
                        # confirms the war is over before finalizing.
                        _passive_shard_tags.append(safe_tag)
                        continue
            elif _ps_state == 'war_ended':
                # War is already done — finalize immediately regardless of mtime.
                # war_ended files are written by save_war_object() at the time of
                # the clan's last Phase-1 poll, which can be up to 22h ago.  Tier-3
                # mtime would skip them every cycle until the next 22h poll fires,
                # causing up to 22h of unnecessary linger.  Bypass Tier-3 entirely.
                _passive_shard_tags.append(safe_tag)
                continue
            elif _ps_state == 'preparation':
                _ps_start = _parse_dt_from_raw(_meta_ps.get('start_time', ''))
                if _ps_start:
                    _secs_to_start = (_ps_start - now).total_seconds()
                    if _secs_to_start > 300:  # war hasn't started yet
                        # Retry 4.5 min after the scheduled war start
                        _p3b_skip_until[safe_tag] = _ps_start.timestamp() + 270
                        _passive_skipped += 1
                        continue
        files = _temp_files_by_clan[safe_tag]
        # ── Tier 3: mtime check (server-machine stat — only for remaining clans) ───
        if _mtime_threshold > 0:
            _p3b_tier3_hits += 1
            newest_mtime = max(os.path.getmtime(f) for f in files)
            if newest_mtime < _mtime_threshold:
                _passive_skipped += 1
                continue
        _passive_shard_tags.append(safe_tag)
    logging.info(
        "[PHASE-3B-SCAN-TIMING] Scanned %d clan(s) in %.3fs (tier3_stat_calls=%d, candidates=%d)",
        _p3b_scan_count, time.monotonic() - _p3b_scan_t0, _p3b_tier3_hits, len(_passive_shard_tags),
    )
    QBcore.bot._last_phase3b_time = _time_mod.time()  # type: ignore[attr-defined]
    if _passive_shard_tags:
        from QBhelperfunctions import manage_war_files as _mwf_p3b  # type: ignore[attr-defined]
        logging.info(f"[PHASE-3B] Finalizing temp files for {len(_passive_shard_tags)} passive clan(s) ({_passive_skipped} unchanged, skipped)...")
        _p3b_done = 0
        for _p3b_idx, _p3b_safe_tag in enumerate(_passive_shard_tags):
            if _p3b_idx % 10 == 0:
                await asyncio.sleep(0)
            _p3b_prescan = _temp_files_by_clan.get(_p3b_safe_tag)
            if not _p3b_prescan:
                continue
            # Snapshot batch sizes before calling manage_war_files so we can
            # detect whether anything was actually finalized for this clan.
            _p3b_appends_before = len(_war_batch_appends)
            _p3b_moves_before   = len(_war_batch_file_moves)
            try:
                await asyncio.to_thread(
                    _with_war_batch, _mwf_p3b, f"#{_p3b_safe_tag}", "",
                    _p3b_prescan, _archive_filenames
                )
                _p3b_done += 1
                if (len(_war_batch_appends) == _p3b_appends_before
                        and len(_war_batch_file_moves) == _p3b_moves_before):
                    # Nothing finalized (still in_war / not_ready):
                    # skip for one full sleep interval so the next cycle can
                    # spend its server-machine budget elsewhere.
                    _p3b_skip_until[_p3b_safe_tag] = _now_ts + CONFIG.sleep_interval
                else:
                    # War was finalized — remove skip-until so the clan is
                    # checked normally from next cycle onwards.
                    _p3b_skip_until.pop(_p3b_safe_tag, None)
            except Exception as _p3b_ex:
                logging.error(f"[PHASE-3B] Error processing passive clan #{_p3b_safe_tag}: {_p3b_ex}")
                # Don't add to skip-until on error; retry next cycle
            # Flush every 5000 clans to cap memory usage (prevents OOM on server-machine after CWL season
            # when passive clans can accumulate ~6 CWL wars each => millions of tuples in RAM).
            # Previous interval of 1000 produced many tiny batches (1 append + ~10 updates)
            # each costing 2-4s for COMMIT on HDD — ~80s total.  5000 amortises the overhead.
            if (_p3b_idx + 1) % 5000 == 0 and (_war_batch_appends or _war_batch_updates):
                _p3b_flush_ok = False
                try:
                    _p3b_flush_a = len(_war_batch_appends)
                    _p3b_flush_u = len(_war_batch_updates)
                    await asyncio.to_thread(
                        CACHE.db_manager.flush_pending_war_writes,  # type: ignore[union-attr]
                        _war_batch_appends, _war_batch_updates, 50,
                        True,  # skip_checkpoint — final flush will checkpoint
                    )
                    _p3b_flush_ok = True
                    logging.info(
                        f"[PHASE-3B] Intermediate flush at {_p3b_idx+1}/{len(_passive_shard_tags)}: "
                        f"{_p3b_flush_a} appends, {_p3b_flush_u} updates"
                    )
                    CACHE.record_cycle_stat("db_appends", _p3b_flush_a)
                    CACHE.record_cycle_stat("db_updates", _p3b_flush_u)
                except Exception as _p3b_flush_ex:
                    logging.error(f"[PHASE-3B] Intermediate flush failed at {_p3b_idx+1}: {_p3b_flush_ex}")
                if _p3b_flush_ok:
                    _war_batch_appends.clear()
                    _war_batch_updates.clear()
                    if _war_batch_file_moves:
                        _p3b_moves_snap = list(_war_batch_file_moves)
                        _war_batch_file_moves.clear()
                        def _do_p3b_interim_moves():
                            for src, dst in _p3b_moves_snap:
                                try:
                                    os.replace(src, dst)
                                except FileNotFoundError:
                                    pass
                        await asyncio.to_thread(_do_p3b_interim_moves)
                        logging.info(f"[PHASE-3B] Interim file moves: {len(_p3b_moves_snap)}")
        logging.info(f"[PHASE-3B] Completed: {_p3b_done}/{len(_passive_shard_tags)} passive clans processed")
        CACHE.record_cycle_stat("p3b_passive_processed", _p3b_done)
        CACHE.record_cycle_stat("p3b_passive_skipped",   _passive_skipped)

    # Prune skip-until dict: drop entries whose deadline has passed (the clan
    # will be re-evaluated next cycle via metadata or mtime).  Save the pruned
    # dict back to bot so it survives into the next cycle.
    QBcore.bot._p3b_skip_until = {  # type: ignore[attr-defined]
        k: v for k, v in _p3b_skip_until.items() if v > _now_ts
    }

    # ── P0 + P1: flush collected war DB writes in batched transactions ──
    _p0_flush_ok = False
    if _war_batch_appends or _war_batch_updates:
        try:
            await asyncio.to_thread(
                CACHE.db_manager.flush_pending_war_writes,  # type: ignore[union-attr]
                _war_batch_appends, _war_batch_updates, 50,
                True,  # skip_checkpoint — single background checkpoint below
            )
            logging.info(
                f"[PHASE-3] P0 batch flush: {len(_war_batch_appends)} appends, "
                f"{len(_war_batch_updates)} updates"
            )
            CACHE.record_cycle_stat("db_appends", len(_war_batch_appends))
            CACHE.record_cycle_stat("db_updates", len(_war_batch_updates))
            _p0_flush_ok = True
        except Exception as e:
            logging.error(
                f"[PHASE-3] P0 batch flush FAILED: {e} — "
                f"skipping {len(_war_batch_file_moves)} deferred file move(s) to prevent archive/DB divergence"
            )

    # Execute deferred file moves only when DB writes succeeded.
    # If the flush failed, files must stay in temp so the next cycle retries them.
    if _war_batch_file_moves and _p0_flush_ok:
        def _do_deferred_moves():
            for src, dst in _war_batch_file_moves:
                try:
                    os.replace(src, dst)
                except FileNotFoundError:
                    pass
        await asyncio.to_thread(_do_deferred_moves)
        logging.info(f"[PHASE-3] Deferred {len(_war_batch_file_moves)} file moves completed")

    # Flush all Phase-3 timestamp updates in a single DB transaction
    if _phase3_ts_batch:
        try:
            await CACHE.db_manager.bulk_update_clan_timestamps(_phase3_ts_batch)  # type: ignore[union-attr]
        except Exception as e:
            logging.error(f"[PHASE-3] Failed to batch-persist timestamps: {e}")

    # Fire a single background WAL checkpoint after all Phase-3 DB writes
    # (war data + timestamps) are complete.  Previous approach ran a
    # checkpoint inside each flush_pending_war_writes() call, causing the
    # subsequent timestamp flush to stall (419 timestamps in 4.75s vs 0.19s
    # when the WAL is clean).
    if CACHE.db_manager is not None:
        asyncio.create_task(
            asyncio.to_thread(CACHE.db_manager.run_passive_checkpoint),
            name="phase3-wal-checkpoint",
        )

    logging.info(f"[PHASE-3] File processing completed")

    # Refresh temp file count so /status reflects archives made this cycle.
    # All deferred file moves (Phase 3 + Phase 3B intermediate flushes) are
    # done by this point, so a fresh scan gives the true disk count.
    try:
        CACHE.temp_total_file_count = len(await asyncio.to_thread(_scan_temp_shards))
    except Exception as _rescan_ex:
        logging.warning(f"[POST-CYCLE-SCAN] Could not refresh temp_total_file_count: {_rescan_ex}")

    # ── Memory hygiene: release heavy objects no longer needed ────────
    # fetch_results holds coc.War objects (each with 30-100 ClanWarMember
    # sub-objects) that would otherwise stay alive through notifications,
    # leaderboard posting, and role-sync below.
    del fetch_results, fetch_tasks, _temp_files_by_clan, _archive_filenames
    del _war_batch_appends, _war_batch_updates, _war_batch_file_moves, _phase3_ts_batch

    # Evict expired CWL cache entries to prevent unbounded growth.
    CACHE.evict_stale_cwl_caches()

    # Update throttle_backlog to the post-cycle remaining count.
    # _overdue_total was snapshotted before the cap; inactive_count clans were
    # actually polled (and all got timestamps updated unless they hit a genuine
    # API failure, which is ~0 in practice).  Subtract to get how many are still
    # overdue so CYCLE-SUMMARY and /status reflect the actual current state.
    CACHE.cycle_stats["throttle_backlog"] = max(0, _overdue_total - inactive_count)

    # Get CoC API statistics for this cycle
    coc_stats = get_coc_stats()
    _no_war_ct = CACHE.cycle_stats.get("api_no_war", 0)
    _actual_fail_ct = failed_count - _no_war_ct  # genuine failures (API errors + process_clan_war_data failures)
    
    if coc_stats['cycle_rate_limits'] > 0:
        logging.warning(
            f"Clan updates complete: {success_count} successful, {_no_war_ct} no_war, {_actual_fail_ct} failed "
            f"({active_count} active, {inactive_count} inactive). "
            f"⚠️ CoC API Rate Limits: {coc_stats['cycle_rate_limits']}/{coc_stats['cycle_total_calls']} calls, "
            f"Sleep time: {coc_stats['cycle_sleep_time']:.1f}s"
        )
    else:
        logging.info(
            f"Clan updates complete: {success_count} successful, {_no_war_ct} no_war, {_actual_fail_ct} failed "
            f"({active_count} active, {inactive_count} inactive). "
            f"CoC API calls: {coc_stats['cycle_total_calls']}"
        )
    
    # Check for war notifications (send DM reminders to players)
    try:
        from qapbot.war_notifications import check_wars_for_notifications
        await check_wars_for_notifications()
    except Exception as e:
        logging.error(f"Error in war notifications: {e}")

    # All remaining steps post to Discord — skip them entirely during a known outage.
    from qapbot.discord_health import is_discord_available
    if not is_discord_available():
        logging.warning(
            "[DISCORD-HEALTH] Discord outage detected — skipping role sync, "
            "leaderboard posts, and registration reposts this cycle."
        )
        return

    # Sync Discord roles for guilds with CoC/clan role features enabled
    try:
        from qapbot.guild_role_manager import sync_all_roles_for_guild
        for _guild in QBcore.bot.guilds:
            _gid = str(_guild.id)
            # DEV mode: only sync the dev guild, skip all others
            if CONFIG.is_dev_mode and _guild.id != CONFIG.discord_guild_id:
                logging.debug(f"[ROLE-SYNC] DEV mode: skipping guild {_guild.name} ({_gid})")
                continue
            _cfg = CACHE.server_config.get(_gid, {})
            if _cfg.get("coc_role_enabled") or _cfg.get("clan_role_enabled"):
                await sync_all_roles_for_guild(_guild, _gid)
    except Exception as e:
        logging.error(f"Error in periodic role sync: {e}")

    logging.info("Starting Discord leaderboard posts...")
    await post_leaderboards_to_subscribed_channels()
    logging.info("Discord leaderboard posts complete.")
    await repost_playerregistration_messages(only_if_not_bottom=True, bump_cooldown_seconds=PLAYERREGISTRATION_BUMP_COOLDOWN_SECONDS)

def _archive_move_nightly() -> None:
    """
    Thread-safe wrapper for the nightly archive file move step.

    Moves war JSON files older than 7 days from archive/ (eSATA SSD) to
    archive_old/ (eSATA SSD staging), preserving shard sub-directory structure.
    Both dirs share the same SSD mount point so os.replace() is a pure metadata
    rename — no data is copied and no SSD wear is incurred.
    ArchiveOld.bat subsequently compresses archive_old/ into archive_compressed/
    on the HDD.
    Runs before DB maintenance so Discord commands are fully responsive during
    the file I/O phase.

    Logs results and all errors via the bot logger (not print); swallows all
    exceptions so a failure here never prevents DB maintenance from starting.
    """
    import logging as _log
    import os as _os
    import time as _time
    try:
        from qapbot.move_old_archive_files import run as _archive_run
    except ImportError as _ie:
        _log.warning(f"[ARCHIVE-MOVE] Could not import move_old_archive_files: {_ie}")
        return

    source_dir = _os.path.abspath(CONFIG.archive_dir)
    dest_dir   = _os.path.abspath(CONFIG.archive_old_dir)
    days       = 7.0

    _log.info(
        f"[ARCHIVE-MOVE] Starting nightly archive file move "
        f"(source={source_dir}, dest={dest_dir}, threshold={days:g}d)..."
    )
    _t0 = _time.monotonic()
    try:
        result = _archive_run(source_dir=source_dir, dest_dir=dest_dir, days=days)
        elapsed = _time.monotonic() - _t0
        _log.info(
            f"[ARCHIVE-MOVE] Done in {elapsed:.1f}s — "
            f"moved={result['moved']:,}, skipped={result['skipped']:,}, "
            f"bytes={result['total_bytes']:,}"
        )
    except FileNotFoundError as _fnf:
        _log.warning(f"[ARCHIVE-MOVE] Skipped — {_fnf}")
    except Exception as _exc:
        _log.error(f"[ARCHIVE-MOVE] Failed: {_exc} — DB maintenance will still proceed")


async def run_nightly_maintenance_routine(db_mgr: Any, run_migration: bool) -> str:
    """
    Full nightly maintenance routine: Step 0 (archive file move) + Step 0.5
    (optional monthly hot->history migration) + Steps 1-3 (WAL checkpoint,
    REINDEX/VACUUM, ANALYZE).

    Shared by the 03:00 UTC scheduled nightly task and the
    /admin Execute Nightly Maintenance command, so both always run the exact
    same sequence — no risk of the admin-triggered run silently skipping the
    archive move or monthly migration steps.

    Args:
        db_mgr: CACHE.db_manager (must not be None).
        run_migration: Whether Step 0.5 (monthly_history_migration) should run
            this call. Callers decide this (e.g. day-of-month gate for the
            scheduled task; same gate re-checked for the admin command).

    Returns:
        The nightly_db_maintenance() result string (for display to the caller).
    """
    logging.info("[NIGHTLY-MAINTENANCE] START")
    _maint_t0 = time.monotonic()
    try:
        # Step 0: move old archive files to archive_old/ — no DB operations,
        # no user-interaction blocking.
        await asyncio.to_thread(_archive_move_nightly)
        # Step 0.5 (when due): migrate data older than the hot retention
        # window (current + previous calendar month) to the history DB,
        # before the VACUUM/REINDEX pass below so the freed space from the
        # migration DELETEs is picked up by nightly_db_maintenance()'s
        # freelist-based VACUUM trigger in the same run.
        if run_migration:
            await db_mgr.monthly_history_migration()
        # Steps 1-3: WAL checkpoint → REINDEX/VACUUM → ANALYZE (blocks
        # Discord commands internally via db_maintenance_mode).
        return await db_mgr.nightly_db_maintenance()
    finally:
        logging.info(
            "[NIGHTLY-MAINTENANCE] END — total duration %.1fs",
            time.monotonic() - _maint_t0,
        )


async def is_monthly_migration_due() -> bool:
    """
    Check whether the monthly hot->history DB migration is due (day == 1 of
    the calendar month, not yet run this month), hydrating
    CACHE.last_history_migration from bot_metadata on first use.

    If due, this immediately CLAIMS it by setting CACHE.last_history_migration
    to now — the caller is expected to actually run monthly_history_migration()
    right after getting True back. This prevents the scheduled nightly task and
    /admin Execute Nightly Maintenance from double-running the migration on the
    same day if both happen to check within the same window.

    Returns:
        True if the caller should run db_manager.monthly_history_migration() now.
    """
    if CACHE.db_manager is None:
        return False
    _now_utc = datetime.now(timezone.utc)
    if CACHE.last_history_migration is None:
        _stored_mig = await CACHE.db_manager.get_bot_metadata("last_history_migration")
        if _stored_mig:
            try:
                CACHE.last_history_migration = datetime.fromisoformat(_stored_mig)
            except ValueError:
                pass
    _due = (
        _now_utc.day == 1
        and (
            CACHE.last_history_migration is None
            or CACHE.last_history_migration.year != _now_utc.year
            or CACHE.last_history_migration.month != _now_utc.month
        )
    )
    if _due:
        CACHE.last_history_migration = _now_utc
    return _due


async def periodic_main() -> None:
    """
    Periodic main loop with exponential backoff error handling and graceful shutdown.

    Behavior:
        - Calls main() at regular intervals (CONFIG.sleep_interval)
        - Implements exponential backoff on errors (up to max_backoff seconds)
        - Detects asyncio event loop mismatches and forces process exit
        - Waits for shutdown_event before initiating cleanup
        - Ensures bot connection is properly closed

    Error Recovery:
        - Normal errors: Exponential backoff from sleep_interval to max_backoff
        - Event loop errors: Immediate process termination to prevent corruption
        - Cleanup failures: Logged but don't prevent shutdown

    Shutdown Process:
        1. Receives shutdown signal (SIGINT/SIGTERM or manual event)
        2. Completes current main() cycle if running
        3. Calls async_cleanup() to save caches and close connections
        4. Closes bot connection to allow bot.run() to return

    Note:
        This is the top-level control loop that manages the entire bot lifecycle.
        All cache and file operations are routed through cache_manager.py.
    """
    # Wait for bot to be fully initialized before starting periodic tasks
    # This prevents race conditions where periodic_main() and on_ready() both
    # call repost_playerregistration_messages() at the same time
    while not hasattr(QBcore.bot, "fully_initialized") or not QBcore.bot.fully_initialized:
        if QBcore.shutdown_event.is_set():
            return  # Don't start if shutdown is already requested
        await asyncio.sleep(0.5)
    
    logging.debug("periodic_main: Bot fully initialized, starting periodic update loop")

    # ONE-TIME STARTUP: Migrate any flat war files to sharded subdirectories.
    # os.replace() on the same filesystem is an atomic rename — no data copy.
    # Safe to re-run: scan only yields direct-child files; sharded files are
    # already inside shard_* subdirs and are invisible to the root scandir.
    try:
        from QBcsvhandling import migrate_war_files_to_shards as _migrate_shards
        _t_moved, _a_moved = await asyncio.to_thread(_migrate_shards)
        if _t_moved or _a_moved:
            logging.info(
                f"[SHARD-MIGRATION] Moved {_t_moved} temp / {_a_moved} archive files to sharded subdirs"
            )
    except Exception as _mig_ex:
        logging.error(f"[SHARD-MIGRATION] Migration error: {_mig_ex}")

    backoff = sleep_interval
    max_backoff = MAX_RETRY_BACKOFF_SECONDS
    while not QBcore.shutdown_event.is_set():
        # Pause update cycles while maintenance mode is active
        if QBcore.maintenance_mode:
            try:
                await asyncio.wait_for(QBcore.shutdown_event.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                pass
            continue

        # Gate: wait for /admin Backfill CWL Groups to finish before starting a
        # new update cycle.  The backfill makes many CoC API calls (one per group);
        # running a cycle concurrently would add unnecessary API pressure and could
        # race on cwl_league_rounds DB writes.  Normally completes within seconds;
        # only blocks when a large backfill was triggered during the sleep phase.
        if not QBcore.backfill_idle_event.is_set():
            logging.info("[CYCLE] Waiting for CWL group backfill to complete before starting next cycle...")
            await QBcore.backfill_idle_event.wait()
            logging.info("[CYCLE] CWL group backfill finished — starting update cycle.")

        QBcore.cycle_idle_event.clear()  # signal: cycle is now running
        QBcore.in_main_cycle = True
        _cycle_start = datetime.now(timezone.utc)
        logging.info(f"[CYCLE-START] Beginning update cycle at {_cycle_start.strftime('%H:%M:%S')} UTC")

        # Feature toggle: NO_DEV_MODE_UPDATES
        # When enabled, skip main() in dev mode and go straight to the sleep phase.
        if NO_DEV_MODE_UPDATES and CONFIG.is_dev_mode:
            logging.info("Update cycle skipped due to feature toggle NO_DEV_MODE_UPDATES=true")
            QBcore.in_main_cycle = False
            QBcore.cycle_idle_event.set()  # signal: cycle finished (skipped)
            if not QBcore.shutdown_event.is_set():
                logging.info(f"Sleeping for {sleep_interval} seconds. Press CTRL+C now to terminate.")
                _t_shutdown = asyncio.ensure_future(QBcore.shutdown_event.wait())
                _t_force = asyncio.ensure_future(QBcore.force_cycle_event.wait())
                try:
                    _done, _pending = await asyncio.wait(
                        {_t_shutdown, _t_force},
                        timeout=sleep_interval,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for _t in _pending:
                        _t.cancel()
                    if _t_force in _done:
                        QBcore.force_cycle_event.clear()
                        logging.info("[CYCLE] Sleep phase interrupted by admin command — starting next cycle immediately.")
                except Exception:
                    pass
            continue

        try:
            await main()
            _cycle_elapsed = (datetime.now(timezone.utc) - _cycle_start).total_seconds()
            if CACHE.last_cwl_recovery_summary:
                logging.info(CACHE.last_cwl_recovery_summary)
                CACHE.last_cwl_recovery_summary = ""
            for _summary_line in CACHE.format_cycle_summary():
                logging.info(_summary_line)
            logging.info(f"[CYCLE-END] Update cycle completed in {_cycle_elapsed:.1f}s")
            # Track cycle runtime stats
            cs = QBcore.cycle_stats
            cs["count"] += 1
            cs["total_s"] += _cycle_elapsed
            if cs["min_s"] is None or _cycle_elapsed < cs["min_s"]:
                cs["min_s"] = _cycle_elapsed
            if cs["max_s"] is None or _cycle_elapsed > cs["max_s"]:
                cs["max_s"] = _cycle_elapsed
            CACHE.lifetime_stats["cycles_completed"] = CACHE.lifetime_stats.get("cycles_completed", 0) + 1
            backoff = sleep_interval

            # Memory management: evict expired clan-cache entries and recover
            # GC pressure.  Both operations are offloaded to a thread because
            # clear_expired() iterates 3000+ entries with datetime comparisons
            # and gc.collect() — while normally fast — becomes CPU-heavy when
            # tracemalloc is active (every freed object's trace is deallocated).
            # Running them on the event loop risked starving discord.py's heartbeat.
            #
            # NOTE ON asyncio.to_thread() AND THE GIL: offloading to a worker
            # thread only lets discord.py's heartbeat *task* stay scheduled while
            # this function runs — it does NOT make gc.collect() itself non-blocking.
            # gc.collect() is a single atomic C call: CPython does not release the
            # GIL while it runs, so for its whole duration NO other thread (incl.
            # the event-loop thread) can execute Python bytecode. A full gc.collect()
            # (generation 2) walks every long-lived object in the process — which on
            # this bot includes the entire CACHE state (clan caches, war caches,
            # user accounts, etc., easily hundreds of thousands of objects) — and
            # was regularly measured taking 1s+ on the server-machine CPU, effectively freezing
            # the whole bot (including Discord command handling) for that long even
            # though it "runs in a thread".
            #
            # FIX: collect only generations 0+1, not the full gen-2 sweep.
            # aiohttp ClientResponse has __del__, so response body buffer chunks
            # (74B × 5M+ per cycle = 361 MB) survive gen-0 GC and get promoted to
            # gen-1 during the cycle. gc.collect(0) skips gen-1 entirely, causing
            # those buffers to accumulate unreleased across cycles (see changelog
            # 2026-05-xx). gc.collect(1) collects gen-0 AND gen-1 — which reclaims
            # those buffers exactly like a full collect would — while skipping the
            # expensive gen-2 walk of the bot's long-lived CACHE state. Python's
            # automatic generational GC (still enabled, default thresholds — never
            # disabled in this codebase) continues to run real gen-2 sweeps on its
            # own schedule, so genuine long-lived reference cycles are still
            # collected; they just aren't force-walked on every single cycle.
            def _post_cycle_cleanup() -> int:
                _t0 = time.perf_counter()
                expired = CACHE.coc_clan_cache.clear_expired()
                _t1 = time.perf_counter()
                import gc as _gc
                _gc.collect(1)
                _t2 = time.perf_counter()
                # Return fragmented free arenas to the OS (Linux/glibc only).
                # Python's pymalloc never releases arenas until fully empty, so
                # after heavy CWL cycles (millions of small dict allocs/frees)
                # freed memory stays in arenas and RSS climbs indefinitely.
                # malloc_trim(0) forces glibc to return all free top-of-heap
                # memory immediately after gc.collect() clears the objects.
                # Silently ignored on non-Linux platforms (e.g. Windows dev env).
                try:
                    import ctypes as _ctypes
                    _ctypes.CDLL("libc.so.6").malloc_trim(0)
                except Exception:
                    pass
                _t3 = time.perf_counter()
                # Timing breakdown so future slowdowns can be pinpointed at a
                # glance instead of re-instrumenting; cheap (one log call/cycle).
                logging.info(
                    "[CYCLE-CLEANUP] clear_expired=%.3fs gc_collect=%.3fs "
                    "malloc_trim=%.3fs total=%.3fs (expired=%d)",
                    _t1 - _t0, _t2 - _t1, _t3 - _t2, _t3 - _t0, expired,
                )
                return expired
            await asyncio.to_thread(_post_cycle_cleanup)

            # --- On-demand memory profiling (PROD-safe) ---
            # If an admin started a trace via /admin Memory Profile, take the
            # snapshot now (end of cycle = peak working-set) and stop tracing.
            if QBcore.memtrace_pending:
                try:
                    import tracemalloc as _tm
                    if _tm.is_tracing():
                        from qapbot.QBdiscocmdshelper_admin_command import save_memtrace_snapshot
                        # Offload to thread: save_memtrace_snapshot runs gc.collect(),
                        # tracemalloc.take_snapshot(), and gc.get_objects() which are
                        # CPU-heavy (100-700ms on server-machine ARM CPUs). Running them on the
                        # event loop starves discord.py's heartbeat → disconnects.
                        await asyncio.to_thread(save_memtrace_snapshot, CACHE)
                        _tm.stop()
                        logging.info("[MEMTRACE] On-demand snapshot taken and tracing stopped")
                    QBcore.memtrace_pending = False
                except Exception as _mte:
                    logging.error(f"[MEMTRACE] Failed to take snapshot: {_mte}")
                    QBcore.memtrace_pending = False
        except RuntimeError as e:
            if "attached to a different loop" in str(e):
                logging.critical("Asyncio event loop mismatch detected. Exiting process to avoid undefined state.")
                import os as _os
                _os._exit(1)
            else:
                logging.error(f"Error in main loop: {e}")
                logging.info(f"Backing off for {backoff} seconds before retrying.")
                QBcore.in_main_cycle = False
                QBcore.cycle_idle_event.set()  # signal: cycle finished (error path)
                try:
                    await asyncio.wait_for(QBcore.shutdown_event.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, max_backoff)
                continue
        else:
            # main() completed normally — clear the flag before sleeping so
            # maintenance start can proceed without waiting for the sleep to end
            QBcore.in_main_cycle = False
            QBcore.cycle_idle_event.set()  # signal: cycle finished (normal path)

            # If /admin Maintenance Start was issued while this cycle was running,
            # close resources now that the cycle has safely finished.
            if QBcore.maintenance_pending:
                _maint_interaction = QBcore.maintenance_pending_interaction
                QBcore.maintenance_pending = False
                QBcore.maintenance_pending_interaction = None
                logging.info("[MAINTENANCE] Deferred shutdown: cycle finished — closing resources now.")
                try:
                    from QBdiscordcmds import do_maintenance_shutdown
                    await do_maintenance_shutdown()
                except Exception as _maint_ex:
                    logging.error(f"[MAINTENANCE] Error during deferred shutdown: {_maint_ex}")
                # Maintenance shutdown has already completed — Discord update is best-effort.
                # Only attempt if the stored interaction is less than 2 hours old; interaction
                # tokens expire at ~15 min but the guard prevents wasted API calls after very
                # long cycles and keeps the intent explicit.
                _elapsed = datetime.now().timestamp() - QBcore.maintenance_pending_interaction_time
                if _maint_interaction is not None and _elapsed < 7200:
                    try:
                        await _maint_interaction.edit_original_response(
                            content=(
                                "🔧 **Maintenance mode is now active.**\n\n"
                                "• All periodic update cycles are suspended\n"
                                "• CoC API client closed\n"
                                "• Database connection closed — safe to copy/edit `data/`\n"
                                "• All slash commands are blocked for regular users\n\n"
                                "Use **`/admin Maintenance End`** when done to restart the bot."
                            )
                        )
                    except Exception as _disc_ex:
                        logging.warning(f"[MAINTENANCE] Could not update Discord message (interaction may have expired): {_disc_ex}")
                # Cancel any pending Optimize DB — the DB is about to be closed.
                # Without this, optimize_db_pending_interaction would be a zombie
                # (no reply ever sent) and the task would hit a closed db_manager.
                if QBcore.optimize_db_pending:
                    _cancel_opt_interaction = QBcore.optimize_db_pending_interaction
                    QBcore.optimize_db_pending = False
                    QBcore.optimize_db_pending_interaction = None
                    if _cancel_opt_interaction is not None:
                        try:
                            await _cancel_opt_interaction.edit_original_response(
                                content="❌ Nightly maintenance cancelled — maintenance mode is being activated."
                            )
                        except Exception as _cancel_disc_ex:
                            logging.debug(f"[DB-OPTIMIZE] Could not send cancellation message: {_cancel_disc_ex}")
                # Clear any queued force-cycle — irrelevant once maintenance suspends all cycles.
                QBcore.force_cycle_pending = False
                continue  # Skip the 300s sleep — maintenance_mode loop handles idle

            # If /admin Optimize DB was queued while this cycle was running,
            # run nightly_db_maintenance() now (identical to the nightly scheduler)
            # and send the result back via the stored interaction.
            # IMPORTANT: Checked BEFORE force_cycle_pending so the task is always
            # launched even when both flags are set simultaneously.
            _optimize_launched = False
            if QBcore.optimize_db_pending and CACHE.db_manager is not None:
                _opt_interaction = QBcore.optimize_db_pending_interaction
                QBcore.optimize_db_pending = False
                QBcore.optimize_db_pending_interaction = None
                _opt_db_mgr = CACHE.db_manager
                _optimize_launched = True
                logging.info("[DB-OPTIMIZE] Deferred run: cycle finished — starting nightly maintenance now.")

                async def _deferred_optimize_task() -> None:
                    QBcore.db_maintenance_idle_event.clear()
                    try:
                        _run_migration_opt = await is_monthly_migration_due()
                        _result = await run_nightly_maintenance_routine(_opt_db_mgr, _run_migration_opt)
                        if _opt_interaction is not None:
                            try:
                                await _opt_interaction.edit_original_response(
                                    content=f"✅ **Nightly maintenance complete.**\n```\n{_result}\n```"
                                )
                            except Exception as _disc_ex:
                                # Interaction token expires after 15 min — VACUUM on HDD can exceed this
                                logging.warning(f"[DB-OPTIMIZE] Could not update Discord message (token likely expired after long VACUUM): {_disc_ex}")
                                logging.info(f"[DB-OPTIMIZE] Result that could not be sent: {_result}")
                    except Exception as _ex:
                        logging.error(f"[DB-OPTIMIZE] Error: {_ex}")
                        if _opt_interaction is not None:
                            try:
                                await _opt_interaction.edit_original_response(
                                    content=f"❌ Nightly maintenance failed: {_ex}"
                                )
                            except Exception:
                                pass
                    finally:
                        QBcore.db_maintenance_idle_event.set()

                asyncio.create_task(_deferred_optimize_task())

            # If /admin Start Update Cycle was queued while this cycle was running,
            # skip the sleep phase and go straight into the next cycle.
            # Checked AFTER optimize_db_pending: if both flags were set, the optimize
            # task is already launched above; we await db_maintenance_idle_event inline
            # to avoid starting a write-heavy cycle while VACUUM holds an exclusive lock.
            if QBcore.force_cycle_pending:
                QBcore.force_cycle_pending = False
                if _optimize_launched and not QBcore.db_maintenance_idle_event.is_set():
                    logging.info("[CYCLE] Waiting for DB optimization to complete before queued cycle...")
                    await QBcore.db_maintenance_idle_event.wait()
                    logging.info("[CYCLE] DB optimization finished — starting queued cycle now.")
                logging.info("[CYCLE] Skipping sleep phase — next cycle queued by admin command.")
                continue

            if not QBcore.shutdown_event.is_set():
                # Log RSS once per cycle so memory trends are visible in plain logs
                # without needing a /memprofile.  /proc/self/status is a single kernel
                # read — negligible overhead even on the server-machine Celeron.
                try:
                    _rss_mb = 0.0
                    with open("/proc/self/status") as _pf:
                        for _pl in _pf:
                            if _pl.startswith("VmRSS:"):
                                _rss_mb = int(_pl.split()[1]) / 1024
                                break
                    logging.info(f"[CYCLE-END] [MEMORY CONSUMPTION] RSS={_rss_mb:.1f} MB")
                except Exception:
                    pass

                # --- Nightly DB maintenance ---
                # Run once per 24 h at exactly 03:00 UTC (hour == 3, minute == 0).
                # Chosen at 03:00 UTC because CWL wars end before ~01:00 UTC, so the
                # heavy burst of war_attacks inserts will have settled and the bot is
                # at its most idle.  WAL checkpoint + REINDEX + ANALYZE typically complete
                # in 30-60s.  VACUUM duration depends on freelist size and disk speed: on
                # an SSD a 1 GB freelist takes ~30s; on a server-machine HDD the same can take 30-60+
                # minutes.  The task runs as asyncio.create_task() so the event loop (and
                # Discord heartbeat) are never blocked — only DB commands are blocked via
                # db_maintenance_mode=True while the VACUUM holds an EXCLUSIVE lock.
                #
                # IMPORTANT (2026-07-18): deliberately placed HERE — after
                # _post_cycle_cleanup() and memtrace have already finished, and after
                # the maintenance_pending/optimize_db_pending/force_cycle_pending checks
                # above (which `continue` early and skip this section entirely) — so
                # the archive-move/DB-maintenance task never overlaps with the same
                # cycle's own cache cleanup or with a concurrent /admin-triggered
                # maintenance/shutdown/immediate-next-cycle request. Previously this
                # was scheduled immediately after "[CYCLE-END] Update cycle completed",
                # racing [ARCHIVE-MOVE] against [COC-CACHE-CLEANUP]/gc.collect() on
                # separate threads for the server-machine's I/O and CPU. Now it only starts once
                # the cycle has truly finished and is about to enter its sleep wait.
                #
                # Persistence: last_db_maintenance is loaded from bot_metadata on the
                # first cycle so a bot restart within the same 03:00 window does not
                # double-fire maintenance.
                _now_utc = datetime.now(timezone.utc)
                if CACHE.last_db_maintenance is None and CACHE.db_manager is not None:
                    # First cycle after startup — hydrate from DB so a restart within
                    # the 03:00 window does not re-trigger maintenance unnecessarily.
                    _stored = await CACHE.db_manager.get_bot_metadata("last_db_maintenance")
                    if _stored:
                        try:
                            CACHE.last_db_maintenance = datetime.fromisoformat(_stored)
                        except ValueError:
                            pass  # Malformed value — treat as never run
                _maint_due = (
                    _now_utc.hour == 3
                    and (
                        CACHE.last_db_maintenance is None
                        or (_now_utc - CACHE.last_db_maintenance).total_seconds() > 20 * 3600
                    )
                )
                # --- Monthly hot->history DB migration ---
                # Runs once a month (day == 1, same 03:00 UTC window as nightly
                # maintenance) — NOT nightly, since the hot DB's retention window
                # (current + previous calendar month) only advances once a month.
                # Guarded by calendar month (not just "ran today") so a bot restart
                # later on day 1, or a missed day-1 window, doesn't skip/double-fire.
                # is_monthly_migration_due() hydrates + claims it atomically (shared
                # with /admin Execute Nightly Maintenance so neither double-runs it).
                _migration_due = await is_monthly_migration_due()
                if _maint_due and CACHE.db_manager is not None:
                    # Pre-set timestamp so a later cycle in the same 03:xx window does not
                    # re-trigger while the background task is still running.
                    CACHE.last_db_maintenance = _now_utc
                    _db_mgr = CACHE.db_manager  # capture non-None ref for the closure
                    _run_migration = _migration_due

                    async def _nightly_maintenance_task() -> None:
                        # Block the next update cycle from starting.
                        # Discord commands remain fully open during Step 0 (file move).
                        # db_maintenance_mode=True is set inside nightly_db_maintenance().
                        QBcore.db_maintenance_idle_event.clear()
                        try:
                            await run_nightly_maintenance_routine(_db_mgr, _run_migration)
                        finally:
                            QBcore.db_maintenance_idle_event.set()  # signal: maintenance done

                    asyncio.create_task(_nightly_maintenance_task())
                elif _migration_due and CACHE.db_manager is not None:
                    # Migration is due but the (once-a-day) maintenance window already
                    # fired earlier today — very unlikely (both share the hour==3 gate)
                    # but run migration standalone as a safety net so day-1 migration
                    # is never silently skipped for a whole month.
                    _db_mgr2 = CACHE.db_manager

                    async def _standalone_migration_task() -> None:
                        QBcore.db_maintenance_idle_event.clear()
                        try:
                            await _db_mgr2.monthly_history_migration()
                        finally:
                            QBcore.db_maintenance_idle_event.set()

                    asyncio.create_task(_standalone_migration_task())

                # Subtract cycle duration from the configured interval so the
                # wall-clock period between cycle starts stays constant.
                # Floor at 30s so a very long cycle never completely eliminates the
                # idle window Discord heartbeats and interactions need.
                _effective_sleep = max(30, sleep_interval - int(_cycle_elapsed))
                logging.info(f"Sleeping for {_effective_sleep} seconds. (cycle took {_cycle_elapsed:.1f}s) Press CTRL+C now to terminate.")
                _t_shutdown = asyncio.ensure_future(QBcore.shutdown_event.wait())
                _t_force = asyncio.ensure_future(QBcore.force_cycle_event.wait())
                try:
                    _done, _pending = await asyncio.wait(
                        {_t_shutdown, _t_force},
                        timeout=_effective_sleep,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for _t in _pending:
                        _t.cancel()
                    if _t_force in _done:
                        QBcore.force_cycle_event.clear()
                        logging.info("[CYCLE] Sleep phase interrupted by admin command — starting next cycle immediately.")
                except Exception:
                    pass

            # Gate: wait for nightly DB maintenance to finish before starting the
            # next update cycle.  Normally already set (maintenance takes ~30s within
            # the 5-min sleep); only blocks on very slow disks or if sleep was skipped.
            if not QBcore.db_maintenance_idle_event.is_set():
                logging.info("[CYCLE] Waiting for nightly DB maintenance to complete before starting next cycle...")
                await QBcore.db_maintenance_idle_event.wait()
                logging.info("[CYCLE] DB maintenance finished — resuming update cycle.")
            continue
        QBcore.in_main_cycle = False
        QBcore.cycle_idle_event.set()  # signal: cycle finished (fallthrough)

    # Graceful exit once event set
    await async_cleanup()
    # Ensure bot connection closes so bot.run returns (especially on Windows)
    try:
        if not QBcore.bot.is_closed():
            await asyncio.wait_for(QBcore.bot.close(), timeout=10.0)
    except (asyncio.TimeoutError, Exception):
        pass
    # For maintenance restart use os._exit() — sys.exit() inside an asyncio task raises
    # SystemExit which only terminates the task, leaving the event loop running and
    # bot.run() hanging forever.  os._exit() bypasses asyncio entirely.
    if QBcore.exit_code != 0:
        import os as _os
        logging.info(f"Forcing process exit with code {QBcore.exit_code} (maintenance restart).")
        _os._exit(QBcore.exit_code)

async def _setup_hook():
    """
    Discord command registration for QapBot with simplified logic:
    - If GLOBAL_GUILD_ID = 0: Register commands globally 
    - If GLOBAL_GUILD_ID > 0: Register commands for that specific guild only
    """
    logging.info("[SETUP_HOOK] Starting setup_hook...")

    # On Windows, asyncio.SelectorEventLoop never calls signal.set_wakeup_fd()
    # (that is only done by loop.add_signal_handler(), which raises
    # NotImplementedError on Windows).  Without a wakeup fd, select.select()
    # ignores pending signals until the next real I/O event arrives — causing a
    # multi-second gap between pressing CTRL+C and the Python signal handler
    # being invoked.  Pointing set_wakeup_fd at the loop's own self-pipe socket
    # fixes this: Python's C-level signal handler writes a byte to _csock,
    # which makes select() return immediately on the next spin, delivering the
    # signal without delay.
    if sys.platform == 'win32':
        loop = asyncio.get_running_loop()
        if hasattr(loop, '_csock'):
            try:
                _loop_any: Any = loop
                signal.set_wakeup_fd(_loop_any._csock.fileno())
                logging.debug("[SETUP_HOOK] Signal wakeup fd configured — CTRL+C will now respond immediately.")
            except (OSError, ValueError) as _e:
                logging.debug(f"[SETUP_HOOK] Could not configure signal wakeup fd: {_e}")
    import QBdiscordcmds  # type: ignore[import-untyped]
    logging.info("[SETUP_HOOK] QBdiscordcmds imported")
    
    # Type: list[Any] to avoid complex Command generic type issues
    COMMANDS: list[Any] = [
        QBdiscordcmds.subscribe,  # type: ignore[misc]
        QBdiscordcmds.unsubscribe,  # type: ignore[misc]
        QBdiscordcmds.leaderboard,  # type: ignore[misc]
        QBdiscordcmds.help,  # type: ignore[misc]
        QBdiscordcmds.subscriptions,  # type: ignore[misc]
        QBdiscordcmds.status,  # type: ignore[misc]
        QBdiscordcmds.ping,  # type: ignore[misc]
        QBdiscordcmds.list,  # type: ignore[misc]
        QBdiscordcmds.admin,  # type: ignore[misc]
        QBdiscordcmds.whois_slash,  # type: ignore[misc]
    ]

    # Command groups
    COMMAND_GROUPS = [
        QBdiscordcmds.clan_group,
        QBdiscordcmds.analyse_group,
    ]
    
    # Context menu commands
    CONTEXT_MENUS = [
        QBdiscordcmds.whois,
        QBdiscordcmds.whois_message
    ]
    
    logging.info(f"[SETUP_HOOK] {len(COMMANDS)} commands, {len(COMMAND_GROUPS)} command groups, and {len(CONTEXT_MENUS)} context menus prepared")
    
    if GLOBAL_GUILD_ID > 0:
        # Guild mode: register commands for specific guild
        logging.info(f"[SETUP_HOOK] Registering commands for guild ID {GLOBAL_GUILD_ID} (guild mode)")
        for cmd in COMMANDS:
            QBcore.bot.tree.add_command(cmd, guild=discord.Object(id=GLOBAL_GUILD_ID))
        for group in COMMAND_GROUPS:
            QBcore.bot.tree.add_command(group, guild=discord.Object(id=GLOBAL_GUILD_ID))
        for ctx in CONTEXT_MENUS:
            QBcore.bot.tree.add_command(ctx, guild=discord.Object(id=GLOBAL_GUILD_ID))
        logging.info(f"[SETUP_HOOK] Commands added to tree, now syncing...")
        
        async def _sync_guild_commands():
            return await QBcore.bot.tree.sync(guild=discord.Object(id=GLOBAL_GUILD_ID))
        
        await discord_retry(
            _sync_guild_commands,
            "guild_command_sync"
        )
        logging.info(f"[SETUP_HOOK] Successfully registered {len(COMMANDS)} commands, {len(COMMAND_GROUPS)} command groups, and {len(CONTEXT_MENUS)} context menus for guild {GLOBAL_GUILD_ID}")
    else:
        # Global mode: register commands globally
        logging.info("[SETUP_HOOK] Registering commands globally (global mode)")
        for cmd in COMMANDS:
            QBcore.bot.tree.add_command(cmd, guild=None)
        for group in COMMAND_GROUPS:
            QBcore.bot.tree.add_command(group, guild=None)
        for ctx in CONTEXT_MENUS:
            QBcore.bot.tree.add_command(ctx, guild=None)
        logging.info(f"[SETUP_HOOK] Commands added to tree, now syncing...")
        
        async def _sync_global_commands():
            return await QBcore.bot.tree.sync(guild=None)
        
        await discord_retry(
            _sync_global_commands,
            "global_command_sync"
        )
        logging.info(f"[SETUP_HOOK] Successfully registered {len(COMMANDS)} commands, {len(COMMAND_GROUPS)} command groups, and {len(CONTEXT_MENUS)} context menus globally")
    
    logging.info("[SETUP_HOOK] Setup hook completed successfully")

async def _clear_global_commands_after_ready():
    """
    Clear global commands when in DEV mode (GLOBAL_GUILD_ID > 0).
    This ensures that only guild-specific commands are available when running in DEV mode.
    """
    if GLOBAL_GUILD_ID == 0:
        return  # Skip if in global mode
        
    logging.info("Clearing global commands from DEV app (cleanup from previous PROD mode testing)...")
    
    try:
        # Clear global commands
        logging.info("[CLEAR-CMDS] Calling tree.clear_commands(guild=None)...")
        QBcore.bot.tree.clear_commands(guild=None)
        logging.info("[CLEAR-CMDS] Calling tree.sync(guild=None) via discord_retry (max 3 retries)...")
        await discord_retry(
            lambda: QBcore.bot.tree.sync(guild=None),
            "clear_global_commands"
        )
        logging.info("✅ Global commands cleared from DEV app successfully")
    except Exception as e:
        logging.error(f"❌ Failed to clear global commands from DEV app: {e}")


async def _wait_for_discord_connection(token: str, max_wait_hours: int = 4) -> None:
    """
    Wait until Discord's HTTP API responds before starting the bot.

    Prevents the bot from crashing immediately on restart during a Discord outage.
    Makes a lightweight GET to /api/v10/gateway (no authentication needed) and
    returns as soon as Discord responds with a non-5xx status. Uses exponential
    backoff capped at 10 minutes per attempt.

    Args:
        token: Discord bot token (unused for the health-check request itself,
               kept in signature so callers can pass it for consistency).
        max_wait_hours: Stop retrying and proceed after this many hours regardless
                        (lets the process manager eventually observe the failure).
    """
    delays = [10, 30, 60, 120, 300, 600]   # 10 s → 30 s → 1 min → 2 min → 5 min → 10 min
    attempt = 0
    start = time.monotonic()

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(
                    "https://discord.com/api/v10/gateway",
                    timeout=aiohttp.ClientTimeout(total=10),
                    headers={"User-Agent": "QapBot-HealthCheck/1.0"},
                ) as resp:
                    if resp.status < 500:
                        if attempt > 0:
                            elapsed = time.monotonic() - start
                            logging.info(
                                f"[DISCORD-HEALTH] Discord reachable (HTTP {resp.status}) "
                                f"after {elapsed:.0f}s — starting bot."
                            )
                        return
                    status_msg = f"HTTP {resp.status}"
            except Exception as exc:
                status_msg = str(exc)

            delay = delays[min(attempt, len(delays) - 1)]
            elapsed = time.monotonic() - start

            if elapsed + delay > max_wait_hours * 3600:
                logging.error(
                    f"[DISCORD-HEALTH] Discord still unreachable after {elapsed:.0f}s "
                    f"({status_msg}). Proceeding anyway — bot.run() may fail and be "
                    "restarted by the process manager."
                )
                return

            logging.warning(
                f"[DISCORD-HEALTH] Discord not reachable ({status_msg}), "
                f"retrying in {delay}s (attempt {attempt + 1})..."
            )
            await asyncio.sleep(delay)
            attempt += 1


async def _clear_dev_guild_commands_before_prod() -> None:
    """
    Clear DEV guild commands using DEV token before starting PROD bot.
    
    This function creates a temporary bot connection with the DEV token to clear
    guild-specific commands from the DEV guild before the PROD bot starts.
    
    Only runs when:
    - GLOBAL_GUILD_ID == 0 (PROD mode)
    - DISCORD_TOKEN_DEV is available
    - DISCORD_GUILD_ID_FOR_CLEANUP is set (target guild ID)
    
    Behavior:
        - Creates temporary bot instance with minimal intents
        - Clears commands for specified guild
        - Closes temporary bot connection
        - Returns silently on success or failure (non-blocking)
    
    Security:
        - No tokens are logged
        - Errors are logged but don't prevent PROD startup
        - 30-second timeout prevents hanging
    """
    if GLOBAL_GUILD_ID > 0:
        return  # Skip if in DEV mode
    
    dev_token = os.getenv("DISCORD_TOKEN_DEV", "")
    dev_guild_id_str = os.getenv("DISCORD_GUILD_ID_FOR_CLEANUP", "")
    
    if not dev_token:
        logging.debug("Skipping DEV guild cleanup: DISCORD_TOKEN_DEV not set")
        return
    
    if not dev_guild_id_str:
        logging.debug("Skipping DEV guild cleanup: DISCORD_GUILD_ID_FOR_CLEANUP not set")
        return
    
    try:
        dev_guild_id = int(dev_guild_id_str)
    except ValueError:
        logging.warning(f"Invalid DISCORD_GUILD_ID_FOR_CLEANUP: {dev_guild_id_str}")
        return
    
    logging.info(f"Clearing guild commands from DEV app for test server {dev_guild_id} (cleanup for PROD mode)...")
    
    temp_bot = None
    bot_task: Optional[asyncio.Task[None]] = None
    cleanup_complete = asyncio.Event()
    cleanup_success = False
    
    try:
        # Create temporary bot instance with minimal intents
        # Use commands.Bot instead of discord.Client so we have the tree attribute
        intents = discord.Intents.default()
        intents.guilds = True  # Need guild intent to access guild objects
        intents.message_content = True  # Enable privileged message content intent for command/message access
        temp_bot = commands.Bot(command_prefix="!", intents=intents)
        
        @temp_bot.event
        async def on_ready():  # type: ignore[misc]  # Used via decorator registration
            nonlocal cleanup_success
            try:
                logging.debug(f"Temporary DEV bot connected as {temp_bot.user}")
                
                # Clear commands for the DEV guild
                guild = discord.Object(id=dev_guild_id)
                temp_bot.tree.clear_commands(guild=guild)
                await temp_bot.tree.sync(guild=guild)
                
                cleanup_success = True
                logging.info(f"✅ Guild commands cleared from DEV app for test server {dev_guild_id} successfully")
                
            except discord.Forbidden:
                logging.warning(f"⚠️ No permission to clear commands in guild {dev_guild_id} (bot may not be in guild)")
            except Exception as e:
                logging.warning(f"⚠️ Failed to clear DEV guild commands: {e}")
            finally:
                # Signal completion - close will happen in outer finally block
                cleanup_complete.set()
        
        # Start temporary bot with timeout
        bot_task = asyncio.create_task(temp_bot.start(dev_token))
        
        # Wait for cleanup to complete or timeout
        try:
            await asyncio.wait_for(cleanup_complete.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            logging.warning("⚠️ DEV guild cleanup timed out after 30 seconds")
        
        if cleanup_success:
            logging.info("DEV app guild command cleanup completed successfully")
        else:
            logging.warning("DEV app guild command cleanup completed with warnings (see above)")
            
    except Exception as e:
        logging.warning(f"⚠️ Error during DEV guild cleanup: {e}")
    finally:
        # Ensure temporary bot is properly disconnected and closed
        if temp_bot and bot_task is not None:
            try:
                # Close the bot connection (this disconnects from gateway and closes HTTP)
                if not temp_bot.is_closed():
                    logging.debug("Closing temporary DEV bot...")
                    await temp_bot.close()
                    logging.debug("Temporary DEV bot close() called")
                
                # Cancel the bot task if still running
                if not bot_task.done():
                    bot_task.cancel()
                    try:
                        await bot_task
                    except asyncio.CancelledError:
                        logging.debug("Bot task cancelled successfully")
                    except Exception as e:
                        logging.debug(f"Error cancelling bot task: {e}")
                else:
                    # Task already finished — retrieve its exception so Python does not
                    # emit "Task exception was never retrieved" (happens when Discord
                    # returns 503 during login before the 30-second cleanup timeout).
                    try:
                        exc = bot_task.exception()
                        if exc is not None:
                            logging.debug(
                                f"DEV cleanup bot task finished with exception "
                                f"(Discord unavailable?): {type(exc).__name__}: {exc}"
                            )
                    except asyncio.CancelledError:
                        pass
                
                # Give Discord gateway time to process the disconnect
                await asyncio.sleep(1.0)
                logging.debug("Temporary DEV bot cleanup completed")
                
            except Exception as e:
                logging.debug(f"Error during temp bot cleanup: {e}")

logging.debug("About to assign setup_hook...")
QBcore.bot.setup_hook = _setup_hook
logging.debug("Setup_hook assigned successfully")

logging.debug("About to register on_ready event...")
@QBcore.bot.event
async def on_ready() -> None:
    """
    Enhanced Discord bot ready event handler with improved safety and initialization.

    Features:
    - Safe handling of multiple on_ready calls
    - Comprehensive error handling for each initialization step
    - Proper sequencing with timeout protection
    - Better monitoring and logging
    - DEV/PROD mode logging

    Behavior:
        - Creates shutdown event for graceful termination
        - Loads all cache data from persistent storage via CACHE.load_all()
        - Authenticates with Clash of Clans API (using DEV or PROD credentials)
        - Records bot start time for uptime tracking
        - Starts the periodic_main() task for continuous operation
        - Handles multiple invocations safely
    """
    logging.debug("on_ready: After first logging...")
    # Prevent multiple initializations from rapid reconnections
    if hasattr(QBcore.bot, "initialization_in_progress") and QBcore.bot.initialization_in_progress:
        logging.info("Bot initialization already in progress, skipping duplicate on_ready")
        return
    
    logging.debug("on_ready: Checking fully_initialized...")
    if hasattr(QBcore.bot, "fully_initialized") and QBcore.bot.fully_initialized:
        logging.info("Bot already fully initialized, handling reconnection")
        # Just log the reconnection, don't reinitialize everything
        logging.info(f"Bot reconnected as {QBcore.bot.user}")
        return
    
    logging.debug("on_ready: Setting initialization_in_progress...")
    QBcore.bot.initialization_in_progress = True
    logging.debug("on_ready: Getting CONFIG.is_dev_mode...")
    mode_str = "DEV" if CONFIG.is_dev_mode else "PROD"
    logging.debug(f"on_ready: mode_str={mode_str}")
    logging.debug("on_ready: Getting CONFIG.discord_guild_id...")
    guild_info = f"guild {CONFIG.discord_guild_id}" if CONFIG.is_dev_mode else "global"
    logging.debug(f"on_ready: guild_info={guild_info}")
    logging.debug("on_ready: About to log initialization start...")
    logging.info(f"=== Starting Discord bot initialization ({mode_str} mode - {guild_info}) ===")
    
    try:
        # Step 1: Initialize shutdown coordination
        if QBcore.shutdown_event is None:  # type: ignore[misc]
            QBcore.shutdown_event = asyncio.Event()
            logging.info("✅ Created shutdown event")
        
        # Step 2: Authenticate with CoC API (initializes database first)
        if CONFIG.no_coc_api:
            logging.info("🔐 [NO_COC_API] Skipping CoC API authentication, initializing database only...")
            try:
                await asyncio.wait_for(startup_login(), timeout=60.0)
                logging.info("✅ Database initialized (NO_COC_API mode)")
            except Exception as e:
                logging.error(f"❌ Error during startup: {e}")
                return
        else:
            logging.info("🔐 Authenticating with Clash of Clans API...")
            try:
                await asyncio.wait_for(startup_login(), timeout=60.0)
                logging.info("✅ CoC API authentication completed")
            except asyncio.TimeoutError:
                logging.error("❌ Startup login timed out after 60 seconds (includes DB init + CoC API authentication)")
                logging.error("🛑 Cannot continue without CoC API access")
                return
            except Exception as e:
                logging.error(f"❌ Error during CoC API login: {e}")
                logging.error("🛑 Cannot continue without CoC API access")
                return
        
        # Step 3: Load cache data with timeout protection (database now initialized)
        logging.info("📂 Loading cache data...")
        try:
            await asyncio.wait_for(
                CACHE.load_all(), 
                timeout=300.0
            )
            QBcore.cache_loaded = True
            logging.info("✅ Cache data loaded successfully")
            
            # Log cache statistics for monitoring
            stats = {
                'clans': len(CACHE.clan_name_cache),
                'families': len(CACHE.clan_families),
                'subscriptions': len(CACHE.subscriptions),
                'users': len(CACHE.user_accounts),
                'messages': len(CACHE.leaderboard_messages)
            }
            logging.info(f"📊 Cache stats: {stats}")

            # Load CWL war-tag recovery queue (if data/missing_cwl_war_tags.txt exists)
            try:
                from QBhelperfunctions import load_cwl_recovery_file as _load_cwl_recovery
                _cwl_n = _load_cwl_recovery()
                if _cwl_n > 0:
                    logging.info(f"🔄 CWL recovery: {_cwl_n:,} war_tags queued for background recovery")
            except Exception as _cwl_ex:
                logging.warning(f"[CWL-RECOVERY] Could not load recovery file: {_cwl_ex}")

        except asyncio.TimeoutError:
            failed_op = getattr(CACHE, '_current_load_operation', 'unknown')
            logging.critical(f"💥 Cache loading timed out after 300 seconds while executing '{failed_op}' — bot cannot start")
            logging.critical("🛑 Terminating bot due to failed cache load")
            await async_cleanup()
            await QBcore.bot.close()
            return
        except Exception as e:
            failed_op = getattr(CACHE, '_current_load_operation', 'unknown')
            logging.critical(f"💥 Cache loading failed during '{failed_op}': {e} — bot cannot start")
            logging.critical("🛑 Terminating bot due to failed cache load")
            await async_cleanup()
            await QBcore.bot.close()
            return
        
        # Step 4: Set bot start time for uptime tracking
        QBcore.bot.start_time = datetime.now()
        QBcore.bot_start_time = time.time()  # Unix float for memprofile uptime reporting
        logging.info("✅ Set bot start time for uptime tracking")
        
        # Step 5: Start periodic task
        logging.info("🔄 Starting periodic main task...")
        try:
            QBcore.bot.loop.create_task(periodic_main())
            QBcore.bot.periodic_task_started = True
            logging.info("✅ Periodic main task started successfully")
        except Exception as e:
            logging.error(f"❌ Failed to start periodic task: {e}")
            return
        
        # Step 6: Clear global commands in DEV mode (guild commands already cleared before PROD startup)
        logging.info("[INIT-STEP-6] Starting post-startup command cleanup...")
        if GLOBAL_GUILD_ID > 0:
            # DEV mode: clear global commands to ensure only guild commands are active
            await _clear_global_commands_after_ready()
        # PROD mode: guild commands already cleared by _clear_dev_guild_commands_before_prod() before startup
        logging.info("[INIT-STEP-6] Done")
        
        # Step 7: Clean up stale UI messages from before restart
        logging.info("[INIT-STEP-7] Starting stale UI message cleanup...")
        try:
            await asyncio.wait_for(cleanup_stale_ui_messages(), timeout=10.0)
        except asyncio.TimeoutError:
            logging.warning("⚠️ Cleanup of stale UI messages timed out after 10 seconds (continuing)")
        except Exception as e:
            logging.error(f"❌ Failed to clean up stale UI messages: {e}")
        logging.info("[INIT-STEP-7] Done")
        
        # Step 8: Repost playerregistration registration messages on startup
        logging.info("[INIT-STEP-8] Starting player registration message repost...")
        try:
            await asyncio.wait_for(repost_playerregistration_messages(), timeout=30.0)
            logging.info("✅ Player registration messages reposted successfully")
        except asyncio.TimeoutError:
            logging.warning("⚠️ Reposting playerregistration messages timed out after 30 seconds (continuing)")
        except Exception as e:
            logging.error(f"❌ Failed to repost playerregistration messages: {e}")
        logging.info("[INIT-STEP-8] Done")
        
        # Step 9: Finalize initialization
        QBcore.bot.fully_initialized = True
        QBcore.bot.initialization_in_progress = False

        logging.info(f"🎉 Bot fully initialized and logged in as {QBcore.bot.user} ({mode_str} mode)")
        logging.info(f"🏠 Connected to {len(QBcore.bot.guilds)} guild(s)")
        logging.info("=== Bot initialization completed successfully ===")
        
    except Exception as e:
        logging.error(f"💥 Critical error during bot initialization: {e}")
        QBcore.bot.initialization_in_progress = False
        # Don't set fully_initialized on error so initialization can be retried
        raise


@QBcore.bot.event
async def on_disconnect() -> None:
    """Log WebSocket disconnects — discord.py will reconnect automatically."""
    if QBcore.shutdown_initiated or QBcore.maintenance_mode:
        # Intentional disconnect caused by bot.close() during graceful shutdown or maintenance — not an error.
        logging.info("[DISCORD-WS] Gateway disconnected (intentional shutdown/maintenance).")
        return
    logging.warning("[DISCORD-WS] Gateway disconnected — awaiting automatic reconnect.")


@QBcore.bot.event
async def on_resumed() -> None:
    """Log WebSocket session resume so reconnect recovery is visible in logs."""
    logging.info("[DISCORD-WS] Gateway session resumed — connection restored.")


@QBcore.bot.event
async def on_member_join(member: discord.Member) -> None:
    """
    Event handler triggered when a new user joins a Discord server.
    
    Automatically assigns the newbie role if:
    1. Role system is enabled for the guild
    2. Newbie role is configured
    
    Also sends a welcome message to the system channel (where Discord shows join notifications) with:
    - How to register player accounts
    - Link to the main clan
    
    Args:
        member: Discord member who joined
        
    Behavior:
        - Logs the join event
        - Calls assign_newbie_role() helper function
        - Calls sync_roles_for_user() immediately for returning members who already have registered
          accounts, so CoC/clan roles are restored at join time instead of waiting up to 5 min
        - Sends welcome message to guild's system channel (same place as Discord's join notification)
        - Uses member mention (@user) for personalized greeting
        - Handles errors gracefully without blocking
    """
    from qapbot.QBdiscocmdshelper import assign_newbie_role
    from qapbot.i18n import t  # type: ignore[attr-defined]
    
    logging.info(f"User {member.display_name} (id={member.id}) joined guild {member.guild.name} (id={member.guild.id})")
    
    # Assign newbie role if configured
    try:
        await assign_newbie_role(member.guild, member.id)
    except Exception as e:
        logging.error(f"Failed to assign newbie role to user {member.id} in guild {member.guild.id}: {e}")

    # Sync CoC/clan roles immediately for returning members who already have registered accounts
    # Without this, roles are only restored on the next 5-minute periodic cycle.
    try:
        guild_id_str_early = str(member.guild.id)
        cfg_early = CACHE.server_config.get(guild_id_str_early, {})
        if cfg_early.get("coc_role_enabled") or cfg_early.get("clan_role_enabled"):
            if str(member.id) in CACHE.user_accounts:
                from qapbot.guild_role_manager import sync_roles_for_user
                await sync_roles_for_user(member.guild, guild_id_str_early, member.id, member=member)
                logging.debug(f"[ON-JOIN] Synced CoC/clan roles for returning member {member.id} in guild {member.guild.id}")
    except Exception as e:
        logging.warning(f"[ON-JOIN] Failed to sync roles for member {member.id} in guild {member.guild.id}: {e}")
    
    # Send welcome channel message
    try:
        guild_id_str = str(member.guild.id)
        guild_id_int = member.guild.id
        
        # Check if welcome message is independently enabled
        config = CACHE.server_config.get(guild_id_str, {})
        if not config.get("welcome_message_enabled", False):
            logging.debug(f"Welcome message not enabled for guild {member.guild.id}, skipping")
            return
        
        # Get the system channel where Discord shows join notifications
        target_channel = member.guild.system_channel
        
        # If no system channel configured, skip message
        if not target_channel:
            logging.warning(f"No system channel found for guild {member.guild.id}, skipping welcome message")
            return
        
        # Get registration channel ID to mention in the welcome message
        registration_channel_id = config.get("registration_channel_id")
        
        # Build welcome message with an explicit display name.
        # Discord clients can briefly show mentions as "unknown-user" until they resolve the user;
        # including the display name makes the greeting correct immediately while still pinging.
        greeting = t(
            'welcome_message.greeting',
            guild_id=guild_id_int,
            user_mention=member.mention,
            server_name=member.guild.name,
            member_count=member.guild.member_count,
        )
        
        # Get registration channel info (only if registration message is active)
        registration_enabled = config.get("registration_message_enabled", False)
        register_info = ""
        if registration_enabled:
            if registration_channel_id:
                try:
                    reg_channel = member.guild.get_channel(int(registration_channel_id))
                    if reg_channel:
                        channel_mention = reg_channel.mention
                        register_info = t('welcome_message.register_info', guild_id=guild_id_int,
                                        channel_mention=channel_mention)
                    else:
                        register_info = t('welcome_message.no_channel_configured', guild_id=guild_id_int)
                except (ValueError, TypeError):
                    register_info = t('welcome_message.no_channel_configured', guild_id=guild_id_int)
            else:
                register_info = t('welcome_message.no_channel_configured', guild_id=guild_id_int)

        # Build clan/apply info based on welcome message mode
        welcome_mode = config.get("welcome_message_mode", "clan_link")
        clan_info = ""

        if welcome_mode == "apply_channel":
            # Point user to apply/ticket channel
            apply_channel_id = config.get("welcome_apply_channel_id")
            if apply_channel_id:
                try:
                    apply_channel = member.guild.get_channel(int(apply_channel_id))
                    if apply_channel:
                        _member_clans = config.get("member_clans", [])
                        _family_clans = [
                            clan
                            for fam in config.get("member_families", [])
                            for clan in CACHE.clan_families.get(fam, {}).get("clans", [])
                        ]
                        _clan_count = len(_member_clans) + len(_family_clans)
                        _apply_key = 'welcome_message.apply_channel_info_plural' if _clan_count > 1 else 'welcome_message.apply_channel_info'
                        clan_info = t(_apply_key, guild_id=guild_id_int,
                                     channel_mention=apply_channel.mention)
                except (ValueError, TypeError):
                    pass
            if not clan_info:
                clan_info = t('welcome_message.no_clan_configured', guild_id=guild_id_int)
        else:
            # Default: clan link mode — one link per selected clan (individual selections
            # plus every clan belonging to a selected family). Zero selections is allowed;
            # the clan-link line is simply omitted from the welcome message in that case.
            welcome_family_tags = config.get("welcome_family_tags", [])
            welcome_clan_tags = config.get("welcome_clan_tags", [])

            resolved_clan_tags: List[str] = []
            _seen_tags: set[str] = set()
            for family_id in welcome_family_tags:
                for clan_tag in CACHE.clan_families.get(family_id, {}).get("clans", []):
                    if clan_tag not in _seen_tags:
                        resolved_clan_tags.append(clan_tag)
                        _seen_tags.add(clan_tag)
            for clan_tag in welcome_clan_tags:
                if clan_tag not in _seen_tags:
                    resolved_clan_tags.append(clan_tag)
                    _seen_tags.add(clan_tag)

            # Sort alphabetically by clan name (case-insensitive) for a stable, readable list
            resolved_clan_tags.sort(key=lambda tag: (CACHE.get_clan_name(tag, tag) or tag).lower())

            if resolved_clan_tags:
                clan_link_lines = [
                    f"**{CACHE.get_clan_name(tag, tag)}**: <https://link.clashofclans.com/en?action=OpenClanProfile&tag={tag.replace('#', '')}>"
                    for tag in resolved_clan_tags
                ]
                if len(resolved_clan_tags) == 1:
                    clan_info = t('welcome_message.clan_info', guild_id=guild_id_int, clan_link=clan_link_lines[0])
                else:
                    clan_info = t('welcome_message.clan_info_plural', guild_id=guild_id_int,
                                   clan_links="\n".join(clan_link_lines))
            else:
                clan_info = ""
        
        # Combine message parts
        parts = [greeting]
        if register_info:
            parts.append(register_info)
        if clan_info:
            parts.append(clan_info)
        welcome_message = "\n\n".join(parts)
        
        # Send message to channel
        try:
            await target_channel.send(welcome_message)
            logging.info(f"Sent welcome message for {member.display_name} (id={member.id}) to channel {target_channel.name}")
        except discord.Forbidden:
            logging.warning(f"Missing permissions to send welcome message in channel {target_channel.name} (guild {member.guild.id})")
        except Exception as send_error:
            logging.error(f"Failed to send welcome message for {member.display_name} (id={member.id}): {send_error}")
            
    except Exception as e:
        logging.error(f"Error in welcome message flow for user {member.id} in guild {member.guild.id}: {e}")


async def cleanup_stale_ui_messages() -> None:
    """
    Clean up messages with UI elements (Views) from before bot restart.
    
    UI elements lose their callbacks after bot restart, making them non-functional.
    This function deletes such messages during startup to prevent confusion.
    
    Currently handles:
    - /clan_management messages (mode='clan_management')
    
    Note: RegistrationView messages are handled by repost_playerregistration_messages()
    which deletes old messages and creates new ones.
    """
    from qapbot.cache_manager import CACHE
    
    messages_to_delete: list[tuple[str, dict[str, Any]]] = []
    
    # Scan leaderboard_messages for messages with UI elements
    for key, data in CACHE.leaderboard_messages.items():
        mode = data.get('mode', '')
        
        # Identify messages with UI elements
        if mode == 'clan_management':
            messages_to_delete.append((key, data))
    
    if not messages_to_delete:
        logging.info("No stale UI messages to clean up")
        return
    
    logging.info(f"🧹 Cleaning up {len(messages_to_delete)} stale UI messages from before restart...")
    
    deleted_count = 0
    failed_count = 0
    
    for key, data in messages_to_delete:
        channel_id = data.get('channel_id')
        message_ids_str = data.get('message_ids', '')
        
        if not channel_id or not message_ids_str:
            continue
        
        # Get channel
        channel = QBcore.bot.get_channel(int(channel_id))
        if not channel:
            logging.debug(f"Channel {channel_id} not found for message cleanup (key: {key})")
            # Remove from cache and database since channel is gone
            await CACHE.delete_leaderboard_message(key)
            continue
        
        # Type guard - only process messageable channels
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            logging.debug(f"Skipping non-messageable channel {channel_id}")
            await CACHE.delete_leaderboard_message(key)
            continue
        
        # Parse message IDs (can be comma-separated for split messages)
        message_ids = [int(mid.strip()) for mid in message_ids_str.split(',') if mid.strip().isdigit()]
        
        # Delete each message
        for message_id in message_ids:
            try:
                message = await channel.fetch_message(message_id)
                await message.delete()
                deleted_count += 1
                logging.debug(f"Deleted stale UI message {message_id} in channel {channel_id}")
            except discord.NotFound:
                # Message already deleted manually
                logging.debug(f"Message {message_id} already deleted (not found)")
            except discord.Forbidden:
                logging.warning(f"No permission to delete message {message_id} in channel {channel_id}")
                failed_count += 1
            except Exception as e:
                logging.warning(f"Failed to delete message {message_id}: {e}")
                failed_count += 1
        
        # Remove from cache and database
        await CACHE.delete_leaderboard_message(key)
    
    if deleted_count > 0:
        logging.info(f"✅ Cleaned up {deleted_count} stale UI messages")
    if failed_count > 0:
        logging.warning(f"⚠️ Failed to delete {failed_count} messages (permission or other errors)")

async def repost_playerregistration_messages(*, only_if_not_bottom: bool = False, bump_cooldown_seconds: int = PLAYERREGISTRATION_BUMP_COOLDOWN_SECONDS) -> None:
    """
    Repost player registration messages on bot startup for guilds with registration messages enabled.

    This function scans CACHE.server_config for guilds with registration_channel_id and
    registration_message_enabled=True, then posts or reposts registration messages with persistent buttons.
    
    Also handles cleanup:
    - Deletes messages when registration_message_enabled is False
    - Deletes messages from old channel and posts to new channel if registration_channel_id changed

    Args:
        only_if_not_bottom: If True, only repost when the tracked registration message is NOT the newest message in the channel.
        bump_cooldown_seconds: Minimum seconds between bumps per channel when only_if_not_bottom=True.

    In DEV mode, only processes the configured DISCORD_GUILD_ID.
    """
    from qapbot.ui_registration import RegistrationView

    async def _get_last_message_id(channel: discord.TextChannel) -> Optional[int]:
        try:
            async for msg in channel.history(limit=1, oldest_first=False):
                return msg.id
        except Exception:
            return None
        return None

    async def _delete_message_from_channel(channel_id: int, message_id: str) -> None:
        """Helper to safely delete a message from a channel."""
        try:
            channel = QBcore.bot.get_channel(channel_id)
            if channel and isinstance(channel, (discord.TextChannel, discord.Thread)):
                message = await channel.fetch_message(int(message_id))
                await message.delete()
                logging.debug(f"Deleted registration message {message_id} from channel {channel_id}")
        except Exception as e:
            logging.debug(f"Could not delete registration message {message_id} from channel {channel_id}: {e}")

    server_config = CACHE.server_config
    playerregistration_count = 0
    filtered_count = 0
    now_utc = datetime.now(timezone.utc)
    
    logging.debug(f"repost_playerregistration_messages called, processing {len(server_config)} guilds")

    for guild_id_str, config in server_config.items():
        try:
            guild_id_int = int(guild_id_str)
        except (ValueError, TypeError):
            continue

        registration_message_enabled = config.get('registration_message_enabled', False)
        registration_channel_id = config.get('registration_channel_id')
        tracked_message_id = str(config.get('registration_message_id') or "").strip()
        old_channel_id = config.get('_old_registration_channel_id')  # Track if channel changed

        # Case 1: Registration message disabled - delete if it exists
        if not registration_message_enabled:
            if tracked_message_id:
                # Determine which channel to delete from (prefer current, fall back to old)
                channel_to_delete_from = registration_channel_id if registration_channel_id else old_channel_id
                
                if channel_to_delete_from:
                    await _delete_message_from_channel(int(channel_to_delete_from), tracked_message_id)

                # Clear the message ID and tracking
                config['registration_message_id'] = None
                config['_old_registration_channel_id'] = None
                server_config[guild_id_str] = config
                CACHE.server_config = server_config
                await CACHE.persist_server_config(guild_id_str)
                logging.info(f"Deleted registration message from guild {guild_id_int} (disabled)")
            continue

        # Case 2: Registration message enabled - post/update
        if not registration_channel_id:
            continue

        # Check if channel ID changed
        if old_channel_id:
            logging.debug(f"Channel change detected: old={old_channel_id}, new={registration_channel_id}")
            try:
                old_id_int = int(old_channel_id) if isinstance(old_channel_id, str) else old_channel_id
                new_id_int = int(registration_channel_id) if isinstance(registration_channel_id, str) else registration_channel_id
                
                if old_id_int != new_id_int:
                    # Channel was actually changed - delete from old channel
                    logging.debug(f"Channel mismatch confirmed: {old_id_int} != {new_id_int}")
                    if tracked_message_id:
                        await _delete_message_from_channel(old_id_int, tracked_message_id)
                        logging.info(f"Deleted registration message from old channel {old_channel_id} (guild {guild_id_int})")
                    # Clear the old channel tracking
                    config['_old_registration_channel_id'] = None
                    # Clear message ID so it will be reposted to the new channel
                    config['registration_message_id'] = None
                    tracked_message_id = ""
                    # Save config after clearing old tracking info
                    server_config[guild_id_str] = config
                    CACHE.server_config = server_config
                    await CACHE.persist_server_config(guild_id_str)
                    logging.debug(f"Cleared old channel tracking for guild {guild_id_int}")
            except (ValueError, TypeError) as e:
                logging.warning(f"Failed to compare channel IDs for guild {guild_id_int}: {e}")

        # DEV MODE OVERRIDE:
        # In DEV mode, skip all channels except the explicitly allowed dev_playerregistration_channel_id.
        # Filter BEFORE trying to fetch channel to avoid "Could not find channel" warnings for other guilds
        # If dev_playerregistration_channel_id is 0 (not configured), allow all channels in the DEV guild
        if CONFIG.is_dev_mode and CONFIG.dev_playerregistration_channel_id > 0 and int(registration_channel_id) != CONFIG.dev_playerregistration_channel_id:
            filtered_count += 1
            continue

        # Get the Discord channel
        channel = QBcore.bot.get_channel(int(registration_channel_id))
        if not channel:
            logging.warning(f"Could not find registration channel {registration_channel_id} for guild {guild_id_int}")
            continue
        
        # Type guard - only process text-based channels that support views
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            logging.warning(f"Registration channel {registration_channel_id} for guild {guild_id_int} is not a text-based channel")
            continue

        if only_if_not_bottom:
            # Cooldown gate (stored in the config)
            last_bump_iso = config.get("registration_message_last_bump_iso")
            if isinstance(last_bump_iso, str) and last_bump_iso:
                try:
                    last_bump_dt = datetime.fromisoformat(last_bump_iso.replace("Z", "+00:00"))
                    if (now_utc - last_bump_dt).total_seconds() < bump_cooldown_seconds:
                        continue
                except Exception:
                    pass

            # Bottom-most check: if tracked message is newest, do nothing
            if tracked_message_id:
                # Type guard for _get_last_message_id (requires TextChannel)
                if isinstance(channel, discord.TextChannel):
                    last_id = await _get_last_message_id(channel)
                    if last_id is not None and str(last_id) == tracked_message_id:
                        continue

        # Delete old registration message if it exists (best-effort)
        if tracked_message_id:
            try:
                old_message = await channel.fetch_message(int(tracked_message_id))
                await old_message.delete()
                logging.debug(f"Deleted old registration message {tracked_message_id} in channel {registration_channel_id}")
            except Exception:
                # Message might already be deleted manually
                pass

        # Create new registration message - channel.guild is guaranteed safe after type guard above
        server_name = channel.guild.name
        from qapbot.QBdiscocmdshelper import get_playerregistration_message
        logging.debug(f"Generating registration message:")
        logging.debug(f"  - Channel: {registration_channel_id}")
        logging.debug(f"  - Guild: {guild_id_int} ({server_name})")
        logging.debug(f"  - About to call get_playerregistration_message('{server_name}', guild_id={guild_id_int})")
        registration_msg = get_playerregistration_message(server_name, guild_id=guild_id_int)
        logging.debug(f"Generated registration message (first 200 chars):\n{registration_msg[:200]}...")

        view = RegistrationView(guild_id_int)

        try:
            # Post new registration message
            new_message = await channel.send(registration_msg, view=view)

            # Update server config with new message ID
            config['registration_message_id'] = str(new_message.id)
            config['registration_message_last_bump_iso'] = now_utc.isoformat().replace("+00:00", "Z")
            config['_old_registration_channel_id'] = None  # Clear old channel tracking after successful post
            server_config[guild_id_str] = config
            CACHE.server_config = server_config
            await CACHE.persist_server_config(guild_id_str)
            playerregistration_count += 1

            channel_name = getattr(channel, 'name', 'Unknown')
            logging.info(f"Reposted registration message in {channel.guild.name}#{channel_name} (new message ID: {new_message.id})")

        except Exception as e:
            logging.error(f"Failed to repost registration message in channel {registration_channel_id}: {e}")

    # Update logging to include filtered count in DEV mode
    if playerregistration_count > 0:
        if CONFIG.is_dev_mode and filtered_count > 0:
            logging.info(f"Successfully reposted {playerregistration_count} registration messages ({filtered_count} guilds filtered in DEV mode)")
        else:
            logging.info(f"Successfully reposted {playerregistration_count} registration messages")
    else:
        if CONFIG.is_dev_mode and filtered_count > 0:
            logging.info(f"No registration messages to repost in guild {CONFIG.discord_guild_id} ({filtered_count} guilds filtered)")
        else:
            logging.info("No registration messages to repost")

def log_lifetime_stats() -> None:
    """Log lifetime stats exactly once.  Safe to call from any shutdown path."""
    if getattr(QBcore, 'lifetime_logged', False):
        return
    QBcore.lifetime_logged = True
    try:
        for _lt_line in CACHE.format_lifetime_log():
            logging.info(_lt_line)
    except Exception:
        pass


async def async_cleanup() -> None:
    """
    Perform cleanup operations before bot shutdown with comprehensive resource management.

    Behavior:
        - Closes API client connections
        - Closes database connection
        - Prevents duplicate cleanup operations
        - Handles errors in cleanup gracefully

    Note:
        All in-memory data uses write-through persistence, so no bulk save is needed.
        Called on shutdown signal or bot exit.
    """
    if getattr(QBcore, 'cleaned_up', False):
        return
    log_lifetime_stats()
    try:
        if QBcore.coc_client is not None and not CONFIG.no_coc_api:
            await QBcore.coc_client.close()
    except Exception as e:
        logging.error(f"Error closing coc_client: {e}")
    
    # Close database connection
    try:
        if CACHE.db_manager is not None:  # type: ignore
            await CACHE.db_manager.close()  # type: ignore
    except Exception as e:
        logging.error(f"[DB] Error closing database: {e}")

    # Shut down Monte Carlo process pool cleanly so worker processes exit
    # before the main process does (avoids InterruptedError tracebacks on Ctrl-C)
    try:
        from QBwarsim import shutdown_sim_pool
        shutdown_sim_pool()
    except Exception:
        pass

    QBcore.cleaned_up = True
    logging.info("Cleanup complete. Goodbye!")

if __name__ == "__main__":
    """
    Main entry point for QapBot.

    Behavior:
        - Selects Discord token based on DEV/PROD mode (from CONFIG)
        - In PROD mode: Clears DEV guild commands before starting
        - Runs the Discord bot using the selected token
        - Handles KeyboardInterrupt for graceful shutdown
        - Ensures cleanup is performed on exit

    Note:
        All shutdown and cleanup logic ensures cache consistency and data integrity.
        Token selection is automatic based on DISCORD_GUILD_ID environment variable.
    """
    # Log startup info once at program entry
    logging.info("QapBot started.")
    logging.info(f"Version: {QBcore.BOT_VERSION}")
    logging.info(f"OS: {platform.system()} {platform.release()}")
    logging.info(f"Python: {sys.version.split()[0]}")
    logical_cores = os.cpu_count() or 1
    from QBwarsim import init_sim_pool, get_cpu_info
    init_sim_pool(
        enabled=CONFIG.sim_multiprocess_enabled,
        max_workers=CONFIG.sim_max_workers,
    )
    _cpu_info = get_cpu_info()
    logging.info(f"CPU: {_cpu_info['cpu']} — {logical_cores} logical core(s)")
    logging.info(
        f"[SIM] Multiprocess: {'enabled' if _cpu_info['sim_enabled'] else 'disabled'} — "
        f"{_cpu_info['sim_workers']} worker(s) of {_cpu_info['logical_cores']} logical core(s) used"
    )

    # Use SelectorEventLoop on Windows: ProactorEventLoop (IOCP) has known edge-case issues
    # with discord.py and aiohttp. SelectorEventLoop is more stable for I/O-bound bots
    # and is fully compatible with discord.py 2.x, aiohttp and aiosqlite on Python 3.7+.
    # Note: set_event_loop_policy is deprecated in Python 3.14 (removed in 3.16).
    # Keep until discord.py recommends a loop_factory-based replacement.
    if sys.platform == 'win32':
        try:
            from asyncio.windows_events import WindowsSelectorEventLoopPolicy as _WinPolicy  # type: ignore[import]
            asyncio.set_event_loop_policy(_WinPolicy())  # type: ignore[deprecated]
            logging.info("Windows: using WindowsSelectorEventLoopPolicy (SelectorEventLoop, more stable than ProactorEventLoop for I/O-bound bots)")
        except ImportError:
            # WindowsSelectorEventLoopPolicy was removed in Python 3.14.
            # Modern discord.py 2.x works fine with the default ProactorEventLoop on 3.14+.
            logging.info("Windows: WindowsSelectorEventLoopPolicy not available (Python 3.14+); using default event loop policy")

    # Use token from CONFIG (auto-selected based on DEV/PROD mode)
    token = CONFIG.discord_token
    mode_str = "DEV" if CONFIG.is_dev_mode else "PROD"
    logging.info(f"Starting QapBot in {mode_str} mode...")
    
    # In PROD mode, clear DEV guild commands before starting
    if not CONFIG.is_dev_mode:
        logging.info("Running DEV guild cleanup before PROD startup...")
        try:
            asyncio.run(_clear_dev_guild_commands_before_prod())
        except Exception as e:
            logging.warning(f"DEV guild cleanup failed: {e}")
            logging.info("Continuing with PROD startup anyway...")

    # Pre-flight: wait until Discord's HTTP API is reachable before handing
    # control to bot.run().  Prevents the bot from crashing immediately when
    # it is restarted during a Discord outage and causing a long gap because
    # the process manager's exponential backoff extends restart intervals.
    asyncio.run(_wait_for_discord_connection(token))

    try:
        QBcore.bot.run(token)
    except KeyboardInterrupt:
        logging.info("KeyboardInterrupt caught. Attempting graceful shutdown...")
    except Exception as _run_exc:
        # When our SIGINT handler closes the bot during login, the in-flight
        # aiohttp request raises ClientConnectionError (connector already closed).
        # Suppress this if shutdown was already initiated by the signal handler.
        if QBcore.shutdown_initiated:
            logging.info(f"Shutdown in progress; suppressed {type(_run_exc).__name__} from bot.run()")
        else:
            raise
    finally:
        # If cleanup not yet done (e.g., signal before task loop exit), perform it now in a fresh loop
        if not getattr(QBcore, 'cleaned_up', False):
            try:
                asyncio.run(async_cleanup())
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(async_cleanup())
                loop.close()

    # Propagate special exit code (e.g. maintenance restart) after bot.run() returns
    _exit_code = getattr(QBcore, 'exit_code', 0)
    if _exit_code != 0:
        logging.info(f"Exiting with code {_exit_code}.")
        sys.exit(_exit_code)
