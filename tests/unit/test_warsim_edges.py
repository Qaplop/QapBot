from __future__ import annotations

from typing import Any

import pytest


@pytest.mark.smoke
def test_assign_attacks_returns_empty_when_no_attackable_bases():
    from QBwarsim import assign_attacks_to_bases_with_stars

    war_data: dict[str, Any] = {
        "attacks_per_member": 1,
        "clan": {
            "tag": "#A",
            "members": [{"tag": "#P1", "townhall": 15, "attacks": []}],
        },
        "opponent": {
            "tag": "#B",
            "members": [{"tag": "#E1", "townhall": 15}],
        },
    }

    # Base already 3-starred by existing attacks, so no assignments should remain.
    war_data["clan"]["members"][0]["attacks"] = [{"defenderTag": "#E1", "stars": 3}]

    assignments = assign_attacks_to_bases_with_stars(war_data, "#A")
    assert assignments == []


@pytest.mark.smoke
def test_calculate_win_probability_uses_destruction_tiebreaker_win():
    from QBwarsim import calculate_win_probability

    result = calculate_win_probability(
        war_data={"clan": {"tag": "#A"}, "opponent": {"tag": "#B"}},
        clan_tag="#A",
        attacks_left=0,
        my_stars=30,
        enemy_stars=30,
        enemy_attacks_left=0,
        my_clan_destruction=95.4,
        enemy_clan_destruction=92.1,
    )

    assert result == (100, 0, 0, 100)


@pytest.mark.smoke
def test_calculate_win_probability_uses_destruction_tiebreaker_draw():
    from QBwarsim import calculate_win_probability

    result = calculate_win_probability(
        war_data={"clan": {"tag": "#A"}, "opponent": {"tag": "#B"}},
        clan_tag="#A",
        attacks_left=0,
        my_stars=25,
        enemy_stars=25,
        enemy_attacks_left=0,
        my_clan_destruction=88.0,
        enemy_clan_destruction=88.0,
    )

    assert result == (0, 0, 100, 100)


@pytest.mark.smoke
def test_calculate_win_probability_deterministic_branches(monkeypatch: pytest.MonkeyPatch):
    import QBwarsim

    def _calculate_max_possible_stars(_war: dict[str, Any], tag: str) -> int:
        return 0 if tag == "#A" else 0

    monkeypatch.setattr(QBwarsim, "calculate_max_possible_stars", _calculate_max_possible_stars)

    war_data = {"clan": {"tag": "#A"}, "opponent": {"tag": "#B"}}

    assert QBwarsim.calculate_win_probability(war_data, "#A", 1, 10, 0, 1) == (100, 0, 0, 100)
    assert QBwarsim.calculate_win_probability(war_data, "#A", 1, 0, 10, 1) == (0, 100, 0, 100)


@pytest.mark.smoke
def test_calculate_win_probability_both_max_stars_certain_draw(monkeypatch: pytest.MonkeyPatch):
    """Both clans already at max stars with attacks still remaining → certain draw.

    Regression: with unused attacks left, attacks_left != 0 so the early
    'all attacks done' path was skipped. The code then fell into MC simulation,
    which correctly returned ~98% draw but never 100%. When all enemy bases are
    already 3-starred, no further stars can be gained — the outcome is certain.
    """
    import QBwarsim

    # Simulate a 10-player war where both clans have 30/30 stars and 6/3 attacks
    # left (late attackers haven't gone), matching the screenshot scenario.
    monkeypatch.setattr(QBwarsim, "calculate_max_possible_stars", lambda _w, _t: 0)

    war_data = {"clan": {"tag": "#A"}, "opponent": {"tag": "#B"}}

    result = QBwarsim.calculate_win_probability(
        war_data, "#A",
        attacks_left=6, my_stars=30,
        enemy_stars=30, enemy_attacks_left=3,
    )

    assert result == (0, 0, 100, 100), f"Expected certain draw, got {result}"


@pytest.mark.smoke
def test_monte_carlo_probabilities_are_clamped_and_sum_100(monkeypatch: pytest.MonkeyPatch):
    import QBwarsim

    def _simulate(_war_data: dict[str, Any], attacker_tag: str, sim_number: int = 0, cwl_league: Any = None, player_skill_factors: Any = None) -> int:
        _ = sim_number
        return 1 if attacker_tag == "#A" else 0

    monkeypatch.setattr(QBwarsim, "simulate_attacks_dynamic", _simulate)

    war_data: dict[str, Any] = {
        "attacks_per_member": 2,
        "clan": {"tag": "#A", "stars": 0, "members": [{"attacks": []}]},
        "opponent": {"tag": "#B", "stars": 0, "members": [{"attacks": []}]},
    }

    win, lose, draw, confidence = QBwarsim.monte_carlo_war_prediction(war_data, "#A", n_sim=10)

    assert win + lose + draw == 100
    assert min(win, lose, draw) >= 1
    assert 0 <= confidence <= 100
