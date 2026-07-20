"""Debug script to investigate specific war tag API response.

This diagnostic tool fetches and displays detailed information about a CWL war from both
archived JSON data and the live Clash of Clans API. It helps troubleshoot API response
issues, data discrepancies, and war state problems.

Features:
- Loads archived war data from JSON file for comparison
- Fetches war details from CoC API using get_league_war()
- Displays all war attributes and object structures
- Shows clan and opponent data including members, stars, and destruction
- Outputs raw API response structure for debugging

Usage:
    python debug_war_api.py
    
Configuration:
    - Edit war_tag and archived_file variables in main() to target specific war
    - Requires valid CoC API credentials in .env file

Output:
    Formatted console output showing:
    - Archived war data comparison
    - API connection status
    - War state and metadata
    - Clan/opponent details
    - All available war attributes
    - Raw API response structure
"""
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownParameterType=false, reportAttributeAccessIssue=false
import asyncio
import coc  # type: ignore[import-untyped]
import json
import os
import sys
from typing import Optional
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

# Use DEV credentials only
COC_EMAIL_DEV = os.getenv('COC_API_EMAIL_DEV')
COC_PASSWORD_DEV = os.getenv('COC_API_PASSWORD_DEV')


async def debug_war_tag(war_tag: str, archived_file: Optional[str] = None):
    """Fetch and display detailed information about a specific war tag."""
    
    print(f"{'='*80}")
    print(f"DEBUG WAR TAG: {war_tag}")
    print(f"{'='*80}\n")
    
    # Load archived data if provided
    if archived_file and os.path.exists(archived_file):
        print("[ARCHIVED DATA]")
        with open(archived_file, 'r', encoding='utf-8') as f:
            archived_war = json.load(f)
        
        print(f"Clan: {archived_war['clan']['name']} ({archived_war['clan']['tag']})")
        print(f"Opponent: {archived_war['opponent']['name']} ({archived_war['opponent']['tag']})")
        print(f"State: {archived_war.get('state', 'unknown')}")
        print(f"Start Time: {archived_war.get('start_time', 'unknown')}")
        print(f"End Time: {archived_war.get('end_time', 'unknown')}")
        print(f"Team Size: {archived_war.get('team_size', 'unknown')}")
        print(f"Is CWL: {archived_war.get('is_cwl', False)}")
        print(f"League Group: {archived_war.get('league_group', {})}")
        print()
    
    # Connect to API
    print("[API CONNECTION]")
    coc_client = coc.Client()
    try:
        await coc_client.login(COC_EMAIL_DEV or "", COC_PASSWORD_DEV or "")
        print("✅ Connected to CoC API (DEV credentials)\n")
    except Exception as e:
        print(f"❌ Failed to connect: {e}")
        return
    
    try:
        # Fetch war from API
        print(f"[API RESPONSE for {war_tag}]")
        api_war = await coc_client.get_league_war(war_tag)
        
        print(f"War State: {api_war.state}")
        print(f"War Type: {type(api_war).__name__}")
        print(f"War Tag: {api_war.tag if hasattr(api_war, 'tag') else 'N/A'}")
        
        # Check clan data
        print(f"\nClan Object: {api_war.clan}")
        print(f"Clan Type: {type(api_war.clan)}")
        if api_war.clan:
            print(f"  Tag: {api_war.clan.tag if hasattr(api_war.clan, 'tag') else 'N/A'}")
            print(f"  Name: {api_war.clan.name if hasattr(api_war.clan, 'name') else 'N/A'}")
            print(f"  Stars: {api_war.clan.stars if hasattr(api_war.clan, 'stars') else 'N/A'}")
            print(f"  Members: {len(api_war.clan.members) if hasattr(api_war.clan, 'members') else 'N/A'}")
        
        # Check opponent data
        print(f"\nOpponent Object: {api_war.opponent}")
        print(f"Opponent Type: {type(api_war.opponent)}")
        if api_war.opponent:
            print(f"  Tag: {api_war.opponent.tag if hasattr(api_war.opponent, 'tag') else 'N/A'}")
            print(f"  Name: {api_war.opponent.name if hasattr(api_war.opponent, 'name') else 'N/A'}")
            print(f"  Stars: {api_war.opponent.stars if hasattr(api_war.opponent, 'stars') else 'N/A'}")
            print(f"  Members: {len(api_war.opponent.members) if hasattr(api_war.opponent, 'members') else 'N/A'}")
        
        # Check all attributes
        print(f"\n[ALL API WAR ATTRIBUTES]")
        for attr in dir(api_war):
            if not attr.startswith('_'):
                try:
                    value = getattr(api_war, attr)
                    if not callable(value):
                        print(f"  {attr}: {value}")
                except Exception as e:
                    print(f"  {attr}: <Error accessing: {e}>")
        
        # Try to dump as JSON-like structure
        print(f"\n[RAW API RESPONSE STRUCTURE]")
        if hasattr(api_war, '__dict__'):
            import pprint
            pprint.pprint(api_war.__dict__)
        
    except coc.NotFound:
        print(f"❌ War not found via API: {war_tag}")
    except Exception as e:
        print(f"❌ Error fetching war: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await coc_client.close()


async def main():
    # Prompt user for archived war file name
    print("Enter the archived war JSON filename:")
    print("  (Example: 2RLU2QCL2_2QCV8992C_202601010000_war_data.json)")
    filename = input("Filename: ").strip()
    
    _prod_base = os.getenv("PROD_DATA_DIR", "")
    _archive_base = os.path.join(_prod_base, "archive") if _prod_base else "archive"
    # Construct full path in archive directory (sharded + legacy flat layout)
    archived_file = os.path.join(_archive_base, filename)
    if not os.path.exists(archived_file):
        try:
            import QBcsvhandling as _qbc_shard
            clan_safe = filename.split("_", 1)[0].upper()
            shard_file = os.path.join(
                _qbc_shard.get_war_shard_dir(clan_safe, _archive_base),
                filename,
            )
            if os.path.exists(shard_file):
                archived_file = shard_file
        except Exception:
            pass

    if not os.path.exists(archived_file):
        print(f"\n❌ Error: File not found in archive/ (flat or shard): {filename}")
        return
    
    # Load the file and extract war_tag
    try:
        with open(archived_file, 'r', encoding='utf-8') as f:
            war_data = json.load(f)
        
        war_tag = war_data.get('war_tag')
        if not war_tag:
            print(f"\n❌ Error: No 'war_tag' field found in {archived_file}")
            print("   This may not be a CWL war file.")
            return
        
        print(f"\n✅ Loaded file: {archived_file}")
        print(f"✅ Extracted war_tag: {war_tag}\n")
        
    except json.JSONDecodeError as e:
        print(f"\n❌ Error: Invalid JSON in file: {e}")
        return
    except Exception as e:
        print(f"\n❌ Error reading file: {e}")
        return
    
    await debug_war_tag(war_tag, archived_file)


if __name__ == "__main__":
    asyncio.run(main())
