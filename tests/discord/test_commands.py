from __future__ import annotations

from datetime import datetime
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest


class _StubCocClanCache:
    def get_stats(self) -> Dict[str, Any]:
        return {"size": 0}

    def get_memory_usage_mb(self) -> float:
        return 0.0


class _FakeDbManager:
    # Methods this fake stubs. Kept in sync with WarHistoryDB via the contract
    # check below so a production rename breaks these tests loudly instead of
    # letting the stub silently drift out of date.
    _STUBBED_METHODS = ("get_global_db_statistics_sync",)

    def get_global_db_statistics_sync(self, force_refresh: bool = False) -> Dict[str, int]:
        return {"clans_count": 0, "wars_count": 0, "attacks_count": 0, "players_count": 0, "players_tracked_count": 0}

    def __getattr__(self, name: str) -> Any:
        # Any db_manager method the command code calls that this fake does not
        # explicitly stub is a real gap — fail loudly with a clear message rather
        # than returning a silently-wrong value.
        raise AttributeError(
            f"_FakeDbManager has no stub for '{name}'. The command under test now "
            f"calls a db_manager method not modelled here — add a stub for it."
        )


def test_fake_db_manager_contract_matches_real_interface() -> None:
    """Guard against silent drift: every method _FakeDbManager stubs must still
    exist on the real WarHistoryDB. A rename/removal in production breaks here."""
    from qapbot.db_manager import WarHistoryDB

    for _method in _FakeDbManager._STUBBED_METHODS:
        assert hasattr(WarHistoryDB, _method), (
            f"WarHistoryDB no longer exposes '{_method}' — update _FakeDbManager "
            f"and the commands/tests that rely on it."
        )



class _FakeCache:
    def __init__(self):
        self.subscriptions: Dict[str, Any] = {}
        self.user_accounts: Dict[str, Any] = {}
        self.leaderboard_messages: Dict[str, Any] = {}
        self.clan_families: Dict[str, Any] = {}
        self.clan_name_cache: Dict[str, Any] = {}
        self.in_war_clan_tags: set = set()
        self.coc_clan_cache = _StubCocClanCache()
        self.db_manager = _FakeDbManager()

    def format_lifetime_summary(self) -> str:
        return "Cycles completed: 0"

    def get_war_file_stats(self) -> Dict[str, int]:
        return {"total": 0, "prep": 0, "in_war": 0, "war_ended": 0, "cwl_known": 0}

    def is_player_verified(self, user_id: str, player_tag: str) -> bool:
        user_entry = self.user_accounts.get(user_id)
        if not user_entry:
            return False
        for player in user_entry.get("players", []):
            if player.get("player_tag", "") == player_tag:
                return bool(player.get("verified", False))
        return False


# ---------------------------------------------------------------------------
# /list action:Accounts discord_user:<@user> (tracker #0053, project owner's spec: let /list
# accounts optionally scope to one Discord user instead of always listing everyone).
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_list_accounts_filters_to_one_discord_user(monkeypatch: pytest.MonkeyPatch, mock_interaction):
    import QBdiscordcmds

    fake_cache = _FakeCache()
    fake_cache.user_accounts = {
        "111": {
            "display_name": "Alice",
            "players": [{"player_name": "AlicePlayer", "player_tag": "#ALICE1", "verified": True}],
        },
        "222": {
            "display_name": "Bob",
            "players": [{"player_name": "BobPlayer", "player_tag": "#BOB1", "verified": False}],
        },
    }
    monkeypatch.setattr(QBdiscordcmds, "CACHE", fake_cache)

    target_user = MagicMock()
    target_user.id = 111
    target_user.display_name = "Alice"

    await QBdiscordcmds.list.callback(mock_interaction, action="ACCOUNTS", discord_user=target_user)  # type: ignore[arg-type]

    mock_interaction.followup.send.assert_awaited()
    _, kwargs = mock_interaction.followup.send.await_args
    embed = kwargs["embed"]
    assert "Alice" in embed.author.name
    assert "AlicePlayer" in embed.description
    assert "#ALICE1" in embed.description
    assert "Bob" not in embed.description
    assert "#BOB1" not in embed.description
    assert kwargs.get("ephemeral") is True


