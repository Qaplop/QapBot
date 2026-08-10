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


def _resolve_template_season_for_event(current_season: str) -> Optional[str]:
    """The season Start Enrollment copies as a template: the calendar month immediately before
    current_season ("YYYY-MM"). CWL is played every month regardless of whether a cwl_events
    planning row exists for it — war_summary/war_attacks are populated by the bot's regular war-
    tracking cycle, independent of this feature — so "last calendar month" is the right template
    source, not "this guild's most recent prior cwl_events row" (which could be many months
    stale, or simply never used the bot for planning before)."""
    try:
        year_str, month_str = current_season.split("-")
        year, month = int(year_str), int(month_str)
    except (ValueError, AttributeError):
        return None
    if month == 1:
        return f"{year - 1}-12"
    return f"{year}-{month - 1:02d}"


async def start_cwl_enrollment(guild_id: int, season: str) -> Dict[str, Any]:
    """The single "Start Enrollment" admin action (CWL_ROSTER_PLANNING_PLAN.md Phase 2): seeds
    cwl_signups from last season's roster, sends the confirm/opt-out DM blast to every resolved
    account, and transitions the event draft -> signup_open. Re-fetches the event fresh by
    guild_id+season rather than trusting a caller-held event dict/id, matching the re-read
    discipline used everywhere else in this feature for actions gated behind a confirmation step.

    Returns a summary dict the caller renders back to the admin: ok, error (reason string if not
    ok), seeded (signup rows created), contacted (DMs actually sent), skipped_optout,
    skipped_unlinked, skipped_dev_guard.

    DEV-mode safety (operational directive, CWL_ROSTER_PLANNING_PLAN.md, 2026-08-10): while
    CONFIG.is_dev_mode is True, only CONFIG.server_admin's own Discord account is actually
    DMed — every other resolved recipient is counted in skipped_dev_guard instead of contacted.
    This lets the whole flow be exercised live in DEV without risking a DM blast to real clan
    members while the feature is still being built. PROD (CONFIG.is_dev_mode is False) is
    unaffected — the guard never activates there.
    """
    from qapbot.config import CONFIG

    summary: Dict[str, Any] = {
        "ok": False, "error": None, "seeded": 0, "contacted": 0,
        "skipped_optout": 0, "skipped_unlinked": 0, "skipped_dev_guard": 0,
    }

    db = CACHE.db_manager
    if db is None:
        summary["error"] = "no_database"
        return summary

    guild_id_str = str(guild_id)
    event = db.get_cwl_event_sync(guild_id_str, season)
    if event is None:
        summary["error"] = "no_event"
        return summary
    if event["status"] != "draft":
        summary["error"] = "not_draft"
        return summary

    all_clans = db.get_cwl_event_clans_sync(event["id"])
    participating_clan_tags = [c["clan_tag"] for c in all_clans if c.get("participating", 1)]
    if not participating_clan_tags:
        summary["error"] = "no_clans"
        return summary

    template_season = _resolve_template_season_for_event(event["cwl_season"])
    participants = (
        db.get_previous_cwl_participants_sync(participating_clan_tags, template_season)
        if template_season else []
    )

    signups_to_create: List[Dict[str, Any]] = []
    dm_targets: List[Dict[str, Any]] = []
    for participant in participants:
        if participant["cwl_permanent_optout"]:
            summary["skipped_optout"] += 1
            continue
        signups_to_create.append({
            "player_tag": participant["player_tag"],
            "player_name": participant["player_name"],
            "discord_id": participant["discord_id"],
            "preferred_league_rank": participant["preferred_league_rank"],
            "source": "template_confirm",
            "status": "pending",
        })
        if participant["discord_id"]:
            dm_targets.append(participant)
        else:
            summary["skipped_unlinked"] += 1

    if signups_to_create:
        db.bulk_create_cwl_signups_sync(event["id"], signups_to_create)
        summary["seeded"] = len(signups_to_create)

    for participant in dm_targets:
        if CONFIG.is_dev_mode and str(participant["discord_id"]) != CONFIG.server_admin:
            summary["skipped_dev_guard"] += 1
            continue
        sent = await _send_cwl_signup_template_dm(event["id"], guild_id, participant)
        if sent:
            summary["contacted"] += 1

    db.update_cwl_event_status_sync(event["id"], "signup_open")
    summary["ok"] = True
    return summary


async def _send_cwl_signup_template_dm(event_id: int, guild_id: int, participant: Dict[str, Any]) -> bool:
    """Send one template-copy confirm/opt-out DM. Kept as its own function so start_cwl_enrollment
    stays readable — this is the only place that builds the DM's content+view pair."""
    from qapbot.i18n import t
    from qapbot.ui_cwl_roster import build_cwl_signup_response_view

    discord_id = participant["discord_id"]
    message = t(
        'cwl.template.dm_body',
        guild_id=guild_id,
        user_id=discord_id,
        player_name=participant["player_name"] or participant["player_tag"],
    )
    view = build_cwl_signup_response_view(event_id, participant["player_tag"])
    return await CACHE.send_user_dm(str(discord_id), message, view=view)
