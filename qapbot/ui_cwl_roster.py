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
from typing import Any, Dict, List, Optional

import discord

from qapbot.cache_manager import CACHE
from qapbot.ui_common import TrackedView


def _parse_cwl_start_time(raw: str) -> Optional[str]:
    """Parse a user-entered CWL start time into a UTC ISO-8601 string ("...T HH:MMZ"), or
    None if unparseable. Accepts "YYYY-MM-DD HH:MM" (space or "T" separator) — always
    interpreted as UTC, since the CWL Management Hub is admin-only and CWL start times need
    one unambiguous reference point regardless of who's reading the roster.
    """
    candidate = raw.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(candidate, fmt).replace(tzinfo=timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%MZ")
        except ValueError:
            continue
    return None

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

    retention_months = guild_config.get("cwl_retention_months", 0)
    retention_choices = ((0, t('cwl.settings.retention_never', guild_id=guild_id)), (3, "3"), (6, "6"), (12, "12"), (24, "24"))
    retention_select: discord.ui.Select[Any] = discord.ui.Select(
        placeholder=t('cwl.settings.retention_placeholder', guild_id=guild_id),
        options=[
            discord.SelectOption(label=label, value=str(value), default=(retention_months == value))
            for value, label in retention_choices
        ],
        row=3,
        custom_id="cwl_settings_retention",
    )
    retention_select.callback = _make_cwl_settings_retention_callback(view)  # type: ignore[assignment]
    view.add_item(retention_select)  # type: ignore[arg-type]


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


def _make_cwl_settings_retention_callback(view: discord.ui.View):
    async def callback(interaction: discord.Interaction) -> None:
        if not await _check_cwl_admin_permission(interaction):
            return
        await interaction.response.defer(thinking=False, ephemeral=False)
        if not interaction.guild or not isinstance(interaction.data, dict):
            return
        values = interaction.data.get("values") or ["0"]
        guild_id_str = str(interaction.guild.id)
        config = CACHE.server_config.setdefault(guild_id_str, {})
        config["cwl_retention_months"] = int(values[0])
        await CACHE.persist_server_config(guild_id_str)
        await _refresh_parent(view, interaction, "cwl_settings")

    return callback


# ---------------------------------------------------------------------------
# cwl_management — season operations (shared content layer)
# ---------------------------------------------------------------------------

def add_cwl_management_components(view: discord.ui.View, guild_id: int) -> None:
    """Attach the cwl_management screen's buttons to *view* — either a ClanManagementView
    (entry point a) or a CwlManagementHubView (entry point b, Phase 1)."""
    from qapbot.i18n import t
    from qapbot.QBdiscocmdshelper_cwl import get_current_cwl_event_sync

    event = get_current_cwl_event_sync(guild_id)

    # Row 1, not 0: row 0 is reserved by both possible shells (ClanManagementView's refresh
    # button, CwlManagementHubView's Settings/Season Management toggle) regardless of mode.
    configure_button = discord.ui.Button(
        label=t('cwl.management.button_configure_clans', guild_id=guild_id),
        style=discord.ButtonStyle.primary,
        custom_id="cwl_management_configure_clans",
        row=1,
    )
    configure_button.callback = _make_cwl_management_configure_callback(view)  # type: ignore[assignment]
    view.add_item(configure_button)  # type: ignore[arg-type]

    # Both gated buttons below are always present but disabled until their backing handler
    # ships (Start Enrollment: Phase 2: Manage Assignments: Phase 4) — a click with no phased
    # backing would be a dead end for dev testers. Real gating conditions replace these
    # hardcoded disables once those phases land.
    start_button = discord.ui.Button(
        label=t('cwl.management.button_start_enrollment', guild_id=guild_id),
        style=discord.ButtonStyle.success,
        custom_id="cwl_management_start_enrollment",
        row=1,
        disabled=True,
    )
    view.add_item(start_button)  # type: ignore[arg-type]

    manage_button = discord.ui.Button(
        label=t('cwl.management.button_manage_assignments', guild_id=guild_id),
        style=discord.ButtonStyle.secondary,
        custom_id="cwl_management_manage_assignments",
        row=1,
        disabled=True,
    )
    view.add_item(manage_button)  # type: ignore[arg-type]

    if event is not None:
        # Surfaced so an admin opening this screen can see at a glance whether every
        # participating clan already has a start time set (Finalize, Phase 4, will require it).
        db = CACHE.db_manager
        clans = db.get_cwl_event_clans_sync(event["id"]) if db is not None else []
        missing_start = [c["clan_tag"] for c in clans if not c.get("cwl_start_at")]
        if missing_start:
            logging.debug(f"[CWL] guild {guild_id} event {event['id']}: {len(missing_start)} clan(s) missing a start time")


def _make_cwl_management_configure_callback(view: discord.ui.View):
    async def callback(interaction: discord.Interaction) -> None:
        if not await _check_cwl_admin_permission(interaction):
            return
        await interaction.response.defer(thinking=False, ephemeral=True)
        if not interaction.guild:
            return
        setup_view = CwlEventSetupView(guild=interaction.guild, parent_view=view)
        await setup_view.send_initial(interaction)

    return callback


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


# ---------------------------------------------------------------------------
# CwlEventSetupView — "Configure Participating Clans" (opened from cwl_management)
# ---------------------------------------------------------------------------

class CwlEventSetupView(TrackedView):
    """Two-phase working-copy view for setting up a CWL event, structurally mirroring
    MemberClansConfigurationView (ui_clan_management.py)'s toggle-button/working-copy/Apply
    pattern for phase 1, then reusing the same ephemeral message for phase 2:

    1. ``select_clans`` (the view's initial state): toggle buttons for which clans participate
       this season, seeded from the current event's clans or — if none yet — the previous
       season's via get_previous_cwl_event_clans_sync() (the confirmed "carry over" default).
       Capped at 20 clans (4 rows x 5 — row 4 reserved for Apply/Cancel), matching
       MemberClansConfigurationView's own practical Discord component budget.
    2. ``edit_details`` (entered on Apply): a single-clan-at-a-time roster-size/start-time
       editor with Prev/Next navigation (see _render_detail_step()) — this is what lets an
       arbitrary number of participating clans each get their own roster size and start time
       without exceeding Discord's 5-action-row budget, which a flat "one row per clan" layout
       could never do once more than a couple of clans are involved. Every edit here persists
       immediately (via _persist_detail_edit()); "Done" simply closes the screen and refreshes
       the cwl_management parent.
    """

    def __init__(self, guild: discord.Guild, parent_view: discord.ui.View, timeout: int = 300):
        super().__init__(timeout=timeout)
        self.guild = guild
        self.parent_view = parent_view
        self.working_clans: Dict[str, Dict[str, Any]] = {}
        self.phase: str = "select_clans"
        self.event_id: Optional[int] = None
        self.detail_clan_tags: List[str] = []
        self.detail_index: int = 0
        self._seed_working_clans()
        self._add_clan_buttons()
        self._add_control_buttons()

    def _seed_working_clans(self) -> None:
        from qapbot.QBdiscocmdshelper_cwl import get_current_cwl_event_sync

        db = CACHE.db_manager
        event = get_current_cwl_event_sync(self.guild.id)
        rows: List[Dict[str, Any]] = []
        if db is not None and event is not None:
            rows = db.get_cwl_event_clans_sync(event["id"])
        if not rows and db is not None:
            # No clans configured for the current/draft event yet — carry over the previous
            # season's participating clans as the default working state (confirmed design
            # decision), rather than starting from an empty selection.
            rows = db.get_previous_cwl_event_clans_sync(str(self.guild.id))
        for row in rows:
            self.working_clans[row["clan_tag"]] = {
                "target_league_rank": row.get("target_league_rank"),
                "roster_size": row.get("roster_size", 15),
                "tier_order": row.get("tier_order", 0),
                "cwl_start_at": row.get("cwl_start_at"),
            }

    def _available_clan_tags(self) -> List[str]:
        from qapbot.QBdiscocmdshelper import get_guild_clans_including_member_config

        return get_guild_clans_including_member_config(self.guild.id)

    def _add_clan_buttons(self) -> None:
        clans_to_show = sorted(
            self._available_clan_tags(),
            key=lambda tag: (CACHE.get_clan_name(tag, tag) or tag).lower(),
        )[:20]  # 4 rows x 5 — row 4 reserved for Apply/Cancel

        for idx, clan_tag in enumerate(clans_to_show):
            clan_name = CACHE.get_clan_name(clan_tag, "Unknown")
            is_selected = clan_tag in self.working_clans
            display_name = (clan_name[:30] + "...") if clan_name and len(clan_name) > 30 else (clan_name or "Unknown")

            button: discord.ui.Button[Any] = discord.ui.Button(
                label=display_name,
                emoji="✅" if is_selected else "➕",
                style=discord.ButtonStyle.success if is_selected else discord.ButtonStyle.secondary,
                custom_id=f"cwl_setup_clan_{clan_tag}",
                row=min(idx // 5, 3),
            )
            button.callback = self._make_toggle_callback(clan_tag)  # type: ignore[assignment]
            self.add_item(button)

    def _make_toggle_callback(self, clan_tag: str):
        async def callback(interaction: discord.Interaction) -> None:
            if not await _check_cwl_admin_permission(interaction):
                return
            await interaction.response.defer(thinking=False, ephemeral=True)
            if clan_tag in self.working_clans:
                del self.working_clans[clan_tag]
            else:
                self.working_clans[clan_tag] = {
                    "target_league_rank": None,
                    "roster_size": 15,
                    "tier_order": len(self.working_clans),
                    "cwl_start_at": None,
                }
            await self._refresh_self(interaction)

        return callback

    def _add_control_buttons(self) -> None:
        from qapbot.i18n import t

        guild_id = self.guild.id

        apply_button: discord.ui.Button[Any] = discord.ui.Button(
            label=t('cwl.setup.button_apply', guild_id=guild_id),
            style=discord.ButtonStyle.primary,
            custom_id="cwl_setup_apply",
            row=4,
        )
        apply_button.callback = self._on_apply  # type: ignore[assignment]
        self.add_item(apply_button)

        cancel_button: discord.ui.Button[Any] = discord.ui.Button(
            label=t('cwl.setup.button_cancel', guild_id=guild_id),
            style=discord.ButtonStyle.secondary,
            custom_id="cwl_setup_cancel",
            row=4,
        )
        cancel_button.callback = self._on_cancel  # type: ignore[assignment]
        self.add_item(cancel_button)

    def _build_content(self) -> str:
        from qapbot.i18n import t

        guild_id = self.guild.id
        if not self.working_clans:
            return t('cwl.setup.no_clans_selected', guild_id=guild_id)
        lines = [t('cwl.setup.header', guild_id=guild_id, count=len(self.working_clans))]
        for tag, cfg in self.working_clans.items():
            name = CACHE.get_clan_name(tag, tag)
            lines.append(f"• **{name}** ({tag}) — {cfg.get('roster_size', 15)} {t('cwl.management.roster_slots', guild_id=guild_id)}")
        return "\n".join(lines)

    async def send_initial(self, interaction: discord.Interaction) -> None:
        msg = await interaction.followup.send(self._build_content(), view=self, ephemeral=True)
        self.message = msg

    async def _refresh_self(self, interaction: discord.Interaction) -> None:
        self.clear_items()
        self._add_clan_buttons()
        self._add_control_buttons()
        try:
            await interaction.edit_original_response(content=self._build_content(), view=self)
        except discord.NotFound:
            pass

    def _persist_detail_edit(self) -> None:
        """Write self.working_clans to the DB as an atomic replace-all — cheap enough (a
        handful of clans) to call again on every single roster-size/start-time edit rather
        than adding a separate partial-update DB method."""
        db = CACHE.db_manager
        if db is None or self.event_id is None:
            return
        clan_configs = [
            {
                "clan_tag": tag,
                "target_league_rank": cfg.get("target_league_rank"),
                "roster_size": cfg.get("roster_size", 15),
                "tier_order": idx,
                "cwl_start_at": cfg.get("cwl_start_at"),
            }
            for idx, (tag, cfg) in enumerate(self.working_clans.items())
        ]
        db.set_cwl_event_clans_sync(self.event_id, clan_configs)

    async def _on_apply(self, interaction: discord.Interaction) -> None:
        if not await _check_cwl_admin_permission(interaction):
            return
        await interaction.response.defer(thinking=False, ephemeral=True)
        from qapbot.QBdiscocmdshelper_cwl import resolve_current_cwl_season

        db = CACHE.db_manager
        if db is None:
            return
        guild_id_str = str(self.guild.id)
        season = resolve_current_cwl_season()
        event_id = db.create_cwl_event_sync(guild_id_str, season, str(interaction.user.id))
        if event_id is None:
            return
        self.event_id = event_id
        self._persist_detail_edit()

        if not self.working_clans:
            try:
                await interaction.delete_original_response()
            except discord.NotFound:
                pass
            self.message = None
            await _refresh_parent(self.parent_view, interaction, "cwl_management")
            return

        # Move into the per-clan roster-size/start-time step, reusing this same ephemeral
        # message — picking participants and configuring their roster/start time is one flow.
        self.phase = "edit_details"
        self.detail_clan_tags = list(self.working_clans.keys())
        self.detail_index = 0
        self._render_detail_step()
        try:
            await interaction.edit_original_response(content=self._build_detail_content(), view=self)
        except discord.NotFound:
            pass

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=False, ephemeral=True)
        try:
            await interaction.delete_original_response()
        except discord.NotFound:
            pass
        self.message = None

    # -- edit_details phase: single-clan-at-a-time roster-size/start-time editor -----------

    def _render_detail_step(self) -> None:
        from qapbot.i18n import t

        self.clear_items()
        guild_id = self.guild.id
        tag = self.detail_clan_tags[self.detail_index]
        cfg = self.working_clans.get(tag, {})
        count = len(self.detail_clan_tags)

        prev_button: discord.ui.Button[Any] = discord.ui.Button(
            label="◀",
            style=discord.ButtonStyle.secondary,
            custom_id="cwl_setup_detail_prev",
            row=0,
            disabled=(count <= 1),
        )
        prev_button.callback = self._make_detail_nav_callback(-1)  # type: ignore[assignment]
        self.add_item(prev_button)

        clan_name = CACHE.get_clan_name(tag, tag)
        label_button: discord.ui.Button[Any] = discord.ui.Button(
            label=f"{clan_name} ({self.detail_index + 1}/{count})",
            style=discord.ButtonStyle.primary,
            custom_id="cwl_setup_detail_label",
            row=0,
            disabled=True,
        )
        self.add_item(label_button)

        next_button: discord.ui.Button[Any] = discord.ui.Button(
            label="▶",
            style=discord.ButtonStyle.secondary,
            custom_id="cwl_setup_detail_next",
            row=0,
            disabled=(count <= 1),
        )
        next_button.callback = self._make_detail_nav_callback(1)  # type: ignore[assignment]
        self.add_item(next_button)

        roster_size = cfg.get("roster_size", 15)
        roster_select: discord.ui.Select[Any] = discord.ui.Select(
            placeholder=t('cwl.setup.roster_size_placeholder', guild_id=guild_id),
            options=[
                discord.SelectOption(label=str(size), value=str(size), default=(roster_size == size))
                for size in (5, 15, 30)
            ],
            row=1,
            custom_id="cwl_setup_detail_roster",
        )
        roster_select.callback = self._on_detail_roster_select  # type: ignore[assignment]
        self.add_item(roster_select)

        start_time_button: discord.ui.Button[Any] = discord.ui.Button(
            label=t('cwl.setup.button_set_start_time', guild_id=guild_id),
            style=discord.ButtonStyle.secondary,
            custom_id="cwl_setup_detail_start_time",
            row=2,
        )
        start_time_button.callback = self._on_detail_start_time_click  # type: ignore[assignment]
        self.add_item(start_time_button)

        done_button: discord.ui.Button[Any] = discord.ui.Button(
            label=t('cwl.setup.button_done', guild_id=guild_id),
            style=discord.ButtonStyle.primary,
            custom_id="cwl_setup_detail_done",
            row=3,
        )
        done_button.callback = self._on_detail_done  # type: ignore[assignment]
        self.add_item(done_button)

    def _build_detail_content(self) -> str:
        from qapbot.i18n import t

        guild_id = self.guild.id
        tag = self.detail_clan_tags[self.detail_index]
        cfg = self.working_clans.get(tag, {})
        start_display = cfg.get("cwl_start_at") or t('cwl.management.start_time_unset', guild_id=guild_id)
        return t(
            'cwl.setup.detail_header',
            guild_id=guild_id,
            clan_name=CACHE.get_clan_name(tag, tag),
            clan_tag=tag,
            index=self.detail_index + 1,
            count=len(self.detail_clan_tags),
            roster_size=cfg.get("roster_size", 15),
            start_time=start_display,
        )

    def _make_detail_nav_callback(self, delta: int):
        async def callback(interaction: discord.Interaction) -> None:
            if not await _check_cwl_admin_permission(interaction):
                return
            await interaction.response.defer(thinking=False, ephemeral=True)
            self.detail_index = (self.detail_index + delta) % len(self.detail_clan_tags)
            self._render_detail_step()
            try:
                await interaction.edit_original_response(content=self._build_detail_content(), view=self)
            except discord.NotFound:
                pass

        return callback

    async def _on_detail_roster_select(self, interaction: discord.Interaction) -> None:
        if not await _check_cwl_admin_permission(interaction):
            return
        await interaction.response.defer(thinking=False, ephemeral=True)
        if not isinstance(interaction.data, dict):
            return
        values = interaction.data.get("values") or ["15"]
        tag = self.detail_clan_tags[self.detail_index]
        self.working_clans[tag]["roster_size"] = int(values[0])
        self._persist_detail_edit()
        self._render_detail_step()
        try:
            await interaction.edit_original_response(content=self._build_detail_content(), view=self)
        except discord.NotFound:
            pass

    async def _on_detail_start_time_click(self, interaction: discord.Interaction) -> None:
        if not await _check_cwl_admin_permission(interaction):
            return
        tag = self.detail_clan_tags[self.detail_index]
        await interaction.response.send_modal(CwlStartTimeModal(self, tag))

    async def _on_detail_done(self, interaction: discord.Interaction) -> None:
        if not await _check_cwl_admin_permission(interaction):
            return
        await interaction.response.defer(thinking=False, ephemeral=True)
        try:
            await interaction.delete_original_response()
        except discord.NotFound:
            pass
        self.message = None
        await _refresh_parent(self.parent_view, interaction, "cwl_management")


class CwlStartTimeModal(discord.ui.Modal):
    """Single-TextInput modal for setting (or clearing) one clan's CWL start time, opened from
    CwlEventSetupView's edit_details step. Submitting re-renders that same view/message via
    interaction.response.edit_message() — valid here because the modal was itself opened from a
    component on that message, not from a slash command.
    """

    def __init__(self, parent_view: CwlEventSetupView, clan_tag: str):
        from qapbot.i18n import t

        guild_id = parent_view.guild.id
        super().__init__(title=t('cwl.setup.start_time_modal_title', guild_id=guild_id))
        self.parent_view = parent_view
        self.clan_tag = clan_tag
        current = parent_view.working_clans.get(clan_tag, {}).get("cwl_start_at") or ""
        self.start_time_input: discord.ui.TextInput[Any] = discord.ui.TextInput(
            label=t('cwl.setup.start_time_modal_label', guild_id=guild_id),
            placeholder=t('cwl.setup.start_time_modal_placeholder', guild_id=guild_id),
            required=False,
            default=current,
            max_length=32,
        )
        self.add_item(self.start_time_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from qapbot.i18n import t

        raw = (self.start_time_input.value or "").strip()
        if not raw:
            self.parent_view.working_clans[self.clan_tag]["cwl_start_at"] = None
        else:
            parsed = _parse_cwl_start_time(raw)
            if parsed is None:
                await interaction.response.send_message(
                    t('cwl.setup.start_time_parse_error', guild_id=self.parent_view.guild.id),
                    ephemeral=True,
                )
                return
            self.parent_view.working_clans[self.clan_tag]["cwl_start_at"] = parsed

        self.parent_view._persist_detail_edit()
        self.parent_view._render_detail_step()
        await interaction.response.edit_message(
            content=self.parent_view._build_detail_content(),
            view=self.parent_view,
        )


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
        already-responded-to interaction (e.g. CwlEventSetupView's own ephemeral Apply button,
        not a click on this anchored message) — its response methods would target the wrong
        message, or fail outright on an already-consumed interaction. So this always resolves
        and edits the guild's tracked anchored message directly via the bot client instead of
        going through the interaction's response at all.
        """
        if not interaction.guild:
            return
        guild_id_str = str(interaction.guild.id)
        config = CACHE.server_config.get(guild_id_str, {})
        channel_id = config.get("cwl_management_channel_id")
        message_id = config.get("cwl_management_message_id")
        if not channel_id or not message_id:
            return

        import QBcore

        channel = QBcore.bot.get_channel(int(channel_id))
        if channel is None or not isinstance(channel, (discord.TextChannel, discord.Thread)):
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

        try:
            message = await channel.fetch_message(int(message_id))
            await message.edit(embed=embed, view=self)
        except discord.NotFound:
            logging.debug(f"[CWL] Hub message {message_id} not found in channel {channel_id} (guild {guild_id_str}) — will be reposted on the next repost_cwl_management_messages() cycle")
        except Exception as e:
            logging.warning(f"[CWL] CwlManagementHubView.refresh_cwl_view() could not update message: {e}")
