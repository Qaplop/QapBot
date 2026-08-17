"""
Database manager for QapBot persistent storage.

This module provides SQLite database operations for war history and all persistent data,
serving as the single database access layer for the application.

Key Features:
- WAL mode for server-machine reliability and concurrent reads
- Idempotent operations (safe to re-run)
- Batch transaction support for performance
- Comprehensive error handling
- Database integrity checks and consistency validation

Architecture:
- Only accessed through cache_manager.py (CACHE.db_manager)
- Never called directly from business logic or commands
- All database queries and writes are centralized here

Example Usage:
    db = WarHistoryDB()
    await db.initialize("data/qapbot.db")
    await db.add_war_records(clan_tag, records)
    history = await db.get_clan_history(clan_tag, month=1, year=2026)

Note: All methods are async for consistency with Discord.py event loop,
      though SQLite operations are synchronous (wrapped with asyncio.to_thread).
      Sync variants (*_sync) exist for use from non-async contexts.
"""

import asyncio
import hashlib
import logging
import os
import queue
import threading
import time as _time
from contextlib import contextmanager

# Separate, independently-patchable reference to time.monotonic used ONLY by the
# migration time-budget code below (_migrate_table_batch_by_date /
# _migrate_cwl_table_by_season / monthly_history_migration). Deliberately NOT
# `_time.monotonic` directly: `_time` IS the real `time` module object (same
# identity, not a copy), so patching `_time.monotonic` in a test also silently
# hijacks asyncio's own event-loop clock (`base_events.py`'s `_run_once()` calls
# `time.monotonic()` on every loop iteration for scheduling) — that produced
# wildly unpredictable extra ticks and broke deterministic deadline tests. This
# module-level name is a plain function reference; rebinding it in a test only
# affects this file's deadline checks, nothing in asyncio.
_monotonic = _time.monotonic
from typing import List, Dict, Any, Optional, Tuple, Set, TYPE_CHECKING, Callable, Awaitable, cast, Iterable

if TYPE_CHECKING:
    import aiosqlite
else:
    try:
        import aiosqlite
    except ImportError:
        aiosqlite = None  # type: ignore


def derive_history_db_path(db_path: str) -> str:
    """Derive the default history DB path from the hot DB path.

    E.g. ``data/qapbot.db`` -> ``data/qapbot_history.db``.
    """
    base, ext = os.path.splitext(db_path)
    return f"{base}_history{ext or '.db'}"


# ─────────────────────────────────────────────────────────────────────────────
# Registry of every table/column that references clans.clan_tag — the single
# source of truth iterated by is_clan_tag_referenced() (orphan-purge guard).
#
# ⚠️ Any NEW table with a `REFERENCES clans` FK (or a non-FK column that
# conceptually depends on a clan being real) MUST be added here, or the orphan
# purge (delete_clan_if_unreferenced) can hard-delete a clans row that table
# still needs — with FK CASCADE, silently wiping its rows.
# tests/unit/test_clan_tag_reference_registry.py asserts every DDL
# `REFERENCES clans` clause has a matching entry, turning that silent
# data-loss bug into a test failure.
#
# Format: (schema, table, column, extra_where).
# extra_where handles deliberately-overloaded columns: leaderboard_messages
# stores clan tags, family tags, AND (mode='whois_player') player tags in the
# same column — a whois_player row matching a tag is NOT evidence of a real
# clan and must not "protect" the very bogus placeholder rows the purge
# targets (see qapbot/docs/COPILOT_PITFALLS_COOKBOOK.md Pitfall 17). Any
# future overloaded column needs its discriminator filtered here the same way.
CLAN_TAG_REFERENCING_TABLES: Tuple[Tuple[str, str, str, str], ...] = (
    # FK-enforced (REFERENCES clans in the DDL):
    ("main", "clan_family_members", "clan_tag", ""),
    ("main", "user_players", "current_clan_tag", ""),
    ("main", "guild_member_clans", "clan_tag", ""),
    ("main", "guild_welcome_clans", "clan_tag", ""),
    ("main", "cwl_event_clans", "clan_tag", ""),
    ("main", "cwl_assignments", "assigned_clan_tag", ""),
    # Non-FK columns that conceptually depend on a real clan:
    ("main", "guild_clan_roles", "clan_tag", ""),
    ("main", "subscriptions", "clan_tag", ""),  # also stores family tags — matching a clan tag still counts
    ("main", "leaderboard_messages", "clan_tag", "AND mode != 'whois_player'"),
    ("main", "guild_config", "welcome_clan_tag", ""),  # legacy single-clan welcome config, still read for backward-compat
    # Historical time-series data (hot + history schemas):
    ("main", "war_summary", "clan_tag", ""),
    ("main", "war_attacks", "clan_tag", ""),
    ("main", "cwl_league_groups", "clan_tag", ""),
    ("history", "war_summary", "clan_tag", ""),
    ("history", "war_attacks", "clan_tag", ""),
    ("history", "cwl_league_groups", "clan_tag", ""),
)


# player_name_fts's rowid scheme version marker (bot_metadata key) — bump the value string
# whenever the rowid-derivation scheme below changes, forcing _backfill_player_name_search_if_needed
# to rebuild the table once on the next startup instead of trusting stale rowids.
PLAYER_NAME_FTS_ROWID_SCHEME_KEY = "player_name_fts_rowid_scheme"
PLAYER_NAME_FTS_ROWID_SCHEME_VALUE = "tag_hash_v1"


def _fts_rowid_for_tag(player_tag: str) -> int:
    """Deterministic FTS5 rowid derived from player_tag (2026-08-17 fix).

    player_name_fts.player_tag is UNINDEXED — FTS5 gives no index of any kind to a non-MATCH
    column, so a plain equality lookup/delete on it (``WHERE player_tag = ?``) has no viable
    query plan and falls back to a full table scan. That was invisible in tests (tiny tables)
    but became catastrophic in PROD the moment player_name_fts held its real ~6.6M rows: every
    incremental war-write flush calls this once per changed player, turning a routine update
    cycle into hundreds of full-table scans back to back and effectively hanging PHASE-3's
    batched flush (confirmed via the 2026-08-17 PROD incident log — the cycle stalled silently
    right after "[PHASE-3B] Completed", the code path that immediately leads into this table's
    incremental writer).

    ``rowid`` is the one column FTS5 genuinely indexes. Assigning it explicitly as a hash of
    player_tag means every caller can compute the same rowid independently — no DB round trip
    needed to look one up — and ``WHERE rowid = ?`` is then O(1) instead of O(table size).
    8 bytes of a cryptographic-strength hash keeps collision probability negligible even at
    PROD's row count (birthday-bound ~3e9, far above 6.6M tags)."""
    digest = hashlib.blake2b(player_tag.encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=False) - (1 << 63)  # fold into signed 64-bit rowid range


def _create_history_schema_sync(conn: Any, build_expensive_indexes: bool = True) -> None:
    """Create the 4 history.* time-series tables on a plain ``sqlite3`` connection (idempotent).

    Sync counterpart to ``WarHistoryDB._create_history_schema()`` (async). In
    production the history DB file already has these tables (created once by
    the async connection in ``initialize()`` before any sync connection ever
    attaches to it), so this is a cheap no-op there. It matters for:
      - ``_SyncConnectionPool``: defensive — guarantees pooled connections
        can always query ``history.*`` even if pool creation somehow raced
        ahead of async schema creation.
      - ``WarHistoryDB._sync_conn()``'s bare-connection fallback (used by
        tests / pre-initialize callers that never call ``initialize()``) —
        those construct a fresh history DB file/`:memory:` db with no schema
        at all, so table creation here is required, not just defensive.

    Safe to call on every connection open — ``CREATE TABLE/INDEX IF NOT EXISTS``.

    Args:
        build_expensive_indexes: When False, skips ``idx_wa_player_tag_date``
            — a composite index whose FIRST build on a multi-million-row
            production ``war_attacks`` table is a full table scan + sort that
            can take minutes. ``_SyncConnectionPool._create_conn()`` passes
            False here defensively: pool-fill runs synchronously on the
            event-loop thread inside ``initialize()`` (plain blocking Python,
            not awaited) — if it ever ran before ``_create_schema()``/
            ``_create_history_schema()`` finish building this index (they run
            first in the current ordering and always build it inline, see
            those methods), attempting the build here too would freeze the
            whole bot, not just time out a coroutine. Tests and standalone
            migration/diagnostic scripts keep the default True — their DBs
            are small/fresh, so building inline there is fine and expected.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS history.war_attacks (
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
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS history.idx_wa_player_tag ON war_attacks(player_tag)")
    conn.execute("CREATE INDEX IF NOT EXISTS history.idx_wa_war_clan ON war_attacks(war_id, clan_tag)")
    conn.execute("CREATE INDEX IF NOT EXISTS history.idx_wa_clan_date ON war_attacks(clan_tag, date)")
    conn.execute("CREATE INDEX IF NOT EXISTS history.idx_wa_zero_attacks ON war_attacks(attack_order) WHERE attack_order = 0")
    # Composite (player_tag, date) — get_player_attack_history_sync (leaderboard scope="all")
    # filters by both; without date in the index, SQLite would rowid-fetch every historical
    # row for that player_tag just to filter down to one month. idx_wa_player_tag (above) is
    # kept rather than dropped: DROP INDEX on the multi-million-row war_attacks table is slow
    # enough that it must only run during nightly maintenance, never on a connection open.
    # See build_expensive_indexes docstring above — this call is intentionally gated,
    # not just IF-NOT-EXISTS-guarded, because the *first* build on a large production
    # table is what caused the 2026-07-30 startup-hang incident.
    if build_expensive_indexes:
        conn.execute("CREATE INDEX IF NOT EXISTS history.idx_wa_player_tag_date ON war_attacks(player_tag, date)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS history.war_summary (
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
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS history.idx_ws_clan_tag ON war_summary(clan_tag)")
    conn.execute("CREATE INDEX IF NOT EXISTS history.idx_ws_clan_date ON war_summary(clan_tag, date)")
    conn.execute("CREATE INDEX IF NOT EXISTS history.idx_ws_cwl_season ON war_summary(clan_tag, cwl_season)")
    conn.execute("CREATE INDEX IF NOT EXISTS history.idx_ws_war_id ON war_summary(war_id)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS history.cwl_league_groups (
            league_group_id   TEXT    NOT NULL,
            cwl_season        TEXT    NOT NULL,
            clan_tag          TEXT    NOT NULL,
            league_rank       TEXT    DEFAULT NULL,
            cwl_ended         INTEGER NOT NULL DEFAULT 0,
            group_rank        INTEGER DEFAULT NULL,
            total_stars       INTEGER DEFAULT NULL,
            total_destruction REAL    DEFAULT NULL,
            PRIMARY KEY (cwl_season, clan_tag)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS history.idx_cwl_league_groups_id "
        "ON cwl_league_groups (league_group_id, cwl_season)"
    )

    conn.execute("""
        CREATE TABLE IF NOT EXISTS history.cwl_league_rounds (
            war_tag         TEXT    NOT NULL PRIMARY KEY,
            cwl_season      TEXT    NOT NULL,
            cwl_round       INTEGER NOT NULL,
            league_group_id TEXT    NOT NULL
        )
    """)


def explicit_column_list_from_conn(conn: Any, table: str) -> str:
    """Module-level counterpart of `WarHistoryDB._explicit_column_list_sync` for standalone
    scripts (`qapbot/scripts/*.py`) that open their own `sqlite3.connect()` + `attach_history_db()`
    instead of going through a `WarHistoryDB` instance — same reason `attach_history_db` itself
    exists as a free function rather than only a class method. See
    `WarHistoryDB._explicit_column_list_sync`'s docstring for the full 2026-08-14/2026-08-16
    hot/history column-order-drift incident writeup this exists to prevent: any query combining
    `main.<table>` and `history.<table>` into one result set MUST name every column explicitly on
    both sides of the `UNION`/`UNION ALL`, never a bare `SELECT *`, since the two schemas'
    physical column order can (and does, for war_attacks/war_summary) differ even when their
    column NAMES and CREATE TABLE source text agree."""
    cur = conn.execute(f"PRAGMA main.table_info({table})")
    return ", ".join(row["name"] for row in cur.fetchall())


def attach_history_db(conn: Any, db_path: str, history_db_path: Optional[str] = None, read_only: bool = False) -> str:
    """Attach the history DB as schema ``history`` on a bare ``sqlite3`` connection.

    Convenience helper for standalone maintenance/backfill scripts
    (``qapbot/scripts/*.py``) that open their own ``sqlite3.connect()``
    instead of going through ``WarHistoryDB`` — lets them transparently
    read/write ``history.*`` tables (hot/history DB split) with a single
    extra line, instead of duplicating the ATTACH + schema-creation logic.

    Ensures the history schema's 4 time-series tables exist (idempotent),
    so ``UNION``/``UNION ALL`` queries against ``history.war_attacks`` etc.
    never fail with "no such table" even against a brand-new history DB file.

    Args:
        conn: An open ``sqlite3.Connection`` (read-write or read-only URI).
        db_path: Path to the hot DB this connection is attached to — used to
            derive the default history path if ``history_db_path`` is not given.
        history_db_path: Explicit history DB path. If omitted, derived from
            ``db_path`` (e.g. ``data/qapbot.db`` -> ``data/qapbot_history.db``).
        read_only: True if ``conn`` was opened read-only (e.g. a diagnostic
            script using ``sqlite3.connect('file:...?mode=ro', uri=True)``).
            ATTACH inherits the read/write mode of the main connection, so a
            read-only connection cannot ``CREATE TABLE`` on the attached
            schema — table creation is skipped in that case (the history DB
            is expected to already have its schema from a prior bot run).

    Returns:
        The resolved history DB path that was attached.
    """
    resolved = history_db_path or derive_history_db_path(db_path)
    if read_only:
        conn.execute(
            "ATTACH DATABASE ? AS history",
            (f"file:{resolved}?mode=ro",),
        )
    else:
        conn.execute("ATTACH DATABASE ? AS history", (resolved,))
        # Schema-qualified: the unqualified PRAGMA journal_mode=WAL executed
        # earlier (on the main connection setup) only affects schema 'main' —
        # it does NOT retroactively apply to a schema ATTACHed afterwards.
        # Without this, 'history' silently stays on the default rollback
        # journal, forcing an fsync per commit — catastrophic on server-machine/external
        # SATA storage (this caused the 2025-07 migration slowdown incident).
        conn.execute("PRAGMA history.journal_mode=WAL")
        conn.execute("PRAGMA history.synchronous=NORMAL")
        _create_history_schema_sync(conn)
    return resolved


class _SyncConnectionPool:
    """Bounded pool of ``sqlite3`` connections for threaded DB access.

    Every connection is created with the full pragma set (WAL, mmap,
    cache_size, etc.) and ``Row`` factory.  The pool supports ``drain()``
    for exclusive maintenance access — it waits for all checked-out
    connections to be released (by the *owning* worker thread, which
    properly finalizes prepared statements), then closes them cleanly.
    """

    def __init__(self, db_path: str, setup_conn: Callable[[Any], None], pool_size: int = 8, history_db_path: Optional[str] = None):
        self._db_path = db_path
        self._setup_conn = setup_conn
        self._pool_size = pool_size
        self._history_db_path = history_db_path
        self._available: queue.Queue[Any] = queue.Queue(maxsize=pool_size)
        self._active: set[int] = set()  # id(conn) of checked-out connections
        self._active_lock = threading.Lock()
        self._drain_event = threading.Event()
        self._drain_event.set()  # not draining
        self._draining = False
        self._closed = False
        self._fill()

    def _create_conn(self) -> Any:
        import sqlite3 as _sq
        conn = _sq.connect(self._db_path, check_same_thread=False)
        conn.row_factory = _sq.Row
        self._setup_conn(conn)
        if self._history_db_path:
            # Attach the history DB as schema 'history' on every pooled connection so
            # *_sync methods can transparently query/write hot (main.*) + history.* data.
            conn.execute("ATTACH DATABASE ? AS history", (self._history_db_path,))
            # Schema-qualified — see attach_history_db() for why this is required
            # (unqualified journal_mode=WAL set before ATTACH doesn't carry over).
            conn.execute("PRAGMA history.journal_mode=WAL")
            conn.execute("PRAGMA history.synchronous=NORMAL")
            # build_expensive_indexes=False: pool-fill runs synchronously on the
            # event-loop thread (see initialize()) — see that flag's docstring above
            # for why this stays False here defensively even though _create_schema()/
            # _create_history_schema() (which run first) already build it inline.
            _create_history_schema_sync(conn, build_expensive_indexes=False)
        return conn

    def _fill(self) -> None:
        for _ in range(self._pool_size):
            self._available.put(self._create_conn())
        logging.info(f"[CONN-POOL] Filled with {self._pool_size} connections")

    def acquire(self, timeout: float = 30.0) -> Any:
        """Check out a connection.  Raises on timeout or if pool is draining."""
        if self._closed:
            raise RuntimeError("[CONN-POOL] Pool is closed")
        if self._draining:
            raise RuntimeError("[CONN-POOL] Pool is draining for maintenance")
        try:
            conn = self._available.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError(
                f"[CONN-POOL] Exhausted (size={self._pool_size}, timeout={timeout}s)"
            )
        with self._active_lock:
            self._active.add(id(conn))
        return conn

    def release(self, conn: Any) -> None:
        """Return a connection.  During drain the *worker thread* closes it."""
        with self._active_lock:
            self._active.discard(id(conn))
            _signal = self._draining and not self._active
        if _signal:
            self._drain_event.set()
        if self._draining or self._closed:
            try:
                conn.close()  # closed by the owning thread → clean finalization
            except Exception:
                pass
        else:
            self._available.put(conn)

    @contextmanager
    def drain(self, timeout: float = 120.0):
        """Close all connections so the caller can run with EXCLUSIVE lock.

        1. Set draining flag → new ``acquire()`` calls raise immediately.
        2. Wait for all checked-out connections to be released (worker
           threads call ``release()`` which closes them inline).
        3. Close remaining idle connections from the queue.
        4. Yield to the caller (maintenance runs here).
        5. On exit, refill the pool with fresh connections.
        """
        self._draining = True
        with self._active_lock:
            if not self._active:
                self._drain_event.set()
            else:
                self._drain_event.clear()
        if not self._drain_event.wait(timeout=timeout):
            logging.warning(
                f"[CONN-POOL] drain() timed out after {timeout}s — "
                f"{len(self._active)} connection(s) still active"
            )
        # Close idle pool connections
        _closed = 0
        while not self._available.empty():
            try:
                c = self._available.get_nowait()
                c.close()
                _closed += 1
            except (queue.Empty, Exception):
                break
        logging.info(f"[CONN-POOL] Drained — {_closed} idle connection(s) closed")
        try:
            yield
        finally:
            self._fill()
            self._draining = False

    def close_all(self) -> None:
        """Shut down the pool (bot shutdown)."""
        self._closed = True
        _closed = 0
        while not self._available.empty():
            try:
                c = self._available.get_nowait()
                c.close()
                _closed += 1
            except (queue.Empty, Exception):
                break
        logging.info(f"[CONN-POOL] Closed {_closed} connection(s)")

class WarHistoryDB:
    """
    SQLite database manager for war history records.
    
    Handles all database operations for historical war data, implementing
    WAL mode for server-machine reliability and concurrent read access.
    
    Schema:
        war_attacks: Per-attack rows for each war participant
        war_summary: Per-war aggregate stats (result, team sizes, stars)
        clans, users, user_players, guild_configs, etc.
    
    Configuration:
        - WAL mode: Allows concurrent reads during writes (server-machine-friendly)
        - Busy timeout: 5000ms for network storage latency
        - Foreign keys: Enabled for referential integrity
        - Temp store: In-memory for faster operations
    
    Thread Safety:
        - Async writes serialised by _write_lock
        - Sync DB access uses a bounded connection pool (_pool) so
          asyncio.to_thread() workers share pre-configured connections
    """

    # 25h, not a short TTL: /status is called maybe once every few days in
    # practice, so a short TTL just means every call pays the full cold-scan
    # cost anyway. These counts barely move within a day, so a long TTL is
    # fine — the cache is explicitly warmed at startup and refreshed at the
    # end of nightly maintenance (see QapBot.py), so /status should always
    # hit a warm cache during normal operation. See get_global_db_statistics_sync.
    _GLOBAL_STATS_TTL = 25 * 3600.0  # seconds

    def __init__(self):
        """Initialize database manager (connection created in initialize())."""
        self.db_path: Optional[str] = None
        self.history_db_path: Optional[str] = None  # ATTACHed as schema 'history' (hot/history DB split)
        self.conn: Optional['aiosqlite.Connection'] = None
        self._initialized = False
        self._write_lock = asyncio.Lock()  # Serialises all writes; prevents 'transaction within transaction'
        self._tls = threading.local()  # Thread-local storage for batch state
        self._sync_write_lock = threading.Lock()  # Serialises sync writes across threads (SQLite allows only 1 writer)
        self._pool: Optional[_SyncConnectionPool] = None  # Created in initialize()
        self._global_stats_cache: Optional[Dict[str, int]] = None  # TTL cache — see get_global_db_statistics_sync
        self._global_stats_cache_ts: float = 0.0

    @property
    def _conn(self) -> Any:
        """Non-optional view of conn. Only valid after _ensure_connection()."""
        assert self.conn is not None, "[DB] Connection is None; _ensure_connection() not called"
        return self.conn

    async def _retry_on_locked(self, coro_factory: Callable[[], Awaitable[None]], *, retries: int = 4, base_delay: float = 0.5) -> None:
        """Retry an async DB operation on ``database is locked`` errors.

        *coro_factory* is a zero-arg callable returning a fresh awaitable
        (the coroutine must be re-created on each attempt because a consumed
        coroutine cannot be re-awaited).

        Backoff: 0.5 → 1.0 → 2.0 → 4.0 s (total ~7.5 s before giving up).
        This keeps user interactions responsive while surviving heavy
        finalization bursts that hold the WAL writer lock.
        """
        import sqlite3
        last_exc = None
        for attempt in range(retries + 1):
            try:
                return await coro_factory()
            except sqlite3.OperationalError as exc:
                if "database is locked" not in str(exc):
                    raise
                last_exc = exc
                if attempt < retries:
                    delay = base_delay * (2 ** attempt)
                    logging.warning(
                        f"[DB-RETRY] database is locked (attempt {attempt + 1}/{retries + 1}), "
                        f"retrying in {delay:.1f}s"
                    )
                    await asyncio.sleep(delay)
        raise last_exc  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Shared sync connection helpers (batch performance)
    # ------------------------------------------------------------------
    @staticmethod
    def _apply_sync_pragmas(conn: Any) -> None:
        """Apply the same pragmas used by the async connection.

        Must be called on every new ``sqlite3.connect()`` so that sync
        writers share WAL mode and a generous busy-timeout with the async
        connection opened in ``initialize()``.
        """
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")  # WAL-safe: skip fsync per commit
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")      # Data integrity — match async connection
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-65536")    # 64 MB page cache (server-machine I/O reduction)
        conn.execute("PRAGMA mmap_size=8589934592")  # 8 GB — shared kernel page cache (see initialize())

    @contextmanager
    def _sync_conn(self):
        """Acquire a sync connection from the pool.

        Usage::

            with self._sync_conn() as conn:
                rows = conn.execute("SELECT ...").fetchall()

        If the current thread is inside a ``sync_batch()`` context, the
        batch connection is reused (no pool acquire/release) so that
        deferred-commit transactions stay on the same connection.
        """
        batch_conn = getattr(self, '_tls', None) and getattr(self._tls, '_batch_conn', None)
        if batch_conn is not None:
            yield batch_conn
            return
        if getattr(self, '_pool', None) is None:
            # Fallback for tests or pre-initialize usage
            import sqlite3
            conn = sqlite3.connect(self.db_path)  # type: ignore[arg-type]
            conn.row_factory = sqlite3.Row
            self._apply_sync_pragmas(conn)
            # Attach a history schema too so Group-2 (hot+history UNION) queries
            # work even for bare/pre-initialize connections (used by unit tests
            # that build their own db file and never call initialize()).
            # ':memory:' main DBs get a fresh ':memory:' history DB (always
            # empty, which is fine — these are test-only code paths).
            _hist_path = getattr(self, 'history_db_path', None) or (
                ":memory:" if self.db_path == ":memory:" else self._derive_history_db_path(self.db_path)  # type: ignore[arg-type]
            )
            try:
                conn.execute("ATTACH DATABASE ? AS history", (_hist_path,))
                # Schema-qualified — see attach_history_db() for why this is required.
                conn.execute("PRAGMA history.journal_mode=WAL")
                conn.execute("PRAGMA history.synchronous=NORMAL")
                _create_history_schema_sync(conn)
            except Exception as _e_attach:
                logging.debug(f"[DB] Fallback sync connection could not attach history schema: {_e_attach}")
            try:
                yield conn
            finally:
                conn.close()
            return
        pool = self._pool
        assert pool is not None, "Connection pool not initialized — call initialize() first"
        conn = pool.acquire()
        try:
            yield conn
        finally:
            pool.release(conn)

    def sync_conn(self):
        """Public alias for :meth:`_sync_conn` — use this from outside the class."""
        return self._sync_conn()

    def _should_commit(self) -> bool:
        """Return False when inside a ``sync_batch()`` with deferred commits."""
        return not getattr(self._tls, 'batch_deferred', False)

    # The only tables ever mirrored between `main` and `history` — kept as a single source
    # of truth so the schema-parity check (see check_hot_history_schema_parity_sync below)
    # and any script/test that needs the list never drift from each other independently of
    # the schemas themselves. Mirrors MIGRATED_TABLES in
    # qapbot/scripts/repair_history_schema_drift.py (that script predates this constant and
    # keeps its own copy deliberately, since it must run standalone against a DB with no
    # guarantee this module version is the one installed).
    _HOT_HISTORY_MIRRORED_TABLES: Tuple[str, ...] = (
        "war_attacks", "war_summary", "cwl_league_groups", "cwl_league_rounds",
    )

    def _explicit_column_list_sync(self, conn: Any, table: str) -> str:
        """Sync counterpart of `_explicit_column_list()` (see that method's docstring for the
        full 2026-08-14 hot/history column-order-drift incident writeup) — comma-joined column
        names for `table`, read from `main`'s own schema via `PRAGMA table_info`.

        Every sync query that reads BOTH `main.<table>` and `history.<table>` into one result
        set (a `UNION ALL` CTE, or an outer `SELECT col, col FROM (SELECT * FROM main.x UNION
        ALL SELECT * FROM history.x)`) MUST use this explicit list on both sides instead of a
        bare `SELECT *` — `UNION ALL SELECT *` matches columns by POSITION, not name, and
        `main.<table>`/`history.<table>` have identical column NAMES but a physically
        different on-disk column ORDER (separate `ALTER TABLE ADD COLUMN` histories). Naming
        each column explicitly is immune to that regardless of which schema's physical order
        it's read against, since SQLite resolves a named column reference by name, never by
        position — this is the read-side counterpart to the write-side fix already in place
        for the monthly migration itself.

        Both schemas are asserted (via `check_hot_history_schema_parity_sync`) to have the
        SAME SET of columns, just possibly reordered — so main's own column list is always a
        safe, complete explicit list to use against `history.<table>` too.
        """
        cur = conn.execute(f"PRAGMA main.table_info({table})")
        return ", ".join(row["name"] for row in cur.fetchall())

    def explicit_column_list_sync(self, conn: Any, table: str) -> str:
        """Public alias for `_explicit_column_list_sync` — same relationship as `sync_conn()`
        is to `_sync_conn()`. Other modules (QBhelperfunctions.py,
        QBdiscocmdshelper_admin_command.py) share this class's `sync_conn()` connection for
        their own hot/history UNION queries and need this too, not just db_manager.py's own
        methods."""
        return self._explicit_column_list_sync(conn, table)

    def check_hot_history_schema_parity_sync(self) -> List[str]:
        """Returns the names of any `_HOT_HISTORY_MIRRORED_TABLES` table whose `main` and
        `history` schemas currently disagree on column SET (added/removed column — a bug this
        code cannot safely work around) — empty list means fully healthy. Column order
        (rather than set) is explicitly NOT flagged here: `_explicit_column_list_sync`/
        `_explicit_column_list` name every column explicitly, so a pure reordering is already
        harmless and expected (that's exactly what the 2026-08-14 incident taught this
        project to design around, rather than trying to keep two independently-ALTERed
        schemas' physical order in lockstep forever).

        Intended to be called once at bot startup (logged loudly if non-empty — a genuine
        column SET mismatch is not automatically recoverable the way a reorder is) and by
        `tests/unit/test_hot_history_schema_parity.py` as a regression guardrail so a future
        migration that adds a column to one schema and not the other fails CI immediately
        instead of surfacing as a silently-wrong stat months later.
        """
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        mismatched: List[str] = []
        with self._sync_conn() as conn:
            for table in self._HOT_HISTORY_MIRRORED_TABLES:
                try:
                    main_cols = {row["name"] for row in conn.execute(f"PRAGMA main.table_info({table})").fetchall()}
                    history_cols = {row["name"] for row in conn.execute(f"PRAGMA history.table_info({table})").fetchall()}
                except sqlite3.Error as e:
                    logging.error(f"[DB-SCHEMA-CHECK] check_hot_history_schema_parity_sync failed for {table}: {e}")
                    continue
                if main_cols != history_cols:
                    mismatched.append(table)
        return mismatched

    def _sync_write_fence(self) -> None:
        """Block the calling thread until any in-progress sync bulk write finishes.

        Call via ``asyncio.to_thread(self._sync_write_fence)`` from async write
        helpers so that the aiosqlite write path doesn't collide with bulk war
        finalization batches that hold ``_sync_write_lock`` for ~10 s per batch.

        After this returns, the caller proceeds with its async write.  If a new
        sync batch starts in the tiny window between the fence and ``BEGIN``,
        SQLite's built-in ``busy_timeout`` provides a second layer of protection.
        """
        acquired = self._sync_write_lock.acquire(timeout=35)
        if acquired:
            self._sync_write_lock.release()
        # If we could not acquire within 35 s the sync writer is badly stuck;
        # proceed anyway — busy_timeout at the SQLite layer is the final guard.

    # ------------------------------------------------------------------
    # War write batch — P0 bulk DB writes + P1 checkpoint suppression
    # ------------------------------------------------------------------
    def war_write_batch(self, *, batch_size: int = 50):
        """Context manager that batches war data writes into fewer transactions.

        While active, ``add_war_data_sync()`` and ``update_war_data_sync()``
        collect their data in thread-local lists instead of writing immediately.
        On exit the pending data is flushed in batches of *batch_size* wars,
        each batch inside a single ``BEGIN … COMMIT`` transaction.

        **P0**: Reduces 691 individual commits to ~14 commits (batch_size=50).
        **P1**: Disables WAL auto-checkpoint during each batch and runs a
        non-blocking PASSIVE checkpoint after all batches complete.

        Callers may also collect ``(src, dst)`` file move tuples in
        ``pending_file_moves`` on the returned context object so that archive
        moves happen *after* the DB writes succeed.

        Usage::

            with CACHE.db_manager.war_write_batch() as batch:
                for clan_tag in clans:
                    manage_war_files(clan_tag, ...)
                # DB writes are flushed here on exit
            # batch.pending_file_moves is available for deferred os.replace
        """
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            if not self.db_path:
                raise RuntimeError("Database not initialized. Call initialize() first.")
            # Thread-local batch state
            self._tls.war_batch_appends = []   # (clan_tag, attack_rows, summary)
            self._tls.war_batch_updates = []   # (clan_tag, war_id, attack_rows, summary)
            self._tls.war_batch_active = True
            self._tls.pending_file_moves = []  # (src, dst) for deferred os.replace

            class _BatchHandle:
                """Thin handle so callers can read pending_file_moves."""
                def __init__(self, tls: Any) -> None:
                    self._tls = tls

                @property
                def pending_file_moves(self) -> list[tuple[str, str]]:
                    return self._tls.pending_file_moves

            handle = _BatchHandle(self._tls)
            try:
                yield handle
            finally:
                self._tls.war_batch_active = False
                _appends: List[Any] = self._tls.war_batch_appends  # type: ignore[assignment]
                _updates: List[Any] = self._tls.war_batch_updates  # type: ignore[assignment]
                # skip_checkpoint=True: don't block here waiting for I/O.
                # The caller (QapBot.py) fires run_passive_checkpoint() as a
                # background asyncio.create_task so Phase 3 starts immediately.
                self._flush_pending_war_writes(_appends, _updates, batch_size, skip_checkpoint=True)
                del self._tls.war_batch_appends
                del self._tls.war_batch_updates
                # pending_file_moves intentionally kept — caller processes them

        return _ctx()

    def _flush_pending_war_writes(
        self,
        appends: List[Any],
        updates: List[Any],
        batch_size: int,
        skip_checkpoint: bool = False,
    ) -> None:
        """Execute all collected war writes in batched transactions.

        Each batch of *batch_size* wars:
        1. Suppresses WAL auto-checkpoint (avoids random I/O mid-burst)
        2. Runs ``BEGIN`` → all INSERTs → ``COMMIT`` (one sync per batch)
        3. Restores auto-checkpoint

        After all batches: runs ``PRAGMA wal_checkpoint(PASSIVE)`` to
        merge WAL pages into the main DB without blocking readers,
        unless *skip_checkpoint* is True (caller owns the checkpoint).
        """
        import sqlite3

        total = len(appends) + len(updates)
        if total == 0:
            return

        with self._sync_conn() as conn:

            # ── Process APPENDS in batches ────────────────────────────
            for i in range(0, len(appends), batch_size):
                batch = appends[i : i + batch_size]
                with self._sync_write_lock:
                    conn.execute("PRAGMA wal_autocheckpoint=0")
                    try:
                        # Flat list of all attack row tuples across the batch
                        all_attack_params: List[Tuple[Any, ...]] = []
                        all_summary_params: List[Tuple[Any, ...]] = []
                        for clan_tag, attack_rows, summary in batch:
                            all_attack_params.extend(self._build_war_attack_params(clan_tag, attack_rows))
                            if summary:
                                all_summary_params.append((
                                    summary["war_id"], clan_tag,
                                    summary.get("opponent_tag", ""),
                                    summary.get("opponent_name", ""),
                                    summary.get("clan_stars", 0),
                                    summary.get("opponent_stars", 0),
                                    summary.get("clan_destruction", 0.0),
                                    summary.get("opp_destruction", 0.0),
                                    summary.get("team_size", 15),
                                    summary.get("attacks_per_member", 2),
                                    summary.get("war_type", "random"),
                                    1 if summary.get("is_cwl") else 0,
                                    summary.get("cwl_season", ""),
                                    summary.get("war_tag", ""),
                                    summary.get("end_time", ""),
                                    summary.get("state", ""),
                                    summary.get("result", ""),
                                    summary["date"],
                                    summary.get("clan_lineup_json", "[]"),
                                    summary.get("opp_lineup_json", "[]"),
                                    summary.get("clan_attacks_used", 0),
                                    summary.get("opp_attacks_used", 0),
                                    summary.get("round_number"),
                                ))

                        if all_attack_params:
                            conn.executemany(self._WAR_ATTACKS_INSERT_OR_IGNORE_SQL, all_attack_params)
                            self._upsert_player_name_index_in_conn(conn, all_attack_params)
                        if all_summary_params:
                            conn.executemany("""
                                INSERT OR REPLACE INTO war_summary
                                (war_id, clan_tag, opponent_tag, opponent_name, clan_stars,
                                 opponent_stars, clan_destruction, opp_destruction, team_size,
                                 attacks_per_member, war_type, is_cwl, cwl_season, war_tag,
                                 end_time, state,
                                 result, date,
                                 clan_lineup_json, opp_lineup_json,
                                 clan_attacks_used, opp_attacks_used, round_number)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, all_summary_params)
                            # Backfill cwl_season for CWL rows written with no season
                            # (happens when the war JSON had no league_group data).
                            # cwl_league_rounds is always written first (get_league_group call),
                            # so it serves as the authoritative season source.
                            _empty_ids = [p[0] for p in all_summary_params if p[11] == 1 and p[12] == ""]
                            if _empty_ids:
                                _ph = ",".join("?" * len(_empty_ids))
                                conn.execute(
                                    f"""
                                    UPDATE war_summary
                                    SET    cwl_season = (
                                               SELECT cwl_season FROM cwl_league_rounds
                                               WHERE  war_tag = war_summary.war_tag
                                           )
                                    WHERE  war_id IN ({_ph})
                                      AND  is_cwl = 1 AND cwl_season = '' AND war_tag != ''
                                      AND  EXISTS (
                                               SELECT 1 FROM cwl_league_rounds
                                               WHERE  war_tag = war_summary.war_tag
                                           )
                                    """,
                                    _empty_ids,
                                )
                        conn.commit()
                        logging.info(
                            f"[DB-BULK-WRITE] Flushed batch of {len(batch)} war appends "
                            f"({len(all_attack_params)} attack rows, {len(all_summary_params)} summaries)"
                        )
                    except sqlite3.Error:
                        conn.rollback()
                        raise
                    finally:
                        conn.execute("PRAGMA wal_autocheckpoint=1000")

            # ── Process UPDATES in batches ────────────────────────────
            for i in range(0, len(updates), batch_size):
                batch = updates[i : i + batch_size]
                with self._sync_write_lock:
                    conn.execute("PRAGMA wal_autocheckpoint=0")
                    try:
                        # Collect all delete keys and insert params across the batch
                        delete_keys: List[Tuple[str, str]] = []
                        all_attack_params: List[Tuple[Any, ...]] = []
                        all_summary_params: List[Tuple[Any, ...]] = []
                        for clan_tag, war_id, attack_rows, summary in batch:
                            if attack_rows:
                                delete_keys.append((war_id, clan_tag))
                                all_attack_params.extend(self._build_war_attack_params(clan_tag, attack_rows))
                            if summary:
                                all_summary_params.append((
                                    summary["war_id"], clan_tag,
                                    summary.get("opponent_tag", ""),
                                    summary.get("opponent_name", ""),
                                    summary.get("clan_stars", 0),
                                    summary.get("opponent_stars", 0),
                                    summary.get("clan_destruction", 0.0),
                                    summary.get("opp_destruction", 0.0),
                                    summary.get("team_size", 15),
                                    summary.get("attacks_per_member", 2),
                                    summary.get("war_type", "random"),
                                    1 if summary.get("is_cwl") else 0,
                                    summary.get("cwl_season", ""),
                                    summary.get("war_tag", ""),
                                    summary.get("end_time", ""),
                                    summary.get("state", ""),
                                    summary.get("result", ""),
                                    summary["date"],
                                    summary.get("clan_lineup_json", "[]"),
                                    summary.get("opp_lineup_json", "[]"),
                                    summary.get("clan_attacks_used", 0),
                                    summary.get("opp_attacks_used", 0),
                                    summary.get("round_number"),
                                ))
                        # Batch DELETE old attack rows, then bulk INSERT new ones
                        if delete_keys:
                            conn.executemany(
                                "DELETE FROM war_attacks WHERE war_id = ? AND clan_tag = ?",
                                delete_keys,
                            )
                        if all_attack_params:
                            # OR IGNORE: the DELETE above removes all rows for each
                            # (war_id, clan_tag) pair.  OR IGNORE guards against the rare
                            # case where the same (war_id, clan_tag) pair appears twice in
                            # the UPDATES batch (e.g. both a 3-part and a 4-part temp file
                            # for the same war trigger ARCHIVE-DIFFERS in one cycle).
                            # The second set of identical rows is silently dropped, which
                            # is correct since both sets contain the same attack data.
                            conn.executemany(self._WAR_ATTACKS_INSERT_OR_IGNORE_SQL, all_attack_params)
                            self._upsert_player_name_index_in_conn(conn, all_attack_params)
                        if all_summary_params:
                            conn.executemany("""
                                INSERT OR REPLACE INTO war_summary
                                (war_id, clan_tag, opponent_tag, opponent_name, clan_stars,
                                 opponent_stars, clan_destruction, opp_destruction, team_size,
                                 attacks_per_member, war_type, is_cwl, cwl_season, war_tag,
                                 end_time, state,
                                 result, date,
                                 clan_lineup_json, opp_lineup_json,
                                 clan_attacks_used, opp_attacks_used, round_number)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, all_summary_params)
                            # Backfill cwl_season for CWL rows written with no season.
                            _empty_ids = [p[0] for p in all_summary_params if p[11] == 1 and p[12] == ""]
                            if _empty_ids:
                                _ph = ",".join("?" * len(_empty_ids))
                                conn.execute(
                                    f"""
                                    UPDATE war_summary
                                    SET    cwl_season = (
                                               SELECT cwl_season FROM cwl_league_rounds
                                               WHERE  war_tag = war_summary.war_tag
                                           )
                                    WHERE  war_id IN ({_ph})
                                      AND  is_cwl = 1 AND cwl_season = '' AND war_tag != ''
                                      AND  EXISTS (
                                               SELECT 1 FROM cwl_league_rounds
                                               WHERE  war_tag = war_summary.war_tag
                                           )
                                    """,
                                    _empty_ids,
                                )
                        conn.commit()
                        logging.info(
                            f"[DB-BULK-UPDATE] Flushed batch of {len(batch)} war updates"
                        )
                    except sqlite3.Error:
                        conn.rollback()
                        raise
                    finally:
                        conn.execute("PRAGMA wal_autocheckpoint=1000")

            # Non-blocking checkpoint: merge WAL pages into main DB
            # while readers continue unimpeded.
            # Skipped when skip_checkpoint=True — caller fires checkpoint
            # as a background task so Phase 3 can start without waiting.
            if not skip_checkpoint:
                with self._sync_write_lock:
                    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def run_passive_checkpoint(self) -> None:
        """Run ``PRAGMA wal_checkpoint(PASSIVE)`` on the sync connection.

        Merges WAL pages into the main DB file without blocking readers.
        Safe to call from a background ``asyncio.to_thread`` task so that
        the event-loop is not stalled while the checkpoint is in progress.
        Callers that pass ``skip_checkpoint=True`` to ``flush_pending_war_writes``
        should fire this separately after critical-path work is done.
        """
        with self._sync_conn() as conn:
            logging.debug("[DB-CHECKPOINT] Starting passive WAL checkpoint...")
            with self._sync_write_lock:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            logging.debug("[DB-CHECKPOINT] Passive WAL checkpoint complete")

    def is_war_batch_active(self) -> bool:
        """Return True if the current thread is inside a ``war_write_batch``."""
        return getattr(self._tls, 'war_batch_active', False)

    def defer_file_move(self, src: str, dst: str) -> None:
        """Append a file move to the current batch's pending list.

        Only valid inside ``war_write_batch()``.  Falls back to
        immediate ``os.replace()`` if no batch is active.
        """
        if getattr(self._tls, 'war_batch_active', False):
            self._tls.pending_file_moves.append((src, dst))
        else:
            os.replace(src, dst)

    def activate_war_batch_on_thread(
        self,
        appends: List[Any],
        updates: List[Any],
        file_moves: List[Any],
    ) -> None:
        """Set war-batch state on the *current* thread.

        Used by callers that spin up worker threads via
        ``asyncio.to_thread()`` and need those workers to participate in a
        shared batch.  Each worker calls this on entry and
        ``deactivate_war_batch_on_thread()`` on exit.

        The three lists are **shared** across workers (safe only when
        workers are serialised, e.g. sequential ``await asyncio.to_thread``).
        """
        self._tls.war_batch_active = True
        self._tls.war_batch_appends = appends
        self._tls.war_batch_updates = updates
        self._tls.pending_file_moves = file_moves

    def deactivate_war_batch_on_thread(self) -> None:
        """Clear war-batch state on the current thread."""
        self._tls.war_batch_active = False

    def flush_pending_war_writes(
        self,
        appends: List[Any],
        updates: List[Any],
        batch_size: int = 50,
        skip_checkpoint: bool = False,
    ) -> None:
        """Public wrapper around ``_flush_pending_war_writes``."""
        self._flush_pending_war_writes(appends, updates, batch_size, skip_checkpoint=skip_checkpoint)

    def sync_batch(self, *, deferred_commit: bool = False):
        """
        Context manager for batch sync DB operations with optional deferred commits.

        When *deferred_commit* is ``True``, individual write methods skip
        their ``conn.commit()`` calls and a single commit is performed when
        the context exits.  **WARNING**: deferred mode holds the SQLite WAL
        writer lock for the entire batch, blocking all other connections
        from writing.  Only use for single-threaded workloads.

        The default (``False``) commits after each write.  With
        ``PRAGMA synchronous=NORMAL`` (set by ``_apply_sync_pragmas``),
        each commit is sub-millisecond (no fsync) so per-write commits are
        essentially free while releasing the WAL lock between writes.

        The connection is obtained from the connection pool.  On exit the
        connection is returned to the pool (or closed if draining).

        Usage::

            with CACHE.db_manager.sync_batch():
                # calls to add_war_data_sync, update_war_data_sync, etc.
                # all share one connection (within this thread)
        """
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            if not self.db_path:
                raise RuntimeError("Database not initialized. Call initialize() first.")
            conn = self._pool.acquire() if self._pool else None
            if conn is None:
                import sqlite3
                conn = sqlite3.connect(self.db_path)  # type: ignore[arg-type]
                conn.row_factory = sqlite3.Row
                self._apply_sync_pragmas(conn)
                _owns = True
            else:
                _owns = False
            self._tls._batch_conn = conn  # reuse for nested _sync_conn() calls
            self._tls.batch_deferred = deferred_commit
            try:
                yield conn
            finally:
                if deferred_commit:
                    with self._sync_write_lock:
                        conn.commit()
                self._tls.batch_deferred = False
                self._tls._batch_conn = None
                if _owns:
                    conn.close()
                elif self._pool:
                    self._pool.release(conn)
        return _ctx()

    @staticmethod
    def _derive_history_db_path(db_path: str) -> str:
        """Derive the default history DB path from the hot DB path.

        E.g. ``data/qapbot.db`` → ``data/qapbot_history.db``. Used when callers
        (e.g. tests) construct :class:`WarHistoryDB` and call ``initialize()``
        with a single path, keeping the hot/history split transparent to
        pre-existing callers.
        """
        base, ext = os.path.splitext(db_path)
        return f"{base}_history{ext or '.db'}"

    async def initialize(self, db_path: str, history_db_path: Optional[str] = None) -> None:
        """
        Initialize database connection and create schema.
        
        Sets up SQLite with server-machine-friendly pragmas:
        - WAL mode for concurrent reads
        - 5 second busy timeout for network latency
        - Memory-based temp storage for performance

        Also ATTACHes a second SQLite file as schema ``history`` (hot/history
        DB split): ``db_path`` (schema ``main``) always holds the current and
        previous calendar month; everything older lives in ``history_db_path``
        and is migrated there once a month by ``nightly_db_maintenance()``.
        
        Args:
            db_path: Path to SQLite database file (e.g., "data/qapbot.db")
            history_db_path: Path to the history SQLite database file. If not
                given, derived from ``db_path`` (e.g. "data/qapbot_history.db").
        
        Raises:
            RuntimeError: If already initialized
            ImportError: If aiosqlite is not installed
            aiosqlite.Error: If database connection or schema creation fails
        """
        if self._initialized:
            logging.warning("[DB] Database already initialized, skipping")
            return
        
        # Check if aiosqlite is available
        if aiosqlite is None:
            raise ImportError(
                "aiosqlite is not installed. Install it with: pip install aiosqlite"
            )
        
        self.db_path = db_path
        self.history_db_path = history_db_path or self._derive_history_db_path(db_path)
        # Ensure directories exist (history DB may live in a different dir)
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        os.makedirs(os.path.dirname(self.history_db_path) or ".", exist_ok=True)
        
        logging.info(f"[DB-INIT] Initializing database at {db_path} (history: {self.history_db_path})")
        
        try:
            # Create connection
            self.conn = await aiosqlite.connect(db_path)
            self._conn.row_factory = aiosqlite.Row  # Named column access: row["col"] instead of row[N]
            
            # Enable WAL mode (CRITICAL for server-machine reliability)
            await self._conn.execute("PRAGMA journal_mode=WAL")
            logging.info("[DB-INIT] Enabled WAL mode for server-machine reliability")
            
            # Set pragmas for performance and reliability
            await self._conn.execute("PRAGMA synchronous=NORMAL")  # Balance safety vs speed
            await self._conn.execute("PRAGMA busy_timeout=30000")   # Wait 30s for locks (server-machine + bulk-write bursts)
            await self._conn.execute("PRAGMA foreign_keys=ON")      # Data integrity
            await self._conn.execute("PRAGMA temp_store=MEMORY")    # Faster temp ops
            await self._conn.execute("PRAGMA cache_size=-65536")    # 64 MB page cache (server-machine I/O reduction)
            # Memory-mapped I/O: let the Linux kernel manage DB page caching in
            # the unified OS page cache instead of SQLite's per-connection heap
            # pool.  On HDD/server-machine this is critical — the kernel deduplicates pages
            # across all connections (async + sync workers) so the entire 10 GB
            # server-machine RAM can serve as a shared read cache, vs. 64 MB × N isolated
            # pools.  The 8 GB value is a virtual address reservation; physical
            # RAM is only consumed for actually-touched pages.
            await self._conn.execute("PRAGMA mmap_size=8589934592")  # 8 GB — kernel page cache for HDD seek reduction
            # Log effective mmap_size (may be capped by compile-time MAX_MMAP_SIZE)
            async with self._conn.execute("PRAGMA mmap_size") as _mmap_cur:
                _effective_mmap = (await _mmap_cur.fetchone())[0]
                _mmap_mb = _effective_mmap / 1024**2
                if _effective_mmap < 8589934592:
                    logging.info(f"[DB-INIT] mmap_size capped by SQLite build: {_mmap_mb:.0f} MB (requested 8192 MB)")
                else:
                    logging.info(f"[DB-INIT] mmap_size: {_mmap_mb:.0f} MB")

            # ATTACH the history database as schema 'history' (hot/history DB split)
            await self._conn.execute("ATTACH DATABASE ? AS history", (self.history_db_path,))
            # Schema-qualified pragmas — the unqualified PRAGMA journal_mode=WAL
            # above only applies to schema 'main' at the time it ran; it does NOT
            # retroactively cover a schema ATTACHed afterwards. Without these,
            # 'history' silently falls back to the default rollback journal,
            # forcing an fsync per commit — catastrophic on server-machine/external SATA
            # storage (root cause of the 2026-07 migration slowdown incident).
            await self._conn.execute("PRAGMA history.journal_mode=WAL")
            await self._conn.execute("PRAGMA history.synchronous=NORMAL")
            logging.info(f"[DB-INIT] Attached history database as schema 'history': {self.history_db_path}")
            
            # Create schema (idempotent) — creates both main.* and history.* tables
            await self._create_schema()
            
            # Create sync connection pool (bounded, pre-configured connections, each with 'history' attached)
            self._pool = _SyncConnectionPool(db_path, self._apply_sync_pragmas, pool_size=8, history_db_path=self.history_db_path)

            self._initialized = True
            logging.info("[DB-INIT] Database initialized successfully")

        except aiosqlite.Error as e:
            logging.error(f"[DB-INIT] Failed to initialize database: {e}")
            if self.conn:
                await self._conn.close()
                self.conn = None
            raise RuntimeError(f"Database initialization failed: {e}") from e
    
    async def _ensure_connection(self) -> None:
        """
        Ensure database connection is alive, reconnecting if necessary.
        
        Replaces the old ``if not self.conn: raise RuntimeError(...)`` guard.
        If the connection is ``None`` or a lightweight health check fails,
        ``_reconnect()`` is called automatically.
        
        Raises:
            RuntimeError: If db_path is not set (never initialized) or reconnect fails
        """
        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        
        if self.conn is None:
            # Don't auto-reconnect during maintenance — the DB was intentionally
            # closed and must stay closed so the data/ directory is safe to copy.
            _in_maintenance = False
            try:
                import QBcore as _qbcore
                _in_maintenance = _qbcore.maintenance_mode
            except ImportError:
                pass
            if _in_maintenance:
                raise RuntimeError(
                    "[DB-MAINT] Database closed for maintenance — aborting auto-reconnect"
                )
            logging.warning("[DB-RECONNECT] Connection is None, attempting reconnect...")
            await self._reconnect()
            return
        
        # Lightweight health check
        try:
            await self._conn.execute("SELECT 1")
        except Exception as e:
            logging.warning(f"[DB-RECONNECT] Health check failed ({e}), reconnecting...")
            await self._reconnect()
    
    async def _reconnect(self) -> None:
        """
        Re-establish database connection with the same pragmas as ``initialize()``.
        
        Closes any existing (broken) connection before opening a fresh one.
        Schema creation is NOT repeated — tables already exist.
        
        Raises:
            RuntimeError: If db_path is not set or reconnection fails
        """
        if not self.db_path:
            raise RuntimeError("Cannot reconnect: db_path not set")
        
        # Close stale handle if any
        if self.conn:
            try:
                await self._conn.close()
            except Exception:
                pass
            self.conn = None
        
        try:
            self.conn = await aiosqlite.connect(self.db_path)
            self._conn.row_factory = aiosqlite.Row  # Named column access: row["col"] instead of row[N]
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA synchronous=NORMAL")
            await self._conn.execute("PRAGMA busy_timeout=30000")    # Match initialize() — 30s for server-machine + bulk-write bursts
            await self._conn.execute("PRAGMA foreign_keys=ON")
            await self._conn.execute("PRAGMA temp_store=MEMORY")
            await self._conn.execute("PRAGMA cache_size=-65536")     # 64 MB page cache
            await self._conn.execute("PRAGMA mmap_size=8589934592")  # 8 GB — kernel page cache
            if self.history_db_path:
                await self._conn.execute("ATTACH DATABASE ? AS history", (self.history_db_path,))
                # Schema-qualified — see initialize() for why this is required.
                await self._conn.execute("PRAGMA history.journal_mode=WAL")
                await self._conn.execute("PRAGMA history.synchronous=NORMAL")
            logging.info("[DB-RECONNECT] Successfully reconnected to database")
        except aiosqlite.Error as e:
            logging.error(f"[DB-RECONNECT] Failed to reconnect: {e}")
            self.conn = None
            raise RuntimeError(f"Database reconnection failed: {e}") from e
    
    async def _create_index_if_missing(self, schema: str, index_name: str, table_name: str, index_sql: str) -> None:
        """Creates an index, logging before/after when it doesn't already exist so a slow
        first-time build on a multi-million-row table (war_attacks/war_summary) shows up as
        visible progress instead of a long silent gap.

        2026-08-14 incident: an operator watching a silent multi-minute gap during exactly
        this build (only ``idx_wa_player_tag_date`` had before/after logging; the other 8
        indices didn't) assumed the process had hung and sent SIGINT mid-build. That raced
        the still-running, uninterruptible background ``CREATE INDEX`` call against
        ``asyncio.run()``'s shutdown-time task cancellation, crashing the aiosqlite worker
        thread with "Event loop is closed" once the index finally finished building against
        an already-closed loop. See ``_close_bot_after_signal`` in QapBot.py for the other
        half of that fix (it now waits out ``QBcore.db_maintenance_mode`` instead of forcing
        a close after a fixed 60s). This function addresses the root cause: give the operator
        something to watch instead of a silent gap, so there's no reason to Ctrl+C at all.

        ``schema`` is ``""`` for main or ``"history."`` for the attached history DB — used to
        query the right sqlite_master for the existence check.
        """
        cur = await self._conn.execute(
            f"SELECT 1 FROM {schema}sqlite_master WHERE type='index' AND name=?", (index_name,)
        )
        row = await cur.fetchone()
        if row is not None:
            await self._conn.execute(index_sql)  # already exists — cheap IF NOT EXISTS no-op
            return

        logging.info(
            f"[DB-SCHEMA] Building {index_name} on {schema or 'main.'}{table_name} for the first "
            f"time — this can take minutes on a multi-million-row table..."
        )
        _t0 = _time.monotonic()
        await self._conn.execute(index_sql)
        logging.info(f"[DB-SCHEMA] {index_name} built in {_time.monotonic() - _t0:.1f}s")

    async def _create_schema(self) -> None:
        """
        Create database schema (idempotent).

        Creates war_attacks, war_summary and other tables if they don't exist.
        Safe to call multiple times (CREATE TABLE IF NOT EXISTS).
        """
        if not self.conn:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        import time as _time
        _t0 = _time.monotonic()
        logging.info("[DB-SCHEMA] Starting schema verification...")

        # ── war_attacks: per-attack rows ──
        logging.info("[DB-SCHEMA] Verifying war_attacks table + indexes...")
        await self._conn.execute("""
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
        """)
        await self._create_index_if_missing(
            "", "idx_wa_player_tag", "war_attacks",
            "CREATE INDEX IF NOT EXISTS idx_wa_player_tag ON war_attacks(player_tag)",
        )
        await self._create_index_if_missing(
            "", "idx_wa_war_clan", "war_attacks",
            "CREATE INDEX IF NOT EXISTS idx_wa_war_clan ON war_attacks(war_id, clan_tag)",
        )
        await self._create_index_if_missing(
            "", "idx_wa_clan_date", "war_attacks",
            "CREATE INDEX IF NOT EXISTS idx_wa_clan_date ON war_attacks(clan_tag, date)",
        )
        # Partial index on missed-attack rows (attack_order = 0). Used by
        # get_global_db_statistics_sync() to compute attacks_count cheaply:
        #   total_rows - COUNT(WHERE attack_order = 0)  →  no full-table scan.
        await self._create_index_if_missing(
            "", "idx_wa_zero_attacks", "war_attacks",
            "CREATE INDEX IF NOT EXISTS idx_wa_zero_attacks ON war_attacks(attack_order) WHERE attack_order = 0",
        )
        # Composite (player_tag, date) — get_player_attack_history_sync (leaderboard
        # scope="all") filters by both; without date in the index, SQLite would
        # rowid-fetch every historical row for that player_tag just to filter down to
        # one month. idx_wa_player_tag (above) is kept rather than dropped: DROP INDEX
        # on this multi-million-row table is slow enough that it must only run during
        # nightly maintenance, never on every connection/startup.
        #
        # Its FIRST build on a multi-million-row table is a full scan + sort that can
        # take minutes — safe here (2026-07-30) because QapBot.py's initialize_database()
        # now calls WarHistoryDB.initialize() strictly BEFORE CoC login and BEFORE
        # periodic_main() starts, with its own generous timeout (not the tight 60s
        # CoC-login one) and QBcore.db_maintenance_mode set — so nothing else can be
        # concurrently writing to the DB while this builds, and a slow first build no
        # longer shares a timeout budget with anything else. Every restart after the
        # first is a cheap IF-NOT-EXISTS check.
        await self._create_index_if_missing(
            "", "idx_wa_player_tag_date", "war_attacks",
            "CREATE INDEX IF NOT EXISTS idx_wa_player_tag_date ON war_attacks(player_tag, date)",
        )

        # ── war_summary: one row per war per actively tracked clan ──
        logging.info("[DB-SCHEMA] Verifying war_summary table + indexes...")
        await self._conn.execute("""
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
        """)
        await self._create_index_if_missing(
            "", "idx_ws_clan_tag", "war_summary",
            "CREATE INDEX IF NOT EXISTS idx_ws_clan_tag ON war_summary(clan_tag)",
        )
        await self._create_index_if_missing(
            "", "idx_ws_clan_date", "war_summary",
            "CREATE INDEX IF NOT EXISTS idx_ws_clan_date ON war_summary(clan_tag, date)",
        )
        await self._create_index_if_missing(
            "", "idx_ws_cwl_season", "war_summary",
            "CREATE INDEX IF NOT EXISTS idx_ws_cwl_season ON war_summary(clan_tag, cwl_season)",
        )
        await self._create_index_if_missing(
            "", "idx_ws_war_id", "war_summary",
            "CREATE INDEX IF NOT EXISTS idx_ws_war_id ON war_summary(war_id)",
        )

        # ── cwl_league_groups: 8-clan group membership per season ──────
        logging.info("[DB-SCHEMA] Verifying cwl_league_groups table...")
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS cwl_league_groups (
                league_group_id   TEXT    NOT NULL,
                cwl_season        TEXT    NOT NULL,
                clan_tag          TEXT    NOT NULL,
                league_rank       TEXT    DEFAULT NULL,
                cwl_ended         INTEGER NOT NULL DEFAULT 0,
                group_rank        INTEGER DEFAULT NULL,
                total_stars       INTEGER DEFAULT NULL,
                total_destruction REAL    DEFAULT NULL,
                PRIMARY KEY (cwl_season, clan_tag)
            )
        """)
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cwl_league_groups_id "
            "ON cwl_league_groups (league_group_id, cwl_season)"
        )

        # ── cwl_league_rounds: one row per CWL war (globally unique) ───
        logging.info("[DB-SCHEMA] Verifying cwl_league_rounds table...")
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS cwl_league_rounds (
                war_tag         TEXT    NOT NULL PRIMARY KEY,
                cwl_season      TEXT    NOT NULL,
                cwl_round       INTEGER NOT NULL,
                league_group_id TEXT    NOT NULL
            )
        """)

        # ── player_name_index: most-recent name per player_tag ───────────────
        # Maintained incrementally on every war write.  Loaded into CACHE at
        # startup for instant in-memory /whois name searches.
        logging.info("[DB-SCHEMA] Verifying player_name_index table...")
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS player_name_index (
                player_tag  TEXT PRIMARY KEY,
                player_name TEXT NOT NULL,
                last_seen   TEXT NOT NULL
            )
        """)

        # ── player_name_search / player_name_fts: SQLite-backed name search (2026-08-17,
        # CWL_PROD_PERFORMANCE_FIX_PLAN.md P2 Step 11) — replaces the in-memory
        # CACHE.player_name_index O(n) Python-side scan (millions of entries on PROD) with
        # SQLite-backed lookups, kept incrementally in sync by the same writers that maintain
        # player_name_index (_upsert_player_name_index_in_conn / update_player_name_index_sync).
        # Two tables, two different jobs:
        #   - player_name_search: a plain table (PK on player_tag) for the guest search's `#`
        #     tag-PREFIX mode — `player_tag LIKE ?||'%'` is index-backed on a PK's own B-tree,
        #     no FTS needed for a prefix match. name_lower is carried for consistency/future use
        #     but deliberately has no separate index: substring matching (what an index on
        #     name_lower could NOT accelerate anyway — LIKE '%needle%' can't use a b-tree index)
        #     is FTS5's job below, so a name_lower index here would just be write-cost with no
        #     matching read ever using it.
        #   - player_name_fts: FTS5 with the trigram tokenizer for actual name-SUBSTRING search
        #     (search_player_names, /whois) — feasibility-gate-confirmed available on both DEV
        #     (SQLite 3.50.4) and PROD (SQLite 3.45.2) via a live runtime probe over SSH, see this
        #     step's own status note in the plan doc. `player_tag UNINDEXED` (NOT the plan's own
        #     draft `UNPREFIXED`, which doesn't exist as an FTS5 column option — verified
        #     2026-08-17, `CREATE VIRTUAL TABLE ... fts5(player_tag UNPREFIXED, ...)` raises
        #     "unrecognized column option") excludes player_tag from the full-text index (we only
        #     ever search by name, never by tag substring) while still storing/returning it.
        #     Trigram tokenization requires >=3 characters to match anything at all (verified
        #     empirically) — search_player_names()'s own minimum-length guard accounts for this.
        logging.info("[DB-SCHEMA] Verifying player_name_search / player_name_fts tables...")
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS player_name_search (
                player_tag  TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                name_lower  TEXT NOT NULL
            )
        """)
        await self._conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS player_name_fts USING fts5(
                player_tag UNINDEXED, name, tokenize='trigram'
            )
        """)

        # Maindata tables
        logging.info("[DB-SCHEMA] Verifying maindata tables...")
        await self._create_maindata_schema()

        # Bot-level persistent metadata (key-value store for cross-restart state)
        logging.info("[DB-SCHEMA] Verifying bot metadata table...")
        await self._create_bot_metadata_schema()

        # Bot-level tester list (DM-testing allowlist, global — not per-guild)
        logging.info("[DB-SCHEMA] Verifying bot testers table...")
        await self._create_bot_testers_schema()

        # History schema (schema 'history', attached database) — hot/history DB split
        logging.info("[DB-SCHEMA] Verifying history.* tables + indexes...")
        await self._create_history_schema()

        await self._conn.commit()
        _elapsed = _time.monotonic() - _t0
        logging.info(f"[DB-SCHEMA] Schema verified in {_elapsed:.2f}s")

        await self._backfill_player_name_search_if_needed()

    async def _backfill_player_name_search_if_needed(self) -> None:
        """One-time idempotent backfill of player_name_search/player_name_fts from
        player_name_index (2026-08-17, CWL_PROD_PERFORMANCE_FIX_PLAN.md Step 11, Rule 12) —
        safe to run on every startup: guarded by a row-count comparison against
        player_name_index, so an already-backfilled DB (the normal case after the first
        startup post-migration) does two indexed COUNT(*) queries and nothing else. Counts stay
        in lockstep after the first backfill because every incremental writer
        (_upsert_player_name_index_in_conn / update_player_name_index_sync) updates all three
        tables together from then on — a mismatch should only ever be observed once, on the
        first startup after this migration ships.

        Also force-rebuilds regardless of the row-count match when
        PLAYER_NAME_FTS_ROWID_SCHEME_KEY isn't set to PLAYER_NAME_FTS_ROWID_SCHEME_VALUE yet
        (2026-08-17 follow-up fix) — the original backfill let SQLite auto-assign
        player_name_fts's rowid, independently of player_name_search's own rowid; the fix
        switched every writer to an explicit rowid derived from player_tag
        (_fts_rowid_for_tag), so any table populated by the old code has stale rowids that the
        new incremental-writer code can no longer address. One full rebuild after this fix
        ships repairs that; the marker then prevents repeating it on every future startup."""
        _rowid_scheme = await self.get_bot_metadata(PLAYER_NAME_FTS_ROWID_SCHEME_KEY)
        _needs_rowid_migration = _rowid_scheme != PLAYER_NAME_FTS_ROWID_SCHEME_VALUE

        async with self._conn.execute("SELECT COUNT(*) FROM player_name_index") as cur:
            source_count = (await cur.fetchone())[0]
        if source_count == 0:
            if _needs_rowid_migration:
                await self.set_bot_metadata(PLAYER_NAME_FTS_ROWID_SCHEME_KEY, PLAYER_NAME_FTS_ROWID_SCHEME_VALUE)
            return
        async with self._conn.execute("SELECT COUNT(*) FROM player_name_search") as cur:
            target_count = (await cur.fetchone())[0]
        if source_count == target_count and not _needs_rowid_migration:
            return

        logging.info(
            f"[DB-SCHEMA] Backfilling player_name_search/player_name_fts from player_name_index "
            f"({source_count:,} source rows, {target_count:,} already present"
            f"{', rowid scheme migration' if _needs_rowid_migration else ''})..."
        )
        _t0 = _time.monotonic()
        async with self._conn.execute("SELECT player_tag, player_name FROM player_name_index") as cur:
            rows = await cur.fetchall()
        pairs = [(row["player_tag"], row["player_name"]) for row in rows]
        await self._conn.execute("DELETE FROM player_name_search")
        await self._conn.execute("DELETE FROM player_name_fts")
        await self._conn.executemany(
            "INSERT INTO player_name_search (player_tag, name, name_lower) VALUES (?, ?, ?)",
            [(tag, name, name.lower()) for tag, name in pairs],
        )
        await self._conn.executemany(
            "INSERT INTO player_name_fts (rowid, player_tag, name) VALUES (?, ?, ?)",
            [(_fts_rowid_for_tag(tag), tag, name) for tag, name in pairs],
        )
        await self._conn.commit()
        await self.set_bot_metadata(PLAYER_NAME_FTS_ROWID_SCHEME_KEY, PLAYER_NAME_FTS_ROWID_SCHEME_VALUE)
        logging.info(
            f"[DB-SCHEMA] Backfilled {len(pairs):,} player_name_search/player_name_fts row(s) "
            f"in {_time.monotonic() - _t0:.2f}s"
        )

    async def _create_history_schema(self) -> None:
        """
        Create the history-DB schema (idempotent) on the attached ``history`` database.

        Mirrors the 4 time-series tables that participate in the hot/history
        split — ``war_attacks``, ``war_summary``, ``cwl_league_groups`` and
        ``cwl_league_rounds`` — plus their indexes, schema-qualified with
        ``history.``. Index names are reused verbatim: SQLite scopes index
        name uniqueness per attached database file, not across the whole
        connection, so there's no collision with the identically named
        indexes on ``main.*``.

        Safe to call multiple times (``CREATE TABLE/INDEX IF NOT EXISTS``).
        Other maindata tables (clans, users, guild_configs, etc.) are NOT
        mirrored here — those stay hot-only (small, not time-series).

        No-op (with a debug log) if no ``history`` schema is currently
        ATTACHed on this connection — this lets test helpers / callers that
        construct a bare connection and call ``_create_schema()`` directly
        (bypassing ``initialize()``'s ATTACH step) keep working unchanged.
        """
        if not self.conn:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        _attached = {row["name"] async for row in await self._conn.execute("PRAGMA database_list")}  # type: ignore[misc]
        if "history" not in _attached:
            logging.debug("[DB-SCHEMA] No 'history' schema attached — skipping history.* table creation")
            return

        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS history.war_attacks (
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
        """)
        await self._create_index_if_missing(
            "history.", "idx_wa_player_tag", "war_attacks",
            "CREATE INDEX IF NOT EXISTS history.idx_wa_player_tag ON war_attacks(player_tag)",
        )
        await self._create_index_if_missing(
            "history.", "idx_wa_war_clan", "war_attacks",
            "CREATE INDEX IF NOT EXISTS history.idx_wa_war_clan ON war_attacks(war_id, clan_tag)",
        )
        await self._create_index_if_missing(
            "history.", "idx_wa_clan_date", "war_attacks",
            "CREATE INDEX IF NOT EXISTS history.idx_wa_clan_date ON war_attacks(clan_tag, date)",
        )
        await self._create_index_if_missing(
            "history.", "idx_wa_zero_attacks", "war_attacks",
            "CREATE INDEX IF NOT EXISTS history.idx_wa_zero_attacks ON war_attacks(attack_order) WHERE attack_order = 0",
        )
        # See the matching comment in _create_schema() (main.war_attacks section) for
        # why building this here, inline, is safe: initialize_database() in QapBot.py
        # sequences WarHistoryDB.initialize() strictly before CoC login and
        # periodic_main(), so a slow first-time build here never races a concurrent
        # writer and never shares a timeout budget with CoC login. This is the big
        # table (history.war_attacks, potentially millions of rows) — the one most
        # likely to actually take minutes on its first build.
        await self._create_index_if_missing(
            "history.", "idx_wa_player_tag_date", "war_attacks",
            "CREATE INDEX IF NOT EXISTS history.idx_wa_player_tag_date ON war_attacks(player_tag, date)",
        )

        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS history.war_summary (
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
        """)
        await self._create_index_if_missing(
            "history.", "idx_ws_clan_tag", "war_summary",
            "CREATE INDEX IF NOT EXISTS history.idx_ws_clan_tag ON war_summary(clan_tag)",
        )
        await self._create_index_if_missing(
            "history.", "idx_ws_clan_date", "war_summary",
            "CREATE INDEX IF NOT EXISTS history.idx_ws_clan_date ON war_summary(clan_tag, date)",
        )
        await self._create_index_if_missing(
            "history.", "idx_ws_cwl_season", "war_summary",
            "CREATE INDEX IF NOT EXISTS history.idx_ws_cwl_season ON war_summary(clan_tag, cwl_season)",
        )
        await self._create_index_if_missing(
            "history.", "idx_ws_war_id", "war_summary",
            "CREATE INDEX IF NOT EXISTS history.idx_ws_war_id ON war_summary(war_id)",
        )

        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS history.cwl_league_groups (
                league_group_id   TEXT    NOT NULL,
                cwl_season        TEXT    NOT NULL,
                clan_tag          TEXT    NOT NULL,
                league_rank       TEXT    DEFAULT NULL,
                cwl_ended         INTEGER NOT NULL DEFAULT 0,
                group_rank        INTEGER DEFAULT NULL,
                total_stars       INTEGER DEFAULT NULL,
                total_destruction REAL    DEFAULT NULL,
                PRIMARY KEY (cwl_season, clan_tag)
            )
        """)
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS history.idx_cwl_league_groups_id "
            "ON cwl_league_groups (league_group_id, cwl_season)"
        )

        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS history.cwl_league_rounds (
                war_tag         TEXT    NOT NULL PRIMARY KEY,
                cwl_season      TEXT    NOT NULL,
                cwl_round       INTEGER NOT NULL,
                league_group_id TEXT    NOT NULL
            )
        """)

    async def _create_maindata_schema(self) -> None:
        """Create maindata tables (idempotent — safe to call on new and existing databases)."""
        if not self.conn:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        # Clans table
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS clans (
                clan_tag TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                has_active_subscriptions BOOLEAN NOT NULL DEFAULT 0,
                last_war_update TEXT,
                warlog_is_public BOOLEAN NOT NULL DEFAULT 1,
                last_checked_via_api TEXT,
                war_league TEXT,
                track_war_updates BOOLEAN NOT NULL DEFAULT 1,
                is_deleted BOOLEAN NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_clans_has_subs ON clans(has_active_subscriptions)"
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_clans_last_war_update ON clans(last_war_update)"
        )
        
        # Clan families table
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS clan_families (
                family_tag TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                owned_by_guild TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        
        # Clan family members
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS clan_family_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                family_tag TEXT NOT NULL,
                clan_tag TEXT NOT NULL,
                added_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (family_tag) REFERENCES clan_families(family_tag) ON DELETE CASCADE,
                FOREIGN KEY (clan_tag) REFERENCES clans(clan_tag) ON DELETE CASCADE,
                UNIQUE (family_tag, clan_tag)
            )
        """)
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_family_members_family_tag ON clan_family_members(family_tag)"
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_family_members_clan_tag ON clan_family_members(clan_tag)"
        )
        
        # Users table
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                discord_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                notification_mode TEXT NOT NULL DEFAULT 'repeated',
                notification_type TEXT NOT NULL DEFAULT 'all_wars',
                hours_before_end INTEGER NOT NULL DEFAULT 4,
                war_reminders_enabled BOOLEAN NOT NULL DEFAULT 1,
                user_language TEXT,
                user_language_locked BOOLEAN NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                CHECK (notification_mode IN ('repeated', 'once')),
                    CHECK (notification_type IN ('all_wars', 'cwl_only')),
                CHECK (hours_before_end BETWEEN 0 AND 24)
            )
        """)
        
        # User players table
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS user_players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id TEXT NOT NULL,
                player_tag TEXT NOT NULL,
                player_name TEXT NOT NULL,
                verified BOOLEAN NOT NULL DEFAULT 0,
                th_level INTEGER,
                current_clan_tag TEXT,
                is_primary BOOLEAN NOT NULL DEFAULT 0,
                added_at TEXT NOT NULL DEFAULT (datetime('now')),
                cwl_permanent_optout INTEGER NOT NULL DEFAULT 0,
                cwl_default_preferred_league_rank TEXT,
                FOREIGN KEY (discord_id) REFERENCES users(discord_id) ON DELETE CASCADE,
                FOREIGN KEY (current_clan_tag) REFERENCES clans(clan_tag) ON DELETE SET NULL,
                UNIQUE (discord_id, player_tag)
            )
        """)
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_players_discord_id ON user_players(discord_id)"
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_players_player_tag ON user_players(player_tag)"
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_players_clan_tag ON user_players(current_clan_tag)"
        )

        # User buddies table (Save-your-Buddy feature: list of watched CoC player tags per watcher)
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS user_buddies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id TEXT NOT NULL,
                player_tag TEXT NOT NULL,
                player_name TEXT NOT NULL,
                added_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (discord_id) REFERENCES users(discord_id) ON DELETE CASCADE,
                UNIQUE (discord_id, player_tag)
            )
        """)
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_buddies_discord_id ON user_buddies(discord_id)"
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_buddies_player_tag ON user_buddies(player_tag)"
        )

        # Guild configuration table
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id TEXT PRIMARY KEY,
                language TEXT NOT NULL DEFAULT 'en',
                newbie_role_id TEXT,
                member_role_id TEXT,
                role_system_enabled BOOLEAN NOT NULL DEFAULT 0,
                registration_channel_id TEXT,
                war_notification_channel_id TEXT,
                registration_message_enabled BOOLEAN NOT NULL DEFAULT 0,
                registration_message_id TEXT,
                registration_message_last_bump_iso TEXT,
                channel_war_notifications_enabled BOOLEAN NOT NULL DEFAULT 0,
                war_notification_threshold_hours REAL NOT NULL DEFAULT 2.0,
                coc_role_enabled BOOLEAN NOT NULL DEFAULT 0,
                clan_role_enabled BOOLEAN NOT NULL DEFAULT 0,
                coc_role_member_id TEXT,
                coc_role_elder_id TEXT,
                coc_role_coleader_id TEXT,
                coc_role_leader_id TEXT,
                welcome_message_enabled BOOLEAN NOT NULL DEFAULT 0,
                welcome_message_mode TEXT NOT NULL DEFAULT 'clan_link',
                welcome_apply_channel_id TEXT,
                welcome_clan_tag TEXT,
                cwl_hub_channel_id TEXT,
                cwl_hub_message_id TEXT,
                cwl_hub_message_enabled BOOLEAN NOT NULL DEFAULT 0,
                cwl_hub_message_last_bump_iso TEXT,
                cwl_management_channel_id TEXT,
                cwl_management_message_id TEXT,
                cwl_management_message_enabled BOOLEAN NOT NULL DEFAULT 0,
                cwl_management_message_last_bump_iso TEXT,
                cwl_retention_months INTEGER NOT NULL DEFAULT 0,
                cwl_selected_season TEXT,
                cwl_enrollment_include_all_linked_accounts BOOLEAN NOT NULL DEFAULT 0,
                timezone_name TEXT NOT NULL DEFAULT 'UTC',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

        # Guild member families
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_member_families (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                family_tag TEXT NOT NULL,
                FOREIGN KEY (guild_id) REFERENCES guild_config(guild_id) ON DELETE CASCADE,
                FOREIGN KEY (family_tag) REFERENCES clan_families(family_tag) ON DELETE CASCADE,
                UNIQUE (guild_id, family_tag)
            )
        """)
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_guild_member_families_guild_id ON guild_member_families(guild_id)"
        )
        
        # Guild member clans
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_member_clans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                clan_tag TEXT NOT NULL,
                FOREIGN KEY (guild_id) REFERENCES guild_config(guild_id) ON DELETE CASCADE,
                FOREIGN KEY (clan_tag) REFERENCES clans(clan_tag) ON DELETE CASCADE,
                UNIQUE (guild_id, clan_tag)
            )
        """)
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_guild_member_clans_guild_id ON guild_member_clans(guild_id)"
        )

        # CWL roster planning: one row per guild x season planning campaign.
        # Hot DB only, no history-DB mirroring — short-lived per-season operational data with a
        # configurable retention purge (guild_config.cwl_retention_months), see nightly_db_maintenance().
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS cwl_events (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id              TEXT    NOT NULL,
                cwl_season            TEXT    NOT NULL,
                status                TEXT    NOT NULL DEFAULT 'draft',
                signup_deadline_at    TEXT,
                template_season       TEXT,
                created_by_discord_id TEXT    NOT NULL,
                created_at            TEXT    NOT NULL DEFAULT (datetime('now')),
                updated_at            TEXT    NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (guild_id) REFERENCES guild_config(guild_id) ON DELETE CASCADE,
                UNIQUE (guild_id, cwl_season)
            )
        """)
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cwl_events_guild_status ON cwl_events(guild_id, status)"
        )

        # CWL roster planning: which member-clans participate in an event + their tier/roster size.
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS cwl_event_clans (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id            INTEGER NOT NULL,
                clan_tag            TEXT    NOT NULL,
                target_league_rank  TEXT,
                roster_size         INTEGER NOT NULL DEFAULT 15,
                tier_order          INTEGER NOT NULL DEFAULT 0,
                cwl_start_at        TEXT,
                -- Deactivating a clan (unchecking it in the UI) must NOT delete this row —
                -- roster_size/cwl_start_at/target_league_rank need to survive a
                -- deactivate-then-reactivate cycle, so "participating" is an explicit column
                -- rather than "row exists" being the implicit signal (that was the original
                -- design and lost data on every toggle-off; see CWL_CLAN_CONFIG_ACTIVITY_PLAN.md).
                participating       INTEGER NOT NULL DEFAULT 1,
                locked_at           TEXT,
                created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (event_id) REFERENCES cwl_events(id) ON DELETE CASCADE,
                FOREIGN KEY (clan_tag) REFERENCES clans(clan_tag) ON DELETE CASCADE,
                UNIQUE (event_id, clan_tag)
            )
        """)
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cwl_event_clans_event ON cwl_event_clans(event_id)"
        )

        # CWL roster planning: one row per player per event (sign-up/template-response tracking).
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS cwl_signups (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id              INTEGER NOT NULL,
                player_tag            TEXT    NOT NULL,
                player_name           TEXT,
                discord_id            TEXT,
                preferred_league_rank TEXT,
                source                TEXT    NOT NULL,
                status                TEXT    NOT NULL DEFAULT 'pending',
                responded_at          TEXT,
                created_at            TEXT    NOT NULL DEFAULT (datetime('now')),
                -- Cross-guild shared-clan foreign-guest tracking (2026-08-15, project owner's
                -- spec): set when a REAL member of some other guild's shared clan gets manually
                -- cross-assigned into one of THIS guild's own (non-shared) clans, and that shared
                -- clan is later detached from this guild — at that point they "become a guest
                -- player automatically." NULL for every ordinary signup (the overwhelming
                -- majority). Not an FK — the origin shared_clan row may itself be pruned later
                -- (e.g. once no guild is left attached to it) without needing to touch every
                -- guest row that ever pointed at it; find_cwl_signups_by_origin_shared_clan_sync
                -- callers already tolerate a dangling id by simply finding nothing to purge.
                origin_shared_clan_id INTEGER,
                FOREIGN KEY (event_id) REFERENCES cwl_events(id) ON DELETE CASCADE,
                UNIQUE (event_id, player_tag)
            )
        """)
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cwl_signups_event ON cwl_signups(event_id)"
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cwl_signups_discord ON cwl_signups(discord_id)"
        )
        # idx_cwl_signups_origin_shared_clan is created later, in the ALTER-TABLE migration block
        # below (_add_column_if_missing("cwl_signups", "origin_shared_clan_id", ...)) — NOT here.
        # This CREATE TABLE IF NOT EXISTS is a no-op against a pre-existing table (the common
        # case on any already-initialized DEV/PROD database), so on such a database the
        # origin_shared_clan_id column genuinely does not exist yet at this point in schema
        # verification — an index on it here would fail with "no such column" and abort the
        # ENTIRE database initialization, which is exactly the startup crash this comment is
        # warning future editors away from reintroducing (2026-08-15 incident, live-tested).

        # CWL roster planning: the actual/suggested player -> clan mapping for an event.
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS cwl_assignments (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id              INTEGER NOT NULL,
                player_tag            TEXT    NOT NULL,
                assigned_clan_tag     TEXT    NOT NULL,
                suggested_clan_tag    TEXT,
                assignment_source     TEXT    NOT NULL DEFAULT 'suggested',
                score                 REAL,
                score_breakdown_json  TEXT,
                locked                BOOLEAN NOT NULL DEFAULT 0,
                notified              BOOLEAN NOT NULL DEFAULT 0,
                switched_at           TEXT,
                alarm_stage_sent      INTEGER NOT NULL DEFAULT 0,
                updated_at            TEXT    NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (event_id) REFERENCES cwl_events(id) ON DELETE CASCADE,
                FOREIGN KEY (assigned_clan_tag) REFERENCES clans(clan_tag) ON DELETE CASCADE,
                UNIQUE (event_id, player_tag)
            )
        """)
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cwl_assignments_event ON cwl_assignments(event_id)"
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cwl_assignments_clan ON cwl_assignments(event_id, assigned_clan_tag)"
        )

        # Cross-guild shared CWL clans (2026-08-15, project owner's spec): a real-world clan can
        # only actually play CWL for one roster, but nothing stops two independent guilds from
        # each configuring the same clan_tag into their own separate cwl_events for the same
        # season — this is the reconciliation layer for that. Deliberately its own tables, never
        # redirecting cwl_signups/cwl_assignments/cwl_event_clans themselves (those stay scoped
        # to one guild's whole event; redirecting per-clan inside them would mean every write
        # path branches per-player on "which clan is this," and a bug there could leak/corrupt a
        # *different* guild's private roster through tables every non-shared guild depends on).
        # See CWL_ROSTER_PLANNING_PLAN.md for the full design.
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS cwl_shared_clans (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                clan_tag                TEXT    NOT NULL,
                cwl_season              TEXT    NOT NULL,
                owner_guild_id          TEXT    NOT NULL,
                owner_event_id          INTEGER NOT NULL,
                owner_resolution_method TEXT    NOT NULL,
                owner_resolved_at       TEXT,
                created_at              TEXT    NOT NULL DEFAULT (datetime('now')),
                updated_at              TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE (clan_tag, cwl_season)
            )
        """)
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cwl_shared_clans_season_tag ON cwl_shared_clans(cwl_season, clan_tag)"
        )

        # Membership list — one row per guild currently participating in a shared clan (owner
        # included). Used for eviction and "who's affected" lookups (notifications, the
        # delete-season guard's repoint-or-prune decision).
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS cwl_shared_clan_guilds (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                shared_clan_id INTEGER NOT NULL,
                guild_id       TEXT    NOT NULL,
                event_id       INTEGER NOT NULL,
                joined_at      TEXT    NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (shared_clan_id) REFERENCES cwl_shared_clans(id) ON DELETE CASCADE,
                FOREIGN KEY (event_id) REFERENCES cwl_events(id) ON DELETE CASCADE,
                UNIQUE (shared_clan_id, guild_id)
            )
        """)
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cwl_shared_clan_guilds_shared_clan ON cwl_shared_clan_guilds(shared_clan_id)"
        )

        # The shared roster itself — mirrors cwl_signups' shape but keyed by shared_clan_id, not
        # event_id, so it's visible identically from every attached guild's board.
        #
        # `status` and `assigned` are DELIBERATELY separate columns, never conflated (2026-08-16,
        # live-testing feedback, project owner's spec, verbatim: "Confirmation status and
        # assignment status should be treated completely separate. the one has a totally
        # different meaning logically than the other! ... The symbols in the player tile should
        # exclusively reflect confirmation status. The assignment status is obvious to the user
        # from the column the player tile appears in."):
        #   - `status` (pending/confirmed/declined/withdrawn) is PURELY the player's own genuine
        #     response — set ONLY by set_cwl_shared_clan_player_status_sync (db_manager.py), which
        #     never touches `assigned`. Nothing an assignment/placement decision does may ever
        #     alter it, for the same reason auto-assigning a player into a clan on the "Manage
        #     Enrollment" board never flips their `cwl_signups.status` to 'confirmed' either.
        #   - `assigned` (0/1) is PURELY "is this player currently placed in THIS clan's column" —
        #     set ONLY by set_cwl_shared_clan_player_assignment_sync, which never touches `status`.
        #     Drag-and-drop and the auto-assign seed both write only this column.
        # The original design ("one clan per shared_clan_id, so status='confirmed' IS the
        # assignment — no separate assignments table needed") conflated the two into one column,
        # which is exactly what caused every auto-assigned player in a freshly-added shared clan
        # to show the ✓ Confirmed badge despite nobody having actually responded — a machine
        # guess and a real human confirmation are not the same fact and must never share storage.
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS cwl_shared_clan_players (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                shared_clan_id    INTEGER NOT NULL,
                player_tag        TEXT    NOT NULL,
                player_name       TEXT,
                discord_id        TEXT,
                status            TEXT    NOT NULL DEFAULT 'pending',
                assigned          INTEGER NOT NULL DEFAULT 0,
                source            TEXT    NOT NULL,
                added_by_guild_id TEXT    NOT NULL,
                responded_at      TEXT,
                updated_at        TEXT    NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (shared_clan_id) REFERENCES cwl_shared_clans(id) ON DELETE CASCADE,
                UNIQUE (shared_clan_id, player_tag)
            )
        """)
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cwl_shared_clan_players_shared_clan ON cwl_shared_clan_players(shared_clan_id)"
        )

        # Guild welcome-message family selections (clan-link mode, multi-select, per-family toggle)
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_welcome_families (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                family_tag TEXT NOT NULL,
                FOREIGN KEY (guild_id) REFERENCES guild_config(guild_id) ON DELETE CASCADE,
                FOREIGN KEY (family_tag) REFERENCES clan_families(family_tag) ON DELETE CASCADE,
                UNIQUE (guild_id, family_tag)
            )
        """)
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_guild_welcome_families_guild_id ON guild_welcome_families(guild_id)"
        )

        # Guild welcome-message individual clan selections (clan-link mode, multi-select)
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_welcome_clans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                clan_tag TEXT NOT NULL,
                FOREIGN KEY (guild_id) REFERENCES guild_config(guild_id) ON DELETE CASCADE,
                FOREIGN KEY (clan_tag) REFERENCES clans(clan_tag) ON DELETE CASCADE,
                UNIQUE (guild_id, clan_tag)
            )
        """)
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_guild_welcome_clans_guild_id ON guild_welcome_clans(guild_id)"
        )

        # Per-clan Discord role IDs per guild (populated when clan_role_enabled)
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_clan_roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                clan_tag TEXT NOT NULL,
                role_id TEXT NOT NULL,
                FOREIGN KEY (guild_id) REFERENCES guild_config(guild_id) ON DELETE CASCADE,
                UNIQUE (guild_id, clan_tag)
            )
        """)
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_guild_clan_roles_guild_id ON guild_clan_roles(guild_id)"
        )

        # Subscriptions table
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                clan_tag TEXT NOT NULL,
                subscription_type TEXT NOT NULL,
                year TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (guild_id, channel_id, clan_tag, subscription_type, year)
            )
        """)
        # Note: No FK constraint - clan_tag stores both clan AND family tags
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_subscriptions_guild_id ON subscriptions(guild_id)"
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_subscriptions_channel_id ON subscriptions(channel_id)"
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_subscriptions_clan_tag ON subscriptions(clan_tag)"
        )
        
        # Notification state table
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS notification_state (
                war_key TEXT NOT NULL,
                player_tag TEXT NOT NULL,
                player_name TEXT NOT NULL,
                discord_id TEXT NOT NULL,
                notification_time TEXT NOT NULL,
                attacks_remaining INTEGER NOT NULL,
                PRIMARY KEY (war_key, player_tag),
                FOREIGN KEY (discord_id) REFERENCES users(discord_id) ON DELETE CASCADE
            )
        """)
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_notification_state_war_key ON notification_state(war_key)"
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_notification_state_discord_id ON notification_state(discord_id)"
        )
        
        # Channel notification state table (tracks per-guild channel notifications)
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS channel_notification_state (
                war_key TEXT NOT NULL,
                guild_id TEXT NOT NULL,
                notification_time TEXT NOT NULL,
                clan_name TEXT NOT NULL,
                opponent_name TEXT NOT NULL,
                PRIMARY KEY (war_key, guild_id)
            )
        """)
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_channel_notification_state_war_key ON channel_notification_state(war_key)"
        )
        
        # Leaderboard messages table
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS leaderboard_messages (
                message_key TEXT PRIMARY KEY,
                clan_tag TEXT,
                channel_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                message_ids TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_leaderboard_messages_clan_tag ON leaderboard_messages(clan_tag)"
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_leaderboard_messages_channel_id ON leaderboard_messages(channel_id)"
        )

        # --- Temporary migration block: CWL roster planning columns on pre-existing DBs ---
        # The CREATE TABLE statements above already bake these columns in for fresh installs;
        # this block only matters for databases created before this feature shipped. Remove once
        # dev and prod are both confirmed migrated, per the established convention (see
        # changelog "Removed all one-time migration code from db_manager.py startup routines").
        await self._add_column_if_missing("user_players", "cwl_permanent_optout", "INTEGER NOT NULL DEFAULT 0")
        await self._add_column_if_missing("user_players", "cwl_default_preferred_league_rank", "TEXT")
        await self._add_column_if_missing("guild_config", "cwl_hub_channel_id", "TEXT")
        await self._add_column_if_missing("guild_config", "cwl_hub_message_id", "TEXT")
        await self._add_column_if_missing("guild_config", "cwl_hub_message_enabled", "BOOLEAN NOT NULL DEFAULT 0")
        await self._add_column_if_missing("guild_config", "cwl_hub_message_last_bump_iso", "TEXT")
        await self._add_column_if_missing("guild_config", "cwl_management_channel_id", "TEXT")
        await self._add_column_if_missing("guild_config", "cwl_management_message_id", "TEXT")
        await self._add_column_if_missing("guild_config", "cwl_management_message_enabled", "BOOLEAN NOT NULL DEFAULT 0")
        await self._add_column_if_missing("guild_config", "cwl_management_message_last_bump_iso", "TEXT")
        await self._add_column_if_missing("guild_config", "cwl_retention_months", "INTEGER NOT NULL DEFAULT 0")
        await self._add_column_if_missing("guild_config", "cwl_selected_season", "TEXT")
        await self._add_column_if_missing("guild_config", "cwl_enrollment_include_all_linked_accounts", "BOOLEAN NOT NULL DEFAULT 0")
        await self._add_column_if_missing("guild_config", "timezone_name", "TEXT NOT NULL DEFAULT 'UTC'")
        await self._add_column_if_missing("cwl_event_clans", "participating", "INTEGER NOT NULL DEFAULT 1")
        await self._add_column_if_missing("cwl_signups", "origin_shared_clan_id", "INTEGER")
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cwl_signups_origin_shared_clan "
            "ON cwl_signups(origin_shared_clan_id, player_tag) WHERE origin_shared_clan_id IS NOT NULL"
        )
        # `assigned` split out of `status` (2026-08-16, live-testing feedback — see the
        # cwl_shared_clan_players CREATE TABLE comment above for the full rationale). A one-time
        # backfill (gated on _add_column_if_missing actually having just added the column, never
        # re-run on an already-migrated DB) preserves every currently-placed player's column under
        # the OLD combined meaning — without it, every player the old status='confirmed' had
        # placed into a clan's column would silently vanish from it the moment this ships.
        assigned_column_added = await self._add_column_if_missing(
            "cwl_shared_clan_players", "assigned", "INTEGER NOT NULL DEFAULT 0"
        )
        if assigned_column_added:
            await self._conn.execute("UPDATE cwl_shared_clan_players SET assigned = 1 WHERE status = 'confirmed'")
            logging.info("[DB-MIGRATE] Backfilled cwl_shared_clan_players.assigned from legacy status='confirmed' rows")

        logging.debug("[DB-SCHEMA] Maindata schema created/verified")

    async def _add_column_if_missing(self, table: str, column: str, ddl_type: str) -> bool:
        """Idempotently ALTER TABLE ADD COLUMN for pre-existing databases (SQLite has no
        ADD COLUMN IF NOT EXISTS). Checks PRAGMA table_info first since re-running ALTER TABLE
        on a column that already exists raises. Part of the temporary migration block above —
        remove alongside it once dev/prod are both migrated. Returns True only when the column
        was actually just added (a fresh migration, not a no-op against an already-migrated DB) —
        callers with a one-time backfill that must never re-run should gate on this."""
        cursor = await self._conn.execute(f"PRAGMA table_info({table})")
        existing_columns = {row[1] async for row in cursor}
        if column not in existing_columns:
            await self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
            logging.info(f"[DB-MIGRATE] Added column {table}.{column}")
            return True
        return False

    async def _create_bot_metadata_schema(self) -> None:
        """Create the bot_metadata key-value table (idempotent)."""
        await self._ensure_connection()
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_metadata (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        await self._conn.commit()
        logging.debug("[DB-SCHEMA] bot_metadata table verified")

    async def get_bot_metadata(self, key: str) -> Optional[str]:
        """Return the stored value for *key*, or None if not set."""
        await self._ensure_connection()
        row = await (await self._conn.execute(
            "SELECT value FROM bot_metadata WHERE key = ?", (key,)
        )).fetchone()
        return row["value"] if row else None

    async def set_bot_metadata(self, key: str, value: str) -> None:
        """Upsert *key* → *value* in bot_metadata."""
        await self._ensure_connection()
        async with self._write_lock:
            await self._conn.execute(
                "INSERT INTO bot_metadata (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            await self._conn.commit()

    async def _create_bot_testers_schema(self) -> None:
        """Create the bot_testers table (idempotent) — bot-wide (not per-guild) list of Discord
        user IDs who receive DMs alongside CONFIG.server_admin while a feature is under the
        cwl_dm_restrict_to_admin-style live-testing guard. See CACHE.testers/add_tester/
        remove_tester (cache_manager.py) for the in-memory side."""
        await self._ensure_connection()
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_testers (
                discord_id TEXT PRIMARY KEY,
                added_at   TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await self._conn.commit()
        logging.debug("[DB-SCHEMA] bot_testers table verified")

    async def get_testers(self) -> List[str]:
        """Return every tester's Discord user ID."""
        await self._ensure_connection()
        cursor = await self._conn.execute("SELECT discord_id FROM bot_testers")
        return [row["discord_id"] for row in await cursor.fetchall()]

    async def add_tester(self, discord_id: str) -> None:
        """Enroll a Discord user ID as a tester (no-op if already enrolled)."""
        await self._ensure_connection()
        async with self._write_lock:
            await self._conn.execute(
                "INSERT OR IGNORE INTO bot_testers (discord_id) VALUES (?)", (discord_id,)
            )
            await self._conn.commit()

    async def remove_tester(self, discord_id: str) -> None:
        """Remove a Discord user ID from the tester list (no-op if not enrolled)."""
        await self._ensure_connection()
        async with self._write_lock:
            await self._conn.execute("DELETE FROM bot_testers WHERE discord_id = ?", (discord_id,))
            await self._conn.commit()

    async def _ensure_clan_exists(self, clan_tag: str) -> None:
        """
        Ensure clan exists in database before saving child records.
        
        If clan doesn't exist in database, saves it from CACHE.clan_name_cache.
        Validates clan_tag format to skip malformed data (e.g., user IDs, channel IDs).
        Valid tags: Start with # and are exactly 9-10 characters total.
        
        Args:
            clan_tag: Clan tag to ensure exists
        """
        await self._ensure_connection()
        assert self.conn is not None  # _ensure_connection() guarantees this
        
        # Validate clan_tag format (skip malformed data like channel IDs, user IDs).
        # CoC clan tags: '#' + 3-9 alphanumeric chars = 4-10 chars total.
        # Use a generous range (5-12) to tolerate edge cases without silently skipping real tags.
        if not clan_tag or not clan_tag.startswith('#') or len(clan_tag) < 5 or len(clan_tag) > 12:
            logging.debug(f"[DB-WRITE] Skipping invalid clan_tag format: {clan_tag!r} (len={len(clan_tag) if clan_tag else 0})")
            return

        # Skip family tags: subscriptions.clan_tag can store family tags (intentional design),
        # but family tags must NOT be inserted into the clans table — they are not real CoC clans
        # and cannot be polled via the CoC API. Inserting them would cause PHASE-1 to poll them
        # every cycle, producing constant NotFound 404 errors.
        cursor = await self._conn.execute(
            "SELECT 1 FROM clan_families WHERE family_tag = ? LIMIT 1",
            (clan_tag,)
        )
        if await cursor.fetchone():
            logging.debug(f"[DB-WRITE] Skipping _ensure_clan_exists for {clan_tag!r}: it is a family tag, not a real CoC clan")
            return

        from qapbot.cache_manager import CACHE
        
        # Check if clan already exists in database
        cursor = await self._conn.execute(
            "SELECT 1 FROM clans WHERE clan_tag = ? LIMIT 1",
            (clan_tag,)
        )
        exists = await cursor.fetchone()
        
        if not exists:
            # Clan not in database, get from cache and save it
            clan_data = CACHE.clan_name_cache.get(clan_tag)
            
            if clan_data:
                # Save full clan data from cache
                await self._save_clan_unlocked(
                    clan_tag=clan_tag,
                    name=str(clan_data.get("name", "Unknown")),
                    has_active_subscriptions=bool(clan_data.get("has_active_subscriptions", False)),
                    last_war_update=clan_data.get("last_war_update") if isinstance(clan_data.get("last_war_update"), str) else None,
                    warlog_is_public=bool(clan_data.get("warlog_is_public", True)),
                    last_checked_via_api=clan_data.get("last_checked_via_api") if isinstance(clan_data.get("last_checked_via_api"), str) else None
                )
                logging.debug(f"[DB-WRITE] Auto-saved clan {clan_tag} from cache to database")
            else:
                # Clan not in cache either, use placeholder (will be updated when clan is fetched)
                await self._save_clan_unlocked(
                    clan_tag=clan_tag,
                    name="Unknown",
                    has_active_subscriptions=False,
                    warlog_is_public=True
                )
                logging.warning(f"[DB-WRITE] Clan {clan_tag} not in cache, saved placeholder to database (will be updated on next clan fetch)")

    async def _ensure_family_exists(self, family_tag: str) -> None:
        """
        Ensure clan family exists in database before saving child records.
        
        If family doesn't exist in database, saves it from CACHE.clan_families.
        Creates a placeholder if not found in cache either.
        
        Args:
            family_tag: Family tag to ensure exists
        """
        await self._ensure_connection()
        assert self.conn is not None  # _ensure_connection() guarantees this
        
        if not family_tag:
            return
        
        from qapbot.cache_manager import CACHE
        
        # Check if family already exists in database
        cursor = await self._conn.execute(
            "SELECT 1 FROM clan_families WHERE family_tag = ? LIMIT 1",
            (family_tag,)
        )
        exists = await cursor.fetchone()
        
        if not exists:
            # Family not in database, get from cache and save it
            family_data = CACHE.clan_families.get(family_tag)
            
            if family_data:
                # Save family record only (no members yet to avoid recursion)
                await self._conn.execute("""
                    INSERT OR IGNORE INTO clan_families 
                    (family_tag, name, owned_by_guild, updated_at)
                    VALUES (?, ?, ?, datetime('now'))
                """, (family_tag, family_data.get("name", "Unknown Family"), family_data.get("owned_by_guild", "")))
                await self._conn.commit()
                logging.debug(f"[DB-WRITE] Auto-saved family {family_tag} from cache to database")
            else:
                # Family not in cache either, use placeholder
                await self._conn.execute("""
                    INSERT OR IGNORE INTO clan_families 
                    (family_tag, name, owned_by_guild, updated_at)
                    VALUES (?, ?, ?, datetime('now'))
                """, (family_tag, "Unknown Family", ""))
                await self._conn.commit()
                logging.warning(f"[DB-WRITE] Family {family_tag} not in cache, saved placeholder to database")

    # ==================== war_attacks + war_summary methods ====================

    # Column list + param-tuple mapping for war_attacks shared by every write path below
    # (sync single-clan add, sync single-clan update, and both branches of the batch
    # flush). Previously each site re-stated its own copy of both the SQL and the tuple
    # construction; any future schema change had to be applied identically N times with
    # no compiler/test signal if one copy was missed. `_WAR_ATTACKS_INSERT_SQL` is used
    # by the two DELETE-then-INSERT paths (update_war_data_sync, update_war_attack_records_sync)
    # where a plain INSERT is correct — OR IGNORE there would silently mask a bug in the
    # preceding DELETE instead of raising on an unexpected duplicate.
    _WAR_ATTACKS_COLUMNS = (
        "war_id, clan_tag, date, player_name, player_tag, th_level, "
        "map_position, attack_order, stars, destruction, defender_tag, "
        "defender_th, defender_map_position, duration, is_fresh, "
        "times_defended, best_def_destruction, "
        "max_attacks, missed_attacks, defensive_stars"
    )
    _WAR_ATTACKS_PLACEHOLDERS = ", ".join("?" * 20)
    _WAR_ATTACKS_INSERT_OR_IGNORE_SQL = (
        f"INSERT OR IGNORE INTO war_attacks ({_WAR_ATTACKS_COLUMNS}) "
        f"VALUES ({_WAR_ATTACKS_PLACEHOLDERS})"
    )
    _WAR_ATTACKS_INSERT_SQL = (
        f"INSERT INTO war_attacks ({_WAR_ATTACKS_COLUMNS}) "
        f"VALUES ({_WAR_ATTACKS_PLACEHOLDERS})"
    )

    @staticmethod
    def _build_war_attack_params(
        clan_tag: str, attack_rows: Iterable[Dict[str, Any]]
    ) -> List[Tuple[Any, ...]]:
        """Build war_attacks INSERT param tuples for *clan_tag*, in `_WAR_ATTACKS_COLUMNS` order.

        Each dict in *attack_rows* must have: WarID, Date, Player, PlayerID, TH_lvl,
        attack_order, stars, destruction, defender_tag, Max_Attacks, Missed_Attacks,
        Defensive_Stars. Optional keys default to 0 / -1 / 0.0 if absent: map_position,
        defender_th, defender_map_position, duration, is_fresh, times_defended,
        best_def_destruction.
        """
        return [
            (
                r["WarID"], clan_tag, r["Date"], r["Player"], r["PlayerID"],
                r["TH_lvl"], r.get("map_position", 0), r["attack_order"],
                r["stars"], r["destruction"], r["defender_tag"],
                r.get("defender_th", 0), r.get("defender_map_position", 0),
                r.get("duration", 0), r.get("is_fresh", -1),
                r.get("times_defended", 0), r.get("best_def_destruction", 0.0),
                r["Max_Attacks"], r["Missed_Attacks"], r["Defensive_Stars"]
            )
            for r in attack_rows
        ]

    def add_war_attack_records_sync(
        self, clan_tag: str, attack_rows: List[Dict[str, Any]]
    ) -> int:
        """
        Insert per-attack rows into war_attacks (idempotent via INSERT OR IGNORE).

        Each dict in *attack_rows* must have:
            WarID, Date, Player, PlayerID, TH_lvl, attack_order, stars,
            destruction, defender_tag, Max_Attacks, Missed_Attacks, Defensive_Stars
        Optional keys (default to 0 / -1 / 0.0 if absent):
            map_position, defender_th, defender_map_position, duration,
            is_fresh, times_defended, best_def_destruction
        """
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        if not attack_rows:
            return 0

        with self._sync_conn() as conn:
            try:
                with self._sync_write_lock:
                    conn.executemany(
                        self._WAR_ATTACKS_INSERT_OR_IGNORE_SQL,
                        self._build_war_attack_params(clan_tag, attack_rows),
                    )
                    inserted = conn.total_changes  # approximate
                    if self._should_commit():
                        conn.commit()
                logging.debug(f"[DB-WRITE-SYNC] war_attacks: inserted rows for {clan_tag}")
                return inserted
            except sqlite3.Error as e:
                logging.error(f"[DB-WRITE-SYNC] war_attacks insert failed for {clan_tag}: {e}")
                raise

    def add_war_summary_sync(
        self, clan_tag: str, summary: Dict[str, Any]
    ) -> bool:
        """
        Insert or replace a war_summary row for a single war.

        *summary* dict keys:
            war_id, opponent_tag, opponent_name, clan_stars, opponent_stars,
            clan_destruction, opp_destruction, team_size, attacks_per_member,
            war_type, is_cwl, cwl_season, result, date,
            clan_lineup_json, opp_lineup_json
        """
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                with self._sync_write_lock:
                    conn.execute("""
                        INSERT OR REPLACE INTO war_summary
                    (war_id, clan_tag, opponent_tag, opponent_name, clan_stars,
                     opponent_stars, clan_destruction, opp_destruction, team_size,
                     attacks_per_member, war_type, is_cwl, cwl_season, war_tag,
                     end_time, state,
                     result, date,
                     clan_lineup_json, opp_lineup_json,
                     clan_attacks_used, opp_attacks_used)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    summary["war_id"], clan_tag,
                    summary.get("opponent_tag", ""),
                    summary.get("opponent_name", ""),
                    summary.get("clan_stars", 0),
                    summary.get("opponent_stars", 0),
                    summary.get("clan_destruction", 0.0),
                    summary.get("opp_destruction", 0.0),
                    summary.get("team_size", 15),
                    summary.get("attacks_per_member", 2),
                    summary.get("war_type", "random"),
                    1 if summary.get("is_cwl") else 0,
                    summary.get("cwl_season", ""),
                    summary.get("war_tag", ""),
                    summary.get("end_time", ""),
                    summary.get("state", ""),
                    summary.get("result", ""),
                    summary["date"],
                    summary.get("clan_lineup_json", "[]"),
                    summary.get("opp_lineup_json", "[]"),
                    summary.get("clan_attacks_used", 0),
                    summary.get("opp_attacks_used", 0),
                ))
                if self._should_commit():
                    conn.commit()
                logging.debug(
                    f"[DB-WRITE-SYNC] war_summary upserted for {clan_tag} war {summary['war_id']}"
                )
                return True
            except sqlite3.Error as e:
                logging.error(
                    f"[DB-WRITE-SYNC] war_summary upsert failed for {clan_tag}: {e}"
                )
                return False

    def add_war_data_sync(
        self, clan_tag: str,
        attack_rows: List[Dict[str, Any]],
        summary: Optional[Dict[str, Any]],
    ) -> bool:
        """Insert war attacks AND war summary in a single transaction/commit.

        Combines ``add_war_attack_records_sync`` + ``add_war_summary_sync`` to
        avoid two separate fsync-commits on HDD/server-machine.  A single commit() reduces
        the per-war I/O cost from ~600 ms (2 × 300 ms fsync) to ~300 ms.

        When inside a ``war_write_batch()`` context, the data is collected
        in thread-local lists instead of being written immediately.  The
        batch context manager flushes all collected data on exit.

        Returns True on success, raises on failure so caller can handle.
        """
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        # P0 batch mode: collect instead of writing immediately
        if self.is_war_batch_active():
            self._tls.war_batch_appends.append((clan_tag, list(attack_rows), summary))
            logging.debug(f"[DB-WRITE-SYNC] war_data queued for batch ({clan_tag})")
            return True

        with self._sync_conn() as conn:
            try:
                with self._sync_write_lock:
                    if attack_rows:
                        conn.executemany(
                            self._WAR_ATTACKS_INSERT_OR_IGNORE_SQL,
                            self._build_war_attack_params(clan_tag, attack_rows),
                        )
                        self._upsert_player_name_index_in_conn(conn, attack_rows)
                    if summary:
                        conn.execute("""
                            INSERT OR REPLACE INTO war_summary
                            (war_id, clan_tag, opponent_tag, opponent_name, clan_stars,
                             opponent_stars, clan_destruction, opp_destruction, team_size,
                             attacks_per_member, war_type, is_cwl, cwl_season, war_tag,
                             end_time, state,
                             result, date,
                             clan_lineup_json, opp_lineup_json,
                             clan_attacks_used, opp_attacks_used, round_number)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            summary["war_id"], clan_tag,
                            summary.get("opponent_tag", ""),
                            summary.get("opponent_name", ""),
                            summary.get("clan_stars", 0),
                            summary.get("opponent_stars", 0),
                            summary.get("clan_destruction", 0.0),
                            summary.get("opp_destruction", 0.0),
                            summary.get("team_size", 15),
                            summary.get("attacks_per_member", 2),
                            summary.get("war_type", "random"),
                            1 if summary.get("is_cwl") else 0,
                            summary.get("cwl_season", ""),
                            summary.get("war_tag", ""),
                            summary.get("end_time", ""),
                            summary.get("state", ""),
                            summary.get("result", ""),
                            summary["date"],
                            summary.get("clan_lineup_json", "[]"),
                            summary.get("opp_lineup_json", "[]"),
                            summary.get("clan_attacks_used", 0),
                            summary.get("opp_attacks_used", 0),
                            summary.get("round_number"),
                        ))
                    if self._should_commit():
                        conn.commit()
                logging.debug(f"[DB-WRITE-SYNC] war_data committed for {clan_tag}")
                return True
            except sqlite3.Error as e:
                logging.error(f"[DB-WRITE-SYNC] war_data commit failed for {clan_tag}: {e}")
                raise

    def update_war_data_sync(
        self, clan_tag: str, war_id: str,
        attack_rows: List[Dict[str, Any]],
        summary: Optional[Dict[str, Any]],
    ) -> bool:
        """Atomic DELETE + re-INSERT attacks AND upsert summary in one commit.

        Late-attack variant of ``add_war_data_sync``.  The attack rows are
        replaced (DELETE old → INSERT new) while the summary is upserted, all
        within a single ``_sync_write_lock`` acquisition and one ``commit()``.

        When inside a ``war_write_batch()`` context, the data is collected
        in thread-local lists instead of being written immediately.
        """
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        if not attack_rows and not summary:
            return False

        # P0 batch mode: collect instead of writing immediately
        if self.is_war_batch_active():
            self._tls.war_batch_updates.append((clan_tag, war_id, list(attack_rows), summary))
            logging.debug(f"[DB-UPDATE-SYNC] war_data queued for batch ({clan_tag}/{war_id})")
            return True

        with self._sync_conn() as conn:
            try:
                with self._sync_write_lock:
                    if attack_rows:
                        conn.execute(
                            "DELETE FROM war_attacks WHERE war_id = ? AND clan_tag = ?",
                            (war_id, clan_tag),
                        )
                        conn.executemany(
                            self._WAR_ATTACKS_INSERT_SQL,
                            self._build_war_attack_params(clan_tag, attack_rows),
                        )
                        self._upsert_player_name_index_in_conn(conn, attack_rows)
                    if summary:
                        conn.execute("""
                            INSERT OR REPLACE INTO war_summary
                            (war_id, clan_tag, opponent_tag, opponent_name, clan_stars,
                             opponent_stars, clan_destruction, opp_destruction, team_size,
                             attacks_per_member, war_type, is_cwl, cwl_season, war_tag,
                             end_time, state,
                             result, date,
                             clan_lineup_json, opp_lineup_json,
                             clan_attacks_used, opp_attacks_used, round_number)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            summary["war_id"], clan_tag,
                            summary.get("opponent_tag", ""),
                            summary.get("opponent_name", ""),
                            summary.get("clan_stars", 0),
                            summary.get("opponent_stars", 0),
                            summary.get("clan_destruction", 0.0),
                            summary.get("opp_destruction", 0.0),
                            summary.get("team_size", 15),
                            summary.get("attacks_per_member", 2),
                            summary.get("war_type", "random"),
                            1 if summary.get("is_cwl") else 0,
                            summary.get("cwl_season", ""),
                            summary.get("war_tag", ""),
                            summary.get("end_time", ""),
                            summary.get("state", ""),
                            summary.get("result", ""),
                            summary["date"],
                            summary.get("clan_lineup_json", "[]"),
                            summary.get("opp_lineup_json", "[]"),
                            summary.get("clan_attacks_used", 0),
                            summary.get("opp_attacks_used", 0),
                            summary.get("round_number"),
                        ))
                    if self._should_commit():
                        conn.commit()
                logging.info(
                    f"[DB-UPDATE-SYNC] war_data updated for {clan_tag} war {war_id}"
                )
                return True
            except sqlite3.Error as e:
                logging.error(
                    f"[DB-UPDATE-SYNC] war_data update failed for {clan_tag}/{war_id}: {e}"
                )
                conn.rollback()
                return False

    def update_war_attack_records_sync(
        self, clan_tag: str, war_id: str, attack_rows: List[Dict[str, Any]]
    ) -> bool:
        """Atomic DELETE + INSERT for war_attacks (late-attack updates)."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        if not attack_rows:
            return False

        with self._sync_conn() as conn:
            try:
                with self._sync_write_lock:
                    conn.execute(
                        "DELETE FROM war_attacks WHERE war_id = ? AND clan_tag = ?",
                        (war_id, clan_tag),
                    )
                    conn.executemany(
                        self._WAR_ATTACKS_INSERT_SQL,
                        self._build_war_attack_params(clan_tag, attack_rows),
                    )
                    if self._should_commit():
                        conn.commit()
                logging.info(
                    f"[DB-UPDATE-SYNC] war_attacks updated for {clan_tag} war {war_id}"
                )
                return True
            except sqlite3.Error as e:
                logging.error(
                    f"[DB-UPDATE-SYNC] war_attacks update failed for {clan_tag}/{war_id}: {e}"
                )
                conn.rollback()
                return False

    def war_attacks_exist_sync(self, clan_tag: str, war_id: str) -> bool:
        """Check if war_attacks already has rows for this war."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                cursor = conn.execute(
                    "SELECT 1 FROM war_attacks WHERE clan_tag = ? AND war_id = ? LIMIT 1",
                    (clan_tag, war_id),
                )
                exists = cursor.fetchone() is not None
                return exists
            except sqlite3.Error as e:
                logging.error(f"[DB-CHECK-SYNC] war_attacks check failed: {e}")
                return False

    def get_war_summary_state_sync(self, clan_tag: str, war_id: str) -> Optional[str]:
        """Return the ``state`` column from war_summary for this war, or None if not found."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT state FROM war_summary WHERE clan_tag = ? AND war_id = ? LIMIT 1",
                    (clan_tag, war_id),
                )
                row = cursor.fetchone()
                return str(row["state"]) if row is not None else None
            except sqlite3.Error as e:
                logging.error(f"[DB-CHECK-SYNC] war_summary state check failed: {e}")
                return None

    def get_direct_cwl_attacks_sync(
        self, opp_clan_tag: str, cwl_season: str
    ) -> List[Dict[str, Any]]:
        """
        Return all attack rows for a specific clan's own CWL wars in a given season.

        Unlike get_cwl_attack_records_sync (which cross-joins via OUR clan's war_summary),
        this queries the opponent clan's war_attacks directly using their clan_tag.

        Used in cwlinfo_comp Step 3 to supplement historical attack data for
        current/upcoming opponents whose war against us is not yet committed to
        war_summary (and would therefore be invisible to the cross-join).

        Returns:
            List of dicts: player_tag, player_name, th_level, stars, defender_tag,
                           defender_th
        """
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                wa_cols = self._explicit_column_list_sync(conn, "war_attacks")
                ws_cols = self._explicit_column_list_sync(conn, "war_summary")
                rows = conn.execute(f"""
                    WITH wa AS (
                        SELECT {wa_cols} FROM main.war_attacks
                        UNION ALL SELECT {wa_cols} FROM history.war_attacks
                    ), ws AS (
                        SELECT {ws_cols} FROM main.war_summary
                        UNION ALL SELECT {ws_cols} FROM history.war_summary
                    )
                    SELECT wa.player_tag, wa.player_name,
                           wa.th_level AS attacker_th,
                           wa.map_position,
                           wa.stars,
                           wa.defender_tag,
                           COALESCE(NULLIF(wa.defender_th, 0), wa.th_level) AS defender_th
                    FROM wa
                    JOIN ws
                        ON wa.war_id   = ws.war_id
                       AND wa.clan_tag = ws.clan_tag
                    WHERE wa.clan_tag   = ?
                      AND ws.cwl_season = ?
                      AND ws.is_cwl     = 1
                      AND wa.attack_order > 0
                """, (opp_clan_tag, cwl_season)).fetchall()
                return [
                    {
                        "player_tag":   row["player_tag"],
                        "player_name":  row["player_name"],
                        "th_level":     int(row["attacker_th"]),
                        "map_position": int(row["map_position"]),
                        "stars":        int(row["stars"]),
                        "defender_tag": row["defender_tag"],
                        "defender_th":  int(row["defender_th"]),
                    }
                    for row in rows
                ]
            except sqlite3.Error as e:
                logging.error(
                    f"[DB-QUERY-SYNC] get_direct_cwl_attacks_sync failed for {opp_clan_tag}: {e}"
                )
                return []

    def _find_cwl_war_id_sync(
        self, conn: Any, clan_tag: str, cwl_season: Optional[str]
    ) -> Optional[Tuple[str, str]]:
        """Finds the war_id (+ its date) of a clan's most recent CWL war — optionally scoped to
        one season — by querying `main.war_summary` and `history.war_summary` SEPARATELY and
        picking whichever result is newer, rather than through a `WITH ws AS (SELECT * FROM main
        UNION ALL SELECT * FROM history)` CTE.

        2026-08-14 finding, measured against real DEV data: that CTE shape, referenced from
        inside a correlated scalar subquery (`wa.war_id = (SELECT ws.war_id FROM ws WHERE ...)`),
        made SQLite's planner give up on pushing the clan_tag/is_cwl filter into each UNION
        branch — `EXPLAIN QUERY PLAN` showed `MATERIALIZE ws` followed by a plain `SCAN` of both
        the full main.war_summary AND full history.war_summary tables, discarding
        idx_ws_clan_date/idx_ws_cwl_season entirely, on EVERY call. ~9.5s per call at DEV's
        current data size (regardless of season filter — cwl_season narrows the result, not the
        scan). Querying each schema directly is a plain indexed SEARCH: ~0.5ms per call, a
        >10,000x difference, confirmed via `resolve_prior_cwl_assignments()`'s 8-clan Start
        Enrollment call dropping from ~77s to ~5ms with no change in the returned rows.
        """
        season_clause = "AND cwl_season = ?" if cwl_season is not None else ""
        params: Tuple[Any, ...] = (clan_tag, cwl_season) if cwl_season is not None else (clan_tag,)
        row_main = conn.execute(
            f"SELECT war_id, date FROM main.war_summary "
            f"WHERE clan_tag = ? AND is_cwl = 1 {season_clause} ORDER BY date DESC LIMIT 1",
            params,
        ).fetchone()
        row_hist = conn.execute(
            f"SELECT war_id, date FROM history.war_summary "
            f"WHERE clan_tag = ? AND is_cwl = 1 {season_clause} ORDER BY date DESC LIMIT 1",
            params,
        ).fetchone()
        candidates = [r for r in (row_main, row_hist) if r is not None]
        if not candidates:
            return None
        best = max(candidates, key=lambda r: r["date"])
        return (best["war_id"], best["date"])

    def get_cwl_roster_sync(
        self, clan_tag: str, cwl_season: str
    ) -> List[Dict[str, Any]]:
        """
        Return the most up-to-date CWL roster for a clan from war_attacks.

        Pulls member info from the most recent war_summary row for this clan
        in the given CWL season.  Returns one entry per player with their
        name, TH level, and map_position as recorded in that war.

        Returns:
            List of dicts: player_tag, player_name, th_level, map_position
        """
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                war = self._find_cwl_war_id_sync(conn, clan_tag, cwl_season)
                if war is None:
                    return []
                war_id, _date = war
                wa_cols = self._explicit_column_list_sync(conn, "war_attacks")
                rows = conn.execute(f"""
                    SELECT player_tag, player_name, th_level, map_position FROM (
                        SELECT {wa_cols} FROM main.war_attacks WHERE war_id = ? AND clan_tag = ?
                        UNION ALL
                        SELECT {wa_cols} FROM history.war_attacks WHERE war_id = ? AND clan_tag = ?
                    )
                    GROUP BY player_tag
                """, (war_id, clan_tag, war_id, clan_tag)).fetchall()
                return [
                    {
                        "player_tag":   row["player_tag"],
                        "player_name":  row["player_name"],
                        "th_level":     int(row["th_level"]),
                        "map_position": int(row["map_position"]),
                    }
                    for row in rows
                ]
            except sqlite3.Error as e:
                logging.error(
                    f"[DB-QUERY-SYNC] get_cwl_roster_sync failed for {clan_tag}: {e}"
                )
                return []

    def get_most_recent_cwl_war_roster_sync(self, clan_tag: str) -> List[Dict[str, Any]]:
        """Same shape as get_cwl_roster_sync() above, but finds this clan's own most recent
        CWL war regardless of season (no cwl_season filter) and includes the attack date. Used
        by the "Manage Enrollment" auto-assignment seed (resolve_prior_cwl_assignments() in
        QBdiscocmdshelper_cwl.py): "if a clan didn't play CWL last month, fall back to that
        clan's own most recent CWL season" — each clan resolved independently, not against one
        shared season — and the date is needed to break ties when the same player qualifies for
        more than one candidate clan (latest attack wins).

        Returns:
            List of dicts: player_tag, player_name, th_level, map_position, date
        """
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                war = self._find_cwl_war_id_sync(conn, clan_tag, None)
                if war is None:
                    return []
                war_id, _date = war
                wa_cols = self._explicit_column_list_sync(conn, "war_attacks")
                rows = conn.execute(f"""
                    SELECT player_tag, player_name, th_level, map_position, date FROM (
                        SELECT {wa_cols} FROM main.war_attacks WHERE war_id = ? AND clan_tag = ?
                        UNION ALL
                        SELECT {wa_cols} FROM history.war_attacks WHERE war_id = ? AND clan_tag = ?
                    )
                    GROUP BY player_tag
                """, (war_id, clan_tag, war_id, clan_tag)).fetchall()
                return [
                    {
                        "player_tag":   row["player_tag"],
                        "player_name":  row["player_name"],
                        "th_level":     int(row["th_level"]),
                        "map_position": int(row["map_position"]),
                        "date":         row["date"],
                    }
                    for row in rows
                ]
            except sqlite3.Error as e:
                logging.error(
                    f"[DB-QUERY-SYNC] get_most_recent_cwl_war_roster_sync failed for {clan_tag}: {e}"
                )
                return []

    def get_last_real_cwl_attack_clan_sync(self, player_tags: List[str]) -> Dict[str, Tuple[str, str]]:
        """For each of ``player_tags``, finds the clan_tag of their own single most recent REAL
        CWL attack (``attack_order > 0`` — excludes the 0-attack "missed attack" sentinel rows
        idx_wa_zero_attacks exists for) across ANY clan they've ever attacked for, any season —
        not scoped to any particular clan or to clans currently participating in CWL. Used by
        the "Manage Enrollment" auto-assignment seed (resolve_prior_cwl_assignments() in
        QBdiscocmdshelper_cwl.py, redesigned 2026-08-14 per the project owner's spec: assign each
        player to wherever they last actually attacked in a CWL war, not to whichever
        currently-participating clan's own roster happens to still list them).

        Batched across all of ``player_tags`` in 2 queries total (one per schema, `main` and
        `history`) — NOT one query per player. Each is a plain indexed SEARCH on
        idx_wa_player_tag joined to war_summary's own (war_id, clan_tag) unique index (verified
        via EXPLAIN QUERY PLAN against real DEV data, 2026-08-14 — no SCAN anywhere, ~0.5-1s for
        300 players). Deliberately NOT the earlier `WITH wa AS (SELECT * FROM main UNION ALL
        SELECT * FROM history)`-CTE-plus-correlated-scalar-subquery shape used by
        get_most_recent_cwl_war_roster_sync() before this same date's fix — that shape made
        SQLite MATERIALIZE and SCAN the entire combined table on every call (~9.5s/clan measured
        against this exact data). Querying each schema directly with GROUP BY player_tag relies
        on SQLite's documented bare-column-with-MAX() behavior (the non-aggregated columns come
        from the row that produced the MAX): https://sqlite.org/lang_select.html#bareagg

        Returns:
            Dict of player_tag -> (clan_tag, date) for players with at least one real CWL
            attack on record. A player with none simply isn't in the dict.
        """
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        if not player_tags:
            return {}

        result: Dict[str, Tuple[str, str]] = {}
        with self._sync_conn() as conn:
            try:
                placeholders = ",".join("?" for _ in player_tags)
                for schema in ("main", "history"):
                    rows = conn.execute(
                        f"""
                        SELECT wa.player_tag, wa.clan_tag, MAX(wa.date) AS date
                        FROM {schema}.war_attacks wa
                        JOIN {schema}.war_summary ws
                            ON ws.war_id = wa.war_id AND ws.clan_tag = wa.clan_tag
                        WHERE wa.attack_order > 0 AND ws.is_cwl = 1
                          AND wa.player_tag IN ({placeholders})
                        GROUP BY wa.player_tag
                        """,
                        player_tags,
                    ).fetchall()
                    for row in rows:
                        player_tag = row["player_tag"]
                        existing = result.get(player_tag)
                        if existing is None or row["date"] > existing[1]:
                            result[player_tag] = (row["clan_tag"], row["date"])
                return result
            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] get_last_real_cwl_attack_clan_sync failed: {e}")
                return {}

    def get_most_recent_th_levels_sync(self, player_tags: List[str]) -> Dict[str, int]:
        """Most recently recorded th_level per player_tag, from war_attacks (any war type — CWL
        or regular alike, whichever is more recent) — used by the "Manage Enrollment" board
        (CWL_ROSTER_PLANNING_PLAN.md) to show each player's current Town Hall next to their
        name. Bounded strictly to the given `player_tags` (never the whole war_attacks table —
        the `SELECT player_tag, MAX(th_level) ... GROUP BY player_tag` shape this deliberately
        avoids is flagged as a full-table-scan anti-pattern in DATABASE_ARCHITECTURE.md), and
        scoped across both the hot and history DBs like the rest of this file's CWL queries.

        Returns: Dict[player_tag, th_level]. A player_tag with no tracked war_attacks row at
        all (e.g. brand new to the bot) is simply absent from the returned dict.
        """
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        if not player_tags:
            return {}

        with self._sync_conn() as conn:
            try:
                placeholders = ",".join("?" for _ in player_tags)
                wa_cols = self._explicit_column_list_sync(conn, "war_attacks")
                rows = conn.execute(f"""
                    WITH wa AS (
                        SELECT {wa_cols} FROM main.war_attacks
                        UNION ALL SELECT {wa_cols} FROM history.war_attacks
                    )
                    SELECT player_tag, th_level, date
                    FROM wa
                    WHERE player_tag IN ({placeholders})
                    ORDER BY player_tag, date DESC
                """, player_tags).fetchall()
                result: Dict[str, int] = {}
                for row in rows:
                    # ORDER BY date DESC above means the first row seen per player_tag is
                    # already its most recent — later duplicates for the same tag are skipped.
                    if row["player_tag"] not in result:
                        result[row["player_tag"]] = int(row["th_level"])
                return result
            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] get_most_recent_th_levels_sync failed: {e}")
                return {}

    def get_recent_cwl_attacks_with_league_sync(
        self, player_tags: List[str], *, since_date: Optional[str] = None
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Each requested player's CWL attacks on or after `since_date` (inclusive, "YYYY-MM-DD";
        None means no lower bound at all — every CWL attack on record), most recent first, each
        annotated with the CWL league tier their attacking clan was in for that round. Feeds the
        "Manage Enrollment" board's player-skill number (compute_league_adjusted_skill_scores,
        QBdiscocmdshelper_cwl.py) and its plain-average counterpart (compute_avg_stars_per_attack)
        — both callers pass the same trailing-3-calendar-month boundary the hover pop-up's own
        get_recent_cwl_player_stats (QBhelperfunctions.py) uses, so the board tile's own number and
        the pop-up's stats can never disagree on their time window again (2026-08-16, project
        owner's spec, verbatim: "our hover over pop-up info is inconsistent to the way we
        calculate the stats for the player tile info. there we use the last 10 cwl attacks. We
        should make this consistent and use the 'last three months' logic for both").

        Previously capped to each player's most recent 10 attacks (a COUNT-based window,
        truncated in Python after fetching everything) — replaced with this SQL date filter (a
        CALENDAR-based window, pushed into the query itself) now that both callers share a
        calendar-month concept of "recent" rather than a fixed attack count.

        No war_summary/war_attacks column stores league tier directly — it's reconstructed via
        war_summary.war_tag -> cwl_league_rounds -> cwl_league_groups.league_rank, joined on
        `league_group_id` (mirroring the existing async get_war_tag_leagues()'s proven approach)
        rather than `(cwl_season, clan_tag)` — every clan in a league group shares the same
        league by CWL's own design, so this needs no clan_tag at all and can't miss a row just
        because the *attacking* clan's own cwl_league_groups entry happens to be the one still
        unpopulated. `SELECT DISTINCT` collapses the one-row-per-group-member duplication that
        joining through league_group_id (rather than a single clan_tag) produces.

        Queried per schema (main, then history) with plain table JOINs — never against a UNION
        ALL CTE (DATABASE_ARCHITECTURE.md's query anti-patterns: SQLite must fully materialize a
        compound subquery used as a JOIN target) — then merged and sorted in Python.

        Returns Dict[player_tag, [{"stars": int, "league_rank": str|None, "date": str}, ...]] —
        a player_tag with zero resolvable CWL-attack-with-league rows in the window (never played
        CWL, played outside the window, or cwl_league_rounds/cwl_league_groups aren't populated
        for those seasons) is simply absent.
        """
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        if not player_tags:
            return {}

        placeholders = ",".join("?" for _ in player_tags)
        date_clause = " AND wa.date >= ?" if since_date is not None else ""
        select_cols = "DISTINCT wa.player_tag, wa.stars, wa.date, clg.league_rank"
        where = f"WHERE wa.player_tag IN ({placeholders}) AND ws.is_cwl = 1{date_clause}"
        main_sql = f"""
            SELECT {select_cols}
            FROM war_attacks wa
            JOIN war_summary ws ON wa.war_id = ws.war_id
            JOIN cwl_league_rounds clr ON clr.war_tag = ws.war_tag
            JOIN cwl_league_groups clg ON clg.league_group_id = clr.league_group_id
            {where}
        """
        history_sql = f"""
            SELECT {select_cols}
            FROM history.war_attacks wa
            JOIN history.war_summary ws ON wa.war_id = ws.war_id
            JOIN history.cwl_league_rounds clr ON clr.war_tag = ws.war_tag
            JOIN history.cwl_league_groups clg ON clg.league_group_id = clr.league_group_id
            {where}
        """
        params: List[Any] = list(player_tags) + ([since_date] if since_date is not None else [])
        with self._sync_conn() as conn:
            try:
                rows = conn.execute(main_sql, params).fetchall()
                rows += conn.execute(history_sql, params).fetchall()
            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] get_recent_cwl_attacks_with_league_sync failed: {e}")
                return {}

        by_tag: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            by_tag.setdefault(row["player_tag"], []).append(
                {"stars": int(row["stars"]), "league_rank": row["league_rank"], "date": row["date"]}
            )
        for attacks in by_tag.values():
            attacks.sort(key=lambda a: a["date"], reverse=True)
        return by_tag

    # --- CWL roster planning (CWL_ROSTER_PLANNING_PLAN.md Phase 1) -------------------------
    # Sync CRUD, matching get_cwl_roster_sync's style above — called from UI callbacks, not
    # the async polling loop.

    def create_cwl_event_sync(
        self,
        guild_id: str,
        cwl_season: str,
        created_by_discord_id: str,
        template_season: Optional[str] = None,
    ) -> Optional[int]:
        """Create a draft cwl_events row, or return the existing id if one already exists for
        this guild+season — idempotent, since "Configure Participating Clans" may be applied
        more than once against the same draft event before Start Enrollment locks it in."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                with self._sync_write_lock:
                    cursor = conn.execute(
                        """
                        INSERT INTO cwl_events (guild_id, cwl_season, created_by_discord_id, template_season)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(guild_id, cwl_season) DO UPDATE SET updated_at = datetime('now')
                        RETURNING id
                        """,
                        (guild_id, cwl_season, created_by_discord_id, template_season),
                    )
                    row = cursor.fetchone()
                    if self._should_commit():
                        conn.commit()
                return int(row["id"]) if row is not None else None
            except sqlite3.Error as e:
                logging.error(f"[DB-WRITE-SYNC] create_cwl_event_sync failed for guild {guild_id} season {cwl_season}: {e}")
                conn.rollback()
                return None

    def get_cwl_event_sync(self, guild_id: str, cwl_season: str) -> Optional[Dict[str, Any]]:
        """Return the cwl_events row for this guild+season, or None if it doesn't exist."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                row = conn.execute(
                    "SELECT * FROM cwl_events WHERE guild_id = ? AND cwl_season = ?",
                    (guild_id, cwl_season),
                ).fetchone()
                return dict(row) if row is not None else None
            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] get_cwl_event_sync failed for guild {guild_id} season {cwl_season}: {e}")
                return None

    def get_cwl_event_by_id_sync(self, event_id: int) -> Optional[Dict[str, Any]]:
        """Return a cwl_events row by its own id, or None. Needed wherever only the id is known
        (e.g. a DynamicItem DM button's custom_id embeds event_id, not guild_id/cwl_season)."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                row = conn.execute(
                    "SELECT * FROM cwl_events WHERE id = ?", (event_id,)
                ).fetchone()
                return dict(row) if row is not None else None
            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] get_cwl_event_by_id_sync failed for event {event_id}: {e}")
                return None

    def list_cwl_events_sync(self, guild_id: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return this guild's cwl_events rows, newest season first, optionally filtered by status."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                if status is not None:
                    rows = conn.execute(
                        "SELECT * FROM cwl_events WHERE guild_id = ? AND status = ? ORDER BY cwl_season DESC",
                        (guild_id, status),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM cwl_events WHERE guild_id = ? ORDER BY cwl_season DESC",
                        (guild_id,),
                    ).fetchall()
                return [dict(row) for row in rows]
            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] list_cwl_events_sync failed for guild {guild_id}: {e}")
                return []

    def list_cwl_events_for_season_across_guilds_sync(self, season: str) -> List[Dict[str, Any]]:
        """Every guild's cwl_events row for one season, across ALL guilds — unlike every other
        cwl_events query in this file (always scoped to one guild_id), this is deliberately
        guild-agnostic: used by /list's "Managed CWLs" option (2026-08-16, project owner's spec)
        to answer "which guilds have a managed CWL for season X" as a single query rather than
        looping every known guild_id. No index on cwl_season alone (only
        idx_cwl_events_guild_status, guild_id-first) — an acceptable full-table scan given
        cwl_events is short-lived operational data per DATABASE_ARCHITECTURE.md, not a
        hot/history-split table. One row per guild (guild_id, cwl_season) is UNIQUE by schema."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                rows = conn.execute(
                    "SELECT * FROM cwl_events WHERE cwl_season = ? ORDER BY guild_id",
                    (season,),
                ).fetchall()
                return [dict(row) for row in rows]
            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] list_cwl_events_for_season_across_guilds_sync failed for season {season}: {e}")
                return []

    def update_cwl_event_status_sync(self, event_id: int, status: str) -> bool:
        """Transition a cwl_events row's status, always stamping updated_at explicitly
        (SQLite's DEFAULT only applies at INSERT — the retention purge keys off this column)."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                with self._sync_write_lock:
                    conn.execute(
                        "UPDATE cwl_events SET status = ?, updated_at = datetime('now') WHERE id = ?",
                        (status, event_id),
                    )
                    if self._should_commit():
                        conn.commit()
                return True
            except sqlite3.Error as e:
                logging.error(f"[DB-WRITE-SYNC] update_cwl_event_status_sync failed for event {event_id}: {e}")
                conn.rollback()
                return False

    def delete_cwl_event_sync(self, event_id: int) -> bool:
        """Delete a cwl_events row outright — cascades to cwl_event_clans/cwl_signups/
        cwl_assignments (ON DELETE CASCADE). Used by the admin "Delete Season" action (mainly
        for testing/starting over), not part of the normal draft->announced lifecycle, which
        uses update_cwl_event_status_sync() instead."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                with self._sync_write_lock:
                    conn.execute("DELETE FROM cwl_events WHERE id = ?", (event_id,))
                    if self._should_commit():
                        conn.commit()
                return True
            except sqlite3.Error as e:
                logging.error(f"[DB-WRITE-SYNC] delete_cwl_event_sync failed for event {event_id}: {e}")
                conn.rollback()
                return False

    def set_cwl_event_clans_sync(self, event_id: int, clan_configs: List[Dict[str, Any]]) -> bool:
        """Replace an event's full cwl_event_clans set in one atomic DELETE + INSERT (matches
        the pattern used elsewhere for whole-collection replacement, e.g. war_attacks updates).

        Callers must pass every clan that has ever been configured for this event — participating
        and deactivated alike — not just the currently-participating ones. A clan omitted here
        entirely loses its row (and thus its roster_size/cwl_start_at); a clan included with
        participating=False keeps its row (and settings) but won't count as participating. This
        is what lets deactivating a clan preserve its settings instead of discarding them.

        Args:
            event_id: the cwl_events row to configure.
            clan_configs: list of dicts, each with clan_tag (required) and optionally
                target_league_rank, roster_size, tier_order, cwl_start_at, participating
                (defaults to True — existing callers written before this field existed still
                mean "participating" by omitting it).
        """
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                with self._sync_write_lock:
                    conn.execute("DELETE FROM cwl_event_clans WHERE event_id = ?", (event_id,))
                    conn.executemany(
                        """
                        INSERT INTO cwl_event_clans
                            (event_id, clan_tag, target_league_rank, roster_size, tier_order, cwl_start_at, participating)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                event_id,
                                cfg["clan_tag"],
                                cfg.get("target_league_rank"),
                                cfg.get("roster_size", 15),
                                cfg.get("tier_order", 0),
                                cfg.get("cwl_start_at"),
                                1 if cfg.get("participating", True) else 0,
                            )
                            for cfg in clan_configs
                        ],
                    )
                    if self._should_commit():
                        conn.commit()
                return True
            except sqlite3.Error as e:
                logging.error(f"[DB-WRITE-SYNC] set_cwl_event_clans_sync failed for event {event_id}: {e}")
                conn.rollback()
                return False

    def deactivate_cwl_event_clan_sync(self, event_id: int, clan_tag: str) -> bool:
        """Mark one clan as no longer participating in one event — the cascade half of the
        "clan removed from guild while still in the active CWL lineup" confirmation flow
        (ui_clan_management.py's MemberClansConfigurationView). Narrower than
        set_cwl_event_clans_sync() (which replaces the event's whole clan set): only flips
        `participating`, leaving roster_size/cwl_start_at/target_league_rank/tier_order intact —
        same "keep the row, just stop counting it" convention as manual deactivation."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                with self._sync_write_lock:
                    conn.execute(
                        "UPDATE cwl_event_clans SET participating = 0 WHERE event_id = ? AND clan_tag = ?",
                        (event_id, clan_tag),
                    )
                    if self._should_commit():
                        conn.commit()
                return True
            except sqlite3.Error as e:
                logging.error(f"[DB-WRITE-SYNC] deactivate_cwl_event_clan_sync failed for event {event_id} clan {clan_tag}: {e}")
                conn.rollback()
                return False

    def get_cwl_event_clans_sync(self, event_id: int) -> List[Dict[str, Any]]:
        """Return an event's participating clans, ordered by tier (0 = highest tier first)."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                rows = conn.execute(
                    "SELECT * FROM cwl_event_clans WHERE event_id = ? ORDER BY tier_order, clan_tag",
                    (event_id,),
                ).fetchall()
                return [dict(row) for row in rows]
            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] get_cwl_event_clans_sync failed for event {event_id}: {e}")
                return []

    def get_cwl_signup_status_counts_sync(self, event_id: int) -> Dict[str, int]:
        """Return {status: count} for an event's cwl_signups (pending/confirmed/declined/
        withdrawn) — statuses with zero rows are simply absent from the dict, not zero-filled."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                rows = conn.execute(
                    "SELECT status, COUNT(*) AS n FROM cwl_signups WHERE event_id = ? GROUP BY status",
                    (event_id,),
                ).fetchall()
                return {row["status"]: int(row["n"]) for row in rows}
            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] get_cwl_signup_status_counts_sync failed for event {event_id}: {e}")
                return {}

    def get_previous_cwl_event_clans_sync(
        self, guild_id: str, exclude_event_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Return the most recent prior cwl_event_clans rows for this guild — the working-copy
        seed for "Configure Participating Clans" when the current season has no rows of its own
        yet, implementing the confirmed carry-over-from-previous-season default. "Most recent
        prior" is the newest cwl_events row for this guild (by cwl_season) other than
        exclude_event_id (the event currently being configured, if it already exists)."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                if exclude_event_id is not None:
                    prev_event = conn.execute(
                        "SELECT id FROM cwl_events WHERE guild_id = ? AND id != ? ORDER BY cwl_season DESC LIMIT 1",
                        (guild_id, exclude_event_id),
                    ).fetchone()
                else:
                    prev_event = conn.execute(
                        "SELECT id FROM cwl_events WHERE guild_id = ? ORDER BY cwl_season DESC LIMIT 1",
                        (guild_id,),
                    ).fetchone()
                if prev_event is None:
                    return []
                # Only carry over clans that were actually participating last season — a clan
                # explicitly deactivated then (row kept for its settings, per the fix above)
                # should not silently reappear as a carry-over default this season.
                rows = conn.execute(
                    "SELECT * FROM cwl_event_clans WHERE event_id = ? AND participating = 1 ORDER BY tier_order, clan_tag",
                    (prev_event["id"],),
                ).fetchall()
                return [dict(row) for row in rows]
            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] get_previous_cwl_event_clans_sync failed for guild {guild_id}: {e}")
                return []

    def get_previous_cwl_season_sync(
        self, guild_id: str, exclude_event_id: Optional[int] = None
    ) -> Optional[str]:
        """Return this guild's most recent prior cwl_events.cwl_season — the same "most recent
        prior event" resolution get_previous_cwl_event_clans_sync (above) uses, exposed here as
        just the season string. Used alongside get_clans_with_cwl_data_for_season_sync to pre-set
        a new season's participating clans from real CWL war history for that prior season,
        across the guild's full family (not just clans that had a cwl_event_clans row before) —
        see CwlCarryOverPromptView._create_season (ui_cwl_roster.py)."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                if exclude_event_id is not None:
                    row = conn.execute(
                        "SELECT cwl_season FROM cwl_events WHERE guild_id = ? AND id != ? ORDER BY cwl_season DESC LIMIT 1",
                        (guild_id, exclude_event_id),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT cwl_season FROM cwl_events WHERE guild_id = ? ORDER BY cwl_season DESC LIMIT 1",
                        (guild_id,),
                    ).fetchone()
                return row["cwl_season"] if row else None
            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] get_previous_cwl_season_sync failed for guild {guild_id}: {e}")
                return None

    def _chunked_in_query_sync(self, conn, sql_template: str, values: List[str], chunk_size: int = 900) -> "List[Any]":
        """Execute sql_template (containing one literal '{placeholders}' slot for the IN (...)
        clause) once per chunk_size-sized slice of values against conn, returning every chunk's
        rows concatenated. Keeps any single query's host-parameter count under SQLite's ~999
        limit (2026-08-17 PROD incident: an unbounded CWL guest-search player list produced
        "too many SQL variables", one contributor to the two production meltdowns —
        CWL_PROD_PERFORMANCE_FIX_PLAN.md Step 4). Callers relying on ORDER BY semantics across
        the merged result (e.g. verified-wins dedup) must re-sort the returned rows themselves —
        a per-chunk ORDER BY does not hold across chunk boundaries."""
        rows: List[Any] = []
        for i in range(0, len(values), chunk_size):
            chunk = values[i:i + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            rows.extend(conn.execute(sql_template.format(placeholders=placeholders), chunk).fetchall())
        return rows

    def get_current_clan_members_sync(self, clan_tags: List[str]) -> List[Dict[str, Any]]:
        """Return every linked account currently tracked as a member of one of clan_tags — the
        seed for Start Enrollment's signup pool (CWL_ROSTER_PLANNING_PLAN.md Phase 2, corrected
        2026-08-10). Sourced from user_players.current_clan_tag (the bot's own live membership
        tracking, kept fresh by the regular clan-data cycle — not a CoC API call here) rather
        than from prior CWL war history (the original design, get_cwl_roster_sync-based): a clan
        with no tracked CWL wars yet — brand new to the bot, or simply never played CWL before —
        would otherwise seed zero signups even though it has real, known members today. A
        departed member (current_clan_tag no longer matching) is correctly excluded, unlike a
        "last season's roster" source would have included them.

        Returns one dict per player_tag: player_tag, player_name, clan_tag, discord_id (None if
        the sentinel 'UNASSIGNED' or unset — no real linked account to DM), verified,
        cwl_permanent_optout, preferred_league_rank, th_level (kept fresh for every current
        member — linked or not — by coc_cache.py's per-clan poll cycle; None if this row
        predates that cycle ever running, e.g. immediately after a fresh DB import). If the same
        player_tag is linked by more than one Discord account (disputed ownership), the verified
        one wins. Callers decide what to do with opted-out/unlinked accounts — this function only
        resolves data, it does not filter, matching the same division of responsibility
        get_previous_cwl_participants_sync (removed) used to have.
        """
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        if not clan_tags:
            return []

        with self._sync_conn() as conn:
            try:
                rows = self._chunked_in_query_sync(
                    conn,
                    """
                    SELECT player_tag, player_name, current_clan_tag, discord_id, verified,
                           cwl_permanent_optout, cwl_default_preferred_league_rank, th_level
                    FROM user_players
                    WHERE current_clan_tag IN ({placeholders})
                    ORDER BY verified DESC
                    """,
                    clan_tags,
                )
            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] get_current_clan_members_sync failed: {e}")
                return []

        # ORDER BY verified DESC means the first row seen per player_tag is the verified one, if
        # any exists — an unverified/disputed second linker never overwrites it. Re-sorted here
        # (rather than trusting per-chunk ORDER BY) because chunking can split one player_tag's
        # candidate rows across chunks, and concatenation doesn't preserve global order.
        rows = sorted(rows, key=lambda r: not r["verified"])
        members_by_tag: Dict[str, sqlite3.Row] = {}
        for row in rows:
            members_by_tag.setdefault(row["player_tag"], row)

        members: List[Dict[str, Any]] = []
        for player_tag, row in members_by_tag.items():
            discord_id = row["discord_id"]
            members.append({
                "player_tag": player_tag,
                "player_name": row["player_name"],
                "clan_tag": row["current_clan_tag"],
                "discord_id": discord_id if discord_id and discord_id != "UNASSIGNED" else None,
                "verified": bool(row["verified"]),
                "cwl_permanent_optout": bool(row["cwl_permanent_optout"]),
                "preferred_league_rank": row["cwl_default_preferred_league_rank"],
                "th_level": row["th_level"],
            })
        return members

    def get_all_players_for_discord_ids_sync(self, discord_ids: List[str]) -> List[Dict[str, Any]]:
        """Return every linked player_tag for discord_ids, regardless of current clan — the
        account-wide expansion for guild_config.cwl_enrollment_include_all_linked_accounts
        (2026-08-15, project owner's spec: an account that already qualifies for CWL enrollment
        via one in-family player should also bring in its other characters, wherever they
        currently play). Unlike get_current_clan_members_sync (clan-scoped, WHERE
        current_clan_tag IN (...)), this is account-scoped — WHERE discord_id IN (...) — so a
        returned clan_tag may belong to a clan that isn't part of THIS guild's own family at all
        (tracked only via a different guild, or a bare channel subscription, or not tracked by
        anyone right now — current_clan_tag has a FK to `clans`, so the clan itself must exist
        there, even if this guild has never heard of it as a "member clan"). Callers already
        treat an unrecognized clan_tag as best-effort display data, same as any other clan_tag.

        Same row shape as get_current_clan_members_sync (player_tag, player_name, clan_tag,
        discord_id, verified, cwl_permanent_optout, preferred_league_rank, th_level) so callers
        can merge results from both uniformly."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        if not discord_ids:
            return []

        with self._sync_conn() as conn:
            try:
                placeholders = ",".join("?" for _ in discord_ids)
                rows = conn.execute(
                    f"""
                    SELECT player_tag, player_name, current_clan_tag, discord_id, verified,
                           cwl_permanent_optout, cwl_default_preferred_league_rank, th_level
                    FROM user_players
                    WHERE discord_id IN ({placeholders})
                    ORDER BY verified DESC
                    """,
                    discord_ids,
                ).fetchall()
            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] get_all_players_for_discord_ids_sync failed: {e}")
                return []

        # Same verified-wins-per-player_tag dedup as get_current_clan_members_sync.
        players_by_tag: Dict[str, sqlite3.Row] = {}
        for row in rows:
            players_by_tag.setdefault(row["player_tag"], row)

        players: List[Dict[str, Any]] = []
        for player_tag, row in players_by_tag.items():
            discord_id = row["discord_id"]
            players.append({
                "player_tag": player_tag,
                "player_name": row["player_name"],
                "clan_tag": row["current_clan_tag"],
                "discord_id": discord_id if discord_id and discord_id != "UNASSIGNED" else None,
                "verified": bool(row["verified"]),
                "cwl_permanent_optout": bool(row["cwl_permanent_optout"]),
                "preferred_league_rank": row["cwl_default_preferred_league_rank"],
                "th_level": row["th_level"],
            })
        return players

    def get_player_links_sync(self, player_tags: List[str]) -> Dict[str, Dict[str, Any]]:
        """player_tag -> {discord_id, player_name, verified} for whichever of player_tags are
        linked to a Discord account (user_players), regardless of current clan — a player_tag
        with no linked account simply isn't a key in the returned dict. Used by the CWL guest
        search (web_bridge.py's _search_cwl_guests_sync) to show whether a found player can actually
        be DMed, without needing clan context the way get_current_clan_members_sync does."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        if not player_tags:
            return {}

        with self._sync_conn() as conn:
            try:
                rows = self._chunked_in_query_sync(
                    conn,
                    """
                    SELECT player_tag, player_name, discord_id, verified
                    FROM user_players
                    WHERE player_tag IN ({placeholders})
                    ORDER BY verified DESC
                    """,
                    player_tags,
                )
            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] get_player_links_sync failed: {e}")
                return {}

        # Same verified-wins-per-player_tag dedup as get_current_clan_members_sync — re-sorted
        # globally first since chunking (Step 4) can split one player_tag's rows across chunks.
        rows = sorted(rows, key=lambda r: not r["verified"])
        links: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            if row["player_tag"] in links:
                continue
            discord_id = row["discord_id"]
            links[row["player_tag"]] = {
                "player_name": row["player_name"],
                "discord_id": discord_id if discord_id and discord_id != "UNASSIGNED" else None,
                "verified": bool(row["verified"]),
            }
        return links

    def get_current_clan_tags_for_players_sync(self, player_tags: List[str]) -> Dict[str, str]:
        """player_tag -> current_clan_tag for ANY player_tag, regardless of whether that clan is
        in some caller-side filter set — unlike get_current_clan_members_sync (clan-scoped:
        WHERE current_clan_tag IN a specific list), this is player-scoped, so it also resolves a
        guest/account-wide-expanded player whose real current clan is entirely outside the
        guild's own family AND isn't itself participating this season. Used as
        _build_enrollment_payload's fallback for exactly that case (2026-08-15 bugfix — those
        players' cards were silently stuck on the plain/default shade forever, never green or
        amber, since get_current_clan_members_sync(all_member_clan_tags) can structurally never
        return them). Same verified-wins-per-tag dedup as get_current_clan_members_sync."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        if not player_tags:
            return {}

        with self._sync_conn() as conn:
            try:
                rows = self._chunked_in_query_sync(
                    conn,
                    """
                    SELECT player_tag, current_clan_tag, verified
                    FROM user_players
                    WHERE player_tag IN ({placeholders}) AND current_clan_tag IS NOT NULL
                    ORDER BY verified DESC
                    """,
                    player_tags,
                )
            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] get_current_clan_tags_for_players_sync failed: {e}")
                return {}

        # Re-sorted globally first (Step 4) — chunking can split one player_tag's rows across
        # chunks, so a per-chunk ORDER BY alone would not guarantee verified-wins here.
        rows = sorted(rows, key=lambda r: not r["verified"])
        clan_tags: Dict[str, str] = {}
        for row in rows:
            clan_tags.setdefault(row["player_tag"], row["current_clan_tag"])
        return clan_tags

    def bulk_create_cwl_signups_sync(self, event_id: int, signups: List[Dict[str, Any]]) -> bool:
        """Bulk-insert cwl_signups rows for Start Enrollment's template-copy step. Idempotent via
        ON CONFLICT(event_id, player_tag) DO NOTHING — safe to re-run without clobbering a row a
        player may have already responded to (defensive; Start Enrollment only runs once per event
        while status is still 'draft', before self-signup/DM responses are even possible)."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        if not signups:
            return True

        with self._sync_conn() as conn:
            try:
                with self._sync_write_lock:
                    conn.executemany(
                        """
                        INSERT INTO cwl_signups
                            (event_id, player_tag, player_name, discord_id, preferred_league_rank, source, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(event_id, player_tag) DO NOTHING
                        """,
                        [
                            (
                                event_id,
                                s["player_tag"],
                                s.get("player_name"),
                                s.get("discord_id"),
                                s.get("preferred_league_rank"),
                                s.get("source", "template_confirm"),
                                s.get("status", "pending"),
                            )
                            for s in signups
                        ],
                    )
                    if self._should_commit():
                        conn.commit()
                return True
            except sqlite3.Error as e:
                logging.error(f"[DB-WRITE-SYNC] bulk_create_cwl_signups_sync failed for event {event_id}: {e}")
                conn.rollback()
                return False

    def upsert_cwl_signup_sync(
        self,
        event_id: int,
        player_tag: str,
        player_name: Optional[str],
        discord_id: Optional[str],
        preferred_league_rank: Optional[str],
        source: str,
        status: str,
        responded_at: Optional[str] = None,
    ) -> bool:
        """Create or overwrite a single cwl_signups row — the write path for an explicit action
        (self-signup, DM confirm/opt-out button, admin-added), as opposed to the bulk template-copy
        seed above. Always overwrites on conflict since an explicit action supersedes whatever was
        there before (e.g. a member re-running /cwl signup to change their preferred tier)."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                with self._sync_write_lock:
                    conn.execute(
                        """
                        INSERT INTO cwl_signups
                            (event_id, player_tag, player_name, discord_id, preferred_league_rank, source, status, responded_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(event_id, player_tag) DO UPDATE SET
                            player_name = excluded.player_name,
                            discord_id = excluded.discord_id,
                            preferred_league_rank = excluded.preferred_league_rank,
                            source = excluded.source,
                            status = excluded.status,
                            responded_at = excluded.responded_at
                        """,
                        (event_id, player_tag, player_name, discord_id, preferred_league_rank, source, status, responded_at),
                    )
                    if self._should_commit():
                        conn.commit()
                return True
            except sqlite3.Error as e:
                logging.error(f"[DB-WRITE-SYNC] upsert_cwl_signup_sync failed for event {event_id} player {player_tag}: {e}")
                conn.rollback()
                return False

    def get_cwl_signups_for_event_sync(
        self, event_id: int, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Return an event's cwl_signups rows, optionally filtered to one status."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                if status is not None:
                    rows = conn.execute(
                        "SELECT * FROM cwl_signups WHERE event_id = ? AND status = ? ORDER BY player_name",
                        (event_id, status),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM cwl_signups WHERE event_id = ? ORDER BY player_name",
                        (event_id,),
                    ).fetchall()
                return [dict(row) for row in rows]
            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] get_cwl_signups_for_event_sync failed for event {event_id}: {e}")
                return []

    def get_cwl_signup_sync(self, event_id: int, player_tag: str) -> Optional[Dict[str, Any]]:
        """Return one cwl_signups row, or None. Callers that mutate signup state on a DM-button
        click or /cwl command must re-read via this rather than trusting closure/cached state —
        the event may have moved to finalized/cancelled since the row was first fetched."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                row = conn.execute(
                    "SELECT * FROM cwl_signups WHERE event_id = ? AND player_tag = ?",
                    (event_id, player_tag),
                ).fetchone()
                return dict(row) if row is not None else None
            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] get_cwl_signup_sync failed for event {event_id} player {player_tag}: {e}")
                return None

    def delete_cwl_signup_sync(self, event_id: int, player_tag: str) -> bool:
        """Remove one cwl_signups row entirely — as opposed to upsert_cwl_signup_sync's
        create-or-overwrite, this is a genuine removal (e.g. purging a foreign shared-clan guest
        once their origin clan's owning guild reassigns them elsewhere — see
        find_cwl_signups_by_origin_shared_clan_sync). Callers that want a player OUT of a clan
        column but still visible in the pool should use delete_cwl_assignment_sync instead; this
        makes them disappear from the pool entirely."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                with self._sync_write_lock:
                    conn.execute(
                        "DELETE FROM cwl_signups WHERE event_id = ? AND player_tag = ?",
                        (event_id, player_tag),
                    )
                    if self._should_commit():
                        conn.commit()
                return True
            except sqlite3.Error as e:
                logging.error(f"[DB-WRITE-SYNC] delete_cwl_signup_sync failed for event {event_id} player {player_tag}: {e}")
                conn.rollback()
                return False

    def mark_cwl_signup_as_shared_clan_guest_sync(
        self, event_id: int, player_tag: str, player_name: Optional[str], discord_id: Optional[str], origin_shared_clan_id: int
    ) -> bool:
        """Cross-guild foreign-guest conversion (2026-08-15, project owner's spec): creates-or-
        flips a cwl_signups row to source='guest_invite' (the same marker a manually invited
        guest player already gets — is_guest is derived from source alone, so this is all the
        frontend needs to show the guest badge) and stamps origin_shared_clan_id so
        find_cwl_signups_by_origin_shared_clan_sync can find it again later if the clan's real
        owning guild reassigns this exact player elsewhere.

        A true upsert, not a plain UPDATE — a player dragged straight from the live-membership
        pool onto a private clan column never gets a cwl_signups row at all (only a cwl_assignments
        one; handle_post_cwl_enrollment_assign's private-clan branch doesn't touch cwl_signups),
        so this is the ONLY row that will ever exist for such a player and must actually create
        it, not silently no-op against a row that was never there. On conflict, only status is
        preserved from any existing row (a real prior response shouldn't be reset to 'pending'
        just because they're being reclassified as a guest) — everything else here always wins,
        since this call is the freshest information about who this player is."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                with self._sync_write_lock:
                    conn.execute(
                        """
                        INSERT INTO cwl_signups
                            (event_id, player_tag, player_name, discord_id, source, status, origin_shared_clan_id)
                        VALUES (?, ?, ?, ?, 'guest_invite', 'pending', ?)
                        ON CONFLICT(event_id, player_tag) DO UPDATE SET
                            player_name = excluded.player_name,
                            discord_id = excluded.discord_id,
                            source = excluded.source,
                            origin_shared_clan_id = excluded.origin_shared_clan_id
                        """,
                        (event_id, player_tag, player_name, discord_id, origin_shared_clan_id),
                    )
                    if self._should_commit():
                        conn.commit()
                return True
            except sqlite3.Error as e:
                logging.error(
                    f"[DB-WRITE-SYNC] mark_cwl_signup_as_shared_clan_guest_sync failed for event {event_id} player {player_tag}: {e}"
                )
                conn.rollback()
                return False

    def find_cwl_signups_by_origin_shared_clan_sync(self, origin_shared_clan_id: int, player_tag: str) -> List[Dict[str, Any]]:
        """Every OTHER guild's local cwl_signups row that traces its placement of player_tag back
        to a shared clan (see mark_cwl_signup_as_shared_clan_guest_sync) — scans ACROSS events/
        guilds by design (unlike every other cwl_signups query in this file, which is always
        event-scoped), since the whole point is finding foreign guests regardless of which guild
        they ended up in. Used to purge them once the clan's real owning guild reassigns this
        exact player elsewhere (project owner's spec: "removed from the guild clan's player
        roster and player pool")."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                rows = conn.execute(
                    "SELECT * FROM cwl_signups WHERE origin_shared_clan_id = ? AND player_tag = ?",
                    (origin_shared_clan_id, player_tag),
                ).fetchall()
                return [dict(row) for row in rows]
            except sqlite3.Error as e:
                logging.error(
                    f"[DB-QUERY-SYNC] find_cwl_signups_by_origin_shared_clan_sync failed for shared_clan "
                    f"{origin_shared_clan_id} player {player_tag}: {e}"
                )
                return []

    def upsert_cwl_assignment_sync(
        self,
        event_id: int,
        player_tag: str,
        assigned_clan_tag: str,
        assignment_source: str = "suggested",
        suggested_clan_tag: Optional[str] = None,
        locked: bool = False,
    ) -> bool:
        """Create or overwrite one player's clan assignment for an event — the write path for
        both the auto-assignment seed (assignment_source='suggested') and a manual drag-and-drop
        move on the "Manage Enrollment" board (assignment_source='admin_override', locked=True).
        "Unassigned" is represented by the row's absence (assigned_clan_tag is NOT NULL) rather
        than a nullable column — see delete_cwl_assignment_sync() for the inverse."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                with self._sync_write_lock:
                    conn.execute(
                        """
                        INSERT INTO cwl_assignments
                            (event_id, player_tag, assigned_clan_tag, suggested_clan_tag, assignment_source, locked)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(event_id, player_tag) DO UPDATE SET
                            assigned_clan_tag = excluded.assigned_clan_tag,
                            suggested_clan_tag = excluded.suggested_clan_tag,
                            assignment_source = excluded.assignment_source,
                            locked = excluded.locked,
                            updated_at = datetime('now')
                        """,
                        (event_id, player_tag, assigned_clan_tag, suggested_clan_tag, assignment_source, 1 if locked else 0),
                    )
                    if self._should_commit():
                        conn.commit()
                return True
            except sqlite3.Error as e:
                logging.error(f"[DB-WRITE-SYNC] upsert_cwl_assignment_sync failed for event {event_id} player {player_tag}: {e}")
                conn.rollback()
                return False

    def bulk_create_cwl_assignments_sync(self, event_id: int, assignments: List[Dict[str, Any]]) -> bool:
        """Bulk-insert cwl_assignments rows for the auto-assignment seed (Start Enrollment,
        resolve_prior_cwl_assignments() in QBdiscocmdshelper_cwl.py). Idempotent via
        ON CONFLICT(event_id, player_tag) DO NOTHING — the seed runs once at Start Enrollment
        time; re-running it (defensively) must never clobber a manual drag-and-drop move that
        happened since. Each dict: player_tag, assigned_clan_tag, and optionally
        suggested_clan_tag (defaults to assigned_clan_tag — the seed's suggestion *is* the
        initial assignment)."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        if not assignments:
            return True

        with self._sync_conn() as conn:
            try:
                with self._sync_write_lock:
                    conn.executemany(
                        """
                        INSERT INTO cwl_assignments
                            (event_id, player_tag, assigned_clan_tag, suggested_clan_tag, assignment_source)
                        VALUES (?, ?, ?, ?, 'suggested')
                        ON CONFLICT(event_id, player_tag) DO NOTHING
                        """,
                        [
                            (
                                event_id,
                                a["player_tag"],
                                a["assigned_clan_tag"],
                                a.get("suggested_clan_tag", a["assigned_clan_tag"]),
                            )
                            for a in assignments
                        ],
                    )
                    if self._should_commit():
                        conn.commit()
                return True
            except sqlite3.Error as e:
                logging.error(f"[DB-WRITE-SYNC] bulk_create_cwl_assignments_sync failed for event {event_id}: {e}")
                conn.rollback()
                return False

    def get_cwl_assignments_sync(self, event_id: int) -> List[Dict[str, Any]]:
        """Return every current assignment for an event (one row per assigned player_tag — an
        unassigned player simply has no row here)."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                rows = conn.execute(
                    "SELECT * FROM cwl_assignments WHERE event_id = ?",
                    (event_id,),
                ).fetchall()
                return [dict(row) for row in rows]
            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] get_cwl_assignments_sync failed for event {event_id}: {e}")
                return []

    def delete_cwl_assignment_sync(self, event_id: int, player_tag: str) -> bool:
        """Remove one player's assignment — the "drag to Unassigned" action on the Manage
        Enrollment board, and the cascade a signup withdrawal triggers (a withdrawn player
        shouldn't linger in a clan column)."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                with self._sync_write_lock:
                    conn.execute(
                        "DELETE FROM cwl_assignments WHERE event_id = ? AND player_tag = ?",
                        (event_id, player_tag),
                    )
                    if self._should_commit():
                        conn.commit()
                return True
            except sqlite3.Error as e:
                logging.error(f"[DB-WRITE-SYNC] delete_cwl_assignment_sync failed for event {event_id} player {player_tag}: {e}")
                conn.rollback()
                return False

    # ------------------------------------------------------------------
    # Cross-guild shared CWL clans (2026-08-15) — see cwl_shared_clans'
    # CREATE TABLE comment above for the design rationale.
    # ------------------------------------------------------------------

    def find_cwl_clan_participation_across_guilds_sync(
        self, clan_tag: str, season: str, exclude_guild_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Every OTHER guild currently participating with clan_tag for this season — scans
        across ALL guilds' cwl_events for the season, not just the caller's own. This is the
        "is this clan already claimed elsewhere" check both trigger points
        (_search_cwl_guests_sync/handle_post_clan_config, start_cwl_enrollment) use before deciding
        whether to invoke ensure_cwl_clan_sharing(). Returns one dict per matching guild:
        guild_id, event_id, participating (always 1 — non-participating rows never count as a
        "claim")."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                if exclude_guild_id is not None:
                    rows = conn.execute(
                        """
                        SELECT ce.guild_id AS guild_id, ce.id AS event_id, cec.participating AS participating
                        FROM cwl_event_clans cec
                        JOIN cwl_events ce ON ce.id = cec.event_id
                        WHERE cec.clan_tag = ? AND ce.cwl_season = ? AND cec.participating = 1
                              AND ce.guild_id != ?
                        """,
                        (clan_tag, season, exclude_guild_id),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT ce.guild_id AS guild_id, ce.id AS event_id, cec.participating AS participating
                        FROM cwl_event_clans cec
                        JOIN cwl_events ce ON ce.id = cec.event_id
                        WHERE cec.clan_tag = ? AND ce.cwl_season = ? AND cec.participating = 1
                        """,
                        (clan_tag, season),
                    ).fetchall()
                return [dict(row) for row in rows]
            except sqlite3.Error as e:
                logging.error(
                    f"[DB-QUERY-SYNC] find_cwl_clan_participation_across_guilds_sync failed for {clan_tag}/{season}: {e}"
                )
                return []

    def get_cwl_shared_clan_sync(self, clan_tag: str, season: str) -> Optional[Dict[str, Any]]:
        """The cwl_shared_clans row for (clan_tag, season), or None if this clan isn't
        (yet) shared for that season."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                row = conn.execute(
                    "SELECT * FROM cwl_shared_clans WHERE clan_tag = ? AND cwl_season = ?",
                    (clan_tag, season),
                ).fetchone()
                return dict(row) if row else None
            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] get_cwl_shared_clan_sync failed for {clan_tag}/{season}: {e}")
                return None

    def get_cwl_shared_clan_by_id_sync(self, shared_clan_id: int) -> Optional[Dict[str, Any]]:
        """Same as get_cwl_shared_clan_sync but by primary key — used once a caller already has
        a shared_clan_id (e.g. from list_cwl_shared_clan_guilds_sync) and needs the parent row's
        owner_guild_id for a permission check."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                row = conn.execute(
                    "SELECT * FROM cwl_shared_clans WHERE id = ?", (shared_clan_id,)
                ).fetchone()
                return dict(row) if row else None
            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] get_cwl_shared_clan_by_id_sync failed for id {shared_clan_id}: {e}")
                return None

    def create_cwl_shared_clan_sync(
        self, clan_tag: str, season: str, owner_guild_id: str, owner_event_id: int, owner_resolution_method: str
    ) -> Optional[int]:
        """Create the cwl_shared_clans row the first time a clan becomes multi-guild for a
        season. Callers must check get_cwl_shared_clan_sync() first — this is a plain INSERT,
        not an upsert, since the (clan_tag, cwl_season) UNIQUE constraint means a second call
        for the same pair is a caller bug, not a race to paper over silently (unlike most
        upsert-style CRUD elsewhere in this feature). Returns the new row's id, or None on
        failure."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                with self._sync_write_lock:
                    cursor = conn.execute(
                        """
                        INSERT INTO cwl_shared_clans
                            (clan_tag, cwl_season, owner_guild_id, owner_event_id,
                             owner_resolution_method, owner_resolved_at)
                        VALUES (?, ?, ?, ?, ?, datetime('now'))
                        """,
                        (clan_tag, season, owner_guild_id, owner_event_id, owner_resolution_method),
                    )
                    if self._should_commit():
                        conn.commit()
                    return cursor.lastrowid
            except sqlite3.Error as e:
                logging.error(f"[DB-WRITE-SYNC] create_cwl_shared_clan_sync failed for {clan_tag}/{season}: {e}")
                conn.rollback()
                return None

    def repoint_cwl_shared_clan_owner_sync(
        self, shared_clan_id: int, new_owner_guild_id: str, new_owner_event_id: int, resolution_method: str
    ) -> bool:
        """Reassign ownership — used both by a fresh resolve_cwl_clan_owner() result and by the
        delete-season guard's repoint-before-prune step when the current owner's guild is the
        one leaving cwl_shared_clan_guilds."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                with self._sync_write_lock:
                    conn.execute(
                        """
                        UPDATE cwl_shared_clans
                        SET owner_guild_id = ?, owner_event_id = ?, owner_resolution_method = ?,
                            owner_resolved_at = datetime('now'), updated_at = datetime('now')
                        WHERE id = ?
                        """,
                        (new_owner_guild_id, new_owner_event_id, resolution_method, shared_clan_id),
                    )
                    if self._should_commit():
                        conn.commit()
                return True
            except sqlite3.Error as e:
                logging.error(f"[DB-WRITE-SYNC] repoint_cwl_shared_clan_owner_sync failed for id {shared_clan_id}: {e}")
                conn.rollback()
                return False

    def add_guild_to_shared_clan_sync(self, shared_clan_id: int, guild_id: str, event_id: int) -> bool:
        """Attach a guild to an already-shared clan's membership list — idempotent (a guild
        re-saving its clan config, or Start Enrollment running again, must not error on an
        already-attached guild)."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                with self._sync_write_lock:
                    conn.execute(
                        """
                        INSERT INTO cwl_shared_clan_guilds (shared_clan_id, guild_id, event_id)
                        VALUES (?, ?, ?)
                        ON CONFLICT(shared_clan_id, guild_id) DO NOTHING
                        """,
                        (shared_clan_id, guild_id, event_id),
                    )
                    if self._should_commit():
                        conn.commit()
                return True
            except sqlite3.Error as e:
                logging.error(f"[DB-WRITE-SYNC] add_guild_to_shared_clan_sync failed for shared_clan {shared_clan_id}: {e}")
                conn.rollback()
                return False

    def list_cwl_shared_clan_guilds_sync(self, shared_clan_id: int) -> List[Dict[str, Any]]:
        """Every guild currently attached to a shared clan (owner included) — used for eviction
        eligibility, notification fan-out, and the delete-season guard's "am I the last one"
        check."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                rows = conn.execute(
                    "SELECT * FROM cwl_shared_clan_guilds WHERE shared_clan_id = ? ORDER BY joined_at",
                    (shared_clan_id,),
                ).fetchall()
                return [dict(row) for row in rows]
            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] list_cwl_shared_clan_guilds_sync failed for shared_clan {shared_clan_id}: {e}")
                return []

    def remove_guild_from_shared_clan_sync(self, shared_clan_id: int, target_guild_id: str) -> bool:
        """Owner-only eviction's DB half — removes target_guild_id from a shared clan's
        membership list. Never touches cwl_shared_clans/cwl_shared_clan_players (the shared
        roster survives; only this guild's participation ends). The caller is responsible for
        also flipping the target guild's own cwl_event_clans.participating to False (this
        function only owns the shared-membership side) and, if target_guild_id was the owner,
        for repointing ownership first via repoint_cwl_shared_clan_owner_sync — this function
        does not check or care who the owner currently is."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                with self._sync_write_lock:
                    conn.execute(
                        "DELETE FROM cwl_shared_clan_guilds WHERE shared_clan_id = ? AND guild_id = ?",
                        (shared_clan_id, target_guild_id),
                    )
                    if self._should_commit():
                        conn.commit()
                return True
            except sqlite3.Error as e:
                logging.error(
                    f"[DB-WRITE-SYNC] remove_guild_from_shared_clan_sync failed for shared_clan {shared_clan_id} guild {target_guild_id}: {e}"
                )
                conn.rollback()
                return False

    def delete_cwl_shared_clan_sync(self, shared_clan_id: int) -> bool:
        """Fully deletes a shared clan record (cascades to cwl_shared_clan_guilds and
        cwl_shared_clan_players) — only ever called by the delete-season guard, and only once
        it's confirmed the deleting guild is the LAST one still attached (see
        CWL_ROSTER_PLANNING_PLAN.md's delete-season guard). Never called as part of ordinary
        eviction — see remove_guild_from_shared_clan_sync for that."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                with self._sync_write_lock:
                    conn.execute("DELETE FROM cwl_shared_clans WHERE id = ?", (shared_clan_id,))
                    if self._should_commit():
                        conn.commit()
                return True
            except sqlite3.Error as e:
                logging.error(f"[DB-WRITE-SYNC] delete_cwl_shared_clan_sync failed for id {shared_clan_id}: {e}")
                conn.rollback()
                return False

    def get_cwl_shared_clan_players_sync(self, shared_clan_id: int) -> List[Dict[str, Any]]:
        """The shared roster for one shared clan — visible identically from every attached
        guild's board (see _build_enrollment_payload's merge)."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                rows = conn.execute(
                    "SELECT * FROM cwl_shared_clan_players WHERE shared_clan_id = ? ORDER BY player_name",
                    (shared_clan_id,),
                ).fetchall()
                return [dict(row) for row in rows]
            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] get_cwl_shared_clan_players_sync failed for shared_clan {shared_clan_id}: {e}")
                return []

    def find_cwl_shared_clan_memberships_for_player_sync(self, season: str, player_tag: str) -> List[Dict[str, Any]]:
        """Every shared clan (for this season) where player_tag is currently PLACED — i.e.
        assigned=1, not status='confirmed' (2026-08-16 follow-up: this is purely an occupancy/
        placement question — every caller uses it to find and evict a conflicting PLACEMENT
        before making a new one, never to check on a genuine response — see
        cwl_shared_clan_players' own CREATE TABLE comment for the full status/assigned split
        rationale). Deliberately player-scoped, not scoped to any one guild's currently-
        participating clans (unlike get_event_shared_clans_by_tag_sync, which
        handle_post_cwl_enrollment_assign used to rely on exclusively for this). 2026-08-16
        live-testing regression fix: dragging a player OUT of the "Assigned to other Guild"
        pseudo-column (enrollmentBoard.ts) — whose underlying clan_tag is, by definition, no
        longer one of the acting guild's participating clans — meant the old participating-scoped
        lookup never found (or cleared) their real cwl_shared_clan_players row at all. Left
        assigned there forever, it would silently "win" again the next time that clan got
        reactivated and its roster re-synced, undoing the reassignment the admin had just made.
        Scanning by player_tag instead finds a shared placement regardless of whether the clan
        happens to be a live column for this guild right now, so it gets cleared no matter which
        column the player was dragged out of."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                rows = conn.execute(
                    """
                    SELECT csc.id AS shared_clan_id, csc.clan_tag AS clan_tag
                    FROM cwl_shared_clan_players cscp
                    JOIN cwl_shared_clans csc ON csc.id = cscp.shared_clan_id
                    WHERE cscp.player_tag = ? AND csc.cwl_season = ? AND cscp.assigned = 1
                    """,
                    (player_tag, season),
                ).fetchall()
                return [dict(row) for row in rows]
            except sqlite3.Error as e:
                logging.error(
                    f"[DB-QUERY-SYNC] find_cwl_shared_clan_memberships_for_player_sync failed for player {player_tag}: {e}"
                )
                return []

    def set_cwl_shared_clan_player_status_sync(
        self,
        shared_clan_id: int,
        player_tag: str,
        player_name: Optional[str],
        discord_id: Optional[str],
        status: str,
        source: str,
        added_by_guild_id: str,
        responded_at: Optional[str] = None,
    ) -> bool:
        """Record a player's genuine RESPONSE (pending/confirmed/declined/withdrawn) — and ONLY
        that (2026-08-16, live-testing feedback, project owner's spec, verbatim: "Confirmation
        status and assignment status should be treated completely separate. the one has a totally
        different meaning logically than the other!"). Deliberately never touches `assigned` —
        the SET clause below doesn't mention it, so it's simply preserved on an existing row, and
        a brand-new row gets the column's own DEFAULT 0 (not placed in any column just because
        someone recorded a response). See set_cwl_shared_clan_player_assignment_sync for the
        other half of this split, and cwl_shared_clan_players' own CREATE TABLE comment for the
        full rationale. Either attached guild's admin/leader may call this (sharing is symmetric
        for editing; only eviction is owner-gated). added_by_guild_id is audit-only, always
        overwritten to whichever guild's action most recently touched the row."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                with self._sync_write_lock:
                    conn.execute(
                        """
                        INSERT INTO cwl_shared_clan_players
                            (shared_clan_id, player_tag, player_name, discord_id, status, source,
                             added_by_guild_id, responded_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(shared_clan_id, player_tag) DO UPDATE SET
                            player_name = excluded.player_name,
                            discord_id = excluded.discord_id,
                            status = excluded.status,
                            source = excluded.source,
                            added_by_guild_id = excluded.added_by_guild_id,
                            responded_at = excluded.responded_at,
                            updated_at = datetime('now')
                        """,
                        (shared_clan_id, player_tag, player_name, discord_id, status, source,
                         added_by_guild_id, responded_at),
                    )
                    if self._should_commit():
                        conn.commit()
                return True
            except sqlite3.Error as e:
                logging.error(
                    f"[DB-WRITE-SYNC] set_cwl_shared_clan_player_status_sync failed for shared_clan {shared_clan_id} player {player_tag}: {e}"
                )
                conn.rollback()
                return False

    def set_cwl_shared_clan_player_assignment_sync(
        self,
        shared_clan_id: int,
        player_tag: str,
        player_name: Optional[str],
        discord_id: Optional[str],
        assigned: bool,
        source: str,
        added_by_guild_id: str,
    ) -> bool:
        """Place or unplace a player in this shared clan's column — and ONLY that (2026-08-16,
        live-testing feedback, project owner's spec — see set_cwl_shared_clan_player_status_sync's
        docstring for the full rationale; "The symbols in the player tile should exclusively
        reflect confirmation status. The assignment status is obvious to the user from the column
        the player tile appears in."). Deliberately never touches `status`/`responded_at` — the
        SET clause below doesn't mention them, so an existing row's genuine response is preserved
        untouched, and a brand-new row gets the column's own DEFAULT 'pending' (an auto-assign or
        drag-and-drop placement is never itself a confirmation). Drag-and-drop and the auto-assign
        seed (assign_cwl_player_sync, QBdiscocmdshelper_cwl.py) are the only callers."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                with self._sync_write_lock:
                    conn.execute(
                        """
                        INSERT INTO cwl_shared_clan_players
                            (shared_clan_id, player_tag, player_name, discord_id, assigned, source,
                             added_by_guild_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(shared_clan_id, player_tag) DO UPDATE SET
                            player_name = excluded.player_name,
                            discord_id = excluded.discord_id,
                            assigned = excluded.assigned,
                            source = excluded.source,
                            added_by_guild_id = excluded.added_by_guild_id,
                            updated_at = datetime('now')
                        """,
                        (shared_clan_id, player_tag, player_name, discord_id, 1 if assigned else 0, source,
                         added_by_guild_id),
                    )
                    if self._should_commit():
                        conn.commit()
                return True
            except sqlite3.Error as e:
                logging.error(
                    f"[DB-WRITE-SYNC] set_cwl_shared_clan_player_assignment_sync failed for shared_clan {shared_clan_id} player {player_tag}: {e}"
                )
                conn.rollback()
                return False

    def delete_cwl_shared_clan_player_sync(self, shared_clan_id: int, player_tag: str) -> bool:
        """Remove one player from a shared clan's roster — the shared-clan equivalent of
        delete_cwl_assignment_sync's "drag to Unassigned" (a shared clan has no separate
        assignments table, so removing the row IS unassigning them)."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                with self._sync_write_lock:
                    conn.execute(
                        "DELETE FROM cwl_shared_clan_players WHERE shared_clan_id = ? AND player_tag = ?",
                        (shared_clan_id, player_tag),
                    )
                    if self._should_commit():
                        conn.commit()
                return True
            except sqlite3.Error as e:
                logging.error(
                    f"[DB-WRITE-SYNC] delete_cwl_shared_clan_player_sync failed for shared_clan {shared_clan_id} player {player_tag}: {e}"
                )
                conn.rollback()
                return False

    async def get_clan_attack_history(
        self,
        clan_tag: str,
        month: Optional[int] = None,
        year: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Async wrapper around get_clan_attack_history_sync for scripts."""
        return self.get_clan_attack_history_sync(clan_tag, month=month, year=year)

    async def get_all_war_clan_tags(self) -> List[str]:
        """Return distinct clan_tags from war_summary (async)."""
        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        with self._sync_conn() as conn:
            rows = conn.execute(
                "SELECT clan_tag FROM main.war_summary "
                "UNION SELECT clan_tag FROM history.war_summary "
                "ORDER BY clan_tag"
            ).fetchall()
            return [r["clan_tag"] for r in rows]

    def get_clan_attack_history_sync(
        self,
        clan_tag: str,
        month: Optional[int] = None,
        year: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Read per-attack rows from war_attacks and aggregate into the
        WarStatsDict-compatible format that _merge_entries / calculate_leaderboard expect.

        Returns list of dicts with keys:
            WarID, Date, Player, PlayerID, TH_lvl, Stars, Attacks,
            Missed_Attacks, Max_Attacks, Defensive_Stars, Total_Dest_Pct
        """
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        # Build date filter
        params: list[Any] = [clan_tag]
        date_clause = ""
        if month is not None and year is not None:
            start_date = f"{year:04d}-{month:02d}-01"
            end_date = f"{year + 1:04d}-01-01" if month == 12 else f"{year:04d}-{month + 1:02d}-01"
            date_clause = " AND date >= ? AND date < ?"
            params.extend([start_date, end_date])
        elif year is not None:
            start_date = f"{year:04d}-01-01"
            end_date = f"{year + 1:04d}-01-01"
            date_clause = " AND date >= ? AND date < ?"
            params.extend([start_date, end_date])

        with self._sync_conn() as conn:
            try:
                wa_cols = self._explicit_column_list_sync(conn, "war_attacks")
                # Aggregate from war_attacks (attack_order > 0 = actual attacks)
                cursor = conn.execute(f"""
                    WITH wa AS (
                        SELECT {wa_cols} FROM main.war_attacks
                        UNION ALL SELECT {wa_cols} FROM history.war_attacks
                    )
                    SELECT war_id, date, player_name, player_tag, th_level,
                       SUM(stars)                                            AS stars,
                       COALESCE(MAX(max_attacks), 0)
                           - COALESCE(MAX(missed_attacks), 0)                AS attacks,
                       MAX(missed_attacks)                                   AS missed_attacks,
                       MAX(max_attacks)                                      AS max_attacks,
                       MAX(defensive_stars)                                  AS defensive_stars,
                       MAX(times_defended)                                   AS times_defended,
                       SUM(destruction)                                      AS total_destruction
                FROM wa
                WHERE clan_tag = ?{date_clause}
                  AND attack_order > 0
                GROUP BY war_id, player_tag
                ORDER BY date DESC
                """, params)
                agg_rows = cursor.fetchall()

                records: list[Dict[str, Any]] = []
                for row in agg_rows:
                    records.append({
                        "WarID": row["war_id"],
                        "Date": row["date"],
                        "Player": row["player_name"],
                        "PlayerID": row["player_tag"],
                        "TH_lvl": row["th_level"],
                        "Stars": row["stars"],
                        "Attacks": row["attacks"],
                        "Missed_Attacks": row["missed_attacks"],
                        "Max_Attacks": row["max_attacks"],
                        "Defensive_Stars": row["defensive_stars"],
                        "Times_Defended": int(row["times_defended"] or 0),
                        "Total_Dest_Pct": float(row["total_destruction"] or 0.0),
                    })

                # Include players with 0 attacks (attack_order == 0 = missed all)
                cursor2 = conn.execute(f"""
                    WITH wa AS (
                        SELECT {wa_cols} FROM main.war_attacks
                        UNION ALL SELECT {wa_cols} FROM history.war_attacks
                    )
                    SELECT war_id, date, player_name, player_tag, th_level,
                           0 AS stars, 0 AS attacks,
                           missed_attacks, max_attacks, defensive_stars, times_defended
                    FROM wa
                    WHERE clan_tag = ?{date_clause}
                      AND attack_order = 0
                    ORDER BY date DESC
                """, params)
                for row in cursor2.fetchall():
                    records.append({
                        "WarID": row["war_id"],
                        "Date": row["date"],
                        "Player": row["player_name"],
                        "PlayerID": row["player_tag"],
                        "TH_lvl": row["th_level"],
                        "Stars": 0,
                        "Attacks": 0,
                        "Missed_Attacks": row["missed_attacks"],
                        "Max_Attacks": row["max_attacks"],
                        "Defensive_Stars": row["defensive_stars"],
                        "Times_Defended": int(row["times_defended"] or 0),
                        "Total_Dest_Pct": 0.0,
                    })

                logging.debug(
                    f"[DB-QUERY-SYNC] war_attacks: {len(records)} records for {clan_tag}"
                )
                return records

            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] get_clan_attack_history_sync failed: {e}")
                raise

    def get_player_attack_history_sync(
        self,
        player_tags: List[str],
        month: Optional[int] = None,
        year: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Cross-clan variant of get_clan_attack_history_sync: aggregates war_attacks
        for a set of player tags regardless of which clan_tag they fought under.

        Used by the leaderboard "all clans" scope so that a player who has since
        joined/switched to a currently-tracked clan still gets credit for stars
        earned while registered to a clan that is no longer tracked/subscribed.

        Uses idx_wa_player_tag_date(player_tag, date) — a composite index so the
        date-range filter is applied inside the index range scan itself. Without
        date in the index, SQLite still narrows by player_tag first (via the older
        single-column idx_wa_player_tag) but then has to rowid-fetch every row that
        player ever has in war_attacks, across all time, just to discard everything
        outside the requested month — for a long-tenured player with a large full
        history, that's the difference between reading a handful of rows and
        reading thousands, multiplied by every player_tag in the roster.

        WarID is returned as "{clan_tag}::{war_id}" (composite) because the raw
        war_id is only unique per (war_id, clan_tag) pair (it's derived from the
        opponent tag + date, from that clan's point of view), so two different
        home clans could otherwise collide on the same war_id string.
        """
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        if not player_tags:
            return []

        date_clause = ""
        date_params: list[Any] = []
        if month is not None and year is not None:
            start_date = f"{year:04d}-{month:02d}-01"
            end_date = f"{year + 1:04d}-01-01" if month == 12 else f"{year:04d}-{month + 1:02d}-01"
            date_clause = " AND date >= ? AND date < ?"
            date_params = [start_date, end_date]
        elif year is not None:
            start_date = f"{year:04d}-01-01"
            end_date = f"{year + 1:04d}-01-01"
            date_clause = " AND date >= ? AND date < ?"
            date_params = [start_date, end_date]

        # Chunk to stay well under SQLite's default host-parameter limit (999)
        # even for large clan families.
        records: list[Dict[str, Any]] = []
        _CHUNK = 400
        tags_list = list(player_tags)
        with self._sync_conn() as conn:
            try:
                wa_cols = self._explicit_column_list_sync(conn, "war_attacks")
                for i in range(0, len(tags_list), _CHUNK):
                    chunk = tags_list[i:i + _CHUNK]
                    placeholders = ",".join("?" for _ in chunk)
                    params: list[Any] = list(chunk) + date_params

                    cursor = conn.execute(f"""
                        WITH wa AS (
                            SELECT {wa_cols} FROM main.war_attacks
                            UNION ALL SELECT {wa_cols} FROM history.war_attacks
                        )
                        SELECT clan_tag || '::' || war_id           AS composite_war_id,
                               date, player_name, player_tag, th_level,
                           SUM(stars)                                            AS stars,
                           COALESCE(MAX(max_attacks), 0)
                               - COALESCE(MAX(missed_attacks), 0)                AS attacks,
                           MAX(missed_attacks)                                   AS missed_attacks,
                           MAX(max_attacks)                                      AS max_attacks,
                           MAX(defensive_stars)                                  AS defensive_stars,
                           MAX(times_defended)                                   AS times_defended,
                           SUM(destruction)                                      AS total_destruction
                        FROM wa
                        WHERE player_tag IN ({placeholders}){date_clause}
                          AND attack_order > 0
                        GROUP BY war_id, clan_tag, player_tag
                        ORDER BY date DESC
                    """, params)

                    for row in cursor.fetchall():
                        records.append({
                            "WarID": row["composite_war_id"],
                            "Date": row["date"],
                            "Player": row["player_name"],
                            "PlayerID": row["player_tag"],
                            "TH_lvl": row["th_level"],
                            "Stars": row["stars"],
                            "Attacks": row["attacks"],
                            "Missed_Attacks": row["missed_attacks"],
                            "Max_Attacks": row["max_attacks"],
                            "Defensive_Stars": row["defensive_stars"],
                            "Times_Defended": int(row["times_defended"] or 0),
                            "Total_Dest_Pct": float(row["total_destruction"] or 0.0),
                        })

                    # Include players with 0 attacks (attack_order == 0 = missed all)
                    cursor2 = conn.execute(f"""
                        WITH wa AS (
                            SELECT {wa_cols} FROM main.war_attacks
                            UNION ALL SELECT {wa_cols} FROM history.war_attacks
                        )
                        SELECT clan_tag || '::' || war_id AS composite_war_id,
                               date, player_name, player_tag, th_level,
                               0 AS stars, 0 AS attacks,
                               missed_attacks, max_attacks, defensive_stars, times_defended
                        FROM wa
                        WHERE player_tag IN ({placeholders}){date_clause}
                          AND attack_order = 0
                        ORDER BY date DESC
                    """, params)
                    for row in cursor2.fetchall():
                        records.append({
                            "WarID": row["composite_war_id"],
                            "Date": row["date"],
                            "Player": row["player_name"],
                            "PlayerID": row["player_tag"],
                            "TH_lvl": row["th_level"],
                            "Stars": 0,
                            "Attacks": 0,
                            "Missed_Attacks": row["missed_attacks"],
                            "Max_Attacks": row["max_attacks"],
                            "Defensive_Stars": row["defensive_stars"],
                            "Times_Defended": int(row["times_defended"] or 0),
                            "Total_Dest_Pct": 0.0,
                        })

                logging.debug(
                    f"[DB-QUERY-SYNC] player war_attacks: {len(records)} records for {len(tags_list)} player tags"
                )
                return records

            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] get_player_attack_history_sync failed: {e}")
                raise

    def get_war_summaries_sync(
        self, clan_tag: Optional[str], *, season: Optional[str] = None, is_cwl: Optional[bool] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Retrieve war_summary rows for a clan, or (with clan_tag=None) across all clans.

        Args:
            clan_tag: Clan tag, or None to query across every clan (used for
                cross-clan CWL-season lookups by the "all clans" leaderboard scope)
            season: Optional CWL season filter, e.g. '2026-02'
            is_cwl: Optional filter — True = CWL only, False = regular only
            limit: Max rows to return
        Returns:
            List of dicts matching war_summary columns.
        """
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        clauses: list[str] = []
        params: list[Any] = []
        if clan_tag is not None:
            clauses.append("clan_tag = ?")
            params.append(clan_tag)
        if season is not None:
            clauses.append("cwl_season = ?")
            params.append(season)
        if is_cwl is not None:
            clauses.append("is_cwl = ?")
            params.append(1 if is_cwl else 0)
        where = " AND ".join(clauses) if clauses else "1=1"

        with self._sync_conn() as conn:
            try:
                ws_cols = self._explicit_column_list_sync(conn, "war_summary")
                sql = (
                    "WITH ws AS ("
                    f"SELECT {ws_cols} FROM main.war_summary UNION ALL SELECT {ws_cols} FROM history.war_summary"
                    f") SELECT * FROM ws WHERE {where} ORDER BY date ASC"
                )
                if limit:
                    sql += f" LIMIT {int(limit)}"
                rows = conn.execute(sql, params).fetchall()
                return [dict(row) for row in rows]
            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] get_war_summaries_sync failed: {e}")
                return []

    def get_cwl_attack_records_sync(
        self, clan_tag: str, cwl_season: str
    ) -> List[Dict[str, Any]]:
        """
        Return individual attack rows for both sides of a clan's CWL season.

        Resolves ``defender_th`` via a global player_tag → MAX(th_level) sub-query
        so every attack has a real TH matchup without any API call.

        Two sets of rows are returned:
        * OUR CLAN  (``is_our_clan=True``)  — attacker is our member.
        * OPPONENT  (``is_our_clan=False``) — attacker is an opponent member, found
          by matching war_summary rows where ``clan_tag = our_opponent_tag`` AND
          ``opponent_tag = our_clan_tag`` AND ``date`` matches (same war, other
          side).  Only included when the opponent clan is also tracked in the DB.

        Defender TH falls back to attacker_th (equal-TH) when the defender player
        is not in the DB (opponents tracked in a different QapBot instance, etc.).

        Returns:
            List of dicts with keys:
                player_tag (str), player_name (str), th_level (int),
                stars (int), defender_tag (str), defender_th (int),
                is_our_clan (bool)
        """
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                wa_cols = self._explicit_column_list_sync(conn, "war_attacks")
                ws_cols = self._explicit_column_list_sync(conn, "war_summary")
                # ── Our clan's attacks ───────────────────────────────────────────────
                our_rows = conn.execute(f"""
                    WITH wa AS (
                        SELECT {wa_cols} FROM main.war_attacks
                        UNION ALL SELECT {wa_cols} FROM history.war_attacks
                    ), ws AS (
                        SELECT {ws_cols} FROM main.war_summary
                        UNION ALL SELECT {ws_cols} FROM history.war_summary
                    )
                    SELECT wa.player_tag, wa.player_name,
                           wa.th_level          AS attacker_th,
                           wa.stars,
                           wa.defender_tag,
                           COALESCE(NULLIF(wa.defender_th, 0), wa.th_level) AS defender_th
                    FROM wa
                    JOIN ws
                        ON wa.war_id   = ws.war_id
                       AND wa.clan_tag = ws.clan_tag
                    WHERE wa.clan_tag   = ?
                      AND ws.cwl_season = ?
                      AND ws.is_cwl     = 1
                      AND wa.attack_order > 0
                """, (clan_tag, cwl_season)).fetchall()

                # ── Opponent clan's attacks (if opponent is also tracked) ────────────
                opp_rows = conn.execute(f"""
                    WITH wa AS (
                        SELECT {wa_cols} FROM main.war_attacks
                        UNION ALL SELECT {wa_cols} FROM history.war_attacks
                    ), ws AS (
                        SELECT {ws_cols} FROM main.war_summary
                        UNION ALL SELECT {ws_cols} FROM history.war_summary
                    )
                    SELECT wa_opp.player_tag, wa_opp.player_name,
                           wa_opp.th_level          AS attacker_th,
                           wa_opp.map_position,
                           wa_opp.stars,
                           wa_opp.defender_tag,
                           COALESCE(NULLIF(wa_opp.defender_th, 0), wa_opp.th_level) AS defender_th,
                           ws_opp.clan_tag           AS opp_clan_tag,
                           ws_us.opponent_name        AS opp_clan_name
                    FROM ws ws_us
                    JOIN ws ws_opp
                        ON ws_opp.clan_tag   = ws_us.opponent_tag
                       AND ws_opp.cwl_season = ws_us.cwl_season
                       AND ws_opp.is_cwl     = 1
                    JOIN wa wa_opp
                        ON wa_opp.war_id   = ws_opp.war_id
                       AND wa_opp.clan_tag = ws_opp.clan_tag
                       AND wa_opp.attack_order > 0
                    WHERE ws_us.clan_tag   = ?
                      AND ws_us.cwl_season = ?
                      AND ws_us.is_cwl     = 1
                    ORDER BY ws_opp.clan_tag, wa_opp.map_position
                """, (clan_tag, cwl_season)).fetchall()

                result: List[Dict[str, Any]] = []
                for row in our_rows:
                    result.append({
                        "player_tag":  row["player_tag"],
                        "player_name": row["player_name"],
                        "th_level":    int(row["attacker_th"]),
                        "stars":       int(row["stars"]),
                        "defender_tag": row["defender_tag"],
                        "defender_th": int(row["defender_th"]),
                        "is_our_clan": True,
                    })
                for row in opp_rows:
                    result.append({
                        "player_tag":   row["player_tag"],
                        "player_name":  row["player_name"],
                        "th_level":     int(row["attacker_th"]),
                        "map_position": int(row["map_position"]),
                        "stars":        int(row["stars"]),
                        "defender_tag": row["defender_tag"],
                        "defender_th":  int(row["defender_th"]),
                        "is_our_clan":  False,
                        "opp_clan_tag": row["opp_clan_tag"],
                        "opp_clan_name": row["opp_clan_name"],
                    })
                return result
            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] get_cwl_attack_records_sync failed: {e}")
                return []

    def _upsert_player_name_search_rows_in_conn(self, conn: Any, tag_name_pairs: List[Tuple[str, str]]) -> None:
        """Upserts (player_tag, name) pairs into BOTH player_name_search (a plain table — real
        upsert support) and player_name_fts (FTS5 — confirmed 2026-08-17: virtual tables don't
        support `ON CONFLICT ... DO UPDATE`, "UPSERT not implemented for virtual table"; a
        confirmed-working DELETE-then-INSERT per row is the correct pattern instead — an
        UPDATE ... WHERE player_tag = ? also works directly against an UNINDEXED column, but
        delete+insert is simpler when a row may not exist yet, same call site either way).

        player_name_fts's DELETE/INSERT target `rowid`, not `player_tag` (2026-08-17 fix,
        see _fts_rowid_for_tag's own docstring) — `WHERE player_tag = ?` on an UNINDEXED FTS5
        column has no index to use and is a full table scan; at PROD's ~6.6M-row scale that
        turned every incremental flush into a full-table-scan storm that stalled the update
        cycle. rowid is computed as a pure function of the tag, so no read-back is needed.

        Callers must pass already-conflict-resolved (player_tag, name) pairs — this helper does
        no timestamp/recency comparison of its own; it just makes the search tables match
        whatever the caller says is current (2026-08-17, CWL_PROD_PERFORMANCE_FIX_PLAN.md
        Step 11)."""
        if not tag_name_pairs:
            return
        conn.executemany("""
            INSERT INTO player_name_search (player_tag, name, name_lower) VALUES (?, ?, ?)
            ON CONFLICT(player_tag) DO UPDATE SET
                name = excluded.name,
                name_lower = excluded.name_lower
        """, [(tag, name, name.lower()) for tag, name in tag_name_pairs])
        conn.executemany(
            "DELETE FROM player_name_fts WHERE rowid = ?",
            [(_fts_rowid_for_tag(tag),) for tag, _ in tag_name_pairs],
        )
        conn.executemany(
            "INSERT INTO player_name_fts (rowid, player_tag, name) VALUES (?, ?, ?)",
            [(_fts_rowid_for_tag(tag), tag, name) for tag, name in tag_name_pairs],
        )

    def _upsert_player_name_index_in_conn(
        self,
        conn: Any,
        attack_rows_iter: Any,
    ) -> None:
        """Upsert most-recent (player_tag, player_name, last_seen) into player_name_index.

        Called from within an existing connection/transaction in each of the
        war write paths.  ALL rows are indexed — including attack_order=0 sentinel
        rows (missed-all-attacks) — so players who never attacked are still findable
        and contribute to reliability/participation tracking.  Deduplicates to the
        most recent date per player_tag before upserting.

        Accepts both dict rows (add_war_data_sync / update_war_data_sync) and
        tuple rows (batch-flush all_attack_params).  Tuple column order matches
        the INSERT parameter list:
          (war_id[0], clan_tag[1], date[2], player_name[3], player_tag[4], ...)
        """
        best: dict[str, tuple[str, str]] = {}  # player_tag -> (player_name, date)
        for r in attack_rows_iter:
            if isinstance(r, dict):
                row = cast(Dict[str, Any], r)
                tag = str(row["PlayerID"] or "")
                name = str(row["Player"] or "")
                date = str(row["Date"] or "")
            else:
                row = cast(Tuple[Any, ...], r)
                tag = str(row[4] or "")
                name = str(row[3] or "")
                date = str(row[2] or "")
            existing_date = best.get(tag, ("", ""))[1]
            if date > existing_date:
                best[tag] = (name, date)
        if not best:
            return
        conn.executemany("""
            INSERT INTO player_name_index (player_tag, player_name, last_seen)
            VALUES (?, ?, ?)
            ON CONFLICT(player_tag) DO UPDATE SET
                player_name = excluded.player_name,
                last_seen   = excluded.last_seen
            WHERE excluded.last_seen > player_name_index.last_seen
        """, [(tag, name, date) for tag, (name, date) in best.items()])
        # player_name_search/player_name_fts mirror player_name_index (2026-08-17, Step 11) —
        # re-read the just-upserted rows' ACTUAL resulting names (not `best`'s own values
        # directly) since the WHERE guard above may have kept an existing, more-recent name for
        # some tag in this batch (a rare out-of-order war-save race); re-reading keeps the search
        # tables authoritative-consistent with player_name_index without duplicating its
        # newest-wins conflict logic here too. Chunked (Step 4) — this batch is normally one
        # war's worth of players, but a bulk-flush caller could hand this a much larger batch.
        resolved = self._chunked_in_query_sync(
            conn,
            "SELECT player_tag, player_name FROM player_name_index WHERE player_tag IN ({placeholders})",
            list(best.keys()),
        )
        self._upsert_player_name_search_rows_in_conn(conn, [(r["player_tag"], r["player_name"]) for r in resolved])

    def load_player_name_index_sync(self) -> Dict[str, str]:
        """Load the full player_name_index table into a {player_tag: player_name} dict.

        Called once at startup by cache_manager.load_all(). PROD's table has grown to millions
        of rows (~6.6M per the 2026-08-16 incident log — CWL_PROD_PERFORMANCE_FIX_PLAN.md — a
        stale "~125K rows" estimate here predates that growth); still small/fast relative to
        war_attacks, and this query itself isn't the bottleneck — see cache_manager.py's
        search_player_names()/CWL_PROD_PERFORMANCE_FIX_PLAN.md Step 9 for what was.
        """
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        with self._sync_conn() as conn:
            try:
                rows = conn.execute(
                    "SELECT player_tag, player_name FROM player_name_index"
                ).fetchall()
                return {row["player_tag"]: row["player_name"] for row in rows}
            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] load_player_name_index_sync failed: {e}")
                return {}

    def update_player_name_index_sync(
        self, updates: List[Tuple[str, str, str]]
    ) -> None:
        """Batch-upsert (player_tag, player_name, last_seen) into player_name_index.

        Used for API-detected name changes (coc_cache.update_player_info_in_user_accounts).
        The last_seen value should be the current UTC datetime so API-sourced names
        always supersede stale war-history names.
        """
        if not updates:
            return
        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        with self._sync_conn() as conn:
            try:
                with self._sync_write_lock:
                    conn.executemany("""
                        INSERT INTO player_name_index (player_tag, player_name, last_seen)
                        VALUES (?, ?, ?)
                        ON CONFLICT(player_tag) DO UPDATE SET
                            player_name = excluded.player_name,
                            last_seen   = excluded.last_seen
                        WHERE excluded.last_seen > player_name_index.last_seen
                    """, updates)
                    # Mirrors player_name_index into player_name_search/player_name_fts
                    # (2026-08-17, Step 11) — same re-read-to-resolve-conflicts reasoning as
                    # _upsert_player_name_index_in_conn's own call site.
                    tags = [u[0] for u in updates]
                    resolved = self._chunked_in_query_sync(
                        conn,
                        "SELECT player_tag, player_name FROM player_name_index WHERE player_tag IN ({placeholders})",
                        tags,
                    )
                    self._upsert_player_name_search_rows_in_conn(
                        conn, [(r["player_tag"], r["player_name"]) for r in resolved]
                    )
                    conn.commit()
            except Exception as e:
                logging.error(f"[DB-WRITE-SYNC] update_player_name_index_sync failed: {e}")

    def search_player_names_sync(self, query: str, limit: int = 25) -> List[Dict[str, str]]:
        """SQLite/FTS5-backed name-substring search over player_name_fts (2026-08-17,
        CWL_PROD_PERFORMANCE_FIX_PLAN.md Step 11) — the SQL-backed counterpart to
        CACHE.search_player_names()'s in-memory scan, used when
        CONFIG.cwl_use_fts_player_search is True. Same alphabetical-sort/25-cap contract as the
        in-memory version, but genuinely index-backed (no in-process bound like
        SEARCH_PLAYER_NAMES_MAX_COLLECT is needed — FTS5's trigram index finds matching rows
        without touching non-matching ones, so LIMIT is honored without an application-level
        early-exit).

        Trigram tokenization needs >=3 characters to form even one trigram, so a shorter query
        can structurally never match anything (verified empirically 2026-08-17) — returns []
        immediately rather than issuing a query guaranteed to find nothing.

        The query is FTS5-literal-quoted before use (confirmed 2026-08-17: an unquoted needle
        containing characters like `-`, `*`, `"`, or words like AND/OR/NOT gets parsed as FTS5
        query syntax instead of literal text — e.g. an unquoted "bob-smith" raises "no such
        column: smith" instead of matching the literal substring) — never build this query with
        the caller's raw string.

        Called synchronously — thread it from any event-loop caller (this mirrors
        CACHE.search_player_names()'s own threading requirement, not a new constraint)."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        query = query.strip()
        if len(query) < 3:
            return []
        literal = '"' + query.replace('"', '""') + '"'
        with self._sync_conn() as conn:
            try:
                rows = conn.execute("""
                    SELECT player_tag, name FROM player_name_fts
                    WHERE player_name_fts MATCH ?
                    ORDER BY name COLLATE NOCASE
                    LIMIT ?
                """, (literal, min(limit, 25))).fetchall()
                return [{"player_tag": row["player_tag"], "player_name": row["name"]} for row in rows]
            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] search_player_names_sync failed: {e}")
                return []

    def search_player_tags_by_prefix_sync(self, prefix: str, limit: int = 12) -> List[Dict[str, str]]:
        """Tag-PREFIX search over player_name_search (2026-08-17, Step 11) — backs the CWL
        guest search's `#` tag mode (web_bridge.py's _search_cwl_guests_sync), used when
        CONFIG.cwl_use_fts_player_search is True. A prefix LIKE pattern (no leading `%`) is
        index-backed on player_name_search's own PK B-tree — no FTS needed for a tag prefix,
        only name substrings need trigram matching."""
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        prefix = prefix.strip()
        if not prefix:
            return []
        with self._sync_conn() as conn:
            try:
                rows = conn.execute("""
                    SELECT player_tag, name FROM player_name_search
                    WHERE player_tag LIKE ?
                    LIMIT ?
                """, (prefix.replace('%', '').replace('_', '') + '%', min(limit, 12))).fetchall()
                return [{"player_tag": row["player_tag"], "player_name": row["name"]} for row in rows]
            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] search_player_tags_by_prefix_sync failed: {e}")
                return []

    def search_players_by_name_sync(
        self, name_substring: str, limit: int = 25
    ) -> List[Dict[str, str]]:
        """
        Search war_attacks for distinct players whose name contains name_substring
        (case-insensitive). Returns up to ``limit`` dicts with 'player_tag' and
        'player_name' (most recent name seen for each tag), ordered by most
        recently seen. Hard cap at 25 — Discord select-menu limit.

        NOTE: This is the slow fallback (full table scan) used only when
        CACHE.player_name_index is not yet populated.  Prefer
        CACHE.search_player_names() for instant in-memory results.
        """
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        pattern = f"%{name_substring}%"
        with self._sync_conn() as conn:
            try:
                wa_cols = self._explicit_column_list_sync(conn, "war_attacks")
                rows = conn.execute(f"""
                    WITH wa AS (
                        SELECT {wa_cols} FROM main.war_attacks
                        UNION ALL SELECT {wa_cols} FROM history.war_attacks
                    )
                    SELECT player_tag, player_name
                    FROM (
                        SELECT player_tag, player_name,
                               ROW_NUMBER() OVER (PARTITION BY player_tag ORDER BY date DESC) AS rn,
                               MAX(date) OVER (PARTITION BY player_tag) AS last_seen
                        FROM wa
                        WHERE player_name LIKE ? COLLATE NOCASE
                    )
                    WHERE rn = 1
                    ORDER BY last_seen DESC
                    LIMIT ?
                """, (pattern, min(limit, 25))).fetchall()
                return [
                    {"player_tag": row["player_tag"], "player_name": row["player_name"]}
                    for row in rows
                ]
            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] search_players_by_name_sync failed: {e}")
                return []

    def get_player_war_history_sync(
        self, player_tag: str, limit: int = 200
    ) -> List[Dict[str, Any]]:
        """
        Return per-war aggregated attack history for a single player across ALL clans.

        Each returned dict represents one war and contains:
            war_id, date, player_name, th_level, clan_tag,
            stars, attacks, missed_attacks, max_attacks,
            total_destruction, is_cwl, cwl_season, result,
            opponent_name, team_size, times_defended

        Rows are sorted newest-first. ``limit`` caps how many wars are returned
        (default 200 is large enough to represent years of history).

        Sentinel rows (attack_order = 0 = missed ALL attacks in that war) are
        merged into the per-war dict with stars=0, attacks=0.
        """
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        tag = player_tag.upper().lstrip("#")
        tag_with_hash = f"#{tag}"

        with self._sync_conn() as conn:
            try:
                # ── Actual attacks (attack_order > 0) ──
                # NOTE: ws is deliberately NOT a UNION ALL CTE joined via LEFT JOIN here.
                # SQLite cannot use a co-routine for the right side of a LEFT JOIN, so a
                # compound (UNION ALL) subquery on that side gets fully MATERIALIZED —
                # i.e. both main.war_summary and history.war_summary are scanned in full
                # (millions of rows) on every call, which caused multi-second queries and
                # OOM crashes on prod. Joining each physical table directly lets SQLite use
                # the UNIQUE(war_id, clan_tag) index for a cheap per-row lookup instead.
                wa_cols = self._explicit_column_list_sync(conn, "war_attacks")
                actual_rows = conn.execute(f"""
                    WITH wa AS (
                        SELECT {wa_cols} FROM main.war_attacks
                        UNION ALL SELECT {wa_cols} FROM history.war_attacks
                    )
                    SELECT
                        wa.war_id,
                        wa.date,
                        wa.player_name,
                        wa.th_level,
                        wa.clan_tag,
                        SUM(wa.stars)                                              AS stars,
                        COALESCE(MAX(wa.max_attacks), 0)
                            - COALESCE(MAX(wa.missed_attacks), 0)                  AS attacks,
                        MAX(wa.missed_attacks)                                     AS missed_attacks,
                        MAX(wa.max_attacks)                                        AS max_attacks,
                        SUM(wa.destruction)                                        AS total_destruction,
                        MAX(wa.times_defended)                                     AS times_defended,
                        COALESCE(ws_h.is_cwl, ws_a.is_cwl,
                            CASE WHEN MAX(wa.max_attacks) = 1 THEN 1 ELSE 0 END)  AS is_cwl,
                        COALESCE(NULLIF(ws_h.cwl_season, ''), NULLIF(ws_a.cwl_season, ''),
                            CASE WHEN MAX(wa.max_attacks) = 1
                                 THEN substr(MAX(wa.date), 1, 7)
                                 ELSE '' END)                                      AS cwl_season,
                        COALESCE(ws_h.result, ws_a.result, '')                     AS result,
                        COALESCE(ws_h.opponent_name, ws_a.opponent_name, '')       AS opponent_name,
                        COALESCE(ws_h.team_size, ws_a.team_size, 0)                AS team_size
                    FROM wa
                    LEFT JOIN main.war_summary ws_h
                        ON wa.war_id   = ws_h.war_id
                       AND wa.clan_tag = ws_h.clan_tag
                    LEFT JOIN history.war_summary ws_a
                        ON wa.war_id   = ws_a.war_id
                       AND wa.clan_tag = ws_a.clan_tag
                    WHERE wa.player_tag = ?
                      AND wa.attack_order > 0
                    GROUP BY wa.war_id, wa.clan_tag
                    ORDER BY wa.date DESC, wa.war_id DESC
                    LIMIT ?
                """, (tag_with_hash, limit)).fetchall()

                # ── Sentinel rows: missed ALL attacks ──
                # Only include wars not already covered by actual_rows.
                # (See the note above actual_rows — same MATERIALIZE-avoidance fix.)
                sentinel_rows = conn.execute(f"""
                    WITH wa AS (
                        SELECT {wa_cols} FROM main.war_attacks
                        UNION ALL SELECT {wa_cols} FROM history.war_attacks
                    )
                    SELECT
                        wa.war_id,
                        wa.date,
                        wa.player_name,
                        wa.th_level,
                        wa.clan_tag,
                        0                                                          AS stars,
                        0                                                          AS attacks,
                        wa.missed_attacks,
                        wa.max_attacks,
                        0.0                                                        AS total_destruction,
                        wa.times_defended,
                        COALESCE(ws_h.is_cwl, ws_a.is_cwl,
                            CASE WHEN wa.max_attacks = 1 THEN 1 ELSE 0 END)       AS is_cwl,
                        COALESCE(NULLIF(ws_h.cwl_season, ''), NULLIF(ws_a.cwl_season, ''),
                            CASE WHEN wa.max_attacks = 1
                                 THEN substr(wa.date, 1, 7)
                                 ELSE '' END)                                      AS cwl_season,
                        COALESCE(ws_h.result, ws_a.result, '')                     AS result,
                        COALESCE(ws_h.opponent_name, ws_a.opponent_name, '')       AS opponent_name,
                        COALESCE(ws_h.team_size, ws_a.team_size, 0)                AS team_size
                    FROM wa
                    LEFT JOIN main.war_summary ws_h
                        ON wa.war_id   = ws_h.war_id
                       AND wa.clan_tag = ws_h.clan_tag
                    LEFT JOIN history.war_summary ws_a
                        ON wa.war_id   = ws_a.war_id
                       AND wa.clan_tag = ws_a.clan_tag
                    WHERE wa.player_tag = ?
                      AND wa.attack_order = 0
                    ORDER BY wa.date DESC, wa.war_id DESC
                    LIMIT ?
                """, (tag_with_hash, limit)).fetchall()

                # Merge: index actual by (war_id, clan_tag) for fast dedup lookup
                seen: set[tuple[str, str]] = set()
                records: List[Dict[str, Any]] = []

                for row in actual_rows:
                    key = (row["war_id"], row["clan_tag"])
                    seen.add(key)
                    records.append({
                        "war_id":           row["war_id"],
                        "date":             row["date"],
                        "player_name":      row["player_name"],
                        "th_level":         int(row["th_level"]),
                        "clan_tag":         row["clan_tag"],
                        "stars":            int(row["stars"] or 0),
                        "attacks":          int(row["attacks"] or 0),
                        "missed_attacks":   int(row["missed_attacks"] or 0),
                        "max_attacks":      int(row["max_attacks"] or 2),
                        "total_destruction": float(row["total_destruction"] or 0.0),
                        "times_defended":   int(row["times_defended"] or 0),
                        "is_cwl":           bool(row["is_cwl"]),
                        "cwl_season":       row["cwl_season"],
                        "result":           row["result"],
                        "opponent_name":    row["opponent_name"],
                        "team_size":        int(row["team_size"] or 0),
                    })

                for row in sentinel_rows:
                    key = (row["war_id"], row["clan_tag"])
                    if key in seen:
                        continue  # already have real attacks for this war
                    records.append({
                        "war_id":           row["war_id"],
                        "date":             row["date"],
                        "player_name":      row["player_name"],
                        "th_level":         int(row["th_level"]),
                        "clan_tag":         row["clan_tag"],
                        "stars":            0,
                        "attacks":          0,
                        "missed_attacks":   int(row["missed_attacks"] or 0),
                        "max_attacks":      int(row["max_attacks"] or 2),
                        "total_destruction": 0.0,
                        "times_defended":   int(row["times_defended"] or 0),
                        "is_cwl":           bool(row["is_cwl"]),
                        "cwl_season":       row["cwl_season"],
                        "result":           row["result"],
                        "opponent_name":    row["opponent_name"],
                        "team_size":        int(row["team_size"] or 0),
                    })

                # Sort merged result newest-first
                records.sort(key=lambda r: (r["date"], r["war_id"]), reverse=True)
                return records[:limit]

            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] get_player_war_history_sync failed: {e}")
                return []

    def get_player_attack_summary_sync(self, player_tag: str, is_cwl: Optional[bool] = None) -> Dict[str, Any]:
        """
        Return aggregate statistics for a single player.

        Args:
            player_tag: CoC player tag (with or without '#').
            is_cwl:
                - None: include all wars (default)
                - True: only CWL wars
                - False: only regular CW wars

        ``total_attacks`` and ``avg_stars`` are computed at the per-war level so
        both modern data (one row per attack) and old aggregated data (one row
        for both attacks in a regular CW) are handled correctly::

            actual_attacks = MAX(max_attacks) - MAX(missed_attacks)  per war
            avg_stars      = SUM(all stars) / SUM(actual_attacks)

        The old aggregated rows have ``stars`` up to 6 (sum of 2 attacks) but
        ``max_attacks=2``, so the division naturally yields ≤ 3.0 — no capping
        needed.

        ``zero_star`` … ``three_star`` (distribution) is computed only from rows
        where ``stars <= 3`` (individual attack rows).  Old aggregated CW rows
        (``stars > 3``) are excluded from the distribution to avoid ambiguity,
        but they ARE counted correctly in ``total_attacks`` and ``avg_stars``
        via the per-war subquery.  ``dist_attacks`` reports how many individual
        rows the distribution is based on.

        Returns an empty dict on error or no data.
        """
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        tag = f"#{player_tag.upper().lstrip('#')}"

        cwl_filter: Optional[int]
        if is_cwl is None:
            cwl_filter = None
        else:
            cwl_filter = 1 if is_cwl else 0

        with self._sync_conn() as conn:
            try:
                # ── Per-war totals (correct for both modern and aggregated rows) ──
                # NOTE: joins directly against main.war_summary / history.war_summary
                # (not a UNION ALL 'ws' CTE) — SQLite forces full materialization of a
                # compound subquery used as the right side of a LEFT JOIN, which turned
                # this into a multi-million-row scan and caused OOM on prod.
                wa_cols = self._explicit_column_list_sync(conn, "war_attacks")
                total_row = conn.execute(f"""
                    WITH wa AS (
                        SELECT {wa_cols} FROM main.war_attacks
                        UNION ALL SELECT {wa_cols} FROM history.war_attacks
                    )
                    SELECT
                        SUM(actual_attacks)                                                    AS total_attacks,
                        CAST(SUM(war_stars) AS FLOAT) / SUM(actual_attacks)                    AS avg_stars,
                        CAST(SUM(CASE WHEN war_dest > 0 THEN war_dest ELSE 0 END) AS FLOAT)
                            / NULLIF(SUM(CASE WHEN war_dest > 0 THEN actual_attacks ELSE 0 END), 0)
                                                                                               AS avg_destruction
                    FROM (
                        SELECT
                            MAX(wa.max_attacks) - MAX(wa.missed_attacks)             AS actual_attacks,
                            SUM(wa.stars)                                             AS war_stars,
                            SUM(wa.destruction)                                       AS war_dest,
                            COALESCE(ws_h.is_cwl, ws_a.is_cwl,
                                CASE WHEN MAX(wa.max_attacks) = 1 THEN 1 ELSE 0 END) AS inferred_is_cwl
                        FROM wa
                        LEFT JOIN main.war_summary ws_h
                            ON wa.war_id   = ws_h.war_id
                           AND wa.clan_tag = ws_h.clan_tag
                        LEFT JOIN history.war_summary ws_a
                            ON wa.war_id   = ws_a.war_id
                           AND wa.clan_tag = ws_a.clan_tag
                        WHERE wa.player_tag = ? AND wa.attack_order > 0
                        GROUP BY wa.war_id, wa.clan_tag
                        HAVING MAX(wa.max_attacks) - MAX(wa.missed_attacks) > 0
                    )
                    WHERE (? IS NULL OR inferred_is_cwl = ?)
                """, (tag, cwl_filter, cwl_filter)).fetchone()

                if total_row is None or int(total_row["total_attacks"] or 0) == 0:
                    return {}

                # ── Star distribution: individual attack rows only (stars <= 3) ──
                dist_row = conn.execute(f"""
                    WITH wa AS (
                        SELECT {wa_cols} FROM main.war_attacks
                        UNION ALL SELECT {wa_cols} FROM history.war_attacks
                    )
                    SELECT
                        SUM(CASE WHEN stars = 0 THEN 1 ELSE 0 END) AS zero_star,
                        SUM(CASE WHEN stars = 1 THEN 1 ELSE 0 END) AS one_star,
                        SUM(CASE WHEN stars = 2 THEN 1 ELSE 0 END) AS two_star,
                        SUM(CASE WHEN stars = 3 THEN 1 ELSE 0 END) AS three_star,
                        COUNT(*)                                    AS dist_attacks
                    FROM wa
                    LEFT JOIN main.war_summary ws_h
                        ON wa.war_id   = ws_h.war_id
                       AND wa.clan_tag = ws_h.clan_tag
                    LEFT JOIN history.war_summary ws_a
                        ON wa.war_id   = ws_a.war_id
                       AND wa.clan_tag = ws_a.clan_tag
                    WHERE wa.player_tag = ?
                      AND wa.attack_order > 0
                      AND wa.stars <= 3
                      AND (
                            ? IS NULL OR
                            COALESCE(ws_h.is_cwl, ws_a.is_cwl,
                                CASE WHEN wa.max_attacks = 1 THEN 1 ELSE 0 END) = ?
                      )
                """, (tag, cwl_filter, cwl_filter)).fetchone()

                return {
                    "total_attacks":   int(total_row["total_attacks"]),
                    "avg_stars":       float(total_row["avg_stars"]       or 0.0),
                    "avg_destruction": float(total_row["avg_destruction"] or 0.0),
                    "zero_star":       int(dist_row["zero_star"]   or 0) if dist_row else 0,
                    "one_star":        int(dist_row["one_star"]    or 0) if dist_row else 0,
                    "two_star":        int(dist_row["two_star"]    or 0) if dist_row else 0,
                    "three_star":      int(dist_row["three_star"]  or 0) if dist_row else 0,
                    "dist_attacks":    int(dist_row["dist_attacks"] or 0) if dist_row else 0,
                }

            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] get_player_attack_summary_sync failed: {e}")
                return {}

    def get_player_monthly_star_dist_sync(self, player_tag: str) -> List[Dict[str, Any]]:
        """
        Return per-month, per-type (CW/CWL) star distribution for individual attacks.

        Used for the Skill chart in the /whois player report.
        Only includes rows where attack_order > 0 and stars <= 3 (individual attacks).
        Old aggregated CW rows (stars > 3) are excluded from the distribution.

        Returns:
            List of dicts sorted by month ascending, each with:
            month, is_cwl, season_key (cwl_season for CWL, month for CW),
            zero_star, one_star, two_star, three_star, dist_attacks.
        """
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized.")

        tag = f"#{player_tag.upper().lstrip('#')}"
        with self._sync_conn() as conn:
            try:
                wa_cols = self._explicit_column_list_sync(conn, "war_attacks")
                rows = conn.execute(f"""
                    WITH wa AS (
                        SELECT {wa_cols} FROM main.war_attacks
                        UNION ALL SELECT {wa_cols} FROM history.war_attacks
                    )
                    SELECT
                        substr(wa.date, 1, 7)                                       AS month,
                        COALESCE(ws_h.is_cwl, ws_a.is_cwl,
                            CASE WHEN wa.max_attacks = 1 THEN 1 ELSE 0 END)         AS is_cwl,
                        COALESCE(NULLIF(ws_h.cwl_season, ''), NULLIF(ws_a.cwl_season, ''),
                            substr(wa.date, 1, 7))                                  AS season_key,
                        SUM(CASE WHEN wa.stars = 0 THEN 1 ELSE 0 END)               AS zero_star,
                        SUM(CASE WHEN wa.stars = 1 THEN 1 ELSE 0 END)               AS one_star,
                        SUM(CASE WHEN wa.stars = 2 THEN 1 ELSE 0 END)               AS two_star,
                        SUM(CASE WHEN wa.stars = 3 THEN 1 ELSE 0 END)               AS three_star,
                        COUNT(*)                                                     AS dist_attacks
                    FROM wa
                    LEFT JOIN main.war_summary ws_h
                        ON wa.war_id   = ws_h.war_id
                       AND wa.clan_tag = ws_h.clan_tag
                    LEFT JOIN history.war_summary ws_a
                        ON wa.war_id   = ws_a.war_id
                       AND wa.clan_tag = ws_a.clan_tag
                    WHERE wa.player_tag = ?
                      AND wa.attack_order > 0
                      AND wa.stars <= 3
                      AND substr(wa.date, 1, 7) != ''
                    GROUP BY month,
                        COALESCE(ws_h.is_cwl, ws_a.is_cwl,
                            CASE WHEN wa.max_attacks = 1 THEN 1 ELSE 0 END),
                        season_key
                    ORDER BY month ASC
                """, (tag,)).fetchall()
                return [
                    {
                        "month":        row["month"],
                        "is_cwl":       bool(row["is_cwl"]),
                        "season_key":   row["season_key"],
                        "zero_star":    int(row["zero_star"]    or 0),
                        "one_star":     int(row["one_star"]     or 0),
                        "two_star":     int(row["two_star"]     or 0),
                        "three_star":   int(row["three_star"]   or 0),
                        "dist_attacks": int(row["dist_attacks"] or 0),
                    }
                    for row in rows
                ]
            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] get_player_monthly_star_dist_sync failed: {e}")
                return []

    def get_cwl_max_rounds_sync(
        self,
        season_clan_pairs: List[Tuple[str, str]],
    ) -> Dict[str, int]:
        """
        Return {cwl_season: max_rounds} for each (cwl_season, clan_tag) pair.

        Queries cwl_league_rounds via cwl_league_groups to count distinct rounds
        played in each group.  Seasons with no cwl_league_rounds data (group was
        never fetched from the API) are omitted — callers should fall back to 7.

        Args:
            season_clan_pairs: List of (cwl_season, clan_tag) tuples.
        """
        import sqlite3

        if not season_clan_pairs or not self.db_path:
            return {}
        try:
            with self._sync_conn() as conn:
                clg_cols = self._explicit_column_list_sync(conn, "cwl_league_groups")
                clr_cols = self._explicit_column_list_sync(conn, "cwl_league_rounds")
                result: Dict[str, int] = {}
                for cwl_season, clan_tag in season_clan_pairs:
                    row = conn.execute(
                        f"""
                        WITH clg AS (
                            SELECT {clg_cols} FROM main.cwl_league_groups
                            UNION ALL SELECT {clg_cols} FROM history.cwl_league_groups
                        ), clr AS (
                            SELECT {clr_cols} FROM main.cwl_league_rounds
                            UNION ALL SELECT {clr_cols} FROM history.cwl_league_rounds
                        )
                        SELECT COUNT(DISTINCT clr.cwl_round) AS max_rounds
                        FROM   clg
                        JOIN   clr
                               ON  clr.league_group_id = clg.league_group_id
                               AND clr.cwl_season      = clg.cwl_season
                        WHERE  clg.clan_tag   = ?
                          AND  clg.cwl_season = ?
                        """,
                        (clan_tag, cwl_season),
                    ).fetchone()
                    rounds = int(row["max_rounds"]) if row and row["max_rounds"] else 0
                    if rounds > 0:
                        result[cwl_season] = rounds
                return result
        except sqlite3.Error as e:
            logging.error(f"[DB-QUERY-SYNC] get_cwl_max_rounds_sync failed: {e}")
            return {}

    def get_player_cwl_attacks_multi_season_sync(
        self, player_tags: List[str],
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        For each player, return CWL attack records from their most recent CWL season.

        Player-centric: searches across **all** clans, so a player who switched
        clans after a CWL still gets their historical skill data resolved.

        For each player_tag the method finds the most recent ``cwl_season`` in
        which that player attacked (regardless of which clan they were in) and
        returns all attack rows from that season.

        Args:
            player_tags: Player tags to look up.

        Returns:
            ``{player_tag: [attack_record_dicts]}`` — only players that have at
            least one CWL attack.  Each dict has keys: player_tag, attacker_th,
            defender_th, stars, cwl_season.
        """
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        if not player_tags:
            return {}

        with self._sync_conn() as conn:
            try:
                # ── Step 1: Find each player's most recent CWL season ──
                placeholders = ",".join("?" for _ in player_tags)
                best_season_rows = conn.execute(f"""
                    SELECT wa.player_tag, MAX(ws.cwl_season) AS best_season
                    FROM war_attacks wa
                    JOIN war_summary ws
                        ON wa.war_id   = ws.war_id
                       AND wa.clan_tag = ws.clan_tag
                    WHERE ws.is_cwl        = 1
                      AND ws.cwl_season   != ''
                      AND wa.attack_order  > 0
                      AND wa.player_tag IN ({placeholders})
                    GROUP BY wa.player_tag
                """, (*player_tags,)).fetchall()

                if not best_season_rows:
                    return {}

                # Map: player_tag → best_season
                player_best: Dict[str, str] = {
                    row["player_tag"]: row["best_season"] for row in best_season_rows
                }

                # ── Step 2: Fetch attack rows for each player from their best season ──
                season_players: Dict[str, List[str]] = {}
                for ptag, season in player_best.items():
                    season_players.setdefault(season, []).append(ptag)

                result: Dict[str, List[Dict[str, Any]]] = {}

                for cwl_season, ptags in season_players.items():
                    ph = ",".join("?" for _ in ptags)
                    rows = conn.execute(f"""
                        SELECT wa.player_tag, wa.th_level AS attacker_th,
                               COALESCE(NULLIF(wa.defender_th, 0), wa.th_level) AS defender_th,
                               wa.stars
                        FROM war_attacks wa
                        JOIN war_summary ws
                            ON wa.war_id   = ws.war_id
                           AND wa.clan_tag = ws.clan_tag
                        WHERE ws.cwl_season = ?
                          AND ws.is_cwl     = 1
                          AND wa.attack_order > 0
                          AND wa.player_tag IN ({ph})
                    """, (cwl_season, *ptags)).fetchall()

                    for row in rows:
                        ptag = row["player_tag"]
                        result.setdefault(ptag, []).append({
                            "player_tag":  ptag,
                            "attacker_th": int(row["attacker_th"]),
                            "defender_th": int(row["defender_th"]),
                            "stars":       int(row["stars"]),
                            "cwl_season":  cwl_season,
                        })

                return result
            except sqlite3.Error as e:
                logging.error(f"[DB-QUERY-SYNC] get_player_cwl_attacks_multi_season_sync failed: {e}")
                return {}

    def check_integrity_sync(self) -> Tuple[bool, List[str]]:
        """
        Run database integrity check using PRAGMA commands (synchronous).
        
        Returns:
            Tuple of (is_ok, error_messages)
        
        Raises:
            RuntimeError: If database not initialized
        """
        import sqlite3
        
        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        
        errors: List[str] = []
        try:
            with self._sync_conn() as conn:
                cursor = conn.execute("PRAGMA integrity_check")
                result = cursor.fetchone()
                # PRAGMA integrity_check returns a single unnamed column; read it
                # positionally (PRAGMA result columns are fixed and unaffected by
                # ALTER TABLE, so positional access is safe here).
                check_val: str = str(result[0]) if result else "error"
                if check_val != 'ok':
                    errors.append(f"Integrity check failed: {check_val}")

                cursor = conn.execute("PRAGMA foreign_key_check")
                fk_violations = cursor.fetchall()
                if fk_violations:
                    errors.append(f"Foreign key violations found: {len(fk_violations)}")

            return (len(errors) == 0, errors)
            
        except sqlite3.Error as e:
            errors.append(f"Database query failed: {e}")
            return (False, errors)

    def get_all_war_attacks_existing_sync(self) -> "frozenset[tuple[str, str]]":
        """
        Return a frozenset of (clan_tag, war_id) pairs that have at least one row
        in war_attacks.  Used for batch existence check instead of one query per file.
        """
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                rows = conn.execute(
                    "SELECT clan_tag, war_id FROM main.war_attacks "
                    "UNION SELECT clan_tag, war_id FROM history.war_attacks"
                ).fetchall()
                return frozenset((row["clan_tag"], row["war_id"]) for row in rows)
            except sqlite3.Error as e:
                logging.error(f"[DB-CHECK-SYNC] get_all_war_attacks_existing_sync failed: {e}")
                return frozenset()

    def get_all_war_summary_keys_sync(self) -> "frozenset[tuple[str, str]]":
        """
        Return a frozenset of (clan_tag, war_id) pairs from war_summary.

        Used by the archive consistency check.  Querying war_summary only (not
        war_attacks) is intentional: war_summary covers every war the bot has
        processed, including PrivateWarLog wars that have no attack rows.  The
        war_attacks table is much larger and loading its full key set into Python
        makes /admin Check Data take 50+ seconds on the server-machine.
        """
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        with self._sync_conn() as conn:
            try:
                rows = conn.execute(
                    "SELECT clan_tag, war_id FROM main.war_summary "
                    "UNION SELECT clan_tag, war_id FROM history.war_summary"
                ).fetchall()
                return frozenset((row["clan_tag"], row["war_id"]) for row in rows)
            except sqlite3.Error as e:
                logging.error(f"[DB-CHECK-SYNC] get_all_war_summary_keys_sync failed: {e}")
                return frozenset()

    def has_cwl_season_data_sync(self, clan_tag: str, season: str) -> bool:
        """
        Return True if war_summary already contains at least one CWL row for this clan + season.

        Used for idempotency checks in backfill_last_cwl_for_clan().

        Args:
            clan_tag: Normalized clan tag (e.g. '#2C9UR9GJY')
            season:   CWL season string as stored in war_summary.cwl_season (e.g. '2025-11')
        """
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        try:
            with self._sync_conn() as conn:
                row = conn.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM main.war_summary WHERE clan_tag = ? AND is_cwl = 1 AND cwl_season = ?)
                      + (SELECT COUNT(*) FROM history.war_summary WHERE clan_tag = ? AND is_cwl = 1 AND cwl_season = ?)
                        AS cnt
                    """,
                    (clan_tag, season, clan_tag, season),
                ).fetchone()
                return bool(row["cnt"]) if row else False
        except sqlite3.Error as e:
            logging.error(f"[DB-QUERY-SYNC] has_cwl_season_data_sync failed for {clan_tag}/{season}: {e}")
            return False

    def get_clans_with_cwl_data_for_season_sync(self, clan_tags: List[str], season: str) -> Set[str]:
        """Batched version of has_cwl_season_data_sync: return the subset of clan_tags that have
        at least one real war_summary row with is_cwl=1 for `season` — "which of these clans
        actually played CWL that season." Used to pre-set a new season's participating clans
        from real war history across the guild's full CWL family (CwlCarryOverPromptView,
        ui_cwl_roster.py — replaces the previous "was participating=1 last time I toggled it"
        carry-over, so a clan added to the family since, or one the admin forgot to flip on/off,
        still gets an accurate default)."""
        import sqlite3

        if not self.db_path or not clan_tags:
            return set()

        try:
            with self._sync_conn() as conn:
                placeholders = ",".join("?" * len(clan_tags))
                rows = conn.execute(
                    f"""
                    SELECT DISTINCT clan_tag FROM (
                        SELECT clan_tag FROM main.war_summary
                            WHERE is_cwl = 1 AND cwl_season = ? AND clan_tag IN ({placeholders})
                        UNION ALL
                        SELECT clan_tag FROM history.war_summary
                            WHERE is_cwl = 1 AND cwl_season = ? AND clan_tag IN ({placeholders})
                    )
                    """,
                    (season, *clan_tags, season, *clan_tags),
                ).fetchall()
                return {row["clan_tag"] for row in rows}
        except sqlite3.Error as e:
            logging.error(f"[DB-QUERY-SYNC] get_clans_with_cwl_data_for_season_sync failed for season {season}: {e}")
            return set()

    def get_all_war_summaries_brief_sync(self) -> List[Tuple[str, str, str, int]]:
        """
        Get all (war_id, clan_tag, date, is_cwl) from war_summary (synchronous).

        Used by integrity checks for duplicate detection.
        """
        import sqlite3

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        try:
            with self._sync_conn() as conn:
                rows = conn.execute(
                    "SELECT war_id, clan_tag, date, is_cwl FROM main.war_summary "
                    "UNION "
                    "SELECT war_id, clan_tag, date, is_cwl FROM history.war_summary "
                    "ORDER BY clan_tag, war_id"
                ).fetchall()
                return [(row["war_id"], row["clan_tag"], row["date"], int(row["is_cwl"] or 0)) for row in rows]
        except sqlite3.Error as e:
            logging.error(f"[DB-QUERY-SYNC] Failed to get all war summaries: {e}")
            return []

    def get_recent_war_summaries_sync(self, days: int = 28) -> List[Tuple[str, str, str]]:
        """
        Get recent (clan_tag, war_id, date) from war_summary (synchronous).
        """
        import sqlite3
        from datetime import datetime, timedelta

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        try:
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")
            with self._sync_conn() as conn:
                rows = conn.execute(
                    "SELECT DISTINCT clan_tag, war_id, date FROM war_summary WHERE date >= ? ORDER BY date DESC",
                    (cutoff,),
                ).fetchall()
                return [(row["clan_tag"], row["war_id"], row["date"]) for row in rows]
        except sqlite3.Error as e:
            logging.error(f"[DB-QUERY-SYNC] Failed to get recent war summaries: {e}")
            return []

    def get_global_db_statistics_sync(self, force_refresh: bool = False) -> Dict[str, int]:
        """
        Get comprehensive global DB statistics across all tables (synchronous).

        Args:
            force_refresh: Bypass the cache and recompute now, refreshing it.
                Used by the end-of-nightly-maintenance refresh (see
                QapBot.py's run_nightly_maintenance_routine) so /status always
                serves an at-most-25h-stale value during normal operation
                instead of ever paying the scan cost inline.

        Returns:
            Dict with keys:
              - clans_count: rows in the clans table
              - wars_count: distinct war_ids in war_summary
              - attacks_count: actual attacks in war_attacks (attack_order > 0)
              - players_count: linked CoC accounts in user_players (registered players)
              - players_tracked_count: all unique players ever seen in wars (player_name_index)

        ``wars_count`` and ``attacks_count`` require a full index scan across the
        (multi-million-row, hot+history) war_summary / war_attacks tables — there
        is no O(1) row-count shortcut for a UNION ALL of two attached schemas.
        On server-machine/slow storage this alone took 10+ seconds, making /status
        unnecessarily slow every time it was called. /status is only used a few
        times a week in practice, so a short TTL bought nothing — every call
        still paid the cold-scan cost. Instead the result is cached for
        ``_GLOBAL_STATS_TTL`` (25h) and the cache is proactively warmed at bot
        startup and refreshed at the end of nightly maintenance (after
        REINDEX/VACUUM/ANALYZE, so it reads post-maintenance state), so this
        slow path should never actually run during normal /status calls.
        """
        import sqlite3
        import time

        if not self.db_path:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        now = time.monotonic()
        if (
            not force_refresh
            and self._global_stats_cache is not None
            and (now - self._global_stats_cache_ts) < self._GLOBAL_STATS_TTL
        ):
            return dict(self._global_stats_cache)

        try:
            with self._sync_conn() as conn:
                clans_count = conn.execute("SELECT COUNT(*) AS cnt FROM clans").fetchone()["cnt"]
                wars_count = conn.execute(
                    "SELECT COUNT(DISTINCT war_id) AS cnt FROM ("
                    "SELECT war_id FROM main.war_summary UNION ALL SELECT war_id FROM history.war_summary"
                    ")"
                ).fetchone()["cnt"]
                # Subtract the small partial-index set (attack_order=0 rows)
                # from the total row count instead of doing a filtered scan.
                # Summed across main + history (each computed independently so
                # each half can still use its own idx_wa_zero_attacks partial index).
                attacks_count = conn.execute("""
                    SELECT (
                        (SELECT COUNT(*) FROM main.war_attacks) - (SELECT COUNT(*) FROM main.war_attacks WHERE attack_order = 0)
                    ) + (
                        (SELECT COUNT(*) FROM history.war_attacks) - (SELECT COUNT(*) FROM history.war_attacks WHERE attack_order = 0)
                    ) AS cnt
                """).fetchone()["cnt"]
                # Count linked CoC accounts (small table — avoids a 22 M-row
                # UNION deduplication scan across war_attacks that made /status
                # take many seconds on HDD).
                players_count = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM user_players"
                ).fetchone()["cnt"]
                # Count all unique players ever seen in wars (player_name_index).
                players_tracked_count = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM player_name_index"
                ).fetchone()["cnt"]

                stats = {
                    'clans_count': clans_count,
                    'wars_count': wars_count,
                    'attacks_count': attacks_count,
                    'players_count': players_count,
                    'players_tracked_count': players_tracked_count,
                }
                self._global_stats_cache = stats
                self._global_stats_cache_ts = time.monotonic()
                return dict(stats)
        except sqlite3.Error as e:
            logging.error(f"[DB-STATS-SYNC] Failed to get global db statistics: {e}")
            return {'clans_count': 0, 'wars_count': 0, 'attacks_count': 0, 'players_count': 0, 'players_tracked_count': 0}

    # ==================== Maindata DB Access Methods ====================
    
    async def get_clan(self, clan_tag: str) -> Optional[Dict[str, Any]]:
        """
        Get clan data from database.
        
        Args:
            clan_tag: Clan tag (e.g., "#2C9UR9GJY")
        
        Returns:
            Clan dict or None if not found
        """
        await self._ensure_connection()
        
        cursor = await self._conn.execute(
            "SELECT * FROM clans WHERE clan_tag = ?",
            (clan_tag,)
        )
        row = await cursor.fetchone()
        
        if not row:
            return None
        
        return {
            "clan_tag": row["clan_tag"],
            "name": row["name"],
            "has_active_subscriptions": bool(row["has_active_subscriptions"]),
            "last_war_update": row["last_war_update"],
            "warlog_is_public": bool(row["warlog_is_public"]),
            "last_checked_via_api": row["last_checked_via_api"]
        }
    
    async def _save_clan_unlocked(self, clan_tag: str, name: str, has_active_subscriptions: bool = False,
                                   last_war_update: Optional[str] = None, warlog_is_public: bool = True,
                                   last_checked_via_api: Optional[str] = None,
                                   war_league: Optional[str] = None,
                                   track_war_updates: Optional[bool] = None,
                                   is_deleted: Optional[bool] = None) -> None:
        """Internal: save clan WITHOUT acquiring _write_lock (called from within an already-locked context).

        war_league:
            Written only when non-None; existing DB value is preserved when caller passes None.
        track_war_updates:
            - None  → defaults to 1 (True) on INSERT; existing DB value kept on UPDATE
            - True  → stored as 1
            - False → stored as 0
            Callers are responsible for computing the correct value based on league
            and subscription status (three-tier model: actively tracked / passively tracked M2+ / passively tracked M3-).
        is_deleted:
            - None  → existing DB value preserved on UPDATE; defaults to 0 on INSERT
            - True  → stored as 1 (clan no longer exists in CoC)
            - False → stored as 0 (clan confirmed live)
        """
        await self._ensure_connection()
        # Convert track_war_updates: None → None (SQL COALESCE(?,1)=1); bool → 0/1
        _track_val = None if track_war_updates is None else (1 if track_war_updates else 0)
        # Convert is_deleted: None → None (SQL COALESCE preserves existing); bool → 0/1
        _is_deleted_val = None if is_deleted is None else (1 if is_deleted else 0)
        await self._conn.execute("""
            INSERT INTO clans 
            (clan_tag, name, has_active_subscriptions, last_war_update, warlog_is_public,
             last_checked_via_api, war_league, track_war_updates, is_deleted)
            VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, 1), COALESCE(?, 0))
            ON CONFLICT(clan_tag) DO UPDATE SET
                name = excluded.name,
                has_active_subscriptions = excluded.has_active_subscriptions,
                last_war_update = excluded.last_war_update,
                warlog_is_public = excluded.warlog_is_public,
                last_checked_via_api = excluded.last_checked_via_api,
                war_league = COALESCE(excluded.war_league, war_league),
                track_war_updates = COALESCE(excluded.track_war_updates, track_war_updates, 1),
                is_deleted = COALESCE(excluded.is_deleted, is_deleted, 0)
        """, (clan_tag, name, 1 if has_active_subscriptions else 0, last_war_update,
              1 if warlog_is_public else 0, last_checked_via_api, war_league, _track_val, _is_deleted_val))
        await self._conn.commit()

    async def save_clan(self, clan_tag: str, name: str, has_active_subscriptions: bool = False,
                       last_war_update: Optional[str] = None, warlog_is_public: bool = True,
                       last_checked_via_api: Optional[str] = None,
                       war_league: Optional[str] = None,
                       track_war_updates: Optional[bool] = None,
                       is_deleted: Optional[bool] = None) -> None:
        """Save or update clan data in database."""
        await self._write_lock.acquire()
        try:
            await self._save_clan_unlocked(clan_tag, name, has_active_subscriptions,
                                           last_war_update, warlog_is_public, last_checked_via_api,
                                           war_league=war_league, track_war_updates=track_war_updates,
                                           is_deleted=is_deleted)
        finally:
            self._write_lock.release()
    
    async def bulk_update_clan_subscription_statuses(self, updates: List[Tuple[bool, str]]) -> None:
        """Bulk-update has_active_subscriptions for multiple clans in one transaction.

        Args:
            updates: List of (new_status, clan_tag) tuples.
        """
        if not updates:
            return
        await self._ensure_connection()
        rows = [(1 if status else 0, tag) for status, tag in updates]
        await self._write_lock.acquire()
        try:
            await self._conn.executemany(
                "UPDATE clans SET has_active_subscriptions = ? WHERE clan_tag = ?",
                rows,
            )
            await self._conn.commit()
        finally:
            self._write_lock.release()

    async def bulk_update_clan_track_war_updates(self, clan_tags: List[str]) -> None:
        """Bulk-set track_war_updates = 1 for a list of clans in one transaction.

        Used by update_all_clan_subscription_statuses() to apply the one-way ratchet
        for all newly-qualifying clans in a single DB round-trip instead of N UPSERT
        calls.

        Args:
            clan_tags: Tags of clans to promote to track_war_updates = 1.
        """
        if not clan_tags:
            return
        await self._ensure_connection()
        rows = [(tag,) for tag in clan_tags]
        await self._write_lock.acquire()
        try:
            await self._conn.executemany(
                "UPDATE clans SET track_war_updates = 1 WHERE clan_tag = ?",
                rows,
            )
            await self._conn.commit()
        finally:
            self._write_lock.release()

    async def bulk_update_clan_timestamps(self, updates: List[Tuple[str, str]]) -> None:
        """Bulk-update last_war_update for multiple clans in one transaction.

        Args:
            updates: List of (timestamp_iso, clan_tag) tuples.
        """
        import time as _time
        if not updates:
            return
        await self._ensure_connection()
        await self._write_lock.acquire()
        try:
            # Suppress WAL autocheckpoint for this commit to prevent the aiosqlite
            # background thread from running a blocking checkpoint after Phase-3 writes
            # have left the WAL with >1000 frames.  A passive checkpoint is already
            # running (or will run) as a background task; we don't need another one here.
            await self._conn.execute("PRAGMA wal_autocheckpoint=0")
            try:
                _t0 = _time.monotonic()
                await self._conn.executemany(
                    "UPDATE clans SET last_war_update = ? WHERE clan_tag = ?",
                    updates,
                )
                await self._conn.commit()
                logging.info(
                    f"[DB-TS-FLUSH] Batch-persisted {len(updates)} clan timestamps in {_time.monotonic() - _t0:.2f}s"
                )
            finally:
                # ALWAYS restore autocheckpoint — if commit() raises (disk full, locked, etc.)
                # and this is not in a finally, wal_autocheckpoint stays 0 for the entire
                # session, causing unbounded WAL growth and eventual disk exhaustion.
                await self._conn.execute("PRAGMA wal_autocheckpoint=1000")
        finally:
            self._write_lock.release()

    async def delete_clan(self, clan_tag: str) -> None:
        """
        Delete clan from database.

        Foreign key CASCADE automatically deletes related records in tables
        that actually declare a FK to clans(clan_tag):
        - clan_family_members (ON DELETE CASCADE)
        - guild_member_clans (ON DELETE CASCADE)
        - user_players.current_clan_tag (ON DELETE SET NULL — row kept, FK nulled)

        NOTE: subscriptions.clan_tag and leaderboard_messages.clan_tag have NO
        FK constraint (they intentionally also store family tags / player tags),
        so rows there are NOT touched by this delete and can be left dangling.
        Prefer `delete_clan_if_unreferenced()` for self-healing cleanup, which
        checks those tables explicitly before deleting.

        Args:
            clan_tag: Clan tag to delete
            
        Raises:
            RuntimeError: If database not initialized
            aiosqlite.Error: If database operation fails
        """
        await self._ensure_connection()
        
        await self._write_lock.acquire()
        try:
            await self._conn.execute("DELETE FROM clans WHERE clan_tag = ?", (clan_tag,))
            await self._conn.commit()
            logging.info(f"[DB-WRITE] Deleted clan {clan_tag} from database")
        except Exception as e:
            logging.error(f"[DB-WRITE] Failed to delete clan {clan_tag}: {e}")
            raise
        finally:
            self._write_lock.release()

    async def is_clan_tag_referenced(self, clan_tag: str) -> bool:
        """
        Check whether *clan_tag* is still referenced anywhere else in the database.

        Iterates the module-level CLAN_TAG_REFERENCING_TABLES registry — the
        single source of truth covering FK-enforced tables, non-FK tag-storage
        tables that conceptually depend on a clan being real (with per-table
        discriminator filters for overloaded columns like
        leaderboard_messages.clan_tag), and historical war/CWL data in BOTH
        the hot ('main') and 'history' attached schemas. New clans-referencing
        tables are added to the registry, not here (enforced by
        tests/unit/test_clan_tag_reference_registry.py).

        Used by `delete_clan_if_unreferenced()` (self-healing cleanup) to make
        sure a clan row is only hard-deleted when nothing else in the DB
        depends on it.

        Args:
            clan_tag: Clan tag to check

        Returns:
            True if any table still references clan_tag, False if it is a
            true orphan.
        """
        await self._ensure_connection()

        # Registry entries are constants only — no injection risk.
        for schema, table, column, extra_where in CLAN_TAG_REFERENCING_TABLES:
            cursor = await self._conn.execute(
                f"SELECT 1 FROM {schema}.{table} WHERE {column} = ? {extra_where} LIMIT 1",
                (clan_tag,)
            )
            if await cursor.fetchone():
                return True

        return False

    async def delete_clan_if_unreferenced(self, clan_tag: str) -> bool:
        """
        Hard-delete a clan row from `clans` if, and only if, nothing else in
        the database still refers to it (see `is_clan_tag_referenced()`).

        Safe to call speculatively — e.g. every time the CoC API confirms a
        clan_tag no longer exists (404 NotFound) — since it is a no-op both
        when the clan is still referenced and when the row is already gone.
        This is the self-healing counterpart to the bug where an unrelated
        tag (e.g. a player tag) got inserted into `clans` as a placeholder:
        once the API confirms it's not a real clan, and nothing references
        it, the bogus row is removed automatically.

        Args:
            clan_tag: Clan tag to conditionally purge

        Returns:
            True if the row was deleted, False if it was kept (still
            referenced) or was already absent.
        """
        await self._ensure_connection()

        cursor = await self._conn.execute(
            "SELECT 1 FROM clans WHERE clan_tag = ? LIMIT 1", (clan_tag,)
        )
        if not await cursor.fetchone():
            return False  # already gone — nothing to do

        if await self.is_clan_tag_referenced(clan_tag):
            logging.debug(
                f"[DB-CLEANUP] Clan {clan_tag} is still referenced elsewhere; not purging"
            )
            return False

        await self.delete_clan(clan_tag)
        logging.info(
            f"[DB-CLEANUP] Purged orphaned clan {clan_tag} from database "
            f"(confirmed gone via API, no remaining references)"
        )
        return True
    
    async def get_existing_cwl_war_pairs(self, war_tags: List[str]) -> Set[Tuple[str, str]]:
        """
        Return (clan_tag, war_tag) pairs already in war_summary for the given war_tags.

        Used by the CWL recovery pipeline to skip DB entries that are already present,
        avoiding redundant file writes and DB inserts.
        """
        if not war_tags:
            return set()
        await self._ensure_connection()
        placeholders = ",".join("?" * len(war_tags))
        cursor = await self._conn.execute(
            f"SELECT clan_tag, war_tag FROM war_summary WHERE war_tag IN ({placeholders}) AND is_cwl = 1",
            war_tags,
        )
        rows = await cursor.fetchall()
        return {(row["clan_tag"], row["war_tag"]) for row in rows}

    async def get_war_tag_leagues(self, war_tags: List[str]) -> Dict[str, Optional[str]]:
        """
        Return {war_tag: league_rank} for each war_tag that has a known league in
        cwl_league_rounds + cwl_league_groups.  Tags with no DB match map to None.

        Used by the CWL recovery pipeline to determine whether an untracked clan
        should be added to the active-polling pool (league >= Master III).
        """
        if not war_tags:
            return {}
        await self._ensure_connection()
        placeholders = ",".join("?" * len(war_tags))
        clr_cols = await self._explicit_column_list("cwl_league_rounds")
        clg_cols = await self._explicit_column_list("cwl_league_groups")
        cursor = await self._conn.execute(
            f"""
            WITH clr AS (
                SELECT {clr_cols} FROM main.cwl_league_rounds
                UNION ALL SELECT {clr_cols} FROM history.cwl_league_rounds
            ), clg AS (
                SELECT {clg_cols} FROM main.cwl_league_groups
                UNION ALL SELECT {clg_cols} FROM history.cwl_league_groups
            )
            SELECT clr.war_tag, clg.league_rank
              FROM clr
              JOIN clg ON clg.league_group_id = clr.league_group_id
             WHERE clr.war_tag IN ({placeholders})
             GROUP BY clr.war_tag
            """,
            war_tags,
        )
        rows = await cursor.fetchall()
        result: Dict[str, Optional[str]] = {wt: None for wt in war_tags}
        for row in rows:
            result[row["war_tag"]] = row["league_rank"]
        return result

    # ── CWL league group + round tables ───────────────────────────────

    async def cwl_group_exists(self, league_group_id: str, cwl_season: str) -> bool:
        """Return True if (league_group_id, cwl_season) already has at least one
        row in cwl_league_groups — i.e. this is NOT the first time this exact
        group has been seen. Checked BEFORE upsert_cwl_league_data so it reflects
        state as of before this call's insert.

        Used to gate cwl_league_groups.league_rank population to genuine first
        discovery — the only moment every member of a group is *guaranteed* to
        share one league (see cache_manager._process_league_group_response).
        Only checks the hot ("main") schema: a CWL season currently being
        discovered for the first time can never already be old enough to have
        been swept into the history schema by monthly_history_migration.
        """
        await self._ensure_connection()
        cursor = await self._conn.execute(
            "SELECT 1 FROM cwl_league_groups WHERE league_group_id = ? AND cwl_season = ? LIMIT 1",
            (league_group_id, cwl_season),
        )
        return (await cursor.fetchone()) is not None

    async def clan_has_cwl_data_for_season(self, clan_tag: str, cwl_season: str) -> bool:
        """Return True if `clan_tag` already has at least one archived war_summary
        row (is_cwl=1) for `cwl_season` — i.e. the bot already captured some of
        this clan's rounds for the season currently in progress.

        Used by cache_manager._sync_group_track_war_updates() to avoid demoting
        (stopping polling for) a clan mid-season when doing so would abandon a
        partially-captured CWL season — see CLAN_WAR_TRACKING.md write-path 7.
        Checks both hot and history schemas: history.war_summary is always
        attached on this connection (see initialize()), and while a season still
        in progress is virtually always in the hot window, checking both is
        cheap and avoids a subtle false-negative right at a monthly boundary.
        """
        await self._ensure_connection()
        cursor = await self._conn.execute(
            """
            SELECT 1 FROM (
                SELECT clan_tag FROM main.war_summary
                WHERE  clan_tag = ? AND cwl_season = ? AND is_cwl = 1
                UNION ALL
                SELECT clan_tag FROM history.war_summary
                WHERE  clan_tag = ? AND cwl_season = ? AND is_cwl = 1
            )
            LIMIT 1
            """,
            (clan_tag, cwl_season, clan_tag, cwl_season),
        )
        return (await cursor.fetchone()) is not None

    async def upsert_cwl_league_data(
        self,
        league_group_id: str,
        cwl_season: str,
        clan_tags: List[str],
        rounds: List[Tuple[int, str]],  # (cwl_round, war_tag)
    ) -> int:
        """
        Upsert cwl_league_groups (8 clan membership rows) and cwl_league_rounds
        (one row per revealed war_tag) in a single transaction.

        Idempotent: uses INSERT OR IGNORE throughout — safe to call on every
        league-group poll.

        Returns:
            Number of rows actually inserted (0 = all rows already existed).

        Args:
            league_group_id: 16-char hex ID derived from make_league_group_id().
            cwl_season: '2026-05' style season string.
            clan_tags: All 8 clan tags in this group.
            rounds: List of (round_number, war_tag) pairs for revealed rounds only
                    (war_tag != '#0').
        """
        await self._ensure_connection()
        await self._write_lock.acquire()
        try:
            # Snapshot total_changes() before the inserts so we can return
            # exactly how many rows were actually written (ignored rows don't count).
            _cur_before = await self._conn.execute("SELECT total_changes() AS tc")
            _row_before = await _cur_before.fetchone()
            _before: int = _row_before["tc"] if _row_before else 0

            await self._conn.executemany(
                "INSERT OR IGNORE INTO cwl_league_groups "
                "(league_group_id, cwl_season, clan_tag) VALUES (?, ?, ?)",
                [(league_group_id, cwl_season, ct) for ct in clan_tags],
            )
            if rounds:
                await self._conn.executemany(
                    "INSERT OR IGNORE INTO cwl_league_rounds "
                    "(war_tag, cwl_season, cwl_round, league_group_id) VALUES (?, ?, ?, ?)",
                    [(wt, cwl_season, rnum, league_group_id) for rnum, wt in rounds],
                )
            await self._conn.commit()

            _cur_after = await self._conn.execute("SELECT total_changes() AS tc")
            _row_after = await _cur_after.fetchone()
            _after: int = _row_after["tc"] if _row_after else 0
            return _after - _before
        finally:
            self._write_lock.release()

    async def get_cwl_group_info(
        self, clan_tag: str, cwl_season: str
    ) -> Optional[Dict[str, Any]]:
        """
        Return group-level info for the given clan + season.

        Returns a dict with keys:
            league_group_id, cwl_ended, league_rank,
            clan_tags (sorted list of all 8 tags in the group),
            rows (list of per-clan dicts with all cwl_league_groups columns)
        or None if no entry exists.
        """
        await self._ensure_connection()
        clg_cols = await self._explicit_column_list("cwl_league_groups")
        # Find this clan's group_id for the season
        cursor = await self._conn.execute(
            f"WITH clg AS ("
            f"SELECT {clg_cols} FROM main.cwl_league_groups UNION ALL SELECT {clg_cols} FROM history.cwl_league_groups"
            ") "
            "SELECT league_group_id, cwl_ended, league_rank "
            "FROM clg WHERE cwl_season = ? AND clan_tag = ?",
            (cwl_season, clan_tag),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        group_id = row["league_group_id"]
        cwl_ended = bool(row["cwl_ended"])
        league_rank = row["league_rank"]

        # Fetch all rows for this group
        cursor2 = await self._conn.execute(
            f"WITH clg AS ("
            f"SELECT {clg_cols} FROM main.cwl_league_groups UNION ALL SELECT {clg_cols} FROM history.cwl_league_groups"
            ") "
            "SELECT clan_tag, league_rank, cwl_ended, group_rank, "
            "total_stars, total_destruction "
            "FROM clg "
            "WHERE league_group_id = ? AND cwl_season = ?",
            (group_id, cwl_season),
        )
        rows = [dict(r) for r in await cursor2.fetchall()]
        return {
            "league_group_id": group_id,
            "cwl_ended": cwl_ended,
            "league_rank": league_rank,
            "clan_tags": sorted(r["clan_tag"] for r in rows),
            "rows": rows,
        }

    async def get_latest_cwl_season_for_clan(self, clan_tag: str) -> Optional[str]:
        """Return the most recent cwl_season that has a cwl_league_groups entry for clan_tag, or None."""
        await self._ensure_connection()
        clg_cols = await self._explicit_column_list("cwl_league_groups")
        cursor = await self._conn.execute(
            f"WITH clg AS ("
            f"SELECT {clg_cols} FROM main.cwl_league_groups UNION ALL SELECT {clg_cols} FROM history.cwl_league_groups"
            ") "
            "SELECT cwl_season FROM clg WHERE clan_tag = ? "
            "ORDER BY cwl_season DESC LIMIT 1",
            (clan_tag,),
        )
        row = await cursor.fetchone()
        return row["cwl_season"] if row else None

    async def get_latest_cwl_season_for_clan_in_month(self, clan_tag: str, month_prefix: str) -> Optional[str]:
        """Return the cwl_season for clan_tag matching month_prefix, preferring the
        plain regular-CWL key over any mid-month bonus sub-season sharing the month.

        Example: month_prefix='2026-06' matches '2026-06', '2026-06-01', '2026-06-16', etc.
        Used to resolve the correct sub-season when multiple CWL events share the same month
        (e.g. a regular CWL and a mid-month bonus CWL).

        Preference order:
        1. The plain "YYYY-MM" regular-CWL key, if present for this month — an
           explicit month/year request should resolve to the regular monthly
           CWL, not a same-month bonus event.
        2. Otherwise, the most recent dated ("YYYY-MM-DD") sub-season for this
           month (e.g. only a mid-month bonus CWL ran that month).

        NOTE: a plain ``ORDER BY cwl_season DESC`` would always pick the dated
        sub-season over the plain key (since "2026-06-16" > "2026-06" as a
        string), which silently returns the wrong season whenever both a
        regular and a bonus CWL exist in the same month. Ordering by key
        length first avoids that.
        """
        await self._ensure_connection()
        clg_cols = await self._explicit_column_list("cwl_league_groups")
        cursor = await self._conn.execute(
            f"WITH clg AS ("
            f"SELECT {clg_cols} FROM main.cwl_league_groups UNION ALL SELECT {clg_cols} FROM history.cwl_league_groups"
            ") "
            "SELECT cwl_season FROM clg WHERE clan_tag = ? "
            "AND cwl_season LIKE ? ORDER BY LENGTH(cwl_season) ASC, cwl_season DESC LIMIT 1",
            (clan_tag, f"{month_prefix}%"),
        )
        row = await cursor.fetchone()
        return row["cwl_season"] if row else None

    async def get_cwl_group_war_stats(
        self, cwl_season: str, clan_tags: List[str]
    ) -> Tuple[Dict[str, Tuple[int, float]], Dict[str, int]]:
        """
        Return cumulative war_summary stats for all clans in a CWL group.

        Returns two dicts keyed by clan_tag:
            stars_map  — {clan_tag: (total_stars, total_destruction)}
            ended_map  — {clan_tag: ended_war_count}
        Only 'war_ended' rows matching the given cwl_season and is_cwl=1 are counted.
        """
        await self._ensure_connection()
        placeholders = ",".join("?" * len(clan_tags))
        params: Tuple[Any, ...] = (cwl_season, *clan_tags)
        ws_cols = await self._explicit_column_list("war_summary")

        cursor = await self._conn.execute(
            f"WITH ws AS ("
            f"SELECT {ws_cols} FROM main.war_summary UNION ALL SELECT {ws_cols} FROM history.war_summary"
            f") "
            f"SELECT clan_tag, "
            f"  SUM(clan_stars) + SUM(CASE WHEN result = 'win' THEN 10 ELSE 0 END) AS tot_stars, "
            f"  SUM(clan_destruction * team_size) AS tot_destr "
            f"FROM ws "
            f"WHERE cwl_season = ? AND is_cwl = 1 AND state = 'war_ended' AND clan_tag IN ({placeholders}) "
            f"GROUP BY clan_tag",
            params,
        )
        stars_map: Dict[str, Tuple[int, float]] = {
            row["clan_tag"]: (int(row["tot_stars"] or 0), float(row["tot_destr"] or 0.0))
            for row in await cursor.fetchall()
        }

        cursor2 = await self._conn.execute(
            f"WITH ws AS ("
            f"SELECT {ws_cols} FROM main.war_summary UNION ALL SELECT {ws_cols} FROM history.war_summary"
            f") "
            f"SELECT clan_tag, COUNT(*) AS ended_wars "
            f"FROM ws "
            f"WHERE cwl_season = ? AND is_cwl = 1 AND state = 'war_ended' AND clan_tag IN ({placeholders}) "
            f"GROUP BY clan_tag",
            params,
        )
        ended_map: Dict[str, int] = {
            row["clan_tag"]: int(row["ended_wars"] or 0)
            for row in await cursor2.fetchall()
        }
        return stars_map, ended_map

    async def update_cwl_group_stats_batch(
        self,
        cwl_season: str,
        group_id: str,
        clan_stats: List[Dict[str, Any]],
        set_cwl_ended: bool,
    ) -> int:
        """
        Write computed group stats for all clans in a group to cwl_league_groups.

        Each entry in clan_stats must have:
            clan_tag, group_rank, total_stars, total_destruction

        Also updates cwl_ended=1 when set_cwl_ended is True.
        Skips rows where all three values already match to minimise DB writes.

        Returns the number of rows actually updated.
        """
        await self._ensure_connection()
        # Read current stored values in one query for comparison
        cursor = await self._conn.execute(
            "SELECT clan_tag, group_rank, total_stars, total_destruction, cwl_ended "
            "FROM cwl_league_groups WHERE league_group_id = ? AND cwl_season = ?",
            (group_id, cwl_season),
        )
        stored: Dict[str, Any] = {
            r["clan_tag"]: {
                "group_rank": r["group_rank"],
                "total_stars": r["total_stars"],
                "total_destruction": r["total_destruction"],
                "cwl_ended": r["cwl_ended"],
            }
            for r in await cursor.fetchall()
        }

        updates: List[Tuple[int, int, float, int, str, str]] = []
        for cs in clan_stats:
            ct = cs["clan_tag"]
            cur = stored.get(ct, {})
            new_ended = 1 if set_cwl_ended else 0
            if (
                cur.get("group_rank") == cs["group_rank"]
                and cur.get("total_stars") == cs["total_stars"]
                and abs((cur.get("total_destruction") or 0.0) - cs["total_destruction"]) < 0.001
                and cur.get("cwl_ended") == new_ended
            ):
                continue  # Nothing changed — skip this row
            updates.append((
                cs["group_rank"], cs["total_stars"], cs["total_destruction"],
                new_ended, cwl_season, ct,
            ))

        if not updates:
            return 0

        await self._write_lock.acquire()
        try:
            await self._conn.executemany(
                "UPDATE cwl_league_groups "
                "SET group_rank=?, total_stars=?, total_destruction=?, cwl_ended=? "
                "WHERE cwl_season=? AND clan_tag=?",
                updates,
            )
            await self._conn.commit()
        finally:
            self._write_lock.release()
        return len(updates)

    async def get_active_cwl_group_member_tags(
        self, cwl_season: str, tracked_clan_tags: List[str]
    ) -> List[str]:
        """
        Return all clan_tags in non-ended CWL groups that contain at least one
        of the given tracked_clan_tags, for any season whose key starts with
        cwl_season (e.g. "2026-06" matches "2026-06-01" and "2026-06-16").

        Used by the main update loop to expand the per-cycle fetch list during
        CWL so star totals for all 8 group members remain up-to-date.
        """
        if not tracked_clan_tags:
            return []
        await self._ensure_connection()
        placeholders = ",".join("?" * len(tracked_clan_tags))
        params: Tuple[Any, ...] = (f"{cwl_season}%", *tracked_clan_tags)
        cursor = await self._conn.execute(
            f"SELECT DISTINCT g2.clan_tag "
            f"FROM cwl_league_groups g1 "
            f"JOIN cwl_league_groups g2 "
            f"  ON g1.league_group_id = g2.league_group_id AND g1.cwl_season = g2.cwl_season "
            f"WHERE g1.cwl_season LIKE ? "
            f"  AND g1.cwl_ended = 0 "
            f"  AND g1.clan_tag IN ({placeholders})",
            params,
        )
        rows = await cursor.fetchall()
        return [row["clan_tag"] for row in rows]

    async def update_cwl_league_rank(
        self, cwl_season: str, group_id: str, league_rank: str, *, force: bool = False
    ) -> None:
        """Set league_rank for all clans in a group.

        Writes when the stored value is NULL **or** when the season is still
        active (cwl_ended = 0).  Once the season has been marked ended
        (cwl_ended = 1), the row is frozen — this prevents a post-season
        get_clan() call (which would return the clan's NEW league after
        promotion/demotion) from overwriting the historically-correct value.

        Called from _process_league_group_response when the league name is known.

        force=True bypasses the freeze entirely. Used only by the deliberate,
        already-verified self-heal cross-check in
        QBhelperfunctions._cwl_self_heal_league_rank / update_cwl_group_stats —
        never by the original derivation path this guard was built to constrain.
        """
        await self._ensure_connection()
        await self._write_lock.acquire()
        try:
            if force:
                sql = (
                    "UPDATE cwl_league_groups SET league_rank = ? "
                    "WHERE league_group_id = ? AND cwl_season = ?"
                )
            else:
                sql = (
                    "UPDATE cwl_league_groups SET league_rank = ? "
                    "WHERE league_group_id = ? AND cwl_season = ? "
                    "AND (league_rank IS NULL OR cwl_ended = 0)"
                )
            await self._conn.execute(sql, (league_rank, group_id, cwl_season))
            await self._conn.commit()
        finally:
            self._write_lock.release()

    def get_cwl_round_for_war_tag_sync(self, war_tag: str) -> Optional[int]:
        """
        Synchronous lookup of cwl_round for a given war_tag.

        Used inside _process_war_history (sync context) to inject round_number
        into the summary dict before DB insertion.  Returns None if not found.
        """
        if not war_tag or war_tag == '#0':
            return None
        try:
            with self._sync_conn() as conn:
                cursor = conn.execute(
                    "SELECT cwl_round FROM cwl_league_rounds WHERE war_tag = ?",
                    (war_tag,),
                )
                row = cursor.fetchone()
                return int(row["cwl_round"]) if row else None
        except Exception:
            return None

    def is_cwl_ended_for_clan_sync(self, clan_tag: str, cwl_season: str) -> bool:
        """
        Synchronous check: returns True if cwl_ended=1 for this clan+season.

        Returns False when unknown (no row, DB error) so callers treat the
        ambiguous case conservatively (allow the API call).
        """
        try:
            with self._sync_conn() as conn:
                cursor = conn.execute(
                    "SELECT cwl_ended FROM cwl_league_groups "
                    "WHERE cwl_season = ? AND clan_tag = ? LIMIT 1",
                    (cwl_season, clan_tag),
                )
                row = cursor.fetchone()
                return bool(row["cwl_ended"]) if row else False
        except Exception:
            return False

    def is_latest_cwl_season_ended_sync(self, clan_tag: str) -> bool:
        """
        Synchronous check: returns True if the most recent CWL season recorded
        for this clan has cwl_ended=1.

        Unlike is_cwl_ended_for_clan_sync(), this does not require the caller to
        know the season key format.  Seasons compare correctly in lexicographic
        order for both "YYYY-MM" (historic) and "YYYY-MM-DD" (API-keyed) formats:
        "2026-06" < "2026-06-01" < "2026-06-16", so ORDER BY DESC gives the
        true latest season regardless of which format is stored.

        Returns False when unknown (no row, DB error) so callers treat the
        ambiguous case conservatively (allow the API call).
        """
        try:
            with self._sync_conn() as conn:
                cursor = conn.execute(
                    "SELECT cwl_ended FROM cwl_league_groups "
                    "WHERE clan_tag = ? "
                    "ORDER BY cwl_season DESC LIMIT 1",
                    (clan_tag,),
                )
                row = cursor.fetchone()
                return bool(row["cwl_ended"]) if row else False
        except Exception:
            return False

    def has_active_cwl_group_sync(self, clan_tag: str) -> bool:
        """
        Synchronous check: returns True if clan_tag has at least one
        cwl_league_groups row with cwl_ended=0 (an active / in-progress CWL
        season).

        Unlike is_latest_cwl_season_ended_sync(), this returns False when no
        row exists at all (clan has never participated in a tracked CWL).
        That distinction matters for the notInWar CWL fallback in
        fetch_clan_war_data: we must not issue an extra get_league_group() API
        call every cycle for clans that simply have no CWL history.

        Returns False on DB error (safe default — skip the fallback).
        """
        try:
            with self._sync_conn() as conn:
                cursor = conn.execute(
                    "SELECT 1 FROM cwl_league_groups "
                    "WHERE clan_tag = ? AND cwl_ended = 0 LIMIT 1",
                    (clan_tag,),
                )
                return cursor.fetchone() is not None
        except Exception:
            return False

    async def is_cwl_ended_for_clan(self, clan_tag: str, cwl_season: str) -> bool:
        """
        Async check: returns True if cwl_ended=1 for this clan+season.

        Returns False when unknown (no row, DB error) so callers treat the
        ambiguous case conservatively (allow processing).
        """
        try:
            await self._ensure_connection()
            cursor = await self._conn.execute(
                "SELECT cwl_ended FROM cwl_league_groups "
                "WHERE cwl_season = ? AND clan_tag = ? LIMIT 1",
                (cwl_season, clan_tag),
            )
            row = await cursor.fetchone()
            return bool(row["cwl_ended"]) if row else False
        except Exception:
            return False

    async def clan_has_in_progress_cwl_data(self, clan_tag: str) -> bool:
        """Return True if clan_tag's most recent cwl_league_groups season is
        still active (cwl_ended=0) AND at least one war_summary row (is_cwl=1)
        is already archived for that same season.

        Season-agnostic (unlike clan_has_cwl_data_for_season, which requires the
        caller to already know the exact season string) — reuses the same
        "most recent season, cwl_ended flag" bookkeeping as
        is_latest_cwl_season_ended_sync(), so it works from any call site that
        only has a clan_tag in scope.

        Used to guard against demoting (stopping polling for) a clan mid-season
        when doing so would abandon a partially-captured CWL season — see
        CLAN_WAR_TRACKING.md write-path 7 / write-path 5's demotion note.
        Returns False on DB error or no cwl_league_groups row at all (safe
        default — proceed with demotion as before this guard existed).
        """
        try:
            await self._ensure_connection()
            cursor = await self._conn.execute(
                """
                WITH clg AS (
                    SELECT clan_tag, cwl_season, cwl_ended FROM main.cwl_league_groups
                    UNION ALL
                    SELECT clan_tag, cwl_season, cwl_ended FROM history.cwl_league_groups
                ),
                latest AS (
                    SELECT cwl_season, cwl_ended,
                           ROW_NUMBER() OVER (ORDER BY cwl_season DESC) AS rn
                    FROM clg
                    WHERE clan_tag = ?
                )
                SELECT 1
                FROM   latest l
                WHERE  l.rn = 1 AND l.cwl_ended = 0
                  AND EXISTS (
                        SELECT 1 FROM main.war_summary ws
                        WHERE ws.clan_tag = ? AND ws.cwl_season = l.cwl_season AND ws.is_cwl = 1
                        UNION ALL
                        SELECT 1 FROM history.war_summary ws
                        WHERE ws.clan_tag = ? AND ws.cwl_season = l.cwl_season AND ws.is_cwl = 1
                  )
                LIMIT 1
                """,
                (clan_tag, clan_tag, clan_tag),
            )
            row = await cursor.fetchone()
            return row is not None
        except Exception:
            return False

    async def get_all_clans_dict(self) -> Dict[str, Dict[str, Any]]:
        """Get all clans as dictionary (clan_tag -> clan_data)."""
        await self._ensure_connection()
        
        cursor = await self._conn.execute("SELECT * FROM clans")
        rows = await cursor.fetchall()
        
        return {
            row["clan_tag"]: {
                "name": row["name"],
                "has_active_subscriptions": bool(row["has_active_subscriptions"]),
                "last_war_update": row["last_war_update"],
                "warlog_is_public": bool(row["warlog_is_public"]),
                "last_checked_via_api": row["last_checked_via_api"],
                "war_league": row["war_league"],
                # Default True for rows that pre-date this column (NULL after migration)
                "track_war_updates": bool(row["track_war_updates"]) if row["track_war_updates"] is not None else True,
                # Default False for rows that pre-date this column (NULL after migration)
                "is_deleted": bool(row["is_deleted"]) if row["is_deleted"] is not None else False,
            }
            for row in rows
        }
    
    async def get_user(self, discord_id: str) -> Optional[Dict[str, Any]]:
        """Get user account with all players from database."""
        await self._ensure_connection()
        
        cursor = await self._conn.execute(
            "SELECT * FROM users WHERE discord_id = ?",
            (discord_id,)
        )
        row = await cursor.fetchone()
        
        if not row:
            return None
        
        # Get user's players
        players_cursor = await self._conn.execute(
            "SELECT * FROM user_players WHERE discord_id = ? ORDER BY is_primary DESC, added_at",
            (discord_id,)
        )
        players_rows = await players_cursor.fetchall()
        
        players: List[Dict[str, Any]] = [
            {
                "player_tag": str(p_row["player_tag"]),
                "player_name": str(p_row["player_name"]) if p_row["player_name"] else None,
                "verified": bool(p_row["verified"]),
                "th_level": int(p_row["th_level"]) if p_row["th_level"] else None,
                "current_clan_tag": str(p_row["current_clan_tag"]) if p_row["current_clan_tag"] else None,
                "is_primary": bool(p_row["is_primary"])
            }
            for p_row in players_rows
        ]
        
        # Get user's buddies (watched players for Save-your-Buddy feature)
        buddies_cursor = await self._conn.execute(
            "SELECT player_tag, player_name FROM user_buddies WHERE discord_id = ? ORDER BY added_at",
            (discord_id,)
        )
        buddies_rows = await buddies_cursor.fetchall()
        watched_players: List[Dict[str, Any]] = [
            {"player_tag": str(b_row["player_tag"]), "player_name": str(b_row["player_name"])}
            for b_row in buddies_rows
        ]

        return {
            "display_name": row["display_name"],
            "notification_settings": {
                "notification_mode": row["notification_mode"],
                "notification_type": row["notification_type"],
                "hours_before_end": row["hours_before_end"],
                "war_reminders": bool(row["war_reminders_enabled"])
            },
            "user_language": row["user_language"],
            "user_language_locked": bool(row["user_language_locked"]),
            "players": players,
            "watched_players": watched_players
        }
    
    async def save_user(self, discord_id: str, user_data: Dict[str, Any]) -> None:
        """Save or update user account in database (atomic transaction).

        Retries on ``database is locked`` so user interactions survive
        concurrent finalization bursts.
        """
        await self._retry_on_locked(
            lambda: self._save_user_impl(discord_id, user_data)
        )

    async def _upsert_users_row(self, discord_id: str, user_data: Dict[str, Any]) -> None:
        """Shared `users` table UPSERT for `_save_user_impl`'s primary and FK-recovery paths.

        Extracted so a future schema change to this INSERT only needs to be made once instead
        of twice in near-identical, easy-to-drift-apart copies.
        """
        notif = user_data.get("notification_settings", {})
        await self._conn.execute("""
            INSERT INTO users
            (discord_id, display_name, notification_mode, notification_type, hours_before_end,
             war_reminders_enabled, user_language, user_language_locked)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
                display_name = excluded.display_name,
                notification_mode = excluded.notification_mode,
                notification_type = excluded.notification_type,
                hours_before_end = excluded.hours_before_end,
                war_reminders_enabled = excluded.war_reminders_enabled,
                user_language = excluded.user_language,
                user_language_locked = excluded.user_language_locked
        """, (
            discord_id,
            user_data.get("display_name", "Unknown"),
            notif.get("notification_mode", "repeated"),
            notif.get("notification_type", "all_wars"),
            notif.get("hours_before_end", 4),
            1 if notif.get("war_reminders", True) else 0,
            user_data.get("user_language"),
            1 if user_data.get("user_language_locked", False) else 0
        ))

    async def _replace_user_players_rows(
        self, discord_id: str, players: List[Dict[str, Any]], null_clan_tags: bool
    ) -> None:
        """Shared `user_players` DELETE+reinsert for `_save_user_impl`'s primary and FK-recovery paths.

        Args:
            null_clan_tags: True for the FK-recovery retry — a referenced clan row doesn't
                exist, so current_clan_tag is cleared for every player instead of failing again.
        """
        await self._conn.execute("DELETE FROM user_players WHERE discord_id = ?", (discord_id,))
        for player in players:
            await self._conn.execute("""
                INSERT INTO user_players
                (discord_id, player_tag, player_name, verified, th_level, current_clan_tag, is_primary)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                discord_id,
                player["player_tag"],
                player["player_name"],
                1 if player.get("verified", False) else 0,
                player.get("th_level"),
                None if null_clan_tags else player.get("current_clan_tag"),
                1 if player.get("is_primary", False) else 0
            ))

    async def _save_user_impl(self, discord_id: str, user_data: Dict[str, Any]) -> None:
        """Inner implementation of save_user (retry-wrapped by caller)."""
        await self._ensure_connection()

        # Gate: wait for any in-progress sync bulk write to finish before
        # acquiring _write_lock.  The two write paths use separate Python locks
        # (_sync_write_lock vs _write_lock) and separate SQLite connections, so
        # without this fence the async BEGIN races the sync BEGIN and loses with
        # "database is locked" (SQLite allows only one writer at a time).
        await asyncio.to_thread(self._sync_write_fence)

        # Ensure all referenced clans exist BEFORE starting the transaction
        # (FK constraint on user_players.current_clan_tag -> clans.clan_tag)
        await self._write_lock.acquire()
        try:
            for player in user_data.get("players", []):
                clan_tag = player.get("current_clan_tag")
                if clan_tag:
                    await self._ensure_clan_exists(clan_tag)
            
            await self._conn.execute("BEGIN")

            await self._upsert_users_row(discord_id, user_data)

            # Delete existing players
            # Forensic guard: this DELETE+reinsert replaces the user's entire players
            # list with whatever the caller passed. A shrink to zero is legitimate only
            # when deliberately unlinking the last account — log it loudly so an
            # accidental cache-blind wipe (see CacheManager.users_loaded) is visible
            # in the log the moment it happens instead of weeks later.
            if not user_data.get("players"):
                _existing_cursor = await self._conn.execute(
                    "SELECT COUNT(*) AS cnt FROM user_players WHERE discord_id = ?", (discord_id,)
                )
                _existing_row = await _existing_cursor.fetchone()
                _existing_count = int(_existing_row["cnt"]) if _existing_row else 0
                if _existing_count > 0:
                    logging.warning(
                        f"[DB-WRITE] save_user({discord_id}) is replacing {_existing_count} existing "
                        f"user_players row(s) with an EMPTY players list — legitimate only for an "
                        f"explicit unlink of the last account"
                    )
            await self._replace_user_players_rows(discord_id, user_data.get("players", []), null_clan_tags=False)

            # Delete existing buddies (Save-your-Buddy)
            await self._conn.execute("DELETE FROM user_buddies WHERE discord_id = ?", (discord_id,))

            # Insert buddies
            for buddy in user_data.get("watched_players", []):
                await self._conn.execute("""
                    INSERT OR IGNORE INTO user_buddies
                    (discord_id, player_tag, player_name)
                    VALUES (?, ?, ?)
                """, (
                    discord_id,
                    buddy["player_tag"],
                    buddy.get("player_name", "Unknown")
                ))

            await self._conn.commit()
        except Exception as e:
            await self._conn.rollback()
            logging.error(f"[DB-WRITE] Transaction failed for save_user({discord_id}): {e}")
            # FK constraint failure: clan tag referenced by a player doesn't exist in clans table.
            # Retry once with current_clan_tag=NULL for all players so the rest of the row is saved.
            if "FOREIGN KEY" in str(e):
                logging.warning(f"[DB-WRITE] Retrying save_user({discord_id}) with current_clan_tag=NULL (FK recovery)")
                try:
                    await self._conn.execute("BEGIN")
                    await self._upsert_users_row(discord_id, user_data)
                    await self._replace_user_players_rows(discord_id, user_data.get("players", []), null_clan_tags=True)
                    await self._conn.commit()
                    logging.info(f"[DB-WRITE] FK recovery succeeded for save_user({discord_id}) — current_clan_tag cleared")
                    return  # recovered successfully, don't re-raise
                except Exception as e2:
                    await self._conn.rollback()
                    logging.error(f"[DB-WRITE] FK recovery also failed for save_user({discord_id}): {e2}")
            raise
        finally:
            self._write_lock.release()
    
    async def delete_user(self, discord_id: str) -> None:
        """Delete user and all associated players (CASCADE) from database."""
        await self._ensure_connection()
        
        await self._write_lock.acquire()
        try:
            await self._conn.execute(
                "DELETE FROM users WHERE discord_id = ?",
                (discord_id,)
            )
            await self._conn.commit()
        finally:
            self._write_lock.release()

    async def get_all_users_dict(self) -> Dict[str, Dict[str, Any]]:
        """Get all users with their players as dictionary (discord_id -> user_data)."""
        await self._ensure_connection()
        
        cursor = await self._conn.execute("SELECT * FROM users")
        users_rows = await cursor.fetchall()
        
        result: Dict[str, Dict[str, Any]] = {}
        for row in users_rows:
            discord_id = str(row["discord_id"])
            user = await self.get_user(discord_id)
            if user:
                result[discord_id] = user
        
        return result
    
    async def get_guild_config(self, guild_id: str) -> Optional[Dict[str, Any]]:
        """Get guild configuration with member clans and families."""
        await self._ensure_connection()
        
        cursor = await self._conn.execute(
            "SELECT * FROM guild_config WHERE guild_id = ?",
            (guild_id,)
        )
        row = await cursor.fetchone()
        
        if not row:
            return None
        
        # Get member families
        families_cursor = await self._conn.execute(
            "SELECT family_tag FROM guild_member_families WHERE guild_id = ?",
            (guild_id,)
        )
        families = [f[0] for f in await families_cursor.fetchall()]
        
        # Get member clans
        clans_cursor = await self._conn.execute(
            "SELECT clan_tag FROM guild_member_clans WHERE guild_id = ?",
            (guild_id,)
        )
        clans = [c[0] for c in await clans_cursor.fetchall()]

        # Get welcome-message family selections (clan-link mode)
        welcome_families_cursor = await self._conn.execute(
            "SELECT family_tag FROM guild_welcome_families WHERE guild_id = ?",
            (guild_id,)
        )
        welcome_family_tags = [f[0] for f in await welcome_families_cursor.fetchall()]

        # Get welcome-message individual clan selections (clan-link mode)
        welcome_clans_cursor = await self._conn.execute(
            "SELECT clan_tag FROM guild_welcome_clans WHERE guild_id = ?",
            (guild_id,)
        )
        welcome_clan_tags = [c[0] for c in await welcome_clans_cursor.fetchall()]

        # Backward-compat: guilds configured under the old single-clan welcome_clan_tag
        # column (before multi-select) haven't populated the new junction tables yet.
        # Fall back to that single tag so their existing welcome message keeps working
        # until the admin next saves via the new selector.
        if not welcome_family_tags and not welcome_clan_tags and row["welcome_clan_tag"]:
            welcome_clan_tags = [row["welcome_clan_tag"]]

        # Load per-clan role IDs
        clan_roles_cursor = await self._conn.execute(
            "SELECT clan_tag, role_id FROM guild_clan_roles WHERE guild_id = ?",
            (guild_id,)
        )
        clan_roles: Dict[str, str] = {r[0]: r[1] for r in await clan_roles_cursor.fetchall()}

        # Access by column name – immune to column order changes from ALTER TABLE migrations.
        return {
            "language": row["language"],
            "newbie_role_id": row["newbie_role_id"],
            "member_role_id": row["member_role_id"],
            "role_system_enabled": bool(row["role_system_enabled"]),
            "registration_channel_id": row["registration_channel_id"],
            "war_notification_channel_id": row["war_notification_channel_id"],
            "registration_message_enabled": bool(row["registration_message_enabled"]),
            "registration_message_id": row["registration_message_id"],
            "registration_message_last_bump_iso": row["registration_message_last_bump_iso"],
            "channel_war_notifications_enabled": bool(row["channel_war_notifications_enabled"]),
            "war_notification_threshold_hours": row["war_notification_threshold_hours"],
            "coc_role_enabled": bool(row["coc_role_enabled"]) if row["coc_role_enabled"] is not None else False,
            "clan_role_enabled": bool(row["clan_role_enabled"]) if row["clan_role_enabled"] is not None else False,
            "coc_role_member_id": row["coc_role_member_id"],
            "coc_role_elder_id": row["coc_role_elder_id"],
            "coc_role_coleader_id": row["coc_role_coleader_id"],
            "coc_role_leader_id": row["coc_role_leader_id"],
            "welcome_message_enabled": bool(row["welcome_message_enabled"]) if row["welcome_message_enabled"] is not None else False,
            "welcome_message_mode": row["welcome_message_mode"] or "clan_link",
            "welcome_apply_channel_id": row["welcome_apply_channel_id"],
            "welcome_clan_tag": row["welcome_clan_tag"],
            "welcome_clan_tags": welcome_clan_tags,
            "welcome_family_tags": welcome_family_tags,
            "member_families": families,
            "member_clans": clans,
            "clan_roles": clan_roles,
            "cwl_hub_channel_id": row["cwl_hub_channel_id"],
            "cwl_hub_message_id": row["cwl_hub_message_id"],
            "cwl_hub_message_enabled": bool(row["cwl_hub_message_enabled"]) if row["cwl_hub_message_enabled"] is not None else False,
            "cwl_hub_message_last_bump_iso": row["cwl_hub_message_last_bump_iso"],
            "cwl_management_channel_id": row["cwl_management_channel_id"],
            "cwl_management_message_id": row["cwl_management_message_id"],
            "cwl_management_message_enabled": bool(row["cwl_management_message_enabled"]) if row["cwl_management_message_enabled"] is not None else False,
            "cwl_management_message_last_bump_iso": row["cwl_management_message_last_bump_iso"],
            "cwl_retention_months": row["cwl_retention_months"] if row["cwl_retention_months"] is not None else 0,
            "cwl_selected_season": row["cwl_selected_season"],
            "cwl_enrollment_include_all_linked_accounts": bool(row["cwl_enrollment_include_all_linked_accounts"]) if row["cwl_enrollment_include_all_linked_accounts"] is not None else False,
            "timezone_name": row["timezone_name"] if row["timezone_name"] is not None else "UTC",
        }
    
    async def save_guild_config(self, guild_id: str, config: Dict[str, Any]) -> None:
        """Save or update guild configuration in database (atomic transaction).

        Retries on ``database is locked`` so user interactions survive
        concurrent finalization bursts.
        """
        await self._retry_on_locked(
            lambda: self._save_guild_config_impl(guild_id, config)
        )

    async def _save_guild_config_impl(self, guild_id: str, config: Dict[str, Any]) -> None:
        """Inner implementation of save_guild_config (retry-wrapped by caller)."""
        await self._ensure_connection()
        
        # Ensure all referenced clans/families exist BEFORE starting the transaction
        # (FK constraints: guild_member_clans.clan_tag -> clans, guild_member_families.family_tag -> clan_families)
        await self._write_lock.acquire()
        try:
            for clan_tag in config.get("member_clans", []):
                await self._ensure_clan_exists(clan_tag)
            for family_tag in config.get("member_families", []):
                await self._ensure_family_exists(family_tag)
            for clan_tag in config.get("welcome_clan_tags", []):
                await self._ensure_clan_exists(clan_tag)
            for family_tag in config.get("welcome_family_tags", []):
                await self._ensure_family_exists(family_tag)
            
            await self._conn.execute("BEGIN")
            
            await self._conn.execute("""
                INSERT INTO guild_config
                (guild_id, language, newbie_role_id, member_role_id, role_system_enabled,
                 registration_channel_id, war_notification_channel_id, registration_message_enabled,
                 registration_message_id, registration_message_last_bump_iso,
                 channel_war_notifications_enabled, war_notification_threshold_hours,
                 coc_role_enabled, clan_role_enabled,
                 coc_role_member_id, coc_role_elder_id, coc_role_coleader_id, coc_role_leader_id,
                 welcome_message_enabled, welcome_message_mode, welcome_apply_channel_id, welcome_clan_tag,
                 cwl_hub_channel_id, cwl_hub_message_id, cwl_hub_message_enabled, cwl_hub_message_last_bump_iso,
                 cwl_management_channel_id, cwl_management_message_id, cwl_management_message_enabled,
                 cwl_management_message_last_bump_iso, cwl_retention_months, cwl_selected_season,
                 cwl_enrollment_include_all_linked_accounts, timezone_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    language = excluded.language,
                    newbie_role_id = excluded.newbie_role_id,
                    member_role_id = excluded.member_role_id,
                    role_system_enabled = excluded.role_system_enabled,
                    registration_channel_id = excluded.registration_channel_id,
                    war_notification_channel_id = excluded.war_notification_channel_id,
                    registration_message_enabled = excluded.registration_message_enabled,
                    registration_message_id = excluded.registration_message_id,
                    registration_message_last_bump_iso = excluded.registration_message_last_bump_iso,
                    channel_war_notifications_enabled = excluded.channel_war_notifications_enabled,
                    war_notification_threshold_hours = excluded.war_notification_threshold_hours,
                    coc_role_enabled = excluded.coc_role_enabled,
                    clan_role_enabled = excluded.clan_role_enabled,
                    coc_role_member_id = excluded.coc_role_member_id,
                    coc_role_elder_id = excluded.coc_role_elder_id,
                    coc_role_coleader_id = excluded.coc_role_coleader_id,
                    coc_role_leader_id = excluded.coc_role_leader_id,
                    welcome_message_enabled = excluded.welcome_message_enabled,
                    welcome_message_mode = excluded.welcome_message_mode,
                    welcome_apply_channel_id = excluded.welcome_apply_channel_id,
                    welcome_clan_tag = excluded.welcome_clan_tag,
                    cwl_hub_channel_id = excluded.cwl_hub_channel_id,
                    cwl_hub_message_id = excluded.cwl_hub_message_id,
                    cwl_hub_message_enabled = excluded.cwl_hub_message_enabled,
                    cwl_hub_message_last_bump_iso = excluded.cwl_hub_message_last_bump_iso,
                    cwl_management_channel_id = excluded.cwl_management_channel_id,
                    cwl_management_message_id = excluded.cwl_management_message_id,
                    cwl_management_message_enabled = excluded.cwl_management_message_enabled,
                    cwl_management_message_last_bump_iso = excluded.cwl_management_message_last_bump_iso,
                    cwl_retention_months = excluded.cwl_retention_months,
                    cwl_selected_season = excluded.cwl_selected_season,
                    cwl_enrollment_include_all_linked_accounts = excluded.cwl_enrollment_include_all_linked_accounts,
                    timezone_name = excluded.timezone_name
            """, (
                guild_id,
                config.get("language", "en"),
                config.get("newbie_role_id"),
                config.get("member_role_id"),
                1 if config.get("role_system_enabled", False) else 0,
                config.get("registration_channel_id"),
                config.get("war_notification_channel_id"),
                1 if config.get("registration_message_enabled", False) else 0,
                config.get("registration_message_id"),
                config.get("registration_message_last_bump_iso"),
                1 if config.get("channel_war_notifications_enabled", False) else 0,
                config.get("war_notification_threshold_hours", 2.0),
                1 if config.get("coc_role_enabled", False) else 0,
                1 if config.get("clan_role_enabled", False) else 0,
                config.get("coc_role_member_id"),
                config.get("coc_role_elder_id"),
                config.get("coc_role_coleader_id"),
                config.get("coc_role_leader_id"),
                1 if config.get("welcome_message_enabled", False) else 0,
                config.get("welcome_message_mode", "clan_link"),
                config.get("welcome_apply_channel_id"),
                config.get("welcome_clan_tag"),
                config.get("cwl_hub_channel_id"),
                config.get("cwl_hub_message_id"),
                1 if config.get("cwl_hub_message_enabled", False) else 0,
                config.get("cwl_hub_message_last_bump_iso"),
                config.get("cwl_management_channel_id"),
                config.get("cwl_management_message_id"),
                1 if config.get("cwl_management_message_enabled", False) else 0,
                config.get("cwl_management_message_last_bump_iso"),
                config.get("cwl_retention_months", 0),
                config.get("cwl_selected_season"),
                1 if config.get("cwl_enrollment_include_all_linked_accounts", False) else 0,
                config.get("timezone_name", "UTC"),
            ))
            
            # Delete existing member families and clans
            await self._conn.execute("DELETE FROM guild_member_families WHERE guild_id = ?", (guild_id,))
            await self._conn.execute("DELETE FROM guild_member_clans WHERE guild_id = ?", (guild_id,))
            
            # Insert member families
            for family_tag in config.get("member_families", []):
                await self._conn.execute(
                    "INSERT OR IGNORE INTO guild_member_families (guild_id, family_tag) VALUES (?, ?)",
                    (guild_id, family_tag)
                )
            
            # Insert member clans
            for clan_tag in config.get("member_clans", []):
                await self._conn.execute(
                    "INSERT OR IGNORE INTO guild_member_clans (guild_id, clan_tag) VALUES (?, ?)",
                    (guild_id, clan_tag)
                )

            # Delete existing welcome-message family/clan selections and re-insert
            await self._conn.execute("DELETE FROM guild_welcome_families WHERE guild_id = ?", (guild_id,))
            await self._conn.execute("DELETE FROM guild_welcome_clans WHERE guild_id = ?", (guild_id,))

            for family_tag in config.get("welcome_family_tags", []):
                await self._conn.execute(
                    "INSERT OR IGNORE INTO guild_welcome_families (guild_id, family_tag) VALUES (?, ?)",
                    (guild_id, family_tag)
                )

            for clan_tag in config.get("welcome_clan_tags", []):
                await self._conn.execute(
                    "INSERT OR IGNORE INTO guild_welcome_clans (guild_id, clan_tag) VALUES (?, ?)",
                    (guild_id, clan_tag)
                )
            
            await self._conn.commit()
        except Exception as e:
            await self._conn.rollback()
            logging.error(f"[DB-WRITE] Transaction failed for save_guild_config({guild_id}): {e}")
            raise
        finally:
            self._write_lock.release()
    
    async def get_all_guild_configs_dict(self) -> Dict[str, Dict[str, Any]]:
        """Get all guild configurations as dictionary (guild_id -> config)."""
        await self._ensure_connection()
        
        cursor = await self._conn.execute("SELECT guild_id FROM guild_config")
        guild_ids = [str(row["guild_id"]) for row in await cursor.fetchall()]
        
        result: Dict[str, Dict[str, Any]] = {}
        for guild_id in guild_ids:
            config = await self.get_guild_config(guild_id)
            if config:
                result[guild_id] = config
        
        return result

    async def save_guild_clan_role(self, guild_id: str, clan_tag: str, role_id: str) -> None:
        """Persist a per-clan Discord role ID for a guild (upsert)."""
        await self._ensure_connection()
        async with self._write_lock:
            await self._conn.execute(
                "INSERT INTO guild_clan_roles (guild_id, clan_tag, role_id) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id, clan_tag) DO UPDATE SET role_id = excluded.role_id",
                (guild_id, clan_tag, role_id)
            )
            await self._conn.commit()

    async def delete_guild_clan_role(self, guild_id: str, clan_tag: str) -> None:
        """Remove the per-clan Discord role record for a guild+clan."""
        await self._ensure_connection()
        async with self._write_lock:
            await self._conn.execute(
                "DELETE FROM guild_clan_roles WHERE guild_id = ? AND clan_tag = ?",
                (guild_id, clan_tag)
            )
            await self._conn.commit()

    async def get_subscriptions_by_channel(self, guild_id: str, channel_id: str) -> Dict[str, Dict[str, Any]]:
        """Get all subscriptions for a channel grouped by clan_tag."""
        await self._ensure_connection()
        
        cursor = await self._conn.execute("""
            SELECT clan_tag, subscription_type, year 
            FROM subscriptions 
            WHERE guild_id = ? AND channel_id = ?
        """, (guild_id, channel_id))
        rows = await cursor.fetchall()
        
        result: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            clan_tag = row["clan_tag"]
            sub_type = row["subscription_type"]
            year = row["year"]
            
            if clan_tag not in result:
                result[clan_tag] = {}
            
            if year:
                result[clan_tag][sub_type] = {"year": year}
            else:
                result[clan_tag][sub_type] = True
        
        return result
    
    async def save_subscriptions_for_channel(self, guild_id: str, channel_id: str,
                                            subscriptions: List[Dict[str, Any]]) -> None:
        """
        Save all subscriptions for a channel (replaces existing).

        Retries on ``database is locked`` so user interactions survive
        concurrent finalization bursts.

        Args:
            guild_id: Discord guild ID
            channel_id: Discord channel ID
            subscriptions: List of subscription dicts:
                [
                    {"clan_tag": "#ABC123", "subscription_type": "attack", "year": 2025},
                    {"clan_tag": None, "subscription_type": "playerlist"}
                ]
        """
        await self._retry_on_locked(
            lambda: self._save_subscriptions_for_channel_impl(guild_id, channel_id, subscriptions)
        )

    async def _save_subscriptions_for_channel_impl(self, guild_id: str, channel_id: str,
                                                    subscriptions: List[Dict[str, Any]]) -> None:
        """Inner implementation of save_subscriptions_for_channel (retry-wrapped)."""
        await self._ensure_connection()
        
        # Collect clan tags that need to exist and ensure them BEFORE starting the transaction
        # (avoids _ensure_clan_exists → _save_clan_unlocked commit racing with an open BEGIN)
        clan_tags_needed: set[str] = set()
        for sub in subscriptions:
            clan_tag = sub.get('clan_tag')
            if clan_tag:  # Skip None values (playerlist subscriptions)
                clan_tags_needed.add(clan_tag)
        
        await self._write_lock.acquire()
        try:
            for clan_tag in clan_tags_needed:
                await self._ensure_clan_exists(clan_tag)
            
            await self._conn.execute("BEGIN")
            
            # Delete existing subscriptions for this channel
            await self._conn.execute(
                "DELETE FROM subscriptions WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel_id)
            )
            
            # Insert new subscriptions
            for sub in subscriptions:
                year = sub.get('year')
                # Convert "current" to None for database storage
                if year == "current" or year is True:
                    year = None
                elif isinstance(year, bool):
                    year = None
                
                await self._conn.execute("""
                    INSERT OR IGNORE INTO subscriptions 
                    (guild_id, channel_id, clan_tag, subscription_type, year)
                    VALUES (?, ?, ?, ?, ?)
                """, (guild_id, channel_id, sub.get('clan_tag'), sub.get('subscription_type'), year))
            
            await self._conn.commit()
        except Exception as e:
            await self._conn.rollback()
            logging.error(f"[DB-WRITE] Transaction failed for save_subscriptions_for_channel({guild_id}/{channel_id}): {e}")
            raise
        finally:
            self._write_lock.release()

    async def delete_subscriptions_for_channel(
        self,
        guild_id: str,
        channel_id: str
    ) -> None:
        """Delete all subscriptions for a specific channel."""
        await self._ensure_connection()
        
        await self._write_lock.acquire()
        try:
            await self._conn.execute("""
                DELETE FROM subscriptions 
                WHERE guild_id = ? AND channel_id = ?
            """, (guild_id, channel_id))
            await self._conn.commit()
        finally:
            self._write_lock.release()

    async def delete_subscriptions_for_guild(self, guild_id: str) -> None:
        """Delete all subscriptions for a guild."""
        await self._ensure_connection()
        
        await self._write_lock.acquire()
        try:
            await self._conn.execute(
                "DELETE FROM subscriptions WHERE guild_id = ?",
                (guild_id,)
            )
            await self._conn.commit()
        finally:
            self._write_lock.release()

    async def get_all_subscriptions_dict(self) -> Dict[str, Dict[str, Dict[str, Dict[str, Any]]]]:
        """Get all subscriptions as nested dict (guild_id -> channel_id -> clan_tag -> sub_types)."""
        await self._ensure_connection()
        
        cursor = await self._conn.execute("SELECT * FROM subscriptions")
        rows = await cursor.fetchall()
        
        result: Dict[str, Dict[str, Dict[str, Dict[str, Any]]]] = {}
        
        for row in rows:
            guild_id = row["guild_id"]
            channel_id = row["channel_id"]
            clan_tag = row["clan_tag"]
            sub_type = row["subscription_type"]
            year = row["year"]
            
            if guild_id not in result:
                result[guild_id] = {}
            if channel_id not in result[guild_id]:
                result[guild_id][channel_id] = {}
            if clan_tag not in result[guild_id][channel_id]:
                result[guild_id][channel_id][clan_tag] = {}
            
            if year:
                result[guild_id][channel_id][clan_tag][sub_type] = {"year": year}
            else:
                result[guild_id][channel_id][clan_tag][sub_type] = True
        
        return result
    
    async def get_all_clan_families(self) -> Dict[str, Dict[str, Any]]:
        """
        Get all clan families with their members from database.
        
        Returns:
            Dict mapping family_tag to family info:
            {
                "family_tag": {
                    "name": "Family Name",
                    "clans": ["#CLAN1", "#CLAN2"],
                    "owned_by_guild": "guild_id"
                }
            }
        """
        await self._ensure_connection()
        
        # Get all families
        cursor = await self._conn.execute("SELECT family_tag, name, owned_by_guild FROM clan_families")
        families = await cursor.fetchall()
        
        result: Dict[str, Dict[str, Any]] = {}
        
        for family_tag, name, owned_by_guild in families:
            # Get member clans for this family
            cursor = await self._conn.execute(
                "SELECT clan_tag FROM clan_family_members WHERE family_tag = ?",
                (family_tag,)
            )
            members = await cursor.fetchall()
            member_tags = [row["clan_tag"] for row in members]
            
            result[family_tag] = {
                "name": name,
                "clans": member_tags,
                "owned_by_guild": owned_by_guild
            }
        
        return result
    
    async def get_all_subscriptions_for_cache(self) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
        """
        Get all subscriptions in cache format (guild_id -> channel_id -> list of subscriptions).
        
        Returns:
            Dict structure matching cache format:
            {
                "guild_id": {
                    "channel_id": [
                        {"clan_tag": "#ABC123", "subscription_type": "attack", "year": 2025},
                        {"clan_tag": None, "subscription_type": "playerlist"}
                    ]
                }
            }
        """
        await self._ensure_connection()
        
        cursor = await self._conn.execute("SELECT guild_id, channel_id, clan_tag, subscription_type, year FROM subscriptions")
        rows = await cursor.fetchall()
        
        result: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        
        for guild_id, channel_id, clan_tag, sub_type, year in rows:
            if guild_id not in result:
                result[guild_id] = {}
            if channel_id not in result[guild_id]:
                result[guild_id][channel_id] = []
            
            sub_entry: Dict[str, Any] = {
                "clan_tag": clan_tag,
                "subscription_type": sub_type
            }
            if year:
                sub_entry["year"] = year
            
            result[guild_id][channel_id].append(sub_entry)
        
        return result
    
    async def save_all_subscriptions(self, subscriptions: Dict[str, Dict[str, List[Dict[str, Any]]]]) -> None:
        """
        Save all subscriptions to database (replaces existing).
        
        Args:
            subscriptions: Full cache structure:
                {
                    "guild_id": {
                        "channel_id": [
                            {"clan_tag": "#ABC123", "subscription_type": "attack", "year": 2025},
                            {"clan_tag": None, "subscription_type": "playerlist"}
                        ]
                    }
                }
        """
        await self._ensure_connection()
        
        # Collect all unique clan tags and ensure they exist BEFORE starting the transaction
        # (avoids _ensure_clan_exists → _save_clan_unlocked commit racing with an open BEGIN)
        clan_tags_needed: set[str] = set()
        for channels in subscriptions.values():
            for sub_list in channels.values():
                for sub in sub_list:
                    clan_tag = sub.get('clan_tag')
                    if clan_tag:  # Skip None values (playerlist subscriptions)
                        clan_tags_needed.add(clan_tag)
        
        await self._write_lock.acquire()
        try:
            for clan_tag in clan_tags_needed:
                await self._ensure_clan_exists(clan_tag)
            
            await self._conn.execute("BEGIN")
            
            # Clear all existing subscriptions
            await self._conn.execute("DELETE FROM subscriptions")
            
            # Insert all subscriptions
            for guild_id, channels in subscriptions.items():
                for channel_id, sub_list in channels.items():
                    for sub in sub_list:
                        year = sub.get('year')
                        # Convert "current" to None for database storage
                        if year == "current" or year is True:
                            year = None
                        elif isinstance(year, bool):
                            year = None
                        
                        await self._conn.execute("""
                            INSERT OR IGNORE INTO subscriptions 
                            (guild_id, channel_id, clan_tag, subscription_type, year)
                            VALUES (?, ?, ?, ?, ?)
                        """, (guild_id, channel_id, sub.get('clan_tag'), sub.get('subscription_type'), year))
            
            await self._conn.commit()
        except Exception as e:
            await self._conn.rollback()
            logging.error(f"[DB-WRITE] Transaction failed for save_all_subscriptions: {e}")
            raise
        finally:
            self._write_lock.release()
    
    async def get_all_leaderboard_messages(self) -> Dict[str, Dict[str, Any]]:
        """
        Get all leaderboard messages in cache format.
        
        Returns:
            Dict structure matching cache format:
            {
                "message_key": {
                    "clan_tag": "#L2J0C0PY",
                    "channel_id": "123456789012345678",
                    "mode": "stars_08_2025",
                    "message_ids": "123456789",
                    "content_hash": "abc123def456..."
                }
            }
        """
        await self._ensure_connection()
        
        cursor = await self._conn.execute(
            "SELECT message_key, clan_tag, channel_id, mode, message_ids, content_hash FROM leaderboard_messages"
        )
        rows = await cursor.fetchall()
        
        result: Dict[str, Dict[str, Any]] = {}
        
        for message_key, clan_tag, channel_id, mode, message_ids, content_hash in rows:
            result[message_key] = {
                "clan_tag": clan_tag,
                "channel_id": channel_id,
                "mode": mode,
                "message_ids": message_ids,
                "content_hash": content_hash
            }
        
        return result
    
    async def save_leaderboard_message(self, message_key: str, clan_tag: Optional[str], channel_id: str,
                                      mode: str, message_ids: str, content_hash: str) -> None:
        """
        Save or update a single leaderboard message in database.

        Retries on ``database is locked`` so user-facing commands (/status etc.)
        survive concurrent Phase-3 finalization bursts that hold the WAL writer lock.

        Args:
            message_key: Timestamp-based key (e.g., "2025-09-06T192107.566")
            clan_tag: Clan tag (None for non-clan messages like clan_management)
            channel_id: Discord channel ID
            mode: Mode string (e.g., "attack_08_2025")
            message_ids: Comma-separated Discord message IDs
            content_hash: Content hash for change detection
        """
        await self._retry_on_locked(
            lambda: self._save_leaderboard_message_impl(message_key, clan_tag, channel_id, mode, message_ids, content_hash)
        )

    async def _save_leaderboard_message_impl(self, message_key: str, clan_tag: Optional[str], channel_id: str,
                                             mode: str, message_ids: str, content_hash: str) -> None:
        """Inner implementation of save_leaderboard_message (retry-wrapped)."""
        await self._ensure_connection()
        await self._write_lock.acquire()
        try:
            # NOTE: leaderboard_messages.clan_tag has NO foreign-key constraint on
            # purpose (see fix_leaderboard_fk.py) — it stores clan tags, family
            # tags, AND (for mode == "whois_player") a *player* tag. Calling
            # _ensure_clan_exists() here used to create a bogus placeholder row
            # in the `clans` table for every /whois player report, since it
            # can't tell a player tag from a clan tag. Do NOT re-add this call;
            # nothing reads leaderboard_messages.clan_tag as a guaranteed real
            # clan, so there is nothing to "ensure".
            #
            # 2026-08-17 follow-up: this exact symptom reappeared for over a month after the
            # above fix — not because this call was re-added HERE, but because
            # CacheManager.set_leaderboard_message() (cache_manager.py), the actual public entry
            # point every caller uses to reach this method, had its OWN separate, never-fixed
            # `_ensure_clan_exists()` call that the original fix's investigation never found
            # (different file, different function). See that function's own comment for the
            # full history. The lesson: when a bug is "call X unconditionally does something
            # wrong for input Y", grep for EVERY call site of X across the whole codebase before
            # declaring it fixed, not just the one call site the investigation happened to reach.
            await self._conn.execute("""
                INSERT OR REPLACE INTO leaderboard_messages 
                (message_key, clan_tag, channel_id, mode, message_ids, content_hash, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            """, (message_key, clan_tag, channel_id, mode, message_ids, content_hash))
            await self._conn.commit()
        finally:
            self._write_lock.release()

    async def delete_leaderboard_message(self, message_key: str) -> None:
        """Delete a leaderboard message from database.

        Retries on ``database is locked`` to match the robustness of save_leaderboard_message.
        """
        await self._retry_on_locked(
            lambda: self._delete_leaderboard_message_impl(message_key)
        )

    async def _delete_leaderboard_message_impl(self, message_key: str) -> None:
        """Inner implementation of delete_leaderboard_message (retry-wrapped)."""
        await self._ensure_connection()
        await self._write_lock.acquire()
        try:
            await self._conn.execute("DELETE FROM leaderboard_messages WHERE message_key = ?", (message_key,))
            await self._conn.commit()
        finally:
            self._write_lock.release()
    
    async def save_clan_family(self, family_tag: str, name: str, owned_by_guild: str,
                               member_clans: List[str]) -> None:
        """
        Save or update a clan family with its members.

        Retries on ``database is locked`` so user interactions survive
        concurrent finalization bursts.

        Args:
            family_tag: Unique family identifier
            name: Family display name
            owned_by_guild: Discord guild ID that owns this family
            member_clans: List of clan tags in this family
        """
        await self._retry_on_locked(
            lambda: self._save_clan_family_impl(family_tag, name, owned_by_guild, member_clans)
        )

    async def _save_clan_family_impl(self, family_tag: str, name: str, owned_by_guild: str,
                                     member_clans: List[str]) -> None:
        """Inner implementation of save_clan_family (retry-wrapped)."""
        await self._ensure_connection()
        
        # Ensure all member clans exist BEFORE starting the transaction
        # (FK constraint on clan_family_members.clan_tag -> clans.clan_tag)
        await self._write_lock.acquire()
        try:
            for clan_tag in member_clans:
                await self._ensure_clan_exists(clan_tag)
            
            await self._conn.execute("BEGIN")
            
            # Save family record
            await self._conn.execute("""
                INSERT INTO clan_families 
                (family_tag, name, owned_by_guild, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(family_tag) DO UPDATE SET
                    name = excluded.name,
                    owned_by_guild = excluded.owned_by_guild,
                    updated_at = excluded.updated_at
            """, (family_tag, name, owned_by_guild))
            
            # Delete existing members
            await self._conn.execute("DELETE FROM clan_family_members WHERE family_tag = ?", (family_tag,))
            
            # Insert new members
            for clan_tag in member_clans:
                await self._conn.execute("""
                    INSERT INTO clan_family_members (family_tag, clan_tag)
                    VALUES (?, ?)
                """, (family_tag, clan_tag))
            
            await self._conn.commit()
        except Exception as e:
            await self._conn.rollback()
            logging.error(f"[DB-WRITE] Transaction failed for save_clan_family({family_tag}): {e}")
            raise
        finally:
            self._write_lock.release()
    
    async def delete_clan_family(self, family_tag: str) -> None:
        """Delete a clan family and its member associations."""
        await self._ensure_connection()
        
        # Members will cascade delete due to foreign key
        await self._write_lock.acquire()
        try:
            await self._conn.execute("DELETE FROM clan_families WHERE family_tag = ?", (family_tag,))
            await self._conn.commit()
        finally:
            self._write_lock.release()
    
    # ─── Notification State Methods ────────────────────────────────────────
    
    async def save_player_notification(self, war_key: str, player_tag: str, player_name: str,
                                       discord_id: str, notification_time: str,
                                       attacks_remaining: int) -> None:
        """Save a player notification record (write-through)."""
        await self._ensure_connection()
        
        await self._write_lock.acquire()
        try:
            await self._conn.execute("""
                INSERT OR REPLACE INTO notification_state
                    (war_key, player_tag, player_name, discord_id, notification_time, attacks_remaining)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (war_key, player_tag, player_name, discord_id, notification_time, attacks_remaining))
            await self._conn.commit()
        finally:
            self._write_lock.release()

    async def save_channel_notification(self, war_key: str, guild_id: str, notification_time: str,
                                        clan_name: str, opponent_name: str) -> None:
        """Save a channel notification record (write-through)."""
        await self._ensure_connection()
        
        await self._write_lock.acquire()
        try:
            await self._conn.execute("""
                INSERT OR REPLACE INTO channel_notification_state
                    (war_key, guild_id, notification_time, clan_name, opponent_name)
                VALUES (?, ?, ?, ?, ?)
            """, (war_key, guild_id, notification_time, clan_name, opponent_name))
            await self._conn.commit()
        finally:
            self._write_lock.release()
    
    async def load_notification_state(self) -> Dict[str, Any]:
        """Load all notification state from DB into the JSON-compatible dict format."""
        await self._ensure_connection()
        
        result: Dict[str, Any] = {}
        
        # Load player notifications
        async with self._conn.execute("SELECT war_key, player_tag, player_name, discord_id, notification_time, attacks_remaining FROM notification_state") as cursor:
            async for row in cursor:
                war_key, player_tag, player_name, discord_id, notif_time, attacks = row
                if war_key not in result:
                    result[war_key] = {"notified_players": {}, "channel_notifications": {}}
                result[war_key]["notified_players"][player_tag] = {
                    "player_name": player_name,
                    "discord_id": discord_id,
                    "notification_time": notif_time,
                    "attacks_remaining": attacks,
                }
        
        # Load channel notifications
        async with self._conn.execute("SELECT war_key, guild_id, notification_time, clan_name, opponent_name FROM channel_notification_state") as cursor:
            async for row in cursor:
                war_key, guild_id, notif_time, clan_name, opponent_name = row
                if war_key not in result:
                    result[war_key] = {"notified_players": {}, "channel_notifications": {}}
                if "channel_notifications" not in result[war_key]:
                    result[war_key]["channel_notifications"] = {}
                result[war_key]["channel_notifications"][guild_id] = {
                    "notification_time": notif_time,
                    "clan_name": clan_name,
                    "opponent_name": opponent_name,
                }
        
        return result
    
    async def delete_notification_state_for_war(self, war_key: str) -> None:
        """Delete all notification state for a given war (atomic cleanup)."""
        await self._ensure_connection()
        
        await self._write_lock.acquire()
        try:
            await self._conn.execute("BEGIN")
            await self._conn.execute("DELETE FROM notification_state WHERE war_key = ?", (war_key,))
            await self._conn.execute("DELETE FROM channel_notification_state WHERE war_key = ?", (war_key,))
            await self._conn.commit()
        except Exception as e:
            await self._conn.rollback()
            logging.error(f"[DB-WRITE] Transaction failed for delete_notification_state_for_war({war_key}): {e}")
            raise
        finally:
            self._write_lock.release()

    def delete_notification_state_sync(self, war_key: str) -> None:
        """
        Synchronous version: delete all notification state for a given war.

        Used from synchronous war-archive paths (e.g. QBhelperfunctions) that
        cannot await the async counterpart.
        """
        if not self.db_path:
            return
        try:
            with self._sync_conn() as conn:
                with self._sync_write_lock:
                    conn.execute("DELETE FROM notification_state WHERE war_key = ?", (war_key,))
                    conn.execute("DELETE FROM channel_notification_state WHERE war_key = ?", (war_key,))
                    conn.commit()
        except Exception as e:
            logging.warning(f"[DB-WRITE-SYNC] delete_notification_state_sync({war_key}) failed: {e}")

    # ─── Hot/History DB Monthly Migration ──────────────────────────────────
    @staticmethod
    def _history_cutoff() -> Tuple[str, str]:
        """Compute the hot/history migration cutoff for "today".

        Retention model: the hot DB (``main``) always holds the current
        calendar month + the immediately preceding calendar month in full.
        Everything strictly older is migrated to ``history`` once a month.

        Returns:
            (cutoff_date, cutoff_month) — ``cutoff_date`` is the first day of
            the previous calendar month as ``YYYY-MM-DD`` (rows with
            ``date < cutoff_date`` are migrated); ``cutoff_month`` is the same
            value as ``YYYY-MM`` (used for CWL-season comparisons, which have
            no ``date`` column).
        """
        import datetime as _dt
        first_of_this_month = _dt.date.today().replace(day=1)
        if first_of_this_month.month == 1:
            cutoff = first_of_this_month.replace(year=first_of_this_month.year - 1, month=12)
        else:
            cutoff = first_of_this_month.replace(month=first_of_this_month.month - 1)
        return cutoff.isoformat(), cutoff.strftime("%Y-%m")

    # Batches between periodic mid-migration WAL checkpoints (see
    # _migrate_table_batch_by_date). 20 batches * 5000 rows/batch = 100K rows
    # of slack between checkpoints — bounds WAL growth to a small, safe amount
    # even across a multi-hour migration, without checkpointing so often that
    # it reintroduces per-batch random-I/O stalls.
    _MIGRATION_CHECKPOINT_INTERVAL_BATCHES = 20

    async def _explicit_column_list(self, table: str) -> str:
        """Comma-joined column names for `table`, read from `main`'s own schema via
        `PRAGMA table_info` — used to build `INSERT INTO history.x (cols) SELECT cols FROM
        main.x` migrations with EXPLICIT column names on both sides, never bare `SELECT *`.

        This exists because of a real incident (found 2026-08-14): `main.war_attacks` and
        `history.war_attacks` — likewise `war_summary` — have identical column *names* but
        their physical on-disk column *order* had silently diverged over time, because each
        schema's columns were added via separate `ALTER TABLE ADD COLUMN` sequences at
        different points in this project's history. `CREATE TABLE IF NOT EXISTS` does NOT
        retroactively reorder an already-existing table to match newer code, so the two
        schemas' CREATE TABLE text looking identical today proves nothing about a live,
        long-running DB file's actual column order. A bare `SELECT *` migration matches
        columns by POSITION, not by name — so every migrated row silently landed in the wrong
        columns from cid 7 (war_attacks) / cid 14 (war_summary) onward, corrupting roughly a
        dozen columns (including `stars`, `date`, and `war_tag`) for every row that ever aged
        into history. See the cardinal rule in `.github/copilot-instructions.md` ("hot/history
        schemas must never drift") for the prevention half of this fix — this function is the
        detection-proof half: explicit columns either migrate correctly by name, or fail
        loudly (`no such column`) the moment a schema really does diverge, instead of silently
        scrambling data again.
        """
        cur = await self._conn.execute(f"PRAGMA main.table_info({table})")
        rows = await cur.fetchall()
        return ", ".join(row["name"] for row in rows)

    async def _migrate_table_batch_by_date(
        self,
        table: str,
        cutoff_date: str,
        batch_size: int,
        checkpoint_every_batches: Optional[int] = None,
        deadline: Optional[float] = None,
    ) -> Tuple[int, bool]:
        """Move rows with ``date < cutoff_date`` from ``main.<table>`` to ``history.<table>``.

        Batched in chunks of ``batch_size`` rows (by ``id``) so no single
        transaction holds the write lock for long — this is the same lesson
        learned from the 44-minute nightly VACUUM incident that started this
        redesign: never do one giant transaction over the whole table.

        Assumes the table has an ``id INTEGER PRIMARY KEY AUTOINCREMENT``
        column (true for ``war_attacks`` and ``war_summary``) and a ``date``
        column holding an ISO-ish, lexicographically-sortable string.

        Every ``checkpoint_every_batches`` batches (default
        ``_MIGRATION_CHECKPOINT_INTERVAL_BATCHES``), runs a ``PASSIVE`` WAL
        checkpoint (unqualified — covers both ``main`` and ``history``, same
        as ``nightly_db_maintenance()``). Without this, the caller's
        ``wal_autocheckpoint=0`` (set for the whole migration) means the WAL
        never shrinks for the entire run — on 2026-08-01 a first-ever
        migration of 8M+ rows ran for ~4h45m uncheckpointed and grew both WAL
        files to fill the disk (287 GB + 103 GB), aborting the migration and
        then failing every other DB write for the rest of the night. A
        periodic PASSIVE checkpoint (non-blocking, safe to run
        mid-transaction-series) keeps the WAL bounded regardless of total
        migration size.

        If ``deadline`` (a ``time.monotonic()`` timestamp) is given, stops
        cleanly after whichever batch is in flight when the deadline passes —
        added after that same incident's recovery turned out to need ~10+
        hours for the full backlog, which nobody can block a live bot on.
        Safe to stop here: each batch is its own committed transaction, so
        there's nothing to roll back or resume from except "keep calling this
        again with the same cutoff_date".

        Returns:
            ``(total_moved, completed)`` — ``completed`` is False iff the
            deadline was hit before every matching row was moved (i.e. more
            rows still exist below ``cutoff_date`` and the caller should not
            treat this table as done).
        """
        interval = checkpoint_every_batches or self._MIGRATION_CHECKPOINT_INTERVAL_BATCHES
        total_moved = 0
        batches_since_checkpoint = 0
        while True:
            if deadline is not None and _monotonic() >= deadline:
                logging.info(f"[HIST-MIGRATE] {table}: time budget reached — stopping early (total {total_moved} this run)")
                return total_moved, False
            cur = await self._conn.execute(
                f"SELECT id FROM main.{table} WHERE date < ? ORDER BY id LIMIT ?",
                (cutoff_date, batch_size),
            )
            rows = await cur.fetchall()
            if not rows:
                break
            ids = [r["id"] for r in rows]
            placeholders = ",".join("?" * len(ids))
            columns = await self._explicit_column_list(table)
            await self._write_lock.acquire()
            try:
                await self._conn.execute("BEGIN")
                await self._conn.execute(
                    f"INSERT OR IGNORE INTO history.{table} ({columns}) "
                    f"SELECT {columns} FROM main.{table} WHERE id IN ({placeholders})",
                    ids,
                )
                await self._conn.execute(
                    f"DELETE FROM main.{table} WHERE id IN ({placeholders})",
                    ids,
                )
                await self._conn.commit()
            except Exception:
                await self._conn.rollback()
                raise
            finally:
                self._write_lock.release()
            total_moved += len(ids)
            logging.info(f"[HIST-MIGRATE] {table}: moved batch of {len(ids)} rows (total {total_moved})")
            batches_since_checkpoint += 1
            if batches_since_checkpoint >= interval:
                batches_since_checkpoint = 0
                try:
                    await self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except Exception as _ckpt_ex:
                    logging.warning(f"[HIST-MIGRATE] Periodic mid-migration checkpoint failed: {_ckpt_ex}")
        return total_moved, True

    async def _migrate_cwl_table_by_season(
        self, table: str, cutoff_month: str, deadline: Optional[float] = None
    ) -> Tuple[int, bool]:
        """Move rows from ``main.<table>`` to ``history.<table>`` for CWL seasons older than ``cutoff_month``.

        ``cwl_league_groups`` and ``cwl_league_rounds`` have no ``date``
        column — only ``cwl_season``, which the CoC API returns in several
        shapes (see ``normalize_cwl_season()``). Each row's season is
        normalised and truncated to its ``YYYY-MM`` prefix before comparing
        against ``cutoff_month`` — comparing the raw (possibly
        ``YYYY-MM-DD``) string against a plain ``YYYY-MM`` cutoff would be
        wrong (different string lengths sort inconsistently).

        Batched per distinct season value (each season naturally has few
        rows — one per clan/war — so no additional row-count chunking needed).

        See ``_migrate_table_batch_by_date`` for the ``deadline`` contract —
        checked between seasons (each season's own transaction always runs to
        completion once started).

        Returns:
            ``(total_moved, completed)``.
        """
        from qapbot.constants import normalize_cwl_season

        cur = await self._conn.execute(f"SELECT DISTINCT cwl_season FROM main.{table}")
        seasons = [r["cwl_season"] for r in await cur.fetchall()]
        old_seasons = [s for s in seasons if normalize_cwl_season(s)[:7] < cutoff_month]

        total_moved = 0
        completed = True
        columns = await self._explicit_column_list(table)
        for season in old_seasons:
            if deadline is not None and _monotonic() >= deadline:
                logging.info(f"[HIST-MIGRATE] {table}: time budget reached — stopping early (total {total_moved} this run)")
                completed = False
                break
            await self._write_lock.acquire()
            try:
                await self._conn.execute("BEGIN")
                await self._conn.execute(
                    f"INSERT OR IGNORE INTO history.{table} ({columns}) "
                    f"SELECT {columns} FROM main.{table} WHERE cwl_season = ?",
                    (season,),
                )
                del_cur = await self._conn.execute(
                    f"DELETE FROM main.{table} WHERE cwl_season = ?", (season,)
                )
                await self._conn.commit()
                total_moved += del_cur.rowcount if del_cur.rowcount and del_cur.rowcount > 0 else 0
            except Exception:
                await self._conn.rollback()
                raise
            finally:
                self._write_lock.release()
        if old_seasons:
            logging.info(f"[HIST-MIGRATE] {table}: moved {total_moved} row(s) across {len(old_seasons)} season(s)")
        return total_moved, completed

    async def monthly_history_migration(
        self,
        batch_size: int = 5000,
        checkpoint_every_batches: Optional[int] = None,
        time_budget_seconds: Optional[float] = None,
    ) -> str:
        """
        Migrate data older than the hot-DB retention window from ``main.*`` to
        ``history.*`` (ATTACHed database). Runs once a month (on/after the
        1st, via QapBot.py's nightly maintenance scheduler) until it reports a
        fully-completed run for the current month — NOT nightly-forever, since
        the retention window only advances monthly, but see ``time_budget_seconds``
        below for why "once a month" doesn't mean "in one sitting".

        Retention model: ``main`` always holds the current calendar month +
        the immediately preceding calendar month in full; this migrates
        everything older out to ``history``.

        Tables migrated:
          - ``war_attacks`` / ``war_summary`` — keyed on the ``date`` column.
          - ``cwl_league_groups`` / ``cwl_league_rounds`` — no ``date`` column;
            keyed on ``cwl_season`` (normalised to ``YYYY-MM``).

        Batched writes (see ``_migrate_table_batch_by_date``) avoid long
        single-transaction stalls. Discord commands are blocked via
        ``QBcore.db_maintenance_mode`` for the duration, matching
        ``nightly_db_maintenance()``'s behaviour.

        Does NOT run VACUUM — the pre-existing nightly VACUUM trigger
        (``freelist_count > 500``) picks up the freed space left in ``main``
        by the DELETEs on its next run. History-DB compaction is handled
        separately (see architecture doc) since ``history`` only receives
        bulk writes once a month.

        Args:
            batch_size: rows per batch/transaction (see ``_migrate_table_batch_by_date``).
            checkpoint_every_batches: override for
                ``_MIGRATION_CHECKPOINT_INTERVAL_BATCHES`` — mainly useful for
                a one-off recovery run where a much bigger interval is safe
                (ample free disk) and reduces checkpoint overhead.
            time_budget_seconds: if set, stops cleanly (not an error) once this
                many seconds have elapsed, leaving whatever wasn't reached for
                the next call — added 2026-08-01 after a first-ever run against
                an 8M+-row backlog needed ~10+ hours, far too long to block a
                live bot on in one sitting. The migration is naturally
                resumable (each call re-selects whatever's still below the
                cutoff), so a time-budgeted call is just "do as much as you
                can in this window, pick back up next time" — the ``bot_metadata``
                "done" marker is only written when a run finishes the full
                backlog with no deadline cutoff (see the ``finally`` block).

        Returns:
            A one-line summary string suitable for logging. Starts with
            "[HIST-MIGRATE] PARTIAL" (not an error) when the time budget was
            hit before finishing.
        """
        if not self.db_path:
            return "[HIST-MIGRATE] Skipped — no db_path configured"

        cutoff_date, cutoff_month = self._history_cutoff()
        logging.info(
            f"[HIST-MIGRATE] Starting monthly migration — cutoff_date={cutoff_date} "
            f"(hot DB retains {cutoff_month} and later)"
        )

        _qbcore = None
        try:
            import QBcore as _qbcore  # type: ignore[no-redef]
            _qbcore.db_maintenance_mode = True
            logging.info("[HIST-MIGRATE] db_maintenance_mode=True — Discord commands blocked")
        except ImportError:
            pass

        t_start = _monotonic()
        deadline = (t_start + time_budget_seconds) if time_budget_seconds else None
        moved: Dict[str, int] = {}
        fully_completed = False
        result = "[HIST-MIGRATE] ERROR: unknown"
        try:
            await self._ensure_connection()

            # Suppress WAL auto-checkpoint on BOTH schemas for the whole migration —
            # this is a long bulk-write burst (thousands of small batch commits),
            # and a mid-burst checkpoint would add random-I/O stalls (same reason
            # _flush_pending_war_writes() does this). Restored + a single passive
            # checkpoint runs in the finally block below, win or lose.
            await self._conn.execute("PRAGMA wal_autocheckpoint=0")
            await self._conn.execute("PRAGMA history.wal_autocheckpoint=0")

            moved["war_attacks"], _wa_done = await self._migrate_table_batch_by_date(
                "war_attacks", cutoff_date, batch_size, checkpoint_every_batches, deadline
            )
            moved["war_summary"], _ws_done = (0, True)
            _cg_done = _cr_done = True
            if _wa_done:
                moved["war_summary"], _ws_done = await self._migrate_table_batch_by_date(
                    "war_summary", cutoff_date, batch_size, checkpoint_every_batches, deadline
                )
            if _wa_done and _ws_done:
                moved["cwl_league_groups"], _cg_done = await self._migrate_cwl_table_by_season(
                    "cwl_league_groups", cutoff_month, deadline
                )
            if _wa_done and _ws_done and _cg_done:
                moved["cwl_league_rounds"], _cr_done = await self._migrate_cwl_table_by_season(
                    "cwl_league_rounds", cutoff_month, deadline
                )
            fully_completed = _wa_done and _ws_done and _cg_done and _cr_done

            elapsed = _monotonic() - t_start
            summary = ", ".join(f"{k}={v}" for k, v in moved.items())
            if fully_completed:
                result = f"[HIST-MIGRATE] cutoff={cutoff_date} — {summary} rows moved — done in {elapsed:.1f}s"
            else:
                result = (
                    f"[HIST-MIGRATE] PARTIAL — time budget reached, cutoff={cutoff_date} — "
                    f"{summary} rows moved this run in {elapsed:.1f}s — more remain, re-run to continue"
                )
            logging.info(result)
        except Exception as e:
            result = f"[HIST-MIGRATE] ERROR: {e}"
            logging.error(result)
        finally:
            try:
                # ALWAYS restore autocheckpoint + merge the WAL back into both DB
                # files — if this were skipped after an exception, wal_autocheckpoint
                # would stay 0 for the rest of the process lifetime, causing
                # unbounded WAL growth on subsequent unrelated writes.
                await self._conn.execute("PRAGMA wal_autocheckpoint=1000")
                await self._conn.execute("PRAGMA history.wal_autocheckpoint=1000")
                await self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                await self._conn.execute("PRAGMA history.wal_checkpoint(PASSIVE)")
            except Exception as _ckpt_ex:
                logging.warning(f"[HIST-MIGRATE] Could not restore autocheckpoint/checkpoint: {_ckpt_ex}")
            if _qbcore is not None:
                _qbcore.db_maintenance_mode = False
                logging.info("[HIST-MIGRATE] db_maintenance_mode=False — Discord commands unblocked")

        # Only stamp "done for this month" on an actual FULL completion — gated on the
        # real fully_completed boolean, not on string-sniffing `result` (a PARTIAL
        # time-budget stop is deliberately not an "ERROR" string, so a prefix check
        # alone would wrongly mark it done — this must be the boolean).
        # Persisting this unconditionally (the pre-2026-08-01-incident behaviour)
        # marked a run that errored out partway (e.g. mid-migration disk-full) as
        # fully complete — is_monthly_migration_due() then skipped retrying for the
        # rest of the month, silently stranding whatever hadn't been moved yet with
        # no automatic retry. The migration itself is naturally resumable (each batch
        # re-selects remaining rows below the cutoff), so simply not claiming success
        # lets the next due check (or a manual re-run) pick up exactly where it left off.
        if fully_completed:
            try:
                import datetime as _dt2
                await self.set_bot_metadata(
                    "last_history_migration",
                    _dt2.datetime.now(_dt2.timezone.utc).isoformat(),
                )
            except Exception as _meta_ex:
                logging.warning(f"[HIST-MIGRATE] Could not persist migration timestamp: {_meta_ex}")

        return result

    # war_attacks' secondary indexes — identical set on both `main` and `history`
    # schemas (see _create_schema()/_create_history_schema()). Used ONLY by
    # fast_bulk_history_migration() below to temporarily drop + rebuild them; the
    # UNIQUE(war_id, player_tag, attack_order) constraint is deliberately never
    # included here — INSERT OR IGNORE's idempotency depends on it, so it must
    # never be dropped. MUST be kept in sync with the CREATE INDEX statements in
    # _create_schema()/_create_history_schema() (and their _sync counterparts) —
    # duplicated here rather than shared because those methods build DDL strings
    # inline rather than from a shared constant; a schema change there needs the
    # matching update made here too.
    _WAR_ATTACKS_SECONDARY_INDEX_DDL: List[Tuple[str, str]] = [
        # (index_name, CREATE INDEX statement — unqualified, targets whichever
        # schema the caller prefixes onto the statement/name at execution time)
        ("idx_wa_player_tag", "CREATE INDEX IF NOT EXISTS {schema}idx_wa_player_tag ON war_attacks(player_tag)"),
        ("idx_wa_war_clan", "CREATE INDEX IF NOT EXISTS {schema}idx_wa_war_clan ON war_attacks(war_id, clan_tag)"),
        ("idx_wa_clan_date", "CREATE INDEX IF NOT EXISTS {schema}idx_wa_clan_date ON war_attacks(clan_tag, date)"),
        ("idx_wa_zero_attacks", "CREATE INDEX IF NOT EXISTS {schema}idx_wa_zero_attacks ON war_attacks(attack_order) WHERE attack_order = 0"),
        ("idx_wa_player_tag_date", "CREATE INDEX IF NOT EXISTS {schema}idx_wa_player_tag_date ON war_attacks(player_tag, date)"),
    ]

    async def _drop_war_attacks_secondary_indexes(self) -> None:
        for schema in ("", "history."):
            for name, _ddl in self._WAR_ATTACKS_SECONDARY_INDEX_DDL:
                await self._conn.execute(f"DROP INDEX IF EXISTS {schema}{name}")

    async def _rebuild_war_attacks_secondary_indexes(self) -> None:
        for schema in ("", "history."):
            for name, ddl in self._WAR_ATTACKS_SECONDARY_INDEX_DDL:
                try:
                    await self._conn.execute(ddl.format(schema=schema))
                except Exception as _idx_ex:
                    logging.error(f"[HIST-MIGRATE-FAST] Failed to rebuild index {schema}{name}: {_idx_ex}")

    async def _bulk_move_chunk(self, table: str, cutoff_date: str, chunk_size: int) -> Tuple[int, bool]:
        """Move up to ``chunk_size`` rows matching ``date < cutoff_date`` from
        ``main.<table>`` to ``history.<table>`` in ONE transaction — no per-row
        secondary-index maintenance (caller must have already dropped them).

        Finds the chunk's upper ``id`` bound via ``OFFSET`` (keyset pagination)
        rather than collecting a chunk_size-long list of ids to bind as an
        ``IN (...)`` clause — a 1M-entry parameter list would exceed SQLite's
        default bound-parameter limit (~32766) — and rather than relying on
        ``LIMIT``/``ORDER BY`` on ``DELETE``, which is a non-default SQLite
        compile-time option not guaranteed available.

        Returns:
            ``(moved_count, is_last_chunk)`` — ``is_last_chunk`` is True when
            fewer than ``chunk_size`` matching rows remained (i.e. this chunk
            cleared the entire remaining backlog for this table).
        """
        cur = await self._conn.execute(
            f"SELECT id FROM main.{table} WHERE date < ? ORDER BY id LIMIT 1 OFFSET ?",
            (cutoff_date, chunk_size - 1),
        )
        boundary_row = await cur.fetchone()
        boundary_id = boundary_row["id"] if boundary_row else None

        # Explicit column list, not `SELECT *` — see _explicit_column_list()'s docstring
        # (2026-08-14 hot/history column-order-drift incident). This call site predates that
        # fix and wasn't covered by it (only _migrate_table_batch_by_date/
        # _migrate_cwl_table_by_season were fixed at the time) — found 2026-08-16 while
        # auditing every main/history query for the same class of bug.
        columns = await self._explicit_column_list(table)

        await self._write_lock.acquire()
        try:
            await self._conn.execute("BEGIN")
            if boundary_id is not None:
                await self._conn.execute(
                    f"INSERT OR IGNORE INTO history.{table} ({columns}) "
                    f"SELECT {columns} FROM main.{table} WHERE date < ? AND id <= ?",
                    (cutoff_date, boundary_id),
                )
                del_cur = await self._conn.execute(
                    f"DELETE FROM main.{table} WHERE date < ? AND id <= ?",
                    (cutoff_date, boundary_id),
                )
            else:
                await self._conn.execute(
                    f"INSERT OR IGNORE INTO history.{table} ({columns}) "
                    f"SELECT {columns} FROM main.{table} WHERE date < ?",
                    (cutoff_date,),
                )
                del_cur = await self._conn.execute(
                    f"DELETE FROM main.{table} WHERE date < ?",
                    (cutoff_date,),
                )
            moved = del_cur.rowcount if del_cur.rowcount and del_cur.rowcount > 0 else 0
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise
        finally:
            self._write_lock.release()
        return moved, boundary_id is None

    async def fast_bulk_history_migration(self, chunk_size: int = 1_000_000) -> str:
        """
        One-off FAST alternative to ``monthly_history_migration()`` for clearing a
        large backlog quickly (added 2026-08-01, same incident as everything else
        this method's siblings document). NOT for routine/automatic use — the only
        intended caller is ``qapbot/scripts/run_history_migration_now.py --fast``,
        run with the bot STOPPED.

        Why the normal batched path is slow: with all secondary indexes present,
        every row moved costs 6 B-tree updates on the ``history`` side (5
        secondary indexes + the UNIQUE constraint) and 6 more removing it from
        ``main`` — that per-row index-maintenance work, not disk I/O, is what
        limited a live 2026-08-01 run to ~900-1000 rows/sec even on fast SSD
        despite the underlying data being only ~10 GB.

        What this does instead: temporarily drops the 5 secondary indexes on
        BOTH ``main.war_attacks`` and ``history.war_attacks`` (the UNIQUE
        constraint is never touched — ``INSERT OR IGNORE``'s idempotency depends
        on it), migrates ``war_attacks`` in large chunks (default 1M rows, one
        commit each — see ``_bulk_move_chunk``) instead of 5000-row batches, then
        rebuilds every dropped index in one efficient sorted pass each via
        ``CREATE INDEX`` — far cheaper than millions of incremental updates.
        ``war_summary``/CWL tables are migrated via the existing (already fast
        enough at their much smaller volume) methods — no index-dropping there.

        Safety:
          - REQUIRES the bot to be stopped — dropping ``main.war_attacks``'
            indexes, even briefly, would badly degrade any concurrent live query
            against it. The 5000-row batched ``monthly_history_migration()``
            path never touches ``main``'s indexes for exactly this reason and
            remains the right choice for anything running alongside a live bot.
          - Indexes are ALWAYS rebuilt in the ``finally`` block, even if the
            migration itself errors out partway — a subsequent bot start should
            never see ``main.war_attacks`` missing its query indexes.
          - Each chunk is still its own committed transaction (same
            resumability guarantee as the batched path — safe to interrupt and
            re-run; already-migrated rows simply won't match `date < cutoff`
            again). No ``time_budget_seconds`` here: this is meant to run once,
            to completion; chunk size (not a time budget) is the safety valve
            bounding how much uncheckpointed WAL a single transaction can grow to.
          - Sets ``PRAGMA temp_store_directory`` to the DB's own directory before
            touching any index (added 2026-08-01, found live during recovery):
            ``CREATE INDEX`` on a table this large needs real external-sort temp
            space, and SQLite's default temp location (typically ``/tmp``) can be
            a small tmpfs on a NAS — unrelated to how much free space the data
            volume itself has. A manual CLI rebuild hit exactly this
            (``SQLITE_FULL`` on a 4.8 GB tmpfs `/tmp`) while recovering from this
            same method having silently died earlier for what's now suspected to
            be the identical reason. Same fix ``nightly_db_maintenance()``
            already applies for VACUUM.

        Returns:
            A one-line summary string suitable for logging.
        """
        if not self.db_path:
            return "[HIST-MIGRATE-FAST] Skipped — no db_path configured"

        cutoff_date, cutoff_month = self._history_cutoff()
        logging.info(
            f"[HIST-MIGRATE-FAST] Starting fast bulk migration — cutoff_date={cutoff_date} "
            f"(hot DB retains {cutoff_month} and later)"
        )

        _qbcore = None
        try:
            import QBcore as _qbcore  # type: ignore[no-redef]
            _qbcore.db_maintenance_mode = True
            logging.info("[HIST-MIGRATE-FAST] db_maintenance_mode=True — Discord commands blocked")
        except ImportError:
            pass

        await self._ensure_connection()

        # CREATE INDEX on a table this large needs real temp/sort space — SQLite's
        # default temp location (typically /tmp) can be a small tmpfs on a NAS,
        # unrelated to how much free space the data volume itself has. Same fix
        # nightly_db_maintenance() already applies for VACUUM (see that method's
        # comments for the matching historical incident — a mystery SIGKILL with
        # no log entry, there from an OOM triggered by a large cache_size, here
        # from SQLITE_FULL on a full temp tmpfs during this run's own index
        # rebuild). Point temp storage at the DB's own directory instead — same
        # volume, effectively unlimited headroom by comparison.
        await self._conn.execute("PRAGMA temp_store=FILE")
        _db_dir = os.path.dirname(str(self.db_path))
        await self._conn.execute(f"PRAGMA temp_store_directory='{_db_dir}'")

        logging.info("[HIST-MIGRATE-FAST] Dropping secondary indexes on main + history war_attacks...")
        await self._drop_war_attacks_secondary_indexes()

        t_start = _monotonic()
        moved: Dict[str, int] = {}
        result = "[HIST-MIGRATE-FAST] ERROR: unknown"
        try:
            await self._conn.execute("PRAGMA wal_autocheckpoint=0")
            await self._conn.execute("PRAGMA history.wal_autocheckpoint=0")

            wa_total = 0
            while True:
                chunk_moved, is_last = await self._bulk_move_chunk("war_attacks", cutoff_date, chunk_size)
                wa_total += chunk_moved
                logging.info(f"[HIST-MIGRATE-FAST] war_attacks: moved chunk of {chunk_moved} rows (total {wa_total})")
                try:
                    await self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except Exception as _ckpt_ex:
                    logging.warning(f"[HIST-MIGRATE-FAST] Periodic checkpoint failed: {_ckpt_ex}")
                if is_last:
                    break
            moved["war_attacks"] = wa_total

            # war_summary/CWL tables: far smaller volume, the existing batched
            # methods are plenty fast already — no index-dropping needed.
            moved["war_summary"], _ = await self._migrate_table_batch_by_date("war_summary", cutoff_date, 5000)
            moved["cwl_league_groups"], _ = await self._migrate_cwl_table_by_season("cwl_league_groups", cutoff_month)
            moved["cwl_league_rounds"], _ = await self._migrate_cwl_table_by_season("cwl_league_rounds", cutoff_month)

            elapsed = _monotonic() - t_start
            summary = ", ".join(f"{k}={v}" for k, v in moved.items())
            result = f"[HIST-MIGRATE-FAST] cutoff={cutoff_date} — {summary} rows moved — done in {elapsed:.1f}s"
            logging.info(result)
        except Exception as e:
            result = f"[HIST-MIGRATE-FAST] ERROR: {e}"
            logging.error(result)
        finally:
            logging.info("[HIST-MIGRATE-FAST] Rebuilding dropped indexes...")
            await self._rebuild_war_attacks_secondary_indexes()
            logging.info("[HIST-MIGRATE-FAST] Index rebuild complete.")
            try:
                await self._conn.execute("PRAGMA wal_autocheckpoint=1000")
                await self._conn.execute("PRAGMA history.wal_autocheckpoint=1000")
                await self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                await self._conn.execute("PRAGMA history.wal_checkpoint(PASSIVE)")
            except Exception as _ckpt_ex:
                logging.warning(f"[HIST-MIGRATE-FAST] Could not restore autocheckpoint/checkpoint: {_ckpt_ex}")
            if _qbcore is not None:
                _qbcore.db_maintenance_mode = False
                logging.info("[HIST-MIGRATE-FAST] db_maintenance_mode=False — Discord commands unblocked")

        if not result.startswith("[HIST-MIGRATE-FAST] ERROR"):
            try:
                import datetime as _dt2
                await self.set_bot_metadata(
                    "last_history_migration",
                    _dt2.datetime.now(_dt2.timezone.utc).isoformat(),
                )
            except Exception as _meta_ex:
                logging.warning(f"[HIST-MIGRATE-FAST] Could not persist migration timestamp: {_meta_ex}")

        return result

    async def nightly_db_maintenance(self) -> str:
        """
        Run nightly DB maintenance in three steps:
          1. WAL checkpoint (TRUNCATE) — unqualified, so it also checkpoints the attached
             'history' schema (SQLite checkpoints every attached WAL-mode DB when no schema
             name is given).
          2. VACUUM (if freelist > 500 or page_size migration pending) or REINDEX (otherwise)
             — mutually exclusive: VACUUM rebuilds all indexes, so REINDEX is skipped when VACUUM runs.
             Main-only: 'history' only ever receives bulk INSERTs (never DELETEs) in normal
             operation, so it doesn't accumulate the freelist bloat VACUUM exists to reclaim.
          3. ANALYZE + PRAGMA optimize (always last, so statistics reflect the final B-tree
             layout) — also unqualified, so it refreshes query-planner statistics for
             'history' tables too (critical for the hot+history UNION queries added by the
             DB split — those tables get zero statistics otherwise).

        Discord commands are blocked via QBcore.db_maintenance_mode during the run so users
        get a friendly "DB optimization in progress" message instead of silent failures.

        Maintenance runs on a dedicated sqlite3 connection with aggressive pragmas
        (cache_size 2 GB, synchronous=OFF, threads=2, temp_store_directory→DB dir).
        The bot's persistent async/sync connections stay open — closing them from the
        async thread leaves zombie-held locks (unfinalized prepared statements in worker
        threads prevent sqlite3_close_v2 from releasing locks immediately), which blocks
        locking_mode=EXCLUSIVE and hangs the maintenance connection for 300s.

        Returns:
            A one-line summary string suitable for logging.
        """
        import time as _time
        if not self.db_path:
            return "[DB-MAINT] Skipped — no db_path configured"

        logging.info("[DB-MAINT] Starting maintenance (1: WAL checkpoint → 2: VACUUM or REINDEX → 3: ANALYZE)...")

        # Block Discord commands during maintenance
        _qbcore = None
        try:
            import QBcore as _qbcore  # type: ignore[no-redef]
            _qbcore.db_maintenance_mode = True
            logging.info("[DB-MAINT] db_maintenance_mode=True — Discord commands blocked")
        except ImportError:
            pass

        def _run() -> str:
            import sqlite3 as _sq
            assert self.db_path  # guarded by early-return above
            t_start = _time.monotonic()

            # Drain the connection pool: waits for all in-flight workers to
            # release their connections, then closes them.  Inside drain() we
            # are the sole owner of the database.
            if self._pool is None:
                raise RuntimeError("[DB-MAINT] Connection pool not initialized")

            with self._pool.drain(timeout=120):
                conn = _sq.connect(self.db_path, timeout=300, isolation_level=None)  # autocommit
                conn.row_factory = _sq.Row
                # ATTACH history so the unqualified WAL-checkpoint (step 1) and ANALYZE
                # (step 3) below transparently cover BOTH schemas — SQLite runs those two
                # commands against every attached WAL-mode database when no schema name is
                # given. VACUUM/REINDEX (step 2) intentionally stay main-only: 'history'
                # only ever receives bulk INSERTs (never DELETEs) in normal operation, so
                # it doesn't accumulate the freelist bloat VACUUM exists to reclaim.
                if self.history_db_path:
                    conn.execute("ATTACH DATABASE ? AS history", (self.history_db_path,))
                    conn.execute("PRAGMA history.journal_mode=WAL")
                    conn.execute("PRAGMA history.synchronous=NORMAL")
                try:
                    # Try EXCLUSIVE locking now that pool is drained
                    try:
                        conn.execute("PRAGMA locking_mode=EXCLUSIVE")
                        conn.execute("BEGIN EXCLUSIVE")
                        conn.execute("COMMIT")
                        logging.info("[DB-MAINT] Acquired EXCLUSIVE lock")
                    except _sq.OperationalError as _exc_lock:
                        logging.warning(f"[DB-MAINT] Could not acquire EXCLUSIVE lock: {_exc_lock} — proceeding in WAL mode")

                    # Maintenance-specific pragmas.
                    # IMPORTANT: keep cache_size server-machine-safe (128 MB, not 2 GB).
                    # VACUUM reads the entire DB into the page cache; an aggressive
                    # cache_size on a memory-constrained server-machine (e.g. 2-4 GB total RAM)
                    # fills physical RAM over ~30-60 min and triggers the Linux OOM
                    # killer — SIGKILL with no cleanup, shell closed, no log entry.
                    # 128 MB is still 64× the SQLite default and keeps HDD I/O low
                    # without exhausting server-machine RAM.
                    conn.execute("PRAGMA mmap_size=0")             # disabled during VACUUM — sequential rebuild gets no benefit from mmap on any storage type
                    conn.execute("PRAGMA cache_size=-131072")       # 128 MB — server-machine-safe (was 2 GB, caused OOM kill)
                    conn.execute("PRAGMA threads=2")
                    conn.execute("PRAGMA synchronous=OFF")
                    conn.execute("PRAGMA temp_store=FILE")          # temp tables to disk — avoids extra RAM pressure
                    import os as _os
                    _db_dir = _os.path.dirname(str(self.db_path))
                    conn.execute(f"PRAGMA temp_store_directory='{_db_dir}'")
                    msgs: list[str] = []

                    # --- Decide VACUUM early so we can skip redundant REINDEX --------
                    _TARGET_PAGE_SIZE = 16384
                    _freelist_early = conn.execute("PRAGMA freelist_count").fetchone()[0]
                    _page_size_early = conn.execute("PRAGMA page_size").fetchone()[0]
                    _page_size_migration = _page_size_early < _TARGET_PAGE_SIZE
                    _do_vacuum = _freelist_early > 500 or _page_size_migration

                    # 1. WAL checkpoint (TRUNCATE)
                    t_step = _time.monotonic()
                    logging.info("[DB-MAINT] Step 1/3: WAL checkpoint (TRUNCATE)...")
                    _ckpt = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    _ckpt_busy, _ckpt_total, _ckpt_moved = (_ckpt[0], _ckpt[1], _ckpt[2]) if _ckpt else (0, 0, 0)
                    logging.info(f"[DB-MAINT] Step 1/3: WAL checkpoint done in {_time.monotonic() - t_step:.1f}s — busy={_ckpt_busy}, frames={_ckpt_total}, moved={_ckpt_moved}")
                    msgs.append(f"WAL checkpoint (moved={_ckpt_moved})")

                    # 2. REINDEX or VACUUM (mutually exclusive)
                    freelist = _freelist_early
                    page_size = _page_size_early

                    major_indexes = [
                        # war_attacks (5): idx_wa_clan_tag / idx_wa_war_id dropped 2026-04-25;
                        # idx_wa_player_tag_date added 2026-07-30 (leaderboard scope="all")
                        "idx_wa_player_tag", "idx_wa_war_clan", "idx_wa_clan_date", "idx_wa_zero_attacks",
                        "idx_wa_player_tag_date",
                        # war_summary (4)
                        "idx_ws_clan_tag", "idx_ws_clan_date", "idx_ws_cwl_season", "idx_ws_war_id",
                        # clans (2)
                        "idx_clans_has_subs", "idx_clans_last_war_update",
                    ]

                    if _do_vacuum:
                        if _page_size_migration:
                            logging.info(
                                f"[DB-MAINT] Step 2/3: PAGE SIZE MIGRATION {page_size} → {_TARGET_PAGE_SIZE} "
                                f"(one-time, reduces B-tree depth for HDD seek reduction)"
                            )
                            # SQLite cannot change page_size via VACUUM while in WAL mode.
                            # Switch to DELETE journal mode first, then back to WAL after VACUUM.
                            conn.execute("PRAGMA journal_mode=DELETE")
                            conn.execute(f"PRAGMA page_size={_TARGET_PAGE_SIZE}")
                        else:
                            # Non-migration VACUUM can stay in WAL mode
                            pass

                        logging.info(f"[DB-MAINT] Step 2/3: REINDEX skipped — VACUUM will rebuild all indexes")
                        msgs.append(f"REINDEX skipped (VACUUM pending)")

                        t_step = _time.monotonic()
                        free_mb = freelist * page_size / 1024**2
                        _reason = (
                            f"page_size migration {page_size}→{_TARGET_PAGE_SIZE}"
                            if _page_size_migration
                            else f"freelist={freelist} pages, ~{free_mb:.0f} MB recoverable"
                        )
                        if free_mb > 200 and not _page_size_migration:
                            logging.info(
                                f"[DB-MAINT] Step 2/3: Large freelist ({free_mb:.0f} MB) — "
                                f"VACUUM INTO will compact to a new file (SSD: typically <5 min). "
                                f"Discord commands will remain blocked until VACUUM completes."
                            )
                        # Log available system RAM before VACUUM so OOM risk is visible in logs.
                        try:
                            with open("/proc/meminfo") as _mf:
                                _memlines = {
                                    k: int(v.split()[0])
                                    for line in _mf
                                    if ":" in line
                                    for k, v in [line.strip().split(":", 1)]
                                }
                            _avail_mb = _memlines.get("MemAvailable", 0) // 1024
                            _total_mb = _memlines.get("MemTotal", 0) // 1024
                            logging.info(
                                f"[DB-MAINT] System RAM: {_avail_mb} MB available / {_total_mb} MB total "
                                f"(cache_size=128 MB — server-machine-safe)"
                            )
                        except Exception:
                            pass  # /proc/meminfo not available (Windows/macOS)
                        logging.info(f"[DB-MAINT] Step 2/3: VACUUM ({_reason})...")
                        saved_mb = 0.0
                        try:
                            import threading as _threading
                            _vacuum_done_evt = _threading.Event()
                            _t_vacuum_start = _time.monotonic()

                            def _vacuum_progress_logger() -> None:
                                while not _vacuum_done_evt.wait(timeout=120):
                                    logging.info(
                                        f"[DB-MAINT] VACUUM still running... "
                                        f"{_time.monotonic() - _t_vacuum_start:.0f}s elapsed"
                                    )

                            _progress_thread = _threading.Thread(
                                target=_vacuum_progress_logger, daemon=True, name="vacuum-progress"
                            )
                            _progress_thread.start()
                            try:
                                pc_before = conn.execute("PRAGMA page_count").fetchone()[0]
                                if _page_size_migration:
                                    # In-place VACUUM: page_size change requires DELETE journal mode
                                    # (already switched above). VACUUM INTO cannot change page_size.
                                    conn.execute("VACUUM")
                                else:
                                    # VACUUM INTO: writes a fresh compacted DB file sequentially.
                                    # On HDD/server-machine this is 10-30× faster than in-place VACUUM because:
                                    #   in-place = random reads of old DB + random seeks back into it
                                    #   VACUUM INTO = sequential reads of old DB + sequential writes
                                    #                 to a new empty file
                                    # Original DB is never modified → safe if interrupted.
                                    _db_path_str = str(self.db_path)
                                    _new_db_path = _db_path_str + ".vacuumed"
                                    try:
                                        if _os.path.exists(_new_db_path):
                                            _os.remove(_new_db_path)
                                            logging.info("[DB-MAINT] Removed leftover .vacuumed file from previous failed run")
                                        logging.info(f"[DB-MAINT] Using VACUUM INTO (sequential HDD writes) → {_new_db_path}")
                                        conn.execute(f"VACUUM INTO '{_new_db_path}'")
                                        # Swap: close our own handle FIRST (Windows cannot delete
                                        # or rename a file that's still open under the current
                                        # process — os.replace()/os.remove() raise WinError 5/32
                                        # otherwise; Linux would tolerate the old ordering via
                                        # unlink-while-open semantics, but closing first is safe
                                        # and correct on both platforms), THEN remove the stale
                                        # WAL/SHM (new DB has no WAL) and atomically replace the
                                        # original with the compacted copy.
                                        try:
                                            conn.close()
                                        except Exception:
                                            pass
                                        for _suf in ("-wal", "-shm"):
                                            _suf_path = _db_path_str + _suf
                                            if _os.path.exists(_suf_path):
                                                try:
                                                    _os.remove(_suf_path)
                                                except Exception as _e_suf:
                                                    logging.warning(f"[DB-MAINT] Could not remove {_suf_path}: {_e_suf}")
                                        _os.replace(_new_db_path, _db_path_str)
                                        logging.info("[DB-MAINT] VACUUM INTO swap complete — compacted DB in place")
                                        # Reopen conn to the swapped-in file.
                                        conn = _sq.connect(_db_path_str, timeout=300, isolation_level=None)
                                        conn.row_factory = _sq.Row
                                        # VACUUM INTO preserves journal_mode in the header, but
                                        # explicitly re-enabling WAL is safe and idempotent.
                                        conn.execute("PRAGMA journal_mode=WAL")
                                        # Re-attach history — the swap above closed the connection
                                        # that had it attached (VACUUM INTO reopens the main file
                                        # under a fresh sqlite3 handle), and the later unqualified
                                        # ANALYZE step relies on 'history' still being attached.
                                        if self.history_db_path:
                                            conn.execute("ATTACH DATABASE ? AS history", (self.history_db_path,))
                                            conn.execute("PRAGMA history.journal_mode=WAL")
                                            conn.execute("PRAGMA history.synchronous=NORMAL")
                                    except Exception:
                                        # Original DB is untouched. Remove partial output.
                                        try:
                                            if _os.path.exists(_new_db_path):
                                                _os.remove(_new_db_path)
                                        except Exception:
                                            pass
                                        # We may have closed `conn` (see comment above) before the
                                        # failure occurred — reopen it against the still-intact
                                        # original file so the outer except/finally blocks (which
                                        # run ANALYZE and PRAGMA locking_mode=NORMAL on `conn`)
                                        # don't crash on a closed connection on top of this failure.
                                        try:
                                            conn.execute("SELECT 1")
                                        except Exception:
                                            conn = _sq.connect(_db_path_str, timeout=300, isolation_level=None)
                                            conn.row_factory = _sq.Row
                                            if self.history_db_path:
                                                conn.execute("ATTACH DATABASE ? AS history", (self.history_db_path,))
                                                conn.execute("PRAGMA history.journal_mode=WAL")
                                                conn.execute("PRAGMA history.synchronous=NORMAL")
                                        raise
                            finally:
                                _vacuum_done_evt.set()
                                _progress_thread.join(timeout=5)
                            new_page_size = conn.execute("PRAGMA page_size").fetchone()[0]
                            pc_after = conn.execute("PRAGMA page_count").fetchone()[0]
                            saved_mb = (pc_before * page_size - pc_after * new_page_size) / 1024**2
                            if _page_size_migration:
                                msgs.append(f"page_size {page_size}→{new_page_size}")
                                # Restore WAL mode (was switched to DELETE for page_size migration)
                                conn.execute("PRAGMA journal_mode=WAL")
                                if new_page_size == page_size:
                                    logging.warning(
                                        f"[DB-MAINT] Step 2/3: page_size UNCHANGED at {page_size} after VACUUM! "
                                        f"This usually means another connection held a lock during VACUUM. "
                                        f"Will retry on next maintenance run."
                                    )
                                else:
                                    logging.info(
                                        f"[DB-MAINT] Step 2/3: Page size migration complete: "
                                        f"{page_size}→{new_page_size}, "
                                        f"pages {pc_before}→{pc_after}"
                                    )
                            msgs.append(f"VACUUM saved {saved_mb:.0f} MB")
                            logging.info("[DB-MAINT] Step 2/3: Post-VACUUM WAL truncate...")
                            _pv_ckpt = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                            _pv_moved = _pv_ckpt[2] if _pv_ckpt else "?"
                            logging.info(f"[DB-MAINT] Step 2/3: Post-VACUUM WAL truncated (moved={_pv_moved})")
                            logging.info(f"[DB-MAINT] Step 2/3: VACUUM done in {_time.monotonic() - t_step:.1f}s")
                        except Exception as _e_vac:
                            msgs.append(f"VACUUM failed")
                            hint = (
                                " — SQLite writes a temp copy of the DB to the OS temp dir"
                                " (TMPDIR, often a small tmpfs). If that filesystem is smaller than"
                                " the DB, VACUUM fails even though the main volume has free space."
                                " Fix: set SQLITE_TMPDIR env var to the DB directory before starting the bot."
                            ) if "full" in str(_e_vac).lower() else ""
                            logging.warning(f"[DB-MAINT] Step 2/3: VACUUM failed: {_e_vac}{hint}")
                            if _page_size_migration:
                                # Restore WAL mode even on failure
                                try:
                                    conn.execute("PRAGMA journal_mode=WAL")
                                except Exception:
                                    pass
                    else:
                        t_step = _time.monotonic()
                        logging.info(f"[DB-MAINT] Step 2/3: REINDEX {len(major_indexes)} indexes...")
                        reindexed = 0
                        for idx in major_indexes:
                            exists = conn.execute(
                                "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (idx,)
                            ).fetchone()
                            if exists:
                                try:
                                    conn.execute(f"REINDEX {idx}")
                                    reindexed += 1
                                except Exception as _e_idx:
                                    logging.warning(f"[DB-MAINT] REINDEX {idx} failed: {_e_idx}")
                        logging.info(f"[DB-MAINT] Step 2/3: REINDEX done in {_time.monotonic() - t_step:.1f}s")
                        msgs.append(f"REINDEX ({reindexed}/{len(major_indexes)})")
                        logging.info(f"[DB-MAINT] Step 2/3: VACUUM skipped (freelist={freelist} pages < 500 threshold)")
                        msgs.append(f"VACUUM skipped (freelist={freelist} pages)")

                    # 3. ANALYZE (always last)
                    t_step = _time.monotonic()
                    logging.info("[DB-MAINT] Step 3/3: ANALYZE + PRAGMA optimize...")
                    conn.execute("PRAGMA analysis_limit=1000")
                    conn.execute("ANALYZE")
                    conn.execute("PRAGMA optimize")
                    logging.info(f"[DB-MAINT] Step 3/3: ANALYZE done in {_time.monotonic() - t_step:.1f}s")
                    msgs.append("ANALYZE+optimize")

                    elapsed = _time.monotonic() - t_start
                    return f"[DB-MAINT] {', '.join(msgs)} — done in {elapsed:.1f}s"
                finally:
                    # Release EXCLUSIVE lock before pool refill.
                    # conn may point to a reopened connection (after VACUUM INTO swap)
                    # or a closed connection (if reopen failed) — guard both cases.
                    try:
                        conn.execute("PRAGMA locking_mode=NORMAL")
                    except Exception:
                        pass
                    try:
                        conn.close()
                    except Exception:
                        pass
            # drain() context exit refills the pool with fresh connections

        result = "[DB-MAINT] ERROR: unknown"
        try:
            # Close the async (aiosqlite) connection before entering EXCLUSIVE mode.
            # The pool drain handles sync connections, but the async connection also
            # holds WAL locks that would block BEGIN EXCLUSIVE for up to 300s.
            if self.conn:
                try:
                    await self._conn.close()
                except Exception:
                    pass
                self.conn = None

            result = await asyncio.to_thread(_run)
            logging.info(result)
            # Release fragmented heap pages back to the OS
            import sys as _sys
            if _sys.platform != "win32":
                try:
                    import ctypes as _ctypes
                    _ctypes.cdll.LoadLibrary("libc.so.6").malloc_trim(0)
                    logging.info("[DB-MAINT] malloc_trim(0) called — fragmented heap pages returned to OS")
                except Exception as _trim_ex:
                    logging.debug(f"[DB-MAINT] malloc_trim unavailable: {_trim_ex}")
        except Exception as e:
            result = f"[DB-MAINT] ERROR: {e}"
            logging.error(result)

        # Reopen the async connection (closed before maintenance for EXCLUSIVE access)
        try:
            await self._reconnect()
        except Exception as _recon_ex:
            logging.error(f"[DB-MAINT] Failed to reopen async connection: {_recon_ex}")

        # Unblock Discord commands
        if _qbcore is not None:
            _qbcore.db_maintenance_mode = False
            logging.info("[DB-MAINT] db_maintenance_mode=False — Discord commands unblocked")

        # Persist timestamp so a bot restart won't double-fire
        try:
            import datetime as _dt
            await self.set_bot_metadata(
                "last_db_maintenance",
                _dt.datetime.now(_dt.timezone.utc).isoformat(),
            )
        except Exception as _meta_ex:
            logging.warning(f"[DB-MAINT] Could not persist maintenance timestamp: {_meta_ex}")

        return result

    async def close(self) -> None:
        """
        Close database connection.

        Should be called during bot shutdown to ensure proper cleanup.

        Sequence to guarantee -wal/-shm sidecar files are physically deleted:
          1. Close all thread-local sync connections first (release their locks).
          2. PRAGMA wal_checkpoint(FULL) — merge all committed WAL frames into
             the main DB file so the sidecar files hold no unsynced data.
          3. Commit any open implicit transaction; close the async connection.
          4. os.remove() the -wal and -shm files directly.

        Why not journal_mode=DELETE:
          journal_mode=DELETE requires SQLite to acquire an EXCLUSIVE lock on the
          database, which fails if ANY other connection has even a shared lock at
          that moment (SQLITE_BUSY → "database is locked"). This is fragile on server-machine
          file systems. Deleting the files with os.remove() after ALL connections
          are closed sidesteps the exclusive-lock requirement entirely — at that
          point the files are just orphaned bytes. The next initialize() call
          re-enables WAL mode with a fresh pair of sidecar files.
        """
        # Step 1: close all sync pool connections
        if self._pool:
            self._pool.close_all()
            self._pool = None

        if self.conn:
            # Step 2: FULL checkpoint — merge all committed WAL frames into main DB.
            # FULL (not TRUNCATE) is used because we will delete the files in step 4;
            # we just need all committed data safely in the main file first.
            try:
                await self._conn.execute("PRAGMA wal_checkpoint(FULL)")
            except Exception as ckpt_ex:
                logging.warning(f"[DB] WAL checkpoint on close failed (continuing): {ckpt_ex}")

            # Step 3: commit any open implicit transaction, then close async connection
            try:
                await self._conn.commit()
            except Exception:
                pass
            await self._conn.close()
            self.conn = None
            self._initialized = False

        # Step 4: physically remove sidecar files now that ALL connections are closed.
        # On Linux: unlink() succeeds even if handles are open (the inode lives until
        # all handles close); works reliably on server-machine/production.
        # On Windows: deletion requires zero open handles. sqlite3.Connection.close()
        # is executed by the aiosqlite background thread, which releases the sqlite3
        # lock — but Windows' memory-mapped view of the .db-shm file (CreateFileMapping)
        # may not be released at the OS level for a short time after close() returns.
        # Fix: retry twice with a 25ms sleep to let Windows finalise the mapping.
        import os as _os
        import time as _time
        _db_path_str = str(self.db_path) if self.db_path else ""
        if _db_path_str:
            for _suffix in ("-wal", "-shm"):
                _sidecar = _db_path_str + _suffix
                if not _os.path.exists(_sidecar):
                    continue
                _removed = False
                for _attempt in range(3):  # try up to 3 times: 0ms, 50ms, 100ms
                    if _attempt:
                        _time.sleep(0.050)
                    try:
                        _os.remove(_sidecar)
                        logging.info(f"[DB] Removed sidecar file: {_sidecar}")
                        _removed = True
                        break
                    except Exception:
                        pass
                if not _removed:
                    # Still locked — not a fatal error. On Linux this never happens.
                    # On Windows the file is harmless; SQLite recreates it on next open.
                    logging.info(f"[DB] Could not remove sidecar file {_sidecar} after 3 attempts (Windows handle delay — harmless)")

        logging.info("[DB] Database connection closed")
