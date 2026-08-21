"""Tests for the "Custodians" button (tracker item #5): per-clan war-notification @mention
configuration, added to ClanManagementView's "notifications" mode next to the existing
"Clan Settings"/"User Settings" buttons.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")


def _make_notifications_view(guild):
    from qapbot.ui_clan_management import ClanManagementView

    sent_message = MagicMock(guild=guild)
    return ClanManagementView(
        clan_tag="#CLAN1", guild_clans=["#CLAN1"], unlinked_players=[],
        sent_message=sent_message, mode="notifications", timeout=300,
    )


@pytest.mark.discord
def test_notifications_mode_includes_custodians_button():
    guild = MagicMock()
    guild.id = 8001
    view = _make_notifications_view(guild)

    custodians_button = next(
        (c for c in view.children if getattr(c, "custom_id", None) == "clan_mgmt_custodians"), None
    )
    assert custodians_button is not None
    assert custodians_button.style == discord.ButtonStyle.primary  # type: ignore[union-attr]


@pytest.mark.discord
def test_roles_mode_has_no_custodians_button():
    from qapbot.cache_manager import CACHE
    from qapbot.ui_clan_management import ClanManagementView

    CACHE.server_config["8002"] = {}
    guild = MagicMock()
    guild.id = 8002
    sent_message = MagicMock(guild=guild)

    view = ClanManagementView(
        clan_tag="#CLAN1", guild_clans=["#CLAN1"], unlinked_players=[],
        sent_message=sent_message, mode="roles", timeout=300,
    )

    assert all(getattr(c, "custom_id", None) != "clan_mgmt_custodians" for c in view.children)


@pytest.mark.discord
@pytest.mark.asyncio
async def test_on_custodians_opens_view_seeded_from_cache():
    from qapbot.cache_manager import CACHE
    from qapbot.ui_clan_management import CustodianConfigurationView

    guild = MagicMock()
    guild.id = 8003
    CACHE.server_config["8003"] = {"clan_custodians": {"#CLAN1": ["111", "222"]}}

    view = _make_notifications_view(guild)
    view._check_admin_permission = AsyncMock(return_value=True)  # type: ignore[method-assign]

    interaction = MagicMock()
    interaction.guild = guild
    interaction.response = AsyncMock()
    interaction.followup = AsyncMock()
    interaction.followup.send = AsyncMock(return_value=MagicMock())

    await view._on_custodians(interaction)

    interaction.followup.send.assert_awaited_once()
    _, kwargs = interaction.followup.send.call_args
    custodians_view = kwargs["view"]
    assert isinstance(custodians_view, CustodianConfigurationView)
    assert custodians_view.custodian_ids == ["111", "222"]
    assert custodians_view.clan_tag == "#CLAN1"


@pytest.mark.discord
def test_user_select_enforces_cap_of_five():
    from qapbot.ui_clan_management import CustodianConfigurationView, CUSTODIAN_LIMIT

    guild = MagicMock()
    guild.id = 8004
    view = CustodianConfigurationView(guild=guild, clan_tag="#CLAN1", current_custodian_ids=[])

    user_select = next(c for c in view.children if isinstance(c, discord.ui.UserSelect))
    assert CUSTODIAN_LIMIT == 5
    assert user_select.max_values == 5
    assert user_select.min_values == 0


@pytest.mark.discord
@pytest.mark.asyncio
async def test_user_select_callback_updates_state_and_rebuilds(mock_interaction):
    from qapbot.ui_clan_management import CustodianConfigurationView

    view = CustodianConfigurationView(guild=mock_interaction.guild, clan_tag="#CLAN1", current_custodian_ids=[])
    mock_interaction.data = {"values": ["333", "444"]}
    mock_interaction.edit_original_response = AsyncMock()

    await view._on_user_select(mock_interaction)

    assert view.custodian_ids == ["333", "444"]
    mock_interaction.edit_original_response.assert_awaited_once()
    _, kwargs = mock_interaction.edit_original_response.call_args
    assert "333" in kwargs["content"]
    assert "444" in kwargs["content"]


@pytest.mark.discord
@pytest.mark.asyncio
async def test_clear_button_empties_selection(mock_interaction):
    from qapbot.ui_clan_management import CustodianConfigurationView

    view = CustodianConfigurationView(guild=mock_interaction.guild, clan_tag="#CLAN1", current_custodian_ids=["111"])
    mock_interaction.edit_original_response = AsyncMock()

    await view._on_clear(mock_interaction)

    assert view.custodian_ids == []
    clear_button = next(c for c in view.children if getattr(c, "custom_id", None) == "clear_custodians")
    assert clear_button.disabled is True  # type: ignore[union-attr]


@pytest.mark.discord
@pytest.mark.asyncio
async def test_apply_persists_and_updates_cache(mock_interaction):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_clan_management import CustodianConfigurationView

    guild_id_str = str(mock_interaction.guild.id)
    CACHE.server_config[guild_id_str] = {}
    CACHE.db_manager = MagicMock()
    CACHE.db_manager.save_guild_clan_custodians = AsyncMock()

    view = CustodianConfigurationView(guild=mock_interaction.guild, clan_tag="#CLAN1", current_custodian_ids=["111", "222"])

    await view._on_apply(mock_interaction)

    CACHE.db_manager.save_guild_clan_custodians.assert_awaited_once_with(guild_id_str, "#CLAN1", ["111", "222"])
    assert CACHE.server_config[guild_id_str]["clan_custodians"]["#CLAN1"] == ["111", "222"]
    mock_interaction.followup.send.assert_awaited_once()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_apply_with_empty_selection_clears_cache_entry(mock_interaction):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_clan_management import CustodianConfigurationView

    guild_id_str = str(mock_interaction.guild.id)
    CACHE.server_config[guild_id_str] = {"clan_custodians": {"#CLAN1": ["111"]}}
    CACHE.db_manager = MagicMock()
    CACHE.db_manager.save_guild_clan_custodians = AsyncMock()

    view = CustodianConfigurationView(guild=mock_interaction.guild, clan_tag="#CLAN1", current_custodian_ids=[])

    await view._on_apply(mock_interaction)

    CACHE.db_manager.save_guild_clan_custodians.assert_awaited_once_with(guild_id_str, "#CLAN1", [])
    assert "#CLAN1" not in CACHE.server_config[guild_id_str]["clan_custodians"]
