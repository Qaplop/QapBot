"""
Configuration management for QapBot with environment variable processing and immutable settings.

This module provides centralized, single-source-of-truth configuration for QapBot. All settings are loaded from environment variables, validated, and exposed via a frozen dataclass singleton (CONFIG). No module should create its own config; all configuration access is via CONFIG.

Features:
- Immutable configuration object (frozen dataclass)
- Environment variable loading with type validation and safe defaults
- No hardcoded credentials in source code
- Centralized access for all modules
- Thread-safe and safe for async usage
- DEV/PROD mode selection based on DISCORD_GUILD_ID
- Fail-fast validation on startup

Configuration Sources:
- Environment variables (primary)
- .env files (loaded by python-dotenv in main modules)
- Hardcoded defaults (fallback values)

Security:
- Sensitive values (API credentials) loaded from environment only
- No sensitive values are logged or exposed
- Configuration object is immutable to prevent accidental modification

Integration:
- Used by all modules requiring configuration values
- Imported as singleton CONFIG object for consistency
- Supports both development and production environments
- DEV mode: DISCORD_GUILD_ID > 0 (uses _DEV credentials)
- PROD mode: DISCORD_GUILD_ID == 0 (uses standard credentials)

Example:
    from qapbot.config import CONFIG
    email = CONFIG.coc_email
    interval = CONFIG.sleep_interval
    max_subs = CONFIG.max_clan_subscriptions
    is_dev = CONFIG.is_dev_mode
"""
import logging
import os
from dataclasses import dataclass
from dotenv import load_dotenv

# Load environment variables from .env file (override=False: OS env vars take priority,
# ensuring tests patching os.environ can control config without .env re-overriding them)
load_dotenv(override=False)

# Import custom exceptions for validation
from qapbot.exceptions import ConfigurationError

