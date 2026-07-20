"""Extended pure-function tests for QBhelperfunctions.

Targets branches and modes not covered by test_helpers_pure.py:
- _merge_entries: mode-based filtering, multi-war dedup, same-date TH upgrade
- _parse_war_stats_from_api: no-attack members, multiple members, no best_opp
- calculate_content_hash: diverse countdown/prediction lines
"""
# pyright: reportPrivateUsage=false
from __future__ import annotations

import pytest

from tests.builders import (
    FakeAttack,
    FakeBestOppAttack,
    FakeWar,
    FakeWarClan,
    FakeWarMember,
    make_history_row,
    make_temp_stats,
)


# ---------------------------------------------------------------------------
# _merge_entries – mode-based filtering
# ---------------------------------------------------------------------------

class TestMergeEntriesFiltering:
    """Each mode filters out players who don't meet criteria."""

    @pytest.mark.smoke
    def test_attack_mode_excludes_zero_attacks(self) -> None:
        from QBhelperfunctions import _merge_entries

        rows = [make_history_row(tag="#P1", stars=0, attacks=0, missed=2)]
        merged = _merge_entries(rows, {}, war_in_progress=False, mode="attack")
        assert "#P1" not in merged

    @pytest.mark.smoke
    def test_attack_mode_includes_players_with_attacks(self) -> None:
        from QBhelperfunctions import _merge_entries

        rows = [make_history_row(tag="#P1", stars=3, attacks=1)]
        merged = _merge_entries(rows, {}, war_in_progress=False, mode="attack")
        assert "#P1" in merged

    @pytest.mark.smoke
    def test_missedattacks_mode_excludes_zero_missed(self) -> None:
        from QBhelperfunctions import _merge_entries

        rows = [make_history_row(tag="#P1", stars=6, attacks=2, missed=0)]
        merged = _merge_entries(rows, {}, war_in_progress=False, mode="missedattacks")
        assert "#P1" not in merged

    @pytest.mark.smoke
    def test_missedattacks_mode_includes_players_with_missed(self) -> None:
        from QBhelperfunctions import _merge_entries

        rows = [make_history_row(tag="#P1", missed=2, attacks=0)]
        merged = _merge_entries(rows, {}, war_in_progress=False, mode="missedattacks")
        assert "#P1" in merged

    @pytest.mark.smoke
    def test_avgstars_mode_excludes_zero_attacks(self) -> None:
        from QBhelperfunctions import _merge_entries

        rows = [make_history_row(tag="#P1", stars=0, attacks=0)]
        merged = _merge_entries(rows, {}, war_in_progress=False, mode="avgstars")
        assert "#P1" not in merged

    @pytest.mark.smoke
    def test_attackdefratio_mode_excludes_zero_attacks(self) -> None:
        from QBhelperfunctions import _merge_entries

        rows = [make_history_row(tag="#P1", stars=0, attacks=0)]
        merged = _merge_entries(rows, {}, war_in_progress=False, mode="attackdefratio")
        assert "#P1" not in merged

    @pytest.mark.smoke
    def test_currentwar_mode_includes_all_players(self) -> None:
        from QBhelperfunctions import _merge_entries

        rows = [make_history_row(tag="#P1", stars=0, attacks=0, missed=0)]
        merged = _merge_entries(rows, {}, war_in_progress=False, mode="currentwar")
        assert "#P1" in merged

    @pytest.mark.smoke
    def test_unknown_mode_defaults_to_attack_filter(self) -> None:
        from QBhelperfunctions import _merge_entries

        rows = [
            make_history_row(tag="#ATK", stars=3, attacks=1),
            make_history_row(tag="#NOATK", stars=0, attacks=0),
        ]
        merged = _merge_entries(rows, {}, war_in_progress=False, mode="some_unknown_mode")
        assert "#ATK" in merged
        assert "#NOATK" not in merged

    @pytest.mark.smoke
    def test_cwl_suffix_stripped_from_mode(self) -> None:
        from QBhelperfunctions import _merge_entries

        rows = [make_history_row(tag="#P1", stars=3, attacks=1)]
        merged = _merge_entries(rows, {}, war_in_progress=False, mode="attack_cwl")
        assert "#P1" in merged  # treated as "attack"


# ---------------------------------------------------------------------------
# _merge_entries – aggregation and deduplication
# ---------------------------------------------------------------------------

