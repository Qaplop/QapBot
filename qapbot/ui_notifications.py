from __future__ import annotations
# pyright: reportUnknownParameterType=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""
Notification-related UI components.

Contains war notification prompts, unified notification settings,
language selection for notifications, and clan-wide notification management.
"""
import discord
import logging
from typing import List, Dict, Any, Optional

from qapbot.i18n import t
from qapbot.cache_manager import CACHE
from qapbot.emojis import BotEmojis


class WarNotificationPromptView(discord.ui.View):
    """
    View for prompting user to activate war notifications after account linking.
    Shows two buttons: "Activate Notifications" and "Skip".
    """
    def __init__(self, guild_id: Optional[int] = None, timeout: int = 600):
        """
        Initialize war notification prompt view.
        
        Args:
            guild_id: Discord guild ID for translation context
            timeout: Timeout in seconds for the view
        """
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.message: Optional[discord.Message] = None
        
        # Translate button labels - cast to Button to satisfy type checker
        from qapbot.i18n import t
        if len(self.children) >= 1 and isinstance(self.children[0], discord.ui.Button):
            self.children[0].label = t('warnotifications.button_activate', guild_id=guild_id)
        if len(self.children) >= 2 and isinstance(self.children[1], discord.ui.Button):
            self.children[1].label = t('warnotifications.button_disable', guild_id=guild_id).replace('🔕', '⏭️').replace('Disable', 'Skip')
    
    async def on_timeout(self) -> None:
        """Delete the message when the view times out."""
        if self.message is not None:
            try:
                await self.message.delete()
            except Exception:
                pass

    @discord.ui.button(label="🔔 Activate Notifications", style=discord.ButtonStyle.primary)
    async def activate_button(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[type-arg]
        """Handle Activate Notifications button click."""
        user_id = str(interaction.user.id)
        user_data = CACHE.user_accounts.get(user_id)
        
        if not user_data:
            from qapbot.i18n import t
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.response.send_message(
                t('playerregistration.user_data_not_found', user_id=user_id, guild_id=guild_id),
                ephemeral=True
            )
            return
        
        # Initialize notification_settings if missing
        if "notification_settings" not in user_data:
            user_data["notification_settings"] = {}
        
        # Enable war reminders with default settings
        user_data["notification_settings"]["war_reminders"] = True
        
        # Set defaults if not already set
        if "notification_mode" not in user_data["notification_settings"]:
            user_data["notification_settings"]["notification_mode"] = "repeated"
        if "notification_type" not in user_data["notification_settings"]:
            user_data["notification_settings"]["notification_type"] = "all_wars"
        if "hours_before_end" not in user_data["notification_settings"]:
            user_data["notification_settings"]["hours_before_end"] = 4
        
        await CACHE.set_user_account(user_id, user_data)
        
        from qapbot.i18n import t
        guild_id = interaction.guild.id if interaction.guild else None
        
        # Edit the original message to replace it with confirmation (remove buttons)
        await interaction.response.edit_message(
            content=t('playerregistration.notifications_enabled_success', guild_id=guild_id),
            view=None
        )
        logging.info(f"USER ACTION: {interaction.user} activated war notifications via account linking workflow")
    
    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.secondary)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[type-arg]
        """Handle Skip button click."""
        from qapbot.i18n import t
        guild_id = interaction.guild.id if interaction.guild else None
        # Edit the original message to replace it with skip message (remove buttons)
        await interaction.response.edit_message(
            content=t('playerregistration.notifications_skipped', guild_id=guild_id),
            view=None
        )
        logging.info(f"USER ACTION: {interaction.user} skipped war notification activation")



class UnifiedNotificationView(discord.ui.View):
    """
    Unified view for all notification management.
    
    Provides buttons to:
    - Enable/Disable notifications (dynamic based on current state)
    - Change War Type (All Wars vs CWL Only)
    - Change Notification Mode (Once vs Repeated)
    """
    def __init__(self, user_id: str, is_enabled: bool, current_type: str, current_mode: str, original_interaction: Optional[discord.Interaction] = None, timeout: int = 180):
        """
        Initialize unified notification view.
        
        Args:
            user_id: Discord user ID
            is_enabled: Whether notifications are currently enabled
            current_type: Current notification_type setting
            current_mode: Current notification_mode setting
            original_interaction: The interaction that created this view (for updating message)
            timeout: Timeout in seconds for the view
        """
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.is_enabled = is_enabled
        self.current_type = current_type
        self.current_mode = current_mode
        self.original_interaction = original_interaction
        self.message: Optional[discord.Message] = None
        
        # Get guild_id for translations
        self.guild_id = original_interaction.guild_id if original_interaction else None
        
        # Import translation function
        from qapbot.i18n import t
        
        # Add all three buttons manually in the correct order
        # Button 1: Enable/Disable (first/leftmost)
        if is_enabled:
            toggle_button = discord.ui.Button(
                label=t('warnotifications.button_disable', user_id=self.user_id, guild_id=self.guild_id),
                style=discord.ButtonStyle.danger,
                row=0
            )
        else:
            toggle_button = discord.ui.Button(
                label=t('warnotifications.button_activate', user_id=self.user_id, guild_id=self.guild_id),
                style=discord.ButtonStyle.success,
                row=0
            )
        toggle_button.callback = self.toggle_notifications
        self.add_item(toggle_button)  # type: ignore[arg-type]
        
        # Button 2: Change War Type (second/middle)
        type_button = discord.ui.Button(
            label=t('warnotifications.button_change_wartype', user_id=self.user_id, guild_id=self.guild_id),
            style=discord.ButtonStyle.primary,
            row=0
        )
        type_button.callback = self.change_type_button
        self.add_item(type_button)  # type: ignore[arg-type]
        
        # Button 3: Change Notification Mode (third/rightmost)
        mode_button = discord.ui.Button(
            label=t('warnotifications.button_change_mode', user_id=self.user_id, guild_id=self.guild_id),
            style=discord.ButtonStyle.primary,
            row=0
        )
        mode_button.callback = self.change_mode_button
        self.add_item(mode_button)  # type: ignore[arg-type]
        
        # Button 4: Change Language (fourth, second row)
        language_button = discord.ui.Button(
            label=t('warnotifications.button_change_language', user_id=self.user_id, guild_id=self.guild_id),
            style=discord.ButtonStyle.primary,
            row=1
        )
        language_button.callback = self.change_language_button
        self.add_item(language_button)  # type: ignore[arg-type]

        # Button 5: Link Buddy (second row, next to language)
        link_buddy_button = discord.ui.Button(
            label=t('warnotifications.button_link_buddy', user_id=self.user_id, guild_id=self.guild_id),
            style=discord.ButtonStyle.secondary,
            row=1
        )
        link_buddy_button.callback = self.link_buddy_button_callback
        self.add_item(link_buddy_button)  # type: ignore[arg-type]

        # Button 6: Remove Buddy (second row, only if user has buddies)
        current_buddies = CACHE.user_accounts.get(self.user_id, {}).get("watched_players", [])
        if current_buddies:
            remove_buddy_button = discord.ui.Button(
                label=t('warnotifications.button_remove_buddy', user_id=self.user_id, guild_id=self.guild_id),
                style=discord.ButtonStyle.danger,
                row=1
            )
            remove_buddy_button.callback = self.remove_buddy_button_callback
            self.add_item(remove_buddy_button)  # type: ignore[arg-type]
    
    async def on_timeout(self) -> None:
        """Delete the message when the view times out."""
        if self.message is not None:
            try:
                await self.message.delete()
            except Exception:
                pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Ensure only the owner can interact (and maintenance guard via super)."""
        if not await super().interaction_check(interaction):
            return False
        if str(interaction.user.id) != self.user_id:
            from qapbot.i18n import t
            # Use user_id parameter for ephemeral error messages
            guild_id = interaction.guild.id if interaction.guild else None
            message = t('warnotifications.error_not_your_settings', user_id=str(interaction.user.id), guild_id=guild_id)
            await interaction.response.send_message(
                message,
                ephemeral=True
            )
            return False
        return True
    
    async def toggle_notifications(self, interaction: discord.Interaction):
        """Handle enable/disable button click."""
        from qapbot.QBdiscocmdshelper import update_notification_subscription
        from qapbot.cache_manager import CACHE
        import logging
        
        enable = not self.is_enabled
        user_data = CACHE.user_accounts.get(self.user_id)
        
        if not user_data:
            from qapbot.i18n import t
            user_id = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            error_msg = t('ui_components.errors.user_data_not_found', user_id=user_id, guild_id=guild_id)
            await interaction.response.send_message(error_msg, ephemeral=True)
            return
        
        result = await update_notification_subscription(
            user_id=self.user_id,
            enable=enable,
            user_data=user_data
        )
        
        if result["success"]:
            action_word = "enabled" if enable else "disabled"
            logging.info(f"User {interaction.user.name} (ID {self.user_id}) {action_word} notifications for {result['player_count']} player(s)")
            
            # Get fresh settings and update the message
            from qapbot.QBdiscocmdshelper import format_notification_settings
            user_data = CACHE.user_accounts.get(self.user_id)
            if user_data:
                settings_text = format_notification_settings(user_data, interaction.user.display_name, user_id=self.user_id, guild_id=interaction.guild_id)
                
                # Read fresh state from CACHE after update
                notif_settings = user_data.get("notification_settings", {})
                new_is_enabled = notif_settings.get("war_reminders", False)
                new_type = notif_settings.get("notification_type", "all_wars")
                new_mode = notif_settings.get("notification_mode", "repeated")
                
                # Create new view with updated state from CACHE
                new_view = UnifiedNotificationView(
                    user_id=self.user_id,
                    is_enabled=new_is_enabled,
                    current_type=new_type,
                    current_mode=new_mode,
                    original_interaction=self.original_interaction
                )
                new_view.message = self.message
                
                await interaction.response.edit_message(content=settings_text, view=new_view)
        else:
            await interaction.response.send_message(f"❌ {result['message']}", ephemeral=True)
    
    async def change_type_button(self, interaction: discord.Interaction):
        """Toggle war type between All Wars and CWL Only."""
        from qapbot.cache_manager import CACHE
        import logging
        
        # Toggle to the opposite type
        new_type = "cwl_only" if self.current_type == "all_wars" else "all_wars"
        
        user_data = CACHE.user_accounts.get(self.user_id)
        if not user_data:
            await interaction.response.send_message(
                t('ui_components.errors.error_user_data_not_found', guild_id=self.guild_id),
                ephemeral=True
            )
            return
        
        # Initialize notification_settings if missing
        if "notification_settings" not in user_data:
            user_data["notification_settings"] = {}
        
        # Update notification_type
        user_data["notification_settings"]["notification_type"] = new_type
        await CACHE.set_user_account(self.user_id, user_data)
        
        logging.info(f"User {interaction.user.name} (ID {self.user_id}) changed notification_type to {new_type}")
        
        # Update the message with fresh settings
        from qapbot.QBdiscocmdshelper import format_notification_settings
        settings_text = format_notification_settings(user_data, interaction.user.display_name, user_id=self.user_id, guild_id=interaction.guild_id)
        
        # Get current enabled state and mode from user data
        notif_settings = user_data.get("notification_settings", {})
        is_enabled = notif_settings.get("war_reminders", False)
        current_mode = notif_settings.get("notification_mode", "repeated")
        
        # Create new view with updated type
        new_view = UnifiedNotificationView(
            user_id=self.user_id,
            is_enabled=is_enabled,
            current_type=new_type,
            current_mode=current_mode,
            original_interaction=self.original_interaction
        )
        new_view.message = self.message
        
        await interaction.response.edit_message(content=settings_text, view=new_view)
    
    async def change_mode_button(self, interaction: discord.Interaction):
        """Toggle notification mode between Once and Repeated."""
        from qapbot.cache_manager import CACHE
        import logging
        
        # Toggle to the opposite mode
        new_mode = "once" if self.current_mode == "repeated" else "repeated"
        
        user_data = CACHE.user_accounts.get(self.user_id)
        if not user_data:
            from qapbot.i18n import t
            user_id = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            error_msg = t('ui_components.errors.user_data_not_found', user_id=user_id, guild_id=guild_id)
            await interaction.response.send_message(error_msg, ephemeral=True)
            return
        
        # Initialize notification_settings if missing
        if "notification_settings" not in user_data:
            user_data["notification_settings"] = {}
        
        # Update notification_mode
        user_data["notification_settings"]["notification_mode"] = new_mode
        await CACHE.set_user_account(self.user_id, user_data)
        
        logging.info(f"User {interaction.user.name} (ID {self.user_id}) changed notification_mode to {new_mode}")
        
        # Update the message with fresh settings
        from qapbot.QBdiscocmdshelper import format_notification_settings
        settings_text = format_notification_settings(user_data, interaction.user.display_name, user_id=self.user_id, guild_id=interaction.guild_id)
        
        # Get current enabled state and type from user data
        notif_settings = user_data.get("notification_settings", {})
        is_enabled = notif_settings.get("war_reminders", False)
        current_type = notif_settings.get("notification_type", "all_wars")
        
        # Create new view with updated mode
        new_view = UnifiedNotificationView(
            user_id=self.user_id,
            is_enabled=is_enabled,
            current_type=current_type,
            current_mode=new_mode,
            original_interaction=self.original_interaction
        )
        new_view.message = self.message
        
        await interaction.response.edit_message(content=settings_text, view=new_view)
    
    async def change_language_button(self, interaction: discord.Interaction):
        """Show language selection dropdown."""
        from qapbot.cache_manager import CACHE
        from qapbot.i18n import t
        
        user_data = CACHE.user_accounts.get(self.user_id)
        if not user_data:
            user_id = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            error_msg = t('ui_components.errors.user_data_not_found', user_id=user_id, guild_id=guild_id)
            await interaction.response.send_message(error_msg, ephemeral=True)
            return
        
        # Create language selection view
        language_view = LanguageSelectionView(
            user_id=self.user_id,
            parent_view=self,
            original_interaction=self.original_interaction
        )
        language_view.message = self.message
        
        await interaction.response.edit_message(
            content=t('warnotifications.title', user_id=self.user_id, guild_id=interaction.guild_id) + "\n\n" + 
                    "Select your preferred language for war notifications:",
            view=language_view
        )

    async def link_buddy_button_callback(self, interaction: discord.Interaction):
        """Show modal to enter a CoC player tag to add as a buddy."""
        modal = LinkBuddyModal(
            user_id=self.user_id,
            parent_view=self,
            original_interaction=self.original_interaction
        )
        await interaction.response.send_modal(modal)

    async def remove_buddy_button_callback(self, interaction: discord.Interaction):
        """Show buddy removal dropdown."""
        from qapbot.cache_manager import CACHE
        user_data = CACHE.user_accounts.get(self.user_id)
        if not user_data:
            await interaction.response.send_message(
                t('ui_components.errors.user_data_not_found', user_id=self.user_id, guild_id=interaction.guild_id),
                ephemeral=True
            )
            return

        watched_players = user_data.get("watched_players", [])
        if not watched_players:
            await interaction.response.send_message(
                t('warnotifications.no_buddies', user_id=self.user_id, guild_id=interaction.guild_id),
                ephemeral=True
            )
            return

        remove_view = RemoveBuddyView(
            user_id=self.user_id,
            watched_players=watched_players,
            parent_view=self,
            original_interaction=self.original_interaction
        )
        remove_view.message = self.message
        from qapbot.QBdiscocmdshelper import format_notification_settings
        settings_text = format_notification_settings(user_data, interaction.user.display_name, user_id=self.user_id, guild_id=interaction.guild_id)
        await interaction.response.edit_message(
            content=settings_text + "\n\n" + t('warnotifications.buddy_removed_placeholder', user_id=self.user_id, guild_id=interaction.guild_id) + ":",
            view=remove_view
        )


