from __future__ import annotations
# pyright: reportUnknownParameterType=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""
Common/shared UI components used across multiple QapBot modules.

Contains:
- update_user_metadata_from_interaction: Utility for user metadata updates
- GenericSelectView: Reusable dropdown selection view
- LanguageSelectView: Standalone language selection for slash commands
"""
import discord
import logging
from typing import List, Callable, Any, Optional, Dict

from qapbot.cache_manager import CACHE


async def check_maintenance_block(interaction: discord.Interaction) -> bool:
    """
    Check if bot is in maintenance or DB-maintenance mode and block the interaction.

    Returns True if the interaction was blocked (caller should abort).
    Returns False if the bot is operating normally (caller should continue).

    NOTE: deliberately does NOT consult QBcore.db_migration_active — the batched
    hot->history migration holds no exclusive lock and must never refuse a user.
    See that flag's docstring in QBcore.py.
    """
    import QBcore as _qbcore

    if not _qbcore.maintenance_mode and not _qbcore.db_maintenance_mode:
        return False

    from qapbot.i18n import t as _t
    guild_id = interaction.guild.id if interaction.guild else None

    if _qbcore.db_maintenance_mode and not _qbcore.maintenance_mode:
        msg = _t('commands.errors.db_maintenance_active', guild_id=guild_id)
        _reason = "db_maintenance"
    else:
        msg = _t('commands.errors.maintenance_mode_active', guild_id=guild_id)
        _reason = "maintenance_mode"
    # Same forensic trail the slash-command guard now keeps — component interactions
    # (buttons/selects/modals) are refused here and were previously just as invisible.
    _qbcore.record_interaction_rejection(_reason, _qbcore.interaction_command_label(interaction))

    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)
    except Exception:
        pass
    return True


async def update_user_metadata_from_interaction(interaction: discord.Interaction) -> None:
    """
    Update user metadata (display_name, user_language) from interaction.
    
    Call this at the start of any interaction handler to keep user data current.
    Extracts locale from interaction context and updates cached user preferences.
    
    Args:
        interaction: Discord interaction object
    
    Example:
        async def on_submit(self, interaction: discord.Interaction):
            await update_user_metadata_from_interaction(interaction)
            # ... rest of handler
    """
    try:
        await CACHE.update_user_metadata(str(interaction.user.id), interaction=interaction)
    except Exception as e:
        logging.debug(f"Failed to update user metadata from interaction: {e}")


class TrackedView(discord.ui.View):
    """Base View for the common "tracked temporary message" pattern.

    Consolidates two behaviors that were previously copy-pasted across 7+ views in
    ui_common.py, ui_notifications.py, ui_registration.py, ui_clan_management.py, and
    QBdiscordcmds.py — with the message-tracking attribute inconsistently named
    `self.message` in some views and `self.sent_message` in others, and 10062
    (expired-interaction-token) suppression present in only 2 of them, so the rest let
    that error propagate noisily:

    1. `self.message`: set this to the sent `discord.Message` after posting the view.
       `on_timeout()` deletes it automatically — best-effort, any delete failure
       (already deleted, missing permissions, etc.) is swallowed.
    2. `on_error()`: suppresses `discord.NotFound` code 10062 ("Unknown interaction" —
       the token expired, typically because the user clicked after Discord's 3-second
       response window) as an INFO log instead of letting it propagate as an error;
       everything else still propagates via `super().on_error()`.

    Subclasses needing extra timeout/error behavior should override and call `super()`
    to keep this shared cleanup/suppression rather than re-implementing it inline.
    See changelog.txt 2026-08-08 (21).
    """
    message: Optional[discord.Message] = None

    async def on_timeout(self) -> None:
        """Delete the tracked message when the view times out."""
        if self.message is not None:
            try:
                await self.message.delete()
            except Exception:
                pass

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:  # type: ignore[type-arg, override]
        """Suppress 'Unknown interaction' (10062) errors from expired tokens; re-raise anything else."""
        if isinstance(error, discord.NotFound) and error.code == 10062:
            logging.info(
                f"[{type(self).__name__}] Expired interaction for '{getattr(item, 'label', item)}' "
                f"(10062) — user likely clicked after the 3-second window"
            )
            return
        await super().on_error(interaction, error, item)


class GenericSelectView(TrackedView):
    """
    Unified generic selector for clans, players, or any list of options.
    Supports both sync and async callback functions with flexible parameters.

    This replaces the need for separate ClanSelectView, PlayerSelectView,
    AdminClanSelectView, and AdminPlayerSelectView classes.
    """
    def __init__(
        self,
        options: List[discord.SelectOption],
        callback_fn: Callable[..., Any],  # type: ignore[type-arg]
        placeholder: str = "Select an option...",
        timeout: int = 180,
        custom_id: Optional[str] = None,
        callback_kwargs: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize generic selector view.

        Args:
            options: List of discord.SelectOption objects (max 25)
            callback_fn: Async function to call when selection is made.
                         Signature: async def callback(interaction, selected_value, **kwargs)
            placeholder: Text shown when nothing is selected
            timeout: Timeout in seconds for the view
            custom_id: Optional custom ID for the select component
            callback_kwargs: Additional keyword arguments to pass to callback_fn
        """
        super().__init__(timeout=timeout)
        self.callback_fn = callback_fn
        self.callback_kwargs = callback_kwargs or {}
        
        # Build Select kwargs - only include custom_id if provided
        select_kwargs = {
            "placeholder": placeholder,
            "min_values": 1,
            "max_values": 1,
            "options": options[:25]  # Discord limit
        }
        if custom_id is not None:
            select_kwargs["custom_id"] = custom_id
        
        self.select = discord.ui.Select(**select_kwargs)  # type: ignore[arg-type]
        self.select.callback = self._on_select  # type: ignore[assignment]
        self.add_item(self.select)  # type: ignore[arg-type]
        self.message: Optional[discord.Message] = None
    
    async def _on_select(self, interaction: discord.Interaction) -> None:
        """Handle selection - delegate to callback function."""
        selected_value = self.select.values[0]
        await self.callback_fn(interaction, selected_value, **self.callback_kwargs)


