"""Unit tests for qapbot.guild_role_manager.

Tests cover:
  - normalize_discord_role_name (pure function)
  - Module constants sanity
  - get_or_create_discord_role (mocked guild)
  - delete_discord_role_safe (mocked role)
  - create_coc_ingame_roles (mocked guild + CACHE)
  - _get_highest_coc_role_for_user (mocked CACHE)
  - create_clan_role / list_clan_roles / delete_clan_role_from_guild (mocked)
  - sync_roles_for_user (mocked guild + CACHE)
"""

from __future__ import annotations

from typing import Any

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Pure-function tests (no mocking needed)
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestNormalizeDiscordRoleName:
    def test_strips_special_chars(self):
        from qapbot.guild_role_manager import normalize_discord_role_name

        assert normalize_discord_role_name("Hello, World!") == "Hello World"

    def test_preserves_hyphens_and_underscores(self):
        from qapbot.guild_role_manager import normalize_discord_role_name

        assert normalize_discord_role_name("My-Clan_Role") == "My-Clan_Role"

    def test_truncates_to_100_chars(self):
        from qapbot.guild_role_manager import normalize_discord_role_name

        long_name = "A" * 200
        result = normalize_discord_role_name(long_name)
        assert len(result) == 100

    def test_empty_string(self):
        from qapbot.guild_role_manager import normalize_discord_role_name

        assert normalize_discord_role_name("") == ""

    def test_unicode_letters_kept(self):
        from qapbot.guild_role_manager import normalize_discord_role_name

        # Letters and digits should be preserved
        result = normalize_discord_role_name("Clàn Röle")
        # Non-ASCII letters are kept (re.sub removes only non-\w\s\-_)
        assert "Cl" in result

    def test_hashtag_stripped(self):
        from qapbot.guild_role_manager import normalize_discord_role_name

        assert "#" not in normalize_discord_role_name("#CLAN123")


@pytest.mark.smoke
class TestConstants:
    def test_coc_role_config_key_has_all_four_roles(self):
        from qapbot.guild_role_manager import COC_ROLE_CONFIG_KEY

        assert set(COC_ROLE_CONFIG_KEY.keys()) == {"member", "elder", "coLeader", "leader"}

    def test_coc_role_priority_ordering(self):
        from qapbot.guild_role_manager import COC_ROLE_PRIORITY

        assert COC_ROLE_PRIORITY["member"] < COC_ROLE_PRIORITY["elder"]
        assert COC_ROLE_PRIORITY["elder"] < COC_ROLE_PRIORITY["coLeader"]
        assert COC_ROLE_PRIORITY["coLeader"] < COC_ROLE_PRIORITY["leader"]

    def test_coc_role_display_names_populated(self):
        from qapbot.guild_role_manager import COC_ROLE_DISPLAY_NAMES, COC_ROLE_CONFIG_KEY

        for coc_key in COC_ROLE_CONFIG_KEY:
            assert coc_key in COC_ROLE_DISPLAY_NAMES
            assert isinstance(COC_ROLE_DISPLAY_NAMES[coc_key], str)
            assert len(COC_ROLE_DISPLAY_NAMES[coc_key]) > 0

    def test_coc_role_field_is_string(self):
        from qapbot.guild_role_manager import COC_ROLE_FIELD

        assert isinstance(COC_ROLE_FIELD, str)
        assert len(COC_ROLE_FIELD) > 0


# ---------------------------------------------------------------------------
# Mocked async tests
# ---------------------------------------------------------------------------


def _make_guild(roles: list[Any] | None = None) -> MagicMock:
    """Build a minimal mock discord.Guild with optional pre-existing roles."""
    guild = MagicMock()
    guild.id = 111222333
    guild.roles = roles or []
    guild.create_role = AsyncMock()
    guild.get_role = MagicMock(return_value=None)
    return guild


def _make_role(name: str, role_id: int):
    role = MagicMock()
    role.name = name
    role.id = role_id
    role.delete = AsyncMock()
    role.mention = f"<@&{role_id}>"
    return role


