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
        style=discord.ButtonStyle.success if include_all_accounts else discord.ButtonStyle.secondary,
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
            db.set_cwl_event_clans_sync(event_id, [
                {
                    "clan_tag": clan_tag, "roster_size": 15, "tier_order": 0,
                    "cwl_start_at": default_start_at, "participating": True,
                }
                for clan_tag in family_clan_tags
            ])
        config = CACHE.server_config.setdefault(guild_id_str, {})
        config["cwl_selected_season"] = target_season
        await CACHE.persist_server_config(guild_id_str)
        # Auto-opens "Configure Participating Clans" as the logical next step (2026-08-16,
        # live-testing feedback, project owner's spec: "after adding a new season we should add a
        # logic that the Configure Participating Clans view is opened automatically as a logical
        # consequence of adding a new season") — all the DB work above is fast/synchronous, so it
        # can finish before responding, letting this be the interaction's own first response
        # (LAUNCH_ACTIVITY has no deferred form — see _launch_cwl_activity's docstring). Replaces
        # the old defer()+_refresh_parent() call: the Hub message no longer needs a separate
        # refresh here since closing the Activity already triggers one via
        # POST /api/cwl/activity-closed.
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
        db = CACHE.db_manager
        if db is None:
            return
        guild_id_str = str(self.guild_id)
        event_id = db.create_cwl_event_sync(guild_id_str, self.target_season, str(discord_user_id))
        if event_id is not None and apply_carry_over:
            from qapbot.QBdiscocmdshelper_cwl import resolve_guild_member_clan_tags

            family_clan_tags = resolve_guild_member_clan_tags(self.guild_id)
            previous_season = db.get_previous_cwl_season_sync(guild_id_str, exclude_event_id=event_id)
            played_last_season = (
                db.get_clans_with_cwl_data_for_season_sync(family_clan_tags, previous_season)
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
            db.set_cwl_event_clans_sync(event_id, clan_configs)
        config = CACHE.server_config.setdefault(guild_id_str, {})
        config["cwl_selected_season"] = self.target_season
        await CACHE.persist_server_config(guild_id_str)

    async def _finish(self, interaction: discord.Interaction, apply_carry_over: bool) -> None:
        if not await _check_cwl_admin_permission(interaction):
            return
        await self._create_season(interaction.user.id, apply_carry_over)
        # Auto-opens "Configure Participating Clans" as the logical next step (2026-08-16,
        # live-testing feedback, project owner's spec — see _launch_cwl_activity's docstring for
        # the hard "must be this interaction's first response" constraint this relies on).
        # Trade-off: this consumes the interaction's one response slot, so — unlike the previous
        # defer()+delete_original_response() sequence — this Yes/No prompt message itself can no
        # longer be cleanly deleted afterward; it's left in place with its now-inert buttons
        # (Discord surfaces "This interaction failed" if clicked again) rather than skip the
        # auto-launch to preserve that cleanup. The Hub message no longer needs a separate
        # refresh here either — closing the Activity already triggers one via
        # POST /api/cwl/activity-closed.
        await _launch_cwl_activity(interaction, self.guild_id, "clan_config")

    async def _on_yes(self, interaction: discord.Interaction) -> None:
        await self._finish(interaction, apply_carry_over=True)

    async def _on_no(self, interaction: discord.Interaction) -> None:
        await self._finish(interaction, apply_carry_over=False)


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
            fallback_key = 'cwl.management.open_web_fallback' if screen == "clan_config" else 'cwl.management.open_enrollment_fallback'
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
            content = t(
                'cwl.management.start_enrollment_summary',
                guild_id=self.guild_id,
                seeded=summary["seeded"],
                assigned=summary["assigned"],
                contacted=summary["contacted"],
                skipped_optout=summary["skipped_optout"],
                skipped_unlinked=summary["skipped_unlinked"],
                skipped_dm_guard=summary["skipped_dm_guard"],
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
        player_name = signup.get("player_name") or self.player_tag
        try:
            await interaction.response.edit_message(
                content=t(response_key, user_id=user_id_str, guild_id=guild_id, player_name=player_name), view=None
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
    owner_guild_id, is_new, other_guild_ids."""
    import QBcore

    owner_guild_id = sharing_result["owner_guild_id"]
    other_guild_ids: List[str] = sharing_result["other_guild_ids"]
    is_new = sharing_result["is_new"]
    clan_name = CACHE.get_clan_name(clan_tag, clan_tag) or clan_tag
    acting_guild_id_str = str(acting_guild_id)
    is_acting_owner = owner_guild_id == acting_guild_id_str

    def _guild_name(gid: str) -> str:
        guild = QBcore.bot.get_guild(int(gid))
        return guild.name if guild else gid

    other_names = ", ".join(_guild_name(g) for g in other_guild_ids) or "another guild"

    if is_new:
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
