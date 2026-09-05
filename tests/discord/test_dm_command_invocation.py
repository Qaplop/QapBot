"""Tests for Phase 0b of the DM interaction foundation (CWL_ROSTER_PLANNING_PLAN.md):
per-command-group @app_commands.guild_only() rollout.

Two layers:
1. A structural regression check that the intended commands (and only those) no longer
   carry guild_only() — cheap, and catches an accidental re-add or an accidental removal
   on a command that was deliberately left guild-only.
2. Functional DM-invocation coverage for the commands that actually gained new logic in
   this phase (ping — pure decorator removal, cheapest possible smoke test; subscriptions
   server_wide=True — resolve_guild_context()-based guild resolution; leaderboard — its
   four output branches (cwlinfo/cwlinfo_comp/cwlgroup/default-text) each gained a DM
   direct-send path bypassing the tracked-posting helpers). The other four converted
   commands (status, help, list, analyse_leaguegroup, analyse_cwlopponent) had zero
   functional interaction.guild dependency per the Phase 0b audit — no code inside them
   changed, only the decorator — so the structural check plus their existing (guild-mode)
   test coverage is the right amount of testing here; a full DM-mode functional re-test of
   unchanged logic would mostly be re-testing discord.py's own decorator mechanics.
3. send_and_track()'s is_dm bypass (Phase 0b follow-up, QBdiscocmdshelper.py) — DMs skip
   the tracked-message-lifecycle bookkeeping (find/delete prior messages of this mode,
   persist new message IDs) since there's no shared-channel clutter to prevent in a
   private 1:1 history; covered directly here since it's shared infrastructure, not
   specific to any one converted command.
"""
from __future__ import annotations

import os
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")

import QBdiscordcmds  # noqa: E402


# ---------------------------------------------------------------------------
# Structural regression check
# ---------------------------------------------------------------------------

_CONVERTED = ["status", "ping", "help", "list", "subscriptions", "leaderboard", "admin"]
_CONVERTED_GROUP_COMMANDS = ["cwl_league_group", "cwl_opponent"]  # analyse_group
_LEFT_GUILD_ONLY = ["subscribe", "unsubscribe", "highlightme"]


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
def test_whois_family_is_dm_invokable():
    """Correction (17): _whois_logic()'s interaction.guild dependency turned out to be
    cosmetic-only (guild nickname/avatar override), already with a working fallback — the
    actual blocker was a separate, unnecessary `if not interaction.guild: reject` guard."""
    assert QBdiscordcmds.whois.guild_only is False
    assert QBdiscordcmds.whois_message.guild_only is False
    assert QBdiscordcmds.whois_slash.guild_only is False


@pytest.mark.discord
def test_whois_slash_user_param_is_user_typed_not_member_typed():
    """discord.Member-typed options can't resolve outside a guild — must be discord.User for
    the user= path to even be selectable from a DM."""
    annotation = QBdiscordcmds.whois_slash.callback.__annotations__["user"]
    assert "Member" not in str(annotation)
    assert "User" in str(annotation)


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
        self.clan_name_cache: Dict[str, Dict[str, Any]] = {}
        self.temp_war_metadata: Dict[str, Dict[str, Any]] = {}

    def get_all_subscriptions_flat(self) -> Dict[str, Any]:
        return {}

    def get_clan_name(self, tag: str, default: str = "") -> str:
        entry = self.clan_name_cache.get(tag)
        if isinstance(entry, dict):
            return entry.get("name", default)
        return default


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


# ---------------------------------------------------------------------------
# send_and_track()'s is_dm bypass (shared infrastructure, QBdiscocmdshelper.py)
# ---------------------------------------------------------------------------

