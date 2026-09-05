from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest


class _FakeCache:
    def __init__(self):
        self.leaderboard_messages: Dict[str, Dict[str, Any]] = {}
        self.user_accounts: Dict[str, Dict[str, Any]] = {}

    async def set_leaderboard_message(self, key: str, entry: Dict[str, Any]) -> None:
        self.leaderboard_messages[key] = entry

    async def delete_leaderboard_message(self, key: str) -> None:
        self.leaderboard_messages.pop(key, None)


@pytest.mark.discord
def test_check_bot_admin_only(mock_interaction):
    from qapbot.QBdiscocmdshelper import check_bot_admin_only

    mock_interaction.user.__str__ = lambda self=None: "BotAdmin"  # type: ignore[assignment]
    assert check_bot_admin_only(mock_interaction, "BotAdmin") is True
    assert check_bot_admin_only(mock_interaction, "SomeoneElse") is False


@pytest.mark.discord
def test_check_bot_admin_only_numeric_id(mock_interaction):
    from qapbot.QBdiscocmdshelper import check_bot_admin_only

    # mock_interaction.user.id == 123456789 (conftest)
    assert check_bot_admin_only(mock_interaction, "123456789") is True
    assert check_bot_admin_only(mock_interaction, "999999999") is False
    # Empty config never grants access
    assert check_bot_admin_only(mock_interaction, "") is False


@pytest.mark.discord
def test_check_bot_admin_or_tester(mock_interaction, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper import check_bot_admin_or_tester

    monkeypatch.setattr(CACHE, "testers", {"999999999"})
    # mock_interaction.user.id == 123456789 (conftest)
    assert check_bot_admin_or_tester(mock_interaction, "123456789") is True  # admin
    monkeypatch.setattr(CACHE, "testers", {"123456789"})
    assert check_bot_admin_or_tester(mock_interaction, "999999999") is True  # tester
    monkeypatch.setattr(CACHE, "testers", set())
    assert check_bot_admin_or_tester(mock_interaction, "999999999") is False  # neither


@pytest.mark.discord
@pytest.mark.asyncio
async def test_check_admin_permissions_numeric_id(mock_interaction):
    from qapbot.QBdiscocmdshelper import check_admin_permissions

    # mock_interaction.user.id == 123456789 (conftest)
    assert await check_admin_permissions(mock_interaction, "123456789") is True
    assert await check_admin_permissions(mock_interaction, "999999999") is False


@pytest.mark.discord
@pytest.mark.asyncio
async def test_check_admin_permissions_fallback_user_string(mock_interaction):
    from qapbot.QBdiscocmdshelper import check_admin_permissions

    mock_interaction.user.__str__ = lambda self=None: "ServerAdmin"  # type: ignore[assignment]
    assert await check_admin_permissions(mock_interaction, "ServerAdmin") is True
    assert await check_admin_permissions(mock_interaction, "Other") is False


@pytest.mark.discord
@pytest.mark.asyncio
async def test_send_and_track_sends_and_tracks(monkeypatch: pytest.MonkeyPatch, mock_interaction):
    import qapbot.QBdiscocmdshelper as helper

    fake_cache = _FakeCache()
    monkeypatch.setattr(helper, "CACHE", fake_cache)

    async def _discord_retry(op, _name="x"):
        return await op()

    monkeypatch.setattr(helper, "discord_retry", _discord_retry)

    sent_message = MagicMock()
    sent_message.id = 4242
    mock_interaction.channel.send = AsyncMock(return_value=sent_message)

    await helper.send_and_track(mock_interaction, content="hello", command_name="status")

    mock_interaction.channel.send.assert_awaited()
    assert fake_cache.leaderboard_messages, "Expected message tracking entry"
    entry = next(iter(fake_cache.leaderboard_messages.values()))
    assert entry["mode"] == "status"
    assert "4242" in entry["message_ids"]
