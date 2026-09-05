"""Tests for QBhelperfunctions war file management — Phase 4 coverage.

Covers: _archive_and_log_war_file, _move_to_archive, _is_war_in_history,
_parse_war_stats_from_api, _process_war_history (partial), manage_war_files (partial).
All tests use tmp_path to avoid touching real data.
"""
# pyright: reportPrivateUsage=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false, reportUnusedImport=false
from __future__ import annotations

import json
import os
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from qapbot.exceptions import WarProcessingError


# ---------------------------------------------------------------------------
# _archive_and_log_war_file
# ---------------------------------------------------------------------------

class TestArchiveAndLogWarFile:
    def test_archive_new_file(self, tmp_path):
        """File is moved from temp to archive shard."""
        from QBhelperfunctions import _archive_and_log_war_file
        import QBcsvhandling

        src = tmp_path / "data" / "temp" / "ABC_DEF_202601010000_war_data.json"
        src.parent.mkdir(parents=True)
        src.write_text("{}")

        old_cwd = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            with patch("QBhelperfunctions.CACHE") as mock_cache:
                mock_cache.db_manager = None
                mock_cache.notification_state = {}
                result = _archive_and_log_war_file(str(src), history_changed=True)
            shard_dir = tmp_path / QBcsvhandling.get_war_shard_dir("ABC", "archive")
            assert result is True
            assert shard_dir.exists()
            assert (shard_dir / "ABC_DEF_202601010000_war_data.json").exists()
            assert not src.exists()
        finally:
            os.chdir(old_cwd)

    def test_archive_replaces_existing(self, tmp_path):
        """Existing archive file is replaced in the shard subdir."""
        from QBhelperfunctions import _archive_and_log_war_file
        import QBcsvhandling

        src = tmp_path / "data" / "temp" / "ABC_DEF_202601010000_war_data.json"
        src.parent.mkdir(parents=True)
        src.write_text('{"new": true}')

        old_cwd = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            shard_path = tmp_path / QBcsvhandling.get_war_shard_dir("ABC", "archive")
            shard_path.mkdir(parents=True, exist_ok=True)
            archive = shard_path / "ABC_DEF_202601010000_war_data.json"
            archive.write_text('{"old": true}')

            with patch("QBhelperfunctions.CACHE") as mock_cache:
                mock_cache.db_manager = None
                mock_cache.notification_state = {}
                result = _archive_and_log_war_file(str(src), history_changed=False)
            assert result is True
            content = json.loads(archive.read_text())
            assert content == {"new": True}
        finally:
            os.chdir(old_cwd)

    def test_file_already_gone(self, tmp_path):
        """Returns True if source file doesn't exist (already processed)."""
        from QBhelperfunctions import _archive_and_log_war_file

        result = _archive_and_log_war_file(str(tmp_path / "nonexistent.json"))
        assert result is True


# ---------------------------------------------------------------------------
# _move_to_archive
# ---------------------------------------------------------------------------

class TestMoveToArchive:
    def test_move_new_file(self, tmp_path):
        from QBhelperfunctions import _move_to_archive
        import QBcsvhandling

        src = tmp_path / "data" / "temp" / "X_Y_war_data.json"
        src.parent.mkdir(parents=True)
        src.write_text("{}")

        old_cwd = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            _move_to_archive(str(src))
            shard_dir = tmp_path / QBcsvhandling.get_war_shard_dir("X", "archive")
            assert (shard_dir / "X_Y_war_data.json").exists()
            assert not src.exists()
        finally:
            os.chdir(old_cwd)

    def test_move_replaces_existing(self, tmp_path):
        from QBhelperfunctions import _move_to_archive
        import QBcsvhandling

        src = tmp_path / "data" / "temp" / "X_Y_war_data.json"
        src.parent.mkdir(parents=True)
        src.write_text('{"v":2}')

        old_cwd = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            shard_path = tmp_path / QBcsvhandling.get_war_shard_dir("X", "archive")
            shard_path.mkdir(parents=True, exist_ok=True)
            archive = shard_path / "X_Y_war_data.json"
            archive.write_text('{"v":1}')

            _move_to_archive(str(src))
            assert json.loads(archive.read_text()) == {"v": 2}
        finally:
            os.chdir(old_cwd)

    def test_source_missing_noop(self, tmp_path):
        """If source file doesn't exist, nothing happens."""
        from QBhelperfunctions import _move_to_archive

        old_cwd = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            _move_to_archive(str(tmp_path / "nonexistent.json"))
            # Should not raise
        finally:
            os.chdir(old_cwd)

    def test_permission_error_raises(self, tmp_path):
        """Exception during move raises WarProcessingError."""
        from QBhelperfunctions import _move_to_archive

        with patch("QBhelperfunctions.os.makedirs", side_effect=PermissionError("denied")):
            with pytest.raises(WarProcessingError):
                _move_to_archive(str(tmp_path / "some_file.json"))


