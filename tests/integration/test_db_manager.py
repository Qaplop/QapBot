from __future__ import annotations

import asyncio
import pytest


@pytest.fixture
async def db(tmp_path):
    from qapbot.db_manager import WarHistoryDB

    db_path = tmp_path / "qapbot_test.db"
    manager = WarHistoryDB()
    await manager.initialize(str(db_path))
    try:
        yield manager
    finally:
        await manager.close()


class TestDbInit:
    @pytest.mark.integration
    async def test_wal_mode_enabled(self, db):
        cursor = await db.conn.execute("PRAGMA journal_mode")
        row = await cursor.fetchone()
        assert row is not None
        assert str(row["journal_mode"]).lower() == "wal"

class TestUsers:
    @pytest.mark.integration
    async def test_save_and_delete_user_cascades_players(self, db):
        await db.save_user(
            "12345",
            {
                "display_name": "Test",
                "notification_settings": {"war_reminders": False},
                "players": [{"player_tag": "#P1", "player_name": "Alice"}],
                "user_language": "en",
            },
        )

        user = await db.get_user("12345")
        assert user is not None
        assert len(user.get("players", [])) == 1

        await db.delete_user("12345")
        assert await db.get_user("12345") is None

    @pytest.mark.integration
    async def test_save_user_dedupes_duplicate_player_tag(self, db):
        """2026-08-21 PROD incident: a race in coc_cache.py could hand save_user() a players
        list with the same player_tag twice (e.g. UNASSIGNED pool). Before this fix that raised
        sqlite3.IntegrityError on the UNIQUE(discord_id, player_tag) constraint and, because
        nothing ever removed the duplicate from the in-memory list, poisoned every subsequent
        save_user() call for that discord_id. Now it silently keeps the first occurrence."""
        await db.save_user(
            "UNASSIGNED",
            {
                "display_name": "UNASSIGNED",
                "players": [
                    {"player_tag": "#DUPTAG", "player_name": "First"},
                    {"player_tag": "#OTHER", "player_name": "Other"},
                    {"player_tag": "#DUPTAG", "player_name": "Second"},
                ],
            },
        )

        user = await db.get_user("UNASSIGNED")
        assert user is not None
        tags = sorted(p["player_tag"] for p in user["players"])
        assert tags == ["#DUPTAG", "#OTHER"]
        dup_entry = next(p for p in user["players"] if p["player_tag"] == "#DUPTAG")
        assert dup_entry["player_name"] == "First"  # first occurrence wins

        # A second, unrelated save must still succeed — proves the pool isn't poisoned.
        await db.save_user(
            "UNASSIGNED",
            {"display_name": "UNASSIGNED", "players": [{"player_tag": "#OTHER", "player_name": "Other"}]},
        )
        user_after = await db.get_user("UNASSIGNED")
        assert user_after is not None
        assert [p["player_tag"] for p in user_after["players"]] == ["#OTHER"]


