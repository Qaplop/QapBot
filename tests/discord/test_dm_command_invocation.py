"""Tests for Phase 0b of the DM interaction foundation (CWL_ROSTER_PLANNING_PLAN.md):
per-command-group @app_commands.guild_only() rollout.

Two layers:
1. A structural regression check that the intended commands (and only those) no longer
   carry guild_only() — cheap, and catches an accidental re-add or an accidental removal
   on a command that was deliberately left guild-only.
2. Functional DM-invocation coverage for the commands that actually gained new logic in
   this phase (ping — pure decorator removal, cheapest possible smoke test; subscriptions
   server_wide=True — the one command whose body changed, using resolve_guild_context()).
   The other five converted commands (status, help, list, analyse_leaguegroup,
   analyse_cwlopponent) had zero functional interaction.guild dependency per the Phase 0b
   audit — no code inside them changed, only the decorator — so the structural check plus
   their existing (guild-mode) test coverage is the right amount of testing here; a full
   DM-mode functional re-test of unchanged logic would mostly be re-testing discord.py's
   own decorator mechanics.
"""
from __future__ import annotations

import os
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")

import QBdiscordcmds  # noqa: E402


# ---------------------------------------------------------------------------
# Structural regression check
# ---------------------------------------------------------------------------

_CONVERTED = ["status", "ping", "help", "list", "subscriptions"]
_CONVERTED_GROUP_COMMANDS = ["cwl_league_group", "cwl_opponent"]  # analyse_group
_LEFT_GUILD_ONLY = ["subscribe", "unsubscribe", "leaderboard", "highlightme", "admin"]
_LEFT_GUILD_ONLY_GROUP_COMMANDS = [("clan_group", "management")]


@pytest.mark.discord
@pytest.mark.parametrize("name", _CONVERTED)
def test_converted_command_is_not_guild_only(name: str):
    assert getattr(QBdiscordcmds, name).guild_only is False


@pytest.mark.discord
def test_converted_analyse_group_commands_are_not_guild_only():
    names = {c.name: c for c in QBdiscordcmds.analyse_group.commands}
    for cmd_name in _CONVERTED_GROUP_COMMANDS:
        assert names[cmd_name].guild_only is False


@pytest.mark.discord
@pytest.mark.parametrize("name", _LEFT_GUILD_ONLY)
def test_deliberately_skipped_command_is_still_guild_only(name: str):
    assert getattr(QBdiscordcmds, name).guild_only is True


@pytest.mark.discord
def test_clan_management_is_still_guild_only():
    names = {c.name: c for c in QBdiscordcmds.clan_group.commands}
    assert names["management"].guild_only is True


@pytest.mark.discord
def test_whois_family_is_still_guild_only():
    assert QBdiscordcmds.whois.guild_only is True
    assert QBdiscordcmds.whois_message.guild_only is True
    assert QBdiscordcmds.whois_slash.guild_only is True


# ---------------------------------------------------------------------------
# Functional DM-invocation coverage
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_ping_works_from_dm(mock_interaction, monkeypatch):
    mock_interaction.guild = None
    mock_interaction.guild_id = None
    fake_bot = MagicMock()
    fake_bot.latency = 0.05
    monkeypatch.setattr(QBdiscordcmds.QBcore, "bot", fake_bot)

    await QBdiscordcmds.ping.callback(mock_interaction)  # type: ignore[arg-type]

    mock_interaction.response.send_message.assert_awaited_once()


class _FakeCache:
    def __init__(self) -> None:
        self.user_accounts: Dict[str, Dict[str, Any]] = {}
        self.server_config: Dict[str, Dict[str, Any]] = {}
        self.subscriptions: Dict[str, Dict[str, Any]] = {}
        self.clan_families: Dict[str, Dict[str, Any]] = {}

    def get_all_subscriptions_flat(self) -> Dict[str, Any]:
        return {}


@pytest.mark.discord
@pytest.mark.asyncio
async def test_subscriptions_server_wide_dm_not_linked(mock_interaction, monkeypatch):
    """DM caller with no linked accounts -> resolve_guild_context() returns None -> the
    dm_not_linked error, not a crash on the old `if not interaction.guild: return`."""
    mock_interaction.guild = None
    # discord.InteractionResponse.is_done() is synchronous in real discord.py; the shared
    # mock_interaction fixture makes response an AsyncMock, whose auto-generated child
    # attributes are themselves AsyncMock (so plain `.return_value` would make is_done()
    # return an unawaited coroutine, which is truthy). Override with a real sync MagicMock.
    mock_interaction.response.is_done = MagicMock(return_value=False)
    fake_cache = _FakeCache()
    monkeypatch.setattr(QBdiscordcmds, "CACHE", fake_cache)
    import qapbot.QBdiscocmdshelper as helper
    monkeypatch.setattr(helper, "CACHE", fake_cache)

    await QBdiscordcmds.subscriptions.callback(mock_interaction, server_wide=True)  # type: ignore[arg-type]

    mock_interaction.response.send_message.assert_awaited_once()
    kwargs = mock_interaction.response.send_message.await_args.kwargs
    assert kwargs.get("ephemeral") is True


@pytest.mark.discord
@pytest.mark.asyncio
async def test_subscriptions_server_wide_dm_resolves_single_guild(mock_interaction, monkeypatch):
    """DM caller linked to exactly one guild's clans -> resolves silently and renders that
    guild's subscriptions via the resolved Guild object, not interaction.guild (which is None)."""
    mock_interaction.guild = None
    # discord.InteractionResponse.is_done() is synchronous in real discord.py; the shared
    # mock_interaction fixture makes response an AsyncMock, whose auto-generated child
    # attributes are themselves AsyncMock (so plain `.return_value` would make is_done()
    # return an unawaited coroutine, which is truthy). Override with a real sync MagicMock.
    mock_interaction.response.is_done = MagicMock(return_value=False)

    fake_cache = _FakeCache()
    fake_cache.user_accounts[str(mock_interaction.user.id)] = {
        "players": [{"player_tag": "#P1", "current_clan_tag": "#CLAN1"}]
    }
    fake_cache.server_config["555"] = {"member_clans": ["#CLAN1"], "member_families": []}
    fake_cache.subscriptions["555"] = {}  # no channel subscriptions -> short-circuits to "no subscriptions"
    monkeypatch.setattr(QBdiscordcmds, "CACHE", fake_cache)
    import qapbot.QBdiscocmdshelper as helper
    monkeypatch.setattr(helper, "CACHE", fake_cache)

    fake_guild = MagicMock()
    fake_guild.id = 555
    fake_guild.name = "TestGuild"
    fake_bot = MagicMock()
    fake_bot.get_guild = MagicMock(return_value=fake_guild)
    fake_bot.fully_initialized = True
    monkeypatch.setattr(QBdiscordcmds.QBcore, "bot", fake_bot)

    sent = {}

    async def _fake_send_and_track(interaction, content=None, command_name=None, embed=None, ephemeral=False):
        sent["content"] = content

    monkeypatch.setattr(QBdiscordcmds, "send_and_track", _fake_send_and_track)

    await QBdiscordcmds.subscriptions.callback(mock_interaction, server_wide=True)  # type: ignore[arg-type]

    # Never touched interaction.guild (which is None) — resolved the Guild via QBcore.bot.get_guild().
    fake_bot.get_guild.assert_called_once_with(555)
    assert sent.get("content") == "This guild has no subscriptions."
