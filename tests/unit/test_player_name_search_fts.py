"""Tests for Step 11 (CWL_PROD_PERFORMANCE_FIX_PLAN.md P2) — the SQLite/FTS5-backed player-name
search: schema creation, the incremental writers that keep player_name_search/player_name_fts in
sync with player_name_index, the one-time backfill, and the two new SQL-backed readers
(search_player_names_sync, search_player_tags_by_prefix_sync).
"""
from __future__ import annotations

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


class TestSchemaAndBackfill:
    @pytest.mark.integration
    async def test_tables_created_empty(self, db):
        cur = await db.conn.execute("SELECT COUNT(*) FROM player_name_search")
        assert (await cur.fetchone())[0] == 0
        cur = await db.conn.execute("SELECT COUNT(*) FROM player_name_fts")
        assert (await cur.fetchone())[0] == 0

    @pytest.mark.integration
    async def test_backfill_populates_from_existing_player_name_index(self, db):
        """Simulates data that existed in player_name_index from before this migration —
        inserted directly (bypassing the writers, which already keep the tables in sync), then
        re-running the backfill picks it up."""
        await db.conn.execute(
            "INSERT INTO player_name_index (player_tag, player_name, last_seen) VALUES (?, ?, ?)",
            ("#PRE1", "PreExisting", "2026-01-01T00:00"),
        )
        await db.conn.commit()

        await db._backfill_player_name_search_if_needed()

        cur = await db.conn.execute("SELECT name FROM player_name_search WHERE player_tag = '#PRE1'")
        row = await cur.fetchone()
        assert row["name"] == "PreExisting"
        assert db.search_player_names_sync("PreExisting") == [
            {"player_tag": "#PRE1", "player_name": "PreExisting"}
        ]

    @pytest.mark.integration
    async def test_backfill_is_idempotent(self, db):
        await db.conn.execute(
            "INSERT INTO player_name_index (player_tag, player_name, last_seen) VALUES (?, ?, ?)",
            ("#A1", "Alice", "2026-01-01T00:00"),
        )
        await db.conn.commit()

        await db._backfill_player_name_search_if_needed()
        await db._backfill_player_name_search_if_needed()  # must not error or duplicate rows

        cur = await db.conn.execute("SELECT COUNT(*) FROM player_name_search")
        assert (await cur.fetchone())[0] == 1
        cur = await db.conn.execute("SELECT COUNT(*) FROM player_name_fts")
        assert (await cur.fetchone())[0] == 1

    @pytest.mark.integration
    async def test_backfill_noop_when_source_empty(self, db):
        # player_name_index starts empty on a fresh DB — must not error.
        await db._backfill_player_name_search_if_needed()
        cur = await db.conn.execute("SELECT COUNT(*) FROM player_name_search")
        assert (await cur.fetchone())[0] == 0


