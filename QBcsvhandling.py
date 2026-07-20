"""
War data persistence utilities for JSON war files and history management.

NOTE: All functions in this module are intended to be called ONLY by qapbot/cache_manager.py
or QBhelperfunctions.py. Do not use these functions directly from business logic or command handlers.

Key Features:
- Load war statistics from JSON temp files (data/temp/)
- Append finalized war data to SQLite database history
- Update history with late attack data
- Archive JSON files after successful database writes
- Defensive error handling for file I/O and data parsing

Integration:
- Used by qapbot/cache_manager.py for loading temp war stats
- Used by QBhelperfunctions.py for war finalization and late attack updates
- Not to be called directly from business logic or command handlers
"""
import os
from typing import TypedDict, Dict, Optional, Any, List, Tuple, Set, cast
import logging
import json as _json
import re as _re
from qapbot.constants import normalize_cwl_season

_PROD_BASE = os.getenv("PROD_DATA_DIR", "")
try:
    _IS_DEV = int(os.getenv("DISCORD_GUILD_ID", "0")) > 0
except ValueError:
    _IS_DEV = False  # type: ignore[misc]
DATA_DIR = os.path.join(_PROD_BASE, "data") if (_PROD_BASE and not _IS_DEV) else "data"
ARCHIVE_DIR = os.path.join(_PROD_BASE, "archive") if (_PROD_BASE and not _IS_DEV) else "archive"
TEMP_DIR = os.path.join(DATA_DIR, "temp")
os.makedirs(TEMP_DIR, exist_ok=True)

import bisect as _bisect

# ---------------------------------------------------------------------------
# Directory sharding — 10 balanced subdirs for data/temp/ and archive/
# ---------------------------------------------------------------------------
# Upper bounds (inclusive) for each shard, keyed on the 3-char clan-tag prefix.
# Computed from 484,532 files across temp + archive + archive_old (±1% balance).
# Lookup: bisect_left(_SHARD_UPPER_BOUNDS, safe_tag[:3].upper()) → shard index.
_SHARD_UPPER_BOUNDS: List[str] = [
    "2C9",  # shard_0: 200 – 2C9  (~48,647 files, 10.0%)
    "2GY",  # shard_1: 2CC – 2GY  (~46,987 files,  9.7%)
    "2LG",  # shard_2: 2J0 – 2LG  (~49,568 files, 10.2%)
    "2QC",  # shard_3: 2LJ – 2QC  (~48,328 files, 10.0%)
    "2RQ",  # shard_4: 2QG – 2RQ  (~49,086 files, 10.1%)
    "8G9",  # shard_5: 2RR – 8G9  (~48,076 files,  9.9%)
    "CJ9",  # shard_6: 8GC – CJ9  (~48,462 files, 10.0%)
    "LY9",  # shard_7: CJC – LY9  (~48,503 files, 10.0%)
    "RJJ",  # shard_8: LYC – RJJ  (~48,417 files, 10.0%)
    "YYY",  # shard_9: RJL – YYY  (~48,458 files, 10.0%)
]
_SHARD_COUNT = 10


def get_war_shard_dir(safe_tag: str, base_dir: str) -> str:
    """Return the shard subdirectory path for a given clan tag and base directory.

    Args:
        safe_tag: Clan tag without '#', uppercase (e.g. '2R0GYVLJJ').
        base_dir: Root directory ('data/temp' or 'archive').

    Returns:
        Full path to the shard subdir (e.g. 'data/temp/shard_5').
    """
    pfx = safe_tag[:3].upper()
    idx = min(_bisect.bisect_left(_SHARD_UPPER_BOUNDS, pfx), _SHARD_COUNT - 1)
    return os.path.join(base_dir, f"shard_{idx}")


