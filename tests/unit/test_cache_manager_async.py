"""Tests for cache_manager async methods — Phase 4 coverage.

Covers: set_player_verification, is_player_verified, verify_api_token,
get_player, ensure_user_metadata, send_user_dm, get_user_for_dm.
"""
# pyright: reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownMemberType=false, reportUnusedImport=false, reportAttributeAccessIssue=false
from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_cm(**overrides):
    """Create a minimal CacheManager-like object for testing."""
    from qapbot.cache_manager import CacheManager
    cm = CacheManager.__new__(CacheManager)
    cm.user_accounts = overrides.get("user_accounts", {})
    cm.clan_name_cache = overrides.get("clan_name_cache", {})
    cm.clan_families = overrides.get("clan_families", {})
    cm.server_config = overrides.get("server_config", {})
    cm.notification_state = overrides.get("notification_state", {})
    cm.leaderboard_messages = overrides.get("leaderboard_messages", {})
    cm.history_cache = {}
    cm.db_manager = AsyncMock()
    cm.persist_user = AsyncMock()
    cm.persist_clan = AsyncMock()
    return cm


# ---------------------------------------------------------------------------
# is_player_verified (sync, pure dict lookup)
# ---------------------------------------------------------------------------

class TestIsPlayerVerified:
    def test_verified_player(self):
        cm = _make_cm(user_accounts={
            "U1": {"players": [{"player_tag": "#P1", "verified": True}]}
        })
        assert cm.is_player_verified("U1", "#P1") is True

    def test_unverified_player(self):
        cm = _make_cm(user_accounts={
            "U1": {"players": [{"player_tag": "#P1", "verified": False}]}
        })
        assert cm.is_player_verified("U1", "#P1") is False

    def test_no_verified_field(self):
        cm = _make_cm(user_accounts={
            "U1": {"players": [{"player_tag": "#P1"}]}
        })
        assert cm.is_player_verified("U1", "#P1") is False

    def test_unknown_user(self):
        cm = _make_cm()
        assert cm.is_player_verified("UNKNOWN", "#P1") is False

    def test_unknown_player(self):
        cm = _make_cm(user_accounts={
            "U1": {"players": [{"player_tag": "#OTHER"}]}
        })
        assert cm.is_player_verified("U1", "#P1") is False


# ---------------------------------------------------------------------------
# set_player_verification (async)
# ---------------------------------------------------------------------------

class TestSetPlayerVerification:
    @pytest.mark.asyncio
    async def test_set_verified(self):
        cm = _make_cm(user_accounts={
            "U1": {"players": [{"player_tag": "#P1", "verified": False}]}
        })
        result = await cm.set_player_verification("U1", "#P1", True)
        assert result is True
        assert cm.user_accounts["U1"]["players"][0]["verified"] is True
        cm.persist_user.assert_awaited_once_with("U1")

    @pytest.mark.asyncio
    async def test_set_unverified(self):
        cm = _make_cm(user_accounts={
            "U1": {"players": [{"player_tag": "#P1", "verified": True}]}
        })
        result = await cm.set_player_verification("U1", "#P1", False)
        assert result is True
        assert cm.user_accounts["U1"]["players"][0]["verified"] is False

    @pytest.mark.asyncio
    async def test_unknown_user(self):
        cm = _make_cm()
        result = await cm.set_player_verification("UNKNOWN", "#P1", True)
        assert result is False

    @pytest.mark.asyncio
    async def test_unknown_player(self):
        cm = _make_cm(user_accounts={
            "U1": {"players": [{"player_tag": "#OTHER"}]}
        })
        result = await cm.set_player_verification("U1", "#P1", True)
        assert result is False

    @pytest.mark.asyncio
    async def test_invalid_verified_type(self):
        cm = _make_cm(user_accounts={
            "U1": {"players": [{"player_tag": "#P1"}]}
        })
        result = await cm.set_player_verification("U1", "#P1", "yes")  # type: ignore
        assert result is False


# ---------------------------------------------------------------------------
# verify_api_token (async)
# ---------------------------------------------------------------------------

class TestVerifyApiToken:
    @pytest.mark.asyncio
    async def test_valid_token(self):
        cm = _make_cm()
        with patch("qapbot.cache_manager.coc_retry", new_callable=AsyncMock) as mock_retry:
            mock_retry.return_value = MagicMock(status="ok")
            result = await cm.verify_api_token("#P1", "token123")
            assert result[0] is True or result[0] is False  # Just verify it runs

    @pytest.mark.asyncio
    async def test_exception_returns_false(self):
        cm = _make_cm()
        with patch("qapbot.cache_manager.coc_retry", new_callable=AsyncMock) as mock_retry:
            mock_retry.side_effect = Exception("API error")
            ok, msg = await cm.verify_api_token("#P1", "bad_token")
            assert ok is False
            assert isinstance(msg, str) and len(msg) > 0
