"""
Core Discord bot and Clash of Clans client initialization for QapBot.

This module provides the global bot instance, CoC API client, and shutdown coordination for the entire QapBot application. It serves as the central initialization point for all shared resources and state management, ensuring cache-centric and singleton access across all modules.

Architecture:
    Implements the singleton pattern for core resources to ensure consistent access across all modules. The bot instance, CoC client, and shutdown coordination are centrally managed here.

Global Objects:
    bot: Discord bot instance with configured intents and command prefix
    coc_client: Clash of Clans API client for data retrieval operations
    shutdown_event: Asyncio event for coordinating graceful shutdown across tasks
    cleaned_up: Prevents duplicate cleanup operations during shutdown
    shutdown_initiated: Internal flag to prevent multiple shutdown sequences

Thread Safety:
    All objects in this module should only be accessed from the main asyncio event loop thread. The shutdown coordination system is designed for single-threaded access.

Integration:
    - Used by all modules for Discord command registration and CoC API access
    - Coordinates shutdown and cleanup for cache consistency
    - Ensures all business logic modules interact with the same bot and client instances

IMPORTANT - Caching:
    ⚠️ NEVER call coc_client.get_clan(clan_tag) directly!
    Always use: await CACHE.coc_clan_cache.get_clan(clan_tag)
    
    The cache provides:
        - 5-minute TTL to reduce API calls
        - Debug logging for cache hits/misses
        - Dramatic performance improvement (15s → 2s on cache hit)

Usage Examples:
    import QBcore
    # Access CoC API
    clan = await QBcore.coc_client.get_clan("#CLANTAG")
    # Add Discord commands
    QBcore.bot.add_command(my_command)
    # Coordinate shutdown
    QBcore.shutdown_event.set()
"""
import aiohttp
import discord
from discord.ext import commands
from discord import app_commands
import asyncio
from collections import deque
from datetime import datetime
from typing import Optional, Any
import coc  # type: ignore[import-untyped]

# ── Patch coc.py BattleModifier enum for soccer/bonus CWL seasons ──────────────
# Supercell introduced handicap battle modifiers (minusOne, minusTwo, minusThree)
# for the June 2026 soccer-themed CWL.  The upstream coc.py library only knows
# 'none' and 'hardMode', so parsing a war response raises ValueError and causes
# the entire war fetch to fail.  We inject the missing members at startup so the
# library handles them gracefully without requiring a library update.
def _patch_coc_battle_modifiers() -> None:
    from coc.enums import BattleModifier  # type: ignore[import-untyped]
    _new_values = {
        "minus_one":   "minusOne",
        "minus_two":   "minusTwo",
        "minus_three": "minusThree",
    }
    for _name, _val in _new_values.items():
        if _val not in BattleModifier._value2member_map_:  # type: ignore[attr-defined]
            _member = object.__new__(BattleModifier)
            _member._name_ = _name   # type: ignore[attr-defined]
            _member._value_ = _val   # type: ignore[attr-defined]
            # Use type.__setattr__ instead of setattr() to bypass EnumType.__setattr__,
            # which in Python 3.12+ raises AttributeError("cannot reassign member")
            # for any name already present in _member_map_.  We must call this BEFORE
            # inserting into _member_map_ so the guard never sees the name.
            type.__setattr__(BattleModifier, _name, _member)    # type: ignore[attr-defined]
            BattleModifier._value2member_map_[_val] = _member   # type: ignore[attr-defined]
            BattleModifier._member_map_[_name] = _member        # type: ignore[attr-defined]
            if _name not in BattleModifier._member_names_:      # type: ignore[attr-defined]
                BattleModifier._member_names_.append(_name)     # type: ignore[attr-defined]

_patch_coc_battle_modifiers()
from qapbot.constants import SECONDS_PER_HOUR
from qapbot.i18n import t  # type: ignore[attr-defined]

BOT_VERSION: str = "1.2.95"

