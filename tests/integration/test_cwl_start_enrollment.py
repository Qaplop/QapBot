"""Tests for start_cwl_enrollment() (CWL_ROSTER_PLANNING_PLAN.md Phase 2, slice 2):
seeding from participating clans' current membership, the confirm/opt-out DM blast, the
CWL DM recipient guard (operational directive added 2026-08-10, made an independent
DEV/PROD-toggleable CONFIG.cwl_dm_restrict_to_admin flag on 2026-08-14 so it can also be
enabled on PROD while the roster-assignment feature is still under live testing there),
and the draft -> signup_open transition.

Corrected 2026-08-10: the seed source was originally last season's CWL war-attacker history
(get_previous_cwl_participants_sync) — a live DEV test showed this seeds zero signups for any
clan with no tracked CWL wars yet, even with real known members. Now seeds from
user_players.current_clan_tag (get_current_clan_members_sync) instead.
"""
from __future__ import annotations

import dataclasses
import os
from unittest.mock import AsyncMock

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


async def _seed_guild_and_clan(db: WarHistoryDB, guild_id: str, clan_tag: str = "#CLAN1") -> None:
    from qapbot.cache_manager import CACHE

    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
    await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES (?, ?)", (clan_tag, "Test Clan"))
    await db.conn.commit()
    # resolve_guild_member_clan_tags() (2026-08-14 auto-assignment redesign) reads the guild's
    # member clans from CACHE.server_config, not the DB directly (matching production — that
    # cache is normally kept in sync by save_guild_config()) — mirror it here so
    # start_cwl_enrollment()'s broadened candidate pool actually includes this seeded clan.
    CACHE.server_config[guild_id] = {"member_clans": [clan_tag], "member_families": []}


async def _seed_current_clan_member(
    db: WarHistoryDB, discord_id: str, player_tag: str, clan_tag: str = "#CLAN1",
    verified: bool = True, cwl_permanent_optout: bool = False,
) -> None:
    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES (?, ?)", (discord_id, discord_id))
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified, cwl_permanent_optout, current_clan_tag) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (discord_id, player_tag, "Player", 1 if verified else 0, 1 if cwl_permanent_optout else 0, clan_tag),
    )
    await db.conn.commit()


async def _make_event(db: WarHistoryDB, guild_id: str, season: str, clan_tag: str = "#CLAN1") -> int:
    event_id = db.create_cwl_event_sync(guild_id, season, "creator")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": clan_tag}])
    return event_id


async def _seed_cwl_war(db: WarHistoryDB, clan_tag: str, players: list, date: str = "2026-07-01T08:00") -> None:
    """attack_order=1 (a real attack) — get_last_real_cwl_attack_clan_sync's auto-assignment
    source excludes 0-attack "missed attack" sentinel rows, see its own docstring."""
    war_id = f"war_{clan_tag}_{date}"
    await db.conn.execute(
        "INSERT INTO war_summary (war_id, clan_tag, opponent_tag, is_cwl, cwl_season, date) VALUES (?, ?, ?, 1, ?, ?)",
        (war_id, clan_tag, "#OPP", date[:7], date),
    )
    for player_tag, player_name in players:
        await db.conn.execute(
            "INSERT INTO war_attacks "
            "(war_id, clan_tag, date, player_name, player_tag, th_level, map_position, attack_order, stars) "
            "VALUES (?, ?, ?, ?, ?, 15, 1, 1, 0)",
            (war_id, clan_tag, date, player_name, player_tag),
        )
    await db.conn.commit()


