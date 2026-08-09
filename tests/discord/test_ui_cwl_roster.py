"""Tests for the CWL roster planning UI layer (CWL_ROSTER_PLANNING_PLAN.md Phase 1):
the shared cwl_settings/cwl_management content layer, both entry points
(ClanManagementView's mode dropdown and CwlManagementHubView), and CwlEventSetupView's
toggle-and-carry-over working-copy flow.
"""
from __future__ import annotations

import os
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import discord
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
def _bypass_cwl_admin_check(monkeypatch):
    """Every CWL settings/management callback re-checks admin permissions per-callback (not
    just at open) — bypass it by default so these tests exercise the CWL-specific logic, not
    the (separately-tested, in test_dm_interaction_foundation.py) permission-check machinery."""
    import qapbot.QBdiscocmdshelper as helper

    async def _always_admin(*args, **kwargs):
        return True

    monkeypatch.setattr(helper, "check_admin_permissions", _always_admin)


async def _seed_guild_and_clans(db: WarHistoryDB, guild_id: str, clan_tags: Dict[str, str]) -> None:
    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
    for tag, name in clan_tags.items():
        await db.conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name) VALUES (?, ?)", (tag, name))
    await db.conn.commit()


# ---------------------------------------------------------------------------
# format_clan_management_message() dispatch — entry point (a)
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_format_clan_management_message_dispatches_cwl_settings(monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper import format_clan_management_message

    CACHE.server_config["555"] = {}
    guild = MagicMock()
    guild.id = 555
    guild.name = "Test Guild"

    embed, secondary, linked, unlinked = await format_clan_management_message("#CLAN1", guild, mode="cwl_settings")

    assert isinstance(embed, discord.Embed)
    assert "CWL Settings" in (embed.title or "")
    assert secondary is None
    assert linked == []
    assert unlinked == []


@pytest.mark.discord
@pytest.mark.asyncio
async def test_format_clan_management_message_dispatches_cwl_management(monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper import format_clan_management_message

    CACHE.server_config["556"] = {}
    CACHE.db_manager = None  # no event configured -> "no event" branch, no DB needed
    guild = MagicMock()
    guild.id = 556
    guild.name = "Test Guild"

    embed, secondary, linked, unlinked = await format_clan_management_message("#CLAN1", guild, mode="cwl_management")

    assert isinstance(embed, discord.Embed)
    assert "CWL Management" in (embed.title or "")
    assert "No CWL event" in (embed.description or "")


@pytest.mark.discord
@pytest.mark.asyncio
async def test_format_clan_management_message_default_still_registrations(monkeypatch):
    """Regression guard: adding the two new elif branches must not shift the trailing bare
    `else` — an unrecognized/omitted mode must still default to registrations, not silently
    render the wrong (or a CWL) screen."""
    import qapbot.QBdiscocmdshelper as helper

    called = {}

    async def _fake_registrations(clan_tag, guild):
        called["hit"] = True
        return MagicMock(), None, [], []

    monkeypatch.setattr(helper, "_format_clan_management_registrations", _fake_registrations)
    guild = MagicMock()
    guild.id = 999

    await helper.format_clan_management_message("#CLAN1", guild)  # mode omitted -> default

    assert called.get("hit") is True


# ---------------------------------------------------------------------------
# ClanManagementView — entry point (a): construction for both new modes
# ---------------------------------------------------------------------------

@pytest.mark.discord
def test_clan_management_view_cwl_settings_mode_constructs_without_row_conflict():
    """The real regression risk here is a Discord row conflict: ClanManagementView always
    reserves row 0 (refresh) and row 2 (mode select) regardless of mode — constructing with
    mode="cwl_settings" must not raise (would, if add_cwl_settings_components() placed
    anything on those rows)."""
    from qapbot.cache_manager import CACHE
    from qapbot.ui_clan_management import ClanManagementView

    CACHE.server_config["555"] = {}
    guild = MagicMock()
    guild.id = 555
    sent_message = MagicMock(guild=guild)

    view = ClanManagementView(
        clan_tag="#CLAN1", guild_clans=["#CLAN1"], unlinked_players=[],
        sent_message=sent_message, mode="cwl_settings", timeout=300,
    )

    assert len(view.children) == 5  # mode select + refresh + channel/toggle buttons + retention select


@pytest.mark.discord
def test_clan_management_view_cwl_management_mode_constructs_without_row_conflict():
    from qapbot.cache_manager import CACHE

    CACHE.server_config["556"] = {}
    CACHE.db_manager = None
    from qapbot.ui_clan_management import ClanManagementView

    guild = MagicMock()
    guild.id = 556
    sent_message = MagicMock(guild=guild)

    view = ClanManagementView(
        clan_tag="#CLAN1", guild_clans=["#CLAN1"], unlinked_players=[],
        sent_message=sent_message, mode="cwl_management", timeout=300,
    )

    assert len(view.children) == 5  # mode select + refresh + configure/start(disabled)/manage(disabled)


@pytest.mark.discord
def test_clan_management_view_cwl_mode_select_options_present():
    from qapbot.cache_manager import CACHE
    from qapbot.ui_clan_management import ClanManagementView

    CACHE.server_config["557"] = {}
    guild = MagicMock()
    guild.id = 557
    sent_message = MagicMock(guild=guild)

    view = ClanManagementView(
        clan_tag="#CLAN1", guild_clans=["#CLAN1"], unlinked_players=[],
        sent_message=sent_message, mode="config", timeout=300,
    )
    mode_select = next(c for c in view.children if getattr(c, "custom_id", None) == "clan_mgmt_mode_select")
    values = {opt.value for opt in mode_select.options}  # type: ignore[union-attr]
    assert "cwl_settings" in values
    assert "cwl_management" in values


# ---------------------------------------------------------------------------
# ChannelConfigurationView — new CWL Management Hub channel slot
# ---------------------------------------------------------------------------

@pytest.mark.discord
def test_cwl_management_channel_slot_registered():
    from qapbot.ui_clan_management import DEFAULT_CHANNEL_SLOTS

    slot = next((s for s in DEFAULT_CHANNEL_SLOTS if s.key == "cwl_management"), None)
    assert slot is not None
    assert slot.config_key == "cwl_management_channel_id"
    assert slot.disable_flag_keys == ("cwl_management_message_enabled",)


# ---------------------------------------------------------------------------
# CwlManagementHubView — entry point (b)
# ---------------------------------------------------------------------------

@pytest.mark.discord
def test_cwl_management_hub_view_constructs_with_toggle_buttons():
    from qapbot.ui_cwl_roster import CwlManagementHubView

    view = CwlManagementHubView()
    assert len(view.children) == 2
    custom_ids = {c.custom_id for c in view.children}  # type: ignore[attr-defined]
    assert custom_ids == {"cwl_hub_mode_settings", "cwl_hub_mode_management"}


@pytest.mark.discord
def test_cwl_management_hub_view_holds_no_per_guild_instance_state():
    """Regression guard for the shared-instance bug this phase caught during review: a single
    CwlManagementHubView instance serves every guild's anchored message via add_view(), so it
    must never cache "current mode" (or any other per-guild value) as instance state."""
    from qapbot.ui_cwl_roster import CwlManagementHubView

    view = CwlManagementHubView()
    assert not hasattr(view, "mode")
    assert not hasattr(view, "message")


@pytest.mark.discord
@pytest.mark.asyncio
async def test_cwl_management_hub_view_refresh_fetches_and_edits_tracked_message(monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import CwlManagementHubView

    CACHE.server_config["777"] = {
        "cwl_management_channel_id": "111",
        "cwl_management_message_id": "222",
    }
    CACHE.db_manager = None

    fake_message = AsyncMock()
    fake_channel = MagicMock(spec=discord.TextChannel)
    fake_channel.fetch_message = AsyncMock(return_value=fake_message)
    fake_bot = MagicMock()
    fake_bot.get_channel = MagicMock(return_value=fake_channel)

    import QBcore
    monkeypatch.setattr(QBcore, "bot", fake_bot)

    interaction = MagicMock()
    interaction.guild = MagicMock()
    interaction.guild.id = 777

    view = CwlManagementHubView()
    await view.refresh_cwl_view(interaction, "cwl_management")

    fake_channel.fetch_message.assert_awaited_once_with(222)
    fake_message.edit.assert_awaited_once()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_cwl_management_hub_view_refresh_noop_without_tracked_message():
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import CwlManagementHubView

    CACHE.server_config["778"] = {}  # never configured
    interaction = MagicMock()
    interaction.guild = MagicMock()
    interaction.guild.id = 778

    view = CwlManagementHubView()
    await view.refresh_cwl_view(interaction, "cwl_management")  # must not raise


# ---------------------------------------------------------------------------
# CwlEventSetupView — toggle + carry-over + Apply
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_cwl_event_setup_view_seeds_from_previous_season_carry_over(db):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import CwlEventSetupView

    await _seed_guild_and_clans(db, "111", {"#CLAN1": "Alpha"})
    old_event_id = db.create_cwl_event_sync("111", "2026-07", "discordid1")
    db.set_cwl_event_clans_sync(old_event_id, [{"clan_tag": "#CLAN1", "roster_size": 30, "tier_order": 0}])

    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}}
    CACHE.server_config["111"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    guild = MagicMock()
    guild.id = 111
    parent = MagicMock()

    view = CwlEventSetupView(guild=guild, parent_view=parent)

    assert "#CLAN1" in view.working_clans
    assert view.working_clans["#CLAN1"]["roster_size"] == 30


@pytest.mark.discord
@pytest.mark.asyncio
async def test_cwl_event_setup_view_toggle_adds_and_removes_clan(db, mock_interaction):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import CwlEventSetupView

    await _seed_guild_and_clans(db, "222", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}}
    CACHE.server_config["222"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    guild = MagicMock()
    guild.id = 222
    parent = MagicMock()
    view = CwlEventSetupView(guild=guild, parent_view=parent)
    assert "#CLAN1" not in view.working_clans

    toggle_button = next(c for c in view.children if getattr(c, "custom_id", None) == "cwl_setup_clan_#CLAN1")
    mock_interaction.edit_original_response = AsyncMock()
    await toggle_button.callback(mock_interaction)  # type: ignore[misc]
    assert "#CLAN1" in view.working_clans

    toggle_button_again = next(c for c in view.children if getattr(c, "custom_id", None) == "cwl_setup_clan_#CLAN1")
    await toggle_button_again.callback(mock_interaction)  # type: ignore[misc]
    assert "#CLAN1" not in view.working_clans


@pytest.mark.discord
@pytest.mark.asyncio
async def test_cwl_event_setup_view_apply_persists_and_refreshes_parent(db, mock_interaction):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import CwlEventSetupView
    from qapbot.QBdiscocmdshelper_cwl import resolve_current_cwl_season

    await _seed_guild_and_clans(db, "333", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}}
    CACHE.server_config["333"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    guild = MagicMock()
    guild.id = 333
    parent = MagicMock()
    parent.refresh_cwl_view = AsyncMock()

    view = CwlEventSetupView(guild=guild, parent_view=parent)
    view.working_clans["#CLAN1"] = {"target_league_rank": None, "roster_size": 15, "tier_order": 0, "cwl_start_at": None}

    mock_interaction.delete_original_response = AsyncMock()
    await view._on_apply(mock_interaction)

    event = db.get_cwl_event_sync("333", resolve_current_cwl_season())
    assert event is not None
    clans = db.get_cwl_event_clans_sync(event["id"])
    assert [c["clan_tag"] for c in clans] == ["#CLAN1"]

    parent.refresh_cwl_view.assert_awaited_once()
    # "Configure Participating Clans" only ever opens from the cwl_management screen.
    assert parent.refresh_cwl_view.await_args.args[1] == "cwl_management"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_cwl_event_setup_view_cancel_does_not_persist(db, mock_interaction):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import CwlEventSetupView

    await _seed_guild_and_clans(db, "444", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}}
    CACHE.server_config["444"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    guild = MagicMock()
    guild.id = 444
    parent = MagicMock()
    view = CwlEventSetupView(guild=guild, parent_view=parent)
    view.working_clans["#CLAN1"] = {"target_league_rank": None, "roster_size": 15, "tier_order": 0, "cwl_start_at": None}

    mock_interaction.delete_original_response = AsyncMock()
    await view._on_cancel(mock_interaction)

    assert db.list_cwl_events_sync("444") == []
