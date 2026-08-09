"""Tests for Phase 0a of the DM interaction foundation (CWL_ROSTER_PLANNING_PLAN.md).

Covers:
- resolve_guild_context(): guild pass-through, DM zero/one/many-match resolution.
- _prompt_dm_guild_picker(): selection resolves the picker, timeout resolves to None.
- check_admin_permissions(): DM-aware resolved_guild_id branch, including the
  cross-guild rejection case flagged in the plan as the one behavior change with
  real security weight (an admin of guild A must not pass a check resolved
  against guild B).
- QapBot.on_message(): DM free-text fallback reply; guild messages and the bot's
  own messages are left alone.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")


class _FakeCache:
    def __init__(self) -> None:
        self.user_accounts: Dict[str, Dict[str, Any]] = {}
        self.server_config: Dict[str, Dict[str, Any]] = {}
        self.subscriptions: Dict[str, Dict[str, Any]] = {}
        self.clan_families: Dict[str, Dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# resolve_guild_context()
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_resolve_guild_context_guild_invoked_is_passthrough(mock_interaction, monkeypatch):
    from qapbot.QBdiscocmdshelper import resolve_guild_context
    import qapbot.QBdiscocmdshelper as helper

    fake_cache = _FakeCache()
    monkeypatch.setattr(helper, "CACHE", fake_cache)

    # interaction.guild is already set by the fixture — must return unchanged
    # and never touch CACHE.
    assert await resolve_guild_context(mock_interaction) == mock_interaction.guild.id
    assert fake_cache.user_accounts == {}


@pytest.mark.discord
@pytest.mark.asyncio
async def test_resolve_guild_context_dm_no_linked_accounts_returns_none(mock_interaction, monkeypatch):
    from qapbot.QBdiscocmdshelper import resolve_guild_context
    import qapbot.QBdiscocmdshelper as helper

    mock_interaction.guild = None
    fake_cache = _FakeCache()
    monkeypatch.setattr(helper, "CACHE", fake_cache)

    assert await resolve_guild_context(mock_interaction) is None


@pytest.mark.discord
@pytest.mark.asyncio
async def test_resolve_guild_context_dm_single_match_resolves_silently(mock_interaction, monkeypatch):
    from qapbot.QBdiscocmdshelper import resolve_guild_context
    import qapbot.QBdiscocmdshelper as helper

    mock_interaction.guild = None
    fake_cache = _FakeCache()
    fake_cache.user_accounts[str(mock_interaction.user.id)] = {
        "players": [{"player_tag": "#P1", "current_clan_tag": "#CLAN1"}]
    }
    fake_cache.server_config["111"] = {"member_clans": ["#CLAN1"], "member_families": []}
    fake_cache.server_config["222"] = {"member_clans": ["#CLAN2"], "member_families": []}
    monkeypatch.setattr(helper, "CACHE", fake_cache)

    assert await resolve_guild_context(mock_interaction) == 111


@pytest.mark.discord
@pytest.mark.asyncio
async def test_resolve_guild_context_dm_linked_but_no_guild_configured_returns_none(mock_interaction, monkeypatch):
    from qapbot.QBdiscocmdshelper import resolve_guild_context
    import qapbot.QBdiscocmdshelper as helper

    mock_interaction.guild = None
    fake_cache = _FakeCache()
    fake_cache.user_accounts[str(mock_interaction.user.id)] = {
        "players": [{"player_tag": "#P1", "current_clan_tag": "#UNCONFIGURED"}]
    }
    fake_cache.server_config["111"] = {"member_clans": ["#CLAN1"], "member_families": []}
    monkeypatch.setattr(helper, "CACHE", fake_cache)

    assert await resolve_guild_context(mock_interaction) is None


@pytest.mark.discord
@pytest.mark.asyncio
async def test_resolve_guild_context_dm_multiple_matches_delegates_to_picker(mock_interaction, monkeypatch):
    from qapbot.QBdiscocmdshelper import resolve_guild_context
    import qapbot.QBdiscocmdshelper as helper

    mock_interaction.guild = None
    fake_cache = _FakeCache()
    fake_cache.user_accounts[str(mock_interaction.user.id)] = {
        "players": [{"player_tag": "#P1", "current_clan_tag": "#CLAN1"}]
    }
    fake_cache.server_config["111"] = {"member_clans": ["#CLAN1"], "member_families": []}
    fake_cache.server_config["222"] = {"member_clans": ["#CLAN1"], "member_families": []}
    monkeypatch.setattr(helper, "CACHE", fake_cache)

    captured_guild_ids = {}

    async def _fake_picker(interaction, guild_ids):
        captured_guild_ids["value"] = sorted(guild_ids)
        return 999

    monkeypatch.setattr(helper, "_prompt_dm_guild_picker", _fake_picker)

    assert await resolve_guild_context(mock_interaction) == 999
    assert captured_guild_ids["value"] == [111, 222]


# ---------------------------------------------------------------------------
# _prompt_dm_guild_picker()
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_prompt_dm_guild_picker_resolves_from_selection(mock_interaction, monkeypatch):
    from qapbot.QBdiscocmdshelper import _prompt_dm_guild_picker
    import qapbot.QBdiscocmdshelper as helper

    fake_cache = _FakeCache()
    monkeypatch.setattr(helper, "CACHE", fake_cache)

    mock_interaction.guild = None
    mock_interaction.client = MagicMock()
    mock_interaction.client.get_guild = MagicMock(return_value=None)  # unresolved name -> falls back to str(id)
    mock_interaction.response.is_done.return_value = False
    mock_interaction.original_response = AsyncMock(return_value=MagicMock())

    sent = {}

    async def _fake_send_message(*, content, view, ephemeral):
        sent["view"] = view

    mock_interaction.response.send_message = _fake_send_message

    task = asyncio.ensure_future(_prompt_dm_guild_picker(mock_interaction, [111, 222]))
    await asyncio.sleep(0)
    await asyncio.sleep(0)  # let the coroutine reach the awaited future

    view = sent["view"]
    assert {opt.value for opt in view.select.options} == {"111", "222"}

    pick_interaction = AsyncMock()
    pick_interaction.response = AsyncMock()
    await view.callback_fn(pick_interaction, "222")

    resolved = await asyncio.wait_for(task, timeout=2)
    assert resolved == 222
    pick_interaction.response.edit_message.assert_awaited_once()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_prompt_dm_guild_picker_timeout_resolves_none(mock_interaction, monkeypatch):
    from qapbot.QBdiscocmdshelper import _prompt_dm_guild_picker
    import qapbot.QBdiscocmdshelper as helper

    fake_cache = _FakeCache()
    monkeypatch.setattr(helper, "CACHE", fake_cache)

    mock_interaction.guild = None
    mock_interaction.client = MagicMock()
    mock_interaction.client.get_guild = MagicMock(return_value=None)
    mock_interaction.response.is_done.return_value = False
    mock_interaction.original_response = AsyncMock(return_value=MagicMock())

    sent = {}

    async def _fake_send_message(*, content, view, ephemeral):
        sent["view"] = view

    mock_interaction.response.send_message = _fake_send_message

    task = asyncio.ensure_future(_prompt_dm_guild_picker(mock_interaction, [111, 222]))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    view = sent["view"]
    view.message = None  # skip the delete-on-timeout branch, not under test here
    await view.on_timeout()

    resolved = await asyncio.wait_for(task, timeout=2)
    assert resolved is None


# ---------------------------------------------------------------------------
# check_admin_permissions() — DM-aware resolved_guild_id branch
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_check_admin_permissions_dm_without_resolved_guild_unchanged(mock_interaction):
    """No resolved_guild_id passed (Phase 0b not yet wired for this call site) —
    behaves exactly as before: falls straight to the bot-admin check."""
    from qapbot.QBdiscocmdshelper import check_admin_permissions

    mock_interaction.guild = None
    assert await check_admin_permissions(mock_interaction, str(mock_interaction.user.id)) is True
    assert await check_admin_permissions(mock_interaction, "999999999") is False


@pytest.mark.discord
@pytest.mark.asyncio
async def test_check_admin_permissions_dm_resolved_guild_grants_guild_admin(mock_interaction):
    from qapbot.QBdiscocmdshelper import check_admin_permissions

    mock_interaction.guild = None
    member = MagicMock()
    member.guild_permissions.administrator = True
    guild = MagicMock()
    guild.fetch_member = AsyncMock(return_value=member)
    mock_interaction.client = MagicMock()
    mock_interaction.client.get_guild = MagicMock(return_value=guild)

    # Not the configured bot admin — only passes via the resolved-guild admin check.
    result = await check_admin_permissions(mock_interaction, "999999999", resolved_guild_id=555)
    assert result is True
    guild.fetch_member.assert_awaited_once_with(mock_interaction.user.id)


@pytest.mark.discord
@pytest.mark.asyncio
async def test_check_admin_permissions_dm_resolved_guild_member_not_admin_falls_through(mock_interaction):
    from qapbot.QBdiscocmdshelper import check_admin_permissions

    mock_interaction.guild = None
    member = MagicMock()
    member.guild_permissions.administrator = False
    guild = MagicMock()
    guild.fetch_member = AsyncMock(return_value=member)
    mock_interaction.client = MagicMock()
    mock_interaction.client.get_guild = MagicMock(return_value=guild)

    assert await check_admin_permissions(mock_interaction, "999999999", resolved_guild_id=555) is False
    assert await check_admin_permissions(mock_interaction, str(mock_interaction.user.id), resolved_guild_id=555) is True


@pytest.mark.discord
@pytest.mark.asyncio
async def test_check_admin_permissions_dm_cross_guild_rejection(mock_interaction):
    """Security case called out explicitly in the plan: an admin of guild A must
    not pass a permission check resolved against guild B they aren't a member of."""
    from qapbot.QBdiscocmdshelper import check_admin_permissions

    mock_interaction.guild = None
    guild_b = MagicMock()
    guild_b.fetch_member = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "Unknown Member"))
    mock_interaction.client = MagicMock()
    mock_interaction.client.get_guild = MagicMock(return_value=guild_b)

    # Caller is not the configured bot admin either — must end up False, never
    # silently granted guild-admin just because *some* guild_id was resolved.
    assert await check_admin_permissions(mock_interaction, "999999999", resolved_guild_id=555) is False


