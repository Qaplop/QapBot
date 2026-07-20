"""Extended account protection tests for security-critical ownership functions.

Covers edge cases in get_verified_player_owner and get_any_player_owner
not addressed by tests/integration/test_account_protection.py.
"""
# pyright: reportUnknownMemberType=false, reportUnknownParameterType=false
# pyright: reportMissingParameterType=false
from __future__ import annotations

from typing import Any, Dict

import pytest


@pytest.fixture
def fake_cache(monkeypatch: pytest.MonkeyPatch):
    """Lightweight fake CACHE for ownership function tests."""
    import qapbot.cache_manager
    import qapbot.QBdiscocmdshelper as helper

    class _FakeCache:
        def __init__(self) -> None:
            self.user_accounts: Dict[str, Dict[str, Any]] = {}

    cache = _FakeCache()
    monkeypatch.setattr(qapbot.cache_manager, "CACHE", cache)
    monkeypatch.setattr(helper, "CACHE", cache)
    return cache


# ---------------------------------------------------------------------------
# get_verified_player_owner edge cases
# ---------------------------------------------------------------------------

class TestGetVerifiedPlayerOwnerEdges:

    @pytest.mark.smoke
    def test_unverified_player_returns_none(self, fake_cache) -> None:
        from qapbot.QBdiscocmdshelper import get_verified_player_owner

        fake_cache.user_accounts["111"] = {
            "display_name": "Owner",
            "players": [{"player_tag": "#P1", "player_name": "X", "verified": False}],
        }
        assert get_verified_player_owner("#P1", "222") is None

    @pytest.mark.smoke
    def test_skips_unassigned_pool(self, fake_cache) -> None:
        from qapbot.QBdiscocmdshelper import get_verified_player_owner

        fake_cache.user_accounts["UNASSIGNED"] = {
            "display_name": "Pool",
            "players": [{"player_tag": "#P1", "player_name": "X", "verified": True}],
        }
        assert get_verified_player_owner("#P1", "222") is None

    @pytest.mark.smoke
    def test_self_lookup_returns_none(self, fake_cache) -> None:
        from qapbot.QBdiscocmdshelper import get_verified_player_owner

        fake_cache.user_accounts["111"] = {
            "display_name": "Self",
            "players": [{"player_tag": "#P1", "player_name": "X", "verified": True}],
        }
        # Requesting user is the owner → returns None
        assert get_verified_player_owner("#P1", "111") is None

    @pytest.mark.smoke
    def test_empty_players_list(self, fake_cache) -> None:
        from qapbot.QBdiscocmdshelper import get_verified_player_owner

        fake_cache.user_accounts["111"] = {
            "display_name": "NoPlayers",
            "players": [],
        }
        assert get_verified_player_owner("#P1", "222") is None

    @pytest.mark.smoke
    def test_none_user_entry(self, fake_cache) -> None:
        from qapbot.QBdiscocmdshelper import get_verified_player_owner

        fake_cache.user_accounts["111"] = None
        assert get_verified_player_owner("#P1", "222") is None

    @pytest.mark.smoke
    def test_players_not_a_list(self, fake_cache) -> None:
        from qapbot.QBdiscocmdshelper import get_verified_player_owner

        fake_cache.user_accounts["111"] = {
            "display_name": "BadFormat",
            "players": "not a list",
        }
        assert get_verified_player_owner("#P1", "222") is None

    @pytest.mark.smoke
    def test_multiple_users_finds_verified_owner(self, fake_cache) -> None:
        from qapbot.QBdiscocmdshelper import get_verified_player_owner

        fake_cache.user_accounts["111"] = {
            "display_name": "NotOwner",
            "players": [{"player_tag": "#OTHER", "player_name": "X", "verified": True}],
        }
        fake_cache.user_accounts["222"] = {
            "display_name": "RealOwner",
            "players": [{"player_tag": "#P1", "player_name": "X", "verified": True}],
        }
        assert get_verified_player_owner("#P1", "333") == "RealOwner"


# ---------------------------------------------------------------------------
# get_any_player_owner edge cases
# ---------------------------------------------------------------------------

class TestGetAnyPlayerOwnerEdges:

    @pytest.mark.smoke
    def test_returns_verified_status(self, fake_cache) -> None:
        from qapbot.QBdiscocmdshelper import get_any_player_owner

        fake_cache.user_accounts["111"] = {
            "display_name": "Owner",
            "players": [{"player_tag": "#P1", "player_name": "X", "verified": True}],
        }
        result = get_any_player_owner("#P1", "222")
        assert result is not None
        user_id, display_name, is_verified = result
        assert user_id == "111"
        assert display_name == "Owner"
        assert is_verified is True

    @pytest.mark.smoke
    def test_returns_unverified_link(self, fake_cache) -> None:
        from qapbot.QBdiscocmdshelper import get_any_player_owner

        fake_cache.user_accounts["111"] = {
            "display_name": "Linker",
            "players": [{"player_tag": "#P1", "player_name": "X", "verified": False}],
        }
        result = get_any_player_owner("#P1", "222")
        assert result is not None
        assert result[2] is False

    @pytest.mark.smoke
    def test_no_owner_returns_none(self, fake_cache) -> None:
        from qapbot.QBdiscocmdshelper import get_any_player_owner

        fake_cache.user_accounts["111"] = {
            "display_name": "Other",
            "players": [{"player_tag": "#OTHER", "player_name": "X"}],
        }
        assert get_any_player_owner("#P1", "222") is None

    @pytest.mark.smoke
    def test_skips_unassigned(self, fake_cache) -> None:
        from qapbot.QBdiscocmdshelper import get_any_player_owner

        fake_cache.user_accounts["UNASSIGNED"] = {
            "display_name": "Pool",
            "players": [{"player_tag": "#P1", "player_name": "X"}],
        }
        assert get_any_player_owner("#P1", "222") is None

    @pytest.mark.smoke
    def test_skips_requesting_user(self, fake_cache) -> None:
        from qapbot.QBdiscocmdshelper import get_any_player_owner

        fake_cache.user_accounts["111"] = {
            "display_name": "Self",
            "players": [{"player_tag": "#P1", "player_name": "X"}],
        }
        assert get_any_player_owner("#P1", "111") is None

    @pytest.mark.smoke
    def test_missing_display_name_uses_default(self, fake_cache) -> None:
        from qapbot.QBdiscocmdshelper import get_any_player_owner

        fake_cache.user_accounts["111"] = {
            "players": [{"player_tag": "#P1", "player_name": "X"}],
        }
        result = get_any_player_owner("#P1", "222")
        assert result is not None
        assert result[1] == "Unknown User"

    @pytest.mark.smoke
    def test_missing_verified_defaults_false(self, fake_cache) -> None:
        from qapbot.QBdiscocmdshelper import get_any_player_owner

        fake_cache.user_accounts["111"] = {
            "display_name": "Owner",
            "players": [{"player_tag": "#P1", "player_name": "X"}],
        }
        result = get_any_player_owner("#P1", "222")
        assert result is not None
        assert result[2] is False
