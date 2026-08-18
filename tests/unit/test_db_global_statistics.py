"""Tests for db_manager.get_global_db_statistics_sync (parallelized 5-query global stats scan)
and preload_global_db_statistics_from_snapshot (bot_metadata-persisted snapshot restore across
restarts). Added 2026-08-18 alongside the fix for the PROD startup DB-connection-pool
contention diagnosed live: the old sequential single-connection scan took ~20s and shared the
same 8-connection sync pool as the periodic clan-fetch cycle's Phase-1, stalling it right at
startup.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from qapbot.db_manager import WarHistoryDB


@pytest.fixture
async def db(tmp_path):
    db_path = tmp_path / "qapbot_test.db"
    manager = WarHistoryDB()
    await manager.initialize(str(db_path))
    try:
        yield manager
    finally:
        await manager.close()


async def _seed_clan(db, clan_tag: str) -> None:
    await db.conn.execute(
        "INSERT INTO clans (clan_tag, name) VALUES (?, ?)", (clan_tag, clan_tag)
    )
    await db.conn.commit()


async def _seed_war_summary(db, schema: str, war_id: str, clan_tag: str) -> None:
    await db.conn.execute(
        f"INSERT INTO {schema}.war_summary (war_id, clan_tag, opponent_tag, date) "
        "VALUES (?, ?, '#OPP', '2026-08-01T00:00')",
        (war_id, clan_tag),
    )
    await db.conn.commit()


async def _seed_war_attack(db, schema: str, war_id: str, player_tag: str, attack_order: int) -> None:
    await db.conn.execute(
        f"INSERT INTO {schema}.war_attacks "
        "(war_id, clan_tag, date, player_name, player_tag, th_level, attack_order, stars) "
        "VALUES (?, '#CLAN', '2026-08-01T00:00', ?, ?, 15, ?, 2)",
        (war_id, player_tag, player_tag, attack_order),
    )
    await db.conn.commit()


async def _seed_user_player(db, discord_id: str, player_tag: str) -> None:
    await db.conn.execute(
        "INSERT OR IGNORE INTO users (discord_id, display_name) VALUES (?, ?)",
        (discord_id, discord_id),
    )
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name) VALUES (?, ?, ?)",
        (discord_id, player_tag, player_tag),
    )
    await db.conn.commit()


async def _seed_tracked_player(db, player_tag: str) -> None:
    await db.conn.execute(
        "INSERT INTO player_name_index (player_tag, player_name, last_seen) VALUES (?, ?, ?)",
        (player_tag, player_tag, "2026-08-01T00:00"),
    )
    await db.conn.commit()


class TestGetGlobalDbStatisticsSync:
    @pytest.mark.integration
    async def test_computes_all_five_counts_correctly(self, db):
        await _seed_clan(db, "#A")
        await _seed_clan(db, "#B")

        # 2 distinct war_ids in main (W1 appears twice — two tracked clans in the same war).
        await _seed_war_summary(db, "main", "W1", "#A")
        await _seed_war_summary(db, "main", "W1", "#B")
        await _seed_war_summary(db, "main", "W2", "#A")
        # 1 more distinct war_id in history.
        await _seed_war_summary(db, "history", "W3", "#A")

        # main: 2 real attacks + 1 zero-attack sentinel (excluded). history: 1 real attack.
        await _seed_war_attack(db, "main", "W1", "#P1", attack_order=1)
        await _seed_war_attack(db, "main", "W1", "#P2", attack_order=2)
        await _seed_war_attack(db, "main", "W1", "#P3", attack_order=0)
        await _seed_war_attack(db, "history", "W3", "#P4", attack_order=1)

        await _seed_user_player(db, "111", "#P1")
        await _seed_user_player(db, "111", "#P2")
        await _seed_user_player(db, "222", "#P3")

        await _seed_tracked_player(db, "#P1")
        await _seed_tracked_player(db, "#P2")
        await _seed_tracked_player(db, "#P3")
        await _seed_tracked_player(db, "#P4")

        stats = db.get_global_db_statistics_sync()

        assert stats == {
            'clans_count': 2,
            'wars_count': 3,  # W1, W2, W3
            'attacks_count': 3,  # excludes the attack_order=0 sentinel
            'players_count': 3,
            'players_tracked_count': 4,
        }

    @pytest.mark.integration
    async def test_serves_cached_value_without_recomputing(self, db):
        await _seed_clan(db, "#A")
        first = db.get_global_db_statistics_sync()
        assert first['clans_count'] == 1

        await _seed_clan(db, "#B")  # data changed underneath the cache
        second = db.get_global_db_statistics_sync()  # force_refresh=False (default)

        assert second['clans_count'] == 1  # still the stale cached value

    @pytest.mark.integration
    async def test_force_refresh_recomputes(self, db):
        await _seed_clan(db, "#A")
        first = db.get_global_db_statistics_sync()
        assert first['clans_count'] == 1

        await _seed_clan(db, "#B")
        second = db.get_global_db_statistics_sync(force_refresh=True)

        assert second['clans_count'] == 2

    @pytest.mark.integration
    async def test_persists_exact_snapshot_to_bot_metadata(self, db):
        await _seed_clan(db, "#A")
        stats = db.get_global_db_statistics_sync()

        raw = await db.get_bot_metadata(db._GLOBAL_STATS_METADATA_KEY)
        assert raw is not None
        payload = json.loads(raw)
        assert payload['clans_count'] == stats['clans_count']
        assert payload['wars_count'] == stats['wars_count']
        assert payload['attacks_count'] == stats['attacks_count']
        assert payload['players_count'] == stats['players_count']
        assert payload['players_tracked_count'] == stats['players_tracked_count']
        # Must be a real, recent UTC timestamp, not a placeholder.
        computed_at = datetime.fromisoformat(payload['computed_at_utc'])
        assert (datetime.now(timezone.utc) - computed_at).total_seconds() < 30

    def test_db_not_initialized_raises(self):
        dm = WarHistoryDB.__new__(WarHistoryDB)
        dm.db_path = None
        with pytest.raises(RuntimeError, match="Database not initialized"):
            dm.get_global_db_statistics_sync()


class TestPreloadGlobalDbStatisticsFromSnapshot:
    @pytest.mark.integration
    async def test_returns_false_when_no_snapshot_exists(self, db):
        assert await db.preload_global_db_statistics_from_snapshot() is False
        assert db._global_stats_cache is None

    @pytest.mark.integration
    async def test_restores_exact_values_from_a_prior_snapshot(self, db):
        await _seed_clan(db, "#A")
        await _seed_clan(db, "#B")
        original = db.get_global_db_statistics_sync()  # persists the snapshot as a side effect

        # Simulate a fresh restart: wipe the in-memory cache.
        db._global_stats_cache = None
        db._global_stats_cache_ts = 0.0

        restored = await db.preload_global_db_statistics_from_snapshot()

        assert restored is True
        assert db._global_stats_cache == original

    @pytest.mark.integration
    async def test_restored_snapshot_is_served_by_get_global_db_statistics_sync(self, db):
        """The whole point: after preload, a plain force_refresh=False call must serve the
        restored values instantly rather than re-scanning."""
        await _seed_clan(db, "#A")
        db.get_global_db_statistics_sync()
        db._global_stats_cache = None
        db._global_stats_cache_ts = 0.0
        await db.preload_global_db_statistics_from_snapshot()

        await _seed_clan(db, "#B")  # would change the answer if a rescan happened
        served = db.get_global_db_statistics_sync()

        assert served['clans_count'] == 1

    @pytest.mark.integration
    async def test_preserves_real_elapsed_age_across_the_gap(self, db):
        """A snapshot computed 10h ago must be restored as 10h stale (not fresh-as-now), so the
        existing 25h TTL check still expires it at the right wall-clock time after a restart."""
        await _seed_clan(db, "#A")
        db.get_global_db_statistics_sync()

        # Rewrite the persisted snapshot as if it were computed 10h ago.
        raw = await db.get_bot_metadata(db._GLOBAL_STATS_METADATA_KEY)
        payload = json.loads(raw)
        payload['computed_at_utc'] = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
        await db.set_bot_metadata(db._GLOBAL_STATS_METADATA_KEY, json.dumps(payload))

        db._global_stats_cache = None
        db._global_stats_cache_ts = 0.0
        await db.preload_global_db_statistics_from_snapshot()

        import time
        age = time.monotonic() - db._global_stats_cache_ts
        assert 9.9 * 3600 < age < 10.1 * 3600

    @pytest.mark.integration
    async def test_unparseable_snapshot_returns_false(self, db):
        await db.set_bot_metadata(db._GLOBAL_STATS_METADATA_KEY, "not valid json")
        assert await db.preload_global_db_statistics_from_snapshot() is False

    @pytest.mark.integration
    async def test_snapshot_missing_a_field_returns_false(self, db):
        await db.set_bot_metadata(
            db._GLOBAL_STATS_METADATA_KEY,
            json.dumps({"clans_count": 1, "computed_at_utc": datetime.now(timezone.utc).isoformat()}),
        )
        assert await db.preload_global_db_statistics_from_snapshot() is False
