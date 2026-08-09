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


async def _build_clan_config_payload(guild_id: int, season: Optional[str] = None) -> Dict[str, Any]:
    """Build the GET response for a given season (or the guild's "current" one if *season* is
    omitted — preserves the pre-dropdown default of resolving via get_current_cwl_event_sync()).

    Season-aware defaults (Phase E.2/E.4): a clan with no row yet for the selected season
    defaults to roster_size=15, cwl_start_at=the 1st of that season's month at 08:00 UTC (the
    game's static schedule) and participating=False — never silently carried over from a
    previous season. Carry-over is instead surfaced as an explicit, frontend-driven choice via
    has_own_data/carry_over_available/carry_over_season/previous_clans below, so the user picks
    "start fresh" vs. "copy last month" rather than it happening invisibly.
    """
    from qapbot.QBdiscocmdshelper import get_guild_clans_including_member_config
    from qapbot.QBdiscocmdshelper_cwl import get_current_cwl_event_sync, resolve_current_cwl_season

    all_tags = get_guild_clans_including_member_config(guild_id)
    db = CACHE.db_manager

    if season is not None:
        event = db.get_cwl_event_sync(str(guild_id), season) if db is not None else None
    else:
        event = get_current_cwl_event_sync(guild_id)
        season = event["cwl_season"] if event else resolve_current_cwl_season()

    # Keyed by clan_tag -> its cwl_event_clans row, which now exists for every clan ever
    # touched (participating or not) — deactivating a clan keeps its row (with settings intact)
    # instead of deleting it, so reactivating doesn't lose roster_size/cwl_start_at. "row exists"
    # is therefore no longer the same thing as "participating"; use the explicit column instead.
    known_rows: Dict[str, Dict[str, Any]] = {}
    if db is not None and event is not None:
        for row in db.get_cwl_event_clans_sync(event["id"]):
            known_rows[row["clan_tag"]] = row
    has_own_data = bool(known_rows)

    default_start_at = f"{season}-01T08:00Z"

    clans: List[Dict[str, Any]] = []
    for tag in sorted(all_tags, key=lambda t: (CACHE.get_clan_name(t, t) or t).lower()):
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

    available_seasons = {resolve_current_cwl_season(), season}
    event_season_by_id: Dict[int, str] = {}
    if db is not None:
        for e in db.list_cwl_events_sync(str(guild_id)):
            available_seasons.add(e["cwl_season"])
            event_season_by_id[e["id"]] = e["cwl_season"]

    previous_clans: Optional[List[Dict[str, Any]]] = None
    carry_over_season: Optional[str] = None
    if db is not None and not has_own_data:
        prev_rows = db.get_previous_cwl_event_clans_sync(
            str(guild_id), exclude_event_id=event["id"] if event is not None else None
        )
        if prev_rows:
            carry_over_season = event_season_by_id.get(prev_rows[0]["event_id"])
            previous_clans = [
                {
                    "clan_tag": r["clan_tag"],
                    "roster_size": r["roster_size"],
                    "cwl_start_at": r["cwl_start_at"],
                }
                for r in prev_rows
            ]

    return {
        "season": season,
        "event_status": event["status"] if event else None,
        "available_seasons": sorted(available_seasons, reverse=True),
        "has_own_data": has_own_data,
        "carry_over_available": previous_clans is not None,
        "carry_over_season": carry_over_season,
        "clans": clans,
        "previous_clans": previous_clans,
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

    season = request.query.get("season") or None
    return web.json_response(await _build_clan_config_payload(guild_id, season))


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

    from qapbot.QBdiscocmdshelper_cwl import resolve_current_cwl_season
    from qapbot.ui_cwl_roster import refresh_cwl_management_hub_message

    season = body.get("season") or resolve_current_cwl_season()
    event_id = db.create_cwl_event_sync(str(guild_id), season, str(discord_user_id))
    if event_id is None:
        return web.json_response({"error": "failed to create/find event"}, status=500)

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