# Central configuration loader
@dataclass(frozen=True)
class BotConfig:
    """
    Immutable configuration object containing all QapBot settings.
    
    Holds all configuration values needed for QapBot operation. The frozen=True parameter makes the object immutable after creation, preventing accidental modification.
    
    Attributes:
        coc_email: Clash of Clans API account email address
        coc_password: Clash of Clans API account password
        discord_token: Discord bot token for authentication
        sleep_interval: Seconds between periodic update cycles (default: 300)
        server_admin: Discord username with administrative privileges
        max_clan_subscriptions: Maximum clans per channel subscription (default: 7)
        is_dev_mode: True if running in DEV mode (guild-specific), False if PROD (global)
        discord_guild_id: Guild ID for DEV mode (0 for PROD/global mode)
    
    Security Notes:
        - API credentials are loaded from environment variables only
        - No sensitive values have default values to prevent exposure
        - Configuration is immutable to prevent runtime modification
        - DEV and PROD credentials are selected automatically based on DISCORD_GUILD_ID
    
    Environment Variables:
        DEV Mode (DISCORD_GUILD_ID > 0):
        - DISCORD_TOKEN_DEV: Required Discord bot token for DEV instance
        - COC_API_EMAIL_DEV: Required CoC API email for DEV
        - COC_API_PASSWORD_DEV: Required CoC API password for DEV
        
        PROD Mode (DISCORD_GUILD_ID == 0):
        - DISCORD_TOKEN: Required Discord bot token for PROD instance
        - COC_API_EMAIL: Required CoC API email for PROD
        - COC_API_PASSWORD: Required CoC API password for PROD
        
        Common:
        - DISCORD_GUILD_ID: Guild ID for DEV mode (0 for PROD/global)
        - SLEEP_INTERVAL: Optional, defaults to 300 seconds (5 minutes)
        - SERVER_ADMIN: Optional, username for bot administration commands
        - MAX_CLAN_SUBSCRIPTIONS: Optional, defaults to 7 clans per channel
    
    Example:
        # DEV mode
        config = BotConfig(
            coc_email="dev@example.com",
            coc_password="dev_secret",
            discord_token="dev_token_123",
            is_dev_mode=True,
            discord_guild_id=123456789,
            ...
        )
        
        # PROD mode
        config = BotConfig(
            coc_email="prod@example.com",
            coc_password="prod_secret",
            discord_token="prod_token_456",
            is_dev_mode=False,
            discord_guild_id=0,
            ...
        )
    """
    coc_email: str
    coc_password: str
    discord_token: str
    sleep_interval: int
    server_admin: str
    max_clan_subscriptions: int = 7
    is_dev_mode: bool = False
    discord_guild_id: int = 0
    # DEV-only: allow repost/bump of playerregistration welcome message ONLY in this channel id
    dev_playerregistration_channel_id: int = 0
    # Notification settings
    notification_hours_before_end: int = 4
    notification_batch_delay: int = 2
    notification_max_retries: int = 1
    
    # Data directory (base for DB, temp, logs) and archive directory.
    # Both are derived from PROD_DATA_DIR at runtime; defaults keep old layout.
    data_dir: str = "data"                 # Overridden by PROD_DATA_DIR env var (PROD mode only)
    archive_dir: str = "archive"           # Overridden by PROD_DATA_DIR env var (PROD mode only)
    archive_old_dir: str = "archive_old"   # Overridden by PROD_DATA_DIR env var (PROD mode only); same SSD volume as archive_dir
    investigate_dir: str = "investigate"   # Always relative to project root (HDD), not SSD

    # Database settings
    db_path: str = "data/qapbot.db"        # SQLite database file path (hot: current + previous calendar month)
    history_db_path: str = "data/qapbot_history.db"  # SQLite history database (ATTACHed as schema 'history'; everything older than db_path's window)
    # Cap on the scheduled nightly window's monthly_history_migration() run (QapBot.py's
    # 03:00 UTC path only — NOT the manual run_history_migration_now.py CLI, which takes its
    # own --time-budget-minutes, and not the per-cycle chunk below). Added 2026-08-01: a
    # first-ever run against a large backlog can take 10+ hours; without a cap, the automatic
    # path would block Discord commands for the whole thing in one sitting. A capped run
    # reports PARTIAL (not done), so is_monthly_migration_due() keeps retrying.
    history_migration_time_budget_minutes: float = 90.0
    # Opportunistic per-cycle migration chunk (added 2026-08-01, same incident): rather than
    # only advancing the migration once/night, spend up to this many minutes of the otherwise-
    # idle sleep window (between update cycles, ~4-4.5 min of a 5-min SLEEP_INTERVAL by
    # default) on migration whenever it's still due. Self-limiting — once
    # is_monthly_migration_due() reports done, this does nothing, so it costs nothing during
    # routine months where each month's backlog is small enough to finish in one or two cycles.
    # During an active large backlog it dominates the idle window (Discord commands blocked
    # most of the time, not just briefly) in exchange for finishing far faster than
    # once-a-night chunking alone. Set to 0 to disable (falls back to the once-a-night
    # scheduled-window chunk only). Keep below the typical idle window (SLEEP_INTERVAL minus
    # typical cycle duration) so the gate at the end of the sleep-wait rarely actually blocks.
    history_migration_cycle_chunk_minutes: float = 4.0
    # Cap on Step 0.5's migration run specifically for the /admin "Execute Nightly Maintenance"
    # command (added 2026-08-01) — deliberately much shorter than
    # history_migration_time_budget_minutes. /admin is an interactive, user-awaited command
    # (Discord interaction token expires after ~15 min); migration progress isn't its purpose
    # (the per-cycle chunk above already carries the bulk of it) — this just takes a quick
    # nibble so the command stays fast and focused on the actual maintenance steps
    # (WAL checkpoint / VACUUM / REINDEX / ANALYZE).
    history_migration_admin_budget_minutes: float = 1.0
    
    # DEV-only: Skip CoC API connection entirely (for testing without valid API token)
    no_coc_api: bool = False

    # Monte Carlo simulation: multi-process parallelism
    sim_multiprocess_enabled: bool = True   # SIM_MULTIPROCESS_ENABLED in .env
    sim_max_workers: int = 0                 # SIM_MAX_WORKERS in .env (0 = all cores, capped at 8)

    # CWL clan-config web bridge (qapbot/web_bridge.py, CWL_CLAN_CONFIG_ACTIVITY_PLAN.md Phase B).
    # Both 0/empty by default = bridge not started. Bound to 127.0.0.1 only — a cloudflared
    # tunnel (not this bot) is what makes it reachable from the Cloudflare Worker.
    # DEV/PROD-suffixed like discord_token/coc_email above (dev and prod hosts share one .env):
    # WEB_BRIDGE_PORT_DEV/WEB_BRIDGE_SECRET_DEV when DISCORD_GUILD_ID > 0 (DEV mode), else
    # WEB_BRIDGE_PORT/WEB_BRIDGE_SECRET. Each must match the corresponding Worker environment's
    # BRIDGE_URL/BRIDGE_SECRET (env.dev vs env.prod in activity/server/wrangler.toml).
    web_bridge_port: int = 0
    web_bridge_secret: str = ""

    # CWL roster-planning feature under active development (CWL_ROSTER_PLANNING_PLAN.md):
    # while True, any CWL-related DM (signup confirm/opt-out blast, future assignment
    # notifications) is only actually delivered to CONFIG.server_admin's own Discord
    # account — every other resolved recipient is skipped (their DB rows are still
    # written; only DM *delivery* is guarded, so the data stays realistic to test
    # against). Independent of is_dev_mode and set separately per host from the shared
    # .env file (DEV/PROD-suffixed like web_bridge_* above) so it can be enabled on
    # PROD too while live-testing there, not just DEV. Defaults to True (safe) — flip
    # to False only once the feature is ready for real players to be DMed.
    cwl_dm_restrict_to_admin: bool = True



