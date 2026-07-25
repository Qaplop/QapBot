"""
Helper functions for the Discord slash commands module QBdiscordcmds.py.

This module provides argument parsing, message tracking, and normalization helpers for Discord slash command handlers. All business logic uses cache-centric data access and unified message ID tracking for spam prevention and update logic.

Key Features:
- Robust argument parsing for leaderboard and flexible slash commands
- Unified message ID tracking for all Discord posts
- Message deletion and cleanup helpers for cache consistency
- Clan and family tag normalization and validation
- Orphaned message cleanup for Discord channels
- Autocomplete choice generation for slash command parameters

Business Rules:
- All Discord posts use unified message ID tracking in CACHE.leaderboard_messages
- All cache operations are atomic and synchronized with persistent storage
- Defensive programming for Discord API errors and rate limits

Integration:
- Used by QBdiscordcmds for slash command argument parsing and message management
- Interacts with cache_manager for all runtime data
- Supports leaderboard, status, and administrative command flows
"""
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false
import asyncio
from datetime import datetime
import hashlib
import logging
import re
import discord
from discord import app_commands
from typing import List, Dict, Any, Optional, Tuple, Set, Callable, Union

from qapbot.emojis import BotEmojis
from qapbot.cache_manager import CACHE
from qapbot.exceptions import ValidationError
from QBhelperfunctions import (generate_message_key_timestamp)
from qapbot.discord_health import discord_retry

async def send_and_track(
    interaction: discord.Interaction, 
    content: Optional[str] = None, 
    command_name: Optional[str] = None, 
    embed: Optional[discord.Embed] = None,
    ephemeral: bool = False
) -> None:
    """
    Send a message and track it for future updates using unified message ID system. Deletes all old messages and stale message IDs in the same context.
    Automatically splits messages longer than 1950 characters into multiple messages (text only).
    
    Args:
        interaction (discord.Interaction): Discord interaction object from slash command
        content (str, optional): Message content to send (will be split if > 1950 chars). Mutually exclusive with embed.
        command_name (str): Unique command identifier for tracking
        embed (discord.Embed, optional): Embed to send (no splitting, embeds support 4096 chars). Mutually exclusive with content.
        ephemeral (bool): If True, send as ephemeral (user-only visible) and skip tracking. Default False.
    
    Note:
        Either content or embed must be provided, but not both.
        Ephemeral messages skip all tracking/cleanup since they can't be deleted by the bot.
    """
    if not command_name:
        raise ValidationError(
            "command_name is required for send_and_track()",
            context={"interaction": interaction.command.name if interaction.command else "unknown"}
        )
    
    if content and embed:
        raise ValidationError(
            "Cannot specify both content and embed for send_and_track()",
            context={"command_name": command_name}
        )
    
    if not content and not embed:
        raise ValidationError(
            "Must specify either content or embed for send_and_track()",
            context={"command_name": command_name}
        )
    
    # Ephemeral messages: send directly without tracking (can't be deleted/tracked)
    if ephemeral:
        try:
            if embed:
                # Check if embed description needs splitting
                if embed.description and len(embed.description) > 4000:
                    logging.info(f"Ephemeral embed description exceeds 4000 chars ({len(embed.description)}), splitting into multiple embeds...")
                    embeds_to_send = _split_embed_by_description(embed, max_size=4000)
                    
                    # Send first embed as response
                    await interaction.followup.send(embed=embeds_to_send[0], ephemeral=True)
                    
                    # Send remaining embeds as followups
                    for embed_to_send in embeds_to_send[1:]:
                        await interaction.followup.send(embed=embed_to_send, ephemeral=True)
                else:
                    await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                if content:
                    await interaction.followup.send(content=content, ephemeral=True)
        except Exception as e:
            logging.error(f"Failed to send ephemeral {command_name} message: {e}")
        return
    
    # Clean up the original deferred response message ("thinking..." or invisible placeholder)
    # For commands that call send_and_track() multiple times, only the first call will succeed
    try:
        await discord_retry(
            lambda: interaction.delete_original_response(),
            "delete_original_response"
        )
    except discord.NotFound:
        # Message doesn't exist or was already deleted - this is fine
        pass
    except discord.HTTPException as e:
        # Error code 10008 = Unknown Message (already deleted by previous send_and_track call)
        if e.code == 10008:
            # Silent pass - this is expected when send_and_track() is called multiple times
            pass
        else:
            logging.warning(f"delete_original_response failed: {e}")
    
    # Generate timestamp-based key using unified helper
    timestamp = generate_message_key_timestamp()
    # Store metadata in value dict
    if not interaction.channel:
        return
    entry = {
        'clan_tag': f"channel_{interaction.channel.id}",
        'channel_id': str(interaction.channel.id),
        'mode': command_name,
        'message_ids': '',
        'content_hash': ''
    }
    keys_to_delete = []
    # For 'status' and 'userinfo', preserve last 3 (preserve_count + new) messages of that type
    preserve_count = 2 if command_name in ('status', 'userinfo') else 0
    # Find all matching keys for this context and mode
    matching_keys = [k for k, v in CACHE.leaderboard_messages.items()
                     if v.get('clan_tag') == entry['clan_tag'] and v.get('channel_id') == entry['channel_id'] and v.get('mode') == entry['mode']]
    # Sort keys by timestamp (oldest first)
    matching_keys_sorted = sorted(matching_keys)
    # Only delete if more than preserve_count exist
    if preserve_count > 0 and len(matching_keys_sorted) > preserve_count:
        keys_to_delete = matching_keys_sorted[:len(matching_keys_sorted)-preserve_count]
    else:
        keys_to_delete = [] if preserve_count > 0 else matching_keys_sorted
    for k in keys_to_delete:
        v = CACHE.leaderboard_messages[k]
        msg_ids = v.get('message_ids', '')
        if msg_ids:
            for msg_id in msg_ids.split(','):
                try:
                    if not interaction.channel or not hasattr(interaction.channel, 'fetch_message'):
                        continue
                    msg = await interaction.channel.fetch_message(int(msg_id))  # type: ignore[union-attr]
                    await discord_retry(  # type: ignore[misc]
                        lambda: msg.delete(),  # type: ignore[misc]
                        f"delete_message_{msg_id}"
                    )
                    await asyncio.sleep(0.5)  # pace deletions to avoid Discord rate limits
                except discord.HTTPException as e:
                    # Error code 10008 = Unknown Message (manually deleted by user)
                    if e.code == 10008:
                        logging.info(f"Message {msg_id} already deleted (error 10008), removing from cache")
                    else:
                        logging.warning(f"Failed to delete message {msg_id}: {e}")
                except Exception as e:
                    logging.info(f"Could not delete message {msg_id}: {e}")
        await CACHE.delete_leaderboard_message(k)
    
    # Send message (embed or text content)
    sent_message_ids = []
    
    if embed:
        # Validate and potentially split embed if description exceeds limit
        # Discord embed limits: https://discord.com/developers/docs/resources/channel#embed-limits
        # - Description: 4096 chars (we use 4000 for safety margin)
        
        # Check if we need to split the embed
        if embed.description and len(embed.description) > 4000:
            logging.info(f"Embed description exceeds 4000 chars ({len(embed.description)}), splitting into multiple embeds...")
            embeds_to_send = _split_embed_by_description(embed, max_size=4000)
        else:
            embeds_to_send = [embed]
        
        # Validate other fields (title, footer, etc.)
        for idx, embed_to_send in enumerate(embeds_to_send):
            if embed_to_send.title and len(embed_to_send.title) > 256:
                logging.warning(f"Embed title exceeds 256 chars, truncating...")
                embed_to_send.title = embed_to_send.title[:253] + "..."
            
            if embed_to_send.footer and embed_to_send.footer.text and len(embed_to_send.footer.text) > 2048:
                logging.warning(f"Embed footer exceeds 2048 chars, truncating...")
                embed_to_send.set_footer(text=embed_to_send.footer.text[:2045] + "...")
        
        # Send all embeds
        for idx, embed_to_send in enumerate(embeds_to_send):
            try:
                if not interaction.channel or not hasattr(interaction.channel, 'send'):
                    continue
                sent = await discord_retry(  # type: ignore[misc]
                    lambda e=embed_to_send: interaction.channel.send(embed=e),  # type: ignore[union-attr]
                    f"send_command_response_{command_name}_embed_{idx}"
                )
                sent_message_ids.append(str(sent.id))  # type: ignore[attr-defined]
            except Exception as e:
                logging.error(f"Failed to send {command_name} embed message {idx}: {e}")
    else:
        # Split content into chunks if necessary (Discord limit: 2000 chars, use 1950 for safety)
        if not content:
            return
        MAX_CHUNK_SIZE = 1950
        chunks = _split_message_into_chunks(content, MAX_CHUNK_SIZE)
        
        # Send all chunks and collect message IDs
        for chunk_idx, chunk in enumerate(chunks):
            try:
                if not interaction.channel or not hasattr(interaction.channel, 'send'):
                    continue
                sent = await discord_retry(  # type: ignore[misc]
                    lambda: interaction.channel.send(chunk),  # type: ignore[union-attr]
                    f"send_command_response_{command_name}_chunk_{chunk_idx}"
                )
                sent_message_ids.append(str(sent.id))  # type: ignore[attr-defined]
            except Exception as e:
                logging.error(f"Failed to send {command_name} message chunk {chunk_idx}: {e}")
    
    # Store all message IDs as comma-separated string
    if sent_message_ids:
        entry['message_ids'] = ','.join(sent_message_ids)  # type: ignore[arg-type]
        await CACHE.set_leaderboard_message(timestamp, entry)

def _split_message_into_chunks(content: str, max_size: int) -> List[str]:
    """
    Split a message into chunks that fit within Discord's character limit while preserving code block formatting.
    
    Args:
        content (str): Message content to split
        max_size (int): Maximum size per chunk (default: 1950 to stay under 2000 limit)
        
    Returns:
        List[str]: List of message chunks, each under max_size characters
        
    Behavior:
        - Preserves code block formatting (```...```)
        - Splits at newlines when possible to keep content readable
        - Ensures each chunk starts and ends with proper code block markers if needed
    """
    # If content fits in one chunk, return as-is
    if len(content) <= max_size:
        return [content]
    
    chunks: List[str] = []
    
    # Check if content is wrapped in code block
    is_code_block = content.startswith('```') and content.endswith('```')
    
    if is_code_block:
        # Extract content between code block markers
        inner_content = content[3:-3]
        
        # Split inner content into chunks
        current_chunk = ""
        lines = inner_content.split('\n')
        
        for line in lines:
            # Check if adding this line would exceed the limit
            # Account for code block markers: ``` at start (3) + ``` at end (3) = 6 chars overhead
            if len(current_chunk) + len(line) + 1 + 6 > max_size:
                if current_chunk:
                    # Close current chunk and add to chunks list
                    chunks.append(f"```{current_chunk}```")
                    current_chunk = ""
            
            # Add line to current chunk
            if current_chunk:
                current_chunk += '\n' + line
            else:
                current_chunk = line
        
        # Add remaining content as final chunk
        if current_chunk:
            chunks.append(f"```{current_chunk}```")
    else:
        # Non-code-block content: split at newlines
        current_chunk = ""
        lines = content.split('\n')
        
        for line in lines:
            if len(current_chunk) + len(line) + 1 > max_size:
                if current_chunk:
                    chunks.append(current_chunk)
                    current_chunk = ""
            
            if current_chunk:
                current_chunk += '\n' + line
            else:
                current_chunk = line
        
        if current_chunk:
            chunks.append(current_chunk)
    
    return chunks if chunks else [content]

def _split_embed_by_description(original_embed: discord.Embed, max_size: int = 4000) -> List[discord.Embed]:
    """
    Split an embed into multiple embeds when description exceeds the character limit.
    
    Args:
        original_embed: The original embed to split
        max_size: Maximum characters for description field (default 4000, Discord limit is 4096)
        
    Returns:
        List[discord.Embed]: List of embeds, each with description under max_size
        
    Behavior:
        - First embed gets the author/title from original
        - All embeds get the same color
        - Last embed gets the footer from original
        - Description is split at newlines to keep content readable
        - Each split embed shows "Part X of Y" in footer
    """
    if not original_embed.description or len(original_embed.description) <= max_size:
        return [original_embed]
    
    # Split description into chunks at newline boundaries
    chunks: List[str] = []
    current_chunk = ""
    lines = original_embed.description.split('\n')
    
    for line in lines:
        # Check if adding this line would exceed the limit
        if len(current_chunk) + len(line) + 1 > max_size:
            if current_chunk:
                chunks.append(current_chunk)
                current_chunk = ""
        
        # Add line to current chunk
        if current_chunk:
            current_chunk += '\n' + line
        else:
            current_chunk = line
    
    # Add remaining content as final chunk
    if current_chunk:
        chunks.append(current_chunk)
    
    # Create embeds from chunks
    embeds: List[discord.Embed] = []
    total_parts = len(chunks)
    
    for idx, chunk in enumerate(chunks):
        embed = discord.Embed(
            description=chunk,
            color=original_embed.color
        )
        
        # First embed gets author/title
        if idx == 0:
            if original_embed.author:
                embed.set_author(name=original_embed.author.name)
            if original_embed.title:
                embed.title = original_embed.title
        
        # Add part indicator to footer
        part_text = f"Part {idx + 1} of {total_parts}"
        
        # Last embed gets original footer (with part indicator)
        if idx == total_parts - 1 and original_embed.footer:
            footer_text = f"{part_text} • {original_embed.footer.text}"
            embed.set_footer(text=footer_text)
        else:
            embed.set_footer(text=part_text)
        
        embeds.append(embed)
    
    return embeds


async def _delete_messages_by_filter(
    channel_id: str,
    filter_fn: Callable[[str, Dict[str, Any]], bool],
    context_description: str
) -> int:
    """
    Unified message deletion helper that deletes messages matching a filter function.
    
    Args:
        channel_id: Discord channel ID as string
        filter_fn: Function that takes (key, value) and returns True if message should be deleted
        context_description: Human-readable description for logging (e.g., "clan #ABC123")
        
    Returns:
        int: Number of message entries deleted
    """
    import QBcore  # type: ignore[misc]
    
    channel = QBcore.bot.get_channel(int(channel_id))
    if not channel:
        logging.warning(f"Channel {channel_id} not found for message deletion.")
        return 0

    keys_to_delete: List[str] = []
    for k, v in list(CACHE.leaderboard_messages.items()):
        if filter_fn(k, v):
            msg_ids = v.get('message_ids', '')  # type: ignore[misc]
            if msg_ids:
                for msg_id in msg_ids.split(','):  # type: ignore[union-attr]
                    try:
                        if not hasattr(channel, 'fetch_message'):
                            continue
                        msg = await channel.fetch_message(int(msg_id))  # type: ignore[union-attr]
                        await discord_retry(  # type: ignore[misc]
                            lambda: msg.delete(),  # type: ignore[misc]
                            f"delete_message_{msg_id}"
                        )
                        logging.debug(f"Deleted message {msg_id} in channel {channel_id}")
                        await asyncio.sleep(0.5)  # pace deletions to avoid Discord rate limits
                    except Exception as e:
                        logging.warning(f"Could not delete message {msg_id}: {e}")
            keys_to_delete.append(k)
    
    for k in keys_to_delete:
        await CACHE.delete_leaderboard_message(k)
    
    if keys_to_delete:
        logging.info(f"Deleted {len(keys_to_delete)} message entries for {context_description} in channel {channel_id}")
    
    return len(keys_to_delete)

async def delete_leaderboard_messages_for_clan_channel(clan_tag: str, channel_id: str) -> None:
    """
    Delete all leaderboard messages for the given clan and channel from Discord and cache.

    Args:
        clan_tag (str): Clash of Clans clan tag (uppercase, with #)
        channel_id (str): Discord channel ID as string
    """
    await _delete_messages_by_filter(
        channel_id,
        lambda k, v: v.get('clan_tag') == clan_tag and v.get('channel_id') == channel_id,  # type: ignore[misc, union-attr]
        f"clan {clan_tag}"
    )

async def cleanup_orphaned_messages_in_channel(channel: discord.TextChannel, bot: discord.Client) -> int:
    """
    Delete orphaned QapBot-authored messages from the specified Discord channel.

    Args:
        channel (discord.TextChannel): The Discord channel to scan for orphaned messages.
        bot (discord.Client): The Discord bot instance for author identification.

    Returns:
        int: Number of orphaned bot messages deleted in the channel.

    Behavior:
        - Scans recent history (up to 100 messages) in the channel.
        - Deletes messages authored by the bot that are not tracked in CACHE.leaderboard_messages.
        - Logs each deletion and returns the count.

    Example:
        deleted_count = await cleanup_orphaned_messages_in_channel(channel, bot)
    """
    bot_user_id = bot.user.id if hasattr(bot, 'user') and bot.user else None
    if not bot_user_id:
        logging.warning("Bot user ID not available; cannot scan for orphaned messages.")
        return 0
    # Build set of tracked message IDs for this channel
    tracked_ids = set()
    for entry in CACHE.leaderboard_messages.values():
        if entry.get('channel_id') == str(channel.id):
            mids = entry.get('message_ids', '')
            if mids:
                for mid in mids.split(','):
                    tracked_ids.add(mid.strip())
    try:
        messages = [msg async for msg in channel.history(limit=50)]
    except Exception as e:
        logging.warning(f"Could not fetch messages for channel {channel.id}: {e}")
        return 0
    bot_msgs = [msg for msg in messages if msg.author.id == bot_user_id]
    logging.debug(f"Bot messages in channel {channel.id}:\n{[msg.id for msg in bot_msgs]}")
    to_delete = [msg for msg in bot_msgs if str(msg.id) not in tracked_ids and not msg.interaction_metadata]

    async def _delete_msg(msg: discord.Message) -> bool:
        try:
            await discord_retry(lambda: msg.delete(), f"delete_orphaned_message_{msg.id}")
            logging.debug(f"Deleted orphaned bot message {msg.id} in channel '{channel.name}' on server '{channel.guild.name}'")
            logging.debug(f"Deleted message content:\n{msg.content}")
            return True
        except Exception as e:
            logging.warning(f"Failed to delete orphaned bot message {msg.id} in channel '{channel.name}' on server '{channel.guild.name}': {e}")
            return False

    delete_results = await asyncio.gather(*[_delete_msg(msg) for msg in to_delete])
    deleted_count = sum(1 for r in delete_results if r)
    logging.info(f"Deleted {deleted_count} orphaned bot messages in channel {channel.id}.")
    return deleted_count


async def cleanup_channel_messages(channel: discord.TextChannel, bot: discord.Client) -> Tuple[int, int]:
    """
    Unified cleanup function for a single Discord channel.
    
    Performs both cleanup tasks in one call:
    1. Deletes orphaned QapBot-authored messages not tracked in cache
    2. Removes stale leaderboard message IDs from cache (when messages no longer exist)
    
    This function is the single source of truth for channel message cleanup.
    Used by both channel-scoped (/admin cleanup messages) and global (/admin cleanup messages all) operations.
    
    Args:
        channel (discord.TextChannel): The Discord channel to clean up
        bot (discord.Client): The Discord bot instance for author identification
        
    Returns:
        Tuple[int, int]: (orphaned_deleted, stale_cleaned)
            - orphaned_deleted: Number of orphaned bot messages deleted from Discord
            - stale_cleaned: Number of stale cache entries removed
            
    Example:
        orphaned, stale = await cleanup_channel_messages(interaction.channel, bot)
        print(f"Deleted {orphaned} orphaned messages, cleaned {stale} stale entries")
    """
    bot_user_id = bot.user.id if hasattr(bot, 'user') and bot.user else None
    if not bot_user_id:
        logging.warning("Bot user ID not available; cannot clean up channel.")
        return 0, 0

    # Build the tracked-IDs index for this channel in one pass
    channel_id = str(channel.id)
    tracked_ids: Set[str] = set()
    entries_to_check: Dict[str, List[str]] = {}
    for k, v in list(CACHE.leaderboard_messages.items()):
        if v.get('channel_id') != channel_id:
            continue
        msg_ids_str = v.get('message_ids', '')
        if msg_ids_str:
            ids = [mid.strip() for mid in msg_ids_str.split(',') if mid.strip()]
            tracked_ids.update(ids)
            if ids:
                entries_to_check[k] = ids

    # Single history fetch — reused for both orphan and stale phases
    try:
        messages = [msg async for msg in channel.history(limit=50)]
    except Exception as e:
        logging.warning(f"Could not fetch messages for channel {channel.id}: {e}")
        return 0, 0
    history_ids = {str(msg.id) for msg in messages}

    # --- Phase 1: Orphan cleanup (no extra API calls beyond the history fetch) ---
    bot_msgs = [msg for msg in messages if msg.author.id == bot_user_id]
    to_delete = [msg for msg in bot_msgs if str(msg.id) not in tracked_ids and not msg.interaction_metadata]
    logging.debug(f"Bot messages in channel {channel.id}: {[msg.id for msg in bot_msgs]}")

    async def _delete_msg(msg: discord.Message) -> bool:
        try:
            await discord_retry(lambda: msg.delete(), f"delete_orphaned_message_{msg.id}")
            logging.debug(f"Deleted orphaned bot message {msg.id} in channel '{channel.name}' on server '{channel.guild.name}'")
            logging.debug(f"Deleted message content:\n{msg.content}")
            return True
        except Exception as e:
            logging.warning(f"Failed to delete orphaned bot message {msg.id} in channel '{channel.name}' on server '{channel.guild.name}': {e}")
            return False

    delete_results = await asyncio.gather(*[_delete_msg(msg) for msg in to_delete])
    orphaned_deleted = sum(1 for r in delete_results if r)
    logging.info(f"Deleted {orphaned_deleted} orphaned bot messages in channel {channel.id}.")

    # --- Phase 2: Stale cache cleanup ---
    # Fast path: any tracked ID present in the history fetch is confirmed existing — no extra call.
    # Only IDs absent from the history snapshot need individual fetch_message verification.
    missing_from_history: Set[str] = {
        mid for ids in entries_to_check.values() for mid in ids if mid not in history_ids
    }

    async def _check_exists(mid: str) -> Tuple[str, bool]:
        try:
            await channel.fetch_message(int(mid))
            return mid, True
        except Exception:
            return mid, False

    if missing_from_history:
        fetch_results = await asyncio.gather(*[_check_exists(mid) for mid in missing_from_history])
        confirmed_missing: Set[str] = {mid for mid, exists in fetch_results if not exists}
    else:
        confirmed_missing = set()

    # An entry is stale only when ALL its tracked messages are absent from history AND confirmed deleted
    keys_to_delete = [
        k for k, ids in entries_to_check.items()
        if all(mid not in history_ids and mid in confirmed_missing for mid in ids)
    ]
    for k in keys_to_delete:
        await CACHE.delete_leaderboard_message(k)

    if keys_to_delete:
        logging.info(f"Cleaned {len(keys_to_delete)} stale cache entries in channel {channel.id}.")

    return orphaned_deleted, len(keys_to_delete)