# ---------------------------------------------------------------------------
# _is_war_in_history
# ---------------------------------------------------------------------------

class TestIsWarInHistory:
    def test_empty_war_id(self):
        from QBhelperfunctions import _is_war_in_history
        assert _is_war_in_history("#TAG", "") is False

    def test_none_war_id(self):
        from QBhelperfunctions import _is_war_in_history
        assert _is_war_in_history("#TAG", None) is False  # type: ignore[arg-type]

    def test_war_exists(self):
        from QBhelperfunctions import _is_war_in_history
        with patch("QBhelperfunctions.CACHE") as mock_cache:
            mock_cache.db_manager.war_attacks_exist_sync.return_value = True
            assert _is_war_in_history("#TAG", "OPP_202501011200") is True

    def test_war_not_exists(self):
        from QBhelperfunctions import _is_war_in_history
        with patch("QBhelperfunctions.CACHE") as mock_cache:
            mock_cache.db_manager.war_attacks_exist_sync.return_value = False
            assert _is_war_in_history("#TAG", "OPP_202501011200") is False

    def test_db_not_initialized(self):
        from QBhelperfunctions import _is_war_in_history
        with patch("QBhelperfunctions.CACHE") as mock_cache:
            mock_cache.db_manager = None
            with pytest.raises(WarProcessingError):
                _is_war_in_history("#TAG", "OPP_202501011200")

    def test_db_exception(self):
        from QBhelperfunctions import _is_war_in_history
        with patch("QBhelperfunctions.CACHE") as mock_cache:
            mock_cache.db_manager.war_attacks_exist_sync.side_effect = Exception("DB error")
            with pytest.raises(WarProcessingError, match="Error checking war history"):
                _is_war_in_history("#TAG", "OPP_202501011200")


# ---------------------------------------------------------------------------
# _parse_war_stats_from_api
# ---------------------------------------------------------------------------

def _make_member(tag, name, th, attacks_data=None, best_opp=None):
    """Create a mock clan member for _parse_war_stats_from_api."""
    m = MagicMock()
    m.tag = tag
    m.name = name
    m.town_hall = th
    if attacks_data:
        atk_list = []
        for a in attacks_data:
            atk = MagicMock()
            atk.stars = a["stars"]
            atk.destruction = a.get("destruction", 0.0)
            atk_list.append(atk)
        m.attacks = atk_list
    else:
        m.attacks = None
    if best_opp is not None:
        opp = MagicMock()
        opp.stars = best_opp
        m.best_opponent_attack = opp
    else:
        m.best_opponent_attack = None
    return m


