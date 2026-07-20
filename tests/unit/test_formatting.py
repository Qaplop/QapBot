import pytest


class TestTextDisplayWidth:
    @pytest.mark.smoke
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("hello", 5),
            ("", 0),
            ("A" * 100, 100),
        ],
    )
    def test_ascii_width(self, text, expected):
        from qapbot.formatting import text_display_width

        assert text_display_width(text) == expected


class TestTruncateToWidth:
    @pytest.mark.smoke
    def test_truncate_respects_target_width(self):
        from qapbot.formatting import truncate_to_width, text_display_width

        out = truncate_to_width("abcdefghij", 5)
        assert text_display_width(out) <= 5
        assert out == "abcde"


class TestRightPadNumber:
    @pytest.mark.smoke
    def test_right_pad_number_pads_left(self):
        from qapbot.formatting import right_pad_number

        assert right_pad_number(42, 5) == "   42"


class TestNormalizePlayerName:
    @pytest.mark.smoke
    def test_normalize_trims_and_collapses_spaces(self):
        from qapbot.formatting import normalize_player_name

        assert normalize_player_name("  a   b  ") == "a b"

    @pytest.mark.smoke
    def test_normalize_empty_passthrough(self):
        from qapbot.formatting import normalize_player_name

        assert normalize_player_name("") == ""
