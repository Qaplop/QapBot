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

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

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

    # Third access tier (2026-08-30, project owner's spec): a standing CWL Coordinator of a clan
    # participating in this season. Checked LAST because it's the only tier needing DB reads —
    # admin and role checks are in-memory, so the common case never pays for this.
    from qapbot.QBdiscocmdshelper_cwl import is_cwl_coordinator_for_current_season

    return await asyncio.to_thread(is_cwl_coordinator_for_current_season, guild_id, discord_user_id)


# Enrollment-board version counter + notification primitive (2026-08-17,
# CWL_PROD_PERFORMANCE_FIX_PLAN.md P1 Step 8) — backs GET /api/cwl/enrollment/wait's long-poll,
# replacing the client's old fixed 12s setInterval poll with an event-triggered one. Keyed by
# guild_id (as str, matching the rest of this module's convention). A guild's entry is created
# lazily on first touch (either a write or a wait) — untouched guilds cost nothing. Also doubles
# as the reconnect/resync mechanism after a bot restart or tunnel blip: a restart resets every
# counter to 0 in-memory, which a stale client's nonzero `known_version` will never match, so it
# gets exactly one extra "changed" response and one refetch — no special-case restart handling
# needed, an in-memory reset just looks like an ordinary change.
_enrollment_version: Dict[str, int] = {}
_enrollment_changed: Dict[str, asyncio.Condition] = {}
_enrollment_waiter_counts: Dict[str, int] = {}
_ENROLLMENT_WAIT_TIMEOUT_SECONDS = 25.0
# Cloudflare's ~100s idle timeout and the Discord Activity proxy both stay comfortably clear of
# this 25s hold; see CWL_PROD_PERFORMANCE_FIX_PLAN.md Step 8 for the empirical DEV verification
# this constant depends on (lower it here, with a comment recording the measured limit, if any
# hop in the Discord proxy -> Worker -> cloudflared -> aiohttp chain turns out to cut it short).
_ENROLLMENT_WAIT_MAX_WAITERS_PER_GUILD = 10


def _enrollment_condition_for(guild_id_str: str) -> asyncio.Condition:
    cond = _enrollment_changed.get(guild_id_str)
    if cond is None:
        cond = asyncio.Condition()
        _enrollment_changed[guild_id_str] = cond
        _enrollment_version.setdefault(guild_id_str, 0)
    return cond


def get_enrollment_version(guild_id: int) -> int:
    """Read-only — used by _build_enrollment_payload_sync (a worker-thread sync core) to stamp
    its own response with the version the wait loop should start from. A plain dict read is
    GIL-safe from a worker thread; nothing here does a Python-level callback mid-read."""
    return _enrollment_version.get(str(guild_id), 0)


async def bump_enrollment_version(guild_id: Optional[int] = None) -> None:
    """Call this immediately after any write that changes what the enrollment payload renders —
    every existing refresh_cwl_management_hub_message()/_refresh_parent(..., "cwl_management")
    call site in this module and in qapbot/ui_cwl_roster.py is exactly the right hook point (they
    already mark "board-visible state changed"), so this is called right alongside each of them.

    guild_id=None bumps EVERY currently-tracked guild (only guilds someone has actually opened
    the board for, or that have been bumped before — never a wasted bump for a guild nobody is
    watching) — the deliberately simple fallback for the cross-guild shared-clan case (a write in
    guild A can change guild B's board) at call sites where resolving the exact partner guild set
    would need extra plumbing/queries; a spurious wake-up only when a write happened anywhere is
    still an enormous reduction versus the old fixed-interval poll (CWL_PROD_PERFORMANCE_FIX_PLAN.md
    Step 8 design notes). Call sites that already know the specific partner guild(s) cheaply
    (e.g. the shared-clan evict endpoint's explicit target_guild_id) bump those directly instead —
    see each call site's own comment.

    Always awaited from the event loop, never from inside a to_thread sync core — every call site
    bumps AFTER its own to_thread hop returns, so asyncio.Condition (loop-only) is never touched
    off-loop."""
    guild_id_strs = [str(guild_id)] if guild_id is not None else list(_enrollment_version.keys())
    for guild_id_str in guild_id_strs:
        new_version = _enrollment_version.get(guild_id_str, 0) + 1
        _enrollment_version[guild_id_str] = new_version
        cond = _enrollment_changed.get(guild_id_str)
        waiters = _enrollment_waiter_counts.get(guild_id_str, 0)
        logging.debug(
            f"[WEB-BRIDGE] bump_enrollment_version guild={guild_id_str} -> version={new_version} "
            f"(parked_waiters={waiters}, tracked={cond is not None})"
        )
        if cond is not None:
            async with cond:
                cond.notify_all()


def _build_clan_config_payload_sync(guild_id: int) -> Dict[str, Any]:
    """Build the GET response for whichever season is currently selected on the guild's CWL
    Management screen (the season select there, CWL_CLAN_CONFIG_ACTIVITY_PLAN.md Phase E.3) —
    this Activity has no season picker of its own; it always just shows/edits that one season.

    A clan with no row yet for that season defaults to roster_size=15, cwl_start_at=the 1st of
    the season's month at 08:00 UTC (the game's static schedule), participating=False.

    Plain synchronous function (2026-08-17, Pitfall 26, COPILOT_PITFALLS_COOKBOOK.md — the rule
    covers READ paths too, not just writes) — every DB/CACHE call in its body is already
    synchronous with no `await` between any of it, so the whole thing is one atomic
    asyncio.to_thread() hop from its only caller, handle_get_clan_config.
    """
    from qapbot.QBdiscocmdshelper import get_guild_clans_including_member_config
    from qapbot.QBdiscocmdshelper_cwl import cwl_league_rank, resolve_guild_member_clan_tags, resolve_selected_cwl_season

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

    # is_guest (2026-08-18, CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md rule f): drives the
    # frontend's "Remove" button, which must NEVER be offered for a real family clan — deliberately
    # resolve_guild_member_clan_tags(), the same CWL-family definition used everywhere else in this
    # feature (start_cwl_enrollment, the cleanup protection logic), not the broader
    # get_guild_clans_including_member_config() above (that one also includes a clan this guild
    # merely subscribes to for war tracking, which is not the same thing as "part of the family").
    family_clan_tags = set(resolve_guild_member_clan_tags(guild_id))

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

        # Cross-guild shared-clan status (2026-08-15, follow-up fix 2026-08-16: this used to be
        # gated on `participating` too — "only meaningful for a currently participating clan" —
        # but a clan's actual cwl_shared_clans record doesn't care whether THIS guild currently
        # has it checked or not; it's the same real-world clan either way. Gating on
        # `participating` meant re-checking a previously-shared-but-deactivated row's checkbox
        # client-side (which only ever flips `clan.participating` locally, never re-fetches)
        # showed no shared/read-only info at all until the NEXT full reload after a Save —
        # live-testing feedback: "re-enabling should check this and if valid also show the guest
        # clan warning and disable the clan settings," without needing an intervening save
        # first. Computed for any KNOWN row (participating or not) instead — a never-configured
        # clan (row is None) still has nothing to share, same as before.
        shared_with: Optional[Dict[str, Any]] = None
        effective_row = row
        if db is not None:
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
            "is_guest": tag not in family_clan_tags,
        })

    return {
        "season": season,
        "event_status": event["status"] if event else None,
        "clans": clans,
    }


