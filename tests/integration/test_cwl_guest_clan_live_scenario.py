"""End-to-end scenario tests reproducing the project owner's live DEV setup (2026-08-19).

This exact scenario — a guild's own clan ("The QCrew") plus a cross-guild-SHARED guest clan
("StayCalm") added before Start Enrollment, then later removed — broke repeatedly across several
rounds of narrower unit-level fixes, each of which passed its own targeted test while the real
end-to-end behavior stayed wrong. These two tests exercise the whole flow the way an admin
actually drives it, so a future regression anywhere along that path fails here even if every
narrower test still passes.
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


async def _member(db, discord_id, player_tag, clan_tag):
    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES (?, ?)", (discord_id, discord_id))
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag) "
        "VALUES (?, ?, ?, 1, ?)", (discord_id, player_tag, player_tag.lstrip("#"), clan_tag))
    await db.conn.commit()


async def _cwl_war(db, clan_tag, players, date="2026-07-01T08:00"):
    war_id = f"war_{clan_tag}_{date}"
    await db.conn.execute(
        "INSERT INTO war_summary (war_id, clan_tag, opponent_tag, is_cwl, cwl_season, date) "
        "VALUES (?, ?, '#OPP', 1, ?, ?)", (war_id, clan_tag, date[:7], date))
    for pt in players:
        await db.conn.execute(
            "INSERT INTO war_attacks (war_id, clan_tag, date, player_name, player_tag, th_level, map_position, attack_order, stars) "
            "VALUES (?, ?, ?, ?, ?, 15, 1, 1, 0)", (war_id, clan_tag, date, pt.lstrip("#"), pt))
    await db.conn.commit()


@pytest.mark.asyncio
async def test_live_scenario_qcrew_plus_shared_staycalm(db, monkeypatch):
    from qapbot import config as config_module
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    monkeypatch.setattr(config_module, "CONFIG", dataclasses.replace(
        config_module.CONFIG, is_dev_mode=False, cwl_dm_restrict_to_admin=False))
    monkeypatch.setattr(CACHE, "db_manager", db)

    gid = "9100"
    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (gid,))
    for tag, name in [("#QCREW", "The QCrew"), ("#STAYCALM", "StayCalm")]:
        await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES (?, ?)", (tag, name))
    await db.conn.commit()
    CACHE.server_config[gid] = {"member_clans": ["#QCREW"], "member_families": []}
    CACHE.clan_families = {}

    # 3 real QCrew members. Two have stale CWL history recorded under StayCalm
    # (they played there previously), one has QCrew history.
    for i, pt in enumerate(["#Q1", "#Q2", "#Q3"]):
        await _member(db, f"dq{i}", pt, "#QCREW")
    await _cwl_war(db, "#STAYCALM", ["#Q1", "#Q2"])
    await _cwl_war(db, "#QCREW", ["#Q3"], date="2026-07-02T08:00")

    # 2 real StayCalm members with StayCalm history.
    for i, pt in enumerate(["#S1", "#S2"]):
        await _member(db, f"ds{i}", pt, "#STAYCALM")
    await _cwl_war(db, "#STAYCALM", ["#S1", "#S2"], date="2026-07-03T08:00")

    event_id = db.create_cwl_event_sync(gid, "2026-08", "creator")
    db.set_cwl_event_clans_sync(event_id, [
        {"clan_tag": "#QCREW", "participating": True},
        {"clan_tag": "#STAYCALM", "participating": True},
    ])
    shared_id = db.create_cwl_shared_clan_sync("#STAYCALM", "2026-08", gid, event_id, "unresolved_first_claimer")
    db.add_guild_to_shared_clan_sync(shared_id, gid, event_id)

    monkeypatch.setattr(CACHE, "send_user_dm_detailed", AsyncMock(return_value=(True, "sent")))
    await start_cwl_enrollment(int(gid), "2026-08")

    local = {a["player_tag"]: a["assigned_clan_tag"] for a in db.get_cwl_assignments_sync(event_id)}
    shared = {p["player_tag"]: p for p in db.get_cwl_shared_clan_players_sync(shared_id)}
    print("\n=== SCENARIO RESULT ===")
    print("local assignments :", local)
    print("shared placements :", {k: (v["assigned"], v["source"]) for k, v in shared.items()})

    # Every real QCrew member belongs in QCrew's own column.
    for pt in ["#Q1", "#Q2", "#Q3"]:
        assert local.get(pt) == "#QCREW", f"{pt} should be in #QCREW, got {local.get(pt)}"
        assert pt not in shared, f"{pt} must not be on StayCalm's shared roster"
    # StayCalm's own members belong to StayCalm (via the shared roster, not local assignments).
    for pt in ["#S1", "#S2"]:
        assert pt in shared and shared[pt]["assigned"] == 1, f"{pt} should be placed in StayCalm"


class _FakeCocClanCache:
    """Stands in for CACHE.coc_clan_cache — records which tags were fetched and populates
    user_players for them, the way coc_cache.update_player_info_in_user_accounts() really does."""

    def __init__(self, db, members_by_clan):
        self.db = db
        self.members_by_clan = members_by_clan
        self.fetched: list = []

    async def get_clan(self, clan_tag):
        self.fetched.append(clan_tag)
        return clan_tag  # opaque handle — only passed straight back to us below

    async def update_player_info_in_user_accounts(self, clan_obj, cache_manager):
        for player_tag, player_name in self.members_by_clan.get(clan_obj, []):
            await self.db.conn.execute(
                "INSERT OR IGNORE INTO users (discord_id, display_name) VALUES ('UNASSIGNED', 'UNASSIGNED')")
            await self.db.conn.execute(
                "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag, th_level) "
                "VALUES ('UNASSIGNED', ?, ?, 0, ?, 16)", (player_tag, player_name, clan_obj))
        await self.db.conn.commit()


@pytest.mark.asyncio
async def test_untracked_guest_clan_members_still_enter_the_pool(db, monkeypatch):
    """2026-08-19 live bug report, project owner: "I added Hoehenloher Land as a guest clan but its
    members don't show up in the player pool."

    Every pool seed reads user_players.current_clan_tag, which is only ever populated for clans
    with clans.has_active_subscriptions — computed from channel subscriptions + member_clans +
    member_families, none of which a CWL *guest* clan is in. A guest clan no guild on this bot
    tracks independently therefore had ZERO member rows and seeded nobody. (Masked in all earlier
    testing because the guest clans used until then happened to be another guild's member clan.)
    """
    from qapbot import config as config_module
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    monkeypatch.setattr(config_module, "CONFIG", dataclasses.replace(
        config_module.CONFIG, is_dev_mode=False, cwl_dm_restrict_to_admin=False))
    monkeypatch.setattr(CACHE, "db_manager", db)

    gid = "9102"
    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (gid,))
    for tag, name in [("#QCREW", "The QCrew"), ("#GUEST", "Hohenloher Land")]:
        await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES (?, ?)", (tag, name))
    await db.conn.commit()
    CACHE.server_config[gid] = {"member_clans": ["#QCREW"], "member_families": []}
    CACHE.clan_families = {}

    # The guild's own clan is tracked; the guest clan has NO user_players rows at all — exactly
    # the live state (0 rows for #LGYLJQVP vs 28 for #L2J0C0PY).
    await _member(db, "dq0", "#Q1", "#QCREW")

    event_id = db.create_cwl_event_sync(gid, "2026-08", "creator")
    db.set_cwl_event_clans_sync(event_id, [
        {"clan_tag": "#QCREW", "participating": True},
        {"clan_tag": "#GUEST", "participating": True},
    ])

    fake_coc = _FakeCocClanCache(db, {"#GUEST": [("#G1", "Guest One"), ("#G2", "Guest Two")]})
    monkeypatch.setattr(CACHE, "coc_clan_cache", fake_coc, raising=False)
    monkeypatch.setattr(CACHE, "coc_client", object(), raising=False)
    monkeypatch.setattr(CACHE, "send_user_dm_detailed", AsyncMock(return_value=(True, "sent")))

    await start_cwl_enrollment(int(gid), "2026-08")

    signups = {s["player_tag"] for s in db.get_cwl_signups_for_event_sync(event_id)}
    assert {"#G1", "#G2"} <= signups, f"untracked guest clan's members must enter the pool, got {sorted(signups)}"
    assert "#Q1" in signups
    # Only the clan that actually lacked member data was fetched — the already-tracked one must
    # not trigger a live API call on every Start Enrollment.
    assert fake_coc.fetched == ["#GUEST"]


@pytest.mark.asyncio
async def test_guest_clan_added_after_enrollment_seeds_untracked_members(db, monkeypatch):
    """Same gap on the OTHER entry point: a guest clan added to the roster once enrollment is
    already open goes through auto_assign_prior_cwl_members() instead of Start Enrollment's bulk
    seed, and read the same empty member list."""
    from qapbot import config as config_module
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import auto_assign_prior_cwl_members

    monkeypatch.setattr(config_module, "CONFIG", dataclasses.replace(
        config_module.CONFIG, is_dev_mode=False, cwl_dm_restrict_to_admin=False))
    monkeypatch.setattr(CACHE, "db_manager", db)

    gid = "9103"
    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (gid,))
    for tag, name in [("#QCREW", "The QCrew"), ("#GUEST", "Hohenloher Land")]:
        await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES (?, ?)", (tag, name))
    await db.conn.commit()
    CACHE.server_config[gid] = {"member_clans": ["#QCREW"], "member_families": []}
    CACHE.clan_families = {}

    event_id = db.create_cwl_event_sync(gid, "2026-08", "creator")
    db.set_cwl_event_clans_sync(event_id, [
        {"clan_tag": "#QCREW", "participating": True},
        {"clan_tag": "#GUEST", "participating": True},
    ])

    fake_coc = _FakeCocClanCache(db, {"#GUEST": [("#G1", "Guest One"), ("#G2", "Guest Two")]})
    monkeypatch.setattr(CACHE, "coc_clan_cache", fake_coc, raising=False)
    monkeypatch.setattr(CACHE, "coc_client", object(), raising=False)

    await auto_assign_prior_cwl_members(int(gid), event_id, "2026-08", "#GUEST")

    signups = {s["player_tag"] for s in db.get_cwl_signups_for_event_sync(event_id)}
    assert {"#G1", "#G2"} <= signups, f"guest clan added post-enrollment must seed its members, got {sorted(signups)}"


@pytest.mark.asyncio
async def test_live_scenario_then_remove_staycalm(db, monkeypatch):
    """Continues the scenario: after enrollment, Remove StayCalm. QCrew members must stay in
    QCrew's column; StayCalm's own members must be purged from the pool."""
    from qapbot import config as config_module
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment, remove_cwl_guest_clan

    monkeypatch.setattr(config_module, "CONFIG", dataclasses.replace(
        config_module.CONFIG, is_dev_mode=False, cwl_dm_restrict_to_admin=False))
    monkeypatch.setattr(CACHE, "db_manager", db)

    gid = "9101"
    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (gid,))
    for tag, name in [("#QCREW", "The QCrew"), ("#STAYCALM", "StayCalm")]:
        await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES (?, ?)", (tag, name))
    await db.conn.commit()
    CACHE.server_config[gid] = {"member_clans": ["#QCREW"], "member_families": []}
    CACHE.clan_families = {}

    for i, pt in enumerate(["#Q1", "#Q2"]):
        await _member(db, f"dq{i}", pt, "#QCREW")
    await _cwl_war(db, "#STAYCALM", ["#Q1"])          # stale history under StayCalm
    await _cwl_war(db, "#QCREW", ["#Q2"], date="2026-07-02T08:00")
    for i, pt in enumerate(["#S1", "#S2"]):
        await _member(db, f"ds{i}", pt, "#STAYCALM")
    await _cwl_war(db, "#STAYCALM", ["#S1", "#S2"], date="2026-07-03T08:00")

    event_id = db.create_cwl_event_sync(gid, "2026-08", "creator")
    db.set_cwl_event_clans_sync(event_id, [
        {"clan_tag": "#QCREW", "participating": True},
        {"clan_tag": "#STAYCALM", "participating": True},
    ])
    shared_id = db.create_cwl_shared_clan_sync("#STAYCALM", "2026-08", gid, event_id, "unresolved_first_claimer")
    db.add_guild_to_shared_clan_sync(shared_id, gid, event_id)

    monkeypatch.setattr(CACHE, "send_user_dm_detailed", AsyncMock(return_value=(True, "sent")))
    await start_cwl_enrollment(int(gid), "2026-08")

    await remove_cwl_guest_clan(int(gid), event_id, "2026-08", "#STAYCALM")

    local = {a["player_tag"]: a["assigned_clan_tag"] for a in db.get_cwl_assignments_sync(event_id)}
    signups = {s["player_tag"] for s in db.get_cwl_signups_for_event_sync(event_id)}
    print("\n=== AFTER REMOVE STAYCALM ===")
    print("local assignments:", local)
    print("signups remaining:", sorted(signups))

    for pt in ["#Q1", "#Q2"]:
        assert pt in signups, f"{pt} (real QCrew member) must stay in the pool"
        assert local.get(pt) == "#QCREW", f"{pt} should still be in #QCREW, got {local.get(pt)}"
    for pt in ["#S1", "#S2"]:
        assert pt not in signups, f"{pt} (StayCalm member) should be purged by Remove"
    assert {c["clan_tag"] for c in db.get_cwl_event_clans_sync(event_id)} == {"#QCREW"}
