"""cwl_signups is an enrollment-time SNAPSHOT, not live state (2026-08-22 — Pitfall 37).

Start Enrollment writes cwl_signups once and nothing ever refreshes it, so its `discord_id` can
name a Discord user who no longer owns the account — or be NULL for an account that has been
linked since. Every read path has to re-resolve against user_players, which is the only authority
for "who owns this account now".

These cover the DM-targeting path (resolve_cwl_pool_dm_targets_sync). The board-payload path is
covered in tests/discord/test_web_bridge.py; the DM-button ownership guard in
tests/discord/test_ui_cwl_roster.py.
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


async def _seed(db: WarHistoryDB, guild_id: str, clan_tag: str) -> int:
    await db._conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
    await db._conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES (?, ?)", (clan_tag, "Test Clan"))
    await db._conn.commit()
    event_id = db.create_cwl_event_sync(guild_id, "2026-09", "admin1")
    assert event_id is not None
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": clan_tag, "participating": True}])
    return event_id


async def _link(
    db: WarHistoryDB, discord_id: str, player_tag: str, clan_tag=None,
    cwl_permanent_optout: bool = False, cwl_permanent_optin: bool = False,
) -> None:
    await db._conn.execute(
        "INSERT OR IGNORE INTO users (discord_id, display_name) VALUES (?, ?)", (discord_id, discord_id)
    )
    await db._conn.execute(
        "INSERT INTO user_players "
        "(discord_id, player_tag, player_name, verified, current_clan_tag, "
        " cwl_permanent_optout, cwl_permanent_optin) "
        "VALUES (?, ?, ?, 0, ?, ?, ?)",
        (
            discord_id, player_tag, "Player", clan_tag,
            1 if cwl_permanent_optout else 0, 1 if cwl_permanent_optin else 0,
        ),
    )
    await db._conn.commit()


class TestNewSignupsAdoptTheGlobalResponse:
    """cwl_player_season_status is the global source of truth for whether a player has already
    answered (rule h). start_cwl_enrollment seeded new local rows from it; four other creation
    paths hardcoded 'pending' and would have contradicted a response the player already gave to
    a different guild. Verified latent at fix time (0 drifted rows live) but fully reachable —
    31 of 116 global rows already held a real response."""

    @pytest.mark.asyncio
    async def test_seed_helper_adopts_an_existing_global_response(self, db):
        import qapbot.QBdiscocmdshelper_cwl as cwl

        db.set_cwl_player_response_status_sync(
            "#ANSWERED", "2026-09", "Answered", "owner1", "confirmed", "2026-09-01T10:00Z", 1, "900",
        )
        assert cwl._seed_status_from_global_sync(db, "#ANSWERED", "2026-09") == "confirmed"

    @pytest.mark.asyncio
    async def test_seed_helper_defaults_to_pending_without_a_global_row(self, db):
        import qapbot.QBdiscocmdshelper_cwl as cwl

        assert cwl._seed_status_from_global_sync(db, "#NEVERSEEN", "2026-09") == "pending"

    @pytest.mark.asyncio
    async def test_seed_helper_is_season_scoped(self, db):
        """A response belongs to one season — last season's answer must not leak into this one."""
        import qapbot.QBdiscocmdshelper_cwl as cwl

        db.set_cwl_player_response_status_sync(
            "#LASTSEASON", "2026-08", "Old", "owner1", "declined", "2026-08-01T10:00Z", 1, "900",
        )
        assert cwl._seed_status_from_global_sync(db, "#LASTSEASON", "2026-09") == "pending"

    @pytest.mark.asyncio
    async def test_seed_helper_honours_permanent_optout(self, db):
        """2026-08-23 follow-up audit (plans/cwl-personal-hub.md Phase 4b-bis): this helper used
        to ignore the standing CWL opt-out/opt-in preference entirely, so a drag-and-drop
        placement or guest invite for a permanently-opted-out player produced a stray 'pending'
        row that never became 'declined' — silently contradicting the very preference this
        feature exists to honour. No existing_global response here, so opt-out alone decides."""
        import qapbot.QBdiscocmdshelper_cwl as cwl

        await _link(db, "owner1", "#OPTEDOUT", cwl_permanent_optout=True)
        assert cwl._seed_status_from_global_sync(db, "#OPTEDOUT", "2026-09") == "declined"

    @pytest.mark.asyncio
    async def test_seed_helper_honours_permanent_optin(self, db):
        import qapbot.QBdiscocmdshelper_cwl as cwl

        await _link(db, "owner1", "#OPTEDIN", cwl_permanent_optin=True)
        assert cwl._seed_status_from_global_sync(db, "#OPTEDIN", "2026-09") == "auto_confirmed"

    @pytest.mark.asyncio
    async def test_seed_helper_existing_global_response_beats_optin(self, db):
        """rule h still wins — a real answer already given for this season is never overridden by
        a standing preference, even via this single-player helper."""
        import qapbot.QBdiscocmdshelper_cwl as cwl

        await _link(db, "owner1", "#BOTH", cwl_permanent_optin=True)
        db.set_cwl_player_response_status_sync(
            "#BOTH", "2026-09", "Both", "owner1", "declined", "2026-09-01T10:00Z", 1, "900",
        )
        assert cwl._seed_status_from_global_sync(db, "#BOTH", "2026-09") == "declined"

    @pytest.mark.asyncio
    async def test_seed_helper_no_user_players_row_defaults_to_pending(self, db):
        """A player_tag with no linked account at all (never in user_players) has no preference
        to honour — same 'pending' default as before this feature existed."""
        import qapbot.QBdiscocmdshelper_cwl as cwl

        assert cwl._seed_status_from_global_sync(db, "#NEVERLINKED", "2026-09") == "pending"

    @pytest.mark.asyncio
    async def test_placement_of_an_opted_out_player_creates_a_declined_signup(self, db, monkeypatch):
        """End-to-end through assign_cwl_player_sync's real drag-and-drop path (not just the
        helper in isolation) — an admin placing a permanently-opted-out player must not leave
        them showing as 'pending' on the board."""
        from qapbot.cache_manager import CACHE
        import qapbot.QBdiscocmdshelper_cwl as cwl

        event_id = await _seed(db, "911", "#CLANI")
        CACHE.db_manager = db
        monkeypatch.setattr(cwl, "resolve_guild_member_clan_tags", lambda *a, **k: ["#CLANI"])
        await _link(db, "owner1", "#OPTEDOUTPLACED", cwl_permanent_optout=True)

        cwl.assign_cwl_player_sync(911, event_id, "2026-09", "#OPTEDOUTPLACED", "#CLANI", source="admin")

        assert db.get_cwl_signup_sync(event_id, "#OPTEDOUTPLACED")["status"] == "declined"

    @pytest.mark.asyncio
    async def test_placement_creates_a_signup_carrying_the_global_response(self, db, monkeypatch):
        """assign_cwl_player_sync's placement path — the drag-and-drop route. A player already
        'declined' globally must not reappear as 'pending' in the guild that just placed them."""
        from qapbot.cache_manager import CACHE
        import qapbot.QBdiscocmdshelper_cwl as cwl

        event_id = await _seed(db, "907", "#CLANG")
        CACHE.db_manager = db
        monkeypatch.setattr(cwl, "resolve_guild_member_clan_tags", lambda *a, **k: ["#CLANG"])
        db.set_cwl_player_response_status_sync(
            "#PLACED", "2026-09", "Placed", "owner1", "declined", "2026-09-01T10:00Z", 99, "908",
        )

        cwl.assign_cwl_player_sync(907, event_id, "2026-09", "#PLACED", "#CLANG", source="admin")

        assert db.get_cwl_signup_sync(event_id, "#PLACED")["status"] == "declined"

    @pytest.mark.asyncio
    async def test_placement_does_not_overwrite_an_existing_local_row(self, db, monkeypatch):
        """The seeding is create-only. All four call sites sit inside
        `if get_cwl_signup_sync(...) is None` and that must stay true, or a placement would
        silently rewrite a status the local guild had already recorded."""
        from qapbot.cache_manager import CACHE
        import qapbot.QBdiscocmdshelper_cwl as cwl

        event_id = await _seed(db, "909", "#CLANH")
        CACHE.db_manager = db
        monkeypatch.setattr(cwl, "resolve_guild_member_clan_tags", lambda *a, **k: ["#CLANH"])
        db.upsert_cwl_signup_sync(event_id, "#KEEP", "Keep", "owner1", None, "template_confirm", "confirmed")
        db.set_cwl_player_response_status_sync(
            "#KEEP", "2026-09", "Keep", "owner1", "declined", "2026-09-01T10:00Z", 99, "910",
        )

        cwl.assign_cwl_player_sync(909, event_id, "2026-09", "#KEEP", "#CLANH", source="admin")

        assert db.get_cwl_signup_sync(event_id, "#KEEP")["status"] == "confirmed"


