"""Tests for UnlinkConfirmView._on_confirm (2026-08-21 PROD incident fix): the handler used to
call interaction.response.edit_message() directly after slow work (unlink_player() + role
sync), which under load blew Discord's 3s interaction-ack window and threw "Unknown interaction"
(10062). It must now defer first and use edit_original_response() for every branch.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")


def _make_view(player_tag="#P1", player_name="Ace"):
    from qapbot.ui_registration import UnlinkConfirmView

    parent_view = MagicMock()
    parent_view._build_message_content = MagicMock(return_value="parent overview")

    return UnlinkConfirmView(
        player_data={"player_tag": player_tag, "player_name": player_name},
        user_id="111",
        guild_id=987654321,
        parent_view=parent_view,
    )


def _make_interaction():
    interaction = MagicMock()
    interaction.guild = None  # skip role-sync branch — irrelevant to this fix
    interaction.user = "TestUser"
    interaction.response = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


@pytest.mark.discord
@pytest.mark.asyncio
async def test_on_confirm_defers_before_any_slow_work(monkeypatch):
    from qapbot.cache_manager import CACHE

    monkeypatch.setattr("qapbot.QBdiscocmdshelper.unlink_player", AsyncMock(return_value=True))
    monkeypatch.setattr(CACHE, "user_accounts", {"111": {"players": []}})

    view = _make_view()
    interaction = _make_interaction()

    await view._on_confirm(interaction)

    interaction.response.defer.assert_awaited_once()
    _, kwargs = interaction.response.defer.call_args
    assert kwargs.get("thinking") is False
    interaction.response.edit_message.assert_not_called()  # the old, non-deferred call path


@pytest.mark.discord
@pytest.mark.asyncio
async def test_on_confirm_success_no_remaining_accounts_uses_edit_original_response(monkeypatch):
    from qapbot.cache_manager import CACHE

    monkeypatch.setattr("qapbot.QBdiscocmdshelper.unlink_player", AsyncMock(return_value=True))
    monkeypatch.setattr(CACHE, "user_accounts", {"111": {"players": []}})

    view = _make_view()
    interaction = _make_interaction()

    await view._on_confirm(interaction)

    interaction.edit_original_response.assert_awaited_once()
    _, kwargs = interaction.edit_original_response.call_args
    assert kwargs["view"] is None


@pytest.mark.discord
@pytest.mark.asyncio
async def test_on_confirm_player_not_found_uses_edit_original_response(monkeypatch):
    monkeypatch.setattr("qapbot.QBdiscocmdshelper.unlink_player", AsyncMock(return_value=False))

    view = _make_view()
    interaction = _make_interaction()

    await view._on_confirm(interaction)

    interaction.response.defer.assert_awaited_once()
    interaction.edit_original_response.assert_awaited_once()
    _, kwargs = interaction.edit_original_response.call_args
    assert kwargs["view"] is view.parent_view
    interaction.response.edit_message.assert_not_called()
