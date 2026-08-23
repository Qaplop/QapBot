"""Tests for reroute_cwl_enrollment_dms_after_ownership_change() (QBdiscocmdshelper_cwl.py,
2026-08-22, tracker #0019).

When a CoC account changes its Discord owner, an enrollment DM already sitting in the OLD owner's
inbox keeps pointing at that account. The sign-up button still works for whoever received the DM
(deliberately — tracker #0016 widened the guard to accept the recorded recipient), so the previous
owner can answer for an account they no longer own and the NEW owner is never asked at all.

Project owner's spec, and the split these tests pin down:
  - UNANSWERED ('pending') -> delete the old owner's DM, re-send to the new owner.
  - ANSWERED (confirmed/declined) -> leave completely untouched; the response is a real
    historical fact.
  - UNLINKED (nobody owns it now) -> also untouched. That is ownership *removal*, not a change,
    and the owner explicitly confirmed for the live #LLV0Y9PQ / .zuurn case that the old
    recipient's button should keep working.
"""
from __future__ import annotations

import asyncio
import dataclasses
import os
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")

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


@pytest.fixture(autouse=True)
def _no_dm_guard(monkeypatch):
    """CONFIG.cwl_dm_restrict_to_admin would otherwise swallow every re-send in the DM batch."""
    from qapbot import config as config_module

    monkeypatch.setattr(
        config_module, "CONFIG",
        dataclasses.replace(config_module.CONFIG, is_dev_mode=False, cwl_dm_restrict_to_admin=False),
    )


def _make_bot(deleted: List[int]) -> MagicMock:
    """A bot whose fetch_user -> dm_channel -> fetch_message -> delete chain records what it
    deleted, so a test can assert the OLD message really was retracted."""
    bot = MagicMock()

    def _make_message(message_id: int) -> MagicMock:
        message = MagicMock()
        message.id = message_id

        async def _delete():
            deleted.append(message_id)

        message.delete = _delete
        return message

    channel = MagicMock()
    channel.fetch_message = AsyncMock(side_effect=lambda mid: _make_message(mid))

    user = MagicMock()
    user.dm_channel = channel
    user.create_dm = AsyncMock(return_value=channel)
    bot.fetch_user = AsyncMock(return_value=user)
    return bot


async def _seed(
    db: WarHistoryDB,
    *,
    guild_id: str = "900",
    season: str = "2026-09",
    old_owner: str = "111",
    new_owner: str | None = "222",
    global_status: str = "pending",
    event_status: str = "signup_open",
    dm_message_id: str | None = "77001",
) -> int:
    """One signup_open event, one pooled player DMed to `old_owner`, now owned by `new_owner`
    (None = unlinked, "UNASSIGNED" = in the unassigned pool). Returns the event_id."""
    from qapbot.cache_manager import CACHE

    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
    await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES ('#CLAN1', 'Alpha')")
    for uid in {old_owner, new_owner} - {None}:
        await db.conn.execute(
            "INSERT OR IGNORE INTO users (discord_id, display_name) VALUES (?, ?)", (uid, uid)
        )
    if new_owner is not None:
        await db.conn.execute(
            "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag) "
            "VALUES (?, '#P1', 'PlayerOne', 1, '#CLAN1')",
            (new_owner,),
        )
    await db.conn.commit()

    CACHE.db_manager = db
    CACHE.server_config[guild_id] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    event_id = db.create_cwl_event_sync(guild_id, season, "creator")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    db.update_cwl_event_status_sync(event_id, event_status)

    db.upsert_cwl_signup_sync(
        event_id, "#P1", "PlayerOne", old_owner, None, "template_confirm", global_status
    )
    db.mark_cwl_player_dm_sent_sync(
        "#P1", season, "PlayerOne", old_owner, event_id, int(guild_id), "2026-08-20T09:00Z",
        message_id=dm_message_id, channel_id="88001",
    )
    if global_status != "pending":
        db.set_cwl_player_response_status_sync(
            "#P1", season, "PlayerOne", old_owner, global_status,
            "2026-08-20T10:00Z", event_id, int(guild_id),
        )
    return event_id


def _capture_dms(monkeypatch) -> List[str]:
    from qapbot.cache_manager import CACHE

    contacted: List[str] = []

    async def fake_send(user_id, message, view=None, embed=None, sent_message_out=None):
        contacted.append(str(user_id))
        dm_message = MagicMock()
        dm_message.id = 99001
        dm_message.channel.id = 88002
        if sent_message_out is not None:
            sent_message_out.append(dm_message)
        return True, "sent"

    monkeypatch.setattr(CACHE, "send_user_dm_detailed", fake_send)
    return contacted


async def _run(monkeypatch, deleted: List[int]) -> Dict[str, int]:
    import QBcore
    from qapbot.QBdiscocmdshelper_cwl import reroute_cwl_enrollment_dms_after_ownership_change

    monkeypatch.setattr(QBcore, "bot", _make_bot(deleted))
    return await reroute_cwl_enrollment_dms_after_ownership_change()


