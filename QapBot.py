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

# Suppress the benign "Ignoring exception in autocomplete... Unknown interaction (10062)" ERROR
# discord.py logs internally (app_commands/tree.py) when the Discord client fires a newer
# autocomplete request before the bot finishes responding to the previous one (e.g. the user kept
# typing) — the older interaction token is already invalid by the time we try to answer it.
# discord.py's own comment there is "Suppress exception since it can't be handled anyway"; it
# already swallows the exception, it just also logs it at ERROR level. Not a bug in our
# autocomplete handlers, and became newly visible once Phase 0b (CWL_ROSTER_PLANNING_PLAN.md)
# started allowing autocomplete-bearing commands like /list to be invoked from DMs.
class _AutocompleteExpiredInteractionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno != logging.ERROR or "Ignoring exception in autocomplete" not in record.getMessage():
            return True
        exc = record.exc_info[1] if record.exc_info else None
        return not (exc is not None and "10062" in str(exc))

# Both filters above match ERROR records logged by discord.py submodule loggers (discord.client,
# discord.app_commands.tree, ...), which are children of the 'discord' logger, not 'discord'
# itself — a Logger's own addFilter() is only consulted for records logged directly on that exact
# logger object, never for records merely propagating up from a child logger's log call (Python's
# logging.Logger.callHandlers() walks ancestor HANDLERS, not ancestor LOGGER filters). Handler-level
# filters, by contrast, run for every record that reaches that handler regardless of which logger
# originated it — so both filters are attached to our actual handlers here rather than the
# 'discord' logger object, which would silently never fire for either of these submodule-sourced
# messages.
for _h in handlers:
    _h.addFilter(_DiscordReconnectFilter())
    _h.addFilter(_AutocompleteExpiredInteractionFilter())

# --- Automatic-GC pause visibility -----------------------------------------
# The end of every update cycle already runs a scoped gc.collect(1) instead of
# a full sweep (see Pitfall 16 / copilot-instructions.md) precisely because a
# full generation-2 collection walks the entire long-lived CACHE object graph
# (hundreds of thousands to millions of objects in PROD: clan_name_cache,
# coc_clan_cache, etc.) and, being a single atomic C call, freezes the
# WHOLE process — every thread, including the event loop — for its duration.
# That fix only covers the *explicit* end-of-cycle call. CPython's automatic
# generational collector is never disabled in this codebase and still runs
# gen-2 sweeps on its own allocation-threshold schedule, including in the
# middle of Phase-1's concurrent API-response churn. Such a pause previously
# produced zero log output during the freeze and then, once the GIL was
# released, a burst of concurrently in-flight coc_retry() calls all logging
# ~the same inflated elapsed time — indistinguishable from "the CoC API is
# slow" (see [COC-API-SLOW] bursts in qapbot_PROD.log). Logging start/stop of
# every automatic collection here makes that failure mode show up explicitly
# instead of being misread as network latency.
import gc
import time as _gc_time
_gc_pause_start: dict[int, float] = {}

def _log_slow_gc(phase: str, info: dict) -> None:  # type: ignore[type-arg]
    gen = info.get("generation", -1)
    if phase == "start":
        _gc_pause_start[gen] = _gc_time.perf_counter()
        return
    t0 = _gc_pause_start.pop(gen, None)
    if t0 is None:
        return
    elapsed = _gc_time.perf_counter() - t0
    if elapsed >= 0.5:  # gen-0 collections are frequent and normally sub-ms; only the rare slow ones matter
        # INFO, not WARNING: expected/monitored background behavior (mitigated by the
        # startup gc.freeze() + nightly re-freeze), not an actionable error on its own.
        logging.info(
            "[GC-AUTO] Automatic gen-%d collection paused the process for %.3fs (collected=%d, uncollectable=%d)",
            gen, elapsed, info.get("collected", 0), info.get("uncollectable", 0),
        )

if not any(getattr(_cb, "__name__", None) == "_log_slow_gc" for _cb in gc.callbacks):
    # Name-based (not identity-based) dedup guard: QBdiscordcmds.py does
    # `from QapBot import GLOBAL_GUILD_ID, run_nightly_maintenance_routine,
    # is_monthly_migration_due` at module level. Since this file runs as
    # `__main__`, that import makes Python load it a SECOND time under the
    # module name "QapBot" (sys.modules has no entry for "QapBot", only
    # "__main__", so it doesn't recognize this as already-loaded) — re-running
    # every top-level statement in this file a second time. The
    # `if __name__ == "__main__":` guard at the bottom correctly stops that
    # second execution from starting a second bot connection, but a bare
    # `gc.callbacks.append(...)` isn't idempotent: each execution creates a new
    # closure object, so the list ends up with two separate entries and every
    # real GC pause gets logged twice (confirmed live in prod 2026-08-08 —
    # every [GC-AUTO] line appeared as an exact near-duplicate pair). Checking
    # by `__name__` instead of object identity catches the duplicate across
    # separate executions, where identity checks (`is`/`in` on the function
    # object) can't. The root cause (the circular import itself) is left
    # alone — fixing that is a larger, separate change — but ANY future
    # module-level code here that mutates shared/global state (not just
    # simple def/class/import) needs the same kind of guard, or it will
    # silently double up too.
    gc.callbacks.append(_log_slow_gc)

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
import zlib as _zlib_mod  # crc32 → stable per-clan re-check phase offset (tracker #0009)
import QBcore
from qapbot.config import CONFIG
from qapbot.cache_manager import CACHE
from qapbot.constants import (
    SECONDS_PER_HOUR,
    MAX_RETRY_BACKOFF_SECONDS,
    PASSIVE_CLAN_REFRESH_INTERVAL_DAYS,
    PLAYERREGISTRATION_BUMP_COOLDOWN_SECONDS
)
from qapbot.discord_health import discord_retry, bulk_sync_global_commands
from qapbot.coc_health import reset_cycle_stats, get_coc_stats, clear_maintenance_detection, is_maintenance_detected, clear_dns_detection
from QBhelperfunctions import (
    generate_leaderboard_text, generate_cwlinfo_embeds, generate_cwlinfo_comp_embeds, post_discord_content_with_tracking,
    post_leaderboard_to_discord, calculate_content_hash,
    fetch_clan_war_data, process_clan_war_data,
    generate_cwl_group_image, update_cwl_group_stats,
    resolve_subscription_period, coc_clan_profile_url,
)

# Sentinel returned by fetch_single_clan when the CoC API responded successfully
# but the clan has no active war (notInWar).  Distinct from None (API failure) so
# Phase 3 can update last_war_update for the no-war case (removing the clan from
# the overdue backlog for 22h) while leaving genuine failures for retry next cycle.
_NOT_IN_WAR: dict[str, Any] = {"__not_in_war__": True}

# Width of the window over which warless clans' 22h re-checks are spread (tracker #0009).
# See compute_recheck_offset_seconds() for why this exists.
RECHECK_JITTER_SECONDS = 7200  # 2h


