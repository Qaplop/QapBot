"""Guardrail for the class of bug behind the 2026-08-14 hot/history column-order-drift
incident (Cardinal Rule 1, .github/copilot-instructions.md; full writeup in
qapbot/docs/DATABASE_ARCHITECTURE.md's "Hot/History DB Split" section).

That incident was a pure column-REORDER (same column names, different physical order) —
already made harmless everywhere by 2026-08-16 (see test_hot_history_read_query_column_
alignment.py) since every `main`/`history` UNION now names its columns explicitly instead of
relying on `SELECT *` positional matching. Reordering alone can therefore never corrupt data
again, by construction, regardless of how the two schemas' physical column order evolves.

What explicit-column-naming does NOT protect against is a column existing in one schema and
not the other — that's not a reorder, it's a genuinely different column SET, and an explicit
column list just fails loudly (`no such column`) rather than silently misreading anything.
`check_hot_history_schema_parity_sync()` (qapbot/db_manager.py) is the tripwire for exactly
that case: it should be called once at bot startup (logged loudly if non-empty) and is
exercised here as a regression test, so a future migration that adds a column to `main.<table>`
without mirroring it onto `history.<table>` (or vice versa) fails CI immediately instead of
surfacing as a mysteriously-erroring or silently-empty stat months later.
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


@pytest.mark.integration
class TestCheckHotHistorySchemaParity:
    async def test_freshly_initialized_db_has_no_parity_mismatches(self, db):
        """A brand-new DB, built by this project's own current schema-creation code for both
        `main` and `history`, must agree on column SET for all 4 mirrored tables — this is the
        healthy baseline every real deployment should also be at."""
        assert db.check_hot_history_schema_parity_sync() == []

    async def test_detects_a_column_added_to_main_but_not_history(self, db):
        """Simulates the exact future mistake this guardrail exists to catch: a schema change
        that ALTERs `main.war_attacks` without mirroring it onto `history.war_attacks`."""
        await db.conn.execute("ALTER TABLE main.war_attacks ADD COLUMN some_new_column INTEGER DEFAULT 0")
        await db.conn.commit()
        assert db.check_hot_history_schema_parity_sync() == ["war_attacks"]

    async def test_detects_a_column_added_to_history_but_not_main(self, db):
        await db.conn.execute("ALTER TABLE history.war_summary ADD COLUMN some_new_column INTEGER DEFAULT 0")
        await db.conn.commit()
        assert db.check_hot_history_schema_parity_sync() == ["war_summary"]

    async def test_multiple_drifted_tables_all_reported(self, db):
        await db.conn.execute("ALTER TABLE main.war_attacks ADD COLUMN some_new_column INTEGER DEFAULT 0")
        await db.conn.execute("ALTER TABLE main.cwl_league_groups ADD COLUMN another_new_column TEXT")
        await db.conn.commit()
        assert set(db.check_hot_history_schema_parity_sync()) == {"war_attacks", "cwl_league_groups"}

    async def test_pure_column_reorder_is_not_flagged(self, db):
        """A reorder (same column NAMES, different physical order) is exactly what the
        2026-08-14 incident was, and is now harmless by design (explicit column lists
        everywhere) — this guardrail deliberately does not treat it as an error, since a false
        positive here would fail CI for a completely safe, expected divergence."""
        await db.conn.execute("DROP TABLE history.cwl_league_rounds")
        await db.conn.execute("""
            CREATE TABLE history.cwl_league_rounds (
                cwl_season      TEXT    NOT NULL,
                cwl_round       INTEGER NOT NULL,
                league_group_id TEXT    NOT NULL,
                war_tag         TEXT    NOT NULL PRIMARY KEY
            )
        """)
        await db.conn.commit()
        assert db.check_hot_history_schema_parity_sync() == []
