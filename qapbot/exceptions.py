"""
Custom exception hierarchy for QapBot.

This module defines a structured exception hierarchy for QapBot, making error
handling more specific and maintainable. By using custom exceptions:
- Callers can catch specific error types for targeted handling
- Error context is preserved with meaningful exception messages
- Debugging is easier with clear exception names
- Error recovery strategies can be implemented per exception type

Exception Hierarchy:
    QapBotError (base)
    ├── ConfigurationError - Invalid or missing configuration
    ├── CacheError - Cache operations (save/load/corruption)
    │   ├── CacheSaveError - Failed to save cache data
    │   ├── CacheLoadError - Failed to load cache data
    │   └── CacheCorruptionError - Corrupted cache data detected
    ├── WarProcessingError - War data processing failures
    │   ├── WarDataFetchError - Failed to fetch war data from API
    │   ├── WarStateError - Invalid war state transition
    │   └── WarHistoryError - Historical war data issues
    ├── LeaderboardError - Leaderboard generation/posting failures
    │   ├── LeaderboardGenerationError - Failed to generate leaderboard
    │   └── LeaderboardPostingError - Failed to post leaderboard
    ├── AccountProtectionError - Account linking security violations
    │   ├── VerifiedAccountError - Attempt to modify verified account
    │   ├── OwnershipConflictError - Account owned by another user
    │   └── ApiTokenValidationError - Invalid API token provided
    ├── NotificationError - War notification failures
    │   ├── NotificationSendError - Failed to send notification
    │   └── NotificationConfigError - Invalid notification configuration
    └── ValidationError - Data validation failures

Usage:
    from qapbot.exceptions import (
        QapBotError,
        ConfigurationError,
        CacheError,
        WarProcessingError,
        AccountProtectionError
    )
    
    # Raise specific exception
    if not clan_tag:
        raise ValidationError("Clan tag is required")
    
    # Catch specific exception types
    try:
        process_war_data(clan_tag)
    except WarDataFetchError as e:
        logging.warning(f"API fetch failed: {e}")
        # Retry logic
    except WarStateError as e:
        logging.error(f"Invalid war state: {e}")
        # Recovery logic
    except WarProcessingError as e:
        logging.error(f"War processing failed: {e}")
        # Generic handling
    
    # Catch all QapBot exceptions
    try:
        operation()
    except QapBotError as e:
        logging.error(f"QapBot operation failed: {e}")
        # Handle all QapBot errors
"""
from typing import Optional, Dict, Any


# ============================================================================
# Base Exception
# ============================================================================

class QapBotError(Exception):
    """
    Base exception for all QapBot-specific errors.
    
    All custom exceptions in QapBot inherit from this class. This allows
    catching all QapBot-specific errors with a single except clause while
    distinguishing them from standard Python exceptions.
    
    Attributes:
        message: Human-readable error description
        context: Optional dict with additional error context
    
    Example:
        try:
            # QapBot operation
            pass
        except QapBotError as e:
            logging.error(f"QapBot error: {e}")
    """
    
    def __init__(self, message: str, context: Optional[Dict[str, Any]] = None):
        """
        Initialize QapBot exception with message and optional context.
        
        Args:
            message: Human-readable error description
            context: Optional dict with error details (clan_tag, user_id, etc.)
        """
        super().__init__(message)
        self.message = message
        self.context = context or {}
    
    def __str__(self):
        """Return string representation with context if available."""
        if self.context:
            context_str = ", ".join(f"{k}={v}" for k, v in self.context.items())
            return f"{self.message} ({context_str})"
        return self.message


# ============================================================================
# Configuration Errors
# ============================================================================

class ConfigurationError(QapBotError):
    """
    Invalid or missing configuration values.
    
    Raised when:
    - Required environment variables are missing
    - Configuration values fail validation
    - Invalid configuration combinations are detected
    
    Example:
        if not coc_email:
            raise ConfigurationError(
                "COC_API_EMAIL or COC_API_EMAIL_DEV must be set",
                context={"is_dev_mode": is_dev_mode}
            )
    """
    pass


# ============================================================================
# Cache Errors
# ============================================================================

class CacheError(QapBotError):
    """
    Base exception for cache operation failures.
    
    Raised when cache operations (save, load, validation) fail.
    Subclasses provide more specific error types for targeted handling.
    """
    pass


class CacheSaveError(CacheError):
    """
    Failed to save cache data to disk.
    
    Raised when:
    - File write operations fail
    - JSON serialization fails
    - Atomic save operation fails
    
    Example:
        try:
            with open(cache_file, 'w') as f:
                json.dump(data, f)
        except Exception as e:
            raise CacheSaveError(
                f"Failed to save {cache_name}",
                context={"file": cache_file, "error": str(e)}
            ) from e
    """
    pass


class CacheLoadError(CacheError):
    """
    Failed to load cache data from disk.
    
    Raised when:
    - Cache file is missing (expected to exist)
    - File read operations fail
    - JSON parsing fails
    
    Example:
        try:
            with open(cache_file, 'r') as f:
                data = json.load(f)
        except FileNotFoundError as e:
            raise CacheLoadError(
                f"{cache_name} not found",
                context={"file": cache_file}
            ) from e
    """
    pass


class CacheCorruptionError(CacheError):
    """
    Cache data is corrupted or invalid.
    
    Raised when:
    - Loaded data fails validation
    - Required fields are missing
    - Data structure is malformed
    
    Example:
        if not isinstance(loaded_data, dict):
            raise CacheCorruptionError(
                f"{cache_name} has invalid structure",
                context={"file": cache_file, "type": type(loaded_data).__name__}
            )
    """
    pass


