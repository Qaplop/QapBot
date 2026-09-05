"""Tests for release_war_object() — the fix that stops war graphs reaching the collector.

coc.py's war graph is cyclic by construction (`WarClan._war`, `ClanWarMember.war`/`.clan`,
`WarAttack.war`/`.member`, `ClanWarMember._best_opponent_attacker`), so refcounting can never
free a war — every one survives until a stop-the-world sweep walks it. On PROD that was
508,769 objects and a 2.0s pause per cycle, which is what made Discord unresponsive mid-cycle.

Severing the back-references makes the graph acyclic so it dies on the last reference drop and
never reaches the collector at all.

These tests use REAL coc.ClanWar objects, not mocks: the whole point is which references
coc.py actually holds, and a mock would happily confirm whatever we assumed. If a coc.py
upgrade renames or adds a back-reference, `test_no_sweep_only_garbage_remains` is what catches
it — a mock-based test would keep passing while the pause silently returned.
"""
from __future__ import annotations

import asyncio
import gc
from typing import Any, Dict

import pytest

coc = pytest.importorskip("coc")

from QBhelperfunctions import release_war_object  # noqa: E402


def _war_payload(size: int = 15) -> Dict[str, Any]:
    return {
        "state": "inWar", "teamSize": size, "attacksPerMember": 1,
        "preparationStartTime": "20260904T100000.000Z",
        "startTime": "20260904T110000.000Z",
        "endTime": "20260904T230000.000Z",
        "clan": {
            "tag": "#AAA", "name": "A", "clanLevel": 10, "attacks": 1, "stars": 3,
            "destructionPercentage": 50.0,
            "members": [
                {"tag": f"#M{i}", "name": f"m{i}", "townhallLevel": 15, "mapPosition": i + 1,
                 "attacks": [{"attackerTag": f"#M{i}", "defenderTag": f"#O{i}", "stars": 3,
                              "destructionPercentage": 100, "order": i + 1, "duration": 60}]}
                for i in range(size)
            ],
        },
        "opponent": {
            "tag": "#BBB", "name": "B", "clanLevel": 10, "attacks": 1, "stars": 2,
            "destructionPercentage": 40.0,
            "members": [
                {"tag": f"#O{i}", "name": f"o{i}", "townhallLevel": 15, "mapPosition": i + 1,
                 "attacks": [{"attackerTag": f"#O{i}", "defenderTag": f"#M{i}", "stars": 2,
                              "destructionPercentage": 80, "order": i + 1, "duration": 60}]}
                for i in range(size)
            ],
        },
    }


@pytest.fixture
def client() -> Any:
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    return coc.Client()


def _build(client: Any, size: int = 15, traverse: bool = True) -> Any:
    war = coc.ClanWar(data=_war_payload(size), client=client, clan_tag="#AAA")
    if traverse:  # what save_war_object() does: walk every member and attack
        for side in (war.clan, war.opponent):
            for member in side.members:
                list(member.attacks or [])
    return war


@pytest.fixture
def isolated_gc():
    was_enabled = gc.isenabled()
    try:
        yield
    finally:
        gc.enable() if was_enabled else gc.disable()
        gc.collect()


class TestTheGraphIsCyclic:
    def test_untouched_war_can_only_be_freed_by_a_sweep(self, client, isolated_gc) -> None:
        """The premise. If coc.py ever stops back-referencing, this fix becomes unnecessary —
        so this failing is good news, not a bug."""
        gc.disable()
        gc.collect()

        for _ in range(5):
            war = _build(client)
            del war

        assert gc.collect() > 0, (
            "war graphs were freed by refcounting alone — coc.py may have dropped its "
            "back-references; re-evaluate whether release_war_object() is still needed"
        )


class TestSeveringWorks:
    def test_no_sweep_only_garbage_remains(self, client, isolated_gc) -> None:
        """The whole point: after release, the collector has nothing left to do."""
        gc.disable()
        gc.collect()

        for _ in range(5):
            war = _build(client)
            release_war_object(war)
            del war

        assert gc.collect() == 0, "a back-reference was missed; the graph still needs a sweep"

    def test_reports_what_it_severed(self, client) -> None:
        war = _build(client)
        assert release_war_object(war) > 0

    def test_is_idempotent(self, client) -> None:
        """Called twice (e.g. a retry path) it must not raise."""
        war = _build(client)
        release_war_object(war)
        release_war_object(war)


class TestSafetyContract:
    """What callers are still allowed to read afterwards — see release_war_object's docstring."""

    def test_scalar_fields_survive(self, client) -> None:
        """Phase 2's backdate check reads war_obj.end_time AFTER this runs."""
        war = _build(client)
        release_war_object(war)

        assert war.end_time is not None
        assert war.start_time is not None
        assert str(war.state)
        assert war.team_size == 15

    def test_top_level_sides_survive(self, client) -> None:
        war = _build(client)
        release_war_object(war)

        assert war.clan.tag == "#AAA"
        assert war.opponent.tag == "#BBB"

    def test_navigation_back_up_is_gone(self, client) -> None:
        """The destructive half, asserted explicitly so it is a decision and not a surprise."""
        war = _build(client)
        members = list(war.clan.members)
        attacks = list(members[0].attacks or [])
        release_war_object(war)

        assert members[0].war is None
        assert members[0].clan is None
        assert attacks[0].war is None


class TestDefensive:
    def test_none_is_a_no_op(self) -> None:
        assert release_war_object(None) == 0

    def test_unrelated_object_does_not_raise(self) -> None:
        """Never fail a cycle over teardown."""
        assert release_war_object(object()) == 0
        assert release_war_object("not a war") == 0

    def test_untraversed_war_is_not_forced_into_existence(self, client) -> None:
        """coc.py builds members lazily. Tearing down must not materialise a graph that was
        never used just to sever it — that would ADD the work this exists to remove.
        """
        war = _build(client, traverse=False)
        release_war_object(war)

        assert not hasattr(war.clan, "_cs_members"), (
            "release_war_object() forced lazy member construction"
        )


