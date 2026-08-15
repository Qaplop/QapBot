"""CWL clan-config web bridge (CWL_CLAN_CONFIG_ACTIVITY_PLAN.md Phase B).

A small aiohttp.web API that runs IN-PROCESS with the bot (same asyncio loop, same CACHE/
db_manager — no data duplication, no second source of truth). It's the only thing a
cloudflared tunnel exposes; the Cloudflare Worker (activity/server) proxies the Discord
Activity's frontend calls through the tunnel to here, attaching a shared secret.

Security model (see the plan doc's "Auth & permission model"): the Worker's OAuth check is a
UX gate only, not the real boundary — every request here re-derives admin status itself via a
live guild-member lookup through the bot's own gateway cache, exactly like
QBdiscocmdshelper.check_admin_permissions() does for a real Discord interaction, just without
needing one. Bound to 127.0.0.1 only (see start_web_bridge()) — cloudflared, not this bot, is
what makes it reachable from Cloudflare at all.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

import discord
from aiohttp import web

from qapbot.cache_manager import CACHE

_runner: Optional[web.AppRunner] = None


def _bridge_log_label(kind: str, raw_id: Any) -> str:
    """"Name (id)" for a guild_id/discord_user_id on a bridge request, resolved via the bot's
    own gateway cache — falls back to the bare id if unresolvable (bot not ready yet, guild/user
    not in cache) rather than failing the request over a logging nicety."""
    if not raw_id:
        return "-"
    try:
        numeric_id = int(raw_id)
    except (TypeError, ValueError):
        return str(raw_id)

    import QBcore

    bot = getattr(QBcore, "bot", None)
    name = None
    if bot is not None:
        if kind == "guild":
            guild = bot.get_guild(numeric_id)
            name = guild.name if guild else None
        else:
            user = bot.get_user(numeric_id)
            name = user.name if user else None
    return f"{name} ({numeric_id})" if name else str(numeric_id)


@web.middleware
async def _access_log_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """Replaces aiohttp's default combined-log-format access log (IP, duplicate embedded
    timestamp, raw numeric IDs) with one line naming the guild/user the request was actually
    for — the bridge's only two identity fields, present as query params on every GET and as
    JSON body fields on every POST. See start_web_bridge()'s access_log=None, which turns the
    default logger off so this doesn't just add a second line."""
    response = await handler(request)
    try:
        guild_id_raw = request.query.get("guild_id")
        discord_user_id_raw = request.query.get("discord_user_id")
        if guild_id_raw is None or discord_user_id_raw is None:
            # Already fully read by the handler by this point — aiohttp caches the raw body on
            # the Request object, so re-reading it here for logging purposes is free.
            try:
                body = await request.json()
            except Exception:
                body = None
            if isinstance(body, dict):
                guild_id_raw = guild_id_raw or body.get("guild_id")
                discord_user_id_raw = discord_user_id_raw or body.get("discord_user_id")
        logging.info(
            f"[WEB-BRIDGE] {request.method} {request.path} "
            f"guild={_bridge_log_label('guild', guild_id_raw)} "
            f"user={_bridge_log_label('user', discord_user_id_raw)} -> {response.status}"
        )
    except Exception:
        logging.debug("[WEB-BRIDGE] access-log middleware failed", exc_info=True)
    return response


def _check_secret(request: web.Request) -> bool:
    from qapbot.config import CONFIG

    provided = request.headers.get("X-Bridge-Secret", "")
    return bool(CONFIG.web_bridge_secret) and provided == CONFIG.web_bridge_secret


async def _resolve_admin(guild_id: int, discord_user_id: int) -> bool:
    """Mirrors check_admin_permissions()'s guild-administrator-or-configured-super-admin
    logic, without needing a discord.Interaction. Only the numeric-ID super-admin form is
    supported here (not the legacy username fallback — there's no live "current username" to
    compare against outside of an interaction, and numeric ID is the recommended config anyway).
    """
    import QBcore
    from qapbot.config import CONFIG

    server_admin = CONFIG.server_admin
    if server_admin and server_admin.isdigit() and str(discord_user_id) == server_admin:
        return True

    guild = QBcore.bot.get_guild(guild_id)
    if guild is None:
        return False
    member = guild.get_member(discord_user_id)
    if member is None:
        try:
            member = await guild.fetch_member(discord_user_id)
        except (discord.NotFound, discord.HTTPException):
            return False
    return bool(member.guild_permissions.administrator)


async def _resolve_admin_or_leader(guild_id: int, discord_user_id: int) -> bool:
    """_resolve_admin() above, extended with the Leader/Co-Leader Discord role check for the
    "Manage Enrollment" screen (CWL_ROSTER_PLANNING_PLAN.md, 2026-08-10) — same defense-in-depth
    re-derivation the bridge already does independently of the Discord-side gate
    (_check_cwl_admin_or_leader_permission in ui_cwl_roster.py). guild_config["coc_role_leader_id"]/
    ["coc_role_coleader_id"] is one shared pair of roles per guild, not per-clan.
    """
    import QBcore

    if await _resolve_admin(guild_id, discord_user_id):
        return True

    guild = QBcore.bot.get_guild(guild_id)
    if guild is None:
        return False
    member = guild.get_member(discord_user_id)
    if member is None:
        try:
            member = await guild.fetch_member(discord_user_id)
        except (discord.NotFound, discord.HTTPException):
            return False

    guild_config = CACHE.server_config.get(str(guild_id), {})
    leader_role_id = guild_config.get("coc_role_leader_id")
    coleader_role_id = guild_config.get("coc_role_coleader_id")
    member_role_ids = {role.id for role in member.roles}
    if leader_role_id and int(leader_role_id) in member_role_ids:
        return True
    if coleader_role_id and int(coleader_role_id) in member_role_ids:
        return True
    return False