class LanguageSelectView(TrackedView):
    """
    Language selection view for /admin set_language command.
    
    Allows server administrators to select the preferred language for their guild.
    """
    def __init__(self, guild_id: int, available_languages: List[tuple[str, str]], timeout: int = 180):
        """
        Initialize language selection view.
        
        Args:
            guild_id: Discord guild ID
            available_languages: List of (language_code, language_name) tuples
            timeout: Timeout in seconds for the view
        """
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        
        from qapbot.i18n import t
        
        # Create select options for languages
        options = [
            discord.SelectOption(
                label=lang_name,
                value=lang_code,
                description=t('ui_components.language_selector.option_description', guild_id=self.guild_id, lang_name=lang_name)
            )
            for lang_code, lang_name in available_languages
        ]
        
        self.select = discord.ui.Select(
            placeholder=t('ui_components.language_selector.placeholder', guild_id=self.guild_id),
            min_values=1,
            max_values=1,
            options=options,  # type: ignore[arg-type]
            custom_id="language_select"
        )
        self.select.callback = self._on_select  # type: ignore[assignment]
        self.add_item(self.select)  # type: ignore[arg-type]
        self.message: Optional[discord.Message] = None

    async def _on_select(self, interaction: discord.Interaction) -> None:
        """Handle language selection."""
        from qapbot.i18n import set_guild_language, t
        
        selected_language = self.select.values[0]
        
        # Set the guild language
        success = await set_guild_language(self.guild_id, selected_language)
        
        if success:
            # Get language name for confirmation message
            lang_names = {
                "en": "English",
                "de": "Deutsch (German)"
            }
            lang_display = lang_names.get(selected_language, selected_language)
            
            # Use the NEW language for the success message
            success_msg = t("commands.admin.set_language.success", guild_id=self.guild_id, language_name=lang_display)
            
            await interaction.response.send_message(
                success_msg,
                ephemeral=True
            )
        else:
            error_msg = t("commands.admin.set_language.error", guild_id=self.guild_id)
            await interaction.response.send_message(
                error_msg,
                ephemeral=True
            )
        
        # Disable the select after use
        self.select.disabled = True
        try:
            await interaction.message.edit(view=self)  # type: ignore[union-attr]
        except Exception:
            pass
        
        self.stop()

