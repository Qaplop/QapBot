
Let me check the coc.py library documentation, which is what we're actually using:

https://cocpy.readthedocs.io/

Let me get the Wars documentation page specifically:


Perfect! I found the relevant information. Here's a summary of the alternative war data retrieval methods in the Clash of Clans API:

CoC API War Data Retrieval Methods
Based on the coc.py documentation, there are several distinct API methods for retrieving war data:

1. Regular Clan Wars
Method: client.get_clan_war(clan_tag)
Use: Get information about a clan's current regular clan war
Returns: ClanWar object
Note: Does NOT fetch CWL wars

2. Clan War League (CWL) - Group Data ✅ (Special CWL API)
Method: client.get_league_group(clan_tag)
Use: Retrieve information about clan's current CWL group
Returns: ClanWarLeagueGroup object containing:
state: Current state of the league group
season: Current season
number_of_rounds: Total rounds (typically 7 for CWL)
rounds: List of war tags for each round
clans: All participating clans
Special: This is the CWL-specific endpoint you mentioned!

3. Clan War League (CWL) - Individual War Data
Method: client.get_league_war(war_tag)
Use: Retrieve a specific CWL war by its war tag
Returns: ClanWar object (with is_cwl=True and war_tag populated)
Note: Requires war tag from get_league_group().rounds

4. Unified War Retrieval (Smart Method)
Method: client.get_current_war(clan_tag, cwl_round=WarRound.current_war)
Use: Automatically searches for regular war first, then CWL if not found
Parameters:
cwl_round: WarRound.previous_war, WarRound.current_war, or WarRound.preparation
Returns: ClanWar object (or None)
Benefit: Single call handles both regular and CWL wars!

5. War Log (Historical Data)
Method: client.get_war_log(clan_tag, page=False, limit=0)
Use: Retrieve clan's war history
Returns: ClanWarLog with ClanWarLogEntry objects
Note: CWL entries have different attributes (totals for the season)