async def _build_clan_config_payload(guild_id: int) -> Dict[str, Any]:
    """Build the GET response for whichever season is currently selected on the guild's CWL
    Management screen (the season select there, CWL_CLAN_CONFIG_ACTIVITY_PLAN.md Phase E.3) —
    this Activity has no season picker of its own; it always just shows/edits that one season.

    A clan with no row yet for that season defaults to roster_size=15, cwl_start_at=the 1st of
    the season's month at 08:00 UTC (the game's static schedule), participating=False.
    """
    from qapbot.QBdiscocmdshelper import get_guild_clans_including_member_config
    from qapbot.QBdiscocmdshelper_cwl import cwl_league_rank, resolve_selected_cwl_season

    db = CACHE.db_manager

    season = resolve_selected_cwl_season(guild_id)
    event = db.get_cwl_event_sync(str(guild_id), season) if db is not None else None

    # Keyed by clan_tag -> its cwl_event_clans row, which now exists for every clan ever
    # touched (participating or not) — deactivating a clan keeps its row (with settings intact)
    # instead of deleting it, so reactivating doesn't lose roster_size/cwl_start_at. "row exists"
    # is therefore no longer the same thing as "participating"; use the explicit column instead.
    known_rows: Dict[str, Dict[str, Any]] = {}
    if db is not None and event is not None:
        for row in db.get_cwl_event_clans_sync(event["id"]):
            known_rows[row["clan_tag"]] = row

    # Union with the guild's own family (2026-08-15 bugfix, found while testing the cross-guild
    # shared-clan settings guard): a guest clan — or a clan this guild only knows about via
    # shared-clan attachment — was never part of member_clans/member_families, so
    # get_guild_clans_including_member_config() alone would silently drop it from the table on
    # every reload after the save that added it, even though its cwl_event_clans row (and its
    # participating=True flag) is still sitting right there in the DB. Any clan with an existing
    # row for this event belongs on the screen regardless of family membership — same "whatever's
    # actually configured, not just the family" rule _build_enrollment_payload already applies to
    # its own player pool.
    all_tags = set(get_guild_clans_including_member_config(guild_id)) | set(known_rows.keys())

    default_start_at = f"{season}-01T08:00Z"

    # Highest tier first (CWL_CLAN_CONFIG_ACTIVITY_PLAN.md Phase E), name as the tiebreaker —
    # matches the Discord-side CWL Management embed's own sort.
    def _sort_key(tag: str) -> tuple:
        return (-cwl_league_rank(CACHE.get_clan_war_league(tag)), (CACHE.get_clan_name(tag, tag) or tag).lower())

    import QBcore

    # Cross-guild shared clans (2026-08-15 follow-up, project owner's spec): "one shared record,"
    # not two independently-edited copies — this cache mirrors _build_enrollment_payload's own
    # owner_clan_rows_cache so a shared owner's event isn't re-fetched once per clan.
    owner_clan_rows_cache: Dict[int, Dict[str, Dict[str, Any]]] = {}

    def _owner_row_for(owner_event_id: int, clan_tag: str) -> Optional[Dict[str, Any]]:
        if owner_event_id not in owner_clan_rows_cache:
            owner_clan_rows_cache[owner_event_id] = {r["clan_tag"]: r for r in db.get_cwl_event_clans_sync(owner_event_id)}
        return owner_clan_rows_cache[owner_event_id].get(clan_tag)

    clans: List[Dict[str, Any]] = []
    for tag in sorted(all_tags, key=_sort_key):
        row = known_rows.get(tag)
        participating = bool(row["participating"]) if row else False

        # Cross-guild shared-clan status (2026-08-15) — only meaningful for a currently
        # participating clan; a deactivated/never-configured row has nothing to share.
        shared_with: Optional[Dict[str, Any]] = None
        effective_row = row
        if participating and db is not None:
            shared = db.get_cwl_shared_clan_sync(tag, season)
            if shared is not None:
                other_guild_ids = [
                    g["guild_id"] for g in db.list_cwl_shared_clan_guilds_sync(shared["id"])
                    if g["guild_id"] != str(guild_id)
                ]
                if other_guild_ids:
                    other_guild_names = []
                    for other_id in other_guild_ids:
                        other_guild = QBcore.bot.get_guild(int(other_id))
                        other_guild_names.append(other_guild.name if other_guild else other_id)
                    shared_with = {
                        "is_owner": shared["owner_guild_id"] == str(guild_id),
                        "other_guild_ids": other_guild_ids,
                        "other_guild_names": other_guild_names,
                    }
                # roster_size/cwl_start_at always come from the OWNER's own row (harmless no-op
                # for the owner itself) — a follower must see the same canonical settings it'll
                # actually get back if it saves without touching them, never a stale/divergent
                # local copy (live-testing feedback, 2026-08-15: this screen was still reading/
                # writing each guild's own independent row for a shared clan, unlike the
                # Manage Enrollment board, which already deferred to the owner correctly).
                owner_row = _owner_row_for(shared["owner_event_id"], tag)
                if owner_row is not None:
                    effective_row = owner_row

        clans.append({
            "clan_tag": tag,
            "name": CACHE.get_clan_name(tag, tag),
            # Tier is always the live value (CoC-defined, never admin-set) — same rule as the
            # Discord-side screens, see CWL_ROSTER_PLANNING_PLAN.md's tier fix.
            "tier": CACHE.get_clan_war_league(tag),
            "participating": participating,
            "roster_size": effective_row["roster_size"] if effective_row else 15,
            "cwl_start_at": effective_row["cwl_start_at"] if effective_row else default_start_at,
            "shared_with": shared_with,
        })

    return {
        "season": season,
        "event_status": event["status"] if event else None,
        "clans": clans,
    }


