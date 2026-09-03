"""
Helper functions for leaderboard generation, war info updates, Discord posting, and cache-centric message management for QapBot.

This module provides the core business logic for QapBot's primary functions:

Core Functions:
- Leaderboard generation with various modes (attack, avgstars, attackdefratio, etc.)
- War information updates with real-time statistics and predictions
- Discord posting with unified message ID tracking and spam prevention
- Historical data processing and player name change handling

Key Features:
- Multi-month aggregation for year-to-date leaderboards with proper war deduplication
- Intelligent message splitting for large leaderboards exceeding Discord limits
- Content normalization and hash-based change detection to prevent unnecessary posts
- Robust error handling and defensive programming for API failures
- All cache operations are atomic and synchronized with persistent storage

Business Rules:
- All leaderboard and war info posts use unified message ID tracking in CACHE.leaderboard_messages
- All data access and persistence is routed through the cache manager for single-source-of-truth consistency
- Defensive programming for external API dependencies and Discord rate limits

Integration:
- Used by Discord command handlers for leaderboard, war info, and message management
- Interacts with cache_manager for all runtime data and message tracking
- Supports both Discord and terminal output styles
"""
import re
import asyncio
import logging
import os
import time
import coc  # type: ignore[import-untyped]
import discord
from datetime import datetime, timedelta, timezone as _tz
from typing import List, Dict, Any, Optional, Tuple, Union, Set, cast
from collections import defaultdict
import hashlib
from qapbot.cache_manager import CACHE
from qapbot.config import CONFIG
from qapbot.formatting import MODE_REGISTRY, DEFAULT_MODE  # type: ignore[attr-defined]
from qapbot.constants import (
    DISCORD_MESSAGE_MAX_LENGTH,
    PASSIVE_CLAN_REFRESH_INTERVAL_DAYS,
    SECONDS_PER_HOUR,
    SECONDS_PER_MINUTE,
    WAR_UPDATE_LEAGUES,
)
from qapbot.exceptions import (
    WarProcessingError,
    WarDataFetchError,
    LeaderboardPostingError
)
from qapbot.formatting import render_leaderboard, normalize_player_name, text_display_width, best_practice_player_cell
from qapbot.discord_health import discord_retry

# Sentinel markers used to pass plain-text (emoji) sections through the leaderboard
# text pipeline without them ending up inside ``` code blocks, where Discord will not
# render custom emojis.
_PLAIN_SENTINEL_START = "\x00PLAIN_S\x00"
_PLAIN_SENTINEL_END   = "\x00PLAIN_E\x00"

# ── Clash of Clans font paths (used by generate_cwl_group_image) ──────────────
_CLASH_FONTS_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qapbot", "fonts")
_CLASH_REGULAR_PATH = os.path.join(_CLASH_FONTS_DIR, "Clash_Regular.otf")
_CLASH_BOLD_PATH    = os.path.join(_CLASH_FONTS_DIR, "Clash_Bold.otf")
_clash_fonts_registered: bool = False

# ── CWL image asset paths (used by generate_cwl_group_image) ─────────────────
_GAMEASSETS_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qapbot", "gameassets")
_MEDAL_ICON_PATH = os.path.join(_GAMEASSETS_DIR, "Icon_League_Medal.png")

# Maps CoC API league_name → gameassets PNG filename.
# Leagues without an available icon (Titan I/II/III, Legend League) are omitted.
_LEAGUE_ICON_MAP: Dict[str, str] = {
    "Bronze League III":   "Icon_HV_CWL_Bronze_3.png",
    "Bronze League II":    "Icon_HV_CWL_Bronze_2.png",
    "Bronze League I":     "Icon_HV_CWL_Bronze_1.png",
    "Silver League III":   "Icon_HV_CWL_Silver_3.png",
    "Silver League II":    "Icon_HV_CWL_Silver_2.png",
    "Silver League I":     "Icon_HV_CWL_Silver_1.png",
    "Gold League III":     "Icon_HV_CWL_Gold_3.png",
    "Gold League II":      "Icon_HV_CWL_Gold_2.png",
    "Gold League I":       "Icon_HV_CWL_Gold_1.png",
    "Crystal League III":  "Icon_HV_CWL_Crystal_3.png",
    "Crystal League II":   "Icon_HV_CWL_Crystal_2.png",
    "Crystal League I":    "Icon_HV_CWL_Crystal_1.png",
    "Master League III":   "Icon_HV_CWL_Master_3.png",
    "Master League II":    "Icon_HV_CWL_Master_2.png",
    "Master League I":     "Icon_HV_CWL_Master_1.png",
    "Champion League III": "Icon_HV_CWL_Champion_3.png",
    "Champion League II":  "Icon_HV_CWL_Champion_2.png",
    "Champion League I":   "Icon_HV_CWL_Champion_1.png",
}

def coc_deep_link(action: str, tag: str) -> str:
    """Build a CoC in-game deep link (https://link.clashofclans.com/...) for the given action/tag.

    Centralizes the URL format so every call site stays consistent (previously built inline
    in several places with a subtly inconsistent '#' encoding).
    """
    return f"https://link.clashofclans.com/en?action={action}&tag=%23{tag.lstrip('#')}"


def coc_clan_profile_url(clan_tag: str) -> str:
    """Deep link that opens a clan's profile in the CoC app."""
    return coc_deep_link("OpenClanProfile", clan_tag)


def coc_player_profile_url(player_tag: str) -> str:
    """Deep link that opens a player's profile in the CoC app."""
    return coc_deep_link("OpenPlayerProfile", player_tag)


def calculate_content_hash(content: str) -> str:
    """
    Calculate a SHA-256 hash of normalized leaderboard content for change detection.

    Args:
        content (str): Raw leaderboard text content to hash

    Returns:
        str: SHA-256 hash string (64 hex characters) representing the normalized content

    Filtering:
        - Removes time-sensitive elements (countdown timers, prediction lines)
        - Skips empty lines
        - Used for cache-optimized Discord posting and message deduplication
    """
    # Normalize content by removing code blocks and normalizing whitespace
    normalized = content.strip()
    
    # Remove time-sensitive elements that change frequently
    lines = normalized.strip().split('\n')
    filtered_lines = []
    for line in lines:
        original_line = line.strip()
        # Truncate lines containing countdown timers.
        # 'War ends in:' / 'War starts in:' are used by the currentwar leaderboard text path.
        # 'Ends in:' / 'Starts in:' are used by the cwlinfo embed path (inwar / preparation rounds).
        for countdown_phrase in ['War ends in:', 'War starts in:', 'Ends in:', 'Starts in:']:
            if countdown_phrase in original_line:
                idx = original_line.find(countdown_phrase)
                original_line = original_line[:idx].rstrip()
        # Skip war prediction line (currentwar leaderboard text format)
        if "The confidence of this prediction" in original_line:
            continue
        # Skip cwlinfo win-probability lines (📊 Win X%, Lose Y%, Draw Z%  ·  Confidence: C%).
        # These change on every run due to the Monte Carlo nature of the simulator and must not
        # trigger a repost — only actual star/destruction changes should do so.
        if "Confidence:" in original_line:
            continue
        # Skip lines that are empty or contain only whitespace
        if not original_line:
            continue
        # Keep all other lines:
        filtered_lines.append(original_line)  # type: ignore[misc]
    # Create hash of normalized content
    normalized_content = '\n'.join(filtered_lines)  # type: ignore[arg-type]
    return hashlib.sha256(normalized_content.encode('utf-8')).hexdigest()

# --- History loading ---

def _load_history_filtered(clan_tag: str, month: Optional[int], year: Optional[int], cwl_season: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Load and cache filtered historical data for a clan.
    
    Args:
        clan_tag: Clash of Clans clan tag
        month: Optional month filter (1-12)
        year: Optional year filter
        cwl_season: Optional exact CWL season filter (e.g. "2026-06" or "2026-06-15")
        
    Returns:
        List of historical war records
    """
    key = (clan_tag, month, year, cwl_season)
    if key in CACHE.history_cache:
        return CACHE.history_cache[key]
    wars = CACHE.get_clan_history(clan_tag)
    filtered: list[Dict[str, Any]] = []
    for row in wars:
        try:
            d = datetime.fromisoformat(row["Date"])
        except (ValueError, KeyError, TypeError):
            continue
        if month and d.month != month:
            continue
        if year and d.year != year:
            continue
        filtered.append(row)  # type: ignore[misc]
    if cwl_season is not None and CACHE.db_manager is not None:
        summary_rows = CACHE.db_manager.get_war_summaries_sync(clan_tag, season=cwl_season)
        allowed_war_ids = {row["war_id"] for row in summary_rows}
        filtered = [r for r in filtered if r.get("WarID") in allowed_war_ids]
    CACHE.history_cache[key] = filtered
    return filtered

def _load_history_filtered_by_players(player_tags: Set[str], month: Optional[int], year: Optional[int], cwl_season: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Load historical war data for a specific set of players, regardless of which
    clan_tag they fought under (used by leaderboard scope="all").

    Unlike _load_history_filtered, this is not cached across calls — it's only used
    for the manual /leaderboard command (not the automatic per-subscription posting
    loop), so the extra DB round-trip per invocation is not a concern.
    """
    if not player_tags or CACHE.db_manager is None:
        return []
    rows = CACHE.db_manager.get_player_attack_history_sync(sorted(player_tags), month, year)
    if cwl_season is not None:
        # war_summary rows aren't scoped to one clan here, so match on the same
        # "{clan_tag}::{war_id}" composite key used in get_player_attack_history_sync.
        summary_rows = CACHE.db_manager.get_war_summaries_sync(None, season=cwl_season)
        allowed = {f"{row['clan_tag']}::{row['war_id']}" for row in summary_rows}
        rows = [r for r in rows if r.get("WarID") in allowed]
    return rows

def _load_history_rows(
    clan_tag: str,
    month: Optional[int],
    year: Optional[int],
    cwl_season: Optional[str],
    *,
    scope: str = "own",
    member_player_tags: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Load history rows for a clan or clan family for a single month, honoring
    leaderboard scope.

    scope="own" (default, unchanged behavior): only wars fought by the clan(s)
        themselves are counted — a player's stats from a clan that has since left
        the family/guild are not included.
    scope="all": counts every war fought by a player currently rostered in the
        target clan(s), even wars fought while registered to a clan that is no
        longer tracked/subscribed. Falls back to "own" behavior if the current
        roster couldn't be resolved (member_player_tags is empty/None).
    """
    if scope == "all" and member_player_tags:
        return _load_history_filtered_by_players(member_player_tags, month, year, cwl_season)
    if clan_tag in CACHE.clan_families:
        clan_tags = CACHE.clan_families[clan_tag].get("clans", [])
    else:
        clan_tags = [clan_tag]
    rows: List[Dict[str, Any]] = []
    for tag in clan_tags:
        rows.extend(_load_history_filtered(tag, month, year, cwl_season))
    return rows

def _merge_entries(history_rows: List[Dict[str, Any]], temp_stats: Dict[str, Dict[str, Any]], war_in_progress: bool, mode: str) -> Dict[str, Dict[str, Any]]:
    """
    Merge historical war data with current temporary war statistics using PlayerID for consistency.
    Ensures the 'Player' field is always set to the most recent display name for each PlayerID.

    This function implements the core player name change handling logic by using PlayerID
    as the primary key while maintaining the most recent player name for display.
    
    Args:
        history_rows: Historical war data from database with player statistics
        temp_stats: Current war statistics from temporary files/memory (keyed by PlayerID)
        war_in_progress: Whether a war is currently ongoing (affects missed attack counting)
    
    Returns:
        Merged player statistics dictionary keyed by PlayerID, containing:
        - Player: Most recent player name (handles name changes and duplicate names)
        - PlayerID: Unique player identifier (never changes)
        - Stars: Total war stars earned
        - Attacks: Total attacks performed
        - Missed_Attacks: Total attacks not used (0 for ongoing wars)
        - Defensive_Stars: Total defensive stars conceded
        - Wars_Count: Number of wars participated in
        - Def_Stars_per_War: Average defensive stars per war
    
    Player Name Change & Duplicate Handling:
        Uses PlayerID as the primary key and tracks the most recent date for each entry.
        When a newer entry is found (based on date), it updates the displayed name while
        preserving all historical statistics under the same PlayerID. If two players have
        the same name but different PlayerIDs, both are tracked independently.
    
    War Deduplication:
        Tracks unique war IDs per player to ensure wars spanning multiple months
        aren't double-counted in year-to-date calculations.
    
    Missed Attacks Invariant:
        For ongoing wars (war_in_progress=True), Missed_Attacks is ALWAYS 0 because:
        - Players can still make their unused attacks before war ends
        - Temp stats are created with Missed_Attacks=0 during preparation/in_war states
        - This function explicitly ignores Missed_Attacks from temp_stats when war_in_progress=True
        - Final missed attack counts are only calculated when war ends and is finalized to history
    
    Example:
        # Player "OldName" (#ABC123) changes name to "NewName" (#ABC123)
        # All historical stats are preserved under PlayerID #ABC123
        # Display name becomes "NewName" (most recent)
        result["#ABC123"]["Player"] == "NewName"  # Latest name
        result["#ABC123"]["Stars"] == 150  # All historical + current stars
        # If two players named "Bob" with different PlayerIDs, both are present
    """
    entries: Dict[str, Dict[str, Any]] = {}
    war_sets: Dict[str, set[str]] = defaultdict(set)
    # def_war_sets: war IDs where the player was actually attacked (times_defended > 0
    # OR defensive_stars > 0 as fallback for older data without times_defended populated)
    def_war_sets: Dict[str, set[str]] = defaultdict(set)
    if "_cwl" in mode:
        mode = mode.replace("_cwl", "")

    for row in history_rows:
        key = row.get("PlayerID") or row["Player"]
        war_id = row.get("WarID") or row.get("WarId")
        try:
            row_date = datetime.fromisoformat(row.get("Date", "1970-01-01T00:00"))
        except (ValueError, TypeError):
            row_date = None
        cur = entries.setdefault(key, {"Player": row["Player"], "PlayerID": row.get("PlayerID", ""), "TH_lvl": row.get("TH_lvl", 0), "Stars":0,"Attacks":0,"Missed_Attacks":0,"Defensive_Stars":0,"Total_Dest_Pct":0.0, "_last_date": row_date})
        # Always update to latest name and TH level based on most recent date
        # BUGFIX (2026-02-07): Use >= instead of > to handle multiple wars on same day
        # This ensures we always show the LATEST TH level when players upgrade mid-period
        if row_date and cur.get("_last_date"):
            if row_date > cur["_last_date"]:
                # Newer date - update both name and TH level
                if row.get("Player", ""):
                    cur["Player"] = row["Player"]
                cur["TH_lvl"] = row.get("TH_lvl", cur.get("TH_lvl", 0))
                cur["_last_date"] = row_date
            elif row_date == cur["_last_date"]:
                # Same date - update TH level to maximum (handles player upgrades)
                cur["TH_lvl"] = max(cur.get("TH_lvl", 0), row.get("TH_lvl", 0))
        cur["Stars"] += row.get("Stars",0)
        cur["Attacks"] += row.get("Attacks",0)
        cur["Missed_Attacks"] += row.get("Missed_Attacks",0)
        cur["Defensive_Stars"] += row.get("Defensive_Stars",0)
        cur["Total_Dest_Pct"] += float(row.get("Total_Dest_Pct", 0.0) or 0.0)
        if war_id:
            war_sets[key].add(war_id)
            # Count this war as a defense if player was actually attacked
            # Fallback: defensive_stars > 0 handles old rows where times_defended is 0 (pre-migration)
            if row.get("Times_Defended", 0) > 0 or row.get("Defensive_Stars", 0) > 0:
                def_war_sets[key].add(war_id)
    current_war_id = None
    if temp_stats:
        any_stat = next(iter(temp_stats.values()))
        current_war_id = any_stat.get("WarID") or any_stat.get("WarId")
    for stat in temp_stats.values():
        key = stat.get("PlayerID") or stat.get("Player")
        player_name = stat.get("Player", "")
        try:
            row_date = datetime.fromisoformat(stat.get("Date", "1970-01-01T00:00"))
        except (ValueError, TypeError):
            row_date = None
        if key not in entries:
            entries[key] = {"Player": player_name, "PlayerID": stat.get("PlayerID",""), "TH_lvl": stat.get("TH_lvl", 0), "Stars":0,"Attacks":0,"Missed_Attacks":0,"Defensive_Stars":0,"Total_Dest_Pct":0.0, "_last_date": row_date}  # type: ignore
        else:
            # Always update to latest name and TH level based on most recent date
            # BUGFIX (2026-02-07): Use >= to handle wars on same day, always show latest TH level
            if row_date and entries[key].get("_last_date"):
                if row_date > entries[key]["_last_date"]:
                    # Newer date - update both name and TH level
                    if player_name:
                        entries[key]["Player"] = player_name
                    entries[key]["TH_lvl"] = stat.get("TH_lvl", entries[key].get("TH_lvl", 0))
                    entries[key]["_last_date"] = row_date
                elif row_date == entries[key]["_last_date"]:
                    # Same date - update TH level to maximum (handles player upgrades)
                    entries[key]["TH_lvl"] = max(entries[key].get("TH_lvl", 0), stat.get("TH_lvl", 0))
        entries[key]["Stars"] += stat.get("Stars",0)  # type: ignore
        entries[key]["Attacks"] += stat.get("Attacks",0)  # type: ignore
        # INVARIANT: When war_in_progress=True, Missed_Attacks from temp_stats is always 0
        # This ensures ongoing war leaderboards show only attacks made, not total opportunities
        # Players can still use their unused attacks, so missed count is premature
        entries[key]["Missed_Attacks"] += 0 if war_in_progress else stat.get("Missed_Attacks",0)  # type: ignore
        entries[key]["Defensive_Stars"] += stat.get("Defensive_Stars",0)  # type: ignore
        entries[key]["Total_Dest_Pct"] += float(stat.get("Total_Dest_Pct", 0.0) or 0.0)  # type: ignore
        if current_war_id:
            war_sets[key].add(current_war_id)  # type: ignore
            if stat.get("Times_Defended", 0) > 0 or stat.get("Defensive_Stars", 0) > 0:  # type: ignore
                def_war_sets[key].add(current_war_id)  # type: ignore
    for key, data in entries.items():
        data.pop("_last_date", None)
        wars_count = len(war_sets.get(key, set()))
        defs_count = len(def_war_sets.get(key, set()))
        data["Wars_Count"] = wars_count
        data["Defs_Count"] = defs_count
        data["Stars_per_Def"] = (data["Defensive_Stars"] / defs_count) if defs_count else 0.0
        # Keep legacy key for any code that still references it
        data["Def_Stars_per_War"] = data["Stars_per_Def"]
        attacks_total = data.get("Attacks", 0) + data.get("Missed_Attacks", 0)
        data["Avg_Dest_Pct"] = (data.get("Total_Dest_Pct", 0.0) / attacks_total) if attacks_total > 0 else 0.0
    if mode == "attack":
        return {k:v for k,v in entries.items() if v["Attacks"] > 0}
    elif mode == "avgstars":
        return {k:v for k,v in entries.items() if v["Attacks"] > 0}
    elif mode == "attackdefratio":
        return {k:v for k,v in entries.items() if v["Attacks"] > 0}
    elif mode == "missedattacks":
        return {k:v for k,v in entries.items() if v["Missed_Attacks"] > 0}
    elif mode == "defensive":
        return {k:v for k,v in entries.items() if v["Defensive_Stars"] > 0}
    elif mode == "currentwar":
        return {k:v for k,v in entries.items()}
    else:
        return {k:v for k,v in entries.items() if v["Attacks"] > 0}

# --- Leaderboard calculation ---

def calculate_leaderboard(clan_tag: str, month: Optional[int] = None, year: Optional[int] = None, *, cwl_only: bool = False, mode: str = DEFAULT_MODE, cwl_season: Optional[str] = None, scope: str = "own", member_player_tags: Optional[Set[str]] = None) -> Dict[str, Dict[str, Any]]:
    """
    Calculate leaderboard statistics for a clan or clan family with comprehensive data processing.
    This function aggregates historical war data with current war statistics to
    generate complete leaderboard statistics for the specified time period.

    If clan_tag is a family tag, aggregates data for all member clans and merges by PlayerID,
    so each player appears only once in the leaderboard with their stats summed from all clans.

    Args:
        clan_tag: Clash of Clans clan tag or family tag (normalized format: #ABCDEFGH or #ABCDEFGHIJ)
        month: Optional month filter (1-12) - if None, includes all months
        year: Optional year filter (4-digit) - if None, includes all years
        cwl_only: Whether to include only CWL (Clan War League) wars
        scope: "own" (default) counts only wars fought by the clan(s) themselves;
            "all" counts every war fought by a player currently rostered in the
            target clan(s), even ones fought while registered to a clan that is
            no longer tracked/subscribed (requires member_player_tags)
        member_player_tags: Current roster tags for the target clan(s), used only
            when scope="all" — resolved by the caller (needs a live CoC API call)

    Returns:
        Dictionary keyed by PlayerID containing comprehensive player statistics:
        - Player: Current player name (handles name changes and duplicate names)
        - PlayerID: Unique player identifier
        - Stars: Total war stars earned
        - Attacks: Total attacks performed  
        - Missed_Attacks: Total attacks not used
        - Defensive_Stars: Total defensive stars conceded
        - Wars_Count: Number of wars participated in
        - Def_Stars_per_War: Average defensive stars per war

    Data Sources:
        1. Historical database records (completed wars)
        2. In-memory temporary statistics (current war if applicable, keyed by PlayerID)

    CWL Filtering:
        When cwl_only=True, filters wars based on Max_Attacks=1 (CWL format)
        vs Max_Attacks=2 (regular war format).

    Current Month Logic:
        For current month requests, includes temporary war statistics if war is ongoing.
        This ensures real-time accuracy for active wars.

    Duplicate Name Handling:
        If two players have the same name but different PlayerIDs, both are tracked independently.

    Example:
        # Get August 2025 CWL leaderboard
        stats = calculate_leaderboard("#L2J0C0PY", month=8, year=2025, cwl_only=True)
        # Get year-to-date leaderboard
        stats = calculate_leaderboard("#L2J0C0PY", year=2025)
        # Get leaderboard for a clan family
        stats = calculate_leaderboard("#34570457B9", month=8, year=2025)
    """
    _now = datetime.now()  # Kept for potential future use
    if cwl_season is not None:
        cwl_only = True
    # Determine if clan_tag is a family tag or a single clan tag
    if clan_tag in CACHE.clan_families:
        clan_tags = CACHE.clan_families[clan_tag].get("clans", [])
    else:
        clan_tags = [clan_tag]
    # Aggregate history for all relevant clans (or, for scope="all", for every
    # player currently rostered in those clans regardless of which clan_tag the
    # underlying wars were fought under).
    all_history_rows: List[Dict[str, Any]] = []
    if mode != "currentwar":
        all_history_rows = _load_history_rows(clan_tag, month, year, cwl_season, scope=scope, member_player_tags=member_player_tags)
        if cwl_only:
            all_history_rows = [r for r in all_history_rows if r.get("Max_Attacks", 2) == 1]
    # Temp (in-progress war) stats are always per-clan — an ongoing war only exists
    # for the clan(s) actually in it right now, so scope doesn't apply here.
    all_temp_stats = {}
    for tag in clan_tags:
        temp_stats = CACHE.get_temp_war_stats(tag)
        include_temp = False
        if temp_stats:
            logging.debug(f"Temp stats found for clan {tag}: {temp_stats}")
            # Get the war's start date from any entry
            first_entry = next(iter(temp_stats.values()), None)
            if first_entry and "Date" in first_entry:
                date_str = first_entry["Date"]
                try:
                    # Parse ISO format (YYYY-MM-DDTHH:MM)
                    war_date = datetime.fromisoformat(date_str)
                    war_month = war_date.month
                    war_year = war_date.year
                    include_temp = (war_month == month and war_year == year) or (mode == "currentwar")
                except (ValueError, TypeError) as e:
                    raise WarProcessingError(
                        f"Error parsing war date from temp stats for clan {tag}: {date_str}",
                        context={"clan_tag": tag, "date_str": date_str, "error": str(e)}
                    )
        logging.debug(f"calculate_leaderboard() called with: clan_tag={tag}, month={month}, year={year}, cwl_only={cwl_only}, mode={mode}, include_temp={include_temp}")
        if include_temp:
            if cwl_only and temp_stats:
                temp_stats = {k: v for k, v in temp_stats.items() if v.get("Max_Attacks", 2) == 1}
            # Merge temp_stats by PlayerID
            for k, v in temp_stats.items():
                pid = v.get("PlayerID", k)
                if pid in all_temp_stats:
                    # Aggregate stats for same PlayerID
                    for stat_key in ("Stars", "Attacks", "Missed_Attacks", "Defensive_Stars", "Times_Defended"):
                        all_temp_stats[pid][stat_key] = all_temp_stats[pid].get(stat_key, 0) + v.get(stat_key, 0)  # type: ignore[misc]
                else:
                    all_temp_stats[pid] = v.copy()
    # Use war_in_progress flag if any clan has temp stats
    war_in_progress = bool(all_temp_stats)  # type: ignore[arg-type]
    return _merge_entries(all_history_rows, all_temp_stats, war_in_progress, mode=mode)  # type: ignore[arg-type]

async def post_leaderboard_to_discord(
    leaderboard_text: str,
    clan_tag: str,
    month: Union[int, List[int], List[Tuple[int, int]]],
    year: Optional[int],
    channel: Union[discord.TextChannel, discord.Thread],
    mode: str = DEFAULT_MODE,
    cwl_season: Optional[str] = None,
) -> None:
    """
    Unified leaderboard posting function for Discord with hash-based optimization, message splitting, and cache tracking.

    This function is the single source of truth for posting leaderboards to Discord and managing message tracking in CACHE.leaderboard_messages.

    Args:
        leaderboard_text: Formatted leaderboard content ready for Discord
        clan_tag: Clash of Clans clan tag (normalized format: #ABCDEFGH)
        month: Month(s) for leaderboard - int (1-12), list of ints for ranges
            ([1,2,3,...]), or list of (month, year) pairs for periods crossing a
            year boundary (in which case `year` is ignored/overwritten)
        year: Year for leaderboard (4-digit format); ignored when month is a list of pairs
        channel: Discord channel or thread where leaderboard will be posted
        mode: Leaderboard mode ("attack", "avgstars", "attackdefratio", "stars_cwl", etc.)

    Message Tracking:
        - The cache key for leaderboard_messages is a timestamp string generated at post time.
        - Message metadata includes clan_tag, channel_id, mode string, message_ids (comma-separated), and content_hash.
        - The mode string encodes mode, CWL status, month(s), and year for lookup and deletion.
        - Before posting, all previous messages for the same context (clan, channel, mode, month, year, cwl_only) are deleted from Discord and cache.

    Behavior:
        - Calculates a content hash for change detection.
        - Splits leaderboard text into multiple Discord messages if it exceeds 2000 characters.
        - Posts all messages and tracks their IDs in CACHE.leaderboard_messages under the timestamp key.
        - Ensures only one set of leaderboard messages per context is present in Discord and cache.
        - Handles both single month and multi-month requests for the month argument.
        - Provides consistent behavior for both automatic main loop posts and manual Discord commands.

    Note:
        The timestamp-based cache key ensures uniqueness and prevents collisions. The mode string is used for lookup and deletion only.
    """
    cwl_only = False
    mode = (mode or DEFAULT_MODE).lower()
    if "_cwl" in mode:
        mode = mode.replace("_cwl", "")
        cwl_only = True

    # Determine month_str for message key and deletion
    if isinstance(month, list) and month and isinstance(month[0], tuple):
        # (month, year) pairs — a period that may cross a year boundary. Encode
        # both month and year per entry so the key stays unique/deterministic;
        # readability doesn't matter here, it's only compared for equality.
        periods_repr: List[Tuple[int, int]] = list(month)  # type: ignore[arg-type]
        month_str = "+".join(f"{y}{m:02d}" for m, y in periods_repr)
        year = periods_repr[-1][1]
    elif isinstance(month, list):
        if len(month) == 1:
            month_str = f"{month[0]:02d}"
        elif len(month) > 1:
            month_str = f"{month[0]:02d}-{month[-1]:02d}"
        else:
            month_str = "current"
    else:
        month_str = f"{month:02d}" if isinstance(month, int) else "current"  # type: ignore[misc]

    # By this point year is always resolved: callers pass a real int, or (when month
    # is a list of (month, year) pairs) the branch above overwrites it from the pairs.
    assert year is not None
    # Compose mode string
    # "currentwar" and "cwlinfo" use just their name (no month/year) since they reflect
    # live state rather than a historical period.
    if mode in ("currentwar", "cwlinfo"):
        mode_str = mode
    elif cwl_only:
        if cwl_season:
            mode_str = f"{mode}_cwl_{cwl_season}"
        else:
            mode_str = f"{mode}_cwl_{month_str}_{year}"
    else:
        mode_str = f"{mode}_{month_str}_{year}"

    # Centralized message deletion before posting
    lookup_month = month_str
    lookup_year = year
    await delete_leaderboard_messages_for_context(clan_tag, str(channel.id), mode, lookup_month, lookup_year, cwl_only)

    # Generate timestamp-based key using unified helper
    timestamp = generate_message_key_timestamp()
    logging.debug(f"Generated unified message key: {timestamp}")

    # Log context for leaderboard processing (INFO level for visibility)
    logging.info(f"Posting leaderboard for clan {clan_tag}, mode {mode_str}, to channel {channel.id}")

    # Check if we need to split the message
    content_hash = calculate_content_hash(leaderboard_text)
    if len(leaderboard_text) > DISCORD_MESSAGE_MAX_LENGTH:
        logging.info(f"Leaderboard too long ({len(leaderboard_text)} chars) for Discord limit ({DISCORD_MESSAGE_MAX_LENGTH}). Using intelligent table splitting...")

    # Post messages
    try:
        messages_posted = await _split_and_post_leaderboard_helper(channel, leaderboard_text)
        if messages_posted:
            await CACHE.set_leaderboard_message(timestamp, {
                'clan_tag': clan_tag,
                'channel_id': str(channel.id),
                'mode': mode_str,
                'message_ids': ','.join(map(str, messages_posted)),
                'content_hash': content_hash
            })
    except Exception as e:
        raise LeaderboardPostingError(
            f"Error posting leaderboard for {clan_tag} in channel {channel.id}: {e}",
            context={"clan_tag": clan_tag, "channel_id": channel.id, "mode": mode_str}
        )

# Dedup set: tracks clan tags for which a background war_league fetch is already running,
# so _resolve_war_league does not schedule multiple concurrent refreshes for the same tag.
_league_refresh_in_progress: set[str] = set()

# Cached reference to the running event loop; set lazily when _resolve_war_league is first
# called from an async context.  Allows the sync code-path inside generate_war_info_text
# (which runs via asyncio.to_thread) to submit a blocking league fetch to the main loop
# instead of silently returning "" and falling back to the default.
_captured_event_loop: Optional[Any] = None

# Temporary side-channel used by generate_cwlinfo_embeds(comp_mode=True) to hand the
# per-player skill-factor debug text back to generate_cwlinfo_comp_embeds without
# changing the List[discord.Embed] return type of the inner function.
_comp_debug_texts: Dict[str, str] = {}


def _resolve_war_league(tag: str, opponent_tag: Optional[str] = None) -> str:
    """
    Resolve the CWL war league name for a clan tag from cache.
    Checks clan_name_cache (DB-backed) then the in-memory coc.Clan object.

    If war_league is missing from the cache entry, a fire-and-forget background task
    is scheduled via _ensure_clan_war_league() so the data is populated for the next
    call without blocking the current one.  Pass opponent_tag when known so the bg
    task can use the opponent's league as a fallback (both clans share the same tier).
    Returns empty string if unknown.
    """
    try:
        _entry = CACHE.clan_name_cache.get(tag)
        if isinstance(_entry, dict):
            _league = _entry.get("war_league") or ""
            if _league:
                return _league
        _cached = CACHE.coc_clan_cache.cache.get(tag, {})
        _clan_obj = _cached.get("data")
        if _clan_obj is not None:
            _wl = getattr(_clan_obj, 'war_league', None)
            if _wl is not None:
                _name = str(getattr(_wl, 'name', '') or '')
                if _name:
                    return _name
    except Exception:
        pass

    # war_league is missing — schedule a one-shot background fetch so it is populated
    # for the next call (stale-while-revalidate pattern, same as CoCClanCache).
    # opponent_tag is passed through so _ensure_clan_war_league can use the opponent's
    # league as a reliable fallback when the primary clan has no warLeague in the API.
    if tag not in _league_refresh_in_progress:
        _league_refresh_in_progress.add(tag)
        _opp = opponent_tag  # capture in closure

        async def _bg_fetch_league(clan_tag: str) -> None:
            try:
                league = await _ensure_clan_war_league(clan_tag, _opp)
                logging.info(
                    f"[WAR-LEAGUE-BG] Background fetch resolved '{league}' for {clan_tag}"
                    + (f" (opponent fallback: {_opp})" if _opp else "")
                )
            except Exception as _ex:
                logging.debug(f"[WAR-LEAGUE-BG] Background fetch failed for {clan_tag}: {_ex}")
            finally:
                _league_refresh_in_progress.discard(clan_tag)

        try:
            import asyncio as _asyncio
            # get_running_loop() raises RuntimeError when there is no running loop,
            # so _bg_fetch_league(tag) is never called (and no coroutine is created)
            # unless we are certain create_task will succeed.
            _running_loop = _asyncio.get_running_loop()
            global _captured_event_loop
            _captured_event_loop = _running_loop  # capture for thread-context use
            _running_loop.create_task(_bg_fetch_league(tag))
        except RuntimeError:
            # Called from a thread (e.g. generate_war_info_text via asyncio.to_thread).
            # Do a synchronous blocking fetch if the main event loop was already captured.
            if _captured_event_loop is not None and _captured_event_loop.is_running():
                import asyncio as _asyncio
                _fut = _asyncio.run_coroutine_threadsafe(
                    _ensure_clan_war_league(tag, _opp), _captured_event_loop
                )
                try:
                    _fetched = _fut.result(timeout=3.0)
                    _league_refresh_in_progress.discard(tag)
                    return _fetched
                except Exception:
                    pass
            _league_refresh_in_progress.discard(tag)

    return ""


def generate_war_info_text(clan_tag: str) -> str:
    # Shared with war_notifications.py — was a function-local duplicate here until C5
    # (2026-08-08); see parse_war_timestamp_field()'s docstring for the format parsed.
    from qapbot.war_notifications import parse_war_timestamp_field as _parse_timestamp_field

    def _fmt_delta_from_secs(secs: Optional[int]) -> str:
        if secs is None:
            return "?"
        if secs < 0:
            secs = 0
        h, r = divmod(secs, SECONDS_PER_HOUR)
        m, _ = divmod(r, SECONDS_PER_MINUTE)
        return f"{h}h {m}m" if h else f"{m}m"
    if clan_tag in CACHE.clan_families:
        return ""
    war_data = CACHE.get_current_war_data(clan_tag)
    if not war_data:
        logging.info(f"No current war data found for clan {clan_tag}")
        return f"No current war information available for {clan_tag}."
    state = war_data.get('state', '')
    my_clan = war_data.get('clan', {})
    opp_clan = war_data.get('opponent', {})
    raw_start = war_data.get('start_time', None)
    raw_end = war_data.get('end_time', None)
    _start_time, start_secs = _parse_timestamp_field(raw_start)
    _end_time, end_secs = _parse_timestamp_field(raw_end)
    my_stars = my_clan.get('stars', 0)
    opp_stars = opp_clan.get('stars', 0)

    if state in ('preparation', 'in_war'):
        attacks_per_member = war_data.get('attacks_per_member', 2) or 2
        my_members = list(my_clan.get('members', []))
        opp_members = list(opp_clan.get('members', []))

        def _count_attacks(members: List[Any]) -> int:
            return sum(len(m.get('attacks', []) or []) for m in members)

        ends_in: str = ""
        starts_in: str = ""
        if state == 'in_war':
            ends_in = _fmt_delta_from_secs(end_secs)
            my_attacks_done = _count_attacks(my_members)
            opp_attacks_done = _count_attacks(opp_members)
        else:
            starts_in = _fmt_delta_from_secs(start_secs)
            my_attacks_done = 0
            opp_attacks_done = 0

        my_total_possible = len(my_members) * attacks_per_member
        opp_total_possible = len(opp_members) * attacks_per_member
        my_attacks_left = max(my_total_possible - my_attacks_done, 0)
        opp_attacks_left = max(opp_total_possible - opp_attacks_done, 0)

        # --- TH lineup emojis ---
        def _build_lineup_row(members: List[Any]) -> str:
            """Return TH emojis with counts sorted descending by TH level."""
            from qapbot.emojis import BotEmojis
            from collections import Counter
            counts: Counter[int] = Counter(m.get('townhall', 0) or 0 for m in members)
            parts: List[str] = []
            for th in sorted(counts, reverse=True):
                attr = f"TH{th:02d}"
                emoji = getattr(BotEmojis, attr, f"[TH{th}]")
                parts.append(f"{emoji}×{counts[th]}")
            return " ".join(parts)

        my_lineup = _build_lineup_row(my_members)
        opp_lineup = _build_lineup_row(opp_members)
        my_name_short = normalize_player_name(my_clan.get('name', 'Our Clan'))
        opp_name_short = normalize_player_name(opp_clan.get('name', 'Opponent'))

        # --- Resolve CWL league for each clan ---
        # Pass each clan's opponent tag so background fetches can use the opponent's
        # warLeague as fallback when the primary clan has no warLeague in the CoC API.
        opp_tag = opp_clan.get('tag', '')
        my_league = _resolve_war_league(clan_tag, opponent_tag=opp_tag or None)
        opp_league = _resolve_war_league(opp_tag, opponent_tag=clan_tag) if opp_tag else ""
        my_league_str  = f" · {my_league}"  if my_league  else ""
        opp_league_str = f" · {opp_league}" if opp_league else ""

        # --- Status line ---
        if state == 'in_war':
            status_line = f"🛡️ War in progress | War ends in: {ends_in}"
        else:
            status_line = f"🛡️ War preparation: {my_clan.get('name', '')} vs {opp_clan.get('name', '')} | War starts in: {starts_in}"

        # --- Win probability (shown in header, between roster and status line) ---
        prob_line: str = ""
        try:
            from QBwarsim import calculate_win_probability
            my_dest = my_clan.get('destruction', None)
            opp_dest = opp_clan.get('destruction', None)
            # Use per-clan leagues so each side's attack probabilities match their own tier.
            # Example: BrawlerBaller (Champion I) vs Die Rußmännchen (Master II) — each clan
            # gets its own distribution instead of both sharing a single blended value.
            my_league_eff: str = my_league or opp_league or _CWL_LEAGUE_DEFAULT
            opp_league_eff: str = opp_league or my_league or _CWL_LEAGUE_DEFAULT
            # Warn only when resolution genuinely failed — not when the real league
            # happens to equal the default string ("Master League I").
            if not my_league and not opp_league:
                logging.warning(
                    f"[WAR-LEAGUE] No league resolved for {clan_tag}; "
                    f"defaulting to '{_CWL_LEAGUE_DEFAULT}'"
                )
            logging.debug(f"Using CWL leagues my='{my_league_eff}', opp='{opp_league_eff}' for war prediction of {clan_tag}")
            # Compute skill factors from CWL history for both clans — using each clan's own
            # league so the Bayesian baseline is calibrated to the correct tier.
            _my_member_tags = [m.get('tag', '') for m in my_members if m.get('tag')]
            _opp_tag = opp_clan.get('tag', '')
            _opp_member_tags = [m.get('tag', '') for m in opp_members if m.get('tag')]
            _all_skill_factors: Dict[str, float] = {}
            _all_skill_factors.update(_load_skill_factors_for_clan(
                clan_tag, _my_member_tags, my_league_eff,
                clan_name=my_clan.get('name', clan_tag),
            ))
            if _opp_tag:
                _all_skill_factors.update(_load_skill_factors_for_clan(
                    _opp_tag, _opp_member_tags, opp_league_eff,
                    clan_name=opp_clan.get('name', _opp_tag),
                ))
            _common_kw: Dict[str, Any] = dict(
                my_clan_destruction=my_dest if isinstance(my_dest, (int, float)) else None,
                enemy_clan_destruction=opp_dest if isinstance(opp_dest, (int, float)) else None,
                cwl_league_my=my_league_eff,
                cwl_league_opp=opp_league_eff,
            )
            # Primary prediction: skill-adjusted (if skill data available).
            win_prob, lose_prob, draw_prob, confidence = calculate_win_probability(
                war_data, my_clan.get('tag', clan_tag),
                my_attacks_left, my_stars, opp_stars, opp_attacks_left,
                player_skill_factors=_all_skill_factors or None, **_common_kw,
            )
            logging.info(
                f"calculate_win_probability returned: win_prob={win_prob}, lose_prob={lose_prob}, "
                f"draw_prob={draw_prob}, confidence={confidence}"
            )
            if win_prob >= 100:
                prob_line = "🎉 Victory is ours! They have no mathematical path back."
            elif lose_prob >= 100:
                prob_line = "💀 We lost. Shake it off – next war we will take revenge."
            else:
                # Two-line format: header with clan name, then the numbers.
                prob_line = (
                    f"\U0001f4ca The probabilities for {my_name_short} are:\n"
                    f"\u2003   Win **{win_prob}%**, Lose {lose_prob}%, "
                    f"Draw {draw_prob}%  \u00b7  Confidence: {confidence}%"
                )
        except Exception as e:
            logging.debug(f"Win probability calculation failed for {clan_tag}: {e}")
            # Non-fatal - continue without probability display

        # --- Sentinel block: status → blank → Rosters → blank → prediction ---
        # The sentinel wraps content that must render as plain Discord text (for custom
        # emojis); _split_and_post_leaderboard_helper posts it outside code blocks.
        sentinel_inner = f"{status_line}\n"
        sentinel_inner += (
            f"\nRosters:\n"
            f"\u200E{my_name_short}{my_league_str}\u200E: {my_lineup}\n"
            f"\u200E{opp_name_short}{opp_league_str}\u200E: {opp_lineup}\n"
        )
        if prob_line:
            sentinel_inner += f"\n{prob_line}\n\u200b"
        roster_block = f"{_PLAIN_SENTINEL_START}{sentinel_inner}{_PLAIN_SENTINEL_END}"

        if state == 'in_war':
            # Compute max additional stars via simulation; on failure degrade gracefully
            # BUGFIX (2026-01-XX): Previously raised WarProcessingError on sim failure, which
            # propagated to periodic_main() causing exponential backoff and frozen leaderboards.
            # Now: sim_failed=True → max_possible shows "?" → leaderboard still posts normally.
            sim_failed = False
            add_my = add_opp = 0
            try:
                from QBwarsim import calculate_max_possible_stars
                if my_clan:
                    add_my = calculate_max_possible_stars(war_data, my_clan.get('tag', clan_tag))
                if opp_clan:
                    add_opp = calculate_max_possible_stars(war_data, opp_clan.get('tag', ''))
                logging.info(f"Max possible stars simulation: add_my={add_my}, add_opp={add_opp}")
            except Exception as e:
                sim_failed = True
                logging.warning(f"[WAR-SIM] Max possible stars simulation failed for {clan_tag} (non-fatal, showing '?' for Stars Max): {e}")

            max_possible_my  = (my_stars + add_my)  if not sim_failed else None
            max_possible_opp = (opp_stars + add_opp) if not sim_failed else None

            my_dest_val  = float(my_clan.get('destruction',  0) or 0)
            opp_dest_val = float(opp_clan.get('destruction', 0) or 0)

            # Stars / attacks table – rendered inside a ``` code block
            COL_NAME  = 16
            COL_STARS = 5
            COL_ATK   = 7
            COL_REM   = 11
            COL_MAX   = 9
            COL_DEST  = 6
            header = (
                f"{'Clan'.ljust(COL_NAME)} {'Stars'.rjust(COL_STARS)} "
                f"{'Attacks'.ljust(COL_ATK)} {'Unused Atks'.ljust(COL_REM)} "
                f"{'Stars Max'.ljust(COL_MAX)} {'🔥%'.rjust(COL_DEST - 2)}"
            )
            underline = (
                f"{'-'*COL_NAME} {'-'*COL_STARS} {'-'*COL_ATK} "
                f"{'-'*COL_REM} {'-'*COL_MAX} {'-'*COL_DEST}"
            )

            def fmt_row(
                clan_name: str,
                stars: int,
                atk_done: int,
                atk_left: int,
                max_possible: Optional[int],
                dest_pct: float,
            ) -> str:
                max_str  = str(max_possible) if max_possible is not None else '?'
                dest_str = f"{dest_pct:.1f}%"
                clan_name = "\u200E" + normalize_player_name(clan_name) + "\u200E"
                clan_name = clan_name + ' ' * (COL_NAME - text_display_width(clan_name))
                return (
                    f"{clan_name} {str(stars).rjust(COL_STARS)} "
                    f"{str(atk_done).rjust(COL_ATK)} {str(atk_left).rjust(COL_REM)} "
                    f"{max_str.rjust(COL_MAX)} {dest_str.rjust(COL_DEST)}"
                )

            row_my = fmt_row(
                my_clan.get('name', 'Our Clan'), my_stars,
                my_attacks_done, my_attacks_left, max_possible_my, my_dest_val,
            )
            row_opp = fmt_row(
                opp_clan.get('name', 'Opponent'), opp_stars,
                opp_attacks_done, opp_attacks_left, max_possible_opp, opp_dest_val,
            )

            war_info_lines = [roster_block, '', header, underline, row_my, row_opp, '']
        else:
            # preparation: no attacks yet → no stats table
            war_info_lines = [roster_block]

        war_info_line = "\n".join(war_info_lines)
    elif state == 'war_ended':
        war_info_line = f"🛡️ War ended: {my_stars}⭐ - {opp_stars}⭐"
    elif state == 'not_in_war':
        war_info_line = f"🛡️ We are currently not in war."
    else:
        war_info_line = f"🛡️ War status unknown ({state})"
    return war_info_line


_EMBED_DESC_LIMIT = 4096


def _desc_lines_to_embeds(
    desc_lines: List[str],
    title: str,
    url: str,
    color: int,
) -> List[discord.Embed]:
    """Convert desc_lines list into one or more Embeds, each ≤ 4096 chars.

    Lines are grouped into rounds by blank-string separators ("").  A new
    Embed is started whenever adding the next round block would push the
    description past Discord's 4096-character limit.  If a single block is
    itself longer than the limit it is hard-truncated as a last resort.
    Only the first embed carries the title/url so the series reads naturally.
    """
    full_text = "\n".join(desc_lines)
    if len(full_text) <= _EMBED_DESC_LIMIT:
        return [discord.Embed(title=title, url=url, description=full_text, color=color)]

    # Group lines into round-level blocks (each block ends with the blank "" separator).
    blocks: List[List[str]] = []
    buf: List[str] = []
    for line in desc_lines:
        buf.append(line)
        if line == "":  # blank separator marks end of a round block
            blocks.append(buf)
            buf = []
    if buf:  # trailing lines with no blank terminator
        blocks.append(buf)

    embeds: List[discord.Embed] = []
    current: List[str] = []

    for block in blocks:
        trial = current + block
        trial_text = "\n".join(trial)
        if len(trial_text) > _EMBED_DESC_LIMIT and current:
            # Flush current chunk as an embed before absorbing this block.
            embeds.append(discord.Embed(
                title=title if not embeds else "",
                url=url if not embeds else "",
                description="\n".join(current),
                color=color,
            ))
            current = list(block)
        else:
            current = trial

    if current:
        desc = "\n".join(current)
        if len(desc) > _EMBED_DESC_LIMIT:  # hard truncate only as last resort
            desc = desc[:_EMBED_DESC_LIMIT]
        embeds.append(discord.Embed(
            title=title if not embeds else "",
            url=url if not embeds else "",
            description=desc,
            color=color,
        ))

    return embeds or [discord.Embed(title=title, url=url, description=full_text[:_EMBED_DESC_LIMIT], color=color)]


def classify_war_result(my_stars: int, opp_stars: int, my_dest: float, opp_dest: float) -> str:
    """Classify a finished war as Win/Loss/Draw: stars decide first, destruction % breaks ties.

    Shared by every "war ended" CWL embed renderer so the tiebreak rule (and its emoji label)
    is defined once instead of being copy-pasted identically across each one.
    """
    if my_stars > opp_stars:
        return "✅ Win"
    if opp_stars > my_stars:
        return "❌ Loss"
    if my_dest > opp_dest:
        return "✅ Win"
    if opp_dest > my_dest:
        return "❌ Loss"
    return "🤝 Draw"


def _generate_cwlinfo_archive_embeds(clan_tag: str) -> List[discord.Embed]:
    """
    Build a single CWL season overview embed from the war_summary DB table.
    All rounds are formatted as text in the embed description.
    Returns an empty list if no CWL data is found in the DB for the clan.

    NOTE: previously read from archive/ JSON files; now reads from the
    war_summary table populated by the migration script and every new war save.
    """

    if not CACHE.db_manager:
        return []

    summaries: List[Dict[str, Any]] = CACHE.db_manager.get_war_summaries_sync(  # type: ignore[union-attr]
        clan_tag, is_cwl=True
    )
    if not summaries:
        return []

    # Group by cwl_season, pick latest
    by_season: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in summaries:
        season = str(row.get('cwl_season') or '')
        if season:
            by_season[season].append(row)

    if not by_season:
        return []

    latest_season: str = max(by_season.keys())
    # Already ordered by date ASC from the query
    season_wars: List[Dict[str, Any]] = by_season[latest_season]

    clan_name: str = CACHE.get_clan_name(clan_tag, clan_tag) or clan_tag
    # Local per-render memo: opponent tags can repeat across rounds in irregular CWL
    # group histories (mid-season bonus CWL, rescheduled wars), so cache each tag's
    # resolved league here instead of re-resolving it on every repeat within this render.
    _league_cache: Dict[str, str] = {}
    def _league_for(tag: str) -> str:
        if tag not in _league_cache:
            _league_cache[tag] = _resolve_war_league(tag)
        return _league_cache[tag]
    my_league: str = _league_for(clan_tag)
    league_hdr: str = f" \u00b7 {my_league}" if my_league else ""

    my_clan_url: str = coc_clan_profile_url(clan_tag)
    desc_lines: List[str] = [
        f"CWL Season **{latest_season}**{league_hdr}\n"
    ]

    for round_idx, row in enumerate(season_wars, start=1):
        my_stars_a: int = int(row.get('clan_stars', 0) or 0)
        opp_stars_a: int = int(row.get('opponent_stars', 0) or 0)
        opp_tag_a: str = str(row.get('opponent_tag', '') or '')
        opp_name_a: str = normalize_player_name(str(row.get('opponent_name', 'Opponent') or 'Opponent'))
        opp_league_a: str = _league_for(opp_tag_a) if opp_tag_a else ""
        opp_league_str_a: str = f"  \u00b7  {opp_league_a}" if opp_league_a else ""
        my_dest_a: float = float(row.get('clan_destruction', 0.0) or 0.0)
        opp_dest_a: float = float(row.get('opp_destruction', 0.0) or 0.0)
        result_lbl_a = classify_war_result(my_stars_a, opp_stars_a, my_dest_a, opp_dest_a)
        n_my_a = normalize_player_name(clan_name)
        opp_url_a = coc_clan_profile_url(opp_tag_a) if opp_tag_a else ""
        my_lineup_asc_a = _lineup_from_json_r(str(row.get('clan_lineup_json') or '[]'), ascending=True)
        opp_lineup_a = _lineup_from_json_r(str(row.get('opp_lineup_json') or '[]'), ascending=False)

        desc_lines.append(
            f"**Round {round_idx}**  \u00b7  {result_lbl_a}  "
            f"`{my_stars_a}\u2b50 \u2013 {opp_stars_a}\u2b50 \u00b7 {my_dest_a:.1f}% \u2013 {opp_dest_a:.1f}%`"
        )
        desc_lines.append(
            f"vs. [\u200e{opp_name_a}\u200e]({opp_url_a})  \u200e`{opp_tag_a}`{opp_league_str_a}"
            if opp_url_a else
            f"vs. \u200e{opp_name_a}\u200e  \u200e`{opp_tag_a}`{opp_league_str_a}"
        )
        if my_lineup_asc_a or opp_lineup_a:
            desc_lines.append(f"`{n_my_a}`  {my_lineup_asc_a}\u2003vs\u2003{opp_lineup_a}  `{opp_name_a}`")
        desc_lines.append("")  # blank line between rounds

    return _desc_lines_to_embeds(
        desc_lines,
        title=f"\u2b50 {normalize_player_name(clan_name)} ({clan_tag})",
        url=my_clan_url,
        color=0x747f8d,
    )


def _build_cwl_lineup(members: List[Any], team_size: int = 15, ascending: bool = False) -> str:
    """Return TH emoji × count string from coc member objects or war-data dicts."""
    from qapbot.emojis import BotEmojis
    from collections import Counter as _Counter
    ths: List[int] = []
    for m in members:
        if hasattr(m, 'town_hall'):
            th = int(getattr(m, 'town_hall', 0) or 0)
        else:
            th = int(m.get('townhall', 0) or 0)
        ths.append(th)
    ths.sort(reverse=True)
    ths = ths[:team_size]
    counts: Any = _Counter(ths)
    parts: List[str] = []
    for th_lv in sorted(counts, reverse=not ascending):
        attr = f"TH{th_lv:02d}"
        emoji = getattr(BotEmojis, attr, f"[TH{th_lv}]")
        parts.append(f"{emoji}×{counts[th_lv]}")
    return " ".join(parts)


def _fmt_secs(secs: Optional[int]) -> str:
    if secs is None or secs < 0:
        return "?"
    h, r = divmod(secs, SECONDS_PER_HOUR)
    m, _ = divmod(r, SECONDS_PER_MINUTE)
    return f"{h}h {m}m" if h else f"{m}m"


def _lineup_from_json_r(lineup_json: str, ascending: bool = False) -> str:
    """Render a stored TH-level JSON array (from war_summary DB) as emoji × count string."""
    from qapbot.emojis import BotEmojis
    try:
        import json as _ljson
        from collections import Counter as _LC
        _ths: List[int] = _ljson.loads(lineup_json or '[]')
        _counts: Any = _LC(_ths)
        _parts: List[str] = []
        for _th in sorted(_counts, reverse=not ascending):
            _attr = f"TH{_th:02d}"
            _emoji = getattr(BotEmojis, _attr, f"[TH{_th}]")
            _parts.append(f"{_emoji}×{_counts[_th]}")
        return " ".join(_parts)
    except Exception:
        return ""


def _load_skill_factors_for_clan(
    clan_tag: str,
    member_tags: List[str],
    cwl_league: str,
    clan_name: str = "",
) -> Dict[str, float]:
    """Load per-player skill factors from the best available CWL history.

    This is a synchronous function — safe to call from a thread (e.g. inside
    generate_war_info_text which runs via asyncio.to_thread).  When calling
    from an async context, wrap with asyncio.to_thread to avoid blocking the
    event loop (each SQLite query can take ~500 ms on large databases).

    Fallback chain per player:
    1. Current month's CWL attack data (if CWL is ongoing / just ended).
    2. Latest past CWL season in which the player participated.
    3. No entry → caller uses base rates for that player.

    Logs one INFO line per clan summarising the source breakdown.

    Returns:
        {player_tag: skill_factor} — only players with CWL history are included.
    """
    from QBwarsim import compute_player_skill_factors_from_attacks as _compute_skill
    import datetime as _dt

    if not CACHE.db_manager or not member_tags:
        return {}

    try:
        per_player = CACHE.db_manager.get_player_cwl_attacks_multi_season_sync(
            member_tags,
        )
    except Exception as _ex:
        logging.debug(f"[SKILL-LOAD] DB query failed for {clan_tag}: {_ex}")
        return {}

    if not per_player:
        _display = clan_name or clan_tag
        logging.debug(
            f"[SKILL-LOAD] Skill-data source for clan {_display} ({cwl_league}): "
            f"{len(member_tags)} players base rates"
        )
        return {}

    # Flatten into attack_records for compute_player_skill_factors_from_attacks.
    attack_records: List[Dict[str, Any]] = []
    for ptag, rows in per_player.items():
        attack_records.extend(rows)

    skill_factors = _compute_skill(attack_records, cwl_league)

    # Build source summary log line.
    current_month = _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y-%m")
    # Count players by their season source.
    season_counts: Dict[str, int] = {}
    for ptag in per_player:
        # All rows for a player come from the same (most recent) season.
        season = per_player[ptag][0]["cwl_season"] if per_player[ptag] else "?"
        season_counts[season] = season_counts.get(season, 0) + 1
    base_rate_count = len(member_tags) - len(per_player)

    parts: List[str] = []
    for season in sorted(season_counts, reverse=True):
        _mm_yy = season[5:7] + "/" + season[2:4] if len(season) >= 7 else season
        label = "CWL " + _mm_yy
        if season.startswith(current_month):
            label = "CWL " + _mm_yy + " (current)"
        parts.append(f"{season_counts[season]} players {label}")
    if base_rate_count > 0:
        parts.append(f"{base_rate_count} players base rates")

    _display = clan_name or clan_tag
    logging.debug(f"[SKILL-LOAD] Skill-data source for clan {_display} ({cwl_league}): {', '.join(parts)}")

    return skill_factors


def _build_cwl_war_proxy(
    clan_tag: str,
    my_members: List[Any],
    opp_tag: str,
    opp_members: List[Any],
    apm: int,
    *,
    my_stars: int = 0,
    opp_stars: int = 0,
    include_attacks: bool = False,
    my_destruction: float = 0.0,
    opp_destruction: float = 0.0,
) -> Dict[str, Any]:
    """Build a war_data proxy dict from coc member objects for calculate_win_probability."""
    def _entry(m: Any, idx: int) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            'tag': getattr(m, 'tag', str(idx)),
            'townhall': getattr(m, 'town_hall', 0) or 0,
        }
        if include_attacks:
            d['attacks'] = [
                {'defenderTag': getattr(a, 'defender_tag', getattr(a, 'defenderTag', '')),
                 'stars': getattr(a, 'stars', 0)}
                for a in (list(getattr(m, 'attacks', []) or []))
            ]
        else:
            d['attacks'] = []
        return d

    clan_side: Dict[str, Any] = {'tag': clan_tag, 'stars': my_stars, 'destruction': my_destruction, 'members': [_entry(m, i) for i, m in enumerate(my_members)]}
    opp_side: Dict[str, Any] = {'tag': opp_tag, 'stars': opp_stars, 'destruction': opp_destruction, 'members': [_entry(m, i) for i, m in enumerate(opp_members)]}
    return {'clan': clan_side, 'opponent': opp_side, 'attacks_per_member': apm}


async def _cwl_append_prediction(
    desc_lines: List[str],
    proxy: Dict[str, Any],
    clan_tag: str,
    my_atk_left: int,
    my_stars: int,
    opp_stars: int,
    opp_atk_left: int,
    cwl_league: str,
    comp_mode: bool,
    player_skill_factors: Optional[Dict[str, float]],
    round_idx: int,
    label: str,
    cwl_league_opp: Optional[str] = None,
) -> None:
    """Run win probability prediction(s) and append result lines to desc_lines.

    cwlinfo (comp_mode=False):
        📊 Win X%, Lose Y%, Draw Z% · Confidence: N%

    cwlinfo_comp (comp_mode=True):
        🧠 Skill-adj.: Win X%, Lose Y%, Draw Z% · Confidence: N%
        📊 Base rates: Win X%, Lose Y%, Draw Z% · Confidence: N%
    """
    try:
        from QBwarsim import calculate_win_probability

        if comp_mode:
            # Both calls are independent — run them concurrently to halve wall time.
            # Each asyncio.to_thread dispatches to the process pool internally,
            # so the GIL is not a bottleneck here.
            (win_s, lose_s, draw_s, conf_s), (win_b, lose_b, draw_b, conf_b) = (
                await asyncio.gather(
                    asyncio.to_thread(
                        calculate_win_probability,
                        proxy, clan_tag, my_atk_left, my_stars, opp_stars, opp_atk_left,
                        player_skill_factors=player_skill_factors or None,
                        cwl_league_my=cwl_league, cwl_league_opp=cwl_league_opp or cwl_league,
                    ),
                    asyncio.to_thread(
                        calculate_win_probability,
                        proxy, clan_tag, my_atk_left, my_stars, opp_stars, opp_atk_left,
                        player_skill_factors=None,
                        cwl_league_my=cwl_league, cwl_league_opp=cwl_league_opp or cwl_league,
                    ),
                    return_exceptions=False,
                )
            )
            # Line 1: skill-adjusted
            desc_lines.append(
                f"\U0001f9e0 Skill-adj.: Win **{win_s}%**, Lose {lose_s}%, "
                f"Draw {draw_s}%  \u00b7  Confidence: {conf_s}%"
            )
            # Line 2: base rates (no skill factors)
            desc_lines.append(
                f"\U0001f4ca Base rates: Win **{win_b}%**, Lose {lose_b}%, "
                f"Draw {draw_b}%  \u00b7  Confidence: {conf_b}%"
            )
        else:
            # cwlinfo: single skill-adjusted line with 📊 prefix
            win_p, lose_p, draw_p, conf = await asyncio.to_thread(
                calculate_win_probability,
                proxy, clan_tag, my_atk_left, my_stars, opp_stars, opp_atk_left,
                player_skill_factors=player_skill_factors or None,
                cwl_league_my=cwl_league, cwl_league_opp=cwl_league_opp or cwl_league,
            )
            desc_lines.append(
                f"\U0001f4ca Win **{win_p}%**, Lose {lose_p}%, "
                f"Draw {draw_p}%  \u00b7  Confidence: {conf}%"
            )
    except Exception as _ex:
        logging.debug(f"[CWL-INFO] Win prob failed round {round_idx} ({label}): {_ex}")


async def _cwl_compute_skill_factors(
    clan_tag: str,
    clan_name: str,
    season: str,
    my_league: str,
    group_clan_map: Dict[str, Any],
    assigned_rounds: List[List[str]],
    _db_season_rows: List[Dict[str, Any]],
    _round_war_cache: Dict[int, Any],
    real_war_opponents: Dict[int, str],
    predicted_round_opponents: Dict[int, str],
    comp_mode: bool,
) -> Dict[str, float]:
    """Compute CWL player skill factors; in comp_mode also populates _comp_debug_texts."""
    player_skill_factors: Dict[str, float] = {}

    # ═══ Universal base: multi-season skill factors for ALL group clans ═══
    # Works identically for cwlinfo, cwlinfo_comp, and currentwar (CW & CWL).
    # Per-player fallback: current CWL → latest past CWL → base rates.
    if CACHE.db_manager:
        _used_league_base = my_league or _CWL_LEAGUE_DEFAULT
        try:
            for _g_tag, _g_obj in group_clan_map.items():
                _g_mems = [getattr(m, 'tag', '') for m in list(getattr(_g_obj, 'members', []) or [])]
                _g_name = normalize_player_name(str(getattr(_g_obj, 'name', _g_tag) or _g_tag))
                if _g_mems:
                    player_skill_factors.update(
                        await asyncio.to_thread(
                            _load_skill_factors_for_clan,
                            _g_tag, _g_mems, _used_league_base,
                            clan_name=_g_name,
                        )
                    )
        except Exception as _sf_base_ex:
            logging.debug(f"[CWL-SKILL] Base skill factor load failed for group: {_sf_base_ex}")

    # ═══ comp_mode supplement: current-season + live cache overlay + debug table ═══
    # Overlays on top of the universal base — current-season factors (including
    # live cache data from the active round) take precedence over historical.
    if comp_mode:
        try:
            from QBwarsim import compute_player_skill_factors_from_attacks as _compute_skill_v2  # type: ignore[attr-defined]
            _attack_records: List[Dict[str, Any]] = []
            # Per-player info for the debug report: {tag: [name, th]}
            # Only populated for OUR clan members (to keep the debug table focused).
            _dbg_player_info: Dict[str, List[Any]] = {}
            # Per-player aggregate stats for debug report: {tag: [attacks, actual_stars]}
            _dbg_my_stats: Dict[str, List[int]] = {}
            # Same two dicts for OPPONENT players
            _dbg_opp_info: Dict[str, List[Any]] = {}
            _dbg_opp_stats: Dict[str, List[int]] = {}
            # Clan grouping for opponent debug table
            _dbg_opp_clan: Dict[str, str] = {}            # player_tag → clan_tag
            _dbg_opp_clan_name: Dict[str, str] = {}       # clan_tag → clan_name
            _dbg_opp_clan_order: List[str] = []           # ordered unique clan tags (first-seen)
            _dbg_opp_player_order: Dict[str, List[str]] = {}  # clan_tag → [player_tags in order]

            # ── Step 1: Past rounds (already in DB) — zero extra API calls ──────────
            # get_cwl_attack_records_sync returns both our clan's attacks AND the
            # opponent clan's attacks (when the opponent is also tracked).  Defender
            # TH is resolved via a global player_tag → MAX(th_level) sub-query in
            # SQL, so no equal-TH fallback is needed unless the defender is in a
            # passively tracked clan (extremely rare edge case — fallback handled in DB fn).
            _db_attack_rows: List[Dict[str, Any]] = []
            if CACHE.db_manager:
                try:
                    _db_attack_rows = CACHE.db_manager.get_cwl_attack_records_sync(
                        clan_tag, season
                    )
                except Exception as _db_ex:
                    logging.debug(f"[CWL-COMP] DB attack records load failed: {_db_ex}")
            for _dbr in _db_attack_rows:
                _ptag = _dbr["player_tag"]
                _atk_th = _dbr["th_level"]
                _attack_records.append({
                    "player_tag": _ptag,
                    "attacker_th": _atk_th,
                    "defender_th": _dbr["defender_th"],
                    "stars": _dbr["stars"],
                })
                # Accumulate for debug report (OUR clan members only)
                if _dbr.get("is_our_clan"):
                    _dbg_player_info.setdefault(_ptag, [_dbr.get("player_name", _ptag), _atk_th, _dbr.get("map_position", 0)])
                    if _ptag not in _dbg_my_stats:
                        _dbg_my_stats[_ptag] = [0, 0, 0]
                    _dbg_my_stats[_ptag][0] += 1
                    _dbg_my_stats[_ptag][1] += _dbr["stars"]
                    _dbg_my_stats[_ptag][2] += _dbr["defender_th"]
                else:
                    _dbg_opp_info.setdefault(_ptag, [_dbr.get("player_name", _ptag), _atk_th, _dbr.get("map_position", 0)])
                    # Track clan grouping (first-seen insertion order)
                    _oclan = _dbr.get("opp_clan_tag", "")
                    _dbg_opp_clan.setdefault(_ptag, _oclan)
                    if _oclan and _oclan not in _dbg_opp_clan_name:
                        _dbg_opp_clan_name[_oclan] = _dbr.get("opp_clan_name", "") or _oclan
                        _dbg_opp_clan_order.append(_oclan)
                        _dbg_opp_player_order[_oclan] = []
                    if _oclan and _ptag not in _dbg_opp_player_order.get(_oclan, []):
                        _dbg_opp_player_order.setdefault(_oclan, []).append(_ptag)
                    if _ptag not in _dbg_opp_stats:
                        _dbg_opp_stats[_ptag] = [0, 0, 0]
                    _dbg_opp_stats[_ptag][0] += 1
                    _dbg_opp_stats[_ptag][1] += _dbr["stars"]
                    _dbg_opp_stats[_ptag][2] += _dbr["defender_th"]

            # ── Step 2: Live / preparation rounds (from pre-scan cache) — no API calls ─
            # _round_war_cache is populated by the pre-scan for all non-DB rounds.
            # These war objects include full member TH data for both clans.
            _db_covered: int = len(_db_season_rows)
            for _ri, _round_tags in enumerate(assigned_rounds):
                _rnum = _ri + 1
                if _rnum <= _db_covered:
                    continue  # already covered by DB above
                _war_obj_s: Any = _round_war_cache.get(_rnum)
                if _war_obj_s is None:
                    continue  # not in cache and not in DB — skip (no API call)
                _wc_s: Any = getattr(_war_obj_s, 'clan', None)
                _wo_s: Any = getattr(_war_obj_s, 'opponent', None)
                _wct_s: str = getattr(_wc_s, 'tag', '') or ''
                _my_s: Any = _wc_s if _wct_s == clan_tag else _wo_s
                _opp_s: Any = _wo_s if _wct_s == clan_tag else _wc_s
                if _my_s is None or _opp_s is None:
                    continue
                # Real defender TH lookup from opponent members
                _def_th_map_s: Dict[str, int] = {
                    getattr(_dm, 'tag', ''): int(getattr(_dm, 'town_hall', 13) or 13)
                    for _dm in list(getattr(_opp_s, 'members', []) or [])
                }
                # Our clan's attacks
                for _am in list(getattr(_my_s, 'members', []) or []):
                    _ptag_s = getattr(_am, 'tag', '')
                    _atk_th_s = int(getattr(_am, 'town_hall', 13) or 13)
                    _pname_s = normalize_player_name(str(getattr(_am, 'name', _ptag_s) or _ptag_s))
                    _dbg_player_info[_ptag_s] = [_pname_s, _atk_th_s, int(getattr(_am, 'map_position', 0) or 0)]
                    for _atk_s in list(getattr(_am, 'attacks', []) or []):
                        _def_tag_s = getattr(_atk_s, 'defender_tag', getattr(_atk_s, 'defenderTag', ''))
                        _stars_s = int(getattr(_atk_s, 'stars', 0) or 0)
                        _def_th_s = _def_th_map_s.get(_def_tag_s, _atk_th_s)
                        _attack_records.append({
                            'player_tag': _ptag_s,
                            'attacker_th': _atk_th_s,
                            'defender_th': _def_th_s,
                            'stars': _stars_s,
                        })
                        if _ptag_s not in _dbg_my_stats:
                            _dbg_my_stats[_ptag_s] = [0, 0, 0]
                        _dbg_my_stats[_ptag_s][0] += 1
                        _dbg_my_stats[_ptag_s][1] += _stars_s
                        _dbg_my_stats[_ptag_s][2] += _def_th_s
                # Opponent attacks (real TH matchup from our members as defenders)
                _my_th_map_s: Dict[str, int] = {
                    getattr(_dm, 'tag', ''): int(getattr(_dm, 'town_hall', 13) or 13)
                    for _dm in list(getattr(_my_s, 'members', []) or [])
                }
                _opp_clan_tag_s: str = getattr(_opp_s, 'tag', '') or ''
                _opp_clan_name_s: str = normalize_player_name(str(getattr(_opp_s, 'name', '') or ''))
                if _opp_clan_tag_s and _opp_clan_tag_s not in _dbg_opp_clan_name:
                    _dbg_opp_clan_name[_opp_clan_tag_s] = _opp_clan_name_s or _opp_clan_tag_s
                    _dbg_opp_clan_order.append(_opp_clan_tag_s)
                    _dbg_opp_player_order[_opp_clan_tag_s] = []
                for _am in list(getattr(_opp_s, 'members', []) or []):
                    _ptag_s = getattr(_am, 'tag', '')
                    _atk_th_s = int(getattr(_am, 'town_hall', 13) or 13)
                    _pname_opp_s = normalize_player_name(str(getattr(_am, 'name', _ptag_s) or _ptag_s))
                    _dbg_opp_info[_ptag_s] = [_pname_opp_s, _atk_th_s, int(getattr(_am, 'map_position', 0) or 0)]
                    # Track clan grouping in in-game (member list) order
                    _dbg_opp_clan.setdefault(_ptag_s, _opp_clan_tag_s)
                    if _opp_clan_tag_s and _ptag_s not in _dbg_opp_player_order.get(_opp_clan_tag_s, []):
                        _dbg_opp_player_order.setdefault(_opp_clan_tag_s, []).append(_ptag_s)
                    for _atk_s in list(getattr(_am, 'attacks', []) or []):
                        _def_tag_s = getattr(_atk_s, 'defender_tag', getattr(_atk_s, 'defenderTag', ''))
                        _stars_opp_s = int(getattr(_atk_s, 'stars', 0) or 0)
                        _def_th_opp_s = _my_th_map_s.get(_def_tag_s, _atk_th_s)
                        _attack_records.append({
                            'player_tag': _ptag_s,
                            'attacker_th': _atk_th_s,
                            'defender_th': _def_th_opp_s,
                            'stars': _stars_opp_s,
                        })
                        if _ptag_s not in _dbg_opp_stats:
                            _dbg_opp_stats[_ptag_s] = [0, 0, 0]
                        _dbg_opp_stats[_ptag_s][0] += 1
                        _dbg_opp_stats[_ptag_s][1] += _stars_opp_s
                        _dbg_opp_stats[_ptag_s][2] += _def_th_opp_s

            # ── Step 3: supplement DB history for current/upcoming opponent clans ─
            # Step 1's cross-join only reaches opponents in OUR completed war_summary
            # rows (rounds 1.._db_covered). The current in-war and preparation-round
            # opponents are not yet in war_summary (war not ended), so the cross-join
            # returns zero rows for them even if they ARE tracked in the DB.
            # Fix: directly query war_attacks for each such clan by their own clan_tag.
            if CACHE.db_manager and _dbg_opp_clan_order:
                _step3_round_map: Dict[str, int] = {
                    tag: rnum
                    for rnum, tag in {**real_war_opponents, **predicted_round_opponents}.items()
                }
                _db_direct_count: int = 0
                for _sup_clan_tag in list(_dbg_opp_clan_order):
                    # Skip clans already fully covered by the Step 1 cross-join.
                    if _step3_round_map.get(_sup_clan_tag, 0) <= _db_covered:
                        continue
                    try:
                        _sup_rows = CACHE.db_manager.get_direct_cwl_attacks_sync(  # type: ignore[union-attr]
                            _sup_clan_tag, season
                        )
                    except Exception as _sup_ex:
                        logging.debug(f"[CWL-COMP] Step3 direct fetch failed for {_sup_clan_tag}: {_sup_ex}")
                        continue
                    # Restrict to players we know from the current roster (live path).
                    _known_ptags: set[str] = {p for p, ct in _dbg_opp_clan.items() if ct == _sup_clan_tag}
                    for _sr in _sup_rows:
                        _pt = _sr["player_tag"]
                        if _pt not in _known_ptags:
                            continue  # not in current CWL roster — skip
                        _attack_records.append({
                            'player_tag': _pt,
                            'attacker_th': _sr["th_level"],
                            'defender_th': _sr["defender_th"],
                            'stars': _sr["stars"],
                        })
                        if _pt not in _dbg_opp_stats:
                            _dbg_opp_stats[_pt] = [0, 0, 0]
                        _dbg_opp_stats[_pt][0] += 1
                        _dbg_opp_stats[_pt][1] += _sr["stars"]
                        _dbg_opp_stats[_pt][2] += _sr["defender_th"]
                        _db_direct_count += 1
                if _db_direct_count:
                    logging.debug(f"[CWL-COMP] Step3 supplemented {_db_direct_count} opponent attack rows from direct DB queries")

            # ── Step 4: register predicted-only opponents (no war object in cache) ─
            # Predicted opponents (e.g. round 7 "not yet scheduled") have no war
            # object in _round_war_cache, so Step 2 never processes them and Step 3
            # skips them (they're absent from _dbg_opp_clan_order).
            # All CWL league group clans are tracked in the DB, so we query their
            # roster and attack history entirely from DB — no group_clan_map needed.
            _all_future_opponents: Dict[str, int] = {
                tag: rnum
                for rnum, tag in {**real_war_opponents, **predicted_round_opponents}.items()
                if rnum > _db_covered and tag
            }
            if CACHE.db_manager:
                for _s4_tag, _s4_rnum in _all_future_opponents.items():
                    if _s4_tag in _dbg_opp_clan_order:
                        continue  # already handled by Steps 2 or 3
                    # Roster from DB (all members incl. 0-attack ones via sentinel rows)
                    try:
                        _s4_roster = CACHE.db_manager.get_cwl_roster_sync(  # type: ignore[union-attr]
                            _s4_tag, season
                        )
                    except Exception as _s4_rex:
                        logging.debug(f"[CWL-COMP] Step4 roster fetch failed for {_s4_tag}: {_s4_rex}")
                        continue
                    if not _s4_roster:
                        continue  # not yet tracked in DB — skip
                    # Clan name: prefer group_clan_map display name if available
                    _s4_clan_obj: Any = group_clan_map.get(_s4_tag)
                    _s4_name: str = normalize_player_name(
                        str(getattr(_s4_clan_obj, 'name', '') or '') or _s4_tag
                    )
                    _dbg_opp_clan_name[_s4_tag] = _s4_name
                    _dbg_opp_clan_order.append(_s4_tag)
                    _dbg_opp_player_order[_s4_tag] = []
                    for _s4_mem in _s4_roster:
                        _s4_ptag: str = _s4_mem["player_tag"]
                        _dbg_opp_info[_s4_ptag] = [
                            normalize_player_name(_s4_mem["player_name"]),
                            _s4_mem["th_level"],
                            _s4_mem["map_position"],
                        ]
                        _dbg_opp_clan.setdefault(_s4_ptag, _s4_tag)
                        _dbg_opp_player_order[_s4_tag].append(_s4_ptag)
                    # Attack history from DB
                    try:
                        _s4_rows = CACHE.db_manager.get_direct_cwl_attacks_sync(  # type: ignore[union-attr]
                            _s4_tag, season
                        )
                    except Exception as _s4_ex:
                        logging.debug(f"[CWL-COMP] Step4 direct fetch failed for {_s4_tag}: {_s4_ex}")
                        _s4_rows = []
                    _s4_known: set[str] = set(_dbg_opp_player_order.get(_s4_tag, []))
                    for _s4r in _s4_rows:
                        _s4_pt = _s4r["player_tag"]
                        if _s4_pt not in _s4_known:
                            continue
                        _attack_records.append({
                            'player_tag': _s4_pt,
                            'attacker_th': _s4r["th_level"],
                            'defender_th': _s4r["defender_th"],
                            'stars': _s4r["stars"],
                        })
                        if _s4_pt not in _dbg_opp_stats:
                            _dbg_opp_stats[_s4_pt] = [0, 0, 0]
                        _dbg_opp_stats[_s4_pt][0] += 1
                        _dbg_opp_stats[_s4_pt][1] += _s4r["stars"]
                        _dbg_opp_stats[_s4_pt][2] += _s4r["defender_th"]
                    logging.debug(
                        f"[CWL-COMP] Step4 registered {_s4_tag} ({_s4_name}) round {_s4_rnum} "
                        f"from DB: {len(_s4_roster)} members, {len(_s4_rows)} attacks"
                    )

            if _attack_records:
                _used_league = my_league or _CWL_LEAGUE_DEFAULT
                _comp_factors = _compute_skill_v2(_attack_records, _used_league)
                player_skill_factors.update(_comp_factors)
                logging.debug(f"[CWL-COMP] {clan_tag}: {len(_comp_factors)} skill factors from {len(_attack_records)} attacks ({len(_db_attack_rows)} from DB, {len(_attack_records)-len(_db_attack_rows)} from live cache)")
                # --- Build per-player skill-factor debug table ---
                # Stored in _comp_debug_texts so generate_cwlinfo_comp_embeds can
                # retrieve it and forward it to post_discord_content_with_tracking as a
                # second message in the SAME channel as the leaderboard embeds.
                try:
                    from QBwarsim import th_star_probabilities as _th_probs_fn, th_star_probs_with_skill as _th_skill_fn  # type: ignore[attr-defined]
                    _dbg_header = (
                        f"🧪 **CWL Skill Factors — {normalize_player_name(clan_name)} ({clan_tag})**\n"
                        f"Season: **{season}** · League: **{_used_league}**\n"
                        f"Attack records: **{len(_attack_records)}** total "
                        f"({len(_db_attack_rows)} from DB, "
                        f"{len(_attack_records)-len(_db_attack_rows)} from live cache)\n"
                    )
                    # Strip everything outside printable ASCII so monospace alignment holds.
                    import re as _re
                    def _asc(n: str) -> str:
                        return _re.sub(r'[^\x20-\x7E]+', '', n).strip() or '?'

                    _rows: List[str] = []
                    _our_tags_dbg = set(_dbg_my_stats.keys())
                    for _pt, _sf in sorted(
                        ((pt, sf) for pt, sf in player_skill_factors.items() if pt in _our_tags_dbg),
                        key=lambda x: int((_dbg_player_info.get(x[0]) or ['?', 13, 0])[2])
                    ):
                        _pinfo = _dbg_player_info.get(_pt, ['?', 13, 0])
                        _pname_d, _th_d = _asc(str(_pinfo[0])), int(_pinfo[1])
                        _atk_d, _stars_d, _sum_def_d = _dbg_my_stats[_pt]
                        _avg_def_d = _sum_def_d / _atk_d if _atk_d else _th_d
                        _std_p3 = _th_probs_fn(_th_d, _th_d, _used_league)[3]
                        _adj_p3 = _th_skill_fn(_th_d, _th_d, _used_league, _sf)[3]
                        _vs18_p3 = _th_skill_fn(_th_d, 18, _used_league, _sf)[3]
                        _arrow = "↑" if _sf >= 1.0 else "↓"
                        _rows.append(
                            f"`{_pname_d[:16]:<16}` TH{_th_d:>2}  "
                            f"{_atk_d:>2} atk / Øth {_avg_def_d:.2f} / {_stars_d:>2}⭐  "
                            f"factor {_arrow}{_sf:.2f}x  "
                            f"3★ {int(_std_p3*100)}%→{int(_adj_p3*100)}%  "
                            f"vs18: {int(_vs18_p3*100)}%"
                        )
                    _opp_tags_dbg = set(_dbg_opp_stats.keys())
                    if _dbg_opp_clan_order or _opp_tags_dbg:
                        # Round-number lookup: opponent clan_tag → round (1-based).
                        # Used to skip past-round clans from the debug table.
                        _opp_round_map: Dict[str, int] = {
                            tag: rnum
                            for rnum, tag in {**real_war_opponents, **predicted_round_opponents}.items()
                        }
                        def _render_opp_row(pt: str, sf: float) -> str:
                            _pinfo_o = _dbg_opp_info.get(pt, ['?', 13, 0])
                            _pname_o, _th_o = _asc(str(_pinfo_o[0])), int(_pinfo_o[1])
                            _atk_o, _stars_o, _sum_def_o = _dbg_opp_stats.get(pt, [0, 0, _th_o])
                            _avg_def_o = _sum_def_o / _atk_o if _atk_o else _th_o
                            _std_p3_o = _th_probs_fn(_th_o, _th_o, _used_league)[3]
                            _adj_p3_o = _th_skill_fn(_th_o, _th_o, _used_league, sf)[3]
                            _vs18_p3_o = _th_skill_fn(_th_o, 18, _used_league, sf)[3]
                            _arrow_o = "↑" if sf >= 1.0 else "↓"
                            return (
                                f"`{_pname_o[:16]:<16}` TH{_th_o:>2}  "
                                f"{_atk_o:>2} atk / Øth {_avg_def_o:.2f} / {_stars_o:>2}⭐  "
                                f"factor {_arrow_o}{sf:.2f}x  "
                                f"3★ {int(_std_p3_o*100)}%→{int(_adj_p3_o*100)}%  "
                                f"vs18: {int(_vs18_p3_o*100)}%"
                            )
                        if _dbg_opp_clan_order:
                            # Grouped by clan — only current and future rounds.
                            # Past rounds (_db_covered) already contributed to the skill
                            # model; no need to display them in the debug table.
                            for _cog_tag in _dbg_opp_clan_order:
                                _cog_round = _opp_round_map.get(_cog_tag, _db_covered + 1)
                                if _cog_round <= _db_covered:
                                    continue  # past round — skip display
                                _cog_name = _asc(_dbg_opp_clan_name.get(_cog_tag, _cog_tag))[:22]
                                _fill = max(2, 48 - len(_cog_name))
                                _rows.append(f"─── {_cog_name} " + "─" * _fill)
                                # Sort players by map_position (index 2 in _dbg_opp_info).
                                # All roster members are shown — players with no attacks yet
                                # display "0 atk" with the neutral 1.0x prior factor.
                                _sorted_ptags = sorted(
                                    _dbg_opp_player_order.get(_cog_tag, []),
                                    key=lambda _p: int((_dbg_opp_info.get(_p) or ['?', 13, 0])[2])
                                )
                                for _pt in _sorted_ptags:
                                    _sf = player_skill_factors.get(_pt, 1.0)
                                    _rows.append(_render_opp_row(_pt, _sf))
                        else:
                            # Fallback: flat list sorted by skill factor (DB-only path)
                            _rows.append("─── opponents ───────────────────────────────────────")
                            for _pt, _sf in sorted(player_skill_factors.items(),
                                                   key=lambda x: x[1], reverse=True):
                                if _pt not in _opp_tags_dbg:
                                    continue
                                _rows.append(_render_opp_row(_pt, _sf))
                    _dbg_body = "\n".join(_rows) if _rows else "(no attack data found)"
                    _comp_debug_texts[clan_tag] = _dbg_header + "```\n" + _dbg_body + "\n```"
                except Exception as _dbg_ex:
                    logging.debug(f"[CWL-COMP] Debug table build failed: {_dbg_ex}")
        except Exception as _sf_ex:
            logging.debug(f"[CWL-COMP] Skill factor load failed for {clan_tag}: {_sf_ex}")

    return player_skill_factors


async def generate_cwlinfo_embeds(clan_tag: str, comp_mode: bool = False) -> List[discord.Embed]:
    """
    Generate a single Discord embed for a CWL season overview.

    All rounds are formatted as text lines in the embed description for a
    uniform single-box appearance. Falls back to archived war data when no
    active CWL league group is found.

    Args:
        clan_tag:   Tracked clan tag, e.g. '#L2J0C0PY'
        comp_mode:  When True, shows two prediction lines per round — the standard
                    TH-level model and a player-skill-adjusted model based on each
                    player's actual attack performance so far this CWL season.
    """
    # Skip the live API call if the most recent CWL season is already ended.
    # Supercell may run bonus mid-month seasons (e.g. a soccer-themed CWL), so
    # a fixed day-of-month cutoff is no longer reliable.  We rely solely on the
    # per-clan cwl_ended flag, which is set once all 7 rounds reach war_ended.
    # For clans with no DB entry (never tracked) the flag returns False
    # conservatively, so the live API call proceeds and returns a 404 if inactive.
    # Wrapped in to_thread(): this is a sync sqlite3 call (not aiosqlite) and this
    # function is awaited directly from the main leaderboard-posting cycle, so an
    # unwrapped call here blocks the event loop (Discord heartbeat/interactions)
    # for its I/O duration. CWL season is when this path is hit most often.
    if CACHE.db_manager is not None and await asyncio.to_thread(
        CACHE.db_manager.is_latest_cwl_season_ended_sync, clan_tag
    ):
        return _generate_cwlinfo_archive_embeds(clan_tag)

    try:
        league_group: Any = await CACHE.get_league_group(clan_tag)
    except Exception as lg_ex:
        logging.info(f"[CWL-INFO] Could not fetch league group for {clan_tag}: {lg_ex}")
        return _generate_cwlinfo_archive_embeds(clan_tag)

    if not league_group:
        return _generate_cwlinfo_archive_embeds(clan_tag)

    clan_name: str = CACHE.get_clan_name(clan_tag, clan_tag) or clan_tag
    my_clan_url: str = coc_clan_profile_url(clan_tag)
    season: str = str(getattr(league_group, 'season', '?') or '?')
    my_league: str = _resolve_war_league(clan_tag)
    league_hdr: str = f" \u00b7 {my_league}" if my_league else ""

    group_clan_map: Dict[str, Any] = {}
    for cwl_clan in list(getattr(league_group, 'clans', []) or []):
        g_tag: str = getattr(cwl_clan, 'tag', '')
        if g_tag:
            group_clan_map[g_tag] = cwl_clan

    # Validity check: if season is unknown or the group has fewer than 2 clans the API
    # returned an incomplete/stale object (sign-up phase before group formation completes).
    # Fall back to archive embeds so the last completed CWL is shown instead.
    if season == '?' or len(group_clan_map) < 2:
        logging.info(
            f"[CWL-INFO] League group for {clan_tag} is incomplete "
            f"(season='{season}', clans={len(group_clan_map)}) — falling back to archive"
        )
        return _generate_cwlinfo_archive_embeds(clan_tag)

    our_eligible: Any = group_clan_map.get(clan_tag)
    our_eligible_members: List[Any] = list(getattr(our_eligible, 'members', []) or []) if our_eligible else []
    # league_group.rounds is List[List[str]] filtered to only rounds with real war tags by coc.py;
    # number_of_rounds is the full count (7) before filtering.
    total_rounds: int = int(getattr(league_group, 'number_of_rounds', 7) or 7)
    assigned_rounds: List[List[str]] = list(getattr(league_group, 'rounds', []) or [])

    # Fire-and-forget: add all 7 group clans to tracking DB (harvest runs only for
    # subscribed clans in top-5 leagues; safe to call every render cycle).
    asyncio.ensure_future(_harvest_cwl_group_clans(clan_tag, None, league_group=league_group))
    # Piggy-back: refresh CWL group standings (nearly free — league_group already fetched).
    asyncio.ensure_future(update_cwl_group_stats(clan_tag, season))

    # -- Load past-round data from DB (zero API calls for warended rounds). --
    # get_war_summaries_sync returns rows ordered by date ASC, mapping 1:1 to
    # rounds 1, 2, ... (N-1) where N = len(assigned_rounds) = the live round.
    _db_season_rows: List[Dict[str, Any]] = []
    if CACHE.db_manager:
        try:
            _all_cwl = CACHE.db_manager.get_war_summaries_sync(clan_tag, is_cwl=True)
            _db_season_rows = [r for r in _all_cwl if str(r.get('cwl_season') or '') == season]
        except Exception as _dex:
            logging.debug(f"[CWL-INFO] DB season rows load failed: {_dex}")

    # -- Pre-scan: populate real_war_opponents from DB + fetch ONLY the live round. --
    # The live round is always the LAST element of assigned_rounds.
    # Past rounds are covered by _db_season_rows — no API calls for them.
    real_war_opponents: Dict[int, str] = {}  # round_idx (1-based) → opponent clan tag
    real_team_size: int = 0
    # Track our actual used roster from the latest real round — best predictor for future rounds.
    last_real_my_members: List[Any] = []  # members from most recent real round (our side)
    _round_war_cache: Dict[int, Any] = {}  # round_idx (1-based) → our coc.ClanWar object

    # Seed real_war_opponents + real_team_size from DB rows (past rounds, no API needed).
    for _db_i, _db_row in enumerate(_db_season_rows):
        real_war_opponents[_db_i + 1] = str(_db_row.get('opponent_tag') or '')
        if not real_team_size:
            real_team_size = int(_db_row.get('team_size', 0) or 0)

    # Fetch all assigned rounds not yet covered by DB rows.
    # A round may be missing from DB if the war is still in-progress or preparation
    # (not yet committed as ended).  When a new round enters preparation, the in-war
    # round is no longer the last element of assigned_rounds, so we must iterate all
    # non-DB rounds — otherwise the in-war opponent is excluded from _used_in_real and
    # mistakenly re-assigned as a predicted future-round opponent.
    _db_covered_rounds: int = len(_db_season_rows)  # rounds 1.._db_covered_rounds have DB data
    for _ri_ps, _round_tags_ps in enumerate(assigned_rounds):
        _rnum_ps: int = _ri_ps + 1
        if _rnum_ps <= _db_covered_rounds:
            continue  # already seeded from DB
        _live_tags: List[str] = [wt for wt in _round_tags_ps if wt and wt not in ('#0', '0')]
        for _wt in _live_tags:
            try:
                _pw: Any = await CACHE.get_league_war(_wt)
                if not _pw:
                    continue
                _pw_clan: Any = getattr(_pw, 'clan', None)
                _pw_opp: Any = getattr(_pw, 'opponent', None)
                _pc: str = getattr(_pw_clan, 'tag', '') or ''
                _po: str = getattr(_pw_opp, 'tag', '') or ''
                _popp: str = _po if _pc == clan_tag else (_pc if _po == clan_tag else '')
                if not real_team_size:
                    real_team_size = int(getattr(_pw, 'team_size', 0) or 0)
                if _popp:
                    real_war_opponents[_rnum_ps] = _popp
                    _round_war_cache[_rnum_ps] = _pw
                    _my_side: Any = _pw_clan if _pc == clan_tag else _pw_opp
                    _mems: List[Any] = list(getattr(_my_side, 'members', []) or [])
                    if _mems:
                        last_real_my_members = _mems
                    break  # found our war for this round — move to next round
            except Exception:
                pass

    # -- Assign predicted opponents for unscheduled rounds --
    # Each of the 8 group clans plays every other clan exactly once across 7 rounds.
    all_group_opponents: List[str] = [t for t in group_clan_map if t != clan_tag]
    _used_in_real: Set[str] = set(real_war_opponents.values())
    _remaining_opps: List[str] = [t for t in all_group_opponents if t not in _used_in_real]
    # Deterministic shuffle: same seed per clan+season so the order is stable across re-renders.
    import random as _random
    _rng = _random.Random(hash(clan_tag + season) & 0xFFFFFFFF)
    _rng.shuffle(_remaining_opps)
    _pred_opp_iter = iter(_remaining_opps)
    # Infer team_size for predicted rounds (CWL sizes: 5, 10, 11, 15, 30).
    if not real_team_size:
        _ec = len(our_eligible_members)
        for _n in (5, 10, 11, 15, 30):
            if _ec <= _n:
                real_team_size = _n
                break
        else:
            real_team_size = 30
    predicted_round_opponents: Dict[int, str] = {}
    for _pri in range(1, total_rounds + 1):
        if _pri not in real_war_opponents:
            predicted_round_opponents[_pri] = next(_pred_opp_iter, '')

    # Hoist here so it's available in the predicted-round block (before real war data is found)
    my_name_disp: str = normalize_player_name(clan_name)

    # Compute per-player skill factors (may populate _comp_debug_texts in comp_mode).
    player_skill_factors: Dict[str, float] = await _cwl_compute_skill_factors(
        clan_tag, clan_name, season, my_league,
        group_clan_map, assigned_rounds, _db_season_rows, _round_war_cache,
        real_war_opponents, predicted_round_opponents, comp_mode,
    )

    desc_lines: List[str] = [
        f"CWL Season **{season}**{league_hdr}\n"
    ]

    for round_idx in range(1, total_rounds + 1):
        # Each element of assigned_rounds is already List[str] of war tags for that round.
        round_war_tags: List[str] = assigned_rounds[round_idx - 1] if round_idx - 1 < len(assigned_rounds) else []
        valid_tags: List[str] = [wt for wt in round_war_tags if wt and wt not in ('#0', '0')]

        if not valid_tags:
            pred_opp_tag: str = predicted_round_opponents.get(round_idx, '')
            if pred_opp_tag:
                pred_opp_obj: Any = group_clan_map.get(pred_opp_tag)
                pred_opp_name: str = normalize_player_name(
                    getattr(pred_opp_obj, 'name', pred_opp_tag) or pred_opp_tag
                )
                pred_opp_url: str = coc_clan_profile_url(pred_opp_tag) if pred_opp_tag else ""
                pred_apm: int = 1  # CWL always 1 attack per member
                pred_n: int = real_team_size
                # Use last known real roster if available — best predictor for future rounds.
                # Fall back to eligible members sorted by TH only when no real war data exists yet.
                if last_real_my_members:
                    pred_my_roster: List[Any] = last_real_my_members
                else:
                    pred_my_roster = sorted(
                        our_eligible_members, key=lambda m: getattr(m, 'town_hall', 0), reverse=True
                    )[:pred_n]
                pred_opp_elig: List[Any] = list(getattr(pred_opp_obj, 'members', []) or []) if pred_opp_obj else []
                pred_opp_roster: List[Any] = sorted(
                    pred_opp_elig, key=lambda m: getattr(m, 'town_hall', 0), reverse=True
                )[:pred_n]
                pred_my_lineup: str = _build_cwl_lineup(pred_my_roster, pred_n, ascending=True)
                pred_opp_lineup: str = _build_cwl_lineup(pred_opp_roster, pred_n)
                desc_lines.append(f"**Round {round_idx}**  \u00b7  \u23f3 Not yet scheduled  \u00b7  *(predicted opponent)*")
                desc_lines.append(
                    f"vs. [\u200e{pred_opp_name}\u200e]({pred_opp_url})  \u200e`{pred_opp_tag}`"
                    if pred_opp_url else f"vs. \u200e{pred_opp_name}\u200e  \u200e`{pred_opp_tag}`"
                )
                if pred_my_lineup or pred_opp_lineup:
                    desc_lines.append(f"`{my_name_disp}`  {pred_my_lineup}\u2003vs\u2003{pred_opp_lineup}  `{pred_opp_name}`")
                pred_proxy = _build_cwl_war_proxy(clan_tag, pred_my_roster, pred_opp_tag, pred_opp_roster, pred_apm)
                await _cwl_append_prediction(
                    desc_lines, pred_proxy, clan_tag,
                    pred_n * pred_apm, 0, 0, pred_n * pred_apm,
                    my_league or _CWL_LEAGUE_DEFAULT, comp_mode, player_skill_factors,
                    round_idx, 'predicted',
                    cwl_league_opp=_resolve_war_league(pred_opp_tag) or my_league or _CWL_LEAGUE_DEFAULT,
                )
            else:
                # No opponents left to assign (shouldn't normally happen in an 8-clan group)
                desc_lines.append(f"**Round {round_idx}**  \u00b7  \u23f3 Not yet scheduled")
                if our_eligible_members:
                    elig_sorted = sorted(our_eligible_members, key=lambda m: getattr(m, 'town_hall', 0), reverse=True)
                    lineup = _build_cwl_lineup(elig_sorted)
                    if lineup:
                        desc_lines.append(f"Eligible roster:  {lineup}")
            desc_lines.append("")
            continue

        # Shortcut for warended past rounds: render from DB without any API call.
        # Past rounds = all assigned rounds except the last one (the live round).
        _live_round_threshold: int = len(assigned_rounds)
        _db_past_row: Optional[Dict[str, Any]] = (
            _db_season_rows[round_idx - 1]
            if round_idx < _live_round_threshold and round_idx - 1 < len(_db_season_rows)
            else None
        )
        if _db_past_row is not None:
            _r_my_stars: int = int(_db_past_row.get('clan_stars', 0) or 0)
            _r_opp_stars: int = int(_db_past_row.get('opponent_stars', 0) or 0)
            _r_opp_tag: str = str(_db_past_row.get('opponent_tag', '') or '')
            _r_opp_name: str = normalize_player_name(str(_db_past_row.get('opponent_name', 'Opponent') or 'Opponent'))
            _r_my_dest: float = float(_db_past_row.get('clan_destruction', 0.0) or 0.0)
            _r_opp_dest: float = float(_db_past_row.get('opp_destruction', 0.0) or 0.0)
            _r_opp_url: str = coc_clan_profile_url(_r_opp_tag) if _r_opp_tag else ""
            _r_result_lbl = classify_war_result(_r_my_stars, _r_opp_stars, _r_my_dest, _r_opp_dest)
            desc_lines.append(f"**Round {round_idx}**  \u00b7  {_r_result_lbl}  `{_r_my_stars}\u2b50 \u2013 {_r_opp_stars}\u2b50 \u00b7 {_r_my_dest:.1f}% \u2013 {_r_opp_dest:.1f}%`")
            if _r_opp_url:
                desc_lines.append(f"vs. [\u200e{_r_opp_name}\u200e]({_r_opp_url})  \u200e`{_r_opp_tag}`")
            else:
                desc_lines.append(f"vs. \u200e{_r_opp_name}\u200e  \u200e`{_r_opp_tag}`")
            _r_my_lineup: str = _lineup_from_json_r(str(_db_past_row.get('clan_lineup_json') or '[]'), ascending=True)
            _r_opp_lineup: str = _lineup_from_json_r(str(_db_past_row.get('opp_lineup_json') or '[]'), ascending=False)
            if _r_my_lineup or _r_opp_lineup:
                desc_lines.append(f"`{my_name_disp}`  {_r_my_lineup}\u2003vs\u2003{_r_opp_lineup}  `{_r_opp_name}`")
            desc_lines.append("")
            continue

        our_war: Any = None
        our_war_my_side: Any = None
        our_war_opp_side: Any = None
        # Use pre-scan cache (live round, fetched above) to avoid any further API calls.
        _cached_war: Any = _round_war_cache.get(round_idx)
        if _cached_war is not None:
            _cw_clan: Any = getattr(_cached_war, 'clan', None)
            _cw_opp: Any = getattr(_cached_war, 'opponent', None)
            _cw_ct: str = getattr(_cw_clan, 'tag', '')
            if clan_tag == _cw_ct:
                our_war = _cached_war; our_war_my_side = _cw_clan; our_war_opp_side = _cw_opp
            else:
                our_war = _cached_war; our_war_my_side = _cw_opp; our_war_opp_side = _cw_clan
        else:
            for wt in valid_tags:
                try:
                    w: Any = await CACHE.get_league_war(wt)
                    if not w:
                        continue
                    w_clan: Any = getattr(w, 'clan', None)
                    w_opp: Any = getattr(w, 'opponent', None)
                    c_tag: str = getattr(w_clan, 'tag', '')
                    o_tag: str = getattr(w_opp, 'tag', '')
                    if clan_tag == c_tag:
                        our_war = w; our_war_my_side = w_clan; our_war_opp_side = w_opp; break
                    elif clan_tag == o_tag:
                        our_war = w; our_war_my_side = w_opp; our_war_opp_side = w_clan; break
                except coc.PrivateWarLog:  # type: ignore[attr-defined]
                    continue
                except Exception as wex:
                    logging.debug(f"[CWL-INFO] Failed to fetch war {wt}: {wex}")
                    continue

        if our_war is None:
            desc_lines.append(f"**Round {round_idx}**  \u00b7  \u2753 War data unavailable")
            desc_lines.append("")
            continue

        opp_tag_r: str = getattr(our_war_opp_side, 'tag', '')
        opp_name_r: str = normalize_player_name(getattr(our_war_opp_side, 'name', 'Opponent'))
        my_stars_r: int = int(getattr(our_war_my_side, 'stars', 0) or 0)
        opp_stars_r: int = int(getattr(our_war_opp_side, 'stars', 0) or 0)
        team_size_r: int = int(getattr(our_war, 'team_size', 15) or 15)
        state_raw: Any = getattr(our_war, 'state', None)
        state_str_e: str = str(
            getattr(state_raw, 'name', state_raw) if state_raw else ''
        ).lower().replace('_', '').replace(' ', '')
        my_war_mems: List[Any] = list(getattr(our_war_my_side, 'members', []) or [])
        opp_war_mems: List[Any] = list(getattr(our_war_opp_side, 'members', []) or [])
        my_name_disp: str = normalize_player_name(clan_name)
        opp_url_r: str = coc_clan_profile_url(opp_tag_r) if opp_tag_r else ""
        my_dest_r: float = float(getattr(our_war_my_side, 'destruction', 0.0) or 0.0)
        opp_dest_r: float = float(getattr(our_war_opp_side, 'destruction', 0.0) or 0.0)
        my_lineup = _build_cwl_lineup(my_war_mems, team_size_r, ascending=True)
        opp_lineup = _build_cwl_lineup(opp_war_mems, team_size_r)

        if state_str_e == 'warended':
            result_lbl = classify_war_result(my_stars_r, opp_stars_r, my_dest_r, opp_dest_r)
            desc_lines.append(f"**Round {round_idx}**  \u00b7  {result_lbl}  `{my_stars_r}\u2b50 – {opp_stars_r}\u2b50 \u00b7 {my_dest_r:.1f}% – {opp_dest_r:.1f}%`")
            desc_lines.append(f"vs. [\u200e{opp_name_r}\u200e]({opp_url_r})  \u200e`{opp_tag_r}`" if opp_url_r else f"vs. \u200e{opp_name_r}\u200e  \u200e`{opp_tag_r}`")
            if my_lineup or opp_lineup:
                desc_lines.append(f"`{my_name_disp}`  {my_lineup}\u2003vs\u2003{opp_lineup}  `{opp_name_r}`")

        elif state_str_e == 'inwar':
            end_time_obj: Any = getattr(our_war, 'end_time', None)
            end_secs: Optional[int] = getattr(end_time_obj, 'seconds_until', None) if end_time_obj else None
            desc_lines.append(f"**Round {round_idx}**  \u00b7  \u2694\ufe0f In War  `{my_stars_r}\u2b50 – {opp_stars_r}\u2b50 \u00b7 {my_dest_r:.1f}% – {opp_dest_r:.1f}%`  \u00b7  Ends in: `{_fmt_secs(end_secs)}`")
            desc_lines.append(f"vs. [\u200e{opp_name_r}\u200e]({opp_url_r})  \u200e`{opp_tag_r}`" if opp_url_r else f"vs. \u200e{opp_name_r}\u200e  \u200e`{opp_tag_r}`")
            if my_lineup or opp_lineup:
                desc_lines.append(f"`{my_name_disp}`  {my_lineup}\u2003vs\u2003{opp_lineup}  `{opp_name_r}`")
            apm_r: int = int(getattr(our_war, 'attacks_per_member', 1) or 1)
            def _cnt(mlist: List[Any]) -> int:
                return sum(len(list(getattr(m, 'attacks', []) or [])) for m in mlist)
            my_atk_left_r = max(team_size_r * apm_r - _cnt(my_war_mems), 0)
            opp_atk_left_r = max(team_size_r * apm_r - _cnt(opp_war_mems), 0)
            proxy_r = _build_cwl_war_proxy(
                clan_tag, my_war_mems, opp_tag_r, opp_war_mems, apm_r,
                my_stars=my_stars_r, opp_stars=opp_stars_r, include_attacks=True,
                my_destruction=my_dest_r, opp_destruction=opp_dest_r,
            )
            await _cwl_append_prediction(
                desc_lines, proxy_r, clan_tag,
                my_atk_left_r, my_stars_r, opp_stars_r, opp_atk_left_r,
                my_league or _CWL_LEAGUE_DEFAULT, comp_mode, player_skill_factors,
                round_idx, 'inwar',
                cwl_league_opp=_resolve_war_league(opp_tag_r) or my_league or _CWL_LEAGUE_DEFAULT,
            )

        elif state_str_e == 'preparation':
            start_time_obj: Any = getattr(our_war, 'start_time', None)
            start_secs: Optional[int] = getattr(start_time_obj, 'seconds_until', None) if start_time_obj else None
            my_roster_p: List[Any] = my_war_mems if my_war_mems else sorted(
                our_eligible_members, key=lambda m: getattr(m, 'town_hall', 0), reverse=True
            )[:team_size_r]
            if opp_war_mems:
                opp_roster_p: List[Any] = opp_war_mems
            else:
                opp_elig_obj: Any = group_clan_map.get(opp_tag_r)
                opp_elig_mems: List[Any] = list(getattr(opp_elig_obj, 'members', []) or []) if opp_elig_obj else []
                opp_roster_p = sorted(opp_elig_mems, key=lambda m: getattr(m, 'town_hall', 0), reverse=True)[:team_size_r]
            my_prep_lineup = _build_cwl_lineup(my_roster_p, team_size_r, ascending=True)
            opp_prep_lineup = _build_cwl_lineup(opp_roster_p, team_size_r)
            desc_lines.append(f"**Round {round_idx}**  \u00b7  \U0001f4c5 Preparation  \u00b7  Starts in: `{_fmt_secs(start_secs)}`")
            desc_lines.append(f"vs. [\u200e{opp_name_r}\u200e]({opp_url_r})  \u200e`{opp_tag_r}`" if opp_url_r else f"vs. \u200e{opp_name_r}\u200e  \u200e`{opp_tag_r}`")
            if my_prep_lineup or opp_prep_lineup:
                desc_lines.append(f"`{my_name_disp}`  {my_prep_lineup}\u2003vs\u2003{opp_prep_lineup}  `{opp_name_r}`")
            apm_p: int = int(getattr(our_war, 'attacks_per_member', 1) or 1)
            proxy_p = _build_cwl_war_proxy(clan_tag, my_roster_p, opp_tag_r, opp_roster_p, apm_p)
            await _cwl_append_prediction(
                desc_lines, proxy_p, clan_tag,
                team_size_r * apm_p, 0, 0, team_size_r * apm_p,
                my_league or _CWL_LEAGUE_DEFAULT, comp_mode, player_skill_factors,
                round_idx, 'prep',
                cwl_league_opp=_resolve_war_league(opp_tag_r) or my_league or _CWL_LEAGUE_DEFAULT,
            )

        elif state_str_e == 'notinwar':
            # Future CWL round: scheduled and opponent is known, but battle hasn't started yet.
            # Build roster from eligible members in the league group (same fallback as preparation).
            my_sched_roster: List[Any] = my_war_mems if my_war_mems else sorted(
                our_eligible_members, key=lambda m: getattr(m, 'town_hall', 0), reverse=True
            )[:team_size_r]
            opp_sched_roster: List[Any] = opp_war_mems
            if not opp_sched_roster:
                opp_sched_obj: Any = group_clan_map.get(opp_tag_r)
                opp_sched_elig: List[Any] = list(getattr(opp_sched_obj, 'members', []) or []) if opp_sched_obj else []
                opp_sched_roster = sorted(opp_sched_elig, key=lambda m: getattr(m, 'town_hall', 0), reverse=True)[:team_size_r]
            my_sched_lineup = _build_cwl_lineup(my_sched_roster, team_size_r, ascending=True)
            opp_sched_lineup = _build_cwl_lineup(opp_sched_roster, team_size_r)
            desc_lines.append(f"**Round {round_idx}**  \u00b7  \U0001f4c5 Scheduled")
            desc_lines.append(f"vs. [\u200e{opp_name_r}\u200e]({opp_url_r})  \u200e`{opp_tag_r}`" if opp_url_r else f"vs. \u200e{opp_name_r}\u200e  \u200e`{opp_tag_r}`")
            if my_sched_lineup or opp_sched_lineup:
                desc_lines.append(f"`{my_name_disp}`  {my_sched_lineup}\u2003vs\u2003{opp_sched_lineup}  `{opp_name_r}`")
            apm_sched: int = int(getattr(our_war, 'attacks_per_member', 1) or 1)
            proxy_sched = _build_cwl_war_proxy(clan_tag, my_sched_roster, opp_tag_r, opp_sched_roster, apm_sched)
            await _cwl_append_prediction(
                desc_lines, proxy_sched, clan_tag,
                team_size_r * apm_sched, 0, 0, team_size_r * apm_sched,
                my_league or _CWL_LEAGUE_DEFAULT, comp_mode, player_skill_factors,
                round_idx, 'scheduled',
                cwl_league_opp=_resolve_war_league(opp_tag_r) or my_league or _CWL_LEAGUE_DEFAULT,
            )

        else:
            desc_lines.append(f"**Round {round_idx}**  \u00b7  State: {state_str_e or '?'}")
            desc_lines.append(f"vs. [\u200e{opp_name_r}\u200e]({opp_url_r})  \u200e`{opp_tag_r}`" if opp_url_r else f"vs. \u200e{opp_name_r}\u200e  \u200e`{opp_tag_r}`")

        desc_lines.append("")  # blank line between rounds

    return _desc_lines_to_embeds(
        desc_lines,
        title=f"\u2b50 {normalize_player_name(clan_name)} ({clan_tag})",
        url=my_clan_url,
        color=0xf0bc44,
    )


def _split_discord_message(content: str, max_len: int = 2000) -> List[str]:
    """Split *content* at newline boundaries so every chunk fits in max_len.

    Code-block fences (```) are detected and re-opened/closed across chunk
    boundaries so the resulting chunks are always valid Discord markdown.
    """
    if len(content) <= max_len:
        return [content]

    chunks: List[str] = []
    in_code_block = False
    current: str = ""

    for line in content.split("\n"):
        is_fence = line.startswith("```")
        addition = ("\n" if current else "") + line

        if current and len(current) + len(addition) > max_len:
            # Close the code block before ending the chunk if needed.
            if in_code_block:
                current += "\n```"
            chunks.append(current)
            # Re-open the code block for the next chunk if needed.
            if in_code_block and not is_fence:
                current = "```\n" + line
            else:
                current = line
        else:
            current += addition

        if is_fence:
            in_code_block = not in_code_block

    if current:
        chunks.append(current)

    return chunks


async def generate_cwlinfo_comp_embeds(clan_tag: str) -> Tuple[List[discord.Embed], str]:
    """Wrapper: CWL info embeds with dual prediction lines (standard + player-skill-adjusted).

    Returns:
        (embeds, debug_text) — debug_text is the per-player skill-factor table
        to be sent as a second message alongside the leaderboard embeds.  Empty
        string when no skill data was available.
    """
    embeds = await generate_cwlinfo_embeds(clan_tag, comp_mode=True)
    debug_text = _comp_debug_texts.pop(clan_tag, '')
    return embeds, debug_text


def _load_cwl_wars_from_db_sync(
    war_tags: List[str],
) -> Tuple[Dict[str, Any], Dict[str, Any], Set[str]]:
    """
    Load per-attack data for specific CWL war tags from war_attacks / war_summary.

    Used as a last-resort fill-in for wars the live CoC API failed to return
    usable attack data for (see generate_cwl_group_analysis_embeds). Unlike
    _load_cwl_analysis_from_db_sync this looks up by war_tag rather than by
    clan_tag + season, so it also picks up wars still in progress: war_summary
    / war_attacks are kept current by this bot's periodic tracking for any
    clan it actively follows, including partial attack data recorded mid-war.

    Returns (attacker_data, defender_data, tags_with_data) — tags_with_data is
    the subset of *war_tags* for which at least one attack row was found.
    """
    empty: Tuple[Dict[str, Any], Dict[str, Any], Set[str]] = ({}, {}, set())
    if not war_tags or not CACHE.db_manager or not CACHE.db_manager.db_path:
        return empty

    try:
        with CACHE.db_manager.sync_conn() as conn:
            ph = ",".join("?" * len(war_tags))
            ws_rows = conn.execute(
                f"SELECT DISTINCT war_id, clan_tag, war_tag FROM war_summary "
                f"WHERE war_tag IN ({ph}) AND is_cwl = 1",
                list(war_tags),
            ).fetchall()
            if not ws_rows:
                return empty
            war_id_to_tag: Dict[str, str] = {str(r["war_id"]): str(r["war_tag"]) for r in ws_rows}
            war_ids = {str(r["war_id"]) for r in ws_rows}

            wid_ph = ",".join("?" * len(war_ids))
            atk_rows = conn.execute(
                f"""
                SELECT wa.war_id, wa.player_tag, wa.player_name, wa.th_level, wa.clan_tag,
                       wa.stars, wa.defender_tag, wa.defender_map_position
                FROM war_attacks wa
                WHERE wa.war_id IN ({wid_ph}) AND wa.attack_order > 0
                """,
                list(war_ids),
            ).fetchall()
    except Exception as exc:
        logging.error(f"[CWL-ANALYSE-DB] Per-war DB lookup failed for {war_tags}: {exc}")
        return empty

    if not atk_rows:
        return empty

    # Same roster-slot normalization as _load_cwl_analysis_from_db_sync below.
    war_side_raw: Dict[Tuple[str, str], Dict[str, int]] = {}
    for r in atk_rows:
        key = (str(r["war_id"]), str(r["clan_tag"]))
        raw_pos = int(r["defender_map_position"] or 0)
        def_tag = str(r["defender_tag"] or "")
        if raw_pos > 0 and def_tag:
            war_side_raw.setdefault(key, {})[def_tag] = raw_pos
    normalized_def_pos: Dict[Tuple[str, str, str], int] = {}
    for (war_id, clan_t), pos_map in war_side_raw.items():
        for rank, (dtag, _) in enumerate(sorted(pos_map.items(), key=lambda kv: kv[1]), start=1):
            normalized_def_pos[(war_id, clan_t, dtag)] = rank

    player_info: Dict[str, Dict[str, Any]] = {}
    for r in atk_rows:
        t = r["player_tag"]
        if t and t not in player_info:
            player_info[t] = {"name": r["player_name"], "th": int(r["th_level"]), "clan": r["clan_tag"]}

    attacker_data: Dict[str, Dict[str, Any]] = {}
    defender_data: Dict[str, Dict[str, Any]] = {}
    tags_with_data: Set[str] = set()
    for r in atk_rows:
        war_id_str = str(r["war_id"])
        wtag = war_id_to_tag.get(war_id_str, '')
        if wtag:
            tags_with_data.add(wtag)
        atk_tag = r["player_tag"]
        def_tag = str(r["defender_tag"] or "")
        stars = int(r["stars"])
        clan_tag_str = str(r["clan_tag"])
        def_map_pos = normalized_def_pos.get(
            (war_id_str, clan_tag_str, def_tag), int(r["defender_map_position"] or 0)
        )

        if atk_tag not in attacker_data:
            attacker_data[atk_tag] = {
                "name": r["player_name"], "clan_tag": r["clan_tag"], "th": int(r["th_level"]),
                "attacks": 0, "total_stars": 0, "map_pos_sum": 0,
            }
        ad = attacker_data[atk_tag]
        ad["attacks"] += 1
        ad["total_stars"] += stars
        if def_map_pos > 0:
            ad["map_pos_sum"] += def_map_pos
        if int(r["th_level"]) > 0:
            ad["th"] = int(r["th_level"])

        if def_tag:
            if def_tag not in defender_data:
                pinfo = player_info.get(def_tag, {})
                defender_data[def_tag] = {
                    "name": pinfo.get("name", def_tag), "clan_tag": pinfo.get("clan", ""),
                    "th": pinfo.get("th", 0), "defenses": 0, "stars_conceded": 0,
                }
            dd = defender_data[def_tag]
            dd["defenses"] += 1
            dd["stars_conceded"] += stars

    return attacker_data, defender_data, tags_with_data


def _load_cwl_analysis_from_db_sync(
    clan_tag: str,
) -> Tuple[Dict[str, Any], Dict[str, Any], str, int, int, Set[str]]:
    """
    Load per-attack CWL data for the most recent CWL season of ``clan_tag``
    from the war_attacks / war_summary tables.

    Returns (attacker_data, defender_data, season, n_group_clans, n_wars, tracked_clan_tags)
    where each data dict has the same shape as the live-API path in
    generate_cwl_group_analysis_embeds.
    """
    empty: Tuple[Dict[str, Any], Dict[str, Any], str, int, int, Set[str]] = ({}, {}, '', 0, 0, set())

    if not CACHE.db_manager or not CACHE.db_manager.db_path:
        return empty

    try:
        with CACHE.db_manager.sync_conn() as conn:
            # Latest CWL season for this clan
            row = conn.execute(
                "SELECT cwl_season FROM war_summary WHERE clan_tag = ? AND is_cwl = 1 "
                "AND cwl_season != '' ORDER BY cwl_season DESC LIMIT 1",
                (clan_tag,),
            ).fetchone()
            if not row:
                return empty
            season = row["cwl_season"]

            # Derive group: our clan + all 7 opponents from war_summary
            opp_rows = conn.execute(
                "SELECT DISTINCT opponent_tag FROM war_summary "
                "WHERE clan_tag = ? AND cwl_season = ? AND is_cwl = 1",
                (clan_tag, season),
            ).fetchall()
            group_clan_tags: Set[str] = {clan_tag} | {str(r["opponent_tag"]) for r in opp_rows}
            n_group_clans = len(group_clan_tags)

            # Tracked clans in this group (those that have DB rows)
            ph = ",".join("?" * len(group_clan_tags))
            tracked_in_db = conn.execute(
                f"SELECT DISTINCT clan_tag FROM war_summary "
                f"WHERE cwl_season = ? AND is_cwl = 1 AND clan_tag IN ({ph})",
                [season] + list(group_clan_tags),
            ).fetchall()
            tracked_clan_tags: Set[str] = {str(r["clan_tag"]) for r in tracked_in_db}
            if not tracked_clan_tags:
                return empty

            # Wars recorded (from our clan's perspective to avoid double-counting)
            n_wars_row = conn.execute(
                "SELECT COUNT(DISTINCT war_id) AS cnt FROM war_summary "
                "WHERE clan_tag = ? AND cwl_season = ? AND is_cwl = 1",
                (clan_tag, season),
            ).fetchone()
            n_wars = int(n_wars_row["cnt"] if n_wars_row else 0)

            # Individual attack rows for all tracked clans
            tph = ",".join("?" * len(tracked_clan_tags))
            atk_rows = conn.execute(
                f"""
                SELECT wa.war_id, wa.player_tag, wa.player_name, wa.th_level, wa.clan_tag,
                       wa.stars, wa.defender_tag, wa.defender_map_position
                FROM war_attacks wa
                JOIN war_summary ws ON wa.war_id = ws.war_id AND wa.clan_tag = ws.clan_tag
                WHERE ws.cwl_season = ? AND ws.is_cwl = 1
                  AND wa.clan_tag IN ({tph})
                  AND wa.attack_order > 0
                """,
                [season] + list(tracked_clan_tags),
            ).fetchall()

        # Process results outside the connection context (read-only data already fetched)
        # Build per-(war_id, attacker_clan) normalized defender rank map.
        # The CoC API stores mapPosition as the league group roster slot (1-30), not the
        # 1-N war lineup rank. Normalize by sorting all seen defenders per war side by
        # their raw position, then assigning sequential ranks 1-N (1 = strongest).
        # Key: (war_id, attacker_clan_tag) → {defender_tag: normalized_rank}
        war_side_raw: Dict[Tuple[str, str], Dict[str, int]] = {}
        for r in atk_rows:
            key = (str(r["war_id"]), str(r["clan_tag"]))
            raw_pos = int(r["defender_map_position"] or 0)
            def_tag = str(r["defender_tag"] or "")
            if raw_pos > 0 and def_tag:
                war_side_raw.setdefault(key, {})[def_tag] = raw_pos
        # Build normalized rank lookup
        normalized_def_pos: Dict[Tuple[str, str, str], int] = {}
        for (war_id, clan_t), pos_map in war_side_raw.items():
            for rank, (dtag, _) in enumerate(
                sorted(pos_map.items(), key=lambda kv: kv[1]), start=1
            ):
                normalized_def_pos[(war_id, clan_t, dtag)] = rank

        # Build player info for defender lookups (from attacker rows)
        player_info: Dict[str, Dict[str, Any]] = {}
        for r in atk_rows:
            t = r["player_tag"]
            if t and t not in player_info:
                player_info[t] = {"name": r["player_name"], "th": int(r["th_level"]), "clan": r["clan_tag"]}

        attacker_data: Dict[str, Dict[str, Any]] = {}
        defender_data: Dict[str, Dict[str, Any]] = {}
        for r in atk_rows:
            atk_tag = r["player_tag"]
            def_tag = str(r["defender_tag"] or "")
            stars = int(r["stars"])
            war_id_str = str(r["war_id"])
            clan_tag_str = str(r["clan_tag"])
            # Use normalized war rank (1-N within each war side) instead of raw roster slot
            def_map_pos = normalized_def_pos.get(
                (war_id_str, clan_tag_str, def_tag),
                int(r["defender_map_position"] or 0),
            )

            if atk_tag not in attacker_data:
                attacker_data[atk_tag] = {
                    "name": r["player_name"], "clan_tag": r["clan_tag"],
                    "th": int(r["th_level"]),
                    "attacks": 0, "total_stars": 0, "map_pos_sum": 0,
                }
            ad = attacker_data[atk_tag]
            ad["attacks"] += 1
            ad["total_stars"] += stars
            if def_map_pos > 0:
                ad["map_pos_sum"] += def_map_pos
            if int(r["th_level"]) > 0:
                ad["th"] = int(r["th_level"])
            if r["player_name"]:
                ad["name"] = r["player_name"]

            if def_tag:
                if def_tag not in defender_data:
                    pinfo = player_info.get(def_tag, {})
                    defender_data[def_tag] = {
                        "name": pinfo.get("name", def_tag),
                        "clan_tag": pinfo.get("clan", ""),
                        "th": pinfo.get("th", 0),
                        "defenses": 0, "stars_conceded": 0,
                    }
                dd = defender_data[def_tag]
                dd["defenses"] += 1
                dd["stars_conceded"] += stars

        return attacker_data, defender_data, season, n_group_clans, n_wars, tracked_clan_tags

    except Exception as exc:
        logging.error(f"[CWL-ANALYSE-DB] DB fallback failed for {clan_tag}: {exc}")
        return empty


# ── CWL promotion / demotion rules (versioned by season) ──────────────────────
#
# Structure:
#   _CWL_PROMO_RULES_VERSIONED: list of (first_season, rules_dict) pairs,
#   sorted DESCENDING by first_season (newest first).
#
#   Each entry is effective from `first_season` up to (but not including) the
#   first_season of the NEXT entry in the list.
#
#   To add a new rule set for a future season (e.g. 2027-05), prepend a new
#   tuple with the new season string and its rules dict.  The lookup function
#   below handles the rest automatically — no other code needs to change.
#
# Rules dict: league_name → (n_promoted, n_demoted)
#
# ── Rules valid from 2026-05 onwards ────────────────────────────────────────
# Supercell has never published these numbers. Empirically measured 2026-07-26
# via `qapbot/scripts/audit_cwl_league_rank.py evaluate-promo-rules` — for every
# clan in a fully-ranked, complete-data 8-clan group in season 2026-07, compared
# its final rank against its CURRENT live league (fetched fresh right after that
# season's promotions applied). Gold League I through Champion League III: huge
# sample sizes (hundreds to 5000+ clans per league) cleanly confirm (2, 2) with
# ~100% consistency at the rank boundary — left unchanged.
#
# Champion League I corrected 2026-07-26: (2, 2) -> (4, 1). n=66, unambiguous:
# ranks 1-4 100% promoted, ranks 5-7 ~89% unchanged, rank 8 ~89% demoted (only
# the bottom rank demotes, not the bottom two).
#
# NOT corrected — inconclusive, see changelog.txt 2026-07-26 and the
# evaluate-promo-rules script's docstring:
#   - Titan League I/II/III, Legend League: the measured data is too noisy to
#     trust — even top-ranked clans show only a ~35/65 split between promoted
#     and unchanged (every other league shows ~100/0 at its rank boundary), and
#     whole groups scatter across Titan I/II/III with often zero members
#     remaining at the group's own recorded league. Initially read as "this
#     tier doesn't follow a rank-based model at all" (e.g. a broader trophy/
#     medal-pool mechanic, the way Legend League already works globally) — but
#     that conclusion doesn't hold up: cross-checking against
#     `reconstruct`'s own confirmation status showed 97.8% of these groups'
#     *recorded* league_rank was never independently verified (all fell into
#     "disagreement" under the old, too-conservative safe-rank band), so the
#     "before" side of every comparison could itself still be the old,
#     never-corrected value. The likelier explanation: Titan/Legend is a small,
#     rarely-independently-verified population, so historical corruption from
#     before the 2026-07-26 root-cause fix has had the most opportunity to
#     accumulate uncorrected there. Champion League I shows the same 0%
#     confirmation rate yet still gave a clean, unambiguous (4, 1) signal, so
#     low confirmation alone doesn't explain the Titan/Legend messiness —
#     genuinely inconclusive without more data. Left at the old placeholder
#     values; revisit with evaluate-promo-rules once a season or two of
#     league_rank populated entirely under the new only-at-group-creation
#     logic has accumulated (should be unambiguous either way by then).
#   - Champion League II: ~5% of groups (19 of 371 sampled) show 6-7 of 8
#     members jumping to a MUCH higher league, including two-tier jumps
#     (Champion II -> Titan III) a single normal promotion can't produce.
#     This looks like leftover corrupted league_rank data our earlier bulk
#     fix didn't catch (i.e. these 19 groups' recorded "Champion League II" is
#     itself probably wrong), not a real promotion-rule quirk — needs manual
#     spot-checking, not a rule-table change.
_CWL_PROMO_RULES_FROM_2026_05: Dict[str, Tuple[int, int]] = {
    "Legend League":       (0, 2),
    "Titan League I":      (2, 2),
    "Titan League II":     (2, 2),
    "Titan League III":    (2, 2),
    "Champion League I":   (4, 1),
    "Champion League II":  (2, 2),
    "Champion League III": (2, 2),
    "Master League I":     (2, 2),
    "Master League II":    (2, 2),
    "Master League III":   (2, 2),
    "Crystal League I":    (2, 2),
    "Crystal League II":   (2, 2),
    "Crystal League III":  (2, 2),
    "Gold League I":       (2, 2),
    "Gold League II":      (2, 2),
    "Gold League III":     (2, 2),
    "Silver League I":     (2, 2),
    "Silver League II":    (2, 2),
    "Silver League III":   (2, 1),
    "Bronze League I":     (3, 1),
    "Bronze League II":    (3, 1),
    "Bronze League III":   (3, 0),
}

# ── Rules valid up to and including 2026-04 ───────────────────────────────────
_CWL_PROMO_RULES_PRE_2026_05: Dict[str, Tuple[int, int]] = {
    "Legend League":       (0, 0),
    "Titan League I":      (0, 0),
    "Titan League II":     (0, 0),
    "Titan League III":    (0, 0),
    "Champion League I":   (0, 1),
    "Champion League II":  (1, 2),
    "Champion League III": (1, 2),
    "Master League I":     (1, 2),
    "Master League II":    (1, 2),
    "Master League III":   (1, 2),
    "Crystal League I":    (1, 2),
    "Crystal League II":   (2, 2),
    "Crystal League III":  (2, 2),
    "Gold League I":       (2, 2),
    "Gold League II":      (2, 2),
    "Gold League III":     (2, 2),
    "Silver League I":     (2, 2),
    "Silver League II":    (2, 2),
    "Silver League III":   (2, 1),
    "Bronze League I":     (3, 1),
    "Bronze League II":    (3, 1),
    "Bronze League III":   (3, 0),
}

# Versioned table: newest first.  Each entry is (first_season_str, rules_dict).
# The effective range of entry[i] is: first_season_str[i] ≤ season < first_season_str[i-1].
# The last entry in the list is the catch-all for the oldest seasons.
_CWL_PROMO_RULES_VERSIONED: List[Tuple[str, Dict[str, Tuple[int, int]]]] = [
    ("2026-05", _CWL_PROMO_RULES_FROM_2026_05),
    ("2000-01", _CWL_PROMO_RULES_PRE_2026_05),   # covers everything before 2026-05
]


def _get_cwl_promo_rules(season: str, league_name: str) -> Tuple[int, int]:
    """Return (n_promoted, n_demoted) for *league_name* in *season*.

    Selects the correct rule table by finding the newest entry whose
    first_season is ≤ *season* (lexicographic comparison works because
    all season strings share the YYYY-MM format).

    Falls back to (2, 2) when league_name is not found in the selected table.
    """
    rules: Dict[str, Tuple[int, int]] = _CWL_PROMO_RULES_PRE_2026_05  # safe default
    for first_season, table in _CWL_PROMO_RULES_VERSIONED:
        if season >= first_season:
            rules = table
            break
    return rules.get(league_name, (2, 2))


# ── CWL league_rank self-heal ──────────────────────────────────────────────────
#
# Background: cwl_league_groups.league_rank was, before the 2026-07-26 fixes to
# _process_league_group_response (see changelog.txt), sometimes written wrong and
# then frozen once cwl_ended=1 — a clan's *current* league got recorded for a
# group instead of the league it actually played that season. The write-time bug
# is fixed, but a group's *existing* frozen value is never re-examined once
# cwl_ended=1 (see the short-circuit in update_cwl_group_stats below), so any row
# corrupted before the fix stays wrong forever unless something re-checks it.
#
# This performs that re-check every time an ended group's standings are served
# (cheap — in-memory clan_name_cache only, no API/DB calls), using the same
# league-independent "safe middle rank" reasoning validated in
# qapbot/scripts/audit_cwl_league_rank.py's `reconstruct` command: no version of
# the promotion/demotion rules below moves a clan more than 3 ranks up or 2 down,
# so a clan ranked outside that band is guaranteed to have stayed in the same
# league the following season. If that clan's CURRENT live league (in
# clan_name_cache) disagrees with the group's frozen league_rank — and the cache
# entry was refreshed within the window where "current" still means "this
# season's outcome" — the frozen value is corrected.
_CWL_SAFE_RANK_MAX_PROMOTED = 3
_CWL_SAFE_RANK_MAX_DEMOTED = 2


def _cwl_self_heal_league_rank(
    rows: List[Dict[str, Any]],
    cwl_season: str,
    recorded_league: str,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Return a corrected league_rank for an ended CWL group, or None if there's
    nothing to correct (already right, or not enough/ambiguous evidence).

    *rows* are the group's DB rows (each with clan_tag + group_rank). *now*
    defaults to the real current time; a caller-supplied value is only for tests.
    """
    if len(cwl_season) != 7:
        return None  # bonus mid-month CWL — group_rank/league semantics differ, skip

    try:
        season_start = datetime.strptime(cwl_season, "%Y-%m").replace(tzinfo=_tz.utc)
    except ValueError:
        return None

    # Promotions/demotions apply within roughly a week or two of the season's own
    # wars ending (~day 9); this margin gives that a buffer. The window closes
    # once the FOLLOWING season's own promotions would plausibly have applied —
    # past that point clans.war_league may reflect a LATER season, not this one.
    _MARGIN_DAYS = 11
    window_start = season_start + timedelta(days=_MARGIN_DAYS)
    _next_month = season_start.month + 1
    _next_year = season_start.year
    if _next_month > 12:
        _next_month = 1
        _next_year += 1
    window_end = season_start.replace(year=_next_year, month=_next_month) + timedelta(days=_MARGIN_DAYS)

    now = now or datetime.now(_tz.utc)
    if now < window_start or now >= window_end:
        return None  # too early or too late to trust current league for this season

    n = len(rows)
    lo, hi = _CWL_SAFE_RANK_MAX_PROMOTED + 1, n - _CWL_SAFE_RANK_MAX_DEMOTED
    if lo > hi:
        return None  # group too small for any rank to be unambiguously safe

    candidates: Dict[str, List[str]] = {}
    for row in rows:
        rank = row.get("group_rank")
        if rank is None or not (lo <= rank <= hi):
            continue
        entry = CACHE.clan_name_cache.get(row.get("clan_tag", ""))
        if not isinstance(entry, dict):
            continue
        wl = entry.get("war_league")
        last_checked = entry.get("last_checked_via_api")
        if not wl or not last_checked:
            continue
        try:
            lc_dt = datetime.fromisoformat(last_checked)
            if lc_dt.tzinfo is None:
                lc_dt = lc_dt.replace(tzinfo=_tz.utc)
        except (ValueError, TypeError):
            continue
        if lc_dt >= window_start:
            candidates.setdefault(wl, []).append(row["clan_tag"])

    if len(candidates) != 1:
        return None  # no fresh safe-rank evidence, or disagreement — don't guess

    reconstructed = next(iter(candidates))
    return reconstructed if reconstructed != recorded_league else None


# ── CWL medal rewards (versioned by season) ────────────────────────────────────
#
# Each league entry maps to:
#   (rank_medals, bonus_medals)
# where rank_medals is a tuple of 8 ints: medals earned for 1st, 2nd, …, 8th place.
# bonus_medals is the season-end bonus (League Medals per Bonus column in the table).
#
# Source: official CWL Medal Rewards table, effective 2026-05.
# Leagues without available data (pre-2026-05) return None from _get_cwl_medals().
#
_CWL_MEDALS_FROM_2026_05: Dict[str, Tuple[Tuple[int, ...], int]] = {
    "Bronze League III":   ((46, 44, 42, 40, 38, 36, 34, 32), 42),
    "Bronze League II":    ((58, 56, 54, 52, 50, 48, 46, 44), 45),
    "Bronze League I":     ((70, 68, 66, 64, 62, 60, 58, 56), 48),
    "Silver League III":   ((88, 85, 82, 79, 76, 73, 70, 67), 51),
    "Silver League II":    ((106, 103, 100, 97, 94, 91, 88, 85), 54),
    "Silver League I":     ((124, 121, 118, 115, 112, 109, 106, 103), 57),
    "Gold League III":     ((148, 144, 140, 136, 132, 128, 124, 120), 60),
    "Gold League II":      ((172, 168, 164, 160, 156, 152, 148, 144), 63),
    "Gold League I":       ((196, 192, 188, 184, 180, 176, 172, 168), 66),
    "Crystal League III":  ((220, 216, 212, 208, 204, 200, 196, 192), 69),
    "Crystal League II":   ((244, 240, 236, 232, 228, 224, 220, 216), 72),
    "Crystal League I":    ((274, 269, 264, 259, 254, 249, 244, 239), 75),
    "Master League III":   ((304, 299, 294, 289, 284, 279, 274, 269), 78),
    "Master League II":    ((334, 329, 324, 319, 314, 309, 304, 299), 81),
    "Master League I":     ((364, 359, 354, 349, 344, 339, 334, 329), 84),
    "Champion League III": ((388, 384, 380, 376, 372, 368, 364, 360), 87),
    "Champion League II":  ((412, 408, 404, 400, 396, 392, 388, 384), 90),
    "Champion League I":   ((436, 432, 428, 424, 420, 416, 412, 408), 93),
    "Titan League III":    ((454, 451, 448, 445, 442, 439, 436, 433), 96),
    "Titan League II":     ((472, 469, 466, 463, 460, 457, 454, 451), 99),
    "Titan League I":      ((490, 487, 484, 481, 478, 475, 472, 469), 102),
    "Legend League":       ((508, 505, 502, 499, 496, 493, 490, 487), 105),
}

# Versioned list: newest first.  Add future season medal tables here.
_CWL_MEDALS_VERSIONED: List[Tuple[str, Dict[str, Tuple[Tuple[int, ...], int]]]] = [
    ("2026-05", _CWL_MEDALS_FROM_2026_05),
]


def _get_cwl_medals(
    season: str, league_name: str
) -> Optional[Tuple[Tuple[int, ...], int]]:
    """Return (rank_medals_tuple, bonus_medals) for *league_name* in *season*.

    rank_medals_tuple contains 8 ints: medals for 1st through 8th place.
    Returns None when no medal data is available for the season/league combination
    (e.g. pre-2026-05 seasons, or Titan/Legend leagues in older data).
    """
    for first_season, table in _CWL_MEDALS_VERSIONED:
        if season >= first_season:
            return table.get(league_name)
    return None


# ── CWL group standings ────────────────────────────────────────────────────────

# In-memory TTL cache: key = "{league_group_id}:{cwl_season}", value = (ts, result_list)
_cwl_group_stats_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_CWL_GROUP_STATS_TTL: float = 600.0   # 10 minutes


async def _apply_cwl_self_heal(
    db: Any, rows: List[Dict[str, Any]], cwl_season: str, group_id: str, league_rank: str
) -> str:
    """Run the safe-rank self-heal cross-check against *rows* and, if it finds a
    correction, persist it (bypassing the cwl_ended freeze via force=True) and
    reflect it in *rows* in place. Returns the (possibly corrected) league_rank.

    Called from both branches of update_cwl_group_stats: the cwl_ended=1
    short-circuit (re-validates an already-frozen value on every subsequent
    render) and the initial computation (fills in league_rank immediately if
    it's still empty the moment a group's standings are first fully computed,
    rather than leaving it permanently empty for any group whose
    _process_league_group_response population never got a chance to run before
    the season ended — see changelog.txt 2026-07-26).
    """
    healed = _cwl_self_heal_league_rank(rows, cwl_season, league_rank)
    if healed is None:
        return league_rank
    logging.info(
        f"[CWL-GROUP-STATS] Self-heal: group {group_id} / {cwl_season} "
        f"league_rank corrected {league_rank!r} -> {healed!r} "
        "(safe-rank cross-check against a fresh, unambiguous group member)"
    )
    await db.update_cwl_league_rank(cwl_season, group_id, healed, force=True)
    for r in rows:
        r["league_rank"] = healed
    return healed


def cwl_group_all_rounds_ended(clan_tags: List[str], ended_wars: Dict[str, int]) -> bool:
    """Has every clan in this league group played out all of its CWL rounds?

    Standard CWL formula: n clans → n-1 rounds (works for short groups too). Extracted from
    update_cwl_group_stats() on 2026-08-22 (tracker #0017) so the on-demand path and the
    `cwl_ended` sweep can never disagree about what "the season finished" means — the sweep marks
    a group ended for real, and a second, subtly-different copy of this test is exactly the kind
    of near-duplicate Cardinal Rule 4 exists to prevent.

    Args:
        clan_tags: Every clan in the league group.
        ended_wars: {clan_tag: count of that clan's war_ended CWL wars this season}, as returned
            by get_cwl_group_war_stats()/get_cwl_group_war_stats_sync(). A clan absent from the
            dict has played zero.

    Returns:
        True only when every clan has reached the expected round count. False for an empty group.

    Note:
        Measured on real PROD data (2026-08-22), only ~55% of groups can EVER satisfy this: the
        rest contain clans whose wars are not tracked at all (group-mates harvested from a
        subscribed clan's group), so their counts stay at zero permanently. That is why the sweep
        pairs this with a time-based test — see constants.cwl_season_window_closed().
    """
    if not clan_tags:
        return False
    expected_rounds = max(len(clan_tags) - 1, 1)
    return all(ended_wars.get(ct, 0) >= expected_rounds for ct in clan_tags)



# ---------------------------------------------------------------------------
# cwl_ended sweep (2026-08-22, tracker #0017)
# ---------------------------------------------------------------------------

# Rotating keyset cursor over cwl_league_groups, carried between cycles. Module-level rather than
# persisted: the candidate query only ever returns groups still at cwl_ended=0, so a restart
# simply resumes from the top of whatever is still unended — no work is lost or repeated
# meaningfully, and the sweep is idempotent either way (Cardinal Rule 12).
_cwl_ended_sweep_cursor: Tuple[str, str] = ("", "")

# One asyncio.to_thread() hop per batch, this many batches per update cycle (project owner's
# spec, 2026-08-22). Sequential with an await between batches, NOT concurrent — see
# sweep_cwl_ended_flags()'s docstring.
CWL_ENDED_SWEEP_BATCH_SIZE = 500
CWL_ENDED_SWEEP_BATCHES_PER_CYCLE = 5


def _sweep_cwl_ended_batch_sync(after_group_id: str, after_season: str, limit: int) -> Dict[str, Any]:
    """One batch of the cwl_ended sweep, start to finish, with no await anywhere inside — the
    whole thing is one asyncio.to_thread() hop (Pitfall 26, COPILOT_PITFALLS_COOKBOOK.md). Doing
    it call-by-call instead would open an interleaving window per group on a path that touches
    thousands of them per cycle.

    Returns {"marked", "checked", "cursor", "exhausted"}.
    """
    from qapbot.constants import cwl_season_window_closed

    db = CACHE.db_manager
    result: Dict[str, Any] = {
        "marked": 0, "checked": 0, "cursor": (after_group_id, after_season), "exhausted": True,
    }
    if db is None:
        return result

    page = db.find_unended_cwl_groups_page_sync(after_group_id, after_season, limit)
    if not page:
        return result

    result["checked"] = len(page)
    result["cursor"] = (page[-1]["league_group_id"], page[-1]["cwl_season"])
    # A short page means the rotation reached the end — the caller wraps back to ("", "").
    result["exhausted"] = len(page) < limit

    to_mark: List[Tuple[str, str]] = []
    for group in page:
        season = group["cwl_season"]
        clan_tags = group["clan_tags"]
        if not clan_tags:
            continue
        # Time test first, and short-circuit: once the window is closed the answer is already
        # decided, so the per-group war_summary aggregate (~0.8 ms) is pure waste. This is what
        # makes burning down the historical backlog nearly free — every season older than
        # CWL_SEASON_WINDOW_DAYS skips the query entirely.
        if cwl_season_window_closed(season):
            to_mark.append((group["league_group_id"], season))
            continue
        # Still inside the window: the group can only be marked if it genuinely finished every
        # round. Same test the on-demand path uses (shared helper), so the two cannot disagree.
        _stars, ended_wars = db.get_cwl_group_war_stats_sync(season, clan_tags)
        if cwl_group_all_rounds_ended(clan_tags, ended_wars):
            to_mark.append((group["league_group_id"], season))

    if to_mark:
        db.mark_cwl_groups_ended_sync(to_mark)
        result["marked"] = len(to_mark)
    return result


async def sweep_cwl_ended_flags(
    batches: int = CWL_ENDED_SWEEP_BATCHES_PER_CYCLE,
    batch_size: int = CWL_ENDED_SWEEP_BATCH_SIZE,
) -> Dict[str, int]:
    """Mark finished CWL league groups as `cwl_ended=1` (2026-08-22, tracker #0017).

    THE BUG THIS FIXES. `cwl_ended` gates `is_latest_cwl_season_ended_sync()`, which is what stops
    `_find_active_cwl_war_for_clan()` re-walking a finished league group's war tags on every
    notInWar clan, forever. But the flag had exactly one writer — `update_cwl_group_stats()`,
    reached only from a `/cwlinfo` render or a `cwlgroup` subscription — and no periodic pass at
    all. Measured on the 2026-08-22 PROD data: 8 of 25,049 August groups were marked ended, and
    136,707 polled clans were sitting at cwl_ended=0, each one re-downloading long-finished CWL
    wars whenever the caches lapsed.

    TWO CONDITIONS, EITHER ONE MARKS THE GROUP:
      1. every clan played all its rounds (`cwl_group_all_rounds_ended`) — the pre-existing test,
         which fires as soon as a tracked group genuinely completes; and
      2. the season's war window is definitively over (`cwl_season_window_closed`).
    (2) is not redundant with (1): measured on real data, only ~55% of groups can EVER satisfy
    (1), because the other ~45% contain clans (6-8 of 8, typically) whose wars nobody fetches —
    they are group-mates harvested from a subscribed clan's group. Those are precisely the groups
    still doing redundant walks, and no amount of waiting completes their data.

    WHY THIS DOES NOT FREEZE ANY STANDINGS. The sweep writes the flag ONLY, via
    `mark_cwl_groups_ended_sync()` — never group_rank/total_stars/total_destruction.
    `update_cwl_group_stats()`'s freeze short-circuit requires `cwl_ended` AND non-NULL stored
    stats, so a group marked here with no stats still recomputes its standings live on the next
    render. That was the project owner's stated concern about option A, and it is structurally
    avoided rather than merely unlikely.

    BATCHES ARE SEQUENTIAL, NOT CONCURRENT. `batches` separate to_thread hops run one after
    another with an await between them, so the event loop (and the Discord gateway heartbeat)
    breathes between chunks instead of being handed one long blocking call. Firing them
    concurrently would put several sqlite writers on the same file and invite `database is
    locked`; the work is index-seek bound, so there is little to win from it anyway.

    Args:
        batches: How many pages to process this cycle.
        batch_size: Groups per page.

    Returns:
        {"checked", "marked", "batches_run", "wrapped"} for the cycle log. Never raises — this
        runs inside the main update cycle and must never break it.
    """
    global _cwl_ended_sweep_cursor

    totals = {"checked": 0, "marked": 0, "batches_run": 0, "wrapped": 0}
    if CACHE.db_manager is None:
        return totals

    for _ in range(max(1, batches)):
        after_group_id, after_season = _cwl_ended_sweep_cursor
        try:
            batch = await asyncio.to_thread(
                _sweep_cwl_ended_batch_sync, after_group_id, after_season, batch_size
            )
        except Exception as e:
            logging.error(f"[CWL-ENDED-SWEEP] Batch failed at cursor {after_group_id!r}: {e}")
            break

        totals["checked"] += batch["checked"]
        totals["marked"] += batch["marked"]
        totals["batches_run"] += 1

        if batch["exhausted"]:
            # End of the rotation — wrap so the next cycle re-walks whatever is still unended
            # (the ~45% that can never complete, plus anything new). Stop here rather than
            # immediately re-walking from the top within the same cycle.
            _cwl_ended_sweep_cursor = ("", "")
            totals["wrapped"] = 1
            break
        _cwl_ended_sweep_cursor = batch["cursor"]

    return totals


async def update_cwl_group_stats(
    clan_tag: str,
    cwl_season: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Compute and persist the CWL league-group standing for all clans in the group
    containing *clan_tag* for *cwl_season*.

    Returned list is sorted by group_rank (ascending) and each entry contains:
        clan_tag, clan_name, group_rank, total_stars, total_destruction,
        cwl_ended, league_rank

    Short-circuit rules (to minimise DB and API traffic):
    - If cwl_ended=1 is already recorded for this group **and** the stored stats
      are non-null, return them from DB (no recomputation).
    - Otherwise recompute from war_summary + in-war temp files, persist only
      changed values, and cache the result for _CWL_GROUP_STATS_TTL seconds.

    Stars from an active (in_war) round are included using CACHE.get_current_war_data()
    which tries in-memory temp_war_objects first and falls back to the temp JSON file.
    """
    import time as _time

    if cwl_season is None:
        # Derive the season from the DB rather than strftime so that a mid-month
        # bonus CWL (e.g. "2026-06-16") is used instead of the plain YYYY-MM key.
        _db0 = CACHE.db_manager
        if _db0 is not None:
            cwl_season = await _db0.get_latest_cwl_season_for_clan(clan_tag) \
                or datetime.now().strftime("%Y-%m")
        else:
            cwl_season = datetime.now().strftime("%Y-%m")

    db = CACHE.db_manager
    if db is None:
        return []

    # ── 1. Load group info from DB ────────────────────────────────────────────
    group_info = await db.get_cwl_group_info(clan_tag, cwl_season)
    if group_info is None:
        logging.info(f"[CWL-GROUP-STATS] No cwl_league_groups entry for {clan_tag} / {cwl_season}")
        return []

    group_id: str = group_info["league_group_id"]
    clan_tags: List[str] = group_info["clan_tags"]
    league_rank: str = group_info.get("league_rank") or ""

    # ── 2. TTL cache hit? ─────────────────────────────────────────────────────
    _cache_key = f"{group_id}:{cwl_season}"
    _cached = _cwl_group_stats_cache.get(_cache_key)
    if _cached:
        _cached_ts, _cached_result = _cached
        if _time.monotonic() - _cached_ts < _CWL_GROUP_STATS_TTL:
            return _cached_result

    # ── 3. If cwl_ended=1 and stored stats non-null, return DB data directly ─
    if group_info["cwl_ended"]:
        rows = group_info["rows"]
        if all(r.get("total_stars") is not None for r in rows):
            league_rank = await _apply_cwl_self_heal(db, rows, cwl_season, group_id, league_rank)
            result = _build_standings_result(rows, clan_tags, league_rank)
            _cwl_group_stats_cache[_cache_key] = (_time.monotonic(), result)
            return result

    # ── 4. Fetch completed-war stars from war_summary ─────────────────────────
    # One query fetching all clans in the group at once via public db_manager method
    db_stars, ended_wars = await db.get_cwl_group_war_stats(cwl_season, clan_tags)

    # ── 5. Add stars from the current in_war round (if any) ──────────────────
    for ct in clan_tags:
        war_data = CACHE.get_current_war_data(ct)
        if not isinstance(war_data, dict):
            continue
        _wd: Dict[str, Any] = cast(Dict[str, Any], war_data)
        state = (_wd.get("state") or "").lower().replace("_", "").replace(" ", "")
        if state != "inwar":
            continue
        is_cwl = bool(_wd.get("is_cwl", False))
        war_cwl_season: str = cast(str, _wd.get("cwl_season") or "")
        # Accept if is_cwl=True and season matches (or season is empty — older temp files)
        if not is_cwl:
            continue
        if war_cwl_season and war_cwl_season != cwl_season:
            continue
        _clan_side: Dict[str, Any] = cast(Dict[str, Any], _wd.get("clan") or {})
        live_stars = int(_clan_side.get("stars") or 0)
        live_avg_destr = float(_clan_side.get("destruction") or 0.0)
        live_team_size = int(_wd.get("team_size") or 15)
        live_destr = live_avg_destr * live_team_size
        prev_stars, prev_destr = db_stars.get(ct, (0, 0.0))
        db_stars[ct] = (prev_stars + live_stars, prev_destr + live_destr)

    # ── 6. Compute ranks ──────────────────────────────────────────────────────
    # Sort: stars DESC, destruction DESC (tie-breaker)
    ranked = sorted(
        clan_tags,
        key=lambda ct: (-(db_stars.get(ct, (0, 0.0))[0]), -(db_stars.get(ct, (0, 0.0))[1])),
    )

    clan_stats: List[Dict[str, Any]] = []
    for rank, ct in enumerate(ranked, start=1):
        stars, destr = db_stars.get(ct, (0, 0.0))
        clan_stats.append({
            "clan_tag": ct,
            "group_rank": rank,
            "total_stars": stars,
            "total_destruction": round(destr, 2),
        })

    # ── 7. Check season completion (all clans × their group's round count ended) ─
    all_ended = cwl_group_all_rounds_ended(clan_tags, ended_wars)

    # ── 8. Persist to DB (only changed rows) ─────────────────────────────────
    n_updated = await db.update_cwl_group_stats_batch(cwl_season, group_id, clan_stats, all_ended)
    if n_updated:
        logging.info(f"[CWL-GROUP-STATS] Updated {n_updated} row(s) for group {group_id} / {cwl_season} (ended={all_ended})")

    # ── 9. Build and cache result ─────────────────────────────────────────────
    # Merge computed stats with existing DB rows for clan_name lookup
    stat_by_tag = {cs["clan_tag"]: cs for cs in clan_stats}
    merged_rows: List[Dict[str, Any]] = []
    for ct in ranked:
        cs = stat_by_tag[ct]
        merged_rows.append({
            "clan_tag": ct,
            "group_rank": cs["group_rank"],
            "total_stars": cs["total_stars"],
            "total_destruction": cs["total_destruction"],
            "cwl_ended": all_ended,
            "league_rank": league_rank,
        })

    # Give the self-heal a chance right at season-completion time too — not just
    # on later re-renders. Without this, a group whose league_rank never got
    # populated during its active season (e.g. _process_league_group_response
    # never ran for it before the season ended) would stay empty forever for
    # any clan reached only via the auto-post loop, since that loop skips
    # calling update_cwl_group_stats again once cwl_ended is observed True.
    if all_ended:
        league_rank = await _apply_cwl_self_heal(db, merged_rows, cwl_season, group_id, league_rank)

    result = _build_standings_result(merged_rows, clan_tags, league_rank)
    _cwl_group_stats_cache[_cache_key] = (_time.monotonic(), result)
    return result


def _build_standings_result(
    rows: List[Dict[str, Any]],
    all_clan_tags: List[str],
    league_rank: str,
) -> List[Dict[str, Any]]:
    """Merge DB rows with clan_name_cache and return a sorted standings list."""
    result: List[Dict[str, Any]] = []
    for row in rows:
        ct = row["clan_tag"]
        name = ""
        _entry = CACHE.clan_name_cache.get(ct)
        if isinstance(_entry, dict):
            name = _entry.get("name") or ct
        result.append({
            "clan_tag": ct,
            "clan_name": name or ct,
            "group_rank": row.get("group_rank") or 0,
            "total_stars": row.get("total_stars") or 0,
            "total_destruction": row.get("total_destruction") or 0.0,
            "cwl_ended": bool(row.get("cwl_ended")),
            "league_rank": row.get("league_rank") or league_rank,
        })
    result.sort(key=lambda r: (r["group_rank"] or 9999))
    return result


def generate_cwl_group_image(
    standings: List[Dict[str, Any]],
    cwl_season: str,
    highlight_clan_tag: str = "",
) -> bytes:
    """
    Render a CWL league-group standings table as a PNG image (bytes).

    Visual design mirrors the in-game CWL standings screen:
    - Dark warm-brown background (#3D1A08).
    - Rust-orange header bar with two-tone (lighter top / darker bottom) effect.
    - Row cards: lighter warm-brown top face + dark near-background shadow on the
      bottom ~35%, mimicking the in-game raised-card look.
    - Promotion/demotion shown as solid-colour circle badges (green ▲ / gray — / red ▼).
    - White/cream clan names and numbers; gold ★.

    Uses matplotlib (already in requirements.txt); returns raw PNG bytes.
    """
    import io
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.patches import FancyBboxPatch, Circle, Rectangle

    n = len(standings)
    league_name = standings[0]["league_rank"] if standings else ""

    # ── Promotion / demotion counts per league ────────────────────────
    # Resolved from the versioned rule table; season selects the correct set.
    _n_promoted, _n_demoted = _get_cwl_promo_rules(cwl_season, league_name)

    # ── Medal rewards (2026-05+: per-rank medals + season bonus) ─────
    _medals_data = _get_cwl_medals(cwl_season, league_name)

    # Resolve the per-rank medal count for the highlighted clan (shown in header).
    # We look up the clan's current rank position in standings, then index into the
    # rank_medals tuple.  Returns None when no medal data or no highlighted clan.
    _hdr_medal_cnt: Optional[int] = None
    if _medals_data is not None and highlight_clan_tag:
        _hm_rank_tpl, _ = _medals_data
        for _he in standings:
            if _he.get("clan_tag", "").upper() == highlight_clan_tag.upper():
                _hl_idx = _he["group_rank"] - 1   # 0-indexed
                if 0 <= _hl_idx < len(_hm_rank_tpl):
                    _hdr_medal_cnt = int(_hm_rank_tpl[_hl_idx])  # type: ignore[index]
                break

    # ── Layout ────────────────────────────────────────────────────────
    FW        = 9.5    # figure width (inches)
    H_HDR     = 0.840  # header bar height (−25% from original)
    H_COL     = 0.572  # column-label row: 17.3pt text (~0.24 in) + 0.08 box bleed + ~0.10 padding each side (+10%)
    H_ROW     = 0.406  # per-clan row height (−25% from original)
    # FancyBboxPatch pad=0.08 bleeds 0.08 in beyond stated bounds on each side,
    # so visible gap = H_GAP - 2*0.08. Keep visible gap ≈ 8 px at 115 dpi (~0.07 in).
    H_GAP     = 0.239  # stated gap → visible gap ≈ 0.079 in (~9 px)
    H_PAD_TOP = 0.18   # top margin = left/right margin (X_L)
    H_PAD_BOT = 0.18   # bottom margin = left/right margin (X_L)
    # Use (n-1) gaps: only n-1 gaps exist between n rows; avoids extra space at bottom.
    FH = H_PAD_TOP + H_HDR + H_COL + n * H_ROW + (n - 1) * H_GAP + H_PAD_BOT

    # Horizontal anchors
    X_L        = 0.18
    X_R        = FW - 0.18
    CARD_W     = X_R - X_L
    X_BADGE_CX = X_L + 0.24   # badge circle center
    X_RANK     = X_L + 0.88
    X_NAME     = X_L + 1.30
    X_STARS_N  = X_R - 2.10   # star count, right-aligned
    X_STAR_I   = X_R - 1.95   # ★ icon (left-aligned after number)
    X_DESTR    = X_R - 0.20   # destruction, right-aligned

    # ── Colour palette (sampled from the in-game screenshot) ──────────
    C_BG           = "#3F2C25"   # background between row cards
    C_HDR_BASE     = "#9B3318"   # header base colour
    C_HDR_LIGHT    = "#AD5B45"   # lighter strip at the TOP of the header
    C_HDR_EDGE     = "#B84030"   # border around header
    C_HDR_TXT      = "#FFFFFF"
    C_HDR_SUB      = "#D4B090"   # season label
    C_COL_LBL      = "#D4A855"   # column header text (amber)
    C_SEP          = "#7A3820"   # separator line

    # Row two-tone: light strip at TOP, base colour for the rest
    C_ROW_BASE     = "#724741"   # row base colour
    C_ROW_LIGHT    = "#8C6663"   # lighter strip at the TOP of each row
    C_ROW_BORDER   = "#5A3530"

    # Blue highlight for the clan that triggered the command
    C_HLT_BASE     = "#465D87"   # blue base colour
    C_HLT_LIGHT    = "#657A99"   # lighter strip at the TOP of the highlighted row
    C_HLT_BORDER   = "#507AAA"

    C_TXT          = "#F2EDE0"   # clan name / number text (warm white)
    C_TXT_RANK     = "#EADA90"   # rank number (slightly gold)
    C_STARS        = "#F5C040"   # gold ★

    C_BADGE_PRO    = "#2D912D"   # green promotion badge (matches promoted rank number colour)
    C_BADGE_NEU    = "#404040"   # gray neutral badge
    C_BADGE_DEM    = "#A52626"   # red demotion badge (matches demoted rank number colour)

    # ── Font setup: register all fonts in qapbot/fonts/ once per process ─────
    # Drop any .otf/.ttf file into qapbot/fonts/ to make it available here
    # (e.g. NotoSansKR-Bold.otf for Korean clan names).  The Clash OTF files
    # are always present; additional Noto files are optional but recommended.
    global _clash_fonts_registered
    import matplotlib.font_manager as _fm
    logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
    if not _clash_fonts_registered:
        for _ext in ("*.otf", "*.ttf"):
            import glob as _glob
            for _font_path in _glob.glob(os.path.join(_CLASH_FONTS_DIR, _ext)):
                try:
                    _fm.fontManager.addfont(_font_path)  # type: ignore[no-untyped-call]
                except Exception:
                    pass
        _clash_fonts_registered = True
    matplotlib.rcParams["axes.unicode_minus"] = False

    def _fp(size: float, bold: bool = True) -> "_fm.FontProperties":
        """Return a FontProperties pointing directly at the Clash OTF file."""
        return _fm.FontProperties(
            fname=_CLASH_BOLD_PATH if bold else _CLASH_REGULAR_PATH,
            size=size,
        )

    def _sym(size: float) -> "_fm.FontProperties":
        """Return a FontProperties for DejaVu Sans — covers all Unicode symbols the
        Clash font lacks (★, ▲, ▼, —, emoji in clan names, etc.).
        DejaVu Sans is always bundled with matplotlib so this never fails."""
        return _fm.FontProperties(family="DejaVu Sans", size=size)

    def _name_fp(name: str, size: float) -> "_fm.FontProperties":
        """Clash Bold for pure Latin names; broad Unicode font stack for names containing
        symbols / emoji outside the Basic Latin + Latin Extended range.
        To extend coverage on prod, drop additional font files into qapbot/fonts/:
          - NotoSansKR-Bold.ttf          → Korean Hangul      (family: "Noto Sans KR")
          - NotoSansArabic-Bold.ttf      → Arabic             (family: "Noto Sans Arabic")
          - NotoSansSC-Bold.ttf          → Chinese            (family: "Noto Sans SC")
          - NotoSans-Bold.ttf            → Greek/Cyrillic/Thai (family: "Noto Sans")
          - NotoSansSymbols2-Regular.ttf → ⭐ U+2B50 and other symbol/emoji chars
                                           (family: "Noto Sans Symbols 2")
        On Windows dev: Segoe UI Emoji / Segoe UI Symbol cover ⭐ and most emoji."""
        if any(ord(c) > 0x024F for c in name):
            return _fm.FontProperties(
                family=["DejaVu Sans", "Noto Sans", "Noto Sans KR", "Noto Sans SC",
                        "Noto Sans Arabic", "Noto Sans Symbols 2",
                        "Segoe UI Emoji", "Segoe UI Symbol", "Segoe UI",
                        "Malgun Gothic", "Microsoft YaHei", "Arial Unicode MS", "sans-serif"],
                size=size, weight="bold",
            )
        return _fp(size)

    from matplotlib import patheffects as _pe

    class _OffsetStroke(_pe.AbstractPathEffect):  # type: ignore[misc]
        """Dark stroked outline displaced 1 display-pixel downward.
        Creates a directional drop-shadow: thicker at the bottom edge,
        barely visible at the top edge."""
        def __init__(self, lw: float = 2.5) -> None:
            super().__init__(offset=(0.0, -1.0))   # 1 px down in display coords
            self._lw = lw

        def draw_path(  # type: ignore[override]
            self, renderer: Any, gc: Any, tpath: Any, affine: Any, rgbFace: Any = None
        ) -> None:
            gc0 = renderer.new_gc()
            gc0.copy_properties(gc)
            gc0.set_linewidth(self._lw)
            gc0.set_foreground("#000000")
            gc0.set_joinstyle("round")
            gc0.set_capstyle("round")
            renderer.draw_path(gc0, tpath, affine + self._offset_transform(renderer), None)  # type: ignore[attr-defined]
            gc0.restore()
            # Draw the original path on top so the shadow sits behind it
            super().draw_path(renderer, gc, tpath, affine, rgbFace)  # type: ignore[misc]

    def _sh(lw: float = 2.5) -> "list[Any]":
        """Return a path_effects list with a 1-px-down directional black drop-shadow."""
        return [_OffsetStroke(lw=lw)]

    from matplotlib.figure import Figure  # OO API — thread-safe, no global pyplot state
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    import matplotlib.image as _mpimg
    from matplotlib.offsetbox import OffsetImage, AnnotationBbox

    # ── Load game asset images (degrade gracefully if files are missing) ──────
    _league_icon_arr: Any = None
    _icon_fname = _LEAGUE_ICON_MAP.get(league_name)
    if _icon_fname:
        try:
            _league_icon_arr = _mpimg.imread(os.path.join(_GAMEASSETS_DIR, _icon_fname))
        except Exception:
            pass  # icon not found — image still renders without it

    _medal_icon_arr: Any = None
    if _medals_data is not None:
        try:
            _medal_icon_arr = _mpimg.imread(_MEDAL_ICON_PATH)
        except Exception:
            pass  # medal icon not found — text still renders without the icon

    fig: Any
    ax: Any
    fig = Figure(figsize=(FW, FH))
    FigureCanvasAgg(fig)  # attach Agg renderer without touching pyplot globals
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, FW)
    ax.set_ylim(0, FH)
    ax.axis("off")
    fig.patch.set_facecolor(C_BG)
    ax.set_facecolor(C_BG)

    # ── Header bar ────────────────────────────────────────────────────
    hdr_bot        = FH - H_PAD_TOP - H_HDR
    HDR_LIGHT_H    = H_HDR * 0.306   # light strip occupies top 30.6% of header (−15%)

    # Full rounded card in the base colour
    hdr_patch = ax.add_patch(FancyBboxPatch(
        (X_L, hdr_bot), CARD_W, H_HDR,
        boxstyle="round,pad=0.10",
        facecolor=C_HDR_BASE, edgecolor=C_HDR_EDGE, linewidth=1.5, zorder=1,
    ))
    hdr_patch.set_path_effects(_sh(5))
    # Lighter strip at the TOP — Rectangle oversized to cover the full top area, clipped
    # to the base card's rounded shape so it never bleeds beyond the card boundary.
    _hdr_strip = ax.add_patch(Rectangle(
        (X_L - 0.10, hdr_bot + H_HDR - HDR_LIGHT_H), CARD_W + 0.20, HDR_LIGHT_H + 0.10,
        facecolor=C_HDR_LIGHT, edgecolor="none", zorder=2,
    ))
    _hdr_strip.set_clip_path(hdr_patch)
    t = ax.text(FW / 2, hdr_bot + H_HDR * 0.72, league_name,
            ha="center", va="center",
            fontproperties=_fp(31.9), color=C_HDR_TXT, zorder=4)
    t.set_path_effects(_sh(3))
    t2 = ax.text(FW / 2, hdr_bot + H_HDR * 0.13, f"CWL {cwl_season}",
            ha="center", va="center",
            fontproperties=_fp(18.6, bold=False), color=C_HDR_SUB, zorder=4)
    t2.set_path_effects(_sh(2))

    # ── League icon — centered in the left zone of the header ────────
    if _league_icon_arr is not None:
        _icon_target_px = H_HDR * 0.684 * 115   # 68.4% of header height (72% × 0.95, 5% smaller)
        _icon_zoom      = _icon_target_px / _league_icon_arr.shape[0]
        _icon_cx        = X_L + 1.10             # fixed center-x: left of league name text
        _icon_cy        = hdr_bot + H_HDR * 0.50  # vertically centred in header
        _oi_league = OffsetImage(_league_icon_arr, zoom=_icon_zoom)
        _ab_league = AnnotationBbox(_oi_league, (_icon_cx, _icon_cy),
                                    frameon=False, zorder=5)
        ax.add_artist(_ab_league)

    # ── Medals earned by highlighted clan — bottom-right of header ───
    if _hdr_medal_cnt is not None:
        _medal_hdr_y = hdr_bot + H_HDR * 0.12   # slightly lower for visual centering with icon
        _tm = ax.text(X_R - 0.46, _medal_hdr_y,
                f"Medals: {_hdr_medal_cnt}",
                ha="right", va="center",
                fontproperties=_fp(14.5, bold=False), color="#4DC020", zorder=4)
        _tm.set_path_effects(_sh(2))
        if _medal_icon_arr is not None:
            _m_hdr_zoom = 23 / _medal_icon_arr.shape[0]   # ~23 px (20% smaller than 29)
            _oi_m_hdr   = OffsetImage(_medal_icon_arr, zoom=_m_hdr_zoom)
            _ab_m_hdr   = AnnotationBbox(_oi_m_hdr, (X_R - 0.20, _medal_hdr_y),
                                         frameon=False, zorder=5)
            ax.add_artist(_ab_m_hdr)

    # ── Column labels ─────────────────────────────────────────────────
    col_mid_y = hdr_bot - (H_COL + 0.05) / 2  # center in visible gap (header bleeds 0.10 below hdr_bot; sep at H_COL-0.05)
    for lbl, x, ha in [
        ("#",        X_RANK,   "center"),
        ("Clan",     X_NAME,   "left"),
        ("Stars ★",  X_STAR_I, "right"),
        ("Destr. %", X_DESTR,  "right"),
    ]:
        # Use DejaVu for column labels — "Stars ★" contains a symbol the Clash font lacks.
        t = ax.text(x, col_mid_y, lbl,
                ha=ha, va="center",
                fontproperties=_sym(17.3), color=C_COL_LBL, zorder=2)
        t.set_path_effects(_sh(2))

    sep_y = hdr_bot - H_COL + 0.05
    ax.plot([X_L, X_R], [sep_y, sep_y], color=C_SEP, linewidth=0.9, zorder=2)

    # ── Clan rows ─────────────────────────────────────────────────────
    rows_top = sep_y - 0.04

    for i, entry in enumerate(standings):
        rank  = entry["group_rank"]
        name  = normalize_player_name(entry["clan_name"])   # strips PUA / game-specific chars
        stars = entry["total_stars"]
        destr = entry["total_destruction"]

        row_bot      = rows_top - (i + 1) * (H_ROW + H_GAP) + H_GAP
        row_mid      = row_bot + H_ROW / 2
        ROW_LIGHT_H  = H_ROW * 0.425   # light strip at top 42.5% of each row (−15%)

        # Pick colours: blue highlight for the command clan, standard otherwise
        _clan_tag = entry.get("clan_tag", "")
        _highlighted = bool(highlight_clan_tag and _clan_tag.upper() == highlight_clan_tag.upper())
        _row_base   = C_HLT_BASE   if _highlighted else C_ROW_BASE
        _row_light  = C_HLT_LIGHT  if _highlighted else C_ROW_LIGHT
        _row_border = C_HLT_BORDER if _highlighted else C_ROW_BORDER

        # Rounded card in base colour
        row_patch = ax.add_patch(FancyBboxPatch(
            (X_L, row_bot), CARD_W, H_ROW,
            boxstyle="round,pad=0.08",
            facecolor=_row_base, edgecolor=_row_border, linewidth=0.7, zorder=1,
        ))
        row_patch.set_path_effects(_sh(4))
        # Lighter strip at the TOP — Rectangle oversized to cover the full top area, clipped
        # to the base card's rounded shape so it never bleeds beyond the card boundary.
        _strip = ax.add_patch(Rectangle(
            (X_L - 0.08, row_bot + H_ROW - ROW_LIGHT_H), CARD_W + 0.16, ROW_LIGHT_H + 0.08,
            facecolor=_row_light, edgecolor="none", zorder=2,
        ))
        _strip.set_clip_path(row_patch)

        # Promotion / demotion badge
        if _n_promoted > 0 and rank <= _n_promoted:
            badge_col, arrow, badge_dy, badge_weight = C_BADGE_PRO, "▲", 0.0, "normal"
        elif _n_demoted > 0 and rank > n - _n_demoted:
            badge_col, arrow, badge_dy, badge_weight = C_BADGE_DEM, "▼", 0.0, "normal"
        else:
            badge_col, arrow, badge_dy, badge_weight = C_BADGE_NEU, "—", 0.0, "bold"

        ax.add_patch(Circle((X_BADGE_CX, row_mid), 0.193,
                            color=badge_col, zorder=4))
        ba = ax.text(X_BADGE_CX, row_mid + badge_dy, arrow,
                ha="center", va="center",
                fontproperties=_fm.FontProperties(family="DejaVu Sans", size=16, weight=badge_weight),
                color="#FFFFFF", zorder=5)
        ba.set_path_effects(_sh(2))

        # Rank number — dark green for promoted clans, dark red for demoted clans
        _rank_color = (
            "#2D912D" if (_n_promoted > 0 and rank <= _n_promoted)
            else "#A52626" if (_n_demoted > 0 and rank > n - _n_demoted)
            else C_TXT_RANK
        )
        rk = ax.text(X_RANK, row_mid, f"{rank}.",
                ha="center", va="center",
                fontproperties=_fp(23.3), color=_rank_color, zorder=4)
        rk.set_path_effects(_sh())

        # Clan name — use DejaVu for names with symbols/emoji outside Clash's glyph set
        disp_name = name if len(name) <= 24 else name[:23] + "…"
        cn = ax.text(X_NAME, row_mid, disp_name,
                ha="left", va="center",
                fontproperties=_name_fp(disp_name, 21.3), color=C_TXT, zorder=4)
        cn.set_path_effects(_sh())

        # Stars: number right-aligned + ★ icon to its right
        sn = ax.text(X_STARS_N, row_mid, f"{stars:,}",
                ha="right", va="center",
                fontproperties=_fp(22.6), color=C_TXT, zorder=4)
        sn.set_path_effects(_sh())
        si = ax.text(X_STAR_I, row_mid + 0.01, "★",
                ha="left", va="center",
                fontproperties=_sym(27), color=C_STARS, zorder=4)
        si.set_path_effects(_sh())

        # Destruction
        dv = ax.text(X_DESTR, row_mid, f"{destr:,.0f}",
                ha="right", va="center",
                fontproperties=_fp(22.6), color=C_TXT, zorder=4)
        dv.set_path_effects(_sh())


    buf = io.BytesIO()
    import warnings
    with warnings.catch_warnings():
        # Clan names may contain emoji/symbols absent from the Clash font; suppress
        # the per-glyph UserWarning — the glyph simply falls back silently.
        warnings.filterwarnings("ignore", category=UserWarning, message="Glyph")
        fig.savefig(buf, format="png", dpi=115,
                    facecolor=fig.get_facecolor(),
                    bbox_inches="tight", pad_inches=0.06)
    buf.seek(0)
    return buf.read()


async def generate_cwl_group_analysis_embeds(clan_tag: str) -> List[discord.Embed]:
    """
    Generate CWL league group analysis embeds for a clan.

    Fetches the current/last CWL group's roster+rounds live (one cheap call), then
    resolves each round war's attack data DB-first: this bot's own periodic tracking
    already records every war for any clan it actively follows — including partial
    data for a war still in progress — so a local lookup covers most/all of the group
    with zero further live calls. Only wars the DB doesn't have yet (e.g. a clan
    outside this bot's tracked family, or a round too new to have been polled) fall
    through to a live per-war fetch, with one cache-bypassing retry for anything that
    comes back empty. Ranks every player across all clans in the group by:
    - Best attackers: most total stars across the season; tiebreak by lowest sum of
      attacked opponents' map positions (map position 1 = strongest defender, so a
      lower position sum means harder opponents were faced; min. 3 attacks to qualify)
    - Best defenders: fewest Ø⭐ conceded per defense (min. 2 defenses)

    Returns up to two Discord embeds (attackers first, defenders second).
    Returns an empty list if no live CWL group is available or fewer than 2 clans
    are found.
    """
    from qapbot.emojis import BotEmojis

    # ── Attempt live API; fall back to historical DB if the season is over ────
    attacker_data: Dict[str, Dict[str, Any]] = {}
    defender_data: Dict[str, Dict[str, Any]] = {}
    season: str = ''
    n_group_clans: int = 0
    wars_with_data: int = 0
    clan_names: Dict[str, str] = {}
    _is_historical: bool = False
    _live_ok: bool = False
    # War tags this bot instance could not confirm attack data for, after live
    # fetch + retry + local DB fallback all came up empty. Surfaced in the
    # embed footer so a live-vs-local discrepancy is visible, not just logged.
    _unrecovered_war_tags: List[str] = []

    try:
        league_group: Any = await CACHE.get_league_group(clan_tag)
    except Exception as lg_ex:
        logging.info(f"[CWL-ANALYSE] Could not fetch live league group for {clan_tag}: {lg_ex}")
        league_group = None

    if league_group:
        season = str(getattr(league_group, 'season', '') or '')
        group_clans = list(getattr(league_group, 'clans', []) or [])
        if season and len(group_clans) >= 2:
            n_group_clans = len(group_clans)

            # ── Build roster maps from the group's member lists ───────────────
            player_to_clan: Dict[str, str] = {}
            player_to_name: Dict[str, str] = {}
            player_to_th: Dict[str, int] = {}
            for cwl_clan in group_clans:
                ctag = str(getattr(cwl_clan, 'tag', '') or '')
                cname = normalize_player_name(str(getattr(cwl_clan, 'name', ctag) or ctag))
                if ctag:
                    clan_names[ctag] = cname
                for m in list(getattr(cwl_clan, 'members', []) or []):
                    mtag = str(getattr(m, 'tag', '') or '')
                    mname = normalize_player_name(str(getattr(m, 'name', mtag) or mtag))
                    mth = int(getattr(m, 'town_hall', 0) or 0)
                    if mtag:
                        player_to_clan[mtag] = ctag
                        player_to_name[mtag] = mname
                        if mth > 0:
                            player_to_th[mtag] = mth

            # ── Collect all war tags from all rounds ──────────────────────────
            all_war_tags: List[str] = [
                wt
                for round_list in (list(getattr(league_group, 'rounds', []) or []))
                for wt in (list(round_list) if round_list else [])
            ]

            def _process_war(war: Any) -> bool:
                """Fold one fetched war's attacks into attacker_data/defender_data.

                Returns True if the war carried at least one real attack (i.e.
                it wasn't an empty/incomplete snapshot).
                """
                war_clan = getattr(war, 'clan', None)
                war_opp = getattr(war, 'opponent', None)
                if war_clan is None or war_opp is None:
                    return False

                # Build tag→TH/name/map_position maps for all members in this specific war
                war_member_th: Dict[str, int] = {}
                war_member_name: Dict[str, str] = {}
                war_member_map_pos: Dict[str, int] = {}
                for side in [war_clan, war_opp]:
                    side_pos_list: List[Tuple[str, int]] = []
                    for m in list(getattr(side, 'members', []) or []):
                        mtag = str(getattr(m, 'tag', '') or '')
                        mth = int(getattr(m, 'town_hall', 0) or 0)
                        mname = normalize_player_name(str(getattr(m, 'name', mtag) or mtag))
                        mpos = int(getattr(m, 'map_position', getattr(m, 'mapPosition', 0)) or 0)
                        if mtag:
                            if mth > 0:
                                war_member_th[mtag] = mth
                            war_member_name[mtag] = mname
                            if mpos > 0:
                                side_pos_list.append((mtag, mpos))
                    # Normalize roster slots to sequential war ranks (1 = strongest)
                    # The CoC API returns mapPosition as the league group roster slot
                    # (1-30), not the 1-N war lineup position. Sorting by roster slot
                    # and assigning sequential ranks ensures avg positions stay ≤ team_size.
                    for rank, (mtag, _) in enumerate(
                        sorted(side_pos_list, key=lambda x: x[1]), start=1
                    ):
                        war_member_map_pos[mtag] = rank

                has_attacks = False
                for side in [war_clan, war_opp]:
                    side_tag = str(getattr(side, 'tag', '') or '')
                    for m in list(getattr(side, 'members', []) or []):
                        m_tag = str(getattr(m, 'tag', '') or '')
                        if not m_tag:
                            continue
                        m_th = war_member_th.get(m_tag, player_to_th.get(m_tag, 0))
                        m_name = war_member_name.get(m_tag, player_to_name.get(m_tag, m_tag))
                        m_clan = player_to_clan.get(m_tag, side_tag)

                        for atk in list(getattr(m, 'attacks', []) or []):
                            def_tag = str(
                                getattr(atk, 'defender_tag', getattr(atk, 'defenderTag', '')) or ''
                            )
                            stars = int(getattr(atk, 'stars', 0) or 0)
                            def_th = war_member_th.get(def_tag, player_to_th.get(def_tag, m_th))
                            has_attacks = True

                            # Attacker stats
                            def_map_pos = war_member_map_pos.get(def_tag, 0)
                            if m_tag not in attacker_data:
                                attacker_data[m_tag] = {
                                    'name': m_name, 'clan_tag': m_clan, 'th': m_th,
                                    'attacks': 0, 'total_stars': 0,
                                    'map_pos_sum': 0,
                                }
                            ad = attacker_data[m_tag]
                            ad['attacks'] += 1
                            ad['total_stars'] += stars
                            if def_map_pos > 0:
                                ad['map_pos_sum'] += def_map_pos
                            if m_th > 0:
                                ad['th'] = m_th
                            if m_name:
                                ad['name'] = m_name

                            # Defender stats (for the player being attacked)
                            if def_tag:
                                def_th_val = max(def_th, war_member_th.get(def_tag, player_to_th.get(def_tag, 0)))
                                def_name = war_member_name.get(def_tag, player_to_name.get(def_tag, def_tag))
                                def_clan = player_to_clan.get(def_tag, '')
                                if def_tag not in defender_data:
                                    defender_data[def_tag] = {
                                        'name': def_name, 'clan_tag': def_clan, 'th': def_th_val,
                                        'defenses': 0, 'stars_conceded': 0,
                                    }
                                dd = defender_data[def_tag]
                                dd['defenses'] += 1
                                dd['stars_conceded'] += stars
                                if def_th_val > 0:
                                    dd['th'] = def_th_val
                                if def_name:
                                    dd['name'] = def_name
                return has_attacks

            if all_war_tags:
                # ── DB-first ─────────────────────────────────────────────────
                # This bot's own periodic tracking already records every war for
                # any clan it actively follows — including partial data for a war
                # still in progress — so check that before ever touching the live
                # API. Usually resolves the whole group with zero live calls, and
                # sidesteps the live-fetch flakiness explored in
                # COPILOT_PITFALLS_COOKBOOK.md Pitfall 24 (a broken API key,
                # transient CoC API staleness) for anything this bot has already
                # seen. Live API becomes the fallback for what the DB doesn't have
                # yet — typically a war involving a clan outside this bot's
                # tracked family, or a round too new for the periodic tracker to
                # have polled yet.
                logging.info(
                    f"[CWL-ANALYSE] Checking local DB for {len(all_war_tags)} wars for "
                    f"{clan_tag} group analysis (season {season})"
                )
                _db_atk, _db_def, _db_tags_with_data = await asyncio.to_thread(
                    _load_cwl_wars_from_db_sync, all_war_tags
                )
                for ptag, ad in _db_atk.items():
                    attacker_data[ptag] = ad
                for ptag, dd in _db_def.items():
                    defender_data[ptag] = dd
                wars_with_data += len(_db_tags_with_data)

                missing_tags: List[str] = [wt for wt in all_war_tags if wt not in _db_tags_with_data]

                if missing_tags:
                    logging.info(
                        f"[CWL-ANALYSE] {len(missing_tags)}/{len(all_war_tags)} war(s) not yet "
                        f"in the local DB for {clan_tag} (season {season}); fetching live: "
                        f"{missing_tags}"
                    )
                    war_futures: Any = await asyncio.gather(
                        *[CACHE.get_league_war(wt) for wt in missing_tags],
                        return_exceptions=True
                    )

                    # A tag is "suspect" if the fetch raised, or it succeeded but
                    # carried no attacks at all — the CoC API is known to mark a
                    # CWL war warEnded slightly before its attack list has fully
                    # replicated, and since ended wars are cached as immutable
                    # (get_league_war), an ordinary retry would just be served
                    # that same incomplete snapshot again. We can't tell "round
                    # genuinely hasn't been attacked yet" apart from "API gave us
                    # a stale snapshot" from the object alone, so every empty
                    # result is treated as suspect and re-verified below.
                    #
                    # coc.PrivateWarLog is the one exception NOT worth retrying: for
                    # get_league_war() specifically it's a misnomer coc.py raises for
                    # ANY 403 on that endpoint, not just genuine warlog-privacy 403s
                    # (see COPILOT_PITFALLS_COOKBOOK.md Pitfall 24) — root-caused to a
                    # broken CoC API key rejecting the request outright ("Invalid
                    # authorization"). That's an auth-level rejection, not a transient
                    # data-freshness gap, so a cache-bypass retry within the same
                    # process/session would just burn an API call for the same
                    # result. We already checked the local DB above, so this simply
                    # counts as unrecovered.
                    suspect_tags: List[str] = []
                    _unrecovered_war_tags = []
                    for wt, w in zip(missing_tags, war_futures):
                        if isinstance(w, Exception):
                            logging.warning(
                                f"[CWL-ANALYSE] get_league_war({wt}) failed: "
                                f"{type(w).__name__}: {w}"
                            )
                            if isinstance(w, coc.PrivateWarLog):  # type: ignore[attr-defined]
                                _unrecovered_war_tags.append(wt)
                            else:
                                suspect_tags.append(wt)
                            continue
                        if w is None:
                            suspect_tags.append(wt)
                            continue
                        if _process_war(w):
                            wars_with_data += 1
                        else:
                            suspect_tags.append(wt)

                    if suspect_tags:
                        # Don't even attempt the retry for a tag whose whole round has
                        # zero recovered data anywhere (DB, or this same live pass) —
                        # that's indistinguishable from "round hasn't started yet", the
                        # retry is guaranteed to fail exactly like it just did, and
                        # logging a retry attempt for it would be noise for the expected,
                        # common case (every group has a not-started trailing round until
                        # the season ends). Only retry tags whose round has SOME other
                        # recovered war — a genuine per-war gap, worth the extra call.
                        _recovered_so_far = set(all_war_tags) - set(suspect_tags) - set(_unrecovered_war_tags)
                        _round_lists = [
                            list(rl) for rl in (list(getattr(league_group, 'rounds', []) or []))
                            if rl
                        ]
                        _not_yet_played_tags: Set[str] = set()
                        for _round_tags in _round_lists:
                            _round_suspect = [t for t in _round_tags if t in suspect_tags]
                            if _round_suspect and not (set(_round_tags) & _recovered_so_far):
                                _not_yet_played_tags.update(_round_suspect)
                        retry_tags = [t for t in suspect_tags if t not in _not_yet_played_tags]
                        # Feed straight into _unrecovered_war_tags (not silently dropped) so
                        # the final safety-net filter below — which already excludes zero-data
                        # rounds from the warning — accounts for them correctly when deciding
                        # what actually counts as "recovered" for any other still-unrecovered
                        # tag in the same round.
                        _unrecovered_war_tags.extend(_not_yet_played_tags)

                        if retry_tags:
                            logging.warning(
                                f"[CWL-ANALYSE] {len(retry_tags)}/{len(missing_tags)} war(s) came "
                                f"back with no attack data for {clan_tag} (season {season}); "
                                f"retrying with a forced cache bypass: {retry_tags}"
                            )
                            retry_futures: Any = await asyncio.gather(
                                *[CACHE.get_league_war(wt, force_refresh=True) for wt in retry_tags],
                                return_exceptions=True
                            )
                            for wt, w in zip(retry_tags, retry_futures):
                                if isinstance(w, Exception):
                                    logging.warning(
                                        f"[CWL-ANALYSE] Retry of get_league_war({wt}) failed: "
                                        f"{type(w).__name__}: {w}"
                                    )
                                    _unrecovered_war_tags.append(wt)
                                    continue
                                if w is None or not _process_war(w):
                                    _unrecovered_war_tags.append(wt)
                                    continue
                                wars_with_data += 1

                # ── Don't warn about rounds that simply haven't been played yet ──
                # Safety net for whatever reached here still unrecovered (PrivateWarLog
                # failures skip straight past the retry gate above, so they still need
                # this check): a round where NONE of its wars have data anywhere (DB or
                # live) is indistinguishable from "not started" — only surface the
                # warning for a round where SOME wars have data but this one doesn't, a
                # genuine gap rather than the season just being mid-way through.
                if _unrecovered_war_tags:
                    _recovered_tags = set(all_war_tags) - set(_unrecovered_war_tags)
                    _round_lists = [
                        list(rl) for rl in (list(getattr(league_group, 'rounds', []) or []))
                        if rl
                    ]
                    # Not-yet-played rounds are silently dropped here, on purpose —
                    # every group has a trailing not-started round until the season
                    # ends, so logging that every single time would just be noise
                    # for the expected, common case. Only a genuine gap (below) is
                    # worth a log line.
                    _genuinely_missing: List[str] = []
                    for _round_tags in _round_lists:
                        _round_unrecovered = [t for t in _round_tags if t in _unrecovered_war_tags]
                        if _round_unrecovered and (set(_round_tags) & _recovered_tags):
                            _genuinely_missing.extend(_round_unrecovered)
                    _unrecovered_war_tags = _genuinely_missing
                    if _unrecovered_war_tags:
                        logging.warning(
                            f"[CWL-ANALYSE] {len(_unrecovered_war_tags)} war(s) have no attack "
                            f"data live or in the local DB for {clan_tag} (season {season}): "
                            f"{_unrecovered_war_tags}"
                        )

                _live_ok = bool(attacker_data or defender_data)

    # ── DB fallback: use historical war_attacks data if live path gave nothing ─
    if not _live_ok:
        logging.info(f"[CWL-ANALYSE] Using historical DB data for {clan_tag}")
        _db_res = await asyncio.to_thread(_load_cwl_analysis_from_db_sync, clan_tag)
        attacker_data, defender_data, season, n_group_clans, wars_with_data, _tracked_tags = _db_res
        if not attacker_data and not defender_data:
            return []
        if not season:
            return []
        clan_names = {t: (CACHE.get_clan_name(t, t) or t) for t in _tracked_tags}
        _is_historical = True

    # ── Rank attackers ────────────────────────────────────────────────────────
    # Primary: most total stars across the whole season.
    # Tiebreak: lowest sum of attacked opponents' map positions — lower map
    # position number means a stronger defender (pos 1 = strongest), so a
    # smaller sum signals the attacker faced harder opponents.
    MIN_ATTACKS = 3   # min total attacks to qualify
    attacker_ranked: List[Tuple[str, Dict[str, Any], int, int]] = []
    for ptag, ad in attacker_data.items():
        if ad['attacks'] >= MIN_ATTACKS:
            attacker_ranked.append((ptag, ad, ad['total_stars'], ad['map_pos_sum']))
    attacker_ranked.sort(key=lambda x: (-x[2], x[3]))  # most stars, then lowest position sum
    top_attackers = attacker_ranked[:10]

    # ── Rank defenders ────────────────────────────────────────────────────────
    MIN_DEFENSES = 2
    defender_ranked: List[Tuple[str, Dict[str, Any], float]] = []
    for ptag, dd in defender_data.items():
        if dd['defenses'] >= MIN_DEFENSES:
            avg_conceded = dd['stars_conceded'] / dd['defenses']
            defender_ranked.append((ptag, dd, avg_conceded))
    # Sort: fewest avg stars conceded first; more defenses wins on tie
    defender_ranked.sort(key=lambda x: (x[2], -x[1]['defenses']))
    top_defenders = defender_ranked[:10]

    # ── Build embeds ──────────────────────────────────────────────────────────
    MEDAL_ICONS = ["🥇", "🥈", "🥉"]
    _data_note = " *(historical data)*" if _is_historical else ""
    group_summary = f"{n_group_clans} clans · {wars_with_data} wars · Season **{season}**{_data_note}"
    if _unrecovered_war_tags:
        _n_missing = len(_unrecovered_war_tags)
        group_summary += (
            f"\n⚠️ *{_n_missing} war{'s' if _n_missing != 1 else ''} could not be verified "
            f"live or in local records — totals below may be incomplete.*"
        )

    def _player_url(ptag: str) -> str:
        return coc_player_profile_url(ptag)

    def _clan_url(ctag: str) -> str:
        return coc_clan_profile_url(ctag)

    def _rank_label(rank: int) -> str:
        #c1 = '\u3000'  # Ideographic Space (full-width space) — 1.665
        c1 = '\u2800'  # Braille Pattern Blank - 1.113
        if rank <= 3:
            return MEDAL_ICONS[rank - 1] + c1
        elif rank < 10:
            return f"**#{rank}**"+c1
        else:
            return f"**#{rank}** "

    def _th_emoji(th: int) -> str:
        attr = f"TH{th:02d}"
        return str(getattr(BotEmojis, attr, f"TH{th}"))

    # Embed 1: Best Attackers
    atk_lines: List[str] = [
        group_summary,
        f"*Ranked by total ⭐ · tiebreak: lowest avg opponent map position (pos 1 = strongest)*\n",
    ]
    for rank, (ptag, ad, total_stars_val, map_pos_sum_val) in enumerate(top_attackers, start=1):
        th = int(ad.get('th', 0))
        clan_t = str(ad.get('clan_tag', ''))
        clan_n = clan_names.get(clan_t, CACHE.get_clan_name(clan_t, clan_t) or clan_t)
        total_atk = int(ad.get('attacks', 0))
        clan_link = (
            f"[{normalize_player_name(clan_n)}]({_clan_url(clan_t)})"
            if clan_t else normalize_player_name(clan_n)
        )
        player_link = f"[{normalize_player_name(ad['name'])}]({_player_url(ptag)})"
        avg_pos = map_pos_sum_val / total_atk if total_atk > 0 else 0
        pos_note = f"avg pos: {avg_pos:.1f}" if map_pos_sum_val > 0 else "pos unknown"
        atk_lines.append(
            # Bare (non-bracketed) LRM (U+200E) at the START of the line and
            # at each transition OUT of an RTL name (after player_link, after
            # clan_link) — matching the proven pattern used for opponent
            # names elsewhere in this codebase: vs. [LRM name LRM](url)  LRM TAG.
            # normalize_player_name() already brackets each name internally
            # with LRM, but that alone only protects the name's own internal
            # word order; live retesting confirmed a whole multi-field row
            # like this one (rank, player, clan, arrow, stats) still gets its
            # RTL runs merged into one and mirrored as a block unless every
            # transition back to LTR content also has its own bare LRM
            # anchor. See changelog for this bug.
            f"\u200E{_rank_label(rank)}{_th_emoji(th)} {player_link}"
            f"\u200E · {clan_link}"
            f"\u200E → ⭐ **{total_stars_val}** *({total_atk} atk · {pos_note})*"
        )
    if not top_attackers:
        atk_lines.append("*Not enough attack data available yet.*")

    atk_embed = discord.Embed(
        title="⚔️ Best Attackers · CWL Group",
        description="\n".join(atk_lines),
        color=0xf0bc44,
    )

    # Embed 2: Best Defenders
    def_lines: List[str] = [
        group_summary,
        f"*Ranked by fewest Ø⭐ conceded per defense (min. {MIN_DEFENSES} defenses)*\n",
    ]
    for rank, (ptag, dd, avg_conceded) in enumerate(top_defenders, start=1):
        th = int(dd.get('th', 0))
        clan_t = str(dd.get('clan_tag', ''))
        clan_n = clan_names.get(clan_t, CACHE.get_clan_name(clan_t, clan_t) or clan_t)
        n_defs = int(dd.get('defenses', 0))
        clan_link = (
            f"[{normalize_player_name(clan_n)}]({_clan_url(clan_t)})"
            if clan_t else normalize_player_name(clan_n)
        )
        player_link = f"[{normalize_player_name(dd['name'])}]({_player_url(ptag)})"
        def_lines.append(
            # See matching comment in the attackers loop above for why bare
            # LRM anchors are needed at every field transition, not just
            # wrapped around each name.
            f"\u200E{_rank_label(rank)}{_th_emoji(th)} {player_link}"
            f"\u200E · {clan_link}"
            f"\u200E → 🛡️ **{avg_conceded:.2f}** avg *({n_defs} defs)*"
        )
    if not top_defenders:
        def_lines.append("*Not enough defense data available yet.*")

    def_embed = discord.Embed(
        title="🛡️ Best Defenders · CWL Group",
        description="\n".join(def_lines),
        color=0x2ecc71,
    )

    return [atk_embed, def_embed]


# ──────────────────────────────────────────────────────────────────────────────
# /analyse cwlopponent — bulk player stats + embed builder
# ──────────────────────────────────────────────────────────────────────────────

def compute_roster_stats_sync(
    player_tags: List[str],
) -> Dict[str, Dict[str, Any]]:
    """
    Bulk-compute CW/CWL attack skill, reliability, and activity for a roster.

    Pulls all needed data in 3 SQL queries (one per-war aggregation, one star
    distribution, one CWL max-rounds lookup) instead of N×3 individual queries.

    Returns
    -------
    dict[player_tag] = {
        'cw_skill':    Optional[int]  — (3★−1★)/n×100, None when <1 CW attack
        'cwl_skill':   Optional[int]  — same for CWL attacks
        'reliability': Optional[int]  — total_used/total_max×100
        'activity':    Optional[int]  — time-normalised %, capped at 100
    }
    """
    import sqlite3 as _sqlite3
    import calendar as _calendar

    _MONTHLY_MAX_CW = 20
    _empty: Dict[str, Any] = {
        'cw_skill': None, 'cwl_skill': None, 'reliability': None, 'activity': None,
    }

    if not player_tags or not CACHE.db_manager or not CACHE.db_manager.db_path:
        return {tag: dict(_empty) for tag in player_tags}

    result: Dict[str, Dict[str, Any]] = {}

    with CACHE.db_manager.sync_conn() as conn:
        conn.row_factory = _sqlite3.Row  # type: ignore[attr-defined]
        ph = ','.join('?' * len(player_tags))
        wa_cols = CACHE.db_manager.explicit_column_list_sync(conn, "war_attacks")
        clg_cols = CACHE.db_manager.explicit_column_list_sync(conn, "cwl_league_groups")
        clr_cols = CACHE.db_manager.explicit_column_list_sync(conn, "cwl_league_rounds")

        # ── Query 1: per-war totals (reliability + activity) ──────────────────
        # Groups all rows (actual + sentinel) by (player_tag, war_id, clan_tag).
        # MAX(max_attacks) and MAX(missed_attacks) are identical across all rows
        # for the same war, so MAX is a safe aggregation.
        # NOTE: 'wa' unions main+history war_attacks (this is an all-time roster
        # stat, not a "current season" lookup, so history must be included — a
        # veteran player's oldest-war date and totals would otherwise silently
        # only reflect the hot (current + previous month) window).  ws is joined
        # against main.war_summary and history.war_summary directly (NOT a UNION
        # ALL CTE) because SQLite fully materializes a compound subquery used as
        # the right side of a LEFT JOIN — see qapbot/db_manager.py get_player_war_history_sync
        # for the incident this caused (59s query / OOM).
        war_rows_raw = conn.execute(f"""
            WITH wa AS (
                SELECT {wa_cols} FROM main.war_attacks
                UNION ALL SELECT {wa_cols} FROM history.war_attacks
            )
            SELECT
                wa.player_tag,
                wa.war_id,
                wa.clan_tag,
                MAX(wa.date)                                                     AS date,
                MAX(wa.max_attacks)                                              AS max_attacks_war,
                MAX(wa.missed_attacks)                                           AS missed_attacks_war,
                COALESCE(ws_h.is_cwl, ws_a.is_cwl,
                    CASE WHEN MAX(wa.max_attacks) = 1 THEN 1 ELSE 0 END)        AS is_cwl,
                COALESCE(NULLIF(ws_h.cwl_season, ''), NULLIF(ws_a.cwl_season, ''),
                    CASE WHEN MAX(wa.max_attacks) = 1
                         THEN substr(MAX(wa.date), 1, 7) ELSE '' END)           AS cwl_season
            FROM wa
            LEFT JOIN main.war_summary ws_h
                ON  wa.war_id   = ws_h.war_id
                AND wa.clan_tag = ws_h.clan_tag
            LEFT JOIN history.war_summary ws_a
                ON  wa.war_id   = ws_a.war_id
                AND wa.clan_tag = ws_a.clan_tag
            WHERE wa.player_tag IN ({ph})
            GROUP BY wa.player_tag, wa.war_id, wa.clan_tag
            ORDER BY wa.player_tag, MAX(wa.date) DESC
        """, player_tags).fetchall()

        # ── Query 2: star distribution (CW / CWL skill) ───────────────────────
        dist_rows_raw = conn.execute(f"""
            WITH wa AS (
                SELECT {wa_cols} FROM main.war_attacks
                UNION ALL SELECT {wa_cols} FROM history.war_attacks
            )
            SELECT
                wa.player_tag,
                COALESCE(ws_h.is_cwl, ws_a.is_cwl,
                    CASE WHEN wa.max_attacks = 1 THEN 1 ELSE 0 END)            AS is_cwl,
                SUM(CASE WHEN wa.stars = 3 THEN 1 ELSE 0 END)                  AS three_star,
                SUM(CASE WHEN wa.stars = 1 THEN 1 ELSE 0 END)                  AS one_star,
                COUNT(*)                                                        AS dist_attacks
            FROM wa
            LEFT JOIN main.war_summary ws_h
                ON  wa.war_id   = ws_h.war_id
                AND wa.clan_tag = ws_h.clan_tag
            LEFT JOIN history.war_summary ws_a
                ON  wa.war_id   = ws_a.war_id
                AND wa.clan_tag = ws_a.clan_tag
            WHERE wa.player_tag IN ({ph})
              AND wa.attack_order > 0
              AND wa.stars <= 3
            GROUP BY wa.player_tag,
                COALESCE(ws_h.is_cwl, ws_a.is_cwl,
                    CASE WHEN wa.max_attacks = 1 THEN 1 ELSE 0 END)
        """, player_tags).fetchall()

        # ── Query 3: CWL max-rounds per season (for activity normalisation) ───
        # INNER JOIN against UNION ALL CTEs is safe here (no forced materialization —
        # that only happens for LEFT JOIN targets), so a plain UNION ALL is used.
        season_clan_pairs: set[tuple[str, str]] = {
            (str(r["cwl_season"]), str(r["clan_tag"]))
            for r in war_rows_raw
            if r["is_cwl"] and r["cwl_season"] and r["clan_tag"]
        }
        cwl_max_rounds: Dict[str, int] = {}
        for cwl_season, ctag in season_clan_pairs:
            row = conn.execute(f"""
                WITH clg AS (
                    SELECT {clg_cols} FROM main.cwl_league_groups
                    UNION ALL SELECT {clg_cols} FROM history.cwl_league_groups
                ), clr AS (
                    SELECT {clr_cols} FROM main.cwl_league_rounds
                    UNION ALL SELECT {clr_cols} FROM history.cwl_league_rounds
                )
                SELECT COUNT(DISTINCT clr.cwl_round) AS max_rounds
                FROM   clg
                JOIN   clr
                       ON  clr.league_group_id = clg.league_group_id
                       AND clr.cwl_season      = clg.cwl_season
                WHERE  clg.clan_tag   = ?
                  AND  clg.cwl_season = ?
            """, (ctag, cwl_season)).fetchone()
            rounds = int(row["max_rounds"]) if row and row["max_rounds"] else 0
            if rounds > 0:
                cwl_max_rounds[cwl_season] = max(cwl_max_rounds.get(cwl_season, 0), rounds)

    # ── Aggregate per-player (Python post-processing) ─────────────────────────
    player_wars: Dict[str, List[Any]] = {}
    for r in war_rows_raw:
        player_wars.setdefault(r["player_tag"], []).append(r)

    player_dist: Dict[str, Dict[int, Dict[str, int]]] = {}
    for r in dist_rows_raw:
        tag = r["player_tag"]
        is_cwl_key = int(r["is_cwl"])
        player_dist.setdefault(tag, {})[is_cwl_key] = {
            'three_star':   int(r["three_star"]   or 0),
            'one_star':     int(r["one_star"]      or 0),
            'dist_attacks': int(r["dist_attacks"]  or 0),
        }

    _now = datetime.today()
    _cy, _cm, _cd = _now.year, _now.month, _now.day

    for tag in player_tags:
        wars = player_wars.get(tag, [])
        if not wars:
            result[tag] = dict(_empty)
            continue

        # Reliability
        total_max  = sum(int(r["max_attacks_war"]  or 0) for r in wars)
        total_used = sum(
            int(r["max_attacks_war"] or 0) - int(r["missed_attacks_war"] or 0)
            for r in wars
        )
        reliability: Optional[int] = (
            round(total_used / total_max * 100) if total_max > 0 else None
        )

        # CW / CWL skill  (same formula as _build_mode_stats in QBdiscordcmds)
        def _skill(d: Dict[str, int]) -> Optional[int]:
            n = d.get('dist_attacks', 0)
            return round((d.get('three_star', 0) - d.get('one_star', 0)) / n * 100) if n else None

        dist = player_dist.get(tag, {})
        cw_skill:  Optional[int] = _skill(dist.get(0, {}))
        cwl_skill: Optional[int] = _skill(dist.get(1, {}))

        # Activity — month-by-month accumulation, identical to _player_report_logic
        activity: Optional[int] = None
        oldest_date_str = str(wars[-1]["date"] or "")  # wars sorted newest→oldest
        if len(oldest_date_str) >= 7:
            _first_month_str = oldest_date_str[:7]
            _fd = int(oldest_date_str[8:10]) if len(oldest_date_str) >= 10 else 1
            _fy = int(_first_month_str[:4])
            _fm = int(_first_month_str[5:7])
            _max_cw  = 0.0
            _max_cwl = 0.0
            _cur = (_fy, _fm)
            _end = (_cy, _cm)
            while _cur <= _end:
                _y, _m     = _cur
                _tot_days  = _calendar.monthrange(_y, _m)[1]
                _is_first  = (_y == _fy and _m == _fm)
                _is_cur_m  = (_y == _cy and _m == _cm)
                if _is_first and _is_cur_m:
                    _days_avail  = _cd - _fd + 1
                    _include_cwl = _fd <= 10 and _cd > 10
                elif _is_first:
                    _days_avail  = _tot_days - _fd + 1
                    _include_cwl = _fd <= 10
                elif _is_cur_m:
                    _days_avail  = _cd
                    _include_cwl = _cd > 10
                else:
                    _days_avail  = _tot_days
                    _include_cwl = True
                _max_cw += _MONTHLY_MAX_CW * _days_avail / _tot_days
                if _include_cwl:
                    _max_cwl += cwl_max_rounds.get(f"{_y:04d}-{_m:02d}", 7)
                _cur = (_y + 1, 1) if _m == 12 else (_y, _m + 1)
            _max_possible = _max_cw + _max_cwl
            if _max_possible > 0:
                activity = min(round(total_used / _max_possible * 100), 100)

        result[tag] = {
            'cw_skill':    cw_skill,
            'cwl_skill':   cwl_skill,
            'reliability': reliability,
            'activity':    activity,
        }

    return result


async def build_cwl_opponent_embeds(
    opp_clan_tag: str,
    opp_clan_name: str,
    season: str,
    roster: List[Dict[str, Any]],
    my_clan_tag: str,
    my_clan_name: str,
    round_number: Optional[int] = None,
) -> List[discord.Embed]:
    """
    Build Discord embeds for /analyse cwlopponent.

    The player table is rendered inside a monospace code block so all columns
    align regardless of name length.  Each embed contains a code block with
    the full column header + separator + as many player rows as fit under the
    4096-char embed description limit.

    Parameters
    ----------
    opp_clan_tag / opp_clan_name : opponent clan
    season                       : CWL season string "YYYY-MM"
    roster                       : list of {player_tag, player_name, th_level, map_position}
    my_clan_tag / my_clan_name   : the requesting clan (shown in footer)
    """
    if not roster:
        return [discord.Embed(
            description=f"❌ No roster data found for **{normalize_player_name(opp_clan_name)}** in season {season}.",
            color=0xe74c3c,
        )]

    # Sort by map_position ascending (war lineup order); unknown positions last
    roster = sorted(roster, key=lambda r: int(r.get('map_position') or 99))
    player_tags = [r['player_tag'] for r in roster]

    # Bulk-compute all stats in one thread call
    stats_map = await asyncio.to_thread(compute_roster_stats_sync, player_tags)

    # ── Column widths ─────────────────────────────────────────────────────────
    W_NUM  = 2   # "35" max
    W_TH   = 2   # "17" max
    W_NAME = 13  # truncated name (no tag col; 1-space sep → 43 chars total)
    W_STAT = 5   # "-100%" … " 100%" … "    —"

    def _val(v: Optional[int]) -> str:
        if v is None:
            return '    —'
        return f"{v}%".rjust(W_STAT)

    # Single-space separators → total line length = 2+1+2+1+14+1+5+1+5+1+5+1+5 = 44 chars
    HDR = (
        f"{'#':>{W_NUM}} {'TH':>{W_TH}} {'Player':<{W_NAME}}"
        f" {'CWL%':>{W_STAT}} {'CW%':>{W_STAT}}"
        f" {'Rel%':>{W_STAT}} {'Act%':>{W_STAT}}"
    )
    SEP = '─' * len(HDR)

    # Build all data rows
    data_rows: List[str] = []
    for pos, r in enumerate(roster, start=1):
        tag   = r['player_tag']
        raw_name = str(r.get('player_name') or tag)
        # Use the same rendering pipeline as the leaderboard: normalize → truncate by
        # display width → gap-fill so the column aligns in Discord's monospace font.
        name  = best_practice_player_cell(raw_name, W_NAME)
        th    = int(r.get('th_level') or 0)
        th_s  = str(th).rjust(W_TH) if th else ' ?'
        s     = stats_map.get(tag, {})
        data_rows.append(
            f"{pos:>{W_NUM}} {th_s} {name}"
            f" {_val(s.get('cwl_skill'))} {_val(s.get('cw_skill'))}"
            f" {_val(s.get('reliability'))} {_val(s.get('activity'))}"
        )

    # ── Shared metadata ───────────────────────────────────────────────────────
    opp_display  = normalize_player_name(opp_clan_name)
    my_display   = normalize_player_name(my_clan_name)
    opp_url      = coc_clan_profile_url(opp_clan_tag)
    clan_header  = (
        f"[{opp_display}]({opp_url})  ({opp_clan_tag})"
        f" — {len(roster)} players"
    )
    footer_text  = (
        f"Season {season} · Requested for {my_display} ({my_clan_tag})"
        f" · CWL Skill = (3★−1★)/n%"
    )

    # ── Assemble embeds, splitting only when necessary ────────────────────────
    # Conservative budget: 4096 - clan_header (~80) - ``` fences (8) - HDR+SEP (~150) - "\n" slack
    _MAX_ROWS_CHARS = 3600
    embeds: List[discord.Embed] = []

    def _make_embed(batch: List[str], first: bool) -> discord.Embed:
        table = f"```\n{HDR}\n{SEP}\n" + "\n".join(batch) + "\n```"
        if first:
            desc = f"{clan_header}\n\n{table}"
            e = discord.Embed(
                title=f"🔍 CWL Round {round_number} Opponent: {opp_display}" if round_number else f"🔍 CWL Opponent: {opp_display}",
                description=desc,
                color=0x3498db,
            )
        else:
            desc = f"*(continued — {opp_display})*\n```\n{HDR}\n{SEP}\n" + "\n".join(batch) + "\n```"
            e = discord.Embed(description=desc, color=0x3498db)
        e.set_footer(text=footer_text)
        return e

    current_batch: List[str] = []
    current_chars = 0
    for row in data_rows:
        row_len = len(row) + 1  # +1 for \n
        if current_batch and current_chars + row_len > _MAX_ROWS_CHARS:
            embeds.append(_make_embed(current_batch, first=not embeds))
            current_batch = []
            current_chars = 0
        current_batch.append(row)
        current_chars += row_len

    if current_batch:
        embeds.append(_make_embed(current_batch, first=not embeds))

    return embeds


async def post_discord_content_with_tracking(
    clan_tag: str,
    channel: Union[discord.TextChannel, discord.Thread],
    mode: str = 'cwlinfo',
    *,
    embeds: Optional[List[discord.Embed]] = None,
    file_bytes: Optional[bytes] = None,
    file_name: str = "attachment.png",
    content_hash: str = '',
    update_existing: bool = False,
    debug_content: str = '',
) -> None:
    """
    Unified Discord posting function with message-ID tracking for later cleanup.

    Supports two mutually exclusive content types:
    - embeds: list of discord.Embed objects (e.g. cwlinfo, cwlinfo_comp)
    - file_bytes + file_name: raw bytes for a file attachment (e.g. cwlgroup PNG)

    Supports two update strategies via update_existing:
    - False (default): delete any existing tracked message for this clan/channel/mode,
      then post a new one. Used by cwlinfo/cwlinfo_comp.
    - True: find the existing tracked message; skip when content hash is unchanged;
      edit in-place when changed; post new if not found or edit fails.
      Used by cwlgroup — avoids noisy delete-and-repost for image updates.

    Args:
        clan_tag: Clash of Clans clan tag (e.g. "#ABC123").
        channel: Discord channel or thread to post into.
        mode: Leaderboard mode string stored in leaderboard_messages tracking
            (e.g. 'cwlinfo', 'cwlinfo_comp', 'cwlgroup_2026-05').
        embeds: Discord embeds to send. Mutually exclusive with file_bytes.
        file_bytes: Raw bytes for a file attachment. Mutually exclusive with embeds.
        file_name: Filename used for the attachment (default: "attachment.png").
        content_hash: Pre-computed content hash for change detection. When absent
            and file_bytes is provided, computed automatically from file_bytes.hex().
        update_existing: When True, edit the existing message in-place instead of
            deleting and reposting. Default False.
        debug_content: Optional extra plain-text sent after embeds (cwlinfo_comp only).
    """
    from io import BytesIO
    channel_id = str(channel.id)
    log_tag = mode.upper()

    # Auto-compute content hash for file content when not supplied by caller.
    effective_hash = content_hash
    if not effective_hash and file_bytes is not None:
        effective_hash = calculate_content_hash(file_bytes.hex())

    # Helpers that create fresh discord.File / BytesIO on every call so that
    # discord_retry can safely retry the coroutine without a consumed stream.
    def _send_call() -> Any:
        if embeds is not None:
            return channel.send(embeds=embeds)
        return channel.send(file=discord.File(fp=BytesIO(file_bytes), filename=file_name))  # type: ignore[arg-type]

    def _edit_call(msg: discord.Message) -> Any:
        if embeds is not None:
            return msg.edit(embeds=embeds)
        return msg.edit(attachments=[discord.File(fp=BytesIO(file_bytes), filename=file_name)])  # type: ignore[arg-type]

    if update_existing:
        # ── Edit-in-place path ─────────────────────────────────────────────
        existing_key: Optional[str] = None
        existing_entry: Optional[Dict[str, Any]] = None
        for k, v in CACHE.leaderboard_messages.items():
            if (
                v.get("clan_tag") == clan_tag
                and v.get("channel_id") == channel_id
                and v.get("mode", "").lower() == mode.lower()
            ):
                existing_key = k
                existing_entry = dict(v)
                break

        if existing_entry and existing_key:
            if existing_entry.get("content_hash") == effective_hash:
                logging.info(
                    f"Skipping {clan_tag}, type {mode} - content unchanged (hash: {effective_hash[:8]}...)"
                )
                return

            msg_id_str = existing_entry.get("message_ids", "")
            msg_id = int(msg_id_str.split(",")[0]) if msg_id_str else None
            if msg_id:
                try:
                    msg = await channel.fetch_message(msg_id)
                    await discord_retry(
                        lambda m=msg: _edit_call(m),
                        f"edit_{mode}_{msg_id}",
                    )
                    existing_entry["content_hash"] = effective_hash
                    await CACHE.set_leaderboard_message(existing_key, existing_entry)
                    logging.info(
                        f"[{log_tag}] Updated existing message {msg_id} for {clan_tag} in {channel_id}."
                    )
                    return
                except discord.NotFound:
                    logging.info(
                        f"[{log_tag}] Message {msg_id} not found — posting new for {clan_tag}."
                    )
                    await CACHE.delete_leaderboard_message(existing_key)
                except Exception as e:
                    logging.warning(
                        f"[{log_tag}] Could not edit message {msg_id} for {clan_tag}: {e} — posting new."
                    )
                    await CACHE.delete_leaderboard_message(existing_key)
        # Fall through to post a new message.

    else:
        # ── Delete-and-repost path ──────────────────────────────────────────
        # Delete using exact mode match — avoids delete_leaderboard_messages_for_context
        # which appends _{month}_{year} and would never find fully-qualified mode strings
        # like "cwlgroup_2026-05".
        from qapbot.QBdiscocmdshelper import _delete_messages_by_filter  # type: ignore[misc,attr-defined]
        await _delete_messages_by_filter(
            channel_id,
            lambda k, v: (
                v.get("mode", "").lower() == mode.lower()
                and v.get("clan_tag") == clan_tag
                and v.get("channel_id") == channel_id
            ),
            f"clan {clan_tag} mode {mode}",
        )
        if embeds is not None and not embeds:
            return  # Nothing to post.

    # Post a new message.
    timestamp = generate_message_key_timestamp()
    try:
        new_msg = await discord_retry(
            _send_call,
            f"send_{mode}",
        )
        if new_msg:  # type: ignore[truthy-bool]
            await CACHE.set_leaderboard_message(timestamp, {
                "clan_tag": clan_tag,
                "channel_id": channel_id,
                "mode": mode,
                "message_ids": str(new_msg.id),  # type: ignore[union-attr]
                "content_hash": effective_hash,
            })
            logging.info(
                f"[{log_tag}] Posted new message {new_msg.id} for {clan_tag} in {channel_id}."
            )
    except Exception as e:
        raise LeaderboardPostingError(
            f"Error posting {mode} content for {clan_tag} in channel {channel.id}: {e}",
            context={"clan_tag": clan_tag, "channel_id": channel_id, "mode": mode},
        )
    if debug_content:
        try:
            for _chunk in _split_discord_message(debug_content):
                await discord_retry(
                    lambda c=_chunk: channel.send(c),
                    f"send_{mode}_debug",
                )
        except Exception as _de:
            logging.debug(f"[{log_tag}] Debug message send failed for {clan_tag}: {_de}")


def _periods_contiguous(periods: List[Tuple[int, int]]) -> bool:
    """True if consecutive (month, year) pairs represent consecutive calendar months."""
    for i in range(1, len(periods)):
        pm, py = periods[i - 1]
        cm, cy = periods[i]
        if py * 12 + pm + 1 != cy * 12 + cm:
            return False
    return True

def _format_periods_label(periods: List[Tuple[int, int]]) -> str:
    """
    Human-readable label for an ordered list of (month, year) pairs.

    Examples: "06-07/2026" (contiguous, same year), "2025-12 to 2026-01"
    (contiguous, crossing a year boundary), "01+03+05/2026" (a non-contiguous
    explicit list — must not be rendered as "01-05", which would misleadingly
    imply Feb and Apr were included too).
    """
    if len(periods) == 1:
        m, y = periods[0]
        return f"{m:02d}/{y}"
    years = {y for _, y in periods}
    if _periods_contiguous(periods):
        m0, y0 = periods[0]
        m1, y1 = periods[-1]
        if len(years) == 1:
            return f"{m0:02d}-{m1:02d}/{y0}"
        return f"{y0}-{m0:02d} to {y1}-{m1:02d}"
    if len(years) == 1:
        y0 = years.pop()
        return "+".join(f"{m:02d}" for m, _ in periods) + f"/{y0}"
    return "+".join(f"{y}-{m:02d}" for m, y in periods)

def parse_month_argument(spec: str, now: datetime, explicit_year: Optional[int] = None) -> List[Tuple[int, int]]:
    """
    Parse the /leaderboard `month` command-line argument into an ordered list of
    (month, year) pairs, oldest first.

    Supported forms:
        "6"       -> a single month
        "6-7"     -> an inclusive range (June, July)
        "1;3;5"   -> an explicit list of months (semicolon-separated)
        "-2"      -> the trailing N months, ending at (and including) the
                     current month — may cross a year boundary (e.g. "-2" in
                     January covers December of the previous year + January)

    Raises:
        ValueError: on malformed input, an out-of-range month (range/list forms
            only — the negative form derives months from `now` and can't go out
            of range), or an explicit year combined with the negative form (that
            form is always relative to `now`, so pairing it with a fixed year
            would be ambiguous).
    """
    spec = spec.strip()
    if not spec:
        raise ValueError("Month value must not be empty.")

    if re.fullmatch(r"-\d+", spec):
        n = int(spec[1:])
        if n < 1:
            raise ValueError("Number of trailing months must be at least 1.")
        if explicit_year is not None:
            raise ValueError("Can't combine an explicit year with a relative '-N' month value.")
        pairs: List[Tuple[int, int]] = []
        for offset in range(n - 1, -1, -1):
            total = (now.year * 12 + (now.month - 1)) - offset
            y, m0 = divmod(total, 12)
            pairs.append((m0 + 1, y))
        return pairs

    base_year = explicit_year or now.year
    try:
        if ";" in spec:
            raw_months = [int(x.strip()) for x in spec.split(";") if x.strip()]
            if not raw_months:
                raise ValueError("empty list")
        elif "-" in spec:
            a_str, b_str = spec.split("-", 1)
            a, b = int(a_str), int(b_str)
            raw_months = list(range(a, b + 1)) if a <= b else list(range(a, b - 1, -1))
        else:
            raw_months = [int(spec)]
    except ValueError:
        raise ValueError(
            f"Could not parse month value '{spec}'. Use a month number (6), "
            "a range (6-7), a list (1;3;5), or a trailing count (-2)."
        )

    for m in raw_months:
        if not (1 <= m <= 12):
            raise ValueError(f"Month must be between 1 and 12 (got {m}).")

    pairs = [(m, base_year) for m in raw_months]
    pairs.sort(key=lambda p: p[1] * 100 + p[0])
    return pairs


def get_recent_cwl_player_stats(player_tag: str, num_months: int = 3, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """One player's missed-attack count and attack/defense star ratio, computed EXACTLY as
    `/leaderboard mode=missedattacks|attackdefratio cwl_only=true month=-{num_months} scope=all`
    would compute them for this player (2026-08-16, Manage Enrollment hover pop-up, project
    owner's spec, verbatim: "They should be calculated exactly as the /leaderboard command would
    do it with the modes missedattacks and attackdefratio and with the options cwl_only=true and
    month=-3", plus the follow-up "the option scope=ALL is also important"). Replaces an earlier
    version of this stat that used a different "last 3 CWL seasons this player has ANY history
    for" window (a bespoke SQL query in db_manager.py) — the project owner reported the two didn't
    agree, which is expected: that window doesn't shrink for gaps and isn't tied to calendar
    months at all, while `month=-3` is a fixed trailing-3-calendar-month window that can easily
    contain 0, 1, 2, or 3 real CWL wars for a given player depending on how recently they've
    actually played.

    Deliberately reuses `_load_history_rows()`/`_merge_entries()` — the exact two functions
    `calculate_leaderboard()` itself calls — rather than re-deriving the aggregation
    independently a second time, so this can never again silently drift from what `/leaderboard`
    actually shows. `mode="currentwar"` is passed to `_merge_entries` only (never to
    `calculate_leaderboard`, which would skip loading history entirely for that mode) purely to
    get its own unconditional entries filter — every OTHER mode's filter drops players who don't
    individually qualify THAT SPECIFIC MONTH (e.g. `attackdefratio`/`missedattacks` both require
    a nonzero count that exact month), which would silently undercount a month where this player
    has missed attacks or defensive stars recorded but made zero real attacks that month.

    scope="all" (member_player_tags={player_tag}) — the command's own default — credits this
    player for CWL wars fought under ANY clan, not just clans this guild still tracks, matching
    `_load_history_rows`'s scope="all" dispatch to `get_player_attack_history_sync` exactly; the
    `clan_tag` argument that function normally takes is irrelevant on that path (only scope="own"
    reads it), so an empty string is passed rather than a real tag.

    Doesn't fold in the CURRENTLY in-progress war's temp stats the way the live command does for
    whichever month is "now" — there's no clan context available to look up an in-progress war for
    a bare player_tag outside of a specific clan's context. Catches up automatically once that war
    saves to history, same as the rest of this stat already lags real-time CoC state by one war.

    Returns `{"seasons": [], "attacks": None, "missed_attacks": None, "attack_defense_ratio":
    None}` when this player has zero CWL history anywhere in the window; otherwise `seasons` is
    always the full `num_months`-long list of "YYYY-MM" strings requested (not just the ones with
    data — this is a fixed calendar window, not an adaptive one), oldest first. `attacks` is the
    total number of real attacks made in the window (2026-08-16 follow-up, project owner's spec:
    "Attacks: n (number of total attacks)").

    `now` defaults to the real current time; overridable for deterministic tests, same reason
    parse_month_argument() itself takes `now` as an explicit argument rather than calling
    datetime.now() internally."""
    periods = parse_month_argument(f"-{num_months}", now or datetime.now(_tz.utc))

    total_stars = 0
    total_attacks = 0
    total_missed = 0
    total_defensive = 0
    any_history = False
    for month, year in periods:
        rows = _load_history_rows("", month, year, None, scope="all", member_player_tags={player_tag})
        rows = [r for r in rows if r.get("Max_Attacks", 2) == 1]  # cwl_only, matching calculate_leaderboard()
        entry = _merge_entries(rows, {}, False, mode="currentwar").get(player_tag)
        if entry is None:
            continue
        any_history = True
        total_stars += entry.get("Stars", 0)
        total_attacks += entry.get("Attacks", 0)
        total_missed += entry.get("Missed_Attacks", 0)
        total_defensive += entry.get("Defensive_Stars", 0)

    if not any_history:
        return {"seasons": [], "attacks": None, "missed_attacks": None, "attack_defense_ratio": None}

    return {
        "seasons": [f"{y:04d}-{m:02d}" for m, y in periods],
        "attacks": total_attacks,
        "missed_attacks": total_missed,
        "attack_defense_ratio": round(total_stars / total_defensive, 2) if total_defensive > 0 else None,
    }


def generate_leaderboard_text(
    clan_tag: str,
    month: Optional[Union[int, List[int], List[Tuple[int, int]]]] = None,
    year: Optional[int] = None,
    mode: str = DEFAULT_MODE,
    style: str = "discord",
    cwl_season: Optional[str] = None,
    scope: str = "own",
    member_player_tags: Optional[Set[str]] = None,
    highlight_player_ids: Optional[Set[str]] = None,
) -> str:
    """
    Generate formatted leaderboard text for a clan or clan family.

    This function produces a leaderboard for either a single clan or a whole clan family.
    If a family tag is provided, all player stats are aggregated across all member clans,
    and each player appears only once in the leaderboard, with their stats summed from all clans.

    Args:
        clan_tag: Clash of Clans clan tag or family tag
        month: Month(s) for leaderboard data — a single month (1-12), a list of
            months (assumed to share `year`), or a list of (month, year) pairs
            for periods that cross a year boundary (e.g. Dec + Jan)
        year: Year for leaderboard data (ignored when month is a list of pairs)
        mode: Leaderboard mode (attack, avgstars, attackdefratio, etc.)
        style: Output style ("discord" or "terminal")
        scope: "own" (default) counts only wars fought by the clan(s) themselves;
            "all" counts every war fought by a player currently rostered in the
            target clan(s), even ones fought under a clan no longer tracked/subscribed
        member_player_tags: Current roster tags for the target clan(s); required
            for scope="all", ignored otherwise
        highlight_player_ids: PlayerIDs to bold/color in the rendered table (style="discord"
            only) — see render_leaderboard(). The caller must post the result inside a
            ```ansi code block for the highlight to actually render.

    Returns:
        Formatted leaderboard text ready for Discord or terminal display

    Behavior:
        - Aggregates player stats by PlayerID across all clans in a family
        - Produces one line per player, regardless of how many clans they played for
        - Supports multi-month and single-month aggregation
        - Uses unified formatting and rendering for output

    Example:
        # Get leaderboard for a clan family for August 2025
        text = generate_leaderboard_text("FAMILYTAG", month=8, year=2025)
        # Get leaderboard for a single clan
        text = generate_leaderboard_text("#L2J0C0PY", month=8, year=2025)
    """
    cwl_only = False
    mode = (mode or DEFAULT_MODE).lower()
    if "_cwl" in mode:
        mode = mode.replace("_cwl", "")
        cwl_only = True
    if cwl_season is not None:
        cwl_only = True
    if mode not in MODE_REGISTRY:
        return f"Unknown leaderboard mode: {mode}"
    # cwlgroup is an image-only mode; it must never reach the text renderer.
    if mode == "cwlgroup":
        return f"[cwlgroup] mode is rendered as an image, not text. Subscription routing error."
    # Determine clan_name based on clan_tag type (clan or family)
    if clan_tag in CACHE.clan_families:
        clan_name = CACHE.clan_families[clan_tag].get("name", "FAMILY")
    else:
        clan_name = CACHE.get_clan_name(clan_tag, "UNKNOWN")
    if mode == "currentwar":
        war_info_line = generate_war_info_text(clan_tag)
    else:
        war_info_line = ""
    month_label = ""

    logging.debug(f"generate_leaderboard_text() called with: clan_tag={clan_tag}, clan_name={clan_name}, mode={mode}, month={month}, year={year}, type(month)={type(month)}")

    # --- Unified month handling ---
    # Always build an ordered list of (month, year) pairs to aggregate, even for a
    # single month. Accepting (month, year) pairs directly — rather than just a
    # list of months sharing one `year` — lets a period cross a year boundary,
    # e.g. a "last 2 months" request made in January (Dec of last year + Jan this year).
    if isinstance(month, list) and month and isinstance(month[0], tuple):
        periods: Optional[List[Tuple[int, int]]] = list(month)  # type: ignore[assignment]
    elif isinstance(month, int):
        periods = [(month, year or datetime.now().year)]
    elif isinstance(month, list):
        yy = year or datetime.now().year
        periods = [(m, yy) for m in month]  # type: ignore[misc]
    else:
        # If month is None and not currentwar mode, default to current month
        if mode != "currentwar":
            periods = [(datetime.now().month, year or datetime.now().year)]
        else:
            periods = None

    aggregated: Dict[str, Dict[str, Any]] = {}
    if periods is None:
        stats_by_player = {}
    else:
        for m, y in periods:
            logging.debug(f"Processing month {m}/{y} for aggregation")
            stats = calculate_leaderboard(clan_tag, m, y, cwl_only=cwl_only, mode=mode, cwl_season=cwl_season, scope=scope, member_player_tags=member_player_tags)
            logging.debug(f"Stats for month {m}/{y}: {len(stats)} players")
            # Load history for this specific month to get war IDs
            history_rows = _load_history_rows(clan_tag, m, y, cwl_season, scope=scope, member_player_tags=member_player_tags)
            month_war_ids: set[str] = set()
            for row in history_rows:
                war_id = row.get("WarID") or row.get("WarId", "")
                if war_id:
                    month_war_ids.add(war_id)  # type: ignore[misc]
            # Sortable across year boundaries — plain month numbers alone would treat
            # January (1) as "earlier" than December (12) of the previous period.
            period_key = y * 100 + m
            for pid, v in stats.items():
                if pid not in aggregated:
                    aggregated[pid] = v.copy()
                    aggregated[pid]["_war_ids"] = month_war_ids.copy()
                    # Track latest period for TH level updates
                    aggregated[pid]["_last_period_key"] = period_key
                else:
                    # Only add stats from wars we haven't seen before
                    new_wars = month_war_ids - aggregated[pid].get("_war_ids", set())
                    if new_wars or (m, y) == periods[-1]:  # Always include the last period for ongoing wars
                        aggregated[pid]["Stars"] += v.get("Stars", 0)
                        aggregated[pid]["Attacks"] += v.get("Attacks", 0)
                        aggregated[pid]["Missed_Attacks"] += v.get("Missed_Attacks", 0)
                        aggregated[pid]["Defensive_Stars"] += v.get("Defensive_Stars", 0)
                        aggregated[pid]["Total_Dest_Pct"] = aggregated[pid].get("Total_Dest_Pct", 0.0) + v.get("Total_Dest_Pct", 0.0)
                        aggregated[pid]["Defs_Count"] = aggregated[pid].get("Defs_Count", 0) + v.get("Defs_Count", 0)
                        aggregated[pid]["Wars_Count"] = len(aggregated[pid].get("_war_ids", set()) | month_war_ids)
                        aggregated[pid]["_war_ids"] |= month_war_ids
                        # BUGFIX (2026-02-07): Always use latest period's TH level for multi-month aggregations
                        # This ensures players who upgrade TH mid-period show their current TH level
                        if period_key >= aggregated[pid].get("_last_period_key", 0):
                            aggregated[pid]["TH_lvl"] = v.get("TH_lvl", aggregated[pid].get("TH_lvl", 0))
                            aggregated[pid]["_last_period_key"] = period_key
        # Clean up temporary war ID tracking and period tracking, then recompute derived metrics
        for pid, agg in aggregated.items():
            agg.pop("_war_ids", None)
            agg.pop("_last_period_key", None)
            defs = agg.get("Defs_Count", 0)
            agg["Stars_per_Def"] = (agg.get("Defensive_Stars", 0) / defs) if defs else 0.0
            agg["Def_Stars_per_War"] = agg["Stars_per_Def"]  # legacy alias
            attacks_total = agg.get("Attacks", 0) + agg.get("Missed_Attacks", 0)
            agg["Avg_Dest_Pct"] = (agg.get("Total_Dest_Pct", 0.0) / attacks_total) if attacks_total > 0 else 0.0
        stats_by_player = aggregated
        # Month label formatting
        if mode != "currentwar":
            if cwl_season and len(cwl_season.split("-")) == 3:
                # Dated mid-month CWL season (e.g. "2026-06-15"): show exact season key
                month_label = f" for {cwl_season}"
            else:
                month_label = f" for {_format_periods_label(periods)}"
        logging.debug(f"Unified aggregation complete. Total players: {len(stats_by_player)}")
    if not stats_by_player:
        _mode_short_labels: Dict[str, str] = {
            "attack":         "Attack Leaderboard",
            "avgstars":       "Avg Stars/Atk Leaderboard",
            "avgstarsbyth":   "Avg Stars/Atk by TH Leaderboard",
            "attackdefratio": "Atk/Def-Ratio Leaderboard",
            "missedattacks":  "Missed Attacks Leaderboard",
            "defense":        "Defense Leaderboard",
            "currentwar":     "Current War",
        }
        mode_label = _mode_short_labels.get(mode, mode.title())
        if cwl_only:
            mode_label += " (CWL)"
        if mode == "currentwar":
            return f"⭐ {clan_name} \u2014 {mode_label}: No active war at the moment."
        period_str = month_label.strip()
        if period_str.startswith("for "):
            period_str = period_str[4:]
        return (
            f"⭐ {clan_name} \u2014 {mode_label}: "
            f"No wars recorded for {period_str}."
        )
    month_label += (" CWL" if cwl_only and mode != "currentwar" else "")
    logging.debug(f"Unified month label: {month_label}")
    # Output stats_by_player in human readable form for debugging
    if mode == "currentwar":
        logging.debug("Leaderboard stats_by_player:")
        for pid, stats in stats_by_player.items():
            logging.debug(f"PlayerID: {pid}, Name: {stats.get('Player','')}, Stars: {stats.get('Stars',0)}, Attacks: {stats.get('Attacks',0)}, Missed_Attacks: {stats.get('Missed_Attacks',0)}, Defensive_Stars: {stats.get('Defensive_Stars',0)}, Wars_Count: {stats.get('Wars_Count',0)}, Def_Stars_per_War: {stats.get('Def_Stars_per_War',0.0)}")
    return render_leaderboard(clan_tag, clan_name or "Unknown", month_label, war_info_line, stats_by_player, mode, style=style, highlight_player_ids=highlight_player_ids)

def resolve_subscription_period(sub: Dict[str, Any], now: Optional[datetime] = None) -> Tuple[int, int, Union[int, List[int]]]:
    """
    Resolve the (month, year, month_range) a subscription targets "right now".

    Mirrors the period logic used by the automatic per-cycle leaderboard posting
    loop (post_leaderboards_to_subscribed_channels() in QapBot.py), so a manual
    re-render of a subscription (e.g. /highlightme) matches what the next
    automatic post would show.

    Args:
        sub: Subscription dict with optional 'month' (int) and 'year' (int or
            the literal string "current" for year-to-date) keys.
        now: Reference time; defaults to datetime.now().

    Returns:
        month: The current calendar month (used by callers for month-1 fallback
            logic when a month has no recorded wars yet).
        year: Target year.
        month_range: What to pass as generate_leaderboard_text()'s `month` arg —
            an int for a single month, or a list of ints for year-to-date.
    """
    now = now or datetime.now()
    sub_month = sub.get('month')
    sub_year = sub.get('year')
    month = now.month
    year = now.year
    month_range: Union[int, List[int]] = month
    if sub_year == "current":
        year = now.year
        month_range = list(range(1, now.month + 1))
    elif isinstance(sub_year, int):
        year = sub_year
    if isinstance(sub_month, int):
        month = sub_month
        month_range = month
    return month, year, month_range

# --- Message key generation (single source of truth) ---

def generate_message_key_timestamp() -> str:
    """
    Generate a standardized timestamp string for leaderboard message keys.
    Returns:
        Timestamp string in format 'YYYY-MM-DDTHH%M%S.mmm'
    """
    return datetime.now(_tz.utc).strftime('%Y-%m-%dT%H%M%S.%f')[:-3]

async def delete_leaderboard_messages_for_context(clan_tag: str, channel_id: str, mode: str, month: str, year: int, cwl_only: bool = False) -> None:
    """
    Delete all leaderboard messages for the given clan, channel, mode, month, year, and cwl_only from Discord and cache.
    
    Args:
        clan_tag: Clan tag (uppercase)
        channel_id: Discord channel ID as string
        mode: Leaderboard mode (e.g., 'attack')
        month: Month string (e.g., '08' or 'current' or '01-06')
        year: Year as int
        cwl_only: Whether to restrict to CWL-only messages
    """
    from qapbot.QBdiscocmdshelper import _delete_messages_by_filter  # type: ignore[misc,attr-defined]
    
    # Compose mode string
    # "currentwar" and "cwlinfo" use just their name (no month/year suffix).
    if mode in ("currentwar", "cwlinfo"):
        mode_str = mode
    elif cwl_only:
        mode_str = f"{mode}_cwl_{month}_{year}"
    else:
        mode_str = f"{mode}_{month}_{year}"
    
    # Use unified deletion helper with mode-specific filter
    await _delete_messages_by_filter(
        channel_id,
        lambda k, v: (  # type: ignore[misc]
            v.get('mode', '').lower() == mode_str.lower()  # type: ignore[union-attr]
            and v.get('clan_tag') == clan_tag  # type: ignore[misc]
            and v.get('channel_id') == channel_id  # type: ignore[misc]
        ),
        f"clan {clan_tag} mode {mode_str}"
    )

async def _split_and_post_leaderboard_helper(channel: Union[discord.TextChannel, discord.Thread], text: str) -> List[int]:
    """
    Helper function for intelligent leaderboard splitting. Called from post_leaderboard_to_discord exclusively.

    Args:
        channel: Discord channel to post messages to
        text: Complete leaderboard text to split and post
        
    Returns:
        List of Discord message IDs that were posted
    """
    messages_posted: list[int] = []

    # If the text contains plain-text (emoji) sentinel sections, extract them and
    # combine each plain segment with its following code-block segment into one
    # Discord message: plain text first (custom emojis render), then a ``` code
    # block for the monospace stats table — all in a single message.
    if _PLAIN_SENTINEL_START in text:
        # Absorb any text before the first sentinel (e.g. "⭐ clan - Current War:\n")
        # into the first plain segment so the header and rosters appear together.
        first_pos = text.index(_PLAIN_SENTINEL_START)
        prefix = text[:first_pos].strip('\n')
        rest = text[first_pos:]

        pattern = re.compile(
            re.escape(_PLAIN_SENTINEL_START) + r'(.*?)' + re.escape(_PLAIN_SENTINEL_END),
            re.DOTALL
        )
        # split gives: ["", plain1, code1, plain2, code2, ...]
        # parts[0] is "" because rest starts with the sentinel.
        parts = pattern.split(rest)
        all_ids: list[int] = []

        # Handle any code content that appears before the very first sentinel (parts[0]).
        if parts[0].strip('\n'):
            ids = await _split_and_post_leaderboard_helper(channel, parts[0].strip('\n'))
            all_ids.extend(ids)

        # Pair each plain segment (odd index) with the code segment that follows it
        # (next even index) and send as one combined message so the output is a
        # single Discord post instead of two.
        for i in range(1, len(parts), 2):
            plain = parts[i].strip('\n')
            if i == 1 and prefix:
                plain = prefix + '\n\n' + plain

            # The code segment immediately after this plain segment (may be empty).
            code = parts[i + 1].strip('\n') if i + 1 < len(parts) else ''

            if not plain and not code:
                continue

            if plain and code:
                # Combine: plain text then code block in one message.
                combined = plain + '\n```ansi\n' + code + '\n```'
                if len(combined) <= DISCORD_MESSAGE_MAX_LENGTH:
                    combined_msg = await discord_retry(
                        lambda m=combined: channel.send(m),
                        "send_combined_plain_and_code"
                    )
                    all_ids.append(combined_msg.id)  # type: ignore[union-attr]
                    continue
                # Combined too long — fall through to separate posting below.

            # Fallback: post plain and code separately (e.g. very long outputs).
            if plain:
                plain_chunks: List[str] = []
                current_chunk_lines: List[str] = []
                current_len = 0
                for pline in plain.split('\n'):
                    needed = len(pline) + (1 if current_chunk_lines else 0)
                    if current_chunk_lines and current_len + needed > DISCORD_MESSAGE_MAX_LENGTH:
                        plain_chunks.append('\n'.join(current_chunk_lines))
                        current_chunk_lines = ['\u2800', pline]
                        current_len = len('\u2800') + 1 + len(pline)
                    else:
                        current_chunk_lines.append(pline)
                        current_len += needed
                if current_chunk_lines:
                    plain_chunks.append('\n'.join(current_chunk_lines))
                for chunk in plain_chunks:
                    if chunk.strip():
                        plain_msg = await discord_retry(
                            lambda p=chunk: channel.send(p),
                            "send_roster_plain_text"
                        )
                        all_ids.append(plain_msg.id)  # type: ignore[union-attr]
            if code:
                ids = await _split_and_post_leaderboard_helper(channel, code)
                all_ids.extend(ids)

        return all_ids

    # If message is short enough, post directly without splitting logic
    DISCORD_LIMIT = DISCORD_MESSAGE_MAX_LENGTH
    formatted_text = '```ansi\n' + text + '```'
    if len(formatted_text) <= DISCORD_LIMIT:
        msg = await discord_retry(
            lambda: channel.send(formatted_text),
            "send_short_leaderboard"
        )
        messages_posted.append(msg.id)  # type: ignore[union-attr]
        logging.debug(f"Posted short message {msg.id} ({len(formatted_text)} chars) without splitting")  # type: ignore[union-attr]
        return messages_posted  # type: ignore[return-value]
    
    lines = text.split('\n')
    # Find where the player table starts
    header_lines: list[str] = []
    table_lines: list[str] = []
    found_table_start = False
    
    for _i, line in enumerate(lines):
        if not found_table_start and 'Player' in line and 'Stars' in line and 'Attacks' in line:
            found_table_start = True
            table_lines.append(line)  # type: ignore[misc]  # Include header with table
        elif found_table_start:
            table_lines.append(line)  # type: ignore[misc]
        else:
            header_lines.append(line)  # type: ignore[misc]
    
    # If we couldn't find the table, fall back to simple split
    if not found_table_start:
        # Log first few lines to help diagnose why header wasn't found
        first_lines_sample = '\n'.join(lines[:5]) if len(lines) >= 5 else '\n'.join(lines)
        logging.warning(
            f"Could not find player table header (looking for line with 'Player', 'Stars', 'Attacks'), using simple split. "
            f"Total lines: {len(lines)}. First 5 lines:\n{first_lines_sample}"
        )
        mid_point = len(lines) // 2
        header_lines = lines[:mid_point]
        table_lines = lines[mid_point:]
    
    # Prepare header content
    header_content = '\n'.join(header_lines).strip()
    
    # Process table with intelligent splitting
    if not table_lines:
        # No table, just post header
        if header_content:
            formatted_header = '```ansi\n' + header_content + '```'
            msg = await discord_retry(
                lambda: channel.send(formatted_header),
                "send_leaderboard_header"
            )
            messages_posted.append(msg.id)  # type: ignore[union-attr]
            logging.debug(f"Posted header-only message {msg.id} ({len(formatted_header)} chars)")  # type: ignore[union-attr]
    else:
        # Split table intelligently
        table_header = table_lines[0] if table_lines else ""  # "Player ... Stars ..." line
        table_separator = table_lines[1] if len(table_lines) > 1 else ""  # "------" line
        player_lines = table_lines[2:] if len(table_lines) > 2 else []  # Actual player data
        
        # Calculate base sizes
        header_size = len(header_content) if header_content else 0
        table_header_size = len(table_header) + len(table_separator) + 2 if table_header else 0  # +2 for newlines
        code_block_overhead = 11  # ```ansi\n ... ``` — tagged fence so ANSI highlight codes render
        safety_margin = 50  # Extra safety margin
        
        # First message: header + table header + as many players as fit
        first_chunk_players = []
        current_size = header_size + table_header_size + code_block_overhead + safety_margin
        
        # Add blank line between header and table
        if header_content and table_header:
            current_size += 1  # +1 for blank line
        
        for player_line in player_lines:
            line_size = len(player_line) + 1  # +1 for newline
            if current_size + line_size > 2000:
                break
            first_chunk_players.append(player_line)  # type: ignore[misc]
            current_size += line_size
        
        # Build and post first message
        first_content_parts: list[str] = []
        if header_content:
            first_content_parts.append(header_content)  # type: ignore[misc]
        if table_header:
            first_content_parts.append('')  # type: ignore[misc]  # Add blank line before table
            first_content_parts.append(table_header)  # type: ignore[misc]
        if table_separator:
            first_content_parts.append(table_separator)  # type: ignore[misc]
        first_content_parts.extend(first_chunk_players)  # type: ignore[misc]
        
        first_message = '```ansi\n' + '\n'.join(first_content_parts) + '```'  # type: ignore[arg-type]
        msg1 = await discord_retry(
            lambda: channel.send(first_message),
            "send_leaderboard_first_chunk"
        )
        messages_posted.append(msg1.id)  # type: ignore[union-attr]
        logging.debug(f"Posted first chunk message {msg1.id} ({len(first_message)} chars) with {len(first_chunk_players)} players")  # type: ignore[union-attr]
        
        # Remaining players in continuation messages
        remaining_players = player_lines[len(first_chunk_players):]  # type: ignore[arg-type]
        
        while remaining_players:
            chunk_players: list[str] = []
            # Start with just players for continuation (no headers)
            current_size = code_block_overhead + safety_margin
            
            for player_line in remaining_players:
                line_size = len(player_line) + 1  # +1 for newline
                if current_size + line_size > 2000:
                    break
                chunk_players.append(player_line)  # type: ignore[misc]
                current_size += line_size
            
            if not chunk_players:
                # Single player line is too long - truncate it
                logging.warning("Single player line too long, truncating")
                chunk_players = [remaining_players[0][:1900] + "..."]
                remaining_players = remaining_players[1:]
            else:
                remaining_players = remaining_players[len(chunk_players):]  # type: ignore[arg-type]
            
            # Build continuation message - just players, no headers
            continuation_message = '```ansi\n' + '\n'.join(chunk_players) + '```'  # type: ignore[arg-type]
            msg = await discord_retry(
                lambda: channel.send(continuation_message),
                f"send_leaderboard_continuation_{len(messages_posted)}"  # type: ignore[arg-type]
            )
            messages_posted.append(msg.id)  # type: ignore[union-attr]
            logging.debug(f"Posted continuation message {msg.id} ({len(continuation_message)} chars) with {len(chunk_players)} players")  # type: ignore[union-attr]
    
    if len(messages_posted) > 1: logging.info(f"Split leaderboard into {len(messages_posted)} messages")  # type: ignore[arg-type]
    return messages_posted  # type: ignore[return-value]

def _is_war_in_history(clan_tag: str, war_id: str) -> bool:
    """
    Check if a specific war ID already exists in the clan's war_attacks database.
    
    Args:
        clan_tag: The clan tag (e.g., "#2J2YC9GC2")
        war_id: The war ID to check (e.g., "2Y2RPP90R_202512191219")
    
    Returns:
        True if the war is already in history, False otherwise
    """
    if not war_id:
        return False
    
    try:
        # Check database for war existence
        if CACHE.db_manager is None:  # type: ignore[has-type]
            raise RuntimeError("Database manager not initialized")
        exists: bool = CACHE.db_manager.war_attacks_exist_sync(clan_tag, war_id)  # type: ignore[union-attr]
        return exists
    except Exception as e:
        raise WarProcessingError(
            f"Error checking war history for {clan_tag}: {e}",
            context={"clan_tag": clan_tag, "war_id": war_id}
        )

def _parse_war_stats_from_api(coc_war_obj: Any, war_id: str, start_dt_iso: str) -> dict[str, Any]:
    """Parse war statistics from coc.War API object into internal format.
    Used for: war finalization, late attack updates, late subscription handling."""
    war_stats = {}
    attacks_per_member = getattr(coc_war_obj, 'attacks_per_member', 2)
    my_clan = coc_war_obj.clan
    
    for member in my_clan.members:
        player_id = member.tag
        attacks = list(member.attacks) if member.attacks else []
        num_attacks = len(attacks)
        total_stars = sum(atk.stars for atk in attacks)
        missed_attacks = max(attacks_per_member - num_attacks, 0)
        best_opp_attack = getattr(member, 'best_opponent_attack', None)
        defensive_stars = best_opp_attack.stars if best_opp_attack else 0
        town_hall = getattr(member, 'town_hall', 0)
        
        war_stats[player_id] = {
            "WarID": war_id,
            "Date": start_dt_iso,
            "Player": member.name,
            "PlayerID": player_id,
            "TH_lvl": town_hall,
            "Stars": total_stars,
            "Attacks": num_attacks,
            "Missed_Attacks": missed_attacks,
            "Max_Attacks": attacks_per_member,
            "Defensive_Stars": defensive_stars,
            "Times_Defended": 1 if best_opp_attack is not None else 0,
        }
    
    return war_stats  # type: ignore[return-value]


def _process_war_history(clan_tag: str, json_file_path: str, war_type_str: Optional[str] = None, archive_set: Optional[Set[str]] = None) -> bool:
    """
    Consolidated helper to process war history from JSON file.
    
    Handles:
    - Load war data from JSON
    - Check if war already in database history
    - Update history if changes detected (late attacks)
    - Append to history if new war
    - Archive JSON with content comparison (see QBcsvhandling._append_current_war_to_history)
    - Invalidate history cache
    
    Archive Logic (handled by QBcsvhandling._append_current_war_to_history):
    - Archive doesn't exist → Move temp to archive
    - Archive exists + identical → Delete temp (duplicate)
    - Archive exists + differs → Replace archive (late attacks)
    
    Args:
        clan_tag: Clan tag (e.g., '#2R0GYVLJJ')
        json_file_path: Full path to JSON war data file
        war_type_str: War type ('cwl', 'random', 'friendly', 'unknown'). If None, extracts from JSON.
        archive_set: Optional pre-built set of archive filenames for O(1) existence checks.
    
    Returns:
        True if history processed and JSON archived, False on failure
    """
    import QBcsvhandling
    import json
    
    try:
        # Load JSON war object directly from file
        if not os.path.exists(json_file_path):
            logging.error(f"[PROCESS-WAR-HISTORY] JSON file not found: {json_file_path}")
            return False
        
        # CRITICAL FIX (2026-01-09): Check if archive already exists with identical content
        # Scenario: War ended, API still returns it (no new war started), Phase 1 saves JSON again
        # Solution: Compare temp file with archive, if identical just delete temp
        import QBcsvhandling as _qbc_shard
        _basename = os.path.basename(json_file_path)
        archive_dir = _qbc_shard.get_war_shard_dir(_basename.split("_")[0], CONFIG.archive_dir)
        archive_file = os.path.join(archive_dir, _basename)
        _archive_exists = (_basename in archive_set) if archive_set is not None else os.path.exists(archive_file)
        # If archive_set says the file exists but the computed shard path doesn't have it
        # (can happen when shard bounds changed between when the file was archived and now),
        # search all shards as a fallback so we never crash on open().
        if _archive_exists and not os.path.exists(archive_file):
            _fallback_path = None
            for _shard_i in range(10):  # 10 shards (matches QBcsvhandling._SHARD_COUNT)
                _candidate = os.path.join(CONFIG.archive_dir, f"shard_{_shard_i}", _basename)
                if os.path.exists(_candidate):
                    _fallback_path = _candidate
                    break
            if _fallback_path:
                archive_file = _fallback_path  # use the actual location
            else:
                logging.debug(f"[ARCHIVE-CHECK] {_basename} in archive_set but missing on disk — treating as new")
                _archive_exists = False
        logging.debug(f"[DUPLICATE-CHECK-START] Checking {_basename}, archive_exists={_archive_exists}")
        
        if _archive_exists:
            # Compare file contents
            try:
                with open(json_file_path, 'r', encoding='utf-8') as f1:
                    temp_content = f1.read()
                with open(archive_file, 'r', encoding='utf-8') as f2:
                    archive_content = f2.read()
                
                logging.debug(f"[DUPLICATE-CHECK] Comparing temp vs archive for {os.path.basename(json_file_path)}: temp_len={len(temp_content)}, archive_len={len(archive_content)}, equal={temp_content == archive_content}")
                
                if temp_content == archive_content:
                    # Identical - just delete temp file, no reprocessing needed
                    os.remove(json_file_path)
                    logging.info(f"[DUPLICATE-SKIP] Deleted duplicate temp file {os.path.basename(json_file_path)} - identical to archive")
                    CACHE.record_cycle_stat("pwh_duplicate_skip")
                    return True
                else:
                    logging.info(f"[ARCHIVE-DIFFERS] Archive exists but content differs for {os.path.basename(json_file_path)} - checking completeness")
                    # Regression guard: never replace an archive that has more attacks than the temp.
                    # A war_ended snapshot fetched via get_league_war() must always be >= any
                    # earlier snapshot because CoC never removes attacks retroactively.
                    # If temp has fewer attacks the archive is more complete — discard temp.
                    try:
                        _temp_obj = json.loads(temp_content)
                        _arch_obj = json.loads(archive_content)
                        _temp_atk = sum(len(m.get('attacks') or []) for _side in ('clan', 'opponent') for m in (_temp_obj.get(_side) or {}).get('members', []))  # type: ignore[misc]
                        _arch_atk = sum(len(m.get('attacks') or []) for _side in ('clan', 'opponent') for m in (_arch_obj.get(_side) or {}).get('members', []))  # type: ignore[misc]
                        if _temp_atk < _arch_atk:
                            os.remove(json_file_path)
                            logging.warning(
                                f"[REGRESSION-GUARD] Discarded temp {os.path.basename(json_file_path)}: "
                                f"temp has {_temp_atk} attacks < archive {_arch_atk} — archive preserved"
                            )
                            CACHE.record_cycle_stat("pwh_regression_guard")
                            return True
                        logging.info(f"[ARCHIVE-DIFFERS] temp={_temp_atk} attacks >= archive={_arch_atk} attacks — proceeding with update")
                        CACHE.record_cycle_stat("pwh_archive_differs")
                    except Exception as _guard_ex:
                        logging.warning(f"[REGRESSION-GUARD] Could not compare attack counts for {os.path.basename(json_file_path)}: {_guard_ex} — proceeding with update")
            except (WarProcessingError, OSError) as ex:
                logging.warning(f"[ARCHIVE-CHECK] Could not compare temp vs archive for {os.path.basename(json_file_path)}: {ex} - proceeding with normal processing")
        
        
        with open(json_file_path, 'r', encoding='utf-8') as f:
            war_obj = json.load(f)
        
        if not war_obj or 'clan' not in war_obj or 'members' not in war_obj['clan']:
            logging.error(f"[PROCESS-WAR-HISTORY] Invalid JSON structure in {os.path.basename(json_file_path)}")
            return False
        
        # Convert JSON war object to war_data dict (CSV-compatible format)
        war_data = {}
        _state = war_obj.get('state', 'unknown')
        start_time = war_obj.get('start_time', '')
        opponent = war_obj.get('opponent')
        
        # Handle missing opponent gracefully (can happen for corrupted JSON or API issues)
        if not opponent or not isinstance(opponent, dict) or opponent.get('tag') is None:  # type: ignore[misc]
            logging.warning(f"[PROCESS-WAR-HISTORY] No valid opponent data in {os.path.basename(json_file_path)} - skipping (likely corrupted or incomplete war data)")
            # Move to archive anyway to prevent repeated processing
            import QBcsvhandling as _qbc_shard
            _basename = os.path.basename(json_file_path)
            archive_dir = _qbc_shard.get_war_shard_dir(_basename.split("_")[0], CONFIG.archive_dir)
            os.makedirs(archive_dir, exist_ok=True)
            archive_file = os.path.join(archive_dir, _basename)
            if os.path.exists(json_file_path):
                # os.replace() is atomic — no SIGINT race between delete+rename
                os.replace(json_file_path, archive_file)
                if archive_set is not None:
                    archive_set.add(_basename)
                logging.info(f"[ARCHIVE] Moved incomplete war data {_basename} to archive")
            CACHE.record_cycle_stat("pwh_incomplete_archived")
            return True  # Return True to prevent retry loops
        
        opponent_tag = opponent.get('tag', 'UNK').lstrip('#')  # type: ignore[misc]
        
        # Build War ID from opponent tag and start time
        # start_time is stored as string like "<Timestamp time=datetime.datetime(2025, 12, 20, 18, 41, 20) seconds_until=-86957>"
        start_dt_compact = None
        start_dt_iso = None
        import re
        if isinstance(start_time, str):
            # Extract datetime values from string representation
            match = re.search(r'datetime\.datetime\((\d+),\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)', start_time)
            if match:
                y, mo, d, h, mi = map(int, match.groups())
                start_dt_compact = f"{y:04d}{mo:02d}{d:02d}{h:02d}{mi:02d}"
                start_dt_iso = f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}"
        elif isinstance(start_time, dict):
            # Fallback: if it's a dict with year/month/day/hour/minute keys
            y, mo, d, h, mi = (start_time.get('year'), start_time.get('month'),  # type: ignore[misc]
                               start_time.get('day'), start_time.get('hour'), start_time.get('minute'))  # type: ignore[misc]
            if all(v is not None for v in [y, mo, d, h, mi]):  # type: ignore[misc]
                start_dt_compact = f"{y:04d}{mo:02d}{d:02d}{h:02d}{mi:02d}"
                start_dt_iso = f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}"
        
        if not start_dt_compact:
            logging.error(f"[PROCESS-WAR-HISTORY] Could not extract start time from {os.path.basename(json_file_path)}")
            return False
        
        war_id = f"{opponent_tag}_{start_dt_compact}"
        
        # Check if this war is already in history
        already_in_history = _is_war_in_history(clan_tag, war_id)
        
        if already_in_history:
            # War exists but no archive (shouldn't happen, but handle gracefully)
            # Check for late attacks
            logging.debug(f"[LATE-CHECK] War {war_id} already in history - checking for late attacks")
            
            # Parse war stats from JSON
            # Reconstruct coc.War-like object for _parse_war_stats_from_api
            import coc  # type: ignore[import-untyped]
            class FakeWar:  # type: ignore[misc]
                def __init__(self, war_dict):  # type: ignore[no-untyped-def]
                    self.state = war_dict.get('state', 'unknown')  # type: ignore[misc]
                    self.attacks_per_member = war_dict.get('attacks_per_member', 2)  # type: ignore[misc]
                    self.clan = self._parse_clan(war_dict.get('clan', {}))  # type: ignore[misc]
                    self.opponent = self._parse_clan(war_dict.get('opponent', {}))  # type: ignore[misc]
                
                def _parse_clan(self, clan_dict):  # type: ignore[no-untyped-def]
                    clan_obj = type('obj', (object,), {})()
                    clan_obj.tag = clan_dict.get('tag', '')  # type: ignore
                    clan_obj.name = clan_dict.get('name', '')  # type: ignore
                    clan_obj.members = []  # type: ignore
                    for m in clan_dict.get('members', []):  # type: ignore[misc]
                        member = type('obj', (object,), {})()
                        member.tag = m.get('tag', '')  # type: ignore[misc]
                        member.name = m.get('name', '')  # type: ignore[misc]
                        # JSON uses 'townhall' (no underscore), not 'town_hall'
                        member.town_hall = m.get('townhall', 0)  # type: ignore[misc]
                        member.map_position = m.get('map_position', 0)  # type: ignore[misc]
                        member.attacks = []  # type: ignore
                        for a in m.get('attacks', []):  # type: ignore[misc]
                            attack = type('obj', (object,), {})()
                            attack.stars = a.get('stars', 0)  # type: ignore[misc]
                            attack.destruction = a.get('destruction', 0.0)  # type: ignore[misc]
                            attack.order = a.get('order', 0)  # type: ignore[misc]
                            attack.defender_tag = a.get('defender_tag', '')  # type: ignore[misc]
                            member.attacks.append(attack)  # type: ignore
                        
                        # Add best_opponent_attack for defensive stars
                        # JSON uses 'bestOpponentAttack' (camelCase), not 'best_opponent_attack'
                        best_opp_atk = m.get('bestOpponentAttack')  # type: ignore[misc]
                        if best_opp_atk and isinstance(best_opp_atk, dict):
                            best_attack = type('obj', (object,), {})()
                            best_attack.stars = best_opp_atk.get('stars', 0)  # type: ignore
                            best_attack.destruction = best_opp_atk.get('destruction', 0.0)  # type: ignore
                            best_attack.order = best_opp_atk.get('order', 0)  # type: ignore
                            best_attack.attacker_tag = best_opp_atk.get('attacker_tag', '')  # type: ignore
                            member.best_opponent_attack = best_attack  # type: ignore
                        else:
                            member.best_opponent_attack = None  # type: ignore
                        
                        clan_obj.members.append(member)  # type: ignore
                    return clan_obj
            
            fake_war = FakeWar(war_obj)
            if not start_dt_iso:  # Type guard
                logging.error(f"Missing start_dt_iso for war {war_id}")
                return False  # Return False instead of None
            war_data = _parse_war_stats_from_api(fake_war, war_id, start_dt_iso)
            
            # Update history with late attacks
            import QBcsvhandling
            history_changed = QBcsvhandling._update_history_with_late_attacks(clan_tag, war_id, war_data)  # type: ignore[misc]
            
            # Also update per-attack + summary tables from raw JSON
            try:
                _war_state_lc = str(war_obj.get('state', '') or '').lower()  # type: ignore[misc]
                _incomplete_in_war = _war_state_lc in {'in_war', 'inwar'}
                atk_rows = QBcsvhandling.build_per_attack_rows(
                    war_obj,
                    clan_tag,
                    for_finalization=not _incomplete_in_war,
                    include_missed_sentinels=not _incomplete_in_war,
                )
                summary = QBcsvhandling.build_war_summary(war_obj, clan_tag)
                # Layer 2 sync lookup: inject round_number if already known
                if summary and summary.get("is_cwl") and summary.get("war_tag"):
                    try:
                        _rn = CACHE.db_manager.get_cwl_round_for_war_tag_sync(summary["war_tag"])  # type: ignore[union-attr]
                        if _rn is not None:
                            summary["round_number"] = _rn
                    except Exception as _rn_ex:
                        logging.debug(f"[LATE-CHECK] round_number lookup failed: {_rn_ex}")
                if atk_rows or summary:
                    CACHE.db_manager.update_war_data_sync(clan_tag, war_id, atk_rows or [], summary)  # type: ignore[union-attr]
            except Exception as _e:
                logging.warning(f"[LATE-CHECK] war_attacks/war_summary update failed for {war_id}: {_e}")
            
            if not history_changed:
                logging.debug(f"[LATE-CHECK] No late attacks detected for {war_id}")
                CACHE.record_cycle_stat("pwh_late_check_no_change")
            else:
                # Invalidate history cache after late attack update
                if clan_tag in CACHE.clan_history:
                    del CACHE.clan_history[clan_tag]
                    logging.debug(f"[CACHE-INVALIDATE] History cache cleared for {clan_tag} after late attack update")
                # BUGFIX (2026-02-05): Also clear filtered history cache
                # Remove all cached entries for this clan (any month/year combination)
                keys_to_delete = [k for k in CACHE.history_cache.keys() if k[0] == clan_tag]
                for key in keys_to_delete:
                    del CACHE.history_cache[key]
                if keys_to_delete:
                    logging.debug(f"[CACHE-INVALIDATE] Cleared {len(keys_to_delete)} filtered history cache entries for {clan_tag}")
                CACHE.record_cycle_stat("pwh_late_attacks_updated_DB")
            
            # Archive handling:
            # - Archive doesn't exist → Move temp to archive
            # - Archive exists + identical → Delete temp (duplicate)
            # - Archive exists + differs → Replace archive (late attacks)
            return _archive_and_log_war_file(json_file_path, history_changed, archive_set=archive_set)
        
        # War not in history yet - finalize it
        logging.info(f"[FINALIZE] Processing new war {war_id} for {clan_tag}")
        CACHE.record_cycle_stat("pwh_new_war_finalized")
        
        # Determine war type from JSON if not provided
        if war_type_str is None:
            war_type_str = war_obj.get('type', 'unknown')
        
        # Friendly wars: archive the temp file but skip history DB write entirely
        if war_type_str == 'friendly':
            logging.info(f"[FINALIZE] Friendly war {war_id} for {clan_tag} — archiving without history append")
            CACHE.record_cycle_stat("pwh_friendly_war_archived")
            return _archive_and_log_war_file(json_file_path, history_changed=False, archive_set=archive_set)
        
        # Use existing _append_current_war_to_history logic (handles database append + JSON archive)
        import QBcsvhandling
        QBcsvhandling._append_current_war_to_history(clan_tag, json_file_path, archive_set=archive_set, war_obj=war_obj)  # type: ignore[misc]
        
        # Invalidate history cache after finalization
        if clan_tag in CACHE.clan_history:
            del CACHE.clan_history[clan_tag]
        # BUGFIX (2026-02-05): Also clear filtered history cache
        # Remove all cached entries for this clan (any month/year combination)
        keys_to_delete = [k for k in CACHE.history_cache.keys() if k[0] == clan_tag]
        for key in keys_to_delete:
            del CACHE.history_cache[key]
        if keys_to_delete:
            logging.debug(f"[CACHE-INVALIDATE] Cleared {len(keys_to_delete)} filtered history cache entries for {clan_tag}")
        
        # Note: _append_current_war_to_history already handles archiving:
        # - Moves JSON to archive/ if not exists
        # - Deletes temp if archive exists and content matches
        # - Replaces archive if content differs (late attacks)
        # File is already archived, so just return success
        return True
        
    except Exception as ex:
        raise WarProcessingError(
            f"Error processing war history for {os.path.basename(json_file_path)}: {ex}",
            context={"file": os.path.basename(json_file_path), "clan_tag": clan_tag}
        )


def manage_war_files(clan_tag: str, current_opponent_tag: Optional[str] = None, war_files_prescan: Optional[List[str]] = None, archive_set: Optional[Set[str]] = None) -> None:
    """
    Unified war file lifecycle management - single source of truth for war finalization and cleanup.
    
    Replaces split cleanup/finalization logic to prevent race conditions and data loss.
    For each war file found for this clan, determines appropriate action based on state:
    
    Decision Logic:
        1. Is it the CURRENT war? (newest file + same opponent) → SKIP (don't touch active war)
        2. Is war_id already in history CSV? → Check for late attacks → ARCHIVE
        3. Is JSON parseable and complete? → FINALIZE → APPEND TO HISTORY → ARCHIVE
        4. Is JSON corrupted/incomplete? → LOG ERROR → ARCHIVE (prevent retry loops)
    
    This function is IDEMPOTENT and ATOMIC:
    - Safe to call multiple times (checks history before appending)
    - No race conditions (single function handles entire lifecycle)
    - Graceful error handling (corrupted files archived, not reprocessed)
    
    Args:
        clan_tag: Clan tag (e.g., '#2C9UR9GJY')
        current_opponent_tag: Current war opponent tag (None if not in war)
                            Used to identify which file is the active war
        war_files_prescan: Optional pre-scanned list of war file paths for this clan.
                          If provided, skip the glob.glob() call and use these files.
                          Callers can pre-scan the temp dir once and pass matching files.
    
    Behavior:
        - Scans data/temp/ for all war files matching clan_tag
        - Sorts by modification time to identify newest (current) war
        - Processes each file based on its state and history status
        - Moves processed files to archive/
        - Invalidates history cache after changes
    
    Edge Cases Handled:
        - Multiple rapid wars (CWL scenario)
        - Orphaned wars (new war started before old finalized)
        - Wars already in history (duplicate prevention)
        - Corrupted JSON files (archived without retry)
        - Missing opponent data (incomplete wars archived)
        - Late attacks (history updated, file re-archived)
    
    Example:
        # After saving new war object (no await - synchronous)
        manage_war_files("#2C9UR9GJY", "2Y2RPP90R")
        # Finalizes old wars, archives processed files, keeps current war intact
    """
    import glob
    
    safe_clan_tag = clan_tag.replace('#', '').upper()
    current_opponent_clean = current_opponent_tag.lstrip('#') if current_opponent_tag else None
    temp_dir = os.path.join(CONFIG.data_dir, "temp")
    
    # Use pre-scanned files if provided, otherwise glob for this clan's files
    if war_files_prescan is not None:
        war_files = list(war_files_prescan)  # copy to avoid mutating caller's list
    else:
        import QBcsvhandling as _qbc_shard
        pattern = os.path.join(_qbc_shard.get_war_shard_dir(safe_clan_tag, temp_dir), f"{safe_clan_tag}_*_war_data.json")
        war_files = glob.glob(pattern)
    if not war_files:
        logging.debug(f"[MANAGE-WAR-FILES] No war files found for {clan_tag}")
        return

    # DB connections are now cached persistently in thread-local storage
    # by _get_sync_conn() — no per-clan sync_batch() needed.
    _manage_war_files_inner(clan_tag, current_opponent_clean, war_files, archive_set)


def _manage_war_files_inner(clan_tag: str, current_opponent_clean: Optional[str], war_files: List[str], archive_set: Optional[Set[str]]) -> None:
    """Inner implementation of manage_war_files, called inside sync_batch context."""
    import json

    def _parse_ts_repr(raw: str) -> Optional[datetime]:
        """Parse coc.py Timestamp repr with 5- or 6-field datetime payload."""
        if not raw:
            return None
        m = re.search(
            r'datetime\.datetime\((\d+),\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)(?:,\s*(\d+))?',
            raw,
        )
        if not m:
            return None
        try:
            y, mo, d, h, mi = [int(x) for x in m.groups()[:5]]
            sec_raw = m.group(6)
            sec = int(sec_raw) if sec_raw is not None else 0
            return datetime(y, mo, d, h, mi, sec, tzinfo=_tz.utc)
        except Exception:
            return None

    def _is_stale_random_preparation(war_obj: Dict[str, Any]) -> bool:
        """True for stale preparation snapshot: (random or friendly) + preparation + end_time in past."""
        _state = str(war_obj.get('state', '') or '').lower()
        _type = str(war_obj.get('type', '') or '').lower()
        if _type not in ('random', 'friendly') or _state != 'preparation':
            return False
        _end_dt = _parse_ts_repr(str(war_obj.get('end_time', '') or ''))
        if _end_dt is None:
            return False
        return datetime.now(_tz.utc) >= _end_dt
    
    # Filter out files that no longer exist on disk.  A pre-scanned list can become
    # stale mid-cycle if a temp file was deleted or moved by another concurrent operation.
    # Silently drop missing paths here so the getmtime sort and every downstream
    # open() always operate on real files.
    war_files = [f for f in war_files if os.path.exists(f)]
    if not war_files:
        logging.debug(f"[MANAGE-WAR-FILES] All pre-scanned files for {clan_tag} were already removed")
        return

    # Sort by modification time (oldest first, newest last)
    war_files.sort(key=lambda x: os.path.getmtime(x))
    
    # Identify current war file (newest + same opponent + active state)
    # CRITICAL: Must check state to avoid treating ended wars as "current"
    # Edge case: Clan finished war weeks ago, no new war started
    # - API still returns old war (state="war_ended")
    # - opponent_tag matches, but war should be finalized, not skipped!
    current_war_file = None
    if current_opponent_clean and len(war_files) > 0:
        # Check if newest file matches current opponent
        newest_file = war_files[-1]
        filename = os.path.basename(newest_file)
        parts = filename.replace('_war_data.json', '').split('_', 2)
        if len(parts) >= 2:
            file_opponent = parts[1]
            if file_opponent == current_opponent_clean:
                # Fast-path: check cached metadata instead of reading JSON from disk.
                # save_war_object() keeps temp_war_metadata current for the latest
                # war file.  Falls back to JSON read only when cache is missing.
                _md = CACHE.temp_war_metadata.get(clan_tag)
                _cached_state = _md.get("state") if _md else None
                if _cached_state is not None:
                    # Check stale-preparation via cached metadata fields
                    _is_stale = False
                    _md_type = str(_md.get("type", "") or "").lower() if _md else ""
                    if _md_type in ("random", "friendly") and _cached_state == "preparation":
                        _end_dt = _parse_ts_repr(str(_md.get("end_time", "") or "")) if _md else None
                        if _end_dt is not None and datetime.now(_tz.utc) >= _end_dt:
                            _is_stale = True
                            logging.info(
                                f"[MANAGE-WAR-FILES] Newest file {filename} is stale preparation "
                                f"(type={_md_type}, end_time in past) - will remove"
                            )
                    if _is_stale:
                        pass  # Not current — will be processed in the per-file loop
                    elif _cached_state in ('preparation', 'in_war', 'inwar'):
                        current_war_file = newest_file
                        logging.debug(f"[MANAGE-WAR-FILES] Identified current active war: {filename} (state={_cached_state}, from cache)")
                    else:
                        logging.debug(f"[MANAGE-WAR-FILES] Newest file {filename} matches opponent but state={_cached_state} - will finalize")
                else:
                    # Fallback: read JSON (no metadata cached for this clan)
                    try:
                        with open(newest_file, 'r', encoding='utf-8') as f:
                            war_obj = json.load(f)
                        state = war_obj.get('state', 'unknown')
                        if _is_stale_random_preparation(war_obj):
                            logging.info(
                                f"[MANAGE-WAR-FILES] Newest file {filename} is stale preparation "
                                f"(end_time in past) - will remove"
                            )
                            state = 'stale_preparation'
                        if state in ['preparation', 'in_war', 'inWar']:
                            current_war_file = newest_file
                            logging.debug(f"[MANAGE-WAR-FILES] Identified current active war: {filename} (state={state})")
                        else:
                            logging.debug(f"[MANAGE-WAR-FILES] Newest file {filename} matches opponent but state={state} - will finalize")
                    except Exception as ex:
                        logging.warning(f"[MANAGE-WAR-FILES] Could not verify state for {filename}: {ex} - treating as old war")
    
    # Process each war file
    processed_count = 0
    for war_file in war_files:
        filename = os.path.basename(war_file)
        
        # Skip current war file
        if war_file == current_war_file:
            logging.debug(f"[MANAGE-WAR-FILES] Skipping current war: {filename}")
            continue
        
        # Inspect non-current file candidate; most will be skipped by state checks below.
        logging.debug(f"[MANAGE-WAR-FILES] Inspecting non-current war file: {filename}")
        
        try:
            # Load war object to check state and type
            try:
                with open(war_file, 'r', encoding='utf-8') as f:
                    war_obj = json.load(f)
            except FileNotFoundError:
                # File was removed between the exists() check above and this open().
                # Narrow race; treat as already handled and move on silently.
                logging.debug(f"[MANAGE-WAR-FILES] File removed mid-cycle (race): {filename}")
                continue
            except Exception as ex:
                logging.error(f"[MANAGE-WAR-FILES] Error reading {filename}: {ex} - archiving")
                import traceback
                logging.debug(traceback.format_exc())
                _move_to_archive(war_file, archive_set=archive_set)
                continue
            
            state = war_obj.get('state', 'unknown')
            war_type = war_obj.get('type', 'unknown')
            is_cwl = (war_type == 'cwl')

            # Edge-case cleanup: stale preparation snapshots (random or friendly type).
            # Exact conditions: state=preparation, end_time in the past.
            if _is_stale_random_preparation(war_obj):
                try:
                    os.remove(war_file)
                    logging.info(
                        f"[MANAGE-WAR-FILES] Removed stale preparation temp file: {filename}"
                    )
                    CACHE.record_cycle_stat("mwf_stale_prep_removed")
                except FileNotFoundError:
                    pass
                except Exception as _rm_ex:
                    logging.warning(
                        f"[MANAGE-WAR-FILES] Could not remove stale temp file {filename}: {_rm_ex}"
                    )
                continue
            
            # Determine if this is an orphaned war (different opponent than current)
            # Extract opponent from filename
            file_parts = filename.replace('_war_data.json', '').split('_', 2)
            file_opponent = file_parts[1] if len(file_parts) >= 2 else None
            is_orphaned = (file_opponent != current_opponent_clean)
            
            # === FINALIZATION DECISION LOGIC ===
            # CWL wars with incomplete state: Skip and let process_orphaned_cwl_wars() fetch final data
            if is_cwl and state != 'war_ended':
                logging.debug(f"[CWL-ORPHAN-SKIP] Skipping finalization of {filename} - state={state}, will be fetched via get_league_war() in process_orphaned_cwl_wars()")
                CACHE.record_cycle_stat("mwf_cwl_orphan_skip")
                continue
            
            # Regular wars - Two cases:
            # 1. War ended normally (state='war_ended') → Finalize
            # 2. Orphaned regular war (different opponent, state='in_war') → Finalize
            #    Edge case: War ended and new war started within 300s cycle
            #    Old war has state='in_war' but can't be refetched (no war_tag)
            #    Must finalize with available data
            if state == 'war_ended':
                logging.debug(f"[FINALIZE-OLD-WAR] Processing {filename} - state={state}, is_cwl={is_cwl}")
                CACHE.record_cycle_stat("mwf_finalize_regular_war_ended")
                success = _process_war_history(clan_tag, war_file, war_type, archive_set=archive_set)
                if success:
                    processed_count += 1
                else:
                    logging.error(f"[MANAGE-WAR-FILES] Failed to finalize {filename} - archiving")
                    _move_to_archive(war_file, archive_set=archive_set)
                # Evict temp_war_metadata — the file has been processed (archived
                # or deleted).  _archive_and_log_war_file() handles the
                # "already in history" sub-path, but the "new war" sub-path goes
                # through _append_current_war_to_history() which skips
                # _archive_and_log_war_file() entirely, leaving the war_ended
                # metadata entry alive and inflating /status counts.
                # Guard: only evict when metadata matches this specific file so we
                # don't clobber an in_war entry for a new war that started after
                # this one ended.
                _meta_mwf = CACHE.temp_war_metadata.get(clan_tag)
                if _meta_mwf and (
                    _meta_mwf.get("state") == "war_ended"
                    or _meta_mwf.get("filepath") == war_file
                ):
                    CACHE.temp_war_metadata.pop(clan_tag, None)
                    CACHE.in_war_clan_tags.discard(clan_tag)
                    logging.debug(f"[METADATA-CLEANUP] Evicted temp_war_metadata for {clan_tag} (war_ended finalized)")
            elif not is_cwl and is_orphaned and state in ['in_war', 'inWar']:
                # EDGE CASE: Orphaned regular war in active state.
                # When current_opponent_clean is set, the clan is in a DIFFERENT
                # war now, so the old war must have ended — finalize immediately.
                # When current_opponent_clean is None (Phase-3B / passive-clan
                # path), is_orphaned is trivially True for every file because
                # no fresh API data was fetched.  We must NOT finalize while the
                # war is still running — Phase-1's 22h re-poll should fetch the
                # war_ended state first.  Guard: only finalize when end_time is
                # in the past, ensuring at least one API call had the opportunity
                # to capture the final result.
                # Note: CWL wars in in_war state are never reached here — the
                # earlier CWL guard (is_cwl and state != 'war_ended') fires first.
                if current_opponent_clean is None:
                    _end_dt = _parse_ts_repr(str(war_obj.get('end_time', '') or ''))
                    if _end_dt is None:
                        # Fallback: derive from start_time + 25h (wars last ~24h)
                        _start_dt = _parse_ts_repr(str(war_obj.get('start_time', '') or ''))
                        if _start_dt is not None:
                            _end_dt = _start_dt + timedelta(hours=25)
                    if _end_dt is None or _end_dt > datetime.now(_tz.utc):
                        logging.debug(
                            f"[MANAGE-WAR-FILES] Skipping {filename} - state={state}, "
                            f"war may still be active (no current-opponent context)"
                        )
                        CACHE.record_cycle_stat("mwf_in_war_guard_skip")
                        continue
                logging.info(f"[FINALIZE-ORPHAN-REGULAR] Processing orphaned regular war {filename} - state={state} (can't refetch from API)")
                CACHE.record_cycle_stat("mwf_finalize_orphan")
                success = _process_war_history(clan_tag, war_file, war_type, archive_set=archive_set)
                if success:
                    processed_count += 1
                else:
                    logging.error(f"[MANAGE-WAR-FILES] Failed to finalize {filename} - archiving")
                    _move_to_archive(war_file, archive_set=archive_set)
            else:
                # War not ready for finalization (preparation state or non-orphaned active war)
                logging.debug(f"[MANAGE-WAR-FILES] Skipping {filename} - state={state}, is_orphaned={is_orphaned} (not ready for finalization)")
                CACHE.record_cycle_stat("mwf_skip_not_ready")
                continue
        except WarProcessingError as ex:
            logging.error(f"[MANAGE-WAR-FILES] Error processing {filename}: {ex} - archiving")
            import traceback
            logging.debug(traceback.format_exc())
            try:
                _move_to_archive(war_file, archive_set=archive_set)
            except WarProcessingError as archive_ex:
                logging.error(f"[MANAGE-WAR-FILES] {archive_ex}")
    
    if processed_count > 0:
        logging.info(f"[MANAGE-WAR-FILES] Finalized {processed_count} old war(s) for {clan_tag}")


def _archive_and_log_war_file(json_file_path: str, history_changed: bool = False, archive_set: Optional[Set[str]] = None) -> bool:
    """
    Move war file to archive and log result.
    
    Args:
        json_file_path: Path to temp war JSON file
        history_changed: Whether history was updated (affects log message)
        archive_set: Optional pre-built set of archive filenames for O(1) existence checks.
    
    Returns:
        True if file archived successfully, False if file doesn't exist
    """
    if not os.path.exists(json_file_path):
        logging.debug(f"[ARCHIVE] JSON file {os.path.basename(json_file_path)} already processed")
        return True
    
    import QBcsvhandling as _qbc_shard
    _basename = os.path.basename(json_file_path)
    archive_dir = _qbc_shard.get_war_shard_dir(_basename.split("_")[0], CONFIG.archive_dir)
    os.makedirs(archive_dir, exist_ok=True)
    archive_file = os.path.join(archive_dir, _basename)
    
    # When inside a war_write_batch(), defer the file move so it happens
    # AFTER the bulk DB write succeeds.  Outside batch mode this falls
    # back to an immediate os.replace().
    if CACHE.db_manager is not None:
        CACHE.db_manager.defer_file_move(json_file_path, archive_file)
    else:
        os.replace(json_file_path, archive_file)
    if archive_set is not None:
        archive_set.add(_basename)

    # Prune in-memory notification state for this war so it doesn't accumulate
    # indefinitely. The notification_state key is "{clan_tag_safe}_{opponent_tag}"
    # (set by _get_war_id in war_notifications.py).  We extract just the first two
    # underscore-delimited segments from the filename stem to match that format,
    # ignoring the trailing timestamp added by the new naming convention.
    _stem = os.path.basename(json_file_path).replace('_war_data.json', '')
    _stem_parts = _stem.split('_', 2)
    war_id = '_'.join(_stem_parts[:2])  # "CLANTAG_OPPTAG" (notification state key)
    if war_id in CACHE.notification_state:
        del CACHE.notification_state[war_id]
        logging.debug(f"[NOTIFICATION-STATE-CLEANUP] Pruned in-memory notification state for {war_id}")
    # Also clean the DB rows so they don't reload on next restart
    if CACHE.db_manager:
        try:
            CACHE.db_manager.delete_notification_state_sync(war_id)
        except Exception as _ns_err:
            logging.debug(f"[NOTIFICATION-STATE-CLEANUP] DB cleanup skipped for {war_id}: {_ns_err}")

    # Evict temp_war_metadata so /status no longer counts this as war_ended.
    # Without this, the entry lingers until the next restart (the file is gone
    # but the in-memory dict still shows state=war_ended, inflating the count).
    # Two conditions (OR) to cover both cases:
    #   1. filepath match: metadata points to this exact file (Phase-1 startup
    #      sets state=in_war; filepath matches the file being archived).
    #   2. state == "war_ended": save_war_object already confirmed the state via
    #      API poll; the file was then queued for finalization by Phase-3B.
    # We do NOT evict when state=in_war and filepath differs — that means a new
    # war has already started for this clan and its in_war entry must be kept.
    _clan_safe = _stem_parts[0]
    _clan_tag_full = f"#{_clan_safe}"
    _meta = CACHE.temp_war_metadata.get(_clan_tag_full)
    if _meta and (
        _meta.get("state") == "war_ended"
        or _meta.get("filepath") == json_file_path
    ):
        CACHE.temp_war_metadata.pop(_clan_tag_full, None)
        CACHE.in_war_clan_tags.discard(_clan_tag_full)
        logging.debug(f"[METADATA-CLEANUP] Evicted temp_war_metadata for {_clan_tag_full} (war archived)")

    if history_changed:
        logging.info(f"[ARCHIVE] Moved {os.path.basename(json_file_path)} to archive (history updated)")
    else:
        logging.info(f"[ARCHIVE-REFRESH] Refreshed {os.path.basename(json_file_path)} in archive (no history changes)")
    
    return True


def _move_to_archive(war_file: str, archive_set: Optional[Set[str]] = None) -> None:
    """Move war file to archive directory. Helper for manage_war_files()."""
    try:
        import QBcsvhandling as _qbc_shard
        _basename = os.path.basename(war_file)
        archive_dir = _qbc_shard.get_war_shard_dir(_basename.split("_")[0], CONFIG.archive_dir)
        os.makedirs(archive_dir, exist_ok=True)
        archive_file = os.path.join(archive_dir, _basename)
        
        if os.path.exists(war_file):
            # os.replace() is atomic — no SIGINT race between delete+rename
            os.replace(war_file, archive_file)
            if archive_set is not None:
                archive_set.add(_basename)
            logging.info(f"[ARCHIVE] Moved {_basename} to archive")
    except Exception as ex:
        raise WarProcessingError(
            f"Failed to move {os.path.basename(war_file)} to archive: {ex}",
            context={"file": os.path.basename(war_file), "archive_dir": archive_dir}  # type: ignore[possibly-unbound]
        )


# CWL leagues for which we harvest all participating clans and track them over time.
# Top 10 from the clashspot.net distribution table (Legend → Master III).
_CWL_HARVEST_LEAGUES: frozenset[str] = WAR_UPDATE_LEAGUES


_CWL_LEAGUE_DEFAULT: str = "Master League I"

async def _ensure_clan_war_league(clan_tag: str, opponent_tag: Optional[str] = None) -> str:
    """
    Resolve and persist the CWL war league for *clan_tag*, trying each source in order:

    1. ``clan_name_cache`` entry (already persisted to DB — fastest path).
    2. Live CoC API: ``CACHE.coc_clan_cache.get_clan(clan_tag)`` populates
       ``war_league`` in ``clan_name_cache`` via ``_update_clan_metadata``.
    3. Opponent's league (``opponent_tag``): used when the CoC API does not return
       a ``warLeague`` for the tracked clan.  Both clans in a war generally share
       the same league tier, so this is a reliable approximation.
    4. Hard default: ``"Master League I"`` — should be essentially unreachable in
       practice (requires BOTH clans to have no warLeague in the API, which almost
       never happens).

    Always returns a non-empty league name.  No exception is raised — failures are
    logged at DEBUG/WARNING level.
    """
    def _from_cache(tag: str) -> Optional[str]:
        entry = CACHE.clan_name_cache.get(tag)
        return (entry.get("war_league") or None) if isinstance(entry, dict) else None

    # 1. Already in clan_name_cache?
    league = _from_cache(clan_tag)
    if league:
        return league

    # 2. Fetch from API (updates clan_name_cache via _update_clan_metadata)
    try:
        await CACHE.coc_clan_cache.get_clan(clan_tag)
        league = _from_cache(clan_tag)
    except Exception as _ex:
        logging.debug(f"[WAR-LEAGUE] API fetch failed for {clan_tag}: {_ex}")

    if league:
        return league

    # 3. Opponent's league (both clans in a war share the same CWL tier)
    if opponent_tag:
        league = _from_cache(opponent_tag)
        if not league:
            try:
                await CACHE.coc_clan_cache.get_clan(opponent_tag)
                league = _from_cache(opponent_tag)
            except Exception as _ex:
                logging.debug(f"[WAR-LEAGUE] API fetch failed for opponent {opponent_tag}: {_ex}")
        if league:
            logging.debug(
                f"[WAR-LEAGUE] Using opponent {opponent_tag} league '{league}' as fallback for {clan_tag}"
            )
            return league

    # 4. Hard default — essentially unreachable (both clans returned no warLeague)
    logging.warning(
        f"[WAR-LEAGUE] No league found for {clan_tag} or opponent {opponent_tag}; "
        f"defaulting to '{_CWL_LEAGUE_DEFAULT}'"
    )
    return _CWL_LEAGUE_DEFAULT


async def _upsert_enemy_clan_on_war_start(
    tracked_clan_tag: str,
    enemy_tag: str,
    coc_war_obj: Any,
    is_cwl: bool,
) -> None:
    """
    Called once per war when the enemy clan is first seen (not yet in clan_name_cache).

    Actions:
    - Fetches the enemy clan via the CoC API to capture war_league and name.
    - Adds the enemy to clan_name_cache and persists to DB.
      - Tracking is league-gated (one-way ratchet): M3+ enemies → actively tracked (track_war_updates=True)
        (22h polling pool); below M3 or unknown league → passively tracked (track_war_updates=False).
      - This applies equally to CWL and regular wars.
    - For CWL wars where the tracked clan is in a top-league, also harvests all
      clans from the league group (see _harvest_cwl_group_clans).
      Group harvest does NOT run for regular wars.

    Args:
        tracked_clan_tag: The clan tag we are tracking (our side of the war).
        enemy_tag:        The opposing clan's tag.
        coc_war_obj:      The coc.ClanWar object (used for CWL group harvest).
        is_cwl:           True when this is a Clan War League war.
    """
    try:
        track_updates: bool = is_cwl  # initial guess; refined below once league is known
        enemy_name = "Unknown"
        enemy_war_league: Optional[str] = None

        try:
            enemy_clan_obj = await CACHE.coc_clan_cache.get_clan(enemy_tag)
            enemy_name = getattr(enemy_clan_obj, 'name', 'Unknown') or 'Unknown'
            _wl_obj = getattr(enemy_clan_obj, 'war_league', None)
            _wl_str = str(getattr(_wl_obj, 'name', '') or '') if _wl_obj else ''
            enemy_war_league = _wl_str if _wl_str else None
        except Exception as fetch_ex:
            logging.warning(f"[WAR-DISCOVERY] Could not fetch enemy clan {enemy_tag}: {fetch_ex}")

        # track_war_updates is league-gated (one-way ratchet: see CLAN_WAR_TRACKING.md).
        # Both CWL and regular-war enemies qualify if they are in M3+.
        # The CWL GROUP HARVEST (scanning all group members) is a separate concern
        # and still runs only for CWL wars of subscribed top-league clans (below).
        track_updates = bool(enemy_war_league and enemy_war_league in _CWL_HARVEST_LEAGUES)

        now_iso = datetime.now(_tz.utc).isoformat()
        CACHE.clan_name_cache[enemy_tag] = {
            "name": enemy_name,
            "has_active_subscriptions": False,
            "last_war_update": now_iso,  # 22h clock starts now
            "warlog_is_public": True,    # assume public until first private-warlog error
            "last_checked_via_api": now_iso,
            "war_league": enemy_war_league,
            "track_war_updates": track_updates,
        }
        await CACHE.persist_clan(enemy_tag)
        logging.info(
            f"[WAR-DISCOVERY] Upserted enemy {enemy_tag} ({enemy_name}) | "
            f"CWL={is_cwl} | track={track_updates} | league={enemy_war_league}"
        )

        # For CWL wars of subscribed top-5-league clans, harvest the full group
        if is_cwl:
            await _harvest_cwl_group_clans(tracked_clan_tag, coc_war_obj)

    except Exception as e:
        logging.warning(f"[WAR-DISCOVERY] Unexpected error for enemy {enemy_tag}: {e}")


async def _harvest_cwl_group_clans(
    tracked_clan_tag: str,
    coc_war_obj: Any,
    *,
    league_group: Any = None,
) -> None:
    """
    Fetch all clans from a CWL league group and add new ones to the tracking pool.

    Only runs when:
    - The tracked clan has active subscriptions (fully subscribed).
    - The tracked clan's war_league is in _CWL_HARVEST_LEAGUES (top 5 leagues).

    For each clan in the group that is not yet in clan_name_cache:
    - Calls get_clan() to fetch war_league and name.
    - Adds to clan_name_cache with track_war_updates=True.
    - Persists to DB.

    Over time this builds up a comprehensive database of all clans competing at
    top-level CWL. The target scale is ~40 000 clans (Masters III – Champions I).

    Args:
        tracked_clan_tag: The subscribed clan we are tracking.
        coc_war_obj:      The coc.ClanWar object (may carry a league_group reference).
        league_group:     Optional pre-fetched ClanWarLeagueGroup; if provided the
                          internal get_league_group() call is skipped.
    """
    try:
        # Bail out silently if maintenance is active — CoC client and DB are closed.
        import QBcore as _qbcore
        if _qbcore.maintenance_mode:
            logging.debug(
                f"[CWL-HARVEST] Skipping {tracked_clan_tag} — maintenance active"
            )
            return
        tracked_data = CACHE.clan_name_cache.get(tracked_clan_tag)
        if not isinstance(tracked_data, dict):
            return

        # Only harvest for fully subscribed clans in the top-5 leagues
        if not tracked_data.get("has_active_subscriptions"):
            logging.debug(f"[CWL-HARVEST] Skipping {tracked_clan_tag} – no active subscriptions")
            return

        tracked_league = tracked_data.get("war_league")
        if tracked_league not in _CWL_HARVEST_LEAGUES:
            logging.debug(
                f"[CWL-HARVEST] Skipping {tracked_clan_tag} – "
                f"league '{tracked_league}' not in harvest set"
            )
            return

        # Use caller-provided league group if available; otherwise fetch from API.
        if league_group is None:
            try:
                league_group = await CACHE.get_league_group(tracked_clan_tag)
            except Exception as lg_ex:
                logging.warning(f"[CWL-HARVEST] Could not fetch league group for {tracked_clan_tag}: {lg_ex}")
                return

        if not league_group:
            return

        harvested = 0
        now_iso = datetime.now(_tz.utc).isoformat()

        group_clans = list(getattr(league_group, 'clans', []) or [])
        for cwl_clan in group_clans:
            group_tag = getattr(cwl_clan, 'tag', None)
            if not group_tag or group_tag == tracked_clan_tag:
                continue

            if group_tag in CACHE.clan_name_cache:
                # Clan is already known — check if it needs promotion.
                # We know this group is Master III+ (guaranteed by the earlier
                # tracked_league guard), so every clan here is playing Master
                # III+ CWL and should have track_war_updates = True.
                existing = CACHE.clan_name_cache[group_tag]
                if (
                    not existing.get("has_active_subscriptions")
                    and not existing.get("track_war_updates")
                ):
                    existing["track_war_updates"] = True
                    # Also fix stale war_league if it doesn't reflect Master III+.
                    # All 8 clans in a CWL group play in the same league tier,
                    # so tracked_league is a correct value for this clan.
                    if not existing.get("war_league") or existing.get("war_league") not in _CWL_HARVEST_LEAGUES:
                        existing["war_league"] = tracked_league
                    await CACHE.persist_clan(group_tag)
                    logging.info(
                        f"[CWL-HARVEST] Promoted existing clan {group_tag} "
                        f"to track_war_updates=True "
                        f"(seen in {tracked_league} group of {tracked_clan_tag})"
                    )
                continue

            try:
                group_obj = await CACHE.coc_clan_cache.get_clan(group_tag)
                group_name = getattr(group_obj, 'name', 'Unknown') or 'Unknown'
                _wl_obj = getattr(group_obj, 'war_league', None)
                _wl_str = str(getattr(_wl_obj, 'name', '') or '') if _wl_obj else ''
                group_war_league: Optional[str] = _wl_str if _wl_str else None

                # All clans are added to the DB.  track_war_updates is
                # league-gated: M3+ → True (22h polling), M4- → False.
                _track = group_war_league in _CWL_HARVEST_LEAGUES

                CACHE.clan_name_cache[group_tag] = {
                    "name": group_name,
                    "has_active_subscriptions": False,
                    "last_war_update": now_iso,
                    "warlog_is_public": True,
                    "last_checked_via_api": now_iso,
                    "war_league": group_war_league,
                    "track_war_updates": _track,
                }
                await CACHE.persist_clan(group_tag)
                harvested += 1
                logging.info(
                    f"[CWL-HARVEST] Added {group_tag} ({group_name}) | "
                    f"league={group_war_league}"
                )
            except Exception as clan_ex:
                logging.warning(f"[CWL-HARVEST] Could not fetch {group_tag}: {clan_ex}")

        if harvested:
            logging.info(
                f"[CWL-HARVEST] Harvested {harvested} new clans from "
                f"{tracked_clan_tag}'s group (league: {tracked_league})"
            )

    except Exception as e:
        logging.warning(f"[CWL-HARVEST] Unexpected error for {tracked_clan_tag}: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# PASSIVE CLAN MONTHLY REFRESH — catches promotions the discovery graph
# never revisits (CLAN_WAR_TRACKING.md write-path 8)
# ──────────────────────────────────────────────────────────────────────────────

_PASSIVE_REFRESH_BATCH_SIZE: int = 1000
_PASSIVE_REFRESH_CONCURRENCY: int = 15


async def refresh_stale_passive_clans(candidates: List[Tuple[str, str]]) -> int:
    """
    Phase 1.6 — lightweight get_clan() ping for passively-tracked clans overdue
    for their monthly refresh, run every update cycle.

    Passively-tracked clans (track_war_updates=False) are excluded from the main
    update loop entirely (QapBot.py's clan categorization fast-rejects them) and
    only ever get refreshed as a side effect of their CWL group happening to be
    rediscovered this season (cache_manager._sync_group_track_war_updates, see
    CLAN_WAR_TRACKING.md write-path 7) — which itself requires at least one
    groupmate to already be reachable from the subscription-rooted discovery
    graph. A clan whose group is never rediscovered can be promoted to Master
    III+ in the real game and the bot never finds out, since promotion
    detection is itself gated by being polled in the first place.

    This does a cheap, clan-info-only get_clan() call — NOT the full
    fetch_clan_war_data() war-data pipeline (no temp files, no war processing;
    we are deliberately not tracking wars for these clans) — for the most
    overdue of the given candidates, capped at _PASSIVE_REFRESH_BATCH_SIZE per
    cycle to avoid a burst against the shared API budget. Each successful
    fetch flows through the existing CoCClanCache.get_clan() ->
    _update_clan_metadata() path, which already transparently refreshes
    war_league and promotes track_war_updates to True if the clan is now
    Master III+ — no special-casing needed here.

    Args:
        candidates: (clan_tag, sort_key) pairs already identified as overdue
            (last_checked_via_api older than PASSIVE_CLAN_REFRESH_INTERVAL_DAYS,
            or never checked — sort_key '' sorts first). Collected by
            QapBot.py's main clan-categorization loop as a side effect of its
            own already-mandatory full clan_name_cache scan, rather than this
            function scanning the cache a second time — see
            CLAN_WAR_TRACKING.md write-path 8.

    Returns the number of clans actually queried this cycle.
    """
    log_prefix = "[PASSIVE-REFRESH]"

    if not candidates:
        return 0

    candidates = sorted(candidates, key=lambda c: c[1])  # most-overdue (or never-checked) first
    batch = [tag for tag, _ in candidates[:_PASSIVE_REFRESH_BATCH_SIZE]]

    logging.info(
        f"{log_prefix} {len(candidates):,} passively-tracked clan(s) overdue "
        f"(>{PASSIVE_CLAN_REFRESH_INTERVAL_DAYS}d) — fetching {len(batch):,} this cycle"
    )

    _sem = asyncio.Semaphore(_PASSIVE_REFRESH_CONCURRENCY)
    _promoted = 0

    async def _refresh_one(tag: str) -> None:
        nonlocal _promoted
        async with _sem:
            try:
                was_tracked = bool(CACHE.clan_name_cache.get(tag, {}).get('track_war_updates'))  # type: ignore[union-attr]
                await CACHE.coc_clan_cache.get_clan(tag)
                now_tracked = bool(CACHE.clan_name_cache.get(tag, {}).get('track_war_updates'))  # type: ignore[union-attr]
                if now_tracked and not was_tracked:
                    _promoted += 1
            except coc.NotFound:
                await _mark_clan_deleted(tag)
            except Exception as e:
                logging.debug(f"{log_prefix} {tag}: fetch failed ({e})")

    _fetch_t0 = time.monotonic()
    await asyncio.gather(*[_refresh_one(t) for t in batch])
    _fetch_elapsed = time.monotonic() - _fetch_t0

    logging.info(
        f"{log_prefix} Fetch: {len(batch):,} check(s) in {_fetch_elapsed:.3f}s "
        f"({_fetch_elapsed / len(batch) * 1000:.1f}ms/clan avg at concurrency "
        f"{_PASSIVE_REFRESH_CONCURRENCY}), {_promoted} promotion(s) detected."
    )
    return len(batch)


async def _find_active_cwl_war_for_clan(
    clan_tag: str,
    label: str = "CWL-FALLBACK",
    max_age: float = 300,
) -> Optional[Tuple[Any, str]]:
    """
    Find the active CWL war for *clan_tag* using shortcut cache + league group.

    Optimises API usage by:
      1. Trying ``clan_active_cwl_war`` shortcut first (≤1 API call).
      2. Falling back to league group iteration with cached war data
         for participant matching (~0 API calls when cache is warm).

    Args:
        clan_tag: Normalized clan tag, e.g. ``'#L2J0C0PY'``.
        label:    Log prefix for traceability (e.g. ``'CWL-FALLBACK'``).
        max_age:  Maximum acceptable age (seconds) for the league group cache
                  entry.  Pass a longer TTL (e.g. 7200) for subscribed clans
                  in the notInWar discovery path to reduce API call frequency;
                  pass 300 (default) when fresh data matters (exception path).

    Returns:
        ``(coc_war_obj, war_tag)`` when an active war is found, otherwise
        ``None``. Active means the war state normalizes to ``preparation`` or
        ``inwar`` (state strings are lower-cased with ``_``/spaces stripped, so
        both the API ``inWar`` form and the synthesized ``in_war`` form match).
    """
    _MATCH_TTL: float = 60  # seconds — fresh enough for attack data, avoids double-fetch

    # ── 1) Shortcut: last-known war tag (≤1 API call) ────────────────
    _shortcut_tag = CACHE.clan_active_cwl_war.get(clan_tag)
    if _shortcut_tag:
        try:
            _cw = await CACHE.get_league_war(_shortcut_tag, max_age=_MATCH_TTL)
            if _cw:
                _tags = {
                    getattr(getattr(_cw, 'clan', None), 'tag', None),
                    getattr(getattr(_cw, 'opponent', None), 'tag', None),
                }
                _ws = str(getattr(_cw, 'state', '') or '').lower().replace('_', '').replace(' ', '')
                if clan_tag in _tags and _ws in ('preparation', 'inwar'):
                    logging.info(
                        f"[{label}] {clan_tag} - active CWL war "
                        f"{_shortcut_tag} (state={_ws}) via shortcut"
                    )
                    return (_cw, _shortcut_tag)
            # Shortcut is stale (war ended or doesn't match any more)
            CACHE.clan_active_cwl_war.pop(clan_tag, None)
        except Exception:
            CACHE.clan_active_cwl_war.pop(clan_tag, None)

    # ── 2) Full iteration: league group → rounds → war tags ──────────
    # Guard A — regular war in progress: regular wars and CWL are mutually
    # exclusive in CoC.  If temp_war_metadata shows a non-CWL war currently in
    # preparation or inWar state, a live CWL war cannot exist — skip the
    # league group API call entirely.
    _tmeta = CACHE.temp_war_metadata.get(clan_tag) if hasattr(CACHE, 'temp_war_metadata') else None
    if _tmeta and not _tmeta.get('is_cwl', False):
        _tmeta_state = str(_tmeta.get('state', '') or '').lower().replace('_', '').replace(' ', '')
        if _tmeta_state in ('preparation', 'inwar'):
            logging.debug(
                f"[{label}] {clan_tag} - regular war in progress ({_tmeta_state}), "
                f"skipping CWL league group fetch"
            )
            return None

    # Guard B — CWL season ended: if we know this CWL season has already ended
    # for this clan, there cannot be an active war → skip the API call entirely.
    # Uses the same cwl_ended flag that gates the CWL-GROUP-EXPAND cycle path.
    # Returns False conservatively when the clan has no DB row (first boot,
    # clan not yet seen this season) so we still fetch in those cases.
    # Wrapped in to_thread(): sync sqlite3 call, awaited directly from the Phase-1
    # hot path (called for every clan every cycle) — unwrapped, this blocks the
    # event loop for its I/O duration, most often during CWL season.
    if CACHE.db_manager is not None and await asyncio.to_thread(
        CACHE.db_manager.is_latest_cwl_season_ended_sync, clan_tag
    ):
        logging.debug(
            f"[{label}] {clan_tag} - latest CWL season ended (cwl_ended=1), "
            f"skipping league group API fetch"
        )
        return None

    try:
        _lg = await CACHE.get_league_group(clan_tag, max_age=max_age)
        if not _lg:
            return None

        for _round in reversed(list(getattr(_lg, 'rounds', []) or [])):
            _live_tags: List[str] = [
                str(wt) for wt in (list(_round) if _round else [])  # type: ignore[arg-type]
                if wt and str(wt) not in ('#0', '0')  # type: ignore[arg-type]
            ]
            for _wt in _live_tags:
                try:
                    # Long TTL for non-matching wars (participants are immutable)
                    _cw = await CACHE.get_league_war(_wt, max_age=600)
                    if not _cw:
                        continue
                    _tags = {
                        getattr(getattr(_cw, 'clan', None), 'tag', None),
                        getattr(getattr(_cw, 'opponent', None), 'tag', None),
                    }
                    if clan_tag not in _tags:
                        continue
                    _ws = str(getattr(_cw, 'state', '') or '').lower().replace('_', '').replace(' ', '')
                    if _ws not in ('preparation', 'inwar'):
                        continue

                    # ✓ Active war found — store shortcut for next cycle
                    CACHE.clan_active_cwl_war[clan_tag] = _wt

                    # Re-fetch with short TTL for fresh attack data
                    _fresh = await CACHE.get_league_war(_wt, max_age=_MATCH_TTL)
                    _result = _fresh if _fresh else _cw
                    logging.info(
                        f"[{label}] {clan_tag} - active CWL war "
                        f"{_wt} (state={_ws}) via league group"
                    )
                    return (_result, _wt)
                except Exception:
                    continue
    except Exception as _cex:
        logging.debug(f"[{label}] {clan_tag} - league group fetch failed: {_cex}")

    return None


async def backfill_last_cwl_for_clan(clan_tag: str) -> str:
    """
    Backfill the last CWL season's war rounds for a clan into the database.

    Fetches all 7 CWL war rounds from the last available league group for the
    given clan via war_tag, writes each round to a temp JSON, and finalises it
    through the normal _process_war_history() pipeline so that war_summary and
    war_attacks tables are populated.

    Design:
    - **Idempotent**: checks war_summary before doing any work; returns early if
      the season is already present in the DB.
    - **Fire-and-forget safe**: can be called via asyncio.ensure_future(); all
      exceptions are caught and logged; never raises.
    - Only saves rounds where our clan is a participant.

    Args:
        clan_tag: Normalized clan tag, e.g. '#2C9UR9GJY'.

    Returns:
        A human-readable summary string (suitable for Discord followup).
    """
    log_prefix = f"[CWL-BACKFILL] {clan_tag}"
    try:
        import QBcore as _qbcore
        if _qbcore.maintenance_mode:
            msg = f"{log_prefix} Skipped — maintenance active"
            logging.debug(msg)
            return msg

        # ── 1. Fetch the last available CWL league group ──────────────────────
        try:
            league_group = await CACHE.get_league_group(clan_tag)
        except Exception as lg_ex:
            msg = f"Could not fetch league group for {clan_tag}: {lg_ex}"
            logging.warning(f"{log_prefix} {msg}")
            return f"❌ {msg}"

        if not league_group:
            msg = f"No CWL league group found for {clan_tag}."
            logging.info(f"{log_prefix} {msg}")
            return f"⚠️ {msg}"

        season: str = str(getattr(league_group, 'season', '') or '')
        if not season:
            msg = f"League group returned no season string for {clan_tag}."
            logging.warning(f"{log_prefix} {msg}")
            return f"⚠️ {msg}"

        # ── 2. Idempotency: skip if season already in DB ───────────────────────
        if CACHE.db_manager is not None:
            already_present = CACHE.db_manager.has_cwl_season_data_sync(clan_tag, season)
            if already_present:
                msg = f"CWL {season} for {clan_tag} already in DB — skipping backfill."
                logging.info(f"{log_prefix} {msg}")
                return f"ℹ️ {msg}"

        # ── 3. Collect all war tags across all rounds ──────────────────────────
        assigned_rounds: List[List[str]] = list(getattr(league_group, 'rounds', []) or [])
        all_war_tags: List[str] = [wt for rnd in assigned_rounds for wt in rnd if wt and '#' in wt]

        if not all_war_tags:
            msg = f"No war tags found in league group for {clan_tag} season {season}."
            logging.info(f"{log_prefix} {msg}")
            return f"⚠️ {msg}"

        logging.info(f"{log_prefix} Starting backfill for season {season} — {len(all_war_tags)} war tag(s)")

        # ── 4. Fetch all wars in parallel ─────────────────────────────────────
        fetch_results = await asyncio.gather(
            *[CACHE.get_league_war(wt) for wt in all_war_tags],
            return_exceptions=True,
        )

        # ── 5. Process each war involving our clan ─────────────────────────────
        processed = 0
        skipped = 0
        errors = 0

        for wt, war_obj in zip(all_war_tags, fetch_results):
            if isinstance(war_obj, BaseException):
                logging.warning(f"{log_prefix} Failed to fetch war {wt}: {war_obj}")
                errors += 1
                continue
            if war_obj is None:  # type: ignore[misc]
                logging.debug(f"{log_prefix} War {wt} returned None — skipping")
                skipped += 1
                continue

            # Filter: only process wars where our clan is a participant
            my_side = getattr(war_obj, 'clan', None)
            opp_side = getattr(war_obj, 'opponent', None)
            my_tag = getattr(my_side, 'tag', None) if my_side else None
            opp_tag_val = getattr(opp_side, 'tag', None) if opp_side else None

            if clan_tag not in (my_tag, opp_tag_val):
                logging.debug(f"{log_prefix} War {wt} does not involve {clan_tag} — skipping")
                skipped += 1
                continue

            other_tag = opp_tag_val if my_tag == clan_tag else my_tag

            # Write JSON (force_save bypasses 24-hour stale guard)
            saved = CACHE.save_war_object(war_obj, tracked_clan_tag=clan_tag, force_save=True)  # type: ignore[misc]
            if not saved:
                logging.debug(f"{log_prefix} save_war_object returned False for war {wt} — already finalized?")
                skipped += 1
                continue

            # Retrieve the actual path written by save_war_object from metadata.
            # Do NOT predict the filename here — save_war_object is the single source
            # of truth for the naming convention; predicting it causes silent mismatches.
            temp_json_path = (CACHE.temp_war_metadata.get(clan_tag) or {}).get("filepath")
            if not temp_json_path:
                logging.warning(f"{log_prefix} Could not resolve temp path for {wt}/{clan_tag} after save")
                errors += 1
                continue

            # Finalize through the normal history pipeline
            try:
                ok = _process_war_history(clan_tag, temp_json_path)
                if ok:
                    processed += 1
                    logging.info(f"{log_prefix} Finalized round vs {other_tag} (war_tag={wt})")
                else:
                    logging.warning(f"{log_prefix} _process_war_history returned False for war {wt}")
                    errors += 1
            except WarProcessingError as wpe:
                logging.warning(f"{log_prefix} _process_war_history error for war {wt}: {wpe}")
                errors += 1

        summary = (
            f"CWL backfill for {clan_tag} season {season}: "
            f"{processed} round(s) saved, {skipped} skipped, {errors} error(s)."
        )
        logging.info(f"{log_prefix} {summary}")
        return f"✅ {summary}" if errors == 0 else f"⚠️ {summary}"

    except Exception as exc:
        logging.error(f"{log_prefix} Unexpected error: {exc}", exc_info=True)
        return f"❌ Unexpected error during CWL backfill for {clan_tag}: {exc}"


async def backfill_cwl_from_war_tags(clan_tag: str, war_tags: List[str]) -> str:
    """
    Backfill CWL war data using a pre-known list of war tags (e.g. sourced from clashspot.net).

    Fetches each war via get_league_war(), verifies our clan is a participant, then writes
    the result through the normal save_war_object() → _process_war_history() pipeline so
    that war_summary and war_attacks tables are populated.

    Idempotent: already-finalized wars are quietly skipped by save_war_object / _process_war_history.

    Args:
        clan_tag: Normalized clan tag with #, e.g. '#2GCRL8L8L'.
        war_tags: List of war tags with #, e.g. ['#8L880P9CG', ...].

    Returns:
        Human-readable summary string suitable for a Discord followup.
    """
    log_prefix = f"[CWL-BACKFILL-TAGS] {clan_tag}"

    if not war_tags:
        return "❌ No war tags provided."

    try:
        # Fetch all wars in parallel
        fetch_results = await asyncio.gather(
            *[CACHE.get_league_war(wt) for wt in war_tags],
            return_exceptions=True,
        )

        processed = 0
        skipped = 0
        errors = 0

        for wt, war_obj in zip(war_tags, fetch_results):
            if isinstance(war_obj, BaseException):
                logging.warning(f"{log_prefix} Failed to fetch war {wt}: {war_obj}")
                errors += 1
                continue
            if war_obj is None:  # type: ignore[misc]
                logging.debug(f"{log_prefix} War {wt} returned None — skipping")
                skipped += 1
                continue

            # Verify our clan is in this war
            my_side = getattr(war_obj, 'clan', None)
            opp_side = getattr(war_obj, 'opponent', None)
            my_tag = getattr(my_side, 'tag', None) if my_side else None
            opp_tag_val = getattr(opp_side, 'tag', None) if opp_side else None

            if clan_tag not in (my_tag, opp_tag_val):
                logging.warning(
                    f"{log_prefix} War {wt}: {clan_tag} not found "
                    f"({my_tag} vs {opp_tag_val}) — skipping"
                )
                skipped += 1
                continue

            other_tag = opp_tag_val if my_tag == clan_tag else my_tag

            saved = CACHE.save_war_object(war_obj, tracked_clan_tag=clan_tag, force_save=True)  # type: ignore[misc]
            if not saved:
                logging.debug(f"{log_prefix} save_war_object returned False for war {wt} — already finalized?")
                skipped += 1
                continue

            # Retrieve the actual path written by save_war_object from metadata.
            # Do NOT predict the filename here — save_war_object is the single source
            # of truth for the naming convention; predicting it causes silent mismatches.
            temp_json_path = (CACHE.temp_war_metadata.get(clan_tag) or {}).get("filepath")
            if not temp_json_path:
                logging.warning(f"{log_prefix} Could not resolve temp path for {wt}/{clan_tag} after save")
                errors += 1
                continue

            try:
                ok = _process_war_history(clan_tag, temp_json_path)
                if ok:
                    processed += 1
                    logging.info(f"{log_prefix} Finalized round vs {other_tag} (war_tag={wt})")
                else:
                    logging.warning(f"{log_prefix} _process_war_history returned False for war {wt}")
                    errors += 1
            except WarProcessingError as wpe:
                logging.warning(f"{log_prefix} _process_war_history error for war {wt}: {wpe}")
                errors += 1

        summary = (
            f"CWL backfill for {clan_tag} from {len(war_tags)} tag(s): "
            f"{processed} round(s) saved, {skipped} skipped, {errors} error(s)."
        )
        logging.info(f"{log_prefix} {summary}")
        return f"✅ {summary}" if errors == 0 else f"⚠️ {summary}"

    except Exception as exc:
        logging.error(f"{log_prefix} Unexpected error: {exc}", exc_info=True)
        return f"❌ Unexpected error during CWL backfill for {clan_tag}: {exc}"


# ──────────────────────────────────────────────────────────────────────────────
# CWL WAR TAG RECOVERY — background catch-up for historical CWL wars
# ──────────────────────────────────────────────────────────────────────────────

_CWL_RECOVERY_FILE: str = os.path.join(CONFIG.data_dir, "missing_cwl_war_tags.txt")
_CWL_RECOVERY_BATCH_SIZE: int = 5000


def load_cwl_recovery_file(file_path: str = _CWL_RECOVERY_FILE) -> int:
    """
    Read war_tags from the CWL recovery file into CACHE.pending_cwl_recovery.

    File format: one war_tag per line (first tab-separated column used).
    Lines starting with '#' are treated as comments and skipped.

    Returns the number of tags loaded.
    """
    from collections import deque as _deque
    if not os.path.exists(file_path):
        return 0
    tags: List[str] = []
    try:
        with open(file_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("# "):
                    continue
                war_tag = line.split("\t")[0]
                if war_tag:
                    tags.append(war_tag)
        CACHE.pending_cwl_recovery = _deque(tags)
        logging.info(f"[CWL-RECOVERY] Loaded {len(tags):,} war_tags from {file_path}")
        return len(tags)
    except Exception as e:
        logging.error(f"[CWL-RECOVERY] Failed to load {file_path}: {e}")
        return 0


def persist_cwl_recovery_file(file_path: str = _CWL_RECOVERY_FILE) -> None:
    """
    Atomically rewrite the CWL recovery file with the war_tags still in
    CACHE.pending_cwl_recovery.  Uses write-to-tmp + os.replace() for atomicity.
    Deletes the file entirely when the queue is empty (recovery complete).
    """
    tmp_path = file_path + ".tmp"
    remaining = list(CACHE.pending_cwl_recovery)
    if not remaining:
        try:
            os.remove(file_path)
            logging.info(f"[CWL-RECOVERY] All tags processed — removed {file_path}")
        except FileNotFoundError:
            pass
        return
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write("# CWL war_tag recovery — remaining tags\n")
            fh.write("# Format: war_tag (one per line)\n\n")
            for tag in remaining:
                fh.write(tag + "\n")
        os.replace(tmp_path, file_path)
        logging.info(f"[CWL-RECOVERY] Persisted {len(remaining):,} remaining tags to {file_path}")
    except Exception as e:
        logging.error(f"[CWL-RECOVERY] Failed to persist {file_path}: {e}")
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass


async def process_cwl_recovery_batch(archive_set: Set[str]) -> int:
    """
    Phase 1.5 — fetch CWL war objects from the API and write temp JSON files only.

    Per tag:
      1. Fetch the war via CACHE.get_league_war(war_tag).
      2. For each participant (clan + opponent that has track_war_updates=True):
         a. Skip if (clan_tag, war_tag) already exists in war_summary DB.
         b. Call save_war_object(force_save=True) to write the temp JSON.
      3. Does NOT call _process_war_history(). The existing Phase 2 orphan scanner
         picks up the written temp files and finalises them via war_write_batch()
         (batched COMMITs, deferred file moves) — no per-write SQLite stalls here.
      4. Every _CWL_RECOVERY_PERSIST_EVERY consumed tags (or when queue empties):
         atomically persist the remaining queue to disk.

    Returns the number of war_tags consumed from the queue this batch.
    """
    if not CACHE.pending_cwl_recovery:
        return 0

    log_prefix = "[CWL-RECOVERY]"
    batch_size = min(_CWL_RECOVERY_BATCH_SIZE, len(CACHE.pending_cwl_recovery))
    batch: List[str] = [CACHE.pending_cwl_recovery.popleft() for _ in range(batch_size)]

    logging.info(
        f"{log_prefix} Processing batch of {batch_size} — "
        f"{len(CACHE.pending_cwl_recovery):,} remaining"
    )

    # Bulk-check DB for (clan_tag, war_tag) pairs already recorded
    existing_pairs: Set[Tuple[str, str]] = set()
    if CACHE.db_manager:
        try:
            existing_pairs = await CACHE.db_manager.get_existing_cwl_war_pairs(batch)
        except Exception as e:
            logging.warning(f"{log_prefix} DB bulk-check failed: {e} — will attempt all writes")

    # Bulk-load war_tag -> league_rank so _fetch_and_write can upsert untracked
    # M3+ clans into clan_name_cache without extra CoC API calls.
    wt_to_league: Dict[str, Optional[str]] = {}
    if CACHE.db_manager:
        try:
            wt_to_league = await CACHE.db_manager.get_war_tag_leagues(batch)
        except Exception as e:
            logging.warning(f"{log_prefix} League bulk-check failed: {e} — clan upserts will be skipped")

    # Fetch all wars in parallel with a dedicated semaphore
    _sem = asyncio.Semaphore(20)

    async def _fetch(wt: str) -> Tuple[Optional[Any], bool]:
        """Returns (war_obj, is_200).  is_200=False for any non-200 response."""
        async with _sem:
            try:
                # Recovery is one-shot historical catch-up. Avoid retaining these
                # large war objects in the long-lived league-war cache.
                war = await CACHE.get_league_war(wt, cache_result=False)
                logging.info(f"{log_prefix} {wt} → 200 OK")
                return war, True
            except coc.NotFound:
                logging.info(f"{log_prefix} {wt} → 404 Not Found")
                return None, False
            except coc.PrivateWarLog:
                logging.info(f"{log_prefix} {wt} → 403 Private War Log")
                return None, False
            except coc.Maintenance:
                logging.info(f"{log_prefix} {wt} → 503 Maintenance")
                return None, False
            except coc.HTTPException as ex:
                status = getattr(ex, 'status', '?')
                logging.info(f"{log_prefix} {wt} → {status} HTTPException")
                return None, False
            except Exception as ex:
                logging.info(f"{log_prefix} {wt} → ERROR {ex}")
                return None, False

    # Pipeline: each coroutine fetches its own war_tag then immediately writes temp files.
    # This eliminates the "dead time" of the old two-phase design (all fetches must
    # complete before the first write starts — ~25 s for 5000 tags with semaphore=20).
    # With pipelining, writes begin ~100 ms into the batch as the first fetches complete.
    #
    # Within a coroutine, clan and opponent writes are sequential (not concurrent).
    # This replaces the old _write_locks_by_wt dict: because both writes for the same
    # war_obj happen in the same coroutine they can never race, and the coc.py
    # non-reentrant generator concern is eliminated without any locking overhead.
    # Members are pre-warmed inside the coroutine (event-loop thread) before the first
    # asyncio.to_thread dispatch — same safety guarantee as the old design.

    written_count = 0
    skip_count = 0
    fail_count = 0
    non_200_tags: List[str] = []
    tags_with_write_jobs: Set[str] = set()
    tags_with_successful_write: Set[str] = set()
    tags_with_exception_write: Set[str] = set()
    tags_with_false_write: Set[str] = set()
    tags_with_failed_write: Set[str] = set()

    async def _fetch_and_write(wt: str) -> Tuple[bool, int, int, bool, bool, bool]:
        """
        Fetch one war_tag then immediately write temp files for its participants.
        Returns (is_200, n_written, n_skipped, had_exception, had_false, had_no_write).
        had_no_write=True means the tag had write candidates but zero succeeded.
        """
        war, is_200 = await _fetch(wt)
        if not is_200:
            return (False, 0, 0, False, False, False)

        my_side = getattr(war, "clan", None)
        opp_side = getattr(war, "opponent", None)

        # Pre-warm members in the coroutine (event-loop thread) so the non-reentrant
        # coc.py generator is materialised before asyncio.to_thread dispatches.
        try:
            if my_side:
                _ = my_side.members  # noqa: F841
            if opp_side:
                _ = opp_side.members  # noqa: F841
        except Exception as _pre_ex:
            logging.debug(f"{log_prefix} {wt} — could not pre-warm members cache: {_pre_ex}")

        my_tag_w: Optional[str] = getattr(my_side, "tag", None) if my_side else None
        opp_tag_w: Optional[str] = getattr(opp_side, "tag", None) if opp_side else None

        to_write: List[str] = []
        n_skipped = 0
        for p_tag in filter(None, [my_tag_w, opp_tag_w]):
            # Do NOT gate on clan_name_cache here. Recovery covers all participants
            # in Master III+ groups, including opponent clans that the bot never
            # tracked and thus never cached. save_war_object(force_save=True) does
            # not need the clan to be in clan_name_cache — it only needs the war
            # object and the target clan tag.
            if (p_tag, wt) in existing_pairs:
                n_skipped += 1
                continue
            to_write.append(p_tag)

        if not to_write:
            # No write candidates — nothing to do (all already in DB or unknown clans).
            return (True, 0, n_skipped, False, False, False)

        # Write clan then opponent sequentially within this coroutine.
        # Sequential order prevents concurrent access to the same war_obj's generators
        # without any locking. Other coroutines (different war_tags) run concurrently.
        n_written = 0
        had_exception = False
        had_false = False
        for p_tag in to_write:
            try:
                ok = await asyncio.to_thread(
                    CACHE.save_war_object, war, p_tag, True  # type: ignore[misc]
                )
                if ok:
                    n_written += 1
                    logging.info(f"{log_prefix} Wrote temp for {wt} / {p_tag}")
                    # Upsert untracked M3+ clans into clan_name_cache so Phase-3B
                    # (and future update-cycle polling) handles them correctly.
                    # Mirrors _upsert_enemy_clan_on_war_start but uses the league
                    # already known from cwl_league_rounds — no extra CoC API call.
                    if p_tag not in CACHE.clan_name_cache:
                        _war_league = wt_to_league.get(wt)
                        if _war_league and _war_league in _CWL_HARVEST_LEAGUES:
                            _side = my_side if p_tag == my_tag_w else opp_side
                            _name = str(getattr(_side, 'name', 'Unknown') or 'Unknown')
                            _now_iso = datetime.now(_tz.utc).isoformat()
                            CACHE.clan_name_cache[p_tag] = {
                                "name": _name,
                                "has_active_subscriptions": False,
                                "last_war_update": _now_iso,
                                "warlog_is_public": True,
                                "last_checked_via_api": _now_iso,
                                "war_league": _war_league,
                                "track_war_updates": True,
                            }
                            try:
                                await CACHE.persist_clan(p_tag)
                                logging.info(
                                    f"{log_prefix} Upserted {p_tag} ({_name}) | "
                                    f"league={_war_league} | track=True"
                                )
                            except Exception as _upsert_ex:
                                logging.warning(
                                    f"{log_prefix} Failed to persist upserted clan "
                                    f"{p_tag}: {_upsert_ex}"
                                )
                else:
                    logging.warning(f"{log_prefix} save_war_object returned False for {wt}/{p_tag}")
                    had_false = True
            except Exception as ex:
                logging.warning(f"{log_prefix} Exception writing {wt}/{p_tag}: {ex}")
                had_exception = True

        had_no_write = (n_written == 0 and not had_exception and not had_false)
        return (True, n_written, n_skipped, had_exception, had_false, had_no_write)

    # Single gather: all war_tags fetched + written concurrently, throttled by _sem.
    all_results: List[Any] = await asyncio.gather(
        *[_fetch_and_write(wt) for wt in batch],
        return_exceptions=True,
    )

    for wt, result in zip(batch, all_results):
        if isinstance(result, BaseException):
            logging.warning(f"{log_prefix} Unhandled exception for {wt}: {result}")
            non_200_tags.append(wt)
            continue

        is_200_r, n_written, n_skipped, had_exception, had_false, had_no_write = result  # type: ignore[misc]

        if not is_200_r:
            non_200_tags.append(wt)
            continue

        written_count += n_written
        skip_count += n_skipped

        if n_written > 0:
            tags_with_write_jobs.add(wt)
            tags_with_successful_write.add(wt)
        elif had_no_write:
            # Had candidates but none wrote — track for re-queue safety check.
            tags_with_write_jobs.add(wt)

        if had_exception:
            tags_with_exception_write.add(wt)
            tags_with_failed_write.add(wt)
            fail_count += 1
        elif had_false:
            tags_with_false_write.add(wt)
            tags_with_failed_write.add(wt)
            fail_count += 1

    # Re-queue exception-failed tags unconditionally.
    exception_requeue_tags: List[str] = sorted(tags_with_exception_write)

    # Re-queue False-return write tags as well (exclude already-counted exception tags).
    false_requeue_tags: List[str] = sorted(tags_with_false_write - tags_with_exception_write)

    # Safety bucket: tags with write candidates but zero successful writes and no tracked failure.
    no_write_tags: List[str] = [
        wt for wt in tags_with_write_jobs
        if wt not in tags_with_successful_write and wt not in tags_with_failed_write
    ]

    # append failed/incomplete tags to the back of the queue for retry.
    requeue_tags = non_200_tags + exception_requeue_tags + false_requeue_tags + no_write_tags
    if requeue_tags:
        CACHE.pending_cwl_recovery.extend(requeue_tags)
        logging.warning(
            f"{log_prefix} Re-queued {len(non_200_tags)} non-200 tag(s) + "
            f"{len(exception_requeue_tags)} exception-write tag(s) + "
            f"{len(false_requeue_tags)} false-write tag(s) + "
            f"{len(no_write_tags)} zero-write tag(s) = {len(requeue_tags)} total re-queued."
        )

    # Change 2: alert when successful file writes are fewer than the batch size.
    if written_count < batch_size:
        logging.warning(
            f"{log_prefix} Only {written_count} successful write(s) for a batch of {batch_size} "
            f"tag(s) — fewer than expected."
        )

    # Persist remaining queue to disk after every batch
    await asyncio.to_thread(persist_cwl_recovery_file)

    net_consumed = batch_size - len(requeue_tags)
    _summary = (
        f"{log_prefix} Batch done: {batch_size} pulled, {net_consumed} net consumed, "
        f"{len(requeue_tags)} re-queued ({len(non_200_tags)} non-200, "
        f"{len(exception_requeue_tags)} exception-write, {len(false_requeue_tags)} false-write, "
        f"{len(no_write_tags)} no-write), "
        f"{written_count} temp files written, {skip_count} already in DB, {fail_count} write failures. "
        f"{len(CACHE.pending_cwl_recovery):,} remaining."
    )
    CACHE.last_cwl_recovery_summary = _summary
    logging.info(_summary)
    return batch_size


async def predict_war_between_clans(clan1_tag: str, clan2_tag: str, n_players: int = 15, apm: int = 2) -> str:
    """
    Predict the outcome of a hypothetical war between two clans using their top-N players by TH.

    Fetches live clan data, resolves CWL leagues, loads skill factors from DB history,
    and runs calculate_win_probability with all attacks remaining (pure pre-war prediction).

    Args:
        clan1_tag: Normalized clan tag (with #) treated as "our" clan.
        clan2_tag: Normalized clan tag (with #) of the opponent.
        n_players: Number of top players (by TH level) to include per side. Defaults to 15.
        apm: Attacks per member — 2 for regular CW, 1 for CWL (tracker #0068). Defaults to 2.

    Returns:
        Discord-ready formatted string with roster emojis and win/lose/draw probabilities.
    """
    from qapbot.formatting import normalize_player_name as _norm_name
    from qapbot.emojis import BotEmojis
    from QBwarsim import calculate_win_probability
    from collections import Counter

    log_prefix = f"[WAR-PREDICT] {clan1_tag} vs {clan2_tag} ({n_players}v{n_players})"
    try:
        # Fetch both clans in parallel
        clan1_obj, clan2_obj = await asyncio.gather(
            CACHE.coc_clan_cache.get_clan(clan1_tag),
            CACHE.coc_clan_cache.get_clan(clan2_tag),
            return_exceptions=True,
        )
        if isinstance(clan1_obj, BaseException) or clan1_obj is None:  # type: ignore[misc]
            return f"❌ Could not fetch clan {clan1_tag}: {clan1_obj}"
        if isinstance(clan2_obj, BaseException) or clan2_obj is None:  # type: ignore[misc]
            return f"❌ Could not fetch clan {clan2_tag}: {clan2_obj}"

        # Top-N members by TH level descending
        def _top_n(clan_obj: Any, n: int) -> List[Any]:
            members = list(getattr(clan_obj, 'members', []) or [])
            members.sort(key=lambda m: getattr(m, 'town_hall', 0) or 0, reverse=True)
            return members[:n]

        my_members  = _top_n(clan1_obj, n_players)
        opp_members = _top_n(clan2_obj, n_players)
        if not my_members:
            return f"❌ No members found for {clan1_tag}"
        if not opp_members:
            return f"❌ No members found for {clan2_tag}"

        my_name  = _norm_name(str(getattr(clan1_obj, 'name', clan1_tag) or clan1_tag))
        opp_name = _norm_name(str(getattr(clan2_obj, 'name', clan2_tag) or clan2_tag))

        # Resolve CWL leagues for both sides
        my_league  = _resolve_war_league(clan1_tag, opponent_tag=clan2_tag)
        opp_league = _resolve_war_league(clan2_tag, opponent_tag=clan1_tag)
        my_league_eff  = my_league  or opp_league or _CWL_LEAGUE_DEFAULT
        opp_league_eff = opp_league or my_league  or _CWL_LEAGUE_DEFAULT

        # Load per-player skill factors from CWL history
        my_tags  = [getattr(m, 'tag', '') for m in my_members  if getattr(m, 'tag', '')]
        opp_tags = [getattr(m, 'tag', '') for m in opp_members if getattr(m, 'tag', '')]
        skill_factors: Dict[str, float] = {}
        skill_factors.update(_load_skill_factors_for_clan(clan1_tag, my_tags,  my_league_eff,  clan_name=my_name))
        skill_factors.update(_load_skill_factors_for_clan(clan2_tag, opp_tags, opp_league_eff, clan_name=opp_name))

        # Build proxy war dict (0 attacks done — pure pre-war prediction)
        proxy = _build_cwl_war_proxy(
            clan1_tag, my_members, clan2_tag, opp_members, apm,
            my_stars=0, opp_stars=0, include_attacks=False,
        )
        total_my  = len(my_members)  * apm
        total_opp = len(opp_members) * apm

        win_prob, lose_prob, draw_prob, confidence = calculate_win_probability(
            proxy, clan1_tag,
            total_my, 0, 0, total_opp,
            player_skill_factors=skill_factors or None,
            cwl_league_my=my_league_eff,
            cwl_league_opp=opp_league_eff,
        )
        logging.info(
            f"{log_prefix} win={win_prob}% lose={lose_prob}% draw={draw_prob}% conf={confidence}%"
        )

        # TH emoji lineup
        def _lineup(members: List[Any]) -> str:
            counts: Counter[int] = Counter(getattr(m, 'town_hall', 0) or 0 for m in members)
            parts: List[str] = []
            for th in sorted(counts, reverse=True):
                emoji = getattr(BotEmojis, f"TH{th:02d}", f"[TH{th}]")
                parts.append(f"{emoji}\u00d7{counts[th]}")
            return " ".join(parts)

        my_lineup  = _lineup(my_members)
        opp_lineup = _lineup(opp_members)
        my_league_str  = f" \u00b7 {my_league}"  if my_league  else ""
        opp_league_str = f" \u00b7 {opp_league}" if opp_league else ""

        if win_prob >= 100:
            prob_line = "🎉 Victory is certain — no mathematical path back for the opponent."
        elif lose_prob >= 100:
            prob_line = "💀 Defeat is certain."
        else:
            prob_line = (
                f"\U0001f4ca The probabilities for {my_name} are:\n"
                f"\u2003   Win **{win_prob}%**, Lose {lose_prob}%, "
                f"Draw {draw_prob}%  \u00b7  Confidence: {confidence}%"
            )

        return (
            f"\u2b50 War Prediction: {my_name} vs {opp_name} ({n_players}v{n_players})\n\n"
            f"Rosters:\n"
            f"\u200e{my_name}{my_league_str}\u200e: {my_lineup}\n"
            f"\u200e{opp_name}{opp_league_str}\u200e: {opp_lineup}\n\n"
            f"{prob_line}"
        )

    except Exception as exc:
        logging.error(f"{log_prefix} Unexpected error: {exc}", exc_info=True)
        return f"❌ War prediction failed: {exc}"


async def _mark_clan_deleted(clan_tag: str) -> None:
    """Mark a clan as deleted after confirming it no longer exists in the CoC API.

    Sets is_deleted=True in the in-memory cache and persists to DB.
    The clan will be skipped by the Phase-1 update loop until it is found
    alive again (which clears the flag via _update_clan_metadata).

    Also runs self-healing cleanup: if nothing else in the database refers to
    this clan_tag (no subscription, family/guild membership, guild clan role,
    registered player, or war history), the row is hard-deleted from `clans`
    instead of being left behind forever as a soft-deleted placeholder. This
    is what cleans up bogus rows (e.g. a player tag mistakenly stored as a
    clan_tag) once the API confirms they're not real clans.
    """
    clan_data = CACHE.clan_name_cache.get(clan_tag)
    if clan_data:
        if not clan_data.get("is_deleted"):
            clan_data["is_deleted"] = True
            await CACHE.persist_clan(clan_tag)
            logging.warning(
                f"[CLAN-DELETED] {clan_tag} — CoC API returned 404 (NotFound). "
                f"Marked as deleted; will be skipped until confirmed alive again."
            )
    else:
        # Clan not in cache (edge case); write directly to DB
        logging.warning(
            f"[CLAN-DELETED] {clan_tag} — CoC API returned 404 (NotFound), "
            f"but clan is not in memory cache. Cannot mark deleted."
        )

    # Self-healing: purge the row entirely if nothing else references it.
    try:
        await CACHE.purge_clan_if_orphaned(clan_tag)
    except Exception as exc:
        logging.error(f"[CLAN-DELETED] Orphan-purge check failed for {clan_tag}: {exc}")



async def fetch_clan_war_data(clan_tag: str) -> Optional[Dict[str, Any]]:
    """
    Fetch war data from CoC API (Phase 1 - Parallel Processing).
    
    This function is optimized for parallel execution via asyncio.gather().
    Only lightweight operations here - API calls and JSON saves only.
    Heavy file I/O and directory scanning moved to process_clan_war_data().
    
    Args:
        clan_tag: Clan tag (e.g., '#2C9UR9GJY')
    
    Returns:
        Dict with war data if successful, None if no war/error:
        {
            'clan_tag': str,
            'war_obj': coc.War object,
            'opponent_tag': str,
            'state': str
        }
    """
    try:
        # === PHASE 1: PARALLEL API CALLS + LIGHTWEIGHT PROCESSING ===
        # Fetch clan data (optional - every 2 days)
        should_fetch_from_api = True
        clan_data = CACHE.clan_name_cache.get(clan_tag)
        age = None
        if isinstance(clan_data, dict):
            last_checked_str = clan_data.get('last_checked_via_api')  # type: ignore[misc]
            if last_checked_str:
                try:
                    # Parse ISO format timestamp with timezone info
                    last_checked = datetime.fromisoformat(last_checked_str)  # type: ignore[arg-type]
                    now_utc = datetime.now(_tz.utc)
                    age = now_utc - last_checked
                    # Determine freshness gate:
                    # - Clans used by a role-enabled guild: 30 min (1800 s) — ensures promotions,
                    #   demotions and clan moves are reflected within one periodic cycle.
                    # - All other clans: 12 h (43200 s) — conserves API quota.
                    _ROLE_GATE = 1800    # 30 min
                    _DEFAULT_GATE = 43200  # 12 h
                    _is_role_clan = False
                    for _gid, _gcfg in CACHE.server_config.items():
                        if not (_gcfg.get("coc_role_enabled") or _gcfg.get("clan_role_enabled")):
                            continue
                        if clan_tag in _gcfg.get("member_clans", []):
                            _is_role_clan = True
                            break
                        for _fid in _gcfg.get("member_families", []):
                            if clan_tag in CACHE.clan_families.get(_fid, {}).get("clans", []):
                                _is_role_clan = True
                                break
                        if _is_role_clan:
                            break
                    _gate = _ROLE_GATE if _is_role_clan else _DEFAULT_GATE
                    if age.total_seconds() < _gate:
                        logging.debug(
                            f"Skipping API fetch for {clan_tag} - last checked "
                            f"{age.total_seconds()/SECONDS_PER_HOUR:.1f}h ago "
                            f"(gate: {'30min role-clan' if _is_role_clan else '12h standard'})"
                        )
                        should_fetch_from_api = False
                except (ValueError, TypeError) as e:
                    logging.warning(f"Could not parse last_checked_via_api for {clan_tag}: {e} - forcing API fetch")

        # Essential-field override: if any required fields are missing in the
        # clan_name_cache entry (e.g. war_league NULL after a DB schema migration
        # that added a new column), bypass the TTL gate and fetch immediately so
        # the data is backfilled without waiting a full 12h/30min TTL cycle.
        # Add new field names to _ESSENTIAL_CLAN_FIELDS as the data model evolves.
        _ESSENTIAL_CLAN_FIELDS = ('war_league',)
        if not should_fetch_from_api and isinstance(clan_data, dict):
            _missing = [f for f in _ESSENTIAL_CLAN_FIELDS if not clan_data.get(f)]
            if _missing:
                logging.info(
                    f"[ESSENTIAL-FIELD-BYPASS] {clan_tag}: forcing API fetch because "
                    f"required field(s) {_missing} are missing in clan_name_cache "
                    f"(TTL gate would have suppressed this fetch)"
                )
                should_fetch_from_api = True

        # Fetch from API only if needed (will auto-update last_checked_via_api in cache manager)
        _clan_obj = None
        if should_fetch_from_api:
            try:
                _clan_obj = await CACHE.coc_clan_cache.get_clan(clan_tag)
                if age:
                    logging.debug(f"[API CALL] get_clan fetch for {clan_tag} - last checked {age.total_seconds()/SECONDS_PER_HOUR:.1f} hours ago")
                else:
                    logging.debug(f"[API CALL] get_clan fetch for {clan_tag} - first check or cache not available")
            except coc.NotFound:
                # Clan no longer exists in CoC — confirm by marking deleted and skip.
                await _mark_clan_deleted(clan_tag)
                return None
            except Exception as e:
                raise WarDataFetchError(
                    f"Error fetching clan {clan_tag}: {type(e).__name__}: {e}",
                    context={"clan_tag": clan_tag, "error_type": type(e).__name__}
                )
        
        # Fetch current war data from API
        coc_war_obj = None
        try:
            coc_war_obj = await CACHE.get_current_war_from_api(clan_tag)
            logging.debug(f"[COC-API-CALL] Fetching of current war for clan {clan_tag} from API COMPLETED successfully!")
            
            # SUCCESS: Mark warlog as public if it was previously marked private
            clan_data = CACHE.clan_name_cache.get(clan_tag)
            if clan_data and isinstance(clan_data, dict):  # type: ignore[misc]
                if clan_data.get('warlog_is_public') == False:  # type: ignore[misc]
                    clan_data['warlog_is_public'] = True
                    await CACHE.persist_clan(clan_tag)
                    logging.info(f"[PUBLIC-WARLOG] {clan_tag} - marked warlog_is_public=True (was previously private)")
            
        except (coc.PrivateWarLog, coc.NotFound) as _private_exc:
            # GET /clans/{tag}/currentwar returns 403/404 for private warlogs.
            # CWL war data IS still accessible via /currentwar/leaguegroup + /clanwarleagues/wars/{warTag}
            # which bypass warlog privacy entirely. Try that fallback before giving up.
            _exc_label = "PrivateWarLog" if isinstance(_private_exc, coc.PrivateWarLog) else "NotFound/404"

            # NotFound from get_current_war: if get_clan was skipped (TTL gate), verify the
            # clan still exists. If it doesn't, mark deleted and stop.  When get_clan already
            # succeeded earlier in this call the clan is definitively alive, so skip the check.
            if isinstance(_private_exc, coc.NotFound) and not should_fetch_from_api:
                _clan_alive = True
                try:
                    await CACHE.coc_clan_cache.get_clan(clan_tag)
                except coc.NotFound:
                    _clan_alive = False
                except Exception:
                    pass  # API transient error — assume alive; retry next cycle
                if not _clan_alive:
                    await _mark_clan_deleted(clan_tag)
                    return None

            # Mark warlog as private in cache immediately
            clan_data = CACHE.clan_name_cache.get(clan_tag)
            if clan_data and isinstance(clan_data, dict):  # type: ignore[misc]
                clan_data['warlog_is_public'] = False

            # CWL fallback: use shortcut + cached league group to find active war
            _fallback_result = await _find_active_cwl_war_for_clan(
                clan_tag, label="CWL-FALLBACK"
            )
            if _fallback_result:
                coc_war_obj = _fallback_result[0]

            if not coc_war_obj:
                # No CWL war found either — update last_war_update and skip this cycle
                clan_data = CACHE.clan_name_cache.get(clan_tag)  # type: ignore[arg-type]
                if clan_data and isinstance(clan_data, dict):  # type: ignore[misc]
                    clan_data['last_war_update'] = datetime.now(_tz.utc).isoformat()
                    await CACHE.persist_clan(clan_tag)  # type: ignore[arg-type]
                logging.info(f"[PRIVATE-WARLOG] {clan_tag} - warlog_is_public=False, no active CWL war found")
                return None

            # CWL war found — persist the warlog_is_public=False flag and fall through to processing
            clan_data = CACHE.clan_name_cache.get(clan_tag)  # type: ignore[arg-type]
            if clan_data and isinstance(clan_data, dict):  # type: ignore[misc]
                await CACHE.persist_clan(clan_tag)  # type: ignore[arg-type]
            logging.info(f"[CWL-FALLBACK] {clan_tag} - processing CWL war via league group fallback")
        
        except Exception as e:
            raise WarDataFetchError(
                f"Error fetching current war for {clan_tag}: {type(e).__name__}: {e}",
                context={"clan_tag": clan_tag, "error_type": type(e).__name__}
            )
        
        if not coc_war_obj:
            # get_current_war() returns None in these cases:
            # 1. Clan is not currently in a war (notInWar state)
            # 2. War log is private (PrivateWarLog consumed by method)
            # 3. Requested war round doesn't exist
            # CWL FALLBACK: During CWL (including mid-month bonus CWL), the /currentwar
            # endpoint returns notInWar for public-warlog clans even when the clan is
            # assigned to an active league war in preparation/in_war state.  Only the
            # /clanwarleagues/wars/{tag} endpoint returns the real state.
            # Guard: is_latest_cwl_season_ended_sync returns False when cwl_ended=0 OR
            # when no DB row exists at all (conservative — ensures first-ever CWL
            # detection works for clans with no history).  Regular-war exclusivity and
            # season-ended checks are handled inside _find_active_cwl_war_for_clan.
            if CACHE.db_manager is None or not await asyncio.to_thread(
                CACHE.db_manager.is_latest_cwl_season_ended_sync, clan_tag
            ):
                # Subscribed clans (active every ~3 min): 2h TTL to avoid hammering
                # the league group endpoint while in a stable notInWar state.
                # 22h-tracked clans: polled at most once per 22h anyway — use 5 min TTL
                # so that a newly discovered CWL is picked up within one cycle.
                _clan_data_niw = CACHE.clan_name_cache.get(clan_tag)
                _is_subscribed_niw = (
                    isinstance(_clan_data_niw, dict)
                    and bool(_clan_data_niw.get('has_active_subscriptions'))
                )
                _niw_max_age = 7200.0 if _is_subscribed_niw else 300.0
                _notinwar_cwl = await _find_active_cwl_war_for_clan(
                    clan_tag, label="CWL-NOTINWAR-FALLBACK", max_age=_niw_max_age
                )
                if _notinwar_cwl:
                    coc_war_obj = _notinwar_cwl[0]
                    logging.info(
                        f"[CWL-NOTINWAR-FALLBACK] {clan_tag} - found active CWL war "
                        f"(public-warlog clan returned notInWar on /currentwar)"
                    )
                    # Fall through to metadata extraction and save below
                else:
                    logging.debug(f"No active war for clan {clan_tag} (notInWar, no active CWL war found)")
                    return None
            else:
                logging.debug(f"No active war for clan {clan_tag} (notInWar state, CWL season ended)")
                return None

        # Extract war metadata
        state = getattr(coc_war_obj, 'state', '')
        my_clan = getattr(coc_war_obj, 'clan', None)
        opp_clan = getattr(coc_war_obj, 'opponent', None)
        opponent_tag = getattr(opp_clan, 'tag', 'UNK') if opp_clan else 'UNK'

        # coc.py occasionally returns a War object with state=notInWar instead of None
        # (clan/opponent have no tag, all zeroes). Apply the same CWL fallback as above.
        if state == 'not_in_war':
            if CACHE.db_manager is None or not await asyncio.to_thread(
                CACHE.db_manager.is_latest_cwl_season_ended_sync, clan_tag
            ):
                _clan_data_niw2 = CACHE.clan_name_cache.get(clan_tag)
                _is_subscribed_niw2 = (
                    isinstance(_clan_data_niw2, dict)
                    and bool(_clan_data_niw2.get('has_active_subscriptions'))
                )
                _niw_max_age2 = 7200.0 if _is_subscribed_niw2 else 300.0
                _notinwar_cwl2 = await _find_active_cwl_war_for_clan(
                    clan_tag, label="CWL-NOTINWAR-FALLBACK", max_age=_niw_max_age2
                )
                if _notinwar_cwl2:
                    coc_war_obj = _notinwar_cwl2[0]
                    state = getattr(coc_war_obj, 'state', '')
                    my_clan = getattr(coc_war_obj, 'clan', None)
                    opp_clan = getattr(coc_war_obj, 'opponent', None)
                    opponent_tag = getattr(opp_clan, 'tag', 'UNK') if opp_clan else 'UNK'
                    logging.info(
                        f"[CWL-NOTINWAR-FALLBACK] {clan_tag} - found active CWL war "
                        f"(public-warlog clan returned notInWar war object)"
                    )
                    # Fall through to save below with refreshed metadata
                else:
                    logging.debug(f"No active war for clan {clan_tag} (notInWar war object, no active CWL war)")
                    return None
            else:
                logging.debug(f"No active war for clan {clan_tag} (notInWar war object — treating as no war)")
                return None

        # Save war object to JSON — offloaded to thread to avoid blocking
        # the event loop with sync file I/O during Phase 1 parallel fetches.
        if my_clan and opp_clan:
            # CWL pre-save invariant: the war_tag must be present in cwl_league_rounds
            # BEFORE the temp file is written.  Any file written with an unresolvable
            # war_tag produces UNKNOWN round data in analysis and archiving pipelines.
            _is_cwl_l2 = bool(getattr(coc_war_obj, 'is_cwl', False))
            _state_l2 = str(state).lower().replace('_', '').replace(' ', '')
            if _is_cwl_l2 and _state_l2 in ('preparation', 'inwar', 'warended') and CACHE.db_manager is not None:
                _war_tag_l2 = str(getattr(coc_war_obj, 'war_tag', '') or '')
                # Wrapped in to_thread(): sync sqlite3 call, awaited directly from the
                # Phase-1 hot path (every CWL-active clan, every cycle) — unwrapped, this
                # blocks the event loop for its I/O duration, most often during CWL season.
                if _war_tag_l2 and await asyncio.to_thread(
                    CACHE.db_manager.get_cwl_round_for_war_tag_sync, _war_tag_l2
                ) is None:
                    # Unknown war_tag: populate cwl_league_rounds NOW, before the
                    # temp file is created, so the round assignment is always present.
                    try:
                        await CACHE.get_league_group(clan_tag, max_age=300)
                        logging.debug(f"[CWL-ROUNDS] Layer 2 (pre-save): get_league_group for {clan_tag} (war_tag={_war_tag_l2}, state={_state_l2})")
                    except Exception as _l2_ex:
                        logging.debug(f"[CWL-ROUNDS] Layer 2 (pre-save): get_league_group failed for {clan_tag}: {_l2_ex}")

            _war_saved = await asyncio.to_thread(
                CACHE.save_war_object, coc_war_obj, clan_tag  # type: ignore[arg-type]
            )

            # CWL fallback for private-warlog clans: the CoC API can return the last
            # war_ended war instead of raising PrivateWarLog when a new CWL war is active.
            # Detect this case: save was skipped (already finalized) + state is war_ended.
            # If so, try the league group route which bypasses warlog privacy entirely.
            _state_lower = str(state).lower().replace('_', '').replace(' ', '')
            if not _war_saved and _state_lower == 'warended':
                _fallback2_result = await _find_active_cwl_war_for_clan(
                    clan_tag, label="CWL-FALLBACK"
                )
                if _fallback2_result:
                    # Replace war object and re-derive metadata from the active CWL war
                    coc_war_obj = _fallback2_result[0]
                    state = getattr(coc_war_obj, 'state', '')
                    my_clan = getattr(coc_war_obj, 'clan', None)
                    opp_clan = getattr(coc_war_obj, 'opponent', None)
                    opponent_tag = getattr(opp_clan, 'tag', 'UNK') if opp_clan else 'UNK'
                    await asyncio.to_thread(
                        CACHE.save_war_object, coc_war_obj, clan_tag  # type: ignore[arg-type]
                    )
                    # Update warlog_is_public flag since we only reach here if it's private
                    clan_data = CACHE.clan_name_cache.get(clan_tag)  # type: ignore[arg-type]
                    if clan_data and isinstance(clan_data, dict):  # type: ignore[misc]
                        clan_data['warlog_is_public'] = False
                        await CACHE.persist_clan(clan_tag)  # type: ignore[arg-type]

            # First-time enemy discovery: upsert the enemy clan into the DB
            # so we capture its war_league for future predictions, and (if CWL)
            # add it to the 22h polling pool.
            _opp_tag = getattr(opp_clan, 'tag', None)
            if _opp_tag and _opp_tag not in CACHE.clan_name_cache:
                _is_cwl = bool(getattr(coc_war_obj, 'is_cwl', False))
                await _upsert_enemy_clan_on_war_start(clan_tag, _opp_tag, coc_war_obj, _is_cwl)  # type: ignore[arg-type]

            # Ensure the tracked clan's own war_league is resolved.
            # Tries: clan_name_cache → API fetch → opponent's league.
            # This is cheap when the league is already cached; only triggers an
            # API call when the tracked clan has not been fetched yet this session.
            await _ensure_clan_war_league(clan_tag, _opp_tag)  # type: ignore[arg-type]
        else:
            # my_clan or opp_clan is None despite a non-null war object.
            # This is unexpected — log a warning, count it, and write a diagnostic file.
            logging.warning(
                f"[SAVE-SKIP-NO-CLAN] Skipping war object save for {clan_tag}: "
                f"state={state}, my_clan={bool(my_clan)}, opp_clan={bool(opp_clan)}"
            )
            CACHE.record_cycle_stat("save_skip_no_clan")
            try:
                import json as _json
                import re as _re
                import datetime as _dt_mod
                from qapbot.config import CONFIG as _CFG
                _inv_dir = _CFG.investigate_dir
                os.makedirs(_inv_dir, exist_ok=True)
                _tag_clean = _re.sub(r'[^A-Z0-9]', '', clan_tag.upper())
                # Only write once per clan — skip if any file for this tag already exists
                _existing = [f for f in os.listdir(_inv_dir) if f.startswith(_tag_clean) and f.endswith("_no_clan.json")]
                if not _existing:
                    _ts = _dt_mod.datetime.now(_dt_mod.timezone.utc).strftime("%Y%m%d%H%M%S")
                    _inv_file = os.path.join(_inv_dir, f"{_tag_clean}_{_ts}_no_clan.json")
                    _war_type_raw = getattr(coc_war_obj, "type", None)  # type: ignore[arg-type]
                    with open(_inv_file, 'w', encoding='utf-8') as _f:
                        _json.dump({
                            "reason": "no_clan",
                            "clan_tag": clan_tag,
                            "state": str(state),
                            "my_clan_present": bool(my_clan),
                            "opp_clan_present": bool(opp_clan),
                            "war_type": str(_war_type_raw) if _war_type_raw else None,
                            "is_cwl": bool(getattr(coc_war_obj, "is_cwl", False)),  # type: ignore[arg-type]
                        }, _f, indent=2)
            except Exception as _inv_ex:
                logging.warning(f"[INVESTIGATE-WRITE] Could not write investigate file for {clan_tag}: {_inv_ex}")

        # Return war data for Phase 2 processing
        return {
            'clan_tag': clan_tag,
            'war_obj': coc_war_obj,
            'opponent_tag': opponent_tag,
            'state': state
        }

    except Exception as e:
        raise WarDataFetchError(
            f"Exception fetching war data for {clan_tag}: {type(e).__name__}: {e}",
            context={"clan_tag": clan_tag, "error_type": type(e).__name__}
        )





def process_clan_war_data(clan_tag: str, war_data: Dict[str, Any], war_files_prescan: Optional[List[str]] = None, archive_set: Optional[Set[str]] = None) -> bool:
    """
    Process war data after API calls (Phase 2 - Sequential Processing).
    
    Handles file management, temp stats calculation, and state management.
    This function contains all the heavy sequential processing that was blocking
    parallel API calls in the old architecture.
    
    Args:
        clan_tag: Clan tag (e.g., '#2C9UR9GJY')
        war_data: Dict returned from fetch_clan_war_data() containing:
            - war_obj: coc.War object
            - opponent_tag: Opponent clan tag
            - state: War state string
        war_files_prescan: Optional pre-scanned list of war file paths for this clan.
                          Passed through to manage_war_files() to avoid repeated glob calls.
    
    Returns:
        bool: True if processing succeeded, False otherwise
    
    Processing Steps:
        1. Manage war files (finalization, archiving, cleanup)
        2. Handle war ID shifts and timestamp updates
        3. Calculate temp stats for active wars
    
    Note:
        This function is called AFTER all parallel API calls complete to avoid
        blocking the event loop with file I/O and directory scanning.
    """
    try:
        coc_war_obj = war_data['war_obj']
        opponent_tag = war_data['opponent_tag']
        state = war_data['state']
        
        # === FAST PATH: Skip heavy processing for states that don't need it ===
        # When a clan has no temp files AND state doesn't need temp stats,
        # we can skip file management and all the expensive parsing.
        _has_temp_files = bool(war_files_prescan)
        if not _has_temp_files and state not in ('preparation', 'in_war'):
            # No files to finalize, no temp stats to build — just clear stale stats
            CACHE.set_temp_war_stats(clan_tag, {})
            return True
        
        # === PHASE 2: SEQUENTIAL FILE PROCESSING ===
        # UNIFIED WAR FILE MANAGEMENT
        # Single function handles all war file lifecycle: finalization, archiving, cleanup
        # This prevents race conditions where cleanup archives files before finalization
        # NOTE: manage_war_files() is synchronous - no blocking async calls
        if _has_temp_files:
            manage_war_files(clan_tag, opponent_tag, war_files_prescan=war_files_prescan, archive_set=archive_set)

        # Extract war metadata for temp stats calculation
        my_clan = getattr(coc_war_obj, 'clan', None)
        _opp_clan = getattr(coc_war_obj, 'opponent', None)
        raw_start = str(getattr(coc_war_obj, 'start_time', ""))
        
        # Parse start time for both compact format (war ID) and ISO format (CSV date)
        start_time_compact = None  # For war ID: YYYYMMDDHHMM
        start_time_iso = None      # For CSV Date column: YYYY-MM-DDTHH:MM
        
        m = re.search(r'datetime\.datetime\(([^)]+)\)', raw_start)
        if m:
            dt_args = [int(x.strip()) for x in m.group(1).split(',')]
            if len(dt_args) >= 5:
                y, mo, d, h, mi = dt_args[:5]
                start_time_compact = f"{y:04d}{mo:02d}{d:02d}{h:02d}{mi:02d}"
                start_time_iso = f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}"
        
        start_dt_compact = start_time_compact or datetime.now(_tz.utc).strftime("%Y%m%d%H%M")
        start_dt_iso = start_time_iso or datetime.now(_tz.utc).strftime("%Y-%m-%dT%H:%M")

        # Determine existing temp war (if any)
        existing_war_id, existing_date, existing_opponent_tag = (None, None, None)
        temp_stats = CACHE.get_temp_war_stats(clan_tag)
        if temp_stats:
            first_entry = next(iter(temp_stats.values()), None)
            if first_entry:
                existing_war_id = first_entry.get('WarID', None)
                existing_date = first_entry.get('Date', None)
                # Extract opponent tag from existing War ID
                if existing_war_id and '_' in existing_war_id:
                    existing_opponent_tag = existing_war_id.split('_')[0]
            logging.debug(f"[FINALIZE-DIAG] temp_stats loaded for {clan_tag}: war_id='{existing_war_id}', opponent='{existing_opponent_tag}', date='{existing_date}'")
        else:
            logging.debug(f"[FINALIZE-DIAG] No temp stats present for {clan_tag}")
        
        # --- War ID construction (uses compact format) ---
        candidate_new_war_id = f"{(opponent_tag or 'UNK').lstrip('#')}_{start_dt_compact}"
        candidate_opponent_tag = (opponent_tag or 'UNK').lstrip('#')
        logging.debug(f"[WAR-ID-CANDIDATE] candidate_new_war_id for {clan_tag}: {candidate_new_war_id}, opponent: {candidate_opponent_tag}")

        # Check if war IDs are identical (normal case - same war, same timestamp)
        war_id_matches = (existing_war_id == candidate_new_war_id)
        
        # If War IDs differ but opponent is same, it's a timestamp shift (same war, different timestamp)
        same_opponent = (existing_opponent_tag == candidate_opponent_tag) if existing_opponent_tag else False
        
        if not war_id_matches and same_opponent and existing_war_id:
            # Same war, different timestamp - UPDATE War ID in temp file (no finalization)
            # This is expected behavior during CoC maintenance or server-side time adjustments
            logging.info(
                f"[WAR-ID-SHIFT] War ID timestamp shifted for {clan_tag}: "
                f"'{existing_war_id}' → '{candidate_new_war_id}' | "
                f"Same opponent: {candidate_opponent_tag} | "
                f"Updating temp file (no finalization to prevent duplicates)"
            )
            # Update all entries in temp_stats with new War ID and date
            for pid in temp_stats:
                temp_stats[pid]['WarID'] = candidate_new_war_id
                temp_stats[pid]['Date'] = start_dt_iso
            CACHE.set_temp_war_stats(clan_tag, temp_stats)
            # Update tracking variables to reflect the updated War ID
            existing_war_id = candidate_new_war_id
            existing_date = start_dt_iso

        # War ID and formatted start time for current war
        war_id = candidate_new_war_id
        formatted_start = start_dt_iso  # Use ISO format for CSV Date column
        attacks_per_member = getattr(coc_war_obj, 'attacks_per_member', 2) or 2

        # Only create/update temp stats for active wars (preparation or in_war)
        if state in ('preparation','in_war'):
            temp_stats_to_save: Dict[str, Any] = {}
            if my_clan and hasattr(my_clan, 'members'):
                for m in my_clan.members:
                    tag_m = getattr(m, 'tag', '')
                    name_m = getattr(m, 'name', '')
                    if not name_m or not tag_m:
                        logging.error(f"Error in war data. Player_tag: ""{tag_m}"", Clan_tag: ""{clan_tag}""")
                        return False
                    atk_list = list(getattr(m, 'attacks', []) or [])
                    stars = sum(getattr(a, 'stars', 0) for a in atk_list)
                    attacks = len(atk_list)
                    total_dest_pct = sum(float(getattr(a, 'destruction', 0.0) or 0.0) for a in atk_list)
                    # INVARIANT: Missed_Attacks is ALWAYS 0 for ongoing wars (preparation/in_war states)
                    # Rationale: Players can still make their unused attacks before war ends
                    # Final missed attack count is calculated only when war ends and is finalized to history
                    # This ensures currentwar leaderboards show attacks made, not total attack opportunities
                    missed_attacks = 0  # Always zero during preparation/in_war
                    best_opp_attack = getattr(m, 'best_opponent_attack', None)
                    defensive_stars = 0
                    if best_opp_attack:
                        defensive_stars = getattr(best_opp_attack, 'stars', 0)
                    temp_stats_to_save[tag_m] = {
                        "WarID": war_id,
                        "Date": formatted_start,  # Now uses ISO format (YYYY-MM-DDTHH:MM)
                        "Player": name_m,
                        "PlayerID": tag_m,
                        "TH_lvl": getattr(m, 'town_hall', 0),
                        "Stars": stars,
                        "Attacks": attacks,
                        "Missed_Attacks": missed_attacks,
                        "Max_Attacks": attacks_per_member,
                        "Defensive_Stars": defensive_stars,
                        "Total_Dest_Pct": total_dest_pct,
                    }
            CACHE.set_temp_war_stats(clan_tag, temp_stats_to_save)
        else:
            # War ended or not in war - clear temp stats to prevent stale data in currentwar leaderboards
            logging.debug(f"[TEMP-STATS-CLEAR] Clearing temp_war_stats for {clan_tag} (state={state})")
            CACHE.set_temp_war_stats(clan_tag, {})
        return True
        
    except Exception as e:
        raise WarProcessingError(
            f"Exception processing war data for {clan_tag}: {type(e).__name__}: {e}",
            context={"clan_tag": clan_tag, "error_type": type(e).__name__}
        )


async def update_clan_war_info_and_stats(clan_tag: str) -> bool:
    """
    Fetch and process clan war information (wrapper for 2-phase architecture).
    
    This function orchestrates the complete war update process:
    - Phase 1: Parallel API calls (fetch_clan_war_data)
    - Phase 2: Sequential processing (process_clan_war_data)
    
    For optimal performance, call this function via parallel execution:
        tasks = [update_clan_war_info_and_stats(tag) for tag in clans]
        results = await asyncio.gather(*tasks)
    
    Args:
        clan_tag: Clan tag to update (e.g., '#2C9UR9GJY')
    
    Returns:
        bool: True if update succeeded OR if there is no active war (notInWar,
              CWL prep day, private warlog — all are normal expected states).
              False only when process_clan_war_data() fails to process valid war data.
              True errors (API failures, etc.) raise WarDataFetchError.
    
    Architecture:
        OLD: Mixed async API calls with blocking file I/O → serialized execution
        NEW: Separate phases → true parallelization of API calls
    
    Note:
        For advanced usage, call fetch_clan_war_data() and process_clan_war_data()
        separately to maximize parallelization across all clans.
    """
    # Phase 1: Fetch war data (async)
    war_data = await fetch_clan_war_data(clan_tag)
    if war_data is None:
        # None means no active regular war (notInWar, CWL prep day, private warlog).
        # This is expected and correct — not a failure. True errors raise WarDataFetchError.
        return True
    
    # Phase 2: Process war data (sync file operations)
    return process_clan_war_data(clan_tag, war_data)


async def process_orphaned_cwl_wars(
    failed_clans: Optional[set[str]] = None,
    temp_files_by_clan: Optional[dict[str, list[str]]] = None,
    archive_set: Optional[set[str]] = None,
) -> Optional[Set[str]]:
    """
    Identify and finalize all orphaned CWL wars across all clans with parallel API calls.
    
    CWL Context:
    - Wars happen in quick succession (War 1 ends → War 2 starts immediately)
    - get_current_war() only returns the active war (War 2)
    - War 1 becomes "orphaned" with stale state="inWar" in temp file
    - get_current_war() will NEVER return War 1 again
    
    Edge Case - Private Warlog After CWL:
    - CWL war ends, clan has private warlog
    - API returns None/404 (ended CWL + private warlog = no data)
    - Old CWL file stays as "newest" but is actually orphaned
    - Solution: If clan in failed_clans, treat "newest" CWL file as orphan
    
    Orphan Detection:
    - CWL war with state != "war_ended"
    - AND (file is NOT the newest file for that clan OR clan in failed_clans)
    - AND the war's battle day is over (end_time more than 1h in the past).
      The last condition matters because CWL rounds overlap — round N+1's file
      appears while round N is still being fought, so "not the newest file" on
      its own flags plenty of wars that cannot possibly be finalized yet.
    
    Args:
        failed_clans: Set of clan tags where API fetch returned None/failed
        temp_files_by_clan: Pre-scanned dict mapping safe clan tag → list of
            temp file paths.  Shared with Phase 3 to avoid a redundant
            glob.glob() over the entire temp directory.
        archive_set: Pre-built set of archive filenames for O(1) existence
            checks, shared with Phase 3.
    
    Returns:
        Set of clan tags that were successfully finalized (files moved to
        archive), or None if no orphans found.  Callers should refresh
        temp_files_by_clan for these clans before Phase 3.
    
    This function:
    1. Scans pre-grouped temp files for CWL wars with state != "war_ended"
    2. Identifies which is the CURRENT war (newest file per clan via filename timestamp)
    3. Fetches final data for ORPHANED wars via get_league_war() in PARALLEL
    4. Updates JSON files with complete data
    5. Wars will be finalized on next manage_war_files() call
    
    Called once per update cycle AFTER all clan war updates complete.
    Keeps manage_war_files() simple and allows parallel API calls for orphans.
    
    Example:
        # In periodic_main():
        results = await asyncio.gather(*[fetch_clan_war_data(tag) for tag in clans])
        finalized = await process_orphaned_cwl_wars(failed, prescan, archive)
    """
    import glob
    import json
    import os
    import asyncio

    temp_dir = os.path.join(CONFIG.data_dir, "temp")
    
    # Build file-by-clan map if not provided by caller (fallback path)
    if temp_files_by_clan is not None:
        _files_by_clan = temp_files_by_clan
    else:
        _all = [
            fp
            for sd in glob.glob(os.path.join(temp_dir, "shard_*"))
            for fp in glob.glob(os.path.join(sd, "*_war_data.json"))
        ]
        _files_by_clan: dict[str, list[str]] = {}  # type: ignore[no-redef]
        for _fp in _all:
            _safe = os.path.basename(_fp).split("_")[0]
            _files_by_clan.setdefault(_safe, []).append(_fp)
    
    # ── Offload synchronous file scanning to a thread ────────────────
    # Pre-scanned file list eliminates redundant glob.glob() and getmtime()
    # calls over thousands of temp files on server-machine storage.
    def _scan_orphans(
        _fbclan: dict[str, list[str]], _failed: set[str]  # type: ignore[type-arg]
    ) -> list[tuple[str, str, str, str, str]]:  # type: ignore[type-arg]
        """Pure-sync helper: scan pre-grouped temp files, return orphaned
        (war_tag, clan_tag, war_file, filename, state) tuples.
        
        Optimisations over the original full-scan version:
        - Uses pre-scanned file list (no redundant glob.glob over the entire
          temp/ directory tree, which grows to tens of thousands of files)
        - Uses filename timestamp instead of os.path.getmtime (eliminates stat() syscalls)
        - For single-file clans, uses end_time + 1h instead of "newer file exists" signal
        - For multi-file clans, requires end_time + 1h *in addition to* that signal
          (see _orphan_ready) so wars still inside their battle day are not re-fetched
        """
        def _extract_ts(fp: str) -> str:
            """Extract YYYYMMDDHHMM timestamp from filename, or '0' if malformed."""
            fn = os.path.basename(fp)
            parts = fn.replace('_war_data.json', '').split('_', 3)
            if len(parts) >= 3:
                ts_candidate = parts[2]  # handles both 3-part and 4-part filenames
                if ts_candidate.isdigit() and len(ts_candidate) == 12:
                    return ts_candidate
            return "0"

        _SINGLE_FILE_ORPHAN_CAP = 5000  # max single-file orphans processed per cycle
        orphans: list[tuple[str, str, str, str, str]] = []  # type: ignore[type-arg]
        now_utc = datetime.now(_tz.utc)
        _ts_re = re.compile(
            r'datetime\.datetime\((\d+),\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)(?:,\s*(\d+))?'
        )

        def _parse_ts_from_str(raw: str) -> Optional[datetime]:
            """Parse coc.py Timestamp repr into UTC datetime.

            Supports both formats observed in temp JSON:
            - datetime.datetime(YYYY, MM, DD, HH, MM)
            - datetime.datetime(YYYY, MM, DD, HH, MM, SS)
            """
            if not raw:
                return None
            m = _ts_re.search(raw)
            if not m:
                return None
            try:
                y, mo, d, h, mi = [int(x) for x in m.groups()[:5]]
                sec_raw = m.group(6)
                sec = int(sec_raw) if sec_raw is not None else 0
                return datetime(y, mo, d, h, mi, sec, tzinfo=_tz.utc)
            except Exception:
                return None

        def _orphan_ready(wf: str) -> bool:
            """True when *wf*'s war ended more than 1h ago (so it can be finalized).

            Used for non-newest files of multi-file clans.  Two tiers, mirroring the
            single-file branch:

            - Tier 1 (zero I/O): the filename timestamp is the war's *start*, and a CWL
              battle day is 24h, so anything younger than 25h cannot yet be a war that
              ended over an hour ago.  On PROD this alone filters ~89% of non-newest
              files without touching disk or the API.
            - Tier 2 (one JSON read): confirm against the recorded end_time, which is the
              only value that accounts for CoC extending a war by a maintenance outage.

            NOTE: CACHE.temp_war_metadata is deliberately *not* consulted — it is keyed by
            clan tag and only ever holds that clan's newest war, so for an older round it
            describes a different war entirely.

            An unreadable file or an unparseable end_time returns True, preserving the
            pre-fix behaviour so a file we cannot interpret is still handed to the orphan
            fetcher rather than being stranded in temp/ forever.
            """
            _fn_ts = _extract_ts(wf)
            if _fn_ts != "0":
                try:
                    _fn_dt = datetime(
                        int(_fn_ts[0:4]), int(_fn_ts[4:6]), int(_fn_ts[6:8]),
                        int(_fn_ts[8:10]), int(_fn_ts[10:12]), tzinfo=_tz.utc,
                    )
                    if now_utc - _fn_dt < timedelta(hours=25):
                        return False
                except ValueError:
                    pass
            try:
                with open(wf, 'r', encoding='utf-8') as _fh:
                    _wo = json.load(_fh)
            except Exception:
                return True
            if _wo.get('state') == 'war_ended':
                # Already final — manage_war_files() finalizes it directly, no API needed.
                return False
            _end_dt = _parse_ts_from_str(str(_wo.get('end_time', '') or ''))
            if _end_dt is None:
                _start_dt = _parse_ts_from_str(str(_wo.get('start_time', '') or ''))
                if _start_dt is not None:
                    _end_dt = _start_dt + timedelta(hours=24)
            if _end_dt is None:
                return True
            return now_utc >= _end_dt + timedelta(hours=1)

        _single_file_count = 0
        for ct, file_paths in _fbclan.items():
            ct_hash = f"#{ct}"
            clan_fetch_failed = ct_hash in _failed

            # Single-file clans: actively tracked clans get a second file when the next war
            # starts (making the old one non-newest = orphan signal).  But passively tracked /
            # discovered clans (CWL group opponents written by cwl_clan_discovery.py)
            # never get a second file, so that signal never fires.  Use end_time + 1h
            # instead: any CWL war whose end_time has passed by more than 1h is done.
            if len(file_paths) <= 1 and not clan_fetch_failed:
                wf = file_paths[0]
                fn = os.path.basename(wf)
                # Gate on filename timestamp before opening the file.
                # CWL wars last at most 24h, so any file younger than 25h
                # cannot have expired yet — skip with zero file I/O.
                fn_ts = _extract_ts(wf)
                if fn_ts != "0":
                    try:
                        # Manual slice+int parse instead of datetime.strptime(): _extract_ts
                        # already guarantees fn_ts is exactly 12 digits (isdigit() + len==12
                        # check above), so strptime's generic format-string matching machinery
                        # (~10-20x slower than direct construction) buys nothing here. This is
                        # the hot path for ~26K single-file clans every cycle (measured
                        # [PHASE-2-TIMING] ~1.4-1.5s even with 0 orphans found).
                        fn_dt = datetime(
                            int(fn_ts[0:4]), int(fn_ts[4:6]), int(fn_ts[6:8]),
                            int(fn_ts[8:10]), int(fn_ts[10:12]), tzinfo=_tz.utc,
                        )
                        if now_utc - fn_dt < timedelta(hours=25):
                            continue  # too recent — still in progress
                    except ValueError:
                        pass
                # File is old enough to be expired — check cached metadata first,
                # fall back to JSON read only when cache is missing.
                _md = CACHE.temp_war_metadata.get(ct_hash)
                _used_cache = False
                if _md and _md.get("war_tag") is not None:
                    # Cached metadata available — use it to avoid disk read
                    state = _md.get("state", "unknown")
                    war_tag = _md.get("war_tag")
                    is_cwl = _md.get("is_cwl", False)
                    if war_tag and is_cwl and state != "war_ended":
                        end_dt = _parse_ts_from_str(str(_md.get("end_time", "") or ""))
                        if end_dt is None:
                            start_dt = _parse_ts_from_str(str(_md.get("start_time", "") or ""))
                            if start_dt is not None:
                                end_dt = start_dt + timedelta(hours=24)
                        if end_dt is not None and now_utc >= end_dt + timedelta(hours=1):
                            orphans.append((war_tag, ct_hash, wf, fn, state))
                            _single_file_count += 1
                            if _single_file_count >= _SINGLE_FILE_ORPHAN_CAP:
                                return orphans
                    _used_cache = True
                if not _used_cache:
                    try:
                        with open(wf, 'r', encoding='utf-8') as fh:
                            war_obj = json.load(fh)
                        state = war_obj.get('state', 'unknown')
                        war_tag = war_obj.get('war_tag')
                        is_cwl = war_obj.get('is_cwl', False)
                        if war_tag and is_cwl and state != 'war_ended':
                            end_dt = _parse_ts_from_str(str(war_obj.get('end_time', '') or ''))
                            if end_dt is None:
                                start_dt = _parse_ts_from_str(str(war_obj.get('start_time', '') or ''))
                                if start_dt is not None:
                                    end_dt = start_dt + timedelta(hours=24)
                            if end_dt is not None and now_utc >= end_dt + timedelta(hours=1):
                                orphans.append((war_tag, ct_hash, wf, fn, state))
                                _single_file_count += 1
                                if _single_file_count >= _SINGLE_FILE_ORPHAN_CAP:
                                    return orphans
                    except Exception:
                        pass
                continue

            # Find newest file using filename timestamp (no stat() calls)
            newest_fp = max(file_paths, key=_extract_ts)

            for wf in file_paths:
                is_newest = (wf == newest_fp)
                if is_newest and not clan_fetch_failed:
                    continue

                fn = os.path.basename(wf)
                # 4-part CWL filename encodes war_tag directly — no JSON read needed.
                # Format: {CLAN}_{OPP}_{TS}_{WAR_TAG}_war_data.json
                # 3-part filenames are regular (non-CWL) wars — not CWL orphans, skip.
                _fn_stem = fn.replace('_war_data.json', '').split('_', 3)
                if len(_fn_stem) != 4:
                    continue
                # "Not the newest file" alone does NOT mean the war is over.
                # CWL rounds overlap: round N+1's preparation file is written while
                # round N is still inside its 24h battle day, which demotes round N's
                # file to non-newest many hours before that war actually ends.  Without
                # a time gate here, every such still-being-fought war is re-fetched via
                # get_league_war() *and* rewritten by save_war_object() on every cycle
                # until it finally ends — then deferred again, because the API correctly
                # keeps reporting it as inWar.  Measured on PROD 2026-09-03: 1,719 of
                # 2,063 non-newest entries (83%) were wars still in progress, and the
                # orphan pass logged 0/2,156 successful for 24h straight while the pool
                # grew linearly.  Apply the same gate the single-file branch uses.
                if not _orphan_ready(wf):
                    continue
                orphans.append((f"#{_fn_stem[3]}", ct_hash, wf, fn, "pre-ended"))
        return orphans

    if failed_clans is None:
        failed_clans = set()

    orphaned_wars = await asyncio.to_thread(_scan_orphans, _files_by_clan, failed_clans)
    
    if not orphaned_wars:
        logging.debug("[ORPHANED-CWL] No orphaned CWL wars found")
        return
    
    logging.info(f"[ORPHANED-CWL] Found {len(orphaned_wars)} orphaned CWL war(s) - fetching final data in parallel")
    
    # Semaphore limits concurrent API calls to avoid throttling (P1a)
    _orphan_sem = asyncio.Semaphore(20)

    # Per-war-tag locks to serialize save_war_object() calls that share the same coc.War object.
    # Background: get_league_war() returns the same cached coc.War instance for both sides of
    # a CWL match. coc.War.attacks / .members are generator-backed properties — two threads
    # iterating them concurrently raises "ValueError: generator already executing".
    # The lock ensures only one asyncio.to_thread(save_war_object) runs at a time per war_tag.
    _war_save_locks: dict[str, asyncio.Lock] = {}

    # Fetch all orphaned wars in parallel
    async def fetch_and_update_orphan(war_tag: str, clan_tag: str, war_file: str, filename: str, old_state: str) -> Tuple[bool, str, str]:
        """Fetch final CWL war data and update JSON file."""
        async with _orphan_sem:
            try:
                # Smart fetch: uses in-memory cache to avoid redundant API calls.
                # - war_ended results are permanently cached → 0 API calls.
                # - Active wars checked within 600s → 0 API calls.
                # - Uncached or stale → 1 API call (result cached for next cycle).
                final_war = await CACHE.get_league_war(war_tag, max_age=600)
                if not final_war or not final_war.clan or not final_war.opponent:  # type: ignore[misc]
                    # API has no data for this war tag (data expired from CoC servers).
                    # If the war started >7 days ago, delete the temp file to break the
                    # infinite retry loop.  Prep-state files have 0 attacks; data loss
                    # is accepted for wars this old.
                    try:
                        _ts_str = filename.replace('_war_data.json', '').split('_')[2]
                        if len(_ts_str) == 12 and _ts_str.isdigit():
                            from datetime import datetime as _dt2, timezone as _tz2, timedelta as _td2
                            _war_start = _dt2.strptime(_ts_str, "%Y%m%d%H%M").replace(tzinfo=_tz2.utc)
                            if _dt2.now(_tz2.utc) - _war_start > _td2(days=7):
                                if os.path.exists(war_file):
                                    os.remove(war_file)
                                    logging.warning(
                                        f"[ORPHANED-CWL] Deleted expired orphan {filename}: "
                                        f"API has no data and war started >7 days ago — data irrecoverably lost"
                                    )
                                return (False, clan_tag, filename)
                    except Exception:
                        pass
                    logging.warning(f"[ORPHANED-CWL] API returned incomplete data for {war_tag} (expired)")
                    return (False, clan_tag, filename)

                # CWL pre-save invariant: ensure war_tag is in cwl_league_rounds before
                # any save_war_object call below.  process_orphaned_cwl_wars operates
                # outside fetch_clan_war_data, so this is the only Layer 2 guard here.
                # Wrapped in to_thread(): sync sqlite3 call, awaited directly inside a
                # asyncio.gather()'d coroutine (up to 20 concurrent, Semaphore-bound) —
                # unwrapped, this blocks the event loop for its I/O duration once per
                # orphaned CWL war, exactly the CWL-season-heavy path flagged for review.
                if CACHE.db_manager is not None and await asyncio.to_thread(
                    CACHE.db_manager.get_cwl_round_for_war_tag_sync, war_tag
                ) is None:
                    try:
                        await CACHE.get_league_group(clan_tag, max_age=300)
                        logging.debug(f"[CWL-ROUNDS] Layer 2 (orphan pre-save): get_league_group for {clan_tag} (war_tag={war_tag})")
                    except Exception as _lg_ex:
                        logging.debug(f"[CWL-ROUNDS] Layer 2 (orphan pre-save): get_league_group failed for {clan_tag}: {_lg_ex}")

                # Check real API state.
                _final_state = str(getattr(final_war, 'state', '') or '').lower().replace('_', '').replace(' ', '')
                if _final_state != 'warended':
                    # War still in progress (e.g. extended due to CoC server maintenance).
                    # Rewrite the JSON with the full fresh API payload so that end_time and
                    # attack snapshots stay current.
                    # NOTE: CoC maintenance can shift start_time forward (the war is extended
                    # by the downtime).  In that case save_war_object writes to a DIFFERENT
                    # filename than the prep-state orphan we are processing — the original
                    # prep file is left behind.  We detect this by re-reading the original
                    # file after the save: if it is still in 'preparation' state the save
                    # wrote elsewhere, so the prep file is stale (0 attacks) and can be
                    # deleted safely.
                    if war_tag not in _war_save_locks:
                        _war_save_locks[war_tag] = asyncio.Lock()
                    async with _war_save_locks[war_tag]:
                        await asyncio.to_thread(
                            CACHE.save_war_object, final_war, clan_tag  # type: ignore[arg-type]
                        )
                    if old_state == 'preparation':
                        try:
                            if os.path.exists(war_file):
                                import json as _json_oae2
                                with open(war_file, 'r', encoding='utf-8') as _fh_oae2:
                                    _cur_state = _json_oae2.load(_fh_oae2).get('state', '')
                                if str(_cur_state).lower().replace(' ', '').replace('_', '') == 'preparation':
                                    os.remove(war_file)
                                    logging.info(
                                        f"[ORPHANED-CWL] Deleted stale prep orphan {filename}: "
                                        f"war now {_final_state} under shifted start_time "
                                        f"(CoC maintenance extension)"
                                    )
                        except Exception:
                            pass
                    logging.debug(
                        f"[ORPHANED-CWL] {filename}: still {_final_state} - refreshed JSON, deferring finalization"
                    )
                    return (False, clan_tag, filename)

                # War has ended — save final data.
                # CRITICAL: save_war_object returns False (FINALIZED-WAR-SKIP) when the war is
                # already in both archive and history. This can happen if the archive was written
                # with stale/incomplete data (e.g. preparation state with 0 attacks) from an
                # earlier cycle. In that case we must force-save so manage_war_files() detects
                # ARCHIVE-DIFFERS, replaces the bad archive, and re-processes DB history.
                # Offloaded to thread to avoid blocking event loop with sync file I/O.
                # Lock serializes both sides of the same CWL match: they share the same cached
                # coc.War object and its generator-backed properties cannot be iterated concurrently.
                if war_tag not in _war_save_locks:
                    _war_save_locks[war_tag] = asyncio.Lock()
                async with _war_save_locks[war_tag]:
                    _saved = await asyncio.to_thread(
                        CACHE.save_war_object, final_war, clan_tag  # type: ignore[arg-type]
                    )
                    if not _saved:
                        _force_saved = await asyncio.to_thread(
                            CACHE.save_war_object, final_war, clan_tag, True  # type: ignore[arg-type]
                        )
                        if not _force_saved:
                            logging.warning(f"[ORPHANED-CWL] Force-save also failed for {filename} (I/O error?) - will retry next cycle")
                            return (False, clan_tag, filename)
                        logging.info(f"[ORPHANED-CWL] Force-saved {filename}: initial save was skipped (war ended >24h ago, stale-war guard); archive created, finalizing")
                logging.info(f"[ORPHANED-CWL] Updated {filename}: {old_state} → {final_war.state}")
                # After a successful finalization, delete the original orphan file if it still
                # exists in non-war_ended state.  This handles start_time drift: CoC sometimes
                # shifts start_time between the prep-phase write (e.g. 08:01) and the
                # war_ended API response (e.g. 09:28), causing save_war_object to write to a
                # different filename and leaving the original prep file permanently stuck.
                try:
                    if os.path.exists(war_file):
                        import json as _json_oae
                        with open(war_file, 'r', encoding='utf-8') as _fh_oae:
                            _orig_state = _json_oae.load(_fh_oae).get('state', '')
                        _norm = str(_orig_state).lower().replace(' ', '').replace('_', '')
                        if _norm != 'warended':
                            os.remove(war_file)
                            logging.info(
                                f"[ORPHANED-CWL] Deleted stale {_orig_state!r}-state orphan {filename}: "
                                f"war finalized under different start_time filename"
                            )
                except Exception:
                    pass
                return (True, clan_tag, filename)
            except WarDataFetchError as api_ex:
                logging.warning(f"[ORPHANED-CWL] API error for {war_tag}: {api_ex}")
                return (False, clan_tag, filename)
    
    # Execute all fetches in parallel
    tasks = [fetch_and_update_orphan(war_tag, clan_tag, war_file, filename, state)  # type: ignore[misc, arg-type]
             for war_tag, clan_tag, war_file, filename, state in orphaned_wars]  # type: ignore[misc]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Count successes
    success_count = sum(1 for r in results if isinstance(r, tuple) and r[0])
    logging.info(f"[ORPHANED-CWL] Parallel fetch complete: {success_count}/{len(orphaned_wars)} successful")  # type: ignore[arg-type]
    
    # Deduplicate finalization by clan — one manage_war_files() call per clan
    # processes ALL that clan's temp files, so calling per-file is wasteful.
    _finalize_clans: Set[str] = set()
    for result in results:
        if not isinstance(result, tuple):
            logging.warning(f"[ORPHANED-CWL] Skipping non-tuple result: {result}")
            continue
        success, clan_tag, _filename = result
        if success:
            _finalize_clans.add(clan_tag)

    # ── Batched sequential finalization (P0 + P1) ──────────────────────
    # Instead of Semaphore(3) parallel manage_war_files() calls that each
    # do their own COMMIT, run all clans sequentially inside a single
    # war_write_batch().  DB writes are collected in-memory and flushed in
    # batched transactions (50 wars/COMMIT) with WAL auto-checkpoint
    # suppressed.  File moves are deferred until after the DB flush.
    #
    # On the PROD HDD this changes ~691 individual commits (each hitting
    # a 5-8 s writeback stall every ~12 writes) into ~14 batched commits,
    # cutting total finalization time from ~7 min to ~30 s.

    def _batch_finalize(clans_to_finalize: Set[str]) -> int:
        """Run all manage_war_files() calls inside a single war_write_batch."""
        done = 0
        with CACHE.db_manager.war_write_batch() as batch:  # type: ignore[union-attr]
            for ct in clans_to_finalize:
                try:
                    _safe_ct = ct.lstrip('#').upper()
                    manage_war_files(
                        ct,
                        war_files_prescan=_files_by_clan.get(_safe_ct),
                        archive_set=archive_set,
                    )
                    logging.info(f"[ORPHANED-CWL] Successfully finalized orphaned war(s) for {ct}")
                    done += 1
                except WarProcessingError as finalize_ex:
                    logging.error(f"[ORPHANED-CWL] Error finalizing war for {ct}: {finalize_ex}")
        # Deferred file moves — only after DB writes succeeded
        _move_count = len(batch.pending_file_moves)
        if _move_count:
            _t0 = time.monotonic()
            for src, dst in batch.pending_file_moves:
                try:
                    os.replace(src, dst)
                except FileNotFoundError:
                    pass
            logging.info(f"[ORPHANED-CWL] Deferred {_move_count} file move(s) completed in {time.monotonic() - _t0:.1f}s")
        return done

    await asyncio.to_thread(_batch_finalize, _finalize_clans)

    return _finalize_clans if _finalize_clans else None