# Discord bot configuration and initialization
intents: discord.Intents = discord.Intents.default()
intents.message_content = True  # Enable privileged message content intent for command/message access
intents.members = True  # Enable privileged members intent for guild member access
intents.presences = True  # Enable privileged presence intent for status/activity tracking

class QapBot(commands.Bot):
    """Extended Discord bot with QapBot-specific attributes for type safety."""
    start_time: Optional[datetime] = None
    last_sync: Optional[datetime] = None
    periodic_task_started: bool = False
    fully_initialized: bool = False
    initialization_in_progress: bool = False
    
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize QapBot with all required attributes."""
        super().__init__(*args, **kwargs)
        self.start_time: Optional[datetime] = None
        self.last_sync: Optional[datetime] = None
        self.periodic_task_started: bool = False
        self.fully_initialized: bool = False
        self.initialization_in_progress: bool = False

bot: QapBot = QapBot(
    command_prefix=commands.when_mentioned,  # responds only to @Bot mentions (and you won't add any)
    intents=intents,
)
tree = bot.tree  # For slash command registration

async def _maintenance_interaction_check(interaction: discord.Interaction) -> bool:
    """
    Global guard: block all slash commands while maintenance mode is active.
    Only /admin with action=MAINTENANCE_END is allowed through.

    Registered via direct assignment (bot.tree.interaction_check = ...) because
    CommandTree.interaction_check is NOT a decorator factory — using @tree.interaction_check
    as a decorator merely calls the method with the function as the interaction argument
    and discards the result, so the check would never fire.
    """
    # Block commands during startup (before on_ready finishes initializing)
    if not getattr(bot, 'fully_initialized', False):
        from qapbot.i18n import t as _t
        guild_id = interaction.guild.id if interaction.guild else None
        msg = _t('commands.errors.startup_in_progress', guild_id=guild_id)
        try:
            await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass
        return False

    if not maintenance_mode and not db_maintenance_mode:
        return True

    # During DB maintenance (VACUUM/REINDEX), block ALL commands with a short notice.
    # This is a temporary state (minutes, not hours) — no admin escape hatch needed.
    if db_maintenance_mode and not maintenance_mode:
        from qapbot.i18n import t as _t
        guild_id = interaction.guild.id if interaction.guild else None
        msg = _t('commands.errors.db_maintenance_active', guild_id=guild_id)
        try:
            await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass
        return False

    # Full maintenance mode — allow only /admin MAINTENANCE_END through
    command_name = interaction.command.name if interaction.command else ""
    if command_name == "admin":
        options = (interaction.data or {}).get("options", [])
        action_value = str(next((opt.get("value", "") for opt in options if opt.get("name") == "action"), ""))
        if action_value.upper() == "MAINTENANCE_END":
            return True
    # All other commands get a polite maintenance notice
    from qapbot.i18n import t as _t
    guild_id = interaction.guild.id if interaction.guild else None
    msg = _t('commands.errors.maintenance_mode_active', guild_id=guild_id)
    try:
        await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass
    return False

# Register via direct assignment — NOT as a decorator (see docstring above)
bot.tree.interaction_check = _maintenance_interaction_check

# ---------------------------------------------------------------------------
# Maintenance guard for View & Modal component interactions
# ---------------------------------------------------------------------------
# bot.tree.interaction_check only fires for slash-commands.  Component
# interactions (buttons, selects, modals) are dispatched directly to View /
# Modal callbacks whose interaction_check defaults to ``return True``.
# Monkey-patching the default on the base classes ensures every view/modal
# that does NOT override interaction_check still gets the maintenance guard.
# Views that DO override it must call ``await super().interaction_check(...)``
# or check maintenance themselves.

_original_view_interaction_check = discord.ui.View.interaction_check
_original_modal_interaction_check = discord.ui.Modal.interaction_check


async def _maintenance_view_interaction_check(
    self: discord.ui.View, interaction: discord.Interaction
) -> bool:
    from qapbot.ui_common import check_maintenance_block
    if await check_maintenance_block(interaction):
        return False
    return bool(await _original_view_interaction_check(self, interaction))


async def _maintenance_modal_interaction_check(
    self: discord.ui.Modal, interaction: discord.Interaction
) -> bool:
    from qapbot.ui_common import check_maintenance_block
    if await check_maintenance_block(interaction):
        return False
    return bool(await _original_modal_interaction_check(self, interaction))


discord.ui.View.interaction_check = _maintenance_view_interaction_check  # type: ignore[assignment]
discord.ui.Modal.interaction_check = _maintenance_modal_interaction_check  # type: ignore[assignment]

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """
    Enhanced global error handler for Discord slash commands with comprehensive error handling.
    
    This runs only for commands that don't have a local @<cmd>.error handler.
    Provides user-friendly error messages and detailed logging for debugging.
    """
    # Determine appropriate response method
    send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
    
    # Enhanced logging for all errors
    import logging
    import traceback
    
    user_info = "Unknown"
    command_info = "unknown command"
    guild_info = "unknown location"
    try:
        user_info = f"{interaction.user.name}#{interaction.user.discriminator}" if interaction.user else "Unknown"
        command_info = f"/{interaction.command.name}" if interaction.command else "Unknown command"
        guild_info = f"in {interaction.guild.name}" if interaction.guild else "in DM"
    except Exception:
        pass

    # Handle expected network errors early — before logging.error() — so they
    # don't generate spurious [ERROR] entries.  None of these warrant a user reply
    # because the connection is already gone.
    _original_early = getattr(error, 'original', None)
    if _original_early and isinstance(_original_early, discord.NotFound) and _original_early.code == 10062:
        logging.warning(f"Interaction expired before response in {command_info}")
        return  # Can't send a message to an expired interaction
    if _original_early and isinstance(_original_early, (aiohttp.ServerDisconnectedError, aiohttp.ClientConnectionResetError, aiohttp.ClientConnectorError)):
        logging.warning(f"Network disconnect during {command_info} ({type(_original_early).__name__}) — skipping user reply")
        return

    try:
        logging.error(f"Slash command error: {error} | User: {user_info} | Command: {command_info} | Location: {guild_info}")
    except Exception:
        pass

    # Handle specific error types with user-friendly messages
    if isinstance(error, app_commands.CommandOnCooldown):
        cooldown_time = error.retry_after
        if cooldown_time < 60:
            time_str = f"{cooldown_time:.1f} seconds"
        elif cooldown_time < SECONDS_PER_HOUR:
            time_str = f"{cooldown_time/60:.1f} minutes"
        else:
            time_str = f"{cooldown_time/SECONDS_PER_HOUR:.1f} hours"
        return await send(f"⏳ **Command on cooldown!** Try again in `{time_str}`.", ephemeral=True)
    
    if isinstance(error, app_commands.NoPrivateMessage):
        return await send(t('commands.errors.dms_only_error', guild_id=None), ephemeral=True)
    
    if isinstance(error, app_commands.CheckFailure):
        return await send(t('commands.errors.check_failure', guild_id=None), ephemeral=True)
    
    if isinstance(error, app_commands.MissingPermissions):
        perms = ', '.join([perm.replace('_', ' ').title() for perm in error.missing_permissions])
        return await send(t('commands.errors.missing_permissions', guild_id=None, perms=perms), ephemeral=True)
    
    if isinstance(error, app_commands.BotMissingPermissions):
        perms = ', '.join([perm.replace('_', ' ').title() for perm in error.missing_permissions])
        return await send(t('commands.errors.bot_missing_permissions', guild_id=None, perms=perms), ephemeral=True)
    
    if isinstance(error, app_commands.TransformerError):
        original_error = getattr(error, 'original', error)
        return await send(f"{t('commands.errors.invalid_input', guild_id=None, original_error=str(original_error))}", ephemeral=True)
    
    # Handle Discord HTTP errors (rate limiting, etc.)
    original_error = getattr(error, 'original', None)
    if original_error and isinstance(original_error, discord.HTTPException):
        http_error = original_error
        if http_error.status == 429:
            return await send(t('commands.errors.rate_limited', guild_id=None), ephemeral=True)
        elif http_error.status == 403:
            return await send(t('commands.errors.access_denied', guild_id=None), ephemeral=True)
        elif http_error.status == 404:
            return await send(t('commands.errors.not_found', guild_id=None), ephemeral=True)
        elif http_error.status >= 500:
            return await send(t('commands.errors.discord_server_error', guild_id=None), ephemeral=True)
    
    # Handle timeout errors
    if original_error and isinstance(original_error, asyncio.TimeoutError):
        return await send(t('commands.errors.timeout_error', guild_id=None), ephemeral=True)
    
    # Enhanced fallback error handling
    try:
        # Log full traceback for debugging
        logging.error(f"Unhandled slash command error in {command_info}: {error}")
        logging.error("".join(traceback.format_exception(type(error), error, error.__traceback__)))
        
        # Provide helpful fallback message
        await send(
            t('commands.errors.unexpected_error', guild_id=None),
            ephemeral=True
        )
    except Exception:
        # Last resort - basic error message without logging
        try:
            await send(t('commands.errors.unexpected_error_fallback', guild_id=None), ephemeral=True)
        except Exception:
            pass  # Give up gracefully if we can't even send a basic message


"""
Discord bot instance with configured intents and command prefix.

