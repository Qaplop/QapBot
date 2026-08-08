"""
Guards the orphan-purge registry (CLAN_TAG_REFERENCING_TABLES in db_manager.py).

is_clan_tag_referenced() / delete_clan_if_unreferenced() only protect a clans row
from hard deletion if the referencing table is listed in the registry. A new table
with a `REFERENCES clans` FK that is NOT registered would get its rows silently
cascade-deleted when the orphan purge fires — these tests turn that silent
data-loss bug into a test failure.
"""
from __future__ import annotations

import pytest

from qapbot.db_manager import CLAN_TAG_REFERENCING_TABLES, WarHistoryDB


@pytest.fixture
async def db(tmp_path):
    db_path = tmp_path / "qapbot_test.db"
    manager = WarHistoryDB()
    await manager.initialize(str(db_path))
    try:
        yield manager
    finally:
        await manager.close()


def _registry_pairs() -> set[tuple[str, str, str]]:
    return {(schema, table, column) for schema, table, column, _ in CLAN_TAG_REFERENCING_TABLES}


class TestClanTagReferenceRegistry:
    @pytest.mark.integration
    async def test_every_clans_fk_in_schema_has_a_registry_entry(self, db):
        """Every FK column referencing clans in the live DDL must be registered.

        Discovers FKs from the actual created schema via PRAGMA foreign_key_list,
        so a newly added `REFERENCES clans` table fails here until it's added to
        CLAN_TAG_REFERENCING_TABLES (and thereby protected from the orphan purge).
        """
        registry = _registry_pairs()

        cursor = await db.conn.execute(
            "SELECT name FROM main.sqlite_master WHERE type = 'table'"
        )
        tables = [row["name"] for row in await cursor.fetchall()]
        assert tables, "schema introspection returned no tables"

        missing: list[str] = []
        for table in tables:
            fk_cursor = await db.conn.execute(f"PRAGMA main.foreign_key_list({table})")
            for fk in await fk_cursor.fetchall():
                if fk["table"] != "clans":
                    continue
                if ("main", table, fk["from"]) not in registry:
                    missing.append(f"main.{table}.{fk['from']}")

        assert not missing, (
            f"Tables with a REFERENCES clans FK missing from CLAN_TAG_REFERENCING_TABLES: {missing}. "
            "Add them to the registry in db_manager.py or the orphan purge "
            "(delete_clan_if_unreferenced) will cascade-delete their rows."
        )

    @pytest.mark.integration
    async def test_registry_entries_point_at_real_tables_and_columns(self, db):
        """Every registry entry must name an existing table + column (catches typos/renames)."""
        for schema, table, column, _extra in CLAN_TAG_REFERENCING_TABLES:
            cursor = await db.conn.execute(f"PRAGMA {schema}.table_info({table})")
            columns = {row["name"] for row in await cursor.fetchall()}
            assert columns, f"{schema}.{table} does not exist"
            assert column in columns, f"{schema}.{table} has no column '{column}'"

    @pytest.mark.integration
    async def test_registry_extra_where_clauses_are_valid_sql(self, db):
        """Each entry's full query (incl. extra_where discriminator filters) must execute."""
        for schema, table, column, extra_where in CLAN_TAG_REFERENCING_TABLES:
            await db.conn.execute(
                f"SELECT 1 FROM {schema}.{table} WHERE {column} = ? {extra_where} LIMIT 1",
                ("#NOTATAG",),
            )

    @pytest.mark.integration
    async def test_purge_protects_clan_referenced_by_registry_table(self, db):
        """A clan referenced only via guild_welcome_clans (the FK the old inline
        enumeration missed) must survive delete_clan_if_unreferenced()."""
        await db.save_clan("#WELCOME1", "Welcome Clan")
        await db.save_guild_config("G1", {"welcome_clan_tags": ["#WELCOME1"]})

        deleted = await db.delete_clan_if_unreferenced("#WELCOME1")
        assert deleted is False

        cursor = await db.conn.execute(
            "SELECT 1 FROM clans WHERE clan_tag = ?", ("#WELCOME1",)
        )
        assert await cursor.fetchone() is not None, (
            "clan referenced by guild_welcome_clans was purged — registry regression"
        )

    @pytest.mark.integration
    async def test_purge_deletes_true_orphan(self, db):
        """An unreferenced clans row (e.g. a bogus placeholder) is still purged."""
        await db.save_clan("#ORPHAN1", "Unknown")

        deleted = await db.delete_clan_if_unreferenced("#ORPHAN1")
        assert deleted is True

        cursor = await db.conn.execute(
            "SELECT 1 FROM clans WHERE clan_tag = ?", ("#ORPHAN1",)
        )
        assert await cursor.fetchone() is None

    @pytest.mark.integration
    async def test_whois_player_leaderboard_row_does_not_protect(self, db):
        """The whois_player discriminator exclusion still works through the registry
        (Pitfall 17: an overloaded column's discriminator must be filtered)."""
        await db.save_clan("#PLAYERTAG", "Unknown")
        await db.save_leaderboard_message(
            message_key="2026-08-08T000000.000",
            clan_tag="#PLAYERTAG",
            channel_id="C1",
            mode="whois_player",
            message_ids="1",
            content_hash="h",
        )

        deleted = await db.delete_clan_if_unreferenced("#PLAYERTAG")
        assert deleted is True, (
            "whois_player leaderboard row wrongly protected a bogus clans row"
        )