class LanguageSelectionView(discord.ui.View):
    """View for selecting notification language preference."""
    
    def __init__(self, user_id: str, parent_view: UnifiedNotificationView, original_interaction: Optional[discord.Interaction], timeout: int = 180):
        """
        Initialize language selection view.
        
        Args:
            user_id: Discord user ID
            parent_view: The parent UnifiedNotificationView to return to after selection
            original_interaction: The original interaction
            timeout: Timeout in seconds for the view
        """
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.parent_view = parent_view
        self.original_interaction = original_interaction
        self.guild_id = original_interaction.guild_id if original_interaction else None
        self.message: Optional[discord.Message] = None
        
        # Add language selection dropdown
        from qapbot.i18n import t
        
        language_select = discord.ui.Select(
            placeholder=t('ui_components.language_selector.placeholder', user_id=self.user_id, guild_id=self.guild_id),
            options=[  # type: ignore[arg-type]
                discord.SelectOption(
                    label=t('warnotifications.language_auto', user_id=self.user_id, guild_id=self.guild_id),
                    value="auto",
                    description="Automatically detect from your Discord language",
                    emoji="🔄"
                ),
                discord.SelectOption(
                    label=t('warnotifications.language_english', user_id=self.user_id, guild_id=self.guild_id),
                    value="en",
                    description="English notifications",
                    emoji="🇺🇸"
                ),
                discord.SelectOption(
                    label=t('warnotifications.language_german', user_id=self.user_id, guild_id=self.guild_id),
                    value="de",
                    description="German / Deutsch",
                    emoji="🇩🇪"
                )
            ]
        )
        language_select.callback = self.language_selected
        self.add_item(language_select)  # type: ignore[arg-type]

    async def on_timeout(self) -> None:
        """Delete the message when the view times out."""
        if self.message is not None:
            try:
                await self.message.delete()
            except Exception:
                pass
    
    async def language_selected(self, interaction: discord.Interaction):
        """Handle language selection."""
        from qapbot.cache_manager import CACHE
        from qapbot.i18n import t
        import logging
        
        # For Select interactions, data is guaranteed to have 'values'
        if not interaction.data or 'values' not in interaction.data:
            return
        selected_language = interaction.data['values'][0]  # type: ignore[index]
        
        user_data = CACHE.user_accounts.get(self.user_id)
        if not user_data:
            user_id = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            error_msg = t('ui_components.errors.user_data_not_found', user_id=user_id, guild_id=guild_id)
            await interaction.response.send_message(error_msg, ephemeral=True)
            return
        
        # Update language preference
        if selected_language == "auto":
            # Auto mode: clear the lock and detect from Discord
            user_data["user_language_locked"] = False
            # Get current Discord locale to set the language
            locale = getattr(interaction, 'locale', None)  # type: ignore[arg-type]
            if locale:
                user_data["user_language"] = 'de' if str(locale).startswith('de') else 'en'
            else:
                user_data["user_language"] = 'en'  # Default fallback
        else:
            # Manual selection: set language and lock it
            user_data["user_language"] = selected_language
            user_data["user_language_locked"] = True
        
        CACHE.user_accounts[self.user_id] = user_data
        await CACHE.persist_user(self.user_id)
        
        logging.info(f"User {interaction.user.name} (ID {self.user_id}) changed notification language to {selected_language} (locked: {user_data.get('user_language_locked', False)})")
        
        # Get updated settings
        from qapbot.QBdiscocmdshelper import format_notification_settings
        settings_text = format_notification_settings(user_data, interaction.user.display_name, user_id=self.user_id, guild_id=interaction.guild_id)
        
        # Get current notification state
        notif_settings = user_data.get("notification_settings", {})
        is_enabled = notif_settings.get("war_reminders", False)
        current_type = notif_settings.get("notification_type", "all_wars")
        current_mode = notif_settings.get("notification_mode", "repeated")
        
        # Return to parent view with updated settings
        new_view = UnifiedNotificationView(
            user_id=self.user_id,
            is_enabled=is_enabled,
            current_type=current_type,
            current_mode=current_mode,
            original_interaction=self.original_interaction
        )
        new_view.message = self.message
        
        # Update the message with new settings
        await interaction.response.edit_message(
            content=settings_text,
            view=new_view
        )
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Ensure only the owner can interact (and maintenance guard via super)."""
        if not await super().interaction_check(interaction):
            return False
        if str(interaction.user.id) != self.user_id:
            from qapbot.i18n import t
            # Use user_id parameter for ephemeral error messages
            guild_id = interaction.guild.id if interaction.guild else None
            message = t('warnotifications.error_not_your_settings', user_id=str(interaction.user.id), guild_id=guild_id)
            await interaction.response.send_message(
                message,
                ephemeral=True
            )
            return False
        return True


# ============================================================================
# Save-your-Buddy Components
# ============================================================================


class BuddyPlayerSelectView(discord.ui.View):
    """View with a select dropdown to pick a player from multiple search matches when adding a buddy."""

    def __init__(self, user_id: str, guild_id: Optional[int], matches: List[Dict[str, Any]],
                 original_interaction: Optional[discord.Interaction], parent_view: 'UnifiedNotificationView',
                 timeout: int = 120):
        """
        Initialize BuddyPlayerSelectView.

        Args:
            user_id: Discord user ID of the watcher
            guild_id: Guild ID for i18n context
            matches: List of matched player dicts [{"tag": ..., "name": ...}]
            original_interaction: The interaction that spawned this flow (for editing original message)
            parent_view: The parent UnifiedNotificationView to return to
            timeout: View timeout in seconds
        """
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.guild_id = guild_id
        self.original_interaction = original_interaction
        self.parent_view = parent_view
        self.message: Optional[discord.Message] = None

        options = []
        for p in matches[:25]:  # Discord limit: max 25 options
            tag = p.get("tag", "")
            name = p.get("name", tag)
            options.append(discord.SelectOption(label=f"{name} ({tag})", value=tag, emoji="👥"))

        select = discord.ui.Select(
            placeholder="Select a player to add as buddy...",
            min_values=1,
            max_values=1,
            options=options  # type: ignore[arg-type]
        )
        select.callback = self.player_selected
        self.add_item(select)  # type: ignore[arg-type]

    async def on_timeout(self) -> None:
        """Delete the message when the view times out."""
        if self.message is not None:
            try:
                await self.message.delete()
            except Exception:
                pass

    async def player_selected(self, interaction: discord.Interaction):
        """Handle player selection — resolve via CoC API, add as buddy, and remove the dropdown message."""
        import logging

        selected_tag = interaction.data["values"][0]  # type: ignore[index]

        # Confirm the player still exists via CoC API for a fresh name
        player_obj = await CACHE.get_player(selected_tag)
        if not player_obj:
            await interaction.response.edit_message(
                content=t('warnotifications.buddy_not_found', user_id=self.user_id, guild_id=self.guild_id, player_tag=selected_tag),
                view=None
            )
            return

        player_tag = player_obj.tag
        player_name = player_obj.name

        user_data = CACHE.user_accounts.get(self.user_id, {})
        watched_players: List[Dict[str, Any]] = user_data.get("watched_players", [])
        own_players: List[Dict[str, Any]] = user_data.get("players", [])

        if any(wp.get("player_tag", "").upper() == player_tag.upper() for wp in watched_players):
            await interaction.response.edit_message(
                content=t('warnotifications.buddy_already_added', user_id=self.user_id, guild_id=self.guild_id, player_tag=player_tag),
                view=None
            )
            return

        if any(p.get("player_tag", "").upper() == player_tag.upper() for p in own_players):
            await interaction.response.edit_message(
                content=t('warnotifications.buddy_own_account', user_id=self.user_id, guild_id=self.guild_id, player_tag=player_tag),
                view=None
            )
            return

        watched_players.append({"player_tag": player_tag, "player_name": player_name})
        user_data["watched_players"] = watched_players
        CACHE.user_accounts[self.user_id] = user_data
        await CACHE.persist_user(self.user_id)

        logging.info(f"USER ACTION: {interaction.user} added buddy {player_tag} ({player_name}) to watch list via name search")

        from qapbot.QBdiscocmdshelper import format_notification_settings
        settings_text = format_notification_settings(user_data, interaction.user.display_name, user_id=self.user_id, guild_id=interaction.guild_id)
        notif_settings = user_data.get("notification_settings", {})
        new_view = UnifiedNotificationView(
            user_id=self.user_id,
            is_enabled=notif_settings.get("war_reminders", False),
            current_type=notif_settings.get("notification_type", "all_wars"),
            current_mode=notif_settings.get("notification_mode", "repeated"),
            original_interaction=self.original_interaction
        )

        # Delete the dropdown message, then update the main settings message
        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.followup.delete_message(interaction.message.id if interaction.message else 0)
        except Exception as e:
            logging.warning(f"Failed to delete buddy select dropdown: {e}")
        if self.original_interaction:
            try:
                await self.original_interaction.edit_original_response(content=settings_text, view=new_view)
            except Exception as e:
                logging.warning(f"Failed to update notification settings after buddy select: {e}")


class LinkBuddyModal(discord.ui.Modal, title="Link Buddy Account"):
    """Modal for searching a CoC player by name or tag substring and adding them to the buddy watch list."""

    # Labels remain in English — discord.py Modal lifecycle requires class-level label definitions.
    # info_display is a read-only paragraph whose `default` text is translated at runtime.
    info_display = discord.ui.TextInput(
        label="ℹ️  Save your Buddy",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=300,
        default="You will receive war attack reminders for this player — in the same DM and at the same times as your own accounts."
    )
    search_input = discord.ui.TextInput(
        label="Player Name or Tag",
        placeholder="e.g. PlayerName or #ABC123XY",
        min_length=2,
        max_length=30,
        required=True
    )

    def __init__(self, user_id: str, parent_view: 'UnifiedNotificationView', original_interaction: Optional[discord.Interaction], timeout: int = 180):
        """
        Initialize LinkBuddyModal.

        Args:
            user_id: Discord user ID of the watcher
            parent_view: The parent UnifiedNotificationView to return to
            original_interaction: The interaction that spawned this modal flow
            timeout: Modal timeout in seconds
        """
        super().__init__()
        self.user_id = user_id
        self.parent_view = parent_view
        self.original_interaction = original_interaction
        self.guild_id = original_interaction.guild_id if original_interaction else None

        # Translate info text and placeholder — labels are class-level and cannot be translated at runtime
        self.info_display.default = t('warnotifications.buddy_modal_info', user_id=self.user_id, guild_id=self.guild_id)
        self.search_input.placeholder = t('warnotifications.buddy_modal_placeholder', user_id=self.user_id, guild_id=self.guild_id)

    async def on_submit(self, interaction: discord.Interaction):
        """Handle modal submission: search by name/tag, resolve via CoC API, add buddy."""
        await interaction.response.defer(ephemeral=True)

        raw_input = self.search_input.value.strip()
        substr = raw_input.lower()

        user_data = CACHE.user_accounts.get(self.user_id, {})
        watched_players: List[Dict[str, Any]] = user_data.get("watched_players", [])
        own_players: List[Dict[str, Any]] = user_data.get("players", [])

        # Build player list for name/substring search (all known players in guild clans)
        from qapbot.QBdiscocmdshelper import get_guild_clans_including_member_config, get_player_list, normalize_clan_tag

        player_list: List[Dict[str, Any]] = []
        if self.guild_id:
            guild_clans = get_guild_clans_including_member_config(self.guild_id)
            if guild_clans:
                player_list = await get_player_list(guild_clans, exclude_registered_players=False, use_live_api=False)

        # Search: name substring match OR exact/partial tag match (normalise '#' prefix)
        substr_notag = substr.lstrip("#")
        already_tracked = {
            wp.get("player_tag", "").upper() for wp in watched_players
        } | {
            p.get("player_tag", "").upper() for p in own_players
        }
        matches = [
            p for p in player_list
            if (substr in p.get("name", "").lower()
                or substr_notag == p.get("tag", "").lstrip("#").lower())
            and p.get("tag", "").upper() not in already_tracked
        ]

        if not matches:
            # No local match — try direct CoC API lookup if input looks like a tag
            direct_tag = normalize_clan_tag(raw_input)
            if direct_tag:
                player_obj = await CACHE.get_player(direct_tag)
                if player_obj:
                    await self._add_buddy(interaction, player_obj.tag, player_obj.name, user_data, watched_players, own_players)
                    return
            # Not found anywhere — reject without adding
            await interaction.followup.send(
                t('warnotifications.buddy_not_found', user_id=self.user_id, guild_id=self.guild_id, player_tag=raw_input),
                ephemeral=True
            )
            return

        if len(matches) == 1:
            p = matches[0]
            # Resolve via CoC API for a fresh/canonical name
            player_obj = await CACHE.get_player(p["tag"])
            name = player_obj.name if player_obj else p.get("name", p["tag"])
            await self._add_buddy(interaction, p["tag"], name, user_data, watched_players, own_players)
            return

        if len(matches) <= 25:
            # Multiple matches — show selection dropdown
            select_view = BuddyPlayerSelectView(
                user_id=self.user_id,
                guild_id=self.guild_id,
                matches=matches,
                original_interaction=self.original_interaction,
                parent_view=self.parent_view
            )
            select_view.message = await interaction.followup.send(
                t('warnotifications.buddy_select_prompt', user_id=self.user_id, guild_id=self.guild_id, count=len(matches)),
                view=select_view,
                ephemeral=True
            )
        else:
            # Too many matches — ask for a more specific search
            await interaction.followup.send(
                t('warnotifications.buddy_too_many_matches', user_id=self.user_id, guild_id=self.guild_id, count=len(matches)),
                ephemeral=True
            )

    async def _add_buddy(self, interaction: discord.Interaction, player_tag: str, player_name: str,
                         user_data: Dict[str, Any], watched_players: List[Dict[str, Any]],
                         own_players: List[Dict[str, Any]]):
        """Internal helper: validate and persist a resolved player as a buddy."""
        import logging

        if any(wp.get("player_tag", "").upper() == player_tag.upper() for wp in watched_players):
            await interaction.followup.send(
                t('warnotifications.buddy_already_added', user_id=self.user_id, guild_id=self.guild_id, player_tag=player_tag),
                ephemeral=True
            )
            return

        if any(p.get("player_tag", "").upper() == player_tag.upper() for p in own_players):
            await interaction.followup.send(
                t('warnotifications.buddy_own_account', user_id=self.user_id, guild_id=self.guild_id, player_tag=player_tag),
                ephemeral=True
            )
            return

        watched_players.append({"player_tag": player_tag, "player_name": player_name})
        user_data["watched_players"] = watched_players
        CACHE.user_accounts[self.user_id] = user_data
        await CACHE.persist_user(self.user_id)

        logging.info(f"USER ACTION: {interaction.user} added buddy {player_tag} ({player_name}) to watch list")

        from qapbot.QBdiscocmdshelper import format_notification_settings
        settings_text = format_notification_settings(user_data, interaction.user.display_name, user_id=self.user_id, guild_id=interaction.guild_id)
        notif_settings = user_data.get("notification_settings", {})
        new_view = UnifiedNotificationView(
            user_id=self.user_id,
            is_enabled=notif_settings.get("war_reminders", False),
            current_type=notif_settings.get("notification_type", "all_wars"),
            current_mode=notif_settings.get("notification_mode", "repeated"),
            original_interaction=self.original_interaction
        )

        if self.original_interaction:
            try:
                await self.original_interaction.edit_original_response(content=settings_text, view=new_view)
            except Exception as e:
                logging.warning(f"Failed to update notification settings message after buddy add: {e}")


class RemoveBuddyView(discord.ui.View):
    """View with a select dropdown to remove a buddy from the watch list."""

    def __init__(self, user_id: str, watched_players: List[Dict[str, Any]], parent_view: 'UnifiedNotificationView', original_interaction: Optional[discord.Interaction], timeout: int = 180):
        """
        Initialize RemoveBuddyView.

        Args:
            user_id: Discord user ID
            watched_players: Current buddy list from user_data
            parent_view: Parent UnifiedNotificationView to return to after removal
            original_interaction: The original interaction
            timeout: View timeout in seconds
        """
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.watched_players = watched_players
        self.parent_view = parent_view
        self.original_interaction = original_interaction
        self.guild_id = original_interaction.guild_id if original_interaction else None
        self.message: Optional[discord.Message] = None

        # Build select options for each buddy
        options = []
        for buddy in watched_players[:25]:  # Discord limit: max 25 options
            b_tag = buddy.get("player_tag", "Unknown")
            b_name = buddy.get("player_name", b_tag)
            options.append(discord.SelectOption(
                label=f"{b_name} ({b_tag})",
                value=b_tag,
                emoji="👥"
            ))

        select = discord.ui.Select(
            placeholder=t('warnotifications.buddy_removed_placeholder', user_id=self.user_id, guild_id=self.guild_id),
            min_values=1,
            max_values=1,
            options=options  # type: ignore[arg-type]
        )
        select.callback = self.buddy_selected
        self.add_item(select)  # type: ignore[arg-type]

    async def on_timeout(self) -> None:
        """Delete the message when the view times out."""
        if self.message is not None:
            try:
                await self.message.delete()
            except Exception:
                pass

    async def buddy_selected(self, interaction: discord.Interaction):
        """Handle buddy selection for removal."""
        import logging
        selected_tag = interaction.data['values'][0]  # type: ignore[index]

        user_data = CACHE.user_accounts.get(self.user_id, {})
        watched_players: List[Dict[str, Any]] = user_data.get("watched_players", [])

        # Find and remove the selected buddy
        removed_buddy = next((wp for wp in watched_players if wp.get("player_tag", "").upper() == selected_tag.upper()), None)
        if removed_buddy:
            watched_players = [wp for wp in watched_players if wp.get("player_tag", "").upper() != selected_tag.upper()]
            user_data["watched_players"] = watched_players
            CACHE.user_accounts[self.user_id] = user_data
            await CACHE.persist_user(self.user_id)

            player_name = removed_buddy.get("player_name", selected_tag)
            logging.info(f"USER ACTION: {interaction.user} removed buddy {selected_tag} ({player_name}) from watch list")

        from qapbot.QBdiscocmdshelper import format_notification_settings
        settings_text = format_notification_settings(user_data, interaction.user.display_name, user_id=self.user_id, guild_id=interaction.guild_id)
        notif_settings = user_data.get("notification_settings", {})
        new_view = UnifiedNotificationView(
            user_id=self.user_id,
            is_enabled=notif_settings.get("war_reminders", False),
            current_type=notif_settings.get("notification_type", "all_wars"),
            current_mode=notif_settings.get("notification_mode", "repeated"),
            original_interaction=self.original_interaction
        )
        new_view.message = self.message

        await interaction.response.edit_message(content=settings_text, view=new_view)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Ensure only the owner can interact (and maintenance guard via super)."""
        if not await super().interaction_check(interaction):
            return False
        if str(interaction.user.id) != self.user_id:
            guild_id = interaction.guild.id if interaction.guild else None
            message = t('warnotifications.error_not_your_settings', user_id=str(interaction.user.id), guild_id=guild_id)
            await interaction.response.send_message(message, ephemeral=True)
            return False
        return True


