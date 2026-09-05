"""Tests for the "clan removed from guild while still in an active CWL lineup" safety check
(CWL_ROSTER_PLANNING_PLAN.md, 2026-08-10 fix) — MemberClansConfigurationView._on_apply() and
CwlLineupRemovalConfirmView in ui_clan_management.py.

Live-tested gap this closes: an admin removed clans from the guild's member-clan list, but the
CWL Management table kept listing them as "Participating Clans" with no warning at all. The fix
blocks the whole member-clan/family change (not just this one clan) behind a confirmation when
any removed clan is still marked participating in a non-cancelled cwl_events row, and — only if
confirmed — deactivates it there too.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

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


@pytest.fixture(autouse=True)
def _bypass_admin_check(monkeypatch):
    import qapbot.QBdiscocmdshelper as helper

    async def _always_admin(*args, **kwargs):
        return True

    monkeypatch.setattr(helper, "check_admin_permissions", _always_admin)


async def _seed_guild_and_clan(db: WarHistoryDB, guild_id: str, clan_tag: str = "#CLAN1") -> None:
    await db._conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
    await db._conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES (?, ?)", (clan_tag, "Test Clan"))
    await db._conn.commit()


def _make_member_clans_view(guild, current_member_clans, current_member_families=None):
    from qapbot.ui_clan_management import MemberClansConfigurationView

    clan_management_view = MagicMock()
    clan_management_view.sent_message = MagicMock(guild=guild)

    original_interaction = MagicMock()
    original_interaction.guild = guild

    view = MemberClansConfigurationView(
        guild=guild,
        clan_management_view=clan_management_view,
        original_interaction=original_interaction,
        current_member_clans=list(current_member_clans),
        current_member_families=list(current_member_families or []),
        clan_families={},
        timeout=300,
    )
    return view


@pytest.mark.discord
@pytest.mark.asyncio
async def test_on_apply_proceeds_immediately_without_cwl_conflict(db, mock_interaction):
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clan(db, "9301", "#CLAN1")
    CACHE.db_manager = db
    CACHE.server_config["9301"] = {"member_clans": ["#CLAN1"], "member_families": []}
    mock_interaction.guild.id = 9301

    view = _make_member_clans_view(mock_interaction.guild, ["#CLAN1"])
    view.member_clans = []  # removing #CLAN1 — but it's not in any CWL event
    view._apply_member_clans_changes = AsyncMock()  # type: ignore[method-assign]

    await view._on_apply(mock_interaction)

    view._apply_member_clans_changes.assert_awaited_once()
    mock_interaction.followup.send.assert_not_awaited()  # no confirmation shown


@pytest.mark.discord
@pytest.mark.asyncio
async def test_on_apply_blocks_and_shows_confirmation_when_clan_actively_in_cwl(db, mock_interaction):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_clan_management import CwlLineupRemovalConfirmView

    await _seed_guild_and_clan(db, "9302", "#CLAN1")
    CACHE.db_manager = db
    CACHE.server_config["9302"] = {"member_clans": ["#CLAN1"], "member_families": []}
    mock_interaction.guild.id = 9302
    event_id = db.create_cwl_event_sync("9302", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])

    view = _make_member_clans_view(mock_interaction.guild, ["#CLAN1"])
    view.member_clans = []  # removing #CLAN1 — it IS actively participating
    view._apply_member_clans_changes = AsyncMock()  # type: ignore[method-assign]

    mock_interaction.followup.send = AsyncMock()
    await view._on_apply(mock_interaction)

    # The whole operation is blocked — apply was never called.
    view._apply_member_clans_changes.assert_not_awaited()
    mock_interaction.followup.send.assert_awaited_once()
    _, kwargs = mock_interaction.followup.send.call_args
    confirm_view = kwargs["view"]
    assert isinstance(confirm_view, CwlLineupRemovalConfirmView)
    assert confirm_view.cwl_conflicts == {"#CLAN1": [(event_id, "2026-09")]}


@pytest.mark.discord
@pytest.mark.asyncio
async def test_on_apply_ignores_clans_still_covered_by_a_kept_family(db, mock_interaction):
    """A clan individually removed but still reachable via a kept family must not trigger the
    conflict check at all — it's not actually being removed from the guild."""
    from qapbot.cache_manager import CACHE

    await _seed_guild_and_clan(db, "9303", "#CLAN1")
    CACHE.db_manager = db
    CACHE.clan_families["fam1"] = {"name": "Family", "clans": ["#CLAN1"]}
    CACHE.server_config["9303"] = {"member_clans": ["#CLAN1"], "member_families": ["fam1"]}
    mock_interaction.guild.id = 9303
    event_id = db.create_cwl_event_sync("9303", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])

    view = _make_member_clans_view(mock_interaction.guild, ["#CLAN1"], ["fam1"])
    view.member_clans = []  # dropped individually...
    view.member_families = ["fam1"]  # ...but family "fam1" (kept) still covers #CLAN1
    view._apply_member_clans_changes = AsyncMock()  # type: ignore[method-assign]

    await view._on_apply(mock_interaction)

    view._apply_member_clans_changes.assert_awaited_once()
    mock_interaction.followup.send.assert_not_awaited()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_confirm_view_yes_applies_and_deactivates_cwl_participation(db, mock_interaction):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_clan_management import CwlLineupRemovalConfirmView

    await _seed_guild_and_clan(db, "9304", "#CLAN1")
    CACHE.db_manager = db
    event_id = db.create_cwl_event_sync("9304", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])

    member_clans_view = MagicMock()
    member_clans_view._apply_member_clans_changes = AsyncMock()

    confirm_view = CwlLineupRemovalConfirmView(
        member_clans_view=member_clans_view,
        cwl_conflicts={"#CLAN1": [(event_id, "2026-09")]},
    )

    await confirm_view._on_confirm(mock_interaction)

    member_clans_view._apply_member_clans_changes.assert_awaited_once_with(mock_interaction)
    clan = db.get_cwl_event_clans_sync(event_id)[0]
    assert clan["participating"] == 0
    mock_interaction.delete_original_response.assert_awaited_once()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_confirm_view_yes_refreshes_the_hub_message(db, mock_interaction, monkeypatch):
    """The Hub's "Participating Clans" table just lost a clan — must not sit stale."""
    from qapbot.cache_manager import CACHE
    from qapbot.ui_clan_management import CwlLineupRemovalConfirmView

    await _seed_guild_and_clan(db, "9306", "#CLAN1")
    CACHE.db_manager = db
    event_id = db.create_cwl_event_sync("9306", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    mock_interaction.guild.id = 9306

    member_clans_view = MagicMock()
    member_clans_view._apply_member_clans_changes = AsyncMock()

    hub_refresh = AsyncMock()
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", hub_refresh)

    confirm_view = CwlLineupRemovalConfirmView(
        member_clans_view=member_clans_view,
        cwl_conflicts={"#CLAN1": [(event_id, "2026-09")]},
    )

    await confirm_view._on_confirm(mock_interaction)

    hub_refresh.assert_awaited_once_with(9306, "cwl_management")


@pytest.mark.discord
@pytest.mark.asyncio
async def test_confirm_view_cancel_applies_nothing(db, mock_interaction):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_clan_management import CwlLineupRemovalConfirmView

    await _seed_guild_and_clan(db, "9305", "#CLAN1")
    CACHE.db_manager = db
    event_id = db.create_cwl_event_sync("9305", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])

    member_clans_view = MagicMock()
    member_clans_view._apply_member_clans_changes = AsyncMock()

    confirm_view = CwlLineupRemovalConfirmView(
        member_clans_view=member_clans_view,
        cwl_conflicts={"#CLAN1": [(event_id, "2026-09")]},
    )

    await confirm_view._on_cancel(mock_interaction)

    member_clans_view._apply_member_clans_changes.assert_not_awaited()
    # Untouched — cancel aborts the whole operation, including the CWL side.
    clan = db.get_cwl_event_clans_sync(event_id)[0]
    assert clan["participating"] == 1
    mock_interaction.delete_original_response.assert_awaited_once()
