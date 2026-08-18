"""
Guild Role Manager for QapBot.

Handles Discord role management for:
1. CoC In-Game Role (Member / Elder / Co-Leader / Leader)
2. Clan Role (one Discord role per member clan in the guild)

Architecture:
    - All role creation/deletion goes through this module
    - Role IDs are stored in CACHE.server_config (guild_id -> config dict)
    - guild_clan_roles: {clan_tag -> discord_role_id} stored per guild
    - Discord API calls are wrapped with proper error handling

Feature Flags (in CACHE.server_config[guild_id]):
    - "coc_role_enabled"  : bool - sync CoC in-game roles (Member/Elder/Co-Leader/Leader)
    - "clan_role_enabled" : bool - sync one role per clan

CoC Role IDs stored in CACHE.server_config[guild_id]:
    - "coc_role_member_id"   : str|None
    - "coc_role_elder_id"    : str|None
    - "coc_role_coleader_id" : str|None
    - "coc_role_leader_id"   : str|None

Clan Role IDs:
    - CACHE.server_config[guild_id]["clan_roles"] : dict {clan_tag: role_id_str}
      Also persisted to guild_clan_roles DB table.

CoC member.role values (from CoC Python library):
    "member", "elder", "coLeader", "leader"
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

import discord

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# CoC in-game role key used in player dicts (set by coc_cache update_player_info*)
COC_ROLE_FIELD = "coc_role"

# Mapping from CoC library role strings → config key used in server_config
COC_ROLE_CONFIG_KEY: Dict[str, str] = {
    "member":    "coc_role_member_id",
    "elder":     "coc_role_elder_id",
    "coLeader":  "coc_role_coleader_id",
    "leader":    "coc_role_leader_id",
}

# Priority order (higher index = higher rank, used for "highest role wins" logic)
COC_ROLE_PRIORITY: Dict[str, int] = {
    "member":   1,
    "elder":    2,
    "coLeader": 3,
    "leader":   4,
}

# Human-readable default Discord role names for CoC in-game roles
# Intentionally prefixed to group them and avoid collisions
COC_ROLE_DISPLAY_NAMES: Dict[str, str] = {
    "member":   "CoC Member",
    "elder":    "CoC Elder",
    "coLeader": "CoC Co-Leader",
    "leader":   "CoC Leader",
}

# All four CoC role config keys in priority order
COC_ROLE_CONFIG_KEYS_ORDERED: List[str] = [
    "coc_role_member_id",
    "coc_role_elder_id",
    "coc_role_coleader_id",
    "coc_role_leader_id",
]

# Module-level set: tracks which clan tags have had their CoC member data refreshed
# since the last bot startup.  coc_role is memory-only (never persisted); after a
# restart all values are None until coc_clan_cache.get_clan() is called.  We force
# one refresh per clan per process lifetime so role sync works on the first cycle.
_coc_role_refreshed_clans: set[str] = set()

# Module-level set tracking (guild_id, user_id) pairs currently being synced.
# Prevents duplicate work when sync_all_roles_for_guild and sync_roles_for_clan_members
# run concurrently and both try to sync the same user at the same moment.
_users_being_synced: set[tuple[str, int]] = set()


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def normalize_discord_role_name(name: str) -> str:
    """
    Normalize a string to a valid Discord role name.

    Discord role name rules:
    - Max 100 characters
    - Leading/trailing whitespace stripped
    - Only alphanumeric, spaces, hyphens, underscores, and common Unicode letters
      are kept.  Everything else (special chars, control chars) is removed.

    Args:
        name: Raw name string (e.g. clan name from CoC API)

    Returns:
        Normalized role name, max 100 chars.

    Examples:
        >>> normalize_discord_role_name("The Q-Crew!")
        "The Q-Crew"
        >>> normalize_discord_role_name("  Hello (World)  ")
        "Hello World"
    """
    # Keep Unicode letters, digits, spaces, hyphens, underscores, apostrophes
    cleaned = re.sub(r"[^\w\s'\-]", "", name, flags=re.UNICODE)
    # Collapse multiple whitespace characters into a single space
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # Truncate to Discord's 100-char limit
    return cleaned[:100]


def _get_config(guild_id: str) -> Dict[str, Any]:
    """Return the server config dict for a guild (never None)."""
    from qapbot.cache_manager import CACHE
    return CACHE.server_config.get(guild_id, {})


# ---------------------------------------------------------------------------
# Discord role creation / lookup helpers
# ---------------------------------------------------------------------------

async def get_or_create_discord_role(
    guild: discord.Guild,
    role_name: str,
    reason: str = "QapBot role management",
    color: Optional[discord.Color] = None,
) -> Optional[discord.Role]:
    """
    Return an existing Discord role by name or create it if missing.

    Matching is case-insensitive on the normalized name.
    If the guild already has a role whose normalized name equals the
    normalized ``role_name``, that role is returned without creating a new one.

    Args:
        guild: Discord guild to search / create in.
        role_name: Desired role name (will be normalized before use).
        reason: Audit log reason for creation.
        color: Optional color for newly created roles.

    Returns:
        discord.Role if found or created, None on permission/API error.
    """
    normalized = normalize_discord_role_name(role_name)
    if not normalized:
        logging.warning(f"[ROLE-MGR] normalize_discord_role_name returned empty for '{role_name}'")
        return None

    # Search existing roles (case-insensitive)
    for role in guild.roles:
        if normalize_discord_role_name(role.name).lower() == normalized.lower():
            logging.debug(f"[ROLE-MGR] Found existing role '{role.name}' ({role.id}) in guild {guild.id}")
            return role

    # Create new role
    try:
        kwargs: Dict[str, Any] = {"name": normalized, "reason": reason}
        if color is not None:
            kwargs["color"] = color
        new_role = await guild.create_role(**kwargs)
        logging.info(f"[ROLE-MGR] Created new Discord role '{normalized}' ({new_role.id}) in guild {guild.id}")
        return new_role
    except discord.Forbidden:
        logging.error(f"[ROLE-MGR] Missing 'Manage Roles' permission in guild {guild.id}")
        return None
    except discord.HTTPException as e:
        logging.error(f"[ROLE-MGR] HTTPException creating role '{normalized}' in guild {guild.id}: {e}")
        return None


async def delete_discord_role_safe(guild: discord.Guild, role_id: int, reason: str = "QapBot role management") -> bool:
    """
    Delete a Discord role by ID, handling NotFound and Forbidden gracefully.

    Args:
        guild: Discord guild.
        role_id: Discord role ID to delete.
        reason: Audit log reason.

    Returns:
        True if deleted (or already gone), False on permission error.
    """
    role = guild.get_role(role_id)
    if role is None:
        logging.debug(f"[ROLE-MGR] Role {role_id} not found in guild {guild.id} (already deleted?)")
        return True
    try:
        await role.delete(reason=reason)
        logging.info(f"[ROLE-MGR] Deleted role '{role.name}' ({role_id}) from guild {guild.id}")
        return True
    except discord.Forbidden:
        logging.error(f"[ROLE-MGR] Missing permission to delete role {role_id} in guild {guild.id}")
        return False
    except discord.NotFound:
        logging.debug(f"[ROLE-MGR] Role {role_id} already deleted from guild {guild.id}")
        return True
    except discord.HTTPException as e:
        logging.error(f"[ROLE-MGR] HTTPException deleting role {role_id}: {e}")
        return False


# ---------------------------------------------------------------------------
# CoC in-game role management
# ---------------------------------------------------------------------------

async def create_coc_ingame_roles(
    guild: discord.Guild,
    guild_id: str,
) -> Dict[str, str]:
    """
    Ensure all four CoC in-game roles exist in the guild.

    Creates roles that are missing; returns existing ones if present.
    Saves the resulting role IDs to CACHE.server_config and persists.

    Args:
        guild: Discord guild object.
        guild_id: Guild ID string (for CACHE access).

    Returns:
        Dict mapping CoC role key (e.g. "member") to Discord role_id string.
        Keys that failed to create map to empty string "".
    """
    from qapbot.cache_manager import CACHE

    config = CACHE.server_config.setdefault(guild_id, {})
    result: Dict[str, str] = {}

    for coc_role_key, config_key in COC_ROLE_CONFIG_KEY.items():
        display_name = COC_ROLE_DISPLAY_NAMES[coc_role_key]

        # If we already have a valid stored role ID, verify it still exists
        existing_id = config.get(config_key)
        if existing_id:
            role = guild.get_role(int(existing_id))
            if role is not None:
                result[coc_role_key] = existing_id
                logging.debug(f"[ROLE-MGR] Reusing existing CoC role '{role.name}' for {coc_role_key}")
                continue
            # Role was deleted externally – fall through to recreate
            logging.info(f"[ROLE-MGR] Stored CoC role {existing_id} no longer exists, recreating")

        role = await get_or_create_discord_role(
            guild,
            display_name,
            reason=f"QapBot CoC in-game roles (coc_role_{coc_role_key})",
            color=discord.Color.blue(),
        )
        if role:
            config[config_key] = str(role.id)
            result[coc_role_key] = str(role.id)
        else:
            result[coc_role_key] = ""

    await CACHE.persist_server_config(guild_id)
    return result


def get_coc_ingame_role_ids(guild_id: str) -> Dict[str, Optional[str]]:
    """
    Return the stored Discord role ID strings for all four CoC in-game roles.

    Args:
        guild_id: Guild ID string.

    Returns:
        Dict e.g. {"member": "123456", "elder": "789012", ...} or None values.
    """
    config = _get_config(guild_id)
    return {
        coc_role_key: config.get(config_key)
        for coc_role_key, config_key in COC_ROLE_CONFIG_KEY.items()
    }


def list_coc_ingame_roles(guild: discord.Guild, guild_id: str) -> List[discord.Role]:
    """
    Return all configured CoC in-game Discord role objects that still exist.

    Args:
        guild: Discord guild.
        guild_id: Guild ID string.

    Returns:
        List of discord.Role objects (only those that exist in the guild).
    """
    ids = get_coc_ingame_role_ids(guild_id)
    roles: List[discord.Role] = []
    for role_id in ids.values():
        if role_id:
            r = guild.get_role(int(role_id))
            if r:
                roles.append(r)
    return roles


# ---------------------------------------------------------------------------
# Clan role management
# ---------------------------------------------------------------------------

async def create_clan_role(
    guild: discord.Guild,
    guild_id: str,
    clan_tag: str,
    clan_name: str,
) -> Optional[str]:
    """
    Ensure a per-clan Discord role exists for the given clan.

    Saves the role ID to CACHE.server_config[guild_id]["clan_roles"][clan_tag]
    and persists to DB.

    Args:
        guild: Discord guild object.
        guild_id: Guild ID string.
        clan_tag: Clan tag (e.g. "#L2J0C0PY").
        clan_name: Human-readable clan name.

    Returns:
        Discord role ID string, or None on failure.
    """
    from qapbot.cache_manager import CACHE

    config = CACHE.server_config.setdefault(guild_id, {})
    clan_roles: Dict[str, str] = config.setdefault("clan_roles", {})

    # Check if we already have a valid role stored
    existing_id = clan_roles.get(clan_tag)
    if existing_id:
        role = guild.get_role(int(existing_id))
        if role is not None:
            logging.debug(f"[ROLE-MGR] Reusing existing clan role '{role.name}' for {clan_tag}")
            return existing_id
        logging.info(f"[ROLE-MGR] Stored clan role {existing_id} for {clan_tag} no longer exists, recreating")

    role_name = normalize_discord_role_name(clan_name)
    if not role_name:
        logging.warning(f"[ROLE-MGR] Empty clan role name for {clan_tag} ('{clan_name}')")
        return None

    role = await get_or_create_discord_role(
        guild,
        role_name,
        reason=f"QapBot per-clan role for {clan_tag}",
        color=discord.Color.green(),
    )
    if role is None:
        return None

    clan_roles[clan_tag] = str(role.id)
    await CACHE.persist_server_config(guild_id)
    # Also persist to guild_clan_roles DB table
    try:
        await CACHE.db_manager.save_guild_clan_role(guild_id, clan_tag, str(role.id))  # type: ignore[union-attr]
    except Exception as e:
        logging.error(f"[ROLE-MGR] Failed to persist clan role to DB: {e}")

    logging.info(f"[ROLE-MGR] Clan role '{role.name}' ({role.id}) ready for {clan_tag} in guild {guild_id}")
    return str(role.id)


def list_clan_roles(guild: discord.Guild, guild_id: str) -> List[Tuple[str, discord.Role]]:
    """
    Return all configured per-clan Discord role objects that still exist.

    Args:
        guild: Discord guild.
        guild_id: Guild ID string.

    Returns:
        List of (clan_tag, discord.Role) tuples.
    """
    config = _get_config(guild_id)
    clan_roles: Dict[str, str] = config.get("clan_roles", {})
    result: List[Tuple[str, discord.Role]] = []
    for clan_tag, role_id in clan_roles.items():
        if role_id:
            r = guild.get_role(int(role_id))
            if r:
                result.append((clan_tag, r))
    return result


async def delete_clan_role_from_guild(
    guild: discord.Guild,
    guild_id: str,
    clan_tag: str,
    reason: str = "QapBot: clan removed from guild",
) -> bool:
    """
    Delete the per-clan Discord role for a specific clan and clean up config.

    Args:
        guild: Discord guild.
        guild_id: Guild ID string.
        clan_tag: Clan tag whose role should be deleted.
        reason: Audit log reason.

    Returns:
        True if deleted (or no role was stored), False on error.
    """
    from qapbot.cache_manager import CACHE

    config = _get_config(guild_id)
    clan_roles: Dict[str, str] = config.get("clan_roles", {})
    role_id_str = clan_roles.get(clan_tag)
    if not role_id_str:
        return True  # Nothing to delete

    success = await delete_discord_role_safe(guild, int(role_id_str), reason=reason)
    if success:
        clan_roles.pop(clan_tag, None)
        await CACHE.persist_server_config(guild_id)
        try:
            await CACHE.db_manager.delete_guild_clan_role(guild_id, clan_tag)  # type: ignore[union-attr]
        except Exception as e:
            logging.error(f"[ROLE-MGR] Failed to remove clan role from DB: {e}")
    return success


# ---------------------------------------------------------------------------
# Player role sync helpers
# ---------------------------------------------------------------------------

def _get_clan_tags_for_user(user_id: str, strict_mode: bool = False) -> List[str]:
    """
    Return all CoC clan tags associated with a Discord user's registered accounts.

    Args:
        user_id: Discord user ID string.
        strict_mode: When True, only players with verified=True are included.
            In SIMPLE mode (False), any linked player qualifies.

    Returns:
        List of clan tag strings (without duplicates).
    """
    from qapbot.cache_manager import CACHE

    user_data = CACHE.user_accounts.get(user_id)
    if not user_data:
        return []
    tags: List[str] = []
    for player in user_data.get("players", []):
        if not isinstance(player, dict):
            continue
        if strict_mode and not player.get("verified", False):  # type: ignore[union-attr]
            continue
        clan_tag = player.get("current_clan_tag")  # type: ignore[union-attr]
        if clan_tag and clan_tag not in tags:
            tags.append(clan_tag)  # type: ignore[arg-type]
    return tags


def _get_highest_coc_role_for_user(user_id: str, guild_clans: Optional[Set[str]] = None, strict_mode: bool = False) -> Optional[str]:
    """
    Return the highest-priority CoC in-game role across all of a user's accounts,
    restricted to players who are actually in a guild-relevant clan.

    Priority order: leader > coLeader > elder > member

    Args:
        user_id: Discord user ID string.
        guild_clans: The full set of clan tags that belong to this guild
            (member_clans + all family clans). When provided, only players
            whose current_clan_tag is in this set are considered. This
            prevents users with accounts in unrelated clans from receiving
            CoC roles. If None, all players are considered (legacy fallback).
        strict_mode: When True, only players with verified=True are included.
            In SIMPLE mode (False), any linked player qualifies.

    Returns:
        CoC role string (e.g. "coLeader") or None if the user has no
        account in a guild-relevant clan (or no role data available).
    """
    from qapbot.cache_manager import CACHE

    user_data = CACHE.user_accounts.get(user_id)
    if not user_data:
        return None

    best_priority = 0
    best_role: Optional[str] = None
    for player in user_data.get("players", []):
        if not isinstance(player, dict):
            continue
        # Only consider players whose current clan is tracked by this guild
        if guild_clans is not None:
            current_clan = player.get("current_clan_tag")  # type: ignore[union-attr]
            if not current_clan or current_clan not in guild_clans:
                continue
        # In STRICT mode, skip unverified players
        if strict_mode and not player.get("verified", False):  # type: ignore[union-attr]
            continue
        coc_role = player.get(COC_ROLE_FIELD)  # type: ignore[union-attr]
        if coc_role and coc_role in COC_ROLE_PRIORITY:
            p = COC_ROLE_PRIORITY[coc_role]
            if p > best_priority:
                best_priority = p
                best_role = str(coc_role)  # type: ignore[arg-type]
    return best_role


async def assign_role_to_member(member: discord.Member, role: discord.Role, reason: str = "QapBot role sync") -> bool:
    """
    Add a Discord role to a guild member if not already present.

    Args:
        member: Discord guild member.
        role: Role to assign.
        reason: Audit log reason.

    Returns:
        True if the role was added (or already present), False on error.
    """
    if role in member.roles:
        return True
    try:
        await member.add_roles(role, reason=reason)
        logging.info(f"[ROLE-SYNC] Assigned role '{role.name}' to {member.display_name} ({member.id})")
        return True
    except discord.Forbidden:
        logging.error(f"[ROLE-SYNC] Missing permission to assign role '{role.name}' in guild {member.guild.name} ({member.guild.id})")
        return False
    except discord.HTTPException as e:
        logging.error(f"[ROLE-SYNC] HTTPException assigning role '{role.name}' to {member.id}: {e}")
        return False


async def remove_role_from_member(member: discord.Member, role: discord.Role, reason: str = "QapBot role sync") -> bool:
    """
    Remove a Discord role from a guild member if present.

    Args:
        member: Discord guild member.
        role: Role to remove.
        reason: Audit log reason.

    Returns:
        True if removed (or already absent), False on error.
    """
    if role not in member.roles:
        return True
    try:
        await member.remove_roles(role, reason=reason)
        logging.info(f"[ROLE-SYNC] Removed role '{role.name}' from {member.display_name} ({member.id})")
        return True
    except discord.Forbidden:
        logging.error(f"[ROLE-SYNC] Missing permission to remove role '{role.name}' in guild {member.guild.name} ({member.guild.id})")
        return False
    except discord.HTTPException as e:
        logging.error(f"[ROLE-SYNC] HTTPException removing role '{role.name}' from {member.id}: {e}")
        return False


# ---------------------------------------------------------------------------
# Core sync: one user, one guild
# ---------------------------------------------------------------------------

async def sync_roles_for_user(
    guild: discord.Guild,
    guild_id: str,
    discord_user_id: int,
    member: Optional[discord.Member] = None,
) -> bool:
    """
    Sync CoC in-game role and clan role(s) for a single Discord user.

    Logic:
    - CoC in-game role: assign the role matching the user's highest CoC rank;
      remove the other three CoC role types.
    - Clan roles: assign roles for every clan the user has an account in;
      remove clan roles for clans the user is no longer in.

    Only acts when the respective feature is enabled in guild config.

    Args:
        guild: Discord guild.
        guild_id: Guild ID string.
        discord_user_id: Discord user ID (int).
        member: Pre-fetched guild member, if available. When provided the
            Discord API is not called (fast path used by guild-wide sync).

    Returns:
        True if role sync actually ran for this user (they were confirmed a guild member
        and the feature is enabled). False if skipped — not a member (confirmed via
        discord.NotFound), a concurrent sync for the same user was already in flight, the
        role feature is disabled, or membership couldn't be determined (Forbidden / a
        persistent HTTPException, already logged internally in that case). All existing
        callers ignore the return value, so this is purely additive.
    """
    _sync_key = (guild_id, discord_user_id)
    if _sync_key in _users_being_synced:
        logging.debug(f"[ROLE-SYNC] Skipping duplicate concurrent sync for user {discord_user_id} in guild {guild_id}")
        return False
    _users_being_synced.add(_sync_key)
    try:
        return await _sync_roles_for_user_impl(guild, guild_id, discord_user_id, member)
    finally:
        _users_being_synced.discard(_sync_key)


async def _sync_roles_for_user_impl(
    guild: discord.Guild,
    guild_id: str,
    discord_user_id: int,
    member: Optional[discord.Member] = None,
) -> bool:
    """Internal implementation of sync_roles_for_user (called after in-flight guard check).

    Returns True iff the user was confirmed a guild member and sync actually ran — see
    sync_roles_for_user()'s docstring for the full contract.
    """
    config = _get_config(guild_id)
    coc_role_enabled: bool = config.get("coc_role_enabled", False)
    clan_role_enabled: bool = config.get("clan_role_enabled", False)
    role_system_enabled: bool = config.get("role_system_enabled", False)
    member_role_strict: bool = config.get("member_role_strict", False)

    if not coc_role_enabled and not clan_role_enabled and not role_system_enabled:
        return False

    # Fetch guild member (may not be in guild)
    if member is None:
        # Fast path: use local cache first (no API call)
        member = guild.get_member(discord_user_id)

    if member is None:
        # Local cache miss — call the API with retry for transient 5xx errors
        _retries = 3
        _delay = 2.0
        for _attempt in range(_retries):
            try:
                member = await guild.fetch_member(discord_user_id)
                break
            except discord.NotFound:
                logging.debug(f"[ROLE-SYNC] User {discord_user_id} not found in guild {guild.name} ({guild_id}), skipping")
                return False
            except discord.Forbidden:
                logging.debug(f"[ROLE-SYNC] No permission to fetch member {discord_user_id} in guild {guild.name} ({guild_id}), skipping")
                return False
            except discord.HTTPException as e:
                _transient = e.status >= 500 or e.status == 429
                if _transient and _attempt < _retries - 1:
                    logging.debug(
                        f"[ROLE-SYNC] Transient error fetching member {discord_user_id} "
                        f"(attempt {_attempt + 1}/{_retries}, status {e.status}): {e} — retrying in {_delay}s"
                    )
                    await asyncio.sleep(_delay)
                    _delay *= 2
                else:
                    logging.warning(
                        f"[ROLE-SYNC] Cannot fetch member {discord_user_id} in guild {guild.name} ({guild_id}) "
                        f"after {_attempt + 1} attempt(s): {e}"
                    )
                    return False
        if member is None:
            return False

    user_id_str = str(discord_user_id)

    # Compute the full set of clan tags that are relevant to this guild once,
    # used by both CoC role and clan role logic below.
    from qapbot.cache_manager import CACHE
    guild_member_clans: Set[str] = set(config.get("member_clans", []))
    for family_id in config.get("member_families", []):
        family_data = CACHE.clan_families.get(family_id, {})
        guild_member_clans.update(family_data.get("clans", []))

    # --- CoC in-game role sync ---
    if coc_role_enabled:
        # Only award a CoC role if the user's player account is actually in
        # one of this guild's tracked clans (member_clans / family clans).
        # Without this filter, a user who is a Leader in an unrelated clan
        # would receive the Leader role here too.
        highest_coc_role = _get_highest_coc_role_for_user(user_id_str, guild_clans=guild_member_clans, strict_mode=member_role_strict)
        coc_role_ids = get_coc_ingame_role_ids(guild_id)

        for coc_role_key, role_id_str in coc_role_ids.items():
            if not role_id_str:
                continue
            role = guild.get_role(int(role_id_str))
            if not role:
                continue
            if coc_role_key == highest_coc_role:
                await assign_role_to_member(member, role, reason="QapBot CoC role sync")
            else:
                await remove_role_from_member(member, role, reason="QapBot CoC role sync")

    # --- Clan role sync ---
    if clan_role_enabled:
        user_clan_tags = set(_get_clan_tags_for_user(user_id_str, strict_mode=member_role_strict))
        clan_roles: Dict[str, str] = config.get("clan_roles", {})

        for clan_tag, role_id_str in clan_roles.items():
            if not role_id_str:
                continue
            role = guild.get_role(int(role_id_str))
            if not role:
                continue
            if clan_tag in user_clan_tags:
                await assign_role_to_member(member, role, reason="QapBot clan role sync")
            else:
                await remove_role_from_member(member, role, reason="QapBot clan role sync")

    # --- Member role sync (assign-only, never revoked by the bot) ---
    # Only assigns the role when the user doesn't have it yet.
    # When the member role is assigned, the newbie role is removed (mutually exclusive).
    # If the user already has the member role, the newbie role is still removed if present.
    # SIMPLE mode: any linked player in a member clan qualifies.
    # STRICT mode: player must be API-verified in a member clan.
    if role_system_enabled:
        _member_role_id_str: Optional[str] = config.get("member_role_id")
        _newbie_role_id_str: Optional[str] = config.get("newbie_role_id")
        _newbie_disc_role: Optional[discord.Role] = None
        if _newbie_role_id_str:
            try:
                _newbie_disc_role = guild.get_role(int(_newbie_role_id_str))
            except (ValueError, TypeError):
                pass
        if _member_role_id_str:
            try:
                _member_disc_role = guild.get_role(int(_member_role_id_str))
            except (ValueError, TypeError):
                _member_disc_role = None
            if _member_disc_role:
                _has_member_role = _member_disc_role in member.roles
                if not _has_member_role:
                    _user_data = CACHE.user_accounts.get(user_id_str, {})
                    _eligible = False
                    for _player in _user_data.get("players", []):
                        if not isinstance(_player, dict):
                            continue
                        _clan_tag = _player.get("current_clan_tag")  # type: ignore[union-attr]
                        if _clan_tag and _clan_tag in guild_member_clans:
                            if not member_role_strict or _player.get("verified", False):  # type: ignore[union-attr]
                                _eligible = True
                                break
                    if _eligible:
                        await assign_role_to_member(member, _member_disc_role, reason="QapBot member role sync")
                        _has_member_role = True
                # Remove newbie role whenever member role is present
                if _has_member_role and _newbie_disc_role and _newbie_disc_role in member.roles:
                    await remove_role_from_member(member, _newbie_disc_role, reason="QapBot member role sync (newbie role removed)")

    return True


# ---------------------------------------------------------------------------
# Sync all users in a guild
# ---------------------------------------------------------------------------

async def sync_all_roles_for_guild(
    guild: discord.Guild,
    guild_id: str,
) -> None:
    """
    Sync CoC in-game and clan roles for all registered Discord users in a guild.

    Called after each periodic CoC API fetch cycle or when features are enabled.

    Args:
        guild: Discord guild.
        guild_id: Guild ID string.
    """
    config = _get_config(guild_id)
    coc_role_enabled: bool = config.get("coc_role_enabled", False)
    clan_role_enabled: bool = config.get("clan_role_enabled", False)
    role_system_enabled: bool = config.get("role_system_enabled", False)

    if not coc_role_enabled and not clan_role_enabled and not role_system_enabled:
        return

    from qapbot.cache_manager import CACHE

    # ------------------------------------------------------------------
    # Bootstrap: coc_role is memory-only and cleared on each restart.
    # fetch_clan_war_data() only calls get_clan() every 2 days, so after
    # a restart coc_role stays None until that throttle expires.  Force
    # one get_clan() per member clan per process lifetime so role sync
    # has valid data on the very first cycle after startup.
    # ------------------------------------------------------------------
    if coc_role_enabled:
        _clans_to_bootstrap: set[str] = set(config.get("member_clans", []))
        for _fid in config.get("member_families", []):
            _fdata = CACHE.clan_families.get(_fid, {})
            _clans_to_bootstrap.update(_fdata.get("clans", []))
        _needs_bootstrap = _clans_to_bootstrap - _coc_role_refreshed_clans
        if _needs_bootstrap:
            logging.info(f"[ROLE-SYNC] Bootstrapping CoC role data for {len(_needs_bootstrap)} clan(s) (first sync after startup): {_needs_bootstrap}")

            async def _bootstrap_clan(_ctag: str) -> tuple[str, bool]:
                try:
                    await CACHE.coc_clan_cache.get_clan(_ctag)
                    return _ctag, True
                except Exception as _e:
                    logging.warning(f"[ROLE-SYNC] Failed to bootstrap CoC role data for clan {_ctag}: {_e}")
                    return _ctag, False

            _results = await asyncio.gather(*[_bootstrap_clan(ct) for ct in _needs_bootstrap])
            for _ctag_r, _ok in _results:
                if _ok:
                    _coc_role_refreshed_clans.add(_ctag_r)

    # This bot serves multiple Discord guilds from one shared registration pool, so
    # CACHE.user_accounts is bot-wide — most registered users belong to a *different*
    # guild than this one. Track which of them we'd actually EXPECT to find as members
    # here (their linked player's current_clan_tag is in a clan this guild covers) so the
    # missing-from-cache check below compares against the right population instead of
    # flagging the (large, expected) set of users who simply aren't in this guild at all.
    _guild_clans: set[str] = set(config.get("member_clans", []))
    for _fid in config.get("member_families", []):
        _fdata = CACHE.clan_families.get(_fid, {})
        _guild_clans.update(_fdata.get("clans", []))

    # Collect all Discord user IDs with registered accounts
    user_ids: List[int] = []
    expected_member_ids: set[int] = set()
    for user_id_str, user_data in CACHE.user_accounts.items():
        if user_id_str == "UNASSIGNED" or not user_data:
            continue
        try:
            _uid = int(user_id_str)
        except ValueError:
            continue
        user_ids.append(_uid)
        if _guild_clans and set(_get_clan_tags_for_user(user_id_str)) & _guild_clans:
            expected_member_ids.add(_uid)

    logging.info(
        f"[ROLE-SYNC] Starting guild-wide role sync for guild {guild.name} ({guild_id}): "
        f"{len(expected_member_ids)} guild-relevant of {len(user_ids)} bot-wide registered users"
    )

    # Build a set of registered user IDs for fast O(1) lookup.
    registered_ids: set[int] = set(user_ids)

    # Chunk the guild once to populate discord.py's local member cache, then
    # iterate only guild members who are registered — avoids scanning all
    # registered users for every guild.
    if not guild.chunked:
        await guild.chunk(cache=True)

    synced = 0
    errors = 0
    seen_ids: set[int] = set()
    for member in guild.members:
        if member.id not in registered_ids:
            continue
        seen_ids.add(member.id)
        try:
            if await sync_roles_for_user(guild, guild_id, member.id, member=member):
                synced += 1
        except Exception as e:
            errors += 1
            logging.warning(f"[ROLE-SYNC] Error syncing roles for user {member.id} in guild {guild.name} ({guild_id}): {e}")

    # An EXPECTED member (registered with a player currently in a clan this guild covers)
    # not found in guild.members after chunk() is ambiguous from the cache alone: guild.chunked
    # is a live count comparison (guild.member_count == len(guild._members)), not a "did we ever
    # chunk" flag, so a cache miss here could mean the user genuinely left the guild (the common
    # case — QapBot doesn't auto-unregister on leave, and this bot's registration pool is shared
    # across multiple guilds so a user can be legitimately absent from any one of them) OR a
    # transient gap (chunk() raced a member join, a brief Discord hiccup). Don't guess from the
    # cache — let sync_roles_for_user's own get_member()-then-fetch_member() fallback (already
    # used by the fast path above and by sync_roles_for_clan_members) make the authoritative call:
    # discord.NotFound → confirmed not a member → silently skipped (DEBUG only, no warning —
    # role sync for a non-member is meaningless, not an error); a successful fetch means the
    # cache was stale and this self-heals by syncing them right here; only a persistent
    # HTTPException (rate limit, outage — genuinely inconclusive) still logs a WARNING, from
    # inside _sync_roles_for_user_impl itself.
    # Capped so a pathologically large gap (e.g. real cache corruption) can't fire an unbounded
    # burst of individual fetch_member() calls in one run — the remainder just gets picked up
    # next cycle once guild.chunked naturally re-syncs the local cache.
    missing_ids = expected_member_ids - seen_ids
    if missing_ids:
        _to_verify = sorted(missing_ids)[:50]
        if len(missing_ids) > len(_to_verify):
            logging.info(
                f"[ROLE-SYNC] Guild {guild.name} ({guild_id}): verifying only {len(_to_verify)} of "
                f"{len(missing_ids)} expected-but-uncached member(s) this run; rest deferred to next cycle"
            )
        for _uid in _to_verify:
            try:
                if await sync_roles_for_user(guild, guild_id, _uid):
                    synced += 1
                # else: confirmed not a member (or otherwise skipped) — already logged
                # at DEBUG (or WARNING, if genuinely inconclusive) inside sync_roles_for_user.
            except Exception as e:
                errors += 1
                logging.warning(f"[ROLE-SYNC] Error syncing roles for user {_uid} in guild {guild.name} ({guild_id}): {e}")

    logging.info(f"[ROLE-SYNC] Guild {guild.name} ({guild_id}) role sync complete: {synced} synced, {errors} errors")


async def sync_roles_for_clan_members(
    guild: discord.Guild,
    guild_id: str,
    clan_tag: str,
    coc_members: "List[Any]",  # List[coc.ClanMember]
) -> None:
    """
    Sync roles for all current members of a specific clan after an API fetch.

    This is a fast path: only touches users registered in this clan, rather than
    all users registered on the server.

    Args:
        guild: Discord guild.
        guild_id: Guild ID string.
        clan_tag: Clan tag that was just fetched from API.
        coc_members: List of coc.ClanMember objects from the API response.
    """
    config = _get_config(guild_id)
    coc_role_enabled: bool = config.get("coc_role_enabled", False)
    clan_role_enabled: bool = config.get("clan_role_enabled", False)

    if not coc_role_enabled and not clan_role_enabled:
        return

    from qapbot.cache_manager import CACHE

    # Build lookup: player_tag -> coc role string
    member_coc_roles: Dict[str, str] = {}
    for m in coc_members:
        tag = getattr(m, "tag", None)
        role = getattr(m, "role", None)
        if tag and role:
            member_coc_roles[tag] = str(role)

    # Find Discord users who have accounts in this clan
    discord_users_to_sync: List[int] = []
    for user_id_str, user_data in CACHE.user_accounts.items():
        if user_id_str == "UNASSIGNED" or not user_data:
            continue
        for player in user_data.get("players", []):
            if not isinstance(player, dict):
                continue
            if player.get("current_clan_tag") == clan_tag:  # type: ignore[union-attr]
                try:
                    discord_users_to_sync.append(int(user_id_str))
                except ValueError:
                    pass
                break

    for discord_user_id in discord_users_to_sync:
        try:
            await sync_roles_for_user(guild, guild_id, discord_user_id)
        except Exception as e:
            logging.warning(f"[ROLE-SYNC] Error syncing roles for user {discord_user_id} in guild {guild.name} ({guild_id}) (clan {clan_tag}): {e}")


# ---------------------------------------------------------------------------
# Create all clan roles for member clans
# ---------------------------------------------------------------------------

async def create_all_clan_roles(
    guild: discord.Guild,
    guild_id: str,
) -> Dict[str, Optional[str]]:
    """
    Ensure a Discord role exists for every configured member clan and family clan.

    Called when "clan role feature" is enabled.

    Args:
        guild: Discord guild.
        guild_id: Guild ID string.

    Returns:
        Dict {clan_tag: discord_role_id or None} for all configured clans.
    """
    from qapbot.cache_manager import CACHE

    config = _get_config(guild_id)
    member_clans: List[str] = config.get("member_clans", [])

    # Also include clans from member families
    all_clans: List[str] = list(member_clans)
    for family_id in config.get("member_families", []):
        family_data = CACHE.clan_families.get(family_id, {})
        for c in family_data.get("clans", []):
            if c not in all_clans:
                all_clans.append(c)

    # Create roles in alphabetical order by clan name for a predictable Discord role list
    all_clans.sort(key=lambda tag: (CACHE.get_clan_name(tag, tag) or tag).lower())

    result: Dict[str, Optional[str]] = {}
    for clan_tag in all_clans:
        clan_name = CACHE.get_clan_name(clan_tag, clan_tag)
        role_id = await create_clan_role(guild, guild_id, clan_tag, clan_name or clan_tag)
        result[clan_tag] = role_id

    return result


# ---------------------------------------------------------------------------
# Role deletion utilities for UI confirmation flows
# ---------------------------------------------------------------------------

def get_coc_roles_to_delete(guild: discord.Guild, guild_id: str) -> List[Tuple[str, str, Optional[discord.Role]]]:
    """
    Return info about CoC in-game roles that would be deleted on disable.

    Args:
        guild: Discord guild.
        guild_id: Guild ID string.

    Returns:
        List of (coc_role_key, display_name, discord_role_or_None).
    """
    config = _get_config(guild_id)
    result: List[Tuple[str, str, Optional[discord.Role]]] = []
    for coc_role_key, config_key in COC_ROLE_CONFIG_KEY.items():
        role_id_str = config.get(config_key)
        if role_id_str:
            role = guild.get_role(int(role_id_str))
            display = COC_ROLE_DISPLAY_NAMES[coc_role_key]
            result.append((coc_role_key, display, role))
    return result


def get_clan_roles_to_delete(guild: discord.Guild, guild_id: str) -> List[Tuple[str, str, Optional[discord.Role]]]:
    """
    Return info about per-clan roles that would be deleted on disable.

    Args:
        guild: Discord guild.
        guild_id: Guild ID string.

    Returns:
        List of (clan_tag, clan_name, discord_role_or_None).
    """
    from qapbot.cache_manager import CACHE

    config = _get_config(guild_id)
    clan_roles: Dict[str, str] = config.get("clan_roles", {})
    result: List[Tuple[str, str, Optional[discord.Role]]] = []
    for clan_tag, role_id_str in clan_roles.items():
        clan_name = CACHE.get_clan_name(clan_tag, clan_tag) or clan_tag
        role = guild.get_role(int(role_id_str)) if role_id_str else None
        result.append((clan_tag, clan_name, role))
    return result


async def delete_all_coc_ingame_roles(guild: discord.Guild, guild_id: str) -> None:
    """
    Delete all four CoC in-game Discord roles and clear their IDs from config.

    Args:
        guild: Discord guild.
        guild_id: Guild ID string.
    """
    from qapbot.cache_manager import CACHE

    config = _get_config(guild_id)
    for config_key in COC_ROLE_CONFIG_KEY.values():
        role_id_str = config.get(config_key)
        if role_id_str:
            success = await delete_discord_role_safe(guild, int(role_id_str), reason="QapBot: CoC role feature disabled")
            if success:
                config[config_key] = None
            else:
                logging.warning(f"[ROLE-MGR] Could not delete CoC role {role_id_str} from guild {guild.id} – ID kept in config")

    await CACHE.persist_server_config(guild_id)


async def delete_all_clan_roles(guild: discord.Guild, guild_id: str) -> None:
    """
    Delete all per-clan Discord roles and clear them from config.

    Args:
        guild: Discord guild.
        guild_id: Guild ID string.
    """
    from qapbot.cache_manager import CACHE

    config = _get_config(guild_id)
    clan_roles: Dict[str, str] = dict(config.get("clan_roles", {}))

    for clan_tag, role_id_str in clan_roles.items():
        success = True
        if role_id_str:
            success = await delete_discord_role_safe(guild, int(role_id_str), reason="QapBot: clan role feature disabled")
        if success:
            config.get("clan_roles", {}).pop(clan_tag, None)
            try:
                await CACHE.db_manager.delete_guild_clan_role(guild_id, clan_tag)  # type: ignore[union-attr]
            except Exception as e:
                logging.error(f"[ROLE-MGR] Failed to remove clan role {clan_tag} from DB: {e}")
        else:
            logging.warning(f"[ROLE-MGR] Could not delete clan role {role_id_str} for {clan_tag} in guild {guild.id} – entry kept in config")

    await CACHE.persist_server_config(guild_id)
