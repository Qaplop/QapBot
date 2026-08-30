"""Tracker #0038: "Remind Pending" CWL signup reminders, and the season-overview counter split
that came out of investigating it.

Covers the new pool-resolution/grouping logic (QBdiscocmdshelper_cwl.py) with a real WarHistoryDB,
same fixture pattern as test_cwl_signup_snapshot_staleness.py — these functions are dominated by
DB joins/filters that a mocked db_manager would just re-assert back at itself.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

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
    so the season overview shows both, matching what the board actually renders.

    Tightened again for tracker #0079: the board also requires the DM to have actually gone out
    before it draws ❓ (a linked, never-DMed pending player renders "Not Invited Yet" instead), so
    the linked half requires dm_sent too."""

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
        db.mark_cwl_player_dm_sent_sync("#LINKED1", "2026-09", "Linked1", "owner1", event_id, 802, "2026-09-01T08:00Z")
        db.upsert_cwl_signup_sync(event_id, "#GHOST1", "Ghost1", None, None, "guest_invite", "pending")
        db.upsert_cwl_signup_sync(event_id, "#GHOST2", "Ghost2", None, None, "guest_invite", "pending")

        assert split_cwl_pending_signups_by_link_sync(event_id) == (1, 2)

    @pytest.mark.asyncio
    async def test_linked_but_never_dmed_is_not_counted_as_pending(self, db):
        """Tracker #0079: the reported symptom exactly — "Ausstehend" said 2 while the board showed
        a single ❓. The second player was linked and pending but had never been sent a DM, so the
        board drew "Not Invited Yet" for them while this counter still called them pending."""
        from qapbot.cache_manager import CACHE
        from qapbot.QBdiscocmdshelper_cwl import split_cwl_pending_signups_by_link_sync

        event_id = await _seed(db, "804", "#CLAND")
        CACHE.db_manager = db
        await _link(db, "owner1", "#INVITED")
        await _link(db, "owner2", "#NOTINVITED")
        db.upsert_cwl_signup_sync(event_id, "#INVITED", "Invited", "owner1", None, "template_confirm", "pending")
        db.mark_cwl_player_dm_sent_sync("#INVITED", "2026-09", "Invited", "owner1", event_id, 804, "2026-09-01T08:00Z")
        db.upsert_cwl_signup_sync(event_id, "#NOTINVITED", "NotInvited", "owner2", None, "auto_seeded", "pending")

        # Exactly one ❓ on the board, so exactly 1 here — never 2.
        assert split_cwl_pending_signups_by_link_sync(event_id) == (1, 0)

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


# ---------------------------------------------------------------------------
# count_cwl_pool_members_missing_dm — season-overview "New players without DM
# invitation" line, and its boolean has_cwl_pool_members_missing_dm() sibling
# ---------------------------------------------------------------------------

