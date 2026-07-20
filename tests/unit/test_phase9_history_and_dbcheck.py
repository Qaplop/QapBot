from __future__ import annotations
# pyright: reportPrivateUsage=false, reportUnknownParameterType=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest

import QBhelperfunctions as hf
from qapbot.QBdiscocmdshelper_admin_command import check_database_consistency


class _FakeDbManager:
    def __init__(self, *, raise_on_integrity: bool = False) -> None:
        self.raise_on_integrity = raise_on_integrity

    def check_integrity_sync(self) -> tuple[bool, list[str]]:
        if self.raise_on_integrity:
            raise RuntimeError("boom")
        return True, []

    def get_global_db_statistics_sync(self) -> Dict[str, int]:
        return {"attacks_count": 3, "wars_count": 2, "clans_count": 1, "players_count": 5, "players_tracked_count": 0}

    def get_all_war_summaries_brief_sync(self) -> list[tuple[str, str, str, int]]:
        return [
            ("OPP_202602011000", "#CLAN", "2026-02-01T10:00:00", 0),
            ("OPP_202602020800", "#CLAN", "2026-02-02T08:00:00", 0),  # 22 h apart → within 48 h duplicate window
        ]

    def war_attacks_exist_sync(self, _clan_tag: str, _war_id: str) -> bool:
        return False

    def get_all_war_summary_keys_sync(self) -> "frozenset[tuple[str, str]]":
        return frozenset()

    def get_recent_war_summaries_sync(self, days: int = 28) -> list[tuple[str, str, str]]:
        _ = days
        return [("#CLAN", "MISSING_WAR", "2026-02-15T10:00:00")]


def _war_not_in_history(_clan_tag: str, _war_id: str) -> bool:
    return False


@pytest.mark.smoke
def test_process_war_history_duplicate_temp_deleted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    import QBcsvhandling

    temp_dir = tmp_path / "data" / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    fname = "CLAN_OPP_202602011000_war_data.json"
    archive_shard = tmp_path / QBcsvhandling.get_war_shard_dir("CLAN", "archive")
    archive_shard.mkdir(parents=True, exist_ok=True)

    payload = {"same": "content"}
    temp_file = temp_dir / fname
    archive_file = archive_shard / fname
    temp_file.write_text(json.dumps(payload), encoding="utf-8")
    archive_file.write_text(json.dumps(payload), encoding="utf-8")

    out = hf._process_war_history("#CLAN", str(temp_file), war_type_str="random")

    assert out is True
    assert temp_file.exists() is False


@pytest.mark.smoke
def test_process_war_history_invalid_json_structure_returns_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    temp_dir = tmp_path / "data" / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    temp_file = temp_dir / "CLAN_OPP_202602011000_war_data.json"
    temp_file.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")

    out = hf._process_war_history("#CLAN", str(temp_file), war_type_str="random")

    assert out is False


@pytest.mark.smoke
def test_process_war_history_missing_opponent_archives_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    import QBcsvhandling

    temp_dir = tmp_path / "data" / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    fname = "CLAN_OPP_202602011000_war_data.json"
    temp_file = temp_dir / fname
    temp_file.write_text(
        json.dumps(
            {
                "clan": {"members": []},
                "opponent": None,
                "start_time": "datetime.datetime(2026, 2, 1, 10, 0)",
            }
        ),
        encoding="utf-8",
    )

    out = hf._process_war_history("#CLAN", str(temp_file), war_type_str="random")

    assert out is True
    assert temp_file.exists() is False
    archive_shard = tmp_path / QBcsvhandling.get_war_shard_dir("CLAN", "archive")
    assert (archive_shard / fname).exists() is True


