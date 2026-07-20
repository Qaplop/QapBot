"""Reusable test data builders for QapBot tests.

Provides factory functions for creating consistent test data structures
used across unit, integration, and discord test suites. These builders
ensure test data matches the expected shapes from CACHE, db_manager,
and war stat processing functions.

Usage:
    from tests.builders import make_war_member, make_war_data, make_user_account
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# War statistics builders (QBhelperfunctions / formatting)
# ---------------------------------------------------------------------------

def make_player_stats(
    *,
    tag: str = "#P1",
    name: str = "Player1",
    th: int = 15,
    stars: int = 5,
    attacks: int = 2,
    missed: int = 0,
    defensive_stars: int = 2,
    wars_count: int = 1,
    defs_count: int | None = None,
) -> Dict[str, Any]:
    """Build a merged player stats dict as produced by _merge_entries / calculate_leaderboard."""
    # defs_count defaults to wars_count when not specified (matches old behaviour)
    effective_defs = defs_count if defs_count is not None else wars_count
    def_per_war = (defensive_stars / wars_count) if wars_count else 0.0
    stars_per_def = (defensive_stars / effective_defs) if effective_defs else 0.0
    return {
        "Player": name,
        "PlayerID": tag,
        "TH_lvl": th,
        "Stars": stars,
        "Attacks": attacks,
        "Missed_Attacks": missed,
        "Defensive_Stars": defensive_stars,
        "Wars_Count": wars_count,
        "Defs_Count": effective_defs,
        "Def_Stars_per_War": round(def_per_war, 2),
        "Stars_per_Def": round(stars_per_def, 2),
    }


def make_history_row(
    *,
    war_id: str = "OPP_202601010000",
    date: str = "2026-01-01T00:00",
    tag: str = "#P1",
    name: str = "Player1",
    th: int = 15,
    stars: int = 3,
    attacks: int = 2,
    missed: int = 0,
    max_attacks: int = 2,
    defensive_stars: int = 1,
) -> Dict[str, Any]:
    """Build a single war history row as returned by db_manager / _load_history_filtered."""
    return {
        "WarID": war_id,
        "Date": date,
        "Player": name,
        "PlayerID": tag,
        "TH_lvl": th,
        "Stars": stars,
        "Attacks": attacks,
        "Missed_Attacks": missed,
        "Max_Attacks": max_attacks,
        "Defensive_Stars": defensive_stars,
        "Times_Defended": 1 if defensive_stars > 0 else 0,
    }


def make_temp_stats(
    *,
    war_id: str = "OPP_202601020000",
    date: str = "2026-01-02T00:00",
    tag: str = "#P1",
    name: str = "Player1",
    th: int = 15,
    stars: int = 6,
    attacks: int = 2,
    missed: int = 0,
    max_attacks: int = 2,
    defensive_stars: int = 2,
) -> Dict[str, Any]:
    """Build a temp_stats entry (current war) as produced by _load_war_data_from_json."""
    return {
        "WarID": war_id,
        "Date": date,
        "Player": name,
        "PlayerID": tag,
        "TH_lvl": th,
        "Stars": stars,
        "Attacks": attacks,
        "Missed_Attacks": missed,
        "Max_Attacks": max_attacks,
        "Defensive_Stars": defensive_stars,
    }


# ---------------------------------------------------------------------------
# JSON war file builders (QBcsvhandling)
# ---------------------------------------------------------------------------

def make_war_json(
    *,
    state: str = "in_war",
    attacks_per_member: int = 2,
    start_time: str = "<Timestamp time=datetime.datetime(2026, 1, 1, 0, 0, 0) seconds_until=-1>",
    clan_tag: str = "#CLAN1",
    clan_name: str = "TestClan",
    clan_members: Optional[List[Dict[str, Any]]] = None,
    opponent_tag: str = "#OPP",
    opponent_name: str = "Opponent",
    opponent_members: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build a JSON war data dict as stored in data/temp/*.json files."""
    if clan_members is None:
        clan_members = [make_war_json_member()]
    if opponent_members is None:
        opponent_members = []
    return {
        "state": state,
        "attacks_per_member": attacks_per_member,
        "start_time": start_time,
        "clan": {
            "tag": clan_tag,
            "name": clan_name,
            "members": clan_members,
        },
        "opponent": {
            "tag": opponent_tag,
            "name": opponent_name,
            "members": opponent_members,
        },
    }


def make_war_json_member(
    *,
    tag: str = "#P1",
    name: str = "Alice",
    townhall: int = 15,
    attacks: Optional[List[Dict[str, Any]]] = None,
    best_opponent_attack: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """Build a member dict inside a JSON war file."""
    member: Dict[str, Any] = {
        "tag": tag,
        "name": name,
        "townhall": townhall,
    }
    if attacks is not None:
        member["attacks"] = attacks
    else:
        member["attacks"] = [{"stars": 3}]
    if best_opponent_attack is not None:
        member["bestOpponentAttack"] = best_opponent_attack
    else:
        member["bestOpponentAttack"] = {"stars": 2}
    return member


# ---------------------------------------------------------------------------
# User account builders (QBdiscocmdshelper / cache_manager)
# ---------------------------------------------------------------------------

def make_user_account(
    *,
    display_name: str = "TestUser",
    players: Optional[List[Dict[str, Any]]] = None,
    language: str = "en",
    war_reminders: bool = True,
) -> Dict[str, Any]:
    """Build a user_accounts entry as stored in CACHE.user_accounts."""
    if players is None:
        players = []
    return {
        "display_name": display_name,
        "notification_settings": {"war_reminders": war_reminders},
        "players": players,
        "user_language": language,
    }


def make_player_link(
    *,
    tag: str = "#P1",
    name: str = "Player1",
    verified: bool = False,
    clan_tag: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a player entry inside a user account."""
    entry: Dict[str, Any] = {
        "player_tag": tag,
        "player_name": name,
        "verified": verified,
    }
    if clan_tag:
        entry["current_clan_tag"] = clan_tag
    return entry


# ---------------------------------------------------------------------------
# coc.py-style fake objects (for _parse_war_stats_from_api)
# ---------------------------------------------------------------------------

@dataclass
class FakeAttack:
    """Minimal coc.WarAttack stand-in."""
    stars: int = 3
    attacker_tag: str = "#P1"
    defender_tag: str = "#E1"
    destruction: float = 100.0
    order: int = 1


@dataclass
class FakeBestOppAttack:
    """Minimal best-opponent-attack stand-in."""
    stars: int = 1


@dataclass
class FakeWarMember:
    """Minimal coc.WarMember stand-in."""
    tag: str = "#P1"
    name: str = "Player1"
    town_hall: int = 15
    attacks: List[FakeAttack] = field(default_factory=lambda: [])  # pyright: ignore[reportUnknownLambdaType]
    best_opponent_attack: Optional[FakeBestOppAttack] = None
    map_position: int = 1


@dataclass
class FakeWarClan:
    """Minimal coc.WarClan stand-in."""
    tag: str = "#CLAN1"
    name: str = "TestClan"
    members: List[FakeWarMember] = field(default_factory=lambda: [])  # pyright: ignore[reportUnknownLambdaType]


@dataclass
class FakeWar:
    """Minimal coc.War stand-in."""
    clan: FakeWarClan = field(default_factory=FakeWarClan)
    attacks_per_member: int = 2
    state: str = "inWar"
    team_size: int = 1
