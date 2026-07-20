from __future__ import annotations
# pyright: reportUnknownParameterType=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from typing import Any, Dict, cast
from unittest.mock import AsyncMock

import discord
import pytest


class _FakeCache:
    def __init__(self) -> None:
        self.leaderboard_messages: Dict[str, Dict[str, Any]] = {}
        self.user_accounts: Dict[str, Dict[str, Any]] = {}
        self.clan_name_cache: Dict[str, Dict[str, str]] = {}
        self.clan_families: Dict[str, Dict[str, Any]] = {}

    async def set_leaderboard_message(self, key: str, entry: Dict[str, Any]) -> None:
        self.leaderboard_messages[key] = entry

    async def delete_leaderboard_message(self, key: str) -> None:
        self.leaderboard_messages.pop(key, None)


def _split_embed_stub(_embed: discord.Embed, max_size: int = 4000) -> list[discord.Embed]:
    _ = max_size
    return [discord.Embed(description="A"), discord.Embed(description="B")]


@pytest.mark.discord
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"content": "x", "command_name": None}, "command_name is required"),
        ({"content": "x", "embed": discord.Embed(description="y"), "command_name": "status"}, "Cannot specify both"),
        ({"command_name": "status"}, "Must specify either"),
    ],
)
async def test_send_and_track_validation_errors(mock_interaction: Any, kwargs: Dict[str, Any], expected: str):
    import qapbot.QBdiscocmdshelper as helper

    with pytest.raises(Exception) as exc:
        await helper.send_and_track(mock_interaction, **kwargs)

    assert expected in str(exc.value)


@pytest.mark.discord
@pytest.mark.asyncio
async def test_send_and_track_ephemeral_content(mock_interaction: Any, monkeypatch: pytest.MonkeyPatch):
    import qapbot.QBdiscocmdshelper as helper

    fake_cache = _FakeCache()
    monkeypatch.setattr(helper, "CACHE", fake_cache)

    await helper.send_and_track(
        mock_interaction,
        content="ephemeral text",
        command_name="status",
        ephemeral=True,
    )

    mock_interaction.followup.send.assert_awaited_once_with(content="ephemeral text", ephemeral=True)
    assert fake_cache.leaderboard_messages == {}


@pytest.mark.discord
@pytest.mark.asyncio
async def test_send_and_track_ephemeral_embed_split(mock_interaction: Any, monkeypatch: pytest.MonkeyPatch):
    import qapbot.QBdiscocmdshelper as helper

    fake_cache = _FakeCache()
    monkeypatch.setattr(helper, "CACHE", fake_cache)

    monkeypatch.setattr(helper, "_split_embed_by_description", _split_embed_stub)

    big_embed = discord.Embed(description="x" * 5001)
    await helper.send_and_track(
        mock_interaction,
        embed=big_embed,
        command_name="status",
        ephemeral=True,
    )

    assert mock_interaction.followup.send.await_count == 2
    first_call = mock_interaction.followup.send.await_args_list[0]
    second_call = mock_interaction.followup.send.await_args_list[1]
    assert first_call.kwargs["embed"].description == "A"
    assert second_call.kwargs["embed"].description == "B"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_send_and_track_status_preserves_last_two_old_entries(mock_interaction: Any, monkeypatch: pytest.MonkeyPatch):
    import qapbot.QBdiscocmdshelper as helper

    fake_cache = _FakeCache()
    fake_cache.leaderboard_messages = {
        "2026-01-01T000000.001": {
            "clan_tag": f"channel_{mock_interaction.channel.id}",
            "channel_id": str(mock_interaction.channel.id),
            "mode": "status",
            "message_ids": "101",
        },
        "2026-01-01T000000.002": {
            "clan_tag": f"channel_{mock_interaction.channel.id}",
            "channel_id": str(mock_interaction.channel.id),
            "mode": "status",
            "message_ids": "102",
        },
        "2026-01-01T000000.003": {
            "clan_tag": f"channel_{mock_interaction.channel.id}",
            "channel_id": str(mock_interaction.channel.id),
            "mode": "status",
            "message_ids": "103",
        },
    }
    monkeypatch.setattr(helper, "CACHE", fake_cache)

    async def _discord_retry(op: Any, _name: str = "x") -> Any:
        return await op()

    monkeypatch.setattr(helper, "discord_retry", _discord_retry)

    deleted_ids: list[int] = []

    async def _fetch_message(mid: int) -> Any:
        async def _delete() -> None:
            deleted_ids.append(mid)

        return type("M", (), {"delete": AsyncMock(side_effect=_delete)})()

    mock_interaction.channel.fetch_message = _fetch_message
    mock_interaction.channel.send = AsyncMock(return_value=type("S", (), {"id": 777})())

    await helper.send_and_track(mock_interaction, content="fresh", command_name="status")

    assert deleted_ids == [101]
    assert "2026-01-01T000000.001" not in fake_cache.leaderboard_messages
    assert any(v.get("message_ids") == "777" for v in fake_cache.leaderboard_messages.values())


@pytest.mark.discord
@pytest.mark.asyncio
async def test_send_and_track_embed_truncates_long_title_and_footer(mock_interaction: Any, monkeypatch: pytest.MonkeyPatch):
    import qapbot.QBdiscocmdshelper as helper

    fake_cache = _FakeCache()
    monkeypatch.setattr(helper, "CACHE", fake_cache)

    async def _discord_retry(op: Any, _name: str = "x") -> Any:
        return await op()

    monkeypatch.setattr(helper, "discord_retry", _discord_retry)

    sent_embeds: list[discord.Embed] = []

    async def _send(*, embed: discord.Embed) -> Any:
        sent_embeds.append(embed)
        return type("S", (), {"id": 888})()

    mock_interaction.channel.send = _send

    embed = discord.Embed(description="ok", title="T" * 300)
    embed.set_footer(text="F" * 3000)

    await helper.send_and_track(mock_interaction, embed=embed, command_name="other")

    assert len(sent_embeds) == 1
    assert len(sent_embeds[0].title or "") <= 256
    assert len(sent_embeds[0].footer.text or "") <= 2048


@pytest.mark.discord
@pytest.mark.asyncio
async def test_get_clan_family_autocomplete_choices_factory_modes(monkeypatch: pytest.MonkeyPatch):
    import qapbot.QBdiscocmdshelper as helper

    fake_cache = _FakeCache()
    fake_cache.clan_name_cache = {
        "#C1": {"name": "Clan One"},
        "#FAM0000001": {"name": "Family As ClanNameCache"},
    }
    fake_cache.clan_families = {
        "#FAM0000001": {"name": "Family One", "clans": ["#C1"]}
    }
    monkeypatch.setattr(helper, "CACHE", fake_cache)

    handler_clans = helper.get_clan_family_autocomplete_choices_factory(mode="clans_only")
    handler_families = helper.get_clan_family_autocomplete_choices_factory(mode="families_only")

    interaction = type("I", (), {})()

    clans = await handler_clans(cast(Any, interaction), "")
    families = await handler_families(cast(Any, interaction), "")

    clan_values = [c.value for c in clans]
    family_values = [c.value for c in families]

    assert "#C1" in clan_values
    assert "#FAM0000001" not in clan_values
    assert family_values == ["#FAM0000001"]
