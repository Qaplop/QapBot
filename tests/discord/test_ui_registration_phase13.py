from __future__ import annotations
# pyright: reportPrivateUsage=false, reportUnknownParameterType=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false

"""
Phase 13: Registration workflow tests — previously uncovered paths.

Covers:
  - PlayerSubstringModal.on_submit  (8 code paths)
  - VerifyAccountModal.on_submit    (happy path + failed verification)
  - RegistrationView war_notifications_button  (with existing accounts)
"""

from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest

from tests.discord.workflow_harness import FakeCache, identity_t, make_interaction


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _player(tag: str, name: str) -> Dict[str, Any]:
    """Player-list dict (used in PlayerSubstringModal.player_list)."""
    return {"tag": tag, "name": name, "is_current_member": True}


def _user_player(tag: str, name: str) -> Dict[str, Any]:
    """Player dict as stored in CACHE.user_accounts."""
    return {"player_tag": tag, "player_name": name, "verified": False, "is_primary": False}


# ===========================================================================
# PlayerSubstringModal.on_submit
# ===========================================================================


@pytest.mark.discord
@pytest.mark.asyncio
async def test_psm_single_match_calls_process_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    """One player matches the substring → process_player_registration called with that tag."""
    import qapbot.ui_registration as ui
    import qapbot.QBdiscocmdshelper as helper

    interaction = make_interaction()
    cache = FakeCache()
    monkeypatch.setattr(ui, "CACHE", cache)
    monkeypatch.setattr(ui, "t", identity_t)

    register = AsyncMock()
    monkeypatch.setattr(helper, "process_player_registration", register)

    modal = ui.PlayerSubstringModal(
        player_list=[_player("#P1", "Alice")],
        user_id=str(interaction.user.id),
        guild_id=interaction.guild.id,
        clans_in_guild=["#C1"],
    )
    modal.substring._value = "alice"  # type: ignore[attr-defined]
    modal.coc_api_token._value = ""  # type: ignore[attr-defined]

    await modal.on_submit(interaction)

    interaction.response.defer.assert_awaited_once()
    register.assert_awaited_once()
    # First positional arg after interaction should be the player tag
    call_args = register.call_args
    assert call_args.args[1] == "#P1" or call_args.kwargs.get("player_tag") == "#P1"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_psm_valid_tag_input_calls_process_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    """No list match, input looks like a CoC tag → normalize → process_player_registration."""
    import qapbot.ui_registration as ui
    import qapbot.QBdiscocmdshelper as helper
    import qapbot.cache_manager as cm

    interaction = make_interaction()
    cache = FakeCache()
    monkeypatch.setattr(ui, "CACHE", cache)
    monkeypatch.setattr(cm, "CACHE", cache)
    monkeypatch.setattr(ui, "t", identity_t)

    monkeypatch.setattr(helper, "normalize_clan_tag", lambda s: "#P99")
    monkeypatch.setattr(helper, "complete_account_linking_flow", AsyncMock())
    register = AsyncMock()
    monkeypatch.setattr(helper, "process_player_registration", register)

    modal = ui.PlayerSubstringModal(
        player_list=[],
        user_id=str(interaction.user.id),
        guild_id=interaction.guild.id,
        clans_in_guild=["#C1"],
    )
    modal.substring._value = "#P99"  # type: ignore[attr-defined]
    modal.coc_api_token._value = ""  # type: ignore[attr-defined]

    await modal.on_submit(interaction)

    register.assert_awaited_once()
    all_values = register.call_args.args + tuple(register.call_args.kwargs.values())
    assert "#P99" in all_values


@pytest.mark.discord
@pytest.mark.asyncio
async def test_psm_invalid_input_sends_error_followup(monkeypatch: pytest.MonkeyPatch) -> None:
    """No list match and not a valid tag → followup.send error, no registration."""
    import qapbot.ui_registration as ui
    import qapbot.QBdiscocmdshelper as helper
    import qapbot.cache_manager as cm

    interaction = make_interaction()
    cache = FakeCache()
    monkeypatch.setattr(ui, "CACHE", cache)
    monkeypatch.setattr(cm, "CACHE", cache)
    monkeypatch.setattr(ui, "t", identity_t)

    # normalize_clan_tag returns None → not a valid tag
    monkeypatch.setattr(helper, "normalize_clan_tag", lambda s: None)
    monkeypatch.setattr(helper, "complete_account_linking_flow", AsyncMock())
    register = AsyncMock()
    monkeypatch.setattr(helper, "process_player_registration", register)

    modal = ui.PlayerSubstringModal(
        player_list=[],
        user_id=str(interaction.user.id),
        guild_id=interaction.guild.id,
        clans_in_guild=["#C1"],
    )
    modal.substring._value = "xyzxyzxyz"  # type: ignore[attr-defined]
    modal.coc_api_token._value = ""  # type: ignore[attr-defined]

    await modal.on_submit(interaction)

    interaction.followup.send.assert_awaited_once()
    register.assert_not_awaited()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_psm_two_matches_sends_select_dropdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """2-25 matches → followup.send with a GenericSelectView for player selection."""
    import qapbot.ui_registration as ui
    import qapbot.QBdiscocmdshelper as helper

    interaction = make_interaction()
    cache = FakeCache()
    monkeypatch.setattr(ui, "CACHE", cache)
    monkeypatch.setattr(ui, "t", identity_t)
    monkeypatch.setattr(helper, "process_player_registration", AsyncMock())

    class _FakeSelectView:
        def __init__(self, **kwargs: Any) -> None:
            pass

    monkeypatch.setattr(ui, "GenericSelectView", _FakeSelectView)

    modal = ui.PlayerSubstringModal(
        player_list=[_player("#P1", "Alice"), _player("#P2", "Alina")],
        user_id=str(interaction.user.id),
        guild_id=interaction.guild.id,
        clans_in_guild=["#C1"],
    )
    modal.substring._value = "ali"  # type: ignore[attr-defined]
    modal.coc_api_token._value = ""  # type: ignore[attr-defined]

    await modal.on_submit(interaction)

    interaction.followup.send.assert_awaited_once()
    sent_view = interaction.followup.send.call_args.kwargs.get("view")
    assert isinstance(sent_view, _FakeSelectView)


