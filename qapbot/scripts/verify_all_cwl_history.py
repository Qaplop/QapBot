"""
Bulk verification script to test if historical CWL wars can be retrieved via API.

This script:
1. Scans all archived war files
2. For each CWL war with a war_tag:
   - Attempts to retrieve from API using get_league_war(war_tag)
   - Compares API data with clan history database
3. Prompts after first comparison to continue or stop
4. Reports discrepancies and API availability
"""
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false
import asyncio
import json
import os
import signal
import sqlite3
import sys
from datetime import datetime
import glob
from dotenv import load_dotenv
from typing import Optional, Dict, Any, Tuple, Set, List

# ── CTRL+C handling ──────────────────────────────────────────────────────────
# First CTRL+C: finish current check then stop and print summary.
# Second CTRL+C: exit immediately.
_stop_requested = False

def _handle_ctrl_c(signum: int, frame: Any) -> None:
    global _stop_requested
    if _stop_requested:
        print("\n[CTRL+C] Forced exit.")
        sys.exit(1)
    _stop_requested = True
    print("\n[CTRL+C] Will stop after current check completes (press again to force exit)...")

signal.signal(signal.SIGINT, _handle_ctrl_c)

# Load environment variables
load_dotenv()

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

import coc  # type: ignore[import]
from qapbot.db_manager import WarHistoryDB
from qapbot.config import CONFIG

# Use DEV credentials only
COC_EMAIL_DEV = os.getenv('COC_API_EMAIL_DEV')
COC_PASSWORD_DEV = os.getenv('COC_API_PASSWORD_DEV')


def extract_year_month_from_timestamp(timestamp_str: str) -> Optional[str]:
    """Extract YYYYMM from timestamp string."""
    if 'datetime.datetime(' in timestamp_str:
        import re
        match = re.search(r'datetime\.datetime\((\d+),\s*(\d+),', timestamp_str)
        if match:
            year = match.group(1)
            month = match.group(2).zfill(2)
            return f"{year}{month}"
    return None


