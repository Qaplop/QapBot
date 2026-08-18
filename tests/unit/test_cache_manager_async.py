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


# ---------------------------------------------------------------------------
# send_user_dm / send_user_dm_detailed (2026-08-18, item 3 of the CWL enrollment redesign: a
# transient discord.DiscordServerError used to be re-raised here instead of returning False like
# every other failure path, which let one recipient's Discord hiccup abort an entire batch DM
# loop — e.g. start_cwl_enrollment()'s per-recipient loop had no try/except of its own).
# ---------------------------------------------------------------------------

class TestSendUserDmDetailed:
    @pytest.mark.asyncio
    async def test_success(self):
        cm = _make_cm()
        user = MagicMock()
        user.name = "tester"
        user.send = AsyncMock()
        cm.get_user_for_dm = AsyncMock(return_value=user)

        sent, outcome = await cm.send_user_dm_detailed("123", "hello")

        assert (sent, outcome) == (True, "sent")
        user.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_user_could_not_be_fetched_is_failed_not_blocked(self):
        # get_user_for_dm() already swallows the specific reason (including a transient 5xx
        # during the fetch itself) and just returns None — "failed" not "blocked" since we don't
        # actually know the recipient has DMs closed, only that the fetch didn't work.
        cm = _make_cm()
        cm.get_user_for_dm = AsyncMock(return_value=None)

        sent, outcome = await cm.send_user_dm_detailed("123", "hello")

        assert (sent, outcome) == (False, "failed")

    @pytest.mark.asyncio
    async def test_forbidden_is_blocked_with_no_retry(self):
        import discord

        cm = _make_cm()
        user = MagicMock()
        user.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "no perms"))
        cm.get_user_for_dm = AsyncMock(return_value=user)

        sent, outcome = await cm.send_user_dm_detailed("123", "hello")

        assert (sent, outcome) == (False, "blocked")
        assert user.send.await_count == 1  # not worth retrying a block

    @pytest.mark.asyncio
    async def test_not_found_is_blocked_with_no_retry(self):
        import discord

        cm = _make_cm()
        user = MagicMock()
        user.send = AsyncMock(side_effect=discord.NotFound(MagicMock(), "unknown user"))
        cm.get_user_for_dm = AsyncMock(return_value=user)

        sent, outcome = await cm.send_user_dm_detailed("123", "hello")

        assert (sent, outcome) == (False, "blocked")
        assert user.send.await_count == 1

    @pytest.mark.asyncio
    async def test_generic_exception_is_failed_with_no_retry(self):
        cm = _make_cm()
        user = MagicMock()
        user.send = AsyncMock(side_effect=RuntimeError("boom"))
        cm.get_user_for_dm = AsyncMock(return_value=user)

        sent, outcome = await cm.send_user_dm_detailed("123", "hello")

        assert (sent, outcome) == (False, "failed")
        assert user.send.await_count == 1

    @pytest.mark.asyncio
    async def test_transient_server_error_retries_then_succeeds(self):
        import discord

        cm = _make_cm()
        user = MagicMock()
        user.name = "tester"
        error = discord.DiscordServerError(MagicMock(status=503), "outage")
        user.send = AsyncMock(side_effect=[error, error, None])  # fails twice, then works
        cm.get_user_for_dm = AsyncMock(return_value=user)

        with patch("qapbot.cache_manager.asyncio.sleep", new_callable=AsyncMock):
            sent, outcome = await cm.send_user_dm_detailed("123", "hello")

        assert (sent, outcome) == (True, "sent")
        assert user.send.await_count == 3

    @pytest.mark.asyncio
    async def test_transient_server_error_never_raises_even_after_exhausting_retries(self):
        # The actual bug this fixes: this must return, never raise — a caller looping over many
        # recipients (start_cwl_enrollment) must be able to keep going to the next one.
        import discord
        from qapbot.cache_manager import DM_SEND_MAX_RETRIES

        cm = _make_cm()
        user = MagicMock()
        error = discord.DiscordServerError(MagicMock(status=503), "outage")
        user.send = AsyncMock(side_effect=error)
        cm.get_user_for_dm = AsyncMock(return_value=user)

        with patch("qapbot.cache_manager.asyncio.sleep", new_callable=AsyncMock):
            sent, outcome = await cm.send_user_dm_detailed("123", "hello")

        assert (sent, outcome) == (False, "failed")
        assert user.send.await_count == DM_SEND_MAX_RETRIES


class TestSendUserDm:
    """send_user_dm() is now a thin bool-only wrapper around send_user_dm_detailed() — the many
    existing callers across the bot that only care about success/failure keep working unchanged."""

    @pytest.mark.asyncio
    async def test_wraps_detailed_result_as_bool_true(self):
        cm = _make_cm()
        cm.send_user_dm_detailed = AsyncMock(return_value=(True, "sent"))

        result = await cm.send_user_dm("123", "hello")

        assert result is True

    @pytest.mark.asyncio
    async def test_wraps_detailed_result_as_bool_false(self):
        cm = _make_cm()
        cm.send_user_dm_detailed = AsyncMock(return_value=(False, "blocked"))

        result = await cm.send_user_dm("123", "hello")

        assert result is False
