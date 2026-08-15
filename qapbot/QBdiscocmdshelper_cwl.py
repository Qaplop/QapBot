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
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

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


def compute_league_adjusted_skill_scores(player_tags: List[str]) -> Dict[str, float]:
    """Player-skill score for the "Manage Enrollment" board's TH/Skill/Alphabetical sort option:
    league-adjusted average stars/attack over each player's last 10 CWL attacks. Each attack's
    raw stars is weighted by _league_weight() above (the league its attacking clan was in for
    that CWL round) before averaging — a 3-star earned in Champion League counts for meaningfully
    more than a 3-star in Master League. Rounded to 2dp.

    A player_tag with no resolvable CWL-attack-with-league data (never played CWL, or league
    data isn't populated for those seasons — get_recent_cwl_attacks_with_league_sync's own
    docstring covers why that can happen) is simply absent from the returned dict — never a
    fabricated 0, matching the same "None means no data, not zero" convention
    compute_roster_stats_sync (QBhelperfunctions.py) already uses."""
    db = CACHE.db_manager
    if db is None or not player_tags:
        return {}
    attacks_by_tag = db.get_recent_cwl_attacks_with_league_sync(player_tags)
    scores: Dict[str, float] = {}
    for tag, attacks in attacks_by_tag.items():
        if not attacks:
            continue
        weighted_total = sum(a["stars"] * _league_weight(a["league_rank"]) for a in attacks)
        scores[tag] = round(weighted_total / len(attacks), 2)
    return scores


def compute_avg_stars_per_attack(player_tags: List[str]) -> Dict[str, float]:
    """The "Manage Enrollment" board's other number-display option (2026-08-14, project owner's
    spec: a second radio group lets the admin pick which number shows next to each player's
    name — this, or the league-adjusted skill score above; defaults to this one). Plain,
    unweighted average stars/attack over each player's last <=10 CWL attacks — the exact same
    attack window compute_league_adjusted_skill_scores() uses, just without the league
    weighting. Same "no data -> absent from the dict, never a fabricated 0" convention."""
    db = CACHE.db_manager
    if db is None or not player_tags:
        return {}
    attacks_by_tag = db.get_recent_cwl_attacks_with_league_sync(player_tags)
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
    include_all_accounts = guild_config.get("cwl_enrollment_include_all_linked_accounts", False)
    include_all_accounts_status_text = (
        t('cwl.settings.status_enabled', guild_id=guild_id_int)
        if include_all_accounts
        else t('cwl.settings.status_disabled', guild_id=guild_id_int)
    )
    enrollment_pool_block = (
        f"⠀\n**{t('cwl.settings.enrollment_pool_block_title', guild_id=guild_id_int)}**\n"
        f"{t('cwl.settings.enrollment_pool_value', guild_id=guild_id_int, status=include_all_accounts_status_text)}"
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


async def start_cwl_enrollment(guild_id: int, season: str) -> Dict[str, Any]:
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

    participants = db.get_current_clan_members_sync(participating_clan_tags)

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

    # Auto-assignment seed — the initial "who probably plays where" suggestion, from each
    # player's own last real CWL attack, anywhere (2026-08-14 redesign — see
    # resolve_prior_cwl_assignments()'s docstring). Runs once, here; every later change happens
    # via manual drag-and-drop on the Manage Enrollment board (assignment_source='admin_override',
    # locked=True there). Scoped to the guild's FULL member-clan roster, not just this season's
    # participating clans — the auto-assign target is still restricted to participating clans
    # (nothing else has a column), but the candidate pool it draws players from is every current
    # member of every guild clan, so a player whose real clan didn't opt into CWL this season (or
    # who transferred since their last CWL war) is still correctly resolved and assignable.
    all_member_clan_tags = resolve_guild_member_clan_tags(guild_id)
    all_members = db.get_current_clan_members_sync(all_member_clan_tags)
    current_member_tags = {p["player_tag"] for p in all_members}
    # resolve_prior_cwl_assignments() only ever resolves entries for the player_tags it was
    # given, so prior_assignments' keys are already a subset of current_member_tags — no extra
    # membership filter needed here (unlike the pre-2026-08-14 design, which queried clan
    # rosters independently of current membership and needed one).
    prior_assignments = resolve_prior_cwl_assignments(list(current_member_tags), participating_clan_tags)
    assignments_to_create = [
        {"player_tag": player_tag, "assigned_clan_tag": clan_tag}
        for player_tag, clan_tag in prior_assignments.items()
    ]
    if assignments_to_create:
        db.bulk_create_cwl_assignments_sync(event["id"], assignments_to_create)
        summary["assigned"] = len(assignments_to_create)

    for participant in dm_targets:
        participant_discord_id = str(participant["discord_id"])
        is_admin = participant_discord_id == CONFIG.server_admin
        is_prod_tester = not CONFIG.is_dev_mode and participant_discord_id in CACHE.testers
        if CONFIG.cwl_dm_restrict_to_admin and not (is_admin or is_prod_tester):
            summary["skipped_dm_guard"] += 1
            continue
        sent = await _send_cwl_signup_template_dm(event["id"], guild_id, season, participant)
        if sent:
            summary["contacted"] += 1

    db.update_cwl_event_status_sync(event["id"], "signup_open")
    summary["ok"] = True
    return summary


async def _send_cwl_signup_template_dm(event_id: int, guild_id: int, season: str, participant: Dict[str, Any]) -> bool:
    """Send one template-copy confirm/opt-out DM. Kept as its own function so start_cwl_enrollment
    stays readable — this is the only place that builds the DM's content+view pair."""
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
    return await CACHE.send_user_dm(str(discord_id), message, view=view)