@pytest.mark.smoke
class TestGetOrCreateDiscordRole:
    async def test_returns_existing_role_by_normalized_name(self):
        from qapbot.guild_role_manager import get_or_create_discord_role

        existing = _make_role("CoC Member", 99)
        guild = _make_guild(roles=[existing])

        result = await get_or_create_discord_role(guild, "CoC Member", reason="test")

        assert result is existing
        guild.create_role.assert_not_called()

    async def test_creates_role_when_not_found(self):
        from qapbot.guild_role_manager import get_or_create_discord_role

        new_role = _make_role("CoC Member", 100)
        guild = _make_guild(roles=[])
        guild.create_role.return_value = new_role

        result = await get_or_create_discord_role(guild, "CoC Member", reason="test")

        guild.create_role.assert_called_once()
        assert result is new_role

    async def test_returns_none_on_forbidden(self):
        import discord
        from qapbot.guild_role_manager import get_or_create_discord_role

        guild = _make_guild(roles=[])
        guild.create_role.side_effect = discord.Forbidden(MagicMock(), "no perms")

        result = await get_or_create_discord_role(guild, "Test Role", reason="test")

        assert result is None


@pytest.mark.smoke
class TestDeleteDiscordRoleSafe:
    async def test_deletes_existing_role(self):
        from qapbot.guild_role_manager import delete_discord_role_safe

        role = _make_role("CoC Member", 42)
        guild = _make_guild()
        guild.get_role.return_value = role

        result = await delete_discord_role_safe(guild, 42, reason="test")

        role.delete.assert_called_once()
        assert result is True

    async def test_returns_true_when_role_not_found(self):
        from qapbot.guild_role_manager import delete_discord_role_safe

        guild = _make_guild()
        guild.get_role.return_value = None

        # Not found = already gone = treated as success (True)
        result = await delete_discord_role_safe(guild, 999, reason="test")

        assert result is True


@pytest.mark.smoke
class TestCreateCocIngameRoles:
    async def test_creates_four_roles_and_stores_ids(self):
        from qapbot.guild_role_manager import create_coc_ingame_roles

        guild = _make_guild(roles=[])

        # create_role returns a new mock each call with incremental IDs
        role_ids = iter(range(1001, 1010))

        def make_role(name: str | None = None, reason: str | None = None, **kwargs: Any) -> MagicMock:
            rid = next(role_ids)
            r = _make_role(name or "role", rid)
            return r

        guild.create_role = AsyncMock(side_effect=make_role)

        fake_config = {}
        fake_cache = MagicMock()
        fake_cache.server_config = {"123456": fake_config}
        fake_cache.persist_server_config = AsyncMock()

        with patch("qapbot.cache_manager.CACHE", fake_cache):
            await create_coc_ingame_roles(guild, "123456")

        assert guild.create_role.call_count == 4
        assert fake_cache.persist_server_config.called
        assert "coc_role_member_id" in fake_config
        assert "coc_role_leader_id" in fake_config
        # All 4 IDs stored
        for key in ("coc_role_member_id", "coc_role_elder_id", "coc_role_coleader_id", "coc_role_leader_id"):
            assert fake_config[key] is not None


