from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import pytest


@dataclass
class _FakeClan:
    tag: str
    name: str


@dataclass
class _FakePlayer:
    tag: str
    name: str
    town_hall: int = 15
    clan: Optional[_FakeClan] = None


class _FakeCache:
    def __init__(self):
        self.user_accounts: dict[str, dict[str, Any]] = {}
        self.server_config: dict[str, dict[str, Any]] = {}
        self.clan_name_cache: dict[str, Any] = {}
        self._persisted: list[str] = []
        self.force_fresh_calls: list[tuple[str, bool]] = []

    async def persist_user(self, discord_id: str) -> None:
        self._persisted.append(str(discord_id))

    async def delete_user_account(self, discord_id: str) -> None:
        self.user_accounts.pop(str(discord_id), None)

    async def send_user_dm(self, user_id: str, message: str) -> bool:
        return True

    async def persist_clan(self, clan_tag: str) -> None:
        return None

    async def get_player(self, player_tag: str, force_fresh: bool = False) -> _FakePlayer:
        # force_fresh mirrors the real CacheManager.get_player signature (2026-08-22): the
        # linking paths under test pass it explicitly, since the name/th_level captured there
        # become persisted account state and must never come from the short-TTL cache.
        self.force_fresh_calls.append((player_tag, force_fresh))
        return _FakePlayer(tag=player_tag, name="PlayerName", clan=_FakeClan(tag="#CLAN", name="ClanName"))


@pytest.fixture
def fake_cache(monkeypatch: pytest.MonkeyPatch) -> _FakeCache:
    import qapbot.cache_manager
    import qapbot.QBdiscocmdshelper as helper

    cache = _FakeCache()
    monkeypatch.setattr(qapbot.cache_manager, "CACHE", cache)
    monkeypatch.setattr(helper, "CACHE", cache)
    return cache


class TestOwnerLookups:
    @pytest.mark.integration
    def test_get_verified_player_owner(self, fake_cache):
        from qapbot.QBdiscocmdshelper import get_verified_player_owner

        fake_cache.user_accounts["111"] = {
            "display_name": "Owner",
            "players": [{"player_tag": "#ABC123", "player_name": "X", "verified": True}],
        }

        assert get_verified_player_owner("#ABC123", requesting_user_id="222") == "Owner"
        assert get_verified_player_owner("#ABC123", requesting_user_id="111") is None

    @pytest.mark.integration
    def test_get_any_player_owner(self, fake_cache):
        from qapbot.QBdiscocmdshelper import get_any_player_owner

        fake_cache.user_accounts["111"] = {
            "display_name": "Owner",
            "players": [{"player_tag": "#ABC123", "player_name": "X", "verified": False}],
        }

        owner = get_any_player_owner("#ABC123", requesting_user_id="222")
        assert owner is not None
        assert owner[0] == "111"
        assert owner[1] == "Owner"
        assert owner[2] is False


class TestLinkingSecurity:
    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_link_rejected_when_verified_by_other(self, fake_cache):
        from qapbot.QBdiscocmdshelper import _link_player_to_user

        fake_cache.user_accounts["111"] = {
            "display_name": "Owner",
            "players": [{"player_tag": "#ABC123", "player_name": "X", "verified": True}],
        }

        ok, msg = await _link_player_to_user(
            target_user_id=222,
            player_tag_raw="#ABC123",
            display_name="NewUser",
        )
        assert ok is False
        assert "#ABC123" in msg
        assert "Owner" in msg
        assert "222" not in fake_cache.user_accounts
        assert fake_cache._persisted == []

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_link_allowed_with_api_token_override_moves_player(self, fake_cache):
        from qapbot.QBdiscocmdshelper import _link_player_to_user

        fake_cache.user_accounts["111"] = {
            "display_name": "Owner",
            "players": [{"player_tag": "#ABC123", "player_name": "X", "verified": True}],
        }

        ok, _msg = await _link_player_to_user(
            target_user_id=222,
            player_tag_raw="#ABC123",
            display_name="NewUser",
            api_token_override=True,
            api_token_user_id="111",
        )
        assert ok is True

        # Removed from old owner
        assert fake_cache.user_accounts["111"]["players"] == []
        # Added to new owner (unverified)
        new_players = fake_cache.user_accounts["222"]["players"]
        assert any(p.get("player_tag") == "#ABC123" for p in new_players)
        moved = next(p for p in new_players if p.get("player_tag") == "#ABC123")
        assert moved.get("verified") is False
        assert "111" in fake_cache._persisted
        assert "222" in fake_cache._persisted

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_linking_never_reads_player_data_from_the_short_ttl_cache(self, fake_cache):
        """2026-08-22: CACHE.get_player() gained a 60s read-through cache. The linking path
        captures player_name/th_level/current_clan_tag/coc_role and PERSISTS them as account
        state — and the role sync runs off that same data immediately afterwards — so it must
        always read live. A cached read here would let a link record a clan the player has
        already left, and hand them the wrong in-game role."""
        from qapbot.QBdiscocmdshelper import _link_player_to_user

        ok, _msg = await _link_player_to_user(
            target_user_id=222,
            player_tag_raw="#ABC123",
            display_name="NewUser",
        )
        assert ok is True

        assert fake_cache.force_fresh_calls, "linking made no get_player call at all"
        assert all(
            forced for _tag, forced in fake_cache.force_fresh_calls
        ), f"linking read player data from the cache: {fake_cache.force_fresh_calls}"