def normalize_clan_tag(clantag: str) -> Optional[str]:
    """
    Normalize and validate a clan tag to standard format with comprehensive validation.
    
    Converts user input to the standard Clash of Clans clan tag format (#ABCDEF123) and validates that it matches the expected pattern for CoC clan tags.
    
    CoC Tag Format Rules:
        - Never contains the letter 'o' (lowercase or uppercase)
        - The game automatically converts 'o' to '0' (zero) when users enter tags
        - This function replicates that behavior for user convenience
    
    Args:
        clantag (str): Raw clan tag input from user
    Returns:
        Optional[str]: Normalized clan tag with # prefix in uppercase format, or None if invalid
    
    Examples:
        normalize_clan_tag("L2JoC0PY") → "#L2J0C0PY"
        normalize_clan_tag("#l2joc0py") → "#L2J0C0PY"
        normalize_clan_tag("  L2J-OC0PY  ") → "#L2J0C0PY"
    """
    clantag = clantag.strip().upper()
    # Replace 'O' with '0' after uppercasing (CoC behavior)
    clantag = clantag.replace('O', '0')
    if not clantag.startswith("#"):
        clantag = "#" + clantag
    if not re.match(r"^#[A-Z0-9]{5,10}$", clantag):
        return None
    return clantag

def _get_clan_tag(clan_identifier: str) -> tuple[int, Optional[str]]:
    """
    Resolve a clan identifier (tag or name) to a normalized clan tag.

    Args:
        clan_identifier (str): Raw clan tag or clan name input.

    Returns:
        tuple[int, Optional[str]]: (number of matches, first normalized clan tag or None)
    """
    clan_identifier = clan_identifier.strip()
    norm_tag = normalize_clan_tag(clan_identifier)
    if norm_tag and norm_tag in CACHE.clan_name_cache:
        return (1, norm_tag)
    # Substring match to clan names (case-insensitive)
    matches = [
        tag for tag, clan_data in CACHE.clan_name_cache.items()
        if clan_identifier.lower() in clan_data["name"].lower()
    ]
    if matches:
        return (len(matches), matches[0])
    return (0, None)

def generate_family_tag(family_name: str, clan_tags: list[str]) -> str:
    """
    Generate a unique family_tag in the format # plus exactly 10 alphanumeric characters.
    
    Uses SHA256 hashing of family name, current timestamp, and sorted clan tags to ensure
    uniqueness and consistency.
    
    Args:
        family_name: Name of the clan family
        clan_tags: List of clan tags in the family
        
    Returns:
        str: Generated family tag (e.g., "#34570457B9")
        
    Example:
        >>> generate_family_tag("The QCrew Family", ["#L2J0C0PY", "#2J2YC9GC2"])
        '#34570457B9'
    """
    base = family_name + str(datetime.now().timestamp()) + ''.join(sorted(clan_tags))
    h = hashlib.sha256(base.encode('utf-8')).hexdigest().upper()
    # Use first 10 alphanumeric chars, replace non-alphanum with 'A'
    tag = '#' + ''.join([c if c.isalnum() else 'A' for c in h if c.isalnum()][:10])
    # Pad with 'A' if less than 10 chars (shouldn't happen with sha256)
    tag = tag.ljust(11, 'A')
    return tag

def _build_autocomplete_choices(  # type: ignore[reportUnusedFunction]
    items: List[Tuple[str, str]],
    current: str,
    max_choices: int = 25
) -> List[app_commands.Choice[str]]:
    """
    Generic autocomplete builder that filters and limits choices.
    
    Note: This function is currently unused but kept as a reusable helper for future autocomplete implementations.
    
    Args:
        items: List of (value, label) tuples
        current: Current user input to filter suggestions
        max_choices: Maximum number of choices to return
        
    Returns:
        List of app_commands.Choice objects for Discord autocomplete
    """
    current_lower = (current or "").lower()
    choices: List[app_commands.Choice[str]] = []
    
    for value, label in items:
        if not current_lower or current_lower in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=value))
            if len(choices) >= max_choices:
                break
    
    return choices

def normalize_family_tag(tag: str) -> Optional[str]:
    """
    Normalize and validate a family tag to strict format: # plus exactly 10 alphanumeric characters.
    
    CoC Tag Format Rules:
        - Never contains the letter 'o' (lowercase or uppercase)
        - The game automatically converts 'o' to '0' (zero) when users enter tags
        - This function replicates that behavior for consistency with clan tags
    
    Args:
        tag (str): Raw family tag input from user
    Returns:
        Optional[str]: Normalized family tag with # prefix and 10 uppercase alphanumeric characters, or None if invalid
    
    Examples:
        normalize_family_tag("34570457B9") → "#34570457B9"
        normalize_family_tag("#3457o457b9") → "#34570457B9"
    """
    tag = tag.strip().upper()
    # Replace 'O' with '0' after uppercasing (CoC behavior)
    tag = tag.replace('O', '0')
    if not tag.startswith("#"):
        tag = "#" + tag
    if not re.match(r"^#[A-Z0-9]{10}$", tag):
        return None
    return tag

from typing import Optional, List, Set
from discord import app_commands

async def get_clan_family_autocomplete_choices(
    current: str,
    channel_id: Optional[str] = None,
    guild_id: Optional[str] = None,
    mode: str = "all",
    max_choices: int = 25
) -> List[app_commands.Choice[str]]:
    """
    Generate autocomplete choices for clan/family parameters with configurable behavior.
    
    Args:
        current: Current user input to filter suggestions
        channel_id: Discord channel ID for subscription-based filtering
        guild_id: Discord guild ID; when provided with subscribed_first mode, restricts
            the fallback to clans subscribed in this guild (not all bot-wide clans)
        mode: Autocomplete mode - "all", "subscribed_only", "subscribed_first", "clans_only", or "guild_first"
        max_choices: Maximum number of choices to return
        
    Returns:
        List of app_commands.Choice objects for Discord autocomplete
        
    Modes:
        - "all": All tracked clans and families
        - "subscribed_only": Only clans/families subscribed in the given channel
        - "subscribed_first": Channel subscriptions first, then other guild-subscribed clans/families
        - "clans_only": Only tracked clans (excludes families)
        - "guild_first": Guild-subscribed clans only when current is empty; when current has text,
            guild-subscribed matches appear first, then all other tracked clans/families that match
    """
    from qapbot.cache_manager import CACHE
    
    current_lower = (current or "").lower()
    choices: List[app_commands.Choice[str]] = []
    seen: Set[str] = set()
    
    def _add_clan_choice(tag: str, name: str) -> bool:
        """Add clan choice if it matches filter and we haven't reached limit."""
        if tag in seen or len(choices) >= max_choices:
            return len(choices) >= max_choices
        
        label = f"{name} ({tag})" if name else tag
        if not current_lower or current_lower in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=tag))
            seen.add(tag)
        return len(choices) >= max_choices
    
    def _add_family_choice(tag: str, info: Dict[str, Any]) -> bool:
        """Add family choice if it matches filter and we haven't reached limit."""
        if tag in seen or len(choices) >= max_choices:
            return len(choices) >= max_choices
        
        name = info.get('name', 'Family')  # type: ignore[misc]
        label = f"{name} ({tag})"
        if not current_lower or current_lower in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=tag))
            seen.add(tag)
        return len(choices) >= max_choices
    
    # Handle guild_first mode: guild clans only when empty, extend to all when searching
    if mode == "guild_first":
        guild_tags: Set[str] = set()
        if guild_id and guild_id in CACHE.subscriptions:
            for ch_subs in CACHE.subscriptions[guild_id].values():
                for s in ch_subs:
                    t_ = s.get('clan_tag')
                    if t_ and t_ != 'PLAYERREGISTRATION':
                        guild_tags.add(t_)
        # Sort guild clans: CWL-active first (O(1) in-memory check), then alphabetical
        def _is_cwl_active(tag: str) -> bool:
            return bool(CACHE.temp_war_metadata.get(tag, {}).get('is_cwl', False))
        guild_clan_tags_sorted = sorted(
            (t for t in guild_tags if t not in CACHE.clan_families),
            key=lambda t: (0 if _is_cwl_active(t) else 1, (CACHE.get_clan_name(t, '') or t).lower()),
        )
        # Add guild-subscribed clans (non-families) filtered by current
        for tag in guild_clan_tags_sorted:
            name = CACHE.get_clan_name(tag, '')
            if name:
                _add_clan_choice(tag, name)
        # Add guild-subscribed families filtered by current
        for tag in guild_tags:
            if tag in CACHE.clan_families:
                _add_family_choice(tag, CACHE.clan_families[tag])
        if not current_lower:
            # No search text: only show guild clans (even if the guild has none yet)
            return choices[:max_choices]
        # Has search text: extend with non-guild tracked clans/families that match
        for tag, clan_data in CACHE.clan_name_cache.items():
            if len(choices) >= max_choices:
                break
            if tag not in guild_tags and tag not in CACHE.clan_families:
                _add_clan_choice(tag, clan_data.get('name', ''))
        for fam_tag, info in CACHE.clan_families.items():
            if len(choices) >= max_choices:
                break
            if fam_tag not in guild_tags:
                _add_family_choice(fam_tag, info)
        return choices[:max_choices]

    # Handle subscription-based modes
    if mode in ["subscribed_only", "subscribed_first"] and channel_id:
        channel_subs = CACHE.get_channel_subscriptions(channel_id)
        for s in channel_subs:
            tag = s.get('clan_tag')
            if not tag or tag == 'PLAYERREGISTRATION':
                continue
            
            if tag in CACHE.clan_families:
                if _add_family_choice(tag, CACHE.clan_families[tag]):
                    break
            else:
                name = CACHE.get_clan_name(tag, '')
                if name and tag and _add_clan_choice(tag, name):
                    break
        
        # For subscribed_only mode, return only subscribed items
        if mode == "subscribed_only":
            return choices[:max_choices]
    
    # Determine which clan tags are eligible for the fallback.
    # In subscribed_first mode with a known guild, restrict to clans/families
    # subscribed in any channel of this guild so cross-server clans are excluded.
    if mode == "subscribed_first" and guild_id and guild_id in CACHE.subscriptions:
        guild_tags: Set[str] = set()
        for ch_subs in CACHE.subscriptions[guild_id].values():
            for s in ch_subs:
                t_ = s.get('clan_tag')
                if t_ and t_ != 'PLAYERREGISTRATION':
                    guild_tags.add(t_)
        fallback_clan_tags = guild_tags
    else:
        fallback_clan_tags = set(CACHE.clan_name_cache.keys())

    # Add remaining tracked clans
    if len(choices) < max_choices:
        for tag in fallback_clan_tags:
            name = CACHE.get_clan_name(tag, '')
            if name and _add_clan_choice(tag, name):
                break
    
    # Add families (skip if mode is "clans_only")
    if len(choices) < max_choices and mode != "clans_only":
        # In subscribed_first mode with guild scope, only include guild-subscribed families
        if mode == "subscribed_first" and guild_id and guild_id in CACHE.subscriptions:
            for fam_tag in fallback_clan_tags:
                if fam_tag in CACHE.clan_families:
                    if _add_family_choice(fam_tag, CACHE.clan_families[fam_tag]):
                        break
        else:
            for fam_tag, info in CACHE.clan_families.items():
                if _add_family_choice(fam_tag, info):
                    break
    
    return choices[:max_choices]

async def get_mode_autocomplete_choices(
    current: str,
    include_cwl_variants: bool = False,
    subscribed_modes_only: bool = False,
    clan_tag: Optional[str] = None,
    channel_id: Optional[str] = None,
    max_choices: int = 25
) -> List[app_commands.Choice[str]]:
    """
    Generate autocomplete choices for leaderboard mode parameters.
    
    Args:
        current: Current user input to filter suggestions
        include_cwl_variants: Whether to include "_cwl" suffixed modes
        subscribed_modes_only: Only show modes subscribed for the given clan in channel
        clan_tag: Clan tag for subscription filtering
        channel_id: Channel ID for subscription filtering  
        max_choices: Maximum number of choices to return
        
    Returns:
        List of app_commands.Choice objects for Discord autocomplete
    """
    from qapbot.formatting import MODE_REGISTRY
    from qapbot.cache_manager import CACHE
    
    current_lower = (current or "").lower()
    choices: List[app_commands.Choice[str]] = []
    
    if subscribed_modes_only and clan_tag and channel_id:
        # Find subscribed modes for this clan in this channel
        channel_subs = CACHE.get_channel_subscriptions(channel_id)
        subscribed_modes: List[str] = []
        for s in channel_subs:
            if s.get('clan_tag') == clan_tag:
                mode = s.get('subscription_type', '')
                if mode and (not current_lower or current_lower in mode.lower()):
                    subscribed_modes.append(mode)
        
        # Add choices for subscribed modes
        for mode in sorted(set(subscribed_modes)):
            description = MODE_REGISTRY.get(mode, {}).get('description', mode)
            name = f"{mode} - {description}"[:100]  # Discord enforces 100-char max
            choices.append(app_commands.Choice(name=name, value=mode))
            if len(choices) >= max_choices:
                break
    else:
        # Filter modes based on criteria
        valid_modes = [
            m for m in MODE_REGISTRY.keys()
            if (include_cwl_variants or not m.endswith("_cwl"))
            and (not current_lower or current_lower in m.lower())
        ]
        
        for mode in sorted(valid_modes):
            description = MODE_REGISTRY.get(mode, {}).get('description', mode)
            name = f"{mode} - {description}"[:100]  # Discord enforces 100-char max
            choices.append(app_commands.Choice(name=name, value=mode))
            if len(choices) >= max_choices:
                break
    
    return choices[:max_choices]


def _extract_clan_and_coc_role(player_obj) -> Tuple[Optional[str], Optional[str]]:
    """
    Resolve (current_clan_tag, coc_role) from a coc.py Player object.

    coc_role uses the same mapping as the periodic sync (coc_cache.py): Role.name gives
    "member"/"elder"/"co_leader"/"leader" — co_leader is remapped to "coLeader" to match
    COC_ROLE_PRIORITY in guild_role_manager.py.
    """
    current_clan_tag = player_obj.clan.tag if player_obj.clan else None
    raw_role = getattr(player_obj, "role", None)
    raw_role_name = getattr(raw_role, "name", None) if raw_role else None
    coc_role = ("coLeader" if raw_role_name == "co_leader" else raw_role_name) if raw_role_name else None
    return current_clan_tag, coc_role


async def _link_player_to_user(
    target_user_id: int,
    player_tag_raw: str,
    display_name: str,
    admin_override: bool = False,
    api_token_override: bool = False,
    api_token_user_id: Optional[str] = None,
    guild_name: Optional[str] = None
) -> Tuple[bool, str]:
    """
    Core logic for linking a CoC player tag to a Discord user in CACHE.user_accounts.
    
    Shared by welcome message account linking, clan management, and admin operations.
    
    Flow:
        - If player already linked to target user: no-op (success).
        - If player exists in UNASSIGNED: restore (preserve metadata).
        - Else: fetch player name from CoC API and add as unverified.
        - Persist on mutation via CACHE.persist_user() (write-through).
    
    Args:
        target_user_id: Discord user ID to receive the linked player.
        player_tag_raw: Raw CoC player tag input.
        display_name: Display name for the user (for messages).
        admin_override: If True, allows bot admin to override verified player security check (default: False)
        api_token_override: If True, user provided valid API token as proof of ownership (default: False)
        api_token_user_id: User ID of previous owner when using API token override (for DM notification)
        guild_name: Discord server name where this linking action happened (for DM notification context)
    
    Returns:
        (ok, message)
    """
    from qapbot.cache_manager import CACHE
    
    normalized_tag = normalize_clan_tag(player_tag_raw)
    if not normalized_tag:
        return False, "Invalid player tag format. Please use a valid player tag (e.g., #ABC123)."
    
    account_tag = str(target_user_id)
    
    # Read existing user entry without mutating cache yet.
    # Mutation and persistence happen only after all validation passes.
    existing_entry = CACHE.user_accounts.get(account_tag)
    if isinstance(existing_entry, dict):
        existing_players = existing_entry.get("players", [])
        user_players_for_check = existing_players if isinstance(existing_players, list) else []
    else:
        user_players_for_check = []
    
    # Check if player already linked to this user
    for p in user_players_for_check:
        if isinstance(p, dict):
            current_tag = p.get("player_tag", "")
            if current_tag == normalized_tag:
                from qapbot.i18n import t
                player_name = p.get("player_name", "Unknown")
                verification_status = "✅ verified" if p.get("verified", False) else "❌ unverified"
                return True, t('playerregistration.player_already_linked', player_name=player_name, player_tag=normalized_tag, status=verification_status)
    
    # SECURITY CHECK - Prevent account theft by checking if player is already verified by another user
    verified_owner = get_verified_player_owner(normalized_tag, account_tag)
    if verified_owner and not admin_override and not api_token_override:
        from qapbot.i18n import t
        logging.warning(f"SECURITY: User {account_tag} ({display_name}) attempted to link {normalized_tag} which is verified by {verified_owner}")
        return False, t('playerregistration.player_already_verified_by_other', player_tag=normalized_tag, owner=verified_owner)
    
    # API TOKEN OVERRIDE - User provided valid API token as proof of ownership
    if api_token_override and api_token_user_id:
        logging.warning(f"API TOKEN OVERRIDE: User {account_tag} ({display_name}) re-linking {normalized_tag} from user {api_token_user_id} via API token")
        
        # Remove player from previous owner and send DM notification
        for other_user_id, other_user_data in CACHE.user_accounts.items():
            if other_user_id != api_token_user_id or not other_user_data:
                continue
            
            other_players = other_user_data.get("players", [])
            for idx, other_player in enumerate(other_players):
                if isinstance(other_player, dict):
                    current_tag = other_player.get("player_tag", "")
                    if current_tag == normalized_tag:
                        removed_player = other_players.pop(idx)
                        await CACHE.persist_user(api_token_user_id)
                        logging.info(f"Removed player {normalized_tag} from {other_user_id} via API token override")
                    
                        # Send DM notification to the previous owner
                        try:
                            from qapbot.i18n import t
                            player_name_for_dm = removed_player.get("player_name", "Unknown")
                            other_display_name = other_user_data.get("display_name", "Unknown User")
                            
                            dm_message = t(
                                'playerregistration.api_token_removed_dm',
                                player_name=player_name_for_dm,
                                player_tag=normalized_tag,
                                guild_name=guild_name or t('playerregistration.unknown_server_name')
                            )
                            success = await CACHE.send_user_dm(str(other_user_id), dm_message)
                            
                            if success:
                                logging.info(f"Sent DM notification to {other_display_name} ({other_user_id}) about API-token-removed account {normalized_tag}")
                            else:
                                logging.warning(f"Could not send DM to {other_user_id} about API-token-removed account {normalized_tag}")
                        except Exception as dm_error:
                            logging.warning(f"Could not send DM to {other_user_id} about API-token-removed account: {dm_error}")
                    
                        break
            break
    
    # ADMIN OVERRIDE - Bot admin is forcefully re-linking a verified player
    elif verified_owner and admin_override:
        logging.warning(f"ADMIN OVERRIDE: Bot admin forcefully re-linking {normalized_tag} from {verified_owner} to {display_name} ({account_tag})")
        
        # Remove player from verified owner and send DM notification
        for other_user_id, other_user_data in CACHE.user_accounts.items():
            if other_user_id == account_tag or not other_user_data:
                continue
            
            other_players = other_user_data.get("players", [])
            for idx, other_player in enumerate(other_players):
                if isinstance(other_player, dict):
                    current_tag = other_player.get("player_tag", "")
                    if current_tag == normalized_tag:
                        removed_player = other_players.pop(idx)
                        await CACHE.persist_user(other_user_id)
                        logging.info(f"Removed player {normalized_tag} from {other_user_id} via admin override")
                    
                        # Send DM notification to the previous verified owner
                        try:
                            from qapbot.i18n import t
                            player_name_for_dm = removed_player.get("player_name", "Unknown")
                            other_display_name = other_user_data.get("display_name", "Unknown User")
                            
                            dm_message = t(
                                'playerregistration.admin_removed_verified_dm',
                                player_name=player_name_for_dm,
                                player_tag=normalized_tag,
                                guild_name=guild_name or t('playerregistration.unknown_server_name')
                            )
                            success = await CACHE.send_user_dm(str(other_user_id), dm_message)
                            
                            if success:
                                logging.info(f"Sent DM notification to {other_display_name} ({other_user_id}) about admin-removed verified account {normalized_tag}")
                            else:
                                logging.warning(f"Could not send DM to {other_user_id} about admin-removed verified account {normalized_tag}")
                        except Exception as dm_error:
                            logging.warning(f"Could not send DM to {other_user_id} about admin-removed verified account: {dm_error}")
                    
                        break
    
    # UNVERIFIED DUPLICATE CHECK - Remove unverified player from another user if found
    # This prevents multiple users from having the same unverified account
    for other_user_id, other_user_data in CACHE.user_accounts.items():
        if other_user_id == account_tag or other_user_id == "UNASSIGNED":
            continue  # Skip current user and UNASSIGNED pool
        
        if not other_user_data:
            continue
        
        other_players = other_user_data.get("players", [])
        for idx, other_player in enumerate(other_players):
            if not isinstance(other_player, dict):
                continue
            
            current_tag = other_player.get("player_tag", "")
            if current_tag == normalized_tag and not other_player.get("verified", False):
                # Found unverified duplicate - remove it from other user
                other_players.pop(idx)
                await CACHE.persist_user(other_user_id)
                
                other_display_name = other_user_data.get("display_name", "Unknown User")
                logging.warning(
                    f"SECURITY: Removed unverified player {normalized_tag} from {other_display_name} ({other_user_id}) "
                    f"because {display_name} ({account_tag}) is now linking it"
                )
                
                # Try to send DM to the other user
                try:
                    from qapbot.i18n import t
                    player_name_for_dm = other_player.get("player_name", "Unknown")
                    dm_message = t(
                        'playerregistration.unverified_removed_dm',
                        player_name=player_name_for_dm,
                        player_tag=normalized_tag,
                        guild_name=guild_name or t('playerregistration.unknown_server_name')
                    )
                    success = await CACHE.send_user_dm(str(other_user_id), dm_message)
                    
                    if success:
                        logging.info(f"Sent DM notification to {other_display_name} ({other_user_id}) about removed unverified account")
                    else:
                        logging.warning(f"Could not send DM to {other_user_id} about removed unverified account")
                except Exception as dm_error:
                    logging.warning(f"Error sending DM to {other_user_id} about removed account: {dm_error}")
                
                break  # Only one duplicate should exist
    
    # Check UNASSIGNED pool for existing player data
    unassigned_entry = CACHE.user_accounts.get("UNASSIGNED", {"display_name": "UNASSIGNED", "players": []})
    unassigned_players = unassigned_entry.get("players", []) if unassigned_entry else []
    unassigned_player = None
    for p in unassigned_players:
        if isinstance(p, dict):
            current_tag = p.get("player_tag", "")
            if current_tag == normalized_tag:
                unassigned_player = p
                break
    
    if unassigned_player:
        # Restore from UNASSIGNED pool
        # SECURITY: Always reset verified status to False for account theft protection
        unassigned_player["verified"] = False

        # Refresh live CoC data before restoring — the UNASSIGNED pool is excluded from the
        # periodic per-clan sync (coc_cache.py skips "UNASSIGNED"), so th_level/
        # current_clan_tag/coc_role can be stale or (for older entries) never set at all.
        # Without this, the instant sync_roles_for_user() right after restore has nothing
        # to assign the CoC/clan role from. Best-effort: keep stale values on API failure.
        try:
            restored_player_obj = await CACHE.get_player(normalized_tag)
            if restored_player_obj:
                if hasattr(restored_player_obj, "town_hall"):
                    unassigned_player["th_level"] = restored_player_obj.town_hall
                unassigned_player["current_clan_tag"], unassigned_player["coc_role"] = _extract_clan_and_coc_role(restored_player_obj)
        except Exception as api_error:
            logging.warning(f"Could not refresh live CoC data for {normalized_tag} during UNASSIGNED restore: {api_error}")

        # Ensure target user entry exists only now that mutation is required
        target_entry = CACHE.user_accounts.get(account_tag)
        if not isinstance(target_entry, dict):
            target_entry = {"display_name": display_name, "players": []}
            CACHE.user_accounts[account_tag] = target_entry
        target_entry["display_name"] = display_name
        target_players = target_entry.get("players")
        if not isinstance(target_players, list):
            target_players = []
            target_entry["players"] = target_players
        target_players.append(unassigned_player)

        if unassigned_entry:
            unassigned_entry["players"] = [
                p for p in unassigned_players
                if not (isinstance(p, dict) and p.get("player_tag", "") == normalized_tag)
            ]
            if not unassigned_entry["players"]:
                CACHE.user_accounts.pop("UNASSIGNED", None)
                await CACHE.delete_user_account("UNASSIGNED")
            else:
                CACHE.user_accounts["UNASSIGNED"] = unassigned_entry
        
        await CACHE.persist_user(account_tag)
        # Also persist UNASSIGNED changes if the pool was modified
        if "UNASSIGNED" in CACHE.user_accounts:
            await CACHE.persist_user("UNASSIGNED")
        from qapbot.i18n import t
        player_name = unassigned_player.get("player_name", "Unknown")
        verification_status = "❌ unverified"  # Always unverified after restoration
        return True, t('playerregistration.restored_from_unassigned', player_name=player_name, player_tag=normalized_tag, display_name=display_name, status=verification_status)
    
    # Fetch from CoC API
    try:
        player_obj = await CACHE.get_player(normalized_tag)
        if not player_obj:
            from qapbot.i18n import t
            return False, t('playerregistration.player_not_found_coc', player_tag=normalized_tag)
        
        player_name = player_obj.name if hasattr(player_obj, "name") else "Unknown"
        th_level = player_obj.town_hall if hasattr(player_obj, "town_hall") else None
        current_clan_tag, coc_role = _extract_clan_and_coc_role(player_obj)
    except Exception as api_error:
        from qapbot.i18n import t
        logging.warning(f"Failed to fetch player info for {normalized_tag}: {api_error}")
        return False, t('playerregistration.player_not_found_coc', player_tag=normalized_tag)
    
    # Add new player as unverified with TH level and clan info
    target_entry = CACHE.user_accounts.get(account_tag)
    if not isinstance(target_entry, dict):
        target_entry = {"display_name": display_name, "players": []}
        CACHE.user_accounts[account_tag] = target_entry
    target_entry["display_name"] = display_name
    target_players = target_entry.get("players")
    if not isinstance(target_players, list):
        target_players = []
        target_entry["players"] = target_players

    target_players.append({
        "player_tag": normalized_tag,  # Database uses player_tag field
        "player_name": player_name,
        "verified": False,
        "th_level": th_level,
        "current_clan_tag": current_clan_tag,
        "coc_role": coc_role
    })
    await CACHE.persist_user(account_tag)
    
    # Update clan_name_cache if player is in a clan
    if current_clan_tag and player_obj.clan:
        clan_data = CACHE.clan_name_cache.get(current_clan_tag)
        if isinstance(clan_data, dict):  # type: ignore[misc]
            # New format - update name if changed
            if clan_data.get('name') != player_obj.clan.name:
                clan_data['name'] = player_obj.clan.name
                await CACHE.persist_clan(current_clan_tag)
        elif clan_data is not None:
            # Old format (string) - convert to dict
            CACHE.clan_name_cache[current_clan_tag] = {
                "name": player_obj.clan.name,
                "has_active_subscriptions": False,
                "last_war_update": None,
                "warlog_is_public": None,
                "last_checked_via_api": None
            }
            await CACHE.persist_clan(current_clan_tag)
    
    from qapbot.i18n import t
    return True, t('playerregistration.linked_unverified', player_name=player_name, player_tag=normalized_tag, display_name=display_name)


