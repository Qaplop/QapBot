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

    async def fake_send_user_dm_detailed(user_id, message, view=None, embed=None, sent_message_out=None):
        sent_dms.append((user_id, message, view))
        return True, "sent"

    monkeypatch.setattr(CACHE, "send_user_dm_detailed", fake_send_user_dm_detailed)

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
async def test_dms_a_guest_player_invited_during_draft(db, monkeypatch):
    """2026-08-20 live bug report: a player manually invited as a guest during the draft phase
    got no enrollment DM when Start Enrollment ran — only the rule-h "Notify New Pool Members"
    button ever reached them. A guest's real current clan is by definition none of this guild's
    pooled clans, so start_cwl_enrollment's get_current_clan_members_sync() scan can't see them;
    their already-existing cwl_signups row is now folded into the DM targets separately."""
    from qapbot import config as config_module
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    monkeypatch.setattr(
        config_module, "CONFIG",
        dataclasses.replace(config_module.CONFIG, is_dev_mode=False, cwl_dm_restrict_to_admin=False),
    )

    await _seed_guild_and_clan(db, "1015")
    monkeypatch.setattr(CACHE, "db_manager", db)
    await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES ('#OTHERCLAN', 'Other')")
    await db.conn.commit()
    await _seed_current_clan_member(db, "d1", "#P1")
    # The guest: linked, but plays in a clan this guild has nothing to do with.
    await _seed_current_clan_member(db, "d9", "#GUEST", clan_tag="#OTHERCLAN")
    # ...and a second guest whose signup row carries no discord_id (the Guests search can add a
    # tag it found no link for) — the link must still be resolved so they get DMed too.
    await _seed_current_clan_member(db, "d8", "#GUEST2", clan_tag="#OTHERCLAN")
    event_id = await _make_event(db, "1015", "2026-08")
    db.upsert_cwl_signup_sync(event_id, "#GUEST", "GuestOne", "d9", None, "guest_invite", "pending")
    db.upsert_cwl_signup_sync(event_id, "#GUEST2", "GuestTwo", None, None, "guest_invite", "pending")

    sent_dms = []

    async def fake_send_user_dm_detailed(user_id, message, view=None, embed=None, sent_message_out=None):
        sent_dms.append(user_id)
        return True, "sent"

    monkeypatch.setattr(CACHE, "send_user_dm_detailed", fake_send_user_dm_detailed)

    summary = await start_cwl_enrollment(1015, "2026-08")

    assert summary["ok"] is True
    assert sorted(sent_dms) == ["d1", "d8", "d9"]
    assert summary["contacted"] == 3
    # Guests were already pooled by the invite — Start Enrollment must not re-seed them.
    assert summary["seeded"] == 1
    assert db.get_cwl_signup_sync(event_id, "#GUEST")["source"] == "guest_invite"


@pytest.mark.asyncio
async def test_skips_a_permanently_opted_out_guest_player(db, monkeypatch):
    """The pooled-guest DM pass above must honour cwl_permanent_optout exactly like the
    current-member pass does."""
    from qapbot import config as config_module
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    monkeypatch.setattr(
        config_module, "CONFIG",
        dataclasses.replace(config_module.CONFIG, is_dev_mode=False, cwl_dm_restrict_to_admin=False),
    )

    await _seed_guild_and_clan(db, "1016")
    monkeypatch.setattr(CACHE, "db_manager", db)
    await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES ('#OTHERCLAN', 'Other')")
    await db.conn.commit()
    await _seed_current_clan_member(
        db, "d9", "#GUEST", clan_tag="#OTHERCLAN", cwl_permanent_optout=True,
    )
    event_id = await _make_event(db, "1016", "2026-08")
    db.upsert_cwl_signup_sync(event_id, "#GUEST", "GuestOne", "d9", None, "guest_invite", "pending")

    sent_dms = []

    async def fake_send_user_dm_detailed(user_id, message, view=None, embed=None, sent_message_out=None):
        sent_dms.append(user_id)
        return True, "sent"

    monkeypatch.setattr(CACHE, "send_user_dm_detailed", fake_send_user_dm_detailed)

    summary = await start_cwl_enrollment(1016, "2026-08")

    assert summary["ok"] is True
    assert sent_dms == []
    assert summary["skipped_optout"] == 1


@pytest.mark.asyncio
async def test_deleting_and_recreating_same_season_dms_again(db, monkeypatch):
    """2026-08-19 live bug report: an admin started enrollment, deleted the season ("Delete
    Season" — mainly for testing/starting over), then started a brand-new event for the SAME
    season string expecting fresh DMs. Before the fix, cwl_player_season_status.dm_sent (global,
    keyed by (player_tag, cwl_season) — deliberately not cascaded from cwl_events) was still
    standing from the deleted event, so the second Start Enrollment silently DMed nobody at all."""
    from qapbot import config as config_module
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    monkeypatch.setattr(
        config_module, "CONFIG",
        dataclasses.replace(config_module.CONFIG, is_dev_mode=False, cwl_dm_restrict_to_admin=False),
    )

    await _seed_guild_and_clan(db, "1006")
    monkeypatch.setattr(CACHE, "db_manager", db)
    await _seed_current_clan_member(db, "d1", "#P1")
    event_id = await _make_event(db, "1006", "2026-08")

    sent_dms = []

    async def fake_send_user_dm_detailed(user_id, message, view=None, embed=None, sent_message_out=None):
        sent_dms.append(user_id)
        return True, "sent"

    monkeypatch.setattr(CACHE, "send_user_dm_detailed", fake_send_user_dm_detailed)

    first_summary = await start_cwl_enrollment(1006, "2026-08")
    assert first_summary["contacted"] == 1
    assert sent_dms == ["d1"]

    assert db.delete_cwl_event_sync(event_id) is True
    await _make_event(db, "1006", "2026-08")  # new event, same season string

    second_summary = await start_cwl_enrollment(1006, "2026-08")

    assert second_summary["contacted"] == 1  # must DM again — the old event is gone for good
    assert sent_dms == ["d1", "d1"]


