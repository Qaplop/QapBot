"""
UI views for the CWL roster planning feature (CWL_ROSTER_PLANNING_PLAN.md).

All CWL-roster views across Phases 1-6 live here (mirrors ui_clan_management.py holding many
related views in one file). Phase 0's DM interaction foundation is generic bot infrastructure,
not CWL-specific, and lives elsewhere.

Shared content layer (Phase 1): add_cwl_settings_components() / add_cwl_management_components()
are free functions, not private view methods — both ClanManagementView (entry point a) and
CwlManagementHubView (entry point b) call the exact same implementation, so the two screens
render identically regardless of which shell opened them.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import discord

from qapbot.cache_manager import CACHE
from qapbot.constants import CWL_LEAGUE_ORDER

# CoC's real league ladder, used for target_league_rank / preferred_league_rank pickers
# throughout this feature (Phase 1's per-clan target tier, Phase 2's sign-up preference).
# Derived from CWL_LEAGUE_ORDER (qapbot/constants.py — the codebase's actual single source of
# truth for the current ladder, already used to sort clans by tier elsewhere) rather than a
# second hand-maintained copy of it: tracker #0047, this list had drifted stale and was missing
# the Legend/Titan tiers CoC added above Champion, precisely the kind of drift a second
# hardcoded copy invites. CWL_LEAGUE_ORDER is lowest-to-highest; this picker wants highest-first,
# plus the "no clan war league synced yet" placeholder CWL_LEAGUE_ORDER itself has no use for.
# Still duplicated verbatim as a TS constant in activity/client/src/playerPrefs.ts (plans/
# cwl-personal-hub.md Phase 5e, its own picker) rather than fetched — Python constants aren't
# reachable from TypeScript. If CWL_LEAGUE_ORDER ever changes, update that file's copy too.
CWL_LEAGUE_RANKS: List[str] = [*reversed(CWL_LEAGUE_ORDER), "Unranked"]


# ---------------------------------------------------------------------------
# cwl_settings — guild preferences (shared content layer)
# ---------------------------------------------------------------------------

def add_cwl_settings_components(view: discord.ui.View, guild_id: int) -> None:
    """Attach the cwl_settings screen's buttons/select to *view* — either a ClanManagementView
    (entry point a) or a CwlManagementHubView (entry point b, Phase 1). Free function per the
    shared-content-layer design: written once, called from both shells.
    """
    from qapbot.i18n import t

    guild_config = CACHE.server_config.get(str(guild_id), {})

    # Rows 0 and 2 are reserved by the two possible shells regardless of which mode is active
    # (ClanManagementView: row 0 = refresh button, row 2 = mode select; CwlManagementHubView:
    # row 0 = Settings/Season Management toggle buttons) — everything here uses rows 1/3/4 so
    # it never collides with either parent's own components.
    channel_button = discord.ui.Button(
        label=t('cwl.settings.button_configure_channels', guild_id=guild_id),
        style=discord.ButtonStyle.secondary,
        custom_id="cwl_settings_channels",
        row=1,
    )
    channel_button.callback = _make_cwl_settings_channels_callback(view)  # type: ignore[assignment]
    view.add_item(channel_button)  # type: ignore[arg-type]

    # tracker #0067: button color reflects the ACTION the click performs, not the current
    # enabled/disabled state -- "Deactivate ..." is always danger (red), "Activate ..." is
    # always success (green), regardless of which one that happens to be right now.
    hub_enabled = bool(guild_config.get("cwl_management_message_enabled", False))
    toggle_button = discord.ui.Button(
        label=(
            t('cwl.settings.button_deactivate_hub', guild_id=guild_id)
            if hub_enabled
            else t('cwl.settings.button_activate_hub', guild_id=guild_id)
        ),
        style=discord.ButtonStyle.danger if hub_enabled else discord.ButtonStyle.success,
        custom_id="cwl_settings_toggle_hub",
        row=1,
    )
    toggle_button.callback = _make_cwl_settings_toggle_callback(view)  # type: ignore[assignment]
    view.add_item(toggle_button)  # type: ignore[arg-type]

    # Player CWL Settings Hub toggle (plans/cwl-personal-hub.md Phase 2b) — same row, same
    # simple activate/deactivate-button pattern as the admin hub toggle just above.
    player_hub_enabled = bool(guild_config.get("cwl_player_hub_message_enabled", False))
    player_hub_toggle_button = discord.ui.Button(
        label=(
            t('cwl.settings.button_deactivate_player_hub', guild_id=guild_id)
            if player_hub_enabled
            else t('cwl.settings.button_activate_player_hub', guild_id=guild_id)
        ),
        style=discord.ButtonStyle.danger if player_hub_enabled else discord.ButtonStyle.success,
        custom_id="cwl_settings_toggle_player_hub",
        row=1,
    )
    player_hub_toggle_button.callback = _make_cwl_settings_toggle_player_hub_callback(view)  # type: ignore[assignment]
    view.add_item(player_hub_toggle_button)  # type: ignore[arg-type]

    retention_button: discord.ui.Button[Any] = discord.ui.Button(
        label=t('cwl.settings.button_configure_retention', guild_id=guild_id),
        style=discord.ButtonStyle.secondary,
        custom_id="cwl_settings_retention_button",
        row=3,
    )
    retention_button.callback = _make_cwl_settings_retention_button_callback(view)  # type: ignore[assignment]
    view.add_item(retention_button)  # type: ignore[arg-type]

    # 2026-08-15, project owner's spec: account-wide enrollment-pool expansion — a plain
    # persistent per-guild toggle (guild_config.cwl_enrollment_include_all_linked_accounts,
    # off by default), same simple activate/deactivate-button pattern as the Hub toggle above.
    # See start_cwl_enrollment()'s docstring (QBdiscocmdshelper_cwl.py) for the actual mechanic.
    include_all_accounts = bool(guild_config.get("cwl_enrollment_include_all_linked_accounts", False))
    include_all_accounts_button: discord.ui.Button[Any] = discord.ui.Button(
        label=(
            t('cwl.settings.button_disable_include_all_accounts', guild_id=guild_id)
            if include_all_accounts
            else t('cwl.settings.button_enable_include_all_accounts', guild_id=guild_id)
        ),
        style=discord.ButtonStyle.danger if include_all_accounts else discord.ButtonStyle.success,
        custom_id="cwl_settings_toggle_include_all_accounts",
        row=3,
    )
    include_all_accounts_button.callback = _make_cwl_settings_toggle_include_all_accounts_callback(view)  # type: ignore[assignment]
    view.add_item(include_all_accounts_button)  # type: ignore[arg-type]


def _make_cwl_settings_channels_callback(view: discord.ui.View):
    async def callback(interaction: discord.Interaction) -> None:
        if not await _check_cwl_admin_permission(interaction):
            return
        await interaction.response.defer(thinking=False, ephemeral=False)
        if not interaction.guild:
            return
        from qapbot.ui_clan_management import ChannelConfigurationView, CWL_CONFIG_CHANNEL_SLOTS

        config = CACHE.server_config.get(str(interaction.guild.id), {})
        current_channels: Dict[str, Optional[discord.TextChannel]] = {}
        for slot in CWL_CONFIG_CHANNEL_SLOTS:
            channel_id = config.get(slot.config_key)
            channel_obj: Optional[discord.TextChannel] = None
            if channel_id:
                try:
                    channel = interaction.guild.get_channel(int(channel_id))
                    if channel and isinstance(channel, discord.TextChannel):
                        channel_obj = channel
                except Exception:
                    pass
            current_channels[slot.key] = channel_obj

        channel_config_view = ChannelConfigurationView(
            guild=interaction.guild,
            clan_management_view=view,  # duck-typed: only needs .refresh_cwl_view()
            original_interaction=interaction,
            current_channels=current_channels,
            slots=CWL_CONFIG_CHANNEL_SLOTS,
            origin_mode="cwl_settings",
            timeout=300,
        )
        header_msg = channel_config_view._format_header()  # type: ignore[attr-defined]
        msg = await interaction.followup.send(header_msg, view=channel_config_view, ephemeral=True)
        channel_config_view.config_message = msg  # type: ignore[attr-defined]

    return callback


# Tracker #0066: both toggle callbacks below fire from BOTH the session-scoped
# ClanManagementView (entry point a — a brand-new instance is built by every refresh, so no
# `self` flag survives across clicks) and CwlManagementHubView, a single instance shared across
# every guild's anchored Hub message (entry point b — a `self` flag there would incorrectly
# block every OTHER guild's toggle too). Neither location can use the usual Pitfall 41
# `_consume_once()` instance-flag guard, so these module-level per-guild sets stand in instead —
# checked synchronously before ANY `await`, so two rapid clicks starting near-simultaneously
# can't both pass.
_CWL_HUB_TOGGLE_IN_PROGRESS: set = set()
_CWL_PLAYER_HUB_TOGGLE_IN_PROGRESS: set = set()


def _make_cwl_settings_toggle_callback(view: discord.ui.View):
    async def callback(interaction: discord.Interaction) -> None:
        guild_id_int = interaction.guild.id if interaction.guild else None
        if guild_id_int is not None:
            if guild_id_int in _CWL_HUB_TOGGLE_IN_PROGRESS:
                return
            _CWL_HUB_TOGGLE_IN_PROGRESS.add(guild_id_int)
        try:
            if not await _check_cwl_admin_permission(interaction):
                return
            await interaction.response.defer(thinking=False, ephemeral=False)
            if not interaction.guild:
                return
            from qapbot.i18n import t

            guild_id_str = str(guild_id_int)
            config = CACHE.server_config.setdefault(guild_id_str, {})
            currently_enabled = bool(config.get("cwl_management_message_enabled", False))

            if not currently_enabled and not config.get("cwl_management_channel_id"):
                await interaction.followup.send(
                    t('cwl.settings.no_channel_set', guild_id=guild_id_int),
                    ephemeral=True,
                )
                return

            config["cwl_management_message_enabled"] = not currently_enabled
            await CACHE.persist_server_config(guild_id_str)

            # Post/delete the anchored Hub message immediately rather than waiting for the next
            # periodic cycle or bot restart — mirrors ClanManagementView._on_toggle_registration.
            # AWAITED (tracker #0066), not fire-and-forget: when this toggle button lives ON the
            # anchored Hub message itself (entry point b), a backgrounded repost could still be
            # mid-flight -- deleting/replacing that very message -- while _refresh_parent() just
            # below concurrently tried to read/edit it, intermittently leaving the visible button
            # in its pre-toggle state until a manual Refresh. Awaiting first guarantees
            # _refresh_parent() always runs against the fully-settled post-repost state.
            try:
                from QapBot import repost_cwl_management_messages
                await repost_cwl_management_messages(only_if_not_bottom=False, guild_id=guild_id_int)
            except Exception as e:
                logging.warning(f"Could not update CWL Management Hub message immediately: {e}")

            await _refresh_parent(view, interaction, "cwl_settings")
        finally:
            if guild_id_int is not None:
                _CWL_HUB_TOGGLE_IN_PROGRESS.discard(guild_id_int)

    return callback


def _make_cwl_settings_toggle_player_hub_callback(view: discord.ui.View):
    """Structural copy of _make_cwl_settings_toggle_callback above, for the Player CWL Settings
    Hub instead of the admin CWL Management Hub (plans/cwl-personal-hub.md Phase 2b)."""
    async def callback(interaction: discord.Interaction) -> None:
        guild_id_int = interaction.guild.id if interaction.guild else None
        if guild_id_int is not None:
            if guild_id_int in _CWL_PLAYER_HUB_TOGGLE_IN_PROGRESS:
                return
            _CWL_PLAYER_HUB_TOGGLE_IN_PROGRESS.add(guild_id_int)
        try:
            if not await _check_cwl_admin_permission(interaction):
                return
            await interaction.response.defer(thinking=False, ephemeral=False)
            if not interaction.guild:
                return
            from qapbot.i18n import t

            guild_id_str = str(guild_id_int)
            config = CACHE.server_config.setdefault(guild_id_str, {})
            currently_enabled = bool(config.get("cwl_player_hub_message_enabled", False))

            if not currently_enabled and not config.get("cwl_player_hub_channel_id"):
                await interaction.followup.send(
                    t('cwl.settings.no_player_hub_channel_set', guild_id=guild_id_int),
                    ephemeral=True,
                )
                return

            config["cwl_player_hub_message_enabled"] = not currently_enabled
            await CACHE.persist_server_config(guild_id_str)

            # AWAITED, not fire-and-forget — same race fix as the admin hub toggle above.
            try:
                from QapBot import repost_cwl_player_hub_messages
                await repost_cwl_player_hub_messages(only_if_not_bottom=False, guild_id=guild_id_int)
            except Exception as e:
                logging.warning(f"Could not update Player CWL Settings Hub message immediately: {e}")

            # Tracker #0070: the line above reposts the PLAYER-facing hub, but the ADMIN anchored
            # CWL Management Hub message also displays this toggle's own status/button as part of
            # its cwl_settings screen — without this, toggling from a DIFFERENT surface than that
            # anchored message (e.g. /clan management's own session-scoped screen) left it stuck
            # showing the pre-toggle state indefinitely, until someone happened to click its
            # manual Refresh button. No explicit mode: this handler has no idea which screen the
            # anchored message currently shows (see refresh_cwl_management_hub_message's own
            # docstring) — auto-detected from the live message instead, so this can never force
            # it onto cwl_settings if it was actually showing cwl_management.
            await refresh_cwl_management_hub_message(guild_id_int)

            await _refresh_parent(view, interaction, "cwl_settings")
        finally:
            if guild_id_int is not None:
                _CWL_PLAYER_HUB_TOGGLE_IN_PROGRESS.discard(guild_id_int)

    return callback


def _make_cwl_settings_retention_button_callback(view: discord.ui.View):
    async def callback(interaction: discord.Interaction) -> None:
        if not await _check_cwl_admin_permission(interaction):
            return
        if not interaction.guild:
            return
        guild_config = CACHE.server_config.get(str(interaction.guild.id), {})
        current_months = guild_config.get("cwl_retention_months", 0)
        await interaction.response.send_modal(CwlRetentionModal(view, interaction.guild.id, current_months))

    return callback


def _make_cwl_settings_toggle_include_all_accounts_callback(view: discord.ui.View):
    async def callback(interaction: discord.Interaction) -> None:
        if not await _check_cwl_admin_permission(interaction):
            return
        await interaction.response.defer(thinking=False, ephemeral=False)
        if not interaction.guild:
            return
        guild_id_str = str(interaction.guild.id)
        config = CACHE.server_config.setdefault(guild_id_str, {})
        config["cwl_enrollment_include_all_linked_accounts"] = not bool(
            config.get("cwl_enrollment_include_all_linked_accounts", False)
        )
        await CACHE.persist_server_config(guild_id_str)
        # Tracker #0070: also push this to the ADMIN anchored CWL Management Hub message, which
        # displays this same setting as part of its cwl_settings screen — see the identical
        # comment on the Player Hub toggle above for why this call exists and why it passes no
        # explicit mode.
        await refresh_cwl_management_hub_message(interaction.guild.id)
        await _refresh_parent(view, interaction, "cwl_settings")

    return callback


class CwlRetentionModal(discord.ui.Modal):
    """Radio-button retention picker, opened from a button rather than shown as an inline
    dropdown on the cwl_settings screen (project owner preference) — uses discord.py 2.7's
    RadioGroup/Label modal components (Components V2); classic discord.py Modals only support
    TextInput, and Discord has no standalone radio-button component outside a modal.
    """

    RETENTION_MONTHS: Tuple[int, ...] = (0, 3, 6, 12, 24)

    def __init__(self, parent_view: discord.ui.View, guild_id: int, current_months: int):
        from qapbot.i18n import t

        super().__init__(title=t('cwl.settings.retention_modal_title', guild_id=guild_id))
        self.parent_view = parent_view
        self.guild_id = guild_id

        options = [
            discord.RadioGroupOption(
                label=(
                    t('cwl.settings.retention_never', guild_id=guild_id)
                    if months == 0
                    else t('cwl.settings.retention_months', guild_id=guild_id, months=months)
                ),
                value=str(months),
                default=(months == current_months),
            )
            for months in self.RETENTION_MONTHS
        ]
        self.radio_group: discord.ui.RadioGroup[Any] = discord.ui.RadioGroup(
            custom_id="cwl_retention_radio",
            options=options,
        )
        self.add_item(discord.ui.Label(
            text=t('cwl.settings.retention_block_title', guild_id=guild_id),
            component=self.radio_group,
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=False, ephemeral=False)
        value = self.radio_group.value
        months = int(value) if value is not None else 0
        guild_id_str = str(self.guild_id)
        config = CACHE.server_config.setdefault(guild_id_str, {})
        config["cwl_retention_months"] = months
        await CACHE.persist_server_config(guild_id_str)
        # Tracker #0070: also push this to the ADMIN anchored CWL Management Hub message — see
        # the identical comment on the Player Hub toggle above for why.
        if interaction.guild is not None:
            await refresh_cwl_management_hub_message(interaction.guild.id)
        await _refresh_parent(self.parent_view, interaction, "cwl_settings")


# ---------------------------------------------------------------------------
# cwl_management — season operations (shared content layer)
# ---------------------------------------------------------------------------

def add_cwl_management_components(view: discord.ui.View, guild_id: int) -> None:
    """Attach the cwl_management screen's buttons (+ season select, if any seasons exist) to
    *view* — either a ClanManagementView (entry point a) or a CwlManagementHubView (entry point
    b, Phase 1)."""
    from qapbot.i18n import t
    from qapbot.QBdiscocmdshelper_cwl import resolve_selected_cwl_season

    db = CACHE.db_manager
    season = resolve_selected_cwl_season(guild_id)
    event = db.get_cwl_event_sync(str(guild_id), season) if db is not None else None
    participating_clans = (
        [c for c in db.get_cwl_event_clans_sync(event["id"]) if c.get("participating", 1)]
        if event is not None and db is not None
        else []
    )

    # Season select (Phase E.3) — row 1, directly below row 0 (reserved by both possible shells:
    # ClanManagementView's refresh button, CwlManagementHubView's Settings/Season Management
    # toggle) and above the action buttons (row 3), per the project owner's explicit request to
    # put it above the buttons. Only shown once at least one season exists; before that, "Add
    # New Season" is the only way to get started (see the no_event embed text).
    #
    # No option is ever marked `default=True`: the season is already shown prominently in the
    # embed's own "Season **{season}** — {status}" header, so the select always displays its
    # `placeholder` ("Select CWL season:") as a static caption instead of duplicating that value
    # — Discord's classic components have no separate label element to attach text to a select,
    # this is the idiomatic way bots achieve one.
    events = db.list_cwl_events_sync(str(guild_id)) if db is not None else []
    if events:
        season_select: discord.ui.Select[Any] = discord.ui.Select(
            placeholder=t('cwl.management.season_select_placeholder', guild_id=guild_id),
            options=[discord.SelectOption(label=e["cwl_season"], value=e["cwl_season"]) for e in events[:25]],
            row=1,
            custom_id="cwl_management_season_select",
        )
        season_select.callback = _make_cwl_management_season_select_callback(view)  # type: ignore[assignment]
        view.add_item(season_select)  # type: ignore[arg-type]

    # "Configure Participating Clans" opens the web Activity (CWL_CLAN_CONFIG_ACTIVITY_PLAN.md)
    # for whichever season is currently selected (the season select above, or its persisted
    # default) — it never creates a season or offers carry-over itself; that's exclusively
    # "Add New Season"'s job (Phase E.3), so this button carries no season-resolution logic at
    # all beyond what the bridge already does.
    configure_button = discord.ui.Button(
        label=t('cwl.management.button_configure_clans', guild_id=guild_id),
        style=discord.ButtonStyle.primary,
        custom_id="cwl_management_configure_clans",
        row=3,
        # Disabled until a season exists to configure — the bridge's POST refuses to save
        # without one (it never creates a season itself, see the comment above), so there's
        # nothing this button could productively do before "Add New Season" has run once.
        disabled=(event is None),
    )
    configure_button.callback = _make_cwl_management_open_web_callback(view)  # type: ignore[assignment]
    view.add_item(configure_button)  # type: ignore[arg-type]

    # "Manage CWL Coordinators" (tracker #0046) — unlike every other button in this row, NOT
    # season-scoped: coordinators are standing per-clan config (see cwl_clan_coordinators' own
    # CREATE TABLE comment, db_manager.py) that carries forward automatically every CWL month, so
    # this button is never gated on `event` existing. Placed in row 3 anyway, right after
    # "Configure Participating Clans" (the other per-clan-config action), rather than getting its
    # own row — rows 0-2 are already fully claimed in one or both of this function's two calling
    # shells (row 0: refresh/hub-toggle buttons; row 1: the season select above; row 2:
    # ClanManagementView's own mode select, ui_clan_management.py) and row 3 always has at least
    # one free slot (configure/start/delete is 3, +add_season is at most 4, Discord's cap is 5).
    coordinators_button = discord.ui.Button(
        label=t('cwl.management.button_manage_coordinators', guild_id=guild_id),
        style=discord.ButtonStyle.secondary,
        custom_id="cwl_management_manage_coordinators",
        row=3,
    )
    coordinators_button.callback = _make_cwl_management_coordinators_callback(view)  # type: ignore[assignment]
    view.add_item(coordinators_button)  # type: ignore[arg-type]

    # Start Enrollment / Manage Assignment — one dynamically-labeled button in the same row-3
    # slot ("Manage Enrollment", 2026-08-10): "Start Enrollment" while the event is still draft
    # (unchanged Phase 2 gating/callback), "Manage Assignment" once signup_open or later — opens
    # the same web Activity mechanism as "Configure Participating Clans" below, just recording a
    # different pending screen (see the plan doc's architectural decision for why screen
    # selection goes through CACHE.pending_cwl_activity_screen rather than routing by event
    # status). Never disabled once reached — an event only reaches signup_open with clans
    # already configured.
    if event is None or event["status"] == "draft":
        start_button = discord.ui.Button(
            label=t('cwl.management.button_start_enrollment', guild_id=guild_id),
            style=discord.ButtonStyle.success,
            custom_id="cwl_management_start_enrollment",
            row=3,
            disabled=(event is None or not participating_clans),
        )
        start_button.callback = _make_cwl_management_start_enrollment_callback(view)  # type: ignore[assignment]
    else:
        start_button = discord.ui.Button(
            label=t('cwl.management.button_manage_assignment', guild_id=guild_id),
            style=discord.ButtonStyle.success,
            custom_id="cwl_management_manage_assignment",
            row=3,
        )
        start_button.callback = _make_cwl_management_open_enrollment_web_callback(view)  # type: ignore[assignment]
    view.add_item(start_button)  # type: ignore[arg-type]

    # Mainly for testing/starting over — not gated behind a later phase like the two buttons
    # above, so it's only disabled when there's genuinely nothing to delete.
    delete_button = discord.ui.Button(
        label=t('cwl.management.button_delete_season', guild_id=guild_id),
        style=discord.ButtonStyle.danger,
        custom_id="cwl_management_delete_season",
        row=3,
        disabled=(event is None),
    )
    delete_button.callback = _make_cwl_management_delete_callback(view)  # type: ignore[assignment]
    view.add_item(delete_button)  # type: ignore[arg-type]

    # 5th and last slot in row 3 (Discord's per-row button cap) — the sole place that creates a
    # season and/or offers the carry-over-from-last-month prompt (Phase E.3/E.4). Omitted
    # entirely (not just disabled) once adding one is genuinely impossible — 2026-08-16,
    # live-testing feedback, project owner's spec: "when adding a new season is not possible, the
    # corresponding button should not be visible" — a deliberate exception to this screen's usual
    # "present but greyed out" convention for the other action buttons, since this one stays
    # unusable for the entire rest of the current CWL month once the season for it already
    # exists, not just until some nearer, more interesting condition is met.
    from qapbot.QBdiscocmdshelper_cwl import resolve_current_cwl_season

    target_season = resolve_current_cwl_season()
    if db is None or db.get_cwl_event_sync(str(guild_id), target_season) is None:
        add_season_button = discord.ui.Button(
            label=t('cwl.management.button_add_season', guild_id=guild_id),
            style=discord.ButtonStyle.success,
            custom_id="cwl_management_add_season",
            row=3,
        )
        add_season_button.callback = _make_cwl_management_add_season_callback(view)  # type: ignore[assignment]
        view.add_item(add_season_button)  # type: ignore[arg-type]

    # "Notify New Pool Members" (rule h, 2026-08-18, CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md)
    # — shown only once enrollment has actually started AND there's at least one pool member who
    # hasn't been sent the enrollment DM yet, by ANY guild, this season (see
    # has_cwl_pool_members_missing_dm's own docstring — it checks both conditions: event status
    # not in draft/cancelled, and at least one DM-able pool tag with dm_sent still false). Omitted
    # entirely rather than just disabled — matching "Add New Season"'s own "don't show what's
    # currently pointless" convention above — since this button has nothing to do the overwhelming
    # majority of the time. Row 4 (2026-08-19, project owner's spec: below the main action-button
    # line, not above it) is the last row Discord allows (0-4) and otherwise unused here, so this
    # always renders as the final row regardless of how many of the rows above it are present.
    if event is not None and event["status"] not in ("draft", "cancelled"):
        from qapbot.QBdiscocmdshelper_cwl import has_cwl_pool_members_missing_dm

        if has_cwl_pool_members_missing_dm(guild_id, season):
            notify_new_members_button = discord.ui.Button(
                label=t('cwl.management.button_notify_new_members', guild_id=guild_id),
                style=discord.ButtonStyle.success,
                custom_id="cwl_management_notify_new_members",
                row=4,
            )
            notify_new_members_button.callback = _make_cwl_management_notify_new_members_callback(view)  # type: ignore[assignment]
            view.add_item(notify_new_members_button)  # type: ignore[arg-type]

    # "Remind Pending" (tracker #0038, 2026-08-23) — same "omit entirely rather than disable,
    # row 4" convention as "Notify New Pool Members" directly above, gated by
    # has_cwl_pending_signups_to_remind (mirrors has_cwl_pool_members_missing_dm's own shape:
    # event status not draft/cancelled, at least one still-pending signup with a live Discord
    # link to actually DM).
    if event is not None and event["status"] not in ("draft", "cancelled"):
        from qapbot.QBdiscocmdshelper_cwl import has_cwl_pending_signups_to_remind

        if has_cwl_pending_signups_to_remind(guild_id, season):
            remind_pending_button = discord.ui.Button(
                label=t('cwl.management.button_remind_pending', guild_id=guild_id),
                style=discord.ButtonStyle.success,
                custom_id="cwl_management_remind_pending",
                row=4,
            )
            remind_pending_button.callback = _make_cwl_management_remind_pending_callback(view)  # type: ignore[assignment]
            view.add_item(remind_pending_button)  # type: ignore[arg-type]

    if event is not None:
        # Surfaced so an admin opening this screen can see at a glance whether every
        # participating clan already has a start time set (Finalize, Phase 4, will require it).
        missing_start = [c["clan_tag"] for c in participating_clans if not c.get("cwl_start_at")]
        if missing_start:
            logging.debug(f"[CWL] guild {guild_id} event {event['id']}: {len(missing_start)} clan(s) missing a start time")


def _make_cwl_management_delete_callback(view: discord.ui.View):
    async def callback(interaction: discord.Interaction) -> None:
        if not await _check_cwl_admin_permission(interaction):
            return
        await interaction.response.defer(thinking=False, ephemeral=True)
        if not interaction.guild:
            return
        from qapbot.QBdiscocmdshelper_cwl import resolve_selected_cwl_season

        db = CACHE.db_manager
        season = resolve_selected_cwl_season(interaction.guild.id)
        event = db.get_cwl_event_sync(str(interaction.guild.id), season) if db is not None else None
        if event is None:
            return

        # Delete-season guard preview (2026-08-15) — read-only, so the admin sees which clans
        # (if any) are shared with another guild BEFORE confirming, not after.
        from qapbot.QBdiscocmdshelper_cwl import get_cwl_event_shared_clan_info_sync
        shared_clan_info = get_cwl_event_shared_clan_info_sync(event["id"], interaction.guild.id, season)

        confirm_view = CwlDeleteSeasonConfirmView(
            parent_view=view,
            guild_id=interaction.guild.id,
            event_id=event["id"],
            season=event["cwl_season"],
            shared_clan_info=shared_clan_info,
        )
        await interaction.followup.send(
            confirm_view._build_content(),  # type: ignore[attr-defined]
            view=confirm_view,
            ephemeral=True,
        )

    return callback


def _make_cwl_management_season_select_callback(view: discord.ui.View):
    async def callback(interaction: discord.Interaction) -> None:
        if not await _check_cwl_admin_permission(interaction):
            return
        if not interaction.guild or not isinstance(interaction.data, dict):
            return
        values = interaction.data.get("values") or []
        if not values:
            return
        await interaction.response.defer(thinking=False, ephemeral=False)
        guild_id_str = str(interaction.guild.id)
        config = CACHE.server_config.setdefault(guild_id_str, {})
        config["cwl_selected_season"] = values[0]
        await CACHE.persist_server_config(guild_id_str)
        await _refresh_parent(view, interaction, "cwl_management")
        # Step 8 (2026-08-17, CWL_PROD_PERFORMANCE_FIX_PLAN.md): switching the selected season
        # changes what the web Activity's enrollment payload IS (it's season-scoped), not just a
        # signup/assignment row within it — an open board must still be told, or it'd keep
        # showing the old season's roster indefinitely.
        from qapbot.web_bridge import bump_enrollment_version
        await bump_enrollment_version(interaction.guild.id)

    return callback


def _make_cwl_management_add_season_callback(view: discord.ui.View):
    async def callback(interaction: discord.Interaction) -> None:
        if not await _check_cwl_admin_permission(interaction):
            return
        if not interaction.guild:
            return
        from qapbot.i18n import t
        from qapbot.QBdiscocmdshelper_cwl import resolve_current_cwl_season

        db = CACHE.db_manager
        guild_id_int = interaction.guild.id
        guild_id_str = str(guild_id_int)
        target_season = resolve_current_cwl_season()

        if db is None:
            await _respond_or_log(
                interaction, t('cwl.management.add_season_db_unavailable', guild_id=guild_id_int),
            )
            return

        # Every db.*_sync() call below is asyncio.to_thread()-wrapped (2026-08-16, live-testing
        # feedback: PROD incident — a real season-create here blocked on a periodic-update-cycle
        # sync write that was mid-transaction, and because this call ran directly on the event
        # loop thread, the WHOLE bot froze for ~5s waiting on _sync_write_lock/PRAGMA
        # busy_timeout=5000 in db_manager.py — every other guild's concurrent CoC API call
        # stalled in lockstep, and this interaction itself blew past Discord's 3s ack window and
        # died as "Unknown interaction"). to_thread() genuinely fixes the whole-bot-freeze half of
        # this (unlike COPILOT_PITFALLS_COOKBOOK.md Pitfall 16's gc.collect()/hashlib case, both
        # threading.Lock.acquire() and CPython's sqlite3 busy-wait release the GIL while blocked,
        # so the event loop keeps running other guilds' work) — it does NOT guarantee THIS
        # interaction beats the 3s deadline if the wait is genuinely that long, which is why every
        # response after one of these calls goes through _respond_or_log() instead of a bare
        # interaction.response.send_message() call that would raise an unhandled 404 on a stale
        # interaction.
        existing_event = await asyncio.to_thread(db.get_cwl_event_sync, guild_id_str, target_season)
        if existing_event is not None:
            await _respond_or_log(
                interaction,
                t('cwl.management.add_season_already_exists', guild_id=guild_id_int, season=target_season),
            )
            return

        # Carry-over-vs-defaults is decided exclusively here — never by "Configure Participating
        # Clans"/the web Activity (Phase E.3's explicit instruction).
        previous_rows = await asyncio.to_thread(db.get_previous_cwl_event_clans_sync, guild_id_str)
        if previous_rows:
            try:
                await interaction.response.defer(thinking=False, ephemeral=True)
            except discord.errors.NotFound:
                logging.warning("[CWL] Add New Season: interaction expired before defer() (carry-over path)")
                return
            prompt_view = CwlCarryOverPromptView(
                parent_view=view, guild_id=guild_id_int, target_season=target_season, previous_rows=previous_rows,
            )
            await interaction.followup.send(
                prompt_view._build_content(), view=prompt_view, ephemeral=True,  # type: ignore[attr-defined]
            )
            return

        # No previous data to offer — create the season outright with plain defaults.
        event_id = await asyncio.to_thread(
            db.create_cwl_event_sync, guild_id_str, target_season, str(interaction.user.id),
        )
        if event_id is None:
            await _respond_or_log(interaction, t('cwl.management.add_season_failed', guild_id=guild_id_int))
            return
        # Auto-enable every family clan (2026-08-16 follow-up, live-testing feedback, project
        # owner's spec: "if after the previous cwls no clan in the guild is enabled then
        # auto-enable all clans of the guild. It doesn't make sense that no clan is enabled after
        # the season was created"). The first fix for this only covered the carry-over ("Yes")
        # path inside CwlCarryOverPromptView._create_season — it never touched THIS branch, which
        # fires whenever get_previous_cwl_event_clans_sync() finds no previously-participating
        # rows at all (a genuinely brand-new guild, or a guild whose prior season also had zero
        # clans enabled) and skips the carry-over prompt entirely, leaving zero cwl_event_clans
        # rows — the exact same "nothing checked" symptom, reached via a completely different
        # code path the first fix never covered. No history to preserve a split from here (there
        # IS no prior data at all), so every family clan is unconditionally enabled.
        #
        # cwl_start_at explicitly set to the same default _build_clan_config_payload() would have
        # shown for a nonexistent row (2026-08-16 follow-up, live-testing feedback: writing a real
        # row here — needed for participating=True to persist — meant that fallback no longer
        # applied, since it only ever fires when NO row exists at all; the date field rendered
        # empty instead of pre-filled once a row with cwl_start_at=NULL existed instead of no row).
        from qapbot.QBdiscocmdshelper_cwl import resolve_guild_member_clan_tags

        family_clan_tags = resolve_guild_member_clan_tags(guild_id_int)
        if family_clan_tags:
            default_start_at = f"{target_season}-01T08:00Z"
            await asyncio.to_thread(
                db.set_cwl_event_clans_sync,
                event_id,
                [
                    {
                        "clan_tag": clan_tag, "roster_size": 15, "tier_order": 0,
                        "cwl_start_at": default_start_at, "participating": True,
                    }
                    for clan_tag in family_clan_tags
                ],
            )
        config = CACHE.server_config.setdefault(guild_id_str, {})
        config["cwl_selected_season"] = target_season
        await CACHE.persist_server_config(guild_id_str)
        # Refresh the screen that hosted this button BEFORE auto-launching the Activity below
        # (2026-08-16 regression fix, live-testing feedback: "/clan management"'s own CWL Season
        # Management screen kept showing "No CWL event has been configured yet" after Add New
        # Season, even though the persistent Hub message updated correctly). Root cause: this used
        # to rely solely on POST /api/cwl/activity-closed's refresh_cwl_management_hub_message()
        # call once the Activity closes — but that function ONLY knows how to resolve and edit a
        # guild's anchored Hub message (channel_id/message_id from server_config); it has no idea
        # the ephemeral "/clan management" message (ClanManagementView, entry point a) even exists,
        # so that message was never touched. _refresh_parent() is safe to call here despite
        # _launch_cwl_activity's "must be interaction's first response" constraint below — neither
        # ClanManagementView.refresh_cwl_view() nor CwlManagementHubView.refresh_cwl_view() ever
        # touch `interaction.response` (both edit a message directly via the bot's own REST client:
        # self.sent_message.edit() / refresh_cwl_management_hub_message()'s channel.fetch_message()
        # + .edit()), so this doesn't consume the interaction's one response slot. For the Hub
        # message specifically this means it now gets refreshed twice (once here, once again via
        # activity-closed) — a harmless no-op duplicate, not worth special-casing away just to
        # avoid it, given the alternative is the ephemeral screen never refreshing at all.
        await _refresh_parent(view, interaction, "cwl_management")
        # Step 8 (2026-08-17): a brand-new event + its initial cwl_event_clans rows — an open
        # board (for this guild specifically; a fresh season has no shared clans yet) must refetch.
        from qapbot.web_bridge import bump_enrollment_version
        await bump_enrollment_version(interaction.guild.id)
        # Auto-opens "Configure Participating Clans" as the logical next step (2026-08-16,
        # live-testing feedback, project owner's spec: "after adding a new season we should add a
        # logic that the Configure Participating Clans view is opened automatically as a logical
        # consequence of adding a new season") — the DB work above is now to_thread()-wrapped (see
        # the comment above) so it no longer freezes the whole bot while waiting on a lock, but it
        # can still legitimately outlast this — LAUNCH_ACTIVITY has no deferred form (see
        # _launch_cwl_activity's docstring), so this remains the interaction's own first response;
        # _launch_cwl_activity already degrades gracefully (falls back to a plain text hint, then
        # swallows the error) if the interaction expired while we were waiting.
        await _launch_cwl_activity(interaction, guild_id_int, "clan_config")

    return callback


class CwlCarryOverPromptView(discord.ui.View):
    """Yes/No prompt shown by "Add New Season" when a previous season has participating clans
    to offer as a template (Phase E.4). Carry-over logic lives exclusively here — never in
    "Configure Participating Clans"/the web Activity, per the project owner's explicit
    instruction that only this button creates seasons or takes over defaults.

    "Yes" (2026-08-15 redesign, project owner's spec): the *participating* flag it pre-sets is
    no longer copied from the previous season's manually-toggled cwl_event_clans rows (a clan
    the admin forgot to flip on, or one added to the family since, silently stayed missing) —
    it's looked up fresh from real CWL war history for every clan currently in the guild's
    family, via get_clans_with_cwl_data_for_season_sync(). A clan that actually played last
    season gets participating=True; every other family clan (played-but-not-last-season, or
    never toggled at all) gets an explicit participating=False row. The other per-clan settings
    (roster_size/target_league_rank/cwl_start_at/tier_order) still carry over from
    previous_rows where available, since only the true/false participation call was wrong, not
    those. See CwlCarryOverPromptView._create_season below."""

    def __init__(
        self,
        parent_view: discord.ui.View,
        guild_id: int,
        target_season: str,
        previous_rows: List[Dict[str, Any]],
        timeout: int = 60,
    ):
        super().__init__(timeout=timeout)
        self.parent_view = parent_view
        self.guild_id = guild_id
        self.target_season = target_season
        self.previous_rows = previous_rows

        from qapbot.i18n import t

        yes_button: discord.ui.Button[Any] = discord.ui.Button(
            label=t('cwl.management.add_season_carry_over_yes', guild_id=guild_id),
            style=discord.ButtonStyle.primary,
            custom_id="cwl_carry_over_yes",
        )
        yes_button.callback = self._on_yes  # type: ignore[assignment]
        self.add_item(yes_button)

        no_button: discord.ui.Button[Any] = discord.ui.Button(
            label=t('cwl.management.add_season_carry_over_no', guild_id=guild_id),
            style=discord.ButtonStyle.secondary,
            custom_id="cwl_carry_over_no",
        )
        no_button.callback = self._on_no  # type: ignore[assignment]
        self.add_item(no_button)

    def _build_content(self) -> str:
        from qapbot.i18n import t

        return t('cwl.management.add_season_carry_over_prompt', guild_id=self.guild_id, season=self.target_season)

    async def _create_season(self, discord_user_id: int, apply_carry_over: bool) -> None:
        # asyncio.to_thread()-wrapped for the same reason as
        # _make_cwl_management_add_season_callback's direct-create path — see that comment for
        # the full PROD-incident writeup. This path does strictly more sequential DB work than
        # that one (an extra get_previous_cwl_season_sync/get_clans_with_cwl_data_for_season_sync
        # round trip), so it's actually more exposed to the same risk, not less.
        db = CACHE.db_manager
        if db is None:
            return
        guild_id_str = str(self.guild_id)
        event_id = await asyncio.to_thread(
            db.create_cwl_event_sync, guild_id_str, self.target_season, str(discord_user_id),
        )
        if event_id is not None and apply_carry_over:
            from qapbot.QBdiscocmdshelper_cwl import resolve_guild_member_clan_tags

            family_clan_tags = resolve_guild_member_clan_tags(self.guild_id)
            previous_season = await asyncio.to_thread(
                db.get_previous_cwl_season_sync, guild_id_str, exclude_event_id=event_id,
            )
            played_last_season = (
                await asyncio.to_thread(db.get_clans_with_cwl_data_for_season_sync, family_clan_tags, previous_season)
                if previous_season else set()
            )
            # Non-participation settings (roster_size etc.) still come from the admin's last
            # manual config where one exists — only the true/false participation call itself
            # moved to real war history (previous_rows is participating=1-only, so a clan
            # re-enrolled here via real history but never manually toggled on before just gets
            # this dict's plain defaults, same as a brand-new family clan would).
            previous_settings = {r["clan_tag"]: r for r in self.previous_rows}
            # Same default _build_clan_config_payload() would show for a nonexistent row
            # (2026-08-16, live-testing feedback: a clan with no prior settings — brand new to
            # the family, or only ever enabled via the auto-enable-all fallback below — got
            # cwl_start_at=NULL here, and once a real row exists that fallback in the payload
            # builder no longer applies, so the date field rendered empty instead of pre-filled).
            default_start_at = f"{self.target_season}-01T08:00Z"
            clan_configs = [
                {
                    "clan_tag": clan_tag,
                    "target_league_rank": previous_settings.get(clan_tag, {}).get("target_league_rank"),
                    "roster_size": previous_settings.get(clan_tag, {}).get("roster_size", 15),
                    "tier_order": previous_settings.get(clan_tag, {}).get("tier_order", 0),
                    "cwl_start_at": previous_settings.get(clan_tag, {}).get("cwl_start_at") or default_start_at,
                    "participating": clan_tag in played_last_season,
                }
                for clan_tag in family_clan_tags
            ]
            # Auto-enable-all fallback (2026-08-16, live-testing feedback, project owner's spec,
            # verbatim: "if after the previous cwls no clan in the guild is enabled then
            # auto-enable all clans of the guild. It doesn't make sense that no clan is enabled
            # after the season was created") — played_last_season comes from real war history for
            # the guild's *previous* season, so a guild with no tracked CWL history at all (brand
            # new to the bot, or simply took a season off) ends up with every family clan
            # correctly-but-uselessly defaulted to participating=False, handing the admin a new
            # season where literally nothing is checked. Only fires when EVERY family clan came
            # back False and there's at least one family clan to enable — a guild where some
            # clans genuinely didn't play last season while others did is left exactly as
            # computed, since that split is real, useful information, not a degenerate case.
            if family_clan_tags and not any(c["participating"] for c in clan_configs):
                for config in clan_configs:
                    config["participating"] = True
            await asyncio.to_thread(db.set_cwl_event_clans_sync, event_id, clan_configs)
        config = CACHE.server_config.setdefault(guild_id_str, {})
        config["cwl_selected_season"] = self.target_season
        await CACHE.persist_server_config(guild_id_str)

    async def _finish(self, interaction: discord.Interaction, apply_carry_over: bool) -> None:
        if not await _check_cwl_admin_permission(interaction):
            return
        await self._create_season(interaction.user.id, apply_carry_over)
        # Refresh the screen that originally hosted "Add New Season" (self.parent_view) — same
        # regression fix and same reasoning as _make_cwl_management_add_season_callback's own
        # direct-create path above; see that comment for the full writeup. Safe here for the same
        # reason: _refresh_parent() never touches `interaction.response`, so it doesn't consume
        # the interaction's one response slot that _launch_cwl_activity below still needs.
        await _refresh_parent(self.parent_view, interaction, "cwl_management")
        # Step 8 (2026-08-17): same reasoning as the direct-create path above — a brand-new event
        # (this time possibly carrying over roster_size/cwl_start_at/participating from last
        # season) needs an open board for this guild to refetch.
        from qapbot.web_bridge import bump_enrollment_version
        await bump_enrollment_version(interaction.guild.id)
        # Auto-opens "Configure Participating Clans" as the logical next step (2026-08-16,
        # live-testing feedback, project owner's spec — see _launch_cwl_activity's docstring for
        # the hard "must be this interaction's first response" constraint this relies on).
        # Trade-off: this consumes the interaction's one response slot, so — unlike the previous
        # defer()+delete_original_response() sequence — this Yes/No prompt message itself can no
        # longer be cleanly deleted afterward; it's left in place with its now-inert buttons
        # (Discord surfaces "This interaction failed" if clicked again) rather than skip the
        # auto-launch to preserve that cleanup.
        await _launch_cwl_activity(interaction, self.guild_id, "clan_config")

    async def _on_yes(self, interaction: discord.Interaction) -> None:
        await self._finish(interaction, apply_carry_over=True)

    async def _on_no(self, interaction: discord.Interaction) -> None:
        await self._finish(interaction, apply_carry_over=False)


async def _respond_or_log(interaction: discord.Interaction, content: str) -> None:
    """`interaction.response.send_message()`, but tolerant of an interaction that expired while a
    to_thread()-wrapped DB call ahead of it was waiting on a lock (2026-08-16, PROD incident — see
    the comment in `_make_cwl_management_add_season_callback`). Discord's ack window is ~3s;
    to_thread() stops that wait from freezing the whole bot but can't make Discord's clock run
    slower, so a genuinely slow write can still make this specific response arrive too late — that
    should log, not crash the gateway's event dispatch with an unhandled 404."""
    try:
        await interaction.response.send_message(content, ephemeral=True)
    except discord.errors.NotFound:
        logging.warning(f"[CWL] Interaction expired before response could be sent: {content!r}")


