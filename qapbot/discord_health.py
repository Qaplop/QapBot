"""
Simple Discord API retry helper with basic error handling.
Replaces the complex health monitoring system with minimal, focused functionality.
"""
import asyncio
import logging
import time
import aiohttp
import discord
from typing import TypeVar, Callable, Any, Dict, Optional

T = TypeVar('T')

# Simple counters for basic statistics
_stats = {
    'total_calls': 0,
    'successful_calls': 0,
    'rate_limits': 0,
    'api_errors': 0
}

# ---------------------------------------------------------------------------
# Outage detection: track consecutive 5xx errors so Discord-facing operations
# can skip posting during a known outage rather than burning retries per call.
# ---------------------------------------------------------------------------
_discord_consecutive_5xx: int = 0
_discord_outage_detected: bool = False
_discord_outage_since: Optional[float] = None
_DISCORD_OUTAGE_THRESHOLD: int = 3   # consecutive 5xx errors → outage declared


def is_discord_available() -> bool:
    """Return False when a Discord outage has been detected (≥3 consecutive 5xx errors)."""
    return not _discord_outage_detected


def _mark_discord_success() -> None:
    """Reset the outage counter on any successful Discord API call."""
    global _discord_consecutive_5xx, _discord_outage_detected, _discord_outage_since
    if _discord_outage_detected:
        duration = time.monotonic() - (_discord_outage_since or 0)
        logging.info(
            f"[DISCORD-HEALTH] Discord outage cleared after {duration:.0f}s — "
            "resuming normal operations."
        )
    _discord_consecutive_5xx = 0
    _discord_outage_detected = False
    _discord_outage_since = None


def _mark_discord_5xx() -> None:
    """Increment the consecutive-5xx counter; declare outage after threshold is hit."""
    global _discord_consecutive_5xx, _discord_outage_detected, _discord_outage_since
    _discord_consecutive_5xx += 1
    if not _discord_outage_detected and _discord_consecutive_5xx >= _DISCORD_OUTAGE_THRESHOLD:
        _discord_outage_detected = True
        _discord_outage_since = time.monotonic()
        logging.warning(
            f"[DISCORD-HEALTH] Discord outage detected ({_discord_consecutive_5xx} consecutive "
            "5xx errors). Skipping Discord posts until service recovers."
        )

async def discord_retry(
    operation: Callable[[], T], 
    operation_name: str = "discord_api_call",
    max_retries: int = 3
) -> T:
    """
    Simple retry wrapper for Discord API calls with rate limit handling.
    
    Args:
        operation: Async function to execute
        operation_name: Name for logging purposes
        max_retries: Maximum retry attempts (default: 3)
    
    Returns:
        Result of the operation
        
    Raises:
        The last exception encountered if all retries fail
        
    Features:
        - Automatic retry with exponential backoff
        - Special handling for rate limits (HTTP 429)
        - Basic statistics tracking
        - Comprehensive error logging
    """
    global _stats
    _stats['total_calls'] += 1
    
    for attempt in range(max_retries + 1):
        try:
            result = await operation()  # type: ignore[misc]
            _stats['successful_calls'] += 1
            _mark_discord_success()
            return result  # type: ignore[return-value]
            
        except discord.HTTPException as e:
            if e.status == 429:  # Rate limited
                _stats['rate_limits'] += 1
                retry_after = getattr(e, 'retry_after', 2 ** attempt)
                if attempt < max_retries:
                    logging.warning(f"{operation_name} rate limited, waiting {retry_after}s (attempt {attempt + 1})")
                    await asyncio.sleep(retry_after)
                    continue
                else:
                    logging.error(f"{operation_name} rate limited after {max_retries + 1} attempts")
                    _stats['api_errors'] += 1
                    raise
            elif 400 <= e.status < 500:
                # 4xx = client error (bad request, forbidden, not found, etc.).
                # These are permanent — retrying will never succeed.
                _stats['api_errors'] += 1
                logging.error(f"{operation_name} failed: {e.status} {e} (not retrying — client error)")
                raise
            else:
                # 5xx = server error (Discord outage/overload).
                _stats['api_errors'] += 1
                _mark_discord_5xx()
                if attempt < max_retries:
                    backoff_time = 2 ** attempt
                    logging.warning(f"{operation_name} HTTP error {e.status}, retrying in {backoff_time}s (attempt {attempt + 1})")
                    await asyncio.sleep(backoff_time)
                else:
                    logging.error(f"{operation_name} failed after {max_retries + 1} attempts: {e}")
                    raise
                    
        except (discord.ConnectionClosed, aiohttp.ClientConnectionResetError, aiohttp.ClientError) as e:
            # Transport/connection reset: give discord.py time to complete
            # its automatic reconnect before retrying.  Normal 2**attempt
            # backoff (1-4s) is too short when reconnects take 5-10s.
            _stats['api_errors'] += 1
            if attempt < max_retries:
                backoff_time = max(2 ** attempt, 15)
                logging.warning(
                    f"{operation_name} connection reset ({type(e).__name__}), "
                    f"waiting {backoff_time}s for reconnect (attempt {attempt + 1})"
                )
                await asyncio.sleep(backoff_time)
            else:
                logging.error(f"{operation_name} failed after {max_retries + 1} attempts (connection errors): {e}")
                raise

        except Exception as e:
            _stats['api_errors'] += 1
            if attempt < max_retries:
                backoff_time = 2 ** attempt
                logging.warning(f"{operation_name} error, retrying in {backoff_time}s (attempt {attempt + 1}): {e}")
                await asyncio.sleep(backoff_time)
            else:
                logging.error(f"{operation_name} failed after {max_retries + 1} attempts: {e}")
                raise
    
    raise RuntimeError("Retry logic error - should not reach here")

