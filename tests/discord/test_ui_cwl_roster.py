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

    assert len(view.children) == 5  # mode select + refresh + channel/toggle/retention buttons


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

    assert len(view.children) == 7  # mode select + refresh + configure/start(disabled)/manage(disabled)/delete/open_web


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


@pytest.mark.discord
@pytest.mark.asyncio
async def test_channel_configuration_view_apply_refreshes_hub_and_closes_ephemeral(db, mock_interaction):
    """Regression guard: ChannelConfigurationView opened from the CWL Management Hub (entry
    point b) used to crash on Apply because it hardcoded a call to _refresh_config_view(),
    which only ClanManagementView has — CwlManagementHubView doesn't. Also covers that the
    ephemeral sub-screen actually closes itself once applied, which it never did before."""
    from qapbot.cache_manager import CACHE
    from qapbot.ui_clan_management import ChannelConfigurationView, CWL_CONFIG_CHANNEL_SLOTS
    from qapbot.ui_cwl_roster import CwlManagementHubView

    guild_id_str = str(mock_interaction.guild.id)
    CACHE.db_manager = db
    CACHE.server_config[guild_id_str] = {}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    hub_view = CwlManagementHubView()
    channel = MagicMock()
    channel.id = 555666777

    config_view = ChannelConfigurationView(
        guild=mock_interaction.guild,
        clan_management_view=hub_view,
        original_interaction=mock_interaction,
        current_channels={"cwl_management": None},
        slots=CWL_CONFIG_CHANNEL_SLOTS,
        origin_mode="cwl_settings",
    )
    config_view.selected_channels["cwl_management"] = channel
    config_view.config_message = AsyncMock()

    await config_view._on_apply(mock_interaction)  # must not raise

    assert CACHE.server_config[guild_id_str]["cwl_management_channel_id"] == str(channel.id)
    config_view.config_message.delete.assert_awaited_once()


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
async def test_cwl_event_setup_view_apply_persists_and_enters_detail_step(db, mock_interaction):
    """Apply persists the clan selection and moves into the per-clan roster-size/start-time
    editor in the same message — it does NOT refresh the cwl_management parent yet (only
    "Done" on the detail step does that, see test_cwl_event_setup_view_detail_step_*)."""
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

    mock_interaction.edit_original_response = AsyncMock()
    await view._on_apply(mock_interaction)

    event = db.get_cwl_event_sync("333", resolve_current_cwl_season())
    assert event is not None
    clans = db.get_cwl_event_clans_sync(event["id"])
    assert [c["clan_tag"] for c in clans] == ["#CLAN1"]

    assert view.phase == "edit_details"
    assert view.detail_clan_tags == ["#CLAN1"]
    parent.refresh_cwl_view.assert_not_awaited()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_cwl_event_setup_view_detail_step_roster_select_persists(db, mock_interaction):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import CwlEventSetupView

    await _seed_guild_and_clans(db, "555", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}}
    CACHE.server_config["555"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    guild = MagicMock()
    guild.id = 555
    parent = MagicMock()
    parent.refresh_cwl_view = AsyncMock()

    view = CwlEventSetupView(guild=guild, parent_view=parent)
    view.working_clans["#CLAN1"] = {"target_league_rank": None, "roster_size": 15, "tier_order": 0, "cwl_start_at": None}
    mock_interaction.edit_original_response = AsyncMock()
    await view._on_apply(mock_interaction)

    mock_interaction.data = {"values": ["30"]}
    await view._on_detail_roster_select(mock_interaction)
    assert view.working_clans["#CLAN1"]["roster_size"] == 30

    persisted = CACHE.db_manager.get_cwl_event_clans_sync(view.event_id)
    assert persisted[0]["roster_size"] == 30

    # "Done" is what actually hands control back to cwl_management.
    await view._on_detail_done(mock_interaction)
    parent.refresh_cwl_view.assert_awaited_once()
    assert parent.refresh_cwl_view.await_args.args[1] == "cwl_management"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_cwl_event_setup_view_detail_step_start_time_modal(db, mock_interaction):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import CwlEventSetupView, CwlStartTimeModal

    await _seed_guild_and_clans(db, "666", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}}
    CACHE.server_config["666"] = {"member_clans": ["#CLAN1"], "member_families": []}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    guild = MagicMock()
    guild.id = 666
    parent = MagicMock()
    parent.refresh_cwl_view = AsyncMock()

    view = CwlEventSetupView(guild=guild, parent_view=parent)
    view.working_clans["#CLAN1"] = {"target_league_rank": None, "roster_size": 15, "tier_order": 0, "cwl_start_at": None}
    mock_interaction.edit_original_response = AsyncMock()
    await view._on_apply(mock_interaction)

    modal = CwlStartTimeModal(view, "#CLAN1")
    modal.start_time_input._value = "2026-09-05 20:00"
    mock_interaction.response.edit_message = AsyncMock()
    await modal.on_submit(mock_interaction)

    assert view.working_clans["#CLAN1"]["cwl_start_at"] == "2026-09-05T20:00Z"
    persisted = CACHE.db_manager.get_cwl_event_clans_sync(view.event_id)
    assert persisted[0]["cwl_start_at"] == "2026-09-05T20:00Z"
    mock_interaction.response.edit_message.assert_awaited_once()

    # Bad input is rejected without mutating state.
    modal2 = CwlStartTimeModal(view, "#CLAN1")
    modal2.start_time_input._value = "not a date"
    mock_interaction.response.send_message = AsyncMock()
    await modal2.on_submit(mock_interaction)
    assert view.working_clans["#CLAN1"]["cwl_start_at"] == "2026-09-05T20:00Z"
    mock_interaction.response.send_message.assert_awaited_once()