def load_config() -> BotConfig:
    """
    Load configuration values from environment variables with DEV/PROD mode selection.
    
    Reads all required configuration values from environment variables, determines whether to run in DEV or PROD mode based on DISCORD_GUILD_ID, and returns an immutable BotConfig object with the appropriate credentials.
    
    Returns:
        BotConfig: Immutable configuration object with all settings
    
    DEV/PROD Mode Selection:
        - DEV mode: DISCORD_GUILD_ID > 0
          Uses: DISCORD_TOKEN_DEV, COC_API_EMAIL_DEV, COC_API_PASSWORD_DEV
        - PROD mode: DISCORD_GUILD_ID == 0
          Uses: DISCORD_TOKEN, COC_API_EMAIL, COC_API_PASSWORD
    
    Environment Variable Processing:
        - DISCORD_GUILD_ID: Determines DEV/PROD mode (default: 0 for PROD)
        - DISCORD_TOKEN or DISCORD_TOKEN_DEV: Selected based on mode
        - COC_API_EMAIL or COC_API_EMAIL_DEV: Selected based on mode
        - COC_API_PASSWORD or COC_API_PASSWORD_DEV: Selected based on mode
        - SLEEP_INTERVAL: Converted to int, defaults to 300 seconds
        - SERVER_ADMIN: Numeric Discord user ID of the bot admin (preferred; a username is
          accepted as deprecated legacy fallback) — optional, for admin command restrictions
        - MAX_CLAN_SUBSCRIPTIONS: Converted to int with validation, defaults to 7
    
    Type Conversion:
        - Numeric values are converted with safe fallbacks
        - Invalid integers default to safe values rather than raising exceptions
        - String values are used directly from environment
    
    Error Handling:
        - Invalid MAX_CLAN_SUBSCRIPTIONS values fall back to default of 7
        - Invalid DISCORD_GUILD_ID values fall back to 0 (PROD mode)
        - Missing optional values use safe defaults
        - Required values (tokens/credentials) must be provided by environment
    
    Security:
        - No sensitive values are logged or exposed in error messages
        - Configuration validation happens early in application startup
        - Automatic credential selection prevents mixing DEV/PROD credentials
    
    Example:
        # With DEV environment variables set:
        # DISCORD_GUILD_ID=123456789
        # DISCORD_TOKEN_DEV=dev_token_123
        # COC_API_EMAIL_DEV=dev@example.com
        # COC_API_PASSWORD_DEV=dev_secret
        config = load_config()
        # Returns: BotConfig(is_dev_mode=True, discord_guild_id=123456789, ...)
        
        # With PROD environment variables set:
        # DISCORD_GUILD_ID=0
        # DISCORD_TOKEN=prod_token_456
        # COC_API_EMAIL=prod@example.com
        # COC_API_PASSWORD=prod_secret
        config = load_config()
        # Returns: BotConfig(is_dev_mode=False, discord_guild_id=0, ...)
    """
    # Determine DEV/PROD mode based on DISCORD_GUILD_ID
    try:
        discord_guild_id = int(os.getenv("DISCORD_GUILD_ID", "0"))
        #discord_guild_id = 0 # Uncomment to force global mode for testing/dev, also uncomment correpsonding line in QapBot.py!!!
    except ValueError:
        discord_guild_id = 0
    
    is_dev_mode = discord_guild_id > 0
    
    # Select credentials based on mode
    if is_dev_mode:
        # DEV mode: use _DEV credentials
        coc_email = os.getenv("COC_API_EMAIL_DEV", "")
        coc_password = os.getenv("COC_API_PASSWORD_DEV", "")
        discord_token = os.getenv("DISCORD_TOKEN_DEV", "")
        # DEV-only: playerregistration channel restriction
        try:
            dev_playerregistration_channel_id = int(os.getenv("DEV_PLAYERREGISTRATION_CHANNEL_ID", "0"))
        except ValueError:
            dev_playerregistration_channel_id = 0
    else:
        # PROD mode: use standard credentials
        dev_playerregistration_channel_id = 0  # Not used in PROD mode
        coc_email = os.getenv("COC_API_EMAIL", "")
        coc_password = os.getenv("COC_API_PASSWORD", "")
        discord_token = os.getenv("DISCORD_TOKEN", "")
    
    sleep_interval = int(os.getenv("SLEEP_INTERVAL", "300"))
    server_admin = os.getenv("SERVER_ADMIN", "")
    if server_admin and not server_admin.isdigit():
        logging.warning(
            "SERVER_ADMIN is set to a username (deprecated — usernames can be re-registered "
            "by others if released). Set SERVER_ADMIN to your numeric Discord user ID instead."
        )
    
    # Safe integer conversion with fallback for max subscriptions
    try:
        max_subs = int(os.getenv("MAX_CLAN_SUBSCRIPTIONS", "15"))
    except ValueError:
        max_subs = 15
    
    # Notification configuration with safe defaults
    try:
        notif_hours = int(os.getenv("NOTIFICATION_HOURS_BEFORE_END", "4"))
    except ValueError:
        notif_hours = 4
    
    try:
        notif_delay = int(os.getenv("NOTIFICATION_BATCH_DELAY", "2"))
    except ValueError:
        notif_delay = 2
    
    try:
        notif_retries = int(os.getenv("NOTIFICATION_MAX_RETRIES", "1"))
    except ValueError:
        notif_retries = 1
    
    # Data + archive directories — all derived from PROD_DATA_DIR base path.
    # When set AND running in PROD mode: data_dir = PROD_DATA_DIR/data,
    #   archive_dir = PROD_DATA_DIR/archive, archive_old_dir = PROD_DATA_DIR/archive_old.
    # archive_old_dir lives on the same SSD volume as archive_dir so that the nightly
    #   archive move can use os.replace() (same-filesystem metadata rename, no data copy).
    # Ignored in DEV mode even if set; falls back to relative paths.
    _prod_base = os.getenv("PROD_DATA_DIR", "")
    if _prod_base and not is_dev_mode:
        data_dir = os.path.join(_prod_base, "data")
        archive_dir = os.path.join(_prod_base, "archive")
        archive_old_dir = os.path.join(_prod_base, "archive_old")
    else:
        data_dir = "data"
        archive_dir = "archive"
        archive_old_dir = "archive_old"
    investigate_dir = "investigate"  # Always relative to project root (HDD), not SSD

    # Database configuration
    db_path = os.getenv("DB_PATH", os.path.join(data_dir, "qapbot.db"))
    history_db_path = os.getenv("HISTORY_DB_PATH", os.path.join(data_dir, "qapbot_history.db"))
    try:
        history_migration_time_budget_minutes = float(os.getenv("HISTORY_MIGRATION_TIME_BUDGET_MINUTES", "90"))
    except ValueError:
        history_migration_time_budget_minutes = 90.0
    try:
        history_migration_cycle_chunk_minutes = float(os.getenv("HISTORY_MIGRATION_CYCLE_CHUNK_MINUTES", "4"))
    except ValueError:
        history_migration_cycle_chunk_minutes = 4.0
    try:
        history_migration_admin_budget_minutes = float(os.getenv("HISTORY_MIGRATION_ADMIN_BUDGET_MINUTES", "1"))
    except ValueError:
        history_migration_admin_budget_minutes = 1.0

    # DEV-only: Skip CoC API connection (for testing without valid API token)
    no_coc_api = os.getenv("NO_COC_API", "false").lower() in ("true", "1", "yes")

    # Monte Carlo simulation parallelism
    sim_multiprocess_enabled = os.getenv("SIM_MULTIPROCESS_ENABLED", "true").lower() not in ("false", "0", "no")
    try:
        sim_max_workers = max(0, int(os.getenv("SIM_MAX_WORKERS", "0")))
    except ValueError:
        sim_max_workers = 0

    # CWL clan-config web bridge (Phase B) — both must be set to start it. DEV/PROD-suffixed
    # like discord_token/coc_email above, since dev and prod hosts share one .env file.
    if is_dev_mode:
        _web_bridge_port_raw = os.getenv("WEB_BRIDGE_PORT_DEV", "0")
        web_bridge_secret = os.getenv("WEB_BRIDGE_SECRET_DEV", "")
    else:
        _web_bridge_port_raw = os.getenv("WEB_BRIDGE_PORT", "0")
        web_bridge_secret = os.getenv("WEB_BRIDGE_SECRET", "")
    try:
        web_bridge_port = max(0, int(_web_bridge_port_raw))
    except ValueError:
        web_bridge_port = 0

    # CWL DM safety toggle (feature under active development) — DEV/PROD-suffixed like
    # web_bridge_* above, so it can be set independently for a DEV host and a PROD host
    # sharing one .env file. Defaults to "true" (restricted) on both.
    if is_dev_mode:
        cwl_dm_restrict_to_admin = os.getenv("CWL_DM_RESTRICT_TO_ADMIN_DEV", "true").lower() in ("true", "1", "yes")
    else:
        cwl_dm_restrict_to_admin = os.getenv("CWL_DM_RESTRICT_TO_ADMIN", "true").lower() in ("true", "1", "yes")

    # Create config object
    config = BotConfig(
        coc_email=coc_email,
        coc_password=coc_password,
        discord_token=discord_token,
        sleep_interval=sleep_interval,
        server_admin=server_admin,
        max_clan_subscriptions=max_subs,
        data_dir=data_dir,
        archive_dir=archive_dir,
        archive_old_dir=archive_old_dir,
        investigate_dir=investigate_dir,
        db_path=db_path,
        history_db_path=history_db_path,
        history_migration_time_budget_minutes=history_migration_time_budget_minutes,
        history_migration_cycle_chunk_minutes=history_migration_cycle_chunk_minutes,
        history_migration_admin_budget_minutes=history_migration_admin_budget_minutes,
        is_dev_mode=is_dev_mode,
        discord_guild_id=discord_guild_id,
        dev_playerregistration_channel_id=dev_playerregistration_channel_id,
        notification_hours_before_end=notif_hours,
        notification_batch_delay=notif_delay,
        notification_max_retries=notif_retries,
        no_coc_api=no_coc_api,
        sim_multiprocess_enabled=sim_multiprocess_enabled,
        sim_max_workers=sim_max_workers,
        web_bridge_port=web_bridge_port,
        web_bridge_secret=web_bridge_secret,
        cwl_dm_restrict_to_admin=cwl_dm_restrict_to_admin,
    )
    
    # Validate configuration (fail fast on startup)
    _validate_config(config)
    
    return config


