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
- [ ] Update `analyze_cwl_rounds.py` to use DB round numbers instead of positional ranking
- [x] No separate backfill script needed — the Layer 2 backfill UPDATE covers all prior rounds automatically on first finalization per group
- [x] Tests (1317 passed)

### Phase: Prod deployment

- [x] Take a prod DB backup before deploying (`BackupProd.bat`)
- [x] Deploy to server-machine — new tables and index are created automatically on first bot startup via `CREATE TABLE IF NOT EXISTS`
- [x] `cwl_league_groups`: 616 rows written for 2026-04 season (Layer 1 fired during active April CWL polling). ✅
- [x] `cwl_league_rounds`: **Empty for May 2026** — code deployed on May 9 (last day of May CWL). April groups were already `ended` (all warTags `#0`), May groups fired too late. **June 2026 will be the first season with full round data.** No bug — this is an expected first-season gap.
- [x] `war_summary.round_number`: NULL for all May 2026 rows — direct consequence of empty `cwl_league_rounds`. Expected. Will populate from June 2026 onward.
- [x] Prod data recovery confirmed: 2026-05-08 rows went from 18,652 → 20,785 after running `recover_missing_wars_20260509.py` and one bot cycle. ✅
- [x] No binding errors, crashes, or data divergence after fix deployment. ✅
- [ ] Run `analyze_cwl_rounds.py` against prod data for June 2026 season; verify round numbers are correct
- [ ] Monitor June 2026 CWL — confirm Layer 1 captures groups at season start and Layer 2 fills gaps

### Phase: Post-approval cleanup (after one season confirmed working)

- [ ] Remove the `_ws_migrations` list and `_legacy_indexes` DROP list from `db_manager.py` (legacy cleanup deferred until feature is stable)
- [ ] Update `analyze_cwl_rounds.py` to remove the positional-ranking fallback once DB round numbers are fully reliable
- [x] Update this plan doc status from `Draft` to `Implemented`
