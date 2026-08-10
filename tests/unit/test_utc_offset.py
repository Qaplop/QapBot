"""Tests for QBhelperfunctions.parse_utc_offset/format_utc_offset — the free-text UTC-offset
picker backing the "Select Timezone" server setting (needed because the CWL Management screen's
monospaced clan table can't use Discord's native per-viewer <t:...> timestamp markup)."""
from __future__ import annotations

import pytest

from QBhelperfunctions import parse_utc_offset, format_utc_offset


class TestParseUtcOffset:
    @pytest.mark.parametrize("text,expected", [
        ("+2", 120),
        ("-5", -300),
        ("+5:30", 330),
        ("-5:45", -345),
        ("+0", 0),
        ("-0:00", 0),
        ("+12", 720),
        ("-12", -720),
        ("+14", 840),
        ("  +2  ", 120),  # surrounding whitespace tolerated
    ])
    def test_parses_valid_offsets(self, text, expected):
        assert parse_utc_offset(text) == expected

    @pytest.mark.parametrize("text", [
        "+15",       # beyond the real-world +14:00 max
        "-13",       # beyond the real-world -12:00 max
        "0",         # no sign
        "5",         # no sign
        "abc",       # not an offset at all
        "",          # empty
        "+5:99",     # invalid minutes component
        "+5:5",      # minutes must be 2 digits
        "++5",       # malformed sign
    ])
    def test_rejects_invalid_offsets(self, text):
        assert parse_utc_offset(text) is None

    def test_rejects_none_and_empty(self):
        assert parse_utc_offset(None) is None  # type: ignore[arg-type]
        assert parse_utc_offset("") is None


class TestFormatUtcOffset:
    @pytest.mark.parametrize("minutes,expected", [
        (0, "+0"),
        (120, "+2"),
        (-300, "-5"),
        (330, "+5:30"),
        (-345, "-5:45"),
        (-720, "-12"),
        (840, "+14"),
    ])
    def test_formats_offsets(self, minutes, expected):
        assert format_utc_offset(minutes) == expected

    def test_roundtrips_through_parse(self):
        for minutes in (0, 60, -60, 330, -345, 720, -720, 840):
            assert parse_utc_offset(format_utc_offset(minutes)) == minutes