class TestParseWarStatsFromApi:
    def test_basic_parsing(self):
        from QBhelperfunctions import _parse_war_stats_from_api

        war = MagicMock()
        war.attacks_per_member = 2
        war.clan.members = [
            _make_member("#P1", "Alice", 16, [{"stars": 3}, {"stars": 2}], best_opp=1),
            _make_member("#P2", "Bob", 15, None, best_opp=None),
        ]

        result = _parse_war_stats_from_api(war, "OPP_202501011200", "2025-01-01T12:00")

        assert "#P1" in result
        assert result["#P1"]["Stars"] == 5
        assert result["#P1"]["Attacks"] == 2
        assert result["#P1"]["Missed_Attacks"] == 0
        assert result["#P1"]["Defensive_Stars"] == 1
        assert result["#P1"]["TH_lvl"] == 16
        assert result["#P1"]["WarID"] == "OPP_202501011200"
        assert result["#P1"]["Date"] == "2025-01-01T12:00"

        assert "#P2" in result
        assert result["#P2"]["Stars"] == 0
        assert result["#P2"]["Attacks"] == 0
        assert result["#P2"]["Missed_Attacks"] == 2
        assert result["#P2"]["Defensive_Stars"] == 0

    def test_cwl_one_attack(self):
        """CWL wars: attacks_per_member = 1."""
        from QBhelperfunctions import _parse_war_stats_from_api

        war = MagicMock()
        war.attacks_per_member = 1
        war.clan.members = [
            _make_member("#P3", "Charlie", 14, [{"stars": 3}], best_opp=2),
        ]

        result = _parse_war_stats_from_api(war, "CWL_202501011200", "2025-01-01T12:00")
        assert result["#P3"]["Max_Attacks"] == 1
        assert result["#P3"]["Missed_Attacks"] == 0
        assert result["#P3"]["Attacks"] == 1

    def test_partial_attacks(self):
        """Player used 1 of 2 attacks."""
        from QBhelperfunctions import _parse_war_stats_from_api

        war = MagicMock()
        war.attacks_per_member = 2
        war.clan.members = [
            _make_member("#P4", "Dave", 13, [{"stars": 1}]),
        ]

        result = _parse_war_stats_from_api(war, "W1", "2025-01-01T00:00")
        assert result["#P4"]["Missed_Attacks"] == 1
        assert result["#P4"]["Attacks"] == 1

    def test_empty_clan(self):
        """No members returns empty dict."""
        from QBhelperfunctions import _parse_war_stats_from_api

        war = MagicMock()
        war.attacks_per_member = 2
        war.clan.members = []

        result = _parse_war_stats_from_api(war, "W1", "2025-01-01T00:00")
        assert result == {}


# ---------------------------------------------------------------------------
# manage_war_files — high-level scenarios
# ---------------------------------------------------------------------------