def _validate_config(config: BotConfig) -> None:
    """
    Validate configuration values and fail fast if invalid.
    
    Performs comprehensive validation of all configuration values to catch
    errors at startup rather than during runtime. This includes checking for:
    - Required credentials are present
    - Numeric values are within valid ranges
    - Mode-specific requirements are met
    
    Args:
        config: BotConfig object to validate
    
    Raises:
        ConfigurationError: If any validation check fails
    
    Validation Rules:
        1. Required credentials must be non-empty strings
        2. Sleep interval must be >= 60 seconds (prevent spam)
        3. Max subscriptions must be >= 1
        4. Notification hours must be >= 0
        5. Notification delays/retries must be >= 0
        6. DEV mode must have valid guild ID > 0
    
    Example:
        config = BotConfig(...)
        _validate_config(config)  # Raises ConfigurationError if invalid
    """
    mode_str = "DEV" if config.is_dev_mode else "PROD"
    
    # Validate required credentials (skip CoC credentials when NO_COC_API is set)
    if not config.no_coc_api:
        if not config.coc_email:
            if config.is_dev_mode:
                raise ConfigurationError(
                    "COC_API_EMAIL_DEV must be set for DEV mode",
                    context={"mode": mode_str}
                )
            else:
                raise ConfigurationError(
                    "COC_API_EMAIL must be set for PROD mode",
                    context={"mode": mode_str}
                )
        
        if not config.coc_password:
            if config.is_dev_mode:
                raise ConfigurationError(
                    "COC_API_PASSWORD_DEV must be set for DEV mode",
                    context={"mode": mode_str}
                )
            else:
                raise ConfigurationError(
                    "COC_API_PASSWORD must be set for PROD mode",
                    context={"mode": mode_str}
                )
    
    if not config.discord_token:
        if config.is_dev_mode:
            raise ConfigurationError(
                "DISCORD_TOKEN_DEV must be set for DEV mode",
                context={"mode": mode_str}
            )
        else:
            raise ConfigurationError(
                "DISCORD_TOKEN must be set for PROD mode",
                context={"mode": mode_str}
            )
    
    # Validate numeric ranges
    _min_sleep = 10 if config.is_dev_mode else 60
    if config.sleep_interval < _min_sleep:
        raise ConfigurationError(
            f"SLEEP_INTERVAL must be >= {_min_sleep} seconds {'(DEV mode)' if config.is_dev_mode else 'to prevent API spam'}, got {config.sleep_interval}",
            context={"sleep_interval": config.sleep_interval}
        )
    
    if config.max_clan_subscriptions < 1:
        raise ConfigurationError(
            f"MAX_CLAN_SUBSCRIPTIONS must be >= 1, got {config.max_clan_subscriptions}",
            context={"max_clan_subscriptions": config.max_clan_subscriptions}
        )
    
    if config.notification_hours_before_end < 0:
        raise ConfigurationError(
            f"NOTIFICATION_HOURS_BEFORE_END must be >= 0, got {config.notification_hours_before_end}",
            context={"notification_hours_before_end": config.notification_hours_before_end}
        )
    
    if config.notification_batch_delay < 0:
        raise ConfigurationError(
            f"NOTIFICATION_BATCH_DELAY must be >= 0, got {config.notification_batch_delay}",
            context={"notification_batch_delay": config.notification_batch_delay}
        )
    
    if config.notification_max_retries < 0:
        raise ConfigurationError(
            f"NOTIFICATION_MAX_RETRIES must be >= 0, got {config.notification_max_retries}",
            context={"notification_max_retries": config.notification_max_retries}
        )
    
    # Validate DEV mode consistency
    if config.is_dev_mode and config.discord_guild_id <= 0:
        raise ConfigurationError(
            "DEV mode requires DISCORD_GUILD_ID > 0",
            context={"is_dev_mode": config.is_dev_mode, "discord_guild_id": config.discord_guild_id}
        )
    
    if not config.is_dev_mode and config.discord_guild_id != 0:
        raise ConfigurationError(
            "PROD mode requires DISCORD_GUILD_ID == 0",
            context={"is_dev_mode": config.is_dev_mode, "discord_guild_id": config.discord_guild_id}
        )

