"""Tests for "Start CWL" (Phase 5) and switch verification/alarms (Phase 6) —
CWL_ROSTER_PLANNING_PLAN.md.

The single fact both phases hang off is "is this player already sitting in the clan they were
assigned to?" — the same green/amber split the Manage Teams board renders. These tests pin that
down in both directions, plus the three exclusions that stop a player being announced twice
(non-participating clan, non-owned shared clan, already notified).
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
    """Point CACHE at the temp DB and give every test a clean server_config/user_accounts."""
    from qapbot.cache_manager import CACHE

    monkeypatch.setattr(CACHE, "db_manager", db, raising=False)
    monkeypatch.setattr(CACHE, "server_config", {}, raising=False)
    monkeypatch.setattr(CACHE, "user_accounts", {}, raising=False)
    monkeypatch.setattr(CACHE, "testers", set(), raising=False)
    return CACHE


@pytest.fixture(autouse=True)
def _dm_guard_off(monkeypatch):
    """CONFIG.cwl_dm_restrict_to_admin defaults to True on this dev machine, which would skip every
    recipient. Rebinding the module-level CONFIG works because _dm_guard_blocks imports it locally
    (same technique as test_cwl_start_enrollment.py's own guard tests)."""
    import qapbot.config as config_module

    monkeypatch.setattr(
        config_module, "CONFIG",
        dataclasses.replace(config_module.CONFIG, cwl_dm_restrict_to_admin=False),
    )


async def _seed_guild_and_clans(db: WarHistoryDB, guild_id: str, clan_tags=("#CLAN1",)) -> None:
    from qapbot.cache_manager import CACHE

    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
    for tag in clan_tags:
        await db.conn.execute(
            "INSERT OR IGNORE INTO clans (clan_tag, name) VALUES (?, ?)", (tag, f"Clan {tag[-1]}")
        )
    await db.conn.commit()
    CACHE.server_config[guild_id] = {"member_clans": list(clan_tags), "member_families": []}


async def _seed_player(
    db: WarHistoryDB, discord_id: str | None, player_tag: str, current_clan_tag: str,
    player_name: str = "Player",
) -> None:
    """A linked account (discord_id set) or an unlinked one in the UNASSIGNED pool."""
    owner = discord_id or "UNASSIGNED"
    await db.conn.execute(
        "INSERT OR IGNORE INTO users (discord_id, display_name) VALUES (?, ?)", (owner, owner)
    )
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag) "
        "VALUES (?, ?, ?, 1, ?)",
        (owner, player_tag, player_name, current_clan_tag),
    )
    await db.conn.commit()


async def _make_announced_event(
    db: WarHistoryDB, guild_id: str, clan_configs, status: str = "signup_open",
) -> int:
    event_id = db.create_cwl_event_sync(guild_id, SEASON, "creator")
    db.set_cwl_event_clans_sync(event_id, clan_configs)
    db.update_cwl_event_status_sync(event_id, status)
    return event_id


def _assign(db: WarHistoryDB, event_id: int, player_tag: str, clan_tag: str) -> None:
    db.upsert_cwl_assignment_sync(
        event_id, player_tag, clan_tag, assignment_source="admin_override", locked=True,
    )


# ---------------------------------------------------------------------------
# Phase 5 — target resolution (the green/amber split)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_targets_split_green_and_amber(db):
    """A player already in their assigned clan is green; one still elsewhere is amber."""
    from qapbot.QBdiscocmdshelper_cwl import resolve_cwl_announcement_targets_sync

    guild_id = "100"
    await _seed_guild_and_clans(db, guild_id, ("#CLAN1", "#CLAN2"))
    event_id = await _make_announced_event(
        db, guild_id,
        [{"clan_tag": "#CLAN1", "cwl_start_at": f"{SEASON}-01T08:00Z"},
         {"clan_tag": "#CLAN2", "cwl_start_at": f"{SEASON}-02T08:00Z"}],
    )
    await _seed_player(db, "u1", "#GREEN", "#CLAN1", "GreenGuy")
    await _seed_player(db, "u2", "#AMBER", "#CLAN2", "AmberGuy")
    _assign(db, event_id, "#GREEN", "#CLAN1")   # already there
    _assign(db, event_id, "#AMBER", "#CLAN1")   # assigned to CLAN1, still sitting in CLAN2

    targets = resolve_cwl_announcement_targets_sync(int(guild_id), event_id, SEASON)

    assert targets["total_assigned"] == 2
    green = targets["groups"]["u1"][0]
    amber = targets["groups"]["u2"][0]
    assert green["in_clan"] is True
    assert green["clan_tag"] == "#CLAN1"
    assert amber["in_clan"] is False
    assert amber["current_clan_tag"] == "#CLAN2"
    assert amber["clan_tag"] == "#CLAN1"


