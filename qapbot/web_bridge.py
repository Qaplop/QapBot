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


async def handle_health(request: web.Request) -> web.Response:
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
    app = web.Application()
    app.router.add_get("/api/health", handle_health)
    app.router.add_get("/api/cwl/clan-config", handle_get_clan_config)
    app.router.add_post("/api/cwl/clan-config", handle_post_clan_config)
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
    _runner = web.AppRunner(app)
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
