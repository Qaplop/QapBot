from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.fixture
async def db(tmp_path):
    from qapbot.db_manager import WarHistoryDB

    db_path = tmp_path / "qapbot_cache_test.db"
    manager = WarHistoryDB()
    await manager.initialize(str(db_path))
    try:
        yield manager
    finally:
        await manager.close()


@pytest.fixture
def cache(db, monkeypatch: pytest.MonkeyPatch):
    from qapbot.cache_manager import CacheManager
    import qapbot.cache_manager as cache_manager_module

    manager = CacheManager()
    manager.db_manager = db
    # Tests exercise post-startup behavior; the users_loaded gate (data-loss guard
    # against write-through on an unloaded cache) is tested separately below.
    manager.users_loaded = True

    # Avoid any filesystem reads from data/temp during integration tests
    monkeypatch.setattr(manager, "load_all_temp_war_stats", lambda: None)

    # Patch global singleton so other modules can use it if needed
    monkeypatch.setattr(cache_manager_module, "CACHE", manager)
    return manager


class TestCacheUserWriteThrough:
    @pytest.mark.integration
    async def test_persist_user_roundtrip(self, cache, db):
        cache.user_accounts["123"] = {
            "display_name": "Tester",
            "notification_settings": {"war_reminders": False},
            "user_language": "en",
            "players": [
                {
                    "player_tag": "#P1",
                    "player_name": "Alice",
                    "verified": True,
                    "th_level": 15,
                    "current_clan_tag": None,
                    "is_primary": True,
                }
            ],
        }

        await cache.persist_user("123")
        loaded = await db.get_user("123")
        assert loaded is not None
        assert loaded["display_name"] == "Tester"
        assert loaded["user_language"] == "en"
        assert len(loaded["players"]) == 1
        assert loaded["players"][0]["player_tag"] == "#P1"
        assert loaded["players"][0]["verified"] is True


class TestCacheLoad:
    @pytest.mark.integration
    async def test_load_user_accounts_from_db(self, cache, db):
        await db.save_user(
            "999",
            {
                "display_name": "FromDB",
                "notification_settings": {"war_reminders": True},
                "players": [{"player_tag": "#X1", "player_name": "X"}],
                "user_language": "de",
            },
        )

        cache.user_accounts = {}
        await cache.load_user_accounts()
        assert cache.user_accounts["999"]["display_name"] == "FromDB"
        assert cache.user_accounts["999"]["user_language"] == "de"


class TestUsersLoadedDataLossGuard:
    """Write-through on an unloaded cache must be refused — a cache-blind save_user()
    replaces the whole players list, so a skeleton entry with players=[] hard-deletes
    every linked account (2026-08-08 prod incident: a button click during startup
    wiped a user's 5 account links)."""

    @pytest.mark.integration
    async def test_persist_user_refused_before_load(self, cache, db):
        cache.users_loaded = False
        await db.save_user(
            "555",
            {
                "display_name": "Victim",
                "notification_settings": {"war_reminders": True},
                "players": [{"player_tag": "#V1", "player_name": "Main"}],
                "user_language": "en",
            },
        )
        # Simulate the incident: pre-load skeleton entry in an otherwise empty cache
        cache.user_accounts["555"] = {"display_name": "Victim", "players": []}

        with pytest.raises(RuntimeError, match="before user accounts are loaded"):
            await cache.persist_user("555")

        # The DB rows must be untouched
        loaded = await db.get_user("555")
        assert loaded is not None
        assert len(loaded["players"]) == 1

    @pytest.mark.integration
    async def test_set_user_account_refused_before_load(self, cache):
        cache.users_loaded = False
        with pytest.raises(RuntimeError, match="before user accounts are loaded"):
            await cache.set_user_account("555", {"display_name": "X", "players": []})

    @pytest.mark.integration
    async def test_delete_user_account_refused_before_load(self, cache):
        cache.users_loaded = False
        with pytest.raises(RuntimeError, match="before user accounts are loaded"):
            await cache.delete_user_account("555")

    @pytest.mark.integration
    async def test_update_user_metadata_skips_before_load(self, cache, db):
        """The exact incident path: update_user_metadata (called by every registration
        button) must NOT fabricate a players=[] skeleton and persist it pre-load."""
        cache.users_loaded = False
        await db.save_user(
            "666",
            {
                "display_name": "Victim2",
                "notification_settings": {"war_reminders": True},
                "players": [{"player_tag": "#V2", "player_name": "Alt"}],
                "user_language": "en",
            },
        )
        cache.user_accounts = {}  # startup state: nothing loaded yet

        result = await cache.update_user_metadata("666")

        assert result is False
        assert "666" not in cache.user_accounts  # no skeleton fabricated
        loaded = await db.get_user("666")
        assert loaded is not None
        assert len(loaded["players"]) == 1  # DB rows untouched

    @pytest.mark.integration
    async def test_load_user_accounts_sets_users_loaded(self, cache):
        cache.users_loaded = False
        await cache.load_user_accounts()
        assert cache.users_loaded is True


