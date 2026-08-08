from __future__ import annotations
# pyright: reportUnknownParameterType=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""
Clan management UI components.

Contains the clan management hub view, configuration views (channels, language,
thresholds, roles), clan family management, account linking for admins,
and data import/export views.
"""
import asyncio
import discord
import logging
from typing import List, Dict, Any, Optional, Callable, Tuple

from qapbot.i18n import t
from qapbot.cache_manager import CACHE
from qapbot.emojis import BotEmojis


class ManualPlayerTagModal(discord.ui.Modal, title="Enter Player Tag"):
    """
    Modal for manually entering a player tag in clan management link account flow.
    
    NOTE: Title is set at class definition (discord.py requirement).
    """
    
    # TextInput MUST be class attribute for discord.py Modal system
    player_tag_input = discord.ui.TextInput(
        label="Player Tag",
        required=True,
        max_length=15,
        placeholder="#PLAYERTAG"
    )
    
    def _translate_inputs(self, guild_id: Optional[int] = None):
        """Translate TextInput labels and placeholders."""
        from qapbot.i18n import t
        self.player_tag_input.label = t('ui_components.modal_label_player_tag', guild_id=guild_id)
        self.player_tag_input.placeholder = t('ui_components.modal_placeholder_player_tag', guild_id=guild_id)
    
    def __init__(self, link_view: 'ClanManagementLinkAccountView'):  # type: ignore[name-defined]
        """
        Initialize manual player tag modal.

        Args:
            link_view: Parent ClanManagementLinkAccountView to update after player fetch
        """
        super().__init__()
        self.link_view = link_view
    
    async def on_submit(self, interaction: discord.Interaction):
        """Handle modal submission - fetch player info and update view."""
        from qapbot.QBdiscocmdshelper import normalize_clan_tag
        from qapbot.cache_manager import CACHE
        import logging
        
        # Normalize player tag
        player_tag = (self.player_tag_input.value or "").strip()
        normalized_tag = normalize_clan_tag(player_tag)
        
        if not normalized_tag:
            from qapbot.i18n import t
            user_id = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            error_msg = t('ui_components.errors.invalid_player_tag', user_id=user_id, guild_id=guild_id)
            await interaction.response.send_message(error_msg, ephemeral=True)
            return
        
        await interaction.response.defer()
        
        # Fetch player from CoC API through cache
        try:
            player = await CACHE.get_player(normalized_tag)
            
            if not player:
                from qapbot.i18n import t
                user_id = str(interaction.user.id)
                guild_id = interaction.guild.id if interaction.guild else None
                error_msg = t('ui_components.errors.player_not_found_coc_api', user_id=user_id, guild_id=guild_id, player_tag=normalized_tag)
                await interaction.followup.send(error_msg, ephemeral=True)
                return
            
            # Create player dict matching unlinked_players format
            player_dict = {
                "tag": normalized_tag,
                "name": player.name,
                "th_level": player.town_hall,
                "activity": 100  # Give manual entries high activity score to appear first
            }
            
            # Replace unlinked_players with just this player
            self.link_view.unlinked_players = [player_dict]
            
            # Auto-select this player
            self.link_view.selected_player_tag = normalized_tag
            
            # Reset pagination
            self.link_view.player_offset = 0
            
            # Rebuild view with new player
            self.link_view.clear_items()
            self.link_view._add_player_select()  # type: ignore[attr-defined]  # row 0 - will show only the manual player
            self.link_view._add_user_select()  # type: ignore[attr-defined]  # row 1  
            self.link_view._add_notification_status_select()  # row 2  # type: ignore[attr-defined]
            self.link_view._add_notification_type_mode_select()  # row 3  # type: ignore[attr-defined]
            self.link_view._add_manual_tag_and_submit_buttons()  # row 4  # type: ignore[attr-defined]
            
            # Update the link view message with rebuilt view (if we have a reference to it)
            if self.link_view.link_view_message:
                try:
                    await self.link_view.link_view_message.edit(view=self.link_view)
                except Exception as edit_error:
                    logging.warning(f"Could not edit link view message after manual player entry: {edit_error}")
            
            logging.info(f"ADMIN ACTION: {interaction.user} manually loaded player {player.name} ({normalized_tag}) for linking")
            
        except Exception as e:
            logging.info(f"[USER ERROR] Failed to fetch player {normalized_tag}: {e}")
            from qapbot.i18n import t
            user_id = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            msg = t('ui_components.errors.player_fetch_error', user_id=user_id, guild_id=guild_id,
                   player_tag=normalized_tag, error=str(e))
            await interaction.followup.send(
                msg,
                ephemeral=True
            )


class ManualUserIDModal(discord.ui.Modal, title="Enter Discord User ID"):
    """
    Modal for manually entering a Discord user ID in clan management link account flow.
    
    NOTE: Title is set at class definition (discord.py requirement).
    """
    
    # TextInput MUST be class attribute for discord.py Modal system
    user_id_input = discord.ui.TextInput(
        label="Discord User ID",
        required=True,
        max_length=20,
        placeholder="123456789012345678"
    )
    
    def __init__(self, link_view: 'ClanManagementLinkAccountView'):  # type: ignore[name-defined]
        """
        Initialize manual Discord user ID modal.

        Args:
            link_view: Parent ClanManagementLinkAccountView to update after user fetch
        """
        super().__init__()
        self.link_view = link_view
    
    async def on_submit(self, interaction: discord.Interaction):
        """Handle modal submission - fetch Discord user and update view."""
        import logging
        from qapbot.cache_manager import CACHE
        
        # Get user ID
        user_id_str = (self.user_id_input.value or "").strip()
        
        # Validate it's a number
        try:
            user_id = int(user_id_str)
        except ValueError:
            from qapbot.i18n import t
            user_id = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            error_msg = t('ui_components.errors.invalid_user_id_format', user_id=user_id, guild_id=guild_id)
            await interaction.response.send_message(
                error_msg,
                ephemeral=True
            )
            return
        
        # Defer with ephemeral to avoid flash
        await interaction.response.defer(ephemeral=True)
        
        # Fetch Discord user through cache
        try:
            user_data = await CACHE.ensure_user_metadata(str(user_id))
            display_name = user_data.get("display_name", "Unknown User")
            
            if not user_data:
                from qapbot.i18n import t
                user_id_for_t = str(interaction.user.id)
                guild_id = interaction.guild.id if interaction.guild else None
                msg = t('ui_components.errors.user_not_found', user_id=user_id_for_t, guild_id=guild_id)
                await interaction.followup.send(
                    msg,
                    ephemeral=True
                )
                return
            
            # Set the selected user ID
            self.link_view.selected_user_id = user_id
            
            # Load user's current notification settings from cache
            user_data = CACHE.user_accounts.get(str(user_id), {})
            if isinstance(user_data, dict):  # type: ignore[misc]
                notif_settings = user_data.get('notification_settings', {})
                
                # Check if user has notifications enabled
                if notif_settings.get('war_reminders', False):
                    # User has notifications enabled - pre-fill with their current settings
                    self.link_view.notification_enabled = True
                    self.link_view.notification_type = notif_settings.get('notification_type', 'all_wars')
                    self.link_view.notification_mode = notif_settings.get('notification_mode', 'repeated')
                else:
                    # User has notifications disabled - pre-fill with defaults
                    self.link_view.notification_enabled = True
                    self.link_view.notification_type = 'all_wars'
                    self.link_view.notification_mode = 'repeated'
            else:
                # User not found in cache - use defaults
                self.link_view.notification_enabled = True
                self.link_view.notification_type = 'all_wars'
                self.link_view.notification_mode = 'repeated'
            
            # Rebuild view with new user
            self.link_view._rebuild_counter += 1  # Force new custom_id for UserSelect  # type: ignore[attr-defined]
            self.link_view.clear_items()
            self.link_view._add_player_select()  # row 0  # type: ignore[attr-defined]
            self.link_view._add_user_select()  # row 1 - will show the selected user  # type: ignore[attr-defined]
            self.link_view._add_notification_status_select()  # row 2  # type: ignore[attr-defined]
            self.link_view._add_notification_type_mode_select()  # row 3  # type: ignore[attr-defined]
            self.link_view._add_manual_tag_and_submit_buttons()  # row 4  # type: ignore[attr-defined]
            
            # Update the link view message with rebuilt view (if we have a reference to it)
            if self.link_view.link_view_message:
                try:
                    # Force complete re-render by first clearing view, then setting new view
                    # This ensures Discord completely resets component state
                    await self.link_view.link_view_message.edit(view=None)
                    from qapbot.i18n import t
                    guild_id = interaction.guild.id if interaction.guild else None
                    header = t('ui_components.prompts.link_account_header', guild_id=guild_id)
                    await self.link_view.link_view_message.edit(
                        content=header,
                        view=self.link_view
                    )
                except Exception as edit_error:
                    logging.warning(f"Could not edit link view message after manual user entry: {edit_error}")
            
            logging.info(f"ADMIN ACTION: {interaction.user} manually loaded Discord user {display_name} ({user_id}) for linking")
            
        except discord.NotFound:
            from qapbot.i18n import t
            user_id_for_t = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            msg = t('ui_components.errors.discord_user_not_found', user_id=user_id_for_t, guild_id=guild_id, target_user_id=user_id)
            await interaction.followup.send(
                msg,
                ephemeral=True
            )
        except Exception as e:
            logging.info(f"[USER ERROR] Failed to fetch Discord user {user_id}: {e}")
            from qapbot.i18n import t
            user_id_for_t = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            msg = t('ui_components.errors.discord_user_fetch_error', user_id=user_id_for_t, guild_id=guild_id, target_user_id=user_id, error=str(e))
            await interaction.followup.send(
                msg,
                ephemeral=True
            )



class ClanManagementView(discord.ui.View):
    """
    Interactive view for clan management message showing clan selection and linking options.
    
    Components:
    - Clan selection dropdown to switch between clans
    - Refresh button to update the message with current data
    - Admin-only interaction: only users with administrator permissions can use UI elements
    
    This view enables clan leaders to:
    1. Select different clans to manage
    2. View linked/unlinked players
    3. Refresh the data
    
    Note:
        - Message is non-ephemeral (visible to everyone)
        - UI interactions require administrator permissions
    """
    def __init__(
        self,
        clan_tag: str,
        guild_clans: List[str],
        unlinked_players: List[Dict[str, Any]],
        sent_message: discord.Message,
        mode: str = "registrations",
        timeout: Optional[int] = None,
        all_embeds: Optional[List[discord.Embed]] = None,
        current_page: int = 0
    ):
        """
        Initialize clan management view.
        
        Args:
            clan_tag: Normalized clan tag (currently selected)
            guild_clans: List of all clan tags subscribed to this guild
            unlinked_players: List of unlinked player dicts with tag, name, th_level
            sent_message: The Discord message object that was sent
            mode: View mode - "registrations" or "notifications" (default: "registrations")
            timeout: View timeout in seconds (default: None - no timeout)
            all_embeds: List of all embeds for pagination (when multiple pages needed)
            current_page: Current page index (0-based)
        """
        super().__init__(timeout=timeout)
        logging.debug(f"ClanManagementView.__init__: clan_tag={clan_tag}, mode={mode}, guild_clans length={len(guild_clans) if guild_clans else 0}, unlinked_players length={len(unlinked_players) if unlinked_players else 0}")
        self.clan_tag = clan_tag
        self.guild_clans = guild_clans
        self.unlinked_players = unlinked_players
        self.sent_message = sent_message
        self.mode = mode
        self.all_embeds = all_embeds or []
        self.current_page = current_page
        
        logging.debug(f"About to call _add_mode_select()")
        # Add mode selection dropdown
        self._add_mode_select()  # type: ignore[attr-defined]
        
        # Add clan selection dropdown (not needed for roles, families, or config mode)
        if mode not in ["roles", "families", "config"]:
            logging.debug(f"Mode requires clan select, calling _add_clan_select()")
            self._add_clan_select()
        else:
            logging.debug(f"Mode {mode} doesn't need clan select, skipping")
        
        # Add pagination buttons if multiple embeds
        if self.all_embeds and len(self.all_embeds) > 1:  # type: ignore[arg-type]
            logging.debug(f"Adding pagination buttons for {len(self.all_embeds)} embeds")  # type: ignore[arg-type]
            self._add_pagination_buttons()
        
        logging.debug(f"Adding mode-specific components for mode={mode}")
        # Add mode-specific buttons
        if mode == "registrations":
            # Add Link Accounts button
            logging.debug(f"Adding link accounts button")
            self._add_link_accounts_button()
        elif mode == "notifications":
            # Add notification management buttons
            logging.debug(f"Adding notification management buttons")
            self._add_notification_management_buttons()
        elif mode == "roles":
            # Add role management buttons
            logging.debug(f"Adding role management buttons")
            self._add_role_management_buttons()  # type: ignore[attr-defined]
        elif mode == "families":
            # Add clan family management buttons
            logging.debug(f"Adding family management buttons")
            self._add_family_management_buttons()
        elif mode == "config":
            # Add basic configuration components
            logging.debug(f"Adding basic config components")
            self._add_basic_config_components()
        
        logging.debug(f"Adding refresh button")
        # Add refresh button
        self._add_refresh_button()  # type: ignore[attr-defined]
        logging.debug(f"ClanManagementView.__init__ completed successfully")
    
    async def on_timeout(self):
        """
        Called when the view times out (after 30 minutes). Delete the message and remove from tracking.
        """
        from qapbot.cache_manager import CACHE
        
        try:
            await self.sent_message.delete()
            logging.info(f"Deleted expired clan management message {self.sent_message.id} (timeout)")
            
            # Remove from tracking
            keys_to_delete = [k for k, v in CACHE.leaderboard_messages.items()
                            if v.get('message_ids') == str(self.sent_message.id) and v.get('mode') == 'clan_management']
            for k in keys_to_delete:
                await CACHE.delete_leaderboard_message(k)
            if keys_to_delete:
                logging.debug(f"Removed {len(keys_to_delete)} tracking entries for expired clan_management message")
                
        except discord.NotFound:
            # Message already deleted
            logging.debug("Clan management message already deleted on timeout")
        except Exception as e:
            logging.debug(f"Could not delete clan management message on timeout: {e}")

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item[Any]) -> None:
        """Suppress 10062 (Unknown interaction / expired token) as a warning; propagate everything else."""
        if isinstance(error, discord.errors.NotFound) and error.code == 10062:
            logging.warning(f"[ClanManagementView] Interaction expired before bot could respond (10062): {item}")
            return
        await super().on_error(interaction, error, item)

    async def _check_admin_permission(self, interaction: discord.Interaction, silent: bool = False) -> bool:
        """
        Check if user has administrator permissions to use this view.
        Optionally suppresses ephemeral error message if permission denied.
        
        Args:
            interaction: Discord interaction
            silent: If True, don't send error message on permission denial
        
        Returns:
            bool: True if user has permission, False otherwise
        """
        from qapbot.config import CONFIG
        from qapbot.QBdiscocmdshelper import check_admin_permissions
        
        has_permission = await check_admin_permissions(interaction, CONFIG.server_admin)
        if not has_permission and not silent:
            from qapbot.i18n import t
            user_id = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            msg = t('ui_components.errors.permission_admin_only', user_id=user_id, guild_id=guild_id)
            await interaction.response.send_message(msg, ephemeral=True)
        return has_permission
    
    def _add_mode_select(self):
        """Add dropdown for selecting management mode."""
        from qapbot.i18n import t
        
        guild_id = self.sent_message.guild.id if self.sent_message and self.sent_message.guild else None
        
        # Build mode options list
        mode_options = [
            discord.SelectOption(
                label=t('ui_components.clan_management.mode_config_label', guild_id=guild_id),
                value="config",
                description=t('ui_components.clan_management.mode_config_desc', guild_id=guild_id),
                default=(self.mode == "config")
            ),
            discord.SelectOption(
                label=t('ui_components.clan_management.mode_roles_label', guild_id=guild_id),
                value="roles",
                description=t('ui_components.clan_management.mode_roles_desc', guild_id=guild_id),
                default=(self.mode == "roles")
            ),
            discord.SelectOption(
                label=t('ui_components.clan_management.mode_families_label', guild_id=guild_id),
                value="families",
                description=t('ui_components.clan_management.mode_families_desc', guild_id=guild_id),
                default=(self.mode == "families")
            ),
            discord.SelectOption(
                label=t('ui_components.clan_management.mode_registrations_label', guild_id=guild_id),
                value="registrations",
                description=t('ui_components.clan_management.mode_registrations_desc', guild_id=guild_id),
                default=(self.mode == "registrations")
            ),
            discord.SelectOption(
                label=t('ui_components.clan_management.mode_notifications_label', guild_id=guild_id),
                value="notifications",
                description=t('ui_components.clan_management.mode_notifications_desc', guild_id=guild_id),
                default=(self.mode == "notifications")
            )
        ]
        
        mode_select = discord.ui.Select(
            placeholder=t('ui_components.clan_management.mode_placeholder', guild_id=guild_id),
            min_values=1,
            max_values=1,
            options=mode_options,  # type: ignore[arg-type]
            custom_id="clan_mgmt_mode_select",
            row=2  # Third row
        )
        mode_select.callback = self._on_mode_select  # type: ignore[assignment]
        self.add_item(mode_select)  # type: ignore[arg-type]
    
    def _add_clan_select(self):
        """Add dropdown for selecting clans."""
        from qapbot.cache_manager import CACHE
        
        # Build clan options from guild_clans, sorted alphabetically by clan name
        # (self.guild_clans itself is tag-sorted, not name-sorted)
        sorted_clan_tags = sorted(
            self.guild_clans,
            key=lambda tag: (CACHE.get_clan_name(tag, "Unknown") or "Unknown").lower()  # type: ignore[arg-type]
        )
        clan_options = []
        for clan_tag in sorted_clan_tags[:25]:  # Discord limit
            clan_name = CACHE.get_clan_name(clan_tag, "Unknown")  # type: ignore[arg-type]
            label = f"{clan_name} ({clan_tag})"
            if len(label) > 100:
                label = label[:97] + "..."
            
            clan_options.append(discord.SelectOption(
                label=label,
                value=clan_tag,
                default=(clan_tag == self.clan_tag)
            ))
        
        if not clan_options:
            return
        
        from qapbot.i18n import t
        guild_id = self.sent_message.guild.id if self.sent_message and self.sent_message.guild else None
        
        clan_select = discord.ui.Select(
            placeholder=t('ui_components.clan_management.clan_placeholder', guild_id=guild_id),
            min_values=1,
            max_values=1,
            options=clan_options,  # type: ignore[arg-type]
            custom_id="clan_mgmt_clan_select",
            row=1  # Second row
        )
        clan_select.callback = self._on_clan_select  # type: ignore[assignment]
        self.add_item(clan_select)  # type: ignore[arg-type]
    
    def _add_pagination_buttons(self):
        """Add previous/next buttons for paginated embeds."""
        from qapbot.i18n import t
        guild_id = self.sent_message.guild.id if self.sent_message and self.sent_message.guild else None
        
        # Previous button
        prev_button = discord.ui.Button(
            label=t('ui_components.clan_management.button_previous', guild_id=guild_id),
            style=discord.ButtonStyle.secondary,
            custom_id="page_prev",
            disabled=(self.current_page == 0),
            row=0
        )
        prev_button.callback = self._on_page_prev  # type: ignore[assignment]
        self.add_item(prev_button)  # type: ignore[arg-type]
        
        # Page indicator (disabled button showing current page)
        page_indicator = discord.ui.Button(
            label=f"Page {self.current_page + 1}/{len(self.all_embeds)}",  # type: ignore[arg-type]
            style=discord.ButtonStyle.secondary,
            custom_id="page_indicator",
            disabled=True,
            row=0
        )
        self.add_item(page_indicator)  # type: ignore[arg-type]
        
        # Next button
        next_button = discord.ui.Button(
            label=t('ui_components.clan_management.button_next', guild_id=guild_id),
            style=discord.ButtonStyle.secondary,
            custom_id="page_next",
            disabled=(self.current_page >= len(self.all_embeds) - 1),  # type: ignore[arg-type]
            row=0
        )
        next_button.callback = self._on_page_next  # type: ignore[assignment]
        self.add_item(next_button)  # type: ignore[arg-type]
    
    def _add_refresh_button(self):
        """Add refresh button to update clan management message."""
        from qapbot.i18n import t
        guild_id = self.sent_message.guild.id if self.sent_message and self.sent_message.guild else None
        
        refresh_button = discord.ui.Button(
            label=t('ui_components.clan_management.button_refresh', guild_id=guild_id),
            style=discord.ButtonStyle.secondary,
            custom_id="clan_mgmt_refresh",
            row=0
        )
        refresh_button.callback = self._on_refresh  # type: ignore[assignment]
        self.add_item(refresh_button)  # type: ignore[arg-type]
    
    def _add_link_accounts_button(self):
        """Add Link Accounts button to open ephemeral linking interface."""
        from qapbot.i18n import t
        guild_id = self.sent_message.guild.id if self.sent_message and self.sent_message.guild else None
        
        link_button = discord.ui.Button(
            label=t('ui_components.clan_management.button_link_accounts', guild_id=guild_id),
            style=discord.ButtonStyle.primary,
            custom_id="clan_mgmt_link_accounts",
            row=4
        )
        link_button.callback = self._on_link_accounts  # type: ignore[assignment]
        self.add_item(link_button)  # type: ignore[arg-type]
    
    def _add_notification_management_buttons(self):
        """Add notification management buttons for clan-wide and user-specific settings."""
        from qapbot.i18n import t
        guild_id = self.sent_message.guild.id if self.sent_message and self.sent_message.guild else None
        
        # Info label button (disabled)
        info_button = discord.ui.Button(
            label=t('ui_components.clan_management.notification_label', guild_id=guild_id),
            style=discord.ButtonStyle.secondary,
            custom_id="clan_mgmt_info_label",
            disabled=True,
            row=4
        )
        self.add_item(info_button)  # type: ignore[arg-type]
        
        # Button 1: Clan-wide notification settings
        clan_settings_button = discord.ui.Button(
            label=t('ui_components.clan_management.button_clan_notifications', guild_id=guild_id),
            style=discord.ButtonStyle.primary,
            custom_id="clan_mgmt_clan_notifications",
            row=4
        )
        clan_settings_button.callback = self._on_clan_notification_settings  # type: ignore[assignment]
        self.add_item(clan_settings_button)  # type: ignore[arg-type]
        
        # Button 2: User-specific notification settings
        user_settings_button = discord.ui.Button(
            label=t('ui_components.clan_management.button_user_notifications', guild_id=guild_id),
            style=discord.ButtonStyle.primary,
            custom_id="clan_mgmt_user_notifications",
            row=4
        )
        user_settings_button.callback = self._on_user_notification_settings  # type: ignore[assignment]
        self.add_item(user_settings_button)  # type: ignore[arg-type]
    
    def _add_role_management_buttons(self):
        """Add role management buttons for configuring auto-role assignment."""
        from qapbot.cache_manager import CACHE
        from qapbot.i18n import t
        
        # Get guild_id from sent_message
        guild_id = str(self.sent_message.guild.id) if self.sent_message and self.sent_message.guild else None
        
        config = CACHE.server_config.get(guild_id, {}) if guild_id else {}
        role_system_enabled = config.get("role_system_enabled", False)
        
        # Convert guild_id to int for translation
        guild_id_int = int(guild_id) if guild_id else None
        
        # Button 1: Enable/Disable role system
        if role_system_enabled:
            toggle_button = discord.ui.Button(
                label=t('ui_components.clan_management.button_toggle_role_system_disable', guild_id=guild_id_int),
                style=discord.ButtonStyle.danger,
                custom_id="clan_mgmt_toggle_roles",
                row=3
            )
        else:
            toggle_button = discord.ui.Button(
                label=t('ui_components.clan_management.button_toggle_role_system_enable', guild_id=guild_id_int),
                style=discord.ButtonStyle.success,
                custom_id="clan_mgmt_toggle_roles",
                row=3
            )
        toggle_button.callback = self._on_toggle_role_system  # type: ignore[assignment]
        self.add_item(toggle_button)  # type: ignore[arg-type]
        
        # Button 2: Configure roles (merged newbie + member)
        configure_roles_button = discord.ui.Button(
            label=t('ui_components.clan_management.button_configure_roles', guild_id=guild_id_int),
            style=discord.ButtonStyle.primary,
            custom_id="clan_mgmt_configure_roles",
            row=3
        )
        configure_roles_button.callback = self._on_configure_roles  # type: ignore[assignment]
        self.add_item(configure_roles_button)  # type: ignore[arg-type]

        coc_role_enabled = config.get("coc_role_enabled", False)
        # Button 3: Enable/Disable CoC in-game roles feature
        if coc_role_enabled:
            coc_role_btn = discord.ui.Button(
                label=t('ui_components.clan_management.button_toggle_coc_roles_disable', guild_id=guild_id_int),
                style=discord.ButtonStyle.danger,
                custom_id="clan_mgmt_toggle_coc_roles",
                row=4
            )
        else:
            coc_role_btn = discord.ui.Button(
                label=t('ui_components.clan_management.button_toggle_coc_roles_enable', guild_id=guild_id_int),
                style=discord.ButtonStyle.success,
                custom_id="clan_mgmt_toggle_coc_roles",
                row=4
            )
        coc_role_btn.callback = self._on_toggle_coc_role_feature  # type: ignore[assignment]
        self.add_item(coc_role_btn)  # type: ignore[arg-type]

        clan_role_enabled = config.get("clan_role_enabled", False)
        # Button 4: Enable/Disable per-clan roles feature
        if clan_role_enabled:
            clan_role_btn = discord.ui.Button(
                label=t('ui_components.clan_management.button_toggle_clan_roles_disable', guild_id=guild_id_int),
                style=discord.ButtonStyle.danger,
                custom_id="clan_mgmt_toggle_clan_roles",
                row=4
            )
        else:
            clan_role_btn = discord.ui.Button(
                label=t('ui_components.clan_management.button_toggle_clan_roles_enable', guild_id=guild_id_int),
                style=discord.ButtonStyle.success,
                custom_id="clan_mgmt_toggle_clan_roles",
                row=4
            )
        clan_role_btn.callback = self._on_toggle_clan_role_feature  # type: ignore[assignment]
        self.add_item(clan_role_btn)  # type: ignore[arg-type]

        # Button 5: Toggle member role mode (SIMPLE / STRICT)
        member_role_strict = config.get("member_role_strict", False)
        if member_role_strict:
            mode_btn = discord.ui.Button(
                label=t('ui_components.role_configuration.button_mode_to_simple', guild_id=guild_id_int),
                style=discord.ButtonStyle.danger,
                custom_id="clan_mgmt_toggle_member_role_mode",
                row=3
            )
        else:
            mode_btn = discord.ui.Button(
                label=t('ui_components.role_configuration.button_mode_to_strict', guild_id=guild_id_int),
                style=discord.ButtonStyle.secondary,
                custom_id="clan_mgmt_toggle_member_role_mode",
                row=3
            )
        mode_btn.callback = self._on_toggle_member_role_mode  # type: ignore[assignment]
        self.add_item(mode_btn)  # type: ignore[arg-type]

    def _add_family_management_buttons(self):
        """Add clan family management buttons."""
        from qapbot.i18n import t
        
        guild_id = self.sent_message.guild.id if self.sent_message and self.sent_message.guild else None
        
        # Button 1: Create new family
        create_family_button = discord.ui.Button(
            label=t('ui_components.clan_management.button_create_family', guild_id=guild_id),
            style=discord.ButtonStyle.success,
            custom_id="clan_mgmt_create_family",
            row=4
        )
        create_family_button.callback = self._on_create_family  # type: ignore[assignment]
        self.add_item(create_family_button)  # type: ignore[arg-type]
        
        # Button 2: Edit family
        edit_family_button = discord.ui.Button(
            label=t('ui_components.clan_management.button_edit_family', guild_id=guild_id),
            style=discord.ButtonStyle.primary,
            custom_id="clan_mgmt_edit_family",
            row=4
        )
        edit_family_button.callback = self._on_edit_family  # type: ignore[assignment]
        self.add_item(edit_family_button)  # type: ignore[arg-type]
        
        # Button 3: Delete family
        delete_family_button = discord.ui.Button(
            label=t('ui_components.clan_management.button_delete_family', guild_id=guild_id),
            style=discord.ButtonStyle.danger,
            custom_id="clan_mgmt_delete_family",
            row=4
        )
        delete_family_button.callback = self._on_delete_family  # type: ignore[assignment]
        self.add_item(delete_family_button)  # type: ignore[arg-type]
    
    def _add_basic_config_components(self):
        """Add basic configuration buttons for channels, language, and toggles."""
        from qapbot.i18n import t
        
        guild_id_int = self.sent_message.guild.id if self.sent_message and self.sent_message.guild else None
        
        # Row 3: 4 configuration buttons (primary actions)
        # Button 1: Select Language
        select_language_button = discord.ui.Button(
            label=t('ui_components.basic_config.button_select_language', guild_id=guild_id_int),
            style=discord.ButtonStyle.primary,
            custom_id="config_select_language",
            row=3
        )
        select_language_button.callback = self._on_select_language  # type: ignore[assignment]
        self.add_item(select_language_button)  # type: ignore[arg-type]
        
        # Button 2: Select Channels
        select_channels_button = discord.ui.Button(
            label=t('ui_components.basic_config.button_select_channels', guild_id=guild_id_int),
            style=discord.ButtonStyle.primary,
            custom_id="config_select_channels",
            row=3
        )
        select_channels_button.callback = self._on_select_channels  # type: ignore[assignment]
        self.add_item(select_channels_button)  # type: ignore[arg-type]
        
        # Button 3: Set Notification Time
        select_threshold_button = discord.ui.Button(
            label=t('ui_components.basic_config.button_select_notification_time', guild_id=guild_id_int),
            style=discord.ButtonStyle.primary,
            custom_id="config_select_threshold",
            row=3
        )
        select_threshold_button.callback = self._on_select_threshold  # type: ignore[assignment]
        self.add_item(select_threshold_button)  # type: ignore[arg-type]
        
        # Button 4: Member Clans
        member_clans_button = discord.ui.Button(
            label=t('ui_components.clan_management.button_member_clans', guild_id=guild_id_int),
            style=discord.ButtonStyle.primary,
            custom_id="config_member_clans",
            row=3
        )
        member_clans_button.callback = self._on_config_manage_member_clans  # type: ignore[assignment]
        self.add_item(member_clans_button)  # type: ignore[arg-type]
        
        # Row 4: 2 activation buttons (secondary actions)
        # Button 1: Activate/Deactivate Registration Message
        guild_config = {}
        if self.sent_message.guild:
            guild_config = self._get_guild_config(self.sent_message.guild.id)
        registration_enabled = guild_config.get("registration_message_enabled", False)
        registration_channel_id = guild_config.get("registration_channel_id")
        toggle_registration_button = discord.ui.Button(
            label=t('ui_components.basic_config.button_activate_registration', guild_id=guild_id_int) if not registration_enabled else t('ui_components.basic_config.button_deactivate_registration', guild_id=guild_id_int),
            style=discord.ButtonStyle.success if (not registration_enabled and registration_channel_id) else discord.ButtonStyle.secondary,
            custom_id="config_toggle_registration",
            row=4
        )
        toggle_registration_button.callback = self._on_toggle_registration  # type: ignore[assignment]
        self.add_item(toggle_registration_button)  # type: ignore[arg-type]
        
        # Button 2: Activate/Deactivate Channel War Notifications
        channel_notifications_enabled = guild_config.get("channel_war_notifications_enabled", False)
        war_notification_channel_id = guild_config.get("war_notification_channel_id")
        toggle_channel_notifications_button = discord.ui.Button(
            label=t('ui_components.basic_config.button_activate_channel_notifications', guild_id=guild_id_int) if not channel_notifications_enabled else t('ui_components.basic_config.button_deactivate_channel_notifications', guild_id=guild_id_int),
            style=discord.ButtonStyle.success if (not channel_notifications_enabled and war_notification_channel_id) else discord.ButtonStyle.secondary,
            custom_id="config_toggle_channel_notifications",
            row=4
        )
        toggle_channel_notifications_button.callback = self._on_toggle_channel_notifications  # type: ignore[assignment]
        self.add_item(toggle_channel_notifications_button)  # type: ignore[arg-type]

        # Button 3: Activate/Deactivate Welcome Message
        welcome_enabled = guild_config.get("welcome_message_enabled", False)
        toggle_welcome_button = discord.ui.Button(
            label=t('ui_components.basic_config.button_activate_welcome_message', guild_id=guild_id_int) if not welcome_enabled else t('ui_components.basic_config.button_deactivate_welcome_message', guild_id=guild_id_int),
            style=discord.ButtonStyle.success if not welcome_enabled else discord.ButtonStyle.secondary,
            custom_id="config_toggle_welcome_message",
            row=4
        )
        toggle_welcome_button.callback = self._on_toggle_welcome_message  # type: ignore[assignment]
        self.add_item(toggle_welcome_button)  # type: ignore[arg-type]

        # Button 4: Configure Welcome Message
        configure_welcome_button = discord.ui.Button(
            label=t('ui_components.basic_config.button_configure_welcome_message', guild_id=guild_id_int),
            style=discord.ButtonStyle.primary,
            custom_id="config_configure_welcome_message",
            row=4
        )
        configure_welcome_button.callback = self._on_configure_welcome_message  # type: ignore[assignment]
        self.add_item(configure_welcome_button)  # type: ignore[arg-type]
    
    def _get_guild_config(self, guild_id: int) -> Dict[str, Any]:  # type: ignore[type-arg]
        """Helper to get guild config."""
        from qapbot.cache_manager import CACHE
        return CACHE.server_config.get(str(guild_id), {})
    
    async def _on_mode_select(self, interaction: discord.Interaction) -> None:
        """Handle mode selection - switch between registration and notification management. Admin-only."""
        # Check admin permission first
        if not await self._check_admin_permission(interaction):
            return
        
        await interaction.response.defer(thinking=False, ephemeral=False)
        
        # For Select interactions, data is guaranteed to have 'values'
        selected_mode = interaction.data['values'][0]  # type: ignore[index]
        logging.debug(f"Mode select: selected_mode={selected_mode}")
        
        # Regenerate message for selected mode
        from qapbot.QBdiscocmdshelper import format_clan_management_message
        
        try:
            # For modes that don't need clan_tag (config, roles, families), use first guild clan or empty string
            logging.debug(f"self.clan_tag={self.clan_tag}, self.guild_clans={self.guild_clans}, len={len(self.guild_clans) if self.guild_clans else 0}")
            clan_dropdown_modes = ("registrations", "notifications")
            if selected_mode in clan_dropdown_modes and self.mode not in clan_dropdown_modes:
                # Entering a clan-scoped screen from one that had no clan selector
                # (config/roles/families): default to the alphabetically-first clan
                # instead of carrying over the previous mode's clan_tag (e.g. the
                # "most active clan" default), which would otherwise appear as an
                # arbitrary pre-selection in the alphabetically-sorted dropdown.
                from qapbot.cache_manager import CACHE
                sorted_clan_tags = sorted(
                    self.guild_clans,
                    key=lambda tag: (CACHE.get_clan_name(tag, "Unknown") or "Unknown").lower()  # type: ignore[arg-type]
                )
                clan_tag_to_use = sorted_clan_tags[0] if sorted_clan_tags else ""
            else:
                clan_tag_to_use = self.clan_tag if self.clan_tag else (self.guild_clans[0] if self.guild_clans else "")
            logging.debug(f"clan_tag_to_use={clan_tag_to_use}")
            
            # Ensure we have a guild object
            if not interaction.guild:
                await interaction.followup.send("Error: This command must be used in a guild.", ephemeral=True)
                return
            
            main_embed, unlinked_embed, linked_players, unlinked_players = await format_clan_management_message(
                clan_tag_to_use,  # Preserve current clan or use first available
                interaction.guild,
                mode=selected_mode  # type: ignore[arg-type]
            )
            logging.debug(f"format_clan_management_message returned: main_embed={main_embed is not None}, unlinked_embed type={type(unlinked_embed)}, is_list={isinstance(unlinked_embed, list)}")  # type: ignore[misc]
            if isinstance(unlinked_embed, list):
                logging.debug(f"unlinked_embed is list with length={len(unlinked_embed)}")
            logging.debug(f"linked_players length={len(linked_players) if linked_players else 0}, unlinked_players length={len(unlinked_players) if unlinked_players else 0}")
            logging.debug(f"linked_players length={len(linked_players) if linked_players else 0}, unlinked_players length={len(unlinked_players) if unlinked_players else 0}")
            
            # Detect mode based on return values and apply rules
            if main_embed is None and isinstance(unlinked_embed, list) and len(unlinked_embed) > 0:  # type: ignore[misc]
                # Rule 3: Pagination mode (>6000 chars) - multiple embeds returned as list
                logging.debug(f"Rule 3: Pagination mode")
                all_embeds = unlinked_embed
                display_embeds = [all_embeds[0]]  # Show first page
                current_page = 0
            elif main_embed is not None and unlinked_embed is not None and not isinstance(unlinked_embed, list):  # type: ignore[misc]
                # Rule 2: Two-embed mode (4096 < chars <= 6000)
                logging.debug(f"Rule 2: Two-embed mode")
                all_embeds = []
                display_embeds = [main_embed, unlinked_embed]
                current_page = 0
            else:
                # Rule 1: Single embed mode (<= 4096 chars)
                logging.debug(f"Rule 1: Single embed mode")
                all_embeds = []
                display_embeds = [main_embed]
                current_page = 0
            
            logging.debug(f"About to create ClanManagementView: clan_tag={clan_tag_to_use}, mode={selected_mode}, all_embeds length={len(all_embeds)}, current_page={current_page}")  # type: ignore[arg-type]
            
            # Create new view with updated mode (preserve clan)
            new_view = ClanManagementView(
                clan_tag=clan_tag_to_use,  # Preserve clan or use first available
                guild_clans=self.guild_clans,
                unlinked_players=unlinked_players,
                sent_message=self.sent_message,
                mode=selected_mode,  # type: ignore[arg-type]
                timeout=1800,
                all_embeds=all_embeds,  # type: ignore[arg-type]
                current_page=current_page
            )
            
            logging.debug(f"ClanManagementView created successfully, about to edit message with {len(display_embeds)} embeds")  # type: ignore[arg-type]
            
            await self.sent_message.edit(
                embeds=display_embeds,  # type: ignore[arg-type]
                view=new_view
            )
            
            logging.debug(f"Message edited successfully")
            
        except discord.NotFound as e:
            # Message was deleted
            logging.info(f"Clan management message was deleted (user: {interaction.user})")
            from qapbot.i18n import t
            user_id = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            msg = t('ui_components.errors.clan_management_message_deleted', user_id=user_id, guild_id=guild_id)
            await interaction.followup.send(
                msg,
                ephemeral=True
            )
        except Exception as e:
            logging.error(f"Failed to switch mode in management view: {e}")
            from qapbot.i18n import t
            user_id = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            msg = t('ui_components.errors.mode_switch_failed', user_id=user_id, guild_id=guild_id, error=str(e))
            await interaction.followup.send(
                msg,
                ephemeral=True
            )
    
    async def _on_clan_select(self, interaction: discord.Interaction) -> None:
        """Handle clan selection - update message to show selected clan. Admin-only."""
        # Check admin permission first
        if not await self._check_admin_permission(interaction):
            return
        
        await interaction.response.defer(thinking=False, ephemeral=False)
        
        # For Select interactions, data is guaranteed to have 'values'
        selected_clan_tag = interaction.data['values'][0]  # type: ignore[index]
        
        # Regenerate message for selected clan
        from qapbot.QBdiscocmdshelper import format_clan_management_message
        
        try:
            # Ensure we have a guild object
            if not interaction.guild:
                await interaction.followup.send("Error: This command must be used in a guild.", ephemeral=True)
                return
            
            main_embed, unlinked_embed, _, unlinked_players = await format_clan_management_message(
                selected_clan_tag,  # type: ignore[arg-type]
                interaction.guild,
                mode=self.mode  # Preserve current mode
            )
            
            # Detect mode based on return values and apply rules
            if main_embed is None and isinstance(unlinked_embed, list):  # type: ignore[misc]
                # Rule 3: Pagination mode (>6000 chars) - multiple embeds returned as list
                all_embeds = unlinked_embed
                display_embeds = [all_embeds[0]]  # Show first page
                current_page = 0
            elif main_embed is not None and unlinked_embed is not None and not isinstance(unlinked_embed, list):  # type: ignore[misc]
                # Rule 2: Two-embed mode (4096 < chars <= 6000)
                all_embeds = []
                display_embeds = [main_embed, unlinked_embed]
                current_page = 0
            else:
                # Rule 1: Single embed mode (<= 4096 chars)
                all_embeds = []
                display_embeds = [main_embed]
                current_page = 0
            
            # Create new view with updated clan (preserve mode)
            new_view = ClanManagementView(
                clan_tag=selected_clan_tag,  # type: ignore[arg-type]
                guild_clans=self.guild_clans,
                unlinked_players=unlinked_players,
                sent_message=self.sent_message,
                mode=self.mode,  # Preserve mode
                timeout=1800,
                all_embeds=all_embeds,  # type: ignore[arg-type]
                current_page=current_page
            )
            
            await self.sent_message.edit(
                embeds=display_embeds,  # type: ignore[arg-type]
                view=new_view
            )
            
        except discord.NotFound as e:
            # Message was deleted
            logging.info(f"Clan management message was deleted (user: {interaction.user})")
            from qapbot.i18n import t
            user_id = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            msg = t('ui_components.errors.clan_management_message_deleted', user_id=user_id, guild_id=guild_id)
            await interaction.followup.send(
                msg,
                ephemeral=True
            )
        except Exception as e:
            logging.error(f"Failed to switch clan in management view: {e}")
            from qapbot.i18n import t
            user_id = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            msg = t('ui_components.errors.clan_load_failed', user_id=user_id, guild_id=guild_id, error=str(e))
            await interaction.followup.send(
                msg,
                ephemeral=True
            )
    
    async def _on_refresh(self, interaction: discord.Interaction) -> None:
        """Handle refresh button — invalidate all guild clan caches, force-fetch fresh
        data from the CoC API for every clan in this guild (subscribed + family +
        member_clans), then regenerate and update the embed.  Admin-only.
        """
        # Check admin permission first
        if not await self._check_admin_permission(interaction):
            return
        
        await interaction.response.defer(thinking=False, ephemeral=False)
        
        # Regenerate message
        from qapbot.QBdiscocmdshelper import format_clan_management_message, get_guild_clans_including_member_config
        
        # Ensure we have guild_id
        if not interaction.guild_id:
            await interaction.followup.send("Error: Guild ID not available.", ephemeral=True)
            return
        
        # Ensure we have a guild
        if not interaction.guild:
            await interaction.followup.send("Error: This command must be used in a guild.", ephemeral=True)
            return
        
        # Re-fetch guild clans to include subscribed clans and member configurations
        updated_guild_clans = get_guild_clans_including_member_config(interaction.guild_id)

        # Invalidate all guild clans and parallel-fetch fresh data from the CoC API.
        # This ensures every view always shows up-to-date clan names, TH levels, roles,
        # and membership — regardless of which mode the admin is currently looking at.
        # return_exceptions=True prevents a single failing clan from aborting the refresh.
        for _tag in updated_guild_clans:
            CACHE.coc_clan_cache.invalidate(_tag)
        await asyncio.gather(
            *[CACHE.coc_clan_cache.get_clan(_tag) for _tag in updated_guild_clans],
            return_exceptions=True
        )
        
        try:
            main_embed, unlinked_embed, _, unlinked_players = await format_clan_management_message(
                self.clan_tag,
                interaction.guild,
                mode=self.mode  # Preserve current mode
            )
            
            # Detect mode based on return values and apply rules
            if main_embed is None and isinstance(unlinked_embed, list):  # type: ignore[misc]
                # Rule 3: Pagination mode (>6000 chars) - multiple embeds returned as list
                all_embeds = unlinked_embed
                display_embeds = [all_embeds[0]]  # Show first page
                current_page = 0
            elif main_embed is not None and unlinked_embed is not None and not isinstance(unlinked_embed, list):  # type: ignore[misc]
                # Rule 2: Two-embed mode (4096 < chars <= 6000)
                all_embeds = []
                display_embeds = [main_embed, unlinked_embed]
                current_page = 0
            else:
                # Rule 1: Single embed mode (<= 4096 chars)
                all_embeds = []
                display_embeds = [main_embed]
                current_page = 0
            
            # Create new view with updated data (preserve mode, update clan list)
            new_view = ClanManagementView(
                clan_tag=self.clan_tag,
                guild_clans=updated_guild_clans,  # Use refreshed clan list
                unlinked_players=unlinked_players,
                sent_message=self.sent_message,
                mode=self.mode,  # Preserve mode
                timeout=1800,
                all_embeds=all_embeds,  # type: ignore[arg-type]
                current_page=current_page
            )
            
            await self.sent_message.edit(
                embeds=display_embeds,  # type: ignore[arg-type]
                view=new_view
            )
            
        except discord.NotFound as e:
            # Message was deleted
            logging.info(f"Clan management message was deleted (user: {interaction.user})")
            guild_id_for_msg = self.sent_message.guild.id if self.sent_message.guild else None
            await interaction.followup.send(
                t('ui_components.errors.error_message_no_longer_exists', guild_id=guild_id_for_msg),
                ephemeral=True
            )
        except Exception as e:
            logging.error(f"Failed to refresh clan management view: {e}")
    
    async def _on_page_prev(self, interaction: discord.Interaction) -> None:
        """Handle previous page button."""
        if not await self._check_admin_permission(interaction):
            return
        
        await interaction.response.defer(thinking=False, ephemeral=False)
        
        if self.current_page > 0:
            self.current_page -= 1
            
            # Create new view with updated page
            new_view = ClanManagementView(
                clan_tag=self.clan_tag,
                guild_clans=self.guild_clans,
                unlinked_players=self.unlinked_players,
                sent_message=self.sent_message,
                mode=self.mode,
                timeout=1800,
                all_embeds=self.all_embeds,  # type: ignore[arg-type]
                current_page=self.current_page
            )
            
            # Update message with current page embed
            await self.sent_message.edit(
                embeds=[self.all_embeds[self.current_page]],  # type: ignore[arg-type]
                view=new_view
            )
    
    async def _on_page_next(self, interaction: discord.Interaction) -> None:
        """Handle next page button."""
        if not await self._check_admin_permission(interaction):
            return
        
        await interaction.response.defer(thinking=False, ephemeral=False)
        
        if self.current_page < len(self.all_embeds) - 1:  # type: ignore[arg-type]
            self.current_page += 1
            
            # Create new view with updated page
            new_view = ClanManagementView(
                clan_tag=self.clan_tag,
                guild_clans=self.guild_clans,
                unlinked_players=self.unlinked_players,
                sent_message=self.sent_message,
                mode=self.mode,
                timeout=1800,
                all_embeds=self.all_embeds,  # type: ignore[arg-type]
                current_page=self.current_page
            )
            
            # Update message with current page embed
            await self.sent_message.edit(
                embeds=[self.all_embeds[self.current_page]],  # type: ignore[arg-type]
                view=new_view
            )
    
    async def _on_link_accounts(self, interaction: discord.Interaction) -> None:
        """Handle Link Accounts button - open ephemeral linking interface. Admin-only."""
        # Check admin permission first
        if not await self._check_admin_permission(interaction):
            return
        
        await interaction.response.defer(thinking=True, ephemeral=True)
        
        # Check if there are any unlinked players
        if not self.unlinked_players:
            from qapbot.i18n import t
            user_id = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            msg = t('ui_components.errors.all_players_linked', user_id=user_id, guild_id=guild_id)
            await interaction.followup.send(msg, ephemeral=True)
            return
        
        # Create linking interface view
        link_view = ClanManagementLinkAccountView(  # type: ignore[name-defined]
            clan_tag=self.clan_tag,
            unlinked_players=self.unlinked_players,
            sent_message=self.sent_message,
            guild_clans=self.guild_clans,
            mode=self.mode,
            timeout=300
        )
        
        from qapbot.i18n import t
        user_id = str(interaction.user.id)
        guild_id = interaction.guild.id if interaction.guild else None
        header_msg = t('ui_components.prompts.link_account_header', user_id=user_id, guild_id=guild_id)
        
        link_view_msg = await interaction.followup.send(
            header_msg,
            view=link_view,  # type: ignore[arg-type]
            ephemeral=True,
            wait=True  # Wait for message to be created so we can store it
        )
        
        # Store the ephemeral message reference in the view
        link_view.link_view_message = link_view_msg

    async def _on_clan_notification_settings(self, interaction: discord.Interaction) -> None:
        """Handle Clan Settings button - open clan-wide notification settings interface. Admin-only."""
        # Check admin permission first
        if not await self._check_admin_permission(interaction):
            return
        
        await interaction.response.defer(thinking=True, ephemeral=True)
        
        # Create clan-wide notification settings view
        from qapbot.ui_notifications import NotificationSettingsView
        clan_settings_view = NotificationSettingsView(
            scope="clan",
            clan_tag=self.clan_tag,
            sent_message=self.sent_message,
            guild_clans=self.guild_clans,
            mode=self.mode,
            timeout=300
        )
        
        from qapbot.i18n import t
        user_id = str(interaction.user.id)
        guild_id = interaction.guild.id if interaction.guild else None
        header = t('ui_components.prompts.clan_notifications_header', user_id=user_id, guild_id=guild_id)
        await interaction.followup.send(header, view=clan_settings_view, ephemeral=True)  # type: ignore[arg-type]
    
    async def _on_user_notification_settings(self, interaction: discord.Interaction) -> None:
        """Handle User Settings button - open user-specific notification settings interface. Admin-only."""
        # Check admin permission first
        if not await self._check_admin_permission(interaction):
            return
        
        await interaction.response.defer(thinking=True, ephemeral=True)
        
        # Get all Discord users with at least one player in this clan
        from qapbot.cache_manager import CACHE
        
        # Build list of Discord users with players in this clan
        users_in_clan = {}
        for user_id, user_data in CACHE.user_accounts.items():
            if isinstance(user_data, dict):  # type: ignore[misc]
                user_players = user_data.get('players', [])
                for user_player in user_players:
                    if isinstance(user_player, dict):
                        player_id = user_player.get('player_tag')
                        player_name = user_player.get('player_name', 'Unknown')
                        current_clan = user_player.get('current_clan_tag')
                        # Check if player's current clan matches this clan
                        if current_clan == self.clan_tag:
                            if user_id not in users_in_clan:
                                users_in_clan[user_id] = {
                                    'display_name': user_data.get('display_name', f'User {user_id}'),
                                    'players': []
                                }
                            users_in_clan[user_id]['players'].append({
                                'player_tag': player_id,
                                'player_name': player_name
                            })
        
        if not users_in_clan:
            from qapbot.i18n import t
            user_id = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            error_msg = t('ui_components.errors.no_users_in_clan', user_id=user_id, guild_id=guild_id)
            await interaction.followup.send(error_msg, ephemeral=True)
            return
        
        # Create user-specific notification settings view
        from qapbot.ui_notifications import NotificationSettingsView
        user_settings_view = NotificationSettingsView(
            scope="user",
            clan_tag=self.clan_tag,
            sent_message=self.sent_message,
            guild_clans=self.guild_clans,
            mode=self.mode,
            users_in_clan=users_in_clan,  # type: ignore[arg-type]
            guild=interaction.guild,
            timeout=300
        )
        
        from qapbot.i18n import t
        user_id = str(interaction.user.id)
        guild_id = interaction.guild.id if interaction.guild else None
        header = t('ui_components.prompts.user_notifications_header', user_id=user_id, guild_id=guild_id)
        await interaction.followup.send(header, view=user_settings_view, ephemeral=True)  # type: ignore[arg-type]
    
    async def _on_toggle_role_system(self, interaction: discord.Interaction) -> None:
        """Handle Enable/Disable Role System button - toggle automatic role assignment. Admin-only."""
        # Check admin permission first
        if not await self._check_admin_permission(interaction):
            return
        
        await interaction.response.defer(thinking=False, ephemeral=True)
        
        from qapbot.cache_manager import CACHE
        
        # Ensure we have a guild
        if not interaction.guild:
            await interaction.followup.send("This command must be used in a guild.", ephemeral=True)
            return
        
        guild_id = str(interaction.guild.id)
        config = CACHE.server_config.get(guild_id, {})
        current_enabled = config.get("role_system_enabled", False)
        
        # If trying to enable, validate configuration first
        if not current_enabled:
            newbie_role_id = config.get("newbie_role_id")
            member_role_id = config.get("member_role_id")
            member_clans = config.get("member_clans", [])
            member_families = config.get("member_families", [])
            
            missing_items = []
            from qapbot.i18n import t
            guild_id_for_errors = interaction.guild.id if interaction.guild else None
            
            if not newbie_role_id:
                missing_items.append(t('ui_components.errors.newbie_role_not_configured', guild_id=guild_id_for_errors))
            if not member_role_id:
                missing_items.append(t('ui_components.errors.member_role_not_configured', guild_id=guild_id_for_errors))
            if not member_clans and not member_families:
                missing_items.append(t('ui_components.errors.member_clans_families_not_configured', guild_id=guild_id_for_errors))
            
            if missing_items:
                missing_text = "\n".join(missing_items)  # type: ignore[arg-type]
                validation_msg = t('ui_components.errors.role_system_config_incomplete', guild_id=guild_id_for_errors, missing_items=missing_text)
                await interaction.followup.send(validation_msg, ephemeral=True)
                return
        
        # Toggle the state
        if guild_id not in CACHE.server_config:
            CACHE.server_config[guild_id] = {}
        
        CACHE.server_config[guild_id]["role_system_enabled"] = not current_enabled
        await CACHE.persist_server_config(guild_id)
        
        # Refresh the view to show updated button state
        from qapbot.QBdiscocmdshelper import format_clan_management_message
        
        try:
            main_embed, _, _, _ = await format_clan_management_message(
                self.clan_tag,
                interaction.guild,
                mode="roles"
            )
            
            # Rebuild view with updated button state
            self.clear_items()
            self._add_mode_select()  # type: ignore[attr-defined]
            self._add_role_management_buttons()  # type: ignore[attr-defined]
            self._add_refresh_button()  # type: ignore[attr-defined]
            
            await self.sent_message.edit(embeds=[main_embed] if main_embed else [], view=self)
            
        except Exception as e:
            from qapbot.i18n import t
            user_id = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            error_msg = t('ui_components.errors.error_toggling_role_system', user_id=user_id, guild_id=guild_id, error=str(e))
            await interaction.followup.send(error_msg, ephemeral=True)
            logging.error(f"Error toggling role system: {e}", exc_info=True)
    
    async def _on_configure_roles(self, interaction: discord.Interaction) -> None:
        """Handle Configure Roles button - set newbie and member roles. Admin-only."""
        # Check admin permission first
        if not await self._check_admin_permission(interaction):
            return
        
        from qapbot.cache_manager import CACHE
        
        # Ensure we have a guild
        if not interaction.guild:
            await interaction.response.send_message("This command must be used in a guild.", ephemeral=True)
            return
        
        guild_id = str(interaction.guild.id)
        config = CACHE.server_config.get(guild_id, {})
        
        # Get current role settings
        newbie_role_id = config.get("newbie_role_id")
        member_role_id = config.get("member_role_id")
        
        # Get role objects for display
        newbie_role = None
        member_role = None
        
        if newbie_role_id:
            try:
                if interaction.guild:
                    newbie_role = interaction.guild.get_role(int(newbie_role_id))
            except Exception:
                pass
        
        if member_role_id:
            try:
                if interaction.guild:
                    member_role = interaction.guild.get_role(int(member_role_id))
            except Exception:
                pass
        
        # Create role configuration view
        role_config_view = RoleConfigurationView(
            guild=interaction.guild,
            clan_management_view=self,
            original_interaction=interaction,
            current_newbie_role=newbie_role,
            current_member_role=member_role,
            timeout=300
        )
        
        # Build current settings display
        newbie_display = newbie_role.mention if newbie_role else "❌ Not set"
        member_display = member_role.mention if member_role else "❌ Not set"
        
        from qapbot.i18n import t
        user_id = str(interaction.user.id)
        guild_id = interaction.guild.id if interaction.guild else None
        header_msg = t('ui_components.prompts.configure_roles_header', 
                       user_id=user_id, guild_id=guild_id,
                       newbie_display=newbie_display, member_display=member_display)
        
        await interaction.response.send_message(
            header_msg,
            view=role_config_view,
            ephemeral=True
        )

    async def _refresh_roles_view(self, interaction: discord.Interaction) -> None:
        """Rebuild and re-render the roles management view after a config change."""
        from qapbot.QBdiscocmdshelper import format_clan_management_message
        try:
            main_embed, _, _, _ = await format_clan_management_message(
                self.clan_tag, interaction.guild, mode="roles"  # type: ignore[arg-type]
            )
            self.clear_items()
            self._add_mode_select()  # type: ignore[attr-defined]
            self._add_role_management_buttons()  # type: ignore[attr-defined]
            self._add_refresh_button()  # type: ignore[attr-defined]
            await self.sent_message.edit(embeds=[main_embed] if main_embed else [], view=self)
        except Exception as err:
            logging.warning(f"[ROLES-UI] _refresh_roles_view failed: {err}")

    async def _on_toggle_coc_role_feature(self, interaction: discord.Interaction) -> None:
        """Handle Enable/Disable CoC In-Game Roles button. Admin-only."""
        if not await self._check_admin_permission(interaction):
            return
        await interaction.response.defer(thinking=False, ephemeral=True)
        if not interaction.guild:
            from qapbot.i18n import t
            await interaction.followup.send(
                t('ui_components.role_configuration_buttons.error_guild_required',
                  guild_id=None), ephemeral=True)
            return

        from qapbot.cache_manager import CACHE
        from qapbot.i18n import t
        from qapbot import guild_role_manager

        guild_id = str(interaction.guild.id)
        guild_id_int = interaction.guild.id
        config = CACHE.server_config.get(guild_id, {})
        currently_enabled = config.get("coc_role_enabled", False)

        if not currently_enabled:
            # ENABLE: create the four CoC roles in this guild
            await guild_role_manager.create_coc_ingame_roles(interaction.guild, guild_id)
            if guild_id not in CACHE.server_config:
                CACHE.server_config[guild_id] = {}
            CACHE.server_config[guild_id]["coc_role_enabled"] = True
            await CACHE.persist_server_config(guild_id)
            # coc_role is not stored in DB – it's populated only when the CoC API is fetched.
            # Refresh all member clans now so _get_highest_coc_role_for_user() returns
            # correct values during the immediate sync below.
            _cfg = CACHE.server_config.get(guild_id, {})
            _clans_to_refresh: set[str] = set(_cfg.get("member_clans", []))
            for _fid in _cfg.get("member_families", []):
                _fdata = CACHE.clan_families.get(_fid, {})
                _clans_to_refresh.update(_fdata.get("clans", []))
            for _ctag in _clans_to_refresh:
                try:
                    await CACHE.coc_clan_cache.get_clan(_ctag)
                except Exception as _e:
                    logging.warning(f"[ROLES-UI] Could not refresh CoC data for {_ctag}: {_e}")
            # Immediately assign roles to all current guild members
            await guild_role_manager.sync_all_roles_for_guild(interaction.guild, guild_id)
            await self._refresh_roles_view(interaction)
        else:
            # DISABLE: find roles to delete and show confirmation if any exist
            roles_to_delete = guild_role_manager.get_coc_roles_to_delete(interaction.guild, guild_id)
            if not roles_to_delete:
                # Nothing to delete – just flip the flag
                CACHE.server_config[guild_id]["coc_role_enabled"] = False
                await CACHE.persist_server_config(guild_id)
                await self._refresh_roles_view(interaction)
            else:
                # Show confirmation view listing roles that will be deleted
                role_lines = "\n".join(
                    f"• {role.mention}" for _, _, role in roles_to_delete if role
                )
                body = (
                    f"**{t('ui_components.role_configuration_buttons.confirm_delete_coc_roles_title', guild_id=guild_id_int)}**\n"
                    f"{t('ui_components.role_configuration_buttons.confirm_delete_coc_roles_body', guild_id=guild_id_int)}\n"
                    f"{role_lines}"
                )
                confirm_view = RoleDeleteConfirmationView(
                    feature="coc",
                    clan_management_view=self,
                    original_interaction=interaction,
                    timeout=120
                )
                confirm_view.confirmation_message = await interaction.followup.send(body, view=confirm_view, ephemeral=True)

    async def _on_toggle_clan_role_feature(self, interaction: discord.Interaction) -> None:
        """Handle Enable/Disable Per-Clan Roles button. Admin-only."""
        if not await self._check_admin_permission(interaction):
            return
        await interaction.response.defer(thinking=False, ephemeral=True)
        if not interaction.guild:
            from qapbot.i18n import t
            await interaction.followup.send(
                t('ui_components.role_configuration_buttons.error_guild_required',
                  guild_id=None), ephemeral=True)
            return

        from qapbot.cache_manager import CACHE
        from qapbot.i18n import t
        from qapbot import guild_role_manager

        guild_id = str(interaction.guild.id)
        guild_id_int = interaction.guild.id
        config = CACHE.server_config.get(guild_id, {})
        currently_enabled = config.get("clan_role_enabled", False)

        if not currently_enabled:
            # ENABLE: create per-clan roles for all member clans (+families)
            await guild_role_manager.create_all_clan_roles(interaction.guild, guild_id)
            if guild_id not in CACHE.server_config:
                CACHE.server_config[guild_id] = {}
            CACHE.server_config[guild_id]["clan_role_enabled"] = True
            await CACHE.persist_server_config(guild_id)
            # Immediately assign roles to all current guild members
            await guild_role_manager.sync_all_roles_for_guild(interaction.guild, guild_id)
            await self._refresh_roles_view(interaction)
        else:
            # DISABLE: find clan roles to delete and show confirmation if any exist
            roles_to_delete = guild_role_manager.get_clan_roles_to_delete(interaction.guild, guild_id)
            if not roles_to_delete:
                CACHE.server_config[guild_id]["clan_role_enabled"] = False
                await CACHE.persist_server_config(guild_id)
                await self._refresh_roles_view(interaction)
            else:
                role_lines = "\n".join(
                    f"• {role.mention}" for _, _, role in roles_to_delete if role
                )
                body = (
                    f"**{t('ui_components.role_configuration_buttons.confirm_delete_clan_roles_title', guild_id=guild_id_int)}**\n"
                    f"{t('ui_components.role_configuration_buttons.confirm_delete_clan_roles_body', guild_id=guild_id_int)}\n"
                    f"{role_lines}"
                )
                confirm_view = RoleDeleteConfirmationView(
                    feature="clan",
                    clan_management_view=self,
                    original_interaction=interaction,
                    timeout=120
                )
                confirm_view.confirmation_message = await interaction.followup.send(body, view=confirm_view, ephemeral=True)

    async def _on_toggle_member_role_mode(self, interaction: discord.Interaction) -> None:
        """Toggle member role assignment mode between SIMPLE and STRICT. Admin-only."""
        if not await self._check_admin_permission(interaction):
            return
        await interaction.response.defer(thinking=False, ephemeral=True)

        from qapbot.cache_manager import CACHE
        if not interaction.guild:
            return
        guild_id = str(interaction.guild.id)
        if guild_id not in CACHE.server_config:
            CACHE.server_config[guild_id] = {}
        current_strict = CACHE.server_config[guild_id].get("member_role_strict", False)
        CACHE.server_config[guild_id]["member_role_strict"] = not current_strict
        await CACHE.persist_server_config(guild_id)
        await self._refresh_roles_view(interaction)

    async def _on_create_family(self, interaction: discord.Interaction) -> None:
        """Handle Create Family button - create a new clan family. Admin-only."""
        # Check admin permission first
        if not await self._check_admin_permission(interaction):
            return
        
        # Show modal for family name
        # CreateFamilyModal is defined in this module
        modal = CreateFamilyModal(clan_management_view=self)
        await interaction.response.send_modal(modal)  # type: ignore[arg-type]
    
    
    async def _on_edit_family(self, interaction: discord.Interaction) -> None:
        """Handle Edit Family button - modify existing clan family. Only owning guild can edit."""
        from qapbot.cache_manager import CACHE
        
        # Only admins can edit families
        if not await self._check_admin_permission(interaction):
            return
        
        # Ensure we have a guild
        if not interaction.guild:
            await interaction.response.send_message("This command must be used in a guild.", ephemeral=True)
            return
        
        guild_id = str(interaction.guild.id)
        
        # Get families owned by this guild ONLY
        owned_families = {}
        for family_id, family_data in CACHE.clan_families.items():
            if family_data.get("owned_by_guild") == guild_id:
                owned_families[family_id] = family_data
        
        if not owned_families:
            from qapbot.i18n import t
            await interaction.response.send_message(
                t('ui_components.errors.error_no_families_to_edit', guild_id=int(guild_id)),
                ephemeral=True
            )
            return

        # Shortcut: only one family — skip dropdown, open edit directly
        if len(owned_families) == 1:
            only_id, only_data = next(iter(owned_families.items()))
            await self._edit_family_selected(interaction, only_id, only_data)  # type: ignore[arg-type]
            return

        # Build family selection options
        family_options = []
        for family_id, family_data in list(owned_families.items())[:25]:  # Discord limit  # type: ignore[arg-type]
            family_name = family_data.get("name", "Unknown")
            clan_count = len(family_data.get("clans", []))  # type: ignore[arg-type]
            family_options.append(
                discord.SelectOption(
                    label=f"{family_name}",
                    value=family_id,  # type: ignore[arg-type]
                    description=f"{clan_count} clans | ID: {family_id}"
                )
            )
        
        # Create view with dropdown selector
        from qapbot.i18n import t
        guild_id = interaction.guild.id if interaction.guild else None
        
        view = discord.ui.View(timeout=180)
        family_select = discord.ui.Select(
            placeholder=t('ui_components.family_management.edit_family_select_placeholder', guild_id=guild_id),
            options=family_options,  # type: ignore[arg-type]
            min_values=1,
            max_values=1,
            custom_id="edit_family_select"
        )
        
        async def select_callback(select_interaction: discord.Interaction):
            # For Select interactions, data is guaranteed to have 'values'
            family_id = select_interaction.data["values"][0]  # type: ignore[index]
            # Delete the selection message before showing edit interface
            try:
                await interaction.delete_original_response()
            except:
                pass
            await self._edit_family_selected(select_interaction, family_id, owned_families[family_id])  # type: ignore[arg-type]
        
        family_select.callback = select_callback  # type: ignore[assignment]
        view.add_item(family_select)  # type: ignore[arg-type]
        
        message_text = t('ui_components.family_management.edit_family_select_message', guild_id=guild_id) + "\n\n" + t('ui_components.family_management.edit_family_select_placeholder', guild_id=guild_id)
        
        await interaction.response.send_message(
            message_text,
            view=view,
            ephemeral=True
        )
    
    async def _edit_family_selected(self, interaction: discord.Interaction, family_id: str, family_data: Dict[str, Any]):
        """Handle family selection for editing."""
        # EditFamilyView is defined in this module
        
        if not interaction.guild:
            return
        
        # Show edit view
        edit_view = EditFamilyView(
            guild=interaction.guild,
            clan_management_view=self,
            family_id=family_id,
            family_data=family_data,
            timeout=300
        )
        
        await edit_view.show_edit_interface(interaction)
    
    async def _on_delete_family(self, interaction: discord.Interaction) -> None:
        """Handle Delete Family button - remove a clan family (ONLY guild owners can delete)."""
        from qapbot.cache_manager import CACHE
        from qapbot.i18n import t
        
        # Ensure we have a guild
        if not interaction.guild:
            await interaction.response.send_message("This command must be used in a guild.", ephemeral=True)
            return
        
        guild_id = str(interaction.guild.id)
        
        # Get families owned by this guild ONLY
        owned_families = {}
        for family_id, family_data in CACHE.clan_families.items():
            if family_data.get("owned_by_guild") == guild_id:
                owned_families[family_id] = family_data
        
        if not owned_families:
            await interaction.response.send_message(
                t('ui_components.errors.error_no_families_to_delete', guild_id=int(guild_id)),
                ephemeral=True
            )
            return

        # Shortcut: only one family — skip dropdown, open confirmation directly
        if len(owned_families) == 1:
            only_id, only_data = next(iter(owned_families.items()))
            await self._show_delete_family_confirmation(interaction, only_id, only_data)  # type: ignore[arg-type]
            return

        # Show family selection dropdown
        family_options = [
            discord.SelectOption(
                label=family_data.get("name", "Unknown")[:100],  # type: ignore[arg-type]
                value=family_id,  # type: ignore[arg-type]
                description=f"{len(family_data.get('clans', []))} clans"  # type: ignore[arg-type]
            )
            for family_id, family_data in list(owned_families.items())  # type: ignore[arg-type]
        ]
        
        view = discord.ui.View(timeout=180)
        family_select = discord.ui.Select(
            placeholder="Select family to delete...",
            options=family_options,  # type: ignore[arg-type]
            min_values=1,
            max_values=1,
            custom_id="delete_family_select"
        )
        
        async def select_callback(select_interaction: discord.Interaction):
            # For Select interactions, data is guaranteed to have 'values'
            family_id = select_interaction.data["values"][0]  # type: ignore[index]
            # Delete the selection message before showing confirmation
            try:
                await interaction.delete_original_response()
            except:
                pass
            await self._show_delete_family_confirmation(select_interaction, family_id, owned_families[family_id])  # type: ignore[arg-type]
        
        family_select.callback = select_callback  # type: ignore[assignment]
        view.add_item(family_select)  # type: ignore[arg-type]
        
        await interaction.response.send_message(
            "**Delete Clan Family**\n\nSelect the family you want to delete:",
            view=view,
            ephemeral=True
        )
    
    async def _show_delete_family_confirmation(self, interaction: discord.Interaction, family_id: str, family_data: Dict[str, Any]) -> None:
        """Show confirmation dialog with affected guilds and subscriptions."""
        from qapbot.cache_manager import CACHE
        
        # Ensure we have a guild
        if not interaction.guild:
            await interaction.response.send_message("This command must be used in a guild.", ephemeral=True)
            return
        
        guild_id = str(interaction.guild.id)
        family_name = family_data.get("name", "Unknown")
        family_clans = family_data.get("clans", [])
        
        # Find all guilds using this family
        affected_guilds = set()
        
        # Check member_families in server_config
        for gid, config in CACHE.server_config.items():
            if family_id in config.get("member_families", []):
                affected_guilds.add(gid)
        
        # Check subscriptions
        for gid, channel_subs in CACHE.subscriptions.items():
            for _, subs in channel_subs.items():
                for sub in subs:
                    if sub.get("clan_tag") == family_id:
                        affected_guilds.add(gid)
        
        # Build confirmation message
        from qapbot.i18n import t
        guild_id = interaction.guild.id if interaction.guild else None
        
        msg_parts = [
            f"{t('ui_components.errors.delete_family_title', guild_id=guild_id, family_name=family_name)}\n",
            f"{t('ui_components.errors.delete_family_action_description', guild_id=guild_id)}\n",
            f"{t('ui_components.errors.delete_family_action_1', guild_id=guild_id)}\n",
            f"{t('ui_components.errors.delete_family_action_2', guild_id=guild_id)}\n",
            f"{t('ui_components.errors.delete_family_action_3', guild_id=guild_id)}\n\n"
        ]
        
        if affected_guilds:
            from qapbot.i18n import t
            guild_id = interaction.guild.id if interaction.guild else None
            msg_parts.append(f"{t('ui_components.errors.family_will_affect_guilds', guild_id=guild_id, count=len(affected_guilds))}\n")  # type: ignore[arg-type]
            for gid in sorted(affected_guilds):  # type: ignore[arg-type]
                try:
                    guild = interaction.client.get_guild(int(gid))  # type: ignore[arg-type]
                    guild_name = guild.name if guild else f"Guild {gid}"
                except:
                    guild_name = f"Guild {gid}"
                msg_parts.append(f"• {guild_name}\n")
        else:
            from qapbot.i18n import t
            guild_id = interaction.guild.id if interaction.guild else None
            msg_parts.append(f"{t('ui_components.errors.no_other_guilds_using_family', guild_id=guild_id)}\n")
        
        msg_parts.append(f"\n{t('ui_components.errors.family_details_header', guild_id=guild_id)}\n")
        msg_parts.append(f"{t('ui_components.errors.family_details_clan_count', guild_id=guild_id, count=len(family_clans))}\n")
        # Build owner guild display: "Guild Name (ID)" or just the ID if guild not cached
        owner_guild_id_str: str = family_data.get("owned_by_guild", "")
        owner_guild_obj = interaction.client.get_guild(int(owner_guild_id_str)) if owner_guild_id_str else None
        owner_guild_display = f"{owner_guild_obj.name} ({owner_guild_id_str})" if owner_guild_obj else owner_guild_id_str or "Unknown"
        msg_parts.append(f"{t('ui_components.errors.family_details_created_by', guild_id=guild_id, owner_guild=owner_guild_display)}\n\n")
        msg_parts.append(f"{t('ui_components.errors.delete_family_warning', guild_id=guild_id)}")
        
        # Store confirmation interaction for deletion before success message
        self.confirmation_interaction = interaction
        
        # Create confirmation view
        confirm_view = discord.ui.View(timeout=180)
        
        confirm_button = discord.ui.Button(
            label="🗑️ Delete Family",
            style=discord.ButtonStyle.danger,
            custom_id="confirm_delete_family"
        )
        
        async def confirm_callback(confirm_interaction: discord.Interaction):
            # Delete confirmation message before showing success
            try:
                await self.confirmation_interaction.delete_original_response()
            except:
                pass
            await self._execute_delete_family(confirm_interaction, family_id, family_name, affected_guilds)  # type: ignore[arg-type]
        
        confirm_button.callback = confirm_callback  # type: ignore[assignment]
        confirm_view.add_item(confirm_button)  # type: ignore[arg-type]
        
        cancel_button = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            custom_id="cancel_delete_family"
        )
        
        async def cancel_callback(cancel_interaction: discord.Interaction):
            await cancel_interaction.response.defer()
            try:
                await self.confirmation_interaction.delete_original_response()
            except:
                pass
        
        cancel_button.callback = cancel_callback  # type: ignore[assignment]
        confirm_view.add_item(cancel_button)  # type: ignore[arg-type]
        
        await interaction.response.send_message(
            "".join(msg_parts),
            view=confirm_view,
            ephemeral=True
        )
    
    async def _execute_delete_family(self, interaction: discord.Interaction, family_id: str, family_name: str, affected_guilds: set) -> None:  # type: ignore[type-arg]
        """Execute the family deletion from all data sources."""
        from qapbot.cache_manager import CACHE

        await interaction.response.defer(ephemeral=True)

        try:
            # ---- Capture pre-deletion guild coverage for clan-role feature ----
            pre_guild_id: str | None = str(interaction.guild.id) if interaction.guild else None
            pre_old_coverage: set[str] | None = None
            if pre_guild_id and interaction.guild:
                pre_guild_config = CACHE.server_config.get(pre_guild_id, {})
                if (pre_guild_config.get("clan_role_enabled", False)
                        and family_id in pre_guild_config.get("member_families", [])):
                    # Build current (pre-deletion) coverage for this guild
                    pre_old_coverage = set(pre_guild_config.get("member_clans", []))
                    for fid in pre_guild_config.get("member_families", []):
                        pre_old_coverage.update(
                            CACHE.clan_families.get(fid, {}).get("clans", [])
                        )
            # ---- end pre-deletion capture ----

            # 1. Remove from clan_families
            if family_id in CACHE.clan_families:
                await CACHE.delete_clan_family(family_id)
                logging.info(f"Deleted family {family_name} ({family_id}) from cache and database")

            # 2. Remove from all subscriptions
            for gid, channel_subs in CACHE.subscriptions.items():
                for channel_id in list(channel_subs.keys()):
                    updated_subs = [
                        sub for sub in channel_subs[channel_id]
                        if sub.get("clan_tag") != family_id
                    ]
                    if updated_subs:
                        await CACHE.set_subscriptions_for_channel(gid, channel_id, updated_subs)
                    else:
                        await CACHE.delete_subscriptions_for_channel(gid, channel_id)
            logging.info(f"Removed family {family_name} ({family_id}) from all subscriptions")

            # 3. Remove from all server_config member_families
            for gid, config in CACHE.server_config.items():
                if family_id in config.get("member_families", []):
                    config["member_families"].remove(family_id)
                    await CACHE.persist_server_config(gid)
                    logging.info(f"Removed family {family_name} ({family_id}) from guild {gid} member_families")

            success_msg = t('ui_components.errors.family_deleted_success', guild_id=self.sent_message.guild.id if self.sent_message.guild else None, family_name=family_name)
            if affected_guilds:
                success_msg += "\n\n" + t('ui_components.errors.family_deleted_affected_guilds', guild_id=self.sent_message.guild.id if self.sent_message.guild else None, count=len(affected_guilds))  # type: ignore[arg-type]

            await interaction.followup.send(success_msg, ephemeral=True)
            logging.info(f"Successfully deleted family {family_name} ({family_id})")

            # Refresh the clan management view so it reflects the deleted family immediately
            try:
                if self.sent_message and self.sent_message.guild:
                    from qapbot.QBdiscocmdshelper import format_clan_management_message, get_guild_clans_including_member_config
                    updated_guild_clans = get_guild_clans_including_member_config(self.sent_message.guild.id)
                    main_embed, unlinked_embed, _, unlinked_players = await format_clan_management_message(
                        self.clan_tag,
                        self.sent_message.guild,
                        mode="families"
                    )
                    if main_embed is None and isinstance(unlinked_embed, list):  # type: ignore[misc]
                        display_embeds = [unlinked_embed[0]]
                        all_embeds = unlinked_embed
                    elif main_embed is not None and unlinked_embed is not None and not isinstance(unlinked_embed, list):  # type: ignore[misc]
                        display_embeds = [main_embed, unlinked_embed]
                        all_embeds = []
                    else:
                        display_embeds = [main_embed]
                        all_embeds = []
                    new_view = ClanManagementView(
                        clan_tag=self.clan_tag,
                        guild_clans=updated_guild_clans,
                        unlinked_players=unlinked_players,
                        sent_message=self.sent_message,
                        mode="families",
                        timeout=1800,
                        all_embeds=all_embeds,  # type: ignore[arg-type]
                        current_page=0
                    )
                    await self.sent_message.edit(embeds=display_embeds, view=new_view)  # type: ignore[arg-type]
            except Exception as _refresh_e:
                logging.warning(f"Could not auto-refresh clan management view after deleting family {family_name}: {_refresh_e}")

            # ---- Post-deletion clan-role handling ----
            if pre_guild_id and pre_old_coverage is not None and interaction.guild:
                post_guild_config = CACHE.server_config.get(pre_guild_id, {})
                post_new_coverage: set[str] = set(post_guild_config.get("member_clans", []))
                for fid in post_guild_config.get("member_families", []):
                    post_new_coverage.update(
                        CACHE.clan_families.get(fid, {}).get("clans", [])
                    )
                removable_tags = await _handle_clan_role_changes_for_guild(
                    interaction.guild, pre_guild_id, pre_old_coverage, post_new_coverage
                )
                if removable_tags:
                    removed_lines = "\n".join(
                        f"• {CACHE.get_clan_name(tag, tag)}"  # type: ignore[arg-type]
                        for tag in removable_tags
                    )
                    confirm_view = ConfirmDeleteClanRolesView(
                        guild=interaction.guild,
                        guild_id=pre_guild_id,
                        removed_tags=removable_tags,
                        role_names=removed_lines,
                    )
                    from qapbot.i18n import t as _t
                    confirm_msg = await interaction.followup.send(
                        _t('ui_components.role_configuration_buttons.removed_clan_roles_prompt',
                           guild_id=int(pre_guild_id),  # type: ignore[arg-type]
                           role_names=removed_lines),
                        view=confirm_view,
                        ephemeral=True,
                        wait=True,
                    )
                    confirm_view._msg = confirm_msg  # type: ignore[misc]
            # ---- end post-deletion clan-role handling ----

        except Exception as e:
            error_msg = t('ui_components.errors.error_deleting_family', guild_id=self.sent_message.guild.id if self.sent_message.guild else None, family_name=family_name, error=str(e))
            await interaction.followup.send(error_msg, ephemeral=True)
            logging.error(f"Error deleting family {family_name} ({family_id}): {e}", exc_info=True)
    
    # ========================================
    # Basic Configuration Callbacks
    # ========================================
    
    async def _on_language_select(self, interaction: discord.Interaction) -> None:
        """Handle language selection."""
        if not await self._check_admin_permission(interaction):
            return
        
        await interaction.response.defer(thinking=False, ephemeral=False)
        
        from qapbot.i18n import t, set_guild_language
        
        # For Select interactions, data is guaranteed to have 'values'
        if not interaction.data or 'values' not in interaction.data:
            return
        selected_language = interaction.data['values'][0]  # type: ignore[index]
        
        # Ensure we have a guild
        if not interaction.guild:
            return
        guild_id = interaction.guild.id
        
        # Update language setting
        await set_guild_language(guild_id, selected_language)
        
        # Refresh view with new language
        await self._refresh_config_view(interaction)
        
        # Send confirmation in new language
        language_display_name = "English" if selected_language == "en" else "Deutsch"
        success_msg = t('ui_components.basic_config.language_updated', guild_id=guild_id, language_name=language_display_name)
        await interaction.followup.send(success_msg, ephemeral=True)
    
    async def _on_war_channel_select(self, interaction: discord.Interaction) -> None:
        """Handle war notification channel selection."""
        if not await self._check_admin_permission(interaction):
            return
        
        await interaction.response.defer(thinking=False, ephemeral=False)
        
        from qapbot.cache_manager import CACHE
        
        # Ensure we have a guild
        if not interaction.guild:
            await interaction.followup.send("This command must be used in a guild.", ephemeral=True)
            return
        
        # ChannelSelect returns channel objects in values
        # Type ignore for discord.py dynamic typing
        selected_channel = interaction.data['resolved']['channels'][interaction.data['values'][0]]  # type: ignore[index]
        selected_channel_id = selected_channel['id']  # type: ignore[index]
        guild_id = str(interaction.guild.id)
        
        # Update war notification channel setting
        if guild_id not in CACHE.server_config:
            CACHE.server_config[guild_id] = {}
        
        CACHE.server_config[guild_id]["war_notification_channel_id"] = selected_channel_id
        await CACHE.persist_server_config(guild_id)
        
        # Refresh view
        await self._refresh_config_view(interaction)
    
    async def _on_toggle_registration(self, interaction: discord.Interaction) -> None:
        """Toggle registration message enabled/disabled."""
        if not await self._check_admin_permission(interaction):
            return
        
        await interaction.response.defer(thinking=False, ephemeral=False)
        
        from qapbot.cache_manager import CACHE
        from qapbot.i18n import t
        
        if not interaction.guild:
            return
        guild_id_str = str(interaction.guild.id)
        guild_id_int = interaction.guild.id

        # Read current state without mutating cache until validation passes
        guild_config = CACHE.server_config.get(guild_id_str, {})
        current_enabled = guild_config.get("registration_message_enabled", False)
        
        # If trying to enable, check if registration channel is set
        if not current_enabled:  # Trying to enable
            registration_channel_id = guild_config.get("registration_channel_id")
            if not registration_channel_id:
                # Cannot enable without a channel set
                await interaction.followup.send(
                    t('ui_components.basic_config.no_registration_channel_set', guild_id=guild_id_int),
                    ephemeral=True
                )
                return
        
        # Toggle state
        if guild_id_str not in CACHE.server_config:
            CACHE.server_config[guild_id_str] = {}
        CACHE.server_config[guild_id_str]["registration_message_enabled"] = not current_enabled
        await CACHE.persist_server_config(guild_id_str)
        
        # Trigger immediate action based on new state
        try:
            from QapBot import repost_playerregistration_messages
            import QBcore
            if current_enabled == False:  # Was disabled, now enabling
                # Post the registration message immediately
                QBcore.spawn_tracked("repost-registration-msg", repost_playerregistration_messages(only_if_not_bottom=False))
            else:  # Was enabled, now disabling
                # Delete the registration message immediately
                QBcore.spawn_tracked("repost-registration-msg", repost_playerregistration_messages(only_if_not_bottom=False))
        except Exception as e:
            logging.warning(f"Could not update registration message immediately: {e}")
        
        # Refresh view
        await self._refresh_config_view(interaction)
    
    async def _on_toggle_channel_notifications(self, interaction: discord.Interaction) -> None:
        """Toggle channel war notifications enabled/disabled."""
        if not await self._check_admin_permission(interaction):
            return
        
        await interaction.response.defer(thinking=False, ephemeral=False)
        
        from qapbot.cache_manager import CACHE
        from qapbot.i18n import t
        
        if not interaction.guild:
            return
        guild_id = str(interaction.guild.id)

        # Read current state without mutating cache until validation passes
        guild_config = CACHE.server_config.get(guild_id, {})
        current_enabled = guild_config.get("channel_war_notifications_enabled", False)

        # Check if war notification channel is set
        war_channel_id = guild_config.get("war_notification_channel_id")
        
        if not current_enabled and not war_channel_id:
            # Trying to enable but no channel set
            error_msg = t('ui_components.basic_config.no_war_channel_set', guild_id=int(guild_id))
            await interaction.followup.send(error_msg, ephemeral=True)
            return
        
        # Toggle state
        if guild_id not in CACHE.server_config:
            CACHE.server_config[guild_id] = {}
        CACHE.server_config[guild_id]["channel_war_notifications_enabled"] = not current_enabled
        await CACHE.persist_server_config(guild_id)
        
        # Refresh view
        await self._refresh_config_view(interaction)

    async def _on_toggle_welcome_message(self, interaction: discord.Interaction) -> None:
        """Toggle welcome message enabled/disabled."""
        if not await self._check_admin_permission(interaction):
            return

        await interaction.response.defer(thinking=False, ephemeral=False)

        from qapbot.cache_manager import CACHE
        from qapbot.i18n import t

        if not interaction.guild:
            return
        guild_id_str = str(interaction.guild.id)
        guild_id_int = interaction.guild.id

        guild_config = CACHE.server_config.get(guild_id_str, {})
        current_enabled = guild_config.get("welcome_message_enabled", False)

        # Consistency check: block enabling if apply_channel mode has no channel configured.
        # Clan-link mode is allowed with zero clans/families selected — the welcome message
        # simply omits the clan-link line in that case.
        if not current_enabled:
            mode = guild_config.get("welcome_message_mode", "clan_link")
            no_channel = mode == "apply_channel" and not guild_config.get("welcome_apply_channel_id", "")
            if no_channel:
                await interaction.followup.send(
                    t('ui_components.basic_config.welcome_enable_blocked', guild_id=guild_id_int),
                    ephemeral=True
                )
                return

        if guild_id_str not in CACHE.server_config:
            CACHE.server_config[guild_id_str] = {}
        CACHE.server_config[guild_id_str]["welcome_message_enabled"] = not current_enabled
        await CACHE.persist_server_config(guild_id_str)

        await self._refresh_config_view(interaction)

    async def _on_configure_welcome_message(self, interaction: discord.Interaction) -> None:
        """Open welcome message configuration view."""
        if not await self._check_admin_permission(interaction):
            return

        await interaction.response.defer(thinking=False, ephemeral=False)

        if not interaction.guild:
            return

        welcome_config_view = WelcomeMessageConfigView(
            guild=interaction.guild,
            clan_management_view=self,
            original_interaction=interaction,
            timeout=300
        )

        initial_content = welcome_config_view._build_header_content(interaction.guild.id)  # type: ignore[reportPrivateUsage]
        msg = await interaction.followup.send(initial_content, view=welcome_config_view, ephemeral=True)
        welcome_config_view.config_message = msg

    async def _refresh_config_view(self, interaction: discord.Interaction) -> None:
        """Helper function to refresh the config view after changes."""
        from qapbot.QBdiscocmdshelper import format_clan_management_message
        
        if not interaction.guild:
            return
        
        try:
            # Regenerate config view
            main_embed, _, _, _ = await format_clan_management_message(
                self.clan_tag,
                interaction.guild,
                mode="config"
            )
            
            # Create new view with updated config
            new_view = ClanManagementView(
                clan_tag=self.clan_tag,
                guild_clans=self.guild_clans,
                unlinked_players=self.unlinked_players,
                sent_message=self.sent_message,
                mode="config",
                timeout=1800
            )
            
            await self.sent_message.edit(
                embeds=[main_embed],  # type: ignore[arg-type]
                view=new_view
            )
            
        except Exception as e:
            logging.error(f"Failed to refresh config view: {e}")
    
    async def _on_select_channels(self, interaction: discord.Interaction) -> None:
        """Open channel configuration view."""
        if not await self._check_admin_permission(interaction):
            return
        
        # Defer first to get a proper response
        await interaction.response.defer(thinking=False, ephemeral=False)
        
        from qapbot.cache_manager import CACHE
        
        if not interaction.guild:
            return
        guild_id_str = str(interaction.guild.id)
        config = CACHE.server_config.get(guild_id_str, {})
        
        # Resolve the current channel object (if any) for every configured slot
        current_channels: Dict[str, Optional[discord.TextChannel]] = {}
        for slot in DEFAULT_CHANNEL_SLOTS:
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
        
        # Create channel configuration view
        channel_config_view = ChannelConfigurationView(
            guild=interaction.guild,
            clan_management_view=self,
            original_interaction=interaction,
            current_channels=current_channels,
            timeout=300
        )
        
        header_msg = channel_config_view._format_header()
        
        # Use followup.send() to get a proper discord.Message object
        msg = await interaction.followup.send(
            header_msg,
            view=channel_config_view,
            ephemeral=True
        )
        
        # Store the message so it can be deleted after applying
        channel_config_view.config_message = msg
    
    async def _on_select_language(self, interaction: discord.Interaction) -> None:
        """Open language configuration view."""
        if not await self._check_admin_permission(interaction):
            return
        
        # Defer first to get a proper response
        await interaction.response.defer(thinking=False, ephemeral=False)
        
        from qapbot.i18n import get_guild_language
        
        if not interaction.guild:
            return
        
        guild_id = interaction.guild.id
        current_language = get_guild_language(guild_id)
        
        # Create language configuration view
        language_config_view = LanguageConfigurationView(
            guild=interaction.guild,
            clan_management_view=self,
            original_interaction=interaction,
            current_language=current_language,
            timeout=300
        )
        
        language_display = "English" if current_language == "en" else "Deutsch" if current_language == "de" else current_language
        header_msg = f"🌍 **Language Configuration**\n\nCurrent Language: **{language_display}**"
        
        # Use followup.send() to get a proper discord.Message object
        msg = await interaction.followup.send(
            header_msg,
            view=language_config_view,
            ephemeral=True
        )
        
        # Store the message so it can be deleted after applying
        language_config_view.config_message = msg
    
    async def _on_select_threshold(self, interaction: discord.Interaction) -> None:
        """Open notification threshold configuration view."""
        if not await self._check_admin_permission(interaction):
            return
        
        # Defer first to get a proper response
        await interaction.response.defer(thinking=False, ephemeral=False)
        
        if not interaction.guild:
            return
        
        guild_id = interaction.guild.id
        guild_config = CACHE.server_config.get(str(guild_id), {})
        current_threshold = guild_config.get("war_notification_threshold_hours", 1.0)
        
        # Create threshold configuration view
        threshold_config_view = NotificationThresholdConfigurationView(
            guild=interaction.guild,
            clan_management_view=self,
            original_interaction=interaction,
            current_threshold_hours=current_threshold,
            timeout=300
        )
        
        from qapbot.i18n import t
        threshold_display = self._format_threshold_label(current_threshold, guild_id)
        title = t('ui_components.basic_config.threshold_config_title', guild_id=guild_id)
        current_msg = t('ui_components.basic_config.threshold_config_current', guild_id=guild_id, threshold=threshold_display)
        header_msg = f"{title}\n\n{current_msg}"
        
        # Use followup.send() to get a proper discord.Message object
        msg = await interaction.followup.send(
            header_msg,
            view=threshold_config_view,
            ephemeral=True
        )
        
        # Store the message so it can be deleted after applying
        threshold_config_view.config_message = msg
    
    def _format_threshold_label(self, hours: float, guild_id: Optional[int] = None) -> str:
        """Format threshold hours as readable label."""
        from qapbot.i18n import t
        
        if hours == 0.5:
            return t('ui_components.basic_config.threshold_time_30_minutes', guild_id=guild_id)
        elif hours == 1.0:
            return t('ui_components.basic_config.threshold_time_1_hour', guild_id=guild_id)
        elif hours == 2.0:
            return t('ui_components.basic_config.threshold_time_2_hours', guild_id=guild_id)
        elif hours == 4.0:
            return t('ui_components.basic_config.threshold_time_4_hours', guild_id=guild_id)
        else:
            # Fallback for custom values
            if hours < 1:
                minutes = int(hours * 60)
                return t('ui_components.basic_config.threshold_time_30_minutes', guild_id=guild_id) if minutes == 30 else f"{minutes} min"
            else:
                return f"{hours:.1f} " + t('ui_components.basic_config.threshold_time_4_hours', guild_id=guild_id).split()[-1]
    
    async def _on_config_manage_member_clans(self, interaction: discord.Interaction) -> None:
        """Handle Member Clans button in config mode - manage which clans grant member role. Admin-only."""
        from qapbot.cache_manager import CACHE
        from qapbot.i18n import t
        
        # Check admin permission first
        if not await self._check_admin_permission(interaction):
            return
        
        if not interaction.guild:
            return
        guild_id = str(interaction.guild.id)
        config = CACHE.server_config.get(guild_id, {})
        
        # Get current member clans and families
        current_member_clans = config.get("member_clans", [])
        current_member_families = config.get("member_families", [])
        
        # Get available clans from subscriptions (exclude non-clan subscriptions like playerregistration and families)
        guild_subscriptions = CACHE.subscriptions.get(guild_id, {})
        available_clans = set()
        for channel_subs in guild_subscriptions.values():
            for sub in channel_subs:
                if "clan_tag" in sub and sub.get("subscription_type") != "playerregistration":
                    tag = sub["clan_tag"]
                    # Skip family tags - only add individual clans
                    if tag not in CACHE.clan_families:
                        available_clans.add(tag)
        
        # Also include any clans that were previously added manually (not in subscriptions)
        # This ensures manually added clans persist when reopening the view
        available_clans.update(current_member_clans)
        
        # Check for direct family subscriptions
        subscribed_family_tags = set()
        for channel_subs in guild_subscriptions.values():
            for sub in channel_subs:
                if "clan_tag" in sub:
                    tag = sub["clan_tag"]
                    # Check if this tag is a family
                    if tag in CACHE.clan_families:
                        subscribed_family_tags.add(tag)
        
        if not available_clans and not subscribed_family_tags:
            await interaction.response.send_message(
                t('ui_components.errors.error_no_clans_available', guild_id=interaction.guild.id),
                ephemeral=True
            )
            return
        
        # Filter families to only those with:
        # 1. Direct subscription on this guild, OR
        # 2. At least one clan with a subscription on this guild
        available_families = {}
        for family_id, family_data in CACHE.clan_families.items():
            # Include if family has direct subscription
            if family_id in subscribed_family_tags:
                available_families[family_id] = family_data
            else:
                # Include if any clan in this family is subscribed
                family_clans = family_data.get("clans", [])
                if any(clan_tag in available_clans for clan_tag in family_clans):
                    available_families[family_id] = family_data
        
        if not interaction.guild:
            return
        
        # Create member clans configuration view
        clans_config_view = MemberClansConfigurationView(
            guild=interaction.guild,
            clan_management_view=self,
            original_interaction=interaction,
            current_member_clans=current_member_clans,
            current_member_families=current_member_families,
            clan_families=available_families,  # type: ignore[arg-type]
            timeout=300
        )
        
        # Build current settings display
        display_parts = []
        
        if current_member_families:
            family_list = []
            for family_id in current_member_families[:5]:
                family_data = CACHE.clan_families.get(family_id, {})
                family_name = family_data.get("name", "Unknown")
                clan_count = len(family_data.get("clans", []))  # type: ignore[arg-type]
                family_list.append(f"🏰 {family_name} ({clan_count} clans)")
            
            if len(current_member_families) > 5:
                family_list.append(f"*... and {len(current_member_families) - 5} more families*")  # type: ignore[arg-type]
            
            display_parts.append(f"**Families ({len(current_member_families)}):**\n" + "\n".join(family_list))  # type: ignore[arg-type]
        
        if current_member_clans:
            clan_list = [f"• {CACHE.get_clan_name(tag, 'Unknown')} ({tag})" for tag in current_member_clans[:5]]  # type: ignore[arg-type]
            if len(current_member_clans) > 5:
                clan_list.append(f"*... and {len(current_member_clans) - 5} more clans*")
            
            display_parts.append(f"**Individual Clans ({len(current_member_clans)}):**\n" + "\n".join(clan_list))  # type: ignore[arg-type]
        
        if not display_parts:
            display_text = t('ui_components.errors.error_no_clans_configured', guild_id=interaction.guild.id)
        else:
            display_text = "\n\n".join(display_parts)  # type: ignore[arg-type]
        
        user_id = str(interaction.user.id)
        guild_id_int = interaction.guild.id
        header_msg = t('ui_components.prompts.member_clans_header', 
                       user_id=user_id, guild_id=guild_id_int, display_text=display_text)
        
        await interaction.response.defer(thinking=False, ephemeral=True)
        msg = await interaction.followup.send(
            header_msg,
            view=clans_config_view,
            ephemeral=True
        )
        # Store reference to message so view can edit it later
        clans_config_view.sent_message = msg


# ============================================================================
# Channel Configuration View
# ============================================================================

class ChannelSlotConfig:
    """Definition for one configurable channel slot in ChannelConfigurationView.

    Adding a new channel slot (e.g. a future CWL hub channel) is a new instance
    in DEFAULT_CHANNEL_SLOTS below — no new select/button/handler code needed.
    """
    __slots__ = ("key", "label", "config_key", "disable_flag_keys", "on_apply")

    def __init__(
        self,
        key: str,
        label: str,
        config_key: str,
        disable_flag_keys: Tuple[str, ...] = (),
        on_apply: Optional[Callable[[str, Optional[str], Optional[str]], None]] = None,
    ):
        self.key = key
        self.label = label
        self.config_key = config_key
        self.disable_flag_keys = disable_flag_keys
        # Called as on_apply(guild_id_str, old_channel_id, new_channel_id) right after this
        # slot's config_key is written, for slot-specific side effects (e.g. change tracking).
        self.on_apply = on_apply


def _track_registration_channel_change(guild_id_str: str, old_channel_id: Optional[str], new_channel_id: Optional[str]) -> None:
    """Record the previous registration channel so the next repost can clean up the old message."""
    if old_channel_id and new_channel_id and old_channel_id != new_channel_id:
        CACHE.server_config[guild_id_str]["_old_registration_channel_id"] = old_channel_id
        logging.debug(f"Channel change tracked during apply: old={old_channel_id}, new={new_channel_id}")


DEFAULT_CHANNEL_SLOTS: Tuple[ChannelSlotConfig, ...] = (
    ChannelSlotConfig(
        key="registration",
        label="Registration",
        config_key="registration_channel_id",
        disable_flag_keys=("registration_message_enabled",),
        on_apply=_track_registration_channel_change,
    ),
    ChannelSlotConfig(
        key="war",
        label="War",
        config_key="war_notification_channel_id",
        disable_flag_keys=("channel_war_notifications_enabled",),
    ),
)


class ChannelConfigurationView(discord.ui.View):
    """Generic view for configuring an arbitrary set of guild notification channels.

    Driven by a list of `ChannelSlotConfig` (default: registration + war notifications) —
    adding a new channel slot is a data change, not a new select/button/handler.
    """
    def __init__(
        self,
        guild: discord.Guild,
        clan_management_view: 'ClanManagementView',
        original_interaction: discord.Interaction,
        current_channels: Optional[Dict[str, Optional[discord.TextChannel]]] = None,
        slots: Tuple[ChannelSlotConfig, ...] = DEFAULT_CHANNEL_SLOTS,
        timeout: int = 300
    ):
        super().__init__(timeout=timeout)
        self.guild = guild
        self.clan_management_view = clan_management_view
        self.original_interaction = original_interaction
        self.slots = slots
        self.selected_channels: Dict[str, Optional[discord.TextChannel]] = dict(current_channels or {})

        # Store config message for later deletion
        self.config_message: Optional[discord.Message] = None

        # Add UI components: one select row per slot, then apply + one clear button per slot
        for row, slot in enumerate(self.slots):
            self._add_channel_select(slot, row)
        button_row = len(self.slots)
        self._add_apply_button(button_row)
        self._add_clear_buttons(button_row)

    def _add_channel_select(self, slot: ChannelSlotConfig, row: int) -> None:
        """Add a channel selector for one slot."""
        select = discord.ui.ChannelSelect(
            placeholder=f"Select {slot.label.lower()} channel...",
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text],
            custom_id=f"config_channel_select_{slot.key}",
            row=row
        )
        select.callback = self._make_select_callback(slot)  # type: ignore[assignment]
        self.add_item(select)  # type: ignore[arg-type]

    def _make_select_callback(self, slot: ChannelSlotConfig) -> Callable[[discord.Interaction], Any]:
        async def _callback(interaction: discord.Interaction) -> None:
            logging.debug(f"ChannelConfigurationView select callback for slot '{slot.key}'")
            await interaction.response.defer(thinking=False, ephemeral=False)
            selected_channel = interaction.data['resolved']['channels'][interaction.data['values'][0]]  # type: ignore[index]
            channel = discord.utils.get(self.guild.text_channels, id=int(selected_channel['id']))  # type: ignore[arg-type]
            self.selected_channels[slot.key] = channel  # type: ignore[assignment]
            logging.debug(f"{slot.label} channel selected: {channel.name if channel else 'None'}")
        return _callback

    def _add_apply_button(self, row: int) -> None:
        """Add apply button to save changes."""
        apply_button = discord.ui.Button(
            label="Apply Changes",
            style=discord.ButtonStyle.success,
            custom_id="apply_channel_config",
            row=row
        )
        apply_button.callback = self._on_apply  # type: ignore[assignment]
        self.add_item(apply_button)  # type: ignore[arg-type]

    def _add_clear_buttons(self, row: int) -> None:
        """Add one clear button per slot."""
        for slot in self.slots:
            clear_button = discord.ui.Button(
                label=f"Clear {slot.label}",
                style=discord.ButtonStyle.danger,
                custom_id=f"clear_channel_{slot.key}",
                row=row
            )
            clear_button.callback = self._make_clear_callback(slot)  # type: ignore[assignment]
            self.add_item(clear_button)  # type: ignore[arg-type]

    def _make_clear_callback(self, slot: ChannelSlotConfig) -> Callable[[discord.Interaction], Any]:
        async def _callback(interaction: discord.Interaction) -> None:
            await interaction.response.defer(thinking=False, ephemeral=False)
            self.selected_channels[slot.key] = None
            await self._refresh_header_message()
        return _callback

    def _format_header(self) -> str:
        """Build the header message text reflecting the currently selected channels."""
        lines = [
            f"{slot.label} Channel: {channel.mention if (channel := self.selected_channels.get(slot.key)) else '❌ Not set'}"
            for slot in self.slots
        ]
        return "🌐 **Channel Configuration**\n\n" + "\n".join(lines)

    async def _refresh_header_message(self) -> None:
        if self.config_message:
            try:
                await self.config_message.edit(content=self._format_header(), view=self)
            except Exception as e:
                logging.error(f"Failed to update config message: {e}")

    async def _on_apply(self, interaction: discord.Interaction) -> None:
        """Apply channel configuration changes for every slot."""
        await interaction.response.defer(thinking=False, ephemeral=False)

        from qapbot.cache_manager import CACHE

        # Ensure we have a guild
        if not interaction.guild:
            await interaction.response.send_message("This command must be used in a guild.", ephemeral=True)
            return

        guild_id_str = str(interaction.guild.id)

        if guild_id_str not in CACHE.server_config:
            CACHE.server_config[guild_id_str] = {}
        guild_config = CACHE.server_config[guild_id_str]

        for slot in self.slots:
            old_channel_id = guild_config.get(slot.config_key)
            selected_channel = self.selected_channels.get(slot.key)
            if selected_channel:
                new_channel_id = str(selected_channel.id)
                guild_config[slot.config_key] = new_channel_id
                if slot.on_apply:
                    slot.on_apply(guild_id_str, old_channel_id, new_channel_id)
            elif slot.config_key in guild_config:
                # If cleared, remove from config and disable the dependent feature(s)
                del guild_config[slot.config_key]
                for flag_key in slot.disable_flag_keys:
                    guild_config[flag_key] = False

        await CACHE.persist_server_config(guild_id_str)

        # Trigger repost if registration message is enabled (handles channel change or other updates)
        if guild_config.get("registration_message_enabled", False):
            logging.debug(f"Registration message enabled, triggering repost after apply")
            try:
                from QapBot import repost_playerregistration_messages
                import QBcore
                QBcore.spawn_tracked("repost-registration-msg", repost_playerregistration_messages(only_if_not_bottom=False))
                logging.debug(f"Repost task created after apply")
            except Exception as e:
                logging.warning(f"Could not trigger repost after channel config apply: {e}")

        # Refresh management view
        await self.clan_management_view._refresh_config_view(interaction)  # type: ignore[attr-defined]

        # Delete the configuration message after a short delay to ensure main view is updated
        if self.config_message:
            try:
                await self.config_message.delete()
                logging.info("Channel config message deleted successfully")
            except discord.NotFound:
                logging.debug("Channel config message already deleted")
            except discord.Forbidden:
                logging.debug("No permission to delete channel config message")
            except Exception as e:
                logging.error(f"Failed to delete channel config message: {e}")


# ============================================================================
# Language Configuration View
# ============================================================================

class LanguageConfigurationView(discord.ui.View):
    """View for configuring server language."""
    def __init__(
        self,
        guild: discord.Guild,
        clan_management_view: 'ClanManagementView',
        original_interaction: discord.Interaction,
        current_language: str = "en",
        timeout: int = 300
    ):
        super().__init__(timeout=timeout)
        self.guild = guild
        self.clan_management_view = clan_management_view
        self.original_interaction = original_interaction
        self.selected_language = current_language
        
        # Store config message for later deletion
        self.config_message: Optional[discord.Message] = None
        
        # Add UI components
        self._add_language_select()
    
    def _add_language_select(self):
        """Add language selector."""
        language_options = [
            discord.SelectOption(label="English", value="en", emoji="🇬🇧"),  # type: ignore[arg-type]
            discord.SelectOption(label="Deutsch", value="de", emoji="🇩🇪", default=(self.selected_language == "de"))  # type: ignore[arg-type]
        ]
        # Set default on English if not de
        if self.selected_language != "de":
            language_options[0].default = True
        
        language_select = discord.ui.Select(
            placeholder="Select language...",
            min_values=1,
            max_values=1,
            options=language_options,  # type: ignore[arg-type]
            custom_id="config_language_select",
            row=0
        )
        language_select.callback = self._on_language_select  # type: ignore[assignment]
        self.add_item(language_select)  # type: ignore[arg-type]
    
    async def _on_language_select(self, interaction: discord.Interaction) -> None:
        """Handle language selection - auto-apply immediately."""
        await interaction.response.defer(thinking=False, ephemeral=False)
        
        from qapbot.i18n import set_guild_language
        
        if not interaction.guild:
            return
        
        # Get selected language
        selected_language = interaction.data['values'][0]  # type: ignore[index]
        self.selected_language = selected_language
        
        await set_guild_language(interaction.guild.id, self.selected_language)  # type: ignore[arg-type]
        
        # Refresh management view
        await self.clan_management_view._refresh_config_view(interaction)  # type: ignore[attr-defined]
        
        # Repost welcome messages with new language immediately
        try:
            from QapBot import repost_playerregistration_messages
            import QBcore
            # Delete old and post new with updated language immediately
            QBcore.spawn_tracked("repost-registration-msg", repost_playerregistration_messages(only_if_not_bottom=False))
            logging.info(f"Triggered welcome message repost with language '{self.selected_language}' for guild {interaction.guild.id}")
        except Exception as e:
            logging.warning(f"Could not trigger welcome message repost after language change: {e}")
        
        # Delete the configuration message after a short delay
        if self.config_message:
            try:
                await self.config_message.delete()
                logging.info("Language config message deleted successfully")
            except discord.NotFound:
                logging.debug("Language config message already deleted")
            except discord.Forbidden:
                logging.debug("No permission to delete language config message")
            except Exception as e:
                logging.error(f"Failed to delete language config message: {e}")


class NotificationThresholdConfigurationView(discord.ui.View):
    """View for configuring war notification time threshold."""
    
    THRESHOLD_OPTIONS = [
        {"label": "30 minutes", "value": "30min", "hours": 0.5},
        {"label": "1 hour", "value": "1h", "hours": 1.0},
        {"label": "2 hours", "value": "2h", "hours": 2.0},
        {"label": "4 hours", "value": "4h", "hours": 4.0},
    ]
    
    def __init__(
        self,
        guild: discord.Guild,
        clan_management_view: 'ClanManagementView',
        original_interaction: discord.Interaction,
        current_threshold_hours: float = 1.0,
        timeout: int = 300
    ):
        super().__init__(timeout=timeout)
        self.guild = guild
        self.clan_management_view = clan_management_view
        self.original_interaction = original_interaction
        self.current_threshold_hours = current_threshold_hours
        
        # Store config message for later deletion
        self.config_message: Optional[discord.Message] = None
        
        # Add UI components
        self._add_threshold_select()
    
    def _add_threshold_select(self):
        """Add notification threshold selector."""
        threshold_options = []
        for opt in self.THRESHOLD_OPTIONS:
            option = discord.SelectOption(
                label=opt["label"],  # type: ignore[arg-type]
                value=opt["value"],  # type: ignore[arg-type]
                emoji="⏰",
                default=(opt["hours"] == self.current_threshold_hours)  # type: ignore[arg-type]
            )
            threshold_options.append(option)
        
        threshold_select = discord.ui.Select(
            placeholder="Select notification threshold...",
            min_values=1,
            max_values=1,
            options=threshold_options,  # type: ignore[arg-type]
            custom_id="config_threshold_select",
            row=0
        )
        threshold_select.callback = self._on_threshold_select  # type: ignore[assignment]
        self.add_item(threshold_select)  # type: ignore[arg-type]
    
    async def _on_threshold_select(self, interaction: discord.Interaction) -> None:
        """Handle threshold selection - auto-apply immediately."""
        await interaction.response.defer(thinking=False, ephemeral=False)
        
        # Get selected threshold value
        selected_value = interaction.data['values'][0]  # type: ignore[index]
        
        # Find matching threshold option
        selected_option = None
        for opt in self.THRESHOLD_OPTIONS:
            if opt["value"] == selected_value:
                selected_option = opt
                break
        
        if not selected_option:
            logging.error(f"Invalid threshold value selected: {selected_value}")
            return
        
        # Update guild config with new threshold
        guild_id = str(self.guild.id)
        if guild_id not in CACHE.server_config:
            CACHE.server_config[guild_id] = {}
        
        CACHE.server_config[guild_id]["war_notification_threshold_hours"] = selected_option["hours"]
        self.current_threshold_hours = selected_option["hours"]
        
        # Save config
        await CACHE.persist_server_config(guild_id)
        
        logging.info(f"Updated war notification threshold for guild {guild_id} to {selected_option['hours']} hours")
        
        # Refresh management view to show new threshold
        await self.clan_management_view._refresh_config_view(interaction)  # type: ignore[attr-defined]
        
        # Delete the configuration message after a short delay
        if self.config_message:
            try:
                await self.config_message.delete()
                logging.info("Notification threshold config message deleted successfully")
            except discord.NotFound:
                logging.debug("Notification threshold config message already deleted")
            except discord.Forbidden:
                logging.debug("No permission to delete notification threshold config message")
            except Exception as e:
                logging.error(f"Failed to delete notification threshold config message: {e}")


class RoleConfigurationView(discord.ui.View):
    """
    View for configuring newbie and member roles for automatic assignment.
    
    Provides:
    - Role selector for newbie role
    - Role selector for member role
    - Clear button for each role
    - Apply button to save changes
    
    Args:
        guild: Discord guild object
        clan_management_view: Parent ClanManagementView to refresh after save
        original_interaction: The interaction that triggered the configuration (for cleanup)
        current_newbie_role: Currently configured newbie role (or None)
        current_member_role: Currently configured member role (or None)
        timeout: View timeout in seconds
    """
    def __init__(
        self,
        guild: discord.Guild,
        clan_management_view: 'ClanManagementView',
        original_interaction: discord.Interaction,
        current_newbie_role: Optional[discord.Role] = None,
        current_member_role: Optional[discord.Role] = None,
        timeout: int = 300
    ):
        super().__init__(timeout=timeout)
        self.guild = guild
        self.clan_management_view = clan_management_view
        self.original_interaction = original_interaction
        
        # Selected roles (start with current values)
        self.newbie_role: Optional[discord.Role] = current_newbie_role
        self.member_role: Optional[discord.Role] = current_member_role
        
        # Add UI components
        self._add_newbie_role_select()
        self._add_member_role_select()
        self._add_clear_buttons()
        self._add_apply_button()
    
    def _add_newbie_role_select(self):
        """Add role selector for newbie role."""
        from qapbot.i18n import t
        guild_id = self.guild.id if self.guild else None
        
        role_select = discord.ui.RoleSelect(
            placeholder=t('ui_components.role_configuration.placeholder_newbie_role', guild_id=guild_id),
            min_values=1,
            max_values=1,
            custom_id="newbie_role_select",
            row=0
        )
        role_select.callback = self._on_newbie_role_select  # type: ignore[assignment]
        self.add_item(role_select)  # type: ignore[arg-type]
    
    def _add_member_role_select(self):
        """Add role selector for member role."""
        from qapbot.i18n import t
        guild_id = self.guild.id if self.guild else None
        
        role_select = discord.ui.RoleSelect(
            placeholder=t('ui_components.role_configuration.placeholder_member_role', guild_id=guild_id),
            min_values=1,
            max_values=1,
            custom_id="member_role_select",
            row=1
        )
        role_select.callback = self._on_member_role_select  # type: ignore[assignment]
        self.add_item(role_select)  # type: ignore[arg-type]
    
    def _add_clear_buttons(self):
        """Add clear buttons for both roles."""
        from qapbot.i18n import t
        guild_id = self.guild.id if self.guild else None
        
        # Clear newbie role button
        clear_newbie_button = discord.ui.Button(
            label=t('ui_components.role_configuration.button_clear_newbie', guild_id=guild_id),
            style=discord.ButtonStyle.secondary,
            custom_id="clear_newbie_role",
            row=2,
            disabled=(self.newbie_role is None)
        )
        clear_newbie_button.callback = self._on_clear_newbie_role  # type: ignore[assignment]
        self.add_item(clear_newbie_button)  # type: ignore[arg-type]
        
        # Clear member role button
        clear_member_button = discord.ui.Button(
            label=t('ui_components.role_configuration.button_clear_member', guild_id=guild_id),
            style=discord.ButtonStyle.secondary,
            custom_id="clear_member_role",
            row=2,
            disabled=(self.member_role is None)
        )
        clear_member_button.callback = self._on_clear_member_role  # type: ignore[assignment]
        self.add_item(clear_member_button)  # type: ignore[arg-type]
    
    def _add_apply_button(self):
        """Add apply button to save changes."""
        from qapbot.i18n import t
        guild_id = self.guild.id if self.guild else None
        
        apply_button = discord.ui.Button(
            label=t('ui_components.role_configuration.button_save', guild_id=guild_id),
            style=discord.ButtonStyle.success,
            custom_id="apply_role_config",
            row=3
        )
        apply_button.callback = self._on_apply  # type: ignore[assignment]
        self.add_item(apply_button)  # type: ignore[arg-type]
    
    def _rebuild_view(self):
        """Rebuild view with updated selections."""
        self.clear_items()
        self._add_newbie_role_select()
        self._add_member_role_select()
        self._add_clear_buttons()
        self._add_apply_button()
    
    async def _on_newbie_role_select(self, interaction: discord.Interaction) -> None:
        """Handle newbie role selection."""
        await interaction.response.defer(thinking=False, ephemeral=False)
        
        selected_roles = interaction.data.get('values', [])  # type: ignore[union-attr]
        if selected_roles:
            role_id = selected_roles[0]
            self.newbie_role = self.guild.get_role(int(role_id))
        
        self._rebuild_view()
        
        from qapbot.i18n import t
        user_id = str(interaction.user.id)
        guild_id_for_t = interaction.guild.id if interaction.guild else None
        newbie_display = self.newbie_role.mention if self.newbie_role else "❌ Not set"
        member_display = self.member_role.mention if self.member_role else "❌ Not set"
        msg = t('ui_components.prompts.configure_roles_updated', 
                user_id=user_id, guild_id=guild_id_for_t,
                newbie_display=newbie_display, member_display=member_display)
        
        await interaction.edit_original_response(
            content=msg,
            view=self
        )
    
    async def _on_member_role_select(self, interaction: discord.Interaction) -> None:
        """Handle member role selection."""
        await interaction.response.defer(thinking=False, ephemeral=False)
        
        selected_roles = interaction.data.get('values', [])  # type: ignore[union-attr]
        if selected_roles:
            role_id = selected_roles[0]
            self.member_role = self.guild.get_role(int(role_id))
        
        self._rebuild_view()
        
        from qapbot.i18n import t
        user_id = str(interaction.user.id)
        guild_id_for_t = interaction.guild.id if interaction.guild else None
        newbie_display = self.newbie_role.mention if self.newbie_role else "❌ Not set"
        member_display = self.member_role.mention if self.member_role else "❌ Not set"
        msg = t('ui_components.prompts.configure_roles_updated', 
                user_id=user_id, guild_id=guild_id_for_t,
                newbie_display=newbie_display, member_display=member_display)
        
        await interaction.edit_original_response(
            content=msg,
            view=self
        )
    
    async def _on_clear_newbie_role(self, interaction: discord.Interaction) -> None:
        """Handle clear newbie role button."""
        await interaction.response.defer(thinking=False, ephemeral=False)
        
        self.newbie_role = None
        self._rebuild_view()
        
        from qapbot.i18n import t
        user_id = str(interaction.user.id)
        guild_id_for_t = interaction.guild.id if interaction.guild else None
        newbie_display = "❌ Cleared"
        member_display = self.member_role.mention if self.member_role else "❌ Not set"
        msg = t('ui_components.prompts.configure_roles_updated', 
                user_id=user_id, guild_id=guild_id_for_t,
                newbie_display=newbie_display, member_display=member_display)
        
        await interaction.edit_original_response(
            content=msg,
            view=self
        )
    
    async def _on_clear_member_role(self, interaction: discord.Interaction) -> None:
        """Handle clear member role button."""
        await interaction.response.defer(thinking=False, ephemeral=False)
        
        self.member_role = None
        self._rebuild_view()
        
        from qapbot.i18n import t
        user_id = str(interaction.user.id)
        guild_id_for_t = interaction.guild.id if interaction.guild else None
        newbie_display = self.newbie_role.mention if self.newbie_role else "❌ Not set"
        member_display = "❌ Cleared"
        msg = t('ui_components.prompts.configure_roles_updated', 
                user_id=user_id, guild_id=guild_id_for_t,
                newbie_display=newbie_display, member_display=member_display)
        
        await interaction.edit_original_response(
            content=msg,
            view=self
        )
    
    async def _on_apply(self, interaction: discord.Interaction) -> None:
        """Apply role configuration changes."""
        await interaction.response.defer(thinking=False, ephemeral=True)
        
        from qapbot.cache_manager import CACHE
        
        guild_id = str(self.guild.id)
        
        # Initialize config if needed
        if guild_id not in CACHE.server_config:
            CACHE.server_config[guild_id] = {}
        
        # Update configuration
        CACHE.server_config[guild_id]["newbie_role_id"] = str(self.newbie_role.id) if self.newbie_role else None
        CACHE.server_config[guild_id]["member_role_id"] = str(self.member_role.id) if self.member_role else None
        
        # Check if role system was enabled and preconditions are lost
        was_enabled = CACHE.server_config[guild_id].get("role_system_enabled", False)
        system_auto_disabled = False
        disabled_reason = None
        
        # Auto-disable role system if preconditions are no longer met
        if was_enabled:
            if not self.newbie_role or not self.member_role:
                CACHE.server_config[guild_id]["role_system_enabled"] = False
                system_auto_disabled = True
                if not self.newbie_role and not self.member_role:
                    disabled_reason = "both newbie and member roles"
                elif not self.newbie_role:
                    disabled_reason = "newbie role"
                else:
                    disabled_reason = "member role"
        
        # Save to file
        await CACHE.persist_server_config(guild_id)
        
        # Send notification if system was auto-disabled
        if system_auto_disabled:
            from qapbot.i18n import t
            user_id = str(interaction.user.id)
            guild_id_for_t = interaction.guild.id if interaction.guild else None
            warning_msg = t('ui_components.messages.role_system_auto_disabled', 
                          user_id=user_id, guild_id=guild_id_for_t, 
                          reason=disabled_reason)
            await interaction.followup.send(warning_msg, ephemeral=True)
        
        # Refresh the clan management view
        from qapbot.QBdiscocmdshelper import format_clan_management_message
        
        try:
            main_embed, _, _, _ = await format_clan_management_message(
                self.clan_management_view.clan_tag,
                self.guild,
                mode="roles"
            )
            
            # Rebuild clan management view
            self.clan_management_view.clear_items()
            self.clan_management_view._add_mode_select()  # type: ignore[attr-defined]
            self.clan_management_view._add_role_management_buttons()  # type: ignore[attr-defined]
            self.clan_management_view._add_refresh_button()  # type: ignore[attr-defined]
            
            await self.clan_management_view.sent_message.edit(
                embeds=[main_embed] if main_embed else [],  # type: ignore[arg-type]
                view=self.clan_management_view
            )
            
            # Delete the configuration message to return to clan management view
            try:
                await self.original_interaction.delete_original_response()
            except Exception:
                pass
                
        except Exception as e:
            from qapbot.i18n import t
            user_id = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            error_msg = t('ui_components.errors.error_saving_configuration', user_id=user_id, guild_id=guild_id, error=str(e))
            await interaction.followup.send(error_msg, ephemeral=True)
            logging.error(f"Error saving role configuration: {e}", exc_info=True)


class AddClanFamilyModal(discord.ui.Modal, title="Add Clan or Family"):
    """Modal for adding a clan or family by entering their tag or name."""
    
    search_input = discord.ui.TextInput(
        label="Clan/Family Tag or Name",
        required=True,
        max_length=50,
        placeholder="Enter tag (e.g., #2C9UR9GJY) or name (e.g., The QCrew)"
    )
    
    def __init__(self, parent_view: 'MemberClansConfigurationView'):
        super().__init__()
        self.parent_view = parent_view
    
    async def on_submit(self, interaction: discord.Interaction):
        """Handle modal submission - search by tag or name and add clan/family."""
        from qapbot.cache_manager import CACHE
        from qapbot.QBdiscocmdshelper import normalize_clan_tag
        
        await interaction.response.defer(thinking=False, ephemeral=True)
        
        search_input = self.search_input.value.strip()
        
        # First, try to normalize as a tag
        normalized_tag = normalize_clan_tag(search_input)
        
        # Check if it's a valid tag and matches a family or clan
        if normalized_tag:
            # Check family by tag
            if normalized_tag in CACHE.clan_families:
                family_id = normalized_tag
                if family_id in self.parent_view.member_families:
                    from qapbot.i18n import t
                    user_id = str(interaction.user.id)
                    guild_id = interaction.guild.id if interaction.guild else None
                    msg = t('ui_components.errors.family_already_added', user_id=user_id, guild_id=guild_id)
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    self.parent_view.member_families.append(family_id)  # type: ignore[arg-type]
                    await self.parent_view._refresh_view()  # type: ignore[attr-defined]
                return
            
            # Check clan by tag
            clan_data = CACHE.clan_name_cache.get(normalized_tag)
            clan_name = None
            if clan_data:
                if isinstance(clan_data, dict):  # type: ignore[misc]
                    clan_name = clan_data.get("name")
                else:
                    clan_name = clan_data  # Old format
            
            if not clan_name:
                # Fetch from CoC API via cache manager
                try:
                    clan = await CACHE.coc_clan_cache.get_clan(normalized_tag)
                    if clan:
                        clan_name = clan.name
                except Exception:
                    pass
            
            if clan_name:
                if normalized_tag in self.parent_view.member_clans:
                    from qapbot.i18n import t
                    user_id = str(interaction.user.id)
                    guild_id = interaction.guild.id if interaction.guild else None
                    msg = t('ui_components.errors.clan_already_added', user_id=user_id, guild_id=guild_id)
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    self.parent_view.member_clans.append(normalized_tag)
                    await self.parent_view._refresh_view()  # type: ignore[attr-defined]
                return
        
        # If not found by tag, search by name substring
        search_lower = search_input.lower()
        matching_families = []
        matching_clans = []
        
        # Search families by name
        for family_id, family_data in CACHE.clan_families.items():
            family_name = family_data.get("name", "").lower()
            if search_lower in family_name:
                matching_families.append((family_id, family_data))
        
        # Search clans by name
        for clan_tag, clan_data in CACHE.clan_name_cache.items():
            # clan_data is a dict with "name" field, extract the name
            clan_name = clan_data.get("name", "") if isinstance(clan_data, dict) else str(clan_data)  # type: ignore[misc]
            if search_lower in clan_name.lower():
                matching_clans.append((clan_tag, clan_name))
        
        # No matches found
        if not matching_families and not matching_clans:
            from qapbot.i18n import t
            user_id = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            error_msg = t('ui_components.errors.clan_not_found', user_id=user_id, guild_id=guild_id)
            await interaction.followup.send(error_msg, ephemeral=True)
            return
        
        # Single family match - add it
        if len(matching_families) == 1 and not matching_clans:  # type: ignore[arg-type]
            family_id, family_data = matching_families[0]
            if family_id in self.parent_view.member_families:
                from qapbot.i18n import t
                user_id = str(interaction.user.id)
                guild_id = interaction.guild.id if interaction.guild else None
                msg = t('ui_components.errors.family_already_added', user_id=user_id, guild_id=guild_id)
                await interaction.followup.send(msg, ephemeral=True)
            else:
                self.parent_view.member_families.append(family_id)  # type: ignore[arg-type]
                await self.parent_view._refresh_view()  # type: ignore[attr-defined]
            return
        
        # Single clan match - add it
        if len(matching_clans) == 1 and not matching_families:  # type: ignore[arg-type]
            clan_tag, clan_name = matching_clans[0]
            if clan_tag in self.parent_view.member_clans:
                from qapbot.i18n import t
                user_id = str(interaction.user.id)
                guild_id = interaction.guild.id if interaction.guild else None
                msg = t('ui_components.errors.clan_already_added', user_id=user_id, guild_id=guild_id)
                await interaction.followup.send(msg, ephemeral=True)
            else:
                self.parent_view.member_clans.append(clan_tag)  # type: ignore[arg-type]
                # Also add to clan_management_view.guild_clans so button appears
                if clan_tag not in self.parent_view.clan_management_view.guild_clans:
                    self.parent_view.clan_management_view.guild_clans.append(clan_tag)  # type: ignore[arg-type]
                await self.parent_view._refresh_view()  # type: ignore[attr-defined]
            return
        
        # Multiple matches - show selection dropdown
        selection_view = SelectClanFamilyDropdownView(
            parent_view=self.parent_view,
            matching_families=matching_families,  # type: ignore[arg-type]
            matching_clans=matching_clans,  # type: ignore[arg-type]
            interaction=interaction
        )
        
        msg = await interaction.followup.send(
            "**Select which clan or family to add:**",
            view=selection_view,
            ephemeral=True
        )
        # Store the message reference for deletion after selection
        selection_view.selection_message = msg


class RoleDeleteConfirmationView(discord.ui.View):
    """Ephemeral confirmation view shown when disabling CoC or clan roles feature.

    Shows Confirm (delete & disable) and Cancel buttons.
    """

    def __init__(
        self,
        feature: str,  # "coc" or "clan"
        clan_management_view: 'ClanManagementView',
        original_interaction: discord.Interaction,
        timeout: int = 120
    ):
        super().__init__(timeout=timeout)
        self.feature = feature
        self.clan_management_view = clan_management_view
        self.original_interaction = original_interaction
        # Set after sending: the WebhookMessage returned by followup.send().
        # Only WebhookMessage.delete() can truly remove an ephemeral followup.
        self.confirmation_message: Optional[discord.WebhookMessage] = None

        from qapbot.i18n import t
        guild_id = original_interaction.guild.id if original_interaction.guild else None

        confirm_btn = discord.ui.Button(
            label=t('ui_components.role_configuration_buttons.button_confirm_delete', guild_id=guild_id),
            style=discord.ButtonStyle.danger,
            custom_id="role_del_confirm",
            row=0
        )
        confirm_btn.callback = self._on_confirm  # type: ignore[assignment]
        self.add_item(confirm_btn)  # type: ignore[arg-type]

        cancel_btn = discord.ui.Button(
            label=t('ui_components.role_configuration_buttons.button_cancel', guild_id=guild_id),
            style=discord.ButtonStyle.secondary,
            custom_id="role_del_cancel",
            row=0
        )
        cancel_btn.callback = self._on_cancel  # type: ignore[assignment]
        self.add_item(cancel_btn)  # type: ignore[arg-type]

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        """Delete roles, disable feature flag, refresh main view."""
        await interaction.response.defer(thinking=False, ephemeral=True)
        # Delete the ephemeral confirmation message via the stored WebhookMessage reference.
        # edit_message() only edits; WebhookMessage.delete() is the only way to truly remove it.
        if self.confirmation_message:
            try:
                await self.confirmation_message.delete()
            except Exception:
                pass

        if not interaction.guild:
            return

        from qapbot.cache_manager import CACHE
        from qapbot import guild_role_manager

        guild_id = str(interaction.guild.id)

        if self.feature == "coc":
            # Set flag False BEFORE deleting so the internal persist inside
            # delete_all_coc_ingame_roles already writes the correct state to DB.
            if guild_id in CACHE.server_config:
                CACHE.server_config[guild_id]["coc_role_enabled"] = False
            await guild_role_manager.delete_all_coc_ingame_roles(interaction.guild, guild_id)
        else:
            if guild_id in CACHE.server_config:
                CACHE.server_config[guild_id]["clan_role_enabled"] = False
            await guild_role_manager.delete_all_clan_roles(interaction.guild, guild_id)

        self.stop()
        await self.clan_management_view._refresh_roles_view(interaction)  # type: ignore[misc]

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        """Dismiss the confirmation without making any changes."""
        self.stop()
        await interaction.response.defer(thinking=False, ephemeral=True)
        if self.confirmation_message:
            try:
                await self.confirmation_message.delete()
            except Exception:
                pass


class SelectClanFamilyDropdownView(discord.ui.View):
    """View with dropdown selector for multiple clan/family matches."""
    
    def __init__(self, parent_view: 'MemberClansConfigurationView', matching_families: List[Any], matching_clans: List[Any], interaction: discord.Interaction, timeout: int = 300):  # type: ignore[type-arg]
        super().__init__(timeout=timeout)
        self.parent_view = parent_view
        self.matching_families = matching_families
        self.matching_clans = matching_clans
        self.interaction = interaction
        self.selection_message = None
        
        # Build select options - up to 25 total
        options = []
        
        # Add family options (up to 12)
        for family_id, family_data in matching_families[:12]:
            family_name = family_data.get("name", "Unknown")
            option = discord.SelectOption(
                label=f"🏰 {family_name}",
                value=f"family_{family_id}"
            )
            options.append(option)
        
        # Add clan options (remaining slots, up to 13)
        remaining_slots = 25 - len(options)  # type: ignore[arg-type]
        for clan_tag, clan_name in matching_clans[:remaining_slots]:
            option = discord.SelectOption(
                label=f"🏯 {clan_name} ({clan_tag})",
                value=f"clan_{clan_tag}"
            )
            options.append(option)
        
        if options:
            select = discord.ui.Select(
                placeholder="Select a clan or family to add...",
                min_values=1,
                max_values=1,
                options=options,  # type: ignore[arg-type]
                custom_id="select_clan_family_dropdown"
            )
            select.callback = self._on_select  # type: ignore[assignment]
            self.add_item(select)  # type: ignore[arg-type]
    
    async def _on_select(self, interaction: discord.Interaction):
        """Handle selection - add the selected clan/family and refresh."""
        await interaction.response.defer(thinking=False, ephemeral=True)
        
        selected_value = interaction.data['values'][0]  # type: ignore[index]
        
        try:
            if selected_value.startswith("family_"):
                family_id = selected_value.replace("family_", "")
                if family_id not in self.parent_view.member_families:
                    self.parent_view.member_families.append(family_id)  # type: ignore[arg-type]
            elif selected_value.startswith("clan_"):
                clan_tag = selected_value.replace("clan_", "")
                if clan_tag not in self.parent_view.member_clans:
                    self.parent_view.member_clans.append(clan_tag)  # type: ignore[arg-type]
                # Also add to clan_management_view.guild_clans (single source of truth)
                if clan_tag not in self.parent_view.clan_management_view.guild_clans:
                    self.parent_view.clan_management_view.guild_clans.append(clan_tag)  # type: ignore[arg-type]
            
            # Delete the selection message
            if self.selection_message:
                try:
                    await self.selection_message.delete()  # type: ignore[misc]
                except Exception as e:
                    logging.debug(f"Could not delete selection message: {e}")
            
            # Refresh the main configuration view
            await self.parent_view._refresh_view()  # type: ignore[attr-defined]
            
        except Exception as e:
            logging.error(f"Error selecting clan/family: {e}", exc_info=True)
            await interaction.followup.send(t('ui_components.errors.error_adding_selection', guild_id=self.parent_view.guild.id), ephemeral=True)


async def _handle_clan_role_changes_for_guild(
    guild: discord.Guild,
    guild_id: str,
    old_coverage: "set[str]",
    new_coverage: "set[str]",
) -> "set[str]":
    """
    Shared helper: reconcile Discord clan roles after any change to which clans a
    guild covers (individual clans or family membership).

    - Creates roles for newly covered clans.
    - Triggers a guild-wide role sync when coverage changed.
    - Returns ``removable_tags``: clans that were covered before but no longer are,
      AND that actually have a stored Discord role (so the caller can prompt the admin).

    Args:
        guild:        Discord guild object.
        guild_id:     Guild ID string.
        old_coverage: Full set of clan tags covered *before* the change.
        new_coverage: Full set of clan tags covered *after* the change.
    """
    from qapbot import guild_role_manager
    from qapbot.cache_manager import CACHE

    config = CACHE.server_config.get(guild_id, {})
    if not config.get("clan_role_enabled", False):
        return set()

    added = new_coverage - old_coverage
    removed = old_coverage - new_coverage

    # Create roles in alphabetical order by clan name for a predictable Discord role list
    for clan_tag in sorted(added, key=lambda tag: (CACHE.get_clan_name(tag, tag) or tag).lower()):  # type: ignore[arg-type]
        clan_name = CACHE.get_clan_name(clan_tag, clan_tag)  # type: ignore[arg-type]
        await guild_role_manager.create_clan_role(guild, guild_id, clan_tag, clan_name or clan_tag)
        logging.info(f"[ROLES] Auto-created clan role for {clan_tag} in guild {guild_id}")

    if added or removed:
        logging.info(f"[ROLES] Triggering guild-wide role sync after family changes in guild {guild_id}")
        await guild_role_manager.sync_all_roles_for_guild(guild, guild_id)

    # Only surface tags that actually have a stored Discord role (skip uncreated ones)
    return {
        tag for tag in removed
        if config.get("clan_roles", {}).get(tag)
    }


class ConfirmDeleteClanRolesView(discord.ui.View):
    """
    Ephemeral confirmation view shown when clans are removed from a guild's member list.

    Asks the admin whether the associated Discord roles should be deleted or kept.
    Shown as a follow-up to MemberClansConfigurationView._on_apply().

    Args:
        guild: The Discord guild.
        guild_id: Guild ID string.
        removed_tags: Set of clan tags whose roles may need to be deleted.
        role_names: Human-readable clan names for display.
    """

    def __init__(
        self,
        guild: discord.Guild,
        guild_id: str,
        removed_tags: set,  # type: ignore[type-arg]
        role_names: str,
        timeout: int = 120,
    ) -> None:
        super().__init__(timeout=timeout)
        self.guild = guild
        self.guild_id = guild_id
        self.removed_tags = removed_tags
        self.role_names = role_names
        self._msg: Optional[discord.Message] = None

        from qapbot.i18n import t
        delete_btn = discord.ui.Button(
            label=t('ui_components.role_configuration_buttons.button_delete_roles', guild_id=int(guild_id)),  # type: ignore[arg-type]
            style=discord.ButtonStyle.danger,
            custom_id="confirm_delete_clan_roles_yes",
        )
        delete_btn.callback = self._on_delete  # type: ignore[assignment]
        self.add_item(delete_btn)  # type: ignore[arg-type]

        keep_btn = discord.ui.Button(
            label=t('ui_components.role_configuration_buttons.button_keep_roles', guild_id=int(guild_id)),  # type: ignore[arg-type]
            style=discord.ButtonStyle.secondary,
            custom_id="confirm_delete_clan_roles_no",
        )
        keep_btn.callback = self._on_keep  # type: ignore[assignment]
        self.add_item(keep_btn)  # type: ignore[arg-type]

    async def _on_delete(self, interaction: discord.Interaction) -> None:
        """Delete the Discord roles for all removed clans."""
        await interaction.response.defer(thinking=False, ephemeral=True)
        from qapbot import guild_role_manager
        deleted: list[str] = []
        failed: list[str] = []
        for clan_tag in self.removed_tags:
            try:
                ok = await guild_role_manager.delete_clan_role_from_guild(
                    self.guild, self.guild_id, clan_tag
                )
                (deleted if ok else failed).append(clan_tag)
            except Exception as e:
                logging.error(f"[ROLES] Failed to delete role for {clan_tag}: {e}")
                failed.append(clan_tag)
        if failed:
            from qapbot.cache_manager import CACHE
            fail_names = ", ".join(CACHE.get_clan_name(tag, tag) for tag in failed)  # type: ignore[arg-type]
            logging.warning(f"[ROLES] Could not delete clan role(s) for: {fail_names} in guild {self.guild_id}")
        self.stop()
        if self._msg:
            try:
                await self._msg.delete()
            except Exception:
                pass

    async def _on_keep(self, interaction: discord.Interaction) -> None:
        """Dismiss and inform the admin roles were kept."""
        await interaction.response.defer(ephemeral=True)
        self.stop()
        if self._msg:
            try:
                await self._msg.delete()
            except Exception:
                pass
        from qapbot.i18n import t
        await interaction.followup.send(
            t('ui_components.role_configuration_buttons.removed_clan_roles_kept',
              guild_id=int(self.guild_id),  # type: ignore[arg-type]
              role_names=self.role_names),
            ephemeral=True,
        )


class MemberClansConfigurationView(discord.ui.View):
    """
    View for configuring which clans and families grant the member role upon verification.
    
    Provides:
    - Buttons to toggle clan families for member role
    - Buttons to toggle individual clans for member role
    - Clear all button to remove all selections
    - Apply button to save changes
    
    Member role is granted when a user verifies with a player in:
    - Any clan from a selected family, OR
    - Any individually selected clan
    
    Args:
        guild: Discord guild object
        clan_management_view: Parent ClanManagementView to refresh after save
        original_interaction: The interaction that triggered the configuration (for cleanup)
        current_member_clans: Currently configured individual member clans list
        current_member_families: Currently configured member families list
        clan_families: Dict of clan families {family_id: {"name": str, "clans": List[str]}}
        timeout: View timeout in seconds
    """
    def __init__(
        self,
        guild: discord.Guild,
        clan_management_view: 'ClanManagementView',
        original_interaction: discord.Interaction,
        current_member_clans: List[str],
        current_member_families: List[str],
        clan_families: Dict[str, Dict[str, Any]],
        timeout: int = 300
    ):
        super().__init__(timeout=timeout)
        self.guild = guild
        self.clan_management_view = clan_management_view
        self.original_interaction = original_interaction
        self.clan_families = clan_families
        self.sent_message = None  # Store reference to the message this view is attached to
        
        # Working copy of member clans and families (start with current values)
        self.member_clans: List[str] = current_member_clans.copy()
        self.member_families: List[str] = current_member_families.copy()
        
        # Add UI components
        self._add_family_buttons()
        self._add_clan_buttons()
        self._add_control_buttons()
    
    def _add_family_buttons(self):
        """Add buttons for each clan family to toggle entire family membership."""
        from qapbot.cache_manager import CACHE
        
        # Use only available families (passed during initialization)
        # These are filtered to show only families that have subscriptions or member clans
        all_families_to_show = dict(self.clan_families)
        
        # Also include any selected member families (in case they were deselected from subscriptions)
        for family_id in self.member_families:
            if family_id not in all_families_to_show and family_id in CACHE.clan_families:
                all_families_to_show[family_id] = CACHE.clan_families[family_id]
        
        if not all_families_to_show:
            return
        
        row = 0
        for family_id, family_data in list(all_families_to_show.items())[:5]:  # Limit to 5 families
            family_name = family_data.get("name", "Unknown Family")
            
            # Check if family is in member_families
            family_selected = family_id in self.member_families
            
            # Truncate family name if too long
            display_name = family_name[:25] + "..." if len(family_name) > 25 else family_name
            
            button = discord.ui.Button(
                label=f"{display_name}",
                emoji="🏰" if family_selected else "🏯",
                style=discord.ButtonStyle.success if family_selected else discord.ButtonStyle.secondary,
                custom_id=f"family_{family_id}",
                row=row,
                disabled=False  # Always enabled for toggling
            )
            button.callback = self._create_family_toggle_callback(family_id)  # type: ignore[assignment]
            self.add_item(button)  # type: ignore[arg-type]
            
            row = (row + 1) % 4  # Use rows 0-3 for families/clans
    
    def _create_family_toggle_callback(self, family_id: str):
        """Create callback function for toggling entire clan family."""
        async def callback(interaction: discord.Interaction):
            await self._on_toggle_family(interaction, family_id)
        return callback
    
    def _add_clan_buttons(self):
        """Add buttons for each available clan to toggle membership."""
        from qapbot.cache_manager import CACHE
        
        # Use guild_clans from parent ClanManagementView (single source of truth),
        # sorted alphabetically by clan name (not tag) for a stable, readable order
        clans_to_show = sorted(
            self.clan_management_view.guild_clans,
            key=lambda tag: (CACHE.get_clan_name(tag, tag) or tag).lower()  # type: ignore[arg-type]
        )
        
        # Calculate how many clan buttons we can show (Discord limit is 25 total)
        # Reserve buttons for control buttons, account for family buttons
        family_button_count = min(len(CACHE.clan_families), 5)
        control_button_count = 3  # Clear All, Apply, Add by Tag
        max_clan_buttons = 25 - control_button_count - family_button_count
        
        clans_to_show = clans_to_show[:max_clan_buttons]
        
        for idx, clan_tag in enumerate(clans_to_show):
            clan_name = CACHE.get_clan_name(clan_tag, "Unknown")  # type: ignore[arg-type]
            is_member_clan = clan_tag in self.member_clans
            
            # Truncate clan name if too long
            display_name = (clan_name[:30] + "...") if (clan_name and len(clan_name) > 30) else (clan_name or "Unknown")
            
            button = discord.ui.Button(
                label=f"{display_name}",
                emoji="✅" if is_member_clan else "➕",
                style=discord.ButtonStyle.success if is_member_clan else discord.ButtonStyle.secondary,
                custom_id=f"member_clan_{clan_tag}",
                row=min(idx // 4, 3),  # Distribute across rows 0-3 (4 rows for clans)
                disabled=False  # Always enabled for toggling
            )
            button.callback = self._create_toggle_callback(clan_tag)  # type: ignore[assignment]
            self.add_item(button)  # type: ignore[arg-type]
    
    def _create_toggle_callback(self, clan_tag: str):
        """Create callback function for toggling clan membership."""
        async def callback(interaction: discord.Interaction):
            await self._on_toggle_clan(interaction, clan_tag)
        return callback
    
    def _add_control_buttons(self):
        """Add Clear All, Apply and Add by Tag buttons."""
        from qapbot.i18n import t
        guild_id = self.guild.id if self.guild else None
        
        # Clear all button
        clear_button = discord.ui.Button(
            label=t('ui_components.member_clans_config.button_clear_all', guild_id=guild_id),
            emoji="🗑️",
            style=discord.ButtonStyle.danger,
            custom_id="clear_all_clans",
            row=4
        )
        clear_button.callback = self._on_clear_all  # type: ignore[assignment]
        self.add_item(clear_button)  # type: ignore[arg-type]
        
        # Apply button
        apply_button = discord.ui.Button(
            label=t('ui_components.member_clans_config.button_apply', guild_id=guild_id),
            emoji="💾",
            style=discord.ButtonStyle.primary,
            custom_id="apply_clans",
            row=4
        )
        apply_button.callback = self._on_apply  # type: ignore[assignment]
        self.add_item(apply_button)  # type: ignore[arg-type]
        
        # Add Clan/Family button
        add_button = discord.ui.Button(
            label="Add Clan/Family",
            emoji="➕",
            style=discord.ButtonStyle.secondary,
            custom_id="add_clan_family",
            row=4
        )
        add_button.callback = self._on_add_clan_family  # type: ignore[assignment]
        self.add_item(add_button)  # type: ignore[arg-type]
    
    async def _on_add_clan_family(self, interaction: discord.Interaction):
        """Handle Add Clan/Family button - open modal for searching clan/family by tag or name."""
        modal = AddClanFamilyModal(self)
        await interaction.response.send_modal(modal)
    
    async def _on_toggle_family(self, interaction: discord.Interaction, family_id: str):
        """Toggle an entire family's membership status."""
        await interaction.response.defer(thinking=False, ephemeral=False)
        
        if family_id in self.member_families:
            self.member_families.remove(family_id)
        else:
            self.member_families.append(family_id)  # type: ignore[arg-type]
        
        # Rebuild view with updated states
        await self._refresh_view()  # type: ignore[attr-defined]
    
    async def _on_toggle_clan(self, interaction: discord.Interaction, clan_tag: str):
        """Toggle a clan's membership status."""
        await interaction.response.defer(thinking=False, ephemeral=False)
        
        if clan_tag in self.member_clans:
            self.member_clans.remove(clan_tag)
        else:
            self.member_clans.append(clan_tag)  # type: ignore[arg-type]
        
        # Rebuild view with updated states
        await self._refresh_view()  # type: ignore[attr-defined]
    
    async def _on_clear_all(self, interaction: discord.Interaction):
        """Clear all member clans and families."""
        await interaction.response.defer(thinking=False, ephemeral=False)
        
        self.member_clans = []
        self.member_families = []
        await self._refresh_view()  # type: ignore[attr-defined]
    
    async def _refresh_view(self):
        """Refresh the view to show updated clan and family states."""
        from qapbot.cache_manager import CACHE
        
        # Rebuild view
        self.clear_items()
        self._add_family_buttons()
        self._add_clan_buttons()
        self._add_control_buttons()
        
        # Build updated display
        display_parts = []
        
        # Show selected families
        if self.member_families:
            family_list = []
            for family_id in self.member_families[:5]:
                # Fetch from CACHE to get families added by exact tag
                family_data = CACHE.clan_families.get(family_id, {})
                family_name = family_data.get("name", "Unknown")
                clan_count = len(family_data.get("clans", []))  # type: ignore[arg-type]
                family_list.append(f"🏰 {family_name} ({clan_count} clans)")
            
            display_parts.append(f"**Families ({len(self.member_families)}):**\n" + "\n".join(family_list))  # type: ignore[arg-type]
        
        # Show selected clans
        if self.member_clans:
            clan_list = [f"• {CACHE.get_clan_name(tag, 'Unknown')} ({tag})" for tag in self.member_clans]  # type: ignore[arg-type]
            
            display_parts.append(f"**Individual Clans ({len(self.member_clans)}):**\n" + "\n".join(clan_list))  # type: ignore[arg-type]
        
        if not display_parts:
            display_text = t('ui_components.errors.error_no_clans_selected', guild_id=self.guild.id)
        else:
            display_text = "\n\n".join(display_parts)  # type: ignore[arg-type]
        
        # Edit the message directly instead of using interaction response
        # This allows _refresh_view to be called from deferred interactions
        if not self.sent_message:
            return
        await self.sent_message.edit(  # type: ignore[misc]
            content=(
                "**Configure Member Clans & Families**\n\n"
                "Select entire clan families or individual clans to grant the member role.\n\n"
                f"{display_text}\n\n"
                "🏰 = Family selected | 🏯 = Family not selected | ✅ = Clan selected | Click **Apply Changes** when done."
            ),
            view=self
        )
    
    async def _on_apply(self, interaction: discord.Interaction):
        """Apply changes and save to cache."""
        await interaction.response.defer(thinking=False, ephemeral=True)
        
        from qapbot.cache_manager import CACHE
        from qapbot.QBdiscocmdshelper import format_clan_management_message
        
        if not interaction.guild:
            return
        
        try:
            guild_id = str(interaction.guild.id)
            
            # Capture old clan/family lists before updating (for clan-role sync)
            old_member_clans: List[str] = list(CACHE.server_config.get(guild_id, {}).get("member_clans", []))
            old_member_families: List[str] = list(CACHE.server_config.get(guild_id, {}).get("member_families", []))

            # Update cache
            if guild_id not in CACHE.server_config:
                CACHE.server_config[guild_id] = {
                    'role_system_enabled': False,
                    'newbie_role_id': None,
                    'member_role_id': None,
                    'member_clans': [],
                    'member_families': []
                }
            
            CACHE.server_config[guild_id]["member_clans"] = self.member_clans
            CACHE.server_config[guild_id]["member_families"] = self.member_families
            
            # Set owned_by_guild for any families without an owner (first guild to use becomes owner)
            for family_id in self.member_families:
                if family_id in CACHE.clan_families:
                    if "owned_by_guild" not in CACHE.clan_families[family_id] or not CACHE.clan_families[family_id].get("owned_by_guild"):
                        CACHE.clan_families[family_id]["owned_by_guild"] = guild_id
                        await CACHE.persist_clan_family(family_id)
            
            # Save any new clans that were added by tag to the clan_name_cache
            for clan_tag in self.member_clans:
                if clan_tag not in CACHE.clan_name_cache:
                    # Fetch and cache the clan data with proper metadata
                    try:
                        clan = await CACHE.coc_clan_cache.get_clan(clan_tag)
                        if clan:
                            CACHE.clan_name_cache[clan_tag] = {  # type: ignore[assignment]
                                "name": clan.name,
                                "has_active_subscriptions": False,
                                "last_war_update": None,
                                "warlog_is_public": getattr(clan, 'is_war_log_public', True)  # type: ignore[arg-type]
                            }
                            warlog_status = getattr(clan, 'is_war_log_public', True)  # type: ignore[arg-type]
                            logging.info(f"[ADD-CLAN] Added {clan.name} ({clan_tag}) to cache - warlog_is_public: {warlog_status}")
                    except Exception as e:
                        # If fetch fails, use unknown as placeholder with default metadata
                        logging.warning(f"[ADD-CLAN] Failed to fetch clan {clan_tag}: {e}")
                        CACHE.clan_name_cache[clan_tag] = {  # type: ignore[assignment]
                            "name": "Unknown",
                            "has_active_subscriptions": False,
                            "last_war_update": None,
                            "warlog_is_public": True
                        }
            
            # Save caches
            for clan_tag in self.member_clans:  # type: ignore[union-attr]
                if clan_tag in CACHE.clan_name_cache:
                    await CACHE.persist_clan(clan_tag)
            
            # Check if role system was enabled and preconditions are lost
            was_enabled = CACHE.server_config[guild_id].get("role_system_enabled", False)
            system_auto_disabled = False
            
            # Auto-disable role system if preconditions are no longer met
            if was_enabled:
                if not self.member_clans and not self.member_families:
                    CACHE.server_config[guild_id]["role_system_enabled"] = False
                    system_auto_disabled = True
            
            # Save to disk
            await CACHE.persist_server_config(guild_id)

            # ---- Clan-role feature hooks ----
            clan_role_enabled = CACHE.server_config[guild_id].get("clan_role_enabled", False)
            if clan_role_enabled and interaction.guild:
                from qapbot import guild_role_manager
                new_clans_set = set(self.member_clans)
                old_clans_set = set(old_member_clans)
                new_families_set = set(self.member_families)
                old_families_set = set(old_member_families)

                roles_created = False

                # Create Discord role for any newly added individual clan, alphabetically by name
                newly_added_clans = sorted(
                    new_clans_set - old_clans_set,
                    key=lambda tag: (CACHE.get_clan_name(tag, tag) or tag).lower()  # type: ignore[arg-type]
                )
                for added_tag in newly_added_clans:
                    clan_name = CACHE.get_clan_name(added_tag, added_tag)  # type: ignore[arg-type]
                    await guild_role_manager.create_clan_role(interaction.guild, guild_id, added_tag, clan_name or added_tag)
                    logging.info(f"[ROLES] Auto-created clan role for {added_tag} in guild {guild_id}")
                    roles_created = True

                # Create Discord roles for clans in any newly added family, alphabetically by name
                for added_family_id in new_families_set - old_families_set:
                    family_clans = sorted(
                        CACHE.clan_families.get(added_family_id, {}).get("clans", []),
                        key=lambda tag: (CACHE.get_clan_name(tag, tag) or tag).lower()  # type: ignore[arg-type]
                    )
                    for clan_tag in family_clans:
                        clan_name = CACHE.get_clan_name(clan_tag, clan_tag)  # type: ignore[arg-type]
                        await guild_role_manager.create_clan_role(interaction.guild, guild_id, clan_tag, clan_name or clan_tag)
                        logging.info(f"[ROLES] Auto-created clan role for {clan_tag} (family {added_family_id}) in guild {guild_id}")
                        roles_created = True

                # Immediately assign new roles to all registered guild members
                if roles_created or new_clans_set != old_clans_set or new_families_set != old_families_set:
                    logging.info(f"[ROLES] Triggering guild-wide role sync after member-clan changes in guild {guild_id}")
                    await guild_role_manager.sync_all_roles_for_guild(interaction.guild, guild_id)

                # Ask admin whether to delete Discord roles for removed clans.
                # Build the set of clans still reachable via the new config
                # (individual list OR any kept family) — these must never lose their role.
                still_covered: set[str] = set(new_clans_set)
                for kept_family_id in new_families_set:
                    still_covered.update(CACHE.clan_families.get(kept_family_id, {}).get("clans", []))

                # 1. Clans individually removed AND no longer covered by any family
                removed_tags: set[str] = (old_clans_set - new_clans_set) - still_covered
                # 2. Clans that were only in a removed family and are not covered elsewhere
                for removed_family_id in old_families_set - new_families_set:
                    for clan_tag in CACHE.clan_families.get(removed_family_id, {}).get("clans", []):
                        if clan_tag not in still_covered:
                            removed_tags.add(clan_tag)
                # Only prompt for tags that actually have a Discord role stored
                removable_tags = {
                    tag for tag in removed_tags
                    if CACHE.server_config.get(guild_id, {}).get("clan_roles", {}).get(tag)
                }
                if removable_tags:
                    removed_lines = "\n".join(
                        f"• {CACHE.get_clan_name(t, t)}"  # type: ignore[arg-type]
                        for t in removable_tags
                    )
                    confirm_view = ConfirmDeleteClanRolesView(
                        guild=interaction.guild,
                        guild_id=guild_id,
                        removed_tags=removable_tags,
                        role_names=removed_lines,
                    )
                    from qapbot.i18n import t as _t
                    confirm_msg = await interaction.followup.send(
                        _t('ui_components.role_configuration_buttons.removed_clan_roles_prompt',
                           guild_id=int(guild_id),  # type: ignore[arg-type]
                           role_names=removed_lines),
                        view=confirm_view,
                        ephemeral=True,
                        wait=True,
                    )
                    confirm_view._msg = confirm_msg  # type: ignore[misc]
            # ---- end clan-role hooks ----

            # Only send message if role system was auto-disabled
            if system_auto_disabled:
                from qapbot.i18n import t
                guild_id = self.clan_management_view.sent_message.guild.id if self.clan_management_view.sent_message.guild else None
                warning_msg = t('ui_components.errors.member_role_system_disabled', guild_id=guild_id)
                await interaction.followup.send(warning_msg, ephemeral=True)
            
            # Refresh parent view
            if not self.clan_management_view.sent_message or not self.clan_management_view.sent_message.guild:
                return
            try:
                main_embed, _, _, _ = await format_clan_management_message(
                    self.clan_management_view.clan_tag,
                    self.clan_management_view.sent_message.guild,
                    mode="config"  # Return to Basic Configuration
                )
                
                self.clan_management_view.clear_items()
                self.clan_management_view._add_mode_select()  # type: ignore[attr-defined]
                self.clan_management_view._add_basic_config_components()  # type: ignore[attr-defined]
                self.clan_management_view._add_refresh_button()  # type: ignore[attr-defined]
                
                await self.clan_management_view.sent_message.edit(
                    embeds=[main_embed] if main_embed else [],  # type: ignore[arg-type]
                    view=self.clan_management_view
                )
            except Exception as e:
                logging.error(f"Error refreshing clan management view after member clans update: {e}", exc_info=True)
            
            # Delete the configuration message to return to clan management view
            try:
                if self.sent_message:
                    await self.sent_message.delete()  # type: ignore[misc]
            except Exception:
                pass
                
        except Exception as e:
            from qapbot.i18n import t
            user_id = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            error_msg = t('ui_components.errors.error_saving_configuration', user_id=user_id, guild_id=guild_id)
            await interaction.followup.send(error_msg, ephemeral=True)
            logging.error(f"Error saving member clans configuration: {e}", exc_info=True)