async def _launch_cwl_activity(interaction: discord.Interaction, guild_id: int, screen: str) -> None:
    """Opens the CWL_CLAN_CONFIG_ACTIVITY_PLAN.md Discord Activity in-context via the
    LAUNCH_ACTIVITY interaction-response callback (type 12) — flagged in the plan as unverified
    from a plain component interaction (only confirmed working for the auto-created Entry Point
    /launch command; live-testing this session confirms it does work from a component click).
    discord.py has no high-level wrapper for this callback type, so it's a raw REST call through
    the bot's own HTTPClient, matching the plan's documented mechanism.

    **Hard constraint (2026-08-16, live-testing feedback — this governs every caller below):**
    type 12 MUST be *interaction*'s very first response — Discord allows exactly one initial
    response per interaction, and LAUNCH_ACTIVITY has no "deferred" or "followup" variant. A
    caller that has already called `interaction.response.defer()`/`send_message()`/`edit_message()`
    for this exact interaction can never call this function afterwards for it — any DB work that
    needs to finish before the Activity opens (so its GET calls see the final state, not a stale
    one) must run to completion *before* calling this, not after. This is why "auto-open the
    Activity after Start Enrollment finishes" isn't implemented the same way: that operation sends
    a real multi-second DM blast, so its confirming interaction has to reply with immediate
    "processing…" feedback (consuming the interaction's one response slot) long before the result
    is known — there is no Discord-API-legal way to defer a response and still launch the Activity
    once the slow work completes. Only genuinely fast, synchronous completions (season creation)
    can auto-launch this way; anything that needs a "please wait" state cannot.
    """
    CACHE.pending_cwl_activity_screen[(str(guild_id), str(interaction.user.id))] = screen
    from discord.http import Route

    try:
        await interaction.client.http.request(
            Route(
                "POST",
                "/interactions/{interaction_id}/{interaction_token}/callback",
                interaction_id=interaction.id,
                interaction_token=interaction.token,
            ),
            json={"type": 12, "data": {}},  # 12 = LAUNCH_ACTIVITY
        )
    except Exception as e:
        logging.warning(f"[CWL] LAUNCH_ACTIVITY callback failed, falling back to a text hint: {e}")
        if not interaction.response.is_done():
            from qapbot.i18n import t
            # Dict lookup rather than a two-way ternary (2026-08-23, plans/cwl-personal-hub.md
            # Phase 5a) — a third screen ('player_prefs', the Player CWL Settings Hub button)
            # needs its own fallback text distinct from the other two.
            fallback_key = {
                "clan_config": 'cwl.management.open_web_fallback',
                "enrollment": 'cwl.management.open_enrollment_fallback',
                "player_prefs": 'cwl.player_hub.open_fallback',
            }.get(screen, 'cwl.management.open_web_fallback')
            try:
                await interaction.response.send_message(t(fallback_key, guild_id=guild_id), ephemeral=True)
            except Exception:
                pass


