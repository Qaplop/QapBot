"""
CoC Clan Cache with stale-while-revalidate strategy.

Provides in-memory caching of CoC clan data to reduce API calls.
The cache uses a two-tier TTL approach:
- Fresh (< soft TTL): return cached data immediately
- Stale (soft..hard TTL): return cached, trigger background refresh
- Expired (> hard TTL): blocking API refresh

Memory Usage:
    ~8-10 KB per clan (with full 50 members)
    For 20 clans (typical): ~200 KB
"""

import asyncio
import logging
import sys
from typing import Dict, Any, List, Optional, Set, TYPE_CHECKING
from datetime import datetime, timezone

import coc  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from qapbot.cache_manager import CacheManager

from qapbot.exceptions import CacheError
from qapbot.coc_health import coc_retry
from qapbot.constants import WAR_UPDATE_LEAGUES as _WAR_UPDATE_LEAGUES


class CoCClanCache:
    """
    In-memory cache for CoC clan data to reduce API calls.
    
    Uses a stale-while-revalidate strategy:
    - Fresh (< soft TTL): return cached data immediately
    - Stale (soft TTL..hard TTL): return cached data, trigger background refresh
    - Expired (> hard TTL): blocking API refresh
    
    Memory Usage:
        ~8-10 KB per clan (with full 50 members)
        For 100 clans: ~1 MB
        For 20 clans (typical): ~200 KB
    
    Example:
        cache = CoCClanCache(soft_ttl_seconds=280, hard_ttl_seconds=600)
        clan = await cache.get_clan("#2C9UR9GJY")
    """
    
    def __init__(self, soft_ttl_seconds: int = 280, hard_ttl_seconds: int = 600):
        """
        Initialize the clan cache with stale-while-revalidate TTLs.
        
        Args:
            soft_ttl_seconds: Soft TTL — data younger than this is considered fresh (default: 280s)
            hard_ttl_seconds: Hard TTL — data older than this requires a blocking refresh (default: 600s)
        """
        self.cache: Dict[str, Dict[str, Any]] = {}  # clan_tag -> {"data": clan_obj, "timestamp": datetime}
        self.soft_ttl_seconds = soft_ttl_seconds
        self.hard_ttl_seconds = hard_ttl_seconds
        self._refreshing: set[str] = set()  # Clan tags currently being refreshed in background
        self.cache_manager: Optional['CacheManager'] = None  # Set after CacheManager initialization
        # Note: Logging may not be configured yet during module import, so use try/except
        try:
            logging.debug(
                f"Initialized CoC Clan Cache with soft_TTL={soft_ttl_seconds}s "
                f"({soft_ttl_seconds/60:.1f}min), hard_TTL={hard_ttl_seconds}s "
                f"({hard_ttl_seconds/60:.1f}min)"
            )
        except Exception:
            pass  # Logging not yet configured during import
    
    async def get_clan(self, clan_tag: str) -> 'coc.Clan':
        """
        Get clan data from cache or API using stale-while-revalidate.
        
        - Fresh (< soft TTL): returns cached data immediately
        - Stale (soft..hard TTL): returns cached data, triggers background refresh
        - Expired (> hard TTL) or miss: blocking API fetch
        
        Args:
            clan_tag: Normalized clan tag (e.g., "#2C9UR9GJY")
            
        Returns:
            coc.Clan object
            
        Raises:
            CacheError: If CoC API client not initialized
            Same exceptions as coc_client.get_clan() if blocking API call fails
        """
        if not self.cache_manager or not self.cache_manager.coc_client:
            raise CacheError(
                "CoC API client not initialized. Call startup_login() first.",
                context={"operation": "get_clan", "clan_tag": clan_tag}
            )
        
        now = datetime.now(timezone.utc)
        
        if clan_tag in self.cache:
            cached = self.cache[clan_tag]
            age_seconds = (now - cached["timestamp"]).total_seconds()
            
            if age_seconds < self.soft_ttl_seconds:
                # Fresh — return immediately
                logging.debug(f"[COC-CACHE-HIT] {clan_tag} (age: {age_seconds:.1f}s)")
                return cached["data"]
            
            if age_seconds < self.hard_ttl_seconds:
                # Stale — return cached data, schedule background refresh
                logging.debug(
                    f"[COC-CACHE-STALE] {clan_tag} (age: {age_seconds:.1f}s, "
                    f"soft={self.soft_ttl_seconds}s, hard={self.hard_ttl_seconds}s)"
                )
                self._schedule_background_refresh(clan_tag)
                return cached["data"]
            
            # Expired — fall through to blocking refresh
            logging.debug(
                f"[COC-CACHE-EXPIRED] {clan_tag} (age: {age_seconds:.1f}s, "
                f"hard_TTL: {self.hard_ttl_seconds}s)"
            )
        else:
            logging.debug(f"[COC-CACHE-MISS] {clan_tag}")
        
        # Blocking fetch from API
        return await self._fetch_and_cache(clan_tag)
    
    async def _fetch_and_cache(self, clan_tag: str) -> 'coc.Clan':
        """
        Fetch clan from CoC API, update cache and clan metadata.
        
        Shared by both blocking get_clan() and background refresh.
        
        Args:
            clan_tag: Normalized clan tag
            
        Returns:
            coc.Clan object
        """
        logging.info(f"[COC-API-CALL] Fetching clan data for {clan_tag} from API")
        now = datetime.now(timezone.utc)
        
        async def _fetch_clan() -> 'coc.Clan':
            return await self.cache_manager.coc_client.get_clan(clan_tag)  # type: ignore[union-attr]
        
        clan_obj: 'coc.Clan' = await coc_retry(  # type: ignore[assignment]
            _fetch_clan,
            operation_name=f"get_clan({clan_tag})"
        )
        self.cache[clan_tag] = {
            "data": clan_obj,  # type: ignore[dict-item]
            "timestamp": now
        }
        
        # Update clan_name_cache and player info if cache_manager is available
        if self.cache_manager:
            await self._update_clan_metadata(clan_obj, now)
        
        return clan_obj
    
    async def _update_clan_metadata(self, clan_obj: 'coc.Clan', now: datetime) -> None:
        """Update clan name cache, warlog status, and player info from a fresh API response.
        
        Uses dirty-tracking to avoid unnecessary DB writes: only persists when
        meaningful data actually changed (name, warlog status, new clan).
        The last_checked_via_api timestamp is updated in-memory every call but
        only written to DB when piggy-backed on another real change, or when
        the cached timestamp is more than 1 hour stale.
        """
        # Update clan name cache (single source of truth for clan names)
        clan_data = self.cache_manager.clan_name_cache.get(clan_obj.tag)  # type: ignore[union-attr, attr-defined]
        dirty = False  # Track if a DB write is needed
        
        # Check if clan is new (not in cache yet)
        if clan_data is None:
            _wl_obj = getattr(clan_obj, 'war_league', None)
            _wl_name: Optional[str] = str(getattr(_wl_obj, 'name', '') or '') if _wl_obj else None
            _wl_name = _wl_name if _wl_name else None  # normalise empty string → None
            # All clans are added to the DB.  track_war_updates is league-gated:
            # M3+ → True (22h polling), M4- or unknown → False (no war updates).
            _track = _wl_name in _WAR_UPDATE_LEAGUES if _wl_name else False
            # New clan - create entry
            logging.info(f"[COC-CACHE] New clan added to database: {clan_obj.name} ({clan_obj.tag}) | league={_wl_name} | track={_track}")  # type: ignore[attr-defined]
            clan_data = {  # type: ignore[var-annotated]
                "name": clan_obj.name,  # type: ignore[attr-defined]
                "has_active_subscriptions": False,
                "last_war_update": None,
                "warlog_is_public": True,
                "last_checked_via_api": now.isoformat(),
                "war_league": _wl_name,
                "track_war_updates": _track,
                "is_deleted": False,
            }
            self.cache_manager.clan_name_cache[clan_obj.tag] = clan_data  # type: ignore[union-attr, index, attr-defined]
            dirty = True
        elif not isinstance(clan_data, dict):  # type: ignore[misc]
            # Unexpected non-dict format (data corruption safety guard)
            logging.error(f"[COC-CACHE] Clan {clan_obj.tag} has unexpected non-dict format in cache!")  # type: ignore[attr-defined]
            _wl_obj = getattr(clan_obj, 'war_league', None)
            _wl_name = str(getattr(_wl_obj, 'name', '') or '') if _wl_obj else None
            _wl_name = _wl_name if _wl_name else None
            clan_data = {  # type: ignore[var-annotated]
                "name": clan_obj.name,  # type: ignore[attr-defined]
                "has_active_subscriptions": False,
                "last_war_update": None,
                "warlog_is_public": True,
                "last_checked_via_api": now.isoformat(),
                "war_league": _wl_name,
                "track_war_updates": True,
                "is_deleted": False,
            }
            self.cache_manager.clan_name_cache[clan_obj.tag] = clan_data  # type: ignore[union-attr, index, attr-defined]
            dirty = True
        
        # Update clan name if changed
        if clan_data and clan_data.get("name") != clan_obj.name:  # type: ignore[attr-defined]
            clan_data["name"] = clan_obj.name  # type: ignore[attr-defined]
            logging.debug(f"[CLAN-NAME-UPDATE] {clan_obj.tag}: {clan_obj.name}")  # type: ignore[attr-defined]
            dirty = True

        # Clear is_deleted flag: a successful get_clan response proves the clan is alive.
        if clan_data and clan_data.get("is_deleted"):  # type: ignore[union-attr]
            clan_data["is_deleted"] = False
            logging.info(f"[CLAN-RESTORED] {clan_obj.tag} ({clan_obj.name}) — previously marked deleted, now confirmed alive.")  # type: ignore[attr-defined]
            dirty = True

        # Update war_league if changed (CWL league tier, e.g. "Crystal League I")
        # war_league only changes between CWL seasons (promotion/demotion), so this is usually a no-op.
        if clan_data is not None:  # type: ignore[misc]
            _wl_obj = getattr(clan_obj, 'war_league', None)
            _new_wl: Optional[str] = str(getattr(_wl_obj, 'name', '') or '') if _wl_obj else None
            _new_wl = _new_wl if _new_wl else None  # normalise empty string → None
            if _new_wl is not None and clan_data.get("war_league") != _new_wl:  # type: ignore[union-attr]
                _old_wl = clan_data.get("war_league")  # type: ignore[union-attr]
                clan_data["war_league"] = _new_wl
                logging.info(f"[WAR-LEAGUE-UPDATE] {clan_obj.tag}: {_old_wl} → {_new_wl}")  # type: ignore[attr-defined]
                dirty = True
                # League changed → update tracking status for non-subscribed clans only.
                # Subscribed / member clans (has_active_subscriptions) are always immune:
                # their track_war_updates stays True regardless of league movement.
                if not clan_data.get("has_active_subscriptions"):  # type: ignore[union-attr]
                    if not clan_data.get("track_war_updates") and _new_wl in _WAR_UPDATE_LEAGUES:  # type: ignore[union-attr]
                        # Promotion: passively tracked clan entered Master III+.
                        clan_data["track_war_updates"] = True  # type: ignore[index]
                        logging.info(
                            f"[WAR-LEAGUE-UPDATE] {clan_obj.tag}: track_war_updates → True "  # type: ignore[attr-defined]
                            f"(promoted to {_new_wl})"
                        )
                    elif clan_data.get("track_war_updates") and _new_wl not in _WAR_UPDATE_LEAGUES:  # type: ignore[union-attr]
                        # Demotion candidate: previously tracked clan dropped below
                        # Master III. Defer if the clan already has archived data
                        # for its current in-progress season — demoting now would
                        # silence polling for the remaining rounds and permanently
                        # freeze an incomplete season on record (see
                        # CLAN_WAR_TRACKING.md write-path 7's mid-season guard;
                        # this mirrors the same reasoning for this older, separate
                        # demotion path).
                        _in_progress = False
                        if self.cache_manager and self.cache_manager.db_manager:
                            try:
                                _in_progress = await self.cache_manager.db_manager.clan_has_in_progress_cwl_data(
                                    str(clan_obj.tag)  # type: ignore[attr-defined]
                                )
                            except Exception as _ip_ex:
                                logging.warning(
                                    "[WAR-LEAGUE-UPDATE] %s: clan_has_in_progress_cwl_data "
                                    "check failed (%s) — proceeding with demotion",
                                    clan_obj.tag, _ip_ex,  # type: ignore[attr-defined]
                                )
                        if _in_progress:
                            logging.info(
                                "[WAR-LEAGUE-UPDATE] %s: demotion deferred (demoted to %s) — "
                                "already has in-progress-season CWL data; will re-evaluate "
                                "once that season ends",
                                clan_obj.tag, _new_wl,  # type: ignore[attr-defined]
                            )
                        else:
                            # Remove any ongoing temp war file first to prevent orphans,
                            # then pull the clan out of the 22h polling pool.
                            _removed = self._cleanup_temp_war_files(str(clan_obj.tag))  # type: ignore[attr-defined]
                            clan_data["track_war_updates"] = False  # type: ignore[index]
                            logging.info(
                                "[WAR-LEAGUE-UPDATE] %s: track_war_updates → False "
                                "(demoted to %s, no subscriptions%s)",
                                clan_obj.tag,  # type: ignore[attr-defined]
                                _new_wl,
                                f", removed {_removed} temp war file(s)" if _removed else "",
                            )
        
        # Always update the in-memory timestamp (cheap), but only force a DB write
        # if it's been more than 1 hour since the last persisted value
        if clan_data:
            old_timestamp = clan_data.get("last_checked_via_api")  # type: ignore[union-attr]
            clan_data["last_checked_via_api"] = now.isoformat()
            if not dirty and old_timestamp and isinstance(old_timestamp, str):
                try:
                    last_check = datetime.fromisoformat(old_timestamp)
                    # Make timezone-aware if needed for comparison
                    if last_check.tzinfo is None:
                        from datetime import timezone as tz
                        last_check = last_check.replace(tzinfo=tz.utc)
                    hours_since = (now - last_check).total_seconds() / 3600
                    if hours_since >= 1.0:
                        dirty = True
                except (ValueError, TypeError):
                    dirty = True  # Can't parse old timestamp, write to fix it
            elif not old_timestamp:
                dirty = True  # No previous timestamp, must write
        
        # Single persist if any data changed
        if dirty:
            await self.cache_manager.persist_clan(clan_obj.tag)  # type: ignore[union-attr, attr-defined]
        
        # Update war log public status (has its own dirty check inside)
        await self._update_warlog_status(clan_obj)

        # Update player info in user_accounts — restricted to clans actually relevant to some
        # guild (member_clans/member_families or a channel subscription; see
        # update_all_clan_subscription_statuses()'s tracked_tags computation, which is exactly
        # what has_active_subscriptions reflects here).
        #
        # 2026-08-14 incident: this call used to run unconditionally for EVERY clan_obj this
        # process's shared get_clan() cache ever touches — which includes every CWL opponent
        # and family-harvested clan it happens to see (~380K entries in PROD's
        # clan_name_cache), not just a guild's actual member clans. Two consequences, live on
        # PROD within the first cycle after this scope bug shipped: (1) update_player_info_in_
        # user_accounts() does a full O(len(user_accounts)) scan of every registered player
        # TWICE per call, plus a persist_user() DB write for any clan with never-before-seen
        # members — multiplying that cost across thousands of one-off opponent clans instead of
        # a small member-clan set turned every single clan fetch (not just member clans) into a
        # slow operation, observed as "fetching clan data is super slow" and, because
        # periodic_main()'s Phase 1 only checks the shutdown event BETWEEN full cycles (not
        # mid-cycle), made a SIGTERM/kill look unresponsive since a cycle now took far longer to
        # finish; (2) real clan members of clans the guild never configured (e.g. CWL
        # opponents) got auto-added to the UNASSIGNED pool, polluting user_players with accounts
        # nobody asked to track. Gating on has_active_subscriptions fixes both at once.
        if clan_data.get("has_active_subscriptions"):  # type: ignore[union-attr]
            await self.update_player_info_in_user_accounts(clan_obj, self.cache_manager)  # type: ignore[arg-type]

        # Schedule background CoC/clan role sync for all guilds that have this clan
        self._schedule_role_sync_for_clan(clan_obj)

    def _cleanup_temp_war_files(self, clan_tag: str) -> int:
        """
        Remove all temp war files for *clan_tag* and clear in-memory war state.

        Called when a non-subscribed clan is demoted below the Master III
        tracking threshold so any in-progress war file does not become an orphan.

        Returns the number of temp files removed.
        """
        import os
        import glob as _glob
        from qapbot.config import CONFIG

        safe_tag = clan_tag.lstrip("#").upper()
        temp_base = os.path.join(CONFIG.data_dir, "temp")
        try:
            import QBcsvhandling as _qbc
            shard_dir = _qbc.get_war_shard_dir(safe_tag, temp_base)
        except Exception:
            shard_dir = temp_base  # fallback: flat layout

        war_files = _glob.glob(os.path.join(shard_dir, f"{safe_tag}_*_war_data.json"))
        removed = 0
        for fp in war_files:
            try:
                os.remove(fp)
                logging.info("[DEMOTION] %s: removed temp war file %s", clan_tag, os.path.basename(fp))
                removed += 1
            except OSError as ex:
                logging.warning("[DEMOTION] %s: could not remove temp war file %s: %s",
                                clan_tag, os.path.basename(fp), ex)

        if removed and self.cache_manager:
            self.cache_manager.set_temp_war_stats(clan_tag, {})
            logging.info("[DEMOTION] %s: cleared in-memory war state", clan_tag)

        return removed

    def _schedule_role_sync_for_clan(self, clan_obj: 'coc.Clan') -> None:
        """
        Fire-and-forget background role sync for every guild that lists this clan
        as a member clan and has CoC or clan role features enabled.

        Called automatically after every real API fetch so role assignments stay
        current within 12 hours (the new get_clan throttle).
        """
        if not self.cache_manager:
            return

        clan_tag: str = clan_obj.tag  # type: ignore[attr-defined]
        members = list(getattr(clan_obj, 'members', []))

        async def _do_role_sync() -> None:
            try:
                import QBcore as _qbcore
                from qapbot.config import CONFIG
                from qapbot.guild_role_manager import sync_roles_for_clan_members
                for guild_id_str, config in self.cache_manager.server_config.items():  # type: ignore[union-attr]
                    # DEV mode: only sync the dev guild, skip all others
                    if CONFIG.is_dev_mode and int(guild_id_str) != CONFIG.discord_guild_id:
                        logging.debug(f"[COC-CACHE] DEV mode: skipping role sync for guild {guild_id_str}")
                        continue
                    if not config.get("coc_role_enabled") and not config.get("clan_role_enabled"):
                        continue
                    # Clan is covered by this guild if it's an individually configured member
                    # clan OR a member of one of the guild's member families. Family-only guilds
                    # (no individual member_clans) must still trigger role sync for their clans.
                    _covered = clan_tag in config.get("member_clans", [])
                    if not _covered:
                        for _family_id in config.get("member_families", []):
                            family_data = self.cache_manager.clan_families.get(_family_id, {})  # type: ignore[union-attr]
                            if clan_tag in family_data.get("clans", []):
                                _covered = True
                                break
                    if not _covered:
                        continue
                    guild = _qbcore.bot.get_guild(int(guild_id_str))
                    if not guild:
                        continue
                    try:
                        await sync_roles_for_clan_members(guild, guild_id_str, clan_tag, members)  # type: ignore[arg-type]
                    except Exception as _inner_e:
                        logging.warning(
                            f"[COC-CACHE] Role sync failed for guild {guild_id_str}, "
                            f"clan {clan_tag}: {_inner_e}"
                        )
            except Exception as _e:
                logging.warning(f"[COC-CACHE] Role sync trigger failed for {clan_tag}: {_e}")

        import QBcore as _qbcore_spawn
        _qbcore_spawn.spawn_tracked(f"coc-cache-role-sync-{clan_tag}", _do_role_sync())

    def _schedule_background_refresh(self, clan_tag: str) -> None:
        """
        Schedule a fire-and-forget background refresh for a stale cache entry.
        
        Only one refresh per clan_tag is scheduled at a time (dedup via _refreshing set).
        Errors in the background task are logged but do NOT propagate to callers.
        """
        if clan_tag in self._refreshing:
            logging.debug(f"[COC-CACHE-REFRESH] Already refreshing {clan_tag}, skipping")
            return
        
        self._refreshing.add(clan_tag)
        
        async def _do_refresh() -> None:
            try:
                # Bail out silently if maintenance is active — the CoC client
                # and DB were intentionally closed and should not be touched.
                _in_maintenance = False
                try:
                    import QBcore as _qbcore
                    _in_maintenance = _qbcore.maintenance_mode
                except ImportError:
                    pass
                if _in_maintenance:
                    logging.debug(
                        f"[COC-CACHE-REFRESH] Skipping {clan_tag} — maintenance active"
                    )
                    return
                await self._fetch_and_cache(clan_tag)
                logging.debug(f"[COC-CACHE-REFRESH] Background refresh complete for {clan_tag}")
            except Exception as e:
                logging.warning(f"[COC-CACHE-REFRESH] Background refresh failed for {clan_tag}: {e}")
            finally:
                self._refreshing.discard(clan_tag)

        import QBcore as _qbcore_spawn
        _qbcore_spawn.spawn_tracked(f"coc-cache-refresh-{clan_tag}", _do_refresh())
    
    async def _update_warlog_status(self, clan_obj: 'coc.Clan') -> None:
        """
        Guard against private war log being incorrectly marked public via clan endpoint.

        Only updates warlog_is_public to False (private) when the clan endpoint says
        is_war_log_public=False. The True (public) direction is intentionally skipped here
        because clan.is_war_log_public=True is unreliable for some clans — those clans
        return True in the clan endpoint but PrivateWarLog on the war endpoint, causing a
        flip-flop if we update here. The war fetch success handler in QBhelperfunctions is
        the authoritative source for marking a clan public.

        Args:
            clan_obj: coc.Clan object from API
        """
        if not self.cache_manager:
            return
        
        clan_tag: str = clan_obj.tag  # type: ignore[assignment, attr-defined]
        is_public: bool = getattr(clan_obj, 'is_war_log_public', True)  # Default to True if attribute missing
        
        clan_data = self.cache_manager.clan_name_cache.get(clan_tag)  # type: ignore[arg-type]
        if not clan_data:
            return
        
        # Skip if clan data is not in dict format (shouldn't happen)
        if not isinstance(clan_data, dict):  # type: ignore[misc]
            logging.warning(f"[WARLOG-STATUS] Clan {clan_tag} not in dict format - skipping")
            return
        
        # Only update to False here. The True direction is handled exclusively by the
        # war fetch success path (QBhelperfunctions get_current_war_data success block),
        # because clan.is_war_log_public=True from the clan endpoint is unreliable —
        # some clans return True there but PrivateWarLog on the actual war endpoint.
        # Trusting True here causes a flip-flop: WARLOG-STATUS logs "changed to public"
        # then PRIVATE-WARLOG immediately resets it back to False in the same cycle.
        if is_public:
            return

        current_status: bool = clan_data.get('warlog_is_public', True)  # type: ignore[assignment]
        if current_status != is_public:
            clan_data['warlog_is_public'] = is_public
            await self.cache_manager.persist_clan(clan_tag)  # type: ignore[arg-type]
            logging.info(f"[WARLOG-STATUS] {clan_tag} war log changed to private (detected via clan endpoint)")
    
    async def update_player_info_in_user_accounts(self, clan_obj: 'coc.Clan', cache_manager: 'CacheManager') -> None:
        """
        Update TH level and clan info for every player this clan-info API response mentions —
        registered accounts and the UNASSIGNED pool alike (2026-08-14: UNASSIGNED used to be
        skipped here entirely, so an unlinked player's th_level/current_clan_tag/name/coc_role
        froze at whatever it was when last linked/unlinked and was never refreshed again by the
        regular clan-poll cycle — only a one-off get_player() call at re-link time kept it
        current at all, per the comment this fixes in QBdiscocmdshelper.py's UNASSIGNED-restore
        path). Also creates a fresh UNASSIGNED-pool entry for any clan member this bot has never
        tracked in ANY account before, so a player who's never been linked by anyone still gets
        a user_players row with real TH/name data instead of remaining invisible until someone
        links them — "never waste info the CoC API already gave us."

        This keeps user_players current with player TH levels and clan membership without
        requiring individual API calls per player.

        Args:
            clan_obj: CoC Clan object with member data
            cache_manager: CacheManager instance with user_accounts data
        """
        # Build lookup: player_tag -> member data
        clan_members = {member.tag: member for member in clan_obj.members}  # type: ignore[misc, attr-defined]

        # Track changes
        changes_made = False
        affected_user_ids: List[str] = []
        name_changes: List[tuple[str, str]] = []  # (player_tag, new_name) for index update
        tracked_tags: Set[str] = set()

        # Update all registered players who are in this clan (including the UNASSIGNED pool —
        # see the docstring above for why that's no longer skipped)
        for user_id, user_data in cache_manager.user_accounts.items():
            # Skip invalid entries (shouldn't happen after validation)
            if not isinstance(user_data, dict):  # type: ignore[misc]
                continue

            players = user_data.get("players", [])
            for player in players:
                if not isinstance(player, dict):
                    continue

                player_tag = player.get("player_tag")  # type: ignore[union-attr]
                if player_tag:
                    tracked_tags.add(player_tag)
                if player_tag in clan_members:
                    member = clan_members[player_tag]
                    
                    # Update TH level
                    old_th: Optional[int] = player.get("th_level")  # type: ignore[assignment]
                    new_th: int = member.town_hall  # type: ignore[attr-defined]
                    if old_th != new_th:
                        player["th_level"] = new_th
                        changes_made = True
                        if user_id not in affected_user_ids:
                            affected_user_ids.append(user_id)
                        logging.debug(f"[USER-ACCOUNTS-UPDATE] {player_tag}: TH {old_th} -> {new_th}")
                    
                    # Update current clan tag (name looked up from clan_name_cache)
                    old_clan_tag: Optional[str] = player.get("current_clan_tag")  # type: ignore[assignment]
                    new_clan_tag: str = clan_obj.tag  # type: ignore[assignment, attr-defined]
                    
                    if old_clan_tag != new_clan_tag:
                        player["current_clan_tag"] = new_clan_tag
                        changes_made = True
                        if user_id not in affected_user_ids:
                            affected_user_ids.append(user_id)
                        logging.debug(f"[USER-ACCOUNTS-UPDATE] {player_tag}: Clan updated to {new_clan_tag}")

                    # Update player name (CoC API is authoritative for current name)
                    old_name: Optional[str] = player.get("player_name")  # type: ignore[assignment]
                    new_name: str = member.name  # type: ignore[attr-defined]
                    if old_name != new_name:
                        player["player_name"] = new_name
                        changes_made = True
                        if user_id not in affected_user_ids:
                            affected_user_ids.append(user_id)
                        if not player_tag:
                            continue
                        name_changes.append((str(player_tag), new_name))
                        logging.info(
                            f"[PLAYER-NAME-UPDATE] {player_tag}: '{old_name}' -> '{new_name}'"
                        )

                    # Update CoC in-game role (member/elder/coLeader/leader)
                    old_coc_role: Optional[str] = player.get("coc_role")  # type: ignore[assignment]
                    raw_member_role = getattr(member, "role", None)  # type: ignore[attr-defined]
                    # Use role.name instead of str() or .value:
                    # str(Role.leader) == "Leader" (title-case) — won't match our keys
                    # Role.elder.value == "admin" — won't match "elder"
                    # Role.name gives "member", "elder", "co_leader", "leader"
                    # Map co_leader → coLeader to match COC_ROLE_PRIORITY
                    _raw_name: Optional[str] = getattr(raw_member_role, "name", None) if raw_member_role else None
                    new_coc_role: Optional[str] = ("coLeader" if _raw_name == "co_leader" else _raw_name) if _raw_name else None
                    if old_coc_role != new_coc_role:
                        player["coc_role"] = new_coc_role
                        changes_made = True
                        if user_id not in affected_user_ids:
                            affected_user_ids.append(user_id)
                        logging.debug(f"[USER-ACCOUNTS-UPDATE] {player_tag}: CoC role {old_coc_role} -> {new_coc_role}")
        
        # Detect departures: players whose current_clan_tag == this clan but are no longer in
        # the current member list. Clear current_clan_tag so role sync stops assigning the old
        # clan role and a fresh get_player() can set the correct new clan on next cycle.
        clan_tag_str: str = str(clan_obj.tag)  # type: ignore[attr-defined]
        for user_id, user_data in cache_manager.user_accounts.items():
            if not isinstance(user_data, dict):  # type: ignore[misc]
                continue
            for player in user_data.get("players", []):
                if not isinstance(player, dict):
                    continue
                p_tag = player.get("player_tag")  # type: ignore[union-attr]
                if (player.get("current_clan_tag") == clan_tag_str  # type: ignore[union-attr]
                        and p_tag not in clan_members):
                    player["current_clan_tag"] = None
                    logging.info(
                        f"[USER-ACCOUNTS-UPDATE] {p_tag}: departed from {clan_tag_str} "
                        f"(not in current member list) — clearing current_clan_tag"
                    )
                    changes_made = True
                    if user_id not in affected_user_ids:
                        affected_user_ids.append(user_id)

        # Any clan member this bot has never tracked in any account (registered or UNASSIGNED)
        # gets a brand-new UNASSIGNED-pool entry — the only way a never-linked player ends up
        # with a user_players row at all, so features like get_current_clan_members_sync() (the
        # "Manage Enrollment" board's player pool) can find them from day one.
        new_tags = [tag for tag in clan_members if tag not in tracked_tags]
        if new_tags:
            unassigned_entry = cache_manager.user_accounts.setdefault(
                "UNASSIGNED", {"display_name": "UNASSIGNED", "players": []}
            )
            unassigned_players = unassigned_entry.setdefault("players", [])  # type: ignore[union-attr]
            for tag in new_tags:
                member = clan_members[tag]
                raw_role = getattr(member, "role", None)
                raw_role_name = getattr(raw_role, "name", None) if raw_role else None
                new_coc_role = ("coLeader" if raw_role_name == "co_leader" else raw_role_name) if raw_role_name else None
                unassigned_players.append({
                    "player_tag": tag,
                    "player_name": member.name,  # type: ignore[attr-defined]
                    "verified": False,
                    "th_level": member.town_hall,  # type: ignore[attr-defined]
                    "current_clan_tag": clan_obj.tag,  # type: ignore[attr-defined]
                    "coc_role": new_coc_role,
                })
                logging.info(f"[USER-ACCOUNTS-UPDATE] {tag}: newly tracked from clan {clan_tag_str} (never seen before)")
            changes_made = True
            if "UNASSIGNED" not in affected_user_ids:
                affected_user_ids.append("UNASSIGNED")

        # Save affected users (write-through: persist only changed users)
        if changes_made:
            for uid in affected_user_ids:
                await cache_manager.persist_user(uid)
            logging.debug(f"[USER-ACCOUNTS-SAVE] Saved player info updates from {clan_obj.tag}")  # type: ignore[attr-defined]

        # Propagate name changes to player_name_index (in-memory + DB)
        if name_changes and cache_manager.db_manager:
            now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            updates = [(tag, name, now_iso) for tag, name in name_changes]
            for tag, name in name_changes:
                cache_manager.set_player_name(tag, name)
            await asyncio.to_thread(
                cache_manager.db_manager.update_player_name_index_sync, updates
            )
            logging.info(
                f"[PLAYER-NAME-INDEX] Updated {len(name_changes)} player name(s) "
                f"from API (clan {clan_tag_str})"
            )
    
    def invalidate(self, clan_tag: str) -> None:
        """
        Evict a clan's cache entry so the next get_clan() call performs a blocking API fetch.

        Use this before a user-triggered refresh to guarantee fresh data is returned
        regardless of the soft/hard TTL (stale-while-revalidate is bypassed).

        Args:
            clan_tag: Normalized clan tag (e.g., "#2C9UR9GJY")
        """
        if clan_tag in self.cache:
            del self.cache[clan_tag]
            logging.debug(f"[COC-CACHE-INVALIDATE] {clan_tag} evicted — next get_clan() will hit API")
    
    def clear_expired(self) -> int:
        """
        Remove all expired entries from cache.
        
        Returns:
            Number of entries removed
        """
        now = datetime.now(timezone.utc)
        expired_tags: List[str] = []
        
        for clan_tag, cached in self.cache.items():
            age_seconds = (now - cached["timestamp"]).total_seconds()
            if age_seconds >= self.hard_ttl_seconds:
                expired_tags.append(clan_tag)
        
        for clan_tag in expired_tags:
            del self.cache[clan_tag]
        
        if expired_tags:
            logging.info(f"[COC-CACHE-CLEANUP] Removed {len(expired_tags)} expired entries")
        
        return len(expired_tags)
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Get cache statistics.
        
        Returns:
            Dict with cache size and age information
        """
        if not self.cache:
            return {"size": 0, "oldest_age_seconds": 0, "newest_age_seconds": 0}
        
        now = datetime.now(timezone.utc)
        ages = [(now - cached["timestamp"]).total_seconds() for cached in self.cache.values()]
        
        return {
            "size": len(self.cache),
            "oldest_age_seconds": max(ages),
            "newest_age_seconds": min(ages),
            "ttl_seconds": self.hard_ttl_seconds
        }
    
    def get_memory_usage_mb(self) -> float:
        """
        Calculate approximate memory usage of the clan cache in MB.
        
        Estimates memory by calculating the size of all cached clan objects
        and their associated metadata.
        
        Returns:
            Memory usage in MB (float)
        """
        if not self.cache:
            return 0.0
        
        total_bytes = 0
        
        for cached in self.cache.values():
            clan_obj = cached["data"]
            
            # Estimate clan object size
            # Basic fields: tag, name, description, location, badge URLs
            base_size = sys.getsizeof(clan_obj)
            
            # Add member list size
            members_size = sys.getsizeof(clan_obj.members)
            for member in clan_obj.members:
                members_size += sys.getsizeof(member)
            
            # Add metadata
            metadata_size = sys.getsizeof(cached["timestamp"]) + sys.getsizeof(cached)
            
            total_bytes += base_size + members_size + metadata_size
        
        return total_bytes / (1024 * 1024)
