"""Stage 1 of plans/tracker-0009-phase1-war-payload-retention.md — payload/coc-object parity.

The plan (§3) proposes returning the lightweight payload dict from Phase 1 instead of the
`coc.ClanWar` object, so ~2,300 heavy graphs stop being retained across the Phase-1/2 boundary.
§5.1 names the danger: **a mistyped payload key yields 0 stars / 0 attacks rather than an
exception**, and that flows into leaderboards and then into war history, where a code revert
cannot undo it.

So §6 says to write this first, and it is the test that actually justifies the change: run both
extraction paths over real temp war files and assert the produced `temp_war_stats` are equal.

HOW IT WORKS. Temp files store our payload, not the raw CoC API response, so a `coc.ClanWar`
has to be reconstructed from the payload to run the object path at all. That reconstruction is
the fixture, not the thing under test: the assertion is that
`stats_via_payload(p) == stats_via_coc(reconstruct(p))` for the same underlying war.

WHAT THIS CANNOT COVER — read before trusting a green run. `Defensive_Stars` is out of scope
here, and deliberately so:

  * `process_clan_war_data()` reads coc.py's `m.best_opponent_attack`, which resolves
    `_best_opponent_attacker` — and that is populated straight from **the CoC API's own**
    `bestOpponentAttack.attackerTag` field.
  * the payload's `bestOpponentAttack` is computed by our `find_best_opponent_attack()`, which
    scans every opponent attack *specifically because the API field misses late CWL attacks*.

They are deliberately different, and the plan's §2 table treats them as a pure key mapping.
Worse, the temp files do not retain the API's original value, so no corpus replay can compare
them — reconstruction would feed our computed value back in and the comparison would be
circular. **`Defensive_Stars` can only be validated by Stage 2 shadow mode on live data, where
the coc object still carries the API's field.** It is the single highest-risk field in the
migration; see §10 of the plan.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

coc = pytest.importorskip("coc")

CORPUS = Path("data/temp")
_DT = re.compile(r"datetime\.datetime\(([^)]*)\)")


def _api_timestamp(raw: Any) -> str:
    """Payload stores a `coc.Timestamp` *repr*; coc.py parses '%Y%m%dT%H%M%S.%fZ'."""
    m = _DT.search(str(raw or ""))
    if not m:
        return "20260101T000000.000Z"
    p = [int(x.strip()) for x in m.group(1).split(",")]
    p += [0] * (6 - len(p))
    return f"{p[0]:04d}{p[1]:02d}{p[2]:02d}T{p[3]:02d}{p[4]:02d}{p[5]:02d}.000Z"


def _api_side(side: Dict[str, Any]) -> Dict[str, Any]:
    members = []
    for m in side.get("members") or []:
        members.append({
            "tag": m.get("tag"),
            "name": m.get("name"),
            "townhallLevel": m.get("townhall") or 0,
            "mapPosition": m.get("map_position") or 0,
            "attacks": [
                {
                    "attackerTag": a.get("attackerTag"),
                    "defenderTag": a.get("defenderTag"),
                    "stars": a.get("stars") or 0,
                    "destructionPercentage": a.get("destruction") or 0,
                    "order": a.get("order") or 0,
                    "duration": a.get("duration") or 0,
                }
                for a in (m.get("attacks") or []) if a
            ],
            # NOTE: feeding OUR computed value back in. That is why Defensive_Stars is out of
            # scope for this harness — see the module docstring.
            "bestOpponentAttack": m.get("bestOpponentAttack") or {},
            "opponentAttacks": m.get("opponentAttacks") or 0,
        })
    badge = side.get("badge") or {}
    return {
        "tag": side.get("tag"), "name": side.get("name"),
        # Carried so build_war_payload()'s simple_badge() round-trips; without it the rebuilt
        # payload differs on `badge` alone and the comparison reports a false positive.
        "badgeUrls": {"small": badge.get("small") or "", "medium": badge.get("medium") or "",
                      "large": badge.get("large") or ""},
        "clanLevel": side.get("level") or 1,
        "attacks": side.get("attacks_used") or 0,
        "stars": side.get("stars") or 0,
        "destructionPercentage": side.get("destruction") or 0.0,
        "members": members,
    }


# §5.2's "state normalisation" risk, confirmed by this harness on the first run: the payload
# stores its OWN normalised state ('in_war'), while coc.py's WarState enum only accepts the raw
# API spelling ('inWar'). Reconstruction has to undo that. Note the same asymmetry is what
# process_clan_war_data()'s `state in ('preparation','in_war')` check depends on, so anything
# reading state from the payload must not assume the coc spelling — or the temp-stats block is
# silently skipped for every in_war clan, which is exactly §5.1's silent-zero failure mode.
_API_STATE = {
    "in_war": "inWar", "war_ended": "warEnded", "not_in_war": "notInWar",
    "in_matchmaking": "inMatchmaking", "enter_war": "enterWar", "matched": "matched",
    "preparation": "preparation",
}


def reconstruct_war(payload: Dict[str, Any], client: Any) -> Any:
    _raw_state = str(payload.get("state") or "in_war")
    api = {
        "state": _API_STATE.get(_raw_state, _raw_state),
        "teamSize": payload.get("team_size") or 0,
        "attacksPerMember": payload.get("attacks_per_member") or 2,
        "preparationStartTime": _api_timestamp(payload.get("preparation_start_time")),
        "startTime": _api_timestamp(payload.get("start_time")),
        "endTime": _api_timestamp(payload.get("end_time")),
        "clan": _api_side(payload.get("clan") or {}),
        "opponent": _api_side(payload.get("opponent") or {}),
    }
    return coc.ClanWar(data=api, client=client, clan_tag=api["clan"]["tag"])


# --- the two extraction paths -------------------------------------------------------------
# stats_via_coc mirrors process_clan_war_data()'s loop (QBhelperfunctions.py) exactly.
# stats_via_payload is the proposed replacement. Defensive_Stars is excluded from BOTH so the
# comparison stays honest about what it proves; see the module docstring.

_FIELDS = ("Player", "PlayerID", "TH_lvl", "Stars", "Attacks", "Max_Attacks", "Total_Dest_Pct")


def stats_via_coc(war: Any, attacks_per_member: int) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    my_clan = getattr(war, "clan", None)
    if not (my_clan and hasattr(my_clan, "members")):
        return out
    for m in my_clan.members:
        tag_m = getattr(m, "tag", "")
        name_m = getattr(m, "name", "")
        if not name_m or not tag_m:
            continue
        atk = list(getattr(m, "attacks", []) or [])
        out[tag_m] = {
            "Player": name_m,
            "PlayerID": tag_m,
            "TH_lvl": getattr(m, "town_hall", 0),
            "Stars": sum(getattr(a, "stars", 0) for a in atk),
            "Attacks": len(atk),
            "Max_Attacks": attacks_per_member,
            "Total_Dest_Pct": sum(float(getattr(a, "destruction", 0.0) or 0.0) for a in atk),
        }
    return out


def stats_via_payload(payload: Dict[str, Any], attacks_per_member: int) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for m in ((payload.get("clan") or {}).get("members") or []):
        tag_m = m.get("tag") or ""
        name_m = m.get("name") or ""
        if not name_m or not tag_m:
            continue
        atk = [a for a in (m.get("attacks") or []) if a]
        out[tag_m] = {
            "Player": name_m,
            "PlayerID": tag_m,
            "TH_lvl": m.get("townhall") or 0,
            "Stars": sum(a.get("stars") or 0 for a in atk),
            "Attacks": len(atk),
            "Max_Attacks": attacks_per_member,
            "Total_Dest_Pct": sum(float(a.get("destruction") or 0.0) for a in atk),
        }
    return out


def compare_one(payload: Dict[str, Any], client: Any, payload_reader: Any = None) -> List[str]:
    """Return a list of human-readable mismatches for one war payload.

    `payload_reader` overrides `stats_via_payload` — used only by the harness self-test, which
    has to inject a faulty READER (the realistic §5.1 failure) rather than corrupt the data.
    Corrupting the data proves nothing here: `reconstruct_war()` reads the same keys, so both
    sides degrade identically and agree at zero. That blind spot is inherent to reconstructing
    the object from the payload, and it is fine — the migration risk is a mistyped key in the
    new reader, and the data is fixed on disk.
    """
    apm = payload.get("attacks_per_member") or 2
    war = reconstruct_war(payload, client)
    a = stats_via_coc(war, apm)
    b = (payload_reader or stats_via_payload)(payload, apm)
    problems: List[str] = []
    if set(a) != set(b):
        problems.append(f"member sets differ: only-coc={sorted(set(a)-set(b))[:3]} only-payload={sorted(set(b)-set(a))[:3]}")
    for tag in sorted(set(a) & set(b)):
        for f in _FIELDS:
            if a[tag][f] != b[tag][f]:
                problems.append(f"{tag}.{f}: coc={a[tag][f]!r} payload={b[tag][f]!r}")
    return problems


def _corpus(limit: Optional[int] = None) -> List[Path]:
    if not CORPUS.exists():
        return []
    files: List[Path] = []
    for root, _dirs, names in os.walk(CORPUS):
        for n in names:
            if n.endswith(".json"):
                files.append(Path(root) / n)
                if limit and len(files) >= limit:
                    return files
    return files


@pytest.fixture(scope="module")
def client() -> Any:
    import asyncio
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    return coc.Client()


class TestSyntheticParity:
    """Runs everywhere, including CI where the corpus is absent."""

    @staticmethod
    def _payload(n: int = 3) -> Dict[str, Any]:
        def side(prefix: str, other: str) -> Dict[str, Any]:
            return {
                "tag": f"#{prefix}", "name": prefix, "level": 10, "attacks_used": n,
                "stars": 3, "destruction": 50.0,
                "members": [
                    {"tag": f"#{prefix}M{i}", "name": f"{prefix}m{i}", "townhall": 15,
                     "map_position": i + 1,
                     "attacks": [{"attackerTag": f"#{prefix}M{i}", "defenderTag": f"#{other}M{i}",
                                  "stars": (i % 4), "destruction": 25 * (i % 4),
                                  "order": i + 1, "duration": 60}],
                     "bestOpponentAttack": {"attackerTag": f"#{other}M{i}",
                                            "defenderTag": f"#{prefix}M{i}", "stars": 2,
                                            "destruction": 60, "order": i + 1, "duration": 60},
                     "opponentAttacks": 1}
                    for i in range(n)
                ],
            }
        return {
            "state": "in_war", "team_size": n, "attacks_per_member": 2,
            "preparation_start_time": "<Timestamp time=datetime.datetime(2026, 9, 3, 10, 0, 0) seconds_until=1>",
            "start_time": "<Timestamp time=datetime.datetime(2026, 9, 3, 11, 0, 0) seconds_until=1>",
            "end_time": "<Timestamp time=datetime.datetime(2026, 9, 4, 11, 0, 0) seconds_until=1>",
            "clan": side("A", "B"), "opponent": side("B", "A"),
        }

    def test_paths_agree_on_a_synthetic_war(self, client: Any) -> None:
        assert compare_one(self._payload(), client) == []

    def test_zero_attack_member_is_not_silently_dropped(self, client: Any) -> None:
        """§5.1's failure mode is a silent 0, so the no-attack case must be explicit."""
        p = self._payload()
        p["clan"]["members"][0]["attacks"] = []
        assert compare_one(p, client) == []
        assert stats_via_payload(p, 2)["#AM0"]["Stars"] == 0
        assert stats_via_payload(p, 2)["#AM0"]["Attacks"] == 0

    def test_harness_detects_a_reader_that_mistypes_a_key(self, client: Any) -> None:
        """A parity harness that cannot fail proves nothing. Self-test: inject a reader with
        exactly the §5.1 slip — a snake_case/camelCase mistype that yields 0 instead of raising —
        and confirm the comparison catches it.

        The first version of this test corrupted the *data* instead, and passed for the wrong
        reason: `reconstruct_war()` reads the same keys, so both sides went to 0 and agreed.
        Kept as a reminder that a green parity run only covers reader errors.
        """
        def faulty(payload: Dict[str, Any], apm: int) -> Dict[str, Dict[str, Any]]:
            out = stats_via_payload(payload, apm)
            for tag, row in out.items():
                member = next(m for m in payload["clan"]["members"] if m["tag"] == tag)
                row["TH_lvl"] = member.get("town_hall") or 0   # wrong key: silently 0
            return out

        problems = compare_one(self._payload(), client, payload_reader=faulty)

        assert problems and any("TH_lvl" in x for x in problems), (
            f"harness missed a mistyped reader key — it would miss a real one too: {problems}"
        )

    def test_a_silent_zero_is_caught_for_every_numeric_field(self, client: Any) -> None:
        """§5.1's danger is a 0, not an exception — so prove each numeric field is compared."""
        for field in ("TH_lvl", "Stars", "Attacks", "Total_Dest_Pct", "Max_Attacks"):
            def faulty(payload: Dict[str, Any], apm: int, _f: str = field) -> Dict[str, Dict[str, Any]]:
                out = stats_via_payload(payload, apm)
                for row in out.values():
                    row[_f] = 0
                return out

            problems = compare_one(self._payload(), client, payload_reader=faulty)
            assert any(field in p for p in problems), f"a silent 0 in {field} would go unnoticed"


