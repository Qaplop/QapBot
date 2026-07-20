"""
ClashPerk User List Import - Bot Library

Function library for importing player accounts from ClashPerk clan management embeds.
Used by /import_data Discord bot command.

Key Functions:
    - parse_clashperk_embed(): Parse ClashPerk embed and match players to Discord users
    - parse_clashperk_embed_with_tag_data(): Parse "Sorted by Tag" view with cached Discord usernames
    - analyze_import_changes(): Preview what will change before importing
    - apply_import_changes(): Apply import changes to user accounts
    
Exception Classes:
    - DuplicatePlayerNamesError: Raised when duplicate player names detected in embed
"""
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false
import discord
import re
import logging
from typing import Tuple, Dict, List, Optional, Any, Set
from qapbot.cache_manager import CACHE


async def get_clan_members_from_client(clan_tag: str) -> Dict[str, str]:
    """
    Get clan members using the CoC API client from CACHE (for bot command use).
    
    Args:
        clan_tag: Clan tag to fetch members for
        
    Returns:
        Dict mapping player_tag -> player_name
    """
    try:
        # Get clan members (via cache)
        clan = await CACHE.coc_clan_cache.get_clan(clan_tag)
        
        # Build member dict
        members = {}
        for member in clan.members:
            members[member.tag] = member.name
        
        return members
    except Exception as e:
        raise Exception(f"Failed to fetch clan members: {e}")


class DuplicatePlayerNamesError(Exception):
    """Raised when duplicate player names are detected in 'Sorted by Name' view."""
    def __init__(self, duplicate_names: List[str], discord_usernames: Optional[Dict[str, Tuple[str, bool]]] = None, clan_tag: Optional[str] = None, clan_name: Optional[str] = None):
        self.duplicate_names = duplicate_names
        self.discord_usernames = discord_usernames or {}
        self.clan_tag = clan_tag
        self.clan_name = clan_name
        super().__init__(f"Duplicate player names detected: {', '.join(duplicate_names)}")


def update_user_accounts_with_results(user_accounts: Dict[str, Any], results: Dict[str, Any]) -> Tuple[Dict[str, Any], int, int, int, Set[str]]:
    """
    Update user accounts with new players from ClashPerk results.
    Smart overwrite logic: only upgrade verified status (False -> True), never downgrade.
    
    Args:
        user_accounts: Existing user accounts dict
        results: Results from parse_clashperk_embed (includes verified status)
        
    Returns:
        Tuple of (updated_user_accounts, added_count, upgraded_count, skipped_count, changed_user_ids)
    """
    added_count = 0
    upgraded_count = 0
    skipped_count = 0
    changed_user_ids: Set[str] = set()
    
    for _key, result in results.items():
        _, user_id, _, display_name, coc_name, player_tag, verified = result
        
        # Skip if no Discord user found
        if not user_id:
            continue
        
        user_id_str = str(user_id)
        
        # Initialize user entry if it doesn't exist
        if user_id_str not in user_accounts:
            user_accounts[user_id_str] = {
                "display_name": display_name,
                "notification_settings": {},
                "players": []
            }
        
        # Check if player_tag already exists for this user
        existing_player = None
        for player in user_accounts[user_id_str]['players']:
            if player.get('player_tag', '') == player_tag:
                existing_player = player
                break
        
        if existing_player:
            # Player exists - check if we should upgrade verified status
            current_verified = existing_player.get('verified', False)
            
            if verified and not current_verified:
                # Upgrade: False -> True
                existing_player['verified'] = True
                upgraded_count += 1
                changed_user_ids.add(user_id_str)
            else:
                # No change needed (already verified or import has lower status)
                skipped_count += 1
        else:
            # Add new player with verified status from ClashPerk
            user_accounts[user_id_str]['players'].append({
                "player_tag": player_tag,  # Database uses player_tag field
                "player_name": coc_name,
                "verified": verified
            })
            added_count += 1
            changed_user_ids.add(user_id_str)
    
    return user_accounts, added_count, upgraded_count, skipped_count, changed_user_ids