class CreateFamilyModal(discord.ui.Modal, title="Create Clan Family"):
    """Modal for creating a new clan family with a name."""
    
    family_name = discord.ui.TextInput(
        label="Family Name",
        required=True,
        max_length=50,
        placeholder="Enter a name for this clan family..."
    )
    
    def __init__(self, clan_management_view: 'ClanManagementView'):
        super().__init__()
        self.clan_management_view = clan_management_view
        
        # Translate placeholder
        from qapbot.i18n import t
        guild_id = getattr(clan_management_view.sent_message, 'guild', None)  # type: ignore[arg-type]
        guild_id = guild_id.id if guild_id else None
        self.family_name.placeholder = t('ui_components.modals.placeholder_family_name', guild_id=guild_id)
    
    async def on_submit(self, interaction: discord.Interaction):
        """Handle modal submission - create family and open edit view."""
        from qapbot.cache_manager import CACHE
        from qapbot.QBdiscocmdshelper import generate_family_tag
        
        family_name = self.family_name.value.strip()
        
        if not family_name:
            from qapbot.i18n import t
            user_id = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            error_msg = t('ui_components.errors.family_name_empty', user_id=user_id, guild_id=guild_id)
            await interaction.response.send_message(error_msg, ephemeral=True)
            return
        
        # Generate family ID
        family_id = generate_family_tag(family_name, [])
        
        # Check if family ID already exists (collision check)
        if family_id in CACHE.clan_families:
            from qapbot.i18n import t
            user_id = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            msg = t('ui_components.errors.family_name_duplicate', user_id=user_id, guild_id=guild_id, family_id=family_id)
            await interaction.response.send_message(
                msg,
                ephemeral=True
            )
            return
        
        if not interaction.guild:
            return
        
        # Create family with empty clan list and owner guild
        current_guild_id = str(interaction.guild.id)
        await CACHE.set_clan_family(family_id, {
            "name": family_name,
            "clans": [],
            "owned_by_guild": current_guild_id
        })
        
        # Auto-attach to current guild
        guild_id = str(interaction.guild.id)
        if guild_id not in CACHE.server_config:
            CACHE.server_config[guild_id] = {
                'role_system_enabled': False,
                'newbie_role_id': None,
                'member_role_id': None,
                'member_clans': [],
                'member_families': []
            }
        
        member_families = CACHE.server_config[guild_id].get('member_families', [])
        if family_id not in member_families:
            member_families.append(family_id)  # type: ignore[arg-type]
            CACHE.server_config[guild_id]['member_families'] = member_families
            await CACHE.persist_server_config(guild_id)
        
        logging.info(f"Created clan family '{family_name}' (ID: {family_id}) and attached to guild {guild_id}")
        
        # Open edit view (interaction.guild already checked above)
        edit_view = EditFamilyView(  # type: ignore[name-defined]
            guild=interaction.guild,  # type: ignore[arg-type]
            clan_management_view=self.clan_management_view,
            family_id=family_id,
            family_data=CACHE.clan_families[family_id],
            timeout=300
        )
        
        await edit_view.show_edit_interface(interaction)