@pytest.mark.asyncio
async def test_one_recipients_dm_failure_does_not_abort_the_rest_of_the_batch(db, monkeypatch):
    """2026-08-18, item 3 of the enrollment redesign: a Discord error for one recipient used to
    re-raise out of CACHE.send_user_dm() and abort start_cwl_enrollment()'s whole DM loop —
    every recipient after the failing one never got DMed, and the event never reached
    signup_open (that transition runs after the loop). The fix now lives one layer down in
    cache_manager.py (retried, never re-raised) — this asserts the outcome an admin actually
    cares about: every OTHER recipient still gets DMed, the event still opens, and the failure
    is reported back instead of silently vanishing."""
    from qapbot import config as config_module
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    monkeypatch.setattr(
        config_module, "CONFIG",
        dataclasses.replace(config_module.CONFIG, is_dev_mode=False, cwl_dm_restrict_to_admin=False),
    )

    await _seed_guild_and_clan(db, "1030")
    monkeypatch.setattr(CACHE, "db_manager", db)
    await _seed_current_clan_member(db, "d1", "#P1")  # this one's DM "fails" (retries exhausted)
    await _seed_current_clan_member(db, "d2", "#P2")  # must still be reached afterward
    await _make_event(db, "1030", "2026-08")

    contacted = []

    async def fake_send_user_dm_detailed(user_id, message, view=None, embed=None, sent_message_out=None):
        if user_id == "d1":
            return False, "failed"
        contacted.append(user_id)
        return True, "sent"

    monkeypatch.setattr(CACHE, "send_user_dm_detailed", fake_send_user_dm_detailed)

    summary = await start_cwl_enrollment(1030, "2026-08")

    assert summary["ok"] is True
    assert summary["seeded"] == 2
    assert summary["contacted"] == 1
    assert contacted == ["d2"]  # d2 was still reached despite d1's failure
    assert summary["failed"] == ["Player"]  # d1's player_name, reported back for follow-up
    assert summary["blocked"] == []
    assert db.get_cwl_event_sync("1030", "2026-08")["status"] == "signup_open"


@pytest.mark.asyncio
async def test_blocked_recipient_is_not_marked_as_dm_sent(db, monkeypatch):
    """2026-08-19, project owner's question after live-testing the "Blocked (DMs disabled or bot
    blocked, not retried)" summary line: a recipient who never actually received the DM must NOT
    be recorded as dm_sent — otherwise they'd be silently skipped forever by the global dedup
    (get_cwl_player_season_dm_status_bulk_sync) even after re-enabling DMs, with no way to ever
    reach them again short of a DB fix. Confirms _send_cwl_enrollment_dm_batch's own contract
    (mark_cwl_player_dm_sent_sync is only ever called inside `if sent:`) holds end-to-end, and
    that every OTHER recipient in the same batch is still reached normally."""
    from qapbot import config as config_module
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    monkeypatch.setattr(
        config_module, "CONFIG",
        dataclasses.replace(config_module.CONFIG, is_dev_mode=False, cwl_dm_restrict_to_admin=False),
    )

    await _seed_guild_and_clan(db, "1031")
    monkeypatch.setattr(CACHE, "db_manager", db)
    await _seed_current_clan_member(db, "d1", "#P1")  # blocks the bot / DMs disabled
    await _seed_current_clan_member(db, "d2", "#P2")  # must still be reached normally
    await _make_event(db, "1031", "2026-08")

    contacted = []

    async def fake_send_user_dm_detailed(user_id, message, view=None, embed=None, sent_message_out=None):
        if user_id == "d1":
            return False, "blocked"
        contacted.append(user_id)
        return True, "sent"

    monkeypatch.setattr(CACHE, "send_user_dm_detailed", fake_send_user_dm_detailed)

    summary = await start_cwl_enrollment(1031, "2026-08")

    assert summary["contacted"] == 1
    assert contacted == ["d2"]
    assert summary["blocked"] == ["Player"]  # d1's player_name
    assert summary["failed"] == []

    # The blocked recipient never got a cwl_player_season_status row at all — never DMed means
    # never marked DMed, so they're still eligible to be reached the moment DMs are re-enabled.
    assert db.get_cwl_player_season_status_sync("#P1", "2026-08") is None
    # The reached recipient DOES have one, dm_sent=1, as normal.
    status = db.get_cwl_player_season_status_sync("#P2", "2026-08")
    assert status is not None
    assert status["dm_sent"] == 1


