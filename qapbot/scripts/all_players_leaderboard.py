"""Experimental all-players leaderboard generator and Discord poster.

This script generates a comprehensive leaderboard aggregating statistics from ALL clans
tracked by QapBot, using the war history database and temporary current war files.
It serves as a test bed for leaderboard formatting and rendering improvements.

Features:
- **Multi-clan aggregation**: Combines data from all clans in the war history database
- **Current war integration**: Includes ongoing wars from temp/*_current_war.csv files
- **Flexible display modes**: Toggle between full player list and neighbor-focused view
- **Reference player highlighting**: Marks a specific player (LEADERBOARD_REFERENCE_PLAYER) for comparison
- **Discord message splitting**: Automatically splits large leaderboards into multiple messages
- **Experimental rendering**: Tests new formatting patterns before main project integration

Configuration:
    SHOW_FULL_PLAYER_LIST (bool): 
        - True: Show all players from all clans
        - False: Show only neighbors (±5 ranks) around reference player
    
    VARIANT_REFERENCE_NAME (str): 
        - Player name to use as reference point for neighbor view (from LEADERBOARD_REFERENCE_PLAYER env var)
    
    CHANNEL_ID (int): 
        - Discord channel ID where leaderboard will be posted (from LEADERBOARD_CHANNEL_ID env var)

Data Sources:
1. Database: War history from qapbot.db (completed wars)
2. Temp files: data/temp/*_current_war.csv (ongoing wars)

Statistics Calculated:
- Total stars earned
- Attack count and missed attacks
- Average stars per attack
- Total wars participated in
- Win rate percentage

Usage:
    python all_players_leaderboard.py
    
Requirements:
    - Valid DISCORD_TOKEN_DEV in .env file
    - War history database (data/qapbot.db)
    - Discord bot with MESSAGE_CONTENT intent enabled

Output:
    - Console log of data discovery and processing
    - Discord messages posted to configured channel
    - Multi-part messages if leaderboard exceeds 2000 characters

Note: This is an experimental script for testing. The main project uses
      generate_leaderboard_text() and post_leaderboard_to_discord() from
      QBhelperfunctions.py for production leaderboards.
"""
import os
import csv
import asyncio
import logging
import math
from typing import Dict, Any, List, Optional, Set
import discord
import glob
import fnmatch
# --- ensure project root on sys.path for 'qapbot' imports when running from scripts dir ---
import sys, pathlib
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent  # Go up two levels to project root
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Import database manager and config
from qapbot.db_manager import WarHistoryDB
from qapbot.config import CONFIG

# Reuse existing data models (but NOT the shared formatter; we embed a local experimental copy below)
from qapbot.formatting import MODE_REGISTRY, DEFAULT_MODE, best_practice_player_cell, right_pad_number, text_display_width  # Use main project constants for leaderboard rendering

# Remove import from deprecated qapbot.stats.models
# Define local data classes compatible with main project
from dataclasses import dataclass
from typing import Optional, List, Dict, Any

@dataclass
class LeaderboardMeta:
    clan_tag: str
    month_label: str

@dataclass
class PlayerStats:
    player: str
    player_id: str
    stars: int
    attacks: int
    missed_attacks: int
    defensive_stars: int
    avg_stars: float
    atk_def_ratio: float
    wars_count: int
    def_stars_per_war: float

@dataclass
class WarInfo:
    line: str

@dataclass
class LeaderboardData:
    meta: LeaderboardMeta
    war_info: Optional[WarInfo]
    players: List[PlayerStats]

DATA_DIR = os.path.join(PROJECT_ROOT, 'data')
TEMP_DIR = os.path.join(DATA_DIR, 'temp')
# Support multiple temp naming variants
TEMP_GLOB_PATTERNS = [
    os.path.join(TEMP_DIR, '*_current_wars.csv'),
    os.path.join(TEMP_DIR, '*_current_war.csv'),
    os.path.join(TEMP_DIR, '*current_war*.csv'),  # broader catch-all
]
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

# DEV mode only: always use DEV token
CHANNEL_ID = int(os.getenv('LEADERBOARD_CHANNEL_ID', '0'))  # target Discord channel (set LEADERBOARD_CHANNEL_ID in .env)
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN_DEV')

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

# --- Data Aggregation ---

