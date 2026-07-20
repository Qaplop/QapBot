from __future__ import annotations
# pyright: reportUnknownParameterType=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportMissingParameterType=false

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class _FakeCache:
    def __init__(self) -> None:
        self.clan_name_cache: dict[str, dict[str, str]] = {}
        self.server_config: dict[str, dict[str, list[str]]] = {}
        self.clan_families: dict[str, dict[str, object]] = {}
        self.coc_clan_cache = SimpleNamespace(get_clan=AsyncMock())

    def get_clan_name(self, clan_tag: str, default: str = "Unknown") -> str:
        return self.clan_name_cache.get(clan_tag, {}).get("name", default)


@pytest.mark.discord
@pytest.mark.asyncio
async def test_add_clan_by_search_invalid_parent_view(monkeypatch: pytest.MonkeyPatch, mock_interaction):
    import qapbot.ui_clan_management as ui
    import qapbot.cache_manager as cache_manager_module

    monkeypatch.setattr(cache_manager_module, "CACHE", _FakeCache())

    result = await ui.add_clan_by_search("#ABC123", object(), mock_interaction)

    assert result["success"] is False
    assert result["show_selection"] is False


@pytest.mark.discord
@pytest.mark.asyncio
async def test_add_clan_by_search_invalid_tag_sends_error(monkeypatch: pytest.MonkeyPatch, mock_interaction):
    import qapbot.ui_clan_management as ui
    import qapbot.cache_manager as cache_manager_module

    parent_view = SimpleNamespace(family_clans=[])
    monkeypatch.setattr(cache_manager_module, "CACHE", _FakeCache())

    result = await ui.add_clan_by_search("#BAD!", parent_view, mock_interaction)

    assert result["success"] is False
    mock_interaction.response.send_message.assert_awaited_once()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_add_clan_by_search_tag_already_in_list(monkeypatch: pytest.MonkeyPatch, mock_interaction):
    import qapbot.ui_clan_management as ui
    import qapbot.cache_manager as cache_manager_module

    parent_view = SimpleNamespace(family_clans=["#ABC123"])
    monkeypatch.setattr(cache_manager_module, "CACHE", _FakeCache())

    result = await ui.add_clan_by_search("#ABC123", parent_view, mock_interaction)

    assert result["success"] is False
    assert "already" in result["message"].lower()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_add_clan_by_search_known_tag_adds_directly(monkeypatch: pytest.MonkeyPatch, mock_interaction):
    import qapbot.ui_clan_management as ui
    import qapbot.cache_manager as cache_manager_module

    cache = _FakeCache()
    cache.clan_name_cache = {"#ABC123": {"name": "Alpha"}}
    monkeypatch.setattr(cache_manager_module, "CACHE", cache)

    parent_view = SimpleNamespace(family_clans=[])
    result = await ui.add_clan_by_search("#ABC123", parent_view, mock_interaction)

    assert result["success"] is True
    assert parent_view.family_clans == ["#ABC123"]


@pytest.mark.discord
@pytest.mark.asyncio
async def test_add_clan_by_search_unknown_tag_fetches_api(monkeypatch: pytest.MonkeyPatch, mock_interaction):
    import qapbot.ui_clan_management as ui
    import qapbot.cache_manager as cache_manager_module

    cache = _FakeCache()
    cache.coc_clan_cache.get_clan = AsyncMock(return_value=SimpleNamespace(name="FetchedClan"))
    monkeypatch.setattr(cache_manager_module, "CACHE", cache)

    parent_view = SimpleNamespace(family_clans=[])
    result = await ui.add_clan_by_search("#XYZ999", parent_view, mock_interaction)

    assert result["success"] is True
    assert parent_view.family_clans == ["#XYZ999"]
    mock_interaction.response.defer.assert_awaited_once()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_add_clan_by_search_name_exact_match_autoads(monkeypatch: pytest.MonkeyPatch, mock_interaction):
    import qapbot.ui_clan_management as ui
    import qapbot.cache_manager as cache_manager_module

    cache = _FakeCache()
    cache.clan_name_cache = {
        "#AAA111": {"name": "Alpha Clan"},
        "#BBB222": {"name": "Beta Clan"},
    }
    monkeypatch.setattr(cache_manager_module, "CACHE", cache)

    parent_view = SimpleNamespace(family_clans=[])
    result = await ui.add_clan_by_search("Alpha Clan", parent_view, mock_interaction)

    assert result["success"] is True
    assert parent_view.family_clans == ["#AAA111"]


@pytest.mark.discord
@pytest.mark.asyncio
async def test_add_clan_by_search_no_matches_sends_error(monkeypatch: pytest.MonkeyPatch, mock_interaction):
    import qapbot.ui_clan_management as ui
    import qapbot.cache_manager as cache_manager_module

    cache = _FakeCache()
    cache.clan_name_cache = {"#AAA111": {"name": "Alpha Clan"}}
    monkeypatch.setattr(cache_manager_module, "CACHE", cache)

    parent_view = SimpleNamespace(family_clans=[])
    result = await ui.add_clan_by_search("NoSuchClan", parent_view, mock_interaction)

    assert result["success"] is False
    assert result["show_selection"] is False
    mock_interaction.response.send_message.assert_awaited()