def parse_embed_with_coc_data(embed_description: str, coc_members: Dict[str, str]) -> Tuple[Dict[str, Tuple[str, bool]], List[str]]:
    """
    Parse Discord usernames from embed using CoC player names as anchors.
    Also detect verified status based on emoji ID.
    
    Strategy:
    1. For each line in the embed, extract the content between backticks
    2. Detect if line contains :Verified: emoji (ID 803884634768408577)
    3. Skip lines that contain player tags (unlinked players)
    4. Search for each CoC player name in that content
    5. When found, extract the text that comes after the player name (that's the Discord username)
    
    Args:
        embed_description: The description field from the embed
        coc_members: Dict mapping player_tag -> player_name from CoC API
        
    Returns:
        Tuple of (results_dict, duplicate_names) where:
            - results_dict: Dict mapping coc_player_name -> (discord_username, verified_status)
            - duplicate_names: List of player names that appeared multiple times in the embed
    """
    lines = embed_description.split('\n')
    results: Dict[str, Tuple[str, bool]] = {}
    duplicate_names: List[str] = []
    in_unlinked_section = False
    skipped_lines: List[str] = []
    
    for line in lines:
        # Check if we've entered the "Players not Linked" section
        if '**Players not Linked' in line:
            in_unlinked_section = True
            continue
        
        # Skip lines in the unlinked section
        if in_unlinked_section:
            continue
        
        # Skip header lines and empty lines
        if not line.strip() or '**' in line:
            continue
        
        # Check if line has :Verified: emoji (ID 803884634768408577)
        verified = ':803884634768408577>' in line  # Discord format: <:EmojiName:EmojiID>
        
        # Extract content between backticks
        match = re.search(r'`([^`]+)`', line)
        if not match:
            continue
        
        content = match.group(1)
        # Remove unicode direction marks for easier processing
        content = content.replace('\u200e', '').replace('\u200f', '').strip()
        
        # Normalize apostrophes and quotes for better matching
        # Replace various apostrophe/quote characters with standard ASCII apostrophe
        apostrophe_chars = ['\u2019', '\u2018', '\u201A', '`', '\u2032', '\u00B4', '\u02BC', '\u02BB', '\u02BD', '\u02BE', '\u02CA', '\u02CB']
        for char in apostrophe_chars:
            content = content.replace(char, "'")
        
        # Skip lines that contain player tags (pattern: #XXXXXXXXX with uppercase letters/numbers)
        if re.search(r'#[A-Z0-9]{7,10}\b', content):
            skipped_lines.append(f"PLAYER_TAG: {content}")
            continue
        
        # Find the LEFTMOST (earliest position) CoC player name in this line
        # This handles cases where multiple CoC names appear in one line
        # Sort by name length (longest first) to avoid substring matches (e.g., "Tify3" before "Tify")
        earliest_pos = len(content)
        earliest_player_name = None
        
        # Sort player names by length (longest first) to match longer names before shorter substrings
        sorted_members = sorted(coc_members.items(), key=lambda x: len(x[1]), reverse=True)  # type: ignore[misc]
        
        earliest_normalized_name: Optional[str] = None  # Initialize before loop
        for _, player_name in sorted_members:
            # Normalize apostrophes in player name for matching
            normalized_player_name = player_name
            apostrophe_chars = ['\u2019', '\u2018', '\u201A', '`', '\u2032', '\u00B4', '\u02BC', '\u02BB', '\u02BD', '\u02BE', '\u02CA', '\u02CB']
            for char in apostrophe_chars:
                normalized_player_name = normalized_player_name.replace(char, "'")
            
            pos = content.find(normalized_player_name)
            if pos != -1 and pos < earliest_pos:
                earliest_pos = pos
                earliest_player_name = player_name  # Keep original name for result dict
                earliest_normalized_name = normalized_player_name
        
        # If we found a CoC player name, extract the Discord username after it
        if earliest_player_name and earliest_normalized_name:
            # Use normalized name length for extraction
            after_name = content[earliest_pos + len(earliest_normalized_name):].strip()
            
            if after_name:
                discord_username = after_name
                
                # Check if this player name already exists (duplicate detection)
                if earliest_player_name in results:
                    # Duplicate player name found - we can't reliably match in "Sorted by Name" view
                    # Track this as a duplicate if not already tracked
                    if earliest_player_name not in duplicate_names:
                        duplicate_names.append(earliest_player_name)
                else:
                    results[earliest_player_name] = (discord_username, verified)
                
                matched = True
            else:
                matched = False
        else:
            matched = False
        
        # If no CoC player name matched, this line was skipped
        if not matched:
            skipped_lines.append(f"NO_MATCH: {content}")
    
    # Log skipped lines for debugging
    if skipped_lines:
        logging.debug(f"Skipped {len(skipped_lines)} lines during parsing")
        for line in skipped_lines:
            logging.debug(f"  {line}")
    
    return results, duplicate_names