Configuration:
    - Command prefix: "!" (e.g., !leaderboard, !subscribe)
    - Message content intent: Enabled for command parsing
    - Help command: Disabled (custom implementation in QBdiscordcmds.py)
    - Case insensitivity: Enabled for command parsing

Dynamic Attributes:
    - bot.start_time: Set during on_ready() event (Optional[datetime])
    - bot.last_sync: Updated during periodic synchronization cycles
    - bot.periodic_task_started: Flag to prevent duplicate task creation

Thread Safety:
    Must only be accessed from the main asyncio event loop thread.

Integration:
    - Used by all modules for Discord command registration and event handling
    - Centralized for cache-centric business logic
"""

# Remove default help command to use custom implementation
bot.remove_command("help")

coc_client: Optional[coc.Client] = None
"""
Clash of Clans API client for accessing game data.

Initialized during startup_login() and provides access to:
    - Clan information and member data
    - War details and attack statistics
    - Player profiles and achievements

Lifecycle:
    1. Initialized as None during import
    2. Created and authenticated in startup_login()
    3. Used throughout application for CoC API calls
    4. Closed during shutdown in async_cleanup()

Thread Safety:
    The coc.Client itself handles connection pooling and is async-safe, but this global reference should only be modified from the main thread.

Integration:
    - Used by all modules for war data and leaderboard generation
    - Centralized for cache-centric business logic

