from __future__ import annotations
# pyright: reportPrivateUsage=false, reportUnknownParameterType=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from types import SimpleNamespace
from typing import Any, Dict, cast
from unittest.mock import AsyncMock

import pytest


class _FakeCache:
    def __init__(self) -> None:
        self.clan_name_cache: Dict[str, Dict[str, Any]] = {}
        self.clan_families: Dict[str, Dict[str, Any]] = {}
        self.temp_war_stats: Dict[str, Dict[str, Any]] = {}
        self.clan_history: Dict[str, list[Dict[str, Any]]] = {}
        self.server_config: Dict[str, Dict[str, list[str]]] = {}
        self.leaderboard_messages: Dict[str, Dict[str, Any]] = {}
        self._channel_subs: Dict[str, list[Dict[str, Any]]] = {}

        self.persist_clan = AsyncMock()
        self.delete_leaderboard_message = AsyncMock(side_effect=self._delete_leaderboard_message)

        self.coc_clan_cache = SimpleNamespace(get_clan=AsyncMock())

    async def _delete_leaderboard_message(self, key: str) -> None:
        self.leaderboard_messages.pop(key, None)

    def get_clan_name(self, clan_tag: str, default: str = "Unknown") -> str:
        return self.clan_name_cache.get(clan_tag, {}).get("name", default)

    def get_channel_subscriptions(self, channel_id: str) -> list[Dict[str, Any]]:
        return self._channel_subs.get(channel_id, [])


def _t_clan_name(key: str, **kwargs: Any) -> str:
    return f"{key}:{kwargs.get('clan_name', '')}"


def _t_clan_tag(key: str, **kwargs: Any) -> str:
    return f"{key}:{kwargs.get('clan_tag', '')}"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_get_clan_family_autocomplete_subscribed_only(monkeypatch: pytest.MonkeyPatch) -> None:
    import qapbot.QBdiscocmdshelper as helper

    cache = _FakeCache()
    cache.clan_name_cache = {
        "#C1": {"name": "Clan One"},
        "#C2": {"name": "Clan Two"},
    }
    cache.clan_families = {
        "#FAM0000001": {"name": "Family One", "clans": ["#C1", "#C2"]},
    }
    cache._channel_subs = {
        "123": [
            {"clan_tag": "PLAYERREGISTRATION", "subscription_type": "playerregistration"},
            {"clan_tag": "#C2", "subscription_type": "attack"},
            {"clan_tag": "#FAM0000001", "subscription_type": "attack"},
        ]
    }

    monkeypatch.setattr(helper, "CACHE", cache)
    import qapbot.cache_manager as cm

    monkeypatch.setattr(cm, "CACHE", cache)

    choices = await helper.get_clan_family_autocomplete_choices(
        current="",
        channel_id="123",
        mode="subscribed_only",
        max_choices=10,
    )

    values = [c.value for c in choices]
    assert "PLAYERREGISTRATION" not in values
    assert "#C2" in values
    assert "#FAM0000001" in values


@pytest.mark.discord
@pytest.mark.asyncio
async def test_get_clan_family_autocomplete_clans_only_excludes_families(monkeypatch: pytest.MonkeyPatch) -> None:
    import qapbot.QBdiscocmdshelper as helper

    cache = _FakeCache()
    cache.clan_name_cache = {
        "#C1": {"name": "Clan One"},
        "#C2": {"name": "Clan Two"},
    }
    cache.clan_families = {
        "#FAM0000001": {"name": "Family One", "clans": ["#C1", "#C2"]},
    }

    monkeypatch.setattr(helper, "CACHE", cache)
    import qapbot.cache_manager as cm

    monkeypatch.setattr(cm, "CACHE", cache)

    choices = await helper.get_clan_family_autocomplete_choices(
        current="",
        mode="clans_only",
        max_choices=10,
    )

    values = [c.value for c in choices]
    assert "#C1" in values and "#C2" in values
    assert "#FAM0000001" not in values


@pytest.mark.discord
@pytest.mark.asyncio
async def test_cleanup_stale_messages_for_channel_deletes_only_fully_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import qapbot.QBdiscocmdshelper as helper

    cache = _FakeCache()
    cache.leaderboard_messages = {
        "k1": {"channel_id": "111", "message_ids": "10,11"},
        "k2": {"channel_id": "111", "message_ids": "12,13"},
        "k3": {"channel_id": "999", "message_ids": "20"},
    }
    monkeypatch.setattr(helper, "CACHE", cache)

    existing_ids = {12}

    async def _fetch_message(mid: int) -> object:
        if mid in existing_ids:
            return object()
        raise RuntimeError("not found")

    channel = SimpleNamespace(id=111, fetch_message=_fetch_message)

    cleaned = await helper.cleanup_stale_messages_for_channel(cast(Any, channel))

    assert cleaned == 1
    assert "k1" not in cache.leaderboard_messages
    assert "k2" in cache.leaderboard_messages
    assert "k3" in cache.leaderboard_messages


