"""Extended tests for QBhelperfunctions.py — Phase 2 coverage boost.

Targets uncovered lines in: generate_war_info_text (lines 462-613),
calculate_leaderboard (lines 262-395), generate_leaderboard_text (lines 622-740),
generate_message_key_timestamp, filter_entries_by_mode, and
helper functions _parse_timestamp_field, _fmt_delta_from_secs.
"""
# pyright: reportUnknownParameterType=false, reportMissingParameterType=false
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false
# pyright: reportUnknownLambdaType=false, reportUnknownArgumentType=false
# pyright: reportUnusedImport=false
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_war_data(
    state: str = "in_war",
    my_stars: int = 10,
    opp_stars: int = 8,
    my_dest: float = 60.0,
    opp_dest: float = 55.0,
    attacks_per_member: int = 2,
    end_secs: int = 7200,
    start_secs: int = 3600,
    my_members: List[Dict[str, Any]] | None = None,
    opp_members: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Build a mock war_data dict for generate_war_info_text tests."""
    ts_end = f"<Timestamp time=datetime.datetime(2025, 12, 24, 10, 0, 0) seconds_until={end_secs}>"
    ts_start = f"<Timestamp time=datetime.datetime(2025, 12, 24, 8, 0, 0) seconds_until={start_secs}>"
    if my_members is None:
        my_members = [
            {"tag": "#P1", "name": "Player1", "attacks": [{"defenderTag": "#E1", "stars": 3}]},
            {"tag": "#P2", "name": "Player2", "attacks": []},
        ]
    if opp_members is None:
        opp_members = [
            {"tag": "#E1", "name": "Enemy1", "attacks": [{"defenderTag": "#P1", "stars": 2}]},
            {"tag": "#E2", "name": "Enemy2", "attacks": []},
        ]
    return {
        "state": state,
        "attacks_per_member": attacks_per_member,
        "start_time": ts_start,
        "end_time": ts_end,
        "clan": {"tag": "#CLAN1", "name": "MyClan", "stars": my_stars, "members": my_members, "destruction": my_dest},
        "opponent": {"tag": "#OPP1", "name": "EnemyClan", "stars": opp_stars, "members": opp_members, "destruction": opp_dest},
    }


def _mock_cache(war_data=None, families=None, temp_stats=None, clan_history=None):
    """Return a mock CACHE object suitable for QBhelperfunctions."""
    mock = MagicMock()
    mock.clan_families = families or {}
    mock.get_current_war_data = MagicMock(return_value=war_data)
    mock.get_clan_name = MagicMock(return_value="MyClan")
    mock.temp_war_stats = temp_stats or {}
    mock.get_temp_war_stats = MagicMock(return_value=temp_stats or {})
    mock.get_clan_history = MagicMock(return_value=clan_history or [])
    mock.clan_name_cache = {}
    mock.history_cache = {}
    return mock


# ---------------------------------------------------------------------------
# generate_war_info_text
# ---------------------------------------------------------------------------

class TestGenerateWarInfoText:
    """Tests for generate_war_info_text covering all state branches."""

    def test_family_tag_returns_empty(self, monkeypatch):
        mock = _mock_cache()
        mock.clan_families = {"#FAM": {"name": "Family", "clans": ["#A"]}}
        monkeypatch.setattr("QBhelperfunctions.CACHE", mock)
        from QBhelperfunctions import generate_war_info_text
        assert generate_war_info_text("#FAM") == ""

    def test_no_war_data(self, monkeypatch):
        mock = _mock_cache(war_data=None)
        monkeypatch.setattr("QBhelperfunctions.CACHE", mock)
        from QBhelperfunctions import generate_war_info_text
        result = generate_war_info_text("#CLAN1")
        assert "No current war" in result

    def test_preparation_state(self, monkeypatch):
        wd = _make_war_data(state="preparation", start_secs=3600)
        mock = _mock_cache(war_data=wd)
        monkeypatch.setattr("QBhelperfunctions.CACHE", mock)
        from QBhelperfunctions import generate_war_info_text
        result = generate_war_info_text("#CLAN1")
        assert "preparation" in result.lower() or "War starts in" in result
        assert "MyClan" in result
        assert "EnemyClan" in result

    def test_in_war_state(self, monkeypatch):
        wd = _make_war_data(state="in_war", end_secs=7200, my_stars=12, opp_stars=8)
        mock = _mock_cache(war_data=wd)
        monkeypatch.setattr("QBhelperfunctions.CACHE", mock)
        # Mock the warsim functions to avoid complex dependencies
        monkeypatch.setattr("QBwarsim.calculate_max_possible_stars", lambda wd, tag: 5)
        monkeypatch.setattr("QBwarsim.calculate_win_probability",
            lambda wd, tag, *a, **kw: (65.0, 25.0, 10.0, 80))
        from QBhelperfunctions import generate_war_info_text
        result = generate_war_info_text("#CLAN1")
        assert "War in progress" in result
        assert "War ends in" in result
        assert "MyClan" in result

    def test_in_war_100_percent_win(self, monkeypatch):
        wd = _make_war_data(state="in_war", my_stars=30, opp_stars=0)
        mock = _mock_cache(war_data=wd)
        monkeypatch.setattr("QBhelperfunctions.CACHE", mock)
        monkeypatch.setattr("QBwarsim.calculate_max_possible_stars", lambda wd, tag: 0)
        monkeypatch.setattr("QBwarsim.calculate_win_probability",
            lambda wd, tag, *a, **kw: (100.0, 0.0, 0.0, 95))
        from QBhelperfunctions import generate_war_info_text
        result = generate_war_info_text("#CLAN1")
        assert "Victory" in result or "🎉" in result

    def test_in_war_100_percent_loss(self, monkeypatch):
        wd = _make_war_data(state="in_war", my_stars=0, opp_stars=30)
        mock = _mock_cache(war_data=wd)
        monkeypatch.setattr("QBhelperfunctions.CACHE", mock)
        monkeypatch.setattr("QBwarsim.calculate_max_possible_stars", lambda wd, tag: 0)
        monkeypatch.setattr("QBwarsim.calculate_win_probability",
            lambda wd, tag, *a, **kw: (0.0, 100.0, 0.0, 95))
        from QBhelperfunctions import generate_war_info_text
        result = generate_war_info_text("#CLAN1")
        assert "lost" in result.lower() or "💀" in result

    def test_war_ended_state(self, monkeypatch):
        wd = _make_war_data(state="war_ended", my_stars=15, opp_stars=12)
        mock = _mock_cache(war_data=wd)
        monkeypatch.setattr("QBhelperfunctions.CACHE", mock)
        from QBhelperfunctions import generate_war_info_text
        result = generate_war_info_text("#CLAN1")
        assert "War ended" in result

    def test_not_in_war_state(self, monkeypatch):
        wd = _make_war_data(state="not_in_war")
        mock = _mock_cache(war_data=wd)
        monkeypatch.setattr("QBhelperfunctions.CACHE", mock)
        from QBhelperfunctions import generate_war_info_text
        result = generate_war_info_text("#CLAN1")
        assert "not in war" in result.lower()

    def test_unknown_state(self, monkeypatch):
        wd = _make_war_data(state="mystery_state")
        mock = _mock_cache(war_data=wd)
        monkeypatch.setattr("QBhelperfunctions.CACHE", mock)
        from QBhelperfunctions import generate_war_info_text
        result = generate_war_info_text("#CLAN1")
        assert "mystery_state" in result


# ---------------------------------------------------------------------------
# Leaderboard mode filtering — tested via calculate_leaderboard(mode=...)
# The filtering is inline inside calculate_leaderboard (not a separate function).
# ---------------------------------------------------------------------------

class TestLeaderboardModeFiltering:
    """Tests the mode-based filtering inside calculate_leaderboard."""

    def _history(self):
        """Two players: Alice (has attacks), Bob (0 attacks, 2 missed)."""
        return [
            {
                "PlayerID": "#P1", "Player": "Alice", "Stars": 5, "Attacks": 3,
                "Missed_Attacks": 0, "Defensive_Stars": 2, "Max_Attacks": 4,
                "Date": "2025-01-15T10:00", "TH_lvl": 15, "WarID": "W1",
                "Month": 1, "Year": 2025,
            },
            {
                "PlayerID": "#P2", "Player": "Bob", "Stars": 0, "Attacks": 0,
                "Missed_Attacks": 2, "Defensive_Stars": 0, "Max_Attacks": 2,
                "Date": "2025-01-15T10:00", "TH_lvl": 14, "WarID": "W1",
                "Month": 1, "Year": 2025,
            },
        ]

    def _setup(self, monkeypatch, mode):
        history = self._history()
        mock = _mock_cache(clan_history=history)
        mock.get_clan_history = MagicMock(return_value=history)
        mock.current_wars = {}
        monkeypatch.setattr("QBhelperfunctions.CACHE", mock)
        from QBhelperfunctions import calculate_leaderboard
        return calculate_leaderboard("#CLAN1", month=1, year=2025, mode=mode)

    def test_attack_mode_excludes_zero_attacks(self, monkeypatch):
        result = self._setup(monkeypatch, "attack")
        assert "#P1" in result
        assert "#P2" not in result

    def test_missedattacks_mode(self, monkeypatch):
        result = self._setup(monkeypatch, "missedattacks")
        assert "#P2" in result  # Bob has 2 missed
        assert "#P1" not in result  # Alice has 0 missed

    def test_currentwar_mode_includes_all(self, monkeypatch):
        """currentwar mode skips history and uses temp stats only — both players shown."""
        temp_stats = {
            "#P1": {
                "PlayerID": "#P1", "Player": "Alice", "Stars": 5, "Attacks": 3,
                "Missed_Attacks": 0, "Defensive_Stars": 2, "Max_Attacks": 4,
                "Date": "2025-01-15T10:00", "TH_lvl": 15, "WarID": "W1",
            },
            "#P2": {
                "PlayerID": "#P2", "Player": "Bob", "Stars": 0, "Attacks": 0,
                "Missed_Attacks": 2, "Defensive_Stars": 0, "Max_Attacks": 2,
                "Date": "2025-01-15T10:00", "TH_lvl": 14, "WarID": "W1",
            },
        }
        mock = _mock_cache()
        mock.get_clan_history = MagicMock(return_value=[])
        mock.get_temp_war_stats = MagicMock(return_value=temp_stats)
        mock.current_wars = {}
        monkeypatch.setattr("QBhelperfunctions.CACHE", mock)
        from QBhelperfunctions import calculate_leaderboard
        result = calculate_leaderboard("#CLAN1", month=1, year=2025, mode="currentwar")
        assert "#P1" in result
        assert "#P2" in result

    def test_unknown_mode_defaults_to_attacks_gt_zero(self, monkeypatch):
        result = self._setup(monkeypatch, "some_future_mode")
        assert "#P1" in result
        assert "#P2" not in result

    def test_avgstars_mode(self, monkeypatch):
        result = self._setup(monkeypatch, "avgstars")
        assert "#P1" in result
        assert "#P2" not in result


# ---------------------------------------------------------------------------
# generate_message_key_timestamp
# ---------------------------------------------------------------------------

class TestGenerateMessageKeyTimestamp:
    def test_format(self):
        from QBhelperfunctions import generate_message_key_timestamp
        key = generate_message_key_timestamp()
        # Should look like 2025-08-29T133053.123
        assert "T" in key
        assert len(key) >= 20

    def test_unique_calls(self):
        from QBhelperfunctions import generate_message_key_timestamp
        import time
        k1 = generate_message_key_timestamp()
        time.sleep(0.01)
        k2 = generate_message_key_timestamp()
        assert k1 != k2


# ---------------------------------------------------------------------------
# calculate_leaderboard — basic paths
# ---------------------------------------------------------------------------

class TestCalculateLeaderboard:
    def test_single_clan_no_data(self, monkeypatch):
        mock = _mock_cache()
        mock.get_clan_history = MagicMock(return_value=[])
        monkeypatch.setattr("QBhelperfunctions.CACHE", mock)
        from QBhelperfunctions import calculate_leaderboard
        result = calculate_leaderboard("#CLAN1", month=1, year=2025)
        assert result == {} or isinstance(result, dict)

    def test_single_clan_with_history(self, monkeypatch):
        history = [
            {
                "PlayerID": "#P1", "Player": "Alice", "Stars": 3, "Attacks": 1,
                "Missed_Attacks": 0, "Defensive_Stars": 1, "Max_Attacks": 2,
                "Date": "2025-01-15T10:00", "TH_lvl": 15, "WarID": "W1",
                "Month": 1, "Year": 2025,
            },
            {
                "PlayerID": "#P2", "Player": "Bob", "Stars": 5, "Attacks": 2,
                "Missed_Attacks": 0, "Defensive_Stars": 2, "Max_Attacks": 2,
                "Date": "2025-01-15T10:00", "TH_lvl": 14, "WarID": "W1",
                "Month": 1, "Year": 2025,
            },
        ]
        mock = _mock_cache(clan_history=history)
        mock.get_clan_history = MagicMock(return_value=history)
        monkeypatch.setattr("QBhelperfunctions.CACHE", mock)
        from QBhelperfunctions import calculate_leaderboard
        result = calculate_leaderboard("#CLAN1", month=1, year=2025)
        assert "#P1" in result
        assert "#P2" in result
        assert result["#P1"]["Stars"] == 3
        assert result["#P2"]["Stars"] == 5

    def test_cwl_only_filters(self, monkeypatch):
        history = [
            {"PlayerID": "#P1", "Player": "A", "Stars": 3, "Attacks": 1,
             "Missed_Attacks": 0, "Defensive_Stars": 0, "Max_Attacks": 1,  # CWL
             "Date": "2025-01-15T10:00", "TH_lvl": 15, "WarID": "W1",
             "Month": 1, "Year": 2025},
            {"PlayerID": "#P2", "Player": "B", "Stars": 5, "Attacks": 2,
             "Missed_Attacks": 0, "Defensive_Stars": 0, "Max_Attacks": 2,  # Regular
             "Date": "2025-01-15T10:00", "TH_lvl": 14, "WarID": "W2",
             "Month": 1, "Year": 2025},
        ]
        mock = _mock_cache(clan_history=history)
        mock.get_clan_history = MagicMock(return_value=history)
        monkeypatch.setattr("QBhelperfunctions.CACHE", mock)
        from QBhelperfunctions import calculate_leaderboard
        result = calculate_leaderboard("#CLAN1", month=1, year=2025, cwl_only=True)
        assert "#P1" in result
        assert "#P2" not in result  # filtered out (Max_Attacks=2)

    def test_family_tag_aggregates(self, monkeypatch):
        history_a = [
            {"PlayerID": "#P1", "Player": "A", "Stars": 3, "Attacks": 1,
             "Missed_Attacks": 0, "Defensive_Stars": 0, "Max_Attacks": 2,
             "Date": "2025-01-15T10:00", "TH_lvl": 15, "WarID": "W1",
             "Month": 1, "Year": 2025},
        ]
        history_b = [
            {"PlayerID": "#P1", "Player": "A", "Stars": 2, "Attacks": 1,
             "Missed_Attacks": 0, "Defensive_Stars": 1, "Max_Attacks": 2,
             "Date": "2025-01-16T10:00", "TH_lvl": 15, "WarID": "W2",
             "Month": 1, "Year": 2025},
        ]
        mock = _mock_cache()
        mock.clan_families = {"#FAM": {"name": "Family", "clans": ["#A", "#B"]}}

        def get_history(tag):
            if tag == "#A":
                return history_a
            return history_b

        mock.get_clan_history = MagicMock(side_effect=get_history)
        monkeypatch.setattr("QBhelperfunctions.CACHE", mock)
        from QBhelperfunctions import calculate_leaderboard
        result = calculate_leaderboard("#FAM", month=1, year=2025)
        # Player #P1 should appear once with aggregated stars
        assert "#P1" in result
        assert result["#P1"]["Stars"] == 5  # 3 + 2


# ---------------------------------------------------------------------------
# generate_leaderboard_text — key branches
# ---------------------------------------------------------------------------

class TestGenerateLeaderboardText:
    def test_unknown_mode(self, monkeypatch):
        mock = _mock_cache()
        monkeypatch.setattr("QBhelperfunctions.CACHE", mock)
        from QBhelperfunctions import generate_leaderboard_text
        result = generate_leaderboard_text("#CLAN1", mode="invalid_mode_xyz")
        assert "Unknown leaderboard mode" in result

    def test_no_stats_returns_no_war(self, monkeypatch):
        mock = _mock_cache()
        mock.get_clan_history = MagicMock(return_value=[])
        monkeypatch.setattr("QBhelperfunctions.CACHE", mock)
        from QBhelperfunctions import generate_leaderboard_text
        result = generate_leaderboard_text("#CLAN1", month=1, year=2025, mode="attack")
        assert "no war" in result.lower() or "currently" in result.lower()

    def test_cwl_mode_suffix(self, monkeypatch):
        history = [
            {"PlayerID": "#P1", "Player": "A", "Stars": 3, "Attacks": 1,
             "Missed_Attacks": 0, "Defensive_Stars": 0, "Max_Attacks": 1,
             "Date": "2025-01-15T10:00", "TH_lvl": 15, "WarID": "W1",
             "Month": 1, "Year": 2025},
        ]
        mock = _mock_cache(clan_history=history)
        mock.get_clan_history = MagicMock(return_value=history)
        monkeypatch.setattr("QBhelperfunctions.CACHE", mock)
        from QBhelperfunctions import generate_leaderboard_text
        result = generate_leaderboard_text("#CLAN1", month=1, year=2025, mode="attack_cwl")
        assert "CWL" in result

    def test_multi_month_label(self, monkeypatch):
        history = [
            {"PlayerID": "#P1", "Player": "A", "Stars": 3, "Attacks": 1,
             "Missed_Attacks": 0, "Defensive_Stars": 0, "Max_Attacks": 2,
             "Date": "2025-01-15T10:00", "TH_lvl": 15, "WarID": "W1",
             "Month": 1, "Year": 2025},
        ]
        mock = _mock_cache(clan_history=history)
        mock.get_clan_history = MagicMock(return_value=history)
        monkeypatch.setattr("QBhelperfunctions.CACHE", mock)
        from QBhelperfunctions import generate_leaderboard_text
        result = generate_leaderboard_text("#CLAN1", month=[1, 2, 3], year=2025, mode="attack")
        # Should contain month range label
        assert "01" in result and "03" in result
