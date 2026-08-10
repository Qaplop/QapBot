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

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import discord

from qapbot.cache_manager import CACHE

# CoC's real league ladder, used for target_league_rank / preferred_league_rank pickers
# throughout this feature (Phase 1's per-clan target tier, Phase 2's sign-up preference).
CWL_LEAGUE_RANKS: List[str] = [
    "Champion League I", "Champion League II", "Champion League III",
    "Master League I", "Master League II", "Master League III",
    "Crystal League I", "Crystal League II", "Crystal League III",
    "Gold League I", "Gold League II", "Gold League III",
    "Silver League I", "Silver League II", "Silver League III",
    "Bronze League I", "Bronze League II", "Bronze League III",
    "Unranked",
]


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

    hub_enabled = bool(guild_config.get("cwl_management_message_enabled", False))
    toggle_button = discord.ui.Button(
        label=(
            t('cwl.settings.button_deactivate_hub', guild_id=guild_id)
            if hub_enabled
            else t('cwl.settings.button_activate_hub', guild_id=guild_id)
        ),
        style=discord.ButtonStyle.success if hub_enabled else discord.ButtonStyle.secondary,
        custom_id="cwl_settings_toggle_hub",
        row=1,
    )
    toggle_button.callback = _make_cwl_settings_toggle_callback(view)  # type: ignore[assignment]
    view.add_item(toggle_button)  # type: ignore[arg-type]

    retention_button: discord.ui.Button[Any] = discord.ui.Button(
        label=t('cwl.settings.button_configure_retention', guild_id=guild_id),
        style=discord.ButtonStyle.secondary,
        custom_id="cwl_settings_retention_button",
        row=3,
    )
    retention_button.callback = _make_cwl_settings_retention_button_callback(view)  # type: ignore[assignment]
    view.add_item(retention_button)  # type: ignore[arg-type]


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