@pytest.mark.discord
@pytest.mark.asyncio
async def test_validate_and_add_clan_to_cache_already_tracked(monkeypatch: pytest.MonkeyPatch) -> None:
    import qapbot.QBdiscocmdshelper as helper
    import qapbot.i18n as i18n

    cache = _FakeCache()
    cache.clan_name_cache = {"#ABC123": {"name": "Known Clan"}}
    monkeypatch.setattr(helper, "CACHE", cache)
    monkeypatch.setattr(i18n, "t", _t_clan_name)

    success, msg, clan_name = await helper.validate_and_add_clan_to_cache("#ABC123")

    assert success is True
    assert clan_name == "Known Clan"
    assert "already_tracked" in msg


@pytest.mark.discord
@pytest.mark.asyncio
async def test_validate_and_add_clan_to_cache_success(monkeypatch: pytest.MonkeyPatch) -> None:
    import qapbot.QBdiscocmdshelper as helper
    import qapbot.i18n as i18n

    cache = _FakeCache()
    cache.coc_clan_cache.get_clan = AsyncMock(return_value=SimpleNamespace(name="Fetched Clan"))
    monkeypatch.setattr(helper, "CACHE", cache)
    monkeypatch.setattr(i18n, "t", _t_clan_name)

    success, msg, clan_name = await helper.validate_and_add_clan_to_cache("#NEW123")

    assert success is True
    assert clan_name == "Fetched Clan"
    assert cache.clan_name_cache["#NEW123"]["name"] == "Fetched Clan"
    assert "#NEW123" in cache.temp_war_stats
    assert "#NEW123" in cache.clan_history
    cache.persist_clan.assert_awaited_once_with("#NEW123")
    assert "added_to_tracking" in msg


@pytest.mark.discord
@pytest.mark.asyncio
async def test_validate_and_add_clan_to_cache_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import qapbot.QBdiscocmdshelper as helper
    import qapbot.i18n as i18n

    cache = _FakeCache()
    cache.coc_clan_cache.get_clan = AsyncMock(side_effect=RuntimeError("api down"))
    monkeypatch.setattr(helper, "CACHE", cache)
    monkeypatch.setattr(i18n, "t", _t_clan_tag)

    success, msg, clan_name = await helper.validate_and_add_clan_to_cache("#FAIL99")

    assert success is False
    assert clan_name is None
    assert "clan_not_found" in msg


@pytest.mark.discord
def test_is_player_in_member_clans_direct_and_family(monkeypatch: pytest.MonkeyPatch) -> None:
    import qapbot.QBdiscocmdshelper as helper

    cache = _FakeCache()
    cache.server_config = {
        "123": {
            "member_clans": ["#CLAN1"],
            "member_families": ["#FAM0000001"],
        }
    }
    cache.clan_families = {
        "#FAM0000001": {"name": "Family", "clans": ["#CLAN2", "#CLAN3"]}
    }
    monkeypatch.setattr(helper, "CACHE", cache)

    assert helper.is_player_in_member_clans("#CLAN1", 123) is True
    assert helper.is_player_in_member_clans("#CLAN2", 123) is True
    assert helper.is_player_in_member_clans("#NONE", 123) is False
    assert helper.is_player_in_member_clans(None, 123) is False


@pytest.mark.discord
@pytest.mark.asyncio
async def test_check_admin_permissions_member_admin_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    import qapbot.QBdiscocmdshelper as helper

    class _FakeMember:
        def __init__(self, is_admin: bool, name: str) -> None:
            self.guild_permissions = SimpleNamespace(administrator=is_admin)
            self._name = name

        def __str__(self) -> str:
            return self._name

    monkeypatch.setattr(helper.discord, "Member", _FakeMember)

    interaction_admin = SimpleNamespace(user=_FakeMember(True, "OtherUser"))
    interaction_named = SimpleNamespace(user=_FakeMember(False, "ServerAdmin"))
    interaction_denied = SimpleNamespace(user=_FakeMember(False, "OtherUser"))

    assert await helper.check_admin_permissions(cast(Any, interaction_admin), "ServerAdmin") is True
    assert await helper.check_admin_permissions(cast(Any, interaction_named), "ServerAdmin") is True
    assert await helper.check_admin_permissions(cast(Any, interaction_denied), "ServerAdmin") is False