def _make_cwl_management_open_web_callback(view: discord.ui.View):
    """LAUNCH_ACTIVITY callback for the "Configure Participating Clans" button — see
    _launch_cwl_activity's own docstring for the underlying mechanism."""
    async def callback(interaction: discord.Interaction) -> None:
        if not await _check_cwl_admin_permission(interaction):
            return
        guild_id = interaction.guild.id if interaction.guild else None
        if guild_id is not None:
            await _launch_cwl_activity(interaction, guild_id, "clan_config")

    return callback


def _make_cwl_management_open_enrollment_web_callback(view: discord.ui.View):
    """Same LAUNCH_ACTIVITY mechanism as _make_cwl_management_open_web_callback above (see
    _launch_cwl_activity's own docstring), but records "enrollment" as the pending screen and is
    gated by the admin-or-leader permission tier rather than admin-only — this is the "Manage
    Assignment" button's callback (CWL_ROSTER_PLANNING_PLAN.md "Manage Enrollment" slice 5)."""
    async def callback(interaction: discord.Interaction) -> None:
        if not await _check_cwl_admin_or_leader_permission(interaction):
            return
        guild_id = interaction.guild.id if interaction.guild else None
        if guild_id is not None:
            await _launch_cwl_activity(interaction, guild_id, "enrollment")

    return callback


