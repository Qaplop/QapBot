"""Tests for the /leaderboard "scope" feature and the flexible `month` argument parser.

Covers:
- parse_month_argument(): single month, range, list, trailing "-N" (incl. year rollover)
- _format_periods_label(): display formatting for contiguous/non-contiguous/cross-year periods
- _load_history_rows(): scope="own" vs scope="all" dispatch, with graceful fallback
- calculate_leaderboard(scope="all"): a current member is credited for wars fought
  under a clan that is no longer tracked/subscribed
"""
# pyright: reportUnknownParameterType=false, reportMissingParameterType=false
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false
# pyright: reportUnknownArgumentType=false, reportUnusedImport=false
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest


def _mock_cache(families=None, temp_stats=None, db_manager=None):
    mock = MagicMock()
    mock.clan_families = families or {}
    mock.get_temp_war_stats = MagicMock(return_value=temp_stats or {})
    mock.clan_name_cache = {}
    mock.history_cache = {}
    mock.db_manager = db_manager
    return mock


# ---------------------------------------------------------------------------
# parse_month_argument
# ---------------------------------------------------------------------------

class TestParseMonthArgument:
    NOW = datetime(2026, 1, 15)  # January — best month to exercise year rollover

    def _parse(self):
        from QBhelperfunctions import parse_month_argument
        return parse_month_argument

    def test_single_month(self):
        assert self._parse()("6", self.NOW) == [(6, 2026)]

    def test_single_month_with_explicit_year(self):
        assert self._parse()("6", self.NOW, explicit_year=2024) == [(6, 2024)]

    def test_range(self):
        assert self._parse()("6-7", self.NOW) == [(6, 2026), (7, 2026)]

    def test_reversed_range_still_ascending(self):
        assert self._parse()("7-6", self.NOW) == [(6, 2026), (7, 2026)]

    def test_list(self):
        assert self._parse()("1;3;5", self.NOW) == [(1, 2026), (3, 2026), (5, 2026)]

    def test_list_out_of_order_is_sorted(self):
        assert self._parse()("5;1;3", self.NOW) == [(1, 2026), (3, 2026), (5, 2026)]

    def test_trailing_count_crosses_year_boundary(self):
        # "now" is January 2026 — the trailing 2 months are Dec 2025 + Jan 2026.
        assert self._parse()("-2", self.NOW) == [(12, 2025), (1, 2026)]

    def test_trailing_count_of_one_is_current_month(self):
        assert self._parse()("-1", self.NOW) == [(1, 2026)]

    def test_trailing_count_rejects_explicit_year(self):
        with pytest.raises(ValueError):
            self._parse()("-2", self.NOW, explicit_year=2025)

    def test_out_of_range_month_rejected(self):
        with pytest.raises(ValueError):
            self._parse()("13", self.NOW)

    def test_zero_month_rejected(self):
        with pytest.raises(ValueError):
            self._parse()("0", self.NOW)

    def test_garbage_input_rejected(self):
        with pytest.raises(ValueError):
            self._parse()("not-a-month", self.NOW)

    def test_empty_input_rejected(self):
        with pytest.raises(ValueError):
            self._parse()("", self.NOW)

    def test_trailing_count_zero_rejected(self):
        with pytest.raises(ValueError):
            self._parse()("-0", self.NOW)


# ---------------------------------------------------------------------------
# _format_periods_label
# ---------------------------------------------------------------------------

class TestFormatPeriodsLabel:
    def _fmt(self):
        from QBhelperfunctions import _format_periods_label
        return _format_periods_label

    def test_single_period(self):
        assert self._fmt()([(6, 2026)]) == "06/2026"

    def test_contiguous_same_year(self):
        assert self._fmt()([(6, 2026), (7, 2026)]) == "06-07/2026"

    def test_contiguous_crossing_year(self):
        assert self._fmt()([(12, 2025), (1, 2026)]) == "2025-12 to 2026-01"

    def test_non_contiguous_same_year(self):
        assert self._fmt()([(1, 2026), (3, 2026), (5, 2026)]) == "01+03+05/2026"

    def test_non_contiguous_crossing_year(self):
        assert self._fmt()([(11, 2025), (2, 2026)]) == "2025-11+2026-02"


# ---------------------------------------------------------------------------
# _load_history_rows — scope dispatch
# ---------------------------------------------------------------------------

