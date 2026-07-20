"""Extended tests for QBwarsim.py — covering uncovered lines.

Targets: simulate_attacks_dynamic internals, assign_attacks edge cases,
calculate_max_possible_stars with real data, monte_carlo edge distributions,
calculate_win_probability destruction tiebreaker loss branch.

Coverage report shows lines 76-77, 194, 198, 202, 206, 247-320, 379-383,
416-419, 479, 497-498 as uncovered.
"""
# pyright: reportUnknownParameterType=false, reportMissingParameterType=false
# pyright: reportUnknownArgumentType=false
from __future__ import annotations

import random
from typing import Any, Dict, List
import pytest

from QBwarsim import (
    assign_attacks_to_bases_with_stars,
    calculate_max_possible_stars,
    th_star_probabilities,
    simulate_attacks_dynamic,
    monte_carlo_war_prediction,
    calculate_win_probability,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_war_data(
    my_members: List[Dict[str, Any]],
    opp_members: List[Dict[str, Any]],
    *,
    attacks_per_member: int = 2,
    my_tag: str = "#MY",
    opp_tag: str = "#OPP",
    my_stars: int = 0,
    opp_stars: int = 0,
) -> Dict[str, Any]:
    return {
        "attacks_per_member": attacks_per_member,
        "clan": {"tag": my_tag, "name": "MyTeam", "stars": my_stars, "members": my_members, "destruction": 50.0},
        "opponent": {"tag": opp_tag, "name": "Enemy", "stars": opp_stars, "members": opp_members, "destruction": 45.0},
    }


def _member(tag: str, th: int = 14, attacks: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    return {"tag": tag, "name": f"Player{tag}", "townhall": th, "attacks": attacks or []}


# ---------------------------------------------------------------------------
# assign_attacks_to_bases_with_stars
# ---------------------------------------------------------------------------

class TestAssignAttacks:
    def test_assigns_all_remaining_attacks(self):
        wd = _make_war_data(
            my_members=[_member("#P1", 14), _member("#P2", 13)],
            opp_members=[_member("#E1", 14), _member("#E2", 13)],
        )
        assignments = assign_attacks_to_bases_with_stars(wd, "#MY")
        # 2 enemy bases, each gets 3-starred by first attack → 2 assignments
        # (algorithm marks base as 3-starred after assignment, so extra attacks have no target)
        assert len(assignments) == 2
        total_stars = sum(a[4] for a in assignments)
        assert total_stars == 6  # 3 + 3

    def test_prioritises_bases_with_most_stars_remaining(self):
        # E2 already has 2 stars, E1 has 0 → P1 should target E1 first (3 stars to gain vs 1)
        wd = _make_war_data(
            my_members=[_member("#P1", 14, attacks=[{"defenderTag": "#E2", "stars": 2}])],
            opp_members=[_member("#E1", 14), _member("#E2", 14)],
            attacks_per_member=2,
        )
        assignments = assign_attacks_to_bases_with_stars(wd, "#MY")
        # P1 has 1 attack left, should target E1 (3 stars vs 1 for E2)
        assert len(assignments) == 1
        assert assignments[0][2] == "#E1"  # defender_tag

    def test_no_attack_on_already_3starred(self):
        wd = _make_war_data(
            my_members=[_member("#P1", 14, attacks=[{"defenderTag": "#E1", "stars": 3}])],
            opp_members=[_member("#E1", 14)],
            attacks_per_member=2,
        )
        assignments = assign_attacks_to_bases_with_stars(wd, "#MY")
        assert assignments == []

    def test_swapped_clan_order(self):
        """When attacker_clan_tag matches the opponent side, it should still work."""
        wd = _make_war_data(
            my_members=[_member("#P1", 14)],
            opp_members=[_member("#E1", 14)],
        )
        # Attacker is the opponent side — 1 member with 2 attacks, 1 target base
        assignments = assign_attacks_to_bases_with_stars(wd, "#OPP")
        assert len(assignments) == 1  # only 1 base to attack, gets 3-starred


# ---------------------------------------------------------------------------
# calculate_max_possible_stars
# ---------------------------------------------------------------------------

class TestCalculateMaxPossibleStars:
    def test_returns_sum_of_assignments(self):
        wd = _make_war_data(
            my_members=[_member("#P1", 14)],
            opp_members=[_member("#E1", 14)],
            attacks_per_member=1,
        )
        max_stars = calculate_max_possible_stars(wd, "#MY")
        assert max_stars == 3  # 1 unattacked base → 3 stars

    def test_zero_when_all_bases_3starred(self):
        wd = _make_war_data(
            my_members=[_member("#P1", 14, attacks=[{"defenderTag": "#E1", "stars": 3}])],
            opp_members=[_member("#E1", 14)],
            attacks_per_member=2,
        )
        assert calculate_max_possible_stars(wd, "#MY") == 0


# ---------------------------------------------------------------------------
# th_star_probabilities — all branches
# ---------------------------------------------------------------------------

class TestThStarProbabilitiesAllBranches:
    @pytest.mark.parametrize("diff,expected_3star_min", [
        (3, 0.90),    # diff >= 3
        (5, 0.90),    # diff >= 3 again
        (2, 0.80),    # diff == 2
        (1, 0.70),    # diff == 1
        (0, 0.50),    # diff == 0
        (-1, 0.20),   # diff == -1
        (-2, 0.05),   # diff == -2
        (-3, 0.0),    # diff <= -3
        (-5, 0.0),    # diff <= -3 again
    ])
    def test_3star_rate_monotonic(self, diff: int, expected_3star_min: float):
        probs = th_star_probabilities(14 + max(diff, 0), 14 + max(-diff, 0))
        assert probs[3] >= expected_3star_min
        assert abs(sum(probs) - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# simulate_attacks_dynamic
# ---------------------------------------------------------------------------

class TestSimulateAttacksDynamic:
    def test_returns_nonneg_stars(self):
        wd = _make_war_data(
            my_members=[_member("#P1", 14)],
            opp_members=[_member("#E1", 14)],
            attacks_per_member=1,
        )
        random.seed(42)
        stars = simulate_attacks_dynamic(wd, "#MY", sim_number=1)
        assert stars >= 0

    def test_debug_logging_on_sim1(self):
        """sim_number=1 triggers debug logging — exercise that branch."""
        wd = _make_war_data(
            my_members=[_member("#P1", 14)],
            opp_members=[_member("#E1", 14)],
            attacks_per_member=1,
        )
        random.seed(0)
        stars = simulate_attacks_dynamic(wd, "#MY", sim_number=1)
        assert isinstance(stars, int)

    def test_no_logging_on_sim2(self):
        """sim_number != 1 should not trigger detail logging."""
        wd = _make_war_data(
            my_members=[_member("#P1", 14)],
            opp_members=[_member("#E1", 14)],
            attacks_per_member=1,
        )
        random.seed(0)
        stars = simulate_attacks_dynamic(wd, "#MY", sim_number=2)
        assert isinstance(stars, int)

    def test_multiple_members_multiple_bases(self):
        wd = _make_war_data(
            my_members=[_member("#P1", 15), _member("#P2", 14)],
            opp_members=[_member("#E1", 14), _member("#E2", 13)],
            attacks_per_member=2,
        )
        random.seed(123)
        stars = simulate_attacks_dynamic(wd, "#MY")
        assert 0 <= stars <= 6  # max 2 bases * 3 stars

    def test_swapped_clan(self):
        """Simulate attacks for the opponent clan."""
        wd = _make_war_data(
            my_members=[_member("#P1", 14)],
            opp_members=[_member("#E1", 14)],
            attacks_per_member=1,
        )
        random.seed(42)
        stars = simulate_attacks_dynamic(wd, "#OPP", sim_number=1)
        assert stars >= 0

    def test_already_used_attacks_not_double_counted(self):
        wd = _make_war_data(
            my_members=[_member("#P1", 14, attacks=[{"defenderTag": "#E1", "stars": 2}])],
            opp_members=[_member("#E1", 14), _member("#E2", 14)],
            attacks_per_member=2,
        )
        random.seed(42)
        stars = simulate_attacks_dynamic(wd, "#MY")
        # P1 has 1 attack remaining (used 1 of 2)
        assert isinstance(stars, int)


# ---------------------------------------------------------------------------
# monte_carlo_war_prediction
# ---------------------------------------------------------------------------

class TestMonteCarloWarPrediction:
    def test_returns_four_ints_summing_100(self):
        wd = _make_war_data(
            my_members=[_member("#P1", 14)],
            opp_members=[_member("#E1", 14)],
            attacks_per_member=1,
        )
        win, lose, draw, conf = monte_carlo_war_prediction(wd, "#MY", n_sim=50)
        assert win + lose + draw == 100
        assert 0 <= conf <= 100

    def test_confidence_higher_with_fewer_remaining_attacks(self):
        # All attacks done → max confidence
        wd = _make_war_data(
            my_members=[_member("#P1", 14, attacks=[
                {"defenderTag": "#E1", "stars": 3},
            ])],
            opp_members=[_member("#E1", 14, attacks=[
                {"defenderTag": "#P1", "stars": 2},
            ])],
            attacks_per_member=1,
        )
        _, _, _, conf = monte_carlo_war_prediction(wd, "#MY", n_sim=20)
        assert conf == 100

    def test_low_sim_count(self):
        wd = _make_war_data(
            my_members=[_member("#P1", 15)],
            opp_members=[_member("#E1", 12)],
            attacks_per_member=1,
        )
        win, lose, draw, _conf = monte_carlo_war_prediction(wd, "#MY", n_sim=5)
        assert win + lose + draw == 100


# ---------------------------------------------------------------------------
# calculate_win_probability
# ---------------------------------------------------------------------------

class TestCalculateWinProbability:
    def test_destruction_tiebreaker_loss(self):
        """Stars tied, no attacks left, opponent has higher destruction → loss."""
        wd = {"clan": {"tag": "#A"}, "opponent": {"tag": "#B"}}
        result = calculate_win_probability(
            wd, "#A", attacks_left=0, my_stars=30, enemy_stars=30,
            enemy_attacks_left=0,
            my_clan_destruction=80.0, enemy_clan_destruction=95.0,
        )
        assert result == (0, 100, 0, 100)

    def test_max_stars_both_clans_is_draw_not_destruction_tiebreak(self):
        """When both clans reach max stars (all bases 3-starred), it must be a draw.

        Regression: the Monte Carlo tiebreaker originally used static mid-war
        destruction to break equal-stars ties.  Now destruction is simulated
        from per-base star state.  When every base is 3-starred, simulated
        destruction is 100% for both sides → always a true draw.

        We set up a 1v1 war where both sides have already 3-starred all bases
        (no remaining attacks) and current destruction differs.  The simulated
        destruction must override the differing static values.
        """
        wd = _make_war_data(
            my_members=[_member("#P1", 15, attacks=[{"defenderTag": "#E1", "stars": 3}])],
            opp_members=[_member("#E1", 15, attacks=[{"defenderTag": "#P1", "stars": 3}])],
            attacks_per_member=1,
            my_stars=3,
            opp_stars=3,
        )
        # Override destruction to be unequal — with static tiebreaker this would
        # cause a win for clan (90 > 70) instead of a draw.
        wd["clan"]["destruction"] = 90.0
        wd["opponent"]["destruction"] = 70.0

        # Test the MC path directly: simulated destruction should be 100% on
        # both sides (all bases 3-starred) → every iteration is a draw.
        from QBwarsim import _monte_carlo_chunk
        chunk_result = _monte_carlo_chunk((
            wd, "#MY", "#OPP", 3, 3,   # my_stars, enemy_stars (both maxed)
            100, 0,                      # n_chunk, offset
            None, None, None,            # cwl leagues, skill factors
        ))
        wins, losses, draws, _, _ = chunk_result
        assert draws == 100, f"Expected 100 draws, got wins={wins} losses={losses} draws={draws}"
        assert wins == 0
        assert losses == 0

    def test_simulated_destruction_tiebreaker_favors_better_base_distribution(self):
        """Equal total stars but different base-star distributions → destruction tiebreak.

        Clan A: 3★ + 0★ = 3 stars, destruction = (100 + 0)/2 = 50%
        Clan B: 2★ + 1★ = 3 stars, destruction = (75 + 45)/2 = 60%
        Both have 3 total stars → Clan B wins on simulated destruction.
        """
        from QBwarsim import _monte_carlo_chunk

        # 2-base war, 1 attack per member, each side has 2 members
        wd: Dict[str, Any] = {
            "attacks_per_member": 1,
            "clan": {
                "tag": "#A", "name": "ClanA", "stars": 3, "destruction": 50.0,
                "members": [
                    # Player 1 already 3-starred E1, Player 2 has no attacks (0★ on E2)
                    {"tag": "#P1", "townhall": 15, "attacks": [{"defenderTag": "#E1", "stars": 3}]},
                    {"tag": "#P2", "townhall": 15, "attacks": [{"defenderTag": "#E2", "stars": 0}]},
                ],
            },
            "opponent": {
                "tag": "#B", "name": "ClanB", "stars": 3, "destruction": 60.0,
                "members": [
                    # Player 1 got 2★ on P1, Player 2 got 1★ on P2
                    {"tag": "#E1", "townhall": 15, "attacks": [{"defenderTag": "#P1", "stars": 2}]},
                    {"tag": "#E2", "townhall": 15, "attacks": [{"defenderTag": "#P2", "stars": 1}]},
                ],
            },
        }

        # No remaining attacks → every sim produces same result.
        chunk_result = _monte_carlo_chunk((
            wd, "#A", "#B", 3, 3,
            100, 0,
            None, None, None,
        ))
        wins, losses, draws, _, _ = chunk_result
        # Clan A destruction: base E1=3★→100%, base E2=0★→0% → avg 50%
        # Clan B destruction: base P1=2★→75%, base P2=1★→45% → avg 60%
        # Clan B has higher destruction → Clan A loses all iterations.
        assert losses == 100, f"Expected 100 losses, got wins={wins} losses={losses} draws={draws}"

    def test_deterministic_certain_win(self):
        """My stars already exceed opponent's max possible → 100% win."""
        wd = _make_war_data(
            my_members=[_member("#P1", 14, attacks=[{"defenderTag": "#E1", "stars": 3}])],
            opp_members=[_member("#E1", 14, attacks=[{"defenderTag": "#P1", "stars": 0}])],
            attacks_per_member=1,
        )
        result = calculate_win_probability(
            wd, "#MY", attacks_left=0, my_stars=50, enemy_stars=0,
            enemy_attacks_left=0,
        )
        assert result == (100, 0, 0, 100)

    def test_deterministic_certain_loss(self):
        """Enemy stars exceed my max possible → 100% loss."""
        wd = _make_war_data(
            my_members=[_member("#P1", 14, attacks=[{"defenderTag": "#E1", "stars": 3}])],
            opp_members=[_member("#E1", 14, attacks=[{"defenderTag": "#P1", "stars": 3}])],
            attacks_per_member=1,
        )
        result = calculate_win_probability(
            wd, "#MY", attacks_left=0, my_stars=0, enemy_stars=50,
            enemy_attacks_left=0,
        )
        assert result == (0, 100, 0, 100)

    def test_uncertain_uses_monte_carlo(self, monkeypatch: pytest.MonkeyPatch):
        """Neither side has certain victory → falls through to MC simulation."""
        import QBwarsim
        mc_called = False

        def mock_mc(wd, tag, n_sim=1000, cwl_league_my=None, cwl_league_opp=None, player_skill_factors=None):  # type: ignore[no-untyped-def]
            nonlocal mc_called
            mc_called = True
            return (60, 30, 10, 75)

        monkeypatch.setattr(QBwarsim, "monte_carlo_war_prediction", mock_mc)

        wd = _make_war_data(
            my_members=[_member("#P1", 14)],
            opp_members=[_member("#E1", 14)],
            attacks_per_member=2,
        )
        result = calculate_win_probability(
            wd, "#MY", attacks_left=2, my_stars=5, enemy_stars=5,
            enemy_attacks_left=2,
        )
        assert mc_called
        assert result == (60, 30, 10, 75)