@pytest.mark.integration
class TestRealCorpusParity:
    """The evidence that gates Stage 2. Skipped where the DEV corpus is absent."""

    def test_parity_over_real_temp_files(self, client: Any) -> None:
        limit = int(os.getenv("PARITY_LIMIT", "4000"))
        files = _corpus(limit)
        if not files:
            pytest.skip(f"no temp-war corpus at {CORPUS} (DEV-only)")

        checked = skipped = 0
        failures: List[Tuple[str, List[str]]] = []
        for f in files:
            try:
                payload = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                skipped += 1
                continue
            if not (payload.get("clan") or {}).get("members"):
                skipped += 1
                continue
            try:
                problems = compare_one(payload, client)
            except Exception as exc:
                failures.append((f.name, [f"raised {type(exc).__name__}: {exc}"]))
                continue
            checked += 1
            if problems:
                failures.append((f.name, problems[:4]))

        assert checked > 0, f"corpus present but nothing comparable (skipped={skipped})"
        assert not failures, (
            f"{len(failures)} of {checked} real wars disagree between the coc-object and payload "
            f"paths. §5.1: a mismatch here is a silent-zero bug in waiting, not noise.\n"
            + "\n".join(f"  {n}: {p}" for n, p in failures[:10])
        )


@pytest.mark.integration
class TestBuildWarPayloadRoundTrip:
    """`build_war_payload()` (§3 Step 1) must reproduce the payload a real temp file holds.

    Behaviour-neutrality of the extraction itself was proved textually — the 160-line body is
    character-identical to the pre-extraction block, only re-indented. This adds the behavioural
    half: rebuild each real war's payload from a reconstructed `coc.ClanWar` and compare.

    `previousBestOpponentAttack` is excluded, and the reason is worth knowing before anyone
    "fixes" it. coc.py derives it as `max(defenses, key=lambda a: a != self.best_opponent_attack
    and ...)` — i.e. it EXCLUDES whatever `best_opponent_attack` returns. Reconstruction has to
    feed our computed `bestOpponentAttack` in as the API's field (the API's own value is not
    retained on disk), so a different attack gets excluded and a different "previous best" comes
    out. 76 of 54,192 wars differ on that field alone for this reason — a fixture limitation, not
    a defect. It is also consumed only by `qapbot/scripts/recover_cwl_war.py`, never by the
    temp-stats path or leaderboards.
    """

    _VOLATILE = re.compile(r" seconds_until=-?\d+")

    @classmethod
    def _norm(cls, o: Any) -> Any:
        """Strip coc.Timestamp's live countdown — Pitfall 55: it changes between writes."""
        if isinstance(o, str):
            return cls._VOLATILE.sub("", o)
        if isinstance(o, dict):
            return {k: cls._norm(v) for k, v in o.items() if k != "previousBestOpponentAttack"}
        if isinstance(o, list):
            return [cls._norm(v) for v in o]
        return o

    def test_rebuilds_real_payloads(self, client: Any) -> None:
        from qapbot.cache_manager import build_war_payload

        files = _corpus(int(os.getenv("PARITY_LIMIT", "2000")))
        if not files:
            pytest.skip(f"no temp-war corpus at {CORPUS} (DEV-only)")

        checked = 0
        bad: List[str] = []
        for f in files:
            try:
                orig = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not (orig.get("clan") or {}).get("members"):
                continue
            war = reconstruct_war(orig, client)
            rebuilt = build_war_payload(war, war.clan, war.opponent)
            checked += 1
            for key in ("clan", "opponent", "attacks", "team_size", "attacks_per_member"):
                if self._norm(rebuilt.get(key)) != self._norm(orig.get(key)):
                    bad.append(f"{f.name}: {key}")
                    break

        assert checked > 0
        assert not bad, f"{len(bad)} of {checked} rebuilt payloads differ: {bad[:5]}"