@pytest.mark.discord
@pytest.mark.asyncio
async def test_list_accounts_no_discord_user_still_lists_everyone(monkeypatch: pytest.MonkeyPatch, mock_interaction):
    """Omitting discord_user must keep today's behavior unchanged — every account listed."""
    import QBdiscordcmds

    fake_cache = _FakeCache()
    fake_cache.user_accounts = {
        "111": {
            "display_name": "Alice",
            "players": [{"player_name": "AlicePlayer", "player_tag": "#ALICE1", "verified": True}],
        },
        "222": {
            "display_name": "Bob",
            "players": [{"player_name": "BobPlayer", "player_tag": "#BOB1", "verified": False}],
        },
    }
    monkeypatch.setattr(QBdiscordcmds, "CACHE", fake_cache)

    await QBdiscordcmds.list.callback(mock_interaction, action="ACCOUNTS")  # type: ignore[arg-type]

    mock_interaction.followup.send.assert_awaited()
    _, kwargs = mock_interaction.followup.send.await_args
    embed = kwargs["embed"]
    assert "Alice" in embed.description
    assert "Bob" in embed.description


@pytest.mark.discord
@pytest.mark.asyncio
async def test_list_accounts_legend_and_icons_match_clan_management(monkeypatch: pytest.MonkeyPatch, mock_interaction):
    """Tracker #0069: every row here is already a linked account (that's what makes it eligible
    to be listed at all), so REDX/"Unregistered" -- clan_management's icon for a player with no
    Discord link -- can never legitimately appear. The two icons that DO apply must match
    clan_management's own meaning: VERIFIED (shield) for API-verified, GCHECK for linked-but-not.
    """
    import QBdiscordcmds
    from qapbot.emojis import BotEmojis

    fake_cache = _FakeCache()
    fake_cache.user_accounts = {
        "111": {
            "display_name": "Alice",
            "players": [
                {"player_name": "VerifiedPlayer", "player_tag": "#V1", "verified": True},
                {"player_name": "UnverifiedPlayer", "player_tag": "#U1", "verified": False},
            ],
        },
    }
    monkeypatch.setattr(QBdiscordcmds, "CACHE", fake_cache)

    await QBdiscordcmds.list.callback(mock_interaction, action="ACCOUNTS")  # type: ignore[arg-type]

    mock_interaction.followup.send.assert_awaited()
    _, kwargs = mock_interaction.followup.send.await_args
    description = kwargs["embed"].description

    assert BotEmojis.VERIFIED in description  # the verified player's row
    assert BotEmojis.GCHECK in description  # the unverified-but-registered player's row
    assert BotEmojis.REDX not in description  # never applicable: nothing listed here is unlinked

    legend_line = next(line for line in description.splitlines() if "Legend" in line)
    assert "Verified" in legend_line
    assert "Registered" in legend_line
    assert "Unregistered" not in legend_line
    assert "API-Verified" not in legend_line  # old, incorrect wording