CWL_COORDINATOR_LIMIT = 2


def _make_cwl_management_coordinators_callback(view: discord.ui.View):
    """Opens the CwlCoordinatorConfigurationView (tracker #0046) for the "Manage CWL
    Coordinators" button — a fresh ephemeral message, same shape as
    _make_cwl_management_notify_new_members_callback below, rather than editing the anchored Hub
    message itself (this config isn't part of that message's own content)."""
    async def callback(interaction: discord.Interaction) -> None:
        if not await _check_cwl_admin_permission(interaction):
            return
        if not interaction.guild:
            return
        guild_id_int = interaction.guild.id

        from qapbot.i18n import t
        from qapbot.QBdiscocmdshelper_cwl import resolve_guild_member_clan_tags

        clan_tags = resolve_guild_member_clan_tags(guild_id_int)
        if not clan_tags:
            await interaction.response.send_message(
                t('cwl.management.coordinators_no_clans', guild_id=guild_id_int), ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)

        guild_id_str = str(guild_id_int)
        current_by_clan = CACHE.server_config.get(guild_id_str, {}).get("cwl_clan_coordinators", {})

        coordinators_view = CwlCoordinatorConfigurationView(
            guild=interaction.guild,
            clan_tags=clan_tags,
            current_coordinator_ids_by_clan=current_by_clan,
            timeout=300,
        )
        header = t('cwl.management.coordinators_header', guild_id=guild_id_int)
        await interaction.followup.send(header, view=coordinators_view, ephemeral=True)  # type: ignore[arg-type]

    return callback


class CwlCoordinatorConfigurationView(discord.ui.View):
    """Tracker #0046 — standing (season-independent) per-clan CWL Coordinators: up to
    CWL_COORDINATOR_LIMIT Discord users per clan, any member of the guild (not restricted to
    that clan's own roster — project owner's spec), persisted independent of any one season so
    they carry forward automatically every CWL month rather than needing to be re-picked. Mirrors
    ui_clan_management.CustodianConfigurationView's UserSelect/Clear/Save shape almost exactly,
    with one addition up front: a clan picker, since "Manage CWL Coordinators" is reached without
    the caller already knowing which single clan the admin wants (unlike CustodianConfigurationView,
    which is only ever opened from a screen already scoped to one clan_tag).
    """

    def __init__(
        self,
        guild: discord.Guild,
        clan_tags: List[str],
        current_coordinator_ids_by_clan: Dict[str, List[str]],
        timeout: int = 300,
    ):
        super().__init__(timeout=timeout)
        self.guild = guild
        self.clan_tags = clan_tags
        # Own working copy — edits here must never mutate the CACHE dict the caller passed in
        # until Save actually runs (same "hold in a working copy, only persist on an explicit
        # action" discipline the web Activity's clanConfigTable.ts uses).
        self.coordinators_by_clan: Dict[str, List[str]] = {
            tag: list(ids) for tag, ids in current_coordinator_ids_by_clan.items()
        }
        self.clan_tag = clan_tags[0]
        self._rebuild_counter = 0

        self._add_clan_select()
        self._add_user_select()
        self._add_clear_button()
        self._add_save_button()

    @property
    def coordinator_ids(self) -> List[str]:
        return self.coordinators_by_clan.get(self.clan_tag, [])

    def _add_clan_select(self) -> None:
        from qapbot.i18n import t
        guild_id = self.guild.id if self.guild else None

        options = []
        for tag in sorted(self.clan_tags, key=lambda tag: (CACHE.get_clan_name(tag, tag) or tag).lower())[:25]:
            name = CACHE.get_clan_name(tag, tag)
            label = f"{name} ({tag})"
            if len(label) > 100:
                label = label[:97] + "..."
            options.append(discord.SelectOption(label=label, value=tag, default=(tag == self.clan_tag)))

        clan_select: discord.ui.Select[Any] = discord.ui.Select(
            placeholder=t('ui_components.cwl_coordinator_configuration.placeholder_clan_select', guild_id=guild_id),
            min_values=1,
            max_values=1,
            options=options,  # type: ignore[arg-type]
            custom_id=f"cwl_coordinator_clan_select_{self._rebuild_counter}",
            row=0,
        )
        clan_select.callback = self._on_clan_select  # type: ignore[assignment]
        self.add_item(clan_select)  # type: ignore[arg-type]

    def _add_user_select(self) -> None:
        from qapbot.i18n import t
        guild_id = self.guild.id if self.guild else None
        default_values = [discord.Object(id=int(uid)) for uid in self.coordinator_ids] or None

        # Dynamic custom_id forces Discord to treat this as a new component after rebuilds (same
        # fix CustodianConfigurationView._add_user_select() applies for the same stale-selection
        # issue).
        user_select = discord.ui.UserSelect(
            placeholder=t('ui_components.cwl_coordinator_configuration.placeholder_user_select', guild_id=guild_id),
            min_values=0,
            max_values=CWL_COORDINATOR_LIMIT,
            custom_id=f"cwl_coordinator_user_select_{self._rebuild_counter}",
            row=1,
            default_values=default_values,  # type: ignore[arg-type]
        )
        user_select.callback = self._on_user_select  # type: ignore[assignment]
        self.add_item(user_select)  # type: ignore[arg-type]

    def _add_clear_button(self) -> None:
        from qapbot.i18n import t
        guild_id = self.guild.id if self.guild else None

        clear_button = discord.ui.Button(
            label=t('ui_components.cwl_coordinator_configuration.button_clear', guild_id=guild_id),
            style=discord.ButtonStyle.secondary,
            custom_id="cwl_coordinator_clear",
            row=2,
            disabled=(len(self.coordinator_ids) == 0),
        )
        clear_button.callback = self._on_clear  # type: ignore[assignment]
        self.add_item(clear_button)  # type: ignore[arg-type]

    def _add_save_button(self) -> None:
        from qapbot.i18n import t
        guild_id = self.guild.id if self.guild else None

        save_button = discord.ui.Button(
            label=t('ui_components.cwl_coordinator_configuration.button_save', guild_id=guild_id),
            style=discord.ButtonStyle.success,
            custom_id="cwl_coordinator_save",
            row=2,
        )
        save_button.callback = self._on_save  # type: ignore[assignment]
        self.add_item(save_button)  # type: ignore[arg-type]

    def _rebuild_view(self) -> None:
        self._rebuild_counter += 1
        self.clear_items()
        self._add_clan_select()
        self._add_user_select()
        self._add_clear_button()
        self._add_save_button()

    def _current_clan_label(self) -> str:
        return f"{CACHE.get_clan_name(self.clan_tag, self.clan_tag)} ({self.clan_tag})"

    def _current_mentions_text(self) -> str:
        if not self.coordinator_ids:
            return "❌ None selected"
        return " ".join(f"<@{uid}>" for uid in self.coordinator_ids)

    async def _on_clan_select(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=False, ephemeral=False)
        values = interaction.data.get('values', [])  # type: ignore[union-attr]
        if values:
            self.clan_tag = values[0]
        self._rebuild_view()
        await self._refresh_message(interaction)

    async def _on_user_select(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=False, ephemeral=False)
        self.coordinators_by_clan[self.clan_tag] = interaction.data.get('values', [])  # type: ignore[union-attr]
        self._rebuild_view()
        await self._refresh_message(interaction)

    async def _on_clear(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=False, ephemeral=False)
        self.coordinators_by_clan[self.clan_tag] = []
        self._rebuild_view()
        await self._refresh_message(interaction)

    async def _on_save(self, interaction: discord.Interaction) -> None:
        """Persist the coordinator configuration for the currently-selected clan only — matches
        CustodianConfigurationView._on_apply's own "one clan at a time" scope; switching clans
        via the picker without saving discards that clan's unsaved edits, same as navigating away
        from a form without submitting."""
        await interaction.response.defer(thinking=False, ephemeral=True)

        guild_id = str(self.guild.id)
        await CACHE.db_manager.save_cwl_clan_coordinators(guild_id, self.clan_tag, self.coordinator_ids)

        if guild_id not in CACHE.server_config:
            CACHE.server_config[guild_id] = {}
        coordinators = CACHE.server_config[guild_id].setdefault("cwl_clan_coordinators", {})
        if self.coordinator_ids:
            coordinators[self.clan_tag] = self.coordinator_ids
        else:
            coordinators.pop(self.clan_tag, None)

        from qapbot.i18n import t
        user_id = str(interaction.user.id)
        guild_id_for_t = interaction.guild.id if interaction.guild else None
        msg = t(
            'ui_components.cwl_coordinator_configuration.saved_message',
            user_id=user_id, guild_id=guild_id_for_t,
            clan=self._current_clan_label(), mentions=self._current_mentions_text(),
        )
        await interaction.followup.send(msg, ephemeral=True)

    async def _refresh_message(self, interaction: discord.Interaction) -> None:
        from qapbot.i18n import t
        user_id = str(interaction.user.id)
        guild_id_for_t = interaction.guild.id if interaction.guild else None
        msg = t(
            'ui_components.cwl_coordinator_configuration.updated_message',
            user_id=user_id, guild_id=guild_id_for_t,
            clan=self._current_clan_label(), mentions=self._current_mentions_text(),
        )
        await interaction.edit_original_response(content=msg, view=self)


