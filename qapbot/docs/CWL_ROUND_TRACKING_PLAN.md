# CWL Round Tracking — Implementation Plan

**Status**: Implemented  
**Session**: 2026-05-06

---

## Problem

War files are stored without an absolute CWL round number. The round is currently
derived by ranking each clan's captured wars chronologically — but this is relative
to when the bot *started* tracking the clan, not the actual CoC round. A clan first
tracked on Round 4 will have that war stored as "Round 1" in the analysis.

This makes completeness analysis unreliable: we can't tell which CoC rounds are
genuinely missing vs. which rounds predate our tracking of that clan.

---

## Source of Truth

`GET /clans/{tag}/currentwar/leaguegroup` returns:

```json
{
  "season": "2026-05",
  "state": "inWar",
  "clans": [ ... ],
  "rounds": [
    { "warTags": ["#ABC", "#DEF", "#GHI", "#JKL"] },
    { "warTags": ["#0",   "#0",   "#0",   "#0"  ] }
  ]
}
```

- 7 round entries always present, each covering 4 wars (8 clans per group)
- `"#0"` = round not yet revealed (only revealed when that round enters `inWar`)
- `GET /clanwarleagues/wars/{warTag}` resolves a war tag → both clan tags + full war data

### Does the API expose a `league_group_id`?

**No.** Inspected `ClanWarLeagueGroup` from coc.py v4:
```python
__slots__ = ("state", "season", "rounds", "number_of_rounds",
             "_client", "__iter_clans", "_cs_clans", "_raw_data")
```
No `tag`, no `id`. The `ExtendedCWLGroup` class has a numeric `_id` but it
comes from a non-public Supercell endpoint — not available via the public API.
We therefore derive our own `league_group_id` (see Schema Changes below).

---

## Proposed Schema Changes

### New table: `cwl_league_rounds`

Four columns exactly — one row per unique war in CWL.

```sql
CREATE TABLE IF NOT EXISTS cwl_league_rounds (
    war_tag         TEXT    NOT NULL PRIMARY KEY,  -- globally unique CWL war tag
    cwl_season      TEXT    NOT NULL,              -- '2026-05'
    cwl_round       INTEGER NOT NULL,              -- 1–7
    league_group_id TEXT    NOT NULL               -- FK → cwl_league_groups
);
```

`war_tag` is the natural primary key (globally unique across all seasons).
No `clan_tag` column here — the link to specific clans goes through
`cwl_league_groups` via `league_group_id`.

### New table: `cwl_league_groups`

One row per (clan × season × group). Stores the full 8-clan membership so
any single clan_tag can find its whole group.

```sql
CREATE TABLE IF NOT EXISTS cwl_league_groups (
    league_group_id TEXT    NOT NULL,  -- derived hash (see below)
    cwl_season      TEXT    NOT NULL,  -- '2026-05'
    clan_tag        TEXT    NOT NULL,  -- one of the 8 clans in this group
    PRIMARY KEY (cwl_season, clan_tag) -- a clan is in at most 1 group per season
);
CREATE INDEX IF NOT EXISTS idx_cwl_league_groups_id
    ON cwl_league_groups (league_group_id, cwl_season);
```

### `league_group_id` derivation

Since no ID is available from the public API, we derive a reproducible
deterministic key from the 8 clan_tags in the group:

```python
# Now a static method: CacheManager._make_league_group_id(clan_tags, cwl_season)
import hashlib

def _make_league_group_id(clan_tags: list[str], cwl_season: str) -> str:
    """Stable 16-char hex ID for a CWL group, unique per season and clan set."""
    sorted_tags = "|".join(sorted(t.lstrip("#").upper() for t in clan_tags))
    key = f"{cwl_season}:{sorted_tags}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]
```

Properties:
- **Deterministic** — same 8 clans + same season always produce the same ID
  regardless of query order or which clan triggered the call.
- **Truly unique** — includes `cwl_season` in the hash, so the same 8 clans
  meeting again in a future season produce a different ID.
- **16 hex chars (64 bits) is the right length at QapBot scale**: 200 000 clans
  → 25 000 groups/season → ~1.5 M lifetime IDs over 5 years. Birthday-paradox
  collision probability at 64 bits is ~6×10⁻⁸ % (negligible). Shorter lengths
  are unsafe: 12 hex = ~0.4 %, 14 hex = ~0.002 %.
- **Recalculable** — can be recomputed at any time from the leaguegroup response.

### Modified: `war_summary`