def prompt_for_input() -> Optional[tuple[str, str]]:
    """Prompt for clan tag or player name.

    Returns:
        ('clan', tag)    — process a specific clan; tag is empty string for all clans
        ('player', name) — char-analysis mode: analyse individual chars of a player name
        None             — user cancelled
    """
    print("\n" + "="*60)
    print("All Players Leaderboard Generator")
    print("="*60)
    user_input = input("\nEnter clan tag (#TAG), player name, or press Enter for ALL clans: ").strip()

    if not user_input:
        confirm = input("\nProcess ALL clans? This may take a while. (Y/N): ").strip().upper()
        if confirm != 'Y':
            print("\nOperation cancelled.")
            return None
        print("\nProcessing ALL clans...\n")
        return ('clan', '')
    elif user_input.startswith('#'):
        clan_tag = user_input.upper()
        print(f"\nProcessing clan: {clan_tag}\n")
        return ('clan', clan_tag)
    else:
        print(f"\nPlayer name mode: analysing chars for '{user_input}'\n")
        return ('player', user_input)

async def _discover_temp_files(clan_tag: Optional[str] = None) -> List[str]:
    """Discover temp war files, optionally filtered by clan tag.
    
    Args:
        clan_tag: Clan tag to filter by (e.g., '#2PP'). If None or empty string, return all files.
    
    Returns:
        List of absolute file paths to temp war CSV files.
    """
    # Handle temp files
    temp_files: List[str] = []
    all_temp_files: List[str] = []
    for pat in TEMP_GLOB_PATTERNS:
        matched = [os.path.abspath(p) for p in glob.glob(pat) if os.path.isfile(p)]
        all_temp_files.extend(matched)
    
    # Filter temp files by clan tag if specified
    if clan_tag:
        clan_tag_normalized = clan_tag.lstrip('#').upper()
        temp_files = [
            f for f in all_temp_files
            if clan_tag_normalized in os.path.basename(f).upper()
        ]
    else:
        temp_files = all_temp_files
    
    if temp_files:
        # Log pattern matching for filtered results
        for pat in TEMP_GLOB_PATTERNS:
            matched_count = sum(1 for f in temp_files if fnmatch.fnmatch(f, pat))
            if matched_count > 0:
                logging.debug(f"Temp pattern {pat} matched {matched_count} file(s)")
        # De-duplicate temp list first
        temp_files = sorted(set(temp_files))
        logging.info(f"Found {len(temp_files)} temp current war file(s) across {len(TEMP_GLOB_PATTERNS)} pattern(s)")
    else:
        logging.info("No temp current war files found for any configured pattern")
    
    # Per-file debug listing
    for f in temp_files:
        logging.debug(f"DATA_FILE [temp]    {f}")
    
    return temp_files

async def load_all_history_rows(db: WarHistoryDB, clan_tag: Optional[str] = None) -> List[Dict[str, Any]]:
    """Load history rows from database, optionally filtered by clan tag.
    
    Args:
        db: Database manager instance
        clan_tag: Clan tag to filter by. If None or empty, load all clans.
    
    Returns:
        List of row dictionaries from database.
    """
    rows: List[Dict[str, Any]] = []
    
    if clan_tag:
        # Load specific clan
        logging.info(f"Loading history for clan: {clan_tag}")
        clan_rows = await db.get_clan_attack_history(clan_tag)
        rows.extend(clan_rows)
        logging.info(f"Loaded {len(clan_rows)} rows for {clan_tag}")
    else:
        # Load all clans from database
        logging.info("Loading history for all clans from database...")
        all_clans = await db.get_all_war_clan_tags()
        logging.info(f"Found {len(all_clans)} clans in database")
        
        for clan in all_clans:
            clan_rows = await db.get_clan_attack_history(clan)
            rows.extend(clan_rows)
            logging.debug(f"Loaded {len(clan_rows)} rows for {clan}")
        
        logging.info(f"Aggregated {len(rows)} total history rows from {len(all_clans)} clan(s)")
    
    # Load temp files for current wars
    temp_files = await _discover_temp_files(clan_tag)
    if temp_files:
        logging.info(f"Loading {len(temp_files)} temp war file(s)...")
        for path in temp_files:
            try:
                with open(path, 'r', encoding='utf-8', newline='') as f:
                    reader = csv.DictReader(f)
                    row_count = 0
                    for r in reader:
                        rows.append(r)
                        row_count += 1
                logging.debug(f"Loaded {row_count} rows from {os.path.basename(path)}")
            except Exception as e:
                logging.error(f"Failed reading temp file {path}: {e}")
    
    return rows


