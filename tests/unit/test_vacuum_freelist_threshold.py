"""
Guard for nightly_db_maintenance()'s VACUUM trigger.

The trigger was a flat `freelist_count > 500` pages (8 MB at a 16 KB page size). That was
correct while the hot->history migration deleted a month of rows once a month: the free list
sat near zero the rest of the time, so it only fired when there was genuinely something to
reclaim.

The 2026-09-01 rolling migration broke that assumption. Deleting ~1.2M rows every night frees
~1.1 GB of pages every night, so an 8 MB trigger fires unconditionally — an occasional VACUUM
becomes a nightly one, costing ~7.5 min of EXCLUSIVE lock and hard Discord block plus a full
rewrite of a 24-40 GB file on NAS-attached SSD. And reclaiming nothing: deletes and inserts
balance in steady state and SQLite reuses free-list pages, so the free list is churn, not waste.

This file pins BOTH directions, because each has a real failure mode:

  * too low  -> VACUUM every night (the regression above)
  * too high -> VACUUM never runs, the free list grows unbounded, and nobody notices because
                a VACUUM that stops happening is completely silent

Measured on the real 2026-09-01 PROD-copy DB: 24.0 GB file, 1.64 GB free list (6.8%) after a
17-day catch-up walk; a single steady-state night frees roughly 1.1 GB.
"""
# pyright: reportPrivateUsage=false
from __future__ import annotations

import dataclasses
import os

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")

import qapbot.config as config_module  # noqa: E402

PAGE_SIZE = 16384
GB = 2 ** 30


def _threshold_pages(page_count: int, cfg=None) -> int:
    """The production expression, kept in one place so the tests below read as intent.

    Deliberately re-stated rather than imported: it lives inline inside
    nightly_db_maintenance()'s sync `_run()` closure, which cannot be called without a real
    DB and an EXCLUSIVE lock. test_production_expression_matches (below) is what keeps this
    copy honest.
    """
    cfg = cfg or config_module.CONFIG
    return max(cfg.vacuum_min_freelist_pages, int(page_count * cfg.vacuum_freelist_fraction))


def _pages(gigabytes: float) -> int:
    return int(gigabytes * GB / PAGE_SIZE)


PROD_FILE_PAGES = _pages(24.0)


def test_routine_nightly_churn_does_not_trigger_vacuum():
    """~1.1 GB freed per night by the rolling migration must NOT fire a 24 GB VACUUM."""
    nightly_churn = _pages(1.1)
    assert nightly_churn <= _threshold_pages(PROD_FILE_PAGES), (
        "a single night's free-list churn crosses the VACUUM threshold — nightly maintenance "
        "would VACUUM every night, which is the 2026-09-01 regression this guards"
    )


def test_two_nights_of_churn_still_does_not_trigger():
    """Headroom check: the free list can lag a night behind reuse without tripping."""
    assert _pages(2.2) <= _threshold_pages(PROD_FILE_PAGES)


def test_a_genuine_one_off_shrink_does_trigger_vacuum():
    """The threshold must not be so high that VACUUM effectively never runs.

    A retention change or a large catch-up leaves multiple GB of genuinely reclaimable
    space; that is exactly what VACUUM is for.
    """
    assert _pages(6.0) > _threshold_pages(PROD_FILE_PAGES), (
        "6 GB of free list does not trigger VACUUM — the threshold is too high and the file "
        "will grow without ever being compacted"
    )


def test_threshold_stays_a_sane_fraction_of_the_file():
    """Pins the configured value into a defensible band.

    Below ~5% daily churn starts tripping it; above ~30% the file carries an absurd amount of
    dead space before anything reclaims it.
    """
    fraction = config_module.CONFIG.vacuum_freelist_fraction
    assert 0.05 <= fraction <= 0.30, f"vacuum_freelist_fraction={fraction} is outside the sane band"


def test_small_databases_fall_back_to_the_absolute_floor():
    """On a fresh/dev DB the proportional term is tiny, so the floor must dominate —
    otherwise a 1 MB test DB would VACUUM on every single maintenance run."""
    tiny = _pages(0.001)
    assert _threshold_pages(tiny) == config_module.CONFIG.vacuum_min_freelist_pages


@pytest.mark.parametrize("fraction,should_fire", [(0.0, True), (0.15, False)])
def test_fraction_zero_restores_the_old_always_vacuum_behaviour(monkeypatch, fraction, should_fire):
    """Documented escape hatch: VACUUM_FREELIST_FRACTION=0 falls back to the floor alone."""
    cfg = dataclasses.replace(config_module.CONFIG, vacuum_freelist_fraction=fraction)
    nightly_churn = _pages(1.1)
    assert (nightly_churn > _threshold_pages(PROD_FILE_PAGES, cfg)) is should_fire


def test_production_expression_matches_this_files_copy():
    """Structural guard against the two expressions drifting apart.

    _threshold_pages() above restates logic that lives inline in db_manager's
    nightly_db_maintenance(); if that source line is edited, this fails and forces the copy
    to be updated with it.
    """
    import inspect

    from qapbot.db_manager import WarHistoryDB

    src = inspect.getsource(WarHistoryDB.nightly_db_maintenance)
    assert "_VAC_CONFIG.vacuum_min_freelist_pages," in src
    assert "int(_page_count_early * _VAC_CONFIG.vacuum_freelist_fraction)," in src
    assert "_freelist_early > _vacuum_threshold or _page_size_migration" in src
