from __future__ import annotations

import json

import pytest


@pytest.mark.smoke
def test_load_war_data_from_json_generates_war_id_and_missed_attacks(tmp_path):
    import QBcsvhandling

    war_json = {
        "state": "in_war",
        "attacks_per_member": 2,
        "start_time": "<Timestamp time=datetime.datetime(2026, 1, 1, 0, 0, 0) seconds_until=-1>",
        "clan": {
            "tag": "#CLAN1",
            "name": "TestClan",
            "members": [
                {
                    "tag": "#P1",
                    "name": "Alice",
                    "townhall": 15,
                    "attacks": [{"stars": 3}],
                    "bestOpponentAttack": {"stars": 2},
                }
            ],
        },
        "opponent": {"tag": "#OPP", "name": "Opponent", "members": []},
    }

    file_path = tmp_path / "war.json"
    file_path.write_text(json.dumps(war_json), encoding="utf-8")

    stats = QBcsvhandling._load_war_data_from_json("#CLAN1", json_file_path=str(file_path), for_finalization=False)
    assert "#P1" in stats
    assert stats["#P1"]["WarID"] == "OPP_202601010000"
    assert stats["#P1"]["Missed_Attacks"] == 0  # ongoing war

    stats_final = QBcsvhandling._load_war_data_from_json("#CLAN1", json_file_path=str(file_path), for_finalization=True)
    assert stats_final["#P1"]["Missed_Attacks"] == 1
