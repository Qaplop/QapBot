"""SQLite memory-budget pragmas (tracker #0106, 2026-09-07).

PROD ran 9 SQLite connections (1 async + 8 pooled) that each reserved `mmap_size=8 GB` and a
64 MB private page cache, against a 25 GB main + 36 GB history DB on a box with 10 GB of RAM.
Those numbers were sized for the original spinning NAS disks, where trading RAM for avoided
seeks was worth nearly any price; the DB now lives on an eSATA SSD, which inverted the trade.

What the old values cost, measured on 2026-09-07: RSS 6.5-8.4 GB, NAS at 94%, then page-fault
thrashing — wall time exploded while CPU time did not (cores_busy 0.63 -> 0.19),
`_get_active_wars()` (a documented zero-I/O in-memory walk) went 3.3s -> 626s, categorizing
465k clans took 144.7s wall for 5.8s of CPU, and one gen-1 `gc.collect()` took 502s freeing the
same ~20k objects it always frees.

These tests pin the properties that keep that from silently coming back: the values are
config-driven rather than four hard-coded copies, and the `history` schema — which does NOT
inherit `cache_size` across ATTACH — gets its own explicit statement.
"""
from __future__ import annotations

import re

from qapbot.db_manager import db_memory_pragmas


class TestPragmaGeneration:
    def test_emits_both_memory_pragmas(self):
        stmts = db_memory_pragmas()
        assert len(stmts) == 2
        assert any("cache_size" in s for s in stmts)
        assert any("mmap_size" in s for s in stmts)

    def test_unqualified_form_has_no_schema_prefix(self):
        for stmt in db_memory_pragmas():
            assert re.match(r"^PRAGMA (cache_size|mmap_size)=", stmt), stmt

    def test_schema_qualified_form_pins_the_attached_database(self):
        """`cache_size` is per-pager and does NOT carry across ATTACH, so the history schema
        needs its own statement or it silently keeps SQLite's default cache."""
        for stmt in db_memory_pragmas("history"):
            assert re.match(r"^PRAGMA history\.(cache_size|mmap_size)=", stmt), stmt

    def test_cache_size_is_negative_meaning_kibibytes_not_pages(self):
        """A positive cache_size is counted in PAGES, so its memory cost would swing with
        page_size (this DB migrates toward 16 KB pages) — the negative form is KiB and stable."""
        cache = next(s for s in db_memory_pragmas() if "cache_size" in s)
        value = int(cache.split("=")[1])
        assert value < 0, f"cache_size must be negative (KiB), got {value}"

    @staticmethod
    def _with_config(monkeypatch, **overrides):
        """BotConfig is a frozen dataclass, so swap the module attribute the helper reads
        rather than mutating the instance."""
        import dataclasses
        import qapbot.config as cfg
        monkeypatch.setattr(cfg, "CONFIG", dataclasses.replace(cfg.CONFIG, **overrides))

    def test_values_track_config_rather_than_being_hard_coded(self, monkeypatch):
        self._with_config(monkeypatch, db_cache_size_mb=32, db_mmap_size_mb=128)

        stmts = db_memory_pragmas()

        assert "PRAGMA cache_size=-32768" in stmts     # 32 MB expressed in KiB
        assert "PRAGMA mmap_size=134217728" in stmts   # 128 MB expressed in bytes

    def test_mmap_can_be_fully_disabled(self, monkeypatch):
        """mmap_size=0 must stay reachable — it is SQLite's own default and the fallback if the
        mapped working set ever turns out to be what pressures the box."""
        self._with_config(monkeypatch, db_mmap_size_mb=0)

        assert "PRAGMA mmap_size=0" in db_memory_pragmas()


class TestPerSchemaBudgets:
    """main and history do different jobs: main (24.5 GB, 16 KB pages) carries every per-cycle
    write; history (35.3 GB, 4 KB pages) sees only the nightly migration and user-command
    UNION ALL reads. Bigger file, colder access, smaller share of both budgets."""

    @staticmethod
    def _mb_from(stmts, kind):
        stmt = next(s for s in stmts if kind in s)
        raw = int(stmt.split("=")[1])
        return abs(raw) // 1024 if kind == "cache_size" else raw // (1024 * 1024)

    def test_history_gets_a_smaller_budget_than_main(self):
        main, history = db_memory_pragmas(), db_memory_pragmas("history")

        assert self._mb_from(history, "cache_size") < self._mb_from(main, "cache_size")
        assert self._mb_from(history, "mmap_size") < self._mb_from(main, "mmap_size")

    def test_history_mmap_is_pinned_not_inherited(self, monkeypatch):
        """An unqualified PRAGMA mmap_size becomes the default for databases ATTACHed later, so
        without an explicit history statement the 35 GB cold DB would silently inherit main's
        larger ceiling — the same shape as the original bug, one schema over."""
        import dataclasses
        import qapbot.config as cfg
        monkeypatch.setattr(cfg, "CONFIG", dataclasses.replace(
            cfg.CONFIG, db_mmap_size_mb=4096, db_history_mmap_size_mb=64,
        ))

        assert "PRAGMA history.mmap_size=67108864" in db_memory_pragmas("history")

    def test_an_unknown_schema_falls_back_to_the_main_budget(self):
        """Defensive: a future ATTACH of some other schema should get main's numbers rather than
        silently landing on history's deliberately-small ones."""
        other = db_memory_pragmas("scratch")
        assert self._mb_from(other, "cache_size") == self._mb_from(db_memory_pragmas(), "cache_size")


