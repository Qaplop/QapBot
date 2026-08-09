# Rate Limiting Implementation

## Overview
Implemented BatchThrottler-based rate limiting with parallel API calls to maximize war update throughput while respecting CoC API limits.

## Architecture

### 1. BatchThrottler Rate Limiter
- **Library**: Uses coc.py's `BatchThrottler` for true parallelization
- **Configuration**: `key_count=10, throttler=coc.BatchThrottler, throttle_limit=100`
- **Keys**: 10 API keys auto-managed by coc.py
- **Configured Limit**: `throttle_limit=100` total requests/sec across all keys (i.e., 10 keys × 10 req/sec per key)
- **Benefit**: Requests across keys execute in parallel, not sequentially

### 1a. Startup key sanity check
`coc.py`'s own key rotation (`HTTPClient.initialise_keys()`, run during `login()`) only checks a
candidate key's name + registered IP against the developer-portal listing before reusing it across
restarts — it never test-calls the key itself. A key that's been revoked or is otherwise broken on
Supercell's auth side, while still listing normally on the portal, therefore stays silently in rotation
forever: every restart picks it back up, and since `BatchThrottler` cycles through the key list in a
stable order, the same fraction of requests keeps failing with `Invalid authorization` on every run. See
`qapbot/docs/COPILOT_PITFALLS_COOKBOOK.md` Pitfall 24 for the incident (2026-08-09) this guards against —
it went undetected for over an hour across 5 DEV restarts because nothing checked at startup, only
surfacing later as scattered per-war 403s.

`QapBot.py: _validate_coc_api_keys()` runs as a fire-and-forget background task right after
`coc_client.login()` (reference held on `QBcore.coc_key_sanity_task` so it isn't garbage-collected
mid-flight; runs from `on_ready()` so the Discord client is already connected by the time it fires).
It tests each of the `key_count` keys directly (bypassing the round-robin) against the cheap, tag-free
`/locations` endpoint, and on any failure logs a `[COC-KEY-SANITY]` `CRITICAL` line naming the
1-indexed position(s) — **and DMs the configured `SERVER_ADMIN`** via `CACHE.send_user_dm()`, on both
DEV and PROD, since a log-only alert had already gone unnoticed for over an hour across 5 restarts in
the incident this guards against. If `SERVER_ADMIN` isn't configured, or the DM can't be delivered
(DMs disabled, bot blocked), it logs a `WARNING` noting that instead — the `CRITICAL` log line is
always written either way. One `INFO` line if all keys pass. It does not block startup and does not
raise; a broken key must still be fixed manually (delete the affected account's `"Created with coc.py
Client"` keys at developer.clashofclans.com and let `coc.py` recreate a fresh set
on the next login) — this only makes the failure visible immediately instead of days later.

### 2. Centralized API Access

All CoC API calls route through `CACHE` (cache_manager):

```python
# Clan data (with stale-while-revalidate caching)
clan_obj = await CACHE.coc_clan_cache.get_clan(clan_tag)

# War data (no caching - always fresh)
war_obj = await CACHE.get_current_war_from_api(clan_tag)
```

**CoCClanCache Strategy** (stale-while-revalidate):
- **Fresh** (< 280s soft TTL): Return cached data immediately
- **Stale** (280s–600s): Return cached data + fire-and-forget background refresh
- **Expired** (> 600s hard TTL): Blocking API fetch

**Benefits:**
- Dramatically reduces blocking API calls during normal operation
- Background refreshes keep cache warm without user-facing latency
- Single point of control for rate limiting
- Easy to add logging/monitoring

### 3. Parallel Processing

Main loop uses a multi-phase pipeline (parallel API I/O, then deterministic file/DB
processing). The actual phases emitted by `QapBot.py` are tagged `[PHASE-1]`,
`[PHASE-1.5]`, `[PHASE-2]`, `[PHASE-3]` and `[PHASE-3B]`:

```python
# PHASE 1: parallel CoC API fetches for active clans (async)
fetch_tasks = [fetch_single_clan(clan_tag, is_active) for clan_tag, is_active in clans_to_update]
fetch_results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

# PHASE 1.5: CWL war-tag recovery batch (async) — re-fetches active CWL wars whose
#            war tags were discovered during Phase 1, writing their temp JSON.

# PHASE 2: orphaned CWL recovery (async) + background WAL checkpoint dispatch
failed_clans = {clan_tag for (clan_tag, _, war_data) in fetch_results if war_data is None}
await process_orphaned_cwl_wars(failed_clans)

# PHASE 3: sequential processing of active-clan fetch results (sync)
for clan_tag, is_active, war_data in fetch_results:
   if war_data is None:
      manage_war_files(clan_tag, "")  # finalize any orphaned/updated wars
      continue
   process_clan_war_data(clan_tag, war_data)

# PHASE 3B: finalize temp files for passive (inactive) clans (sync), using an
#           mtime-based skip-until optimization so unchanged passive war files on
#           HDD/server-machine are not re-read every cycle.
```