class _FakeLeaderboardCache:
    def __init__(self) -> None:
        self.leaderboard_messages: Dict[str, Dict[str, Any]] = {}
        self.user_accounts: Dict[str, Any] = {}
        self.clan_families: Dict[str, Any] = {}
        self.subscriptions: Dict[str, Any] = {}

    async def set_leaderboard_message(self, key: str, entry: Dict[str, Any]) -> None:
        self.leaderboard_messages[key] = entry

    async def delete_leaderboard_message(self, key: str) -> None:
        self.leaderboard_messages.pop(key, None)


async def _passthrough_discord_retry(op, _name="x"):
    return await op()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_send_and_track_dm_skips_tracking_and_deletion(mock_interaction, monkeypatch):
    import qapbot.QBdiscocmdshelper as helper

    mock_interaction.guild = None
    fake_cache = _FakeLeaderboardCache()
    # A pre-existing tracked message for this channel+mode — must survive untouched, since a DM
    # send must not run the "find and delete prior messages of this mode" cleanup at all.
    fake_cache.leaderboard_messages["pre-existing"] = {
        "clan_tag": f"channel_{mock_interaction.channel.id}",
        "channel_id": str(mock_interaction.channel.id),
        "mode": "leaderboard_test",
        "message_ids": "111",
    }
    monkeypatch.setattr(helper, "CACHE", fake_cache)
    monkeypatch.setattr(helper, "discord_retry", _passthrough_discord_retry)

    await helper.send_and_track(mock_interaction, content="hello", command_name="leaderboard_test")

    mock_interaction.channel.send.assert_awaited_once_with("hello")
    # Untouched: neither deleted (cleanup skipped) nor added to (final tracking-store skipped).
    assert fake_cache.leaderboard_messages == {
        "pre-existing": {
            "clan_tag": f"channel_{mock_interaction.channel.id}",
            "channel_id": str(mock_interaction.channel.id),
            "mode": "leaderboard_test",
            "message_ids": "111",
        }
    }


@pytest.mark.discord
@pytest.mark.asyncio
async def test_send_and_track_guild_still_tracks(mock_interaction, monkeypatch):
    """Companion regression guard: guild-invoked sends must keep tracking as before."""
    import qapbot.QBdiscocmdshelper as helper

    fake_cache = _FakeLeaderboardCache()
    monkeypatch.setattr(helper, "CACHE", fake_cache)
    monkeypatch.setattr(helper, "discord_retry", _passthrough_discord_retry)

    await helper.send_and_track(mock_interaction, content="hello", command_name="leaderboard_test")

    mock_interaction.channel.send.assert_awaited_once_with("hello")
    assert len(fake_cache.leaderboard_messages) == 1
    stored = next(iter(fake_cache.leaderboard_messages.values()))
    assert stored["mode"] == "leaderboard_test"
    assert stored["message_ids"]