@pytest.mark.discord
@pytest.mark.asyncio
async def test_psm_over25_matches_multiple_clans_shows_clan_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """>25 matches with multiple clans, not yet filtered → clan filter GenericSelectView sent."""
    import qapbot.ui_registration as ui
    import qapbot.QBdiscocmdshelper as helper

    interaction = make_interaction()
    cache = FakeCache()
    cache.get_clan_name = lambda tag, default="Unknown Clan": f"Clan ({tag})"  # type: ignore[assignment]
    monkeypatch.setattr(ui, "CACHE", cache)
    monkeypatch.setattr(ui, "t", identity_t)
    monkeypatch.setattr(helper, "process_player_registration", AsyncMock())

    class _FakeSelectView:
        def __init__(self, **kwargs: Any) -> None:
            pass

    monkeypatch.setattr(ui, "GenericSelectView", _FakeSelectView)

    players = [_player(f"#P{i}", f"Ali{i}") for i in range(30)]

    modal = ui.PlayerSubstringModal(
        player_list=players,
        user_id=str(interaction.user.id),
        guild_id=interaction.guild.id,
        clans_in_guild=["#C1", "#C2"],  # Multiple clans
        filtered_by_clan=False,
    )
    modal.substring._value = "ali"  # type: ignore[attr-defined]
    modal.coc_api_token._value = ""  # type: ignore[attr-defined]

    await modal.on_submit(interaction)

    interaction.followup.send.assert_awaited_once()
    sent_view = interaction.followup.send.call_args.kwargs.get("view")
    assert isinstance(sent_view, _FakeSelectView)


@pytest.mark.discord
@pytest.mark.asyncio
async def test_psm_over25_matches_single_clan_sends_specific_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """>25 matches with only one clan → too_many_matches_specific error, no view attached."""
    import qapbot.ui_registration as ui
    import qapbot.QBdiscocmdshelper as helper

    interaction = make_interaction()
    cache = FakeCache()
    monkeypatch.setattr(ui, "CACHE", cache)
    monkeypatch.setattr(ui, "t", identity_t)
    monkeypatch.setattr(helper, "process_player_registration", AsyncMock())

    class _FakeSelectView:
        def __init__(self, **kwargs: Any) -> None:
            pass

    monkeypatch.setattr(ui, "GenericSelectView", _FakeSelectView)

    players = [_player(f"#P{i}", f"Ali{i}") for i in range(30)]

    modal = ui.PlayerSubstringModal(
        player_list=players,
        user_id=str(interaction.user.id),
        guild_id=interaction.guild.id,
        clans_in_guild=["#C1"],  # Single clan → goes to error branch
        filtered_by_clan=False,
    )
    modal.substring._value = "ali"  # type: ignore[attr-defined]
    modal.coc_api_token._value = ""  # type: ignore[attr-defined]

    await modal.on_submit(interaction)

    interaction.followup.send.assert_awaited_once()
    # Pure error message — no view
    send_kwargs = interaction.followup.send.call_args.kwargs
    assert "view" not in send_kwargs or send_kwargs.get("view") is None


@pytest.mark.discord
@pytest.mark.asyncio
async def test_psm_no_match_own_player_calls_complete_linking(monkeypatch: pytest.MonkeyPatch) -> None:
    """No clan-player match, but input matches user's own linked player → complete_account_linking_flow."""
    import qapbot.ui_registration as ui
    import qapbot.QBdiscocmdshelper as helper
    import qapbot.cache_manager as cm

    interaction = make_interaction()
    user_id = str(interaction.user.id)
    cache = FakeCache()
    cache.user_accounts[user_id] = {"players": [_user_player("#P1", "Alice")]}
    monkeypatch.setattr(ui, "CACHE", cache)
    monkeypatch.setattr(cm, "CACHE", cache)
    monkeypatch.setattr(ui, "t", identity_t)

    complete_linking = AsyncMock()
    monkeypatch.setattr(helper, "complete_account_linking_flow", complete_linking)
    monkeypatch.setattr(helper, "normalize_clan_tag", lambda s: None)

    modal = ui.PlayerSubstringModal(
        player_list=[],  # Empty guild player list
        user_id=user_id,
        guild_id=interaction.guild.id,
        clans_in_guild=["#C1"],
    )
    modal.substring._value = "alice"  # type: ignore[attr-defined]
    modal.coc_api_token._value = ""  # type: ignore[attr-defined]

    await modal.on_submit(interaction)

    complete_linking.assert_awaited_once()


