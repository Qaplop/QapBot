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

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import discord

from qapbot.cache_manager import CACHE
from qapbot.constants import CWL_LEAGUE_ORDER

# Serializes start_cwl_enrollment() per (guild_id, season) — 2026-08-21 hardening, same bug class
# and same fix shape as CoCClanCache._update_locks (COPILOT_PITFALLS_COOKBOOK.md Pitfall 35).
# The only server-side guard was `event["status"] != "draft"`, and that status isn't written until
# AFTER the whole DM batch finishes — for a ~120-recipient blast under load that window is minutes
# long, during which a second trigger (another admin, another device, a freshly reopened confirm
# dialog) sails straight past the check and starts a duplicate run. The UI's button-disable only
# protects the one already-rendered message, not a new one.
_enrollment_locks: Dict[Tuple[str, str], asyncio.Lock] = {}


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


def _league_weight(tier: Optional[str], growth_rate: float = 1.4) -> float:
    """Weight for one CWL attack's league tier, feeding the "Manage Enrollment" board's
    league-adjusted player-skill score (live-testing feedback, 2026-08-14; formula/growth_rate
    confirmed with the project owner). Exponential across the 8 league *groups* (Bronze..Legend)
    rather than linear across all 22 fine-grained tiers, since skill differentiation compounds
    going up leagues — Champion beating Master should count for meaningfully more than Silver
    beating Bronze does, not the same flat bonus either way. A small +3%/step bonus for I > II >
    III within a group keeps sub-tier ordering meaningful without letting it dominate the
    group-to-group jump. `growth_rate=1.4` means Legend-league stars count ~10.5x Bronze-league
    stars, and each single group step (e.g. Master -> Champion) is worth 1.4x on its own.

    CWL_LEAGUE_ORDER is laid out in perfect groups of 3 (Bronze III, Bronze II, Bronze I, Silver
    III, ...) with "Legend League" as a lone 22nd entry — so `index // 3` is the group (0=Bronze
    .. 6=Titan, 7=Legend) and `index % 3` is the sub-tier bonus (0=III, 1=II, 2=I; always 0 for
    Legend, which has no sub-tier) with no string parsing needed.

    Unknown/unresolvable tier -> baseline weight 1.0 (never zero/negative — a player whose
    league can't be resolved still gets counted, just unweighted, rather than zeroed out)."""
    index = cwl_league_rank(tier)
    if index < 0:
        return 1.0
    group_index = index // 3
    subtier_bonus = index % 3
    return (growth_rate ** group_index) * (1 + 0.03 * subtier_bonus)


def _cwl_stats_window_since_date(num_months: int = 3, now: Optional[datetime] = None) -> str:
    """First day of the earliest of the trailing `num_months` calendar months (inclusive of the
    current month), as a "YYYY-MM-DD" SQL lower bound — the exact same window boundary
    get_recent_cwl_player_stats (QBhelperfunctions.py) computes via parse_month_argument for the
    hover pop-up's own stats, duplicated here in single-date-string form since a bulk per-roster
    query needs one SQL `>=` boundary rather than a per-month breakdown (2026-08-16, project
    owner's spec: "use the 'last three months' logic for both" the pop-up and the player tiles).
    `now` overridable for deterministic tests, same reason parse_month_argument() itself takes
    `now` explicitly rather than calling datetime.now() internally."""
    from QBhelperfunctions import parse_month_argument

    month, year = parse_month_argument(f"-{num_months}", now or datetime.now(timezone.utc))[0]
    return f"{year:04d}-{month:02d}-01"


def compute_league_adjusted_skill_scores(player_tags: List[str], *, now: Optional[datetime] = None) -> Dict[str, float]:
    """Player-skill score for the "Manage Enrollment" board's TH/Skill/Alphabetical sort option:
    league-adjusted average stars/attack over each player's CWL attacks in the trailing 3
    calendar months (2026-08-16, project owner's spec — previously the player's last 10 CWL
    attacks; changed for consistency with the hover pop-up's own "last 3 months" stats, see
    _cwl_stats_window_since_date's docstring). Each attack's raw stars is weighted by
    _league_weight() above (the league its attacking clan was in for that CWL round) before
    averaging — a 3-star earned in Champion League counts for meaningfully more than a 3-star in
    Master League. Rounded to 2dp.

    A player_tag with no resolvable CWL-attack-with-league data in the window (never played CWL,
    played outside the window, or league data isn't populated for those seasons —
    get_recent_cwl_attacks_with_league_sync's own docstring covers why that can happen) is simply
    absent from the returned dict — never a fabricated 0, matching the same "None means no data,
    not zero" convention compute_roster_stats_sync (QBhelperfunctions.py) already uses."""
    db = CACHE.db_manager
    if db is None or not player_tags:
        return {}
    since_date = _cwl_stats_window_since_date(now=now)
    attacks_by_tag = db.get_recent_cwl_attacks_with_league_sync(player_tags, since_date=since_date)
    scores: Dict[str, float] = {}
    for tag, attacks in attacks_by_tag.items():
        if not attacks:
            continue
        weighted_total = sum(a["stars"] * _league_weight(a["league_rank"]) for a in attacks)
        scores[tag] = round(weighted_total / len(attacks), 2)
    return scores


def compute_avg_stars_per_attack(player_tags: List[str], *, now: Optional[datetime] = None) -> Dict[str, float]:
    """The "Manage Enrollment" board's other number-display option (2026-08-14, project owner's
    spec: a second radio group lets the admin pick which number shows next to each player's
    name — this, or the league-adjusted skill score above; defaults to this one). Plain,
    unweighted average stars/attack over each player's CWL attacks in the trailing 3 calendar
    months — the exact same window compute_league_adjusted_skill_scores() uses, just without the
    league weighting. Same "no data -> absent from the dict, never a fabricated 0" convention."""
    db = CACHE.db_manager
    if db is None or not player_tags:
        return {}
    since_date = _cwl_stats_window_since_date(now=now)
    attacks_by_tag = db.get_recent_cwl_attacks_with_league_sync(player_tags, since_date=since_date)
    averages: Dict[str, float] = {}
    for tag, attacks in attacks_by_tag.items():
        if not attacks:
            continue
        averages[tag] = round(sum(a["stars"] for a in attacks) / len(attacks), 2)
    return averages