# ========================================================================================
# Role Assignment Helper Functions
# ========================================================================================

def is_player_in_member_clans(player_clan_tag: Optional[str], guild_id: int) -> bool:
    """
    Check if a player's clan is eligible for member role assignment.
    
    Checks if player's current clan is in:
    1. The guild's member_clans list (individual clans)
    2. Any of the guild's member_families (clan families)
    
    Args:
        player_clan_tag: Current clan tag of the player (None if no clan)
        guild_id: Discord guild ID
        
    Returns:
        bool: True if player is in a member clan/family, False otherwise
        
    Example:
        >>> is_player_in_member_clans("#L2J0C0PY", 1234567890)
        True  # Player is in a member clan
    """
    if not player_clan_tag:
        return False
    
    guild_id_str = str(guild_id)
    config = CACHE.server_config.get(guild_id_str, {})
    
    # Check individual member clans
    member_clans = config.get("member_clans", [])
    if player_clan_tag in member_clans:
        return True
    
    # Check member families
    member_families = config.get("member_families", [])
    for family_id in member_families:
        family_data = CACHE.clan_families.get(family_id, {})
        family_clans = family_data.get("clans", [])
        if player_clan_tag in family_clans:
            return True
    
    return False


async def assign_member_role(guild: discord.Guild, user_id: int, player_name: str, player_clan_tag: Optional[str]) -> None:
    """
    Assign member role to a Discord user if role system is enabled and player is in member clans.
    
    Args:
        guild: Discord guild object
        user_id: Discord user ID to assign role to
        player_name: Player name (for logging)
        player_clan_tag: Player's current clan tag
        
    Returns:
        None (logs errors if role assignment fails)
        
    Example:
        >>> await assign_member_role(guild, 123456789, "PlayerName", "#L2J0C0PY")
    """
    guild_id_str = str(guild.id)
    config = CACHE.server_config.get(guild_id_str, {})
    
    # Check if role system is enabled
    if not config.get("role_system_enabled", False):
        logging.debug(f"Role system not enabled for guild {guild.id}, skipping member role assignment")
        return
    
    # Check if player is in member clans/families
    if not is_player_in_member_clans(player_clan_tag, guild.id):
        logging.debug(f"Player {player_name} not in member clans/families, skipping member role assignment")
        return
    
    # Get member role ID
    member_role_id = config.get("member_role_id")
    if not member_role_id:
        logging.warning(f"Member role not configured for guild {guild.id}, cannot assign role")
        return
    
    # Get the role object
    try:
        member_role = guild.get_role(int(member_role_id))
        if not member_role:
            logging.error(f"Member role {member_role_id} not found in guild {guild.id}")
            return
    except (ValueError, TypeError) as e:
        logging.error(f"Invalid member role ID {member_role_id} for guild {guild.id}: {e}")
        return
    
    # Get the guild member
    try:
        member = await guild.fetch_member(user_id)
        if not member:
            logging.warning(f"User {user_id} not found in guild {guild.id}, cannot assign member role")
            return
    except discord.NotFound:
        logging.warning(f"User {user_id} not found in guild {guild.id}, cannot assign member role")
        return
    except Exception as e:
        logging.error(f"Failed to fetch user {user_id} from guild {guild.id}: {e}")
        return
    
    # Assign the role and remove newbie role (mutually exclusive)
    try:
        if member_role not in member.roles:
            await member.add_roles(member_role, reason=f"Player {player_name} verified in member clan")
            logging.info(f"AUTO-ROLE: Assigned member role to {member.display_name} (user_id={user_id}) for player {player_name}")
        else:
            logging.debug(f"User {member.display_name} already has member role")
        # Remove newbie role if present (mutually exclusive with member role)
        newbie_role_id = config.get("newbie_role_id")
        if newbie_role_id:
            try:
                newbie_role = guild.get_role(int(newbie_role_id))
                if newbie_role and newbie_role in member.roles:
                    await member.remove_roles(newbie_role, reason=f"Player {player_name} promoted to member (newbie role removed)")
                    logging.info(f"AUTO-ROLE: Removed newbie role from {member.display_name} (user_id={user_id}) after member role assignment")
            except (ValueError, TypeError):
                pass
    except discord.Forbidden:
        logging.error(f"Missing permissions to assign member role to user {user_id} in guild {guild.id}")
    except Exception as e:
        logging.error(f"Failed to assign member role to user {user_id} in guild {guild.id}: {e}")


async def assign_newbie_role(guild: discord.Guild, user_id: int) -> None:
    """
    Assign newbie role to a Discord user if role system is enabled.
    
    Args:
        guild: Discord guild object
        user_id: Discord user ID to assign role to
        
    Returns:
        None (logs errors if role assignment fails)
        
    Example:
        >>> await assign_newbie_role(guild, 123456789)
    """
    guild_id_str = str(guild.id)
    config = CACHE.server_config.get(guild_id_str, {})
    
    # Check if role system is enabled
    if not config.get("role_system_enabled", False):
        logging.debug(f"Role system not enabled for guild {guild.id}, skipping newbie role assignment")
        return
    
    # Get newbie role ID
    newbie_role_id = config.get("newbie_role_id")
    if not newbie_role_id:
        logging.warning(f"Newbie role not configured for guild {guild.id}, cannot assign role")
        return
    
    # Get the role object
    try:
        newbie_role = guild.get_role(int(newbie_role_id))
        if not newbie_role:
            logging.error(f"Newbie role {newbie_role_id} not found in guild {guild.id}")
            return
    except (ValueError, TypeError) as e:
        logging.error(f"Invalid newbie role ID {newbie_role_id} for guild {guild.id}: {e}")
        return
    
    # Get the guild member
    try:
        member = await guild.fetch_member(user_id)
        if not member:
            logging.warning(f"User {user_id} not found in guild {guild.id}, cannot assign newbie role")
            return
    except discord.NotFound:
        logging.warning(f"User {user_id} not found in guild {guild.id}, cannot assign newbie role")
        return
    except Exception as e:
        logging.error(f"Failed to fetch user {user_id} from guild {guild.id}: {e}")
        return
    
    # Assign the role
    try:
        if newbie_role not in member.roles:
            await member.add_roles(newbie_role, reason="New user joined server")
            logging.info(f"AUTO-ROLE: Assigned newbie role to {member.display_name} (user_id={user_id}) on server join")
        else:
            logging.debug(f"User {member.display_name} already has newbie role")
    except discord.Forbidden:
        logging.error(f"Missing permissions to assign newbie role to user {user_id} in guild {guild.id}")
    except Exception as e:
        logging.error(f"Failed to assign newbie role to user {user_id} in guild {guild.id}: {e}")


async def complete_account_linking_flow(
    *,
    interaction: discord.Interaction,
    target_user_id: int,
    player_tag: str,
    api_token: Optional[str] = None,
    show_api_prompt: bool = False,
    send_success_message: bool = True,
    use_followup: bool = True,
    player_selection_interaction: Optional[discord.Interaction] = None,
    skip_notification_prompt: bool = False,
    admin_override: bool = False
) -> Tuple[bool, str]:
    """
    Complete modular flow for account linking (shared by welcome message and clan management).
    
    Sequential steps:
    1. Registration - link player to Discord user (via _link_player_to_user)
    2. API Verification - if api_token provided, verify; if show_api_prompt=True, show prompt
    3. Notification Check - check and prompt for war notifications (unless skip_notification_prompt=True)
    
    Args:
        interaction: Discord interaction (for followup messages)
        target_user_id: Discord user ID to link player to
        player_tag: CoC player tag (already normalized)
        api_token: Optional API token for immediate verification
        show_api_prompt: If True and no api_token, show API verification prompt
        send_success_message: If True, send success message via followup
        use_followup: If True, use interaction.followup; if False, use interaction.response
        skip_notification_prompt: If True, skip notification prompt (used when notifications already configured)
        admin_override: If True, allows bot admin to override verified player security check (default: False)
        
    Returns:
        (ok, message) - Success status and message
    """
    from qapbot.cache_manager import CACHE
    import logging
    
    account_tag = str(target_user_id)
    
    # Fetch display name (try guild member first, then fall back to user object)
    display_name = f"UserID:{target_user_id}"
    try:
        if interaction.guild:
            member = await interaction.guild.fetch_member(target_user_id)
            if member:
                display_name = member.display_name
    except Exception:
        # User not in guild - get from cache
        try:
            user_data = await CACHE.ensure_user_metadata(str(target_user_id))
            display_name = user_data.get("display_name", "Unknown User")
        except Exception as user_fetch_error:
            logging.warning(f"Could not fetch display name for user {target_user_id}: {user_fetch_error}")
    
    # STEP 0: API TOKEN OVERRIDE CHECK - Check if player is already linked to someone else
    # If yes and no API token provided, prompt for API token to prove ownership
    normalized_tag = normalize_clan_tag(player_tag)
    if not normalized_tag:
        return False, "Invalid player tag format"
    existing_owner = get_any_player_owner(normalized_tag, account_tag)
    
    if existing_owner and not api_token and not admin_override:
        # Player is already linked to another user
        prev_user_id, prev_display_name, is_verified = existing_owner
        
        # Check if interaction response has already been used
        if interaction.response.is_done():
            # Cannot show modal after response is used - send followup message instead
            from qapbot.i18n import t
            guild_id = interaction.guild.id if interaction.guild else None
            user_id = str(interaction.user.id)
            
            # Verified accounts are API-protected (re-link requires proof of ownership);
            # unverified accounts still require a token, but aren't "protected" per se.
            status_key = (
                'playerregistration.already_linked_needs_api_token_verified'
                if is_verified else
                'playerregistration.already_linked_needs_api_token_unverified'
            )
            error_msg = t(
                status_key,
                user_id=user_id,
                guild_id=guild_id,
                player_tag=normalized_tag,
                prev_display_name=prev_display_name
            )
            
            await interaction.followup.send(error_msg, ephemeral=True)
            return False, "API token required - instruction message sent"
        
        # Show modal asking for API token (only works if response not yet used)
        from qapbot.ui_registration import ApiTokenOwnershipModal
        from qapbot.i18n import t
        guild_id = interaction.guild.id if interaction.guild else None
        
        modal = ApiTokenOwnershipModal(
            player_tag=normalized_tag,
            target_user_id=target_user_id,
            previous_owner_name=prev_display_name,
            previous_owner_id=prev_user_id,
            is_verified=is_verified,
            player_selection_interaction=player_selection_interaction,
            guild_id=guild_id
        )
        
        # Show modal asking for API token
        await interaction.response.send_modal(modal)
        
        # Return early - modal will handle the rest
        return True, "API token required - modal shown"
    
    # If API token is provided and player is linked elsewhere, validate it first
    api_token_override = False
    api_token_user_id = None
    
    if api_token and existing_owner:
        prev_user_id, prev_display_name, is_verified = existing_owner
        
        # Validate API token for this player
        is_valid, validation_msg = await validate_api_token_for_player(normalized_tag, api_token)
        
        if not is_valid:
            # API token validation failed
            from qapbot.i18n import t
            error_msg = t('playerregistration.api_token_invalid', guild_id=interaction.guild.id if interaction.guild else None, message=validation_msg)
            
            if send_success_message:
                if use_followup:
                    await interaction.followup.send(error_msg, ephemeral=True)
                else:
                    await interaction.response.send_message(error_msg, ephemeral=True)
            
            return False, error_msg
        
        # API token is valid - set override flags
        api_token_override = True
        api_token_user_id = prev_user_id
        logging.info(f"API token validated for {normalized_tag} - user {account_tag} will override link from {prev_user_id}")
    
    # STEP 1: REGISTRATION - Link player to user
    ok, link_msg = await _link_player_to_user(
        target_user_id, 
        player_tag, 
        display_name, 
        admin_override=admin_override,
        api_token_override=api_token_override,
        api_token_user_id=api_token_user_id,
        guild_name=interaction.guild.name if interaction.guild else None
    )
    
    if not ok:
        return False, link_msg
    
    # Check if player was already linked (message starts with "Player already linked:")
    if link_msg.startswith("Player already linked:"):
        # Player was already in account
        # If NO api_token provided, just check notifications and return
        if not api_token:
            if send_success_message:
                if use_followup:
                    await interaction.followup.send(link_msg, ephemeral=True)
                else:
                    await interaction.response.send_message(link_msg, ephemeral=True)
            
            # Still check and prompt for war notifications (modular flow applies to all paths)
            if not skip_notification_prompt:
                await check_and_prompt_war_notifications(
                    user_id=account_tag,
                    interaction=interaction,
                    success_message="",  # Already sent message above
                    use_followup=True,  # Always use followup after sending message
                    player_selection_interaction=player_selection_interaction
                )
            return True, link_msg
        
        # If api_token IS provided, continue to verification step below
        # (Don't return early - allow verification to proceed)
    
    # Get player entry for verification/notification checks
    user_entry = CACHE.user_accounts.get(account_tag)
    if not user_entry:
        from qapbot.i18n import t
        return False, t('playerregistration.failed_retrieve_user_entry', guild_id=interaction.guild.id if interaction.guild else None)
    
    player_entry = get_user_player(user_entry, player_tag)
    if not player_entry:
        from qapbot.i18n import t
        return False, t('playerregistration.failed_retrieve_player_entry', guild_id=interaction.guild.id if interaction.guild else None, player_tag=player_tag)
    
    player_name = player_entry.get("player_name", "Unknown")
    is_verified = player_entry.get("verified", False)

    async def _assign_and_sync_roles_for_link() -> None:
        """STEP 2.5: member role assignment + CoC/clan role sync.

        SIMPLE mode doesn't gate roles on verification, so this must run right after
        linking (not deferred until the user interacts with the optional API-verification
        prompt) — called both here, before that prompt is shown, and again below for the
        normal (no-prompt) path. Safe to call twice: assign_member_role/sync_roles_for_user
        are idempotent.
        """
        if not interaction.guild:
            return
        should_assign_member_role = False
        _linking_guild_id_str = str(interaction.guild.id)
        _member_role_strict: bool = CACHE.server_config.get(_linking_guild_id_str, {}).get("member_role_strict", False)

        if not _member_role_strict:
            # SIMPLE mode: any linked account qualifies
            should_assign_member_role = True
        elif (api_token_override or is_verified or (api_token and player_entry.get("verified", False))) or admin_override:
            # STRICT mode: require API verification or admin override
            should_assign_member_role = True

        if should_assign_member_role:
            player_clan_tag = player_entry.get("current_clan_tag")
            try:
                await assign_member_role(interaction.guild, target_user_id, player_name, player_clan_tag)
            except Exception as e:
                logging.error(f"Failed to assign member role to user {target_user_id}: {e}")

        try:
            from qapbot.guild_role_manager import sync_roles_for_user
            await sync_roles_for_user(interaction.guild, _linking_guild_id_str, target_user_id)
        except Exception as _role_sync_e:
            logging.warning(f"[ROLE-SYNC] Post-link role sync failed for {target_user_id}: {_role_sync_e}")

    # STEP 2: API VERIFICATION
    verification_message = None
    
    # If API token override was used, mark player as verified automatically
    if api_token_override:
        player_entry["verified"] = True
        await CACHE.persist_user(account_tag)
        from qapbot.i18n import t
        verification_message = t('playerregistration.api_token_verified_success', guild_id=interaction.guild.id if interaction.guild else None, player_name=player_name, player_tag=normalized_tag)
        logging.info(f"USER ACTION: Verified player {player_name} ({normalized_tag}) for user {target_user_id} via API token ownership proof")
    elif is_verified:
        from qapbot.i18n import t
        verification_message = t('playerregistration.player_already_verified', guild_id=interaction.guild.id if interaction.guild else None)
    elif api_token:
        # API token provided - attempt verification
        guild_id = interaction.guild.id if interaction.guild else None
        verified, verify_msg = await verify_and_update_player(
            player_entry, player_tag, api_token, guild_id=guild_id
        )
        if verified:
            await CACHE.persist_user(account_tag)
            from qapbot.i18n import t
            verification_message = t('playerregistration.player_verified_success', guild_id=interaction.guild.id if interaction.guild else None, message=verify_msg)
            logging.info(f"USER ACTION: Verified player {player_name} ({player_tag}) for user {target_user_id}")
            logging.debug(f"[VERIFICATION-SUCCESS] verification_message={verification_message}")
        else:
            # Verification FAILED - send error message immediately and return
            from qapbot.i18n import t
            verification_message = t('playerregistration.player_verification_failed', guild_id=interaction.guild.id if interaction.guild else None, message=verify_msg)
            logging.warning(f"Verification failed for player {player_name} ({player_tag})")
            
            # Send error message to user immediately
            if send_success_message:
                if use_followup:
                    await interaction.followup.send(verification_message, ephemeral=True)
                else:
                    await interaction.response.send_message(verification_message, ephemeral=True)
            
            # Return failure status - do NOT continue to notification prompt
            return False, verification_message
    elif show_api_prompt and not is_verified:
        # SIMPLE mode doesn't gate roles on verification — assign/sync now instead of
        # making the user wait for a skip/verify click just to get their roles.
        await _assign_and_sync_roles_for_link()

        # Show API verification prompt for unverified players
        # Notification check will happen in the button callbacks (verify/skip)
        from qapbot.ui_registration import ApiVerificationPromptView
        from qapbot.i18n import t
        guild_id = interaction.guild.id if interaction.guild else None
        view = ApiVerificationPromptView(player_tag, player_name, player_selection_interaction=player_selection_interaction, guild_id=guild_id)
        prompt_text = t('playerregistration.api_prompt_full', guild_id=guild_id, link_msg=link_msg)
        
        if send_success_message:
            try:
                # Respect use_followup parameter - use response for modal submissions
                if use_followup:
                    await interaction.followup.send(prompt_text, view=view, ephemeral=True)
                else:
                    await interaction.response.send_message(prompt_text, view=view, ephemeral=True)
            except discord.errors.NotFound:
                # Webhook expired (interaction too old, >15 minutes)
                logging.warning(f"Webhook expired when trying to show API verification prompt for {player_name} ({player_tag}). User will need to try registration again.")
                # Try to inform user via response instead (if not already responded)
                from qapbot.i18n import t
                try:
                    await interaction.response.send_message(
                        t('playerregistration.webhook_expired', guild_id=interaction.guild.id if interaction.guild else None),
                        ephemeral=True
                    )
                except:
                    pass  # Interaction already responded, user saw original error
                return False, "Interaction webhook expired"
        
        # Return early - notification check will happen after user interacts with API prompt
        return True, link_msg
    
    # STEP 2.5: ROLE ASSIGNMENT + CoC/CLAN ROLE SYNC
    await _assign_and_sync_roles_for_link()

    # STEP 3: NOTIFICATION CHECK (only if we didn't show API prompt and skip not requested)
    # Combine registration message and verification message if both exist
    combined_message = link_msg
    if verification_message:
        combined_message = verification_message
    
    logging.debug(f"[NOTIFICATION-FLOW] skip_notification_prompt={skip_notification_prompt}, send_success_message={send_success_message}, use_followup={use_followup}")
    logging.debug(f"[NOTIFICATION-FLOW] combined_message={combined_message}")
    
    if not skip_notification_prompt:
        success_msg = combined_message if send_success_message else ""
        await check_and_prompt_war_notifications(
            user_id=account_tag,
            interaction=interaction,
            success_message=success_msg,
            use_followup=use_followup,
            player_selection_interaction=player_selection_interaction
        )
    else:
        # If skipping notification prompt but need to send success message
        if send_success_message and combined_message:
            logging.debug(f"[NOTIFICATION-FLOW] Skipping notification prompt, sending message directly")
            if use_followup:
                await interaction.followup.send(combined_message, ephemeral=True)
            else:
                await interaction.response.send_message(combined_message, ephemeral=True)

    return True, combined_message