```sql
ALTER TABLE war_summary ADD COLUMN round_number INTEGER;
-- NULL for regular wars; 1-7 for CWL wars
-- cwl_season and war_tag columns already exist
```

---

## Linking Strategy

### Round → war_summary

`cwl_league_rounds.war_tag = war_summary.war_tag` — globally unique 1:1 join.

Backfill query after each `cwl_league_rounds` upsert:

```sql
UPDATE war_summary
SET    round_number = (
    SELECT r.cwl_round
    FROM   cwl_league_rounds r
    WHERE  r.war_tag = war_summary.war_tag
)
WHERE  is_cwl        = 1
  AND  round_number IS NULL;
```

(Simpler than before — no `cwl_season`/`clan_tag` join needed since
`war_tag` is globally unique.)

### Clan → all group members

```sql
-- Given one clan_tag, find all 8 clans in its group for a season:
SELECT g2.clan_tag
FROM   cwl_league_groups g1
JOIN   cwl_league_groups g2
    ON g2.league_group_id = g1.league_group_id
   AND g2.cwl_season      = g1.cwl_season
WHERE  g1.clan_tag  = '#QUERIED_CLAN'
  AND  g1.cwl_season = '2026-05';
```

### Clan → round numbers

```sql
-- Given one clan_tag, get all CWL round numbers for a season:
SELECT r.cwl_round, r.war_tag
FROM   cwl_league_groups  g
JOIN   cwl_league_rounds  r
    ON r.league_group_id = g.league_group_id
   AND r.cwl_season      = g.cwl_season
WHERE  g.clan_tag  = '#QUERIED_CLAN'
  AND  g.cwl_season = '2026-05'
ORDER  BY r.cwl_round;
```

---

## Data Acquisition Strategy

Two complementary layers — together they guarantee completeness with minimum
extra API load.

### Layer 1 — Opportunity-based (zero extra API calls)

Every existing site that already calls `GET /currentwar/leaguegroup` for any
reason automatically triggers `CacheManager._process_league_group_response()` —
it is called directly from `cache_manager.get_league_group()` on every fresh API
response, regardless of which call site triggered the fetch.

1. Reads the `clans` list from the response → gets **all 8 clan tags** in the group.
2. For each revealed war tag (not `"#0"`) in each round:
   - Checks `cwl_league_rounds` in the DB for every clan in the group.
   - Upserts any round rows that are new or updated (war_tag changed from `#0`).
3. No additional API call is made — this is pure free riding on calls that were
   going to happen anyway.

Existing call sites to instrument:
- `_find_active_cwl_war_for_clan()` (private-warlog fallback — already calls leaguegroup)
- Any other future site that fetches the league group

Because each group has 8 clans, a single call for one clan populates data for
all 8. During an active CWL season most groups will be covered by organic
traffic long before they need the fallback layer.

### Layer 2 — Finalization fallback (worst-case 1/8 × N extra calls)

Triggered only when a CWL war is finalized and `round_number` is still unknown.

```
war finalized → about to insert into war_summary (is_cwl=1)
│
└── round_number already in cwl_league_rounds for this clan+season+war_tag?
    │
    ├── YES → include round_number in the INSERT directly
    │
    └── NO  → call GET /currentwar/leaguegroup for this clan (Layer 2)
               └── run _process_league_group_response()
                   (upserts round data for all 8 clans in the group)
               └── include now-resolved round_number in the INSERT
```

**Why 1/8 × N in the worst case**: each group has 8 clans. The first finalized
war in a group triggers one league-group call and populates round data for all
8 members. The remaining 7 clans in that group already have their data in the
DB, so their finalizations skip the call entirely. In practice Layer 1 will
have covered many groups already, making the true cost much lower.

### Combined update flow

```
_CacheManager._process_league_group_response(lg_response, season)
│
├── extract all 8 clan_tags from lg_response.clans
├── league_group_id = self._make_league_group_id(group_clan_tags, season)
│
├── UPSERT cwl_league_groups — 8 rows (skip INSERT if already present):
│     INSERT OR IGNORE INTO cwl_league_groups
│       (league_group_id, cwl_season, clan_tag) VALUES (?, ?, ?)
│     -- for each of the 8 clan_tags
│
├── for round 1..7:
│     for each war_tag in lg_response.rounds[r] where war_tag != '#0':
│       INSERT OR IGNORE INTO cwl_league_rounds
│         (war_tag, cwl_season, cwl_round, league_group_id)
│         VALUES (?, ?, ?, ?)
│       -- INSERT OR IGNORE because war_tag PK never changes once revealed
│
└── run backfill UPDATE on war_summary (see Linking Strategy above)
```