class TestCarryForwardWritesUseLiveOwner:
    """Every READ path re-resolves ownership live, so a stale snapshot value can no longer
    mis-route a DM or grey out a tile. But the write paths that COPY one snapshot into the other
    (a clan becoming shared, a placement) were still laundering an outdated owner into a second
    table, where the next feature to read that column would naturally trust it. Resolving at the
    write boundary stops the stale value spreading — the same self-healing the DM button does."""

    @pytest.mark.asyncio
    async def test_migrating_a_local_roster_to_shared_writes_the_live_owner(self, db):
        import qapbot.QBdiscocmdshelper_cwl as cwl

        event_id = await _seed(db, "905", "#CLANE")
        db.upsert_cwl_signup_sync(event_id, "#MIG", "Mig", "staleowner", None, "template_confirm", "confirmed")
        db.upsert_cwl_assignment_sync(event_id, "#MIG", "#CLANE")
        await _link(db, "trueowner", "#MIG", clan_tag="#CLANE")

        shared_clan_id = db.create_cwl_shared_clan_sync("#CLANE", "2026-09", "905", event_id, "test")
        cwl._migrate_local_clan_roster_to_shared(db, event_id, shared_clan_id, "#CLANE", "905")

        rows = {p["player_tag"]: p for p in db.get_cwl_shared_clan_players_sync(shared_clan_id)}
        assert rows["#MIG"]["dmed_discord_id"] == "trueowner"

    @pytest.mark.asyncio
    async def test_migration_keeps_the_snapshot_owner_for_a_never_linked_tag(self, db):
        """A guest tag added by search has no user_players row at all — there is nothing live to
        resolve, so the recorded owner (who really was DMed) must survive rather than be blanked."""
        import qapbot.QBdiscocmdshelper_cwl as cwl

        event_id = await _seed(db, "906", "#CLANF")
        db.upsert_cwl_signup_sync(event_id, "#GUESTX", "GuestX", "guestowner", None, "guest_invite", "pending")
        db.upsert_cwl_assignment_sync(event_id, "#GUESTX", "#CLANF")

        shared_clan_id = db.create_cwl_shared_clan_sync("#CLANF", "2026-09", "906", event_id, "test")
        cwl._migrate_local_clan_roster_to_shared(db, event_id, shared_clan_id, "#CLANF", "906")

        rows = {p["player_tag"]: p for p in db.get_cwl_shared_clan_players_sync(shared_clan_id)}
        assert rows["#GUESTX"]["dmed_discord_id"] == "guestowner"

    @pytest.mark.asyncio
    async def test_live_owners_helper_omits_never_linked_tags(self, db):
        import qapbot.QBdiscocmdshelper_cwl as cwl

        await _link(db, "someone", "#KNOWN1", clan_tag=None)
        result = cwl._live_owners_or_sync(db, ["#KNOWN1", "#UNKN0WN"])

        assert result == {"#KNOWN1": "someone"}

    @pytest.mark.asyncio
    async def test_live_owners_helper_is_empty_for_no_tags(self, db):
        import qapbot.QBdiscocmdshelper_cwl as cwl

        assert cwl._live_owners_or_sync(db, []) == {}