async def _build_enrollment_payload(guild_id: int) -> Dict[str, Any]:
    """Build the GET response for the "Manage Enrollment" board — the guild's selected season's
    participating clans as columns, and a merged player list combining cwl_signups (whatever's
    already been recorded) with get_current_clan_members_sync() (so a current member who never
    got/answered the template DM still shows up, ready for a 1-click sign-up rather than being
    invisible until they act first). Each player is annotated with their current assignment, if
    any (None = the Unassigned pool).

    2026-08-14 (project owner's spec): the player pool is every current member of every guild
    member clan, not just this season's participating clans — "really ALL players of ALL member
    clans should be in the pool" — so an admin can manually drag in a player from a clan that
    isn't participating this season. Only the clan COLUMNS themselves stay restricted to
    participating clans (see resolve_prior_cwl_assignments()'s docstring for why the same split
    applies to auto-assignment).

    2026-08-15 (guest clans/players, project owner's spec): a clan invited as a "guest" via the
    Configure Participating Clans screen's Guests search is just a normal participating-clan row
    with no special marker — see _search_cwl_guests()/handle_post_clan_config() — so its roster
    is pulled into the pool below via participating_clan_tags, unioned with the family pool. A
    guest *player* (an individual invited directly, possibly from a clan that isn't participating
    at all) is a plain cwl_signups row with source='guest_invite', surfaced via each player's
    `is_guest` flag — see handle_post_cwl_enrollment_guest().

    2026-08-15 (cross-guild shared clans, slice 4 — live shared roster): a participating clan
    that's actually shared with another guild (cwl_shared_clans) gets its roster_size from the
    OWNER guild's own cwl_event_clans row, not this guild's possibly-vestigial one, and its
    player pool from cwl_shared_clan_players instead of this guild's own cwl_signups/
    cwl_assignments — so both guilds' boards render the identical live roster. See the merge
    blocks below (marked 2026-08-15) for exactly where each override happens."""
    from qapbot.QBdiscocmdshelper_cwl import (
        compute_avg_stars_per_attack,
        compute_league_adjusted_skill_scores,
        cwl_league_rank,
        resolve_guild_member_clan_tags,
        resolve_selected_cwl_season,
    )
    from qapbot.emojis import th_icon_url

    db = CACHE.db_manager
    season = resolve_selected_cwl_season(guild_id)
    event = db.get_cwl_event_sync(str(guild_id), season) if db is not None else None

    if db is None or event is None:
        return {
            "season": season,
            "event_status": event["status"] if event else None,
            "clans": [],
            "players": [],
        }

    all_clans = db.get_cwl_event_clans_sync(event["id"])
    participating_clans = [c for c in all_clans if c.get("participating", 1)]
    participating_clan_tags = [c["clan_tag"] for c in participating_clans]

    # Highest tier first, name as the tiebreaker — same sort as the clan-config payload and the
    # Discord-side CWL Management embed.
    def _tier_for(clan_row: Dict[str, Any]) -> Optional[str]:
        return CACHE.get_clan_war_league(clan_row["clan_tag"]) or clan_row.get("target_league_rank")

    participating_clans.sort(
        key=lambda c: (-cwl_league_rank(_tier_for(c)), (CACHE.get_clan_name(c["clan_tag"], c["clan_tag"]) or "").lower())
    )

    # Cross-guild shared clans (2026-08-15, slice 4: live shared roster) — cache lookups since
    # the same shared_clan row and owner-event clan list are needed again below for the player
    # pool, and re-fetching per player would be wasteful. Empty for the ~98% of events with no
    # shared clans (one indexed query per participating clan, no shared rows to find).
    shared_clan_by_tag: Dict[str, Dict[str, Any]] = {}
    owner_clan_rows_cache: Dict[int, Dict[str, Dict[str, Any]]] = {}
    for c in participating_clans:
        shared = db.get_cwl_shared_clan_sync(c["clan_tag"], season)
        if shared is not None:
            shared_clan_by_tag[c["clan_tag"]] = shared

    def _owner_row_for(clan_tag: str, owner_event_id: int) -> Optional[Dict[str, Any]]:
        if owner_event_id not in owner_clan_rows_cache:
            owner_clan_rows_cache[owner_event_id] = {r["clan_tag"]: r for r in db.get_cwl_event_clans_sync(owner_event_id)}
        return owner_clan_rows_cache[owner_event_id].get(clan_tag)

    # Cross-guild shared clans, enrollment-status enrichment (2026-08-15 follow-up, project
    # owner's spec: "the enrollment status per player is global and should be stored per player
    # only once... no harm to show the enrollment status also for players from other
    # clans/guilds") — a shared clan's current member who isn't ASSIGNED yet (so the roster merge
    # below doesn't cover them) still sits in this guild's own pool via the plain current-member
    # union below; if the sharing partner guild's own cwl_signups already has a real status for
    # that exact real-world player (e.g. that guild's own Start Enrollment already DMed them),
    # showing it here is strictly more accurate than the previous hardcoded None — same player,
    # same clan, same season, just recorded through the other guild's event so far.
    owner_signups_cache: Dict[int, Dict[str, Dict[str, Any]]] = {}

    def _owner_signup_status_for(clan_tag: str, player_tag: str) -> Optional[str]:
        shared = shared_clan_by_tag.get(clan_tag)
        if shared is None:
            return None
        owner_event_id = shared["owner_event_id"]
        if owner_event_id not in owner_signups_cache:
            owner_signups_cache[owner_event_id] = {
                s["player_tag"]: s for s in db.get_cwl_signups_for_event_sync(owner_event_id)
            }
        owner_signup = owner_signups_cache[owner_event_id].get(player_tag)
        return owner_signup["status"] if owner_signup is not None else None

    clans = []
    for c in participating_clans:
        # A shared clan's roster_size is the OWNER's setting, not this (possibly non-owner)
        # guild's own row — "one shared record," not two independently-edited copies (see
        # CWL_ROSTER_PLANNING_PLAN.md). Falls back to this guild's own row if the owner's row is
        # somehow missing (shouldn't happen, defensive only).
        shared = shared_clan_by_tag.get(c["clan_tag"])
        effective_row = c
        if shared is not None:
            owner_row = _owner_row_for(c["clan_tag"], shared["owner_event_id"])
            if owner_row is not None:
                effective_row = owner_row
        clans.append({
            "clan_tag": c["clan_tag"],
            "name": CACHE.get_clan_name(c["clan_tag"], c["clan_tag"]),
            "tier": _tier_for(c),
            "roster_size": effective_row["roster_size"],
        })

    players_by_tag: Dict[str, Dict[str, Any]] = {}
    for signup in db.get_cwl_signups_for_event_sync(event["id"]):
        players_by_tag[signup["player_tag"]] = {
            "player_tag": signup["player_tag"],
            "player_name": signup["player_name"],
            "discord_id": signup["discord_id"],
            "signup_status": signup["status"],
            # source='guest_invite' is set by handle_post_cwl_enrollment_guest — the only writer
            # that uses it (every other signup path uses 'template_confirm'/'admin_added'/etc.).
            # Badge-only on the frontend; doesn't change any pool/eligibility logic.
            "is_guest": signup["source"] == "guest_invite",
        }
    # user_players.th_level (2026-08-14: now kept fresh for every current member, linked or not
    # — see coc_cache.py's update_player_info_in_user_accounts) is the primary TH source: live
    # from the CoC API, not dependent on the player ever having made a tracked war attack.
    # Pool is every guild member clan PLUS every clan actually participating this season
    # (2026-08-15: a guest clan invited via the "Guests" search on Configure Participating Clans
    # is added straight into cwl_event_clans like any other clan — see CWL_ROSTER_PLANNING_PLAN.md
    # — but isn't part of the guild's own family, so resolve_guild_member_clan_tags() alone would
    # never surface its roster here; the union is what actually gives it "full participating
    # clan" treatment: a board column from `clans` above, and its live members in the pool here).
    all_member_clan_tags = list(set(resolve_guild_member_clan_tags(guild_id)) | set(participating_clan_tags))
    live_th_by_tag: Dict[str, int] = {}
    optout_by_tag: Dict[str, bool] = {}
    current_clan_by_tag: Dict[str, str] = {}
    for member in db.get_current_clan_members_sync(all_member_clan_tags):
        if member.get("th_level") is not None:
            live_th_by_tag[member["player_tag"]] = member["th_level"]
        optout_by_tag[member["player_tag"]] = bool(member["cwl_permanent_optout"])
        current_clan_by_tag[member["player_tag"]] = member["clan_tag"]
        if member["player_tag"] not in players_by_tag:
            players_by_tag[member["player_tag"]] = {
                "player_tag": member["player_tag"],
                "player_name": member["player_name"],
                "discord_id": member["discord_id"],
                "signup_status": _owner_signup_status_for(member["clan_tag"], member["player_tag"]),
                "is_guest": False,
            }

    assigned_clan_by_tag = {
        a["player_tag"]: a["assigned_clan_tag"] for a in db.get_cwl_assignments_sync(event["id"])
    }

    # Cross-guild shared-clan roster merge (2026-08-15, slice 4: live shared roster) — a shared
    # clan's actual roster lives in cwl_shared_clan_players, not this guild's own
    # cwl_signups/cwl_assignments (which may hold stale pre-sharing data, or nothing at all if
    # this guild only joined the shared clan after another guild had already built up a roster
    # for it). Overrides (not just adds) any player_tag already in players_by_tag/
    # assigned_clan_by_tag from the normal per-guild sources above, so there's exactly one
    # source of truth per shared-clan player rather than two potentially-disagreeing ones —
    # "one shared record," the whole point of this feature (CWL_ROSTER_PLANNING_PLAN.md).
    for clan_tag, shared in shared_clan_by_tag.items():
        for shared_player in db.get_cwl_shared_clan_players_sync(shared["id"]):
            tag = shared_player["player_tag"]
            players_by_tag[tag] = {
                "player_tag": tag,
                "player_name": shared_player["player_name"],
                "discord_id": shared_player["discord_id"],
                "signup_status": shared_player["status"],
                "is_guest": shared_player["source"] == "guest_invite",
            }
            # A shared clan has no separate assignments table — one shared_clan_id already IS
            # exactly one clan (see cwl_shared_clan_players' own CREATE TABLE comment), so
            # status='confirmed' IS the assignment; anything else (pending/declined/withdrawn)
            # means not currently assigned.
            if shared_player["status"] == "confirmed":
                assigned_clan_by_tag[tag] = clan_tag
            else:
                assigned_clan_by_tag.pop(tag, None)

    # Fallback only for player_tags live_th_by_tag didn't cover (e.g. a signed-up player who has
    # since left every participating clan, so get_current_clan_members_sync no longer returns
    # them) — bounded to just this payload's own player_tags (never the whole war_attacks table,
    # see DATABASE_ARCHITECTURE.md's query anti-patterns).
    fallback_tags = [tag for tag in players_by_tag if tag not in live_th_by_tag]
    th_levels_by_tag = db.get_most_recent_th_levels_sync(fallback_tags)
    # Same idea, for current_clan_tag (2026-08-15 bugfix): get_current_clan_members_sync above is
    # clan-scoped to all_member_clan_tags, so a guest/account-wide-expanded player whose real
    # current clan is neither in the guild's family nor itself participating this season never
    # gets a current_clan_by_tag entry at all — their card was silently stuck plain/default
    # forever, never green ("moved") or amber ("hasn't moved yet"), which is exactly backwards
    # since these are the players a lead most needs that signal for. Player-scoped (not
    # clan-scoped) fallback picks them up regardless of which clan they're actually in.
    clan_fallback_tags = [tag for tag in players_by_tag if tag not in current_clan_by_tag]
    current_clan_fallback_by_tag = db.get_current_clan_tags_for_players_sync(clan_fallback_tags)
    skill_scores_by_tag = compute_league_adjusted_skill_scores(list(players_by_tag.keys()))
    avg_stars_by_tag = compute_avg_stars_per_attack(list(players_by_tag.keys()))
    for player_tag, player in players_by_tag.items():
        player["assigned_clan_tag"] = assigned_clan_by_tag.get(player_tag)
        th_level = live_th_by_tag.get(player_tag, th_levels_by_tag.get(player_tag))
        player["th_level"] = th_level
        player["th_icon_url"] = th_icon_url(th_level) if th_level is not None else None
        player["skill_score"] = skill_scores_by_tag.get(player_tag)
        player["avg_stars"] = avg_stars_by_tag.get(player_tag)
        # Default False for a player only known via an old cwl_signups row who's since left
        # every guild clan (get_current_clan_members_sync no longer covers them) — same
        # fallback-tolerant shape as the th_level lookup just above.
        player["cwl_permanent_optout"] = optout_by_tag.get(player_tag, False)
        # None only when truly unknown (no current_clan_tag on record anywhere) — lets the board
        # tell that apart from "currently in a different clan than their assignment"
        # (same-clan/different-clan highlighting, 2026-08-14).
        player["current_clan_tag"] = current_clan_by_tag.get(player_tag, current_clan_fallback_by_tag.get(player_tag))

    players = sorted(players_by_tag.values(), key=lambda p: (p["player_name"] or p["player_tag"]).lower())

    return {
        "season": season,
        "event_status": event["status"],
        "clans": clans,
        "players": players,
    }