@pytest.mark.discord
@pytest.mark.asyncio
async def test_check_admin_permissions_dm_resolved_guild_not_cached_falls_through(mock_interaction):
    """resolved_guild_id doesn't correspond to a guild the bot's cache knows about
    (e.g. stale/bad id) — must not raise, just fall through to the bot-admin check."""
    from qapbot.QBdiscocmdshelper import check_admin_permissions

    mock_interaction.guild = None
    mock_interaction.client = MagicMock()
    mock_interaction.client.get_guild = MagicMock(return_value=None)

    assert await check_admin_permissions(mock_interaction, "999999999", resolved_guild_id=555) is False
    assert await check_admin_permissions(mock_interaction, str(mock_interaction.user.id), resolved_guild_id=555) is True


# ---------------------------------------------------------------------------
# QapBot.on_message()
# ---------------------------------------------------------------------------

@pytest.fixture()
def qapbot_module():
    import QapBot  # noqa: E402  (module-level code is side-effect-light; see test_periodic_main_control.py)
    return QapBot


def _make_message(*, guild, is_bot: bool, content: str = "hello"):
    message = AsyncMock()
    message.guild = guild
    message.author = MagicMock()
    message.author.bot = is_bot
    message.author.id = 42
    message.content = content
    message.channel = AsyncMock()
    return message