@pytest.mark.smoke
def test_process_war_history_new_war_appends_and_invalidates_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    temp_dir = tmp_path / "data" / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    temp_file = temp_dir / "CLAN_OPP_202602011000_war_data.json"
    temp_file.write_text(
        json.dumps(
            {
                "state": "war_ended",
                "type": "random",
                "start_time": "datetime.datetime(2026, 2, 1, 10, 0)",
                "clan": {"tag": "#CLAN", "members": []},
                "opponent": {"tag": "#OPP", "members": []},
            }
        ),
        encoding="utf-8",
    )

    import QBcsvhandling

    called: Dict[str, Any] = {}

    def _append_current_war_to_history(clan_tag: str, file_path: str, **kwargs: Any) -> None:
        called["clan_tag"] = clan_tag
        called["file_path"] = file_path

    monkeypatch.setattr(QBcsvhandling, "_append_current_war_to_history", _append_current_war_to_history)
    monkeypatch.setattr(hf, "_is_war_in_history", _war_not_in_history)

    monkeypatch.setattr(hf.CACHE, "clan_history", {"#CLAN": [{"x": 1}], "#OTHER": [{"y": 2}]}, raising=False)
    monkeypatch.setattr(
        hf.CACHE,
        "history_cache",
        {
            ("#CLAN", 1, 2026): [{"x": 1}],
            ("#CLAN", 2, 2026): [{"x": 2}],
            ("#OTHER", 1, 2026): [{"y": 1}],
        },
        raising=False,
    )

    out = hf._process_war_history("#CLAN", str(temp_file), war_type_str=None)

    assert out is True
    assert called["clan_tag"] == "#CLAN"
    assert called["file_path"] == str(temp_file)
    assert "#CLAN" not in hf.CACHE.clan_history
    assert "#OTHER" in hf.CACHE.clan_history
    assert ("#CLAN", 1, 2026) not in hf.CACHE.history_cache
    assert ("#CLAN", 2, 2026) not in hf.CACHE.history_cache
    assert ("#OTHER", 1, 2026) in hf.CACHE.history_cache


@pytest.mark.smoke
def test_check_database_consistency_db_manager_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import qapbot.cache_manager as cm

    monkeypatch.setattr(cm, "CACHE", SimpleNamespace(db_manager=None))

    out = check_database_consistency(str(tmp_path / "qapbot.db"), str(tmp_path))

    assert out["integrity_ok"] is False
    assert out["integrity_errors"] == ["Database manager not initialized"]


@pytest.mark.smoke
def test_check_database_consistency_missing_db_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import qapbot.cache_manager as cm

    monkeypatch.setattr(cm, "CACHE", SimpleNamespace(db_manager=_FakeDbManager()))

    out = check_database_consistency(str(tmp_path / "missing.db"), str(tmp_path))

    assert out["integrity_ok"] is False
    assert "Database file not found" in out["integrity_errors"][0]


@pytest.mark.smoke
def test_check_database_consistency_query_exception(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import qapbot.cache_manager as cm

    db_file = tmp_path / "qapbot.db"
    db_file.write_text("", encoding="utf-8")

    monkeypatch.setattr(cm, "CACHE", SimpleNamespace(db_manager=_FakeDbManager(raise_on_integrity=True)))

    out = check_database_consistency(str(db_file), str(tmp_path), include_integrity=True)

    assert out["integrity_ok"] is False
    assert out["integrity_errors"][0].startswith("Database query failed:")


@pytest.mark.smoke
def test_check_database_consistency_success_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import qapbot.cache_manager as cm

    data_dir = tmp_path / "data"
    archive_dir = data_dir / "archive"
    temp_dir = data_dir / "temp"
    archive_dir.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)

    archive_file = archive_dir / "CLAN_OPP_202602011000_war_data.json"
    archive_file.write_text(
        json.dumps(
            {
                "opponent": {"tag": "#OPP"},
                "start_time": "datetime.datetime(2026, 2, 1, 10, 0)",
            }
        ),
        encoding="utf-8",
    )

    temp_file = temp_dir / "CLAN_OPP_202602011000_war_data.json"
    temp_file.write_text(
        json.dumps(
            {
                "clan": {"name": "ClanA"},
                "state": "in_war",
                "start_time": "datetime.datetime(2026, 2, 1, 10, 0)",
                "end_time": "datetime.datetime(2026, 2, 2, 10, 0)",
            }
        ),
        encoding="utf-8",
    )

    db_file = tmp_path / "qapbot.db"
    db_file.write_text("", encoding="utf-8")

    monkeypatch.setattr(cm, "CACHE", SimpleNamespace(
        db_manager=_FakeDbManager(),
        in_war_clan_tags={"#CLAN"},
        temp_war_metadata={
            "#CLAN": {
                "state": "inWar",
                "start_time": "datetime.datetime(2026, 1, 1, 0, 0)",
                "end_time": "datetime.datetime(2026, 1, 2, 0, 0)",
                "filepath": "data/temp/CLAN_X_202601010000_war_data.json",
            }
        },
        clan_name_cache={"#CLAN": "TestClan"},
    ))

    out = check_database_consistency(str(db_file), str(data_dir))

    assert out["integrity_ok"] is True
    assert out["total_records"] == 3
    assert out["total_wars"] == 2
    assert out["total_clans"] == 1
    assert len(out["duplicate_wars"]) == 1
    assert len(out["missing_from_db"]) == 1
    assert out["missing_from_archive"] == [("#CLAN", "MISSING_WAR")]
    assert len(out["current_wars"]) == 1