class TestMergeEntriesAggregation:

    @pytest.mark.smoke
    def test_multi_war_aggregation(self) -> None:
        from QBhelperfunctions import _merge_entries

        rows = [
            make_history_row(war_id="W1", tag="#P1", stars=3, attacks=2, missed=0, defensive_stars=1),
            make_history_row(war_id="W2", tag="#P1", date="2026-01-02T00:00", stars=4, attacks=2, missed=1, defensive_stars=2),
        ]
        merged = _merge_entries(rows, {}, war_in_progress=False, mode="attack")
        p = merged["#P1"]
        assert p["Stars"] == 7
        assert p["Attacks"] == 4
        assert p["Missed_Attacks"] == 1
        assert p["Defensive_Stars"] == 3
        assert p["Wars_Count"] == 2

    @pytest.mark.smoke
    def test_war_in_progress_ignores_temp_missed(self) -> None:
        from QBhelperfunctions import _merge_entries

        history = [make_history_row(tag="#P1", missed=1)]
        temp = {"#P1": make_temp_stats(tag="#P1", missed=2)}
        merged = _merge_entries(history, temp, war_in_progress=True, mode="attack")
        assert merged["#P1"]["Missed_Attacks"] == 1  # temp missed ignored

    @pytest.mark.smoke
    def test_war_ended_includes_temp_missed(self) -> None:
        from QBhelperfunctions import _merge_entries

        history = [make_history_row(tag="#P1", missed=1)]
        temp = {"#P1": make_temp_stats(tag="#P1", missed=2)}
        merged = _merge_entries(history, temp, war_in_progress=False, mode="attack")
        assert merged["#P1"]["Missed_Attacks"] == 3  # both counted

    @pytest.mark.smoke
    def test_same_date_th_takes_max(self) -> None:
        from QBhelperfunctions import _merge_entries

        rows = [
            make_history_row(war_id="W1", tag="#P1", th=14, date="2026-01-01T00:00"),
            make_history_row(war_id="W2", tag="#P1", th=15, date="2026-01-01T00:00"),
        ]
        merged = _merge_entries(rows, {}, war_in_progress=False, mode="currentwar")
        assert merged["#P1"]["TH_lvl"] == 15

    @pytest.mark.smoke
    def test_newer_date_updates_name(self) -> None:
        from QBhelperfunctions import _merge_entries

        rows = [
            make_history_row(war_id="W1", tag="#P1", name="OldName", date="2026-01-01T00:00"),
            make_history_row(war_id="W2", tag="#P1", name="NewName", date="2026-02-01T00:00"),
        ]
        merged = _merge_entries(rows, {}, war_in_progress=False, mode="currentwar")
        assert merged["#P1"]["Player"] == "NewName"

    @pytest.mark.smoke
    def test_def_stars_per_war_calculated(self) -> None:
        from QBhelperfunctions import _merge_entries

        rows = [
            make_history_row(war_id="W1", tag="#P1", defensive_stars=4),
            make_history_row(war_id="W2", tag="#P1", date="2026-01-02T00:00", defensive_stars=6),
        ]
        merged = _merge_entries(rows, {}, war_in_progress=False, mode="currentwar")
        assert merged["#P1"]["Def_Stars_per_War"] == 5.0  # 10/2

    @pytest.mark.smoke
    def test_new_player_from_temp_only(self) -> None:
        from QBhelperfunctions import _merge_entries

        temp = {"#NEW": make_temp_stats(tag="#NEW", name="NewPlayer", stars=6)}
        merged = _merge_entries([], temp, war_in_progress=True, mode="currentwar")
        assert "#NEW" in merged
        assert merged["#NEW"]["Player"] == "NewPlayer"
        assert merged["#NEW"]["Stars"] == 6

    @pytest.mark.smoke
    def test_zero_wars_def_per_war_is_zero(self) -> None:
        """Edge case: player with no tracked wars (shouldn't happen, but defensive)."""
        from QBhelperfunctions import _merge_entries

        # Manually create a scenario where war_sets is empty
        rows = [make_history_row(war_id="", tag="#P1")]  # empty war_id
        merged = _merge_entries(rows, {}, war_in_progress=False, mode="currentwar")
        assert merged["#P1"]["Def_Stars_per_War"] == 0.0

    @pytest.mark.smoke
    def test_multiple_players_independent(self) -> None:
        from QBhelperfunctions import _merge_entries

        rows = [
            make_history_row(tag="#P1", name="Alice", stars=3, attacks=2),
            make_history_row(tag="#P2", name="Bob", stars=5, attacks=2),
        ]
        merged = _merge_entries(rows, {}, war_in_progress=False, mode="attack")
        assert merged["#P1"]["Stars"] == 3
        assert merged["#P2"]["Stars"] == 5


# ---------------------------------------------------------------------------
# _parse_war_stats_from_api
# ---------------------------------------------------------------------------