class TestDbPhase2:
    @pytest.mark.integration
    async def test_save_subscriptions_normalizes_year_and_delete_guild(self, db):
        await db.save_subscriptions_for_channel(
            "guild1",
            "channel1",
            [
                {"clan_tag": "#CLAN0001", "subscription_type": "attack", "year": "current"},
                {"clan_tag": "#CLAN0001", "subscription_type": "defense", "year": 2026},
            ],
        )

        from_cache_format = await db.get_all_subscriptions_for_cache()
        entries = from_cache_format["guild1"]["channel1"]

        attack_entry = next(e for e in entries if e["subscription_type"] == "attack")
        defense_entry = next(e for e in entries if e["subscription_type"] == "defense")

        assert "year" not in attack_entry
        assert defense_entry["year"] == "2026"

        await db.delete_subscriptions_for_guild("guild1")
        assert await db.get_all_subscriptions_for_cache() == {}

    @pytest.mark.integration
    async def test_save_clan_family_roundtrip_and_delete(self, db):
        await db.save_clan_family("FAM1", "Family 1", "guild1", ["#CLAN0002"])

        families = await db.get_all_clan_families()
        assert "FAM1" in families
        assert families["FAM1"]["name"] == "Family 1"
        assert "#CLAN0002" in families["FAM1"]["clans"]

        await db.delete_clan_family("FAM1")
        families_after = await db.get_all_clan_families()
        assert "FAM1" not in families_after

    @pytest.mark.integration
    async def test_save_and_get_guild_config_roundtrip(self, db):
        await db.save_clan("#CLAN0003", "Clan 3")
        await db.save_clan_family("FAM2", "Family 2", "guild1", ["#CLAN0003"])
        config = {
            "language": "de",
            "newbie_role_id": "100",
            "member_role_id": "200",
            "role_system_enabled": True,
            "registration_channel_id": "300",
            "war_notification_channel_id": "400",
            "registration_message_enabled": True,
            "registration_message_id": "500",
            "registration_message_last_bump_iso": "2026-02-18T10:00:00",
            "channel_war_notifications_enabled": True,
            "war_notification_threshold_hours": 3.0,
            "member_families": ["FAM2"],
            "member_clans": ["#CLAN0003"],
        }

        await db.save_guild_config("guild1", config)

        loaded = await db.get_guild_config("guild1")
        assert loaded is not None
        assert loaded["language"] == "de"
        assert loaded["role_system_enabled"] is True
        assert loaded["member_families"] == ["FAM2"]
        assert loaded["member_clans"] == ["#CLAN0003"]

        all_configs = await db.get_all_guild_configs_dict()
        assert "guild1" in all_configs

    @pytest.mark.integration
    async def test_notification_state_roundtrip_and_delete(self, db):
        await db.save_user(
            "123",
            {
                "display_name": "NotifyUser",
                "notification_settings": {"war_reminders": True},
                "players": [],
                "user_language": "en",
            },
        )

        await db.save_player_notification(
            war_key="WAR#2",
            player_tag="#P1",
            player_name="Alice",
            discord_id="123",
            notification_time="2026-02-18T12:00:00",
            attacks_remaining=1,
        )
        await db.save_channel_notification(
            war_key="WAR#2",
            guild_id="guild1",
            notification_time="2026-02-18T12:00:00",
            clan_name="Clan A",
            opponent_name="Clan B",
        )

        state = await db.load_notification_state()
        assert "WAR#2" in state
        assert state["WAR#2"]["notified_players"]["#P1"]["discord_id"] == "123"
        assert state["WAR#2"]["channel_notifications"]["guild1"]["opponent_name"] == "Clan B"

        await db.delete_notification_state_for_war("WAR#2")
        state_after = await db.load_notification_state()
        assert "WAR#2" not in state_after


