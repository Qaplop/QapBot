from __future__ import annotations
# pyright: reportUnknownParameterType=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportMissingParameterType=false

from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest


class _FakeCache:
    def __init__(self) -> None:
        self.user_accounts: Dict[str, Dict[str, Any]] = {}
        self.set_user_account = AsyncMock(side_effect=self._set_user)
        self.persist_user = AsyncMock()

    async def _set_user(self, user_id: str, user_data: Dict[str, Any]) -> None:
        self.user_accounts[user_id] = user_data


def _format_settings_stub(*_args: object, **_kwargs: object) -> str:
    return "settings"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_war_notification_activate_with_missing_user_shows_error(monkeypatch: pytest.MonkeyPatch, mock_interaction):
    import qapbot.ui_notifications as ui

    fake_cache = _FakeCache()
    monkeypatch.setattr(ui, "CACHE", fake_cache)

    view = ui.WarNotificationPromptView(guild_id=mock_interaction.guild.id)
    button = view.children[0]
    await button.callback(mock_interaction)

    mock_interaction.response.send_message.assert_awaited_once()
    _, kwargs = mock_interaction.response.send_message.await_args
    assert kwargs.get("ephemeral") is True


@pytest.mark.discord
@pytest.mark.asyncio
async def test_war_notification_activate_enables_defaults_and_edits_message(monkeypatch: pytest.MonkeyPatch, mock_interaction):
    import qapbot.ui_notifications as ui

    fake_cache = _FakeCache()
    user_id = str(mock_interaction.user.id)
    fake_cache.user_accounts[user_id] = {"players": [{"player_tag": "#P1"}]}
    monkeypatch.setattr(ui, "CACHE", fake_cache)

    view = ui.WarNotificationPromptView(guild_id=mock_interaction.guild.id)
    button = view.children[0]
    await button.callback(mock_interaction)

    notif = fake_cache.user_accounts[user_id]["notification_settings"]
    assert notif["war_reminders"] is True
    assert notif["notification_mode"] == "repeated"
    assert notif["notification_type"] == "all_wars"
    assert notif["hours_before_end"] == 4

    fake_cache.set_user_account.assert_awaited_once()
    mock_interaction.response.edit_message.assert_awaited_once()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_unified_notification_interaction_check_blocks_other_user(mock_interaction):
    from qapbot.ui_notifications import UnifiedNotificationView

    view = UnifiedNotificationView(
        user_id="999999",
        is_enabled=False,
        current_type="all_wars",
        current_mode="repeated",
        original_interaction=mock_interaction,
    )

    ok = await view.interaction_check(mock_interaction)
    assert ok is False
    mock_interaction.response.send_message.assert_awaited_once()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_unified_change_type_updates_cache_and_message(monkeypatch: pytest.MonkeyPatch, mock_interaction):
    import qapbot.ui_notifications as ui
    import qapbot.QBdiscocmdshelper as helper
    import qapbot.cache_manager as cache_manager_module

    fake_cache = _FakeCache()
    user_id = str(mock_interaction.user.id)
    fake_cache.user_accounts[user_id] = {
        "display_name": "TestUser",
        "notification_settings": {
            "war_reminders": True,
            "notification_type": "all_wars",
            "notification_mode": "repeated",
        },
        "players": [{"player_tag": "#P1"}],
    }

    monkeypatch.setattr(ui, "CACHE", fake_cache)
    monkeypatch.setattr(cache_manager_module, "CACHE", fake_cache)
    monkeypatch.setattr(helper, "format_notification_settings", _format_settings_stub)
    mock_interaction.guild_id = mock_interaction.guild.id

    view = ui.UnifiedNotificationView(
        user_id=user_id,
        is_enabled=True,
        current_type="all_wars",
        current_mode="repeated",
        original_interaction=mock_interaction,
    )

    await view.change_type_button(mock_interaction)

    assert fake_cache.user_accounts[user_id]["notification_settings"]["notification_type"] == "cwl_only"
    fake_cache.set_user_account.assert_awaited_once()
    mock_interaction.response.edit_message.assert_awaited_once()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_language_selection_auto_sets_locale_and_persists(monkeypatch: pytest.MonkeyPatch, mock_interaction):
    import qapbot.ui_notifications as ui
    import qapbot.QBdiscocmdshelper as helper
    import qapbot.cache_manager as cache_manager_module

    fake_cache = _FakeCache()
    user_id = str(mock_interaction.user.id)
    fake_cache.user_accounts[user_id] = {
        "display_name": "TestUser",
        "notification_settings": {
            "war_reminders": True,
            "notification_type": "all_wars",
            "notification_mode": "repeated",
        },
        "players": [{"player_tag": "#P1"}],
    }

    monkeypatch.setattr(ui, "CACHE", fake_cache)
    monkeypatch.setattr(cache_manager_module, "CACHE", fake_cache)
    monkeypatch.setattr(helper, "format_notification_settings", _format_settings_stub)
    mock_interaction.guild_id = mock_interaction.guild.id
    mock_interaction.locale = "de"
    mock_interaction.data = {"values": ["auto"]}

    parent = ui.UnifiedNotificationView(
        user_id=user_id,
        is_enabled=True,
        current_type="all_wars",
        current_mode="repeated",
        original_interaction=mock_interaction,
    )
    lang_view = ui.LanguageSelectionView(user_id=user_id, parent_view=parent, original_interaction=mock_interaction)

    await lang_view.language_selected(mock_interaction)

    assert fake_cache.user_accounts[user_id]["user_language"] == "de"
    assert fake_cache.user_accounts[user_id]["user_language_locked"] is False
    fake_cache.persist_user.assert_awaited_once_with(user_id)
    mock_interaction.response.edit_message.assert_awaited_once()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_language_selection_interaction_check_blocks_other_user(mock_interaction):
    from qapbot.ui_notifications import LanguageSelectionView, UnifiedNotificationView

    parent = UnifiedNotificationView(
        user_id="123",
        is_enabled=True,
        current_type="all_wars",
        current_mode="repeated",
        original_interaction=mock_interaction,
    )
    lang_view = LanguageSelectionView(user_id="999", parent_view=parent, original_interaction=mock_interaction)

    ok = await lang_view.interaction_check(mock_interaction)
    assert ok is False
    mock_interaction.response.send_message.assert_awaited_once()