# ========================================================================================
# Refactored Helper Functions for Reducing Code Duplication
# ========================================================================================

def _is_configured_admin(user: Union[discord.User, discord.Member], server_admin: str) -> bool:
    """
    Check whether a Discord user matches the configured bot admin (SERVER_ADMIN).

    Preferred configuration is the numeric Discord user ID (spoof-proof: IDs are
    immutable). A non-numeric value is treated as a legacy username match, which
    is deprecated because usernames can be released and re-registered by others.

    Args:
        user: Discord user or member from the interaction
        server_admin: Configured SERVER_ADMIN value (numeric ID preferred, username legacy)

    Returns:
        bool: True if the user is the configured admin, False otherwise
    """
    if not server_admin:
        return False
    if server_admin.isdigit():
        return str(getattr(user, "id", "")) == server_admin
    # Legacy fallback: username match (deprecated — configure the numeric user ID instead)
    return str(user) == server_admin

async def check_admin_permissions(interaction: discord.Interaction, server_admin: str) -> bool:
    """
    Check if user has administrator permissions or is the configured bot admin.
    
    Args:
        interaction: Discord interaction object
        server_admin: Configured bot admin — numeric Discord user ID (preferred) or username (legacy)
        
    Returns:
        bool: True if user has permissions, False otherwise
        
    Usage:
        if not await check_admin_permissions(interaction, SERVER_ADMIN):
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.followup.send(t('commands.errors.admin_required', guild_id=guild_id), ephemeral=True)
            return
    """
    # Check if user is a Member (has guild_permissions) or fallback to User check
    if isinstance(interaction.user, discord.Member):
        return interaction.user.guild_permissions.administrator or _is_configured_admin(interaction.user, server_admin)
    return _is_configured_admin(interaction.user, server_admin)

def check_bot_admin_only(interaction: discord.Interaction, server_admin: str) -> bool:
    """
    Check if user is the bot server admin (stricter than Discord server admin).
    
    Args:
        interaction: Discord interaction object
        server_admin: Configured bot admin — numeric Discord user ID (preferred) or username (legacy)
        
    Returns:
        bool: True if user is the bot admin, False otherwise
        
    Usage:
        if not check_bot_admin_only(interaction, SERVER_ADMIN):
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.followup.send(t('commands.errors.bot_admin_required', guild_id=guild_id), ephemeral=True)
            return
    """
    return _is_configured_admin(interaction.user, server_admin)

def get_clan_display_name(clan_tag: str) -> str:
    """
    Get formatted display name for a clan.
    
    Args:
        clan_tag: Normalized clan tag (e.g., "#ABC123")
        
    Returns:
        str: Formatted display name "ClanName (#ABC123)" or just tag if name unknown
        
    Example:
        >>> get_clan_display_name("#L2J0C0PY")
        "My Clan (#L2J0C0PY)"
    """
    clan_name = CACHE.get_clan_name(clan_tag, "Unknown Clan")
    return f"{clan_name} ({clan_tag})"

def get_family_display_name(family_tag: str) -> str:
    """
    Get formatted display name for a clan family.
    
    Args:
        family_tag: Normalized family tag (e.g., "#ABCD123456")
        
    Returns:
        str: Formatted display name "FamilyName (#ABCD123456)" or just tag if name unknown
        
    Example:
        >>> get_family_display_name("#ABCD123456")
        "My Family (#ABCD123456)"
    """
    if family_tag in CACHE.clan_families:
        family_name = CACHE.clan_families[family_tag].get("name", "Unknown Family")
        return f"{family_name} ({family_tag})"
    return family_tag

def get_clan_or_family_display_name(tag: str) -> str:
    """
    Get formatted display name for either a clan or family tag.
    
    Automatically detects whether tag is a clan or family and returns appropriate display name.
    
    Args:
        tag: Normalized clan or family tag
        
    Returns:
        str: Formatted display name with appropriate clan/family name
        
    Example:
        >>> get_clan_or_family_display_name("#L2J0C0PY")
        "My Clan (#L2J0C0PY)"
        >>> get_clan_or_family_display_name("#ABCD123456")
        "My Family (#ABCD123456)"
    """
    if tag in CACHE.clan_families:
        return get_family_display_name(tag)
    return get_clan_display_name(tag)

async def validate_and_add_clan_to_cache(clan_tag: str) -> Tuple[bool, str, Optional[str]]:
    """
    Validate and add a clan to the cache if it doesn't exist.
    
    Checks if the clan exists in clan_name_cache. If not, attempts to fetch from 
    Clash of Clans API and add to cache with proper initialization.
    
    Args:
        clan_tag: Normalized clan tag (e.g., "#ABC123")
        
    Returns:
        Tuple[bool, str, Optional[str]]: 
            - success: True if clan is valid and in cache
            - message: User-friendly status message
            - clan_name: Clan name if successful, None otherwise
            
    Example:
        >>> success, msg, name = await validate_and_add_clan_to_cache("#ABC123")
        >>> if success:
        ...     print(f"Clan {name} is now tracked")
        ... else:
        ...     print(f"Error: {msg}")
    """
    # Check if clan already exists in cache
    if clan_tag in CACHE.clan_name_cache:
        clan_name = CACHE.get_clan_name(clan_tag, "Unknown")
        from qapbot.i18n import t
        msg = t('clan_management_errors.already_tracked', clan_name=clan_name, clan_tag=clan_tag)
        return True, msg, clan_name
    
    # Attempt to fetch clan from CoC API (via cache)
    try:
        clan_obj = await CACHE.coc_clan_cache.get_clan(clan_tag)
        clan_name = clan_obj.name if hasattr(clan_obj, "name") else "Unknown Clan"
        
        # Add to clan_name_cache (always use dict format for Phase 3)
        existing = CACHE.clan_name_cache.get(clan_tag)
        if isinstance(existing, dict):
            existing['name'] = clan_name
        else:
            # New clan or old format - create dict structure
            CACHE.clan_name_cache[clan_tag] = {
                "name": clan_name,
                "has_active_subscriptions": False,
                "last_war_update": None,
                "warlog_is_public": None,
                "last_checked_via_api": None
            }
        await CACHE.persist_clan(clan_tag)
        
        # Initialize empty temp war stats and clan history for new clan
        CACHE.temp_war_stats[clan_tag] = {}
        CACHE.clan_history[clan_tag] = []
        
        logging.info(f"Successfully added new clan {clan_tag} ({clan_name}) to tracking")

        # Fire-and-forget CWL history backfill for the newly added clan.
        # The backfill function is idempotent (checks DB first) so safe to call here.
        try:
            from QBhelperfunctions import backfill_last_cwl_for_clan  # type: ignore[attr-defined]
            asyncio.ensure_future(backfill_last_cwl_for_clan(clan_tag))
            logging.info(f"[CWL-BACKFILL] Scheduled backfill for newly added clan {clan_tag}")
        except Exception as _bf_ex:
            logging.warning(f"[CWL-BACKFILL] Could not schedule backfill for {clan_tag}: {_bf_ex}")

        from qapbot.i18n import t
        msg = t('clan_management_errors.added_to_tracking', clan_name=clan_name, clan_tag=clan_tag)
        return True, msg, clan_name
        
    except Exception as api_error:
        from qapbot.i18n import t
        error_msg = t('clan_management_errors.clan_not_found', clan_tag=clan_tag)
        logging.warning(f"Failed to fetch clan info for {clan_tag}: {api_error}")
        return False, error_msg, None

async def resolve_clan_or_family_tag(identifier: str) -> Tuple[bool, Optional[str], bool]:
    """
    Resolve a clan identifier (tag or name) to a normalized tag.
    
    Checks both clan families and individual clans. Returns success status,
    resolved tag, and whether it's a family.
    
    Args:
        identifier: Raw clan/family tag or name input
        
    Returns:
        Tuple[bool, Optional[str], bool]: (success, resolved_tag, is_family)
        
    Examples:
        >>> await resolve_clan_or_family_tag("#ABC123")
        (True, "#ABC123", False)
        >>> await resolve_clan_or_family_tag("Family Name")
        (True, "#ABCD123456", True)
        >>> await resolve_clan_or_family_tag("Invalid")
        (False, None, False)
    """
    # Check if it's a family tag
    if identifier in CACHE.clan_families:
        return True, identifier, True
    
    # Try to resolve as clan tag/name
    count, resolved = _get_clan_tag(identifier)
    if count > 0 and resolved:
        return True, resolved, False
    
    return False, None, False

def get_user_player(user_entry: Dict[str, Any], player_tag: str) -> Optional[Dict[str, Any]]:
    """
    Find a player in a user's registered players list.
    
    Args:
        user_entry: User account entry from CACHE.user_accounts
        player_tag: Normalized player tag to search for
        
    Returns:
        Optional[dict]: Player entry dict if found, None otherwise
        
    Example:
        >>> user_entry = CACHE.user_accounts.get("123456789")
        >>> player = get_user_player(user_entry, "#PLAYER123")
        >>> if player:
        ...     print(player.get("player_name"))
    """
    if not user_entry:
        return None
    
    players = user_entry.get("players", [])
    if not isinstance(players, list):
        return None
    
    for p in players:
        if isinstance(p, dict):
            current_tag = p.get("player_tag", "")
            if current_tag == player_tag:
                return p
    
    return None


async def set_primary_account(user_id: str, player_tag: str) -> bool:
    """
    Set a player as the primary account for a user.
    Removes primary flag from all other players for this user.
    
    Args:
        user_id: Discord user ID
        player_tag: Normalized player tag to set as primary
        
    Returns:
        bool: True if successful, False if player not found or already primary
    """
    user_entry = CACHE.user_accounts.get(user_id)
    if not user_entry:
        return False
    
    players = user_entry.get("players", [])
    if not isinstance(players, list):
        return False
    
    # Find the target player
    target_player = None
    for player in players:
        if isinstance(player, dict):
            current_tag = player.get("player_tag", "")
            if current_tag == player_tag:
                target_player = player
                break
    
    if not target_player:
        return False
    
    # Check if already primary
    if target_player.get("is_primary", False):
        return False
    
    # Remove primary flag from all players
    for player in players:
        if isinstance(player, dict):
            player["is_primary"] = False
    
    # Set target as primary
    target_player["is_primary"] = True
    
    # Save changes (write-through)
    await CACHE.persist_user(user_id)
    
    return True


async def unlink_player(user_id: str, player_tag: str) -> bool:
    """
    Unlink a player from a user's account.
    Moves the player to UNASSIGNED pool to preserve metadata.
    
    Args:
        user_id: Discord user ID
        player_tag: Normalized player tag to unlink
        
    Returns:
        bool: True if successful, False if player not found
    """
    user_entry = CACHE.user_accounts.get(user_id)
    if not user_entry:
        return False
    
    players = user_entry.get("players", [])
    if not isinstance(players, list):
        return False
    
    # Find the player
    player_to_remove = None
    for player in players:
        if isinstance(player, dict):
            current_tag = player.get("player_tag", "")
            if current_tag == player_tag:
                player_to_remove = player
                break
    
    if not player_to_remove:
        return False
    
    # Move to UNASSIGNED pool
    unassigned_entry = CACHE.user_accounts.setdefault("UNASSIGNED", {"display_name": "UNASSIGNED", "players": []})
    
    # Check if player is already in UNASSIGNED pool (edge case)
    already_unassigned = any(
        isinstance(p, dict) and p.get("player_tag", "") == player_tag
        for p in unassigned_entry.get("players", [])
    )
    
    if not already_unassigned:
        # Remove primary flag before moving to UNASSIGNED
        player_to_remove["is_primary"] = False
        unassigned_entry["players"].append(player_to_remove)
    
    # Remove from user's account
    user_entry["players"] = [
        p for p in players
        if not (isinstance(p, dict) and p.get("player_tag", "") == player_tag)
    ]
    
    # Save changes (write-through: persist both affected users)
    await CACHE.persist_user(user_id)
    await CACHE.persist_user("UNASSIGNED")
    
    return True


def get_verified_player_owner(player_tag: str, requesting_user_id: str) -> Optional[str]:
    """
    Check if a player is already verified by another Discord user.
    
    Args:
        player_tag: Normalized player tag to check
        requesting_user_id: Discord user ID of the user making the request (to exclude from check)
        
    Returns:
        Optional[str]: Display name of the user who has this player verified, or None if not verified by anyone else
        
    Example:
        >>> owner = get_verified_player_owner("#PLAYER123", "123456789")
        >>> if owner:
        ...     print(f"Player is already verified by {owner}")
    """
    for user_id, user_entry in CACHE.user_accounts.items():
        # Skip UNASSIGNED pool and the requesting user
        if user_id == "UNASSIGNED" or user_id == requesting_user_id:
            continue
        
        if not user_entry:
            continue
        
        players = user_entry.get("players", [])
        if not isinstance(players, list):
            continue
        
        for player in players:
            if isinstance(player, dict):
                current_tag = player.get("player_tag", "")
                if current_tag == player_tag:
                    # Found player in another user's account - check if verified
                    if player.get("verified", False):
                        return user_entry.get("display_name", "Unknown User")
    
    return None

def get_any_player_owner(player_tag: str, requesting_user_id: str) -> Optional[Tuple[str, str, bool]]:
    """
    Check if a player is already linked to another Discord user (verified or unverified).
    
    This is used for CoC API token override - any existing link requires API token proof.
    
    Args:
        player_tag: Normalized player tag to check
        requesting_user_id: Discord user ID of the user making the request (to exclude from check)
        
    Returns:
        Optional[Tuple[str, str, bool]]: (user_id, display_name, is_verified) or None if not linked elsewhere
        
    Example:
        >>> result = get_any_player_owner("#PLAYER123", "123456789")
        >>> if result:
        ...     user_id, display_name, is_verified = result
        ...     print(f"Player is linked to {display_name} (verified={is_verified})")
    """
    for user_id, user_entry in CACHE.user_accounts.items():
        # Skip UNASSIGNED pool and the requesting user
        if user_id == "UNASSIGNED" or user_id == requesting_user_id:
            continue
        
        if not user_entry:
            continue
        
        players = user_entry.get("players", [])
        if not isinstance(players, list):
            continue
        
        for player in players:
            if isinstance(player, dict):
                current_tag = player.get("player_tag", "")
                if current_tag == player_tag:
                    # Found player in another user's account
                    display_name = user_entry.get("display_name", "Unknown User")
                    is_verified = player.get("verified", False)
                    return (user_id, display_name, is_verified)
    
    return None

async def validate_api_token_for_player(player_tag: str, api_token: str) -> Tuple[bool, str]:
    """
    [DEPRECATED] Use CACHE.verify_api_token() instead.
    
    Validate that the provided CoC API token belongs to the specified player.
    
    This validates ownership by attempting to fetch the player using the API token.
    The CoC API returns player data only if the token belongs to that player.
    
    Args:
        player_tag: Normalized player tag
        api_token: CoC API token to validate
        
    Returns:
        Tuple[bool, str]: (is_valid, message) - True if token is valid for this player
        
    Example:
        >>> valid, msg = await validate_api_token_for_player("#PLAYER123", "token123", client)
        >>> if valid:
        ...     print("Token is valid - user owns this player")
    """
    from qapbot.cache_manager import CACHE
    return await CACHE.verify_api_token(player_tag, api_token)

async def restore_player_from_unassigned(
    user_entry: Dict[str, Any],
    player_tag: str,
    display_name: str
) -> Tuple[bool, Optional[Dict[str, Any]], str]:
    """
    Attempt to restore a player from the UNASSIGNED pool.
    
    If player exists in UNASSIGNED pool, moves it to the user's account
    and removes it from UNASSIGNED. Automatically saves to cache.
    
    Args:
        user_entry: User account entry to restore player to
        player_tag: Normalized player tag to restore
        display_name: Display name for the user account
        
    Returns:
        Tuple[bool, Optional[dict], str]: (restored, player_data, message)
        
    Example:
        >>> user_entry = CACHE.user_accounts.get("123456789")
        >>> restored, player, msg = await restore_player_from_unassigned(user_entry, "#PLAYER123", "User")
        >>> if restored:
        ...     print(f"Restored: {msg}")
    """
    unassigned_entry = CACHE.user_accounts.get("UNASSIGNED", {"display_name": "UNASSIGNED", "players": []})
    if not unassigned_entry:
        return False, None, "UNASSIGNED pool is corrupted"
    
    unassigned_players = unassigned_entry.get("players", [])
    if not isinstance(unassigned_players, list):
        return False, None, "UNASSIGNED players list is corrupted"
    
    # Find player in UNASSIGNED pool
    unassigned_player = None
    for p in unassigned_players:
        if isinstance(p, dict):
            current_tag = p.get("player_tag", "")
            if current_tag == player_tag:
                unassigned_player = p
                break
    
    if not unassigned_player:
        return False, None, "Player not found in UNASSIGNED pool"
    
    # Always reset verified status to False when restoring from UNASSIGNED
    unassigned_player["verified"] = False
    
    # Restore player to user account
    user_entry["players"].append(unassigned_player)
    
    # Remove from UNASSIGNED pool
    unassigned_entry["players"] = [
        p for p in unassigned_players
        if not (isinstance(p, dict) and p.get("player_tag", "") == player_tag)
    ]
    
    # Clean up UNASSIGNED entry if empty
    if not unassigned_entry["players"]:
        CACHE.user_accounts.pop("UNASSIGNED", None)
        await CACHE.delete_user_account("UNASSIGNED")
    else:
        CACHE.user_accounts["UNASSIGNED"] = unassigned_entry
    
    user_entry["display_name"] = display_name
    
    # Save changes (write-through: persist affected users)
    # Find the user_id that owns user_entry
    for uid, udata in CACHE.user_accounts.items():
        if udata is user_entry:
            await CACHE.persist_user(uid)
            break
    # Persist UNASSIGNED changes
    if "UNASSIGNED" in CACHE.user_accounts:
        await CACHE.persist_user("UNASSIGNED")
    
    from qapbot.i18n import t
    player_name = unassigned_player.get("player_name", "Unknown")
    verification_status = "❌ unverified"  # Always unverified after restoration
    message = t('playerregistration.restored_to_account', player_name=player_name, player_tag=player_tag, status=verification_status)
    
    return True, unassigned_player, message

async def _verify_player_with_token(
    player_tag: str,
    player_name: str,
    api_token: str,
    guild_id: Optional[int] = None
) -> Tuple[bool, str]:
    """
    Core verification logic: verify player ownership using API token.
    
    Args:
        player_tag: Normalized player tag
        player_name: Player display name
        api_token: Supercell API token
        guild_id: Discord guild ID for translation context
        
    Returns:
        (success: bool, message: str)
    """
    from qapbot.cache_manager import CACHE
    from qapbot.i18n import t
    
    # Use centralized API token verification
    success, _ = await CACHE.verify_api_token(player_tag, api_token)
    
    if success:
        message = t('playerregistration.verification_success', guild_id=guild_id, player_name=player_name, player_tag=player_tag)
        return True, message
    else:
        message = t('playerregistration.verification_failed', guild_id=guild_id, player_name=player_name, player_tag=player_tag)
        return False, message