@pytest.mark.smoke
class TestGetHighestCocRole:
    def _player(self, tag: str, role: str | None, clan_tag: str | None = None) -> dict[str, Any]:
        return {"player_tag": tag, "coc_role": role, "current_clan_tag": clan_tag}

    async def test_returns_highest_priority_role(self):
        from qapbot.guild_role_manager import _get_highest_coc_role_for_user  # type: ignore[reportPrivateUsage]

        fake_cache = MagicMock()
        fake_cache.user_accounts = {
            "555": {"players": [
                self._player("#A1", "member"),
                self._player("#A2", "coLeader"),
            ]}
        }

        with patch("qapbot.cache_manager.CACHE", fake_cache):
            result = _get_highest_coc_role_for_user("555")

        assert result == "coLeader"

    async def test_returns_none_when_no_accounts(self):
        from qapbot.guild_role_manager import _get_highest_coc_role_for_user  # type: ignore[reportPrivateUsage]

        fake_cache = MagicMock()
        fake_cache.user_accounts = {}

        with patch("qapbot.cache_manager.CACHE", fake_cache):
            result = _get_highest_coc_role_for_user("999")

        assert result is None

    async def test_guild_clans_filter_excludes_unrelated_clan(self):
        """Regression: user is Leader in a clan not tracked by this guild → no CoC role."""
        from qapbot.guild_role_manager import _get_highest_coc_role_for_user  # type: ignore[reportPrivateUsage]

        fake_cache = MagicMock()
        fake_cache.user_accounts = {
            "111": {"players": [
                self._player("#FOREIGN", "leader", clan_tag="#FOREIGN"),
            ]}
        }

        with patch("qapbot.cache_manager.CACHE", fake_cache):
            # guild only tracks #STAY — foreign clan must not yield a role
            result = _get_highest_coc_role_for_user("111", guild_clans={"#STAY"})

        assert result is None

    async def test_guild_clans_filter_includes_matching_clan(self):
        """User with an account in a tracked clan gets the correct CoC role."""
        from qapbot.guild_role_manager import _get_highest_coc_role_for_user  # type: ignore[reportPrivateUsage]

        fake_cache = MagicMock()
        fake_cache.user_accounts = {
            "111": {"players": [
                self._player("#FOREIGN", "leader", clan_tag="#FOREIGN"),
                self._player("#STAY1", "elder", clan_tag="#STAY1"),
            ]}
        }

        with patch("qapbot.cache_manager.CACHE", fake_cache):
            result = _get_highest_coc_role_for_user("111", guild_clans={"#STAY1"})

        assert result == "elder"

    async def test_guild_clans_filter_picks_highest_among_multiple_tracked(self):
        """Multiple accounts in tracked clans → highest role wins."""
        from qapbot.guild_role_manager import _get_highest_coc_role_for_user  # type: ignore[reportPrivateUsage]

        fake_cache = MagicMock()
        fake_cache.user_accounts = {
            "111": {"players": [
                self._player("#STAY1", "member",   clan_tag="#STAY1"),
                self._player("#STAY2", "coLeader", clan_tag="#STAY2"),
                self._player("#FOREIGN", "leader", clan_tag="#FOREIGN"),
            ]}
        }

        with patch("qapbot.cache_manager.CACHE", fake_cache):
            result = _get_highest_coc_role_for_user("111", guild_clans={"#STAY1", "#STAY2"})

        assert result == "coLeader"


@pytest.mark.smoke
class TestCreateClanRole:
    async def test_creates_role_and_persists(self):
        from qapbot.guild_role_manager import create_clan_role

        new_role = _make_role("TestClan", 2001)
        guild = _make_guild(roles=[])
        guild.create_role = AsyncMock(return_value=new_role)

        fake_config: dict[str, Any] = {"clan_roles": {}}
        fake_cache = MagicMock()
        fake_cache.server_config = {"123": fake_config}
        fake_cache.persist_server_config = AsyncMock()
        fake_cache.db_manager = MagicMock()
        fake_cache.db_manager.save_guild_clan_role = AsyncMock()

        with patch("qapbot.cache_manager.CACHE", fake_cache):
            result = await create_clan_role(guild, "123", "#TAG", "TestClan")

        # Returns role ID string, not the Role object
        assert result == str(new_role.id)
        assert fake_config["clan_roles"]["#TAG"] == str(new_role.id)
        fake_cache.db_manager.save_guild_clan_role.assert_called_once_with("123", "#TAG", str(new_role.id))