class TestManageWarFiles:
    def test_no_war_files(self, tmp_path):
        """No files found — silent return."""
        from QBhelperfunctions import manage_war_files

        old_cwd = os.getcwd()
        os.chdir(str(tmp_path))
        (tmp_path / "data" / "temp").mkdir(parents=True)
        try:
            manage_war_files("#ABC123", None)  # Should not raise
        finally:
            os.chdir(old_cwd)

    def test_corrupted_json_archived(self, tmp_path):
        """Corrupted JSON file gets moved to archive shard."""
        from QBhelperfunctions import manage_war_files
        import QBcsvhandling

        shard_dir = tmp_path / QBcsvhandling.get_war_shard_dir("ABC123", os.path.join("data", "temp"))
        shard_dir.mkdir(parents=True)

        bad_file = shard_dir / "ABC123_OPPONENT_war_data.json"
        bad_file.write_text("NOT VALID JSON {{{")

        old_cwd = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            manage_war_files("#ABC123", None)
            # Bad file should be archived in the correct shard
            archive_shard = tmp_path / QBcsvhandling.get_war_shard_dir("ABC123", "archive")
            assert (archive_shard / "ABC123_OPPONENT_war_data.json").exists()
            assert not bad_file.exists()
        finally:
            os.chdir(old_cwd)

    def test_current_war_skipped(self, tmp_path):
        """File matching current opponent in active state is skipped."""
        from QBhelperfunctions import manage_war_files

        temp_dir = tmp_path / "data" / "temp"
        temp_dir.mkdir(parents=True)

        war_data = {
            "state": "in_war",
            "type": "random",
            "clan": {"tag": "#ABC123", "members": []},
            "opponent": {"tag": "#OPPONENT", "members": []},
        }
        active_file = temp_dir / "ABC123_OPPONENT_war_data.json"
        active_file.write_text(json.dumps(war_data))

        old_cwd = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            manage_war_files("#ABC123", "#OPPONENT")
            # File should still be in temp (not archived)
            assert active_file.exists()
        finally:
            os.chdir(old_cwd)

    def test_war_ended_processed(self, tmp_path):
        """war_ended file triggers _process_war_history."""
        from QBhelperfunctions import manage_war_files

        temp_dir = tmp_path / "data" / "temp"
        temp_dir.mkdir(parents=True)

        war_data = {
            "state": "war_ended",
            "type": "random",
            "clan": {"tag": "#ABC123", "name": "TestClan", "members": [
                {"tag": "#P1", "name": "Player1", "townhall": 16, "attacks": [], "map_position": 1}
            ]},
            "opponent": {"tag": "#OPP", "name": "EnemyClan", "members": []},
            "start_time": "datetime.datetime(2025, 1, 1, 12, 0)",
        }
        ended_file_name = "ABC123_OPP_war_data.json"
        import QBcsvhandling
        shard_dir = tmp_path / QBcsvhandling.get_war_shard_dir("ABC123", os.path.join("data", "temp"))
        shard_dir.mkdir(parents=True)
        ended_file = shard_dir / ended_file_name
        ended_file.write_text(json.dumps(war_data))

        old_cwd = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            with patch("QBhelperfunctions._process_war_history", return_value=True) as mock_proc:
                manage_war_files("#ABC123", None)
                mock_proc.assert_called_once()
        finally:
            os.chdir(old_cwd)

    def test_cwl_non_ended_skipped(self, tmp_path):
        """CWL war in preparation/in_war state is skipped (not finalized)."""
        from QBhelperfunctions import manage_war_files

        temp_dir = tmp_path / "data" / "temp"
        temp_dir.mkdir(parents=True)

        war_data = {
            "state": "in_war",
            "type": "cwl",
            "clan": {"tag": "#ABC123", "members": []},
            "opponent": {"tag": "#OPP", "members": []},
        }
        cwl_file = temp_dir / "ABC123_OPP_war_data.json"
        cwl_file.write_text(json.dumps(war_data))

        old_cwd = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            with patch("QBhelperfunctions._process_war_history") as mock_proc:
                manage_war_files("#ABC123", "#DIFFERENT")
                mock_proc.assert_not_called()
            # File should still be in temp
            assert cwl_file.exists()
        finally:
            os.chdir(old_cwd)


# ---------------------------------------------------------------------------
# process_clan_war_data
# ---------------------------------------------------------------------------

def _make_war_payload(state, opponent_tag, members_data, attacks_per_member=2):
    """Build a war payload dict for process_clan_war_data tests.

    Stage 3 of tracker-0009: Phase 1 no longer returns a `coc.ClanWar`, so these tests feed the
    payload that replaced it. `start_time` keeps the `datetime.datetime(...)` repr form because
    the payload stores `str(coc_war_obj.start_time)` and the war-ID regex parses that.
    """
    return {
        "state": state,
        "attacks_per_member": attacks_per_member,
        "start_time": "datetime.datetime(2025, 6, 15, 10, 30)",
        "clan": {
            "tag": "#MYCLAN",
            "name": "MyClan",
            "members": [
                {
                    "tag": md["tag"],
                    "name": md["name"],
                    "townhall": md.get("th", 16),
                    "attacks": [
                        {"stars": a["stars"], "destruction": a.get("destruction", 0.0)}
                        for a in md.get("attacks", [])
                    ],
                    "bestOpponentAttack": None,
                }
                for md in members_data
            ],
        },
        "opponent": {"tag": opponent_tag, "name": "Enemy", "members": []},
    }