class TestDmTargetingUsesLiveOwner:
    """resolve_cwl_pool_dm_targets_sync merged its four sources with first-non-None-wins
    (`entry["discord_id"] = entry["discord_id"] or discord_id`), and the live user_players
    source ran LAST — so it could fill a missing link but never CORRECT a stale one."""

    @pytest.mark.asyncio
    async def test_stale_signup_owner_is_overridden_by_live_link(self, db, monkeypatch):
        """The live #29JQV2YCL case: a pooled player with NO current_clan_tag (so the clan-scoped
        `members` source structurally can't return them) whose signup row names a Discord user who
        no longer owns the account. Before the fix this DMed the wrong person."""
        from qapbot.cache_manager import CACHE
        import qapbot.QBdiscocmdshelper_cwl as cwl

        event_id = await _seed(db, "901", "#CLANA")
        CACHE.db_manager = db
        monkeypatch.setattr(cwl, "resolve_cwl_pool_clan_tags_sync", lambda *a, **k: ["#CLANA"])

        db.upsert_cwl_signup_sync(event_id, "#GHOUL", "OneEyeGhoul", "oldowner", None, "template_confirm", "pending")
        await _link(db, "newowner", "#GHOUL", clan_tag=None)  # clanless — `members` misses it

        result = cwl.resolve_cwl_pool_dm_targets_sync(901, event_id, "2026-09", preloaded_members=[])

        targets = {t["player_tag"]: t["discord_id"] for t in result["targets"]}
        assert targets["#GHOUL"] == "newowner"

    @pytest.mark.asyncio
    async def test_signup_owner_for_now_unlinked_account_is_cleared(self, db, monkeypatch):
        """The other direction: the account has been returned to the UNASSIGNED pool, so nobody
        owns it and nobody should be DMed about it. get_player_links_sync maps 'UNASSIGNED' to
        None, and the authoritative override has to apply that None rather than treat it as
        "no information" and keep the stale owner."""
        from qapbot.cache_manager import CACHE
        import qapbot.QBdiscocmdshelper_cwl as cwl

        event_id = await _seed(db, "902", "#CLANB")
        CACHE.db_manager = db
        monkeypatch.setattr(cwl, "resolve_cwl_pool_clan_tags_sync", lambda *a, **k: ["#CLANB"])

        db.upsert_cwl_signup_sync(event_id, "#GONE", "Gone", "exowner", None, "template_confirm", "pending")
        await _link(db, "UNASSIGNED", "#GONE", clan_tag=None)

        result = cwl.resolve_cwl_pool_dm_targets_sync(902, event_id, "2026-09", preloaded_members=[])

        assert [t["player_tag"] for t in result["targets"]] == []
        assert result["skipped_unlinked"] == 1

    @pytest.mark.asyncio
    async def test_live_member_source_still_wins_over_the_snapshot(self, db, monkeypatch):
        """Regression guard on the fix itself: the bug was deliberately NOT fixed by reordering
        the sources, because `members` (source 1) is also live and must keep winning over the
        snapshot. A player present in BOTH must resolve to the member row's owner."""
        from qapbot.cache_manager import CACHE
        import qapbot.QBdiscocmdshelper_cwl as cwl

        event_id = await _seed(db, "903", "#CLANC")
        CACHE.db_manager = db
        monkeypatch.setattr(cwl, "resolve_cwl_pool_clan_tags_sync", lambda *a, **k: ["#CLANC"])

        db.upsert_cwl_signup_sync(event_id, "#BOTH", "Both", "stale", None, "template_confirm", "pending")
        members = [{
            "player_tag": "#BOTH", "player_name": "Both", "clan_tag": "#CLANC",
            "discord_id": "liveowner", "verified": False, "cwl_permanent_optout": False,
            "preferred_league_rank": None, "th_level": 15,
            "cwl_permanent_optin": False, "cwl_optout_send_dm_anyway": False,
        }]

        result = cwl.resolve_cwl_pool_dm_targets_sync(903, event_id, "2026-09", preloaded_members=members)

        targets = {t["player_tag"]: t["discord_id"] for t in result["targets"]}
        assert targets["#BOTH"] == "liveowner"

    @pytest.mark.asyncio
    async def test_never_linked_guest_tag_keeps_its_signup_discord_id(self, db, monkeypatch):
        """A guest tag added by search that was never linked has no user_players row at all, so
        get_player_links_sync returns nothing for it — the snapshot value is all we have and must
        survive, not be blanked by an over-eager override."""
        from qapbot.cache_manager import CACHE
        import qapbot.QBdiscocmdshelper_cwl as cwl

        event_id = await _seed(db, "904", "#CLAND")
        CACHE.db_manager = db
        monkeypatch.setattr(cwl, "resolve_cwl_pool_clan_tags_sync", lambda *a, **k: ["#CLAND"])

        db.upsert_cwl_signup_sync(event_id, "#GUEST", "Guest", "guestowner", None, "guest_invite", "pending")

        result = cwl.resolve_cwl_pool_dm_targets_sync(904, event_id, "2026-09", preloaded_members=[])

        targets = {t["player_tag"]: t["discord_id"] for t in result["targets"]}
        assert targets["#GUEST"] == "guestowner"


