"""
Application-wide constants for ClashControl.

This module centralizes all magic numbers and constant values used throughout the
application. By defining constants here, we:
- Make the code more maintainable and readable
- Provide a single source of truth for configuration values
- Enable easy adjustment of parameters without searching through code
- Document the meaning and purpose of each value

Usage:
    from clashcontrol.constants import DISCORD_MESSAGE_MAX_LENGTH, COC_API_RATE_LIMIT
    
    if len(message) > DISCORD_MESSAGE_MAX_LENGTH:
        # Split message
        pass
"""
from typing import Tuple

# ============================================================================
# ClashControl links
# ============================================================================

SUPPORT_INVITE_URL = "https://discord.gg/Eg3f7VQA2T"
"""Permanent invite to ClashControl's public support channel on The QCrew server (tracker #0135
follow-up). Same link as SUPPORT_URL in activity/client/src/landingPage.ts."""


def bot_install_url(application_id: object) -> str:
    """The "Add to server" link for this bot (Discord's OAuth2 install page, which applies the
    install settings from the Developer Portal). Shared by /about's fallback text and the greeting
    DM (tracker #0141) so the link is built in one place.

    Args:
        application_id: The bot's application id (client.application_id).
    """
    return f"https://discord.com/oauth2/authorize?client_id={application_id}"

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

WAR_SUMMARY_UNKNOWN = -1
"""Marker value "data not available" in a numeric war_summary column (2026-09-26).

Why it exists: wars from 2025-07-26 to 2026-01 survive only as legacy per-player rows in
war_attacks (the retired war_history table, copied over on 2026-03-07 with no raw war JSON left to
rebuild them from). Their war_summary rows were reconstructed from what those legacy rows DO hold
(backfill_legacy_war_summaries.py): opponent tag (from war_id), team size, attacks per member, CWL
flag/season, attacks used and opponent stars. Everything they do not hold is stored as -1:

    clan_stars       — legacy rows keep each player's star TOTAL, so several attacks on the same base
                       can't be deduplicated (the sum is exact in only ~5 % of regular wars)
    clan_destruction, opp_destruction, opp_attacks_used — never stored for these wars
    opponent_stars   — only for the bot's first week (before 2025-08-03), when stars conceded were
                       not recorded yet (9 wars); real for every later legacy war

`result` is '' for the same wars — the existing "no result known" convention for TEXT.

Rules for readers (see also .github/copilot-instructions.md, pitfall 45, and
clashcontrol/docs/DATABASE_ARCHITECTURE.md § war_summary: reconstructed legacy rows):
  - A value < 0 in these columns means "not available", never a real number. Real values are
    always >= 0, so `value >= 0` is the "is known" test.
  - SQL aggregates must exclude it: SUM(CASE WHEN col >= 0 THEN col ELSE 0 END), never a bare
    SUM(col) — a bare SUM silently subtracts 1 per legacy war.
  - Display it as "?" and never derive a result from it (classify_war_result() returns an
    "unknown" label when the deciding value is unavailable).
-1 rather than 0 so a reader that forgets the rule shows an obviously wrong number instead of a
plausible zero; rather than NULL because these columns are NOT NULL and relaxing that would mean
rebuilding war_summary in both the hot and the history DB (Cardinal Rule 1).
"""
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

CWL_LEAGUE_ORDER: Tuple[str, ...] = (
    "Bronze League III",
    "Bronze League II",
    "Bronze League I",
    "Silver League III",
    "Silver League II",
    "Silver League I",
    "Gold League III",
    "Gold League II",
    "Gold League I",
    "Crystal League III",
    "Crystal League II",
    "Crystal League I",
    "Master League III",
    "Master League II",
    "Master League I",
    "Champion League III",
    "Champion League II",
    "Champion League I",
    "Titan League III",
    "Titan League II",
    "Titan League I",
    "Legend League",
)
"""Canonical CWL league ladder, lowest to highest — used to sort clans by tier, highest first,
in the CWL Management embed and the web Activity's clan-config table (CWL_CLAN_CONFIG_ACTIVITY_PLAN.md
Phase E). Mirrors chart_clans_per_league.py's own LEAGUE_ORDER (kept separate there — that
module pulls in matplotlib, too heavy a dependency for this constants module).

Also the source clashcontrol/ui_cwl_roster.py's CWL_LEAGUE_RANKS (the target_league_rank/
preferred_league_rank picker list, reversed plus an "Unranked" placeholder) is derived from —
and, one level further out, what activity/client/src/playerPrefs.ts's own hand-duplicated TS
copy of that picker list must be kept in sync with by hand, since Python constants aren't
reachable from TypeScript. QBwarsim.py's CWL_LEAGUE_STAR_DISTRIBUTION also mirrors this same key
set (its own dict, star-percentage data rather than a pure ladder, so not derived from this
tuple directly). If CoC ever changes the league ladder again (tracker #0047: the Legend/Titan
tiers above Champion had been added upstream but were missing from the picker list — caught only
there, not here), update this tuple and its dependents together."""

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