@pytest.mark.smoke
class TestDeleteAllCocIngameRoles:
    async def test_deletes_all_four_and_clears_config(self):
        from qapbot.guild_role_manager import delete_all_coc_ingame_roles

        roles = {rid: _make_role(f"role{rid}", rid) for rid in (301, 302, 303, 304)}

        guild = _make_guild()
        guild.get_role = MagicMock(side_effect=lambda rid: roles.get(rid))  # type: ignore[arg-type]

        fake_config = {
            "coc_role_member_id": "301",
            "coc_role_elder_id": "302",
            "coc_role_coleader_id": "303",
            "coc_role_leader_id": "304",
        }
        fake_cache = MagicMock()
        fake_cache.server_config = {"99": fake_config}
        fake_cache.persist_server_config = AsyncMock()

        with patch("qapbot.cache_manager.CACHE", fake_cache):
            await delete_all_coc_ingame_roles(guild, "99")

        for r in roles.values():
            r.delete.assert_called_once()
        assert fake_config.get("coc_role_member_id") is None
        assert fake_config.get("coc_role_leader_id") is None


# ---------------------------------------------------------------------------
# Bootstrap refresh (module-level _coc_role_refreshed_clans)
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestCocRoleBootstrap:
    """Verify sync_all_roles_for_guild bootstraps coc_role data on first call."""

    async def test_bootstrap_fetches_uncached_clan(self):
        """First sync after startup should call coc_clan_cache.get_clan() for each member clan."""
        import qapbot.guild_role_manager as grm
        from qapbot.guild_role_manager import sync_all_roles_for_guild

        grm._coc_role_refreshed_clans.clear()  # simulate fresh startup

        guild = _make_guild(roles=[])
        guild.chunked = True  # skip Discord chunk() API call
        guild.members = []    # no members → user loop is a no-op

        fake_coc_cache = MagicMock()
        fake_coc_cache.get_clan = AsyncMock()

        fake_cache = MagicMock()
        fake_cache.server_config = {
            "111": {
                "coc_role_enabled": True,
                "clan_role_enabled": False,
                "member_clans": ["#CLAN1"],
                "member_families": [],
            }
        }
        fake_cache.user_accounts = {}
        fake_cache.clan_families = {}
        fake_cache.coc_clan_cache = fake_coc_cache

        with patch("qapbot.cache_manager.CACHE", fake_cache):
            await sync_all_roles_for_guild(guild, "111")

        fake_coc_cache.get_clan.assert_called_once_with("#CLAN1")
        assert "#CLAN1" in grm._coc_role_refreshed_clans

    async def test_bootstrap_skips_already_refreshed_clan(self):
        """Second sync should NOT re-fetch a clan already in _coc_role_refreshed_clans."""
        import qapbot.guild_role_manager as grm
        from qapbot.guild_role_manager import sync_all_roles_for_guild

        grm._coc_role_refreshed_clans.add("#CLAN1")  # simulate already-bootstrapped

        guild = _make_guild(roles=[])
        guild.chunked = True
        guild.members = []

        fake_coc_cache = MagicMock()
        fake_coc_cache.get_clan = AsyncMock()

        fake_cache = MagicMock()
        fake_cache.server_config = {
            "111": {
                "coc_role_enabled": True,
                "clan_role_enabled": False,
                "member_clans": ["#CLAN1"],
                "member_families": [],
            }
        }
        fake_cache.user_accounts = {}
        fake_cache.clan_families = {}
        fake_cache.coc_clan_cache = fake_coc_cache

        with patch("qapbot.cache_manager.CACHE", fake_cache):
            await sync_all_roles_for_guild(guild, "111")

        fake_coc_cache.get_clan.assert_not_called()


# ---------------------------------------------------------------------------
# Bounded-concurrency role sync (sync_roles_for_clan_members / sync_all_roles_for_guild)
# ---------------------------------------------------------------------------


def _make_registered_users(user_ids: list[int], clan_tag: str = "#CLAN1") -> dict[str, Any]:
    """Build a CACHE.user_accounts-shaped dict where each user has one player in clan_tag."""
    return {
        str(uid): {"players": [{"current_clan_tag": clan_tag, "verified": True}]}
        for uid in user_ids
    }


