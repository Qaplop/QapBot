"""
War notification system for QapBot player reminders.

This module handles automated Discord DM notifications to players who haven't completed
their clan war attacks as the war deadline approaches. It integrates with the existing
player registration system (CACHE.user_accounts) and respects user subscription preferences.

Features:
- Automated war reminder DMs to registered players
- Configurable timing threshold (hours before war end)
- Per-player subscription control with notification modes
- Duplicate notification prevention via state tracking
- Graceful error handling for DM failures (blocked/disabled DMs)
- Rate limiting compliance with batch delays

Integration:
- Called from periodic_main() loop after war data updates
- Uses CACHE for war data and user account lookups
- Sends Discord DMs via bot instance from QBcore
- Tracks notification state to prevent duplicates

Notification Logic:
- Checks all active wars (state = "in_war")
- Identifies players with remaining attacks
- Maps CoC player tags to Discord IDs via CACHE.user_accounts
- Sends reminder DMs only to subscribed players
- Records notification in database to prevent re-sending

Error Handling:
- DM failures logged but don't block other notifications
- Missing player mappings logged for transparency
- War data errors gracefully skipped
- Rate limit delays between notifications

Example:
    from qapbot.war_notifications import check_wars_for_notifications
    
    # In periodic loop
    await check_wars_for_notifications()
"""

import logging
import asyncio
import re
import time
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime, timezone
import discord
from qapbot.exceptions import (
    NotificationError,
    NotificationSendError
)

from qapbot.config import CONFIG
from qapbot.cache_manager import CACHE
from qapbot.i18n import t
from qapbot.formatting import normalize_player_name

# Reverse-lookup tables rebuilt at the start of each notification cycle.
# Avoids O(n_users * n_members * n_wars) scanning in _get_player_discord_id /
# _get_buddy_watcher_discord_ids — reduced to O(1) per lookup.
_notification_player_index: Dict[str, str] = {}         # tag_clean → discord_id
_notification_watcher_index: Dict[str, List[str]] = {}  # tag_clean_upper → [discord_ids]


async def check_wars_for_notifications() -> None:
    """
    Main entry point for war notification system.
    
    Checks all active wars and sends reminder DMs to players who:
    - Have remaining attacks
    - Are registered in CACHE.user_accounts
    - Have notifications enabled
    - Haven't been notified yet for this war
    - War is within notification threshold (hours before end)
    
    Called from periodic_main() loop after war data updates.
    
    Behavior:
        - Iterates through all temp war data files
        - Filters wars within notification time window
        - Identifies players needing reminders
        - Sends batched DMs with rate limit delays
        - Updates notification state to prevent duplicates
        - SKIPPED in DEV mode to prevent spam during development
    
    Error Handling:
        - Individual notification failures don't block others
        - Missing war data files are skipped with logging
        - DM failures (blocked/disabled) are logged but continue
        - Cache save failures are logged as critical
    
    Rate Limiting:
        - Adds delay between DMs to respect Discord rate limits
        - Configurable via CONFIG.notification_batch_delay
    
    Example:
        # In QapBot.py periodic_main()
        await check_wars_for_notifications()
    """
    try:
        # Skip notifications in DEV mode
        if CONFIG.is_dev_mode:
            logging.debug("🔕 War notification check skipped (DEV mode)")
            return

        # Skip entirely when Discord is in a known outage — avoids burning
        # the DM send budget against a service that will reject every request.
        from qapbot.discord_health import is_discord_available
        if not is_discord_available():
            logging.debug("[DISCORD-HEALTH] Skipping war notifications this cycle (Discord outage).")
            return
        
        logging.debug("🔔 Starting war notification check...")

        # Overall timing so a slow cycle can be pinpointed at a glance from
        # plain INFO logs (no need to re-enable DEBUG on PROD to diagnose).
        _fn_t0 = time.monotonic()

        # Build reverse lookup indices once — O(n_users) total instead of
        # O(n_users * n_members * n_wars) from per-member linear scans.
        _t_index0 = time.monotonic()
        global _notification_player_index, _notification_watcher_index
        _notification_player_index = {}
        _notification_watcher_index = {}
        for _disc_id, _udata in CACHE.user_accounts.items():
            _nsettings = _udata.get("notification_settings", {})
            if _nsettings.get("war_reminders", False):
                for _p in _udata.get("players", []):
                    _tc = _p.get("player_tag", "").lstrip("#")
                    if _tc:
                        _notification_player_index[_tc] = _disc_id
            for _wp in _udata.get("watched_players", []):
                _tc = _wp.get("player_tag", "").lstrip("#").upper()
                if _tc:
                    _notification_watcher_index.setdefault(_tc, []).append(_disc_id)
        _index_elapsed = time.monotonic() - _t_index0

        # Yield to the event loop after the synchronous index-building loop.
        # On slow hardware (server-machine ARM/Celeron CPU) iterating CACHE.user_accounts can take
        # several seconds, starving the event loop of Discord interactions.
        await asyncio.sleep(0)

        # Get all active wars — zero file I/O, but CPU-bound: regex parsing +
        # dict/list building per in-war clan, with no yield points inside the
        # loop. At normal scale this is negligible; during CWL, active_wars
        # can reach 15k+ and this becomes a genuine multi-second synchronous
        # block — confirmed via prod [NOTIFY-TIMING] logs
        # (get_active_wars=15.680s for active_wars=17302 on the server-machine
        # Celeron CPU), which froze the event loop for that whole duration
        # (no Discord heartbeat/interactions processed). Run in a worker
        # thread so the event loop stays responsive while this runs.
        # (This exact asyncio.to_thread() wrapping existed here before and was
        # removed once _get_active_wars() became zero-I/O, on the assumption
        # that "zero I/O" also meant "fast enough to stay synchronous" — true
        # at the scale measured then, not at current CWL-season scale.)
        _t0 = time.monotonic()
        active_wars = await asyncio.to_thread(_get_active_wars)
        _get_wars_elapsed = time.monotonic() - _t0
        logging.debug(f"[NOTIFY] wars loaded in {_get_wars_elapsed:.3f}s, {len(active_wars)} active war(s)")

        # Extra defensive yield — asyncio.to_thread() above already lets the
        # event loop run other coroutines while _get_active_wars() executes
        # in its worker thread, but this costs nothing and adds one more
        # scheduling opportunity before the war_loop below starts.
        await asyncio.sleep(0)
        
        if not active_wars:
            logging.debug("No active wars found for notification check")
            logging.info(
                "[NOTIFY-TIMING] index_build=%.3fs get_active_wars=%.3fs war_loop=0.000s "
                "total=%.3fs (active_wars=0)",
                _index_elapsed, _get_wars_elapsed, time.monotonic() - _fn_t0,
            )
            return
        
        logging.debug(f"Found {len(active_wars)} active war(s) to check")
        
        # Check each war for notification opportunities
        # NOTE: We process ALL wars here - threshold filtering happens separately:
        # - DMs are sent based on individual player notification preferences (no threshold)
        # - Channel notifications are filtered by per-guild threshold in _send_channel_war_notification()
        notifications_sent = 0
        channel_notifications_sent = 0
        _war_loop_t0 = time.monotonic()
        _slowest_war: Optional[Tuple[str, float]] = None
        for clan_tag, war_id, war_data in active_wars:
            _war_t0 = time.monotonic()
            try:
                dm_count, channel_count = await _process_war_for_notifications(clan_tag, war_id, war_data)
                notifications_sent += dm_count
                channel_notifications_sent += channel_count
            except Exception as e:
                logging.error(f"Error processing war {war_id} for notifications: {e}")
                # Continue with other wars even if one fails
                continue
            finally:
                _war_elapsed = time.monotonic() - _war_t0
                if _slowest_war is None or _war_elapsed > _slowest_war[1]:
                    _slowest_war = (war_id, _war_elapsed)
                # Yield to the event loop between wars.
                # _process_war_for_notifications only yields when it actually reaches a
                # Discord API call (e.g. channel send).  For wars with no subscribed guilds
                # the entire call returns synchronously, so without this yield the outer
                # loop runs as one uninterrupted synchronous burst — causing Discord
                # interactions to expire on slow hardware.
                await asyncio.sleep(0)

        logging.info(
            "[NOTIFY-TIMING] index_build=%.3fs get_active_wars=%.3fs war_loop=%.3fs "
            "total=%.3fs (active_wars=%d, slowest=%s)",
            _index_elapsed, _get_wars_elapsed, time.monotonic() - _war_loop_t0,
            time.monotonic() - _fn_t0, len(active_wars),
            f"{_slowest_war[0]}:{_slowest_war[1]:.3f}s" if _slowest_war else "n/a",
        )

        if notifications_sent > 0 or channel_notifications_sent > 0:
            if notifications_sent > 0:
                logging.info(f"✅ Sent {notifications_sent} DM war reminder notification(s)")
            if channel_notifications_sent > 0:
                logging.info(f"✅ Sent {channel_notifications_sent} channel war notification(s)")
            # Notification state already persisted via write-through pattern
        else:
            logging.debug("No notifications sent this cycle")
    
    except Exception as e:
        raise NotificationError(
            "Failed to check wars for notifications",
            context={"error": str(e)}
        )