def aggregate_player_stats(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]: # For main project compatibility
    agg: Dict[str, Dict[str, Any]] = {}
    wars_by_player: Dict[str, Set[str]] = {}
    for r in rows:
        pid = r.get('PlayerID') or r.get('Player')
        if not pid:
            continue
        wars_by_player.setdefault(pid, set())
        war_id = r.get('WarID') or r.get('WarId')
        if war_id:
            wars_by_player[pid].add(war_id)
        cur = agg.setdefault(pid, {
            'Player': r.get('Player',''),
            'PlayerID': r.get('PlayerID',''),
            'Stars': 0,
            'Attacks': 0,
            'Missed_Attacks': 0,
            'Defensive_Stars': 0
        })
        # Update latest display name if date newer (optional future enhancement)
        try:
            cur['Stars'] += int(r.get('Stars', 0))
        except Exception:
            pass
        try:
            cur['Attacks'] += int(r.get('Attacks', 0))
        except Exception:
            pass
        try:
            cur['Missed_Attacks'] += int(r.get('Missed_Attacks', 0))
        except Exception:
            pass
        try:
            cur['Defensive_Stars'] += int(r.get('Defensive_Stars', 0))
        except Exception:
            pass
    # Derived
    for pid, data in list(agg.items()):
        wars = len(wars_by_player.get(pid, set()))
        data['Wars_Count'] = wars
        data['Def_Stars_per_War'] = (data['Defensive_Stars'] / wars) if wars else 0.0
    return agg

# --- Rendering ---

_ref_qaplop_stats: Optional[PlayerStats] = None  # captured original reference-player stats
# Config toggle: True for full player list, False for shortened neighbor list
SHOW_FULL_PLAYER_LIST = False

def build_leaderboard_text(agg: Dict[str, Dict[str, Any]]) -> str: # For main project compatibility, taken from generate_leaderboard_text
    global _ref_qaplop_stats
    import re, unicodedata
    ZERO_WIDTH_PATTERN = re.compile(r"[\u200B-\u200F\u202A-\u202E\u2060\uFE0E\uFE0F\uFEFF]")

    def has_special(name: str) -> bool:
        if not name:
            return False
        nfkc = unicodedata.normalize('NFKC', name)
        if nfkc != name:
            return True  # normalization changed (e.g., small caps, compatibility forms)
        for ch in name:
            if not ch.isascii():
                return True  # any non-ASCII
            cat = unicodedata.category(ch)
            if (cat == 'Cf') or (cat == 'Zs' and ch != ' '):
                return True  # format or atypical separator
        if ZERO_WIDTH_PATTERN.search(name):
            return True  # zero-width / variation selectors
        return False

    # Build full list first
    full_players: List[PlayerStats] = []
    for entry in agg.values():
        name = entry.get('Player','')
        attacks = entry.get('Attacks', 0)
        stars = entry.get('Stars', 0)
        defensive = entry.get('Defensive_Stars', 0)
        wars_count = entry.get('Wars_Count', 0)
        def_per_war = entry.get('Def_Stars_per_War', 0.0)
        avg = (stars / attacks) if attacks else 0.0
        atk_def_ratio = (stars / defensive) if defensive else math.inf
        full_players.append(PlayerStats(
            player=name,
            player_id=entry.get('PlayerID',''),
            stars=stars,
            attacks=attacks,
            missed_attacks=entry.get('Missed_Attacks',0),
            defensive_stars=defensive,
            avg_stars=avg,
            atk_def_ratio=atk_def_ratio,
            wars_count=wars_count,
            def_stars_per_war=def_per_war
        ))
    if not full_players:
        return "```No player data found.```"

    # Sort full list using standard ordering
    full_players.sort(key=lambda p: (-p.stars, p.attacks, p.player.lower()))

    if SHOW_FULL_PLAYER_LIST:
        filtered_players = full_players
        meta = LeaderboardMeta(clan_tag='#ALL', month_label='ALL-TIME (FULL LIST)')
        war_info = None
    else:
        # After sorting full_players determine reference-player stats
        qaplop_entry = next((p for p in full_players if p.player == VARIANT_REFERENCE_NAME), None)
        if qaplop_entry:
            _ref_qaplop_stats = qaplop_entry
        # Identify special indices
        special_indices = {i for i, p in enumerate(full_players) if has_special(p.player)}
        if not special_indices:
            return "```No players with special characters detected (nothing to compare).```"
        # Include immediate neighbors for comparison
        neighbor_indices: Set[int] = set()
        for i in special_indices:
            if i - 1 >= 0:
                neighbor_indices.add(i - 1)
            if i + 1 < len(full_players):
                neighbor_indices.add(i + 1)
        selected_indices = sorted(special_indices | neighbor_indices)
        filtered_players = [full_players[i] for i in selected_indices]
        # Ensure reference player present for comparison
        if qaplop_entry and all(p.player != VARIANT_REFERENCE_NAME for p in filtered_players):
            filtered_players.append(qaplop_entry)
        meta = LeaderboardMeta(clan_tag='#ALL', month_label='ALL-TIME (SPECIAL+NEIGHBORS)')
        war_info = None
    lb_data = LeaderboardData(meta=meta, war_info=war_info, players=filtered_players)
    return render_leaderboard(lb_data, mode='stars', style='discord')

