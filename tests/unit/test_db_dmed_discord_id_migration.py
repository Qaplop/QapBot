"""discord_id -> dmed_discord_id migration across the three CWL snapshot tables (2026-08-22).

The old name invited the wrong reading: the column was copied out of user_players and then
trusted as current ownership, which greyed out linked players on the enrollment board and
mis-routed enrollment DMs (Pitfall 37). It only ever recorded who we DMed. Renamed rather than
dropped — it is the one place the value exists for a never-linked guest tag added via search,
and the audit trail is worth keeping.
"""
from __future__ import annotations

import pytest

from qapbot.db_manager import WarHistoryDB


CWL_SNAPSHOT_TABLES = ("cwl_signups", "cwl_shared_clan_players", "cwl_player_season_status")


async def _columns(db: WarHistoryDB, table: str) -> set:
    cursor = await db._conn.execute(f"PRAGMA table_info({table})")
    return {row[1] async for row in cursor}


class TestFreshDatabase:
    @pytest.mark.integration
    async def test_new_db_uses_the_new_name_on_all_three_tables(self, tmp_path):
        db = WarHistoryDB()
        await db.initialize(str(tmp_path / "fresh.db"))
        try:
            for table in CWL_SNAPSHOT_TABLES:
                cols = await _columns(db, table)
                assert "dmed_discord_id" in cols, f"{table} missing dmed_discord_id"
                assert "discord_id" not in cols, f"{table} still has the old discord_id"
        finally:
            await db.close()

    @pytest.mark.integration
    async def test_user_players_keeps_its_real_ownership_column(self, tmp_path):
        """The rename must NOT touch the tables where discord_id genuinely means ownership —
        renaming those would be actively wrong."""
        db = WarHistoryDB()
        await db.initialize(str(tmp_path / "fresh2.db"))
        try:
            for table in ("users", "user_players", "notification_state"):
                cols = await _columns(db, table)
                assert "discord_id" in cols, f"{table} lost its real discord_id column"
                assert "dmed_discord_id" not in cols, f"{table} was renamed by mistake"
        finally:
            await db.close()

    @pytest.mark.integration
    async def test_unused_signup_discord_index_is_gone(self, tmp_path):
        """idx_cwl_signups_discord was dropped, not renamed — verified 2026-08-22 that nothing
        ever filtered cwl_signups by that column, so it was pure write cost."""
        db = WarHistoryDB()
        await db.initialize(str(tmp_path / "fresh3.db"))
        try:
            cursor = await db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='cwl_signups'"
            )
            names = {row[0] async for row in cursor}
            assert "idx_cwl_signups_discord" not in names
        finally:
            await db.close()


class TestLegacyDatabase:
    @staticmethod
    async def _make_legacy(db_path) -> None:
        """Build a DB, then rewind the three tables to the pre-migration column name so the next
        initialize() sees exactly what a real already-shipped database looks like."""
        db = WarHistoryDB()
        await db.initialize(str(db_path))
        try:
            for table in CWL_SNAPSHOT_TABLES:
                await db._conn.execute(
                    f"ALTER TABLE {table} RENAME COLUMN dmed_discord_id TO discord_id"
                )
            await db._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cwl_signups_discord ON cwl_signups(discord_id)"
            )
            await db._conn.commit()
        finally:
            await db.close()

    @pytest.mark.integration
    async def test_migration_renames_and_preserves_data(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        await self._make_legacy(db_path)

        # Write a row under the OLD name, as a pre-migration bot would have.
        db = WarHistoryDB()
        await db.initialize(str(db_path))
        # initialize() already migrated, so rewind once more and insert legacy-style.
        try:
            await db._conn.execute("ALTER TABLE cwl_player_season_status RENAME COLUMN dmed_discord_id TO discord_id")
            await db._conn.execute(
                "INSERT INTO cwl_player_season_status (player_tag, cwl_season, player_name, discord_id, status) "
                "VALUES ('#LEGACY', '2026-09', 'Legacy', '4242', 'confirmed')"
            )
            await db._conn.commit()
        finally:
            await db.close()

        migrated = WarHistoryDB()
        await migrated.initialize(str(db_path))
        try:
            cols = await _columns(migrated, "cwl_player_season_status")
            assert "dmed_discord_id" in cols and "discord_id" not in cols
            row = migrated.get_cwl_player_season_status_sync("#LEGACY", "2026-09")
            assert row is not None
            assert row["dmed_discord_id"] == "4242", "data did not survive the rename"
            assert row["status"] == "confirmed"
        finally:
            await migrated.close()

    @pytest.mark.integration
    async def test_migration_is_idempotent_across_restarts(self, tmp_path):
        """A no-op on every startup after the first — the whole point of the PRAGMA guard."""
        db_path = tmp_path / "legacy2.db"
        await self._make_legacy(db_path)

        for _ in range(3):
            db = WarHistoryDB()
            await db.initialize(str(db_path))
            try:
                for table in CWL_SNAPSHOT_TABLES:
                    cols = await _columns(db, table)
                    assert "dmed_discord_id" in cols
                    assert "discord_id" not in cols
            finally:
                await db.close()

    @pytest.mark.integration
    async def test_legacy_index_is_dropped_by_the_migration(self, tmp_path):
        db_path = tmp_path / "legacy3.db"
        await self._make_legacy(db_path)

        db = WarHistoryDB()
        await db.initialize(str(db_path))
        try:
            cursor = await db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='cwl_signups'"
            )
            names = {row[0] async for row in cursor}
            assert "idx_cwl_signups_discord" not in names
        finally:
            await db.close()

    @pytest.mark.integration
    async def test_both_columns_present_is_left_alone_and_warned(self, tmp_path, caplog):
        """A half-applied migration must not be guessed at — the helper refuses rather than
        picking which column holds the real data."""
        db_path = tmp_path / "halfway.db"
        db = WarHistoryDB()
        await db.initialize(str(db_path))
        try:
            await db._conn.execute("ALTER TABLE cwl_signups ADD COLUMN discord_id TEXT")
            await db._conn.commit()
        finally:
            await db.close()

        reopened = WarHistoryDB()
        with caplog.at_level("WARNING"):
            await reopened.initialize(str(db_path))
        try:
            cols = await _columns(reopened, "cwl_signups")
            assert "discord_id" in cols and "dmed_discord_id" in cols
            assert any("BOTH" in r.getMessage() for r in caplog.records)
        finally:
            await reopened.close()