def migrate_war_files_to_shards() -> tuple[int, int]:
    """Migrate flat-layout war JSON files in temp/ and archive/ to sharded subdirs.

    Uses os.replace() — atomic rename on the same filesystem (HDD/server-machine), no data
    copy needed.  Safe to run repeatedly: only direct-child .json files are moved;
    files already inside shard_* subdirs are invisible to the root scandir.

    Returns:
        (temp_moved, archive_moved): count of files migrated per directory.
    """
    import logging as _log
    temp_moved = 0
    archive_moved = 0
    for root_dir, label in [
        (TEMP_DIR, "temp"),
        (ARCHIVE_DIR, "archive"),
    ]:
        if not os.path.isdir(root_dir):
            continue
        try:
            entries = list(os.scandir(root_dir))
        except OSError as _e:
            _log.warning(f"[SHARD-MIGRATION] Cannot scan {root_dir}: {_e}")
            continue
        for entry in entries:
            if not entry.is_file() or not entry.name.endswith("_war_data.json"):
                continue
            safe_tag = entry.name.split("_")[0]
            shard_dir = get_war_shard_dir(safe_tag, root_dir)
            try:
                os.makedirs(shard_dir, exist_ok=True)
                os.replace(entry.path, os.path.join(shard_dir, entry.name))
                if label == "temp":
                    temp_moved += 1
                else:
                    archive_moved += 1
            except Exception as _mv_ex:
                _log.warning(f"[SHARD-MIGRATION] Failed to move {entry.name} → {shard_dir}: {_mv_ex}")
    return temp_moved, archive_moved

class WarStatsDict(TypedDict):
    """
    TypedDict for war statistics row in CSV files.
    
    Fields:
        WarID (str): Unique war identifier
        Date (str): War start date in ISO format
        Player (str): Player display name
        PlayerID (str): Permanent player identifier
        TH_lvl (int): Townhall level (0-18, 0=unknown)
        Stars (int): War stars earned
        Attacks (int): Number of attacks performed
        Missed_Attacks (int): Number of attacks not used
        Max_Attacks (int): Maximum attacks allowed per player
        Defensive_Stars (int): Defensive stars conceded
    """
    WarID: str
    Date: str
    Player: str
    PlayerID: str
    TH_lvl: int
    Stars: int
    Attacks: int
    Missed_Attacks: int
    Max_Attacks: int
    Defensive_Stars: int
    Times_Defended: int


# ---------------------------------------------------------------------------
# Parse per-attack rows + war_summary from a JSON war-data dict
# ---------------------------------------------------------------------------

_TS_RE = _re.compile(r'datetime\.datetime\(([^)]+)\)')


def _parse_start_time(raw: str) -> Tuple[Optional[str], Optional[str], Optional[int], Optional[int], Optional[int]]:
    """Return (war_id_timestamp, iso_date, year, month, day) or (None,…)×5."""
    m = _TS_RE.search(raw)
    if not m:
        return None, None, None, None, None
    parts = [int(x.strip()) for x in m.group(1).split(',')]
    if len(parts) < 5:
        return None, None, None, None, None
    y, mo, d, h, mi = parts[:5]
    return (
        f"{y:04d}{mo:02d}{d:02d}{h:02d}{mi:02d}",
        f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}",
        y, mo, d,
    )


