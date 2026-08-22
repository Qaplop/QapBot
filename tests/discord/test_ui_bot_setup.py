"""Tests for the `/admin -> Bot Setup` tracker-channel configuration screen
(BUG_FEATURE_TRACKER_PLAN.md Phase 2): qapbot.ui_tracker.BotSetupView + start_bot_setup().
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")

from qapbot.ui_tracker import (
    TRACKER_SETTING_BUG_CHANNEL,
    TRACKER_SETTING_DONE_TESTING_CHANNEL,
    TRACKER_SETTING_ENABLED,
    TRACKER_SETTING_GUILD_ID,
    TRACKER_SETTING_IMPLEMENTED_CHANNEL,
    TRACKER_SETTING_TEST_CHANNEL,
    BotSetupView,
    start_bot_setup,
)

ADMIN_ID = "123456789"  # matches tests/conftest.py's mock_interaction.user.id


def _make_guild(channels_by_id=None):
    guild = MagicMock()
    guild.id = 987654321
    channels_by_id = channels_by_id or {}

    def get_channel(channel_id):
        return channels_by_id.get(int(channel_id))
    guild.get_channel.side_effect = get_channel
    return guild


def _make_view(guild=None, current_channel_ids=None, tracker_enabled=True, server_admin=ADMIN_ID):
    guild = guild or _make_guild()
    return BotSetupView(
        guild=guild,
        server_admin=server_admin,
        current_channel_ids=current_channel_ids or {},
        tracker_enabled=tracker_enabled,
        user_id=ADMIN_ID,
    )


# -- interaction_check (admin gate) --------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_interaction_check_allows_configured_admin(mock_interaction):
    view = _make_view(server_admin=ADMIN_ID)
    mock_interaction.user.id = int(ADMIN_ID)
    assert await view.interaction_check(mock_interaction) is True
    mock_interaction.response.send_message.assert_not_awaited()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_interaction_check_rejects_non_admin(mock_interaction):
    view = _make_view(server_admin=ADMIN_ID)
    mock_interaction.user.id = 999999999  # not the configured admin
    assert await view.interaction_check(mock_interaction) is False
    mock_interaction.response.send_message.assert_awaited_once()
    _, kwargs = mock_interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True


# -- channel select persistence (Cardinal Rule 8) ------------------------

@pytest.mark.discord
def test_channel_select_prefilled_with_current_channel():
    """Reopening the view must show the already-configured channel as selected, not blank."""
    bug_channel = MagicMock(spec=discord.TextChannel)
    bug_channel.id = 111
    guild = _make_guild({111: bug_channel})

    view = _make_view(guild=guild, current_channel_ids={"bug": "111"})

    bug_select = next(item for item in view.children if getattr(item, "custom_id", None) == "tracker_setup_channel_bug")
    assert [dv.id for dv in bug_select.default_values] == [bug_channel.id]

    slot_custom_ids = {
        item.custom_id for item in view.children
        if isinstance(item, discord.ui.ChannelSelect)
    }
    assert slot_custom_ids == {
        "tracker_setup_channel_bug", "tracker_setup_channel_test",
        "tracker_setup_channel_implemented", "tracker_setup_channel_done_testing",
    }


@pytest.mark.discord
@pytest.mark.asyncio
async def test_select_callback_updates_selection_and_refreshes_header(mock_interaction):
    new_channel = MagicMock(spec=discord.TextChannel)
    new_channel.id = 222
    new_channel.mention = "<#222>"
    guild = _make_guild({222: new_channel})
    view = _make_view(guild=guild)
    view.message = AsyncMock()

    mock_interaction.data = {
        "values": ["222"],
        "resolved": {"channels": {"222": {"id": "222"}}},
    }

    bug_select = next(item for item in view.children if getattr(item, "custom_id", None) == "tracker_setup_channel_bug")
    await bug_select.callback(mock_interaction)

    assert view.selected_channel_ids["bug"] == "222"
    view.message.edit.assert_awaited_once()
    _, kwargs = view.message.edit.call_args
    assert "<#222>" in kwargs["content"]


# -- format_header ---------------------------------------------------------

@pytest.mark.discord
def test_format_header_shows_not_set_and_disabled_status():
    view = _make_view(tracker_enabled=False)
    header = view.format_header()
    assert "Not set" in header
    assert "Disabled" in header


@pytest.mark.discord
def test_format_header_shows_configured_channel_and_enabled_status():
    bug_channel = MagicMock(spec=discord.TextChannel)
    bug_channel.id = 111
    bug_channel.mention = "<#111>"
    guild = _make_guild({111: bug_channel})
    view = _make_view(guild=guild, current_channel_ids={"bug": "111"}, tracker_enabled=True)
    header = view.format_header()
    assert "<#111>" in header
    assert "Enabled" in header


@pytest.mark.discord
def test_format_header_shows_all_four_configured_channels():
    channels = {}
    for cid in (111, 222, 333, 444):
        ch = MagicMock(spec=discord.TextChannel)
        ch.id = cid
        ch.mention = f"<#{cid}>"
        channels[cid] = ch
    guild = _make_guild(channels)
    view = _make_view(guild=guild, current_channel_ids={
        "bug": "111", "test": "222", "implemented": "333", "done_testing": "444",
    })
    header = view.format_header()
    for cid in (111, 222, 333, 444):
        assert f"<#{cid}>" in header


# -- Save --------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_on_save_persists_every_configured_channel_and_guild_id(mock_interaction, monkeypatch):
    from qapbot.cache_manager import CACHE

    set_setting = AsyncMock()
    monkeypatch.setattr(CACHE, "set_tracker_setting", set_setting)

    guild = _make_guild()
    guild.id = 555
    view = _make_view(guild=guild, current_channel_ids={"bug": "111"})
    view.message = AsyncMock()

    await view._on_save(mock_interaction)

    calls = {call.args[0]: call.args[1] for call in set_setting.await_args_list}
    assert calls[TRACKER_SETTING_BUG_CHANNEL] == "111"
    assert TRACKER_SETTING_TEST_CHANNEL not in calls  # never configured, never written
    assert TRACKER_SETTING_IMPLEMENTED_CHANNEL not in calls
    assert TRACKER_SETTING_DONE_TESTING_CHANNEL not in calls
    assert calls[TRACKER_SETTING_GUILD_ID] == "555"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_on_save_persists_implemented_and_done_testing_channels(mock_interaction, monkeypatch):
    from qapbot.cache_manager import CACHE

    set_setting = AsyncMock()
    monkeypatch.setattr(CACHE, "set_tracker_setting", set_setting)

    guild = _make_guild()
    view = _make_view(guild=guild, current_channel_ids={"implemented": "333", "done_testing": "444"})
    view.message = AsyncMock()

    await view._on_save(mock_interaction)

    calls = {call.args[0]: call.args[1] for call in set_setting.await_args_list}
    assert calls[TRACKER_SETTING_IMPLEMENTED_CHANNEL] == "333"
    assert calls[TRACKER_SETTING_DONE_TESTING_CHANNEL] == "444"


# -- Toggle enabled/disabled --------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_on_toggle_enabled_flips_state_and_persists(mock_interaction, monkeypatch):
    from qapbot.cache_manager import CACHE

    set_setting = AsyncMock()
    monkeypatch.setattr(CACHE, "set_tracker_setting", set_setting)

    view = _make_view(tracker_enabled=True)
    view.message = AsyncMock()

    await view._on_toggle_enabled(mock_interaction)

    assert view.tracker_enabled is False
    set_setting.assert_awaited_once_with(TRACKER_SETTING_ENABLED, "0")

    await view._on_toggle_enabled(mock_interaction)
    assert view.tracker_enabled is True
    set_setting.assert_awaited_with(TRACKER_SETTING_ENABLED, "1")


# -- Close -----------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_on_close_deletes_message(mock_interaction):
    view = _make_view()
    message = AsyncMock()
    view.message = message
    await view._on_close(mock_interaction)
    message.delete.assert_awaited_once()
    assert view.message is None


# -- start_bot_setup entry point ---------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_start_bot_setup_sends_view_seeded_from_cache_settings(mock_interaction, monkeypatch):
    from qapbot.cache_manager import CACHE

    bug_channel = MagicMock(spec=discord.TextChannel)
    bug_channel.id = 111
    bug_channel.mention = "<#111>"
    mock_interaction.guild = _make_guild({111: bug_channel})
    mock_interaction.guild.id = 987654321
    mock_interaction.user.id = int(ADMIN_ID)

    monkeypatch.setattr(CACHE, "tracker_settings", {
        TRACKER_SETTING_BUG_CHANNEL: "111",
        TRACKER_SETTING_ENABLED: "0",
    })

    await start_bot_setup(mock_interaction)

    mock_interaction.followup.send.assert_awaited_once()
    args, kwargs = mock_interaction.followup.send.call_args
    assert "<#111>" in args[0]
    assert kwargs["ephemeral"] is True
    view = kwargs["view"]
    assert isinstance(view, BotSetupView)
    assert view.tracker_enabled is False
