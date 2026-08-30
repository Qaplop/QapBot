"""Tests for the four-phase CWL model, war-phase transition and roster freeze —
plans/cwl-phase-model-and-war-phase.md (2026-08-30, project owner's spec).

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