@pytest.mark.discord
def test_cwl_start_time_modal_prefills_static_ingame_schedule_when_unset():
    """CWL sign-ups/war always start on the 1st of the season month at 08:00 UTC — a clan with
    no start time set yet should have the modal's field prefilled with that default, not
    blank, so an admin can just accept it instead of typing the same value every time."""
    from qapbot.ui_cwl_roster import CwlEventSetupView, CwlStartTimeModal, _default_cwl_start_time

    guild = MagicMock()
    guild.id = 888
    view = MagicMock(spec=CwlEventSetupView)
    view.guild = guild
    view.working_clans = {"#CLAN1": {"cwl_start_at": None}}

    modal = CwlStartTimeModal(view, "#CLAN1")

    assert modal.start_time_input.default == _default_cwl_start_time()
    assert _default_cwl_start_time().endswith("-01 08:00")


# ---------------------------------------------------------------------------
# CwlRetentionModal — radio-button retention picker (replaces the inline Select)
# ---------------------------------------------------------------------------

@pytest.mark.discord
def test_cwl_retention_modal_seeds_default_option():
    from qapbot.ui_cwl_roster import CwlRetentionModal

    parent = MagicMock()
    modal = CwlRetentionModal(parent, guild_id=777, current_months=12)

    default_values = {opt.value for opt in modal.radio_group.options if opt.default}
    assert default_values == {"12"}
    assert {opt.value for opt in modal.radio_group.options} == {"0", "3", "6", "12", "24"}


@pytest.mark.discord
@pytest.mark.asyncio
async def test_cwl_retention_modal_persists_selection(db, mock_interaction):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import CwlRetentionModal

    guild_id_str = str(mock_interaction.guild.id)
    CACHE.db_manager = db
    CACHE.server_config[guild_id_str] = {}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}

    parent = MagicMock()
    parent.refresh_cwl_view = AsyncMock()

    modal = CwlRetentionModal(parent, mock_interaction.guild.id, current_months=0)
    modal.radio_group._value = "12"

    await modal.on_submit(mock_interaction)

    assert CACHE.server_config[guild_id_str]["cwl_retention_months"] == 12
    persisted = await db.get_guild_config(guild_id_str)
    assert persisted is not None and persisted.get("cwl_retention_months") == 12
    parent.refresh_cwl_view.assert_awaited_once()
    assert parent.refresh_cwl_view.await_args.args[1] == "cwl_settings"


# ---------------------------------------------------------------------------
# CACHE.get_clan_war_league — CWL tier is CoC-defined, not admin-set
# ---------------------------------------------------------------------------

@pytest.mark.discord
def test_get_clan_war_league_reads_from_clan_name_cache():
    from qapbot.cache_manager import CACHE

    CACHE.clan_name_cache = {
        "#CLAN1": {"name": "Alpha", "war_league": "Crystal League I"},
        "#CLAN2": {"name": "Beta"},  # never synced a war_league yet
        "#CLAN3": "LegacyStringFormat",  # pre-dict cache format
    }

    assert CACHE.get_clan_war_league("#CLAN1") == "Crystal League I"
    assert CACHE.get_clan_war_league("#CLAN2") is None
    assert CACHE.get_clan_war_league("#CLAN3") is None
    assert CACHE.get_clan_war_league("#UNKNOWN", default="fallback") == "fallback"


