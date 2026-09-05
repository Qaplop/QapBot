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
# get_war_file_stats (tracker item #0090)
# ---------------------------------------------------------------------------

class TestGetWarFileStats:
    """tracker item #0090, live bug report: /status crashed with `RuntimeError: dictionary
    changed size during iteration` inside get_war_file_stats(). Root cause: Phase 3's per-clan
    war processing runs process_clan_war_data()/manage_war_files() via asyncio.to_thread() -- a
    REAL worker thread that can insert/pop temp_war_metadata entries while this method runs on
    the event-loop thread. Two coroutines on the same event loop can't interleave without an
    await, but two genuine OS threads can race at the bytecode level. Fixed by snapshotting with
    list(...values()) before iterating (the same idiom QapBot.py's cycle code already uses for
    clan_name_cache) -- these tests reproduce the underlying "mutate while iterating" hazard
    deterministically (no real threads needed: mutating a dict's size while its live .values()
    view is being iterated raises regardless of what triggers the mutation), by having one
    entry's own .get() call insert a new key -- standing in for a genuinely concurrent insert."""

    @pytest.mark.smoke
    def test_counts_by_state_and_cwl(self, cache) -> None:
        cache.temp_war_metadata = {
            "#A": {"state": "preparation", "is_cwl": False},
            "#B": {"state": "in_war", "is_cwl": True},
            "#C": {"state": "war_ended", "is_cwl": False},
        }
        cache.temp_total_file_count = 0
        stats = cache.get_war_file_stats()
        assert stats["prep"] == 1
        assert stats["in_war"] == 1
        assert stats["war_ended"] == 1
        assert stats["cwl_known"] == 1

    def test_tolerates_dict_mutated_during_iteration(self, cache) -> None:
        temp_war_metadata: dict = {}

        class _MutatingEntry(dict):
            """Standing in for a genuinely concurrent asyncio.to_thread() worker inserting a
            new clan's metadata mid-loop."""
            def get(self, *args, **kwargs):
                temp_war_metadata.setdefault("#NEW", {"state": "in_war"})
                return super().get(*args, **kwargs)

        temp_war_metadata["#EXISTING"] = _MutatingEntry({"state": "preparation"})
        cache.temp_war_metadata = temp_war_metadata
        cache.temp_total_file_count = 0

        stats = cache.get_war_file_stats()  # must not raise RuntimeError

        assert stats["prep"] == 1  # only the pre-snapshot entry is counted


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
