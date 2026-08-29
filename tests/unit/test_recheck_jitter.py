"""Per-clan re-check phase offset (tracker #0009, 2026-08-29).

The flat INACTIVE_CLAN_UPDATE_INTERVAL_HOURS=22 threshold made the polling schedule
self-clumping: every clan polled in one cycle came due again in one cycle 22h later. On PROD
that produced 3000-4200-clan bursts recurring at exactly 22h and drifting 2h earlier each day,
and each burst cost +541 to +700 MB of RSS in a single cycle because every clan polled in a
cycle is held in memory simultaneously. These tests lock in the properties that actually make
the spread work — determinism above all, since a per-cycle random draw would look like jitter
while dispersing nothing.
"""
from __future__ import annotations

import pytest

from QapBot import RECHECK_JITTER_SECONDS, compute_recheck_offset_seconds


TAGS = [f"#C{i:06d}" for i in range(5000)]


class TestComputeRecheckOffsetSeconds:
    def test_offset_is_within_the_jitter_window(self):
        for tag in TAGS[:500]:
            offset = compute_recheck_offset_seconds(tag)
            assert 0 <= offset < RECHECK_JITTER_SECONDS

    def test_never_negative_so_a_clan_is_never_polled_more_often_than_before(self):
        """The offset is only ever ADDED to the 22h interval. A negative offset would pull
        polls forward and increase total API volume, which is the opposite of the intent."""
        assert all(compute_recheck_offset_seconds(t) >= 0 for t in TAGS[:1000])

    def test_offset_is_deterministic_across_calls(self):
        """THE critical property. A fresh random draw per cycle would jitter each clan's due
        time around the same clump centre without ever dispersing the clump; only a stable
        per-clan phase makes a wave spread out once and stay spread."""
        first = [compute_recheck_offset_seconds(t) for t in TAGS[:200]]
        second = [compute_recheck_offset_seconds(t) for t in TAGS[:200]]
        assert first == second

    def test_offset_is_stable_across_processes(self):
        """crc32, not hash() — str hashing is salted per process (PYTHONHASHSEED), so a
        hash()-based offset would silently reshuffle every clan's phase on each restart."""
        import subprocess
        import sys

        code = (
            "import sys; sys.path.insert(0, r'.');"
            "from QapBot import compute_recheck_offset_seconds as f;"
            "print(','.join(str(f(f'#C{i:06d}')) for i in range(20)))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=180
        )
        assert out.returncode == 0, out.stderr
        expected = ",".join(str(compute_recheck_offset_seconds(t)) for t in TAGS[:20])
        assert out.stdout.strip().endswith(expected)

    def test_offsets_are_spread_across_the_whole_window(self):
        """A clumped population must actually disperse: with ~5000 clans every 10-minute
        bucket of the 2h window should receive a non-trivial share, rather than the whole
        wave landing in one or two cycles."""
        bucket_seconds = 600
        buckets: dict[int, int] = {}
        for tag in TAGS:
            buckets[compute_recheck_offset_seconds(tag) // bucket_seconds] = (
                buckets.get(compute_recheck_offset_seconds(tag) // bucket_seconds, 0) + 1
            )
        expected_buckets = RECHECK_JITTER_SECONDS // bucket_seconds
        assert len(buckets) == expected_buckets, "some buckets got no clans at all"
        ideal = len(TAGS) / expected_buckets
        assert all(0.5 * ideal < n < 1.5 * ideal for n in buckets.values()), buckets

    def test_distinct_tags_do_not_collapse_onto_one_offset(self):
        offsets = {compute_recheck_offset_seconds(t) for t in TAGS}
        assert len(offsets) > 1000

    @pytest.mark.parametrize("jitter", [0, -1])
    def test_non_positive_jitter_disables_the_spread(self, jitter: int):
        """Escape hatch: setting the window to 0 restores the exact previous flat-threshold
        behaviour without needing to unpick the call site."""
        assert all(compute_recheck_offset_seconds(t, jitter) == 0 for t in TAGS[:100])

    def test_window_is_well_under_the_recheck_interval(self):
        """The spread must stay small relative to the 22h interval it perturbs — a window
        approaching the interval itself would materially delay coverage, not just smooth it."""
        assert 0 < RECHECK_JITTER_SECONDS <= 4 * 3600