class TestLoadHistoryRowsScope:
    def test_scope_own_uses_per_clan_history(self, monkeypatch):
        mock = _mock_cache()
        monkeypatch.setattr("QBhelperfunctions.CACHE", mock)
        calls = []

        def fake_filtered(tag, month, year, cwl_season):
            calls.append(tag)
            return [{"WarID": f"W-{tag}", "PlayerID": "#P1", "Player": "A", "Stars": 1,
                     "Attacks": 1, "Missed_Attacks": 0, "Defensive_Stars": 0, "Max_Attacks": 2}]

        monkeypatch.setattr("QBhelperfunctions._load_history_filtered", fake_filtered)
        from QBhelperfunctions import _load_history_rows
        rows = _load_history_rows("#CLAN1", 6, 2026, None, scope="own", member_player_tags={"#P1"})
        assert calls == ["#CLAN1"]
        assert len(rows) == 1

    def test_scope_all_uses_player_query(self, monkeypatch):
        db = MagicMock()
        db.get_player_attack_history_sync = MagicMock(return_value=[
            {"WarID": "#OLD::W1", "PlayerID": "#P1", "Player": "A", "Stars": 2,
             "Attacks": 1, "Missed_Attacks": 0, "Defensive_Stars": 0, "Max_Attacks": 2},
        ])
        mock = _mock_cache(db_manager=db)
        monkeypatch.setattr("QBhelperfunctions.CACHE", mock)
        from QBhelperfunctions import _load_history_rows
        rows = _load_history_rows("#CLAN1", 6, 2026, None, scope="all", member_player_tags={"#P1"})
        db.get_player_attack_history_sync.assert_called_once_with(["#P1"], 6, 2026)
        assert len(rows) == 1
        assert rows[0]["WarID"] == "#OLD::W1"

    def test_scope_all_without_member_tags_falls_back_to_own(self, monkeypatch):
        mock = _mock_cache()
        monkeypatch.setattr("QBhelperfunctions.CACHE", mock)
        calls = []

        def fake_filtered(tag, month, year, cwl_season):
            calls.append(tag)
            return []

        monkeypatch.setattr("QBhelperfunctions._load_history_filtered", fake_filtered)
        from QBhelperfunctions import _load_history_rows
        _load_history_rows("#CLAN1", 6, 2026, None, scope="all", member_player_tags=None)
        assert calls == ["#CLAN1"]  # fell back to the per-clan path, not the player query


# ---------------------------------------------------------------------------
# calculate_leaderboard(scope="all") — the actual feature end to end
# ---------------------------------------------------------------------------

class TestCalculateLeaderboardScopeAll:
    def test_credits_stats_from_a_no_longer_tracked_clan(self, monkeypatch):
        """
        Alice is currently in #NEW (a member clan) but earned stars in #OLD
        earlier in the month, before switching. #OLD is no longer subscribed
        anywhere, so scope="own" would miss those stars; scope="all" must not.
        """
        db = MagicMock()
        db.get_player_attack_history_sync = MagicMock(return_value=[
            {"WarID": "#OLD::W1", "PlayerID": "#P1", "Player": "Alice", "Stars": 2,
             "Attacks": 1, "Missed_Attacks": 0, "Defensive_Stars": 0, "Max_Attacks": 2,
             "Date": "2026-06-05T10:00", "TH_lvl": 15},
            {"WarID": "#NEW::W2", "PlayerID": "#P1", "Player": "Alice", "Stars": 3,
             "Attacks": 1, "Missed_Attacks": 0, "Defensive_Stars": 0, "Max_Attacks": 2,
             "Date": "2026-06-20T10:00", "TH_lvl": 15},
        ])
        mock = _mock_cache(db_manager=db)
        monkeypatch.setattr("QBhelperfunctions.CACHE", mock)
        from QBhelperfunctions import calculate_leaderboard

        result = calculate_leaderboard(
            "#NEW", month=6, year=2026, mode="attack",
            scope="all", member_player_tags={"#P1"},
        )

        assert "#P1" in result
        assert result["#P1"]["Stars"] == 5
        assert result["#P1"]["Wars_Count"] == 2

    def test_scope_own_only_sees_current_clan_stats(self, monkeypatch):
        """Same setup as above, but scope="own" (default) — only #NEW's own history counts."""
        history_new = [
            {"WarID": "W2", "PlayerID": "#P1", "Player": "Alice", "Stars": 3,
             "Attacks": 1, "Missed_Attacks": 0, "Defensive_Stars": 0, "Max_Attacks": 2,
             "Date": "2026-06-20T10:00", "TH_lvl": 15},
        ]
        mock = _mock_cache()
        mock.get_clan_history = MagicMock(return_value=history_new)
        monkeypatch.setattr("QBhelperfunctions.CACHE", mock)
        from QBhelperfunctions import calculate_leaderboard

        result = calculate_leaderboard("#NEW", month=6, year=2026, mode="attack")

        assert result["#P1"]["Stars"] == 3  # the #OLD stint's 2 stars are NOT counted
