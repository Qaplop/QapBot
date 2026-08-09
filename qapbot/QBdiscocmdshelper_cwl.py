"""
Command-logic helpers and shared embed-builder functions for the CWL roster planning feature.

Split out of QBdiscocmdshelper.py the same way QBdiscocmdshelper_admin_command.py was — keeps
an already-large file from growing further with an unrelated concern. See
CWL_ROSTER_PLANNING_PLAN.md for the full feature design.

The two embed builders here (format_clan_management_cwl_settings /
format_clan_management_cwl_management) are the "shared content layer": they're called both
from format_clan_management_message()'s dispatch (the /clan management path) and directly from
the CWL Management Hub view's own render step (the anchored-message path) — written once,
shown in two places, per the plan's Phase 1 design.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import discord

from qapbot.cache_manager import CACHE
from qapbot.constants import CWL_LEAGUE_ORDER


def cwl_league_rank(tier: Optional[str]) -> int:
    """Numeric rank for sorting clans by CWL tier, highest league first (CWL_CLAN_CONFIG_ACTIVITY_PLAN.md
    Phase E: both the CWL Management embed and the web Activity's clan-config table sort this
    way now). Unknown/never-synced tiers rank below Bronze League III (-1), so they sort last."""
    if not tier:
        return -1
    try:
        return CWL_LEAGUE_ORDER.index(tier)
    except ValueError:
        return -1


def cwl_start_at_discord_timestamp(cwl_start_at: Optional[str], style: str = "f") -> Optional[str]:
    """Convert a stored UTC "YYYY-MM-DDTHH:MMZ" cwl_start_at into Discord's `<t:unix:style>`
    timestamp markup, or None if unset/unparseable.

    Discord's client renders this markup in *each viewer's own* locale/timezone automatically —
    no per-guild timezone setting is needed to satisfy "show it in the calling client's
    timezone" (CWL_CLAN_CONFIG_ACTIVITY_PLAN.md Phase E.4); this is the native mechanism built
    for exactly that, and it's more correct than a single guild-wide setting could ever be
    (each admin sees their own local time, not the guild's).
    """
    if not cwl_start_at:
        return None
    try:
        naive = datetime.strptime(cwl_start_at.rstrip("Z"), "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    epoch = int(naive.replace(tzinfo=timezone.utc).timestamp())
    return f"<t:{epoch}:{style}>"


def resolve_selected_cwl_season(guild_id: int) -> str:
    """The season currently selected for CWL Management display/editing — the persisted
    guild_config.cwl_selected_season if set (driven by the season dropdown on the CWL
    Management screen, CWL_CLAN_CONFIG_ACTIVITY_PLAN.md Phase E.3), else falls back to
    get_current_cwl_event_sync()'s season, else the calendar default. Single resolution path
    shared by the Discord-side embed/season-select and the web bridge, so both always agree on
    "which season is this" without either needing its own heuristic.
    """
    guild_id_str = str(guild_id)
    selected = CACHE.server_config.get(guild_id_str, {}).get("cwl_selected_season")
    if selected:
        return selected
    event = get_current_cwl_event_sync(guild_id)
    return event["cwl_season"] if event else resolve_current_cwl_season()


def resolve_current_cwl_season() -> str:
    """Default cwl_season key for a newly-created event: the *upcoming* calendar month (UTC).

    CWL itself runs in roughly the first ten days of each month, so a given month's war league
    is already locked in (or actively running) before roster planning for it could happen —
    whenever leadership is setting up a roster, they are always preparing for next month's
    season, never the current one. There is no CoC-API-derived season key
    (normalize_cwl_season() in qapbot/constants.py) available yet to normalize against at setup
    time — this is a simple calendar default, not an API-observed value.
    """
    now = datetime.now(timezone.utc)
    if now.month == 12:
        return f"{now.year + 1}-01"
    return f"{now.year}-{now.month + 1:02d}"


def get_current_cwl_event_sync(guild_id: int) -> Optional[Dict[str, Any]]:
    """Return the guild's "current" CWL event for the cwl_management screen to show.

    "Current" means: the most recent non-terminal event (draft/signup_open/finalized) if one
    exists, else the single most recent event overall (announced/cancelled — still worth
    showing so leadership can see what was last sent out), else None if the guild has never
    created a CWL event at all.
    """
    db = CACHE.db_manager
    if db is None:
        return None
    events = db.list_cwl_events_sync(str(guild_id))
    if not events:
        return None
    for status in ("draft", "signup_open", "finalized"):
        for event in events:
            if event["status"] == status:
                return event
    return events[0]  # list_cwl_events_sync already orders by cwl_season DESC


async def format_clan_management_cwl_settings(
    guild: discord.Guild,
) -> Tuple[discord.Embed, Optional[discord.Embed], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """CWL guild-preferences screen ("cwl_settings" mode) — shared by /clan management and the
    CWL Management Hub. Modeled on _format_clan_management_config()'s block-per-setting style
    (QBdiscocmdshelper.py). Secondary embed and player lists are always empty here, matching
    every other non-registrations mode.
    """
    from qapbot.i18n import t

    guild_id_int = guild.id
    guild_id_str = str(guild_id_int)
    guild_config = CACHE.server_config.get(guild_id_str, {})

    management_channel_id = guild_config.get("cwl_management_channel_id")
    management_enabled = guild_config.get("cwl_management_message_enabled", False)
    retention_months = guild_config.get("cwl_retention_months", 0)

    if management_channel_id:
        try:
            channel = guild.get_channel(int(management_channel_id))
            channel_display = f"<#{management_channel_id}>" if channel else f"❌ {t('cwl.settings.channel_not_set', guild_id=guild_id_int)}"
        except Exception:
            channel_display = f"❌ {t('cwl.settings.channel_not_set', guild_id=guild_id_int)}"
    else:
        channel_display = f"❌ {t('cwl.settings.channel_not_set', guild_id=guild_id_int)}"

    status_emoji = "🟢" if management_enabled else "🔴"
    status_text = (
        t('cwl.settings.status_enabled', guild_id=guild_id_int)
        if management_enabled
        else t('cwl.settings.status_disabled', guild_id=guild_id_int)
    )
    hub_block = (
        f"⠀\n**{t('cwl.settings.hub_block_title', guild_id=guild_id_int)}**\n"
        f"{t('cwl.settings.hub_status', guild_id=guild_id_int, status=f'{status_emoji} {status_text}')}\n"
        f"{t('cwl.settings.hub_channel', guild_id=guild_id_int, channel=channel_display)}"
    )

    retention_display = (
        t('cwl.settings.retention_never', guild_id=guild_id_int)
        if not retention_months
        else t('cwl.settings.retention_months', guild_id=guild_id_int, months=retention_months)
    )
    retention_block = (
        f"⠀\n**{t('cwl.settings.retention_block_title', guild_id=guild_id_int)}**\n"
        f"{t('cwl.settings.retention_value', guild_id=guild_id_int, value=retention_display)}"
    )

    embed = discord.Embed(
        title=t('cwl.settings.title', guild_id=guild_id_int),
        description=t('cwl.settings.description', guild_id=guild_id_int, guild_name=guild.name),
        color=discord.Color.gold(),
    )
    embed.add_field(name="", value=hub_block, inline=False)
    embed.add_field(name="", value=retention_block, inline=False)

    return embed, None, [], []


async def format_clan_management_cwl_management(
    guild: discord.Guild,
) -> Tuple[discord.Embed, Optional[discord.Embed], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """CWL season-operations screen ("cwl_management" mode) — shared by /clan management and
    the CWL Management Hub. A snapshot rendered on open/refresh, not auto-live-updating — same
    convention as the existing registrations embed (QBdiscocmdshelper.py).
    """
    from qapbot.i18n import t

    guild_id_int = guild.id
    season = resolve_selected_cwl_season(guild_id_int)
    db = CACHE.db_manager
    event = db.get_cwl_event_sync(str(guild_id_int), season) if db is not None else None

    embed = discord.Embed(
        title=t('cwl.management.title', guild_id=guild_id_int),
        color=discord.Color.gold(),
    )

    if event is None:
        embed.description = t('cwl.management.no_event', guild_id=guild_id_int, season=season)
        return embed, None, [], []

    # get_cwl_event_clans_sync() returns every clan ever configured for this event, including
    # deactivated ones whose settings are kept (not deleted) so reactivating restores them — the
    # "Participating Clans" display must only show the ones actually currently participating.
    all_clans = db.get_cwl_event_clans_sync(event["id"]) if db is not None else []
    clans = [c for c in all_clans if c.get("participating", 1)]

    def _tier_for(clan: Dict[str, Any]) -> Optional[str]:
        # The CWL tier is CoC-defined (war_league), never admin-set — prefer the live value
        # over the stored snapshot in case the clan was promoted/demoted since.
        return CACHE.get_clan_war_league(clan["clan_tag"]) or clan.get("target_league_rank")

    # Highest tier first (CWL_CLAN_CONFIG_ACTIVITY_PLAN.md Phase E), name as the tiebreaker.
    clans = sorted(
        clans,
        key=lambda c: (-cwl_league_rank(_tier_for(c)), (CACHE.get_clan_name(c["clan_tag"], c["clan_tag"]) or "").lower()),
    )

    embed.description = t(
        'cwl.management.season_header',
        guild_id=guild_id_int,
        season=event["cwl_season"],
        status=t(f'cwl.management.event_status_{event["status"]}', guild_id=guild_id_int),
    )

    if not clans:
        clans_block = t('cwl.management.no_clans_configured', guild_id=guild_id_int)
    else:
        lines = []
        for clan in clans:
            clan_name = CACHE.get_clan_name(clan["clan_tag"], clan["clan_tag"])
            tier = _tier_for(clan) or t('cwl.management.tier_unset', guild_id=guild_id_int)
            # Discord renders <t:unix:f> in each viewer's own local timezone automatically —
            # see cwl_start_at_discord_timestamp()'s docstring.
            start_display = (
                cwl_start_at_discord_timestamp(clan.get("cwl_start_at"))
                or t('cwl.management.start_time_unset', guild_id=guild_id_int)
            )
            lines.append(
                f"• **{clan_name}** ({clan['clan_tag']}) — {tier}, "
                f"{clan['roster_size']} {t('cwl.management.roster_slots', guild_id=guild_id_int)}, "
                f"{t('cwl.management.start_label', guild_id=guild_id_int)}: {start_display}"
            )
        clans_block = "\n".join(lines)
    embed.add_field(
        name=t('cwl.management.clans_block_title', guild_id=guild_id_int),
        value=clans_block,
        inline=False,
    )

    signup_counts = db.get_cwl_signup_status_counts_sync(event["id"]) if db is not None else {}
    signup_lines = [
        f"{t(f'cwl.management.signup_status_{status}', guild_id=guild_id_int)}: {signup_counts.get(status, 0)}"
        for status in ("pending", "confirmed", "declined", "withdrawn")
    ]
    embed.add_field(
        name=t('cwl.management.signups_block_title', guild_id=guild_id_int),
        value="\n".join(signup_lines),
        inline=False,
    )

    return embed, None, [], []