# --- Message Splitting (reuse logic style) ---

def split_leaderboard_for_discord(full_text: str) -> List[str]: # Message-Split from main project, taken from post_leaderboard_to_discord
    text = full_text.strip('`').strip()
    lines = text.split('\n')
    header_lines: List[str] = []
    table_lines: List[str] = []
    found_table = False
    for line in lines:
        if not found_table and 'Player' in line and 'Stars' in line and 'Attacks' in line:
            found_table = True
            table_lines.append(line)
        elif found_table:
            table_lines.append(line)
        else:
            header_lines.append(line)
    if not found_table:
        # Simple fallback split
        mid = len(lines)//2
        return ['```'+ '\n'.join(lines[:mid]) +'```', '```'+ '\n'.join(lines[mid:]) +'```'] if len(text)>2000 else ['```'+text+'```']
    header_content = '\n'.join(header_lines).strip()
    table_header = table_lines[0]
    table_sep = table_lines[1] if len(table_lines)>1 else ''
    player_lines = table_lines[2:] if len(table_lines)>2 else []
    parts: List[str] = []
    code_overhead = 6
    safety = 50
    # First chunk size baseline
    first_players: List[str] = []
    size = code_overhead + safety + len(table_header)+len(table_sep)+2 + (len(header_content) if header_content else 0)
    if header_content:
        size += 1
    for pl in player_lines:
        ls = len(pl)+1
        if size + ls > 2000:
            break
        first_players.append(pl)
        size += ls
    first_parts: List[str] = []
    if header_content:
        first_parts.append(header_content)
    first_parts.append('')
    first_parts.append(table_header)
    if table_sep:
        first_parts.append(table_sep)
    first_parts.extend(first_players)
    parts.append('```'+'\n'.join(first_parts)+'```')
    remaining = player_lines[len(first_players):]
    while remaining:
        chunk: List[str] = []
        size = code_overhead + safety
        for pl in remaining:
            ls = len(pl)+1
            if size + ls > 2000:
                break
            chunk.append(pl)
            size += ls
        if not chunk:
            chunk = [remaining[0][:1900]+'...']
            remaining = remaining[1:]
        else:
            remaining = remaining[len(chunk):]
        parts.append('```'+'\n'.join(chunk)+'```')
    return parts if (len(parts) > 1 or len(parts[0]) > 2000) else parts

# --- Discord Posting ---

def build_player_char_test_text(player_name: str) -> str:
    """Build a Discord test message with testlines for each unique char in player_name.

    Each unique non-space character gets one line of 30 repetitions between pipe markers,
    followed by the 81-pipe reference line — matching the existing testline format so the
    user can visually measure rendered widths against the ruler.

    Args:
        player_name: Raw player name (not normalized) to analyse
    Returns:
        str: Discord code block ready to post
    """
    import unicodedata
    from qapbot.formatting import text_display_width_float

    digit_line = "|" + "1234567890" * 8
    pipe_line = "|" * 81

    test_lines: List[str] = [
        f"Char analysis for: {player_name}",
        "",
        digit_line,
        pipe_line,
    ]

    seen: set[str] = set()
    for ch in player_name:
        if ch in seen or ch == ' ':
            continue
        seen.add(ch)
        w = text_display_width_float(ch)
        try:
            ch_name = unicodedata.name(ch)
        except ValueError:
            ch_name = "(no name)"
        label = f"U+{ord(ch):04X}  {ch_name}  (assumed={w:.3f})"
        test_lines.append(f"|{ch * 30}| {label}")
        test_lines.append(pipe_line)

    return '```' + "\n".join(test_lines) + '```'