def compute_recheck_offset_seconds(
    clan_tag: str, jitter_seconds: int = RECHECK_JITTER_SECONDS
) -> int:
    """Stable per-clan phase offset added to the flat INACTIVE_CLAN_UPDATE_INTERVAL_HOURS check.

    A flat 22h threshold makes the polling schedule self-clumping: every clan polled in the
    same cycle becomes due again in the same cycle 22h later, forever. Confirmed on PROD
    (tracker #0009) — 3000-4200-clan bursts recurred at exactly 22h and drifted 2h earlier
    each day as 22h aliased against the 24h day (08-25 07:11 -> 08-26 05:19 -> 08-27 03:21,
    and 08-25 11:48 -> 08-26 09:48 -> 08-27 07:48). Those waves drove +541 to +700 MB
    single-cycle RSS jumps, because every clan polled in a cycle is held simultaneously
    (its coc.ClanWar in fetch_results, its coc.Clan in coc_clan_cache).

    The offset MUST be stable per clan rather than a fresh random draw per cycle: a fresh
    draw would only jitter each due-time back and forth around the same clump centre, never
    dispersing it. crc32 of the tag is deterministic and uniformly distributed, survives
    restarts with nothing to persist, and — because each clan keeps its own phase on every
    subsequent poll — a clumped population spreads over the window once and then stays spread.

    Only ever DELAYS a poll (offset >= 0), so no clan is polled more often than before.

    Args:
        clan_tag: Clan tag, e.g. "#2C9UR9GJY". Used only as a hash input.
        jitter_seconds: Width of the spread window. <= 0 disables jitter entirely
                        (offset 0), restoring the flat threshold.

    Returns:
        Seconds in [0, jitter_seconds) to add on top of the 22h interval for this clan.
    """
    if jitter_seconds <= 0:
        return 0
    return _zlib_mod.crc32(clan_tag.encode("utf-8")) % jitter_seconds


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

    2026-08-14: that flat 60 s budget didn't account for QBcore.db_maintenance_mode,
    set for the whole duration of initialize_database() — including a first-run index
    build on a multi-million-row table that can legitimately take several minutes (see
    WarHistoryDB._create_index_if_missing in db_manager.py). A SIGINT arriving mid-build
    let this fallback fire its 60 s force-close anyway, which raced asyncio.run()'s
    shutdown-time task cancellation against the still-running, uninterruptible
    background CREATE INDEX call — crashing the aiosqlite worker thread once it finally
    finished, against an already-closed loop. Fix: time spent in db_maintenance_mode
    doesn't count against the 60 s budget. initialize_database() already has its own
    generous (30 min) watchdog, so once it actually finishes — success or failure —
    db_maintenance_mode clears and the normal 60 s fallback countdown resumes.

    IMPORTANT: Always call bot.close() after cleanup so bot.run() / asyncio.run()
    can actually return.  In maintenance mode cleaned_up=True immediately (resources
    were closed by do_maintenance_shutdown), but bot.close() was never called —
    without it bot.start() blocks forever on the Discord WebSocket, preventing exit.
    """
    _waited = 0.0
    while _waited < 60.0:  # 60 s budget, paused while db_maintenance_mode is True
        await asyncio.sleep(0.1)
        if QBcore.cleaned_up or QBcore.bot.is_closed():
            break  # resources clean — fall through to close the Discord connection
        if not QBcore.db_maintenance_mode:
            _waited += 0.1
    else:
        # Fallback: periodic_main did not finish within 60 s of actual idle time
        # (not started or stuck) — genuinely long DB maintenance doesn't count.
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

async def initialize_database() -> None:
    """
    Initialize the database (schema creation + any first-run index/schema migrations).

    Runs once per process (guarded by ``CACHE.db_manager is None``), BEFORE CoC API
    login and BEFORE the periodic update cycle (``periodic_main()``) starts. This is
    a deliberate ordering, not incidental:

    2026-07-30 incident history — two earlier attempts got this wrong:
      1. DB init originally happened inside ``startup_login()``, itself wrapped in
         ``asyncio.wait_for(..., timeout=60.0)`` in ``on_ready()``. A first-time schema
         migration (a composite index build on a multi-million-row table) took minutes,
         not seconds, and blew that 60s budget — the bot never finished starting.
      2. Deferring the slow migration to a fire-and-forget background task solved the
         timeout, but not the real problem: the migration still holds SQLite's single
         writer lock for its whole duration, and running it with zero coordination
         while ``periodic_main()``'s PHASE-1 was already doing live concurrent writes
         caused a "database is locked" storm across the whole app.
    Fix: DB init (including any slow migration) now runs to completion, sequenced
    strictly BEFORE anything else in the app could possibly write to the DB —
    ``periodic_main()`` and CoC login both happen after this returns. Discord's
    gateway connection is already up by the time ``on_ready()`` runs, so the bot still
    shows online; ``QBcore.db_maintenance_mode`` blocks Discord commands with the
    existing friendly "maintenance in progress" message for the (normally instant,
    occasionally multi-minute on a first run after a schema change) duration.

    A generous 30-minute timeout guards against a genuinely stuck/hung migration
    without constraining the CoC-login timeout, which stays a tight, separate 60s in
    ``on_ready()`` — that one has no legitimate reason to ever be slow.
    """
    if CACHE.db_manager is not None:
        return  # already initialized (e.g. duplicate on_ready invocation)

    logging.info("💾 Initializing database (includes any pending first-run schema migrations)...")
    QBcore.db_maintenance_mode = True
    try:
        from qapbot.db_manager import WarHistoryDB
        db_manager = WarHistoryDB()
        await asyncio.wait_for(
            db_manager.initialize(CONFIG.db_path, CONFIG.history_db_path),
            timeout=1800.0,
        )
        CACHE.db_manager = db_manager
        logging.info(f"[DB] Database initialized at {CONFIG.db_path} (history: {CONFIG.history_db_path})")

        # Guardrail for the 2026-08-14 hot/history schema-drift incident class of bug (see
        # check_hot_history_schema_parity_sync's docstring, qapbot/db_manager.py): a genuine
        # column SET mismatch (not just reordering, which every affected query is now immune
        # to) between main.<table> and history.<table> is not automatically recoverable, so
        # it's surfaced loudly here rather than silently causing "no such column" failures or
        # missing stats deep inside some later query.
        mismatched_tables = await asyncio.to_thread(db_manager.check_hot_history_schema_parity_sync)
        if mismatched_tables:
            logging.critical(
                "[DB-SCHEMA-CHECK] hot/history column SET mismatch detected for: "
                f"{', '.join(mismatched_tables)} — a column exists in one schema but not the "
                "other. Queries against these tables may fail or silently omit data. See "
                "qapbot/scripts/repair_history_schema_drift.py / DATABASE_ARCHITECTURE.md."
            )
    except asyncio.TimeoutError:
        logging.error("❌ Database initialization timed out after 30 minutes")
        raise RuntimeError("Database initialization timed out - bot cannot start")
    except Exception as e:
        logging.error(f"[DB] Failed to initialize database: {e}")
        raise RuntimeError("Database initialization failed - bot cannot start")
    finally:
        QBcore.db_maintenance_mode = False


async def _validate_coc_api_keys() -> None:
    """
    Sanity-check that every provisioned CoC API key actually authenticates.

    coc.py's own key rotation (``HTTPClient.initialise_keys()``, called during
    ``login()``) only checks a candidate key's name + registered IP against the
    developer-portal listing before reusing it across restarts — it never
    test-calls the key itself. A key that's been revoked or is otherwise
    broken on Supercell's auth side, while still listing normally on the
    portal, therefore stays silently in rotation forever: every restart keeps
    picking it back up, and BatchThrottler's stable round-robin means the same
    fraction of requests keeps failing with "Invalid authorization" on every
    run. This bit DEV for over an hour across 5 restarts before being traced
    (2026-08-09) purely because nothing surfaced it at startup — it only
    showed up later as scattered per-war 403s. See
    qapbot/docs/COPILOT_PITFALLS_COOKBOOK.md Pitfall 24 for the full incident.

    Runs as a fire-and-forget background task (not awaited by
    startup_login()) so a slow/unreachable key doesn't extend the bot's
    startup timeout budget; only logs, never raises.
    """
    client = QBcore.coc_client
    http = getattr(client, 'http', None) if client else None
    keys: List[str] = list(getattr(http, '_keys', None) or []) if http else []
    if not keys:
        return

    base_url = getattr(http, 'base_url', 'https://api.clashofclans.com/v1')
    bad_key_positions: List[int] = []

    async def _check_one(index: int, key: str) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{base_url}/locations",
                    headers={"Authorization": f"Bearer {key}"},
                    params={"limit": 1},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        body = (await resp.text())[:200]
                        logging.critical(
                            f"[COC-KEY-SANITY] API key #{index + 1}/{len(keys)} failed validation: "
                            f"HTTP {resp.status} — {body}"
                        )
                        bad_key_positions.append(index)
        except Exception as exc:
            logging.critical(f"[COC-KEY-SANITY] API key #{index + 1}/{len(keys)} check errored: {exc}")
            bad_key_positions.append(index)

    try:
        await asyncio.gather(*[_check_one(i, k) for i, k in enumerate(keys)])
    except Exception as exc:
        logging.error(f"[COC-KEY-SANITY] Key validation pass itself errored: {exc}")
        return

    if bad_key_positions:
        mode_str = "DEV" if CONFIG.is_dev_mode else "PROD"
        positions_1idx = [p + 1 for p in bad_key_positions]
        logging.critical(
            f"[COC-KEY-SANITY] ({mode_str}) {len(bad_key_positions)}/{len(keys)} CoC API key(s) "
            f"failed validation at startup (1-indexed position(s) {positions_1idx}) — "
            f"a matching fraction of requests will silently fail until the broken key(s) are "
            f"removed at developer.clashofclans.com (delete the keys named 'Created with coc.py "
            f"Client' for this account; coc.py recreates a fresh full set on the next restart)."
        )
        # Log-only alerts get missed for days (this exact incident took over an hour across 5
        # restarts to notice manually, see Pitfall 24) — DM the configured admin so a broken key
        # is impossible to miss. This runs from on_ready() -> _run_startup_initialization(), so
        # the Discord client is already logged in and ready; no wait_until_ready() needed.
        if CONFIG.server_admin:
            dm_sent = await CACHE.send_user_dm(
                CONFIG.server_admin,
                f"⚠️ **QapBot CoC API key alert ({mode_str})**\n"
                f"{len(bad_key_positions)}/{len(keys)} CoC API key(s) failed validation at "
                f"startup (position(s) {positions_1idx}).\n"
                f"A matching fraction of CWL/war requests will silently fail until this is fixed.\n"
                f"Fix: delete the keys named `Created with coc.py Client` for the {mode_str} "
                f"account at developer.clashofclans.com, then restart the bot.",
            )
            if not dm_sent:
                logging.warning(
                    "[COC-KEY-SANITY] Could not DM the configured admin about the broken key(s) "
                    "(DMs disabled, bot blocked, or user fetch failed) — see the CRITICAL log "
                    "line above instead."
                )
        else:
            logging.warning(
                "[COC-KEY-SANITY] SERVER_ADMIN is not configured — cannot DM an alert about the "
                "broken key(s) above."
            )
    else:
        logging.info(f"[COC-KEY-SANITY] All {len(keys)} CoC API key(s) validated OK.")


async def startup_login() -> None:
    """
    Authenticate the Clash of Clans API client with BatchThrottler.

    Behavior:
        - Creates coc.Client with BatchThrottler for true parallelization (10 req/sec)
        - Loads credentials from CONFIG (auto-selects DEV/PROD based on DISCORD_GUILD_ID)
        - Authenticates with CoC API
        - Stores client in CACHE for centralized API access
        - Ensures QBcore.coc_client is ready for backward compatibility

    Database initialization happens separately, in ``initialize_database()``, called
    before this function so its (occasionally slow, first-run-only) schema migrations
    never share this function's tight login timeout — see that function's docstring.

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
        # Fire-and-forget key sanity check (see _validate_coc_api_keys docstring) — holds a
        # strong reference on QBcore so the task isn't garbage-collected mid-flight, per the
        # standard asyncio create_task() pitfall.
        QBcore.coc_key_sanity_task = asyncio.create_task(_validate_coc_api_keys())
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
                    month, year, month_range = resolve_subscription_period(sub)
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
        await repost_cwl_management_messages(only_if_not_bottom=True, bump_cooldown_seconds=PLAYERREGISTRATION_BUMP_COOLDOWN_SECONDS)
        await repost_cwl_player_hub_messages(only_if_not_bottom=True, bump_cooldown_seconds=PLAYERREGISTRATION_BUMP_COOLDOWN_SECONDS)
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

    # Per-clan re-check phase offset (2026-08-29, tracker #0009) — spreads the warless 22h
    # re-checks over a 2h window so they stop re-forming into one huge per-cycle wave.
    # Full rationale in compute_recheck_offset_seconds() at module level.
    def _recheck_cutoff_for(clan_tag: str) -> datetime:
        """Per-clan due-time cutoff: the flat 22h cutoff pushed back by a stable 0-2h offset."""
        return cutoff_dt - timedelta(seconds=compute_recheck_offset_seconds(clan_tag))

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
    # Passive-clan monthly refresh (Phase 1.6, see CLAN_WAR_TRACKING.md write-path 8)
    # piggybacks its candidate discovery on THIS loop instead of running a second
    # full clan_name_cache scan: this loop already visits every clan every cycle
    # and fast-rejects exactly the track_war_updates=False population Phase 1.6
    # needs — collecting candidates here during that already-paid-for pass cut a
    # ~5s redundant second scan entirely (confirmed via prod [CATEGORIZE-TIMING]
    # vs the old separate [PASSIVE-REFRESH] scan line, both ~5s for the same
    # ~420K clans).
    _passive_refresh_candidates: list[tuple[str, str]] = []  # (clan_tag, sort_key)
    _passive_refresh_cutoff = now - timedelta(days=PASSIVE_CLAN_REFRESH_INTERVAL_DAYS)

    _categorize_clan_count = 0
    for clan_tag, clan_data in list(CACHE.clan_name_cache.items()):
        _categorize_clan_count += 1
        if _categorize_clan_count % 2000 == 0:
            await asyncio.sleep(0)

        try:
            # Fast-reject path first: passively-tracked / deleted clans are
            # >99.9% of all entries and never need anything below this.
            _is_deleted = clan_data.get('is_deleted')
            if not clan_data.get('track_war_updates', True) or _is_deleted:
                if not _is_deleted and not clan_data.get('has_active_subscriptions'):
                    _last_checked = clan_data.get('last_checked_via_api')
                    if not _last_checked:
                        _passive_refresh_candidates.append((clan_tag, ''))
                    else:
                        try:
                            _lc_dt = datetime.fromisoformat(_last_checked)
                            if _lc_dt.tzinfo is None:
                                _lc_dt = _lc_dt.replace(tzinfo=timezone.utc)
                            if _lc_dt < _passive_refresh_cutoff:
                                _passive_refresh_candidates.append((clan_tag, _last_checked))
                        except (ValueError, TypeError):
                            _passive_refresh_candidates.append((clan_tag, ''))
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

            # Check if clan has ongoing war using IN-MEMORY metadata (no disk I/O).
            # Hoisted above the due-check (2026-08-29) because the jitter below needs it too;
            # the else-branch consumer further down reuses this same lookup.
            meta = CACHE.temp_war_metadata.get(clan_tag)
            # War-critical clans (preparation/in_war) are EXEMPT from the re-check jitter, for
            # exactly the reason they are exempt from _MAX_INACTIVE_PER_CYCLE further below:
            # the else-branch smart-backdating sets their last_war_update to land the next poll
            # at a precise moment (30min after war start / 8min BEFORE war end). Adding up to 2h
            # of jitter on top would push that poll past the moment it exists to capture — the
            # in_war snapshot would miss late attacks and the preparation temp file would never
            # be overwritten. Only warless clans, whose 22h re-check has no such deadline, are
            # spread out.
            _is_war_critical = bool(meta) and meta.get('state', '') in ('preparation', 'in_war', 'inwar')
            _due_cutoff = cutoff_dt if _is_war_critical else _recheck_cutoff_for(clan_tag)

            if last_update_dt <= _due_cutoff:
                clans_to_update.append((clan_tag, False))
            else:
                hours_since_update = (now - last_update_dt).total_seconds() / SECONDS_PER_HOUR
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

    # Flush all backdated timestamp updates in a single DB transaction.
    # Merge in any timestamps that failed to persist at a previous call site (this cycle's
    # Phase-3 flush, or an earlier cycle's) so a transient DB error gets a real retry here
    # instead of silently waiting for a restart to self-heal — see QBcore.pending_clan_timestamp_retries.
    if QBcore.pending_clan_timestamp_retries:
        _timestamp_batch.extend(QBcore.pending_clan_timestamp_retries)
        QBcore.pending_clan_timestamp_retries = []
    if _timestamp_batch:
        try:
            await CACHE.db_manager.bulk_update_clan_timestamps(_timestamp_batch)  # type: ignore[union-attr]
            logging.debug(f"[CATEGORIZE] Batch-persisted {len(_timestamp_batch)} backdated timestamps")
        except Exception as e:
            logging.error(f"[CATEGORIZE] Failed to batch-persist timestamps: {e} — queued {len(_timestamp_batch)} for retry")
            QBcore.pending_clan_timestamp_retries.extend(_timestamp_batch)

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
    #
    # 2026-08-29 (tracker #0009): lowered 5000 -> 1500 as a memory bound, not a runtime one.
    # Every clan polled in a cycle is held simultaneously — its coc.ClanWar sits in
    # fetch_results until Phase 3 consumes it (each pinning ~120-170 KB of raw API payload via
    # coc.py's un-exhausted _iter_members generator), and its coc.Clan sits in coc_clan_cache
    # (~69-90 KB each, measured). A 3800-4200-clan wave cost +541 to +700 MB of RSS in a SINGLE
    # cycle on PROD, and quiet cycles only give back 10-40 MB, so each wave ratcheted the floor
    # up until the process sat at ~6 GB. Distribution over 963 cycles: median 669, p75 857,
    # p90 1317, p95 1929, mean 817 — so 1500 clips only the top ~8% of cycles and leaves ~2x
    # headroom over the mean for the deferred backlog to drain (a 4200-clan wave spills ~2700,
    # which clears in 3-4 following cycles = ~20 min against a 22h SLA). War-critical clans stay
    # exempt from the cap below, so nothing latency-sensitive is deferred by this.
    _MAX_INACTIVE_PER_CYCLE = 1500
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
    # PHASE 1.6: PASSIVE CLAN REFRESH (lightweight get_clan() ping)
    # ============================================================================
    # Passively-tracked clans (track_war_updates=False) are excluded from the main
    # polling loop entirely and only ever get refreshed as a side effect of their
    # CWL group happening to be rediscovered this season (see
    # CLAN_WAR_TRACKING.md write-path 8). This periodically pings the most-overdue
    # ones so a real-game promotion to Master III+ is never missed indefinitely
    # for a clan whose group nobody else ever touches.
    # Candidate discovery happens above, piggybacked on the categorization loop
    # (_passive_refresh_candidates) — no second clan_name_cache scan here.
    # Runs every cycle (no interval gate) at _PASSIVE_REFRESH_BATCH_SIZE=1000/run
    # (QBhelperfunctions.py) to burn down the initial backlog in about a day
    # instead of trickling it out over a month. Once caught up, per-cycle
    # get_clan() volume drops close to zero naturally (few clans newly overdue
    # each cycle).
    _phase16_t0 = time.monotonic()
    try:
        from QBhelperfunctions import refresh_stale_passive_clans  # type: ignore[attr-defined]
        await refresh_stale_passive_clans(_passive_refresh_candidates)
    except Exception as e:
        logging.error(f"[PHASE-1.6] Passive clan refresh error: {e}")
    logging.info(f"[PHASE-1.6-TIMING] Completed in {time.monotonic() - _phase16_t0:.3f}s")

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
        QBcore.spawn_tracked(
            "phase2-wal-checkpoint",
            asyncio.to_thread(CACHE.db_manager.run_passive_checkpoint),
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

        # ── Memory hygiene: drop the list's reference to this result as soon as it is in
        # hand (2026-08-29, tracker #0009). Each result holds a coc.ClanWar whose
        # un-exhausted _iter_members generator pins the whole raw API payload (~120-170 KB
        # measured), and fetch_results is only deleted much further down — after Phase 3B,
        # notifications, leaderboards and role sync. Clearing the slot here means at most ONE
        # war payload (the `result` local, rebound next iteration) outlives its own iteration,
        # instead of the entire cycle's worth staying resident to the end of the cycle.
        # Assignment (not resize) during enumerate() is safe. Placed before the type guards so
        # every `continue` path below is covered by this single line.
        fetch_results[_idx] = None  # type: ignore[call-overload]

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

    # Flush all Phase-3 timestamp updates in a single DB transaction.
    # Merge in any timestamps that failed to persist at a previous call site — see
    # QBcore.pending_clan_timestamp_retries.
    if QBcore.pending_clan_timestamp_retries:
        _phase3_ts_batch.extend(QBcore.pending_clan_timestamp_retries)
        QBcore.pending_clan_timestamp_retries = []
    if _phase3_ts_batch:
        try:
            await CACHE.db_manager.bulk_update_clan_timestamps(_phase3_ts_batch)  # type: ignore[union-attr]
        except Exception as e:
            logging.error(f"[PHASE-3] Failed to batch-persist timestamps: {e} — queued {len(_phase3_ts_batch)} for retry")
            QBcore.pending_clan_timestamp_retries.extend(_phase3_ts_batch)

    # Fire a single background WAL checkpoint after all Phase-3 DB writes
    # (war data + timestamps) are complete.  Previous approach ran a
    # checkpoint inside each flush_pending_war_writes() call, causing the
    # subsequent timestamp flush to stall (419 timestamps in 4.75s vs 0.19s
    # when the WAL is clean).
    if CACHE.db_manager is not None:
        QBcore.spawn_tracked(
            "phase3-wal-checkpoint",
            asyncio.to_thread(CACHE.db_manager.run_passive_checkpoint),
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
    # Same, for the short-TTL player cache (2026-08-22). Its hard size cap is enforced on
    # insert; this releases entries that simply aged out between bursts.
    CACHE.evict_stale_player_cache()

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
    
    # Mark finished CWL league groups as cwl_ended=1 (2026-08-22, tracker #0017). That flag is
    # what stops _find_active_cwl_war_for_clan() re-walking a finished group's war tags on every
    # notInWar clan; before this it had no periodic writer at all, so 136,707 polled clans sat at
    # cwl_ended=0 re-downloading long-finished CWL wars. Deliberately BEFORE the Discord-health
    # return below — this is pure DB work with no Discord I/O, so it must keep running during an
    # outage. 5 sequential to_thread batches of 500; see the function's docstring.
    try:
        from QBhelperfunctions import sweep_cwl_ended_flags
        _sweep = await sweep_cwl_ended_flags()
        if _sweep["marked"]:
            logging.info(
                f"[CWL-ENDED-SWEEP] Marked {_sweep['marked']} league group(s) as ended "
                f"({_sweep['checked']} checked across {_sweep['batches_run']} batch(es)"
                f"{', rotation wrapped' if _sweep['wrapped'] else ''})"
            )
    except Exception as e:
        logging.error(f"Error in CWL ended-flag sweep: {e}")

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

    # Re-route CWL enrollment DMs whose account changed Discord owner while the DM was still
    # unanswered (2026-08-22, tracker #0019). Placed after the Discord-health guard above because
    # it deletes and sends DMs; a no-op (one cheap query, scoped to signup_open events) on every
    # cycle where nothing changed hands. See the function's own docstring for why this is a sweep
    # rather than a hook on the link/unlink path.
    try:
        from qapbot.QBdiscocmdshelper_cwl import reroute_cwl_enrollment_dms_after_ownership_change
        _reroute = await reroute_cwl_enrollment_dms_after_ownership_change()
        if _reroute["rerouted"] or _reroute["send_failed"]:
            logging.info(
                f"[CWL-DM-REROUTE] {_reroute['rerouted']} DM(s) re-routed to a new owner "
                f"({_reroute['retracted']} old message(s) retracted, "
                f"{_reroute['send_failed']} could not be re-sent) out of "
                f"{_reroute['checked']} unanswered DM(s) checked"
            )
    except Exception as e:
        logging.error(f"Error in CWL enrollment DM re-route sweep: {e}")

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
    await repost_cwl_management_messages(only_if_not_bottom=True, bump_cooldown_seconds=PLAYERREGISTRATION_BUMP_COOLDOWN_SECONDS)
    await repost_cwl_player_hub_messages(only_if_not_bottom=True, bump_cooldown_seconds=PLAYERREGISTRATION_BUMP_COOLDOWN_SECONDS)

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


async def _warm_global_db_stats_cache(force_refresh: bool = False) -> None:
    """
    Populate (or refresh) CACHE.db_manager's /status global-DB-statistics
    cache (see get_global_db_statistics_sync's docstring — it's a multi-GB
    full-table-scan query, cached for 25h rather than recomputed per call).

    Called fire-and-forget at startup so the first /status after a restart
    never blocks on the cold scan, and again with force_refresh=True at the
    end of nightly maintenance so the cache reflects post-VACUUM/ANALYZE
    state and stays warm across the 25h window between /status calls.
    Swallows all exceptions — a reporting-stat warm-up must never break
    startup or nightly maintenance.

    2026-08-18: the plain startup call (force_refresh=False) no longer re-runs the multi-GB
    scan cold on every restart. It first tries to restore the last EXACT snapshot persisted to
    bot_metadata by a previous force_refresh=True run (nightly maintenance, or a manual /status
    force-refresh) — a single tiny row read, effectively instant. This was confirmed live on
    PROD to matter: the cold scan shared db_manager's sync connection pool with the very first
    periodic clan-fetch cycle's Phase-1 (also starting at startup), starving it for its ~20s
    duration. Only a brand-new DB with no snapshot yet falls through to the real scan — which is
    now itself parallelized (5 independent queries run concurrently instead of sequentially) so
    even that cold-start path is faster than before. force_refresh=True always does the real
    scan and refreshes the persisted snapshot for the next restart.
    """
    db_mgr = getattr(CACHE, "db_manager", None)
    if db_mgr is None:
        return
    try:
        _t0 = time.monotonic()
        if not force_refresh and await db_mgr.preload_global_db_statistics_from_snapshot():
            logging.info(
                f"[DB-STATS-WARM] Restored global DB statistics from persisted snapshot in "
                f"{time.monotonic() - _t0:.3f}s"
            )
            return
        await asyncio.to_thread(db_mgr.get_global_db_statistics_sync, force_refresh=force_refresh)
        logging.info(f"[DB-STATS-WARM] Global DB statistics cache warmed in {time.monotonic() - _t0:.1f}s")
    except Exception as _exc:
        logging.warning(f"[DB-STATS-WARM] Failed to warm global DB statistics cache: {_exc}")


async def run_nightly_maintenance_routine(
    db_mgr: Any, run_migration: bool, migration_time_budget_seconds: Optional[float] = None
) -> str:
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
        migration_time_budget_seconds: How long Step 0.5 may run for, if
            run_migration is True. Defaults to
            CONFIG.history_migration_time_budget_minutes * 60 (the scheduled
            nightly task's budget) when not given. Added 2026-08-01 so the
            /admin command — an interactive, user-awaited call, unlike the
            fire-and-forget scheduled task — can pass a much shorter budget:
            migration isn't /admin's purpose (the opportunistic per-cycle
            chunking already carries the bulk of migration progress), and a
            long migration wait risks the Discord interaction token expiring
            (~15 min) before the reply can be sent.

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
        # The budget caps how long this can block Discord commands in one
        # sitting (2026-08-01: an uncapped first-ever run against a large
        # backlog took 10+ hours). A capped run reports PARTIAL, not done, so
        # is_monthly_migration_due() keeps retrying later.
        if run_migration:
            _budget = (
                migration_time_budget_seconds
                if migration_time_budget_seconds is not None
                else CONFIG.history_migration_time_budget_minutes * 60
            )
            await db_mgr.monthly_history_migration(time_budget_seconds=_budget)
        # Steps 1-3: WAL checkpoint → REINDEX/VACUUM → ANALYZE (blocks
        # Discord commands internally via db_maintenance_mode).
        _result = await db_mgr.nightly_db_maintenance()
        # Step 4: refresh the /status global-DB-statistics cache now that
        # maintenance is fully done, so it reflects post-VACUUM/ANALYZE state
        # and stays warm for the full 25h TTL until the next nightly run.
        await _warm_global_db_stats_cache(force_refresh=True)

        # Step 5: full GC sweep + re-freeze, so tomorrow's automatic gen-2 sweeps stay cheap.
        # Nightly-only counterpart to the startup gc.freeze() (see [GC-FREEZE] near on_ready)
        # and the per-cycle scoped gc.collect(1) (see the [GC-AUTO] pause logger registered
        # near the top of this file / _post_cycle_cleanup). Every cycle promotes newly-created
        # long-lived CACHE growth (new clans, new war metadata — substantial during a CWL
        # season, e.g. 7000+ active wars) into gen-2, which the one-time startup freeze does
        # NOT cover, so automatic gen-2 sweeps re-grow expensive over the following days
        # (confirmed via prod [GC-AUTO] pauses recurring even after the startup freeze —
        # Issue 3, 2026-08-08). Fix: (a) gc.unfreeze() + a real full gc.collect() here actually
        # frees any genuine reference cycles that only a full sweep catches — the per-cycle
        # gc.collect(1) intentionally skips gen-2 every time, so those need SOME real collection
        # point or they'd become permanent floating garbage once re-frozen; then (b) re-freezing
        # folds today's legitimate CACHE growth back into the permanent generation, shrinking
        # what tomorrow's automatic sweeps need to walk. Runs during this maintenance window
        # (db_maintenance_mode=True, Discord commands already blocked) since a real full collect
        # over the whole heap costs the same multi-second price the per-cycle scoping exists to
        # avoid during live cycles. asyncio.to_thread() here only keeps the Discord heartbeat
        # *task* schedulable while it runs (Pitfall 16) — best-effort, never fails maintenance.
        try:
            def _nightly_gc_refresh() -> tuple[int, float]:
                _gc_t0 = time.perf_counter()
                gc.unfreeze()
                collected = gc.collect()
                gc.freeze()
                return collected, time.perf_counter() - _gc_t0
            _gc_collected, _gc_elapsed = await asyncio.to_thread(_nightly_gc_refresh)
            logging.info(
                "[NIGHTLY-MAINTENANCE] GC refresh: collected=%d unreachable object(s) in %.3fs, "
                "re-froze %d object(s) into the permanent generation",
                _gc_collected, _gc_elapsed, gc.get_freeze_count(),
            )
        except Exception as _gc_ex:
            logging.warning(f"[NIGHTLY-MAINTENANCE] GC refresh failed (non-fatal): {_gc_ex}")

        return _result
    finally:
        _elapsed = time.monotonic() - _maint_t0
        QBcore.nightly_maintenance_durations.append(_elapsed)
        logging.info(
            "[NIGHTLY-MAINTENANCE] END — total duration %.1fs",
            _elapsed,
        )


async def is_monthly_migration_due(ignore_in_process_claim: bool = False) -> bool:
    """
    Check whether the monthly hot->history DB migration is due (not yet
    fully completed for the current calendar month), hydrating
    CACHE.last_history_migration from bot_metadata on first use.

    Default behavior (``ignore_in_process_claim=False``, used by the scheduled
    03:00 UTC nightly task and the standalone safety-net path in
    ``periodic_main()``): gated on ``day == 1`` AND immediately CLAIMS it by
    setting ``CACHE.last_history_migration`` to now the moment it returns
    True — the caller is expected to actually run ``monthly_history_migration()``
    right after. This prevents those two automatic paths from double-running
    the migration if both happen to check within the same window, and stops
    them from re-firing every cycle for the rest of the month once claimed.

    ``ignore_in_process_claim=True`` (added 2026-08-01, used by the manual
    ``/admin`` Execute Nightly Maintenance command): bypasses BOTH the
    ``day == 1`` gate and the in-memory claim, re-deriving purely from
    persisted ``bot_metadata``. Needed because the time-budgeted/chunked
    migration (see ``monthly_history_migration``'s ``time_budget_seconds``)
    can legitimately take several separate runs across multiple days to
    finish a large backlog — an operator explicitly invoking ``/admin``
    should always be able to trigger "one more chunk" regardless of what
    already auto-fired earlier in this same bot process, or what day it is.
    The automatic paths deliberately keep the once-per-process-per-month
    behavior so they don't loop unsupervised — only a human explicitly asking
    for another run bypasses that.

    Returns:
        True if the caller should run db_manager.monthly_history_migration() now.
    """
    if CACHE.db_manager is None:
        return False
    _now_utc = datetime.now(timezone.utc)

    if ignore_in_process_claim:
        _stored_mig = await CACHE.db_manager.get_bot_metadata("last_history_migration")
        _last_done: Optional[datetime] = None
        if _stored_mig:
            try:
                _last_done = datetime.fromisoformat(_stored_mig)
            except ValueError:
                pass
        return (
            _last_done is None
            or _last_done.year != _now_utc.year
            or _last_done.month != _now_utc.month
        )

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
                # CWL Guests board hover-stats cache (2026-08-17, CWL_PROD_PERFORMANCE_FIX_PLAN.md
                # P1 Step 7) — cleared once per cycle so a just-finished war round's stats show up
                # within one cycle instead of waiting out the full 15-minute TTL.
                from qapbot.web_bridge import clear_player_stats_cache
                clear_player_stats_cache()
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
                except Exception as _trim_ex:
                    # Expected/routine on non-Linux (no libc.so.6, e.g. Windows dev env) —
                    # DEBUG, not a real error. Logged (not silently swallowed) so a genuine
                    # Linux-prod failure is still visible if malloc_trim itself starts erroring.
                    logging.debug(f"[CYCLE-CLEANUP] malloc_trim(0) skipped: {_trim_ex}")
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

                QBcore.spawn_tracked("deferred-optimize-db", _deferred_optimize_task())

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
                # Due-check uses the UTC calendar date, not "elapsed > 20h", because
                # last_db_maintenance is shared with manual /admin Optimize DB /
                # Execute Nightly Maintenance runs (nightly_db_maintenance() persists
                # it unconditionally — see db_manager.py). A rolling-hours window meant
                # a manual/test run any time after ~07:00 UTC, followed by a same-day
                # restart (which re-hydrates from that fresh bot_metadata value), would
                # silently suppress that night's 03:00 UTC automatic run for up to 20h
                # (confirmed in prod 2026-08-09: a 17:16 UTC manual test run + evening
                # redeploy blocked the same night's automatic maintenance). Comparing
                # calendar dates instead only skips when maintenance (of any origin)
                # has already run *today*, which still covers the original restart-
                # within-the-same-window case while no longer being sensitive to what
                # time of day an unrelated manual run happened.
                _maint_due = (
                    _now_utc.hour == 3
                    and (
                        CACHE.last_db_maintenance is None
                        or CACHE.last_db_maintenance.date() != _now_utc.date()
                    )
                )
                # --- Monthly hot->history DB migration ---
                # Historically ran only on day == 1 of the month, once per bot-process
                # lifetime (via is_monthly_migration_due()'s in-memory claim). Changed
                # 2026-08-01 (same incident as the WAL-growth/time-budget fixes above):
                # a large backlog can take several separate time-budgeted runs to finish,
                # and a bot left running continuously (no restart) would otherwise only
                # ever fire the migration once, then silently never again until next
                # month's day 1. _migration_due here uses ignore_in_process_claim=True —
                # re-derives fresh from bot_metadata every cycle, no day-of-month
                # restriction. Two consumers below with different cadences: the
                # `if _maint_due` branch is itself throttled to once/night (hour==3 AND
                # >20h since last run); the `elif` branch (opportunistic per-cycle chunk)
                # is NOT further throttled here — it fires every cycle the migration is
                # still due, bounded per-firing by history_migration_cycle_chunk_minutes.
                _migration_due = await is_monthly_migration_due(ignore_in_process_claim=True)
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

                    QBcore.spawn_tracked("nightly-db-maintenance", _nightly_maintenance_task())
                elif (
                    _migration_due
                    and CACHE.db_manager is not None
                    and CONFIG.history_migration_cycle_chunk_minutes > 0
                ):
                    # Opportunistic per-cycle migration chunk — added 2026-08-01, same
                    # incident as everything else above. Rather than only advancing the
                    # migration once/night (or relying on a rare "the nightly window didn't
                    # fire" safety net), spend up to history_migration_cycle_chunk_minutes of
                    # the sleep phase's otherwise-idle time on migration whenever it's still
                    # due — reuses the exact same asyncio.create_task() +
                    # db_maintenance_idle_event gate machinery the sleep-wait below already
                    # has, so the next cycle simply waits if this chunk runs long, same as it
                    # already does for nightly maintenance. Uses the same
                    # ignore_in_process_claim=True check as the block above (re-derives fresh
                    # from bot_metadata every cycle, no day-of-month restriction), so this
                    # supersedes the old once-per-process "safety net" entirely — it's now a
                    # strict superset (fires on any day, any time, every cycle, not just as a
                    # rare fallback). Self-limiting: once is_monthly_migration_due() reports
                    # done, this branch's condition is simply False and costs nothing.
                    _db_mgr2 = CACHE.db_manager

                    async def _cycle_migration_chunk_task() -> None:
                        QBcore.db_maintenance_idle_event.clear()
                        try:
                            await _db_mgr2.monthly_history_migration(
                                time_budget_seconds=CONFIG.history_migration_cycle_chunk_minutes * 60
                            )
                        finally:
                            QBcore.db_maintenance_idle_event.set()

                    QBcore.spawn_tracked("cycle-migration-chunk", _cycle_migration_chunk_task())

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
        QBdiscordcmds.highlightme,  # type: ignore[misc]
        QBdiscordcmds.help,  # type: ignore[misc]
        QBdiscordcmds.subscriptions,  # type: ignore[misc]
        QBdiscordcmds.status,  # type: ignore[misc]
        QBdiscordcmds.ping,  # type: ignore[misc]
        QBdiscordcmds.list,  # type: ignore[misc]
        QBdiscordcmds.admin,  # type: ignore[misc]
        QBdiscordcmds.whois_slash,  # type: ignore[misc]
    ]

    # Bug/feature tracker (BUG_FEATURE_TRACKER_PLAN.md §3.1) — PROD-only, no env var: DEV must
    # never register these commands. A copy of PROD's DB onto DEV (routine for realistic-data
    # testing) carries PROD's real tracker channel IDs (bot_settings) along with it, so an
    # independently-configurable toggle would let DEV post real-looking items into PROD's
    # actual channels. CONFIG.tracker_enabled is always `not is_dev_mode` (qapbot/config.py).
    if CONFIG.tracker_enabled:
        COMMANDS.append(QBdiscordcmds.bug)  # type: ignore[misc]
        COMMANDS.append(QBdiscordcmds.feature)  # type: ignore[misc]

    # Command groups
    COMMAND_GROUPS = [
        QBdiscordcmds.clan_group,
        QBdiscordcmds.analyse_group,
        QBdiscordcmds.cwl_group,
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
            payload = [cmd.to_dict(QBcore.bot.tree) for cmd in QBcore.bot.tree.get_commands(guild=None)]
            return await bulk_sync_global_commands(QBcore.bot, payload)

        await discord_retry(
            _sync_global_commands,
            "global_command_sync"
        )
        logging.info(f"[SETUP_HOOK] Successfully registered {len(COMMANDS)} commands, {len(COMMAND_GROUPS)} command groups, and {len(CONTEXT_MENUS)} context menus globally")
    
    # Register the generic RegistrationView so buttons on registration messages posted
    # BEFORE this process started keep working immediately after a restart, instead of
    # being dead until the next repost cycle re-attaches a fresh view. Requires
    # timeout=None + stable custom_ids on every button (both already the case); the
    # guild-less instance resolves guild context per-click from the interaction.
    from qapbot.ui_registration import RegistrationView
    QBcore.bot.add_view(RegistrationView())
    logging.info("[SETUP_HOOK] Registered persistent RegistrationView for restart-surviving buttons")

    # CWL Management Hub message (CWL_ROSTER_PLANNING_PLAN.md Phase 1) — same restart-survival
    # reasoning as RegistrationView above: a single generic dispatch instance resolves guild
    # context per-click rather than being constructed per-guild.
    from qapbot.ui_cwl_roster import CwlManagementHubView
    QBcore.bot.add_view(CwlManagementHubView())
    logging.info("[SETUP_HOOK] Registered persistent CwlManagementHubView for restart-surviving buttons")

    # Player CWL Settings Hub message (plans/cwl-personal-hub.md Phase 5b) — player-facing
    # counterpart to CwlManagementHubView above, same restart-survival reasoning.
    from qapbot.ui_cwl_roster import CwlPlayerHubView
    QBcore.bot.add_view(CwlPlayerHubView())
    logging.info("[SETUP_HOOK] Registered persistent CwlPlayerHubView for restart-surviving buttons")

    # CWL template-copy DM confirm/opt-out buttons (CWL_ROSTER_PLANNING_PLAN.md Phase 2) — a
    # DynamicItem, not add_view(): the custom_id itself carries the per-DM state (action/
    # event_id/player_tag), so a bot restart between sending the DM and a member clicking it
    # still resolves correctly instead of leaving a dead button until the next repost cycle
    # (there is no repost cycle for a DM anyway, unlike the anchored messages above).
    from qapbot.ui_cwl_roster import CwlSignupResponseButton
    QBcore.bot.add_dynamic_items(CwlSignupResponseButton)
    logging.info("[SETUP_HOOK] Registered persistent CwlSignupResponseButton dynamic item for restart-surviving DM buttons")

    # "Remind Pending" combined-message confirm/decline buttons (tracker #0038) — own custom_id
    # namespace (cwl:remind:... vs. cwl:signup:... above), same restart-survival reasoning.
    from qapbot.ui_cwl_roster import CwlReminderResponseButton
    QBcore.bot.add_dynamic_items(CwlReminderResponseButton)
    logging.info("[SETUP_HOOK] Registered persistent CwlReminderResponseButton dynamic item for restart-surviving DM buttons")

    # Bug/feature tracker item buttons (BUG_FEATURE_TRACKER_PLAN.md Phase 3/5) — DynamicItems so
    # a bot restart between posting an item/test-case message and a click still resolves
    # correctly. Registered unconditionally: genuinely harmless on DEV here, unlike the
    # on_raw_reaction_add listener below (2026-08-22 live bug report) — a button click is a
    # component INTERACTION, which Discord delivers only to the bot application that actually
    # owns the message, so DEV's own copy of these DynamicItems is simply never invoked for a
    # PROD-authored message no matter what DEV's own tracker DB contains.
    from qapbot.ui_tracker import TrackerItemButton, TrackerTestPassButton, TrackerTestFailButton, TrackerTestMoveDoneButton
    QBcore.bot.add_dynamic_items(TrackerItemButton, TrackerTestPassButton, TrackerTestFailButton, TrackerTestMoveDoneButton)
    logging.info("[SETUP_HOOK] Registered persistent tracker item/test-case dynamic items for restart-surviving buttons")

    # CWL clan-config web bridge (CWL_CLAN_CONFIG_ACTIVITY_PLAN.md Phase B) — no-ops unless
    # WEB_BRIDGE_PORT/WEB_BRIDGE_SECRET are both configured, so this is a no-op for any
    # deployment that hasn't opted into the Discord Activity feature.
    from qapbot.web_bridge import start_web_bridge
    await start_web_bridge()

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
            lambda: bulk_sync_global_commands(QBcore.bot, []),
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
    # Prevent multiple initializations from rapid reconnections. QBcore.on_ready_lock is the
    # actual correctness guarantee (see its docstring) — this non-blocking peek only preserves
    # the existing "skip duplicate, log and return" behavior instead of queuing the second
    # invocation behind the first.
    if QBcore.on_ready_lock.locked():
        logging.info("Bot initialization already in progress, skipping duplicate on_ready")
        return

    logging.debug("on_ready: Checking fully_initialized...")
    if hasattr(QBcore.bot, "fully_initialized") and QBcore.bot.fully_initialized:
        logging.info("Bot already fully initialized, handling reconnection")
        # Just log the reconnection, don't reinitialize everything
        logging.info(f"Bot reconnected as {QBcore.bot.user}")
        return

    async with QBcore.on_ready_lock:
        await _run_startup_initialization()


async def _run_startup_initialization() -> None:
    """The one-time startup sequence (Steps 1-9) run by on_ready() under QBcore.on_ready_lock.

    Every early-return failure path below resets `initialization_in_progress` (or terminates the
    bot outright, for fatal steps) — a failure that left it True forever would permanently block
    all future on_ready() re-entry via the lock/flag check above, wedging the bot until a full
    process restart even after a transient error (e.g. a momentary DB or CoC-API hiccup) recovers.
    """
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

        # Step 1.5: Initialize database FIRST, strictly before CoC login and before
        # periodic_main() can possibly start. See initialize_database()'s docstring —
        # this ordering is what keeps a rare, first-run-only slow schema migration
        # (a) off the tight 60s CoC-login timeout below and (b) from ever racing with
        # live concurrent DB writes, since nothing else touches the DB yet at this point.
        try:
            await initialize_database()
        except Exception as e:
            logging.error(f"❌ Error during database initialization: {e}")
            QBcore.bot.initialization_in_progress = False
            return

        # Step 2: Authenticate with CoC API (database already initialized above)
        if CONFIG.no_coc_api:
            logging.info("🔐 [NO_COC_API] Skipping CoC API authentication...")
            try:
                await asyncio.wait_for(startup_login(), timeout=60.0)
            except Exception as e:
                logging.error(f"❌ Error during startup: {e}")
                QBcore.bot.initialization_in_progress = False
                return
        else:
            logging.info("🔐 Authenticating with Clash of Clans API...")
            try:
                await asyncio.wait_for(startup_login(), timeout=60.0)
                logging.info("✅ CoC API authentication completed")
            except asyncio.TimeoutError:
                logging.error("❌ CoC API login timed out after 60 seconds")
                logging.error("🛑 Cannot continue without CoC API access")
                QBcore.bot.initialization_in_progress = False
                return
            except Exception as e:
                logging.error(f"❌ Error during CoC API login: {e}")
                logging.error("🛑 Cannot continue without CoC API access")
                QBcore.bot.initialization_in_progress = False
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

            # Raise the gen-2 collection threshold (2026-08-17, CWL_PROD_PERFORMANCE_FIX_PLAN.md
            # P2 Step 10 — mitigation, not the fix; P0 already removed the actual allocation
            # source behind the 2026-08-16 PROD meltdown's escalating gen-2 [GC-AUTO] pauses,
            # 1.5-5.5s each). Default thresholds are (700, 10, 10): a gen-2 sweep runs once gen-1
            # has been collected 10 times since the last gen-2 sweep. Raising just the gen-2
            # multiplier to 20 (gen-0/gen-1 left at their defaults — those stay cheap regardless
            # of heap size) halves how often automatic gen-2 sweeps run, without changing gen-0/
            # gen-1's own frequent, cheap collections at all. Set once, here, before the
            # gc.freeze() below — threshold and freeze state are independent (thresholds gate
            # WHEN a sweep runs; freezing controls WHAT a sweep walks), so order between the two
            # doesn't matter, but co-locating them keeps every startup GC decision in one place.
            gc.set_threshold(700, 10, 20)

            # Freeze the just-loaded CACHE state into gc's permanent generation.
            # CPython's automatic collector is never disabled in this codebase
            # (see the [GC-AUTO] logger registered near the top of this file) and
            # runs real gen-2 sweeps on its own allocation-threshold schedule —
            # including mid-cycle, during Phase-1's concurrent API-response churn.
            # A gen-2 sweep walks every tracked object; without freezing, that
            # includes the hundreds of thousands to millions of long-lived CACHE
            # objects (clan_name_cache, coc_clan_cache, etc.) just loaded above,
            # which is what turns an automatic collection into a multi-second
            # process-wide freeze. gc.freeze() moves everything currently tracked
            # into the permanent generation, which automatic collections skip
            # entirely — so future sweeps only walk new (post-startup) churn, not
            # this baseline. A one-time full gc.collect() first ensures we don't
            # freeze in any startup-only garbage. Both are atomic C calls (see
            # Pitfall 16), so they're offloaded to a thread purely to keep
            # discord.py's heartbeat task schedulable while they run — not because
            # that makes them non-blocking.
            def _freeze_startup_heap() -> int:
                gc.collect()
                gc.freeze()
                return gc.get_freeze_count()
            _frozen_count = await asyncio.to_thread(_freeze_startup_heap)
            logging.info(f"[GC-FREEZE] Froze {_frozen_count:,} startup object(s) into the permanent generation")

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
            # Fatal, same as a Step-3 cache-load failure: without periodic_main() the bot can
            # never fetch war data or run any update cycle, so there is nothing to recover into —
            # a "success" reconnect that left the bot connected but running zero cycles would look
            # healthy in Discord while being silently non-functional. Terminate the same way Step 3
            # does rather than leaving a half-alive process for a process supervisor to notice late.
            logging.critical(f"💥 Failed to start periodic task: {e} — bot cannot function")
            logging.critical("🛑 Terminating bot due to failed periodic-task startup")
            QBcore.bot.initialization_in_progress = False
            await async_cleanup()
            await QBcore.bot.close()
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

        # Step 8b: Repost CWL Management Hub messages on startup
        logging.info("[INIT-STEP-8b] Starting CWL Management Hub message repost...")
        try:
            await asyncio.wait_for(repost_cwl_management_messages(), timeout=30.0)
            logging.info("✅ CWL Management Hub messages reposted successfully")
        except asyncio.TimeoutError:
            logging.warning("⚠️ Reposting CWL Management Hub messages timed out after 30 seconds (continuing)")
        except Exception as e:
            logging.error(f"❌ Failed to repost CWL Management Hub messages: {e}")
        logging.info("[INIT-STEP-8b] Done")

        # Step 8c: Repost Player CWL Settings Hub messages on startup (plans/cwl-personal-hub.md
        # Phase 3) — same unconditional (only_if_not_bottom defaults False) startup repost as
        # Step 8b just above, which is also what makes the cwl_admin_hub_* custom_id rename
        # (Phase 2d) self-healing on every real deploy with no separate repair step.
        logging.info("[INIT-STEP-8c] Starting Player CWL Settings Hub message repost...")
        try:
            await asyncio.wait_for(repost_cwl_player_hub_messages(), timeout=30.0)
            logging.info("✅ Player CWL Settings Hub messages reposted successfully")
        except asyncio.TimeoutError:
            logging.warning("⚠️ Reposting Player CWL Settings Hub messages timed out after 30 seconds (continuing)")
        except Exception as e:
            logging.error(f"❌ Failed to repost Player CWL Settings Hub messages: {e}")
        logging.info("[INIT-STEP-8c] Done")

        # Step 9: Finalize initialization
        QBcore.bot.fully_initialized = True
        QBcore.bot.initialization_in_progress = False

        # Warm the /status global-DB-statistics cache in the background (25h
        # TTL — see get_global_db_statistics_sync) so the first /status call
        # after a restart doesn't pay the multi-GB cold full-table-scan cost.
        # Fire-and-forget: must not delay "fully_initialized" or on_ready.
        QBcore.spawn_tracked("warm-global-db-stats", _warm_global_db_stats_cache())

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

    # Tracker "Reply to requestor" pending access (ticket #0021 follow-up, 2026-08-22): finish
    # granting channel access to a reporter who was invited via DM while not yet a guild member.
    # apply_pending_requestor_access() gates on CONFIG.tracker_enabled itself (see its own
    # docstring) — this is a raw gateway event, fired on every bot in the guild including DEV.
    try:
        from qapbot.ui_tracker import apply_pending_requestor_access
        await apply_pending_requestor_access(member)
    except Exception as e:
        logging.error(f"[TRACKER] Failed to apply pending requestor access for {member.id}: {e}")

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
                    f"**{CACHE.get_clan_name(tag, tag)}**: <{coc_clan_profile_url(tag)}>"
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


@QBcore.bot.event
async def on_message(message: discord.Message) -> None:
    """
    Event handler for messages sent directly to the bot (DMs).

    Fulfils the long-standing backlog item ("Add on_message handler for DM so that
    bot can reply to direct chat input from users") as the first slice of the CWL
    roster planning feature's Phase 0 (DM interaction foundation) — see
    CWL_ROSTER_PLANNING_PLAN.md. Deliberately narrow: not a chatbot, no NLU/intent
    parsing, just a pointer back to /help for free text sent directly to the bot in
    a DM. Slash commands typed from DMs go through normal application-command
    dispatch, not this handler.
    """
    # Bug/feature tracker upload window (BUG_FEATURE_TRACKER_PLAN.md §5.4) — must run BEFORE
    # the DM fallback below: a file uploaded in a DM during an open window belongs to the
    # tracker, not the generic "use /help" reply. Directly edited into this handler (not a
    # second @QBcore.bot.event) since a second on_message registration would silently replace
    # this one instead of adding to it (discord.py's Client.event() does plain setattr).
    from qapbot.ui_tracker import handle_tracker_upload_message
    if await handle_tracker_upload_message(message):
        return

    if not message.author.bot and message.guild is None:
        from qapbot.i18n import t

        logging.debug(f"[DM] Received DM from user {message.author.id}: {message.content[:80]!r}")
        try:
            await message.channel.send(t('commands.dm.fallback_reply', user_id=str(message.author.id)))
        except discord.Forbidden:
            logging.warning(f"[DM] Could not reply to DM from user {message.author.id} — forbidden (blocked/closed DMs)")
        except Exception as e:
            logging.error(f"[DM] Failed to reply to DM from user {message.author.id}: {e}")

    # Required when overriding on_message on a commands.Bot subclass to keep prefix-command
    # dispatch working. No @QBcore.bot.command() handlers exist today (slash commands only),
    # so this is currently a no-op — kept for correctness per discord.py's documented contract,
    # and as a safety net if a prefix command is ever added.
    await QBcore.bot.process_commands(message)


@QBcore.bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
    """Bug/feature tracker's 👍 test-case sign-off shortcut (BUG_FEATURE_TRACKER_PLAN.md §2.4,
    §5.4) — the bot had no on_raw_reaction_add listener before this. Raw (not cached) so it
    keeps working after a restart. Delegates entirely to handle_tracker_test_reaction(), which
    no-ops for anything that isn't 👍 on a known test-case message from the bot admin —
    including, as of 2026-08-22, on DEV: a raw reaction event fires for every bot present in a
    channel regardless of which bot's message was reacted to (unlike component interactions,
    which Discord routes only to the owning application), and DEV's tracker DB is periodically
    seeded from a PROD backup, so it isn't the harmless no-op it looks like without that gate —
    see handle_tracker_test_reaction()'s own docstring for the live incident this fixed.
    """
    from qapbot.ui_tracker import handle_tracker_test_reaction
    try:
        await handle_tracker_test_reaction(payload)
    except Exception as e:
        logging.error(f"[TRACKER] on_raw_reaction_add handler failed: {e}", exc_info=True)


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
        
        # Get channel. get_channel() is a local-cache lookup, not proof of deletion (a cache
        # miss can happen on a startup gateway-chunking gap) — confirm via fetch_channel
        # before purging the cache entry, and only purge on a confirmed discord.NotFound.
        channel = QBcore.bot.get_channel(int(channel_id))
        if not channel:
            try:
                channel = await QBcore.bot.fetch_channel(int(channel_id))
            except discord.NotFound:
                logging.debug(f"Channel {channel_id} confirmed deleted for message cleanup (key: {key})")
                await CACHE.delete_leaderboard_message(key)
                continue
            except Exception as e:
                logging.debug(f"Could not confirm channel {channel_id} is deleted ({e}); leaving cache entry for retry next restart (key: {key})")
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

async def _get_channel_last_message_id(channel: discord.TextChannel) -> Optional[int]:
    """Return the newest message ID in `channel`, or None on failure/empty channel."""
    try:
        async for msg in channel.history(limit=1, oldest_first=False):
            return msg.id
    except Exception:
        return None
    return None


async def _confirm_delete_message_from_channel(channel_id: int, message_id: str, *, log_label: str = "anchored") -> bool:
    """Best-effort delete of a tracked anchored message (registration, future CWL hubs, etc.).

    Returns True only when Discord has *confirmed* the message (or its channel) no
    longer exists — i.e. it's safe for the caller to drop tracking. `bot.get_channel()`
    is a local-cache lookup, not proof of deletion, so a cache miss falls back to a live
    `fetch_channel()` call. Any inconclusive outcome (Forbidden, HTTPException, network
    error) returns False so the caller keeps tracking and retries next cycle instead of
    losing the message ID while a live message may still be sitting in the channel
    (that's how the old/new message pair turns into a visible duplicate — see
    qapbot/docs/COPILOT_PITFALLS_COOKBOOK.md Pitfall 15).
    """
    try:
        channel = QBcore.bot.get_channel(channel_id)
        if not channel:
            try:
                channel = await QBcore.bot.fetch_channel(channel_id)
            except discord.NotFound:
                return True  # Channel itself confirmed gone.
            except Exception as e:
                logging.debug(f"Could not confirm channel {channel_id} is gone: {e}")
                return False
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return False
        message = await channel.fetch_message(int(message_id))
        await message.delete()
        logging.debug(f"Deleted {log_label} message {message_id} from channel {channel_id}")
        return True
    except discord.NotFound:
        return True  # Message (or its channel) already gone.
    except Exception as e:
        logging.debug(f"Could not delete {log_label} message {message_id} from channel {channel_id}: {e}")
        return False


async def repost_anchored_message(
    *,
    log_label: str,
    enabled_key: str,
    channel_key: str,
    message_id_key: str,
    old_channel_key: str,
    last_bump_key: str,
    build_content_and_view: Callable[[Any, int], Any],
    dev_mode_allowed_channel_id: Optional[int] = None,
    only_if_not_bottom: bool = False,
    bump_cooldown_seconds: int = 300,
    guild_id: Optional[int] = None,
) -> Tuple[int, int]:
    """
    Generic anchored-message lifecycle shared by every persistent, per-guild tracked message
    (registration; the CWL Management/Personal Hub messages reuse this instead of a copy).

    Handles: posting/reposting, deleting when disabled, following a channel change, and never
    risking a duplicate on an inconclusive delete (see COPILOT_PITFALLS_COOKBOOK.md Pitfall 15).
    All per-feature specifics (which config keys, what content/view to post) are passed in —
    this function itself has no registration-specific knowledge.

    Args:
        log_label: short name used in log lines (e.g. "registration").
        enabled_key/channel_key/message_id_key/old_channel_key/last_bump_key: the
            CACHE.server_config[guild_id] keys this feature uses for its anchored message.
        build_content_and_view: async callback (channel, guild_id_int) -> (content, view, embed)
            returning what to post/repost. embed may be None (registration posts plain content
            only); callers whose anchored message shows live embed-based state (e.g. the CWL
            Management Hub) pass a real discord.Embed here instead.
        dev_mode_allowed_channel_id: in DEV mode, skip all channels except this one
            (None/0 = no DEV-mode filtering).
        only_if_not_bottom: if True, only repost when the tracked message is NOT the newest
            message in the channel (rate-limited by bump_cooldown_seconds).
        guild_id: if given, process ONLY this one guild instead of sweeping every guild in
            CACHE.server_config (tracker #0061: a single guild's settings change — toggling the
            hub on/off, changing its channel, changing the guild's language — must not delete and
            repost every OTHER enabled guild's anchored message too). Periodic/startup callers
            (main()'s bump cycle, on_ready()) omit this to keep sweeping the whole fleet, which is
            their actual job.

    Returns:
        (posted_count, filtered_count) — filtered_count only ever increments in DEV mode.
    """
    server_config = CACHE.server_config
    now_utc = datetime.now(timezone.utc)
    posted_count = 0
    filtered_count = 0

    if guild_id is not None:
        guild_id_str_filter = str(guild_id)
        guilds_to_process = (
            [(guild_id_str_filter, server_config[guild_id_str_filter])]
            if guild_id_str_filter in server_config else []
        )
        logging.debug(f"repost_anchored_message({log_label}) called, processing single guild {guild_id}")
    else:
        guilds_to_process = list(server_config.items())
        logging.debug(f"repost_anchored_message({log_label}) called, processing {len(guilds_to_process)} guilds")

    for guild_id_str, config in guilds_to_process:
        try:
            guild_id_int = int(guild_id_str)
        except (ValueError, TypeError):
            continue

        message_enabled = config.get(enabled_key, False)
        channel_id = config.get(channel_key)
        tracked_message_id = str(config.get(message_id_key) or "").strip()
        old_channel_id = config.get(old_channel_key)  # Track if channel changed

        # Case 1: message disabled - delete if it exists
        if not message_enabled:
            if tracked_message_id:
                # Determine which channel to delete from (prefer current, fall back to old)
                channel_to_delete_from = channel_id if channel_id else old_channel_id

                deletion_confirmed = True
                if channel_to_delete_from:
                    deletion_confirmed = await _confirm_delete_message_from_channel(
                        int(channel_to_delete_from), tracked_message_id, log_label=log_label
                    )

                if deletion_confirmed:
                    # Clear the message ID and tracking
                    config[message_id_key] = None
                    config[old_channel_key] = None
                    server_config[guild_id_str] = config
                    CACHE.server_config = server_config
                    await CACHE.persist_server_config(guild_id_str)
                    logging.info(f"Deleted {log_label} message from guild {guild_id_int} (disabled)")
                else:
                    # Deletion outcome was inconclusive (rate limit/network/permissions) — keep
                    # tracking so we retry next cycle instead of losing the message ID while a
                    # live message may still be sitting in the channel.
                    logging.warning(f"Could not confirm deletion of {log_label} message for guild {guild_id_int}; will retry next cycle")
            continue

        # Case 2: message enabled - post/update
        if not channel_id:
            continue

        # Check if channel ID changed
        if old_channel_id:
            logging.debug(f"Channel change detected for {log_label}: old={old_channel_id}, new={channel_id}")
            try:
                old_id_int = int(old_channel_id) if isinstance(old_channel_id, str) else old_channel_id
                new_id_int = int(channel_id) if isinstance(channel_id, str) else channel_id

                if old_id_int != new_id_int:
                    # Channel was actually changed - delete from old channel
                    logging.debug(f"Channel mismatch confirmed: {old_id_int} != {new_id_int}")
                    old_deletion_confirmed = True
                    if tracked_message_id:
                        old_deletion_confirmed = await _confirm_delete_message_from_channel(
                            old_id_int, tracked_message_id, log_label=log_label
                        )
                        if old_deletion_confirmed:
                            logging.info(f"Deleted {log_label} message from old channel {old_channel_id} (guild {guild_id_int})")
                        else:
                            logging.warning(f"Could not confirm deletion of {log_label} message from old channel {old_channel_id} (guild {guild_id_int}); will retry next cycle")
                    if old_deletion_confirmed:
                        # Clear the old channel tracking
                        config[old_channel_key] = None
                        # Clear message ID so it will be reposted to the new channel
                        config[message_id_key] = None
                        tracked_message_id = ""
                        # Save config after clearing old tracking info
                        server_config[guild_id_str] = config
                        CACHE.server_config = server_config
                        await CACHE.persist_server_config(guild_id_str)
                        logging.debug(f"Cleared old channel tracking for guild {guild_id_int} ({log_label})")
            except (ValueError, TypeError) as e:
                logging.warning(f"Failed to compare channel IDs for guild {guild_id_int} ({log_label}): {e}")

        # DEV MODE OVERRIDE:
        # In DEV mode, skip all channels except the explicitly allowed one.
        # Filter BEFORE trying to fetch channel to avoid "Could not find channel" warnings for other guilds.
        # If dev_mode_allowed_channel_id is 0/None (not configured), allow all channels in the DEV guild.
        if CONFIG.is_dev_mode and dev_mode_allowed_channel_id and int(channel_id) != dev_mode_allowed_channel_id:
            filtered_count += 1
            continue

        # Get the Discord channel
        channel = QBcore.bot.get_channel(int(channel_id))
        if not channel:
            logging.warning(f"Could not find {log_label} channel {channel_id} for guild {guild_id_int}")
            continue

        # Type guard - only process text-based channels that support views
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            logging.warning(f"{log_label.capitalize()} channel {channel_id} for guild {guild_id_int} is not a text-based channel")
            continue

        if only_if_not_bottom:
            # Cooldown gate (stored in the config)
            last_bump_iso = config.get(last_bump_key)
            if isinstance(last_bump_iso, str) and last_bump_iso:
                try:
                    last_bump_dt = datetime.fromisoformat(last_bump_iso.replace("Z", "+00:00"))
                    if (now_utc - last_bump_dt).total_seconds() < bump_cooldown_seconds:
                        continue
                except Exception:
                    pass

            # Bottom-most check: if tracked message is newest, do nothing
            if tracked_message_id:
                # Type guard for _get_channel_last_message_id (requires TextChannel)
                if isinstance(channel, discord.TextChannel):
                    last_id = await _get_channel_last_message_id(channel)
                    if last_id is not None and str(last_id) == tracked_message_id:
                        continue

        # Delete old anchored message if it exists. Only proceed to post a new one once
        # deletion is confirmed (discord.NotFound = already gone, fine) — an inconclusive
        # failure (rate limit, network blip, permissions) must NOT fall through to posting a
        # new message on top of a possibly-still-live old one, since that's exactly how the
        # old+new pair shows up as a visible duplicate message.
        if tracked_message_id:
            try:
                old_message = await channel.fetch_message(int(tracked_message_id))
                await old_message.delete()
                logging.debug(f"Deleted old {log_label} message {tracked_message_id} in channel {channel_id}")
            except discord.NotFound:
                # Message already gone (manually deleted, or never existed in this channel).
                pass
            except Exception as e:
                logging.warning(f"Could not confirm deletion of old {log_label} message {tracked_message_id} in channel {channel_id} ({e}); skipping repost this cycle to avoid a duplicate.")
                continue

        content, view, embed = await build_content_and_view(channel, guild_id_int)

        try:
            # Post new anchored message
            new_message = await channel.send(content, view=view, embed=embed)

            # Update server config with new message ID
            config[message_id_key] = str(new_message.id)
            config[last_bump_key] = now_utc.isoformat().replace("+00:00", "Z")
            config[old_channel_key] = None  # Clear old channel tracking after successful post
            server_config[guild_id_str] = config
            CACHE.server_config = server_config
            await CACHE.persist_server_config(guild_id_str)
            posted_count += 1

            channel_name = getattr(channel, 'name', 'Unknown')
            logging.info(f"Reposted {log_label} message in {channel.guild.name}#{channel_name} (new message ID: {new_message.id})")

        except Exception as e:
            logging.error(f"Failed to repost {log_label} message in channel {channel_id}: {e}")

    return posted_count, filtered_count


async def repost_playerregistration_messages(*, only_if_not_bottom: bool = False, bump_cooldown_seconds: int = PLAYERREGISTRATION_BUMP_COOLDOWN_SECONDS, guild_id: Optional[int] = None) -> None:
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
        guild_id: if given, only process this one guild (see repost_anchored_message()'s guild_id
            doc — tracker #0061).

    In DEV mode, only processes the configured DISCORD_GUILD_ID.

    Thin registration-specific wrapper around the generic repost_anchored_message() driver —
    only the config keys and message content/view differ per anchored-message feature.
    """
    from qapbot.ui_registration import RegistrationView
    from qapbot.QBdiscocmdshelper import get_playerregistration_message

    async def _build_registration_content_and_view(channel: Any, guild_id_int: int) -> Tuple[str, discord.ui.View, Optional[discord.Embed]]:
        server_name = channel.guild.name
        logging.debug(f"Generating registration message:")
        logging.debug(f"  - Guild: {guild_id_int} ({server_name})")
        registration_msg = get_playerregistration_message(server_name, guild_id=guild_id_int)
        logging.debug(f"Generated registration message (first 200 chars):\n{registration_msg[:200]}...")
        return registration_msg, RegistrationView(guild_id_int), None

    playerregistration_count, filtered_count = await repost_anchored_message(
        log_label="registration",
        enabled_key="registration_message_enabled",
        channel_key="registration_channel_id",
        message_id_key="registration_message_id",
        old_channel_key="_old_registration_channel_id",
        last_bump_key="registration_message_last_bump_iso",
        build_content_and_view=_build_registration_content_and_view,
        dev_mode_allowed_channel_id=CONFIG.dev_playerregistration_channel_id or None,
        only_if_not_bottom=only_if_not_bottom,
        bump_cooldown_seconds=bump_cooldown_seconds,
        guild_id=guild_id,
    )

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


async def repost_cwl_management_messages(*, only_if_not_bottom: bool = False, bump_cooldown_seconds: int = PLAYERREGISTRATION_BUMP_COOLDOWN_SECONDS, guild_id: Optional[int] = None) -> None:
    """
    Repost CWL Management Hub messages for guilds with the hub enabled
    (CWL_ROSTER_PLANNING_PLAN.md Phase 1) — the admin-facing anchored message showing the
    cwl_settings/cwl_management screens (entry point b; /clan management's own mode dropdown
    is entry point a, sharing the exact same content/component builders).

    Thin wrapper around the generic repost_anchored_message() driver, same discipline as
    repost_playerregistration_messages() above — only the config keys and message content/view
    differ per anchored-message feature.

    guild_id: if given, only process this one guild (see repost_anchored_message()'s guild_id
        doc — tracker #0061).
    """
    from qapbot.ui_cwl_roster import CwlManagementHubView, add_cwl_management_components
    from qapbot.QBdiscocmdshelper_cwl import format_clan_management_cwl_management

    async def _build_cwl_management_content_and_view(channel: Any, guild_id_int: int) -> Tuple[str, discord.ui.View, Optional[discord.Embed]]:
        guild = channel.guild
        view = CwlManagementHubView()
        embed, _, _, _ = await format_clan_management_cwl_management(guild)
        add_cwl_management_components(view, guild_id_int)
        return "", view, embed

    cwl_management_count, filtered_count = await repost_anchored_message(
        log_label="CWL Management Hub",
        enabled_key="cwl_management_message_enabled",
        channel_key="cwl_management_channel_id",
        message_id_key="cwl_management_message_id",
        old_channel_key="_old_cwl_management_channel_id",
        last_bump_key="cwl_management_message_last_bump_iso",
        build_content_and_view=_build_cwl_management_content_and_view,
        # Reuses the registration message's DEV test channel rather than requiring a second,
        # dedicated env var — both exist purely to avoid posting to real guilds' channels while
        # testing in DEV mode.
        dev_mode_allowed_channel_id=CONFIG.dev_playerregistration_channel_id or None,
        only_if_not_bottom=only_if_not_bottom,
        bump_cooldown_seconds=bump_cooldown_seconds,
        guild_id=guild_id,
    )

    if cwl_management_count > 0:
        if CONFIG.is_dev_mode and filtered_count > 0:
            logging.info(f"Successfully reposted {cwl_management_count} CWL Management Hub messages ({filtered_count} guilds filtered in DEV mode)")
        else:
            logging.info(f"Successfully reposted {cwl_management_count} CWL Management Hub messages")
    else:
        if CONFIG.is_dev_mode and filtered_count > 0:
            logging.info(f"No CWL Management Hub messages to repost in guild {CONFIG.discord_guild_id} ({filtered_count} guilds filtered)")
        else:
            logging.info("No CWL Management Hub messages to repost")

async def repost_cwl_player_hub_messages(*, only_if_not_bottom: bool = False, bump_cooldown_seconds: int = PLAYERREGISTRATION_BUMP_COOLDOWN_SECONDS, guild_id: Optional[int] = None) -> None:
    """
    Repost Player CWL Settings Hub messages for guilds with the hub enabled
    (plans/cwl-personal-hub.md Phase 3) — the player-facing anchored message, structurally
    parallel to repost_cwl_management_messages() just above (its admin-facing counterpart).

    Thin wrapper around the generic repost_anchored_message() driver, same discipline as every
    other anchored-message feature in this file — only the config keys and message
    content/view differ per feature.

    guild_id: if given, only process this one guild (see repost_anchored_message()'s guild_id
        doc — tracker #0061).
    """
    from qapbot.ui_cwl_roster import build_cwl_player_hub_content_and_view

    cwl_player_hub_count, filtered_count = await repost_anchored_message(
        log_label="Player CWL Settings Hub",
        enabled_key="cwl_player_hub_message_enabled",
        channel_key="cwl_player_hub_channel_id",
        message_id_key="cwl_player_hub_message_id",
        old_channel_key="_old_cwl_player_hub_channel_id",
        last_bump_key="cwl_player_hub_message_last_bump_iso",
        build_content_and_view=build_cwl_player_hub_content_and_view,
        # Same DEV test-channel reuse as repost_cwl_management_messages() above.
        dev_mode_allowed_channel_id=CONFIG.dev_playerregistration_channel_id or None,
        only_if_not_bottom=only_if_not_bottom,
        bump_cooldown_seconds=bump_cooldown_seconds,
        guild_id=guild_id,
    )

    if cwl_player_hub_count > 0:
        if CONFIG.is_dev_mode and filtered_count > 0:
            logging.info(f"Successfully reposted {cwl_player_hub_count} Player CWL Settings Hub messages ({filtered_count} guilds filtered in DEV mode)")
        else:
            logging.info(f"Successfully reposted {cwl_player_hub_count} Player CWL Settings Hub messages")
    else:
        if CONFIG.is_dev_mode and filtered_count > 0:
            logging.info(f"No Player CWL Settings Hub messages to repost in guild {CONFIG.discord_guild_id} ({filtered_count} guilds filtered)")
        else:
            logging.info("No Player CWL Settings Hub messages to repost")

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

    # CWL clan-config web bridge (Phase B) — no-op if it was never started
    try:
        from qapbot.web_bridge import stop_web_bridge
        await stop_web_bridge()
    except Exception as e:
        logging.error(f"[WEB-BRIDGE] Error during shutdown: {e}")

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
