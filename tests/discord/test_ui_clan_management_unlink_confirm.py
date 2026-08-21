"""Tests for ClanManagementUnlinkPlayerConfirmView._on_confirm (2026-08-21 PROD incident fix):
same missing-defer bug as UnlinkConfirmView (ui_registration.py) — must defer immediately and
use edit_original_response(), not a bare interaction.response.edit_message() after slow work.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")


def _make_view(discord_user_id="222", verified=False):
    from qapbot.ui_clan_management import ClanManagementUnlinkPlayerConfirmView

    parent_view = MagicMock()
    sent_message = MagicMock()
    sent_message.guild = None  # skips the post-success refresh block (guarded by an early return)

    return ClanManagementUnlinkPlayerConfirmView(
        player_data={"tag": "#P1", "name": "Ace", "discord_user_id": discord_user_id, "verified": verified},
        clan_tag="#CLAN1",
        sent_message=sent_message,
        guild_clans=["#CLAN1"],
        mode="registrations",
        parent_view=parent_view,
    )


def _make_interaction():
    interaction = MagicMock()
    interaction.guild = None  # skip role-sync branch — irrelevant to this fix
    interaction.user = "AdminUser"
    interaction.response = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


@pytest.mark.discord
@pytest.mark.asyncio
async def test_on_confirm_defers_before_any_slow_work(monkeypatch):
    monkeypatch.setattr("qapbot.QBdiscocmdshelper.unlink_player", AsyncMock(return_value=True))

    view = _make_view()
    interaction = _make_interaction()

    await view._on_confirm(interaction)

    interaction.response.defer.assert_awaited_once()
    _, kwargs = interaction.response.defer.call_args
    assert kwargs.get("thinking") is False
    interaction.response.edit_message.assert_not_called()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_on_confirm_success_uses_edit_original_response(monkeypatch):
    monkeypatch.setattr("qapbot.QBdiscocmdshelper.unlink_player", AsyncMock(return_value=True))

    view = _make_view()
    interaction = _make_interaction()

    await view._on_confirm(interaction)

    interaction.edit_original_response.assert_awaited_once()
    _, kwargs = interaction.edit_original_response.call_args
    assert kwargs["view"] is None


@pytest.mark.discord
@pytest.mark.asyncio
async def test_on_confirm_not_found_uses_edit_original_response(monkeypatch):
    monkeypatch.setattr("qapbot.QBdiscocmdshelper.unlink_player", AsyncMock(return_value=False))

    view = _make_view()
    interaction = _make_interaction()

    await view._on_confirm(interaction)

    interaction.response.defer.assert_awaited_once()
    interaction.edit_original_response.assert_awaited_once()
    _, kwargs = interaction.edit_original_response.call_args
    assert kwargs["view"] is None
    interaction.response.edit_message.assert_not_called()