async def _search_cwl_guests(guild_id: int, query: str) -> List[Dict[str, Any]]:
    """Unified fuzzy search for the "Guests" invite flow on Configure Participating Clans
    (2026-08-15, project owner's spec: "a discord user, a coc player name or a coc player_tag
    with intelligent sub-string match and auto-complete as far as possible") — one flat result
    list mixing clan hits and player hits (never nested), so the frontend renders one list with
    type badges instead of juggling three different shapes.

    Clan hits: substring match over CACHE.clan_name_cache — every clan tracked *anywhere* by
    this bot, not just this guild's own family (that's the whole point of a "guest" clan) —
    excluding any clan already participating in this guild's currently-selected event. A clan
    already participating in ANOTHER guild's event for the same season is still shown (never
    hidden — 2026-08-15, project owner's spec: "the user should be informed... and asked if he
    would like to add the clan to the own guild's clan roster nevertheless"), annotated with
    `already_shared_with` (that other guild's display name, or its raw ID if unresolvable) so
    the frontend can highlight it and prompt for confirmation before adding.

    Player hits: two match paths merged and deduped by player_tag —
      1. CACHE.search_player_names() (name substring, already powers /whois) plus a direct tag
         match/prefix if `query` looks like a tag.
      2. Discord-account display-name substring over CACHE.user_accounts — each of THAT
         account's linked players becomes its own player hit (flattened, not nested), so
         searching a Discord username surfaces their character(s) directly, ready to add.
    Every player hit is cross-referenced against get_player_links_sync() so the frontend knows
    upfront whether "send DM now" is even possible for it (discord_id present) — a never-linked
    tag typed in directly still works as a hit with discord_id=null, just can't be DMed until its
    owner links a real account."""
    from qapbot.QBdiscocmdshelper_cwl import resolve_selected_cwl_season

    db = CACHE.db_manager
    query = (query or "").strip()
    if not query or db is None:
        return []

    guild_id_str = str(guild_id)
    already_participating: Set[str] = set()
    season = resolve_selected_cwl_season(guild_id)
    event = db.get_cwl_event_sync(guild_id_str, season)
    if event is not None:
        already_participating = {
            c["clan_tag"] for c in db.get_cwl_event_clans_sync(event["id"]) if c.get("participating", 1)
        }

    needle = query.lower()
    results: List[Dict[str, Any]] = []

    import QBcore

    for clan_tag, _info in CACHE.clan_name_cache.items():
        if clan_tag in already_participating:
            continue
        name = CACHE.get_clan_name(clan_tag, clan_tag) or clan_tag
        if needle in name.lower() or needle in clan_tag.lower():
            # Cross-guild claim check (2026-08-15) — reported, never used to hide the hit.
            already_shared_with = None
            other_claims = db.find_cwl_clan_participation_across_guilds_sync(
                clan_tag, season, exclude_guild_id=guild_id_str
            )
            if other_claims:
                other_guild = QBcore.bot.get_guild(int(other_claims[0]["guild_id"]))
                already_shared_with = other_guild.name if other_guild else other_claims[0]["guild_id"]
            # Same live-tier source _build_clan_config_payload uses for every other row (never
            # admin-set) — without this, a newly-added guest clan showed "—" for tier until the
            # next full page reload picked it up from the payload builder instead (live-testing
            # feedback, 2026-08-15).
            results.append({
                "type": "clan", "clan_tag": clan_tag, "clan_name": name,
                "clan_tier": CACHE.get_clan_war_league(clan_tag),
                "already_shared_with": already_shared_with,
            })

    player_hits: Dict[str, Dict[str, Any]] = {}
    for match in CACHE.search_player_names(query, limit=25):
        player_hits[match["player_tag"]] = {"player_tag": match["player_tag"], "player_name": match["player_name"]}
    if query.startswith("#"):
        upper_query = query.upper()
        for tag, name in CACHE.player_name_index.items():
            if tag.upper().startswith(upper_query):
                player_hits.setdefault(tag, {"player_tag": tag, "player_name": name})
        # A tag typed exactly that the index doesn't know about at all — still offered as a raw
        # hit (name falls back to the tag itself) so the admin can add it directly; whether it's
        # actually a real CoC tag is only found out once something tries to use it.
        if len(upper_query) >= 5 and upper_query not in player_hits:
            player_hits[upper_query] = {"player_tag": upper_query, "player_name": upper_query}

    for discord_id, account in CACHE.user_accounts.items():
        if discord_id == "UNASSIGNED":
            continue
        display_name = account.get("display_name") or ""
        if needle not in display_name.lower():
            continue
        for player in account.get("players", []):
            tag = player.get("player_tag")
            if not tag:
                continue
            player_hits.setdefault(tag, {"player_tag": tag, "player_name": player.get("player_name") or tag})

    if player_hits:
        links = db.get_player_links_sync(list(player_hits.keys()))
        for tag, hit in player_hits.items():
            link = links.get(tag, {})
            results.append({
                "type": "player",
                "player_tag": tag,
                "player_name": link.get("player_name") or hit["player_name"],
                "discord_id": link.get("discord_id"),
            })

    return results[:25]


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def handle_get_cwl_screen(request: web.Request) -> web.Response:
    """Returns the screen hint recorded by whichever Discord button most recently fired
    LAUNCH_ACTIVITY for this (guild, user) (CACHE.pending_cwl_activity_screen — see its field
    docstring in cache_manager.py for the full "no reliable way to pass this through Discord's
    own launch mechanism" design history). No admin/leader re-check here — it reveals nothing
    sensitive (just which of two already permission-gated screens to fetch next), and the
    caller doesn't know which permission level even applies until this returns.

    2026-08-14: reads (not pops) the recorded value — it used to be popped on read, but Discord's
    "pop out" button (opens the Activity in its own separate window) re-runs main.ts's initial
    fetch sequence, including this call, a second time for the SAME logical launch. Popping meant
    that second call always found nothing left and fell back to "clan_config", landing the
    popped-out window on the wrong screen even though the user never clicked a different button.
    Reading non-destructively fixes that — the value now simply persists until the NEXT distinct
    button click overwrites it, which is exactly the "which screen was most recently requested"
    semantics this was always meant to have.
    """
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        guild_id_str = str(int(request.query["guild_id"]))
        discord_user_id_str = str(int(request.query["discord_user_id"]))
    except (KeyError, ValueError):
        return web.json_response({"error": "missing/invalid guild_id or discord_user_id"}, status=400)

    screen = CACHE.pending_cwl_activity_screen.get((guild_id_str, discord_user_id_str), "clan_config")
    return web.json_response({"screen": screen})


