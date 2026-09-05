from __future__ import annotations
# pyright: reportPrivateUsage=false, reportUnknownParameterType=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest

from tests.discord.workflow_harness import FakeCache, identity_t, make_interaction


def _make_users(count: int) -> Dict[str, Dict[str, Any]]:
    users: Dict[str, Dict[str, Any]] = {}
    for index in range(count):
        user_id = f"u{index}"
        users[user_id] = {
            "display_name": f"User{index}",
            "players": [{"player_name": f"Player{index}", "player_tag": f"#P{index}"}],
        }
    return users


@pytest.mark.discord
@pytest.mark.asyncio
async def test_unified_change_mode_missing_user_shows_error(monkeypatch: pytest.MonkeyPatch):
    import qapbot.ui_notifications as ui
    import qapbot.cache_manager as cache_manager_module
    import qapbot.i18n as i18n_module

    interaction = make_interaction()
    cache = FakeCache()

    monkeypatch.setattr(ui, "CACHE", cache)
    monkeypatch.setattr(cache_manager_module, "CACHE", cache)
    monkeypatch.setattr(i18n_module, "t", identity_t)

    view = ui.UnifiedNotificationView(
        user_id=str(interaction.user.id),
        is_enabled=True,
        current_type="all_wars",
        current_mode="repeated",
        original_interaction=interaction,
    )

    await view.change_mode_button(interaction)

    interaction.response.send_message.assert_awaited_once()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_unified_change_language_missing_user_shows_error(monkeypatch: pytest.MonkeyPatch):
    import qapbot.ui_notifications as ui
    import qapbot.cache_manager as cache_manager_module
    import qapbot.i18n as i18n_module

    interaction = make_interaction()
    cache = FakeCache()

    monkeypatch.setattr(ui, "CACHE", cache)
    monkeypatch.setattr(cache_manager_module, "CACHE", cache)
    monkeypatch.setattr(i18n_module, "t", identity_t)

    view = ui.UnifiedNotificationView(
        user_id=str(interaction.user.id),
        is_enabled=True,
        current_type="all_wars",
        current_mode="repeated",
        original_interaction=interaction,
    )

    await view.change_language_button(interaction)

    interaction.response.send_message.assert_awaited_once()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_language_selected_manual_sets_lock_and_persists(monkeypatch: pytest.MonkeyPatch):
    import qapbot.ui_notifications as ui
    import qapbot.QBdiscocmdshelper as helper
    import qapbot.cache_manager as cache_manager_module
    import qapbot.i18n as i18n_module

    interaction = make_interaction()
    user_id = str(interaction.user.id)

    cache = FakeCache()
    cache.user_accounts[user_id] = {
        "display_name": "TestUser",
        "notification_settings": {
            "war_reminders": True,
            "notification_type": "all_wars",
            "notification_mode": "repeated",
        },
        "players": [{"player_tag": "#P1"}],
    }

    monkeypatch.setattr(ui, "CACHE", cache)
    monkeypatch.setattr(cache_manager_module, "CACHE", cache)
    monkeypatch.setattr(i18n_module, "t", identity_t)
    monkeypatch.setattr(helper, "format_notification_settings", lambda *_args, **_kwargs: "settings")

    interaction.data = {"values": ["en"]}

    parent = ui.UnifiedNotificationView(
        user_id=user_id,
        is_enabled=True,
        current_type="all_wars",
        current_mode="repeated",
        original_interaction=interaction,
    )
    view = ui.LanguageSelectionView(user_id=user_id, parent_view=parent, original_interaction=interaction)

    await view.language_selected(interaction)

    assert cache.user_accounts[user_id]["user_language"] == "en"
    assert cache.user_accounts[user_id]["user_language_locked"] is True
    cache.persist_user.assert_awaited_once_with(user_id)
    interaction.response.edit_message.assert_awaited_once()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_language_selected_missing_values_returns_early(monkeypatch: pytest.MonkeyPatch):
    import qapbot.ui_notifications as ui
    import qapbot.i18n as i18n_module

    interaction = make_interaction()
    interaction.data = {}
    monkeypatch.setattr(i18n_module, "t", identity_t)

    parent = ui.UnifiedNotificationView(
        user_id=str(interaction.user.id),
        is_enabled=True,
        current_type="all_wars",
        current_mode="repeated",
        original_interaction=interaction,
    )
    view = ui.LanguageSelectionView(
        user_id=str(interaction.user.id),
        parent_view=parent,
        original_interaction=interaction,
    )

    await view.language_selected(interaction)

    interaction.response.send_message.assert_not_awaited()
    interaction.response.edit_message.assert_not_awaited()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_notification_settings_user_select_pagination_actions(monkeypatch: pytest.MonkeyPatch):
    import qapbot.ui_notifications as ui
    import qapbot.i18n as i18n_module

    interaction = make_interaction()
    monkeypatch.setattr(i18n_module, "t", identity_t)

    sent_message = SimpleNamespace(edit=AsyncMock())
    users_in_clan = _make_users(30)

    view = ui.NotificationSettingsView(
        scope="user",
        clan_tag="#C1",
        sent_message=sent_message,  # type: ignore[arg-type]  # duck-typed: only needs .edit()
        guild_clans=["#C1"],
        users_in_clan=users_in_clan,
        guild=interaction.guild,
    )

    interaction.data = {"values": ["__load_more__"]}
    await view._on_user_select(interaction)
    assert view.user_offset == 24

    interaction.data = {"values": ["__back_to_first__"]}
    await view._on_user_select(interaction)
    assert view.user_offset == 0
    interaction.edit_original_response.assert_awaited()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_notification_settings_user_select_prefills_from_cache(monkeypatch: pytest.MonkeyPatch):
    import qapbot.ui_notifications as ui
    import qapbot.cache_manager as cache_manager_module
    import qapbot.i18n as i18n_module

    interaction = make_interaction()
    monkeypatch.setattr(i18n_module, "t", identity_t)

    cache = FakeCache()
    cache.user_accounts["u2"] = {
        "notification_settings": {
            "war_reminders": True,
            "notification_type": "cwl_only",
            "notification_mode": "once",
        }
    }

    monkeypatch.setattr(ui, "CACHE", cache)
    monkeypatch.setattr(cache_manager_module, "CACHE", cache)

    sent_message = SimpleNamespace(edit=AsyncMock())
    users_in_clan = _make_users(3)

    view = ui.NotificationSettingsView(
        scope="user",
        clan_tag="#C1",
        sent_message=sent_message,  # type: ignore[arg-type]  # duck-typed: only needs .edit()
        guild_clans=["#C1"],
        users_in_clan=users_in_clan,
        guild=interaction.guild,
    )

    interaction.data = {"values": ["u2"]}
    await view._on_user_select(interaction)

    assert view.selected_user_id == "u2"
    assert view.notification_enabled is True
    assert view.notification_type == "cwl_only"
    assert view.notification_mode == "once"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_notification_settings_apply_clan_scope_no_affected_users(monkeypatch: pytest.MonkeyPatch):
    import qapbot.ui_notifications as ui
    import qapbot.cache_manager as cache_manager_module
    import qapbot.i18n as i18n_module

    interaction = make_interaction()
    monkeypatch.setattr(i18n_module, "t", identity_t)

    cache = FakeCache()
    cache.user_accounts = {
        "u1": {"players": [{"current_clan_tag": "#OTHER"}]},
        "u2": {"players": [{"current_clan_tag": "#OTHER"}]},
    }

    monkeypatch.setattr(ui, "CACHE", cache)
    monkeypatch.setattr(cache_manager_module, "CACHE", cache)

    sent_message = SimpleNamespace(edit=AsyncMock())

    view = ui.NotificationSettingsView(
        scope="clan",
        clan_tag="#C1",
        sent_message=sent_message,  # type: ignore[arg-type]  # duck-typed: only needs .edit()
        guild_clans=["#C1"],
        guild=interaction.guild,
    )
    view.notification_enabled = True
    view.notification_type = "all_wars"
    view.notification_mode = "repeated"

    await view._on_apply(interaction)

    cache.persist_user.assert_not_awaited()
    sent_message.edit.assert_not_awaited()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_notification_settings_apply_user_scope_updates_and_refreshes(monkeypatch: pytest.MonkeyPatch):
    import qapbot.ui_notifications as ui
    import qapbot.cache_manager as cache_manager_module
    import qapbot.i18n as i18n_module
    import qapbot.QBdiscocmdshelper as helper
    import qapbot.ui_clan_management as clan_mgmt

    interaction = make_interaction()
    monkeypatch.setattr(i18n_module, "t", identity_t)

    cache = FakeCache()
    cache.user_accounts = {
        "u1": {
            "players": [{"current_clan_tag": "#C1"}],
            "notification_settings": {},
        }
    }

    monkeypatch.setattr(ui, "CACHE", cache)
    monkeypatch.setattr(cache_manager_module, "CACHE", cache)
    monkeypatch.setattr(
        helper,
        "format_clan_management_message",
        AsyncMock(return_value=(SimpleNamespace(title="Main"), None, None, [])),
    )

    class _FakeClanManagementView:
        def __init__(self, **kwargs: Any):
            _ = kwargs

    monkeypatch.setattr(clan_mgmt, "ClanManagementView", _FakeClanManagementView)

    sent_message = SimpleNamespace(edit=AsyncMock())
    users_in_clan = {"u1": {"display_name": "User1", "players": [{"player_name": "P1"}]}}

    view = ui.NotificationSettingsView(
        scope="user",
        clan_tag="#C1",
        sent_message=sent_message,  # type: ignore[arg-type]  # duck-typed: only needs .edit()
        guild_clans=["#C1"],
        users_in_clan=users_in_clan,
        guild=interaction.guild,
    )
    view.selected_user_id = "u1"
    view.notification_enabled = False
    view.notification_type = "cwl_only"
    view.notification_mode = "once"

    await view._on_apply(interaction)

    user_notif = cache.user_accounts["u1"]["notification_settings"]
    assert user_notif["war_reminders"] is False
    assert user_notif["notification_type"] == "cwl_only"
    assert user_notif["notification_mode"] == "once"
    cache.persist_user.assert_awaited_once_with("u1")
    sent_message.edit.assert_awaited_once()
