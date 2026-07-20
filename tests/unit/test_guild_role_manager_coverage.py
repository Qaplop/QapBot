"""Tests for qapbot/guild_role_manager.py — Phase 3 coverage.

Covers: normalize_discord_role_name, _get_config, get_coc_ingame_role_ids,
_get_clan_tags_for_user, _get_highest_coc_role_for_user,
get_coc_roles_to_delete, get_clan_roles_to_delete.
"""
# pyright: reportUnknownMemberType=false, reportUnknownParameterType=false
# pyright: reportMissingParameterType=false, reportUnknownLambdaType=false
# pyright: reportPrivateUsage=false, reportUnusedImport=false
from __future__ import annotations

from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest


def _mock_cache(**overrides) -> MagicMock:
    cache = MagicMock()
    cache.server_config = overrides.get("server_config", {})
    cache.user_accounts = overrides.get("user_accounts", {})
    cache.clan_families = overrides.get("clan_families", {})
    cache.get_clan_name = MagicMock(side_effect=lambda tag, default=None: default or tag)
    return cache


# ---------------------------------------------------------------------------
# normalize_discord_role_name
# ---------------------------------------------------------------------------

class TestNormalizeDiscordRoleName:
    def _fn(self):
        from qapbot.guild_role_manager import normalize_discord_role_name
        return normalize_discord_role_name

    def test_plain_name(self):
        assert self._fn()("Warriors") == "Warriors"

    def test_removes_special_chars(self):
        result = self._fn()("Test@#$%Clan!")
        assert "@" not in result
        assert "#" not in result
        assert "!" not in result

    def test_keeps_apostrophe_hyphen(self):
        result = self._fn()("Night's Watch - Elite")
        assert "'" in result
        assert "-" in result

    def test_collapses_whitespace(self):
        result = self._fn()("Too   Many   Spaces")
        assert "  " not in result
        assert result == "Too Many Spaces"

    def test_truncates_to_100(self):
        result = self._fn()("A" * 200)
        assert len(result) == 100

    def test_strips_leading_trailing(self):
        result = self._fn()("  Hello  ")
        assert result == "Hello"

    def test_unicode_preserved(self):
        result = self._fn()("Ünïcödé Çlàn")
        assert "Ü" in result

    def test_empty_string(self):
        result = self._fn()("")
        assert result == ""


# ---------------------------------------------------------------------------
# _get_config
# ---------------------------------------------------------------------------

class TestGetConfig:
    def _fn(self):
        from qapbot.guild_role_manager import _get_config
        return _get_config

    def test_returns_config(self, monkeypatch):
        cache = _mock_cache(server_config={"123": {"key": "value"}})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        result = self._fn()("123")
        assert result == {"key": "value"}

    def test_missing_guild_returns_empty(self, monkeypatch):
        cache = _mock_cache(server_config={})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        result = self._fn()("999")
        assert result == {}


# ---------------------------------------------------------------------------
# get_coc_ingame_role_ids
# ---------------------------------------------------------------------------

class TestGetCocIngameRoleIds:
    def _fn(self):
        from qapbot.guild_role_manager import get_coc_ingame_role_ids
        return get_coc_ingame_role_ids

    def test_all_roles_set(self, monkeypatch):
        config = {
            "coc_role_member_id": "111",
            "coc_role_elder_id": "222",
            "coc_role_coleader_id": "333",
            "coc_role_leader_id": "444",
        }
        cache = _mock_cache(server_config={"G1": config})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        result = self._fn()("G1")
        assert result["member"] == "111"
        assert result["elder"] == "222"
        assert result["coLeader"] == "333"
        assert result["leader"] == "444"

    def test_missing_roles_return_none(self, monkeypatch):
        cache = _mock_cache(server_config={"G1": {}})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        result = self._fn()("G1")
        assert all(v is None for v in result.values())

    def test_partial_config(self, monkeypatch):
        config = {"coc_role_leader_id": "999"}
        cache = _mock_cache(server_config={"G1": config})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        result = self._fn()("G1")
        assert result["leader"] == "999"
        assert result["member"] is None


# ---------------------------------------------------------------------------
# _get_clan_tags_for_user
# ---------------------------------------------------------------------------

class TestGetClanTagsForUser:
    def _fn(self):
        from qapbot.guild_role_manager import _get_clan_tags_for_user
        return _get_clan_tags_for_user

    def test_no_user(self, monkeypatch):
        cache = _mock_cache(user_accounts={})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        assert self._fn()("unknown") == []

    def test_single_player(self, monkeypatch):
        cache = _mock_cache(user_accounts={
            "U1": {"players": [{"current_clan_tag": "#C1"}]}
        })
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        assert self._fn()("U1") == ["#C1"]

    def test_deduplicates(self, monkeypatch):
        cache = _mock_cache(user_accounts={
            "U1": {"players": [
                {"current_clan_tag": "#C1"},
                {"current_clan_tag": "#C1"},
                {"current_clan_tag": "#C2"},
            ]}
        })
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        result = self._fn()("U1")
        assert result == ["#C1", "#C2"]

    def test_skips_non_dict(self, monkeypatch):
        cache = _mock_cache(user_accounts={
            "U1": {"players": ["invalid", {"current_clan_tag": "#C1"}]}
        })
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        assert self._fn()("U1") == ["#C1"]

    def test_skips_empty_clan_tag(self, monkeypatch):
        cache = _mock_cache(user_accounts={
            "U1": {"players": [{"current_clan_tag": ""}, {"current_clan_tag": None}]}
        })
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        assert self._fn()("U1") == []