async def verify_and_update_player(
    player_entry: Dict[str, Any],
    player_tag: str,
    api_token: str,
    guild_id: Optional[int] = None
) -> Tuple[bool, str]:
    """
    Verify player ownership using CoC API and update verification status.
    
    Attempts to verify player using the provided API token. Updates player_entry
    in-place if verification succeeds. Does NOT save to cache (caller must save).
    
    Args:
        player_entry: Player entry dict to update with verification status
        player_tag: Normalized player tag to verify
        api_token: CoC API token for verification
        guild_id: Discord guild ID for translation context
        
    Returns:
        Tuple[bool, str]: (success, user_message)
        
    Example:
        >>> player_entry = {"player_id": "#ABC", "player_name": "Player", "verified": False}
        >>> success, msg = await verify_and_update_player(player_entry, "#ABC", "token", guild_id=123)
        >>> if success:
        ...     await CACHE.persist_user(user_id)  # Caller must persist
    """
    player_name = player_entry.get("player_name", "Unknown")
    success, message = await _verify_player_with_token(player_tag, player_name, api_token, guild_id=guild_id)
    
    if success:
        player_entry["verified"] = True
    
    return success, message

def is_already_subscribed(
    subscriptions: List[Dict[str, Any]],
    clan_tag: str,
    mode: str,
    year_scope: Optional[str]
) -> bool:
    """
    Check if a clan/mode/year combination is already subscribed.
    
    Args:
        subscriptions: List of subscription dicts for a channel
        clan_tag: Normalized clan or family tag
        mode: Leaderboard mode (e.g., "attack", "defense")
        year_scope: Year scope ("current" for YTD, None for monthly)
        
    Returns:
        bool: True if already subscribed, False otherwise
        
    Example:
        >>> subs = CACHE.get_channel_subscriptions(channel_id)
        >>> if is_already_subscribed(subs, "#ABC123", "attack", None):
        ...     print("Already subscribed!")
    """
    return any(
        s.get('clan_tag') == clan_tag
        and s.get('subscription_type') == mode
        and s.get('year') == year_scope
        for s in subscriptions
        if s
    )

async def cleanup_stale_messages_for_channel(channel: discord.TextChannel) -> int:
    """
    Clean up stale leaderboard message IDs from cache for a specific channel.
    
    Removes cache entries where all tracked message IDs no longer exist in Discord.
    Automatically saves cache if any entries are removed.
    
    Args:
        channel: Discord TextChannel to clean up
        
    Returns:
        int: Number of stale cache entries removed
        
    Example:
        >>> cleaned_count = await cleanup_stale_messages_for_channel(interaction.channel)
        >>> print(f"Cleaned {cleaned_count} stale entries")
    """
    channel_id = str(channel.id)

    # Collect all unique message IDs across every cache entry for this channel
    entries_to_check: Dict[str, List[str]] = {}
    all_msg_ids: Set[str] = set()
    for k, v in list(CACHE.leaderboard_messages.items()):
        if v.get('channel_id') != channel_id:
            continue
        msg_ids_str = v.get('message_ids', '')
        if not msg_ids_str:
            continue
        ids = [mid.strip() for mid in msg_ids_str.split(',') if mid.strip()]
        if ids:
            entries_to_check[k] = ids
            all_msg_ids.update(ids)

    if not all_msg_ids:
        return 0

    # Fetch all tracked message IDs in parallel instead of one-by-one
    async def _check_exists(mid: str) -> Tuple[str, bool]:
        try:
            await channel.fetch_message(int(mid))
            return mid, True
        except Exception:
            return mid, False

    fetch_results = await asyncio.gather(*[_check_exists(mid) for mid in all_msg_ids])
    exists_map: Dict[str, bool] = dict(fetch_results)

    # An entry is stale only when ALL its tracked messages are missing
    keys_to_delete = [
        k for k, ids in entries_to_check.items()
        if all(not exists_map.get(mid, True) for mid in ids)
    ]

    # Remove stale entries
    for k in keys_to_delete:
        await CACHE.delete_leaderboard_message(k)

    return len(keys_to_delete)


# ============================================================================
# Unified Player List Generation
# ============================================================================

def get_guild_subscribed_clans(guild_id: int) -> List[str]:
    """
    Get all clan tags that are subscribed (directly or via families) in a guild.
    
    This function is the single source of truth for determining which clans are
    active/relevant in a specific Discord guild. Used by welcome message,
    Used by welcome message, clan management, and admin linking flows to ensure consistent clan filtering.
    
    When a clan is subscribed, ALL clans in its family are also included to ensure
    players from any clan in the family can be registered via welcome message.
    
    Args:
        guild_id: Discord guild ID to get subscribed clans from
        
    Returns:
        List of clan tags (sorted) that have subscriptions in the guild.
        Includes clans from clan families.
        Excludes playerregistration pseudo-subscriptions.
        
    Examples:
        >>> clans = get_guild_subscribed_clans(123456789)
        >>> print(clans)
        ['#2C9UR9GJY', '#L2J0C0PY', '#28GRPC00C']
    """
    guild_id_str = str(guild_id)
    clans_in_guild = set()
    families_to_expand = set()
    
    # Traverse all channels in this guild's subscriptions
    guild_subs = CACHE.subscriptions.get(guild_id_str, {})
    for _, subs in guild_subs.items():  # type: ignore[misc]
        for s in subs:
            if not s:
                continue
            if s.get('subscription_type') == 'playerregistration':
                continue  # Do not treat the welcome-message tracker as a clan subscription
            tag = s.get('clan_tag')
            if not tag:
                continue
            
            # Check if this is a family ID (direct family subscription)
            if tag in CACHE.clan_families:
                families_to_expand.add(tag)
                clans_in_guild.update(CACHE.clan_families[tag].get('clans', []))
            else:
                # Individual clan subscription - add it and check if it belongs to a family
                clans_in_guild.add(tag)
                
                # Check if this clan belongs to any family
                for family_id, family_data in CACHE.clan_families.items():
                    if tag in family_data.get('clans', []):
                        # This clan is part of a family - include all clans from that family
                        families_to_expand.add(family_id)
                        clans_in_guild.update(family_data.get('clans', []))
    
    return sorted(clans_in_guild)


def get_guild_clans_including_member_config(guild_id: int) -> List[str]:
    """
    Get all clans that should be available for selection in management views.
    
    This includes:
    1. All clans from subscriptions (directly subscribed or via families)
    2. All clans from member_families (configured to grant member role)
    3. All clans from member_clans (individually configured to grant member role)
    
    This ensures that clans in member_families or member_clans always appear in selectors,
    even if they don't have active subscriptions. This is critical for "Manage Registrations"
    and "Manage War Notifications" views where users should be able to interact with all
    clans they've configured.
    
    Args:
        guild_id: Discord guild ID
        
    Returns:
        List of clan tags (sorted) available for selection
    """
    clans_in_guild = set()
    
    # 1. Add all subscribed clans (both direct and via families)
    clans_in_guild.update(get_guild_subscribed_clans(guild_id))
    
    # 2. Add clans from member_families configuration
    guild_id_str = str(guild_id)
    config = CACHE.server_config.get(guild_id_str, {})
    
    member_families = config.get("member_families", [])
    for family_id in member_families:
        if family_id in CACHE.clan_families:
            clans_in_guild.update(CACHE.clan_families[family_id].get("clans", []))
    
    # 3. Add clans from member_clans configuration (individual clans)
    member_clans = config.get("member_clans", [])
    clans_in_guild.update(member_clans)
    
    return sorted(clans_in_guild)


async def get_player_list(
    clan_tags: List[str],
    *,
    exclude_registered_players: bool = False,
    use_live_api: bool = False,
    sort_by: str = "name",
    max_players: Optional[int] = None
) -> List[Dict[str, Any]]:
    """
    Build a unified player list from multiple clans with flexible filtering.
    
    This function serves as the single source of truth for player list generation,
    replacing fragmented logic across the codebase.
    
    Args:
        clan_tags: List of clan tags to aggregate players from (e.g., ["#ABC123", "#DEF456"])
        exclude_registered_players: If True, exclude all registered players globally
                                    (both verified and unverified)
        use_live_api: If True, fetch current clan member list from CoC API and merge
                     with cached data (updates names, adds new players)
        sort_by: Sort order - "name" (default), "tag", or "activity" (recent war attacks)
        max_players: Optional limit on number of players to return
        
    Returns:
        List of player dicts: [{"tag": "#PLAYER", "name": "PlayerName", "activity": 15}, ...]
        Sorted according to sort_by parameter
        
    Data Sources:
        - Cached: temp war stats (priority) + war history database (fallback)
        - Live API: current clan member list (if use_live_api=True)
        
    Examples:
        # Welcome message: single clan, exclude registered
        >>> players = await get_player_list(["#ABC123"], exclude_registered_players=True)
        
        # Welcome message and admin flows: all clans, exclude registered
        >>> all_clans = list(CACHE.clan_name_cache.keys())
        >>> players = await get_player_list(all_clans, exclude_registered_players=True)
        
        # Admin operations: all clans, no filtering
        >>> players = await get_player_list(all_clans)
        
        # Live API mode: get current clan members + cached data
        >>> players = await get_player_list(["#ABC123"], use_live_api=True)
        
        # Sort by recent activity
        >>> players = await get_player_list(["#ABC123"], sort_by="activity")
    """
    latest_by_tag: Dict[str, str] = {}
    current_members: Set[str] = set()  # Track players confirmed by API as current members
    
    # STEP 1: Always start with cached data (temp stats + history)
    for clan_tag in clan_tags:
        _gather_players_from_cache(clan_tag, latest_by_tag)
    
    # STEP 2: If use_live_api, merge API data (adds new players, updates names)
    th_levels: Dict[str, int] = {}  # Store TH levels from API
    if use_live_api:
        for clan_tag in clan_tags:
            try:
                clan_obj = await CACHE.coc_clan_cache.get_clan(clan_tag)
                for member in clan_obj.members:
                    current_members.add(member.tag)  # Mark as current member
                    th_levels[member.tag] = member.town_hall  # Store TH level
                    if member.tag not in latest_by_tag:
                        # New player not in cache - add it
                        latest_by_tag[member.tag] = member.name
                    else:
                        # Player exists in cache - update name if changed
                        if latest_by_tag[member.tag] != member.name:
                            latest_by_tag[member.tag] = member.name
            except Exception as e:
                logging.warning(f"Failed to fetch clan {clan_tag} from API: {e}")
                # Cache data already gathered, continue with next clan
    
    # STEP 3: Apply filtering
    if exclude_registered_players:
        registered_ids = _get_registered_player_ids()
        latest_by_tag = {tag: name for tag, name in latest_by_tag.items() 
                        if tag not in registered_ids}
    
    # STEP 4: Build result with activity scores and current member flag
    players = []
    for tag, name in latest_by_tag.items():
        activity_score = _calculate_activity_score(tag, clan_tags)
        player_dict = {
            "tag": tag,
            "name": name,
            "activity": activity_score,
            "is_current_member": tag in current_members  # Flag for current clan members
        }
        # Add TH level if available from API
        if tag in th_levels:
            player_dict["th_level"] = th_levels[tag]
        players.append(player_dict)
    
    # STEP 5: Apply sorting
    if sort_by == "activity":
        players.sort(key=lambda x: x["activity"], reverse=True)  # type: ignore[misc]
    elif sort_by == "tag":
        players.sort(key=lambda x: x["tag"] or "")  # type: ignore[misc]
    else:  # Default: sort by name
        players.sort(key=lambda x: (x["name"].lower() if x.get("name") else "zzz", x.get("tag") or ""))  # type: ignore[misc]
    
    # STEP 6: Apply max limit if specified
    if max_players is not None:
        players = players[:max_players]
    
    return players


def _gather_players_from_cache(clan_tag: str, result_dict: Dict[str, str]) -> None:
    """
    Helper: aggregate players from temp war stats and history for one clan.
    
    Args:
        clan_tag: Clan tag to gather players from
        result_dict: Dictionary to populate with {player_tag: player_name}
                    Players already in dict are NOT overwritten (priority preserved)
    """
    # Temp war stats (priority - most recent data)
    temp_stats = CACHE.get_temp_war_stats(clan_tag)
    for pid, pdata in temp_stats.items():
        if not pid:
            continue
        name = (pdata or {}).get("Player", "") if isinstance(pdata, dict) else ""
        if name:
            result_dict[pid] = name
    
    # History (fallback for players not in recent wars)
    history_rows = CACHE.get_clan_history(clan_tag)
    for row in history_rows:
        if not isinstance(row, dict):
            continue
        pid = row.get("PlayerID", "")
        if not pid or pid in result_dict:
            continue
        name = row.get("Player", "") or "Unknown"
        result_dict[pid] = name


def _get_registered_player_ids() -> Set[str]:
    """
    Helper: get all registered player IDs across all users (verified and unverified).
    
    Returns:
        Set of player tags that are registered to any Discord user
    """
    registered_ids: Set[str] = set()
    for acct_tag, entry in (CACHE.user_accounts or {}).items():
        if acct_tag == "UNASSIGNED":
            continue
        if not entry:
            continue
        for p in entry.get("players", []):
            if p:  # type: ignore[misc]
                pid = p.get("player_tag", "")  # type: ignore[misc, union-attr]
                if pid:
                    registered_ids.add(pid)
    return registered_ids


def _calculate_activity_score(player_tag: str, clan_tags: List[str]) -> int:
    """
    Calculate player activity score from attack count in last 2 months.
    
    This function is designed to be extended in the future with additional
    activity metrics (donations, war stars, CWL performance, etc.).
    
    Args:
        player_tag: Player tag to calculate activity for
        clan_tags: List of clan tags to check activity across
        
    Returns:
        Activity score (currently: number of attacks in last 2 months)
        
    Future Extensions:
        - Add donation counts
        - Add war stars earned
        - Add CWL performance metrics
        - Add weighted scoring system
    """
    from datetime import datetime, timedelta
    
    now = datetime.now()
    two_months_ago = now - timedelta(days=60)
    attack_count = 0
    history_attacks = 0
    temp_attacks = 0
    
    # Count attacks from war history database
    for clan_tag in clan_tags:
        history_rows = CACHE.get_clan_history(clan_tag)
        
        for row in history_rows:
            if not isinstance(row, dict):
                continue
            
            row_player_id = row.get("PlayerID")
            if row_player_id == player_tag:
                # Parse war date from history (format: "YYYY-MM-DDTHH:MM:SS" ISO format)
                war_date_str = row.get("Date", "")
                try:
                    # Handle ISO format with time: 2025-07-26T21:47:00
                    war_date = datetime.fromisoformat(war_date_str)
                    if war_date >= two_months_ago:
                        attacks = int(row.get("Attacks", 0) or 0)
                        history_attacks += attacks
                except (ValueError, TypeError):
                    continue
    
    # Count attacks from temp war stats (active/recent wars)
    for clan_tag in clan_tags:
        temp_stats = CACHE.get_temp_war_stats(clan_tag)
        player_data = temp_stats.get(player_tag)
        if isinstance(player_data, dict):
            # Temp stats represent the most recent war(s), assumed to be within 2 months
            attacks = int(player_data.get("Attacks", 0) or 0)
            temp_attacks += attacks
    
    attack_count = history_attacks + temp_attacks
    
    return attack_count


# ============================================================================
# Player Registration Helper Functions
# ============================================================================

async def check_and_prompt_war_notifications(
    user_id: str,
    interaction: discord.Interaction,
    success_message: str,
    *,
    use_followup: bool = False,
    player_selection_interaction: Optional[discord.Interaction] = None
) -> None:
    """
    Check if user has war notifications enabled and prompt them if not.
    
    This function separates notification prompting from registration/verification logic,
    making the code more modular and reducing duplication.
    
    Args:
        user_id: Discord user ID to check notification status for
        interaction: Current interaction to respond to
        success_message: Success message to show (e.g., "✅✅ Verified player...")
        use_followup: If True, use followup.send instead of response.send_message
        player_selection_interaction: Optional interaction to delete player selection message
    
    Behavior:
        - If user has war notifications enabled: sends success_message only
        - If user doesn't have notifications: appends notification prompt with Activate/Skip buttons
    
    Example:
        await check_and_prompt_war_notifications(
            user_id="123456789",
            interaction=interaction,
            success_message="✅✅ Verified player John (#ABC123)",
            use_followup=False
        )
    """
    from qapbot.ui_notifications import WarNotificationPromptView
    from qapbot.i18n import t
    import logging
    
    # Get user data and check notification status
    user_data = CACHE.user_accounts.get(user_id, {})
    notification_settings = user_data.get("notification_settings", {})
    war_reminders_enabled = notification_settings.get("war_reminders", False)
    
    logging.info(f"[NOTIFICATION-CHECK] User {user_id}: notification_settings={notification_settings}, war_reminders_enabled={war_reminders_enabled}")
    
    # Delete player selection message if it exists
    if player_selection_interaction:
        try:
            await player_selection_interaction.delete_original_response()
            logging.debug("Successfully deleted player selection message before response")
        except Exception as e:
            logging.debug(f"Could not delete player selection message: {e}")
    
    guild_id = interaction.guild.id if interaction.guild else None

    if war_reminders_enabled:
        # User already has notifications - just show success message
        logging.info(f"[NOTIFICATION-CHECK] User {user_id} already has notifications enabled - showing simple success message")
        if success_message:
            if use_followup:
                await interaction.followup.send(success_message, ephemeral=True)
            else:
                await interaction.response.send_message(success_message, ephemeral=True)
    else:
        # User doesn't have notifications - show prompt
        logging.info(f"[NOTIFICATION-CHECK] User {user_id} has no notifications - showing prompt with buttons")
        view = WarNotificationPromptView(guild_id=guild_id)

        prompt_text = t('playerregistration.war_notifications_prompt', user_id=user_id, guild_id=guild_id)
        message_text = f"{success_message}\n\n{prompt_text}" if success_message else prompt_text
        
        if use_followup:
            view.message = await interaction.followup.send(message_text, view=view, ephemeral=True)
        else:
            await interaction.response.send_message(message_text, view=view, ephemeral=True)
            view.message = await interaction.original_response()


async def process_player_registration(
    interaction: discord.Interaction,
    player_tag: str,
    player_name: Optional[str] = None,
    api_token: Optional[str] = None,
    *,
    target_user_id: Optional[int] = None,
    use_followup: bool = False,
    player_selection_interaction: Optional[discord.Interaction] = None,
    show_api_prompt: bool = True,
    admin_override: bool = False
) -> None:
    """
    Universal entry point for player registration with automatic CoC API fallback.
    
    This function provides a complete registration flow with built-in player lookup:
    1. Validates and normalizes player tag
    2. Fetches player data from CoC API if name not provided (handles unknown clans)
    3. Cleans up UI messages
    4. Delegates to complete_account_linking_flow() for registration → verification → notifications

    Used by ALL registration flows:
    - Welcome message modal (PlayerSubstringModal) - self-registration
    - Clan management - admin linking to another user (uses target_user_id)

    Args:
        interaction: Discord interaction object
        player_tag: Player tag (will be normalized)
        player_name: Player name (optional - will fetch from CoC API if not provided)
        api_token: CoC API token for verification (optional)
        target_user_id: Discord user ID to link player to (defaults to interaction.user.id)
        use_followup: If True, use interaction.followup; if False, use interaction.response
        player_selection_interaction: Optional interaction to delete player selection message
        admin_override: If True, allows bot admin to override verified player security check (default: False)
        show_api_prompt: If True, show API verification prompt (False for admin linking)

    Example:
        >>> # Self-registration (welcome message)
        >>> await process_player_registration(
        ...     interaction, "#ABC123", use_followup=False
        ... )
        >>> 
        >>> # Admin linking to another user (clan management)
        >>> await process_player_registration(
        ...     interaction, "#ABC123", target_user_id=123456789,
        ...     show_api_prompt=False, use_followup=True
        ... )
    """
    # Determine target user (default to interaction user for self-registration)
    final_target_user_id = target_user_id if target_user_id is not None else interaction.user.id
    
    # Validate and normalize player tag
    normalized_tag = normalize_clan_tag(player_tag)
    if not normalized_tag:
        from qapbot.i18n import t
        guild_id = interaction.guild.id if interaction.guild else None
        msg = t('playerregistration.invalid_player_tag_format', guild_id=guild_id)
        if player_selection_interaction:
            try:
                await player_selection_interaction.delete_original_response()
            except Exception:
                pass
            await interaction.followup.send(msg, ephemeral=True)
        elif use_followup:
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return
    
    # Fetch player data from CoC API if name not provided or unknown
    # This allows linking players from unknown clans (edge case)
    resolved_name = player_name
    if not resolved_name or resolved_name == "Unknown":
        try:
            logging.debug(f"process_player_registration: Fetching player data from CoC API for {normalized_tag}")
            player_obj = await CACHE.get_player(normalized_tag)
            if not player_obj:
                from qapbot.i18n import t
                guild_id = interaction.guild.id if interaction.guild else None
                logging.info(f"[USER ERROR] process_player_registration: Player {normalized_tag} not found in CoC API")
                msg = t('playerregistration.coc_api_lookup_failed', guild_id=guild_id, player_tag=normalized_tag)
                if player_selection_interaction:
                    try:
                        await player_selection_interaction.delete_original_response()
                        logging.debug("Deleted player selection message after CoC API error")
                    except Exception:
                        pass
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
                return
            
            resolved_name = player_obj.name if hasattr(player_obj, "name") else "Unknown"
            logging.info(f"process_player_registration: Successfully resolved player name: {resolved_name} ({normalized_tag})")
        except Exception as api_error:
            from qapbot.i18n import t
            guild_id = interaction.guild.id if interaction.guild else None
            logging.info(f"[USER ERROR] process_player_registration: CoC API lookup failed for {normalized_tag}: {api_error}")
            msg = t('playerregistration.coc_api_lookup_failed', guild_id=guild_id, player_tag=normalized_tag)
            
            # Delete player selection message if it exists
            if player_selection_interaction:
                try:
                    await player_selection_interaction.delete_original_response()
                    logging.debug("Deleted player selection message after CoC API error")
                except Exception:
                    pass
            
            # Send error message - check if response already used (e.g., from modal defer)
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return
    
    # Delete player selection message if it exists (before showing any prompts)
    if player_selection_interaction:
        try:
            await player_selection_interaction.delete_original_response()
            logging.debug("Successfully deleted player selection message before registration")
        except discord.NotFound:
            logging.debug("Player selection message already deleted")
        except Exception as e:
            logging.debug(f"Could not delete player selection message: {e}")
    
    # Use unified linking flow for registration → verification → notifications
    _, _ = await complete_account_linking_flow(  # type: ignore[misc]
        interaction=interaction,
        target_user_id=final_target_user_id,
        player_tag=normalized_tag,
        api_token=api_token if api_token else None,
        show_api_prompt=show_api_prompt if not api_token else False,
        send_success_message=True,
        use_followup=use_followup,
        player_selection_interaction=player_selection_interaction,
        admin_override=admin_override
    )
    
    # If complete_account_linking_flow() already sent error message, don't send it again
    # (verification failures and other errors are already sent by complete_account_linking_flow)
    # No need to send duplicate error messages