class RenameFamilyModal(discord.ui.Modal, title="Rename Clan Family"):
    """Modal for renaming an existing clan family."""
    
    family_name = discord.ui.TextInput(
        label="New Family Name",
        required=True,
        max_length=50,
        placeholder="Enter a new name for this clan family..."
    )
    
    def __init__(self, edit_family_view: 'EditFamilyView', current_name: str):
        super().__init__()
        self.edit_family_view = edit_family_view
        self.family_name.default = current_name
        
        # Translate placeholder
        from qapbot.i18n import t
        guild_id = getattr(edit_family_view, 'guild_id', None)  # type: ignore[arg-type]
        self.family_name.placeholder = t('ui_components.modals.placeholder_new_family_name', guild_id=guild_id)
    
    async def on_submit(self, interaction: discord.Interaction):
        """Handle modal submission - update family name and refresh view."""
        new_name = self.family_name.value.strip()
        
        if not new_name:
            from qapbot.i18n import t
            user_id = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            error_msg = t('ui_components.errors.family_name_empty', user_id=user_id, guild_id=guild_id)
            await interaction.response.send_message(error_msg, ephemeral=True)
            return
        
        # Update the family name in the edit view
        self.edit_family_view.family_name = new_name
        
        # Refresh the edit view to show new name
        await self.edit_family_view._refresh_view(interaction)  # type: ignore[attr-defined]