@pytest.mark.asyncio
async def test_start_targets_unknown_current_clan_is_amber(db):
    """No current clan on record must render AMBER, never green — green would assert something we
    cannot back up, while amber's "be in X before Y" stays true wherever they are."""
    from qapbot.QBdiscocmdshelper_cwl import resolve_cwl_announcement_targets_sync

    guild_id = "101"
    await _seed_guild_and_clans(db, guild_id)
    event_id = await _make_announced_event(
        db, guild_id, [{"clan_tag": "#CLAN1", "cwl_start_at": f"{SEASON}-01T08:00Z"}]
    )
    await _seed_player(db, "u1", "#GHOST", None, "Ghost")  # linked, but in no clan on record
    _assign(db, event_id, "#GHOST", "#CLAN1")

    targets = resolve_cwl_announcement_targets_sync(int(guild_id), event_id, SEASON)

    account = targets["groups"]["u1"][0]
    assert account["in_clan"] is False
    assert account["current_clan_tag"] is None


@pytest.mark.asyncio
async def test_start_targets_group_one_users_accounts_together(db):
    """A main and an alt in different clans belong in ONE DM, each carrying its own start time."""
    from qapbot.QBdiscocmdshelper_cwl import resolve_cwl_announcement_targets_sync

    guild_id = "102"
    await _seed_guild_and_clans(db, guild_id, ("#CLAN1", "#CLAN2"))
    event_id = await _make_announced_event(
        db, guild_id,
        [{"clan_tag": "#CLAN1", "cwl_start_at": f"{SEASON}-01T08:00Z"},
         {"clan_tag": "#CLAN2", "cwl_start_at": f"{SEASON}-03T20:00Z"}],
    )
    await _seed_player(db, "u1", "#MAIN", "#CLAN1", "Main")
    await _seed_player(db, "u1", "#ALT", "#CLAN1", "Alt")
    _assign(db, event_id, "#MAIN", "#CLAN1")
    _assign(db, event_id, "#ALT", "#CLAN2")

    targets = resolve_cwl_announcement_targets_sync(int(guild_id), event_id, SEASON)

    assert list(targets["groups"].keys()) == ["u1"]
    by_tag = {a["player_tag"]: a for a in targets["groups"]["u1"]}
    assert by_tag["#MAIN"]["cwl_start_at"] == f"{SEASON}-01T08:00Z"
    assert by_tag["#ALT"]["cwl_start_at"] == f"{SEASON}-03T20:00Z"
    assert by_tag["#MAIN"]["in_clan"] is True    # already in CLAN1
    assert by_tag["#ALT"]["in_clan"] is False    # assigned CLAN2, sitting in CLAN1


@pytest.mark.asyncio
async def test_start_targets_exclude_non_participating_and_notified(db):
    """An assignment pointing at a clan that isn't a column here ("Assigned to other Guild") is
    another guild's business; an already-notified one must not be announced twice."""
    from qapbot.QBdiscocmdshelper_cwl import resolve_cwl_announcement_targets_sync

    guild_id = "103"
    await _seed_guild_and_clans(db, guild_id, ("#CLAN1", "#OTHER"))
    event_id = await _make_announced_event(
        db, guild_id,
        [{"clan_tag": "#CLAN1", "cwl_start_at": f"{SEASON}-01T08:00Z"},
         {"clan_tag": "#OTHER", "participating": False}],
    )
    await _seed_player(db, "u1", "#DONE", "#CLAN1", "AlreadyTold")
    await _seed_player(db, "u2", "#ELSEWHERE", "#CLAN1", "OtherGuild")
    _assign(db, event_id, "#DONE", "#CLAN1")
    _assign(db, event_id, "#ELSEWHERE", "#OTHER")
    db.mark_cwl_assignment_notified_sync(event_id, "#DONE")

    targets = resolve_cwl_announcement_targets_sync(int(guild_id), event_id, SEASON)

    assert targets["groups"] == {}
    assert targets["already_notified"] == 1