@pytest.mark.asyncio
async def test_non_participating_family_clan_members_are_still_pooled_and_dmed(db, monkeypatch):
    """The original live bug report this whole redesign started from: a test user ("Lucas")
    with linked accounts across several clans in one guild's family only got enrollment DMs for
    SOME of them — the accounts whose clan happened to be toggled `participating` for this CWL
    event. Rule b (2026-08-18, project owner's spec, verbatim): "when a guild starts a new
    season ALL memebrs of all clans should be put to the player pool regardless of participation
    status of each clan." #CLAN2 here is a real family clan (same as #CLAN1) but NOT toggled
    participating for this event — its member must still be seeded and DMed. #OTHER_CLAN is a
    genuinely unrelated clan (neither family nor configured on this event at all) and must still
    be excluded — this isn't "pool literally everyone in the database," just the whole family."""
    from qapbot import config as config_module
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    monkeypatch.setattr(
        config_module, "CONFIG",
        dataclasses.replace(config_module.CONFIG, is_dev_mode=False, cwl_dm_restrict_to_admin=False),
    )

    guild_id = "1031"
    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
    await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES ('#CLAN1', 'Clan One')")
    await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES ('#CLAN2', 'Clan Two')")
    await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES ('#OTHER_CLAN', 'Unrelated Clan')")
    await db.conn.commit()
    # Both #CLAN1 and #CLAN2 are this guild's own family — #OTHER_CLAN is not.
    CACHE.server_config[guild_id] = {"member_clans": ["#CLAN1", "#CLAN2"], "member_families": []}
    monkeypatch.setattr(CACHE, "db_manager", db)

    await _seed_current_clan_member(db, "d1", "#P1", clan_tag="#CLAN1")
    await _seed_current_clan_member(db, "d2", "#P2", clan_tag="#CLAN2")  # family, but not participating
    await _seed_current_clan_member(db, "d3", "#P3", clan_tag="#OTHER_CLAN")  # not family at all

    event_id = db.create_cwl_event_sync(guild_id, "2026-08", "creator")
    db.set_cwl_event_clans_sync(event_id, [
        {"clan_tag": "#CLAN1", "participating": True},
        {"clan_tag": "#CLAN2", "participating": False},
    ])

    contacted = []

    async def fake_send_user_dm_detailed(user_id, message, view=None, embed=None, sent_message_out=None):
        contacted.append(user_id)
        return True, "sent"

    monkeypatch.setattr(CACHE, "send_user_dm_detailed", fake_send_user_dm_detailed)

    summary = await start_cwl_enrollment(int(guild_id), "2026-08")

    assert summary["ok"] is True
    assert summary["seeded"] == 2  # #P1 and #P2 — not #P3
    assert summary["contacted"] == 2
    assert sorted(contacted) == ["d1", "d2"]
    assert db.get_cwl_signup_sync(event_id, "#P1") is not None
    assert db.get_cwl_signup_sync(event_id, "#P2") is not None  # the fix: non-participating family clan, still pooled
    assert db.get_cwl_signup_sync(event_id, "#P3") is None  # genuinely unrelated clan, still excluded


@pytest.mark.asyncio
async def test_globally_already_dmed_player_is_seeded_with_real_status_but_not_redmed(db, monkeypatch):
    """Rule h (2026-08-18, project owner's spec, verbatim): "we generally need only one
    enrollment DM per player regardless of how many guilds and/or clans invite him... Then the
    player accepts or declines or is pending and that status is shown automatically in guild A's
    and guild B's clan rosters." Simulates the cross-guild scenario without needing a second real
    guild: #P1 already has a global cwl_player_season_status row (as if some OTHER guild's Start
    Enrollment already DMed and they confirmed) before THIS guild's Start Enrollment runs."""
    from qapbot import config as config_module
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    monkeypatch.setattr(
        config_module, "CONFIG",
        dataclasses.replace(config_module.CONFIG, is_dev_mode=False, cwl_dm_restrict_to_admin=False),
    )

    await _seed_guild_and_clan(db, "1032")
    monkeypatch.setattr(CACHE, "db_manager", db)
    await _seed_current_clan_member(db, "d1", "#P1")  # already globally DMed+confirmed elsewhere
    await _seed_current_clan_member(db, "d2", "#P2")  # never contacted by anyone yet
    await _make_event(db, "1032", "2026-08")

    # A REAL other guild's event (2026-08-19 follow-up fix: the bulk dedup lookup now only trusts
    # a dm_sent=1 row if it's still traceable to a live cwl_events row — see
    # get_cwl_player_season_dm_status_bulk_sync's own docstring) — this is what "some OTHER
    # guild's Start Enrollment already DMed and they confirmed" actually looks like in production.
    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES ('999')")
    await db.conn.commit()
    other_event_id = db.create_cwl_event_sync("999", "2026-08", "otherdiscordid")
    db.mark_cwl_player_dm_sent_sync("#P1", "2026-08", "Player", "d1", other_event_id, 999, "2026-08-17T09:00Z")
    db.set_cwl_player_response_status_sync(
        "#P1", "2026-08", "Player", "d1", "confirmed", "2026-08-17T09:05Z", other_event_id, 999,
    )

    contacted = []

    async def fake_send_user_dm_detailed(user_id, message, view=None, embed=None, sent_message_out=None):
        contacted.append(user_id)
        return True, "sent"

    monkeypatch.setattr(CACHE, "send_user_dm_detailed", fake_send_user_dm_detailed)

    summary = await start_cwl_enrollment(1032, "2026-08")

    assert summary["ok"] is True
    assert summary["seeded"] == 2
    assert summary["contacted"] == 1
    assert summary["skipped_already_dm_globally"] == 1
    assert contacted == ["d2"]  # #P1 (d1) was NOT re-DMed

    event_id = db.get_cwl_event_sync("1032", "2026-08")["id"]
    p1_signup = db.get_cwl_signup_sync(event_id, "#P1")
    assert p1_signup["status"] == "confirmed"  # seeded with the REAL global status, not 'pending'
    p2_signup = db.get_cwl_signup_sync(event_id, "#P2")
    assert p2_signup["status"] == "pending"  # never contacted anywhere -> genuinely pending


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

    monkeypatch.setattr(CACHE, "send_user_dm_detailed", AsyncMock(return_value=(True, "sent")))

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

    monkeypatch.setattr(CACHE, "send_user_dm_detailed", AsyncMock(return_value=(True, "sent")))

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

    async def fake_send_user_dm_detailed(user_id, message, view=None, embed=None, sent_message_out=None):
        sent_to.append(user_id)
        return True, "sent"

    monkeypatch.setattr(CACHE, "send_user_dm_detailed", fake_send_user_dm_detailed)

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

    async def fake_send_user_dm_detailed(user_id, message, view=None, embed=None, sent_message_out=None):
        sent_to.append(user_id)
        return True, "sent"

    monkeypatch.setattr(CACHE, "send_user_dm_detailed", fake_send_user_dm_detailed)

    summary = await start_cwl_enrollment(1009, "2026-08")

    assert summary["contacted"] == 1
    assert summary["skipped_dm_guard"] == 1
    assert sent_to == ["d1"]