class AddClanModal(discord.ui.Modal, title="Add Clan to Family"):
    """Modal for adding a clan by name or tag."""
    
    clan_search = discord.ui.TextInput(
        label="Clan Name (substring) or Tag (complete)",
        required=True,
        max_length=50,
        placeholder="Enter clan name or tag (e.g., 'Dark' or '#2C9UR9GJY')"
    )
    
    def __init__(self, parent_view: Any):  # type: ignore[misc]
        """
        Initialize add clan modal.
        
        Args:
            parent_view: EditFamilyView or MemberClansConfigurationView
        """
        super().__init__()
        self.parent_view = parent_view
        
        # Translate placeholder
        from qapbot.i18n import t
        guild_id = getattr(parent_view, 'guild_id', None)  # type: ignore[arg-type]
        self.clan_search.placeholder = t('ui_components.modals.placeholder_clan_search', guild_id=guild_id)
    
    async def on_submit(self, interaction: discord.Interaction):
        """Handle modal submission - search for clan and add it."""
        search_input = self.clan_search.value.strip()
        
        if not search_input:
            from qapbot.i18n import t
            user_id = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            error_msg = t('ui_components.errors.search_input_empty', user_id=user_id, guild_id=guild_id)
            await interaction.response.send_message(error_msg, ephemeral=True)
            return
        
        # Use the helper function to find and add the clan
        result = await add_clan_by_search(search_input, self.parent_view, interaction)
        
        if result["success"]:
            # Check if we need to add matches as buttons (Requirement 4)
            if result.get("add_as_buttons") and result.get("matches"):
                # Add matched clans as grey buttons to the view
                matches = result["matches"]
                logging.debug(f"Adding {len(matches)} matched clans as buttons")
                
                # Respond to interaction to close modal (no message shown)
                await interaction.response.defer(ephemeral=True)
                
                # Don't rebuild - just add matched buttons to existing view (preserves button order and state)
                existing_custom_ids = {
                    item.custom_id
                    for item in self.parent_view.children
                    if hasattr(item, 'custom_id') and getattr(item, 'custom_id', None)
                }
                added_button_count = 0
                duplicate_matches: list[tuple[str, str]] = []

                for clan_tag, clan_name in matches:
                    custom_id = f"family_clan_{clan_tag}"

                    # Skip if this clan button already exists in the view
                    if custom_id in existing_custom_ids:
                        duplicate_matches.append((clan_tag, clan_name))
                        continue

                    button = discord.ui.Button(
                        label=f"{clan_name[:30]}",  # Truncate if too long
                        emoji="➕",  # Use emoji parameter like regular clan buttons
                        style=discord.ButtonStyle.secondary,
                        custom_id=custom_id  # Same custom_id as regular clan buttons
                    )
                    # Reuse the same toggle callback - no duplicate code needed
                    button.callback = self.parent_view._create_toggle_callback(clan_tag)  # type: ignore[assignment]
                    self.parent_view.add_item(button)
                    existing_custom_ids.add(custom_id)
                    added_button_count += 1

                # Follow-up hint when search produced no new selectable buttons
                if added_button_count == 0 and duplicate_matches:
                    normalized_search = search_input.strip().lower().removeprefix('#')
                    search_matches_existing = any(
                        normalized_search in clan_name.lower()
                        or normalized_search == clan_tag.lower().removeprefix('#')
                        for clan_tag, clan_name in duplicate_matches
                    )

                    if not search_matches_existing:
                        return

                    from qapbot.i18n import t
                    user_id = str(interaction.user.id)
                    guild_id = interaction.guild.id if interaction.guild else None
                    list_name = "family" if hasattr(self.parent_view, 'family_clans') else "member clans"
                    hint = t('ui_components.errors.all_matches_already_added', user_id=user_id, guild_id=guild_id, list_name=list_name)
                    await interaction.followup.send(hint, ephemeral=True)
                    return
                
                # Update parent view message (don't rebuild buttons - preserves existing order)
                from qapbot.cache_manager import CACHE
                from qapbot.i18n import t
                guild_id = interaction.guild_id
                
                if self.parent_view.family_clans:
                    clan_list = "\n".join([  # type: ignore[arg-type]
                        f"• {CACHE.get_clan_name(tag, 'Unknown')} ({tag})" 
                        for tag in self.parent_view.family_clans[:10]
                    ])
                    clan_count = len(self.parent_view.family_clans)  # type: ignore[arg-type]
                    if clan_count > 10:
                        clan_list += f"\n*... and {clan_count - 10} more*"
                else:
                    clan_list = t('ui_components.family_management.no_clans_empty', guild_id=guild_id)
                
                content = t('ui_components.family_management.edit_family_content', guild_id=guild_id,
                           family_name=self.parent_view.family_name,
                           family_id=self.parent_view.family_id,
                           clan_count=len(self.parent_view.family_clans),  # type: ignore[arg-type]
                           clan_list=clan_list)
                
                await self.parent_view.sent_message.edit(content=content, view=self.parent_view)
            else:
                # Direct match (exact/single/API) - defer to close modal silently
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True)
                
                # Refresh view with updated clan count (message content and button states)
                if hasattr(self.parent_view, 'sent_message') and self.parent_view.sent_message:  # type: ignore[arg-type]
                    try:
                        from qapbot.cache_manager import CACHE
                        from qapbot.i18n import t
                        guild_id = interaction.guild_id
                        
                        # Rebuild clan buttons to show the newly added clan with correct state
                        self.parent_view.clear_items()
                        if hasattr(self.parent_view, '_add_clan_buttons'):  # type: ignore[arg-type]
                            self.parent_view._add_clan_buttons()
                        if hasattr(self.parent_view, '_add_control_buttons'):  # type: ignore[arg-type]
                            self.parent_view._add_control_buttons()
                        
                        # Build updated display
                        if self.parent_view.family_clans:
                            clan_list = "\n".join([  # type: ignore[arg-type]
                                f"• {CACHE.get_clan_name(tag, 'Unknown')} ({tag})" 
                                for tag in self.parent_view.family_clans[:10]
                            ])
                            clan_count = len(self.parent_view.family_clans)  # type: ignore[arg-type]
                            if clan_count > 10:
                                clan_list += f"\n*... and {clan_count - 10} more*"
                        else:
                            clan_list = t('ui_components.family_management.no_clans_empty', guild_id=guild_id)
                        
                        content = t('ui_components.family_management.edit_family_content', guild_id=guild_id,
                                   family_name=self.parent_view.family_name,
                                   family_id=self.parent_view.family_id,
                                   clan_count=len(self.parent_view.family_clans),  # type: ignore[arg-type]
                                   clan_list=clan_list)
                        
                        await self.parent_view.sent_message.edit(content=content, view=self.parent_view)
                        logging.debug(f"Updated view after direct clan match")
                    except Exception as e:
                        logging.error(f"Error updating view after direct clan match: {e}", exc_info=True)
                else:
                    logging.warning(f"Cannot update parent view after clan add - sent_message not available")
        else:
            # Error handling - no selection view anymore (Requirement 1)
            pass


