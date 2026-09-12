"""#0094 (resolved 2026-09-12): the poll loop must not store what it can never re-read.

fetch_clan_war_data() streams ~55K distinct clans/day through CoCClanCache. Its own
last_checked_via_api gate (12h; 30min for role clans) is far longer than the cache's 600s
TTL, so it structurally cannot hit its own writes. PROD measured exactly that:

    protected: 17/93    hit_rate=18.3%
    other:      0/11193 hit_rate= 0.0%

so the write is now gated on protected membership. These tests pin the gate itself. The
earlier attempt at this change was reverted because it was made on an assumption in the
same commit that added the counters meant to test it — hence the emphasis here on pinning
BOTH directions, not just the memory-saving one.
"""
# pyright: reportPrivateUsage=false, reportMissingParameterType=false, reportUnknownMemberType=false
from __future__ import annotations

import inspect

import QBhelperfunctions


class TestStoreResultGateIsWiredToProtectedMembership:
    """Source-level: the real call path needs a live coc client, a populated CACHE and an
    awaited API round-trip. What must not regress is cheap to pin directly — that the call
    passes store_result at all, and derives it from protected_tags rather than a constant."""

    def _src(self) -> str:
        return inspect.getsource(QBhelperfunctions.fetch_clan_war_data)

    def test_get_clan_is_called_with_an_explicit_store_result(self):
        src = self._src()
        assert "get_clan(clan_tag, store_result=" in src, (
            "the poll-loop get_clan() call lost its store_result argument — it would resume "
            "storing every streamed clan, which #0094 measured at 0% hit rate over 11,193 "
            "requests"
        )

    def test_store_result_is_derived_from_protected_tags_not_hardcoded(self):
        src = self._src()
        assert "protected_tags" in src, (
            "store_result is no longer derived from protected membership"
        )
        # The failure mode that matters: someone 'simplifies' it to a constant.
        assert "store_result=False)" not in src, (
            "store_result hardcoded False would stop caching PROTECTED clans too, which DO "
            "re-read (measured 18.3%) — that is the population this cache exists for"
        )
        assert "store_result=True)" not in src, "hardcoded True restores the #0094 problem"

    def test_reads_are_not_gated_only_the_write(self):
        """store_result only suppresses the cache WRITE. If a future change also skipped the
        cache READ for unprotected clans, this optimisation would start costing real API
        calls instead of just saving memory."""
        from qapbot.coc_cache import CoCClanCache
        sig = inspect.signature(CoCClanCache.get_clan)
        assert "store_result" in sig.parameters
        doc = inspect.getdoc(CoCClanCache.get_clan) or ""
        assert "Reads are unaffected" in doc, (
            "get_clan's contract no longer documents that reads are unaffected — the #0094 "
            "gate is only safe because of that property"
        )


class TestProtectedPopulationIsObservable:
    """#0094 also asks for MAX_COC_CLAN_CACHE_ENTRIES to be sized 'for the population
    actually stored'. With the gate applied that population IS the protected set, so the
    count has to be visible in a PROD log that runs at INFO."""

    def test_protected_count_is_logged_at_info_not_debug(self):
        import QapBot
        src = inspect.getsource(QapBot)
        idx = src.find("[COC-CACHE-PROTECT]")
        assert idx != -1, "the protected-count line is gone"
        # Walk back to the logging call that emits it.
        call = src[max(0, idx - 200):idx]
        assert "logging.info" in call, (
            "[COC-CACHE-PROTECT] is not at INFO — PROD runs at INFO, so the number needed to "
            "size the cache cap would be invisible again"
        )
