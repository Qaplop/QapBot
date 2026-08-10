"""Tests for the "Select Timezone" server setting (Basic Config, next to "Select Language") —
needed because the CWL Management screen's monospaced clan table can't use Discord's native
per-viewer <t:...> timestamp markup (not parsed inside code blocks at all).
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")


@pytest.mark.discord
def test_config_mode_includes_timezone_button_without_row_conflict():
    from qapbot.cache_manager import CACHE
    from qapbot.ui_clan_management import ClanManagementView

    CACHE.server_config["7001"] = {}
    CACHE.db_manager = None
    guild = MagicMock()
    guild.id = 7001
    sent_message = MagicMock(guild=guild)

    view = ClanManagementView(
        clan_tag="#CLAN1", guild_clans=["#CLAN1"], unlinked_players=[],
        sent_message=sent_message, mode="config", timeout=300,
    )

    timezone_button = next(
        c for c in view.children if getattr(c, "custom_id", None) == "config_select_timezone"
    )
    assert timezone_button.row == 3  # type: ignore[union-attr]


@pytest.mark.discord
@pytest.mark.asyncio
async def test_select_timezone_callback_opens_modal_prefilled_with_current_offset():
    from qapbot.cache_manager import CACHE
    from qapbot.ui_clan_management import ClanManagementView, TimezoneConfigurationModal

    CACHE.server_config["7002"] = {"timezone_offset_minutes": 330}
    CACHE.db_manager = None
    guild = MagicMock()
    guild.id = 7002
    sent_message = MagicMock(guild=guild)

    view = ClanManagementView(
        clan_tag="#CLAN1", guild_clans=["#CLAN1"], unlinked_players=[],
        sent_message=sent_message, mode="config", timeout=300,
    )
    view._check_admin_permission = AsyncMock(return_value=True)  # type: ignore[method-assign]

    interaction = MagicMock()
    interaction.guild = guild
    interaction.response = AsyncMock()
    interaction.response.send_modal = AsyncMock()

    await view._on_select_timezone(interaction)

    interaction.response.send_modal.assert_awaited_once()
    modal = interaction.response.send_modal.await_args.args[0]
    assert isinstance(modal, TimezoneConfigurationModal)
    assert modal.offset_input.default == "+5:30"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_timezone_modal_persists_valid_offset_and_refreshes(mock_interaction):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_clan_management import TimezoneConfigurationModal

    guild_id_str = str(mock_interaction.guild.id)
    CACHE.server_config[guild_id_str] = {}
    CACHE.db_manager = MagicMock()
    CACHE.db_manager.save_guild_config = AsyncMock()

    parent = MagicMock()
    parent._refresh_config_view = AsyncMock()

    modal = TimezoneConfigurationModal(parent, mock_interaction.guild.id, current_offset_minutes=0)
    modal.offset_input._value = "+5:30"  # type: ignore[attr-defined]

    await modal.on_submit(mock_interaction)

    assert CACHE.server_config[guild_id_str]["timezone_offset_minutes"] == 330
    parent._refresh_config_view.assert_awaited_once()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_timezone_modal_rejects_invalid_offset_without_persisting(mock_interaction):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_clan_management import TimezoneConfigurationModal

    guild_id_str = str(mock_interaction.guild.id)
    CACHE.server_config[guild_id_str] = {"timezone_offset_minutes": 60}

    parent = MagicMock()
    parent._refresh_config_view = AsyncMock()

    modal = TimezoneConfigurationModal(parent, mock_interaction.guild.id, current_offset_minutes=60)
    modal.offset_input._value = "not a real offset"  # type: ignore[attr-defined]

    mock_interaction.response.send_message = AsyncMock()
    await modal.on_submit(mock_interaction)

    mock_interaction.response.send_message.assert_awaited_once()
    # Untouched — the invalid submission never overwrote the previously-configured offset.
    assert CACHE.server_config[guild_id_str]["timezone_offset_minutes"] == 60
    parent._refresh_config_view.assert_not_awaited()