@pytest.mark.asyncio
async def test_pending_dm_is_retracted_and_resent_to_the_new_owner(db, monkeypatch):
    event_id = await _seed(db)
    contacted = _capture_dms(monkeypatch)
    deleted: List[int] = []

    result = await _run(monkeypatch, deleted)

    assert result["rerouted"] == 1
    assert result["retracted"] == 1
    # The old owner's message really was deleted...
    assert deleted == [77001]
    # ...and the new owner got the DM.
    assert contacted == ["222"]

    # Both recorded-recipient columns now name the new owner, so the old owner's button — even if
    # their message somehow survived — no longer passes CwlSignupResponseButton's guard.
    assert db.get_cwl_signup_sync(event_id, "#P1")["dmed_discord_id"] == "222"
    assert db.get_cwl_player_season_status_sync("#P1", "2026-09")["dmed_discord_id"] == "222"


@pytest.mark.asyncio
async def test_fire_and_forget_trigger_runs_the_same_sweep(db, monkeypatch):
    """fire_cwl_dm_reroute_after_ownership_change() (_link_player_to_user()'s near-real-time
    trigger, 2026-08-23 follow-up) must schedule this exact sweep rather than some parallel
    hand-rolled version — verified end to end by letting the scheduled background task actually
    run and checking it produced the same effect as the awaited per-cycle call above."""
    from qapbot.QBdiscocmdshelper_cwl import fire_cwl_dm_reroute_after_ownership_change

    event_id = await _seed(db)
    contacted = _capture_dms(monkeypatch)
    deleted: List[int] = []
    import QBcore
    monkeypatch.setattr(QBcore, "bot", _make_bot(deleted))

    fire_cwl_dm_reroute_after_ownership_change()

    # The trigger only schedules the task (asyncio.create_task) and returns immediately; give the
    # event loop a turn to actually run it, then wait for it to finish.
    current = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not current]
    await asyncio.gather(*pending)

    assert deleted == [77001]
    assert contacted == ["222"]
    assert db.get_cwl_signup_sync(event_id, "#P1")["dmed_discord_id"] == "222"


@pytest.mark.asyncio
@pytest.mark.parametrize("answered", ["confirmed", "declined"])
async def test_an_already_answered_dm_is_never_touched(db, monkeypatch, answered):
    """The project owner's spec is explicit: a response is a real historical fact and must not be
    retracted or re-asked, no matter who owns the account now."""
    event_id = await _seed(db, global_status=answered)
    contacted = _capture_dms(monkeypatch)
    deleted: List[int] = []

    result = await _run(monkeypatch, deleted)

    assert result == {"checked": 0, "rerouted": 0, "retracted": 0, "send_failed": 0, "capped": 0}
    assert deleted == []
    assert contacted == []
    assert db.get_cwl_signup_sync(event_id, "#P1")["dmed_discord_id"] == "111"
    assert db.get_cwl_player_season_status_sync("#P1", "2026-09")["status"] == answered


@pytest.mark.asyncio
async def test_an_unlinked_account_is_left_alone_the_jerry_case(db, monkeypatch):
    """#LLV0Y9PQ was DMed to .zuurn and has since been unlinked; the project owner confirmed that
    button should keep working. Unlinking is ownership removal, not an ownership change — there is
    nobody to re-route to."""
    event_id = await _seed(db, new_owner=None)
    contacted = _capture_dms(monkeypatch)
    deleted: List[int] = []

    result = await _run(monkeypatch, deleted)

    # It IS a candidate (still pending, still open) — it just isn't a re-route.
    assert result["checked"] == 1
    assert result["rerouted"] == 0
    assert deleted == []
    assert contacted == []
    assert db.get_cwl_signup_sync(event_id, "#P1")["dmed_discord_id"] == "111"


@pytest.mark.asyncio
async def test_an_account_in_the_unassigned_pool_counts_as_unlinked(db, monkeypatch):
    """get_player_links_sync reports an UNASSIGNED-pool row as discord_id=None, so it must fall
    through the same way a missing row does — never re-routed to the literal string."""
    event_id = await _seed(db, new_owner="UNASSIGNED")
    contacted = _capture_dms(monkeypatch)
    deleted: List[int] = []

    result = await _run(monkeypatch, deleted)

    assert result["rerouted"] == 0
    assert deleted == []
    assert contacted == []
    assert db.get_cwl_signup_sync(event_id, "#P1")["dmed_discord_id"] == "111"


@pytest.mark.asyncio
async def test_unchanged_ownership_is_a_no_op(db, monkeypatch):
    await _seed(db, old_owner="111", new_owner="111")
    contacted = _capture_dms(monkeypatch)
    deleted: List[int] = []

    result = await _run(monkeypatch, deleted)

    assert result["checked"] == 1
    assert result["rerouted"] == 0
    assert deleted == []
    assert contacted == []


