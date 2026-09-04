"""Regression tests for the Phase-1 SQLite round-trip storm (2026-09-04, build 10 profiling).

`_save_user_impl()` used to call `_ensure_clan_exists()` once per player of the user being
saved, and `_replace_user_players_rows()` used to INSERT one `await execute()` per player.
For discord_id="UNASSIGNED" — the pseudo-user holding every unlinked player the bot has ever
seen, 5,794 of 6,076 `user_players` rows on PROD — a single newly-discovered player therefore
cost ~11,600 event-loop round-trips.  One cold Phase-1 issued ~418,000 of them and spun the
loop 840,029 times (a warm one: 7,565), which was the dominant cost of a 287.7s cycle.

These tests assert the round-trip COUNT, not just the resulting rows: the correctness tests
elsewhere already passed while the storm was happening, so only a count assertion can fail if
someone reintroduces a per-player `await` in either path.
"""
# pyright: reportPrivateUsage=false, reportMissingParameterType=false
from __future__ import annotations

from typing import Any, Dict, List

import pytest


@pytest.fixture
async def db(tmp_path):  # type: ignore[no-untyped-def]
    from qapbot.db_manager import WarHistoryDB

    manager = WarHistoryDB()
    await manager.initialize(str(tmp_path / "qapbot_test.db"))
    try:
        yield manager
    finally:
        await manager.close()


def _players(n: int, clan_tag: str = "#AAA111") -> List[Dict[str, Any]]:
    return [
        {"player_tag": f"#P{i:05d}", "player_name": f"P{i}", "current_clan_tag": clan_tag}
        for i in range(n)
    ]