def build_per_attack_rows(war_data: Any, clan_tag: str,
                          *, for_finalization: bool = False,
                          include_missed_sentinels: bool = True
                          ) -> List[Dict[str, Any]]:
    """
    Build one row per attack from a JSON war-data dict.

    A player with 0 attacks gets a sentinel row with ``attack_order = 0`` when
    ``include_missed_sentinels`` is True.

    For incomplete in-war snapshots that are finalized as orphaned regular wars,
    callers can set ``for_finalization=False`` and ``include_missed_sentinels=False``
    so only real attacks are persisted and no speculative missed-attacks are stored.

    Returns a flat list of dicts ready for ``db_manager.add_war_attack_records_sync``.
    """
    ts_compact, date_iso, *_ = _parse_start_time(str(war_data.get('start_time', '')))
    if not ts_compact:
        return []

    opponent: Any = war_data.get('opponent', {}) or {}
    opp_tag = (opponent.get('tag') or 'UNK').lstrip('#')
    war_id = f"{opp_tag}_{ts_compact}"
    attacks_per_member: int = int(war_data.get('attacks_per_member', 2) or 2)
    war_state = str(war_data.get('state', '')).lower()

    clan_data: Any = war_data.get('clan', {}) or {}
    members: Any = clan_data.get('members', [])

    # Build defender-TH and defender-map-position lookup from both sides
    # (attacker is always on our clan side, defender is always on opponent side)
    opponent_members: Any = (war_data.get('opponent', {}) or {}).get('members', []) or []  # type: ignore[union-attr]
    own_members_list: Any = members or []
    _def_th_map: Dict[str, int] = {}
    _def_pos_map: Dict[str, int] = {}
    for _side in (opponent_members, own_members_list):  # type: ignore[union-attr]
        for _m in _side:  # type: ignore[union-attr]
            _t = str(_m.get('tag') or '')  # type: ignore[union-attr, arg-type]
            if _t:
                _def_th_map[_t] = int(_m.get('townhall', 0) or 0)  # type: ignore[union-attr, arg-type]
                _def_pos_map[_t] = int(_m.get('map_position', 0) or 0)  # type: ignore[union-attr, arg-type]

    rows: List[Dict[str, Any]] = []
    for member in members:
        p_tag = member.get('tag', '')
        p_name = member.get('name', 'Unknown')
        th = int(member.get('townhall', 0) or 0)
        atks: Any = member.get('attacks', [])
        num_attacks = len(atks)

        if for_finalization or war_state == 'war_ended':
            missed = max(attacks_per_member - num_attacks, 0)
        else:
            missed = 0

        best_opp: Any = member.get('bestOpponentAttack')
        def_stars = int(best_opp.get('stars', 0) if hasattr(best_opp, 'get') else 0)
        best_def_destr = float(best_opp.get('destruction', 0.0) if hasattr(best_opp, 'get') else 0.0)
        times_defended = int(member.get('opponentAttacks', 0) or 0)

        if atks:
            for atk in atks:
                _dtag = str(atk.get('defenderTag', '') or '')
                _raw_fresh = atk.get('fresh')
                rows.append({
                    "WarID": war_id, "Date": date_iso,
                    "Player": p_name, "PlayerID": p_tag, "TH_lvl": th,
                    "map_position": int(member.get('map_position', 0) or 0),
                    "attack_order": int(atk.get('order', 0) or 0),
                    "stars": int(atk.get('stars', 0) or 0),
                    "destruction": float(atk.get('destruction', 0.0) or 0.0),
                    "defender_tag": _dtag,
                    "defender_th": _def_th_map.get(_dtag, 0),
                    "defender_map_position": _def_pos_map.get(_dtag, 0),
                    "duration": int(atk.get('duration', 0) or 0),
                    "is_fresh": (1 if _raw_fresh else (0 if _raw_fresh is False else -1)),
                    "times_defended": times_defended,
                    "best_def_destruction": best_def_destr,
                    "Max_Attacks": attacks_per_member,
                    "Missed_Attacks": missed,
                    "Defensive_Stars": def_stars,
                })
        elif include_missed_sentinels:
            # Sentinel row: player had no attacks
            rows.append({
                "WarID": war_id, "Date": date_iso,
                "Player": p_name, "PlayerID": p_tag, "TH_lvl": th,
                "map_position": int(member.get('map_position', 0) or 0),
                "attack_order": 0,
                "stars": 0, "destruction": 0.0, "defender_tag": "",
                "defender_th": 0, "defender_map_position": 0,
                "duration": 0, "is_fresh": -1,
                "times_defended": times_defended,
                "best_def_destruction": best_def_destr,
                "Max_Attacks": attacks_per_member,
                "Missed_Attacks": missed,
                "Defensive_Stars": def_stars,
            })

    return rows


