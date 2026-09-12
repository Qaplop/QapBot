"""#0113 re-measurement (2026-09-12): pin the [DB-BULK-WRITE] elapsed= field.

The pre-#0106 "1.08s -> 3.18s" write-batch figure that #0113 was filed against was
derived from timestamps BETWEEN log lines, not a measured duration — noisy, since
that also counts whatever ran between batches (API fetches, Phase-1/2 work, etc.).
This test pins that `_flush_pending_war_writes` now logs a real duration covering
just the params-build + executemany + commit critical section, so the PROD
re-measurement's correlation analysis has a field to parse.
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportAttributeAccessIssue=false
from __future__ import annotations

import sqlite3
import threading

import pytest

from qapbot.db_manager import WarHistoryDB

_CREATE_WAR_ATTACKS = """
    CREATE TABLE IF NOT EXISTS war_attacks (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        war_id                 TEXT    NOT NULL,
        clan_tag               TEXT    NOT NULL,
        date                   TEXT    NOT NULL,
        player_name            TEXT    NOT NULL,
        player_tag             TEXT    NOT NULL,
        th_level               INTEGER NOT NULL,
        map_position           INTEGER NOT NULL DEFAULT 0,
        attack_order           INTEGER NOT NULL DEFAULT 0,
        stars                  INTEGER NOT NULL,
        destruction            REAL    NOT NULL DEFAULT 0.0,
        defender_tag           TEXT    NOT NULL DEFAULT '',
        defender_th            INTEGER NOT NULL DEFAULT 0,
        defender_map_position  INTEGER NOT NULL DEFAULT 0,
        duration               INTEGER NOT NULL DEFAULT 0,
        is_fresh               INTEGER NOT NULL DEFAULT -1,
        times_defended         INTEGER NOT NULL DEFAULT 0,
        best_def_destruction   REAL    NOT NULL DEFAULT 0.0,
        max_attacks            INTEGER NOT NULL DEFAULT 2,
        missed_attacks         INTEGER NOT NULL DEFAULT 0,
        defensive_stars        INTEGER NOT NULL DEFAULT 0,
        created_at             TEXT    DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(war_id, player_tag, attack_order)
    )
"""

_CREATE_WAR_SUMMARY = """
    CREATE TABLE IF NOT EXISTS war_summary (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        war_id               TEXT    NOT NULL,
        clan_tag             TEXT    NOT NULL,
        opponent_tag         TEXT    NOT NULL,
        opponent_name        TEXT    NOT NULL DEFAULT '',
        clan_stars           INTEGER NOT NULL DEFAULT 0,
        opponent_stars       INTEGER NOT NULL DEFAULT 0,
        clan_destruction     REAL    NOT NULL DEFAULT 0.0,
        opp_destruction      REAL    NOT NULL DEFAULT 0.0,
        team_size            INTEGER NOT NULL DEFAULT 15,
        attacks_per_member   INTEGER NOT NULL DEFAULT 2,
        war_type             TEXT    NOT NULL DEFAULT 'random',
        is_cwl               INTEGER NOT NULL DEFAULT 0,
        cwl_season           TEXT    NOT NULL DEFAULT '',
        war_tag              TEXT    NOT NULL DEFAULT '',
        end_time             TEXT    NOT NULL DEFAULT '',
        state                TEXT    NOT NULL DEFAULT '',
        result               TEXT    NOT NULL DEFAULT '',
        date                 TEXT    NOT NULL,
        clan_lineup_json     TEXT    NOT NULL DEFAULT '[]',
        opp_lineup_json      TEXT    NOT NULL DEFAULT '[]',
        clan_attacks_used    INTEGER NOT NULL DEFAULT 0,
        opp_attacks_used     INTEGER NOT NULL DEFAULT 0,
        round_number         INTEGER,
        created_at           TEXT    DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(war_id, clan_tag)
    )
