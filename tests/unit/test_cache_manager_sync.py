"""Additional cache_manager unit tests for non-async synchronous methods.

Targets: get_clan_name, _calculate_subscription_status, and save_war_object
serialization helpers. These are pure/sync methods that can be tested without
full DB initialization.
"""
# pyright: reportUnknownMemberType=false, reportUnknownParameterType=false
# pyright: reportMissingParameterType=false, reportPrivateUsage=false
from __future__ import annotations

import pytest

from qapbot.cache_manager import CacheManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cache() -> CacheManager:
    """Minimal CacheManager with no DB, suitable for sync method tests."""

    cm = CacheManager.__new__(CacheManager)
    cm.clan_name_cache = {}
    cm.subscriptions = {}
    cm.clan_families = {}
    cm.user_accounts = {}
    cm.leaderboard_messages = {}
    cm.server_config = {}
    cm.notification_state = {}
    return cm


# ---------------------------------------------------------------------------
# get_clan_name
# ---------------------------------------------------------------------------

class TestGetClanName:

    @pytest.mark.smoke
    def test_returns_name_from_dict_format(self, cache) -> None:
        cache.clan_name_cache["#ABC"] = {"name": "MyClan", "has_active_subscriptions": True}
        assert cache.get_clan_name("#ABC") == "MyClan"

    @pytest.mark.smoke
    def test_returns_string_format_backward_compat(self, cache) -> None:
        cache.clan_name_cache["#OLD"] = "OldFormatClan"
        assert cache.get_clan_name("#OLD") == "OldFormatClan"

    @pytest.mark.smoke
    def test_returns_default_when_missing(self, cache) -> None:
        assert cache.get_clan_name("#MISS") == "Unknown"

    @pytest.mark.smoke
    def test_returns_custom_default(self, cache) -> None:
        assert cache.get_clan_name("#MISS", default="N/A") == "N/A"

    @pytest.mark.smoke
    def test_returns_none_when_default_is_none(self, cache) -> None:
        assert cache.get_clan_name("#MISS", default=None) is None

    @pytest.mark.smoke
    def test_empty_cache_returns_default(self, cache) -> None:
        assert cache.get_clan_name("#ANY") == "Unknown"

    @pytest.mark.smoke
    def test_dict_without_name_key_returns_default(self, cache) -> None:
        cache.clan_name_cache["#NONAME"] = {"has_active_subscriptions": False}
        assert cache.get_clan_name("#NONAME") == "Unknown"


# ---------------------------------------------------------------------------
# _calculate_subscription_status
# ---------------------------------------------------------------------------

class TestCalculateSubscriptionStatus:

    @pytest.mark.smoke
    def test_direct_subscription_returns_true(self, cache) -> None:
        cache.subscriptions = {
            "guild1": {
                "channel1": [{"clan_tag": "#CLAN1", "subscription_type": "attack"}]
            }
        }
        assert cache._calculate_subscription_status("#CLAN1") is True

    @pytest.mark.smoke
    def test_no_subscriptions_returns_false(self, cache) -> None:
        cache.subscriptions = {}
        assert cache._calculate_subscription_status("#CLAN1") is False

    @pytest.mark.smoke
    def test_other_clan_subscribed_returns_false(self, cache) -> None:
        cache.subscriptions = {
            "guild1": {
                "channel1": [{"clan_tag": "#OTHER", "subscription_type": "attack"}]
            }
        }
        assert cache._calculate_subscription_status("#CLAN1") is False

    @pytest.mark.smoke
    def test_family_subscription_returns_true(self, cache) -> None:
        cache.clan_families = {
            "FAMILY1": {"name": "Fam", "clans": ["#CLAN1"], "owned_by_guild": "guild1"}
        }
        cache.subscriptions = {
            "guild1": {
                "channel1": [{"clan_tag": "FAMILY1", "subscription_type": "attack"}]
            }
        }
        assert cache._calculate_subscription_status("#CLAN1") is True

    @pytest.mark.smoke
    def test_family_without_subscription_returns_false(self, cache) -> None:
        cache.clan_families = {
            "FAMILY1": {"name": "Fam", "clans": ["#CLAN1"], "owned_by_guild": "guild1"}
        }
        cache.subscriptions = {}
        assert cache._calculate_subscription_status("#CLAN1") is False

    @pytest.mark.smoke
    def test_multiple_guilds_and_channels(self, cache) -> None:
        cache.subscriptions = {
            "guild1": {
                "ch1": [{"clan_tag": "#OTHER"}],
                "ch2": [{"clan_tag": "#CLAN1", "subscription_type": "defense"}],
            },
            "guild2": {
                "ch3": [{"clan_tag": "#OTHER2"}],
            },
        }
        assert cache._calculate_subscription_status("#CLAN1") is True
        assert cache._calculate_subscription_status("#OTHER2") is True
        assert cache._calculate_subscription_status("#MISSING") is False