async def add_clan_by_search(search_input: str, parent_view: Any, interaction: discord.Interaction) -> Dict[str, Any]:  # type: ignore[misc]
    """
    Helper function to add a clan to a family or member clans list by searching for it.
    
    This function is reusable across EditFamilyView and MemberClansConfigurationView.
    
    Flow:
    1. Check if input is a complete clan tag
    2. If tag is known in clan_name_cache, add it directly
    3. If tag is unknown, fetch from CoC API and add
    4. If input is name substring, filter matching clans and show selection
    
    Args:
        search_input: Clan name substring or complete tag
        parent_view: EditFamilyView or MemberClansConfigurationView with family_clans or member_clans attribute
        interaction: Discord interaction
    
    Returns:
        Dict with keys:
            - success: bool
            - message: str (error message or success message)
            - show_selection: bool (whether to show selection dialog)
            - view: Optional[View] (selection view if show_selection is True)
    """
    from qapbot.cache_manager import CACHE
    from qapbot.QBdiscocmdshelper import normalize_clan_tag
    
    # Determine which list to add to
    if hasattr(parent_view, 'family_clans'):  # type: ignore[arg-type]
        target_list = parent_view.family_clans
        list_name = "family"
    elif hasattr(parent_view, 'member_clans'):  # type: ignore[arg-type]
        target_list = parent_view.member_clans
        list_name = "member clans"
    else:
        from qapbot.i18n import t
        return {
            "success": False,
            "message": t('ui_components.errors.error_parent_view_invalid', guild_id=None),
            "show_selection": False
        }
    
    # Check if input looks like a clan tag (starts with #)
    if search_input.startswith('#'):
        clan_tag = normalize_clan_tag(search_input)
        
        if not clan_tag:
            from qapbot.i18n import t
            user_id = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            error_msg = t('ui_components.errors.invalid_clan_tag', user_id=user_id, guild_id=guild_id)
            await interaction.response.send_message(error_msg, ephemeral=True)
            return {"success": False, "message": "Invalid clan tag format", "show_selection": False}
        
        # Check if already in list
        if clan_tag in target_list:
            from qapbot.i18n import t
            user_id = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            msg = t('ui_components.errors.clan_already_subscribed', user_id=user_id, guild_id=guild_id, clan_tag=clan_tag)
            await interaction.response.send_message(msg, ephemeral=True)
            return {"success": False, "message": f"Clan already in {list_name}", "show_selection": False}
        
        # Check if clan is known in cache
        if clan_tag in CACHE.clan_name_cache:
            clan_name = CACHE.get_clan_name(clan_tag, "Unknown")  # type: ignore[arg-type]
            target_list.append(clan_tag)  # type: ignore[arg-type]
            return {"success": True, "message": f"Added {clan_name}", "show_selection": False}
        
        # Clan tag not in cache - fetch from API
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            
            # Fetch clan from API (this will update clan_name_cache automatically)
            clan_obj = await CACHE.coc_clan_cache.get_clan(clan_tag)
            
            # Add to list
            target_list.append(clan_tag)  # type: ignore[arg-type]
            return {"success": True, "message": f"Added {clan_obj.name}", "show_selection": False}
            
        except Exception as e:
            from qapbot.i18n import t
            user_id = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            error_msg = t('ui_components.errors.clan_not_found_api', user_id=user_id, guild_id=guild_id, clan_tag=clan_tag)
            await interaction.followup.send(error_msg, ephemeral=True)
            logging.error(f"Error fetching clan {clan_tag}: {e}", exc_info=True)
            return {"success": False, "message": f"API error: {e}", "show_selection": False}
    
    # Input is a name substring - search clan_name_cache
    search_lower = search_input.lower()
    matches = []
    exact_match_tag = None
    
    for clan_tag in CACHE.clan_name_cache.keys():
        clan_name = CACHE.get_clan_name(clan_tag, "Unknown")  # type: ignore[arg-type]
        if clan_name and search_lower in clan_name.lower():
            matches.append((clan_tag, clan_name))
            # Check for exact name match (Requirement 2)
            if search_lower == clan_name.lower():
                exact_match_tag = clan_tag
    
    if not matches:
        from qapbot.i18n import t
        user_id = str(interaction.user.id)
        guild_id = interaction.guild.id if interaction.guild else None
        error_msg = t('ui_components.errors.no_clans_found', user_id=user_id, guild_id=guild_id, search_input=search_input)
        await interaction.response.send_message(error_msg, ephemeral=True)
        return {"success": False, "message": "No matches found", "show_selection": False}
    
    # If exact match found and not in list, auto-add it (Requirement 2)
    if exact_match_tag and exact_match_tag not in target_list:
        clan_name = CACHE.get_clan_name(exact_match_tag, "Unknown")  # type: ignore[arg-type]
        target_list.append(exact_match_tag)
        return {"success": True, "message": f"Added {clan_name}", "show_selection": False}
    
    # Filter out clans already in list
    available_matches = [(tag, name) for tag, name in matches if tag not in target_list]
    
    if not available_matches:
        from qapbot.i18n import t
        user_id = str(interaction.user.id)
        guild_id = interaction.guild.id if interaction.guild else None
        msg = t('ui_components.errors.all_matches_already_added', user_id=user_id, guild_id=guild_id, list_name=list_name)
        await interaction.response.send_message(msg, ephemeral=True)
        return {"success": False, "message": "All matches already added", "show_selection": False}
    
    # Single match - add directly
    if len(available_matches) == 1:  # type: ignore[arg-type]
        clan_tag, clan_name = available_matches[0]
        target_list.append(clan_tag)  # type: ignore[arg-type]
        return {"success": True, "message": f"Added {clan_name}", "show_selection": False}
    
    # Multiple matches - calculate if they fit as buttons (Requirement 4 & 5)
    # Calculate available button slots (max 20 total buttons, 5 per row x 4 rows)
    guild_id_str = str(interaction.guild.id) if interaction.guild else None
    if not guild_id_str:
        return {"success": False, "message": "Guild context missing", "show_selection": False}
    guild_config = CACHE.server_config.get(guild_id_str, {})
    
    # Collect clans that would be shown as buttons
    clans_set = set(target_list)  # Clans already in family
    clans_set.update(guild_config.get('member_clans', []))
    member_families = guild_config.get('member_families', [])
    for family_id in member_families:
        family_data = CACHE.clan_families.get(family_id, {})
        clans_set.update(family_data.get('clans', []))
    
    current_button_count = min(len(sorted(clans_set)), 20)  # type: ignore[arg-type]
    max_buttons = 20
    available_slots = max_buttons - current_button_count
    
    # If matches fit in available slots, return them for button display (Requirement 4)
    if len(available_matches) <= available_slots:  # type: ignore[arg-type]
        # Return matches so they can be added as buttons (no success message)
        match_count = len(available_matches)  # type: ignore[arg-type]
        return {
            "success": True,
            "message": f"Found {match_count} clans",
            "show_selection": False,
            "add_as_buttons": True,
            "matches": available_matches
        }
    
    # Too many matches - ask for more specific input (Requirement 5)
    from qapbot.i18n import t
    user_id = str(interaction.user.id)
    guild_id = interaction.guild.id if interaction.guild else None
    error_msg = t('ui_components.errors.too_many_clan_matches', user_id=user_id, guild_id=guild_id, count=len(available_matches), search_input=search_input, available_slots=available_slots)  # type: ignore[arg-type]
    await interaction.response.send_message(
        error_msg,
        ephemeral=True
    )
    return {"success": False, "message": "Too many matches", "show_selection": False}