class TestSwapAvoidanceInvariants:
    """THE governing constraint: the NAS's swap file is on the HDD root volume, not on the SSD
    that holds the DB. A swapped page costs ~10 ms to fault back in; a dropped DB page costs
    ~100 us to re-read from SSD. So the whole strategy is "never give the kernel a reason to
    swap", and that sorts the two knobs counter-intuitively:

      cache_size -> malloc'd, ANONYMOUS -> reclaimable only by writing to HDD swap. Dangerous.
      mmap_size  -> clean, FILE-backed  -> dropped and re-read from SSD, never swapped. Safe.

    These pin the resulting invariants so a future "let's give SQLite more cache" has to argue
    with them explicitly rather than quietly re-creating 2026-09-07.
    """

    def test_anonymous_cache_total_stays_bounded(self):
        """cache_size is paid per connection in anonymous memory, so it multiplies by pool size
        — unlike mmap, where all connections map the same files and share the physical pages."""
        from qapbot.config import CONFIG
        total_anon_mb = (
            CONFIG.db_cache_size_mb + CONFIG.db_history_cache_size_mb
        ) * CONFIG.db_pool_size

        assert total_anon_mb <= 512, (
            f"{total_anon_mb} MB of anonymous SQLite cache across {CONFIG.db_pool_size} "
            "connections — this is precisely the memory that can be pushed to HDD swap"
        )

    def test_the_safe_knob_carries_more_of_the_budget_than_the_dangerous_one(self):
        from qapbot.config import CONFIG
        mapped = CONFIG.db_mmap_size_mb + CONFIG.db_history_mmap_size_mb
        anon = (CONFIG.db_cache_size_mb + CONFIG.db_history_cache_size_mb) * CONFIG.db_pool_size

        assert mapped > anon, (
            f"mapped ceiling {mapped} MB should exceed anonymous cache {anon} MB — "
            "the file-backed cache is the one that cannot reach HDD swap"
        )

    def test_mmap_ceiling_stays_far_below_the_database_size(self):
        """Bounded, not unlimited. The original 8 GB let SQLite map arbitrarily much of a
        24.5 + 35.3 GB corpus; a large actively-referenced mapped set looks hot to the kernel
        and biases reclaim toward swapping the anonymous Python heap instead."""
        from qapbot.config import CONFIG
        assert CONFIG.db_mmap_size_mb <= 4096
        assert CONFIG.db_history_mmap_size_mb <= CONFIG.db_mmap_size_mb


class TestAppliedEverywhere:
    """The four call sites (sync pragmas, initialize, reconnect, pooled connection) had drifted
    into repeating the same literals — which is how the 8 GB reservation ended up on all 9
    connections unnoticed. No literal may come back."""

    def test_no_hardcoded_legacy_values_remain_in_db_manager(self):
        from pathlib import Path
        src = Path(__file__).resolve().parents[2] / "qapbot" / "db_manager.py"
        text = src.read_text(encoding="utf-8-sig")

        assert "cache_size=-65536" not in text, "the 64 MB literal is back"
        # The VACUUM path legitimately pins its own values (mmap off, bigger cache for a
        # sequential rebuild) and is excluded by matching only the exact 8 GB reservation.
        assert "mmap_size=8589934592" not in text, "the 8 GB mmap literal is back"

    def test_every_pragma_application_goes_through_the_helper(self):
        from pathlib import Path
        src = Path(__file__).resolve().parents[2] / "qapbot" / "db_manager.py"
        text = src.read_text(encoding="utf-8-sig")

        # main-schema application: _apply_sync_pragmas, initialize(), _reconnect()
        # history-schema application: pooled _create_conn, initialize(), _reconnect()
        assert text.count("db_memory_pragmas()") >= 3
        assert text.count('db_memory_pragmas("history")') >= 3


class TestConfigDefaults:
    """Defaults encode the post-SSD tuning; env vars exist so PROD can retune without a deploy."""

    def test_ssd_era_defaults(self):
        from qapbot.config import CONFIG
        assert CONFIG.db_mmap_size_mb == 1024
        assert CONFIG.db_history_mmap_size_mb == 256
        assert CONFIG.db_cache_size_mb == 32
        assert CONFIG.db_history_cache_size_mb == 8
        assert CONFIG.db_pool_size >= 1

    def test_env_overrides_are_parsed_and_clamped(self, monkeypatch):
        """Calls load_config() directly rather than reloading the module: a reload swaps
        qapbot.config.CONFIG for a new object while every module that did
        `from qapbot.config import CONFIG` keeps the old one, and that split state breaks
        unrelated tests downstream."""
        from qapbot.config import load_config
        monkeypatch.setenv("DB_MMAP_SIZE_MB", "256")
        monkeypatch.setenv("DB_CACHE_SIZE_MB", "0")     # clamped to >=1: a 0 cache is invalid
        monkeypatch.setenv("DB_POOL_SIZE", "-3")        # clamped to >=1

        loaded = load_config()

        assert loaded.db_mmap_size_mb == 256
        assert loaded.db_cache_size_mb == 1
        assert loaded.db_pool_size == 1

    def test_unparseable_env_falls_back_to_the_default(self, monkeypatch):
        from qapbot.config import load_config
        monkeypatch.setenv("DB_MMAP_SIZE_MB", "not-a-number")

        assert load_config().db_mmap_size_mb == 1024