# ---------------------------------------------------------------------------
# _get_highest_coc_role_for_user
# ---------------------------------------------------------------------------

class TestGetHighestCocRoleForUser:
    def _fn(self):
        from qapbot.guild_role_manager import _get_highest_coc_role_for_user
        return _get_highest_coc_role_for_user

    def test_no_user(self, monkeypatch):
        cache = _mock_cache(user_accounts={})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        assert self._fn()("unknown") is None

    def test_leader_wins(self, monkeypatch):
        cache = _mock_cache(user_accounts={
            "U1": {"players": [
                {"coc_role": "member", "current_clan_tag": "#C1"},
                {"coc_role": "leader", "current_clan_tag": "#C1"},
            ]}
        })
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        assert self._fn()("U1") == "leader"

    def test_guild_clans_filter(self, monkeypatch):
        cache = _mock_cache(user_accounts={
            "U1": {"players": [
                {"coc_role": "leader", "current_clan_tag": "#OTHER"},
                {"coc_role": "elder", "current_clan_tag": "#C1"},
            ]}
        })
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        result = self._fn()("U1", guild_clans={"#C1"})
        assert result == "elder"

    def test_unknown_role_ignored(self, monkeypatch):
        cache = _mock_cache(user_accounts={
            "U1": {"players": [{"coc_role": "admin", "current_clan_tag": "#C1"}]}
        })
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        assert self._fn()("U1") is None

    def test_no_coc_role_field(self, monkeypatch):
        cache = _mock_cache(user_accounts={
            "U1": {"players": [{"current_clan_tag": "#C1"}]}
        })
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        assert self._fn()("U1") is None

    def test_non_dict_player_skipped(self, monkeypatch):
        cache = _mock_cache(user_accounts={
            "U1": {"players": ["invalid", {"coc_role": "elder", "current_clan_tag": "#C1"}]}
        })
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        assert self._fn()("U1") == "elder"


# ---------------------------------------------------------------------------
# get_coc_roles_to_delete
# ---------------------------------------------------------------------------

class TestGetCocRolesToDelete:
    def _fn(self):
        from qapbot.guild_role_manager import get_coc_roles_to_delete
        return get_coc_roles_to_delete

    def test_returns_configured_roles(self, monkeypatch):
        config = {"coc_role_leader_id": "999", "coc_role_member_id": "888"}
        cache = _mock_cache(server_config={"G1": config})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)

        guild = MagicMock()
        role_obj = MagicMock()
        guild.get_role = MagicMock(return_value=role_obj)

        result = self._fn()(guild, "G1")
        assert len(result) == 2
        # Each tuple: (coc_role_key, display_name, role_obj)
        keys = [r[0] for r in result]
        assert "leader" in keys
        assert "member" in keys

    def test_no_roles_configured(self, monkeypatch):
        cache = _mock_cache(server_config={"G1": {}})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        guild = MagicMock()
        result = self._fn()(guild, "G1")
        assert result == []


# ---------------------------------------------------------------------------
# get_clan_roles_to_delete
# ---------------------------------------------------------------------------

class TestGetClanRolesToDelete:
    def _fn(self):
        from qapbot.guild_role_manager import get_clan_roles_to_delete
        return get_clan_roles_to_delete

    def test_returns_clan_roles(self, monkeypatch):
        config = {"clan_roles": {"#C1": "111", "#C2": "222"}}
        cache = _mock_cache(server_config={"G1": config})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)

        role_obj = MagicMock()
        guild = MagicMock()
        guild.get_role = MagicMock(return_value=role_obj)

        result = self._fn()(guild, "G1")
        assert len(result) == 2
        tags = [r[0] for r in result]
        assert "#C1" in tags
        assert "#C2" in tags

    def test_no_clan_roles(self, monkeypatch):
        cache = _mock_cache(server_config={"G1": {}})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        guild = MagicMock()
        result = self._fn()(guild, "G1")
        assert result == []

    def test_role_not_found_returns_none(self, monkeypatch):
        config = {"clan_roles": {"#C1": "111"}}
        cache = _mock_cache(server_config={"G1": config})
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)

        guild = MagicMock()
        guild.get_role = MagicMock(return_value=None)

        result = self._fn()(guild, "G1")
        assert len(result) == 1
        assert result[0][2] is None  # role is None
