"""Tests for the CWL roster planning UI layer (CWL_ROSTER_PLANNING_PLAN.md Phase 1):
the shared cwl_settings/cwl_management content layer, both entry points
(ClanManagementView's mode dropdown and CwlManagementHubView), and the
"Configure Participating Clans" button's LAUNCH_ACTIVITY callback (the web Activity is now
the sole clan-config entry point; the native toggle-and-carry-over flow was retired).
"""
from __future__ import annotations

import os
from typing import Any, Dict, List
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
# notify_cwl_clan_shared — ownership-message honesty (2026-08-18)
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_notify_cwl_clan_shared_reports_unresolved_ownership_honestly(monkeypatch):
    """Bug fixed 2026-08-18 (live-tested in DEV, project owner's report, verbatim: "the message
    is wrong. akatsuki doesn't have a leader in our guild!! but it doesn't have the leader in the
    other guild as well. is this a race condition?"). NOT a race condition — resolve_cwl_clan_
    owner() (QBdiscocmdshelper_cwl.py) already correctly detects "no resolvable Leader/Co-Leader
    anywhere" and returns owner_resolution_method='unresolved_first_claimer'; this notification
    just always claimed "real in-game Leader/Co-Leader" ownership regardless of that."""
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import notify_cwl_clan_shared

    CACHE.clan_name_cache = {"#AKATSUKI": {"name": "!!AKATSUKI!!"}}

    posted = []

    async def fake_post(guild_id, message, discord_id):
        posted.append((guild_id, message))

    monkeypatch.setattr("qapbot.ui_cwl_roster._post_cwl_shared_clan_notice", fake_post)

    import QBcore
    fake_guild = MagicMock()
    fake_guild.name = "CoC | Stay"
    monkeypatch.setattr(QBcore, "bot", MagicMock(get_guild=MagicMock(return_value=fake_guild)))

    await notify_cwl_clan_shared(
        acting_guild_id=100,
        clan_tag="#AKATSUKI",
        season="2026-09",
        sharing_result={
            "shared_clan_id": 1,
            "owner_guild_id": "100",
            "owner_resolution_method": "unresolved_first_claimer",
            "is_new": True,
            "other_guild_ids": ["200"],
        },
        acting_discord_id=42,
    )

    acting_message = posted[0][1]
    assert "recognized owner" not in acting_message  # the fix — no false ownership claim
    assert "unresolved" in acting_message.lower()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_notify_cwl_clan_shared_still_names_a_real_resolved_owner(monkeypatch):
    """Sibling case — a genuinely resolved owner (leader/co-leader found) must still get the
    original, accurate ownership claim, unaffected by the unresolved-case fix."""
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import notify_cwl_clan_shared

    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha"}}

    posted = []

    async def fake_post(guild_id, message, discord_id):
        posted.append((guild_id, message))

    monkeypatch.setattr("qapbot.ui_cwl_roster._post_cwl_shared_clan_notice", fake_post)

    import QBcore
    fake_guild = MagicMock()
    fake_guild.name = "Other Guild"
    monkeypatch.setattr(QBcore, "bot", MagicMock(get_guild=MagicMock(return_value=fake_guild)))

    await notify_cwl_clan_shared(
        acting_guild_id=100,
        clan_tag="#CLAN1",
        season="2026-09",
        sharing_result={
            "shared_clan_id": 1,
            "owner_guild_id": "100",
            "owner_resolution_method": "leader_verified",
            "is_new": True,
            "other_guild_ids": ["200"],
        },
        acting_discord_id=42,
    )

    acting_message = posted[0][1]
    assert "real in-game Leader/Co-Leader" in acting_message
    assert "recognized owner" in acting_message


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

    # mode select + refresh + channel/hub-toggle/player-hub-toggle/retention/
    # include-all-accounts-toggle buttons
    assert len(view.children) == 7


@pytest.mark.discord
def test_player_hub_toggle_button_present_disabled_state():
    """plans/cwl-personal-hub.md Phase 2b — presence/label/style when the Player CWL Settings
    Hub is currently disabled (the default for every guild)."""
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import add_cwl_settings_components

    CACHE.server_config["557"] = {"cwl_player_hub_message_enabled": False}
    view = discord.ui.View(timeout=None)

    add_cwl_settings_components(view, 557)

    button = next(c for c in view.children if getattr(c, "custom_id", None) == "cwl_settings_toggle_player_hub")
    assert button.label == "Activate Player CWL Settings Hub Message"
    assert button.style == discord.ButtonStyle.secondary


@pytest.mark.discord
def test_player_hub_toggle_button_present_enabled_state():
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import add_cwl_settings_components

    CACHE.server_config["558"] = {"cwl_player_hub_message_enabled": True}
    view = discord.ui.View(timeout=None)

    add_cwl_settings_components(view, 558)

    button = next(c for c in view.children if getattr(c, "custom_id", None) == "cwl_settings_toggle_player_hub")
    assert button.label == "Deactivate Player CWL Settings Hub Message"
    assert button.style == discord.ButtonStyle.success


@pytest.mark.discord
@pytest.mark.asyncio
async def test_player_hub_toggle_guards_when_no_channel_set(db, mock_interaction):
    """Attempting to enable with no channel configured yet must not flip the flag — same guard
    the admin hub toggle already has."""
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import add_cwl_settings_components

    guild_id_str = str(mock_interaction.guild.id)
    CACHE.db_manager = db
    CACHE.server_config[guild_id_str] = {}  # no cwl_player_hub_channel_id
    mock_interaction.response.defer = AsyncMock()
    mock_interaction.followup.send = AsyncMock()

    view = discord.ui.View(timeout=None)
    add_cwl_settings_components(view, mock_interaction.guild.id)
    button = next(c for c in view.children if getattr(c, "custom_id", None) == "cwl_settings_toggle_player_hub")

    await button.callback(mock_interaction)

    assert CACHE.server_config[guild_id_str].get("cwl_player_hub_message_enabled", False) is False
    mock_interaction.followup.send.assert_awaited_once()
    args, kwargs = mock_interaction.followup.send.call_args
    assert "Player CWL Settings Hub channel" in args[0]
    assert kwargs.get("ephemeral") is True