IMPORTANT - Caching:
    ⚠️ NEVER call coc_client.get_clan(clan_tag) directly!
    Always use: await CACHE.coc_clan_cache.get_clan(clan_tag)
    
    The cache provides:
        - 5-minute TTL to reduce API calls
        - Debug logging for cache hits/misses
        - Dramatic performance improvement (15s → 2s on cache hit)
        - Dramatic performance improvement (15s → 2s on cache hit)
"""

shutdown_event: asyncio.Event = asyncio.Event()
"""
Asyncio event for coordinating graceful shutdown across all tasks.

Used to signal all running tasks that shutdown has been initiated. When set, all periodic tasks should complete their current cycle and exit cleanly.

Lifecycle:
    1. Initialized immediately at module import
    2. Set by signal handlers or manual shutdown requests
    3. Waited on by periodic_main() and other long-running tasks

Integration:
    - Used by main loop and cleanup routines for cache consistency
    - Ensures all business logic modules coordinate shutdown
"""

cleaned_up: bool = False
"""
Flag indicating whether cleanup operations have been completed.

Prevents duplicate cleanup operations when multiple shutdown paths are triggered (e.g., signal handler + manual shutdown).

Thread Safety:
    Should only be modified from the main asyncio event loop thread.

Integration:
    - Used by async_cleanup and shutdown routines for cache consistency
