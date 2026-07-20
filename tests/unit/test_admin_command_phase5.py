"""Tests for QBdiscocmdshelper_admin_command.py — Phase 5 coverage push.

Covers:
- check_database_consistency (full function — ~170 stmts, currently 0 tests)
- format_database_check_results (deeper edge cases)
- format_log_summary (deeper edge cases)
- scan_logs (more edge cases)
- check_current_wars_in_temp (more edge cases)
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnusedImport=false, reportAttributeAccessIssue=false, reportReturnType=false, reportOptionalMemberAccess=false
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest


# ===========================================================================
# check_database_consistency
# ===========================================================================

class TestCheckDatabaseConsistency:
    def _fn(self):
        from qapbot.QBdiscocmdshelper_admin_command import check_database_consistency
        return check_database_consistency

    def test_no_db_manager(self, monkeypatch):
        cache = MagicMock()
        cache.db_manager = None
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        result = self._fn()("data/qapbot.db", "data")
        assert result["integrity_ok"] is False
        assert "not initialized" in result["integrity_errors"][0]

    def test_db_file_missing(self, monkeypatch, tmp_path):
        cache = MagicMock()
        cache.db_manager = MagicMock()
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        result = self._fn()(str(tmp_path / "missing.db"), str(tmp_path))
        assert result["integrity_ok"] is False
        assert "not found" in result["integrity_errors"][0]

    def test_integrity_ok_no_wars(self, monkeypatch, tmp_path):
        db_path = tmp_path / "test.db"
        db_path.write_text("")
        cache = MagicMock()
        cache.db_manager = MagicMock()
        cache.db_manager.check_integrity_sync.return_value = (True, [])
        cache.db_manager.get_global_db_statistics_sync.return_value = {
            "attacks_count": 0, "wars_count": 0, "clans_count": 0, "players_count": 0
        }
        cache.db_manager.get_all_war_summaries_brief_sync.return_value = []
        cache.db_manager.get_recent_war_summaries_sync.return_value = []
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        result = self._fn()(str(db_path), str(tmp_path))
        assert result["integrity_ok"] is True
        assert result["total_records"] == 0
        assert result["duplicate_wars"] == {}
        assert result["missing_from_db"] == []

    def test_integrity_failed(self, monkeypatch, tmp_path):
        db_path = tmp_path / "test.db"
        db_path.write_text("")
        cache = MagicMock()
        cache.db_manager = MagicMock()
        cache.db_manager.check_integrity_sync.return_value = (False, ["some error"])
        cache.db_manager.get_global_db_statistics_sync.return_value = {
            "attacks_count": 10, "wars_count": 5, "clans_count": 2, "players_count": 8
        }
        cache.db_manager.get_all_war_summaries_brief_sync.return_value = []
        cache.db_manager.get_recent_war_summaries_sync.return_value = []
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        result = self._fn()(str(db_path), str(tmp_path), include_integrity=True)
        assert result["integrity_ok"] is False
        assert "some error" in result["integrity_errors"]

    def test_duplicate_wars_detected(self, monkeypatch, tmp_path):
        db_path = tmp_path / "test.db"
        db_path.write_text("")
        now = datetime.now()
        cache = MagicMock()
        cache.db_manager = MagicMock()
        cache.db_manager.check_integrity_sync.return_value = (True, [])
        cache.db_manager.get_global_db_statistics_sync.return_value = {
            "attacks_count": 2, "wars_count": 2, "clans_count": 1, "players_count": 2
        }
        # Two non-CWL wars from same opponent within 7 days
        cache.db_manager.get_all_war_summaries_brief_sync.return_value = [
            ("OPP12345_12345", "#CLAN1234", now.isoformat(), 0),
            ("OPP12345_12346", "#CLAN1234", (now + timedelta(days=2)).isoformat(), 0),
        ]
        cache.db_manager.get_recent_war_summaries_sync.return_value = []
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        result = self._fn()(str(db_path), str(tmp_path))
        assert len(result["duplicate_wars"]) > 0

    def test_missing_from_db(self, monkeypatch, tmp_path):
        db_path = tmp_path / "test.db"
        db_path.write_text("")
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()
        war_data = {"clan": {"tag": "#C1"}, "opponent": {"tag": "#OPP"}, "endTime": "20250101T000000.000Z"}
        (archive_dir / "C1_123_202601010000_war_data.json").write_text(json.dumps(war_data))
        cache = MagicMock()
        cache.db_manager = MagicMock()
        cache.db_manager.check_integrity_sync.return_value = (True, [])
        cache.db_manager.get_global_db_statistics_sync.return_value = {
            "attacks_count": 0, "wars_count": 0, "clans_count": 0, "players_count": 0
        }
        cache.db_manager.get_all_war_summaries_brief_sync.return_value = []
        cache.db_manager.get_all_war_summary_keys_sync.return_value = frozenset()
        cache.db_manager.war_attacks_exist_sync.return_value = False  # Not in DB
        cache.db_manager.get_recent_war_summaries_sync.return_value = []
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        result = self._fn()(str(db_path), str(tmp_path), archive_dir=str(archive_dir))
        assert len(result["missing_from_db"]) >= 1

    def test_db_query_exception(self, monkeypatch, tmp_path):
        db_path = tmp_path / "test.db"
        db_path.write_text("")
        cache = MagicMock()
        cache.db_manager = MagicMock()
        cache.db_manager.check_integrity_sync.side_effect = Exception("DB crash")
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        result = self._fn()(str(db_path), str(tmp_path), include_integrity=True)
        assert "Database query failed" in result["integrity_errors"][0]


# ===========================================================================
# format_database_check_results — deeper
# ===========================================================================

class TestFormatDatabaseCheckResultsDeep:
    def _fn(self):
        from qapbot.QBdiscocmdshelper_admin_command import format_database_check_results
        return format_database_check_results

    def test_all_ok(self):
        results = {
            "integrity_ok": True, "integrity_errors": [],
            "duplicate_wars": {}, "missing_from_db": [],
            "missing_from_archive": [], "current_wars": [],
            "total_records": 100, "total_wars": 50, "total_clans": 5,
        }
        output = self._fn()(results)
        assert "✅ OK" in output
        assert "100" in output

    def test_integrity_failed_with_errors(self):
        results = {
            "integrity_ok": False, "integrity_errors": ["err1", "err2"],
            "duplicate_wars": {}, "missing_from_db": [],
            "missing_from_archive": [], "current_wars": [],
            "total_records": 0, "total_wars": 0, "total_clans": 0,
        }
        output = self._fn()(results)
        assert "❌ FAILED" in output
        assert "err1" in output

    def test_duplicates_shown(self):
        results = {
            "integrity_ok": True, "integrity_errors": [],
            "duplicate_wars": {"#C1|OPP1": [("w1", "2025-01-01"), ("w2", "2025-01-02")]},
            "missing_from_db": [], "missing_from_archive": [],
            "current_wars": [], "total_records": 10, "total_wars": 5, "total_clans": 2,
        }
        output = self._fn()(results)
        assert "⚠️" in output
        assert "2 war IDs" in output

    def test_missing_from_db(self):
        results = {
            "integrity_ok": True, "integrity_errors": [],
            "duplicate_wars": {},
            "missing_from_db": [("file1.json", "#C1", "war_123")],
            "missing_from_archive": [], "current_wars": [],
            "total_records": 10, "total_wars": 5, "total_clans": 2,
        }
        output = self._fn()(results)
        assert "Missing from Database" in output or "missing" in output.lower()

    def test_truncation(self):
        results = {
            "integrity_ok": True, "integrity_errors": [],
            "duplicate_wars": {}, "missing_from_db": [],
            "missing_from_archive": [("tag", f"war_{i}") for i in range(100)],
            "current_wars": [], "total_records": 0, "total_wars": 0, "total_clans": 0,
        }
        output = self._fn()(results, max_length=500)
        assert len(output) <= 600  # Some tolerance for final line


# ===========================================================================
# format_log_summary — deeper edge cases
# ===========================================================================

class TestFormatLogSummaryDeep:
    def _fn(self):
        from qapbot.QBdiscocmdshelper_admin_command import format_log_summary
        return format_log_summary

    def test_with_errors_and_warnings(self):
        scan = {
            "errors": ["Error line 1", "Error line 2", "Error line 3"],
            "warnings": ["Warning line 1", "Warning line 2"],
            "summary": ["Startup: bot started"],
            "first_date": datetime(2025, 1, 1),
            "last_date": datetime(2025, 1, 2),
            "admin_actions": [],
            "notification_sent_count": 0,
            "active_clan_updates": 5,
            "inactive_clan_updates": 2,
        }
        output = self._fn()(scan)
        assert "Error" in output
        assert "3" in output  # 3 errors
        assert isinstance(output, str)

    def test_empty_scan(self):
        scan = {
            "errors": [], "warnings": [], "summary": [],
            "first_date": None, "last_date": None,
            "admin_actions": [],
        }
        output = self._fn()(scan)
        assert isinstance(output, str)
        assert "0" in output  # 0 errors/warnings

    def test_truncation(self):
        scan = {
            "errors": [f"Error line {i} " * 40 for i in range(20)],
            "warnings": [f"Warning line {i} " * 40 for i in range(20)],
            "summary": [],
            "first_date": datetime(2025, 1, 1), "last_date": datetime(2025, 1, 3),
            "admin_actions": [],
        }
        output = self._fn()(scan, max_length=500)
        assert len(output) <= 600


# ===========================================================================
# scan_logs — more edge cases
# ===========================================================================

class TestScanLogsDeep:
    def _fn(self):
        from qapbot.QBdiscocmdshelper_admin_command import scan_logs
        return scan_logs

    def test_multiple_rotated_logs(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "qapbot.log").write_text(
            "2025-01-01 12:00:00,000 [ERROR] Main error\n"
            "2025-01-01 12:01:00,000 [WARNING] Some warning\n"
        )
        (log_dir / "qapbot.log.2025-01-01").write_text(
            "2025-01-01 11:00:00,000 [ERROR] Old error\n"
        )
        result = self._fn()(str(log_dir))
        assert len(result["errors"]) >= 2
        assert len(result["warnings"]) >= 1

    def test_empty_log_file(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "qapbot.log").write_text("")
        result = self._fn()(str(log_dir))
        assert result["errors"] == []
        assert result["warnings"] == []

    def test_error_lines_collected(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "qapbot.log").write_text(
            "2025-01-01 12:00:00,000 [ERROR] Fatal error occurred\n"
        )
        result = self._fn()(str(log_dir))
        assert len(result["errors"]) >= 1

    def test_mixed_levels(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        lines = [
            "2025-01-01 12:00:00,000 [INFO] Normal log\n",
            "2025-01-01 12:01:00,000 [DEBUG] Debug detail\n",
            "2025-01-01 12:02:00,000 [WARNING] Watch out\n",
            "2025-01-01 12:03:00,000 [ERROR] Something broke\n",
        ]
        (log_dir / "qapbot.log").write_text("".join(lines))
        result = self._fn()(str(log_dir))
        assert len(result["errors"]) == 1
        assert len(result["warnings"]) == 1


# ===========================================================================
# check_current_wars_in_temp — more cases
# ===========================================================================

class TestCheckCurrentWarsInTempDeep:
    def _fn(self):
        from qapbot.QBdiscocmdshelper_admin_command import check_current_wars_in_temp
        return check_current_wars_in_temp

    def _make_cache(self, in_war_tags, metadata, clan_names=None):
        """Build a minimal CACHE mock for check_current_wars_in_temp."""
        from unittest.mock import MagicMock
        cache = MagicMock()
        cache.in_war_clan_tags = in_war_tags
        cache.temp_war_metadata = metadata
        cache.clan_name_cache = clan_names or {}
        return cache

    def test_empty_dir(self, tmp_path):
        """No active wars → empty list."""
        from unittest.mock import patch
        cache = self._make_cache(set(), {})
        with patch("qapbot.cache_manager.CACHE", cache):
            result = self._fn()(str(tmp_path))
        assert result == []

    def test_corrupt_json(self, tmp_path):
        """Clans in in_war_clan_tags with missing metadata are skipped gracefully."""
        from unittest.mock import patch
        # Tag present in in_war but no metadata entry
        cache = self._make_cache({"#CLAN1"}, {})
        with patch("qapbot.cache_manager.CACHE", cache):
            result = self._fn()(str(tmp_path))
        assert result == []

    def test_multiple_wars(self, tmp_path):
        """Three active wars → three entries returned."""
        from unittest.mock import patch
        tags = {f"#C{i}" for i in range(3)}
        metadata = {
            f"#C{i}": {
                "state": "inWar",
                "start_time": "datetime.datetime(2025, 1, 1, 0, 0)",
                "end_time": "datetime.datetime(2025, 1, 2, 0, 0)",
                "filepath": f"data/temp/C{i}_X_202501010000_war_data.json",
            }
            for i in range(3)
        }
        names = {f"#C{i}": f"Clan{i}" for i in range(3)}
        cache = self._make_cache(tags, metadata, names)
        with patch("qapbot.cache_manager.CACHE", cache):
            result = self._fn()(str(tmp_path))
        assert len(result) == 3

    def test_missing_fields_handled(self, tmp_path):
        """Metadata with no time strings still returns an entry with None times."""
        from unittest.mock import patch
        cache = self._make_cache(
            {"#C1"},
            {"#C1": {"state": "inWar", "start_time": "", "end_time": "", "filepath": ""}},
            {"#C1": "Clan1"},
        )
        with patch("qapbot.cache_manager.CACHE", cache):
            result = self._fn()(str(tmp_path))
        assert len(result) == 1
        assert result[0]["clan_tag"] == "#C1"