@pytest.mark.asyncio
async def test_dm_guard_also_dms_enrolled_testers_in_prod(db, monkeypatch):
    """2026-08-15 (/admin MANAGE_TESTERS): the guard's exception isn't just
    CONFIG.server_admin any more — in PROD, anyone enrolled in CACHE.testers is treated the
    same way, so a hand-picked group can see the real DM while the rest of the guild stays
    guarded."""
    from qapbot import config as config_module
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    fake_config = dataclasses.replace(
        config_module.CONFIG, is_dev_mode=False, server_admin="d1", cwl_dm_restrict_to_admin=True,
    )
    monkeypatch.setattr(config_module, "CONFIG", fake_config)
    monkeypatch.setattr(CACHE, "testers", {"d2"})

    await _seed_guild_and_clan(db, "1010")
    monkeypatch.setattr(CACHE, "db_manager", db)
    await _seed_current_clan_member(db, "d1", "#P1")  # matches server_admin -> DMed
    await _seed_current_clan_member(db, "d2", "#P2")  # matches a tester -> DMed
    await _seed_current_clan_member(db, "d3", "#P3")  # neither -> guarded/skipped
    await _make_event(db, "1010", "2026-08")

    sent_to = []

    async def fake_send_user_dm_detailed(user_id, message, view=None, embed=None, sent_message_out=None):
        sent_to.append(user_id)
        return True, "sent"

    monkeypatch.setattr(CACHE, "send_user_dm_detailed", fake_send_user_dm_detailed)

    summary = await start_cwl_enrollment(1010, "2026-08")

    assert summary["contacted"] == 2
    assert summary["skipped_dm_guard"] == 1
    assert sorted(sent_to) == ["d1", "d2"]


@pytest.mark.asyncio
async def test_dm_guard_ignores_testers_in_dev(db, monkeypatch):
    """2026-08-15 follow-up (project owner's spec): testers are a PROD-only concept — a DEV
    host must keep DMing only CONFIG.server_admin, exactly as it did before testers existed,
    even if CACHE.testers is non-empty (e.g. shared in-memory state from another guild/test)."""
    from qapbot import config as config_module
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    fake_config = dataclasses.replace(
        config_module.CONFIG, is_dev_mode=True, server_admin="d1", cwl_dm_restrict_to_admin=True,
    )
    monkeypatch.setattr(config_module, "CONFIG", fake_config)
    monkeypatch.setattr(CACHE, "testers", {"d2"})

    await _seed_guild_and_clan(db, "1011")
    monkeypatch.setattr(CACHE, "db_manager", db)
    await _seed_current_clan_member(db, "d1", "#P1")  # matches server_admin -> DMed
    await _seed_current_clan_member(db, "d2", "#P2")  # a tester, but DEV mode -> guarded/skipped
    await _make_event(db, "1011", "2026-08")

    sent_to = []

    async def fake_send_user_dm_detailed(user_id, message, view=None, embed=None, sent_message_out=None):
        sent_to.append(user_id)
        return True, "sent"

    monkeypatch.setattr(CACHE, "send_user_dm_detailed", fake_send_user_dm_detailed)

    summary = await start_cwl_enrollment(1011, "2026-08")

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

    async def fake_send_user_dm_detailed(user_id, message, view=None, embed=None, sent_message_out=None):
        sent_to.append(user_id)
        return True, "sent"

    monkeypatch.setattr(CACHE, "send_user_dm_detailed", fake_send_user_dm_detailed)

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

    monkeypatch.setattr(CACHE, "send_user_dm_detailed", AsyncMock(return_value=(True, "sent")))

    summary = await start_cwl_enrollment(1010, "2026-08")

    assert summary["assigned"] == 1
    event_id = db.get_cwl_event_sync("1010", "2026-08")["id"]
    assignments = db.get_cwl_assignments_sync(event_id)
    assert len(assignments) == 1
    assert assignments[0]["player_tag"] == "#P1"
    assert assignments[0]["assigned_clan_tag"] == "#CLAN1"
    assert assignments[0]["assignment_source"] == "suggested"


