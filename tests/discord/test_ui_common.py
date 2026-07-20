from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import discord
import pytest


@pytest.mark.discord
@pytest.mark.asyncio
async def test_update_user_metadata_from_interaction_calls_cache(monkeypatch: pytest.MonkeyPatch):
    import qapbot.ui_common as ui

    fake_cache = SimpleNamespace(update_user_metadata=AsyncMock())
    monkeypatch.setattr(ui, "CACHE", fake_cache, raising=False)

    interaction = SimpleNamespace(user=SimpleNamespace(id=12345))
    await ui.update_user_metadata_from_interaction(cast(Any, interaction))

    fake_cache.update_user_metadata.assert_awaited_once_with("12345", interaction=interaction)


@pytest.mark.discord
@pytest.mark.asyncio
async def test_update_user_metadata_from_interaction_swallows_errors(monkeypatch: pytest.MonkeyPatch):
    import qapbot.ui_common as ui

    fake_cache = SimpleNamespace(update_user_metadata=AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(ui, "CACHE", fake_cache, raising=False)

    interaction = SimpleNamespace(user=SimpleNamespace(id=999))
    await ui.update_user_metadata_from_interaction(cast(Any, interaction))


@pytest.mark.discord
@pytest.mark.asyncio
async def test_generic_select_view_limits_options_and_forwards_selection():
    from qapbot.ui_common import GenericSelectView

    callback = AsyncMock()
    options = [discord.SelectOption(label=f"Option {i}", value=f"v{i}") for i in range(30)]

    view = GenericSelectView(
        options=options,
        callback_fn=callback,
        callback_kwargs={"guild_id": 123},
    )

    fake_interaction = SimpleNamespace()
    view.select = SimpleNamespace(values=["v2"])  # type: ignore[assignment]
    on_select = getattr(view, "_on_select")
    await on_select(cast(Any, fake_interaction))

    callback.assert_awaited_once_with(fake_interaction, "v2", guild_id=123)


@pytest.mark.discord
@pytest.mark.asyncio
async def test_language_select_view_success_path(monkeypatch: pytest.MonkeyPatch):
    import qapbot.i18n as i18n
    from qapbot.ui_common import LanguageSelectView

    def _t_success(key: str, **kwargs: Any) -> str:
        return f"{key}:{kwargs.get('lang_name', '')}"

    monkeypatch.setattr(i18n, "t", _t_success)

    async def _set_language(_guild_id: int, _selected_language: str) -> bool:
        return True

    monkeypatch.setattr(i18n, "set_guild_language", _set_language)

    view = LanguageSelectView(guild_id=123, available_languages=[("de", "Deutsch")])
    fake_select = SimpleNamespace(values=["de"], disabled=False)
    view.select = fake_select  # type: ignore[assignment]

    interaction = SimpleNamespace(
        response=SimpleNamespace(send_message=AsyncMock()),
        message=SimpleNamespace(edit=AsyncMock()),
    )

    on_select = getattr(view, "_on_select")
    await on_select(cast(Any, interaction))

    interaction.response.send_message.assert_awaited_once()
    _, kwargs = interaction.response.send_message.await_args
    assert kwargs.get("ephemeral") is True
    assert fake_select.disabled is True


@pytest.mark.discord
@pytest.mark.asyncio
async def test_language_select_view_failure_path(monkeypatch: pytest.MonkeyPatch):
    import qapbot.i18n as i18n
    from qapbot.ui_common import LanguageSelectView

    def _t_failure(key: str, **kwargs: Any) -> str:
        _ = kwargs
        return key

    monkeypatch.setattr(i18n, "t", _t_failure)

    async def _set_language(_guild_id: int, _selected_language: str) -> bool:
        return False

    monkeypatch.setattr(i18n, "set_guild_language", _set_language)

    view = LanguageSelectView(guild_id=123, available_languages=[("en", "English")])
    fake_select = SimpleNamespace(values=["en"], disabled=False)
    view.select = fake_select  # type: ignore[assignment]

    interaction = SimpleNamespace(
        response=SimpleNamespace(send_message=AsyncMock()),
        message=SimpleNamespace(edit=AsyncMock()),
    )

    on_select = getattr(view, "_on_select")
    await on_select(cast(Any, interaction))

    interaction.response.send_message.assert_awaited_once()
    _, kwargs = interaction.response.send_message.await_args
    assert kwargs.get("ephemeral") is True
    assert fake_select.disabled is True


# ---------------------------------------------------------------------------
# check_maintenance_block tests
# ---------------------------------------------------------------------------


@pytest.mark.discord
@pytest.mark.asyncio
async def test_check_maintenance_block_returns_false_when_not_in_maintenance(monkeypatch: pytest.MonkeyPatch):
    """Normal operation — helper returns False so callers proceed."""
    import QBcore as _qbcore
    from qapbot.ui_common import check_maintenance_block

    monkeypatch.setattr(_qbcore, "maintenance_mode", False)
    monkeypatch.setattr(_qbcore, "db_maintenance_mode", False)

    interaction = SimpleNamespace(guild=SimpleNamespace(id=1))
    result = await check_maintenance_block(cast(Any, interaction))
    assert result is False


@pytest.mark.discord
@pytest.mark.asyncio
async def test_check_maintenance_block_blocks_during_maintenance(monkeypatch: pytest.MonkeyPatch):
    """Full maintenance mode — helper returns True and sends ephemeral msg."""
    import QBcore as _qbcore
    from qapbot.ui_common import check_maintenance_block

    monkeypatch.setattr(_qbcore, "maintenance_mode", True)
    monkeypatch.setattr(_qbcore, "db_maintenance_mode", False)

    send_msg = AsyncMock()
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=1),
        response=SimpleNamespace(is_done=lambda: False, send_message=send_msg),
    )
    result = await check_maintenance_block(cast(Any, interaction))
    assert result is True
    send_msg.assert_awaited_once()
    _, kwargs = send_msg.await_args
    assert kwargs.get("ephemeral") is True


@pytest.mark.discord
@pytest.mark.asyncio
async def test_check_maintenance_block_blocks_during_db_maintenance(monkeypatch: pytest.MonkeyPatch):
    """DB maintenance mode — helper returns True and sends ephemeral msg."""
    import QBcore as _qbcore
    from qapbot.ui_common import check_maintenance_block

    monkeypatch.setattr(_qbcore, "maintenance_mode", False)
    monkeypatch.setattr(_qbcore, "db_maintenance_mode", True)

    send_msg = AsyncMock()
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=1),
        response=SimpleNamespace(is_done=lambda: False, send_message=send_msg),
    )
    result = await check_maintenance_block(cast(Any, interaction))
    assert result is True
    send_msg.assert_awaited_once()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_check_maintenance_block_uses_followup_when_response_done(monkeypatch: pytest.MonkeyPatch):
    """When the interaction response is already consumed, use followup."""
    import QBcore as _qbcore
    from qapbot.ui_common import check_maintenance_block

    monkeypatch.setattr(_qbcore, "maintenance_mode", True)
    monkeypatch.setattr(_qbcore, "db_maintenance_mode", False)

    followup_send = AsyncMock()
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=1),
        response=SimpleNamespace(is_done=lambda: True),
        followup=SimpleNamespace(send=followup_send),
    )
    result = await check_maintenance_block(cast(Any, interaction))
    assert result is True
    followup_send.assert_awaited_once()
