"""
Script to recover CWL war data that was deleted instead of archived.

Uses CoC API to:
1. Get league group via get_league_group (retrieves war tags)
2. Get war data via get_league_war (retrieves full war JSON)
3. Save recovered war to archive directory

Usage:
    python -m qapbot.scripts.recover_cwl_war
"""
# pyright: reportUnknownVariableType=false
# pyright: reportUnknownMemberType=false
# pyright: reportUnknownArgumentType=false
# pyright: reportUnknownParameterType=false

import asyncio
import json
import logging
import os
import sys
from typing import Any, Optional

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

import coc  # type: ignore[import-untyped]
from qapbot.config import CONFIG

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)


def serialize_war_object(war_obj: Any, tracked_clan_tag: Optional[str] = None) -> dict[str, Any]:
    """
    Convert war object to JSON-serializable dict matching QapBot's expected format.
    
    Args:
        war_obj: coc.ClanWar object from API
        tracked_clan_tag: Optional clan tag being tracked (e.g., '#L2J0C0PY')
                         If provided, ensures this clan is in "clan" position in JSON
    """
    
    def serialize_timestamp(ts: Any) -> str:
        """Serialize a Timestamp object to string format matching cache_manager.py."""
        if hasattr(ts, 'time'):
            # Return in the format: "<Timestamp time=datetime.datetime(...)>"
            dt = ts.time
            return f"<Timestamp time=datetime.datetime({dt.year}, {dt.month}, {dt.day}, {dt.hour}, {dt.minute}, {dt.second}) seconds_until={ts.seconds_until}>"
        return str(ts)
    
    def serialize_attack(attack: Any) -> dict[str, Any]:
        """Serialize an attack object."""
        return {
            'attackerTag': attack.attacker_tag,
            'defenderTag': attack.defender_tag,
            'stars': attack.stars,
            'destruction': attack.destruction,
            'order': attack.order,
            'duration': getattr(attack, 'duration', None),
            'fresh': getattr(attack, 'fresh', None)
        }
    def find_best_opponent_attack(member_tag: Any, opponent_members: Any) -> Optional[dict[str, Any]]:
        """Find the best (highest stars) opponent attack against a member."""
        best_attack = None
        best_stars = -1
        
        for opp_member in opponent_members:
            for attack in (opp_member.attacks or []):
                if attack.defender_tag == member_tag:
                    if attack.stars > best_stars:
                        best_stars = attack.stars
                        best_attack = attack
        
        if best_attack:
            return {
                'attackerTag': best_attack.attacker_tag,
                'defenderTag': best_attack.defender_tag,
                'stars': best_attack.stars,
                'destruction': best_attack.destruction,
                'order': best_attack.order,
                'duration': getattr(best_attack, 'duration', None),
                'fresh': getattr(best_attack, 'fresh', None)
            }
        return None
    
    def calculate_defensive_stars(member_tag: Any, opponent_members: Any) -> int:
        """Calculate defensive stars for a member by counting opponent attacks."""
        defensive_stars = 0
        for opp_member in opponent_members:
            for attack in (opp_member.attacks or []):
                if attack.defender_tag == member_tag:
                    defensive_stars += attack.stars
        return defensive_stars
    
    def serialize_player(player: Any, opponent_members: Any) -> dict[str, Any]:
        """Serialize a player object with defensive stars calculation."""
        # Get previous_best_opponent_attack safely
        previous_best_attack = None
        try:
            previous_best_attack = getattr(player, "previous_best_opponent_attack", None)
        except (ValueError, AttributeError):
            previous_best_attack = None
        
        return {
            'tag': player.tag,
            'name': player.name,
            'townhall': player.town_hall,
            'map_position': player.map_position,
            'role': None,
            'donated': None,
            'received': None,
            'attacks': [serialize_attack(attack) for attack in (player.attacks or [])],
            'opponentAttacks': calculate_defensive_stars(player.tag, opponent_members),
            'bestOpponentAttack': find_best_opponent_attack(player.tag, opponent_members),
            'previousBestOpponentAttack': serialize_attack(previous_best_attack) if previous_best_attack else None
        }
    
    def serialize_clan(clan_obj: Any, opponent_members: Any) -> dict[str, Any]:
        """Serialize a clan object with proper defensive stars."""
        return {
            'tag': clan_obj.tag,
            'name': clan_obj.name,
            'level': clan_obj.level,
            'badge': {
                'name': '',
                'url': getattr(clan_obj.badge, 'url', ''),
                'small': getattr(clan_obj.badge, 'small', ''),
                'medium': getattr(clan_obj.badge, 'medium', ''),
                'large': getattr(clan_obj.badge, 'large', '')
            },
            'stars': clan_obj.stars,
            'destruction': clan_obj.destruction,
            'attack_wins': None,
            'attacks_used': getattr(clan_obj, 'attacks_used', 0),
            'wins': None,
            'members': [serialize_player(member, opponent_members) for member in clan_obj.members]
        }
    
    # Get opponent members lists for defensive star calculation
    # CRITICAL: Normalize data structure for tracked clan
    # If tracked_clan_tag provided and API returned our clan in opponent position,
    # swap clan/opponent so our tracked clan is ALWAYS in "clan" position in JSON
    clan_obj = war_obj.clan
    opponent_obj = war_obj.opponent
    
    if tracked_clan_tag and opponent_obj.tag == tracked_clan_tag:
        logging.debug(f"Tracked clan {tracked_clan_tag} in opponent position - swapping for JSON consistency")
        clan_obj, opponent_obj = opponent_obj, clan_obj
    
    clan_members = clan_obj.members
    opponent_members = opponent_obj.members
    
    # Extract league group information if available (match cache_manager.py)
    league_group = getattr(war_obj, "league_group", None)
    league_group_data = None
    if league_group:
        league_group_data = {
            "tag": getattr(league_group, "tag", ""),
            "state": str(getattr(league_group, "state", "")),
            "season": getattr(league_group, "season", "")
        }
    
    # Build the serializable dict matching cache_manager.py format
    # State format: Extract .name attribute from enum (e.g., "war_ended" not "War Ended")
    war_state: Any = getattr(war_obj, "state", "unknown")
    state_str = str(war_state.name if hasattr(war_state, "name") else war_state)  # type: ignore[union-attr]
    
    war_dict = {
        'state': state_str,
        'team_size': war_obj.team_size,
        'attacks_per_member': war_obj.attacks_per_member,
        'type': 'cwl',
        'is_cwl': True,
        'war_tag': war_obj.war_tag,
        'start_time': serialize_timestamp(war_obj.start_time),
        'end_time': serialize_timestamp(war_obj.end_time),
        'preparation_start_time': serialize_timestamp(war_obj.preparation_start_time),
        'league_group': league_group_data,
        'clan': serialize_clan(clan_obj, opponent_members),
        'opponent': serialize_clan(opponent_obj, clan_members),
        'attacks': [serialize_attack(attack) for attack in (war_obj.attacks or [])]
    }
    
    return war_dict