**Notes:**
- Phase 1 is the primary beneficiary of `BatchThrottler` (many concurrent API requests).
- Phase 1.5 batches CWL war-tag follow-up fetches discovered during Phase 1.
- Phase 3 and Phase 3B remain intentionally sequential to avoid race conditions in
  file/DB work. Phase 3B additionally skips unchanged passive war files via an
  mtime / skip-until cache to minimize server-machine I/O.


## Implementation Details

### Modified Files

1. **cache_manager.py**
   - Added `coc_client` attribute to CacheManager
   - Added `get_current_war_from_api()` wrapper method
   - Updated docstring with rate limiting info

2. **QapBot.py**
   - Modified `startup_login()` to create client with `key_count=10, throttler=coc.BatchThrottler, throttle_limit=100`
   - Stores client in both `QBcore.coc_client` (backward compat) and `CACHE.coc_client`
   - Refactored `main()` to use `asyncio.gather()` for parallel updates
   - Improved logging with success/failed counts

3. **QBhelperfunctions.py**
   - Replaced `QBcore.coc_client.get_current_war()` with `CACHE.get_current_war_from_api()`

### Key Design Decisions

**Q: Why not cache war data like clan data?**
A: War data is always single-use and needs fresh data for real-time statistics. Caching would provide no benefit.

**Q: Why use coc.py's BatchThrottler instead of custom?**
A: It manages multiple API keys automatically, uses true parallelization (not sequential queuing), and integrates seamlessly with coc.Client.

**Q: How does BatchThrottler differ from the default throttler?**
A: The default throttler processes requests sequentially per key. BatchThrottler dispatches requests across keys in parallel, allowing higher concurrency up to the configured `throttle_limit` (bounded by the per-key limit).

## Future Enhancements

### Monitoring & Metrics

Add logging for:
- Requests per second (actual vs limit)
- Cache hit/miss/stale ratios
- Background refresh success rate
- API errors and rate limit hits
- Average request latency by type

## Testing

**Test scenarios:**
1. ✅ Single clan update (baseline)
2. ✅ 10 clans in parallel (at rate limit)
3. ✅ 50 clans in parallel (exceeds rate limit - should queue)
4. ✅ Error isolation (one clan failure doesn't stop others)
5. ✅ Rate limit safety (no 429 errors from CoC API)

## Performance Impact

**After:** Parallel API fetches with BatchThrottler (10 keys) + sequential processing
- Total cycle time depends on API latency, number of clans, and local file/DB work.
- The configured request budget is higher, so API fetches are less likely to become the bottleneck.

## Configuration

Current settings:
```python
# In startup_login()
client = coc.Client(
    key_count=10,                     # 10 API keys managed automatically
    throttler=coc.BatchThrottler,     # Parallel request dispatching
    throttle_limit=100                # 100 req/sec total (10 keys × 10 req/sec)
)
```

To adjust:
```python
# Conservative (fewer keys, lower throughput)
client = coc.Client(key_count=5, throttler=coc.BatchThrottler, throttle_limit=50)

# Single key (fallback, sequential)
client = coc.Client(throttle_limit=10)
```

**Recommendation:** Keep `key_count` and `throttle_limit` aligned with the official per-key limit (10 req/sec/key). Reduce `key_count` only if encountering key provisioning/account limits.

## CoC API Maintenance Handling

CoC server maintenance is a **global outage** lasting 10–30+ minutes. Retrying individual
clan fetches within the same cycle is pointless and caused extreme cycle bloat:

> 1889 clans × (10s sleep + retry + 20s sleep) / 20 concurrent slots ≈ **2833s extra**
> — observed in the wild as a 3204s cycle on 2026-05-26

### Strategy: cycle-wide fast-fail (`qapbot/coc_health.py`)

- `_maintenance_detected` (bool) — module-level flag, reset at each cycle start
- **First** `coc.Maintenance` received by any concurrent `coc_retry()` call:
  - Sets `_maintenance_detected = True`
  - Logs **one** `[WARNING] [COC-MAINTENANCE]` with the clan that triggered it
  - Raises immediately — no sleep, no retry
- **All subsequent** concurrent `coc.Maintenance` hits (flag already set):
  - Log at `DEBUG` only
  - Raise immediately — no sleep, no retry
- After `asyncio.gather()` completes Phase 1, `QapBot.main()` checks `is_maintenance_detected()`
  and emits one `[PHASE-1] [WARNING]` summary with the total affected clan count
- Phases 2–4 still run normally on existing temp files — war finalization is unaffected
- **Next cycle** (≥5 min later): `clear_maintenance_detection()` resets the flag;
  all API calls re-probe the CoC API as normal

### Result

| Scenario | Before | After |
|---|---|---|
| Maintenance log noise | ~3800 WARNING lines / event | 1 WARNING + N DEBUG lines |
| Cycle duration during maintenance | ~53 min (2026-05-26) | ~5 min (fast-fail) |
| Phases 2–4 | Blocked waiting for retries | Run normally on existing temp files |