class EditFamilyView(discord.ui.View):
    """View for editing a clan family - add/remove clans, rename."""
    
    def __init__(
        self,
        guild: discord.Guild,
        clan_management_view: 'ClanManagementView',
        family_id: str,
        family_data: Dict[str, Any],
        timeout: int = 300
    ):
        super().__init__(timeout=timeout)
        self.guild = guild
        self.clan_management_view = clan_management_view
        self.family_id = family_id
        self.family_name = family_data.get("name", "Unknown")
        
        # Working copy of clan list
        self.family_clans: List[str] = family_data.get("clans", []).copy()
        
        # Add UI components
        self._add_clan_buttons()
        self._add_control_buttons()
    
    def _add_clan_buttons(self):
        """Add buttons for clans: family members and clans with server subscriptions, sorted alphabetically."""
        from qapbot.cache_manager import CACHE
        
        guild_id = str(self.guild.id) if self.guild else None
        
        if not guild_id:
            return
        
        # Collect all relevant clans (family members + server subscriptions)
        clans_set = set()
        
        # Clans in the family
        clans_set.update(self.family_clans)
        
        # Clans with subscriptions on this server (direct or via other families)
        guild_config = CACHE.server_config.get(guild_id, {})
        clans_set.update(guild_config.get('member_clans', []))
        
        # Clans from families subscribed to this server
        member_families = guild_config.get('member_families', [])
        for family_id in member_families:
            family_data = CACHE.clan_families.get(family_id, {})
            clans_set.update(family_data.get('clans', []))
        
        # Sort all clans alphabetically for stable, consistent button positions
        clans_to_show = sorted(clans_set)[:20]  # type: ignore[arg-type] - Limit to 20 buttons (5 per row x 4 rows)
        
        for idx, clan_tag in enumerate(clans_to_show):
            clan_name = CACHE.get_clan_name(clan_tag, "Unknown")  # type: ignore[arg-type]
            is_in_family = clan_tag in self.family_clans
            
            # Truncate clan name if too long
            display_name = clan_name[:30] + "..." if clan_name and len(clan_name) > 30 else (clan_name or "Unknown")
            
            button = discord.ui.Button(
                label=f"{display_name}",
                emoji="✅" if is_in_family else "➕",
                style=discord.ButtonStyle.success if is_in_family else discord.ButtonStyle.secondary,
                custom_id=f"family_clan_{clan_tag}",
                row=idx // 5  # 5 items per row: row 0 (0-4), row 1 (5-9), row 2 (10-14), row 3 (15-19)
            )
            button.callback = self._create_toggle_callback(clan_tag)  # type: ignore[assignment]
            self.add_item(button)  # type: ignore[arg-type]
    
    def _create_toggle_callback(self, clan_tag: str):
        """Create callback function for toggling clan membership in family."""
        async def callback(interaction: discord.Interaction):
            await self._on_toggle_clan(interaction, clan_tag)
        return callback
    
    def _add_control_buttons(self):
        """Add Clear All, Add Clan, Rename, and Save buttons."""
        from qapbot.i18n import t
        
        guild_id = self.guild.id if self.guild else None
        
        # Clear all button
        clear_button = discord.ui.Button(
            label=t('ui_components.family_management.button_clear_all', guild_id=guild_id),
            emoji="🗑️",
            style=discord.ButtonStyle.danger,
            custom_id="clear_all_family_clans",
            row=4
        )
        clear_button.callback = self._on_clear_all  # type: ignore[assignment]
        self.add_item(clear_button)  # type: ignore[arg-type]
        
        # Add clan button
        add_clan_button = discord.ui.Button(
            label=t('ui_components.family_management.button_add_clan', guild_id=guild_id),
            emoji="➕",
            style=discord.ButtonStyle.secondary,
            custom_id="add_clan_to_family",
            row=4
        )
        add_clan_button.callback = self._on_add_clan  # type: ignore[assignment]
        self.add_item(add_clan_button)  # type: ignore[arg-type]
        
        # Rename button
        rename_button = discord.ui.Button(
            label=t('ui_components.family_management.button_rename', guild_id=guild_id),
            emoji="✏️",
            style=discord.ButtonStyle.secondary,
            custom_id="rename_family",
            row=4
        )
        rename_button.callback = self._on_rename  # type: ignore[assignment]
        self.add_item(rename_button)  # type: ignore[arg-type]
        
        # Save button
        save_button = discord.ui.Button(
            label=t('ui_components.family_management.button_save', guild_id=guild_id),
            emoji="💾",
            style=discord.ButtonStyle.primary,
            custom_id="save_family",
            row=4
        )
        save_button.callback = self._on_save  # type: ignore[assignment]
        self.add_item(save_button)  # type: ignore[arg-type]
    
    async def _on_toggle_clan(self, interaction: discord.Interaction, clan_tag: str):
        """Toggle a clan's membership in the family."""
        await interaction.response.defer()
        
        # Toggle membership
        if clan_tag in self.family_clans:
            self.family_clans.remove(clan_tag)
            is_in_family = False
        else:
            self.family_clans.append(clan_tag)  # type: ignore[arg-type]
            is_in_family = True
        
        # Update only this button's appearance (no view rebuild - preserves button order)
        for item in self.children:
            if hasattr(item, 'custom_id') and item.custom_id == f"family_clan_{clan_tag}":  # type: ignore[arg-type, attr-defined]
                item.emoji = "✅" if is_in_family else "➕"  # type: ignore[attr-defined]
                item.style = discord.ButtonStyle.success if is_in_family else discord.ButtonStyle.secondary  # type: ignore[attr-defined]
                break
        
        # Update message with new clan count
        from qapbot.cache_manager import CACHE
        from qapbot.i18n import t
        guild_id = self.guild.id if self.guild else None
        
        if self.family_clans:
            clan_list = "\n".join([  # type: ignore[arg-type]
                f"• {CACHE.get_clan_name(tag, 'Unknown')} ({tag})" 
                for tag in self.family_clans[:10]
            ])
            if len(self.family_clans) > 10:
                clan_list += f"\n*... and {len(self.family_clans) - 10} more*"
        else:
            clan_list = t('ui_components.family_management.no_clans_empty', guild_id=guild_id)
        
        content = t('ui_components.family_management.edit_family_content', guild_id=guild_id,
                   family_name=self.family_name,
                   family_id=self.family_id,
                   clan_count=len(self.family_clans),
                   clan_list=clan_list)
        
        await self.sent_message.edit(content=content, view=self)
    
    async def _on_clear_all(self, interaction: discord.Interaction):
        """Clear all clans from family."""
        self.family_clans = []
        await self._refresh_view(interaction)
    
    async def _on_add_clan(self, interaction: discord.Interaction):
        """Show modal to add a clan by name or tag."""
        modal = AddClanModal(parent_view=self)
        await interaction.response.send_modal(modal)
    
    async def _on_rename(self, interaction: discord.Interaction):
        """Show modal to rename the family."""
        modal = RenameFamilyModal(edit_family_view=self, current_name=self.family_name)
        await interaction.response.send_modal(modal)
    
    async def _refresh_view(self, interaction: discord.Interaction):
        """Refresh the view to show updated clan states."""
        from qapbot.cache_manager import CACHE
        
        # Rebuild view
        self.clear_items()
        self._add_clan_buttons()
        self._add_control_buttons()
        
        # Build updated display
        if self.family_clans:
            clan_list = "\n".join([  # type: ignore[arg-type]
                f"• {CACHE.get_clan_name(tag, 'Unknown')} ({tag})" 
                for tag in self.family_clans[:10]
            ])
            if len(self.family_clans) > 10:
                clan_list += f"\n*... and {len(self.family_clans) - 10} more*"
        else:
            from qapbot.i18n import t
            guild_id = interaction.guild_id
            clan_list = t('ui_components.family_management.no_clans_empty', guild_id=guild_id)
        
        from qapbot.i18n import t
        guild_id = interaction.guild_id
        content = t('ui_components.family_management.edit_family_content', guild_id=guild_id,
                   family_name=self.family_name,
                   family_id=self.family_id,
                   clan_count=len(self.family_clans),
                   clan_list=clan_list)
        await interaction.response.edit_message(content=content, view=self)
    
    async def _on_save(self, interaction: discord.Interaction):
        """Save changes to family."""
        from qapbot.cache_manager import CACHE
        from qapbot.QBdiscocmdshelper import format_clan_management_message

        # Defer immediately so we can always use followup (no second response needed)
        await interaction.response.defer(thinking=False, ephemeral=True)

        try:
            guild_id = str(self.guild.id) if self.guild else None

            # CRITICAL: Family ID must NEVER change - it's an immutable identifier
            # Changing the ID would break all references in member_families across all guilds
            # Update the family while preserving all existing fields (especially owned_by_guild)
            existing_family = CACHE.clan_families.get(self.family_id, {})

            # Capture old clan list BEFORE overwriting CACHE (needed for role diff)
            old_family_clans: set[str] = set(existing_family.get("clans", []))

            updated_family = dict(existing_family)  # Preserve all existing fields
            updated_family["name"] = self.family_name
            updated_family["clans"] = self.family_clans
            await CACHE.set_clan_family(self.family_id, updated_family)
            logging.info(f"Saved changes to family '{self.family_name}' (ID: {self.family_id}): {len(self.family_clans)} clans")

            # Update subscription status for all affected clans
            family_data = CACHE.clan_families.get(self.family_id, {})
            for clan_tag in family_data.get('clans', []):
                await CACHE.update_clan_subscription_status(clan_tag)
            logging.debug(f"Updated subscription status for {len(family_data.get('clans', []))} clans in family {self.family_id}")  # type: ignore[arg-type]

            # ---- Clan-role feature hooks ----
            # Only applies when this family is subscribed as a member_family in the current guild
            if guild_id and self.guild and interaction.guild:
                guild_config = CACHE.server_config.get(guild_id, {})
                if (guild_config.get("clan_role_enabled", False)
                        and self.family_id in guild_config.get("member_families", [])):
                    # Build full old/new guild coverage:
                    # individual member_clans + all member_family clans
                    # (for THIS family: use old/new clan list; all others: current state)
                    individual: set[str] = set(guild_config.get("member_clans", []))
                    old_coverage: set[str] = set(individual)
                    new_coverage: set[str] = set(individual)
                    for fid in guild_config.get("member_families", []):
                        other = set(CACHE.clan_families.get(fid, {}).get("clans", []))
                        if fid == self.family_id:
                            old_coverage.update(old_family_clans)
                            new_coverage.update(set(self.family_clans))
                        else:
                            old_coverage.update(other)
                            new_coverage.update(other)

                    removable_tags = await _handle_clan_role_changes_for_guild(
                        interaction.guild, guild_id, old_coverage, new_coverage
                    )
                    if removable_tags:
                        removed_lines = "\n".join(
                            f"• {CACHE.get_clan_name(tag, tag)}"  # type: ignore[arg-type]
                            for tag in removable_tags
                        )
                        confirm_view = ConfirmDeleteClanRolesView(
                            guild=interaction.guild,
                            guild_id=guild_id,
                            removed_tags=removable_tags,
                            role_names=removed_lines,
                        )
                        from qapbot.i18n import t as _t
                        confirm_msg = await interaction.followup.send(
                            _t('ui_components.role_configuration_buttons.removed_clan_roles_prompt',
                               guild_id=int(guild_id),  # type: ignore[arg-type]
                               role_names=removed_lines),
                            view=confirm_view,
                            ephemeral=True,
                            wait=True,
                        )
                        confirm_view._msg = confirm_msg  # type: ignore[misc]
            # ---- end clan-role hooks ----

            # Refresh the clan management view
            try:
                if not self.clan_management_view.sent_message or not self.clan_management_view.sent_message.guild:
                    logging.warning("Cannot refresh clan management view: message or guild is None")
                    return
                embed, _, _, _ = await format_clan_management_message(
                    self.clan_management_view.clan_tag,
                    self.clan_management_view.sent_message.guild,
                    mode="families"
                )

                self.clan_management_view.clear_items()
                self.clan_management_view._add_mode_select()  # type: ignore[attr-defined]
                self.clan_management_view._add_family_management_buttons()  # type: ignore[attr-defined]
                self.clan_management_view._add_refresh_button()  # type: ignore[attr-defined]

                await self.clan_management_view.sent_message.edit(embed=embed, view=self.clan_management_view)

                logging.debug(f"Successfully refreshed clan management view after saving family '{self.family_name}'")
            except Exception as e:
                logging.error(f"Error refreshing family view after save: {e}", exc_info=True)
                raise

            # Delete the edit message
            try:
                await interaction.delete_original_response()
                logging.debug(f"Deleted edit family message for {self.family_name}")
            except Exception as e:
                logging.debug(f"Could not delete edit message: {e}")

        except Exception as e:
            from qapbot.i18n import t
            user_id = str(interaction.user.id)
            gid = interaction.guild.id if interaction.guild else None
            error_msg = t('ui_components.errors.error_saving_family', user_id=user_id, guild_id=gid)
            await interaction.followup.send(error_msg, ephemeral=True)
            logging.error(f"Error saving family: {e}", exc_info=True)
    
    async def show_edit_interface(self, interaction: discord.Interaction):
        """Show the edit interface (called after family selection or creation)."""
        from qapbot.cache_manager import CACHE
        
        # Build display
        if self.family_clans:
            clan_list = "\n".join([  # type: ignore[arg-type]
                f"• {CACHE.get_clan_name(tag, 'Unknown')} ({tag})" 
                for tag in self.family_clans[:10]
            ])
            if len(self.family_clans) > 10:
                clan_list += f"\n*... and {len(self.family_clans) - 10} more*"
        else:
            from qapbot.i18n import t
            guild_id = interaction.guild_id
            clan_list = t('ui_components.family_management.no_clans_with_hint', guild_id=guild_id)
        
        from qapbot.i18n import t
        guild_id = interaction.guild_id
        content = t('ui_components.family_management.edit_family_content', guild_id=guild_id,
                   family_name=self.family_name,
                   family_id=self.family_id,
                   clan_count=len(self.family_clans),
                   clan_list=clan_list)
        
        # Use followup if interaction was already used (from modal)
        try:
            if interaction.response.is_done():
                self.sent_message = await interaction.followup.send(
                    content,
                    view=self,
                    ephemeral=True,
                    wait=True
                )
            else:
                await interaction.response.send_message(
                    content,
                    view=self,
                    ephemeral=True
                )
                self.sent_message = await interaction.original_response()
        except Exception as e:
            logging.error(f"Error showing edit interface: {e}", exc_info=True)

