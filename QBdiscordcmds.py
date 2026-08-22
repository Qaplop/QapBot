"""
Discord command handlers for QapBot with comprehensive argument parsing, cache-centric message management, and unified message ID tracking.

This module contains all Discord bot commands with robust error handling and intelligent message tracking to prevent spam. All business logic 
uses cache-centric data access and unified message ID tracking for spam prevention and update logic.

Commands:
- Clan subscription management (/subscribe, /unsubscribe, /subscriptions)
- Leaderboard generation with flexible argument parsing (/leaderboard)
- Bot information and status (/status, /help, /ping)
- Administrative commands (/admin with multiple actions: Cleanup Messages, Check Logs, Check Data, List Subscriptions, Test Notify, Remove Clan, List Clans, Import Data, Debug Message)
- Clan family management via /clan_management (families mode), /list_families
- Account listing commands (/list_accounts)
- Player listing (/list_players)

Key Features:
- Intelligent argument parsing for clan tags, modes, and time periods in flexible order
- Unified message ID tracking system that replaces duplicate requests instead of spamming
- Smart message splitting for large leaderboards that exceed Discord's 2000 character limit
- Comprehensive error handling with user-friendly messages and administrative logging
- Automatic cleanup of stale message references when Discord messages are deleted

Business Rules:
- All leaderboard posts use unique tracking keys in CACHE.leaderboard_messages
- All cache operations are atomic and synchronized with persistent storage
- Defensive programming for Discord API errors and rate limits

Integration:
- Used by all Discord command flows for leaderboard, subscription, and admin actions
- Interacts with cache_manager for all runtime data and message tracking
- Supports both single message posts and intelligent table splitting for large clans
"""
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false
import asyncio
import io
import discord
from discord import app_commands
from datetime import datetime, timedelta, timezone
import calendar
import logging
import os
import sys
import builtins

from qapbot.emojis import BotEmojis
import platform
import psutil
import QBcore
from typing import Optional, Union, Any, Dict, List, Tuple, Set
from QBhelperfunctions import (
    generate_leaderboard_text, generate_cwlinfo_embeds, generate_cwlinfo_comp_embeds, post_discord_content_with_tracking, post_leaderboard_to_discord,
    update_clan_war_info_and_stats, generate_cwl_group_analysis_embeds,
    update_cwl_group_stats, generate_cwl_group_image,
    build_cwl_opponent_embeds, parse_month_argument, resolve_subscription_period,
    coc_clan_profile_url, coc_player_profile_url,
)
from QapBot import GLOBAL_GUILD_ID, run_nightly_maintenance_routine, is_monthly_migration_due
from qapbot.config import CONFIG
from qapbot.cache_manager import CACHE
from qapbot.discord_health import get_simple_discord_stats
from dotenv import load_dotenv
from qapbot.QBdiscocmdshelper import (  # type: ignore[attr-defined]
    _get_clan_tag, normalize_clan_tag, normalize_family_tag, send_and_track,  # type: ignore[misc]
    delete_leaderboard_messages_for_clan_channel, get_clan_family_autocomplete_choices,
    get_mode_autocomplete_choices,
    # Refactored helper functions
    check_admin_permissions, get_clan_display_name, get_family_display_name, get_clan_or_family_display_name,
    resolve_clan_or_family_tag, get_user_player, restore_player_from_unassigned,  # type: ignore[attr-defined]
    verify_and_update_player, is_already_subscribed, cleanup_stale_messages_for_channel,  # type: ignore[attr-defined]
    validate_and_add_clan_to_cache, resolve_guild_context
)
from qapbot.i18n import t  # type: ignore[attr-defined]

load_dotenv()
SERVER_ADMIN = CONFIG.server_admin or os.getenv("SERVER_ADMIN", "")

# Set dev_mode prefix for command descriptions
if GLOBAL_GUILD_ID:
    dev_mode = "[DEV] "
else:
    dev_mode = ""


def _log_cmd(interaction: discord.Interaction, cmd: str, **params: Any) -> None:
    """Log a command invocation with who issued it and the parameters used."""
    user = interaction.user
    param_str = "  ".join(f"{k}={v!r}" for k, v in params.items() if v is not None)
    msg = f"[CMD] /{cmd}  user={user} ({user.id})  guild={interaction.guild_id}"
    if param_str:
        msg += f"  {param_str}"
    logging.info(msg)


def _log_cmd_done(interaction: discord.Interaction, cmd: str) -> None:
    """Log successful command completion."""
    user = interaction.user
    logging.info(f"[CMD-DONE] /{cmd}  user={user} ({user.id})  guild={interaction.guild_id}")


async def _safe_defer(
    interaction: discord.Interaction,
    *,
    thinking: bool = False,
    ephemeral: bool = False,
) -> bool:
    """Wrap interaction.response.defer() in try/except discord.NotFound.

    During network instability the Discord gateway may close and reopen.
    The new WebSocket session assigns a fresh interaction token, so the old
    token (already in our interaction object) becomes invalid.  Calling
    defer() on it raises discord.NotFound (code 10062 — Unknown Interaction).
    Without a local guard the exception propagates to on_app_command_error,
    which handles it silently — but any state set before defer() remains dirty.

    Returns True  — defer succeeded; caller may continue normally.
    Returns False — interaction expired; caller must return immediately
                    (no followup can be sent to a dead interaction).
    """
    try:
        await interaction.response.defer(thinking=thinking, ephemeral=ephemeral)
        return True
    except discord.NotFound:
        logging.warning(
            "[DEFER] Interaction expired before defer — "
            f"command={getattr(interaction.command, 'name', 'unknown')} "
            f"user={interaction.user.id} guild={interaction.guild_id}"
        )
        return False


@app_commands.command(name="subscribe", description=dev_mode+"Subscribe a channel to clan or clan family leaderboard updates.")
@app_commands.describe(
    clan="Clan or clan-family tag or name to subscribe to",
    mode="Leaderboard mode for subscription (defaults to 'attack' if not specified)",
    cwl_only="Restrict subscription to CWL stats only",
    current_year="Use current year instead of current month for scheduled leaderboard posts"
)
# NOT converted in Phase 0b (CWL_ROSTER_PLANNING_PLAN.md): subscribes *this channel* to periodic
# leaderboard posts — a persistent guild-channel concept with no coherent DM analog (unlike
# highlightme, below). Revisit only with an explicit redesign (e.g. an explicit channel parameter).
@app_commands.guild_only()
async def subscribe(
    interaction: discord.Interaction,
    clan: str,
    mode: Optional[str] = None,
    cwl_only: Optional[bool] = False,
    current_year: Optional[bool] = False
):
    """
    Subscribe a channel to clan or clan family leaderboard updates for a specific mode.

    This command now supports two time scopes:
        - Default: current month (no month/year stored)
        - Current year (year-to-date): stored with year="current"
    """
    if not await _safe_defer(interaction, thinking=True, ephemeral=True):
        return
    _log_cmd(interaction, "subscribe", clan=clan, mode=mode, cwl_only=cwl_only, current_year=current_year)
    # Allow SERVER_ADMIN to bypass admin check
    if not await check_admin_permissions(interaction, SERVER_ADMIN):
        guild_id = interaction.guild.id if interaction.guild else None
        await interaction.followup.send(t('commands.errors.admin_required', guild_id=guild_id), ephemeral=True)
        return
    
    # Resolve clan tag
    if clan in CACHE.clan_families:
        tag = clan
    else:
        count, resolved = _get_clan_tag(clan)
        if count == 0 or not resolved:
            # Try to add as new clan by validating and fetching from Clash of Clans API
            normalized_tag = normalize_clan_tag(clan)
            if not normalized_tag:
                guild_id = interaction.guild.id if interaction.guild else None
                await interaction.followup.send(t('commands.errors.invalid_clan_tag_format_leaderboard', guild_id=guild_id, clan=clan), ephemeral=True)
                return
            
            # Use helper function to validate and add clan to cache
            success, message, _clan_name = await validate_and_add_clan_to_cache(normalized_tag)
            if not success:
                await interaction.followup.send(message, ephemeral=True)
                return
            
            tag = normalized_tag
        else:
            tag = resolved

    # Validate mode
    from qapbot.formatting import MODE_REGISTRY, DEFAULT_MODE  # type: ignore[attr-defined]
    valid_modes = set(MODE_REGISTRY.keys())  # type: ignore[arg-type]
    requested_mode = mode.lower() if mode else DEFAULT_MODE
    
    # Apply CWL suffix if cwl_only is True
    if cwl_only and not requested_mode.endswith("_cwl"):
        requested_mode = f"{requested_mode}_cwl"
    
    if requested_mode not in valid_modes:
        guild_id = interaction.guild.id if interaction.guild else None
        modes_list = ', '.join(sorted(valid_modes))
        await interaction.followup.send(t('commands.errors.invalid_mode', guild_id=guild_id, mode=mode, modes=modes_list), ephemeral=True)
        return
    
    guild_id = str(interaction.guild_id)
    if not interaction.channel:
        return
    channel_id = str(interaction.channel.id)
    current_subs = CACHE.get_channel_subscriptions(channel_id)
    total_subs = len(current_subs)
    if total_subs >= CONFIG.max_clan_subscriptions:
        guild_id = interaction.guild.id if interaction.guild else None
        await interaction.followup.send(t('commands.errors.max_subscriptions_reached', guild_id=guild_id, max_subs=CONFIG.max_clan_subscriptions), ephemeral=True)
        return

    # Check for duplicate subscription (match on clan, mode, year scope)
    year_scope = 'current' if current_year else None
    if is_already_subscribed(current_subs, tag, requested_mode, year_scope):
        guild_id = interaction.guild.id if interaction.guild else None
        display_name = get_clan_or_family_display_name(tag)
        await interaction.followup.send(t('commands.errors.already_subscribed', guild_id=guild_id, display_name=display_name, mode=requested_mode), ephemeral=True)
        return

    # Ensure clan exists in database before adding subscription
    await CACHE.db_manager._ensure_clan_exists(tag)  # type: ignore[union-attr]
    
    # Add subscription with optional year-to-date scope
    new_subscription = {
        'clan_tag': tag,
        'subscription_type': requested_mode,
    }
    if current_year:
        new_subscription['year'] = 'current'
    current_subs.append(new_subscription)
    await CACHE.set_channel_subscriptions(guild_id, channel_id, current_subs)
    
    # Update subscription status for affected clans
    if tag in CACHE.clan_families:
        # Family subscription - update all member clans
        family_data = CACHE.clan_families.get(tag, {})
        for member_clan_tag in family_data.get('clans', []):
            await CACHE.update_clan_subscription_status(member_clan_tag)
    else:
        # Direct clan subscription
        await CACHE.update_clan_subscription_status(tag)
    
    # Get display name for confirmation
    display_name = get_clan_or_family_display_name(tag)
    
    guild_id = interaction.guild.id if interaction.guild else None
    date_suffix = " (current year)" if current_year else " (current month)"
    await interaction.followup.send(t('commands.errors.subscribed_success', guild_id=guild_id, display_name=display_name, mode=requested_mode, date_suffix=date_suffix), ephemeral=True)
    if not interaction.channel:
        return
    _log_cmd_done(interaction, "subscribe")
    logging.info(f"ADMIN ACTION: {interaction.user} subscribed channel {interaction.channel.id} to {tag} for mode '{requested_mode}' (year_scope={'current' if current_year else 'month'})")

@subscribe.autocomplete('clan')
async def subscribe_clan_autocomplete(interaction: discord.Interaction, current: str):
    """Autocomplete for clan: shows server's clans when empty, extends to all tracked clans when searching."""
    guild_id = str(interaction.guild_id) if interaction.guild_id else None
    return await get_clan_family_autocomplete_choices(current, guild_id=guild_id, mode="guild_first")

@subscribe.autocomplete('mode')
async def subscribe_mode_autocomplete(interaction: discord.Interaction, current: str):
    """Autocomplete for mode from MODE_REGISTRY keys, excluding '_cwl' variants."""
    return await get_mode_autocomplete_choices(current, include_cwl_variants=False)

@app_commands.command(name="unsubscribe", description=dev_mode+"Unsubscribe a channel from clan or clan family leaderboard updates.")
@app_commands.describe(
    clan="Clan or clan-family tag or name to unsubscribe from",
    mode="Leaderboard mode to unsubscribe from (optional - if omitted, unsubscribes from all modes)",
    current_year="Unsubscribe the year-to-date variant (True) or the monthly variant (False). Leave empty to affect both."
)
# NOT converted in Phase 0b (CWL_ROSTER_PLANNING_PLAN.md): mirror of subscribe, above — same
# this-channel-subscription concept with no coherent DM analog.
@app_commands.guild_only()
async def unsubscribe(
    interaction: discord.Interaction,
    clan: str,
    mode: Optional[str] = None,
    current_year: Optional[bool] = None
):
    """
    Unsubscribe a channel from clan or clan family leaderboard updates for one or more modes.

    This command allows unsubscribing from one or all modes for a single clan or family 
    in the current channel. If no mode is given, unsubscribes from all modes for the given clan/family.

    Args:
        interaction: Discord interaction object
        clan: Clan tag, family tag, or clan name to unsubscribe from
        mode: Leaderboard mode to unsubscribe from (optional)

    Permissions Required:
        - Discord server administrator permissions
        - Must be used in a guild channel (not DMs)

    Behavior:
        - Validates clan/family existence
        - Validates mode against MODE_REGISTRY if provided
        - Removes matching subscriptions from channel
        - Deletes related leaderboard messages
        - Returns clear error messages for invalid inputs
    """
    if not await _safe_defer(interaction, thinking=True, ephemeral=True):
        return
    _log_cmd(interaction, "unsubscribe", clan=clan, mode=mode)
    # Allow SERVER_ADMIN to bypass admin check
    if not await check_admin_permissions(interaction, SERVER_ADMIN):
        guild_id = interaction.guild.id if interaction.guild else None
        await interaction.followup.send(t('commands.errors.admin_required', guild_id=guild_id), ephemeral=True)
        return
    
    # Resolve clan tag (special-case: PLAYERREGISTRATION)
    is_playerregistration_marker = clan.strip().upper() == "PLAYERREGISTRATION"
    if is_playerregistration_marker:
        tag = "PLAYERREGISTRATION"
    elif clan in CACHE.clan_families:
        tag = clan
    else:
        count, resolved = _get_clan_tag(clan)
        if count == 0 or not resolved:
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.followup.send(t('commands.errors.invalid_clan_or_family', guild_id=guild_id, tag=clan), ephemeral=True)
            return
        tag = resolved
    
    # Validate clan/family existence (skip for special marker)
    if not is_playerregistration_marker:
        is_family = tag in CACHE.clan_families
        is_clan = tag in CACHE.clan_name_cache
        if not is_clan and not is_family:
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.followup.send(t('commands.errors.invalid_clan_or_family_no_format', guild_id=guild_id, tag=tag), ephemeral=True)
            return
    
    if not interaction.channel:
        return
    channel_id = str(interaction.channel.id)
    current_subs = CACHE.get_channel_subscriptions(channel_id)
    if not current_subs:
        guild_id = interaction.guild.id if interaction.guild else None
        await interaction.followup.send(t('commands.errors.no_subscriptions', guild_id=guild_id), ephemeral=True)
        return
    
    # Find all subscriptions for this tag in this channel, filtered by time scope if provided
    scope_year = 'current' if current_year else None  # False or None => current month scope only
    if is_playerregistration_marker:
        current_subs_for_tag = [
            s for s in current_subs
            if s.get('subscription_type') == 'playerregistration'  # type: ignore[misc]
        ]
    else:
        current_subs_for_tag = [
            s for s in current_subs
            if s.get('clan_tag') == tag and s.get('year') == scope_year
        ]
    if not current_subs_for_tag:
        guild_id = interaction.guild.id if interaction.guild else None
        await interaction.followup.send(t('commands.errors.no_matching_subscriptions', guild_id=guild_id), ephemeral=True)
        return
    
    # Validate mode if provided
    if mode:
        from qapbot.formatting import MODE_REGISTRY  # type: ignore[attr-defined]
        valid_modes = set(MODE_REGISTRY.keys()) | {"playerregistration"}  # type: ignore[arg-type]
        requested_mode = mode.lower()
        
        if requested_mode not in valid_modes:
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.followup.send(t('commands.errors.invalid_mode_short', guild_id=guild_id, mode=mode), ephemeral=True)
            return
        
        mode_subscribed = any(s.get('subscription_type') == requested_mode for s in current_subs_for_tag)
        if not mode_subscribed:
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.followup.send(t('commands.errors.not_subscribed_mode', guild_id=guild_id), ephemeral=True)
            return
        
        modes_to_remove = [requested_mode]
    else:
        modes_to_remove = [s.get('subscription_type') for s in current_subs_for_tag]
    
    # Remove subscriptions (respecting time scope; for playerregistration ignore scope)
    guild_id = str(interaction.guild_id)
    if is_playerregistration_marker:
        updated_subs = [s for s in current_subs if not (s.get('subscription_type') in modes_to_remove and s.get('subscription_type') == 'playerregistration')]  # type: ignore[misc]
    else:
        updated_subs = [
            s for s in current_subs
            if not (
                s.get('clan_tag') == tag
                and s.get('subscription_type') in modes_to_remove
                and s.get('year') == scope_year
            )
        ]
    
    if updated_subs:
        await CACHE.set_channel_subscriptions(guild_id, channel_id, updated_subs)
    else:
        await CACHE.remove_channel_subscriptions(channel_id)
    
    # Update subscription status for affected clans
    # Must check if this was the LAST subscription for the clan/family
    if not is_playerregistration_marker:
        if tag in CACHE.clan_families:
            # Family unsubscription - update all member clans
            family_data = CACHE.clan_families.get(tag, {})
            for member_clan_tag in family_data.get('clans', []):
                await CACHE.update_clan_subscription_status(member_clan_tag)
        else:
            # Direct clan unsubscription
            await CACHE.update_clan_subscription_status(tag)
    
    # Delete related leaderboard messages and playerregistration messages
    for mode_name in modes_to_remove:
        if mode_name == 'playerregistration':
            for sub in current_subs_for_tag:
                if sub.get('subscription_type') == 'playerregistration':
                    message_id = sub.get('message_id')
                    if message_id and interaction.channel and hasattr(interaction.channel, 'fetch_message'):
                        try:
                            message = await interaction.channel.fetch_message(int(message_id))  # type: ignore[union-attr]
                            await message.delete()  # type: ignore[misc]
                            logging.info(f"Deleted playerregistration message {message_id} from channel {channel_id}")
                        except Exception as e:
                            logging.debug(f"Could not delete playerregistration message {message_id}: {e}")
        else:
            await delete_leaderboard_messages_for_clan_channel(tag, channel_id)

    # Get display name for confirmation
    display_name = get_clan_or_family_display_name(tag)
    
    modes_str_list: List[str] = [str(m) for m in modes_to_remove if m is not None]
    
    if len(modes_to_remove) == 1:
        guild_id = interaction.guild.id if interaction.guild else None
        await interaction.followup.send(t('commands.errors.unsubscribed_single_mode', guild_id=guild_id, display_name=display_name, mode=modes_str_list[0]), ephemeral=True)
    else:
        guild_id = interaction.guild.id if interaction.guild else None
        await interaction.followup.send(t('commands.errors.unsubscribed_multiple_modes', guild_id=guild_id, display_name=display_name, mode_count=len(modes_to_remove), modes=', '.join(modes_str_list)), ephemeral=True)
    
    if not interaction.channel:
        return
    _log_cmd_done(interaction, "unsubscribe")
    logging.info(f"ADMIN ACTION: {interaction.user} unsubscribed channel {interaction.channel.id} from {tag} for mode(s): {', '.join(modes_str_list)}")

@unsubscribe.autocomplete('clan')  # type: ignore[attr-defined]
async def unsubscribe_clan_autocomplete(interaction: discord.Interaction, current: str):  # type: ignore[return]
    """Autocomplete for clan: suggests only subscribed clans/families for this channel."""
    if not interaction.channel:
        return []  # type: ignore[return-value]
    channel_id = str(interaction.channel.id)
    return await get_clan_family_autocomplete_choices(current, channel_id=channel_id, mode="subscribed_only")

@unsubscribe.autocomplete('mode')  # type: ignore[attr-defined]
async def unsubscribe_mode_autocomplete(interaction: discord.Interaction, current: str):  # type: ignore[return]
    """Autocomplete for mode: suggests only subscribed modes for the selected clan in this channel."""
    if not interaction.channel:
        return []  # type: ignore[return-value]
    channel_id = str(interaction.channel.id)
    clan = interaction.namespace.clan if hasattr(interaction, 'namespace') and hasattr(interaction.namespace, 'clan') else None
    current_year = interaction.namespace.current_year if hasattr(interaction, 'namespace') and hasattr(interaction.namespace, 'current_year') else None
    
    if clan:
        if clan in CACHE.clan_families:
            tag = clan
        else:
            count, resolved = _get_clan_tag(clan)
            tag = resolved if count > 0 and resolved else clan
        
        # Filter subscribed modes by time scope if provided
        desired_year = 'current' if current_year else None
        return await get_mode_autocomplete_choices(
            current,
            subscribed_modes_only=True,
            clan_tag=tag,
            channel_id=channel_id,
            max_choices=25
        ) if current_year is None else [
            choice for choice in await get_mode_autocomplete_choices(
                current,
                subscribed_modes_only=True,
                clan_tag=tag,
                channel_id=channel_id,
                max_choices=25
            ) if any(
                s.get('subscription_type') == choice.value
                and s.get('clan_tag') == tag
                and s.get('year') == desired_year
                for s in CACHE.get_channel_subscriptions(channel_id)
            )
        ]
    else:
        return await get_mode_autocomplete_choices(current, include_cwl_variants=True)