# discord.py 2.7.1's AppCommandType enum has no member for this — it predates Discord Activities'
# auto-created "launch" Entry Point command (see CWL_CLAN_CONFIG_ACTIVITY_PLAN.md). Raw type id
# per Discord's API docs (application command type 4, PRIMARY_ENTRY_POINT).
PRIMARY_ENTRY_POINT_TYPE = 4

async def bulk_sync_global_commands(bot: discord.Client, tree_payload: list) -> list:
    """Bulk-overwrites global commands via a raw HTTP call, always re-including whatever
    Activities Entry Point command already exists on the application.

    Once Activities is enabled in the Developer Portal, Discord auto-creates a global
    PRIMARY_ENTRY_POINT command that `discord.py`'s `CommandTree` has no knowledge of.
    `tree.sync(guild=None)`/`tree.clear_commands(guild=None)` + `tree.sync()` both submit a
    bulk overwrite payload that omits it, which Discord rejects outright (HTTP 400, error code
    50240) rather than silently deleting it — so every global bulk-upsert must fetch the
    current commands first and splice the Entry Point one back in, even when clearing
    everything else (`tree_payload=[]`).
    """
    app_id = bot.application_id
    existing = await bot.http.get_global_commands(app_id)
    entry_points = [cmd for cmd in existing if cmd.get('type') == PRIMARY_ENTRY_POINT_TYPE]
    return await bot.http.bulk_upsert_global_commands(app_id, payload=tree_payload + entry_points)

def get_simple_discord_stats() -> Dict[str, Any]:
    """
    Get basic Discord API statistics for status reporting.
    
    Returns:
        Dictionary containing:
        - total_calls: Total API calls attempted
        - successful_calls: Successful API calls
        - api_errors: Failed API calls
        - rate_limits: Rate limit hits
        - success_rate: Success percentage
    """
    global _stats
    
    total = _stats['total_calls']
    success_rate = round((_stats['successful_calls'] / total * 100), 1) if total > 0 else 0
    
    return {
        'total_calls': total,
        'successful_calls': _stats['successful_calls'],
        'api_errors': _stats['api_errors'],
        'rate_limits': _stats['rate_limits'],
        'success_rate': success_rate
    }

def reset_stats() -> None:
    """Reset all statistics counters."""
    global _stats
    _stats = {
        'total_calls': 0,
        'successful_calls': 0,
        'rate_limits': 0,
        'api_errors': 0
    }