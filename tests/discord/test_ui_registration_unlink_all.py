from __future__ import annotations
# pyright: reportPrivateUsage=false, reportUnknownParameterType=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

"""
Tracker #45 follow-up: a user with an unbounded number of linked accounts needs a way to
unlink all of them at once, without paginating through single-unlink confirmations one at a
time. Covers the batch backend (unlink_all_players) and its confirmation view, mirroring the
existing UnlinkConfirmView / unlink_player() test coverage.
"""

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")


def _players(count: int, prefix: str = "#P") -> list[dict]:
    return [
        {"player_tag": f"{prefix}{i}", "player_name": f"Player{i}", "verified": False, "is_primary": (i == 0)}
        for i in range(count)
    ]


# ---------------------------------------------------------------------------
# unlink_all_players() — backend
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_unlink_all_moves_every_account_to_unassigned(monkeypatch: pytest.MonkeyPatch) -> None:
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper import unlink_all_players

    monkeypatch.setattr(CACHE, "user_accounts", {"111": {"players": _players(60)}})
    monkeypatch.setattr(CACHE, "persist_user", AsyncMock())

    count = await unlink_all_players("111")

    assert count == 60
    assert CACHE.user_accounts["111"]["players"] == []
    unassigned_tags = {p["player_tag"] for p in CACHE.user_accounts["UNASSIGNED"]["players"]}
    assert unassigned_tags == {f"#P{i}" for i in range(60)}
    # is_primary must be cleared on every moved account, not just the one that had it.
    assert all(not p["is_primary"] for p in CACHE.user_accounts["UNASSIGNED"]["players"])


@pytest.mark.discord
@pytest.mark.asyncio
async def test_unlink_all_persists_once_per_side_regardless_of_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of batching: O(1) DB writes, not O(n)."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper import unlink_all_players

    monkeypatch.setattr(CACHE, "user_accounts", {"111": {"players": _players(60)}})
    persist_mock = AsyncMock()
    monkeypatch.setattr(CACHE, "persist_user", persist_mock)

    await unlink_all_players("111")

    assert persist_mock.await_count == 2
    persisted_ids = {call.args[0] for call in persist_mock.await_args_list}
    assert persisted_ids == {"111", "UNASSIGNED"}


@pytest.mark.discord
@pytest.mark.asyncio
async def test_unlink_all_no_accounts_returns_zero_and_skips_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper import unlink_all_players

    monkeypatch.setattr(CACHE, "user_accounts", {"111": {"players": []}})
    persist_mock = AsyncMock()
    monkeypatch.setattr(CACHE, "persist_user", persist_mock)

    count = await unlink_all_players("111")

    assert count == 0
    persist_mock.assert_not_awaited()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_unlink_all_unknown_user_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper import unlink_all_players

    monkeypatch.setattr(CACHE, "user_accounts", {})
    persist_mock = AsyncMock()
    monkeypatch.setattr(CACHE, "persist_user", persist_mock)

    count = await unlink_all_players("nonexistent")

    assert count == 0
    persist_mock.assert_not_awaited()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_unlink_all_dedupes_against_already_unassigned_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirrors unlink_player()'s edge-case handling: a tag already sitting in the UNASSIGNED
    pool (e.g. a prior partial operation) must not be duplicated there."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper import unlink_all_players

    monkeypatch.setattr(CACHE, "user_accounts", {
        "111": {"players": _players(3)},
        "UNASSIGNED": {"display_name": "UNASSIGNED", "players": [{"player_tag": "#P1", "player_name": "Player1"}]},
    })
    monkeypatch.setattr(CACHE, "persist_user", AsyncMock())

    count = await unlink_all_players("111")

    assert count == 3
    unassigned_tags = [p["player_tag"] for p in CACHE.user_accounts["UNASSIGNED"]["players"]]
    assert sorted(unassigned_tags) == ["#P0", "#P1", "#P2"]  # no duplicate #P1


# ---------------------------------------------------------------------------
# UnlinkAllConfirmView — inline confirmation UX
# ---------------------------------------------------------------------------

def _make_view(user_id="111", guild_id=987654321, account_count=60):
    from qapbot.ui_registration import UnlinkAllConfirmView

    parent_view = MagicMock()
    parent_view._build_message_content = MagicMock(return_value="parent overview")

    return UnlinkAllConfirmView(
        user_id=user_id,
        guild_id=guild_id,
        account_count=account_count,
        parent_view=parent_view,
    )


def _make_interaction():
    interaction = MagicMock()
    interaction.guild = None  # skip role-sync branch — covered separately below
    interaction.user = "TestUser"
    interaction.response = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


@pytest.mark.discord
@pytest.mark.asyncio
async def test_on_confirm_defers_before_slow_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "qapbot.QBdiscocmdshelper.unlink_all_players", AsyncMock(return_value=60)
    )

    view = _make_view()
    interaction = _make_interaction()

    await view._on_confirm(interaction)

    interaction.response.defer.assert_awaited_once()
    _, kwargs = interaction.response.defer.call_args
    assert kwargs.get("thinking") is False
    interaction.response.edit_message.assert_not_called()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_on_confirm_calls_unlink_all_once_with_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    unlink_mock = AsyncMock(return_value=60)
    monkeypatch.setattr("qapbot.QBdiscocmdshelper.unlink_all_players", unlink_mock)

    view = _make_view(user_id="111")
    interaction = _make_interaction()

    await view._on_confirm(interaction)

    unlink_mock.assert_awaited_once_with("111")


@pytest.mark.discord
@pytest.mark.asyncio
async def test_on_confirm_shows_empty_state_with_no_view(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "qapbot.QBdiscocmdshelper.unlink_all_players", AsyncMock(return_value=60)
    )

    view = _make_view()
    interaction = _make_interaction()

    await view._on_confirm(interaction)

    interaction.edit_original_response.assert_awaited_once()
    _, kwargs = interaction.edit_original_response.call_args
    assert kwargs["view"] is None


@pytest.mark.discord
@pytest.mark.asyncio
async def test_on_confirm_syncs_roles_once_when_in_guild(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "qapbot.QBdiscocmdshelper.unlink_all_players", AsyncMock(return_value=60)
    )
    sync_mock = AsyncMock()
    monkeypatch.setattr("qapbot.guild_role_manager.sync_roles_for_user", sync_mock)

    view = _make_view(user_id="111", guild_id=987654321)
    interaction = _make_interaction()
    interaction.guild = MagicMock(id=987654321)

    await view._on_confirm(interaction)

    sync_mock.assert_awaited_once()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_on_cancel_restores_parent_view(monkeypatch: pytest.MonkeyPatch) -> None:
    view = _make_view()
    interaction = _make_interaction()

    await view._on_cancel(interaction)

    interaction.response.edit_message.assert_awaited_once()
    _, kwargs = interaction.response.edit_message.call_args
    assert kwargs["view"] is view.parent_view
    assert kwargs["content"] == "parent overview"