@app_commands.command(name="leaderboard", description=dev_mode+"Display leaderboard(s) for subscribed clans or families with various modes.")
@app_commands.describe(
    clan="Clan or clan-family tag or name. Leave empty for this channel's subscriptions.",
    mode="Leaderboard mode: attack (total stars), defense (def stars taken), avgstars (stars per attack), currentwar (ongoing war), etc.",
    month="Month: a number (6), a range (6-7), a list (1;3;5), or trailing count (-2 = last 2 months)",
    year="Year (YYYY)",
    cwl_only="Restrict to CWL stats only",
    season="CWL season to filter by (YYYY-MM or YYYY-MM-DD for mid-month CWL, e.g. 2026-05 or 2026-06-15). Overrides month/year and forces CWL mode.",
    scope="ALL: count every clan a current member played for (default). OWN: this server's clans only.",
)
@app_commands.choices(scope=[
    app_commands.Choice(name="All clans a current member played for (default)", value="all"),
    app_commands.Choice(name="Own clans only (current member clans of this server)", value="own"),
])
# DM-invokable (Phase 0b, CWL_ROSTER_PLANNING_PLAN.md) — not actually channel-bound: the
# clan-omitted path reads "this channel's" subscriptions, which in a DM is simply empty (nobody
# subscribes a DM channel — /subscribe stays guild_only()) and already degrades to the existing
# "nothing to do" error, not a crash. The output step below branches on interaction.guild is None
# to send directly (no tracking/dedup — see send_and_track's is_dm handling and the comments at
# each branch below) instead of going through post_discord_content_with_tracking()/
# post_leaderboard_to_discord(), which stay guild-channel-only (shared with the periodic broadcast
# system — not worth the risk of widening their typed channel parameter for this).
@app_commands.checks.cooldown(1, 5.0, key=lambda i: (i.guild_id, i.channel_id))
async def leaderboard(
    interaction: discord.Interaction,
    clan: Optional[str] = None,
    mode: Optional[str] = None,
    month: Optional[str] = None,
    year: Optional[int] = None,
    cwl_only: Optional[bool] = False,
    season: Optional[str] = None,
    scope: Optional[str] = None,
):
    """
    Show leaderboard(s) using slash parameters with autocomplete for target and mode.

    Behavior:
        - If clan omitted: uses all subscribed clans/families for this channel
        - Mode: if omitted, uses subscription mode per entry or DEFAULT_MODE
        - CWL-only modifier toggles "_cwl" suffix on the mode
        - Month: single month, "a-b" range, "a;b;c" list, or "-N" for the trailing N months
          (may cross a year boundary, e.g. "-2" in January = December + January)
        - Month/year: year only -> YTD; month+year -> specific period(s); neither -> current month
        - Scope: "all" (default) credits a current member's stats even for wars fought
          while registered to a clan no longer tracked/subscribed here; "own" restricts to
          wars fought by this server's current member clans, as before
    """
    if not await _safe_defer(interaction, thinking=True, ephemeral=True):
        return
    _log_cmd(interaction, "leaderboard", clan=clan, mode=mode, month=month, year=year, cwl_only=cwl_only, season=season, scope=scope)

    scope = (scope or "all").lower()
    if scope not in ("own", "all"):
        scope = "all"

    # Convert 2-digit year to 4-digit year (add 2000)
    if year is not None and year < 100:
        year = year + 2000

    # Season override: normalise YYYY-MM-DD → Monday of its ISO week; extract
    # month/year for the time window; force CWL mode on all modes.
    cwl_season: Optional[str] = None
    season_period: Optional[Tuple[int, int]] = None
    if season:
        from qapbot.constants import normalize_cwl_season
        cwl_season = normalize_cwl_season(season.strip())
        _sp = cwl_season.split("-")
        year = int(_sp[0])
        season_period = (int(_sp[1]), year)
        cwl_only = True

    # Parse the month argument (single value, range, list, or trailing "-N") into an
    # ordered list of (month, year) pairs. A season override always wins.
    month_periods: Optional[List[Tuple[int, int]]] = None
    if season_period is not None:
        month_periods = [season_period]
    elif month is not None:
        try:
            month_periods = parse_month_argument(month, datetime.now(timezone.utc), year)
        except ValueError as e:
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.followup.send(
                t('commands.errors.invalid_month_spec', guild_id=guild_id, error=str(e)),
                ephemeral=True,
            )
            return

    # cwlgroup only ever targets a single month (a CWL season maps to one month) —
    # take the earliest resolved period as its target, ignoring ranges/lists.
    cwlgroup_month: Optional[int] = month_periods[0][0] if month_periods else None
    cwlgroup_year: Optional[int] = month_periods[0][1] if month_periods else year

    subs = CACHE.get_all_subscriptions_flat()
    if not interaction.channel:
        return
    channel_id = str(interaction.channel.id)
    subscribed = [s for s in subs.get(channel_id, []) if s.get('clan_tag') != 'PLAYERREGISTRATION']  # type: ignore[misc]

    tags: List[str] = []
    per_tag_modes: List[str] = []

    from qapbot.formatting import MODE_REGISTRY, DEFAULT_MODE, apply_cwl_mode_suffix  # type: ignore[attr-defined]
    valid_modes = set(MODE_REGISTRY.keys())  # type: ignore[arg-type]

    def apply_cwl(m: str) -> str:
        return apply_cwl_mode_suffix(m, bool(cwl_only))

    # Resolve clans and modes
    if not clan:
        if not subscribed:
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.followup.send(t('commands.errors.nothing_to_do_no_subs', guild_id=guild_id), ephemeral=True)
            return
        tags = [str(s.get('clan_tag')) for s in subscribed if s.get('clan_tag')]
        if mode:
            m = mode.lower()
            if m not in valid_modes and not m.endswith("_cwl"):
                guild_id = interaction.guild.id if interaction.guild else None
                valid_modes_str = ', '.join(sorted(valid_modes))
                await interaction.followup.send(t('commands.errors.invalid_mode_with_valid', guild_id=guild_id, mode=mode, valid_modes=valid_modes_str), ephemeral=True)
                return
            m = apply_cwl(m)
            per_tag_modes = [m] * len(tags)
        else:
            per_tag_modes = [apply_cwl(s.get('subscription_type', DEFAULT_MODE).lower()) for s in subscribed]
    else:
        clan_stripped = clan.strip()
        if clan_stripped in CACHE.clan_families:
            tags = [clan_stripped]
        else:
            count, resolved = _get_clan_tag(clan_stripped)
            if count == 0 or not resolved:
                guild_id = interaction.guild.id if interaction.guild else None
                await interaction.followup.send(t('commands.errors.invalid_clan_target', guild_id=guild_id), ephemeral=True)
                return
            tags = [resolved]
        if mode:
            m = mode.lower()
            if m not in valid_modes and not m.endswith("_cwl"):
                guild_id = interaction.guild.id if interaction.guild else None
                valid_modes_str = ', '.join(sorted(valid_modes))
                await interaction.followup.send(t('commands.errors.invalid_mode_with_valid', guild_id=guild_id, mode=mode, valid_modes=valid_modes_str), ephemeral=True)
                return
            per_tag_modes = [apply_cwl(m)]
        else:
            per_tag_modes = [apply_cwl(DEFAULT_MODE)]

    # Resolve time window
    now = datetime.now(timezone.utc)
    explicit_time = (month_periods is not None or year is not None)
    per_tag_periods: List[Tuple[Union[int, List[int], List[Tuple[int, int]]], int]] = []
    if explicit_time:
        if month_periods is not None:
            periods = month_periods
        else:
            # year given without a month -> year-to-date within that year
            yr = year if year else now.year
            month_range = builtins.list(builtins.range(1, now.month + 1)) if yr == now.year else builtins.list(builtins.range(1, 13))
            periods = [(m, yr) for m in month_range]
        month_range = periods  # for the log line below only
        yr = periods[-1][1]
        per_tag_periods = [(periods, yr)] * len(tags)
    else:
        yr = now.year
        month_range = now.month
        if not clan:
            # Align periods to subscriptions order for this channel
            for sub in subscribed:
                sub_year = sub.get('year')
                if sub_year == "current":
                    per_tag_periods.append((builtins.list(builtins.range(1, now.month + 1)), now.year))
                else:
                    per_tag_periods.append((now.month, now.year))
            # Pad in case of mismatch
            while len(per_tag_periods) < len(tags):
                per_tag_periods.append((now.month, now.year))
        else:
            per_tag_periods = [(now.month, now.year)] * len(tags)

    try:
        await interaction.delete_original_response()
    except discord.NotFound:
        pass
    logging.info(f"Processing leaderboard for tags: {tags} with modes: {per_tag_modes}, default_month_range: {month_range}, default_year: {yr}")
    
    # Collect all individual clan tags that need war info updates.
    # cwlinfo uses the CWL league group endpoint — not the regular war endpoint —
    # so skip those clans to avoid spurious "Failed to update war info" warnings
    # during CWL season (get_current_war correctly returns notInWar then).
    clans_needing_updates: set[str] = set()  # type: ignore[misc]
    
    if not clan:
        # No clan argument: Update war info for all clans subscribed to current channel
        # This includes individual clans and all members of subscribed families
        for i, tag in enumerate(tags):
            if per_tag_modes[i] in ('cwlinfo', 'cwlinfo_comp', 'cwlgroup'):
                continue  # CWL embed / group standings don't use the regular war endpoint
            if tag in CACHE.clan_families:
                # Add all member clans from subscribed families
                family_clans = CACHE.clan_families[tag].get('clans', [])
                clans_needing_updates.update(family_clans)  # type: ignore[attr-defined]
                logging.debug(f"Added {len(family_clans)} member clans from family {tag}: {family_clans}")
            else:
                # Add individual subscribed clans
                clans_needing_updates.add(tag)  # type: ignore[attr-defined]
                logging.debug(f"Added individual subscribed clan: {tag}")
    else:
        # Clan/family argument provided: Update war info for specified clan or family members
        for i, tag in enumerate(tags):
            if per_tag_modes[i] in ('cwlinfo', 'cwlinfo_comp', 'cwlgroup'):
                continue  # CWL embed / group standings don't use the regular war endpoint
            if tag in CACHE.clan_families:
                # Family specified: update all member clans
                family_clans = CACHE.clan_families[tag].get('clans', [])
                clans_needing_updates.update(family_clans)  # type: ignore[attr-defined]
                logging.debug(f"Added {len(family_clans)} member clans from specified family {tag}: {family_clans}")
            else:
                # Individual clan specified: update that clan
                clans_needing_updates.add(tag)  # type: ignore[attr-defined]
                logging.debug(f"Added specified individual clan: {tag}")
    
    # Update war info for all collected individual clans (each clan only once)
    logging.info(f"Updating war info for {len(clans_needing_updates)} unique clans: {sorted(clans_needing_updates)}")  # type: ignore[arg-type]
    for clan_tag in clans_needing_updates:  # type: ignore[misc]
        if not await update_clan_war_info_and_stats(clan_tag):  # type: ignore[arg-type]
            logging.warning(f"War data processing failed for {clan_tag}")

    # Resolve current rosters for scope="all" — credits a current member's stats
    # even for wars fought while registered to a clan no longer tracked/subscribed
    # here. Uses the same stale-while-revalidate coc_clan_cache as everywhere else.
    #
    # PERF: fetch every distinct constituent clan in ONE parallel gather instead of
    # awaiting them one at a time — a cache miss/expiry is a live CoC API call, and
    # sequentially awaiting a whole channel's subscribed clans/families (no clan
    # filter given) is what made /leaderboard take minutes to respond on prod.
    # Same pattern as ui_clan_management.py's guild-clan refresh.
    member_tags_by_tag: Dict[str, Set[str]] = {}
    if scope == "all":
        tag_to_constituent: Dict[str, List[str]] = {
            tag: (CACHE.clan_families[tag].get('clans', []) if tag in CACHE.clan_families else [tag])
            for tag in tags
        }
        all_constituent_clans: Set[str] = {ct for cts in tag_to_constituent.values() for ct in cts}
        clan_results = await asyncio.gather(
            *(CACHE.coc_clan_cache.get_clan(ct) for ct in all_constituent_clans),
            return_exceptions=True,
        )
        clan_obj_by_tag: Dict[str, Any] = {}
        for ct, result in zip(all_constituent_clans, clan_results):
            if isinstance(result, BaseException):
                logging.warning(f"[leaderboard scope=all] Failed to fetch roster for {ct}: {result}")
            else:
                clan_obj_by_tag[ct] = result
        for tag, constituent in tag_to_constituent.items():
            roster: Set[str] = set()
            for ct in constituent:
                clan_obj = clan_obj_by_tag.get(ct)
                if clan_obj is not None:
                    roster.update(m.tag for m in clan_obj.members if getattr(m, "tag", None))
            member_tags_by_tag[tag] = roster

    # Auto-highlight (bold, via ANSI code block) the invoking user's own registered
    # player(s) in the rendered table, so they can spot themselves in a long list.
    highlight_player_ids: Set[str] = set()
    user_entry = CACHE.user_accounts.get(str(interaction.user.id))
    if user_entry:
        for p in user_entry.get('players', []):
            if isinstance(p, dict) and p.get('player_tag'):
                highlight_player_ids.add(p['player_tag'])

    # Generate and post leaderboards for requested targets. DM invocations send directly
    # (no tracking/dedup — a DM has no shared-channel clutter to prevent) rather than going
    # through the tracked-posting helpers shared with the periodic broadcast system.
    is_dm = interaction.guild is None
    for i, tag in enumerate(tags):
        is_family = tag in CACHE.clan_families
        if is_family and per_tag_modes[i] in ('currentwar', 'cwlinfo', 'cwlinfo_comp', 'cwlgroup'):
            continue  # Skip live-war / group modes for families
        period_month, period_year = per_tag_periods[i] if i < len(per_tag_periods) else (month_range, yr)
        if per_tag_modes[i] == 'cwlinfo':
            if not interaction.channel:
                continue
            cwl_embeds = await generate_cwlinfo_embeds(tag)
            if is_dm:
                for cwl_embed in cwl_embeds:
                    await interaction.followup.send(embed=cwl_embed)
            elif isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
                await post_discord_content_with_tracking(tag, interaction.channel, embeds=cwl_embeds)
        elif per_tag_modes[i] == 'cwlinfo_comp':
            if not interaction.channel:
                continue
            cwl_embeds, cwl_debug = await generate_cwlinfo_comp_embeds(tag)
            if is_dm:
                for cwl_embed in cwl_embeds:
                    await interaction.followup.send(embed=cwl_embed)
                if cwl_debug:
                    await interaction.followup.send(content=cwl_debug)
            elif isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
                await post_discord_content_with_tracking(tag, interaction.channel, 'cwlinfo_comp', embeds=cwl_embeds, debug_content=cwl_debug)
        elif per_tag_modes[i] == 'cwlgroup':
            if interaction.channel:
                guild_id = interaction.guild.id if interaction.guild else None
                # Year without month: error — we can't know which month to show
                if year is not None and cwlgroup_month is None:
                    await interaction.followup.send(
                        t('commands.errors.cwlgroup_year_without_month', guild_id=guild_id),
                        ephemeral=True,
                    )
                    continue
                # Resolve the target season
                _explicit_season: bool
                if cwl_season:
                    # Exact season was given via the season parameter: use it directly
                    _season = cwl_season
                    _explicit_season = True
                elif cwlgroup_month is not None:
                    _yr = cwlgroup_year if cwlgroup_year is not None else datetime.now().year
                    _month_prefix = f"{_yr}-{cwlgroup_month:02d}"
                    # Look up the most recent sub-season for this month so that a
                    # mid-month bonus CWL (e.g. "2026-06-16") is returned instead
                    # of the plain YYYY-MM key that would miss it in the DB.
                    _db2 = CACHE.db_manager
                    if _db2 is not None:
                        _season = await _db2.get_latest_cwl_season_for_clan_in_month(
                            tag, _month_prefix
                        ) or _month_prefix
                    else:
                        _season = _month_prefix
                    _explicit_season = True
                else:
                    # No month/year: use the most recent known season for this clan.
                    # Season keys are now date-based (e.g. "2026-06-01"), so
                    # strftime("%Y-%m") would not match the DB entries.
                    _db = CACHE.db_manager
                    _latest = (await _db.get_latest_cwl_season_for_clan(tag) if _db else None)  # type: ignore[union-attr]
                    _season = _latest or datetime.now().strftime("%Y-%m")
                    _explicit_season = False

                standings = await update_cwl_group_stats(tag, _season)

                if not standings and not _explicit_season:
                    # Rule 2: fall back to the most recent season with data
                    _last_season = await CACHE.db_manager.get_latest_cwl_season_for_clan(tag)  # type: ignore[union-attr]
                    if _last_season and _last_season != _season:
                        standings = await update_cwl_group_stats(tag, _last_season)
                        _season = _last_season

                if not standings:
                    # Rule 3: no CWL data found at all
                    _entry = CACHE.clan_name_cache.get(tag)
                    _clan_name = (_entry.get("name") if isinstance(_entry, dict) else None) or tag
                    await interaction.followup.send(
                        t('commands.errors.cwlgroup_no_data', guild_id=guild_id,
                          clan_name=_clan_name, tag=tag, season=_season),
                        ephemeral=True,
                    )
                else:
                    img_bytes = await asyncio.to_thread(generate_cwl_group_image, standings, _season, tag)
                    if is_dm:
                        await interaction.followup.send(file=discord.File(io.BytesIO(img_bytes), filename="cwlgroup.png"))
                    elif isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
                        await post_discord_content_with_tracking(tag, interaction.channel, f"cwlgroup_{_season}", file_bytes=img_bytes, file_name="cwlgroup.png", update_existing=False)
        else:
            text = await asyncio.to_thread(
                generate_leaderboard_text, tag, month=period_month, year=period_year,
                mode=per_tag_modes[i], cwl_season=cwl_season,
                scope=scope, member_player_tags=member_tags_by_tag.get(tag),
                highlight_player_ids=highlight_player_ids,
            )
            if is_dm:
                # ```ansi fence matches post_leaderboard_to_discord()'s formatting — the
                # auto-highlight feature above embeds ANSI codes for the caller's own player(s).
                await send_and_track(interaction, content=f"```ansi\n{text}```", command_name=f'leaderboard_{tag}_{per_tag_modes[i]}', ephemeral=False)
            elif interaction.channel and isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
                await post_leaderboard_to_discord(text, tag, month=period_month, year=period_year, channel=interaction.channel, mode=per_tag_modes[i], cwl_season=cwl_season)


@leaderboard.autocomplete('clan')  # type: ignore[attr-defined]
async def leaderboard_clan_autocomplete(interaction: discord.Interaction, current: str):  # type: ignore[return]
    """Autocomplete for clan: shows server's clans when empty, extends to all tracked clans when
    searching. In a DM (no single interaction.guild_id to key off), offers the union of clans
    from every guild the caller is linked to via their registered CoC accounts."""
    if interaction.guild_id:
        return await get_clan_family_autocomplete_choices(current, guild_id=str(interaction.guild_id), mode="guild_first")
    from qapbot.QBdiscocmdshelper import get_dm_caller_matched_guild_ids
    guild_ids = [str(g) for g in get_dm_caller_matched_guild_ids(str(interaction.user.id))]
    return await get_clan_family_autocomplete_choices(current, guild_ids=guild_ids, mode="guild_first")


@leaderboard.autocomplete('mode')  # type: ignore[attr-defined]
async def leaderboard_mode_autocomplete(interaction: discord.Interaction, current: str):
    """Autocomplete for mode from MODE_REGISTRY keys, excluding '_cwl' variants (redundant with cwl_only argument)."""
    return await get_mode_autocomplete_choices(current, include_cwl_variants=False)

@app_commands.command(name="highlightme", description=dev_mode+"Re-post this channel's leaderboards with your own player(s) highlighted (one-time).")
# NOT converted in Phase 0b (CWL_ROSTER_PLANNING_PLAN.md): re-posts *this channel's* subscribed
# leaderboards — a guild-channel-subscription concept with no DM analog. The guard at the top of
# this function (not isinstance(interaction.channel, (TextChannel, Thread)) -> silent return) is
# effectively this command's own guild-only check.
@app_commands.guild_only()
@app_commands.checks.cooldown(1, 5.0, key=lambda i: (i.guild_id, i.channel_id))
async def highlightme(interaction: discord.Interaction):
    """
    Re-post every leaderboard subscribed to this channel with the invoking user's
    own registered player(s) highlighted (bold/color via ANSI codes — same
    mechanism /leaderboard already uses for the auto-highlight-yourself feature).

    Behavior:
        - Only clan/family subscriptions whose mode renders as a per-row text
          table are eligible; cwlinfo/cwlinfo_comp/cwlgroup post embeds/images
          and have no per-row highlighting to apply.
        - A leaderboard is only reposted if the invoking user's player(s)
          actually appear in it; leaderboards they're not listed in are left
          untouched.
        - One-time by construction: the highlighted text's content hash never
          matches the plain text the next automatic update cycle renders, so
          that cycle always reposts (and thereby clears the highlight) even if
          the underlying stats didn't change. No extra state is tracked.
        - If the channel has no eligible subscriptions, or the user isn't
          listed in any of them, nothing is reposted — just a small ephemeral
          notice explaining why.
        - A rolling (no explicit month/year) subscription accumulates a separate
          tracked message per calendar month over time (05/2026, 06/2026, ...).
          Only the latest (current month's) message is ever targeted here —
          resolve_subscription_period() always resolves such a subscription to
          "now" — so older months' messages are left untouched.
    """
    if not await _safe_defer(interaction, thinking=True, ephemeral=True):
        return
    _log_cmd(interaction, "highlightme")
    guild_id = interaction.guild.id if interaction.guild else None

    if not interaction.channel or not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
        return
    channel_id = str(interaction.channel.id)

    subs = CACHE.get_all_subscriptions_flat()
    # Only modes that go through render_leaderboard() (a per-row text table) can be
    # highlighted — cwlinfo/cwlinfo_comp/cwlgroup post embeds/images instead.
    subscribed = [
        s for s in subs.get(channel_id, [])
        if s.get('clan_tag') and s.get('clan_tag') != 'PLAYERREGISTRATION'
        and s.get('subscription_type') not in ('cwlinfo', 'cwlinfo_comp', 'cwlgroup', 'playerregistration')
    ]
    if not subscribed:
        await interaction.followup.send(t('commands.errors.highlightme_no_leaderboard', guild_id=guild_id), ephemeral=True)
        return

    highlight_player_ids: Set[str] = set()
    user_entry = CACHE.user_accounts.get(str(interaction.user.id))
    if user_entry:
        for p in user_entry.get('players', []):
            if isinstance(p, dict) and p.get('player_tag'):
                highlight_player_ids.add(p['player_tag'])

    from qapbot.formatting import DEFAULT_MODE, leaderboard_text_has_highlight  # type: ignore[attr-defined]

    reposted = 0
    if highlight_player_ids:
        for sub in subscribed:
            clan_tag = sub.get('clan_tag')
            if not clan_tag:
                continue
            mode = sub.get('subscription_type', DEFAULT_MODE)
            sub_month = sub.get('month')
            sub_year = sub.get('year')
            month, year, month_range = resolve_subscription_period(sub)

            text = await asyncio.to_thread(
                generate_leaderboard_text, clan_tag, month=month_range, year=year,
                mode=mode, highlight_player_ids=highlight_player_ids,
            )
            # Mirror the automatic posting loop's "no data yet this month" fallback
            # (post_leaderboards_to_subscribed_channels in QapBot.py) so the content
            # rendered here matches whatever is actually posted right now. Note:
            # month_range/year (the message's tracking key) intentionally stay as the
            # current period — only the fallback TEXT comes from the previous month —
            # so this still targets the latest message for a rolling subscription
            # instead of spawning a stray duplicate on a past month's message.
            if sub_month is None and sub_year is None and "no wars recorded for" in text.lower() and mode != "currentwar":
                prev_month = month - 1
                prev_year = year
                if prev_month == 0:
                    prev_month = 12
                    prev_year -= 1
                text = await asyncio.to_thread(
                    generate_leaderboard_text, clan_tag, month=prev_month, year=prev_year,
                    mode=mode, highlight_player_ids=highlight_player_ids,
                )

            if not leaderboard_text_has_highlight(text):
                continue  # user isn't listed in this leaderboard — leave it untouched

            await post_leaderboard_to_discord(text, clan_tag, month_range, year, interaction.channel, mode=mode)
            reposted += 1

    if reposted == 0:
        await interaction.followup.send(t('commands.errors.highlightme_not_listed', guild_id=guild_id), ephemeral=True)
        return

    _log_cmd_done(interaction, "highlightme")
    await interaction.followup.send(t('commands.highlightme.success', guild_id=guild_id, count=reposted), ephemeral=True)


# ---------------------------------------------------------------------------
# Bug/feature tracker (BUG_FEATURE_TRACKER_PLAN.md Phase 3) — thin wrappers around the shared
# qapbot.ui_tracker.start_tracker_item() coroutine; only item_type differs between the two.
# Registered conditionally (CONFIG.tracker_enabled) in QapBot.py's _setup_hook() — see plan §3.1.
# ---------------------------------------------------------------------------

@app_commands.command(name="bug", description=dev_mode + "Report a bug to the QapBot maintainer.")
@app_commands.describe(
    attachment1="Optional screenshot/log file",
    attachment2="Optional screenshot/log file",
    attachment3="Optional screenshot/log file",
)
@app_commands.checks.cooldown(2, 300.0, key=lambda i: i.user.id)
async def bug(
    interaction: discord.Interaction,
    attachment1: Optional[discord.Attachment] = None,
    attachment2: Optional[discord.Attachment] = None,
    attachment3: Optional[discord.Attachment] = None,
) -> None:
    """Open the bug-report modal (works in any guild the bot serves, and in DMs)."""
    _log_cmd(interaction, "bug")
    from qapbot.ui_tracker import start_tracker_item
    await start_tracker_item(interaction, "bug", attachment1, attachment2, attachment3)


@app_commands.command(name="feature", description=dev_mode + "Request a feature for QapBot.")
@app_commands.describe(
    attachment1="Optional screenshot/mockup file",
    attachment2="Optional screenshot/mockup file",
    attachment3="Optional screenshot/mockup file",
)
@app_commands.checks.cooldown(2, 300.0, key=lambda i: i.user.id)
async def feature(
    interaction: discord.Interaction,
    attachment1: Optional[discord.Attachment] = None,
    attachment2: Optional[discord.Attachment] = None,
    attachment3: Optional[discord.Attachment] = None,
) -> None:
    """Open the feature-request modal (works in any guild the bot serves, and in DMs)."""
    _log_cmd(interaction, "feature")
    from qapbot.ui_tracker import start_tracker_item
    await start_tracker_item(interaction, "feature", attachment1, attachment2, attachment3)


def _get_help_command_dm_status() -> Dict[str, bool]:
    """Maps /help's display names to their command's `.guild_only` flag, so /help can filter
    itself to only the commands actually invokable from a DM.

    Built at call time, not import time: it references command objects (e.g. analyse_leaguegroup,
    admin) defined later in this module. Python only resolves a function body's names when the
    function actually runs, so this forward reference is safe as long as nothing calls this
    before the whole module has finished loading (true for both of its callers below, which only
    run per-interaction, long after import).
    """
    return {
        "subscribe": subscribe.guild_only,
        "unsubscribe": unsubscribe.guild_only,
        "subscriptions": subscriptions.guild_only,
        "leaderboard": leaderboard.guild_only,
        "highlightme": highlightme.guild_only,
        "analyse cwl_league_group": analyse_leaguegroup.guild_only,
        "analyse cwl_opponent": analyse_cwlopponent.guild_only,
        "clan management": clan_management.guild_only,
        "admin": admin.guild_only,
        "list": list.guild_only,
        "whois": whois_slash.guild_only,
        "ping": ping.guild_only,
        "status": status.guild_only,
        "help": help.guild_only,
        "bug": bug.guild_only,
        "feature": feature.guild_only,
    }


@app_commands.command(name="help", description=dev_mode+"Display help information about the available bot commands.")
@app_commands.describe(
    command="Optional: Select a specific command to get detailed help"
)
# DM-invokable (Phase 0b, CWL_ROSTER_PLANNING_PLAN.md) — display-only guild_id, no functional guild dependency.
# Filters its own command listing to DM-available commands when invoked from a DM — see
# _get_help_command_dm_status() above.
@app_commands.checks.cooldown(1, 5.0, key=lambda i: (i.guild_id, i.channel_id))
async def help(interaction: discord.Interaction, command: Optional[str] = None):
    """
    Display help information with optional detailed view for specific commands.
    
    If command parameter is provided, show detailed help for that command.
    Otherwise, show list of all available commands with short descriptions.
    """
    from qapbot.i18n import t  # type: ignore[attr-defined]

    is_dm = interaction.guild is None

    # Command list for autocomplete and validation — in a DM, filtered down to only the
    # commands that are actually invokable there (see _get_help_command_dm_status()). Lowercase
    # (not the usual module-constant SCREAMING_CASE) since this is a plain local variable,
    # reassigned below for the DM case — an all-caps name here reads as a real constant and
    # trips static-analysis "constant redefinition" warnings for a completely legal reassignment.
    available_commands = [
        "subscribe", "unsubscribe", "subscriptions", "leaderboard", "highlightme", "analyse cwl_league_group",
        "analyse cwl_opponent", "clan management", "admin", "list", "whois", "ping", "status", "help"
    ]
    if CONFIG.tracker_enabled:
        available_commands += ["bug", "feature"]
    if is_dm:
        dm_status = _get_help_command_dm_status()
        available_commands = [c for c in available_commands if not dm_status.get(c, True)]

    user_id = str(interaction.user.id)
    guild_id = interaction.guild_id
    
    if not await _safe_defer(interaction, thinking=False):
        return
    _log_cmd(interaction, "help", command=command)
    
    # If specific command is requested, show detailed help
    if command:
        command_lower = command.lower()
        if command_lower not in available_commands:
            error_embed = discord.Embed(
                title=t('commands.help.command_not_found_title', user_id=user_id, guild_id=guild_id),
                description=t('commands.help.command_not_found_desc', user_id=user_id, guild_id=guild_id, command=command),
                color=discord.Color.red()
            )
            await send_and_track(interaction, embed=error_embed, command_name='help', ephemeral=True)
            return
        
        # Get detailed help for the command
        title = t(f'commands.help.{command_lower}.title', user_id=user_id, guild_id=guild_id)
        description = t(f'commands.help.{command_lower}.detailed', user_id=user_id, guild_id=guild_id)
        
        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color.blue()
        )
        await send_and_track(interaction, embed=embed, command_name='help', ephemeral=True)
        return
    
    # Build command ID mapping from registered commands
    command_ids = {}
    help_command_id = None
    try:
        # Get commands from the app's command tree
        app_id = interaction.client.application_id
        if not app_id:
            logging.warning("Application ID not available")
            app_id = 0
        
        # Use guild commands for DEV mode, global commands for PROD
        if CONFIG.discord_guild_id > 0:
            # DEV mode: fetch guild-specific commands
            commands = await interaction.client.http.get_guild_commands(app_id, CONFIG.discord_guild_id)
        else:
            # PROD mode: fetch global commands
            commands = await interaction.client.http.get_global_commands(app_id)
        
        # Map command names to their IDs and find help command
        for cmd in commands:
            command_ids[cmd['name']] = cmd['id']
            if cmd['name'] == 'help':
                help_command_id = cmd['id']
    except Exception as e:
        logging.warning(f"Failed to fetch command IDs: {e}")
    
    # Show list of all commands with short descriptions
    embed = discord.Embed(
        title=t('commands.help.list_title', user_id=user_id, guild_id=guild_id),
        color=discord.Color.blue()
    )
    
    # Build description with clickable help command mention
    if help_command_id:
        description = t('commands.help.list_description', user_id=user_id, guild_id=guild_id)
        embed.description = description
    else:
        embed.description = t('commands.help.list_description', user_id=user_id, guild_id=guild_id)
    if is_dm:
        embed.description = (embed.description or "") + "\n" + t('commands.help.dm_filtered_note', user_id=user_id, guild_id=guild_id)

    # Organize commands by category (reorganized per user request)
    categories = {
        t('commands.help.category_leaderboards', user_id=user_id, guild_id=guild_id): ["subscribe", "unsubscribe", "subscriptions", "leaderboard", "highlightme"],
        t('commands.help.category_clan_player_info', user_id=user_id, guild_id=guild_id): ["analyse cwl_league_group", "analyse cwl_opponent", "whois"],
        t('commands.help.category_administration', user_id=user_id, guild_id=guild_id): ["clan management", "admin", "list"],
        t('commands.help.category_bot_info', user_id=user_id, guild_id=guild_id): ["ping", "status", "help"],
    }
    if CONFIG.tracker_enabled:
        categories[t('commands.help.category_tracker', user_id=user_id, guild_id=guild_id)] = ["bug", "feature"]
    if is_dm:
        dm_status = _get_help_command_dm_status()
        categories = {
            category: [c for c in commands_list if not dm_status.get(c, True)]
            for category, commands_list in categories.items()
        }

    for category, commands_list in categories.items():
        if not commands_list:
            continue  # entire category filtered out in DM \u2014 skip it, including its blank-line spacer
        # Add blank line before category (except first)
        embed.add_field(name="\u200b", value="", inline=False)

        field_value = ""
        for cmd in commands_list:
            short_desc = t(f'commands.help.{cmd}.short', user_id=user_id, guild_id=guild_id)
            
            # Map subcommand display names to their parent command IDs
            cmd_key = cmd
            if cmd == "clan management":
                cmd_key = "clan"
            elif cmd in ("analyse cwl_league_group", "analyse cwl_opponent"):
                cmd_key = "analyse"
            
            # Use clickable command mention if ID is available, otherwise use code formatting
            if cmd_key in command_ids:
                cmd_id = command_ids[cmd_key]  # type: ignore[has-type]
                field_value += f"</{cmd}:{cmd_id}> - {short_desc}\n"
            else:
                field_value += f"`/{cmd}` - {short_desc}\n"
        
        if field_value.strip():
            embed.add_field(name=category, value=field_value, inline=False)
    
    _log_cmd_done(interaction, "help")
    await send_and_track(interaction, embed=embed, command_name='help', ephemeral=True)

@help.autocomplete('command')
async def help_command_autocomplete(interaction: discord.Interaction, current: str):
    """Autocomplete for help command parameter — filtered to DM-available commands when
    invoked from a DM (see _get_help_command_dm_status())."""
    commands_list = [
        "subscribe", "unsubscribe", "subscriptions", "leaderboard", "highlightme", "analyse cwl_league_group",
        "analyse cwl_opponent", "clan management", "admin", "list", "whois", "ping", "status", "help"
    ]
    if interaction.guild is None:
        dm_status = _get_help_command_dm_status()
        commands_list = [c for c in commands_list if not dm_status.get(c, True)]

    current_lower = (current or "").lower()
    choices = [
        app_commands.Choice(name=cmd, value=cmd)
        for cmd in commands_list
        if not current_lower or current_lower in cmd
    ]
    return choices[:25]