async def handle_get_cwl_enrollment(request: web.Request) -> web.Response:
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        guild_id = int(request.query["guild_id"])
        discord_user_id = int(request.query["discord_user_id"])
    except (KeyError, ValueError):
        return web.json_response({"error": "missing/invalid guild_id or discord_user_id"}, status=400)

    if not await _resolve_admin_or_leader(guild_id, discord_user_id):
        return web.json_response({"error": "not an admin or leader of this guild"}, status=403)

    return web.json_response(await _build_enrollment_payload(guild_id))


async def handle_post_cwl_enrollment_signup(request: web.Request) -> web.Response:
    """1-click sign-up/withdraw from the Manage Enrollment board. Never trusts client-supplied
    player_name/discord_id — both are resolved server-side (from the existing cwl_signups row if
    one exists, else from get_current_clan_members_sync(), matching start_cwl_enrollment()'s own
    resolution) rather than accepted from the request body, consistent with every other identity
    field in this feature never coming from the client."""
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
        guild_id = int(body["guild_id"])
        discord_user_id = int(body["discord_user_id"])
        player_tag = str(body["player_tag"])
        action = str(body["action"])
    except (KeyError, ValueError, TypeError):
        return web.json_response({"error": "invalid request body"}, status=400)
    if action not in ("confirm", "withdraw"):
        return web.json_response({"error": "action must be 'confirm' or 'withdraw'"}, status=400)

    if not await _resolve_admin_or_leader(guild_id, discord_user_id):
        return web.json_response({"error": "not an admin or leader of this guild"}, status=403)

    db = CACHE.db_manager
    if db is None:
        return web.json_response({"error": "database not ready"}, status=503)

    from datetime import datetime, timezone

    from qapbot.QBdiscocmdshelper_cwl import (
        get_event_shared_clans_by_tag_sync,
        resolve_selected_cwl_season,
        sync_cwl_shared_clan_roster_to_local_pools,
    )
    from qapbot.ui_cwl_roster import refresh_cwl_management_hub_message

    season = resolve_selected_cwl_season(guild_id)
    event = db.get_cwl_event_sync(str(guild_id), season)
    if event is None:
        return web.json_response(
            {"error": f"no CWL event exists yet for season {season}"}, status=409
        )

    # Cross-guild shared-clan write-path branch (2026-08-15, slice 4) — checked FIRST: a player
    # already sitting in a shared clan's roster may exist ONLY there (e.g. this guild's own
    # cwl_signups/get_current_clan_members_sync have never heard of them — they joined the
    # roster via the OTHER attached guild), so this also doubles as this branch's own
    # player_name/discord_id resolution rather than falling through to the normal one below and
    # 404ing on a player this guild's own tables genuinely don't know about.
    player_shared_clan = None
    shared_roster_row = None
    for shared in get_event_shared_clans_by_tag_sync(event["id"], season).values():
        match = next(
            (p for p in db.get_cwl_shared_clan_players_sync(shared["id"]) if p["player_tag"] == player_tag), None
        )
        if match is not None:
            player_shared_clan, shared_roster_row = shared, match
            break

    new_status = "confirmed" if action == "confirm" else "withdrawn"
    responded_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")

    if player_shared_clan is not None:
        db.upsert_cwl_shared_clan_player_sync(
            player_shared_clan["id"], player_tag, shared_roster_row["player_name"], shared_roster_row["discord_id"],
            new_status, "admin_added", str(guild_id), responded_at,
        )
        # De-sync guard (2026-08-15) — see sync_cwl_shared_clan_roster_to_local_pools's docstring:
        # keeps every OTHER attached guild's own local cwl_signups pool aware of this player too.
        sync_cwl_shared_clan_roster_to_local_pools(player_shared_clan["id"])
    else:
        existing = db.get_cwl_signup_sync(event["id"], player_tag)
        if existing is not None:
            player_name = existing["player_name"]
            discord_id = existing["discord_id"]
            preferred_league_rank = existing["preferred_league_rank"]
        else:
            all_clans = db.get_cwl_event_clans_sync(event["id"])
            participating_clan_tags = [c["clan_tag"] for c in all_clans if c.get("participating", 1)]
            member = next(
                (m for m in db.get_current_clan_members_sync(participating_clan_tags) if m["player_tag"] == player_tag),
                None,
            )
            if member is None:
                return web.json_response(
                    {"error": "player is not a current member of any participating clan"}, status=404
                )
            player_name = member["player_name"]
            discord_id = member["discord_id"]
            preferred_league_rank = member["preferred_league_rank"]

        db.upsert_cwl_signup_sync(
            event["id"], player_tag, player_name, discord_id, preferred_league_rank,
            "admin_added", new_status, responded_at=responded_at,
        )
        # A withdrawn player shouldn't linger assigned to a clan column.
        if action == "withdraw":
            db.delete_cwl_assignment_sync(event["id"], player_tag)

    try:
        await refresh_cwl_management_hub_message(guild_id, "cwl_management")
    except Exception as e:
        logging.warning(f"[WEB-BRIDGE] Saved signup but could not refresh the Hub message: {e}")

    return web.json_response({"ok": True})


