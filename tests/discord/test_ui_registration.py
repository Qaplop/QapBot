from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.mark.discord
@pytest.mark.asyncio
async def test_registration_view_labels_are_translated():
    from qapbot.ui_registration import RegistrationView

    view = RegistrationView(guild_id=123)
    labels = [c.label for c in view.children]
    assert "🔗 Link Accounts" in labels
    assert "⚙️ War Notifications" in labels


@pytest.mark.discord
@pytest.mark.asyncio
async def test_registration_war_notifications_no_accounts_sends_message(monkeypatch: pytest.MonkeyPatch, mock_interaction):
    import qapbot.ui_registration as ui

    # Avoid any metadata update I/O
    monkeypatch.setattr(ui, "update_user_metadata_from_interaction", AsyncMock())

    class _FakeCache:
        user_accounts = {}

    monkeypatch.setattr(ui, "CACHE", _FakeCache())

    view = ui.RegistrationView(guild_id=mock_interaction.guild.id)
    button = next(c for c in view.children if getattr(c, "custom_id", "") == "registration_war_notifications")
    await button.callback(mock_interaction)

    mock_interaction.followup.send.assert_awaited()
    args, kwargs = mock_interaction.followup.send.await_args
    assert "don't have any registered players" in args[0].lower()
    assert kwargs.get("ephemeral") is True