# ---------------------------------------------------------------------------
# leaderboard()'s DM output path
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_leaderboard_dm_default_text_sends_directly_without_tracking(mock_interaction, monkeypatch):
    """The common case: /leaderboard clan:<tag> from a DM, default mode. Must route through
    send_and_track's is_dm bypass rather than post_leaderboard_to_discord (guild-channel-typed,
    shared with the periodic broadcast system)."""
    mock_interaction.guild = None
    mock_interaction.guild_id = None

    fake_cache = _FakeLeaderboardCache()
    fake_cache.user_accounts = {}
    fake_cache.clan_families = {}  # "#CLAN1" resolved via _get_clan_tag below, not as a family
    fake_cache.subscriptions = {}

    def _fake_get_all_subscriptions_flat():
        return {}

    fake_cache.get_all_subscriptions_flat = _fake_get_all_subscriptions_flat  # type: ignore[attr-defined]
    monkeypatch.setattr(QBdiscordcmds, "CACHE", fake_cache)

    monkeypatch.setattr(QBdiscordcmds, "_get_clan_tag", lambda clan: (1, "#CLAN1"))
    monkeypatch.setattr(QBdiscordcmds, "update_clan_war_info_and_stats", AsyncMock(return_value=True))
    monkeypatch.setattr(QBdiscordcmds, "generate_leaderboard_text", lambda *a, **k: "PLAYER TABLE")

    sent_and_tracked = {}

    async def _fake_send_and_track(interaction, content=None, command_name=None, embed=None, ephemeral=False):
        sent_and_tracked["content"] = content
        sent_and_tracked["command_name"] = command_name
        sent_and_tracked["ephemeral"] = ephemeral

    monkeypatch.setattr(QBdiscordcmds, "send_and_track", _fake_send_and_track)

    post_leaderboard_mock = AsyncMock()
    monkeypatch.setattr(QBdiscordcmds, "post_leaderboard_to_discord", post_leaderboard_mock)

    await QBdiscordcmds.leaderboard.callback(
        mock_interaction, clan="#CLAN1", scope="own",  # type: ignore[arg-type]
    )

    # Routed through send_and_track (is_dm-aware), never the guild-channel-typed helper.
    post_leaderboard_mock.assert_not_awaited()
    assert sent_and_tracked["ephemeral"] is False
    assert sent_and_tracked["content"] == "```ansi\nPLAYER TABLE```"
    assert sent_and_tracked["command_name"] == "leaderboard_#CLAN1_attack"


# ---------------------------------------------------------------------------
# cleanup_channel_messages()'s DM-safety (QBdiscocmdshelper.py)
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_cleanup_channel_messages_dm_channel_no_crash(monkeypatch):
    """A DMChannel has no .name and DMChannel.guild is always None (never raises, per
    discord.py's own docs — 'provided for compatibility purposes in duck typing') — but
    channel.guild.name would still raise AttributeError on None. Regression guard for the
    log-line fix that made this DM-safe."""
    import qapbot.QBdiscocmdshelper as helper

    fake_cache = _FakeLeaderboardCache()
    monkeypatch.setattr(helper, "CACHE", fake_cache)
    monkeypatch.setattr(helper, "discord_retry", _passthrough_discord_retry)

    bot_user = MagicMock()
    bot_user.id = 999
    fake_bot = MagicMock()
    fake_bot.user = bot_user

    class _FakeDMChannel:
        id = 42
        guild = None  # matches discord.DMChannel.guild's real behavior

        async def history(self, limit=50):
            for _ in ():
                yield  # pragma: no cover — empty async generator

    result = await helper.cleanup_channel_messages(_FakeDMChannel(), fake_bot)  # type: ignore[arg-type]
    assert result == (0, 0)