def parse_sorted_by_tag_embed(embed_description: str) -> Dict[str, Tuple[str, str, bool]]:
    """
    Parse player tags from 'Sorted by Tag' view.
    
    Args:
        embed_description: The description field from the embed
        
    Returns:
        Dict mapping player_tag -> (player_name, player_tag, verified_status)
    """
    lines = embed_description.split('\n')
    results: Dict[str, Tuple[str, str, bool]] = {}
    in_unlinked_section = False
    skipped_lines: List[str] = []
    
    # Count total players for logging
    total_player_count = 0
    for line in lines:
        if '**' in line and 'Players' in line:
            # Try to extract player count from lines like "**Players Linked: 42**"
            count_match = re.search(r'(\d+)', line)
            if count_match and 'Linked' in line and 'not' not in line.lower():
                total_player_count = int(count_match.group(1))
    
    if total_player_count > 0:
        logging.info(f"ClashPerk reports {total_player_count} players in server")
    
    for line_num, line in enumerate(lines, 1):
        # Check for section boundaries
        if '**Players not Linked' in line:
            in_unlinked_section = True
            logging.info(f"Entering unlinked section at line {line_num}: {line.strip()}")
            continue
        
        if '**Players not in the Server' in line:
            in_unlinked_section = True
            logging.info(f"Entering unlinked section at line {line_num}: {line.strip()}")
            continue
        
        if in_unlinked_section:
            continue
        
        # Skip header lines and empty lines
        if not line.strip() or '**' in line:
            continue
        
        # Check if line has :Verified: emoji
        verified = ':803884634768408577>' in line
        
        # Extract content between backticks
        match = re.search(r'`([^`]+)`', line)
        if not match:
            continue
        
        content = match.group(1)
        content = content.replace('\u200e', '').replace('\u200f', '').strip()
        
        # Extract player tag (format: #XXXXXXXXX)
        tag_match = re.search(r'#([A-Z0-9]{7,10})\b', content)
        if not tag_match:
            skipped_lines.append(f"NO_TAG: {content}")
            continue
        
        player_tag = f"#{tag_match.group(1)}"
        
        # Extract player name (everything before the tag)
        player_name = content[:content.find('#')].strip()
        
        # Strip Town Hall level prefix (format: "TH_NUMBER     PLAYER_NAME")
        # The TH level appears as 1-2 digits at the start, followed by whitespace
        th_prefix_match = re.match(r'^(\d{1,2})\s+(.+)$', player_name)
        if th_prefix_match:
            player_name = th_prefix_match.group(2).strip()
        
        if player_name:
            results[player_tag] = (player_name, player_tag, verified)
    
    # Log results and skipped lines
    logging.info(f"Tag view parsing complete: Found {len(results)} players")
    if skipped_lines:
        logging.warning(f"Skipped {len(skipped_lines)} lines during tag view parsing:")
        for skip_msg in skipped_lines:
            logging.warning(f"  {skip_msg}")
    
    return results