def _make_cwl_settings_toggle_callback(view: discord.ui.View):
    async def callback(interaction: discord.Interaction) -> None:
        if not await _check_cwl_admin_permission(interaction):
            return
        await interaction.response.defer(thinking=False, ephemeral=False)
        if not interaction.guild:
            return
        from qapbot.i18n import t

        guild_id_str = str(interaction.guild.id)
        guild_id_int = interaction.guild.id
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

        # Post/delete the anchored Hub message immediately rather than waiting for the
        # next periodic cycle or bot restart — mirrors ClanManagementView._on_toggle_registration.
        try:
            from QapBot import repost_cwl_management_messages
            import QBcore
            QBcore.spawn_tracked("repost-cwl-management-msg", repost_cwl_management_messages(only_if_not_bottom=False))
        except Exception as e:
            logging.warning(f"Could not update CWL Management Hub message immediately: {e}")

        await _refresh_parent(view, interaction, "cwl_settings")

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
    # season and/or offers the carry-over-from-last-month prompt (Phase E.3/E.4).
    add_season_button = discord.ui.Button(
        label=t('cwl.management.button_add_season', guild_id=guild_id),
        style=discord.ButtonStyle.success,
        custom_id="cwl_management_add_season",
        row=3,
    )
    add_season_button.callback = _make_cwl_management_add_season_callback(view)  # type: ignore[assignment]
    view.add_item(add_season_button)  # type: ignore[arg-type]

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
        confirm_view = CwlDeleteSeasonConfirmView(
            parent_view=view,
            guild_id=interaction.guild.id,
            event_id=event["id"],
            season=event["cwl_season"],
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
            await interaction.response.send_message(
                t('cwl.management.add_season_db_unavailable', guild_id=guild_id_int), ephemeral=True,
            )
            return

        if db.get_cwl_event_sync(guild_id_str, target_season) is not None:
            await interaction.response.send_message(
                t('cwl.management.add_season_already_exists', guild_id=guild_id_int, season=target_season),
                ephemeral=True,
            )
            return

        # Carry-over-vs-defaults is decided exclusively here — never by "Configure Participating
        # Clans"/the web Activity (Phase E.3's explicit instruction).
        previous_rows = db.get_previous_cwl_event_clans_sync(guild_id_str)
        if previous_rows:
            await interaction.response.defer(thinking=False, ephemeral=True)
            prompt_view = CwlCarryOverPromptView(
                parent_view=view, guild_id=guild_id_int, target_season=target_season, previous_rows=previous_rows,
            )
            await interaction.followup.send(
                prompt_view._build_content(), view=prompt_view, ephemeral=True,  # type: ignore[attr-defined]
            )
            return

        # No previous data to offer — create the season outright with plain defaults.
        event_id = db.create_cwl_event_sync(guild_id_str, target_season, str(interaction.user.id))
        if event_id is None:
            await interaction.response.send_message(
                t('cwl.management.add_season_failed', guild_id=guild_id_int), ephemeral=True,
            )
            return
        config = CACHE.server_config.setdefault(guild_id_str, {})
        config["cwl_selected_season"] = target_season
        await CACHE.persist_server_config(guild_id_str)
        await interaction.response.defer(thinking=False, ephemeral=True)
        await _refresh_parent(view, interaction, "cwl_management")

    return callback


class CwlCarryOverPromptView(discord.ui.View):
    """Yes/No prompt shown by "Add New Season" when a previous season has participating clans
    to offer as a template (Phase E.4). Carry-over logic lives exclusively here — never in
    "Configure Participating Clans"/the web Activity, per the project owner's explicit
    instruction that only this button creates seasons or takes over defaults."""

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
        db = CACHE.db_manager
        if db is None:
            return
        guild_id_str = str(self.guild_id)
        event_id = db.create_cwl_event_sync(guild_id_str, self.target_season, str(discord_user_id))
        if event_id is not None and apply_carry_over:
            clan_configs = [
                {
                    "clan_tag": r["clan_tag"],
                    "target_league_rank": r.get("target_league_rank"),
                    "roster_size": r.get("roster_size", 15),
                    "tier_order": r.get("tier_order", 0),
                    "cwl_start_at": r.get("cwl_start_at"),
                    "participating": True,
                }
                for r in self.previous_rows
            ]
            db.set_cwl_event_clans_sync(event_id, clan_configs)
        config = CACHE.server_config.setdefault(guild_id_str, {})
        config["cwl_selected_season"] = self.target_season
        await CACHE.persist_server_config(guild_id_str)

    async def _finish(self, interaction: discord.Interaction, apply_carry_over: bool) -> None:
        if not await _check_cwl_admin_permission(interaction):
            return
        await interaction.response.defer(thinking=False, ephemeral=True)
        await self._create_season(interaction.user.id, apply_carry_over)
        try:
            await interaction.delete_original_response()
        except discord.NotFound:
            pass
        await _refresh_parent(self.parent_view, interaction, "cwl_management")

    async def _on_yes(self, interaction: discord.Interaction) -> None:
        await self._finish(interaction, apply_carry_over=True)

    async def _on_no(self, interaction: discord.Interaction) -> None:
        await self._finish(interaction, apply_carry_over=False)


def _make_cwl_management_open_web_callback(view: discord.ui.View):
    """Opens the CWL_CLAN_CONFIG_ACTIVITY_PLAN.md Discord Activity in-context via the
    LAUNCH_ACTIVITY interaction-response callback (type 12) — flagged in the plan as unverified
    from a plain component interaction (only confirmed working for the auto-created Entry Point
    /launch command). discord.py has no high-level wrapper for this callback type, so it's a
    raw REST call through the bot's own HTTPClient, matching the plan's documented mechanism.
    """
    async def callback(interaction: discord.Interaction) -> None:
        if not await _check_cwl_admin_permission(interaction):
            return
        guild_id = interaction.guild.id if interaction.guild else None
        if guild_id is not None:
            CACHE.pending_cwl_activity_screen[(str(guild_id), str(interaction.user.id))] = "clan_config"
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
                try:
                    await interaction.response.send_message(
                        t('cwl.management.open_web_fallback', guild_id=guild_id),
                        ephemeral=True,
                    )
                except Exception:
                    pass

    return callback


def _make_cwl_management_open_enrollment_web_callback(view: discord.ui.View):
    """Same LAUNCH_ACTIVITY mechanism as _make_cwl_management_open_web_callback above, but
    records "enrollment" as the pending screen (CACHE.pending_cwl_activity_screen) before
    launching, and is gated by the admin-or-leader permission tier rather than admin-only —
    this is the "Manage Assignment" button's callback (CWL_ROSTER_PLANNING_PLAN.md "Manage
    Enrollment" slice 5)."""
    async def callback(interaction: discord.Interaction) -> None:
        if not await _check_cwl_admin_or_leader_permission(interaction):
            return
        guild_id = interaction.guild.id if interaction.guild else None
        if guild_id is not None:
            CACHE.pending_cwl_activity_screen[(str(guild_id), str(interaction.user.id))] = "enrollment"
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
                try:
                    await interaction.response.send_message(
                        t('cwl.management.open_enrollment_fallback', guild_id=guild_id),
                        ephemeral=True,
                    )
                except Exception:
                    pass

    return callback


class CwlDeleteSeasonConfirmView(discord.ui.View):
    """Confirm/cancel dialog for deleting a CWL event outright, mirroring
    ui_registration.py's UnlinkConfirmView. Mainly for testing/starting over — deletion
    cascades to cwl_event_clans/cwl_signups/cwl_assignments (ON DELETE CASCADE)."""

    def __init__(self, parent_view: discord.ui.View, guild_id: int, event_id: int, season: str, timeout: int = 60):
        super().__init__(timeout=timeout)
        self.parent_view = parent_view
        self.guild_id = guild_id
        self.event_id = event_id
        self.season = season

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

        return t('cwl.management.delete_confirm_body', guild_id=self.guild_id, season=self.season)

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        if not await _check_cwl_admin_permission(interaction):
            return
        await interaction.response.defer(thinking=False, ephemeral=True)
        db = CACHE.db_manager
        if db is not None:
            db.delete_cwl_event_sync(self.event_id)
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
        await interaction.response.defer(thinking=False, ephemeral=True)
        from qapbot.i18n import t
        from qapbot.QBdiscocmdshelper_cwl import start_cwl_enrollment

        summary = await start_cwl_enrollment(self.guild_id, self.season)
        if not summary["ok"]:
            content = t(f"cwl.management.start_enrollment_error_{summary['error']}", guild_id=self.guild_id)
        else:
            content = t(
                'cwl.management.start_enrollment_summary',
                guild_id=self.guild_id,
                seeded=summary["seeded"],
                assigned=summary["assigned"],
                contacted=summary["contacted"],
                skipped_optout=summary["skipped_optout"],
                skipped_unlinked=summary["skipped_unlinked"],
                skipped_dev_guard=summary["skipped_dev_guard"],
            )
        try:
            await interaction.edit_original_response(content=content, view=None)
        except discord.NotFound:
            pass
        if summary["ok"]:
            await _refresh_parent(self.parent_view, interaction, "cwl_management")

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=False, ephemeral=True)
        try:
            await interaction.delete_original_response()
        except discord.NotFound:
            pass


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

        db = CACHE.db_manager
        user_id_str = str(interaction.user.id)
        if db is None:
            await interaction.response.send_message(
                t('cwl.template.db_unavailable', user_id=user_id_str), ephemeral=True
            )
            return

        # Never trust the reconstructed item's own state beyond routing (action/event_id/
        # player_tag) — always re-read cwl_signups/cwl_events live, since the event may have
        # moved to finalized/cancelled (or this row may no longer exist) since the DM was sent.
        event = db.get_cwl_event_by_id_sync(self.event_id)
        signup = db.get_cwl_signup_sync(self.event_id, self.player_tag)
        guild_id = int(event["guild_id"]) if event is not None else None

        if event is None or signup is None:
            await interaction.response.send_message(
                t('cwl.template.no_longer_valid', user_id=user_id_str, guild_id=guild_id), ephemeral=True
            )
            return
        if signup.get("discord_id") and signup["discord_id"] != user_id_str:
            await interaction.response.send_message(
                t('cwl.template.not_your_signup', user_id=user_id_str, guild_id=guild_id), ephemeral=True
            )
            return
        if event["status"] != "signup_open":
            await interaction.response.send_message(
                t('cwl.template.signup_closed', user_id=user_id_str, guild_id=guild_id), ephemeral=True
            )
            return

        new_status = "confirmed" if self.action == "confirm" else "declined"
        source = "template_confirm" if self.action == "confirm" else "template_optout"
        responded_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        db.upsert_cwl_signup_sync(
            self.event_id, self.player_tag, signup.get("player_name"), signup.get("discord_id"),
            signup.get("preferred_league_rank"), source, new_status, responded_at=responded_at,
        )

        response_key = 'cwl.template.confirmed_msg' if self.action == "confirm" else 'cwl.template.declined_msg'
        try:
            await interaction.response.edit_message(
                content=t(response_key, user_id=user_id_str, guild_id=guild_id), view=None
            )
        except discord.NotFound as e:
            if getattr(e, "code", None) == 10062:
                logging.warning(
                    f"[CwlSignupResponseButton] Interaction expired before bot could respond (10062): "
                    f"event={self.event_id} player={self.player_tag}"
                )
            else:
                raise


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