# ---------------------------------------------------------------------------
# admin() — DM invocation
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_admin_test_notify_rejects_dm(mock_interaction):
    """TEST_NOTIFY's user picker is a discord.ui.UserSelect (guild-scoped component) — must
    reject DM invocation explicitly rather than showing a broken/empty picker."""
    mock_interaction.guild = None

    await QBdiscordcmds.admin.callback(mock_interaction, action="TEST_NOTIFY")  # type: ignore[arg-type]

    mock_interaction.followup.send.assert_awaited_once()
    sent_text = mock_interaction.followup.send.await_args.args[0]
    assert "server" in sent_text.lower() or "dm" in sent_text.lower()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_admin_manage_testers_rejects_dm(mock_interaction):
    """MANAGE_TESTERS' picker is the same guild-scoped discord.ui.UserSelect as TEST_NOTIFY —
    same DM-rejection requirement (2026-08-15)."""
    mock_interaction.guild = None

    await QBdiscordcmds.admin.callback(mock_interaction, action="MANAGE_TESTERS")  # type: ignore[arg-type]

    mock_interaction.followup.send.assert_awaited_once()
    sent_text = mock_interaction.followup.send.await_args.args[0]
    assert "server" in sent_text.lower() or "dm" in sent_text.lower()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_admin_manage_testers_rejects_non_bot_admin(mock_interaction, monkeypatch):
    """Only the configured bot admin may open the tester picker — a guild administrator who
    isn't the bot admin must still be rejected (stricter than check_admin_permissions)."""
    mock_interaction.user.id = 111111111
    monkeypatch.setattr(QBdiscordcmds, "SERVER_ADMIN", "123456789")  # doesn't match user.id

    await QBdiscordcmds.admin.callback(mock_interaction, action="MANAGE_TESTERS")  # type: ignore[arg-type]

    mock_interaction.followup.send.assert_awaited_once()
    sent_text = mock_interaction.followup.send.await_args.args[0]
    assert "bot administrator" in sent_text.lower()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_admin_cleanup_messages_works_from_dm_for_bot_admin(mock_interaction, monkeypatch):
    """CLEANUP_MESSAGES has no modal (unlike REMOVE_CLAN/DEBUG_MESSAGE), so it's fully
    resolve_guild_context()-aware; a configured bot-admin must be able to run it from a DM."""
    mock_interaction.guild = None
    mock_interaction.user.id = 123456789  # matches SERVER_ADMIN fixture convention below
    # See test_subscriptions_server_wide_dm_not_linked's comment above: response.is_done()
    # is synchronous in real discord.py; override the AsyncMock-inherited async default.
    mock_interaction.response.is_done = MagicMock(return_value=False)

    fake_cache = _FakeLeaderboardCache()
    fake_cache.user_accounts = {}  # type: ignore[attr-defined]  # resolve_guild_context() reads this
    monkeypatch.setattr(QBdiscordcmds, "CACHE", fake_cache)
    import qapbot.QBdiscocmdshelper as helper
    monkeypatch.setattr(helper, "CACHE", fake_cache)
    monkeypatch.setattr(QBdiscordcmds, "SERVER_ADMIN", "123456789")

    cleanup_mock = AsyncMock(return_value="Deleted 0 orphaned bot messages in this channel.")
    monkeypatch.setattr(
        "qapbot.QBdiscocmdshelper_admin_command.handle_cleanup_messages_channel", cleanup_mock
    )

    await QBdiscordcmds.admin.callback(mock_interaction, action="CLEANUP_MESSAGES")  # type: ignore[arg-type]

    cleanup_mock.assert_awaited_once()
    mock_interaction.followup.send.assert_awaited()


# ---------------------------------------------------------------------------
# help() — DM-filtered command listing
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_help_dm_filters_to_dm_available_commands(mock_interaction, monkeypatch):
    mock_interaction.guild = None
    mock_interaction.guild_id = None
    mock_interaction.client = MagicMock()
    mock_interaction.client.application_id = 0

    await QBdiscordcmds.help.callback(mock_interaction)  # type: ignore[arg-type]

    mock_interaction.followup.send.assert_awaited_once()
    embed = mock_interaction.followup.send.await_args.kwargs.get("embed")
    assert embed is not None
    field_values = " ".join(f.value for f in embed.fields)
    # DM-available commands appear...
    assert "/status" in field_values or "status" in field_values
    # ...guild-only commands (e.g. subscribe, highlightme, clan management) do not.
    assert "/subscribe`" not in field_values
    assert "/highlightme`" not in field_values


@pytest.mark.discord
@pytest.mark.asyncio
async def test_help_dm_rejects_detail_request_for_guild_only_command(mock_interaction, monkeypatch):
    mock_interaction.guild = None
    mock_interaction.guild_id = None

    await QBdiscordcmds.help.callback(mock_interaction, command="subscribe")  # type: ignore[arg-type]

    mock_interaction.followup.send.assert_awaited_once()
    embed = mock_interaction.followup.send.await_args.kwargs.get("embed")
    assert embed is not None
    assert embed.color == discord.Color.red()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_help_command_autocomplete_dm_excludes_guild_only(mock_interaction):
    mock_interaction.guild = None

    choices = await QBdiscordcmds.help_command_autocomplete(mock_interaction, "")  # type: ignore[arg-type]

    values = {c.value for c in choices}
    assert "status" in values
    assert "subscribe" not in values
    assert "highlightme" not in values
    assert "whois" in values  # whois is DM-invokable now — see test_whois_family_is_dm_invokable


