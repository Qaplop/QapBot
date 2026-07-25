from __future__ import annotations
# pyright: reportUnknownParameterType=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""
Player registration, account management, and registration flow UI components.

Contains registration message views, account linking, verification modals,
and player search/selection components.
"""
import asyncio
import discord
import logging
from typing import List, Dict, Callable, Any, Optional, Set

from qapbot.i18n import t  # type: ignore[reportUnusedImport]  # tests monkeypatch ui_registration.t
from qapbot.cache_manager import CACHE
from qapbot.ui_common import GenericSelectView, update_user_metadata_from_interaction
from qapbot.ui_notifications import WarNotificationPromptView


async def _clan_filter_callback(
    interaction: discord.Interaction,
    selected_clan_tag: str,
    substring: str,
    api_token: str,
    guild_id_param: int,
    clans_in_guild: List[str]
):
    """
    Callback for clan selection when filtering too many player matches.
    Re-shows substring modal with players filtered to selected clan.
    
    Args:
        interaction: Discord interaction from clan select
        selected_clan_tag: The selected clan tag to filter by
        substring: Original substring that user entered
        api_token: Optional API token from original modal
        guild_id: Guild ID for context
        clans_in_guild: Full list of clans in guild
    """
    from qapbot.QBdiscocmdshelper import get_player_list
    
    logging.debug(f"_clan_filter_callback: Selected clan {selected_clan_tag}, substring '{substring}'")
    
    # Get players from cache only — live API call would block this interaction response (3-second window)
    player_list_with_activity = await get_player_list(
        [selected_clan_tag],
        exclude_registered_players=True,
        use_live_api=False
    )
    player_list = [{"tag": p["tag"], "name": p["name"], "is_current_member": p.get("is_current_member", False)} for p in player_list_with_activity]
    
    logging.debug(f"_clan_filter_callback: Got {len(player_list)} total players from clan {selected_clan_tag}")
    
    if not player_list:
        from qapbot.i18n import t
        user_id = str(interaction.user.id)
        guild_id_for_t = interaction.guild.id if interaction.guild else None
        message = t('playerregistration.no_linkable_players', user_id=user_id, guild_id=guild_id_for_t)
        await interaction.response.send_message(message, ephemeral=True)
        return
    
    # Filter by the original substring (name only - tag filtering not useful after clan selection)
    matches = [
        p for p in player_list
        if substring.lower() in (p.get("name", "").lower())
    ]
    
    logging.debug(f"_clan_filter_callback: After substring filter, {len(matches)} matches for '{substring}' in clan {selected_clan_tag}")
    logging.debug(f"_clan_filter_callback: Sample matches: {[p.get('name', 'Unknown') for p in matches[:5]]}")
    
    # HEURISTIC: If still > 25 matches, filter to current clan members only (exclude historical players)
    if len(matches) > 25:
        current_members_only = [p for p in matches if p.get("is_current_member", False)]
        if current_members_only:
            logging.debug(f"_clan_filter_callback: Applied current member filter: {len(matches)} → {len(current_members_only)} matches")
            matches = current_members_only
        else:
            logging.debug(f"_clan_filter_callback: No current members found in matches, keeping all {len(matches)} matches")
    
    # If still > 25 after all filtering, ask for more specific substring
    if len(matches) > 25:
        from qapbot.i18n import t
        user_id = str(interaction.user.id)
        guild_id_for_t = interaction.guild.id if interaction.guild else None
        message = t('playerregistration.too_many_matches_specific', user_id=user_id, guild_id=guild_id_for_t, count=len(matches), substring=substring)
        await interaction.response.send_message(
            message,
            ephemeral=True
        )
        return
    
    # If 0 matches, inform user
    if len(matches) == 0:
        from qapbot.i18n import t
        user_id = str(interaction.user.id)
        guild_id: int = interaction.guild.id if interaction.guild else 0
        message = t('playerregistration.no_matches', user_id=user_id, guild_id=guild_id)
        await interaction.response.send_message(
            message,
            ephemeral=True
        )
        return
    
    # 1-25 matches - process them
    from qapbot.QBdiscocmdshelper import process_player_registration
    
    # Single match - register directly
    if len(matches) == 1:
        p = matches[0]
        await process_player_registration(
            interaction, p["tag"], p.get("name", "Unknown"), api_token, use_followup=False
        )
        return
    
    # Multiple matches (2-25) - show selection dropdown
    player_options = []
    for p in matches:
        tag = p.get("tag", "")
        name = p.get("name", "Unknown")
        label = f"{name} ({tag})" if tag else name
        if tag:
            player_options.append(discord.SelectOption(label=label[:100], value=tag))  # type: ignore[arg-type]
    
    from qapbot.i18n import t
    view = GenericSelectView(
        options=player_options,  # type: ignore[arg-type]
        callback_fn=_user_player_select_callback,
        placeholder=t('playerregistration.select_player', guild_id=guild_id_param),
        timeout=600,
        callback_kwargs={
            "matches": matches,
            "api_token": api_token,
            "process_registration_fn": process_player_registration,
            "clan_selection_interaction": interaction  # Pass for deletion in player callback
        }
    )
    
    # Delete clan selection message and show player selection
    from qapbot.i18n import t
    user_id = str(interaction.user.id)
    await interaction.response.edit_message(content=t('ui_components.player_selection.multiple_matches', user_id=user_id, guild_id=guild_id_param), view=view)


async def _user_player_select_callback(
    interaction: discord.Interaction,
    selected_player_tag: str,
    matches: List[Dict[str, str]],
    api_token: str,
    process_registration_fn: Callable[..., Any],  # type: ignore[type-arg]
    clan_selection_interaction: Optional[discord.Interaction] = None
):
    """
    Callback for user player selection (user registration workflow).
    Processes player registration.
    
    Args:
        interaction: Discord interaction (player dropdown selection)
        selected_player_tag: The selected player tag
        matches: List of player dicts with 'tag' and 'name' keys for name lookup
        api_token: CoC API token for verification
        process_registration_fn: Async function to process player registration
        clan_selection_interaction: Optional clan selection interaction to delete
    """
    # Defer WITHOUT thinking indicator to keep the message visible initially
    try:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
    except Exception:
        pass
    
    # Delete clan selection message if it exists (from prior clan filtering step)
    if clan_selection_interaction:
        try:
            await clan_selection_interaction.delete_original_response()
            logging.debug("Successfully deleted clan selection message from player callback")
        except Exception as e:
            logging.debug(f"Could not delete clan selection message: {e}")

    player_name = "Unknown"
    for p in matches:
        if p.get("tag") == selected_player_tag:
            player_name = p.get("name", "Unknown")
            break

    # Pass the player selection interaction so we can delete the message before sending result
    await process_registration_fn(
        interaction, selected_player_tag, player_name, api_token, use_followup=True, player_selection_interaction=interaction
    )


async def _show_player_search_modal(interaction: discord.Interaction, guild_id: int, user_mentioned: bool = False, action_view_interaction: Optional[discord.Interaction] = None):
    """
    Show player search modal with all subscribed clan members for registration.
    
    FLOW:
    1. Show PlayerSubstringModal immediately with ALL players from ALL subscribed clans
    2. User enters player name/tag substring (and optional API token)
    3. Modal callback handles matching:
       - No matches: Try direct CoC API tag lookup
       - 1 match: Proceed to registration
       - 2-25 matches: Show player selection dropdown
       - >25 matches: Show clan filter dropdown (if multiple clans) OR ask for more specific search
    
    Args:
        interaction: Discord interaction object
        guild_id: Guild ID to get subscribed clans from
        user_mentioned: Whether @user was already mentioned in a previous message (to avoid repeating)
        action_view_interaction: Optional interaction to delete action view message before showing modal
    """
    # PlayerSubstringModal is defined in this module
    from qapbot.QBdiscocmdshelper import get_player_list, get_guild_clans_including_member_config
    from qapbot.i18n import t
    
    # Get all clans (subscribed + member configurations) available in this guild
    clans_in_guild = get_guild_clans_including_member_config(guild_id)

    # Delete action view message if coming from "Link new account" flow
    if action_view_interaction:
        try:
            await action_view_interaction.delete_original_response()
            logging.debug("Successfully deleted action view message")
        except discord.NotFound:
            logging.debug("Action view message already deleted")
        except Exception as e:
            logging.debug(f"Could not delete action view message: {e}")

    if not clans_in_guild:
        user_id_local = str(interaction.user.id)
        guild_id_for_t = interaction.guild.id if interaction.guild else None
        message = t('playerregistration.no_clans_available', user_id=user_id_local, guild_id=guild_id_for_t)
        await interaction.response.send_message(message, ephemeral=True)
        return
    
    # Get players from cache only — live API calls here block the Discord interaction response.
    # Discord requires a response within 3 seconds; sequential API calls across 10+ clans easily exceed this.
    # Players not in war history can still register by entering their #TAG directly in the modal.
    player_list_with_activity = await get_player_list(
        clans_in_guild,
        exclude_registered_players=True,
        use_live_api=False
    )
    # Convert to format for PlayerSubstringModal
    player_list = [{"tag": p["tag"], "name": p["name"], "is_current_member": p.get("is_current_member", False)} for p in player_list_with_activity]
    
    if not player_list:
        user_id_local = str(interaction.user.id)
        guild_id_for_t = interaction.guild.id if interaction.guild else None
        message = t('playerregistration.no_linkable_players', user_id=user_id_local, guild_id=guild_id_for_t)
        await interaction.response.send_message(
            message,
            ephemeral=True
        )
        return
    
    # NEW: Show modal immediately with ALL players (clan selection happens later if needed)
    # Pass clan list for potential clan filtering in modal callback
    await interaction.response.send_modal(
        PlayerSubstringModal(
            player_list, 
            clan_selection_interaction=None,
            user_id=str(interaction.user.id),
            guild_id=guild_id,
            clans_in_guild=clans_in_guild
        )
    )


class RegistrationView(discord.ui.View):
    """
    Persistent registration message view with account linking button.
    Button stays active indefinitely and triggers clan selection flow.
    """
    def __init__(self, guild_id: int):
        """
        Initialize registration view.

        Args:
            guild_id: Discord guild ID where this view is posted
        """
        super().__init__(timeout=None)  # No timeout - button stays active indefinitely
        self.guild_id = guild_id
        
        # Update button labels with translations
        from qapbot.i18n import t
        self.link_account_button.label = t("playerregistration.button_link_account", guild_id=guild_id)
        self.war_notifications_button.label = t("playerregistration.button_war_notifications", guild_id=guild_id)
        self.api_verification_button.label = t("playerregistration.button_api_verification", guild_id=guild_id)
        self.my_accounts_button.label = t("playerregistration.button_my_accounts", guild_id=guild_id)
    
    @discord.ui.button(label="🔗 Link Account", style=discord.ButtonStyle.primary, row=0, custom_id="registration_link_account")
    async def link_account_button(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[type-arg]
        """Handle the Link account button click - check for unverified accounts first."""
        # Update user metadata from interaction
        await update_user_metadata_from_interaction(interaction)
        
        # AccountActionView is defined in this module
        from qapbot.i18n import t
        
        # Check if user has unverified accounts
        user_id = str(interaction.user.id)
        user_entry = CACHE.user_accounts.get(user_id, {"players": []})
        user_players: List[Dict[str, Any]] = user_entry.get("players", [])  # type: ignore[assignment]
        
        unverified_players = []
        for player in user_players:
            if isinstance(player, dict) and not player.get("verified", False):  # type: ignore[misc]
                unverified_players.append(player)
        
        # If user has unverified accounts, show action selection
        if unverified_players:
            view = AccountActionView(unverified_players, self.guild_id, interaction)  # type: ignore[arg-type]
            message = f"⠀\n{interaction.user.mention} {t('playerregistration.action_prompt', guild_id=self.guild_id)}\n⠀"
            await interaction.response.send_message(
                message,
                view=view,
                ephemeral=True
            )
            view.message = await interaction.original_response()
            return
        
        # Otherwise, proceed with player search modal
        await _show_player_search_modal(interaction, self.guild_id, user_mentioned=False)
    
    @discord.ui.button(label="⚙️ War Notifications", style=discord.ButtonStyle.primary, row=0, custom_id="registration_war_notifications")
    async def war_notifications_button(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[type-arg]
        """Handle War Notifications button click - opens notification settings dialog."""
        # Update user metadata from interaction
        await update_user_metadata_from_interaction(interaction)
        
        from qapbot.i18n import t
        
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        user_id = str(interaction.user.id)
        
        # Check if user has registered players
        user_data = CACHE.user_accounts.get(user_id)
        if not user_data or not user_data.get("players"):
            # Use user_id parameter for ephemeral messages
            guild_id = interaction.guild.id if interaction.guild else None
            message = t('playerregistration.no_accounts_link_first', user_id=user_id, guild_id=guild_id)
            await interaction.followup.send(
                message,
                ephemeral=True
            )
            return
        
        # Get current settings
        from qapbot.QBdiscocmdshelper import format_notification_settings
        from qapbot.ui_notifications import UnifiedNotificationView
        
        try:
            # Get settings text
            guild_id_val = interaction.guild.id if interaction.guild else 0
            settings_text = format_notification_settings(user_data, interaction.user.display_name, user_id=user_id, guild_id=guild_id_val)
            
            # Determine if notifications are enabled
            notif_settings = user_data.get("notification_settings", {})
            is_enabled = notif_settings.get("war_reminders", False)
            current_type = notif_settings.get("notification_type", "all_wars")
            current_mode = notif_settings.get("notification_mode", "repeated")
            
            # Create unified view with all controls
            view = UnifiedNotificationView(
                user_id=user_id,
                is_enabled=is_enabled,
                current_type=current_type,
                current_mode=current_mode,
                original_interaction=interaction
            )
            
            view.message = await interaction.followup.send(settings_text, view=view, ephemeral=True)
            
        except Exception as e:
            from qapbot.i18n import t
            user_id = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            error_msg = t('playerregistration.error_showing_settings', user_id=user_id, guild_id=guild_id, error=str(e))
            await interaction.followup.send(error_msg, ephemeral=True)
            logging.error(f"Error in war notifications button: {e}", exc_info=True)
    
    @discord.ui.button(label="🔑 API Verification", style=discord.ButtonStyle.primary, row=0, custom_id="registration_api_verification")
    async def api_verification_button(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[type-arg]
        """Handle API Verification button click - shows unverified accounts for verification."""
        # Update user metadata from interaction
        await update_user_metadata_from_interaction(interaction)
        
        from qapbot.i18n import t
        
        user_id = str(interaction.user.id)
        user_entry = CACHE.user_accounts.get(user_id, {"players": []})
        user_players: List[Dict[str, Any]] = user_entry.get("players", [])  # type: ignore[assignment]
        
        # Edge case 2: No players linked yet
        if not user_players:
            user_id_local = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            message = t('playerregistration.no_linked_accounts', user_id=user_id_local, guild_id=guild_id)
            await interaction.response.send_message(
                message,
                ephemeral=True
            )
            return
        
        # Find unverified players
        unverified_players = []
        for player in user_players:
            if isinstance(player, dict) and not player.get("verified", False):  # type: ignore[misc]
                unverified_players.append(player)
        
        # Edge case 1: All players already verified
        if not unverified_players:
            user_id_local = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            message = t('playerregistration.all_verified', user_id=user_id_local, guild_id=guild_id)
            await interaction.response.send_message(
                message,
                ephemeral=True
            )
            return
        
        # Show verification selector
        options = []
        for player in unverified_players[:25]:  # Discord limit
            player_tag = player.get("player_tag", "")
            player_name = player.get("player_name", "Unknown")
            
            # Skip if player_tag is empty
            if not player_tag:
                continue
            
            label = t('playerregistration.verify_player', guild_id=self.guild_id, player_name=player_name, player_tag=player_tag)
            options.append(discord.SelectOption(label=label[:100], value=f"verify:{player_tag}"))  # type: ignore[arg-type]
        
        view = GenericSelectView(
            options=options,  # type: ignore[arg-type]
            callback_fn=self._on_verify_select,
            placeholder=t('playerregistration.select_account_verify', guild_id=self.guild_id),
            timeout=600
        )
        
        user_id = str(interaction.user.id)
        guild_id = interaction.guild.id if interaction.guild else None
        prompt = t('playerregistration.verification_prompt', user_id=user_id, guild_id=guild_id)
        await interaction.response.send_message(
            f"⠀\n{prompt}\n⠀",
            view=view,
            ephemeral=True
        )
        view.message = await interaction.original_response()
    
    async def _on_verify_select(self, interaction: discord.Interaction, selected_value: str):
        """Handle verification selection from dropdown."""
        from qapbot.i18n import t
        
        if not selected_value.startswith("verify:"):
            user_id_local = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            message = t('playerregistration.invalid_selection', user_id=user_id_local, guild_id=guild_id)
            await interaction.response.send_message(message, ephemeral=True)
            return
        
        player_tag = selected_value.split(":", 1)[1]
        
        # Find player data
        user_id = str(interaction.user.id)
        user_entry = CACHE.user_accounts.get(user_id, {"players": []})
        user_players: List[Dict[str, Any]] = user_entry.get("players", [])  # type: ignore[assignment]
        
        player_data = None
        for player in user_players:
            p_tag = player.get("player_tag")  # type: ignore[misc]
            if isinstance(player, dict) and p_tag == player_tag:  # type: ignore[misc]
                player_data = player
                break
        
        if not player_data:
            user_id_local = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            message = t('playerregistration.player_not_found', user_id=user_id_local, guild_id=guild_id)
            await interaction.response.send_message(message, ephemeral=True)
            return
        
        # Show verification modal (VerifyAccountModal is defined in this module)
        modal = VerifyAccountModal(player_data, action_view_interaction=interaction, guild_id=self.guild_id)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="📋 My Accounts", style=discord.ButtonStyle.secondary, row=0, custom_id="registration_my_accounts")
    async def my_accounts_button(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[type-arg]
        """Handle My Accounts button click - show account management view."""
        # Update user metadata from interaction
        await update_user_metadata_from_interaction(interaction)
        
        # AccountManagementView is defined in this module
        from qapbot.i18n import t
        
        user_id = str(interaction.user.id)
        user_entry = CACHE.user_accounts.get(user_id, {"players": []})
        user_players: List[Dict[str, Any]] = user_entry.get("players", [])  # type: ignore[assignment]
        
        # Check if user has any linked accounts
        if not user_players:
            user_id_local = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            message = t('playerregistration.account_management_no_accounts', user_id=user_id_local, guild_id=guild_id)
            await interaction.response.send_message(message, ephemeral=True)
            return
        
        # Show account management view
        view = AccountManagementView(user_id, self.guild_id, interaction.user.display_name)
        await view.show_overview(interaction)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:  # type: ignore[type-arg, override]
        """Suppress 'Unknown interaction' (10062) errors caused by expired tokens; re-raise anything else."""
        if isinstance(error, discord.NotFound) and error.code == 10062:
            logging.info(f"[REGISTRATION] Expired interaction for '{getattr(item, 'label', item)}' (10062) — user likely clicked after 3-second window")
            return
        await super().on_error(interaction, error, item)


class AccountActionView(discord.ui.View):
    """
    View for selecting between verifying existing account or linking new account.
    Shows unverified players and a "Link new account" option.
    """
    def __init__(self, unverified_players: List[Dict[str, str]], guild_id: int, action_view_interaction: discord.Interaction):
        """
        Initialize account action view.

        Args:
            unverified_players: List of unverified player dicts with 'player_id' and 'player_name'
            guild_id: Discord guild ID for continuing to clan selection if needed
            action_view_interaction: Original interaction that created the action view message (for deletion)
        """
        super().__init__(timeout=600)
        self.unverified_players = unverified_players
        self.guild_id = guild_id
        self.action_view_interaction = action_view_interaction
        self.message: Optional[discord.Message] = None
        
        # Build options: unverified players + "Link new account"
        from qapbot.i18n import t
        options = []
        for player in unverified_players[:24]:  # Leave room for "Link new account" option
            player_tag = player.get("player_tag", "")
            player_name = player.get("player_name", "Unknown")
            
            # Skip if player_tag is empty
            if not player_tag:
                continue
            
            label = t('playerregistration.verify_player', guild_id=guild_id, player_name=player_name, player_tag=player_tag)
            options.append(discord.SelectOption(label=label[:100], value=f"verify:{player_tag}"))  # type: ignore[arg-type]
        
        # Add "Link new account" option
        options.append(discord.SelectOption(
            label=t('playerregistration.button_link_new', guild_id=guild_id),
            value="link_new",
            description=t('playerregistration.button_link_new_desc', guild_id=guild_id)
        ))
        
        self.select = discord.ui.Select(
            placeholder=t('playerregistration.choose_action', guild_id=guild_id),
            min_values=1,
            max_values=1,
            options=options  # type: ignore[arg-type]
        )
        self.select.callback = self._on_select  # type: ignore[assignment]
        self.add_item(self.select)  # type: ignore[arg-type]

    async def on_timeout(self) -> None:
        """Delete the message when the view times out."""
        if self.message is not None:
            try:
                await self.message.delete()
            except Exception:
                pass
    
    async def _on_select(self, interaction: discord.Interaction) -> None:
        """Handle action selection."""
        from qapbot.i18n import t
        selection = self.select.values[0]
        
        if selection == "link_new":
            # Continue with player search modal (user already mentioned, delete action view first)
            await _show_player_search_modal(interaction, self.guild_id, user_mentioned=True, action_view_interaction=self.action_view_interaction)
            
        elif selection.startswith("verify:"):
            # Extract player_tag and show verification modal
            player_tag = selection.split(":", 1)[1]
            
            # Find the player data
            player_data = None
            for player in self.unverified_players:
                p_tag = player.get("player_tag")
                if p_tag == player_tag:
                    player_data = player
                    break
            
            if not player_data:
                user_id_local = str(interaction.user.id)
                guild_id = interaction.guild.id if interaction.guild else None
                message = t('playerregistration.player_not_found', user_id=user_id_local, guild_id=guild_id)
                await interaction.response.send_message(message, ephemeral=True)
                return
            
            # Show verification modal (pass action_view_interaction for deletion after completion)
            modal = VerifyAccountModal(player_data, self.action_view_interaction, self.guild_id)
            await interaction.response.send_modal(modal)


class VerifyAccountModal(discord.ui.Modal, title="Verify Account"):
    """
    Modal for verifying an existing unverified account with CoC API token.
    
    NOTE: Title is set at class definition (discord.py requirement).
    TextInput labels/placeholders are translated at instantiation.
    """
    
    # TextInput MUST be class attribute for discord.py Modal system
    coc_api_token = discord.ui.TextInput(
        label="CoC API Token",
        required=True,
        max_length=32,
        placeholder="Your API token"
    )
    
    def _translate_inputs(self, guild_id: Optional[int] = None):
        """Translate TextInput labels and placeholders."""
        from qapbot.i18n import t
        self.coc_api_token.label = t('ui_components.modal_label_coc_api_token', guild_id=guild_id)
        self.coc_api_token.placeholder = t('ui_components.modal_placeholder_api_token', guild_id=guild_id)
    
    def __init__(self, player_data: Dict[str, str], action_view_interaction: Optional[discord.Interaction] = None, guild_id: Optional[int] = None, parent_view: Optional['AccountManagementView'] = None):
        """
        Initialize verification modal.

        Args:
            player_data: Player dict with 'player_id' and 'player_name'
            action_view_interaction: Optional interaction to delete action view message after completion
            guild_id: Discord guild ID for translation context
            parent_view: Optional AccountManagementView to refresh after successful verification
        """
        from qapbot.i18n import t
        
        # NOTE: super().__init__() must be called WITHOUT title parameter
        # Title is already set in class definition above
        super().__init__()
        
        # Translate TextInput placeholder after instantiation
        # (Label remains in English due to discord.py Modal lifecycle requirements)
        self.coc_api_token.placeholder = t('ui_components.modals.placeholder_api_token', guild_id=guild_id)
        
        self.guild_id = guild_id
        self.player_data = player_data
        self.action_view_interaction = action_view_interaction
        self.parent_view = parent_view
    
    async def on_submit(self, interaction: discord.Interaction):
        """Handle modal submission - verify the account."""
        from qapbot.QBdiscocmdshelper import verify_and_update_player
        from qapbot.i18n import t
        
        api_token = (self.coc_api_token.value or "").strip()
        if not api_token:
            user_id_local = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            message = t('playerregistration.api_token_required', user_id=user_id_local, guild_id=guild_id)
            await interaction.response.send_message(message, ephemeral=True)
            return
        
        player_id = self.player_data.get("player_tag", "")
        player_name = self.player_data.get("player_name", "Unknown")
        
        # Get user entry
        user_id = str(interaction.user.id)
        user_entry = CACHE.user_accounts.get(user_id, {"players": []})
        
        if not isinstance(user_entry, dict):  # type: ignore[misc]
            guild_id = interaction.guild.id if interaction.guild else None
            message = t('playerregistration.user_data_corrupted', user_id=user_id, guild_id=guild_id)
            await interaction.response.send_message(message, ephemeral=True)
            return
        
        # Find the player in the user's account
        existing_player = None
        for player in user_entry.get("players", []):
            if isinstance(player, dict):
                current_tag = player.get("player_tag", "")
                if current_tag == player_id:  # type: ignore[misc]
                    existing_player = player
                    break
        
        if not existing_player:
            guild_id = interaction.guild.id if interaction.guild else None
            message = t('playerregistration.player_not_in_account', user_id=user_id, guild_id=guild_id, player_name=player_name, player_tag=player_id)
            await interaction.response.send_message(message, ephemeral=True)
            return
        
        # Attempt verification
        guild_id = interaction.guild.id if interaction.guild else None
        verified, verify_msg = await verify_and_update_player(
            existing_player, player_id, api_token, guild_id=guild_id
        )
        
        if verified:
            await CACHE.persist_user(user_id)
            logging.info(f"USER ACTION: {interaction.user} verified player {player_name} ({player_id}) via welcome screen")
            
            # Assign member role if player is in member clans/families
            if interaction.guild:
                from qapbot.QBdiscocmdshelper import assign_member_role
                player_clan_tag = existing_player.get("current_clan_tag")
                try:
                    await assign_member_role(interaction.guild, interaction.user.id, player_name, player_clan_tag)  # type: ignore[arg-type]
                except Exception as e:
                    logging.error(f"Failed to assign member role after verification: {e}")

            # Sync CoC + clan roles immediately (mirrors ApiTokenEntryModal.on_submit /
            # ApiVerificationPromptView.skip_button, which perform the same sync)
            if interaction.guild:
                try:
                    from qapbot.guild_role_manager import sync_roles_for_user
                    await sync_roles_for_user(interaction.guild, str(interaction.guild.id), interaction.user.id)
                except Exception as _role_sync_e:
                    logging.warning(f"[ROLE-SYNC] Post-verify role sync failed for {interaction.user.id}: {_role_sync_e}")

            # If called from AccountManagementView, refresh it after verification
            if self.parent_view and self.parent_view.original_interaction:
                # Defer the modal response first
                await interaction.response.defer(ephemeral=True)
                
                # Update parent view's data and refresh the message
                overview_text = self.parent_view._build_message_content()  # type: ignore[attr-defined]
                await self.parent_view.original_interaction.edit_original_response(
                    content=overview_text,
                    view=self.parent_view
                )
                
                # Send success message as followup
                from qapbot.i18n import t
                guild_id = interaction.guild.id if interaction.guild else None
                success_msg = t('playerregistration.player_verified_success', user_id=user_id, guild_id=guild_id, message=verify_msg)
                await interaction.followup.send(success_msg, ephemeral=True)
            else:
                # Use centralized notification check helper (modular approach)
                # Note: Do NOT pass action_view_interaction - welcome message should never be deleted
                from qapbot.QBdiscocmdshelper import check_and_prompt_war_notifications
                from qapbot.i18n import t
                guild_id = interaction.guild.id if interaction.guild else None
                await check_and_prompt_war_notifications(
                    user_id=user_id,
                    interaction=interaction,
                    success_message=t('playerregistration.player_verified_success', user_id=user_id, guild_id=guild_id, message=verify_msg),
                    use_followup=False,
                    player_selection_interaction=None  # Keep welcome message - never delete it
                )
        else:
            # Verification failed - respond to modal submission with error
            # Welcome message is kept so user can try again
            await interaction.response.send_message(verify_msg, ephemeral=True)


class ApiTokenOwnershipModal(discord.ui.Modal, title="Prove Account Ownership"):
    """
    Modal for entering CoC API token to prove ownership when player is already linked to another user.
    
    This implements Rule 4 of account protection: Any user can re-link accounts by providing
    a valid CoC API token as cryptographic proof of rightful ownership.
    
    Args:
        player_tag: Normalized player tag being claimed
        target_user_id: Discord user ID claiming this player
        previous_owner_name: Display name of current owner
        previous_owner_id: Discord user ID of current owner
        is_verified: Whether player is currently verified
        player_selection_interaction: Optional interaction to delete player selection message
        guild_id: Guild ID for i18n
    """
    
    api_token_input = discord.ui.TextInput(
        label="CoC API Token",
        placeholder="Enter your API token from Clash of Clans",
        required=True,
        max_length=200,
        style=discord.TextStyle.short
    )
    
    def __init__(
        self,
        player_tag: str,
        target_user_id: int,
        previous_owner_name: str,
        previous_owner_id: str,
        is_verified: bool,
        player_selection_interaction: Optional[discord.Interaction] = None,
        guild_id: Optional[int] = None
    ):
        super().__init__()
        self.player_tag = player_tag
        self.target_user_id = target_user_id
        self.previous_owner_name = previous_owner_name
        self.previous_owner_id = previous_owner_id
        self.is_verified = is_verified
        self.player_selection_interaction = player_selection_interaction
        self.guild_id = guild_id
        
        # Translate placeholder (label cannot be translated due to discord.py limitations)
        from qapbot.i18n import t
        self.api_token_input.placeholder = t('ui_components.modals.placeholder_api_token_ownership', guild_id=guild_id)
    
    async def on_submit(self, interaction: discord.Interaction):
        """Handle API token submission and attempt ownership transfer."""
        from qapbot.QBdiscocmdshelper import process_player_registration
        from qapbot.i18n import t
        import logging
        
        api_token = self.api_token_input.value.strip()
        
        if not api_token:
            user_id = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.response.send_message(
                t('playerregistration.api_token_required', user_id=user_id, guild_id=guild_id),
                ephemeral=True
            )
            return
        
        # Defer the interaction immediately - modal submission consumes the initial response
        # All subsequent messages must use followup
        await interaction.response.defer(ephemeral=True)
        
        # Delete player selection message if it exists
        if self.player_selection_interaction:
            try:
                await self.player_selection_interaction.delete_original_response()
                logging.debug("Deleted player selection message before API token ownership transfer")
            except Exception:
                pass
        
        # Call process_player_registration with api_token to trigger ownership transfer
        # The api_token will be validated and if valid, player will be transferred in VERIFIED state
        # CRITICAL: use_followup=True because we deferred the interaction above
        await process_player_registration(
            interaction=interaction,
            player_tag=self.player_tag,
            player_name=None,  # Will be fetched from CoC API
            api_token=api_token,
            target_user_id=self.target_user_id,
            use_followup=True,  # MUST be True after defer
            show_api_prompt=False  # No need to prompt again
        )



class AccountManagementView(discord.ui.View):
    """
    View for managing user's linked Clash of Clans accounts.
    
    Features:
    - Display overview of all linked accounts
    - Select player from dropdown
    - Action buttons: Verify, Unlink, Set Primary (enabled/disabled based on selection)
    """
    def __init__(self, user_id: str, guild_id: int, display_name: str):
        """
        Initialize account management view.
        
        Args:
            user_id: Discord user ID
            guild_id: Guild ID for i18n
            display_name: Discord display name
        """
        super().__init__(timeout=600)
        self.user_id = user_id
        self.guild_id = guild_id
        self.display_name = display_name
        self.selected_player_tag = None
        self.original_interaction = None  # Store original interaction for editing
        
        from qapbot.i18n import t
        
        # Build player selection dropdown (row 0 — keeps the "Select a player below to manage:"
        # prompt attached directly above the dropdown, matching Discord's layout expectations)
        self._build_player_select()

        # Refresh button — row 2, below the action buttons
        self.refresh_button = discord.ui.Button(
            label=t('playerregistration.button_refresh_accounts', guild_id=guild_id),
            style=discord.ButtonStyle.secondary,
            custom_id="account_action_refresh",
            row=2
        )
        self.refresh_button.callback = self._on_refresh_click  # type: ignore[assignment]
        self.add_item(self.refresh_button)  # type: ignore[arg-type]

        # Add action buttons (initially all disabled, row 1)
        self.verify_button = discord.ui.Button(
            label=t('playerregistration.button_verify', guild_id=guild_id),
            style=discord.ButtonStyle.success,
            custom_id="account_action_verify",
            disabled=True,
            row=1
        )
        self.verify_button.callback = self._on_verify_click  # type: ignore[assignment]
        self.add_item(self.verify_button)  # type: ignore[arg-type]
        
        self.primary_button = discord.ui.Button(
            label=t('playerregistration.button_set_primary', guild_id=guild_id),
            style=discord.ButtonStyle.primary,
            custom_id="account_action_set_primary",
            disabled=True,
            row=1
        )
        self.primary_button.callback = self._on_set_primary_click  # type: ignore[assignment]
        self.add_item(self.primary_button)  # type: ignore[arg-type]
        
        self.unlink_button = discord.ui.Button(
            label=t('playerregistration.button_unlink', guild_id=guild_id),
            style=discord.ButtonStyle.danger,
            custom_id="account_action_unlink",
            disabled=True,
            row=1
        )
        self.unlink_button.callback = self._on_unlink_click  # type: ignore[assignment]
        self.add_item(self.unlink_button)  # type: ignore[arg-type]
    
    def _build_player_select(self, selected_tag: Optional[str] = None):
        """Build player selection dropdown from user's accounts.
        
        Args:
            selected_tag: Optional player tag to set as default selection
        """
        from qapbot.i18n import t
        
        user_entry = CACHE.user_accounts.get(self.user_id, {"players": []})
        user_players: List[Dict[str, Any]] = user_entry.get("players", [])  # type: ignore[assignment]
        
        options = []
        for player in user_players[:25]:  # Discord limit
            if not isinstance(player, dict):  # type: ignore[misc]
                continue
            
            player_tag = player.get("player_tag", "")
            player_name = player.get("player_name", "Unknown")
            verified = player.get("verified", False)
            is_primary = player.get("is_primary", False)
            
            # Skip if player_tag is empty
            if not player_tag:
                continue
            
            # Build label with indicators
            verify_emoji = "✅" if verified else "❌"
            primary_emoji = "⭐ " if is_primary else ""
            label = f"{primary_emoji}{player_name} ({player_tag}) {verify_emoji}"
            
            from qapbot.i18n import t
            options.append(discord.SelectOption(
                label=label[:100],
                value=player_tag,
                description=t('ui_components.player_selection.verified_desc', guild_id=self.guild_id) if verified else t('ui_components.player_selection.unverified_desc', guild_id=self.guild_id),
                default=(player_tag == selected_tag)  # Mark as default if it matches selected tag
            ))
        
        if not options:
            # Fallback - should not happen as we check before showing this view
            from qapbot.i18n import t
            options.append(discord.SelectOption(label=t('ui_components.player_selection.no_accounts', guild_id=self.guild_id), value="none"))  # type: ignore[arg-type]
        
        # Build select
        self.player_select = discord.ui.Select(
            placeholder=t('playerregistration.account_management_select_player', guild_id=self.guild_id),
            min_values=1,
            max_values=1,
            options=options,  # type: ignore[arg-type]
            custom_id="account_management_player_select",
            row=0
        )
        self.player_select.callback = self._on_player_select  # type: ignore[assignment]
        self.add_item(self.player_select)  # type: ignore[arg-type]
    
    def _build_message_content(self, status_message: Optional[str] = None, show_selection_prompt: bool = True) -> str:
        """Build message content with account overview.
        
        Args:
            status_message: Optional status/result message to show before selection prompt
            show_selection_prompt: Whether to show "Select a player below to manage:" text
        """
        from qapbot.i18n import t
        
        user_entry = CACHE.user_accounts.get(self.user_id, {"players": []})
        user_players: List[Dict[str, Any]] = user_entry.get("players", [])  # type: ignore[assignment]
        
        # Build player list for overview
        player_lines = []
        for player in user_players:
            if not isinstance(player, dict):  # type: ignore[misc]
                continue
            
            player_name = player.get("player_name", "Unknown")
            player_tag = player.get("player_tag", "Unknown")
            verified = player.get("verified", False)
            is_primary = player.get("is_primary", False)
            
            verify_emoji = "✅" if verified else "❌"
            primary_emoji = t('playerregistration.account_management_primary_indicator', guild_id=self.guild_id) if is_primary else t('playerregistration.account_management_empty_indicator', guild_id=self.guild_id)

            line = t('playerregistration.account_management_player_format',
                    guild_id=self.guild_id,
                    primary_emoji=primary_emoji,
                    player_name=player_name,
                    player_tag=player_tag,
                    verify_emoji=verify_emoji)

            # Append TH level and current clan name if known (populated after a Refresh or periodic sync)
            detail_parts = []
            th_level = player.get("th_level")
            current_clan_tag = player.get("current_clan_tag")
            if th_level is not None:
                detail_parts.append(f"TH{th_level}")
            if current_clan_tag:
                clan_name = CACHE.get_clan_name(current_clan_tag, current_clan_tag)
                detail_parts.append(clan_name)
            if detail_parts:
                line += t('playerregistration.account_management_player_clan',
                          guild_id=self.guild_id,
                          detail=" · ".join(detail_parts))

            player_lines.append(line)
        
        player_list = "\n".join(player_lines) if player_lines else "None"  # type: ignore[arg-type]
        
        overview_text = t('playerregistration.account_management_title', guild_id=self.guild_id) + "\n\n" + \
                       t('playerregistration.account_management_overview',
                         guild_id=self.guild_id,
                         display_name=self.display_name,
                         total=len(user_players),
                         player_list=player_list)
        
        # Add status message before selection prompt if provided
        if status_message:
            overview_text += f"\n\n{status_message}"
        
        # Add selection prompt only if requested
        if show_selection_prompt:
            overview_text += "\n\n" + t('playerregistration.account_management_select_prompt', guild_id=self.guild_id)
        
        return overview_text
    
    async def show_overview(self, interaction: discord.Interaction):
        """Show account overview with player selection dropdown."""
        self.original_interaction = interaction
        overview_text = self._build_message_content()  # type: ignore[attr-defined]
        
        await interaction.response.send_message(
            overview_text,
            view=self,
            ephemeral=True
        )
    
    async def _on_player_select(self, interaction: discord.Interaction):
        """Handle player selection - update message and enable/disable buttons."""
        selected_tag = self.player_select.values[0]
        self.selected_player_tag = selected_tag
        
        if selected_tag == "none":
            # Keep buttons disabled
            overview_text = self._build_message_content()  # type: ignore[attr-defined]
            await interaction.response.edit_message(content=overview_text, view=self)
            return
        
        # Get player data
        user_entry = CACHE.user_accounts.get(self.user_id, {"players": []})
        user_players: List[Dict[str, Any]] = user_entry.get("players", [])  # type: ignore[assignment]
        
        player_data = None
        for player in user_players:
            player_tag = player.get("player_tag")  # type: ignore[misc]
            if isinstance(player, dict) and player_tag == selected_tag:  # type: ignore[misc]
                player_data = player
                break
        
        if not player_data:
            # Keep buttons disabled
            overview_text = self._build_message_content()  # type: ignore[attr-defined]
            await interaction.response.edit_message(content=overview_text, view=self)
            return
        
        # Update button states based on player data
        verified = player_data.get("verified", False)
        is_primary = player_data.get("is_primary", False)
        
        # Verify button: only enabled if not verified
        self.verify_button.disabled = verified
        
        # Set Primary button: only enabled if not already primary
        self.primary_button.disabled = is_primary
        
        # Unlink button: always enabled when player selected
        self.unlink_button.disabled = False
        
        # Rebuild the select with the selected value as default to persist selection
        # Remove old select and add new one with default_values
        self.remove_item(self.player_select)  # type: ignore[arg-type]
        self._build_player_select(selected_tag=selected_tag)
        
        # Update message content (dropdown will show selection)
        overview_text = self._build_message_content()  # type: ignore[attr-defined]
        
        await interaction.response.edit_message(content=overview_text, view=self)


    async def _on_verify_click(self, interaction: discord.Interaction):
        """Handle Verify button click - show verification modal."""
        if not self.selected_player_tag:
            return
        
        user_entry = CACHE.user_accounts.get(self.user_id, {"players": []})
        user_players: List[Dict[str, Any]] = user_entry.get("players", [])  # type: ignore[assignment]
        
        player_data = None
        for player in user_players:
            if isinstance(player, dict):  # type: ignore[misc]
                current_tag = player.get("player_tag", "")
                if current_tag == self.selected_player_tag:  # type: ignore[misc]
                    player_data = player
                    break
        
        if not player_data:
            return
        
        modal = VerifyAccountModal(
            player_data=player_data,
            action_view_interaction=self.original_interaction,
            guild_id=self.guild_id,
            parent_view=self
        )
        await interaction.response.send_modal(modal)
    
    async def _on_set_primary_click(self, interaction: discord.Interaction):
        """Handle Set Primary button click - update in place."""
        from qapbot.QBdiscocmdshelper import set_primary_account
        
        if not self.selected_player_tag:
            return
        
        user_entry = CACHE.user_accounts.get(self.user_id, {"players": []})
        user_players: List[Dict[str, Any]] = user_entry.get("players", [])  # type: ignore[assignment]
        
        player_data = None
        for player in user_players:
            if isinstance(player, dict):  # type: ignore[misc]
                current_tag = player.get("player_tag", "")
                if current_tag == self.selected_player_tag:  # type: ignore[misc]
                    player_data = player
                    break
        
        if not player_data:
            return
        
        player_name = player_data.get("player_name", "Unknown")
        
        success = await set_primary_account(self.user_id, self.selected_player_tag)
        
        if success:
            # Refresh player data after change
            user_entry = CACHE.user_accounts.get(self.user_id, {"players": []})
            user_players: List[Dict[str, Any]] = user_entry.get("players", [])  # type: ignore[assignment]
            
            for player in user_players:
                if isinstance(player, dict):  # type: ignore[misc]
                    current_tag = player.get("player_tag", "")
                    if current_tag == self.selected_player_tag:  # type: ignore[misc]
                        player_data = player
                        break
            
            # Update button states (primary button should now be disabled)
            self.primary_button.disabled = True
            
            # Rebuild select to update star indicator in dropdown
            self.remove_item(self.player_select)  # type: ignore[arg-type]
            self._build_player_select(selected_tag=self.selected_player_tag)
            
            # Update message content (updated player list shows the change)
            overview_text = self._build_message_content()  # type: ignore[attr-defined]
            
            await interaction.response.edit_message(content=overview_text, view=self)
            logging.info(f"USER ACTION: {interaction.user} set {player_name} ({self.selected_player_tag}) as primary account")
        else:
            overview_text = self._build_message_content()  # type: ignore[attr-defined]
            await interaction.response.edit_message(content=overview_text, view=self)
    
    async def _on_unlink_click(self, interaction: discord.Interaction):
        """Handle Unlink button click - show confirmation inline."""
        from qapbot.i18n import t
        
        if not self.selected_player_tag:
            return
        
        user_entry = CACHE.user_accounts.get(self.user_id, {"players": []})
        user_players: List[Dict[str, Any]] = user_entry.get("players", [])  # type: ignore[assignment]
        
        player_data = None
        for player in user_players:
            if isinstance(player, dict):  # type: ignore[misc]
                current_tag = player.get("player_tag", "")
                if current_tag == self.selected_player_tag:  # type: ignore[misc]
                    player_data = player
                    break
        
        if not player_data:
            return
        
        player_name = player_data.get("player_name", "Unknown")
        
        # Show confirmation view inline
        confirm_view = UnlinkConfirmView(
            player_data=player_data,
            user_id=self.user_id,
            guild_id=self.guild_id,
            parent_view=self
        )
        
        confirm_text = self._build_message_content(  # type: ignore[attr-defined]
            status_message=f"⚠️ {t('playerregistration.unlink_confirm', guild_id=self.guild_id, player_name=player_name, player_tag=self.selected_player_tag)}",
            show_selection_prompt=False
        )
        
        await interaction.response.edit_message(
            content=confirm_text,
            view=confirm_view
        )

    async def _on_refresh_click(self, interaction: discord.Interaction) -> None:
        """Handle Refresh button — fetch fresh player data from CoC API for all linked accounts."""
        # Defer immediately so we can do async API work before responding
        await interaction.response.defer(ephemeral=True)

        user_entry = CACHE.user_accounts.get(self.user_id, {"players": []})
        user_players: List[Dict[str, Any]] = user_entry.get("players", [])  # type: ignore[assignment]

        tags = [p.get("player_tag") for p in user_players
                if p.get("player_tag")]

        # Parallel-fetch all player tags from the CoC API
        results = await asyncio.gather(
            *[CACHE.get_player(tag) for tag in tags],  # type: ignore[arg-type]
            return_exceptions=True
        )

        changes_made = False
        for tag, result in zip(tags, results):
            if not result or isinstance(result, Exception):
                logging.warning(f"[MY-ACCOUNTS-REFRESH] Could not fetch {tag}: {result}")
                continue

            player_obj = result
            for player in user_players:
                if player.get("player_tag") != tag:
                    continue

                # Name
                new_name = getattr(player_obj, "name", None)
                if new_name and player.get("player_name") != new_name:
                    player["player_name"] = new_name
                    changes_made = True

                # TH level
                new_th = getattr(player_obj, "town_hall", None)
                if new_th is not None and player.get("th_level") != new_th:
                    player["th_level"] = new_th
                    changes_made = True

                # Current clan tag (None when player is clanless)
                coc_clan = getattr(player_obj, "clan", None)
                new_clan_tag = coc_clan.tag if coc_clan else None
                if player.get("current_clan_tag") != new_clan_tag:
                    player["current_clan_tag"] = new_clan_tag
                    changes_made = True

                # CoC in-game role (member / elder / coLeader / leader)
                raw_role = getattr(player_obj, "role", None)
                _raw_name = getattr(raw_role, "name", None) if raw_role else None
                new_coc_role: Optional[str] = (
                    ("coLeader" if _raw_name == "co_leader" else _raw_name)
                    if _raw_name else None
                )
                if player.get("coc_role") != new_coc_role:
                    player["coc_role"] = new_coc_role
                    changes_made = True

                break  # matched — move to next tag

        if changes_made:
            await CACHE.persist_user(self.user_id)
            logging.info(f"[MY-ACCOUNTS-REFRESH] Persisted updated player data for user {self.user_id}")

        # Rebuild dropdown (player names may have changed)
        self.remove_item(self.player_select)  # type: ignore[arg-type]
        self._build_player_select(selected_tag=self.selected_player_tag)

        from qapbot.i18n import t as _t
        status = _t('playerregistration.account_management_refreshed',
                    user_id=self.user_id, guild_id=self.guild_id)
        overview_text = self._build_message_content(status_message=status)  # type: ignore[attr-defined]
        await interaction.edit_original_response(content=overview_text, view=self)


class UnlinkConfirmView(discord.ui.View):
    """
    Confirmation dialog for unlinking a player account.
    Shows Confirm and Cancel buttons inline.
    """
    def __init__(
        self,
        player_data: Dict[str, Any],
        user_id: str,
        guild_id: int,
        parent_view: 'AccountManagementView'
    ):
        """
        Initialize unlink confirmation view.
        
        Args:
            player_data: Player dict with player_id and player_name
            user_id: Discord user ID
            guild_id: Guild ID for i18n
            parent_view: AccountManagementView instance to restore on cancel
        """
        super().__init__(timeout=60)
        self.player_data = player_data
        self.user_id = user_id
        self.guild_id = guild_id
        self.parent_view = parent_view
        
        from qapbot.i18n import t
        
        # Confirm button
        confirm_button = discord.ui.Button(
            label=t('playerregistration.button_confirm_unlink', guild_id=guild_id),
            style=discord.ButtonStyle.danger,
            custom_id="unlink_confirm"
        )
        confirm_button.callback = self._on_confirm  # type: ignore[assignment]
        self.add_item(confirm_button)  # type: ignore[arg-type]
        
        # Cancel button
        cancel_button = discord.ui.Button(
            label=t('playerregistration.button_cancel', guild_id=guild_id),
            style=discord.ButtonStyle.secondary,
            custom_id="unlink_cancel"
        )
        cancel_button.callback = self._on_cancel  # type: ignore[assignment]
        self.add_item(cancel_button)  # type: ignore[arg-type]
    
    async def _on_confirm(self, interaction: discord.Interaction):
        """Handle Confirm button - unlink the player and update view."""
        from qapbot.QBdiscocmdshelper import unlink_player
        from qapbot.i18n import t
        
        player_tag = self.player_data.get("player_tag", "")
        player_name = self.player_data.get("player_name", "Unknown")
        
        success = await unlink_player(self.user_id, player_tag)

        if success:
            # STEP: ROLE SYNC
            # Recheck clan/member/CoC roles immediately after unlinking, mirroring
            # the post-link role sync in complete_account_linking_flow (STEP 3.5).
            if interaction.guild:
                try:
                    from qapbot.guild_role_manager import sync_roles_for_user
                    await sync_roles_for_user(interaction.guild, str(interaction.guild.id), int(self.user_id))
                except Exception as _role_sync_e:
                    logging.warning(f"[ROLE-SYNC] Post-unlink role sync failed for {self.user_id}: {_role_sync_e}")

            # Check if user still has accounts
            user_entry = CACHE.user_accounts.get(self.user_id, {"players": []})
            user_players: List[Dict[str, Any]] = user_entry.get("players", [])  # type: ignore[assignment]
            
            if not user_players:
                # No more accounts - show simple message
                message = t('playerregistration.unlink_success',
                           guild_id=self.guild_id,
                           player_name=player_name,
                           player_tag=player_tag)
                message += "\n\n" + t('playerregistration.account_management_no_accounts', guild_id=self.guild_id)
                await interaction.response.edit_message(content=message, view=None)
            else:
                # Create fresh AccountManagementView with updated data
                new_view = AccountManagementView(self.user_id, self.guild_id, self.parent_view.display_name)
                new_view.original_interaction = self.parent_view.original_interaction
                
                success_msg = t('playerregistration.unlink_success',
                               guild_id=self.guild_id,
                               player_name=player_name,
                               player_tag=player_tag)
                
                overview_text = new_view._build_message_content(status_message=f"✅ {success_msg}")  # type: ignore[attr-defined]
                
                await interaction.response.edit_message(content=overview_text, view=new_view)
            
            logging.info(f"USER ACTION: {interaction.user} unlinked {player_name} ({player_tag})")
        else:
            message = t('playerregistration.player_not_found', guild_id=self.guild_id)
            overview_text = self.parent_view._build_message_content(status_message=f"❌ {message}")  # type: ignore[attr-defined]
            await interaction.response.edit_message(content=overview_text, view=self.parent_view)
    
    async def _on_cancel(self, interaction: discord.Interaction):
        """Handle Cancel button - restore parent view."""
        # Restore parent view with current selection state preserved
        overview_text = self.parent_view._build_message_content()  # type: ignore[attr-defined]
        await interaction.response.edit_message(content=overview_text, view=self.parent_view)


class ApiVerificationPromptView(discord.ui.View):
    """
    View for prompting user to optionally verify with API token before registration.
    Shows two buttons: "Enter API token" and "Skip API verification".
    """
    def __init__(self, player_tag: str, player_name: str, player_selection_interaction: Optional[discord.Interaction] = None, guild_id: Optional[int] = None, timeout: int = 600):
        """
        Initialize API verification prompt view.
        
        Args:
            player_tag: Normalized player tag to be registered
            player_name: Player name
            player_selection_interaction: Optional interaction to delete player selection message
            guild_id: Discord guild ID for translation context
            timeout: Timeout in seconds for the view
        """
        super().__init__(timeout=timeout)
        self.player_tag = player_tag
        self.player_name = player_name
        self.player_selection_interaction = player_selection_interaction
        self.guild_id = guild_id
        self.prompt_message = None  # Will be set after message is sent
        
        # Translate button labels - cast to Button to satisfy type checker
        from qapbot.i18n import t
        if len(self.children) >= 1 and isinstance(self.children[0], discord.ui.Button):
            self.children[0].label = t('playerregistration.button_enter_api_token', guild_id=guild_id)
        if len(self.children) >= 2 and isinstance(self.children[1], discord.ui.Button):
            self.children[1].label = t('playerregistration.button_skip_api_verification', guild_id=guild_id)
    
    @discord.ui.button(label="🔑 Enter API token", style=discord.ButtonStyle.primary)
    async def enter_token_button(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[type-arg]
        """Handle Enter API token button click - show modal for token entry."""
        # Show API token entry modal for new player registration with verification
        modal = ApiTokenEntryModal(
            player_tag=self.player_tag,
            player_name=self.player_name,
            player_selection_interaction=self.player_selection_interaction,
            guild_id=self.guild_id
        )
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="⏭️ Skip API verification", style=discord.ButtonStyle.secondary)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[type-arg]
        """Handle Skip button click - use centralized notification check."""
        # Use centralized notification check helper (modular approach)
        from qapbot.i18n import t
        
        user_id = str(interaction.user.id)
        guild_id = interaction.guild.id if interaction.guild else None
        success_message = t('playerregistration.skip_success_message', guild_id=guild_id, player_name=self.player_name, player_tag=self.player_tag)
        
        # Get user data to check notification status
        user_data = CACHE.user_accounts.get(user_id, {})
        notification_settings = user_data.get("notification_settings", {})
        war_reminders_enabled = notification_settings.get("war_reminders", False)
        
        if not war_reminders_enabled:
            # User doesn't have war notifications - show prompt with buttons
            view = WarNotificationPromptView(guild_id=guild_id)
            message_text = (
                f"{success_message}\n\n"
                f"{t('playerregistration.war_notifications_prompt', guild_id=guild_id)}"
            )
            view.message = interaction.message
            await interaction.response.edit_message(content=message_text, view=view)
        else:
            # User already has notifications - just show success message
            await interaction.response.edit_message(content=success_message, view=None)

        # Sync CoC + clan roles immediately (complete_account_linking_flow returned early
        # before STEP 3.5 when the API prompt was shown)
        if interaction.guild:
            try:
                from qapbot.guild_role_manager import sync_roles_for_user
                await sync_roles_for_user(interaction.guild, str(interaction.guild.id), interaction.user.id)
            except Exception as _role_sync_e:
                logging.warning(f"[ROLE-SYNC] Post-skip role sync failed for {interaction.user.id}: {_role_sync_e}")

        logging.info(f"USER ACTION: {interaction.user} skipped verification for player {self.player_name} ({self.player_tag})")


class ApiTokenEntryModal(discord.ui.Modal, title="Enter API Token"):
    """
    Modal for entering API token to verify player during registration.
    Used when player is being registered for the first time with verification.
    """
    
    # TextInput MUST be class attribute for discord.py Modal system
    coc_api_token = discord.ui.TextInput(
        label="CoC API Token",
        required=True,
        max_length=32,
        placeholder="Your API token"
    )
    
    def __init__(self, player_tag: str, player_name: str, player_selection_interaction: Optional[discord.Interaction] = None, guild_id: Optional[int] = None):
        """
        Initialize API token entry modal for player registration with verification.
        
        Args:
            player_tag: Normalized player tag to be registered
            player_name: Player name
            player_selection_interaction: Optional interaction to delete player selection message
            guild_id: Discord guild ID for translation context
        """
        from qapbot.i18n import t
        
        # NOTE: super().__init__() must be called WITHOUT title parameter
        # Title is already set in class definition above
        super().__init__()
        
        # Translate TextInput placeholder after instantiation
        # (Label remains in English due to discord.py Modal lifecycle requirements)
        self.coc_api_token.placeholder = t('ui_components.modals.placeholder_api_token', guild_id=guild_id)
        
        self.player_tag = player_tag
        self.player_name = player_name
        self.player_selection_interaction = player_selection_interaction
        self.guild_id = guild_id
    
    async def on_submit(self, interaction: discord.Interaction):
        """Handle modal submission - verify already-linked player with API token."""
        from qapbot.QBdiscocmdshelper import verify_and_update_player, check_and_prompt_war_notifications
        from qapbot.cache_manager import CACHE
        from qapbot.i18n import t
        import logging
        
        api_token = (self.coc_api_token.value or "").strip()
        if not api_token:
            user_id_local = str(interaction.user.id)
            guild_id = interaction.guild.id if interaction.guild else None
            message = t('playerregistration.api_token_required', user_id=user_id_local, guild_id=guild_id)
            await interaction.response.send_message(message, ephemeral=True)
            return
        
        # Get user entry
        user_id = str(interaction.user.id)
        user_entry = CACHE.user_accounts.get(user_id, {"players": []})
        
        if not isinstance(user_entry, dict):  # type: ignore[misc]
            guild_id = interaction.guild.id if interaction.guild else None
            message = t('playerregistration.user_data_corrupted', user_id=user_id, guild_id=guild_id)
            await interaction.response.send_message(message, ephemeral=True)
            return
        
        # Find the player in the user's account (player should already be linked)
        existing_player = None
        for player in user_entry.get("players", []):
            if isinstance(player, dict):
                current_tag = player.get("player_tag", "")
                if current_tag == self.player_tag:  # type: ignore[misc]
                    existing_player = player
                    break
        
        if not existing_player:
            guild_id = interaction.guild.id if interaction.guild else None
            message = t('playerregistration.player_not_in_account', user_id=user_id, guild_id=guild_id, player_name=self.player_name, player_tag=self.player_tag)
            await interaction.response.send_message(message, ephemeral=True)
            return
        
        # Attempt verification
        guild_id = interaction.guild.id if interaction.guild else None
        verified, verify_msg = await verify_and_update_player(
            existing_player, self.player_tag, api_token, guild_id=guild_id
        )
        
        if verified:
            await CACHE.persist_user(str(interaction.user.id))
            logging.info(f"USER ACTION: {interaction.user} verified player {self.player_name} ({self.player_tag}) via API prompt")
            
            # Assign member role if player is in member clans/families
            if interaction.guild:
                from qapbot.QBdiscocmdshelper import assign_member_role
                player_clan_tag = existing_player.get("current_clan_tag")
                try:
                    await assign_member_role(interaction.guild, interaction.user.id, self.player_name, player_clan_tag)  # type: ignore[arg-type]
                except Exception as e:
                    logging.error(f"Failed to assign member role after verification: {e}")
            
            # Sync CoC + clan roles immediately (complete_account_linking_flow returned early
            # before STEP 3.5 when the API prompt was shown)
            if interaction.guild:
                try:
                    from qapbot.guild_role_manager import sync_roles_for_user
                    await sync_roles_for_user(interaction.guild, str(interaction.guild.id), interaction.user.id)
                except Exception as _role_sync_e:
                    logging.warning(f"[ROLE-SYNC] Post-verify role sync failed for {interaction.user.id}: {_role_sync_e}")

            # Use centralized notification check helper (modular approach)
            # Note: Don't pass player_selection_interaction here because:
            # - ApiTokenEntryModal is triggered from API prompt button, not player selection
            # - The message to delete is the API prompt message, which is the interaction's message
            # - Passing player_selection_interaction would delete the wrong message (welcome message)
            await check_and_prompt_war_notifications(
                user_id=user_id,
                interaction=interaction,
                success_message=verify_msg,
                use_followup=False,  # Modal hasn't responded yet
                player_selection_interaction=None  # Don't delete any extra messages
            )
        else:
            # Verification failed - send error message
            logging.warning(f"Verification failed for {self.player_name} ({self.player_tag}): {verify_msg}")
            await interaction.response.send_message(verify_msg, ephemeral=True)


class PlayerSubstringModal(discord.ui.Modal, title="Enter Player Data"):
    """
    Modal for entering player name/tag substring and optional API token.
    Handles player matching, direct tag lookup, and registration flow.
    
    NOTE: Title is set at class definition (discord.py requirement).
    TextInput labels/placeholders are translated at instantiation.
    """
    
    # TextInputs MUST be class attributes for discord.py Modal system
    substring = discord.ui.TextInput(
        label="Player Name or Tag",
        required=True,
        max_length=32,
        placeholder="Name or #TAG"
    )
    coc_api_token = discord.ui.TextInput(
        label="CoC API Token (optional)",
        required=False,
        max_length=32,
        placeholder="Your API token"
    )
    
    def __init__(self, player_list: List[Dict[str, Any]], clan_selection_interaction: Optional[discord.Interaction] = None, user_id: Optional[str] = None, guild_id: Optional[int] = None, clans_in_guild: Optional[List[str]] = None, filtered_by_clan: bool = False):
        """
        Initialize player substring modal.

        Args:
            player_list: List of player dicts with 'tag' and 'name' keys
            clan_selection_interaction: Optional interaction that created the clan selection message (for deletion)
            user_id: Discord user ID for language preference
            guild_id: Optional guild ID for clan filtering fallback (when > 25 matches)
            clans_in_guild: Optional list of clan tags available for filtering
            filtered_by_clan: Whether this modal is being shown after clan filtering (for messaging)
        """
        from qapbot.i18n import t
        
        # NOTE: super().__init__() must be called WITHOUT title parameter
        # Title is already set in class definition above
        super().__init__()
        
        # Translate TextInput placeholders after instantiation
        # (Labels remain in English due to discord.py Modal lifecycle requirements)
        self.substring.placeholder = t('playerregistration.modal_placeholder_player_name', user_id=user_id, guild_id=guild_id)
        self.coc_api_token.placeholder = t('playerregistration.modal_placeholder_api_token', user_id=user_id, guild_id=guild_id)
        
        self.player_list = player_list
        self.clan_selection_interaction = clan_selection_interaction
        self.user_id = user_id
        self.guild_id = guild_id
        self.clans_in_guild = clans_in_guild
        self.filtered_by_clan = filtered_by_clan

    async def on_submit(self, interaction: discord.Interaction):
        """Handle modal submission - match players or lookup direct tag."""
        # Import here to avoid circular dependency
        from qapbot.QBdiscocmdshelper import process_player_registration, normalize_clan_tag
        from qapbot.cache_manager import CACHE
        
        # Defer interaction immediately - modal submission uses the response
        # All subsequent messages must use followup
        await interaction.response.defer(ephemeral=True)
        
        substr_raw = (self.substring.value or "").strip()
        substr = substr_raw.lower()
        api_token = (self.coc_api_token.value or "").strip()

        logging.debug(f"PlayerSubstringModal: User entered '{substr_raw}', searching in {len(self.player_list)} players")
        
        # Debug: Log first few player names for troubleshooting
        if len(self.player_list) > 0:
            sample_names = [p.get("name", "Unknown") for p in self.player_list[:5]]
            logging.debug(f"PlayerSubstringModal: Sample player names: {sample_names}")

        # FIRST: Try to match against cached player list
        # - Name: substring match (case-insensitive)
        # - Tag: exact match only (no substring - tags like #2J would match too many)
        matches = [
            p for p in self.player_list
            if substr in (p.get("name", "").lower()) or substr == (p.get("tag", "").lower())
        ]
        
        logging.debug(f"PlayerSubstringModal: Found {len(matches)} matches in player list")
        
        # SECOND: If no matches in player list, check if user is trying to re-link an already linked player
        if not matches:
            from qapbot.QBdiscocmdshelper import complete_account_linking_flow, normalize_clan_tag
            from qapbot.cache_manager import CACHE
            
            user_id = str(interaction.user.id)
            user_entry = CACHE.user_accounts.get(user_id, {})
            
            # Try to match against user's existing players
            for player in user_entry.get("players", []):
                if not isinstance(player, dict):  # type: ignore[misc]
                    continue
                player_name = player.get("player_name", "").lower()
                player_tag = player.get("player_tag", "").lower()
                
                # Check if search matches existing player name (substring) or tag (exact match only)
                if substr in player_name or substr == player_tag:
                    # Player already linked - use the same flow as new registration
                    p_id = player.get("player_tag", "")
                    
                    if self.clan_selection_interaction:
                        try:
                            await self.clan_selection_interaction.delete_original_response()
                        except Exception:
                            pass
                    
                    # Use the unified flow (handles already-linked detection, verification, notifications)
                    await complete_account_linking_flow(
                        interaction=interaction,
                        target_user_id=interaction.user.id,
                        player_tag=p_id,  # type: ignore[arg-type]
                        api_token=api_token if api_token else None,
                        show_api_prompt=False,
                        send_success_message=True,
                        use_followup=True  # Using followup after defer
                    )
                    return
        
        # If no matches in player list, try a live API name search or direct tag entry.
        if not matches:
            # An explicit leading '#' is a strong signal the user intended to enter a tag.
            # Without it, prefer a live name search first — normalize_clan_tag()'s pattern
            # (5-10 alphanumeric chars) also matches short plain player names (e.g. "Killer"
            # -> "#KILLER"), which would otherwise hijack real name searches and report a
            # bogus "player tag not found" error instead of finding the player via the live
            # clan roster (which includes members not yet present in the war-history cache).
            explicit_tag_intent = substr_raw.startswith('#')

            if not explicit_tag_intent and substr and self.clans_in_guild:
                logging.debug(f"PlayerSubstringModal: Trying live API name search for '{substr_raw}' before tag fallback")
                for clan_tag in self.clans_in_guild:
                    try:
                        clan_obj = await CACHE.coc_clan_cache.get_clan(clan_tag)
                        live_hits = [
                            {"tag": m.tag, "name": m.name, "is_current_member": True}
                            for m in clan_obj.members
                            if substr in m.name.lower()
                        ]
                        matches.extend(live_hits)
                    except Exception as e:
                        logging.debug(f"PlayerSubstringModal: Live API search failed for {clan_tag}: {e}")
                # Deduplicate by tag
                seen: Set[str] = set()
                matches = [m for m in matches if not (m["tag"] in seen or seen.add(m["tag"]))]  # type: ignore[func-returns-value]
                logging.debug(f"PlayerSubstringModal: Live API search found {len(matches)} matches for '{substr_raw}'")

            # Only fall back to raw tag lookup if the live name search (if attempted) found nothing.
            if not matches:
                direct_tag = normalize_clan_tag(substr_raw) if substr_raw else None
                logging.debug(f"PlayerSubstringModal: No name matches, attempting direct tag registration for '{direct_tag}'")
                if direct_tag:
                    # Delete clan selection message before processing
                    if self.clan_selection_interaction:
                        try:
                            await self.clan_selection_interaction.delete_original_response()
                            logging.debug("Successfully deleted clan selection message (direct tag)")
                        except Exception as e:
                            logging.debug(f"Could not delete clan selection message: {e}")

                    # process_player_registration will handle CoC API lookup and validation
                    await process_player_registration(
                        interaction=interaction,
                        player_tag=direct_tag,
                        player_name=None,  # Will be fetched from CoC API
                        api_token=api_token,
                        use_followup=True
                    )
                    return

            if not matches:
                logging.info(f"PlayerSubstringModal: No matches for '{substr_raw}' (checked {len(self.player_list)} players + live API)")
                if self.clan_selection_interaction:
                    try:
                        await self.clan_selection_interaction.delete_original_response()
                        logging.debug("Successfully deleted clan selection message (no matches)")
                    except Exception as e:
                        logging.debug(f"Could not delete clan selection message: {e}")
                from qapbot.i18n import t
                user_id = str(interaction.user.id)
                guild_id = interaction.guild.id if interaction.guild else None
                message = t('playerregistration.no_matches', user_id=user_id, guild_id=guild_id)
                await interaction.followup.send(message, ephemeral=True)
                return
        
        # Single match found
        if len(matches) == 1:
            p = matches[0]
            # Delete clan selection message before processing
            if self.clan_selection_interaction:
                try:
                    await self.clan_selection_interaction.delete_original_response()
                    logging.debug("Successfully deleted clan selection message (single match)")
                except Exception as e:
                    logging.debug(f"Could not delete clan selection message: {e}")
            await process_player_registration(
                interaction, p["tag"], p.get("name", "Unknown"), api_token, use_followup=True
            )
            return

        # Multiple matches - check if we need clan filtering first
        if len(matches) > 25:
            # Too many matches - offer clan selection to narrow down
            if self.clans_in_guild and len(self.clans_in_guild) > 1 and not self.filtered_by_clan:
                # Build clan options
                from qapbot.i18n import t
                clan_choices = []
                for tag in self.clans_in_guild:
                    name = CACHE.get_clan_name(tag, "Unknown Clan")  # type: ignore[arg-type]
                    label = f"{name} ({tag})"
                    clan_choices.append((tag, label))
                
                clan_options = [
                    discord.SelectOption(label=label[:100], value=tag)  # type: ignore[arg-type]
                    for tag, label in clan_choices[:25]
                ]
                
                # Create view with callback that will re-filter by clan
                view = GenericSelectView(
                    options=clan_options,  # type: ignore[arg-type]
                    callback_fn=_clan_filter_callback,
                    placeholder=t('playerregistration.clan_filter_prompt', user_id=self.user_id, guild_id=self.guild_id),
                    timeout=600,
                    callback_kwargs={
                        "substring": substr_raw,
                        "api_token": api_token,
                        "guild_id": self.guild_id,
                        "clans_in_guild": self.clans_in_guild
                    }
                )
                
                user_id = str(interaction.user.id)
                guild_id = interaction.guild.id if interaction.guild else None
                message = t('playerregistration.too_many_matches_clan', user_id=user_id, guild_id=guild_id, count=len(matches), substring=substr_raw)
                await interaction.followup.send(
                    message,
                    view=view,
                    ephemeral=True
                )
                return
            else:
                # Already filtered by clan or only one clan - ask for more specific substring
                from qapbot.i18n import t
                user_id = str(interaction.user.id)
                guild_id = interaction.guild.id if interaction.guild else None
                message = t('playerregistration.too_many_matches_specific', user_id=user_id, guild_id=guild_id, count=len(matches), substring=substr_raw)
                await interaction.followup.send(
                    message,
                    ephemeral=True
                )
                return
        
        # 2-25 matches - show selection dropdown
        from qapbot.i18n import t
        player_options = []
        for p in matches[:25]:
            tag = p.get("tag", "")
            name = p.get("name", "Unknown")
            label = f"{name} ({tag})" if tag else name
            if tag:
                player_options.append(discord.SelectOption(label=label[:100], value=tag))  # type: ignore[arg-type]
        
        view = GenericSelectView(
            options=player_options,  # type: ignore[arg-type]
            callback_fn=_user_player_select_callback,
            placeholder=t('playerregistration.select_player', user_id=self.user_id, guild_id=self.guild_id),
            timeout=600,
            callback_kwargs={
                "matches": matches,
                "api_token": api_token,
                "process_registration_fn": process_player_registration
            }
        )
        
        # Try to delete the clan selection message before sending player selection
        if self.clan_selection_interaction:
            try:
                await self.clan_selection_interaction.delete_original_response()
                logging.debug("Successfully deleted clan selection message")
            except discord.NotFound:
                logging.debug("Clan selection message already deleted")
            except Exception as e:
                logging.debug(f"Could not delete clan selection message: {e}")
        
        user_id = str(interaction.user.id)
        guild_id = interaction.guild.id if interaction.guild else None
        prompt = t('playerregistration.select_player_prompt', user_id=user_id, guild_id=guild_id, substring=substr_raw)
        await interaction.followup.send(prompt, view=view, ephemeral=True)


# ============================================================================
# Unified Notification Management View
# ============================================================================


