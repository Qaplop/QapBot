"""Tests for the four-phase CWL model, war-phase transition and roster freeze —
plans/implemented/cwl-phase-model-and-war-phase.md (2026-08-30, project owner's spec).

The load-bearing idea under all of it: a clan's roster is fixed by the GAME the moment its CWL
starts, so from that point the board records reality instead of planning it. Everything here is a
consequence of that — the freeze, its one exception (whoever was in the clan at lock time), the
no-show reconciliation, and the un-deletable season.
"""
from __future__ import annotations

import dataclasses
import os
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")

from qapbot.db_manager import WarHistoryDB

SEASON = "2026-09"


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
def _cache_db(db, monkeypatch):
    from qapbot.cache_manager import CACHE

    monkeypatch.setattr(CACHE, "db_manager", db, raising=False)
    monkeypatch.setattr(CACHE, "server_config", {}, raising=False)
    monkeypatch.setattr(CACHE, "user_accounts", {}, raising=False)
    monkeypatch.setattr(CACHE, "testers", set(), raising=False)
    return CACHE


@pytest.fixture(autouse=True)
def _dm_guard_off(monkeypatch):
    import qapbot.config as config_module

    monkeypatch.setattr(
        config_module, "CONFIG",
        dataclasses.replace(config_module.CONFIG, cwl_dm_restrict_to_admin=False),
    )


async def _seed(db, guild_id: str, clan_tags=("#CLAN1", "#CLAN2")) -> None:
    from qapbot.cache_manager import CACHE

    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
    for tag in clan_tags:
        await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES (?, ?)", (tag, f"Clan{tag[-1]}"))
    await db.conn.commit()
    CACHE.server_config[guild_id] = {
        "member_clans": list(clan_tags), "member_families": [], "cwl_selected_season": SEASON,
    }


async def _player(db, discord_id, player_tag, current_clan_tag, name="Player") -> None:
    owner = discord_id or "UNASSIGNED"
    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES (?, ?)", (owner, owner))
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag) "
        "VALUES (?, ?, ?, 1, ?)",
        (owner, player_tag, name, current_clan_tag),
    )
    await db.conn.commit()


async def _event(db, guild_id, clan_configs, status="announced") -> int:
    event_id = db.create_cwl_event_sync(guild_id, SEASON, "creator")
    db.set_cwl_event_clans_sync(event_id, clan_configs)
    db.update_cwl_event_status_sync(event_id, status)
    return event_id


# ---------------------------------------------------------------------------
# Phase derivation + step indicator
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status,locked,expected_phase,expected_key",
    [
        ("draft", False, 1, "setup"),
        ("signup_open", False, 2, "enrollment"),
        ("announced", False, 3, "preparation"),
        ("war", False, 4, "war"),
        # A locked clan means War even before the sweep has written status='war' — the sweep runs
        # once per cycle, and the indicator must show reality rather than lag a cycle behind it.
        ("announced", True, 4, "war"),
        ("cancelled", False, 0, None),
    ],
)
def test_phase_derivation(status, locked, expected_phase, expected_key):
    from qapbot.QBdiscocmdshelper_cwl import resolve_cwl_phase

    clans = [{"participating": 1, "locked_at": "2026-09-01T08:00Z" if locked else None}]
    info = resolve_cwl_phase({"status": status}, clans)
    assert info["phase"] == expected_phase
    assert info["key"] == expected_key


def test_phase_none_event_has_no_indicator():
    from qapbot.QBdiscocmdshelper_cwl import render_cwl_step_indicator, resolve_cwl_phase

    assert render_cwl_step_indicator(resolve_cwl_phase(None), 1) is None


def test_step_indicator_marks_done_current_and_upcoming():
    from qapbot.QBdiscocmdshelper_cwl import render_cwl_step_indicator, resolve_cwl_phase

    info = resolve_cwl_phase({"status": "announced"}, [{"participating": 1, "locked_at": None}])
    rendered = render_cwl_step_indicator(info, 1)

    assert "✅ Setup" in rendered
    assert "✅ Enrollment" in rendered
    assert "🔵 **Preparation**" in rendered   # current step: marker + bold
    assert "⚪ War" in rendered


def test_step_indicator_war_shows_how_many_clans_actually_started():
    """A guild reaches War when the FIRST clan starts — a bare "War" would imply all of them have."""
    from qapbot.QBdiscocmdshelper_cwl import render_cwl_step_indicator, resolve_cwl_phase

    clans = [
        {"participating": 1, "locked_at": "x"},
        {"participating": 1, "locked_at": None},
        {"participating": 1, "locked_at": None},
        # A non-participating clan must not inflate the denominator.
        {"participating": 0, "locked_at": None},
    ]
    rendered = render_cwl_step_indicator(resolve_cwl_phase({"status": "war"}, clans), 1)

    assert "🔵 **War (1/3 started)**" in rendered