# ---------------------------------------------------------------------------
# "Delete Season" — CwlDeleteSeasonConfirmView
# ---------------------------------------------------------------------------

@pytest.mark.discord
def test_cwl_management_delete_button_disabled_without_event():
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import add_cwl_management_components

    CACHE.server_config["333"] = {}
    CACHE.db_manager = None  # no event configured -> disabled
    view = discord.ui.View(timeout=300)

    add_cwl_management_components(view, 333)

    delete_button = next(c for c in view.children if getattr(c, "custom_id", None) == "cwl_management_delete_season")
    assert delete_button.disabled is True  # type: ignore[union-attr]


@pytest.mark.discord
@pytest.mark.asyncio
async def test_cwl_delete_season_confirm_view_confirm_deletes_and_refreshes(db, mock_interaction):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import CwlDeleteSeasonConfirmView

    await _seed_guild_and_clans(db, "444", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["444"] = {}

    event_id = db.create_cwl_event_sync("444", "2026-09", "discordid1")

    parent = MagicMock()
    parent.refresh_cwl_view = AsyncMock()

    confirm_view = CwlDeleteSeasonConfirmView(parent_view=parent, guild_id=444, event_id=event_id, season="2026-09")
    assert "2026-09" in confirm_view._build_content()

    await confirm_view._on_confirm(mock_interaction)

    assert db.get_cwl_event_sync("444", "2026-09") is None
    mock_interaction.delete_original_response.assert_awaited_once()
    parent.refresh_cwl_view.assert_awaited_once()
    assert parent.refresh_cwl_view.await_args.args[1] == "cwl_management"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_cwl_delete_season_confirm_view_cancel_does_not_delete(db, mock_interaction):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import CwlDeleteSeasonConfirmView

    await _seed_guild_and_clans(db, "555", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["555"] = {}

    event_id = db.create_cwl_event_sync("555", "2026-09", "discordid1")

    parent = MagicMock()
    parent.refresh_cwl_view = AsyncMock()
    confirm_view = CwlDeleteSeasonConfirmView(parent_view=parent, guild_id=555, event_id=event_id, season="2026-09")

    await confirm_view._on_cancel(mock_interaction)

    assert db.get_cwl_event_sync("555", "2026-09") is not None
    mock_interaction.delete_original_response.assert_awaited_once()
    parent.refresh_cwl_view.assert_not_awaited()


# ---------------------------------------------------------------------------
# "Open Clan Config (Web)" — LAUNCH_ACTIVITY interaction-response callback
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_cwl_management_open_web_callback_sends_launch_activity(mock_interaction):
    from qapbot.ui_cwl_roster import _make_cwl_management_open_web_callback

    mock_interaction.id = 123456789
    mock_interaction.token = "test-token"
    callback = _make_cwl_management_open_web_callback(MagicMock())

    await callback(mock_interaction)

    mock_interaction.client.http.request.assert_awaited_once()
    args, kwargs = mock_interaction.client.http.request.await_args
    route = args[0]
    assert route.method == "POST"
    assert route.url == "https://discord.com/api/v10/interactions/123456789/test-token/callback"
    assert kwargs["json"] == {"type": 12, "data": {}}  # 12 = LAUNCH_ACTIVITY


@pytest.mark.discord
@pytest.mark.asyncio
async def test_cwl_management_open_web_callback_falls_back_if_launch_activity_rejected(mock_interaction):
    """If Discord ever rejects LAUNCH_ACTIVITY from a plain component interaction (the risk
    CWL_CLAN_CONFIG_ACTIVITY_PLAN.md flagged as unverified for this path), admins should get a
    clear ephemeral hint instead of a silently dead button."""
    from qapbot.ui_cwl_roster import _make_cwl_management_open_web_callback

    mock_interaction.client.http.request = AsyncMock(side_effect=RuntimeError("boom"))
    mock_interaction.response.is_done = MagicMock(return_value=False)
    mock_interaction.response.send_message = AsyncMock()

    callback = _make_cwl_management_open_web_callback(MagicMock())
    await callback(mock_interaction)

    mock_interaction.response.send_message.assert_awaited_once()


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