@pytest.mark.asyncio
async def test_seeds_auto_assignments_for_a_guest_clans_own_current_members(db, monkeypatch):
    """Confirmed live-testing bug (2026-08-16, project owner's spec, verbatim): "added staycalm
    as a guest clan to the qcrew's clan roster... staycalm didn't get the previous month player
    auto-assignment." A guest clan is by definition NOT part of the guild's own family
    (resolve_guild_member_clan_tags() only ever returns the family) — the auto-assign candidate
    pool must still include a guest clan's own current members (unioned with the family, matching
    _build_enrollment_payload's own player pool), or resolve_prior_cwl_assignments() can never
    place them into their own column no matter how much real prior-CWL-attack history they have,
    since they were never even in the candidate list to begin with."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    guild_id = 10102
    await _seed_guild_and_clan(db, str(guild_id), clan_tag="#CLAN1")  # "The QCrew" — the only family clan
    monkeypatch.setattr(CACHE, "db_manager", db)
    await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES ('#GUESTCLAN', 'StayCalm')")
    await db.conn.commit()
    # #P1 is a current member of #GUESTCLAN only — never part of this guild's own family.
    await _seed_current_clan_member(db, "d1", "#P1", "#GUESTCLAN")
    await _seed_cwl_war(db, "#GUESTCLAN", [("#P1", "Alpha")])

    event_id = db.create_cwl_event_sync(str(guild_id), "2026-08", "creator")
    db.set_cwl_event_clans_sync(event_id, [
        {"clan_tag": "#CLAN1", "participating": True},
        {"clan_tag": "#GUESTCLAN", "participating": True},
    ])
    monkeypatch.setattr(CACHE, "send_user_dm_detailed", AsyncMock(return_value=(True, "sent")))

    summary = await start_cwl_enrollment(guild_id, "2026-08")

    assert summary["assigned"] == 1
    assignments = db.get_cwl_assignments_sync(event_id)
    assert len(assignments) == 1
    assert assignments[0]["player_tag"] == "#P1"
    assert assignments[0]["assigned_clan_tag"] == "#GUESTCLAN"


@pytest.mark.asyncio
async def test_current_family_clan_membership_beats_stale_history_for_a_guest_clan(db, monkeypatch):
    """Live bug report, project owner, verbatim: "when staycalm gets added during the very start
    of adding the new season the theqcrew members get auto assigned to staycalm and not to the
    qcrew as they should." resolve_prior_cwl_assignments() assigns purely by "last real CWL
    attack," independent of current clan (by design, for players whose current clan isn't
    participating) — but a player who is a genuine CURRENT member of a clan that IS participating
    must never have their own real, live family-clan membership overridden by some earlier
    season's attack history for a totally different (here: guest) clan."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    guild_id = 10103
    await _seed_guild_and_clan(db, str(guild_id), clan_tag="#CLAN1")  # "The QCrew"
    monkeypatch.setattr(CACHE, "db_manager", db)
    await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES ('#GUESTCLAN', 'StayCalm')")
    await db.conn.commit()
    # #P1 is a genuine CURRENT member of the guild's own family clan (#CLAN1)...
    await _seed_current_clan_member(db, "d1", "#P1", "#CLAN1")
    # ...but their last REAL CWL attack on record was for #GUESTCLAN (StayCalm) — e.g. from
    # before they transferred into #CLAN1.
    await _seed_cwl_war(db, "#GUESTCLAN", [("#P1", "Alpha")])

    event_id = db.create_cwl_event_sync(str(guild_id), "2026-08", "creator")
    db.set_cwl_event_clans_sync(event_id, [
        {"clan_tag": "#CLAN1", "participating": True},
        {"clan_tag": "#GUESTCLAN", "participating": True},
    ])
    monkeypatch.setattr(CACHE, "send_user_dm_detailed", AsyncMock(return_value=(True, "sent")))

    summary = await start_cwl_enrollment(guild_id, "2026-08")

    assert summary["assigned"] == 1
    assignments = db.get_cwl_assignments_sync(event_id)
    assert len(assignments) == 1
    assert assignments[0]["player_tag"] == "#P1"
    assert assignments[0]["assigned_clan_tag"] == "#CLAN1"  # their real current clan wins


@pytest.mark.asyncio
async def test_current_clan_does_not_beat_history_pointing_at_another_family_clan(db, monkeypatch):
    """2026-08-20 live bug report, project owner ("The Marines" family, PROD): player Killer's
    last real CWL attack was for the family's own #MARINES2 ("The Marines II") last season; they
    have since transferred to the family's other clan, #MARINES1 ("The Marines"), completely
    normal end-of-season churn within the same multi-clan family. The 2026-08-19 override above
    (see test_current_family_clan_membership_beats_stale_history_for_a_guest_clan) redirected them
    to #MARINES1 anyway, purely because #MARINES1 is also participating this season — its actual
    intended scope (per its own docstring, "most commonly a guest clan") was history pointing
    OUTSIDE the family, never history pointing at the family's own other clan. #MARINES2 must win
    here: it's real, one-season-old, in-family CWL history, not stale foreign data."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    guild_id = 10104
    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (str(guild_id),))
    for tag, name in [("#MARINES1", "The Marines"), ("#MARINES2", "The Marines II")]:
        await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES (?, ?)", (tag, name))
    await db.conn.commit()
    CACHE.server_config[str(guild_id)] = {"member_clans": ["#MARINES1", "#MARINES2"], "member_families": []}
    monkeypatch.setattr(CACHE, "db_manager", db)

    # Killer is a genuine CURRENT member of #MARINES1...
    await _seed_current_clan_member(db, "d1", "#KILLER", "#MARINES1")
    # ...but their last REAL CWL attack on record was for #MARINES2 — the family's OTHER clan,
    # last season.
    await _seed_cwl_war(db, "#MARINES2", [("#KILLER", "Killer")])

    event_id = db.create_cwl_event_sync(str(guild_id), "2026-08", "creator")
    db.set_cwl_event_clans_sync(event_id, [
        {"clan_tag": "#MARINES1", "participating": True},
        {"clan_tag": "#MARINES2", "participating": True},
    ])
    monkeypatch.setattr(CACHE, "send_user_dm_detailed", AsyncMock(return_value=(True, "sent")))

    summary = await start_cwl_enrollment(guild_id, "2026-08")

    assert summary["assigned"] == 1
    assignments = db.get_cwl_assignments_sync(event_id)
    assert len(assignments) == 1
    assert assignments[0]["player_tag"] == "#KILLER"
    assert assignments[0]["assigned_clan_tag"] == "#MARINES2"  # real in-family history wins


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

    monkeypatch.setattr(CACHE, "send_user_dm_detailed", AsyncMock(return_value=(True, "sent")))

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

    monkeypatch.setattr(CACHE, "send_user_dm_detailed", AsyncMock(return_value=(True, "sent")))

    summary = await start_cwl_enrollment(1012, "2026-08")

    assert summary["seeded"] == 1  # still seeded as a signup candidate
    assert summary["assigned"] == 0  # just not auto-assigned to any clan


@pytest.mark.asyncio
async def test_account_wide_expansion_off_by_default(db, monkeypatch):
    """guild_config.cwl_enrollment_include_all_linked_accounts defaults False — an account's
    out-of-family player must NOT be pulled in unless the guild opts in (2026-08-15)."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    await _seed_guild_and_clan(db, "1013")
    monkeypatch.setattr(CACHE, "db_manager", db)
    # d1 has one player in the participating clan (#CLAN1) and one in a clan this guild's own
    # family never included (Qaplop/Marines+QCrew scenario) — still needs a `clans` row
    # (user_players.current_clan_tag FK), just not in guild "1013"'s member_clans.
    await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES ('#QCREW', 'QCrew')")
    await db.conn.commit()
    await _seed_current_clan_member(db, "d1", "#P1", clan_tag="#CLAN1")
    await _seed_current_clan_member(db, "d1", "#P2", clan_tag="#QCREW")
    await _make_event(db, "1013", "2026-08")

    monkeypatch.setattr(CACHE, "send_user_dm_detailed", AsyncMock(return_value=(True, "sent")))

    summary = await start_cwl_enrollment(1013, "2026-08")

    assert summary["seeded"] == 1  # only #P1 — #P2 stays invisible to this event
    event_id = db.get_cwl_event_sync("1013", "2026-08")["id"]
    assert db.get_cwl_signup_sync(event_id, "#P2") is None