class TestDmTargetingHonoursCwlPreferences:
    """plans/cwl-personal-hub.md Phase 4c: resolve_cwl_pool_dm_targets_sync's opt-out handling
    gains a second flag (cwl_optout_send_dm_anyway) and a second output list (optout_no_dm), on
    top of its pre-existing cwl_permanent_optout skip. Covers both pool sources that can carry
    the flags — the live `members` scan (source 1) and the get_player_links_sync fallback for
    tags `members` structurally can't see (guest/shared-clan sources 2/3)."""

    @pytest.mark.asyncio
    async def test_optout_without_dm_anyway_is_skipped_and_recorded_in_optout_no_dm(self, db, monkeypatch):
        from qapbot.cache_manager import CACHE
        import qapbot.QBdiscocmdshelper_cwl as cwl

        event_id = await _seed(db, "905", "#CLANE")
        CACHE.db_manager = db
        monkeypatch.setattr(cwl, "resolve_cwl_pool_clan_tags_sync", lambda *a, **k: ["#CLANE"])
        members = [{
            "player_tag": "#OUT", "player_name": "Out", "clan_tag": "#CLANE",
            "discord_id": "d1", "verified": True, "cwl_permanent_optout": True,
            "preferred_league_rank": None, "th_level": None,
            "cwl_permanent_optin": False, "cwl_optout_send_dm_anyway": False,
        }]

        result = cwl.resolve_cwl_pool_dm_targets_sync(905, event_id, "2026-09", preloaded_members=members)

        assert [t["player_tag"] for t in result["targets"]] == []
        assert result["skipped_optout"] == 1
        assert [e["player_tag"] for e in result["optout_no_dm"]] == ["#OUT"]

    @pytest.mark.asyncio
    async def test_optout_with_dm_anyway_is_still_a_dm_target(self, db, monkeypatch):
        from qapbot.cache_manager import CACHE
        import qapbot.QBdiscocmdshelper_cwl as cwl

        event_id = await _seed(db, "906", "#CLANF")
        CACHE.db_manager = db
        monkeypatch.setattr(cwl, "resolve_cwl_pool_clan_tags_sync", lambda *a, **k: ["#CLANF"])
        members = [{
            "player_tag": "#OUTBUTDM", "player_name": "OutButDM", "clan_tag": "#CLANF",
            "discord_id": "d1", "verified": True, "cwl_permanent_optout": True,
            "preferred_league_rank": None, "th_level": None,
            "cwl_permanent_optin": False, "cwl_optout_send_dm_anyway": True,
        }]

        result = cwl.resolve_cwl_pool_dm_targets_sync(906, event_id, "2026-09", preloaded_members=members)

        assert [t["player_tag"] for t in result["targets"]] == ["#OUTBUTDM"]
        assert result["skipped_optout"] == 0
        # Still recorded — a 'declined' row is owed regardless of whether the DM goes out.
        assert [e["player_tag"] for e in result["optout_no_dm"]] == ["#OUTBUTDM"]

    @pytest.mark.asyncio
    async def test_optin_member_is_a_plain_dm_target_no_special_handling(self, db, monkeypatch):
        from qapbot.cache_manager import CACHE
        import qapbot.QBdiscocmdshelper_cwl as cwl

        event_id = await _seed(db, "913", "#CLANK")
        CACHE.db_manager = db
        monkeypatch.setattr(cwl, "resolve_cwl_pool_clan_tags_sync", lambda *a, **k: ["#CLANK"])
        members = [{
            "player_tag": "#IN", "player_name": "In", "clan_tag": "#CLANK",
            "discord_id": "d1", "verified": True, "cwl_permanent_optout": False,
            "preferred_league_rank": None, "th_level": None,
            "cwl_permanent_optin": True, "cwl_optout_send_dm_anyway": False,
        }]

        result = cwl.resolve_cwl_pool_dm_targets_sync(913, event_id, "2026-09", preloaded_members=members)

        assert [t["player_tag"] for t in result["targets"]] == ["#IN"]
        assert result["skipped_optout"] == 0
        assert result["optout_no_dm"] == []

    @pytest.mark.asyncio
    async def test_optout_reached_only_via_the_link_fallback_source_is_still_skipped(self, db, monkeypatch):
        """A guest/shared-clan entry (pool sources 2/3) has no row in `members` at all — its
        opt-out flag can only ever be resolved via the get_player_links_sync fallback merge. This
        is the exact gap Phase 4b-bis closes: without it, an opted-out guest player skipped from
        the DM would never get a cwl_signups row from ANY seed site."""
        from qapbot.cache_manager import CACHE
        import qapbot.QBdiscocmdshelper_cwl as cwl

        event_id = await _seed(db, "914", "#CLANL")
        CACHE.db_manager = db
        monkeypatch.setattr(cwl, "resolve_cwl_pool_clan_tags_sync", lambda *a, **k: ["#CLANL"])
        # Individually-invited guest: a cwl_signups row with no clan membership at all, so
        # `members` (clan-scoped) structurally never sees it — only the link-fallback merge does.
        db.upsert_cwl_signup_sync(event_id, "#GUESTOUT", "GuestOut", "d1", None, "guest_invite", "pending")
        await _link(db, "d1", "#GUESTOUT", clan_tag=None, cwl_permanent_optout=True)

        result = cwl.resolve_cwl_pool_dm_targets_sync(914, event_id, "2026-09", preloaded_members=[])

        assert [t["player_tag"] for t in result["targets"]] == []
        assert result["skipped_optout"] == 1
        assert [e["player_tag"] for e in result["optout_no_dm"]] == ["#GUESTOUT"]