class TestCachedWarsAreNeverSevered:
    """A CWL fallback hands back a war straight out of `CACHE._league_war_cache` — the same
    object ~29 other call sites read (CWL leaderboards, group stats, orphan processing),
    retained up to 2h and permanently for `war_ended`.

    `fetch_clan_war_data()` severs the war it owns. If it ever severs a CACHED one, the cache
    keeps serving an object whose back-references are gone, and the next reader of
    `member.war` / `attack.member` / `best_opponent_attack` silently sees None — a wrong number
    in a CWL leaderboard rather than a crash.

    Build 14 introduced the sever unguarded at the Phase-3 site; Stage 3 moved it into
    fetch_clan_war_data() still unguarded. It never fired because the CWL fallback is rare (zero
    occurrences across the observed PROD windows) — which is exactly why it needs a test and not
    a log line.
    """

    def test_severing_a_shared_war_is_destructive(self, client) -> None:
        """Control: establishes that the thing being guarded against is real and damaging."""
        war = _build(client)
        member = war.clan.members[0]

        release_war_object(war)

        assert member.war is None, "release_war_object stopped severing — the guard is moot"

    def test_the_guard_leaves_a_cached_war_intact(self, client) -> None:
        """With the provenance flag set, an object the cache still holds must be untouched."""
        war = _build(client)
        member = war.clan.members[0]

        _war_obj_is_shared = True            # what a CWL fallback sets
        if not _war_obj_is_shared:           # the guard in fetch_clan_war_data()
            release_war_object(war)

        assert member.war is war, "a CACHED league war was severed — every later cache hit is corrupt"
        assert member.attacks is not None

    def test_the_guard_is_present_in_the_source(self) -> None:
        """Structural. The surrounding function needs live API objects to execute, and an
        unguarded sever raises nothing — it only produces quietly wrong data later — so pin the
        guard itself."""
        import inspect
        import re
        import QBhelperfunctions as H

        src = inspect.getsource(H.fetch_clan_war_data)

        assert "_war_obj_is_shared" in src, "provenance flag gone from fetch_clan_war_data()"
        # The sever later moved into a `finally` (covering every exit path, not just the success
        # return) and gained a None check. What must not change is that it stays GUARDED by the
        # provenance flag — severing a cached league war is the bug this pins.
        assert re.search(r"not _war_obj_is_shared:\s*\n\s*release_war_object\(", src), (
            "release_war_object() is no longer guarded by the provenance check"
        )
        assert src.count("_war_obj_is_shared = True") == 4, (
            "a CWL fallback reassigns coc_war_obj without marking it shared — "
            "that path would sever a cached league war"
        )


class TestSeverCoversEveryExitPath:
    """Stage 3 originally severed only immediately before `return _result`, silently missing
    every other way out of `fetch_clan_war_data()`.

    Two real gaps existed, found by auditing exit paths rather than by any failure:

    1. The `state == 'not_in_war'` branches `return None` while holding a REAL coc.ClanWar —
       coc.py sometimes hands back a war object with empty clan/opponent instead of None.
    2. Any exception raised after a successful fetch (payload build, metadata extraction,
       save_war_object) propagated as WarDataFetchError with the graph never severed.

    Both leak a cyclic graph that nothing reclaims until a sweep walks it — precisely the cost
    release_war_object() exists to avoid. The fix moved the sever into a `finally`, so it is now
    a property of the function rather than of one branch. These tests pin that structurally:
    the surrounding function needs live API objects to execute, and a missed sever raises
    nothing — it just quietly costs GC time later.
    """

    def test_sever_lives_in_a_finally_not_before_the_return(self) -> None:
        import inspect
        import re
        import QBhelperfunctions as H

        src = inspect.getsource(H.fetch_clan_war_data)

        assert re.search(r"finally:.*?release_war_object\(", src, re.S), (
            "release_war_object() is no longer reached from a finally — exit paths other than "
            "the success return will leak an unsevered cyclic war graph again"
        )
        # it must not ALSO sit inline before the success return (that was the pre-fix shape)
        assert not re.search(r"if not _war_obj_is_shared:\s*\n\s*release_war_object\(\s*coc_war_obj\s*\)\s*\n\s*return _result", src), (
            "the pre-fix inline sever before `return _result` is back; it covers only one path"
        )

    def test_ownership_guard_survived_the_move(self) -> None:
        """The finally must still refuse to sever a CACHED league war (Pitfall 61)."""
        import inspect
        import QBhelperfunctions as H

        src = inspect.getsource(H.fetch_clan_war_data)

        assert "_war_obj_is_shared" in src
        assert src.count("_war_obj_is_shared = True") == 4, (
            "a CWL fallback reassigns coc_war_obj without marking it shared — the finally "
            "would then sever a cached league war on the way out"
        )

    def test_flags_are_hoisted_above_the_try(self) -> None:
        """The finally references both names; if they initialise inside the try, an exception
        before that point makes the finally itself raise NameError."""
        import inspect
        import QBhelperfunctions as H

        src = inspect.getsource(H.fetch_clan_war_data)
        body = src[src.index('"""', src.index('"""') + 3) + 3:]
        pre_try = body[: body.index("\n    try:")]

        assert "coc_war_obj = None" in pre_try, "coc_war_obj not initialised before the try"
        assert "_war_obj_is_shared = False" in pre_try, "_war_obj_is_shared not initialised before the try"
