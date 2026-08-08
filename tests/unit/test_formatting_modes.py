"""Comprehensive tests for formatting.py: MODE_REGISTRY, render_leaderboard, width functions.

Covers all leaderboard rendering modes, player name normalization edge cases,
best_practice_player_cell padding, and column rendering for Discord/terminal output.
"""
# pyright: reportPrivateUsage=false
from __future__ import annotations

from typing import Any, Dict

import pytest

from tests.builders import make_player_stats


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stats_dict(*players: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Convert list of player stats into the {PlayerID: stats} dict render_leaderboard expects."""
    return {p["PlayerID"]: p for p in players}


# ---------------------------------------------------------------------------
# render_leaderboard – parametrized across all modes
# ---------------------------------------------------------------------------

ALL_MODES = [
    "currentwar",
    "attack",
    "avgstars",
    "avgstarsbyth",
    "attackdefratio",
    "defense",
    "missedattacks",
    "attack_cwl",
    "avgstars_cwl",
    "attackdefratio_cwl",
    "defense_cwl",
    "missedattacks_cwl",
]


# ---------------------------------------------------------------------------
# apply_cwl_mode_suffix — /leaderboard cwl_only suffix guard
# ---------------------------------------------------------------------------
# Regression: forcing cwl_only=True (e.g. via the `season` option) on an
# inherently-CWL mode like "cwlgroup" must NOT append "_cwl", since no
# "cwlgroup_cwl" entry exists in MODE_REGISTRY. Doing so previously caused
# /leaderboard mode=cwlgroup season=2026-06 to fall through to the generic
# image/text leaderboard renderer instead of the CWL group-standings image.

class TestApplyCwlModeSuffix:
    @pytest.mark.parametrize("mode", ["cwlgroup", "cwlinfo", "cwlinfo_comp", "currentwar"])
    def test_no_cwl_suffix_modes_unchanged_when_cwl_only(self, mode):
        from qapbot.formatting import apply_cwl_mode_suffix
        assert apply_cwl_mode_suffix(mode, cwl_only=True) == mode

    @pytest.mark.parametrize("mode", ["cwlgroup", "cwlinfo", "cwlinfo_comp", "currentwar"])
    def test_no_cwl_suffix_modes_unchanged_when_not_cwl_only(self, mode):
        from qapbot.formatting import apply_cwl_mode_suffix
        assert apply_cwl_mode_suffix(mode, cwl_only=False) == mode

    def test_regular_mode_gets_cwl_suffix_when_cwl_only(self):
        from qapbot.formatting import apply_cwl_mode_suffix
        assert apply_cwl_mode_suffix("attack", cwl_only=True) == "attack_cwl"

    def test_regular_mode_unchanged_when_not_cwl_only(self):
        from qapbot.formatting import apply_cwl_mode_suffix
        assert apply_cwl_mode_suffix("attack", cwl_only=False) == "attack"

    def test_already_suffixed_mode_not_double_suffixed(self):
        from qapbot.formatting import apply_cwl_mode_suffix
        assert apply_cwl_mode_suffix("attack_cwl", cwl_only=True) == "attack_cwl"

    def test_all_no_cwl_suffix_modes_exist_in_registry_without_variant(self):
        """Guard against drift: every mode in NO_CWL_SUFFIX_MODES must exist in
        MODE_REGISTRY, and none of them should have a "<mode>_cwl" counterpart."""
        from qapbot.formatting import MODE_REGISTRY, NO_CWL_SUFFIX_MODES
        for mode in NO_CWL_SUFFIX_MODES:
            assert mode in MODE_REGISTRY
            assert f"{mode}_cwl" not in MODE_REGISTRY


class TestRenderLeaderboardModes:
    """Parametrized coverage for every MODE_REGISTRY entry."""

    @pytest.mark.smoke
    @pytest.mark.parametrize("mode", ALL_MODES)
    def test_render_produces_nonempty_output(self, mode: str) -> None:
        from qapbot.formatting import render_leaderboard

        players = _stats_dict(
            make_player_stats(tag="#P1", name="Alice", stars=6, attacks=2, missed=0, defensive_stars=3, wars_count=2),
            make_player_stats(tag="#P2", name="Bob", stars=4, attacks=2, missed=1, defensive_stars=5, wars_count=2),
        )
        result = render_leaderboard(
            clan_tag="#CLAN1",
            clan_name="TestClan",
            month_label=" for 01/2026",
            war_info_line="",
            stats_by_player=players,
            mode=mode,
        )
        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.smoke
    @pytest.mark.parametrize("mode", ALL_MODES)
    def test_render_contains_player_names(self, mode: str) -> None:
        from qapbot.formatting import render_leaderboard

        players = _stats_dict(
            make_player_stats(tag="#P1", name="Alpha", stars=5, attacks=2, defensive_stars=2, wars_count=1),
        )
        result = render_leaderboard(
            clan_tag="#C",
            clan_name="Clan",
            month_label="",
            war_info_line="",
            stats_by_player=players,
            mode=mode,
        )
        assert "Alpha" in result

    @pytest.mark.smoke
    @pytest.mark.parametrize("mode", ALL_MODES)
    def test_render_contains_header_and_separator(self, mode: str) -> None:
        from qapbot.formatting import render_leaderboard

        players = _stats_dict(
            make_player_stats(tag="#P1", name="X", stars=3, attacks=1),
        )
        result = render_leaderboard(
            clan_tag="#C",
            clan_name="Clan",
            month_label="",
            war_info_line="",
            stats_by_player=players,
            mode=mode,
        )
        assert "---" in result  # separator line
        assert "Player" in result or "TH" in result  # header columns

    @pytest.mark.smoke
    def test_render_terminal_style(self) -> None:
        from qapbot.formatting import render_leaderboard

        players = _stats_dict(
            make_player_stats(tag="#P1", name="TerminalUser", stars=3, attacks=1),
        )
        result = render_leaderboard(
            clan_tag="#C",
            clan_name="Clan",
            month_label="",
            war_info_line="",
            stats_by_player=players,
            mode="attack",
            style="terminal",
        )
        assert "TerminalUser" in result

    @pytest.mark.smoke
    def test_render_includes_war_info_line(self) -> None:
        from qapbot.formatting import render_leaderboard

        players = _stats_dict(make_player_stats())
        result = render_leaderboard(
            clan_tag="#C",
            clan_name="Clan",
            month_label="",
            war_info_line="🛡️ War in progress",
            stats_by_player=players,
            mode="currentwar",
        )
        assert "War in progress" in result

    @pytest.mark.smoke
    def test_render_includes_month_label(self) -> None:
        from qapbot.formatting import render_leaderboard

        players = _stats_dict(make_player_stats())
        result = render_leaderboard(
            clan_tag="#C",
            clan_name="Clan",
            month_label=" for 02/2026",
            war_info_line="",
            stats_by_player=players,
            mode="attack",
        )
        assert "02/2026" in result


class TestRenderLeaderboardGroupedByTH:
    """Tests for avgstarsbyth mode which groups players by TH level."""

    @pytest.mark.smoke
    def test_avgstarsbyth_shows_th_headers(self) -> None:
        from qapbot.formatting import render_leaderboard

        players = _stats_dict(
            make_player_stats(tag="#P1", name="TH16", th=16, stars=6, attacks=2),
            make_player_stats(tag="#P2", name="TH15", th=15, stars=4, attacks=2),
        )
        result = render_leaderboard(
            clan_tag="#C",
            clan_name="Clan",
            month_label="",
            war_info_line="",
            stats_by_player=players,
            mode="avgstarsbyth",
        )
        assert "TH16:" in result
        assert "TH15:" in result

    @pytest.mark.smoke
    def test_avgstarsbyth_orders_th_descending(self) -> None:
        from qapbot.formatting import render_leaderboard

        players = _stats_dict(
            make_player_stats(tag="#P1", name="Low", th=12, stars=4, attacks=2),
            make_player_stats(tag="#P2", name="High", th=17, stars=6, attacks=2),
        )
        result = render_leaderboard(
            clan_tag="#C",
            clan_name="Clan",
            month_label="",
            war_info_line="",
            stats_by_player=players,
            mode="avgstarsbyth",
        )
        th17_pos = result.index("TH17:")
        th12_pos = result.index("TH12:")
        assert th17_pos < th12_pos  # TH17 appears before TH12


class TestRenderLeaderboardSorting:
    """Verify sort order for specific modes."""

    @pytest.mark.smoke
    def test_attack_mode_sorts_by_stars_desc(self) -> None:
        from qapbot.formatting import render_leaderboard

        players = _stats_dict(
            make_player_stats(tag="#LOW", name="Low", stars=2, attacks=2),
            make_player_stats(tag="#HIGH", name="High", stars=9, attacks=2),
        )
        result = render_leaderboard("#C", "Clan", "", "", players, "attack")
        high_pos = result.index("High")
        low_pos = result.index("Low")
        assert high_pos < low_pos

    @pytest.mark.smoke
    def test_missedattacks_mode_sorts_by_missed_desc(self) -> None:
        from qapbot.formatting import render_leaderboard

        players = _stats_dict(
            make_player_stats(tag="#FEW", name="Few", missed=1, attacks=1),
            make_player_stats(tag="#MANY", name="Many", missed=4, attacks=0),
        )
        result = render_leaderboard("#C", "Clan", "", "", players, "missedattacks")
        many_pos = result.index("Many")
        few_pos = result.index("Few")
        assert many_pos < few_pos

    @pytest.mark.smoke
    def test_defense_mode_sorts_by_stars_per_def_asc(self) -> None:
        from qapbot.formatting import render_leaderboard

        players = _stats_dict(
            make_player_stats(tag="#GOOD", name="Good", defensive_stars=2, wars_count=2, defs_count=2),
            make_player_stats(tag="#BAD", name="Bad", defensive_stars=10, wars_count=2, defs_count=2),
        )
        result = render_leaderboard("#C", "Clan", "", "", players, "defense")
        good_pos = result.index("Good")
        bad_pos = result.index("Bad")
        assert good_pos < bad_pos  # fewer stars per defense first


class TestRenderLeaderboardEdgeCases:
    """Edge case coverage."""

    @pytest.mark.smoke
    def test_zero_attacks_avg_shows_zero(self) -> None:
        from qapbot.formatting import render_leaderboard

        players = _stats_dict(
            make_player_stats(tag="#P1", name="ZeroAtk", stars=0, attacks=0),
        )
        result = render_leaderboard("#C", "Clan", "", "", players, "currentwar")
        assert "0.00" in result

    @pytest.mark.smoke
    def test_attackdefratio_zero_defense(self) -> None:
        from qapbot.formatting import render_leaderboard

        players = _stats_dict(
            make_player_stats(tag="#P1", name="NoDef", stars=5, attacks=2, defensive_stars=0),
        )
        result = render_leaderboard("#C", "Clan", "", "", players, "attackdefratio")
        assert "-" in result  # ratio shows dash when no defensive stars

    @pytest.mark.smoke
    def test_unknown_mode_falls_back_to_default(self) -> None:
        from qapbot.formatting import render_leaderboard

        players = _stats_dict(make_player_stats())
        result = render_leaderboard("#C", "Clan", "", "", players, "nonexistent_mode")
        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.smoke
    def test_empty_clan_name_uses_tag(self) -> None:
        from qapbot.formatting import render_leaderboard

        players = _stats_dict(make_player_stats())
        result = render_leaderboard("#CLAN99", "", "", "", players, "attack")
        assert "#CLAN99" in result


# ---------------------------------------------------------------------------
# normalize_player_name – extended coverage
# ---------------------------------------------------------------------------

class TestNormalizePlayerNameExtended:

    @pytest.mark.smoke
    @pytest.mark.parametrize("raw,should_contain", [
        ("  hello  ", "hello"),
        ("A\u200dB", "AB"),          # ZWJ removed
        ("test\uFE0Fing", "testing"),  # VS16 removed
        ("em—dash", "em-dash"),       # em-dash normalized
        ("en–dash", "en-dash"),       # en-dash normalized
        ("☆star★", "⭐star⭐"),      # star consolidation
        ("♛king", "👑king"),          # crown consolidation
        ("❤️love", "♥love"),          # heart consolidation
    ])
    def test_normalize_special_chars(self, raw: str, should_contain: str) -> None:
        from qapbot.formatting import normalize_player_name

        result = normalize_player_name(raw)
        assert should_contain in result

    @pytest.mark.smoke
    def test_normalize_rtl_text_gets_bidi_isolate_marks(self) -> None:
        from qapbot.formatting import normalize_player_name

        result = normalize_player_name("عربي")
        # FSI (First Strong Isolate) ... PDI (Pop Directional Isolate) wrap the
        # RTL run so it cannot reorder surrounding LTR punctuation/emoji in
        # embeds, while still rendering the text in its own natural direction.
        assert "\u2068" in result
        assert "\u2069" in result

    @pytest.mark.smoke
    def test_normalize_pure_ascii_no_bidi_isolate_marks(self) -> None:
        from qapbot.formatting import normalize_player_name

        result = normalize_player_name("PureAscii")
        assert "\u2068" not in result
        assert "\u2069" not in result

    @pytest.mark.smoke
    def test_normalize_control_chars_removed(self) -> None:
        from qapbot.formatting import normalize_player_name

        result = normalize_player_name("te\x00st\x01name")
        assert "testname" in result

    @pytest.mark.smoke
    def test_normalize_collapses_exotic_spaces(self) -> None:
        from qapbot.formatting import normalize_player_name

        result = normalize_player_name("a\u2000\u2001\u2002b")
        assert result == "a b"

    @pytest.mark.smoke
    def test_normalize_empty_after_stripping_falls_back(self) -> None:
        from qapbot.formatting import normalize_player_name

        # String of only zero-width chars
        result = normalize_player_name("\u200d\u200c\uFEFF")
        # Should return something (fallback to original stripped)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# text_display_width / text_display_width_float
# ---------------------------------------------------------------------------

class TestDisplayWidthExtended:

    @pytest.mark.smoke
    @pytest.mark.parametrize("char,min_width", [
        ("\u3000", 1.5),       # Ideographic space
        ("\u2800", 1.0),       # Braille pattern blank
        ("⚡", 2.0),            # Lightning
        ("日", 1.5),            # CJK char
        ("A", 1.0),            # ASCII
    ])
    def test_special_char_widths(self, char: str, min_width: float) -> None:
        from qapbot.formatting import text_display_width_float

        w = text_display_width_float(char)
        assert w >= min_width

    @pytest.mark.smoke
    def test_float_width_empty_string(self) -> None:
        from qapbot.formatting import text_display_width_float

        assert text_display_width_float("") == 0.0

    @pytest.mark.smoke
    def test_zero_width_chars_contribute_nothing(self) -> None:
        from qapbot.formatting import text_display_width_float

        assert text_display_width_float("\u200d") == 0.0  # ZWJ
        assert text_display_width_float("\uFE0F") == 0.0  # VS16


# ---------------------------------------------------------------------------
# best_practice_player_cell
# ---------------------------------------------------------------------------

class TestBestPracticePlayerCell:

    @pytest.mark.smoke
    def test_ascii_name_exact_width(self) -> None:
        from qapbot.formatting import best_practice_player_cell, text_display_width_float

        cell = best_practice_player_cell("Alice", 17)
        w = text_display_width_float(cell)
        # Should be within 0.5 of target
        assert abs(w - 17) < 0.5, f"Width {w} not close to 17"

    @pytest.mark.smoke
    def test_long_name_truncated_to_target(self) -> None:
        from qapbot.formatting import best_practice_player_cell, text_display_width

        cell = best_practice_player_cell("A" * 30, 17)
        assert text_display_width(cell) <= 18  # at most 1 overshoot

    @pytest.mark.smoke
    def test_empty_name_produces_padded_cell(self) -> None:
        from qapbot.formatting import best_practice_player_cell, text_display_width

        cell = best_practice_player_cell("", 17)
        assert text_display_width(cell) >= 16  # adequately padded

    @pytest.mark.smoke
    def test_cjk_name_respects_width(self) -> None:
        from qapbot.formatting import best_practice_player_cell, text_display_width

        cell = best_practice_player_cell("日本語テスト", 17)
        w = text_display_width(cell)
        assert w <= 18


# ---------------------------------------------------------------------------
# right_pad_number
# ---------------------------------------------------------------------------

class TestRightPadNumberExtended:

    @pytest.mark.smoke
    @pytest.mark.parametrize("val,width,expected", [
        (0, 5, "    0"),
        (999, 3, "999"),
        (1.5, 6, "   1.5"),
        ("N/A", 5, "  N/A"),
    ])
    def test_various_inputs(self, val: Any, width: int, expected: str) -> None:
        from qapbot.formatting import right_pad_number

        assert right_pad_number(val, width) == expected

    @pytest.mark.smoke
    def test_value_wider_than_width(self) -> None:
        from qapbot.formatting import right_pad_number

        result = right_pad_number(12345, 3)
        assert result == "12345"  # no truncation, just returned as-is


# ---------------------------------------------------------------------------
# truncate_to_width
# ---------------------------------------------------------------------------

class TestTruncateToWidthExtended:

    @pytest.mark.smoke
    def test_short_string_unchanged(self) -> None:
        from qapbot.formatting import truncate_to_width

        assert truncate_to_width("hi", 10) == "hi"

    @pytest.mark.smoke
    def test_exact_width_unchanged(self) -> None:
        from qapbot.formatting import truncate_to_width

        assert truncate_to_width("abcde", 5) == "abcde"

    @pytest.mark.smoke
    def test_cjk_truncation_respects_double_width(self) -> None:
        from qapbot.formatting import truncate_to_width, text_display_width

        result = truncate_to_width("日本語テスト", 5)
        assert text_display_width(result) <= 5


# ---------------------------------------------------------------------------
# _find_optimal_space_combination
# ---------------------------------------------------------------------------

class TestFindOptimalSpaceCombination:

    @pytest.mark.smoke
    def test_tiny_gap_returns_unchanged(self) -> None:
        from qapbot.formatting import _find_optimal_space_combination, text_display_width_float

        cell = "Alice        "
        result = _find_optimal_space_combination(cell, 0.01, 17, text_display_width_float)
        assert result == cell  # gap < 0.05 threshold

    @pytest.mark.smoke
    def test_moderate_gap_fills(self) -> None:
        from qapbot.formatting import _find_optimal_space_combination, text_display_width_float

        cell = "Alice      "
        original_width = text_display_width_float(cell)
        target = 17
        gap = target - original_width
        if gap > 0.05:
            result = _find_optimal_space_combination(cell, gap, target, text_display_width_float)
            result_width = text_display_width_float(result)
            assert result_width >= original_width


# ---------------------------------------------------------------------------
# render_leaderboard — highlight_player_ids (auto-highlight a player's own row)
# ---------------------------------------------------------------------------

class TestRenderLeaderboardHighlight:

    @pytest.mark.smoke
    def test_highlighted_row_wrapped_in_ansi_codes(self) -> None:
        from qapbot.formatting import render_leaderboard, _ANSI_HIGHLIGHT, _ANSI_RESET

        players = _stats_dict(
            make_player_stats(tag="#P1", name="Alice", stars=6, attacks=2, defensive_stars=2, wars_count=1),
            make_player_stats(tag="#P2", name="Bob", stars=4, attacks=2, defensive_stars=1, wars_count=1),
        )
        result = render_leaderboard(
            clan_tag="#C", clan_name="Clan", month_label="", war_info_line="",
            stats_by_player=players, mode="attack", highlight_player_ids={"#P1"},
        )
        lines = result.split("\n")
        alice_line = next(l for l in lines if "Alice" in l)
        bob_line = next(l for l in lines if "Bob" in l)
        assert alice_line.startswith(_ANSI_HIGHLIGHT)
        assert alice_line.endswith(_ANSI_RESET)
        assert _ANSI_HIGHLIGHT not in bob_line
        assert _ANSI_RESET not in bob_line

    @pytest.mark.smoke
    def test_no_highlight_set_means_no_ansi_codes(self) -> None:
        from qapbot.formatting import render_leaderboard, _ANSI_HIGHLIGHT

        players = _stats_dict(
            make_player_stats(tag="#P1", name="Alice", stars=6, attacks=2, defensive_stars=2, wars_count=1),
        )
        result = render_leaderboard(
            clan_tag="#C", clan_name="Clan", month_label="", war_info_line="",
            stats_by_player=players, mode="attack",
        )
        assert _ANSI_HIGHLIGHT not in result

    @pytest.mark.smoke
    def test_terminal_style_never_gets_ansi_codes(self) -> None:
        """Console/log output must stay plain even if a caller passes highlight_player_ids."""
        from qapbot.formatting import render_leaderboard, _ANSI_HIGHLIGHT

        players = _stats_dict(
            make_player_stats(tag="#P1", name="Alice", stars=6, attacks=2, defensive_stars=2, wars_count=1),
        )
        result = render_leaderboard(
            clan_tag="#C", clan_name="Clan", month_label="", war_info_line="",
            stats_by_player=players, mode="attack", style="terminal",
            highlight_player_ids={"#P1"},
        )
        assert _ANSI_HIGHLIGHT not in result

    @pytest.mark.smoke
    def test_highlight_works_in_grouped_by_th_mode(self) -> None:
        """avgstarsbyth groups rows by TH level via a separate code path — must highlight there too."""
        from qapbot.formatting import render_leaderboard, _ANSI_HIGHLIGHT

        players = _stats_dict(
            make_player_stats(tag="#P1", name="Alice", th=16, stars=6, attacks=2, defensive_stars=2, wars_count=1),
            make_player_stats(tag="#P2", name="Bob", th=15, stars=4, attacks=2, defensive_stars=1, wars_count=1),
        )
        result = render_leaderboard(
            clan_tag="#C", clan_name="Clan", month_label="", war_info_line="",
            stats_by_player=players, mode="avgstarsbyth", highlight_player_ids={"#P1"},
        )
        lines = result.split("\n")
        alice_line = next(l for l in lines if "Alice" in l)
        bob_line = next(l for l in lines if "Bob" in l)
        assert _ANSI_HIGHLIGHT in alice_line
        assert _ANSI_HIGHLIGHT not in bob_line

    @pytest.mark.smoke
    def test_unmatched_highlight_id_leaves_everything_plain(self) -> None:
        from qapbot.formatting import render_leaderboard, _ANSI_HIGHLIGHT

        players = _stats_dict(
            make_player_stats(tag="#P1", name="Alice", stars=6, attacks=2, defensive_stars=2, wars_count=1),
        )
        result = render_leaderboard(
            clan_tag="#C", clan_name="Clan", month_label="", war_info_line="",
            stats_by_player=players, mode="attack", highlight_player_ids={"#SOMEONE_ELSE"},
        )
        assert _ANSI_HIGHLIGHT not in result
