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
    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
    await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES (?, ?)", (clan_tag, "Test Clan"))
    await db.conn.commit()
    event_id = db.create_cwl_event_sync(guild_id, "2026-09", "admin1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": clan_tag, "participating": True}])
    return event_id


async def _link(db: WarHistoryDB, discord_id: str, player_tag: str, clan_tag=None) -> None:
    await db.conn.execute(
        "INSERT OR IGNORE INTO users (discord_id, display_name) VALUES (?, ?)", (discord_id, discord_id)
    )
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag) "
        "VALUES (?, ?, ?, 0, ?)",
        (discord_id, player_tag, "Player", clan_tag),
    )
    await db.conn.commit()


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
        assert rows["#MIG"]["discord_id"] == "trueowner"

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
        assert rows["#GUESTX"]["discord_id"] == "guestowner"

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