async def refresh_cwl_management_hub_message(guild_id: int, mode: str) -> None:
    """Resolve and edit a guild's anchored CWL Management Hub message directly via the bot
    client — no discord.Interaction needed at all, so this is callable from contexts that
    don't have one (the web bridge's HTTP handlers, Phase B) as well as from
    CwlManagementHubView.refresh_cwl_view() (which just derives guild_id from its interaction
    and delegates here). Silently no-ops if the guild has no Hub message configured/tracked.
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

    view = CwlManagementHubView()
    view.clear_items()
    view._add_toggle_buttons(mode)
    if mode == "cwl_settings":
        add_cwl_settings_components(view, guild_id)
    else:
        add_cwl_management_components(view, guild_id)

    from qapbot.QBdiscocmdshelper_cwl import format_clan_management_cwl_settings, format_clan_management_cwl_management

    builder = format_clan_management_cwl_settings if mode == "cwl_settings" else format_clan_management_cwl_management
    embed, _, _, _ = await builder(guild)

    try:
        message = await channel.fetch_message(int(message_id))
        await message.edit(embed=embed, view=view)
    except discord.NotFound:
        logging.debug(f"[CWL] Hub message {message_id} not found in channel {channel_id} (guild {guild_id_str}) — will be reposted on the next repost_cwl_management_messages() cycle")
    except Exception as e:
        logging.warning(f"[CWL] refresh_cwl_management_hub_message() could not update message: {e}")


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
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self._add_toggle_buttons("cwl_management")

    def _add_toggle_buttons(self, active_mode: str) -> None:
        settings_button: discord.ui.Button[Any] = discord.ui.Button(
            label="Settings",
            style=discord.ButtonStyle.primary if active_mode == "cwl_settings" else discord.ButtonStyle.secondary,
            custom_id="cwl_hub_mode_settings",
            row=0,
        )
        settings_button.callback = self._on_select_settings  # type: ignore[assignment]
        self.add_item(settings_button)

        management_button: discord.ui.Button[Any] = discord.ui.Button(
            label="Season Management",
            style=discord.ButtonStyle.primary if active_mode == "cwl_management" else discord.ButtonStyle.secondary,
            custom_id="cwl_hub_mode_management",
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
            custom_id="cwl_hub_refresh",
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