async def load_history_for_war(
    db: WarHistoryDB,
    clan_tag: str,
    opponent_tag: str,
    war_year_month: str,
    clan_history_cache: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    script_conn: Optional[sqlite3.Connection] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Load history data for a specific war from database.

    Uses a shared persistent sqlite3 connection (script_conn) so the page
    cache stays warm across all clans — avoiding the cold-start penalty of
    opening a new connection per call.  Falls back to the WarHistoryDB method
    if no connection is provided.
    
    Also maintains an in-memory clan_history_cache to skip repeated queries
    for clans that appear in multiple archive files.
    
    Args:
        db: Database manager instance (fallback if no script_conn)
        clan_tag: Clan tag (with #)
        opponent_tag: Opponent clan tag (with #)
        war_year_month: War year-month in YYYYMM format
        clan_history_cache: Optional shared dict; populated on first query per clan.
        script_conn: Optional persistent sqlite3 connection for fast repeated queries.
    
    Returns:
        Tuple of (history_data dict, error message)
    """
    try:
        opponent_normalized = opponent_tag.replace('#', '')

        # Fetch all attack records for this clan, using cache to skip repeat DB hits
        if clan_history_cache is not None and clan_tag in clan_history_cache:
            all_records = clan_history_cache[clan_tag]
        elif script_conn is not None:
            # Fast path: reuse the persistent connection — page cache is already warm
            cursor = script_conn.execute("""
                SELECT war_id, date, player_name, player_tag, th_level,
                       SUM(stars) AS stars,
                       COALESCE(MAX(max_attacks), 0) - COALESCE(MAX(missed_attacks), 0) AS attacks,
                       MAX(missed_attacks) AS missed_attacks,
                       MAX(max_attacks) AS max_attacks,
                       MAX(defensive_stars) AS defensive_stars,
                       SUM(destruction) AS total_destruction
                FROM war_attacks
                WHERE clan_tag = ?
                GROUP BY war_id, player_tag
                ORDER BY date DESC
            """, (clan_tag,))
            all_records = [
                {"WarID": r["war_id"], "Player": r["player_name"],
                 "Stars": r["stars"], "Attacks": r["attacks"]}
                for r in cursor.fetchall()
            ]
            if clan_history_cache is not None:
                clan_history_cache[clan_tag] = all_records
        else:
            all_records = await db.get_clan_attack_history(clan_tag)
            if clan_history_cache is not None:
                clan_history_cache[clan_tag] = all_records
        
        # Find matching war in history by opponent and month
        war_players: List[Dict[str, Any]] = []
        for row in all_records:
            war_id = row.get('WarID', '')
            if war_id.startswith(opponent_normalized):
                # Check month matches
                war_id_parts = war_id.split('_')
                if len(war_id_parts) == 2 and len(war_id_parts[1]) >= 6:
                    file_year_month = war_id_parts[1][:6]
                    if file_year_month == war_year_month:
                        war_players.append(row)
        
        if war_players:
            total_stars = sum(int(p.get('Stars', 0)) for p in war_players)
            total_attacks = sum(int(p.get('Attacks', 0)) for p in war_players)
            # Store player data for detailed comparison
            players_dict = {p.get('Player', ''): int(p.get('Stars', 0)) for p in war_players}
            return {
                'total_stars': total_stars,
                'total_attacks': total_attacks,
                'player_count': len(war_players),
                'war_id': war_players[0].get('WarID', ''),
                'players': players_dict
            }, None
        else:
            return None, f"No war found for opponent {opponent_normalized} in {war_year_month}"
    
    except Exception as e:
        return None, f"Database error: {e}"


async def verify_war(
    war_file: str,
    coc_client: Any,
    db: WarHistoryDB,
    comparison_count: int,
    verified_war_tags: Optional[Set[str]] = None,
    clan_history_cache: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    script_conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Verify a single war file against API and history.
    
    Args:
        war_file: Path to war archive JSON
        coc_client: CoC API client
        db: Database manager instance
        comparison_count: Current comparison number
        verified_war_tags: Set of already verified war tags
        clan_history_cache: Shared per-clan history cache to avoid repeated DB queries.
        script_conn: Persistent sqlite3 connection for fast repeated queries.
    
    Returns:
        Dict with verification results
    """
    if verified_war_tags is None:
        verified_war_tags = set()
    
    print(f"\n{'='*80}")
    print(f"[{comparison_count}] Processing: {os.path.basename(war_file)}")
    print('='*80)
    
    # Load archived war
    with open(war_file, 'r', encoding='utf-8') as f:
        archived_war = json.load(f)
    
    war_tag = archived_war.get('war_tag', None)
    is_cwl = archived_war.get('is_cwl', False)
    clan_tag = archived_war['clan']['tag']
    opponent_tag = archived_war['opponent']['tag']
    
    print(f"Clan: {archived_war['clan']['name']} ({clan_tag})")
    print(f"Opponent: {archived_war['opponent']['name']} ({opponent_tag})")
    print(f"War Tag: {war_tag}")
    print(f"Is CWL: {is_cwl}")
    print(f"Archived State: {archived_war.get('state', 'unknown')}")
    print(f"Archived Clan Stars (war result): {archived_war['clan']['stars']}")
    print(f"Archived Opponent Stars: {archived_war['opponent']['stars']}")
    
    # Calculate individual player stars sum (for player statistics) - store player data
    archived_attack_count = 0
    archived_player_stars_sum = 0
    archived_players = {}
    for member in archived_war['clan'].get('members', []):
        attacks = member.get('attacks', [])
        if attacks:
            player_stars = sum(attack.get('stars', 0) for attack in attacks)
            archived_player_stars_sum += player_stars
            archived_attack_count += len(attacks)
            archived_players[member['name']] = player_stars
    
    print(f"Total: {archived_attack_count} attacks, {archived_player_stars_sum} stars")
    
    if not is_cwl:
        print("⏭️  Skipping - Not a CWL war")
        return {'skipped': True, 'reason': 'Not CWL'}
    
    if not war_tag:
        print("⏭️  Skipping - No war_tag")
        return {'skipped': True, 'reason': 'No war_tag'}
    
    # Check if already verified
    if war_tag in verified_war_tags:
        print(f"⏭️  Skipping - Already verified (found in cwl_verified_wars.txt)")
        return {'skipped': True, 'reason': 'Already verified'}
    
    # Extract year-month
    start_time_str = archived_war.get('start_time', '')
    war_year_month = extract_year_month_from_timestamp(start_time_str)
    
    if not war_year_month:
        print("⚠️  Could not extract year-month from timestamp")
        return {'skipped': True, 'reason': 'No timestamp'}
    
    print(f"War Month: {war_year_month[:4]}-{war_year_month[4:]}")
    
    # Try to fetch from API
    print(f"\n[API] Fetching war from API using war_tag: {war_tag}")
    api_war = None
    api_error = None
    api_player_stars_sum = 0
    api_players = {}
    
    try:
        api_war = await coc_client.get_league_war(war_tag)
        print(f"✅ API Success: Retrieved war data")
        print(f"   State: {api_war.state}")
        
        # Check if war data is valid (sometimes API returns "Not in War" state with None clans)
        if api_war.clan is None or api_war.opponent is None:
            raise ValueError(f"War data incomplete - State: {api_war.state}")
        
        # Find the correct clan in the API response by matching clan_tag
        if api_war.clan.tag == clan_tag:
            api_our_clan = api_war.clan
            api_opponent_clan = api_war.opponent
        else:
            api_our_clan = api_war.opponent
            api_opponent_clan = api_war.clan
        
        print(f"   Our Clan: {api_our_clan.name} ({api_our_clan.tag}) - {api_our_clan.stars} stars")
        print(f"   Opponent: {api_opponent_clan.name} ({api_opponent_clan.tag}) - {api_opponent_clan.stars} stars")
        
        # Calculate individual player stars sum (for player statistics) - store player data
        api_attack_count = 0
        api_player_stars_sum = 0
        api_players = {}
        for member in api_our_clan.members:
            if member.attacks:
                player_stars = sum(attack.stars for attack in member.attacks)
                api_player_stars_sum += player_stars
                api_attack_count += len(member.attacks)
                api_players[member.name] = player_stars
        
        print(f"   Total: {api_attack_count} attacks, {api_player_stars_sum} stars")
        
    except coc.NotFound:
        api_error = "NotFound - War not available via API"
        print(f"❌ API Error: {api_error}")
        api_war = None  # Ensure api_war is None on error
    except Exception as e:
        api_error = str(e)
        print(f"❌ API Error: {api_error}")
        api_war = None  # Ensure api_war is None on error
    
    # Load history
    print(f"\n[HISTORY] Checking clan history database...")
    history_data, history_error = await load_history_for_war(
        db, clan_tag, opponent_tag, war_year_month, clan_history_cache, script_conn
    )
    
    if history_error or history_data is None:
        print(f"❌ History Error: {history_error}")
    else:
        print(f"✅ History Found: {history_data['war_id']}")
        print(f"   Total Stars: {history_data['total_stars']}, Attacks: {history_data['total_attacks']}, Players: {history_data['player_count']}")
    
    # Compare
    print(f"\n[COMPARISON] - Individual Player Star Sums:")
    discrepancies = []
    
    if api_war and history_data:
        print(f"  Archived: {archived_player_stars_sum}")
        print(f"  API: {api_player_stars_sum}")
        print(f"  History DB: {history_data['total_stars']}")
        
        # Compare individual player star sums across all three sources
        if archived_player_stars_sum != api_player_stars_sum:
            discrepancies.append(f"Archived ≠ API: {archived_player_stars_sum} ≠ {api_player_stars_sum}")
        if archived_player_stars_sum != history_data['total_stars']:
            discrepancies.append(f"Archived ≠ History: {archived_player_stars_sum} ≠ {history_data['total_stars']}")
        if api_player_stars_sum != history_data['total_stars']:
            discrepancies.append(f"API ≠ History: {api_player_stars_sum} ≠ {history_data['total_stars']}")
        
    elif api_war:
        print(f"API available but no history data")
        discrepancies.append("History missing")
    elif history_data:
        print(f"History available but API returned: {api_error}")
        print(f"  Archived player stars sum: {archived_player_stars_sum}")
        print(f"  History DB: {history_data['total_stars']}")
        
        if archived_player_stars_sum != history_data['total_stars']:
            discrepancies.append(f"Archived ≠ History: {archived_player_stars_sum} ≠ {history_data['total_stars']}")
    else:
        print(f"Neither API nor history available")
        discrepancies.append("Both API and history unavailable")
    
    if discrepancies:
        print(f"\n⚠️  DISCREPANCIES:")
        for disc in discrepancies:
            print(f"   - {disc}")
        
        # Show detailed player-by-player comparison
        if api_war and history_data:
            print(f"\n[DETAILED PLAYER COMPARISON]")
            print(f"{'Player Name':<25} {'Archived':>10} {'API':>10} {'History':>10}")
            print("-" * 60)
            
            # Get all unique player names
            all_players = set(archived_players.keys()) | set(api_players.keys()) | set(history_data.get('players', {}).keys())
            
            for player_name in sorted(all_players):
                archived_stars = archived_players.get(player_name, '-')
                api_stars = api_players.get(player_name, '-')
                history_stars = history_data.get('players', {}).get(player_name, '-')
                print(f"{player_name:<25} {str(archived_stars):>10} {str(api_stars):>10} {str(history_stars):>10}")
        
        elif history_data:
            # Only archived and history available
            print(f"\n[DETAILED PLAYER COMPARISON]")
            print(f"{'Player Name':<25} {'Archived':>10} {'History':>10}")
            print("-" * 45)
            
            all_players = set(archived_players.keys()) | set(history_data.get('players', {}).keys())
            
            for player_name in sorted(all_players):
                archived_stars = archived_players.get(player_name, '-')
                history_stars = history_data.get('players', {}).get(player_name, '-')
                print(f"{player_name:<25} {str(archived_stars):>10} {str(history_stars):>10}")
    else:
        print(f"\n✅ No discrepancies - all data consistent!")
    
    return {
        'file': os.path.basename(war_file),
        'war_tag': war_tag,
        'clan_tag': clan_tag,
        'api_available': api_war is not None,
        'api_error': api_error,
        'history_available': history_data is not None,
        'history_error': history_error,
        'discrepancies': discrepancies
    }


async def main():
    """Main execution function."""
    print("=" * 80)
    print("CWL History Bulk Verification")
    print("=" * 80)
    
    # Find all archived war files (sharded + legacy flat layout)
    archive_dir = CONFIG.archive_dir
    war_files = glob.glob(os.path.join(archive_dir, "**", "*_war_data.json"), recursive=True)
    
    print(f"\nFound {len(war_files)} archived war files")
    
    if not war_files:
        print("No archived war files found!")
        return
    
    # Load list of already verified wars (now in scripts directory)
    scripts_dir = os.path.dirname(os.path.abspath(__file__))
    verified_wars_file = os.path.join(scripts_dir, "cwl_verified_wars.txt")
    verified_war_tags = set()
    if os.path.exists(verified_wars_file):
        with open(verified_wars_file, 'r', encoding='utf-8') as f:
            verified_war_tags = set(line.strip() for line in f if line.strip())
        print(f"Loaded {len(verified_war_tags)} previously verified wars")
    
    # Initialize database
    print("\nInitializing database...")
    db = WarHistoryDB()
    try:
        await db.initialize(CONFIG.db_path, CONFIG.history_db_path)
        print(f"✅ Database initialized at {CONFIG.db_path} (history: {CONFIG.history_db_path})")
    except Exception as e:
        print(f"❌ Failed to initialize database: {e}")
        return
    
    # Connect to API
    print("\nConnecting to CoC API...")
    
    if not COC_EMAIL_DEV or not COC_PASSWORD_DEV:
        print("❌ CoC API credentials not found in environment variables")
        print("   Please set COC_API_EMAIL_DEV and COC_API_PASSWORD_DEV")
        await db.close()
        return
    
    coc_client = coc.Client(key_count=10, throttler=coc.BatchThrottler, throttle_limit=100)
    try:
        await coc_client.login(COC_EMAIL_DEV, COC_PASSWORD_DEV)
        print("✅ Connected to CoC API (DEV credentials)")
    except Exception as e:
        print(f"❌ Failed to connect: {e}")
        await db.close()
        return
    
    # Prepare discrepancy log (now in scripts directory)
    discrepancy_log_file = os.path.join(scripts_dir, "cwl_discrepancies.txt")

    # Open a single persistent SQLite connection for all history queries.
    # Keeps the page cache warm across clans — avoids the 0.5s cold-start
    # penalty that occurs when get_clan_attack_history_sync opens a new
    # connection per call.
    script_conn = sqlite3.connect(CONFIG.db_path)
    script_conn.row_factory = sqlite3.Row
    script_conn.execute("PRAGMA cache_size=-65536")
    script_conn.execute("PRAGMA temp_store=MEMORY")
    print(f"✅ Persistent query connection opened")

    try:
        results = []
        comparison_count = 0
        
        print(f"\nProcessing all {len(war_files)} war files...")
        print(f"Discrepancies will be logged to: {discrepancy_log_file}")
        print(f"Verified wars will be logged to: {verified_wars_file}\n")
        
        # Shared clan history cache: avoids a new DB connection per archive file.
        # Most clans appear in multiple archive files (e.g. 7 CWL wars per season),
        # so caching by clan_tag reduces DB queries from O(files) to O(unique clans).
        clan_history_cache: Dict[str, List[Dict[str, Any]]] = {}

        with open(discrepancy_log_file, 'w', encoding='utf-8') as log_f, \
             open(verified_wars_file, 'a', encoding='utf-8') as verified_f:
            log_f.write("CWL History Discrepancy Report\n")
            log_f.write(f"Generated: {datetime.now().isoformat()}\n")
            log_f.write("=" * 80 + "\n\n")
            
            for i, war_file in enumerate(war_files, 1):
                if _stop_requested:
                    print(f"\nStopped by user after processing {i - 1}/{len(war_files)} files.")
                    break

                result = await verify_war(
                    war_file, coc_client, db, i, verified_war_tags,
                    clan_history_cache, script_conn
                )
                
                if not result.get('skipped', False):
                    comparison_count += 1
                    results.append(result)
                    
                    # If no discrepancies, add to verified list
                    if not result['discrepancies']:
                        verified_f.write(f"{result['war_tag']}\n")
                        verified_f.flush()
                    
                    # Log discrepancies
                    if result['discrepancies']:
                        log_f.write(f"War #{comparison_count}\n")
                        log_f.write(f"File: {result['file']}\n")
                        log_f.write(f"War Tag: {result['war_tag']}\n")
                        log_f.write(f"Clan: {result['clan_tag']}\n")
                        log_f.write(f"API Available: {result['api_available']}\n")
                        if result['api_error']:
                            log_f.write(f"API Error: {result['api_error']}\n")
                        log_f.write(f"History Available: {result['history_available']}\n")
                        if result['history_error']:
                            log_f.write(f"History Error: {result['history_error']}\n")
                        log_f.write("Discrepancies:\n")
                        for disc in result['discrepancies']:
                            log_f.write(f"  - {disc}\n")
                        log_f.write("\n" + "-" * 80 + "\n\n")
                        log_f.flush()  # Ensure data is written immediately
                    
                    # After first CWL war, ask if user wants to continue
                    if comparison_count == 1:
                        user_input = input("\nContinue with remaining wars? (y/n): ").strip().lower()
                        if user_input != 'y':
                            print("Stopping verification after first war.")
                            break

            print(f"\n[CACHE] History cache: {len(clan_history_cache)} unique clans loaded (saved ~{max(0, comparison_count - len(clan_history_cache))} DB connections)")
        
        # Summary
        print("\n" + "=" * 80)
        print("VERIFICATION SUMMARY")
        print("=" * 80)
        
        total = len(results)
        api_available = sum(1 for r in results if r['api_available'])
        history_available = sum(1 for r in results if r['history_available'])
        with_discrepancies = sum(1 for r in results if r['discrepancies'])
        
        print(f"Total CWL wars verified: {total}")
        print(f"API available: {api_available}/{total} ({api_available/total*100:.1f}%)" if total > 0 else "API available: N/A")
        print(f"History available: {history_available}/{total} ({history_available/total*100:.1f}%)" if total > 0 else "History available: N/A")
        print(f"Wars with discrepancies: {with_discrepancies}/{total}")
        
        print(f"\n📄 Discrepancy report saved to: {discrepancy_log_file}")
        
        if with_discrepancies > 0:
            print(f"\n⚠️  Wars with issues:")
            for r in results:
                if r['discrepancies']:
                    print(f"  - {r['file']} ({r['war_tag']})")
                    for disc in r['discrepancies'][:2]:  # Show first 2 discrepancies
                        print(f"      {disc}")
                    if len(r['discrepancies']) > 2:
                        print(f"      ... and {len(r['discrepancies']) - 2} more")
        
        # API errors breakdown
        api_errors = {}
        for r in results:
            if not r['api_available'] and r['api_error']:
                error = r['api_error']
                api_errors[error] = api_errors.get(error, 0) + 1
        
        if api_errors:
            print(f"\n📊 API Error Breakdown:")
            for error, count in api_errors.items():
                print(f"  - {error}: {count} wars")
        
    finally:
        script_conn.close()
        await coc_client.close()
        await db.close()
        print("\n✅ Cleanup complete - database and API connections closed")


if __name__ == "__main__":
    asyncio.run(main())