@pytest.mark.discord
@pytest.mark.asyncio
async def test_help_command_autocomplete_guild_includes_all():
    guild_interaction = MagicMock()
    guild_interaction.guild = MagicMock()

    choices = await QBdiscordcmds.help_command_autocomplete(guild_interaction, "")  # type: ignore[arg-type]

    values = {c.value for c in choices}
    assert "subscribe" in values


# ---------------------------------------------------------------------------
# list() — Testers action (2026-08-15)
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_list_testers_rejects_non_bot_admin(mock_interaction, monkeypatch):
    mock_interaction.user.id = 111111111
    monkeypatch.setattr(QBdiscordcmds, "SERVER_ADMIN", "123456789")  # doesn't match user.id

    await QBdiscordcmds.list.callback(mock_interaction, action="TESTERS")  # type: ignore[arg-type]

    mock_interaction.followup.send.assert_awaited_once()
    sent_text = mock_interaction.followup.send.await_args.args[0]
    assert "bot administrator" in sent_text.lower()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_list_testers_shows_server_admin_and_testers_together(mock_interaction, monkeypatch):
    """The bot admin always appears (never stored in CACHE.testers, see /admin MANAGE_TESTERS'
    comment on why it's excluded there) alongside whoever's actually enrolled."""
    mock_interaction.user.id = 123456789
    monkeypatch.setattr(QBdiscordcmds, "SERVER_ADMIN", "123456789")
    monkeypatch.setattr(QBdiscordcmds.CACHE, "testers", {"222", "333"})

    await QBdiscordcmds.list.callback(mock_interaction, action="TESTERS")  # type: ignore[arg-type]

    mock_interaction.followup.send.assert_awaited_once()
    embed = mock_interaction.followup.send.await_args.kwargs.get("embed")
    assert embed is not None
    assert "<@123456789>" in embed.description
    assert "<@222>" in embed.description
    assert "<@333>" in embed.description


@pytest.mark.discord
@pytest.mark.asyncio
async def test_list_testers_empty_when_nothing_configured(mock_interaction, monkeypatch):
    mock_interaction.user.id = 123456789
    monkeypatch.setattr(QBdiscordcmds, "SERVER_ADMIN", "123456789")
    monkeypatch.setattr(QBdiscordcmds.CACHE, "testers", set())

    await QBdiscordcmds.list.callback(mock_interaction, action="TESTERS")  # type: ignore[arg-type]

    embed = mock_interaction.followup.send.await_args.kwargs.get("embed")
    # server_admin still shows even with an empty tester set.
    assert "<@123456789>" in embed.description


# ---------------------------------------------------------------------------
# subscriptions() — server_wide forced True in DM (round 3 follow-up)
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_subscriptions_dm_forces_server_wide_even_when_false(mock_interaction, monkeypatch):
    """"Current channel" is the DM itself in a DM, never a meaningful subscription scope — must
    be forced to the server_wide path regardless of what the caller passed (including the
    parameter's own default of False)."""
    mock_interaction.guild = None
    mock_interaction.response.is_done = MagicMock(return_value=False)

    fake_cache = _FakeCache()
    fake_cache.user_accounts[str(mock_interaction.user.id)] = {
        "players": [{"player_tag": "#P1", "current_clan_tag": "#CLAN1"}]
    }
    fake_cache.server_config["555"] = {"member_clans": ["#CLAN1"], "member_families": []}
    fake_cache.subscriptions["555"] = {}
    monkeypatch.setattr(QBdiscordcmds, "CACHE", fake_cache)
    import qapbot.QBdiscocmdshelper as helper
    monkeypatch.setattr(helper, "CACHE", fake_cache)

    fake_guild = MagicMock()
    fake_guild.id = 555
    fake_guild.name = "TestGuild"
    fake_bot = MagicMock()
    fake_bot.get_guild = MagicMock(return_value=fake_guild)
    monkeypatch.setattr(QBdiscordcmds.QBcore, "bot", fake_bot)

    sent = {}

    async def _fake_send_and_track(interaction, content=None, command_name=None, embed=None, ephemeral=False):
        sent["content"] = content

    monkeypatch.setattr(QBdiscordcmds, "send_and_track", _fake_send_and_track)

    # server_wide left at its default (False) — the DM override must still kick in.
    await QBdiscordcmds.subscriptions.callback(mock_interaction)  # type: ignore[arg-type]

    fake_bot.get_guild.assert_called_once_with(555)
    assert sent.get("content") == "This guild has no subscriptions."


