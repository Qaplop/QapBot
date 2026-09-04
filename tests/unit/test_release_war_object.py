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