# ============================================================================
# Clan Management Components
# ============================================================================




class NotificationSettingsView(discord.ui.View):
    """
    Unified notification settings interface for clan-wide or user-specific configuration.
    
    Provides selectors for:
    - User selector (user scope only, with pagination)
    - Notification status (Enable/Disable)
    - Notification mode (Once / Repeated)
    - Notification type (All Wars / CWL Only)
    - Apply button (scope-specific action)
    
    Args:
        scope: "clan" for clan-wide settings, "user" for user-specific settings
        clan_tag: Clan tag for context
        sent_message: The clan management message to update
        guild_clans: List of all guild clan tags
        mode: Current clan management mode
        users_in_clan: Dict of users in clan (user scope only)
        guild: Discord guild (user scope only)
        timeout: View timeout in seconds
    """
    def __init__(
        self,
        scope: str,
        clan_tag: str,
        sent_message: discord.Message,
        guild_clans: List[str],
        mode: str = "notifications",
        users_in_clan: Optional[Dict[str, Dict[str, Any]]] = None,
        guild: Optional[discord.Guild] = None,
        timeout: int = 300
    ):
        """Initialize notification settings view with specified scope."""
        super().__init__(timeout=timeout)
        self.scope = scope  # "clan" or "user"
        self.clan_tag = clan_tag
        self.sent_message = sent_message
        self.guild_clans = guild_clans
        self.mode = mode
        self.users_in_clan = users_in_clan  # Only for user scope
        self.guild = guild  # Only for user scope
        
        # Pagination state for user selector (user scope only)
        self.user_offset = 0
        self.users_per_page = 24
        
        # Selected values
        self.selected_user_id: Optional[str] = None  # Only for user scope
        self.notification_enabled: Optional[bool] = None
        self.notification_type: Optional[str] = None
        self.notification_mode: Optional[str] = None
        
        # Add UI components based on scope
        if self.scope == "user":
            self._add_user_select()  # row 0  # type: ignore[attr-defined]
            status_row, mode_row, type_row, apply_row = 1, 2, 3, 4
        else:  # clan scope
            status_row, mode_row, type_row, apply_row = 0, 1, 2, 3
        
        self._add_notification_status_select(row=status_row)
        self._add_notification_mode_select(row=mode_row)
        self._add_notification_type_select(row=type_row)
        self._add_apply_button(row=apply_row)
    
    def _add_user_select(self):
        """Add dropdown for user selection with pagination (user scope only)."""
        if self.scope != "user" or not self.users_in_clan:
            return
        
        user_ids = list(self.users_in_clan.keys())
        total_users = len(user_ids)
        
        # Calculate pagination
        start_idx = self.user_offset
        end_idx = min(start_idx + self.users_per_page, total_users)
        has_more = end_idx < total_users
        
        # Get current page of users
        page_user_ids = user_ids[start_idx:end_idx]
        
        options = []
        
        # Add "Back to first page" option if not on first page
        if self.user_offset > 0:
            from qapbot.i18n import t
            guild_id = self.guild.id if self.guild else None
            options.append(discord.SelectOption(
                label=t('ui_components.notification_settings.button_back_to_first', guild_id=guild_id),
                value="__back_to_first__"
            ))
        
        # Add user options
        for user_id in page_user_ids:
            user_info = self.users_in_clan[user_id]
            display_name = user_info.get('display_name', f'User {user_id}')
            players = user_info.get('players', [])
            player_count = len(players)
            
            options.append(discord.SelectOption(
                label=f"{display_name} ({player_count} players)",
                value=user_id,
                description=f"Players: {', '.join(p['player_name'][:20] for p in players[:3])}",
                default=(user_id == self.selected_user_id)
            ))
        
        # Add "Load more..." option if there are more users
        if has_more:
            from qapbot.i18n import t
            guild_id = self.guild.id if self.guild else None
            options.append(discord.SelectOption(
                label=f"📄 {t('ui_components.notification_settings.button_load_more', guild_id=guild_id)}",
                value="__load_more__"
            ))
        
        # Show pagination info in placeholder
        from qapbot.i18n import t
        guild_id = self.guild.id if self.guild else None
        
        if self.user_offset > 0:
            placeholder = t('ui_components.user_notification_settings.placeholder_pagination', guild_id=guild_id, start=start_idx+1, end=end_idx, total=total_users)
        else:
            placeholder = t('ui_components.user_notification_settings.placeholder_select', guild_id=guild_id, total=total_users)
        
        select = discord.ui.Select(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,  # type: ignore[arg-type]
            custom_id=f"{self.scope}_notif_user",
            row=0
        )
        select.callback = self._on_user_select  # type: ignore[assignment]
        self.add_item(select)  # type: ignore[arg-type]
    
    def _add_notification_status_select(self, row: int = 0):
        """Add dropdown for enable/disable notifications."""
        from qapbot.i18n import t
        guild_id = self.guild.id if self.guild else None
        
        options = [
            discord.SelectOption(
                label=t('ui_components.notification_settings.label_enable', guild_id=guild_id),
                value="enabled",
                emoji=BotEmojis.ENABLED,
                default=(self.notification_enabled is True)
            ),
            discord.SelectOption(
                label=t('ui_components.notification_settings.label_disable', guild_id=guild_id),
                value="disabled",
                emoji=BotEmojis.DISABLED,
                default=(self.notification_enabled is False)
            )
        ]
        
        select = discord.ui.Select(
            placeholder=t('ui_components.notification_settings.placeholder_status', guild_id=guild_id),
            min_values=1,
            max_values=1,
            options=options,  # type: ignore[arg-type]
            custom_id=f"{self.scope}_notif_status",
            row=row
        )
        select.callback = self._on_status_select  # type: ignore[assignment]
        self.add_item(select)  # type: ignore[arg-type]
    
    def _add_notification_type_select(self, row: int = 2):
        """Add dropdown for notification type (all wars / CWL only)."""
        from qapbot.i18n import t
        guild_id = self.guild.id if self.guild else None
        
        options = [
            discord.SelectOption(
                label=t('ui_components.notification_settings.label_all_wars', guild_id=guild_id),
                value="all_wars",
                emoji=BotEmojis.ALLWARS,
                description=t('ui_components.notification_settings.desc_all_wars', guild_id=guild_id),
                default=(self.notification_type == "all_wars")
            ),
            discord.SelectOption(
                label=t('ui_components.notification_settings.label_cwl_only', guild_id=guild_id),
                value="cwl_only",
                emoji=BotEmojis.CWL,
                description=t('ui_components.notification_settings.desc_cwl_only', guild_id=guild_id),
                default=(self.notification_type == "cwl_only")
            )
        ]
        
        select = discord.ui.Select(
            placeholder=t('ui_components.notification_settings.placeholder_type', guild_id=guild_id),
            min_values=1,
            max_values=1,
            options=options,  # type: ignore[arg-type]
            custom_id=f"{self.scope}_notif_type",
            row=row
        )
        select.callback = self._on_type_select  # type: ignore[assignment]
        self.add_item(select)  # type: ignore[arg-type]
    
    def _add_notification_mode_select(self, row: int = 1):
        """Add dropdown for notification mode (once / repeated)."""
        from qapbot.i18n import t
        guild_id = self.guild.id if self.guild else None
        
        options = [
            discord.SelectOption(
                label=t('ui_components.notification_settings.label_repeated', guild_id=guild_id),
                value="repeated",
                emoji=BotEmojis.REPEATED,
                description=t('ui_components.notification_settings.desc_repeated', guild_id=guild_id),
                default=(self.notification_mode == "repeated")
            ),
            discord.SelectOption(
                label=t('ui_components.notification_settings.label_once', guild_id=guild_id),
                value="once",
                emoji=BotEmojis.ONCE,
                description=t('ui_components.notification_settings.desc_once', guild_id=guild_id),
                default=(self.notification_mode == "once")
            )
        ]
        
        select = discord.ui.Select(
            placeholder=t('ui_components.notification_settings.placeholder_mode', guild_id=guild_id),
            min_values=1,
            max_values=1,
            options=options,  # type: ignore[arg-type]
            custom_id=f"{self.scope}_notif_mode",
            row=row
        )
        select.callback = self._on_mode_select  # type: ignore[assignment]
        self.add_item(select)  # type: ignore[arg-type]
    
    def _add_apply_button(self, row: int = 3):
        """Add button to apply settings (scope-specific)."""
        from qapbot.i18n import t
        guild_id = self.guild.id if self.guild else None
        
        # Check if all required selections are made
        if self.scope == "user":
            all_selected = (
                self.selected_user_id is not None and
                self.notification_enabled is not None and
                self.notification_type is not None and
                self.notification_mode is not None
            )
            button_label = t('ui_components.notification_settings.button_apply_user', guild_id=guild_id)
        else:  # clan scope
            all_selected = (
                self.notification_enabled is not None and
                self.notification_type is not None and
                self.notification_mode is not None
            )
            button_label = t('ui_components.notification_settings.button_apply_all', guild_id=guild_id)
        
        apply_button = discord.ui.Button(
            label=f"✅ {button_label}",
            style=discord.ButtonStyle.success,
            custom_id=f"{self.scope}_notif_apply",
            row=row,
            disabled=not all_selected
        )
        apply_button.callback = self._on_apply  # type: ignore[assignment]
        self.add_item(apply_button)  # type: ignore[arg-type]
    
    def _rebuild_view(self):
        """Rebuild view with current selections."""
        self.clear_items()
        
        if self.scope == "user":
            self._add_user_select()  # type: ignore[attr-defined]
            self._add_notification_status_select(row=1)
            self._add_notification_mode_select(row=2)
            self._add_notification_type_select(row=3)
            self._add_apply_button(row=4)
        else:  # clan scope
            self._add_notification_status_select(row=0)
            self._add_notification_mode_select(row=1)
            self._add_notification_type_select(row=2)
            self._add_apply_button(row=3)
    
    async def _on_user_select(self, interaction: discord.Interaction) -> None:
        """Handle user selection (user scope only)."""
        value = interaction.data['values'][0]  # type: ignore[index]
        
        # Check for pagination actions
        if value == "__back_to_first__":
            await interaction.response.defer(thinking=False, ephemeral=False)
            self.user_offset = 0
            self._rebuild_view()
            await interaction.edit_original_response(view=self)
            
        elif value == "__load_more__":
            await interaction.response.defer(thinking=False, ephemeral=False)
            self.user_offset += self.users_per_page
            self._rebuild_view()
            await interaction.edit_original_response(view=self)
        else:
            await interaction.response.defer(thinking=False, ephemeral=False)
            self.selected_user_id = value
            
            # Pre-fill with user's current notification settings
            from qapbot.cache_manager import CACHE
            user_data = CACHE.user_accounts.get(value, {})  # type: ignore[arg-type]
            if isinstance(user_data, dict):  # type: ignore[misc]
                notif_settings = user_data.get('notification_settings', {})
                self.notification_enabled = notif_settings.get('war_reminders')
                self.notification_type = notif_settings.get('notification_type')
                self.notification_mode = notif_settings.get('notification_mode')
            
            self._rebuild_view()
            await interaction.edit_original_response(view=self)
    
    async def _on_status_select(self, interaction: discord.Interaction) -> None:
        """Handle notification status selection."""
        await interaction.response.defer(thinking=False, ephemeral=False)
        
        value = interaction.data['values'][0]  # type: ignore[index]
        self.notification_enabled = (value == "enabled")
        
        self._rebuild_view()
        await interaction.edit_original_response(view=self)
    
    async def _on_type_select(self, interaction: discord.Interaction) -> None:
        """Handle notification type selection."""
        await interaction.response.defer(thinking=False, ephemeral=False)
        
        self.notification_type = interaction.data['values'][0]  # type: ignore[index]
        
        self._rebuild_view()
        await interaction.edit_original_response(view=self)
    
    async def _on_mode_select(self, interaction: discord.Interaction) -> None:
        """Handle notification mode selection."""
        await interaction.response.defer(thinking=False, ephemeral=False)
        
        self.notification_mode = interaction.data['values'][0]  # type: ignore[index]
        
        self._rebuild_view()
        await interaction.edit_original_response(view=self)
    
    async def _on_apply(self, interaction: discord.Interaction) -> None:
        """Apply selected settings (scope-specific logic)."""
        await interaction.response.defer(thinking=False, ephemeral=True)
        
        from qapbot.cache_manager import CACHE
        from qapbot.QBdiscocmdshelper import format_clan_management_message

        affected_users: set[str] = set()
        
        if self.scope == "clan":
            # Clan-wide: Update all users with players in this clan
            for user_id, user_data in CACHE.user_accounts.items():
                if isinstance(user_data, dict):  # type: ignore[misc]
                    user_players = user_data.get('players', [])
                    # Check if this user has any players with current_clan_tag matching this clan
                    has_clan_player = any(
                        isinstance(p, dict) and p.get('current_clan_tag') == self.clan_tag
                        for p in user_players
                    )
                    
                    if has_clan_player:
                        # Update user-level notification settings
                        if 'notification_settings' not in user_data:
                            user_data['notification_settings'] = {}
                        
                        user_data['notification_settings']['war_reminders'] = self.notification_enabled
                        user_data['notification_settings']['notification_type'] = self.notification_type
                        user_data['notification_settings']['notification_mode'] = self.notification_mode
                        affected_users.add(user_id)
            
            # Save changes
            save_needed = len(affected_users) > 0
            
        else:  # user scope
            # User-specific: Update only the selected user
            if not self.selected_user_id:
                return
            user_data = CACHE.user_accounts.get(self.selected_user_id, {})
            if isinstance(user_data, dict):  # type: ignore[misc]
                # Ensure notification_settings dict exists
                if 'notification_settings' not in user_data:
                    user_data['notification_settings'] = {}
                
                # Update user-level notification settings
                user_data['notification_settings']['war_reminders'] = self.notification_enabled
                user_data['notification_settings']['notification_type'] = self.notification_type
                user_data['notification_settings']['notification_mode'] = self.notification_mode
                
                save_needed = True
            else:
                save_needed = False
        
        # Save changes if needed
        if save_needed:
            if self.scope == "clan":
                for uid in affected_users:
                    await CACHE.persist_user(uid)
            elif self.selected_user_id:
                await CACHE.persist_user(self.selected_user_id)
            
            # Refresh the clan management view with ALL pages regenerated
            try:
                if not interaction.guild:
                    return
                from qapbot.ui_clan_management import ClanManagementView
                main_embed, unlinked_embed, _, unlinked_players = await format_clan_management_message(
                    self.clan_tag,
                    interaction.guild,
                    mode=self.mode
                )
                
                # Determine pagination mode and rebuild view accordingly
                if main_embed is None and isinstance(unlinked_embed, list):  # type: ignore[misc]
                    # Pagination mode (Rule 3): multiple embeds returned
                    all_embeds = unlinked_embed
                    display_embeds = [all_embeds[0]]  # Show first page
                    
                    # Rebuild ClanManagementView with all updated embeds for pagination
                    new_view = ClanManagementView(
                        clan_tag=self.clan_tag,
                        guild_clans=self.guild_clans,
                        unlinked_players=unlinked_players,
                        sent_message=self.sent_message,
                        mode=self.mode,
                        timeout=1800,
                        all_embeds=all_embeds,  # type: ignore[arg-type]
                        current_page=0  # Reset to first page after update
                    )
                    await self.sent_message.edit(embeds=display_embeds, view=new_view)
                    
                elif main_embed is not None and unlinked_embed is not None and not isinstance(unlinked_embed, list):  # type: ignore[misc]
                    # Two-embed mode (Rule 2): display both embeds
                    display_embeds = [main_embed, unlinked_embed]
                    
                    # Rebuild ClanManagementView without pagination
                    new_view = ClanManagementView(
                        clan_tag=self.clan_tag,
                        guild_clans=self.guild_clans,
                        unlinked_players=unlinked_players,
                        sent_message=self.sent_message,
                        mode=self.mode,
                        timeout=1800
                    )
                    await self.sent_message.edit(embeds=display_embeds, view=new_view)
                    
                else:
                    # Single-embed mode (Rule 1): display one embed
                    display_embeds = [main_embed]
                    
                    # Rebuild ClanManagementView without pagination
                    new_view = ClanManagementView(
                        clan_tag=self.clan_tag,
                        guild_clans=self.guild_clans,
                        unlinked_players=unlinked_players,
                        sent_message=self.sent_message,
                        mode=self.mode,
                        timeout=1800
                    )
                    await self.sent_message.edit(embeds=display_embeds, view=new_view)
                    
            except Exception as e:
                logging.error(f"Failed to refresh clan management view after {self.scope} settings update: {e}")