class TestParseWarStatsFromApi:

    @pytest.mark.smoke
    def test_no_attacks_member(self) -> None:
        from QBhelperfunctions import _parse_war_stats_from_api

        war = FakeWar(
            clan=FakeWarClan(members=[
                FakeWarMember(tag="#P1", name="NoAttacker", attacks=[], best_opponent_attack=None),
            ]),
            attacks_per_member=2,
        )
        stats = _parse_war_stats_from_api(war, "WID", "2026-01-01T00:00")
        assert stats["#P1"]["Stars"] == 0
        assert stats["#P1"]["Attacks"] == 0
        assert stats["#P1"]["Missed_Attacks"] == 2
        assert stats["#P1"]["Defensive_Stars"] == 0

    @pytest.mark.smoke
    def test_multiple_members(self) -> None:
        from QBhelperfunctions import _parse_war_stats_from_api

        war = FakeWar(
            clan=FakeWarClan(members=[
                FakeWarMember(tag="#P1", name="Alice", attacks=[FakeAttack(stars=3)], best_opponent_attack=FakeBestOppAttack(stars=1)),
                FakeWarMember(tag="#P2", name="Bob", attacks=[FakeAttack(stars=2), FakeAttack(stars=3)], best_opponent_attack=FakeBestOppAttack(stars=0)),
            ]),
            attacks_per_member=2,
        )
        stats = _parse_war_stats_from_api(war, "WID", "2026-01-01T00:00")
        assert len(stats) == 2
        assert stats["#P1"]["Stars"] == 3
        assert stats["#P1"]["Missed_Attacks"] == 1
        assert stats["#P2"]["Stars"] == 5
        assert stats["#P2"]["Missed_Attacks"] == 0

    @pytest.mark.smoke
    def test_no_best_opponent_attack(self) -> None:
        from QBhelperfunctions import _parse_war_stats_from_api

        war = FakeWar(
            clan=FakeWarClan(members=[
                FakeWarMember(tag="#P1", name="Safe", attacks=[FakeAttack(stars=3)], best_opponent_attack=None),
            ]),
        )
        stats = _parse_war_stats_from_api(war, "WID", "2026-01-01T00:00")
        assert stats["#P1"]["Defensive_Stars"] == 0

    @pytest.mark.smoke
    def test_all_fields_populated(self) -> None:
        from QBhelperfunctions import _parse_war_stats_from_api

        war = FakeWar(
            clan=FakeWarClan(members=[
                FakeWarMember(
                    tag="#P1",
                    name="FullStats",
                    town_hall=16,
                    attacks=[FakeAttack(stars=3), FakeAttack(stars=2)],
                    best_opponent_attack=FakeBestOppAttack(stars=2),
                ),
            ]),
            attacks_per_member=2,
        )
        stats = _parse_war_stats_from_api(war, "WAR_123", "2026-02-15T12:00")
        p = stats["#P1"]
        assert p["WarID"] == "WAR_123"
        assert p["Date"] == "2026-02-15T12:00"
        assert p["Player"] == "FullStats"
        assert p["PlayerID"] == "#P1"
        assert p["TH_lvl"] == 16
        assert p["Stars"] == 5
        assert p["Attacks"] == 2
        assert p["Missed_Attacks"] == 0
        assert p["Max_Attacks"] == 2
        assert p["Defensive_Stars"] == 2


# ---------------------------------------------------------------------------
# calculate_content_hash
# ---------------------------------------------------------------------------

class TestCalculateContentHash:

    @pytest.mark.smoke
    def test_identical_content_same_hash(self) -> None:
        from QBhelperfunctions import calculate_content_hash

        h1 = calculate_content_hash("Line 1\nLine 2")
        h2 = calculate_content_hash("Line 1\nLine 2")
        assert h1 == h2

    @pytest.mark.smoke
    def test_different_content_different_hash(self) -> None:
        from QBhelperfunctions import calculate_content_hash

        h1 = calculate_content_hash("Line 1")
        h2 = calculate_content_hash("Line 2")
        assert h1 != h2

    @pytest.mark.smoke
    def test_countdown_timer_ignored(self) -> None:
        from QBhelperfunctions import calculate_content_hash

        h1 = calculate_content_hash("Header\nWar ends in: 1h 0m\nBody")
        h2 = calculate_content_hash("Header\nWar ends in: 5h 30m\nBody")
        assert h1 == h2

    @pytest.mark.smoke
    def test_war_starts_timer_ignored(self) -> None:
        from QBhelperfunctions import calculate_content_hash

        h1 = calculate_content_hash("Header\nWar starts in: 10h 5m\nBody")
        h2 = calculate_content_hash("Header\nWar starts in: 2h 0m\nBody")
        assert h1 == h2

    @pytest.mark.smoke
    def test_prediction_line_ignored(self) -> None:
        from QBhelperfunctions import calculate_content_hash

        h1 = calculate_content_hash("Stats\nThe confidence of this prediction is 99%\nEnd")
        h2 = calculate_content_hash("Stats\nThe confidence of this prediction is 12%\nEnd")
        assert h1 == h2

    @pytest.mark.smoke
    def test_empty_lines_stripped(self) -> None:
        from QBhelperfunctions import calculate_content_hash

        h1 = calculate_content_hash("A\n\nB")
        h2 = calculate_content_hash("A\nB")
        assert h1 == h2

    @pytest.mark.smoke
    def test_hash_is_hex_string(self) -> None:
        from QBhelperfunctions import calculate_content_hash

        h = calculate_content_hash("test content")
        assert len(h) == 64  # SHA-256 hex digest
        assert all(c in "0123456789abcdef" for c in h)