@pytest.mark.discord
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attacks_field_value,expected_apm",
    [
        ("", 2),      # left blank -- the common case -- must default to regular CW (2)
        ("2", 2),     # explicit regular CW
        ("1", 1),     # explicit CWL
        ("garbage", 2),  # anything not exactly "1" also falls back to the regular-CW default
    ],
)
async def test_war_predict_modal_apm_matches_corrected_label(
    monkeypatch: pytest.MonkeyPatch, mock_interaction, attacks_field_value, expected_apm,
):
    """Tracker #0068: regular CW is 2 attacks/member, CWL is 1 (the modal's label had this
    backwards). The parsing logic had the same inversion baked in -- fix both together so the
    corrected label text actually matches what submitting the form does."""
    import QBdiscordcmds
    import qapbot.QBdiscocmdshelper as helper

    monkeypatch.setattr(helper, "check_bot_admin_only", lambda interaction, server_admin: True)

    captured: Dict[str, Any] = {}

    async def fake_predict(clan1_tag, clan2_tag, n_players, apm):
        captured["apm"] = apm
        return "prediction result"

    monkeypatch.setattr("QBhelperfunctions.predict_war_between_clans", fake_predict)

    await QBdiscordcmds.admin.callback(mock_interaction, action="WAR_PREDICT")  # type: ignore[arg-type]

    mock_interaction.response.send_modal.assert_awaited_once()
    modal = mock_interaction.response.send_modal.await_args.args[0]

    assert "2 = regular CW, 1 = CWL" in modal.attacks_input.text

    modal.clan1.component._value = "#ABCDE12"  # type: ignore[attr-defined]
    modal.clan2.component._value = "#FGHJK34"  # type: ignore[attr-defined]
    modal.participants.component._value = "15"  # type: ignore[attr-defined]
    modal.attacks_input.component._value = attacks_field_value  # type: ignore[attr-defined]

    await modal.on_submit(mock_interaction)

    assert captured["apm"] == expected_apm


@pytest.mark.discord
@pytest.mark.asyncio
async def test_list_accounts_discord_user_with_no_accounts_shows_dedicated_message(monkeypatch: pytest.MonkeyPatch, mock_interaction):
    """A discord_user with no CACHE.user_accounts entry at all must not be conflated with the
    generic "nobody has any accounts" empty state — the message should name the actual user."""
    import QBdiscordcmds

    fake_cache = _FakeCache()
    fake_cache.user_accounts = {
        "222": {"display_name": "Bob", "players": [{"player_name": "BobPlayer", "player_tag": "#BOB1", "verified": False}]},
    }
    monkeypatch.setattr(QBdiscordcmds, "CACHE", fake_cache)

    target_user = MagicMock()
    target_user.id = 999
    target_user.mention = "<@999>"
    target_user.display_name = "NoAccountsUser"

    await QBdiscordcmds.list.callback(mock_interaction, action="ACCOUNTS", discord_user=target_user)  # type: ignore[arg-type]

    mock_interaction.followup.send.assert_awaited()
    _, kwargs = mock_interaction.followup.send.await_args
    embed = kwargs["embed"]
    assert "<@999>" in embed.description
    assert "Bob" not in embed.description
    assert kwargs.get("ephemeral") is True


@pytest.mark.discord
@pytest.mark.asyncio
async def test_status_calls_send_and_track(monkeypatch: pytest.MonkeyPatch, mock_interaction):
    import QBdiscordcmds

    captured: Dict[str, Any] = {}

    async def _send_and_track(interaction, content=None, command_name=None, embed=None, ephemeral=False):
        captured["content"] = content
        captured["command_name"] = command_name

    monkeypatch.setattr(QBdiscordcmds, "CACHE", _FakeCache())
    monkeypatch.setattr(QBdiscordcmds, "send_and_track", _send_and_track)
    monkeypatch.setattr(QBdiscordcmds, "GLOBAL_GUILD_ID", 123)
    monkeypatch.setattr(QBdiscordcmds, "get_simple_discord_stats", lambda: {"total_calls": 0, "success_rate": 0, "rate_limits": 0, "api_errors": 0})

    class _BotStub:
        start_time = datetime.now()
        last_sync = None
        fully_initialized = True

    monkeypatch.setattr(QBdiscordcmds.QBcore, "bot", _BotStub())

    # status is an app_commands.Command; call its underlying callback
    await QBdiscordcmds.status.callback(mock_interaction)  # type: ignore[arg-type]

    assert captured["command_name"] == "status"
    assert "QapBot Status" in (captured["content"] or "")