class TestProcessClanWarData:
    def test_preparation_saves_temp_stats(self):
        from QBhelperfunctions import process_clan_war_data

        war_payload = _make_war_payload("preparation", "#OPP123", [
            {"tag": "#P1", "name": "Alice", "th": 16},
            {"tag": "#P2", "name": "Bob", "th": 15, "attacks": [{"stars": 3}]},
        ])

        war_data = {
            "war_payload": war_payload,
            "opponent_tag": "#OPP123",
            "state": "preparation",
        }

        with patch("QBhelperfunctions.CACHE") as mock_cache, \
             patch("QBhelperfunctions.manage_war_files"):
            mock_cache.get_temp_war_stats.return_value = {}
            mock_cache.clan_history = {}
            mock_cache.history_cache = {}

            result = process_clan_war_data("#MYCLAN", war_data)
            assert result is True
            mock_cache.set_temp_war_stats.assert_called_once()
            call_args = mock_cache.set_temp_war_stats.call_args
            stats = call_args[0][1]
            assert "#P1" in stats
            assert "#P2" in stats
            assert stats["#P2"]["Stars"] == 3
            assert stats["#P2"]["Attacks"] == 1
            assert stats["#P1"]["WarID"].startswith("OPP123_")

    def test_war_ended_clears_temp_stats(self):
        from QBhelperfunctions import process_clan_war_data

        war_payload = _make_war_payload("war_ended", "#OPP", [])

        war_data = {
            "war_payload": war_payload,
            "opponent_tag": "#OPP",
            "state": "war_ended",
        }

        with patch("QBhelperfunctions.CACHE") as mock_cache, \
             patch("QBhelperfunctions.manage_war_files"):
            mock_cache.get_temp_war_stats.return_value = {}
            mock_cache.clan_history = {}
            mock_cache.history_cache = {}

            result = process_clan_war_data("#MYCLAN", war_data)
            assert result is True
            mock_cache.set_temp_war_stats.assert_called_once_with("#MYCLAN", {})

    def test_in_war_populates_stats(self):
        from QBhelperfunctions import process_clan_war_data

        war_payload = _make_war_payload("in_war", "#OPP", [
            {"tag": "#P1", "name": "Alice", "attacks": [{"stars": 2}, {"stars": 1}]},
        ])

        war_data = {
            "war_payload": war_payload,
            "opponent_tag": "#OPP",
            "state": "in_war",
        }

        with patch("QBhelperfunctions.CACHE") as mock_cache, \
             patch("QBhelperfunctions.manage_war_files"):
            mock_cache.get_temp_war_stats.return_value = {}
            mock_cache.clan_history = {}
            mock_cache.history_cache = {}

            result = process_clan_war_data("#MYCLAN", war_data)
            assert result is True
            stats = mock_cache.set_temp_war_stats.call_args[0][1]
            assert stats["#P1"]["Stars"] == 3
            assert stats["#P1"]["Missed_Attacks"] == 0  # Still in war

    def test_missing_player_name_returns_false(self):
        """If a member has no name, returns False."""
        from QBhelperfunctions import process_clan_war_data

        war_payload = _make_war_payload("preparation", "#OPP", [])
        # Add a member with empty name
        war_payload["clan"]["members"] = [
            {"tag": "#P1", "name": "", "townhall": 10, "attacks": [], "bestOpponentAttack": None}
        ]

        war_data = {
            "war_payload": war_payload,
            "opponent_tag": "#OPP",
            "state": "preparation",
        }

        with patch("QBhelperfunctions.CACHE") as mock_cache, \
             patch("QBhelperfunctions.manage_war_files"):
            mock_cache.get_temp_war_stats.return_value = {}
            mock_cache.clan_history = {}
            mock_cache.history_cache = {}

            result = process_clan_war_data("#MYCLAN", war_data)
            assert result is False

    def test_war_id_shift_updates_existing(self):
        """When war ID timestamp shifts, existing temp stats are updated."""
        from QBhelperfunctions import process_clan_war_data

        war_payload = _make_war_payload("in_war", "#OPP", [
            {"tag": "#P1", "name": "Alice"},
        ])

        war_data = {
            "war_payload": war_payload,
            "opponent_tag": "#OPP",
            "state": "in_war",
        }

        existing_stats = {
            "#P1": {
                "WarID": "OPP_202506151029",  # Old timestamp
                "Date": "2025-06-15T10:29",
                "Player": "Alice",
                "PlayerID": "#P1",
            }
        }

        with patch("QBhelperfunctions.CACHE") as mock_cache, \
             patch("QBhelperfunctions.manage_war_files"):
            mock_cache.get_temp_war_stats.return_value = existing_stats
            mock_cache.clan_history = {}
            mock_cache.history_cache = {}

            result = process_clan_war_data("#MYCLAN", war_data)
            assert result is True