class TestConcurrentWrites:
    """Regression test for 'cannot start a transaction within a transaction'.

    Simulates the PHASE-1 parallel clan fetch scenario where two coroutines call
    save_user concurrently.  Before the asyncio.Lock fix, one coroutine's
    _save_clan_unlocked would leave an implicit SQLite transaction open; the other
    coroutine would then hit ``OperationalError: cannot start a transaction within
    a transaction`` when issuing the explicit BEGIN.
    """

    @pytest.mark.integration
    async def test_concurrent_save_user_does_not_raise(self, db):
        """Two concurrent save_user calls must both complete without DB errors."""

        async def save(user_id: str, clan_tag: str) -> None:
            await db.save_user(
                user_id,
                {
                    "display_name": f"User {user_id}",
                    "notification_settings": {"war_reminders": True},
                    "players": [
                        {
                            "player_tag": f"#P{user_id}",
                            "player_name": f"Player {user_id}",
                            "current_clan_tag": clan_tag,
                        }
                    ],
                    "user_language": "en",
                },
            )

        # Pre-create the clans so _save_clan_unlocked is a no-op (clan already exists).
        # The real race also fires when the clan is *not* pre-cached, so test both paths.
        await db.save_clan("#2CLAN_A01", "Clan A")
        await db.save_clan("#2CLAN_B01", "Clan B")

        # Path 1: clans already exist — _ensure_clan_exists takes the fast path (SELECT only)
        await asyncio.gather(
            save("U1", "#2CLAN_A01"),
            save("U2", "#2CLAN_B01"),
        )

        u1 = await db.get_user("U1")
        u2 = await db.get_user("U2")
        assert u1 is not None and u2 is not None

    @pytest.mark.integration
    async def test_concurrent_save_user_with_new_clan_does_not_raise(self, db):
        """Concurrent save_user where the clan does NOT yet exist triggers _save_clan_unlocked.

        This is the exact scenario from the 2026-02-20 production error:
        _save_clan_unlocked issues an INSERT + COMMIT (auto-transaction).  Without
        the lock, a concurrent save_user's explicit BEGIN would crash.
        """

        async def save_with_new_clan(user_id: str, clan_tag: str) -> None:
            # Clan does NOT exist yet; _ensure_clan_exists will call _save_clan_unlocked
            await db.save_user(
                user_id,
                {
                    "display_name": f"User {user_id}",
                    "notification_settings": {"war_reminders": False},
                    "players": [
                        {
                            "player_tag": f"#PP{user_id}",
                            "player_name": f"Player {user_id}",
                            "current_clan_tag": clan_tag,
                        }
                    ],
                    "user_language": "en",
                },
            )

        # Each user has a different brand-new clan tag — both require an INSERT into clans
        await asyncio.gather(
            save_with_new_clan("V1", "#NEWCLAN01"),
            save_with_new_clan("V2", "#NEWCLAN02"),
            save_with_new_clan("V3", "#NEWCLAN03"),
        )

        for uid in ("V1", "V2", "V3"):
            assert await db.get_user(uid) is not None


class TestCwlGroupExpansion:
    """get_active_cwl_group_member_tags drives the CWL-GROUP-EXPAND fetch list.

    It must (a) return every member of a group containing a tracked clan,
    (b) exclude groups whose cwl_ended flag is set, and (c) only match the
    requested season — the contract relied on by the main update loop.
    """

    async def _make_group(self, db, season, group_id, tags):
        await db.upsert_cwl_league_data(
            league_group_id=group_id,
            cwl_season=season,
            clan_tags=tags,
            rounds=[],
        )

    @pytest.mark.integration
    async def test_returns_all_group_members_for_active_group(self, db):
        tags = ["#A1", "#A2", "#A3", "#A4"]
        await self._make_group(db, "2026-06", "grpA", tags)

        result = await db.get_active_cwl_group_member_tags("2026-06", ["#A1"])
        assert set(result) == set(tags)

    @pytest.mark.integration
    async def test_excludes_ended_groups(self, db):
        tags = ["#B1", "#B2", "#B3"]
        await self._make_group(db, "2026-06", "grpB", tags)
        # Mark the group ended via the same setter the live code uses.
        clan_stats = [
            {"clan_tag": t, "group_rank": i + 1, "total_stars": 0, "total_destruction": 0.0}
            for i, t in enumerate(tags)
        ]
        await db.update_cwl_group_stats_batch("2026-06", "grpB", clan_stats, True)

        result = await db.get_active_cwl_group_member_tags("2026-06", ["#B1"])
        assert result == []

    @pytest.mark.integration
    async def test_only_matches_requested_season(self, db):
        await self._make_group(db, "2026-05", "grpOld", ["#C1", "#C2"])
        await self._make_group(db, "2026-06", "grpNew", ["#C1", "#C3"])

        # Querying the new season must not pull in the old season's members.
        result = await db.get_active_cwl_group_member_tags("2026-06", ["#C1"])
        assert set(result) == {"#C1", "#C3"}