# Days after a CWL season's start key beyond which that season's war window is definitively
# over (2026-08-22, tracker #0017). Measured against real war_summary data:
#   regular CWL "YYYY-MM"   → wars ran day 2 .. day 10-11 of the month
#   bonus  CWL "YYYY-MM-DD" → wars ran key+2 .. key+11 (observed: 2026-06-15 → 06-17..06-26)
# so 14 days clears the last real war of both shapes by 3-4 days. The only later rows found in
# ~1M CWL war_summary rows were 5 stragglers tagged 2026-07 that landed 2026-08-04..06 — noise,
# not a tail.
CWL_SEASON_WINDOW_DAYS = 14


def cwl_season_window_closed(cwl_season: str, now: object = None) -> bool:
    """True when *cwl_season*'s CWL war window is definitively over, so no clan in that season
    can still have an active league war.

    Added for tracker #0017's `cwl_ended` sweep. The sweep's other test — "every clan in the
    group has played all its rounds" — can only ever be satisfied for a group whose members we
    actually track: measured on real data, ~45% of groups have 6-8 of their 8 clans with no
    recorded wars at all (they are group-mates harvested from a subscribed clan's group, whose
    own wars nobody ever fetches). Those groups would stay unended forever, which is exactly the
    population still doing redundant get_league_war() walks. This is the time-based answer to the
    same question, and it resolves 100% of groups rather than ~55%.

    Both season-key shapes normalize_cwl_season() can produce are handled, and both mean "start
    of the CWL", so one rule covers them:
      - "YYYY-MM"     → regular monthly CWL, window starts at the 1st of that month
      - "YYYY-MM-DD"  → mid-month bonus CWL, window starts on that date

    Args:
        cwl_season: A season key as stored in cwl_league_groups.cwl_season.
        now: Optional datetime to evaluate against (tests); defaults to the current UTC time.

    Returns:
        True if the window closed at least CWL_SEASON_WINDOW_DAYS ago. False for an empty or
        unparseable key — never guess a season is over on a value we don't understand, since
        marking it ended suppresses live CWL detection for every clan in it.
    """
    from datetime import date, datetime, timedelta, timezone

    if not cwl_season:
        return False
    parts = cwl_season.split("-")
    try:
        if len(parts) == 2:
            start = date(int(parts[0]), int(parts[1]), 1)
        elif len(parts) == 3:
            start = date.fromisoformat(cwl_season)
        else:
            return False
    except ValueError:
        return False

    reference = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    return reference.date() >= start + timedelta(days=CWL_SEASON_WINDOW_DAYS)


# --- Clan Capital raid weekends (tracker #0115) ---------------------------------------------
# A raid season runs Fri 07:00 UTC -> Mon 07:00 UTC every week (COC_GAME_MECHANICS.md
# § Clan Capital Raid Weekends). Season keys are the ISO start timestamp, e.g.
# "2026-09-18T07:00:00Z" — identical across clans, so cross-clan lookups are plain equality.
RAID_SEASON_START_WEEKDAY = 4   # Friday (Monday == 0)
RAID_SEASON_START_HOUR_UTC = 7
RAID_SEASON_DURATION_HOURS = 72
# Base raid attacks per player; the API's bonusAttackLimit (0/1) is the EARNED extra attack on top.
RAID_BASE_ATTACK_LIMIT = 5


def current_raid_season_bounds(now: object = None) -> Tuple[str, str]:
    """Return the ISO (start, end) of the raid season that most recently started at or before
    *now* — i.e. this weekend's season while it runs, and last weekend's season from Mon 07:00
    until the next Fri 07:00.

    Args:
        now: Optional timezone-aware datetime (tests); defaults to the current UTC time.

    Returns:
        (season_start, season_end) as "YYYY-MM-DDTHH:MM:SSZ" strings.
    """
    from datetime import datetime, timedelta, timezone

    reference = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    reference = reference.astimezone(timezone.utc)
    days_back = (reference.weekday() - RAID_SEASON_START_WEEKDAY) % 7
    start = (reference - timedelta(days=days_back)).replace(
        hour=RAID_SEASON_START_HOUR_UTC, minute=0, second=0, microsecond=0
    )
    if start > reference:  # a Friday before 07:00 -> the season that started a week earlier
        start -= timedelta(days=7)
    end = start + timedelta(hours=RAID_SEASON_DURATION_HOURS)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return start.strftime(fmt), end.strftime(fmt)


def is_capital_raid_window(now: object = None) -> bool:
    """True from Fri 07:00 UTC up to (not including) Mon 07:00 UTC — the only time raid data can
    change. Outside it, the raid update step makes no API calls except catch-up for unfinalized
    seasons (tracker #0115)."""
    from datetime import datetime, timezone

    reference = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    _start, end = current_raid_season_bounds(reference)
    return reference.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") < end


def coc_timestamp_to_iso(raw: str) -> str:
    """Convert a CoC API timestamp ("20260918T070000.000Z") to the raid season key format
    ("2026-09-18T07:00:00Z"). Returns "" for an empty/unparseable value."""
    from datetime import datetime

    try:
        return datetime.strptime(raw, "%Y%m%dT%H%M%S.%fZ").strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return ""