@pytest.mark.discord
@pytest.mark.asyncio
async def test_admin_check_logs_calls_scanner_when_bot_admin(monkeypatch: pytest.MonkeyPatch, mock_interaction):
    import QBdiscordcmds
    import qapbot.QBdiscocmdshelper_admin_command as admin_helpers
    import qapbot.QBdiscocmdshelper as helper

    monkeypatch.setattr(QBdiscordcmds, "SERVER_ADMIN", "BotAdmin")
    monkeypatch.setattr(QBdiscordcmds, "CACHE", _FakeCache())
    monkeypatch.setattr(QBdiscordcmds, "QBcore", MagicMock(bot=MagicMock()))

    # Make bot-admin check pass (admin() imports from helper at runtime)
    monkeypatch.setattr(helper, "check_bot_admin_only", lambda interaction, server_admin: True)

    monkeypatch.setattr(admin_helpers, "scan_logs", lambda _dir: {"files": 1})
    monkeypatch.setattr(admin_helpers, "format_log_summary", lambda _res, **kwargs: "LOG SUMMARY")

    await QBdiscordcmds.admin.callback(mock_interaction, action="CHECK_LOGS")  # type: ignore[arg-type]
    mock_interaction.followup.send.assert_awaited()
    args, kwargs = mock_interaction.followup.send.await_args
    assert "LOG SUMMARY" in args[0]
    assert kwargs.get("ephemeral") is True


@pytest.mark.discord
@pytest.mark.asyncio
async def test_admin_check_data_calls_checker_when_bot_admin(monkeypatch: pytest.MonkeyPatch, mock_interaction):
    import QBdiscordcmds
    import qapbot.QBdiscocmdshelper_admin_command as admin_helpers
    import qapbot.QBdiscocmdshelper as helper

    monkeypatch.setattr(QBdiscordcmds, "SERVER_ADMIN", "BotAdmin")
    monkeypatch.setattr(QBdiscordcmds, "CACHE", _FakeCache())
    monkeypatch.setattr(QBdiscordcmds, "QBcore", MagicMock(bot=MagicMock()))
    monkeypatch.setattr(helper, "check_bot_admin_only", lambda interaction, server_admin: True)

    monkeypatch.setattr(admin_helpers, "check_database_consistency", lambda _db, _data, **_kw: {"ok": True})
    monkeypatch.setattr(admin_helpers, "format_database_check_results", lambda _res: "DATA OK")

    await QBdiscordcmds.admin.callback(mock_interaction, action="CHECK_DATA")  # type: ignore[arg-type]
    mock_interaction.followup.send.assert_awaited()
    args, _ = mock_interaction.followup.send.await_args
    assert "DATA OK" in args[0]


# ---------------------------------------------------------------------------
# /list action:Managed CWLs (2026-08-16, project owner's spec) — cross-guild data (which OTHER
# guilds have a managed CWL for a season), same bot-admin gate as TESTERS.
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_list_managed_cwls_rejects_non_bot_admin(monkeypatch: pytest.MonkeyPatch, mock_interaction):
    import QBdiscordcmds
    import qapbot.QBdiscocmdshelper as helper

    monkeypatch.setattr(QBdiscordcmds, "SERVER_ADMIN", "BotAdmin")
    monkeypatch.setattr(QBdiscordcmds, "CACHE", _FakeCache())
    monkeypatch.setattr(helper, "check_bot_admin_only", lambda interaction, server_admin: False)

    await QBdiscordcmds.list.callback(mock_interaction, action="MANAGED_CWLS")  # type: ignore[arg-type]

    mock_interaction.followup.send.assert_awaited()
    args, kwargs = mock_interaction.followup.send.await_args
    assert "bot administrator" in args[0]
    assert kwargs.get("ephemeral") is True