async def post_all_players_leaderboard(clan_tag: Optional[str] = None, player_name: Optional[str] = None) -> None:
    """Generate and post leaderboard for specified clan tag or all clans,
    or run char-analysis for a player name.

    Args:
        clan_tag: Clan tag to filter by. If None or empty, process all clans.
                  Ignored when player_name is set.
        player_name: If set, skip leaderboard and post char-analysis testlines
                     for each unique char in this name instead.
    """
    if not DISCORD_TOKEN:
        logging.error('DISCORD_TOKEN not set in environment.')
        return

    # Only initialise the database when we actually need leaderboard data
    db: Optional[WarHistoryDB] = None
    if not player_name:
        logging.info("Initializing database...")
        db = WarHistoryDB()
        try:
            await db.initialize(CONFIG.db_path)
            logging.info(f"✅ Database initialized at {CONFIG.db_path}")
        except Exception as e:
            logging.error(f"❌ Failed to initialize database: {e}")
            return
    
    intents = discord.Intents.default()
    intents.guilds = True
    intents.message_content = True  # Enable privileged message content intent for command/message access
    client = discord.Client(intents=intents)

    async def _resolve_channel(cid: int):
        ch = client.get_channel(cid)
        if ch is None:
            logging.debug(f"Channel {cid} not in cache; attempting fetch...")
            try:
                ch = await client.fetch_channel(cid)
            except discord.Forbidden:
                logging.error(f"Forbidden to access channel {cid}.")
            except discord.NotFound:
                logging.error(f"Channel {cid} not found (NotFound).")
            except Exception as e:
                logging.error(f"Unexpected error fetching channel {cid}: {e}")
        return ch

    # Event set by on_ready when all work is done; lets the outer coroutine
    # drive shutdown without relying on client.start() returning cleanly.
    work_done = asyncio.Event()

    @client.event
    async def on_ready():  # type: ignore
        try:
            logging.info(f'Logged in as {client.user} - preparing output...')
            if player_name:
                # Char-analysis mode: post testlines for each unique char in the player name
                text = build_player_char_test_text(player_name)
                parts = split_leaderboard_for_discord(text)
            else:
                assert db is not None
                rows = await load_all_history_rows(db, clan_tag)
                if not rows:
                    logging.warning('No history rows found; nothing to post.')
                    print("=== ALL-PLAYERS LEADERBOARD (NO DATA) ===")
                    return
                agg = aggregate_player_stats(rows)
                if not agg:
                    logging.warning('Aggregation produced no player stats; nothing to post.')
                    print("=== ALL-PLAYERS LEADERBOARD (NO PLAYER STATS) ===")
                    return
                text = build_leaderboard_text(agg)
                parts = split_leaderboard_for_discord(text)
            channel = await _resolve_channel(CHANNEL_ID)
            if channel is None:
                logging.error(f'Channel {CHANNEL_ID} not found or inaccessible after fetch attempt.')
            else:
                logging.info(f'Posting in {len(parts)} part(s) to channel {CHANNEL_ID}...')
                for idx, part in enumerate(parts, start=1):
                    print(f"\n=== DISCORD MESSAGE PART {idx}/{len(parts)} (len={len(part)}) ===")
                    print(part)
                    await channel.send(part)  # type: ignore[union-attr]
                    logging.info(f'Sent part {idx}/{len(parts)} (len={len(part)})')
        except Exception as e:
            logging.exception(f'Unexpected error during posting: {e}')
        finally:
            work_done.set()

    # Run client.start() as a background task so we can drive shutdown ourselves.
    # client.start() on Windows often doesn't return after client.close() is called
    # (discord.py's internal reconnect loop keeps it alive), so we can't simply await it.
    start_task = asyncio.ensure_future(client.start(DISCORD_TOKEN))
    try:
        await work_done.wait()
    finally:
        # Close the Discord client first (stops the websocket / reconnect loop)
        if not client.is_closed():
            try:
                await client.close()
            except Exception:
                pass
        # Give start_task a moment to notice the client is closed and return
        try:
            await asyncio.wait_for(start_task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            start_task.cancel()
            try:
                await start_task
            except (asyncio.CancelledError, Exception):
                pass
        # Cancel any remaining tasks (aiohttp keep-alives etc.) so asyncio.run() exits cleanly
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        # Close database connection if it was opened
        if db is not None:
            await db.close()
        logging.info('Client and database closed; exiting script.')

# Keep reference name constant for variant comparisons
VARIANT_REFERENCE_NAME = os.getenv('LEADERBOARD_REFERENCE_PLAYER', '')  # set LEADERBOARD_REFERENCE_PLAYER in .env

def render_leaderboard(data: LeaderboardData, mode: Optional[str] = None, style: str = 'discord') -> str:
    """
    Render a complete leaderboard using MODE_REGISTRY and DEFAULT_MODE from main project.
    Uses best-practice cell padding and normalization for player names.
    Args:
        data (LeaderboardData): Leaderboard data object
        mode (str): Leaderboard mode identifier (e.g., "attack", "avgstars", "defense")
        style (str): Output style - "discord" (code blocks) or "terminal" (plain text)
    Returns:
        str: Formatted leaderboard text ready for display
    """
    mode = (mode or DEFAULT_MODE).lower()
    spec = MODE_REGISTRY.get(mode, MODE_REGISTRY[DEFAULT_MODE])
    cols = spec["columns"]
    sort_key = spec["sort_key"]
    header_label_map = {
        "attack": "Attack Leaderboard",
        "avgstars": "Average Stars per Attack Leaderboard",
        "attackdefratio": "Attack/Defense-Ratio Leaderboard",
        "missedattacks": "Missed Attacks Leaderboard",
        "defense": "Defense (Fewest Defensive Stars per War) Leaderboard",
        "currentwar": "Current War"
    }
    header_title = header_label_map.get(mode, mode.title())
    meta = data.meta
    lines: List[str] = []
    lines.append(f"⭐ {meta.clan_tag} - {header_title} ({meta.month_label}):")
    lines.append("")
    lines.append(data.war_info.line if data.war_info else "_No current war info available._")
    lines.append("")
    # Header cells
    header_cells: List[str] = []
    for idx, (col, w) in enumerate(cols):
        cur = text_display_width(col)
        if idx == 0:
            if cur < w:
                col = col + " " * (w - cur)
        else:
            if cur < w:
                col = " " * (w - cur) + col
        header_cells.append(col)
    lines.append(" ".join(header_cells))
    sep = " ".join(['-' * w for _, w in cols])
    lines.append(sep)
    # Sort players using sort_key from MODE_REGISTRY, adapting for dataclass attributes
    players = list(data.players)
    def _adapted_sort_key(p: PlayerStats) -> Any:
        # If p is a dict, use as is; if dataclass, use attributes
        try:
            return sort_key(p)
        except AttributeError:
            # Build a dict-like view for the lambda
            return sort_key({
                "Player": getattr(p, "player", ""),
                "Stars": getattr(p, "stars", 0),
                "Attacks": getattr(p, "attacks", 0),
                "Missed_Attacks": getattr(p, "missed_attacks", 0),
                "Defensive_Stars": getattr(p, "defensive_stars", 0),
                "Wars_Count": getattr(p, "wars_count", 0),
                "Def_Stars_per_War": getattr(p, "def_stars_per_war", 0.0)
            })
    players.sort(key=_adapted_sort_key)
    base_player_width = cols[0][1]
    for p in players:
        player_cell = best_practice_player_cell(p.player, base_player_width)
        # Build row_parts based on mode
        if mode == "missedattacks":
            row_parts = [
                player_cell,
                right_pad_number(p.missed_attacks, cols[1][1]),
                right_pad_number(p.attacks, cols[2][1]),
                right_pad_number(p.stars, cols[3][1]),
                right_pad_number(p.defensive_stars, cols[4][1])
            ]
        elif mode == "attackdefratio":
            defensive = p.defensive_stars
            ratio = f"{(p.stars / defensive):.2f}" if defensive else "-"
            row_parts = [
                player_cell,
                right_pad_number(p.stars, cols[1][1]),
                right_pad_number(p.attacks, cols[2][1]),
                right_pad_number(f"{p.avg_stars:.2f}", cols[3][1]),
                right_pad_number(defensive, cols[4][1]),
                right_pad_number(ratio, cols[5][1])
            ]
        elif mode == "defense":
            row_parts = [
                player_cell,
                right_pad_number(p.stars, cols[1][1]),
                right_pad_number(p.attacks, cols[2][1]),
                right_pad_number(p.defensive_stars, cols[3][1]),
                right_pad_number(p.wars_count, cols[4][1]),
                right_pad_number(f"{p.def_stars_per_war:.2f}", cols[5][1])
            ]
        else:  # attack, avgstars, currentwar, etc.
            avg = p.avg_stars
            row_parts = [
                player_cell,
                right_pad_number(p.stars, cols[1][1]),
                right_pad_number(p.attacks, cols[2][1]),
                right_pad_number(f"{avg:.2f}", cols[3][1]),
                right_pad_number(p.defensive_stars, cols[4][1])
            ]
        lines.append(" ".join(row_parts))
    body = "\n".join(lines)
    # Preserve testlines logic for visual alignment
    testline_chars = {
        '\u3000': 1.675,    # Ideographic Space (　)
        '\u2800': 1.125,    # Braille Pattern Blank
        '\u0020': 1.00,     # SPACE (regular space)
        #'\u4E09': 1.66,    # 三
        #'\u30C4': 1.66,    # ツ
        #'\uC9C0': 1.66,    # 지
        #'\uBBFC': 1.66,    # 민
        #'☠': 1.00,         # U+2730 (Star)
        #'✌': 1.00,         # U+15F7 (Latin Letter B)
        #'✌️': 1.00,         # U+15F7 (Latin Letter B)
        #'ᗩ': 1.00,         # U+15F9 (Latin Letter A)
        #'ᑎ': 1.00,         # U+145E (Latin Letter N)
        #'ᘜ': 1.00,         # U+161C (Latin Letter G)
        # Small capital letters from "ᴄᴏɴʀᴀᴛᴜʀɴ"
        # Crown/Queen symbol alternatives (♛ U+265B renders inconsistently iPad vs PC)
        # --- Unhandled chars from BrawlerBaller CWL names (need empirical measurement) ---
        '✯': 1.367,          # U+2782 DINGBAT NEGATIVE CIRCLED DIGIT THREE - Dingbats block (0x2700-0x27BF), cat=No, not in emoji ranges
        'ø': 1.267,          # U+2746 SNOWFLAKE - Dingbats block (0x2700-0x27BF), cat=So, not in emoji ranges
        '✞': 1.083,          # U+271E LATIN CROSS - Dingbats block (0x2700-0x27BF), cat=So, not in emoji ranges
        '⚝': 1.467,          # U+269D OUTLINED WHITE STAR - Dingbats block (0x2700-0x27BF), cat=So, not in emoji ranges
        '➳': 1.34,          # U+2793 HEAVY WIDE-HEADED RIGHTWARDS ARROW - Dingbats block (0x2700-0x27BF), cat=So, not in emoji ranges
        # Additional Unicode space characters for empirical testing
    }
    # Clan: #L2J0C0PY , #2RQ9PPPYP
    digit_line = "|" + "1234567890" * 8  # 80 digits
    pipe_line = "|" * 81
    lightning_line = '|' + ('\u26A1' * 30) + '|'

    # Generate a 30-digit test line for each unicode space, with the name at the end
    space_test_lines: List[str] = []
    for code, _width in testline_chars.items():
        import unicodedata
        if len(code) == 1:
            name = unicodedata.name(code, f"U+{ord(code):04X}")
        else:
            # Multi-char: join names or fallback to codepoints
            try:
                name = '+'.join(unicodedata.name(c, f"U+{ord(c):04X}") for c in code)
            except Exception:
                name = ' '.join(f"U+{ord(c):04X}" for c in code)
        line = "|" + (code * 30) + "|" + " " + name
        space_test_lines.append(line)
        space_test_lines.append(pipe_line)

    test_lines = [
        digit_line + "\n" + pipe_line,
        lightning_line,
        pipe_line
    ] + space_test_lines
    #test_lines = "" # This de-activates visual test lines
    body += "\n" + "\n".join(test_lines)
    return '```' + body + '```'

# Append / restore entry point
if __name__ == '__main__':
    result = prompt_for_input()
    if result is None:
        import sys
        sys.exit(0)

    input_type, input_value = result
    if input_type == 'player':
        asyncio.run(post_all_players_leaderboard(player_name=input_value))
    else:
        asyncio.run(post_all_players_leaderboard(clan_tag=input_value if input_value else None))