"""

def _make_db(tmp_path) -> WarHistoryDB:
    """Same fallback-connection pattern as test_db_player_attack_history.py's
    _make_db(): _pool=None forces _sync_conn() through its bare-connection path,
    which ATTACHes a history schema automatically."""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute(_CREATE_WAR_ATTACKS)
    conn.execute(_CREATE_WAR_SUMMARY)
    conn.commit()
    conn.close()

    dm = WarHistoryDB.__new__(WarHistoryDB)
    dm.db_path = db_path
    dm._pool = None  # force fallback to direct sqlite3.connect in _sync_conn
    dm._sync_write_lock = threading.Lock()
    return dm


def _path_of(dm: WarHistoryDB) -> str:
    """Non-Optional view of dm.db_path — always set by _make_db() above."""
    assert dm.db_path is not None
    return dm.db_path


class TestBulkWriteTimingInstrumentation:
    """Appends use empty attack_rows (summary only) deliberately — a non-empty
    attack_rows list also exercises _upsert_player_name_index_in_conn, which in
    turn touches player_name_search/player_name_fts (fts5). Replicating that whole
    schema here would test the fts5 mirror, not the timer; the timer wraps the same
    try block regardless of which of the two INSERTs inside it actually ran, so a
    summary-only append is sufficient to pin the elapsed= field."""

    def test_logs_elapsed_alongside_existing_counts(self, tmp_path, caplog):
        dm = _make_db(tmp_path)
        appends = [
            ("#CLAN1", [], {"war_id": "W1", "date": "2026-09-12T10:00"}),
        ]

        with caplog.at_level("INFO"):
            dm.flush_pending_war_writes(appends, [], batch_size=50)

        [line] = [r.message for r in caplog.records if "[DB-BULK-WRITE]" in r.message]
        # Pre-existing fields must survive unchanged — this is additive instrumentation.
        assert "Flushed batch of 1 war appends" in line
        assert "0 attack rows" in line
        assert "1 summaries" in line
        # The new field the #0113 re-measurement needs.
        assert "elapsed=" in line
        # elapsed= is no longer last on the line (conn= follows it), so split on the
        # unit rather than rstrip'ping it.
        elapsed = float(line.split("elapsed=")[1].split("s ")[0])
        assert elapsed >= 0.0

    def test_elapsed_logged_per_batch_not_once_for_the_whole_call(self, tmp_path, caplog):
        """Two batches (batch_size=1, two appends) must each get their own elapsed=
        value — a single aggregate figure would hide which specific batch was slow,
        which is exactly the resolution the correlation analysis needs."""
        dm = _make_db(tmp_path)
        appends = [
            ("#CLAN1", [], {"war_id": "W1", "date": "2026-09-12T10:00"}),
            ("#CLAN2", [], {"war_id": "W2", "date": "2026-09-12T10:05"}),
        ]

        with caplog.at_level("INFO"):
            dm.flush_pending_war_writes(appends, [], batch_size=1)

        lines = [r.message for r in caplog.records if "[DB-BULK-WRITE]" in r.message]
        assert len(lines) == 2
        for line in lines:
            assert "elapsed=" in line


class TestBulkUpdateTimingInstrumentation:
    """The updates loop was left untimed by the first #0113 pass. Updates are rare on PROD
    (413 vs 116,235 appends over build 40's 60.6h run) but DELETE-then-reinsert, churning
    more pages per row than an append — so an untimed update batch is an unmeasured
    confounder for any read-latency spike attributed to appends."""

    def test_update_batch_logs_elapsed_and_conn(self, tmp_path, caplog):
        dm = _make_db(tmp_path)
        updates = [
            ("#CLAN1", "W1", [], {"war_id": "W1", "date": "2026-09-12T10:00"}),
        ]

        with caplog.at_level("INFO"):
            dm.flush_pending_war_writes([], updates, batch_size=50)

        [line] = [r.message for r in caplog.records if "[DB-BULK-UPDATE]" in r.message]
        assert "Flushed batch of 1 war updates" in line
        assert "elapsed=" in line
        assert "conn=" in line

    def test_append_batch_logs_conn_identity(self, tmp_path, caplog):
        """conn= is what separates the two mechanisms #0113 conflates: SQLite's pager cache
        is per-connection, so a write can only pollute the one connection it held."""
        dm = _make_db(tmp_path)
        appends = [("#CLAN1", [], {"war_id": "W1", "date": "2026-09-12T10:00"})]

        with caplog.at_level("INFO"):
            dm.flush_pending_war_writes(appends, [], batch_size=50)

        [line] = [r.message for r in caplog.records if "[DB-BULK-WRITE]" in r.message]
        assert "conn=" in line


class TestReadTimingAggregator:
    """#0113 (2026-09-12): the windowed aggregator behind [DB-READ-TIMING].

    Every test resets the module-level accumulator in a fixture. That global is shared
    process-wide, so a leaked sample would silently corrupt an unrelated test's aggregate
    — the same class of cross-test contamination that the GC-policy work hit in #0106.
    """

    @pytest.fixture(autouse=True)
    def _reset_accumulator(self):
        import qapbot.db_manager as dbm
        dbm._read_timing_samples.clear()
        dbm._read_timing_window_start = 0.0
        yield
        dbm._read_timing_samples.clear()
        dbm._read_timing_window_start = 0.0

    def test_does_not_log_until_the_window_elapses(self, caplog):
        import qapbot.db_manager as dbm
        with caplog.at_level("INFO"):
            for _ in range(50):
                dbm._record_read_timing("probe", 0.001, conn_id=1)

        assert not [r for r in caplog.records if "[DB-READ-TIMING]" in r.message]
        assert len(dbm._read_timing_samples["probe"]) == 50

    def test_flushes_aggregate_once_the_window_has_passed(self, caplog):
        import qapbot.db_manager as dbm
        dbm._record_read_timing("probe", 0.010, conn_id=1)
        dbm._record_read_timing("probe", 0.020, conn_id=1)
        # Force the window open rather than sleeping 60s.
        dbm._read_timing_window_start -= dbm._READ_TIMING_WINDOW_S + 1

        with caplog.at_level("INFO"):
            dbm._record_read_timing("probe", 0.030, conn_id=1)

        [line] = [r.message for r in caplog.records if "[DB-READ-TIMING]" in r.message]
        assert "probe" in line
        assert "n=3" in line
        assert "max=30.00ms" in line
        # Accumulator must be emptied, or the next window double-counts these samples.
        assert not dbm._read_timing_samples

    def test_conn_mean_spread_is_zero_when_all_samples_share_a_connection(self, caplog):
        """The OS-page-cache signature: every connection slows together, so the spread
        between per-connection means stays flat."""
        import qapbot.db_manager as dbm
        for _ in range(5):
            dbm._record_read_timing("probe", 0.010, conn_id=7)
        dbm._read_timing_window_start -= dbm._READ_TIMING_WINDOW_S + 1

        with caplog.at_level("INFO"):
            dbm._record_read_timing("probe", 0.010, conn_id=7)

        [line] = [r.message for r in caplog.records if "[DB-READ-TIMING]" in r.message]
        assert "conns=1" in line
        assert "conn_mean_spread=0.00ms" in line

    def test_conn_mean_spread_exposes_one_slow_connection(self, caplog):
        """The per-connection pager signature: one connection carries a polluted cache
        while the others do not. This is the field that tells the two mechanisms apart."""
        import qapbot.db_manager as dbm
        for _ in range(5):
            dbm._record_read_timing("probe", 0.001, conn_id=1)
        for _ in range(5):
            dbm._record_read_timing("probe", 0.051, conn_id=2)
        dbm._read_timing_window_start -= dbm._READ_TIMING_WINDOW_S + 1

        with caplog.at_level("INFO"):
            dbm._record_read_timing("probe", 0.001, conn_id=1)

        [line] = [r.message for r in caplog.records if "[DB-READ-TIMING]" in r.message]
        assert "conns=2" in line
        spread = float(line.split("conn_mean_spread=")[1].rstrip("ms"))
        assert spread == pytest.approx(50.0, abs=1.0)


class TestWarSummaryStateReadIsSampled:
    @pytest.fixture(autouse=True)
    def _reset_accumulator(self):
        import qapbot.db_manager as dbm
        dbm._read_timing_samples.clear()
        dbm._read_timing_window_start = 0.0
        yield
        dbm._read_timing_samples.clear()
        dbm._read_timing_window_start = 0.0

    def test_successful_lookup_is_sampled(self, tmp_path):
        import qapbot.db_manager as dbm
        dm = _make_db(tmp_path)
        conn = sqlite3.connect(_path_of(dm))
        conn.execute(
            "INSERT INTO war_summary (war_id, clan_tag, opponent_tag, state, date) "
            "VALUES ('W1', '#C1', '#OPP', 'war_ended', '2026-09-12T10:00')"
        )
        conn.commit()
        conn.close()

        assert dm.get_war_summary_state_sync("#C1", "W1") == "war_ended"
        assert len(dbm._read_timing_samples["get_war_summary_state_sync"]) == 1

    def test_failed_lookup_is_still_sampled(self, tmp_path):
        """Recorded in `finally`: a read slow enough to error is exactly the case #0113
        cares about, so dropping those samples would bias the aggregate optimistic."""
        import qapbot.db_manager as dbm
        dm = _make_db(tmp_path)
        conn = sqlite3.connect(_path_of(dm))
        conn.execute("DROP TABLE war_summary")
        conn.commit()
        conn.close()

        assert dm.get_war_summary_state_sync("#C1", "W1") is None
        assert len(dbm._read_timing_samples["get_war_summary_state_sync"]) == 1


class TestMeminfoSampling:
    """#0113 (2026-09-12): /proc/meminfo sampling for the OS-page-cache mechanism.

    The first cut sampled only at cycle end — ~14s after the last flush on PROD, by which
    time the kernel had already written back whatever the write dirtied. These pin the
    parser and the delta rendering that replaced it.
    """

    _SAMPLE = [
        "MemTotal:       10240000 kB",
        "MemFree:          512000 kB",
        "MemAvailable:    8294400 kB",
        "Buffers:           10240 kB",
        "Cached:          5431296 kB",
        "SwapCached:        20480 kB",
        "Dirty:              3072 kB",
        "Writeback:             0 kB",
    ]

    def test_parses_the_four_fields_in_mb(self):
        from qapbot.db_manager import _parse_meminfo_mb
        got = _parse_meminfo_mb(self._SAMPLE)
        assert got == {
            "MemAvailable": 8100, "Cached": 5304, "Dirty": 3, "Writeback": 0,
        }

    def test_swapcached_is_not_mistaken_for_cached(self):
        """'Cached' must be an exact key match — a prefix/substring test would read
        SwapCached's value and silently report the wrong number."""
        from qapbot.db_manager import _parse_meminfo_mb
        assert _parse_meminfo_mb(self._SAMPLE)["Cached"] == 5304   # not 20480 kB -> 20

    def test_malformed_line_is_skipped_not_fatal(self):
        from qapbot.db_manager import _parse_meminfo_mb
        got = _parse_meminfo_mb(["Dirty:  not-a-number", "Cached:   1048576 kB"])
        assert got == {"Cached": 1024}

    def test_missing_proc_yields_empty_dict_not_an_exception(self, monkeypatch):
        """The write path calls this inline in a log f-string; raising there would abort a
        committed batch's log line on any non-Linux box."""
        import qapbot.db_manager as dbm

        def _boom(*_a, **_kw):
            raise FileNotFoundError("/proc/meminfo")

        monkeypatch.setattr("builtins.open", _boom)
        assert dbm._meminfo_mb() == {}

    def test_delta_string_reports_signed_change_against_the_before_snapshot(self):
        from qapbot.db_manager import _meminfo_delta_str
        before = {"Cached": 5000, "Dirty": 2, "Writeback": 0, "MemAvailable": 8300}
        after = {"Cached": 5120, "Dirty": 45, "Writeback": 12, "MemAvailable": 8100}
        out = _meminfo_delta_str(before, after)
        assert "cached=5120MB(+120)" in out
        assert "dirty=45MB(+43)" in out
        assert "writeback=12MB(+12)" in out
        assert "avail=8100MB(-200)" in out

    def test_delta_string_is_empty_when_unavailable(self):
        """Must degrade to '' so the [DB-BULK-WRITE] line stays well-formed off Linux."""
        from qapbot.db_manager import _meminfo_delta_str
        assert _meminfo_delta_str({}, {}) == ""

    def test_delta_string_omits_deltas_when_no_before_snapshot(self):
        from qapbot.db_manager import _meminfo_delta_str
        out = _meminfo_delta_str({}, {"Cached": 100, "Dirty": 1, "Writeback": 0, "MemAvailable": 50})
        assert "cached=100MB" in out and "(+" not in out

    def test_write_batch_line_stays_wellformed_without_proc(self, tmp_path, caplog):
        """End-to-end on a box with no /proc: the batch must still commit and log."""
        dm = _make_db(tmp_path)
        appends = [("#CLAN1", [], {"war_id": "W1", "date": "2026-09-12T10:00"})]

        with caplog.at_level("INFO"):
            dm.flush_pending_war_writes(appends, [], batch_size=50)

        [line] = [r.message for r in caplog.records if "[DB-BULK-WRITE]" in r.message]
        assert "elapsed=" in line and "conn=" in line
