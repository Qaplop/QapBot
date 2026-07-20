from __future__ import annotations

from dataclasses import dataclass

import pytest


@pytest.mark.smoke
def test_calculate_content_hash_ignores_countdown_and_prediction_lines():
    from QBhelperfunctions import calculate_content_hash

    base = """```\nHeader\nWar ends in: 1h 2m\nThe confidence of this prediction is 99%\nBody\n```"""
    changed = """```\nHeader\nWar ends in: 9h 9m\nThe confidence of this prediction is 12%\nBody\n```"""
    assert calculate_content_hash(base) == calculate_content_hash(changed)


@pytest.mark.smoke
def test_merge_entries_respects_name_and_th_updates_and_war_in_progress_missed_attacks():
    from QBhelperfunctions import _merge_entries

    history_rows = [
        {
            "WarID": "W1",
            "Date": "2026-01-01T00:00",
            "Player": "OldName",
            "PlayerID": "#P1",
            "TH_lvl": 14,
            "Stars": 2,
            "Attacks": 1,
            "Missed_Attacks": 1,
            "Max_Attacks": 2,
            "Defensive_Stars": 0,
        },
        {
            "WarID": "W2",
            "Date": "2026-01-02T00:00",
            "Player": "NewName",
            "PlayerID": "#P1",
            "TH_lvl": 15,
            "Stars": 3,
            "Attacks": 2,
            "Missed_Attacks": 0,
            "Max_Attacks": 2,
            "Defensive_Stars": 1,
        },
    ]

    temp_stats = {
        "#P1": {
            "WarID": "W3",
            "Date": "2026-01-02T00:00",  # same day, TH should take max
            "Player": "NewestName",
            "PlayerID": "#P1",
            "TH_lvl": 16,
            "Stars": 6,
            "Attacks": 2,
            "Missed_Attacks": 2,  # must be ignored for ongoing war
            "Max_Attacks": 2,
            "Defensive_Stars": 2,
        }
    }

    merged = _merge_entries(history_rows, temp_stats, war_in_progress=True, mode="attack")
    assert "#P1" in merged
    p1 = merged["#P1"]
    # Same-date temp_stats updates TH via max(), but name changes only on strictly newer dates
    assert p1["Player"] == "NewName"
    assert p1["TH_lvl"] == 16
    assert p1["Stars"] == 2 + 3 + 6
    assert p1["Attacks"] == 1 + 2 + 2
    assert p1["Missed_Attacks"] == 1  # history only; temp ignored
    assert p1["Wars_Count"] == 3


@dataclass
class _FakeAttack:
    stars: int


@dataclass
class _FakeBestOppAttack:
    stars: int


@dataclass
class _FakeMember:
    tag: str
    name: str
    town_hall: int
    attacks: list[_FakeAttack]
    best_opponent_attack: _FakeBestOppAttack | None = None


@dataclass
class _FakeClan:
    members: list[_FakeMember]


@dataclass
class _FakeWar:
    clan: _FakeClan
    attacks_per_member: int = 2


@pytest.mark.smoke
def test_parse_war_stats_from_api_parses_basic_fields():
    from QBhelperfunctions import _parse_war_stats_from_api

    war = _FakeWar(
        clan=_FakeClan(
            members=[
                _FakeMember(
                    tag="#P1",
                    name="Alice",
                    town_hall=15,
                    attacks=[_FakeAttack(stars=3), _FakeAttack(stars=2)],
                    best_opponent_attack=_FakeBestOppAttack(stars=1),
                )
            ]
        )
    )

    out = _parse_war_stats_from_api(war, war_id="WID", start_dt_iso="2026-01-01T00:00")
    assert out["#P1"]["WarID"] == "WID"
    assert out["#P1"]["Player"] == "Alice"
    assert out["#P1"]["TH_lvl"] == 15
    assert out["#P1"]["Stars"] == 5
    assert out["#P1"]["Attacks"] == 2
    assert out["#P1"]["Missed_Attacks"] == 0
    assert out["#P1"]["Defensive_Stars"] == 1