async def parse_clashperk_embed(
    guild: discord.Guild,
    channel_id: int,
    message_id: int
) -> Tuple[Dict[str, Any], str, str, int, int]:
    """
    Parse a ClashPerk clan management embed and match players to Discord users.
    This is the main function used by the /import_data command.
    
    Args:
        guild: Discord guild where message is located
        channel_id: Channel ID containing the message
        message_id: Message ID of the embed
        
    Returns:
        Tuple of (results_dict, clan_tag, clan_name, verified_count, total_count) where:
            - results_dict: Dict mapping key (player_name or player_tag) -> (discord_username, user_id, username, display_name, coc_player_name, player_tag, verified)
            - clan_tag: Detected clan tag from embed
            - clan_name: Clan name from CoC API
            - verified_count: Number of verified accounts detected
            - total_count: Total number of players found
            
    Raises:
        Exception: If message/channel not found or parsing fails
        DuplicatePlayerNamesError: If duplicate player names detected in "Sorted by Name" view
    """
    # Fetch the message
    channel = guild.get_channel(channel_id)
    if not channel or not isinstance(channel, discord.TextChannel):
        raise Exception(f"Channel {channel_id} not found or not a text channel")
    
    message = await channel.fetch_message(message_id)
    if not message:
        raise Exception(f"Message {message_id} not found in channel {channel_id}")
    
    if not message.embeds or len(message.embeds) == 0:
        raise Exception(f"Message {message_id} does not contain any embeds")
    
    # Get the first embed
    embed = message.embeds[0]
    
    # Extract clan tag from embed author name
    clan_tag = None
    if embed.author and embed.author.name:
        match = re.search(r'\(#([A-Z0-9]+)\)', embed.author.name)
        if match:
            clan_tag = f"#{match.group(1)}"
    
    if not clan_tag:
        raise Exception("Could not detect clan tag from embed author field. Expected format: 'ClanName (#TAG)'")
    
    if not embed.description:
        raise Exception("Embed has no description field")
    
    # Check footer to determine view type
    footer_text = embed.footer.text if embed.footer else ""
    if not footer_text:
        raise Exception("Embed has no footer text")
    
    is_sorted_by_tag = "Sorted by Tag" in footer_text
    is_sorted_by_name = "Sorted by Name" in footer_text
    
    if not is_sorted_by_tag and not is_sorted_by_name:
        raise Exception("Could not detect ClashPerk view type. Footer must contain 'Sorted by Name'.")
    
    # Get clan members from CoC API
    try:
        coc_members = await get_clan_members_from_client(clan_tag)
        clan = await CACHE.coc_clan_cache.get_clan(clan_tag)
        clan_name = clan.name
    except Exception as e:
        raise Exception(f"Failed to fetch clan data from CoC API for {clan_tag}. The clan may not exist or API is unavailable. Error: {str(e)}")
    
    if is_sorted_by_tag:
        # Parse "Sorted by Tag" view - get player_tag -> (player_name, player_tag, verified)
        tag_data = parse_sorted_by_tag_embed(embed.description)
        
        if not tag_data:
            raise Exception("No players found in embed. The embed format may not match ClashPerk's clan management format.")
        
        # This view doesn't have Discord usernames, so we need to have been called with prior data
        # For now, just return what we can extract
        # This will be enhanced later to combine with "Sorted by Name" data
        verified_count = sum(1 for _, _, verified in tag_data.values() if verified)
        total_count = len(tag_data)
        
        # We can't match Discord users without usernames, so return empty results
        # This signals the calling code that we need the other view
        raise Exception("'Sorted by Tag' view detected, but this view doesn't contain Discord usernames. Please use 'Sorted by Name' view first.")
    
    # Parse "Sorted by Name" view - get player_name -> (discord_username, verified), and detect duplicates
    discord_usernames, duplicate_names = parse_embed_with_coc_data(embed.description, coc_members)
    
    if not discord_usernames:
        raise Exception("No players found in embed. The embed format may not match ClashPerk's clan management format.")
    
    # Check for duplicates detected during parsing
    if duplicate_names and is_sorted_by_name:
        # Can't reliably match duplicates in "Sorted by Name" view
        # But we have the discord usernames, so include them in the exception
        raise DuplicatePlayerNamesError(duplicate_names, discord_usernames, clan_tag, clan_name)
    
    verified_count = sum(1 for _, verified in discord_usernames.values() if verified)
    total_count = len(discord_usernames)
    
    # Now search Discord for these usernames
    results: Dict[str, Any] = {}
    
    for coc_player_name, (discord_username, verified) in discord_usernames.items():
        # Find the player tag for this CoC player
        player_tag = next((tag for tag, name in coc_members.items() if name == coc_player_name), None)
        
        found = False
        discord_username_lower = discord_username.lower()
        
        # Try exact match first
        for member in guild.members:
            if member.display_name.lower() == discord_username_lower:
                results[coc_player_name] = (discord_username, member.id, member.name, member.display_name, coc_player_name, player_tag, verified)
                found = True
                break
        
        # Try substring match if exact didn't work
        if not found:
            for member in guild.members:
                if discord_username_lower in member.display_name.lower():
                    results[coc_player_name] = (discord_username, member.id, member.name, member.display_name, coc_player_name, player_tag, verified)
                    found = True
                    break
        
        if not found:
            results[coc_player_name] = (discord_username, None, None, None, coc_player_name, player_tag, verified)
    
    return results, clan_tag, clan_name, verified_count, total_count


