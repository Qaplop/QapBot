from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, cast

import pytest


def _load_war_data(*args: Any, **kwargs: Any) -> dict[str, Any]:
    import QBcsvhandling

    loader = cast(Callable[..., dict[str, Any]], getattr(QBcsvhandling, "_load_war_data_from_json"))
    return loader(*args, **kwargs)


@pytest.mark.smoke
def test_load_war_data_from_json_missing_file_returns_empty_dict(tmp_path: Path):
    missing = tmp_path / "does_not_exist.json"
    stats = _load_war_data("#CLAN", json_file_path=str(missing))
    assert stats == {}


@pytest.mark.smoke
def test_load_war_data_from_json_invalid_json_returns_empty_dict(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not-json}", encoding="utf-8")

    stats = _load_war_data("#CLAN", json_file_path=str(bad))
    assert stats == {}


@pytest.mark.smoke
def test_load_war_data_from_json_unparsable_start_time_returns_empty_dict(tmp_path: Path):
    payload: dict[str, Any] = {
        "state": "in_war",
        "attacks_per_member": 2,
        "start_time": "bad timestamp",
        "clan": {"tag": "#CLAN", "members": []},
        "opponent": {"tag": "#OPP", "members": []},
    }

    file_path = tmp_path / "war.json"
    file_path.write_text(json.dumps(payload), encoding="utf-8")

    stats = _load_war_data("#CLAN", json_file_path=str(file_path))
    assert stats == {}


@pytest.mark.smoke
def test_load_war_data_from_json_missing_clan_or_opponent_returns_empty_dict(tmp_path: Path):
    payload: dict[str, Any] = {
        "state": "in_war",
        "attacks_per_member": 2,
        "start_time": "<Timestamp time=datetime.datetime(2026, 1, 1, 0, 0, 0) seconds_until=-1>",
        "clan": {},
        "opponent": {},
    }

    file_path = tmp_path / "war_missing.json"
    file_path.write_text(json.dumps(payload), encoding="utf-8")

    stats = _load_war_data("#CLAN", json_file_path=str(file_path))
    assert stats == {}


@pytest.mark.smoke
def test_load_war_data_from_json_war_ended_calculates_missed_attacks(tmp_path: Path):
    payload: dict[str, Any] = {
        "state": "war_ended",
        "attacks_per_member": 2,
        "start_time": "<Timestamp time=datetime.datetime(2026, 1, 2, 5, 30, 0) seconds_until=-1>",
        "clan": {
            "tag": "#CLAN1",
            "name": "Clan1",
            "members": [
                {
                    "tag": "#P1",
                    "name": "Alice",
                    "townhall": 15,
                    "attacks": [{"stars": 2}],
                    "bestOpponentAttack": {"stars": 3},
                }
            ],
        },
        "opponent": {"tag": "#OPP", "name": "Opponent", "members": []},
    }

    file_path = tmp_path / "war_ended.json"
    file_path.write_text(json.dumps(payload), encoding="utf-8")

    stats = _load_war_data("#CLAN1", json_file_path=str(file_path), for_finalization=False)

    assert stats["#P1"]["WarID"] == "OPP_202601020530"
    assert stats["#P1"]["Missed_Attacks"] == 1
    assert stats["#P1"]["Defensive_Stars"] == 3
