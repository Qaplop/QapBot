"""Stage 2 (shadow mode) of plans/tracker-0009-phase1-war-payload-retention.md.

Phase 1 now carries `war_payload` alongside `war_obj`. `process_clan_war_data()` computes the
temp-war stats from the coc object (still authoritative) AND from the payload, compares them,
and logs `[PAYLOAD-PARITY]` on divergence. Corruption is impossible while the payload is
non-authoritative, which is the point: §5.1's failure mode is a silent 0 reaching war history,
and a code revert cannot undo that.

These tests cover the two halves that must not break:

1. `stats_from_war_payload()` reads the right keys. The payload mixes conventions (`townhall`,
   `map_position` beside `bestOpponentAttack`, `opponentAttacks`), and §3 Step 3 warns that this
   is exactly where a silent-zero bug hides.
2. `_compare_shadow_stats()` actually detects divergence. A comparator that cannot fail would
   make shadow mode worthless while looking green.

WHY OFFLINE PARITY CANNOT SETTLE `Defensive_Stars`: the coc path reads
`m.best_opponent_attack`, which resolves the API's own `bestOpponentAttack.attackerTag`; the
payload's field is computed by `find_best_opponent_attack()` scanning every opponent attack, to
catch late CWL attacks the API field misses. Replaying temp files feeds our computed value back
in as the API's, so the two agree trivially — measured: 0 diffs over 6,000 replayed wars, which
proves nothing. Only live PROD data has both values. That is what Stage 2 is for.
"""
from __future__ import annotations

from typing import Any, Dict

import pytest

from QBhelperfunctions import _compare_shadow_stats, stats_from_war_payload


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


def _coc_equivalent(payload: Dict[str, Any], apm: int) -> Dict[str, Dict[str, Any]]:
    """What the authoritative coc-object path would produce for the same war."""
    return {
        m["tag"]: {
            "Player": m["name"], "PlayerID": m["tag"], "TH_lvl": m["townhall"],
            "Stars": sum(a["stars"] for a in m["attacks"]),
            "Attacks": len(m["attacks"]), "Missed_Attacks": 0, "Max_Attacks": apm,
            "Defensive_Stars": m["bestOpponentAttack"]["stars"],
            "Total_Dest_Pct": sum(float(a["destruction"]) for a in m["attacks"]),
        }
        for m in payload["clan"]["members"]
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


class TestShadowComparator:
    def test_identical_stats_report_nothing(self) -> None:
        p = _payload()
        assert _compare_shadow_stats("#AAA", _coc_equivalent(p, 2), p, 2) == []

    @pytest.mark.parametrize("field", [
        "Player", "TH_lvl", "Stars", "Attacks", "Max_Attacks", "Defensive_Stars", "Total_Dest_Pct",
    ])
    def test_a_divergence_in_any_field_is_detected(self, field: str) -> None:
        """A comparator that cannot fail makes shadow mode worthless while looking green."""
        p = _payload()
        auth = _coc_equivalent(p, 2)
        auth["#M0"][field] = "sentinel" if field == "Player" else 999

        diffs = _compare_shadow_stats("#AAA", auth, p, 2)

        assert any(field in d for d in diffs), f"divergence in {field} went unreported: {diffs}"

    def test_a_silent_zero_is_detected(self) -> None:
        """The specific §5.1 shape: the payload path yields 0 where the coc path had a value."""
        p = _payload()
        auth = _coc_equivalent(p, 2)
        for m in p["clan"]["members"]:
            m["bestOpponentAttack"] = {"stars": 0}

        diffs = _compare_shadow_stats("#AAA", auth, p, 2)

        assert diffs and all("Defensive_Stars" in d for d in diffs)

    def test_missing_and_extra_members_are_reported(self) -> None:
        p = _payload()
        auth = _coc_equivalent(p, 2)
        auth["#GHOST"] = dict(auth["#M0"])
        p["clan"]["members"] = p["clan"]["members"][:2]

        diffs = _compare_shadow_stats("#AAA", auth, p, 2)

        assert any("missing" in d for d in diffs)

    def test_float_noise_is_not_reported_as_divergence(self) -> None:
        """Destruction sums are floats; 1e-9 drift must not spam a PROD warning every cycle."""
        p = _payload()
        auth = _coc_equivalent(p, 2)
        auth["#M0"]["Total_Dest_Pct"] += 1e-9

        assert _compare_shadow_stats("#AAA", auth, p, 2) == []