---

## API Cost

| Call | Trigger | Worst-case frequency |
|------|---------|----------------------|
| `GET /currentwar/leaguegroup` (Layer 1) | already-scheduled polls | 0 extra calls |
| `GET /currentwar/leaguegroup` (Layer 2) | first finalized war per group with unknown round | ≤4 per group per season (see below) |

**Why at most 4 Layer 2 calls per group per season:** Each call reveals data for both the current `inWar` round and the next round already in `preparation`, so covering all 7 rounds requires at most ⌈7/2⌉ = 4 calls per group — giving a season-total worst case of 4 × (N/8) = N/2 additional API calls across all tracked clans.

---

## Why `war_summary` coverage is inherently asymmetric (by design)

`cwl_league_rounds` (this doc) tracks *round metadata* cheaply for all 8 clans in
a group via Layer 1/2 above — but that is independent of whether the bot ever
archives a `war_summary` row for each clan's own perspective of a war. Archiving
is per-clan: `add_war_data_sync()` writes exactly one row for the `clan_tag`
whose own war fetch produced it (see `QBcsvhandling._append_current_war_to_history`).
A war between clan A and clan B only gets **two** `war_summary` rows if *both*
A and B are independently fetched and finalized — round metadata knowing about
both sides does not cause both sides to be archived.

Whether a clan is fetched at all is gated by `track_war_updates`, a **one-way
ratchet** (see `qapbot/docs/CLAN_WAR_TRACKING.md`): only clans that are
subscribed, or currently/formerly Master III+, are actively polled. Clans
below Master III that are not subscribed and never a group-mate of a
subscribed clan are "passively tracked" — no `war_summary` row is ever written
for their own perspective. `CWL-GROUP-EXPAND` (`QapBot.py`) force-polls all 8
members of any group containing a subscribed clan every cycle, which closes
the gap for those groups, but groups with **no** subscribed member and whose
clans are below M3 are never polled at all.

Practical consequence for `qapbot/scripts/analyze_cwl_rounds.py --fetch-missing`:
- "Missing both entries" (counter==2): the whole group had no subscribed
  clan and no M3+ presence — neither side was ever fetched.
- "Missing one entry" (counter==1): one clan of the pairing was tracked
  (subscribed, group-expanded, or M3+) and got archived; the other was a
  below-M3, non-subscribed opponent that was never independently fetched.
  This is the dominant case in practice.

This is **expected behavior**, not a pipeline bug — the bot intentionally does
not attempt full bidirectional real-time archiving for every clan in every
CWL group globally (that would require actively polling ~8x the clan
population). `process_cwl_recovery_batch()` (`QBhelperfunctions.py`) — fed by
this script's `--fetch-missing` output — is the intended catch-up path, and it
also only writes rows for participants with `track_war_updates=True`
(`QBhelperfunctions.py:6197`), so a below-M3 non-subscribed opponent's
perspective stays permanently unarchived even after recovery, unless that
clan is itself subscribed or promoted to M3+ later.

**2026-08-08 fix**: `_sync_group_track_war_updates()` (`qapbot/cache_manager.py`,
see `CLAN_WAR_TRACKING.md` write-path 7) now corrects `track_war_updates` for
every group member the moment a group's `league_rank` is confirmed — including
inserting members never before seen in `clan_name_cache` — closing the
"already-known-but-stuck" variant of the ~40,000-clan incident referenced
above for any group *reachable* from the discovery graph. It does **not**
close the "missing both" case for a wholly foreign group with zero connection
to any subscribed clan (directly or transitively): every discovery entry point
still requires the querying clan tag to already be graph-reachable — there is
no CoC rankings/location-endpoint scan or other independent clan enumeration
in this codebase. That residual case is architectural, not a bug to fix
incrementally.

In practice this residual case is expected to self-resolve, not accumulate: CWL matchmaking pairs
clans by strength/league, not by "known to the bot" status, and any clan that is `track_war_updates
= True` stays in the active polling pool permanently (one-way ratchet — see `CLAN_WAR_TRACKING.md`).
So the very next season a wholly-foreign clan gets grouped alongside *any* already-tracked clan,
that group's `get_league_group()` response reaches `_sync_group_track_war_updates()` and every
member — including the previously-foreign one — gets inserted and correctly tracked from then on.
Given the already-large and still-growing tracked population (~110,000 clans across Bronze–Legend
as of the 2026-08 season), the probability of a given active M3+ clan going many consecutive
seasons without ever sharing a group with a tracked clan is low and shrinks further each season.
This is a probabilistic/eventual-consistency guarantee, not a hard one by construction — but no
further engineering is needed here unless it's later observed not to hold in practice.