@pytest.mark.asyncio
async def test_event_not_signup_open_is_never_swept(db, monkeypatch):
    """Any other event status makes the button return `signup_closed` regardless, so there is
    nothing to protect — and a finalized event's historical record must not be rewritten."""
    await _seed(db, event_status="finalized")
    contacted = _capture_dms(monkeypatch)
    deleted: List[int] = []

    result = await _run(monkeypatch, deleted)

    assert result["checked"] == 0
    assert contacted == []


@pytest.mark.asyncio
async def test_legacy_row_without_a_message_id_is_still_repointed_and_resent(db, monkeypatch):
    """Rows predating the 2026-08-19 dm_sent_via_* columns have no message to delete. Re-pointing
    dmed_discord_id still fixes the worse half of the bug (the new owner never being asked) AND
    stops the old owner answering, since the button's guard reads that column."""
    event_id = await _seed(db, dm_message_id=None)
    contacted = _capture_dms(monkeypatch)
    deleted: List[int] = []

    result = await _run(monkeypatch, deleted)

    assert result["rerouted"] == 1
    assert result["retracted"] == 0  # nothing to retract
    assert deleted == []
    assert contacted == ["222"]
    assert db.get_cwl_signup_sync(event_id, "#P1")["dmed_discord_id"] == "222"


@pytest.mark.asyncio
async def test_sweep_is_idempotent(db, monkeypatch):
    """Cardinal Rule 12 — a re-routed row's dmed_discord_id then matches the live owner, so it
    stops matching the detection on every subsequent pass."""
    await _seed(db)
    contacted = _capture_dms(monkeypatch)
    deleted: List[int] = []

    first = await _run(monkeypatch, deleted)
    second = await _run(monkeypatch, deleted)

    assert first["rerouted"] == 1
    assert second["rerouted"] == 0
    assert contacted == ["222"]  # exactly one DM, not two


@pytest.mark.asyncio
async def test_a_failed_resend_leaves_the_player_recoverable(db, monkeypatch):
    """The old DM is deleted first on purpose, so a failed re-send must not strand the player: the
    row stays pending with dm_sent=0, which is exactly what "Notify New Pool Members" picks up."""
    from qapbot.cache_manager import CACHE

    await _seed(db)
    monkeypatch.setattr(
        CACHE, "send_user_dm_detailed",
        AsyncMock(return_value=(False, "blocked")),
    )
    deleted: List[int] = []

    result = await _run(monkeypatch, deleted)

    assert result["rerouted"] == 0
    assert result["send_failed"] == 1
    assert deleted == [77001]
    global_row = db.get_cwl_player_season_status_sync("#P1", "2026-09")
    assert global_row["status"] == "pending"
    assert global_row["dm_sent"] == 0


@pytest.mark.asyncio
async def test_per_cycle_cap_bounds_the_dm_burst(db, monkeypatch):
    """A mass re-link must never turn into an unbounded DM burst in one cycle."""
    import qapbot.QBdiscocmdshelper_cwl as cwl_mod
    from qapbot.cache_manager import CACHE

    guild_id, season = "901", "2026-09"
    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
    await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES ('#CLAN1', 'Alpha')")
    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES ('222', 'New')")
    total = 5
    for i in range(total):
        await db.conn.execute(
            "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag) "
            "VALUES ('222', ?, ?, 1, '#CLAN1')",
            (f"#P{i}", f"Player{i}"),
        )
    # Commit every async write BEFORE any sync writer runs — the sync connection is a separate
    # sqlite3 handle and would otherwise hit "database is locked" on the open async transaction.
    await db.conn.commit()
    CACHE.db_manager = db
    CACHE.server_config[guild_id] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    event_id = db.create_cwl_event_sync(guild_id, season, "creator")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    db.update_cwl_event_status_sync(event_id, "signup_open")

    for i in range(total):
        tag = f"#P{i}"
        db.upsert_cwl_signup_sync(event_id, tag, f"Player{i}", "111", None, "template_confirm", "pending")
        db.mark_cwl_player_dm_sent_sync(
            tag, season, f"Player{i}", "111", event_id, int(guild_id), "2026-08-20T09:00Z",
            message_id=str(77100 + i), channel_id="88001",
        )

    monkeypatch.setattr(cwl_mod, "_MAX_DM_REROUTES_PER_CYCLE", 2)
    contacted = _capture_dms(monkeypatch)
    deleted: List[int] = []

    result = await _run(monkeypatch, deleted)

    assert result["rerouted"] == 2
    assert result["capped"] == total - 2
    assert len(contacted) == 2

    # The remainder is picked up by the next cycle rather than being lost.
    second = await _run(monkeypatch, deleted)
    assert second["rerouted"] == 2
    assert len(contacted) == 4
