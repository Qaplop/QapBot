from __future__ import annotations

from datetime import datetime
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

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
    args, kwargs = mock_interaction.followup.send.await_args
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
    fake_cache.db_manager = _FakeDb()
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
    fake_cache.db_manager = _FakeDb()
    monkeypatch.setattr(QBdiscordcmds, "SERVER_ADMIN", "BotAdmin")
    monkeypatch.setattr(QBdiscordcmds, "CACHE", fake_cache)
    monkeypatch.setattr(helper, "check_bot_admin_only", lambda interaction, server_admin: True)

    await QBdiscordcmds.list.callback(mock_interaction, action="MANAGED_CWLS", season="2026-09")  # type: ignore[arg-type]

    mock_interaction.followup.send.assert_awaited()
    args, kwargs = mock_interaction.followup.send.await_args
    assert "2026-09" in args[0]
    assert kwargs.get("ephemeral") is True
    assert kwargs.get("ephemeral") is True