class TestCountCwlPoolMembersMissingDm:
    """Backs both the "Notify New Pool Members" button's gating and the new season-overview
    count line — one shared resolution so the two can never disagree (2026-08-23)."""

    @pytest.mark.asyncio
    async def test_zero_when_no_event(self, db):
        from qapbot.cache_manager import CACHE
        from qapbot.QBdiscocmdshelper_cwl import count_cwl_pool_members_missing_dm

        CACHE.db_manager = db
        assert count_cwl_pool_members_missing_dm(813, "2026-09") == 0

    @pytest.mark.asyncio
    async def test_zero_while_event_is_still_draft(self, db):
        from qapbot.cache_manager import CACHE
        from qapbot.QBdiscocmdshelper_cwl import count_cwl_pool_members_missing_dm

        await _seed(db, "814", "#CLANL")
        CACHE.db_manager = db
        await _link(db, "owner1", "#P1")

        assert count_cwl_pool_members_missing_dm(814, "2026-09") == 0

    @pytest.mark.asyncio
    async def test_counts_dmable_pool_members_never_contacted(self, db):
        from qapbot.cache_manager import CACHE
        from qapbot.QBdiscocmdshelper_cwl import count_cwl_pool_members_missing_dm

        event_id = await _seed(db, "815", "#CLANM")
        db.update_cwl_event_status_sync(event_id, "signup_open")
        CACHE.db_manager = db
        await _link(db, "owner1", "#NEW1")
        await _link(db, "owner2", "#NEW2")
        db.upsert_cwl_signup_sync(event_id, "#NEW1", "New1", "owner1", None, "guest_invite", "pending")
        db.upsert_cwl_signup_sync(event_id, "#NEW2", "New2", "owner2", None, "guest_invite", "pending")

        assert count_cwl_pool_members_missing_dm(815, "2026-09") == 2

    @pytest.mark.asyncio
    async def test_already_dmed_members_are_not_counted(self, db):
        from qapbot.cache_manager import CACHE
        from qapbot.QBdiscocmdshelper_cwl import count_cwl_pool_members_missing_dm

        event_id = await _seed(db, "816", "#CLANN")
        db.update_cwl_event_status_sync(event_id, "signup_open")
        CACHE.db_manager = db
        await _link(db, "owner1", "#DMED")
        db.upsert_cwl_signup_sync(event_id, "#DMED", "Dmed", "owner1", None, "template_confirm", "pending")
        db.mark_cwl_player_dm_sent_sync(
            "#DMED", "2026-09", "Dmed", "owner1", event_id, 816, "2026-09-01T10:00Z", "m", "c",
        )

        assert count_cwl_pool_members_missing_dm(816, "2026-09") == 0

    @pytest.mark.asyncio
    async def test_settled_status_without_dm_sent_is_not_counted(self, db):
        """Tracker #0075, live bug report: "Stay shows 3 uninvited, but in the team management
        view only 1 is shown". A standing opt-in seeds 'auto_confirmed' immediately and the send
        is always attempted (resolve_seeded_cwl_signup_status branch 3), but it can still fail
        silently (DM guard, blocked, left every mutual guild, a transient failure) same as any
        other recipient — leaving dm_sent False for an account that has already answered via its
        standing preference. enrollmentBoard.ts's hasVisibleRealStatus() never shows such a
        player as "Not Invited Yet" (only a 'pending' row without dm_sent gets that treatment),
        so this count must agree and exclude it too."""
        from qapbot.cache_manager import CACHE
        from qapbot.QBdiscocmdshelper_cwl import count_cwl_pool_members_missing_dm

        event_id = await _seed(db, "819", "#CLANQ")
        db.update_cwl_event_status_sync(event_id, "signup_open")
        CACHE.db_manager = db
        await _link(db, "owner1", "#OPTIN1")
        db.upsert_cwl_signup_sync(event_id, "#OPTIN1", "Optin1", "owner1", None, "auto_optin", "auto_confirmed")

        assert count_cwl_pool_members_missing_dm(819, "2026-09") == 0

    @pytest.mark.asyncio
    async def test_unlinked_pool_members_are_not_counted(self, db):
        """Nobody to DM — mirrors resolve_cwl_pool_dm_targets_sync's own skipped_unlinked bucket."""
        from qapbot.cache_manager import CACHE
        from qapbot.QBdiscocmdshelper_cwl import count_cwl_pool_members_missing_dm

        event_id = await _seed(db, "817", "#CLANO")
        db.update_cwl_event_status_sync(event_id, "signup_open")
        CACHE.db_manager = db
        db.upsert_cwl_signup_sync(event_id, "#GHOST", "Ghost", None, None, "guest_invite", "pending")

        assert count_cwl_pool_members_missing_dm(817, "2026-09") == 0

    @pytest.mark.asyncio
    async def test_boolean_sibling_matches_the_count(self, db):
        from qapbot.cache_manager import CACHE
        from qapbot.QBdiscocmdshelper_cwl import has_cwl_pool_members_missing_dm

        event_id = await _seed(db, "818", "#CLANP")
        db.update_cwl_event_status_sync(event_id, "signup_open")
        CACHE.db_manager = db
        await _link(db, "owner1", "#NEW1")
        db.upsert_cwl_signup_sync(event_id, "#NEW1", "New1", "owner1", None, "guest_invite", "pending")

        assert has_cwl_pool_members_missing_dm(818, "2026-09") is True