"""

lifetime_logged: bool = False
"""Flag ensuring lifetime stats are logged exactly once per bot run."""

shutdown_initiated: bool = False
"""
Internal flag to prevent multiple shutdown sequences from running concurrently.

Used by signal handlers to ensure only one shutdown sequence runs even if multiple signals are received rapidly.

Thread Safety:
    Access is protected by signal handler execution model - signals are processed sequentially in the main thread.

Integration:
    - Used by shutdown coordination for cache consistency
"""

maintenance_mode: bool = False
"""
True while the bot is in maintenance mode.
In this state all periodic update cycles are suspended and all slash commands are blocked
(except /admin so the operator can end maintenance and trigger a restart).
Set to True by /admin MAINTENANCE_START and never reset to False at runtime — the bot
restarts fresh after /admin MAINTENANCE_END.
"""

db_maintenance_mode: bool = False
"""
True while nightly DB maintenance (or /admin Optimize DB) is running.
All slash commands are blocked with a short "DB optimization in progress" message.
Set/cleared by nightly_db_maintenance() in db_manager.py.
"""

maintenance_pending: bool = False
"""
True when /admin MAINTENANCE_START was issued while an update cycle was in progress.
periodic_main() checks this flag immediately after the cycle finishes and, if set,
calls do_maintenance_shutdown() to close resources and complete the maintenance start.
This avoids any timeout — maintenance simply waits for the natural end of the cycle.
"""

maintenance_pending_interaction: Any = None
"""
Stores the discord.Interaction from /admin MAINTENANCE_START when maintenance was
deferred (cycle was running). periodic_main() uses it to update the waiting message
to the final "maintenance active" confirmation once shutdown completes.
Cleared (set to None) after use.
"""

maintenance_pending_interaction_time: float = 0.0
"""
Unix timestamp (datetime.now().timestamp()) recorded when maintenance_pending_interaction
was stored. Used to skip the Discord edit attempt if the cycle ran longer than 2 hours —
interaction tokens expire at ~15 min; the 2-hour guard is a safe programmatic ceiling.
Maintenance shutdown itself is never gated on this value.
"""

in_main_cycle: bool = False
"""
True while periodic_main() is executing the main() update cycle.
Used by the maintenance-start handler to wait until the current cycle finishes before
closing the database, avoiding mid-cycle DB writes after the connection is closed.
"""

cycle_idle_event: asyncio.Event = asyncio.Event()
cycle_idle_event.set()  # starts set — bot is idle until the first cycle begins
"""
Set whenever the bot is NOT executing an update cycle (idle or sleeping).
Cleared at the start of each update cycle, set again as soon as in_main_cycle goes False.
The MAINTENANCE_START handler awaits this event so it gets an instant wake-up the
moment the running cycle finishes, instead of polling every second.
"""

force_cycle_event: asyncio.Event = asyncio.Event()
"""
Set by /admin Start Update Cycle to interrupt the sleep phase and trigger the next update
cycle immediately. Cleared by periodic_main() after consuming the signal.
"""

db_maintenance_idle_event: asyncio.Event = asyncio.Event()
db_maintenance_idle_event.set()  # starts set — no maintenance running
"""
Cleared while nightly DB maintenance (ANALYZE/REINDEX/VACUUM) is in flight.
Set again as soon as the maintenance task completes (success or error).
periodic_main() awaits this event after the sleep phase so the next update
cycle never starts while VACUUM is holding an exclusive DB lock.
"""

backfill_idle_event: asyncio.Event = asyncio.Event()
backfill_idle_event.set()  # starts set — no backfill running
"""
Cleared while /admin Backfill CWL Groups is executing CoC API calls.
Set again as soon as the backfill completes (success or error).
periodic_main() checks this event at the top of the update loop so no cycle
starts while the backfill is in flight and making CoC API calls.
"""

force_cycle_pending: bool = False
"""
True when /admin Start Update Cycle was issued while an update cycle was already running.
periodic_main() checks this flag immediately after a cycle finishes and skips the sleep
phase when set (analogous to maintenance_pending but without closing resources).
Cleared by periodic_main() when the deferred wake-up is consumed.
"""

optimize_db_pending: bool = False
"""
True when /admin Optimize DB was issued while an update cycle was in progress.
periodic_main() checks this flag immediately after the cycle finishes and, if set,
runs nightly_db_maintenance() exactly as the nightly scheduler does.
Cleared by periodic_main() after the task is launched.
"""

optimize_db_pending_interaction: Any = None
"""
Stores the discord.Interaction from /admin Optimize DB when the run was deferred
(cycle was running). Used to send the final result once the task completes.
Cleared (set to None) after use.
"""

EXIT_CODE_MAINTENANCE: int = 42
"""
Exit code written to sys.exit() when /admin MAINTENANCE_END is issued.
The start.sh wrapper script detects this code and immediately restarts the bot,
skipping the normal "stopped — not restarting" path.
"""

# --- On-demand memory profiling (PROD-safe) ---
memtrace_pending: bool = False
"""
When True, periodic_main() will take a tracemalloc snapshot after the current
cycle completes and then stop tracing.  Set by /admin Memory Profile in PROD mode.
tracemalloc is started with nframe=1 (~2-3 % CPU) for one cycle only.
"""

memtrace_baseline: object = None  # type: ignore[assignment]
"""
Baseline tracemalloc.Snapshot taken at the moment tracing starts.
Stored so save_memtrace_snapshot() can call snapshot.compare_to(baseline)
and emit a differential section showing only what grew since tracing began.
Reset to None after the snapshot is saved.
"""

bot_start_time: float = 0.0
"""Unix timestamp set once in on_ready; used to report uptime in memory profiles."""

exit_code: int = 0
"""
Process exit code to pass to sys.exit() after graceful shutdown.
0 = normal exit (do not restart).
EXIT_CODE_MAINTENANCE (42) = restart immediately (maintenance cycle complete).
"""

# New: track whether caches were successfully loaded at least once during this run
cache_loaded: bool = False
"""
True if CACHE.load_all() completed successfully during startup. 
Used to prevent accidental overwriting of persistent cache files with empty data on abnormal shutdown.
"""

# Rate limiting monitoring
rate_limit_hits: int = 0
"""
Counter for Discord API rate limit hits to monitor bot health.
"""

# Update cycle runtime statistics
cycle_stats: dict[str, Any] = {"count": 0, "min_s": None, "max_s": None, "total_s": 0.0}
"""
Runtime statistics for the periodic update cycle.
  count   – total number of successfully completed cycles since startup.
  min_s   – shortest cycle duration in seconds (None until first cycle).
  max_s   – longest cycle duration in seconds (None until first cycle).
  total_s – cumulative duration in seconds (used to compute avg).