@pytest.mark.smoke
class TestSyncRolesForClanMembersConcurrency:
    """Verify sync_roles_for_clan_members() uses bounded concurrency, not a serial loop."""

    async def test_all_users_synced_exactly_once(self):
        import qapbot.guild_role_manager as grm

        user_ids = list(range(1, 13))  # 12 users
        fake_cache = MagicMock()
        fake_cache.server_config = {"111": {"coc_role_enabled": True, "clan_role_enabled": False}}
        fake_cache.user_accounts = _make_registered_users(user_ids)

        synced_ids: list[int] = []

        async def _fake_sync(guild, guild_id, discord_user_id, member=None):
            synced_ids.append(discord_user_id)
            return True

        guild = _make_guild(roles=[])

        with patch("qapbot.cache_manager.CACHE", fake_cache), \
             patch.object(grm, "sync_roles_for_user", _fake_sync):
            await grm.sync_roles_for_clan_members(guild, "111", "#CLAN1", [])

        assert sorted(synced_ids) == user_ids
        assert len(synced_ids) == len(set(synced_ids))

    async def test_concurrency_is_bounded(self):
        import asyncio as _asyncio

        import qapbot.guild_role_manager as grm

        user_ids = list(range(1, 21))  # 20 users
        fake_cache = MagicMock()
        fake_cache.server_config = {"111": {"coc_role_enabled": True, "clan_role_enabled": False}}
        fake_cache.user_accounts = _make_registered_users(user_ids)

        in_flight = 0
        max_in_flight = 0

        async def _fake_sync(guild, guild_id, discord_user_id, member=None):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await _asyncio.sleep(0.01)
            in_flight -= 1
            return True

        guild = _make_guild(roles=[])

        with patch("qapbot.cache_manager.CACHE", fake_cache), \
             patch.object(grm, "sync_roles_for_user", _fake_sync):
            await grm.sync_roles_for_clan_members(guild, "111", "#CLAN1", [])

        assert max_in_flight <= grm._ROLE_SYNC_CONCURRENCY
        assert max_in_flight > 1  # proves it's no longer a serial loop

    async def test_one_failure_does_not_abort_batch(self, caplog):
        import qapbot.guild_role_manager as grm

        user_ids = list(range(1, 8))  # 7 users
        fake_cache = MagicMock()
        fake_cache.server_config = {"111": {"coc_role_enabled": True, "clan_role_enabled": False}}
        fake_cache.user_accounts = _make_registered_users(user_ids)

        synced_ids: list[int] = []

        async def _fake_sync(guild, guild_id, discord_user_id, member=None):
            if discord_user_id == 4:
                raise RuntimeError("boom")
            synced_ids.append(discord_user_id)
            return True

        guild = _make_guild(roles=[])

        with patch("qapbot.cache_manager.CACHE", fake_cache), \
             patch.object(grm, "sync_roles_for_user", _fake_sync), \
             caplog.at_level("WARNING"):
            await grm.sync_roles_for_clan_members(guild, "111", "#CLAN1", [])

        assert sorted(synced_ids) == [uid for uid in user_ids if uid != 4]
        assert any(
            "Error syncing roles for user 4" in rec.message for rec in caplog.records
        )

    async def test_empty_user_list_short_circuits(self):
        import qapbot.guild_role_manager as grm

        fake_cache = MagicMock()
        fake_cache.server_config = {"111": {"coc_role_enabled": True, "clan_role_enabled": False}}
        fake_cache.user_accounts = {}  # no registered users in this clan

        guild = _make_guild(roles=[])

        with patch("qapbot.cache_manager.CACHE", fake_cache), \
             patch.object(grm, "sync_roles_for_user", AsyncMock()) as mock_sync:
            await grm.sync_roles_for_clan_members(guild, "111", "#CLAN1", [])

        mock_sync.assert_not_called()