# ---------------------------------------------------------------------------
# whois — DM invocation (round 3: made DM-invokable after further review)
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_whois_logic_dm_falls_back_to_user_display_info(mock_interaction, monkeypatch):
    """interaction.guild is None in a DM — must not crash, and must fall back to the plain
    Discord username/avatar instead of a guild member's nickname/avatar override."""
    _whois_logic = QBdiscordcmds._whois_logic

    mock_interaction.guild = None
    fake_cache = _FakeCache()
    monkeypatch.setattr(QBdiscordcmds, "CACHE", fake_cache)

    target = MagicMock()
    target.id = 555
    target.name = "TargetUser"
    target.display_avatar.url = "https://example.com/avatar.png"

    await _whois_logic(mock_interaction, target)

    mock_interaction.followup.send.assert_awaited_once()
    embed = mock_interaction.followup.send.await_args.kwargs.get("embed")
    assert embed is not None
    assert "TargetUser" in embed.title


# ---------------------------------------------------------------------------
# leaderboard/analyse clan autocomplete — union of the DM caller's guilds (round 3)
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_leaderboard_clan_autocomplete_dm_offers_all_linked_guilds_clans(mock_interaction, monkeypatch):
    mock_interaction.guild_id = None
    fake_cache = _FakeCache()
    fake_cache.user_accounts[str(mock_interaction.user.id)] = {
        "players": [
            {"player_tag": "#P1", "current_clan_tag": "#CLANA"},
            {"player_tag": "#P2", "current_clan_tag": "#CLANB"},
        ]
    }
    fake_cache.server_config["111"] = {"member_clans": ["#CLANA"], "member_families": []}
    fake_cache.server_config["222"] = {"member_clans": ["#CLANB"], "member_families": []}
    fake_cache.subscriptions["111"] = {"chanA": [{"clan_tag": "#CLANA"}]}
    fake_cache.subscriptions["222"] = {"chanB": [{"clan_tag": "#CLANB"}]}
    fake_cache.clan_name_cache = {"#CLANA": {"name": "Clan A"}, "#CLANB": {"name": "Clan B"}}
    monkeypatch.setattr(QBdiscordcmds, "CACHE", fake_cache)
    import qapbot.QBdiscocmdshelper as helper
    monkeypatch.setattr(helper, "CACHE", fake_cache)
    # get_clan_family_autocomplete_choices() does its own `from qapbot.cache_manager import
    # CACHE` inside its body (not the module-level reference above) — must patch the actual
    # cache_manager singleton too, or it silently reads real (empty-in-tests) data.
    import qapbot.cache_manager as cache_manager_module
    monkeypatch.setattr(cache_manager_module, "CACHE", fake_cache)

    choices = await QBdiscordcmds.leaderboard_clan_autocomplete(mock_interaction, "")  # type: ignore[arg-type]

    values = {c.value for c in choices}
    assert values == {"#CLANA", "#CLANB"}