@pytest.mark.asyncio
async def test_account_wide_expansion_pulls_in_other_clan_players_when_enabled(db, monkeypatch):
    """With the toggle on, d1's #P2 (an out-of-family clan player) is seeded alongside #P1 —
    the Marines/QCrew scenario from the project owner's own account."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    await _seed_guild_and_clan(db, "1014")
    monkeypatch.setattr(CACHE, "db_manager", db)
    CACHE.server_config["1014"]["cwl_enrollment_include_all_linked_accounts"] = True
    await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES ('#QCREW', 'QCrew')")
    await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES ('#OTHER', 'Other')")
    await db.conn.commit()
    await _seed_current_clan_member(db, "d1", "#P1", clan_tag="#CLAN1")
    await _seed_current_clan_member(db, "d1", "#P2", clan_tag="#QCREW")
    # d2 has no player in the participating clan at all — must not qualify d2's own other
    # players just because d2 happens to exist; only accounts already resolved as
    # participants (via an in-family player) get expanded.
    await _seed_current_clan_member(db, "d2", "#P3", clan_tag="#OTHER")
    await _make_event(db, "1014", "2026-08")

    monkeypatch.setattr(CACHE, "send_user_dm_detailed", AsyncMock(return_value=(True, "sent")))

    summary = await start_cwl_enrollment(1014, "2026-08")

    assert summary["seeded"] == 2  # #P1 (in-family) + #P2 (expanded via d1's account)
    event_id = db.get_cwl_event_sync("1014", "2026-08")["id"]
    assert db.get_cwl_signup_sync(event_id, "#P1") is not None
    assert db.get_cwl_signup_sync(event_id, "#P2") is not None
    assert db.get_cwl_signup_sync(event_id, "#P3") is None  # d2 never qualified


@pytest.mark.asyncio
async def test_start_enrollment_detects_and_reports_shared_clan(db, monkeypatch):
    """2026-08-15 (cross-guild shared CWL clans): Start Enrollment is the second of the two
    trigger points — a clan this guild has configured as participating might already be claimed
    by another guild for the same season. summary['shared_clans'] must report it so the caller
    (ui_cwl_roster.py) can notify."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    await _seed_guild_and_clan(db, "1015", clan_tag="#CLAN1")
    monkeypatch.setattr(CACHE, "db_manager", db)

    # Guild 9999 already has #CLAN1 as a participating clan for the same season.
    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES ('9999')")
    await db.conn.commit()
    other_event_id = db.create_cwl_event_sync("9999", "2026-08", "otherdiscordid")
    db.set_cwl_event_clans_sync(other_event_id, [{"clan_tag": "#CLAN1", "participating": True}])

    await _make_event(db, "1015", "2026-08")
    monkeypatch.setattr(CACHE, "send_user_dm_detailed", AsyncMock(return_value=(True, "sent")))

    summary = await start_cwl_enrollment(1015, "2026-08")

    assert summary["ok"] is True
    assert len(summary["shared_clans"]) == 1
    assert summary["shared_clans"][0]["clan_tag"] == "#CLAN1"
    assert summary["shared_clans"][0]["is_new"] is True
    assert summary["shared_clans"][0]["other_guild_ids"] == ["9999"]

    shared = db.get_cwl_shared_clan_sync("#CLAN1", "2026-08")
    assert shared is not None
    guild_ids = {g["guild_id"] for g in db.list_cwl_shared_clan_guilds_sync(shared["id"])}
    assert guild_ids == {"1015", "9999"}