def _get_active_wars() -> List[Tuple[str, str, Dict[str, Any]]]:
    """
    Retrieve all active wars from in-memory caches only (zero filesystem I/O).

    Uses CACHE.in_war_clan_tags for an O(n_in_war) pre-filter instead of
    iterating all 18k+ temp_war_metadata entries. The in_war_clan_tags set is
    maintained by save_war_object() and load_all_temp_war_stats().

    Returns:
        List of tuples: (clan_tag, war_id, war_data)
        war_id format: "{CLAN_CLEAN}_{OPP_CLEAN}"

    Filters:
        - Clan must be in CACHE.in_war_clan_tags (state == "in_war")
        - War end time must be in the future (computed from actual UTC datetime,
          not the stale seconds_until snapshot stored in the Timestamp string)
        - Threshold filtering done per-guild in _process_war_for_notifications

    Performance:
        O(n_in_war) set iteration instead of O(n_all_temp) full metadata scan.
        hours_remaining is stored in war_data so downstream functions never
        re-parse the same Timestamp string.

    Example:
        active_wars = _get_active_wars()
        # Returns: [("#2CLANTAG01", "2CLANTAG01_2OPPTAG02", {...}), ...]
    """
    active_wars: List[Tuple[str, str, Dict[str, Any]]] = []

    for clan_tag in list(CACHE.in_war_clan_tags):
        meta = CACHE.temp_war_metadata.get(clan_tag)
        if not meta:
            continue

        # State double-check: the set can lag behind metadata if set_temp_war_stats
        # updated metadata without going through save_war_object.  Self-heal and skip.
        if meta.get("state") != "in_war":
            CACHE.in_war_clan_tags.discard(clan_tag)
            continue

        temp_stats = CACHE.temp_war_stats.get(clan_tag, {})
        if not temp_stats:
            continue

        # WarID format in temp_war_stats: "{OPP_CLEAN}_{YYYYMMDDHHMM}"
        first = next(iter(temp_stats.values()), None)
        if not first:
            continue

        full_war_id = first.get("WarID", "")
        if not full_war_id or "_" not in full_war_id:
            continue

        attacks_per_member = int(first.get("Max_Attacks", 2))
        opp_clean = full_war_id.split("_")[0]  # opponent tag without '#'

        # Notification war_id: "{CLAN_CLEAN}_{OPP_CLEAN}"
        war_id = f"{clan_tag.lstrip('#')}_{opp_clean}"

        # Compute actual UTC-based hours remaining (not the stale seconds_until snapshot).
        # Wars that ended since the last save_war_object call are correctly excluded here.
        hours_remaining = _get_hours_until_war_end({"end_time": meta.get("end_time", "")})
        if hours_remaining is None or hours_remaining <= 0:
            continue

        # Clan/opponent names from name cache (no I/O)
        clan_data = CACHE.clan_name_cache.get(clan_tag)
        clan_name = clan_data.get("name", "Unknown") if isinstance(clan_data, dict) else "Unknown"
        opp_data = CACHE.clan_name_cache.get("#" + opp_clean)
        opp_name = opp_data.get("name", "Unknown") if isinstance(opp_data, dict) else "Unknown"

        # Build member list from in-memory stats
        members: List[Dict[str, Any]] = [
            {
                "tag": stats.get("PlayerID", ""),
                "name": stats.get("Player", ""),
                "attacks": [None] * int(stats.get("Attacks", 0)),  # len == attacks used
            }
            for stats in temp_stats.values()
            if stats.get("PlayerID")
        ]

        war_type = "cwl" if attacks_per_member == 1 else "regular"

        war_data: Dict[str, Any] = {
            "state": "in_war",
            "clan": {"tag": clan_tag, "name": clan_name, "members": members},
            "opponent": {"tag": "#" + opp_clean, "name": opp_name},
            "attacks_per_member": attacks_per_member,
            "end_time": meta.get("end_time", ""),
            "hours_remaining": hours_remaining,  # cached — downstream functions must not re-parse
            "type": war_type,
            "is_cwl": war_type == "cwl",
        }

        active_wars.append((clan_tag, war_id, war_data))
        logging.debug(f"[NOTIFY] War {war_id} is active: {hours_remaining:.1f}h remaining")

    return active_wars


def parse_war_timestamp_field(ts_str: Any) -> Tuple[Optional[datetime], Optional[int]]:
    """
    Parse a QapBot war-JSON Timestamp string, e.g.:
    "<Timestamp time=datetime.datetime(2025, 8, 29, 13, 20, 53) seconds_until=-72658>"

    Shared parser for the embedded datetime plus the stale seconds_until snapshot —
    previously duplicated (with slightly different strictness) between this module
    and QBhelperfunctions.generate_war_info_text()'s function-local copy.

    Args:
        ts_str: Raw Timestamp repr string (or any string containing one).

    Returns:
        (end_dt, seconds_until) tuple:
        - end_dt: timezone-aware (UTC) datetime parsed from the embedded
          datetime.datetime(...) call, or None if that portion is missing/malformed.
          Seconds default to 0 if the source only provides (year, month, day, hour, minute).
        - seconds_until: the stale seconds_until snapshot from the string (accurate only
          at serialization time), or None if absent.
    """
    if not isinstance(ts_str, str) or not ts_str:
        return None, None

    dt_obj: Optional[datetime] = None
    m = re.search(
        r"datetime\.datetime\((\d+),\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)(?:,\s*(\d+))?\)",
        ts_str,
    )
    if m:
        year, month, day, hour, minute = (int(m.group(i)) for i in range(1, 6))
        second = int(m.group(6)) if m.group(6) else 0
        dt_obj = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)

    seconds_until: Optional[int] = None
    m2 = re.search(r"seconds_until=([-\d]+)", ts_str)
    if m2:
        seconds_until = int(m2.group(1))

    return dt_obj, seconds_until