@pytest.mark.discord
@pytest.mark.asyncio
async def test_leaderboard_clan_autocomplete_guild_unaffected(mock_interaction, monkeypatch):
    """Regression guard: guild-invoked autocomplete keeps using interaction.guild_id directly,
    never touching CACHE.user_accounts at all."""
    mock_interaction.guild_id = 999
    fake_cache = _FakeCache()
    fake_cache.subscriptions["999"] = {"chan": [{"clan_tag": "#CLANC"}]}
    fake_cache.clan_name_cache = {"#CLANC": {"name": "Clan C"}}
    monkeypatch.setattr(QBdiscordcmds, "CACHE", fake_cache)
    import qapbot.cache_manager as cache_manager_module
    monkeypatch.setattr(cache_manager_module, "CACHE", fake_cache)

    choices = await QBdiscordcmds.leaderboard_clan_autocomplete(mock_interaction, "")  # type: ignore[arg-type]

    values = {c.value for c in choices}
    assert values == {"#CLANC"}
    assert fake_cache.user_accounts == {}


# ---------------------------------------------------------------------------
# Benign discord.py log-noise filters (QapBot.py) — round 3
# ---------------------------------------------------------------------------

@pytest.fixture()
def qapbot_module_for_filters():
    import QapBot
    return QapBot


@pytest.mark.discord
def test_autocomplete_expired_interaction_filter_suppresses_10062(qapbot_module_for_filters):
    import logging

    record = logging.LogRecord(
        name="discord.app_commands.tree", level=logging.ERROR, pathname=__file__, lineno=1,
        msg="Ignoring exception in autocomplete for %r (Guild: %s, User: %s)",
        args=("list", None, 123), exc_info=None,
    )
    try:
        raise Exception("404 Not Found (error code: 10062): Unknown interaction")
    except Exception as e:
        record.exc_info = (type(e), e, e.__traceback__)

    filt = qapbot_module_for_filters._AutocompleteExpiredInteractionFilter()
    assert filt.filter(record) is False


@pytest.mark.discord
def test_autocomplete_expired_interaction_filter_keeps_genuine_errors(qapbot_module_for_filters):
    import logging

    record = logging.LogRecord(
        name="discord.app_commands.tree", level=logging.ERROR, pathname=__file__, lineno=1,
        msg="Ignoring exception in autocomplete for %r (Guild: %s, User: %s)",
        args=("list", None, 123), exc_info=None,
    )
    try:
        raise ValueError("a genuine bug")
    except Exception as e:
        record.exc_info = (type(e), e, e.__traceback__)

    filt = qapbot_module_for_filters._AutocompleteExpiredInteractionFilter()
    assert filt.filter(record) is True


@pytest.mark.discord
def test_discord_reconnect_filter_suppresses_reconnect_message(qapbot_module_for_filters):
    import logging

    record = logging.LogRecord(
        name="discord.client", level=logging.ERROR, pathname=__file__, lineno=1,
        msg="Attempting a reconnect in 5.00s", args=(), exc_info=None,
    )
    filt = qapbot_module_for_filters._DiscordReconnectFilter()
    assert filt.filter(record) is False


@pytest.mark.discord
def test_log_filters_are_attached_to_every_handler(qapbot_module_for_filters):
    """Regression guard for the actual bug this round fixed: a filter added via
    logging.getLogger('discord').addFilter(...) never fires for records that only propagate up
    from a child logger like discord.app_commands.tree — Python's Logger.callHandlers() walks
    ancestor HANDLERS, not ancestor LOGGER filters. Both filters must be attached to the real
    handlers instead."""
    for handler in qapbot_module_for_filters.handlers:
        filter_types = {type(f) for f in handler.filters}
        assert qapbot_module_for_filters._DiscordReconnectFilter in filter_types
        assert qapbot_module_for_filters._AutocompleteExpiredInteractionFilter in filter_types