class CwlDeleteSeasonConfirmView(discord.ui.View):
    """Confirm/cancel dialog for deleting a CWL event outright, mirroring
    ui_registration.py's UnlinkConfirmView. Mainly for testing/starting over — deletion
    cascades to cwl_event_clans/cwl_signups/cwl_assignments (ON DELETE CASCADE).

    shared_clan_info (2026-08-15, delete-season guard): the read-only preview from
    get_cwl_event_shared_clan_info_sync() (QBdiscocmdshelper_cwl.py), computed by the caller
    before this view is even shown — surfaced in _build_content() as a warning, and reused
    verbatim (not recomputed) once the admin actually confirms, so the preview and the real
    action can never disagree about what's affected."""

    def __init__(
        self, parent_view: discord.ui.View, guild_id: int, event_id: int, season: str,
        shared_clan_info: Optional[List[Dict[str, Any]]] = None, timeout: int = 60,
    ):
        super().__init__(timeout=timeout)
        self.parent_view = parent_view
        self.guild_id = guild_id
        self.event_id = event_id
        self.season = season
        self.shared_clan_info = shared_clan_info or []

        from qapbot.i18n import t

        confirm_button: discord.ui.Button[Any] = discord.ui.Button(
            label=t('cwl.management.button_confirm_delete', guild_id=guild_id),
            style=discord.ButtonStyle.danger,
            custom_id="cwl_delete_confirm",
        )
        confirm_button.callback = self._on_confirm  # type: ignore[assignment]
        self.add_item(confirm_button)

        cancel_button: discord.ui.Button[Any] = discord.ui.Button(
            label=t('cwl.setup.button_cancel', guild_id=guild_id),
            style=discord.ButtonStyle.secondary,
            custom_id="cwl_delete_cancel",
        )
        cancel_button.callback = self._on_cancel  # type: ignore[assignment]
        self.add_item(cancel_button)

    def _build_content(self) -> str:
        from qapbot.i18n import t

        content = t('cwl.management.delete_confirm_body', guild_id=self.guild_id, season=self.season)
        # Only clans that STAY shared with someone else are worth warning about — a shared clan
        # with no other guilds left attached is about to be genuinely, fully deleted, same as
        # any other clan in this event, so it doesn't need special-casing in the warning.
        clans_with_others = [info["clan_tag"] for info in self.shared_clan_info if info["other_guild_ids"]]
        if clans_with_others:
            content += t(
                'cwl.management.delete_confirm_shared_clan_warning',
                guild_id=self.guild_id, clans=", ".join(clans_with_others),
            )
        return content

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        if not await _check_cwl_admin_permission(interaction):
            return
        await interaction.response.defer(thinking=False, ephemeral=True)
        db = CACHE.db_manager
        if db is not None:
            # Delete-season guard (2026-08-15) — MUST run before delete_cwl_event_sync(): repoints
            # ownership away from this guild for any shared clan that still has another guild
            # attached (preserving that clan's shared roster), and prunes the shared record
            # outright only for a clan where this guild is the last one left. See
            # prune_or_detach_shared_clans_before_deletion's docstring for the full rationale.
            from qapbot.QBdiscocmdshelper_cwl import prune_or_detach_shared_clans_before_deletion
            await prune_or_detach_shared_clans_before_deletion(self.guild_id, self.event_id, self.season)
            # Snapshot which players got an enrollment DM via THIS event, and where, BEFORE
            # delete_cwl_event_sync() below clears their cwl_player_season_status rows (2026-08-19
            # fix, live bug report — see that function's own docstring for why it clears them at
            # all) — this is the only chance to retract those now-orphaned DMs' dangling Confirm/
            # Opt Out buttons afterward.
            dm_refs = await asyncio.to_thread(
                db.get_cwl_player_season_status_dm_refs_for_event_sync, self.event_id
            )
            # to_thread()-wrapped even though this callback already deferred above (so THIS
            # interaction is safe from the 3s ack deadline either way) — an un-wrapped sync write
            # here would still freeze the whole event loop for every OTHER concurrent guild/
            # interaction while it waits on _sync_write_lock/busy_timeout. See Pitfall 26,
            # COPILOT_PITFALLS_COOKBOOK.md.
            await asyncio.to_thread(db.delete_cwl_event_sync, self.event_id)
            if dm_refs:
                from qapbot.QBdiscocmdshelper_cwl import cleanup_stale_cwl_enrollment_dms
                cleanup_result = await cleanup_stale_cwl_enrollment_dms(interaction.client, dm_refs)
                logging.info(
                    f"[CWL-ENROLLMENT] Delete Season for event {self.event_id}: retracted "
                    f"{cleanup_result['deleted']}/{len(dm_refs)} stale enrollment DM(s), "
                    f"{cleanup_result['failed']} could not be removed."
                )
        # The season select (Phase E.3) can't keep pointing at a season that no longer has an
        # event — clear the persisted selection so the next open falls back to
        # resolve_selected_cwl_season()'s other-events/calendar-default resolution.
        guild_id_str = str(self.guild_id)
        config = CACHE.server_config.get(guild_id_str, {})
        if config.get("cwl_selected_season") == self.season:
            config["cwl_selected_season"] = None
            await CACHE.persist_server_config(guild_id_str)
        try:
            await interaction.delete_original_response()
        except discord.NotFound:
            pass
        await _refresh_parent(self.parent_view, interaction, "cwl_management")
        # Step 8 (2026-08-17): the whole event (and, via prune_or_detach_shared_clans_before_
        # deletion above, possibly another guild's shared-clan attachment/ownership) is gone —
        # that function doesn't report which other guild(s) it touched, so bump globally rather
        # than resolve it here; deletion is rare enough that this is cheap.
        from qapbot.web_bridge import bump_enrollment_version
        await bump_enrollment_version(None)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=False, ephemeral=True)
        try:
            await interaction.delete_original_response()
        except discord.NotFound:
            pass


# ---------------------------------------------------------------------------
# cwl_management — Start Enrollment (Phase 2)
# ---------------------------------------------------------------------------

def _make_cwl_management_start_enrollment_callback(view: discord.ui.View):
    async def callback(interaction: discord.Interaction) -> None:
        if not await _check_cwl_admin_permission(interaction):
            return
        await interaction.response.defer(thinking=False, ephemeral=True)
        if not interaction.guild:
            return
        from qapbot.QBdiscocmdshelper_cwl import resolve_selected_cwl_season

        db = CACHE.db_manager
        season = resolve_selected_cwl_season(interaction.guild.id)
        event = db.get_cwl_event_sync(str(interaction.guild.id), season) if db is not None else None
        if event is None:
            return
        confirm_view = CwlStartEnrollmentConfirmView(
            parent_view=view,
            guild_id=interaction.guild.id,
            season=event["cwl_season"],
        )
        await interaction.followup.send(
            confirm_view._build_content(),  # type: ignore[attr-defined]
            view=confirm_view,
            ephemeral=True,
        )

    return callback


class CwlStartEnrollmentConfirmView(discord.ui.View):
    """Confirm/cancel dialog for Start Enrollment, mirroring CwlDeleteSeasonConfirmView — this
    action sends real DMs to real members (the template-copy confirm/opt-out blast) and seeds
    cwl_signups, so it's deliberately not a single-click action."""

    def __init__(self, parent_view: discord.ui.View, guild_id: int, season: str, timeout: int = 60):
        super().__init__(timeout=timeout)
        self.parent_view = parent_view
        self.guild_id = guild_id
        self.season = season

        from qapbot.i18n import t

        confirm_button: discord.ui.Button[Any] = discord.ui.Button(
            label=t('cwl.management.button_confirm_start_enrollment', guild_id=guild_id),
            style=discord.ButtonStyle.success,
            custom_id="cwl_start_enrollment_confirm",
        )
        confirm_button.callback = self._on_confirm  # type: ignore[assignment]
        self.add_item(confirm_button)

        cancel_button: discord.ui.Button[Any] = discord.ui.Button(
            label=t('cwl.setup.button_cancel', guild_id=guild_id),
            style=discord.ButtonStyle.secondary,
            custom_id="cwl_start_enrollment_cancel",
        )
        cancel_button.callback = self._on_cancel  # type: ignore[assignment]
        self.add_item(cancel_button)

    def _build_content(self) -> str:
        from qapbot.i18n import t

        return t('cwl.management.start_enrollment_confirm_body', guild_id=self.guild_id, season=self.season)

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        if not await _check_cwl_admin_permission(interaction):
            return
        from qapbot.i18n import t

        # start_cwl_enrollment() below sends a real DM to every resolved member and does several
        # DB writes — takes a few real seconds. A bare defer() leaves the Yes/Cancel buttons
        # looking untouched the whole time, which invites a double-click; disable them and swap
        # to a "processing" label immediately so the click has visible, instant feedback.
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]
        await interaction.response.edit_message(
            content=t('cwl.management.start_enrollment_processing', guild_id=self.guild_id), view=self
        )
        from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

        summary = await start_cwl_enrollment(self.guild_id, self.season)
        if not summary["ok"]:
            content = t(f"cwl.management.start_enrollment_error_{summary['error']}", guild_id=self.guild_id)
        else:
            # Blocked (DMs closed/bot blocked) vs no_mutual_guild (recipient left every guild the
            # bot is in, 2026-08-23, tracker #0031) vs failed-after-retries (transient,
            # cache_manager.py's DM_SEND_MAX_RETRIES) get separate lines — an admin follows up
            # differently for each (2026-08-18, item 3 of the enrollment redesign: surface DM
            # problems instead of letting a batch abort silently).
            dm_issues = ""
            if summary["blocked"]:
                dm_issues += t(
                    'cwl.management.start_enrollment_dm_blocked_line',
                    guild_id=self.guild_id, names=", ".join(summary["blocked"]),
                )
            if summary["no_mutual_guild"]:
                dm_issues += t(
                    'cwl.management.start_enrollment_dm_no_mutual_guild_line',
                    guild_id=self.guild_id, names=", ".join(summary["no_mutual_guild"]),
                )
            if summary["failed"]:
                from qapbot.cache_manager import DM_SEND_MAX_RETRIES

                dm_issues += t(
                    'cwl.management.start_enrollment_dm_failed_line',
                    guild_id=self.guild_id, retries=DM_SEND_MAX_RETRIES, names=", ".join(summary["failed"]),
                )
            content = t(
                'cwl.management.start_enrollment_summary',
                guild_id=self.guild_id,
                seeded=summary["seeded"],
                assigned=summary["assigned"],
                contacted=summary["contacted"],
                skipped_optout=summary["skipped_optout"],
                skipped_unlinked=summary["skipped_unlinked"],
                skipped_dm_guard=summary["skipped_dm_guard"],
                dm_issues=dm_issues,
            )
        # A one-click "open Teams Management" follow-up (2026-08-16, live-testing feedback,
        # project owner's spec: "when starting the enrollment process the 'Teams Management' view
        # should be opened automatically after the enrollment start is finished") — true
        # zero-click auto-launch isn't achievable here the way it is for Add New Season: this
        # confirm click already had to respond immediately with "processing…" feedback (above),
        # long before start_cwl_enrollment()'s multi-second DM blast finishes, and LAUNCH_ACTIVITY
        # has no deferred form (see _launch_cwl_activity's own docstring) — there's no legal way to
        # defer this interaction's response and still launch the Activity once the result is
        # known. This button is the closest equivalent: it's already sitting in the completion
        # message, one click away, rather than making the admin navigate back to find "Manage
        # Assignment" again. Only shown on success — nothing to manage yet if it failed.
        follow_up_view = CwlOpenEnrollmentView(self.guild_id) if summary["ok"] else None
        try:
            await interaction.edit_original_response(content=content, view=follow_up_view)
        except discord.NotFound:
            pass
        if summary["ok"]:
            # Cross-guild shared-clan notifications (2026-08-15) — one of the two trigger points
            # (the other is handle_post_clan_config's guest-clan add, web_bridge.py). Best-effort,
            # after the main summary is already shown — a notification failure must never mask
            # the real Start Enrollment result.
            for shared in summary["shared_clans"]:
                await notify_cwl_clan_shared(
                    self.guild_id, shared["clan_tag"], self.season, shared,
                    acting_discord_id=interaction.user.id,
                )
            await _refresh_parent(self.parent_view, interaction, "cwl_management")
            # Step 8 (2026-08-17): the bulk signup/assignment seed always changes this guild's own
            # board; each summary["shared_clans"] entry (ensure_cwl_clan_sharing's own return
            # shape) already names the exact partner guild(s) precisely, same as
            # handle_post_clan_config's identical loop in web_bridge.py.
            from qapbot.web_bridge import bump_enrollment_version
            await bump_enrollment_version(self.guild_id)
            for shared in summary["shared_clans"]:
                for other_guild_id in shared.get("other_guild_ids", []):
                    await bump_enrollment_version(int(other_guild_id))

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=False, ephemeral=True)
        try:
            await interaction.delete_original_response()
        except discord.NotFound:
            pass


def _make_cwl_management_notify_new_members_callback(view: discord.ui.View):
    async def callback(interaction: discord.Interaction) -> None:
        if not await _check_cwl_admin_permission(interaction):
            return
        await interaction.response.defer(thinking=False, ephemeral=True)
        if not interaction.guild:
            return
        from qapbot.QBdiscocmdshelper_cwl import resolve_selected_cwl_season

        db = CACHE.db_manager
        season = resolve_selected_cwl_season(interaction.guild.id)
        event = db.get_cwl_event_sync(str(interaction.guild.id), season) if db is not None else None
        if event is None:
            return
        confirm_view = CwlNotifyNewMembersConfirmView(
            parent_view=view,
            guild_id=interaction.guild.id,
            season=event["cwl_season"],
        )
        await interaction.followup.send(
            confirm_view._build_content(),  # type: ignore[attr-defined]
            view=confirm_view,
            ephemeral=True,
        )

    return callback


def _make_cwl_management_remind_pending_callback(view: discord.ui.View):
    async def callback(interaction: discord.Interaction) -> None:
        if not await _check_cwl_admin_permission(interaction):
            return
        await interaction.response.defer(thinking=False, ephemeral=True)
        if not interaction.guild:
            return
        from qapbot.QBdiscocmdshelper_cwl import resolve_selected_cwl_season

        db = CACHE.db_manager
        season = resolve_selected_cwl_season(interaction.guild.id)
        event = db.get_cwl_event_sync(str(interaction.guild.id), season) if db is not None else None
        if event is None:
            return
        confirm_view = CwlRemindPendingConfirmView(
            parent_view=view,
            guild_id=interaction.guild.id,
            season=event["cwl_season"],
        )
        await interaction.followup.send(
            confirm_view._build_content(),  # type: ignore[attr-defined]
            view=confirm_view,
            ephemeral=True,
        )

    return callback