@pytest.mark.asyncio
async def test_start_targets_unlinked_player_is_named_not_dmed(db):
    """An unlinked account can't be reached at all — it must be counted AND named, since that's
    exactly who a lead has to chase by hand."""
    from qapbot.QBdiscocmdshelper_cwl import resolve_cwl_announcement_targets_sync

    guild_id = "104"
    await _seed_guild_and_clans(db, guild_id)
    event_id = await _make_announced_event(
        db, guild_id, [{"clan_tag": "#CLAN1", "cwl_start_at": f"{SEASON}-01T08:00Z"}]
    )
    await _seed_player(db, None, "#NOLINK", "#CLAN1", "Unlinked")
    _assign(db, event_id, "#NOLINK", "#CLAN1")

    targets = resolve_cwl_announcement_targets_sync(int(guild_id), event_id, SEASON)

    assert targets["groups"] == {}
    assert targets["skipped_unlinked"] == 1
    assert targets["unlinked_names"] == ["Unlinked"]


@pytest.mark.asyncio
async def test_start_targets_missing_start_time_is_reported(db):
    from qapbot.QBdiscocmdshelper_cwl import resolve_cwl_announcement_targets_sync

    guild_id = "105"
    await _seed_guild_and_clans(db, guild_id, ("#CLAN1", "#CLAN2"))
    event_id = await _make_announced_event(
        db, guild_id,
        [{"clan_tag": "#CLAN1", "cwl_start_at": f"{SEASON}-01T08:00Z"}, {"clan_tag": "#CLAN2"}],
    )

    targets = resolve_cwl_announcement_targets_sync(int(guild_id), event_id, SEASON)

    assert targets["missing_start_times"] == ["#CLAN2"]


# ---------------------------------------------------------------------------
# Phase 5 — announce_cwl_rosters() end to end
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_cwl_refuses_when_a_clan_has_no_start_time(db):
    """The surviving half of the original Phase 4 Finalize gate: no partial send, and the caller
    gets the offending clans back so it can name them."""
    from qapbot.QBdiscocmdshelper_cwl import announce_cwl_rosters

    guild_id = "110"
    await _seed_guild_and_clans(db, guild_id, ("#CLAN1", "#CLAN2"))
    event_id = await _make_announced_event(
        db, guild_id,
        [{"clan_tag": "#CLAN1", "cwl_start_at": f"{SEASON}-01T08:00Z"}, {"clan_tag": "#CLAN2"}],
    )
    await _seed_player(db, "u1", "#P1", "#CLAN1")
    _assign(db, event_id, "#P1", "#CLAN1")

    result = await announce_cwl_rosters(int(guild_id), SEASON)

    assert result["ok"] is False
    assert result["error"] == "missing_start_times"
    assert "#CLAN2" in result["missing_start_times"]
    # Nothing was announced and the event did NOT advance.
    assert db.get_cwl_event_sync(guild_id, SEASON)["status"] == "signup_open"


