import pytest


@pytest.mark.smoke
def test_constants_exist_and_types_are_sane():
    from qapbot import constants

    assert isinstance(constants.DISCORD_MESSAGE_MAX_LENGTH, int)
    assert constants.DISCORD_MESSAGE_MAX_LENGTH > 0

    assert isinstance(constants.COC_CLAN_CACHE_TTL_SECONDS, int)
    assert constants.COC_CLAN_CACHE_TTL_SECONDS > 0

    assert isinstance(constants.SECONDS_PER_DAY, int)
    assert constants.SECONDS_PER_DAY == 24 * 60 * 60


class TestNormalizeCwlSeason:
    """normalize_cwl_season must disambiguate month-boundary sign-up snapshots
    (regular monthly CWL) from genuine mid-month bonus CWL events."""

    def test_empty_string_unchanged(self):
        from qapbot.constants import normalize_cwl_season
        assert normalize_cwl_season("") == ""

    def test_regular_yyyy_mm_unchanged(self):
        from qapbot.constants import normalize_cwl_season
        assert normalize_cwl_season("2026-05") == "2026-05"

    def test_month_end_boundary_rolls_to_next_month(self):
        """Regression: the July CWL sign-up snapshot was mislabeled '2026-06-29'."""
        from qapbot.constants import normalize_cwl_season
        assert normalize_cwl_season("2026-06-29") == "2026-07"

    def test_december_month_end_boundary_rolls_to_next_year(self):
        from qapbot.constants import normalize_cwl_season
        assert normalize_cwl_season("2026-12-28") == "2027-01"

    def test_month_start_boundary_stays_in_month(self):
        from qapbot.constants import normalize_cwl_season
        assert normalize_cwl_season("2026-07-02") == "2026-07"
        assert normalize_cwl_season("2026-07-06") == "2026-07"

    def test_genuine_mid_month_snaps_to_monday(self):
        from qapbot.constants import normalize_cwl_season
        assert normalize_cwl_season("2026-06-16") == "2026-06-15"
        assert normalize_cwl_season("2026-06-17") == "2026-06-15"

    def test_unparseable_date_returned_unchanged(self):
        from qapbot.constants import normalize_cwl_season
        assert normalize_cwl_season("2026-13-99") == "2026-13-99"