class ClanManagementLinkAccountView(discord.ui.View):
    """
    Comprehensive account linking interface for clan management.
    
    Provides selectors for:
    - Player selection (from unlinked players)
    - Discord user selection
    - Notification status (Enable/Disable)
    - Notification type (All Wars / CWL Only)
    - Notification mode (Once / Repeated)
    """
    def __init__(
        self,
        clan_tag: str,
        unlinked_players: List[Dict[str, Any]],
        sent_message: discord.Message,
        guild_clans: List[str],
        mode: str = "registrations",
        timeout: int = 300
    ):
        """
        Initialize comprehensive link account view.
        
        Args:
            clan_tag: Clan tag for context
            unlinked_players: List of unlinked player dicts
            sent_message: The clan management message to update after linking
            guild_clans: List of all guild clan tags
            mode: Current clan management mode
            timeout: View timeout in seconds
        """
        super().__init__(timeout=timeout)
        self.clan_tag = clan_tag
        self.unlinked_players = unlinked_players
        self.original_unlinked_players = unlinked_players.copy()  # Preserve original list for restoring after manual entry
        self.sent_message = sent_message
        self.guild = sent_message.guild  # Extract guild from message for i18n
        self.guild_clans = guild_clans
        self.mode = mode
        self.link_view_message: Optional[discord.Message] = None  # Track the ephemeral link view message
        
        # Pagination state for player selector (max 50 players in clan)
        self.player_offset = 0
        self.players_per_page = 24  # Show 24 + "Load more..." = 25 total
        
        # Selected values (to be populated by user)
        self.selected_player_tag: Optional[str] = None
        self.selected_user_id: Optional[int] = None
        self.notification_enabled: bool = True
        self.notification_type: str = "all_wars"
        self.notification_mode: str = "repeated"
        
        # Counter to force component refresh when needed
        self._rebuild_counter: int = 0  # type: ignore[attr-defined]
        
        # Add all UI components
        self._add_player_select()  # row 0  # type: ignore[attr-defined]
        self._add_user_select()  # row 1  # type: ignore[attr-defined]
        self._add_notification_status_select()  # row 2 (enable/disable)  # type: ignore[attr-defined]
        self._add_notification_type_mode_select()  # row 3 (combined war type + mode)  # type: ignore[attr-defined]
        self._add_manual_tag_and_submit_buttons()  # row 4 (manual tag button + submit button)  # type: ignore[attr-defined]
    
    def _add_player_select(self):
        """Add player selection dropdown with pagination support."""
        
        # Players already have activity scores from get_player_list() in format_clan_management_message
        # Just extract and sort them
        player_activity = []
        for player in self.unlinked_players:
            tag = player.get("tag", "")
            name = player.get("name", "Unknown")
            th = player.get("th_level", "?")
            activity_score = player.get("activity", 0)  # Already calculated
            
            player_activity.append({
                "tag": tag,
                "name": name,
                "th": th,
                "activity": activity_score
            })
        
        # Sort by activity score (highest first), then by name
        player_activity.sort(key=lambda x: (-x["activity"], x["name"].lower()))  # type: ignore[misc]
        
        # Calculate pagination
        total_players = len(player_activity)
        start_idx = self.player_offset
        end_idx = start_idx + self.players_per_page
        has_more = end_idx < total_players
        
        # Get current page of players
        current_page = player_activity[start_idx:end_idx]
        
        # Build player options
        player_options = []
        for player in current_page:
            tag = player["tag"]
            name = player["name"]
            th = player["th"]
            
            # Format: PlayerName (TH# #TAG)
            label = f"{name} (TH{th} {tag})"
            if len(label) > 100:
                label = label[:97] + "..."
            
            player_options.append(discord.SelectOption(
                label=label,
                value=tag,
                default=(tag == self.selected_player_tag)  # Mark as selected if this is the chosen player
            ))
        
        # Add navigation options
        # Add "Back to first page" option if not on first page
        if self.player_offset > 0:
            from qapbot.i18n import t
            guild_id = self.guild.id if self.guild else None
            label = t('ui_components.notification_settings.button_back_to_first', guild_id=guild_id)
            player_options.insert(0, discord.SelectOption(
                label=label,
                value="__back_to_first__"
            ))
        
        # Add "Load more..." option if there are more players
        if has_more:
            remaining = total_players - end_idx
            player_options.append(discord.SelectOption(
                label=f"📄 Load more... ({remaining} remaining)",
                value="__load_more__"
            ))
        
        if not player_options:
            return
        
        # Show pagination info in placeholder if not on first page
        from qapbot.i18n import t
        guild_id = self.guild.id if self.guild else None
        
        if self.player_offset > 0:
            placeholder = t('ui_components.user_notification_settings.placeholder_player_pagination', guild_id=guild_id, start=start_idx+1, end=min(end_idx, total_players), total=total_players)
        else:
            placeholder = t('ui_components.user_notification_settings.placeholder_player_select', guild_id=guild_id)
        
        player_select = discord.ui.Select(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=player_options,  # type: ignore[arg-type]
            custom_id="link_player_select",
            row=0
        )
        player_select.callback = self._on_player_select  # type: ignore[assignment]
        self.add_item(player_select)  # type: ignore[arg-type]
    
    def _add_user_select(self):
        """Add Discord user selection."""
        from qapbot.i18n import t
        guild_id = self.guild.id if self.guild else None
        
        # Prepare default values if user is already selected
        default_values = None
        if self.selected_user_id is not None:
            default_values = [discord.Object(id=self.selected_user_id)]
        
        # Use dynamic custom_id to force Discord to treat this as a new component after rebuilds
        # This fixes the issue where clearing with X button leaves stale state
        user_select = discord.ui.UserSelect(
            placeholder=t('ui_components.user_notification_settings.placeholder_user_select', guild_id=guild_id),
            min_values=1,
            max_values=1,
            custom_id=f"link_user_select_{self._rebuild_counter}",  # type: ignore[attr-defined]
            row=1,
            default_values=default_values if default_values else None  # type: ignore[arg-type]
        )
        user_select.callback = self._on_user_select  # type: ignore[assignment]
        self.add_item(user_select)  # type: ignore[arg-type]
    
    def _add_notification_status_select(self):
        """Add notification enable/disable selector."""
        from qapbot.i18n import t
        guild_id = self.guild.id if self.guild else None
        
        status_select = discord.ui.Select(
            placeholder=t('ui_components.user_notification_settings.placeholder_status', guild_id=guild_id),
            min_values=1,
            max_values=1,
            options=[  # type: ignore[arg-type]
                discord.SelectOption(
                    label=t('ui_components.user_notification_settings.label_enable', guild_id=guild_id),
                    value="enabled",
                    description=t('ui_components.user_notification_settings.desc_enable', guild_id=guild_id),
                    default=self.notification_enabled
                ),
                discord.SelectOption(
                    label=t('ui_components.user_notification_settings.label_disable', guild_id=guild_id),
                    value="disabled",
                    description=t('ui_components.user_notification_settings.desc_disable', guild_id=guild_id),
                    default=not self.notification_enabled
                )
            ],
            custom_id="link_notif_status",
            row=2
        )
        status_select.callback = self._on_status_select  # type: ignore[assignment]
        self.add_item(status_select)  # type: ignore[arg-type]
    
    def _add_notification_type_mode_select(self):
        """Add combined war type + notification mode selector."""
        from qapbot.i18n import t
        guild_id = self.guild.id if self.guild else None
        
        # Determine current combined value
        current_value = f"{self.notification_type}:{self.notification_mode}"
        
        type_mode_select = discord.ui.Select(
            placeholder=t('ui_components.user_notification_settings.placeholder_type_mode', guild_id=guild_id),
            min_values=1,
            max_values=1,
            options=[  # type: ignore[arg-type]
                discord.SelectOption(
                    label=t('ui_components.user_notification_settings.label_all_repeated', guild_id=guild_id),
                    value="all_wars:repeated",
                    description=t('ui_components.user_notification_settings.desc_all_repeated', guild_id=guild_id),
                    default=(current_value == "all_wars:repeated")
                ),
                discord.SelectOption(
                    label=t('ui_components.user_notification_settings.label_all_once', guild_id=guild_id),
                    value="all_wars:once",
                    description=t('ui_components.user_notification_settings.desc_all_once', guild_id=guild_id),
                    default=(current_value == "all_wars:once")
                ),
                discord.SelectOption(
                    label=t('ui_components.user_notification_settings.label_cwl_repeated', guild_id=guild_id),
                    value="cwl_only:repeated",
                    description=t('ui_components.user_notification_settings.desc_cwl_repeated', guild_id=guild_id),
                    default=(current_value == "cwl_only:repeated")
                ),
                discord.SelectOption(
                    label=t('ui_components.user_notification_settings.label_cwl_once', guild_id=guild_id),
                    value="cwl_only:once",
                    description=t('ui_components.user_notification_settings.desc_cwl_once', guild_id=guild_id),
                    default=(current_value == "cwl_only:once")
                )
            ],
            custom_id="link_notif_type_mode",
            row=3
        )
        type_mode_select.callback = self._on_type_mode_select  # type: ignore[assignment]
        self.add_item(type_mode_select)  # type: ignore[arg-type]
    
    def _add_notification_mode_select(self):
        """Add notification mode selector (once/repeated)."""
        from qapbot.i18n import t
        guild_id = self.guild.id if self.guild else None
        
        mode_select = discord.ui.Select(
            placeholder=t('ui_components.user_notification_settings.placeholder_mode', guild_id=guild_id),
            min_values=1,
            max_values=1,
            options=[  # type: ignore[arg-type]
                discord.SelectOption(
                    label=t('ui_components.user_notification_settings.label_repeated_short', guild_id=guild_id),
                    value="repeated",
                    description=t('ui_components.user_notification_settings.desc_repeated_short', guild_id=guild_id),
                    default=True
                ),
                discord.SelectOption(
                    label=t('ui_components.user_notification_settings.label_once_short', guild_id=guild_id),
                    value="once",
                    description=t('ui_components.user_notification_settings.desc_once_short', guild_id=guild_id)
                )
            ],
            custom_id="link_notif_mode",
            row=3
        )
        mode_select.callback = self._on_mode_select  # type: ignore[assignment]
        self.add_item(mode_select)  # type: ignore[arg-type]
    
    def _add_manual_tag_and_submit_buttons(self):
        """Add submit button, manual player tag button, and manual user ID button."""
        from qapbot.i18n import t
        guild_id = self.guild.id if self.guild else None
        
        # Submit button (leftmost)
        submit_button = discord.ui.Button(
            label=f"✅ {t('ui_components.link_account.button_link', guild_id=guild_id)}",
            style=discord.ButtonStyle.success,
            custom_id="link_submit",
            row=4
        )
        submit_button.callback = self._on_submit  # type: ignore[assignment]
        self.add_item(submit_button)  # type: ignore[arg-type]
        
        # Manual tag entry button (middle)
        manual_tag_button = discord.ui.Button(
            label=t('ui_components.link_account.button_by_tag', guild_id=guild_id),
            style=discord.ButtonStyle.secondary,
            custom_id="link_manual_tag",
            row=4
        )
        manual_tag_button.callback = self._on_manual_tag  # type: ignore[assignment]
        self.add_item(manual_tag_button)  # type: ignore[arg-type]
        
        # Manual Discord user ID entry button (rightmost)
        manual_user_button = discord.ui.Button(
            label=t('ui_components.link_account.button_by_user_id', guild_id=guild_id),
            style=discord.ButtonStyle.secondary,
            custom_id="link_manual_user",
            row=4
        )
        manual_user_button.callback = self._on_manual_user_id  # type: ignore[assignment]
        self.add_item(manual_user_button)  # type: ignore[arg-type]
    
    async def _on_player_select(self, interaction: discord.Interaction) -> None:
        """Handle player selection or pagination."""
        selected_value = interaction.data['values'][0]  # type: ignore[index]
        
        # Check if "Back to first page" was selected
        if selected_value == "__back_to_first__":
            # Reset to first page
            self.player_offset = 0
            
            # Rebuild view with first page
            self._rebuild_view_with_new_page()
            
            # Update the message with new view
            await interaction.response.edit_message(view=self)
        # Check if "Load more..." was selected
        elif selected_value == "__load_more__":
            # Update offset to next page
            self.player_offset += self.players_per_page
            
            # Rebuild view with new page
            self._rebuild_view_with_new_page()
            
            # Update the message with new view
            await interaction.response.edit_message(view=self)
        else:
            # Normal player selection
            self.selected_player_tag = selected_value
            await interaction.response.defer()
    
    def _rebuild_view_with_new_page(self):
        """Rebuild view components to show new player page."""
        # Clear all items
        self.clear_items()
        
        # Re-add all components (player select will use updated offset)
        self._add_player_select()  # row 0 - with new pagination  # type: ignore[attr-defined]
        self._add_user_select()  # row 1  # type: ignore[attr-defined]
        self._add_notification_status_select()  # row 2  # type: ignore[attr-defined]
        self._add_notification_type_mode_select()  # row 3  # type: ignore[attr-defined]
        self._add_manual_tag_and_submit_buttons()  # row 4  # type: ignore[attr-defined]
    
    async def _on_user_select(self, interaction: discord.Interaction) -> None:
        """Handle Discord user selection and load user's current notification settings."""
        from qapbot.cache_manager import CACHE
        
        selected_user = interaction.data['resolved']['users']  # type: ignore[index]
        self.selected_user_id = int(list(selected_user.keys())[0])
        
        # Load user's current notification settings from cache
        user_data = CACHE.user_accounts.get(str(self.selected_user_id), {})
        if isinstance(user_data, dict):  # type: ignore[misc]
            notif_settings = user_data.get('notification_settings', {})
            
            # Check if user has notifications enabled
            if notif_settings.get('war_reminders', False):
                # User has notifications enabled - pre-fill with their current settings
                self.notification_enabled = True
                self.notification_type = notif_settings.get('notification_type', 'all_wars')
                self.notification_mode = notif_settings.get('notification_mode', 'repeated')
            else:
                # User has notifications disabled - pre-fill with defaults
                self.notification_enabled = True
                self.notification_type = 'all_wars'
                self.notification_mode = 'repeated'
        else:
            # User not found in cache - use defaults
            self.notification_enabled = True
            self.notification_type = 'all_wars'
            self.notification_mode = 'repeated'
        
        # Rebuild view to reflect updated notification settings
        self.clear_items()
        self._add_player_select()  # row 0  # type: ignore[attr-defined]
        self._add_user_select()  # row 1  # type: ignore[attr-defined]
        self._add_notification_status_select()  # row 2  # type: ignore[attr-defined]
        self._add_notification_type_mode_select()  # row 3  # type: ignore[attr-defined]
        self._add_manual_tag_and_submit_buttons()  # row 4  # type: ignore[attr-defined]
        
        await interaction.response.edit_message(view=self)
    
    async def _on_status_select(self, interaction: discord.Interaction) -> None:
        """Handle notification status selection."""
        self.notification_enabled = (interaction.data['values'][0] == "enabled")  # type: ignore[index]
        await interaction.response.defer()
    
    async def _on_type_mode_select(self, interaction: discord.Interaction) -> None:
        """Handle combined notification type and mode selection."""
        value = interaction.data['values'][0]  # type: ignore[index]
        # Parse format: "war_type:mode"
        self.notification_type, self.notification_mode = value.split(':')
        await interaction.response.defer()
    
    async def _on_mode_select(self, interaction: discord.Interaction) -> None:
        """Handle notification mode selection."""
        self.notification_mode = interaction.data['values'][0]  # type: ignore[index]
        from qapbot.i18n import t
        user_id = str(interaction.user.id)
        guild_id = interaction.guild.id if interaction.guild else None
        mode_text = t('warnotifications.mode_repeated', user_id=user_id, guild_id=guild_id) if self.notification_mode == "repeated" else t('warnotifications.mode_once', user_id=user_id, guild_id=guild_id)
        msg = t('ui_components.messages.notification_mode_updated', user_id=user_id, guild_id=guild_id, mode=mode_text)
        await interaction.response.send_message(
            msg,
            ephemeral=True,
            delete_after=3
        )
    
    async def _on_manual_tag(self, interaction: discord.Interaction) -> None:
        """Handle manual player tag entry button."""
        modal = ManualPlayerTagModal(link_view=self)
        await interaction.response.send_modal(modal)
    
    async def _on_manual_user_id(self, interaction: discord.Interaction) -> None:
        """Handle manual Discord user ID entry button."""
        modal = ManualUserIDModal(link_view=self)
        await interaction.response.send_modal(modal)
    
    async def _on_submit(self, interaction: discord.Interaction, admin_override: bool = False) -> None:
        """Handle submit button - perform account linking with all settings.
        
        Args:
            interaction: Discord interaction
            admin_override: If True, allows admin to override verified player security check
        """
        # Track whether we deferred this interaction
        # If admin_override=True from parameter, we're being called from ClanManagementAdminOverrideView
        # and should NOT defer this interaction (it's the confirm button)
        interaction_deferred = False
        
        if not admin_override:
            # Normal submit button - defer this interaction
            await interaction.response.defer()
            interaction_deferred = True
        # else: admin override callback from confirm dialog - don't defer, we'll send a separate response
        
        # Validate selections
        if not self.selected_player_tag:
            from qapbot.i18n import t
            user_id = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            error_msg = t('ui_components.errors.no_player_selected', user_id=user_id, guild_id=guild_id)
            await interaction.followup.send(error_msg, ephemeral=True)
            return
        
        if not self.selected_user_id:
            from qapbot.i18n import t
            user_id = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            error_msg = t('ui_components.errors.no_user_selected', user_id=user_id, guild_id=guild_id)
            await interaction.followup.send(error_msg, ephemeral=True)
            return
        
        # Perform account linking
        from qapbot.QBdiscocmdshelper import complete_account_linking_flow, get_verified_player_owner, normalize_clan_tag, check_admin_permissions
        from qapbot.cache_manager import CACHE
        from qapbot.config import CONFIG
        import logging
        
        normalized_tag = normalize_clan_tag(self.selected_player_tag)
        
        # Check if user is admin - if so, enable admin override for entire flow
        is_admin = await check_admin_permissions(interaction, CONFIG.server_admin)
        if is_admin and not admin_override:
            # User is admin - enable admin override (allows role assignment and bypass API token requirement)
            admin_override = True
            logging.info(f"Admin {interaction.user} linking player {normalized_tag} - admin_override enabled")
        
        # ACCOUNT PROTECTION: Check if player is already VERIFIED by another Discord user
        if not normalized_tag:
            return
        
        verified_owner = get_verified_player_owner(normalized_tag, str(self.selected_user_id))
        
        if verified_owner and not admin_override:
            # Player is verified by another user - require admin confirmation
            # Fetch player name for display
            player_name = next((p["name"] for p in self.unlinked_players if p.get("tag") == self.selected_player_tag), None)
            if not player_name:
                try:
                    player_obj = await CACHE.get_player(normalized_tag)
                    player_name = player_obj.name if player_obj else "Unknown"
                except Exception:
                    player_name = "Unknown"
            
            # Show admin override confirmation dialog (custom for clan management)
            if not normalized_tag:
                return
            
            confirm_view = ClanManagementAdminOverrideView(
                link_view=self,
                player_tag=normalized_tag,
                player_name=player_name,
                verified_owner_name=verified_owner
            )
            
            from qapbot.i18n import t
            user_id = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            msg = t('ui_components.prompts.admin_override_confirm', user_id=user_id, guild_id=guild_id,
                   player_name=player_name, player_tag=normalized_tag, 
                   verified_owner=verified_owner, selected_user_id=self.selected_user_id)
            
            await interaction.followup.send(
                msg,
                view=confirm_view,
                ephemeral=True
            )
            return
        
        # Get player name before linking
        player_name = next((p["name"] for p in self.unlinked_players if p.get("tag") == self.selected_player_tag), "Unknown")
        
        # Link the account (without API verification prompt since admin is linking)
        if not normalized_tag:
            return
        
        ok, msg = await complete_account_linking_flow(
            interaction=interaction,
            target_user_id=self.selected_user_id,
            player_tag=normalized_tag,
            api_token=None,
            show_api_prompt=False,  # Admin linking doesn't prompt for API
            send_success_message=False,  # We'll send custom message
            use_followup=True,
            skip_notification_prompt=True,  # Notifications already configured in selectors
            admin_override=admin_override  # Pass through admin override flag
        )
        
        if not ok:
            # Send error message
            from qapbot.i18n import t
            guild_id_for_error = interaction.guild.id if interaction.guild else None
            error_msg = t('ui_components.errors.error_failed_to_link_account', guild_id=guild_id_for_error, error=msg)
            if interaction_deferred:
                # Interaction was deferred - use followup
                await interaction.followup.send(error_msg, ephemeral=True)
            else:
                # Admin override from confirm dialog - interaction not deferred, use response
                await interaction.response.send_message(error_msg, ephemeral=True)
            return
        
        # Apply notification settings
        user_id_str = str(self.selected_user_id)
        user_data = CACHE.user_accounts.get(user_id_str, {})
        
        # Initialize notification_settings if missing
        if "notification_settings" not in user_data:
            user_data["notification_settings"] = {}
        
        # Apply all selected settings
        user_data["notification_settings"]["war_reminders"] = self.notification_enabled
        user_data["notification_settings"]["notification_type"] = self.notification_type
        user_data["notification_settings"]["notification_mode"] = self.notification_mode
        if "hours_before_end" not in user_data["notification_settings"]:
            user_data["notification_settings"]["hours_before_end"] = 4
        
        await CACHE.set_user_account(user_id_str, user_data)
        
        # Log admin action
        logging.info(
            f"CLAN_MANAGEMENT: {interaction.user} linked player {player_name} ({self.selected_player_tag}) "
            f"to discord_user={self.selected_user_id} with notifications: "
            f"enabled={self.notification_enabled}, type={self.notification_type}, mode={self.notification_mode}"
        )
        
        # Build status message for update
        notification_status_emoji = BotEmojis.GCHECK if self.notification_enabled else BotEmojis.REDX
        status_text = "enabled" if self.notification_enabled else "disabled"
        type_text = "all wars" if self.notification_type == "all_wars" else "CWL only"
        mode_text = "repeated" if self.notification_mode == "repeated" else "once"
        
        # Update the Link Account view message with status at the bottom
        # Use appropriate method based on whether this is admin override callback
        if admin_override and self.link_view_message:
            # Admin override: edit the stored link view message directly
            await self.link_view_message.edit(
                content=(
                    "**Link Player Account**\n\n"
                    "Select a player to link, choose notification settings, and select the Discord user.\n\n"
                    "─────────────────────────────────\n"
                    f"{BotEmojis.GCHECK} **Last action:** Successfully linked **{player_name}** to Discord User ID `{self.selected_user_id}` "
                    f"with notifications {notification_status_emoji} **{status_text}** ({type_text}, {mode_text})\n"
                    "─────────────────────────────────"
                ),
                view=self  # Keep view active for next transaction
            )
        else:
            # Normal submit: use interaction's edit_original_response
            await interaction.edit_original_response(
                content=(
                    "**Link Player Account**\n\n"
                    "Select a player to link, choose notification settings, and select the Discord user.\n\n"
                    "─────────────────────────────────\n"
                    f"{BotEmojis.GCHECK} **Last action:** Successfully linked **{player_name}** to Discord User ID `{self.selected_user_id}` "
                    f"with notifications {notification_status_emoji} **{status_text}** ({type_text}, {mode_text})\n"
                    "─────────────────────────────────"
                ),
                view=self  # Keep view active for next transaction
            )
        
        # Clear player selection for next transaction but keep user and notification settings
        linked_player_tag = self.selected_player_tag
        self.selected_player_tag = None
        
        # Restore original unlinked list (removing linked player) for next transaction
        # This ensures the selector repopulates correctly after manual entry + linking
        self.unlinked_players = [p for p in self.original_unlinked_players if p.get("tag") != linked_player_tag]
        self.original_unlinked_players = self.unlinked_players.copy()  # Update baseline
        
        # Reset pagination
        self.player_offset = 0
        
        # Rebuild the view with updated player list and preserved settings
        self.clear_items()
        self._add_player_select()  # row 0 - cleared selection, updated list  # type: ignore[attr-defined]
        self._add_user_select()  # row 1 - keeps previous user  # type: ignore[attr-defined]
        self._add_notification_status_select()  # row 2 - keeps previous setting  # type: ignore[attr-defined]
        self._add_notification_type_mode_select()  # row 3 - keeps previous setting  # type: ignore[attr-defined]
        self._add_manual_tag_and_submit_buttons()  # row 4  # type: ignore[attr-defined]
        
        # Update the message with new view
        # Use appropriate method based on whether this is admin override callback
        if admin_override and self.link_view_message:
            # Admin override: edit the stored link view message directly
            await self.link_view_message.edit(view=self)
        else:
            # Normal submit: use interaction's edit_original_response
            await interaction.edit_original_response(view=self)
        
        # Refresh the original clan management message
        try:
            from qapbot.QBdiscocmdshelper import format_clan_management_message
            
            if not self.sent_message or not self.sent_message.guild:
                logging.warning("Cannot refresh clan management message: message or guild is None")
                return
            
            main_embed, unlinked_embed, _, unlinked_players = await format_clan_management_message(
                self.clan_tag,
                self.sent_message.guild
            )
            
            # Create new view for clan management
            new_view = ClanManagementView(
                clan_tag=self.clan_tag,
                guild_clans=self.guild_clans,
                unlinked_players=unlinked_players,
                sent_message=self.sent_message,
                mode=self.mode,
                timeout=1800
            )
            
            # Update original clan management message
            embeds = [main_embed]
            if unlinked_embed:
                embeds.append(unlinked_embed)
            
            await self.sent_message.edit(
                embeds=embeds,  # type: ignore[arg-type]
                view=new_view
            )
        except Exception as refresh_error:
            logging.warning(f"Could not auto-refresh clan management message after linking: {refresh_error}")