async def do_maintenance_shutdown() -> None:
    """Close all resources after maintenance mode is confirmed active.

    Called either directly from the MAINTENANCE_START handler (when no cycle
    is running) or by periodic_main() at the end of a cycle when
    QBcore.maintenance_pending was set during the cycle.
    """
    # Note: we do NOT set QBcore.shutdown_initiated = True here.
    # on_disconnect() now checks maintenance_mode directly to suppress the spurious WARNING.
    # Setting shutdown_initiated would block the SIGTERM handler (_signal_shutdown_handler
    # returns immediately when shutdown_initiated is True), so `kill PID` after entering
    # maintenance mode would be silently ignored and the process would never exit.

    # Shut down Monte Carlo worker pool (ephemeral per-call — this is a no-op,
    # kept for API compatibility with async_cleanup).
    try:
        from QBwarsim import shutdown_sim_pool
        shutdown_sim_pool()
        logging.info("[MAINTENANCE] Monte Carlo worker pool shut down.")
    except Exception as e:
        logging.warning(f"[MAINTENANCE] Error shutting down sim pool: {e}")

    # Close CoC client (stop outgoing API calls)
    try:
        if QBcore.coc_client is not None and not CONFIG.no_coc_api:
            await asyncio.wait_for(QBcore.coc_client.close(), timeout=10.0)
            logging.info("[MAINTENANCE] CoC API client closed.")
    except asyncio.TimeoutError:
        logging.warning("[MAINTENANCE] Timed out waiting for CoC client to close.")
    except Exception as e:
        logging.warning(f"[MAINTENANCE] Error closing CoC client: {e}")

    # Close database last — FULL checkpoint + direct os.remove() of sidecar files.
    try:
        if CACHE.db_manager is not None:  # type: ignore[attr-defined]
            await asyncio.wait_for(CACHE.db_manager.close(), timeout=10.0)  # type: ignore[attr-defined]
            logging.info("[MAINTENANCE] Database closed.")
    except asyncio.TimeoutError:
        logging.warning("[MAINTENANCE] Timed out waiting for database to close.")
    except Exception as e:
        logging.warning(f"[MAINTENANCE] Error closing database: {e}")

    # Log lifetime stats before marking cleaned_up (async_cleanup will be a no-op after this)
    from QapBot import log_lifetime_stats
    log_lifetime_stats()
    # Mark cleaned_up so async_cleanup() on eventual exit doesn't double-close
    QBcore.cleaned_up = True


