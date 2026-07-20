from __future__ import annotations
# pyright: reportPrivateUsage=false

from typing import Any, Dict
from unittest.mock import AsyncMock

import discord
import pytest


class _Phase3Cache:
    def __init__(self) -> None:
        self.user_accounts: Dict[str, Dict[str, Any]] = {}
        self.clan_name_cache: Dict[str, Dict[str, Any]] = {}
        self.clan_families: Dict[str, Dict[str, Any]] = {}
        self.persist_user = AsyncMock()

    def get_clan_name(self, clan_tag: str, default: str = "Unknown") -> str:
        return self.clan_name_cache.get(clan_tag, {}).get("name", default)


@pytest.mark.discord
def test_split_message_into_chunks_preserves_code_blocks():
    from qapbot.QBdiscocmdshelper import _split_message_into_chunks

    content = "```line1\nline2\nline3\nline4\nline5```"
    chunks = _split_message_into_chunks(content, max_size=16)

    assert len(chunks) >= 2
    assert all(chunk.startswith("```") and chunk.endswith("```") for chunk in chunks)


@pytest.mark.discord
def test_split_embed_by_description_splits_and_adds_part_footer():
    from qapbot.QBdiscocmdshelper import _split_embed_by_description

    description = "\n".join(f"line-{i}" for i in range(20))
    embed = discord.Embed(title="Title", description=description, color=discord.Color.blue())
    embed.set_footer(text="original")

    parts = _split_embed_by_description(embed, max_size=40)

    assert len(parts) > 1
    assert parts[0].title == "Title"
    assert "Part" in (parts[-1].footer.text or "")


@pytest.mark.discord
def test_clan_tag_normalization_and_resolve_by_name(monkeypatch: pytest.MonkeyPatch):
    import qapbot.QBdiscocmdshelper as helper

    cache = _Phase3Cache()
    cache.clan_name_cache = {
        "#ABC123": {"name": "The Alpha Clan"},
        "#XYZ999": {"name": "Zulu Squad"},
    }
    monkeypatch.setattr(helper, "CACHE", cache)

    assert helper.normalize_clan_tag("abc123") == "#ABC123"
    assert helper.normalize_clan_tag("A!B@C") is None

    count, tag = helper._get_clan_tag("alpha")
    assert count == 1
    assert tag == "#ABC123"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_set_primary_account_updates_flags_and_persists(monkeypatch: pytest.MonkeyPatch):
    import qapbot.QBdiscocmdshelper as helper

    cache = _Phase3Cache()
    cache.user_accounts = {
        "123": {
            "players": [
                {"player_tag": "#P1", "is_primary": True},
                {"player_tag": "#P2", "is_primary": False},
            ]
        }
    }
    monkeypatch.setattr(helper, "CACHE", cache)

    result = await helper.set_primary_account("123", "#P2")

    assert result is True
    players = cache.user_accounts["123"]["players"]
    assert players[0]["is_primary"] is False
    assert players[1]["is_primary"] is True
    cache.persist_user.assert_awaited_once_with("123")


@pytest.mark.discord
@pytest.mark.asyncio
async def test_unlink_player_moves_to_unassigned_and_persists(monkeypatch: pytest.MonkeyPatch):
    import qapbot.QBdiscocmdshelper as helper

    cache = _Phase3Cache()
    cache.user_accounts = {
        "123": {
            "players": [
                {"player_tag": "#P1", "player_name": "Alice", "is_primary": True},
                {"player_tag": "#P2", "player_name": "Bob", "is_primary": False},
            ]
        }
    }
    monkeypatch.setattr(helper, "CACHE", cache)

    result = await helper.unlink_player("123", "#P1")

    assert result is True
    remaining_tags = [p["player_tag"] for p in cache.user_accounts["123"]["players"]]
    assert remaining_tags == ["#P2"]

    unassigned_players = cache.user_accounts["UNASSIGNED"]["players"]
    assert any(p["player_tag"] == "#P1" for p in unassigned_players)
    assert next(p for p in unassigned_players if p["player_tag"] == "#P1")["is_primary"] is False

    cache.persist_user.assert_any_await("123")
    cache.persist_user.assert_any_await("UNASSIGNED")