# ============================================================================
# Autocomplete Factory Functions
# ============================================================================

def create_clan_autocomplete(filter_subscribed: bool = False, channel_specific: bool = False):
    """
    Factory function to create clan tag autocomplete handlers with flexible filtering.

    Args:
        filter_subscribed: If True, only show subscribed clans
        channel_specific: If True, filter to current channel subscriptions

    Returns:
        Async autocomplete function for app_commands

    Example:
        >>> @app_commands.autocomplete(clantag=create_clan_autocomplete(filter_subscribed=True))
        >>> async def subscribe(interaction, clantag: str): ...
    """
    async def clan_autocomplete(interaction: discord.Interaction, current: str) -> List[discord.app_commands.Choice[str]]:
        """Autocomplete for clan tags with configurable filtering."""
        current_lower = current.lower() if current else ""
        choices = []
        
        if filter_subscribed and channel_specific:
            # Only show clans subscribed in current channel
            channel_id = str(interaction.channel.id) if interaction.channel else None
            if channel_id:
                subs = CACHE.get_channel_subscriptions(channel_id)
                subscribed_tags = {s.get('clan_tag') for s in subs if s and s.get('clan_tag') and s.get('clan_tag') != 'PLAYERREGISTRATION'}
                
                for tag in subscribed_tags:
                    if tag in CACHE.clan_families:
                        continue  # Skip families
                    if not tag:
                        continue
                    name = CACHE.get_clan_name(tag, "Unknown")
                    if tag and name:  # Ensure both are not None
                        label = f"{name} ({tag})"
                        if not current_lower or current_lower in label.lower():
                            choices.append(discord.app_commands.Choice(name=label[:100], value=tag))
        elif filter_subscribed:
            # Show all subscribed clans across all channels
            all_subscribed = set()
            for guild_data in CACHE.subscriptions.values():
                for channel_subs in guild_data.values():
                    for s in channel_subs:
                        if s:
                            tag = s.get('clan_tag')
                            if tag and tag != 'PLAYERREGISTRATION' and tag not in CACHE.clan_families:
                                all_subscribed.add(tag)
            
            for tag in sorted(all_subscribed):
                name = CACHE.get_clan_name(tag, "Unknown")
                label = f"{name} ({tag})"
                if not current_lower or current_lower in label.lower():
                    choices.append(discord.app_commands.Choice(name=label[:100], value=tag))
        else:
            # Show all known clans (no filtering)
            for tag, clan_data in sorted(CACHE.clan_name_cache.items(), key=lambda x: x[1]["name"].lower()):
                if tag in CACHE.clan_families:
                    continue  # Skip families
                label = f"{clan_data['name']} ({tag})"
                if not current_lower or current_lower in label.lower():
                    choices.append(discord.app_commands.Choice(name=label[:100], value=tag))
        
        return choices[:25]
    
    return clan_autocomplete


def create_family_autocomplete():
    """
    Factory function to create family tag autocomplete handler.

    Returns:
        Async autocomplete function for app_commands

    Example:
        >>> @app_commands.autocomplete(family_tag=create_family_autocomplete())
        >>> async def clanfamily(interaction, family_tag: str): ...
    """
    async def family_autocomplete(interaction: discord.Interaction, current: str) -> List[discord.app_commands.Choice[str]]:
        """Autocomplete for family tags."""
        current_lower = current.lower() if current else ""
        choices = []
        
        for fam_tag, fam_data in sorted(CACHE.clan_families.items(), key=lambda x: x[1].get('name', '').lower()):
            if not fam_data:
                continue
            name = fam_data.get('name', 'Unknown Family')
            label = f"{name} ({fam_tag})"
            if not current_lower or current_lower in label.lower():
                choices.append(discord.app_commands.Choice(name=label[:100], value=fam_tag))
        
        return choices[:25]
    
    return family_autocomplete


def get_clan_family_autocomplete_choices_factory(mode: str = "all"):
    """
    Factory function to create clan/family autocomplete handler with mode filtering.
    
    Args:
        mode: Autocomplete mode - "all", "clans_only", or "families_only"
        
    Returns:
        Async autocomplete function for app_commands
        
    Example:
        >>> @app_commands.autocomplete(clan=get_clan_family_autocomplete_choices_factory(mode="clans_only"))
        >>> async def clan_management(interaction, clan: str): ...
    """
    async def autocomplete_handler(interaction: discord.Interaction, current: str) -> List[discord.app_commands.Choice[str]]:
        """Autocomplete for clans or families based on mode."""
        current_lower = current.lower() if current else ""
        choices = []
        
        # Add clans if mode allows
        if mode in ("all", "clans_only"):
            for tag, clan_data in sorted(CACHE.clan_name_cache.items(), key=lambda x: x[1]["name"].lower()):
                if tag in CACHE.clan_families:
                    continue  # Skip families (they have different format)
                label = f"{clan_data['name']} ({tag})"
                if not current_lower or current_lower in label.lower():
                    choices.append(discord.app_commands.Choice(name=label[:100], value=tag))
                if len(choices) >= 25:
                    break
        
        # Add families if mode allows
        if mode in ("all", "families_only") and len(choices) < 25:
            for fam_tag, fam_data in sorted(CACHE.clan_families.items(), key=lambda x: x[1].get('name', '').lower()):
                if not fam_data:
                    continue
                name = fam_data.get('name', 'Unknown Family')
                label = f"{name} ({fam_tag})"
                if not current_lower or current_lower in label.lower():
                    choices.append(discord.app_commands.Choice(name=label[:100], value=fam_tag))
                if len(choices) >= 25:
                    break
        
        return choices[:25]
    
    return autocomplete_handler


# ============================================================================
# Playerregistration Command Helper Functions
# ============================================================================

def get_playerregistration_message(server_name: str, guild_id: Optional[int] = None) -> str:
    """
    Generate registration message text for player registration.
    
    Args:
        server_name: Name of the Discord server
        guild_id: Discord guild ID for language preference (optional)
        
    Returns:
        str: Formatted registration message with bold headline (localized)
        
    Note:
        This is the single source of truth for the registration message text.
        Used by both setup_playerregistration_message() and repost_playerregistration_messages().
        Uses i18n for multi-language support.
    """
    from qapbot.i18n import t, get_guild_language
    import logging
    
    # Get current language for logging
    current_language = get_guild_language(guild_id)
    logging.debug(f"get_playerregistration_message: guild_id={guild_id}, language='{current_language}'")
    
    title = t("playerregistration.welcome_title", guild_id=guild_id, server_name=server_name)
    body = t("playerregistration.welcome_body", guild_id=guild_id)
    
    return f"⠀\n**{title}**\n\n{body}\n⠀"


# Keep old name as alias for any callers not yet updated
get_playerregistration_welcome_message = get_playerregistration_message

async def setup_playerregistration_message(interaction: discord.Interaction, guild_id: str, channel_id: str) -> Tuple[bool, str, Optional[discord.Message]]:
    """
    Create and post playerregistration registration message with persistent button.

    Args:
        interaction: Discord interaction object
        guild_id: Guild ID string
        channel_id: Channel ID string

    Returns:
        Tuple of (success: bool, status_message: str, posted_message: Optional[discord.Message])

    Business Logic:
        1. Check for existing playerregistration subscription
        2. Delete old message if found (best-effort)
        3. Create registration message with RegistrationView button
        4. Return message for subscription tracking
    """
    from qapbot.ui_registration import RegistrationView
    import logging
    
    current_subs = CACHE.get_channel_subscriptions(channel_id)

    # If playerregistration already exists, find and delete old messages
    existing_indices = [i for i, s in enumerate(current_subs) if s and s.get('subscription_type') == 'playerregistration']
    if existing_indices:
        # Delete all tracked old messages (best-effort) and collapse duplicates to a single entry
        for i in existing_indices:
            old_message_id = current_subs[i].get('message_id')
            if old_message_id:
                try:
                    if not interaction.channel or not hasattr(interaction.channel, 'fetch_message'):
                        continue
                    old_msg = await interaction.channel.fetch_message(int(old_message_id))  # type: ignore[union-attr]
                    await old_msg.delete()
                    logging.info(f"Deleted old playerregistration message {old_message_id} from channel {channel_id}")
                except Exception as e:
                    logging.debug(f"Could not delete old playerregistration message {old_message_id} from channel {channel_id}: {e}")
    
    # Create and post the registration message
    server_name = interaction.guild.name if interaction.guild else "Unknown Server"
    registration_msg = get_playerregistration_message(server_name, guild_id=interaction.guild_id)
    
    if not interaction.channel or not hasattr(interaction.channel, 'send'):
        logging.warning("Cannot send registration message: channel is None or does not support sending messages")
        return False, "error", None
    
    if not interaction.guild_id:
        logging.warning("Cannot send registration message: guild_id is None")
        return False, "error", None
    
    view = RegistrationView(interaction.guild_id)
    message = await interaction.channel.send(registration_msg, view=view)  # type: ignore[union-attr]
    
    return True, "created", message


async def update_playerregistration_subscription(channel_id: str, guild_id: str, message_id: str, existing_indices: List[int]) -> str:
    """
    Update or create playerregistration subscription tracking entry.

    Args:
        channel_id: Channel ID string
        guild_id: Guild ID string
        message_id: Posted message ID string
        existing_indices: List of indices of existing playerregistration subscriptions

    Returns:
        Action label: "updated" or "created"

    Business Logic:
        - If existing entry found, update message_id and remove duplicates
        - Otherwise, add new subscription entry
        - Save subscriptions to cache
    """
    current_subs = CACHE.get_channel_subscriptions(channel_id)
    
    if existing_indices:
        # Keep only the first entry; remove any duplicates
        keep_idx = existing_indices[0]
        current_subs = [s for j, s in enumerate(current_subs) if j == keep_idx or j not in existing_indices]
        
        # Update the kept subscription entry
        for s in current_subs:
            if s and s.get('subscription_type') == 'playerregistration':
                s['clan_tag'] = 'PLAYERREGISTRATION'  # Stable marker (not a real clan)
                s['message_id'] = message_id
                break
        action_label = "updated"
    else:
        # Add special subscription to track this playerregistration message
        current_subs.append({
            'clan_tag': 'PLAYERREGISTRATION',  # Special marker for playerregistration messages
            'subscription_type': 'playerregistration',
            'message_id': message_id
        })
        action_label = "created"

    await CACHE.set_channel_subscriptions(guild_id, channel_id, current_subs)
    
    return action_label


def format_notification_settings(user_data: Dict[str, Any], display_name: str, user_id: Optional[str] = None, guild_id: Optional[int] = None) -> str:
    """
    Format notification settings for display.
    
    Args:
        user_data: User account data from CACHE.user_accounts
        display_name: Discord display name
        user_id: Discord user ID for user-specific language preference
        guild_id: Optional guild ID for translation context fallback
    
    Returns:
        Formatted settings string for Discord message
    
    Example:
        settings = format_notification_settings(user_data, "Our Clan", guild_id=123456)
        # Returns formatted settings with player list and notification status
    """
    from qapbot.i18n import t
    
    notif_settings = user_data.get("notification_settings", {})
    war_reminders = notif_settings.get("war_reminders", False)
    notif_mode = notif_settings.get("notification_mode", "repeated")
    notif_type = notif_settings.get("notification_type", "all_wars")
    hours_before = notif_settings.get("hours_before_end", 4)
    
    # Get user language preference
    user_language = user_data.get("user_language", "en")
    user_language_locked = user_data.get("user_language_locked", False)
    
    # Format language display
    if user_language_locked:
        # User manually set the language - map language codes to translation keys
        language_map = {
            "en": "warnotifications.language_english",
            "de": "warnotifications.language_german"
        }
        language_display_key = language_map.get(user_language.lower(), "warnotifications.language_english")
        language_display = t(language_display_key, user_id=user_id, guild_id=guild_id)
    else:
        # Language auto-detected from Discord
        language_display = t('warnotifications.language_auto', user_id=user_id, guild_id=guild_id)
    
    players = user_data.get("players", [])
    
    status_emoji = "✅" if war_reminders else "❌"
    status_text = t('warnotifications.enabled', user_id=user_id, guild_id=guild_id) if war_reminders else t('warnotifications.disabled', user_id=user_id, guild_id=guild_id)
    
    # Format notification type display
    if notif_type == "cwl_only":
        type_value = t('warnotifications.wartype_cwl', user_id=user_id, guild_id=guild_id)
        type_description = t('warnotifications.wartype_cwl_desc', user_id=user_id, guild_id=guild_id)
    else:
        type_value = t('warnotifications.wartype_all', user_id=user_id, guild_id=guild_id)
        type_description = t('warnotifications.wartype_all_desc', user_id=user_id, guild_id=guild_id)
    
    # Format notification mode display
    if notif_mode == "once":
        mode_value = t('warnotifications.mode_once', user_id=user_id, guild_id=guild_id)
        mode_description = t('warnotifications.mode_once_desc', user_id=user_id, guild_id=guild_id, hours=hours_before)
    elif notif_mode == "repeated":
        mode_value = t('warnotifications.mode_repeated', user_id=user_id, guild_id=guild_id)
        mode_description = t('warnotifications.mode_repeated_desc', user_id=user_id, guild_id=guild_id, hours=hours_before)
    else:
        mode_value = notif_mode
        mode_description = ""  # Fallback for unknown modes
    
    # Format own player list
    players = user_data.get("players", [])
    player_lines = []
    for player in players:
        if not isinstance(player, dict):
            continue
        p_name = player.get("player_name", "Unknown")
        p_id = player.get("player_tag", "Unknown")
        verified = player.get("verified", False)
        verify_emoji = "✅" if verified else "❌"
        player_lines.append(f"•  {p_name} ({p_id}) {verify_emoji}")

    # Cap player list to avoid exceeding Discord's 2000 char message limit
    MAX_PLAYER_LINES = 15
    if len(player_lines) > MAX_PLAYER_LINES:
        overflow = len(player_lines) - MAX_PLAYER_LINES
        player_lines = player_lines[:MAX_PLAYER_LINES]
        player_lines.append(f"... (+{overflow} more)")

    if player_lines:
        players_text = t('warnotifications.players_title', user_id=user_id, guild_id=guild_id) + "\n\n" + "\n".join(player_lines)
    else:
        players_text = t('warnotifications.players_title', user_id=user_id, guild_id=guild_id) + "\n\n" + t('warnotifications.no_players_registered', user_id=user_id, guild_id=guild_id)

    # Format buddy list (Save-your-Buddy section)
    watched_players = user_data.get("watched_players", [])
    buddy_lines = []
    for buddy in watched_players:
        if not isinstance(buddy, dict):
            continue
        b_name = buddy.get("player_name", "Unknown")
        b_tag = buddy.get("player_tag", "Unknown")
        buddy_lines.append(f"•  {b_name} ({b_tag}) 👥")

    # Cap buddy list for the same reason
    MAX_BUDDY_LINES = 10
    if len(buddy_lines) > MAX_BUDDY_LINES:
        overflow_b = len(buddy_lines) - MAX_BUDDY_LINES
        buddy_lines = buddy_lines[:MAX_BUDDY_LINES]
        buddy_lines.append(f"... (+{overflow_b} more)")

    if buddy_lines:
        buddies_text = "\n\n" + t('warnotifications.buddies_title', user_id=user_id, guild_id=guild_id) + "\n\n" + "\n".join(buddy_lines)
    else:
        buddies_text = "\n\n" + t('warnotifications.buddies_title', user_id=user_id, guild_id=guild_id) + "\n\n" + t('warnotifications.no_buddies', user_id=user_id, guild_id=guild_id)

    message = f"""{t('warnotifications.title', user_id=user_id, guild_id=guild_id)}

{t('warnotifications.description', user_id=user_id, guild_id=guild_id)}

{t('warnotifications.user_label', user_id=user_id, guild_id=guild_id)} {display_name}
{t('warnotifications.status_label', user_id=user_id, guild_id=guild_id)} {status_emoji} **__`{status_text}`__**
{t('warnotifications.wartype_label', user_id=user_id, guild_id=guild_id)} **__`{type_value}`__**{type_description}
{t('warnotifications.mode_label', user_id=user_id, guild_id=guild_id)} **__`{mode_value}`__**{mode_description}
{t('warnotifications.timing_label', user_id=user_id, guild_id=guild_id)} **__`{hours_before}`__**{t('warnotifications.timing_hours', user_id=user_id, guild_id=guild_id)}
{t('warnotifications.language_label', user_id=user_id, guild_id=guild_id)} **__`{language_display}`__**

```{players_text}{buddies_text}```⠀"""

    # Last-resort safety clamp — Discord rejects messages > 2000 chars
    if len(message) > 1990:
        suffix = "\n...```⠀"
        message = message[:1990 - len(suffix)] + suffix

    return message


