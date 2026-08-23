"""Tracker #0038: "Remind Pending" CWL signup reminders, and the season-overview counter split
that came out of investigating it.

Covers the new pool-resolution/grouping logic (QBdiscocmdshelper_cwl.py) with a real WarHistoryDB,
same fixture pattern as test_cwl_signup_snapshot_staleness.py — these functions are dominated by
DB joins/filters that a mocked db_manager would just re-assert back at itself.
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


async def _link(db: WarHistoryDB, discord_id: str, player_tag: str, optout: bool = False) -> None:
    await db.conn.execute(
        "INSERT OR IGNORE INTO users (discord_id, display_name) VALUES (?, ?)", (discord_id, discord_id)
    )
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified, cwl_permanent_optout) "
        "VALUES (?, ?, ?, 0, ?)",
        (discord_id, player_tag, f"Player-{player_tag}", int(optout)),
    )
    await db.conn.commit()


# ---------------------------------------------------------------------------
# split_cwl_pending_signups_by_link_sync — season-overview counter split
# ---------------------------------------------------------------------------

class TestSplitPendingSignupsByLink:
    """The old single "Ausstehend" count counted every pending cwl_signups row regardless of
    Discord link, while Teams-verwalten only shows a pending ❓ icon for a linked account — split
    so the season overview shows both, matching what the board actually renders."""

    @pytest.mark.asyncio
    async def test_no_signups_is_zero_zero(self, db):
        from qapbot.cache_manager import CACHE
        from qapbot.QBdiscocmdshelper_cwl import split_cwl_pending_signups_by_link_sync

        event_id = await _seed(db, "801", "#CLANA")
        CACHE.db_manager = db

        assert split_cwl_pending_signups_by_link_sync(event_id) == (0, 0)

    @pytest.mark.asyncio
    async def test_splits_linked_from_unlinked_pending(self, db):
        from qapbot.cache_manager import CACHE
        from qapbot.QBdiscocmdshelper_cwl import split_cwl_pending_signups_by_link_sync

        event_id = await _seed(db, "802", "#CLANB")
        CACHE.db_manager = db
        await _link(db, "owner1", "#LINKED1")
        db.upsert_cwl_signup_sync(event_id, "#LINKED1", "Linked1", "owner1", None, "template_confirm", "pending")
        db.upsert_cwl_signup_sync(event_id, "#GHOST1", "Ghost1", None, None, "guest_invite", "pending")
        db.upsert_cwl_signup_sync(event_id, "#GHOST2", "Ghost2", None, None, "guest_invite", "pending")

        assert split_cwl_pending_signups_by_link_sync(event_id) == (1, 2)

    @pytest.mark.asyncio
    async def test_non_pending_rows_are_excluded_from_both_halves(self, db):
        from qapbot.cache_manager import CACHE
        from qapbot.QBdiscocmdshelper_cwl import split_cwl_pending_signups_by_link_sync

        event_id = await _seed(db, "803", "#CLANC")
        CACHE.db_manager = db
        await _link(db, "owner1", "#CONFIRMED1")
        db.upsert_cwl_signup_sync(
            event_id, "#CONFIRMED1", "Confirmed1", "owner1", None, "template_confirm", "confirmed",
        )

        assert split_cwl_pending_signups_by_link_sync(event_id) == (0, 0)


# ---------------------------------------------------------------------------
# resolve_cwl_pending_reminder_targets_sync — grouping + opt-out/unlinked filtering
# ---------------------------------------------------------------------------

class TestResolvePendingReminderTargets:
    @pytest.mark.asyncio
    async def test_groups_multiple_pending_accounts_under_one_discord_user(self, db):
        """The whole point of tracker #0038's redesign: one combined reminder per Discord user,
        not one DM per account."""
        from qapbot.cache_manager import CACHE
        from qapbot.QBdiscocmdshelper_cwl import resolve_cwl_pending_reminder_targets_sync

        event_id = await _seed(db, "804", "#CLAND")
        CACHE.db_manager = db
        await _link(db, "multiowner", "#ALT1")
        await _link(db, "multiowner", "#ALT2")
        db.upsert_cwl_signup_sync(event_id, "#ALT1", "Alt1", "multiowner", None, "template_confirm", "pending")
        db.upsert_cwl_signup_sync(event_id, "#ALT2", "Alt2", "multiowner", None, "template_confirm", "pending")

        result = resolve_cwl_pending_reminder_targets_sync(event_id)

        assert set(result["groups"].keys()) == {"multiowner"}
        tags = {a["player_tag"] for a in result["groups"]["multiowner"]}
        assert tags == {"#ALT1", "#ALT2"}

    @pytest.mark.asyncio
    async def test_unlinked_pending_account_is_skipped_not_grouped(self, db):
        from qapbot.cache_manager import CACHE
        from qapbot.QBdiscocmdshelper_cwl import resolve_cwl_pending_reminder_targets_sync

        event_id = await _seed(db, "805", "#CLANE")
        CACHE.db_manager = db
        db.upsert_cwl_signup_sync(event_id, "#GHOST", "Ghost", None, None, "guest_invite", "pending")

        result = resolve_cwl_pending_reminder_targets_sync(event_id)

        assert result["groups"] == {}
        assert result["skipped_unlinked"] == 1

    @pytest.mark.asyncio
    async def test_permanently_opted_out_account_is_skipped_not_grouped(self, db):
        """A permanent opt-out must not be re-pinged just because its signup row still reads
        'pending' — same honouring resolve_cwl_pool_dm_targets_sync already applies at invite
        time."""
        from qapbot.cache_manager import CACHE
        from qapbot.QBdiscocmdshelper_cwl import resolve_cwl_pending_reminder_targets_sync

        event_id = await _seed(db, "806", "#CLANF")
        CACHE.db_manager = db
        await _link(db, "optedout", "#OPTOUT1", optout=True)
        db.upsert_cwl_signup_sync(event_id, "#OPTOUT1", "Optout1", "optedout", None, "template_confirm", "pending")

        result = resolve_cwl_pending_reminder_targets_sync(event_id)

        assert result["groups"] == {}
        assert result["skipped_optout"] == 1

    @pytest.mark.asyncio
    async def test_confirmed_and_declined_signups_are_not_pool_members(self, db):
        from qapbot.cache_manager import CACHE
        from qapbot.QBdiscocmdshelper_cwl import resolve_cwl_pending_reminder_targets_sync

        event_id = await _seed(db, "807", "#CLANG")
        CACHE.db_manager = db
        await _link(db, "owner1", "#DONE1")
        db.upsert_cwl_signup_sync(event_id, "#DONE1", "Done1", "owner1", None, "template_confirm", "confirmed")

        result = resolve_cwl_pending_reminder_targets_sync(event_id)

        assert result["groups"] == {}

    @pytest.mark.asyncio
    async def test_uses_the_live_link_not_the_stale_snapshot_owner(self, db):
        """Same "live wins over a stale snapshot" rule as resolve_cwl_pool_dm_targets_sync (Pitfall
        37) — an account re-linked to someone else must group under the NEW owner."""
        from qapbot.cache_manager import CACHE
        from qapbot.QBdiscocmdshelper_cwl import resolve_cwl_pending_reminder_targets_sync

        event_id = await _seed(db, "808", "#CLANH")
        CACHE.db_manager = db
        await _link(db, "new_owner", "#RELINKED")
        db.upsert_cwl_signup_sync(
            event_id, "#RELINKED", "Relinked", "old_owner", None, "template_confirm", "pending",
        )

        result = resolve_cwl_pending_reminder_targets_sync(event_id)

        assert set(result["groups"].keys()) == {"new_owner"}


# ---------------------------------------------------------------------------
# has_cwl_pending_signups_to_remind — button-gating check
# ---------------------------------------------------------------------------

class TestHasCwlPendingSignupsToRemind:
    @pytest.mark.asyncio
    async def test_false_when_event_missing(self, db):
        from qapbot.cache_manager import CACHE
        from qapbot.QBdiscocmdshelper_cwl import has_cwl_pending_signups_to_remind

        CACHE.db_manager = db
        assert has_cwl_pending_signups_to_remind(809, "2026-09") is False

    @pytest.mark.asyncio
    async def test_false_while_event_is_still_draft(self, db):
        from qapbot.cache_manager import CACHE
        from qapbot.QBdiscocmdshelper_cwl import has_cwl_pending_signups_to_remind

        await _seed(db, "810", "#CLANI")
        CACHE.db_manager = db
        await _link(db, "owner1", "#P1")
        # create_cwl_event_sync leaves the event in 'draft' by default.

        assert has_cwl_pending_signups_to_remind(810, "2026-09") is False

    @pytest.mark.asyncio
    async def test_true_once_a_dmable_pending_signup_exists(self, db):
        from qapbot.cache_manager import CACHE
        from qapbot.QBdiscocmdshelper_cwl import has_cwl_pending_signups_to_remind

        event_id = await _seed(db, "811", "#CLANJ")
        db.update_cwl_event_status_sync(event_id, "signup_open")
        CACHE.db_manager = db
        await _link(db, "owner1", "#P1")
        db.upsert_cwl_signup_sync(event_id, "#P1", "P1", "owner1", None, "template_confirm", "pending")

        assert has_cwl_pending_signups_to_remind(811, "2026-09") is True

    @pytest.mark.asyncio
    async def test_false_when_only_unlinked_pending_signups_exist(self, db):
        """Mirrors the season-overview split: an unlinked pending account has nobody to remind."""
        from qapbot.cache_manager import CACHE
        from qapbot.QBdiscocmdshelper_cwl import has_cwl_pending_signups_to_remind

        event_id = await _seed(db, "812", "#CLANK")
        db.update_cwl_event_status_sync(event_id, "signup_open")
        CACHE.db_manager = db
        db.upsert_cwl_signup_sync(event_id, "#GHOST", "Ghost", None, None, "guest_invite", "pending")

        assert has_cwl_pending_signups_to_remind(812, "2026-09") is False