@pytest.mark.discord
@pytest.mark.asyncio
async def test_player_hub_toggle_flips_flag_and_persists_when_channel_set(db, mock_interaction, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import add_cwl_settings_components

    guild_id_str = str(mock_interaction.guild.id)
    CACHE.db_manager = db
    CACHE.server_config[guild_id_str] = {"cwl_player_hub_channel_id": "999888777"}
    CACHE.subscriptions = {}
    CACHE.clan_families = {}
    mock_interaction.response.defer = AsyncMock()

    # repost_cwl_player_hub_messages doesn't exist until Phase 3 lands later in this same run;
    # the toggle callback already tolerates that import failing (try/except, logs a warning) —
    # this test only asserts the flag flip + persistence, not the repost side effect.
    view = discord.ui.View(timeout=None)
    add_cwl_settings_components(view, mock_interaction.guild.id)
    button = next(c for c in view.children if getattr(c, "custom_id", None) == "cwl_settings_toggle_player_hub")

    await button.callback(mock_interaction)

    assert CACHE.server_config[guild_id_str]["cwl_player_hub_message_enabled"] is True
    persisted = await db.get_guild_config(guild_id_str)
    assert persisted["cwl_player_hub_message_enabled"] is True


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

    # mode select + refresh + configure(web)/start(disabled)/delete/add_season — the row-3 slot
    # is a single dynamically-labeled start/manage button now (slice 5), not two side by side.
    # (no season select: CACHE.db_manager is None here, so there are no events to list)
    assert len(view.children) == 6


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
def test_cwl_player_hub_channel_slot_registered():
    """plans/cwl-personal-hub.md Phase 2a — the "Configure Channels" button on the cwl_settings
    screen needs zero new select/button/handler code for a new slot, per ChannelSlotConfig's own
    docstring guarantee; this is the data-only change that adds it."""
    from qapbot.ui_clan_management import CWL_CONFIG_CHANNEL_SLOTS, DEFAULT_CHANNEL_SLOTS

    slot = next((s for s in DEFAULT_CHANNEL_SLOTS if s.key == "cwl_player_hub"), None)
    assert slot is not None
    assert slot.config_key == "cwl_player_hub_channel_id"
    assert slot.disable_flag_keys == ("cwl_player_hub_message_enabled",)

    # Both CWL hub slots must be offered together on the cwl_settings screen's "Configure
    # Channels" button — this is what makes the second channel-select row appear automatically.
    assert {s.key for s in CWL_CONFIG_CHANNEL_SLOTS} == {"cwl_management", "cwl_player_hub"}


@pytest.mark.discord
def test_cwl_player_hub_channel_change_is_tracked():
    from qapbot.cache_manager import CACHE
    from qapbot.ui_clan_management import _track_cwl_player_hub_channel_change

    guild_id_str = "888"
    CACHE.server_config[guild_id_str] = {}

    _track_cwl_player_hub_channel_change(guild_id_str, "111", "222")

    assert CACHE.server_config[guild_id_str]["_old_cwl_player_hub_channel_id"] == "111"


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
    assert len(view.children) == 3
    custom_ids = {c.custom_id for c in view.children}  # type: ignore[attr-defined]
    assert custom_ids == {"cwl_admin_hub_mode_settings", "cwl_admin_hub_mode_management", "cwl_admin_hub_refresh"}


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
async def test_cwl_management_hub_view_refresh_button_rerenders_current_mode(monkeypatch):
    """Last-resort manual fallback (2026-08-10) — clicking it must re-render whichever mode is
    currently shown, not always default back to cwl_management."""
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import CwlManagementHubView

    CACHE.db_manager = None
    CACHE.server_config["778"] = {}

    interaction = MagicMock()
    interaction.guild = MagicMock()
    interaction.guild.id = 778
    interaction.response = AsyncMock()

    async def _always_admin(*args, **kwargs):
        return True

    import qapbot.ui_cwl_roster as ui_cwl_roster_module
    monkeypatch.setattr(ui_cwl_roster_module, "_check_cwl_admin_permission", _always_admin)

    view = CwlManagementHubView()
    view._render = AsyncMock()  # type: ignore[method-assign]
    refresh_button = next(c for c in view.children if getattr(c, "custom_id", None) == "cwl_admin_hub_refresh")

    await refresh_button.callback(interaction)  # type: ignore[misc]

    view._render.assert_awaited_once_with(interaction, "cwl_management")


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
# cwl_league_rank / cwl_start_at_discord_timestamp / resolve_selected_cwl_season (Phase E)
# ---------------------------------------------------------------------------

@pytest.mark.discord
def test_cwl_league_rank_orders_highest_first():
    from qapbot.QBdiscocmdshelper_cwl import cwl_league_rank

    assert cwl_league_rank("Legend League") > cwl_league_rank("Titan League I")
    assert cwl_league_rank("Titan League I") > cwl_league_rank("Champion League I")
    assert cwl_league_rank("Bronze League III") > cwl_league_rank(None)
    assert cwl_league_rank("Bronze League III") > cwl_league_rank("Not A Real League")
    assert cwl_league_rank(None) == cwl_league_rank("Not A Real League")


@pytest.mark.discord
def test_cwl_start_at_discord_timestamp_renders_native_markup():
    from qapbot.QBdiscocmdshelper_cwl import cwl_start_at_discord_timestamp

    result = cwl_start_at_discord_timestamp("2026-09-01T08:00Z")
    assert result is not None
    assert result.startswith("<t:") and result.endswith(":f>")

    assert cwl_start_at_discord_timestamp(None) is None
    assert cwl_start_at_discord_timestamp("not a date") is None


def test_timezone_abbreviation_reflects_dst_state_at_season_start():
    from qapbot.QBdiscocmdshelper_cwl import timezone_abbreviation

    assert timezone_abbreviation("Europe/Berlin", "2026-09") == "CEST"  # summer -> DST active
    assert timezone_abbreviation("Europe/Berlin", "2026-12") == "CET"   # winter -> DST inactive
    assert timezone_abbreviation("Asia/Kolkata", "2026-09") == "IST"    # no DST, fixed +5:30
    assert timezone_abbreviation("UTC", "2026-09") == "UTC"


def test_timezone_abbreviation_falls_back_to_the_raw_name_on_bad_input():
    from qapbot.QBdiscocmdshelper_cwl import timezone_abbreviation

    assert timezone_abbreviation("Not/A_Real_Zone", "2026-09") == "Not/A_Real_Zone"
    assert timezone_abbreviation("UTC", "not-a-season") == "UTC"


@pytest.mark.discord
def test_resolve_selected_cwl_season_prefers_persisted_selection(db):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_selected_cwl_season

    CACHE.db_manager = db
    CACHE.server_config["9999"] = {"cwl_selected_season": "2026-02"}

    assert resolve_selected_cwl_season(9999) == "2026-02"


@pytest.mark.discord
def test_resolve_selected_cwl_season_falls_back_without_persisted_selection():
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_current_cwl_season, resolve_selected_cwl_season

    CACHE.db_manager = None
    CACHE.server_config["8887"] = {}

    assert resolve_selected_cwl_season(8887) == resolve_current_cwl_season()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_format_clan_management_cwl_management_sorts_by_tier_and_renders_compact_table(db):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import format_clan_management_cwl_management

    await _seed_guild_and_clans(db, "6543", {"#CLAN1": "Bronze Clan", "#CLAN2": "Champion Clan"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {
        "#CLAN1": {"name": "Bronze Clan", "war_league": "Bronze League III"},
        "#CLAN2": {"name": "Champion Clan", "war_league": "Champion League I"},
    }
    CACHE.server_config["6543"] = {}

    event_id = db.create_cwl_event_sync("6543", "2026-05", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [
        {"clan_tag": "#CLAN1", "roster_size": 15, "cwl_start_at": "2026-05-01T08:00Z", "participating": True},
        {"clan_tag": "#CLAN2", "roster_size": 15, "cwl_start_at": "2026-05-01T08:00Z", "participating": True},
    ])

    guild = MagicMock()
    guild.id = 6543

    embed, _, _, _ = await format_clan_management_cwl_management(guild)

    clans_field = next(f for f in embed.fields if "Clan" in f.name)
    # Champion (higher tier) must be listed before Bronze.
    assert clans_field.value.index("Champion Clan") < clans_field.value.index("Bronze Clan")
    # Monospaced code-block table, not bullet lines.
    assert clans_field.value.startswith("```") and clans_field.value.endswith("```")
    # Clan tag dropped, "League" dropped from the per-clan tier value (but not the "League"
    # column header itself), compact "YY-MM-DD HH:MM" start time (UTC by default).
    assert "#CLAN1" not in clans_field.value and "#CLAN2" not in clans_field.value
    assert "Champion League I" not in clans_field.value and "Bronze League III" not in clans_field.value
    assert "Champion I" in clans_field.value
    assert "CWL Start (UTC)" in clans_field.value
    assert "26-05-01 08:00" in clans_field.value


@pytest.mark.discord
@pytest.mark.asyncio
async def test_format_clan_management_cwl_management_shifts_start_time_by_guild_timezone(db):
    """The table can't use Discord's native per-viewer <t:...> markup (not parsed inside code
    blocks), so it falls back to the guild's one configured timezone_name instead."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import format_clan_management_cwl_management

    await _seed_guild_and_clans(db, "6544", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {"#CLAN1": {"name": "Alpha", "war_league": "Master League I"}}
    CACHE.server_config["6544"] = {"timezone_name": "Asia/Kolkata"}  # fixed UTC+5:30, no DST

    event_id = db.create_cwl_event_sync("6544", "2026-05", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [
        {"clan_tag": "#CLAN1", "roster_size": 15, "cwl_start_at": "2026-05-01T10:00Z", "participating": True},
    ])

    guild = MagicMock()
    guild.id = 6544

    embed, _, _, _ = await format_clan_management_cwl_management(guild)

    clans_field = next(f for f in embed.fields if "Clan" in f.name)
    assert "CWL Start (IST)" in clans_field.value  # abbreviation, not the full zone name
    assert "26-05-01 15:30" in clans_field.value  # 10:00 UTC + 5:30


@pytest.mark.discord
@pytest.mark.asyncio
async def test_format_clan_management_cwl_management_applies_dst_correctly(db):
    """The whole point of a real IANA zone (over a raw UTC offset) is DST-awareness — a summer
    start (CEST, UTC+2) and a winter start (CET, UTC+1) in the same zone must shift differently."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import format_clan_management_cwl_management

    await _seed_guild_and_clans(db, "6545", {"#CLAN1": "Alpha", "#CLAN2": "Bravo"})
    CACHE.db_manager = db
    CACHE.clan_name_cache = {
        "#CLAN1": {"name": "Alpha", "war_league": "Master League I"},
        "#CLAN2": {"name": "Bravo", "war_league": "Master League I"},
    }
    CACHE.server_config["6545"] = {"timezone_name": "Europe/Berlin"}

    event_id = db.create_cwl_event_sync("6545", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [
        {"clan_tag": "#CLAN1", "roster_size": 15, "cwl_start_at": "2026-09-01T10:00Z", "participating": True},  # CEST (+2)
        {"clan_tag": "#CLAN2", "roster_size": 15, "cwl_start_at": "2026-11-01T10:00Z", "participating": True},  # CET (+1)
    ])

    guild = MagicMock()
    guild.id = 6545

    embed, _, _, _ = await format_clan_management_cwl_management(guild)

    clans_field = next(f for f in embed.fields if "Clan" in f.name)
    assert "26-09-01 12:00" in clans_field.value  # summer: UTC+2
    assert "26-11-01 11:00" in clans_field.value  # winter: UTC+1


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
async def test_cwl_delete_season_confirm_view_retracts_stale_enrollment_dms(db, mock_interaction, monkeypatch):
    """2026-08-19 live bug report: Delete Season left every recipient's Confirm/Opt Out DM
    buttons sitting live-looking (though no longer functional) in their DMs. _on_confirm must
    snapshot the DM refs BEFORE delete_cwl_event_sync() clears cwl_player_season_status (see that
    function's own docstring), then best-effort retract the actual DM messages."""
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import CwlDeleteSeasonConfirmView

    await _seed_guild_and_clans(db, "448", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["448"] = {}

    event_id = db.create_cwl_event_sync("448", "2026-09", "discordid1")
    db.mark_cwl_player_dm_sent_sync(
        "#P1", "2026-09", "PlayerOne", "10", event_id, 448, "2026-08-19T09:00Z", "msg1", "chan1",
    )

    parent = MagicMock()
    parent.refresh_cwl_view = AsyncMock()
    confirm_view = CwlDeleteSeasonConfirmView(parent_view=parent, guild_id=448, event_id=event_id, season="2026-09")

    cleanup_mock = AsyncMock(return_value={"deleted": 1, "failed": 0})
    monkeypatch.setattr("qapbot.QBdiscocmdshelper_cwl.cleanup_stale_cwl_enrollment_dms", cleanup_mock)

    await confirm_view._on_confirm(mock_interaction)

    cleanup_mock.assert_awaited_once()
    call_args = cleanup_mock.await_args
    assert call_args.args[0] is mock_interaction.client
    assert call_args.args[1] == [
        {"player_tag": "#P1", "dmed_discord_id": "10", "message_id": "msg1", "channel_id": "chan1"}
    ]
    # The dm_sent record it referenced is now gone too (delete_cwl_event_sync's own cleanup).
    assert db.get_cwl_player_season_status_sync("#P1", "2026-09") is None


@pytest.mark.discord
@pytest.mark.asyncio
async def test_cwl_delete_season_confirm_view_skips_cleanup_when_nobody_was_dmed(db, mock_interaction, monkeypatch):
    """No dm_refs to retract — the Discord-API cleanup call must not even be attempted."""
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import CwlDeleteSeasonConfirmView

    await _seed_guild_and_clans(db, "449", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["449"] = {}

    event_id = db.create_cwl_event_sync("449", "2026-09", "discordid1")

    parent = MagicMock()
    parent.refresh_cwl_view = AsyncMock()
    confirm_view = CwlDeleteSeasonConfirmView(parent_view=parent, guild_id=449, event_id=event_id, season="2026-09")

    cleanup_mock = AsyncMock()
    monkeypatch.setattr("qapbot.QBdiscocmdshelper_cwl.cleanup_stale_cwl_enrollment_dms", cleanup_mock)

    await confirm_view._on_confirm(mock_interaction)

    cleanup_mock.assert_not_awaited()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_cwl_delete_season_confirm_view_warns_about_shared_clans(db, mock_interaction):
    """2026-08-15 (delete-season guard) — a clan shared with another guild shows up in the
    warning text, but deleting still proceeds: this guild's own event is fully removed while
    the OTHER guild's attachment to the shared clan (and its roster) survives untouched."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import get_cwl_event_shared_clan_info_sync
    from qapbot.ui_cwl_roster import CwlDeleteSeasonConfirmView

    await _seed_guild_and_clans(db, "446", {"#CLAN1": "Alpha"})
    await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES ('447')")
    await db.conn.commit()
    CACHE.db_manager = db
    CACHE.server_config["446"] = {}

    event_id = db.create_cwl_event_sync("446", "2026-09", "discordid1")
    other_event_id = db.create_cwl_event_sync("447", "2026-09", "otherdiscordid")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    shared_clan_id = db.create_cwl_shared_clan_sync("#CLAN1", "2026-09", "446", event_id, "unresolved_first_claimer")
    db.add_guild_to_shared_clan_sync(shared_clan_id, "446", event_id)
    db.add_guild_to_shared_clan_sync(shared_clan_id, "447", other_event_id)
    db.set_cwl_shared_clan_player_status_sync(shared_clan_id, "#P1", "Player", "111", "confirmed", "guest_invite", "446")

    shared_clan_info = get_cwl_event_shared_clan_info_sync(event_id, 446, "2026-09")
    parent = MagicMock()
    parent.refresh_cwl_view = AsyncMock()
    confirm_view = CwlDeleteSeasonConfirmView(
        parent_view=parent, guild_id=446, event_id=event_id, season="2026-09", shared_clan_info=shared_clan_info,
    )
    assert "#CLAN1" in confirm_view._build_content()

    await confirm_view._on_confirm(mock_interaction)

    assert db.get_cwl_event_sync("446", "2026-09") is None  # this guild's own event is gone
    shared = db.get_cwl_shared_clan_by_id_sync(shared_clan_id)
    assert shared is not None  # shared record survives — guild 447 still attached
    assert shared["owner_guild_id"] == "447"  # repointed away from the deleting guild
    remaining_guilds = {g["guild_id"] for g in db.list_cwl_shared_clan_guilds_sync(shared_clan_id)}
    assert remaining_guilds == {"447"}
    assert db.get_cwl_shared_clan_players_sync(shared_clan_id) != []  # roster preserved


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
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import _make_cwl_management_open_web_callback

    mock_interaction.id = 123456789
    mock_interaction.token = "test-token"
    mock_interaction.guild.id = 111222
    mock_interaction.user.id = 333444
    callback = _make_cwl_management_open_web_callback(MagicMock())

    await callback(mock_interaction)

    mock_interaction.client.http.request.assert_awaited_once()
    args, kwargs = mock_interaction.client.http.request.await_args
    route = args[0]
    assert route.method == "POST"
    assert route.url == "https://discord.com/api/v10/interactions/123456789/test-token/callback"
    assert kwargs["json"] == {"type": 12, "data": {}}  # 12 = LAUNCH_ACTIVITY
    # Recorded *before* the LAUNCH_ACTIVITY call so the Activity's first fetch (GET
    # /api/cwl/screen) sees it regardless of how quickly the client races that request.
    assert CACHE.pending_cwl_activity_screen[("111222", "333444")] == "clan_config"


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


# ---------------------------------------------------------------------------
# "Manage Assignment" — same LAUNCH_ACTIVITY mechanism, "enrollment" screen (slice 5)
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_cwl_management_open_enrollment_web_callback_sends_launch_activity(mock_interaction):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import _make_cwl_management_open_enrollment_web_callback

    mock_interaction.id = 987654321
    mock_interaction.token = "test-token-2"
    mock_interaction.guild.id = 555666
    mock_interaction.user.id = 777888
    callback = _make_cwl_management_open_enrollment_web_callback(MagicMock())

    await callback(mock_interaction)

    mock_interaction.client.http.request.assert_awaited_once()
    args, kwargs = mock_interaction.client.http.request.await_args
    route = args[0]
    assert route.method == "POST"
    assert route.url == "https://discord.com/api/v10/interactions/987654321/test-token-2/callback"
    assert kwargs["json"] == {"type": 12, "data": {}}  # 12 = LAUNCH_ACTIVITY
    assert CACHE.pending_cwl_activity_screen[("555666", "777888")] == "enrollment"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_cwl_management_open_enrollment_web_callback_falls_back_if_launch_activity_rejected(mock_interaction):
    from qapbot.ui_cwl_roster import _make_cwl_management_open_enrollment_web_callback

    mock_interaction.client.http.request = AsyncMock(side_effect=RuntimeError("boom"))
    mock_interaction.response.is_done = MagicMock(return_value=False)
    mock_interaction.response.send_message = AsyncMock()

    callback = _make_cwl_management_open_enrollment_web_callback(MagicMock())
    await callback(mock_interaction)

    mock_interaction.response.send_message.assert_awaited_once()
    args, _ = mock_interaction.response.send_message.call_args
    assert "enrollment" in args[0].lower()


# ---------------------------------------------------------------------------
# "Add New Season" — season creation + the exclusively-here carry-over prompt
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_add_season_creates_event_directly_when_no_previous_data(db, mock_interaction):
    """2026-08-16 follow-up, live-testing feedback, project owner's spec: "after adding a new
    season we should add a logic that the Configure Participating Clans view is opened
    automatically as a logical consequence of adding a new season" — verified here via the
    LAUNCH_ACTIVITY interaction-response callback, same mechanism/assertion pattern as
    test_cwl_management_open_web_callback_sends_launch_activity below."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_current_cwl_season
    from qapbot.ui_cwl_roster import _make_cwl_management_add_season_callback

    await _seed_guild_and_clans(db, "1111", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["1111"] = {}
    mock_interaction.id = 42
    mock_interaction.token = "test-token"
    mock_interaction.guild.id = 1111
    mock_interaction.user.id = 999

    parent = MagicMock()
    parent.refresh_cwl_view = AsyncMock()

    callback = _make_cwl_management_add_season_callback(parent)
    await callback(mock_interaction)

    season = resolve_current_cwl_season()
    assert db.get_cwl_event_sync("1111", season) is not None
    assert CACHE.server_config["1111"]["cwl_selected_season"] == season
    mock_interaction.client.http.request.assert_awaited_once()
    _, kwargs = mock_interaction.client.http.request.await_args
    assert kwargs["json"] == {"type": 12, "data": {}}  # 12 = LAUNCH_ACTIVITY
    assert CACHE.pending_cwl_activity_screen[("1111", "999")] == "clan_config"
    # No carry-over data existed, so no ephemeral prompt should have been sent either.
    mock_interaction.followup.send.assert_not_awaited()
    # 2026-08-16 regression fix: the screen that hosted this button must refresh itself too, not
    # rely solely on the Hub-only refresh POST /api/cwl/activity-closed triggers once the
    # auto-launched Activity closes — see _make_cwl_management_add_season_callback's own comment.
    parent.refresh_cwl_view.assert_awaited_once()
    assert parent.refresh_cwl_view.await_args.args[1] == "cwl_management"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_add_season_auto_enables_family_clans_when_no_previous_data_at_all(db, mock_interaction):
    """2026-08-16 follow-up, live-testing feedback, project owner's spec, verbatim: "just created
    a new season after restarting the dev bot and still all clans unchecked! you said you fixed
    this but your fix doesn't work." The first fix only covered CwlCarryOverPromptView's "Yes"
    carry-over path — it never touched THIS branch, which fires whenever
    get_previous_cwl_event_clans_sync() finds no previously-participating rows at all (a
    genuinely brand-new guild, or a guild whose prior season also had zero clans enabled) and
    skips the carry-over prompt entirely, leaving zero cwl_event_clans rows — the exact same
    "nothing checked" symptom via a completely different code path."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_current_cwl_season
    from qapbot.ui_cwl_roster import _make_cwl_management_add_season_callback

    await _seed_guild_and_clans(db, "1112", {"#CLAN1": "Alpha", "#CLAN2": "Bravo"})
    CACHE.db_manager = db
    CACHE.server_config["1112"] = {"member_clans": ["#CLAN1", "#CLAN2"]}
    mock_interaction.guild.id = 1112

    parent = MagicMock()
    parent.refresh_cwl_view = AsyncMock()

    callback = _make_cwl_management_add_season_callback(parent)
    await callback(mock_interaction)

    season = resolve_current_cwl_season()
    event = db.get_cwl_event_sync("1112", season)
    assert event is not None
    clans = {c["clan_tag"]: c for c in db.get_cwl_event_clans_sync(event["id"])}
    assert clans["#CLAN1"]["participating"] == 1
    assert clans["#CLAN2"]["participating"] == 1
    # 2026-08-16 follow-up: writing a real row here (needed for participating=True to persist)
    # meant _build_clan_config_payload's "no row -> default start time" fallback no longer
    # applied — the date field must still come pre-filled, not empty.
    assert clans["#CLAN1"]["cwl_start_at"] == f"{season}-01T08:00Z"
    assert clans["#CLAN2"]["cwl_start_at"] == f"{season}-01T08:00Z"
    # No carry-over data existed, so no ephemeral prompt should have been sent either.
    mock_interaction.followup.send.assert_not_awaited()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_add_season_rejects_when_season_already_exists(db, mock_interaction):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_current_cwl_season
    from qapbot.ui_cwl_roster import _make_cwl_management_add_season_callback

    await _seed_guild_and_clans(db, "2222", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["2222"] = {}
    mock_interaction.guild.id = 2222

    season = resolve_current_cwl_season()
    db.create_cwl_event_sync("2222", season, "discordid1")

    mock_interaction.response.send_message = AsyncMock()
    callback = _make_cwl_management_add_season_callback(MagicMock())
    await callback(mock_interaction)

    mock_interaction.response.send_message.assert_awaited_once()
    # Still exactly one event for that season — idempotent create_cwl_event_sync() would have
    # silently no-op'd anyway, but the point is the admin gets told, not left guessing.
    assert len(db.list_cwl_events_sync("2222")) == 1


@pytest.mark.discord
@pytest.mark.asyncio
async def test_add_season_offers_carry_over_prompt_when_previous_data_exists(db, mock_interaction):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_current_cwl_season
    from qapbot.ui_cwl_roster import _make_cwl_management_add_season_callback, CwlCarryOverPromptView

    await _seed_guild_and_clans(db, "3333", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["3333"] = {}
    mock_interaction.guild.id = 3333

    old_event_id = db.create_cwl_event_sync("3333", "2026-01", "discordid1")
    db.set_cwl_event_clans_sync(old_event_id, [
        {"clan_tag": "#CLAN1", "roster_size": 30, "cwl_start_at": "2026-01-01T08:00Z", "participating": True},
    ])

    mock_interaction.followup.send = AsyncMock()
    callback = _make_cwl_management_add_season_callback(MagicMock())
    await callback(mock_interaction)

    season = resolve_current_cwl_season()
    # No event created yet — that only happens once the admin answers Yes/No.
    assert db.get_cwl_event_sync("3333", season) is None
    mock_interaction.followup.send.assert_awaited_once()
    _, kwargs = mock_interaction.followup.send.call_args
    assert isinstance(kwargs.get("view"), CwlCarryOverPromptView)


@pytest.mark.discord
@pytest.mark.asyncio
async def test_cwl_carry_over_prompt_yes_presets_participating_from_real_war_history(db, mock_interaction):
    """2026-08-15 redesign: "Yes" no longer just copies the previous season's manually-toggled
    participating flags — it looks up which family clans actually played CWL last season (real
    war_summary data) and pre-sets true/false across the FULL family, while the other settings
    (roster_size etc.) still carry over from the previous config where one existed."""
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import CwlCarryOverPromptView

    await _seed_guild_and_clans(db, "4444", {"#CLAN1": "Alpha", "#CLAN2": "Bravo"})
    CACHE.db_manager = db
    CACHE.server_config["4444"] = {"member_clans": ["#CLAN1", "#CLAN2"]}
    mock_interaction.id = 42
    mock_interaction.token = "test-token"
    mock_interaction.guild.id = 4444
    mock_interaction.user.id = 999

    # Previous season's admin-configured settings (roster_size etc.) — #CLAN1 only.
    old_event_id = db.create_cwl_event_sync("4444", "2026-01", "discordid1")
    db.set_cwl_event_clans_sync(old_event_id, [
        {"clan_tag": "#CLAN1", "target_league_rank": "Master League II",
         "roster_size": 30, "cwl_start_at": "2026-01-01T08:00Z", "participating": True},
    ])
    # Real CWL war history: #CLAN1 actually played last season, #CLAN2 didn't (no row at all).
    await db.conn.execute(
        "INSERT INTO war_summary (war_id, clan_tag, opponent_tag, is_cwl, cwl_season, date) "
        "VALUES ('w1', '#CLAN1', '#OPP', 1, '2026-01', '2026-01-05T08:00')"
    )
    await db.conn.commit()

    previous_rows = db.get_previous_cwl_event_clans_sync("4444")
    parent = MagicMock()
    parent.refresh_cwl_view = AsyncMock()
    prompt_view = CwlCarryOverPromptView(
        parent_view=parent, guild_id=4444, target_season="2026-02", previous_rows=previous_rows,
    )

    await prompt_view._on_yes(mock_interaction)

    event = db.get_cwl_event_sync("4444", "2026-02")
    assert event is not None
    clans = {c["clan_tag"]: c for c in db.get_cwl_event_clans_sync(event["id"])}
    assert clans["#CLAN1"]["participating"] == 1  # played last season -> True
    assert clans["#CLAN1"]["roster_size"] == 30  # settings still carried over
    assert clans["#CLAN1"]["cwl_start_at"] == "2026-01-01T08:00Z"
    assert clans["#CLAN2"]["participating"] == 0  # didn't play last season -> False
    assert clans["#CLAN2"]["roster_size"] == 15  # never configured before -> plain default
    assert CACHE.server_config["4444"]["cwl_selected_season"] == "2026-02"
    # 2026-08-16 follow-up: "Configure Participating Clans" now auto-opens as the logical next
    # step, via the same LAUNCH_ACTIVITY mechanism the button itself uses — this consumes the
    # interaction's one response slot, so the prompt message is no longer separately deleted/
    # refreshed here (see _finish's own comment for the trade-off).
    mock_interaction.client.http.request.assert_awaited_once()
    _, kwargs = mock_interaction.client.http.request.await_args
    assert kwargs["json"] == {"type": 12, "data": {}}  # 12 = LAUNCH_ACTIVITY
    assert CACHE.pending_cwl_activity_screen[("4444", "999")] == "clan_config"
    # 2026-08-16 regression fix: same as the direct-create path — the screen that hosted "Add New
    # Season" (self.parent_view here) must refresh itself, not rely solely on the Hub-only
    # activity-closed refresh.
    parent.refresh_cwl_view.assert_awaited_once()
    assert parent.refresh_cwl_view.await_args.args[1] == "cwl_management"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_cwl_carry_over_prompt_yes_auto_enables_all_when_none_played_last_season(db, mock_interaction):
    """2026-08-16 follow-up, live-testing feedback, project owner's spec, verbatim: "if after the
    previous cwls no clan in the guild is enabled then auto-enable all clans of the guild. It
    doesn't make sense that no clan is enabled after the season was created." A guild with no
    tracked CWL history at all (brand new to the bot, or simply took a season off) would
    otherwise get a freshly-created season where every single family clan defaults to
    participating=False, handing the admin a table with nothing checked."""
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import CwlCarryOverPromptView

    await _seed_guild_and_clans(db, "4445", {"#CLAN1": "Alpha", "#CLAN2": "Bravo"})
    CACHE.db_manager = db
    CACHE.server_config["4445"] = {"member_clans": ["#CLAN1", "#CLAN2"]}
    mock_interaction.guild.id = 4445

    # No real CWL war history anywhere for either clan — a genuinely fresh/inactive guild.
    previous_rows: List[Dict[str, Any]] = []
    parent = MagicMock()
    parent.refresh_cwl_view = AsyncMock()
    prompt_view = CwlCarryOverPromptView(
        parent_view=parent, guild_id=4445, target_season="2026-02", previous_rows=previous_rows,
    )

    await prompt_view._on_yes(mock_interaction)

    event = db.get_cwl_event_sync("4445", "2026-02")
    assert event is not None
    clans = {c["clan_tag"]: c for c in db.get_cwl_event_clans_sync(event["id"])}
    assert clans["#CLAN1"]["participating"] == 1
    assert clans["#CLAN2"]["participating"] == 1
    # 2026-08-16 follow-up: a clan with no previous settings must still get a sensible default
    # start time, not NULL — once a real row exists, the payload builder's own "no row -> default"
    # fallback no longer applies.
    assert clans["#CLAN1"]["cwl_start_at"] == "2026-02-01T08:00Z"
    assert clans["#CLAN2"]["cwl_start_at"] == "2026-02-01T08:00Z"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_cwl_carry_over_prompt_yes_leaves_real_split_alone(db, mock_interaction):
    """The auto-enable-all fallback must NOT fire when the family is a genuine mix of played/
    didn't-play — that split is real, useful information from actual war history, not a
    degenerate all-False case that needs rescuing."""
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import CwlCarryOverPromptView

    await _seed_guild_and_clans(db, "4446", {"#CLAN1": "Alpha", "#CLAN2": "Bravo"})
    CACHE.db_manager = db
    CACHE.server_config["4446"] = {"member_clans": ["#CLAN1", "#CLAN2"]}
    mock_interaction.guild.id = 4446

    db.create_cwl_event_sync("4446", "2026-01", "discordid1")  # resolves as the previous season
    await db.conn.execute(
        "INSERT INTO war_summary (war_id, clan_tag, opponent_tag, is_cwl, cwl_season, date) "
        "VALUES ('w1', '#CLAN1', '#OPP', 1, '2026-01', '2026-01-05T08:00')"
    )
    await db.conn.commit()

    previous_rows: List[Dict[str, Any]] = []
    parent = MagicMock()
    parent.refresh_cwl_view = AsyncMock()
    prompt_view = CwlCarryOverPromptView(
        parent_view=parent, guild_id=4446, target_season="2026-02", previous_rows=previous_rows,
    )

    await prompt_view._on_yes(mock_interaction)

    event = db.get_cwl_event_sync("4446", "2026-02")
    clans = {c["clan_tag"]: c for c in db.get_cwl_event_clans_sync(event["id"])}
    assert clans["#CLAN1"]["participating"] == 1  # played -> True, as computed
    assert clans["#CLAN2"]["participating"] == 0  # didn't play -> stays False, not rescued


@pytest.mark.discord
@pytest.mark.asyncio
async def test_cwl_carry_over_prompt_no_creates_without_copying(db, mock_interaction):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import CwlCarryOverPromptView

    await _seed_guild_and_clans(db, "5555", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["5555"] = {"member_clans": ["#CLAN1"]}
    mock_interaction.guild.id = 5555

    previous_rows = [{"clan_tag": "#CLAN1", "roster_size": 30, "cwl_start_at": "2026-01-01T08:00Z"}]
    parent = MagicMock()
    parent.refresh_cwl_view = AsyncMock()
    prompt_view = CwlCarryOverPromptView(
        parent_view=parent, guild_id=5555, target_season="2026-02", previous_rows=previous_rows,
    )

    await prompt_view._on_no(mock_interaction)

    event = db.get_cwl_event_sync("5555", "2026-02")
    assert event is not None
    assert db.get_cwl_event_clans_sync(event["id"]) == []
    assert CACHE.server_config["5555"]["cwl_selected_season"] == "2026-02"


# ---------------------------------------------------------------------------
# Season select — persisted selection driving both entry points (Phase E.3)
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_season_select_callback_persists_selection_and_refreshes(db, mock_interaction):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import _make_cwl_management_season_select_callback

    await _seed_guild_and_clans(db, "6666", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["6666"] = {}
    mock_interaction.guild.id = 6666
    mock_interaction.data = {"values": ["2026-03"]}

    parent = MagicMock()
    parent.refresh_cwl_view = AsyncMock()

    callback = _make_cwl_management_season_select_callback(parent)
    await callback(mock_interaction)

    assert CACHE.server_config["6666"]["cwl_selected_season"] == "2026-03"
    parent.refresh_cwl_view.assert_awaited_once()
    assert parent.refresh_cwl_view.await_args.args[1] == "cwl_management"


@pytest.mark.discord
def test_season_select_absent_without_any_events(db):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import add_cwl_management_components

    CACHE.db_manager = db
    CACHE.server_config["7777"] = {}
    view = discord.ui.View(timeout=300)

    add_cwl_management_components(view, 7777)

    assert not any(getattr(c, "custom_id", None) == "cwl_management_season_select" for c in view.children)
    configure_button = next(c for c in view.children if getattr(c, "custom_id", None) == "cwl_management_configure_clans")
    assert configure_button.disabled is True  # type: ignore[union-attr]


@pytest.mark.discord
@pytest.mark.asyncio
async def test_season_select_present_and_configure_enabled_once_a_season_exists(db):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import add_cwl_management_components

    await _seed_guild_and_clans(db, "8888", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["8888"] = {}
    db.create_cwl_event_sync("8888", "2026-04", "discordid1")

    view = discord.ui.View(timeout=300)
    add_cwl_management_components(view, 8888)

    season_select = next(c for c in view.children if getattr(c, "custom_id", None) == "cwl_management_season_select")
    values = {opt.value for opt in season_select.options}  # type: ignore[union-attr]
    assert values == {"2026-04"}
    configure_button = next(c for c in view.children if getattr(c, "custom_id", None) == "cwl_management_configure_clans")
    assert configure_button.disabled is False  # type: ignore[union-attr]


@pytest.mark.discord
@pytest.mark.asyncio
async def test_add_season_button_hidden_when_current_season_already_exists(db):
    """2026-08-16, live-testing feedback, project owner's spec: "when adding a new season is not
    possible, the corresponding button should not be visible" — a deliberate exception to this
    screen's usual "present but greyed out" convention (see button_add_season's own comment)."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_current_cwl_season
    from qapbot.ui_cwl_roster import add_cwl_management_components

    await _seed_guild_and_clans(db, "8889", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["8889"] = {}
    db.create_cwl_event_sync("8889", resolve_current_cwl_season(), "discordid1")

    view = discord.ui.View(timeout=300)
    add_cwl_management_components(view, 8889)

    assert not any(getattr(c, "custom_id", None) == "cwl_management_add_season" for c in view.children)


@pytest.mark.discord
@pytest.mark.asyncio
async def test_add_season_button_visible_when_current_season_does_not_exist(db):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import add_cwl_management_components

    await _seed_guild_and_clans(db, "8890", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["8890"] = {}
    # A past/unrelated season exists, but not the current one — the button must still show.
    db.create_cwl_event_sync("8890", "2026-01", "discordid1")

    view = discord.ui.View(timeout=300)
    add_cwl_management_components(view, 8890)

    assert any(getattr(c, "custom_id", None) == "cwl_management_add_season" for c in view.children)


def _notify_new_members_button(view: discord.ui.View):
    return next(
        (c for c in view.children if getattr(c, "custom_id", None) == "cwl_management_notify_new_members"), None
    )


@pytest.mark.discord
@pytest.mark.asyncio
async def test_notify_new_members_button_absent_while_draft(db):
    """Rule h (2026-08-18, CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md) — the button only makes
    sense once enrollment has actually started (a draft event has no DMs sent yet at all)."""
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import add_cwl_management_components

    await _seed_guild_and_clans(db, "8891", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["8891"] = {"member_clans": ["#CLAN1"], "member_families": []}
    event_id = db.create_cwl_event_sync("8891", "2026-08", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES ('10', '10')")
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag) "
        "VALUES ('10', '#P1', 'Player', 1, '#CLAN1')"
    )
    await db.conn.commit()

    view = discord.ui.View(timeout=300)
    add_cwl_management_components(view, 8891)

    assert _notify_new_members_button(view) is None


@pytest.mark.discord
@pytest.mark.asyncio
async def test_notify_new_members_button_absent_when_everyone_already_dmed(db):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import add_cwl_management_components

    await _seed_guild_and_clans(db, "8892", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["8892"] = {"member_clans": ["#CLAN1"], "member_families": []}
    event_id = db.create_cwl_event_sync("8892", "2026-08", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    db.update_cwl_event_status_sync(event_id, "signup_open")
    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES ('10', '10')")
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag) "
        "VALUES ('10', '#P1', 'Player', 1, '#CLAN1')"
    )
    await db.conn.commit()
    db.mark_cwl_player_dm_sent_sync("#P1", "2026-08", "Player", "10", event_id, 8892, "2026-08-18T09:00Z")

    view = discord.ui.View(timeout=300)
    add_cwl_management_components(view, 8892)

    assert _notify_new_members_button(view) is None


@pytest.mark.discord
@pytest.mark.asyncio
async def test_notify_new_members_button_present_when_someone_missing_dm(db):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import add_cwl_management_components

    await _seed_guild_and_clans(db, "8893", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["8893"] = {"member_clans": ["#CLAN1"], "member_families": []}
    event_id = db.create_cwl_event_sync("8893", "2026-08", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    db.update_cwl_event_status_sync(event_id, "signup_open")
    await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES ('10', '10')")
    await db.conn.execute(
        "INSERT INTO user_players (discord_id, player_tag, player_name, verified, current_clan_tag) "
        "VALUES ('10', '#P1', 'Player', 1, '#CLAN1')"
    )
    await db.conn.commit()
    # Never marked dm_sent — a genuinely new/never-contacted pool member.

    view = discord.ui.View(timeout=300)
    add_cwl_management_components(view, 8893)

    button = _notify_new_members_button(view)
    assert button is not None
    # 2026-08-19, project owner's spec: green (not grey), and below the main action-button line
    # (row 3), not above it.
    assert button.row == 4  # type: ignore[union-attr]
    assert button.style == discord.ButtonStyle.success  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Start Enrollment (Phase 2) — button gating, confirm dialog, DynamicItem DM buttons
# ---------------------------------------------------------------------------

def _start_button(view: discord.ui.View):
    return next(c for c in view.children if getattr(c, "custom_id", None) == "cwl_management_start_enrollment")


@pytest.mark.discord
@pytest.mark.asyncio
async def test_start_enrollment_button_disabled_without_event(db):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import add_cwl_management_components

    CACHE.db_manager = db
    CACHE.server_config["9001"] = {}
    view = discord.ui.View(timeout=300)

    add_cwl_management_components(view, 9001)

    assert _start_button(view).disabled is True  # type: ignore[union-attr]


@pytest.mark.discord
@pytest.mark.asyncio
async def test_start_enrollment_button_disabled_without_participating_clans(db):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import add_cwl_management_components

    await _seed_guild_and_clans(db, "9002", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["9002"] = {}
    db.create_cwl_event_sync("9002", "2026-08", "discordid1")  # no clans configured yet

    view = discord.ui.View(timeout=300)
    add_cwl_management_components(view, 9002)

    assert _start_button(view).disabled is True  # type: ignore[union-attr]


@pytest.mark.discord
@pytest.mark.asyncio
async def test_start_enrollment_button_replaced_by_manage_assignment_once_signup_open(db):
    """Once enrollment has started, the row-3 slot flips to a single dynamically-labeled
    "Manage Assignment" button (CWL_ROSTER_PLANNING_PLAN.md "Manage Enrollment" slice 5) — the
    old "Start Enrollment" custom_id no longer exists at all, rather than sticking around
    disabled."""
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import add_cwl_management_components

    await _seed_guild_and_clans(db, "9003", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["9003"] = {}
    event_id = db.create_cwl_event_sync("9003", "2026-08", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1"}])
    db.update_cwl_event_status_sync(event_id, "signup_open")

    view = discord.ui.View(timeout=300)
    add_cwl_management_components(view, 9003)

    assert not any(getattr(c, "custom_id", None) == "cwl_management_start_enrollment" for c in view.children)
    manage_button = next(c for c in view.children if getattr(c, "custom_id", None) == "cwl_management_manage_assignment")
    assert manage_button.disabled is False  # type: ignore[union-attr]
    assert manage_button.callback is not None  # type: ignore[union-attr]


@pytest.mark.discord
@pytest.mark.asyncio
async def test_start_enrollment_button_enabled_for_draft_event_with_clans(db):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import add_cwl_management_components

    await _seed_guild_and_clans(db, "9004", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["9004"] = {}
    event_id = db.create_cwl_event_sync("9004", "2026-08", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1"}])

    view = discord.ui.View(timeout=300)
    add_cwl_management_components(view, 9004)

    button = _start_button(view)
    assert button.disabled is False  # type: ignore[union-attr]
    assert button.callback is not None  # type: ignore[union-attr]


@pytest.mark.discord
@pytest.mark.asyncio
async def test_start_enrollment_callback_opens_confirm_view(db, mock_interaction):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import CwlStartEnrollmentConfirmView, _make_cwl_management_start_enrollment_callback

    await _seed_guild_and_clans(db, "9005", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["9005"] = {}
    event_id = db.create_cwl_event_sync("9005", "2026-08", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1"}])
    mock_interaction.guild.id = 9005

    mock_interaction.followup.send = AsyncMock()
    callback = _make_cwl_management_start_enrollment_callback(MagicMock())
    await callback(mock_interaction)

    mock_interaction.followup.send.assert_awaited_once()
    _, kwargs = mock_interaction.followup.send.call_args
    view = kwargs.get("view")
    assert isinstance(view, CwlStartEnrollmentConfirmView)
    assert view.season == "2026-08"
    # Nothing enrolled yet — only the confirm dialog was shown.
    assert db.get_cwl_event_sync("9005", "2026-08")["status"] == "draft"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_start_enrollment_confirm_view_confirm_starts_enrollment_and_refreshes(db, mock_interaction, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import CwlStartEnrollmentConfirmView

    await _seed_guild_and_clans(db, "9006", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["9006"] = {}
    event_id = db.create_cwl_event_sync("9006", "2026-08", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1"}])

    parent = MagicMock()
    parent.refresh_cwl_view = AsyncMock()
    confirm_view = CwlStartEnrollmentConfirmView(parent_view=parent, guild_id=9006, season="2026-08")
    assert "2026-08" in confirm_view._build_content()

    mock_interaction.edit_original_response = AsyncMock()
    await confirm_view._on_confirm(mock_interaction)

    assert db.get_cwl_event_sync("9006", "2026-08")["status"] == "signup_open"
    # Buttons disabled + a "processing" edit fired immediately (as the interaction response
    # itself), before the slow DM-sending work — so a double-click can't re-trigger this.
    mock_interaction.response.edit_message.assert_awaited_once()
    _, processing_kwargs = mock_interaction.response.edit_message.call_args
    assert processing_kwargs["view"] is confirm_view
    assert all(item.disabled for item in confirm_view.children)  # type: ignore[union-attr]
    mock_interaction.edit_original_response.assert_awaited_once()
    _, kwargs = mock_interaction.edit_original_response.call_args
    # 2026-08-16 follow-up, live-testing feedback, project owner's spec: "when starting the
    # enrollment process the 'Teams Management' view should be opened automatically after the
    # enrollment start is finished" — true zero-click auto-launch isn't achievable (see
    # CwlOpenEnrollmentView's own docstring), so the completion message carries a one-click
    # "Open Teams Management" follow-up button instead of a bare `view=None`.
    from qapbot.ui_cwl_roster import CwlOpenEnrollmentView

    assert isinstance(kwargs["view"], CwlOpenEnrollmentView)
    assert kwargs["view"].guild_id == 9006
    parent.refresh_cwl_view.assert_awaited_once()
    assert parent.refresh_cwl_view.await_args.args[1] == "cwl_management"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_open_enrollment_view_button_launches_activity(mock_interaction):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import CwlOpenEnrollmentView

    mock_interaction.id = 42
    mock_interaction.token = "test-token"
    mock_interaction.guild.id = 9006
    mock_interaction.user.id = 999

    view = CwlOpenEnrollmentView(guild_id=9006)
    open_button = next(iter(view.children))

    await open_button.callback(mock_interaction)  # type: ignore[misc]

    mock_interaction.client.http.request.assert_awaited_once()
    _, kwargs = mock_interaction.client.http.request.await_args
    assert kwargs["json"] == {"type": 12, "data": {}}  # 12 = LAUNCH_ACTIVITY
    assert CACHE.pending_cwl_activity_screen[("9006", "999")] == "enrollment"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_start_enrollment_confirm_view_cancel_does_not_start(db, mock_interaction):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import CwlStartEnrollmentConfirmView

    await _seed_guild_and_clans(db, "9007", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    CACHE.server_config["9007"] = {}
    event_id = db.create_cwl_event_sync("9007", "2026-08", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1"}])

    parent = MagicMock()
    parent.refresh_cwl_view = AsyncMock()
    confirm_view = CwlStartEnrollmentConfirmView(parent_view=parent, guild_id=9007, season="2026-08")

    await confirm_view._on_cancel(mock_interaction)

    assert db.get_cwl_event_sync("9007", "2026-08")["status"] == "draft"
    mock_interaction.delete_original_response.assert_awaited_once()
    parent.refresh_cwl_view.assert_not_awaited()


# ---------------------------------------------------------------------------
# CwlSignupResponseButton — restart-safe DM confirm/opt-out DynamicItem
# ---------------------------------------------------------------------------

class TestCwlSignupResponseButton:
    def test_template_matches_valid_custom_id(self):
        import re
        from qapbot.ui_cwl_roster import CWL_SIGNUP_RESPONSE_TEMPLATE

        m = re.match(CWL_SIGNUP_RESPONSE_TEMPLATE, "cwl:signup:confirm:42:#ABC12")
        assert m is not None
        assert m.group("action") == "confirm"
        assert m.group("event_id") == "42"
        assert m.group("player_tag") == "#ABC12"

    def test_template_rejects_malformed_custom_id(self):
        import re
        from qapbot.ui_cwl_roster import CWL_SIGNUP_RESPONSE_TEMPLATE

        assert re.match(CWL_SIGNUP_RESPONSE_TEMPLATE, "cwl:signup:maybe:42:#ABC12") is None  # bad action
        assert re.match(CWL_SIGNUP_RESPONSE_TEMPLATE, "cwl:signup:confirm:abc:#ABC12") is None  # non-numeric event_id
        assert re.match(CWL_SIGNUP_RESPONSE_TEMPLATE, "cwl:signup:confirm:42:ABC12") is None  # missing '#'
        assert re.match(CWL_SIGNUP_RESPONSE_TEMPLATE, "not:cwl:at:all") is None

    @pytest.mark.asyncio
    async def test_from_custom_id_reconstructs_state(self):
        import re
        from qapbot.ui_cwl_roster import CWL_SIGNUP_RESPONSE_TEMPLATE, CwlSignupResponseButton

        match = re.match(CWL_SIGNUP_RESPONSE_TEMPLATE, "cwl:signup:optout:7:#ZZZ1")
        item = await CwlSignupResponseButton.from_custom_id(MagicMock(), MagicMock(), match)
        assert item.action == "optout"
        assert item.event_id == 7
        assert item.player_tag == "#ZZZ1"

    @pytest.mark.discord
    @pytest.mark.asyncio
    async def test_confirm_click_updates_signup_and_edits_message(self, db, mock_interaction):
        from qapbot.cache_manager import CACHE
        from qapbot.ui_cwl_roster import CwlSignupResponseButton

        await _seed_guild_and_clans(db, "9101", {"#CLAN1": "Alpha"})
        CACHE.db_manager = db
        event_id = db.create_cwl_event_sync("9101", "2026-08", "discordid1")
        db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1"}])
        db.update_cwl_event_status_sync(event_id, "signup_open")
        db.upsert_cwl_signup_sync(event_id, "#P1", "Alpha", "123456789", None, "template_confirm", "pending")

        mock_interaction.user.id = 123456789  # matches the signup's discord_id
        mock_interaction.response.edit_message = AsyncMock()

        button = CwlSignupResponseButton("confirm", event_id, "#P1")
        await button.callback(mock_interaction)

        signup = db.get_cwl_signup_sync(event_id, "#P1")
        assert signup["status"] == "confirmed"
        assert signup["responded_at"] is not None
        mock_interaction.response.edit_message.assert_awaited_once()
        _, kwargs = mock_interaction.response.edit_message.call_args
        assert kwargs["view"] is None
        # The confirmation must name whose sign-up this was — the DM only ever mentions one
        # player_tag, but a Discord account can have several linked accounts, so restating the
        # name avoids any ambiguity about which one just got confirmed.
        assert "Alpha" in kwargs["content"]

    @pytest.mark.discord
    @pytest.mark.asyncio
    async def test_confirm_click_overwrites_an_auto_confirmed_row(self, db, mock_interaction):
        """plans/cwl-personal-hub.md Phase 4c: a row seeded 'auto_confirmed' by a standing
        opt-in preference must still be fully overridable by the member's own real DM response —
        _apply_cwl_signup_response() doesn't special-case the row's prior status, so a click
        (in either direction) always writes the real answer over whatever seeded it."""
        from qapbot.cache_manager import CACHE
        from qapbot.ui_cwl_roster import CwlSignupResponseButton

        await _seed_guild_and_clans(db, "9109", {"#CLAN1": "Alpha"})
        CACHE.db_manager = db
        event_id = db.create_cwl_event_sync("9109", "2026-08", "discordid1")
        db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1"}])
        db.update_cwl_event_status_sync(event_id, "signup_open")
        db.upsert_cwl_signup_sync(event_id, "#P1", "Alpha", "123456789", None, "auto_optin", "auto_confirmed")

        mock_interaction.user.id = 123456789
        mock_interaction.response.edit_message = AsyncMock()

        # The member decides NOT to play this season after all — declines their own auto-seeded row.
        button = CwlSignupResponseButton("optout", event_id, "#P1")
        await button.callback(mock_interaction)

        signup = db.get_cwl_signup_sync(event_id, "#P1")
        assert signup["status"] == "declined"
        assert signup["responded_at"] is not None

    @pytest.mark.discord
    @pytest.mark.asyncio
    async def test_confirm_click_bumps_enrollment_version(self, db, mock_interaction, monkeypatch):
        """2026-08-17 regression guard (CWL_PROD_PERFORMANCE_FIX_PLAN.md P1 Step 8, found via
        live-testing): confirming/opting out via this DM button is a real cwl_signups write, but
        this callback has no refresh_cwl_management_hub_message()/_refresh_parent() call to
        piggyback the version bump onto (it only edits the DM itself) — it was the one write path
        the original Step 8 audit missed entirely, so an open Manage Enrollment board never
        learned about a player's DM response at all."""
        from qapbot.cache_manager import CACHE
        from qapbot.ui_cwl_roster import CwlSignupResponseButton
        import qapbot.web_bridge as web_bridge_module

        await _seed_guild_and_clans(db, "9104", {"#CLAN1": "Alpha"})
        CACHE.db_manager = db
        event_id = db.create_cwl_event_sync("9104", "2026-08", "discordid1")
        db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1"}])
        db.update_cwl_event_status_sync(event_id, "signup_open")
        db.upsert_cwl_signup_sync(event_id, "#P1", "Alpha", "123456789", None, "template_confirm", "pending")

        mock_interaction.user.id = 123456789
        mock_interaction.response.edit_message = AsyncMock()

        bump_spy = AsyncMock(wraps=web_bridge_module.bump_enrollment_version)
        monkeypatch.setattr(web_bridge_module, "bump_enrollment_version", bump_spy)

        before = web_bridge_module._enrollment_version.get("9104", 0)

        button = CwlSignupResponseButton("confirm", event_id, "#P1")
        await button.callback(mock_interaction)

        bump_spy.assert_awaited_once_with(9104)
        assert web_bridge_module._enrollment_version.get("9104", 0) == before + 1

    @pytest.mark.discord
    @pytest.mark.asyncio
    async def test_confirm_click_propagates_to_another_guilds_local_signup(self, db, mock_interaction, monkeypatch):
        """Rule h (2026-08-18, CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md, project owner's
        spec, verbatim): "The player has a global 'Got dm message already' attribute that is
        valid for guild a and b regardless of which guild sent the dm first. Then the player
        accepts or declines or is pending and that status is shown automatically in guild a's
        and guild B's clan rosters. no need to manage anything manually." This DM's custom_id
        only ever names ONE event (guild 9106, the one that actually sent it) — a second guild
        (9107) already pooling the same real-world player for the same season must see the
        response too, with zero action from that guild's own admin."""
        from qapbot.cache_manager import CACHE
        from qapbot.ui_cwl_roster import CwlSignupResponseButton
        import qapbot.web_bridge as web_bridge_module

        await _seed_guild_and_clans(db, "9106", {"#CLAN1": "Alpha"})
        await db.conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES ('9107')")
        await db.conn.commit()
        CACHE.db_manager = db
        event_id = db.create_cwl_event_sync("9106", "2026-08", "discordid1")
        db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1"}])
        db.update_cwl_event_status_sync(event_id, "signup_open")
        db.upsert_cwl_signup_sync(event_id, "#P1", "Alpha", "123456789", None, "template_confirm", "pending")

        # A second guild's event, same season, same real-world player already pooled there too
        # (e.g. via its own guest-invite) — must be updated automatically, no separate DM.
        other_event_id = db.create_cwl_event_sync("9107", "2026-08", "otherdiscordid")
        db.upsert_cwl_signup_sync(other_event_id, "#P1", "Alpha", "123456789", None, "guest_invite", "pending")

        mock_interaction.user.id = 123456789
        mock_interaction.response.edit_message = AsyncMock()

        bump_spy = AsyncMock(wraps=web_bridge_module.bump_enrollment_version)
        monkeypatch.setattr(web_bridge_module, "bump_enrollment_version", bump_spy)

        button = CwlSignupResponseButton("confirm", event_id, "#P1")
        await button.callback(mock_interaction)

        assert db.get_cwl_signup_sync(event_id, "#P1")["status"] == "confirmed"
        assert db.get_cwl_signup_sync(other_event_id, "#P1")["status"] == "confirmed"  # propagated
        bumped_guilds = {call.args[0] for call in bump_spy.await_args_list}
        assert bumped_guilds == {9106, 9107}

        global_status = db.get_cwl_player_season_status_sync("#P1", "2026-08")
        assert global_status is not None
        assert global_status["status"] == "confirmed"

    @pytest.mark.discord
    @pytest.mark.asyncio
    async def test_optout_click_marks_declined(self, db, mock_interaction):
        from qapbot.cache_manager import CACHE
        from qapbot.ui_cwl_roster import CwlSignupResponseButton

        await _seed_guild_and_clans(db, "9102", {"#CLAN1": "Alpha"})
        CACHE.db_manager = db
        event_id = db.create_cwl_event_sync("9102", "2026-08", "discordid1")
        db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1"}])
        db.update_cwl_event_status_sync(event_id, "signup_open")
        db.upsert_cwl_signup_sync(event_id, "#P1", "Alpha", "123456789", None, "template_confirm", "pending")

        mock_interaction.user.id = 123456789
        mock_interaction.response.edit_message = AsyncMock()

        button = CwlSignupResponseButton("optout", event_id, "#P1")
        await button.callback(mock_interaction)

        signup = db.get_cwl_signup_sync(event_id, "#P1")
        assert signup["status"] == "declined"
        assert signup["source"] == "template_optout"
        mock_interaction.response.edit_message.assert_awaited_once()
        _, kwargs = mock_interaction.response.edit_message.call_args
        assert "Alpha" in kwargs["content"]

    @pytest.mark.discord
    @pytest.mark.asyncio
    async def test_click_on_deleted_signup_shows_no_longer_valid(self, db, mock_interaction):
        from qapbot.cache_manager import CACHE
        from qapbot.ui_cwl_roster import CwlSignupResponseButton

        await _seed_guild_and_clans(db, "9103", {"#CLAN1": "Alpha"})
        CACHE.db_manager = db
        event_id = db.create_cwl_event_sync("9103", "2026-08", "discordid1")

        mock_interaction.response.send_message = AsyncMock()
        button = CwlSignupResponseButton("confirm", event_id, "#NEVERUP")
        await button.callback(mock_interaction)

        mock_interaction.response.send_message.assert_awaited_once()
        assert db.get_cwl_signup_sync(event_id, "#NEVERUP") is None

    @pytest.mark.discord
    @pytest.mark.asyncio
    async def test_click_by_wrong_account_is_rejected(self, db, mock_interaction):
        from qapbot.cache_manager import CACHE
        from qapbot.ui_cwl_roster import CwlSignupResponseButton

        await _seed_guild_and_clans(db, "9104", {"#CLAN1": "Alpha"})
        CACHE.db_manager = db
        event_id = db.create_cwl_event_sync("9104", "2026-08", "discordid1")
        db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1"}])
        db.update_cwl_event_status_sync(event_id, "signup_open")
        db.upsert_cwl_signup_sync(event_id, "#P1", "Alpha", "111111111", None, "template_confirm", "pending")

        mock_interaction.user.id = 999999999  # different account
        mock_interaction.response.send_message = AsyncMock()

        button = CwlSignupResponseButton("confirm", event_id, "#P1")
        await button.callback(mock_interaction)

        mock_interaction.response.send_message.assert_awaited_once()
        assert db.get_cwl_signup_sync(event_id, "#P1")["status"] == "pending"  # unchanged

    @pytest.mark.discord
    @pytest.mark.asyncio
    async def test_click_by_current_owner_is_accepted_when_snapshot_names_a_former_owner(
        self, db, mock_interaction
    ):
        """2026-08-22 (Pitfall 37): cwl_signups is an enrollment-time snapshot, so its discord_id
        can name a Discord user who no longer owns the account. Guarding on the snapshot alone
        told the account's REAL current owner "not your signup" and refused their response."""
        from qapbot.cache_manager import CACHE
        from qapbot.ui_cwl_roster import CwlSignupResponseButton

        await _seed_guild_and_clans(db, "9110", {"#CLAN1": "Alpha"})
        CACHE.db_manager = db
        event_id = db.create_cwl_event_sync("9110", "2026-08", "discordid1")
        db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1"}])
        db.update_cwl_event_status_sync(event_id, "signup_open")
        db.upsert_cwl_signup_sync(event_id, "#P1", "Alpha", "111111111", None, "template_confirm", "pending")
        # Reality now: the account was re-linked to someone else after enrollment ran.
        await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES ('222222222', 'New')")
        await db.conn.execute(
            "INSERT INTO user_players (discord_id, player_tag, player_name, verified) "
            "VALUES ('222222222', '#P1', 'Alpha', 0)"
        )
        await db.conn.commit()

        mock_interaction.user.id = 222222222  # the CURRENT owner
        mock_interaction.response.edit_message = AsyncMock()

        button = CwlSignupResponseButton("confirm", event_id, "#P1")
        await button.callback(mock_interaction)

        signup = db.get_cwl_signup_sync(event_id, "#P1")
        assert signup["status"] == "confirmed"
        # ...and the row self-heals, so it stops re-stamping the outdated owner on every response.
        assert signup["dmed_discord_id"] == "222222222"

    @pytest.mark.discord
    @pytest.mark.asyncio
    async def test_click_by_snapshot_owner_still_accepted_after_relink(self, db, mock_interaction):
        """The DM was genuinely delivered to the snapshot owner, so their button must keep
        working even once the account has been re-linked elsewhere — accepting BOTH owners is
        deliberate, not an oversight."""
        from qapbot.cache_manager import CACHE
        from qapbot.ui_cwl_roster import CwlSignupResponseButton

        await _seed_guild_and_clans(db, "9111", {"#CLAN1": "Alpha"})
        CACHE.db_manager = db
        event_id = db.create_cwl_event_sync("9111", "2026-08", "discordid1")
        db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1"}])
        db.update_cwl_event_status_sync(event_id, "signup_open")
        db.upsert_cwl_signup_sync(event_id, "#P1", "Alpha", "111111111", None, "template_confirm", "pending")
        await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES ('222222222', 'New')")
        await db.conn.execute(
            "INSERT INTO user_players (discord_id, player_tag, player_name, verified) "
            "VALUES ('222222222', '#P1', 'Alpha', 0)"
        )
        await db.conn.commit()

        mock_interaction.user.id = 111111111  # the SNAPSHOT owner, who actually received the DM
        mock_interaction.response.edit_message = AsyncMock()

        button = CwlSignupResponseButton("confirm", event_id, "#P1")
        await button.callback(mock_interaction)

        assert db.get_cwl_signup_sync(event_id, "#P1")["status"] == "confirmed"

    @pytest.mark.discord
    @pytest.mark.asyncio
    async def test_click_by_third_party_still_rejected_when_owners_disagree(self, db, mock_interaction):
        """Account protection (Cardinal Rule 2) must survive the widening above: someone who is
        neither the snapshot owner nor the live owner is still refused."""
        from qapbot.cache_manager import CACHE
        from qapbot.ui_cwl_roster import CwlSignupResponseButton

        await _seed_guild_and_clans(db, "9112", {"#CLAN1": "Alpha"})
        CACHE.db_manager = db
        event_id = db.create_cwl_event_sync("9112", "2026-08", "discordid1")
        db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1"}])
        db.update_cwl_event_status_sync(event_id, "signup_open")
        db.upsert_cwl_signup_sync(event_id, "#P1", "Alpha", "111111111", None, "template_confirm", "pending")
        await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES ('222222222', 'New')")
        await db.conn.execute(
            "INSERT INTO user_players (discord_id, player_tag, player_name, verified) "
            "VALUES ('222222222', '#P1', 'Alpha', 0)"
        )
        await db.conn.commit()

        mock_interaction.user.id = 999999999  # neither owner
        mock_interaction.response.send_message = AsyncMock()

        button = CwlSignupResponseButton("confirm", event_id, "#P1")
        await button.callback(mock_interaction)

        mock_interaction.response.send_message.assert_awaited_once()
        assert db.get_cwl_signup_sync(event_id, "#P1")["status"] == "pending"  # unchanged

    @pytest.mark.discord
    @pytest.mark.asyncio
    async def test_click_after_event_no_longer_signup_open_is_rejected(self, db, mock_interaction):
        from qapbot.cache_manager import CACHE
        from qapbot.ui_cwl_roster import CwlSignupResponseButton

        await _seed_guild_and_clans(db, "9105", {"#CLAN1": "Alpha"})
        CACHE.db_manager = db
        event_id = db.create_cwl_event_sync("9105", "2026-08", "discordid1")
        db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1"}])
        db.update_cwl_event_status_sync(event_id, "signup_open")
        db.upsert_cwl_signup_sync(event_id, "#P1", "Alpha", "123456789", None, "template_confirm", "pending")
        db.update_cwl_event_status_sync(event_id, "finalized")  # closed since the DM was sent

        mock_interaction.user.id = 123456789
        mock_interaction.response.send_message = AsyncMock()

        button = CwlSignupResponseButton("confirm", event_id, "#P1")
        await button.callback(mock_interaction)

        mock_interaction.response.send_message.assert_awaited_once()
        assert db.get_cwl_signup_sync(event_id, "#P1")["status"] == "pending"  # unchanged


# ---------------------------------------------------------------------------
# CwlReminderResponseButton — tracker #0038's combined-message reminder button
# ---------------------------------------------------------------------------

class TestCwlReminderResponseButton:
    def test_template_matches_valid_custom_id(self):
        import re
        from qapbot.ui_cwl_roster import CWL_REMINDER_RESPONSE_TEMPLATE

        m = re.match(CWL_REMINDER_RESPONSE_TEMPLATE, "cwl:remind:confirm:42:#ABC12")
        assert m is not None
        assert m.group("action") == "confirm"
        assert m.group("event_id") == "42"
        assert m.group("player_tag") == "#ABC12"

    def test_template_does_not_collide_with_the_original_signup_button(self):
        """Own custom_id namespace — the two button classes must never both match the same
        string, or add_dynamic_items() could route a click to the wrong handler."""
        import re
        from qapbot.ui_cwl_roster import CWL_REMINDER_RESPONSE_TEMPLATE, CWL_SIGNUP_RESPONSE_TEMPLATE

        assert re.match(CWL_REMINDER_RESPONSE_TEMPLATE, "cwl:signup:confirm:42:#ABC12") is None
        assert re.match(CWL_SIGNUP_RESPONSE_TEMPLATE, "cwl:remind:confirm:42:#ABC12") is None

    @pytest.mark.asyncio
    async def test_from_custom_id_reconstructs_state(self):
        import re
        from qapbot.ui_cwl_roster import CWL_REMINDER_RESPONSE_TEMPLATE, CwlReminderResponseButton

        match = re.match(CWL_REMINDER_RESPONSE_TEMPLATE, "cwl:remind:optout:7:#ZZZ1")
        item = await CwlReminderResponseButton.from_custom_id(MagicMock(), MagicMock(), match)
        assert item.action == "optout"
        assert item.event_id == 7
        assert item.player_tag == "#ZZZ1"

    def test_build_view_puts_one_row_per_account(self):
        from qapbot.ui_cwl_roster import CwlReminderResponseButton, build_cwl_reminder_response_view

        accounts = [
            {"player_tag": "#A1", "player_name": "Alt One"},
            {"player_tag": "#A2", "player_name": "Alt Two"},
        ]
        view = build_cwl_reminder_response_view(42, accounts, guild_id=None)

        buttons = [item for item in view.children if isinstance(item, CwlReminderResponseButton)]
        assert len(buttons) == 4  # confirm + decline per account
        assert {b.player_tag for b in buttons} == {"#A1", "#A2"}
        assert {b.row for b in buttons} == {0, 1}

    @pytest.mark.discord
    @pytest.mark.asyncio
    async def test_confirm_click_finalizes_when_it_was_the_users_only_pending_account(
        self, db, mock_interaction
    ):
        """Same end state as the original single-account button: nothing left pending for this
        user, so the message becomes a plain confirmation with no buttons."""
        from qapbot.cache_manager import CACHE
        from qapbot.ui_cwl_roster import CwlReminderResponseButton

        await _seed_guild_and_clans(db, "9201", {"#CLAN1": "Alpha"})
        CACHE.db_manager = db
        event_id = db.create_cwl_event_sync("9201", "2026-08", "discordid1")
        db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1"}])
        db.update_cwl_event_status_sync(event_id, "signup_open")
        db.upsert_cwl_signup_sync(event_id, "#P1", "Alpha", "123456789", None, "template_confirm", "pending")
        # Scope is re-derived from cwl_player_season_status.dm_sent_via_message_id (Phase 4d),
        # not just from the cwl_signups snapshot — this message covers exactly #P1.
        mock_interaction.message.id = 555001
        db.mark_cwl_player_dm_sent_sync(
            "#P1", "2026-08", "Alpha", "123456789", event_id, 9201, "2026-08-17T09:00Z", "555001",
        )

        mock_interaction.user.id = 123456789
        mock_interaction.response.edit_message = AsyncMock()

        button = CwlReminderResponseButton("confirm", event_id, "#P1")
        await button.callback(mock_interaction)

        assert db.get_cwl_signup_sync(event_id, "#P1")["status"] == "confirmed"
        mock_interaction.response.edit_message.assert_awaited_once()
        _, kwargs = mock_interaction.response.edit_message.call_args
        assert kwargs["view"] is None
        assert "Alpha" in kwargs["content"]

    @pytest.mark.discord
    @pytest.mark.asyncio
    async def test_confirm_click_leaves_siblings_pending_when_more_accounts_remain(
        self, db, mock_interaction
    ):
        """The whole reason this is a separate button class from CwlSignupResponseButton: one
        account's click must not wipe the other still-pending accounts' buttons out of the same
        combined message."""
        from qapbot.cache_manager import CACHE
        from qapbot.ui_cwl_roster import CwlReminderResponseButton

        await _seed_guild_and_clans(db, "9202", {"#CLAN1": "Alpha"})
        CACHE.db_manager = db
        event_id = db.create_cwl_event_sync("9202", "2026-08", "discordid1")
        db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1"}])
        db.update_cwl_event_status_sync(event_id, "signup_open")
        db.upsert_cwl_signup_sync(event_id, "#ALT1", "AltOne", "123456789", None, "template_confirm", "pending")
        db.upsert_cwl_signup_sync(event_id, "#ALT2", "AltTwo", "123456789", None, "template_confirm", "pending")
        # The remaining-group re-derivation reads the LIVE link (user_players), not the
        # cwl_signups snapshot's dmed_discord_id — both accounts need a real link row here.
        await db.conn.execute("INSERT OR IGNORE INTO users (discord_id, display_name) VALUES ('123456789', 'U')")
        await db.conn.execute(
            "INSERT INTO user_players (discord_id, player_tag, player_name, verified) VALUES "
            "('123456789', '#ALT1', 'AltOne', 0), ('123456789', '#ALT2', 'AltTwo', 0)"
        )
        await db.conn.commit()
        # Scope is re-derived from cwl_player_season_status.dm_sent_via_message_id (Phase 4d) —
        # both accounts share this one combined reminder message.
        mock_interaction.message.id = 555002
        db.mark_cwl_player_dm_sent_sync(
            "#ALT1", "2026-08", "AltOne", "123456789", event_id, 9202, "2026-08-17T09:00Z", "555002",
        )
        db.mark_cwl_player_dm_sent_sync(
            "#ALT2", "2026-08", "AltTwo", "123456789", event_id, 9202, "2026-08-17T09:00Z", "555002",
        )

        mock_interaction.user.id = 123456789
        mock_interaction.response.edit_message = AsyncMock()

        button = CwlReminderResponseButton("confirm", event_id, "#ALT1")
        await button.callback(mock_interaction)

        assert db.get_cwl_signup_sync(event_id, "#ALT1")["status"] == "confirmed"
        assert db.get_cwl_signup_sync(event_id, "#ALT2")["status"] == "pending"  # sibling untouched
        mock_interaction.response.edit_message.assert_awaited_once()
        _, kwargs = mock_interaction.response.edit_message.call_args
        assert kwargs["view"] is not None  # still has AltTwo's buttons — not wiped

    @pytest.mark.discord
    @pytest.mark.asyncio
    async def test_click_by_wrong_account_is_rejected_without_touching_the_message(
        self, db, mock_interaction
    ):
        from qapbot.cache_manager import CACHE
        from qapbot.ui_cwl_roster import CwlReminderResponseButton

        await _seed_guild_and_clans(db, "9203", {"#CLAN1": "Alpha"})
        CACHE.db_manager = db
        event_id = db.create_cwl_event_sync("9203", "2026-08", "discordid1")
        db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1"}])
        db.update_cwl_event_status_sync(event_id, "signup_open")
        db.upsert_cwl_signup_sync(event_id, "#P1", "Alpha", "111111111", None, "template_confirm", "pending")

        mock_interaction.user.id = 999999999  # different account
        mock_interaction.response.send_message = AsyncMock()
        mock_interaction.response.edit_message = AsyncMock()

        button = CwlReminderResponseButton("confirm", event_id, "#P1")
        await button.callback(mock_interaction)

        mock_interaction.response.send_message.assert_awaited_once()
        mock_interaction.response.edit_message.assert_not_awaited()
        assert db.get_cwl_signup_sync(event_id, "#P1")["status"] == "pending"  # unchanged


# ---------------------------------------------------------------------------
# find_active_cwl_participation — the guild-clan-removal safety check
# (CWL_ROSTER_PLANNING_PLAN.md, 2026-08-10 fix)
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_find_active_cwl_participation_flags_participating_clan(db):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import find_active_cwl_participation

    await _seed_guild_and_clans(db, "9201", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    event_id = db.create_cwl_event_sync("9201", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])

    conflicts = find_active_cwl_participation("9201", {"#CLAN1"})
    assert conflicts == {"#CLAN1": [(event_id, "2026-09")]}


@pytest.mark.discord
@pytest.mark.asyncio
async def test_find_active_cwl_participation_ignores_deactivated_clan(db):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import find_active_cwl_participation

    await _seed_guild_and_clans(db, "9202", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    event_id = db.create_cwl_event_sync("9202", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": False}])

    assert find_active_cwl_participation("9202", {"#CLAN1"}) == {}


@pytest.mark.discord
@pytest.mark.asyncio
async def test_find_active_cwl_participation_ignores_cancelled_events(db):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import find_active_cwl_participation

    await _seed_guild_and_clans(db, "9203", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    event_id = db.create_cwl_event_sync("9203", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])
    db.update_cwl_event_status_sync(event_id, "cancelled")

    assert find_active_cwl_participation("9203", {"#CLAN1"}) == {}


@pytest.mark.discord
@pytest.mark.asyncio
async def test_find_active_cwl_participation_no_conflict_for_unrelated_clan(db):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import find_active_cwl_participation

    await _seed_guild_and_clans(db, "9204", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    event_id = db.create_cwl_event_sync("9204", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1", "participating": True}])

    assert find_active_cwl_participation("9204", {"#CLAN2"}) == {}


def test_find_active_cwl_participation_returns_empty_without_db_manager():
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import find_active_cwl_participation

    CACHE.db_manager = None
    assert find_active_cwl_participation("1", {"#CLAN1"}) == {}


# ---------------------------------------------------------------------------
# ClanManagementView.refresh_cwl_view — Hub auto-refresh (2026-08-10 fix)
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_clan_management_view_refresh_cwl_view_also_refreshes_the_hub(monkeypatch):
    """A CWL change made through /clan management (entry point a) must not leave the anchored
    CWL Management Hub message (entry point b) stale — this is the actual live-testing gap the
    project owner reported."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper import format_clan_management_message
    from qapbot.ui_clan_management import ClanManagementView

    CACHE.server_config["9210"] = {}
    CACHE.db_manager = None

    guild = MagicMock()
    guild.id = 9210
    sent_message = AsyncMock(guild=guild)

    view = ClanManagementView(
        clan_tag="#CLAN1", guild_clans=["#CLAN1"], unlinked_players=[],
        sent_message=sent_message, mode="cwl_management", timeout=300,
    )

    async def _fake_format(*args, **kwargs):
        return MagicMock(), None, [], []

    monkeypatch.setattr("qapbot.QBdiscocmdshelper.format_clan_management_message", _fake_format)

    hub_refresh = AsyncMock()
    monkeypatch.setattr("qapbot.ui_cwl_roster.refresh_cwl_management_hub_message", hub_refresh)

    interaction = MagicMock()
    interaction.guild = guild

    await view.refresh_cwl_view(interaction, "cwl_management")

    hub_refresh.assert_awaited_once_with(9210, "cwl_management")


# ---------------------------------------------------------------------------
# resolve_prior_cwl_assignments — "Manage Enrollment" auto-assignment seed
# (CWL_ROSTER_PLANNING_PLAN.md, 2026-08-10; redesigned player-centric 2026-08-14)
# ---------------------------------------------------------------------------

async def _seed_cwl_war(
    db, clan_tag: str, players: list, date: str = "2026-07-01T08:00", war_id: str = None,
    attack_order: int = 1,
) -> None:
    """Seeds one CWL war_summary row plus one war_attacks row per player. attack_order defaults
    to 1 (a real attack) — pass 0 to seed a "missed attack" sentinel row instead, which
    get_last_real_cwl_attack_clan_sync's attack_order > 0 filter must exclude."""
    war_id = war_id or f"war_{clan_tag}_{date}"
    await db.conn.execute(
        "INSERT INTO war_summary (war_id, clan_tag, opponent_tag, is_cwl, cwl_season, date) VALUES (?, ?, ?, 1, ?, ?)",
        (war_id, clan_tag, "#OPP", date[:7], date),
    )
    for player_tag, player_name, th_level, map_position in players:
        await db.conn.execute(
            "INSERT INTO war_attacks "
            "(war_id, clan_tag, date, player_name, player_tag, th_level, map_position, attack_order, stars) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (war_id, clan_tag, date, player_name, player_tag, th_level, map_position, attack_order),
        )
    await db.conn.commit()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_resolve_prior_cwl_assignments_single_clan(db):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_prior_cwl_assignments

    await _seed_guild_and_clans(db, "9301", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    await _seed_cwl_war(db, "#CLAN1", [("#P1", "Alpha", 15, 1), ("#P2", "Bravo", 14, 2)])

    assert resolve_prior_cwl_assignments(["#P1", "#P2"], ["#CLAN1"]) == {"#P1": "#CLAN1", "#P2": "#CLAN1"}


@pytest.mark.discord
@pytest.mark.asyncio
async def test_resolve_prior_cwl_assignments_player_with_no_history_contributes_nothing(db):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_prior_cwl_assignments

    await _seed_guild_and_clans(db, "9302", {"#CLAN1": "Alpha", "#CLAN2": "Bravo"})
    CACHE.db_manager = db
    await _seed_cwl_war(db, "#CLAN1", [("#P1", "Alpha", 15, 1)])
    # #P2 has never played a CWL war — contributes no candidate, doesn't error.

    assert resolve_prior_cwl_assignments(["#P1", "#P2"], ["#CLAN1", "#CLAN2"]) == {"#P1": "#CLAN1"}


@pytest.mark.discord
@pytest.mark.asyncio
async def test_resolve_prior_cwl_assignments_conflict_latest_attack_wins(db):
    """A player with real CWL attacks for both #CLAN1 and #CLAN2 must resolve to whichever one
    is more recent — a straight per-player "last real attack, any clan" resolution now, not a
    per-clan roster lookup."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_prior_cwl_assignments

    await _seed_guild_and_clans(db, "9303", {"#CLAN1": "Alpha", "#CLAN2": "Bravo"})
    CACHE.db_manager = db
    await _seed_cwl_war(db, "#CLAN1", [("#P1", "Alpha", 15, 1)], date="2026-07-01T08:00")
    await _seed_cwl_war(db, "#CLAN2", [("#P1", "Alpha", 15, 1)], date="2026-05-01T08:00")

    assert resolve_prior_cwl_assignments(["#P1"], ["#CLAN1", "#CLAN2"]) == {"#P1": "#CLAN1"}

    # Reversed dates -> reversed winner, confirming it's genuinely date-driven, not insertion-order.
    await _seed_guild_and_clans(db, "9304", {"#CLAN3": "Charlie", "#CLAN4": "Delta"})
    await _seed_cwl_war(db, "#CLAN3", [("#P2", "Bravo", 15, 1)], date="2026-05-01T08:00")
    await _seed_cwl_war(db, "#CLAN4", [("#P2", "Bravo", 15, 1)], date="2026-07-01T08:00")
    assert resolve_prior_cwl_assignments(["#P2"], ["#CLAN3", "#CLAN4"]) == {"#P2": "#CLAN4"}


@pytest.mark.discord
@pytest.mark.asyncio
async def test_resolve_prior_cwl_assignments_excludes_zero_attack_sentinel_rows(db):
    """A player merely listed on a war's roster with no real attack (attack_order=0) must NOT
    resolve to that clan — "last attack" means an actual attack, per the project owner's spec."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_prior_cwl_assignments

    await _seed_guild_and_clans(db, "9305", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    await _seed_cwl_war(db, "#CLAN1", [("#P1", "Alpha", 15, 1)], attack_order=0)

    assert resolve_prior_cwl_assignments(["#P1"], ["#CLAN1"]) == {}


@pytest.mark.discord
@pytest.mark.asyncio
async def test_resolve_prior_cwl_assignments_ignores_non_participating_target_clan(db):
    """A player's last real CWL attack was for #CLAN1, but only #CLAN2 is participating this
    season — there's no column to place them in, so they must be left unassigned rather than
    forced into a clan they didn't play for."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_prior_cwl_assignments

    await _seed_guild_and_clans(db, "9306", {"#CLAN1": "Alpha", "#CLAN2": "Bravo"})
    CACHE.db_manager = db
    await _seed_cwl_war(db, "#CLAN1", [("#P1", "Alpha", 15, 1)])

    assert resolve_prior_cwl_assignments(["#P1"], ["#CLAN2"]) == {}
    # Once #CLAN1 also participates, the very same history resolves them there.
    assert resolve_prior_cwl_assignments(["#P1"], ["#CLAN1", "#CLAN2"]) == {"#P1": "#CLAN1"}


@pytest.mark.discord
@pytest.mark.asyncio
async def test_resolve_prior_cwl_assignments_independent_of_current_clan(db):
    """A player's last real CWL attack was for #CLAN1 — they resolve there even though the
    caller's player_tags pool (their current membership) is nothing to do with #CLAN1 here;
    resolve_prior_cwl_assignments() itself has no notion of "current clan" at all, only the
    caller decides who's even in the candidate pool."""
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_prior_cwl_assignments

    await _seed_guild_and_clans(db, "9307", {"#CLAN1": "Alpha", "#CLAN2": "Bravo"})
    CACHE.db_manager = db
    await _seed_cwl_war(db, "#CLAN1", [("#P1", "Alpha", 15, 1)])

    # #P1 is passed in as a candidate (e.g. currently a member of #CLAN2) — still resolves to
    # #CLAN1, the clan of their last real attack, exactly as the spec requires.
    assert resolve_prior_cwl_assignments(["#P1"], ["#CLAN1"]) == {"#P1": "#CLAN1"}


def test_resolve_prior_cwl_assignments_returns_empty_without_db_manager():
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_prior_cwl_assignments

    CACHE.db_manager = None
    assert resolve_prior_cwl_assignments(["#P1"], ["#CLAN1"]) == {}


def test_resolve_prior_cwl_assignments_returns_empty_for_no_players():
    from qapbot.QBdiscocmdshelper_cwl import resolve_prior_cwl_assignments

    assert resolve_prior_cwl_assignments([], ["#CLAN1"]) == {}


# ---------------------------------------------------------------------------
# resolve_guild_member_clan_tags — CWL enrollment's candidate-pool source (2026-08-14)
# ---------------------------------------------------------------------------

def test_resolve_guild_member_clan_tags_individual_and_family(monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_guild_member_clan_tags

    monkeypatch.setitem(
        CACHE.server_config, "9401",
        {"member_clans": ["#CLAN1"], "member_families": ["FAM1"]},
    )
    monkeypatch.setitem(CACHE.clan_families, "FAM1", {"clans": ["#CLAN2", "#CLAN3"]})

    assert resolve_guild_member_clan_tags(9401) == ["#CLAN1", "#CLAN2", "#CLAN3"]


def test_resolve_guild_member_clan_tags_dedupes_clan_in_both_direct_and_family(monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_guild_member_clan_tags

    monkeypatch.setitem(
        CACHE.server_config, "9402",
        {"member_clans": ["#CLAN1"], "member_families": ["FAM1"]},
    )
    monkeypatch.setitem(CACHE.clan_families, "FAM1", {"clans": ["#CLAN1", "#CLAN2"]})

    assert resolve_guild_member_clan_tags(9402) == ["#CLAN1", "#CLAN2"]


def test_resolve_guild_member_clan_tags_unknown_guild_returns_empty(monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import resolve_guild_member_clan_tags

    monkeypatch.delitem(CACHE.server_config, "9403", raising=False)
    assert resolve_guild_member_clan_tags(9403) == []


# ---------------------------------------------------------------------------
# _league_weight / compute_league_adjusted_skill_scores — "Manage Enrollment" board's
# player-skill sort (live-testing feedback, 2026-08-14; formula/growth_rate confirmed with the
# project owner: 1.4x per league group, +3%/step within a group)
# ---------------------------------------------------------------------------

def test_league_weight_baseline_for_bronze_iii():
    from qapbot.QBdiscocmdshelper_cwl import _league_weight

    assert _league_weight("Bronze League III") == pytest.approx(1.0)


def test_league_weight_legend_is_about_ten_and_a_half_times_bronze():
    from qapbot.QBdiscocmdshelper_cwl import _league_weight

    assert _league_weight("Legend League") == pytest.approx(1.4 ** 7, rel=1e-9)


def test_league_weight_one_group_step_is_1_4x():
    from qapbot.QBdiscocmdshelper_cwl import _league_weight

    champion_ii = _league_weight("Champion League II")
    master_ii = _league_weight("Master League II")
    assert champion_ii / master_ii == pytest.approx(1.4, rel=1e-9)


def test_league_weight_subtier_bonus_orders_i_above_ii_above_iii():
    from qapbot.QBdiscocmdshelper_cwl import _league_weight

    w3 = _league_weight("Gold League III")
    w2 = _league_weight("Gold League II")
    w1 = _league_weight("Gold League I")
    assert w3 < w2 < w1


def test_league_weight_unknown_tier_returns_baseline():
    from qapbot.QBdiscocmdshelper_cwl import _league_weight

    assert _league_weight(None) == 1.0
    assert _league_weight("Not A Real League") == 1.0


async def _seed_cwl_attack_with_league(
    db, clan_tag: str, cwl_season: str, league_rank: str, player_tag: str, stars: int,
    date: str = "2026-07-01T08:00", war_id: str = None, league_group_id: str = None,
) -> None:
    """Seeds one CWL war_attacks row plus the league-reconstruction chain
    (cwl_league_rounds -> cwl_league_groups) get_recent_cwl_attacks_with_league_sync() needs."""
    war_id = war_id or f"war_{clan_tag}_{date}"
    league_group_id = league_group_id or f"group_{cwl_season}_{clan_tag}"
    await db.conn.execute(
        "INSERT INTO war_summary (war_id, clan_tag, opponent_tag, is_cwl, cwl_season, date, war_tag) VALUES (?, ?, ?, 1, ?, ?, ?)",
        (war_id, clan_tag, "#OPP", cwl_season, date, war_id),
    )
    await db.conn.execute(
        "INSERT INTO war_attacks (war_id, clan_tag, date, player_name, player_tag, th_level, map_position, stars) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (war_id, clan_tag, date, player_tag, player_tag, 15, 1, stars),
    )
    await db.conn.execute(
        "INSERT INTO cwl_league_rounds (war_tag, cwl_season, cwl_round, league_group_id) VALUES (?, ?, 1, ?)",
        (war_id, cwl_season, league_group_id),
    )
    await db.conn.execute(
        "INSERT OR IGNORE INTO cwl_league_groups (league_group_id, cwl_season, clan_tag, league_rank) VALUES (?, ?, ?, ?)",
        (league_group_id, cwl_season, clan_tag, league_rank),
    )
    await db.conn.commit()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_compute_league_adjusted_skill_scores_weights_by_league(db):
    from datetime import datetime

    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import compute_league_adjusted_skill_scores

    await _seed_guild_and_clans(db, "9310", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    # Same 3-star attack, but one earned in Legend, one in Bronze III — Legend must score higher.
    await _seed_cwl_attack_with_league(db, "#CLAN1", "2026-05", "Legend League", "#P1", stars=3, date="2026-05-01T08:00", war_id="w1")
    await _seed_cwl_attack_with_league(db, "#CLAN1", "2026-07", "Bronze League III", "#P2", stars=3, date="2026-07-01T08:00", war_id="w2")

    scores = compute_league_adjusted_skill_scores(["#P1", "#P2"], now=datetime(2026, 7, 15))
    assert scores["#P1"] > scores["#P2"]
    assert scores["#P2"] == pytest.approx(3.0)  # Bronze III weight is exactly 1.0
    assert scores["#P1"] == pytest.approx(3 * (1.4 ** 7), abs=0.01)  # rounded to 2dp


@pytest.mark.discord
@pytest.mark.asyncio
async def test_compute_league_adjusted_skill_scores_excludes_attacks_outside_trailing_three_months(db):
    """2026-08-16, project owner's spec: "our hover over pop-up info is inconsistent to the way
    we calculate the stats for the player tile info. there we use the last 10 cwl attacks. We
    should make this consistent and use the 'last three months' logic for both" — replaces the
    old count-based "last 10 attacks" cap with the same trailing-3-calendar-month window the
    pop-up's own get_recent_cwl_player_stats uses."""
    from datetime import datetime

    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import compute_league_adjusted_skill_scores

    await _seed_guild_and_clans(db, "9311", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    # April 2026 — outside the trailing-3-month window ending July — 1-star, must be excluded.
    await _seed_cwl_attack_with_league(
        db, "#CLAN1", "2026-04", "Bronze League III", "#P1", stars=1, date="2026-04-01T08:00", war_id="war_apr",
    )
    # May/June/July 2026 — inside the window — 3-star each.
    for month in ("05", "06", "07"):
        await _seed_cwl_attack_with_league(
            db, "#CLAN1", f"2026-{month}", "Bronze League III", "#P1", stars=3,
            date=f"2026-{month}-01T08:00", war_id=f"war{month}",
        )

    scores = compute_league_adjusted_skill_scores(["#P1"], now=datetime(2026, 7, 15))
    assert scores["#P1"] == pytest.approx(3.0)  # only the three in-window 3-star attacks counted


@pytest.mark.discord
@pytest.mark.asyncio
async def test_compute_league_adjusted_skill_scores_absent_for_player_with_no_cwl_history(db):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import compute_league_adjusted_skill_scores

    await _seed_guild_and_clans(db, "9312", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db

    assert compute_league_adjusted_skill_scores(["#NEVERPLAYED"]) == {}


def test_compute_league_adjusted_skill_scores_returns_empty_without_db_manager():
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import compute_league_adjusted_skill_scores

    CACHE.db_manager = None
    assert compute_league_adjusted_skill_scores(["#P1"]) == {}


# ---------------------------------------------------------------------------
# compute_avg_stars_per_attack — board's other number-display option (2026-08-14):
# unweighted, same attack window as compute_league_adjusted_skill_scores above.
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_compute_avg_stars_per_attack_ignores_league_weighting(db):
    """Same setup as the league-weighted test above — Legend and Bronze III both earned a
    3-star, but this metric must score them identically since it isn't league-adjusted."""
    from datetime import datetime

    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import compute_avg_stars_per_attack

    await _seed_guild_and_clans(db, "9313", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    await _seed_cwl_attack_with_league(db, "#CLAN1", "2026-05", "Legend League", "#P1", stars=3, date="2026-05-01T08:00", war_id="w1")
    await _seed_cwl_attack_with_league(db, "#CLAN1", "2026-07", "Bronze League III", "#P2", stars=3, date="2026-07-01T08:00", war_id="w2")

    averages = compute_avg_stars_per_attack(["#P1", "#P2"], now=datetime(2026, 7, 15))
    assert averages["#P1"] == pytest.approx(3.0)
    assert averages["#P2"] == pytest.approx(3.0)


@pytest.mark.discord
@pytest.mark.asyncio
async def test_compute_avg_stars_per_attack_excludes_attacks_outside_trailing_three_months(db):
    """Same fix/reasoning as compute_league_adjusted_skill_scores' own trailing-3-month test."""
    from datetime import datetime

    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import compute_avg_stars_per_attack

    await _seed_guild_and_clans(db, "9314", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    await _seed_cwl_attack_with_league(
        db, "#CLAN1", "2026-04", "Bronze League III", "#P1", stars=1, date="2026-04-01T08:00", war_id="war_apr",
    )
    for month in ("05", "06", "07"):
        await _seed_cwl_attack_with_league(
            db, "#CLAN1", f"2026-{month}", "Bronze League III", "#P1", stars=3,
            date=f"2026-{month}-01T08:00", war_id=f"war{month}",
        )

    averages = compute_avg_stars_per_attack(["#P1"], now=datetime(2026, 7, 15))
    assert averages["#P1"] == pytest.approx(3.0)  # only the three in-window 3-star attacks counted


@pytest.mark.discord
@pytest.mark.asyncio
async def test_compute_avg_stars_per_attack_absent_for_player_with_no_cwl_history(db):
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import compute_avg_stars_per_attack

    await _seed_guild_and_clans(db, "9315", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db

    assert compute_avg_stars_per_attack(["#NEVERPLAYED"]) == {}


def test_compute_avg_stars_per_attack_returns_empty_without_db_manager():
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper_cwl import compute_avg_stars_per_attack

    CACHE.db_manager = None
    assert compute_avg_stars_per_attack(["#P1"]) == {}


# _check_cwl_admin_or_leader_permission() tests live in
# tests/unit/test_check_admin_or_leader_permission.py — this file's module-level autouse
# _bypass_cwl_admin_check fixture forces check_admin_permissions() to always return True for
# every test here, which would silently defeat any test of the leader-role-holder path.


@pytest.mark.discord
@pytest.mark.asyncio
async def test_repaired_signup_row_revives_a_dead_dm_button(db, mock_interaction):
    """Tracker #0016, end-to-end. The button resolves (event_id, player_tag) at CLICK time, so a
    DM sent without a cwl_signups row shows "this sign-up is no longer valid" forever -- and
    creating the row afterwards repairs the DM already sitting in the user's inbox, no re-send.
    Asserts the actual user-visible symptom flips, not just that a row appeared."""
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import CwlSignupResponseButton

    await _seed_guild_and_clans(db, "9120", {"#CLAN1": "Alpha"})
    CACHE.db_manager = db
    event_id = db.create_cwl_event_sync("9120", "2026-09", "discordid1")
    db.set_cwl_event_clans_sync(event_id, [{"clan_tag": "#CLAN1"}])
    db.update_cwl_event_status_sync(event_id, "signup_open")
    # DM went out, but nothing seeded the row its button needs -> dead on arrival.
    db.mark_cwl_player_dm_sent_sync(
        "#DEADBTN", "2026-09", "DeadBtn", "123456789", event_id, 9120,
        "2026-09-01T10:00Z", "msg1", "chan1",
    )

    mock_interaction.user.id = 123456789
    mock_interaction.response.send_message = AsyncMock()
    mock_interaction.response.edit_message = AsyncMock()

    button = CwlSignupResponseButton("optout", event_id, "#DEADBTN")
    await button.callback(mock_interaction)

    # Before the repair: rejected outright, nothing recorded.
    mock_interaction.response.send_message.assert_awaited_once()
    assert db.get_cwl_signup_sync(event_id, "#DEADBTN") is None

    await db._repair_cwl_signups_for_sent_dms()

    mock_interaction.response.send_message.reset_mock()
    await button.callback(mock_interaction)

    # After: the same click on the same untouched DM now records the response.
    mock_interaction.response.send_message.assert_not_awaited()
    assert db.get_cwl_signup_sync(event_id, "#DEADBTN")["status"] == "declined"


# ---------------------------------------------------------------------------
# CwlPlayerHubView / build_cwl_player_hub_content_and_view — plans/cwl-personal-hub.md
# Phase 5b, pulled forward into Phase 3 because repost_cwl_player_hub_messages() has a hard
# import dependency on the content builder (see the plan's own scope-box note for why).
# ---------------------------------------------------------------------------

@pytest.mark.discord
def test_cwl_player_hub_view_constructs_with_exactly_one_button():
    from qapbot.ui_cwl_roster import CwlPlayerHubView

    view = CwlPlayerHubView()

    assert len(view.children) == 1
    button = view.children[0]
    assert button.custom_id == "cwl_player_hub_open_prefs"
    assert button.label == "Your CWL Preferences"
    assert button.style == discord.ButtonStyle.primary


@pytest.mark.discord
@pytest.mark.asyncio
async def test_cwl_player_hub_button_launches_activity_with_player_prefs_screen(mock_interaction):
    """No permission gate (member-facing, unlike CwlManagementHubView's admin-only toggle
    buttons) and no defer/send_message before the LAUNCH_ACTIVITY call, per
    _launch_cwl_activity's own hard constraint."""
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import CwlPlayerHubView

    mock_interaction.id = 123456789
    mock_interaction.token = "test-token"
    mock_interaction.guild.id = 222333
    mock_interaction.user.id = 444555

    view = CwlPlayerHubView()
    button = view.children[0]

    await button.callback(mock_interaction)

    mock_interaction.client.http.request.assert_awaited_once()
    _, kwargs = mock_interaction.client.http.request.await_args
    assert kwargs["json"] == {"type": 12, "data": {}}
    assert CACHE.pending_cwl_activity_screen[("222333", "444555")] == "player_prefs"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_cwl_player_hub_button_does_nothing_outside_a_guild(mock_interaction):
    """Guarded the same way every other guild-scoped CWL callback is — a DM-context interaction
    (interaction.guild is None) must not attempt to resolve a guild_id at all."""
    from qapbot.ui_cwl_roster import CwlPlayerHubView

    mock_interaction.guild = None
    view = CwlPlayerHubView()
    button = view.children[0]

    await button.callback(mock_interaction)

    mock_interaction.client.http.request.assert_not_awaited()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_build_cwl_player_hub_content_and_view_returns_embed_and_view():
    from qapbot.ui_cwl_roster import CwlPlayerHubView, build_cwl_player_hub_content_and_view

    channel = MagicMock()
    channel.guild = MagicMock()

    content, view, embed = await build_cwl_player_hub_content_and_view(channel, 999888)

    assert content == ""
    assert isinstance(view, CwlPlayerHubView)
    assert embed is not None
    assert embed.title == "🛡️ Your CWL Preferences"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_launch_cwl_activity_fallback_uses_player_hub_key_for_player_prefs_screen(mock_interaction, monkeypatch):
    """The dict-based fallback lookup (Phase 5a widening) must pick the new, distinct
    cwl.player_hub.open_fallback text for this screen, not silently fall back to one of the
    other two screens' wording."""
    from qapbot.ui_cwl_roster import _launch_cwl_activity

    mock_interaction.response.send_message = AsyncMock()
    mock_interaction.response.is_done = MagicMock(return_value=False)
    mock_interaction.client.http.request = AsyncMock(side_effect=Exception("simulated failure"))

    await _launch_cwl_activity(mock_interaction, 555, "player_prefs")

    mock_interaction.response.send_message.assert_awaited_once()
    args, _ = mock_interaction.response.send_message.call_args
    assert "CWL preferences" in args[0]


# ---------------------------------------------------------------------------
# /cwl preferences slash command — plans/cwl-personal-hub.md Phase 5a-bis, the second entry
# point into the same player_prefs Activity screen as CwlPlayerHubView's button (above), for
# guilds that never posted (or disabled) the anchored hub message.
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_cwl_preferences_command_launches_activity_with_player_prefs_screen(mock_interaction):
    """No permission gate and no defer/send_message before the LAUNCH_ACTIVITY call, exactly
    like the CwlPlayerHubView button — this command is just an alternate way to reach the same
    screen."""
    from qapbot.cache_manager import CACHE
    import QBdiscordcmds

    mock_interaction.id = 987654321
    mock_interaction.token = "test-token"
    mock_interaction.guild.id = 333444
    mock_interaction.user.id = 555666

    await QBdiscordcmds.cwl_preferences.callback(mock_interaction)  # type: ignore[arg-type]

    mock_interaction.client.http.request.assert_awaited_once()
    _, kwargs = mock_interaction.client.http.request.await_args
    assert kwargs["json"] == {"type": 12, "data": {}}
    assert CACHE.pending_cwl_activity_screen[("333444", "555666")] == "player_prefs"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_cwl_preferences_command_does_nothing_outside_a_guild(mock_interaction):
    """Guild-only (declared via @app_commands.guild_only()), but the callback body also guards
    directly since Discord's guild-only enforcement is client-side only for some invocation
    paths — matches the same defensive pattern as every other guild-scoped CWL callback."""
    import QBdiscordcmds

    mock_interaction.guild = None

    await QBdiscordcmds.cwl_preferences.callback(mock_interaction)  # type: ignore[arg-type]

    mock_interaction.client.http.request.assert_not_awaited()