# ============================================================================
# War Processing Errors
# ============================================================================

class WarProcessingError(QapBotError):
    """
    Base exception for war data processing failures.
    
    Raised when war data fetch, processing, or state management fails.
    Subclasses provide more specific error types.
    """
    pass


class WarDataFetchError(WarProcessingError):
    """
    Failed to fetch war data from CoC API.
    
    Raised when:
    - API requests fail (network, rate limits, maintenance)
    - Clan not found
    - War log is private
    
    Example:
        try:
            war = await coc_client.get_current_war(clan_tag)
        except coc.NotFound as e:
            raise WarDataFetchError(
                f"Clan not found: {clan_tag}",
                context={"clan_tag": clan_tag}
            ) from e
    """
    pass


class WarStateError(WarProcessingError):
    """
    Invalid war state transition or operation.
    
    Raised when:
    - War state is invalid for operation
    - State transition is not allowed
    - War ID mismatch detected
    
    Example:
        if war.state not in ["preparation", "inWar"]:
            raise WarStateError(
                f"Invalid war state for operation: {war.state}",
                context={"clan_tag": clan_tag, "state": war.state}
            )
    """
    pass


class WarHistoryError(WarProcessingError):
    """
    Historical war data operation failed.
    
    Raised when:
    - Failed to load history from CSV
    - Duplicate war entry detected
    - Late attack update failed
    
    Example:
        if war_id in existing_wars:
            raise WarHistoryError(
                f"War already in history: {war_id}",
                context={"clan_tag": clan_tag, "war_id": war_id}
            )
    """
    pass


# ============================================================================
# Leaderboard Errors
# ============================================================================

class LeaderboardError(QapBotError):
    """
    Base exception for leaderboard operation failures.
    
    Raised when leaderboard generation or posting fails.
    """
    pass


class LeaderboardGenerationError(LeaderboardError):
    """
    Failed to generate leaderboard text.
    
    Raised when:
    - History data is invalid
    - Mode calculation fails
    - Rendering fails
    
    Example:
        if not history_data:
            raise LeaderboardGenerationError(
                f"No history data for clan: {clan_tag}",
                context={"clan_tag": clan_tag, "mode": mode}
            )
    """
    pass


class LeaderboardPostingError(LeaderboardError):
    """
    Failed to post leaderboard to Discord.
    
    Raised when:
    - Discord API errors
    - Channel not found
    - Permission errors
    
    Example:
        try:
            await channel.send(leaderboard_text)
        except discord.Forbidden as e:
            raise LeaderboardPostingError(
                f"No permission to post in channel",
                context={"channel_id": channel.id}
            ) from e
    """
    pass


# ============================================================================
# Account Protection Errors
# ============================================================================

class AccountProtectionError(QapBotError):
    """
    Base exception for account linking security violations.
    
    Raised when account protection rules are violated.
    These exceptions prevent unauthorized account modifications.
    """
    pass


class VerifiedAccountError(AccountProtectionError):
    """
    Attempt to modify a verified account without proper authorization.
    
    Raised when:
    - Non-admin tries to re-link verified account
    - Verified account modification without admin override
    
    Example:
        if verified_owner and not admin_override:
            raise VerifiedAccountError(
                f"Account is verified by another user",
                context={
                    "player_tag": player_tag,
                    "verified_owner": verified_owner,
                    "requester": requesting_user_id
                }
            )
    """
    pass


class OwnershipConflictError(AccountProtectionError):
    """
    Account is already owned by another Discord user.
    
    Raised when:
    - Player is linked to different user
    - Ownership check fails during linking
    
    Example:
        if current_owner and current_owner != requesting_user_id:
            raise OwnershipConflictError(
                f"Player already linked to different user",
                context={
                    "player_tag": player_tag,
                    "current_owner": current_owner,
                    "requester": requesting_user_id
                }
            )
    """
    pass


class ApiTokenValidationError(AccountProtectionError):
    """
    API token validation failed.
    
    Raised when:
    - API token doesn't match player
    - Token validation request fails
    - Invalid token format
    
    Example:
        if not is_valid_token:
            raise ApiTokenValidationError(
                f"API token validation failed for player",
                context={"player_tag": player_tag}
            )
    """
    pass


# ============================================================================
# Notification Errors
# ============================================================================

class NotificationError(QapBotError):
    """
    Base exception for war notification failures.
    
    Raised when notification operations fail.
    """
    pass


class NotificationSendError(NotificationError):
    """
    Failed to send notification to user.
    
    Raised when:
    - DM delivery fails
    - User has DMs disabled
    - Network/API errors
    
    Example:
        try:
            await user.send(notification_message)
        except discord.Forbidden as e:
            raise NotificationSendError(
                f"Cannot send DM to user",
                context={"user_id": user.id}
            ) from e
    """
    pass


class NotificationConfigError(NotificationError):
    """
    Invalid notification configuration.
    
    Raised when:
    - Invalid notification type
    - Invalid mode setting
    - Configuration inconsistency
    
    Example:
        if notification_type not in ["all", "cwl"]:
            raise NotificationConfigError(
                f"Invalid notification type: {notification_type}",
                context={"user_id": user_id, "type": notification_type}
            )
    """
    pass


# ============================================================================
# Validation Errors
# ============================================================================

class ValidationError(QapBotError):
    """
    Data validation failed.
    
    Raised when:
    - Input data is invalid
    - Required fields are missing
    - Data format is incorrect
    
    Example:
        if not clan_tag.startswith('#'):
            raise ValidationError(
                f"Invalid clan tag format: {clan_tag}",
                context={"clan_tag": clan_tag}
            )
    """
    pass