async def parse_clashperk_embed_with_tag_data(
    guild: discord.Guild,
    channel_id: int,
    message_id: int,
    cached_discord_usernames: Optional[Dict[str, Tuple[str, bool]]] = None
) -> Tuple[Dict[str, Any], str, str, int, int]:
    """
    Parse ClashPerk embed when user has switched to 'Sorted by Tag' view.
    This function combines tag data from the current view with Discord username data
    that should have been cached from the previous 'Sorted by Name' parse attempt.
    
    Args:
        guild: Discord guild where message is located
        channel_id: Channel ID containing the message
        message_id: Message ID of the embed
        cached_discord_usernames: Optional cached data from previous parse (player_name -> (discord_username, verified))
        
    Returns:
        Tuple of (results_dict, clan_tag, clan_name, verified_count, total_count) where:
            - results_dict: Dict with player_tag as key -> (discord_username, user_id, username, display_name, coc_player_name, player_tag, verified)
            - Other values same as parse_clashperk_embed
        
    Raises:
        Exception: If message format is wrong or still shows 'Sorted by Name'
    """
    # Fetch the message
    channel = guild.get_channel(channel_id)
    if not channel or not isinstance(channel, discord.TextChannel):
        raise Exception(f"Channel {channel_id} not found or not a text channel")
    
    message = await channel.fetch_message(message_id)
    if not message or not message.embeds:
        raise Exception("Message or embed not found")
    
    embed = message.embeds[0]
    
    # Check footer - must be "Sorted by Tag" now
    footer_text = embed.footer.text if embed.footer else ""
    if not footer_text:
        raise Exception("Embed has no footer text")
    if "Sorted by Tag" not in footer_text:
        raise Exception("Embed is not showing 'Sorted by Tag' view. Please switch the view using ClashPerk's reactions and try again.")
    
    # Extract clan info
    clan_tag = None
    if embed.author and embed.author.name:
        match = re.search(r'\(#([A-Z0-9]+)\)', embed.author.name)
        if match:
            clan_tag = f"#{match.group(1)}"
    
    if not clan_tag:
        raise Exception("Could not detect clan tag from embed author field")
    
    # Get clan info (via cache)
    clan = await CACHE.coc_clan_cache.get_clan(clan_tag)
    clan_name = clan.name
    
    # Ensure embed.description is not None
    if not embed.description:
        raise Exception("Embed has no description field")
    
    # Parse tag data
    tag_data = parse_sorted_by_tag_embed(embed.description)
    
    if not tag_data:
        raise Exception("No player tags found in 'Sorted by Tag' view")
    
    # If we don't have cached discord usernames, we need to fetch them from the original view
    # For now, we'll try to match what we can
    # In practice, we need the calling code to store the discord_usernames from the first parse
    
    # DEBUG: Log what we have in tag_data vs cached_discord_usernames
    logging.warning("=== PARSE TAG DATA DEBUG ===")
    tag_data_player_names = [name for name, _, _ in tag_data.values()]
    logging.warning(f"Tag data entries ({len(tag_data)}): {len(tag_data)} player tags")
    logging.warning(f"Tag data player names: {sorted(set(tag_data_player_names))}")
    if cached_discord_usernames:
        logging.warning(f"Cached username player names ({len(cached_discord_usernames)}): {sorted(cached_discord_usernames.keys())}")
        # Check for mismatches
        tag_names = set(tag_data_player_names)
        cached_names = set(cached_discord_usernames.keys())
        only_in_tag = tag_names - cached_names
        only_in_cached = cached_names - tag_names
        if only_in_tag:
            logging.warning(f"Names ONLY in tag data: {sorted(only_in_tag)}")
        if only_in_cached:
            logging.warning(f"Names ONLY in cached data: {sorted(only_in_cached)}")
    else:
        logging.warning("No cached discord usernames provided!")
    
    # Match Discord users
    results: Dict[str, Any] = {}
    verified_count = 0
    
    # Now tag_data is Dict[player_tag -> (player_name, player_tag, verified)]
    for player_tag, (player_name, _, verified) in tag_data.items():
        if verified:
            verified_count += 1
        
        # Try to get Discord username from cache or skip
        discord_username = None
        if cached_discord_usernames and player_name in cached_discord_usernames:
            discord_username, _ = cached_discord_usernames[player_name]
        
        if not discord_username:
            # No Discord username available, skip this player
            results[player_tag] = (None, None, None, None, player_name, player_tag, verified)
            continue
        
        # Search for Discord user
        found = False
        discord_username_lower = discord_username.lower()
        
        for member in guild.members:
            if member.display_name.lower() == discord_username_lower:
                results[player_tag] = (discord_username, member.id, member.name, member.display_name, player_name, player_tag, verified)
                found = True
                break
        
        if not found:
            for member in guild.members:
                if discord_username_lower in member.display_name.lower():
                    results[player_tag] = (discord_username, member.id, member.name, member.display_name, player_name, player_tag, verified)
                    found = True
                    break
        
        if not found:
            results[player_tag] = (discord_username, None, None, None, player_name, player_tag, verified)
    
    total_count = len(tag_data)
    
    return results, clan_tag, clan_name, verified_count, total_count