def _build_enrollment_payload_sync(guild_id: int) -> Dict[str, Any]:
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
    with no special marker — see _search_cwl_guests_sync()/handle_post_clan_config() — so its roster
    is pulled into the pool below via participating_clan_tags, unioned with the family pool. A
    guest *player* (an individual invited directly, possibly from a clan that isn't participating
    at all) is a plain cwl_signups row with source='guest_invite', surfaced via each player's
    `is_guest` flag — see handle_post_cwl_enrollment_guest().

    2026-08-15 (cross-guild shared clans, slice 4 — live shared roster): a participating clan
    that's actually shared with another guild (cwl_shared_clans) gets its roster_size from the
    OWNER guild's own cwl_event_clans row, not this guild's possibly-vestigial one, and its
    player pool from cwl_shared_clan_players instead of this guild's own cwl_signups/
    cwl_assignments — so both guilds' boards render the identical live roster. See the merge
    blocks below (marked 2026-08-15) for exactly where each override happens.

    Plain synchronous function (2026-08-17, Pitfall 26, COPILOT_PITFALLS_COOKBOOK.md — the rule
    covers READ paths too, not just writes): every DB/CACHE call and skill-score/avg-stars pass
    below is already synchronous with no `await` between any of it, so the whole thing is one
    atomic asyncio.to_thread() hop from its only caller, handle_get_cwl_enrollment."""
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
            "version": get_enrollment_version(guild_id),
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
        # Lock state (2026-08-30): the board renders a locked column read-only and blocks drops
        # into it, except for the players who were already in the clan when its roster locked.
        # Sent as an explicit tag list rather than a flag per player so the frontend can answer
        # "may this specific card go here?" locally, without a round trip per drag.
        locked_at = c.get("locked_at")
        clans.append({
            "clan_tag": c["clan_tag"],
            "name": CACHE.get_clan_name(c["clan_tag"], c["clan_tag"]),
            "tier": _tier_for(c),
            "roster_size": effective_row["roster_size"],
            "locked_at": locked_at,
            "eligible_player_tags": (
                sorted(db.get_cwl_locked_clan_member_tags_sync(season, c["clan_tag"]))
                if locked_at else []
            ),
        })

    players_by_tag: Dict[str, Dict[str, Any]] = {}
    # cwl_signups.preferred_league_rank — a frozen, one-time COPY of the standing default taken
    # when Start Enrollment first seeded this row, NOT a genuine distinct per-invite answer (tracker
    # #0058 correction: no DM/response flow ever actually asks the player this question; see this
    # value's use below for the full explanation and why it's now only the LAST-resort fallback).
    # Kept in its own map (tracker #0057) rather than directly on players_by_tag, since
    # cwl_shared_clan_players has no equivalent column at all (see the shared-clan merge block
    # below) and the live-default fallback (links_by_tag, below) needs to apply uniformly
    # regardless of source.
    signup_preferred_league_by_tag: Dict[str, Optional[str]] = {}
    for signup in db.get_cwl_signups_for_event_sync(event["id"]):
        players_by_tag[signup["player_tag"]] = {
            "player_tag": signup["player_tag"],
            "player_name": signup["player_name"],
            "discord_id": signup["dmed_discord_id"],
            "signup_status": signup["status"],
            # source='guest_invite' is set by handle_post_cwl_enrollment_guest — the only writer
            # that uses it (every other signup path uses 'template_confirm'/'admin_added'/etc.).
            # Badge-only on the frontend; doesn't change any pool/eligibility logic.
            "is_guest": signup["source"] == "guest_invite",
        }
        signup_preferred_league_by_tag[signup["player_tag"]] = signup.get("preferred_league_rank")
    # user_players.th_level (2026-08-14: now kept fresh for every current member, linked or not
    # — see coc_cache.py's update_player_info_in_user_accounts) is the primary TH source: live
    # from the CoC API, not dependent on the player ever having made a tracked war attack.
    # Pool is every guild member clan PLUS every clan actually participating this season
    # (2026-08-15: a guest clan invited via the "Guests" search on Configure Participating Clans
    # is added straight into cwl_event_clans like any other clan — see CWL_ROSTER_PLANNING_PLAN.md
    # — but isn't part of the guild's own family, so resolve_guild_member_clan_tags() alone would
    # never surface its roster here; the union is what actually gives it "full participating
    # clan" treatment: a board column from `clans` above, and its live members in the pool here).
    family_clan_tags = set(resolve_guild_member_clan_tags(guild_id))
    all_member_clan_tags = list(family_clan_tags | set(participating_clan_tags))
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

    # A local assignment pointing at a clan tag that ISN'T currently a column here needs to be
    # told apart (2026-08-19, live bug report: unchecking Hohenloher Land — a plain guest clan
    # tracked by no other guild — rendered its 41 members in the "Assigned to other Guild"
    # pseudo-column even though no other guild is involved at all). Two real cases can produce
    # this: (1) a genuinely cross-guild SHARED clan — even one this guild just detached from via
    # detach_guild_from_shared_clan_on_deactivation, which deliberately mirrors that state into a
    # local row so the player doesn't vanish (see that function's own "Orphaned-assignment
    # preservation" comment) — where "Assigned to other Guild" is the correct, real label; (2) a
    # clan this guild simply UNCHECKED via the plain Configure Participating Clans checkbox, which
    # is purely cosmetic and leaves cwl_assignments untouched (rule f,
    # CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md) — no other guild is involved, so the dormant
    # local row must not be surfaced as if one were; the player should render as Unassigned until
    # the clan is re-checked, at which point the untouched row brings them straight back.
    # get_cwl_shared_clan_sync is the same "is this REALLY a cross-guild clan" signal used
    # throughout this feature (e.g. _prepare_and_save_clan_config_sync above).
    # A local assignment pointing at a clan tag that ISN'T currently a column here needs to be
    # told apart (2026-08-19, live bug report: unchecking Hohenloher Land — a plain guest clan
    # tracked by no other guild — rendered its 41 members in the "Assigned to other Guild"
    # pseudo-column even though no other guild is involved at all). Two real cases can produce
    # this: (1) a genuinely cross-guild SHARED clan — even one this guild just detached from via
    # detach_guild_from_shared_clan_on_deactivation, which deliberately mirrors that state into a
    # local row so the player doesn't vanish (see that function's own "Orphaned-assignment
    # preservation" comment) — where "Assigned to other Guild" is the correct, real label; (2) a
    # clan this guild simply UNCHECKED via the plain Configure Participating Clans checkbox, which
    # is purely cosmetic and leaves cwl_assignments untouched (rule f,
    # CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md) — no other guild is involved, so the dormant
    # local row must not be surfaced as if one were; the player should render as Unassigned until
    # the clan is re-checked, at which point the untouched row brings them straight back.
    # get_cwl_shared_clan_sync is the same "is this REALLY a cross-guild clan" signal used
    # throughout this feature (e.g. _prepare_and_save_clan_config_sync above).
    participating_clan_tag_set = set(participating_clan_tags)
    for tag, clan_tag in list(assigned_clan_by_tag.items()):
        if clan_tag in participating_clan_tag_set:
            continue
        if db.get_cwl_shared_clan_sync(clan_tag, season) is not None:
            continue
        del assigned_clan_by_tag[tag]

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
                "discord_id": shared_player["dmed_discord_id"],
                # The RAW status, unconditionally (2026-08-16, live-testing feedback, project
                # owner's spec, verbatim: "Confirmation status and assignment status should be
                # treated completely separate... The symbols in the player tile should exclusively
                # reflect confirmation status. The assignment status is obvious to the user from
                # the column the player tile appears in."). `status` and `assigned` are now
                # genuinely independent columns (see cwl_shared_clan_players' own CREATE TABLE
                # comment) — an assignment/placement write (drag-and-drop, the auto-assign seed)
                # never touches `status` at all, so this always reflects a real player response
                # (or the honest 'pending' default) with nothing left to correct for here.
                "signup_status": shared_player["status"],
                "is_guest": shared_player["source"] == "guest_invite",
            }
            # Placement is driven by `assigned`, not `status` (2026-08-16 follow-up — the two
            # used to be conflated into one column precisely because a shared clan has no
            # separate assignments table; they're now genuinely separate columns instead).
            if shared_player["assigned"]:
                assigned_clan_by_tag[tag] = clan_tag
            else:
                assigned_clan_by_tag.pop(tag, None)

    # Cross-guild PRIVATE placement mirror (2026-08-20, live bug report, project owner: "Killer
    # and Qaplop show up in unassigned pool instead of being highlighted as being assigned already
    # in another guild" — a player pooled here via cwl_enrollment_include_all_linked_accounts'
    # account-wide expansion, who is ALREADY deliberately placed in a DIFFERENT guild's own
    # (private, non-shared) CWL roster this season. That real placement lives purely in the other
    # guild's own cwl_assignments — this guild has no local record of it at all, so it rendered as
    # plain Unassigned instead of the "Assigned to other Guild" pseudo-column a cross-guild SHARED
    # clan placement already gets via the merge just above. Only checked for players still missing
    # a local assignment after every other source above — a real local/shared placement always
    # wins outright, this is purely a display fallback for "we don't have one, does someone else."
    # Batched (find_cwl_players_private_placements_in_other_guilds_sync), never once per player —
    # the same function handle_post_cwl_enrollment_guest and assign_cwl_player_sync already use
    # (singly) to REFUSE creating this exact conflict in the first place; this is that same signal
    # reused to correctly DISPLAY a conflict that already existed before either guard ran (e.g.
    # seeded via account-wide expansion, never through the guest-invite endpoint at all).
    unresolved_tags = [tag for tag in players_by_tag if tag not in assigned_clan_by_tag]
    other_guild_placements = db.find_cwl_players_private_placements_in_other_guilds_sync(
        unresolved_tags, season, str(guild_id)
    )
    for tag, placement in other_guild_placements.items():
        assigned_clan_by_tag[tag] = placement["clan_tag"]

    # Fallback chain for player_tags live_th_by_tag didn't cover (e.g. a signed-up player who has
    # since left every participating clan, so get_current_clan_members_sync no longer returns
    # them) — bounded to just this payload's own player_tags (never the whole war_attacks table,
    # see DATABASE_ARCHITECTURE.md's query anti-patterns). Two further sources, tried in recency
    # order (2026-08-20, live bug report: a player who left their tracked clan showed a blank TH
    # badge despite the bot already having their TH cached): user_players.th_level first — kept
    # fresh by every clan poll this bot runs, for ANY clan, not just this guild's own, so it's
    # almost always at least as current as a war_attacks row — then war_attacks history last, for
    # the rare player_tag with no user_players row at all (e.g. added via a search that only
    # confirmed a tag exists, never fetched/cached a full profile).
    fallback_tags = [tag for tag in players_by_tag if tag not in live_th_by_tag]
    cached_th_by_tag = db.get_cached_th_levels_for_players_sync(fallback_tags)
    war_fallback_tags = [tag for tag in fallback_tags if tag not in cached_th_by_tag]
    th_levels_by_tag = db.get_most_recent_th_levels_sync(war_fallback_tags)
    # Same idea, for current_clan_tag (2026-08-15 bugfix): get_current_clan_members_sync above is
    # clan-scoped to all_member_clan_tags, so a guest/account-wide-expanded player whose real
    # current clan is neither in the guild's family nor itself participating this season never
    # gets a current_clan_by_tag entry at all — their card was silently stuck plain/default
    # forever, never green ("moved") or amber ("hasn't moved yet"), which is exactly backwards
    # since these are the players a lead most needs that signal for. Player-scoped (not
    # clan-scoped) fallback picks them up regardless of which clan they're actually in.
    clan_fallback_tags = [tag for tag in players_by_tag if tag not in current_clan_by_tag]
    current_clan_fallback_by_tag = db.get_current_clan_tags_for_players_sync(clan_fallback_tags)
    # Live link + opt-out resolution for EVERY tag in the payload (2026-08-22, live bug report:
    # "B.A.B.A is still shown as unlinked in the teams management view while it is clearly linked
    # to user odin"). cwl_signups is an enrollment-time SNAPSHOT, written once by Start Enrollment
    # and never refreshed — but players_by_tag is seeded from it FIRST (above), and the live
    # get_current_clan_members_sync pass then deliberately skips any tag it already has. So an
    # account linked AFTER enrollment ran kept the snapshot's NULL discord_id forever (verified
    # live: #2RPLRVUG9 was linked 6h after the 2026-08-18 07:57 snapshot; 17 players on that one
    # board rendered grey), and an account re-linked to a different Discord user kept the OLD owner
    # forever (8 more on the same board). user_players is the only authority for "who owns this
    # account now", so this OVERRIDES rather than falls back — exactly as the is_guest rule below
    # overrides its write-path guess whenever a live current clan is known. A tag with no
    # user_players row at all (a guest tag added by search that was never linked) keeps whatever
    # the snapshot had, same fallback-tolerant shape as the th_level lookup above.
    # get_player_links_sync is the right helper: chunked (SQLite's ~999 host-parameter limit), and
    # it already applies the verified-wins + UNASSIGNED tiebreak and maps 'UNASSIGNED' -> None.
    # It also returns cwl_permanent_optout, which closes the identical clan-scoped blind spot in
    # optout_by_tag (populated above from get_current_clan_members_sync only, so a pooled player
    # who is in no member clan silently defaulted to False).
    links_by_tag = db.get_player_links_sync(list(players_by_tag.keys()))
    skill_scores_by_tag = compute_league_adjusted_skill_scores(list(players_by_tag.keys()))
    avg_stars_by_tag = compute_avg_stars_per_attack(list(players_by_tag.keys()))
    # 2026-08-29, project owner's spec (live bug report/clarification): "Not Invited Yet" means
    # "hasn't received the enrollment DM yet" — NOT "has no cwl_signups row". A 'pending' row
    # gets created for a player Start Enrollment/Notify New Pool Members merely INTENDED to DM
    # (seeded before the send so the DM's buttons have something to resolve against, per tracker
    # #0016), even when the actual send never went out (DM guard, blocked, left every mutual
    # guild, a transient failure) — dm_sent is the one signal that's only ever true once a DM
    # genuinely reached them, the same bulk lookup _send_cwl_enrollment_dm_batch()'s own dedup
    # check and the season overview's "New players without DM invitation" line already use, so
    # the board can finally agree with those instead of taking cwl_signups.status at face value.
    dm_sent_by_tag = db.get_cwl_player_season_dm_status_bulk_sync(list(players_by_tag.keys()), season)
    for player_tag, player in players_by_tag.items():
        player["assigned_clan_tag"] = assigned_clan_by_tag.get(player_tag)
        th_level = live_th_by_tag.get(
            player_tag, cached_th_by_tag.get(player_tag, th_levels_by_tag.get(player_tag))
        )
        player["th_level"] = th_level
        player["th_icon_url"] = th_icon_url(th_level) if th_level is not None else None
        player["skill_score"] = skill_scores_by_tag.get(player_tag)
        player["avg_stars"] = avg_stars_by_tag.get(player_tag)
        player["dm_sent"] = dm_sent_by_tag.get(player_tag, False)
        # Live link overrides the enrollment-time snapshot (see links_by_tag above).
        link = links_by_tag.get(player_tag)
        if link is not None:
            player["discord_id"] = link["discord_id"]
        # Display name for the tooltip (2026-08-22, live-testing feedback: "instead of showing
        # 'Linked' the tooltip could as well show the name of the linked discord user" — the board
        # already resolves discord_id above, it just never carried a human-readable name with it).
        # CACHE.user_accounts is the same in-memory source _search_cwl_guests_sync's
        # @-prefixed Discord-name search already reads from — no extra DB query needed.
        player["discord_display_name"] = (
            CACHE.user_accounts.get(str(player["discord_id"]), {}).get("display_name")
            if player["discord_id"] is not None else None
        )
        # Default False for a player only known via an old cwl_signups row who's since left
        # every guild clan (get_current_clan_members_sync no longer covers them) — same
        # fallback-tolerant shape as the th_level lookup just above. links_by_tag is consulted
        # second (2026-08-22): it covers player-scoped rows the clan-scoped optout_by_tag misses.
        player["cwl_permanent_optout"] = optout_by_tag.get(
            player_tag, bool(link["cwl_permanent_optout"]) if link is not None else False
        )
        # Live standing default wins over the frozen enrollment-time snapshot (tracker #0058
        # correction, 2026-08-29): despite tracker #0057's original framing, cwl_signups.
        # preferred_league_rank is NOT a genuine "this season's own answer" the player gives when
        # responding to the invite DM — no DM/response flow ever asks that question. It's purely a
        # one-time COPY of the standing default taken when Start Enrollment first seeded the row
        # (see the signups_to_create block in start_cwl_enrollment(), QBdiscocmdshelper_cwl.py) and
        # never refreshed after — confirm/decline (handle_cwl_signup_response, ui_cwl_roster.py)
        # just writes it straight back unchanged. Live bug report: a player who changed their
        # standing preference to "None" AFTER already responding still saw their old tier in the
        # tooltip forever, because the stale snapshot was checked first.
        #
        # `link is not None` (from links_by_tag, tracker #0059 — player-scoped, queried for every
        # tag regardless of clan, unlike the clan-scoped get_current_clan_members_sync pass above)
        # is the correct test for "is there live data at all", NOT `or` on the value itself: the
        # live value legitimately IS None for "no preference" (exactly Sir Lancelot's case above),
        # and `None or signup_value` would wrongly fall through to the stale snapshot in that case
        # — the same "can't tell false-because-unset from false-because-off" trap `or` always has.
        # Only a player with NO user_players row at all (never linked — a guest tag added by
        # search) has no live signal, and keeps the frozen snapshot as the sole surviving one.
        player["preferred_league_rank"] = (
            link["preferred_league_rank"] if link is not None else signup_preferred_league_by_tag.get(player_tag)
        )
        # None only when truly unknown (no current_clan_tag on record anywhere) — lets the board
        # tell that apart from "currently in a different clan than their assignment"
        # (same-clan/different-clan highlighting, 2026-08-14).
        current_clan_tag = current_clan_by_tag.get(player_tag, current_clan_fallback_by_tag.get(player_tag))
        player["current_clan_tag"] = current_clan_tag
        # is_guest, redefined around live current-clan membership (2026-08-16 follow-up,
        # live-testing feedback, project owner's spec, verbatim: "the yellow marker is defined as
        # being a GUEST player for this guild... a member is a member regardless of assignment
        # status, a guest is a guest regardless of assignment status... that rule is pretty simple
        # and generic"). Overrides whatever the three write-path-specific markers above guessed
        # (signup source, shared-roster source, or the hardcoded False for a plain family-clan
        # member) whenever a live current clan is actually known — a real family-clan member must
        # never carry the badge no matter how they ended up in this pool (e.g. old auto_assigned/
        # auto_seeded history from a clan they've since left), and a player currently in any other
        # clan must always carry it, including sitting unassigned or moved into "Assigned to other
        # Guild." Only truly untrackable players (no current_clan_tag anywhere — left the game
        # entirely) keep the earlier write-path-based guess, since there's no live signal to use.
        if current_clan_tag is not None:
            player["is_guest"] = current_clan_tag not in family_clan_tags

    players = sorted(players_by_tag.values(), key=lambda p: (p["player_name"] or p["player_tag"]).lower())

    from qapbot.QBdiscocmdshelper_cwl import (
        count_cwl_pool_members_missing_dm,
        resolve_cwl_pending_reminder_targets_sync,
        resolve_cwl_pending_roster_updates_sync,
    )

    # Same three action-button gates the Hub message uses (add_cwl_management_components,
    # ui_cwl_roster.py) — reused here rather than re-derived, so the Teams Management board's own
    # copies of these buttons (2026-08-30, project owner's spec: "same logic for all three buttons
    # ... in all three views") can never disagree with the Hub about who is eligible for what.
    #
    # "Send Roster Updates" is meaningless before the FIRST announcement — resolve_cwl_pending_
    # roster_updates_sync compares against `notified_clan_tag`, which is None for literally every
    # assigned player pre-announcement, so calling it during signup_open would count the entire
    # not-yet-announced roster as "pending updates" (caught live, 2026-08-30: the board showed
    # "Send Roster Updates (9)" during Enrollment, before Announce Rosters had ever been pressed).
    # The Hub avoids this by only calling the resolver in its `else` branch (status != signup_open);
    # mirrored here with the same branch.
    if event["status"] in ("draft", "cancelled", "signup_open"):
        pending_roster_updates = 0
    else:
        pending = resolve_cwl_pending_roster_updates_sync(guild_id, event["id"], season)
        pending_roster_updates = len(pending["moved"]) + len(pending["dropped"]) + len(pending["new"])

    if event["status"] in ("draft", "cancelled"):
        pool_missing_dm_count = 0
        pending_reminder_count = 0
    else:
        pool_missing_dm_count = count_cwl_pool_members_missing_dm(guild_id, season)
        reminder_groups = resolve_cwl_pending_reminder_targets_sync(event["id"])["groups"]
        pending_reminder_count = sum(len(accounts) for accounts in reminder_groups.values())

    return {
        "season": season,
        "event_status": event["status"],
        "clans": clans,
        "players": players,
        "version": get_enrollment_version(guild_id),
        # Drives the board footer's "Send Roster Updates" button (2026-08-30, spec item 4). Sent
        # with every payload so the client's own optimistic per-drag bump gets corrected back down
        # on the next refresh — a drag that turned out to owe nobody a DM (A->B->A) must not leave
        # the button stuck on forever.
        "pending_roster_updates": pending_roster_updates,
        # Drive the board footer's "Notify New Pool Members" / "Remind Pending" buttons — same
        # counts the season overview shows and the Hub buttons are gated on.
        "pool_missing_dm_count": pool_missing_dm_count,
        "pending_reminder_count": pending_reminder_count,
    }


async def notify_new_cwl_pool_members(guild_id: int, season: str) -> Dict[str, Any]:
    """Rule h's "Notify New Pool Members" action (2026-08-18, CWL_ENROLLMENT_PLAYER_POOL_
    REDESIGN_PLAN.md) — for an event past `draft`, re-resolves the actual current pool and runs
    it through the shared DM batch helper. Both the pool resolution
    (resolve_cwl_pool_dm_targets_sync) and the sending (_send_cwl_enrollment_dm_batch) are the
    exact same functions start_cwl_enrollment uses, so this button and Start Enrollment can't
    drift apart about who counts as a pool member (2026-08-20 — see
    resolve_cwl_pool_dm_targets_sync's docstring for the bug that came of them not being shared).
    Who actually gets DMed is `resolve_cwl_pool_tags_missing_dm_sync()` — the SAME set the season
    overview's "New players without DM invitation" line counts and the button's own visibility is
    gated on, so the number on screen and the number contacted are the same thing by construction.

    An earlier version of this docstring claimed the batch helper's own global dm_sent dedup made
    that automatic, with "no separate filtering needed here". That was true when written and
    quietly stopped being true: tracker #0075 added a settled-status exclusion to the COUNT only,
    so the two drifted, and tracker #0079 caught it live (the line said 1, the action listed 2).
    Shared resolution beats shared intent."""
    from qapbot.QBdiscocmdshelper_cwl import (
        _send_cwl_enrollment_dm_batch,
        resolve_cwl_pool_dm_targets_sync,
        resolve_cwl_pool_tags_missing_dm_sync,
        resolve_seeded_cwl_signup_status,
    )

    db = CACHE.db_manager
    if db is None:
        return {"ok": False, "error": "no_database"}
    event = await asyncio.to_thread(db.get_cwl_event_sync, str(guild_id), season)
    if event is None:
        return {"ok": False, "error": "no_event"}
    if event["status"] in ("draft", "cancelled"):
        return {"ok": False, "error": "not_open"}

    pool = await asyncio.to_thread(resolve_cwl_pool_dm_targets_sync, guild_id, event["id"], season)

    # 4b-bis (plans/cwl-personal-hub.md) — same gap this button hit for Start Enrollment's own
    # primary seed loop: an opted-out member who joined the pool after enrollment started (this
    # button's whole purpose) gets skipped from the DM by _send_cwl_enrollment_dm_batch's own
    # seed-before-DM step (that step only ever seeds who it's ABOUT to DM), so without this pass
    # they would never get a cwl_signups row at all. Uses the SAME resolve_seeded_cwl_signup_status
    # precedence Start Enrollment does, so the two callers can never disagree about the result for
    # the same member with the same preferences — harmlessly redundant (ON CONFLICT DO NOTHING)
    # for an entry some other seed path already covered.
    optout_no_dm = pool["optout_no_dm"]
    if optout_no_dm:
        optout_no_dm_status_by_tag = await asyncio.to_thread(
            db.get_cwl_player_season_status_bulk_sync,
            [entry["player_tag"] for entry in optout_no_dm], season,
        )
        extra_signups: List[Dict[str, Any]] = []
        for entry in optout_no_dm:
            status, source = resolve_seeded_cwl_signup_status(
                optout_no_dm_status_by_tag.get(entry["player_tag"]), True, False,
            )
            extra_signups.append({
                "player_tag": entry["player_tag"],
                "player_name": entry["player_name"],
                "dmed_discord_id": entry["discord_id"],
                "preferred_league_rank": None,
                "source": source,
                "status": status,
            })
        await asyncio.to_thread(db.bulk_create_cwl_signups_sync, event["id"], extra_signups)

    # Send to EXACTLY the set the "New players without DM invitation" count reports (2026-08-30,
    # tracker #0079). The batch helper's own dedup only checks global dm_sent, which is a strictly
    # wider net than the count's — it also excludes anyone whose local signup row already carries a
    # settled status (tracker #0075). Filtering here rather than inside the batch helper is
    # deliberate: Start Enrollment shares that helper and MUST still contact an auto_confirmed
    # member (to tell them a standing preference enrolled them), so the narrowing belongs to this
    # caller, whose whole job is "reach the people nobody has invited yet".
    missing_dm_tags = await asyncio.to_thread(
        resolve_cwl_pool_tags_missing_dm_sync, guild_id, event["id"], season
    )
    dm_targets = [t for t in pool["targets"] if t["player_tag"] in missing_dm_tags]
    dm_result = await _send_cwl_enrollment_dm_batch(event["id"], guild_id, season, dm_targets)
    # dm_result["skipped_unlinked"] folds in both the pool-resolution-time count (pool's own
    # "no linked Discord account") and the send-time re-check inside _send_cwl_enrollment_dm_batch
    # (2026-08-22, live bug report: "Daniel" had no linked account at all yet still received a DM
    # and turned up "blocked" — that account had been unlinked in the window between this pool
    # resolution and the actual send).
    dm_result["skipped_unlinked"] += pool["skipped_unlinked"]
    return {"ok": True, **dm_result}


async def remind_pending_cwl_players(guild_id: int, season: str) -> Dict[str, Any]:
    """Tracker #0038's "Remind Pending" action: retracts each still-pending, linked player's old
    invitation DM, clears their global dm_sent dedup (both required BEFORE re-sending, same order
    _reset_and_resend_enrollment_dm uses for the single-player "reset to pending" action), then
    sends one combined reminder DM group per Discord user via send_cwl_reminder_dm_group
    (QBdiscocmdshelper_cwl.py) — grouping by Discord user rather than by account is the whole
    point of this action (project owner's spec): someone with several pending alt accounts gets
    one personal intro DM plus one buttons DM covering all of them, not N separate DMs.

    Pool resolution (resolve_cwl_pending_reminder_targets_sync) is deliberately its own function,
    not a reuse of resolve_cwl_pool_dm_targets_sync — that one's global dm_sent dedup would skip
    everyone already DMed, which is precisely who this action needs to re-reach."""
    from qapbot.QBdiscocmdshelper_cwl import (
        _dm_guard_blocks,
        _retract_enrollment_dms_for_tags,
        resolve_cwl_pending_reminder_targets_sync,
        send_cwl_reminder_dm_group,
    )

    db = CACHE.db_manager
    if db is None:
        return {"ok": False, "error": "no_database"}
    event = await asyncio.to_thread(db.get_cwl_event_sync, str(guild_id), season)
    if event is None:
        return {"ok": False, "error": "no_event"}
    if event["status"] in ("draft", "cancelled"):
        return {"ok": False, "error": "not_open"}

    pool = await asyncio.to_thread(resolve_cwl_pending_reminder_targets_sync, event["id"])
    summary: Dict[str, Any] = {
        "ok": True, "contacted": 0, "contacted_users": 0,
        "skipped_dm_guard": 0, "skipped_unlinked": pool["skipped_unlinked"],
        "skipped_optout": pool["skipped_optout"],
        "blocked": [], "no_mutual_guild": [], "failed": [],
    }
    for discord_id, accounts in pool["groups"].items():
        if _dm_guard_blocks(discord_id):
            summary["skipped_dm_guard"] += len(accounts)
            continue

        tags = [a["player_tag"] for a in accounts]
        await _retract_enrollment_dms_for_tags(event["id"], tags, "Remind Pending")
        for tag in tags:
            await asyncio.to_thread(db.clear_cwl_player_dm_sent_sync, tag, season)

        group_result = await send_cwl_reminder_dm_group(event["id"], guild_id, season, discord_id, accounts)
        summary["contacted"] += group_result["contacted"]
        if group_result["contacted"]:
            summary["contacted_users"] += 1
        summary["blocked"].extend(group_result["blocked"])
        summary["no_mutual_guild"].extend(group_result["no_mutual_guild"])
        summary["failed"].extend(group_result["failed"])

    logging.info(
        f"[CWL-ENROLLMENT] Remind Pending complete: guild={guild_id} season={season} "
        f"event={event['id']} contacted_accounts={summary['contacted']} "
        f"contacted_users={summary['contacted_users']} skipped_dm_guard={summary['skipped_dm_guard']} "
        f"skipped_unlinked={summary['skipped_unlinked']} skipped_optout={summary['skipped_optout']} "
        f"blocked={len(summary['blocked'])} no_mutual_guild={len(summary['no_mutual_guild'])} "
        f"failed={len(summary['failed'])}"
    )
    return summary


# CWL Guests search (2026-08-17 PROD meltdown fix): the search used to be an uncapped scan
# over CACHE.clan_name_cache (~430K entries) and, in `#` mode, CACHE.player_name_index
# (~6.6M entries) — a short prefix like "#2" accumulated millions of hits before the [:12] cap
# was ever applied, feeding get_player_links_sync() an IN() clause with millions of
# placeholders ("too many SQL variables") and triggering multi-second gen-2 GC pauses that
# blocked the Discord gateway heartbeat. GUEST_SEARCH_MIN_NEEDLE_TAG/_TEXT reject queries too
# short to be useful before any scan starts; GUEST_SEARCH_CAP bounds collection DURING the
# scan (break as soon as enough hits exist) instead of after it completes.
GUEST_SEARCH_MIN_NEEDLE_TAG = 2
GUEST_SEARCH_MIN_NEEDLE_TEXT = 3
GUEST_SEARCH_CAP = 12


def _search_cwl_guests_sync(guild_id: int, query: str) -> List[Dict[str, Any]]:
    """Unified fuzzy search for the "Guests" invite flow on Configure Participating Clans
    (2026-08-15, project owner's spec: "a discord user, a coc player name or a coc player_tag
    with intelligent sub-string match and auto-complete as far as possible") — one flat result
    list mixing clan hits and player hits (never nested), so the frontend renders one list with
    type badges instead of juggling three different shapes.

    Clan hits: substring match over CACHE.clan_name_cache — every clan tracked *anywhere* by
    this bot, not just this guild's own family (that's the whole point of a "guest" clan) —
    excluding every clan already in this guild's own lineup for the season (its whole clan
    family, plus every clan already configured for this event, participating or not — rule b/f;
    see resolve_cwl_pool_clan_tags_sync). A clan already participating in ANOTHER guild's event
    for the same season is still shown (never hidden — 2026-08-15, project owner's spec: "the
    user should be informed... and asked if he would like to add the clan to the own guild's
    clan roster nevertheless"), annotated with `already_shared_with` (that other guild's display
    name, or its raw ID if unresolvable) so the frontend can highlight it and prompt for
    confirmation before adding.

    Player hits, excluding anyone already in this guild's lineup for the season (2026-08-20 fix,
    live bug report: a current member of the guild's own participating clan kept showing up as
    an addable guest — the earlier already-invited-signup exclusion alone didn't cover a family-
    clan member with no cwl_signups row of their own yet, e.g. before Start Enrollment has run):
    a current member of any clan in resolve_cwl_pool_clan_tags_sync's union, OR anyone who
    already has a cwl_signups row for this event (an individually-invited guest, or one already
    seeded by Start Enrollment). Two match paths merged and deduped by player_tag —
      1. CACHE.search_player_names() (name substring, already powers /whois) plus a direct tag
         match/prefix if `query` looks like a tag.
      2. Discord-account display-name substring over CACHE.user_accounts — each of THAT
         account's linked players becomes its own player hit (flattened, not nested), so
         searching a Discord username surfaces their character(s) directly, ready to add.
    Every player hit is cross-referenced against get_player_links_sync() so the frontend knows
    upfront whether "send DM now" is even possible for it (discord_id present) — a never-linked
    tag typed in directly still works as a hit with discord_id=null, just can't be DMed until its
    owner links a real account.

    Two namespace-restricting prefixes (2026-08-16, live-testing feedback, project owner's spec,
    verbatim): "in discord when referring to another discord user you can start with an @ symbol,
    e.g. @major, indicating that we are talking about a discord user. add this to the intelligent
    search. when a given expression starts with @ assume it is a discord user and only search in
    that name space. similarly when the expression starts with a #, assume that we are talking
    about a clan, player or family tag."
      - `@needle`: Discord-account display-name substring ONLY — no clan matching, no CoC
        player-name matching. Returns every linked player of every matching account.
      - `#needle`: TAG substring matching only (clan_tag / player_tag) — no name-substring
        matching on either side. Still returns both clans and players, since a tag alone doesn't
        say which.
      - anything else: the original unrestricted default — name substrings for clans and
        players, plus the Discord display-name pass, all merged together.

    Interleaved clan/player ordering, each capped at 12 (2026-08-16 follow-up, live-testing
    feedback, project owner's spec, verbatim: "Do the interleave, cap each type 12 / 12") — a
    broad query can match dozens of clans (e.g. "major" matched 20 clan names in prod), which
    under the old "all clan hits first, one combined 25-cap at the very end" ordering buried
    every player/Discord hit past the visible scroll area, making the search look like it wasn't
    finding Discord users at all when it actually was. Capping each type at 12 before merging (and
    round-robining one-of-each into the final list) guarantees both kinds of matches are visible
    without scrolling through a wall of same-type rows first. Doesn't apply to the `@`-restricted
    path since that's single-type by construction."""
    from qapbot.config import CONFIG
    from qapbot.QBdiscocmdshelper_cwl import resolve_cwl_pool_clan_tags_sync, resolve_selected_cwl_season

    db = CACHE.db_manager
    query = (query or "").strip()
    if not query or db is None:
        return []

    guild_id_str = str(guild_id)
    season = resolve_selected_cwl_season(guild_id)
    event = db.get_cwl_event_sync(guild_id_str, season)

    # "This guild's own lineup for the season" — a guest, by definition, is never part of it —
    # shared with start_cwl_enrollment/resolve_cwl_pool_dm_targets_sync (2026-08-20, see
    # resolve_cwl_pool_clan_tags_sync's own docstring for the three-near-identical-unions history).
    pool_clan_tags = resolve_cwl_pool_clan_tags_sync(guild_id, event["id"] if event is not None else None)
    already_pooled_player_tags = {
        m["player_tag"] for m in db.get_current_clan_members_sync(pool_clan_tags)
    }
    if event is not None:
        already_pooled_player_tags |= {
            s["player_tag"] for s in db.get_cwl_signups_for_event_sync(event["id"])
        }

    import QBcore

    def _discord_display_name_player_hits(needle: str) -> Dict[str, Dict[str, Any]]:
        hits: Dict[str, Dict[str, Any]] = {}
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
                hits.setdefault(tag, {"player_tag": tag, "player_name": player.get("player_name") or tag})
        return hits

    def _finalize_player_hits(player_hits: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        tags = [tag for tag in player_hits if tag not in already_pooled_player_tags]
        if not tags:
            return []
        links = db.get_player_links_sync(tags)
        out: List[Dict[str, Any]] = []
        for tag in tags:
            hit = player_hits[tag]
            link = links.get(tag, {})
            entry: Dict[str, Any] = {
                "type": "player",
                "player_tag": tag,
                "player_name": link.get("player_name") or hit["player_name"],
                "discord_id": link.get("discord_id"),
            }
            # Internal-only marker, stripped before the response leaves
            # handle_get_cwl_guest_search() — see _resolve_guest_tag_via_coc_api().
            if hit.get("unverified"):
                entry["unverified"] = True
            out.append(entry)
        return out

    if query.startswith("@"):
        needle = query[1:].strip().lower()
        if len(needle) < GUEST_SEARCH_MIN_NEEDLE_TAG:
            return []
        return _finalize_player_hits(_discord_display_name_player_hits(needle))[:25]

    tag_only_mode = query.startswith("#")
    if tag_only_mode:
        if len(query[1:].strip()) < GUEST_SEARCH_MIN_NEEDLE_TAG:
            return []
    elif len(query) < GUEST_SEARCH_MIN_NEEDLE_TEXT:
        return []

    # Scan loop collects ONLY pure cache lookups (no DB queries) and breaks as soon as the cap
    # is reached — iteration order is cache-iteration order, unchanged by the early break, since
    # no sorting ever happened here (the [:12] used to be applied after the full scan instead).
    clan_hits: List[Dict[str, Any]] = []
    pool_clan_tag_set = set(pool_clan_tags)
    for clan_tag, _info in CACHE.clan_name_cache.items():
        if len(clan_hits) >= GUEST_SEARCH_CAP:
            break
        if clan_tag in pool_clan_tag_set:
            continue
        name = CACHE.get_clan_name(clan_tag, clan_tag) or clan_tag
        if tag_only_mode:
            if query.upper() not in clan_tag.upper():
                continue
        elif query.lower() not in name.lower() and query.lower() not in clan_tag.lower():
            continue
        # Same live-tier source _build_clan_config_payload uses for every other row (never
        # admin-set) — without this, a newly-added guest clan showed "—" for tier until the
        # next full page reload picked it up from the payload builder instead (live-testing
        # feedback, 2026-08-15).
        clan_hits.append({
            "type": "clan", "clan_tag": clan_tag, "clan_name": name,
            "clan_tier": CACHE.get_clan_war_league(clan_tag),
        })

    # Cross-guild claim check (2026-08-15) — reported, never used to hide the hit — runs AFTER
    # the cap so it never fires more than GUEST_SEARCH_CAP times per search (was: once per
    # matching clan found during the uncapped scan, the other PROD meltdown contributor).
    for hit in clan_hits:
        already_shared_with = None
        other_claims = db.find_cwl_clan_participation_across_guilds_sync(
            hit["clan_tag"], season, exclude_guild_id=guild_id_str
        )
        if other_claims:
            other_guild = QBcore.bot.get_guild(int(other_claims[0]["guild_id"]))
            already_shared_with = other_guild.name if other_guild else other_claims[0]["guild_id"]
        hit["already_shared_with"] = already_shared_with

    player_hits: Dict[str, Dict[str, Any]] = {}
    if tag_only_mode:
        upper_query = query.upper()
        # SQLite-backed tag-prefix lookup (2026-08-17, Step 11; unconditional since 2026-08-18,
        # PLAYER_NAME_INDEX_RETIREMENT_PLAN.md Steps 5-6, once DEV+PROD burn-in confirmed parity
        # with the retired in-memory scan) — index-backed on player_name_search's own PK, no
        # in-memory scan at all.
        if db is not None:
            for match in db.search_player_tags_by_prefix_sync(upper_query, limit=GUEST_SEARCH_CAP):
                player_hits.setdefault(match["player_tag"], match)
        # A tag typed exactly that the index doesn't know about at all — still offered as a raw
        # hit (name falls back to the tag itself) so the admin can add it directly. Marked
        # `unverified` so handle_get_cwl_guest_search() can tell "the DB found nothing real" from
        # "the DB found something", which is what gates the CoC API fallback
        # (_resolve_guest_tag_via_coc_api); the marker never reaches the frontend. Added
        # regardless of the cap above (a single entry — the final [:12] slice below re-caps it).
        if len(upper_query) >= 5 and upper_query not in player_hits:
            player_hits[upper_query] = {
                "player_tag": upper_query, "player_name": upper_query, "unverified": True
            }
    else:
        for match in CACHE.search_player_names(query, limit=25):
            player_hits[match["player_tag"]] = {"player_tag": match["player_tag"], "player_name": match["player_name"]}
        for tag, hit in _discord_display_name_player_hits(query.lower()).items():
            if len(player_hits) >= GUEST_SEARCH_CAP:
                break
            if tag not in player_hits:
                player_hits[tag] = hit

    player_result_list = _finalize_player_hits(player_hits)

    capped_clans = clan_hits[:GUEST_SEARCH_CAP]
    capped_players = player_result_list[:GUEST_SEARCH_CAP]
    results: List[Dict[str, Any]] = []
    for i in range(max(len(capped_clans), len(capped_players))):
        if i < len(capped_clans):
            results.append(capped_clans[i])
        if i < len(capped_players):
            results.append(capped_players[i])

    return results


# CoC API fallback for an unknown-but-well-formed tag (2026-08-20). When the DB search above
# finds nothing real for a query that normalize_clan_tag() accepts, ask the CoC API directly:
# clan first, then player. Both go through the cache_manager wrappers (Cardinal Rule 9), which is
# also what makes the "add it to the DB immediately" half free — coc_clan_cache.get_clan()'s
# _update_clan_metadata() already inserts a never-before-seen clan with the league-gated
# track_war_updates value (M3+ → True, below → False), so there is deliberately no separate clan
# write here; duplicating it would be a second source of that tier rule.
#
# Negative-result cache: a miss is NOT cached by coc_clan_cache (only successes are), and this
# search fires per debounced keystroke, so without this a run of no-match queries that happen to
# look like tags ("MARINES" normalizes fine) would issue two API calls each, every keystroke.
# Only a definitive coc.NotFound for BOTH lookups is recorded — a transient API error must stay
# retryable.
_GUEST_TAG_API_MISS_TTL_SECONDS = 600.0
_GUEST_TAG_API_MISS_CAP = 256
_guest_tag_api_misses: Dict[str, float] = {}


def _guest_tag_api_miss_is_active(tag: str) -> bool:
    expiry = _guest_tag_api_misses.get(tag)
    if expiry is None:
        return False
    if expiry <= time.monotonic():
        del _guest_tag_api_misses[tag]
        return False
    return True


def _record_guest_tag_api_miss(tag: str) -> None:
    now = time.monotonic()
    for stale_tag, expiry in list(_guest_tag_api_misses.items()):
        if expiry <= now:
            del _guest_tag_api_misses[stale_tag]
    while len(_guest_tag_api_misses) >= _GUEST_TAG_API_MISS_CAP:
        _guest_tag_api_misses.pop(next(iter(_guest_tag_api_misses)))
    _guest_tag_api_misses[tag] = now + _GUEST_TAG_API_MISS_TTL_SECONDS


def _build_api_clan_hit_sync(guild_id: int, clan_tag: str) -> Optional[Dict[str, Any]]:
    """Same clan-hit shape (and same already-in-the-lineup exclusion / already_shared_with
    annotation) _search_cwl_guests_sync produces, for a clan the API just revealed. Returns None
    when the clan is already in this guild's own lineup for the season (resolve_cwl_pool_clan_
    tags_sync — family or already configured for this event, participating or not) — the scan
    excludes those deliberately, and the API fallback must not smuggle them back in as addable."""
    from qapbot.QBdiscocmdshelper_cwl import resolve_cwl_pool_clan_tags_sync, resolve_selected_cwl_season

    import QBcore

    db = CACHE.db_manager
    if db is None:
        return None
    guild_id_str = str(guild_id)
    season = resolve_selected_cwl_season(guild_id)
    event = db.get_cwl_event_sync(guild_id_str, season)
    if clan_tag in resolve_cwl_pool_clan_tags_sync(guild_id, event["id"] if event is not None else None):
        return None

    already_shared_with = None
    other_claims = db.find_cwl_clan_participation_across_guilds_sync(
        clan_tag, season, exclude_guild_id=guild_id_str
    )
    if other_claims:
        other_guild = QBcore.bot.get_guild(int(other_claims[0]["guild_id"]))
        already_shared_with = other_guild.name if other_guild else other_claims[0]["guild_id"]

    return {
        "type": "clan",
        "clan_tag": clan_tag,
        "clan_name": CACHE.get_clan_name(clan_tag, clan_tag) or clan_tag,
        "clan_tier": CACHE.get_clan_war_league(clan_tag),
        "already_shared_with": already_shared_with,
    }


def _player_already_in_cwl_pool_sync(guild_id: int, player_tag: str) -> bool:
    """True when player_tag is already part of this guild's CWL pool for its currently-selected
    season — a current member of this guild's own lineup (resolve_cwl_pool_clan_tags_sync), or
    already carrying a cwl_signups row for the event — the API-fallback counterpart of
    _build_api_clan_hit_sync's pool-lineup check, so the fallback can't resurface someone
    already in the pool either."""
    from qapbot.QBdiscocmdshelper_cwl import resolve_cwl_pool_clan_tags_sync, resolve_selected_cwl_season

    db = CACHE.db_manager
    if db is None:
        return False
    season = resolve_selected_cwl_season(guild_id)
    event = db.get_cwl_event_sync(str(guild_id), season)
    pool_clan_tags = resolve_cwl_pool_clan_tags_sync(guild_id, event["id"] if event is not None else None)
    current_clan_tag = db.get_current_clan_tags_for_players_sync([player_tag]).get(player_tag)
    if current_clan_tag is not None and current_clan_tag in pool_clan_tags:
        return True
    if event is None:
        return False
    return db.get_cwl_signup_sync(event["id"], player_tag) is not None


def _persist_api_player_hit_sync(player_tag: str, player_name: str) -> Dict[str, Any]:
    """Writes an API-discovered player into player_name_index (which mirrors into
    player_name_search/player_name_fts), so the very next keystroke finds it in the DB without
    another API call, and returns it in _finalize_player_hits' shape."""
    from datetime import datetime, timezone

    db = CACHE.db_manager
    discord_id = None
    if db is not None:
        db.update_player_name_index_sync(
            [(player_tag, player_name, datetime.now(timezone.utc).isoformat())]
        )
        link = db.get_player_links_sync([player_tag]).get(player_tag, {})
        player_name = link.get("player_name") or player_name
        discord_id = link.get("discord_id")
    return {
        "type": "player",
        "player_tag": player_tag,
        "player_name": player_name,
        "discord_id": discord_id,
    }


async def _resolve_guest_tag_via_coc_api(guild_id: int, query: str) -> Optional[Dict[str, Any]]:
    """Clan-then-player CoC API lookup for `query`, returning one guest-search hit or None.

    None means "nothing to add": the query isn't a well-formed tag, the API doesn't know it, the
    lookup failed, it resolved to a clan already in this guild's own lineup, or it resolved to a
    player already in this guild's CWL pool for the currently-selected season (2026-08-20 fix,
    live bug report: an already-pooled player resurfaced through this fallback even after the DB
    search itself was taught to exclude them — an exact-tag re-search for someone with no
    player_name_index entry finds nothing "real" in the DB and falls through to here). Callers
    keep whatever the DB search produced in that case (including its raw unverified placeholder),
    so an API outage degrades to exactly the pre-2026-08-20 behavior."""
    import coc

    from qapbot.QBdiscocmdshelper import normalize_clan_tag

    tag = normalize_clan_tag(query)
    if not tag or _guest_tag_api_miss_is_active(tag):
        return None

    clan_definitely_absent = False
    try:
        await CACHE.coc_clan_cache.get_clan(tag)
    except coc.NotFound:
        clan_definitely_absent = True
    except Exception as e:
        logging.warning(f"[GUEST-SEARCH] CoC clan lookup for {tag} failed: {e}")
    else:
        logging.info(f"[GUEST-SEARCH] {tag} resolved to a clan via CoC API")
        return await asyncio.to_thread(_build_api_clan_hit_sync, guild_id, tag)

    player = await CACHE.get_player(tag)
    if player is None:
        if clan_definitely_absent:
            _record_guest_tag_api_miss(tag)
        return None
    if await asyncio.to_thread(_player_already_in_cwl_pool_sync, guild_id, tag):
        return None

    logging.info(f"[GUEST-SEARCH] {tag} resolved to player {player.name} via CoC API")
    return await asyncio.to_thread(_persist_api_player_hit_sync, tag, player.name)


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

    Three screens as of plans/cwl-personal-hub.md Phase 5a ('clan_config' / 'enrollment' /
    'player_prefs') — this handler needed no code change for the third; it already returns
    whatever CwlPlayerHubView's button (or /cwl preferences) recorded.
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


async def handle_get_i18n(request: web.Request) -> web.Response:
    """GET /api/i18n?guild_id=&discord_user_id=&ns=cwl.activity — plans/cwl-personal-hub.md
    Phase 6c. Bulk-fetch translation endpoint for the Discord Activity, which has no server-side
    rendering of its own and so cannot call `t()` directly; this is the one HTTP round-trip that
    lets it render every string in the caller's own language on launch.

    Language resolution is the EXACT chain `t()` itself uses
    (`get_user_language(discord_user_id) or get_guild_language(guild_id) or default`) — never
    re-derived independently — so the Activity can never disagree with a DM the same member
    receives from the bot.

    No permission gate beyond the bridge secret: a translation catalog reveals nothing
    guild/account-specific, unlike every other CWL bridge endpoint."""
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        guild_id_int = int(request.query["guild_id"])
        discord_user_id_str = str(int(request.query["discord_user_id"]))
    except (KeyError, ValueError):
        return web.json_response({"error": "missing/invalid guild_id or discord_user_id"}, status=400)
    namespace = request.query.get("ns")
    if not namespace:
        return web.json_response({"error": "missing ns"}, status=400)

    from qapbot.i18n import get_guild_language, get_namespace, get_user_language

    # Not to_thread-wrapped (unlike every DB-backed handler here, Pitfall 26) — this is a pure
    # in-memory dict traversal over the already-loaded translation catalog, the same cost class
    # as calling t() directly, which every handler in this file already does unwrapped.
    language = get_user_language(discord_user_id_str) or get_guild_language(guild_id_int)
    strings = get_namespace(namespace, language)
    return web.json_response({"lang": language, "strings": strings})


def _build_player_prefs_payload_sync(guild_id: int, discord_user_id: int) -> Dict[str, Any]:
    """Build the GET/POST response for the Player CWL Settings Hub's Activity screen
    (plans/cwl-personal-hub.md Phase 5c) — the ONLY function that resolves which accounts a
    request may act on, which is what makes this feature's account protection (Cardinal Rule 2)
    hold: every account in the response comes SOLELY from
    get_all_players_for_discord_ids_sync([str(discord_user_id)]), never from anything
    client-supplied.

    Block I ("accounts") is season-independent and always populated when the caller has any
    linked accounts at all. Block II ("season_rows") additionally needs the guild's CURRENT CWL
    event — resolved via get_current_cwl_event_sync(), the same "which event is relevant to this
    member right now" resolver the plan's Phase 5c spec calls for, deliberately NOT the admin's
    cwl_selected_season UI-selection state. Both season/event_status and season_rows are
    None/[] when the guild has no CWL event at all; block I still renders (preferences are
    season-independent).

    Plain synchronous function (Pitfall 26, COPILOT_PITFALLS_COOKBOOK.md) — callers wrap the
    whole thing in one asyncio.to_thread() hop."""
    from qapbot.QBdiscocmdshelper_cwl import get_current_cwl_event_sync

    db = CACHE.db_manager
    if db is None:
        return {"season": None, "event_status": None, "accounts": [], "season_rows": []}

    players = db.get_all_players_for_discord_ids_sync([str(discord_user_id)])
    # Ordered by display name (case-insensitive), not "primary account first" — the primary flag
    # isn't part of get_all_players_for_discord_ids_sync's row shape, which deliberately matches
    # get_current_clan_members_sync's exactly so Start Enrollment's account-wide expansion can
    # merge the two uniformly (see that method's own docstring); adding a column here would break
    # that contract for a purely cosmetic ordering nicety.
    players.sort(key=lambda p: (p["player_name"] or p["player_tag"]).lower())

    accounts: List[Dict[str, Any]] = []
    for p in players:
        # Data model's defensive precedence: if both flags are somehow 1, opt-out wins.
        if p["cwl_permanent_optout"]:
            mode = "optout"
        elif p["cwl_permanent_optin"]:
            mode = "optin"
        else:
            mode = "none"
        accounts.append({
            "player_tag": p["player_tag"],
            "player_name": p["player_name"],
            "verified": bool(p["verified"]),
            "preferred_league_rank": p["preferred_league_rank"],
            "mode": mode,
            "send_dm_anyway": bool(p["cwl_optout_send_dm_anyway"]),
        })

    event = get_current_cwl_event_sync(guild_id)
    if event is None:
        return {"season": None, "event_status": None, "accounts": accounts, "season_rows": []}

    signups_by_tag = {s["player_tag"]: s for s in db.get_cwl_signups_for_event_sync(event["id"])}
    assignments_by_tag = {a["player_tag"]: a for a in db.get_cwl_assignments_sync(event["id"])}
    clans_by_tag = {c["clan_tag"]: c for c in db.get_cwl_event_clans_sync(event["id"])}

    season_rows: List[Dict[str, Any]] = []
    for p in players:
        tag = p["player_tag"]
        signup = signups_by_tag.get(tag)
        assignment = assignments_by_tag.get(tag)
        assigned_clan_tag = assignment["assigned_clan_tag"] if assignment else None
        clan_row = clans_by_tag.get(assigned_clan_tag) if assigned_clan_tag else None
        # Same tier resolution _build_enrollment_payload_sync's own _tier_for uses: the live
        # CoC-API-derived war league wins when known, falling back to the admin-set
        # target_league_rank only when it isn't.
        tier = None
        if assigned_clan_tag:
            tier = CACHE.get_clan_war_league(assigned_clan_tag) or (
                clan_row.get("target_league_rank") if clan_row else None
            )
        season_rows.append({
            "player_tag": tag,
            "player_name": p["player_name"],
            "signup_status": signup["status"] if signup else None,
            "assigned_clan_tag": assigned_clan_tag,
            "assigned_clan_name": (
                CACHE.get_clan_name(assigned_clan_tag, assigned_clan_tag) if assigned_clan_tag else None
            ),
            "assigned_clan_tier": tier,
            "assigned_clan_start_at": clan_row.get("cwl_start_at") if clan_row else None,
        })

    return {
        "season": event["cwl_season"],
        "event_status": event["status"],
        "accounts": accounts,
        "season_rows": season_rows,
    }


async def handle_get_cwl_player_prefs(request: web.Request) -> web.Response:
    """GET /api/cwl/player-prefs?guild_id=&discord_user_id= — plans/cwl-personal-hub.md
    Phase 5c. No permission gate beyond the bridge secret: unlike every OTHER CWL bridge
    endpoint (admin/leader-gated), this one's "authorization" IS the payload builder itself —
    it can only ever return discord_user_id's OWN linked accounts, so there's nothing an
    unprivileged caller could see here that isn't already their own data."""
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        guild_id = int(request.query["guild_id"])
        discord_user_id = int(request.query["discord_user_id"])
    except (KeyError, ValueError):
        return web.json_response({"error": "missing/invalid guild_id or discord_user_id"}, status=400)

    payload = await asyncio.to_thread(_build_player_prefs_payload_sync, guild_id, discord_user_id)
    return web.json_response(payload)


async def handle_post_cwl_player_prefs(request: web.Request) -> web.Response:
    """POST /api/cwl/player-prefs — plans/cwl-personal-hub.md Phase 5c. Applies one or more
    preference changes, each mapping to one set_cwl_preferences_sync() call, then returns the
    freshly rebuilt GET payload (never an optimistic client-side merge — the client always
    re-renders from server truth).

    Account protection (Cardinal Rule 2): every change's player_tag must belong to
    discord_user_id's OWN linked accounts (player_tag: null, "apply to all", needs no check —
    set_cwl_preferences_sync's own WHERE discord_id = ? already scopes it correctly). A
    player_tag naming someone else's account is a 403 for the WHOLE request, not a partial
    apply-the-rest-and-silently-skip-that-one — silently dropping just that one change would be
    much harder for a caller to notice went wrong than a hard rejection.
    """
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
        guild_id = int(body["guild_id"])
        discord_user_id = int(body["discord_user_id"])
        changes = body["changes"]
        if not isinstance(changes, list):
            raise TypeError("changes must be a list")
    except (KeyError, ValueError, TypeError):
        return web.json_response({"error": "invalid request body"}, status=400)

    db = CACHE.db_manager
    if db is None:
        return web.json_response({"error": "database not ready"}, status=503)

    owned_tags = {
        p["player_tag"]
        for p in await asyncio.to_thread(db.get_all_players_for_discord_ids_sync, [str(discord_user_id)])
    }
    for change in changes:
        tag = change.get("player_tag")
        if tag is not None and tag not in owned_tags:
            return web.json_response({"error": "not your account"}, status=403)

    def _apply_changes_sync() -> None:
        for change in changes:
            kwargs: Dict[str, Any] = {}
            if "mode" in change and change["mode"] is not None:
                kwargs["mode"] = change["mode"]
            if "send_dm_anyway" in change and change["send_dm_anyway"] is not None:
                kwargs["send_dm_anyway"] = bool(change["send_dm_anyway"])
            if change.get("rank_provided"):
                kwargs["league_rank"] = change.get("league_rank")
                kwargs["rank_provided"] = True
            if kwargs:
                db.set_cwl_preferences_sync(str(discord_user_id), change.get("player_tag"), **kwargs)

    await asyncio.to_thread(_apply_changes_sync)

    payload = await asyncio.to_thread(_build_player_prefs_payload_sync, guild_id, discord_user_id)
    return web.json_response(payload)


# Maps _apply_cwl_signup_response()'s failure codes onto HTTP statuses, matching the precedent
# handle_post_cwl_enrollment_status() already sets for the same underlying conditions (403 for
# an ownership mismatch, 409 for "the thing this needs no longer applies", 503 for no DB).
_PLAYER_PREFS_STATUS_ERROR_HTTP_STATUS: Dict[str, int] = {
    "db_unavailable": 503,
    "no_longer_valid": 409,
    "not_your_signup": 403,
    "signup_closed": 409,
}


async def handle_post_cwl_player_prefs_status(request: web.Request) -> web.Response:
    """POST /api/cwl/player-prefs/status — plans/cwl-personal-hub.md Phase 5c. The member
    changing their own invitation status from block II. `action`, not `status` — deliberately
    the same vocabulary as the DM button's own custom_id (cwl:signup:confirm|optout), because
    this endpoint's entire contract is "do exactly what that button does": it calls
    _apply_cwl_signup_response() directly, which gets the account-ownership check, the
    event['status'] != 'signup_open' guard (== the spec's "only after enrollment started"),
    upsert_cwl_signup_sync, propagate_cwl_player_response, and bump_enrollment_version all for
    free and provably identical to a real DM click — see that function's own docstring
    (ui_cwl_roster.py).

    Then reconciles the member's own invitation/reminder DM (best-effort, matching
    cleanup_stale_cwl_enrollment_dms()'s swallow-everything posture) via
    rerender_cwl_dm_after_response() with no interaction — so a member who answers here while
    their DM is still unanswered doesn't leave a live-looking Confirm/Opt Out pair in their inbox
    contradicting what they just did. The status write has already committed by this point; a DM
    that can't be edited must never fail the member's action.
    """
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
        guild_id = int(body["guild_id"])
        discord_user_id = int(body["discord_user_id"])
        player_tag = str(body["player_tag"]).upper()
        action = str(body["action"])
    except (KeyError, ValueError, TypeError):
        return web.json_response({"error": "invalid request body"}, status=400)
    if action not in ("confirm", "optout"):
        return web.json_response({"error": f"unsupported action '{action}'"}, status=400)

    from qapbot.QBdiscocmdshelper_cwl import get_current_cwl_event_sync
    from qapbot.ui_cwl_roster import _apply_cwl_signup_response, rerender_cwl_dm_after_response

    event = await asyncio.to_thread(get_current_cwl_event_sync, guild_id)
    if event is None:
        return web.json_response({"error": "no_longer_valid"}, status=409)

    result = await _apply_cwl_signup_response(event["id"], player_tag, action, str(discord_user_id))
    if result["code"] != "ok":
        status = _PLAYER_PREFS_STATUS_ERROR_HTTP_STATUS.get(result["code"], 409)
        return web.json_response({"error": result["code"]}, status=status)

    db = CACHE.db_manager
    if db is not None:
        try:
            season = event["cwl_season"]
            global_row = await asyncio.to_thread(db.get_cwl_player_season_status_sync, player_tag, season)
            if global_row and global_row.get("dm_sent_via_message_id") and global_row.get("dmed_discord_id"):
                import QBcore

                dm_owner = await QBcore.bot.fetch_user(int(global_row["dmed_discord_id"]))
                dm_channel = dm_owner.dm_channel or await dm_owner.create_dm()
                message = await dm_channel.fetch_message(int(global_row["dm_sent_via_message_id"]))
                await rerender_cwl_dm_after_response(
                    message, event["id"], season, global_row["dmed_discord_id"],
                    action=action, player_name=result["player_name"], interaction=None,
                )
        except Exception as e:
            logging.warning(
                f"[WEB-BRIDGE] player-prefs status change: could not reconcile DM for {player_tag}: {e}"
            )

    payload = await asyncio.to_thread(_build_player_prefs_payload_sync, guild_id, discord_user_id)
    return web.json_response(payload)


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

    payload = await asyncio.to_thread(_build_enrollment_payload_sync, guild_id)
    return web.json_response(payload)


async def handle_get_cwl_enrollment_wait(request: web.Request) -> web.Response:
    """Long-poll endpoint (2026-08-17, CWL_PROD_PERFORMANCE_FIX_PLAN.md P1 Step 8) replacing the
    client's old fixed 12s setInterval poll of GET /api/cwl/enrollment. Same admin-or-leader gate
    as the enrollment GET. `known_version` is whatever the client last saw (from that GET's own
    "version" field, or a prior wait's response); if the guild's current version already differs,
    responds immediately. Otherwise parks on the guild's asyncio.Condition for up to
    _ENROLLMENT_WAIT_TIMEOUT_SECONDS, released the instant any write bumps the version
    (bump_enrollment_version(), called next to every existing hub-refresh call site). Carries NO
    payload of its own on purpose — the client refetches the full enrollment payload via the
    existing GET whenever this responds `changed: true`, so this handler stays allocation-free
    and there's exactly one place (`_build_enrollment_payload_sync`) that builds the actual board
    data. Every hop between here and the Discord Activity (Worker, cloudflared, the Discord
    proxy itself) only ever sees an ordinary HTTP request that happens to take up to
    _ENROLLMENT_WAIT_TIMEOUT_SECONDS — no streaming, no protocol upgrade."""
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        guild_id = int(request.query["guild_id"])
        discord_user_id = int(request.query["discord_user_id"])
        known_version = int(request.query["known_version"])
    except (KeyError, ValueError):
        return web.json_response(
            {"error": "missing/invalid guild_id, discord_user_id or known_version"}, status=400
        )

    if not await _resolve_admin_or_leader(guild_id, discord_user_id):
        return web.json_response({"error": "not an admin or leader of this guild"}, status=403)

    guild_id_str = str(guild_id)
    current = _enrollment_version.get(guild_id_str, 0)
    if current != known_version:
        logging.debug(
            f"[WEB-BRIDGE] enrollment/wait guild={guild_id_str} known_version={known_version} "
            f"already stale (current={current}) -> immediate changed=true"
        )
        return web.json_response({"changed": True, "version": current})

    # Waiter cap (2026-08-17, Step 8) — beyond this many parked coroutines for one guild, degrade
    # gracefully (report "changed" so the client refetches and re-issues the wait with a fresh
    # known_version) rather than accumulate coroutines without bound. Kept at INFO (unlike the
    # routine parking/released lines below) — genuinely hitting this cap (10+ concurrent viewers
    # of one guild's board) is rare and worth surfacing, not routine per-cycle noise.
    if _enrollment_waiter_counts.get(guild_id_str, 0) >= _ENROLLMENT_WAIT_MAX_WAITERS_PER_GUILD:
        logging.info(f"[WEB-BRIDGE] enrollment/wait guild={guild_id_str} waiter cap hit -> immediate changed=true")
        return web.json_response({"changed": True, "version": current})

    _enrollment_waiter_counts[guild_id_str] = _enrollment_waiter_counts.get(guild_id_str, 0) + 1
    logging.debug(
        f"[WEB-BRIDGE] enrollment/wait guild={guild_id_str} parking at version={current} "
        f"(waiters now {_enrollment_waiter_counts[guild_id_str]}, timeout={_ENROLLMENT_WAIT_TIMEOUT_SECONDS}s)"
    )
    try:
        cond = _enrollment_condition_for(guild_id_str)
        async with cond:
            try:
                await asyncio.wait_for(cond.wait(), timeout=_ENROLLMENT_WAIT_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                pass
    finally:
        _enrollment_waiter_counts[guild_id_str] = _enrollment_waiter_counts.get(guild_id_str, 1) - 1

    current = _enrollment_version.get(guild_id_str, 0)
    changed = current != known_version
    logging.debug(f"[WEB-BRIDGE] enrollment/wait guild={guild_id_str} released -> changed={changed} version={current}")
    return web.json_response({"changed": changed, "version": current})


async def handle_get_cwl_clan_names(request: web.Request) -> web.Response:
    """Resolves clan_tag -> name for tags the Manage Enrollment board's hover pop-up doesn't
    already know locally (2026-08-16, project owner's spec: "show the pop-up as soon as possible
    with what we already have, then fetch more data... starting with the clan name"). Only a
    player's `current_clan_tag` needs this — every clan actually on the board already carries its
    own name in the initial enrollment payload (`EnrollmentClan.name`), so this is purely for a
    player whose real current clan isn't one of this event's own columns (e.g. any clan CACHE has
    ever seen, not just this guild's). Same admin-or-leader gate as the enrollment screen itself
    since this is only ever called from that board. Silently omits any tag CACHE doesn't
    recognize at all — the client already falls back to displaying the raw tag for those."""
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        guild_id = int(request.query["guild_id"])
        discord_user_id = int(request.query["discord_user_id"])
    except (KeyError, ValueError):
        return web.json_response({"error": "missing/invalid guild_id or discord_user_id"}, status=400)
    tags = [t for t in request.query.get("tags", "").split(",") if t]

    if not await _resolve_admin_or_leader(guild_id, discord_user_id):
        return web.json_response({"error": "not an admin or leader of this guild"}, status=403)

    names = {tag: CACHE.get_clan_name(tag, None) for tag in tags}
    return web.json_response({"names": {tag: name for tag, name in names.items() if name is not None}})


# Player-stats cache (2026-08-17, CWL_PROD_PERFORMANCE_FIX_PLAN.md P1 Step 7) — the hover
# pop-up's 3-month main+history SQL aggregation ran ~100+ times in the incident session, almost
# always for the same handful of players a lead was scanning back and forth across. Bridge-side
# only: get_recent_cwl_player_stats has exactly one caller anywhere in the codebase (this
# handler — confirmed 2026-08-17, no QBhelperfunctions.py/QBdiscocmdshelper_cwl.py caller and no
# /whois usage exists today), so there's no "always-fresh" caller to keep separate from.
# player_tag -> (monotonic timestamp, payload). TTL, not correctness — stats change at most once
# per war round, so a stale-by-minutes read is harmless.
_player_stats_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_PLAYER_STATS_CACHE_TTL_SECONDS = 15 * 60
_PLAYER_STATS_CACHE_MAX_ENTRIES = 2000


def clear_player_stats_cache() -> None:
    """Wholesale invalidation, called once per update cycle (QapBot.py's [CYCLE-CLEANUP] hook) so
    a just-finished war round's stats show up within one cycle instead of waiting out the full
    TTL. Safe to call from a worker thread (dict.clear() is a single GIL-atomic C call, same
    thread-safety assumption CACHE.coc_clan_cache.clear_expired() already relies on in that same
    cleanup hook) — nothing here does a Python-level callback mid-clear."""
    _player_stats_cache.clear()


async def handle_get_cwl_player_stats(request: web.Request) -> web.Response:
    """Backs the second half of the Manage Enrollment board's hover pop-up progressive fetch
    (2026-08-16, project owner's spec, verbatim: "get the number of missed cwl attacks from the
    last three season's" / "add the attack / defense ratio from the last three cwl seaons" —
    refined after a live-testing mismatch report to: "calculated exactly as the /leaderboard
    command would do it with the modes missedattacks and attackdefratio and with the options
    cwl_only=true and month=-3" / "the option scope=ALL is also important"). Single-player, called
    on-demand per hover — see get_recent_cwl_player_stats's own docstring (QBhelperfunctions.py)
    for the full computation, which deliberately reuses /leaderboard's own aggregation functions
    rather than a separate query, so the two can never disagree again. Same admin-or-leader gate
    as the enrollment screen itself. A player with no CWL history at all still gets a 200 with
    null fields (not a 404/error) — the client just leaves those pop-up lines out, same as it
    already does for an unresolved clan name.

    TTL-cached per player_tag (2026-08-17, Step 7, see _player_stats_cache above) — a repeat
    hover within 15 minutes never touches the DB at all."""
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        guild_id = int(request.query["guild_id"])
        discord_user_id = int(request.query["discord_user_id"])
        player_tag = str(request.query["player_tag"])
    except (KeyError, ValueError):
        return web.json_response({"error": "missing/invalid guild_id, discord_user_id or player_tag"}, status=400)

    if not await _resolve_admin_or_leader(guild_id, discord_user_id):
        return web.json_response({"error": "not an admin or leader of this guild"}, status=403)

    now = time.monotonic()
    cached = _player_stats_cache.get(player_tag)
    if cached is not None and now - cached[0] < _PLAYER_STATS_CACHE_TTL_SECONDS:
        return web.json_response(cached[1])

    from QBhelperfunctions import get_recent_cwl_player_stats

    # asyncio.to_thread()-wrapped (2026-08-17, Pitfall 26, COPILOT_PITFALLS_COOKBOOK.md — the
    # rule covers READ paths too, not just writes) — this is the 3-month main+history SQL
    # aggregation the PROD incident log showed running synchronously on the loop once per tile
    # hover (~100+ calls in the session).
    stats = await asyncio.to_thread(get_recent_cwl_player_stats, player_tag)

    if len(_player_stats_cache) >= _PLAYER_STATS_CACHE_MAX_ENTRIES:
        # dict.popitem() is LIFO (evicts the most-recently-INSERTED key), not true "oldest" LRU
        # eviction — the plan's own "no external LRU dependency" tradeoff. In practice this cap
        # is far larger than any real player pool a board would ever hover through in a session,
        # so it's a safety valve against unbounded growth, not a hot eviction path.
        _player_stats_cache.popitem()
    _player_stats_cache[player_tag] = (now, stats)

    return web.json_response(stats)


# The only statuses an admin may set from the board's right-click menu (2026-08-22, tracker
# #0014). Deliberately excludes 'withdrawn' — legacy-only, its one writer was deleted 2026-08-19
# (see activity/client/src/types.ts's EnrollmentPlayer.signup_status comment). Also deliberately
# excludes 'auto_confirmed' (plans/cwl-personal-hub.md Phase 4) — that status means "a standing
# opt-in preference seeded this", and an admin setting it by hand would assert a preference the
# member never actually expressed; the three statuses below remain the complete admin-settable set.
ADMIN_SETTABLE_ENROLLMENT_STATUSES: Tuple[str, ...] = ("confirmed", "declined", "pending")


async def handle_post_cwl_enrollment_status(request: web.Request) -> web.Response:
    """Admin override of one player's enrollment status from the Manage Enrollment board's
    right-click menu (2026-08-22, tracker #0014, project owner's spec: "allows guild admins to
    change the enrollment status of every member of the player pool ... Confirmed / Declined /
    Pending (sends DM again!)").

    "Last action wins" (the spec's explicit conflict rule, both directions) needs no new
    machinery here: this handler and the player's own DM button (CwlSignupResponseButton.
    callback, ui_cwl_roster.py) write the SAME global row through the SAME function
    (propagate_cwl_player_response), which upserts with no ordering guard — so whichever write
    lands later is the one that stands. The one thing that could break that symmetry is
    retracting the player's DM out from under them, which is why only the `pending` branch below
    deletes it: after an admin sets confirmed/declined the DM deliberately stays live, so the
    player can still answer and still win.

    `pending` additionally retracts the old DM and sends a fresh one to the account's CURRENT
    owner — the re-send goes through _send_cwl_enrollment_dm_batch() rather than a hand-rolled
    send, so it inherits that helper's signup-row seeding (Pitfall 38: a DM sent without a
    cwl_signups row carries a permanently dead button).
    """
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
        guild_id = int(body["guild_id"])
        discord_user_id = int(body["discord_user_id"])
        player_tag = str(body["player_tag"]).upper()
        status = str(body["status"])
    except (KeyError, ValueError, TypeError):
        return web.json_response({"error": "invalid request body"}, status=400)

    if status not in ADMIN_SETTABLE_ENROLLMENT_STATUSES:
        return web.json_response({"error": f"unsupported status '{status}'"}, status=400)

    # Same gate as POST /api/cwl/enrollment/assign, the board's other write action — the board is
    # itself opened behind _check_cwl_admin_or_leader_permission (ui_cwl_roster.py), so an
    # admin-only gate here would let a leader open the board and then silently fail on this one
    # action.
    if not await _resolve_admin_or_leader(guild_id, discord_user_id):
        return web.json_response({"error": "not an admin or leader of this guild"}, status=403)

    db = CACHE.db_manager
    if db is None:
        return web.json_response({"error": "database not ready"}, status=503)

    from datetime import datetime, timezone

    from qapbot.QBdiscocmdshelper_cwl import (
        _send_cwl_enrollment_dm_batch,
        cleanup_stale_cwl_enrollment_dms,
        propagate_cwl_player_response,
        resolve_selected_cwl_season,
    )
    from qapbot.ui_cwl_roster import refresh_cwl_management_hub_message

    season = resolve_selected_cwl_season(guild_id)
    event = await asyncio.to_thread(db.get_cwl_event_sync, str(guild_id), season)
    if event is None:
        return web.json_response({"error": f"no CWL event exists yet for season {season}"}, status=409)
    # Same guard notify_new_cwl_pool_members() already applies before DMing: a draft event hasn't
    # enrolled anyone yet and a cancelled one is dead, so there is no enrollment status to
    # override in either.
    if event["status"] in ("draft", "cancelled"):
        return web.json_response(
            {"error": f"enrollment is not open for season {season} (event is {event['status']})"},
            status=409,
        )
    event_id = event["id"]

    # One to_thread hop for the whole read-then-write unit (Pitfall 26) — this handler shares the
    # bot's event loop with the Discord gateway (see module docstring), and the reads below feed
    # straight into the write with no await in between.
    def _apply_status_sync() -> Dict[str, Any]:
        signup = db.get_cwl_signup_sync(event_id, player_tag) or {}
        global_row = db.get_cwl_player_season_status_sync(player_tag, season) or {}
        # Live ownership wins over either snapshot (Pitfall 37) — cwl_signups.dmed_discord_id and
        # cwl_player_season_status.dmed_discord_id both record who was DMed at some earlier point,
        # never who owns the account now.
        #
        # The fallback distinguishes two cases get_player_links_sync deliberately reports
        # differently, because only one of them is safe to fall back on:
        #   - tag ABSENT from the dict  = user_players has no row at all (a guest tag added by
        #     search that was never linked). Keep whatever recipient was recorded rather than
        #     blanking it — same fallback CwlSignupResponseButton.callback makes.
        #   - tag PRESENT, discord_id None = the account sits in the UNASSIGNED pool, i.e. nobody
        #     owns it right now. Falling back here would re-stamp the stale owner onto the row
        #     (exactly the self-perpetuating staleness Pitfall 37 describes) and, on the `pending`
        #     branch, aim a fresh DM at someone who no longer owns the account.
        links = db.get_player_links_sync([player_tag])
        if player_tag in links:
            owner_discord_id = links[player_tag].get("discord_id")
        else:
            owner_discord_id = signup.get("dmed_discord_id") or global_row.get("dmed_discord_id")
        link = links.get(player_tag) or {}
        player_name = (
            signup.get("player_name") or global_row.get("player_name") or link.get("player_name") or player_tag
        )
        responded_at = (
            None if status == "pending" else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        )
        # Creates the local row when the player had none — a pooled family member the board shows
        # but Start Enrollment never seeded a signup for still needs somewhere to hold the status.
        db.upsert_cwl_signup_sync(
            event_id, player_tag, player_name, owner_discord_id,
            signup.get("preferred_league_rank"), "admin_status", status, responded_at=responded_at,
        )
        return {
            "owner_discord_id": owner_discord_id,
            "player_name": player_name,
            "responded_at": responded_at,
            # Only meaningful for the `pending` branch below — the DM to retract, if this player
            # was ever actually DMed for this season.
            "dm_message_id": global_row.get("dm_sent_via_message_id"),
            "dm_recipient_id": global_row.get("dmed_discord_id"),
        }

    context = await asyncio.to_thread(_apply_status_sync)

    # Global source of truth + every OTHER guild's local mirror (rule h) — the same call the DM
    # button makes, which is what makes "last action wins" hold across both write paths.
    affected_guild_ids = await propagate_cwl_player_response(
        player_tag, season, status, context["responded_at"], context["player_name"],
        context["owner_discord_id"], event_id, guild_id,
    )

    dm_result: Optional[Dict[str, Any]] = None
    if status == "pending":
        dm_result = await _reset_and_resend_enrollment_dm(
            db, event_id, guild_id, season, player_tag, context,
            cleanup_stale_cwl_enrollment_dms, _send_cwl_enrollment_dm_batch,
        )

    try:
        await refresh_cwl_management_hub_message(guild_id, "cwl_management")
    except Exception as e:
        logging.warning(f"[WEB-BRIDGE] Saved enrollment status but could not refresh the Hub message: {e}")
    await bump_enrollment_version(guild_id)
    for other_guild_id in affected_guild_ids:
        await bump_enrollment_version(other_guild_id)

    return web.json_response({"ok": True, "status": status, "dm": dm_result})


async def _reset_and_resend_enrollment_dm(
    db: Any, event_id: int, guild_id: int, season: str, player_tag: str, context: Dict[str, Any],
    cleanup_stale_cwl_enrollment_dms: Any, send_dm_batch: Any,
) -> Dict[str, Any]:
    """The `pending` half of handle_post_cwl_enrollment_status(): retract the old enrollment DM,
    clear the global dm_sent record, and send a fresh DM to the account's current owner.

    Split out purely to keep the handler readable — it has no other caller. The two helper
    functions are passed in rather than re-imported so the handler's single import block stays
    the one place this module reaches into QBdiscocmdshelper_cwl.

    Returns a small outcome dict the frontend surfaces in the board's footer, so an admin can see
    WHY no DM went out when one didn't: {"sent": bool, "reason": str|None}.
    """
    import QBcore

    bot = getattr(QBcore, "bot", None)
    # Best-effort retraction, exactly like the Delete-Season path — a DM that can't be removed is
    # no worse than the pre-fix behavior, and must never block the status reset itself.
    if bot is not None and context["dm_message_id"] and context["dm_recipient_id"]:
        try:
            await cleanup_stale_cwl_enrollment_dms(
                bot,
                [{
                    "player_tag": player_tag,
                    "dmed_discord_id": context["dm_recipient_id"],
                    "message_id": context["dm_message_id"],
                }],
            )
        except Exception as e:
            logging.warning(f"[WEB-BRIDGE] Could not retract the old enrollment DM for {player_tag}: {e}")

    # Must happen BEFORE the re-send: _send_cwl_enrollment_dm_batch()'s global dedup would
    # otherwise report skipped_already_dm_globally and send nothing at all.
    await asyncio.to_thread(db.clear_cwl_player_dm_sent_sync, player_tag, season)

    if not context["owner_discord_id"]:
        # No linked Discord account — the status reset still stands (the board shows them as
        # pending again), there is simply nobody to ask.
        return {"sent": False, "reason": "unlinked"}

    batch = await send_dm_batch(
        event_id, guild_id, season,
        [{
            "player_tag": player_tag,
            "player_name": context["player_name"],
            "discord_id": context["owner_discord_id"],
        }],
    )
    if batch["contacted"]:
        return {"sent": True, "reason": None}
    if batch["blocked"]:
        return {"sent": False, "reason": "blocked"}
    if batch["skipped_unlinked"]:
        return {"sent": False, "reason": "unlinked"}
    if batch["skipped_dm_guard"]:
        return {"sent": False, "reason": "dm_guard"}
    return {"sent": False, "reason": "failed"}


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

    from qapbot.QBdiscocmdshelper_cwl import assign_cwl_player_sync, resolve_selected_cwl_season
    from qapbot.ui_cwl_roster import refresh_cwl_management_hub_message

    season = resolve_selected_cwl_season(guild_id)
    event = await asyncio.to_thread(db.get_cwl_event_sync, str(guild_id), season)
    if event is None:
        return web.json_response(
            {"error": f"no CWL event exists yet for season {season}"}, status=409
        )

    # A deliberate human drag-and-drop action — routed through the one general assignment method
    # (2026-08-16, live-testing feedback, project owner's spec: "one general method that assigns
    # players to any pool and the race condition checks should all be implemented there") so the
    # conflict-purge (evicting the player from any OTHER shared clan they're already confirmed in)
    # can never be skipped here or by any other write path. See assign_cwl_player_sync's own
    # docstring for the full purge/write logic this used to duplicate inline.
    # asyncio.to_thread()-wrapped (2026-08-16, Pitfall 26, COPILOT_PITFALLS_COOKBOOK.md) — this
    # bridge handler shares the bot's event loop (see module docstring), so an un-wrapped sync
    # write here freezes the whole bot exactly like an interaction callback would.
    error_message = await asyncio.to_thread(
        assign_cwl_player_sync, guild_id, event["id"], season, player_tag, clan_tag,
        source="admin_override", locked=True,
    )
    # 2026-08-20: a private cross-guild placement conflict (see assign_cwl_player_sync's own
    # docstring) refuses the write entirely rather than silently double-booking the player into
    # two guilds' rosters for the same season — surfaced here the same way the guest-invite
    # endpoint's own cross-guild conflict is (409, plain error string), before any Hub-message
    # refresh or version bump runs for what would otherwise look like a no-op successful drop.
    if error_message is not None:
        return web.json_response({"error": error_message}, status=409)

    try:
        await refresh_cwl_management_hub_message(guild_id, "cwl_management")
    except Exception as e:
        logging.warning(f"[WEB-BRIDGE] Saved assignment but could not refresh the Hub message: {e}")
    # Step 8: assign_cwl_player_sync can write to another guild's cwl_shared_clan_players (target
    # is a shared clan) or purge the player from one they're placed in elsewhere — it doesn't
    # currently report which, so bump globally rather than resolve the exact partner guild for
    # every single drag-and-drop move (see bump_enrollment_version's own docstring for the
    # tradeoff; this is the plan's explicitly-sanctioned simpler fallback).
    await bump_enrollment_version(None)

    return web.json_response({"ok": True})


# 2026-08-17 PROD meltdown fix, Step 3 (CWL_PROD_PERFORMANCE_FIX_PLAN.md): single-flight,
# newest-wins guard for the guest search per (guild_id, discord_user_id). Each request bumps its
# own generation number for that key BEFORE attempting the per-key Semaphore(1) — this is what
# lets a keystroke that's still queued behind an in-flight search discover, the instant it
# acquires the semaphore, that an even-newer keystroke has already superseded it, and bail out
# immediately (no thread dispatch at all) instead of running a now-pointless search. A request
# that's already RUNNING when it gets superseded is NOT interrupted — it finishes and returns its
# real (if by-then-stale) results; the frontend's own searchRequestId guard is what discards
# those on render, so the backend doesn't need to duplicate that logic. Net effect: at most one
# guest-search thread ever runs per admin at a time, and a burst of keystrokes collapses to
# "whatever was already running" + "the latest one", never a pile-up of every keystroke in
# between.
_guest_search_generation: Dict[tuple, int] = {}
_guest_search_semaphores: Dict[tuple, asyncio.Semaphore] = {}


def _guest_search_semaphore_for(key: tuple) -> asyncio.Semaphore:
    sem = _guest_search_semaphores.get(key)
    if sem is None:
        sem = asyncio.Semaphore(1)
        _guest_search_semaphores[key] = sem
    return sem


async def handle_get_cwl_guest_search(request: web.Request) -> web.Response:
    """Backs the "Guests" search box on Configure Participating Clans — see
    _search_cwl_guests_sync()'s docstring for the actual matching logic. Admin-only, same as
    every other route here; a plain empty-results response (not an error) for a query with no
    hits. Runs off the event loop via asyncio.to_thread (2026-08-17, Pitfall 26,
    COPILOT_PITFALLS_COOKBOOK.md — this incident is exactly why READ paths need the same
    treatment as writes, not just this one) with the single-flight guard described above.

    When the DB search comes back with nothing real and the query is a well-formed CoC tag, falls
    back to a live CoC API lookup that also persists the hit — see
    _resolve_guest_tag_via_coc_api()."""
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

    key = (guild_id, discord_user_id)
    my_generation = _guest_search_generation.get(key, 0) + 1
    _guest_search_generation[key] = my_generation

    async with _guest_search_semaphore_for(key):
        if _guest_search_generation.get(key) != my_generation:
            # Superseded by a newer keystroke while queued behind another search for this same
            # admin — skip the now-pointless work entirely rather than run it just to have the
            # frontend throw the result away.
            return web.json_response({"results": [], "stale": True})
        results = await asyncio.to_thread(_search_cwl_guests_sync, guild_id, query)

    # Nothing real in the DB for a query that looks like a tag → ask the CoC API and persist
    # whatever it finds (2026-08-20) — see _resolve_guest_tag_via_coc_api(). Deliberately outside
    # the single-flight semaphore: it awaits network I/O, and holding the per-admin lock across
    # that would serialize an admin's keystrokes behind a remote call.
    if not any(not r.get("unverified") for r in results):
        api_hit = await _resolve_guest_tag_via_coc_api(guild_id, query)
        if api_hit is not None:
            results = [api_hit]

    for result in results:
        result.pop("unverified", None)

    return web.json_response({"results": results})


async def handle_post_cwl_enrollment_guest(request: web.Request) -> web.Response:
    """Adds one guest PLAYER directly to the current season's cwl_signups — bypassing the "must
    already be a current member of a participating clan" restriction every other pool-seeding
    path (Start Enrollment, auto-assign) enforces via get_current_clan_members_sync(), since the
    whole point of a guest is that they aren't. Guest CLANS never go through this endpoint at
    all — they're added straight into cwl_event_clans via the existing POST /cwl/clan-config
    save, same as any other clan (see _search_cwl_guests_sync's docstring for why that needs no
    new persistence of its own).

    Purely a pool-add now (2026-08-18, CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md, rules c/e) —
    no longer sends an immediate DM. Rule c: a guest player invited before enrollment starts is
    added to the pool and gets DMed only when Start Enrollment runs (or, once already open, via
    the rule-h "notify new pool members" flow) — the "Send enrollment DM immediately" checkbox
    that used to bypass that timing is gone, replaced entirely by that later flow."""
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
        guild_id = int(body["guild_id"])
        discord_user_id = int(body["discord_user_id"])
        player_tag = str(body["player_tag"]).upper()
        player_name = str(body.get("player_name") or player_tag)
        guest_discord_id = body.get("discord_id")
    except (KeyError, ValueError, TypeError):
        return web.json_response({"error": "invalid request body"}, status=400)

    if not await _resolve_admin(guild_id, discord_user_id):
        return web.json_response({"error": "not an admin of this guild"}, status=403)

    db = CACHE.db_manager
    if db is None:
        return web.json_response({"error": "database not ready"}, status=503)

    from qapbot.QBdiscocmdshelper_cwl import resolve_selected_cwl_season, get_cwl_guest_clan_tags_sync
    from qapbot.ui_cwl_roster import refresh_cwl_management_hub_message

    season = resolve_selected_cwl_season(guild_id)
    event = await asyncio.to_thread(db.get_cwl_event_sync, str(guild_id), season)
    if event is None:
        return web.json_response(
            {"error": f"no CWL event exists yet for season {season}"}, status=409
        )

    # Race condition 1 (2026-08-19, guest-player provenance feature, project owner's spec):
    # reject an individual add if the player's LIVE current clan is already a guest clan on this
    # event's roster — they're already in the pool via that clan (rule f), so a second,
    # individual add would be redundant and would wrongly make them independently removable.
    #
    # Race condition 2 (2026-08-20, live bug report, project owner: guest-inviting a player
    # individually while they were already deliberately placed in a DIFFERENT guild's own CWL
    # roster this season left them showing as plain Unassigned here — nothing at all like the
    # "Assigned to other Guild" treatment a shared-clan conflict already gets. An individual
    # guest-player invite is a completely separate code path from a guest CLAN and never gets
    # cwl_shared_clan_players' cross-guild conflict machinery; without this check, dragging them
    # into a column here would then give them a SECOND, fully independent placement in a second
    # guild's roster for the same season — something real CWL rules never allow, and something
    # this guild has no authority to silently resolve on another guild's behalf. Blocked here
    # (not silently evicted from the other guild) for the same reason a locked local assignment
    # elsewhere is never silently overridden either — see find_cwl_player_private_placement_in_
    # other_guilds_sync's own docstring.
    # asyncio.to_thread()-wrapped as one atomic unit (2026-08-16, Pitfall 26,
    # COPILOT_PITFALLS_COOKBOOK.md) — this bridge handler shares the bot's event loop with the
    # Discord gateway/interactions (see module docstring), so a sync DB call made directly here
    # would freeze the whole bot exactly like an un-wrapped Discord interaction callback would.
    def _check_and_upsert_sync() -> Optional[str]:
        guest_clan_tags = get_cwl_guest_clan_tags_sync(db, event["id"], guild_id)
        current_clan_tag = db.get_current_clan_tags_for_players_sync([player_tag]).get(player_tag)
        if current_clan_tag and current_clan_tag in guest_clan_tags:
            clan_name = CACHE.get_clan_name(current_clan_tag, current_clan_tag) or current_clan_tag
            return (
                f"{player_name} is already in the player pool as a member of guest clan "
                f"{clan_name} — they can't be added individually. Remove the whole guest "
                f"clan to remove them."
            )

        other_placements = db.find_cwl_player_private_placement_in_other_guilds_sync(
            player_tag, season, str(guild_id)
        )
        if other_placements:
            import QBcore

            placement = other_placements[0]
            other_clan_name = (
                CACHE.get_clan_name(placement["clan_tag"], placement["clan_tag"]) or placement["clan_tag"]
            )
            other_guild = QBcore.bot.get_guild(int(placement["guild_id"]))
            other_guild_name = other_guild.name if other_guild else f"guild {placement['guild_id']}"
            return (
                f"{player_name} is already placed in {other_clan_name}'s CWL roster in "
                f"{other_guild_name} this season — they can't be individually guest-invited "
                f"while that placement stands."
            )

        # A guest invited AFTER they already answered another guild's DM must show that real
        # response, not a fresh 'pending' contradicting it (rule h) — same seeding
        # start_cwl_enrollment does. 2026-08-22.
        from qapbot.QBdiscocmdshelper_cwl import _seed_status_from_global_sync

        db.upsert_cwl_signup_sync(
            event["id"], player_tag, player_name, guest_discord_id, None,
            source="guest_invite", status=_seed_status_from_global_sync(db, player_tag, season),
        )
        return None

    error_message = await asyncio.to_thread(_check_and_upsert_sync)
    if error_message is not None:
        return web.json_response({"error": error_message}, status=409)

    try:
        await refresh_cwl_management_hub_message(guild_id, "cwl_management")
    except Exception as e:
        logging.warning(f"[WEB-BRIDGE] Saved guest but could not refresh the Hub message: {e}")
    # A guest PLAYER add is always local to this guild's own cwl_signups — no shared-clan branch
    # (see this handler's own docstring: guest CLANS go through a different endpoint entirely).
    await bump_enrollment_version(guild_id)

    return web.json_response({"ok": True})


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

    payload = await asyncio.to_thread(_build_clan_config_payload_sync, guild_id)
    return web.json_response(payload)


def _prepare_and_save_clan_config_sync(db: Any, guild_id: int, season: str, clans_in: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Synchronous unit of work for handle_post_clan_config() — everything from the initial event
    lookup through set_cwl_event_clans_sync() has no `await` between any of it today, so it's
    bundled into one atomic asyncio.to_thread() hop (2026-08-16, Pitfall 26,
    COPILOT_PITFALLS_COOKBOOK.md) rather than wrapped call-by-call, which would introduce new
    interleaving windows a concurrent save for the same event could race through. Returns
    {"error": ..., "status": ...} on failure, else {"event_id", "event_status",
    "previously_participating", "clan_configs"} for the caller's post-await tail (the cross-guild
    sharing/detach loops below, which DO need real awaits and so stay in the async function)."""
    # Never creates a season itself (CWL_CLAN_CONFIG_ACTIVITY_PLAN.md Phase E.3: that's
    # exclusively "Add New Season"'s job) — the guild's currently-selected season must already
    # have an event, or this is a stale/pre-bootstrap request and the admin needs to use
    # "Add New Season" in Discord first.
    event = db.get_cwl_event_sync(str(guild_id), season)
    if event is None:
        return {
            "error": f"no CWL event exists yet for season {season} — use \"Add New Season\" in Discord first",
            "status": 409,
        }
    event_id = event["id"]

    # Snapshot of what was on the roster BEFORE this save.
    existing_rows = db.get_cwl_event_clans_sync(event_id)
    # Participating-only subset — the cross-guild sharing check below (2026-08-15) only needs to
    # run for clans newly turning on, not every clan on every save.
    previously_participating = {c["clan_tag"] for c in existing_rows if c.get("participating", 1)}
    # ALL previously-known tags, participating or not (2026-08-18, rule b/d,
    # CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md) — lets the caller tell "genuinely new to the
    # roster this save" apart from "already here, just re-toggled," since pool-seeding now needs
    # to trigger on the former regardless of participating state.
    previously_known_tags = {c["clan_tag"] for c in existing_rows}

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
    # Locked-clan freeze (2026-08-30, project owner's spec: "As soon as a clan started their cwl
    # season in-game it can no longer be removed from the clan roster"). A clan whose CWL is
    # already running in-game has its settings forced back to what's stored, exactly like the
    # shared-clan guard right below does for a non-owner guild — the frontend renders these rows
    # read-only, and this is the server-side half that makes it real rather than cosmetic.
    # Silently overriding (rather than 4xx-ing the whole save) is deliberate and matches that
    # existing guard: one frozen clan must not block an admin's legitimate edits to every other
    # clan in the same save.
    locked_rows = {c["clan_tag"]: c for c in existing_rows if c.get("locked_at")}
    for config in clan_configs:
        locked = locked_rows.get(config["clan_tag"])
        if locked is None:
            continue
        config["participating"] = bool(locked.get("participating", 1))
        config["roster_size"] = locked["roster_size"]
        config["cwl_start_at"] = locked["cwl_start_at"]
        logging.info(
            f"[CWL-FREEZE] guild {guild_id} event {event_id}: {config['clan_tag']} has started CWL "
            f"in-game — submitted changes ignored, stored settings kept"
        )
    # A locked clan omitted from the payload entirely would lose its row (set_cwl_event_clans_sync
    # deletes anything not sent), which would silently un-freeze it. Re-append it instead.
    sent_tags = {c["clan_tag"] for c in clan_configs}
    for clan_tag, row in locked_rows.items():
        if clan_tag in sent_tags:
            continue
        clan_configs.append({
            "clan_tag": clan_tag,
            "target_league_rank": row["target_league_rank"],
            "roster_size": row["roster_size"],
            "tier_order": row["tier_order"],
            "cwl_start_at": row["cwl_start_at"],
            "participating": bool(row.get("participating", 1)),
        })
        logging.warning(
            f"[CWL-FREEZE] guild {guild_id} event {event_id}: {clan_tag} was omitted from a save "
            f"but has already started CWL — row restored"
        )
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

    return {
        "event_id": event_id,
        "event_status": event["status"],
        "previously_participating": previously_participating,
        "previously_known_tags": previously_known_tags,
        "clan_configs": clan_configs,
    }


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
        auto_assign_prior_cwl_members,
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
    # asyncio.to_thread()-wrapped as one bundled hop — see _prepare_and_save_clan_config_sync's
    # own docstring (2026-08-16, Pitfall 26, COPILOT_PITFALLS_COOKBOOK.md).
    prepared = await asyncio.to_thread(_prepare_and_save_clan_config_sync, db, guild_id, season, clans_in)
    if prepared.get("error"):
        return web.json_response({"error": prepared["error"]}, status=prepared["status"])
    event_id = prepared["event_id"]
    event_status = prepared["event_status"]
    previously_participating = prepared["previously_participating"]
    previously_known_tags = prepared["previously_known_tags"]
    clan_configs = prepared["clan_configs"]

    # Cross-guild shared-clan check (2026-08-15, project owner's spec) — the first of the two
    # trigger points (the other is start_cwl_enrollment). The frontend's Guests search already
    # highlighted an already-claimed clan and had the admin confirm adding it anyway (see
    # _search_cwl_guests_sync's docstring) — this is the mechanical step that actually establishes
    # the shared record now that the save is going through. ensure_cwl_clan_sharing() is a
    # cheap no-op for the overwhelming majority of clans that aren't shared with anyone.
    newly_participating_tags = [
        c["clan_tag"] for c in clan_configs
        if c["participating"] and c["clan_tag"] not in previously_participating
    ]
    # Step 8: guild_ids whose board also needs a version bump because THIS save touched a shared
    # clan they're attached to — precise where the write path already resolves it cheaply
    # (ensure_cwl_clan_sharing's own return value), global fallback where it doesn't (detach,
    # below) — see bump_enrollment_version's own docstring for the tradeoff.
    also_bump_guild_ids: set = set()
    for clan_tag in newly_participating_tags:
        sharing_result = await ensure_cwl_clan_sharing(guild_id, event_id, season, clan_tag)
        if sharing_result is not None:
            also_bump_guild_ids.update(int(gid) for gid in sharing_result.get("other_guild_ids", []))
            try:
                await notify_cwl_clan_shared(guild_id, clan_tag, season, sharing_result, acting_discord_id=discord_user_id)
            except Exception as e:
                logging.warning(f"[WEB-BRIDGE] Clan {clan_tag} became shared but notification failed: {e}")

        # Auto-assign-on-add (2026-08-15, live-testing feedback, project owner's spec) — only
        # once enrollment has actually started (a still-draft event is about to get a
        # comprehensive seed from Start Enrollment itself, which already covers this). Runs on
        # every (re-)activation of the clan, including a re-add after a prior removal — gated per
        # PLAYER, not per clan, so it correctly fills in any newly-qualifying members even when a
        # couple of deliberately locked placements already survived the removal (2026-08-16, see
        # auto_assign_prior_cwl_members's own docstring).
        if event_status != "draft":
            try:
                await auto_assign_prior_cwl_members(guild_id, event_id, season, clan_tag)
            except Exception as e:
                logging.warning(f"[WEB-BRIDGE] Clan {clan_tag} added but prior-CWL auto-assign failed: {e}")

    # Pool-seed a newly-added clan that stayed UNCHECKED (2026-08-18, rule b/d,
    # CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md, live-tested bug: a guest clan invited and
    # immediately left unchecked never got its members into the pool at all — "in the pool"
    # must not depend on the participating checkbox, only the auto-assignment TARGET does).
    # Deliberately separate from the loop above: no cross-guild sharing/ownership resolution for
    # a clan that isn't even participating, and auto_assign_prior_cwl_members itself already
    # skips actually placing anyone into a non-participating clan's column (pure pool-visibility
    # seed only — see its own docstring) — so this only needs to run once enrollment is open,
    # same gate as the loop above.
    newly_added_non_participating_tags = [
        c["clan_tag"] for c in clan_configs
        if not c["participating"] and c["clan_tag"] not in previously_known_tags
    ]
    if event_status != "draft":
        for clan_tag in newly_added_non_participating_tags:
            try:
                await auto_assign_prior_cwl_members(guild_id, event_id, season, clan_tag)
            except Exception as e:
                logging.warning(f"[WEB-BRIDGE] Clan {clan_tag} added (unchecked) but pool-visibility seed failed: {e}")

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

    # Removing a clan strands anyone already TOLD they'd play for it (2026-08-30, spec item 5).
    # They effectively return to the pool — _build_enrollment_payload drops assignments pointing at
    # a non-participating clan — but the assignment row itself survives, so the ordinary
    # moved/dropped comparison would never notice and they'd sit holding instructions for a clan
    # that isn't in the season any more. Tombstoning them here routes them through the exact same
    # "Send Roster Updates" batch as every other change, rather than inventing a second DM path.
    if newly_deactivated_tags and event_status not in ("draft", "cancelled"):
        try:
            await asyncio.to_thread(
                _tombstone_announced_players_of_removed_clans_sync,
                event_id, newly_deactivated_tags,
            )
        except Exception as e:
            logging.warning(f"[WEB-BRIDGE] Clan removal: could not record pending update DMs: {e}")

    # Best-effort — a failure here shouldn't fail the save itself (matches how Discord-side
    # callbacks treat repost/refresh failures as logged-not-raised).
    try:
        await refresh_cwl_management_hub_message(guild_id, "cwl_management")
    except Exception as e:
        logging.warning(f"[WEB-BRIDGE] Saved clan-config but could not refresh the Hub message: {e}")

    await bump_enrollment_version(guild_id)
    for other_guild_id in also_bump_guild_ids:
        await bump_enrollment_version(other_guild_id)
    if newly_deactivated_tags:
        # detach_guild_from_shared_clan_on_deactivation() doesn't report which guild(s) it
        # repointed ownership to — global fallback for this (rare) branch only.
        await bump_enrollment_version(None)

    return web.json_response({"ok": True, "event_id": event_id})


def _tombstone_announced_players_of_removed_clans_sync(event_id: int, removed_clan_tags: List[str]) -> int:
    """Mark every already-announced player of a just-removed clan as owed an update DM
    (2026-08-30, spec item 5). Returns how many were recorded.

    Only players with a `notified_clan_tag` are recorded: someone who was never told where they'd
    play has nothing to be corrected about, and telling them they've been removed from a roster
    they never knew they were on would be pure noise."""
    db = CACHE.db_manager
    if db is None or not removed_clan_tags:
        return 0
    removed = set(removed_clan_tags)
    names = {s["player_tag"]: s["player_name"] for s in db.get_cwl_signups_for_event_sync(event_id)}
    recorded = 0
    for assignment in db.get_cwl_assignments_sync(event_id):
        if assignment["assigned_clan_tag"] not in removed or not assignment["notified_clan_tag"]:
            continue
        db.record_cwl_dropped_notified_player_sync(
            event_id, assignment["player_tag"],
            names.get(assignment["player_tag"]),
            assignment["notified_clan_tag"],
        )
        recorded += 1
    if recorded:
        logging.info(
            f"[CWL-UPDATE] event {event_id}: {recorded} announced player(s) pending a removal DM "
            f"after clan(s) {', '.join(sorted(removed))} left the season"
        )
    return recorded


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
    # Step 8: both guild_ids are already known precisely here — no need for the global fallback.
    await bump_enrollment_version(guild_id)
    await bump_enrollment_version(target_guild_id)

    return web.json_response({"ok": True})


async def handle_post_cwl_guest_clan_remove(request: web.Request) -> web.Response:
    """Full removal of a guest clan from the season (rule f's "Remove" button, 2026-08-18,
    CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md) — as opposed to unchecking a guest clan's
    checkbox and saving (now purely cosmetic, see detach_guild_from_shared_clan_on_deactivation's
    own docstring), this deletes the clan's cwl_event_clans row entirely AND removes its members
    from the player pool (the destructive cleanup that used to run automatically on every
    uncheck+Save). Rejects a family-clan tag outright — family membership is never removable this
    way, fail closed here rather than relying solely on remove_cwl_guest_clan's own internal
    protection logic."""
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
        guild_id = int(body["guild_id"])
        discord_user_id = int(body["discord_user_id"])
        clan_tag = str(body["clan_tag"])
    except (KeyError, ValueError, TypeError):
        return web.json_response({"error": "invalid request body"}, status=400)

    if not await _resolve_admin(guild_id, discord_user_id):
        return web.json_response({"error": "not an admin of this guild"}, status=403)

    db = CACHE.db_manager
    if db is None:
        return web.json_response({"error": "database not ready"}, status=503)

    from qapbot.QBdiscocmdshelper_cwl import (
        remove_cwl_guest_clan,
        resolve_guild_member_clan_tags,
        resolve_selected_cwl_season,
    )
    from qapbot.ui_cwl_roster import refresh_cwl_management_hub_message

    if clan_tag in resolve_guild_member_clan_tags(guild_id):
        return web.json_response({"error": "cannot remove a family clan this way"}, status=400)

    season = resolve_selected_cwl_season(guild_id)
    event = await asyncio.to_thread(db.get_cwl_event_sync, str(guild_id), season)
    if event is None:
        return web.json_response({"error": f"no CWL event exists yet for season {season}"}, status=409)
    event_id = event["id"]

    existing_clans = await asyncio.to_thread(db.get_cwl_event_clans_sync, event_id)
    if not any(c["clan_tag"] == clan_tag for c in existing_clans):
        return web.json_response({"error": "clan is not on this event's roster"}, status=404)

    await remove_cwl_guest_clan(guild_id, event_id, season, clan_tag)

    try:
        await refresh_cwl_management_hub_message(guild_id, "cwl_management")
    except Exception as e:
        logging.warning(f"[WEB-BRIDGE] Removed guest clan {clan_tag} but could not refresh the Hub message: {e}")
    await bump_enrollment_version(guild_id)

    return web.json_response({"ok": True})


async def handle_get_cwl_guest_players(request: web.Request) -> web.Response:
    """Rule g (2026-08-18, CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md) — lists every pooled
    player currently carrying the guest badge, for the "Remove Guest Players" multi-select on
    Configure Participating Clans: individually-invited guest players (source='guest_invite')
    AND orphaned ex-guest-clan players (their clan's cwl_event_clans row was unchecked or
    deleted, but rule f means their own cwl_signups/cwl_assignments rows survived that).

    Deliberately reuses _build_enrollment_payload_sync's own is_guest computation (Cardinal Rule
    4 — search before creating) rather than re-deriving it: that function already redefines
    is_guest around live current-clan membership regardless of write-path source ("a member is a
    member regardless of assignment status, a guest is a guest regardless," project owner's spec)
    — exactly the definition rule g's removal list needs, and the only place in this codebase
    that already computes it correctly for every player in the pool.

    Further filtered (2026-08-19, guest-player provenance feature, project owner's spec) to
    exclude guest-clan-derived players — a guest player whose LIVE current clan is still on the
    event roster as a guest clan can only be removed by removing that whole clan (rule f), so
    listing them here would offer a removal action that handle_post_cwl_guest_players_remove
    would just reject. An orphaned ex-guest-clan player (their clan's roster row is gone) is NOT
    filtered here — get_cwl_guest_clan_tags_sync no longer includes that clan's tag, so they
    correctly fall through as individually-removable, matching rule f's "their own rows survived"
    guarantee."""
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        guild_id = int(request.query["guild_id"])
        discord_user_id = int(request.query["discord_user_id"])
    except (KeyError, ValueError):
        return web.json_response({"error": "missing/invalid guild_id or discord_user_id"}, status=400)

    if not await _resolve_admin(guild_id, discord_user_id):
        return web.json_response({"error": "not an admin of this guild"}, status=403)

    db = CACHE.db_manager
    if db is None:
        return web.json_response({"error": "database not ready"}, status=503)

    from qapbot.QBdiscocmdshelper_cwl import resolve_selected_cwl_season, get_cwl_guest_clan_tags_sync

    def _build_sync() -> List[Dict[str, Any]]:
        payload = _build_enrollment_payload_sync(guild_id)
        candidates = [p for p in payload["players"] if p["is_guest"]]
        season = resolve_selected_cwl_season(guild_id)
        event = db.get_cwl_event_sync(str(guild_id), season)
        if event is None:
            return candidates
        guest_clan_tags = get_cwl_guest_clan_tags_sync(db, event["id"], guild_id)
        # Also excludes a player currently showing in the "Assigned to other Guild" pseudo-column
        # — defense in depth alongside the real fix in
        # _cleanup_local_pool_for_plain_clan_deactivation_sync (QBdiscocmdshelper_cwl.py, 2026-08-19):
        # that function now purges a deliberately-placed player who's a genuine DIRECT member of
        # the clan being removed (they never belonged in "Assigned to other Guild" — see its own
        # docstring), so the normal Remove path no longer leaves one of these dangling in the
        # first place. This filter still matters for the genuinely legitimate case (a real member
        # of THIS guild's own pool, deliberately cross-assigned to another guild's clan, correctly
        # preserved) — that player's local row DOES survive, on purpose, and must never be listed
        # here as "individually removable": handle_post_cwl_guest_players_remove is scoped to
        # LOCAL tables only, so removing them here would silently break the very placement rule f
        # exists to protect, without the "affects another guild" warning the drag-and-drop path
        # gives via the column-header tooltip. After the (91) payload-builder fix, ANY player
        # whose assigned_clan_tag is non-null but not among the currently-participating clans
        # (payload["clans"]) is, by construction, pointing at a genuine cross-guild shared clan —
        # see _build_enrollment_payload_sync's own comment on that filter for why nothing else can
        # produce that combination anymore.
        participating_clan_tags = {c["clan_tag"] for c in payload["clans"]}
        return [
            p for p in candidates
            if p["current_clan_tag"] not in guest_clan_tags
            and not (p["assigned_clan_tag"] is not None and p["assigned_clan_tag"] not in participating_clan_tags)
        ]

    guest_player_rows = await asyncio.to_thread(_build_sync)
    guest_players = [
        {
            "player_tag": p["player_tag"],
            "player_name": p["player_name"],
            "current_clan_tag": p["current_clan_tag"],
            "assigned_clan_tag": p["assigned_clan_tag"],
        }
        for p in guest_player_rows
    ]
    return web.json_response({"players": guest_players})


async def handle_post_cwl_guest_players_remove(request: web.Request) -> web.Response:
    """Rule g's actual removal — per tag, the same two calls
    _cleanup_local_pool_for_plain_clan_deactivation_sync already uses (delete_cwl_assignment_sync
    + delete_cwl_signup_sync), no new deletion primitive needed. Deliberately does NOT touch
    cwl_player_season_status (Phase 5) — rule h requires a removed-then-re-added guest player's
    global dm_sent/response record to survive this exact operation.

    Scoped to LOCAL cwl_signups/cwl_assignments only — a guest player who also happens to sit in
    a cross-guild shared clan's roster (cwl_shared_clan_players) is a much rarer case this action
    doesn't reach; that roster has its own dedicated removal path (shared-clan eviction/detach).

    Rejects guest-clan-derived tags instead of deleting them (2026-08-19, guest-player provenance
    feature, project owner's spec, verbatim: "If a user tries so ... the bot should inform the
    user that this is a player added by the guest clan. Only removing the whole guest clan will
    remove that player.") — reached both from the Configure Participating Clans panel (now
    pre-filtered by handle_get_cwl_guest_players, so this should be rare there) and from the
    Manage Teams board's per-player context menu (not pre-filtered, so this is the primary
    enforcement point for that path)."""
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
        guild_id = int(body["guild_id"])
        discord_user_id = int(body["discord_user_id"])
        player_tags = [str(t) for t in body["player_tags"]]
    except (KeyError, ValueError, TypeError):
        return web.json_response({"error": "invalid request body"}, status=400)

    if not await _resolve_admin(guild_id, discord_user_id):
        return web.json_response({"error": "not an admin of this guild"}, status=403)

    db = CACHE.db_manager
    if db is None:
        return web.json_response({"error": "database not ready"}, status=503)

    from qapbot.QBdiscocmdshelper_cwl import resolve_selected_cwl_season, get_cwl_guest_clan_tags_sync
    from qapbot.ui_cwl_roster import refresh_cwl_management_hub_message

    season = resolve_selected_cwl_season(guild_id)
    event = await asyncio.to_thread(db.get_cwl_event_sync, str(guild_id), season)
    if event is None:
        return web.json_response({"error": f"no CWL event exists yet for season {season}"}, status=409)
    event_id = event["id"]

    def _remove_players_sync() -> Tuple[List[str], List[Dict[str, str]]]:
        guest_clan_tags = get_cwl_guest_clan_tags_sync(db, event_id, guild_id)
        current_clan_tags = db.get_current_clan_tags_for_players_sync(player_tags)
        removed: List[str] = []
        rejected: List[Dict[str, str]] = []
        for tag in player_tags:
            current_clan_tag = current_clan_tags.get(tag)
            if current_clan_tag and current_clan_tag in guest_clan_tags:
                rejected.append({
                    "player_tag": tag,
                    "clan_tag": current_clan_tag,
                    "clan_name": CACHE.get_clan_name(current_clan_tag, current_clan_tag) or current_clan_tag,
                })
                continue
            db.delete_cwl_assignment_sync(event_id, tag)
            db.delete_cwl_signup_sync(event_id, tag)
            removed.append(tag)
        return removed, rejected

    removed, rejected = await asyncio.to_thread(_remove_players_sync)

    if removed:
        try:
            await refresh_cwl_management_hub_message(guild_id, "cwl_management")
        except Exception as e:
            logging.warning(f"[WEB-BRIDGE] Removed {len(removed)} guest player(s) but could not refresh the Hub message: {e}")
        await bump_enrollment_version(guild_id)

    return web.json_response({"ok": True, "removed": removed, "rejected": rejected})


async def handle_post_cwl_activity_closed(request: web.Request) -> web.Response:
    """Best-effort notification that the Activity iframe is about to close — fired by
    main.ts's closeActivity() on EVERY close (Save, Cancel, or the native X/back gesture alike),
    not just after a successful save (2026-08-16, live-testing feedback: on iPad, the Hub
    message's launch buttons stayed visibly greyed out/unresponsive after closing the Activity —
    Discord's own client-side "an Activity was launched from this message" visual state, which
    QapBot's own code never sets directly (no `disabled=True` anywhere tied to Activity session
    state — only `refresh_cwl_management_hub_message()`'s pre-existing save-triggered refreshes
    happened to incidentally clear it, since each is a genuine new message.edit() REST call, a
    completely different HTTP request from "respond to the original interaction" and so not
    subject to whatever Discord's client had cached for that specific interaction). Closing
    WITHOUT saving anything (a plain view, or Cancel) never triggered any refresh at all, leaving
    nothing to clear that stuck state — this endpoint plugs that gap by unconditionally
    triggering the exact same refresh on every close, regardless of whether anything changed.
    Never returns an error status for anything past auth — a missing/misconfigured Hub message
    is `refresh_cwl_management_hub_message()`'s own no-op, not a client-visible failure, and the
    Activity is already in the process of closing by the time this fires.

    Deliberately does NOT call bump_enrollment_version() (2026-08-17, Step 8) — unlike every
    other refresh_cwl_management_hub_message() call site in this module, this one fires
    unconditionally on every close regardless of whether anything actually changed, so bumping
    here would wake every parked wait() for this guild on a plain Cancel/back-gesture close with
    zero real change, defeating the "steady-state = zero rebuilds" point of the wait endpoint."""
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
        guild_id = int(body["guild_id"])
        discord_user_id = int(body["discord_user_id"])
    except (KeyError, ValueError, TypeError):
        return web.json_response({"error": "invalid request body"}, status=400)

    if not await _resolve_admin_or_leader(guild_id, discord_user_id):
        return web.json_response({"error": "not an admin or leader of this guild"}, status=403)

    from qapbot.ui_cwl_roster import refresh_cwl_management_hub_message

    try:
        await refresh_cwl_management_hub_message(guild_id, "cwl_management")
    except Exception as e:
        logging.warning(f"[WEB-BRIDGE] Activity-closed Hub refresh failed: {e}")

    # Trigger (b) of the spec's three ways to flush pending line-up updates (2026-08-30). The spec
    # asked for an ephemeral here; that is impossible — an ephemeral needs a live interaction token
    # and discordSdk.close() is a client-side call with none behind it (the original
    # LAUNCH_ACTIVITY both consumed its one response slot and expires after 15 minutes). A DM to
    # the admin who just closed the board carries the same prompt and the same button.
    try:
        await _dm_pending_roster_updates_notice(guild_id, discord_user_id)
    except Exception as e:
        logging.warning(f"[WEB-BRIDGE] Activity-closed pending-updates DM failed: {e}")

    return web.json_response({"ok": True})


async def handle_post_cwl_send_roster_updates(request: web.Request) -> web.Response:
    """Trigger (a) of the three the spec asks for (2026-08-30): the "Send Roster Updates" button
    sitting beside Close on the Teams Management board itself, so an admin who has just finished
    reshuffling can flush the DMs without leaving the screen.

    Gated by _resolve_admin_or_leader — the same gate as the board's other write actions, since an
    admin-only gate here would let a leader (or, since 2026-08-30, a coordinator) rearrange the
    board and then silently fail on the one action that tells anyone about it."""
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
        guild_id = int(body["guild_id"])
        discord_user_id = int(body["discord_user_id"])
    except (KeyError, ValueError, TypeError):
        return web.json_response({"error": "invalid request body"}, status=400)

    if not await _resolve_admin_or_leader(guild_id, discord_user_id):
        return web.json_response({"error": "not an admin or leader of this guild"}, status=403)

    from qapbot.QBdiscocmdshelper_cwl import resolve_selected_cwl_season, send_cwl_roster_updates

    season = await asyncio.to_thread(resolve_selected_cwl_season, guild_id)
    result = await send_cwl_roster_updates(guild_id, season)
    if not result["ok"]:
        return web.json_response({"error": result["error"]}, status=409)

    from qapbot.ui_cwl_roster import refresh_cwl_management_hub_message

    try:
        await refresh_cwl_management_hub_message(guild_id, "cwl_management")
    except Exception as e:
        logging.warning(f"[WEB-BRIDGE] Hub refresh after roster updates failed: {e}")
    return web.json_response({
        "ok": True, "moved": result["moved"], "new": result["new"], "dropped": result["dropped"],
        "contacted_users": result["contacted_users"],
        "skipped_unlinked": result["skipped_unlinked"],
        "skipped_dm_guard": result["skipped_dm_guard"],
    })


async def handle_post_cwl_notify_new_pool_members(request: web.Request) -> web.Response:
    """The Teams Management board's own copy of the Hub's "Notify New Pool Members" button
    (2026-08-30, project owner's spec: "same logic for all three buttons... in all three views") —
    same underlying notify_new_cwl_pool_members() the Hub button calls, just reachable from the
    board itself so an admin reshuffling the pool doesn't have to leave it. Same admin-or-leader
    gate as the board's other write actions (send-updates, assign)."""
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
        guild_id = int(body["guild_id"])
        discord_user_id = int(body["discord_user_id"])
    except (KeyError, ValueError, TypeError):
        return web.json_response({"error": "invalid request body"}, status=400)

    if not await _resolve_admin_or_leader(guild_id, discord_user_id):
        return web.json_response({"error": "not an admin or leader of this guild"}, status=403)

    from qapbot.QBdiscocmdshelper_cwl import resolve_selected_cwl_season

    season = await asyncio.to_thread(resolve_selected_cwl_season, guild_id)
    result = await notify_new_cwl_pool_members(guild_id, season)
    if not result["ok"]:
        return web.json_response({"error": result["error"]}, status=409)

    from qapbot.ui_cwl_roster import refresh_cwl_management_hub_message

    try:
        await refresh_cwl_management_hub_message(guild_id, "cwl_management")
    except Exception as e:
        logging.warning(f"[WEB-BRIDGE] Hub refresh after notify-new-members failed: {e}")
    return web.json_response({"ok": True, "contacted": result["contacted"]})


async def handle_post_cwl_remind_pending(request: web.Request) -> web.Response:
    """The Teams Management board's own copy of the Hub's "Remind Pending" button (2026-08-30,
    project owner's spec — same "same logic... in all three views" reasoning as notify-new-members
    above). Same underlying remind_pending_cwl_players() the Hub button calls."""
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
        guild_id = int(body["guild_id"])
        discord_user_id = int(body["discord_user_id"])
    except (KeyError, ValueError, TypeError):
        return web.json_response({"error": "invalid request body"}, status=400)

    if not await _resolve_admin_or_leader(guild_id, discord_user_id):
        return web.json_response({"error": "not an admin or leader of this guild"}, status=403)

    from qapbot.QBdiscocmdshelper_cwl import resolve_selected_cwl_season

    season = await asyncio.to_thread(resolve_selected_cwl_season, guild_id)
    result = await remind_pending_cwl_players(guild_id, season)
    if not result["ok"]:
        return web.json_response({"error": result["error"]}, status=409)

    from qapbot.ui_cwl_roster import refresh_cwl_management_hub_message

    try:
        await refresh_cwl_management_hub_message(guild_id, "cwl_management")
    except Exception as e:
        logging.warning(f"[WEB-BRIDGE] Hub refresh after remind-pending failed: {e}")
    return web.json_response({"ok": True, "contacted": result["contacted"]})


async def _dm_pending_roster_updates_notice(guild_id: int, discord_user_id: int) -> None:
    """DM whoever just closed the Teams Management board if they left line-up changes unsent.

    Deliberately silent when nothing is pending — the overwhelmingly common case is closing a board
    you only looked at, and a "you have 0 updates" DM on every close would train people to ignore
    the one that matters. Best-effort throughout: this fires while the Activity is already closing,
    so nothing here may raise into the response."""
    from qapbot.QBdiscocmdshelper_cwl import (
        count_cwl_pending_roster_updates, resolve_selected_cwl_season,
    )
    from qapbot.i18n import t
    from qapbot.ui_cwl_roster import CwlPendingUpdatesDmView

    season = await asyncio.to_thread(resolve_selected_cwl_season, guild_id)
    pending = await asyncio.to_thread(count_cwl_pending_roster_updates, guild_id, season)
    if not pending:
        return
    await CACHE.send_user_dm(
        str(discord_user_id),
        t(
            'cwl.management.close_pending_updates_dm',
            user_id=str(discord_user_id), guild_id=guild_id, season=season, count=pending,
        ),
        view=CwlPendingUpdatesDmView(guild_id, season),
    )


# ---------------------------------------------------------------------------
# Bug/feature tracker (BUG_FEATURE_TRACKER_PLAN.md Phase 6) — reuses the existing shared
# X-Bridge-Secret (plan §6.4/§8.7: holding it grants tracker-admin + CWL-endpoint access,
# accepted for a single-admin setup). X-Tracker-Admin is attribution-only — whose name shows
# up on status changes/comments the agent posts — deliberately NOT authentication, since it's
# self-asserted by whoever already holds the real secret.
# ---------------------------------------------------------------------------

def _tracker_admin_label(request: web.Request) -> str:
    return request.headers.get("X-Tracker-Admin") or "agent"


async def handle_get_tracker_items(request: web.Request) -> web.Response:
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    status = request.query.get("status")
    item_type = request.query.get("type")
    try:
        limit = int(request.query.get("limit", "50"))
    except ValueError:
        limit = 50

    items = await CACHE.db_manager.list_tracker_items(status=status, item_type=item_type, limit=limit)  # type: ignore[union-attr]
    payload = [
        {
            "item_number": it["item_number"], "item_type": it["item_type"], "status": it["status"],
            "priority": it["priority"],
            "title": it["title"], "reporter_id": it["reporter_id"], "reporter_name": it["reporter_name"],
            "created_at": it["created_at"],
        }
        for it in items
    ]
    return web.json_response({"items": payload})


async def handle_post_tracker_create_item(request: web.Request) -> web.Response:
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    from qapbot.ui_tracker import create_tracker_item_for_agent

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid body"}, status=400)
    item_type = body.get("item_type")
    title = (body.get("title") or "").strip()
    description = (body.get("description") or "").strip()
    if item_type not in ("bug", "feature"):
        return web.json_response({"error": "item_type must be 'bug' or 'feature'"}, status=400)
    if not title or not description:
        return web.json_response({"error": "title and description are required"}, status=400)
    if "priority" in body and body["priority"] not in ("HIGH", "MEDIUM", "LOW"):
        return web.json_response({"error": "priority must be one of HIGH/MEDIUM/LOW"}, status=400)

    try:
        result = await create_tracker_item_for_agent(
            item_type=item_type, title=title, description=description,
            details=body.get("details"), environment=body.get("environment"),
            priority=body.get("priority", "MEDIUM"), reporter_name=_tracker_admin_label(request),
        )
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({
        "ok": True, "item_number": result["item"]["item_number"], "jump_url": result["jump_url"],
    })


async def handle_get_tracker_item(request: web.Request) -> web.Response:
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        item_number = int(request.match_info["item_number"])
    except ValueError:
        return web.json_response({"error": "invalid item_number"}, status=400)

    item = await CACHE.db_manager.get_tracker_item(item_number)  # type: ignore[union-attr]
    if item is None:
        return web.json_response({"error": "not found"}, status=404)
    attachments = await CACHE.db_manager.get_tracker_attachments(item_number)  # type: ignore[union-attr]
    testcases = await CACHE.db_manager.get_tracker_testcases(item_number)  # type: ignore[union-attr]
    return web.json_response({
        "item": dict(item),
        "attachments": [
            {"id": a["id"], "filename": a["filename"], "original_name": a["original_name"],
             "content_type": a["content_type"], "size_bytes": a["size_bytes"]}
            for a in attachments
        ],
        "testcases": [dict(c) for c in testcases],
    })


async def handle_get_tracker_attachment(request: web.Request) -> web.StreamResponse:
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        item_number = int(request.match_info["item_number"])
        attachment_id = int(request.match_info["attachment_id"])
    except ValueError:
        return web.json_response({"error": "invalid id"}, status=400)

    attachments = await CACHE.db_manager.get_tracker_attachments(item_number)  # type: ignore[union-attr]
    match = next((a for a in attachments if a["id"] == attachment_id), None)
    if match is None:
        return web.json_response({"error": "not found"}, status=404)

    # Path containment check: local_path is always written under CONFIG.tracker_data_dir at
    # attachment-store time (ui_tracker._persist_attachment) — this re-verifies it rather than
    # trusting the stored value blindly, matching the untrusted-input posture of plan §6.6.
    from qapbot.config import CONFIG
    local_path = os.path.abspath(match["local_path"])
    tracker_root = os.path.abspath(CONFIG.tracker_data_dir)
    if os.path.commonpath([local_path, tracker_root]) != tracker_root:
        return web.json_response({"error": "invalid path"}, status=400)
    if not os.path.exists(local_path):
        return web.json_response({"error": "file missing on disk"}, status=404)
    return web.FileResponse(local_path)


async def handle_post_tracker_status(request: web.Request) -> web.Response:
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    from qapbot.ui_tracker import STATUS_VALUES, apply_status_change

    try:
        item_number = int(request.match_info["item_number"])
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid body"}, status=400)
    new_status = body.get("status")
    if new_status not in STATUS_VALUES:
        return web.json_response({"error": f"status must be one of {list(STATUS_VALUES)}"}, status=400)

    try:
        item = await apply_status_change(item_number, new_status, note=body.get("note"), actor_id=_tracker_admin_label(request))
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=404)
    return web.json_response({"ok": True, "item": dict(item)})


async def handle_post_tracker_comment(request: web.Request) -> web.Response:
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    from qapbot.ui_tracker import post_comment

    try:
        item_number = int(request.match_info["item_number"])
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid body"}, status=400)
    text = (body.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "text required"}, status=400)

    try:
        await post_comment(item_number, text, _tracker_admin_label(request))
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=404)
    except discord.HTTPException as e:
        # 2026-08-23, tracker #0028: this used to propagate uncaught, and aiohttp's own default
        # error handler turned it into a bare text/plain 500 the MCP client couldn't parse as
        # JSON at all — reported as "500, message='Attempt to decode JSON with unexpected
        # mimetype'". post_comment() now chunks long comments, so this should be rare; still
        # caught here so a genuine Discord-side failure (outage, permissions) reports a real
        # error instead of a mimetype-confusion message that hides what actually went wrong.
        logging.error(f"[TRACKER] Discord rejected the comment for item #{item_number}: {e}")
        return web.json_response({"error": f"Discord rejected the comment: {e}"}, status=502)
    return web.json_response({"ok": True})


async def handle_get_tracker_thread(request: web.Request) -> web.Response:
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    from qapbot.ui_tracker import get_thread_messages

    try:
        item_number = int(request.match_info["item_number"])
    except ValueError:
        return web.json_response({"error": "invalid item_number"}, status=400)
    try:
        limit = max(1, min(int(request.query.get("limit", "50")), 200))
    except ValueError:
        return web.json_response({"error": "invalid limit"}, status=400)

    try:
        messages = await get_thread_messages(item_number, limit=limit)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=404)
    return web.json_response({"messages": messages})


async def handle_post_tracker_testcases(request: web.Request) -> web.Response:
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    from qapbot.ui_tracker import post_test_cases

    try:
        item_number = int(request.match_info["item_number"])
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid body"}, status=400)
    cases = body.get("cases")
    if not isinstance(cases, list) or not cases:
        return web.json_response({"error": "cases required (non-empty list)"}, status=400)
    for case in cases:
        if not isinstance(case, dict) or case.get("environment") not in ("DEV", "PROD") or not case.get("description"):
            return web.json_response({"error": "each case needs environment (DEV/PROD) and description"}, status=400)
        if "priority" in case and case["priority"] not in ("HIGH", "MEDIUM", "LOW"):
            return web.json_response({"error": "priority must be one of HIGH/MEDIUM/LOW"}, status=400)

    try:
        item = await post_test_cases(item_number, cases, actor_id=_tracker_admin_label(request))
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=404)
    except discord.HTTPException as e:
        # 2026-08-23, tracker #0028 — same reasoning as handle_post_tracker_comment above.
        # post_test_cases() itself now chunks (_post_or_refresh_testcase_message), so this is a
        # genuine Discord-side failure rather than the routine case; note that set_tracker_
        # testcases() (the DB write) already committed by this point regardless of whether the
        # Discord post below it succeeds — a caller retrying on this error re-sends the same
        # cases, which is safe (set_tracker_testcases replaces the full set, not appends).
        logging.error(f"[TRACKER] Discord rejected the test-case message for item #{item_number}: {e}")
        return web.json_response({"error": f"Discord rejected the test-case message: {e}"}, status=502)
    return web.json_response({"ok": True, "item": dict(item)})


async def handle_post_tracker_testcase_pass(request: web.Request) -> web.Response:
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    from qapbot.ui_tracker import mark_environment_passed_and_refresh

    try:
        item_number = int(request.match_info["item_number"])
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid body"}, status=400)
    environment = body.get("environment")
    if environment not in ("DEV", "PROD"):
        return web.json_response({"error": "environment must be DEV or PROD"}, status=400)

    item = await CACHE.db_manager.get_tracker_item(item_number)  # type: ignore[union-attr]
    if item is None:
        return web.json_response({"error": "not found"}, status=404)
    testcases = await CACHE.db_manager.get_tracker_testcases(item_number)  # type: ignore[union-attr]
    if not any(c["environment"] == environment for c in testcases):
        return web.json_response({"error": f"no test cases for environment {environment}"}, status=400)

    result = await mark_environment_passed_and_refresh(item_number, environment, _tracker_admin_label(request))
    item = await CACHE.db_manager.get_tracker_item(item_number)  # type: ignore[union-attr]
    testcases = await CACHE.db_manager.get_tracker_testcases(item_number)  # type: ignore[union-attr]
    linked_item = result.get("linked_item")
    return web.json_response({
        "ok": True, "item": dict(item), "testcases": [dict(c) for c in testcases],
        "testcases_just_completed": result["just_completed"],
        "moved": result["moved"],
        "linked_item": dict(linked_item) if linked_item is not None else None,
    })


async def handle_post_tracker_testcase_fail(request: web.Request) -> web.Response:
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    from qapbot.ui_tracker import mark_testing_failed

    try:
        item_number = int(request.match_info["item_number"])
    except ValueError:
        return web.json_response({"error": "invalid item_number"}, status=400)

    item = await CACHE.db_manager.get_tracker_item(item_number)  # type: ignore[union-attr]
    if item is None:
        return web.json_response({"error": "not found"}, status=404)

    await mark_testing_failed(item_number, _tracker_admin_label(request))
    item = await CACHE.db_manager.get_tracker_item(item_number)  # type: ignore[union-attr]
    return web.json_response({"ok": True, "item": dict(item)})


async def handle_post_tracker_testcase_move_done(request: web.Request) -> web.Response:
    if not _check_secret(request):
        return web.json_response({"error": "forbidden"}, status=403)
    from qapbot.ui_tracker import finalize_testcases_move

    try:
        item_number = int(request.match_info["item_number"])
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid body"}, status=400)
    force = bool(body.get("force", False))

    item = await CACHE.db_manager.get_tracker_item(item_number)  # type: ignore[union-attr]
    if item is None:
        return web.json_response({"error": "not found"}, status=404)
    testcases = await CACHE.db_manager.get_tracker_testcases(item_number)  # type: ignore[union-attr]
    if not testcases:
        return web.json_response({"error": "no test cases posted for this item"}, status=400)
    unchecked = [c for c in testcases if not c["passed"]]
    if unchecked and not force:
        return web.json_response({
            "ok": True, "moved": False, "needs_confirmation": True,
            "unchecked_count": len(unchecked),
            "unchecked": [{"environment": c["environment"], "description": c["description"]} for c in unchecked],
        })

    result = await finalize_testcases_move(item_number)
    linked_item = result.get("linked_item")
    return web.json_response({
        "ok": True, "moved": result["moved"], "needs_confirmation": False,
        "linked_item": dict(linked_item) if linked_item is not None else None,
    })


def create_app() -> web.Application:
    app = web.Application(middlewares=[_access_log_middleware])
    app.router.add_get("/api/health", handle_health)
    app.router.add_get("/api/i18n", handle_get_i18n)
    app.router.add_get("/api/cwl/player-prefs", handle_get_cwl_player_prefs)
    app.router.add_post("/api/cwl/player-prefs", handle_post_cwl_player_prefs)
    app.router.add_post("/api/cwl/player-prefs/status", handle_post_cwl_player_prefs_status)
    app.router.add_get("/api/cwl/clan-config", handle_get_clan_config)
    app.router.add_post("/api/cwl/clan-config", handle_post_clan_config)
    app.router.add_get("/api/cwl/screen", handle_get_cwl_screen)
    app.router.add_get("/api/cwl/enrollment", handle_get_cwl_enrollment)
    app.router.add_get("/api/cwl/enrollment/wait", handle_get_cwl_enrollment_wait)
    app.router.add_get("/api/cwl/clan-names", handle_get_cwl_clan_names)
    app.router.add_get("/api/cwl/player-stats", handle_get_cwl_player_stats)
    app.router.add_post("/api/cwl/enrollment/assign", handle_post_cwl_enrollment_assign)
    app.router.add_post("/api/cwl/enrollment/status", handle_post_cwl_enrollment_status)
    app.router.add_get("/api/cwl/guest-search", handle_get_cwl_guest_search)
    app.router.add_post("/api/cwl/enrollment/guest", handle_post_cwl_enrollment_guest)
    app.router.add_post("/api/cwl/enrollment/guest-clan/remove", handle_post_cwl_guest_clan_remove)
    app.router.add_get("/api/cwl/enrollment/guest-players", handle_get_cwl_guest_players)
    app.router.add_post("/api/cwl/enrollment/guest-players/remove", handle_post_cwl_guest_players_remove)
    app.router.add_post("/api/cwl/shared-clan/evict", handle_post_cwl_shared_clan_evict)
    app.router.add_post("/api/cwl/activity-closed", handle_post_cwl_activity_closed)
    app.router.add_post("/api/cwl/enrollment/send-updates", handle_post_cwl_send_roster_updates)
    app.router.add_post("/api/cwl/enrollment/notify-new-members", handle_post_cwl_notify_new_pool_members)
    app.router.add_post("/api/cwl/enrollment/remind-pending", handle_post_cwl_remind_pending)
    app.router.add_get("/api/tracker/items", handle_get_tracker_items)
    app.router.add_post("/api/tracker/items", handle_post_tracker_create_item)
    app.router.add_get("/api/tracker/items/{item_number}", handle_get_tracker_item)
    app.router.add_get("/api/tracker/items/{item_number}/attachments/{attachment_id}", handle_get_tracker_attachment)
    app.router.add_post("/api/tracker/items/{item_number}/status", handle_post_tracker_status)
    app.router.add_post("/api/tracker/items/{item_number}/comment", handle_post_tracker_comment)
    app.router.add_get("/api/tracker/items/{item_number}/thread", handle_get_tracker_thread)
    app.router.add_post("/api/tracker/items/{item_number}/testcases", handle_post_tracker_testcases)
    app.router.add_post("/api/tracker/items/{item_number}/testcases/pass", handle_post_tracker_testcase_pass)
    app.router.add_post("/api/tracker/items/{item_number}/testcases/fail", handle_post_tracker_testcase_fail)
    app.router.add_post("/api/tracker/items/{item_number}/testcases/move-done", handle_post_tracker_testcase_move_done)
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
