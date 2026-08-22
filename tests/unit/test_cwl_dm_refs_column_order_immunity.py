"""Cardinal Rule 14 regression guard for
`get_cwl_player_season_status_dm_refs_for_event_sync()` (fixed 2026-08-23).

That reader used to build its result with positional row access:

    {"player_tag": row[0], "dmed_discord_id": row[1],
     "message_id": row[2], "channel_id": row[3]}

WHAT THE RISK ACTUALLY IS — the obvious guess is wrong, and this test file was initially written
against the wrong one. Because the reader's SELECT names its four columns explicitly, `row[N]`
indexes the PROJECTION, not physical storage. An `ALTER TABLE ... ADD COLUMN` reorder therefore
genuinely CANNOT break it: verified empirically by reverting the fix and watching
`test_reader_survives_a_physically_reordered_table` below still pass. Rule 14's headline incident
(`bool(row[12])` silently reading `created_at`) was a `SELECT *` case, which this is not.

The real exposure is a **code edit**. Add or reorder a single column in that SELECT list and every
index shifts onto the wrong field — silently, with no error and no type mismatch to trip over,
since `dmed_discord_id`, `dm_sent_via_message_id` and `dm_sent_via_channel_id` are all TEXT ids of
similar shape. The blast radius is concrete: the only consumer,
`cleanup_stale_cwl_enrollment_dms()`, feeds these values straight into `bot.fetch_user(...)` and
`channel.fetch_message(...)` in order to DELETE a Discord message. Swapped fields there mean
deleting the wrong message, or hammering the API with garbage ids.

So the guard that actually earns its place here is the structural one,
`test_no_positional_row_access_remains_in_the_reader` — it is the test that fails against the old
code (confirmed by reverting), and it fails on the *next* reintroduction of the pattern too. The
behavioural tests around it pin the reader's real contract and the column-order immunity of the
whole write→read path, which is worth having on its own terms even though it is not what
distinguishes the fix.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")

from qapbot.db_manager import WarHistoryDB


# Deliberately distinguishable values: if any two ever get swapped, the assertion names exactly
# which pair moved rather than just reporting "not equal".
PLAYER_TAG = "#PLAYER1"
DMED_DISCORD_ID = "700000000000000001"
MESSAGE_ID = "800000000000000002"
CHANNEL_ID = "900000000000000003"


@pytest.fixture
async def db(tmp_path):
    manager = WarHistoryDB()
    await manager.initialize(str(tmp_path / "dmrefs.db"), str(tmp_path / "dmrefs_history.db"))
    try:
        yield manager
    finally:
        await manager.close()


async def _seed_event_and_dm(db: WarHistoryDB, guild_id: str = "500", season: str = "2026-09") -> int:
    """One event plus one cwl_player_season_status row with a real DM recorded against it."""
    await db._conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
    await db._conn.commit()

    event_id = db.create_cwl_event_sync(guild_id, season, "creator")
    db.mark_cwl_player_dm_sent_sync(
        PLAYER_TAG, season, "PlayerOne", DMED_DISCORD_ID, event_id, int(guild_id),
        "2026-09-01T09:00Z", message_id=MESSAGE_ID, channel_id=CHANNEL_ID,
    )
    return event_id


def _assert_ref_is_correct(ref: dict) -> None:
    """Every field checked by VALUE, not just by presence — a mis-indexed read returns a fully
    populated dict with the values rotated between keys, so a shape-only assertion passes."""
    assert ref["player_tag"] == PLAYER_TAG
    assert ref["dmed_discord_id"] == DMED_DISCORD_ID
    assert ref["message_id"] == MESSAGE_ID
    assert ref["channel_id"] == CHANNEL_ID


def test_no_positional_row_access_remains_in_the_reader():
    """THE guard for this fix — the one test that actually fails against the old code (confirmed
    by reverting the fix and re-running: it reports ['row[0]', 'row[1]', 'row[2]', 'row[3]']).

    Structural rather than behavioural because the defect is an edit-time hazard, not a runtime
    one: positional access here is correct *today* and only becomes wrong the moment someone
    touches the SELECT list. No behavioural test can catch that in advance — but this one stops
    the pattern coming back.

    Scoped to the single reader rather than the whole module on purpose: db_manager.py
    legitimately uses row[1] for `PRAGMA table_info` introspection and for app-level parameter
    tuples, neither of which Rule 14 covers.
    """
    import inspect
    import re

    source = inspect.getsource(WarHistoryDB.get_cwl_player_season_status_dm_refs_for_event_sync)
    # Strip the docstring and comments — they legitimately quote the old positional form.
    body = re.sub(r'""".*?"""', "", source, flags=re.DOTALL)
    body = "\n".join(line.split("#", 1)[0] for line in body.splitlines())

    offenders = re.findall(r"\brow\[\d+\]", body)
    assert offenders == [], f"positional row access is back in the reader: {offenders}"


@pytest.mark.asyncio
async def test_reader_returns_the_correct_values(db):
    """The reader's actual contract, asserted by value — the baseline every other test here
    builds on."""
    event_id = await _seed_event_and_dm(db)

    refs = db.get_cwl_player_season_status_dm_refs_for_event_sync(event_id)

    assert len(refs) == 1
    _assert_ref_is_correct(refs[0])


@pytest.mark.asyncio
async def test_positional_access_breaks_when_the_projection_changes(db):
    """Demonstrates the real failure mode the fix forecloses, so the structural guard above is not
    just asserting that correct code is correct.

    Runs the reader's own WHERE clause under a SELECT list with one extra leading column — exactly
    what a future edit ("also fetch dm_sent_at") would produce — and shows that positional access
    now yields the wrong fields while named access is unaffected. Note the values are all
    plausible-looking strings: nothing would throw, the wrong Discord message would simply be
    deleted.
    """
    await _seed_event_and_dm(db)

    with db._sync_conn() as conn:
        row = conn.execute(
            """
            SELECT dm_sent_at, player_tag, dmed_discord_id, dm_sent_via_message_id,
                   dm_sent_via_channel_id
            FROM cwl_player_season_status
            WHERE player_tag = ?
            """,
            (PLAYER_TAG,),
        ).fetchone()

    # What the old code would now produce — every field shifted by one, silently.
    assert row[0] != PLAYER_TAG, "row[0] is dm_sent_at, not player_tag"
    assert row[1] != DMED_DISCORD_ID
    assert row[2] != MESSAGE_ID
    assert row[3] != CHANNEL_ID
    # ...and the value that would have been passed to bot.fetch_user() is now a player tag.
    assert row[1] == PLAYER_TAG

    # What the fix does — immune to the same edit.
    assert row["player_tag"] == PLAYER_TAG
    assert row["dmed_discord_id"] == DMED_DISCORD_ID
    assert row["dm_sent_via_message_id"] == MESSAGE_ID
    assert row["dm_sent_via_channel_id"] == CHANNEL_ID


@pytest.mark.asyncio
async def test_write_then_read_path_survives_a_physically_reordered_table(db):
    """Column-order immunity of the whole write→read path, end to end.

    Deliberately NOT presented as the guard for this fix: verified by reverting, the old
    positional version passes this too, because the reader's explicit SELECT list fixes the
    projection order regardless of physical storage. It is kept because the property is real and
    worth pinning — `mark_cwl_player_dm_sent_sync()`'s named INSERT and this reader together must
    stay immune to the `ALTER TABLE ADD COLUMN` reshaping this table has already undergone twice
    (dm_sent_via_message_id / dm_sent_via_channel_id, 2026-08-19), and which has already left
    `main` and `history` war_summary in different physical orders.
    """
    event_id = await _seed_event_and_dm(db)

    # A different physical layout than CREATE TABLE produces: the three columns the reader depends
    # on are pushed to the end, and dm_sent (an INTEGER) is moved into the slot dmed_discord_id
    # used to occupy. Column SET is unchanged — the "pure reorder" case Cardinal Rule 1 calls
    # harmless *provided* every access names its columns.
    await db._conn.execute("ALTER TABLE cwl_player_season_status RENAME TO _cwl_pss_old")
    await db._conn.execute("""
        CREATE TABLE cwl_player_season_status (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            player_tag             TEXT    NOT NULL,
            dm_sent                INTEGER NOT NULL DEFAULT 0,
            cwl_season             TEXT    NOT NULL,
            status                 TEXT    NOT NULL DEFAULT 'pending',
            player_name            TEXT,
            dm_sent_at             TEXT,
            dm_sent_via_event_id   INTEGER,
            dm_sent_via_guild_id   TEXT,
            responded_at           TEXT,
            responded_via_event_id INTEGER,
            responded_via_guild_id TEXT,
            created_at             TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at             TEXT    NOT NULL DEFAULT (datetime('now')),
            dm_sent_via_message_id TEXT,
            dm_sent_via_channel_id TEXT,
            dmed_discord_id        TEXT,
            UNIQUE (player_tag, cwl_season)
        )
    """)
    # Column-name-explicit copy across the rebuild (Cardinal Rule 1) — a SELECT * here would
    # itself corrupt the data and mask what this test is measuring.
    await db._conn.execute("""
        INSERT INTO cwl_player_season_status
            (player_tag, cwl_season, player_name, dmed_discord_id, dm_sent, dm_sent_at,
             dm_sent_via_event_id, dm_sent_via_guild_id, dm_sent_via_message_id,
             dm_sent_via_channel_id, status, responded_at, responded_via_event_id,
             responded_via_guild_id)
        SELECT player_tag, cwl_season, player_name, dmed_discord_id, dm_sent, dm_sent_at,
               dm_sent_via_event_id, dm_sent_via_guild_id, dm_sent_via_message_id,
               dm_sent_via_channel_id, status, responded_at, responded_via_event_id,
               responded_via_guild_id
        FROM _cwl_pss_old
    """)
    await db._conn.execute("DROP TABLE _cwl_pss_old")
    await db._conn.commit()

    # Sanity: the reorder really happened, otherwise this test proves nothing at all.
    with db._sync_conn() as conn:
        physical = [r["name"] for r in conn.execute("PRAGMA table_info(cwl_player_season_status)")]
    assert physical.index("dmed_discord_id") > physical.index("dm_sent_via_channel_id"), (
        "fixture failed to reorder the table — the test would pass vacuously"
    )

    refs = db.get_cwl_player_season_status_dm_refs_for_event_sync(event_id)

    assert len(refs) == 1
    _assert_ref_is_correct(refs[0])


@pytest.mark.asyncio
async def test_guild_clan_roles_read_is_also_name_based(db):
    """The identical violation found in the same file during the 2026-08-23 audit —
    get_guild_config()'s `{r[0]: r[1]}` over guild_clan_roles, fixed alongside."""
    guild_id = "501"
    await db._conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
    await db._conn.execute(
        "INSERT INTO guild_clan_roles (guild_id, clan_tag, role_id) VALUES (?, '#CLANA', '4242')",
        (guild_id,),
    )
    await db._conn.commit()

    config = await db.get_guild_config(guild_id)

    assert config["clan_roles"] == {"#CLANA": "4242"}