# ---------------------------------------------------------------------------
# rerender_cwl_dm_after_response — Phase 4d (plans/cwl-personal-hub.md) unification of
# CwlSignupResponseButton's and CwlReminderResponseButton's previously-diverged rendering
# ---------------------------------------------------------------------------

class TestRerenderCwlDmAfterResponse:
    @pytest.mark.asyncio
    async def test_single_account_scope_finalizes_with_view_none(self, db):
        """Byte-identical to the pre-unification single-account invitation behavior."""
        from qapbot.cache_manager import CACHE
        from qapbot.ui_cwl_roster import rerender_cwl_dm_after_response

        event_id = await _seed(db, "820", "#CLANQ")
        CACHE.db_manager = db
        db.mark_cwl_player_dm_sent_sync(
            "#P1", "2026-09", "PlayerOne", "d1", event_id, 820, "2026-09-01T09:00Z", "700001",
        )
        db.set_cwl_player_response_status_sync(
            "#P1", "2026-09", "PlayerOne", "d1", "confirmed", "2026-09-01T09:05Z", event_id, 820,
        )

        message = MagicMock()
        message.id = 700001
        interaction = AsyncMock()
        interaction.response.edit_message = AsyncMock()

        await rerender_cwl_dm_after_response(
            message, event_id, "2026-09", "d1",
            action="confirm", player_name="PlayerOne", interaction=interaction,
        )

        interaction.response.edit_message.assert_awaited_once()
        _, kwargs = interaction.response.edit_message.call_args
        assert kwargs["view"] is None
        assert "PlayerOne" in kwargs["content"]

    @pytest.mark.asyncio
    async def test_two_of_three_still_pending_rerenders_with_their_buttons(self, db):
        from qapbot.cache_manager import CACHE
        from qapbot.ui_cwl_roster import CwlReminderResponseButton, rerender_cwl_dm_after_response

        event_id = await _seed(db, "821", "#CLANR")
        CACHE.db_manager = db
        for tag, name in (("#P1", "One"), ("#P2", "Two"), ("#P3", "Three")):
            db.mark_cwl_player_dm_sent_sync(
                tag, "2026-09", name, "d1", event_id, 821, "2026-09-01T09:00Z", "700002",
            )
        # #P1 just answered (the account whose button was clicked); #P2/#P3 remain pending.
        db.set_cwl_player_response_status_sync(
            "#P1", "2026-09", "One", "d1", "confirmed", "2026-09-01T09:05Z", event_id, 821,
        )

        message = MagicMock()
        message.id = 700002
        interaction = AsyncMock()
        interaction.response.edit_message = AsyncMock()

        await rerender_cwl_dm_after_response(
            message, event_id, "2026-09", "d1",
            action="confirm", player_name="One", interaction=interaction,
        )

        interaction.response.edit_message.assert_awaited_once()
        _, kwargs = interaction.response.edit_message.call_args
        assert kwargs["view"] is not None
        buttons = [item for item in kwargs["view"].children if isinstance(item, CwlReminderResponseButton)]
        assert {b.player_tag for b in buttons} == {"#P2", "#P3"}

    @pytest.mark.asyncio
    async def test_last_of_three_finalizes_with_view_none(self, db):
        """Once the third and final account is answered, the message finalizes exactly like the
        single-account case — there is no distinct "group finalized" wording in the
        pre-unification code, so none is invented here either."""
        from qapbot.cache_manager import CACHE
        from qapbot.ui_cwl_roster import rerender_cwl_dm_after_response

        event_id = await _seed(db, "822", "#CLANS")
        CACHE.db_manager = db
        for tag, name, status in (("#P1", "One", "confirmed"), ("#P2", "Two", "declined"), ("#P3", "Three", "pending")):
            db.mark_cwl_player_dm_sent_sync(
                tag, "2026-09", name, "d1", event_id, 822, "2026-09-01T09:00Z", "700003",
            )
            if status != "pending":
                db.set_cwl_player_response_status_sync(
                    tag, "2026-09", name, "d1", status, "2026-09-01T09:05Z", event_id, 822,
                )
        # #P3 is the last one to answer now.
        db.set_cwl_player_response_status_sync(
            "#P3", "2026-09", "Three", "d1", "declined", "2026-09-01T09:10Z", event_id, 822,
        )

        message = MagicMock()
        message.id = 700003
        interaction = AsyncMock()
        interaction.response.edit_message = AsyncMock()

        await rerender_cwl_dm_after_response(
            message, event_id, "2026-09", "d1",
            action="optout", player_name="Three", interaction=interaction,
        )

        _, kwargs = interaction.response.edit_message.call_args
        assert kwargs["view"] is None
        assert "Three" in kwargs["content"]

    @pytest.mark.asyncio
    async def test_scope_ignores_accounts_pointing_at_a_different_message_id(self, db):
        from qapbot.cache_manager import CACHE
        from qapbot.ui_cwl_roster import CwlReminderResponseButton, rerender_cwl_dm_after_response

        event_id = await _seed(db, "823", "#CLANT")
        CACHE.db_manager = db
        db.mark_cwl_player_dm_sent_sync(
            "#SAME", "2026-09", "Same", "d1", event_id, 823, "2026-09-01T09:00Z", "700004a",
        )
        db.mark_cwl_player_dm_sent_sync(
            "#OTHER", "2026-09", "Other", "d1", event_id, 823, "2026-09-01T09:00Z", "700004b",
        )

        message = MagicMock()
        message.id = "700004a"  # matches only #SAME's dm_sent_via_message_id
        interaction = AsyncMock()
        interaction.response.edit_message = AsyncMock()

        await rerender_cwl_dm_after_response(
            message, event_id, "2026-09", "d1",
            action="confirm", player_name="Same", interaction=interaction,
        )

        _, kwargs = interaction.response.edit_message.call_args
        # #SAME is still 'pending' (never answered in this test) and alone in scope, so it
        # re-renders with just its own buttons — #OTHER (a different message) must never appear.
        assert kwargs["view"] is not None
        buttons = [item for item in kwargs["view"].children if isinstance(item, CwlReminderResponseButton)]
        assert {b.player_tag for b in buttons} == {"#SAME"}

    @pytest.mark.asyncio
    async def test_no_interaction_path_edits_the_message_directly(self, db):
        """The whole point of this function: it must be drivable WITHOUT an Interaction, for a
        future non-Discord caller (e.g. the Activity's own DM reconciliation)."""
        from qapbot.cache_manager import CACHE
        from qapbot.ui_cwl_roster import rerender_cwl_dm_after_response

        event_id = await _seed(db, "824", "#CLANU")
        CACHE.db_manager = db
        db.mark_cwl_player_dm_sent_sync(
            "#P1", "2026-09", "PlayerOne", "d1", event_id, 824, "2026-09-01T09:00Z", "700005",
        )
        db.set_cwl_player_response_status_sync(
            "#P1", "2026-09", "PlayerOne", "d1", "declined", "2026-09-01T09:05Z", event_id, 824,
        )

        message = AsyncMock()
        message.id = 700005

        await rerender_cwl_dm_after_response(
            message, event_id, "2026-09", "d1",
            action="optout", player_name="PlayerOne", interaction=None,
        )

        message.edit.assert_awaited_once()
        _, kwargs = message.edit.call_args
        assert kwargs["view"] is None
        assert "PlayerOne" in kwargs["content"]
