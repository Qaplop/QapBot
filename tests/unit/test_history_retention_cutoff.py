"""
Structural guard for WarHistoryDB._history_cutoff()'s rolling retention rule.

The 2026-09-01 redesign replaced a calendar-month cutoff (which shed ~38M rows on the 1st
of every month) with a rolling one:

    cutoff = min(today - CONFIG.history_retention_days, first day of previous month)

The `min()` floor is the whole reason a 60-day setting is safe. The long-standing,
documented contract is "hot always holds the current + the immediately preceding calendar
month", and the oldest row that contract must retain can be **61** days old — day 31 of a
31-day month following a 31-day month, e.g. 2026-08-31 where 2026-07-01 is 61 days back.
A plain `today - 60` migrates that day out and breaks the contract on 8 dates every 4
years. With the floor it cannot, on any date, for any retention value.

These tests exist because that reasoning is arithmetic a future edit can silently break
(e.g. "the floor looks redundant, drop it", or "make it today - 30 to save disk"). The
invariant is asserted over a full 4-year sweep including leap years and every
month-length combination, so the guard does not depend on which dates a reviewer thought
to try.
"""
# pyright: reportPrivateUsage=false
from __future__ import annotations

import dataclasses
import datetime
import os

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")

import qapbot.config as config_module  # noqa: E402
from qapbot.db_manager import WarHistoryDB  # noqa: E402


def _first_of_previous_month(d: datetime.date) -> datetime.date:
    """Independent reimplementation — a test that reuses the production helper would
    pass even if that helper were wrong."""
    first = d.replace(day=1)
    return (first - datetime.timedelta(days=1)).replace(day=1)


def _sweep(start=datetime.date(2024, 1, 1), days=366 * 4):
    for n in range(days):
        yield start + datetime.timedelta(days=n)


@pytest.fixture
def retention(monkeypatch):
    """Set CONFIG.history_retention_days for a test (BotConfig is frozen)."""
    def _set(days: int):
        monkeypatch.setattr(
            config_module, "CONFIG",
            dataclasses.replace(config_module.CONFIG, history_retention_days=days),
        )
    return _set


@pytest.mark.parametrize("days", [30, 45, 60, 62, 90])
def test_cutoff_never_violates_the_calendar_month_contract(retention, days):
    """The floor must hold for ANY retention value, not just the configured one.

    This is what makes history_retention_days freely tunable: shortening it can only
    change when rows move, never what hot is guaranteed to still contain.
    """
    retention(days)
    violations = []
    for d in _sweep():
        cutoff = datetime.date.fromisoformat(WarHistoryDB._history_cutoff(d)[0])
        if cutoff > _first_of_previous_month(d):
            violations.append((d.isoformat(), cutoff.isoformat()))
    assert not violations, (
        f"retention={days}d migrated data the hot DB must still hold, on "
        f"{len(violations)} date(s), first: {violations[:3]}"
    )


def test_floor_actually_binds_at_a_short_retention(retention):
    """Guards against the floor being silently unreachable (and so untested).

    At the configured default (75) the floor never binds — the rolling term is always
    the smaller of the two — so the invariant sweep above would pass on that term alone
    and prove nothing about the floor. Pinning a value where the floor DOES bind keeps
    the guard honest.
    """
    retention(60)
    bound = [
        d for d in _sweep()
        if datetime.date.fromisoformat(WarHistoryDB._history_cutoff(d)[0])
        != d - datetime.timedelta(days=60)
    ]
    assert bound, "the calendar floor never binds — the 61-day worst case is not covered"
    # 8 dates per 4 years is the known figure; assert the shape, not an exact count, so a
    # leap-year boundary shift does not fail the build for no reason.
    assert 1 <= len(bound) <= 20, f"unexpected number of floor-bound dates: {len(bound)}"


def test_plain_rolling_60_would_break_the_contract():
    """Pins the reason the floor exists.

    If this ever fails, the 61-day worst case has stopped being real and the floor could
    genuinely be dropped — but that must be a deliberate, evidenced decision, not an
    assumption.
    """
    naive_violations = [
        d for d in _sweep()
        if (d - datetime.timedelta(days=60)) > _first_of_previous_month(d)
    ]
    assert naive_violations, "expected `today - 60` to violate the contract on some date"
    assert datetime.date(2024, 1, 31) in naive_violations