# Global configuration object - immutable singleton
CONFIG = load_config()
"""
Global configuration object providing centralized access to all QapBot settings.

This singleton object is loaded once at module import time and provides immutable
access to all configuration values throughout the application. All modules should
import and use this CONFIG object rather than creating their own configuration instances.

The CONFIG object automatically selects DEV or PROD credentials based on DISCORD_GUILD_ID:
- DEV mode (DISCORD_GUILD_ID > 0): Uses _DEV credentials
- PROD mode (DISCORD_GUILD_ID == 0): Uses standard credentials

Usage:
    from qapbot.config import CONFIG
    email = CONFIG.coc_email
    token = CONFIG.discord_token
    interval = CONFIG.sleep_interval
    max_subs = CONFIG.max_clan_subscriptions
    if CONFIG.is_dev_mode:
        # Running in DEV mode
        pass

Thread Safety:
    The CONFIG object is immutable (frozen dataclass) and safe for concurrent access
    from multiple threads or asyncio tasks.

Validation:
    Configuration is validated once at load time. If required environment variables
    are missing or invalid, the application should fail early rather than during
    runtime operations.

Example:
    from qapbot.config import CONFIG
    
    # Check mode
    if CONFIG.is_dev_mode:
        print(f"Running in DEV mode for guild {CONFIG.discord_guild_id}")
    else:
        print("Running in PROD mode (global)")
    
    # Use credentials (automatically selected)
    await coc_client.login(CONFIG.coc_email, CONFIG.coc_password)
    bot.run(CONFIG.discord_token)
"""