class CwlNotifyNewMembersConfirmView(discord.ui.View):
    """Confirm/cancel dialog for rule h's "Notify New Pool Members" (2026-08-18,
    CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md) — sends real DMs, so not a single-click action,
    mirroring CwlStartEnrollmentConfirmView's own shape exactly."""

    def __init__(self, parent_view: discord.ui.View, guild_id: int, season: str, timeout: int = 60):
        super().__init__(timeout=timeout)
        self.parent_view = parent_view
        self.guild_id = guild_id
        self.season = season

        from qapbot.i18n import t

        confirm_button: discord.ui.Button[Any] = discord.ui.Button(
            label=t('cwl.management.button_confirm_notify_new_members', guild_id=guild_id),
            style=discord.ButtonStyle.success,
            custom_id="cwl_notify_new_members_confirm",
        )
        confirm_button.callback = self._on_confirm  # type: ignore[assignment]
        self.add_item(confirm_button)

        cancel_button: discord.ui.Button[Any] = discord.ui.Button(
            label=t('cwl.setup.button_cancel', guild_id=guild_id),
            style=discord.ButtonStyle.secondary,
            custom_id="cwl_notify_new_members_cancel",
        )
        cancel_button.callback = self._on_cancel  # type: ignore[assignment]
        self.add_item(cancel_button)

    def _build_content(self) -> str:
        from qapbot.i18n import t

        return t('cwl.management.notify_new_members_confirm_body', guild_id=self.guild_id, season=self.season)

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        if not await _check_cwl_admin_permission(interaction):
            return
        from qapbot.i18n import t

        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]
        await interaction.response.edit_message(
            content=t('cwl.management.notify_new_members_processing', guild_id=self.guild_id), view=self
        )
        from qapbot.web_bridge import notify_new_cwl_pool_members

        result = await notify_new_cwl_pool_members(self.guild_id, self.season)
        if not result["ok"]:
            content = t(f"cwl.management.notify_new_members_error_{result['error']}", guild_id=self.guild_id)
        else:
            # Same blocked/failed reporting shape as Start Enrollment's own summary (item 3 of
            # the enrollment redesign) — reuses those same i18n lines, generic enough to apply
            # to any DM batch, not just the original Start Enrollment one.
            dm_issues = ""
            if result["blocked"]:
                dm_issues += t(
                    'cwl.management.start_enrollment_dm_blocked_line',
                    guild_id=self.guild_id, names=", ".join(result["blocked"]),
                )
            if result["no_mutual_guild"]:
                dm_issues += t(
                    'cwl.management.start_enrollment_dm_no_mutual_guild_line',
                    guild_id=self.guild_id, names=", ".join(result["no_mutual_guild"]),
                )
            if result["failed"]:
                from qapbot.cache_manager import DM_SEND_MAX_RETRIES

                dm_issues += t(
                    'cwl.management.start_enrollment_dm_failed_line',
                    guild_id=self.guild_id, retries=DM_SEND_MAX_RETRIES, names=", ".join(result["failed"]),
                )
            content = t(
                'cwl.management.notify_new_members_summary',
                guild_id=self.guild_id,
                contacted=result["contacted"],
                skipped_dm_guard=result["skipped_dm_guard"],
                skipped_already_dm_globally=result["skipped_already_dm_globally"],
                skipped_unlinked=result["skipped_unlinked"],
                dm_issues=dm_issues,
            )
        try:
            await interaction.edit_original_response(content=content, view=None)
        except discord.NotFound:
            pass
        if result["ok"]:
            await _refresh_parent(self.parent_view, interaction, "cwl_management")
            from qapbot.web_bridge import bump_enrollment_version
            await bump_enrollment_version(self.guild_id)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=False, ephemeral=True)
        try:
            await interaction.delete_original_response()
        except discord.NotFound:
            pass


class CwlRemindPendingConfirmView(discord.ui.View):
    """Confirm/cancel dialog for tracker #0038's "Remind Pending" action — sends real DMs, so not
    a single-click action, mirroring CwlNotifyNewMembersConfirmView's own shape exactly (including
    its double-click guard: buttons disabled + edit_message() before the long-running send)."""

    def __init__(self, parent_view: discord.ui.View, guild_id: int, season: str, timeout: int = 60):
        super().__init__(timeout=timeout)
        self.parent_view = parent_view
        self.guild_id = guild_id
        self.season = season

        from qapbot.i18n import t

        confirm_button: discord.ui.Button[Any] = discord.ui.Button(
            label=t('cwl.management.button_confirm_remind_pending', guild_id=guild_id),
            style=discord.ButtonStyle.success,
            custom_id="cwl_remind_pending_confirm",
        )
        confirm_button.callback = self._on_confirm  # type: ignore[assignment]
        self.add_item(confirm_button)

        cancel_button: discord.ui.Button[Any] = discord.ui.Button(
            label=t('cwl.setup.button_cancel', guild_id=guild_id),
            style=discord.ButtonStyle.secondary,
            custom_id="cwl_remind_pending_cancel",
        )
        cancel_button.callback = self._on_cancel  # type: ignore[assignment]
        self.add_item(cancel_button)

    def _build_content(self) -> str:
        from qapbot.i18n import t

        return t('cwl.management.remind_pending_confirm_body', guild_id=self.guild_id, season=self.season)

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        if not await _check_cwl_admin_permission(interaction):
            return
        from qapbot.i18n import t

        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]
        await interaction.response.edit_message(
            content=t('cwl.management.remind_pending_processing', guild_id=self.guild_id), view=self
        )
        from qapbot.web_bridge import remind_pending_cwl_players

        result = await remind_pending_cwl_players(self.guild_id, self.season)
        if not result["ok"]:
            content = t(f"cwl.management.remind_pending_error_{result['error']}", guild_id=self.guild_id)
        else:
            # Same blocked/failed reporting shape as Start Enrollment's own summary — reuses
            # those same i18n lines, generic enough to apply to any DM batch.
            dm_issues = ""
            if result["blocked"]:
                dm_issues += t(
                    'cwl.management.start_enrollment_dm_blocked_line',
                    guild_id=self.guild_id, names=", ".join(result["blocked"]),
                )
            if result["no_mutual_guild"]:
                dm_issues += t(
                    'cwl.management.start_enrollment_dm_no_mutual_guild_line',
                    guild_id=self.guild_id, names=", ".join(result["no_mutual_guild"]),
                )
            if result["failed"]:
                from qapbot.cache_manager import DM_SEND_MAX_RETRIES

                dm_issues += t(
                    'cwl.management.start_enrollment_dm_failed_line',
                    guild_id=self.guild_id, retries=DM_SEND_MAX_RETRIES, names=", ".join(result["failed"]),
                )
            content = t(
                'cwl.management.remind_pending_summary',
                guild_id=self.guild_id,
                contacted=result["contacted"],
                contacted_users=result["contacted_users"],
                skipped_dm_guard=result["skipped_dm_guard"],
                skipped_unlinked=result["skipped_unlinked"],
                skipped_optout=result["skipped_optout"],
                dm_issues=dm_issues,
            )
        try:
            await interaction.edit_original_response(content=content, view=None)
        except discord.NotFound:
            pass
        if result["ok"]:
            await _refresh_parent(self.parent_view, interaction, "cwl_management")
            from qapbot.web_bridge import bump_enrollment_version
            await bump_enrollment_version(self.guild_id)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=False, ephemeral=True)
        try:
            await interaction.delete_original_response()
        except discord.NotFound:
            pass


class CwlOpenEnrollmentView(discord.ui.View):
    """One-button follow-up shown on Start Enrollment's completion message (2026-08-16,
    live-testing feedback, project owner's spec: "when starting the enrollment process the 'Teams
    Management' view should be opened automatically after the enrollment start is finished").
    True zero-click auto-launch isn't achievable here — see CwlStartEnrollmentConfirmView._on_
    confirm's own comment for why — so this is the closest legal equivalent: the button is
    already sitting in the completion message, one click away, instead of making the admin
    navigate back to find "Manage Assignment" again."""

    def __init__(self, guild_id: int, timeout: int = 300):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id

        from qapbot.i18n import t

        open_button: discord.ui.Button[Any] = discord.ui.Button(
            label=t('cwl.management.button_open_enrollment_after_start', guild_id=guild_id),
            style=discord.ButtonStyle.primary,
            custom_id="cwl_open_enrollment_after_start",
        )
        open_button.callback = self._on_open  # type: ignore[assignment]
        self.add_item(open_button)

    async def _on_open(self, interaction: discord.Interaction) -> None:
        if not await _check_cwl_admin_or_leader_permission(interaction):
            return
        await _launch_cwl_activity(interaction, self.guild_id, "enrollment")


# ---------------------------------------------------------------------------
# Template-copy DM confirm/opt-out buttons (Phase 2) — DynamicItem, restart-safe
# ---------------------------------------------------------------------------

CWL_SIGNUP_RESPONSE_TEMPLATE = r'^cwl:signup:(?P<action>confirm|optout):(?P<event_id>\d+):(?P<player_tag>#[A-Z0-9]{1,15})$'


def build_cwl_signup_response_view(event_id: int, player_tag: str, guild_id: Optional[int] = None) -> discord.ui.View:
    """Build the confirm/opt-out button pair for one template-copy DM. timeout=None since these
    must keep working for as long as the sign-up window is open, independent of any single bot
    session — CwlSignupResponseButton is a DynamicItem precisely so a bot restart between send
    and click doesn't silently break it (registered once via add_dynamic_items(), QapBot.py)."""
    view = discord.ui.View(timeout=None)
    view.add_item(CwlSignupResponseButton("confirm", event_id, player_tag, guild_id))
    view.add_item(CwlSignupResponseButton("optout", event_id, player_tag, guild_id))
    return view


async def _apply_cwl_signup_response(
    event_id: int, player_tag: str, action: str, user_id_str: str,
) -> Dict[str, Any]:
    """Shared response-recording logic for CWL sign-up confirm/opt-out buttons — extracted from
    CwlSignupResponseButton.callback() (2026-08-23, tracker #0038) so CwlReminderResponseButton's
    combined-message variant applies the exact same ownership/propagation rules rather than
    duplicating them. Does everything EXCEPT the final interaction response, since the two callers
    render it differently: the original button replaces its whole single-account DM, while the
    combined-message button must leave sibling accounts' buttons in the same message untouched.

    Returns {"code": "db_unavailable"|"no_longer_valid"|"not_your_signup"|"signup_closed"|"ok",
    "guild_id": Optional[int], "player_name": Optional[str]} — every code maps 1:1 onto an
    existing `cwl.template.*` i18n key of the same name, so callers can format the ephemeral
    error with `t(f'cwl.template.{code}', ...)` directly. `guild_id`/`player_name` are filled in
    whenever known (None is a safe, already-established value for `t()`'s optional guild_id)."""
    db = CACHE.db_manager
    if db is None:
        return {"code": "db_unavailable", "guild_id": None, "player_name": None}

    # Never trust a caller's own cached state beyond routing (action/event_id/player_tag) —
    # always re-read cwl_signups/cwl_events live, since the event may have moved to
    # finalized/cancelled (or this row may no longer exist) since the DM was sent.
    event = await asyncio.to_thread(db.get_cwl_event_by_id_sync, event_id)
    signup = await asyncio.to_thread(db.get_cwl_signup_sync, event_id, player_tag)
    # The live owner from user_players, alongside the snapshot's (2026-08-22). cwl_signups is
    # written once by Start Enrollment and never refreshed, so its recorded recipient can name a
    # Discord user who no longer owns this account — see Pitfall 37.
    links = await asyncio.to_thread(db.get_player_links_sync, [player_tag])
    live_discord_id = (links.get(player_tag) or {}).get("discord_id")
    guild_id = int(event["guild_id"]) if event is not None else None

    if event is None or signup is None:
        return {"code": "no_longer_valid", "guild_id": guild_id, "player_name": None}
    # Account protection (Cardinal Rule 2): a third party who is neither owner is still rejected.
    # Both owners are accepted deliberately — the DM was genuinely delivered to the snapshot
    # owner, so their button must keep working, while the CURRENT owner must not be locked out of
    # responding for their own account. An empty set of known owners means nobody was ever
    # recorded for this tag — the click is allowed through.
    known_owner_ids = {oid for oid in (signup.get("dmed_discord_id"), live_discord_id) if oid}
    if known_owner_ids and user_id_str not in known_owner_ids:
        return {"code": "not_your_signup", "guild_id": guild_id, "player_name": signup.get("player_name")}
    if event["status"] != "signup_open":
        return {"code": "signup_closed", "guild_id": guild_id, "player_name": signup.get("player_name")}

    new_status = "confirmed" if action == "confirm" else "declined"
    source = "template_confirm" if action == "confirm" else "template_optout"
    responded_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    # to_thread()-wrapped (Pitfall 26) — this is hit by every member responding to a CWL signup
    # template, so an unwrapped write here is a high-traffic instance of the whole-bot-freeze risk.
    # Persist the LIVE owner, not the snapshot's (2026-08-22) — writing the recorded recipient
    # straight back is what made the staleness self-perpetuating. Falls back to the snapshot when
    # user_players has no row for this tag at all (a guest tag added by search that was never
    # linked), so a never-linked signup keeps whatever it had rather than being blanked.
    owner_discord_id = live_discord_id or signup.get("dmed_discord_id")
    await asyncio.to_thread(
        db.upsert_cwl_signup_sync,
        event_id, player_tag, signup.get("player_name"), owner_discord_id,
        signup.get("preferred_league_rank"), source, new_status, responded_at=responded_at,
    )
    # rule h (2026-08-18, CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md) — this DM's custom_id only
    # ever names ONE event_id, but the SAME real-world player may be pooled by other guilds too
    # for this same season; propagate_cwl_player_response() writes the global source of truth and
    # fans this exact response out to every one of those guilds' own local boards.
    from qapbot.QBdiscocmdshelper_cwl import propagate_cwl_player_response

    affected_guild_ids = await propagate_cwl_player_response(
        player_tag, event["cwl_season"], new_status, responded_at,
        signup.get("player_name"), owner_discord_id, event_id, guild_id or 0,
    )
    # Step 8 (2026-08-17) — a player confirming/opting out via a DM button never updated an open
    # Manage Enrollment board on its own; bump every affected guild's board version here.
    from qapbot.web_bridge import bump_enrollment_version

    if guild_id is not None:
        await bump_enrollment_version(guild_id)
    for other_guild_id in affected_guild_ids:
        await bump_enrollment_version(other_guild_id)

    return {
        "code": "ok", "guild_id": guild_id, "player_name": signup.get("player_name") or player_tag,
        # Phase 4d (plans/cwl-personal-hub.md) — rerender_cwl_dm_after_response() needs the
        # season for its DB query and its multi-account intro text; event is already fetched
        # here, so this avoids either caller re-fetching it just for this one field.
        "season": event["cwl_season"],
    }


