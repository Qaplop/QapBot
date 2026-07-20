"""Extended edge-case tests for QBcsvhandling._load_war_data_from_json.

Covers: multi-member wars, members without attacks, missing bestOpponentAttack,
auto-discovery path, townhall=0 default, and multiple attack aggregation.
"""
# pyright: reportPrivateUsage=false, reportUnknownVariableType=false
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

import pytest

from tests.builders import make_war_json, make_war_json_member


def _load_war_data(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    import QBcsvhandling
    return QBcsvhandling._load_war_data_from_json(*args, **kwargs)


# ---------------------------------------------------------------------------
# Multi-member and aggregation
# ---------------------------------------------------------------------------

class TestMultiMemberWar:

    @pytest.mark.smoke
    def test_multiple_members_all_parsed(self, tmp_path: Path) -> None:
        war = make_war_json(
            clan_members=[
                make_war_json_member(tag="#P1", name="Alice", attacks=[{"stars": 3}]),
                make_war_json_member(tag="#P2", name="Bob", attacks=[{"stars": 2}, {"stars": 1}]),
                make_war_json_member(tag="#P3", name="Charlie", attacks=[]),
            ],
        )
        fp = tmp_path / "war.json"
        fp.write_text(json.dumps(war), encoding="utf-8")

        stats = _load_war_data("#CLAN1", json_file_path=str(fp))
        assert len(stats) == 3
        assert stats["#P1"]["Stars"] == 3
        assert stats["#P2"]["Stars"] == 3
        assert stats["#P3"]["Stars"] == 0

    @pytest.mark.smoke
    def test_multiple_attacks_summed(self, tmp_path: Path) -> None:
        war = make_war_json(
            clan_members=[
                make_war_json_member(
                    tag="#P1",
                    attacks=[{"stars": 3}, {"stars": 2}],
                ),
            ],
        )
        fp = tmp_path / "war.json"
        fp.write_text(json.dumps(war), encoding="utf-8")

        stats = _load_war_data("#CLAN1", json_file_path=str(fp))
        assert stats["#P1"]["Stars"] == 5
        assert stats["#P1"]["Attacks"] == 2


# ---------------------------------------------------------------------------
# Missing / optional fields
# ---------------------------------------------------------------------------

class TestMissingFields:

    @pytest.mark.smoke
    def test_no_best_opponent_attack_yields_zero_defensive(self, tmp_path: Path) -> None:
        member = make_war_json_member(tag="#P1")
        member.pop("bestOpponentAttack", None)

        war = make_war_json(clan_members=[member])
        fp = tmp_path / "war.json"
        fp.write_text(json.dumps(war), encoding="utf-8")

        stats = _load_war_data("#CLAN1", json_file_path=str(fp))
        assert stats["#P1"]["Defensive_Stars"] == 0

    @pytest.mark.smoke
    def test_missing_townhall_defaults_to_zero(self, tmp_path: Path) -> None:
        member = make_war_json_member(tag="#P1")
        member.pop("townhall", None)

        war = make_war_json(clan_members=[member])
        fp = tmp_path / "war.json"
        fp.write_text(json.dumps(war), encoding="utf-8")

        stats = _load_war_data("#CLAN1", json_file_path=str(fp))
        assert stats["#P1"]["TH_lvl"] == 0

    @pytest.mark.smoke
    def test_no_attacks_key_still_works(self, tmp_path: Path) -> None:
        member = make_war_json_member(tag="#P1")
        member.pop("attacks", None)

        war = make_war_json(clan_members=[member])
        fp = tmp_path / "war.json"
        fp.write_text(json.dumps(war), encoding="utf-8")

        stats = _load_war_data("#CLAN1", json_file_path=str(fp))
        # Member without attacks key: should still be parsed
        assert "#P1" in stats
        assert stats["#P1"]["Attacks"] == 0

    @pytest.mark.smoke
    def test_member_without_tag_skipped(self, tmp_path: Path) -> None:
        member = make_war_json_member(tag="#P1")
        no_tag_member = {"name": "NoTag", "townhall": 10, "attacks": []}

        war = make_war_json(clan_members=[member, no_tag_member])
        fp = tmp_path / "war.json"
        fp.write_text(json.dumps(war), encoding="utf-8")

        stats = _load_war_data("#CLAN1", json_file_path=str(fp))
        assert len(stats) == 1  # only #P1


# ---------------------------------------------------------------------------
# Missed attacks calculation
# ---------------------------------------------------------------------------

class TestMissedAttacksCalculation:

    @pytest.mark.smoke
    def test_ongoing_war_missed_attacks_zero(self, tmp_path: Path) -> None:
        war = make_war_json(
            state="in_war",
            attacks_per_member=2,
            clan_members=[
                make_war_json_member(tag="#P1", attacks=[{"stars": 3}]),
            ],
        )
        fp = tmp_path / "war.json"
        fp.write_text(json.dumps(war), encoding="utf-8")

        stats = _load_war_data("#CLAN1", json_file_path=str(fp), for_finalization=False)
        assert stats["#P1"]["Missed_Attacks"] == 0

    @pytest.mark.smoke
    def test_finalization_calculates_missed(self, tmp_path: Path) -> None:
        war = make_war_json(
            state="in_war",
            attacks_per_member=2,
            clan_members=[
                make_war_json_member(tag="#P1", attacks=[{"stars": 3}]),
            ],
        )
        fp = tmp_path / "war.json"
        fp.write_text(json.dumps(war), encoding="utf-8")

        stats = _load_war_data("#CLAN1", json_file_path=str(fp), for_finalization=True)
        assert stats["#P1"]["Missed_Attacks"] == 1

    @pytest.mark.smoke
    def test_war_ended_always_calculates_missed(self, tmp_path: Path) -> None:
        war = make_war_json(
            state="war_ended",
            attacks_per_member=2,
            clan_members=[
                make_war_json_member(tag="#P1", attacks=[]),
            ],
        )
        fp = tmp_path / "war.json"
        fp.write_text(json.dumps(war), encoding="utf-8")

        stats = _load_war_data("#CLAN1", json_file_path=str(fp), for_finalization=False)
        assert stats["#P1"]["Missed_Attacks"] == 2

    @pytest.mark.smoke
    def test_all_attacks_used_zero_missed(self, tmp_path: Path) -> None:
        war = make_war_json(
            attacks_per_member=2,
            clan_members=[
                make_war_json_member(tag="#P1", attacks=[{"stars": 3}, {"stars": 2}]),
            ],
        )
        fp = tmp_path / "war.json"
        fp.write_text(json.dumps(war), encoding="utf-8")

        stats = _load_war_data("#CLAN1", json_file_path=str(fp), for_finalization=True)
        assert stats["#P1"]["Missed_Attacks"] == 0


class TestBuildPerAttackRowsIncompleteWar:

    @pytest.mark.smoke
    def test_incomplete_war_can_store_only_real_attacks(self) -> None:
        import QBcsvhandling

        war = make_war_json(
            state="in_war",
            attacks_per_member=2,
            clan_members=[
                make_war_json_member(tag="#P1", attacks=[{"order": 1, "stars": 3, "destruction": 100.0}]),
                make_war_json_member(tag="#P2", attacks=[]),
            ],
        )

        rows = QBcsvhandling.build_per_attack_rows(
            war,
            "#CLAN1",
            for_finalization=False,
            include_missed_sentinels=False,
        )

        assert len(rows) == 1
        assert rows[0]["PlayerID"] == "#P1"
        assert rows[0]["attack_order"] == 1
        assert rows[0]["Missed_Attacks"] == 0

    @pytest.mark.smoke
    def test_incomplete_war_default_keeps_sentinel_behavior(self) -> None:
        import QBcsvhandling

        war = make_war_json(
            state="in_war",
            attacks_per_member=2,
            clan_members=[
                make_war_json_member(tag="#P1", attacks=[]),
            ],
        )

        rows = QBcsvhandling.build_per_attack_rows(
            war,
            "#CLAN1",
            for_finalization=True,
        )

        assert len(rows) == 1
        assert rows[0]["attack_order"] == 0
        assert rows[0]["Missed_Attacks"] == 2


# ---------------------------------------------------------------------------
# War ID generation
# ---------------------------------------------------------------------------

class TestWarIdGeneration:

    @pytest.mark.smoke
    def test_war_id_format_from_timestamp(self, tmp_path: Path) -> None:
        war = make_war_json(
            start_time="<Timestamp time=datetime.datetime(2026, 3, 15, 14, 30, 0) seconds_until=-1>",
            opponent_tag="#2YOPP",
        )
        fp = tmp_path / "war.json"
        fp.write_text(json.dumps(war), encoding="utf-8")

        stats = _load_war_data("#CLAN1", json_file_path=str(fp))
        assert stats["#P1"]["WarID"] == "2YOPP_202603151430"

    @pytest.mark.smoke
    def test_date_iso_from_timestamp(self, tmp_path: Path) -> None:
        war = make_war_json(
            start_time="<Timestamp time=datetime.datetime(2026, 12, 25, 8, 0, 0) seconds_until=-1>",
        )
        fp = tmp_path / "war.json"
        fp.write_text(json.dumps(war), encoding="utf-8")

        stats = _load_war_data("#CLAN1", json_file_path=str(fp))
        assert stats["#P1"]["Date"] == "2026-12-25T08:00"


# ---------------------------------------------------------------------------
# Auto-discovery path (glob-based)
# ---------------------------------------------------------------------------

class TestAutoDiscovery:

    @pytest.mark.smoke
    def test_no_file_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When no JSON exists in temp, returns empty dict."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data" / "temp").mkdir(parents=True, exist_ok=True)

        stats = _load_war_data("#CLAN1")
        assert stats == {}

    @pytest.mark.smoke
    def test_auto_discovers_matching_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Auto-discovery finds a file matching the clan tag pattern in the correct shard."""
        monkeypatch.chdir(tmp_path)
        import QBcsvhandling
        shard_dir = Path(QBcsvhandling.get_war_shard_dir("CLAN1", os.path.join("data", "temp")))
        shard_dir.mkdir(parents=True, exist_ok=True)

        war = make_war_json(clan_tag="#CLAN1")
        (shard_dir / "CLAN1_OPP_202601010000_war_data.json").write_text(json.dumps(war), encoding="utf-8")

        stats = _load_war_data("#CLAN1")
        assert len(stats) > 0

    @pytest.mark.smoke
    def test_preparation_state_no_missed(self, tmp_path: Path) -> None:
        """Preparation state: members haven't attacked yet, missed=0."""
        war = make_war_json(
            state="preparation",
            clan_members=[
                make_war_json_member(tag="#P1", attacks=[]),
            ],
        )
        fp = tmp_path / "war.json"
        fp.write_text(json.dumps(war), encoding="utf-8")

        stats = _load_war_data("#CLAN1", json_file_path=str(fp), for_finalization=False)
        assert stats["#P1"]["Missed_Attacks"] == 0


