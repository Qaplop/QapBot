from __future__ import annotations
# pyright: reportPrivateUsage=false, reportUnknownParameterType=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest

from tests.discord.workflow_harness import FakeCache, identity_t, make_interaction


def _guild_clans_empty(_guild_id: int) -> list[str]:
    return []


def _guild_clans_one(_guild_id: int) -> list[str]:
    return ["#C1"]


@pytest.mark.discord
@pytest.mark.asyncio
async def test_show_player_search_modal_no_clans(monkeypatch: pytest.MonkeyPatch):
    import qapbot.ui_registration as ui
    import qapbot.QBdiscocmdshelper as helper

    interaction = make_interaction()
    monkeypatch.setattr(ui, "t", identity_t)
    monkeypatch.setattr(helper, "get_guild_clans_including_member_config", _guild_clans_empty)

    await ui._show_player_search_modal(interaction, interaction.guild.id)

    interaction.response.send_message.assert_awaited_once()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_show_player_search_modal_no_players(monkeypatch: pytest.MonkeyPatch):
    import qapbot.ui_registration as ui
    import qapbot.QBdiscocmdshelper as helper

    interaction = make_interaction()
    monkeypatch.setattr(ui, "t", identity_t)
    monkeypatch.setattr(helper, "get_guild_clans_including_member_config", _guild_clans_one)
    monkeypatch.setattr(helper, "get_player_list", AsyncMock(return_value=[]))

    await ui._show_player_search_modal(interaction, interaction.guild.id)

    interaction.response.send_message.assert_awaited_once()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_show_player_search_modal_sends_modal(monkeypatch: pytest.MonkeyPatch):
    import qapbot.ui_registration as ui
    import qapbot.QBdiscocmdshelper as helper

    interaction = make_interaction()
    monkeypatch.setattr(ui, "t", identity_t)
    monkeypatch.setattr(helper, "get_guild_clans_including_member_config", _guild_clans_one)
    monkeypatch.setattr(
        helper,
        "get_player_list",
        AsyncMock(return_value=[{"tag": "#P1", "name": "Alice", "is_current_member": True}]),
    )

    captured: Dict[str, Any] = {}

    class _FakeModal:
        def __init__(
            self,
            player_list: list[Dict[str, Any]],
            clan_selection_interaction: Any,
            user_id: str,
            guild_id: int,
            clans_in_guild: list[str],
        ):
            captured["player_list"] = player_list
            captured["user_id"] = user_id
            captured["guild_id"] = guild_id
            captured["clans_in_guild"] = clans_in_guild

    monkeypatch.setattr(ui, "PlayerSubstringModal", _FakeModal)

    await ui._show_player_search_modal(interaction, interaction.guild.id)

    interaction.response.send_modal.assert_awaited_once()
    assert captured["user_id"] == str(interaction.user.id)
    assert captured["guild_id"] == interaction.guild.id
    assert captured["clans_in_guild"] == ["#C1"]
    assert captured["player_list"][0]["tag"] == "#P1"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_clan_filter_callback_no_players(monkeypatch: pytest.MonkeyPatch):
    import qapbot.ui_registration as ui
    import qapbot.QBdiscocmdshelper as helper

    interaction = make_interaction()
    monkeypatch.setattr(ui, "t", identity_t)
    monkeypatch.setattr(helper, "get_player_list", AsyncMock(return_value=[]))

    await ui._clan_filter_callback(interaction, "#C1", "ali", "", interaction.guild.id, ["#C1"])

    interaction.response.send_message.assert_awaited_once()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_clan_filter_callback_too_many_after_filter(monkeypatch: pytest.MonkeyPatch):
    import qapbot.ui_registration as ui
    import qapbot.QBdiscocmdshelper as helper

    interaction = make_interaction()
    monkeypatch.setattr(ui, "t", identity_t)

    players = [{"tag": f"#P{i}", "name": f"Ali{i}", "is_current_member": False} for i in range(30)]
    monkeypatch.setattr(helper, "get_player_list", AsyncMock(return_value=players))

    await ui._clan_filter_callback(interaction, "#C1", "ali", "", interaction.guild.id, ["#C1"])

    interaction.response.send_message.assert_awaited_once()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_clan_filter_callback_single_match_registers(monkeypatch: pytest.MonkeyPatch):
    import qapbot.ui_registration as ui
    import qapbot.QBdiscocmdshelper as helper

    interaction = make_interaction()
    monkeypatch.setattr(ui, "t", identity_t)

    monkeypatch.setattr(
        helper,
        "get_player_list",
        AsyncMock(return_value=[{"tag": "#P1", "name": "Alice", "is_current_member": True}]),
    )
    register = AsyncMock()
    monkeypatch.setattr(helper, "process_player_registration", register)

    await ui._clan_filter_callback(interaction, "#C1", "ali", "token", interaction.guild.id, ["#C1"])

    register.assert_awaited_once()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_clan_filter_callback_multiple_matches_shows_select(monkeypatch: pytest.MonkeyPatch):
    import qapbot.ui_registration as ui
    import qapbot.QBdiscocmdshelper as helper

    interaction = make_interaction()
    monkeypatch.setattr(ui, "t", identity_t)

    monkeypatch.setattr(
        helper,
        "get_player_list",
        AsyncMock(
            return_value=[
                {"tag": "#P1", "name": "Alice", "is_current_member": True},
                {"tag": "#P2", "name": "Alina", "is_current_member": True},
            ]
        ),
    )
    monkeypatch.setattr(helper, "process_player_registration", AsyncMock())

    class _FakeSelectView:
        def __init__(self, **kwargs: Any):
            _ = kwargs

    monkeypatch.setattr(ui, "GenericSelectView", _FakeSelectView)

    await ui._clan_filter_callback(interaction, "#C1", "ali", "token", interaction.guild.id, ["#C1"])

    interaction.response.edit_message.assert_awaited_once()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_user_player_select_callback_calls_registration(monkeypatch: pytest.MonkeyPatch):
    import qapbot.ui_registration as ui

    interaction = make_interaction()
    register = AsyncMock()
    clan_selection_interaction = make_interaction(user_id=111)

    await ui._user_player_select_callback(
        interaction,
        selected_player_tag="#P1",
        matches=[{"tag": "#P1", "name": "Alice"}],
        api_token="tok",
        process_registration_fn=register,
        clan_selection_interaction=clan_selection_interaction,
    )

    interaction.response.defer.assert_awaited_once()
    clan_selection_interaction.delete_original_response.assert_awaited_once()
    register.assert_awaited_once()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_account_action_view_link_new_path(monkeypatch: pytest.MonkeyPatch):
    import qapbot.ui_registration as ui

    interaction = make_interaction()
    action_interaction = make_interaction(user_id=999)
    monkeypatch.setattr(ui, "t", identity_t)

    show_modal = AsyncMock()
    monkeypatch.setattr(ui, "_show_player_search_modal", show_modal)

    view = ui.AccountActionView(
        [{"player_tag": "#P1", "player_name": "Alice"}],
        interaction.guild.id,
        action_interaction,
    )

    # "Link new account" is its own button (not a select option) so pagination can use the
    # select's full option budget for unverified players.
    await view._on_link_new_click(interaction)

    show_modal.assert_awaited_once_with(interaction, interaction.guild.id, user_mentioned=True, action_view_interaction=action_interaction)


