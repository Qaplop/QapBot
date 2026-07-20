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


class TestMaxPossibleStars:
    @pytest.mark.smoke
    def test_calculate_max_possible_stars_returns_non_negative(self, sample_war_data):
        from QBwarsim import calculate_max_possible_stars

        max_stars = calculate_max_possible_stars(sample_war_data, attacker_clan_tag="#CLAN1")
        assert isinstance(max_stars, int)
        assert max_stars >= 0