@pytest.mark.asyncio
async def test_start_enrollment_never_double_books_a_confirmed_shared_clan_guest(db, monkeypatch):
    """Confirmed live-testing bug (2026-08-16, project owner's spec, verbatim): "I deleted The
    QCrew's saison and created it new. QManiac was auto-assigned to The QCrew's player roster
    although QManiac was already assigned to StayCalm's roster as a guest player. This race
    condition should be checked during creation of the season and auto-assignment." Deleting and
    recreating an event re-runs this exact auto-assign seed from scratch — a player who is
    already a deliberately admin_override-confirmed guest on a DIFFERENT (shared) clan's roster
    must never get silently reassigned into this event's own auto-assign target, purely because
    that target happens to be their last real CWL attack destination. The general
    assign_cwl_player_sync (QBdiscocmdshelper_cwl.py) now gates every auto-assign write on
    deliberate=False, which never evicts the existing claim — it mirrors the player into a local
    assignment pointing at their REAL shared clan instead of the auto-assign target."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    await _seed_guild_and_clan(db, "1020", clan_tag="#CLAN1")  # "The QCrew"
    monkeypatch.setattr(CACHE, "db_manager", db)
    await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES ('#SHAREDCLAN', 'StayCalm')")
    await db.conn.commit()

    event_id = db.create_cwl_event_sync("1020", "2026-08", "creator")
    db.set_cwl_event_clans_sync(event_id, [
        {"clan_tag": "#CLAN1", "participating": True},
        {"clan_tag": "#SHAREDCLAN", "participating": True},
    ])
    shared_id = db.create_cwl_shared_clan_sync("#SHAREDCLAN", "2026-08", "1020", event_id, "unresolved_first_claimer")
    db.add_guild_to_shared_clan_sync(shared_id, "1020", event_id)
    db.set_cwl_shared_clan_player_assignment_sync(shared_id, "#QMANIAC", "QManiac", "d1", True, "admin_override", "1020")

    # #QMANIAC's last real CWL attack was for #CLAN1 — exactly the signal that would normally
    # auto-assign them there, if not for already being a deliberately-placed guest elsewhere.
    await _seed_current_clan_member(db, "d1", "#QMANIAC", "#CLAN1")
    await _seed_cwl_war(db, "#CLAN1", [("#QMANIAC", "QManiac")])
    monkeypatch.setattr(CACHE, "send_user_dm_detailed", AsyncMock(return_value=(True, "sent")))

    summary = await start_cwl_enrollment(1020, "2026-08")

    assert summary["ok"] is True
    assignments = {a["player_tag"]: a["assigned_clan_tag"] for a in db.get_cwl_assignments_sync(event_id)}
    # Never double-booked into #CLAN1 — any local mirror row, if present, points at their REAL
    # shared clan instead (here #SHAREDCLAN is itself a currently-participating column, so the
    # shared-table merge in _build_enrollment_payload takes priority over this local mirror
    # regardless — see test_start_enrollment_shows_confirmed_shared_guest_as_orphaned_when_shared_clan_not_participating
    # for the case where that merge doesn't apply and the mirror is what actually surfaces them).
    assert assignments.get("#QMANIAC") != "#CLAN1"

    shared_players = {p["player_tag"]: p for p in db.get_cwl_shared_clan_players_sync(shared_id)}
    assert shared_players["#QMANIAC"]["assigned"] == 1  # untouched, still placed there
    assert shared_players["#QMANIAC"]["source"] == "admin_override"


@pytest.mark.asyncio
async def test_start_enrollment_shows_confirmed_shared_guest_as_orphaned_when_shared_clan_not_participating(db, monkeypatch):
    """2026-08-16 follow-up, live-testing feedback, project owner's spec, verbatim: "QManiac still
    assigned to StayCalm. So during auto-assignment this should have been recognized and instead
    of putting QManiac to the unassigned pool he should have been assigned to the 'Assigned to
    other clan' pool." Unlike the sibling test above, #SHAREDCLAN here is NOT a participating
    clan of this event at all (the guild deleted/recreated its season without StayCalm on the
    roster) — nothing in _build_enrollment_payload's shared-table merge would ever resolve
    #QMANIAC's real placement on its own, so assign_cwl_player_sync's local mirror is the ONLY
    thing standing between them and silently landing in plain Unassigned."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    await _seed_guild_and_clan(db, "1021", clan_tag="#CLAN1")  # "The QCrew"
    monkeypatch.setattr(CACHE, "db_manager", db)
    await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES ('#SHAREDCLAN', 'StayCalm')")
    await db.conn.commit()

    # #SHAREDCLAN already exists as a shared clan (from an earlier season/round) but this NEW
    # event only has #CLAN1 participating — StayCalm isn't on this event's roster at all.
    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES ('1020a')")
    await db.conn.commit()
    other_event_id = db.create_cwl_event_sync("1020a", "2026-08", "other-creator")
    shared_id = db.create_cwl_shared_clan_sync("#SHAREDCLAN", "2026-08", "1020a", other_event_id, "unresolved_first_claimer")
    db.set_cwl_shared_clan_player_assignment_sync(shared_id, "#QMANIAC", "QManiac", "d1", True, "admin_override", "1020a")

    event_id = db.create_cwl_event_sync("1021", "2026-08", "creator")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])

    await _seed_current_clan_member(db, "d1", "#QMANIAC", "#CLAN1")
    await _seed_cwl_war(db, "#CLAN1", [("#QMANIAC", "QManiac")])
    monkeypatch.setattr(CACHE, "send_user_dm_detailed", AsyncMock(return_value=(True, "sent")))

    summary = await start_cwl_enrollment(1021, "2026-08")

    assert summary["ok"] is True
    assignments = {a["player_tag"]: a["assigned_clan_tag"] for a in db.get_cwl_assignments_sync(event_id)}
    assert assignments.get("#QMANIAC") == "#SHAREDCLAN"  # surfaces via "Assigned to other Guild", not bare Unassigned

    shared_players = {p["player_tag"]: p for p in db.get_cwl_shared_clan_players_sync(shared_id)}
    assert shared_players["#QMANIAC"]["assigned"] == 1  # untouched, still placed there