@pytest.mark.asyncio
async def test_start_cwl_sends_marks_notified_and_announces(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import announce_cwl_rosters

    guild_id = "111"
    await _seed_guild_and_clans(db, guild_id)
    event_id = await _make_announced_event(
        db, guild_id, [{"clan_tag": "#CLAN1", "cwl_start_at": f"{SEASON}-01T08:00Z"}]
    )
    await _seed_player(db, "u1", "#P1", "#CLAN1", "Green")
    _assign(db, event_id, "#P1", "#CLAN1")
    sent = AsyncMock(return_value=(True, "sent"))
    monkeypatch.setattr(CACHE, "send_user_dm_detailed", sent, raising=False)

    result = await announce_cwl_rosters(int(guild_id), SEASON)

    assert result["ok"] is True
    assert result["contacted"] == 1
    assert result["contacted_users"] == 1
    assert sent.await_count == 1
    body = sent.await_args.args[1]
    assert "Green" in body
    assert "<t:" in body  # per-viewer timestamp markup, not a server-rendered date
    # notified is set, so a re-run has nobody left and the button gate goes false.
    assert db.get_cwl_assignments_sync(event_id)[0]["notified"] == 1
    assert db.get_cwl_event_sync(guild_id, SEASON)["status"] == "announced"

    rerun = await announce_cwl_rosters(int(guild_id), SEASON)
    assert rerun["ok"] is False
    assert rerun["error"] == "nobody_to_notify"
    assert sent.await_count == 1  # no second DM


@pytest.mark.asyncio
async def test_start_cwl_amber_dm_carries_the_clan_join_link(db, monkeypatch):
    """The amber variant's whole point (project owner's spec): a direct link to the assigned clan."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import announce_cwl_rosters

    guild_id = "112"
    await _seed_guild_and_clans(db, guild_id, ("#CLAN1", "#CLAN2"))
    event_id = await _make_announced_event(
        db, guild_id,
        [{"clan_tag": "#CLAN1", "cwl_start_at": f"{SEASON}-01T08:00Z"}, {"clan_tag": "#CLAN2", "cwl_start_at": f"{SEASON}-01T08:00Z"}],
    )
    await _seed_player(db, "u1", "#P1", "#CLAN2", "Mover")
    _assign(db, event_id, "#P1", "#CLAN1")
    sent = AsyncMock(return_value=(True, "sent"))
    monkeypatch.setattr(CACHE, "send_user_dm_detailed", sent, raising=False)

    await announce_cwl_rosters(int(guild_id), SEASON)

    body = sent.await_args.args[1]
    assert "link.clashofclans.com" in body
    assert "%23CLAN1" in body  # the ASSIGNED clan, never the current one
    # Tracker #0076, live bug report: a bare URL in plain DM content makes Discord auto-unfurl
    # it into a big link-preview card — angle brackets (<...>) suppress that.
    assert "<https://link.clashofclans.com" in body
    assert "%23CLAN1>" in body


@pytest.mark.asyncio
async def test_start_cwl_respects_the_dm_guard(db, monkeypatch):
    """The guard that keeps DEV testing from blasting real members must apply here like everywhere
    else — and a guarded recipient must NOT be marked notified, or they'd never be reachable."""
    import qapbot.config as config_module
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import announce_cwl_rosters

    guild_id = "113"
    await _seed_guild_and_clans(db, guild_id)
    event_id = await _make_announced_event(
        db, guild_id, [{"clan_tag": "#CLAN1", "cwl_start_at": f"{SEASON}-01T08:00Z"}]
    )
    await _seed_player(db, "not-the-admin", "#P1", "#CLAN1")
    _assign(db, event_id, "#P1", "#CLAN1")
    monkeypatch.setattr(
        config_module, "CONFIG",
        dataclasses.replace(
            config_module.CONFIG, cwl_dm_restrict_to_admin=True, server_admin="the-admin",
        ),
    )
    sent = AsyncMock(return_value=(True, "sent"))
    monkeypatch.setattr(CACHE, "send_user_dm_detailed", sent, raising=False)

    result = await announce_cwl_rosters(int(guild_id), SEASON)

    assert result["skipped_dm_guard"] == 1
    assert sent.await_count == 0
    assert db.get_cwl_assignments_sync(event_id)[0]["notified"] == 0


# ---------------------------------------------------------------------------
# Phase 6 — escalation-stage arithmetic
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "hours_left,already_sent,expected",
    [
        (48.0, 0, None),   # too early for anything
        (23.0, 0, 1),      # inside the 24h window
        (23.0, 1, None),   # already nudged, never re-sent
        (1.0, 1, 2),       # escalates to urgent
        (1.0, 2, None),    # urgent already sent
        (1.0, 0, 2),       # bot was down across 24h -> straight to urgent, never a stale nudge
        (-3.0, 0, 2),      # already started and still not there: most urgent case, not the least
        (None, 0, None),   # no start time -> never guess a schedule
    ],
)
def test_due_alarm_stage(hours_left, already_sent, expected):
    from qapbot.QBdiscocmdshelper_cwl import _due_cwl_alarm_stage

    assert _due_cwl_alarm_stage(hours_left, already_sent) == expected


