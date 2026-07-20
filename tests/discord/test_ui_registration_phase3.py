from __future__ import annotations
# pyright: reportUnknownParameterType=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportMissingParameterType=false

from unittest.mock import AsyncMock

import pytest


@pytest.mark.discord
@pytest.mark.asyncio
async def test_link_account_with_unverified_players_shows_action_view(monkeypatch: pytest.MonkeyPatch, mock_interaction):
    import qapbot.ui_registration as ui

    monkeypatch.setattr(ui, "update_user_metadata_from_interaction", AsyncMock())
    show_modal = AsyncMock()
    monkeypatch.setattr(ui, "_show_player_search_modal", show_modal)

    class _FakeCache:
        user_accounts = {
            str(mock_interaction.user.id): {
                "players": [
                    {"player_tag": "#P1", "player_name": "Alice", "verified": False}
                ]
            }
        }

    monkeypatch.setattr(ui, "CACHE", _FakeCache())

    view = ui.RegistrationView(guild_id=mock_interaction.guild.id)
    button = next(c for c in view.children if getattr(c, "custom_id", "") == "registration_link_account")
    await button.callback(mock_interaction)

    mock_interaction.response.send_message.assert_awaited_once()
    _, kwargs = mock_interaction.response.send_message.await_args
    assert kwargs.get("ephemeral") is True
    assert isinstance(kwargs.get("view"), ui.AccountActionView)
    show_modal.assert_not_awaited()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_link_account_without_unverified_opens_search_modal(monkeypatch: pytest.MonkeyPatch, mock_interaction):
    import qapbot.ui_registration as ui

    monkeypatch.setattr(ui, "update_user_metadata_from_interaction", AsyncMock())
    show_modal = AsyncMock()
    monkeypatch.setattr(ui, "_show_player_search_modal", show_modal)

    class _FakeCache:
        user_accounts = {
            str(mock_interaction.user.id): {
                "players": [
                    {"player_tag": "#P1", "player_name": "Alice", "verified": True}
                ]
            }
        }

    monkeypatch.setattr(ui, "CACHE", _FakeCache())

    view = ui.RegistrationView(guild_id=mock_interaction.guild.id)
    button = next(c for c in view.children if getattr(c, "custom_id", "") == "registration_link_account")
    await button.callback(mock_interaction)

    show_modal.assert_awaited_once_with(mock_interaction, mock_interaction.guild.id, user_mentioned=False)


@pytest.mark.discord
@pytest.mark.asyncio
async def test_api_verification_no_players_sends_info(monkeypatch: pytest.MonkeyPatch, mock_interaction):
    import qapbot.ui_registration as ui

    monkeypatch.setattr(ui, "update_user_metadata_from_interaction", AsyncMock())

    class _FakeCache:
        user_accounts = {str(mock_interaction.user.id): {"players": []}}

    monkeypatch.setattr(ui, "CACHE", _FakeCache())

    view = ui.RegistrationView(guild_id=mock_interaction.guild.id)
    button = next(c for c in view.children if getattr(c, "custom_id", "") == "registration_api_verification")
    await button.callback(mock_interaction)

    mock_interaction.response.send_message.assert_awaited_once()
    _, kwargs = mock_interaction.response.send_message.await_args
    assert kwargs.get("ephemeral") is True


@pytest.mark.discord
@pytest.mark.asyncio
async def test_api_verification_all_verified_sends_info(monkeypatch: pytest.MonkeyPatch, mock_interaction):
    import qapbot.ui_registration as ui

    monkeypatch.setattr(ui, "update_user_metadata_from_interaction", AsyncMock())

    class _FakeCache:
        user_accounts = {
            str(mock_interaction.user.id): {
                "players": [
                    {"player_tag": "#P1", "player_name": "Alice", "verified": True}
                ]
            }
        }

    monkeypatch.setattr(ui, "CACHE", _FakeCache())

    view = ui.RegistrationView(guild_id=mock_interaction.guild.id)
    button = next(c for c in view.children if getattr(c, "custom_id", "") == "registration_api_verification")
    await button.callback(mock_interaction)

    mock_interaction.response.send_message.assert_awaited_once()
    _, kwargs = mock_interaction.response.send_message.await_args
    assert kwargs.get("ephemeral") is True