# ---------------------------------------------------------------------------
# Caller-level integration: _append_current_war_to_history with in_war JSON
# ---------------------------------------------------------------------------

class TestAppendCurrentWarToHistoryIncompleteWar:
    """Verify that the _append_current_war_to_history caller passes the correct
    parameters to build_per_attack_rows when the snapshot state is 'in_war'.

    This guards against a regression where the caller could inadvertently re-enable
    speculative missed-attack storage without the direct-call unit tests catching it.
    """

    @pytest.mark.smoke
    def test_in_war_snapshot_no_sentinel_no_missed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_append_current_war_to_history must not emit sentinels or missed>0 for in_war wars."""
        import types

        import QBcsvhandling
        import qapbot.cache_manager as cache_mod

        captured_rows: list[list[dict]] = []
        captured_sentinels: list[bool] = []
        original_build = QBcsvhandling.build_per_attack_rows

        def spy_build(war_data, clan_tag, *, for_finalization=False, include_missed_sentinels=True):  # type: ignore[no-untyped-def]
            rows = original_build(
                war_data,
                clan_tag,
                for_finalization=for_finalization,
                include_missed_sentinels=include_missed_sentinels,
            )
            captured_rows.append(rows)
            captured_sentinels.append(include_missed_sentinels)
            return rows

        monkeypatch.setattr(QBcsvhandling, "build_per_attack_rows", spy_build)

        # Stub CACHE on the cache_manager module so the local `from qapbot.cache_manager import CACHE`
        # inside _append_current_war_to_history picks it up.
        fake_db = types.SimpleNamespace(
            add_war_data_sync=lambda *a, **kw: None,
            __bool__=lambda self: True,
        )
        fake_cache = types.SimpleNamespace(db_manager=fake_db)
        monkeypatch.setattr(cache_mod, "CACHE", fake_cache)

        war = make_war_json(
            state="in_war",
            attacks_per_member=2,
            clan_members=[
                make_war_json_member(tag="#P1", attacks=[{"order": 1, "stars": 3, "destruction": 100.0}]),
                make_war_json_member(tag="#P2", attacks=[]),  # would become sentinel under old code
            ],
        )

        QBcsvhandling._append_current_war_to_history(  # type: ignore[attr-defined]
            "#CLAN1",
            war_obj=war,
        )

        assert captured_sentinels, "build_per_attack_rows was not called"
        assert captured_sentinels[0] is False, (
            "_append_current_war_to_history must pass include_missed_sentinels=False for in_war state"
        )
        rows = captured_rows[0]
        # Only the real attack row for #P1 should appear; #P2 has no attacks and no sentinel
        assert len(rows) == 1, f"Expected 1 row (real attack only), got {len(rows)}"
        assert rows[0]["PlayerID"] == "#P1"
        assert rows[0]["Missed_Attacks"] == 0