# ---------------------------------------------------------------------------
# locked_at must survive an ordinary clan-config save
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clan_config_save_preserves_locked_at(db):
    """Regression: set_cwl_event_clans_sync is a DELETE + INSERT, so a naive re-insert would
    silently un-freeze every locked clan on the next ordinary save of the config screen — and
    bounce the guild's phase indicator back out of War."""
    guild_id = "300"
    await _seed(db, guild_id)
    event_id = await _event(db, guild_id, [{"clan_tag": "#CLAN1", "cwl_start_at": f"{SEASON}-01T08:00Z"}])
    db.mark_cwl_event_clan_locked_sync("#CLAN1", SEASON)
    assert db.get_cwl_event_clans_sync(event_id)[0]["locked_at"] is not None

    # An ordinary save from the config screen, which knows nothing about locked_at.
    db.set_cwl_event_clans_sync(
        event_id, [{"clan_tag": "#CLAN1", "roster_size": 30, "cwl_start_at": f"{SEASON}-01T09:00Z"}]
    )

    row = db.get_cwl_event_clans_sync(event_id)[0]
    assert row["locked_at"] is not None, "locked_at must survive a config save"
    assert row["roster_size"] == 30, "the admin's own edits must still apply"


# ---------------------------------------------------------------------------
# Roster freeze
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_freeze_blocks_moving_a_player_out_of_a_started_clan(db):
    from qapbot.QBdiscocmdshelper_cwl import assign_cwl_player_sync

    guild_id = "301"
    await _seed(db, guild_id)
    event_id = await _event(db, guild_id, [{"clan_tag": "#CLAN1"}, {"clan_tag": "#CLAN2"}])
    await _player(db, "u1", "#P1", "#CLAN1")
    db.upsert_cwl_assignment_sync(event_id, "#P1", "#CLAN1", assignment_source="admin_override", locked=True)
    db.mark_cwl_event_clan_locked_sync("#CLAN1", SEASON)
    db.store_cwl_locked_clan_members_sync(SEASON, "#CLAN1", [{"player_tag": "#P1"}], "league_group")

    error = assign_cwl_player_sync(
        int(guild_id), event_id, SEASON, "#P1", "#CLAN2", source="admin_override", deliberate=True
    )

    assert error is not None and "locked in-game" in error
    assert db.get_cwl_assignments_sync(event_id)[0]["assigned_clan_tag"] == "#CLAN1"


@pytest.mark.asyncio
async def test_freeze_blocks_adding_an_ineligible_player_to_a_started_clan(db):
    from qapbot.QBdiscocmdshelper_cwl import assign_cwl_player_sync

    guild_id = "302"
    await _seed(db, guild_id)
    event_id = await _event(db, guild_id, [{"clan_tag": "#CLAN1"}, {"clan_tag": "#CLAN2"}])
    await _player(db, "u1", "#LATE", "#CLAN2")
    db.mark_cwl_event_clan_locked_sync("#CLAN1", SEASON)
    db.store_cwl_locked_clan_members_sync(SEASON, "#CLAN1", [{"player_tag": "#SOMEONEELSE"}], "league_group")

    error = assign_cwl_player_sync(
        int(guild_id), event_id, SEASON, "#LATE", "#CLAN1", source="admin_override", deliberate=True
    )

    assert error is not None and "not in the clan when its" in error
    assert db.get_cwl_assignments_sync(event_id) == []


@pytest.mark.asyncio
async def test_freeze_allows_a_player_who_was_in_the_clan_at_lock_time(db):
    """The one exception, project owner's spec: someone sitting in the unassigned pool who WAS in
    the clan when it started is still eligible and may be dragged in during War phase."""
    from qapbot.QBdiscocmdshelper_cwl import assign_cwl_player_sync

    guild_id = "303"
    await _seed(db, guild_id)
    event_id = await _event(db, guild_id, [{"clan_tag": "#CLAN1"}, {"clan_tag": "#CLAN2"}])
    await _player(db, "u1", "#ELIGIBLE", "#CLAN1")
    db.mark_cwl_event_clan_locked_sync("#CLAN1", SEASON)
    db.store_cwl_locked_clan_members_sync(SEASON, "#CLAN1", [{"player_tag": "#ELIGIBLE"}], "league_group")

    error = assign_cwl_player_sync(
        int(guild_id), event_id, SEASON, "#ELIGIBLE", "#CLAN1", source="admin_override", deliberate=True
    )

    assert error is None
    assert db.get_cwl_assignments_sync(event_id)[0]["assigned_clan_tag"] == "#CLAN1"