class TestDmBatchSeedsSignupRows:
    """Tracker #0016: CwlSignupResponseButton resolves a click by (event_id, player_tag) and
    returns `no_longer_valid` when no cwl_signups row exists, so a DM sent without one carries a
    permanently dead button. start_cwl_enrollment seeded its rows before DMing; "Notify New Pool
    Members" did not -- and its whole job is reaching pool members added AFTER Start Enrollment,
    i.e. exactly the players with no row. 27 live DMs across 4 users were dead on arrival."""

    @staticmethod
    def _target(tag: str, name: str, discord_id: str) -> dict:
        return {"player_tag": tag, "player_name": name, "discord_id": discord_id}

    @pytest.fixture(autouse=True)
    def _allow_dms(self, monkeypatch):
        """CONFIG.cwl_dm_restrict_to_admin defaults to True on DEV (the PROD-testing guard), which
        would skip every target here before the seeding path is even reached."""
        import dataclasses

        import qapbot.config as config_module
        import qapbot.QBdiscocmdshelper_cwl as cwl

        relaxed = dataclasses.replace(
            config_module.CONFIG, is_dev_mode=False, cwl_dm_restrict_to_admin=False
        )
        monkeypatch.setattr(config_module, "CONFIG", relaxed)
        monkeypatch.setattr(cwl, "CONFIG", relaxed, raising=False)

    @pytest.mark.asyncio
    async def test_missing_signup_row_is_created_before_the_dm_goes_out(self, db, monkeypatch):
        from qapbot.cache_manager import CACHE
        import qapbot.QBdiscocmdshelper_cwl as cwl

        event_id = await _seed(db, "920", "#CLANI")
        await _link(db, "owner1", "#NEWGUY")
        CACHE.db_manager = db
        sent = []

        async def _fake_dm(evt, gid, season, participant):
            # The row its button needs must already exist at send time, not after.
            assert db.get_cwl_signup_sync(evt, participant["player_tag"]) is not None
            sent.append(participant["player_tag"])
            return True, "sent", "msg1", "chan1"

        monkeypatch.setattr(cwl, "send_cwl_signup_template_dm", _fake_dm)

        result = await cwl._send_cwl_enrollment_dm_batch(
            event_id, 920, "2026-09", [self._target("#NEWGUY", "NewGuy", "owner1")]
        )

        assert result["contacted"] == 1
        assert sent == ["#NEWGUY"]
        row = db.get_cwl_signup_sync(event_id, "#NEWGUY")
        assert row is not None
        assert row["dmed_discord_id"] == "owner1"

    @pytest.mark.asyncio
    async def test_seeded_row_adopts_the_global_response(self, db, monkeypatch):
        """Not a hardcoded 'pending' -- a player who already answered another guild's DM must
        not be contradicted (rule h)."""
        from qapbot.cache_manager import CACHE
        import qapbot.QBdiscocmdshelper_cwl as cwl

        event_id = await _seed(db, "921", "#CLANJ")
        await _link(db, "owner1", "#ANSWERED2")
        CACHE.db_manager = db
        db.set_cwl_player_response_status_sync(
            "#ANSWERED2", "2026-09", "Answered", "owner1", "confirmed", "2026-09-01T10:00Z", 99, "922",
        )

        async def _fake_dm(evt, gid, season, participant):
            return True, "sent", "m", "c"

        monkeypatch.setattr(cwl, "send_cwl_signup_template_dm", _fake_dm)
        await cwl._send_cwl_enrollment_dm_batch(
            event_id, 921, "2026-09", [self._target("#ANSWERED2", "Answered", "owner1")]
        )

        assert db.get_cwl_signup_sync(event_id, "#ANSWERED2")["status"] == "confirmed"

    @pytest.mark.asyncio
    async def test_skipped_target_gets_no_row(self, db, monkeypatch):
        """A player skipped by the global dm_sent dedup never received this DM, so they must not
        gain a board entry as a side effect of it."""
        from qapbot.cache_manager import CACHE
        import qapbot.QBdiscocmdshelper_cwl as cwl

        event_id = await _seed(db, "923", "#CLANK")
        other_event_id = await _seed(db, "924", "#CLANK2")
        CACHE.db_manager = db
        # Already DMed by ANOTHER guild's event -> the batch's dedup skips them. The event must
        # really exist: get_cwl_player_season_dm_status_bulk_sync deliberately distrusts a
        # dm_sent row whose dm_sent_via_event_id has been deleted (2026-08-19 orphan self-heal).
        db.mark_cwl_player_dm_sent_sync(
            "#ALREADY", "2026-09", "Already", "owner1", other_event_id, 924,
            "2026-09-01T10:00Z", "m", "c",
        )

        async def _fake_dm(evt, gid, season, participant):
            raise AssertionError("should not have DMed a skipped target")

        monkeypatch.setattr(cwl, "send_cwl_signup_template_dm", _fake_dm)
        result = await cwl._send_cwl_enrollment_dm_batch(
            event_id, 923, "2026-09", [self._target("#ALREADY", "Already", "owner1")]
        )

        assert result["skipped_already_dm_globally"] == 1
        assert db.get_cwl_signup_sync(event_id, "#ALREADY") is None

    @pytest.mark.asyncio
    async def test_existing_row_is_never_clobbered(self, db, monkeypatch):
        from qapbot.cache_manager import CACHE
        import qapbot.QBdiscocmdshelper_cwl as cwl

        event_id = await _seed(db, "925", "#CLANL")
        await _link(db, "owner1", "#KEEP2")
        CACHE.db_manager = db
        db.upsert_cwl_signup_sync(
            event_id, "#KEEP2", "Keep2", "owner1", None, "template_confirm", "declined",
        )

        async def _fake_dm(evt, gid, season, participant):
            return True, "sent", "m", "c"

        monkeypatch.setattr(cwl, "send_cwl_signup_template_dm", _fake_dm)
        await cwl._send_cwl_enrollment_dm_batch(
            event_id, 925, "2026-09", [self._target("#KEEP2", "Keep2", "owner1")]
        )

        assert db.get_cwl_signup_sync(event_id, "#KEEP2")["status"] == "declined"


