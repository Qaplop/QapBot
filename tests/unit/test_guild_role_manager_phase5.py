"""Tests for guild_role_manager.py — Phase 5 coverage push.

Targets:
- normalize_discord_role_name
- _get_config
- get_coc_ingame_role_ids
- list_coc_ingame_roles
- list_clan_roles
- _get_clan_tags_for_user
- _get_highest_coc_role_for_user
- assign_role_to_member / remove_role_from_member
- get_coc_roles_to_delete / get_clan_roles_to_delete
- delete_all_coc_ingame_roles / delete_all_clan_roles
- get_or_create_discord_role / delete_discord_role_safe
- create_coc_ingame_roles / create_clan_role / create_all_clan_roles
- sync_roles_for_user / sync_all_roles_for_guild
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnusedImport=false, reportAttributeAccessIssue=false, reportReturnType=false, reportOptionalMemberAccess=false
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cache(**overrides) -> MagicMock:
    cache = MagicMock()
    cache.server_config = overrides.get("server_config", {})
    cache.user_accounts = overrides.get("user_accounts", {})
    cache.clan_families = overrides.get("clan_families", {})
    cache.persist_server_config = AsyncMock()
    cache.db_manager = MagicMock()
    cache.db_manager.save_guild_clan_role = AsyncMock()
    cache.db_manager.delete_guild_clan_role = AsyncMock()
    cache.coc_clan_cache = MagicMock()
    cache.coc_clan_cache.get_clan = AsyncMock()
    cache.get_clan_name = MagicMock(side_effect=lambda tag, default="": default)  # type: ignore[misc]
    return cache


def _make_guild(roles=None) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = 12345
    guild.name = "TestGuild"
    guild.roles = roles or []
    guild.get_role = MagicMock(side_effect=lambda rid: next((r for r in guild.roles if r.id == rid), None))  # type: ignore[misc]
    guild.create_role = AsyncMock()
    guild.chunked = True
    guild.members = []
    guild.chunk = AsyncMock()
    guild.get_member = MagicMock(return_value=None)
    guild.fetch_member = AsyncMock(side_effect=discord.NotFound(MagicMock(), "not found"))
    return guild


def _make_role(role_id, name="TestRole"):
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = name
    role.delete = AsyncMock()
    return role


def _make_member(member_id=1, roles=None):
    member = MagicMock(spec=discord.Member)
    member.id = member_id
    member.display_name = f"User{member_id}"
    member.roles = roles or []
    member.guild = _make_guild()
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    return member


# ===========================================================================
# normalize_discord_role_name
# ===========================================================================

class TestNormalizeDiscordRoleName:
    def _fn(self):
        from qapbot.guild_role_manager import normalize_discord_role_name
        return normalize_discord_role_name

    def test_basic_name(self):
        assert self._fn()("Hello World") == "Hello World"

    def test_strips_special_chars(self):
        assert self._fn()("The Q-Crew!") == "The Q-Crew"

    def test_strips_parentheses(self):
        assert self._fn()("  Hello (World)  ") == "Hello World"

    def test_truncates_100(self):
        name = "A" * 200
        assert len(self._fn()(name)) == 100

    def test_collapses_whitespace(self):
        assert self._fn()("Hello    World") == "Hello World"

    def test_preserves_apostrophe(self):
        assert self._fn()("It's Fine") == "It's Fine"

    def test_preserves_hyphens(self):
        assert self._fn()("Co-Leader") == "Co-Leader"

    def test_preserves_unicode(self):
        assert self._fn()("Ünïcödé") == "Ünïcödé"

    def test_empty_string(self):
        assert self._fn()("") == ""

    def test_only_special_chars(self):
        assert self._fn()("!!!@@@###") == ""


# ===========================================================================
# _get_config
# ===========================================================================

class TestGetConfig:
    def test_returns_config_for_guild(self, monkeypatch):
        from qapbot.guild_role_manager import _get_config
        cache = _make_cache(server_config={"123": {"member_clans": ["#A"]}})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        assert _get_config("123") == {"member_clans": ["#A"]}

    def test_returns_empty_for_missing_guild(self, monkeypatch):
        from qapbot.guild_role_manager import _get_config
        cache = _make_cache()
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        assert _get_config("999") == {}


# ===========================================================================
# get_coc_ingame_role_ids
# ===========================================================================

class TestGetCocIngameRoleIds:
    def test_returns_ids(self, monkeypatch):
        from qapbot.guild_role_manager import get_coc_ingame_role_ids
        cache = _make_cache(server_config={
            "123": {
                "coc_role_member_id": "111",
                "coc_role_elder_id": "222",
                "coc_role_coleader_id": "333",
                "coc_role_leader_id": "444",
            }
        })
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        result = get_coc_ingame_role_ids("123")
        assert result == {
            "member": "111",
            "elder": "222",
            "coLeader": "333",
            "leader": "444",
        }

    def test_returns_none_for_missing(self, monkeypatch):
        from qapbot.guild_role_manager import get_coc_ingame_role_ids
        cache = _make_cache(server_config={"123": {}})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        result = get_coc_ingame_role_ids("123")
        assert all(v is None for v in result.values())


# ===========================================================================
# list_coc_ingame_roles
# ===========================================================================

class TestListCocIngameRoles:
    def test_returns_existing_roles(self, monkeypatch):
        from qapbot.guild_role_manager import list_coc_ingame_roles
        role1 = _make_role(111, "CoC Member")
        role2 = _make_role(222, "CoC Elder")
        guild = _make_guild(roles=[role1, role2])
        cache = _make_cache(server_config={"G1": {
            "coc_role_member_id": "111",
            "coc_role_elder_id": "222",
            "coc_role_coleader_id": "333",
        }})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        result = list_coc_ingame_roles(guild, "G1")
        assert role1 in result
        assert role2 in result
        assert len(result) == 2  # 333 not found

    def test_empty_config(self, monkeypatch):
        from qapbot.guild_role_manager import list_coc_ingame_roles
        guild = _make_guild()
        cache = _make_cache(server_config={"G1": {}})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        assert list_coc_ingame_roles(guild, "G1") == []


# ===========================================================================
# list_clan_roles
# ===========================================================================

class TestListClanRoles:
    def test_returns_existing_clan_roles(self, monkeypatch):
        from qapbot.guild_role_manager import list_clan_roles
        role1 = _make_role(111, "ClanA")
        guild = _make_guild(roles=[role1])
        cache = _make_cache(server_config={"G1": {"clan_roles": {"#CLANA": "111", "#CLANB": "222"}}})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        result = list_clan_roles(guild, "G1")
        assert len(result) == 1
        assert result[0] == ("#CLANA", role1)

    def test_empty_clan_roles(self, monkeypatch):
        from qapbot.guild_role_manager import list_clan_roles
        guild = _make_guild()
        cache = _make_cache(server_config={"G1": {}})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        assert list_clan_roles(guild, "G1") == []


# ===========================================================================
# _get_clan_tags_for_user
# ===========================================================================

class TestGetClanTagsForUser:
    def test_returns_tags(self, monkeypatch):
        from qapbot.guild_role_manager import _get_clan_tags_for_user
        cache = _make_cache(user_accounts={
            "U1": {"players": [
                {"current_clan_tag": "#C1"},
                {"current_clan_tag": "#C2"},
                {"current_clan_tag": "#C1"},  # dup
            ]}
        })
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        result = _get_clan_tags_for_user("U1")
        assert result == ["#C1", "#C2"]

    def test_no_user(self, monkeypatch):
        from qapbot.guild_role_manager import _get_clan_tags_for_user
        cache = _make_cache(user_accounts={})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        assert _get_clan_tags_for_user("U1") == []

    def test_skips_non_dict_players(self, monkeypatch):
        from qapbot.guild_role_manager import _get_clan_tags_for_user
        cache = _make_cache(user_accounts={
            "U1": {"players": ["bad", None, {"current_clan_tag": "#C1"}]}
        })
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        assert _get_clan_tags_for_user("U1") == ["#C1"]

    def test_skips_empty_tags(self, monkeypatch):
        from qapbot.guild_role_manager import _get_clan_tags_for_user
        cache = _make_cache(user_accounts={
            "U1": {"players": [{"current_clan_tag": ""}, {"current_clan_tag": None}]}
        })
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        assert _get_clan_tags_for_user("U1") == []


# ===========================================================================
# _get_highest_coc_role_for_user
# ===========================================================================

class TestGetHighestCocRoleForUser:
    def test_leader_wins(self, monkeypatch):
        from qapbot.guild_role_manager import _get_highest_coc_role_for_user
        cache = _make_cache(user_accounts={
            "U1": {"players": [
                {"coc_role": "member", "current_clan_tag": "#C1"},
                {"coc_role": "leader", "current_clan_tag": "#C2"},
            ]}
        })
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        assert _get_highest_coc_role_for_user("U1") == "leader"

    def test_guild_clans_filter(self, monkeypatch):
        from qapbot.guild_role_manager import _get_highest_coc_role_for_user
        cache = _make_cache(user_accounts={
            "U1": {"players": [
                {"coc_role": "leader", "current_clan_tag": "#OUTSIDE1"},
                {"coc_role": "elder", "current_clan_tag": "#C1"},
            ]}
        })
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        result = _get_highest_coc_role_for_user("U1", guild_clans={"#C1"})
        assert result == "elder"  # leader filtered out

    def test_no_user(self, monkeypatch):
        from qapbot.guild_role_manager import _get_highest_coc_role_for_user
        cache = _make_cache(user_accounts={})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        assert _get_highest_coc_role_for_user("U1") is None

    def test_no_coc_role(self, monkeypatch):
        from qapbot.guild_role_manager import _get_highest_coc_role_for_user
        cache = _make_cache(user_accounts={
            "U1": {"players": [{"current_clan_tag": "#C1"}]}
        })
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        assert _get_highest_coc_role_for_user("U1") is None

    def test_unknown_role_ignored(self, monkeypatch):
        from qapbot.guild_role_manager import _get_highest_coc_role_for_user
        cache = _make_cache(user_accounts={
            "U1": {"players": [{"coc_role": "unknown_role", "current_clan_tag": "#C1"}]}
        })
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        assert _get_highest_coc_role_for_user("U1") is None

    def test_skips_non_dict_players(self, monkeypatch):
        from qapbot.guild_role_manager import _get_highest_coc_role_for_user
        cache = _make_cache(user_accounts={
            "U1": {"players": ["bad", {"coc_role": "elder", "current_clan_tag": "#C1"}]}
        })
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        assert _get_highest_coc_role_for_user("U1") == "elder"


# ===========================================================================
# assign_role_to_member / remove_role_from_member
# ===========================================================================

class TestAssignRole:
    @pytest.mark.asyncio
    async def test_assigns_when_missing(self):
        from qapbot.guild_role_manager import assign_role_to_member
        role = _make_role(1)
        member = _make_member(roles=[])
        assert await assign_role_to_member(member, role) is True
        member.add_roles.assert_awaited_once_with(role, reason="QapBot role sync")

    @pytest.mark.asyncio
    async def test_skips_when_present(self):
        from qapbot.guild_role_manager import assign_role_to_member
        role = _make_role(1)
        member = _make_member(roles=[role])
        assert await assign_role_to_member(member, role) is True
        member.add_roles.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_forbidden_returns_false(self):
        from qapbot.guild_role_manager import assign_role_to_member
        role = _make_role(1)
        member = _make_member(roles=[])
        member.add_roles = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "nope"))
        assert await assign_role_to_member(member, role) is False

    @pytest.mark.asyncio
    async def test_http_exception_returns_false(self):
        from qapbot.guild_role_manager import assign_role_to_member
        role = _make_role(1)
        member = _make_member(roles=[])
        member.add_roles = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "err"))
        assert await assign_role_to_member(member, role) is False


class TestRemoveRole:
    @pytest.mark.asyncio
    async def test_removes_when_present(self):
        from qapbot.guild_role_manager import remove_role_from_member
        role = _make_role(1)
        member = _make_member(roles=[role])
        assert await remove_role_from_member(member, role) is True
        member.remove_roles.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_when_absent(self):
        from qapbot.guild_role_manager import remove_role_from_member
        role = _make_role(1)
        member = _make_member(roles=[])
        assert await remove_role_from_member(member, role) is True
        member.remove_roles.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_forbidden_returns_false(self):
        from qapbot.guild_role_manager import remove_role_from_member
        role = _make_role(1)
        member = _make_member(roles=[role])
        member.remove_roles = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "nope"))
        assert await remove_role_from_member(member, role) is False

    @pytest.mark.asyncio
    async def test_http_exception_returns_false(self):
        from qapbot.guild_role_manager import remove_role_from_member
        role = _make_role(1)
        member = _make_member(roles=[role])
        member.remove_roles = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "err"))
        assert await remove_role_from_member(member, role) is False


# ===========================================================================
# get_coc_roles_to_delete / get_clan_roles_to_delete
# ===========================================================================

class TestGetCocRolesToDelete:
    def test_returns_stored_roles(self, monkeypatch):
        from qapbot.guild_role_manager import get_coc_roles_to_delete
        role1 = _make_role(111, "CoC Member")
        guild = _make_guild(roles=[role1])
        cache = _make_cache(server_config={"G1": {"coc_role_member_id": "111"}})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        result = get_coc_roles_to_delete(guild, "G1")
        assert len(result) == 1
        assert result[0][0] == "member"
        assert result[0][2] == role1

    def test_empty_config(self, monkeypatch):
        from qapbot.guild_role_manager import get_coc_roles_to_delete
        guild = _make_guild()
        cache = _make_cache(server_config={"G1": {}})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        assert get_coc_roles_to_delete(guild, "G1") == []


class TestGetClanRolesToDelete:
    def test_returns_stored_clan_roles(self, monkeypatch):
        from qapbot.guild_role_manager import get_clan_roles_to_delete
        role1 = _make_role(111, "ClanA")
        guild = _make_guild(roles=[role1])
        cache = _make_cache(server_config={"G1": {"clan_roles": {"#CA": "111"}}})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        result = get_clan_roles_to_delete(guild, "G1")
        assert len(result) == 1
        assert result[0][0] == "#CA"
        assert result[0][2] == role1


# ===========================================================================
# get_or_create_discord_role
# ===========================================================================

class TestGetOrCreateDiscordRole:
    @pytest.mark.asyncio
    async def test_finds_existing(self):
        from qapbot.guild_role_manager import get_or_create_discord_role
        role = _make_role(1, "Hello World")
        guild = _make_guild(roles=[role])
        result = await get_or_create_discord_role(guild, "Hello World")
        assert result == role
        guild.create_role.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_creates_new(self):
        from qapbot.guild_role_manager import get_or_create_discord_role
        new_role = _make_role(2, "NewRole")
        guild = _make_guild(roles=[])
        guild.create_role = AsyncMock(return_value=new_role)
        result = await get_or_create_discord_role(guild, "NewRole")
        assert result == new_role

    @pytest.mark.asyncio
    async def test_empty_name_returns_none(self):
        from qapbot.guild_role_manager import get_or_create_discord_role
        guild = _make_guild()
        result = await get_or_create_discord_role(guild, "!!!@@@")
        assert result is None

    @pytest.mark.asyncio
    async def test_forbidden_returns_none(self):
        from qapbot.guild_role_manager import get_or_create_discord_role
        guild = _make_guild()
        guild.create_role = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "nope"))
        result = await get_or_create_discord_role(guild, "MyRole")
        assert result is None

    @pytest.mark.asyncio
    async def test_http_error_returns_none(self):
        from qapbot.guild_role_manager import get_or_create_discord_role
        guild = _make_guild()
        guild.create_role = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "err"))
        result = await get_or_create_discord_role(guild, "MyRole")
        assert result is None

    @pytest.mark.asyncio
    async def test_with_color(self):
        from qapbot.guild_role_manager import get_or_create_discord_role
        new_role = _make_role(3, "Colored")
        guild = _make_guild()
        guild.create_role = AsyncMock(return_value=new_role)
        result = await get_or_create_discord_role(guild, "Colored", color=discord.Color.red())
        assert result == new_role
        # Verify color was passed
        call_kwargs = guild.create_role.call_args[1]
        assert "color" in call_kwargs


# ===========================================================================
# delete_discord_role_safe
# ===========================================================================

class TestDeleteDiscordRoleSafe:
    @pytest.mark.asyncio
    async def test_deletes_existing(self):
        from qapbot.guild_role_manager import delete_discord_role_safe
        role = _make_role(111, "ToDelete")
        guild = _make_guild(roles=[role])
        assert await delete_discord_role_safe(guild, 111) is True
        role.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_not_found_returns_true(self):
        from qapbot.guild_role_manager import delete_discord_role_safe
        guild = _make_guild(roles=[])
        assert await delete_discord_role_safe(guild, 999) is True

    @pytest.mark.asyncio
    async def test_forbidden_returns_false(self):
        from qapbot.guild_role_manager import delete_discord_role_safe
        role = _make_role(111)
        role.delete = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "nope"))
        guild = _make_guild(roles=[role])
        assert await delete_discord_role_safe(guild, 111) is False

    @pytest.mark.asyncio
    async def test_discord_not_found_returns_true(self):
        from qapbot.guild_role_manager import delete_discord_role_safe
        role = _make_role(111)
        role.delete = AsyncMock(side_effect=discord.NotFound(MagicMock(), "gone"))
        guild = _make_guild(roles=[role])
        assert await delete_discord_role_safe(guild, 111) is True

    @pytest.mark.asyncio
    async def test_http_exception_returns_false(self):
        from qapbot.guild_role_manager import delete_discord_role_safe
        role = _make_role(111)
        role.delete = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "err"))
        guild = _make_guild(roles=[role])
        assert await delete_discord_role_safe(guild, 111) is False


# ===========================================================================
# delete_all_coc_ingame_roles / delete_all_clan_roles
# ===========================================================================

class TestDeleteAllCocIngameRoles:
    @pytest.mark.asyncio
    async def test_deletes_all_stored(self, monkeypatch):
        from qapbot.guild_role_manager import delete_all_coc_ingame_roles
        role = _make_role(111, "CoC Member")
        guild = _make_guild(roles=[role])
        config = {"coc_role_member_id": "111", "coc_role_elder_id": None}
        cache = _make_cache(server_config={"G1": config})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        await delete_all_coc_ingame_roles(guild, "G1")
        role.delete.assert_awaited_once()
        assert config["coc_role_member_id"] is None
        cache.persist_server_config.assert_awaited_once_with("G1")


class TestDeleteAllClanRoles:
    @pytest.mark.asyncio
    async def test_deletes_and_cleans_db(self, monkeypatch):
        from qapbot.guild_role_manager import delete_all_clan_roles
        role = _make_role(111, "ClanA")
        guild = _make_guild(roles=[role])
        config = {"clan_roles": {"#CA": "111"}}
        cache = _make_cache(server_config={"G1": config})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        await delete_all_clan_roles(guild, "G1")
        role.delete.assert_awaited_once()
        assert "#CA" not in config["clan_roles"]
        cache.db_manager.delete_guild_clan_role.assert_awaited_once_with("G1", "#CA")
        cache.persist_server_config.assert_awaited_once_with("G1")


# ===========================================================================
# create_coc_ingame_roles
# ===========================================================================

class TestCreateCocIngameRoles:
    @pytest.mark.asyncio
    async def test_creates_missing_roles(self, monkeypatch):
        from qapbot.guild_role_manager import create_coc_ingame_roles
        new_role = _make_role(999, "CoC Member")
        guild = _make_guild()
        guild.create_role = AsyncMock(return_value=new_role)
        cache = _make_cache(server_config={"G1": {}})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        result = await create_coc_ingame_roles(guild, "G1")
        assert all(v == "999" or v == "" for v in result.values())
        cache.persist_server_config.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reuses_existing_roles(self, monkeypatch):
        from qapbot.guild_role_manager import create_coc_ingame_roles
        role = _make_role(111, "CoC Member")
        guild = _make_guild(roles=[role])
        config = {"coc_role_member_id": "111"}
        cache = _make_cache(server_config={"G1": config})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        # Only create_role should be called for the 3 roles that don't have stored IDs
        new_role = _make_role(222, "New")
        guild.create_role = AsyncMock(return_value=new_role)
        result = await create_coc_ingame_roles(guild, "G1")
        assert result["member"] == "111"  # Reused


# ===========================================================================
# create_clan_role
# ===========================================================================

class TestCreateClanRole:
    @pytest.mark.asyncio
    async def test_creates_new_clan_role(self, monkeypatch):
        from qapbot.guild_role_manager import create_clan_role
        new_role = _make_role(999, "MyClan")
        guild = _make_guild()
        guild.create_role = AsyncMock(return_value=new_role)
        cache = _make_cache(server_config={"G1": {}})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        result = await create_clan_role(guild, "G1", "#CLAN1234", "MyClan")
        assert result == "999"
        cache.db_manager.save_guild_clan_role.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reuses_existing(self, monkeypatch):
        from qapbot.guild_role_manager import create_clan_role
        role = _make_role(111, "MyClan")
        guild = _make_guild(roles=[role])
        cache = _make_cache(server_config={"G1": {"clan_roles": {"#C": "111"}}})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        result = await create_clan_role(guild, "G1", "#C", "MyClan")
        assert result == "111"

    @pytest.mark.asyncio
    async def test_empty_name_returns_none(self, monkeypatch):
        from qapbot.guild_role_manager import create_clan_role
        guild = _make_guild()
        cache = _make_cache(server_config={"G1": {}})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        result = await create_clan_role(guild, "G1", "#C", "!!!")
        assert result is None


# ===========================================================================
# create_all_clan_roles
# ===========================================================================

class TestCreateAllClanRoles:
    @pytest.mark.asyncio
    async def test_creates_for_all_member_clans(self, monkeypatch):
        from qapbot.guild_role_manager import create_all_clan_roles
        new_role = _make_role(999, "Clan")
        guild = _make_guild()
        guild.create_role = AsyncMock(return_value=new_role)
        config = {"member_clans": ["#C1", "#C2"], "member_families": []}
        cache = _make_cache(server_config={"G1": config})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        result = await create_all_clan_roles(guild, "G1")
        assert "#C1" in result
        assert "#C2" in result

    @pytest.mark.asyncio
    async def test_includes_family_clans(self, monkeypatch):
        from qapbot.guild_role_manager import create_all_clan_roles
        new_role = _make_role(999, "Clan")
        guild = _make_guild()
        guild.create_role = AsyncMock(return_value=new_role)
        config = {"member_clans": [], "member_families": ["fam1"]}
        cache = _make_cache(server_config={"G1": config}, clan_families={"fam1": {"clans": ["#F1"]}})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        result = await create_all_clan_roles(guild, "G1")
        assert "#F1" in result


# ===========================================================================
# delete_clan_role_from_guild
# ===========================================================================

class TestDeleteClanRoleFromGuild:
    @pytest.mark.asyncio
    async def test_deletes_and_persists(self, monkeypatch):
        from qapbot.guild_role_manager import delete_clan_role_from_guild
        role = _make_role(111, "ClanA")
        guild = _make_guild(roles=[role])
        config = {"clan_roles": {"#CA": "111"}}
        cache = _make_cache(server_config={"G1": config})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        assert await delete_clan_role_from_guild(guild, "G1", "#CA") is True
        assert "#CA" not in config["clan_roles"]

    @pytest.mark.asyncio
    async def test_no_stored_role_returns_true(self, monkeypatch):
        from qapbot.guild_role_manager import delete_clan_role_from_guild
        guild = _make_guild()
        cache = _make_cache(server_config={"G1": {"clan_roles": {}}})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        assert await delete_clan_role_from_guild(guild, "G1", "#MISSING") is True


# ===========================================================================
# sync_roles_for_user
# ===========================================================================

class TestSyncRolesForUser:
    @pytest.mark.asyncio
    async def test_skips_when_both_disabled(self, monkeypatch):
        from qapbot.guild_role_manager import sync_roles_for_user
        guild = _make_guild()
        cache = _make_cache(server_config={"G1": {"coc_role_enabled": False, "clan_role_enabled": False}})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        await sync_roles_for_user(guild, "G1", 123)
        # No member fetch attempted
        guild.fetch_member.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_assigns_coc_role(self, monkeypatch):
        from qapbot.guild_role_manager import sync_roles_for_user
        role_member = _make_role(111, "CoC Member")
        role_elder = _make_role(222, "CoC Elder")
        guild = _make_guild(roles=[role_member, role_elder])
        member = _make_member(member_id=42, roles=[])
        guild.get_member = MagicMock(return_value=member)
        cache = _make_cache(
            server_config={"G1": {
                "coc_role_enabled": True, "clan_role_enabled": False,
                "coc_role_member_id": "111", "coc_role_elder_id": "222",
                "member_clans": ["#C1"], "member_families": [],
            }},
            user_accounts={
                "42": {"players": [{"coc_role": "elder", "current_clan_tag": "#C1"}]}
            },
        )
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        await sync_roles_for_user(guild, "G1", 42)
        member.add_roles.assert_awaited()  # elder added
        # remove_roles not called because member has no roles to remove
        member.remove_roles.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_user_not_found_skips(self, monkeypatch):
        from qapbot.guild_role_manager import sync_roles_for_user
        guild = _make_guild()
        guild.get_member = MagicMock(return_value=None)
        guild.fetch_member = AsyncMock(side_effect=discord.NotFound(MagicMock(), "not found"))
        cache = _make_cache(server_config={"G1": {"coc_role_enabled": True, "clan_role_enabled": False}})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        # Should not raise
        await sync_roles_for_user(guild, "G1", 999)