class TestWritersKeepSearchTablesInSync:
    @pytest.mark.integration
    async def test_upsert_player_name_index_in_conn_syncs_search_tables(self, db):
        """Simulates a war-save write path — calls the low-level sync writer directly against a
        real sync connection, then verifies both new search tables reflect it."""
        with db._sync_conn() as conn:
            db._upsert_player_name_index_in_conn(conn, [
                {"PlayerID": "#W1", "Player": "WarPlayer", "Date": "2026-08-01T10:00"},
            ])
            conn.commit()

        assert db.search_player_names_sync("WarPlayer") == [{"player_tag": "#W1", "player_name": "WarPlayer"}]
        assert db.search_player_tags_by_prefix_sync("#W1") == [{"player_tag": "#W1", "player_name": "WarPlayer"}]

    @pytest.mark.integration
    async def test_upsert_respects_newest_wins_against_player_name_index(self, db):
        """A batch with an OLDER date than what's already recorded must not overwrite the search
        tables with the stale name (2026-08-17 correctness point — the writer re-reads the
        ACTUAL resulting name from player_name_index after its own WHERE-guarded upsert, rather
        than trusting the batch's own value directly, exactly so this can't happen)."""
        with db._sync_conn() as conn:
            db._upsert_player_name_index_in_conn(conn, [
                {"PlayerID": "#W2", "Player": "NewerName", "Date": "2026-08-10T10:00"},
            ])
            conn.commit()
        with db._sync_conn() as conn:
            db._upsert_player_name_index_in_conn(conn, [
                {"PlayerID": "#W2", "Player": "OlderName", "Date": "2026-08-01T10:00"},
            ])
            conn.commit()

        assert db.search_player_names_sync("NewerName") == [{"player_tag": "#W2", "player_name": "NewerName"}]
        assert db.search_player_names_sync("OlderName") == []

    @pytest.mark.integration
    async def test_update_player_name_index_sync_syncs_search_tables(self, db):
        db.update_player_name_index_sync([("#API1", "ApiPlayer", "2026-08-17T12:00")])
        assert db.search_player_names_sync("ApiPlayer") == [{"player_tag": "#API1", "player_name": "ApiPlayer"}]
        assert db.search_player_tags_by_prefix_sync("#API1") == [{"player_tag": "#API1", "player_name": "ApiPlayer"}]

    @pytest.mark.integration
    async def test_update_syncs_search_tables_for_a_large_batch(self, db):
        """Chunked IN() re-read (Step 4 pattern reused here) must handle a batch well past the
        ~900-value SQLite host-parameter chunk size without erroring."""
        updates = [(f"#BULK{i:04d}", f"BulkPlayer{i:04d}", "2026-08-17T12:00") for i in range(1500)]
        db.update_player_name_index_sync(updates)
        assert db.search_player_tags_by_prefix_sync("#BULK0000") == [
            {"player_tag": "#BULK0000", "player_name": "BulkPlayer0000"}
        ]
        assert db.search_player_tags_by_prefix_sync("#BULK1499") == [
            {"player_tag": "#BULK1499", "player_name": "BulkPlayer1499"}
        ]


class TestSearchPlayerNamesSync:
    @pytest.mark.integration
    async def test_substring_match_case_insensitive(self, db):
        db.update_player_name_index_sync([("#A1", "AliceWonder", "2026-08-17T00:00")])
        assert db.search_player_names_sync("ALICE") == [{"player_tag": "#A1", "player_name": "AliceWonder"}]
        assert db.search_player_names_sync("wonder") == [{"player_tag": "#A1", "player_name": "AliceWonder"}]

    @pytest.mark.integration
    async def test_below_three_chars_returns_empty(self, db):
        """Trigram tokenization needs >=3 characters to form even one trigram — verified
        empirically (2026-08-17) that a shorter needle structurally cannot match anything."""
        db.update_player_name_index_sync([("#A1", "Alice", "2026-08-17T00:00")])
        assert db.search_player_names_sync("Al") == []
        assert db.search_player_names_sync("A") == []
        assert db.search_player_names_sync("") == []

    @pytest.mark.integration
    async def test_special_characters_are_literal_not_query_syntax(self, db):
        """A raw hyphen/quote/asterisk in the needle must be treated as literal text, not FTS5
        query syntax — verified empirically (2026-08-17) that an unquoted MATCH either raises
        ("no such column: smith" for a hyphen) or silently changes meaning for these."""
        db.update_player_name_index_sync([
            ("#A1", "bob-smith", "2026-08-17T00:00"),
            ("#A2", 'Weird"Quote', "2026-08-17T00:00"),
            ("#A3", "Sam*Star", "2026-08-17T00:00"),
        ])
        assert db.search_player_names_sync("bob-smith") == [{"player_tag": "#A1", "player_name": "bob-smith"}]
        assert db.search_player_names_sync("smith") == [{"player_tag": "#A1", "player_name": "bob-smith"}]
        assert db.search_player_names_sync('Weird"Quote') == [{"player_tag": "#A2", "player_name": 'Weird"Quote'}]
        assert db.search_player_names_sync("*Star") == [{"player_tag": "#A3", "player_name": "Sam*Star"}]

    @pytest.mark.integration
    async def test_capped_at_25(self, db):
        updates = [(f"#P{i:03d}", f"Player{i:03d}", "2026-08-17T00:00") for i in range(30)]
        db.update_player_name_index_sync(updates)
        assert len(db.search_player_names_sync("Player", limit=50)) == 25
        assert len(db.search_player_names_sync("Player", limit=5)) == 5

    @pytest.mark.integration
    async def test_sorted_alphabetically(self, db):
        db.update_player_name_index_sync([
            ("#Z1", "ZebraName", "2026-08-17T00:00"),
            ("#A1", "AppleName", "2026-08-17T00:00"),
            ("#M1", "MangoName", "2026-08-17T00:00"),
        ])
        names = [r["player_name"] for r in db.search_player_names_sync("Name")]
        assert names == sorted(names, key=str.lower)

    @pytest.mark.integration
    async def test_no_match_returns_empty(self, db):
        db.update_player_name_index_sync([("#A1", "Alice", "2026-08-17T00:00")])
        assert db.search_player_names_sync("Zzzzz") == []


