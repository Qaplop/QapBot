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


@pytest.mark.discord
@pytest.mark.asyncio
async def test_registration_view_blocks_before_fully_initialized(monkeypatch: pytest.MonkeyPatch):
    """Guards against the exact race that caused a false 'no linked accounts' report:
    add_view() registers this view for dispatch before on_ready() finishes loading
    CACHE, so a click during that window must be blocked with a friendly message
    instead of reading a not-yet-populated cache."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import QBcore
    from qapbot.ui_registration import RegistrationView

    monkeypatch.setattr(QBcore.bot, "fully_initialized", False, raising=False)

    send_msg = AsyncMock()
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=1),
        response=SimpleNamespace(is_done=lambda: False, send_message=send_msg),
    )

    view = RegistrationView()  # generic add_view() dispatch instance
    allowed = await view.interaction_check(interaction)  # type: ignore[arg-type]

    assert allowed is False
    send_msg.assert_awaited_once()
    args, kwargs = send_msg.await_args
    assert "starting up" in args[0].lower()
    assert kwargs.get("ephemeral") is True


@pytest.mark.discord
@pytest.mark.asyncio
async def test_registration_view_allows_once_fully_initialized(monkeypatch: pytest.MonkeyPatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import QBcore
    from qapbot.ui_registration import RegistrationView

    monkeypatch.setattr(QBcore.bot, "fully_initialized", True, raising=False)
    monkeypatch.setattr(QBcore, "maintenance_mode", False, raising=False)
    monkeypatch.setattr(QBcore, "db_maintenance_mode", False, raising=False)

    send_msg = AsyncMock()
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=1),
        response=SimpleNamespace(is_done=lambda: False, send_message=send_msg),
    )

    view = RegistrationView()
    allowed = await view.interaction_check(interaction)  # type: ignore[arg-type]

    assert allowed is True
    send_msg.assert_not_awaited()