@pytest.fixture()
def fake_bot(qapbot_module, monkeypatch):
    """Replaces the whole QBcore.bot object (not just an attribute on whatever
    it currently is) so this test is independent of other tests' bot-state
    mutations — QBcore is a shared module-level singleton across the session."""
    bot = MagicMock()
    bot.process_commands = AsyncMock()
    monkeypatch.setattr(qapbot_module.QBcore, "bot", bot)
    return bot


@pytest.mark.discord
@pytest.mark.asyncio
async def test_on_message_dm_free_text_sends_fallback_reply(qapbot_module, fake_bot):
    message = _make_message(guild=None, is_bot=False)

    await qapbot_module.on_message(message)

    message.channel.send.assert_awaited_once()
    sent_text = message.channel.send.await_args.args[0]
    assert isinstance(sent_text, str) and sent_text
    fake_bot.process_commands.assert_awaited_once_with(message)


@pytest.mark.discord
@pytest.mark.asyncio
async def test_on_message_ignores_bots_own_messages(qapbot_module, fake_bot):
    message = _make_message(guild=None, is_bot=True)

    await qapbot_module.on_message(message)

    message.channel.send.assert_not_awaited()
    fake_bot.process_commands.assert_awaited_once_with(message)


@pytest.mark.discord
@pytest.mark.asyncio
async def test_on_message_guild_message_skips_dm_reply(qapbot_module, fake_bot):
    guild = MagicMock()
    message = _make_message(guild=guild, is_bot=False)

    await qapbot_module.on_message(message)

    message.channel.send.assert_not_awaited()
    fake_bot.process_commands.assert_awaited_once_with(message)