@pytest.mark.asyncio
async def test_start_enrollment_current_clan_beats_a_stale_non_deliberate_shared_placement(db, monkeypatch):
    """The actual live root cause (2026-08-19, project owner, verbatim: "the qcrew members were
    falsely auto-assigned to staycalm... theqcrew members get auto assigned to staycalm and not
    to the qcrew as they should" — traced through two layers: start_cwl_enrollment's own auto-
    assign already correctly redirects a genuine current family-clan member's target to their own
    real clan (see the sibling test on resolve_prior_cwl_assignments' call site), but
    assign_cwl_player_sync's conflict guard was silently discarding that corrected target right
    back to a stale cwl_shared_clan_players row from an EARLIER, unrelated season/event cycle —
    UNLIKE the sibling tests above, that stale row is NOT admin_override (a real deliberate
    placement, which must always still win regardless — see test_start_enrollment_never_double_
    books_a_confirmed_shared_clan_guest), it's just a leftover 'auto_assigned' guess with nothing
    deliberate behind it, and the player's own live current clan flatly contradicts it."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    await _seed_guild_and_clan(db, "1022", clan_tag="#CLAN1")  # "The QCrew"
    monkeypatch.setattr(CACHE, "db_manager", db)
    await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES ('#SHAREDCLAN', 'StayCalm')")
    await db.conn.commit()

    event_id = db.create_cwl_event_sync("1022", "2026-08", "creator")
    db.set_cwl_event_clans_sync(event_id, [
        {"clan_tag": "#CLAN1", "participating": True},
        {"clan_tag": "#SHAREDCLAN", "participating": True},
    ])
    shared_id = db.create_cwl_shared_clan_sync("#SHAREDCLAN", "2026-08", "1022", event_id, "unresolved_first_claimer")
    db.add_guild_to_shared_clan_sync(shared_id, "1022", event_id)
    # Stale — a passive auto-assign guess from an earlier cycle (NOT admin_override), left behind
    # even though this player is now a genuine current member of #CLAN1.
    db.set_cwl_shared_clan_player_assignment_sync(shared_id, "#QMANIAC", "QManiac", "d1", True, "auto_assigned", "1022")

    await _seed_current_clan_member(db, "d1", "#QMANIAC", "#CLAN1")
    await _seed_cwl_war(db, "#SHAREDCLAN", [("#QMANIAC", "QManiac")])  # stale history too
    monkeypatch.setattr(CACHE, "send_user_dm_detailed", AsyncMock(return_value=(True, "sent")))

    summary = await start_cwl_enrollment(1022, "2026-08")

    assert summary["ok"] is True
    assignments = {a["player_tag"]: a["assigned_clan_tag"] for a in db.get_cwl_assignments_sync(event_id)}
    assert assignments.get("#QMANIAC") == "#CLAN1"  # their own real current clan wins
    shared_players = {p["player_tag"]: p for p in db.get_cwl_shared_clan_players_sync(shared_id)}
    assert "#QMANIAC" not in shared_players  # stale placement purged, not left dangling


@pytest.mark.asyncio
async def test_start_enrollment_never_assigns_a_player_into_an_unrelated_shared_clan(db, monkeypatch):
    """2026-08-19 review finding — the real, mass-scale cause of "the qcrew members were falsely
    auto-assigned to staycalm" (project owner's live report: a guild's OWN members piled into a
    guest clan's column en masse, 26 in one screenshot).

    assign_cwl_player_sync's `deliberate=False` guard was deciding "this player already has a real
    claim elsewhere" from `shared_clan_ids_to_clear` — a set that, by construction, contains EVERY
    participating shared clan whether or not the player is in it (it exists to drive the eviction
    sweep, where a miss is a harmless no-op delete). So with any shared guest clan on the roster,
    every auto-assigned player whose live current clan didn't happen to equal their auto-assign
    target got an `orphaned_elsewhere` assignment written pointing at that guest clan — with zero
    relationship to it and no row on its roster at all.

    Here #LONER is a current member of a family clan that isn't participating, with real CWL
    history for the guild's own participating #CLAN1, and nothing whatsoever to do with
    #SHAREDCLAN."""
    from qapbot import config as config_module
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    monkeypatch.setattr(
        config_module, "CONFIG",
        dataclasses.replace(config_module.CONFIG, is_dev_mode=False, cwl_dm_restrict_to_admin=False),
    )
    monkeypatch.setattr(CACHE, "db_manager", db)

    gid = "1023"
    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (gid,))
    for tag, name in [("#CLAN1", "QCrew"), ("#CLAN2", "QCrew2"), ("#SHAREDCLAN", "StayCalm")]:
        await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES (?, ?)", (tag, name))
    await db.conn.commit()
    CACHE.server_config[gid] = {"member_clans": ["#CLAN1", "#CLAN2"], "member_families": []}
    CACHE.clan_families = {}

    event_id = db.create_cwl_event_sync(gid, "2026-08", "creator")
    db.set_cwl_event_clans_sync(event_id, [
        {"clan_tag": "#CLAN1", "participating": True},
        {"clan_tag": "#SHAREDCLAN", "participating": True},
        {"clan_tag": "#CLAN2", "participating": False},
    ])
    shared_id = db.create_cwl_shared_clan_sync("#SHAREDCLAN", "2026-08", gid, event_id, "unresolved_first_claimer")
    db.add_guild_to_shared_clan_sync(shared_id, gid, event_id)

    await _seed_current_clan_member(db, "d1", "#LONER", "#CLAN2")
    await _seed_cwl_war(db, "#CLAN1", [("#LONER", "Loner")])
    monkeypatch.setattr(CACHE, "send_user_dm_detailed", AsyncMock(return_value=(True, "sent")))

    await start_cwl_enrollment(int(gid), "2026-08")

    assignments = {a["player_tag"]: a["assigned_clan_tag"] for a in db.get_cwl_assignments_sync(event_id)}
    assert assignments.get("#LONER") == "#CLAN1"  # their real CWL history, not the guest clan
    assert "#LONER" not in {p["player_tag"] for p in db.get_cwl_shared_clan_players_sync(shared_id)}


@pytest.mark.asyncio
async def test_start_enrollment_shared_clans_empty_when_nothing_shared(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

    await _seed_guild_and_clan(db, "1016", clan_tag="#CLAN1")
    monkeypatch.setattr(CACHE, "db_manager", db)
    await _make_event(db, "1016", "2026-08")
    monkeypatch.setattr(CACHE, "send_user_dm_detailed", AsyncMock(return_value=(True, "sent")))

    summary = await start_cwl_enrollment(1016, "2026-08")

    assert summary["shared_clans"] == []