class TestSearchPlayerTagsByPrefixSync:
    @pytest.mark.integration
    async def test_prefix_match(self, db):
        db.update_player_name_index_sync([
            ("#ABC123", "Alice", "2026-08-17T00:00"),
            ("#ABD999", "Bob", "2026-08-17T00:00"),
            ("#XYZ111", "Carol", "2026-08-17T00:00"),
        ])
        tags = {r["player_tag"] for r in db.search_player_tags_by_prefix_sync("#AB")}
        assert tags == {"#ABC123", "#ABD999"}

    @pytest.mark.integration
    async def test_capped_at_limit(self, db):
        updates = [(f"#QAP{i:03d}", f"Player{i}", "2026-08-17T00:00") for i in range(20)]
        db.update_player_name_index_sync(updates)
        assert len(db.search_player_tags_by_prefix_sync("#QAP", limit=12)) == 12

    @pytest.mark.integration
    async def test_no_match_returns_empty(self, db):
        assert db.search_player_tags_by_prefix_sync("#NEVERSEEN") == []

    @pytest.mark.integration
    async def test_empty_prefix_returns_empty(self, db):
        assert db.search_player_tags_by_prefix_sync("") == []


class TestCacheManagerRolloutFlag:
    """CACHE.search_player_names() delegates to db_manager.search_player_names_sync() only when
    CONFIG.cwl_use_fts_player_search is True (2026-08-17, Step 11 rollout flag — defaults False,
    the in-memory scan stays the active path until DEV+PROD burn-in confirms parity)."""

    @pytest.mark.integration
    async def test_delegates_to_sqlite_when_flag_true(self, db, monkeypatch):
        import dataclasses
        from qapbot.cache_manager import CacheManager
        from qapbot.config import CONFIG

        db.update_player_name_index_sync([("#A1", "Alice", "2026-08-17T00:00")])

        cache = CacheManager.__new__(CacheManager)
        cache.db_manager = db
        cache.player_name_index = {}  # deliberately empty — proves the SQLite path was used

        monkeypatch.setattr("qapbot.config.CONFIG", dataclasses.replace(CONFIG, cwl_use_fts_player_search=True))

        assert cache.search_player_names("Alice") == [{"player_tag": "#A1", "player_name": "Alice"}]

    def test_uses_in_memory_path_when_flag_false(self, monkeypatch):
        import dataclasses
        from qapbot.cache_manager import CacheManager
        from qapbot.config import CONFIG

        cache = CacheManager.__new__(CacheManager)
        cache.db_manager = None  # would AttributeError if the SQLite path were mistakenly used
        cache.player_name_index = {"#A1": ("Alice", "alice")}

        monkeypatch.setattr("qapbot.config.CONFIG", dataclasses.replace(CONFIG, cwl_use_fts_player_search=False))

        assert cache.search_player_names("Alice") == [{"player_tag": "#A1", "player_name": "Alice"}]