async def rerender_cwl_dm_after_response(
    message: Any, event_id: int, season: str, discord_id: str,
    *, action: str, player_name: str, interaction: Optional[discord.Interaction] = None,
) -> None:
    """Re-render one enrollment/reminder DM from live state after its owner answered — from a
    button click, from the Activity (Phase 5c, a later session), or from anywhere else.

    Unifies CwlSignupResponseButton's and CwlReminderResponseButton's previously-diverged
    end-of-click rendering (plans/cwl-personal-hub.md Phase 4d "Design note" — the split was
    deliberate at the time: an invitation DM covers exactly one account and can simply be wiped,
    while a reminder DM can cover up to 5 and wiping the whole message on one click would destroy
    the still-pending siblings' buttons). This function makes both shapes go through the SAME
    "re-derive scope from the DB, then render" path — Pitfall 24's "live wins over a stored
    snapshot" convention, generalized to cover N=1 as a degenerate case of N=many rather than a
    separate code path.

    Scope (which accounts this message covers) is re-derived, never assumed: every
    cwl_player_season_status row whose dm_sent_via_message_id equals this message's id, for this
    season (get_cwl_player_season_status_rows_by_dm_message_sync — the same bookkeeping both DM
    senders already write per-account, so no new column or custom_id snapshot is needed). Of
    those, the ones still 'pending' get re-rendered with their own confirm/decline buttons; when
    none remain, the message finalizes with the existing single-account
    cwl.template.confirmed_msg/declined_msg, naming `player_name` — the exact text both button
    classes already showed before this unification, preserved byte-for-byte regardless of how
    many accounts the message originally covered (there was never a distinct "group finalized"
    wording in the pre-unification code to reuse; inventing one now would be a real wording
    change, not a refactor). `player_name` is a required caller-supplied parameter rather than
    re-derived from `covered`+`discord_id` here, because one Discord user can own MULTIPLE
    accounts covered by the same reminder DM — looking it up by discord_id alone would be
    ambiguous about which of their several accounts was actually just answered; the caller already
    knows this unambiguously (it's the account whose button was clicked, or whose status the
    caller just changed) and _apply_cwl_signup_response()'s own return already names it.

    Applies the edit through `interaction.response.edit_message()` when an interaction is
    available (keeping the click's own response slot, exactly as before) and through
    `message.edit()` otherwise — deliberately tolerant of failure there (discord.HTTPException,
    caught and logged) since a non-interaction caller (Phase 5c) has already committed the
    underlying status write by the time this runs; a DM that can't be edited must never undo it.
    """
    from qapbot.i18n import t

    db = CACHE.db_manager
    covered = (
        await asyncio.to_thread(db.get_cwl_player_season_status_rows_by_dm_message_sync, str(message.id), season)
        if db is not None else []
    )
    remaining = [
        {"player_tag": row["player_tag"], "player_name": row["player_name"]}
        for row in covered if row["status"] == "pending"
    ]

    guild_id: Optional[int] = None
    if db is not None:
        event = await asyncio.to_thread(db.get_cwl_event_by_id_sync, event_id)
        guild_id = int(event["guild_id"]) if event is not None else None

    if remaining:
        content = t('cwl.reminder.dm_buttons_intro', user_id=discord_id, guild_id=guild_id, season=season)
        view: Optional[discord.ui.View] = build_cwl_reminder_response_view(event_id, remaining, guild_id)
    else:
        response_key = 'cwl.template.confirmed_msg' if action == "confirm" else 'cwl.template.declined_msg'
        content = t(response_key, user_id=discord_id, guild_id=guild_id, player_name=player_name)
        view = None

    if interaction is not None:
        try:
            await interaction.response.edit_message(content=content, view=view)
        except discord.NotFound as e:
            if getattr(e, "code", None) == 10062:
                logging.warning(
                    f"[rerender_cwl_dm_after_response] Interaction expired before bot could respond "
                    f"(10062): event={event_id} discord_id={discord_id}"
                )
            else:
                raise
    else:
        try:
            await message.edit(content=content, view=view)
        except discord.HTTPException as e:
            logging.warning(
                f"[rerender_cwl_dm_after_response] Could not edit DM message {message.id} for "
                f"event={event_id} discord_id={discord_id}: {e}"
            )


class CwlSignupResponseButton(
    discord.ui.DynamicItem[discord.ui.Button],  # type: ignore[type-arg]
    template=CWL_SIGNUP_RESPONSE_TEMPLATE,
):
    """Restart-safe confirm/opt-out button for Phase 2's template-copy DM. custom_id embeds
    action/event_id/player_tag so from_custom_id() can fully reconstruct this item's state on
    every dispatch without a lookup table — the guild_id used only for label localization at
    initial-send time is deliberately NOT part of the custom_id (keeps it short, and it's
    resolvable from event_id via get_cwl_event_by_id_sync() if ever needed on click)."""

    def __init__(self, action: str, event_id: int, player_tag: str, guild_id: Optional[int] = None):
        self.action = action
        self.event_id = event_id
        self.player_tag = player_tag

        from qapbot.i18n import t

        label_key = 'cwl.template.confirm_button' if action == "confirm" else 'cwl.template.optout_button'
        style = discord.ButtonStyle.success if action == "confirm" else discord.ButtonStyle.secondary
        super().__init__(
            discord.ui.Button(
                label=t(label_key, guild_id=guild_id),
                style=style,
                custom_id=f"cwl:signup:{action}:{event_id}:{player_tag}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: 're.Match[str]', /
    ) -> 'CwlSignupResponseButton':
        return cls(action=match["action"], event_id=int(match["event_id"]), player_tag=match["player_tag"])

    async def callback(self, interaction: discord.Interaction) -> None:
        from qapbot.i18n import t

        user_id_str = str(interaction.user.id)
        result = await _apply_cwl_signup_response(self.event_id, self.player_tag, self.action, user_id_str)
        guild_id = result["guild_id"]

        if result["code"] != "ok":
            await interaction.response.send_message(
                t(f'cwl.template.{result["code"]}', user_id=user_id_str, guild_id=guild_id), ephemeral=True
            )
            return

        # Phase 4d (plans/cwl-personal-hub.md) — the render half now goes through the same
        # shared re-derive-from-DB path CwlReminderResponseButton uses below, rather than this
        # class's own hardcoded "always wipe the whole message" behavior. For a genuine
        # single-account invitation DM the re-derived scope is always exactly one account, so the
        # rendered result is unchanged — this is a pure refactor of HOW that result is reached.
        await rerender_cwl_dm_after_response(
            interaction.message, self.event_id, result["season"], user_id_str,
            action=self.action, player_name=result["player_name"], interaction=interaction,
        )


# ---------------------------------------------------------------------------
# "Remind Pending" combined-message confirm/decline buttons (tracker #0038) —
# DynamicItem, restart-safe, own custom_id namespace (see Design note,
# plans/tracker-0038-remind-pending-cwl.md, for why this is not a reuse of
# CwlSignupResponseButton above)
# ---------------------------------------------------------------------------

CWL_REMINDER_RESPONSE_TEMPLATE = r'^cwl:remind:(?P<action>confirm|optout):(?P<event_id>\d+):(?P<player_tag>#[A-Z0-9]{1,15})$'


def build_cwl_reminder_response_view(
    event_id: int, accounts: List[Dict[str, Any]], guild_id: Optional[int] = None
) -> discord.ui.View:
    """Tracker #0038's combined-message reminder view — one row per account (accounts must
    already be ≤5, Discord's 5-action-row cap; send_cwl_reminder_dm_group, QBdiscocmdshelper_cwl.
    py, chunks longer groups into multiple DMs before calling this), each holding a confirm+
    decline button pair labeled with the account name so several accounts in one message stay
    distinguishable. timeout=None for the same restart-survival reason as
    build_cwl_signup_response_view — CwlReminderResponseButton is a DynamicItem, registered via
    add_dynamic_items() in QapBot.py."""
    view = discord.ui.View(timeout=None)
    for row, account in enumerate(accounts):
        view.add_item(CwlReminderResponseButton(
            "confirm", event_id, account["player_tag"], account.get("player_name"), row, guild_id
        ))
        view.add_item(CwlReminderResponseButton(
            "optout", event_id, account["player_tag"], account.get("player_name"), row, guild_id
        ))
    return view


class CwlReminderResponseButton(
    discord.ui.DynamicItem[discord.ui.Button],  # type: ignore[type-arg]
    template=CWL_REMINDER_RESPONSE_TEMPLATE,
):
    """Restart-safe confirm/decline button for tracker #0038's "Remind Pending" combined-message
    DM — one Discord user's several still-pending CoC accounts, all in a single message. A
    distinct custom_id namespace (`cwl:remind:...` vs. the original `cwl:signup:...`) from
    CwlSignupResponseButton, not a reuse of it, because the two need different end-of-click
    behavior: the original replaces its whole single-account message with `view=None`; this one
    must leave sibling accounts' buttons in the same message untouched, so instead of trying to
    surgically remove just this row from the message's existing component tree, it re-derives
    this Discord user's remaining pending accounts LIVE from the DB and re-renders the message
    from that fresh query (matches the codebase's established "live wins over a stored snapshot"
    convention — Pitfall 24, COPILOT_PITFALLS_COOKBOOK.md). Both button classes share their
    response-recording logic via `_apply_cwl_signup_response()`."""

    def __init__(
        self, action: str, event_id: int, player_tag: str, player_name: Optional[str] = None,
        row: Optional[int] = None, guild_id: Optional[int] = None,
    ):
        self.action = action
        self.event_id = event_id
        self.player_tag = player_tag

        from qapbot.i18n import t

        label_key = (
            'cwl.reminder.confirm_button_labeled' if action == "confirm"
            else 'cwl.reminder.optout_button_labeled'
        )
        style = discord.ButtonStyle.success if action == "confirm" else discord.ButtonStyle.secondary
        super().__init__(
            discord.ui.Button(
                label=t(label_key, guild_id=guild_id, player_name=player_name or player_tag),
                style=style,
                custom_id=f"cwl:remind:{action}:{event_id}:{player_tag}",
                row=row,
            )
        )

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: 're.Match[str]', /
    ) -> 'CwlReminderResponseButton':
        return cls(action=match["action"], event_id=int(match["event_id"]), player_tag=match["player_tag"])

    async def callback(self, interaction: discord.Interaction) -> None:
        from qapbot.i18n import t

        user_id_str = str(interaction.user.id)
        result = await _apply_cwl_signup_response(self.event_id, self.player_tag, self.action, user_id_str)
        guild_id = result["guild_id"]

        if result["code"] != "ok":
            await interaction.response.send_message(
                t(f'cwl.template.{result["code"]}', user_id=user_id_str, guild_id=guild_id), ephemeral=True
            )
            return

        # Phase 4d (plans/cwl-personal-hub.md) — same shared re-derive-from-DB rendering
        # CwlSignupResponseButton now uses above, replacing this class's own hand-rolled
        # "re-derive this user's remaining pending accounts and rebuild" logic. Scope is now
        # message-id-scoped (get_cwl_player_season_status_rows_by_dm_message_sync) rather than
        # "every pending signup in this event owned by this discord_id" — a real behavior
        # refinement, not just a refactor: the old scoping could pull in accounts from a
        # DIFFERENT reminder DM chunk (send_cwl_reminder_dm_group splits a user's pending
        # accounts into multiple ≤5-account messages when they exceed Discord's 5-action-row
        # cap), which message-id scoping can no longer do.
        await rerender_cwl_dm_after_response(
            interaction.message, self.event_id, result["season"], user_id_str,
            action=self.action, player_name=result["player_name"], interaction=interaction,
        )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

async def _check_cwl_admin_permission(interaction: discord.Interaction) -> bool:
    """Admin-permission re-check for every CWL settings/management callback — matches
    ClanManagementView's own per-callback re-check convention (not just a check at open time),
    since these screens (especially the CWL Management Hub) can stay open for a long time.
    """
    from qapbot.config import CONFIG
    from qapbot.QBdiscocmdshelper import check_admin_permissions
    from qapbot.i18n import t

    resolved_guild_id = interaction.guild.id if interaction.guild else None
    if not await check_admin_permissions(interaction, CONFIG.server_admin, resolved_guild_id=resolved_guild_id):
        guild_id = interaction.guild.id if interaction.guild else None
        msg = t('commands.errors.admin_required', guild_id=guild_id)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        except Exception:
            pass
        return False
    return True


async def _check_cwl_admin_or_leader_permission(interaction: discord.Interaction) -> bool:
    """Permission re-check for the "Manage Assignment" launch button specifically — admin,
    configured bot admin, OR a current holder of the guild's Leader/Co-Leader Discord role
    (CWL_ROSTER_PLANNING_PLAN.md "Manage Enrollment", 2026-08-10). Every other CWL Management
    button stays on _check_cwl_admin_permission() (admin-only), unchanged."""
    from qapbot.config import CONFIG
    from qapbot.QBdiscocmdshelper import check_admin_or_leader_permission
    from qapbot.i18n import t

    resolved_guild_id = interaction.guild.id if interaction.guild else None
    guild_config = CACHE.server_config.get(str(resolved_guild_id), {}) if resolved_guild_id else {}
    if not await check_admin_or_leader_permission(
        interaction, CONFIG.server_admin, guild_config, resolved_guild_id=resolved_guild_id
    ):
        msg = t('commands.errors.admin_required', guild_id=resolved_guild_id)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        except Exception:
            pass
        return False
    return True


async def _refresh_parent(view: discord.ui.View, interaction: discord.Interaction, mode: str) -> None:
    """Duck-typed refresh: both ClanManagementView and CwlManagementHubView implement
    refresh_cwl_view(interaction, mode) — this is what lets the same "Configure Participating
    Clans" (and every other CWL settings/management control) work identically regardless of
    which shell it was opened from, per the Phase 1 shared-content-layer design.

    mode is passed explicitly by every caller rather than read from view state: unlike
    ClanManagementView (one instance per open session), CwlManagementHubView is a *single*
    shared instance serving every guild's anchored message via add_view() — storing "current
    mode" as instance state would let concurrent clicks from different guilds clobber each
    other's screen.
    """
    refresh = getattr(view, "refresh_cwl_view", None)
    if refresh is not None:
        await refresh(interaction, mode)
    else:
        logging.warning(f"[CWL] parent_view {type(view).__name__} has no refresh_cwl_view() — cannot refresh in place")


def _detect_cwl_management_hub_mode(message: discord.Message) -> str:
    """Infers which screen (cwl_settings vs cwl_management) the anchored CWL Management Hub
    message currently shows, from its own Settings/Season Management toggle buttons' styles
    (CwlManagementHubView._add_toggle_buttons: the active mode's button is primary, the inactive
    one secondary) — tracker #0070, used by refresh_cwl_management_hub_message() when the caller
    doesn't itself know the message's current mode (it holds no such state of its own — see
    CwlManagementHubView's own docstring). Defaults to "cwl_management" (the class's own
    __init__ default) if detection fails for any reason."""
    for action_row in message.components or []:
        for child in getattr(action_row, "children", []):
            if getattr(child, "custom_id", None) == "cwl_admin_hub_mode_settings":
                if getattr(child, "style", None) == discord.ButtonStyle.primary:
                    return "cwl_settings"
                return "cwl_management"
    return "cwl_management"


async def refresh_cwl_management_hub_message(guild_id: int, mode: Optional[str] = None) -> None:
    """Resolve and edit a guild's anchored CWL Management Hub message directly via the bot
    client — no discord.Interaction needed at all, so this is callable from contexts that
    don't have one (the web bridge's HTTP handlers, Phase B) as well as from
    CwlManagementHubView.refresh_cwl_view() (which just derives guild_id from its interaction
    and delegates here). Silently no-ops if the guild has no Hub message configured/tracked.

    mode: which screen to rebuild the message with. Every existing caller already knows this
    (a click on the anchored message itself, or a web Activity action that's always about
    cwl_management) and passes it explicitly. Omit it (tracker #0070) when the caller has no way
    to know — e.g. a cwl_settings mutation made from /clan management's own session-scoped
    screen, a different surface than the anchored message, which previously left the anchored
    message showing stale settings indefinitely until a manual Refresh click. In that case the
    current mode is detected from the live message's own toggle-button state instead of forcing
    a specific screen, so a background refresh can never flip the admin's anchored message to a
    screen they didn't ask for.
    """
    import QBcore

    guild = QBcore.bot.get_guild(guild_id)
    if guild is None:
        return
    guild_id_str = str(guild_id)
    config = CACHE.server_config.get(guild_id_str, {})
    channel_id = config.get("cwl_management_channel_id")
    message_id = config.get("cwl_management_message_id")
    if not channel_id or not message_id:
        return

    channel = QBcore.bot.get_channel(int(channel_id))
    if channel is None or not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return

    try:
        message = await channel.fetch_message(int(message_id))
    except discord.NotFound:
        logging.debug(f"[CWL] Hub message {message_id} not found in channel {channel_id} (guild {guild_id_str}) — will be reposted on the next repost_cwl_management_messages() cycle")
        return
    except Exception as e:
        logging.warning(f"[CWL] refresh_cwl_management_hub_message() could not fetch message: {e}")
        return

    resolved_mode = mode if mode is not None else _detect_cwl_management_hub_mode(message)

    view = CwlManagementHubView()
    view.clear_items()
    view._add_toggle_buttons(resolved_mode)
    if resolved_mode == "cwl_settings":
        add_cwl_settings_components(view, guild_id)
    else:
        add_cwl_management_components(view, guild_id)

    from qapbot.QBdiscocmdshelper_cwl import format_clan_management_cwl_settings, format_clan_management_cwl_management

    builder = format_clan_management_cwl_settings if resolved_mode == "cwl_settings" else format_clan_management_cwl_management
    embed, _, _, _ = await builder(guild)

    try:
        await message.edit(embed=embed, view=view)
    except Exception as e:
        logging.warning(f"[CWL] refresh_cwl_management_hub_message() could not update message: {e}")


