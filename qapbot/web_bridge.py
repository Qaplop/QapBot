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
import time
from typing import Any, Dict, List, Optional, Set, Tuple

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
        logging.info(
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

    return {
        "season": season,
        "event_status": event["status"],
        "clans": clans,
        "players": players,
        "version": get_enrollment_version(guild_id),
    }


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
        if not player_hits:
            return []
        links = db.get_player_links_sync(list(player_hits.keys()))
        out: List[Dict[str, Any]] = []
        for tag, hit in player_hits.items():
            link = links.get(tag, {})
            out.append({
                "type": "player",
                "player_tag": tag,
                "player_name": link.get("player_name") or hit["player_name"],
                "discord_id": link.get("discord_id"),
            })
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
    for clan_tag, _info in CACHE.clan_name_cache.items():
        if len(clan_hits) >= GUEST_SEARCH_CAP:
            break
        if clan_tag in already_participating:
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
        for tag, name in CACHE.player_name_index.items():
            if len(player_hits) >= GUEST_SEARCH_CAP:
                break
            if tag.upper().startswith(upper_query):
                player_hits.setdefault(tag, {"player_tag": tag, "player_name": name})
        # A tag typed exactly that the index doesn't know about at all — still offered as a raw
        # hit (name falls back to the tag itself) so the admin can add it directly; whether it's
        # actually a real CoC tag is only found out once something tries to use it. Added
        # regardless of the cap above (a single entry — the final [:12] slice below re-caps it).
        if len(upper_query) >= 5 and upper_query not in player_hits:
            player_hits[upper_query] = {"player_tag": upper_query, "player_name": upper_query}
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
        logging.info(
            f"[WEB-BRIDGE] enrollment/wait guild={guild_id_str} known_version={known_version} "
            f"already stale (current={current}) -> immediate changed=true"
        )
        return web.json_response({"changed": True, "version": current})

    # Waiter cap (2026-08-17, Step 8) — beyond this many parked coroutines for one guild, degrade
    # gracefully (report "changed" so the client refetches and re-issues the wait with a fresh
    # known_version) rather than accumulate coroutines without bound.
    if _enrollment_waiter_counts.get(guild_id_str, 0) >= _ENROLLMENT_WAIT_MAX_WAITERS_PER_GUILD:
        logging.info(f"[WEB-BRIDGE] enrollment/wait guild={guild_id_str} waiter cap hit -> immediate changed=true")
        return web.json_response({"changed": True, "version": current})

    _enrollment_waiter_counts[guild_id_str] = _enrollment_waiter_counts.get(guild_id_str, 0) + 1
    logging.info(
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
    logging.info(f"[WEB-BRIDGE] enrollment/wait guild={guild_id_str} released -> changed={changed} version={current}")
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


def _apply_cwl_enrollment_signup_sync(
    db: Any, event_id: int, season: str, player_tag: str, action: str, guild_id: int,
) -> Dict[str, Any]:
    """Synchronous unit of work for handle_post_cwl_enrollment_signup() — see that function's own
    comment for why this is one atomic asyncio.to_thread() hop. Returns {"ok": True} on success,
    or {"error": ..., "status": ...} for the one case this needs to report back as an HTTP error."""
    from datetime import datetime, timezone

    from qapbot.QBdiscocmdshelper_cwl import get_event_shared_clans_by_tag_sync, sync_cwl_shared_clan_roster_to_local_pools

    # Cross-guild shared-clan write-path branch (2026-08-15, slice 4) — checked FIRST: a player
    # already sitting in a shared clan's roster may exist ONLY there (e.g. this guild's own
    # cwl_signups/get_current_clan_members_sync have never heard of them — they joined the
    # roster via the OTHER attached guild), so this also doubles as this branch's own
    # player_name/discord_id resolution rather than falling through to the normal one below and
    # 404ing on a player this guild's own tables genuinely don't know about.
    player_shared_clan = None
    shared_roster_row = None
    for shared in get_event_shared_clans_by_tag_sync(event_id, season).values():
        match = next(
            (p for p in db.get_cwl_shared_clan_players_sync(shared["id"]) if p["player_tag"] == player_tag), None
        )
        if match is not None:
            player_shared_clan, shared_roster_row = shared, match
            break

    new_status = "confirmed" if action == "confirm" else "withdrawn"
    responded_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")

    if player_shared_clan is not None:
        # Status-only (2026-08-16, live-testing feedback, project owner's spec: confirmation and
        # assignment must never be conflated) — confirming/withdrawing records the player's own
        # genuine response and nothing else; whether they're actually placed in this clan's
        # column (assigned) is a completely separate decision, left untouched either way.
        db.set_cwl_shared_clan_player_status_sync(
            player_shared_clan["id"], player_tag, shared_roster_row["player_name"], shared_roster_row["discord_id"],
            new_status, "admin_added", str(guild_id), responded_at,
        )
        # De-sync guard (2026-08-15) — see sync_cwl_shared_clan_roster_to_local_pools's docstring:
        # keeps every OTHER attached guild's own local cwl_signups pool aware of this player too.
        sync_cwl_shared_clan_roster_to_local_pools(player_shared_clan["id"])
    else:
        existing = db.get_cwl_signup_sync(event_id, player_tag)
        if existing is not None:
            player_name = existing["player_name"]
            discord_id = existing["discord_id"]
            preferred_league_rank = existing["preferred_league_rank"]
        else:
            all_clans = db.get_cwl_event_clans_sync(event_id)
            participating_clan_tags = [c["clan_tag"] for c in all_clans if c.get("participating", 1)]
            member = next(
                (m for m in db.get_current_clan_members_sync(participating_clan_tags) if m["player_tag"] == player_tag),
                None,
            )
            if member is None:
                return {"error": "player is not a current member of any participating clan", "status": 404}
            player_name = member["player_name"]
            discord_id = member["discord_id"]
            preferred_league_rank = member["preferred_league_rank"]

        db.upsert_cwl_signup_sync(
            event_id, player_tag, player_name, discord_id, preferred_league_rank,
            "admin_added", new_status, responded_at=responded_at,
        )
        # A withdrawn player shouldn't linger assigned to a clan column.
        if action == "withdraw":
            db.delete_cwl_assignment_sync(event_id, player_tag)

    # "shared" tells the caller whether sync_cwl_shared_clan_roster_to_local_pools() above may
    # have touched another guild's local pool too (2026-08-17, Step 8) — if so, the caller bumps
    # the enrollment version globally rather than just for this guild.
    return {"ok": True, "shared": player_shared_clan is not None}


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

    from qapbot.QBdiscocmdshelper_cwl import resolve_selected_cwl_season
    from qapbot.ui_cwl_roster import refresh_cwl_management_hub_message

    season = resolve_selected_cwl_season(guild_id)
    event = await asyncio.to_thread(db.get_cwl_event_sync, str(guild_id), season)
    if event is None:
        return web.json_response(
            {"error": f"no CWL event exists yet for season {season}"}, status=409
        )

    # Whole read/write sequence bundled into one asyncio.to_thread() hop (2026-08-16, Pitfall 26,
    # COPILOT_PITFALLS_COOKBOOK.md) — this bridge handler runs on the SAME event loop as the
    # Discord gateway/interactions (see module docstring), so a sync DB call made directly here
    # freezes the whole bot exactly like an un-wrapped Discord interaction callback would. No
    # `await` happens between the first DB read and the last DB write below, so one hop preserves
    # today's atomicity instead of introducing new interleaving windows a per-line wrap would.
    result = await asyncio.to_thread(
        _apply_cwl_enrollment_signup_sync, db, event["id"], season, player_tag, action, guild_id,
    )
    if result.get("error"):
        return web.json_response({"error": result["error"]}, status=result["status"])

    try:
        await refresh_cwl_management_hub_message(guild_id, "cwl_management")
    except Exception as e:
        logging.warning(f"[WEB-BRIDGE] Saved signup but could not refresh the Hub message: {e}")
    # Step 8: a shared-clan response (result["shared"]) also updated another guild's local pool
    # via sync_cwl_shared_clan_roster_to_local_pools() — bump globally rather than resolve the
    # exact partner guild here (see bump_enrollment_version's own docstring for the tradeoff).
    await bump_enrollment_version(None if result.get("shared") else guild_id)

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
    await asyncio.to_thread(
        assign_cwl_player_sync, guild_id, event["id"], season, player_tag, clan_tag,
        source="admin_override", locked=True,
    )

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
    treatment as writes, not just this one) with the single-flight guard described above."""
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

    return web.json_response({"results": results})


async def handle_post_cwl_enrollment_guest(request: web.Request) -> web.Response:
    """Adds one guest PLAYER directly to the current season's cwl_signups — bypassing the "must
    already be a current member of a participating clan" restriction
    handle_post_cwl_enrollment_signup enforces, since the whole point of a guest is that they
    aren't. Guest CLANS never go through this endpoint at all — they're added straight into
    cwl_event_clans via the existing POST /cwl/clan-config save, same as any other clan (see
    _search_cwl_guests_sync's docstring for why that needs no new persistence of its own)."""
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
    event = await asyncio.to_thread(db.get_cwl_event_sync, str(guild_id), season)
    if event is None:
        return web.json_response(
            {"error": f"no CWL event exists yet for season {season}"}, status=409
        )

    # asyncio.to_thread()-wrapped (2026-08-16, Pitfall 26, COPILOT_PITFALLS_COOKBOOK.md) — see
    # handle_post_cwl_enrollment_signup's comment for why this matters on this bridge specifically.
    await asyncio.to_thread(
        db.upsert_cwl_signup_sync, event["id"], player_tag, player_name, guest_discord_id, None,
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
    # A guest PLAYER add is always local to this guild's own cwl_signups — no shared-clan branch
    # (see this handler's own docstring: guest CLANS go through a different endpoint entirely).
    await bump_enrollment_version(guild_id)

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

    return {
        "event_id": event_id,
        "event_status": event["status"],
        "previously_participating": previously_participating,
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

    await bump_enrollment_version(guild_id)
    for other_guild_id in also_bump_guild_ids:
        await bump_enrollment_version(other_guild_id)
    if newly_deactivated_tags:
        # detach_guild_from_shared_clan_on_deactivation() doesn't report which guild(s) it
        # repointed ownership to — global fallback for this (rare) branch only.
        await bump_enrollment_version(None)

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
    # Step 8: both guild_ids are already known precisely here — no need for the global fallback.
    await bump_enrollment_version(guild_id)
    await bump_enrollment_version(target_guild_id)

    return web.json_response({"ok": True})


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

    return web.json_response({"ok": True})


def create_app() -> web.Application:
    app = web.Application(middlewares=[_access_log_middleware])
    app.router.add_get("/api/health", handle_health)
    app.router.add_get("/api/cwl/clan-config", handle_get_clan_config)
    app.router.add_post("/api/cwl/clan-config", handle_post_clan_config)
    app.router.add_get("/api/cwl/screen", handle_get_cwl_screen)
    app.router.add_get("/api/cwl/enrollment", handle_get_cwl_enrollment)
    app.router.add_get("/api/cwl/enrollment/wait", handle_get_cwl_enrollment_wait)
    app.router.add_get("/api/cwl/clan-names", handle_get_cwl_clan_names)
    app.router.add_get("/api/cwl/player-stats", handle_get_cwl_player_stats)
    app.router.add_post("/api/cwl/enrollment/signup", handle_post_cwl_enrollment_signup)
    app.router.add_post("/api/cwl/enrollment/assign", handle_post_cwl_enrollment_assign)
    app.router.add_get("/api/cwl/guest-search", handle_get_cwl_guest_search)
    app.router.add_post("/api/cwl/enrollment/guest", handle_post_cwl_enrollment_guest)
    app.router.add_post("/api/cwl/shared-clan/evict", handle_post_cwl_shared_clan_evict)
    app.router.add_post("/api/cwl/activity-closed", handle_post_cwl_activity_closed)
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
