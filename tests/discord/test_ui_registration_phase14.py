from __future__ import annotations
# pyright: reportPrivateUsage=false, reportUnknownParameterType=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

"""
Phase 14: AccountManagementView — Refresh button and TH/clan detail suffix.

Covers:
  - _build_message_content: TH + clan name suffix when data is present
  - _build_message_content: no suffix when th_level is absent
  - _on_refresh_click: changed fields → persist_user called
  - _on_refresh_click: no field changes → persist_user NOT called
  - _on_refresh_click: CoC API error → no crash, persist_user NOT called
"""

from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest

from tests.discord.workflow_harness import FakeCache, identity_t, make_interaction


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _user_player(
    tag: str,
    name: str = "Alice",
    th_level: Any = None,
    current_clan_tag: Any = None,
) -> Dict[str, Any]:
    return {
        "player_tag": tag,
        "player_name": name,
        "verified": False,
        "is_primary": False,
        "th_level": th_level,
        "current_clan_tag": current_clan_tag,
    }


class _FakePlayer:
    """Minimal CoC player object returned by CACHE.get_player()."""

    def __init__(
        self,
        name: str = "NewName",
        town_hall: int = 15,
        clan_tag: str | None = "#C1",
        role_name: str | None = "elder",
    ):
        self.name = name
        self.town_hall = town_hall
        self.clan = SimpleNamespace(tag=clan_tag) if clan_tag else None
        self.role = SimpleNamespace(name=role_name) if role_name else None


# ---------------------------------------------------------------------------
# _build_message_content — TH + clan suffix
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_build_message_content_shows_th_and_clan_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    """TH level and clan name appear in the player line when both are set."""
    import qapbot.ui_registration as ui

    cache = FakeCache()
    cache.user_accounts["123"] = {
        "players": [_user_player("#P1", th_level=15, current_clan_tag="#C1")]
    }
    cache.clan_name_cache["#C1"] = {"name": "The Clan"}
    monkeypatch.setattr(ui, "CACHE", cache)
    monkeypatch.setattr(ui, "t", identity_t)

    view = ui.AccountManagementView(user_id="123", guild_id=1, display_name="Alice")
    content = view._build_message_content()

    assert "TH15" in content
    assert "The Clan" in content


@pytest.mark.discord
@pytest.mark.asyncio
async def test_build_message_content_no_suffix_when_no_th_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """No TH/clan suffix when th_level is None and current_clan_tag is None."""
    import qapbot.ui_registration as ui

    cache = FakeCache()
    cache.user_accounts["123"] = {
        "players": [_user_player("#P1", th_level=None, current_clan_tag=None)]
    }
    monkeypatch.setattr(ui, "CACHE", cache)
    monkeypatch.setattr(ui, "t", identity_t)

    view = ui.AccountManagementView(user_id="123", guild_id=1, display_name="Alice")
    content = view._build_message_content()

    assert "TH" not in content


# ---------------------------------------------------------------------------
# _on_refresh_click
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_on_refresh_click_updates_fields_and_persists(monkeypatch: pytest.MonkeyPatch) -> None:
    """When CoC API returns changed data, fields are updated and persist_user is called."""
    import qapbot.ui_registration as ui

    cache = FakeCache()
    cache.user_accounts["123"] = {
        "players": [_user_player("#P1", name="OldName", th_level=10, current_clan_tag=None)]
    }
    cache.get_player = AsyncMock(return_value=_FakePlayer(name="NewName", town_hall=15, clan_tag="#C1"))
    monkeypatch.setattr(ui, "CACHE", cache)
    monkeypatch.setattr(ui, "t", identity_t)

    view = ui.AccountManagementView(user_id="123", guild_id=1, display_name="Alice")
    interaction = make_interaction(user_id=123)

    await view._on_refresh_click(interaction)

    # Fields updated in-memory
    player = cache.user_accounts["123"]["players"][0]
    assert player["player_name"] == "NewName"
    assert player["th_level"] == 15
    assert player["current_clan_tag"] == "#C1"
    assert player["coc_role"] == "elder"
    # Persisted
    cache.persist_user.assert_awaited_once_with("123")
    # Response edited
    interaction.edit_original_response.assert_awaited_once()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_on_refresh_click_no_changes_skips_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    """When API returns identical data, persist_user is NOT called."""
    import qapbot.ui_registration as ui

    cache = FakeCache()
    cache.user_accounts["123"] = {
        "players": [
            {
                "player_tag": "#P1",
                "player_name": "Alice",
                "verified": False,
                "is_primary": False,
                "th_level": 15,
                "current_clan_tag": "#C1",
                "coc_role": "elder",
            }
        ]
    }
    cache.get_player = AsyncMock(
        return_value=_FakePlayer(name="Alice", town_hall=15, clan_tag="#C1", role_name="elder")
    )
    monkeypatch.setattr(ui, "CACHE", cache)
    monkeypatch.setattr(ui, "t", identity_t)

    view = ui.AccountManagementView(user_id="123", guild_id=1, display_name="Alice")
    interaction = make_interaction(user_id=123)

    await view._on_refresh_click(interaction)

    cache.persist_user.assert_not_awaited()
    interaction.edit_original_response.assert_awaited_once()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_on_refresh_click_api_error_does_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """When CoC API raises an exception for a player, refresh completes gracefully."""
    import qapbot.ui_registration as ui

    cache = FakeCache()
    cache.user_accounts["123"] = {
        "players": [_user_player("#P1")]
    }
    cache.get_player = AsyncMock(side_effect=Exception("CoC API down"))
    monkeypatch.setattr(ui, "CACHE", cache)
    monkeypatch.setattr(ui, "t", identity_t)

    view = ui.AccountManagementView(user_id="123", guild_id=1, display_name="Alice")
    interaction = make_interaction(user_id=123)

    await view._on_refresh_click(interaction)

    cache.persist_user.assert_not_awaited()
    interaction.edit_original_response.assert_awaited_once()
