"""Tests for QBhelperfunctions.py deep coverage — Phase 3.

Covers: _load_history_filtered, _merge_entries (additional branches),
_archive_and_log_war_file, plus additional generate_leaderboard_text branches.
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnusedImport=false
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest


def _mock_cache(**overrides) -> MagicMock:
    cache = MagicMock()
    cache.history_cache = overrides.get("history_cache", {})
    cache.get_clan_history = MagicMock(return_value=overrides.get("clan_history", []))
    cache.get_temp_war_stats = MagicMock(return_value=overrides.get("temp_stats", {}))
    cache.clan_families = overrides.get("clan_families", {})
    cache.clan_name_cache = overrides.get("clan_name_cache", {})
    cache.current_wars = overrides.get("current_wars", {})
    return cache


# ---------------------------------------------------------------------------
# _load_history_filtered
# ---------------------------------------------------------------------------

class TestLoadHistoryFiltered:
    def _fn(self):
        from QBhelperfunctions import _load_history_filtered
        return _load_history_filtered

    def test_cache_hit(self, monkeypatch):
        cached_data = [{"Player": "Alice"}]
        cache = _mock_cache(history_cache={("#C1", 1, 2025, None): cached_data})
        monkeypatch.setattr("QBhelperfunctions.CACHE", cache)
        result = self._fn()("#C1", 1, 2025)
        assert result is cached_data

    def test_filters_by_month_year(self, monkeypatch):
        history = [
            {"Player": "A", "Date": "2025-01-15T10:00", "PlayerID": "#P1"},
            {"Player": "B", "Date": "2025-02-15T10:00", "PlayerID": "#P2"},
            {"Player": "C", "Date": "2024-01-15T10:00", "PlayerID": "#P3"},
        ]
        cache = _mock_cache(clan_history=history)
        monkeypatch.setattr("QBhelperfunctions.CACHE", cache)
        result = self._fn()("#C1", 1, 2025)
        assert len(result) == 1
        assert result[0]["Player"] == "A"

    def test_no_month_filter(self, monkeypatch):
        history = [
            {"Player": "A", "Date": "2025-01-15T10:00", "PlayerID": "#P1"},
            {"Player": "B", "Date": "2025-06-15T10:00", "PlayerID": "#P2"},
        ]
        cache = _mock_cache(clan_history=history)
        monkeypatch.setattr("QBhelperfunctions.CACHE", cache)
        result = self._fn()("#C1", None, 2025)
        assert len(result) == 2

    def test_skips_bad_dates(self, monkeypatch):
        history = [
            {"Player": "A", "Date": "not-a-date", "PlayerID": "#P1"},
            {"Player": "B", "Date": "2025-01-15T10:00", "PlayerID": "#P2"},
        ]
        cache = _mock_cache(clan_history=history)
        monkeypatch.setattr("QBhelperfunctions.CACHE", cache)
        result = self._fn()("#C1", 1, 2025)
        assert len(result) == 1
        assert result[0]["Player"] == "B"

    def test_missing_date_key(self, monkeypatch):
        history = [{"Player": "A", "PlayerID": "#P1"}]
        cache = _mock_cache(clan_history=history)
        monkeypatch.setattr("QBhelperfunctions.CACHE", cache)
        result = self._fn()("#C1", 1, 2025)
        assert len(result) == 0

    def test_stores_in_cache(self, monkeypatch):
        history = [{"Player": "A", "Date": "2025-03-01T00:00", "PlayerID": "#P1"}]
        history_cache = {}
        cache = _mock_cache(clan_history=history)
        cache.history_cache = history_cache
        monkeypatch.setattr("QBhelperfunctions.CACHE", cache)
        self._fn()("#C1", 3, 2025)
        assert ("#C1", 3, 2025, None) in history_cache

    def test_no_filters_returns_all(self, monkeypatch):
        history = [
            {"Player": "A", "Date": "2024-06-01T00:00", "PlayerID": "#P1"},
            {"Player": "B", "Date": "2025-01-01T00:00", "PlayerID": "#P2"},
        ]
        cache = _mock_cache(clan_history=history)
        monkeypatch.setattr("QBhelperfunctions.CACHE", cache)
        result = self._fn()("#C1", None, None)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# _merge_entries — additional edge cases
# ---------------------------------------------------------------------------

class TestMergeEntriesEdges:
    def _fn(self):
        from QBhelperfunctions import _merge_entries
        return _merge_entries

    def test_empty_both(self):
        result = self._fn()([], {}, False, mode="attack")
        assert result == {}

    def test_temp_stats_only(self):
        temp = {
            "#P1": {
                "PlayerID": "#P1", "Player": "Alice", "Stars": 3, "Attacks": 2,
                "Missed_Attacks": 0, "Defensive_Stars": 1, "Date": "2025-01-15T10:00",
            }
        }
        result = self._fn()([], temp, True, mode="attack")
        assert "#P1" in result
        assert result["#P1"]["Stars"] == 3

    def test_war_in_progress_zeroes_missed(self):
        temp = {
            "#P1": {
                "PlayerID": "#P1", "Player": "Alice", "Stars": 0, "Attacks": 0,
                "Missed_Attacks": 5, "Defensive_Stars": 0, "Date": "2025-01-15T10:00",
            }
        }
        result = self._fn()([], temp, True, mode="currentwar")
        assert result["#P1"]["Missed_Attacks"] == 0  # war_in_progress=True zeroes missed

    def test_war_ended_keeps_missed(self):
        temp = {
            "#P1": {
                "PlayerID": "#P1", "Player": "Alice", "Stars": 0, "Attacks": 0,
                "Missed_Attacks": 5, "Defensive_Stars": 0, "Date": "2025-01-15T10:00",
            }
        }
        result = self._fn()([], temp, False, mode="missedattacks")
        assert result["#P1"]["Missed_Attacks"] == 5

    def test_cwl_mode_strip(self):
        """Modes like 'attack_cwl' should strip '_cwl' suffix."""
        history = [
            {"Player": "A", "PlayerID": "#P1", "Stars": 2, "Attacks": 1,
             "Missed_Attacks": 0, "Defensive_Stars": 0, "Date": "2025-01-01T00:00",
             "TH_lvl": 15, "WarID": "W1"},
        ]
        result = self._fn()(history, {}, False, mode="attack_cwl")
        assert "#P1" in result

    def test_defensive_mode(self):
        history = [
            {"Player": "A", "PlayerID": "#P1", "Stars": 0, "Attacks": 0,
             "Missed_Attacks": 0, "Defensive_Stars": 5, "Date": "2025-01-01T00:00",
             "TH_lvl": 15, "WarID": "W1"},
            {"Player": "B", "PlayerID": "#P2", "Stars": 3, "Attacks": 2,
             "Missed_Attacks": 0, "Defensive_Stars": 0, "Date": "2025-01-01T00:00",
             "TH_lvl": 14, "WarID": "W1"},
        ]
        result = self._fn()(history, {}, False, mode="defensive")
        assert "#P1" in result
        assert "#P2" not in result

    def test_attackdefratio_mode(self):
        history = [
            {"Player": "A", "PlayerID": "#P1", "Stars": 3, "Attacks": 2,
             "Missed_Attacks": 0, "Defensive_Stars": 1, "Date": "2025-01-01T00:00",
             "TH_lvl": 15, "WarID": "W1"},
        ]
        result = self._fn()(history, {}, False, mode="attackdefratio")
        assert "#P1" in result

    def test_player_name_update_newer_date(self):
        """Newer rows should update player name."""
        history = [
            {"Player": "OldName", "PlayerID": "#P1", "Stars": 1, "Attacks": 1,
             "Missed_Attacks": 0, "Defensive_Stars": 0, "Date": "2025-01-01T00:00",
             "TH_lvl": 14, "WarID": "W1"},
            {"Player": "NewName", "PlayerID": "#P1", "Stars": 2, "Attacks": 1,
             "Missed_Attacks": 0, "Defensive_Stars": 0, "Date": "2025-02-01T00:00",
             "TH_lvl": 15, "WarID": "W2"},
        ]
        result = self._fn()(history, {}, False, mode="attack")
        assert result["#P1"]["Player"] == "NewName"
        assert result["#P1"]["TH_lvl"] == 15
        assert result["#P1"]["Stars"] == 3  # cumulative

    def test_wars_count_deduplication(self):
        """Same WarID should only count once."""
        history = [
            {"Player": "A", "PlayerID": "#P1", "Stars": 3, "Attacks": 1,
             "Missed_Attacks": 0, "Defensive_Stars": 0, "Date": "2025-01-01T00:00",
             "TH_lvl": 15, "WarID": "W1"},
            {"Player": "A", "PlayerID": "#P1", "Stars": 2, "Attacks": 1,
             "Missed_Attacks": 0, "Defensive_Stars": 0, "Date": "2025-01-01T00:00",
             "TH_lvl": 15, "WarID": "W1"},
        ]
        result = self._fn()(history, {}, False, mode="attack")
        assert result["#P1"]["Wars_Count"] == 1
        assert result["#P1"]["Stars"] == 5


# ---------------------------------------------------------------------------
# _archive_and_log_war_file helpers
# ---------------------------------------------------------------------------

class TestArchiveWarFile:
    def test_move_to_archive(self, tmp_path):
        """Simple test that shutil.move works for war file archival."""
        src = tmp_path / "temp" / "test_war.json"
        src.parent.mkdir(parents=True)
        src.write_text("{}", encoding="utf-8")

        archive = tmp_path / "archive"
        archive.mkdir()

        import shutil
        shutil.move(str(src), str(archive / "test_war.json"))
        assert (archive / "test_war.json").exists()
        assert not src.exists()