@app_commands.command(name="admin", description=dev_mode+"Administrative diagnostic actions and utilities")
@app_commands.describe(
    action="Select the administrative action to perform"
)
@app_commands.choices(action=[
    app_commands.Choice(name="Cleanup Messages - Clean up this discord server (admin)", value="CLEANUP_MESSAGES"),
    app_commands.Choice(name="Cleanup Messages All - Clean up all channels/servers (bot admin)", value="CLEANUP_MESSAGES_ALL"),
    app_commands.Choice(name="Check Logs - Scan and summarize logs (bot admin)", value="CHECK_LOGS"),
    app_commands.Choice(name="Check Data - Validate data consistency (bot admin)", value="CHECK_DATA"),
    app_commands.Choice(name="List All Subscriptions - View all subscriptions (bot admin)", value="LIST_ALL_SUBSCRIPTIONS"),
    app_commands.Choice(name="Test Notify - Test war notifications for a clan (bot admin)", value="TEST_NOTIFY"),
    app_commands.Choice(name="Manage Testers - Add/remove users who receive live-testing DMs (bot admin)", value="MANAGE_TESTERS"),
    app_commands.Choice(name="Bot Setup - Configure tracker channels (bot admin)", value="BOT_SETUP"),
    app_commands.Choice(name="Remove Clan - Remove a clan from tracking (admin)", value="REMOVE_CLAN"),
    app_commands.Choice(name="List all tracked Clans - List all tracked clans with names and tags (admin)", value="LIST_CLANS"),
    app_commands.Choice(name="Import Player Registration Data - Import player accounts from ClashPerk embed (bot admin)", value="IMPORT_DATA"),
    app_commands.Choice(name="Analyze Message - Fetch and analyze a Discord message structure (admin)", value="DEBUG_MESSAGE"),
    app_commands.Choice(name="Refresh Data - Force refresh all clan data from CoC API (bot admin)", value="REFRESH_DATA"),
    app_commands.Choice(name="Retrieve CWL Data - Backfill CWL history for a clan (bot admin)", value="RETRIEVE_CWL"),
    app_commands.Choice(name="Backfill CWL Groups - Fetch league groups for clans with <7 rounds (bot admin)", value="BACKFILL_CWL_GROUPS"),
    app_commands.Choice(name="War Prediction - Predict outcome between two clans (bot admin)", value="WAR_PREDICT"),
    app_commands.Choice(name="Start Update Cycle - Skip sleep phase and start next update cycle immediately (bot admin)", value="START_UPDATE_CYCLE"),
    app_commands.Choice(name="Execute Nightly Maintenance - Run archive move + DB ANALYZE/REINDEX/VACUUM now (bot admin)", value="OPTIMIZE_DB"),
    app_commands.Choice(name="Memory Profile - Dump memory allocation stats to log file (bot admin)", value="MEMORY_PROFILE"),
    app_commands.Choice(name="Maintenance Start - Suspend updates and close DB for safe data access (bot admin)", value="MAINTENANCE_START"),
    app_commands.Choice(name="Maintenance End - Restart bot and resume normal operation (bot admin)", value="MAINTENANCE_END"),
])
# DM-invokable (Phase 0b follow-up, CWL_ROSTER_PLANNING_PLAN.md) — per-sub-action review found 17 of
# 20 sub-actions have no functional interaction.guild dependency at all (bot-wide/global effect via
# check_bot_admin_only, or global CACHE data). Three sub-actions have a genuine, narrow guild scope
# and are individually gated inside their own branch rather than blocking the whole command — see
# IMPORT_DATA (needs a specific guild channel's ClashPerk embed message — "current channel" is
# meaningless in a DM) and TEST_NOTIFY/MANAGE_TESTERS (their user-picker uses discord.ui.UserSelect,
# a guild-scoped component — MANAGE_TESTERS' allowlist itself is bot-wide, only its picker UI needs
# a guild). REMOVE_CLAN/DEBUG_MESSAGE open a modal as their first response, which can't be preceded
# by resolve_guild_context()'s ambiguous-guild picker (also a first response) — so their permission
# check is deliberately left DM-narrower than the rest: bot-admins get full DM access as before,
# guild-admins-only get the existing "you need admin permissions" rejection rather than a picker.
async def admin(
    interaction: discord.Interaction,
    action: str
):
    """
    Administrative actions command with multiple subactions.
    
    Actions include diagnostic tools and administrative utilities.
    """
    action_norm = action.upper()
    _log_cmd(interaction, "admin", action=action)

    # Handle MAINTENANCE_START action (bot admin only)
    if action_norm == "MAINTENANCE_START":
        from qapbot.QBdiscocmdshelper import check_bot_admin_only
        if not check_bot_admin_only(interaction, CONFIG.server_admin):
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.response.send_message(
                t('commands.errors.bot_admin_only', guild_id=guild_id), ephemeral=True
            )
            return
        if QBcore.maintenance_mode:
            await interaction.response.send_message(
                "⚠️ Bot is already in maintenance mode.", ephemeral=True
            )
            return
        # Set the flag BEFORE deferring so periodic_main skips the next cycle immediately
        QBcore.maintenance_mode = True
        logging.info(f"[MAINTENANCE] Started by {interaction.user} ({interaction.user.id})")

        try:
            await interaction.response.defer(thinking=True, ephemeral=True)
        except discord.NotFound:
            # Interaction expired before we could respond (e.g. event loop was briefly
            # busy after a reconnect).  Reset the flag so the bot is not stuck in a
            # maintenance-mode limbo where all commands are blocked but resources are
            # never actually closed.
            QBcore.maintenance_mode = False
            logging.warning("[MAINTENANCE] Interaction expired before defer — maintenance mode cancelled (flag reset).")
            return

        # If a cycle is currently running, defer resource shutdown to the end of that
        # cycle — periodic_main() checks maintenance_pending after main() returns and
        # calls do_maintenance_shutdown() before going to sleep.  This avoids any
        # timeout: we simply wait for the natural end of the cycle, however long it takes.
        if not QBcore.cycle_idle_event.is_set():
            QBcore.maintenance_pending = True
            QBcore.maintenance_pending_interaction = interaction
            QBcore.maintenance_pending_interaction_time = datetime.now().timestamp()
            logging.info("[MAINTENANCE] Update cycle in progress — shutdown deferred until cycle ends.")
            await interaction.edit_original_response(
                content=(
                    "⚠️ **WARNING — Do NOT touch any data files yet!**\n\n"
                    "An update cycle is currently in progress. The database and CoC client "
                    "are still active and writing data.\n\n"
                    "Maintenance mode has been queued — resources will be closed automatically "
                    "as soon as the running cycle finishes.\n\n"
                    "**Wait for this message to update before accessing or modifying any files.**"
                )
            )
            return

        # No cycle running — if DB optimization/nightly maintenance is active,
        # wait for it to finish before closing DB resources for maintenance mode.
        # This prevents closing the DB while VACUUM/REINDEX is in progress.
        if not QBcore.db_maintenance_idle_event.is_set():
            logging.info("[MAINTENANCE] Waiting for active DB maintenance to finish before shutdown...")
            await interaction.edit_original_response(
                content=(
                    "⏳ **Maintenance mode queued.**\n\n"
                    "A database maintenance task is currently running.\n"
                    "Waiting for it to finish before closing resources safely..."
                )
            )
            await QBcore.db_maintenance_idle_event.wait()

        await do_maintenance_shutdown()

        await interaction.edit_original_response(
            content=(
                "🔧 **Maintenance mode is now active.**\n\n"
                "• All periodic update cycles are suspended\n"
                "• CoC API client closed\n"
                "• Database connection closed — safe to copy/edit `data/`\n"
                "• All slash commands are blocked for regular users\n\n"
                "Use **`/admin Maintenance End`** when done to restart the bot."
            )
        )
        return

    # Handle MAINTENANCE_END action (bot admin only)
    if action_norm == "MAINTENANCE_END":
        from qapbot.QBdiscocmdshelper import check_bot_admin_only
        if not check_bot_admin_only(interaction, CONFIG.server_admin):
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.response.send_message(
                t('commands.errors.bot_admin_only', guild_id=guild_id), ephemeral=True
            )
            return
        if not QBcore.maintenance_mode:
            await interaction.response.send_message(
                "⚠️ Bot is not currently in maintenance mode.", ephemeral=True
            )
            return
        if not await _safe_defer(interaction, thinking=False, ephemeral=True):
            return
        await interaction.followup.send(
            "🔧 **Maintenance ending.** Bot is restarting now — normal operation will resume in a few seconds.",
            ephemeral=True
        )
        logging.info(
            f"[MAINTENANCE] Ended by {interaction.user} ({interaction.user.id}) — "
            f"restarting with exit code {QBcore.EXIT_CODE_MAINTENANCE}"
        )
        QBcore.exit_code = QBcore.EXIT_CODE_MAINTENANCE
        QBcore.shutdown_event.set()
        return

    # Handle RETRIEVE_CWL action (bot admin only)
    if action_norm == "RETRIEVE_CWL":
        from qapbot.QBdiscocmdshelper import check_bot_admin_only
        if not check_bot_admin_only(interaction, CONFIG.server_admin):
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.response.send_message(
                t('commands.errors.bot_admin_only', guild_id=guild_id), ephemeral=True
            )
            return

        class RetrieveCWLModal(discord.ui.Modal, title="Retrieve CWL Data"):
            clantag = discord.ui.TextInput(label="Clan Tag", placeholder="#CLAN_TAG", max_length=15)  # type: ignore[var-annotated]
            war_tags_input = discord.ui.TextInput(  # type: ignore[var-annotated]
                label="War Tags (comma-separated, # optional)",
                placeholder="8L880P9CG, 2ABCD1234, ... (leave blank = use CoC API auto-fetch)",
                required=False,
                max_length=200,
            )

            async def on_submit(self, modal_interaction: discord.Interaction) -> None:
                if not await _safe_defer(modal_interaction, thinking=True, ephemeral=True):
                    return

                raw_tag = str(self.clantag.value)  # type: ignore[attr-defined]
                from qapbot.QBdiscocmdshelper import normalize_clan_tag as _norm
                clan_tag_norm = _norm(raw_tag)
                if not clan_tag_norm:
                    guild_id = modal_interaction.guild.id if modal_interaction.guild else None
                    await modal_interaction.followup.send(
                        t('commands.errors.invalid_clan_tag_format', guild_id=guild_id), ephemeral=True
                    )
                    return

                raw_wars = str(self.war_tags_input.value or "").strip()  # type: ignore[attr-defined]
                if raw_wars:
                    # Parse comma-separated war tags; prepend # if missing
                    war_tags = [
                        (f"#{p}" if not p.startswith('#') else p)
                        for part in raw_wars.split(',')
                        if (p := part.strip().upper())
                    ]
                    from QBhelperfunctions import backfill_cwl_from_war_tags  # type: ignore[attr-defined]
                    result_msg = await backfill_cwl_from_war_tags(clan_tag_norm, war_tags)
                else:
                    from QBhelperfunctions import backfill_last_cwl_for_clan  # type: ignore[attr-defined]
                    result_msg = await backfill_last_cwl_for_clan(clan_tag_norm)

                await modal_interaction.followup.send(result_msg, ephemeral=True)
                logging.info(
                    f"ADMIN ACTION: {modal_interaction.user} triggered CWL backfill for {clan_tag_norm}: {result_msg}"
                )

        await interaction.response.send_modal(RetrieveCWLModal())
        return

    # Handle BACKFILL_CWL_GROUPS action (bot admin only)
    if action_norm == "BACKFILL_CWL_GROUPS":
        from qapbot.QBdiscocmdshelper import check_bot_admin_only
        if not check_bot_admin_only(interaction, CONFIG.server_admin):
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.response.send_message(
                t('commands.errors.bot_admin_only', guild_id=guild_id), ephemeral=True
            )
            return

        class BackfillCWLGroupsModal(discord.ui.Modal, title="Backfill CWL League Groups"):
            season_input = discord.ui.TextInput(  # type: ignore[var-annotated]
                label="Season (YYYY-MM)",
                placeholder="2026-05",
                max_length=7,
            )

            async def on_submit(self, modal_interaction: discord.Interaction) -> None:
                if not await _safe_defer(modal_interaction, thinking=True, ephemeral=True):
                    return
                season = str(self.season_input.value).strip()  # type: ignore[attr-defined]
                from qapbot.QBdiscocmdshelper_admin_command import handle_backfill_cwl_groups  # type: ignore[attr-defined]
                try:
                    message = await handle_backfill_cwl_groups(CACHE, season)
                    await modal_interaction.followup.send(message, ephemeral=True)
                except Exception as e:
                    await modal_interaction.followup.send(f"❌ Backfill failed: {e}", ephemeral=True)
                    logging.error(f"BACKFILL_CWL_GROUPS failed: {e}", exc_info=True)
                logging.info(
                    f"ADMIN ACTION: {modal_interaction.user} triggered CWL group backfill "
                    f"for season {season}"
                )

        await interaction.response.send_modal(BackfillCWLGroupsModal())
        return

    # Handle WAR_PREDICT action (bot admin only)
    if action_norm == "WAR_PREDICT":
        from qapbot.QBdiscocmdshelper import check_bot_admin_only
        if not check_bot_admin_only(interaction, CONFIG.server_admin):
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.response.send_message(
                t('commands.errors.bot_admin_only', guild_id=guild_id), ephemeral=True
            )
            return

        class WarPredictModal(discord.ui.Modal, title="War Prediction"):
            clan1 = discord.ui.TextInput(label="Clan 1 Tag (treated as \"our\" clan)", placeholder="#CLAN1TAG", max_length=15)  # type: ignore[var-annotated]
            clan2 = discord.ui.TextInput(label="Clan 2 Tag (opponent)", placeholder="#CLAN2TAG", max_length=15)  # type: ignore[var-annotated]
            participants = discord.ui.TextInput(label="Players per side (e.g. 5, 10, 15)", placeholder="15", max_length=3, required=False)  # type: ignore[var-annotated]
            attacks_input = discord.ui.TextInput(label="Attacks per player (1 = regular CW, 2 = CWL)", placeholder="1", max_length=1, required=False)  # type: ignore[var-annotated]

            async def on_submit(self, modal_interaction: discord.Interaction) -> None:
                if not await _safe_defer(modal_interaction, thinking=True, ephemeral=True):
                    return
                from qapbot.QBdiscocmdshelper import normalize_clan_tag as _norm
                guild_id = modal_interaction.guild.id if modal_interaction.guild else None

                tag1 = _norm(str(self.clan1.value))  # type: ignore[attr-defined]
                tag2 = _norm(str(self.clan2.value))  # type: ignore[attr-defined]
                if not tag1:
                    await modal_interaction.followup.send(
                        t('commands.errors.invalid_clan_tag_format', guild_id=guild_id), ephemeral=True
                    )
                    return
                if not tag2:
                    await modal_interaction.followup.send(
                        t('commands.errors.invalid_clan_tag_format', guild_id=guild_id), ephemeral=True
                    )
                    return

                raw_n = str(self.participants.value).strip()  # type: ignore[attr-defined]
                try:
                    n_players = int(raw_n)
                    if n_players < 1 or n_players > 50:
                        raise ValueError()
                except (ValueError, TypeError):
                    n_players = 15

                raw_apm = str(self.attacks_input.value).strip()  # type: ignore[attr-defined]
                apm = 2 if raw_apm == "2" else 1

                from QBhelperfunctions import predict_war_between_clans  # type: ignore[attr-defined]
                result_msg = await predict_war_between_clans(tag1, tag2, n_players, apm)
                await modal_interaction.followup.send(result_msg, ephemeral=True)
                logging.info(
                    f"ADMIN ACTION: {modal_interaction.user} ran war prediction {tag1} vs {tag2} ({n_players}v{n_players}, apm={apm})"
                )

        await interaction.response.send_modal(WarPredictModal())
        return

    # Handle REMOVE_CLAN action. DM-invokable, but deliberately not resolved_guild_id-aware: this
    # opens a modal as its first response, which can't be preceded by resolve_guild_context()'s
    # ambiguous-guild picker (also a first response — Discord allows only one). Bot-admins already
    # pass via the existing fallback below; a guild-admin-only caller gets the normal "you need
    # admin permissions" rejection from DM rather than a picker, which is a narrower-but-safe
    # default, not a regression (identical to today's DM behavior before this phase).
    if action_norm == "REMOVE_CLAN":
        if not await check_admin_permissions(interaction, SERVER_ADMIN):
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.response.send_message(t('commands.errors.admin_required', guild_id=guild_id), ephemeral=True)
            return
        
        # Create a modal to ask for clan tag
        class RemoveClanModal(discord.ui.Modal, title="Remove Clan"):
            clantag = discord.ui.TextInput(label="Clan Tag", placeholder="#ABC123", max_length=10)  # type: ignore[var-annotated]
            
            async def on_submit(self, modal_interaction: discord.Interaction) -> None:
                if not await _safe_defer(modal_interaction, thinking=True, ephemeral=True):
                    return
                
                clantag = normalize_clan_tag(str(self.clantag.value))  # type: ignore[attr-defined]
                if not clantag:
                    guild_id = modal_interaction.guild.id if modal_interaction.guild else None
                    await modal_interaction.followup.send(t('commands.errors.invalid_clan_tag_format', guild_id=guild_id), ephemeral=True)
                    return
                if clantag not in CACHE.clan_name_cache:
                    guild_id = modal_interaction.guild.id if modal_interaction.guild else None
                    await modal_interaction.followup.send(t('commands.errors.clan_not_tracked', guild_id=guild_id, clan_tag=clantag), ephemeral=True)
                    return
                
                clan_display = get_clan_display_name(clantag)
                
                subs = CACHE.get_all_subscriptions_flat()
                for _, clans in subs.items():
                    for s in clans:
                        clan_tag = s.get('clan_tag')  # type: ignore[misc]
                        if clan_tag == clantag:
                            guild_id = modal_interaction.guild.id if modal_interaction.guild else None
                            await modal_interaction.followup.send(t('commands.errors.clan_still_subscribed', guild_id=guild_id, clan_display=clan_display), ephemeral=True)
                            return
                
                try:
                    # Remove from in-memory cache and database (write-through)
                    await CACHE.delete_clan_from_cache(clantag)
                    logging.info(f"Removed clan from cache: {clantag}")
                    
                    # Remove leaderboard messages for this clan (prevent FK constraint error on shutdown)
                    messages_to_remove = [key for key, data in CACHE.leaderboard_messages.items() if data.get('clan_tag') == clantag]
                    for key in messages_to_remove:
                        await CACHE.delete_leaderboard_message(key)
                    if messages_to_remove:
                        logging.info(f"Removed {len(messages_to_remove)} leaderboard messages for clan {clantag}")
                    
                    guild_id = modal_interaction.guild.id if modal_interaction.guild else None
                    await modal_interaction.followup.send(t('commands.errors.clan_removed', guild_id=guild_id, clan_display=clan_display), ephemeral=True)
                except Exception as save_error:
                    logging.error(f"FATAL: Failed to save clan cache after removing {clantag}: {save_error}")
                    guild_id = modal_interaction.guild.id if modal_interaction.guild else None
                    await modal_interaction.followup.send(t('commands.errors.failed_to_save_clan_cache', guild_id=guild_id, clan_display=clan_display), ephemeral=True)
        
        # Show the modal
        await interaction.response.send_modal(RemoveClanModal())
        return
    
    # Handle LIST_CLANS action — shows a modal to collect an optional filter string
    if action_norm == "LIST_CLANS":
        MAX_LIST_CLANS = 100

        class ListClansModal(discord.ui.Modal, title="List Tracked Clans"):
            filter_input = discord.ui.TextInput(  # type: ignore[var-annotated]
                label="Filter (name or tag contains)",
                placeholder="Leave blank to list first 100 clans",
                required=False,
                max_length=50,
            )

            async def on_submit(self, modal_interaction: discord.Interaction) -> None:
                if not await _safe_defer(modal_interaction, thinking=True, ephemeral=True):
                    return
                filter_str = (str(self.filter_input.value) or "").strip().lower()  # type: ignore[attr-defined]

                # Build sorted clan list applying optional filter
                all_sorted = sorted(
                    CACHE.clan_name_cache.items(),
                    key=lambda item: (CACHE.get_clan_name(item[0], "") or "").lower(),
                )
                total_all = len(all_sorted)

                if filter_str:
                    matched = [
                        (tag, _)
                        for tag, _ in all_sorted
                        if filter_str in tag.lower() or filter_str in (CACHE.get_clan_name(tag, "") or "").lower()
                    ]
                else:
                    matched = all_sorted  # type: ignore[assignment]

                total_matched = len(matched)
                clans_to_show = matched[:MAX_LIST_CLANS]
                truncated = total_matched > MAX_LIST_CLANS

                # Build header
                if filter_str:
                    header = f"Tracked clans matching '{filter_str}' ({min(total_matched, MAX_LIST_CLANS)}/{total_matched} shown)\n"
                else:
                    header = f"Tracked clans: {min(total_matched, MAX_LIST_CLANS)}/{total_all} shown\n"
                if truncated:
                    header += f"(showing first {MAX_LIST_CLANS} — use a filter to narrow results)\n"

                table_header = f"{'Tag':<15} {'Name':<25}\n"
                separator = f"{'-'*15} {'-'*25}\n"

                clan_lines = []
                for tag, _ in clans_to_show:
                    name = CACHE.get_clan_name(tag, "Unknown Clan")
                    clan_lines.append(f"{tag:<15} {name:<25}")

                # Chunk into 1900-char blocks (code block adds 6 chars for ```)
                MAX_CHARS = 1900
                base_text = header + table_header + separator
                base_len = len(base_text)

                chunks: List[str] = []
                current_chunk_lines: List[str] = []
                current_len = base_len

                for line in clan_lines:
                    line_len = len(line) + 1  # +1 for newline
                    if current_len + line_len > MAX_CHARS and current_chunk_lines:
                        chunk_text = base_text + "\n".join(current_chunk_lines)
                        chunks.append(f"```{chunk_text}```")
                        current_chunk_lines = []
                        current_len = base_len
                    current_chunk_lines.append(line)
                    current_len += line_len

                if current_chunk_lines:
                    chunk_text = base_text + "\n".join(current_chunk_lines)
                    chunks.append(f"```{chunk_text}```")

                if not chunks:
                    no_result = f"No clans found{f' matching \"{filter_str}\"' if filter_str else ''}."
                    await modal_interaction.followup.send(f"```{no_result}```", ephemeral=True)
                    return

                await modal_interaction.followup.send(chunks[0], ephemeral=True)
                for chunk in chunks[1:]:
                    await modal_interaction.followup.send(chunk, ephemeral=True)

        await interaction.response.send_modal(ListClansModal())
        return
    
    # Handle IMPORT_DATA action (bot admin only - shows modal)
    # Handle IMPORT_DATA action — stays guild-only in effect: the modal below already rejects
    # a missing interaction.guild on submit ("Command must be used in a guild channel") since
    # the ClashPerk embed being imported lives in a specific guild channel that a DM's "current
    # channel" can never be — the modal opens fine from a DM, it just can't succeed there. No
    # code change needed; documenting why this one stays effectively guild-scoped.
    if action_norm == "IMPORT_DATA":
        from qapbot.QBdiscocmdshelper import check_bot_admin_only
        if not check_bot_admin_only(interaction, CONFIG.server_admin):
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.response.send_message(
                t('commands.errors.bot_admin_only', guild_id=guild_id),
                ephemeral=True
            )
            return
        
        # Create a modal to ask for message ID
        class ImportDataModal(discord.ui.Modal, title="Import Player Registration Data"):
            message_id = discord.ui.TextInput(label="Message ID", placeholder="Paste the ClashPerk embed message ID", max_length=20)  # type: ignore[var-annotated]
            
            async def on_submit(self, modal_interaction: discord.Interaction) -> None:
                if not await _safe_defer(modal_interaction, thinking=True, ephemeral=True):
                    return
                
                try:
                    message_id_int = int(str(self.message_id.value))  # type: ignore[has-type]
                except ValueError:
                    await modal_interaction.followup.send(
                        "❌ Invalid message_id. Must be a numeric Discord message ID.",
                        ephemeral=True
                    )
                    return
                
                # Use current channel where command was issued
                current_channel_id = interaction.channel_id
                
                # Parse the ClashPerk embed
                try:
                    from qapbot.import_clashperk_userlist import parse_clashperk_embed, analyze_import_changes, DuplicatePlayerNamesError  # type: ignore[attr-defined]
                    from qapbot.ui_clan_management import ImportDataConfirmView, SwitchViewContinueView
                    
                    if not interaction.guild or not current_channel_id:
                        await modal_interaction.followup.send("❌ Command must be used in a guild channel.", ephemeral=True)
                        return
                    
                    results, clan_tag, clan_name, verified_count, total_count = await parse_clashperk_embed(  # type: ignore[misc]
                        guild=interaction.guild,
                        channel_id=current_channel_id,
                        message_id=message_id_int
                    )
                except DuplicatePlayerNamesError as e:  # type: ignore[misc]
                    # Duplicate player names detected - prompt user to switch view
                    duplicate_embed = discord.Embed(
                        title="⚠️ Duplicate Player Names Detected",
                        description=f"This clan has players with identical names, which prevents accurate matching in 'Sorted by Name' view.",
                        color=discord.Color.orange()
                    )
                    duplicate_embed.add_field(
                        name="Duplicate Names Found",
                        value=", ".join(f"`{name}`" for name in e.duplicate_names[:10]),  # type: ignore[misc, union-attr]
                        inline=False
                    )
                    duplicate_embed.add_field(
                        name="📋 Action Required",
                        value="1. React to the ClashPerk message to switch view\n"
                              "2. Select **'Sorted by Tag'** view\n"
                              "3. Click the **'Continue'** button below",
                        inline=False
                    )
                    duplicate_embed.set_footer(text="The 'Sorted by Tag' view shows player tags which allows accurate matching.")
                    
                    if current_channel_id is None:
                        await modal_interaction.followup.send("❌ Channel ID unavailable.", ephemeral=True)
                        return
                    
                    view = SwitchViewContinueView(  # type: ignore[misc]
                        original_interaction=interaction,
                        channel_id=current_channel_id,
                        message_id=message_id_int,
                        duplicate_names=e.duplicate_names,  # type: ignore[misc, arg-type]
                        clan_name=e.clan_name or "Unknown",  # type: ignore[misc, arg-type]
                        clan_tag=e.clan_tag or "Unknown",  # type: ignore[misc, arg-type]
                        discord_usernames=e.discord_usernames  # type: ignore[misc, arg-type]
                    )
                    
                    await modal_interaction.followup.send(
                        embed=duplicate_embed,
                        view=view,
                        ephemeral=True
                    )
                    return
                except Exception as e:
                    import traceback
                    error_msg = f"❌ Failed to parse ClashPerk embed: {str(e)}"
                    error_lower = str(e).lower()
                    
                    if "rate limit" in error_lower:
                        error_msg += "\n\n**Tip**: CoC API rate limit reached. Please try again in a few moments."
                    elif "failed to fetch clan data" in error_lower:
                        error_msg += "\n\n**Tip**: The clan tag may be invalid or the CoC API is temporarily unavailable."
                    elif "no players found" in error_lower:
                        error_msg += "\n\n**Tip**: The embed format doesn't match ClashPerk's clan management format. Make sure you're using the correct message."
                    
                    is_user_error = any(phrase in error_lower for phrase in ["rate limit", "failed to fetch", "no players found"])
                    
                    error_embed = discord.Embed(
                        title="❌ Import Failed",
                        description=error_msg,
                        color=discord.Color.red()
                    )
                    
                    if is_user_error:
                        logging.warning(f"Import data validation failed: {str(e)}")
                    else:
                        logging.error(f"Import data unexpected error: {traceback.format_exc()}")
                    
                    await modal_interaction.followup.send(embed=error_embed, ephemeral=True)
                    return
                
                # Use in-memory CACHE instead of loading from disk
                user_accounts = CACHE.user_accounts
                
                # Analyze what will change
                to_add, to_upgrade, to_skip = analyze_import_changes(user_accounts, results)  # type: ignore[has-type]
                
                # Count Discord users found vs not found
                found_count = sum(1 for v in results.values() if v[1] is not None)  # type: ignore[has-type]
                not_found_count = len(results) - found_count  # type: ignore[arg-type]
                
                # Create preview embed
                preview_embed = discord.Embed(
                    title=f"📊 Import Preview - {clan_name}",
                    description=f"Review the changes that will be made if you confirm this import.",
                    color=discord.Color.blue()
                )
                
                preview_embed.add_field(
                    name="📋 Source Information",
                    value=f"**Clan**: {clan_name} ({clan_tag})\n"
                          f"**Total Players**: {total_count}\n"
                          f"**Verified Accounts**: {verified_count} 🔐\n"
                          f"**Unverified Accounts**: {total_count - verified_count} 🔓",
                    inline=False
                )
                
                preview_embed.add_field(
                    name="🔍 Discord Matching",
                    value=f"**Found**: {found_count} players\n"
                          f"**Not Found**: {not_found_count} players",
                    inline=False
                )
                
                preview_embed.add_field(
                    name="📈 Changes Summary",
                    value=f"➕ **New Players**: {len(to_add)}\n"  # type: ignore[arg-type]
                          f"⬆️ **Upgraded Status**: {len(to_upgrade)}\n"  # type: ignore[arg-type]
                          f"⏭️ **Skipped**: {len(to_skip)}",  # type: ignore[arg-type]
                    inline=False
                )
                
                # Create confirmation view
                view = ImportDataConfirmView(  # type: ignore[misc]
                    original_interaction=interaction,
                    user_accounts=user_accounts,
                    results=results,  # type: ignore[arg-type]
                    clan_tag=clan_tag,
                    clan_name=clan_name,
                    to_add_count=len(to_add),  # type: ignore[arg-type]
                    to_upgrade_count=len(to_upgrade),  # type: ignore[arg-type]
                    to_skip_count=len(to_skip)  # type: ignore[arg-type]
                )
                
                await modal_interaction.followup.send(embed=preview_embed, view=view, ephemeral=True)
        
        # Show the modal
        await interaction.response.send_modal(ImportDataModal())
        return
    
    # Handle REFRESH_DATA action (bot admin only)
    if action_norm == "REFRESH_DATA":
        if not await _safe_defer(interaction, thinking=True, ephemeral=True):
            return
        from qapbot.QBdiscocmdshelper import check_bot_admin_only, get_guild_clans_including_member_config
        guild_id = interaction.guild.id if interaction.guild else None
        if not check_bot_admin_only(interaction, CONFIG.server_admin):
            await interaction.followup.send(
                t('commands.errors.bot_admin_only', guild_id=guild_id),
                ephemeral=True
            )
            return
        # Collect all unique clan tags across every guild
        all_tags: set[str] = set()
        for gid_str in CACHE.server_config:
            all_tags.update(get_guild_clans_including_member_config(int(gid_str)))
        if not all_tags:
            await interaction.followup.send("ℹ️ No clans are currently tracked.", ephemeral=True)
            return
        # Invalidate cache for each tag, then parallel-fetch all
        for tag in all_tags:
            CACHE.coc_clan_cache.invalidate(tag)
        results = await asyncio.gather(
            *[CACHE.coc_clan_cache.get_clan(tag) for tag in all_tags],
            return_exceptions=True
        )
        ok = sum(1 for r in results if not isinstance(r, Exception))
        fail = len(results) - ok
        msg = f"✅ Refreshed **{ok}/{len(all_tags)}** clans from CoC API."
        if fail:
            msg += f"\n⚠️ {fail} clan(s) failed to refresh."
        await interaction.followup.send(msg, ephemeral=True)
        return

    # Handle DEBUG_MESSAGE action. Same modal-vs-picker tradeoff as REMOVE_CLAN, above — DM-invokable
    # for bot-admins (existing fallback), guild-admins-only get the normal rejection rather than a
    # picker. The fetch itself is already cross-guild by design (QBcore.bot.get_channel/fetch_channel),
    # so no guild-object substitution would be needed even if the picker conflict were resolved later.
    if action_norm == "DEBUG_MESSAGE":
        if not await check_admin_permissions(interaction, SERVER_ADMIN):
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.response.send_message(t('commands.errors.admin_required', guild_id=guild_id), ephemeral=True)
            return
        
        # Create a modal to ask for message and channel ID
        class DebugMessageModal(discord.ui.Modal, title="Debug Message"):
            message_id = discord.ui.TextInput(label="Message ID", placeholder="The message ID to fetch", max_length=20)  # type: ignore[var-annotated]
            channel_id = discord.ui.TextInput(label="Channel ID (optional)", placeholder="Defaults to current channel", max_length=20, required=False)  # type: ignore[var-annotated]
            
            async def on_submit(self, modal_interaction: discord.Interaction) -> None:
                if not await _safe_defer(modal_interaction, thinking=True, ephemeral=True):
                    return
                
                try:
                    # Determine which channel to fetch from
                    if str(self.channel_id.value).strip():  # type: ignore[attr-defined]
                        try:
                            # Fetch channel directly from bot (works across guilds)
                            channel = QBcore.bot.get_channel(int(str(self.channel_id.value)))  # type: ignore[attr-defined]
                            if not channel:
                                channel = await QBcore.bot.fetch_channel(int(str(self.channel_id.value)))  # type: ignore[attr-defined]
                        except Exception as e:
                            guild_id = modal_interaction.guild.id if modal_interaction.guild else None
                            await modal_interaction.followup.send(t('commands.errors.could_not_fetch_channel', guild_id=guild_id, error=str(e)), ephemeral=True)
                            return
                    else:
                        channel = interaction.channel
                    
                    if not channel or not hasattr(channel, 'fetch_message'):
                        await modal_interaction.followup.send("❌ Channel not available or doesn't support messages.", ephemeral=True)
                        return
                    
                    # Fetch the message
                    try:
                        message = await channel.fetch_message(int(str(self.message_id.value)))  # type: ignore[union-attr]
                    except Exception as e:
                        guild_id = modal_interaction.guild.id if modal_interaction.guild else None
                        await modal_interaction.followup.send(t('commands.errors.could_not_fetch_message', guild_id=guild_id, error=str(e)), ephemeral=True)
                        return
                    
                    # Build analysis report
                    report: List[str] = []
                    report.append(f"**Message Analysis for ID: {str(self.message_id.value)}**\n")  # type: ignore[attr-defined]
                    report.append(f"**Author:** {message.author} ({message.author.id})")  # type: ignore[attr-defined,union-attr]
                    # Handle channels that may not have a name attribute
                    channel_name = getattr(channel, 'name', f'Channel {channel.id}')
                    report.append(f"**Channel:** {channel_name} ({channel.id})")
                    report.append(f"**Created:** {message.created_at}\n")  # type: ignore[union-attr]
                    
                    # Content
                    if message.content:  # type: ignore[union-attr]
                        report.append(f"**Content:**\n```\n{message.content[:500]}\n```\n")  # type: ignore[union-attr]
                    else:
                        report.append("**Content:** (empty)\n")
                    
                    # Embeds
                    if message.embeds:  # type: ignore[union-attr]
                        report.append(f"**Embeds:** {len(message.embeds)} embed(s)\n")  # type: ignore[union-attr,arg-type]
                        for idx, embed in enumerate(message.embeds):  # type: ignore[union-attr,arg-type]
                            report.append(f"**--- Embed {idx + 1} ---**")
                            report.append(f"Type: {embed.type}")
                            report.append(f"Color: {embed.color}")
                            
                            if embed.title:
                                report.append(f"Title: {embed.title}")
                            if embed.description:
                                desc_preview = embed.description[:300].replace('\n', '\\n')
                                report.append(f"Description: {desc_preview}...")
                            if embed.url:
                                report.append(f"URL: {embed.url}")
                            if embed.author:
                                report.append(f"Author: {embed.author.name}")
                            if embed.footer:
                                report.append(f"Footer: {embed.footer.text}")
                            if embed.thumbnail:
                                report.append(f"Thumbnail: {embed.thumbnail.url}")
                            if embed.image:
                                report.append(f"Image: {embed.image.url}")
                            
                            # Fields
                            if embed.fields:
                                report.append(f"Fields: {len(embed.fields)} field(s)")
                                for field_idx, field in enumerate(embed.fields):
                                    field_value = field.value if field.value else ""
                                    field_preview = field_value[:100].replace('\n', '\\n')
                                    report.append(f"  Field {field_idx + 1}: name='{field.name}', inline={field.inline}")
                                    report.append(f"    value: {field_preview}...")
                            
                            report.append("")  # Blank line between embeds
                    else:
                        report.append("**Embeds:** None\n")
                    
                    # Components (buttons, selects, etc.)
                    if message.components:  # type: ignore[union-attr]
                        report.append(f"**Components:** {len(message.components)} component(s)")  # type: ignore[union-attr,arg-type]
                        for comp_idx, action_row in enumerate(message.components):  # type: ignore[union-attr,arg-type]
                            if hasattr(action_row, 'children'):
                                report.append(f"  Action Row {comp_idx + 1}: {len(action_row.children)} item(s)")  # type: ignore[union-attr]
                    
                    # Send report in chunks if needed
                    full_report = "\n".join(report)
                    
                    # Discord has a 2000 char limit per message, split if needed
                    if len(full_report) <= 1900:
                        await modal_interaction.followup.send(full_report, ephemeral=True)
                    else:
                        # Split into chunks
                        chunks: List[str] = []
                        current_chunk: List[str] = []
                        current_length = 0
                        
                        for line in report:
                            line_length = len(line) + 1  # +1 for newline
                            if current_length + line_length > 1900:
                                chunks.append("\n".join(current_chunk))
                                current_chunk = [line]
                                current_length = line_length
                            else:
                                current_chunk.append(line)
                                current_length += line_length
                        
                        if current_chunk:
                            chunks.append("\n".join(current_chunk))
                        
                        # Send chunks
                        for idx, chunk in enumerate(chunks):
                            if idx == 0:
                                await modal_interaction.followup.send(chunk, ephemeral=True)
                            else:
                                guild_id = modal_interaction.guild.id if modal_interaction.guild else None
                                continued_msg = t('commands.errors.debug_continued', guild_id=guild_id, current=idx + 1, total=len(chunks))
                                await modal_interaction.followup.send(f"{continued_msg}\n{chunk}", ephemeral=True)
                    
                    # Also log to console for detailed analysis
                    logging.info(f"Message {str(self.message_id.value)} structure analysis:")  # type: ignore[attr-defined]
                    logging.info(f"Content: {message.content}")  # type: ignore[union-attr]
                    if message.embeds:  # type: ignore[union-attr]
                        for idx, embed in enumerate(message.embeds):  # type: ignore[union-attr,arg-type]
                            logging.info(f"Embed {idx}: {embed.to_dict()}")
                    
                except Exception as e:
                    logging.error(f"Error in debug_message command: {e}", exc_info=True)
                    guild_id = modal_interaction.guild.id if modal_interaction.guild else None
                    await modal_interaction.followup.send(t('commands.errors.error_analyzing_message', guild_id=guild_id, error=str(e)), ephemeral=True)
        
        # Show the modal
        await interaction.response.send_modal(DebugMessageModal())
        return
    
    # Handle CLEANUP_MESSAGES action (channel scope — also DM-safe, cleans up the bot's own
    # orphaned messages in whichever channel this was invoked from, including a DM channel)
    if action_norm == "CLEANUP_MESSAGES":
        # resolve_guild_context() may need to send the interaction's first response itself (the
        # ambiguous multi-guild DM picker) — must run before _safe_defer(), not after. If it
        # already responded (the picker), _safe_defer() must be skipped entirely — it would raise
        # discord.InteractionResponded on an already-answered interaction.
        resolved_guild_id = await resolve_guild_context(interaction)
        if not interaction.response.is_done():
            if not await _safe_defer(interaction, thinking=True, ephemeral=True):
                return
        if not await check_admin_permissions(interaction, SERVER_ADMIN, resolved_guild_id=resolved_guild_id):
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.followup.send(t('commands.errors.admin_required', guild_id=guild_id), ephemeral=True)
            return
        from qapbot.QBdiscocmdshelper_admin_command import handle_cleanup_messages_channel  # type: ignore[attr-defined]
        try:
            message = await handle_cleanup_messages_channel(interaction, interaction.channel, QBcore.bot, str(interaction.user))
            await interaction.followup.send(message, ephemeral=True)
        except Exception as e:
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.followup.send(t('commands.errors.error_during_channel_cleanup', guild_id=guild_id, error=str(e)), ephemeral=True)
            logging.error(f"CLEANUP_MESSAGES failed: {e}")
        return

    # Handle CLEANUP_MESSAGES_ALL action (all channels, bot admin only)
    if action_norm == "CLEANUP_MESSAGES_ALL":
        if not await _safe_defer(interaction, thinking=True, ephemeral=True):
            return
        from qapbot.QBdiscocmdshelper import check_bot_admin_only
        if not check_bot_admin_only(interaction, SERVER_ADMIN):
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.followup.send(
                t('commands.errors.global_cleanup_restricted', guild_id=guild_id), ephemeral=True)
            return
        from qapbot.QBdiscocmdshelper_admin_command import handle_cleanup_messages_all  # type: ignore[attr-defined]
        try:
            message = await handle_cleanup_messages_all(QBcore.bot, CACHE, str(interaction.user))
            await interaction.followup.send(message, ephemeral=True)
        except Exception as e:
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.followup.send(t('commands.errors.error_during_global_cleanup', guild_id=guild_id, error=str(e)), ephemeral=True)
            logging.error(f"CLEANUP_MESSAGES_ALL failed: {e}")
        return
    
    # Handle CHECK_LOGS action
    if action_norm == "CHECK_LOGS":
        if not await _safe_defer(interaction, thinking=True, ephemeral=True):
            return
        from qapbot.QBdiscocmdshelper import check_bot_admin_only
        if not check_bot_admin_only(interaction, SERVER_ADMIN):
            await interaction.followup.send(
                "🔒 This command is restricted to the bot administrator.\n"
                "Only the configured bot admin can access diagnostic logs.",
                ephemeral=True
            )
            return
        
        from qapbot.QBdiscocmdshelper_admin_command import scan_logs, format_log_summary, format_nightly_maintenance_stats
        import os

        log_dir = os.path.join(CONFIG.data_dir, "logs")

        try:
            scan_result = scan_logs(log_dir)
            nightly_maint_section = format_nightly_maintenance_stats(QBcore.nightly_maintenance_durations, log_dir)
            summary_text = format_log_summary(scan_result, bot_version=QBcore.BOT_VERSION, nightly_maint_section=nightly_maint_section)
            await interaction.followup.send(summary_text, ephemeral=True)
            logging.info(f"ADMIN ACTION: {interaction.user} CHECK_LOGS")
        except Exception as e:
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.followup.send(t('commands.errors.error_scanning_logs', guild_id=guild_id, error=str(e)), ephemeral=True)
            logging.error(f"CHECK_LOGS failed: {e}")
        return
    
    # Handle CHECK_DATA action
    if action_norm == "CHECK_DATA":
        if not await _safe_defer(interaction, thinking=True, ephemeral=True):
            return
        from qapbot.QBdiscocmdshelper import check_bot_admin_only
        if not check_bot_admin_only(interaction, SERVER_ADMIN):
            await interaction.followup.send(
                "🔒 This command is restricted to the bot administrator.\n"
                "Only the configured bot admin can access history data diagnostics.",
                ephemeral=True
            )
            return
        
        from qapbot.QBdiscocmdshelper_admin_command import check_database_consistency, format_database_check_results
        import os
        
        data_dir = CONFIG.data_dir
        db_path = CONFIG.db_path
        
        try:
            # Run in a thread so the event loop (and Discord heartbeats) stay alive
            check_results = await asyncio.to_thread(check_database_consistency, db_path, data_dir, archive_dir=CONFIG.archive_dir)
            summary_text = format_database_check_results(check_results)
            await interaction.followup.send(summary_text, ephemeral=True)
            logging.info(f"ADMIN ACTION: {interaction.user} CHECK_DATA (database validation)")
        except Exception as e:
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.followup.send(t('commands.errors.error_checking_data', guild_id=guild_id, error=str(e)), ephemeral=True)
            logging.error(f"CHECK_DATA failed: {e}")
        return
    
    # Handle LIST_ALL_SUBSCRIPTIONS action
    if action_norm == "LIST_ALL_SUBSCRIPTIONS":
        if not await _safe_defer(interaction, thinking=True, ephemeral=True):
            return
        from qapbot.QBdiscocmdshelper import check_bot_admin_only
        if not check_bot_admin_only(interaction, SERVER_ADMIN):
            await interaction.followup.send(
                "🔒 This command is restricted to the bot administrator.\n"
                "Only the configured bot admin can view all subscriptions across guilds.",
                ephemeral=True
            )
            return
        
        from qapbot.QBdiscocmdshelper_admin_command import list_all_subscriptions  # type: ignore[attr-defined]
        
        try:
            chunks = await list_all_subscriptions(QBcore.bot, CACHE)
            for chunk in chunks:
                await interaction.followup.send(chunk, ephemeral=True)
            logging.info(f"ADMIN ACTION: {interaction.user} LIST_ALL_SUBSCRIPTIONS")
        except Exception as e:
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.followup.send(t('commands.errors.error_listing_subscriptions', guild_id=guild_id, error=str(e)), ephemeral=True)
            logging.error(f"LIST_ALL_SUBSCRIPTIONS failed: {e}")
        return
    
    # Handle TEST_NOTIFY action — stays guild-only: the user picker below is a
    # discord.ui.UserSelect, a guild-scoped component (populated from a guild's member list;
    # has no DM equivalent), unlike the rest of /admin.
    if action_norm == "TEST_NOTIFY":
        if not await _safe_defer(interaction, thinking=True, ephemeral=True):
            return
        if interaction.guild is None:
            await interaction.followup.send(t('commands.errors.dms_only_error', guild_id=None), ephemeral=True)
            return
        from qapbot.QBdiscocmdshelper import check_bot_admin_only
        if not check_bot_admin_only(interaction, SERVER_ADMIN):
            await interaction.followup.send(
                "🔒 This command is restricted to the bot administrator.\n"
                "Only the configured bot admin can test notifications.",
                ephemeral=True
            )
            return

        # Create a custom view with user select for test notifications
        class TestNotifyView(discord.ui.View):
            def __init__(self, original_interaction: discord.Interaction):
                super().__init__(timeout=180)
                self.original_interaction = original_interaction
                self.message: Optional[discord.Message] = None
                self.user_select: discord.ui.UserSelect = discord.ui.UserSelect(  # type: ignore[var-annotated]
                    placeholder="Select a user to send test notification",
                    min_values=1,
                    max_values=1
                )
                async def on_user_select_callback(select_interaction: discord.Interaction):
                    return await self.on_user_select(select_interaction)
                self.user_select.callback = on_user_select_callback  # type: ignore[method-assign]
                self.add_item(self.user_select)  # type: ignore[arg-type]

            async def on_timeout(self) -> None:
                """Delete the message when the view times out."""
                if self.message is not None:
                    try:
                        await self.message.delete()
                    except Exception:
                        pass
            
            async def on_user_select(self, select_interaction: discord.Interaction):
                """Send mock-up test notification to selected user"""
                selected_member = self.user_select.values[0]  # type: ignore[attr-defined]

                # Defer immediately so we stay within the 3-second interaction window
                # while the async DM send runs (can take several seconds).
                if not await _safe_defer(select_interaction, ephemeral=True):
                    return

                # Delete the user selection message (standard procedure for consecutive ephemerals)
                try:
                    await self.original_interaction.delete_original_response()
                except discord.NotFound:
                    pass  # Message already deleted
                except Exception as e:
                    logging.warning(f"Failed to delete TEST_NOTIFY selection message: {e}")
                
                try:
                    # Import war notification formatting functions
                    from qapbot.war_notifications import _format_aggregated_reminder_message  # type: ignore[misc]

                    # Ensure user metadata is current (cache handles API calls internally)
                    user_data = await CACHE.ensure_user_metadata(str(selected_member.id))
                    selected_user_name = user_data.get("display_name", selected_member.display_name)
                    
                    # Get user's language preference from user_data
                    user_language = user_data.get('user_language', 'en')
                    
                    # DEBUG: Log language detection
                    logging.info(f"TEST_NOTIFY: User {selected_user_name} ({selected_member.id})")
                    logging.info(f"  - Stored user_language: {user_data.get('user_language', 'missing')}")
                    logging.info(f"  - Detected language: {user_language}")
                    
                    # Create mock player data for test notification
                    mock_player_list: List[Dict[str, Any]] = [
                        {
                            "player_name": "Player One",
                            "attacks_remaining": 1,
                            "clan_name": "Test Clan",
                            "opponent_name": "Enemy Clan",
                            "hours_remaining": 2.0
                        },
                        {
                            "player_name": "Player Two",
                            "attacks_remaining": 1,
                            "clan_name": "Test Clan",
                            "opponent_name": "Enemy Clan",
                            "hours_remaining": 2.0
                        },
                        {
                            "player_name": "Player Three",
                            "attacks_remaining": 2,
                            "clan_name": "Test Clan",
                            "opponent_name": "Enemy Clan",
                            "hours_remaining": 2.0
                        },
                        {
                            "player_name": "Player Four",
                            "attacks_remaining": 2,
                            "clan_name": "Test Clan",
                            "opponent_name": "Enemy Clan",
                            "hours_remaining": 2.0
                        }
                    ]
                    
                    # Format message using the same function as real notifications
                    # Pass discord_user_id and guild_id for proper language resolution
                    mock_notification = _format_aggregated_reminder_message(
                        selected_user_name, 
                        mock_player_list,
                        discord_user_id=str(selected_member.id),
                        guild_id=select_interaction.guild.id if select_interaction.guild else None
                    )
                    
                    # Send DM through cache manager (handles fetch internally)
                    success = await CACHE.send_user_dm(str(selected_member.id), mock_notification)
                    
                    if success:
                        result_message = (
                            f"✅ **Test Notification Sent**\n\n"
                            f"**Recipient:** {selected_user_name}\n"
                            f"**Stored Language:** {user_data.get('user_language', 'not set')}\n"
                            f"**Display Name:** {user_data.get('display_name', 'not set')}\n"
                            f"**Message Language:** {user_language.upper()}\n"
                            f"**Status:** Successfully delivered"
                        )
                        
                        # Only show language info if using default English
                        if user_language == 'en' and user_data.get('user_language', 'not set') == 'not set':
                            result_message += (
                                f"\n\nℹ️ Language is captured when user interacts with bot.\n"
                                f"If showing 'en', user hasn't interacted yet (defaults to English)."
                            )
                        
                        logging.info(f"ADMIN ACTION: {interaction.user} sent TEST_NOTIFY to {selected_user_name} ({selected_member.id}) in {user_language}")
                    else:
                        result_message = (
                            f"❌ **Failed to Send Test Notification**\n\n"
                            f"**Recipient:** {selected_user_name}\n"
                            f"**Reason:** User has DMs disabled or blocked the bot, or user not found"
                        )
                        logging.warning(f"TEST_NOTIFY failed: Cannot DM {selected_user_name} ({selected_member.id})")
                    
                    # Send result to admin
                    await select_interaction.followup.send(result_message, ephemeral=True)

                except Exception as e:
                    await select_interaction.followup.send(
                        f"❌ Error sending test notification: {e}",
                        ephemeral=True
                    )
                    logging.error(f"TEST_NOTIFY failed: {e}", exc_info=True)
        
        # Create and send view (pass interaction for message cleanup)
        view = TestNotifyView(interaction)
        view.message = await interaction.followup.send(
            "🧪 **Test Notification**\n\n"
            "Select a Discord user to send a mock-up war notification:",
            view=view,
            ephemeral=True
        )
        return

    # Handle MANAGE_TESTERS action — stays guild-only: the user picker below is a
    # discord.ui.UserSelect, a guild-scoped component (populated from a guild's member list; has
    # no DM equivalent), same restriction as TEST_NOTIFY above. The allowlist it manages
    # (CACHE.testers) is itself bot-wide, not guild-scoped — only the picker UI needs a guild.
    if action_norm == "MANAGE_TESTERS":
        if not await _safe_defer(interaction, thinking=True, ephemeral=True):
            return
        if interaction.guild is None:
            await interaction.followup.send(t('commands.errors.dms_only_error', guild_id=None), ephemeral=True)
            return
        from qapbot.QBdiscocmdshelper import check_bot_admin_only
        if not check_bot_admin_only(interaction, SERVER_ADMIN):
            await interaction.followup.send(
                "🔒 This command is restricted to the bot administrator.\n"
                "Only the configured bot admin can manage the tester list.",
                ephemeral=True
            )
            return

        # SERVER_ADMIN is excluded here on purpose (2026-08-15, /list TESTERS follow-up): the bot
        # admin's own account is already an always-on DM recipient via the separate `is_admin`
        # check in every cwl_dm_restrict_to_admin-style guard — it was never added to
        # CACHE.testers by this picker and never should be, so it can't be shown as toggleable
        # here (and, symmetrically, selecting the admin's own account below is a silent no-op —
        # see on_user_select). /list TESTERS is the place to see the *effective* full list
        # (testers + the bot admin together).
        current_tester_ids = sorted(CACHE.testers - {SERVER_ADMIN})

        # Multi-select user picker for the bot-wide DM-testing allowlist (CACHE.testers) — the
        # accounts every cwl_dm_restrict_to_admin-style guard treats as equivalent to
        # CONFIG.server_admin. Pre-populated with the current list (default_values) so the admin
        # sees who's already enrolled. Whatever is selected when the picker closes *replaces* the
        # list (not an incremental add) — deselecting someone removes them, matching how a native
        # multi-select naturally reads, and needing only one control for both enroll and remove.
        class ManageTestersView(discord.ui.View):
            def __init__(self, original_interaction: discord.Interaction, tester_ids: List[str]):
                super().__init__(timeout=180)
                self.original_interaction = original_interaction
                self.message: Optional[discord.Message] = None
                select_kwargs: Dict[str, Any] = dict(
                    placeholder="Select every user who should receive live-testing DMs",
                    min_values=0,
                    max_values=25,
                )
                default_values = [discord.Object(id=int(tid)) for tid in tester_ids if tid.isdigit()]
                if default_values:
                    select_kwargs["default_values"] = default_values
                self.user_select: discord.ui.UserSelect = discord.ui.UserSelect(**select_kwargs)  # type: ignore[var-annotated]
                async def on_user_select_callback(select_interaction: discord.Interaction):
                    return await self.on_user_select(select_interaction)
                self.user_select.callback = on_user_select_callback  # type: ignore[method-assign]
                self.add_item(self.user_select)  # type: ignore[arg-type]

            async def on_timeout(self) -> None:
                """Delete the message when the view times out."""
                if self.message is not None:
                    try:
                        await self.message.delete()
                    except Exception:
                        pass

            async def on_user_select(self, select_interaction: discord.Interaction):
                """Replace CACHE.testers with whatever is currently selected."""
                if not await _safe_defer(select_interaction, ephemeral=True):
                    return

                # Delete the picker message (standard procedure for consecutive ephemerals,
                # matches TEST_NOTIFY above).
                try:
                    await self.original_interaction.delete_original_response()
                except discord.NotFound:
                    pass
                except Exception as e:
                    logging.warning(f"Failed to delete MANAGE_TESTERS selection message: {e}")

                try:
                    # SERVER_ADMIN is filtered out of both sides — selecting/deselecting the bot
                    # admin's own account here is a silent no-op, since their DM eligibility never
                    # actually depended on CACHE.testers (see the comment above where
                    # current_tester_ids is built).
                    new_ids = {str(u.id) for u in self.user_select.values} - {SERVER_ADMIN}  # type: ignore[attr-defined]
                    previous_ids = set(CACHE.testers) - {SERVER_ADMIN}
                    added = new_ids - previous_ids
                    removed = previous_ids - new_ids

                    for discord_id in added:
                        await CACHE.add_tester(discord_id)
                    for discord_id in removed:
                        await CACHE.remove_tester(discord_id)

                    lines = []
                    if added:
                        lines.append("✅ **Added:** " + ", ".join(f"<@{uid}>" for uid in sorted(added)))
                    if removed:
                        lines.append("❌ **Removed:** " + ", ".join(f"<@{uid}>" for uid in sorted(removed)))
                    if not added and not removed:
                        lines.append("No changes — selection matched the existing tester list.")
                    if new_ids:
                        lines.append("\n**Current testers (" + str(len(new_ids)) + "):** " + ", ".join(f"<@{uid}>" for uid in sorted(new_ids)))
                    else:
                        lines.append("\n**Current testers:** none")

                    await select_interaction.followup.send("\n".join(lines), ephemeral=True)
                    logging.info(
                        f"ADMIN ACTION: {interaction.user} updated tester list: "
                        f"+{sorted(added)} -{sorted(removed)}"
                    )

                except Exception as e:
                    await select_interaction.followup.send(
                        f"❌ Error updating tester list: {e}",
                        ephemeral=True
                    )
                    logging.error(f"MANAGE_TESTERS failed: {e}", exc_info=True)

        view = ManageTestersView(interaction, current_tester_ids)
        current_list_text = ", ".join(f"<@{tid}>" for tid in current_tester_ids) if current_tester_ids else "none"
        view.message = await interaction.followup.send(
            "🧪 **Manage Testers**\n\n"
            f"Currently enrolled: {current_list_text}\n\n"
            "Select the Discord users who should receive DMs alongside the bot admin while a "
            "feature is under live-testing restriction (e.g. CWL enrollment DMs while "
            "CWL_DM_RESTRICT_TO_ADMIN is on). Whatever is selected replaces the list — deselect "
            "someone to remove them.\n\n"
            "⚠️ **PROD only** — this list is ignored on a DEV host; DEV always DMs only the "
            "bot admin, same as before testers existed. The bot admin's own account always "
            "receives these DMs and isn't managed here — see `/list Testers` for the full "
            "effective recipient list.",
            view=view,
            ephemeral=True
        )
        return

    # Handle BOT_SETUP action (bot admin only) — BUG_FEATURE_TRACKER_PLAN.md Phase 2
    if action_norm == "BOT_SETUP":
        if not await _safe_defer(interaction, thinking=True, ephemeral=True):
            return
        if interaction.guild is None:
            await interaction.followup.send(t('commands.errors.dms_only_error', guild_id=None), ephemeral=True)
            return
        from qapbot.QBdiscocmdshelper import check_bot_admin_only
        if not check_bot_admin_only(interaction, SERVER_ADMIN):
            await interaction.followup.send(
                t('commands.errors.bot_admin_only', user_id=str(interaction.user.id), guild_id=interaction.guild.id),
                ephemeral=True
            )
            return
        from qapbot.ui_tracker import start_bot_setup
        await start_bot_setup(interaction)
        return

    # Handle START_UPDATE_CYCLE action (bot admin only)
    if action_norm == "START_UPDATE_CYCLE":
        # Defer immediately — setting force_cycle_event wakes the update loop which can
        # compete for the event loop before send_message completes, causing a timeout.
        if not await _safe_defer(interaction, ephemeral=True):
            return
        from qapbot.QBdiscocmdshelper import check_bot_admin_only
        if not check_bot_admin_only(interaction, CONFIG.server_admin):
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.followup.send(
                t('commands.errors.bot_admin_only', guild_id=guild_id), ephemeral=True
            )
            return
        if QBcore.maintenance_mode:
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.followup.send(
                t('commands.errors.maintenance_mode_active', guild_id=guild_id), ephemeral=True
            )
            return
        # Cycle is currently running — queue next one
        if not QBcore.cycle_idle_event.is_set():
            if QBcore.force_cycle_pending:
                await interaction.followup.send(
                    "⚠️ The next update cycle is already queued. It will start immediately once the current cycle finishes.",
                    ephemeral=True
                )
                return
            QBcore.force_cycle_pending = True
            await interaction.followup.send(
                "⏳ An update cycle is currently in progress.\n\nThe next cycle has been queued and will start immediately once the current one finishes.",
                ephemeral=True
            )
            return
        # Bot is in sleep phase — interrupt it immediately
        QBcore.force_cycle_event.set()
        await interaction.followup.send(
            "▶️ **Update cycle starting now.** The sleep phase has been interrupted — the next update cycle will begin immediately.",
            ephemeral=True
        )
        return

    # Handle OPTIMIZE_DB action (bot admin only)
    if action_norm == "OPTIMIZE_DB":
        if not await _safe_defer(interaction, ephemeral=True):
            return
        from qapbot.QBdiscocmdshelper import check_bot_admin_only
        if not check_bot_admin_only(interaction, CONFIG.server_admin):
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.followup.send(
                t('commands.errors.bot_admin_only', guild_id=guild_id), ephemeral=True
            )
            return
        if QBcore.maintenance_mode:
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.followup.send(
                t('commands.errors.maintenance_mode_active', guild_id=guild_id), ephemeral=True
            )
            return
        if CACHE.db_manager is None:
            await interaction.followup.send(
                "❌ Database manager is not available.", ephemeral=True
            )
            return

        # If a cycle is currently running, defer until after it finishes
        if not QBcore.cycle_idle_event.is_set():
            if QBcore.optimize_db_pending:
                await interaction.followup.send(
                    "⚠️ A nightly maintenance run is already queued and will run as soon as the current update cycle finishes.",
                    ephemeral=True
                )
                return
            QBcore.optimize_db_pending = True
            QBcore.optimize_db_pending_interaction = interaction
            await interaction.followup.send(
                "⏳ An update cycle is currently in progress.\n\nNightly maintenance (archive move + DB optimization) has been queued and will run automatically once the current cycle finishes.",
                ephemeral=True
            )
            return

        # Bot is idle — run immediately (identical routine to the scheduled
        # 03:00 UTC nightly task: archive move + migration-if-due + DB maintenance)
        # (interaction already deferred at top of block)
        logging.info(f"[DB-OPTIMIZE] Manual nightly maintenance run triggered by {interaction.user} ({interaction.user.id})")
        _db_mgr = CACHE.db_manager

        async def _run_optimize_and_reply() -> None:
            QBcore.db_maintenance_idle_event.clear()
            try:
                # ignore_in_process_claim=True: a manual admin trigger should always be
                # able to run "one more chunk" of a still-incomplete migration, regardless
                # of what already auto-fired earlier in this bot process or what day of
                # the month it is — see is_monthly_migration_due()'s docstring.
                _run_migration_opt = await is_monthly_migration_due(ignore_in_process_claim=True)
                # Short budget (default 1 min, not the 90-min scheduled-nightly one) — this
                # is an interactive, user-awaited command whose actual purpose is the
                # maintenance steps (checkpoint/VACUUM/REINDEX/ANALYZE), not migration
                # progress (the opportunistic per-cycle chunk already carries that); a long
                # migration wait here also risks the Discord interaction token (~15 min)
                # expiring before the reply can be sent.
                result = await run_nightly_maintenance_routine(
                    _db_mgr,
                    _run_migration_opt,
                    migration_time_budget_seconds=CONFIG.history_migration_admin_budget_minutes * 60,
                )
                try:
                    await interaction.followup.send(
                        f"✅ **Nightly maintenance complete.**\n```\n{result}\n```",
                        ephemeral=True
                    )
                except Exception as _disc_ex:
                    # Interaction token expires after 15 min — VACUUM on HDD can exceed this
                    logging.warning(f"[DB-OPTIMIZE] Could not send result to Discord (token likely expired after long VACUUM): {_disc_ex}")
                    logging.info(f"[DB-OPTIMIZE] Result that could not be sent: {result}")
            except Exception as _ex:
                logging.error(f"[DB-OPTIMIZE] Error: {_ex}")
                try:
                    await interaction.followup.send(
                        f"❌ Nightly maintenance failed: {_ex}", ephemeral=True
                    )
                except Exception:
                    pass
            finally:
                QBcore.db_maintenance_idle_event.set()

        QBcore.spawn_tracked("admin-optimize-db", _run_optimize_and_reply())
        return

    # Handle MEMORY_PROFILE action (bot admin only)
    if action_norm == "MEMORY_PROFILE":
        if not await _safe_defer(interaction, thinking=True, ephemeral=True):
            return
        from qapbot.QBdiscocmdshelper import check_bot_admin_only
        if not check_bot_admin_only(interaction, SERVER_ADMIN):
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.followup.send(
                t('commands.errors.bot_admin_only', guild_id=guild_id), ephemeral=True
            )
            return
        from qapbot.QBdiscocmdshelper_admin_command import handle_memory_profile  # type: ignore[attr-defined]
        try:
            message = await handle_memory_profile(CACHE)
            await interaction.followup.send(message, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Memory profile failed: {e}", ephemeral=True)
            logging.error(f"MEMORY_PROFILE failed: {e}", exc_info=True)
        return

    # Unknown action
    guild_id = interaction.guild.id if interaction.guild else None
    await interaction.followup.send(t('commands.errors.unknown_action', guild_id=guild_id, action=action), ephemeral=True)

# Create command group for clan-related commands
clan_group = app_commands.Group(name="clan", description=dev_mode+"Clan management commands")

@clan_group.command(name="management", description=dev_mode+"Display clan members with linking status and tools to link player accounts.")
# NOT converted in Phase 0b (CWL_ROSTER_PLANNING_PLAN.md): needs a real discord.Guild object for
# member lookups (format_clan_management_message) and posts a non-ephemeral, persistent
# interactive view via interaction.channel.send(), guarded on isinstance(interaction.channel,
# (TextChannel, Thread)) — a DM channel fails that guard. Revisit only with a real Guild-object
# substitution (interaction.client.get_guild(resolved_guild_id)) and a decision on where a
# DM-invoked instance of this view would actually post.
@app_commands.guild_only()
@app_commands.checks.cooldown(1, 10.0, key=lambda i: (i.guild_id, i.user.id))
async def clan_management(interaction: discord.Interaction):
    """
    Display clan management interface showing linked/unlinked players.
    
    Shows:
    - Clan selector dropdown (pre-selects most active clan on this server)
    - All players in the clan with their TH level and linking status
    - Interactive tools to quickly link unlinked players to Discord users
    - Refresh button to update the view
    
    Note:
        - Message is non-ephemeral (visible to everyone)
        - Only admins can interact with UI elements
        - Old messages are automatically deleted when new ones are posted
    
    Args:
        interaction: Discord interaction object
    """
    if not await _safe_defer(interaction, thinking=True, ephemeral=False):
        return
    _log_cmd(interaction, "clan management")
    
    from qapbot.QBdiscocmdshelper import format_clan_management_message, get_most_active_clan_for_guild, get_guild_clans_including_member_config
    from qapbot.ui_clan_management import ClanManagementView
    from QBhelperfunctions import generate_message_key_timestamp
    
    if not interaction.guild_id or not interaction.guild:
        await interaction.followup.send("❌ This command must be used in a guild.", ephemeral=True)
        return
    
    # Get all clans including subscriptions and member configurations
    guild_clans = get_guild_clans_including_member_config(interaction.guild_id)
    
    if not guild_clans:
        await interaction.followup.send(
            "❌ No clans are subscribed to this server. Use `/subscribe` to add clans first.",
            ephemeral=True
        )
        return
    
    # Get most active clan for this guild (default selection)
    default_clan_tag = get_most_active_clan_for_guild(interaction.guild_id, guild_clans)
    
    # Generate clan management message for default clan (default mode: config)
    try:
        main_embed, unlinked_embed, _linked_players, unlinked_players = await format_clan_management_message(
            default_clan_tag,
            interaction.guild,
            mode="config"  # Default to config mode (Basic Configuration)
        )
    except Exception as e:
        logging.error(f"Failed to generate clan management message for {default_clan_tag}: {e}")
        await interaction.followup.send(
            f"❌ Failed to generate clan management view: {str(e)}",
            ephemeral=True
        )
        return
    
    # Delete old clan_management messages in this channel
    if not interaction.channel or not hasattr(interaction.channel, 'fetch_message'):
        await interaction.followup.send("❌ Channel doesn't support messages.", ephemeral=True)
        return
    
    channel_id = str(interaction.channel.id)
    keys_to_delete = [k for k, v in CACHE.leaderboard_messages.items()
                      if v.get('channel_id') == channel_id and v.get('mode') == 'clan_management']
    
    for k in keys_to_delete:
        v = CACHE.leaderboard_messages[k]
        msg_ids = v.get('message_ids', '')
        if msg_ids:
            for msg_id in msg_ids.split(','):
                try:
                    msg = await interaction.channel.fetch_message(int(msg_id))  # type: ignore[union-attr]
                    await msg.delete()  # type: ignore[union-attr]
                    logging.debug(f"Deleted old clan_management message {msg_id}")
                except discord.NotFound:
                    logging.debug(f"Old clan_management message {msg_id} already deleted")
                except Exception as e:
                    logging.debug(f"Could not delete old clan_management message {msg_id}: {e}")
        await CACHE.delete_leaderboard_message(k)
    
    # Delete the deferred "thinking" message
    try:
        await interaction.delete_original_response()
    except Exception:
        pass
    
    # Detect mode based on return values and apply rules
    all_embeds: List[discord.Embed] = []
    display_embeds: List[discord.Embed]
    if main_embed is None and isinstance(unlinked_embed, builtins.list):  # type: ignore[misc]
        # Rule 3: Pagination mode (>6000 chars) - multiple embeds returned as list
        all_embeds = unlinked_embed  # type: ignore[assignment]
        display_embeds = [all_embeds[0]]
        current_page = 0
    elif main_embed is not None and unlinked_embed is not None and not isinstance(unlinked_embed, builtins.list):  # type: ignore[misc]
        # Rule 2: Two-embed mode (4096 < chars <= 6000)
        all_embeds = []
        display_embeds = [main_embed, unlinked_embed]
        current_page = 0
    else:
        # Rule 1: Single embed mode (<= 4096 chars)
        all_embeds = []
        display_embeds = [main_embed] if main_embed else []
        current_page = 0
    
    if not interaction.channel or not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
        await interaction.followup.send("❌ Cannot send message in this channel type.", ephemeral=True)
        return
    
    # Send new message
    sent_message = await interaction.channel.send(
        embeds=display_embeds
    )
    
    # Create interactive view with clan selector and mode selector
    view = ClanManagementView(
        clan_tag=default_clan_tag,
        guild_clans=guild_clans,
        unlinked_players=unlinked_players,
        sent_message=sent_message,
        mode="config",  # Default mode (Basic Configuration)
        timeout=1800,  # 30 minute timeout (int required)
        all_embeds=all_embeds if all_embeds else None,
        current_page=current_page
    )
    
    # Edit message to add view
    await sent_message.edit(view=view)
    
    # Track the message
    timestamp = generate_message_key_timestamp()
    message_key = f"{channel_id}_{timestamp}_clan_management"
    await CACHE.set_leaderboard_message(message_key, {
        'clan_tag': None,  # No clan associated with clan_management messages
        'channel_id': channel_id,
        'mode': 'clan_management',
        'message_ids': str(sent_message.id),
        'content_hash': ''  # Not used for clan_management
    })


# =============================================================================
# /analyse command group
# =============================================================================

analyse_group = app_commands.Group(name="analyse", description=dev_mode+"Performance analysis commands for Clash of Clans war data.")


@analyse_group.command(
    name="cwl_league_group",
    description=dev_mode+"Analyse all clans in a CWL group: top attackers and best defenders.",
)
@app_commands.describe(clan="Clan tag or name to look up the CWL league group for")
# DM-invokable (Phase 0b, CWL_ROSTER_PLANNING_PLAN.md) — display-only guild_id, no functional guild dependency.
@app_commands.checks.cooldown(1, 30.0, key=lambda i: (i.guild_id, i.user.id))
async def analyse_leaguegroup(interaction: discord.Interaction, clan: str) -> None:
    """
    Fetch the current/last CWL league group for the given clan and rank every
    player across all clans in the group by offensive and defensive performance.

    Reports two categories (top 10 each):
    - ⚔️  Best attackers: highest Ø⭐ on equal-or-higher TH targets
    - 🛡️  Best defenders: fewest Ø⭐ conceded per defense

    Shows player names, tags, clans, clan tags, and performance values.
    Use the autocomplete dropdown to pick a guild clan, or type any clan tag directly.
    """
    if not await _safe_defer(interaction, thinking=True, ephemeral=False):
        return
    _log_cmd(interaction, "analyse leaguegroup", clan=clan)
    guild_id = interaction.guild.id if interaction.guild else None

    # Resolve clan tag — mirrors the pattern used by /subscribe and /leaderboard
    if not clan or not clan.strip():
        await interaction.followup.send(t('commands.errors.invalid_clan_tag_or_name', guild_id=guild_id), ephemeral=True)
        return

    count, resolved = _get_clan_tag(clan)
    if count == 0 or not resolved:
        normalized = normalize_clan_tag(clan)
        if not normalized:
            await interaction.followup.send(t('commands.errors.invalid_clan_tag_or_name', guild_id=guild_id), ephemeral=True)
            return
        success, error_msg, _clan_name = await validate_and_add_clan_to_cache(normalized)
        if not success:
            await interaction.followup.send(error_msg, ephemeral=True)
            return
        clan_tag = normalized
    else:
        clan_tag = resolved

    try:
        embeds = await generate_cwl_group_analysis_embeds(clan_tag)
    except Exception as exc:
        logging.error(f"[ANALYSE] CWL group analysis failed for {clan_tag}: {exc}", exc_info=True)
        await interaction.followup.send("❌ Failed to generate analysis. Please try again later.", ephemeral=True)
        return

    if not embeds:
        clan_name = CACHE.get_clan_name(clan_tag, clan_tag) or clan_tag
        await interaction.followup.send(
            f"❌ No active CWL group found for **{clan_name}** (`{clan_tag}`).\n"
            "This command works during or shortly after the CWL season.",
            ephemeral=True,
        )
        return

    clan_name = CACHE.get_clan_name(clan_tag, clan_tag) or clan_tag
    embeds[0].set_footer(text=f"Requested for: {clan_name} ({clan_tag})")
    _log_cmd_done(interaction, "analyse leaguegroup")
    await interaction.followup.send(embeds=embeds)


@analyse_leaguegroup.autocomplete('clan')
async def analyse_leaguegroup_clan_autocomplete(interaction: discord.Interaction, current: str):
    """Autocomplete: suggests tracked clans for the guild (also accepts any typed tag). In a DM,
    offers the union of clans from every guild the caller is linked to."""
    if interaction.guild_id:
        return await get_clan_family_autocomplete_choices(current, guild_id=str(interaction.guild_id), mode="guild_first")
    from qapbot.QBdiscocmdshelper import get_dm_caller_matched_guild_ids
    guild_ids = [str(g) for g in get_dm_caller_matched_guild_ids(str(interaction.user.id))]
    return await get_clan_family_autocomplete_choices(current, guild_ids=guild_ids, mode="guild_first")


# ─────────────────────────────────────────────────────────────────────────────
# /analyse cwlopponent
# ─────────────────────────────────────────────────────────────────────────────

async def _cwlopponent_select_callback(
    interaction: discord.Interaction,
    opp_clan_tag: str,
    *,
    my_clan_tag: str,
    my_clan_name: str,
    season: str,
    league_group: Any,
    war_info_map: Optional[Dict[str, Any]] = None,
    _view: Any = None,
) -> None:
    """
    Callback: user selected an opponent clan from the dropdown.
    Defers non-ephemerally so the analysis table is visible to all.

    Roster source priority (round-aware):
      1. war_ended  → DB war_attacks by war_id  (exact per-round lineup)
      2. in_war / preparation with known war_tag → API via get_league_war
      3. Future rounds (no war yet) → league_group.clans[x].members (full registry)
      4. Last resort → get_cwl_roster_sync (most-recent war's roster from DB)
    """
    if not await _safe_defer(interaction, thinking=True, ephemeral=False):
        return

    # Delete the ephemeral dropdown message (view.message was set after followup.send)
    if _view is not None and getattr(_view, 'message', None) is not None:
        try:
            await _view.message.delete()
        except (discord.NotFound, discord.HTTPException):
            pass

    opp_clan_name = CACHE.get_clan_name(opp_clan_tag, opp_clan_tag) or opp_clan_tag
    opp_info      = (war_info_map or {}).get(opp_clan_tag, {})
    opp_war_id    = opp_info.get('war_id')
    opp_war_tag   = opp_info.get('war_tag') or ''
    opp_war_state = opp_info.get('state') or ''

    roster: List[Dict[str, Any]] = []

    # ── 1. Ended war: query war_attacks by war_id (exact per-round lineup) ────
    if not roster and opp_war_state == 'war_ended' and opp_war_id and CACHE.db_manager:
        try:
            import sqlite3 as _sq

            def _fetch_ended_roster() -> List[Dict[str, Any]]:
                with CACHE.db_manager.sync_conn() as _c:  # type: ignore[union-attr]
                    _c.row_factory = _sq.Row
                    _rows = _c.execute(
                        """
                        SELECT player_tag, player_name, th_level, map_position
                        FROM   war_attacks
                        WHERE  clan_tag = ? AND war_id = ?
                        GROUP  BY player_tag
                        ORDER  BY map_position ASC
                        """,
                        (opp_clan_tag, opp_war_id),
                    ).fetchall()
                    return [
                        {
                            'player_tag':   r['player_tag'],
                            'player_name':  r['player_name'],
                            'th_level':     int(r['th_level']),
                            'map_position': int(r['map_position']),
                        }
                        for r in _rows
                    ]

            roster = await asyncio.to_thread(_fetch_ended_roster)
            if roster:
                logging.debug(
                    f"[CWLOPPONENT] roster from DB war_attacks war_id={opp_war_id} "
                    f"({len(roster)} players)"
                )
        except Exception as _ex:
            logging.warning(
                f"[CWLOPPONENT] DB war_attacks fetch failed for {opp_clan_tag} "
                f"war_id={opp_war_id}: {_ex}"
            )

    # ── 2. In-war / preparation: fetch live from API via war_tag ─────────────
    if not roster and opp_war_tag and opp_war_tag != '#0':
        try:
            _war = await CACHE.get_league_war(opp_war_tag)
            if _war is not None:  # type: ignore[truthy-bool]
                _war_clan = getattr(_war, 'clan', None)
                _war_opp  = getattr(_war, 'opponent', None)
                _opp_side = None
                for _side in [_war_clan, _war_opp]:
                    if _side and str(getattr(_side, 'tag', '')) == opp_clan_tag:
                        _opp_side = _side
                        opp_clan_name = str(getattr(_side, 'name', opp_clan_name) or opp_clan_name)
                        break
                if _opp_side is not None:
                    for _m in [*(getattr(_opp_side, 'members', None) or [])]:  # type: ignore[misc]
                        _mtag = str(getattr(_m, 'tag', '') or '')
                        if not _mtag:
                            continue
                        roster.append({
                            'player_tag':   _mtag,
                            'player_name':  str(getattr(_m, 'name', _mtag) or _mtag),
                            'th_level':     int(getattr(_m, 'town_hall', 0) or 0),
                            'map_position': int(
                                getattr(_m, 'map_position',
                                        getattr(_m, 'mapPosition', 99)) or 99
                            ),
                        })
                if roster:
                    logging.debug(
                        f"[CWLOPPONENT] roster from API war_tag={opp_war_tag} "
                        f"({len(roster)} players)"
                    )
        except Exception as _ex:
            logging.warning(
                f"[CWLOPPONENT] API get_league_war failed for {opp_clan_tag} "
                f"tag={opp_war_tag}: {_ex}"
            )

    # ── 3. Future rounds / no war yet: use league_group full registry ─────────
    if not roster and league_group is not None:
        for _cwl_clan in [*(getattr(league_group, 'clans', None) or [])]:
            if str(getattr(_cwl_clan, 'tag', '')) == opp_clan_tag:
                opp_clan_name = str(getattr(_cwl_clan, 'name', opp_clan_name) or opp_clan_name)
                for _m in [*(getattr(_cwl_clan, 'members', None) or [])]:
                    _mtag = str(getattr(_m, 'tag', '') or '')
                    if not _mtag:
                        continue
                    roster.append({
                        'player_tag':   _mtag,
                        'player_name':  str(getattr(_m, 'name', _mtag) or _mtag),
                        'th_level':     int(getattr(_m, 'town_hall', 0) or 0),
                        'map_position': int(getattr(_m, 'map_position', 99) or 99),
                    })
                break
        if roster:
            logging.debug(
                f"[CWLOPPONENT] roster from league_group ({len(roster)} players)"
            )

    # ── 4. Last resort: DB most-recent war roster for this opponent ───────────
    if not roster and CACHE.db_manager:
        try:
            _db_roster = await asyncio.to_thread(
                CACHE.db_manager.get_cwl_roster_sync, opp_clan_tag, season
            )
            roster = [
                {
                    'player_tag':   r['player_tag'],
                    'player_name':  r['player_name'],
                    'th_level':     r['th_level'],
                    'map_position': r['map_position'],
                }
                for r in _db_roster
            ]
            if roster:
                logging.debug(
                    f"[CWLOPPONENT] roster from get_cwl_roster_sync ({len(roster)} players)"
                )
        except Exception as _db_ex:
            logging.warning(f"[CWLOPPONENT] DB roster fallback failed for {opp_clan_tag}: {_db_ex}")

    if not roster:
        await interaction.followup.send(
            f"❌ No roster data found for **{opp_clan_name}** (`{opp_clan_tag}`) in season {season}.",
            ephemeral=True,
        )
        return

    try:
        embeds = await build_cwl_opponent_embeds(
            opp_clan_tag=opp_clan_tag,
            opp_clan_name=opp_clan_name,
            season=season,
            roster=roster,
            my_clan_tag=my_clan_tag,
            my_clan_name=my_clan_name,
            round_number=opp_info.get('round_number'),
        )
    except Exception as exc:
        logging.error(f"[CWLOPPONENT] embed build failed for {opp_clan_tag}: {exc}", exc_info=True)
        await interaction.followup.send(
            "❌ Failed to build the analysis. Please try again later.", ephemeral=True
        )
        return

    await interaction.followup.send(embeds=embeds[:10])
    _log_cmd_done(interaction, "analyse cwlopponent")


@analyse_group.command(
    name="cwl_opponent",
    description=dev_mode + "Analyse a CWL opponent clan: player stats (skill/reliability/activity) for their roster.",
)
@app_commands.describe(clan="Clan tag or name to look up the CWL league group for")
# DM-invokable (Phase 0b, CWL_ROSTER_PLANNING_PLAN.md) — display-only guild_id, no functional guild dependency.
@app_commands.checks.cooldown(1, 30.0, key=lambda i: (i.guild_id, i.user.id))
async def analyse_cwlopponent(interaction: discord.Interaction, clan: str) -> None:
    """
    Step 1: resolve the clan's current CWL league group.
    Step 2: show a dropdown with the 7 opponent clans in the group.
    Step 3: user picks one → _cwlopponent_select_callback builds the table.
    """
    if not await _safe_defer(interaction, thinking=True, ephemeral=True):
        return
    _log_cmd(interaction, "analyse cwlopponent", clan=clan)
    guild_id = interaction.guild.id if interaction.guild else None

    if not clan or not clan.strip():
        await interaction.followup.send(
            t('commands.errors.invalid_clan_tag_or_name', guild_id=guild_id), ephemeral=True
        )
        return

    count, resolved = _get_clan_tag(clan)
    if count == 0 or not resolved:
        from qapbot.QBdiscocmdshelper import normalize_clan_tag as _norm_ct  # type: ignore[misc]
        normalized = _norm_ct(clan)
        if not normalized:
            await interaction.followup.send(
                t('commands.errors.invalid_clan_tag_or_name', guild_id=guild_id), ephemeral=True
            )
            return
        success, error_msg, _cn = await validate_and_add_clan_to_cache(normalized)
        if not success:
            await interaction.followup.send(error_msg, ephemeral=True)
            return
        clan_tag = normalized
    else:
        clan_tag = resolved

    my_clan_name = CACHE.get_clan_name(clan_tag, clan_tag) or clan_tag

    # ── Determine season and fetch league group ────────────────────────────────
    from datetime import datetime as _dt
    _now_season = _dt.now().strftime("%Y-%m")

    league_group: Any = None
    season: str = _now_season

    # Always try the live API — a mid-month bonus CWL season may be active.
    try:
        league_group = await CACHE.get_league_group(clan_tag)
        if league_group:
            season = str(getattr(league_group, 'season', _now_season) or _now_season)
    except Exception as _lg_ex:
        logging.info(f"[CWLOPPONENT] live group fetch failed for {clan_tag}: {_lg_ex}")
        league_group = None

    # If no live group, fall back to most recent DB season
    if not league_group and CACHE.db_manager:
        try:
            _last_season = await CACHE.db_manager.get_latest_cwl_season_for_clan(clan_tag)
            if _last_season:
                season = _last_season
        except Exception:
            pass

    # ── Collect opponent clan tags from the group ─────────────────────────────
    opponent_tags: List[str] = []

    if league_group is not None:
        for cwl_clan in [*(getattr(league_group, 'clans', None) or [])]:
            ctag = str(getattr(cwl_clan, 'tag', '') or '')
            if ctag and ctag != clan_tag:
                opponent_tags.append(ctag)
    elif CACHE.db_manager:
        try:
            group_info = await CACHE.db_manager.get_cwl_group_info(clan_tag, season)
            if group_info:
                opponent_tags = [
                    t_  for t_ in group_info.get('clan_tags', [])
                    if t_ != clan_tag
                ]
        except Exception as _gi_ex:
            logging.info(f"[CWLOPPONENT] DB group info failed for {clan_tag}/{season}: {_gi_ex}")

    if not opponent_tags:
        await interaction.followup.send(
            f"❌ No CWL group found for **{my_clan_name}** (`{clan_tag}`) in season **{season}**.\n"
            "This command works during or shortly after the CWL season.",
            ephemeral=True,
        )
        return

    # ── Look up which round we face each opponent (from war_summary) ──────────
    # Synchronous — runs in a thread to avoid blocking the event loop.
    def _fetch_round_map() -> Dict[str, Dict[str, Any]]:
        import sqlite3 as _sq
        if not CACHE.db_manager or not CACHE.db_manager.db_path:
            return {}
        try:
            with CACHE.db_manager.sync_conn() as _conn:
                _conn.row_factory = _sq.Row
                # LEFT JOIN cwl_league_rounds so that in-progress wars
                # (round_number still NULL in war_summary) still get their
                # round from the authoritative cwl_league_rounds table.
                rows = _conn.execute(
                    """
                    SELECT ws.opponent_tag,
                           COALESCE(ws.round_number, clr.cwl_round) AS round_number,
                           ws.war_id,
                           ws.war_tag,
                           ws.state
                    FROM   war_summary ws
                    LEFT JOIN cwl_league_rounds clr ON clr.war_tag = ws.war_tag
                    WHERE  ws.clan_tag   = ?
                      AND  ws.cwl_season = ?
                      AND  ws.is_cwl     = 1
                    """,
                    (clan_tag, season),
                ).fetchall()
                result: Dict[str, Dict[str, Any]] = {
                    r["opponent_tag"]: {
                        'round_number': r["round_number"],
                        'war_id':       r["war_id"],
                        'war_tag':      r["war_tag"],
                        'state':        r["state"],
                    }
                    for r in rows
                }

                # Also check in-memory temp war metadata for the current
                # active CWL war, which may not have a war_summary row yet.
                meta = CACHE.temp_war_metadata.get(clan_tag, {})
                if meta.get("is_cwl") and meta.get("opponent_tag"):
                    opp = meta["opponent_tag"]
                    if opp not in result:
                        wt  = meta.get("war_tag")
                        rnd = None
                        if wt:
                            rnd_row = _conn.execute(
                                "SELECT cwl_round FROM cwl_league_rounds WHERE war_tag = ?",
                                (wt,),
                            ).fetchone()
                            rnd = int(rnd_row["cwl_round"]) if rnd_row else None
                        result[opp] = {
                            'round_number': rnd,
                            'war_id':       None,  # no war_summary row yet
                            'war_tag':      wt,
                            'state':        meta.get("state"),
                        }

            return result
        except Exception as _ex:
            logging.warning(f"[CWLOPPONENT] war_info_map fetch failed: {_ex}")
            return {}

    war_info_map: Dict[str, Dict[str, Any]] = await asyncio.to_thread(_fetch_round_map)

    # ── Enrich from live league_group.rounds (async) ──────────────────────────
    # The DB covers war_ended and (via cwl_league_rounds JOIN) in_war rounds.
    # Preparation and future rounds that are already assigned a war_tag in the
    # API response are NOT in war_summary yet — iterate league_group.rounds and
    # call get_league_war() for any round not yet resolved.  This is exactly
    # what /leaderboard cwlinfo does to show the prep round.
    if league_group is not None:
        _assigned: List[Any] = [*(getattr(league_group, 'rounds', None) or [])]
        for _ri, _round_tags in enumerate(_assigned):
            _rnum = _ri + 1
            _live_tags: List[str] = [
                wt for wt in (_round_tags or [])
                if wt and wt not in ('#0', '0')
            ]
            for _wt in _live_tags:
                try:
                    _pw = await CACHE.get_league_war(_wt)
                    if not _pw:
                        continue
                    _pc = str(getattr(getattr(_pw, 'clan', None), 'tag', '') or '')
                    _po = str(getattr(getattr(_pw, 'opponent', None), 'tag', '') or '')
                    _popp = _po if _pc == clan_tag else (_pc if _po == clan_tag else '')
                    if not _popp:
                        continue
                    existing = war_info_map.get(_popp, {})
                    # Only fill in what the DB didn't know
                    if existing.get('round_number') is None:
                        existing['round_number'] = _rnum
                    if not existing.get('war_tag'):
                        existing['war_tag'] = _wt
                    if not existing.get('state'):
                        existing['state'] = str(getattr(_pw, 'state', '') or '')
                    war_info_map[_popp] = existing
                    break  # found our war for this round
                except Exception:
                    pass

    # Sort opponents: known rounds first (ascending), then unknowns
    opponent_tags_sorted = sorted(
        opponent_tags,
        key=lambda t: (
            war_info_map.get(t, {}).get('round_number') is None,
            war_info_map.get(t, {}).get('round_number') or 0,
        ),
    )

    # ── Build dropdown with up to 7 opponents ─────────────────────────────────
    from qapbot.ui_common import GenericSelectView  # type: ignore[misc]
    options: List[discord.SelectOption] = []
    for otag in opponent_tags_sorted[:7]:
        oname = CACHE.get_clan_name(otag, otag) or otag
        rnd   = war_info_map.get(otag, {}).get('round_number')
        rnd_prefix = f"Round {rnd}" if rnd is not None else "Round ?"
        label = f"{rnd_prefix}: {oname} ({otag})"
        options.append(
            discord.SelectOption(
                label=label[:100],
                value=otag,
            )
        )

    view = GenericSelectView(
        options=options,
        callback_fn=_cwlopponent_select_callback,
        placeholder="Select an opponent clan…",
        timeout=120,
        callback_kwargs={
            'my_clan_tag':  clan_tag,
            'my_clan_name': my_clan_name,
            'season':       season,
            'league_group': league_group,
            'war_info_map': war_info_map,
        },
    )
    view.message = await interaction.followup.send(
        f"🔍 **CWL Opponent Analysis** — {my_clan_name} (`{clan_tag}`)  ·  Season **{season}**\n"
        "Select an opponent clan to analyse their last known roster:",
        view=view,
        ephemeral=True,
    )
    # Patch after send so the callback can delete this message
    view.callback_kwargs['_view'] = view


@analyse_cwlopponent.autocomplete('clan')
async def analyse_cwlopponent_clan_autocomplete(interaction: discord.Interaction, current: str):
    """Autocomplete: suggests tracked clans for the guild (also accepts any typed tag). In a DM,
    offers the union of clans from every guild the caller is linked to."""
    if interaction.guild_id:
        return await get_clan_family_autocomplete_choices(current, guild_id=str(interaction.guild_id), mode="guild_first")
    from qapbot.QBdiscocmdshelper import get_dm_caller_matched_guild_ids
    guild_ids = [str(g) for g in get_dm_caller_matched_guild_ids(str(interaction.user.id))]
    return await get_clan_family_autocomplete_choices(current, guild_ids=guild_ids, mode="guild_first")


@app_commands.command(name="subscriptions", description=dev_mode+"Lists clan and family subscriptions for the current channel or entire discord server.")
@app_commands.describe(
    server_wide="If True, show all subscriptions for the entire discord server (default: False - current channel only). Always treated as True in a DM."
)
# DM-invokable (Phase 0b, CWL_ROSTER_PLANNING_PLAN.md) — read-only. server_wide=True resolves the
# guild via resolve_guild_context() when invoked from a DM (see below); the default per-channel view
# needs no guild resolution at all, since interaction.channel is meaningful (if subscription-less) in
# a DM too.
@app_commands.checks.cooldown(1, 5.0, key=lambda i: (i.guild_id, i.channel_id))
async def subscriptions(interaction: discord.Interaction, server_wide: bool = False):
    """
    Lists clan and family subscriptions for the current channel or entire discord server.

    Args:
        interaction: Discord interaction object
        server_wide: If True, show all subscriptions across all channels in the discord server.
            Forced True in a DM regardless of what's passed — "the current channel" is the DM
            itself there, which is never a meaningful subscription scope.
    """
    if interaction.guild is None:
        server_wide = True
    if server_wide:
        # resolve_guild_context() may need to send the interaction's first response itself (the
        # ambiguous multi-guild DM picker) — must run before _safe_defer(), not after.
        resolved_guild_id = await resolve_guild_context(interaction)
        if resolved_guild_id is None:
            msg = t('commands.errors.dm_not_linked', user_id=str(interaction.user.id))
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return
        if not interaction.response.is_done():
            if not await _safe_defer(interaction, thinking=False):
                return
        _log_cmd(interaction, "subscriptions", server_wide=server_wide)

        guild = QBcore.bot.get_guild(resolved_guild_id)
        if not guild:
            return
        guild_id = str(guild.id)
        guild_subs = CACHE.subscriptions.get(guild_id, {})

        if not guild_subs:
            await send_and_track(interaction, "This guild has no subscriptions.", 'subscriptions')
            return

        # Collect all subscriptions across all channels
        all_entries: List[Dict[str, Any]] = []
        for channel_id, channel_clans in guild_subs.items():
            try:
                channel = guild.get_channel(int(channel_id))
                channel_name = channel.name if channel else f"Unknown ({channel_id})"
            except:
                channel_name = f"Unknown ({channel_id})"

            for entry in channel_clans:
                if 'clan_tag' in entry:  # type: ignore[misc]
                    entry_with_channel = entry.copy()
                    entry_with_channel['channel_name'] = channel_name
                    entry_with_channel['channel_id'] = channel_id
                    all_entries.append(entry_with_channel)

        if not all_entries:
            await send_and_track(interaction, "This guild has no clan or family subscriptions.", 'subscriptions')
            return

        header = f"**Guild-Wide Subscriptions ({guild.name}):**\n"
        channel_col = 18
        name_col = 17
        tag_col = 13
        type_col = 13
        scope_col = 14
        
        table = (
            f"{'Channel':<{channel_col}} {'Clan/Family':<{name_col}} {'Tag':<{tag_col}} {'Type':<{type_col}} {'Scope':<{scope_col}}\n"
            f"{'-'*channel_col} {'-'*name_col} {'-'*tag_col} {'-'*type_col} {'-'*scope_col}\n"
        )
        
        # Sort by channel name, then clan tag
        sorted_entries = sorted(all_entries, key=lambda x: (x.get('channel_name', ''), x.get('clan_tag', '')))  # type: ignore[misc,arg-type]
        
        for entry in sorted_entries:
            channel_name = entry.get('channel_name', '')[:channel_col]  # type: ignore[misc]
            tag = entry.get('clan_tag', '')[:tag_col]  # type: ignore[misc]
            sub_type = entry.get('subscription_type', '')[:type_col]  # type: ignore[misc]
            year_scope = entry.get('year')  # type: ignore[misc]
            scope_label = "Current Year" if year_scope == "current" else (f"Year {year_scope}" if isinstance(year_scope, int) else "Current Month")
            scope_label = scope_label[:scope_col]
            
            if sub_type == "playerregistration" or tag == "PLAYERREGISTRATION":
                name = ""
                sub_type = ""
                scope_label = ""
            else:
                name = CACHE.get_clan_name(tag, '')
                if not name and tag in CACHE.clan_families:
                    name = CACHE.clan_families[tag].get('name', '')
                name = (name or '')[:name_col]
            
            table += f"{channel_name:<{channel_col}} {name:<{name_col}} {tag:<{tag_col}} {sub_type:<{type_col}} {scope_label:<{scope_col}}\n"
        
        await send_and_track(interaction, f"```{header}{table}```", 'subscriptions')
    else:
        # Original behavior: show subscriptions for current channel only — no guild resolution
        # needed, interaction.channel is meaningful (if subscription-less) in a DM too.
        if not await _safe_defer(interaction, thinking=False):
            return
        _log_cmd(interaction, "subscriptions", server_wide=server_wide)
        if not interaction.channel:
            return
        channel_id = str(interaction.channel.id)
        subs = CACHE.get_all_subscriptions_flat()
        clans = subs.get(channel_id, [])
        if not clans:
            await send_and_track(interaction, "This channel is not subscribed to any clans or families.", 'subscriptions')
            return
        header = "**Channel Subscriptions:**\n"
        name_col = 25
        tag_col = 13
        type_col = 13
        scope_col = 14
        table = (
            f"{'Clan/Family':<{name_col}} {'Tag':<{tag_col}} {'Type':<{type_col}} {'Scope':<{scope_col}}\n"
            f"{'-'*name_col} {'-'*tag_col} {'-'*type_col} {'-'*scope_col}\n"
        )
        # Extract clan_tag and subscription_type from each dict and sort by clan_tag
        sorted_entries = sorted(
            [s for s in clans if 'clan_tag' in s],  # type: ignore[misc]
            key=lambda x: str(x.get('clan_tag', ''))  # type: ignore[arg-type,return-value,misc]
        )
        for entry in sorted_entries:
            tag = (entry.get('clan_tag') or '')[:tag_col]  # type: ignore[misc]
            sub_type = entry.get('subscription_type', '')[:type_col]  # type: ignore[misc]

            year_scope = entry.get('year')  # type: ignore[misc]
            scope_label = "Current Year" if year_scope == "current" else (f"Year {year_scope}" if isinstance(year_scope, int) else "Current Month")
            scope_label = scope_label[:scope_col]

            if sub_type == "playerregistration" or tag == "PLAYERREGISTRATION":
                name = ""
                sub_type = ""
                scope_label = ""
            else:
                name = CACHE.get_clan_name(tag, '')
                if not name and tag in CACHE.clan_families:
                    name = CACHE.clan_families[tag].get('name', '')
                name = (name or '')[:name_col]

            table += f"{name:<{name_col}} {tag:<{tag_col}} {sub_type:<{type_col}} {scope_label:<{scope_col}}\n"
        await send_and_track(interaction, f"```{header}{table}```", 'subscriptions')

@app_commands.command(name="status", description=dev_mode+"Show bot status including uptime, memory usage, and cache statistics.")
@app_commands.describe(
    force_refresh="If True, refresh the cached database statistics before showing status "
                  "(default: False - use cached values, up to 25h stale; see /status output for freshness)"
)
# DM-invokable (Phase 0b, CWL_ROSTER_PLANNING_PLAN.md) — purely bot-wide stats, no interaction.guild usage.
@app_commands.checks.cooldown(1, 5.0, key=lambda i: (i.guild_id, i.channel_id))
async def status(interaction: discord.Interaction, force_refresh: bool = False):
    """
    Show bot status including uptime, memory usage, and cache statistics.

    Args:
        interaction: Discord interaction object
        force_refresh: If True, bypass the 25h database-statistics cache
            (see get_global_db_statistics_sync) and recompute it now. This
            re-runs the multi-GB full-table scan across the hot+history DBs,
            so it can take several seconds — normal /status calls should
            leave this False and rely on the cache warmed at startup /
            refreshed nightly.
    """
    if not await _safe_defer(interaction, thinking=True):
        return
    _log_cmd(interaction, "status", force_refresh=force_refresh)

    def _n(v: int) -> str:
        """Integer with European thousand separator (period)."""
        return f"{v:,}".replace(",", ".")

    def _f(v: float, d: int = 1) -> str:
        """Float with European decimal comma and thousand period."""
        s = f"{v:,.{d}f}"
        return s.replace(".", "\x00").replace(",", ".").replace("\x00", ",")

    if not getattr(QBcore.bot, "fully_initialized", False):
        await interaction.followup.send(
            "⏳ Bot is still starting up. Please try again in a few seconds.",
            ephemeral=True
        )
        return

    if not hasattr(QBcore.bot, "start_time") or QBcore.bot.start_time is None:
        QBcore.bot.start_time = datetime.now()
    uptime = datetime.now() - QBcore.bot.start_time
    process = psutil.Process(os.getpid())
    mem_mb = process.memory_info().rss / 1024 / 1024
    last_sync = getattr(QBcore.bot, "last_sync", None)
    last_sync_str = last_sync.strftime("%Y-%m-%d %H:%M:%S") if last_sync else "N/A"
    os_name = platform.system()
    os_version = platform.release()
    os_info = f"OS: {os_name} {os_version}"
    python_info = f"Python: {sys.version.split()[0]}"
    from QBwarsim import get_cpu_info as _get_cpu_info
    _cpu = _get_cpu_info()
    cpu_info = (
        f"CPU: {_cpu['cpu']}\n"
        f"Cores: {_cpu['logical_cores']} logical"
        f" | Sim workers: "
        + (f"{_cpu['sim_workers']} / {_cpu['logical_cores']}" if _cpu['sim_enabled'] else "disabled")
    )
    
    # Determine mode based on GLOBAL_GUILD_ID
    mode = "DEV" if GLOBAL_GUILD_ID else "PROD"
    
    # Get simple Discord API stats
    health_stats = get_simple_discord_stats()
    
    # Simple health status based on recent performance
    if health_stats['total_calls'] > 0:
        api_section = (
            f"API Calls: {_n(health_stats['total_calls'])} "
            f"(Success: {_f(health_stats['success_rate'])}%)\n"
            f"Rate Limits: {health_stats['rate_limits']} | "
            f"Errors: {health_stats['api_errors']}\n"
        )
    else:
        api_section = "API Calls: None yet\n"
    
    # Calculate number of subscribed servers and channels
    subscribed_servers = len(CACHE.subscriptions)
    subscribed_channels = sum(len(channels) for channels in CACHE.subscriptions.values())
    
    # Calculate number of users with notification subscriptions and their total players
    notification_users = 0
    notification_player_count = 0
    for user_data in CACHE.user_accounts.values():
        if user_data.get("notification_settings", {}).get("war_reminders", False):
            notification_users += 1
            notification_player_count += len(user_data.get("players", []))
    
    # Calculate clan update optimization stats
    # Three tiers:
    #   active       – has_active_subscriptions=True  → polled every cycle
    #   inactive_22h – track_war_updates=True, no subs  → polled on 22h threshold
    #   passively    – track_war_updates=False          → skipped entirely (enemy-only)
    active_clans = 0
    inactive_22h_clans = 0
    untracked_clans = 0
    private_log_clans = 0
    
    for clan_data in CACHE.clan_name_cache.values():
        if isinstance(clan_data, dict):  # type: ignore[misc]
            if clan_data.get('has_active_subscriptions', False):  # type: ignore[misc]
                active_clans += 1
            elif clan_data.get('track_war_updates', True):  # type: ignore[misc]
                inactive_22h_clans += 1
            else:
                untracked_clans += 1
            if not clan_data.get('warlog_is_public', True):  # type: ignore[misc]
                private_log_clans += 1
    
    # Get clan_info cache memory consumption
    clan_cache_stats = CACHE.coc_clan_cache.get_stats()
    clan_cache_memory = CACHE.coc_clan_cache.get_memory_usage_mb()

    # Get global DB statistics and archive file count in parallel (both are I/O-bound)
    from qapbot.cache_manager import count_archive_files_sync
    db_stats, archive_file_count = await asyncio.gather(
        asyncio.to_thread(CACHE.db_manager.get_global_db_statistics_sync, force_refresh=force_refresh),  # type: ignore[union-attr]
        asyncio.to_thread(count_archive_files_sync, CONFIG.archive_dir),
    )
    # War file stats from in-memory cache only (zero I/O)
    war_file_stats = CACHE.get_war_file_stats()
    
    # Get CoC API stats
    from qapbot.coc_health import get_coc_stats
    coc_stats = get_coc_stats()
    
    # Format CoC API stats section
    if coc_stats['total_calls'] > 0:
        coc_success_rate = (coc_stats['successful_calls'] / coc_stats['total_calls']) * 100
        coc_api_section = (
            f"API Calls: {_n(coc_stats['total_calls'])} "
            f"(Success: {_f(coc_success_rate)}%)\n"
            f"Rate Limits: {coc_stats['rate_limits']} | "
            f"Errors: {_n(coc_stats['api_errors'])}\n"
        )
        if coc_stats['total_sleep_time'] > 0:
            coc_api_section += f"Total Sleep Time: {_f(coc_stats['total_sleep_time'])}s\n"
        # Per-operation breakdown (top operations sorted by call count)
        _by_op: Dict[str, Any] = coc_stats.get('total_calls_by_op', {})
        if _by_op:
            _sorted_ops = sorted(_by_op.items(), key=lambda x: x[1], reverse=True)  # type: ignore[misc]
            _op_lines = ", ".join(f"{op}={_n(cnt)}" for op, cnt in _sorted_ops[:6])
            coc_api_section += f"Breakdown: {_op_lines}\n"
    else:
        coc_api_section = "API Calls: None yet\n"
    
    msg = (
        f"**QapBot Status**\n"
        f"Version: {QBcore.BOT_VERSION}\n"
        f"Mode: {mode}\n"
        f"Uptime: {str(timedelta(seconds=int(uptime.total_seconds())))}\n"
        f"{os_info}\n"
        f"{python_info}\n"
        f"{cpu_info}\n"
        f"Memory: {_f(mem_mb)} MB\n"
        f"Last Stats Sync: {last_sync_str}\n"
        f"Clan Cache (in-memory): {_n(clan_cache_stats['size'])} clans, {_f(clan_cache_memory, 2)} MB\n"
        f"\n**Database Statistics**\n"
        f"Clans tracked: {_n(db_stats['clans_count'])} (Active: {_n(active_clans)}, 22h-polled: {_n(inactive_22h_clans)}, Enemy-only: {_n(untracked_clans)})\n"
        f"Private War Logs: {_n(private_log_clans)} (still tracked for CWL)\n"
        f"Total number of players tracked: {_n(db_stats['players_tracked_count'])}\n"
        f"Wars recorded: {_n(db_stats['wars_count'])} (Currently in war: {_n(len(CACHE.in_war_clan_tags))})\n"
        f"Attacks recorded: {_n(db_stats['attacks_count'])}\n"
        f"Linked CoC accounts: {_n(db_stats['players_count'])}\n"
        f"Clan-Families: {len(CACHE.clan_families)}\n"
        f"Discord-Users: {len(CACHE.user_accounts)}\n"
        f"Notification Subscriptions: {notification_users} ({notification_player_count} CoC accounts)\n"
        f"Active Servers: {subscribed_servers}\n"
        f"Subscribed Channels: {subscribed_channels}\n"
        f"Leaderboard Messages: {_n(len(CACHE.leaderboard_messages))}\n"
        f"\n**War File Statistics**\n"
        f"Temp files: {_n(war_file_stats['total'])} "
        f"(prep: {_n(war_file_stats['prep'])}, in_war: {_n(war_file_stats['in_war'])}"
        + (f", war_ended: {_n(war_file_stats['war_ended'])}" if war_file_stats['war_ended'] > 0 else "")
        + (f", other: {_n(war_file_stats['other'])}" if war_file_stats.get('other', 0) > 0 else "")
        + f", cwl\u2248{war_file_stats['cwl_known']})\n"
        f"Archive files: {_n(archive_file_count)}\n"
        f"\n**Discord API Stats**\n"
        f"{api_section}"
        f"\n**CoC API Stats**\n"
        f"{coc_api_section}"
    )

    # Append update cycle runtime stats + all lifetime totals in one merged section
    cs = QBcore.cycle_stats
    if cs["count"] > 0:
        avg_s = cs["total_s"] / cs["count"]
        cycle_section = (
            f"Cycles completed: {_n(cs['count'])}\n"
            f"Min: {_f(cs['min_s'])}s | "
            f"Avg: {_f(avg_s)}s | "
            f"Max: {_f(cs['max_s'])}s\n"
            f"{CACHE.format_lifetime_summary()}\n"
        )
    else:
        cycle_section = "No cycles completed yet\n"
    msg += f"\n**Update Cycle Stats (since last start)**\n{cycle_section}"

    from qapbot.QBdiscocmdshelper_admin_command import format_nightly_maintenance_stats
    nightly_maint_section = format_nightly_maintenance_stats(
        QBcore.nightly_maintenance_durations, os.path.join(CONFIG.data_dir, "logs")
    )
    msg += f"\n**Nightly Maintenance**\n{nightly_maint_section}\n"

    _log_cmd_done(interaction, "status")
    await send_and_track(interaction, f"```{msg}```", 'status')

@app_commands.command(name="ping", description=dev_mode+"Show bot latency and responsiveness.")
# DM-invokable (Phase 0b, CWL_ROSTER_PLANNING_PLAN.md) — display-only guild_id, no functional guild dependency.
@app_commands.checks.cooldown(1, 5.0, key=lambda i: (i.guild_id, i.channel_id))
async def ping(interaction: discord.Interaction):
    """
    Check bot latency: WebSocket heartbeat RTT (bot.latency).

    discord.py sends a heartbeat over the persistent WebSocket connection and
    times the ACK — both timestamps on the bot's own clock, so there is no
    clock-skew error.  This is the lowest-overhead path to Discord and the
    standard "ping" metric used by discord.py bots.
    """
    guild_id = interaction.guild.id if interaction.guild else None
    _log_cmd(interaction, "ping")
    latency_ms = round(QBcore.bot.latency * 1000)
    try:
        await interaction.response.send_message(
            f"```{t('commands.errors.pong_latency', guild_id=guild_id, latency_ms=latency_ms)}```",
            ephemeral=True,
        )
    except discord.NotFound:
        logging.warning(
            "[DEFER] Interaction expired before defer — "
            f"command=ping user={interaction.user.id} guild={interaction.guild_id}"
        )
        return
    _log_cmd_done(interaction, "ping")

@app_commands.command(name="list", description=dev_mode+"List accounts, families, or players with various filters.")
@app_commands.describe(
    action="Select what to list: Accounts, Families, or Players",
    clan="(Players only) Clan tag or name to list players for",
    family="(Players only) Clan family tag to list players for",
    season="(Managed CWLs only) CWL season as YYYY-MM — defaults to the next upcoming season"
)
@app_commands.choices(action=[
    app_commands.Choice(name="Accounts - List all Discord user accounts and registered players", value="ACCOUNTS"),
    app_commands.Choice(name="Families - List all clan families and their member clans", value="FAMILIES"),
    app_commands.Choice(name="Players - List all players for a given clan or family", value="PLAYERS"),
    app_commands.Choice(name="Tracked Clans - Chart of tracked clans per war league", value="TRACKED_CLANS"),
    app_commands.Choice(name="Testers - List all DM-testing recipients (bot admin)", value="TESTERS"),
    app_commands.Choice(name="Managed CWLs - List all guilds with a managed CWL for a season (bot admin)", value="MANAGED_CWLS"),
])
# DM-invokable (Phase 0b, CWL_ROSTER_PLANNING_PLAN.md) — operates on global CACHE data, no admin gate, display-only guild_id.
@app_commands.checks.cooldown(1, 5.0, key=lambda i: (i.guild_id, i.channel_id))
async def list(
    interaction: discord.Interaction,
    action: str,
    clan: Optional[str] = None,
    family: Optional[str] = None,
    season: Optional[str] = None
):
    """
    Consolidated list command with three actions: Accounts, Families, and Players.

    Actions:
    - Accounts: List all Discord user accounts and their registered players with verification status
    - Families: List all clan families and their member clans
    - Players: List all players for a given clan or family
    """
    action_norm = action.upper()
    _log_cmd(interaction, "list", action=action, clan=clan, family=family, season=season)
    
    # Handle ACCOUNTS action
    if action_norm == "ACCOUNTS":
        if not await _safe_defer(interaction, thinking=True, ephemeral=True):
            return
        
        if not CACHE.user_accounts or all(tag == "UNASSIGNED" for tag in CACHE.user_accounts.keys()):
            empty_embed = discord.Embed(
                title="Registered Accounts",
                description="No registered accounts found.",
                color=0x2B2D31  # Dark gray
            )
            await send_and_track(interaction, command_name='list_accounts', embed=empty_embed, ephemeral=True)
            return

        from qapbot.formatting import best_practice_player_cell
        
        # Unicode left-to-right marks for proper text direction
        LRM = "\u200E"
        RLM = "\u200F"
        
        # Build embed
        embed = discord.Embed(
            color=0x2B2D31  # Dark gray matching clan_management style
        )
        
        # Set title
        embed.set_author(name="Registered Accounts")
        
        # Build description with header
        description = f"**Legend: ({BotEmojis.GCHECK} API-Verified / {BotEmojis.REDX} Not Verified)**\n\n"
        
        # Count total players and verified players
        total_players = 0
        verified_players = 0
        
        for account_tag, entry in CACHE.user_accounts.items():
            # Skip the UNASSIGNED pool - only show real Discord user accounts
            if account_tag == "UNASSIGNED":
                continue
                
            display_name = entry.get("display_name", f"UserID:{account_tag}")
            description += f"**{display_name}'s accounts:**\n"
            
            players = entry.get("players", [])
            if not players:
                description += "  *No players registered*\n\n"
            else:
                for player in players:
                    total_players += 1
                    player_name = player.get("player_name", "Unknown")
                    player_tag = player.get("player_tag", "Unknown")
                    
                    # Target widths for columns (name + tag together)
                    max_width = 38  # Total width for player name and tag
                    
                    # Format as "PlayerName (#PLAYERTAG)"
                    player_display = f"{player_name} ({player_tag})"
                    
                    # Use best-practice padding with width calculation
                    player_padded = best_practice_player_cell(player_display, max_width)
                    
                    # Add LRM markers to force LTR
                    player_padded = f"{LRM}{player_padded}{LRM}"
                    
                    # Check verification status
                    is_verified = CACHE.is_player_verified(account_tag, player_tag)
                    if is_verified:
                        verified_players += 1
                    verification_emoji = BotEmojis.GCHECK if is_verified else BotEmojis.REDX
                    
                    # Build line with inline code block
                    line = f"{verification_emoji} `{LRM}{player_padded}{RLM}`"
                    description += f"{line}\n"
                
                description += "\n"  # Extra space between users
        
        # Add summary footer
        embed.set_footer(text=f"Total: {total_players} players • Verified: {verified_players}")
        
        # Set description (send_and_track will handle truncation/splitting if needed)
        embed.description = description
        
        await send_and_track(interaction, command_name='list_accounts', embed=embed, ephemeral=True)
        return
    
    # Handle FAMILIES action
    if action_norm == "FAMILIES":
        if not await _safe_defer(interaction, thinking=False):
            return
        msg = "**Clan Families:**\n"
        for family_tag, info in CACHE.clan_families.items():
            family_name = info.get("name", "")
            clan_tags = info.get("clans", [])
            msg += f"\n{family_name} ({family_tag}) clans:\n"
            for tag in clan_tags:
                clan_name = CACHE.get_clan_name(tag, "Unknown Clan")
                msg += f"    •  {clan_name} ({tag})\n"
        await send_and_track(interaction, f"```{msg}```", 'list_families')
        return
    
    # Handle PLAYERS action
    if action_norm == "PLAYERS":
        if not await _safe_defer(interaction, thinking=False, ephemeral=True):
            return
        
        # Validate that at least one parameter is provided
        if not clan and not family:
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.followup.send(t('commands.errors.provide_clan_or_family', guild_id=guild_id), ephemeral=True)
            return
        
        # Validate that only one parameter is provided
        if clan and family:
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.followup.send(t('commands.errors.provide_one_not_both', guild_id=guild_id), ephemeral=True)
            return
        
        clan_tags_to_process = []
        display_name = ""
        
        if family:
            # Process family: get all member clan tags
            family_tag = normalize_family_tag(family.strip()) if not family.startswith('#') else family.strip()
            if not family_tag or family_tag not in CACHE.clan_families:
                guild_id = interaction.guild.id if interaction.guild else None
                await interaction.followup.send(t('commands.errors.family_not_found', guild_id=guild_id, family=family), ephemeral=True)
                return
            
            family_info = CACHE.clan_families[family_tag]
            clan_tags_to_process = family_info.get('clans', [])
            display_name = get_family_display_name(family_tag)
            
            if not clan_tags_to_process:
                guild_id = interaction.guild.id if interaction.guild else None
                await interaction.followup.send(t('commands.errors.family_has_no_clans', guild_id=guild_id, display_name=display_name), ephemeral=True)
                return
        
        else:
            # Process single clan
            if not clan:
                guild_id = interaction.guild.id if interaction.guild else None
                await interaction.followup.send(t('commands.errors.invalid_clan_tag_or_name', guild_id=guild_id), ephemeral=True)
                return
            num_matches, clantag_norm = _get_clan_tag(clan)
            if num_matches == 0 or not clantag_norm:
                guild_id = interaction.guild.id if interaction.guild else None
                await interaction.followup.send(t('commands.errors.invalid_clan_tag_or_name', guild_id=guild_id), ephemeral=True)
                return
            
            clan_tags_to_process = [clantag_norm]
            display_name = get_clan_display_name(clantag_norm)
        
        # Aggregate players from all clans
        latest_by_tag = {}
        for clan_tag in clan_tags_to_process:
            # Get current war players from cache
            temp_stats = CACHE.get_temp_war_stats(clan_tag)
            for pid, pdata in temp_stats.items():
                if pid not in latest_by_tag:
                    name = pdata.get('Player', 'Unknown')
                    latest_by_tag[pid] = name
            
            # Get history players from cache
            history_rows = CACHE.get_clan_history(clan_tag)
            for row in history_rows:
                pid = row.get('PlayerID', '')
                name = row.get('Player', 'Unknown')
                if pid and pid not in latest_by_tag:
                    latest_by_tag[pid] = name
        if not latest_by_tag:
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.followup.send(t('commands.errors.no_player_data_found', guild_id=guild_id, display_name=display_name), ephemeral=True)
            return
        
        msg = f"**Players for {display_name}:**\n"
        msg += f"{'Tag':<13} {'Name':<20}\n"
        msg += f"{'-'*13} {'-'*20}\n"
        # Sort by player name (case-insensitive), then by tag as tiebreaker
        for tag, name in sorted(latest_by_tag.items(), key=lambda x: (x[1].lower() if x[1] else 'zzz', x[0])):  # type: ignore[misc,arg-type]
            msg += f"{tag:<13} {name or 'Unknown':<20}\n"
        await send_and_track(interaction, f"```{msg}```", 'list_players')
        return

    # Handle TRACKED_CLANS action
    if action_norm == "TRACKED_CLANS":
        if not await _safe_defer(interaction, thinking=True):
            return
        guild_id = interaction.guild.id if interaction.guild else None

        # Build league → (actively tracked, passively tracked) from in-memory cache (no DB query needed)
        from collections import Counter
        league_tracked:   Counter[str] = Counter()
        league_untracked: Counter[str] = Counter()
        for clan_data in CACHE.clan_name_cache.values():
            league = clan_data.get("war_league")
            if league:
                if clan_data.get("track_war_updates", True):
                    league_tracked[league] += 1
                else:
                    league_untracked[league] += 1

        all_leagues = set(league_tracked.keys()) | set(league_untracked.keys())
        if not all_leagues:
            await interaction.followup.send(t('commands.list.tracked_clans_no_data', guild_id=guild_id), ephemeral=True)
            return

        chart_data = {l: (league_tracked[l], league_untracked[l]) for l in all_leagues}
        total_tracked_count   = sum(league_tracked.values())
        total_untracked_count = sum(league_untracked.values())
        total = total_tracked_count + total_untracked_count

        # Generate chart bytes using the shared helper in qapbot.chart_clans_per_league.
        # Run in a thread: matplotlib rendering is CPU-bound and blocks the event loop
        # for 15–20 s on slow server-machine hardware, preventing heartbeats → connection reset by Discord.
        from qapbot.chart_clans_per_league import make_chart_bytes
        import asyncio as _asyncio
        buf = io.BytesIO(await _asyncio.to_thread(make_chart_bytes, chart_data))
        await interaction.followup.send(
            content=(
                f"**Tracked Clans per War League** — {total:,} total"
                f" ({total_tracked_count:,} actively tracked, {total_untracked_count:,} passively tracked)"
            ),
            file=discord.File(buf, "tracked_clans.jpg"),
        )
        return

    # Handle TESTERS action (bot admin only) — the *effective* full DM-testing recipient list:
    # CACHE.testers (the /admin MANAGE_TESTERS allowlist, PROD-only) plus CONFIG.server_admin,
    # who always receives these DMs (DEV and PROD alike) and isn't stored in CACHE.testers at
    # all — MANAGE_TESTERS deliberately can't add/remove that account (see its comment above),
    # so this is the only place that shows the two together as one list.
    if action_norm == "TESTERS":
        if not await _safe_defer(interaction, thinking=True, ephemeral=True):
            return
        from qapbot.QBdiscocmdshelper import check_bot_admin_only
        if not check_bot_admin_only(interaction, SERVER_ADMIN):
            await interaction.followup.send(
                "🔒 This command is restricted to the bot administrator.",
                ephemeral=True
            )
            return

        tester_ids = set(CACHE.testers) - {SERVER_ADMIN}
        lines = []
        if SERVER_ADMIN:
            lines.append(f"<@{SERVER_ADMIN}> — bot admin, always included (DEV and PROD)")
        for discord_id in sorted(tester_ids):
            lines.append(f"<@{discord_id}> — tester, PROD only")

        embed = discord.Embed(
            title="DM Testers",
            description=(
                "\n".join(lines) if lines else "No DM-testing recipients configured."
            ),
            color=0x2B2D31,
        )
        embed.set_footer(text="Manage the tester list (excluding the bot admin) via /admin Manage Testers.")
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    # Handle MANAGED_CWLS action (bot admin only) — cross-guild data (which OTHER guilds have a
    # CWL set up), same sensitivity class as TESTERS above, same gate (2026-08-16, project
    # owner's spec: "gives a list of all guilds that have a managed cwl for that season").
    if action_norm == "MANAGED_CWLS":
        if not await _safe_defer(interaction, thinking=True, ephemeral=True):
            return
        guild_id = interaction.guild.id if interaction.guild else None
        from qapbot.QBdiscocmdshelper import check_bot_admin_only
        if not check_bot_admin_only(interaction, SERVER_ADMIN):
            await interaction.followup.send(t('commands.errors.bot_admin_required', guild_id=guild_id), ephemeral=True)
            return

        from qapbot.QBdiscocmdshelper_cwl import resolve_current_cwl_season

        if season:
            # YYYY-MM only — the same shape resolve_current_cwl_season()/every cwl_events row
            # already uses; reject anything else rather than silently querying a season that can
            # never match a real row.
            import re
            if not re.fullmatch(r"\d{4}-\d{2}", season):
                await interaction.followup.send(
                    t('commands.list.invalid_season_format', guild_id=guild_id, season=season), ephemeral=True
                )
                return
            resolved_season = season
        else:
            # "the next upcoming one" (project owner's spec, verbatim) — despite its name,
            # resolve_current_cwl_season() IS the next-upcoming-month resolver, not "whichever
            # season is currently selected" (that's resolve_selected_cwl_season(), which is
            # per-guild and not what a guild-agnostic default should use here).
            resolved_season = resolve_current_cwl_season()

        db = CACHE.db_manager
        rows = db.list_cwl_events_for_season_across_guilds_sync(resolved_season) if db else []

        if not rows:
            await interaction.followup.send(
                t('commands.list.managed_cwls_no_data', guild_id=guild_id, season=resolved_season), ephemeral=True
            )
            return

        def _guild_label(gid: str) -> str:
            resolved_guild = QBcore.bot.get_guild(int(gid))
            return resolved_guild.name if resolved_guild else f"Unknown guild ({gid})"

        lines = [
            f"**{_guild_label(row['guild_id'])}** — {row['status'].replace('_', ' ').title()}"
            for row in sorted(rows, key=lambda r: _guild_label(r["guild_id"]).lower())
        ]

        embed = discord.Embed(
            title=f"Managed CWLs — {resolved_season}",
            description="\n".join(lines),
            color=0x2B2D31,
        )
        embed.set_footer(text=f"{len(rows)} guild(s) with a managed CWL for {resolved_season}.")
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

@list.autocomplete('clan')  # type: ignore[attr-defined]
async def list_clan_autocomplete(interaction: discord.Interaction, current: str):
    """Autocomplete for clan: suggests all tracked clans (excludes families)."""
    return await get_clan_family_autocomplete_choices(current, mode="clans_only")

@list.autocomplete('family')  # type: ignore[attr-defined]
async def list_family_autocomplete(interaction: discord.Interaction, current: str):
    """Autocomplete for family: suggests all clan families."""
    from discord import app_commands
    
    current_lower = (current or "").lower()
    choices: List[app_commands.Choice[str]] = []
    
    for fam_tag, info in CACHE.clan_families.items():
        family_name = info.get('name', 'Family')
        label = f"{family_name} ({fam_tag})"
        if not current_lower or current_lower in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=fam_tag))
        if len(choices) >= 25:
            break
    
    return choices[:25]