class TestDmBatchRechecksLinkBeforeSending:
    """Live bug report, 2026-08-22: "Daniel", a player with NO linked Discord account at all,
    was still sent a signup DM (and turned up in the admin's "blocked" report). resolve_cwl_pool_
    dm_targets_sync() resolves the pool once, up front; a DM batch with many recipients then runs
    for a while (real Discord API calls, possibly retried) -- long enough for an account already
    queued to get unlinked before its own turn comes up. _send_cwl_enrollment_dm_batch must
    re-check user_players immediately before sending, not trust the resolved-pool snapshot."""

    @staticmethod
    def _target(tag: str, name: str, discord_id: str) -> dict:
        return {"player_tag": tag, "player_name": name, "discord_id": discord_id}

    @pytest.fixture(autouse=True)
    def _allow_dms(self, monkeypatch):
        import dataclasses

        import qapbot.config as config_module
        import qapbot.QBdiscocmdshelper_cwl as cwl

        relaxed = dataclasses.replace(
            config_module.CONFIG, is_dev_mode=False, cwl_dm_restrict_to_admin=False
        )
        monkeypatch.setattr(config_module, "CONFIG", relaxed)
        monkeypatch.setattr(cwl, "CONFIG", relaxed, raising=False)

    @pytest.mark.asyncio
    async def test_never_linked_account_is_skipped_not_dmed(self, db, monkeypatch):
        """No user_players row at all for this tag -- the pool resolver should never have handed
        this target over with a real discord_id in the first place, but the batch must not trust
        it blindly regardless of how it got here."""
        from qapbot.cache_manager import CACHE
        import qapbot.QBdiscocmdshelper_cwl as cwl

        event_id = await _seed(db, "926", "#CLANM")
        CACHE.db_manager = db

        async def _fake_dm(evt, gid, season, participant):
            raise AssertionError("must not DM a player with no linked Discord account")

        monkeypatch.setattr(cwl, "send_cwl_signup_template_dm", _fake_dm)
        result = await cwl._send_cwl_enrollment_dm_batch(
            event_id, 926, "2026-09", [self._target("#DANIEL", "Daniel", "999")]
        )

        assert result["contacted"] == 0
        assert result["skipped_unlinked"] == 1
        assert db.get_cwl_signup_sync(event_id, "#DANIEL") is None

    @pytest.mark.asyncio
    async def test_unlinked_after_pool_resolution_is_skipped_not_dmed(self, db, monkeypatch):
        """The exact race: linked at pool-resolution time, unlinked before the send loop reaches
        them."""
        from qapbot.cache_manager import CACHE
        import qapbot.QBdiscocmdshelper_cwl as cwl

        event_id = await _seed(db, "927", "#CLANN")
        await _link(db, "owner1", "#RACED")
        CACHE.db_manager = db
        # Simulates an admin unlinking the account between pool resolution and the send loop.
        await db._conn.execute("DELETE FROM user_players WHERE player_tag = ?", ("#RACED",))
        await db._conn.commit()

        async def _fake_dm(evt, gid, season, participant):
            raise AssertionError("must not DM an account unlinked before the send loop reached it")

        monkeypatch.setattr(cwl, "send_cwl_signup_template_dm", _fake_dm)
        result = await cwl._send_cwl_enrollment_dm_batch(
            event_id, 927, "2026-09", [self._target("#RACED", "Raced", "owner1")]
        )

        assert result["contacted"] == 0
        assert result["skipped_unlinked"] == 1
        assert db.get_cwl_signup_sync(event_id, "#RACED") is None

    @pytest.mark.asyncio
    async def test_relinked_to_a_different_owner_dms_the_new_owner(self, db, monkeypatch):
        """The live re-check also picks up a re-link to someone ELSE, not just an unlink -- same
        "live wins over a stale snapshot" rule already applied at pool-build time."""
        from qapbot.cache_manager import CACHE
        import qapbot.QBdiscocmdshelper_cwl as cwl

        event_id = await _seed(db, "928", "#CLANO")
        await _link(db, "new_owner", "#RELINKED")
        CACHE.db_manager = db

        async def _fake_dm(evt, gid, season, participant):
            assert participant["discord_id"] == "new_owner"
            return True, "sent", "m", "c"

        monkeypatch.setattr(cwl, "send_cwl_signup_template_dm", _fake_dm)
        result = await cwl._send_cwl_enrollment_dm_batch(
            event_id, 928, "2026-09", [self._target("#RELINKED", "Relinked", "old_owner")]
        )

        assert result["contacted"] == 1
        assert db.get_cwl_signup_sync(event_id, "#RELINKED")["dmed_discord_id"] == "new_owner"