def _get_hours_until_war_end(war_data: Dict[str, Any]) -> Optional[float]:
    """
    Calculate hours remaining until war end.

    Args:
        war_data: War data dictionary from CACHE.war_info

    Returns:
        Hours remaining as float, or None if unable to parse.
        Returns a negative value when the war has already ended.

    Note:
        Parses the actual UTC datetime from the coc.py Timestamp string so the result
        is accurate at any query time — independent of when the file was last written.
        Format: "<Timestamp time=datetime.datetime(Y, M, D, H, Mm[, S]) seconds_until=N>"
        The 'seconds_until' field in the string is stale (recorded at serialization time)
        and is only used as a fallback when the datetime component is absent.

    Example:
        hours = _get_hours_until_war_end(war_data)
        # Returns: 4.5 (if 4.5 hours remaining), or -1.2 (if war ended 1.2 h ago)
    """
    try:
        end_time_str = war_data.get("end_time", "")
        if not end_time_str:
            return None

        # Primary path: parse the actual UTC end datetime.
        # 'seconds_until' in the stored string was accurate only at serialization time;
        # using the real datetime avoids false positives for wars that ended long ago
        # but whose temp files haven't been archived yet (stale between bot cycles).
        end_dt, seconds_until = parse_war_timestamp_field(end_time_str)
        if end_dt is not None:
            return (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600

        # Fallback: use seconds_until (only accurate right after serialization).
        if seconds_until is not None:
            return seconds_until / 3600

        return None
    except (ValueError, AttributeError, IndexError) as e:
        logging.warning(f"Failed to parse war end time: {e}")
        return None


def _get_war_id(clan_tag: str, war_data: Dict[str, Any]) -> str:
    """
    Generate unique war identifier for notification state tracking.
    
    Args:
        clan_tag: Clan tag (e.g., "#2C9UR9GJY")
        war_data: War data dictionary containing opponent info
    
    Returns:
        War ID string: "{clan_tag}_{opponent_tag}" (without # symbols)
    
    Example:
        war_id = _get_war_id("#2C9UR9GJY", war_data)
        # Returns: "2CLANTAG01_2OPPTAG02"
    """
    clan_tag_clean = clan_tag.lstrip("#")
    opponent_tag = war_data.get("opponent", {}).get("tag", "UNKNOWN").lstrip("#")
    return f"{clan_tag_clean}_{opponent_tag}"


async def _process_war_for_notifications(clan_tag: str, war_id: str, war_data: Dict[str, Any]) -> Tuple[int, int]:
    """
    Process a single war and send notifications to players needing reminders.
    
    Args:
        clan_tag: Clan tag (e.g., "#2C9UR9GJY")
        war_id: Unique war identifier for state tracking
        war_data: War data dictionary from CACHE.war_info
    
    Returns:
        Tuple of (dm_notifications_sent, channel_notifications_sent)
    
    Logic:
        1. Get players with remaining attacks
        2. Filter to registered players with notifications enabled
        3. Skip already-notified players
        4. Send DM reminders with rate limiting
        5. Send channel notification (once per war per guild)
        5. Update notification state
    
    Error Handling:
        - Individual DM failures logged but don't stop batch
        - Missing player data logged for transparency
    
    Example:
        dm_sent, ch_sent = await _process_war_for_notifications("#2CLANTAG01", "2CLANTAG01_2OPPTAG02", war_data)
        # Returns: (3, 1) if 3 DMs and 1 channel notification sent
    """
    notifications_sent = 0
    channel_notifications_sent = 0
    _proc_t0 = time.monotonic()

    # Get players needing DM reminders (filtered by individual user preferences)
    players_to_notify = _get_players_needing_reminders(clan_tag, war_id, war_data)
    _t_reminders = time.monotonic() - _proc_t0

    # Yield between the two synchronous member-scan functions.  Each iterates
    # all clan members with dict lookups + string ops; with tracemalloc active
    # on ARM CPUs the combined sync gap can reach 500ms-2s per war.
    await asyncio.sleep(0)

    # Get ALL players with attacks remaining for channel notification
    # (independent of individual DM preferences - filtered by guild threshold in channel function)
    _t_channel0 = time.monotonic()
    players_for_channel = _get_players_with_attacks_remaining(war_data)
    _t_attacks_remaining = time.monotonic() - _t_channel0
    
    if not players_to_notify:
        logging.debug(f"No players need DM reminders for war {war_id}")
        # Still check for channel notification even if no DMs to send
        _t_send0 = time.monotonic()
        try:
            channel_notifications_sent = await _send_channel_war_notification(clan_tag, war_data, players_for_channel)
        except Exception as e:
            logging.error(f"Failed to send channel notification for war {war_id}: {e}")
            channel_notifications_sent = 0
        # Only worth logging when this single war took long enough to matter
        # (>0.3s) — keeps normal-speed cycles quiet while still pinpointing
        # the culprit sub-step the next time a cycle is slow.
        _total_elapsed = time.monotonic() - _proc_t0
        if _total_elapsed > 0.3:
            logging.info(
                "[NOTIFY-WAR-TIMING] war=%s get_reminders=%.3fs get_attacks_remaining=%.3fs "
                "send_channel=%.3fs total=%.3fs",
                war_id, _t_reminders, _t_attacks_remaining,
                time.monotonic() - _t_send0, _total_elapsed,
            )
        return (0, channel_notifications_sent)
    
    logging.info(f"📢 Found {len(players_to_notify)} player(s) to notify for war {war_id}")
    
    # Group players by discord_id to send one message per user
    users_to_notify: Dict[str, List[Dict[str, Any]]] = {}
    for player_info in players_to_notify:
        discord_id = player_info["discord_id"]
        if not str(discord_id).strip().isdigit():
            player_name = player_info.get("player_name", "?")
            logging.debug(f"Skipping war reminder for {player_name}: discord_id is '{discord_id}' (account not linked)")
            continue
        if discord_id not in users_to_notify:
            users_to_notify[discord_id] = []
        users_to_notify[discord_id].append(player_info)
    
    logging.info(f"📨 Sending to {len(users_to_notify)} Discord user(s)")
    
    # Send notifications with rate limiting
    for discord_id, player_list in users_to_notify.items():
        try:
            success = await _send_aggregated_war_reminder_dm(discord_id, player_list, war_data)
            
            if success:
                # Record notification in state for all players
                for player_info in player_list:
                    if player_info.get("is_buddy", False):
                        _record_buddy_notification(war_id, player_info)
                    else:
                        await _record_notification(war_id, player_info)
                notifications_sent += 1
                
                # Rate limit delay
                if notifications_sent < len(users_to_notify):
                    await asyncio.sleep(CONFIG.notification_batch_delay)
        
        except discord.DiscordServerError as e:
            logging.warning(f"Transient Discord server error for user {discord_id} (HTTP {e.status}): skipping this cycle")
            continue
        except Exception as e:
            logging.error(f"Failed to send DM to Discord user {discord_id}: {e}")
            # Continue with other users
            continue
    
    # Send channel notification if enabled for this war's clan
    # Use players_for_channel (not players_to_notify) to be independent of DM filtering
    try:
        channel_notifications_sent = await _send_channel_war_notification(clan_tag, war_data, players_for_channel)
    except Exception as e:
        logging.error(f"Error sending channel notification for war {war_id}: {e}")
    
    return (notifications_sent, channel_notifications_sent)


def _get_players_needing_reminders(clan_tag: str, war_id: str, war_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Identify players who need war reminders based on attack status and subscription.
    
    Args:
        clan_tag: Clan tag (e.g., "#2C9UR9GJY")
        war_id: Unique war identifier
        war_data: War data dictionary
    
    Returns:
        List of player info dicts containing:
        - player_tag: CoC player tag
        - player_name: Player display name
        - discord_id: Discord user ID
        - attacks_remaining: Number of attacks not yet used
        - clan_name: Clan name for context
        - opponent_name: Opponent clan name
        - hours_remaining: Time until war end
    
    Filters:
        - Player has attacks remaining (< attacks_per_member)
        - Player registered in CACHE.user_accounts
        - Player has notifications enabled
        - Player not already notified for this war
    
    Example:
        players = _get_players_needing_reminders("#2CLANTAG01", "2CLANTAG01_2OPPTAG02", war_data)
        # Returns: [{"player_tag": "#2CLANTAG01", "discord_id": "123456789012345678", ...}]
    """
    players_to_notify: List[Dict[str, Any]] = []
    
    clan_members = war_data.get("clan", {}).get("members", [])
    attacks_per_member = war_data.get("attacks_per_member", 2)
    clan_name = war_data.get("clan", {}).get("name", "Unknown")
    opponent_name = war_data.get("opponent", {}).get("name", "Unknown")
    hours_remaining: Optional[float] = war_data.get("hours_remaining")
    if hours_remaining is None:
        hours_remaining = _get_hours_until_war_end(war_data)

    if hours_remaining is None:
        return []

    for member in clan_members:
        player_tag = member.get("tag", "")
        player_name = member.get("name", "Unknown")
        attacks_used = len(member.get("attacks", []))
        attacks_remaining = attacks_per_member - attacks_used

        if attacks_remaining <= 0:
            continue  # Player completed all attacks

        # Check if player registered and has notifications enabled
        discord_id = _get_player_discord_id(player_tag)
        if not discord_id:
            continue  # Player not registered or notifications disabled
        
        # Check notification_type filter (all_wars vs cwl_only)
        if not _should_notify_for_war_type(discord_id, war_data):
            continue  # War type doesn't match user's notification preferences
        
        # Check notification eligibility based on mode (once vs repeated)
        if not _should_send_notification(war_id, player_tag, discord_id, hours_remaining):
            continue  # Already notified or not time for next notification yet

        players_to_notify.append({
            "player_tag": player_tag,
            "player_name": player_name,
            "discord_id": discord_id,
            "attacks_remaining": attacks_remaining,
            "clan_name": clan_name,
            "opponent_name": opponent_name,
            "hours_remaining": hours_remaining,
            "is_buddy": False,
        })

    # ── Save-your-Buddy: also notify users who are watching this player ──
    # Re-scan war members to find buddy watchers
    for member in clan_members:
        player_tag = member.get("tag", "")
        player_name = member.get("name", "Unknown")
        attacks_used = len(member.get("attacks", []))
        attacks_remaining = attacks_per_member - attacks_used

        if attacks_remaining <= 0:
            continue  # Player completed all attacks

        # Find all watchers for this player tag
        watcher_ids = _get_buddy_watcher_discord_ids(player_tag)
        for watcher_discord_id in watcher_ids:
            # Skip if this watcher's own account already covers this player (avoid dup)
            already_own = any(
                pi["discord_id"] == watcher_discord_id and pi["player_tag"].lstrip("#") == player_tag.lstrip("#")
                for pi in players_to_notify
            )
            if already_own:
                continue

            # Check watcher has notifications enabled
            watcher_data = CACHE.user_accounts.get(watcher_discord_id, {})
            notif_settings = watcher_data.get("notification_settings", {})
            if not notif_settings.get("war_reminders", False):
                continue

            # Check war type filter for watcher
            if not _should_notify_for_war_type(watcher_discord_id, war_data):
                continue

            # Check buddy notification eligibility (separate tracking from own accounts)
            if not _should_send_buddy_notification(war_id, player_tag, watcher_discord_id, hours_remaining):
                continue

            players_to_notify.append({
                "player_tag": player_tag,
                "player_name": player_name,
                "discord_id": watcher_discord_id,  # watcher, not the player's owner
                "attacks_remaining": attacks_remaining,
                "clan_name": clan_name,
                "opponent_name": opponent_name,
                "hours_remaining": hours_remaining,
                "is_buddy": True,
            })

    return players_to_notify


def _get_players_with_attacks_remaining(war_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Get ALL players with remaining attacks (for channel notifications).
    
    This function is simpler than _get_players_needing_reminders() because it
    doesn't filter by individual user DM preferences. It's used for channel
    notifications which operate on guild-level threshold, not individual preferences.
    
    Args:
        war_data: War data dictionary
    
    Returns:
        List of player info dicts containing:
        - player_tag: CoC player tag
        - player_name: Player display name
        - attacks_remaining: Number of attacks not yet used
        - clan_name: Clan name for context
        - opponent_name: Opponent clan name
        - hours_remaining: Time until war end
    
    Filters:
        - Player has attacks remaining (< attacks_per_member)
    
    Example:
        players = _get_players_with_attacks_remaining(war_data)
        # Returns: [{"player_tag": "#2CLANTAG01", "player_name": "John", "attacks_remaining": 2, ...}]
    """
    players_with_attacks: List[Dict[str, Any]] = []
    
    clan_members = war_data.get("clan", {}).get("members", [])
    attacks_per_member = war_data.get("attacks_per_member", 2)
    clan_name = war_data.get("clan", {}).get("name", "Unknown")
    opponent_name = war_data.get("opponent", {}).get("name", "Unknown")
    hours_remaining: Optional[float] = war_data.get("hours_remaining")
    if hours_remaining is None:
        hours_remaining = _get_hours_until_war_end(war_data)

    if hours_remaining is None:
        return []

    for member in clan_members:
        player_tag = member.get("tag", "")
        player_name = member.get("name", "Unknown")
        attacks_used = len(member.get("attacks", []))
        attacks_remaining = attacks_per_member - attacks_used
        
        if attacks_remaining <= 0:
            continue  # Player completed all attacks
        
        players_with_attacks.append({
            "player_tag": player_tag,
            "player_name": player_name,
            "attacks_remaining": attacks_remaining,
            "clan_name": clan_name,
            "opponent_name": opponent_name,
            "hours_remaining": hours_remaining,
        })
    
    return players_with_attacks


def _should_notify_for_war_type(discord_id: str, war_data: Dict[str, Any]) -> bool:
    """
    Check if user should receive notifications for this war type.
    
    Args:
        discord_id: Discord user ID
        war_data: War data dictionary containing type/is_cwl fields
    
    Returns:
        True if user should be notified, False otherwise
    
    Logic:
        - notification_type = "all_wars": notify for all wars (default)
        - notification_type = "cwl_only": notify only for CWL wars
    
    Example:
        should_notify = _should_notify_for_war_type("123456789012345678", war_data)
    """
    user_data = CACHE.user_accounts.get(discord_id)
    if not user_data:
        return False
    
    notif_settings = user_data.get("notification_settings", {})
    notif_type = notif_settings.get("notification_type", "all_wars")
    
    # If set to all_wars, always notify
    if notif_type == "all_wars":
        return True
    
    # If set to cwl_only, check if this is a CWL war
    if notif_type == "cwl_only":
        war_type = war_data.get("type", "").lower()
        is_cwl = war_data.get("is_cwl", False)
        return war_type == "cwl" or is_cwl is True
    
    # Unknown notification_type, default to notify
    return True


def _get_player_discord_id(player_tag: str) -> Optional[str]:
    """
    Map CoC player tag to Discord user ID using CACHE.user_accounts.
    
    Args:
        player_tag: Clash of Clans player tag (e.g., "#2CLANTAG01")
    
    Returns:
        Discord user ID as string, or None if not found or notifications disabled
    
    Filters:
        - Player must be registered in CACHE.user_accounts
        - Player must have notification_settings.war_reminders = True
    
    Note:
        Uses CACHE.user_accounts for lookups (loaded from database)
    
    Example:
        discord_id = _get_player_discord_id("#2CLANTAG01")
        # Returns: "123456789012345678" or None
    """
    player_tag_clean = player_tag.lstrip("#")
    return _notification_player_index.get(player_tag_clean)


def _get_buddy_watcher_discord_ids(player_tag: str) -> List[str]:
    """
    Find all Discord users who are watching a given CoC player tag as a buddy.

    Args:
        player_tag: Clash of Clans player tag (e.g., "#2CLANTAG01")

    Returns:
        List of Discord user IDs (as strings) of watchers whose notifications are enabled.

    Note:
        Uses CACHE.user_accounts and checks each user's "watched_players" list.
    """
    player_tag_clean = player_tag.lstrip("#").upper()
    return list(_notification_watcher_index.get(player_tag_clean, []))


def _should_send_buddy_notification(war_id: str, player_tag: str, watcher_discord_id: str, hours_remaining: float) -> bool:
    """
    Determine whether a buddy notification should be sent to a watcher.

    Uses the watcher's own notification settings (mode, hours_before_end).
    Tracks separately from own-account notifications in CACHE.notification_state
    under the "buddy_notifications" section, keyed by "{discord_id}_{player_tag}".

    Args:
        war_id: Unique war identifier
        player_tag: CoC player tag being watched
        watcher_discord_id: Discord user ID of the watcher
        hours_remaining: Hours until war ends

    Returns:
        True if notification should be sent, False otherwise.
    """
    user_data = CACHE.user_accounts.get(watcher_discord_id)
    if not user_data:
        return False

    notif_settings = user_data.get("notification_settings", {})
    notif_mode = notif_settings.get("notification_mode", "repeated")
    hours_before_end = notif_settings.get("hours_before_end", 4)

    if hours_remaining > hours_before_end:
        return False

    player_tag_clean = player_tag.lstrip("#").upper()
    buddy_key = f"{watcher_discord_id}_{player_tag_clean}"

    buddy_state = CACHE.notification_state.get(war_id, {}).get("buddy_notifications", {})
    watcher_state = buddy_state.get(buddy_key)

    if notif_mode == "once":
        return watcher_state is None

    # Repeated mode: same interval logic as _should_send_notification
    if watcher_state is None:
        return True

    last_notif_time_str = watcher_state.get("notification_time")
    if not last_notif_time_str:
        return True

    try:
        last_notif_time = datetime.strptime(last_notif_time_str, "%Y-%m-%d %H:%M:%S")
        hours_since_last = (datetime.now() - last_notif_time).total_seconds() / 3600

        if hours_remaining > 2:
            required_interval = 2.0
        elif hours_remaining > 0.5:
            required_interval = 1.0
        else:
            required_interval = 0.5

        return hours_since_last >= required_interval
    except Exception as e:
        logging.warning(f"Error parsing buddy notification time for {buddy_key}: {e}")
        return True


def _record_buddy_notification(war_id: str, player_info: Dict[str, Any]) -> None:
    """
    Record a buddy notification in CACHE (in-memory; not persisted to DB).

    Tracks per-watcher state under
    CACHE.notification_state[war_id]["buddy_notifications"]["{discord_id}_{player_tag}"].

    Args:
        war_id: Unique war identifier
        player_info: Player info dict with is_buddy=True, discord_id=watcher_id
    """
    player_tag_clean = player_info["player_tag"].lstrip("#").upper()
    watcher_discord_id = player_info["discord_id"]
    buddy_key = f"{watcher_discord_id}_{player_tag_clean}"

    if war_id not in CACHE.notification_state:
        CACHE.notification_state[war_id] = {"notified_players": {}, "buddy_notifications": {}}

    if "buddy_notifications" not in CACHE.notification_state[war_id]:
        CACHE.notification_state[war_id]["buddy_notifications"] = {}

    CACHE.notification_state[war_id]["buddy_notifications"][buddy_key] = {
        "player_name": player_info["player_name"],
        "watcher_discord_id": watcher_discord_id,
        "notification_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "attacks_remaining": player_info["attacks_remaining"],
    }
    # Note: buddy notification state is not persisted to DB;
    # re-sending after a bot restart during an ongoing war is an acceptable edge case.


def _is_already_notified(war_id: str, player_tag: str) -> bool:  # type: ignore[misc]
    """
    Check if player has already been notified for this war.
    
    Args:
        war_id: Unique war identifier
        player_tag: CoC player tag (e.g., "#2CLANTAG01")
    
    Returns:
        True if notification already sent, False otherwise
    
    Note:
        Uses CACHE.notification_state for lookup
    
    Example:
        already_sent = _is_already_notified("2CLANTAG01_2OPPTAG02", "#2CLANTAG01")
        # Returns: True or False
    """
    player_tag_clean = player_tag.lstrip("#")
    
    war_notifications = CACHE.notification_state.get(war_id, {}).get("notified_players", {})
    return player_tag_clean in war_notifications


def _should_send_notification(war_id: str, player_tag: str, discord_id: str, hours_remaining: float) -> bool:
    """
    Determine if notification should be sent based on mode (once vs repeated) and timing.
    
    Args:
        war_id: Unique war identifier
        player_tag: CoC player tag
        discord_id: Discord user ID
        hours_remaining: Hours until war ends
    
    Returns:
        True if notification should be sent, False otherwise
    
    Logic:
        - Check user's hours_before_end preference (default: 4 hours)
        - Once mode: Send only if never notified before AND within threshold
        - Repeated mode: Send based on time intervals:
          * Every 2 hours when > 2h remaining
          * Every hour when 0.5h < remaining <= 2h
          * At 30 minutes before war end
    
    Example:
        should_send = _should_send_notification("2CLANTAG01_2OPPTAG02", "#2CLANTAG01", "123456789012345678", 3.5)
    """
    user_data = CACHE.user_accounts.get(discord_id)
    if not user_data:
        return False
    
    notif_settings = user_data.get("notification_settings", {})
    notif_mode = notif_settings.get("notification_mode", "repeated")
    hours_before_end = notif_settings.get("hours_before_end", 4)
    
    # Check if war is within user's notification threshold
    if hours_remaining > hours_before_end:
        logging.debug(f"War not within user threshold: {hours_remaining:.1f}h > {hours_before_end}h")
        return False
    
    player_tag_clean = player_tag.lstrip("#")
    war_state = CACHE.notification_state.get(war_id, {}).get("notified_players", {})
    player_state = war_state.get(player_tag_clean)
    
    # Once mode: only send if never notified
    if notif_mode == "once":
        return player_state is None
    
    # Repeated mode: check timing intervals
    if player_state is None:
        # First notification - already checked threshold above
        return True
    
    # Get last notification time
    last_notif_time_str = player_state.get("notification_time")
    if not last_notif_time_str:
        return True  # No timestamp, allow notification
    
    try:
        last_notif_time = datetime.strptime(last_notif_time_str, "%Y-%m-%d %H:%M:%S")
        hours_since_last = (datetime.now() - last_notif_time).total_seconds() / 3600
        
        # Determine required interval based on time remaining
        if hours_remaining > 2:
            required_interval = 2.0  # Every 2 hours
        elif hours_remaining > 0.5:
            required_interval = 1.0  # Every hour
        else:
            required_interval = 0.5  # Every 30 minutes (final reminder)
        
        # Send if enough time has passed since last notification
        if hours_since_last >= required_interval:
            logging.debug(f"Repeated notification due: {hours_since_last:.1f}h since last, interval={required_interval}h")
            return True
        else:
            logging.debug(f"Repeated notification skipped: {hours_since_last:.1f}h since last, need {required_interval}h")
            return False
    
    except Exception as e:
        raise NotificationError(
            f"Error parsing notification time: {e}",
            context={"war_id": war_id, "player_tag": player_tag, "last_notif_time": last_notif_time_str}
        )
        return True  # Allow notification on error


async def _record_notification(war_id: str, player_info: Dict[str, Any]) -> None:
    """
    Record notification in state to prevent duplicate sends.
    
    Args:
        war_id: Unique war identifier
        player_info: Player info dict from _get_players_needing_reminders
    
    Updates:
        CACHE.notification_state[war_id]["notified_players"][player_tag]
    
    State Structure:
        {
            "war_id": {
                "notified_players": {
                    "player_tag": {
                        "player_name": "...",
                        "discord_id": "...",
                        "notification_time": "2025-12-23 06:00:00",
                        "attacks_remaining": 2
                    }
                }
            }
        }
    
    Note:
        Uses write-through pattern - persists immediately to DB.
    
    Example:
        await _record_notification("2CLANTAG01_2OPPTAG02", player_info)
    """
    player_tag_clean = player_info["player_tag"].lstrip("#")
    
    if war_id not in CACHE.notification_state:
        CACHE.notification_state[war_id] = {"notified_players": {}}
    
    CACHE.notification_state[war_id]["notified_players"][player_tag_clean] = {
        "player_name": player_info["player_name"],
        "discord_id": player_info["discord_id"],
        "notification_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "attacks_remaining": player_info["attacks_remaining"],
    }
    
    # Write-through: persist immediately to DB
    await CACHE.persist_player_notification(war_id, player_tag_clean)


def _is_channel_notification_sent(war_id: str, guild_id: str) -> bool:
    """
    Check if channel notification already sent for this war+guild combination.
    
    Args:
        war_id: Unique war identifier
        guild_id: Discord guild ID
    
    Returns:
        True if channel notification already sent, False otherwise
    
    Uses:
        CACHE.notification_state[war_id]["channel_notifications"][guild_id]
    
    Example:
        already_sent = _is_channel_notification_sent("2CLANTAG01_2OPPTAG02", "123456789012345678")
    """
    if war_id not in CACHE.notification_state:
        return False
    
    channel_notifications = CACHE.notification_state[war_id].get("channel_notifications", {})
    return guild_id in channel_notifications


async def _record_channel_notification(war_id: str, guild_id: str, clan_name: str, opponent_name: str) -> None:
    """
    Record channel notification in state to prevent duplicate sends.
    
    Args:
        war_id: Unique war identifier
        guild_id: Discord guild ID
        clan_name: Clan name for context
        opponent_name: Opponent clan name for context
    
    Updates:
        CACHE.notification_state[war_id]["channel_notifications"][guild_id]
    
    State Structure:
        {
            "war_id": {
                "notified_players": {...},  # Existing DM tracking
                "channel_notifications": {  # Channel notification tracking
                    "guild_id": {
                        "notification_time": "2026-01-13 01:00:00",
                        "clan_name": "The QCrew",
                        "opponent_name": "Enemy Clan"
                    }
                }
            }
        }
    
    Note:
        Uses write-through pattern - persists immediately to DB.
    
    Example:
        await _record_channel_notification("2CLANTAG01_2OPPTAG02", "123456789012345678", "Our Clan", "Enemy Clan")
    """
    if war_id not in CACHE.notification_state:
        CACHE.notification_state[war_id] = {"notified_players": {}, "channel_notifications": {}}
    
    if "channel_notifications" not in CACHE.notification_state[war_id]:
        CACHE.notification_state[war_id]["channel_notifications"] = {}
    
    CACHE.notification_state[war_id]["channel_notifications"][guild_id] = {
        "notification_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "clan_name": clan_name,
        "opponent_name": opponent_name,
    }
    
    # Write-through: persist immediately to DB
    await CACHE.persist_channel_notification(war_id, guild_id)


async def _send_aggregated_war_reminder_dm(discord_id: str, player_list: List[Dict[str, Any]], war_data: Dict[str, Any]) -> bool:
    """
    Send aggregated war reminder DM to a Discord user with all their CoC accounts.
    
    Args:
        discord_id: Discord user ID
        player_list: List of player info dicts for this user's CoC accounts
        war_data: War data dict for context (clan names, time remaining)
    
    Returns:
        True if DM sent successfully, False otherwise
    
    DM Content:
        Aggregated reminder with all accounts listed in tabular format,
        automatically translated to user's Discord language preference
    
    Example:
        success = await _send_aggregated_war_reminder_dm("123456789012345678", player_list, war_data)
    """
    try:
        from qapbot.cache_manager import CACHE
        
        # Ensure user metadata is up-to-date and get user object from cache
        user_data = await CACHE.ensure_user_metadata(str(discord_id))
        display_name = user_data.get("display_name", "User")
        
        # Get guild context for fallback language resolution (DMs don't have guild context)
        # For DM context, only user_id matters; guild_id can be None
        message = _format_aggregated_reminder_message(display_name, player_list, str(discord_id), guild_id=0)  # Use 0 instead of None
        
        # Send DM through cache manager (handles fetch internally)
        player_names = ", ".join([p["player_name"] for p in player_list])
        success = await CACHE.send_user_dm(str(discord_id), message)
        
        if success:
            logging.info(f"✅ Sent war reminder to {display_name} (Discord ID: {discord_id}) for accounts: {player_names}")
        else:
            logging.info(f"ℹ️ Could not send war reminder to {display_name} (Discord ID: {discord_id}) for accounts: {player_names} (DMs disabled or bot blocked)")
        
        return success
    
    except discord.DiscordServerError:
        raise  # Already logged as WARNING in send_user_dm; let outer loop handle
    except Exception as e:
        raise NotificationSendError(
            f"Unexpected error sending DM to Discord user {discord_id}: {e}",
            context={"discord_id": discord_id, "player_count": len(player_list)}
        )
        return False



def _format_aggregated_reminder_message(display_name: str, player_list: List[Dict[str, Any]], discord_user_id: Optional[str] = None, guild_id: Optional[int] = None) -> str:
    """
    Format aggregated war reminder DM message for multiple accounts.
    
    Args:
        display_name: Discord display name
        player_list: List of player info dicts for this user's accounts
        discord_user_id: Discord user ID for proper language fallback (optional)
        guild_id: Guild ID for language fallback (optional)
    
    Returns:
        Formatted message string with all accounts in tabular format
    
    Example:
        message = _format_aggregated_reminder_message("Alex", player_list, "123456789012345678", 123456789)
    
    Language Resolution:
        Uses user_id and guild_id parameters for t() function fallback chain:
        1. Check user's language preference
        2. Fall back to guild language if user has no preference
        3. Fall back to default (en) if neither set
    """
    # Get war context from first player (all same war)
    first_player = player_list[0]
    hours = first_player["hours_remaining"]
    clan_name = first_player["clan_name"]
    opponent_name = first_player["opponent_name"]
    
    # Format time remaining
    if hours < 1:
        minutes = int(hours * 60)
        time_str = t('ui_components.war_notification_dm.war_ends_minutes', user_id=discord_user_id, guild_id=guild_id, minutes=minutes)
    else:
        time_str = t('ui_components.war_notification_dm.war_ends_hours', user_id=discord_user_id, guild_id=guild_id, hours=f"{hours:.1f}")
    
    # Build account table — separate own and buddy accounts
    own_players = [p for p in player_list if not p.get("is_buddy", False)]
    buddy_players = [p for p in player_list if p.get("is_buddy", False)]
    has_buddies = len(buddy_players) > 0

    def _format_account_lines(players: List[Dict[str, Any]]) -> List[str]:
        lines: List[str] = []
        for player_info in players:
            attacks = player_info["attacks_remaining"]
            attack_word = t('ui_components.war_notification_dm.attack_line_single', user_id=discord_user_id, guild_id=guild_id) if attacks == 1 else t('ui_components.war_notification_dm.attack_line_multiple', user_id=discord_user_id, guild_id=guild_id)
            remaining_word = t('ui_components.war_notification_dm.attack_line_remaining', user_id=discord_user_id, guild_id=guild_id)
            normalized_name = normalize_player_name(player_info['player_name'])
            lines.append(f"  • **{normalized_name}** - {attacks} {attack_word} {remaining_word}")
        return lines

    total_attacks = sum(p["attacks_remaining"] for p in player_list)
    account_count = len(player_list)

    if has_buddies:
        # Display with section headers
        sections: List[str] = []
        if own_players:
            own_section_label = t('warnotifications.buddy_dm_own_section', user_id=discord_user_id, guild_id=guild_id)
            sections.append(own_section_label + "\n" + "\n".join(_format_account_lines(own_players)))
        if buddy_players:
            buddy_section_label = t('warnotifications.buddy_dm_section', user_id=discord_user_id, guild_id=guild_id)
            sections.append(buddy_section_label + "\n" + "\n".join(_format_account_lines(buddy_players)))
        accounts_text = "\n\n".join(sections)
    else:
        accounts_text = "\n".join(_format_account_lines(own_players))
    
    # Select appropriate plural form
    if total_attacks == 1 and account_count == 1:
        attacks_line = t('ui_components.war_notification_dm.attacks_remaining_single', 
                       user_id=discord_user_id, guild_id=guild_id, total_attacks=total_attacks, account_count=account_count)
    elif total_attacks == 1 or account_count == 1:
        attacks_line = t('ui_components.war_notification_dm.attacks_remaining_multiple_single',
                       user_id=discord_user_id, guild_id=guild_id, total_attacks=total_attacks, account_count=account_count)
    else:
        attacks_line = t('ui_components.war_notification_dm.attacks_remaining_multiple_multiple',
                       user_id=discord_user_id, guild_id=guild_id, total_attacks=total_attacks, account_count=account_count)
    
    header = t('ui_components.war_notification_dm.header', user_id=discord_user_id, guild_id=guild_id)
    greeting = t('ui_components.war_notification_dm.greeting', user_id=discord_user_id, guild_id=guild_id, display_name=display_name)
    matchup = t('ui_components.war_notification_dm.war_matchup', user_id=discord_user_id, guild_id=guild_id, clan_name=clan_name, opponent_name=opponent_name)
    reminder = t('ui_components.war_notification_dm.reminder', user_id=discord_user_id, guild_id=guild_id)
    
    message = f"""━━━━━━━━━━━━━━━━━━━━━━
⚔️ **{header}** ⚔️
━━━━━━━━━━━━━━━━━━━━━━

{greeting}

{attacks_line}

{accounts_text}

📍 {matchup}
⏰ {time_str}

{reminder}

━━━━━━━━━━━━━━━━━━━━━━"""
    
    return message


async def _send_channel_war_notification(clan_tag: str, war_data: Dict[str, Any], players_to_notify: List[Dict[str, Any]]) -> int:
    """
    Send aggregated war notification to configured Discord channel.
    
    Posts a consolidated message to the channel showing all players with pending attacks.
    Only sends if channel war notifications are enabled for guilds subscribed to this clan.
    Prevents duplicate sends by tracking in notification_state (once per war per guild).
    
    Args:
        clan_tag: Clan tag (e.g., "#2C9UR9GJY")
        war_data: War data dictionary with clan and opponent info
        players_to_notify: List of player info dicts with attack details
    
    Returns:
        Number of channel notifications sent (0 or more, one per guild)
    
    Channel Notification Format:
        - Embed with clan vs opponent
        - List of players with pending attacks
        - Time remaining until war end
        - Attack count per player
    
    Example:
        count = await _send_channel_war_notification("#2C9UR9GJY", war_data, players_to_notify)
        # Returns: 2 (if notifications sent to 2 guilds)
    """
    if not players_to_notify:
        return 0
    
    from qapbot.cache_manager import CACHE
    from qapbot.i18n import t
    import QBcore
    
    notifications_sent = 0
    
    clan_name = war_data.get("clan", {}).get("name", "Unknown")
    opponent_name = war_data.get("opponent", {}).get("name", "Unknown")
    hours_remaining: Optional[float] = war_data.get("hours_remaining")
    if hours_remaining is None:
        hours_remaining = _get_hours_until_war_end(war_data)

    if hours_remaining is None:
        return 0
    
    # Format time remaining - will be used within guild context, need guild_id for proper translation
    # Note: guild_id will be set in the loop below for each subscribed guild
    
    # Find all guilds subscribed to this clan
    subscribed_guilds: Dict[str, bool] = {}
    for guild_id, channel_subs in CACHE.subscriptions.items():
        for _channel_id, subs in channel_subs.items():
            for sub in subs:
                # Check if this subscription includes the clan
                if sub.get("clan_tag") == clan_tag:
                    subscribed_guilds[guild_id] = True
                    break
                # Check if subscription is to a family containing this clan
                elif sub.get("clan_tag") in CACHE.clan_families:
                    family_id = sub.get("clan_tag")
                    if family_id and isinstance(family_id, str):
                        family_data = CACHE.clan_families.get(family_id, {})
                        if clan_tag in family_data.get("clans", []):
                            subscribed_guilds[guild_id] = True
                            break
    
    if not subscribed_guilds:
        return 0
    
    # Get war_id for duplicate prevention
    war_id = _get_war_id(clan_tag, war_data)
    
    # For each subscribed guild, check if channel notifications are enabled
    for guild_id in subscribed_guilds:
        try:
            # Check if channel notification already sent for this war+guild combination
            if _is_channel_notification_sent(war_id, guild_id):
                logging.debug(f"Channel notification already sent for war {war_id} to guild {guild_id}")
                continue
            
            # DEV MODE FILTER: In DEV mode, only post to configured channels on the DEV guild
            # (unlike welcome messages which filter to a specific channel ID)
            # War notifications post to ANY configured war notification channel on the DEV guild
            if CONFIG.is_dev_mode and CONFIG.discord_guild_id > 0:
                if int(guild_id) != CONFIG.discord_guild_id:
                    logging.debug(f"Skipping war notification to guild {guild_id} (DEV mode, only posting to guild {CONFIG.discord_guild_id})")
                    continue
            
            guild_config = CACHE.server_config.get(guild_id, {})
            channel_notifications_enabled = guild_config.get("channel_war_notifications_enabled", False)
            war_notification_channel_id = guild_config.get("war_notification_channel_id")
            
            if not channel_notifications_enabled or not war_notification_channel_id:
                continue
            
            # Check if war is within this guild's notification threshold
            threshold_hours = guild_config.get("war_notification_threshold_hours", 1.0)
            if not (0 <= hours_remaining <= threshold_hours):
                logging.debug(f"War {clan_tag} not within guild {guild_id}'s threshold ({threshold_hours}h), skipping channel notification")
                continue
            
            # Format time remaining with guild-specific language
            guild_id_int = int(guild_id)
            if hours_remaining < 1:
                minutes = int(hours_remaining * 60)
                time_str = t('ui_components.war_notification_dm.war_ends_minutes', guild_id=guild_id_int, minutes=minutes)
            else:
                time_str = t('ui_components.war_notification_dm.war_ends_hours', guild_id=guild_id_int, hours=f"{hours_remaining:.1f}")
            
            # Get the channel object — prefer cache lookup (get_channel) over
            # API call (fetch_channel) since the bot already caches all guild
            # channels via the guilds intent.  Falls back to fetch_channel only
            # if the cache somehow missed it.
            try:
                channel = QBcore.bot.get_channel(int(war_notification_channel_id))
                if channel is None:
                    channel = await QBcore.bot.fetch_channel(int(war_notification_channel_id))
            except Exception as e:
                logging.warning(f"Could not fetch war notification channel {war_notification_channel_id} for guild {guild_id}: {e}")
                continue
            
            # Build player list
            player_lines: List[str] = []
            for player_info in players_to_notify:
                attacks_remaining = player_info["attacks_remaining"]
                player_name = player_info["player_name"]
                # Normalize player name to add LTR marks for RTL scripts
                normalized_name = normalize_player_name(player_name)
                player_lines.append(f"• **{normalized_name}** - {attacks_remaining} attack(s) remaining")
            
            player_list_text = "\n".join(player_lines)
            
            # Create embed
            embed = discord.Embed(
                title=t('ui_components.basic_config.war_channel_notification_title', 
                       guild_id=guild_id_int, clan_name=clan_name, opponent_name=opponent_name),
                description=t('ui_components.basic_config.war_channel_notification_description',
                             guild_id=guild_id_int, count=len(players_to_notify)),
                color=discord.Color.red()
            )
            
            embed.add_field(
                name=t('ui_components.basic_config.war_channel_notification_players_field',
                       guild_id=guild_id_int),
                value=player_list_text if len(player_list_text) < 1024 else player_list_text[:1021] + "...",
                inline=False
            )
            
            embed.add_field(
                name=t('ui_components.basic_config.war_channel_notification_time',
                       guild_id=guild_id_int, time=time_str),
                value="\u200b",
                inline=False
            )
            
            # Build custodian @mention line, if any custodians are configured for this clan
            custodian_ids = guild_config.get("clan_custodians", {}).get(clan_tag, [])
            mention_content: Optional[str] = None
            if custodian_ids:
                mentions = " ".join(f"<@{uid}>" for uid in custodian_ids)
                mention_content = t('ui_components.basic_config.war_channel_notification_custodians_line',
                                     guild_id=guild_id_int, mentions=mentions)

            # CWL Coordinator @mention line (tracker #0087/#0088, project owner's spec: integrate the
            # requested coordinator reminder into THIS existing channel notification rather than
            # building a separate coordinator DM reminder). Same shape as the custodian line above —
            # cwl_clan_coordinators is keyed by clan_tag exactly like clan_custodians.
            #
            # CWL wars only: coordinators are a CWL-specific role, and the donation nudge below
            # ("before the next war day") is meaningless for a regular war, which has no next day.
            #
            # The donation half of #0087/#0088 is a plain memory-aid sentence with no data behind it,
            # deliberately: the CoC API does not expose whether a war member's clan castle has been
            # filled, and nothing in this codebase tracks donations at all, so a real check isn't
            # possible — see plans/implemented/tracker-0085-0086-0087-cwl-coordinator-role.md.
            if war_data.get("is_cwl"):
                coordinator_ids = guild_config.get("cwl_clan_coordinators", {}).get(clan_tag, [])
                if coordinator_ids:
                    coordinator_mentions = " ".join(f"<@{uid}>" for uid in coordinator_ids)
                    coordinator_line = t('ui_components.basic_config.war_channel_notification_coordinators_line',
                                         guild_id=guild_id_int, mentions=coordinator_mentions)
                    mention_content = (
                        f"{mention_content}\n{coordinator_line}" if mention_content else coordinator_line
                    )

            # Send to channel
            if isinstance(channel, (discord.TextChannel, discord.Thread)):
                await channel.send(
                    content=mention_content,
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions(users=True, everyone=False, roles=False)
                )
            logging.info(f"✅ Sent channel war notification to guild {guild_id} channel {war_notification_channel_id} for {len(players_to_notify)} players")
            
            # Record that channel notification was sent for this war+guild
            await _record_channel_notification(war_id, guild_id, clan_name, opponent_name)
            notifications_sent += 1
            
        except Exception as e:
            logging.error(f"Error sending channel notification to guild {guild_id}: {e}", exc_info=True)
            continue
    
    return notifications_sent