def cwl_start_at_compact(cwl_start_at: Optional[str], timezone_name: str = "UTC") -> Optional[str]:
    """Compact fixed "YY-MM-DD HH:MM" rendering of a stored UTC cwl_start_at, converted into
    timezone_name (the guild's configured timezone_name, an IANA zone id like "Europe/Berlin")
    via the stdlib zoneinfo module — correctly DST-aware, unlike a fixed offset. Falls back to
    plain UTC if timezone_name is unset/unrecognized rather than raising. Used by the CWL
    Management embed's monospaced clan table (format_clan_management_cwl_management()), where a
    code-block table needs one consistent column width/format for every row — incompatible with
    Discord's native per-viewer <t:unix:style> markup (cwl_start_at_discord_timestamp() below),
    which Discord doesn't even parse inside a code block in the first place, and whose rendered
    format Discord controls (no style matches "YY-MM-DD HH:MM"). Since a single shared table
    can't show each viewer their own local time, it shows the guild's one configured zone
    instead — a per-guild setting (see the "Select Timezone" button next to "Select Language" in
    Basic Config) is a deliberate exception to the CWL_CLAN_CONFIG_ACTIVITY_PLAN.md item 9
    "never a guild-wide timezone setting" decision, made necessary specifically by the
    code-block incompatibility above — everywhere else in the codebase still prefers native
    per-viewer markup."""
    if not cwl_start_at:
        return None
    try:
        naive = datetime.strptime(cwl_start_at.rstrip("Z"), "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    aware_utc = naive.replace(tzinfo=timezone.utc)
    try:
        local = aware_utc.astimezone(ZoneInfo(timezone_name))
    except Exception:
        local = aware_utc
    return local.strftime("%y-%m-%d %H:%M")


def timezone_abbreviation(timezone_name: str, cwl_season: str) -> str:
    """Short abbreviation ("CEST", "UTC", "IST") for timezone_name, used in the CWL Management
    table's "CWL Start (CEST)" header — the full IANA zone name ("Europe/Berlin") reliably wraps
    the header onto a second line inside Discord's code-block width, breaking the table's column
    alignment. Resolved against cwl_season's own official start (1st of that month, 08:00 UTC)
    rather than "now", since the abbreviation should reflect the season being displayed, not
    whatever DST state happens to be current when the embed is rendered. A season's actual
    per-clan start times are all within a 48h window of that reference point (the Activity's own
    picker enforces this), so in practice every row shares this same abbreviation; on the rare
    case a table straddles a DST transition, only the header label is approximate — each row's
    own HH:MM (cwl_start_at_compact() above) is always individually correct regardless."""
    try:
        reference = datetime.strptime(f"{cwl_season}-01T08:00", "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
        return reference.astimezone(ZoneInfo(timezone_name)).tzname() or timezone_name
    except Exception:
        return timezone_name


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


def find_active_cwl_participation(
    guild_id: str, clan_tags: Iterable[str]
) -> Dict[str, List[Tuple[int, str]]]:
    """For each clan_tag in clan_tags, return the (event_id, cwl_season) pairs — of this guild's
    non-cancelled cwl_events — where it's currently marked participating. The safety check behind
    MemberClansConfigurationView's "you're about to remove a clan that's still in an active CWL
    lineup" confirmation (ui_clan_management.py): the event_id lets the confirm view deactivate
    the clan there too (deactivate_cwl_event_clan_sync) without a second lookup, and the season
    string is what gets shown to the admin. A clan absent from the returned dict has no
    active-CWL entanglement and can be removed from the guild without touching cwl_event_clans at
    all. "Non-cancelled" deliberately includes announced/finalized events too, not just
    draft/signup_open — an admin removing a clan after assignments were made or DMs were sent
    still needs to know, even though nothing downstream currently acts on 'finalized'/'announced'
    (those phases aren't built yet)."""
    db = CACHE.db_manager
    if db is None or not clan_tags:
        return {}
    clan_tag_set = set(clan_tags)
    conflicts: Dict[str, List[Tuple[int, str]]] = {}
    for event in db.list_cwl_events_sync(str(guild_id)):
        if event["status"] == "cancelled":
            continue
        for clan in db.get_cwl_event_clans_sync(event["id"]):
            if clan["clan_tag"] in clan_tag_set and clan.get("participating", 1):
                conflicts.setdefault(clan["clan_tag"], []).append((event["id"], event["cwl_season"]))
    return conflicts


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

    # 2026-08-15, project owner's spec: account-wide enrollment-pool expansion toggle — see
    # start_cwl_enrollment()'s docstring (QBdiscocmdshelper_cwl.py) for the actual mechanic.
    # Same "Status: {emoji} {text}" line shape as every other enable/disable setting on this
    # screen (hub_block above) — live-testing feedback, 2026-08-15: it read as an outlier
    # squeezed onto one line with the description instead.
    include_all_accounts = guild_config.get("cwl_enrollment_include_all_linked_accounts", False)
    include_all_accounts_status_emoji = "🟢" if include_all_accounts else "🔴"
    include_all_accounts_status_text = (
        t('cwl.settings.status_enabled', guild_id=guild_id_int)
        if include_all_accounts
        else t('cwl.settings.status_disabled', guild_id=guild_id_int)
    )
    enrollment_pool_block = (
        f"⠀\n**{t('cwl.settings.enrollment_pool_block_title', guild_id=guild_id_int)}**\n"
        f"{t('cwl.settings.enrollment_pool_status', guild_id=guild_id_int, status=f'{include_all_accounts_status_emoji} {include_all_accounts_status_text}')}\n"
        f"{t('cwl.settings.enrollment_pool_description', guild_id=guild_id_int)}"
    )

    embed = discord.Embed(
        title=t('cwl.settings.title', guild_id=guild_id_int),
        description=t('cwl.settings.description', guild_id=guild_id_int, guild_name=guild.name),
        color=discord.Color.gold(),
    )
    embed.add_field(name="", value=hub_block, inline=False)
    embed.add_field(name="", value=retention_block, inline=False)
    embed.add_field(name="", value=enrollment_pool_block, inline=False)

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
        # Monospaced code-block table, not bullet lines (project owner's explicit "clean table
        # like design" ask) — clan tag/"League"/"roster slots" text all dropped here to keep rows
        # short enough for large families to fit on one screen. cwl_start_at_compact() (not the
        # native <t:...> markup used elsewhere) renders a fixed, DST-aware "YY-MM-DD HH:MM" in
        # the guild's configured timezone_name (Basic Config's "Select Timezone", next to "Select
        # Language") — Discord doesn't parse <t:...> markup inside code blocks at all, so a
        # code-block table can't use native per-viewer timestamps regardless of format. The
        # header shows a short abbreviation ("CEST") rather than the full IANA zone name
        # ("Europe/Berlin") — the latter reliably wraps the header row inside Discord's
        # code-block width, breaking the table's column alignment. See timezone_abbreviation()'s
        # docstring for why it's resolved against the season's own start, not "now".
        timezone_name = CACHE.server_config.get(str(guild_id_int), {}).get("timezone_name", "UTC")
        tz_abbr = timezone_abbreviation(timezone_name, event["cwl_season"])
        header_clan = t('cwl.management.table_header_clan', guild_id=guild_id_int)
        header_tier = t('cwl.management.table_header_tier', guild_id=guild_id_int)
        header_roster = t('cwl.management.table_header_roster', guild_id=guild_id_int)
        header_start = f"{t('cwl.management.table_header_start', guild_id=guild_id_int)} ({tz_abbr})"

        rows = []
        for clan in clans:
            clan_name = CACHE.get_clan_name(clan["clan_tag"], clan["clan_tag"])
            tier = _tier_for(clan) or t('cwl.management.tier_unset', guild_id=guild_id_int)
            tier_short = tier.replace(" League", "")
            start_display = (
                cwl_start_at_compact(clan.get("cwl_start_at"), timezone_name)
                or t('cwl.management.start_time_unset', guild_id=guild_id_int)
            )
            rows.append((clan_name, tier_short, str(clan["roster_size"]), start_display))

        name_w = max(len(header_clan), *(len(r[0]) for r in rows))
        tier_w = max(len(header_tier), *(len(r[1]) for r in rows))
        roster_w = max(len(header_roster), *(len(r[2]) for r in rows))
        start_w = max(len(header_start), *(len(r[3]) for r in rows))

        table_lines = [
            f"{header_clan.ljust(name_w)}  {header_tier.ljust(tier_w)}  {header_roster.ljust(roster_w)}  {header_start}",
            f"{'-' * name_w}  {'-' * tier_w}  {'-' * roster_w}  {'-' * start_w}",
        ]
        for name, tier_short, roster, start_display in rows:
            table_lines.append(f"{name.ljust(name_w)}  {tier_short.ljust(tier_w)}  {roster.ljust(roster_w)}  {start_display}")
        clans_block = "```\n" + "\n".join(table_lines) + "\n```"
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


def resolve_guild_member_clan_tags(guild_id: int) -> List[str]:
    """All of a guild's member clan tags — individually configured member_clans plus every clan
    covered by a member_family — the same "is this clan actually relevant to this guild"
    resolution QBdiscocmdshelper.py's is_player_in_member_clans() and cache_manager.py's
    update_all_clan_subscription_statuses() already use, applied here to build the FULL set
    instead of testing one candidate clan. Used as the CWL enrollment candidate pool (2026-08-14
    redesign, project owner's spec): every current member of every guild member clan belongs in
    the pool, regardless of whether that specific clan opted into this season's CWL — only the
    auto-assignment TARGET (and the columns shown on the board) are restricted to clans actually
    participating this season."""
    config = CACHE.server_config.get(str(guild_id), {})
    tags: List[str] = list(config.get("member_clans", []))
    for family_id in config.get("member_families", []):
        family_data = CACHE.clan_families.get(family_id, {})
        for clan_tag in family_data.get("clans", []):
            if clan_tag not in tags:
                tags.append(clan_tag)
    return tags


def resolve_cwl_pool_clan_tags_sync(guild_id: int, event_id: Optional[int]) -> List[str]:
    """The guild's whole clan family unioned with every clan configured for event_id —
    participating or not (rule b/f, CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md: pool membership
    ignores the participating toggle, and unchecking a guest clan leaves its members pooled) —
    the definition of "this guild's own lineup for the season," which a guest, by definition, is
    never part of. event_id=None (no CWL event exists yet for the guild's selected season) still
    returns the family half — that's not conditional on a season existing.

    Single source for this union, shared by start_cwl_enrollment, resolve_cwl_pool_dm_targets_sync
    and the Guests search's own already-in-the-lineup exclusion (web_bridge.py) — three near-
    identical inline unions were already drifting apart once (2026-08-20, see
    resolve_cwl_pool_dm_targets_sync's own docstring), so a fourth inline copy for the search
    wasn't an option."""
    family_tags = set(resolve_guild_member_clan_tags(guild_id))
    db = CACHE.db_manager
    if db is None or event_id is None:
        return list(family_tags)
    all_clans = db.get_cwl_event_clans_sync(event_id)
    return list(family_tags | {c["clan_tag"] for c in all_clans})


def get_cwl_guest_clan_tags_sync(db: Any, event_id: int, guild_id: int) -> Set[str]:
    """Every clan currently on this event's roster (checked or unchecked — rule f means an
    unchecked guest clan's members stay pooled too) that is NOT part of the guild's own family —
    i.e. every guest clan. (2026-08-19, guest-player provenance feature, project owner's spec.)

    Used to classify a guest player's provenance WITHOUT persisting a new "how were you added"
    column: a guest player whose LIVE current clan (user_players.current_clan_tag) is one of
    these tags is "clan-derived" — their presence in the pool traces back to that guest clan
    being on the roster, so only removing the whole clan can remove them (rule f) — never
    individually. A guest player whose current clan is anything else (no clan at all, an
    unrelated clan, or a clan that was never invited as a guest) was necessarily added some other
    way — in practice, individually (rule c).

    Deliberately DERIVED from live state on every call, not a frozen flag set once at seed time:
    this is exactly what makes "clan invitation beats individual invitation" (project owner's
    spec, verbatim: "if a guest player is invited individually and then his clan is invited as a
    guest clan then the status of that player should be switched from individual invitation to
    guest clan invitation") fall out for free — the moment that clan is added to the roster, this
    function starts classifying the player as clan-derived on every subsequent check, with no
    write-time flip logic needed anywhere. The reverse can't silently happen: a guest CLAN never
    gets removed without going through remove_cwl_guest_clan (rule f), which purges its members
    from the pool entirely — there's no path where a clan simply drops off this set while its
    members remain pooled."""
    family_tags = set(resolve_guild_member_clan_tags(guild_id))
    return {c["clan_tag"] for c in db.get_cwl_event_clans_sync(event_id) if c["clan_tag"] not in family_tags}


def has_cwl_pool_members_missing_dm(guild_id: int, season: str) -> bool:
    """Rule h's button-gating check (2026-08-18, CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md) —
    true when at least one of this guild's currently-pooled, DM-able players has never been sent
    the enrollment DM by ANY guild this season. Drives the "Notify New Pool Members" button
    (add_cwl_management_components, ui_cwl_roster.py).

    Answers "would pressing that button actually reach anyone?", so it resolves the pool through
    the very same resolve_cwl_pool_dm_targets_sync() the button itself (and Start Enrollment)
    uses — anything else risks the button appearing for a pool the action then declines to DM, or
    vice versa. Safe on this call path despite running straight on the event loop (Pitfall 26,
    COPILOT_PITFALLS_COOKBOOK.md — add_cwl_management_components() is the synchronous CWL
    Management screen render): the resolver is a handful of indexed lookups, not the
    skill-score/avg-stars board payload builder."""
    db = CACHE.db_manager
    if db is None:
        return False
    event = db.get_cwl_event_sync(str(guild_id), season)
    if event is None or event["status"] in ("draft", "cancelled"):
        return False

    tags_with_discord = [
        target["player_tag"]
        for target in resolve_cwl_pool_dm_targets_sync(guild_id, event["id"], season)["targets"]
    ]
    if not tags_with_discord:
        return False

    dm_status = db.get_cwl_player_season_dm_status_bulk_sync(tags_with_discord, season)
    return not all(dm_status.get(tag, False) for tag in tags_with_discord)


def resolve_prior_cwl_assignments(player_tags: List[str], participating_clan_tags: List[str]) -> Dict[str, str]:
    """player_tag -> clan_tag, the "Manage Enrollment" auto-assignment seed. Redesigned
    2026-08-14 (project owner's spec, replacing the 2026-08-10 per-clan-roster original): for
    each of ``player_tags`` (the full candidate pool — every current member of every guild
    member clan, not just clans participating in CWL this season), finds their own single most
    recent REAL CWL attack across ANY clan they've ever attacked for
    (get_last_real_cwl_attack_clan_sync — "last attack" literally, not merely being listed on a
    war's roster; a 0-attack sentinel row doesn't count), and assigns them to that clan — but
    ONLY if that clan is actually participating this season (``participating_clan_tags``), since
    that's the only set of clans with a column on the board to assign them into. A player whose
    last real CWL attack was for a clan that isn't participating this season is therefore left
    unassigned, same as a player with no CWL history at all — there's nowhere to put them.

    Assignment is independent of the player's CURRENT clan — a player who attacked for clan A
    last CWL and has since transferred to clan B is still assigned to clan A if clan A is
    participating, matching "assign to wherever they last actually played," not "wherever
    they're currently rostered."

    A player with no real CWL attack on record anywhere simply isn't in the returned dict — the
    caller decides what "no prior assignment" means (in start_cwl_enrollment(), it just means
    they start out in the Unassigned pool)."""
    db = CACHE.db_manager
    if db is None or not player_tags:
        return {}

    participating = set(participating_clan_tags)
    last_attack_clan = db.get_last_real_cwl_attack_clan_sync(player_tags)
    return {
        player_tag: clan_tag
        for player_tag, (clan_tag, _date) in last_attack_clan.items()
        if clan_tag in participating
    }


async def resolve_cwl_clan_owner(
    clan_tag: str, season: str, affected_guild_ids: List[int]
) -> Tuple[str, str, Optional[int]]:
    """Cross-guild shared-clan ownership resolver (2026-08-15, project owner's spec): among
    affected_guild_ids (guilds already claiming clan_tag as participating for `season` — by
    construction every one of them already has a cwl_events row for it, see
    ensure_cwl_clan_sharing's caller), decide which guild is the "owner" — the one with a
    Discord-linked, guild-present account holding the clan's real in-game Leader or Co-Leader
    rank. Leader strictly outranks Co-Leader; within the same rank, a verified link (proven CoC
    API token ownership) outranks an unverified one.

    Tie-break bugfix (2026-08-15, live-testing feedback): a real leader's Discord account is very
    often a member of MORE than one of the affected guilds' servers — an alliance server, a
    personal/test server, etc. — which has nothing to do with which guild actually manages the
    clan. Originally, a tie beyond role+verified fell to whichever guild happened to be checked
    first, and affected_guild_ids always lists the ACTING (newly-joining) guild first — so a
    brand-new guest-clan guild would silently beat the clan's real, pre-existing home guild
    whenever the same leader account happened to be present in both. Fixed by ranking "is this
    clan tracked in THIS guild's own member_clans/member_families" (resolve_guild_member_clan_tags)
    ahead of iteration order — a guild that natively tracks the clan as its own is much stronger
    evidence of being its real administrative home than mere incidental Discord co-membership.

    Returns (owner_guild_id, resolution_method, owner_event_id). If no affected guild has a
    resolvable leader/co-leader (clan not found, no linked account, none of the linked accounts
    are actually members of any affected guild's Discord server — including when the bot process
    running this resolution simply isn't a member of one of the affected guilds at all, e.g. a
    DEV bot testing against a real PROD-only guild, logged as a warning below so that's instantly
    diagnosable), resolution_method='unresolved_first_claimer' and owner = affected_guild_ids[0]
    — sharing still proceeds (never blocks), just with no eviction rights granted to anyone until
    a later resolution succeeds.

    Deliberately resolved once and frozen by the caller (not re-run on every periodic role
    poll) — see CWL_ROSTER_PLANNING_PLAN.md for why continuous re-resolution would be more
    confusing than helpful mid-season.
    """
    import QBcore
    from qapbot.guild_role_manager import COC_ROLE_PRIORITY

    db = CACHE.db_manager

    # Live roster — same role-name mapping already used by the one other place in the codebase
    # that parses coc.Role (coc_cache.py's clan-member sync loop), so this stays consistent with
    # it rather than re-deriving a second, possibly-drifting version of the same mapping.
    role_by_tag: Dict[str, str] = {}
    try:
        clan = await CACHE.coc_clan_cache.get_clan(clan_tag)
        for member in clan.members:
            raw_role = getattr(member, "role", None)
            raw_name = getattr(raw_role, "name", None) if raw_role else None
            role = ("coLeader" if raw_name == "co_leader" else raw_name) if raw_name else None
            if role:
                role_by_tag[member.tag] = role
    except Exception as e:
        logging.warning(f"[CWL-SHARED-CLAN] Could not fetch live roster for {clan_tag} to resolve ownership: {e}")

    candidate_tags = [tag for tag, role in role_by_tag.items() if role in ("leader", "coLeader")]
    links = db.get_player_links_sync(candidate_tags) if (db is not None and candidate_tags) else {}

    # Diagnostic visibility (2026-08-15, live-testing feedback: a DEV bot instance that isn't
    # actually a member of a real production guild's Discord server can never verify *that*
    # guild's membership at all — get_guild() returns None and the guild is silently excluded
    # from the whole candidate search below, with no other signal that anything went wrong. Not
    # a bug in the ranking logic itself, but worth a loud warning so a wrong resolution like that
    # is instantly diagnosable instead of requiring a DB deep-dive to explain).
    for guild_id in affected_guild_ids:
        if QBcore.bot.get_guild(guild_id) is None:
            logging.warning(
                f"[CWL-SHARED-CLAN] Bot is not a member of guild {guild_id} — it can never be "
                f"credited as the owner of {clan_tag} from this bot process, even if its real "
                f"Leader/Co-Leader account is linked and verified."
            )

    # "Native family" tiebreak (2026-08-15, live-testing feedback / bugfix): a guild that tracks
    # clan_tag as one of its OWN member_clans/member_families is far stronger evidence of being
    # that clan's real administrative home than "some linked leader's Discord account happens to
    # also be a member of this guild's server" — the latter is unreliable on its own (the same
    # real leader is very often ALSO a member of other Discord servers with zero bearing on which
    # one manages the clan: alliance servers, personal/test servers, etc.). Ranked ahead of guild
    # iteration order so a guest-clan guild doesn't win a same-role/same-verified tie against the
    # clan's actual home guild just by being checked first (affected_guild_ids always lists the
    # ACTING guild first, which is exactly backwards as a tiebreak default).
    best: Optional[Tuple[int, bool, bool, str, str]] = None  # (role_priority, verified, is_native_family, guild_id, resolution_method)
    for tag in candidate_tags:
        link = links.get(tag)
        if not link or not link.get("discord_id"):
            continue
        discord_id = link["discord_id"]
        role = role_by_tag[tag]
        role_priority = COC_ROLE_PRIORITY.get(role, 0)
        verified = bool(link.get("verified"))
        resolution_method = ("leader" if role == "leader" else "coleader") + ("_verified" if verified else "_unverified")

        for guild_id in affected_guild_ids:
            guild = QBcore.bot.get_guild(guild_id)
            if guild is None:
                continue
            member = guild.get_member(int(discord_id))
            if member is None:
                try:
                    member = await guild.fetch_member(int(discord_id))
                except (discord.NotFound, discord.HTTPException):
                    continue
            if member is None:
                continue
            is_native_family = clan_tag in resolve_guild_member_clan_tags(guild_id)
            candidate = (role_priority, verified, is_native_family, str(guild_id), resolution_method)
            if best is None or candidate[:3] > best[:3]:
                best = candidate

    if best is not None:
        owner_guild_id, resolution_method = best[3], best[4]
    else:
        owner_guild_id, resolution_method = str(affected_guild_ids[0]), "unresolved_first_claimer"

    owner_event_id: Optional[int] = None
    if db is not None:
        owner_event = db.get_cwl_event_sync(owner_guild_id, season)
        owner_event_id = owner_event["id"] if owner_event else None

    return owner_guild_id, resolution_method, owner_event_id


async def ensure_cwl_clan_sharing(guild_id: int, event_id: int, season: str, clan_tag: str) -> Optional[Dict[str, Any]]:
    """Cross-guild shared-clan orchestrator (2026-08-15, project owner's spec) — called from
    both trigger points (handle_post_clan_config in web_bridge.py, when a clan becomes newly
    participating; start_cwl_enrollment below, for each of its participating clans) so the logic
    lives in exactly one place. No-ops (returns None) if clan_tag isn't shared with any other
    guild for this season — the overwhelming majority of calls, and cheap (one indexed lookup).

    When sharing IS involved, returns {"shared_clan_id", "owner_guild_id",
    "owner_resolution_method", "is_new", "other_guild_ids"} for the caller to build a
    confirmation prompt / notification from — this function itself does not send any DM or post
    any message, callers own that (different trigger points want different wording).
    owner_resolution_method is one of resolve_cwl_clan_owner's own values ("leader_verified",
    "leader_unverified", "coleader_verified", "coleader_unverified", or "unresolved_first_claimer"
    when no resolvable Leader/Co-Leader was found in any affected guild — added 2026-08-18,
    live-tested bug: notify_cwl_clan_shared used to always claim "real in-game Leader/Co-Leader"
    ownership even for the unresolved case, which is simply false)."""
    # Every sync DB call below is asyncio.to_thread()-wrapped (2026-08-16, Pitfall 26,
    # COPILOT_PITFALLS_COOKBOOK.md — root-caused from a PROD incident in ui_cwl_roster.py's Add
    # New Season button, but the same "sync write called directly on the event loop thread"
    # anti-pattern was present throughout this whole shared-clan subsystem). Each branch's own
    # sequence of DB calls is bundled into ONE to_thread() hop (a private _*_sync helper) rather
    # than one hop per line — this branch currently has zero `await`s in it, so it's atomic with
    # respect to other coroutines today (nothing else can run between its reads and writes); a
    # per-line wrap would introduce new interleaving windows a second concurrent
    # ensure_cwl_clan_sharing() call for the same clan_tag/season could race through. One hop for
    # the whole branch preserves that.
    db = CACHE.db_manager
    if db is None:
        return None
    guild_id_str = str(guild_id)

    existing = await asyncio.to_thread(db.get_cwl_shared_clan_sync, clan_tag, season)
    if existing is not None:
        # Already an established shared clan — just attach this guild, no re-resolution (owner
        # stays frozen once resolved; see resolve_cwl_clan_owner's docstring).
        return await asyncio.to_thread(
            _attach_guild_to_existing_shared_clan_sync, db, existing, event_id, clan_tag, guild_id_str,
        )

    others = await asyncio.to_thread(
        db.find_cwl_clan_participation_across_guilds_sync, clan_tag, season, exclude_guild_id=guild_id_str,
    )
    if not others:
        return None  # not shared with anyone — nothing to do, the common case

    affected_guild_ids = [guild_id] + [int(o["guild_id"]) for o in others]
    owner_guild_id, resolution_method, owner_event_id = await resolve_cwl_clan_owner(clan_tag, season, affected_guild_ids)
    if owner_event_id is None:
        # Defensive only — every affected guild already has an event for this season by
        # construction (find_cwl_clan_participation_across_guilds_sync only returns guilds with
        # a real participating row), so this shouldn't happen; never let a resolution bug crash
        # the calling flow (guest-clan add / Start Enrollment) over it.
        owner_guild_id, owner_event_id, resolution_method = guild_id_str, event_id, "unresolved_first_claimer"

    return await asyncio.to_thread(
        _create_new_shared_clan_sync, db, clan_tag, season, owner_guild_id, owner_event_id,
        resolution_method, event_id, guild_id_str, others,
    )


def _attach_guild_to_existing_shared_clan_sync(
    db: Any, existing: Dict[str, Any], event_id: int, clan_tag: str, guild_id_str: str,
) -> Dict[str, Any]:
    """Synchronous unit of work for ensure_cwl_clan_sharing()'s "already-shared" branch — run as
    one atomic asyncio.to_thread() hop by that function; see its own comment for why."""
    db.add_guild_to_shared_clan_sync(existing["id"], guild_id_str, event_id)
    _migrate_local_clan_roster_to_shared(db, event_id, existing["id"], clan_tag, guild_id_str)
    sync_cwl_shared_clan_roster_to_local_pools(existing["id"])
    other_guild_ids = [
        g["guild_id"] for g in db.list_cwl_shared_clan_guilds_sync(existing["id"]) if g["guild_id"] != guild_id_str
    ]
    return {
        "shared_clan_id": existing["id"],
        "owner_guild_id": existing["owner_guild_id"],
        "owner_resolution_method": existing["owner_resolution_method"],
        "is_new": False,
        "other_guild_ids": other_guild_ids,
    }


def _create_new_shared_clan_sync(
    db: Any, clan_tag: str, season: str, owner_guild_id: str, owner_event_id: int, resolution_method: str,
    event_id: int, guild_id_str: str, others: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Synchronous unit of work for ensure_cwl_clan_sharing()'s "brand-new shared clan" branch —
    run as one atomic asyncio.to_thread() hop by that function; see its own comment for why."""
    shared_clan_id = db.create_cwl_shared_clan_sync(clan_tag, season, owner_guild_id, owner_event_id, resolution_method)
    if shared_clan_id is None:
        return None
    db.add_guild_to_shared_clan_sync(shared_clan_id, guild_id_str, event_id)
    _migrate_local_clan_roster_to_shared(db, event_id, shared_clan_id, clan_tag, guild_id_str)
    for other in others:
        db.add_guild_to_shared_clan_sync(shared_clan_id, other["guild_id"], other["event_id"])
        _migrate_local_clan_roster_to_shared(db, other["event_id"], shared_clan_id, clan_tag, other["guild_id"])
    sync_cwl_shared_clan_roster_to_local_pools(shared_clan_id)

    return {
        "shared_clan_id": shared_clan_id,
        "owner_guild_id": owner_guild_id,
        "owner_resolution_method": resolution_method,
        "is_new": True,
        "other_guild_ids": [o["guild_id"] for o in others],
    }


def _seed_status_from_global_sync(db: Any, player_tag: str, cwl_season: str) -> str:
    """The status a NEW local cwl_signups row must start in, for this player and season.

    cwl_player_season_status is the global source of truth for whether a player has already
    answered (rule h, CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md — project owner's spec: "that
    status is shown automatically in guild a's and guild B's clan rosters. no need to manage
    anything manually"). A response is a fact about the *player and season*, not about whichever
    guild happened to DM them, so a local row created afterwards by a DIFFERENT guild must adopt
    it rather than starting fresh at 'pending' and contradicting what the player already said.

    start_cwl_enrollment() already does this via the bulk reader; this is the single-player
    equivalent for the four other paths that create signup rows (2026-08-22 audit: the
    cross-guild orphan mirror, drag-and-drop placement, the auto_seeded pool seed and the guest
    invite all hardcoded 'pending'). Verified latent rather than active at the time of the fix —
    0 live rows had drifted — but 31 of 116 global rows already held a real response, so the
    precondition was fully in place.

    Callers must still only use this when CREATING a row: an existing local row is never
    overwritten from here (all four call sites sit inside `if get_cwl_signup_sync(...) is None`).
    """
    existing_global = db.get_cwl_player_season_status_sync(player_tag, cwl_season)
    return existing_global["status"] if existing_global else "pending"


def _live_owners_or_sync(db: Any, player_tags: List[str]) -> Dict[str, Optional[str]]:
    """player_tag -> the account's CURRENT owner from user_players, for carry-forward writes.

    cwl_signups and cwl_shared_clan_players are enrollment-time SNAPSHOTS (Pitfall 37). Every
    READ path now re-resolves ownership live, so a stale value in either column can no longer
    mis-route a DM or grey out a board tile — but the write paths that COPY one snapshot into
    the other (a clan becoming shared, a drag-and-drop placement) were still laundering an
    outdated owner into a second table, where the next feature to read that column would
    naturally trust it. Resolving at the write boundary keeps the stale value from spreading in
    the first place, the same way CwlSignupResponseButton self-heals the row it touches.

    A tag with no user_players row at all (a guest tag added by search that was never linked)
    is absent from the result — callers keep their own fallback for that. A tag whose only row
    is the UNASSIGNED sentinel maps to None; callers deliberately fall back rather than blanking,
    so this can only ever CORRECT an owner, never erase a record of who was originally DMed.

    Batched (one query for the whole set) — the migrate-to-shared path below runs over a whole
    clan roster, and a per-player query there would be one round-trip per assigned member.
    """
    if not player_tags:
        return {}
    return {tag: link["discord_id"] for tag, link in db.get_player_links_sync(player_tags).items()}


def _migrate_local_clan_roster_to_shared(db: Any, event_id: int, shared_clan_id: int, clan_tag: str, guild_id_str: str) -> None:
    """Folds a guild's pre-existing LOCAL cwl_assignments+cwl_signups rows for clan_tag into the
    shared roster (cwl_shared_clan_players) the moment that guild attaches to a shared clan —
    without this, a clan that already had confirmed players/assignments in this guild's own
    event before becoming shared would silently lose that data once the live board switches
    over to reading cwl_shared_clan_players instead (see _build_enrollment_payload's merge).
    Only migrates players actually ASSIGNED to clan_tag (via cwl_assignments) — a player merely
    signed up but not yet assigned to any clan isn't specific to this clan, so stays exactly
    where it already was (the general Unassigned pool, unaffected by clan-level sharing).

    Sets BOTH placement and status explicitly, in two separate calls (2026-08-16, live-testing
    feedback — see cwl_shared_clan_players' own CREATE TABLE comment for the full status/assigned
    split rationale) — this is the one legitimate place that needs to set both at once, since it's
    carrying forward two genuinely real, independent prior facts (they WERE locally assigned here,
    AND they had this exact real response), not deriving one from the other."""
    migrating = [
        signup
        for assignment in db.get_cwl_assignments_sync(event_id)
        if assignment["assigned_clan_tag"] == clan_tag
        for signup in [db.get_cwl_signup_sync(event_id, assignment["player_tag"])]
        if signup is not None
    ]
    # Resolve current ownership once for the whole roster rather than carrying each signup's
    # snapshot value straight across into cwl_shared_clan_players (2026-08-22, Pitfall 37).
    live_owners = _live_owners_or_sync(db, [s["player_tag"] for s in migrating])
    for signup in migrating:
        owner = live_owners.get(signup["player_tag"]) or signup["dmed_discord_id"]
        db.set_cwl_shared_clan_player_assignment_sync(
            shared_clan_id, signup["player_tag"], signup["player_name"], owner,
            True, signup["source"], guild_id_str,
        )
        db.set_cwl_shared_clan_player_status_sync(
            shared_clan_id, signup["player_tag"], signup["player_name"], owner,
            signup["status"], signup["source"], guild_id_str, signup.get("responded_at"),
        )


def sync_cwl_shared_clan_roster_to_local_pools(shared_clan_id: int) -> None:
    """De-sync guard (2026-08-15, project owner's spec): cwl_shared_clan_players is authoritative
    for board display (_build_enrollment_payload's merge always re-reads it live), but a player
    added there by ONE attached guild is otherwise invisible to any OTHER attached guild's own
    LOCAL cwl_signups-based logic — Start Enrollment's DM blast already ran, so nothing will
    re-seed them, and any current or future feature that reads a guild's own cwl_signups table
    directly (not through the merge) would silently never learn this player exists for that guild.
    Mirrors every current shared-roster player into every attached guild's own cwl_signups as a
    'guest_invite' row, so each guild's local pool always has at least a placeholder entry.

    Never overwrites an existing local row (checked via get_cwl_signup_sync first) — a guild may
    already have its own real signup for that player_tag (its own family member, or their actual
    DM-response history), which a mirrored placeholder must never clobber; this is purely a
    fill-the-gap operation, exactly what avoids creating duplicate/conflicting local records for
    the same player. Called after every write to cwl_shared_clan_players (both here and from
    web_bridge.py's signup/assign handlers) — idempotent and cheap (one clan's roster, rarely
    shared), so simplest to just always resync the whole roster rather than track deltas."""
    db = CACHE.db_manager
    if db is None:
        return
    guild_rows = db.list_cwl_shared_clan_guilds_sync(shared_clan_id)
    if not guild_rows:
        return
    shared_players = db.get_cwl_shared_clan_players_sync(shared_clan_id)
    if not shared_players:
        return
    for guild_row in guild_rows:
        event_id = guild_row["event_id"]
        for player in shared_players:
            if db.get_cwl_signup_sync(event_id, player["player_tag"]) is not None:
                continue
            db.upsert_cwl_signup_sync(
                event_id, player["player_tag"], player["player_name"], player["dmed_discord_id"], None,
                source="guest_invite", status=player["status"],
            )


async def propagate_cwl_player_response(
    player_tag: str, cwl_season: str, status: str, responded_at: Optional[str],
    player_name: Optional[str], discord_id: Optional[str],
    originating_event_id: int, originating_guild_id: int,
) -> List[int]:
    """Writes the global source of truth (cwl_player_season_status) then fans the new status out
    to every OTHER guild's already-existing local mirror for this exact player_tag+season — the
    season-wide generalization of sync_cwl_shared_clan_roster_to_local_pools's one-shared-clan
    fan-out, above (2026-08-18, CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md rule h, project
    owner's spec, verbatim: "The player has a global 'Got dm message already' attribute...
    Then the player accepts or declines or is pending and that status is shown automatically in
    guild a's and guild B's clan rosters. no need to manage anything manually").

    Never CREATES a new row anywhere (mirrors update_cwl_signup_status_sync's own "only fills a
    gap in an already-pooled guild" contract) — only a guild that already has this player pooled
    gets their local mirror updated; a guild that hasn't pooled them at all is untouched, exactly
    like sync_cwl_shared_clan_roster_to_local_pools never invents shared-roster membership.

    The one real caller, CwlSignupResponseButton.callback, must still do its OWN own-guild write
    first (this function's job starts one step later — propagating a response that's already been
    recorded for the originating event/guild) — that's what lets a brand-new signup row get
    created there via the normal upsert, something this function deliberately never does.

    Returns every OTHER guild_id (deduped, excluding the originating one) whose board just
    changed, for the caller's bump_enrollment_version() fan-out (Step 8 pattern)."""
    db = CACHE.db_manager
    if db is None:
        return []
    return await asyncio.to_thread(
        _propagate_cwl_player_response_sync, db, player_tag, cwl_season, status, responded_at,
        player_name, discord_id, originating_event_id, originating_guild_id,
    )


def _propagate_cwl_player_response_sync(
    db: Any, player_tag: str, cwl_season: str, status: str, responded_at: Optional[str],
    player_name: Optional[str], discord_id: Optional[str],
    originating_event_id: int, originating_guild_id: int,
) -> List[int]:
    db.set_cwl_player_response_status_sync(
        player_tag, cwl_season, player_name, discord_id, status, responded_at,
        originating_event_id, originating_guild_id,
    )

    affected_guild_ids: List[int] = []
    for event_id in db.find_cwl_signup_events_for_player_and_season_sync(player_tag, cwl_season):
        if event_id == originating_event_id:
            continue
        db.update_cwl_signup_status_sync(event_id, player_tag, status, responded_at)
        event = db.get_cwl_event_by_id_sync(event_id)
        if event is not None:
            affected_guild_ids.append(int(event["guild_id"]))

    for shared_clan_id in db.find_cwl_shared_clan_ids_for_player_and_season_sync(player_tag, cwl_season):
        db.set_cwl_shared_clan_player_status_sync(
            shared_clan_id, player_tag, player_name, discord_id, status, "admin_added",
            str(originating_guild_id), responded_at,
        )
        for guild_row in db.list_cwl_shared_clan_guilds_sync(shared_clan_id):
            guild_id = int(guild_row["guild_id"])
            if guild_id != originating_guild_id:
                affected_guild_ids.append(guild_id)

    return list(dict.fromkeys(affected_guild_ids))  # dedup, preserve first-seen order


def assign_cwl_player_sync(
    guild_id: int,
    event_id: int,
    season: str,
    player_tag: str,
    target_clan_tag: Optional[str],
    *,
    source: str,
    assignment_source: Optional[str] = None,
    signup_source: str = "admin_added",
    locked: bool = False,
    deliberate: bool = True,
) -> Optional[str]:
    """The one general "place a player into a CWL pool" write path (2026-08-16, live-testing
    feedback, project owner's spec, verbatim: "we should have one general method that assigns
    players to any pool and the race condition checks should all be implemented there so that we
    don't have multiple code paths for player assignment/dragging"). Every caller that decides a
    player belongs in a specific clan column (or back in Unassigned) should route through this
    function instead of writing cwl_assignments/cwl_shared_clan_players directly, so the
    conflict-purge below can never be silently skipped by a new or overlooked call site.

    Confirmed live-testing bug this fixes: deleting and recreating a season's event re-ran Start
    Enrollment's own inline bulk auto-assign, which — unlike handle_post_cwl_enrollment_assign's
    drag-and-drop write path this function is extracted from — never checked whether a candidate
    was already a CONFIRMED member of a totally different shared clan's roster elsewhere in this
    guild. A player already a confirmed guest in one clan's shared roster got silently
    double-booked into another clan's column by the very next season's auto-assign seed, purely
    because that OTHER clan happened to be their last real CWL attack destination.

    Before writing the new placement, purges the player from every OTHER shared clan they're
    currently PLACED (assigned) in — same two-source union `handle_post_cwl_enrollment_assign`
    used (this guild's own currently-participating shared clans, PLUS, via
    find_cwl_shared_clan_memberships_for_player_sync, any shared clan anywhere this player is
    assigned in — covering the "sitting in the orphaned/Assigned-to-other-Guild pseudo-column"
    case too) — then writes the new placement:
      - target_clan_tag is shared with another guild this season: cwl_shared_clan_players,
        assigned=1 via set_cwl_shared_clan_player_assignment_sync — NEVER touches `status` (see
        that table's own CREATE TABLE comment: confirmation and assignment are deliberately
        separate columns) — clears any stale local cwl_assignments row.
      - target_clan_tag is None: clears any local cwl_assignments row (Unassigned).
      - otherwise (this guild's own private clan): cwl_assignments, first ensuring a local
        cwl_signups row exists (the purge above may have just removed the player's ONLY local
        record — e.g. they only ever existed via a shared roster row that just got purged —
        leaving them with an assignment but no resolvable name/discord_id otherwise).

    `source` labels cwl_shared_clan_players.source (why they're on that shared roster — e.g.
    'admin_override', 'auto_assigned'). `assignment_source` labels the separate, differently-
    scoped cwl_assignments.assignment_source column (defaults to `source` when omitted, matching
    the pre-extraction drag-and-drop behavior of using one label for both — pass it explicitly
    when a caller needs the two columns to disagree, e.g. the auto-assign seed uses
    source='auto_assigned' for the shared table but assignment_source='suggested' locally, to
    keep each column's own pre-existing vocabulary intact). `signup_source` labels a NEW local
    cwl_signups row created as a byproduct of the private-clan branch above (default 'admin_added',
    matching drag-and-drop's own pre-extraction behavior — pass 'auto_assigned' for an auto-seed
    caller so a freshly-created signup reads as machine-seeded, not admin-added).

    `deliberate` (default True, matching drag-and-drop's own pre-extraction behavior — a human
    dragging a card always wins, evicting whatever it takes) — set False for an automatic/
    best-guess placement (auto-assign seeds): if the player already holds ANY existing claim
    (already PLACED/assigned in a different shared clan, or a locked local assignment in a
    different clan) the target is never written, and it never evicts the existing claim either.
    When the conflict is a
    different SHARED clan specifically, it instead mirrors the player into a local assignment
    pointing at that real clan (2026-08-16 follow-up, live-testing feedback, project owner's spec:
    "instead of putting QManiac to the unassigned pool he should have been assigned to the
    'Assigned to other clan' pool") — so the frontend's orphaned pseudo-column surfaces where they
    actually are instead of them silently vanishing into plain Unassigned; a conflicting LOCKED
    LOCAL assignment elsewhere is left untouched since it's already visible wherever it points.
    This is the actual fix for the confirmed live-testing bug (2026-08-16, project owner's spec,
    verbatim): deleting and recreating a season's event re-ran Start Enrollment's own auto-assign
    seed, which silently moved a player who was already a deliberately admin_override-confirmed
    guest in one shared clan's roster into a completely different clan's column, purely because
    that other clan happened to be their last real CWL attack destination — an automatic guess
    must never override a real, existing placement, deliberate or not.

    EXCEPTION (2026-08-19 fix, live bug report, project owner: "the qcrew members were falsely
    auto-assigned to staycalm... theqcrew members get auto assigned to staycalm and not to the
    qcrew as they should"): the "different SHARED clan" conflict above is only honored as an
    UNTOUCHABLE existing claim when it's either (a) itself deliberate
    (cwl_shared_clan_players.source == 'admin_override' — a real human drag, never silently
    overridden, matching the locked-local-assignment rule right below it) or (b) the player's LIVE
    current clan tag (user_players.current_clan_tag) genuinely differs from target_clan_tag. If
    neither holds — a non-deliberate (e.g. 'auto_assigned') shared-clan row that the player's own
    live current clan actually contradicts — that live fact wins instead: it may well be stale
    (e.g. leftover from an earlier season cycle), and the normal write/eviction proceeds rather
    than deferring to it. This is what makes start_cwl_enrollment's own "current clan beats stale
    history" override (see resolve_prior_cwl_assignments' call site) actually take effect
    end-to-end — without this, that override's corrected target got silently discarded right back
    to the stale shared-clan entry the instant it reached this function.

    Returns None on a normal write (or a no-op defer, matching this function's pre-2026-08-20
    behavior of returning nothing either way) — or an error string when a DELIBERATE placement
    was refused because the player already has a private (non-cross-guild-shared) placement in a
    DIFFERENT guild's own CWL roster this season (2026-08-20, live bug report: guest-inviting a
    player individually — a completely separate code path from a guest CLAN, so it never went
    through any of the cross-guild conflict machinery above — let them get dragged into a SECOND,
    fully independent placement in a second guild's roster for the same season, something real
    CWL rules never allow. Unlike a same-guild shared-clan conflict, a private cross-guild
    conflict is refused rather than silently evicting the player from the other guild's board —
    this guild has no authority or visibility to safely do that on another guild's behalf; a
    human admin needs to resolve it directly, in that other guild, first). A non-deliberate
    (auto-assign) placement instead just quietly skips, the same as every other conflict this
    function already defers on above — see the check itself for why it doesn't also attempt the
    shared-clan branch's "mirror into an orphaned local assignment" treatment."""
    db = CACHE.db_manager
    if db is None:
        return None

    shared_clans_by_tag = get_event_shared_clans_by_tag_sync(event_id, season)

    if target_clan_tag is not None:
        other_private_placement = next(
            iter(db.find_cwl_player_private_placement_in_other_guilds_sync(player_tag, season, str(guild_id))),
            None,
        )
        if other_private_placement is not None:
            if not deliberate:
                # An automatic guess never creates a real conflict — just skip. Unlike the
                # shared-clan defer logic below, there's no local record here to mirror an
                # "Assigned to other Guild" placeholder into without also teaching
                # _build_enrollment_payload's purge step (web_bridge.py) to preserve a private
                # (non-shared) orphaned assignment — out of scope for this fix; the player simply
                # stays plain Unassigned here rather than being silently double-booked.
                return None
            other_clan_name = CACHE.get_clan_name(
                other_private_placement["clan_tag"], other_private_placement["clan_tag"]
            ) or other_private_placement["clan_tag"]
            import QBcore

            other_guild = QBcore.bot.get_guild(int(other_private_placement["guild_id"]))
            other_guild_name = other_guild.name if other_guild else f"guild {other_private_placement['guild_id']}"
            return (
                f"Already placed in {other_clan_name}'s CWL roster in {other_guild_name} this "
                f"season — remove that placement first (in that guild's own board) before "
                f"assigning them here."
            )

    shared_clan_ids_to_clear: Dict[int, str] = {
        shared["id"]: tag for tag, shared in shared_clans_by_tag.items() if tag != target_clan_tag
    }
    for membership in db.find_cwl_shared_clan_memberships_for_player_sync(season, player_tag):
        if membership["clan_tag"] != target_clan_tag:
            shared_clan_ids_to_clear[membership["shared_clan_id"]] = membership["clan_tag"]

    if not deliberate:
        # The player's REAL competing placements only — shared clans they are actually PLACED in
        # (cwl_shared_clan_players.assigned=1), never merely "some shared clan that happens to be
        # participating." shared_clan_ids_to_clear above deliberately casts a much wider net (it
        # drives the eviction sweep further down, where hitting a clan the player isn't in is a
        # harmless no-op delete) — reusing THAT set for this defer decision was a real bug
        # (2026-08-19 review): with any participating shared clan on the roster, EVERY
        # auto-assigned player hit this branch and got an "orphaned_elsewhere" assignment written
        # pointing at that clan, whether or not they had ever had anything to do with it. That is
        # what actually piled a guild's own members into a guest clan's column en masse (project
        # owner's live report: "the qcrew members were falsely auto-assigned to staycalm").
        actual_placements = [
            (m["shared_clan_id"], m["clan_tag"])
            for m in db.find_cwl_shared_clan_memberships_for_player_sync(season, player_tag)
            if m["clan_tag"] != target_clan_tag
        ]
        if actual_placements:
            def _placement_row(shared_clan_id: int) -> Optional[Dict[str, Any]]:
                return next(
                    (p for p in db.get_cwl_shared_clan_players_sync(shared_clan_id) if p["player_tag"] == player_tag),
                    None,
                )

            # A DELIBERATE placement elsewhere (source='admin_override' — a real human drag, not
            # an earlier auto-assign guess) is never overridden, no matter what — same rule as the
            # locked-local-assignment check below, unconditional. Scanned across ALL of the
            # player's placements rather than just the first one found, so a deliberate placement
            # can't be missed (and silently evicted) merely because some other, automatic one
            # happened to be enumerated ahead of it.
            chosen: Optional[Tuple[int, str, Dict[str, Any]]] = None
            for shared_clan_id, other_tag in actual_placements:
                row = _placement_row(shared_clan_id)
                if row is None:
                    continue
                if row["source"] == "admin_override":
                    chosen = (shared_clan_id, other_tag, row)
                    break
                if chosen is None:
                    chosen = (shared_clan_id, other_tag, row)

            # Anything NOT deliberate (an automatic/passive placement, e.g. 'auto_assigned') only
            # still wins if it isn't contradicted by the player's own LIVE current clan: if they
            # are genuinely a current member of the target right now, that live fact beats a
            # possibly-stale automatic placement, so fall through to the normal write/eviction
            # (which purges the stale row) instead of deferring to it.
            if chosen is not None:
                other_shared_row = chosen[2]
                is_deliberate_elsewhere = other_shared_row["source"] == "admin_override"
                current_clan_tag = db.get_current_clan_tags_for_players_sync([player_tag]).get(player_tag)
                if is_deliberate_elsewhere or current_clan_tag != target_clan_tag:
                    # Not evicted (an automatic guess never evicts a real placement) — but leaving
                    # the player with no local record at all would show them as bare Unassigned,
                    # which is misleading: they DO have a real home, just not one with a column in
                    # THIS event. Mirror them into a local assignment pointing at their real
                    # placement instead, the same "orphaned assignment preservation" pattern
                    # detach_guild_from_shared_clan_on_deactivation already uses, so the frontend's
                    # "Assigned to other Guild" pseudo-column (enrollmentBoard.ts) picks them up
                    # correctly instead of them silently vanishing into Unassigned (2026-08-16
                    # follow-up, live-testing feedback, project owner's spec, verbatim: "QManiac
                    # still assigned to StayCalm. So during auto-assignment this should have been
                    # recognized and instead of putting QManiac to the unassigned pool he should
                    # have been assigned to the 'Assigned to other clan' pool").
                    if db.get_cwl_signup_sync(event_id, player_tag) is None:
                        # Live owner, not the other guild's shared-roster snapshot (Pitfall 37).
                        mirrored_owner = (
                            _live_owners_or_sync(db, [player_tag]).get(player_tag)
                            or other_shared_row["dmed_discord_id"]
                        )
                        db.upsert_cwl_signup_sync(
                            event_id, player_tag, other_shared_row["player_name"],
                            mirrored_owner, None, signup_source,
                            _seed_status_from_global_sync(db, player_tag, season),
                        )
                    db.upsert_cwl_assignment_sync(
                        event_id, player_tag, chosen[1], assignment_source="orphaned_elsewhere", locked=False,
                    )
                    return
        existing_assignment = next(
            (a for a in db.get_cwl_assignments_sync(event_id) if a["player_tag"] == player_tag), None
        )
        if (
            existing_assignment is not None
            and existing_assignment["assigned_clan_tag"] != target_clan_tag
            and existing_assignment["locked"]
        ):
            return  # already deliberately placed elsewhere locally — same rule, same reason

    removed_player_name: Optional[str] = None
    removed_discord_id: Optional[str] = None
    for shared_clan_id in shared_clan_ids_to_clear:
        shared_row = next(
            (p for p in db.get_cwl_shared_clan_players_sync(shared_clan_id) if p["player_tag"] == player_tag),
            None,
        )
        if shared_row is not None:
            removed_player_name = shared_row["player_name"]
            removed_discord_id = shared_row["dmed_discord_id"]
        db.delete_cwl_shared_clan_player_sync(shared_clan_id, player_tag)
        purge_orphaned_shared_clan_guests_sync(shared_clan_id, player_tag)

    def _resolve_identity(candidate_tags: List[str]) -> Tuple[str, Optional[str]]:
        # The NAME keeps its existing snapshot-first precedence (the recorded name is the one
        # any DM text already used); only the OWNER is re-resolved live, since that value gets
        # persisted into cwl_shared_clan_players / a fresh cwl_signups row below and would
        # otherwise carry an outdated owner into a second table (2026-08-22, Pitfall 37).
        live_owner = _live_owners_or_sync(db, [player_tag]).get(player_tag)
        existing_signup = db.get_cwl_signup_sync(event_id, player_tag)
        if existing_signup is not None:
            return existing_signup["player_name"], live_owner or existing_signup["dmed_discord_id"]
        if removed_player_name is not None:
            return removed_player_name, live_owner or removed_discord_id
        member = next(
            (m for m in db.get_current_clan_members_sync(candidate_tags) if m["player_tag"] == player_tag),
            None,
        )
        if member is not None:
            return member["player_name"], live_owner or member["discord_id"]
        return player_tag, live_owner

    if target_clan_tag is not None and target_clan_tag in shared_clans_by_tag:
        player_name, discord_id = _resolve_identity(list(set(resolve_guild_member_clan_tags(guild_id)) | {target_clan_tag}))
        # Assignment-only (2026-08-16, live-testing feedback, project owner's spec: "Confirmation
        # status and assignment status should be treated completely separate"). Placing a player
        # here — whether by drag-and-drop or the auto-assign seed — is never itself a
        # confirmation; any genuine response they've already recorded (or haven't) is left
        # completely untouched by this call.
        db.set_cwl_shared_clan_player_assignment_sync(
            shared_clans_by_tag[target_clan_tag]["id"], player_tag, player_name, discord_id,
            True, source, str(guild_id),
        )
        sync_cwl_shared_clan_roster_to_local_pools(shared_clans_by_tag[target_clan_tag]["id"])
        db.delete_cwl_assignment_sync(event_id, player_tag)
    elif target_clan_tag is None:
        db.delete_cwl_assignment_sync(event_id, player_tag)
    else:
        player_name, discord_id = _resolve_identity(list(set(resolve_guild_member_clan_tags(guild_id)) | {str(target_clan_tag)}))
        if db.get_cwl_signup_sync(event_id, player_tag) is None:
            db.upsert_cwl_signup_sync(
                event_id, player_tag, player_name, discord_id, None, signup_source,
                _seed_status_from_global_sync(db, player_tag, season),
            )
        db.upsert_cwl_assignment_sync(
            event_id, player_tag, str(target_clan_tag),
            assignment_source=assignment_source or source, suggested_clan_tag=str(target_clan_tag), locked=locked,
        )
    return None


async def ensure_cwl_clan_membership_tracked(clan_tags: Iterable[str]) -> None:
    """Make sure every clan in clan_tags actually has live membership data in user_players before
    anything tries to seed a CWL player pool from it (2026-08-19 fix, live bug report, project
    owner: "I added Hoehenloher Land as a guest clan but its members don't show up in the player
    pool").

    Root cause this closes: EVERY pool seed in this feature ultimately reads
    get_current_clan_members_sync() — i.e. user_players.current_clan_tag — and that table is only
    ever populated by coc_cache.py's update_player_info_in_user_accounts(), which is deliberately
    gated on clans.has_active_subscriptions (the 2026-08-14 scope-bug incident: running it for
    every clan the shared get_clan() cache ever touches polluted user_players with thousands of
    CWL-opponent members and made every clan fetch slow — see the comment at that call site).
    has_active_subscriptions is computed in update_all_clan_subscription_statuses() from channel
    subscriptions + guild member_clans + member_families ONLY — a CWL **guest** clan is in none of
    those sets. So an invited guest clan that no guild on this bot happens to track independently
    has ZERO user_players rows, every seed reads an empty member list, and the clan gets a board
    column with no players in it, forever. The bug was masked in all earlier live testing because
    the guest clans used until now (e.g. StayCalm) happened to be another guild's own member clan
    and so were already tracked for unrelated reasons.

    Fix: for each clan with no tracked members, fetch it once from the CoC API and run the exact
    same population path the regular poll cycle uses (update_player_info_in_user_accounts), which
    creates the UNASSIGNED-pool user_players rows — with real name/TH/clan — that the seeds then
    find. Deliberately scoped to clans an admin has explicitly put on a CWL roster and only when
    they have no data at all, so it never reintroduces the 2026-08-14 blanket-tracking behavior.

    Not a substitute for a subscription: the snapshot is refreshed only on the next call here
    (guest-clan add / Start Enrollment), not by the poll cycle, since deliberately NOT flipping
    has_active_subscriptions avoids permanently ratcheting track_war_updates on for a clan that's
    only along for one season. A guest clan's roster barely moves inside a single CWL season, and
    re-adding the clan re-syncs it.

    Best-effort throughout — a CoC API failure logs and leaves the clan seeded from whatever is
    already known rather than failing the admin's save/Start Enrollment."""
    db = CACHE.db_manager
    if db is None:
        return
    tags = [t for t in dict.fromkeys(clan_tags) if t]
    if not tags:
        return

    tracked = await asyncio.to_thread(db.get_current_clan_members_sync, tags)
    known_tags = {m["clan_tag"] for m in tracked}
    missing = [t for t in tags if t not in known_tags]
    if not missing:
        return

    coc_clan_cache = getattr(CACHE, "coc_clan_cache", None)
    if coc_clan_cache is None or getattr(CACHE, "coc_client", None) is None:
        logging.warning(
            f"[CWL-POOL-SEED] {len(missing)} clan(s) have no tracked members and the CoC API "
            f"client isn't available — their members can't be pooled: {', '.join(missing)}"
        )
        return

    for clan_tag in missing:
        try:
            clan_obj = await coc_clan_cache.get_clan(clan_tag)
            await coc_clan_cache.update_player_info_in_user_accounts(clan_obj, CACHE)
            logging.info(
                f"[CWL-POOL-SEED] {clan_tag} had no tracked members (not subscribed by any guild) "
                f"— populated user_players from a live CoC fetch so its members can enter the pool"
            )
        except Exception as e:
            logging.warning(f"[CWL-POOL-SEED] Could not fetch members for untracked CWL clan {clan_tag}: {e}")


async def auto_assign_prior_cwl_members(guild_id: int, event_id: int, season: str, clan_tag: str) -> None:
    """Fills a gap Start Enrollment's own bulk seed can't reach (2026-08-15, live-testing
    feedback, project owner's spec): that seed only ever runs ONCE, over whichever clans were
    participating at that exact moment — a clan added to the roster *afterwards* (a guest clan,
    or any clan reactivated post-Start-Enrollment) gets a board column but no players, and stays
    that way forever since nothing else ever seeds it. Called from handle_post_clan_config
    (web_bridge.py) for every clan newly ADDED to the roster this save (2026-08-18 broadened,
    rule b/d, CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md, live-tested bug: a guest clan added
    while its checkbox stayed unchecked never got seeded at all under the old "only clans newly
    turning participating" trigger — pool membership must not depend on the checkbox) — once
    ensure_cwl_clan_sharing() has already settled whether it's shared, for whichever of those
    clans are actually participating. Only actually acts once enrollment has started (event
    status past 'draft'); a still-draft event doesn't need this at all, since Start Enrollment is
    about to comprehensively seed everything itself. See _auto_assign_prior_cwl_members_sync's
    own comment for how a non-participating clan still gets pool membership (Step 2 below) without
    ever being treated as a valid assignment target (Step 1).

    Two things happen here, both scoped to clan_tag's CURRENT members only:

    1. Auto-assignment (project owner's spec, verbatim): "check if that clan's player roster is
       still empty. If so auto-assign the players that are members of exactly that added clan and
       were participating in an earlier CWL in that exact clan." Two conditions, both required:
       CURRENT membership in clan_tag, AND that same clan_tag was the destination of their own
       last real CWL attack, anywhere (get_last_real_cwl_attack_clan_sync — same signal
       resolve_prior_cwl_assignments uses for Start Enrollment's own seed, just pre-filtered to
       this one clan). Gated PER PLAYER, not on the whole roster being empty (2026-08-16,
       live-testing feedback, project owner's spec, verbatim: "after removing staycalm as guest
       clan and then re-adding it only the two players that were assigned manually are in
       staycalm's roster. The re-add should also have done a re-auto-assign in case the guest
       clan is not controlled by its own guild.") — a clan re-added after deactivation can already
       carry over a couple of deliberately locked/admin_override placements (the ones
       _cleanup_local_pool_for_plain_clan_deactivation_sync intentionally preserves), and the OLD
       "roster non-empty -> skip everyone" gate wrongly treated those survivors as proof the whole
       clan was "already handled," silently skipping every other genuinely-qualifying player too.
       Only a player who is THEMSELVES already placed in clan_tag (assigned locally, or assigned=1
       in the shared table) is skipped — assign_cwl_player_sync's own deliberate=False conflict
       handling already protects anyone locked elsewhere from being evicted, so there was never a
       need for this function to also gate on the aggregate roster state.
    2. Visibility seed (2026-08-15 follow-up, live-testing feedback: members without prior CWL
       history in this exact clan were showing with no status icon at all — no ?/✓/✗, not even
       "Not Linked" — since they never got ANY cwl_signups/cwl_shared_clan_players row, unlike a
       clan that was already participating when Start Enrollment ran, where EVERY current member
       gets seeded 'pending' regardless of history). Every OTHER current member without a status
       row yet gets a plain 'pending' placeholder too — same as Start Enrollment's own bulk seed
       gives every clan present at that time, just applied retroactively for a clan added later.
       Unlike step 1, this isn't gated on placement state at all — it's pure visibility,
       independent of whether anyone's actually been assigned yet."""
    # This whole function is already pure sync DB work despite the `async def` (no `await` in the
    # body below) — bundled into one asyncio.to_thread() hop rather than wrapping each call
    # individually, both to avoid the overhead of many small hops and to keep the same
    # atomicity-with-respect-to-other-coroutines this body already had before this fix (2026-08-16,
    # Pitfall 26, COPILOT_PITFALLS_COOKBOOK.md).
    #
    # ...but first make sure there ARE members to seed from: a guest clan no guild on this bot
    # subscribes to has no user_players rows at all, so both steps below would silently no-op
    # (2026-08-19, live bug report — see ensure_cwl_clan_membership_tracked's own docstring). This
    # is an await, hence outside the to_thread hop; it's a cheap indexed lookup that returns
    # immediately for the normal case where the clan is already tracked.
    await ensure_cwl_clan_membership_tracked([clan_tag])
    await asyncio.to_thread(_auto_assign_prior_cwl_members_sync, guild_id, event_id, season, clan_tag)


def _auto_assign_prior_cwl_members_sync(guild_id: int, event_id: int, season: str, clan_tag: str) -> None:
    db = CACHE.db_manager
    if db is None:
        return

    # rule b/d (2026-08-18, CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md, live-tested bug: a guest
    # clan added to the roster while its checkbox stayed UNCHECKED never got its members into the
    # pool at all — this function is called for every newly-added-to-roster clan now (see
    # handle_post_clan_config, web_bridge.py), not just ones that are actually participating, so
    # that "in the pool" genuinely doesn't depend on the checkbox per rule b/f/d. A non-
    # participating clan is not a valid assignment TARGET though (participating_clan_tags still
    # restricts that everywhere else — resolve_prior_cwl_assignments, the auto-assign seed in
    # start_cwl_enrollment) — Step 1 below (actually placing a qualifying player INTO clan_tag's
    # column) is skipped for one, while Step 2 (pure pool-visibility seed, no assignment) still
    # runs unconditionally, matching rule d's "members of a guest clan enter the pool" literally
    # without ever assigning anyone to a column that doesn't currently exist on the board.
    clan_row = next((c for c in db.get_cwl_event_clans_sync(event_id) if c["clan_tag"] == clan_tag), None)
    is_participating = bool(clan_row["participating"]) if clan_row else True

    shared = db.get_cwl_shared_clan_sync(clan_tag, season)
    if shared is not None:
        shared_rows = db.get_cwl_shared_clan_players_sync(shared["id"])
        already_shared = {p["player_tag"] for p in shared_rows}
        already_placed = {p["player_tag"] for p in shared_rows if p.get("assigned")}
    else:
        already_shared = set()
        already_placed = {a["player_tag"] for a in db.get_cwl_assignments_sync(event_id) if a["assigned_clan_tag"] == clan_tag}

    current_members = db.get_current_clan_members_sync([clan_tag])
    if not current_members:
        return
    members_by_tag = {m["player_tag"]: m for m in current_members}

    qualifying_tags: set = set()
    if is_participating:
        current_tags = [m["player_tag"] for m in current_members]
        last_attack_clan = db.get_last_real_cwl_attack_clan_sync(current_tags)
        qualifying_tags = {
            tag for tag, (attack_clan_tag, _date) in last_attack_clan.items()
            if attack_clan_tag == clan_tag and tag not in already_placed
        }
        for tag in qualifying_tags:
            # Routed through the general assignment method (2026-08-16, live-testing feedback,
            # project owner's spec — see assign_cwl_player_sync's own docstring) so this seed can
            # never place a player here without first checking (and, if needed, evicting) any
            # OTHER shared clan they're already PLACED in — a target that's shared lands them
            # there via the separate `assigned` column (never touching `status`, see
            # cwl_shared_clan_players' own CREATE TABLE comment); otherwise they land in this
            # guild's own local cwl_assignments.
            assign_cwl_player_sync(
                guild_id, event_id, season, tag, clan_tag,
                source="auto_assigned", assignment_source="suggested", signup_source="auto_assigned",
                locked=False, deliberate=False,
            )
    # else: not currently a valid assignment TARGET (participating_clan_tags still restricts
    # that everywhere — see this function's own docstring) — every current member instead falls
    # straight through to Step 2's plain visibility seed below, landing in Unassigned with a
    # pending icon rather than any column.

    # Step 2: visibility seed for everyone else — see docstring. A 'pending' shared_clan_players
    # row still populates players_by_tag via _build_enrollment_payload's merge without counting
    # as an assignment (assigned=0 by default — unset by this status-only write, see
    # cwl_shared_clan_players' own CREATE TABLE comment), so this correctly lands them in
    # Unassigned with a pending icon, not in the clan's own column.
    for tag, member in members_by_tag.items():
        if tag in qualifying_tags:
            continue  # already handled above
        if shared is not None:
            if tag in already_shared:
                continue
            db.set_cwl_shared_clan_player_status_sync(
                shared["id"], tag, member["player_name"], member["discord_id"],
                "pending", "auto_seeded", str(guild_id),
            )
        else:
            if db.get_cwl_signup_sync(event_id, tag) is not None:
                continue
            db.upsert_cwl_signup_sync(
                event_id, tag, member["player_name"], member["discord_id"], member.get("preferred_league_rank"),
                "auto_seeded", _seed_status_from_global_sync(db, tag, season),
            )

    if shared is not None:
        sync_cwl_shared_clan_roster_to_local_pools(shared["id"])


def get_event_shared_clans_by_tag_sync(event_id: int, season: str) -> Dict[str, Dict[str, Any]]:
    """clan_tag -> cwl_shared_clans row, for every PARTICIPATING clan in this event that's
    shared (2026-08-15, slice 4: live shared roster) — used by assign_cwl_player_sync to know
    which clans need a cwl_shared_clan_players write instead of the normal per-guild
    cwl_signups/cwl_assignments one. Empty for the overwhelming majority of events (no shared
    clans at all)."""
    db = CACHE.db_manager
    if db is None:
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    for c in db.get_cwl_event_clans_sync(event_id):
        if not c.get("participating", 1):
            continue
        shared = db.get_cwl_shared_clan_sync(c["clan_tag"], season)
        if shared is not None:
            result[c["clan_tag"]] = shared
    return result


def get_cwl_event_shared_clan_info_sync(event_id: int, guild_id: int, season: str) -> List[Dict[str, Any]]:
    """Read-only: for an event, which of its clans are part of a shared-clan record, and which
    OTHER guild(s) are also attached — used both to preview the delete-season confirmation
    dialog's warning text (before anything is mutated) and, at actual confirm time, by
    prune_or_detach_shared_clans_before_deletion() to know what needs repointing/pruning.
    Doesn't mutate anything itself."""
    db = CACHE.db_manager
    if db is None:
        return []
    guild_id_str = str(guild_id)
    info: List[Dict[str, Any]] = []
    for clan in db.get_cwl_event_clans_sync(event_id):
        shared = db.get_cwl_shared_clan_sync(clan["clan_tag"], season)
        if shared is None:
            continue
        guilds = db.list_cwl_shared_clan_guilds_sync(shared["id"])
        other_guild_ids = [g["guild_id"] for g in guilds if g["guild_id"] != guild_id_str]
        info.append({
            "clan_tag": clan["clan_tag"],
            "shared_clan_id": shared["id"],
            "owner_guild_id": shared["owner_guild_id"],
            "other_guild_ids": other_guild_ids,
        })
    return info


async def _detach_or_prune_one_shared_clan(
    guild_id_str: str, shared_clan_id: int, clan_tag: str, season: str,
    owner_guild_id: str, other_guild_ids: List[str],
) -> None:
    """Shared detach/repoint/prune mechanics for ONE shared clan (2026-08-19 DRY refactor — this
    was byte-for-byte duplicated between prune_or_detach_shared_clans_before_deletion, below, and
    detach_guild_from_shared_clan_on_deactivation, further down, which only ever differed in how
    many shared clans they process and what triggers them; both callers already had docstrings
    calling the other a "narrower sibling," acknowledging the duplication).

    If other guilds remain attached, detach only THIS guild from cwl_shared_clan_guilds
    (repointing ownership to one of the remaining guilds first, if this guild was the owner) —
    the shared roster survives untouched. If this guild was the last one attached, the shared
    record itself (and its roster, via cascade) is pruned — nothing left for anyone to dangle
    from."""
    db = CACHE.db_manager
    if db is None:
        return
    if other_guild_ids:
        await asyncio.to_thread(db.remove_guild_from_shared_clan_sync, shared_clan_id, guild_id_str)
        if owner_guild_id == guild_id_str:
            new_owner_guild_id, resolution_method, new_owner_event_id = await resolve_cwl_clan_owner(
                clan_tag, season, [int(g) for g in other_guild_ids]
            )
            if new_owner_event_id is not None:
                await asyncio.to_thread(
                    db.repoint_cwl_shared_clan_owner_sync,
                    shared_clan_id, new_owner_guild_id, new_owner_event_id, resolution_method,
                )
    else:
        await asyncio.to_thread(db.delete_cwl_shared_clan_sync, shared_clan_id)


async def prune_or_detach_shared_clans_before_deletion(guild_id: int, event_id: int, season: str) -> None:
    """Delete-season guard (2026-08-15, data-loss fix confirmed with the project owner) — MUST
    run before delete_cwl_event_sync() for this event. cwl_shared_clans.owner_event_id
    deliberately has no ON DELETE CASCADE (see CWL_ROSTER_PLANNING_PLAN.md): without this
    pre-step, a guild deleting its own season would either dangle a shared clan's owner_event_id
    (if it was the owner and just left the cascade to do nothing) or, if the FK ever were
    CASCADE, silently wipe the shared roster out from under the OTHER guild with no warning.

    For each shared clan this event participates in: if other guilds remain attached, detach
    only THIS guild (repointing ownership to one of the remaining guilds first, if this guild
    was the owner) — the shared roster survives untouched. If this guild is the last one
    attached, the shared record itself (and its roster, via cascade) is pruned — nothing left
    for anyone to dangle from."""
    db = CACHE.db_manager
    if db is None:
        return
    guild_id_str = str(guild_id)

    shared_clan_info = await asyncio.to_thread(get_cwl_event_shared_clan_info_sync, event_id, guild_id, season)
    for info in shared_clan_info:
        await _detach_or_prune_one_shared_clan(
            guild_id_str, info["shared_clan_id"], info["clan_tag"], season,
            info["owner_guild_id"], info["other_guild_ids"],
        )


def _cleanup_local_pool_for_plain_clan_deactivation_sync(
    db: Any, guild_id: int, event_id: int, clan_tag: str, shared_clan_id: Optional[int] = None,
) -> List[str]:
    """The LOCAL-table counterpart of the SHARED-clan orphaned-preservation/stale-mirror cleanup
    in detach_guild_from_shared_clan_on_deactivation (see that function's own `shared is None`
    branch for why this exists — 2026-08-16 follow-up, live-testing feedback). A plain guest clan
    (never cross-guild shared) has no cwl_shared_clan_players table to read its roster from — the
    roster IS the local cwl_signups/cwl_assignments rows directly — so this reads those instead,
    but applies the exact same rule: a genuinely DELIBERATE placement (assignment_source==
    'admin_override' AND locked) is preserved untouched — it's already a real local
    cwl_assignments row, so simply not deleting it is enough — unlike the shared branch, nothing
    needs to be freshly materialized here, since there was never a separate shared table to
    mirror FROM in the first place.

    That placement does NOT have to point AT clan_tag itself (2026-08-19 fix, live bug report,
    project owner: "I dragged a StayCalm member to TheQCrew's roster and then I removed
    StayCalm... that member should have stayed but is gone now" — exactly the "Foreign-guest
    conversion" scenario detach_guild_from_shared_clan_on_deactivation's own top section already
    describes, verbatim spec: "a player of the guest clan in the guild clan's player roster
    becomes a guest player automatically"). A player who's a real CURRENT member of clan_tag but
    was manually drag-assigned into a DIFFERENT clan (one of the guild's own family clans, most
    commonly) still enters `candidate_tags` via their live clan_tag membership — the deliberate-
    placement check below must look at THEIR assignment wherever it actually points, not only at
    one scoped to clan_tag, or exactly this cross-assignment gets wrongly purged the moment
    clan_tag is removed.

    "Preserved untouched," precisely (2026-08-19 second fix, live bug report, project owner —
    correcting the framing above, verbatim: "assigning one of its own members to itself and then
    removing that clan again should not leave that manually assigned player in the guild's
    pool... the assigned to other guild case serves a different purpose, namely a player that is
    rightfully member of the current player pool (e.g. because he is a member of this guild) but
    is assigned to another guild's roster"): a deliberate placement pointing AT clan_tag itself is
    only preserved when clan_tag is genuinely shared (shared_clan_id is not None) AND the player's
    own LIVE current clan is NOT clan_tag — i.e. they're rightfully part of THIS guild's pool by
    some OTHER measure (a family member, an individually-invited guest, a linked account) who
    chose to be placed on another guild's roster. A player whose own current clan actually IS
    clan_tag is never "ours, assigned to theirs" — they're simply a real member of the (now
    foreign-again) clan itself, whose only connection to this guild's pool was clan_tag's now-
    ended guest invitation, so they fall through to the normal purge below like every other direct
    member, exactly as a never-shared plain clan's own self-assigned member already does. See
    CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md's "Assigned to other Guild" semantics section for
    the canonical statement of this rule.

    Discord-linked-account sweep (2026-08-16 follow-up, live-testing feedback, project owner's
    spec, verbatim): "not only the staycalm members were added to The QCrew's player pool but
    also [players from other, unrelated clans linked to the same Discord accounts]... I would say
    to keep things easy only direct members of a guest clan are added and removed and not their
    [linked-account] members... when removing a guest clan... we always remove all the players
    that are linked to the removed clan's discord users. There is one exception... When a discord
    user has linked players either in one of the guild's member clans or in another guest clan of
    the guild then these players should remain." Root cause this closes: Start Enrollment's own
    account-wide-linked-accounts expansion (guild_config.cwl_enrollment_include_all_linked_
    accounts) doesn't distinguish a Discord account that qualified via a FAMILY clan member from
    one that qualified via a GUEST clan member — either way, ALL of that account's other linked
    players get swept into the pool, even ones in completely unrelated clans. The prior version of
    this cleanup only ever considered clan_tag's own DIRECT current members, so those swept-in alt
    accounts (never direct clan_tag members themselves) were silently left behind forever.

    Two DIFFERENT protection rules, deliberately NOT symmetric (2026-08-16 second follow-up,
    project owner's spec, verbatim, confirmed "guest clans only"): a Discord account with a
    linked player in one of the guild's own FAMILY clans is ALWAYS protected in full,
    unconditionally — family membership was never gated by the linked-accounts toggle on the
    add side either, so it isn't here. A Discord account with a linked player in ANOTHER
    currently-active GUEST clan is protected ONLY while guild_config.cwl_enrollment_include_all_
    linked_accounts is currently True — mirrors exactly what a fresh Start Enrollment run would
    produce for that other clan right now, given the CURRENT setting, not whatever was true when
    these players were originally added (the toggle may well have changed since). Either way, a
    player who is THEMSELVES a genuine DIRECT current member of a protective clan is always kept,
    toggle or not — that's their own independent membership, nothing to do with account linkage.

    "Kept" means their SIGNUP survives — a stale non-deliberate ASSIGNMENT specifically pointing
    at clan_tag is still cleared even for a protected player (2026-08-19 fix, live bug report,
    project owner: real family-clan members were stuck rendering as "Assigned to other Guild"
    forever after the guest clan their stale prior-CWL-history auto-assignment pointed at got
    removed — this cleanup already correctly refused to delete their pool membership, it just
    never cleared the resulting dangling pointer). See _clear_stale_assignment_if_any below.

    Candidate set: every CURRENT live member of clan_tag (catches auto_seeded visibility-only
    signups with no assignment at all) UNIONED with every player who already has a local
    cwl_assignments row pointing at clan_tag (catches someone who's since left clan_tag in-game
    but still has a stale local assignment from when they were a member) UNIONED with every
    player still on clan_tag's SHARED roster, if any, per shared_clan_id (2026-08-19 fix, live
    bug report, project owner: players he "never added... through the player guest invite
    feature" kept lingering as individually-removable guest players after removing a shared
    clan — traced to sync_cwl_shared_clan_roster_to_local_pools(), which mirrors every
    shared-roster player into this guild's own local cwl_signups as a source='guest_invite'
    placeholder the moment the shared clan is added, but writes no local cwl_assignments row and
    no origin_shared_clan_id; a player who's since left clan_tag in real life falls out of BOTH
    the live-membership and the local-assignment candidate sources above and becomes permanently
    invisible to this cleanup without this third source) UNIONED with every OTHER player sharing
    a Discord account with one of clan_tag's own direct members (the sweep)."""
    current_members = db.get_current_clan_members_sync([clan_tag])
    candidate_tags = {m["player_tag"] for m in current_members}
    # Every local assignment in this event, keyed by player_tag regardless of destination — used
    # below both to extend the candidate set (only the clan_tag-pointing subset, same as before)
    # and to check whether a candidate has a deliberate placement ANYWHERE (not necessarily at
    # clan_tag — see this function's own docstring for why that distinction matters).
    all_assignments_by_tag = {a["player_tag"]: a for a in db.get_cwl_assignments_sync(event_id)}
    candidate_tags.update(tag for tag, a in all_assignments_by_tag.items() if a["assigned_clan_tag"] == clan_tag)
    if shared_clan_id is not None:
        candidate_tags.update(p["player_tag"] for p in db.get_cwl_shared_clan_players_sync(shared_clan_id))
    if not candidate_tags:
        return []

    family_clan_tags = set(resolve_guild_member_clan_tags(guild_id))
    other_active_guest_clan_tags = {
        c["clan_tag"] for c in db.get_cwl_event_clans_sync(event_id)
        if c.get("participating", 1) and c["clan_tag"] != clan_tag and c["clan_tag"] not in family_clan_tags
    }
    protective_clan_tags = family_clan_tags | other_active_guest_clan_tags
    include_linked_accounts = bool(
        CACHE.server_config.get(str(guild_id), {}).get("cwl_enrollment_include_all_linked_accounts", False)
    )

    discord_id_by_tag: Dict[str, Optional[str]] = {m["player_tag"]: m["discord_id"] for m in current_members}
    discord_ids = {did for did in discord_id_by_tag.values() if did}
    if discord_ids:
        for p in db.get_all_players_for_discord_ids_sync(list(discord_ids)):
            candidate_tags.add(p["player_tag"])
            discord_id_by_tag.setdefault(p["player_tag"], p["discord_id"])

    current_clan_by_tag = db.get_current_clan_tags_for_players_sync(list(candidate_tags))

    # A discord_id is "family-protected" if ANY of its linked players directly sits in one of
    # the guild's own family clans right now; "guest-protected" if any sits directly in another
    # currently-active guest clan. Computed once, up front, so every candidate sharing that
    # account gets the same answer regardless of iteration order.
    family_protected_discord_ids = set()
    guest_protected_discord_ids = set()
    for tag in candidate_tags:
        did = discord_id_by_tag.get(tag)
        if not did:
            continue
        current_clan = current_clan_by_tag.get(tag)
        if current_clan in family_clan_tags:
            family_protected_discord_ids.add(did)
        elif current_clan in other_active_guest_clan_tags:
            guest_protected_discord_ids.add(did)

    def _clear_stale_assignment_if_any(tag: str, assignment: Optional[Dict[str, Any]]) -> None:
        # A protected player's POOL MEMBERSHIP (signup) always survives — but if their assignment
        # is a non-deliberate one that happens to point AT clan_tag (the one being removed), that
        # pointer is now stale: that column no longer exists. Left uncleared, they render as
        # "Assigned to other Guild" forever instead of correctly falling back to Unassigned
        # (2026-08-19 fix, live bug report, project owner: "the qcrew members were falsely
        # auto-assigned to staycalm... after removing staycalm the error becomes obvious" — the
        # actual root cause was resolve_prior_cwl_assignments/auto-assign placing a REAL family-
        # clan member into a guest/shared clan's column based on stale prior-CWL-attack history;
        # this cleanup already correctly refused to delete their pool membership for it, it just
        # never cleared the resulting dangling pointer once that clan left the roster).
        if assignment is not None and assignment["assigned_clan_tag"] == clan_tag:
            db.delete_cwl_assignment_sync(event_id, tag)

    purged_tags: List[str] = []
    for tag in candidate_tags:
        assignment = all_assignments_by_tag.get(tag)
        if (
            assignment is not None
            and assignment["assignment_source"] == "admin_override"
            and assignment["locked"]
            and (
                assignment["assigned_clan_tag"] != clan_tag
                or (shared_clan_id is not None and current_clan_by_tag.get(tag) != clan_tag)
            )
        ):
            continue  # a genuine, deliberate drag-and-drop placement — preserved as-is UNLESS it
                      # points AT clan_tag itself AND (clan_tag was never actually shared with
                      # anyone, OR this player is themselves a genuine CURRENT/direct member of
                      # clan_tag). "Assigned to other Guild" exists for exactly one purpose
                      # (2026-08-19, project owner's spec, verbatim: "the assigned to other guild
                      # case serves a different purpose namely that a player that is rightfully
                      # member of the current player pool (e.g. because he is a member of this
                      # guild) but is assigned to another guilds roster, those are the player that
                      # should appear in the assigned to other guild category") — a player who
                      # genuinely belongs in THIS guild's pool (current_clan_by_tag.get(tag) !=
                      # clan_tag, e.g. one of this guild's own family-clan members) but was
                      # deliberately drag-assigned INTO a clan another guild manages. A player
                      # whose own LIVE current clan actually IS clan_tag is the opposite: a real
                      # member of the (now-removed) foreign clan itself, who was only ever in this
                      # guild's pool because that clan was temporarily guest-invited — dragging
                      # them into their OWN clan's column doesn't make them "ours assigned to
                      # theirs," so nothing here is worth preserving once that guest invitation
                      # ends, shared or not (live bug report, project owner: he drag-assigned a
                      # real member of a guest clan — first the never-shared Hohenloher Land/Akaza
                      # case, then the SAME symptom again for a genuinely shared clan/StayCalm's
                      # own member "STY - Basement" — into that clan's own column, then removed
                      # the clan; the player stayed behind either as an unexplained "Assigned to
                      # other Guild" entry or lingering in Remove Guest Players instead of being
                      # purged like every other real member of the removed clan). Unconditionally
                      # true for a placement pointing at any OTHER clan regardless of clan_tag's
                      # own shared status or the player's own current clan (the classic
                      # cross-assignment/"foreign-guest conversion" case — see
                      # test_remove_guest_clan_preserves_a_member_drag_assigned_into_a_family_clan,
                      # tests/unit/test_cwl_clan_ownership.py).
        if current_clan_by_tag.get(tag) in protective_clan_tags:
            _clear_stale_assignment_if_any(tag, assignment)
            continue  # a genuine DIRECT current member of a family/other-active-guest clan — always kept
        did = discord_id_by_tag.get(tag)
        if did and did in family_protected_discord_ids:
            _clear_stale_assignment_if_any(tag, assignment)
            continue  # linked to a genuine family-clan member — unconditional, toggle-independent
        if did and include_linked_accounts and did in guest_protected_discord_ids:
            _clear_stale_assignment_if_any(tag, assignment)
            continue  # linked to another active guest clan's direct member, AND the expansion
                      # setting is currently on — matches what a fresh add would produce right now
        db.delete_cwl_assignment_sync(event_id, tag)
        db.delete_cwl_signup_sync(event_id, tag)
        purged_tags.append(tag)

    # Returned so the ASYNC caller can retract these players' now-dangling enrollment DMs
    # (2026-08-21, tracker #0011). It can't be done here: this is a plain sync function run via
    # asyncio.to_thread, while cleanup_stale_cwl_enrollment_dms() is async and needs a live bot.
    logging.info(
        f"[CWL-POOL-CLEANUP] guild={guild_id} event={event_id} clan={clan_tag} deactivated: "
        f"purged={len(purged_tags)} kept={len(candidate_tags) - len(purged_tags)} "
        f"(of {len(candidate_tags)} candidate tag(s))"
    )
    return purged_tags


async def detach_guild_from_shared_clan_on_deactivation(guild_id: int, event_id: int, season: str, clan_tag: str) -> None:
    """Narrower sibling of prune_or_detach_shared_clans_before_deletion, same detach/repoint/prune
    logic, but for a different trigger (2026-08-15, live-testing feedback): a guild turning a
    previously-participating SHARED clan back OFF via a normal Configure Participating Clans save
    (unchecking it, not deleting the whole season). Without this, a guild that deactivated a
    shared clan stayed listed in cwl_shared_clan_guilds forever — a confusing, wrong "still
    sharing" entry for eviction-target lists and notifications even though this guild opted out —
    and if it happened to be the OWNER, an inactive/opted-out guild kept blocking legitimate
    eviction rights indefinitely, with nobody actually managing the clan's canonical settings.

    Called from handle_post_clan_config (web_bridge.py) for every clan newly turning
    participating=False this save. Re-adding the same clan later (ensure_cwl_clan_sharing) finds
    no existing attachment (this function already detached it) and correctly treats it as a fresh
    join — repoint-on-detach here means a stale/inactive owner can never block that re-join.

    Foreign-guest conversion (2026-08-15, project owner's spec, verbatim: "in the moment the
    guest clan is removed that assigned players remain in their rosters even in that of the
    guild clan... a player of the guest clan in the guild clan's player roster becomes a guest
    player automatically"): before detaching, find this clan's REAL current members who are
    currently cross-assigned into one of THIS guild's own (private, non-shared) clans — they
    were removed from the shared roster back when that cross-assignment happened (see
    handle_post_cwl_enrollment_assign's "remove from other shared clan" step), so there's nothing
    left in cwl_shared_clan_players to find them by; the only remaining signal is "real current
    member of clan_tag" cross-referenced against this guild's own cwl_assignments. Each one gets
    flipped to a guest signup (mark_cwl_signup_as_shared_clan_guest_sync), stamped with this
    shared clan's id so a later reassignment by the clan's real owning guild
    (handle_post_cwl_enrollment_assign's purge hook) can find and remove them.

    Everything through computing `other_guild_ids` runs as one asyncio.to_thread() hop
    (_detach_guild_from_shared_clan_on_deactivation_sync, 2026-08-16, Pitfall 26,
    COPILOT_PITFALLS_COOKBOOK.md) — this function has no `await` before that point today, so
    bundling preserves that same atomicity instead of introducing new interleaving windows a
    per-line wrap would; the tail (repoint-or-prune, delegated to _detach_or_prune_one_shared_clan
    — 2026-08-19 DRY refactor, shared with prune_or_detach_shared_clans_before_deletion) was
    never atomic with the rest regardless, since it has a real `await resolve_cwl_clan_owner()`
    of its own."""
    db = CACHE.db_manager
    if db is None:
        return
    pre = await asyncio.to_thread(
        _detach_guild_from_shared_clan_on_deactivation_sync, db, guild_id, event_id, season, clan_tag,
    )
    if pre is None:
        return
    shared, other_guild_ids, guild_id_str = pre
    await _detach_or_prune_one_shared_clan(
        guild_id_str, shared["id"], clan_tag, season, shared["owner_guild_id"], other_guild_ids,
    )


def _detach_guild_from_shared_clan_on_deactivation_sync(
    db: Any, guild_id: int, event_id: int, season: str, clan_tag: str,
) -> Optional[Tuple[Dict[str, Any], List[str], str]]:
    """Synchronous unit of work for detach_guild_from_shared_clan_on_deactivation() — see that
    function's own comment for why this is one atomic asyncio.to_thread() hop. Returns None for
    both of the function's early-return cases (never shared, or not actually attached); otherwise
    (shared, other_guild_ids, guild_id_str) for the caller's post-await tail."""
    shared = db.get_cwl_shared_clan_sync(clan_tag, season)
    if shared is None:
        # Plain (never cross-guild-shared) guest clan — SUPERSEDED 2026-08-18 (rule f,
        # CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md, project owner's spec): unchecking a guest
        # clan is now purely cosmetic — it only removes the clan from the guild's active roster,
        # its players stay in the player pool untouched. This used to call
        # _cleanup_local_pool_for_plain_clan_deactivation_sync() here automatically on every
        # deactivation (2026-08-16 follow-up, live-testing feedback, verbatim: "I removed
        # staycalm but their players were not removed from the qcrew's player pool as it should
        # have... we fixed this earlier this day and now it's back") — that destructive cleanup
        # logic (preserve a deliberate placement, remove everything machine-seeded) is exactly
        # right, it's just no longer triggered by a plain checkbox uncheck+Save. It now runs only
        # from the explicit "Remove" button (remove_cwl_guest_clan, this file — which calls
        # _cleanup_local_pool_for_plain_clan_deactivation_sync() unconditionally itself, not just
        # for this never-shared case; see that function's own docstring for the 2026-08-18 bug
        # this fixed for a SHARED guest clan specifically).
        return
    guild_id_str = str(guild_id)
    guilds = db.list_cwl_shared_clan_guilds_sync(shared["id"])
    if not any(g["guild_id"] == guild_id_str for g in guilds):
        return  # not actually attached — nothing to do (defensive)

    current_members = db.get_current_clan_members_sync([clan_tag])
    # Computed unconditionally (not just inside the `if current_members:` block below) — reused
    # further down by the orphaned-assignment-preservation loop's own membership check, which
    # must still run correctly even when clan_tag currently has zero live members.
    members_by_tag = {m["player_tag"]: m for m in current_members}
    if current_members:
        my_assignments = {a["player_tag"]: a["assigned_clan_tag"] for a in db.get_cwl_assignments_sync(event_id)}
        for tag, member in members_by_tag.items():
            assigned_clan = my_assignments.get(tag)
            if assigned_clan is None or assigned_clan == clan_tag:
                continue  # not cross-assigned into one of my OWN other clans
            db.mark_cwl_signup_as_shared_clan_guest_sync(
                event_id, tag, member["player_name"], member["discord_id"], shared["id"]
            )

    # Orphaned-assignment preservation (2026-08-16, live-testing feedback, project owner's spec):
    # the OTHER direction — one of THIS guild's own players (family member or otherwise) who was
    # drag-assigned INTO the shared clan itself lives only in cwl_shared_clan_players, never in
    # this guild's own cwl_assignments (see handle_post_cwl_enrollment_assign's shared-destination
    # branch, which deliberately clears any local assignment row). Once this clan stops being a
    # participating column here, that player would otherwise vanish from the board entirely — not
    # even falling back to Unassigned — since nothing in the payload builder would ever resolve
    # their assignment again. Mirroring every currently-confirmed shared-roster player into a
    # local cwl_assignments row (still pointing at clan_tag, now a "column that doesn't exist"
    # tag) reuses exactly the same mechanism a plain, never-shared guest clan already gets for
    # free when deactivated — its stale local assignment simply survives untouched — and the
    # frontend's new "Assigned to other Guild" column (enrollmentBoard.ts) is what actually
    # surfaces it instead of leaving it silently invisible. Reuses
    # mark_cwl_signup_as_shared_clan_guest_sync for the signup half too (not just the "cross-
    # assigned into MY other clan" case above) — deliberately uniform treatment regardless of
    # whether this particular player is really this guild's own family member or a stranger:
    # stamping origin_shared_clan_id means purge_orphaned_shared_clan_guests_sync will correctly
    # clean up this guild's own now-stale local copy too, the moment the clan's real owning guild
    # ever reassigns this same player away from clan_tag on ITS OWN board — the shared roster
    # itself is untouched by any of this (still exactly what get_cwl_shared_clan_players_sync
    # returns), only local mirrors are ever written here.
    #
    # Scoped to source == 'admin_override' ONLY (2026-08-16 follow-up, live-testing feedback,
    # project owner's spec, verbatim: "all players from that guest clan that are not already
    # assigned to a member clan player roster should be removed from the player pool
    # completely... only [the one deliberately drag-assigned player] should have stayed"). This
    # ONLY controls which players get a NEW local mirror row written here — it does NOT delete
    # anything for anyone else; see the SUPERSEDED note below for why.
    #
    # SUPERSEDED 2026-08-19 (rule f, CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md, live bug
    # report: "Members vanished from player pool after uncheck for StayCalm" — a genuinely
    # cross-guild-shared clan). This loop used to ALSO delete every non-admin_override shared-
    # roster player's existing local cwl_signups/cwl_assignments row here (a "stale local-mirror
    # cleanup" — see git history for the full 2026-08-16 rationale, itself a fix for a real bug
    # at the time) — correct BEFORE rule f existed, but rule f (2026-08-18) requires a mere
    # uncheck+Save to be purely cosmetic for the player pool here too, exactly like the plain-clan
    # branch above (`shared is None`). That deletion is gone: a non-admin_override player's local
    # mirror (if they already have one, from while this clan was still an active, participating
    # column) is now left completely untouched by this function. Deletion of the local pool only
    # ever happens via the explicit "Remove" button (remove_cwl_guest_clan, below) — which itself
    # still preserves a genuine deliberate placement (see the assignment_source write below).
    #
    # Mirror written as assignment_source="admin_override"/locked=True — matching the SAME live
    # values a real drag-and-drop placement carries, not a softer "orphaned_on_detach"/locked=False
    # marker (2026-08-19 fix, project owner's spec, verbatim, confirmed explicitly: "'assigned
    # players remain in their rosters... becomes a guest player automatically' even when the clan
    # is removed" — NOT just on a mere uncheck). _cleanup_local_pool_for_plain_clan_deactivation_
    # sync (called unconditionally by remove_cwl_guest_clan right after this) only ever preserves
    # a row with exactly these values, so writing anything softer here meant Remove silently threw
    # the "preserved" placement away again the instant after this loop wrote it — this now
    # actually survives both an Uncheck AND a subsequent (or immediate) Remove, matching the spec.
    # Excludes a shared_player who is themselves a genuine CURRENT/direct member of clan_tag
    # (2026-08-19 fix, live bug report, project owner: "STY - Basement" — a real StayCalm member,
    # deliberately drag-assigned into StayCalm's OWN column — still got mirrored here and rendered
    # as "Assigned to other Guild" even on a mere UNCHECK, before Remove's own cleanup ever had a
    # chance to run; project owner's correction of the framing, verbatim: "the assigned to other
    # guild case serves a different purpose, namely a player that is rightfully member of the
    # current player pool... but is assigned to another guild's roster" — a real direct member of
    # clan_tag itself was never "rightfully in THIS guild's pool" to begin with, just a foreign
    # player along for clan_tag's guest invitation; reuses members_by_tag, already computed above
    # for the OTHER direction's own membership check). This is the single write-time source of
    # this mirror — fixing it here (not just in _cleanup_local_pool_for_plain_clan_deactivation_
    # sync's later purge) matters because that cleanup step never even runs on a plain Uncheck
    # (rule f, SUPERSEDED note above) — without this, a mere Uncheck (not a full Remove) would
    # already show clan_tag's own real members as "Assigned to other Guild," which is wrong
    # regardless of which action triggered it.
    for shared_player in db.get_cwl_shared_clan_players_sync(shared["id"]):
        tag = shared_player["player_tag"]
        if shared_player["assigned"] and shared_player["source"] == "admin_override" and tag not in members_by_tag:
            db.mark_cwl_signup_as_shared_clan_guest_sync(
                event_id, tag, shared_player["player_name"], shared_player["dmed_discord_id"], shared["id"]
            )
            db.upsert_cwl_assignment_sync(event_id, tag, clan_tag, assignment_source="admin_override", locked=True)

    other_guild_ids = [g["guild_id"] for g in guilds if g["guild_id"] != guild_id_str]
    return shared, other_guild_ids, guild_id_str


async def remove_cwl_guest_clan(guild_id: int, event_id: int, season: str, clan_tag: str) -> None:
    """Full removal of a guest clan from the season — rule f's "Remove" button (2026-08-18,
    CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md), called from web_bridge.py's
    handle_post_cwl_guest_clan_remove. Unchecking a guest clan (a normal Configure Participating
    Clans save) is now purely cosmetic — see detach_guild_from_shared_clan_on_deactivation's own
    docstring — this function is the only thing that still does the full destructive purge that
    used to run automatically on every uncheck.

    Bug fixed 2026-08-18 (live-tested in DEV, project owner's report: "Remove guest clan didn't
    remove the players from the player pool"): this used to run the local-pool cleanup only for a
    plain (never cross-guild-shared) clan, in an if/else against the shared-detach call — for a
    SHARED guest clan it ran ONLY detach_guild_from_shared_clan_on_deactivation, and the guild's
    own local cwl_signups/cwl_assignments rows for this clan's real current members were never
    touched at all in that branch. Fixed by always running the local cleanup afterward.

    A genuine admin_override placement into a DIFFERENT clan than the one being removed always
    survives, Uncheck OR Remove alike (2026-08-19 fix, project owner's spec, verbatim, confirmed
    explicitly: "'assigned players remain in their rosters... becomes a guest player
    automatically' even when the clan is removed") — detach_guild_from_shared_clan_on_
    deactivation's shared-roster mirror-write now stamps the same assignment_source=
    "admin_override"/locked=True values a real drag-and-drop placement carries (not a softer
    marker), which is exactly what _cleanup_local_pool_for_plain_clan_deactivation_sync (called
    unconditionally right after) preserves — so it's never purged out from under itself, on
    Uncheck or Remove, shared clan or plain. A placement pointing AT clan_tag itself is a
    different case (2026-08-19, second fix, live bug report — "STY - Basement"/Akaza): see
    _cleanup_local_pool_for_plain_clan_deactivation_sync's own docstring and
    CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md's "Assigned to other Guild" semantics section —
    that survives only when the player is themselves NOT a genuine current member of clan_tag,
    since a real member of the clan being removed was never "rightfully in this guild's pool" to
    begin with.

    Order: the shared-detach step (if applicable) runs FIRST — it owns the cross-guild
    bookkeeping (detach from cwl_shared_clan_guilds, repoint ownership, foreign-guest
    conversion) and may itself write new local rows; the local-pool cleanup then runs
    unconditionally afterward, so it sees and correctly purges/preserves whatever that step just
    left behind, on top of every genuine current member of clan_tag. Finally the cwl_event_clans
    row itself is deleted entirely (guest clans have no roster_size/tier_order/cwl_start_at worth
    preserving across a remove/re-add cycle, unlike a family clan's plain participating toggle).

    Callers must reject a family-clan tag before calling this (defense in depth — the caller,
    not this function, owns that check, matching this codebase's usual "fail closed at the
    endpoint" convention for destructive actions).

    shared_clan_id is looked up ONCE here and threaded into the local cleanup too (2026-08-19
    fix, live bug report, project owner: players who were never manually invited kept lingering
    as individually-removable guest players after a shared clan's removal — traced to
    sync_cwl_shared_clan_roster_to_local_pools(), which mirrors every shared-roster player into
    this guild's own local cwl_signups as a source='guest_invite' placeholder the moment the
    shared clan is added (so the local pool has something to show), but writes no
    cwl_assignments row and no origin_shared_clan_id — the ONLY two things
    _cleanup_local_pool_for_plain_clan_deactivation_sync's candidate set otherwise notices. A
    player who's since left the shared clan in real life (their live current_clan_tag no longer
    matches clan_tag either) becomes fully invisible to that candidate set and the mirror row
    sits there forever. See _cleanup_local_pool_for_plain_clan_deactivation_sync's own docstring
    for the actual fix."""
    db = CACHE.db_manager
    if db is None:
        return
    shared = await asyncio.to_thread(db.get_cwl_shared_clan_sync, clan_tag, season)
    if shared is not None:
        await detach_guild_from_shared_clan_on_deactivation(guild_id, event_id, season, clan_tag)
    purged_tags = await asyncio.to_thread(
        _cleanup_local_pool_for_plain_clan_deactivation_sync, db, guild_id, event_id, clan_tag,
        shared["id"] if shared is not None else None,
    )
    await asyncio.to_thread(db.delete_cwl_event_clan_sync, event_id, clan_tag)

    # Retract the purged players' now-dangling enrollment DMs (2026-08-21, tracker #0011). Their
    # cwl_signups row is gone, so clicking Confirm/Opt Out would report "this sign-up is no longer
    # valid (the season may have been deleted)" — misleading, since the season is very much alive;
    # only their clan left the roster. Delete-Season already does exactly this
    # (CwlDeleteSeasonConfirmView._on_confirm, ui_cwl_roster.py); this path never did.
    # Deliberately scoped to purged_tags: players the cleanup PRESERVED keep their pool
    # membership, so their DM is still live and must not be retracted.
    if purged_tags:
        await _retract_enrollment_dms_for_tags(event_id, purged_tags, context=f"clan {clan_tag} removed")


def purge_orphaned_shared_clan_guests_sync(shared_clan_id: int, player_tag: str) -> None:
    """The other half of the foreign-guest conversion (2026-08-15, project owner's spec,
    verbatim: "only when that exact guest player is re-assigned by his owning guild... this
    player should be removed from the guild clan's player roster and player pool"). Called from
    handle_post_cwl_enrollment_assign (web_bridge.py) every time a player is removed from a
    shared clan's roster — regardless of which guild did it or where the player went instead;
    the point is simply that they're no longer part of that shared clan's roster, so any OTHER
    guild's foreign-guest placement that was only legitimized by them having been on it needs to
    go too. Finds every cwl_signups row anywhere that traces back to this exact shared clan for
    this exact player (mark_cwl_signup_as_shared_clan_guest_sync stamped them) and deletes both
    that signup and its assignment outright — "removed from the roster AND the pool," not merely
    unassigned. A no-op for the overwhelming majority of removals (nobody ever cross-assigned
    this player into a foreign clan in the first place)."""
    db = CACHE.db_manager
    if db is None:
        return
    for row in db.find_cwl_signups_by_origin_shared_clan_sync(shared_clan_id, player_tag):
        db.delete_cwl_assignment_sync(row["event_id"], player_tag)
        db.delete_cwl_signup_sync(row["event_id"], player_tag)


async def evict_guild_from_shared_clan(
    acting_guild_id: int, target_guild_id: int, clan_tag: str, season: str
) -> Dict[str, Any]:
    """Owner-only eviction (2026-08-15, project owner's spec: "the admins of the owner guild
    should be able to remove the clan from the other guild's clan roster") — removes
    target_guild_id's participation in a shared clan. Gated to the CURRENT owner guild only;
    callers must already have verified acting_guild_id's admin permission separately (this
    function only checks the *ownership* half, not general admin-ness — see
    handle_post_cwl_shared_clan_evict, web_bridge.py, for the full gate).

    Returns {"ok": bool, "error": Optional[str]} — error is one of 'not_shared', 'not_owner',
    'cannot_evict_owner' (the owner can't evict themselves — see prune_or_detach_shared_clans_
    before_deletion / delete-season instead, which is the real "I want out" path for an owner).

    This whole function is pure sync DB work despite the `async def` (no `await` in the body) —
    bundled into one asyncio.to_thread() hop (2026-08-16, Pitfall 26, COPILOT_PITFALLS_COOKBOOK.md)."""
    db = CACHE.db_manager
    if db is None:
        return {"ok": False, "error": "no_database"}
    return await asyncio.to_thread(_evict_guild_from_shared_clan_sync, db, acting_guild_id, target_guild_id, clan_tag, season)


def _evict_guild_from_shared_clan_sync(
    db: Any, acting_guild_id: int, target_guild_id: int, clan_tag: str, season: str,
) -> Dict[str, Any]:
    shared = db.get_cwl_shared_clan_sync(clan_tag, season)
    if shared is None:
        return {"ok": False, "error": "not_shared"}
    if shared["owner_guild_id"] != str(acting_guild_id):
        return {"ok": False, "error": "not_owner"}
    if str(target_guild_id) == shared["owner_guild_id"]:
        return {"ok": False, "error": "cannot_evict_owner"}

    target_event = db.get_cwl_event_sync(str(target_guild_id), season)
    db.remove_guild_from_shared_clan_sync(shared["id"], str(target_guild_id))
    if target_event is not None:
        db.deactivate_cwl_event_clan_sync(target_event["id"], clan_tag)
    return {"ok": True, "error": None}


def _seed_prior_cwl_assignments_sync(
    guild_id: int, event_id: int, season: str, prior_assignments: Dict[str, str],
) -> None:
    """Synchronous unit of work for start_cwl_enrollment()'s auto-assignment seed loop — see that
    function's own comment for why this is one atomic asyncio.to_thread() hop."""
    for player_tag, target_clan_tag in prior_assignments.items():
        assign_cwl_player_sync(
            guild_id, event_id, season, player_tag, target_clan_tag,
            source="auto_assigned", assignment_source="suggested", signup_source="auto_assigned",
            locked=False, deliberate=False,
        )


async def start_cwl_enrollment(guild_id: int, season: str) -> Dict[str, Any]:
    """Per-(guild_id, season)-serialized wrapper around the real implementation below — see
    _enrollment_locks' comment at the top of this module for why. Concurrent calls for the SAME
    guild+season queue up rather than overlapping; the second one then re-reads the event and
    correctly bails with error='not_draft', because the first run has by then written the
    draft -> signup_open transition. Different guilds/seasons are unaffected."""
    lock = _enrollment_locks.setdefault((str(guild_id), season), asyncio.Lock())
    async with lock:
        return await _start_cwl_enrollment_locked(guild_id, season)


async def _start_cwl_enrollment_locked(guild_id: int, season: str) -> Dict[str, Any]:
    """The single "Start Enrollment" admin action (CWL_ROSTER_PLANNING_PLAN.md Phase 2): seeds
    cwl_signups from the participating clans' *current* membership, sends the confirm/opt-out DM
    blast to every resolved account, and transitions the event draft -> signup_open. Re-fetches
    the event fresh by guild_id+season rather than trusting a caller-held event dict/id, matching
    the re-read discipline used everywhere else in this feature for actions gated behind a
    confirmation step.

    Corrected 2026-08-10 (live-tested in DEV): the original design seeded from last season's CWL
    war-attacker history (get_previous_cwl_participants_sync, since removed) — a clan with no
    tracked CWL wars yet (new to the bot, or simply hasn't played CWL before) seeded zero signups
    even though it has real, known members today. Now seeds from get_current_clan_members_sync()
    (user_players.current_clan_tag) instead — "who's actually in this clan right now."

    Account-wide expansion (guild_config.cwl_enrollment_include_all_linked_accounts, 2026-08-15,
    project owner's spec, off by default): once the clan-scoped participant list above is
    resolved, if the guild has opted in, every Discord account represented in it also has its
    OTHER linked players pulled in — regardless of which clan those play for, including one this
    guild has never added to its own family (tracked only via a different guild, or a bare
    channel subscription). Example: an account with Player A in a participating clan and Player
    B in some unrelated clan gets both seeded once this is on, where only Player A would be
    without it. This only expands the *signup pool* for THIS event — the auto-assignment seed
    (resolve_prior_cwl_assignments, below) is intentionally left untouched and still only draws
    from resolve_guild_member_clan_tags()'s family-wide pool, so an expanded-in account always
    starts Unassigned on the board rather than being silently auto-assigned somewhere.

    Returns a summary dict the caller renders back to the admin: ok, error (reason string if not
    ok), seeded (signup rows created), contacted (DMs actually sent), skipped_optout,
    skipped_unlinked, skipped_dm_guard.

    DM safety toggle (operational directive, CWL_ROSTER_PLANNING_PLAN.md, 2026-08-10, extended
    2026-08-14, extended again 2026-08-15): while CONFIG.cwl_dm_restrict_to_admin is True, only
    two kinds of recipient are actually DMed — every other resolved recipient is counted in
    skipped_dm_guard instead of contacted:
      1. CONFIG.server_admin's own Discord account, always (DEV and PROD alike).
      2. Anyone enrolled in CACHE.testers (the /admin MANAGE_TESTERS allowlist) — but only when
         CONFIG.is_dev_mode is False. Testers exist to validate the real message on a real PROD
         send without blasting the whole guild; in DEV, CACHE.testers is ignored and only
         server_admin gets DMed, same as before testers existed — otherwise a DEV host (which
         every dev machine runs by default) would DM real testers' accounts for routine local
         testing that was never meant to reach them.
    This lets the whole flow be exercised live — in DEV or PROD — without risking a DM blast to
    real clan members while the feature is still being built. The toggle is independent of
    CONFIG.is_dev_mode (set separately per host from the shared .env file) precisely so it can
    also be enabled on PROD while live-testing there.
    """
    from qapbot.config import CONFIG

    summary: Dict[str, Any] = {
        "ok": False, "error": None, "seeded": 0, "contacted": 0, "assigned": 0,
        "skipped_optout": 0, "skipped_unlinked": 0, "skipped_dm_guard": 0,
        # rule h (2026-08-18) — a candidate already DMed for this season by ANY guild is counted
        # here instead of "contacted"; their signup row is still seeded with their real global
        # status (see the bulk lookup above signups_to_create), just never re-DMed.
        "skipped_already_dm_globally": 0,
        # Cross-guild shared-clan results (2026-08-15), one entry per participating clan that
        # turned out to already be claimed by another guild for this season — the caller
        # (ui_cwl_roster.py's Start Enrollment callback) uses this to notify the admin and fire
        # notify_cwl_clan_shared(). Empty for the overwhelming majority of runs.
        "shared_clans": [],
        # Per-recipient DM outcomes (2026-08-18, item 3 of the enrollment redesign) — populated
        # by _send_cwl_enrollment_dm_batch(). "blocked": recipient has DMs closed / blocked the
        # bot / couldn't be fetched — retrying won't help. "failed": a transient Discord error
        # that didn't recover after DM_SEND_MAX_RETRIES attempts (cache_manager.py). Both are
        # player_name (or player_tag) strings, rendered back to the admin in the Start
        # Enrollment summary so they know who to follow up with.
        "blocked": [],
        "failed": [],
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

    # Cross-guild shared-clan check (2026-08-15, project owner's spec) — Start Enrollment is the
    # second of the two trigger points (the first is handle_post_clan_config's guest-clan add,
    # web_bridge.py): a clan this guild has configured as participating might already be claimed
    # by another guild for this exact season, e.g. this guild is the clan's real home but some
    # other guild found and guest-invited it first. ensure_cwl_clan_sharing() is a no-op (and
    # cheap — one indexed lookup) for the overwhelming majority of clans that aren't shared with
    # anyone; only genuinely-shared clans get the ownership-resolution/notification path.
    for clan_tag in participating_clan_tags:
        sharing_result = await ensure_cwl_clan_sharing(guild_id, event["id"], season, clan_tag)
        if sharing_result is not None:
            summary["shared_clans"].append({"clan_tag": clan_tag, **sharing_result})

    # rule b (2026-08-18, CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md, project owner's spec:
    # "when a guild starts a new season ALL memebrs of all clans should be put to the player
    # pool regardless of participation status of each clan") — the seed/DM candidate set is the
    # guild's WHOLE clan family (resolve_guild_member_clan_tags, regardless of each clan's
    # participating toggle this season) unioned with every clan ever configured for this event —
    # guest or family, participating or not (all_clans, already fetched above). The union with
    # all_clans matters for a guest clan invited then unchecked before Start Enrollment ever ran
    # (rule f: unchecking no longer purges the pool) — its members must still be pooled and DMed
    # here, same as rule d requires for a still-checked guest clan. participating_clan_tags
    # itself is UNCHANGED below — it still restricts the auto-assignment TARGET (only a clan
    # actually fielding a CWL roster this season is a valid assignment destination) and the
    # cross-guild sharing check above.
    pool_candidate_tags = resolve_cwl_pool_clan_tags_sync(guild_id, event["id"])
    # A guest clan added to the roster BEFORE Start Enrollment hits the same untracked-clan gap as
    # one added after it (2026-08-19, live bug report — see ensure_cwl_clan_membership_tracked's
    # own docstring): with no user_players rows, it contributes nothing to `participants` here and
    # its column comes up empty. No-op for every clan that's already tracked, which is all of them
    # in the common all-family-clans case.
    await ensure_cwl_clan_membership_tracked(pool_candidate_tags)
    participants = db.get_current_clan_members_sync(pool_candidate_tags)

    # Account-wide expansion (guild_config.cwl_enrollment_include_all_linked_accounts,
    # 2026-08-15, project owner's spec): a Discord account that already qualifies via one
    # participating-clan player also brings in its OTHER linked players, wherever they
    # currently play — even a clan this guild's own family has never included. Off by default
    # (existing guilds see no behavior change unless they opt in via /clan management's
    # cwl_settings screen).
    guild_config = CACHE.server_config.get(guild_id_str, {})
    if guild_config.get("cwl_enrollment_include_all_linked_accounts", False):
        qualifying_discord_ids = {
            p["discord_id"] for p in participants if p["discord_id"]
        }
        if qualifying_discord_ids:
            expanded = db.get_all_players_for_discord_ids_sync(list(qualifying_discord_ids))
            existing_tags = {p["player_tag"] for p in participants}
            for player in expanded:
                if player["player_tag"] not in existing_tags:
                    participants.append(player)
                    existing_tags.add(player["player_tag"])

    # rule h (2026-08-18, CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md, project owner's spec: "the
    # player has a global 'Got dm message already' attribute... Then the player accepts or
    # declines or is pending and that status is shown automatically in guild A's and guild B's
    # clan rosters") — before seeding, look up each candidate's existing GLOBAL status for this
    # season. A player already confirmed/declined via another guild's DM must show that real
    # status here from the moment this guild's own signup row is created, not a fresh 'pending'
    # that would contradict what the player already told a different guild.
    global_status_by_tag = await asyncio.to_thread(
        db.get_cwl_player_season_status_bulk_sync, [p["player_tag"] for p in participants], season,
    )

    signups_to_create: List[Dict[str, Any]] = []
    for participant in participants:
        if participant["cwl_permanent_optout"]:
            continue
        existing_global = global_status_by_tag.get(participant["player_tag"])
        signups_to_create.append({
            "player_tag": participant["player_tag"],
            "player_name": participant["player_name"],
            "dmed_discord_id": participant["discord_id"],
            "preferred_league_rank": participant["preferred_league_rank"],
            "source": "template_confirm",
            "status": existing_global["status"] if existing_global else "pending",
        })

    if signups_to_create:
        # asyncio.to_thread()-wrapped (2026-08-16, Pitfall 26, COPILOT_PITFALLS_COOKBOOK.md) — a
        # real bulk write, potentially every member across every participating clan.
        await asyncio.to_thread(db.bulk_create_cwl_signups_sync, event["id"], signups_to_create)
        summary["seeded"] = len(signups_to_create)

    # Recipients come from the SAME pool resolver the "Notify New Pool Members" button uses, run
    # after the seed above so it sees the complete pool (2026-08-20 fix — see
    # resolve_cwl_pool_dm_targets_sync's docstring for the bug that made sharing it necessary).
    # `participants` is handed over so its clan-member scan and account-wide expansion aren't
    # redone; the resolver adds the sources that scan structurally can't see (guest players,
    # shared-clan rosters) and owns both skip counts, so they're defined in exactly one place.
    pool = await asyncio.to_thread(
        resolve_cwl_pool_dm_targets_sync, guild_id, event["id"], season, participants,
    )
    dm_targets = pool["targets"]
    summary["skipped_optout"] = pool["skipped_optout"]
    summary["skipped_unlinked"] = pool["skipped_unlinked"]

    # Auto-assignment seed — the initial "who probably plays where" suggestion, from each
    # player's own last real CWL attack, anywhere (2026-08-14 redesign — see
    # resolve_prior_cwl_assignments()'s docstring). Runs once, here; every later change happens
    # via manual drag-and-drop on the Manage Enrollment board (assignment_source='admin_override',
    # locked=True there). Scoped to the guild's FULL member-clan roster, not just this season's
    # participating clans — the auto-assign target is still restricted to participating clans
    # (nothing else has a column), but the candidate pool it draws players from is every current
    # member of every guild clan, so a player whose real clan didn't opt into CWL this season (or
    # who transferred since their last CWL war) is still correctly resolved and assignable.
    #
    # Unioned with participating_clan_tags (2026-08-16, live-testing feedback: a guest clan added
    # before Start Enrollment ran got a completely empty column despite several of its own current
    # members having real prior-CWL-attack history for exactly that clan) — resolve_guild_member_
    # clan_tags() only ever returns the guild's own family by definition, which structurally
    # excludes every guest clan (the whole point of a guest clan is that it's NOT part of the
    # family). Without this union, a guest clan's own current members were never even in the
    # candidate pool to begin with, so resolve_prior_cwl_assignments() could never place them no
    # matter how much real history they had — this is the exact same union
    # _build_enrollment_payload() (web_bridge.py) already applies to its own player pool, for the
    # identical underlying reason.
    all_member_clan_tags = list(set(resolve_guild_member_clan_tags(guild_id)) | set(participating_clan_tags))
    all_members = db.get_current_clan_members_sync(all_member_clan_tags)
    current_member_tags = {p["player_tag"] for p in all_members}
    # resolve_prior_cwl_assignments() only ever resolves entries for the player_tags it was
    # given, so prior_assignments' keys are already a subset of current_member_tags — no extra
    # membership filter needed here (unlike the pre-2026-08-14 design, which queried clan
    # rosters independently of current membership and needed one).
    # Routed through the general assignment method, one player at a time (2026-08-16, live-testing
    # feedback, project owner's spec — see assign_cwl_player_sync's own docstring), replacing the
    # old direct bulk_create_cwl_assignments_sync call. Confirmed live-testing bug this fixes:
    # deleting and recreating a season's event re-ran this exact seed, which previously never
    # checked whether a candidate was already a CONFIRMED guest in a totally different shared
    # clan's roster elsewhere in this guild — they got silently double-booked purely because
    # their last real CWL attack happened to be for this event's target clan. The bulk path also
    # never branched on the target clan being shared at all (always wrote a plain local
    # cwl_assignments row even for a shared-clan target) — assign_cwl_player_sync now correctly
    # writes to cwl_shared_clan_players instead when that's the case, matching every other write
    # path in this feature.
    prior_assignments = resolve_prior_cwl_assignments(list(current_member_tags), participating_clan_tags)
    # A player's CURRENT clan wins over resolve_prior_cwl_assignments' stale "last real CWL
    # attack" history whenever that current clan is itself a valid, participating target AND the
    # history it's overriding points OUTSIDE the guild's own clan family (2026-08-19 fix, live bug
    # report, project owner: "when staycalm gets added during the very start of adding the new
    # season the theqcrew members get auto assigned to staycalm and not to the qcrew as they
    # should"). resolve_prior_cwl_assignments' own documented design ("assign to wherever they
    # last actually played, not wherever they're currently rostered") is still exactly right for a
    # player whose CURRENT clan isn't participating this season (or isn't tracked at all) — there's
    # nowhere better to put them, that's the whole reason history is consulted in the first place.
    # But for a player who is a genuine CURRENT member of a clan that IS participating, that's
    # unambiguously where they actually belong right now; some earlier season's history for a
    # totally different participating clan OUTSIDE the family (a guest clan — this override's
    # actual original bug) must never override that live fact.
    #
    # The family-membership guard (2026-08-20 fix, live bug report, project owner: a multi-clan
    # family's own player who genuinely played CWL for the family's OTHER clan (#2JYQ909PC, "The
    # Marines II") last season, then transferred to this family's #2J00L8YRU ("The Marines")
    # afterward — completely normal end-of-season churn — got redirected away from their accurate,
    # one-season-old history to their new current clan anyway, purely because that new clan also
    # happens to participate this season) is what the original fix's own docstring already called
    # out as the intended scope ("most commonly a guest clan") but the code never actually encoded:
    # it redirected on ANY participating current clan, not specifically a foreign/guest one. History
    # that already points at one of the family's OWN clans is real, current-family CWL history, not
    # stale foreign data — it must be left alone; only history pointing outside the family gets
    # redirected to the player's real current (participating) clan.
    #
    # Only ever REDIRECTS an existing entry — never ADDS one for a tag resolve_prior_cwl_
    # assignments left out entirely. That set already reflects every other exclusion this
    # function's caller (start_cwl_enrollment) applies before this point (permanently opted-out
    # accounts, no real CWL history at all) — reusing it here means this override can't
    # accidentally resurrect an excluded player into a fresh assignment/signup; it can only fix
    # WHICH already-valid entry a player has, never manufacture a new one from nothing (see
    # test_no_cwl_history_leaves_player_unassigned / test_skips_permanently_opted_out_accounts,
    # which lock this scoping in).
    current_clan_by_tag = {m["player_tag"]: m["clan_tag"] for m in all_members}
    participating_set = set(participating_clan_tags)
    family_clan_tags = set(resolve_guild_member_clan_tags(guild_id))
    for tag in list(prior_assignments):
        current_clan = current_clan_by_tag.get(tag)
        if (
            current_clan is not None
            and current_clan in participating_set
            and prior_assignments[tag] not in family_clan_tags
        ):
            prior_assignments[tag] = current_clan
    # Whole loop bundled into one asyncio.to_thread() hop (2026-08-16, Pitfall 26,
    # COPILOT_PITFALLS_COOKBOOK.md) rather than one hop per player — it's pure sync work with no
    # `await` inside, so one hop is both cheaper and keeps this loop atomic with respect to other
    # coroutines, matching its behavior before this fix.
    await asyncio.to_thread(_seed_prior_cwl_assignments_sync, guild_id, event["id"], season, prior_assignments)
    summary["assigned"] = len(prior_assignments)

    dm_result = await _send_cwl_enrollment_dm_batch(event["id"], guild_id, season, dm_targets)
    summary["contacted"] += dm_result["contacted"]
    summary["skipped_dm_guard"] += dm_result["skipped_dm_guard"]
    summary["skipped_already_dm_globally"] += dm_result["skipped_already_dm_globally"]
    summary["blocked"] = dm_result["blocked"]
    summary["failed"] = dm_result["failed"]

    await asyncio.to_thread(db.update_cwl_event_status_sync, event["id"], "signup_open")
    summary["ok"] = True
    # Success-path audit line (2026-08-21) — this action previously logged NOTHING on success,
    # which is exactly why tracker #0011's investigation couldn't tell from a full day of PROD
    # logs whether/when it had run, or how its DM set compared to the seeded signup rows.
    logging.info(
        f"[CWL-ENROLLMENT] Start Enrollment complete: guild={guild_id} season={season} "
        f"event={event['id']} seeded={summary['seeded']} contacted={summary['contacted']} "
        f"assigned={summary['assigned']} skipped_optout={summary['skipped_optout']} "
        f"skipped_unlinked={summary['skipped_unlinked']} "
        f"skipped_dm_guard={summary['skipped_dm_guard']} "
        f"skipped_already_dm_globally={summary['skipped_already_dm_globally']} "
        f"blocked={len(summary['blocked'])} failed={len(summary['failed'])}"
    )
    return summary


def resolve_cwl_pool_dm_targets_sync(
    guild_id: int, event_id: int, season: str,
    preloaded_members: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """The single definition of "who is in this event's player pool, and can we DM them" — shared
    by start_cwl_enrollment() and notify_new_cwl_pool_members() (the "Notify New Pool Members"
    button, web_bridge.py) so the two can never disagree about the pool. Both used to resolve
    their own recipients independently (Start Enrollment from a clan-scoped current-member scan,
    the button from the heavyweight board payload `_build_enrollment_payload_sync`), which is
    exactly how a manually-invited guest player ended up reachable by one and invisible to the
    other (2026-08-20 live bug report).

    The pool is the union of three sources, deduped by player_tag, first non-empty discord_id
    winning:
      1. current members of the guild's whole clan family unioned with every clan configured for
         this event — participating or not (rule b/f, CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md);
      2. this event's existing cwl_signups rows — the only way an individually-invited guest
         player is reachable at all, since their real current clan is by definition none of the
         above (deliberately not filtered by `source`: every row here is in the pool, see
         Cardinal Rule 24 on not gating behavior on a static write-time marker);
      3. cross-guild shared clans' rosters (cwl_shared_clan_players), whose players may have no
         local row of either kind.

    Returns {"targets", "skipped_optout", "skipped_unlinked"} — targets are the
    {player_tag, player_name, discord_id} dicts _send_cwl_enrollment_dm_batch() consumes, and the
    two counts are what the Start Enrollment summary reports. cwl_permanent_optout is honoured
    for every source, not just source 1 (its per-account "never DM me about CWL" semantics don't
    care how the player got pooled). preloaded_members lets start_cwl_enrollment pass the member
    list it already fetched (plus its account-wide expansion) rather than re-running that scan.

    Plain synchronous function (Pitfall 26, COPILOT_PITFALLS_COOKBOOK.md) — no `await` anywhere
    inside, so callers wrap the whole thing in one asyncio.to_thread() hop.
    """
    db = CACHE.db_manager
    result: Dict[str, Any] = {"targets": [], "skipped_optout": 0, "skipped_unlinked": 0}
    if db is None:
        return result

    all_clans = db.get_cwl_event_clans_sync(event_id)
    members = preloaded_members
    if members is None:
        members = db.get_current_clan_members_sync(resolve_cwl_pool_clan_tags_sync(guild_id, event_id))

    pool: Dict[str, Dict[str, Any]] = {}
    optout_by_tag: Dict[str, bool] = {}

    def _merge(
        player_tag: str,
        player_name: Optional[str],
        discord_id: Optional[str],
        authoritative_discord_id: bool = False,
    ) -> None:
        """Merge one source's view of a player into the pool.

        Args:
            player_tag: The player this row is about.
            player_name: Name to record if none has been merged yet (always first-wins — the
                signup's recorded name is the one the DM text should use).
            discord_id: DM recipient for this player, or None if this source doesn't know one.
            authoritative_discord_id: When True, `discord_id` REPLACES any previously merged
                value instead of only filling a None — including replacing it with None, which is
                what user_players returns for an account that is now in the UNASSIGNED pool
                (nobody owns it, so nobody should be DMed about it; it correctly falls through to
                `skipped_unlinked` below). Set by the live user_players source
                (2026-08-22): with plain first-non-None-wins, a source that runs last can add a
                missing link but can never CORRECT a stale one, so an enrollment-time
                cwl_signups snapshot silently kept ownership forever. Verified live: #29JQV2YCL
                has no current_clan_tag (so the clan-scoped `members` source below never returns
                it) and its signup row named a Discord user who no longer owns the account — a
                "Notify New Pool Members" run would have DMed the wrong person about it.
                Deliberately NOT fixed by reordering the sources: `members` is also live and must
                keep winning over the snapshot.
        """
        entry = pool.setdefault(
            player_tag, {"player_tag": player_tag, "player_name": None, "discord_id": None}
        )
        entry["player_name"] = entry["player_name"] or player_name
        if authoritative_discord_id:
            entry["discord_id"] = discord_id
        else:
            entry["discord_id"] = entry["discord_id"] or discord_id

    for member in members:
        optout_by_tag[member["player_tag"]] = bool(member["cwl_permanent_optout"])
        _merge(member["player_tag"], member["player_name"], member["discord_id"])

    for signup in db.get_cwl_signups_for_event_sync(event_id):
        _merge(signup["player_tag"], signup["player_name"], signup["dmed_discord_id"])

    for clan in all_clans:
        if not clan.get("participating", 1):
            continue
        shared = db.get_cwl_shared_clan_sync(clan["clan_tag"], season)
        if shared is None:
            continue
        for shared_player in db.get_cwl_shared_clan_players_sync(shared["id"]):
            _merge(shared_player["player_tag"], shared_player["player_name"], shared_player["dmed_discord_id"])

    # Sources 2/3 carry no opt-out flag and may carry no discord_id at all (the Guests search can
    # add a tag it found no Discord link for) — user_players is the authority for both.
    # authoritative_discord_id=True (2026-08-22): sources 2/3 are enrollment-time snapshots, so
    # whatever they supplied here may name a Discord user who no longer owns the account; this
    # live source must be able to overwrite it, not merely fill a gap. Query scope is unchanged —
    # optout_by_tag is populated only from `members` (source 1, also live), so `unknown_tags` is
    # precisely the set whose discord_id could have come from a snapshot.
    unknown_tags = [tag for tag in pool if tag not in optout_by_tag]
    for tag, link in (db.get_player_links_sync(unknown_tags) if unknown_tags else {}).items():
        optout_by_tag[tag] = link["cwl_permanent_optout"]
        _merge(tag, link["player_name"], link["discord_id"], authoritative_discord_id=True)

    for entry in pool.values():
        if optout_by_tag.get(entry["player_tag"]):
            result["skipped_optout"] += 1
        elif entry["discord_id"]:
            result["targets"].append(entry)
        else:
            result["skipped_unlinked"] += 1
    return result


async def _send_cwl_enrollment_dm_batch(
    event_id: int, guild_id: int, season: str, dm_targets: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Shared DM-sending loop for start_cwl_enrollment() — one place for the admin DM-guard and
    the per-recipient outcome bookkeeping (2026-08-18, item 3 of the enrollment redesign: a
    Discord error must never abort the batch — that's now guaranteed one layer down, in
    cache_manager.py's send_user_dm_detailed(), which retries a transient error internally and
    never raises — so this loop just needs to keep going and record what happened to who).

    Returns {"contacted", "skipped_dm_guard", "skipped_already_dm_globally", "blocked", "failed"}
    — the latter two are lists of player_name (or player_tag) strings for the Start Enrollment
    summary to report back to the admin, since "blocked" (DMs closed/bot blocked) and "failed"
    (transient error, retries exhausted) call for different admin follow-up.

    Global dm_sent dedup (2026-08-18, rule h, project owner's spec: "we generally need only one
    enrollment DM per player regardless of how many guilds and/or clans invite him") — checked
    once per batch via a single bulk lookup, not per-recipient, so a guild pooling hundreds of
    players doesn't cost hundreds of extra queries. A player skipped here already has their real
    global status seeded into this guild's own cwl_signups row by start_cwl_enrollment's caller
    (see the bulk status lookup right before signups_to_create is built) — this function only
    ever decides whether to DM, never touches status.
    """
    from qapbot.config import CONFIG

    result: Dict[str, Any] = {
        "contacted": 0, "skipped_dm_guard": 0, "skipped_already_dm_globally": 0, "blocked": [], "failed": [],
    }
    db = CACHE.db_manager
    already_dm_by_tag = await asyncio.to_thread(
        db.get_cwl_player_season_dm_status_bulk_sync, [p["player_tag"] for p in dm_targets], season,
    ) if db is not None else {}

    # Decide who we will actually DM BEFORE sending, so the signup rows their buttons need can be
    # seeded in one batch (2026-08-22, tracker #0016). CwlSignupResponseButton resolves a click by
    # (event_id, player_tag) and returns `no_longer_valid` when no cwl_signups row exists, so a DM
    # sent without one carries a permanently dead button. start_cwl_enrollment seeds its rows
    # before calling this; "Notify New Pool Members" (web_bridge.notify_new_cwl_pool_members) did
    # not — and since its whole job is reaching pool members added AFTER Start Enrollment ran,
    # i.e. exactly the players with no row yet, every DM it sent to a new member was dead on
    # arrival. Confirmed live: 27 such DMs across 4 users, all stuck 'pending' because nobody
    # could respond. Putting the invariant here rather than in that one caller means any future
    # caller of this batch helper inherits it.
    to_dm: List[Dict[str, Any]] = []
    for participant in dm_targets:
        if already_dm_by_tag.get(participant["player_tag"]):
            result["skipped_already_dm_globally"] += 1
            continue
        participant_discord_id = str(participant["discord_id"])
        is_admin = participant_discord_id == CONFIG.server_admin
        is_prod_tester = not CONFIG.is_dev_mode and participant_discord_id in CACHE.testers
        if CONFIG.cwl_dm_restrict_to_admin and not (is_admin or is_prod_tester):
            result["skipped_dm_guard"] += 1
            continue
        to_dm.append(participant)

    # Only the players we're actually about to DM — a target skipped by either guard above must
    # not gain a board entry as a side effect of a DM it never received. bulk_create is
    # ON CONFLICT(event_id, player_tag) DO NOTHING, so this is a no-op for Start Enrollment's
    # already-seeded rows and never clobbers a response a player has already given.
    if db is not None and to_dm:
        # Both reads go through asyncio.to_thread (Pitfall 26) and are BULK — this runs on the
        # DM-blast path, where a per-player query would be one blocking round trip per recipient.
        existing_rows = await asyncio.to_thread(db.get_cwl_signups_for_event_sync, event_id)
        existing_tags = {s["player_tag"] for s in existing_rows}
        missing = [p for p in to_dm if p["player_tag"] not in existing_tags]
        if missing:
            global_status_by_tag = await asyncio.to_thread(
                db.get_cwl_player_season_status_bulk_sync, [p["player_tag"] for p in missing], season,
            )
            await asyncio.to_thread(
                db.bulk_create_cwl_signups_sync,
                event_id,
                [
                    {
                        "player_tag": p["player_tag"],
                        "player_name": p["player_name"],
                        "dmed_discord_id": p["discord_id"],
                        "preferred_league_rank": None,
                        "source": "template_confirm",
                        # Never a hardcoded 'pending' — a player who already answered another
                        # guild's DM must not be contradicted here (rule h). Same seeding
                        # start_cwl_enrollment does, just from the bulk reader.
                        "status": (
                            global_status_by_tag[p["player_tag"]]["status"]
                            if p["player_tag"] in global_status_by_tag else "pending"
                        ),
                    }
                    for p in missing
                ],
            )
            logging.info(
                f"[CWL-ENROLLMENT] Seeded {len(missing)} missing cwl_signups row(s) for event "
                f"{event_id} before DMing — their buttons would otherwise have been dead (#0016)"
            )

    for participant in to_dm:
        sent, outcome, dm_message_id, dm_channel_id = await send_cwl_signup_template_dm(
            event_id, guild_id, season, participant
        )
        if sent:
            result["contacted"] += 1
            if db is not None:
                await asyncio.to_thread(
                    db.mark_cwl_player_dm_sent_sync,
                    participant["player_tag"], season, participant["player_name"], participant["discord_id"],
                    event_id, guild_id, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
                    dm_message_id, dm_channel_id,
                )
        elif outcome == "blocked":
            result["blocked"].append(participant["player_name"] or participant["player_tag"])
        else:
            result["failed"].append(participant["player_name"] or participant["player_tag"])
    return result


async def send_cwl_signup_template_dm(
    event_id: int, guild_id: int, season: str, participant: Dict[str, Any],
) -> Tuple[bool, str, Optional[str], Optional[str]]:
    """Send one template-copy confirm/opt-out DM. Originally kept as its own function just so
    start_cwl_enrollment stayed readable — no longer private (dropped the leading underscore
    2026-08-15) since the Guests invite flow (web_bridge.py's handle_post_cwl_enrollment_guest)
    now calls it directly too, for a one-off single-recipient send outside the bulk loop below.

    Returns (sent, outcome, message_id, channel_id) — see CacheManager.send_user_dm_detailed()'s
    own docstring for what "blocked" vs "failed" means (2026-08-18, item 3 of the enrollment
    redesign). message_id/channel_id (2026-08-19, added for the Delete-Season DM-retraction fix —
    see db_manager.py's cwl_player_season_status CREATE TABLE comment) are None whenever sent is
    False, and are the caller's only way to later find/delete this exact DM."""
    from qapbot.i18n import t
    from qapbot.ui_cwl_roster import build_cwl_signup_response_view

    discord_id = participant["discord_id"]
    message = t(
        'cwl.template.dm_body',
        guild_id=guild_id,
        user_id=discord_id,
        season=season,
        player_name=participant["player_name"] or participant["player_tag"],
    )
    view = build_cwl_signup_response_view(event_id, participant["player_tag"], guild_id)
    sent_message_ref: List[Any] = []
    sent, outcome = await CACHE.send_user_dm_detailed(
        str(discord_id), message, view=view, sent_message_out=sent_message_ref
    )
    dm_message = sent_message_ref[0] if sent_message_ref else None
    message_id = str(dm_message.id) if dm_message is not None else None
    channel_id = str(dm_message.channel.id) if dm_message is not None else None
    return sent, outcome, message_id, channel_id


async def _retract_enrollment_dms_for_tags(
    event_id: int, player_tags: List[str], context: str,
) -> None:
    """Retract the enrollment DMs of specific players whose signup rows were just purged
    (2026-08-21, tracker #0011). Thin async bridge so a plain-sync cleanup running under
    asyncio.to_thread can still get its DMs retracted: the sync side returns the affected tags,
    this resolves their DM refs and hands them to cleanup_stale_cwl_enrollment_dms().

    Uses QBcore.bot rather than taking a bot/interaction parameter — the only production caller
    (remove_cwl_guest_clan, reached from web_bridge.py's clan-config save) has no interaction in
    scope, and this matches how other non-interaction contexts in this codebase reach the client.
    Best-effort throughout, exactly like the Delete-Season path it mirrors."""
    import QBcore

    db = CACHE.db_manager
    bot = getattr(QBcore, "bot", None)
    if db is None or bot is None:
        return

    wanted = set(player_tags)
    all_refs = await asyncio.to_thread(
        db.get_cwl_player_season_status_dm_refs_for_event_sync, event_id
    )
    dm_refs = [r for r in all_refs if r["player_tag"] in wanted]
    if not dm_refs:
        return

    result = await cleanup_stale_cwl_enrollment_dms(bot, dm_refs)
    logging.info(
        f"[CWL-ENROLLMENT] {context} (event {event_id}): retracted "
        f"{result['deleted']}/{len(dm_refs)} now-stale enrollment DM(s), "
        f"{result['failed']} could not be removed."
    )


async def cleanup_stale_cwl_enrollment_dms(bot: Any, dm_refs: List[Dict[str, Any]]) -> Dict[str, int]:
    """Best-effort delete of the CWL enrollment DMs a just-deleted event sent (2026-08-19 fix,
    live bug report: "Delete Season" left every recipient's Confirm/Opt Out buttons sitting
    live-looking in their DMs — clicking one now correctly reports the sign-up as no longer
    valid, but the button itself stayed there looking clickable in the meantime, which is more
    confusing than just removing the message outright — project owner's stated preference).

    dm_refs comes from db.get_cwl_player_season_status_dm_refs_for_event_sync(event_id), read by
    the caller BEFORE delete_cwl_event_sync() clears those rows away — this function only ever
    talks to the Discord API, never the DB.

    Always called AFTER the season itself is already deleted, so every failure mode here (DMs
    closed, bot blocked, the user or message already gone, any other API hiccup) is non-fatal and
    silently skipped — there's nothing left to roll back to, and a stale DM that can't be
    retracted is no worse than the pre-fix behavior."""
    import discord

    result = {"deleted": 0, "failed": 0}
    for ref in dm_refs:
        try:
            user = await bot.fetch_user(int(ref["dmed_discord_id"]))
            dm_channel = user.dm_channel or await user.create_dm()
            message = await dm_channel.fetch_message(int(ref["message_id"]))
            await message.delete()
            result["deleted"] += 1
        except discord.NotFound:
            pass  # message and/or user already gone — nothing to retract
        except discord.Forbidden:
            result["failed"] += 1  # DMs closed / bot blocked
        except (discord.HTTPException, ValueError, TypeError) as e:
            logging.warning(
                f"[CWL-ENROLLMENT] Could not delete stale enrollment DM for "
                f"discord_id={ref.get('discord_id')}: {e}"
            )
            result["failed"] += 1
    return result
