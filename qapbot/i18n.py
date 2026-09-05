"""
Internationalization (i18n) support for QapBot.

This module provides translation management for user-facing Discord messages.
All terminal output and logging remains in English only.

Key Features:
- JSON-based translation files (qapbot/translations/{lang}.json)
- In-memory caching of translations for performance
- Fallback chain: requested_lang → en → key_path
- Per-guild language preferences stored in server config database
- Variable interpolation using {variable} syntax
- Hot reload support for development

Translation Scope:
- User-facing Discord messages (commands, responses, UI)
- Bot-generated notifications and status messages
- Error messages shown to users
- Help text and command descriptions

NOT Translated:
- Terminal output and log messages (English only)
- Leaderboard column headers (keep as-is for consistency)
- Number formatting (no locale-specific formatting)
- Admin-only internal messages

Architecture:
- TranslationManager: Singleton class managing all translations
- t() function: Primary interface for translation lookups
- Guild language preferences stored in CACHE.discord_server_config

Integration:
- Used throughout QapBot for all user-facing text
- Initialized on bot startup
- Provides context-aware translations based on guild_id

Example Usage:
    from qapbot.i18n import t
    
    # Simple translation
    message = t("common.errors.not_found")
    
    # With variable interpolation
    message = t("playerregistration.welcome_title", server_name="My Server")
    
    # Guild-specific language
    message = t("commands.leaderboard.title", guild_id=123456789)
"""

import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional


class TranslationManager:
    """
    Manages loading, caching, and retrieval of translations.
    
    Singleton pattern ensures only one instance exists across the application.
    """
    
    _instance: Optional['TranslationManager'] = None
    
    def __init__(self):
        """Initialize translation manager with empty cache."""
        self.translations: Dict[str, Dict[str, Any]] = {}
        self.default_language = "en"
        self.translations_dir = Path(__file__).parent / "translations"
        self._loaded = False
    
    @classmethod
    def get_instance(cls) -> 'TranslationManager':
        """Get or create singleton instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    def load_translations(self) -> None:
        """
        Load all translation files from the translations directory.
        
        Scans qapbot/translations/ for .json files and loads them into memory.
        Each file should be named {language_code}.json (e.g., en.json, es.json).
        
        Behavior:
            - Loads all available language files
            - Caches translations in memory for fast lookup
            - Logs loading status (English only, not translated)
            - Gracefully handles missing or malformed files
        """
        if self._loaded:
            return
        
        if not self.translations_dir.exists():
            logging.warning(f"Translations directory not found: {self.translations_dir}")
            self.translations_dir.mkdir(parents=True, exist_ok=True)
            return
        
        loaded_count = 0
        for file_path in self.translations_dir.glob("*.json"):
            lang_code = file_path.stem
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    self.translations[lang_code] = json.load(f)
                loaded_count += 1
                logging.debug(f"Loaded translations for language: {lang_code}")
            except json.JSONDecodeError as e:
                logging.error(f"Failed to parse translation file {file_path}: {e}")
            except Exception as e:
                logging.error(f"Failed to load translation file {file_path}: {e}")
        
        if loaded_count > 0:
            logging.info(f"Loaded {loaded_count} translation file(s)")
        else:
            logging.warning("No translation files loaded")
        
        self._loaded = True
    
    def reload_translations(self) -> None:
        """
        Reload all translation files from disk.
        
        Useful for development when translation files are updated without restarting the bot.
        """
        self.translations.clear()
        self._loaded = False
        self.load_translations()
        logging.info("Translations reloaded")
    
    def get_translation(self, key: str, language: Optional[str] = None, **kwargs: Any) -> str:
        """
        Get translation for a given key with fallback support.
        
        Args:
            key: Translation key in dot notation (e.g., "commands.leaderboard.title")
            language: Target language code (e.g., "en", "es")
            **kwargs: Variables for interpolation in the translation string
        
        Returns:
            Translated string with variables interpolated, or key if not found
        
        Fallback Chain:
            1. Try requested language
            2. Fall back to default language (English)
            3. Return key path if translation not found
        
        Example:
            >>> tm.get_translation("welcome.title", "es", name="John")
            "Bienvenido, John"
        """
        if not self._loaded:
            self.load_translations()
        
        if language is None:
            language = self.default_language
        
        # Try requested language first
        translation = self._get_nested_value(self.translations.get(language, {}), key)
        
        # Fall back to default language if not found
        if translation is None and language != self.default_language:
            translation = self._get_nested_value(
                self.translations.get(self.default_language, {}), 
                key
            )
        
        # Fall back to key if still not found
        if translation is None:
            logging.debug(f"Translation not found for key '{key}' in language '{language}'")
            return key
        
        # Interpolate variables
        if kwargs:
            try:
                return translation.format(**kwargs)
            except KeyError as e:
                logging.warning(f"Missing variable {e} for translation key '{key}'")
                return translation
        
        return translation
    
    def _get_nested_value(self, data: Dict[str, Any], key_path: str) -> Optional[str]:
        """
        Navigate nested dictionary using dot notation.
        
        Args:
            data: Dictionary to search
            key_path: Dot-separated path (e.g., "commands.leaderboard.title")
        
        Returns:
            Value at the key path, or None if not found
        
        Example:
            >>> data = {"commands": {"leaderboard": {"title": "Leaderboard"}}}
            >>> _get_nested_value(data, "commands.leaderboard.title")
            "Leaderboard"
        """
        keys = key_path.split('.')
        current = data
        
        for key in keys:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                return None
        
        return current if isinstance(current, str) else None
    
    def get_available_languages(self) -> list[str]:
        """
        Get list of available language codes.
        
        Returns:
            List of language codes (e.g., ["en", "es", "fr"])
        """
        if not self._loaded:
            self.load_translations()
        return list(self.translations.keys())


# Global translation manager instance
_translation_manager = TranslationManager.get_instance()


def t(key: str, guild_id: Optional[int] = None, user_id: Optional[str] = None, **kwargs: Any) -> str:
    """
    Get translated string for the given key.
    
    Primary interface for translation lookups throughout QapBot.
    Automatically determines language based on context (user or guild preferences).
    
    Args:
        key: Translation key in dot notation (e.g., "commands.leaderboard.title")
        guild_id: Discord guild ID for language preference lookup (for public messages)
        user_id: Discord user ID for language preference lookup (for ephemeral messages/DMs)
        **kwargs: Variables for interpolation in the translation string
    
    Returns:
        Translated string with variables interpolated
    
    Language Resolution (Fallback Chain):
        1. If user_id provided AND user has language preference: Use user's language
        2. Else if guild_id provided: Use guild's language preference
        3. Otherwise: Use default language (English)
    
    Usage:
        - Ephemeral messages/DMs: Pass BOTH user_id and guild_id for proper fallback
        - Public messages: Pass only guild_id
    
    Examples:
        >>> t("common.errors.not_found")
        "Not found"
        
        >>> t("playerregistration.welcome_title", server_name="My Server")
        "Welcome to My Server"
        
        >>> t("commands.leaderboard.no_data", guild_id=123456789)
        "No war data available"
        
        >>> # Ephemeral message - fallback to guild language if user has no preference
        >>> t("warnotifications.button_activate", user_id="123456789012345678", guild_id=123456789)
        "🔔 Benachrichtigungen aktivieren"
    """
    # Priority: user language (if set) > guild language > default
    language = None
    
    if user_id:
        # Try to get user's language preference (returns None if not set)
        language = get_user_language(user_id)
        if language:
            logging.debug(f"t('{key}'): User {user_id} has language preference '{language}'")
    
    # Fall back to guild language if user has no preference
    if language is None and guild_id:
        language = get_guild_language(guild_id)
        if user_id:
            logging.debug(f"t('{key}'): User {user_id} has no preference, falling back to guild {guild_id} language")
    
    if language is None:
        language = _translation_manager.default_language
        logging.debug(f"t('{key}'): No user/guild language specified, using default '{language}'")
    else:
        logging.debug(f"t('{key}'): Final language selection '{language}' (user_id={user_id}, guild_id={guild_id})")
    
    return _translation_manager.get_translation(key, language, **kwargs)


def get_namespace(namespace: str, language: Optional[str] = None) -> Dict[str, str]:
    """Flattened {dotted_key: raw_template} for one namespace subtree, for bulk delivery to a
    non-Python client (plans/cwl-personal-hub.md Phase 6b — the Discord Activity's `/api/i18n`
    endpoint fetches `cwl.activity` this way once at launch, rather than round-tripping `t()`
    per string). Placeholders are left UNINTERPOLATED (`{name}` intact) — the caller substitutes.

    Falls back to `en` PER KEY, not per namespace, matching `get_translation()`'s own chain: a
    `de.json` missing one key under this namespace must yield English for that key only, never
    silently drop every sibling key just because one is missing. Keys nested more than one level
    below the namespace (none exist yet, but a future one might) are flattened with the same
    dot-notation the rest of this module uses.

    Returns `{}` for a namespace that doesn't exist in either language — never raises.
    """
    _translation_manager.load_translations()
    if language is None:
        language = _translation_manager.default_language

    def _resolve_subtree(lang: str) -> Dict[str, Any]:
        current: Any = _translation_manager.translations.get(lang, {})
        for part in namespace.split('.'):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return {}
        return current if isinstance(current, dict) else {}

    def _flatten(data: Dict[str, Any], prefix: str = "") -> Dict[str, str]:
        flat: Dict[str, str] = {}
        for k, v in data.items():
            dotted_key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                flat.update(_flatten(v, dotted_key))
            elif isinstance(v, str):
                flat[dotted_key] = v
        return flat

    # Per-key fallback: start from the English subtree (guarantees every key that exists in
    # English is present), then overlay whatever the target language actually has — a key the
    # target is missing simply keeps its English value instead of vanishing.
    merged = _flatten(_resolve_subtree(_translation_manager.default_language))
    merged.update(_flatten(_resolve_subtree(language)))
    return merged


def get_guild_language(guild_id: Optional[int]) -> str:
    """
    Get language preference for a guild.
    
    Args:
        guild_id: Discord guild ID
    
    Returns:
        Language code (e.g., "en", "es") from guild config, or default language
    
    Note:
        Language preference is stored in CACHE.server_config[guild_id]["language"]
        Falls back to default language (English) if not configured.
    """
    if guild_id is None:
        logging.debug(f"get_guild_language: guild_id is None, using default '{_translation_manager.default_language}'")
        return _translation_manager.default_language
    
    try:
        from qapbot.cache_manager import CACHE
        guild_id_str = str(guild_id)
        server_config = CACHE.server_config.get(guild_id_str, {})
        language = server_config.get("language", _translation_manager.default_language)
        logging.debug(f"get_guild_language: guild {guild_id} ({guild_id_str}) -> language '{language}'")
        logging.debug(f"  Available guilds in CACHE.server_config: {list(CACHE.server_config.keys())}")
        logging.debug(f"  Guild {guild_id_str} config: {server_config}")
        return language
    except Exception as e:
        logging.error(f"Error getting guild language for {guild_id}: {e}")
        return _translation_manager.default_language


def get_user_language(user_id: Optional[str]) -> Optional[str]:
    """
    Get language preference for a specific user.
    
    Args:
        user_id: Discord user ID (string)
    
    Returns:
        Language code (e.g., "en", "de") if user has set a preference, None otherwise
    
    Behavior:
        - Returns user's language preference if explicitly set
        - Returns None if user has no preference (enables fallback to guild language)
        - This allows t() to properly implement: user preference → guild language → default
    
    Note:
        This function is synchronous. For async contexts, use war_notifications._get_user_language()
    
    Example:
        >>> get_user_language("123456789012345678")  # User has preference
        "de"
        >>> get_user_language("999999999999999999")  # User has no preference
        None
    """
    if user_id is None:
        return None
    
    try:
        from qapbot.cache_manager import CACHE
        user_data = CACHE.user_accounts.get(str(user_id), {})
        # Return None if user has no language preference set (enables fallback)
        return user_data.get("user_language", None)
    except Exception as e:
        logging.debug(f"Error getting user language: {e}")
        return None


async def set_guild_language(guild_id: int, language: str) -> bool:
    """
    Set language preference for a guild.
    
    Args:
        guild_id: Discord guild ID
        language: Language code (e.g., "en", "es")
    
    Returns:
        True if language was set successfully, False otherwise
    
    Note:
        Updates CACHE.server_config and persists to disk.
        Validates that the language exists before setting.
    """
    try:
        from qapbot.cache_manager import CACHE
        
        # Validate language exists
        available_languages = _translation_manager.get_available_languages()
        if language not in available_languages:
            logging.warning(f"Attempted to set invalid language '{language}' for guild {guild_id}")
            return False
        
        # Update cache
        guild_id_str = str(guild_id)
        if guild_id_str not in CACHE.server_config:
            CACHE.server_config[guild_id_str] = {}
        
        CACHE.server_config[guild_id_str]["language"] = language
        await CACHE.persist_server_config(guild_id_str)
        
        logging.info(f"Set language '{language}' for guild {guild_id}")
        return True
    except Exception as e:
        logging.error(f"Failed to set guild language for {guild_id}: {e}")
        return False


def reload_translations() -> None:
    """
    Reload all translation files from disk.
    
    Useful for development when translation files are updated without restarting the bot.
    """
    _translation_manager.reload_translations()


def get_available_languages() -> list[str]:
    """
    Get list of available language codes.
    
    Returns:
        List of language codes (e.g., ["en", "es", "fr"])
    """
    return _translation_manager.get_available_languages()
