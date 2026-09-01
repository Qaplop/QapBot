"""
Tests for qapbot.coc_health.apply_coc_library_patches()'s clanCapital shim.

Background (2026-08-31/09-01 PROD incident): coc.py 4.0.0's Clan._from_data
guards on `clanCapital` being truthy but then indexes `["districts"]`
unconditionally. The CoC API started returning a non-empty `clanCapital` object
with no `districts` key, so building a Clan raised `KeyError: 'districts'` — and
because a generator expression evaluates its outermost iterable eagerly, the
failure happened during construction and killed the *whole* clan fetch, not just
the capital part. 438 clans stopped being war-tracked; 42,871 fetches failed in
one day, each retried 3x with 1s+2s backoff.

Clan Capital data is not persisted anywhere in QapBot, so normalising the missing
key to an empty district list is lossless for this bot.
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportMissingParameterType=false, reportUnknownVariableType=false, reportAttributeAccessIssue=false
from __future__ import annotations

import coc
import pytest

from qapbot.coc_health import apply_coc_library_patches


def _clan_payload(clan_capital):
    """Minimal /clans/{tag} payload — only what Clan._from_data actually reads."""
    return {
        "tag": "#2ABC123",
        "name": "Test Clan",
        "clanCapital": clan_capital,
        "memberList": [],
        "labels": [],
    }


@pytest.fixture(autouse=True)
def _patched():
    apply_coc_library_patches()


def test_clan_capital_without_districts_no_longer_raises():
    """The exact PROD payload shape: clanCapital present, districts absent."""
    clan = coc.Clan(data=_clan_payload({"capitalHallLevel": 1}), client=None)

    assert clan.capital_districts == []


def test_clan_capital_with_null_districts_no_longer_raises():
    """Defensive: an explicit null is as unusable as a missing key."""
    clan = coc.Clan(data=_clan_payload({"capitalHallLevel": 1, "districts": None}), client=None)

    assert clan.capital_districts == []


def test_normal_payload_still_parses_districts():
    """Regression guard: the shim must not swallow real district data."""
    payload = _clan_payload({
        "capitalHallLevel": 8,
        "districts": [{"id": 70000000, "name": "Capital Peak", "districtHallLevel": 8}],
    })

    clan = coc.Clan(data=payload, client=None)

    assert [d.name for d in clan.capital_districts] == ["Capital Peak"]


def test_absent_clan_capital_still_yields_empty_list():
    """The library's own else-branch — untouched by the shim."""
    clan = coc.Clan(data=_clan_payload(None), client=None)

    assert clan.capital_districts == []


def test_apply_is_idempotent():
    """startup_login() doubles as coc_retry()'s reconnect callback, so this runs
    many times per process — it must not wrap the wrapper each time."""
    apply_coc_library_patches()
    first = coc.Clan._from_data
    apply_coc_library_patches()

    assert coc.Clan._from_data is first