@pytest.mark.smoke
class TestSyncAllRolesForGuildConcurrency:
    """Verify sync_all_roles_for_guild() also uses bounded concurrency for its member loop."""

    def _guild_with_members(self, user_ids: list[int]):
        guild = _make_guild(roles=[])
        guild.chunked = True  # skip Discord chunk() API call
        members = []
        for uid in user_ids:
            m = MagicMock()
            m.id = uid
            members.append(m)
        guild.members = members
        return guild

    async def test_all_users_synced_exactly_once(self):
        import qapbot.guild_role_manager as grm

        user_ids = list(range(1, 13))  # 12 users
        fake_cache = MagicMock()
        fake_cache.server_config = {
            "111": {
                "coc_role_enabled": True,
                "clan_role_enabled": False,
                "member_clans": ["#CLAN1"],
                "member_families": [],
            }
        }
        fake_cache.user_accounts = _make_registered_users(user_ids)
        fake_cache.clan_families = {}

        synced_ids: list[int] = []

        async def _fake_sync(guild, guild_id, discord_user_id, member=None):
            synced_ids.append(discord_user_id)
            return True

        guild = self._guild_with_members(user_ids)

        with patch("qapbot.cache_manager.CACHE", fake_cache), \
             patch.object(grm, "sync_roles_for_user", _fake_sync):
            await grm.sync_all_roles_for_guild(guild, "111")

        assert sorted(synced_ids) == user_ids
        assert len(synced_ids) == len(set(synced_ids))

    async def test_concurrency_is_bounded(self):
        import asyncio as _asyncio

        import qapbot.guild_role_manager as grm

        user_ids = list(range(1, 21))  # 20 users
        fake_cache = MagicMock()
        fake_cache.server_config = {
            "111": {
                "coc_role_enabled": True,
                "clan_role_enabled": False,
                "member_clans": ["#CLAN1"],
                "member_families": [],
            }
        }
        fake_cache.user_accounts = _make_registered_users(user_ids)
        fake_cache.clan_families = {}

        in_flight = 0
        max_in_flight = 0

        async def _fake_sync(guild, guild_id, discord_user_id, member=None):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await _asyncio.sleep(0.01)
            in_flight -= 1
            return True

        guild = self._guild_with_members(user_ids)

        with patch("qapbot.cache_manager.CACHE", fake_cache), \
             patch.object(grm, "sync_roles_for_user", _fake_sync):
            await grm.sync_all_roles_for_guild(guild, "111")

        assert max_in_flight <= grm._ROLE_SYNC_CONCURRENCY
        assert max_in_flight > 1  # proves it's no longer a serial loop

    async def test_counters_match_results(self, caplog):
        """The final 'N synced, M errors' tally must be correct once counting moves
        from in-loop increments to post-gather counting (see plan §3.4)."""
        import qapbot.guild_role_manager as grm

        user_ids = list(range(1, 8))  # 7 users
        fake_cache = MagicMock()
        fake_cache.server_config = {
            "111": {
                "coc_role_enabled": True,
                "clan_role_enabled": False,
                "member_clans": ["#CLAN1"],
                "member_families": [],
            }
        }
        fake_cache.user_accounts = _make_registered_users(user_ids)
        fake_cache.clan_families = {}

        async def _fake_sync(guild, guild_id, discord_user_id, member=None):
            if discord_user_id in (2, 5):
                raise RuntimeError("boom")
            return True

        guild = self._guild_with_members(user_ids)

        with patch("qapbot.cache_manager.CACHE", fake_cache), \
             patch.object(grm, "sync_roles_for_user", _fake_sync), \
             caplog.at_level("INFO"):
            await grm.sync_all_roles_for_guild(guild, "111")

        summary_lines = [
            rec.message for rec in caplog.records if "role sync complete" in rec.message
        ]
        assert len(summary_lines) == 1
        assert "5 synced, 2 errors" in summary_lines[0]