class ClanManagementAdminOverrideView(discord.ui.View):
    """
    Confirmation dialog for clan management when admin tries to re-link a verified player.
    
    Similar to AdminOverrideConfirmView but integrates with ClanManagementLinkAccountView
    to preserve notification settings and view state.
    """
    def __init__(
        self,
        link_view: 'ClanManagementLinkAccountView',
        player_tag: str,
        player_name: str,
        verified_owner_name: str
    ):
        super().__init__(timeout=60)
        self.link_view = link_view
        self.player_tag = player_tag
        self.player_name = player_name
        self.verified_owner_name = verified_owner_name
        
        from qapbot.i18n import t
        # Get guild_id from link_view
        guild_id = link_view.guild.id if link_view.guild else None
        
        # Create buttons dynamically with translations
        cancel_button = discord.ui.Button(
            label=f"❌ {t('ui_components.confirmation_dialogs.button_cancel_default', guild_id=guild_id)}",
            style=discord.ButtonStyle.secondary,
            custom_id="cancel_clan_mgmt"
        )
        cancel_button.callback = self._cancel_callback
        self.add_item(cancel_button)  # type: ignore[arg-type]
        
        confirm_button = discord.ui.Button(
            label=f"⚠️ {t('ui_components.confirmation_dialogs.button_confirm_override', guild_id=guild_id)}",
            style=discord.ButtonStyle.danger,
            custom_id="confirm_clan_mgmt"
        )
        confirm_button.callback = self._confirm_callback
        self.add_item(confirm_button)  # type: ignore[arg-type]
    
    async def _cancel_callback(self, interaction: discord.Interaction):
        """Cancel the admin override."""
        from qapbot.i18n import t
        user_id = str(interaction.user.id)
        guild_id = interaction.guild.id if interaction.guild else None
        msg = t('ui_components.messages.admin_override_cancelled', user_id=user_id, guild_id=guild_id)
        await interaction.response.send_message(msg, ephemeral=True)
        
        # Disable all buttons
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        try:
            await interaction.message.edit(view=self)  # type: ignore[union-attr]
        except Exception:
            pass
        
        self.stop()
    
    async def _confirm_callback(self, interaction: discord.Interaction):
        """Confirm the admin override and proceed with linking."""
        # Disable all buttons
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        try:
            await interaction.message.edit(view=self)  # type: ignore[union-attr]
        except Exception:
            pass
        
        # Call the link view's _on_submit with admin_override=True
        # Call the protected method for admin override flow
        await self.link_view._on_submit(interaction, admin_override=True)  # type: ignore[misc]
        
        # Delete the confirmation message
        try:
            await interaction.message.delete()  # type: ignore[union-attr]
        except Exception:
            pass
        
        self.stop()


class AdminOverrideConfirmView(discord.ui.View):
    """
    Confirmation dialog for bot admin to override verified player security check.
    
    Used for admin operations that need to re-link a verified player.
    Defaults to "Cancel" to require explicit confirmation.
    
    Args:
        original_interaction: The command interaction
        player_tag: The player tag being linked
        player_name: The player name for display
        target_user_id: The user receiving the linked player
        verified_owner_name: Display name of current verified owner
        callback_kwargs: Additional kwargs for process_player_registration
    """
    def __init__(
        self,
        original_interaction: discord.Interaction,
        player_tag: str,
        player_name: str,
        target_user_id: int,
        verified_owner_name: str,
        callback_kwargs: Optional[Dict[str, Any]] = None
    ):
        super().__init__(timeout=60)
        self.original_interaction = original_interaction
        self.player_tag = player_tag
        self.player_name = player_name
        self.target_user_id = target_user_id
        self.verified_owner_name = verified_owner_name
        self.callback_kwargs = callback_kwargs or {}
        
        from qapbot.i18n import t
        # Get guild_id from original interaction
        guild_id = original_interaction.guild_id
        
        # Create buttons dynamically with translations
        cancel_button = discord.ui.Button(
            label=f"❌ {t('ui_components.confirmation_dialogs.button_cancel_default', guild_id=guild_id)}",
            style=discord.ButtonStyle.secondary,
            custom_id="cancel"
        )
        cancel_button.callback = self._cancel_callback
        self.add_item(cancel_button)  # type: ignore[arg-type]
        
        confirm_button = discord.ui.Button(
            label=f"⚠️ {t('ui_components.confirmation_dialogs.button_confirm_override', guild_id=guild_id)}",
            style=discord.ButtonStyle.danger,
            custom_id="confirm"
        )
        confirm_button.callback = self._confirm_callback
        self.add_item(confirm_button)  # type: ignore[arg-type]
        
    async def _cancel_callback(self, interaction: discord.Interaction):
        """Cancel the admin override."""
        from qapbot.i18n import t
        user_id = str(interaction.user.id)
        guild_id = interaction.guild.id if interaction.guild else None
        msg = t('ui_components.messages.admin_override_cancelled', user_id=user_id, guild_id=guild_id)
        await interaction.response.send_message(msg, ephemeral=True)
        
        # Disable all buttons
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        try:
            await interaction.message.edit(view=self)  # type: ignore[union-attr]
        except Exception:
            pass
        
        self.stop()
    
    async def _confirm_callback(self, interaction: discord.Interaction):
        """Confirm the admin override and proceed with linking."""
        from qapbot.QBdiscocmdshelper import process_player_registration
        
        await interaction.response.defer(ephemeral=True)
        
        # Disable all buttons
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        try:
            await interaction.message.edit(view=self)  # type: ignore[union-attr]
        except Exception:
            pass
        
        # Call process_player_registration with admin_override=True
        await process_player_registration(
            interaction=self.original_interaction,
            player_tag=self.player_tag,
            player_name=self.player_name,
            target_user_id=self.target_user_id,
            use_followup=True,
            show_api_prompt=False,
            admin_override=True,
            **self.callback_kwargs
        )
        
        # Delete the confirmation message
        try:
            await interaction.message.delete()  # type: ignore[union-attr]
        except Exception:
            pass
        
        self.stop()

class ImportDataConfirmView(discord.ui.View):
    """
    Confirmation dialog for importing ClashPerk player data.
    
    Shows preview of changes (add/upgrade/skip counts) and asks user to confirm before importing.
    Used by /import_data command.
    """
    def __init__(
        self,
        original_interaction: discord.Interaction,
        user_accounts: Dict[str, Any],
        results: Dict[str, Any],
        clan_name: str,
        clan_tag: str,
        to_add_count: int,
        to_upgrade_count: int,
        to_skip_count: int
    ):
        super().__init__(timeout=300)  # 5 minute timeout for review
        self.original_interaction = original_interaction
        self.user_accounts = user_accounts
        self.results = results
        self.clan_name = clan_name
        self.clan_tag = clan_tag
        self.to_add_count = to_add_count
        self.to_upgrade_count = to_upgrade_count
        self.to_skip_count = to_skip_count
        
        from qapbot.i18n import t
        # Get guild_id from original interaction
        guild_id = original_interaction.guild_id
        
        # Create buttons dynamically with translations
        cancel_button = discord.ui.Button(
            label=f"❌ {t('ui_components.confirmation_dialogs.button_cancel', guild_id=guild_id)}",
            style=discord.ButtonStyle.secondary,
            custom_id="cancel_import"
        )
        cancel_button.callback = self._cancel_callback
        self.add_item(cancel_button)  # type: ignore[arg-type]
        
        confirm_button = discord.ui.Button(
            label=f"✅ {t('ui_components.confirmation_dialogs.button_confirm_import', guild_id=guild_id)}",
            style=discord.ButtonStyle.success,
            custom_id="confirm_import"
        )
        confirm_button.callback = self._confirm_callback
        self.add_item(confirm_button)  # type: ignore[arg-type]
        
    async def _cancel_callback(self, interaction: discord.Interaction):
        """Cancel the import."""
        from qapbot.i18n import t
        user_id = str(interaction.user.id)
        guild_id = interaction.guild.id if interaction.guild else None
        msg = t('ui_components.messages.import_cancelled', user_id=user_id, guild_id=guild_id)
        await interaction.response.send_message(msg, ephemeral=True)
        
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        try:
            await interaction.message.edit(view=self)  # type: ignore[union-attr]
        except Exception:
            pass
        
        self.stop()
    
    async def _confirm_callback(self, interaction: discord.Interaction):
        """Confirm and apply the import."""
        from qapbot.import_clashperk_userlist import apply_import_changes
        from qapbot.QBdiscocmdshelper import send_and_track
        from qapbot.cache_manager import CACHE
        
        await interaction.response.defer(ephemeral=True)
        
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        try:
            await interaction.message.edit(view=self)  # type: ignore[union-attr]
        except Exception:
            pass
        
        try:
            _, added, upgraded, skipped, changed_user_ids = apply_import_changes(self.user_accounts, self.results)
            
            # Persist only users that were actually changed (per-record write-through)
            for uid in changed_user_ids:
                await CACHE.persist_user(uid)
            
            from qapbot.i18n import t
            guild_id = interaction.guild_id
            title = t('ui_components.import_data.complete_title', guild_id=guild_id, clan_name=self.clan_name)
            description = t('ui_components.import_data.complete_description', guild_id=guild_id)
            success_embed = discord.Embed(
                title=title,
                description=description,
                color=discord.Color.green()
            )
            field_clan = t('ui_components.import_data.field_clan', guild_id=guild_id)
            field_new = t('ui_components.import_data.field_new_players', guild_id=guild_id)
            field_upgraded = t('ui_components.import_data.field_upgraded', guild_id=guild_id)
            field_skipped = t('ui_components.import_data.field_skipped', guild_id=guild_id)
            success_embed.add_field(name=field_clan, value=f"{self.clan_name} ({self.clan_tag})", inline=False)
            success_embed.add_field(name=field_new, value=str(added), inline=True)
            success_embed.add_field(name=field_upgraded, value=str(upgraded), inline=True)
            success_embed.add_field(name=field_skipped, value=str(skipped), inline=True)
            
            await send_and_track(
                interaction=self.original_interaction,
                command_name="import_data",
                embed=success_embed,
                ephemeral=True
            )
            
        except Exception as e:
            from qapbot.i18n import t
            guild_id = interaction.guild_id
            title = t('ui_components.import_data.failed_title', guild_id=guild_id)
            error_embed = discord.Embed(
                title=title,
                description=f"Error: {str(e)}",
                color=discord.Color.red()
            )
            await send_and_track(
                interaction=self.original_interaction,
                command_name="import_data",
                embed=error_embed,
                ephemeral=True
            )
        
        try:
            await interaction.message.delete()  # type: ignore[union-attr]
        except Exception:
            pass
        
        self.stop()


class SwitchViewContinueView(discord.ui.View):
    """
    Prompt view asking user to switch ClashPerk view from 'Sorted by Name' to 'Sorted by Tag'.
    Used when duplicate player names are detected.
    """
    def __init__(
        self,
        original_interaction: discord.Interaction,
        channel_id: int,
        message_id: int,
        duplicate_names: List[str],
        clan_name: str,
        clan_tag: str,
        discord_usernames: Dict[str, tuple[str, bool]]
    ):
        super().__init__(timeout=300)  # 5 minute timeout
        self.original_interaction = original_interaction
        self.channel_id = channel_id
        self.message_id = message_id
        self.duplicate_names = duplicate_names
        self.clan_name = clan_name
        self.clan_tag = clan_tag
        self.discord_usernames = discord_usernames
        
        from qapbot.i18n import t
        # Get guild_id from original interaction
        guild_id = original_interaction.guild_id
        
        # Create buttons dynamically with translations
        continue_button = discord.ui.Button(
            label=f" {t('ui_components.confirmation_dialogs.button_continue_switched', guild_id=guild_id)}",
            style=discord.ButtonStyle.success,
            custom_id="continue_import"
        )
        continue_button.callback = self._continue_callback
        self.add_item(continue_button)  # type: ignore[arg-type]
        
        cancel_button = discord.ui.Button(
            label=f" {t('ui_components.confirmation_dialogs.button_cancel', guild_id=guild_id)}",
            style=discord.ButtonStyle.secondary,
            custom_id="cancel_switch"
        )
        cancel_button.callback = self._cancel_callback
        self.add_item(cancel_button)  # type: ignore[arg-type]
    
    async def _continue_callback(self, interaction: discord.Interaction):
        """User confirms they switched the view, continue parsing."""
        from qapbot.import_clashperk_userlist import parse_clashperk_embed_with_tag_data
        from qapbot.QBdiscocmdshelper import send_and_track
        
        await interaction.response.defer(ephemeral=True)
        
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        try:
            await interaction.message.edit(view=self)  # type: ignore[union-attr]
        except Exception:
            pass
        
        try:
            # Re-fetch and parse with tag data
            if not interaction.guild:
                return
            results, clan_tag, clan_name, verified_count, total_count = await parse_clashperk_embed_with_tag_data(
                guild=interaction.guild,
                channel_id=self.channel_id,
                message_id=self.message_id,
                cached_discord_usernames=self.discord_usernames
            )
            
            # Continue with normal import flow (same as original command)
            from qapbot.import_clashperk_userlist import analyze_import_changes
            # ImportDataConfirmView is defined in this module
            from qapbot.cache_manager import CACHE
            
            # Use in-memory CACHE instead of loading from disk
            user_accounts = CACHE.user_accounts
            to_add, to_upgrade, to_skip = analyze_import_changes(user_accounts, results)
            found_count = sum(1 for v in results.values() if v[1] is not None)
            not_found_count = len(results) - found_count
            
            if len(to_add) == 0 and len(to_upgrade) == 0:
                info_embed = discord.Embed(
                    title=f"? No Changes - {clan_name}",
                    description="All data is already up-to-date.",
                    color=discord.Color.light_grey()
                )
                await send_and_track(
                    interaction=self.original_interaction,
                    command_name="import_data",
                    embed=info_embed,
                    ephemeral=True
                )
                return
            
            from qapbot.i18n import t
            guild_id = interaction.guild_id
            title = t('ui_components.import_data.preview_title', guild_id=guild_id, clan_name=clan_name)
            description = t('ui_components.import_data.preview_description', guild_id=guild_id)
            preview_embed = discord.Embed(
                title=title,
                description=description,
                color=discord.Color.blue()
            )
            field_source = t('ui_components.import_data.field_source_info', guild_id=guild_id)
            field_matching = t('ui_components.import_data.field_discord_matching', guild_id=guild_id)
            field_summary = t('ui_components.import_data.field_changes_summary', guild_id=guild_id)
            footer_text = t('ui_components.import_data.preview_footer', guild_id=guild_id)
            
            preview_embed.add_field(
                name=field_source,
                value=f"**Clan**: {clan_name} ({clan_tag})\n**Total Players**: {total_count}\n**Verified Accounts**: {verified_count} ",
                inline=False
            )
            preview_embed.add_field(
                name=field_matching,
                value=f"**Found**: {found_count} players\n**Not Found**: {not_found_count} players",
                inline=False
            )
            preview_embed.add_field(
                name=field_summary,
                value=f" **New Players**: {len(to_add)}\n **Upgraded Status**: {len(to_upgrade)}\n **Skipped**: {len(to_skip)}",
                inline=False
            )
            preview_embed.set_footer(text=footer_text)
            
            view = ImportDataConfirmView(
                original_interaction=self.original_interaction,
                user_accounts=user_accounts,
                results=results,
                clan_name=clan_name,
                clan_tag=clan_tag,
                to_add_count=len(to_add),
                to_upgrade_count=len(to_upgrade),
                to_skip_count=len(to_skip)
            )
            
            await interaction.followup.send(embed=preview_embed, view=view, ephemeral=True)
            
        except Exception as e:
            from qapbot.i18n import t
            guild_id = interaction.guild_id
            title = t('ui_components.import_data.parse_failed_title', guild_id=guild_id)
            error_embed = discord.Embed(
                title=title,
                description=f"Error: {str(e)}",
                color=discord.Color.red()
            )
            await send_and_track(
                interaction=self.original_interaction,
                command_name="import_data",
                embed=error_embed,
                ephemeral=True
            )
        
        try:
            await interaction.message.delete()  # type: ignore[union-attr]
        except Exception:
            pass
        
        self.stop()
    
    async def _cancel_callback(self, interaction: discord.Interaction):
        """Cancel the import."""
        await interaction.response.send_message(t('ui_components.import_cancelled', guild_id=interaction.guild.id if interaction.guild else None), ephemeral=True)
        
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        try:
            await interaction.message.edit(view=self)  # type: ignore[union-attr]
        except Exception:
            pass
        
        self.stop()


class WelcomeMessageConfigView(discord.ui.View):
    """Ephemeral view for configuring the welcome message mode, clan-link selection and apply/ticket channel.

    Clan-link mode supports multi-selecting entire clan families and/or individual clans via
    toggle buttons. Mutual exclusion is enforced per-family: selecting a family deselects any
    individually-selected clans that belong to it, and individually selecting a clan that
    belongs to a currently-selected family deselects that family. Different families are fully
    independent of each other (e.g. Family A selected as a whole while Family B has 2 of 5
    clans picked individually).

    Changes are held in pending instance variables and only written to CACHE/DB when Save is clicked.
    Cancel or timeout discards all pending changes.
    """

    _MAX_FAMILY_CLAN_SLOTS = 20  # 4 rows x 5 buttons, row 4 reserved for mode/save/cancel

    def __init__(
        self,
        guild: discord.Guild,
        clan_management_view: 'ClanManagementView',
        original_interaction: discord.Interaction,
        timeout: int = 300
    ):
        super().__init__(timeout=timeout)
        self.guild = guild
        self.clan_management_view = clan_management_view
        self.original_interaction = original_interaction
        self.config_message: Optional[discord.Message] = None

        from qapbot.cache_manager import CACHE
        from qapbot.QBdiscocmdshelper import get_guild_clans_including_member_config
        guild_id = guild.id
        guild_config = CACHE.server_config.get(str(guild_id), {})

        # Pending state — only written to DB on Save
        self._pending_mode: str = guild_config.get("welcome_message_mode", "clan_link")
        self._pending_clan_tags: List[str] = list(guild_config.get("welcome_clan_tags", []))
        self._pending_family_tags: List[str] = list(guild_config.get("welcome_family_tags", []))
        self._pending_channel_id: str = guild_config.get("welcome_apply_channel_id", "") or ""

        # Universe of selectable families (attached to this guild) and clans
        member_family_ids = guild_config.get("member_families", [])
        self._families: Dict[str, Dict[str, Any]] = {
            fid: CACHE.clan_families[fid] for fid in member_family_ids if fid in CACHE.clan_families
        }
        # Also surface any pending family selections not (any longer) in member_families,
        # so a previously-saved selection remains visible/toggleable instead of disappearing.
        for fid in self._pending_family_tags:
            if fid not in self._families and fid in CACHE.clan_families:
                self._families[fid] = CACHE.clan_families[fid]

        self._all_clan_tags: List[str] = get_guild_clans_including_member_config(guild_id)

        # Map clan_tag -> owning family_id, restricted to families actually shown here
        self._family_of_clan: Dict[str, str] = {}
        for fid, fdata in self._families.items():
            for clan_tag in fdata.get("clans", []):
                self._family_of_clan[clan_tag] = fid

        self._build_items()

    # ── Item construction ───────────────────────────────────────────────

    def _build_items(self) -> None:
        """Rebuild all view items from current pending state."""
        self.clear_items()
        if self._pending_mode == "clan_link":
            self._add_family_and_clan_buttons()
        else:
            self._add_channel_select()
        self._add_mode_and_control_buttons()

    def _add_family_and_clan_buttons(self) -> None:
        """Add toggle buttons for families (row 0..3) followed by individual clans, filling rows 0-3."""
        from qapbot.cache_manager import CACHE

        state = {"row": 0, "col": 0}

        def next_row() -> int:
            r = state["row"]
            state["col"] += 1
            if state["col"] == 5:
                state["col"] = 0
                state["row"] += 1
            return r

        added = 0
        for family_id, family_data in list(self._families.items())[:5]:
            if added >= self._MAX_FAMILY_CLAN_SLOTS:
                break
            family_name = family_data.get("name", "Unknown Family")
            selected = family_id in self._pending_family_tags
            display_name = family_name[:25] + "..." if len(family_name) > 25 else family_name
            button = discord.ui.Button(
                label=display_name,
                emoji="🏰" if selected else "🏯",
                style=discord.ButtonStyle.success if selected else discord.ButtonStyle.secondary,
                custom_id=f"welcome_family_{family_id}",
                row=next_row()
            )
            button.callback = self._make_family_toggle_callback(family_id)  # type: ignore[assignment]
            self.add_item(button)  # type: ignore[arg-type]
            added += 1

        # Clan buttons, sorted alphabetically by clan name (not tag)
        sorted_clan_tags = sorted(
            self._all_clan_tags,
            key=lambda tag: (CACHE.get_clan_name(tag, tag) or tag).lower()  # type: ignore[arg-type]
        )
        for clan_tag in sorted_clan_tags:
            if added >= self._MAX_FAMILY_CLAN_SLOTS:
                break
            clan_name = CACHE.get_clan_name(clan_tag, clan_tag) or clan_tag  # type: ignore[arg-type]
            selected = clan_tag in self._pending_clan_tags
            display_name = clan_name[:30] + "..." if len(clan_name) > 30 else clan_name
            button = discord.ui.Button(
                label=display_name,
                emoji="✅" if selected else "➕",
                style=discord.ButtonStyle.success if selected else discord.ButtonStyle.secondary,
                custom_id=f"welcome_clan_{clan_tag}",
                row=next_row()
            )
            button.callback = self._make_clan_toggle_callback(clan_tag)  # type: ignore[assignment]
            self.add_item(button)  # type: ignore[arg-type]
            added += 1

    def _add_channel_select(self) -> None:
        """Add the apply/ticket channel selector (row 0), shown only in apply_channel mode."""
        from qapbot.i18n import t
        guild_id = self.guild.id
        channel_select = discord.ui.ChannelSelect(
            placeholder=t('ui_components.basic_config.welcome_channel_select_placeholder', guild_id=guild_id),
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text],
            custom_id="welcome_apply_channel_select",
            row=0
        )
        channel_select.callback = self._on_channel_select  # type: ignore[assignment]
        self.add_item(channel_select)  # type: ignore[arg-type]

    def _add_mode_and_control_buttons(self) -> None:
        """Add mode toggle + Save/Cancel buttons, always on row 4."""
        from qapbot.i18n import t
        guild_id = self.guild.id

        clan_link_btn = discord.ui.Button(
            label=t('ui_components.basic_config.config_welcome_mode_clan_link', guild_id=guild_id),
            style=discord.ButtonStyle.success if self._pending_mode == "clan_link" else discord.ButtonStyle.secondary,
            custom_id="welcome_mode_clan_link",
            row=4
        )
        clan_link_btn.callback = self._on_mode_clan_link  # type: ignore[assignment]
        self.add_item(clan_link_btn)  # type: ignore[arg-type]

        apply_channel_btn = discord.ui.Button(
            label=t('ui_components.basic_config.config_welcome_mode_apply_channel', guild_id=guild_id),
            style=discord.ButtonStyle.success if self._pending_mode == "apply_channel" else discord.ButtonStyle.secondary,
            custom_id="welcome_mode_apply_channel",
            row=4
        )
        apply_channel_btn.callback = self._on_mode_apply_channel  # type: ignore[assignment]
        self.add_item(apply_channel_btn)  # type: ignore[arg-type]

        save_btn = discord.ui.Button(
            label=t('ui_components.basic_config.welcome_save', guild_id=guild_id),
            style=discord.ButtonStyle.success,
            custom_id="welcome_save",
            row=4
        )
        save_btn.callback = self._on_save  # type: ignore[assignment]
        self.add_item(save_btn)  # type: ignore[arg-type]

        cancel_btn = discord.ui.Button(
            label=t('ui_components.basic_config.welcome_cancel', guild_id=guild_id),
            style=discord.ButtonStyle.secondary,
            custom_id="welcome_cancel",
            row=4
        )
        cancel_btn.callback = self._on_cancel  # type: ignore[assignment]
        self.add_item(cancel_btn)  # type: ignore[arg-type]

    # ── Display helper ───────────────────────────────────────────────────

    def _build_header_content(self, guild_id_int: int, error: str = "") -> str:
        """Return the dialog message content built from current pending state."""
        from qapbot.cache_manager import CACHE
        from qapbot.i18n import t

        mode_label = (
            t('ui_components.basic_config.config_welcome_mode_clan_link', guild_id=guild_id_int)
            if self._pending_mode == "clan_link"
            else t('ui_components.basic_config.config_welcome_mode_apply_channel', guild_id=guild_id_int)
        )
        not_set = t('ui_components.basic_config.config_channel_not_set', guild_id=guild_id_int)

        if self._pending_mode == "clan_link":
            lines: List[str] = []
            for family_id in self._pending_family_tags:
                family_data = self._families.get(family_id) or CACHE.clan_families.get(family_id, {})
                family_name = family_data.get("name", family_id)
                clan_count = len(family_data.get("clans", []))
                lines.append(f"🏰 {family_name} ({clan_count} clans)")
            for clan_tag in self._pending_clan_tags:
                clan_data = CACHE.clan_name_cache.get(clan_tag, {})
                clan_name = clan_data.get("name", clan_tag) if clan_data else clan_tag
                lines.append(f"• {clan_name}")
            selection_display = "\n".join(lines) if lines else not_set
            detail = t('ui_components.basic_config.config_welcome_clan', guild_id=guild_id_int, clan=selection_display)
        else:
            apply_display = not_set
            if self._pending_channel_id:
                try:
                    ch = self.guild.get_channel(int(self._pending_channel_id))
                    apply_display = ch.mention if ch else not_set
                except Exception:
                    pass
            detail = t('ui_components.basic_config.config_welcome_apply_channel', guild_id=guild_id_int, channel=apply_display)

        explanation = t('ui_components.basic_config.welcome_explanation', guild_id=guild_id_int)
        content = (
            f"👋 **{t('ui_components.basic_config.config_welcome_block_title', guild_id=guild_id_int)}**\n\n"
            f"{explanation}\n\n"
            f"{t('ui_components.basic_config.config_welcome_mode', guild_id=guild_id_int, mode=mode_label)}\n"
            f"{detail}"
        )
        if error:
            content += f"\n\n⚠️ {error}"
        return content

    async def _push_update(self, guild_id_int: int, error: str = "") -> None:
        """Edit config_message in-place with current pending state."""
        content = self._build_header_content(guild_id_int, error=error)
        if self.config_message:
            try:
                await self.config_message.edit(content=content, view=self)
            except Exception as e:
                logging.warning(f"[WelcomeMessageConfigView] Could not refresh config message: {e}")

    # ── Interaction callbacks ───────────────────────────────────────────

    def _make_family_toggle_callback(self, family_id: str):
        """Create callback toggling an entire family; deselects its individually-picked clans."""
        async def callback(interaction: discord.Interaction) -> None:
            await interaction.response.defer(thinking=False, ephemeral=False)
            if not interaction.guild:
                return
            if family_id in self._pending_family_tags:
                self._pending_family_tags.remove(family_id)
            else:
                self._pending_family_tags.append(family_id)
                family_clan_set = set(self._families.get(family_id, {}).get("clans", []))
                self._pending_clan_tags = [c for c in self._pending_clan_tags if c not in family_clan_set]
            self._build_items()
            await self._push_update(interaction.guild.id)
        return callback

    def _make_clan_toggle_callback(self, clan_tag: str):
        """Create callback toggling an individual clan; deselects its owning family, if selected."""
        async def callback(interaction: discord.Interaction) -> None:
            await interaction.response.defer(thinking=False, ephemeral=False)
            if not interaction.guild:
                return
            if clan_tag in self._pending_clan_tags:
                self._pending_clan_tags.remove(clan_tag)
            else:
                self._pending_clan_tags.append(clan_tag)
                owning_family = self._family_of_clan.get(clan_tag)
                if owning_family and owning_family in self._pending_family_tags:
                    self._pending_family_tags.remove(owning_family)
            self._build_items()
            await self._push_update(interaction.guild.id)
        return callback

    async def _on_channel_select(self, interaction: discord.Interaction) -> None:
        """Update pending channel selection."""
        await interaction.response.defer(thinking=False, ephemeral=False)
        if not interaction.guild:
            return
        self._pending_channel_id = str(interaction.data['values'][0])  # type: ignore[index]
        self._build_items()
        await self._push_update(interaction.guild.id)

    async def _on_mode_clan_link(self, interaction: discord.Interaction) -> None:
        await self._set_pending_mode(interaction, "clan_link")

    async def _on_mode_apply_channel(self, interaction: discord.Interaction) -> None:
        await self._set_pending_mode(interaction, "apply_channel")

    async def _set_pending_mode(self, interaction: discord.Interaction, mode: str) -> None:
        """Switch pending mode. Prior clan/family/channel selections are preserved either way."""
        await interaction.response.defer(thinking=False, ephemeral=False)
        if not interaction.guild:
            return
        self._pending_mode = mode
        self._build_items()
        await self._push_update(interaction.guild.id)

    async def _on_save(self, interaction: discord.Interaction) -> None:
        """Validate pending state, persist to DB, refresh main config, close dialog."""
        await interaction.response.defer(thinking=False, ephemeral=False)
        if not interaction.guild:
            return

        from qapbot.cache_manager import CACHE
        from qapbot.i18n import t

        guild_id_str = str(interaction.guild.id)
        guild_id_int = interaction.guild.id

        # Consistency check before writing. Clan-link mode with zero clans/families selected
        # is allowed — the welcome message simply omits the clan-link line in that case.
        if self._pending_mode == "apply_channel" and not self._pending_channel_id:
            await self._push_update(
                guild_id_int,
                error=t('ui_components.basic_config.welcome_error_no_channel', guild_id=guild_id_int)
            )
            return

        # Write to CACHE and DB
        if guild_id_str not in CACHE.server_config:
            CACHE.server_config[guild_id_str] = {}
        CACHE.server_config[guild_id_str]["welcome_message_mode"] = self._pending_mode
        CACHE.server_config[guild_id_str]["welcome_clan_tags"] = list(self._pending_clan_tags)
        CACHE.server_config[guild_id_str]["welcome_family_tags"] = list(self._pending_family_tags)
        CACHE.server_config[guild_id_str]["welcome_apply_channel_id"] = self._pending_channel_id or None
        # Legacy single-clan column is no longer used going forward.
        CACHE.server_config[guild_id_str].pop("welcome_clan_tag", None)
        await CACHE.persist_server_config(guild_id_str)

        # Refresh main config embed
        await self.clan_management_view._refresh_config_view(interaction)  # type: ignore[reportPrivateUsage]

        # Close dialog
        if self.config_message:
            try:
                await self.config_message.delete()
            except Exception:
                pass
        self.stop()

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        """Discard all pending changes and close the dialog."""
        await interaction.response.defer(thinking=False, ephemeral=False)
        if self.config_message:
            try:
                await self.config_message.delete()
            except Exception:
                pass
        self.stop()