async def recover_cwl_war(clan_tag: str, war_tags: list[str]) -> None:
    """
    Recover CWL war data using pre-known war tags (obtained externally, e.g. ClashSpot).

    Saves each recovered war as a JSON file in data/temp/ so that the bot's normal
    manage_war_files() pipeline picks them up and writes them to the database.

    Idempotent: skips any war whose output file already exists in data/temp/ or
    archive/, so re-running the script never creates duplicate DB entries.

    Args:
        clan_tag: The tracked clan tag (with #). Used to orient the JSON so our
                  clan is always in the "clan" position.
        war_tags: List of CWL war tags (with #) for each round to recover.
    """
    logging.info(f"Recovering {len(war_tags)} CWL war(s) for clan {clan_tag}")

    async with coc.Client() as client:
        try:
            await client.login(CONFIG.coc_email, CONFIG.coc_password)
            logging.info("Logged in to CoC API")
        except Exception as e:
            logging.error(f"Failed to login to CoC API: {e}")
            return

        recovered_count = 0
        skipped_count = 0

        for round_idx, war_tag in enumerate(war_tags, start=1):
            logging.info(f"\n--- Round {round_idx} | {war_tag} ---")
            try:
                war: Any = await client.get_league_war(war_tag)

                if not war:
                    logging.warning(f"Round {round_idx}: No data returned for {war_tag}")
                    skipped_count += 1
                    continue

                # Verify our clan is actually in this war
                if war.clan.tag != clan_tag and war.opponent.tag != clan_tag:
                    logging.warning(
                        f"Round {round_idx}: {clan_tag} not found in war "
                        f"({war.clan.tag} vs {war.opponent.tag}) — skipping"
                    )
                    skipped_count += 1
                    continue

                # Only store wars that have finished
                # Use .name (e.g. 'war_ended') — consistent with cache_manager.py.
                # str(war.state) returns the enum repr ('WarState.war_ended'), not the name.
                war_state_name = str(getattr(war.state, 'name', str(war.state)))
                if war_state_name != 'war_ended':
                    logging.info(f"Round {round_idx}: Skipping (state: {war_state_name})")
                    skipped_count += 1
                    continue

                # Build standard filename: {OURCLAN}_{OPPONENT}_{YYYYMMDDHHMM}_war_data.json
                # Our clan always goes first so manage_war_files() can match it.
                if war.clan.tag == clan_tag:
                    our_clean = war.clan.tag.lstrip('#')
                    opp_clean = war.opponent.tag.lstrip('#')
                else:
                    our_clean = war.opponent.tag.lstrip('#')
                    opp_clean = war.clan.tag.lstrip('#')

                # Derive start timestamp for the filename
                _wst = getattr(war, 'start_time', None)
                if _wst and hasattr(_wst, 'time'):
                    _wst = _wst.time
                import datetime as _dt_mod
                _war_ts = _wst.strftime("%Y%m%d%H%M") if _wst else _dt_mod.datetime.now(_dt_mod.timezone.utc).strftime("%Y%m%d%H%M")

                filename = f"{our_clean}_{opp_clean}_{_war_ts}_war_data.json"
                import QBcsvhandling as _qbc_shard
                _temp_base = os.path.join(CONFIG.data_dir, "temp")
                temp_path = os.path.join(_qbc_shard.get_war_shard_dir(our_clean.upper(), _temp_base), filename)
                archive_path = os.path.join(_qbc_shard.get_war_shard_dir(our_clean.upper(), CONFIG.archive_dir), filename)

                # Idempotency: skip if already queued in temp or already in archive/DB
                if os.path.exists(temp_path):
                    logging.info(f"Round {round_idx}: Already in temp queue: {filename} — skipping")
                    skipped_count += 1
                    continue
                if os.path.exists(archive_path):
                    logging.info(f"Round {round_idx}: Already in archive: {filename} — skipping")
                    skipped_count += 1
                    continue

                # Serialize (our clan always in 'clan' position) and save to temp
                war_dict = serialize_war_object(war, tracked_clan_tag=clan_tag)
                os.makedirs(os.path.dirname(temp_path), exist_ok=True)
                with open(temp_path, 'w', encoding='utf-8') as f:
                    json.dump(war_dict, f, indent=2, ensure_ascii=False)

                logging.info(f"Round {round_idx}: ✅ Saved → {filename}")
                logging.info(f"   {war.clan.name} {war.clan.stars}★ vs {war.opponent.stars}★ {war.opponent.name}")
                recovered_count += 1

            except coc.NotFound:
                logging.error(f"Round {round_idx}: {war_tag} not found — API data may have expired")
            except coc.PrivateWarLog:
                logging.warning(f"Round {round_idx}: {war_tag} — private war log")
            except Exception as e:
                logging.error(f"Round {round_idx}: Error retrieving {war_tag}: {e}")

        print(f"\n{'='*60}")
        print(f"  Recovered : {recovered_count} war(s) → written to data/temp/")
        print(f"  Skipped   : {skipped_count} war(s)")
        if recovered_count:
            print()
            print("  Next step: restart the bot (or wait for its next cycle) to")
            print("  process the temp files and write them to the database.")
        print(f"{'='*60}")


async def main():
    """Main entry point."""
    print("="*60)
    print("CWL War Recovery Script  (manual war tags)")
    print("="*60)

    # Clan tag
    clan_tag = input("\nEnter your clan tag (e.g. 2GCULRVPP or #2GCULRVPP): ").strip()
    if not clan_tag:
        print("Error: Clan tag is required")
        return
    if not clan_tag.startswith('#'):
        clan_tag = f'#{clan_tag}'

    # Collect up to 7 war tags (one per round)
    print("\nEnter the war tag for each CWL round (press Enter to stop early):")
    war_tags: list[str] = []
    for i in range(1, 8):
        raw = input(f"  Round {i} war tag (or Enter to stop): ").strip()
        if not raw:
            if i == 1:
                print("No war tags provided.")
                return
            print(f"  Stopping after round {i - 1}.")
            break
        if not raw.startswith('#'):
            raw = f'#{raw}'
        war_tags.append(raw)

    print(f"\nRecovering {len(war_tags)} war(s) for {clan_tag} ...")
    await recover_cwl_war(clan_tag, war_tags)


if __name__ == '__main__':
    asyncio.run(main())