def analyze_import_changes(user_accounts: Dict[str, Any], results: Dict[str, Any]) -> Tuple[List[Tuple[str, str, str, bool]], List[Tuple[str, str, str]], List[Tuple[str, str, str, bool]]]:
    """
    Analyze what changes will be made if results are imported.
    Used for preview before confirmation.
    
    Args:
        user_accounts: Existing user accounts dict
        results: Results from parse_clashperk_embed
        
    Returns:
        Tuple of (to_add, to_upgrade, to_skip) where each is a list of tuples:
            - to_add: [(coc_name, player_tag, discord_display_name, verified), ...]
            - to_upgrade: [(coc_name, player_tag, discord_display_name), ...]
            - to_skip: [(coc_name, player_tag, discord_display_name, verified), ...]
    """
    to_add: List[Tuple[str, str, str, bool]] = []
    to_upgrade: List[Tuple[str, str, str]] = []
    to_skip: List[Tuple[str, str, str, bool]] = []
    
    for _key, result in results.items():
        _, user_id, _, display_name, coc_name, player_tag, verified = result
        
        # Skip if no Discord user found
        if not user_id:
            continue
        
        user_id_str = str(user_id)
        
        # Check if user exists
        if user_id_str not in user_accounts:
            # New user, will be added
            to_add.append((coc_name, player_tag, display_name, verified))
            continue
        
        # Check if player_tag already exists for this user
        existing_player = None
        for player in user_accounts[user_id_str]['players']:
            current_tag = player.get('player_tag', '')
            if current_tag == player_tag:
                existing_player = player
                break
        
        if existing_player:
            # Player exists - check if we should upgrade verified status
            current_verified = existing_player.get('verified', False)
            
            if verified and not current_verified:
                # Will upgrade: False -> True
                to_upgrade.append((coc_name, player_tag, display_name))
            else:
                # No change needed
                to_skip.append((coc_name, player_tag, display_name, current_verified))
        else:
            # New player for existing user
            to_add.append((coc_name, player_tag, display_name, verified))
    
    return to_add, to_upgrade, to_skip


def apply_import_changes(user_accounts: Dict[str, Any], results: Dict[str, Any]) -> Tuple[Dict[str, Any], int, int, int, Set[str]]:
    """
    Apply the import changes to user accounts.
    This is the same logic as update_user_accounts_with_results but returns simpler stats.
    
    Args:
        user_accounts: Existing user accounts dict (will be modified)
        results: Results from parse_clashperk_embed
        
    Returns:
        Tuple of (updated_user_accounts, added_count, upgraded_count, skipped_count, changed_user_ids)
    """
    return update_user_accounts_with_results(user_accounts, results)