def test_cutoff_month_matches_cutoff_date(retention):
    """CWL tables key on YYYY-MM and must advance with the date tables, not separately."""
    retention(60)
    for d in _sweep(days=400):
        cutoff_date, cutoff_month = WarHistoryDB._history_cutoff(d)
        assert cutoff_month == cutoff_date[:7]


def test_zero_or_negative_retention_is_clamped(retention):
    """A misconfigured 0 must not make the cutoff `today` and drain the whole hot DB."""
    retention(0)
    d = datetime.date(2026, 9, 15)
    cutoff = datetime.date.fromisoformat(WarHistoryDB._history_cutoff(d)[0])
    assert cutoff <= _first_of_previous_month(d)

# ---------------------------------------------------------------------------------------
# CWL load-phase guard
# ---------------------------------------------------------------------------------------
# history_retention_days does more than bound disk: it decides WHICH days of the month
# carry the heavy migration, because a row dated D migrates N days later. CWL runs days
# 1-10 and produces ~2x the normal war volume, so a badly chosen N stacks the two heaviest
# jobs the bot has onto the same nights. This is invisible from the constant itself, which
# is exactly why it needs a test.
# ---------------------------------------------------------------------------------------

CWL_DAYS = range(1, 11)  # CWL season runs days 1-10 of each month


def _cutoff_series(days_value: int, start=datetime.date(2026, 1, 1), n=730):
    """(date, cutoff) for each day, computed the way production does."""
    import qapbot.config as cfg
    original = cfg.CONFIG
    cfg.CONFIG = dataclasses.replace(original, history_retention_days=days_value)
    try:
        return [(start + datetime.timedelta(days=i),
                 datetime.date.fromisoformat(
                     WarHistoryDB._history_cutoff(start + datetime.timedelta(days=i))[0]))
                for i in range(n)]
    finally:
        cfg.CONFIG = original


def _cwl_migration_days(days_value: int):
    """Calendar days on which CWL-dated rows (day 1-10 of some month) get migrated."""
    series = _cutoff_series(days_value)
    out = []
    for (_, prev), (day, cur) in zip(series, series[1:]):
        advanced = (cur - prev).days
        for k in range(advanced):
            if (prev + datetime.timedelta(days=k)).day in CWL_DAYS:
                out.append(day)
                break
    return out


def _max_cutoff_jump(days_value: int) -> int:
    series = _cutoff_series(days_value)
    return max(((cur - prev).days for (_, prev), (_, cur) in zip(series, series[1:])),
               default=1)


def test_configured_retention_never_migrates_cwl_data_during_a_cwl_season():
    """The load-distribution property the configured default exists to provide.

    At 60 days, every CWL-data migration day landed inside a CWL window (227 of 235 over
    two years) — the heaviest migration nights stacked on the heaviest update-cycle
    nights. The band 71-78 is the only one that avoids it entirely.
    """
    import qapbot.config as cfg
    offenders = [d for d in _cwl_migration_days(cfg.CONFIG.history_retention_days)
                 if d.day in CWL_DAYS]
    assert not offenders, (
        "history_retention_days={} migrates CWL-dated rows during a CWL season on {} day(s) "
        "(first: {}). Only 71-78 avoids this — see the config comment before changing it."
        .format(cfg.CONFIG.history_retention_days, len(offenders), offenders[:5])
    )


def test_configured_retention_advances_the_cutoff_smoothly():
    """No freeze-then-jump. Below 61 the floor binds and the cutoff jumps up to 12 days at
    once — landing on the 1st of the month, which is the cliff the redesign removed."""
    import qapbot.config as cfg
    jump = _max_cutoff_jump(cfg.CONFIG.history_retention_days)
    assert jump <= 1, (
        "history_retention_days={} lets the cutoff jump {} days at once; the walk must "
        "advance at most 1 day/night. Values below 61 trip the calendar floor."
        .format(cfg.CONFIG.history_retention_days, jump)
    )


@pytest.mark.parametrize("days_value", [50, 60])
def test_known_bad_retention_values_still_reproduce_their_problem(days_value):
    """Pins WHY the default is what it is.

    If either of these ever stops failing its property, the analysis behind the chosen
    value has changed and the constant should be revisited deliberately — not silently
    inherited.
    """
    if days_value == 50:
        # Floor binds for ~1/3 of days, then the cutoff jumps ~12 days on the 1st.
        assert _max_cutoff_jump(50) > 1
    else:
        # Smooth, but every CWL-data migration day lands inside a CWL window.
        assert [d for d in _cwl_migration_days(60) if d.day in CWL_DAYS]