@list.autocomplete('season')  # type: ignore[attr-defined]
async def list_season_autocomplete(interaction: discord.Interaction, current: str):
    """Autocomplete for season (Managed CWLs only): the next 6 calendar months as YYYY-MM
    convenience suggestions — a free-typed value in that shape still works even if it isn't
    offered here (any month could in principle have a managed CWL somewhere)."""
    from discord import app_commands

    now = datetime.now(timezone.utc)
    choices: List[app_commands.Choice[str]] = []
    for i in range(6):
        month_index = now.month - 1 + i
        year = now.year + month_index // 12
        month = month_index % 12 + 1
        value = f"{year}-{month:02d}"
        if not current or current in value:
            choices.append(app_commands.Choice(name=value, value=value))
    return choices[:25]


# ============================================================================
# User Context Menu Commands
# ============================================================================

class _WhoisChartSelectView(discord.ui.View):
    """Ephemeral chart selector shown below the /whois player report embed.

    The user picks a chart from the dropdown; the message is then updated
    in-place with the rendered chart image embedded via attachment://.
    Charts are generated on demand (lazy) to avoid unnecessary work.
    """

    def __init__(
        self,
        war_rows: List[Dict[str, Any]],
        player_name: str,
        tag: str,
        guild_id: Optional[int],
        user_id: Optional[int],
        monthly_star_dist: Optional[List[Dict[str, Any]]] = None,
        cwl_max_rounds: Optional[Dict[str, int]] = None,
    ) -> None:
        super().__init__(timeout=1800)
        self.war_rows = war_rows
        self.player_name = player_name
        self.tag = tag
        self.guild_id = guild_id
        self.user_id = user_id
        self.monthly_star_dist: List[Dict[str, Any]] = monthly_star_dist or []
        self.cwl_max_rounds: Dict[str, int] = cwl_max_rounds or {}
        self.message: Optional[discord.Message] = None
        self.report_message: Optional[discord.Message] = None

        self.select: discord.ui.Select[discord.ui.View] = discord.ui.Select(
            placeholder=t('commands.whois.chart_select_placeholder', guild_id=guild_id),
            options=[
                discord.SelectOption(
                    label=t('commands.whois.chart_option_season', guild_id=guild_id),
                    value="season",
                    emoji="📈",
                    description=t('commands.whois.chart_option_season_desc', guild_id=guild_id),
                ),
                discord.SelectOption(
                    label=t('commands.whois.chart_option_skill', guild_id=guild_id),
                    value="skill",
                    emoji="🎯",
                    description=t('commands.whois.chart_option_skill_desc', guild_id=guild_id),
                ),
                discord.SelectOption(
                    label=t('commands.whois.chart_option_reliability', guild_id=guild_id),
                    value="reliability",
                    emoji="🛡️",
                    description=t('commands.whois.chart_option_reliability_desc', guild_id=guild_id),
                ),
                discord.SelectOption(
                    label=t('commands.whois.chart_option_activity', guild_id=guild_id),
                    value="activity",
                    emoji="⚡",
                    description=t('commands.whois.chart_option_activity_desc', guild_id=guild_id),
                ),
                discord.SelectOption(
                    label=t('commands.whois.chart_option_last10', guild_id=guild_id),
                    value="last10",
                    emoji="📊",
                    description=t('commands.whois.chart_option_last10_desc', guild_id=guild_id),
                ),
            ],
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        mode = interaction.data["values"][0]  # type: ignore[index]

        # Defer immediately so chart generation (which can take several seconds)
        # does not exceed Discord's 3-second response window (error 10062).
        if not await _safe_defer(interaction):
            return

        # Mark selected option as default so the dropdown reflects current choice
        for opt in self.select.options:
            opt.default = (opt.value == mode)

        try:
            from qapbot.chart_player_report import (
                make_season_chart_bytes, make_last10_chart_bytes,
                make_skill_chart_bytes, make_reliability_chart_bytes,
                make_activity_chart_bytes,
            )
            if mode == "season":
                chart_bytes = await asyncio.to_thread(
                    make_season_chart_bytes, self.war_rows, self.player_name, self.tag
                )
                filename = "season_trend.jpg"
            elif mode == "skill":
                chart_bytes = await asyncio.to_thread(
                    make_skill_chart_bytes, self.monthly_star_dist, self.player_name, self.tag
                )
                filename = "skill_trend.jpg"
            elif mode == "reliability":
                chart_bytes = await asyncio.to_thread(
                    make_reliability_chart_bytes, self.war_rows, self.player_name, self.tag
                )
                filename = "reliability_trend.jpg"
            elif mode == "activity":
                chart_bytes = await asyncio.to_thread(
                    make_activity_chart_bytes, self.war_rows, self.player_name, self.tag,
                    self.cwl_max_rounds
                )
                filename = "activity_trend.jpg"
            else:
                chart_bytes = await asyncio.to_thread(
                    make_last10_chart_bytes, self.war_rows, self.player_name, self.tag
                )
                filename = "last10_wars.jpg"

            chart_embed = discord.Embed()
            chart_embed.set_image(url=f"attachment://{filename}")
            await interaction.edit_original_response(
                content="",
                embed=chart_embed,
                attachments=[discord.File(io.BytesIO(chart_bytes), filename)],
                view=self,
            )
        except Exception as _err:
            logging.warning(f"[WHOIS-CHART] Chart generation failed for {self.tag}: {_err}")
            await interaction.edit_original_response(
                content="⚠️ Chart konnte nicht erstellt werden.",
                view=self,
            )

    async def on_timeout(self) -> None:
        channel_id = str(self.message.channel.id) if self.message else None
        for _msg in (self.message, self.report_message):
            if _msg:
                try:
                    await _msg.delete()
                except Exception:
                    pass
        # Remove DB/cache tracking entries without re-attempting Discord deletions
        if channel_id:
            _keys = [
                k for k, v in CACHE.leaderboard_messages.items()
                if v.get("mode") == "whois_player"
                and v.get("clan_tag") == self.tag
                and v.get("channel_id") == channel_id
            ]
            for _k in _keys:
                try:
                    await CACHE.delete_leaderboard_message(_k)
                except Exception as _e:
                    logging.warning(f"[WHOIS-CHART] Cache cleanup failed for {_k}: {_e}")


async def _player_report_logic(
    interaction: discord.Interaction, player_tag: str
) -> None:
    """
    Build and send a war performance report embed for a CoC player tag.
    Queries the local war_attacks / war_summary database (no live CoC API call).
    interaction.response must already be deferred or otherwise consumed by the caller.

    The main embed is sent as a non-ephemeral (public) message.  Error messages
    (no tag, no data) are sent as ephemeral so only the requesting user sees them.
    The chart selector followup is also ephemeral (personal choice).
    """
    tag_clean = player_tag.strip().upper().lstrip("#")
    if not tag_clean:
        await interaction.followup.send(
            t('commands.whois.player_report_no_args', guild_id=interaction.guild_id),
            ephemeral=True,
        )
        return
    tag = f"#{tag_clean}"

    # ── Query DB (sync → thread) ───────────────────────────────────────────────
    war_rows: List[Dict[str, Any]] = await asyncio.to_thread(
        CACHE.db_manager.get_player_war_history_sync, tag  # type: ignore[union-attr]
    )
    _season_clan_pairs = [*{
        (r["cwl_season"], r["clan_tag"])
        for r in war_rows
        if r["is_cwl"] and r["cwl_season"] and r["clan_tag"]
    }]
    _attack_summary, cw_attack_summary, cwl_attack_summary, monthly_star_dist, cwl_max_rounds = await asyncio.gather(
        asyncio.to_thread(CACHE.db_manager.get_player_attack_summary_sync, tag),                        # type: ignore[union-attr]
        asyncio.to_thread(CACHE.db_manager.get_player_attack_summary_sync, tag, False),                 # type: ignore[union-attr]
        asyncio.to_thread(CACHE.db_manager.get_player_attack_summary_sync, tag, True),                  # type: ignore[union-attr]
        asyncio.to_thread(CACHE.db_manager.get_player_monthly_star_dist_sync, tag),                     # type: ignore[union-attr]
        asyncio.to_thread(CACHE.db_manager.get_cwl_max_rounds_sync, _season_clan_pairs),                # type: ignore[union-attr]
    )

    if not war_rows:
        await interaction.followup.send(
            t('commands.whois.player_report_no_data', guild_id=interaction.guild_id).format(tag=tag),
            ephemeral=True,
        )
        return

    # ── Basic identity ─────────────────────────────────────────────────────────
    player_name = war_rows[0]["player_name"]
    th_level    = war_rows[0]["th_level"]  # most-recent TH
    last_war_clan_tag  = war_rows[0]["clan_tag"]
    _last_war_clan_data = CACHE.clan_name_cache.get(last_war_clan_tag, {})
    last_war_clan_name = _last_war_clan_data.get("name") or last_war_clan_tag
    last_war_clan_league = _last_war_clan_data.get("war_league")

    # ── Separate CW and CWL ────────────────────────────────────────────────────
    cw_rows  = [r for r in war_rows if not r["is_cwl"]]
    cwl_rows = [r for r in war_rows if     r["is_cwl"]]

    total_wars = len(war_rows)
    cw_count   = len(cw_rows)
    cwl_count  = len(cwl_rows)

    # ── Overall reliability (from per-war data) ────────────────────────────────
    total_max    = sum(r["max_attacks"] for r in war_rows)
    total_used   = sum(r["attacks"]     for r in war_rows)
    reliability  = round(total_used / total_max * 100) if total_max > 0 else 0

    # ── Win / loss record ─────────────────────────────────────────────────────
    # DB stores result as 'win' / 'loss' / 'draw' (not 'lose').
    # Orphaned war_attacks rows (no war_summary match) have result='' and must
    # be excluded from all counts — otherwise they inflate draws.
    cw_with_result  = [r for r in cw_rows  if r["result"]]
    cw_wins   = sum(1 for r in cw_with_result if r["result"].lower() == "win")
    cw_losses = sum(1 for r in cw_with_result if r["result"].lower() == "loss")
    cw_draws  = len(cw_with_result) - cw_wins - cw_losses
    cw_no_result = cw_count - len(cw_with_result)

    cwl_with_result = [r for r in cwl_rows if r["result"]]
    cwl_wins   = sum(1 for r in cwl_with_result if r["result"].lower() == "win")
    cwl_losses = sum(1 for r in cwl_with_result if r["result"].lower() == "loss")
    cwl_draws  = len(cwl_with_result) - cwl_wins - cwl_losses
    cwl_no_result = cwl_count - len(cwl_with_result)

    def _build_mode_stats(rows: List[Dict[str, Any]], summary: Dict[str, Any]) -> Dict[str, Any]:
        max_attacks = sum(r["max_attacks"] for r in rows)
        used_attacks = sum(r["attacks"] for r in rows)
        missed_attacks = sum(r["missed_attacks"] for r in rows)
        reliability = round(used_attacks / max_attacks * 100) if max_attacks > 0 else 0

        avg_stars = float(summary.get("avg_stars", 0.0) or 0.0)
        avg_dest = float(summary.get("avg_destruction", 0.0) or 0.0)
        dist_attacks = int(summary.get("dist_attacks", 0) or 0)
        zero_star = int(summary.get("zero_star", 0) or 0)
        one_star = int(summary.get("one_star", 0) or 0)
        two_star = int(summary.get("two_star", 0) or 0)
        three_star = int(summary.get("three_star", 0) or 0)

        def pct(n: int) -> str:
            return f"{round(n / dist_attacks * 100)}%" if dist_attacks > 0 else "—"

        recent_rows = rows[:10]
        recent_stars = sum(r["stars"] for r in recent_rows)
        recent_attacks = sum(r["attacks"] for r in recent_rows)
        recent_avg = min(recent_stars / recent_attacks, 3.0) if recent_attacks > 0 else 0.0
        trend_diff = recent_avg - avg_stars
        trend_icon = "📈" if trend_diff > 0.1 else ("📉" if trend_diff < -0.1 else "➡️")

        skill_num = round((three_star - one_star) / dist_attacks * 100) if dist_attacks > 0 else None
        skill_pct_str = f"{skill_num}%" if skill_num is not None else "—"

        return {
            "max_attacks": max_attacks,
            "used_attacks": used_attacks,
            "missed_attacks": missed_attacks,
            "reliability": reliability,
            "avg_stars": avg_stars,
            "avg_dest": avg_dest,
            "zero_star": zero_star,
            "one_star": one_star,
            "two_star": two_star,
            "three_star": three_star,
            "pct": pct,
            "skill_pct_str": skill_pct_str,
            "recent_rows": recent_rows,
            "recent_avg": recent_avg,
            "trend_icon": trend_icon,
        }

    cw_stats = _build_mode_stats(cw_rows, cw_attack_summary)
    cwl_stats = _build_mode_stats(cwl_rows, cwl_attack_summary)

    # ── Activity rating ────────────────────────────────────────────────────────
    _MONTHLY_MAX_CW  = 20
    _first_date_str  = war_rows[-1]["date"] if war_rows and war_rows[-1]["date"] else ""
    _first_month_str = _first_date_str[:7]
    if _first_month_str:
        _fy  = int(_first_month_str[:4])
        _fm  = int(_first_month_str[5:7])
        _fd  = int(_first_date_str[8:10]) if len(_first_date_str) >= 10 else 1
        _now = datetime.today()
        _cy, _cm, _cd = _now.year, _now.month, _now.day
        # Month-by-month accumulation of theoretical CW and CWL maximums:
        # CW:  normalize max by days_available/total_days for incomplete months.
        # CWL: only count seasons where the 10-day window applies
        #       (first month: first_day ≤ 10 ; current month: today.day > 10).
        _max_cw  = 0.0
        _max_cwl = 0.0
        _cur = (_fy, _fm)
        _end = (_cy, _cm)
        while _cur <= _end:
            _y, _m      = _cur
            _total_days = calendar.monthrange(_y, _m)[1]
            _is_first   = (_y == _fy and _m == _fm)
            _is_cur_m   = (_y == _cy and _m == _cm)
            if _is_first and _is_cur_m:
                _days_avail  = _cd - _fd + 1
                _include_cwl = _fd <= 10 and _cd > 10
            elif _is_first:
                _days_avail  = _total_days - _fd + 1
                _include_cwl = _fd <= 10
            elif _is_cur_m:
                _days_avail  = _cd
                _include_cwl = _cd > 10
            else:
                _days_avail  = _total_days
                _include_cwl = True
            _max_cw  += _MONTHLY_MAX_CW * _days_avail / _total_days
            if _include_cwl:
                _max_cwl += cwl_max_rounds.get(f"{_y:04d}-{_m:02d}", 7)
            _cur = (_y + 1, 1) if _m == 12 else (_y, _m + 1)
        _max_possible = _max_cw + _max_cwl
        _total_used   = cw_stats["used_attacks"] + cwl_stats["used_attacks"]
        activity_pct  = min(round(_total_used / _max_possible * 100), 100) if _max_possible > 0 else 0
        _activity_detail = (
            f"{_total_used} / {round(_max_possible)} max attacks"
            f"  •  since {_first_month_str}"
        )
    else:
        activity_pct     = 0
        _activity_detail = "no data"

    # ── Embed color based on reliability ──────────────────────────────────────
    color = 0x2ecc71 if reliability >= 85 else (0xf1c40f if reliability >= 65 else 0xe74c3c)

    th_emoji = getattr(BotEmojis, f"TH{th_level:02d}", f"TH{th_level}")
    cw_record_str = f"**{cw_wins}** Won, **{cw_losses}** Lost, **{cw_draws}** Draw"
    if cw_no_result:
        cw_record_str += f"  *({cw_no_result} no data)*"

    profile_url = coc_player_profile_url(tag_clean)
    clan_tag_clean = last_war_clan_tag.lstrip('#')
    clan_url = coc_clan_profile_url(clan_tag_clean)
    embed = discord.Embed(
        description=(
            f"## Player: {th_emoji} {th_level}  [{player_name}]({profile_url})  ({tag})\n"
            f"**Clan:** **[{last_war_clan_name}]({clan_url})**  ({last_war_clan_tag})"
            + (f"  · {last_war_clan_league}" if last_war_clan_league else "")
        ),
        color=color,
    )

    # Field 1 — Overview
    embed.add_field(
        name="📊 Overview",
        value=(
            f"Wars: **{total_wars}** total  ({cw_count} CW / {cwl_count} CWL)\n"
            f"CW Record: {cw_record_str}\n"
            f"CWL Record: **{cwl_wins}** Won, **{cwl_losses}** Lost, **{cwl_draws}** Draw"
            + (f"  *({cwl_no_result} no data)*" if cwl_no_result else "")
            + f"\nReliability: **{reliability}%** (CW & CWL)\n"
            + f"Activity: **{activity_pct}%**  ({_activity_detail})\n⠀"
        ),
        inline=False,
    )

    # Field 2 — Regular CWs (line-by-line layout)
    _ind = "\u00a0" * 7
    embed.add_field(
        name="🏆 Regular CWs",
        value=(
            f"⚔️ Attacks: Used: **{cw_stats['used_attacks']} / {cw_stats['max_attacks']}**, "
            f"Missed: **{cw_stats['missed_attacks']}**, Reliability: **{cw_stats['reliability']}%**\n"
            f"⭐ Avg Stars/Attack: **{cw_stats['avg_stars']:.2f}**, Dest: **{cw_stats['avg_dest']:.1f}%**\n"
            f"{_ind}3★ {cw_stats['pct'](cw_stats['three_star'])}, "
            f"2★ {cw_stats['pct'](cw_stats['two_star'])}, "
            f"1★ {cw_stats['pct'](cw_stats['one_star'])}, "
            f"0★ {cw_stats['pct'](cw_stats['zero_star'])}, "
            f"Skill: **{cw_stats['skill_pct_str']}**\n"
            f"{_ind}Trend (last 10 CW) avg. {cw_stats['recent_avg']:.2f} {cw_stats['trend_icon']} "
            f"(overall: {cw_stats['avg_stars']:.2f})\n⠀"
        ),
        inline=False,
    )

    # Field 6+ — CWL summary (if any)
    if cwl_count > 0:
        # Count distinct seasons; fall back to date month when cwl_season is empty
        seasons = len({
            r["cwl_season"] if r["cwl_season"] else r["date"][:7]
            for r in cwl_rows
        })
        cwl_dates = sorted(
            r["cwl_season"] if r["cwl_season"] else r["date"][:7]
            for r in cwl_rows
        )
        cwl_range = f"{cwl_dates[0]} – {cwl_dates[-1]}" if cwl_dates else "?"

        embed.add_field(
            name="🏆 CWL wars",
            value=(
                f"**Seasons:** {seasons}  ({cwl_range})\n"
                f"⚔️ Attacks: Used: **{cwl_stats['used_attacks']} / {cwl_stats['max_attacks']}**, "
                f"Missed: **{cwl_stats['missed_attacks']}**, Reliability: **{cwl_stats['reliability']}%**\n"
                f"⭐ Avg Stars/Attack: **{cwl_stats['avg_stars']:.2f}**, Dest: **{cwl_stats['avg_dest']:.1f}%**\n"
                f"{_ind}3★ {cwl_stats['pct'](cwl_stats['three_star'])}, "
                f"2★ {cwl_stats['pct'](cwl_stats['two_star'])}, "
                f"1★ {cwl_stats['pct'](cwl_stats['one_star'])}, "
                f"0★ {cwl_stats['pct'](cwl_stats['zero_star'])}, "
                f"Skill: **{cwl_stats['skill_pct_str']}**\n"
                f"{_ind}Trend (last 10 CWL) avg. {cwl_stats['recent_avg']:.2f} {cwl_stats['trend_icon']} "
                f"(overall: {cwl_stats['avg_stars']:.2f})"
            ),
            inline=False,
        )

    oldest_date = war_rows[-1]["date"][:10] if war_rows[-1]["date"] else "?"
    embed.set_footer(text=f"⠀\nBased on {total_wars} archived wars  •  First recorded: {oldest_date}  •  See charts below")

    # ── Delete any previous whois_player entry for this player in this channel ──
    _ch = interaction.channel
    _ch_id = str(_ch.id) if _ch else None
    if _ch_id:
        from qapbot.QBdiscocmdshelper import _delete_messages_by_filter  # type: ignore[misc,attr-defined]
        await _delete_messages_by_filter(
            _ch_id,
            lambda k, v: (
                v.get("mode") == "whois_player"
                and v.get("clan_tag") == tag
                and v.get("channel_id") == _ch_id
            ),
            f"player {tag} (whois replacement)",
        )

    # Use channel.send() instead of followup.send() so the message is a completely
    # standalone channel message with no interaction reply-reference.
    # followup.send() always links to the interaction's original response (an ephemeral
    # "thinking" indicator or the already-deleted selection message), which causes
    # Discord to show "Nachricht konnte nicht geladen werden" for all other users.
    report_msg: Optional[discord.Message] = None
    if _ch and hasattr(_ch, 'send'):
        report_msg = await _ch.send(embed=embed)  # type: ignore[union-attr]
        # Remove the ephemeral thinking indicator / already-gone selection message
        try:
            await interaction.delete_original_response()
        except (discord.NotFound, discord.HTTPException):
            pass
    else:
        await interaction.followup.send(embed=embed)

    # ── Chart selector (lazy — charts are generated only when the user picks one) ──
    try:
        user_id = interaction.user.id if interaction.user else None
        chart_view = _WhoisChartSelectView(war_rows, player_name, tag, interaction.guild_id, user_id, monthly_star_dist, cwl_max_rounds)
        _chart_content = t(
            'commands.whois.chart_prompt',
            user_id=str(user_id) if user_id else None,
            guild_id=interaction.guild_id,
        ).format(player_name=player_name)
        if _ch and hasattr(_ch, 'send'):
            chart_msg = await _ch.send(content=_chart_content, view=chart_view)  # type: ignore[union-attr]
        else:
            chart_msg = await interaction.followup.send(content=_chart_content, view=chart_view)
        chart_view.message = chart_msg
        chart_view.report_message = report_msg
        # ── Track both messages together as one DB entry ────────────────────────
        if _ch_id:
            from QBhelperfunctions import generate_message_key_timestamp  # type: ignore[misc]
            _msg_ids = ",".join(str(m.id) for m in (report_msg, chart_msg) if m)
            await CACHE.set_leaderboard_message(
                generate_message_key_timestamp(),
                {
                    "clan_tag": tag,       # player_tag stored here; field is non-FK constrained
                    "channel_id": _ch_id,
                    "mode": "whois_player",
                    "message_ids": _msg_ids,
                    "content_hash": "",
                },
            )
    except Exception as _chart_err:
        logging.warning(f"[PLAYER-REPORT] Chart selector setup failed for {tag}: {_chart_err}")


async def _whois_logic(interaction: discord.Interaction, user: Union[discord.User, discord.Member]) -> None:
    """
    Shared logic for the whois context menu commands (user and message variants).

    Displays CoC player accounts registered for *user*, including:
    - User avatar and nickname
    - Registered player accounts with TH level (with icon), hero levels, clan role, and clan name
    - Verification status
    - Primary account indicator
    """
    # Note: interaction.response must already be deferred by the caller.
    
    user_id_str = str(user.id)
    user_entry = CACHE.user_accounts.get(user_id_str)

    # Get guild member for display name and avatar (guild-specific nickname/avatar override) —
    # optional, not required: falls back to the plain Discord username/avatar when unavailable
    # (interaction.guild is None in a DM, or the target isn't a member of that guild).
    member = interaction.guild.get_member(user.id) if interaction.guild else None
    display_name = member.display_name if member else user.name
    avatar_url = (member.display_avatar.url if member else user.display_avatar.url)
    
    if not user_entry or not user_entry.get("players"):
        embed = discord.Embed(
            title=f"{display_name} ({user_id_str})",
            description=f"**Username**\n{user.name}\n\n**Player Accounts (0)**\n*No registered accounts found.*",
            color=0x2B2D31
        )
        embed.set_thumbnail(url=avatar_url)
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    # Build single embed with all player info
    players = user_entry.get("players", [])
    player_count = len(players)
    
    # Create single embed
    embed = discord.Embed(
        title=f"{display_name} ({user_id_str})",
        color=0x2B2D31
    )
    embed.set_thumbnail(url=avatar_url)
    embed.add_field(name="Username", value=user.name, inline=False)
    
    # Build player list — collected as (th_level, text) tuples so we can sort
    # highest TH first before embedding. That way if the embed size cap is hit,
    # the most relevant accounts are shown rather than the most recently added ones.
    player_tuples: List[Tuple[int, int, str]] = []  # (th_level: int, hero_sum: int, text: str)

    # Fetch every account's live data up front, in BOUNDED PARALLEL (2026-08-22 bug report:
    # /whois timing out). This used to be one `await asyncio.wait_for(CACHE.get_player(tag),
    # timeout=8.0)` per account INSIDE the formatting loop below — i.e. fully serial. CACHE.
    # get_player() is uncached (every call is a live API round-trip via coc_retry), so a user with
    # 82 linked accounts issued 82 serialised calls: 30s wall clock live, with individual calls
    # logged at 2.18s and 4.46s because the background clan-poll cycle saturates the API at the
    # same time — which is also what blew the old 8s per-account budget. Mirrors the parallel
    # pattern already used by AccountManagementView._on_refresh_click (ui_registration.py).
    # Semaphore-bounded rather than a bare gather: the CoC rate limiter is shared with the poll
    # cycle, so 82 simultaneous calls would spike it (same bound as the other batch call sites,
    # e.g. QBdiscocmdshelper_admin_command.py's _cleanup_sem).
    valid_players: List[Dict[str, Any]] = [p for p in players if isinstance(p, dict)]
    _whois_sem = asyncio.Semaphore(10)

    async def _fetch_one(tag: str) -> Any:
        async with _whois_sem:
            return await CACHE.get_player(tag)  # type: ignore[arg-type]

    fetch_tags: List[str] = [p.get("player_tag", "") for p in valid_players]  # type: ignore[misc]
    try:
        # ONE budget for the whole batch, not one per account. Generous because it now covers all
        # accounts at once and the API is demonstrably slow under concurrent cycle load.
        fetched: List[Any] = await asyncio.wait_for(
            asyncio.gather(*[_fetch_one(tag) for tag in fetch_tags], return_exceptions=True),
            timeout=45.0,
        )
    except (asyncio.TimeoutError, asyncio.CancelledError):
        # Degrade to the cached per-account data the clan-poll cycle already keeps fresh
        # (player_name/th_level/current_clan_tag) rather than failing the whole command — a
        # degraded row beats an error row, and that data is already in memory.
        logging.warning(
            f"[WHOIS] Live fetch for {len(fetch_tags)} account(s) of {user_id_str} exceeded the "
            "batch timeout — rendering from cached account data"
        )
        fetched = [None] * len(fetch_tags)

    def _cached_row(
        player: Dict[str, Any], player_name: str, player_tag: str, verified: bool, is_primary: bool
    ) -> Tuple[int, int, str]:
        """Render one account from CACHE.user_accounts data when the live fetch didn't land.

        The clan-poll cycle keeps player_name/th_level/current_clan_tag fresh for every tracked
        account (see coc_cache.py's update_player_info_in_user_accounts), so a row built from it
        is genuinely useful — only hero levels are missing. Sorts by its cached TH like any other
        row rather than being dumped at the bottom.
        """
        from qapbot.emojis import BotEmojis

        cached_th = player.get("th_level")
        status_icon = BotEmojis.VERIFIED if verified else BotEmojis.GCHECK
        default_text = " ✓ (Default)" if is_primary else ""
        profile_url = coc_player_profile_url(player_tag)  # type: ignore[union-attr]
        if isinstance(cached_th, int):
            th_emoji = getattr(BotEmojis, f'TH{cached_th:02d}', f'TH{cached_th}')
            head = f"{th_emoji} {cached_th} • "
        else:
            cached_th = 0
            head = ""
        line1 = f"{head}**[{player_name}]({profile_url})** ({player_tag}) {status_icon}{default_text}"
        return (cached_th, 0, f"{line1}\n*Live data unavailable — showing last known info*")

    for player, fetch_result in zip(valid_players, fetched):
        player_tag = player.get("player_tag", "")  # type: ignore[misc]
        player_name = player.get("player_name", "Unknown")  # type: ignore[misc]
        verified = player.get("verified", False)  # type: ignore[misc]
        is_primary = player.get("is_primary", False)  # type: ignore[misc]

        try:
            if isinstance(fetch_result, BaseException):
                raise fetch_result
            player_obj = fetch_result
            if not player_obj:
                # get_player() returned None (fetch failed and was swallowed there), or the batch
                # timeout above degraded every row. Previously `continue` — which silently dropped
                # the account from the output entirely; show what's cached instead.
                player_tuples.append(_cached_row(player, player_name, player_tag, verified, is_primary))
                continue

            # Get TH level
            th_level = player_obj.town_hall
            
            # Get verification status icon (same as clan management)
            from qapbot.emojis import BotEmojis
            if verified:
                status_icon = BotEmojis.VERIFIED
            else:
                status_icon = BotEmojis.GCHECK
            
            # Default indicator
            default_text = " ✓ (Default)" if is_primary else ""
            
            # Line 1: TH-Icon, Player-Name, (#player-tag), Link-status
            profile_url = coc_player_profile_url(player_tag)  # type: ignore[union-attr]
            # Use custom TH emoji instead of text
            th_emoji = getattr(BotEmojis, f'TH{th_level:02d}', f'TH{th_level}')
            line1 = f"{th_emoji} {th_level}\u2003•\u2003**[{player_name}]({profile_url})** ({player_tag}) {status_icon}{default_text}"
            
            # Line 2: heroes only (no league display)
            line2_parts: List[str] = []
            hero_sum: int = 0
            
            # Heroes
            hero_emoji_map = {
                "Barbarian King": BotEmojis.HERO_KING,
                "Archer Queen": BotEmojis.HERO_QUEEN,
                "Grand Warden": BotEmojis.HERO_WARDEN,
                "Royal Champion": BotEmojis.HERO_RC,
                "Minion Prince": BotEmojis.HERO_MP,
                "Dragon Duke": BotEmojis.HERO_DD,
            }
            
            if player_obj.heroes:
                for hero in player_obj.heroes:
                    # Skip builder base heroes
                    if hero.name in ["Battle Machine", "Battle Copter"]:
                        continue
                    hero_icon = hero_emoji_map.get(hero.name, "⚔️")
                    # Log unmapped heroes
                    if hero.name not in hero_emoji_map:
                        logging.warning(f"Unmapped hero found: {hero.name} (level {hero.level})")
                    line2_parts.append(f"{hero_icon} {hero.level}")
                    hero_sum += hero.level
            
            # Use alternating spaces and non-breaking spaces for wider separation
            line2 = " \u00A0 \u00A0 \u00A0 ".join(line2_parts) if line2_parts else "*No league/heroes*"
            
            # Line 3: Leader/Co-Leader/Elder/Member of clan-name
            if player_obj.clan:
                role_name = player_obj.role.name if player_obj.role else "member"  # type: ignore[misc, union-attr]
                
                # Normalize role name and map to display terms
                role_name_lower = role_name.lower() if role_name else "member"  # type: ignore[union-attr]
                role_display = {
                    "member": "Member",
                    "admin": "Elder",
                    "elder": "Elder",
                    "coleader": "Co-Leader",
                    "co-leader": "Co-Leader",
                    "co_leader": "Co-Leader",
                    "leader": "Leader"
                }.get(role_name_lower, "Member")  # type: ignore[arg-type]
                
                line3 = f"{role_display} of {player_obj.clan.name}"
            else:
                line3 = "*No Clan*"
            
            # Combine lines for this player (swap line 2 and 3)
            player_tuples.append((th_level, hero_sum, f"{line1}\n{line3}\n{line2}"))
            
        except (asyncio.TimeoutError, asyncio.CancelledError) as e:
            # Routine under API load, not a defect — WARNING without a traceback (2026-08-22).
            # ERROR + exc_info here meant a user with many accounts could emit one stack trace per
            # account during a busy cycle; that is the log-flooding pattern Pitfall 24 documents.
            logging.warning(f"[WHOIS] Timed out fetching player data for {player_tag}: {e}")
            player_tuples.append(_cached_row(player, player_name, player_tag, verified, is_primary))
        except Exception as e:
            logging.error(f"Error fetching player data for {player_tag}: {e}", exc_info=True)
            # Add minimal error info (TH 0, hero_sum 0 → sorts to bottom)
            player_tuples.append((0, 0, f"❌ **{player_name}** ({player_tag})\n*Could not fetch player data*"))
    
    # Sort highest TH first, then highest hero sum within the same TH level.
    player_tuples.sort(key=lambda x: (x[0], x[1]), reverse=True)
    player_lines: List[str] = [text for _, __, text in player_tuples]

    def _embed_total_len(e: discord.Embed) -> int:
        total = len(e.title or '') + len(e.description or '')
        for f in e.fields:
            total += len(f.name or '') + len(f.value or '')
        return total

    # Discord limits: 6000 chars per embed (and per message total).
    # Safest approach: send each continuation embed as its own separate followup message.
    # We use 950 / 5500 as conservative sub-limits to leave headroom.
    FIELD_VALUE_LIMIT = 950
    EMBED_TOTAL_LIMIT = 5500

    all_embeds: List[discord.Embed] = [embed]
    current_field_lines: List[str] = []
    current_field_len: int = 0
    is_first_field: bool = True

    def _active_embed() -> discord.Embed:
        return all_embeds[-1]

    def _commit_field() -> None:
        nonlocal current_field_lines, current_field_len, is_first_field
        if not current_field_lines:
            return
        value = "\n\n".join(current_field_lines)
        name = f"\n⠀\nPlayer Accounts ({player_count})" if is_first_field else "\u200b"
        # Spill to a new embed if this field would push current embed over the limit
        if _embed_total_len(_active_embed()) + len(name) + len(value) > EMBED_TOTAL_LIMIT:
            all_embeds.append(discord.Embed(color=0x747f8d))
        _active_embed().add_field(name=name, value=value, inline=False)
        is_first_field = False
        current_field_lines = []
        current_field_len = 0

    if player_lines:
        for player_info in player_lines:
            player_len = len(player_info) + 2  # +2 for the "\n\n" separator

            # Speculatively check: would adding this player overflow the current field
            # OR push the embed over the total limit?
            test_value_len = current_field_len + player_len
            field_name_len = len(f"\n⠀\nPlayer Accounts ({player_count})" if is_first_field else "\u200b")
            projected_embed = _embed_total_len(_active_embed()) + field_name_len + test_value_len

            if current_field_lines and (
                test_value_len > FIELD_VALUE_LIMIT or projected_embed > EMBED_TOTAL_LIMIT
            ):
                # Commit current field before adding this player
                _commit_field()

            current_field_lines.append(player_info)
            current_field_len += player_len

        # Commit any remaining players
        _commit_field()
    else:
        embed.add_field(name=f"\n⠀\nPlayer Accounts ({player_count})", value="*No player data available*", inline=False)

    # Send each embed as a separate followup message to avoid the 6000-char
    # per-message total limit when multiple embeds are bundled in one send().
    await interaction.followup.send(embed=all_embeds[0], ephemeral=True)
    for extra_embed in all_embeds[1:]:
        await interaction.followup.send(embed=extra_embed, ephemeral=True)


# DM-invokable (Phase 0b follow-up, CWL_ROSTER_PLANNING_PLAN.md) — correction against the original
# draft: interaction.guild.get_member(user.id) in _whois_logic() turned out to be purely cosmetic
# (guild nickname/avatar override), already with a working display_name/avatar_url fallback for
# member is None — the guard that actually blocked DM use was a separate, unnecessary
# `if not interaction.guild: reject` above that fallback, not a real data dependency. Right-clicking
# a user works the same way in a DM as in a guild (targets the other DM participant).
@app_commands.context_menu(name="whois")
async def whois(interaction: discord.Interaction, user: discord.User):
    """User context menu: right-click a user → Apps → whois."""
    if not await _safe_defer(interaction, thinking=True, ephemeral=True):
        return
    _log_cmd(interaction, "whois", target_user=str(user))
    await _whois_logic(interaction, user)
    _log_cmd_done(interaction, "whois")


# DM-invokable (Phase 0b follow-up, CWL_ROSTER_PLANNING_PLAN.md) — same correction as whois, above.
@app_commands.context_menu(name="whois")
async def whois_message(interaction: discord.Interaction, message: discord.Message):
    """Message context menu: right-click a message → Apps → whois (runs whois on the message author)."""
    if not await _safe_defer(interaction, thinking=True, ephemeral=True):
        return
    _log_cmd(interaction, "whois", message_author=str(message.author))
    await _whois_logic(interaction, message.author)  # type: ignore[arg-type]
    _log_cmd_done(interaction, "whois")


async def _whois_player_select_callback(interaction: discord.Interaction, selected_tag: str) -> None:
    """Callback when user picks a player from the disambiguation dropdown in /whois."""
    if not await _safe_defer(interaction):
        return
    try:
        await interaction.delete_original_response()  # remove the selection dropdown
    except (discord.NotFound, discord.HTTPException):
        pass
    await _player_report_logic(interaction, selected_tag)
    _log_cmd_done(interaction, "whois")


def _build_guild_player_name_matches(guild_id: Optional[int], needle_lower: str) -> List[Dict[str, str]]:
    """Guild-first half of /whois's two-step name search (2026-08-18,
    PLAYER_NAME_INDEX_RETIREMENT_PLAN.md Steps 1-3) — an always-complete, uncapped substring
    match over the guild's OWN player pool, built fresh from the same 3 in-memory sources the
    old post-search reorder step used (CACHE.user_accounts, CACHE.temp_war_stats,
    CACHE.coc_clan_cache for the guild's own clans). A guild's own roster tops out in the
    hundreds, so this runs directly on the event loop — no asyncio.to_thread() needed here,
    unlike the capped global SQL fallback in whois_slash's own caller.

    Guarantees completeness for guild members specifically: unlike the global FTS5 fallback
    (search_player_names_full_sync), a guild member can never be excluded here just because a
    huge global match count pushed them past a cap — the whole point of this redesign. One
    narrow gap: a tag known only via temp_war_stats (not registered, not a current clan member —
    e.g. left mid-war) has no name available from any of these 3 sources and is silently
    skipped, rather than adding a DB round-trip for a name lookup to what's meant to stay a
    fast, in-memory-only pass. Returns [] with no guild context (DM invocation) or when the
    guild has no configured clans; callers then rely entirely on the global fallback, matching
    pre-redesign DM behavior."""
    if not guild_id:
        return []
    from qapbot.QBdiscocmdshelper import get_guild_clans_including_member_config
    guild_clan_tags: set[str] = set(get_guild_clans_including_member_config(guild_id))
    if not guild_clan_tags:
        return []

    tag_to_clan: Dict[str, str] = {}
    tag_to_name: Dict[str, str] = {}
    # Source 1: registered Discord users (current_clan_tag + player_name fields)
    for user_data in CACHE.user_accounts.values():
        for p in user_data.get("players", []):
            if isinstance(p, dict):
                ptag = p.get("player_tag", "")
                if not ptag:
                    continue
                ctag = p.get("current_clan_tag") or ""
                if ctag:
                    tag_to_clan[ptag] = ctag
                pname = p.get("player_name")
                if pname:
                    tag_to_name.setdefault(ptag, pname)
    # Source 2: all players currently in active wars (covers unregistered members) — only keys
    # carry a tag->clan association here, no name. A tag found ONLY here (not source 1/3) has
    # no name to match against and is silently skipped below — narrow edge case (e.g. a player
    # who left their clan mid-war, so no longer in coc_clan_cache), accepted rather than adding
    # a DB round-trip (and the asyncio.to_thread() wrapping it would require) to what's meant to
    # stay a small, always-in-memory, event-loop-safe pass (2026-08-18, Step 5 — this fallback
    # used to resolve via CACHE.player_name_index, retired in the same change).
    for clan_tag, player_stats in CACHE.temp_war_stats.items():
        for player_tag in player_stats:
            tag_to_clan[player_tag] = clan_tag
    # Source 3: cached clan members for guild clans (covers between-wars) — freshest source,
    # overwrites any stale name source 1 might hold.
    for clan_tag in guild_clan_tags:
        _clan_obj = CACHE.coc_clan_cache.cache.get(clan_tag, {}).get("data")
        if _clan_obj is not None:
            for _member in getattr(_clan_obj, 'members', []):
                _ptag = getattr(_member, 'tag', '')
                if _ptag:
                    tag_to_clan[_ptag] = clan_tag
                    _mname = getattr(_member, 'name', '')
                    if _mname:
                        tag_to_name[_ptag] = _mname

    matches: List[Dict[str, str]] = []
    for ptag, ctag in tag_to_clan.items():
        if ctag not in guild_clan_tags:
            continue
        name = tag_to_name.get(ptag)
        if not name or needle_lower not in name.lower():
            continue
        matches.append({"player_tag": ptag, "player_name": name})
    matches.sort(key=lambda m: m["player_name"].lower())
    return matches


@app_commands.command(name="whois", description=dev_mode+"Show CoC accounts for a Discord user, or war history for a player.")
@app_commands.describe(
    user="The Discord user to look up",
    player="Player tag (e.g. #ABC123) or name substring to search",
)
# DM-invokable (Phase 0b follow-up, CWL_ROSTER_PLANNING_PLAN.md) — correction against the original
# draft: _whois_logic()'s guild dependency was cosmetic-only (see whois/whois_message, above), and
# `user` is now typed discord.User instead of discord.Member — Member-typed options can only
# resolve against a guild's member list, which doesn't exist in a DM; User-typed options resolve
# to any Discord user regardless of context. _whois_logic() already accepted
# Union[discord.User, discord.Member], so no change needed on that side. The player= path
# (_player_report_logic) was already guild-agnostic.
async def whois_slash(
    interaction: discord.Interaction,
    user: Optional[discord.User] = None,
    player: Optional[str] = None,
) -> None:
    """Slash command: /whois — look up a Discord user's CoC accounts, or a player's war history."""
    if not await _safe_defer(interaction, thinking=True, ephemeral=True):
        return
    _log_cmd(interaction, "whois", user=str(user) if user else None, player=player)
    if player:
        player_stripped = player.strip()
        if player_stripped.startswith('#'):
            # Explicit tag — pass directly
            await _player_report_logic(interaction, player_stripped)
            _log_cmd_done(interaction, "whois")
        else:
            # Two-step search (2026-08-18, PLAYER_NAME_INDEX_RETIREMENT_PLAN.md Steps 1-3):
            # guild members first, via an always-complete in-memory pass over the guild's own
            # player pool (never subject to any cap — see _build_guild_player_name_matches),
            # then a capped global FTS5 fallback for everyone else. The guild pass IS the
            # priority ordering now, structurally (it's collected and sorted first), replacing
            # the old single uncapped scan over CACHE.player_name_index followed by a separate
            # sort-by-tag_to_clan reorder step.
            needle = player_stripped.lower()
            guild_matches = _build_guild_player_name_matches(interaction.guild_id, needle)
            guild_tags_matched = {m["player_tag"] for m in guild_matches}

            # Global fallback only above the FTS5 trigram floor (3 chars) — below that, the
            # guild pass above (no floor of its own) may already have answered; skip silently
            # rather than issuing a query guaranteed to find nothing (Pitfall 26 doesn't apply
            # to skipping a call, only to making one unwrapped).
            global_matches: List[Dict[str, str]] = []
            if len(needle) >= 3 and CACHE.db_manager is not None:
                global_raw: List[Dict[str, str]] = await asyncio.to_thread(
                    CACHE.db_manager.search_player_names_full_sync, needle
                )
                global_matches = [m for m in global_raw if m["player_tag"] not in guild_tags_matched]

            all_matches = guild_matches + global_matches
            matches = all_matches[:25]
            if not matches:
                # "Too short" only when the guild pass (which has no length floor of its own)
                # also found nothing — a guild admin searching "Al" for their own 3-member
                # roster should get a working result, not an error.
                if len(needle) < 3:
                    await interaction.followup.send(
                        t('commands.whois.player_search_too_short', guild_id=interaction.guild_id).format(name=player_stripped),
                        ephemeral=True,
                    )
                else:
                    await interaction.followup.send(
                        t('commands.whois.player_not_found', guild_id=interaction.guild_id).format(name=player_stripped),
                        ephemeral=True,
                    )
            elif len(matches) == 1:
                await _player_report_logic(interaction, matches[0]["player_tag"])
                _log_cmd_done(interaction, "whois")
            else:
                # Ambiguous — let user pick from a dropdown
                from qapbot.ui_common import GenericSelectView
                options = [
                    discord.SelectOption(
                        label=m["player_name"][:100],
                        value=m["player_tag"],
                        description=m["player_tag"],
                    )
                    for m in matches
                ]
                view = GenericSelectView(
                    options=options,
                    callback_fn=_whois_player_select_callback,
                    placeholder=t('commands.whois.player_select_placeholder', guild_id=interaction.guild_id),
                )
                msg = t('commands.whois.player_select_prompt', guild_id=interaction.guild_id).format(name=player_stripped)
                if len(all_matches) > 25:
                    msg = f"{msg}\n{t('commands.whois.player_select_too_many', guild_id=interaction.guild_id)}"
                view.message = await interaction.followup.send(msg, view=view, ephemeral=True)
    elif user:
        await _whois_logic(interaction, user)
        _log_cmd_done(interaction, "whois")
    else:
        await interaction.followup.send(
            t('commands.whois.player_report_no_args', guild_id=interaction.guild_id),
            ephemeral=True,
        )


# ============================================================================
# All UI components and helper functions have been moved to:
# - Split UI modules: ui_common.py (GenericSelectView), ui_registration.py (RegistrationView, PlayerSubstringModal)
# - qapbot/QBdiscocmdshelper.py (get_player_list, process_player_registration)
# ============================================================================
# NOTE: All administrative actions are now integrated into the single /admin command.
# See the admin() function definition for all available actions and handlers.