@pytest.mark.discord
@pytest.mark.asyncio
async def test_list_managed_cwls_defaults_to_next_upcoming_season(monkeypatch: pytest.MonkeyPatch, mock_interaction):
    import QBdiscordcmds
    import qapbot.QBdiscocmdshelper as helper
    from qapbot.QBdiscocmdshelper_cwl import resolve_current_cwl_season

    expected_season = resolve_current_cwl_season()

    class _FakeDb:
        def list_cwl_events_for_season_across_guilds_sync(self, season):
            assert season == expected_season
            return [
                {"guild_id": "111", "cwl_season": season, "status": "signup_open"},
                {"guild_id": "222", "cwl_season": season, "status": "draft"},
            ]

    fake_cache = _FakeCache()
    fake_cache.db_manager = _FakeDb()  # type: ignore[assignment]  # deliberately narrower stub for this test
    monkeypatch.setattr(QBdiscordcmds, "SERVER_ADMIN", "BotAdmin")
    monkeypatch.setattr(QBdiscordcmds, "CACHE", fake_cache)
    monkeypatch.setattr(helper, "check_bot_admin_only", lambda interaction, server_admin: True)

    guild_a = MagicMock(name="Alpha Guild")
    guild_a.name = "Alpha Guild"
    guild_b = MagicMock(name="Beta Guild")
    guild_b.name = "Beta Guild"
    bot = MagicMock()
    bot.get_guild = MagicMock(side_effect=lambda gid: {111: guild_a, 222: guild_b}.get(gid))
    monkeypatch.setattr(QBdiscordcmds, "QBcore", MagicMock(bot=bot))

    await QBdiscordcmds.list.callback(mock_interaction, action="MANAGED_CWLS")  # type: ignore[arg-type]

    mock_interaction.followup.send.assert_awaited()
    _, kwargs = mock_interaction.followup.send.await_args
    embed = kwargs["embed"]
    assert expected_season in embed.title
    assert "Alpha Guild" in embed.description
    assert "Beta Guild" in embed.description
    assert "Signup Open" in embed.description
    assert "Draft" in embed.description
    assert kwargs.get("ephemeral") is True


@pytest.mark.discord
@pytest.mark.asyncio
async def test_list_managed_cwls_rejects_invalid_season_format(monkeypatch: pytest.MonkeyPatch, mock_interaction):
    import QBdiscordcmds
    import qapbot.QBdiscocmdshelper as helper

    monkeypatch.setattr(QBdiscordcmds, "SERVER_ADMIN", "BotAdmin")
    monkeypatch.setattr(QBdiscordcmds, "CACHE", _FakeCache())
    monkeypatch.setattr(helper, "check_bot_admin_only", lambda interaction, server_admin: True)

    await QBdiscordcmds.list.callback(mock_interaction, action="MANAGED_CWLS", season="not-a-season")  # type: ignore[arg-type]

    mock_interaction.followup.send.assert_awaited()
    args, kwargs = mock_interaction.followup.send.await_args
    assert "not-a-season" in args[0]
    assert kwargs.get("ephemeral") is True


@pytest.mark.discord
@pytest.mark.asyncio
async def test_list_managed_cwls_no_data_message(monkeypatch: pytest.MonkeyPatch, mock_interaction):
    import QBdiscordcmds
    import qapbot.QBdiscocmdshelper as helper

    class _FakeDb:
        def list_cwl_events_for_season_across_guilds_sync(self, season):
            return []

    fake_cache = _FakeCache()
    fake_cache.db_manager = _FakeDb()  # type: ignore[assignment]  # deliberately narrower stub for this test
    monkeypatch.setattr(QBdiscordcmds, "SERVER_ADMIN", "BotAdmin")
    monkeypatch.setattr(QBdiscordcmds, "CACHE", fake_cache)
    monkeypatch.setattr(helper, "check_bot_admin_only", lambda interaction, server_admin: True)

    await QBdiscordcmds.list.callback(mock_interaction, action="MANAGED_CWLS", season="2026-09")  # type: ignore[arg-type]

    mock_interaction.followup.send.assert_awaited()
    args, kwargs = mock_interaction.followup.send.await_args
    assert "2026-09" in args[0]
    assert kwargs.get("ephemeral") is True
    assert kwargs.get("ephemeral") is True