**2026-08-09 finding: the mid-season-abandonment bucket regrows after the fix, not just a
one-time backlog.** `repromote_mid_season_clans.py --apply` was run on prod 2026-08-08 and
confirmed down to 0. One day later, a dry-run against a fresh prod backup found **2,332 new**
clans back in the exact same state (`track_war_updates=0` with archived `war_summary` rows for
the in-progress 2026-08 season). Root cause: the 2026-08-08 guard
(`clan_has_cwl_data_for_season()` / `clan_has_in_progress_cwl_data()`) only blocks a demotion
*at the moment it's evaluated* — if a clan is demoted while it genuinely has zero season data yet
(e.g. an early-round demotion), the demotion is correct at that instant. But
`CWL-GROUP-EXPAND` (`QapBot.py`, ~line 1049) force-fetches all 8 members of any CWL group
containing an actively-tracked clan every cycle, via
`db_manager.get_active_cwl_group_member_tags()` — which filters only on group membership and
`cwl_ended=0`, **not** on the fetched clan's own `track_war_updates`. That fetch gets archived
through the normal Phase 3/3B pipeline regardless of `track_war_updates`. So an already-(correctly)
-demoted clan can still pick up `war_summary` rows later in the same season purely by being a
group-mate of a tracked clan — landing it right back in the "demoted mid-season with partial
data" bucket, as a brand-new instance, not a leftover. Nothing re-evaluates `track_war_updates`
for it afterward unless its league itself changes (a real promotion/demotion event), since
`_sync_group_track_war_updates()` only acts on a league-based mismatch.
Expected to keep recurring every cycle, for the rest of any in-progress season, until fixed.
**Fixed 2026-08-09**: `_sync_group_track_war_updates()` (`qapbot/cache_manager.py`) now also
force-promotes `track_war_updates → True` whenever a non-subscribed clan is currently `False`,
still correctly below Master III (no league mismatch, so the demotion-transition branch never
ran), but `clan_has_cwl_data_for_season()` is true for the season being processed — reusing the
same guard helper (now extracted into `_clan_has_cwl_data_for_season_safe()`) proactively instead
of only at demotion time. This still only fires when this clan's group is next resolved via
`get_league_group()` (Layer 1/2, see docstring above) — not immediately when
`CWL-GROUP-EXPAND` archives the row — so a regrown clan can sit un-healed for up to that long, but
is no longer permanently stuck requiring a manual `repromote_mid_season_clans.py` re-run.

---

## Open Questions / Decisions Needed

1. **Backfill**: ✅ Resolved — backfill the current season. Previous rounds'
   war_tags remain present in the leaguegroup response for the entire CWL season,
   so no extra API calls are needed. Each time a CWL war finalizes, Layer 2 calls
   `GET /currentwar/leaguegroup`, which returns all revealed war_tags for rounds
   1–N. The single backfill UPDATE then populates `round_number` for all prior
   `war_summary` rows in one pass — no explicit per-round loop needed.

2. **Private warlog path**: ✅ Decided — piggyback `_process_league_group_response()`
   onto the existing `_find_active_cwl_war_for_clan` call (Layer 1). No separate
   dedicated call needed unless the clan has no organic traffic.

3. **`opponent_tag` resolution**: ✅ Not needed. `war_summary` already stores
   `war_tag`, so `cwl_league_rounds.war_tag = war_summary.war_tag` is the
   direct join key. No extra API call required.

4. **Round 7 visibility**: ✅ Not an issue. CWL seasons never end early, so
   all 7 rounds always complete and the finalization fallback always delivers
   round data. Additionally, the league group endpoint returns war_tags for
   both the currently `inWar` round **and** the next round already in
   `preparation`. So when round 6 is ongoing, round 7's war_tags are already
   visible — meaning the Layer 2 fallback call at round-6 finalization covers
   round 7 as well. No dangling `#0` rows at season end.

5. **Multiple tracked clans in same group**: ✅ Handled — `_process_league_group_response`
   upserts rows for all 8 clans regardless of which one triggered the call.

---

## Implementation Checklist