class TestRepairSignupsForSentDms:
    """Tracker #0016, part 2: the forward fix can't help the 27 DMs already sitting in inboxes --
    the batch's global dm_sent dedup skips anyone already contacted, so they'd never be revisited.
    But the button resolves (event_id, player_tag) at CLICK time, so creating the missing row
    repairs the delivered DM in place: no re-send, the existing button just starts working."""

    @staticmethod
    async def _dmed_without_signup(db, guild_id: str, clan: str, tag: str, status: str = "pending"):
        event_id = await _seed(db, guild_id, clan)
        db.update_cwl_event_status_sync(event_id, "signup_open")
        db.mark_cwl_player_dm_sent_sync(
            tag, "2026-09", "Dangler", "owner1", event_id, int(guild_id),
            "2026-09-01T10:00Z", "msg1", "chan1",
        )
        if status != "pending":
            db.set_cwl_player_response_status_sync(
                tag, "2026-09", "Dangler", "owner1", status, "2026-09-01T11:00Z", event_id, guild_id,
            )
        assert db.get_cwl_signup_sync(event_id, tag) is None  # the dead-button state
        return event_id

    @pytest.mark.asyncio
    async def test_repair_creates_the_missing_row(self, db):
        event_id = await self._dmed_without_signup(db, "930", "#CLANM", "#DEAD1")

        await db._repair_cwl_signups_for_sent_dms()

        row = db.get_cwl_signup_sync(event_id, "#DEAD1")
        assert row is not None
        assert row["dmed_discord_id"] == "owner1"

    @pytest.mark.asyncio
    async def test_repair_carries_the_global_response(self, db):
        event_id = await self._dmed_without_signup(db, "931", "#CLANN", "#DEAD2", status="declined")

        await db._repair_cwl_signups_for_sent_dms()

        assert db.get_cwl_signup_sync(event_id, "#DEAD2")["status"] == "declined"

    @pytest.mark.asyncio
    async def test_repair_is_idempotent(self, db):
        event_id = await self._dmed_without_signup(db, "932", "#CLANO2", "#DEAD3")

        for _ in range(3):
            await db._repair_cwl_signups_for_sent_dms()

        rows = [s for s in db.get_cwl_signups_for_event_sync(event_id) if s["player_tag"] == "#DEAD3"]
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_repair_never_clobbers_an_existing_row(self, db):
        event_id = await _seed(db, "933", "#CLANP")
        db.update_cwl_event_status_sync(event_id, "signup_open")
        db.upsert_cwl_signup_sync(
            event_id, "#LIVE1", "Live", "owner1", None, "template_confirm", "confirmed",
        )
        db.mark_cwl_player_dm_sent_sync(
            "#LIVE1", "2026-09", "Live", "owner1", event_id, 933, "2026-09-01T10:00Z", "m", "c",
        )

        await db._repair_cwl_signups_for_sent_dms()

        assert db.get_cwl_signup_sync(event_id, "#LIVE1")["status"] == "confirmed"

    @pytest.mark.asyncio
    async def test_repair_skips_events_that_are_no_longer_signup_open(self, db):
        """Only 'signup_open' events have an actionable button, so that's the whole broken-link
        population -- and skipping the rest avoids retroactively adding players to a finalized
        event's historical board."""
        event_id = await self._dmed_without_signup(db, "934", "#CLANQ", "#DEAD4")
        db.update_cwl_event_status_sync(event_id, "finalized")

        await db._repair_cwl_signups_for_sent_dms()

        assert db.get_cwl_signup_sync(event_id, "#DEAD4") is None

    @pytest.mark.asyncio
    async def test_repair_ignores_players_never_dmed(self, db):
        event_id = await _seed(db, "935", "#CLANR")
        db.update_cwl_event_status_sync(event_id, "signup_open")
        db.set_cwl_player_response_status_sync(
            "#NEVERDM", "2026-09", "NeverDM", "owner1", "pending", None, event_id, "935",
        )

        await db._repair_cwl_signups_for_sent_dms()

        assert db.get_cwl_signup_sync(event_id, "#NEVERDM") is None
