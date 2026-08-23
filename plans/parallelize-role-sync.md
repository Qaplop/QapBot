# Implementation Plan — Parallelize Discord role sync (bounded concurrency)

**Source:** `backlog.txt` item "Parallelize role sync (found via `log_time_gaps.py` against the
first PROD cycle after the 2026-08-14 `has_active_subscriptions` fix — changelog (12))".
No tracker item exists for this, so the plan filename carries no `tracker-NNNN-` prefix
(Cardinal Rule 15).

**Status:** not started. Hand-over target: sonnet-5.

---

## 1. Problem

[qapbot/guild_role_manager.py:960-964](../qapbot/guild_role_manager.py#L960-L964) —
`sync_roles_for_clan_members()` ends in a plain serial loop:

```python
for discord_user_id in discord_users_to_sync:
    try:
        await sync_roles_for_user(guild, guild_id, discord_user_id)
    except Exception as e:
        logging.warning(...)
```

Every iteration awaits a chain of Discord API round-trips (`fetch_member()` on a local cache
miss, then up to 4 CoC-role `add_roles`/`remove_roles` edits plus one per configured clan role).
Those go through discord.py's per-guild rate-limit bucket one at a time, so a clan with many
registered members burns real wall-clock time inside a single update cycle. `log_time_gaps.py`
measured **4 gaps of 34s / 17.9s / 17.8s / 11.1s in one ~5min PROD cycle**.

This runs on the background task spawned by
[qapbot/coc_cache.py:434](../qapbot/coc_cache.py#L434) (`_do_role_sync` →
`spawn_tracked("coc-cache-role-sync-<tag>")`), once per clan per real API fetch, and it iterates
guilds sequentially inside that task too — so the per-clan cost multiplies by the number of
guilds covering that clan.

## 2. Fix

Replace the serial loop with **bounded concurrency**, using the same pattern as Phase 1's fetch
semaphore in [QapBot.py:1137-1142](../QapBot.py#L1137-L1142) and
[QapBot.py:1161-1166](../QapBot.py#L1161-L1166): a per-call `asyncio.Semaphore` + an inner
`async def` worker + `asyncio.gather(..., return_exceptions=True)`.

### Why per-call and not a module-level semaphore

Phase 1 constructs its semaphore inside the function, and we should match that:

- A module-level semaphore would be shared across *all* guilds and *all* clans, coupling
  unrelated guilds' role syncs to each other — Discord's role-edit rate limit is per-guild, so
  that coupling buys nothing and only adds contention.
- It also avoids any loop-binding question about constructing an `asyncio.Semaphore` at import
  time.

### Concurrency value

Use `_ROLE_SYNC_CONCURRENCY = 5` (module-level constant, so it is greppable and tunable in one
place — but the `Semaphore` itself is constructed per call).

5, not Phase 1's 20: Phase 1 bounds *outbound CoC API connections*, where the ceiling is
connection/SSL pressure. Here the ceiling is Discord's per-guild member-modify rate limit, which
discord.py already serializes behind its own bucket lock — concurrency above a handful just
queues up behind that lock and holds more coroutines open for no gain. The win we are after is
overlapping the *non-bucket* parts: `fetch_member()` round-trips, the many calls that turn out
to be complete no-ops (`assign_role_to_member()` returns early when the role is already present,
[guild_role_manager.py:530-531](../qapbot/guild_role_manager.py#L530-L531) — no API call at all),
and per-user CACHE work. Start at 5; it is a one-constant change if PROD logs show headroom.

## 3. Changes

### 3.1 `qapbot/guild_role_manager.py` — constant

Add next to the other module-level constants (near `_users_being_synced`, around
[line 88](../qapbot/guild_role_manager.py#L88)):

```python
# Max concurrent per-user role syncs inside one sync_roles_for_clan_members() /
# sync_all_roles_for_guild() call.  Bounds how many Discord member-edit chains are in
# flight at once: discord.py already serializes the actual role-edit calls behind its
# per-guild rate-limit bucket, so this exists to overlap the *other* work (fetch_member()
# round-trips, no-op role checks, CACHE lookups) without opening an unbounded number of
# coroutines.  Same pattern as Phase 1's _FETCH_CONCURRENCY in QapBot.py, lower value
# because the binding constraint here is a Discord per-guild bucket, not connection count.
_ROLE_SYNC_CONCURRENCY = 5
```

### 3.2 `qapbot/guild_role_manager.py` — replace the serial loop

Replace [lines 960-964](../qapbot/guild_role_manager.py#L960-L964) with:

```python
    if not discord_users_to_sync:
        return

    _sem = asyncio.Semaphore(_ROLE_SYNC_CONCURRENCY)

    async def _sync_one(discord_user_id: int) -> None:
        """Sync one user's roles, semaphore-bounded.  Never raises."""
        async with _sem:
            try:
                await sync_roles_for_user(guild, guild_id, discord_user_id)
            except Exception as e:
                logging.warning(
                    f"[ROLE-SYNC] Error syncing roles for user {discord_user_id} in guild "
                    f"{guild.name} ({guild_id}) (clan {clan_tag}): {e}"
                )

    _t0 = time.monotonic()
    await asyncio.gather(
        *(_sync_one(uid) for uid in discord_users_to_sync),
        return_exceptions=True,
    )
    logging.info(
        "[ROLE-SYNC] Clan %s in guild %s (%s): synced %d user(s) in %.2fs (max %d concurrent)",
        clan_tag, guild.name, guild_id, len(discord_users_to_sync),
        time.monotonic() - _t0, _ROLE_SYNC_CONCURRENCY,
    )
```

Notes for the implementer:

- **Keep the warning message text byte-identical** to the current one. It is the same log line
  operators grep for, and `log_time_gaps.py` correlation against past PROD logs depends on it.
- `asyncio` is already imported ([line 34](../qapbot/guild_role_manager.py#L34)). **`time` is
  not** — add `import time` to the stdlib import block.
- `return_exceptions=True` is belt-and-braces: `_sync_one` already swallows everything, but this
  matches Phase 1 and guarantees one pathological worker can never abort the batch.
- The new INFO line is the verification hook — after deploy, `log_time_gaps.py` on a PROD cycle
  should show the 30s-class gaps collapse, and this line gives the per-clan number directly.

### 3.3 The `_users_being_synced` guard — verify, don't change

[guild_role_manager.py:606-612](../qapbot/guild_role_manager.py#L606-L612) *skips* (does not wait
for) a user already being synced. Two things to confirm while implementing:

1. **Within one call there are no duplicates**: the collection loop at
   [lines 946-958](../qapbot/guild_role_manager.py#L946-L958) `break`s after the first matching
   player, so each `user_id_str` is appended at most once. Concurrency inside one call therefore
   cannot self-collide. Verify this still holds; do not add a `set()` dedupe on top (it would
   change iteration order for no benefit).
2. **Across calls the skip rate will rise**: with N clan syncs now running fast and overlapping,
   a user registered in two clans of the same guild is more likely to hit the guard and be
   skipped for one of them. This is **acceptable and pre-existing** — the guard exists for
   exactly that case, and the skipped sync is redundant work (the in-flight one computes the
   same result from the same CACHE). Do **not** convert the guard into a lock/wait as part of
   this change; that is a separate design decision with its own deadlock surface.

### 3.4 Optional Step — `sync_all_roles_for_guild()` (recommend including; get sign-off)

The backlog item names `sync_roles_for_clan_members()` only, but the identical serial pattern
exists twice more in the same file, in `sync_all_roles_for_guild()`:

- [lines 855-864](../qapbot/guild_role_manager.py#L855-L864) — the `for member in guild.members`
  loop over every registered guild member (**the bigger population of the two functions**).
- [lines 887-896](../qapbot/guild_role_manager.py#L887-L896) — the capped (≤50) missing-member
  verification loop, where every iteration is a guaranteed `fetch_member()` API call.

Same treatment applies cleanly, with three extra constraints:

- Both loops maintain `synced` / `errors` counters. Under `gather` these should not be
  incremented by `+= 1` on a closure variable — that happens to work in asyncio (no preemption
  mid-statement) but is fragile to read. Have the worker **return** a `bool`/sentinel and tally
  the gathered results afterwards, the way Phase 1 tallies `fetch_results` at
  [QapBot.py:1230-1236](../QapBot.py#L1230-L1236).
- The first loop passes `member=member` (fast path, no `fetch_member()`); preserve that.
- Build `seen_ids` in a plain synchronous pass over `guild.members` **before** launching the
  gather, so `missing_ids` is still computed from a complete set (today it is filled during the
  same loop that syncs).

**Recommendation: do include it**, in the same change. It is the same three-line pattern, it is
where the larger member population is, and splitting it off means touching and re-testing the
same function twice. If the reviewer prefers the minimal backlog-scoped change, do 3.1–3.3 only
and leave this section here as the follow-up.

## 4. Tests

Extend `tests/unit/test_guild_role_manager.py` — it already has async class-based tests and the
fixture shape to copy from (see `TestCocRoleBootstrap` at
[line 359](../tests/unit/test_guild_role_manager.py#L359) for the
`patch("qapbot.cache_manager.CACHE", fake_cache)` + fake-guild setup). Add a
`TestRoleSyncConcurrency` class marked `@pytest.mark.smoke`:

1. **`test_all_users_synced_exactly_once`** — 12 registered users in the clan; patch
   `qapbot.guild_role_manager.sync_roles_for_user` with an async fake that records its
   `discord_user_id`. Assert the recorded set equals all 12, with no duplicates.
2. **`test_concurrency_is_bounded`** — the important one. Fake `sync_roles_for_user` increments
   an `in_flight` counter, tracks `max_in_flight`, `await asyncio.sleep(0.01)`, decrements.
   Assert `max_in_flight <= grm._ROLE_SYNC_CONCURRENCY` **and** `max_in_flight > 1`. The second
   half is what actually proves it is no longer serial — a test checking only the upper bound
   passes against the unfixed code.
3. **`test_one_failure_does_not_abort_batch`** — fake raises `RuntimeError` for one specific user
   id, succeeds for the rest. Assert every other user was still synced, and that the existing
   `[ROLE-SYNC] Error syncing roles for user ...` warning was emitted (`caplog`).
4. **`test_empty_user_list_short_circuits`** — no registered users in the clan → returns without
   calling `sync_roles_for_user` and without raising.
5. If 3.4 is included: mirror tests 1–3 against `sync_all_roles_for_guild`, plus
   **`test_counters_match_results`** — assert the final
   `"role sync complete: N synced, M errors"` INFO line reports the right N/M when some users
   fail, since that tally moves from in-loop increments to post-gather counting.

Run with `.\run_tests.ps1` — **never** a raw pytest command. Baseline as of the last commit is
**2527 tests pass**; report the real number from the run in the changelog entry.

## 5. Docs (Cardinal Rule 15 — same change, not a follow-up)

- **`qapbot/docs/CODE_STRUCTURE.md`** — the `guild_role_manager.py` tree at
  [lines 987-996](../qapbot/docs/CODE_STRUCTURE.md#L987-L996) currently says
  `"Calls sync_roles_for_user() for every registered guild member"`. Update that line and add a
  `sync_roles_for_clan_members` entry noting the bounded-concurrency (`_ROLE_SYNC_CONCURRENCY`)
  behaviour.
- **`qapbot/docs/RATE_LIMITING_IMPLEMENTATION.md`** — this doc covers the CoC-API side and the
  parallel fetch pipeline; it has no section on the *Discord* side. Add a short one: "Discord
  role-edit concurrency" — where the bound lives, why 5 vs Phase 1's 20, and the fact that
  discord.py serializes the actual edits per-guild anyway. That is the natural home for the next
  person asking "why is role sync capped at 5".
- Do **not** add a Pitfall entry — this is a performance change, not a trap that bit us.

## 6. Housekeeping

- **`backlog.txt`** — remove the "Parallelize role sync" item (lines 11-17). This is the
  Implementation Workflow's step 6: the item is explicitly a "fix it next time this code is
  touched" deferral, and this change is that touch.
- **`changelog.txt`** — new entry at the **TOP** of the file, dated with **today's** date and the
  next sequence number for that date (today's format: `2026-08-23 (N)`). Content: what was
  serial, the measured PROD gaps it caused (34s/17.9s/17.8s/11.1s in one ~5min cycle), the
  bounded-concurrency fix and the chosen limit, files touched, and the real test count.
- **This plan file** — once implemented, tests pass, and committed:
  `git mv plans/parallelize-role-sync.md plans/implemented/` in the same turn.

## 7. Verification on DEV / PROD

1. `.\run_tests.ps1` green.
2. DEV: run one full update cycle with `coc_role_enabled` / `clan_role_enabled` on, and confirm
   the new `[ROLE-SYNC] Clan ... synced N user(s) in X.XXs` lines appear with plausible numbers
   and no new `Forbidden` / `429` warnings.
3. PROD (after sign-off): run `qapbot/scripts/log_time_gaps.py` against the first full cycle
   after deploy — the 30s-class gaps attributable to role sync should be gone. That script is how
   the problem was found, so it is the right instrument to confirm the fix.
4. Sanity-check on DEV that roles are still correct (one user gains a role, one loses one) — the
   concurrency change must not alter *what* gets assigned, only how fast.

## 8. Out of scope

- Changing `_users_being_synced` from skip-semantics to wait-semantics (§3.3).
- Parallelizing the *guild* loop in `coc_cache.py`'s `_do_role_sync`
  ([lines 410-441](../qapbot/coc_cache.py#L410-L441)). Guild count is small and each guild is a
  separate rate-limit bucket, so the per-guild fix above should already cover the measured cost;
  revisit only if PROD logs still show a gap after this lands.
- Any change to `assign_role_to_member` / `remove_role_from_member` error handling.
