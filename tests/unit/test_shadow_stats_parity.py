"""`stats_from_war_payload()` — the AUTHORITATIVE temp-war-stats loop since Stage 3 of
plans/tracker-0009-phase1-war-payload-retention.md.

Phase 1 no longer returns the `coc.ClanWar` at all; `process_clan_war_data()` builds temp stats
from the payload dict alone. These rows are not ephemeral — `_merge_entries()` folds temp stats
into history, so they become the permanent `war_summary`/`war_attacks` rows. §5.1's failure mode
is a silent 0 reaching war history, which a code revert cannot undo.

What these tests pin:

1. The right keys are read. The payload mixes conventions (`townhall`, `map_position` beside
   `bestOpponentAttack`, `opponentAttacks`), and §3 Step 3 warns that this is exactly where a
   silent-zero bug hides.
2. `strict=True` reproduces the retired coc loop's abort-on-malformed-member behaviour. This is
   the one difference between the two paths that shadow mode could never have surfaced: the coc
   loop's `return False` happened *before* the shadow comparison was reached, so no amount of
   live divergence data could have shown it. It was found by reading both loops side by side
   during the Stage 3 flip.

Stage 2's shadow comparator and its tests were removed with the flip — with one path left, it
could only compare the payload against itself. Cross-path parity is still covered offline by
tests/integration/test_war_payload_parity.py, which reconstructs a `coc.ClanWar` from a payload
and runs both extractions (54,192 real wars, zero mismatches).
"""
from __future__ import annotations

from typing import Any, Dict

import pytest

from QBhelperfunctions import stats_from_war_payload


def _payload(members: int = 3) -> Dict[str, Any]:
    return {
        "state": "in_war",
        "clan": {
            "tag": "#AAA", "name": "A",
            "members": [
                {
                    "tag": f"#M{i}", "name": f"m{i}", "townhall": 14 + (i % 2),
                    "map_position": i + 1,
                    "attacks": [{"attackerTag": f"#M{i}", "defenderTag": f"#O{i}",
                                 "stars": 2, "destruction": 55.5, "order": i + 1}],
                    "bestOpponentAttack": {"attackerTag": f"#O{i}", "defenderTag": f"#M{i}",
                                           "stars": 3, "destruction": 100},
                    "opponentAttacks": 1,
                }
                for i in range(members)
            ],
        },
    }


class TestStatsFromPayload:
    def test_reads_every_field_from_the_right_key(self) -> None:
        rows = stats_from_war_payload(_payload(), attacks_per_member=2)

        assert set(rows) == {"#M0", "#M1", "#M2"}
        r = rows["#M0"]
        assert r["Player"] == "m0"
        assert r["PlayerID"] == "#M0"
        assert r["TH_lvl"] == 14, "TH_lvl must come from 'townhall', not 'town_hall'"
        assert r["Stars"] == 2
        assert r["Attacks"] == 1
        assert r["Max_Attacks"] == 2
        assert r["Defensive_Stars"] == 3, "must come from camelCase 'bestOpponentAttack'"
        assert r["Total_Dest_Pct"] == pytest.approx(55.5)

    def test_missed_attacks_is_always_zero(self) -> None:
        """Mirrors the coc path's stated invariant: players can still use remaining attacks."""
        assert all(r["Missed_Attacks"] == 0 for r in stats_from_war_payload(_payload(), 2).values())

    def test_member_without_attacks_yields_explicit_zeros(self) -> None:
        """§5.1's failure mode is a silent 0 — so the legitimate zero case must be pinned."""
        p = _payload()
        p["clan"]["members"][0]["attacks"] = []
        r = stats_from_war_payload(p, 2)["#M0"]

        assert r["Stars"] == 0 and r["Attacks"] == 0 and r["Total_Dest_Pct"] == 0

    def test_member_without_best_opponent_attack(self) -> None:
        p = _payload()
        p["clan"]["members"][0]["bestOpponentAttack"] = None
        assert stats_from_war_payload(p, 2)["#M0"]["Defensive_Stars"] == 0

    def test_nameless_or_tagless_members_are_skipped(self) -> None:
        """The coc path bails on these; the payload path must not invent a row."""
        p = _payload()
        p["clan"]["members"][0]["name"] = ""
        p["clan"]["members"][1]["tag"] = ""
        assert set(stats_from_war_payload(p, 2)) == {"#M2"}

    def test_empty_and_malformed_payloads_do_not_raise(self) -> None:
        assert stats_from_war_payload({}, 2) == {}
        assert stats_from_war_payload({"clan": None}, 2) == {}
        assert stats_from_war_payload({"clan": {"members": None}}, 2) == {}


class TestStrictMode:
    """`strict=True` is what `process_clan_war_data()` uses, so it carries the production
    semantics of the loop this function replaced."""

    def test_strict_returns_none_on_a_member_without_name_or_tag(self) -> None:
        """The retired coc loop logged an error and `return False`d the whole clan rather than
        writing partial stats. Skipping the member instead would silently drop a player from
        history — a quiet data loss, which is exactly what §5.1 warns about."""
        p = _payload()
        p["clan"]["members"][1]["name"] = ""

        assert stats_from_war_payload(p, 2, strict=True, clan_tag="#AAA") is None

    def test_non_strict_still_skips_so_the_two_modes_really_differ(self) -> None:
        """Guards against the overloads collapsing into one behaviour."""
        p = _payload()
        p["clan"]["members"][1]["name"] = ""

        rows = stats_from_war_payload(p, 2)

        assert set(rows) == {"#M0", "#M2"}

    def test_strict_is_identical_to_non_strict_on_well_formed_data(self) -> None:
        """The abort path must be the ONLY difference — otherwise the flip changed live data."""
        p = _payload()

        assert stats_from_war_payload(p, 2, strict=True, clan_tag="#AAA") == stats_from_war_payload(p, 2)

    def test_strict_logs_the_offending_tag(self, caplog: pytest.LogCaptureFixture) -> None:
        """The old message interpolated nothing (adjacent string literals swallowed the
        f-prefix), so it logged a literal '{tag_m}'. Fixed during the Stage 3 migration — pin it
        so the diagnostic stays usable."""
        p = _payload()
        p["clan"]["members"][0]["name"] = ""

        with caplog.at_level("ERROR"):
            stats_from_war_payload(p, 2, strict=True, clan_tag="#AAA")

        assert "#M0" in caplog.text and "#AAA" in caplog.text
        assert "{tag_m}" not in caplog.text