@pytest.mark.asyncio
async def test_freeze_leaves_unlocked_clans_completely_alone(db):
    """The freeze is per-clan: a family's other clans keep working normally while one is at war."""
    from qapbot.QBdiscocmdshelper_cwl import assign_cwl_player_sync

    guild_id = "304"
    await _seed(db, guild_id, ("#CLAN1", "#CLAN2", "#CLAN3"))
    event_id = await _event(db, guild_id, [{"clan_tag": t} for t in ("#CLAN1", "#CLAN2", "#CLAN3")])
    await _player(db, "u1", "#P1", "#CLAN2")
    db.upsert_cwl_assignment_sync(event_id, "#P1", "#CLAN2", assignment_source="admin_override", locked=True)
    db.mark_cwl_event_clan_locked_sync("#CLAN1", SEASON)  # a DIFFERENT clan started
    db.store_cwl_locked_clan_members_sync(SEASON, "#CLAN1", [{"player_tag": "#X"}], "league_group")

    error = assign_cwl_player_sync(
        int(guild_id), event_id, SEASON, "#P1", "#CLAN3", source="admin_override", deliberate=True
    )

    assert error is None
    assert db.get_cwl_assignments_sync(event_id)[0]["assigned_clan_tag"] == "#CLAN3"


# ---------------------------------------------------------------------------
# Locked-roster snapshot + no-show reconciliation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_locked_member_snapshot_is_write_once(db):
    """Never re-taken: by the time a later cycle re-observes the lock, membership has drifted, and
    overwriting would destroy the only record of who was actually eligible."""
    assert db.store_cwl_locked_clan_members_sync(SEASON, "#CLAN1", [{"player_tag": "#A"}], "league_group") == 1
    assert db.store_cwl_locked_clan_members_sync(SEASON, "#CLAN1", [{"player_tag": "#B"}], "live_membership") == 1
    # #A survives; a genuine re-run of the same set inserts nothing.
    assert db.store_cwl_locked_clan_members_sync(SEASON, "#CLAN1", [{"player_tag": "#A"}], "league_group") == 0
    assert db.get_cwl_locked_clan_member_tags_sync(SEASON, "#CLAN1") == {"#A", "#B"}


