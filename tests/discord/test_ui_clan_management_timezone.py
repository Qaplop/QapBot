"""Tests for the "Select Timezone" server setting (Basic Config, next to "Select Language") —
needed because the CWL Management screen's monospaced clan table can't use Discord's native
per-viewer <t:...> timestamp markup (not parsed inside code blocks at all). A real IANA zone
picker (via stdlib zoneinfo) rather than a raw UTC offset, so European DST transitions shift
the displayed time correctly.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

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
def test_common_timezones_list_is_valid_and_within_select_cap():
    """Every entry must be a real IANA zone zoneinfo can load, and the list must fit Discord's
    25-option Select cap."""
    from zoneinfo import ZoneInfo
    from qapbot.ui_clan_management import COMMON_TIMEZONES

    assert 0 < len(COMMON_TIMEZONES) <= 25
    for tz_id, label in COMMON_TIMEZONES:
        ZoneInfo(tz_id)  # raises ZoneInfoNotFoundError if invalid — the test itself is the check
        assert label


@pytest.mark.discord
@pytest.mark.asyncio
async def test_select_timezone_callback_opens_view_prefilled_with_current_timezone():
    from qapbot.cache_manager import CACHE
    from qapbot.ui_clan_management import ClanManagementView, TimezoneConfigurationView

    CACHE.server_config["7002"] = {"timezone_name": "Europe/Berlin"}
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
    interaction.followup = AsyncMock()
    interaction.followup.send = AsyncMock(return_value=MagicMock())

    await view._on_select_timezone(interaction)

    interaction.followup.send.assert_awaited_once()
    _, kwargs = interaction.followup.send.call_args
    timezone_view = kwargs["view"]
    assert isinstance(timezone_view, TimezoneConfigurationView)
    assert timezone_view.selected_timezone == "Europe/Berlin"
    select = timezone_view.children[0]
    default_values = {opt.value for opt in select.options if opt.default}  # type: ignore[union-attr]
    assert default_values == {"Europe/Berlin"}


@pytest.mark.discord
@pytest.mark.asyncio
async def test_timezone_select_persists_choice_and_refreshes(mock_interaction):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_clan_management import TimezoneConfigurationView

    guild_id_str = str(mock_interaction.guild.id)
    CACHE.server_config[guild_id_str] = {}
    CACHE.db_manager = MagicMock()
    CACHE.db_manager.save_guild_config = AsyncMock()

    parent = MagicMock()
    parent._refresh_config_view = AsyncMock()

    view = TimezoneConfigurationView(parent, mock_interaction.guild.id, current_timezone="UTC")
    mock_interaction.data = {"values": ["Europe/Berlin"]}

    await view._on_timezone_select(mock_interaction)

    assert CACHE.server_config[guild_id_str]["timezone_name"] == "Europe/Berlin"
    parent._refresh_config_view.assert_awaited_once()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_timezone_select_deletes_config_message_after_applying(mock_interaction):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_clan_management import TimezoneConfigurationView

    guild_id_str = str(mock_interaction.guild.id)
    CACHE.server_config[guild_id_str] = {}
    CACHE.db_manager = MagicMock()
    CACHE.db_manager.save_guild_config = AsyncMock()

    parent = MagicMock()
    parent._refresh_config_view = AsyncMock()

    view = TimezoneConfigurationView(parent, mock_interaction.guild.id, current_timezone="UTC")
    view.config_message = AsyncMock()
    mock_interaction.data = {"values": ["Asia/Tokyo"]}

    await view._on_timezone_select(mock_interaction)

    view.config_message.delete.assert_awaited_once()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_basic_config_embed_shows_configured_timezone_between_language_and_registration():
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper import format_clan_management_message

    CACHE.server_config["7003"] = {"timezone_name": "Europe/Berlin"}
    guild = MagicMock()
    guild.id = 7003
    guild.name = "Test Guild"

    embed, _, _, _ = await format_clan_management_message("#CLAN1", guild, mode="config")

    field_values = [f.value or "" for f in embed.fields]
    timezone_index = next(i for i, v in enumerate(field_values) if "Europe/Berlin" in v)
    language_index = next(i for i, v in enumerate(field_values) if "Server Language" in v)
    registration_index = next(i for i, v in enumerate(field_values) if "Registration Message" in v)
    assert language_index < timezone_index < registration_index