# ---------------------------------------------------------------------------
# Phase 6 — the sweep
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_switch_sweep_records_a_player_who_moved(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import check_cwl_roster_switches

    guild_id = "120"
    await _seed_guild_and_clans(db, guild_id, ("#CLAN1", "#CLAN2"))
    event_id = await _make_announced_event(
        db, guild_id,
        [{"clan_tag": "#CLAN1", "cwl_start_at": f"{SEASON}-28T08:00Z"}],
        status="announced",
    )
    await _seed_player(db, "u1", "#P1", "#CLAN2")     # not there yet
    _assign(db, event_id, "#P1", "#CLAN1")
    db.mark_cwl_assignment_notified_sync(event_id, "#P1")
    monkeypatch.setattr(CACHE, "send_user_dm_detailed", AsyncMock(return_value=(True, "sent")), raising=False)

    # Still elsewhere -> not switched.
    await check_cwl_roster_switches()
    assert db.get_cwl_assignments_sync(event_id)[0]["switched_at"] is None

    # They transfer; the next sweep records it.
    await db.conn.execute(
        "UPDATE user_players SET current_clan_tag = ? WHERE player_tag = ?", ("#CLAN1", "#P1")
    )
    await db.conn.commit()
    counters = await check_cwl_roster_switches()

    assert counters["switched"] == 1
    assert db.get_cwl_assignments_sync(event_id)[0]["switched_at"] is not None


@pytest.mark.asyncio
async def test_switch_sweep_escalates_once_per_stage(db, monkeypatch):
    """An every-cycle sweep must not re-nag inside one threshold window."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import check_cwl_roster_switches

    guild_id = "121"
    await _seed_guild_and_clans(db, guild_id, ("#CLAN1", "#CLAN2"))
    # A start time already in the past: due for the urgent stage immediately.
    event_id = await _make_announced_event(
        db, guild_id, [{"clan_tag": "#CLAN1", "cwl_start_at": "2020-01-01T08:00Z"}], status="announced",
    )
    await _seed_player(db, "u1", "#P1", "#CLAN2")
    _assign(db, event_id, "#P1", "#CLAN1")
    db.mark_cwl_assignment_notified_sync(event_id, "#P1")
    sent = AsyncMock(return_value=(True, "sent"))
    monkeypatch.setattr(CACHE, "send_user_dm_detailed", sent, raising=False)

    first = await check_cwl_roster_switches()
    assert first["alarms"] == 1
    assert db.get_cwl_assignments_sync(event_id)[0]["alarm_stage_sent"] == 2

    second = await check_cwl_roster_switches()
    assert second["alarms"] == 0
    assert sent.await_count == 1


@pytest.mark.asyncio
async def test_switch_sweep_stops_once_the_clan_roster_locks(db, monkeypatch):
    """Both lock triggers, and the fact that a locked clan goes quiet: nagging someone about a
    roster they can no longer join is actively wrong, not merely wasteful."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import check_cwl_roster_switches

    guild_id = "122"
    await _seed_guild_and_clans(db, guild_id, ("#CLAN1", "#CLAN2"))
    event_id = await _make_announced_event(
        db, guild_id, [{"clan_tag": "#CLAN1", "cwl_start_at": "2020-01-01T08:00Z"}], status="announced",
    )
    await _seed_player(db, "u1", "#P1", "#CLAN2")
    _assign(db, event_id, "#P1", "#CLAN1")
    db.mark_cwl_assignment_notified_sync(event_id, "#P1")
    sent = AsyncMock(return_value=(True, "sent"))
    monkeypatch.setattr(CACHE, "send_user_dm_detailed", sent, raising=False)

    # Primary trigger: a real is_cwl war row for this clan and season.
    await db.conn.execute(
        "INSERT INTO war_summary (war_id, clan_tag, opponent_tag, is_cwl, cwl_season, date) "
        "VALUES (?, ?, ?, 1, ?, ?)",
        ("w1", "#CLAN1", "#OPP", SEASON, f"{SEASON}-01T08:00"),
    )
    await db.conn.commit()

    counters = await check_cwl_roster_switches()

    assert counters["locked"] == 1
    assert counters["alarms"] == 0          # locked before any alarm could fire
    assert sent.await_count == 0
    clans = db.get_cwl_event_clans_sync(event_id)
    assert clans[0]["locked_at"] is not None


@pytest.mark.asyncio
async def test_lock_fallback_trigger_is_the_league_group_row(db):
    """A private-warlog clan never yields an is_cwl war row — the cwl_league_groups row is what
    covers it."""
    from qapbot.QBdiscocmdshelper_cwl import check_cwl_roster_switches

    guild_id = "123"
    await _seed_guild_and_clans(db, guild_id)
    event_id = await _make_announced_event(
        db, guild_id, [{"clan_tag": "#CLAN1", "cwl_start_at": f"{SEASON}-01T08:00Z"}], status="announced",
    )
    await db.conn.execute(
        "INSERT INTO cwl_league_groups (league_group_id, cwl_season, clan_tag) VALUES (?, ?, ?)",
        ("grp1", SEASON, "#CLAN1"),
    )
    await db.conn.commit()

    counters = await check_cwl_roster_switches()

    assert counters["locked"] == 1
    assert db.get_cwl_event_clans_sync(event_id)[0]["locked_at"] is not None


@pytest.mark.asyncio
async def test_lock_is_write_once_across_repeated_sweeps(db):
    from qapbot.QBdiscocmdshelper_cwl import check_cwl_roster_switches

    guild_id = "124"
    await _seed_guild_and_clans(db, guild_id)
    event_id = await _make_announced_event(
        db, guild_id, [{"clan_tag": "#CLAN1", "cwl_start_at": f"{SEASON}-01T08:00Z"}], status="announced",
    )
    await db.conn.execute(
        "INSERT INTO cwl_league_groups (league_group_id, cwl_season, clan_tag) VALUES (?, ?, ?)",
        ("grp1", SEASON, "#CLAN1"),
    )
    await db.conn.commit()

    first = await check_cwl_roster_switches()
    locked_at = db.get_cwl_event_clans_sync(event_id)[0]["locked_at"]
    second = await check_cwl_roster_switches()

    assert first["locked"] == 1
    assert second["locked"] == 0  # re-observation is not a state change
    assert db.get_cwl_event_clans_sync(event_id)[0]["locked_at"] == locked_at


@pytest.mark.asyncio
async def test_sweep_ignores_events_that_never_started_cwl(db, monkeypatch):
    """Only 'announced' events are monitored — a roster still being edited must not fire alarms."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import check_cwl_roster_switches

    guild_id = "125"
    await _seed_guild_and_clans(db, guild_id, ("#CLAN1", "#CLAN2"))
    event_id = await _make_announced_event(
        db, guild_id, [{"clan_tag": "#CLAN1", "cwl_start_at": "2020-01-01T08:00Z"}],
        status="signup_open",
    )
    await _seed_player(db, "u1", "#P1", "#CLAN2")
    _assign(db, event_id, "#P1", "#CLAN1")
    db.mark_cwl_assignment_notified_sync(event_id, "#P1")
    sent = AsyncMock(return_value=(True, "sent"))
    monkeypatch.setattr(CACHE, "send_user_dm_detailed", sent, raising=False)

    counters = await check_cwl_roster_switches()

    assert counters["events"] == 0
    assert sent.await_count == 0


@pytest.mark.asyncio
async def test_stage2_escalates_to_the_clans_cwl_coordinators(db, monkeypatch):
    """Tracker #0046's standing per-clan coordinators are the leadership escalation target."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import check_cwl_roster_switches

    guild_id = "126"
    await _seed_guild_and_clans(db, guild_id, ("#CLAN1", "#CLAN2"))
    CACHE.server_config[guild_id]["cwl_clan_coordinators"] = {"#CLAN1": ["coord-1"]}
    event_id = await _make_announced_event(
        db, guild_id, [{"clan_tag": "#CLAN1", "cwl_start_at": "2020-01-01T08:00Z"}], status="announced",
    )
    await _seed_player(db, "u1", "#P1", "#CLAN2", "Missing Guy")
    _assign(db, event_id, "#P1", "#CLAN1")
    db.mark_cwl_assignment_notified_sync(event_id, "#P1")
    sent = AsyncMock(return_value=(True, "sent"))
    monkeypatch.setattr(CACHE, "send_user_dm_detailed", sent, raising=False)

    counters = await check_cwl_roster_switches()

    assert counters["coordinator_dms"] == 1
    recipients = [call.args[0] for call in sent.await_args_list]
    assert "coord-1" in recipients
    coordinator_body = [c.args[1] for c in sent.await_args_list if c.args[0] == "coord-1"][0]
    assert "Missing Guy" in coordinator_body


@pytest.mark.asyncio
async def test_still_missing_section_lists_players_and_locked_clans(db, monkeypatch):
    """The leadership half of Phase 6, rendered by the SHARED content layer so /clan management and
    the CWL Management Hub can't diverge."""
    from unittest.mock import MagicMock

    from qapbot.QBdiscocmdshelper_cwl import (
        check_cwl_roster_switches, format_clan_management_cwl_management,
    )

    guild_id = "128"
    await _seed_guild_and_clans(db, guild_id, ("#CLAN1", "#CLAN2"))
    event_id = await _make_announced_event(
        db, guild_id,
        [{"clan_tag": "#CLAN1", "cwl_start_at": f"{SEASON}-28T08:00Z"}],
        status="announced",
    )
    await db.conn.execute(
        "UPDATE guild_config SET cwl_selected_season = ? WHERE guild_id = ?", (SEASON, guild_id)
    )
    await db.conn.commit()
    from qapbot.cache_manager import CACHE
    CACHE.server_config[guild_id]["cwl_selected_season"] = SEASON
    await _seed_player(db, "u1", "#P1", "#CLAN2", "Straggler")
    _assign(db, event_id, "#P1", "#CLAN1")
    db.mark_cwl_assignment_notified_sync(event_id, "#P1")

    guild = MagicMock()
    guild.id = int(guild_id)
    embed, _, _, _ = await format_clan_management_cwl_management(guild)
    missing = [f for f in embed.fields if "Still Missing" in (f.name or "")]
    assert missing and "Straggler" in missing[0].value

    # Once the clan's roster locks, the section stops showing a stale missing-count.
    await db.conn.execute(
        "INSERT INTO cwl_league_groups (league_group_id, cwl_season, clan_tag) VALUES (?, ?, ?)",
        ("grp1", SEASON, "#CLAN1"),
    )
    await db.conn.commit()
    await check_cwl_roster_switches()

    embed2, _, _, _ = await format_clan_management_cwl_management(guild)
    missing2 = [f for f in embed2.fields if "Still Missing" in (f.name or "")]
    assert missing2 and "roster locked" in missing2[0].value
    assert "Straggler" not in missing2[0].value


@pytest.mark.asyncio
async def test_unreachable_player_stage_is_still_bumped(db, monkeypatch):
    """An unlinked player can never be DMed — bumping their stage anyway is what stops the sweep
    retrying them every cycle forever. The coordinator escalation covers them instead."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import check_cwl_roster_switches

    guild_id = "127"
    await _seed_guild_and_clans(db, guild_id, ("#CLAN1", "#CLAN2"))
    event_id = await _make_announced_event(
        db, guild_id, [{"clan_tag": "#CLAN1", "cwl_start_at": "2020-01-01T08:00Z"}], status="announced",
    )
    await _seed_player(db, None, "#P1", "#CLAN2", "NoLink")
    _assign(db, event_id, "#P1", "#CLAN1")
    db.mark_cwl_assignment_notified_sync(event_id, "#P1")
    monkeypatch.setattr(CACHE, "send_user_dm_detailed", AsyncMock(return_value=(True, "sent")), raising=False)

    counters = await check_cwl_roster_switches()

    assert counters["alarms"] == 0  # nobody to DM
    assert db.get_cwl_assignments_sync(event_id)[0]["alarm_stage_sent"] == 2