@pytest.mark.asyncio
async def test_no_show_is_dropped_to_the_pool_and_dmed(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import reconcile_cwl_locked_clan_roster

    guild_id = "305"
    await _seed(db, guild_id)
    event_id = await _event(db, guild_id, [{"clan_tag": "#CLAN1"}])
    await _player(db, "u1", "#MOVED", "#CLAN1", "Moved")
    await _player(db, "u2", "#NOSHOW", "#CLAN2", "NoShow")
    for tag in ("#MOVED", "#NOSHOW"):
        db.upsert_cwl_assignment_sync(event_id, tag, "#CLAN1", assignment_source="admin_override", locked=True)
        db.mark_cwl_assignment_notified_sync(event_id, tag)
    db.store_cwl_locked_clan_members_sync(SEASON, "#CLAN1", [{"player_tag": "#MOVED"}], "league_group")
    sent = AsyncMock(return_value=(True, "sent"))
    monkeypatch.setattr(CACHE, "send_user_dm_detailed", sent, raising=False)

    result = await reconcile_cwl_locked_clan_roster(int(guild_id), event_id, SEASON, "#CLAN1", "Clan1")

    assert result["dropped"] == 1 and result["dm_sent"] == 1
    remaining = {a["player_tag"] for a in db.get_cwl_assignments_sync(event_id)}
    assert remaining == {"#MOVED"}, "only the no-show is dropped"
    assert "NoShow" in sent.await_args.args[1]


@pytest.mark.asyncio
async def test_reconciliation_does_nothing_without_a_snapshot(db, monkeypatch):
    """Safety valve: an empty eligible set means the snapshot failed, NOT that nobody is eligible.
    Dropping a whole roster on the strength of a failed API call would be catastrophic."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import reconcile_cwl_locked_clan_roster

    guild_id = "306"
    await _seed(db, guild_id)
    event_id = await _event(db, guild_id, [{"clan_tag": "#CLAN1"}])
    await _player(db, "u1", "#P1", "#CLAN2")
    db.upsert_cwl_assignment_sync(event_id, "#P1", "#CLAN1", assignment_source="admin_override", locked=True)
    db.mark_cwl_assignment_notified_sync(event_id, "#P1")
    sent = AsyncMock(return_value=(True, "sent"))
    monkeypatch.setattr(CACHE, "send_user_dm_detailed", sent, raising=False)

    result = await reconcile_cwl_locked_clan_roster(int(guild_id), event_id, SEASON, "#CLAN1", "Clan1")

    assert result["dropped"] == 0
    assert len(db.get_cwl_assignments_sync(event_id)) == 1
    assert sent.await_count == 0


@pytest.mark.asyncio
async def test_never_announced_no_show_is_dropped_silently(db, monkeypatch):
    """Someone never told to be there has nothing to be corrected about — drop, but no DM."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import reconcile_cwl_locked_clan_roster

    guild_id = "307"
    await _seed(db, guild_id)
    event_id = await _event(db, guild_id, [{"clan_tag": "#CLAN1"}])
    await _player(db, "u1", "#QUIET", "#CLAN2")
    db.upsert_cwl_assignment_sync(event_id, "#QUIET", "#CLAN1", assignment_source="admin_override", locked=True)
    db.store_cwl_locked_clan_members_sync(SEASON, "#CLAN1", [{"player_tag": "#OTHER"}], "league_group")
    sent = AsyncMock(return_value=(True, "sent"))
    monkeypatch.setattr(CACHE, "send_user_dm_detailed", sent, raising=False)

    result = await reconcile_cwl_locked_clan_roster(int(guild_id), event_id, SEASON, "#CLAN1", "Clan1")

    assert result["dropped"] == 1 and result["dm_sent"] == 0
    assert sent.await_count == 0


@pytest.mark.asyncio
async def test_first_locked_clan_moves_the_event_into_war(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import check_cwl_roster_switches

    guild_id = "308"
    await _seed(db, guild_id)
    event_id = await _event(db, guild_id, [{"clan_tag": "#CLAN1", "cwl_start_at": f"{SEASON}-01T08:00Z"},
                                           {"clan_tag": "#CLAN2", "cwl_start_at": f"{SEASON}-02T08:00Z"}])
    await db.conn.execute(
        "INSERT INTO cwl_league_groups (league_group_id, cwl_season, clan_tag) VALUES (?, ?, ?)",
        ("grp1", SEASON, "#CLAN1"),
    )
    await db.conn.commit()
    monkeypatch.setattr(CACHE, "send_user_dm_detailed", AsyncMock(return_value=(True, "sent")), raising=False)

    counters = await check_cwl_roster_switches()

    assert counters["entered_war"] == 1
    assert db.get_cwl_event_sync(guild_id, SEASON)["status"] == "war"
    # ...and monitoring must keep running for the clans that HAVEN'T started.
    assert db.get_active_cwl_events_sync()[0]["id"] == event_id


# ---------------------------------------------------------------------------
# Season deletion once war has started
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_locked_clan_tags_drive_the_delete_block(db):
    guild_id = "309"
    await _seed(db, guild_id)
    event_id = await _event(db, guild_id, [{"clan_tag": "#CLAN1"}, {"clan_tag": "#CLAN2"}])

    assert db.get_locked_cwl_clan_tags_sync(event_id) == set()
    db.mark_cwl_event_clan_locked_sync("#CLAN1", SEASON)
    assert db.get_locked_cwl_clan_tags_sync(event_id) == {"#CLAN1"}


@pytest.mark.discord
@pytest.mark.asyncio
async def test_delete_button_disabled_once_a_clan_started(db):
    import discord

    from qapbot.ui_cwl_roster import add_cwl_management_components

    guild_id = "310"
    await _seed(db, guild_id)
    event_id = await _event(db, guild_id, [{"clan_tag": "#CLAN1", "cwl_start_at": f"{SEASON}-01T08:00Z"}])

    view = discord.ui.View(timeout=300)
    add_cwl_management_components(view, int(guild_id))
    delete = next(c for c in view.children if getattr(c, "custom_id", None) == "cwl_management_delete_season")
    assert delete.disabled is False

    db.mark_cwl_event_clan_locked_sync("#CLAN1", SEASON)
    view2 = discord.ui.View(timeout=300)
    add_cwl_management_components(view2, int(guild_id))
    delete2 = next(c for c in view2.children if getattr(c, "custom_id", None) == "cwl_management_delete_season")
    assert delete2.disabled is True


# ---------------------------------------------------------------------------
# Pending roster updates (spec item 4)
# ---------------------------------------------------------------------------

async def _announced_player(db, event_id, guild_id, tag, clan_tag, name, discord_id="u1"):
    await _player(db, discord_id, tag, clan_tag, name)
    db.upsert_cwl_assignment_sync(event_id, tag, clan_tag, assignment_source="admin_override", locked=True)
    db.mark_cwl_assignment_notified_sync(event_id, tag, True, clan_tag)


@pytest.mark.asyncio
async def test_moving_an_announced_player_makes_an_update_pending(db):
    from qapbot.QBdiscocmdshelper_cwl import (
        assign_cwl_player_sync, resolve_cwl_pending_roster_updates_sync,
    )

    guild_id = "320"
    await _seed(db, guild_id)
    event_id = await _event(db, guild_id, [{"clan_tag": "#CLAN1"}, {"clan_tag": "#CLAN2"}])
    await _announced_player(db, event_id, guild_id, "#P1", "#CLAN1", "Mover")

    pending = resolve_cwl_pending_roster_updates_sync(int(guild_id), event_id, SEASON)
    assert pending == {"moved": [], "dropped": [], "new": []}, "nothing changed yet"

    assign_cwl_player_sync(int(guild_id), event_id, SEASON, "#P1", "#CLAN2", source="admin_override")

    pending = resolve_cwl_pending_roster_updates_sync(int(guild_id), event_id, SEASON)
    assert len(pending["moved"]) == 1
    assert pending["moved"][0]["notified_clan_tag"] == "#CLAN1"
    assert pending["moved"][0]["clan_tag"] == "#CLAN2"


@pytest.mark.asyncio
async def test_dragging_there_and_back_cancels_itself_out(db):
    """The avalanche protection, obtained structurally rather than by debouncing: "pending" is a
    comparison against what was last SENT, so A→B→A ends up owing nobody a DM."""
    from qapbot.QBdiscocmdshelper_cwl import (
        assign_cwl_player_sync, resolve_cwl_pending_roster_updates_sync,
    )

    guild_id = "321"
    await _seed(db, guild_id)
    event_id = await _event(db, guild_id, [{"clan_tag": "#CLAN1"}, {"clan_tag": "#CLAN2"}])
    await _announced_player(db, event_id, guild_id, "#P1", "#CLAN1", "Indecisive")

    assign_cwl_player_sync(int(guild_id), event_id, SEASON, "#P1", "#CLAN2", source="admin_override")
    assign_cwl_player_sync(int(guild_id), event_id, SEASON, "#P1", "#CLAN1", source="admin_override")

    pending = resolve_cwl_pending_roster_updates_sync(int(guild_id), event_id, SEASON)
    assert pending["moved"] == [] and pending["dropped"] == [] and pending["new"] == []


@pytest.mark.asyncio
async def test_unassigning_an_announced_player_leaves_a_tombstone(db):
    """Unassigning DELETES the assignment row, so without a tombstone the fact that this player is
    owed a "you're off the roster" DM would vanish with it."""
    from qapbot.QBdiscocmdshelper_cwl import (
        assign_cwl_player_sync, resolve_cwl_pending_roster_updates_sync,
    )

    guild_id = "322"
    await _seed(db, guild_id)
    event_id = await _event(db, guild_id, [{"clan_tag": "#CLAN1"}])
    await _announced_player(db, event_id, guild_id, "#P1", "#CLAN1", "Dropped")

    assign_cwl_player_sync(int(guild_id), event_id, SEASON, "#P1", None, source="admin_override")

    pending = resolve_cwl_pending_roster_updates_sync(int(guild_id), event_id, SEASON)
    assert len(pending["dropped"]) == 1
    assert pending["dropped"][0]["notified_clan_tag"] == "#CLAN1"

    # Putting them back clears it — a stale tombstone would send a contradictory removal notice.
    assign_cwl_player_sync(int(guild_id), event_id, SEASON, "#P1", "#CLAN1", source="admin_override")
    pending = resolve_cwl_pending_roster_updates_sync(int(guild_id), event_id, SEASON)
    assert pending["dropped"] == [] and pending["moved"] == []


@pytest.mark.asyncio
async def test_a_never_announced_player_counts_as_new_not_moved(db):
    from qapbot.QBdiscocmdshelper_cwl import resolve_cwl_pending_roster_updates_sync

    guild_id = "323"
    await _seed(db, guild_id)
    event_id = await _event(db, guild_id, [{"clan_tag": "#CLAN1"}])
    await _player(db, "u9", "#NEW", "#CLAN1", "Latecomer")
    db.upsert_cwl_assignment_sync(event_id, "#NEW", "#CLAN1", assignment_source="admin_override", locked=True)

    pending = resolve_cwl_pending_roster_updates_sync(int(guild_id), event_id, SEASON)
    assert len(pending["new"]) == 1 and pending["moved"] == []


@pytest.mark.asyncio
async def test_enrollment_payload_hides_pending_updates_before_first_announcement(db):
    """Regression (caught live, 2026-08-30): the Teams Management board showed "Send Roster
    Updates (9)" during Enrollment, before Announce Rosters had ever been pressed.
    resolve_cwl_pending_roster_updates_sync() correctly counts every never-announced assigned
    player as "new" — that is right for count_cwl_pending_roster_updates() and the Hub, which both
    only ever call it once status has left signup_open. _build_enrollment_payload_sync() had no
    such guard and called it unconditionally, so pre-announcement "new" placements (which
    Announce Rosters, not Send Roster Updates, is meant to handle) leaked into the board's own
    pending count. Mirrors the Hub's own branch (add_cwl_management_components, ui_cwl_roster.py:
    `if event["status"] == "signup_open": ... else: count_cwl_pending_roster_updates(...)`)."""
    from qapbot.QBdiscocmdshelper_cwl import assign_cwl_player_sync
    from qapbot.web_bridge import _build_enrollment_payload_sync

    guild_id = "324"
    await _seed(db, guild_id)
    event_id = await _event(db, guild_id, [{"clan_tag": "#CLAN1"}], status="signup_open")
    await _player(db, "u1", "#NEW", "#CLAN1", "Latecomer")
    db.upsert_cwl_assignment_sync(event_id, "#NEW", "#CLAN1", assignment_source="admin_override", locked=True)

    payload = _build_enrollment_payload_sync(int(guild_id))
    assert payload["event_status"] == "signup_open"
    assert payload["pending_roster_updates"] == 0, "not-yet-announced players are Announce Rosters' job"

    db.update_cwl_event_status_sync(event_id, "announced")
    db.mark_cwl_assignment_notified_sync(event_id, "#NEW", True, "#CLAN1")
    assign_cwl_player_sync(int(guild_id), event_id, SEASON, "#NEW", None, source="admin_override")

    payload = _build_enrollment_payload_sync(int(guild_id))
    assert payload["event_status"] == "announced"
    assert payload["pending_roster_updates"] == 1, "dropping an announced player is a real pending update"


@pytest.mark.asyncio
async def test_pending_roster_updates_stay_zero_during_enrollment(db):
    """Regression (live bug report, 2026-08-30): closing the Teams Management board during
    Enrollment produced a "11 line-up update(s) still unsent" DM — a message that only makes sense
    in Preparation/War. _dm_pending_roster_updates_notice (web_bridge.py) calls
    count_cwl_pending_roster_updates() directly with no phase branch of its own (unlike the Hub's
    button, which only calls it from an `else: status != signup_open` branch) — so the phase gate
    now lives inside count_cwl_pending_roster_updates() itself, the one function every caller
    shares, rather than being each caller's job to re-derive."""
    from qapbot.QBdiscocmdshelper_cwl import count_cwl_pending_roster_updates

    guild_id = "325"
    await _seed(db, guild_id)
    event_id = await _event(db, guild_id, [{"clan_tag": "#CLAN1"}], status="signup_open")
    await _player(db, "u1", "#NEW", "#CLAN1", "Latecomer")
    db.upsert_cwl_assignment_sync(event_id, "#NEW", "#CLAN1", assignment_source="admin_override", locked=True)

    assert count_cwl_pending_roster_updates(int(guild_id), SEASON) == 0

    db.update_cwl_event_status_sync(event_id, "announced")
    db.mark_cwl_assignment_notified_sync(event_id, "#NEW", True, "#CLAN1")
    assert count_cwl_pending_roster_updates(int(guild_id), SEASON) == 0, "just-announced, nothing changed since"


@pytest.mark.asyncio
async def test_send_roster_updates_dms_once_and_clears_pending(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import (
        assign_cwl_player_sync, count_cwl_pending_roster_updates, send_cwl_roster_updates,
    )

    guild_id = "324"
    await _seed(db, guild_id)
    event_id = await _event(db, guild_id, [{"clan_tag": "#CLAN1", "cwl_start_at": f"{SEASON}-01T08:00Z"},
                                           {"clan_tag": "#CLAN2", "cwl_start_at": f"{SEASON}-01T08:00Z"}])
    await _announced_player(db, event_id, guild_id, "#P1", "#CLAN1", "Mover")
    assign_cwl_player_sync(int(guild_id), event_id, SEASON, "#P1", "#CLAN2", source="admin_override")
    sent = AsyncMock(return_value=(True, "sent"))
    monkeypatch.setattr(CACHE, "send_user_dm_detailed", sent, raising=False)

    assert count_cwl_pending_roster_updates(int(guild_id), SEASON) == 1
    result = await send_cwl_roster_updates(int(guild_id), SEASON)

    assert result["ok"] and result["moved"] == 1 and result["contacted_users"] == 1
    body = sent.await_args.args[1]
    assert "Mover" in body
    # Draining it must actually clear the pending state, or the Hub button would never go away.
    assert count_cwl_pending_roster_updates(int(guild_id), SEASON) == 0


@pytest.mark.asyncio
async def test_one_user_with_two_changed_accounts_gets_one_dm(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import assign_cwl_player_sync, send_cwl_roster_updates

    guild_id = "325"
    await _seed(db, guild_id)
    event_id = await _event(db, guild_id, [{"clan_tag": "#CLAN1", "cwl_start_at": f"{SEASON}-01T08:00Z"},
                                           {"clan_tag": "#CLAN2", "cwl_start_at": f"{SEASON}-01T08:00Z"}])
    await _announced_player(db, event_id, guild_id, "#MAIN", "#CLAN1", "Main", "u1")
    await _announced_player(db, event_id, guild_id, "#ALT", "#CLAN1", "Alt", "u1")
    assign_cwl_player_sync(int(guild_id), event_id, SEASON, "#MAIN", "#CLAN2", source="admin_override")
    assign_cwl_player_sync(int(guild_id), event_id, SEASON, "#ALT", None, source="admin_override")
    sent = AsyncMock(return_value=(True, "sent"))
    monkeypatch.setattr(CACHE, "send_user_dm_detailed", sent, raising=False)

    result = await send_cwl_roster_updates(int(guild_id), SEASON)

    assert result["moved"] == 1 and result["dropped"] == 1
    assert result["contacted_users"] == 1
    assert sent.await_count == 1, "one person, one DM — never one per account"
    body = sent.await_args.args[1]
    assert "Main" in body and "Alt" in body


# ---------------------------------------------------------------------------
# Clan added / removed during Preparation (spec item 5)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_removing_a_clan_makes_its_announced_players_pending(db):
    """Removing a clan strands anyone already told they'd play for it. Their assignment row
    survives (it just stops being rendered), so the ordinary moved/dropped comparison would never
    notice — the tombstone is what routes them into the same update batch as every other change."""
    from qapbot.QBdiscocmdshelper_cwl import resolve_cwl_pending_roster_updates_sync
    from qapbot.web_bridge import _tombstone_announced_players_of_removed_clans_sync

    guild_id = "340"
    await _seed(db, guild_id)
    event_id = await _event(db, guild_id, [{"clan_tag": "#CLAN1"}, {"clan_tag": "#CLAN2"}])
    await _announced_player(db, event_id, guild_id, "#TOLD", "#CLAN1", "Told", "u1")
    # Never announced: nothing to correct, so telling them they've been removed from a roster they
    # never knew they were on would be pure noise.
    await _player(db, "u2", "#QUIET", "#CLAN1", "Quiet")
    db.upsert_cwl_assignment_sync(event_id, "#QUIET", "#CLAN1", assignment_source="admin_override", locked=True)

    recorded = _tombstone_announced_players_of_removed_clans_sync(event_id, ["#CLAN1"])

    assert recorded == 1
    pending = resolve_cwl_pending_roster_updates_sync(int(guild_id), event_id, SEASON)
    assert [p["player_tag"] for p in pending["dropped"]] == ["#TOLD"]


@pytest.mark.asyncio
async def test_removing_an_unrelated_clan_touches_nobody(db):
    from qapbot.web_bridge import _tombstone_announced_players_of_removed_clans_sync

    guild_id = "341"
    await _seed(db, guild_id)
    event_id = await _event(db, guild_id, [{"clan_tag": "#CLAN1"}, {"clan_tag": "#CLAN2"}])
    await _announced_player(db, event_id, guild_id, "#P1", "#CLAN1", "Stays", "u1")

    assert _tombstone_announced_players_of_removed_clans_sync(event_id, ["#CLAN2"]) == 0
    assert db.get_cwl_dropped_notified_players_sync(event_id) == []


@pytest.mark.asyncio
async def test_late_added_never_contacted_player_gets_one_combined_dm(db, monkeypatch):
    """Spec item 5's edge case: a player whose clan joined the season after Start Enrollment ran
    has TWO unanswered questions — "do you want to play?" and "here's where you play". They arrive
    as one message with the confirm/opt-out buttons attached, not two separate DMs."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import send_cwl_roster_updates

    guild_id = "342"
    await _seed(db, guild_id)
    event_id = await _event(db, guild_id, [{"clan_tag": "#CLAN1", "cwl_start_at": f"{SEASON}-01T08:00Z"}])
    # Still sitting in another clan — the realistic late-add case, and the one that must carry a
    # join link (a player already in their assigned clan gets the green variant, which has none).
    await _player(db, "u1", "#LATE", "#CLAN2", "Latecomer")
    db.upsert_cwl_assignment_sync(event_id, "#LATE", "#CLAN1", assignment_source="admin_override", locked=True)
    sent = AsyncMock(return_value=(True, "sent"))
    monkeypatch.setattr(CACHE, "send_user_dm_detailed", sent, raising=False)

    result = await send_cwl_roster_updates(int(guild_id), SEASON)

    assert result["new"] == 1
    assert sent.await_count == 1, "one message, not an enrollment DM plus an assignment DM"
    assert sent.await_args.kwargs.get("view") is not None, "confirm/opt-out buttons must be attached"
    body = sent.await_args.args[1]
    assert "Latecomer" in body, "the roster half of the combined message"
    assert "link.clashofclans.com" in body, "and its join link, since they aren't in the clan yet"
    # The combined DM answers the enrollment question, so "Notify New Pool Members" must not then
    # send them a second, redundant invitation.
    assert db.get_cwl_player_season_dm_status_bulk_sync(["#LATE"], SEASON).get("#LATE") is True


@pytest.mark.asyncio
async def test_already_enrolled_player_gets_no_confirm_buttons(db, monkeypatch):
    """Someone who already answered the enrollment question this season just needs the roster
    information — re-asking would be the redundant second DM this design exists to avoid."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import send_cwl_roster_updates

    guild_id = "343"
    await _seed(db, guild_id)
    event_id = await _event(db, guild_id, [{"clan_tag": "#CLAN1", "cwl_start_at": f"{SEASON}-01T08:00Z"}])
    await _player(db, "u1", "#ASKED", "#CLAN1", "Asked")
    db.upsert_cwl_assignment_sync(event_id, "#ASKED", "#CLAN1", assignment_source="admin_override", locked=True)
    db.mark_cwl_player_dm_sent_sync("#ASKED", SEASON, "Asked", "u1", event_id, int(guild_id), "2026-09-01T08:00Z")
    sent = AsyncMock(return_value=(True, "sent"))
    monkeypatch.setattr(CACHE, "send_user_dm_detailed", sent, raising=False)

    await send_cwl_roster_updates(int(guild_id), SEASON)

    assert sent.await_count == 1
    assert sent.await_args.kwargs.get("view") is None


# ---------------------------------------------------------------------------
# Roster completeness check (spec item 6)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_underfilled_clans_are_reported_and_full_ones_are_not(db):
    from qapbot.QBdiscocmdshelper_cwl import resolve_cwl_underfilled_clans_sync

    guild_id = "330"
    await _seed(db, guild_id)
    event_id = await _event(db, guild_id, [
        {"clan_tag": "#CLAN1", "roster_size": 2},
        {"clan_tag": "#CLAN2", "roster_size": 1},
    ])
    await _player(db, "u1", "#A", "#CLAN1")
    await _player(db, "u2", "#B", "#CLAN2")
    await _player(db, "u3", "#C", "#CLAN2")
    db.upsert_cwl_assignment_sync(event_id, "#A", "#CLAN1", assignment_source="admin_override", locked=True)
    db.upsert_cwl_assignment_sync(event_id, "#B", "#CLAN2", assignment_source="admin_override", locked=True)
    db.upsert_cwl_assignment_sync(event_id, "#C", "#CLAN2", assignment_source="admin_override", locked=True)

    short = resolve_cwl_underfilled_clans_sync(int(guild_id), event_id, SEASON)

    # CLAN1 has 1 of 2; CLAN2 is over-filled (2 of 1) which is a deliberate reserve, not a problem.
    assert [s["clan_tag"] for s in short] == ["#CLAN1"]
    assert short[0]["assigned"] == 1 and short[0]["roster_size"] == 2


# ---------------------------------------------------------------------------
# Coordinator completeness check (tracker #0084)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clans_missing_coordinator_are_reported_and_covered_ones_are_not(db):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_cwl_clans_missing_coordinator_sync

    guild_id = "331"
    await _seed(db, guild_id)
    event_id = await _event(db, guild_id, [
        {"clan_tag": "#CLAN1"},
        {"clan_tag": "#CLAN2"},
    ])
    CACHE.server_config[guild_id]["cwl_clan_coordinators"] = {"#CLAN1": ["coord-1"], "#CLAN2": []}

    missing = resolve_cwl_clans_missing_coordinator_sync(int(guild_id), event_id)

    assert [c["clan_tag"] for c in missing] == ["#CLAN2"]


@pytest.mark.asyncio
async def test_no_missing_coordinator_when_all_participating_clans_covered(db):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_cwl_clans_missing_coordinator_sync

    guild_id = "332"
    await _seed(db, guild_id)
    event_id = await _event(db, guild_id, [{"clan_tag": "#CLAN1"}])
    CACHE.server_config[guild_id]["cwl_clan_coordinators"] = {"#CLAN1": ["coord-1"]}

    missing = resolve_cwl_clans_missing_coordinator_sync(int(guild_id), event_id)

    assert missing == []


# ---------------------------------------------------------------------------
# Coordinator board access
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_coordinator_of_a_participating_clan_gets_board_access(db):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import is_cwl_coordinator_for_current_season

    guild_id = "311"
    await _seed(db, guild_id)
    await _event(db, guild_id, [{"clan_tag": "#CLAN1"}, {"clan_tag": "#CLAN2"}])
    CACHE.server_config[guild_id]["cwl_clan_coordinators"] = {"#CLAN1": ["coord-1"]}

    assert is_cwl_coordinator_for_current_season(int(guild_id), "coord-1") is True
    assert is_cwl_coordinator_for_current_season(int(guild_id), "nobody") is False


@pytest.mark.asyncio
async def test_coordinator_of_a_sitting_out_clan_gets_no_access(db):
    """Coordinator config is standing and carries forward every month, so it has to be scoped to
    clans actually participating this season ("for this season", project owner's spec)."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import is_cwl_coordinator_for_current_season

    guild_id = "312"
    await _seed(db, guild_id)
    await _event(db, guild_id, [{"clan_tag": "#CLAN1", "participating": True},
                                {"clan_tag": "#CLAN2", "participating": False}])
    CACHE.server_config[guild_id]["cwl_clan_coordinators"] = {"#CLAN2": ["coord-2"]}

    assert is_cwl_coordinator_for_current_season(int(guild_id), "coord-2") is False
