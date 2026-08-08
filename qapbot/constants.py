"""
Application-wide constants for QapBot.

This module centralizes all magic numbers and constant values used throughout the
application. By defining constants here, we:
- Make the code more maintainable and readable
- Provide a single source of truth for configuration values
- Enable easy adjustment of parameters without searching through code
- Document the meaning and purpose of each value

Usage:
    from qapbot.constants import DISCORD_MESSAGE_MAX_LENGTH, COC_API_RATE_LIMIT
    
    if len(message) > DISCORD_MESSAGE_MAX_LENGTH:
        # Split message
        pass
"""

# ============================================================================
# Discord API Limits
# ============================================================================

DISCORD_MESSAGE_MAX_LENGTH = 2000
"""Maximum characters in a single Discord message."""

DISCORD_EMBED_MAX_LENGTH = 4096
"""Maximum characters in a Discord embed description."""

DISCORD_EMBED_FIELD_MAX_LENGTH = 1024
"""Maximum characters in a Discord embed field value."""

DISCORD_EMBED_TITLE_MAX_LENGTH = 256
"""Maximum characters in a Discord embed title."""

DISCORD_EMBED_MAX_FIELDS = 25
"""Maximum number of fields in a Discord embed."""


# ============================================================================
# Clash of Clans API Configuration
# ============================================================================

COC_API_MAX_KEYS = 10
"""Maximum number of API keys to use with CoC client."""

COC_API_RATE_LIMIT_PER_SECOND = 10
"""Official CoC API rate limit: requests per second per API key."""

COC_API_BATCH_THROTTLE_LIMIT = 100
"""Batch throttler limit (COC_API_MAX_KEYS * COC_API_RATE_LIMIT_PER_SECOND)."""

COC_CLAN_CACHE_TTL_SECONDS = 280
"""Time-to-live for cached clan data (4 minutes 40 seconds)."""


# ============================================================================
# War Processing Configuration
# ============================================================================

INACTIVE_CLAN_UPDATE_INTERVAL_HOURS = 22
"""Hours between updates for inactive clans (no active war)."""

ACTIVE_CLAN_UPDATE_INTERVAL_SECONDS = 300
"""Seconds between updates for active clans (5 minutes, same as SLEEP_INTERVAL)."""

WAR_STALE_THRESHOLD_HOURS = 24
"""Hours after which a war file is considered stale if state != 'warEnded'."""

CWL_WAR_EXPIRY_DAYS = 60
"""Days after which CWL war data can be archived/cleaned."""

WAR_END_NOTIFICATION_HOURS_BEFORE = 4
"""Hours before war end to send notifications (default, overridden by CONFIG)."""

WAR_UPDATE_LEAGUES: frozenset[str] = frozenset({
    "Legend League",
    "Titan League I",
    "Titan League II",
    "Titan League III",
    "Champion League I",
    "Champion League II",
    "Champion League III",
    "Master League I",
    "Master League II",
    "Master League III",
})
"""War leagues (Master III and above) that qualify a non-subscribed clan for
active ``track_war_updates`` tracking (22h polling pool). Single source of
truth — previously duplicated as ``_WAR_UPDATE_LEAGUES`` in coc_cache.py and
cache_manager.py, and as ``_CWL_HARVEST_LEAGUES`` in QBhelperfunctions.py."""

PASSIVE_CLAN_REFRESH_INTERVAL_DAYS = 30
"""Days between opportunistic get_clan() refreshes for passively-tracked
(track_war_updates=False) clans that the CWL-group discovery graph never
revisits (QBhelperfunctions.refresh_stale_passive_clans). Catches a clan's
promotion to Master III+ even when none of its groupmates are ever reachable
this season — see CLAN_WAR_TRACKING.md write-path 8."""


# ============================================================================
# Cache and History Configuration
# ============================================================================

HISTORY_CACHE_TTL_SECONDS = 3600
"""Time-to-live for cached historical war data (1 hour)."""

BACKUP_FILE_RETENTION_COUNT = 3
"""Number of backup files to retain during atomic saves."""


# ============================================================================
# Message Management
# ============================================================================

PLAYERREGISTRATION_BUMP_COOLDOWN_SECONDS = 6 * 3600
"""Cooldown between playerregistration message bumps (6 hours)."""

MESSAGE_CLEANUP_STALE_DAYS = 7
"""Days after which orphaned message IDs are removed from cache."""


# ============================================================================
# Retry and Backoff Configuration
# ============================================================================

MAX_RETRY_BACKOFF_SECONDS = 3600
"""Maximum backoff time for exponential retry (1 hour)."""

INITIAL_RETRY_BACKOFF_SECONDS = 60
"""Initial backoff time for exponential retry (1 minute)."""

DEFAULT_RETRY_MAX_ATTEMPTS = 3
"""Default maximum retry attempts for transient failures."""


# ============================================================================
# Time Conversion Constants
# ============================================================================

SECONDS_PER_MINUTE = 60
"""Seconds in a minute."""

SECONDS_PER_HOUR = 3600
"""Seconds in an hour."""

SECONDS_PER_DAY = 86400
"""Seconds in a day."""

MINUTES_PER_HOUR = 60
"""Minutes in an hour."""


# ============================================================================
# War Simulation Constants (QBwarsim.py)
# ============================================================================

WAR_SIM_SAMPLE_SIZE = 10000
"""Number of simulations to run for war outcome prediction."""

WAR_SIM_DEBUG_SAMPLES = 10
"""Number of simulation results to log for debugging."""

WAR_SIM_EQUAL_TH_3STAR_RATE = 0.60
"""Probability of 3-star attack on equal Town Hall level."""