@pytest.mark.asyncio
async def test_rejects_when_no_event(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    monkeypatch.setattr(CACHE, "db_manager", db)
    summary = await start_cwl_enrollment(1001, "2099-01")
    assert summary["ok"] is False
    assert summary["error"] == "no_event"


@pytest.mark.asyncio
async def test_rejects_when_not_draft(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    await _seed_guild_and_clan(db, "1002")
    monkeypatch.setattr(CACHE, "db_manager", db)
    event_id = await _make_event(db, "1002", "2026-08")
    db.update_cwl_event_status_sync(event_id, "signup_open")

    summary = await start_cwl_enrollment(1002, "2026-08")
    assert summary["ok"] is False
    assert summary["error"] == "not_draft"


@pytest.mark.asyncio
async def test_rejects_when_no_participating_clans(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    await _seed_guild_and_clan(db, "1003")
    monkeypatch.setattr(CACHE, "db_manager", db)
    db.create_cwl_event_sync("1003", "2026-08", "creator")  # no clans configured

    summary = await start_cwl_enrollment(1003, "2026-08")
    assert summary["ok"] is False
    assert summary["error"] == "no_clans"


@pytest.mark.asyncio
async def test_no_tracked_members_still_opens_enrollment_with_zero_seeded(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    await _seed_guild_and_clan(db, "1004")
    monkeypatch.setattr(CACHE, "db_manager", db)
    await _make_event(db, "1004", "2026-08")

    summary = await start_cwl_enrollment(1004, "2026-08")
    assert summary["ok"] is True
    assert summary["seeded"] == 0
    assert summary["contacted"] == 0
    assert db.get_cwl_event_sync("1004", "2026-08")["status"] == "signup_open"


@pytest.mark.asyncio
async def test_seeds_signups_and_dms_linked_confirmed_accounts(db, monkeypatch):
    from qapbot import config as config_module
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    # Deterministic regardless of this machine's own .env (DEV checkouts have is_dev_mode=True
    # ambiently, and cwl_dm_restrict_to_admin defaults True everywhere) — the DM guard itself
    # is exercised separately below.
    monkeypatch.setattr(
        config_module, "CONFIG",
        dataclasses.replace(config_module.CONFIG, is_dev_mode=False, cwl_dm_restrict_to_admin=False),
    )

    await _seed_guild_and_clan(db, "1005")
    monkeypatch.setattr(CACHE, "db_manager", db)
    # German guild — the DM text AND its button labels must both localize (2026-08-14 fix: the
    # buttons used to always render in the default language because guild_id was never passed
    # through to build_cwl_signup_response_view()).
    CACHE.server_config["1005"] = {"language": "de"}
    await _seed_current_clan_member(db, "d1", "#P1")  # linked, not opted out
    await _seed_current_clan_member(db, "UNASSIGNED", "#P2")  # tracked but not linked to a real account
    await _make_event(db, "1005", "2026-08")

    sent_dms = []

    async def fake_send_user_dm(user_id, message, view=None, embed=None):
        sent_dms.append((user_id, message, view))
        return True

    monkeypatch.setattr(CACHE, "send_user_dm", fake_send_user_dm)

    summary = await start_cwl_enrollment(1005, "2026-08")

    assert summary["ok"] is True
    assert summary["seeded"] == 2
    assert summary["contacted"] == 1
    assert summary["skipped_unlinked"] == 1
    assert summary["skipped_optout"] == 0
    assert len(sent_dms) == 1
    assert sent_dms[0][0] == "d1"
    assert "2026-08" in sent_dms[0][1]  # the new dm_body names the season
    view = sent_dms[0][2]
    assert view is not None  # a view (the confirm/opt-out buttons) was attached
    button_labels = {item.item.label for item in view.children}  # type: ignore[union-attr]
    assert button_labels == {"Bestätigen", "Abmelden"}  # German labels, not the English default

    signup = db.get_cwl_signup_sync(db.get_cwl_event_sync("1005", "2026-08")["id"], "#P1")
    assert signup["status"] == "pending"
    assert signup["source"] == "template_confirm"


@pytest.mark.asyncio
async def test_departed_member_is_not_seeded(db, monkeypatch):
    """A player currently in a different clan (or no clan at all) must not be pulled into this
    clan's enrollment, even if they were previously in it — matches live membership only."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    await _seed_guild_and_clan(db, "1009", clan_tag="#CLAN1")
    await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES ('#OTHER_CLAN', 'Other Clan')")
    await db.conn.commit()
    monkeypatch.setattr(CACHE, "db_manager", db)
    await _seed_current_clan_member(db, "d1", "#P1", clan_tag="#OTHER_CLAN")
    await _make_event(db, "1009", "2026-08", clan_tag="#CLAN1")

    monkeypatch.setattr(CACHE, "send_user_dm", AsyncMock(return_value=True))

    summary = await start_cwl_enrollment(1009, "2026-08")
    assert summary["seeded"] == 0


@pytest.mark.asyncio
async def test_skips_permanently_opted_out_accounts(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    await _seed_guild_and_clan(db, "1006")
    monkeypatch.setattr(CACHE, "db_manager", db)
    await _seed_current_clan_member(db, "d1", "#P1", cwl_permanent_optout=True)
    await _make_event(db, "1006", "2026-08")

    monkeypatch.setattr(CACHE, "send_user_dm", AsyncMock(return_value=True))

    summary = await start_cwl_enrollment(1006, "2026-08")

    assert summary["seeded"] == 0
    assert summary["contacted"] == 0
    assert summary["skipped_optout"] == 1
    event_id = db.get_cwl_event_sync("1006", "2026-08")["id"]
    assert db.get_cwl_signup_sync(event_id, "#P1") is None  # never even created


@pytest.mark.asyncio
async def test_dm_guard_only_dms_the_configured_server_admin_in_dev(db, monkeypatch):
    """Operational directive (CWL_ROSTER_PLANNING_PLAN.md, added 2026-08-10): while testing
    Phase 2 in DEV, a bulk DM blast must only ever reach the project owner's own account.
    The guard is CONFIG.cwl_dm_restrict_to_admin (independent of is_dev_mode since 2026-08-14) —
    this exercises it with is_dev_mode=True to confirm DEV behavior is unchanged."""
    from qapbot import config as config_module
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    fake_config = dataclasses.replace(
        config_module.CONFIG, is_dev_mode=True, server_admin="d1", cwl_dm_restrict_to_admin=True,
    )
    monkeypatch.setattr(config_module, "CONFIG", fake_config)

    await _seed_guild_and_clan(db, "1007")
    monkeypatch.setattr(CACHE, "db_manager", db)
    await _seed_current_clan_member(db, "d1", "#P1")  # matches server_admin -> DMed
    await _seed_current_clan_member(db, "d2", "#P2")  # does not match -> guarded/skipped
    await _make_event(db, "1007", "2026-08")

    sent_to = []

    async def fake_send_user_dm(user_id, message, view=None, embed=None):
        sent_to.append(user_id)
        return True

    monkeypatch.setattr(CACHE, "send_user_dm", fake_send_user_dm)

    summary = await start_cwl_enrollment(1007, "2026-08")

    assert summary["seeded"] == 2  # both signup rows are still created — only DM delivery is guarded
    assert summary["contacted"] == 1
    assert summary["skipped_dm_guard"] == 1
    assert sent_to == ["d1"]


@pytest.mark.asyncio
async def test_dm_guard_only_dms_the_configured_server_admin_in_prod(db, monkeypatch):
    """2026-08-14: the DM guard is now independent of is_dev_mode specifically so it can also
    be enabled on PROD while the roster-assignment feature is still being live-tested there —
    this is the scenario that motivated splitting it out of the is_dev_mode check."""
    from qapbot import config as config_module
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    fake_config = dataclasses.replace(
        config_module.CONFIG, is_dev_mode=False, server_admin="d1", cwl_dm_restrict_to_admin=True,
    )
    monkeypatch.setattr(config_module, "CONFIG", fake_config)

    await _seed_guild_and_clan(db, "1009")
    monkeypatch.setattr(CACHE, "db_manager", db)
    await _seed_current_clan_member(db, "d1", "#P1")
    await _seed_current_clan_member(db, "d2", "#P2")
    await _make_event(db, "1009", "2026-08")

    sent_to = []

    async def fake_send_user_dm(user_id, message, view=None, embed=None):
        sent_to.append(user_id)
        return True

    monkeypatch.setattr(CACHE, "send_user_dm", fake_send_user_dm)

    summary = await start_cwl_enrollment(1009, "2026-08")

    assert summary["contacted"] == 1
    assert summary["skipped_dm_guard"] == 1
    assert sent_to == ["d1"]


@pytest.mark.asyncio
async def test_prod_mode_is_unaffected_when_dm_guard_disabled(db, monkeypatch):
    from qapbot import config as config_module
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    fake_config = dataclasses.replace(
        config_module.CONFIG, is_dev_mode=False, server_admin="d1", cwl_dm_restrict_to_admin=False,
    )
    monkeypatch.setattr(config_module, "CONFIG", fake_config)

    await _seed_guild_and_clan(db, "1008")
    monkeypatch.setattr(CACHE, "db_manager", db)
    await _seed_current_clan_member(db, "d1", "#P1")
    await _seed_current_clan_member(db, "d2", "#P2")
    await _make_event(db, "1008", "2026-08")

    sent_to = []

    async def fake_send_user_dm(user_id, message, view=None, embed=None):
        sent_to.append(user_id)
        return True

    monkeypatch.setattr(CACHE, "send_user_dm", fake_send_user_dm)

    summary = await start_cwl_enrollment(1008, "2026-08")

    assert summary["contacted"] == 2
    assert summary["skipped_dm_guard"] == 0
    assert sorted(sent_to) == ["d1", "d2"]


@pytest.mark.asyncio
async def test_seeds_auto_assignments_from_last_months_cwl_activity(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    await _seed_guild_and_clan(db, "1010")
    monkeypatch.setattr(CACHE, "db_manager", db)
    await _seed_current_clan_member(db, "d1", "#P1")
    await _seed_cwl_war(db, "#CLAN1", [("#P1", "Alpha")])
    await _make_event(db, "1010", "2026-08")

    monkeypatch.setattr(CACHE, "send_user_dm", AsyncMock(return_value=True))

    summary = await start_cwl_enrollment(1010, "2026-08")

    assert summary["assigned"] == 1
    event_id = db.get_cwl_event_sync("1010", "2026-08")["id"]
    assignments = db.get_cwl_assignments_sync(event_id)
    assert len(assignments) == 1
    assert assignments[0]["player_tag"] == "#P1"
    assert assignments[0]["assigned_clan_tag"] == "#CLAN1"
    assert assignments[0]["assignment_source"] == "suggested"


@pytest.mark.asyncio
async def test_departed_member_is_not_auto_assigned(db, monkeypatch):
    """A player with CWL history for this clan who is no longer a current member (per
    user_players.current_clan_tag) must not get auto-assigned — matches current membership."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    await _seed_guild_and_clan(db, "1011")
    monkeypatch.setattr(CACHE, "db_manager", db)
    # #P1 has CWL history for #CLAN1 but is not currently tracked as a member of any clan.
    await _seed_cwl_war(db, "#CLAN1", [("#P1", "Alpha")])
    await _make_event(db, "1011", "2026-08")

    monkeypatch.setattr(CACHE, "send_user_dm", AsyncMock(return_value=True))

    summary = await start_cwl_enrollment(1011, "2026-08")

    assert summary["assigned"] == 0
    event_id = db.get_cwl_event_sync("1011", "2026-08")["id"]
    assert db.get_cwl_assignments_sync(event_id) == []


@pytest.mark.asyncio
async def test_no_cwl_history_leaves_player_unassigned(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    await _seed_guild_and_clan(db, "1012")
    monkeypatch.setattr(CACHE, "db_manager", db)
    await _seed_current_clan_member(db, "d1", "#P1")  # never played CWL
    await _make_event(db, "1012", "2026-08")

    monkeypatch.setattr(CACHE, "send_user_dm", AsyncMock(return_value=True))

    summary = await start_cwl_enrollment(1012, "2026-08")

    assert summary["seeded"] == 1  # still seeded as a signup candidate
    assert summary["assigned"] == 0  # just not auto-assigned to any clan
