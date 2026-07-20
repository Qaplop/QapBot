from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from qapbot.QBdiscocmdshelper_admin_command import (
    check_current_wars_in_temp,
    extract_war_id_from_json_data,
    format_database_check_results,
    format_log_summary,
    parse_log_line,
    scan_logs,
)


def test_parse_log_line_parses_valid_timestamp() -> None:
    line = "2026-02-01 12:34:56,789 [INFO] hello"
    dt, content = parse_log_line(line)

    assert content == line
    assert dt == datetime(2026, 2, 1, 12, 34, 56, 789000)


def test_parse_log_line_handles_invalid_datetime_values() -> None:
    line = "2026-99-01 12:34:56,789 [INFO] impossible-date"
    dt, content = parse_log_line(line)

    assert content == line
    assert dt is None


def test_scan_logs_for_missing_dir_returns_error() -> None:
    result = scan_logs("C:/path/that/does/not/exist")

    assert len(result["errors"]) == 1
    assert "Log directory not found" in result["errors"][0]
    assert result["warnings"] == []


def test_scan_logs_collects_metrics_and_events(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "qapbot.log"
    log_file.write_text(
        "\n".join(
            [
                "2026-02-01 10:00:00,000 [INFO] QapBot started",
                "2026-02-01 10:00:01,000 [INFO] [ACTIVE] Fetching #AAA",
                "2026-02-01 10:00:02,000 [INFO] [INACTIVE] Fetching #BBB",
                "2026-02-01 10:00:03,000 [INFO] [INACTIVE] Smart timestamp set",
                "2026-02-01 10:00:04,000 [INFO] [FINALIZE-OLD-WAR] done",
                "2026-02-01 10:00:05,000 [INFO] [ARCHIVE] Moved file",
                "2026-02-01 10:00:06,000 [DEBUG] [LATE-CHECK] polling",
                "2026-02-01 10:00:07,000 [INFO] [DUPLICATE-SKIP] skip",
                "2026-02-01 10:00:08,000 [INFO] [CWL-ORPHAN-SKIP] keep",
                "2026-02-01 10:00:09,000 [INFO] leaderboard - content changed",
                "2026-02-01 10:00:10,000 [INFO] calculate_win_probability returned 0.9",
                "2026-02-01 10:00:11,000 [INFO] ADMIN ACTION: cleanup",
                "2026-02-01 10:00:12,000 [INFO] ✅ Sent war reminder",
                "2026-02-01 10:00:13,000 [WARNING] warning line",
                "2026-02-01 10:00:14,000 [ERROR] error line",
            ]
        ),
        encoding="utf-8",
    )

    result = scan_logs(str(logs_dir))

    assert result["active_clan_updates"] == 1
    assert result["inactive_clan_updates"] == 1
    assert result["smart_timestamp_calcs"] == 1
    assert result["wars_finalized"] == 1
    assert result["wars_archived"] == 1
    assert result["late_attack_checks"] == 1
    assert result["duplicate_wars_skipped"] == 1
    assert result["cwl_orphan_skips"] == 1
    assert result["notification_sent_count"] == 1
    assert len(result["admin_actions"]) == 1
    assert len(result["warnings"]) == 1
    assert len(result["errors"]) == 1
    assert any(s.startswith("Startup:") for s in result["summary"])
    assert result["first_date"] is not None
    assert result["last_date"] is not None


def test_format_log_summary_reports_no_critical_issues() -> None:
    scan_result: Dict[str, Any] = {
        "errors": [],
        "warnings": [],
        "summary": [],
        "admin_actions": [],
        "notification_sent_count": 0,
        "active_clan_updates": 0,
        "inactive_clan_updates": 0,
        "smart_timestamp_calcs": 0,
        "wars_finalized": 0,
        "wars_archived": 0,
        "cwl_orphan_skips": 0,
        "late_attack_checks": 0,
        "duplicate_wars_skipped": 0,
        "first_date": None,
        "last_date": None,
    }

    output = format_log_summary(scan_result, max_length=500)

    assert "Total Errors:** 0" in output
    assert "Total Warnings:** 0" in output
    assert "No critical issues detected" in output


def test_extract_war_id_from_json_data_uses_datetime_string() -> None:
    json_data: Dict[str, Any] = {
        "opponent": {"tag": "#ABCD1234"},
        "start_time": "datetime.datetime(2026, 1, 3, 14, 5)",
    }

    war_id = extract_war_id_from_json_data(json_data)

    assert war_id == "ABCD1234_202601031405"


def test_extract_war_id_from_json_data_falls_back_to_now() -> None:
    json_data: Dict[str, Any] = {"opponent": {"tag": "#XYZ"}, "start_time": "not-a-datetime"}

    war_id = extract_war_id_from_json_data(json_data)

    assert re.fullmatch(r"XYZ_\d{12}", war_id)


def test_check_current_wars_in_temp_parses_valid_file(tmp_path: Path) -> None:
    from unittest.mock import MagicMock, patch

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # check_current_wars_in_temp now reads from CACHE instead of disk
    cache = MagicMock()
    cache.in_war_clan_tags = {"#2AAA"}
    cache.temp_war_metadata = {
        "#2AAA": {
            "state": "in_war",
            "start_time": "datetime.datetime(2026, 2, 1, 10, 0)",
            "end_time": "datetime.datetime(2026, 2, 2, 10, 0)",
            "filepath": "data/temp/2AAA_9BBB_202601010000_war_data.json",
        }
    }
    cache.clan_name_cache = {"#2AAA": "ClanA"}

    with patch("qapbot.cache_manager.CACHE", cache):
        wars = check_current_wars_in_temp(str(data_dir))

    assert len(wars) == 1
    assert wars[0]["clan_tag"] == "#2AAA"
    assert wars[0]["clan_name"] == "ClanA"
    assert wars[0]["war_state"] == "in_war"
    assert wars[0]["start_time"] is not None
    assert wars[0]["end_time"] is not None


def test_format_database_check_results_handles_truncation() -> None:
    results: Dict[str, Any] = {
        "integrity_ok": False,
        "integrity_errors": ["err1", "err2"],
        "duplicate_wars": {
            "#A|OPP": [("OPP_202601010000", "2026-01-01T00:00:00")],
        },
        "missing_from_db": [("f.json", "#A", "OPP_202601010000")],
        "missing_from_archive": [("#A", "OPP_202601020000")],
        "current_wars": [],
        "total_records": 10,
        "total_wars": 5,
        "total_clans": 1,
    }

    output = format_database_check_results(results, max_length=180)

    assert "Database Validation Check" in output
    assert "truncated for Discord length limit" in output