def build_war_summary(war_data: Any, clan_tag: str) -> Optional[Dict[str, Any]]:
    """
    Build a ``war_summary`` dict from a JSON war-data dict.

    Returns ``None`` when start_time cannot be parsed.
    """
    ts_compact, date_iso, _yr, _mo, *_ = _parse_start_time(str(war_data.get('start_time', '')))
    if not ts_compact:
        return None

    clan_d: Any = war_data.get('clan', {}) or {}
    opp_d: Any = war_data.get('opponent', {}) or {}
    opp_tag = (opp_d.get('tag') or 'UNK').lstrip('#')
    war_id = f"{opp_tag}_{ts_compact}"
    is_cwl = bool(war_data.get('is_cwl') or war_data.get('type') == 'cwl')
    my_stars = int(clan_d.get('stars', 0) or 0)
    opp_stars = int(opp_d.get('stars', 0) or 0)

    my_dest = float(clan_d.get('destruction', 0.0) or 0.0)
    opp_dest = float(opp_d.get('destruction', 0.0) or 0.0)
    if my_stars > opp_stars:
        result = 'win'
    elif opp_stars > my_stars:
        result = 'loss'
    elif my_dest > opp_dest:
        result = 'win'
    elif opp_dest > my_dest:
        result = 'loss'
    else:
        result = 'draw'

    def _lineup(mems: Any) -> str:
        import json as _j
        ths = sorted([int(m.get('townhall', 0) or 0) for m in mems], reverse=True)
        return _j.dumps(ths)

    _, end_time_iso, *_ = _parse_start_time(str(war_data.get('end_time', '')))
    # Key written by save_war_object is 'league_group' (not 'league_group_data').
    _lg_raw = war_data.get('league_group')
    if isinstance(_lg_raw, dict):
        _lg = cast(Dict[str, Any], _lg_raw)
    else:
        _lg: Dict[str, Any] = {}
    _lg_season_raw = _lg.get('season')
    if not isinstance(_lg_season_raw, str):
        _lg_season_raw = ''
    _lg_api_season = _lg_season_raw.strip()

    return {
        "war_id": war_id,
        "opponent_tag": '#' + opp_tag,
        "opponent_name": opp_d.get('name', ''),
        "clan_stars": my_stars,
        "opponent_stars": opp_stars,
        "clan_destruction": float(clan_d.get('destruction', 0.0) or 0.0),
        "opp_destruction": float(opp_d.get('destruction', 0.0) or 0.0),
        "team_size": int(war_data.get('team_size', 15) or 15),
        "attacks_per_member": int(war_data.get('attacks_per_member', 2) or 2),
        "war_type": str(war_data.get('type', 'random') or 'random'),
        "is_cwl": is_cwl,
        # Normalise to ISO Monday so mid-month CWLs with different per-clan start dates
        # share one season key.  Empty string when no league_group data is available
        # (do not fabricate a YYYY-MM fallback — that produced the old broken entries).
        "cwl_season": normalize_cwl_season(_lg_api_season) if is_cwl else "",
        "war_tag": str(war_data.get('war_tag') or ''),
        "end_time": end_time_iso or '',
        "state": str(war_data.get('state') or '').lower(),
        "result": result,
        "date": date_iso,
        "clan_lineup_json": _lineup(clan_d.get('members', [])),
        "opp_lineup_json": _lineup(opp_d.get('members', [])),
        "clan_attacks_used": int(clan_d.get('attacks_used', 0) or 0),
        "opp_attacks_used": int(opp_d.get('attacks_used', 0) or 0),
    }