async def handle_post_cwl_enrollment_assign(request: web.Request) -> web.Response:
    """Drag-and-drop move on the Manage Enrollment board. clan_tag: null means the card was
    dropped on the Unassigned column."""
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
        guild_id = int(body["guild_id"])
        discord_user_id = int(body["discord_user_id"])
        player_tag = str(body["player_tag"])
        clan_tag = body.get("clan_tag")
    except (KeyError, ValueError, TypeError):
        return web.json_response({"error": "invalid request body"}, status=400)

    if not await _resolve_admin_or_leader(guild_id, discord_user_id):
        return web.json_response({"error": "not an admin or leader of this guild"}, status=403)

    db = CACHE.db_manager
    if db is None:
        return web.json_response({"error": "database not ready"}, status=503)

    from qapbot.QBdiscocmdshelper_cwl import (
        get_event_shared_clans_by_tag_sync,
        purge_orphaned_shared_clan_guests_sync,
        resolve_guild_member_clan_tags,
        resolve_selected_cwl_season,
        sync_cwl_shared_clan_roster_to_local_pools,
    )
    from qapbot.ui_cwl_roster import refresh_cwl_management_hub_message

    season = resolve_selected_cwl_season(guild_id)
    event = db.get_cwl_event_sync(str(guild_id), season)
    if event is None:
        return web.json_response(
            {"error": f"no CWL event exists yet for season {season}"}, status=409
        )

    # Cross-guild shared-clan write-path branch (2026-08-15, slice 4) — a shared clan's roster
    # lives in cwl_shared_clan_players, not this guild's own cwl_assignments (see
    # _build_enrollment_payload's merge, which would just silently override a local write).
    shared_clans_by_tag = get_event_shared_clans_by_tag_sync(event["id"], season)

    # Being dragged away from a shared clan (to Unassigned, a private clan, or a DIFFERENT
    # shared clan) — remove the player from every OTHER shared clan they currently sit in first,
    # regardless of destination. "Other" because if clan_tag itself is the destination and also
    # shared, that case is upserted (not deleted) below.
    for tag, shared in shared_clans_by_tag.items():
        if tag == clan_tag:
            continue
        db.delete_cwl_shared_clan_player_sync(shared["id"], player_tag)
        # Foreign-guest purge (2026-08-15, project owner's spec) — this player just left `tag`'s
        # shared roster (whoever did it, wherever they went instead); any OTHER guild that had
        # cross-assigned this exact player into one of its OWN clans, on the strength of them
        # having been a real member of `tag`, loses that legitimacy the moment they leave — purge
        # it there too, not just here.
        purge_orphaned_shared_clan_guests_sync(shared["id"], player_tag)

    if clan_tag is not None and clan_tag in shared_clans_by_tag:
        # Destination is a shared clan — resolve player_name/discord_id the same way
        # handle_post_cwl_enrollment_signup does for a not-yet-signed-up player: prefer an
        # existing signup row, else fall back to current clan membership.
        existing_signup = db.get_cwl_signup_sync(event["id"], player_tag)
        if existing_signup is not None:
            player_name, discord_id = existing_signup["player_name"], existing_signup["discord_id"]
        else:
            candidate_tags = list(set(resolve_guild_member_clan_tags(guild_id)) | {clan_tag})
            member = next(
                (m for m in db.get_current_clan_members_sync(candidate_tags) if m["player_tag"] == player_tag),
                None,
            )
            player_name = member["player_name"] if member else player_tag
            discord_id = member["discord_id"] if member else None
        db.upsert_cwl_shared_clan_player_sync(
            shared_clans_by_tag[clan_tag]["id"], player_tag, player_name, discord_id,
            "confirmed", "admin_override", str(guild_id),
        )
        # De-sync guard (2026-08-15) — see sync_cwl_shared_clan_roster_to_local_pools's docstring:
        # keeps every OTHER attached guild's own local cwl_signups pool aware of this player too.
        sync_cwl_shared_clan_roster_to_local_pools(shared_clans_by_tag[clan_tag]["id"])
        # Clear any stale LOCAL assignment row too — this player's assignment now lives in the
        # shared table exclusively (see _build_enrollment_payload's merge, which reads the
        # shared table as authoritative regardless, but a lingering local row is dead weight).
        db.delete_cwl_assignment_sync(event["id"], player_tag)
    elif clan_tag is None:
        db.delete_cwl_assignment_sync(event["id"], player_tag)
    else:
        db.upsert_cwl_assignment_sync(
            event["id"], player_tag, str(clan_tag), assignment_source="admin_override", locked=True
        )

    try:
        await refresh_cwl_management_hub_message(guild_id, "cwl_management")
    except Exception as e:
        logging.warning(f"[WEB-BRIDGE] Saved assignment but could not refresh the Hub message: {e}")

    return web.json_response({"ok": True})


async def handle_get_cwl_guest_search(request: web.Request) -> web.Response:
    """Backs the "Guests" search box on Configure Participating Clans — see
    _search_cwl_guests()'s docstring for the actual matching logic. Admin-only, same as every
    other route here; a plain empty-results response (not an error) for a query with no hits."""
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        guild_id = int(request.query["guild_id"])
        discord_user_id = int(request.query["discord_user_id"])
    except (KeyError, ValueError):
        return web.json_response({"error": "missing/invalid guild_id or discord_user_id"}, status=400)
    query = request.query.get("q", "")

    if not await _resolve_admin(guild_id, discord_user_id):
        return web.json_response({"error": "not an admin of this guild"}, status=403)

    return web.json_response({"results": await _search_cwl_guests(guild_id, query)})