# ===========================================================================
# VerifyAccountModal.on_submit — happy path and failed verification
# ===========================================================================


@pytest.mark.discord
@pytest.mark.asyncio
async def test_verify_modal_happy_path_calls_war_notifications_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Valid token + successful verification → persist_user + check_and_prompt_war_notifications(use_followup=False)."""
    import qapbot.ui_registration as ui
    import qapbot.QBdiscocmdshelper as helper

    interaction = make_interaction()
    user_id = str(interaction.user.id)
    cache = FakeCache()
    cache.user_accounts[user_id] = {"players": [_user_player("#P1", "Alice")]}
    monkeypatch.setattr(ui, "CACHE", cache)
    monkeypatch.setattr(ui, "t", identity_t)

    monkeypatch.setattr(helper, "verify_and_update_player", AsyncMock(return_value=(True, "Verified!")))
    monkeypatch.setattr(helper, "assign_member_role", AsyncMock())
    check_notifications = AsyncMock()
    monkeypatch.setattr(helper, "check_and_prompt_war_notifications", check_notifications)

    modal = ui.VerifyAccountModal(
        {"player_tag": "#P1", "player_name": "Alice"},
        guild_id=interaction.guild.id,
    )
    modal.coc_api_token._value = "valid_token_123"  # type: ignore[attr-defined]

    await modal.on_submit(interaction)

    cache.persist_user.assert_awaited_once_with(user_id)
    check_notifications.assert_awaited_once()
    assert check_notifications.call_args.kwargs.get("use_followup") is False


@pytest.mark.discord
@pytest.mark.asyncio
async def test_verify_modal_verification_failed_sends_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Valid token provided but verification fails → response.send_message with error, no persist."""
    import qapbot.ui_registration as ui
    import qapbot.QBdiscocmdshelper as helper

    interaction = make_interaction()
    user_id = str(interaction.user.id)
    cache = FakeCache()
    cache.user_accounts[user_id] = {"players": [_user_player("#P1", "Alice")]}
    monkeypatch.setattr(ui, "CACHE", cache)
    monkeypatch.setattr(ui, "t", identity_t)

    monkeypatch.setattr(helper, "verify_and_update_player", AsyncMock(return_value=(False, "Bad token")))

    modal = ui.VerifyAccountModal(
        {"player_tag": "#P1", "player_name": "Alice"},
        guild_id=interaction.guild.id,
    )
    modal.coc_api_token._value = "wrong_token"  # type: ignore[attr-defined]

    await modal.on_submit(interaction)

    cache.persist_user.assert_not_called()
    interaction.response.send_message.assert_awaited_once()


# ===========================================================================
# RegistrationView.war_notifications_button — with existing accounts
# ===========================================================================


@pytest.mark.discord
@pytest.mark.asyncio
async def test_war_notifications_button_with_accounts_sends_unified_view(monkeypatch: pytest.MonkeyPatch) -> None:
    """User has linked accounts → defer + UnifiedNotificationView sent via followup (ephemeral)."""
    import qapbot.ui_registration as ui
    import qapbot.QBdiscocmdshelper as helper
    import qapbot.ui_notifications as ui_notif

    interaction = make_interaction()
    user_id = str(interaction.user.id)
    cache = FakeCache()
    cache.user_accounts[user_id] = {
        "players": [_user_player("#P1", "Alice")],
        "notification_settings": {
            "war_reminders": True,
            "notification_type": "all_wars",
            "notification_mode": "repeated",
        },
    }
    monkeypatch.setattr(ui, "CACHE", cache)
    monkeypatch.setattr(ui, "t", identity_t)
    monkeypatch.setattr(ui, "update_user_metadata_from_interaction", AsyncMock())

    monkeypatch.setattr(helper, "format_notification_settings", lambda *a, **kw: "settings text")

    class _FakeUnifiedView:
        def __init__(self, **kwargs: Any) -> None:
            pass

    monkeypatch.setattr(ui_notif, "UnifiedNotificationView", _FakeUnifiedView)

    view = ui.RegistrationView(guild_id=interaction.guild.id)
    button = next(c for c in view.children if getattr(c, "custom_id", "") == "registration_war_notifications")
    await button.callback(interaction)

    interaction.response.defer.assert_awaited_once()
    interaction.followup.send.assert_awaited_once()

    send_kwargs = interaction.followup.send.call_args.kwargs
    assert isinstance(send_kwargs.get("view"), _FakeUnifiedView)
    assert send_kwargs.get("ephemeral") is True