async def _post_cwl_shared_clan_notice(guild_id: int, message: str, discord_id: Optional[int]) -> None:
    """One guild's half of notify_cwl_clan_shared() below — a **new** message into that guild's
    CWL Management Hub channel (if configured), distinct from refresh_cwl_management_hub_message
    ()'s in-place status re-render (nobody would notice that as "something happened"), plus an
    optional DM. Both best-effort: a missing Hub channel or an unreachable DM target never
    raises, only logs — this must never fail the calling flow (a guest-clan add / Start
    Enrollment) over a notification."""
    import QBcore

    guild_id_str = str(guild_id)
    config = CACHE.server_config.get(guild_id_str, {})
    channel_id = config.get("cwl_management_channel_id")
    if channel_id:
        channel = QBcore.bot.get_channel(int(channel_id))
        if channel is not None and isinstance(channel, (discord.TextChannel, discord.Thread)):
            try:
                await channel.send(message)
            except Exception as e:
                logging.warning(f"[CWL] notify_cwl_clan_shared could not post to guild {guild_id_str}'s Hub channel: {e}")

    if discord_id is not None:
        try:
            await CACHE.send_user_dm(str(discord_id), message)
        except Exception as e:
            logging.warning(f"[CWL] notify_cwl_clan_shared could not DM {discord_id}: {e}")


async def notify_cwl_clan_shared(
    acting_guild_id: int, clan_tag: str, season: str, sharing_result: Dict[str, Any],
    acting_discord_id: Optional[int] = None,
) -> None:
    """Cross-guild notification for a shared CWL clan (2026-08-15, project owner's spec) —
    called right after ensure_cwl_clan_sharing() (QBdiscocmdshelper_cwl.py) returns a non-None
    result, from both trigger points (guest-clan add, Start Enrollment). There's no addressable
    "guild admin list" anywhere in this bot (guild-admin = anyone holding Discord's
    Administrator permission, an open/dynamic set, not stored) — so the acting guild gets a Hub-
    channel post *and* a DM to the specific admin who triggered this (the one Discord ID we
    always actually know), while every *other* affected guild only gets the Hub-channel post
    (best-effort, only if one is configured — no reliable DM target for a guild we're not
    currently acting on behalf of).

    sharing_result is exactly what ensure_cwl_clan_sharing() returned: shared_clan_id,
    owner_guild_id, owner_resolution_method, is_new, other_guild_ids."""
    import QBcore

    owner_guild_id = sharing_result["owner_guild_id"]
    other_guild_ids: List[str] = sharing_result["other_guild_ids"]
    is_new = sharing_result["is_new"]
    # 2026-08-18, live-tested bug (project owner's report, verbatim: "the message is wrong.
    # akatsuki doesn't have a leader in our guild!! but it doesn't have the leader in the other
    # guild as well.") — this used to unconditionally claim "real in-game Leader/Co-Leader"
    # ownership even when resolve_cwl_clan_owner() (QBdiscocmdshelper_cwl.py) couldn't find one
    # in ANY affected guild and fell back to "unresolved_first_claimer" (first guild to claim it,
    # no eviction rights granted to anyone). Not a race condition — the resolver was already
    # correctly detecting this case, the notification text just never branched on it.
    owner_resolution_method = sharing_result.get("owner_resolution_method")
    is_unresolved = owner_resolution_method == "unresolved_first_claimer"
    clan_name = CACHE.get_clan_name(clan_tag, clan_tag) or clan_tag
    acting_guild_id_str = str(acting_guild_id)
    is_acting_owner = owner_guild_id == acting_guild_id_str

    def _guild_name(gid: str) -> str:
        guild = QBcore.bot.get_guild(int(gid))
        return guild.name if guild else gid

    other_names = ", ".join(_guild_name(g) for g in other_guild_ids) or "another guild"

    if is_new:
        if is_unresolved:
            ownership_line = (
                "⚠️ No resolvable real in-game Leader/Co-Leader was found for this clan in any "
                "affected guild, so ownership is unresolved for now — nobody currently has "
                "eviction rights until a linked, verified Leader/Co-Leader account is found."
            )
        else:
            ownership_line = (
                "Your guild is the recognized owner (real in-game Leader/Co-Leader) — only your "
                "guild's admins can remove another guild from this clan's roster."
                if is_acting_owner else
                f"**{_guild_name(owner_guild_id)}** is the recognized owner (real in-game "
                "Leader/Co-Leader) and can remove your guild from this clan's roster if needed."
            )
        acting_message = (
            f"🔗 **Shared CWL Clan**\n{clan_name} ({clan_tag}) is now shared for season {season} "
            f"with **{other_names}**. {ownership_line}"
        )
    else:
        acting_message = (
            f"🔗 {clan_name} ({clan_tag}) is a shared CWL clan for season {season} — your guild "
            f"has joined its existing roster, shared with **{other_names}**."
        )
    await _post_cwl_shared_clan_notice(acting_guild_id, acting_message, acting_discord_id)

    other_message = (
        f"🔗 **Shared CWL Clan**\n{clan_name} ({clan_tag}) is now also managed by "
        f"**{_guild_name(acting_guild_id_str)}** for season {season}."
    )
    for other_guild_id in other_guild_ids:
        await _post_cwl_shared_clan_notice(int(other_guild_id), other_message, None)


# ---------------------------------------------------------------------------
# CwlManagementHubView — entry point (b): dedicated anchored admin message
# ---------------------------------------------------------------------------

class CwlManagementHubView(discord.ui.View):
    """Persistent, permanently-anchored admin message toggling between the cwl_settings and
    cwl_management screens — structurally parallel to the (Phase 2) Personal CWL Hub, but
    admin-only. Registered once via QBcore.bot.add_view(CwlManagementHubView()) in
    _setup_hook() (static/generic dispatch instance, no per-invocation dynamic data — every
    component needs a stable custom_id for add_view() to route restart-spanning clicks,
    matching RegistrationView's own add_view() retrofit).

    Deliberately holds **no per-guild instance state** (no self.mode, no self.message): this
    one instance handles every guild's anchored Hub message concurrently, so "which screen is
    currently shown" is always passed explicitly by the caller (see refresh_cwl_view()) rather
    than trusted from shared state — the exact bug class RegistrationView's own generic
    dispatch instance already had to solve (per-click guild resolution instead of a
    constructor-time guild_id).

    2026-08-23 (plans/cwl-personal-hub.md Phase 2d): this view's three custom_ids were renamed
    cwl_hub_* -> cwl_admin_hub_* to retire the bare `cwl_hub_*` prefix, which collided with
    guild_config's OWN (separate, unrelated) cwl_hub_* columns — see the plan's "Naming
    convention" section. The plan called for a one-time forced-repost repair (a persisted
    per-guild marker) to fix already-posted messages still carrying the old ids, reasoning that
    the periodic maintenance repost (`only_if_not_bottom=True`, QapBot.py's `main()`) skips a
    message already at the bottom of its channel and so could leave it stale indefinitely.
    That reasoning is correct for the PERIODIC repost, but doesn't apply to the STARTUP one:
    QapBot.py's on_ready() Step 8b calls `repost_cwl_management_messages()` with no
    `only_if_not_bottom` argument at all — defaulting to `False`, i.e. an UNCONDITIONAL repost —
    once per bot process start, which is what actually happens on every real deploy. The rename
    is therefore already self-healing on the very next restart with no extra repair code; adding
    one on top would only risk a redundant double-repost in the same startup sequence for no
    benefit. Verified by reading Step 8b directly, not inferred.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self._add_toggle_buttons("cwl_management")

    def _add_toggle_buttons(self, active_mode: str) -> None:
        settings_button: discord.ui.Button[Any] = discord.ui.Button(
            label="Settings",
            style=discord.ButtonStyle.primary if active_mode == "cwl_settings" else discord.ButtonStyle.secondary,
            custom_id="cwl_admin_hub_mode_settings",
            row=0,
        )
        settings_button.callback = self._on_select_settings  # type: ignore[assignment]
        self.add_item(settings_button)

        management_button: discord.ui.Button[Any] = discord.ui.Button(
            label="Season Management",
            style=discord.ButtonStyle.primary if active_mode == "cwl_management" else discord.ButtonStyle.secondary,
            custom_id="cwl_admin_hub_mode_management",
            row=0,
        )
        management_button.callback = self._on_select_management  # type: ignore[assignment]
        self.add_item(management_button)

        # Last-resort manual fallback (2026-08-10): most CWL-data changes now auto-refresh this
        # Hub message on their own (see refresh_cwl_management_hub_message() call sites in
        # ClanManagementView.refresh_cwl_view() / the web bridge / CwlLineupRemovalConfirmView),
        # but this covers whatever edge case doesn't — or simply "I don't trust it, show me the
        # current state" — same role ClanManagementView._add_refresh_button() plays there.
        from qapbot.i18n import t

        refresh_button: discord.ui.Button[Any] = discord.ui.Button(
            label=t('ui_components.clan_management.button_refresh', guild_id=None),
            style=discord.ButtonStyle.secondary,
            custom_id="cwl_admin_hub_refresh",
            row=0,
        )
        refresh_button.callback = self._make_refresh_callback(active_mode)  # type: ignore[assignment]
        self.add_item(refresh_button)

    def _make_refresh_callback(self, mode: str):
        async def callback(interaction: discord.Interaction) -> None:
            await self._render(interaction, mode)
        return callback

    async def _render(self, interaction: discord.Interaction, mode: str) -> None:
        if not await _check_cwl_admin_permission(interaction):
            return
        if not interaction.guild:
            return
        self.clear_items()
        self._add_toggle_buttons(mode)
        if mode == "cwl_settings":
            add_cwl_settings_components(self, interaction.guild.id)
        else:
            add_cwl_management_components(self, interaction.guild.id)

        from qapbot.QBdiscocmdshelper_cwl import format_clan_management_cwl_settings, format_clan_management_cwl_management

        builder = format_clan_management_cwl_settings if mode == "cwl_settings" else format_clan_management_cwl_management
        embed, _, _, _ = await builder(interaction.guild)

        # This IS the anchored message's own interaction (a direct click on this view) — safe
        # to target via the interaction's own response.
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_select_settings(self, interaction: discord.Interaction) -> None:
        await self._render(interaction, "cwl_settings")

    async def _on_select_management(self, interaction: discord.Interaction) -> None:
        await self._render(interaction, "cwl_management")

    async def refresh_cwl_view(self, interaction: discord.Interaction, mode: str) -> None:
        """Duck-typed refresh target for CWL sub-screens (see _refresh_parent() above).

        Unlike _render() above, the *interaction* passed here often belongs to a different,
        already-responded-to interaction (e.g. the "Configure Participating Clans" button's own
        ephemeral open-web response, not a click on this anchored message) — its response
        methods would target the wrong message, or fail outright on an already-consumed
        interaction. Delegates to the
        guild-id-based core below, which has no interaction dependency at all — that's also
        what the web bridge (Phase B, no Interaction available in an HTTP handler) calls
        directly after a clan-config change made from the Activity.
        """
        if not interaction.guild:
            return
        await refresh_cwl_management_hub_message(interaction.guild.id, mode)


# ---------------------------------------------------------------------------
# Player CWL Settings Hub — anchored message + persistent view (plans/cwl-personal-hub.md
# Phase 5b, pulled forward into Phase 3: repost_cwl_player_hub_messages() (QapBot.py) has a
# hard import dependency on build_cwl_player_hub_content_and_view, so this piece must exist
# before Phase 3 can even import without crashing at every startup/maintenance cycle. Pure
# Python, no Worker/client involvement — the Activity screen this button launches
# ('player_prefs') isn't implemented until a later session; see _launch_cwl_activity's
# docstring for what that means for this button in the meantime.
# ---------------------------------------------------------------------------

async def build_cwl_player_hub_content_and_view(channel: Any, guild_id_int: int) -> Tuple[str, discord.ui.View, Optional[discord.Embed]]:
    """The (content, view, embed) callback repost_anchored_message() expects — player-facing
    counterpart to repost_cwl_management_messages()'s own inline builder (QapBot.py). Static
    per-guild content (no live per-member data baked in, unlike the admin hub's embed), so
    unlike CwlManagementHubView this needs no refresh_*_on_mutation companion — nothing a member
    does in the Activity ever changes this message."""
    from qapbot.i18n import t

    embed = discord.Embed(
        title=t('cwl.player_hub.title', guild_id=guild_id_int),
        description=t('cwl.player_hub.description', guild_id=guild_id_int),
        color=discord.Color.gold(),
    )
    view = CwlPlayerHubView()
    return "", view, embed


class CwlPlayerHubView(discord.ui.View):
    """Persistent per-guild anchored message, player-facing counterpart to
    CwlManagementHubView — plans/cwl-personal-hub.md Phase 5b. Holds no per-guild instance state
    (one instance serves every guild concurrently, the same generic-dispatch constraint
    CwlManagementHubView documents — "which guild" is always resolved per-click from the
    interaction, never from constructor-time state). Exactly one button, which launches the
    'player_prefs' Activity screen.

    No interaction_check() override: the `fully_initialized` gate is applied globally to every
    discord.ui.View by the monkey-patch in QBcore.py — CwlManagementHubView relies on exactly
    that and has no override; this view mirrors it.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)
        from qapbot.i18n import t

        button: discord.ui.Button[Any] = discord.ui.Button(
            label=t('cwl.player_hub.button_preferences', guild_id=None),
            style=discord.ButtonStyle.primary,
            custom_id="cwl_player_hub_open_prefs",
        )
        button.callback = self._on_open_preferences  # type: ignore[assignment]
        self.add_item(button)

    async def _on_open_preferences(self, interaction: discord.Interaction) -> None:
        # Deliberately NO permission gate (Phase 5a-bis's own rule, applying here too — this
        # screen is for every member, not just admins/leaders) and NO defer/send_message before
        # this — LAUNCH_ACTIVITY (type 12) must be the interaction's very first response, see
        # _launch_cwl_activity's own hard-constraint docstring.
        if not interaction.guild:
            return
        await _launch_cwl_activity(interaction, interaction.guild.id, "player_prefs")