- [x] ~~Q1 (backfill scope)~~ — resolved: backfill current season; prior war_tags present all season, no extra API calls needed
- [x] ~~Q2~~ — resolved: piggyback onto `_find_active_cwl_war_for_clan` (Layer 1)
- [x] ~~Q3 (opponent_tag resolution)~~ — resolved: `war_summary.war_tag` is the direct join key, no extra API call
- [x] ~~Q4 (round 7 / incomplete seasons)~~ — resolved: non-issue
- [x] ~~Q5 (multiple clans in same group)~~ — resolved: all 8 clans populated per call
- [x] **Clean up legacy DB migration code in `db_manager.py` before adding new schema:**
  - Remove `_ws_migrations` list (5 `ALTER TABLE war_summary ADD COLUMN` statements —
    all columns already in the `CREATE TABLE IF NOT EXISTS` definition; fail silently every startup)
  - Remove `_legacy_indexes` DROP list (already absent on all current DBs — instant no-ops every startup)
- [x] Write DB migration: `CREATE TABLE IF NOT EXISTS cwl_league_rounds` +
      `CREATE TABLE IF NOT EXISTS cwl_league_groups` +
      `CREATE INDEX IF NOT EXISTS idx_cwl_league_groups_id` +
      `ALTER TABLE war_summary ADD COLUMN round_number`
- [x] Implement `CacheManager._make_league_group_id(clan_tags, cwl_season)` (SHA-256 of sorted tags, 16 hex chars)
- [x] Implement `CacheManager._process_league_group_response(lg, season)` method in `cache_manager.py`:
  - called directly from `get_league_group()` on every fresh API response (no callback/hook needed)
- [x] Hook into war finalization path: resolve round via Layer 2 *before* the `war_summary` INSERT so `round_number` is written on first write (no deferred UPDATE)
- [x] Update `analyze_cwl_rounds.py` to use DB round numbers instead of positional ranking (script rewritten to read `round_number` / `cwl_league_rounds` throughout; no positional-ranking code remains — see changelog.txt "Rewrote: analyze_cwl_rounds.py — DB-backed round numbers, UNKNOWN row, 4-part filename support")
- [x] No separate backfill script needed — the Layer 2 backfill UPDATE covers all prior rounds automatically on first finalization per group
- [x] Tests (1317 passed)

### Phase: Prod deployment

- [x] Take a prod DB backup before deploying (`BackupProd.bat`)
- [x] Deploy to server-machine — new tables and index are created automatically on first bot startup via `CREATE TABLE IF NOT EXISTS`
- [x] `cwl_league_groups`: 616 rows written for 2026-04 season (Layer 1 fired during active April CWL polling). ✅
- [x] `cwl_league_rounds`: **Empty for May 2026** — code deployed on May 9 (last day of May CWL). April groups were already `ended` (all warTags `#0`), May groups fired too late. **June 2026 will be the first season with full round data.** No bug — this is an expected first-season gap. Confirmed: June and July 2026 both show full `cwl_league_rounds`/`cwl_league_groups` data.
- [x] `war_summary.round_number`: NULL for all May 2026 rows — direct consequence of empty `cwl_league_rounds`. Expected. Confirmed populated (100% coverage on CWL rows) from June 2026 onward.
- [x] Prod data recovery confirmed: 2026-05-08 rows went from 18,652 → 20,785 after running `recover_missing_wars_20260509.py` and one bot cycle. ✅
- [x] No binding errors, crashes, or data divergence after fix deployment. ✅
- [x] Run `analyze_cwl_rounds.py` against prod data for June 2026 season; verify round numbers are correct — confirmed: `cwl_league_rounds`/`cwl_league_groups` are populated for 2026-06 and 2026-07, and `war_summary.round_number` coverage is 100% for CWL wars in both seasons (verified directly against `data/qapbot.db`)
- [x] Monitor June 2026 CWL — confirm Layer 1 captures groups at season start and Layer 2 fills gaps — confirmed via the same DB check above; no gap remained for June or July 2026

### Phase: Post-approval cleanup (after one season confirmed working)

- [x] Remove the `_ws_migrations` list and `_legacy_indexes` DROP list from `db_manager.py` — confirmed removed; no references remain in `qapbot/db_manager.py`
- [x] Update `analyze_cwl_rounds.py` to remove the positional-ranking fallback once DB round numbers are fully reliable — confirmed: no positional/chronological-ranking fallback code remains in the script
- [x] Update this plan doc status from `Draft` to `Implemented`