class _CountingConn:
    """Wraps the real aiosqlite connection and counts awaited statement calls."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.execute_calls = 0
        self.executemany_calls = 0

    async def execute(self, *a: Any, **k: Any) -> Any:
        self.execute_calls += 1
        return await self._inner.execute(*a, **k)

    async def executemany(self, *a: Any, **k: Any) -> Any:
        self.executemany_calls += 1
        return await self._inner.executemany(*a, **k)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@pytest.mark.integration
class TestEnsureClansExistIsBulk:
    async def test_many_existing_tags_cost_two_queries_per_chunk(self, db) -> None:  # type: ignore[no-untyped-def]
        """The production shape: the tags overwhelmingly exist already, so this is pure lookup.

        Every clan the UNASSIGNED pool references was inserted by an earlier poll, so the
        steady-state cost of a save is exactly this lookup — which must not scale with pool
        size. (Inserting genuinely-new clans still costs per-clan writes, correctly so; that is
        bounded by real discoveries, not by how many players the pool holds.)
        """
        tags = [f"#C{i:05d}" for i in range(500)]
        await db._ensure_clans_exist(tags)  # first pass creates them

        counting = _CountingConn(db.conn)
        db.conn = counting
        await db._ensure_clans_exist(tags)

        # 2 SELECTs per 400-tag chunk (clan_families + clans) = 4 for 500 tags.
        assert counting.execute_calls <= 6, (
            f"lookup did not batch: {counting.execute_calls} execute() calls for 500 known tags"
        )

    async def test_duplicate_tags_are_collapsed(self, db) -> None:  # type: ignore[no-untyped-def]
        """The UNASSIGNED pool is thousands of players across a few hundred clans."""
        counting = _CountingConn(db.conn)
        db.conn = counting

        await db._ensure_clans_exist(["#AAA111"] * 2000)

        assert counting.execute_calls <= 5, (
            f"2000 copies of one tag cost {counting.execute_calls} calls; expected de-duplication"
        )
        cursor = await db.conn.execute("SELECT COUNT(*) c FROM clans WHERE clan_tag = ?", ("#AAA111",))
        assert (await cursor.fetchone())["c"] == 1

    async def test_invalid_and_empty_input_is_a_no_op(self, db) -> None:  # type: ignore[no-untyped-def]
        await db._ensure_clans_exist([])
        await db._ensure_clans_exist(["", "1145641080621109312", "#", None])  # type: ignore[list-item]

        cursor = await db.conn.execute("SELECT COUNT(*) c FROM clans")
        assert (await cursor.fetchone())["c"] == 0

    async def test_family_tags_are_never_inserted_as_clans(self, db) -> None:  # type: ignore[no-untyped-def]
        """Family tags in the batch must be skipped, exactly as the per-tag path skipped them.

        Inserting one would make PHASE-1 poll it every cycle and 404 forever.
        """
        await db.conn.execute(
            "INSERT INTO clan_families (family_tag, name, owned_by_guild) VALUES (?, ?, ?)",
            ("#FAM123", "Fam", "1145641080621109312"),
        )
        await db.conn.commit()

        await db._ensure_clans_exist(["#FAM123", "#REAL01"])

        cursor = await db.conn.execute("SELECT clan_tag FROM clans")
        assert {r["clan_tag"] for r in await cursor.fetchall()} == {"#REAL01"}

    async def test_single_tag_wrapper_still_works(self, db) -> None:  # type: ignore[no-untyped-def]
        """_ensure_clan_exists() has many callers and must keep its old behaviour."""
        await db._ensure_clan_exists("#SOLO01")

        cursor = await db.conn.execute("SELECT COUNT(*) c FROM clans WHERE clan_tag = ?", ("#SOLO01",))
        assert (await cursor.fetchone())["c"] == 1

    async def test_existing_clan_is_not_rewritten(self, db) -> None:  # type: ignore[no-untyped-def]
        """A tag already present must be left alone, not re-saved over its real name."""
        await db.save_clan(clan_tag="#KEEP01", name="Real Name")
        await db._ensure_clans_exist(["#KEEP01"])

        cursor = await db.conn.execute("SELECT name FROM clans WHERE clan_tag = ?", ("#KEEP01",))
        assert (await cursor.fetchone())["name"] == "Real Name"


@pytest.mark.integration
class TestSaveUserDoesNotStorm:
    async def test_large_pool_save_does_not_scale_round_trips(self, db) -> None:  # type: ignore[no-untyped-def]
        """The actual regression: saving a 1000-player pool must not cost ~2000 round-trips."""
        await db.save_user("UNASSIGNED", {"players": _players(1000)})

        counting = _CountingConn(db.conn)
        db.conn = counting
        await db.save_user("UNASSIGNED", {"players": _players(1000)})

        assert counting.executemany_calls >= 1, "player INSERTs are not batched"
        assert counting.execute_calls < 100, (
            f"save_user of 1000 players cost {counting.execute_calls} execute() round-trips — "
            "a per-player await was reintroduced"
        )

    async def test_rows_are_still_written_correctly(self, db) -> None:  # type: ignore[no-untyped-def]
        """Batching must not change what lands in the table."""
        await db.save_user("UNASSIGNED", {"players": _players(50)})

        cursor = await db.conn.execute(
            "SELECT player_tag, player_name, current_clan_tag FROM user_players "
            "WHERE discord_id = ? ORDER BY player_tag",
            ("UNASSIGNED",),
        )
        rows = await cursor.fetchall()
        assert len(rows) == 50
        assert rows[0]["player_tag"] == "#P00000"
        assert rows[0]["player_name"] == "P0"
        assert rows[0]["current_clan_tag"] == "#AAA111"

    async def test_duplicate_player_tags_still_deduped(self, db) -> None:  # type: ignore[no-untyped-def]
        """The 2026-08-21 duplicate guard must survive the executemany rewrite.

        A duplicate reaching the UNIQUE(discord_id, player_tag) constraint raises
        IntegrityError, which historically poisoned the UNASSIGNED pool permanently.
        """
        dupes = _players(3) + [{"player_tag": "#P00000", "player_name": "dupe", "current_clan_tag": "#AAA111"}]
        await db.save_user("UNASSIGNED", {"players": dupes})

        cursor = await db.conn.execute(
            "SELECT player_name FROM user_players WHERE discord_id = ? AND player_tag = ?",
            ("UNASSIGNED", "#P00000"),
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1
        assert rows[0]["player_name"] == "P0", "dedup must keep the FIRST occurrence"

    async def test_empty_player_list_clears_the_pool(self, db) -> None:  # type: ignore[no-untyped-def]
        """The early `return` added for the empty case must not skip the DELETE."""
        await db.save_user("UNASSIGNED", {"players": _players(5)})
        await db.save_user("UNASSIGNED", {"players": []})

        cursor = await db.conn.execute(
            "SELECT COUNT(*) c FROM user_players WHERE discord_id = ?", ("UNASSIGNED",)
        )
        assert (await cursor.fetchone())["c"] == 0