async def handle_post_cwl_enrollment_guest(request: web.Request) -> web.Response:
    """Adds one guest PLAYER directly to the current season's cwl_signups — bypassing the "must
    already be a current member of a participating clan" restriction
    handle_post_cwl_enrollment_signup enforces, since the whole point of a guest is that they
    aren't. Guest CLANS never go through this endpoint at all — they're added straight into
    cwl_event_clans via the existing POST /cwl/clan-config save, same as any other clan (see
    _search_cwl_guests's docstring for why that needs no new persistence of its own)."""
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
        guild_id = int(body["guild_id"])
        discord_user_id = int(body["discord_user_id"])
        player_tag = str(body["player_tag"]).upper()
        player_name = str(body.get("player_name") or player_tag)
        guest_discord_id = body.get("discord_id")
        send_dm_on_save = bool(body.get("send_dm_on_save", False))
    except (KeyError, ValueError, TypeError):
        return web.json_response({"error": "invalid request body"}, status=400)

    if not await _resolve_admin(guild_id, discord_user_id):
        return web.json_response({"error": "not an admin of this guild"}, status=403)

    db = CACHE.db_manager
    if db is None:
        return web.json_response({"error": "database not ready"}, status=503)

    from qapbot.QBdiscocmdshelper_cwl import resolve_selected_cwl_season, send_cwl_signup_template_dm
    from qapbot.ui_cwl_roster import refresh_cwl_management_hub_message

    season = resolve_selected_cwl_season(guild_id)
    event = db.get_cwl_event_sync(str(guild_id), season)
    if event is None:
        return web.json_response(
            {"error": f"no CWL event exists yet for season {season}"}, status=409
        )

    db.upsert_cwl_signup_sync(
        event["id"], player_tag, player_name, guest_discord_id, None,
        source="guest_invite", status="pending",
    )

    dm_sent = False
    if send_dm_on_save and guest_discord_id:
        # Deliberately bypasses CONFIG.cwl_dm_restrict_to_admin — that guard exists to stop an
        # accidental BULK blast while the feature is under live-testing; this is one admin
        # deliberately choosing to DM one specific person right now, same category of action as
        # /admin's TEST_NOTIFY, not the kind of blast the guard was built to catch.
        dm_sent = await send_cwl_signup_template_dm(event["id"], guild_id, season, {
            "player_tag": player_tag, "player_name": player_name, "discord_id": guest_discord_id,
        })

    try:
        await refresh_cwl_management_hub_message(guild_id, "cwl_management")
    except Exception as e:
        logging.warning(f"[WEB-BRIDGE] Saved guest but could not refresh the Hub message: {e}")

    return web.json_response({"ok": True, "dm_sent": dm_sent})


async def handle_get_clan_config(request: web.Request) -> web.Response:
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        guild_id = int(request.query["guild_id"])
        discord_user_id = int(request.query["discord_user_id"])
    except (KeyError, ValueError):
        return web.json_response({"error": "missing/invalid guild_id or discord_user_id"}, status=400)

    if not await _resolve_admin(guild_id, discord_user_id):
        return web.json_response({"error": "not an admin of this guild"}, status=403)

    return web.json_response(await _build_clan_config_payload(guild_id))


async def handle_post_clan_config(request: web.Request) -> web.Response:
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
        guild_id = int(body["guild_id"])
        discord_user_id = int(body["discord_user_id"])
        clans_in = body["clans"]
    except (KeyError, ValueError, TypeError):
        return web.json_response({"error": "invalid request body"}, status=400)

    if not await _resolve_admin(guild_id, discord_user_id):
        return web.json_response({"error": "not an admin of this guild"}, status=403)

    db = CACHE.db_manager
    if db is None:
        return web.json_response({"error": "database not ready"}, status=503)

    from qapbot.QBdiscocmdshelper_cwl import (
        auto_assign_prior_cwl_members_if_empty,
        detach_guild_from_shared_clan_on_deactivation,
        ensure_cwl_clan_sharing,
        resolve_selected_cwl_season,
    )
    from qapbot.ui_cwl_roster import notify_cwl_clan_shared, refresh_cwl_management_hub_message

    # Never creates a season itself (CWL_CLAN_CONFIG_ACTIVITY_PLAN.md Phase E.3: that's
    # exclusively "Add New Season"'s job) — the guild's currently-selected season must already
    # have an event, or this is a stale/pre-bootstrap request and the admin needs to use
    # "Add New Season" in Discord first.
    season = resolve_selected_cwl_season(guild_id)
    event = db.get_cwl_event_sync(str(guild_id), season)
    if event is None:
        return web.json_response(
            {"error": f"no CWL event exists yet for season {season} — use \"Add New Season\" in Discord first"},
            status=409,
        )
    event_id = event["id"]

    # Snapshot of what was participating BEFORE this save — the cross-guild sharing check below
    # (2026-08-15) only needs to run for clans newly turning on, not every clan on every save.
    previously_participating = {
        c["clan_tag"] for c in db.get_cwl_event_clans_sync(event_id) if c.get("participating", 1)
    }

    # Same shape create_cwl_event_sync()/set_cwl_event_clans_sync() already expect.
    # Every clan the frontend sent is persisted, participating or not — set_cwl_event_clans_sync()
    # drops any clan omitted entirely, so a deactivated clan must still be included (with
    # participating=False) or its roster_size/cwl_start_at would be lost, not just hidden.
    clan_configs = [
        {
            "clan_tag": c["clan_tag"],
            "target_league_rank": CACHE.get_clan_war_league(c["clan_tag"]),
            "roster_size": int(c.get("roster_size", 15)),
            "tier_order": idx,
            "cwl_start_at": c.get("cwl_start_at"),
            "participating": bool(c.get("participating", False)),
        }
        for idx, c in enumerate(clans_in)
    ]
    # Cross-guild shared-clan settings guard (2026-08-15 follow-up, project owner's spec) — a
    # clan already shared with another guild has exactly ONE canonical roster_size/cwl_start_at/
    # target_league_rank: the OWNER's. A non-owner guild's own form must never be able to persist
    # a divergent value for these (even from a stale UI that briefly still showed them editable)
    # — only `participating` (this guild's own opt-in/out) is genuinely per-guild. Only clans
    # already shared BEFORE this save are affected; a clan newly becoming shared as a *result* of
    # this save (handled below) has no owner row yet to defer to, so its freshly-submitted values
    # become the initial canonical ones instead.
    guild_id_str = str(guild_id)
    for cfg in clan_configs:
        shared = db.get_cwl_shared_clan_sync(cfg["clan_tag"], season)
        if shared is None or shared["owner_guild_id"] == guild_id_str:
            continue
        owner_row = next(
            (r for r in db.get_cwl_event_clans_sync(shared["owner_event_id"]) if r["clan_tag"] == cfg["clan_tag"]), None
        )
        if owner_row is not None:
            cfg["roster_size"] = owner_row["roster_size"]
            cfg["cwl_start_at"] = owner_row["cwl_start_at"]
            cfg["target_league_rank"] = owner_row["target_league_rank"]

    db.set_cwl_event_clans_sync(event_id, clan_configs)

    # Cross-guild shared-clan check (2026-08-15, project owner's spec) — the first of the two
    # trigger points (the other is start_cwl_enrollment). The frontend's Guests search already
    # highlighted an already-claimed clan and had the admin confirm adding it anyway (see
    # _search_cwl_guests's docstring) — this is the mechanical step that actually establishes
    # the shared record now that the save is going through. ensure_cwl_clan_sharing() is a
    # cheap no-op for the overwhelming majority of clans that aren't shared with anyone.
    newly_participating_tags = [
        c["clan_tag"] for c in clan_configs
        if c["participating"] and c["clan_tag"] not in previously_participating
    ]
    for clan_tag in newly_participating_tags:
        sharing_result = await ensure_cwl_clan_sharing(guild_id, event_id, season, clan_tag)
        if sharing_result is not None:
            try:
                await notify_cwl_clan_shared(guild_id, clan_tag, season, sharing_result, acting_discord_id=discord_user_id)
            except Exception as e:
                logging.warning(f"[WEB-BRIDGE] Clan {clan_tag} became shared but notification failed: {e}")

        # Auto-assign-on-add (2026-08-15, live-testing feedback, project owner's spec) — only
        # once enrollment has actually started (a still-draft event is about to get a
        # comprehensive seed from Start Enrollment itself, which already covers this).
        if event["status"] != "draft":
            try:
                await auto_assign_prior_cwl_members_if_empty(guild_id, event_id, season, clan_tag)
            except Exception as e:
                logging.warning(f"[WEB-BRIDGE] Clan {clan_tag} added but prior-CWL auto-assign failed: {e}")

    # Cross-guild shared-clan detach-on-deactivate (2026-08-15, live-testing feedback) — the
    # inverse of the block above: a clan turning OFF this save that's currently shared must
    # detach this guild from cwl_shared_clan_guilds (and repoint ownership if this guild was the
    # owner), so a deactivated guild doesn't stay listed as "still sharing" indefinitely.
    newly_deactivated_tags = [
        c["clan_tag"] for c in clan_configs
        if not c["participating"] and c["clan_tag"] in previously_participating
    ]
    for clan_tag in newly_deactivated_tags:
        try:
            await detach_guild_from_shared_clan_on_deactivation(guild_id, event_id, season, clan_tag)
        except Exception as e:
            logging.warning(f"[WEB-BRIDGE] Clan {clan_tag} deactivated but shared-clan detach failed: {e}")

    # Best-effort — a failure here shouldn't fail the save itself (matches how Discord-side
    # callbacks treat repost/refresh failures as logged-not-raised).
    try:
        await refresh_cwl_management_hub_message(guild_id, "cwl_management")
    except Exception as e:
        logging.warning(f"[WEB-BRIDGE] Saved clan-config but could not refresh the Hub message: {e}")

    return web.json_response({"ok": True, "event_id": event_id})


