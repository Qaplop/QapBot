"""Regression test for the READ-side counterpart of the 2026-08-14 hot/history
column-order-drift incident (Cardinal Rule 1, .github/copilot-instructions.md; full writeup in
qapbot/docs/DATABASE_ARCHITECTURE.md's "Hot/History DB Split" section).

The 2026-08-14 fix (`_explicit_column_list()`, `WarHistoryDB._migrate_date_window_batched`)
only covered the *write* path (the monthly hot->history migration). On 2026-08-16, while
investigating why the "Manage Enrollment" hover pop-up showed a player's Skill Score but not
their Attacks/Missed-Attacks/Attack-Defense-Ratio, the same class of bug was found on the
*read* side: every sync method that reads BOTH `main.<table>` and `history.<table>` into one
result set via `WITH x AS (SELECT * FROM main.t UNION ALL SELECT * FROM history.t)` was
matching columns by POSITION, not name — so any row actually read from `history` (i.e. any war
old enough to have been archived) came back with scrambled `max_attacks`/`missed_attacks`/
`defensive_stars` values whenever `history.<table>`'s physical column order differs from
`main.<table>`'s, which real production data confirmed it does (verified against DEV's real
`data/qapbot_history.db`).

The fix (`WarHistoryDB._explicit_column_list_sync`) mirrors the write-side one: every affected
query now names its columns explicitly on both sides of the `UNION ALL`, immune to physical
order regardless of which schema a row is actually read from. This test proves that fix
survives a genuine column-order divergence between `main.war_attacks` and `history.war_attacks`
— reproducing the REAL drift pattern found on DEV (not a synthetic single-column swap) — for
the two query functions most directly implicated: `get_player_attack_history_sync` (the
`/leaderboard scope=all` + hover pop-up data source) and `get_clan_attack_history_sync` (the
`/leaderboard scope=own` data source). A `SELECT *`-based implementation would fail this test.
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


async def _rebuild_history_war_attacks_with_real_drift_order(db: WarHistoryDB) -> None:
    """Recreates `history.war_attacks` with the EXACT column order found on DEV's real
    `data/qapbot_history.db` (2026-08-16) — `map_position` moved next to `th_level`, and
    `max_attacks`/`missed_attacks`/`defensive_stars`/`created_at` moved to the very end, after
    `defender_th`/`defender_map_position`/`duration`/`is_fresh`/`times_defended`/
    `best_def_destruction` — versus `main.war_attacks`'s own order (see the CREATE TABLE at
    qapbot/db_manager.py's `initialize()`, main schema section), where `attack_order`/`stars`/
    `destruction`/`defender_tag`/`max_attacks`/`missed_attacks`/`defensive_stars`/`created_at`
    come first and `map_position`/`defender_th`/`defender_map_position`/`duration`/`is_fresh`/
    `times_defended`/`best_def_destruction` are appended at the end instead — i.e. a real,
    already-observed divergence, not a hypothetical one."""
    await db.conn.execute("DROP TABLE history.war_attacks")
    await db.conn.execute("""
        CREATE TABLE history.war_attacks (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            war_id                 TEXT    NOT NULL,
            clan_tag               TEXT    NOT NULL,
            date                   TEXT    NOT NULL,
            player_name            TEXT    NOT NULL,
            player_tag             TEXT    NOT NULL,
            th_level               INTEGER NOT NULL,
            map_position           INTEGER NOT NULL DEFAULT 0,
            attack_order           INTEGER NOT NULL DEFAULT 0,
            stars                  INTEGER NOT NULL,
            destruction            REAL    NOT NULL DEFAULT 0.0,
            defender_tag           TEXT    NOT NULL DEFAULT '',
            defender_th            INTEGER NOT NULL DEFAULT 0,
            defender_map_position  INTEGER NOT NULL DEFAULT 0,
            duration               INTEGER NOT NULL DEFAULT 0,
            is_fresh               INTEGER NOT NULL DEFAULT -1,
            times_defended         INTEGER NOT NULL DEFAULT 0,
            best_def_destruction   REAL    NOT NULL DEFAULT 0.0,
            max_attacks            INTEGER NOT NULL DEFAULT 2,
            missed_attacks         INTEGER NOT NULL DEFAULT 0,
            defensive_stars        INTEGER NOT NULL DEFAULT 0,
            created_at             TEXT    DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(war_id, player_tag, attack_order)
        )
    """)
    await db.conn.commit()


@pytest.mark.integration
class TestReadQueriesSurviveHistoryColumnDrift:
    async def test_get_player_attack_history_sync_reads_correct_values_from_drifted_history(self, tmp_path):
        db = WarHistoryDB()
        db_path = str(tmp_path / "drift_test.db")
        history_db_path = str(tmp_path / "drift_test_history.db")
        await db.initialize(db_path, history_db_path)
        try:
            await _rebuild_history_war_attacks_with_real_drift_order(db)

            # A CWL attack (max_attacks=1) old enough to have already migrated to `history` —
            # named columns on INSERT, so this is correct regardless of physical order.
            await db.conn.execute(
                "INSERT INTO history.war_attacks "
                "(war_id, clan_tag, date, player_name, player_tag, th_level, attack_order, "
                " stars, max_attacks, missed_attacks, defensive_stars) "
                "VALUES ('war_1', '#CLAN', '2020-01-01', 'Butterblume', '#P1', 18, 1, "
                " 3, 1, 0, 2)",
            )
            await db.conn.commit()

            records = db.get_player_attack_history_sync(["#P1"], month=1, year=2020)
            assert len(records) == 1
            r = records[0]
            # The whole point: a SELECT * UNION would read these back scrambled (e.g.
            # max_attacks landing under is_fresh's real value, or vice versa) because
            # history's physical column order differs from main's for these fields.
            assert r["Stars"] == 3
            assert r["Max_Attacks"] == 1
            assert r["Missed_Attacks"] == 0
            assert r["Defensive_Stars"] == 2
            assert r["Attacks"] == 1  # max_attacks - missed_attacks
        finally:
            await db.close()

    async def test_get_clan_attack_history_sync_reads_correct_values_from_drifted_history(self, tmp_path):
        db = WarHistoryDB()
        db_path = str(tmp_path / "drift_test2.db")
        history_db_path = str(tmp_path / "drift_test2_history.db")
        await db.initialize(db_path, history_db_path)
        try:
            await _rebuild_history_war_attacks_with_real_drift_order(db)

            await db.conn.execute(
                "INSERT INTO history.war_attacks "
                "(war_id, clan_tag, date, player_name, player_tag, th_level, attack_order, "
                " stars, max_attacks, missed_attacks, defensive_stars) "
                "VALUES ('war_2', '#CLAN', '2020-02-01', 'Killer', '#P2', 17, 1, "
                " 2, 1, 0, 1)",
            )
            await db.conn.commit()

            records = db.get_clan_attack_history_sync("#CLAN", month=2, year=2020)
            assert len(records) == 1
            r = records[0]
            assert r["Stars"] == 2
            assert r["Max_Attacks"] == 1
            assert r["Missed_Attacks"] == 0
            assert r["Defensive_Stars"] == 1
            assert r["Attacks"] == 1
        finally:
            await db.close()