"""

# Nightly maintenance duration history
nightly_maintenance_durations: "deque[float]" = deque(maxlen=10)
"""
Rolling window of the last 10 completed nightly-maintenance run durations
(seconds), oldest first. Populated by run_nightly_maintenance_routine() in
QapBot.py after every run — the scheduled 03:00 UTC task, /admin Execute
Nightly Maintenance, and the deferred-optimize path all share that one
function, so all three feed this same history.
Read by /status and /admin Check Logs (via
qapbot.QBdiscocmdshelper_admin_command.format_nightly_maintenance_stats) to
report min/avg/max. Empty until this process completes its own first run —
until then those commands fall back to the last run's duration read from the
log file (find_last_nightly_maintenance_duration), since maintenance runs at
most once/night and the last real run may predate this process's start.
"""

last_rate_limit: Optional[datetime] = None
"""
Timestamp of last rate limit hit for monitoring.
"""

@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel) -> None:
    """
    Auto-cleanup when a Discord channel is deleted.

    Removes all subscriptions and leaderboard_messages cache entries tied to the
    deleted channel so orphaned DB rows never accumulate.  The standard /unsubscribe
    command is no longer possible once the channel is gone, so this is the only
    reliable cleanup path.

    Requires only the default `guilds` intent (already enabled).
    """
    import logging
    if not isinstance(channel, discord.TextChannel):
        return  # only text channels can hold subscriptions

    channel_id_str = str(channel.id)
    guild_id_str = str(channel.guild.id)

    try:
        from qapbot.cache_manager import CACHE

        # 1. Collect affected clan tags BEFORE removing subscriptions (for status update)
        channel_subs = CACHE.get_channel_subscriptions(channel_id_str)
        affected_clan_tags = {s["clan_tag"] for s in channel_subs if "clan_tag" in s}

        # 2. Remove subscriptions from CACHE + DB (write-through)
        removed = await CACHE.remove_channel_subscriptions(channel_id_str)
        if removed:
            logging.warning(
                f"[CHANNEL-DELETE] Auto-removed subscriptions for deleted channel "
                f"{channel_id_str} (#{channel.name}) in guild {guild_id_str} ({channel.guild.name})"
            )

        # 3. Purge leaderboard_messages cache entries for this channel
        stale_keys = [k for k, v in list(CACHE.leaderboard_messages.items())
                      if v.get("channel_id") == channel_id_str]
        for k in stale_keys:
            await CACHE.delete_leaderboard_message(k)
        if stale_keys:
            logging.info(
                f"[CHANNEL-DELETE] Purged {len(stale_keys)} leaderboard_messages entries "
                f"for deleted channel {channel_id_str}"
            )

        # 4. Recalculate has_active_subscriptions for each formerly-subscribed clan
        for clan_tag in affected_clan_tags:
            await CACHE.update_clan_subscription_status(clan_tag)

    except Exception as e:
        logging.error(f"[CHANNEL-DELETE] Error handling deletion of channel {channel_id_str}: {e}")


@bot.event
async def on_app_command_completion(interaction: discord.Interaction, command: app_commands.Command[Any, ..., Any]) -> None:  # type: ignore[misc]
    """
    Monitor successful command completions for health tracking.
    """
    try:
        import logging
        user_info = f"{interaction.user.name}" if interaction.user else "Unknown"
        command_info = f"/{command.name}" if command else "Unknown"
        guild_info = f"in {interaction.guild.name}" if interaction.guild else "in DM"
        logging.debug(f"Command completed: {command_info} by {user_info} {guild_info}")
    except Exception:
        pass

@bot.event
async def on_error(event: str, *args: Any, **kwargs: Any) -> None:
    """
    Global error handler for Discord.py events with enhanced logging and rate limit detection.
    """
    import logging
    import traceback
    
    try:
        # Check if this is a rate limit error
        if len(args) > 0 and hasattr(args[0], 'status') and args[0].status == 429:  # type: ignore[misc]
            global rate_limit_hits, last_rate_limit
            from datetime import datetime
            rate_limit_hits += 1
            last_rate_limit = datetime.now()
            logging.warning(f"Discord API rate limit hit #{rate_limit_hits} during event '{event}'")
        
        # Log the full error
        logging.error(f"Discord event error in '{event}': {args}")
        
        # Log traceback for debugging
        import sys
        exc_info = sys.exc_info()
        if exc_info[0] is not None:
            logging.error("".join(traceback.format_exception(*exc_info)))
            
    except Exception as log_error:
        # Fallback logging if our enhanced logging fails
        print(f"[DISCORD ERROR] Event: {event}, Error: {args}, Logging failed: {log_error}")