async def handle_post_cwl_shared_clan_evict(request: web.Request) -> web.Response:
    """Owner-only eviction (2026-08-15, project owner's spec) — removes target_guild_id's
    participation in a shared clan. Two permission layers: the standard admin-of-guild_id check
    every route here has, PLUS evict_guild_from_shared_clan()'s own ownership check (only the
    current OWNER guild may evict another guild, not just any admin of any attached guild)."""
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
        guild_id = int(body["guild_id"])
        discord_user_id = int(body["discord_user_id"])
        clan_tag = str(body["clan_tag"])
        target_guild_id = int(body["target_guild_id"])
    except (KeyError, ValueError, TypeError):
        return web.json_response({"error": "invalid request body"}, status=400)

    if not await _resolve_admin(guild_id, discord_user_id):
        return web.json_response({"error": "not an admin of this guild"}, status=403)

    db = CACHE.db_manager
    if db is None:
        return web.json_response({"error": "database not ready"}, status=503)

    from qapbot.QBdiscocmdshelper_cwl import evict_guild_from_shared_clan, resolve_selected_cwl_season
    from qapbot.ui_cwl_roster import refresh_cwl_management_hub_message

    season = resolve_selected_cwl_season(guild_id)
    result = await evict_guild_from_shared_clan(guild_id, target_guild_id, clan_tag, season)
    if not result["ok"]:
        status = 403 if result["error"] == "not_owner" else 400
        return web.json_response({"error": result["error"]}, status=status)

    try:
        await refresh_cwl_management_hub_message(guild_id, "cwl_management")
    except Exception as e:
        logging.warning(f"[WEB-BRIDGE] Evicted guild {target_guild_id} from shared clan {clan_tag} but could not refresh the Hub message: {e}")

    return web.json_response({"ok": True})


def create_app() -> web.Application:
    app = web.Application(middlewares=[_access_log_middleware])
    app.router.add_get("/api/health", handle_health)
    app.router.add_get("/api/cwl/clan-config", handle_get_clan_config)
    app.router.add_post("/api/cwl/clan-config", handle_post_clan_config)
    app.router.add_get("/api/cwl/screen", handle_get_cwl_screen)
    app.router.add_get("/api/cwl/enrollment", handle_get_cwl_enrollment)
    app.router.add_post("/api/cwl/enrollment/signup", handle_post_cwl_enrollment_signup)
    app.router.add_post("/api/cwl/enrollment/assign", handle_post_cwl_enrollment_assign)
    app.router.add_get("/api/cwl/guest-search", handle_get_cwl_guest_search)
    app.router.add_post("/api/cwl/enrollment/guest", handle_post_cwl_enrollment_guest)
    app.router.add_post("/api/cwl/shared-clan/evict", handle_post_cwl_shared_clan_evict)
    return app


async def start_web_bridge() -> None:
    """Start the CWL clan-config web bridge if configured (WEB_BRIDGE_PORT and
    WEB_BRIDGE_SECRET both set) — silently does nothing otherwise, so guilds/deployments that
    haven't opted into this feature see no behavior change. Call once from QapBot.py's
    _setup_hook(). Bound to 127.0.0.1 only — see the module docstring.
    """
    global _runner
    from qapbot.config import CONFIG

    if not CONFIG.web_bridge_port or not CONFIG.web_bridge_secret:
        logging.info("[WEB-BRIDGE] Not starting — WEB_BRIDGE_PORT/WEB_BRIDGE_SECRET not both configured")
        return

    app = create_app()
    # access_log=None: the custom _access_log_middleware above replaces aiohttp's default
    # combined-log-format line (IP, duplicate embedded timestamp, raw numeric IDs) with one that
    # names the guild/user instead — this just stops it from also logging the old line.
    _runner = web.AppRunner(app, access_log=None)
    await _runner.setup()
    site = web.TCPSite(_runner, "127.0.0.1", CONFIG.web_bridge_port)
    await site.start()
    logging.info(f"[WEB-BRIDGE] Listening on 127.0.0.1:{CONFIG.web_bridge_port}")


async def stop_web_bridge() -> None:
    """Clean shutdown counterpart to start_web_bridge() — safe to call even if it never
    started (no-op)."""
    global _runner
    if _runner is not None:
        await _runner.cleanup()
        _runner = None