class TestCachePreserveUnknownKeys:
    @pytest.mark.integration
    async def test_set_user_account_preserves_unknown_keys(self, cache):
        cache.user_accounts["777"] = {
            "display_name": "Existing",
            "players": [],
            "user_language": "en",
            "custom_future_field": {"a": 1},
        }

        await cache.set_user_account(
            "777",
            {
                "display_name": "Existing",
                "players": [],
                "user_language": "en",
            },
        )

        assert cache.user_accounts["777"]["custom_future_field"]["a"] == 1


class TestCacheSubscriptionsAndStatus:
    @pytest.mark.integration
    async def test_set_and_delete_subscriptions_write_through(self, cache, db):
        subs = [
            {"clan_tag": "#CLAN0001", "subscription_type": "attack", "year": 2026},
            {"clan_tag": None, "subscription_type": "playerlist"},
        ]

        await cache.set_subscriptions_for_channel("guild1", "channel1", subs)

        assert cache.subscriptions["guild1"]["channel1"] == subs
        from_db = await db.get_all_subscriptions_for_cache()
        assert "guild1" in from_db
        assert "channel1" in from_db["guild1"]

        await cache.delete_subscriptions_for_channel("guild1", "channel1")
        assert "guild1" not in cache.subscriptions
        from_db_after = await db.get_all_subscriptions_for_cache()
        assert from_db_after == {}

    @pytest.mark.integration
    async def test_update_all_clan_subscription_statuses_direct_and_family(self, cache):
        cache.clan_name_cache = {
            "#CLAN0001": {"name": "Clan 1", "has_active_subscriptions": False},
            "#CLAN0002": {"name": "Clan 2", "has_active_subscriptions": False},
        }
        cache.clan_families = {
            "#FAMILY01": {"name": "Fam", "clans": ["#CLAN0002"], "owned_by_guild": "guild1"}
        }
        cache.subscriptions = {
            "guild1": {
                "channel1": [
                    {"clan_tag": "#CLAN0001", "subscription_type": "attack"},
                    {"clan_tag": "#FAMILY01", "subscription_type": "attack"},
                ]
            }
        }

        await cache.update_all_clan_subscription_statuses()

        assert cache.clan_name_cache["#CLAN0001"]["has_active_subscriptions"] is True
        assert cache.clan_name_cache["#CLAN0002"]["has_active_subscriptions"] is True

        # Second call: nothing changed, no DB update needed
        await cache.update_all_clan_subscription_statuses()


class TestCacheAdditionalWriteThrough:
    @pytest.mark.integration
    async def test_leaderboard_message_channel_tag_normalized_and_deleted(self, cache, db):
        await cache.set_leaderboard_message(
            "msg_key_1",
            {
                "clan_tag": "channel_123",
                "channel_id": "987",
                "mode": "status",
                "message_ids": "111,222",
                "content_hash": "abc",
            },
        )

        from_db = await db.get_all_leaderboard_messages()
        assert from_db["msg_key_1"]["clan_tag"] is None

        await cache.delete_leaderboard_message("msg_key_1")
        assert "msg_key_1" not in cache.leaderboard_messages
        assert "msg_key_1" not in await db.get_all_leaderboard_messages()

    @pytest.mark.integration
    async def test_validate_clan_cache_consistency_adds_missing_clans(self, cache, db):
        cache.server_config = {
            "guild1": {
                "member_clans": ["#MISS0001"],
                "member_families": [],
            }
        }
        cache.clan_name_cache = {}

        await cache.validate_clan_cache_consistency()

        assert "#MISS0001" in cache.clan_name_cache
        assert cache.clan_name_cache["#MISS0001"]["name"] == "Unknown (auto-added)"

        clan_row = await db.get_clan("#MISS0001")
        assert clan_row is not None
        assert clan_row["name"] == "Unknown (auto-added)"

    @pytest.mark.integration
    async def test_persist_notifications_roundtrip(self, cache):
        assert cache.db_manager is not None
        await cache.db_manager.save_user(
            "123",
            {
                "display_name": "NotifyUser",
                "notification_settings": {"war_reminders": True},
                "players": [],
                "user_language": "en",
            },
        )

        cache.notification_state = {
            "WAR#1": {
                "notified_players": {
                    "#P1": {
                        "player_name": "Alice",
                        "discord_id": "123",
                        "notification_time": "2026-02-18T10:00:00",
                        "attacks_remaining": 1,
                    }
                },
                "channel_notifications": {
                    "guild1": {
                        "notification_time": "2026-02-18T10:00:00",
                        "clan_name": "Clan A",
                        "opponent_name": "Clan B",
                    }
                },
            }
        }

        # Missing entries should no-op
        await cache.persist_player_notification("UNKNOWN", "#P1")
        await cache.persist_channel_notification("UNKNOWN", "guild1")

        await cache.persist_player_notification("WAR#1", "#P1")
        await cache.persist_channel_notification("WAR#1", "guild1")

        cache.notification_state = {}
        await cache.load_notification_state()

        assert "WAR#1" in cache.notification_state
        assert cache.notification_state["WAR#1"]["notified_players"]["#P1"]["player_name"] == "Alice"
        assert cache.notification_state["WAR#1"]["channel_notifications"]["guild1"]["clan_name"] == "Clan A"
