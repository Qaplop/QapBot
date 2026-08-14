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
from typing import Any, Dict, List, Optional

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

    all_tags = get_guild_clans_including_member_config(guild_id)
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

    default_start_at = f"{season}-01T08:00Z"

    # Highest tier first (CWL_CLAN_CONFIG_ACTIVITY_PLAN.md Phase E), name as the tiebreaker —
    # matches the Discord-side CWL Management embed's own sort.
    def _sort_key(tag: str) -> tuple:
        return (-cwl_league_rank(CACHE.get_clan_war_league(tag)), (CACHE.get_clan_name(tag, tag) or tag).lower())

    clans: List[Dict[str, Any]] = []
    for tag in sorted(all_tags, key=_sort_key):
        row = known_rows.get(tag)
        clans.append({
            "clan_tag": tag,
            "name": CACHE.get_clan_name(tag, tag),
            # Tier is always the live value (CoC-defined, never admin-set) — same rule as the
            # Discord-side screens, see CWL_ROSTER_PLANNING_PLAN.md's tier fix.
            "tier": CACHE.get_clan_war_league(tag),
            "participating": bool(row["participating"]) if row else False,
            "roster_size": row["roster_size"] if row else 15,
            "cwl_start_at": row["cwl_start_at"] if row else default_start_at,
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
    applies to auto-assignment)."""
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
    clans = [
        {
            "clan_tag": c["clan_tag"],
            "name": CACHE.get_clan_name(c["clan_tag"], c["clan_tag"]),
            "tier": _tier_for(c),
            "roster_size": c["roster_size"],
        }
        for c in participating_clans
    ]

    players_by_tag: Dict[str, Dict[str, Any]] = {}
    for signup in db.get_cwl_signups_for_event_sync(event["id"]):
        players_by_tag[signup["player_tag"]] = {
            "player_tag": signup["player_tag"],
            "player_name": signup["player_name"],
            "discord_id": signup["discord_id"],
            "signup_status": signup["status"],
        }
    # user_players.th_level (2026-08-14: now kept fresh for every current member, linked or not
    # — see coc_cache.py's update_player_info_in_user_accounts) is the primary TH source: live
    # from the CoC API, not dependent on the player ever having made a tracked war attack.
    # Pool is every guild member clan (not just this season's participating ones) — see this
    # function's docstring.
    all_member_clan_tags = resolve_guild_member_clan_tags(guild_id)
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
                "signup_status": None,
            }

    assigned_clan_by_tag = {
        a["player_tag"]: a["assigned_clan_tag"] for a in db.get_cwl_assignments_sync(event["id"])
    }
    # Fallback only for player_tags live_th_by_tag didn't cover (e.g. a signed-up player who has
    # since left every participating clan, so get_current_clan_members_sync no longer returns
    # them) — bounded to just this payload's own player_tags (never the whole war_attacks table,
    # see DATABASE_ARCHITECTURE.md's query anti-patterns).
    fallback_tags = [tag for tag in players_by_tag if tag not in live_th_by_tag]
    th_levels_by_tag = db.get_most_recent_th_levels_sync(fallback_tags)
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
        # None for the same reason — lets the board tell "no current clan on record" apart from
        # "currently in a different clan than their assignment" (same-clan/different-clan
        # highlighting, 2026-08-14).
        player["current_clan_tag"] = current_clan_by_tag.get(player_tag)

    players = sorted(players_by_tag.values(), key=lambda p: (p["player_name"] or p["player_tag"]).lower())

    return {
        "season": season,
        "event_status": event["status"],
        "clans": clans,
        "players": players,
    }


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

    from qapbot.QBdiscocmdshelper_cwl import resolve_selected_cwl_season
    from qapbot.ui_cwl_roster import refresh_cwl_management_hub_message

    season = resolve_selected_cwl_season(guild_id)
    event = db.get_cwl_event_sync(str(guild_id), season)
    if event is None:
        return web.json_response(
            {"error": f"no CWL event exists yet for season {season}"}, status=409
        )

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

    new_status = "confirmed" if action == "confirm" else "withdrawn"
    responded_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
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

    from qapbot.QBdiscocmdshelper_cwl import resolve_selected_cwl_season
    from qapbot.ui_cwl_roster import refresh_cwl_management_hub_message

    season = resolve_selected_cwl_season(guild_id)
    event = db.get_cwl_event_sync(str(guild_id), season)
    if event is None:
        return web.json_response(
            {"error": f"no CWL event exists yet for season {season}"}, status=409
        )

    if clan_tag is None:
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

    from qapbot.QBdiscocmdshelper_cwl import resolve_selected_cwl_season
    from qapbot.ui_cwl_roster import refresh_cwl_management_hub_message

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
    db.set_cwl_event_clans_sync(event_id, clan_configs)

    # Best-effort — a failure here shouldn't fail the save itself (matches how Discord-side
    # callbacks treat repost/refresh failures as logged-not-raised).
    try:
        await refresh_cwl_management_hub_message(guild_id, "cwl_management")
    except Exception as e:
        logging.warning(f"[WEB-BRIDGE] Saved clan-config but could not refresh the Hub message: {e}")

    return web.json_response({"ok": True, "event_id": event_id})


def create_app() -> web.Application:
    app = web.Application(middlewares=[_access_log_middleware])
    app.router.add_get("/api/health", handle_health)
    app.router.add_get("/api/cwl/clan-config", handle_get_clan_config)
    app.router.add_post("/api/cwl/clan-config", handle_post_clan_config)
    app.router.add_get("/api/cwl/screen", handle_get_cwl_screen)
    app.router.add_get("/api/cwl/enrollment", handle_get_cwl_enrollment)
    app.router.add_post("/api/cwl/enrollment/signup", handle_post_cwl_enrollment_signup)
    app.router.add_post("/api/cwl/enrollment/assign", handle_post_cwl_enrollment_assign)
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