async def update_notification_subscription(
    user_id: str,
    enable: bool,
    user_data: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Update notification subscription for ALL of a user's players.
    
    Notification settings are user-level: enabling/disabling affects all
    registered players for that Discord user.
    
    Args:
        user_id: Discord user ID
        enable: True to subscribe, False to unsubscribe
        user_data: Optional user data from CACHE (will fetch if not provided)
    
    Returns:
        Dict with:
        - success: bool
        - message: str (error message if failed)
        - player_count: int (number of players affected)
        - player_names: List[str] (names of affected players)
    
    Side Effects:
        Updates CACHE.user_accounts with notification_settings for ALL players
        and persists the affected user to database (write-through)
    
    Example:
        result = await update_notification_subscription(
            user_id="123456789012345678",
            enable=True
        )
    """
    if user_data is None:
        user_data = CACHE.user_accounts.get(user_id)
        if not user_data:
            return {
                "success": False,
                "message": "User not found in accounts",
                "player_count": 0,
                "player_names": []
            }
    
    players = user_data.get("players", [])
    if not players:
        return {
            "success": False,
            "message": "No registered players found",
            "player_count": 0,
            "player_names": []
        }
    
    # Initialize or update notification_settings with all required fields
    if "notification_settings" not in user_data:
        user_data["notification_settings"] = {}
    
    # Ensure all fields exist with defaults
    if "notification_mode" not in user_data["notification_settings"]:
        user_data["notification_settings"]["notification_mode"] = "repeated"
    if "notification_type" not in user_data["notification_settings"]:
        user_data["notification_settings"]["notification_type"] = "all_wars"
    if "hours_before_end" not in user_data["notification_settings"]:
        user_data["notification_settings"]["hours_before_end"] = 4
    
    # Update subscription status (applies to ALL players)
    user_data["notification_settings"]["war_reminders"] = enable
    
    # Ensure correct field order while preserving all existing fields
    ordered_user_data = {
        "display_name": user_data.get("display_name", "Unknown"),
        "notification_settings": user_data["notification_settings"],
        "players": user_data.get("players", [])
    }
    
    # Preserve additional fields (user_language, user_language_locked, etc.)
    for key, value in user_data.items():
        if key not in ordered_user_data:
            ordered_user_data[key] = value
    
    # Update CACHE with properly ordered data
    CACHE.user_accounts[user_id] = ordered_user_data
    await CACHE.persist_user(user_id)
    
    # Collect player names for confirmation
    player_names = [p.get("player_name", "Unknown") for p in players]
    
    return {
        "success": True,
        "message": "",
        "player_count": len(players),
        "player_names": player_names
    }

def get_most_active_clan_for_guild(guild_id: int, clan_tags: List[str]) -> str:
    """
    Get the most active clan for a guild based on subscription count and war stats.
    
    Args:
        guild_id: Discord guild ID
        clan_tags: List of clan tags to choose from
        
    Returns:
        str: Most active clan tag (or first clan if no clear winner)
    """
    from qapbot.cache_manager import CACHE
    
    if not clan_tags:
        return ""
    
    # Count subscriptions per clan for this guild
    clan_sub_counts = {tag: 0 for tag in clan_tags}
    
    subs = CACHE.get_all_subscriptions_flat()
    for _, channel_subs in subs.items():  # type: ignore[misc]
        # Check if channel belongs to this guild (rough heuristic: high bits match)
        # Note: This is simplified; ideally we'd track guild_id per channel
        for sub in channel_subs:
            if sub:
                tag = sub.get('clan_tag')
                if tag in clan_sub_counts:
                    clan_sub_counts[tag] += 1
    
    # Return clan with most subscriptions, or first clan if tied
    if clan_sub_counts:
        most_active = max(clan_sub_counts.items(), key=lambda x: x[1])
        return most_active[0]
    
    return clan_tags[0]


def _split_content_into_embeds(
    clan_name: str,
    clan_tag: str,
    header_text: str,
    content_lines: List[str],
    max_length: int = 4096,
    repeat_header: bool = True
) -> List[discord.Embed]:
    """
    Helper function to split content into multiple embeds when needed.
    
    Args:
        clan_name: Clan name for embed author (all embeds)
        clan_tag: Clan tag for embed author (all embeds)
        header_text: Header text to include in embeds
        content_lines: List of content sections (each should be a complete unit)
        max_length: Maximum characters per embed (default 4096, Discord's limit)
        repeat_header: If True, include header_text on every page (for pagination)
        
    Returns:
        List of Discord embeds (1 or more)
    """
    embeds = []
    current_content = header_text
    
    for line in content_lines:
        # Check if adding this line would exceed limit
        test_content = current_content + "\n" + line if current_content != header_text else header_text + line
        
        if len(test_content) > max_length and current_content != header_text:
            # Save current embed and start new one
            embed = discord.Embed(color=0x2B2D31)
            embed.set_author(name=f"{clan_name} ({clan_tag})")
            embed.description = current_content
            embeds.append(embed)
            
            # Start new embed with or without header based on repeat_header
            current_content = header_text + line if repeat_header else line
        else:
            # Add to current content
            if current_content == header_text:
                current_content = header_text + line
            else:
                current_content += "\n" + line
    
    # Add final embed
    if current_content:
        embed = discord.Embed(color=0x2B2D31)
        embed.set_author(name=f"{clan_name} ({clan_tag})")
        embed.description = current_content
        embeds.append(embed)
    
    return embeds if embeds else [discord.Embed(color=0x2B2D31, description="No content")]


def _split_content_into_two_embeds(
    clan_name: str,
    clan_tag: str,
    header_text: str,
    content_lines: List[str]
) -> List[discord.Embed]:
    """
    Split content into exactly two embeds, making the first embed as close to 4096 chars as possible
    without splitting midline.
    
    Args:
        clan_name: Clan name for embed author (first embed only)
        clan_tag: Clan tag for embed author (first embed only)
        header_text: Header text to include in first embed only
        content_lines: List of content sections (each should be a complete unit)
        
    Returns:
        List of 1-2 Discord embeds
    """
    # Build first embed with header
    first_content = header_text
    split_index = 0
    
    # Add lines to first embed until we approach 4096 limit
    for i, line in enumerate(content_lines):
        test_content = first_content + "\n" + line if first_content != header_text else header_text + line
        
        if len(test_content) > 4096:
            # Stop before this line - it would exceed limit
            break
        else:
            # Add to first embed
            if first_content == header_text:
                first_content = header_text + line
            else:
                first_content += "\n" + line
            split_index = i + 1
    
    # Create first embed
    first_embed = discord.Embed(color=0x2B2D31)
    first_embed.set_author(name=f"{clan_name} ({clan_tag})")
    first_embed.description = first_content
    
    # Check if we need a second embed
    if split_index >= len(content_lines):
        # All content fit in first embed
        return [first_embed]
    
    # Create second embed with remaining content
    second_content = "\n".join(content_lines[split_index:])
    second_embed = discord.Embed(color=0x2B2D31)
    second_embed.description = second_content
    
    return [first_embed, second_embed]


async def format_clan_management_message(clan_tag: str, guild: discord.Guild, mode: str = "registrations") -> Tuple[discord.Embed, Optional[discord.Embed], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Format a clan management message showing linked and unlinked players using embeds.
    
    Args:
        clan_tag: Normalized clan tag (e.g., "#2C9UR9GJY")
        guild: Discord guild object for member lookup
        mode: View mode - "registrations" (default) or "notifications"
        
    Returns:
        Tuple[discord.Embed, Optional[discord.Embed], List[Dict], List[Dict]]: 
            (main_embed, unlinked_embed, linked_players, unlinked_players)
        
    Message Format:
        Main Embed (dark background):
            Title: ClanName (#CLANTAG)
            Field: Players in the Server: 49
            Content: 
                ✅ 18    PlayerName DiscordName
                ✅ 18    PlayerName DiscordName
                ...
        
        Unlinked Embed (dark background):
            Title: Players not Linked: 1
            Content:
                ❌ 18 PlayerName #PLAYERTAG
            Footer: Sorted by Town Hall Level
        
    Player Dict Format:
        {
            "tag": "#PLAYERTAG",
            "name": "PlayerName",
            "th_level": 18,
            "discord_user_id": "123456789",  # Only for linked players
            "discord_name": "DiscordName",   # Only for linked players
            "verified": True/False           # Only for linked players
        }
    """
    # Route to appropriate mode handler
    if mode == "notifications":
        return await _format_clan_management_notifications(clan_tag, guild)
    elif mode == "roles":
        return await _format_clan_management_roles(guild)
    elif mode == "families":
        return await _format_clan_management_families(guild)
    elif mode == "config":
        return await _format_clan_management_config(guild)
    else:  # Default to registrations
        return await _format_clan_management_registrations(clan_tag, guild)


async def _format_clan_management_registrations(clan_tag: str, guild: discord.Guild) -> Tuple[discord.Embed, Optional[discord.Embed], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Format clan management message for registrations mode (current functionality).
    """
    from qapbot.cache_manager import CACHE
    from qapbot.i18n import t
    
    guild_id = str(guild.id)
    guild_id_int = int(guild_id)
    
    # Get clan name
    clan_name = CACHE.get_clan_name(clan_tag, "Unknown Clan")
    
    # Use unified player list function to get all players with activity scores
    try:
        player_list_with_activity = await get_player_list(
            [clan_tag],
            exclude_registered_players=False,  # Include all players
            use_live_api=True,  # Get current clan members
            sort_by="activity"  # Sort by activity (most active first)
        )
    except Exception as e:
        logging.error(f"Failed to get player list for {clan_tag}: {e}")
        from qapbot.i18n import t
        error_desc = t('clan_management_errors.fetch_failed')
        return discord.Embed(title="Error", description=error_desc), None, [], []
    
    if not player_list_with_activity:
        from qapbot.i18n import t
        error_desc = t('clan_management_errors.no_players')
        return discord.Embed(title="Error", description=error_desc), None, [], []
    
    # Filter to only current clan members
    player_list_with_activity = [p for p in player_list_with_activity if p.get("is_current_member", False)]
    
    if not player_list_with_activity:
        from qapbot.i18n import t
        error_desc = t('clan_management_errors.no_members')
        return discord.Embed(title="Error", description=error_desc), None, [], []
    
    # Build player dict with activity scores and TH levels
    player_data = {}
    for player in player_list_with_activity:
        tag = player["tag"]
        player_data[tag] = {
            "tag": tag,
            "name": player["name"],
            "th_level": player.get("th_level"),  # Already fetched from API via get_player_list
            "activity": player["activity"]
        }
    
    # No need for second API call - TH levels already included from get_player_list
    
    # Build reverse lookup: player_tag -> (discord_user_id, verified)
    player_to_discord = {}
    for user_id, user_data in CACHE.user_accounts.items():
        if user_id == "UNASSIGNED" or not user_data:
            continue
        
        for player in user_data.get("players", []):
            if not isinstance(player, dict):
                continue
            
            player_id = player.get("player_tag")
            if player_id:
                player_to_discord[player_id] = {
                    "user_id": user_id,
                    "verified": player.get("verified", False)
                }
    
    # Separate linked and unlinked players
    linked_players = []
    unlinked_players = []
    
    for player_tag, pdata in player_data.items():
        th_level = pdata.get("th_level") or "?"
        activity = pdata.get("activity", 0)
        
        if player_tag in player_to_discord:
            # Player is linked
            discord_info = player_to_discord[player_tag]
            user_id = discord_info["user_id"]
            verified = discord_info["verified"]
            
            # Get Discord display name from cache (no API call needed!)
            user_account = CACHE.user_accounts.get(user_id, {})
            discord_name = user_account.get("display_name", f"UserID:{user_id[:10]}")
            
            linked_players.append({
                "tag": player_tag,
                "name": pdata["name"],
                "th_level": th_level,
                "discord_user_id": user_id,
                "discord_name": discord_name,
                "verified": verified,
                "activity": activity
            })
        else:
            # Player is not linked
            unlinked_players.append({
                "tag": player_tag,
                "name": pdata["name"],
                "th_level": th_level,
                "activity": activity
            })
    
    # Sort by TH level (descending), then by name
    # Handle cases where th_level might be "?" or None
    linked_players.sort(key=lambda x: (-(x["th_level"] if isinstance(x["th_level"], int) else 0), x["name"].lower()))  # type: ignore[misc]
    unlinked_players.sort(key=lambda x: (-(x["th_level"] if isinstance(x["th_level"], int) else 0), x["name"].lower()))  # type: ignore[misc]
    
    # Build main embed with DD bot style formatting
    main_embed = discord.Embed(
        color=0x2B2D31  # Dark gray
    )
    
    # Use author field for clan name (like DD bot)
    main_embed.set_author(name=f"{clan_name} ({clan_tag})")
    
    # Build header with legend
    header_text = f"**{t('ui_components.clan_registrations_display.players_in_clan', guild_id=guild_id_int, count=len(player_data))}**\n\n"
    legend_text = f"{BotEmojis.VERIFIED} {t('ui_components.clan_registrations_display.legend_verified', guild_id=guild_id_int)}\u00A0\u00A0\u00A0\u00A0\u00A0{BotEmojis.GCHECK} {t('ui_components.clan_registrations_display.legend_registered', guild_id=guild_id_int)}\u00A0\u00A0\u00A0\u00A0\u00A0{BotEmojis.REDX} {t('ui_components.clan_registrations_display.legend_unregistered', guild_id=guild_id_int)}\n\n"
    
    # Build linked players section with inline code formatting (single backticks per line)
    linked_lines = []
    linked_lines.append(f"**{t('ui_components.clan_registrations_display.players_linked', guild_id=guild_id_int, count=len(linked_players))}**")
    
    # Import width calculation functions from formatting module
    from qapbot.formatting import best_practice_player_cell  # noqa: F401
    
    # Unicode left-to-right marks
    LRM = "\u200E"
    RLM = "\u200F"
    
    # Add ALL linked players with inline code formatting (no limit)
    for player in linked_players:
        th = player["th_level"]
        name = player["name"]
        discord_name = player["discord_name"]
        verified = player["verified"]
        
        # Pad TH level to 2 digits for alignment
        th_str = f"{th:>2}"
        
        # Target widths for columns
        max_name_width = 17
        max_discord_width = 17  # Reduced by 1 char
        
        # Use best-practice padding with width calculation (includes normalization)
        name_padded = best_practice_player_cell(name, max_name_width)
        discord_cell = best_practice_player_cell(discord_name, max_discord_width)
        
        # Add LRM markers around each text component to force LTR
        name_padded = f"{LRM}{name_padded}{LRM}"
        discord_cell = f"{LRM}{discord_cell}{LRM}"
        
        # Build line with inline code - use blue verified icon if verified, otherwise green checkmark
        line_content = f"{th_str} {name_padded} {discord_cell}"
        status_icon = BotEmojis.VERIFIED if verified else BotEmojis.GCHECK
        line = f"{status_icon} `{LRM}{line_content}{RLM}`"
        linked_lines.append(line)
    
    linked_text = "\n".join(linked_lines) + "\n"
    
    # Build unlinked players section with inline code formatting
    unlinked_text = ""
    if unlinked_players:
        unlinked_lines = []
        unlinked_lines.append(f"\n**{t('ui_components.clan_registrations_display.players_not_linked', guild_id=guild_id_int, count=len(unlinked_players))}**")
        
        for player in unlinked_players:
            th = player["th_level"]
            name = player["name"]
            tag = player["tag"]
            
            # Pad TH level
            th_str = f"{th:>2}"
            
            # Target width for name column - match linked block
            max_name_width = 17
            max_tag_width = 17  # Pad tag to match discord name column width
            
            # Use best-practice padding with width calculation
            name_padded = best_practice_player_cell(name, max_name_width)
            
            # Pad tag to fixed width
            tag_padded = f"{tag:<{max_tag_width}}"
            
            # Add LRM markers around name to force LTR
            name_padded = f"{LRM}{name_padded}{LRM}"
            tag_padded = f"{LRM}{tag_padded}{LRM}"
            
            # Build line with inline code
            line_content = f"{th_str} {name_padded} {tag_padded}"
            line = f"{BotEmojis.REDX} `{LRM}{line_content}{RLM}`"
            unlinked_lines.append(line)
        
        unlinked_text = "\n".join(unlinked_lines)
    
    # Combine all content and calculate total character count
    full_content = header_text + legend_text + linked_text + unlinked_text
    total_chars = len(full_content)
    
    # Split into sections by player (each line is a section)
    content_sections = (linked_text + unlinked_text).split('\n')
    
    # Apply pagination rules based on character count
    if total_chars <= 4096:
        # Rule 1: Single embed, no pagination
        embed = discord.Embed(color=0x2B2D31)
        embed.set_author(name=f"{clan_name} ({clan_tag})")
        embed.description = full_content
        return embed, None, linked_players, unlinked_players
    
    elif total_chars <= 6000:
        # Rule 2: Two embeds, first as close to 4096 as possible, no pagination
        embeds = _split_content_into_two_embeds(
            clan_name=clan_name or "Unknown Clan",
            clan_tag=clan_tag,
            header_text=header_text + legend_text,
            content_lines=list(content_sections)  # type: ignore[arg-type]
        )
        if len(embeds) == 1:
            return embeds[0], None, linked_players, unlinked_players
        elif len(embeds) == 2:
            return embeds[0], embeds[1], linked_players, unlinked_players
        else:
            # Fallback if split logic returns more than 2
            return embeds[0], embeds[1], linked_players, unlinked_players
    
    else:
        # Rule 3: Pagination mode (>6000 chars)
        embeds = _split_content_into_embeds(
            clan_name=clan_name or "Unknown Clan",
            clan_tag=clan_tag,
            header_text=header_text + legend_text,
            content_lines=list(content_sections),  # type: ignore[arg-type]
            max_length=4000,  # Conservative 4096 limit with buffer
            repeat_header=True  # Include header and legend on every page
        )
        # Return as list in unlinked_embed slot for pagination
        return None, embeds, linked_players, unlinked_players  # type: ignore[return-value]


async def _format_clan_management_notifications(clan_tag: str, guild: discord.Guild) -> Tuple[discord.Embed, Optional[discord.Embed], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Format clan management message for notifications mode.
    
    Shows Discord users who have at least one player in the selected clan,
    along with ALL their linked players (across all clans) and their notification settings.
    
    Uses multiple embeds if content exceeds single embed limit.
    
    Args:
        clan_tag: Normalized clan tag
        guild: Discord guild object
        
    Returns:
        Tuple with:
        - main_embed: First embed (or None if returning via unlinked_embed)
        - unlinked_embed: Either single embed, list of embeds, or None
        - linked_players: Empty list (not used in notification mode)
        - unlinked_players: Empty list (not used in notification mode)
        
    Note: When multiple embeds needed, returns (None, List[embeds], [], [])
    """
    from qapbot.cache_manager import CACHE
    from qapbot.i18n import t
    
    guild_id = str(guild.id)
    guild_id_int = int(guild_id)
    
    # Get clan name
    clan_name = CACHE.get_clan_name(clan_tag, "Unknown Clan")
    
    # Get current clan members from API (via cache)
    try:
        clan_obj = await CACHE.coc_clan_cache.get_clan(clan_tag)
        clan_member_tags = {member.tag for member in clan_obj.members}
    except Exception as e:
        logging.error(f"Failed to fetch clan {clan_tag} from API: {e}")
        return discord.Embed(title="Error", description=f"❌ Failed to fetch clan data."), None, [], []
    
    if not clan_member_tags:
        return discord.Embed(title="Error", description=f"❌ No members found in clan."), None, [], []
    
    # Find Discord users who have at least ONE player in this clan
    users_with_players_in_clan = {}  # user_id -> user_data
    
    for user_id, user_data in CACHE.user_accounts.items():
        if user_id == "UNASSIGNED" or not user_data:
            continue
        
        players = user_data.get("players", [])
        if not players:
            continue
        
        # Check if user has any player in this clan
        has_player_in_clan = False
        for player in players:
            if not isinstance(player, dict):
                continue
            player_tag = player.get("player_tag")
            if player_tag in clan_member_tags:
                has_player_in_clan = True
                break
        
        if has_player_in_clan:
            users_with_players_in_clan[user_id] = user_data
    
    # If no users found, return empty view
    if not users_with_players_in_clan:
        main_embed = discord.Embed(color=0x2B2D31)
        main_embed.set_author(name=f"{clan_name} ({clan_tag})")
        main_embed.description = f"**{t('ui_components.war_notifications_display.title', guild_id=guild_id_int)}**\n\n{t('ui_components.war_notifications_display.no_players_found', guild_id=guild_id_int)}"
        return main_embed, None, [], []
    
    # Build output grouped by Discord user
    
    LRM = "\u200E"
    RLM = "\u200F"
    
    user_sections = []
    
    for user_id, user_data in sorted(users_with_players_in_clan.items()):
        # Get Discord display name from cache (no API call needed!)
        discord_name = user_data.get("display_name", f"UserID:{user_id[:10]}")
        
        # Get notification settings
        notif_settings = user_data.get("notification_settings", {})
        war_reminders = notif_settings.get("war_reminders", False)
        notif_mode = notif_settings.get("notification_mode", "repeated")
        notif_type = notif_settings.get("notification_type", "all_wars")
        
        # Format notification status with icons only (text in legend)
        if war_reminders:
            notif_emoji = BotEmojis.ENABLED
        else:
            notif_emoji = BotEmojis.DISABLED
        
        # Mode icon: repeated or once
        mode_emoji = BotEmojis.ONCE if notif_mode == "once" else BotEmojis.REPEATED
        
        # Type icon: CWL only or all wars
        type_emoji = BotEmojis.CWL if notif_type == "cwl_only" else BotEmojis.ALLWARS
        
        # Format header for this user (emojis before name on same line)
        user_header = f"{notif_emoji} {mode_emoji} {type_emoji} **{discord_name}**"
        
        # Get ALL players for this user (across all clans)
        players = user_data.get("players", [])
        player_lines = []
        
        for player in players:
            if not isinstance(player, dict):
                continue
            
            player_tag = player.get("player_tag")
            player_name = player.get("player_name", "Unknown")
            verified = player.get("verified", False)
            
            # Get player's TH level and clan
            # Read TH and clan info from user_accounts (updated by clan cache)
            th_level = player.get("th_level", "?")
            player_clan_tag = player.get("current_clan_tag")
            
            # Fetch player info from API if missing (only when th_level is unknown or clan is unknown)
            if th_level == "?" or th_level is None or player_clan_tag is None:
                player_info = await CACHE.fetch_and_update_player_info(player_tag)  # type: ignore[arg-type]
                if player_info:
                    th_level = player_info.get("th_level", "?")
                    player_clan_tag = player_info.get("clan_tag")
                    # Update the player dict in memory so subsequent loops don't refetch
                    player["th_level"] = th_level
                    player["current_clan_tag"] = player_clan_tag
            
            # Determine if player is in current clan
            in_current_clan = (player_tag in clan_member_tags)
            
            # Status icon
            if verified:
                status_icon = BotEmojis.VERIFIED
            else:
                status_icon = BotEmojis.GCHECK
            
            # Format: TH  PlayerName  [clan indicator if not in current clan]
            th_str = f"{th_level:>2}"
            
            # Use visual indicator suffix for players in other clans (maintains emoji alignment)
            if in_current_clan:
                # Normal player in current clan (no padding to save characters)
                line = f"  {status_icon} `{LRM}{th_str} {LRM}{player_name}{LRM}{RLM}`"
            else:
                # Other clan player with trailing indicator (no padding to save characters)
                line = f"  {status_icon} `{LRM}{th_str} {LRM}{player_name}{LRM}{RLM}` 🔹"
            
            player_lines.append(line)
        
        # Combine user section
        user_section = user_header + "\n" + "\n".join(player_lines) + "\n"  # Add blank line after user section
        user_sections.append(user_section)
    
    # Build header with count and legend
    header_text = f"**{t('ui_components.war_notifications_display.title', guild_id=guild_id_int)}**\n"
    header_text += f"{t('ui_components.war_notifications_display.players_with_accounts', guild_id=guild_id_int, count=len(users_with_players_in_clan))}\n\n"
    header_text += f"**{t('ui_components.war_notifications_display.legend_title', guild_id=guild_id_int)}:**\n"
    header_text += f"{BotEmojis.VERIFIED} {t('ui_components.war_notifications_display.legend_verified', guild_id=guild_id_int)}  {BotEmojis.GCHECK} {t('ui_components.war_notifications_display.legend_registered', guild_id=guild_id_int)}  🔹 {t('ui_components.war_notifications_display.legend_player_other_clan', guild_id=guild_id_int)}\n"
    header_text += f"{BotEmojis.ENABLED} {t('ui_components.war_notifications_display.legend_enabled', guild_id=guild_id_int)}  {BotEmojis.DISABLED} {t('ui_components.war_notifications_display.legend_disabled', guild_id=guild_id_int)}  {BotEmojis.REPEATED} {t('ui_components.war_notifications_display.legend_repeated', guild_id=guild_id_int)}  {BotEmojis.ONCE} {t('ui_components.war_notifications_display.legend_once', guild_id=guild_id_int)}  {BotEmojis.ALLWARS} {t('ui_components.war_notifications_display.legend_all_wars', guild_id=guild_id_int)}  {BotEmojis.CWL} {t('ui_components.war_notifications_display.legend_cwl_only', guild_id=guild_id_int)}\n\n"
    
    # Calculate total character count
    full_content = header_text + "\n".join(user_sections)
    total_chars = len(full_content)
    
    # Apply pagination rules based on character count
    if total_chars <= 4096:
        # Rule 1: Single embed, no pagination
        embed = discord.Embed(color=0x2B2D31)
        embed.set_author(name=f"{clan_name} ({clan_tag})")
        embed.description = full_content
        return embed, None, [], []
    
    elif total_chars <= 6000:
        # Rule 2: Two embeds, first as close to 4096 as possible, no pagination
        embeds = _split_content_into_two_embeds(
            clan_name=clan_name or "Unknown Clan",
            clan_tag=clan_tag,
            header_text=header_text,
            content_lines=list(user_sections)  # type: ignore[arg-type]
        )
        if len(embeds) == 1:
            return embeds[0], None, [], []
        elif len(embeds) == 2:
            return embeds[0], embeds[1], [], []
        else:
            # Fallback if split logic returns more than 2
            return embeds[0], embeds[1], [], []
    
    else:
        # Rule 3: Pagination mode (>6000 chars)
        logging.info(f"[NOTIFICATION-VIEW] Total chars {total_chars} > 6000, using pagination")
        embeds = _split_content_into_embeds(
            clan_name=clan_name or "Unknown Clan",
            clan_tag=clan_tag,
            header_text=header_text,
            content_lines=list(user_sections),  # type: ignore[arg-type]
            max_length=4000,  # Conservative 4096 limit with buffer
            repeat_header=True  # Include header on every page
        )
        # Return as list in unlinked_embed slot for pagination
        return None, embeds, [], []  # type: ignore[return-value]


async def _format_clan_management_roles(guild: discord.Guild) -> Tuple[discord.Embed, Optional[discord.Embed], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Format clan management message for server roles configuration mode.
    
    Shows current role assignment configuration:
    - Newbie role (given to all new verifiers)
    - Member role (given when verified in member clans)
    - List of clans that grant member status
    
    Args:
        guild: Discord guild object
        
    Returns:
        Tuple with:
        - main_embed: Configuration display embed
        - unlinked_embed: None
        - linked_players: Empty list
        - unlinked_players: Empty list
    """
    from qapbot.cache_manager import CACHE
    from qapbot.i18n import t
    
    guild_id = str(guild.id)
    guild_id_int = int(guild_id)
    config = CACHE.server_config.get(guild_id, {})
    
    # Get configuration values
    role_system_enabled = config.get("role_system_enabled", False)
    newbie_role_id = config.get("newbie_role_id")
    member_role_id = config.get("member_role_id")
    member_role_strict: bool = config.get("member_role_strict", False)
    member_clans = config.get("member_clans", [])
    member_families = config.get("member_families", [])
    coc_role_enabled = config.get("coc_role_enabled", False)
    clan_role_enabled = config.get("clan_role_enabled", False)

    # Embed color: green if any role system is active
    any_enabled = role_system_enabled or coc_role_enabled or clan_role_enabled
    embed = discord.Embed(
        title=t('ui_components.role_configuration.title', guild_id=guild_id_int),
        color=discord.Color.green() if any_enabled else discord.Color.red()
    )

    # ── Member Families ──────────────────────────────────────────────────
    families_icon = BotEmojis.GCHECK if member_families else BotEmojis.REDX
    if member_families:
        family_lines = []
        for family_id in member_families:
            family_data = CACHE.clan_families.get(family_id, {})
            family_name = family_data.get("name", "Unknown")
            clan_count = len(family_data.get("clans", []))
            family_lines.append(f"🏰 {family_name} ({clan_count} clans)")
        families_display = "\n".join(family_lines)
    else:
        families_display = t('ui_components.role_configuration.no_families_configured', guild_id=guild_id_int)

    embed.add_field(
        name=f"{families_icon} {t('ui_components.role_configuration.member_families_title', guild_id=guild_id_int, count=len(member_families))}",
        value=families_display,
        inline=False
    )

    # ── Individual Member Clans ──────────────────────────────────────────
    clans_icon = BotEmojis.GCHECK if member_clans else BotEmojis.REDX
    if member_clans:
        clan_lines = []
        for clan_tag in member_clans:
            clan_name = CACHE.get_clan_name(clan_tag, "Unknown")
            clan_lines.append(f"• {clan_name} ({clan_tag})")
        clans_display = "\n".join(clan_lines)
    else:
        clans_display = t('ui_components.role_configuration.no_clans_configured', guild_id=guild_id_int)

    embed.add_field(
        name=f"⠀\n{clans_icon} {t('ui_components.role_configuration.member_clans_title', guild_id=guild_id_int, count=len(member_clans))}",
        value=clans_display,
        inline=False
    )

    # ── Role Assignment Mode ─────────────────────────────────────────────
    _mode_value_key = "assignment_mode_strict" if member_role_strict else "assignment_mode_simple"
    mode_lines = [
        t(f'ui_components.role_configuration.{_mode_value_key}', guild_id=guild_id_int),
        "",
        f"*{t('ui_components.role_configuration.assignment_mode_scope', guild_id=guild_id_int)}*",
    ]
    embed.add_field(
        name=f"⠀\n{t('ui_components.role_configuration.assignment_mode_section_title', guild_id=guild_id_int)}",
        value="\n".join(mode_lines),
        inline=False
    )

    # ── Auto-assign Simple Server Roles ──────────────────────────────────
    simple_status_emoji = "🟢" if role_system_enabled else "🔴"
    simple_status_text = (
        t('ui_components.basic_config.config_status_enabled', guild_id=guild_id_int)
        if role_system_enabled else
        t('ui_components.basic_config.config_status_disabled', guild_id=guild_id_int)
    )
    simple_lines = [
        f"**{t('ui_components.role_configuration.status_label', guild_id=guild_id_int)}:** {simple_status_emoji} {simple_status_text}",
        "",
    ]
    # Newbie role
    _member_desc_key = "member_role_desc_strict" if member_role_strict else "member_role_desc_simple"
    if newbie_role_id:
        try:
            newbie_role_obj = guild.get_role(int(newbie_role_id))
            if newbie_role_obj:
                simple_lines.append(f"{BotEmojis.GCHECK} **{t('ui_components.role_configuration.newbie_role_title', guild_id=guild_id_int)}** {newbie_role_obj.mention}")
                simple_lines.append(f"*{t('ui_components.role_configuration.newbie_role_desc', guild_id=guild_id_int)}*")
            else:
                simple_lines.append(f"⚠️ **{t('ui_components.role_configuration.newbie_role_title', guild_id=guild_id_int)}** {t('ui_components.role_configuration.role_not_found', guild_id=guild_id_int)}")
        except Exception:
            simple_lines.append(f"⚠️ **{t('ui_components.role_configuration.newbie_role_title', guild_id=guild_id_int)}** {t('ui_components.role_configuration.invalid_role_id', guild_id=guild_id_int)}")
    else:
        simple_lines.append(f"{BotEmojis.REDX} **{t('ui_components.role_configuration.newbie_role_title', guild_id=guild_id_int)}** {t('ui_components.role_configuration.not_configured', guild_id=guild_id_int)}")
    # Member role
    simple_lines.append("")
    if member_role_id:
        try:
            member_role_obj = guild.get_role(int(member_role_id))
            if member_role_obj:
                simple_lines.append(f"{BotEmojis.GCHECK} **{t('ui_components.role_configuration.member_role_title', guild_id=guild_id_int)}** {member_role_obj.mention}")
                simple_lines.append(f"*{t(f'ui_components.role_configuration.{_member_desc_key}', guild_id=guild_id_int)}*")
            else:
                simple_lines.append(f"⚠️ **{t('ui_components.role_configuration.member_role_title', guild_id=guild_id_int)}** {t('ui_components.role_configuration.role_not_found', guild_id=guild_id_int)}")
        except Exception:
            simple_lines.append(f"⚠️ **{t('ui_components.role_configuration.member_role_title', guild_id=guild_id_int)}** {t('ui_components.role_configuration.invalid_role_id', guild_id=guild_id_int)}")
    else:
        simple_lines.append(f"{BotEmojis.REDX} **{t('ui_components.role_configuration.member_role_title', guild_id=guild_id_int)}** {t('ui_components.role_configuration.not_configured', guild_id=guild_id_int)}")

    embed.add_field(
        name=f"⠀\n{t('ui_components.role_configuration.auto_assign_simple_title', guild_id=guild_id_int)}",
        value="\n".join(simple_lines),
        inline=False
    )

    # ── Auto-assign CoC In-Game Roles ─────────────────────────────────────
    coc_status_emoji = "🟢" if coc_role_enabled else "🔴"
    coc_status_text = (
        t('ui_components.role_configuration_buttons.coc_role_status_enabled', guild_id=guild_id_int)
        if coc_role_enabled else
        t('ui_components.role_configuration_buttons.coc_role_status_disabled', guild_id=guild_id_int)
    )
    coc_lines = [f"**{t('ui_components.role_configuration.status_label', guild_id=guild_id_int)}:** {coc_status_emoji} {coc_status_text}"]
    if coc_role_enabled:
        from qapbot.guild_role_manager import COC_ROLE_DISPLAY_NAMES, COC_ROLE_CONFIG_KEY
        for coc_key, config_key in COC_ROLE_CONFIG_KEY.items():
            role_id_str = config.get(config_key)
            display_name = COC_ROLE_DISPLAY_NAMES.get(coc_key, coc_key)
            if role_id_str:
                role_obj = guild.get_role(int(role_id_str))
                coc_lines.append(role_obj.mention if role_obj else f"⚠️ {display_name} (deleted)")
            else:
                coc_lines.append(f"⚠️ {display_name} (not created)")

    embed.add_field(
        name=f"⠀\n{t('ui_components.role_configuration_buttons.coc_role_display_title', guild_id=guild_id_int)}",
        value="\n".join(coc_lines),
        inline=False
    )

    # ── Auto-assign Clan Roles ────────────────────────────────────────────
    clan_status_emoji = "🟢" if clan_role_enabled else "🔴"
    clan_status_text = (
        t('ui_components.role_configuration_buttons.coc_role_status_enabled', guild_id=guild_id_int)
        if clan_role_enabled else
        t('ui_components.role_configuration_buttons.coc_role_status_disabled', guild_id=guild_id_int)
    )
    clan_lines = [f"**{t('ui_components.role_configuration.status_label', guild_id=guild_id_int)}:** {clan_status_emoji} {clan_status_text}"]
    if clan_role_enabled:
        clan_roles: Dict[str, str] = config.get("clan_roles", {})
        if clan_roles:
            for ctag, rid_str in clan_roles.items():
                clan_display = CACHE.get_clan_name(ctag, ctag)
                role_obj = guild.get_role(int(rid_str))
                clan_lines.append(f"• {clan_display}: {role_obj.mention if role_obj else '⚠️ deleted'}")
        else:
            clan_lines.append(t('ui_components.role_configuration_buttons.clan_role_no_roles', guild_id=guild_id_int))

    embed.add_field(
        name=f"⠀\n{t('ui_components.role_configuration_buttons.clan_role_display_title', guild_id=guild_id_int)}",
        value="\n".join(clan_lines),
        inline=False
    )

    return embed, None, [], []


async def _format_clan_management_families(guild: discord.Guild) -> Tuple[discord.Embed, Optional[discord.Embed], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Format clan management message for families mode.
    
    Shows only families attached to the current guild. Provides options to attach
    unattached families or detach existing families.
    
    Args:
        guild: Discord guild object
    
    Returns:
        Tuple of (main_embed, secondary_embed, linked_players, unlinked_players)
        Secondary embed and player lists are None/empty for families mode.
    """
    from qapbot.cache_manager import CACHE
    from qapbot.i18n import t
    
    guild_id = str(guild.id)
    guild_id_int = int(guild_id)
    _ = CACHE.server_config.get(guild_id, {})  # Config validation for future use
    
    embed = discord.Embed(
        title=t('ui_components.family_management.title', guild_id=guild_id_int),
        description=t('ui_components.family_management.description', guild_id=guild_id_int, guild_name=guild.name),
        color=discord.Color.blue()
    )
    
    # Get families owned by this guild
    guild_families = {}
    other_families = {}
    
    for family_id, family_data in CACHE.clan_families.items():
        if family_data.get("owned_by_guild") == guild_id:
            guild_families[family_id] = family_data
        else:
            other_families[family_id] = family_data
    
    if not guild_families:
        embed.add_field(
            name=t('ui_components.family_management.no_families_owned_title', guild_id=guild_id_int),
            value=t('ui_components.family_management.no_families_owned_desc', guild_id=guild_id_int),
            inline=False
        )
    else:
        # Build families list in format: • Family Name [#ID]
        families_lines = []
        for family_id, family_data in guild_families.items():
            family_name = family_data.get("name", "Unknown Family")
            families_lines.append(f"• {family_name} ({family_id})")
        
        families_display = "\n".join(families_lines)
        embed.add_field(
            name=t('ui_components.family_management.families_header', guild_id=guild_id_int, count=len(guild_families)),
            value=families_display,
            inline=False
        )
        
        # Display clan details for each family
        for family_id, family_data in guild_families.items():
            family_name = family_data.get("name", "Unknown Family")
            family_clans = family_data.get("clans", [])
            
            # Build clan list
            if family_clans:
                clan_lines = []
                for clan_tag in family_clans:
                    clan_name = CACHE.get_clan_name(clan_tag, "Unknown")
                    clan_lines.append(f"• {clan_name} ({clan_tag})")
                
                clans_display = "\n".join(clan_lines)
            else:
                clans_display = t('ui_components.family_management.no_clans_in_family', guild_id=guild_id_int)
            
            embed.add_field(
                name=f"⠀\n🏰 {t('ui_components.family_management.family_clans_header', guild_id=guild_id_int, family_name=family_name, count=len(family_clans))}",
                value=f"\n{clans_display}",
                inline=False
            )
    
    return embed, None, [], []


async def _format_clan_management_config(guild: discord.Guild) -> Tuple[discord.Embed, Optional[discord.Embed], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Format clan management message for basic configuration mode.
    
    Shows server language, welcome message block, and war notification channel block with integrated status.
    
    Args:
        guild: Discord guild object
    
    Returns:
        Tuple of (main_embed, secondary_embed, linked_players, unlinked_players)
        Secondary embed and player lists are None/empty for config mode.
    """
    from qapbot.cache_manager import CACHE
    from qapbot.i18n import t, get_guild_language
    
    guild_id_str = str(guild.id)
    guild_id_int = guild.id
    guild_config = CACHE.server_config.get(guild_id_str, {})
    
    # Get current settings
    registration_channel_id = guild_config.get("registration_channel_id")
    war_notification_channel_id = guild_config.get("war_notification_channel_id")
    registration_enabled = guild_config.get("registration_message_enabled", False)
    channel_notifications_enabled = guild_config.get("channel_war_notifications_enabled", False)
    welcome_enabled = guild_config.get("welcome_message_enabled", False)
    welcome_mode = guild_config.get("welcome_message_mode", "clan_link")
    welcome_apply_channel_id = guild_config.get("welcome_apply_channel_id")
    
    # Get current language
    current_language = get_guild_language(guild_id_int)
    language_display = "English" if current_language == "en" else "Deutsch" if current_language == "de" else current_language
    
    # Format language block
    language_block = f"⠀\n{t('ui_components.basic_config.config_language_block', guild_id=guild_id_int, language_name=language_display)}"
    
    # Format registration message block
    registration_status_emoji = "🟢" if registration_enabled else "🔴"
    if registration_channel_id:
        try:
            registration_channel = guild.get_channel(int(registration_channel_id))
            registration_channel_display = f"<#{registration_channel_id}>" if registration_channel else f"❌ {t('ui_components.basic_config.config_channel_not_set', guild_id=guild_id_int)}"
        except:
            registration_channel_display = f"❌ {t('ui_components.basic_config.config_channel_not_set', guild_id=guild_id_int)}"
    else:
        registration_channel_display = f"❌ {t('ui_components.basic_config.config_channel_not_set', guild_id=guild_id_int)}"
    
    registration_status_text = t('ui_components.basic_config.config_status_enabled', guild_id=guild_id_int) if registration_enabled else t('ui_components.basic_config.config_status_disabled', guild_id=guild_id_int)
    welcome_block = f"⠀\n**{t('ui_components.basic_config.config_registration_block_title', guild_id=guild_id_int)}**\n{t('ui_components.basic_config.config_registration_status', guild_id=guild_id_int, status=f'{registration_status_emoji} {registration_status_text}')}\n{t('ui_components.basic_config.config_registration_channel', guild_id=guild_id_int, channel=registration_channel_display)}"

    # Format welcome message block (independent join-greeting)
    welcome_msg_status_emoji = "🟢" if welcome_enabled else "🔴"
    welcome_msg_status_text = t('ui_components.basic_config.config_status_enabled', guild_id=guild_id_int) if welcome_enabled else t('ui_components.basic_config.config_status_disabled', guild_id=guild_id_int)
    if welcome_mode == "apply_channel":
        mode_label = t('ui_components.basic_config.config_welcome_mode_apply_channel', guild_id=guild_id_int)
        if welcome_apply_channel_id:
            try:
                apply_channel = guild.get_channel(int(welcome_apply_channel_id))
                apply_channel_display = f"<#{welcome_apply_channel_id}>" if apply_channel else f"❌ {t('ui_components.basic_config.config_channel_not_set', guild_id=guild_id_int)}"
            except Exception:
                apply_channel_display = f"❌ {t('ui_components.basic_config.config_channel_not_set', guild_id=guild_id_int)}"
        else:
            apply_channel_display = f"❌ {t('ui_components.basic_config.config_channel_not_set', guild_id=guild_id_int)}"
        mode_detail = f"\n{t('ui_components.basic_config.config_welcome_apply_channel', guild_id=guild_id_int, channel=apply_channel_display)}"
    else:
        mode_label = t('ui_components.basic_config.config_welcome_mode_clan_link', guild_id=guild_id_int)
        welcome_clan_tags = guild_config.get("welcome_clan_tags", [])
        welcome_family_tags = guild_config.get("welcome_family_tags", [])
        selection_lines = []
        for family_id in welcome_family_tags:
            family_data = CACHE.clan_families.get(family_id, {})
            family_name = family_data.get("name", family_id)
            clan_count = len(family_data.get("clans", []))
            selection_lines.append(f"🏰 {family_name} ({clan_count} clans)")
        for clan_tag in welcome_clan_tags:
            clan_data = CACHE.clan_name_cache.get(clan_tag, {})
            clan_name = clan_data.get("name", clan_tag) if clan_data else clan_tag
            selection_lines.append(f"• {clan_name}")
        if selection_lines:
            clan_display = "\n".join(selection_lines)
        else:
            clan_display = f"❌ {t('ui_components.basic_config.config_channel_not_set', guild_id=guild_id_int)}"
        mode_detail = f"\n{t('ui_components.basic_config.config_welcome_clan', guild_id=guild_id_int, clan=clan_display)}"
    welcome_msg_block = (
        f"⠀\n**{t('ui_components.basic_config.config_welcome_block_title', guild_id=guild_id_int)}**\n"
        f"{t('ui_components.basic_config.config_welcome_status', guild_id=guild_id_int, status=f'{welcome_msg_status_emoji} {welcome_msg_status_text}')}\n"
        f"{t('ui_components.basic_config.config_welcome_mode', guild_id=guild_id_int, mode=mode_label)}"
        f"{mode_detail}"
    )
    
    # Format war notification block
    war_status_emoji = "🟢" if channel_notifications_enabled else "🔴"
    if war_notification_channel_id:
        try:
            war_channel = guild.get_channel(int(war_notification_channel_id))
            war_channel_display = f"<#{war_notification_channel_id}>" if war_channel else f"❌ {t('ui_components.basic_config.config_channel_not_set', guild_id=guild_id_int)}"
        except:
            war_channel_display = f"❌ {t('ui_components.basic_config.config_channel_not_set', guild_id=guild_id_int)}"
    else:
        war_channel_display = f"❌ {t('ui_components.basic_config.config_channel_not_set', guild_id=guild_id_int)}"
    
    # Get notification threshold
    threshold_hours = guild_config.get("war_notification_threshold_hours", 1.0)
    if threshold_hours == 0.5:
        threshold_display = f"30 {t('ui_components.basic_config.config_time_minutes', guild_id=guild_id_int)}"
    elif threshold_hours == 1.0:
        threshold_display = f"1 {t('ui_components.basic_config.config_time_hour', guild_id=guild_id_int)}"
    elif threshold_hours == 2.0:
        threshold_display = f"2 {t('ui_components.basic_config.config_time_hours', guild_id=guild_id_int)}"
    elif threshold_hours == 4.0:
        threshold_display = f"4 {t('ui_components.basic_config.config_time_hours', guild_id=guild_id_int)}"
    else:
        if threshold_hours < 1:
            threshold_display = f"{int(threshold_hours * 60)} {t('ui_components.basic_config.config_time_minutes', guild_id=guild_id_int)}"
        else:
            threshold_display = f"{threshold_hours:.1f} {t('ui_components.basic_config.config_time_hours', guild_id=guild_id_int)}"
    
    war_status_text = t('ui_components.basic_config.config_status_enabled', guild_id=guild_id_int) if channel_notifications_enabled else t('ui_components.basic_config.config_status_disabled', guild_id=guild_id_int)
    war_block = f"⠀\n**{t('ui_components.basic_config.config_war_block_title', guild_id=guild_id_int)}**\n{t('ui_components.basic_config.config_war_status', guild_id=guild_id_int, status=f'{war_status_emoji} {war_status_text}')}\n{t('ui_components.basic_config.config_war_channel', guild_id=guild_id_int, channel=war_channel_display)}\n{t('ui_components.basic_config.config_war_threshold', guild_id=guild_id_int, threshold=threshold_display)}"
    
    # Format member families block
    member_families = guild_config.get("member_families", [])
    families_display_lines = []
    if member_families:
        families_display_lines.append(f"⠀\n{BotEmojis.GCHECK} {t('ui_components.basic_config.config_member_families', guild_id=guild_id_int, count=len(member_families))}")
        for family_id in member_families:
            family_data = CACHE.clan_families.get(family_id, {})
            family_name = family_data.get("name", "Unknown")
            clan_count = len(family_data.get("clans", []))
            families_display_lines.append(f"🏰 {family_name} {t('ui_components.basic_config.config_clans_in_family', guild_id=guild_id_int, count=clan_count)}")
    else:
        families_display_lines.append(f"{BotEmojis.REDX} {t('ui_components.basic_config.config_member_families', guild_id=guild_id_int, count=0)}")
    
    # Format member clans block
    member_clans = guild_config.get("member_clans", [])
    clans_display_lines = []
    if member_clans:
        clans_display_lines.append(f"⠀\n{BotEmojis.GCHECK} {t('ui_components.basic_config.config_individual_clans', guild_id=guild_id_int, count=len(member_clans))}")
        for clan_tag in member_clans:
            clan_name = CACHE.get_clan_name(clan_tag, "Unknown")
            clans_display_lines.append(f"• {clan_name} ({clan_tag})")
    else:
        clans_display_lines.append(f"{BotEmojis.REDX} {t('ui_components.basic_config.config_individual_clans', guild_id=guild_id_int, count=0)}")
    
    member_config_block = "\n".join(families_display_lines) + "\n\n" + "\n".join(clans_display_lines)
    
    # Create embed
    embed = discord.Embed(
        title=t('ui_components.basic_config.title', guild_id=guild_id_int),
        description=t('ui_components.basic_config.config_description', guild_id=guild_id_int, guild_name=guild.name),
        color=discord.Color.blue()
    )
    
    # Add blocks
    embed.add_field(
        name="",
        value=language_block,
        inline=False
    )
    
    embed.add_field(
        name="",
        value=welcome_block,
        inline=False
    )

    embed.add_field(
        name="",
        value=welcome_msg_block,
        inline=False
    )
    
    embed.add_field(
        name="",
        value=war_block,
        inline=False
    )
    
    embed.add_field(
        name="",
        value=member_config_block,
        inline=False
    )
    
    #embed.set_footer(text="Use buttons below to configure settings")
    
    return embed, None, [], []