def _load_war_data_from_json(clan_tag: str, json_file_path: Optional[str] = None, for_finalization: bool = False, preloaded_raw_data: Optional[Dict[str, Any]] = None) -> Dict[str, WarStatsDict]:
    """
    Load current war statistics from JSON war file in data/temp/ directory.
    
    This function converts the complete JSON war object into the temp_war_stats format
    used by the bot. It handles all war states (preparation, in_war, war_ended) and
    correctly calculates missed attacks based on war state.
    
    Args:
        clan_tag (str): Clash of Clans clan tag (normalized format: #ABCDEFGH)
        json_file_path (str): Optional specific JSON file path to load. If None, auto-discovers.
        for_finalization (bool): If True, always calculate missed attacks (used during war finalization).
                                 If False, respect war state (0 for ongoing wars, actual for ended wars).
                                 Default: False (for loading temp stats during ongoing wars)
    
    Returns:
        Dict[str, WarStatsDict]: Mapping PlayerID to war statistics, empty dict if no JSON found
        
    Notes:
        - Only searches data/temp/ directory (active wars and recently ended before archival)
        - War files are moved to archive AFTER finalization completes
        - Handles war_ended state (which temp CSV does not)
        - Extracts War ID from opponent tag and start time
        - Calculates missed attacks: 0 for ongoing wars (unless for_finalization=True), actual for war_ended
        - Defensive stars from bestOpponentAttack
        - REGRESSION FIX (2026-01-12): Regular wars saved with state='in_war' but finalized later
          need for_finalization=True to calculate correct missed attacks
    """
    import json
    import glob
    import re
    
    # If specific file provided, use it
    if json_file_path:
        if not os.path.exists(json_file_path):
            logging.warning(f"Specified JSON file not found: {json_file_path}")
            return {}
        json_file = json_file_path
    else:
        # Auto-discover (legacy behavior)
        safe_clan_tag = re.sub(r'[^A-Z0-9]', '', clan_tag.upper())
        temp_dir = get_war_shard_dir(safe_clan_tag, TEMP_DIR)
        
        # Search for JSON files matching this clan tag pattern in the correct shard
        pattern = os.path.join(temp_dir, f"{safe_clan_tag}_*_war_data.json")
        json_files = glob.glob(pattern)
        
        if not json_files:
            logging.debug(f"No JSON war file found for clan {clan_tag} in temp directory")
            return {}
        
        # If multiple files exist, use the most recent (shouldn't happen, but safety check)
        if len(json_files) > 1:
            logging.warning(f"Multiple JSON war files found for clan {clan_tag}: {json_files}. Using most recent.")
            json_files.sort(key=lambda x: os.path.getmtime(x), reverse=True)
        
        json_file = json_files[0]
    
    try:
        if preloaded_raw_data is not None:
            war_data = preloaded_raw_data
        else:
            with open(json_file, "r", encoding="utf-8") as f:
                war_data = json.load(f)

        # Extract war metadata
        war_state = war_data.get('state', 'unknown')
        attacks_per_member = war_data.get('attacks_per_member', 2)
        start_time_str = war_data.get('start_time', '')
        
        # Parse start time to get War ID components
        # Format: <Timestamp time=datetime.datetime(2025, 12, 18, 7, 7, 51) seconds_until=-137224>
        war_id_timestamp = None
        war_date_iso = None
        m = re.search(r'datetime\.datetime\(([^)]+)\)', start_time_str)
        if m:
            dt_args = [int(x.strip()) for x in m.group(1).split(',')]
            if len(dt_args) >= 5:
                y, mo, d, h, mi = dt_args[:5]
                war_id_timestamp = f"{y:04d}{mo:02d}{d:02d}{h:02d}{mi:02d}"
                war_date_iso = f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}"
        
        if not war_id_timestamp or not war_date_iso:
            logging.error(f"Could not parse start time from JSON file {json_file}: {start_time_str}")
            return {}
        
        # Get clan and opponent data
        clan_data = war_data.get('clan', {})
        opponent_data = war_data.get('opponent', {})
        
        if not clan_data or not opponent_data:
            logging.error(f"Missing clan or opponent data in JSON file {json_file}")
            return {}
        
        # Construct War ID: {opponent_tag}_{timestamp}
        opponent_tag = opponent_data.get('tag', 'UNK').lstrip('#')
        war_id = f"{opponent_tag}_{war_id_timestamp}"
        
        # Process clan members
        temp_stats: Dict[str, WarStatsDict] = {}
        members = clan_data.get('members', [])
        
        for member in members:
            player_id = member.get('tag', '')
            player_name = member.get('name', 'Unknown')
            
            if not player_id:
                logging.warning(f"Skipping member with no tag in JSON file {json_file}")
                continue
            
            # Calculate attack stats
            attacks = member.get('attacks', [])
            num_attacks = len(attacks)
            total_stars = sum(atk.get('stars', 0) for atk in attacks)
            
            # Calculate missed attacks based on context
            # INVARIANT: Missed_Attacks is 0 for ongoing wars, actual for war_ended OR for_finalization
            # REGRESSION FIX (2026-01-12): Regular wars may be saved with state='in_war' but
            # need correct missed attacks when finalizing to history. Use for_finalization flag.
            if for_finalization or war_state.lower() == 'war_ended':
                missed_attacks = max(attacks_per_member - num_attacks, 0)
            else:
                missed_attacks = 0  # Players can still attack
            
            # Get defensive stars from best opponent attack
            best_opp_attack = member.get('bestOpponentAttack')
            defensive_stars = 0
            if best_opp_attack and isinstance(best_opp_attack, dict):
                defensive_stars = best_opp_attack.get('stars', 0)  # type: ignore[misc]
            
            # Create WarStatsDict entry
            temp_stats[player_id] = {
                "WarID": war_id,
                "Date": war_date_iso,
                "Player": player_name,
                "PlayerID": player_id,
                "TH_lvl": member.get('townhall', 0),
                "Stars": total_stars,
                "Attacks": num_attacks,
                "Missed_Attacks": missed_attacks,
                "Max_Attacks": attacks_per_member,
                "Defensive_Stars": defensive_stars,
                "Times_Defended": 1 if (best_opp_attack and isinstance(best_opp_attack, dict)) else 0,
            }
        
        logging.debug(f"Loaded {len(temp_stats)} player stats from JSON file {json_file} for clan {clan_tag} (state: {war_state})")
        return temp_stats
        
    except json.JSONDecodeError as e:
        logging.error(f"Failed to parse JSON file {json_file}: {e}")
        return {}
    except Exception as e:
        logging.error(f"Error loading war data from JSON file {json_file}: {e}")
        return {}