WAR_SIM_TH_ADVANTAGE_3STAR_RATE = 0.85
"""Probability of 3-star attack with Town Hall advantage."""

WAR_SIM_TH_DISADVANTAGE_3STAR_RATE = 0.10
"""Probability of 3-star attack with -2 Town Hall disadvantage."""


# ============================================================================
# Leaderboard Display Configuration
# ============================================================================

LEADERBOARD_MAX_PLAYERS = 50
"""Maximum players to display in a leaderboard (before splitting)."""

LEADERBOARD_PLAYER_NAME_MAX_LENGTH = 15
"""Maximum character length for player names in leaderboard."""


# ============================================================================
# Subscription Limits
# ============================================================================

DEFAULT_MAX_CLAN_SUBSCRIPTIONS = 7
"""Default maximum number of clan subscriptions per channel."""

MAX_CLAN_FAMILY_SIZE = 10
"""Maximum number of clans in a clan family."""


# ============================================================================
# Logging Configuration
# ============================================================================

LOG_ROTATION_WHEN = "midnight"
"""When to rotate log files (midnight daily)."""

LOG_ROTATION_INTERVAL = 1
"""Interval for log rotation (1 day)."""

LOG_BACKUP_COUNT = 7
"""Number of daily log files to keep."""

LOG_FORMAT_STRING = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
"""Standard log format string."""


# ============================================================================
# Notification Configuration
# ============================================================================

NOTIFICATION_BATCH_DELAY_SECONDS = 2
"""Delay between notification batches to avoid rate limits (default)."""

NOTIFICATION_MAX_RETRIES = 1
"""Maximum retry attempts for failed notification delivery (default)."""

NOTIFICATION_DM_TIMEOUT_SECONDS = 10
"""Timeout for sending DM notifications."""


# ============================================================================
# CWL Season Normalisation
# ============================================================================

def normalize_cwl_season(raw: str) -> str:
    """Normalise a raw CWL season string to a stable, unique key.

    The CoC API returns different formats for different CWL types/phases:
    - Regular monthly CWL  → ``"YYYY-MM"``  (e.g. ``"2026-05"``) once the
      league group is fully formed.
    - Regular monthly CWL, **sign-up/group-formation snapshot** → the API
      briefly returns a literal ``"YYYY-MM-DD"`` date (the group-creation day,
      which is always a Monday) instead of ``"YYYY-MM"`` while the group is
      still being assembled. Because matchmaking always starts on a Monday,
      this creation date can fall in the **last few days of the previous
      month** (e.g. ``"2026-06-29"`` for the July CWL) or the first few days
      of the target month. This is NOT a real mid-month event — it must be
      folded into the regular ``"YYYY-MM"`` key of the month the CWL is
      actually *for*, otherwise the season silently gets mislabeled and
      breaks month-prefix lookups (CWL group expansion, leaderboards, etc.).
    - Mid-month bonus CWL  → ``"YYYY-MM-DD"`` where the date is the day each
      individual clan *started* their war, so clans in the same event can get
      different values (e.g. ``"2026-06-16"`` vs ``"2026-06-17"``). Genuine
      bonus events start well inside the month (observed around day 14-20),
      clear of the month-boundary noise described above.

    Disambiguation heuristic for ``"YYYY-MM-DD"`` input:
    - Day-of-month ``<= 6``  → boundary snapshot for the *current* month's
      regular CWL → folded to that month's ``"YYYY-MM"``.
    - Day-of-month ``>= 25`` → boundary snapshot for *next* month's regular
      CWL (matchmaking Monday lands before month-end) → folded to next
      month's ``"YYYY-MM"``.
    - Day-of-month ``7-24``  → genuine mid-month bonus CWL → snapped to the
      **ISO Monday** of that week so all clans whose CWL started in the same
      Mon-Sun window share one key.

    Normalisation rules:
    - Empty string → returned unchanged.
    - ``YYYY-MM`` format → returned unchanged (regular monthly CWL).
    - ``YYYY-MM-DD`` format → see heuristic above.

    Examples::

        normalize_cwl_season("")            → ""
        normalize_cwl_season("2026-05")     → "2026-05"
        normalize_cwl_season("2026-06-29")  → "2026-07"      # month-end boundary → next month
        normalize_cwl_season("2026-07-02")  → "2026-07"      # month-start boundary → same month
        normalize_cwl_season("2026-06-16")  → "2026-06-15"   # genuine mid-month, Tuesday → Monday
        normalize_cwl_season("2026-06-17")  → "2026-06-15"   # genuine mid-month, Wednesday → Monday
    """
    if not raw:
        return raw
    parts = raw.split("-")
    if len(parts) == 2:
        return raw  # YYYY-MM — regular monthly CWL, keep as-is
    if len(parts) == 3:
        try:
            from datetime import date, timedelta
            dt = date.fromisoformat(raw)
        except ValueError:
            return raw  # Unparseable date — return raw unchanged

        if dt.day <= 6:
            # Group-formation snapshot taken just before/at the start of the
            # target month — the CWL is for THIS month.
            return f"{dt.year:04d}-{dt.month:02d}"
        if dt.day >= 25:
            # Group-formation snapshot taken in the last days of the prior
            # month (matchmaking Monday landed before month-end) — the CWL is
            # for NEXT month.
            if dt.month == 12:
                return f"{dt.year + 1:04d}-01"
            return f"{dt.year:04d}-{dt.month + 1:02d}"

        # Genuine mid-month bonus CWL — snap to the ISO Monday of that week.
        monday = dt - timedelta(days=dt.weekday())  # weekday(): Mon=0 … Sun=6
        return monday.isoformat()
    return raw