@pytest.mark.discord
@pytest.mark.asyncio
async def test_account_action_view_verify_missing_player(monkeypatch: pytest.MonkeyPatch):
    import qapbot.ui_registration as ui

    interaction = make_interaction()
    action_interaction = make_interaction(user_id=999)
    monkeypatch.setattr(ui, "t", identity_t)

    view = ui.AccountActionView(
        [{"player_tag": "#P1", "player_name": "Alice"}],
        interaction.guild.id,
        action_interaction,
    )
    view.select = AsyncMock(values=["verify:#NOPE"])  # type: ignore[assignment]

    await view._on_select(interaction)

    interaction.response.send_message.assert_awaited_once()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_verify_account_modal_submit_error_paths(monkeypatch: pytest.MonkeyPatch):
    import qapbot.ui_registration as ui

    interaction = make_interaction()
    cache = FakeCache()
    monkeypatch.setattr(ui, "CACHE", cache)
    monkeypatch.setattr(ui, "t", identity_t)

    modal = ui.VerifyAccountModal({"player_tag": "#P1", "player_name": "Alice"}, guild_id=interaction.guild.id)

    modal.coc_api_token._value = ""  # type: ignore[attr-defined]
    await modal.on_submit(interaction)
    interaction.response.send_message.assert_awaited_once()

    interaction2 = make_interaction(user_id=555)
    modal2 = ui.VerifyAccountModal({"player_tag": "#P1", "player_name": "Alice"}, guild_id=interaction.guild.id)
    modal2.coc_api_token._value = "abc"  # type: ignore[attr-defined]

    await modal2.on_submit(interaction2)
    interaction2.response.send_message.assert_awaited_once()