def _append_current_war_to_history(clan_tag: str, json_file_path: Optional[str] = None, archive_set: Optional[Set[str]] = None, war_obj: Optional[Dict[str, Any]] = None) -> None:  # type: ignore[misc]
    """
    PRIVATE: Only to be called by cache_manager.py
    Appends the current war statistics to the clan's history database.
    
    Database-Only Implementation (2026-02-14):
    - Loads war data exclusively from JSON files (saved by cache_manager.save_war_object())
    - Writes directly to SQLite database (no CSV)
    - Moves JSON file to archive after successful database write (permanent record)
    - Compares content before deletion: only deletes if identical to archive
    - Replaces archive if content differs (handles late attacks)

    Args:
        clan_tag (str): Clash of Clans clan tag (normalized format: #ABCDEFGH)
        json_file_path (str): Optional specific JSON file path to process. If None, auto-discovers.
        archive_set: Optional pre-built set of archive filenames for O(1) existence checks.
        war_obj: Optional pre-loaded JSON dict to skip redundant file reads.
    Returns:
        None
    """
    from qapbot.cache_manager import CACHE
    
    logging.debug(f"append_current_war_to_history called with clan_tag={clan_tag}, json_file_path={json_file_path}")
    
    # Verify database is initialized
    if not CACHE.db_manager:  # type: ignore[has-type]
        logging.error(f"[HISTORY-APPEND-FAILED] Database manager not initialized for {clan_tag}")
        return
    
    # Load war data from JSON files (skip if war_obj already provided)
    # REGRESSION FIX (2026-01-12): Pass for_finalization=True to calculate correct missed attacks
    # even when war state is 'in_war' (regular wars saved during war, finalized after)
    if war_obj is not None:
        # Pre-loaded war object passed from caller — skip disk read
        temp_stats = True  # non-empty sentinel; we only check truthiness below
    elif json_file_path:
        # Use specific file provided
        temp_stats = _load_war_data_from_json(clan_tag, json_file_path, for_finalization=True)
    else:
        # Fallback: auto-discover (legacy behavior)
        temp_stats = _load_war_data_from_json(clan_tag, for_finalization=True)
    
    if not temp_stats:
        logging.error(
            f"[HISTORY-APPEND-FAILED] No war data found for clan {clan_tag}. "
            f"No JSON files found in data/temp/. "
            f"Cannot append to history. This usually means the war was never tracked (e.g., clan subscribed after war ended)."
        )
        return
    
    logging.debug(f"Using war data from JSON for clan {clan_tag}: {len(temp_stats) if not isinstance(temp_stats, bool) else '(pre-loaded)'} players")
    
    # Determine which JSON file to move to archive after successful append
    if json_file_path and os.path.exists(json_file_path):
        temp_file_to_delete = json_file_path
    else:
        # Fallback: Find JSON file (legacy auto-discovery)
        import glob
        import re
        safe_clan_tag = re.sub(r'[^A-Z0-9]', '', clan_tag.upper())
        temp_dir = get_war_shard_dir(safe_clan_tag, TEMP_DIR)
        pattern = os.path.join(temp_dir, f"{safe_clan_tag}_*_war_data.json")
        json_files = glob.glob(pattern)
        temp_file_to_delete = json_files[0] if json_files else None
    
    # Write to war_attacks + war_summary tables
    _raw_war = war_obj  # Use pre-loaded object if available
    _json_src = json_file_path or temp_file_to_delete
    if _raw_war is None:
        # Fallback: load from disk (legacy path or when war_obj not provided)
        if _json_src and os.path.exists(_json_src):
            try:
                with open(_json_src, 'r', encoding='utf-8') as _jf:
                    _raw_war = _json.load(_jf)
            except Exception as _e:
                logging.error(f"[DB-WRITE] Failed to read JSON for {clan_tag}: {_e}")
                return
        else:
            logging.error(f"[DB-WRITE] No JSON source for war_attacks/war_summary for {clan_tag}")
            return
    if _raw_war:
        try:
            _raw_state = str((_raw_war or {}).get('state') or '').lower()
            _incomplete_in_war = _raw_state in {'in_war', 'inwar'}
            atk_rows = build_per_attack_rows(
                _raw_war,
                clan_tag,
                for_finalization=not _incomplete_in_war,
                include_missed_sentinels=not _incomplete_in_war,
            )
            summary = build_war_summary(_raw_war, clan_tag)
            # Layer 2 sync lookup: inject round_number if already known from
            # cwl_league_rounds (populated by Layer 1 in _find_active_cwl_war_for_clan)
            if summary and summary.get("is_cwl") and summary.get("war_tag"):
                try:
                    _rn = CACHE.db_manager.get_cwl_round_for_war_tag_sync(summary["war_tag"])  # type: ignore[union-attr]
                    if _rn is not None:
                        summary["round_number"] = _rn
                except Exception as _rn_ex:
                    logging.debug(f"[DB-WRITE] round_number lookup failed: {_rn_ex}")
            # Single transaction + single commit for both tables — halves
            # the fsync cost on HDD/server-machine (~300 ms instead of ~600 ms).
            CACHE.db_manager.add_war_data_sync(clan_tag, atk_rows or [], summary)  # type: ignore[union-attr]
            war_id = atk_rows[0]['WarID'] if atk_rows else 'UNKNOWN'
            logging.info(f"[DB-WRITE] Appended war {war_id} to war_attacks/war_summary for clan {clan_tag}")
        except Exception as _e:
            logging.error(f"[DB-WRITE] war_attacks/war_summary write failed for {clan_tag}: {_e}")
            logging.error(f"[DB-WRITE] War data NOT saved. Manual intervention required.")
            return
    
    # After successful append: move to archive (NOT delete - archive is permanent record)
    if temp_file_to_delete and os.path.exists(temp_file_to_delete):
        try:
            _basename = os.path.basename(temp_file_to_delete)
            archive_dir = get_war_shard_dir(_basename.split("_")[0], ARCHIVE_DIR)
            os.makedirs(archive_dir, exist_ok=True)
            archive_file = os.path.join(archive_dir, _basename)
            
            # Check if archive already exists and compare content
            _archive_exists = (_basename in archive_set) if archive_set is not None else os.path.exists(archive_file)
            if _archive_exists:
                # Compare file contents to detect late attacks or updates
                with open(temp_file_to_delete, 'r', encoding='utf-8') as temp_f:
                    temp_content = temp_f.read()
                with open(archive_file, 'r', encoding='utf-8') as archive_f:
                    archive_content = archive_f.read()
                
                if temp_content == archive_content:
                    # Content identical - delete temp file
                    os.remove(temp_file_to_delete)
                    logging.info(f"Deleted temp war file (identical to archive): {_basename}")
                else:
                    # Content differs — regression guard before replacing archive.
                    # Never overwrite an archive that has more attacks than the temp file.
                    _allow_replace = True
                    try:
                        _temp_obj = _json.loads(temp_content)
                        _arch_obj = _json.loads(archive_content)
                        _temp_atk = sum(len(m.get('attacks') or []) for _side in ('clan', 'opponent') for m in (_temp_obj.get(_side) or {}).get('members', []))  # type: ignore[union-attr]
                        _arch_atk = sum(len(m.get('attacks') or []) for _side in ('clan', 'opponent') for m in (_arch_obj.get(_side) or {}).get('members', []))  # type: ignore[union-attr]
                        if _temp_atk < _arch_atk:
                            os.remove(temp_file_to_delete)
                            logging.warning(
                                f"[REGRESSION-GUARD] Discarded temp {_basename}: "
                                f"temp has {_temp_atk} attacks < archive {_arch_atk} — archive preserved"
                            )
                            _allow_replace = False
                        else:
                            logging.info(f"[ARCHIVE-UPDATE] temp={_temp_atk} attacks >= archive={_arch_atk} attacks — replacing archive")
                    except Exception as _guard_ex:
                        logging.warning(f"[REGRESSION-GUARD] Could not compare attack counts for {_basename}: {_guard_ex} — proceeding with update")
                    if _allow_replace:
                        # Content differs (late attacks) - replace archive with updated version.
                        # Defer when inside a batch so the file move stays atomic with the DB write.
                        if CACHE.db_manager is not None and CACHE.db_manager.is_war_batch_active():  # type: ignore[misc]
                            CACHE.db_manager.defer_file_move(temp_file_to_delete, archive_file)
                        else:
                            # os.replace() is atomic — no SIGINT race between delete+rename
                            os.replace(temp_file_to_delete, archive_file)
                        logging.info(f"Updated archive with late attacks: {_basename}")
            else:
                # Move to archive for permanent record.
                # CRITICAL: when inside a war_write_batch() the DB write is deferred.
                # Defer the file move too so that a bot restart between here and the
                # batch flush cannot leave the file in archive/ with no DB record.
                if CACHE.db_manager is not None and CACHE.db_manager.is_war_batch_active():  # type: ignore[misc]
                    CACHE.db_manager.defer_file_move(temp_file_to_delete, archive_file)
                    # archive_set NOT updated here — file hasn't moved yet.
                    # The deferred-moves executor updates it after the DB flush.
                else:
                    os.rename(temp_file_to_delete, archive_file)
                    if archive_set is not None:
                        archive_set.add(_basename)
                logging.debug(f"Moved temp war file to archive: {_basename}")
        except Exception as e:
            logging.error(f"Failed to archive temp file {temp_file_to_delete}: {e}")
    else:
        logging.warning(f"No temp file to archive after append for clan {clan_tag}")

def _update_history_with_late_attacks(clan_tag: str, war_id: str, updated_war_stats: Dict[str, WarStatsDict]) -> bool:  # type: ignore[misc]
    """
    PRIVATE: Update history database with late attack data from JSON.
    
    Uses db_manager for all database operations (no direct sqlite3 access).
    Deletes existing records for the war_id and inserts updated records.
    
    Args:
        clan_tag (str): Clash of Clans clan tag (normalized format: #ABCDEFGH)
        war_id (str): War ID to update
        updated_war_stats (Dict[str, WarStatsDict]): Updated war statistics from JSON
    
    Returns:
        bool: True if update successful, False otherwise
    """
    from qapbot.cache_manager import CACHE
    
    # Verify database is initialized
    if not CACHE.db_manager:  # type: ignore[has-type]
        logging.error(f"[HISTORY-UPDATE] Database manager not initialized for {clan_tag}")
        return False
    
    if not updated_war_stats:
        logging.error(f"[HISTORY-UPDATE] No updated war data provided for war {war_id}")
        return False
    
    # _update_history_with_late_attacks is no longer needed — late attacks are
    # handled directly by update_war_attack_records_sync in _process_war_history.
    # Return True to preserve cache-invalidation logic in the caller.
    return True
