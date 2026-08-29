import pytest


class TestThStarProbabilities:
    @pytest.mark.smoke
    @pytest.mark.parametrize(
        "atk_th,def_th",
        [
            (16, 16),
            (16, 14),
            (14, 16),
            (8, 8),
        ],
    )
    def test_probabilities_sum_to_one(self, atk_th, def_th):
        from QBwarsim import th_star_probabilities

        probs = th_star_probabilities(atk_th, def_th)
        assert len(probs) == 4
        assert abs(sum(probs) - 1.0) < 1e-9

    @pytest.mark.smoke
    def test_higher_th_has_better_3star_rate(self):
        from QBwarsim import th_star_probabilities

        higher = th_star_probabilities(16, 14)
        equal = th_star_probabilities(14, 14)
        assert higher[3] >= equal[3]


class TestCwlLeagueStarDistribution:
    """Tracker #0048: this table is hand-transcribed from a clashspot.net screenshot each time
    CoC's season resets, so it has no other guard against a row being mistyped or a league
    dropped — these checks exist to catch exactly that."""

    @pytest.mark.smoke
    def test_covers_every_league_in_the_ladder(self):
        from QBwarsim import CWL_LEAGUE_STAR_DISTRIBUTION
        from qapbot.constants import CWL_LEAGUE_ORDER

        assert set(CWL_LEAGUE_STAR_DISTRIBUTION.keys()) == set(CWL_LEAGUE_ORDER)

    @pytest.mark.smoke
    @pytest.mark.parametrize("league_name", [
        "Legend League", "Titan League I", "Titan League II", "Titan League III",
        "Champion League I", "Champion League II", "Champion League III",
        "Master League I", "Master League II", "Master League III",
        "Crystal League I", "Crystal League II", "Crystal League III",
        "Gold League I", "Gold League II", "Gold League III",
        "Silver League I", "Silver League II", "Silver League III",
        "Bronze League I", "Bronze League II", "Bronze League III",
    ])
    def test_row_sums_to_one(self, league_name):
        from QBwarsim import get_league_star_probs

        probs = get_league_star_probs(league_name)
        assert probs is not None
        assert len(probs) == 4
        assert abs(sum(probs) - 1.0) < 1e-3


class TestMaxPossibleStars:
    @pytest.mark.smoke
    def test_calculate_max_possible_stars_returns_non_negative(self, sample_war_data):
        from QBwarsim import calculate_max_possible_stars

        max_stars = calculate_max_possible_stars(sample_war_data, attacker_clan_tag="#CLAN1")
        assert isinstance(max_stars, int)
        assert max_stars >= 0
