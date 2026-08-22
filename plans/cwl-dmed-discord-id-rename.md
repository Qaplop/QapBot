# Implementation plan: rename the CWL snapshot identity column + seed status from the global table

## Context

Follow-up to the 2026-08-22 snapshot-staleness work
(`plans/implemented/cwl-board-stale-link-and-whois-timeout.md`). That fixed every reader and
writer to resolve ownership live, which removed the symptom. This addresses the *cause* the
project owner identified: duplicating identity state across tables invites drift by construction.

Two independent work items, both scoped from live evidence.

---

## Item 1 — `discord_id` → `dmed_discord_id` in the three CWL snapshot tables

### Why (verified)

`discord_id` is copied out of `user_players` into **three** CWL tables: `cwl_signups`,
`cwl_shared_clan_players`, `cwl_player_season_status`. Account ownership is a **global,
always-current** fact with exactly one authority, so nothing about it is event-scoped.

The column earns nothing as an ownership field: **no query anywhere filters by it.**
`idx_cwl_signups_discord` (`db_manager.py:2145`) is an unused index — the column is only written,
carried along, and read back.

The real problem is that it **conflates two different facts**:

| fact | scope | authority |
|---|---|---|
| "who owns this account" | global, always current | `user_players` — never copy it |
| "who we sent the enrollment DM to" | event/season-scoped, historical | legitimately lives here |

That conflation is exactly why `CwlSignupResponseButton`'s guard had to accept **both** the
snapshot owner and the live owner: one column was being asked to be two things.

Renaming makes the column mean only the second thing. It also keeps the one case where the value
exists nowhere else — a never-linked guest tag added via search, which has no `user_players` row
at all but does have a Discord user we would DM.

### Scope

**Schema** (`db_manager.py`):
- `CREATE TABLE` for all three tables → `dmed_discord_id`.
- Migration: `ALTER TABLE <t> RENAME COLUMN discord_id TO dmed_discord_id`, guarded by a
  column-exists check so it is idempotent (Cardinal Rule 12). SQLite has supported
  `RENAME COLUMN` since 3.25; PROD is well past that (local build 3.50).
- Drop `idx_cwl_signups_discord` rather than renaming it — verified unused, and re-creating an
  index nothing queries would just re-add cost.

**CRUD** (`db_manager.py`) — parameter names, SQL, and returned dict keys all move to
`dmed_discord_id`, so call sites read the honest name:
`upsert_cwl_signup_sync` (4693), `get_cwl_signups_for_event_sync` (4739), `get_cwl_signup_sync`
(4765), `get_cwl_player_season_status_sync` (4896), `get_cwl_player_season_status_bulk_sync`
(4968), `mark_cwl_player_dm_sent_sync` (4999), `set_cwl_player_response_status_sync` (5051),
`get_cwl_shared_clan_players_sync` (5648), `set_cwl_shared_clan_player_status_sync` (5708),
`set_cwl_shared_clan_player_assignment_sync` (5766).

**Callers**: `QBdiscocmdshelper_cwl.py`, `web_bridge.py`, `ui_cwl_roster.py`.

**Deliberately NOT renamed** — these are real ownership/actor fields, not snapshots:
`user_players.discord_id`, `users.discord_id`, `cwl_events.created_by_discord_id`, and the
`discord_user_id` request parameters on the bridge. Renaming those would be actively wrong.

**Frontend**: `activity/client/src/` reads `player.discord_id` from the board payload
(`enrollmentBoard.ts:794`, `:144`, `:1051`, `:1063`). The payload key stays **`discord_id`** —
that field is now genuinely resolved live from `user_players` on every read, so it *is* ownership
and the name is correct there. Only the DB column changes. No frontend change, no redeploy.

### Tests

- Migration is idempotent: run `initialize_database()` twice against a DB created with the old
  column name; assert the rename happens once, data survives, and the second run is a no-op.
- A fresh DB gets `dmed_discord_id` directly and no `discord_id` column on those three tables.
- The board payload still exposes `discord_id` (frontend contract unchanged) while the DB column
  is `dmed_discord_id` — guards against renaming the API surface by accident.
- Existing suites for signups / shared clans / season status keep passing with renamed keys.

---

## Item 2 — four signup-creation paths ignore the global response

### Why (verified)

`cwl_player_season_status` is the declared global source of truth for a player's response
(rule h). `start_cwl_enrollment` correctly seeds new signup rows from it
(`QBdiscocmdshelper_cwl.py:2315-2330`), so a guild pooling a player who already answered
elsewhere doesn't contradict them.

Four other creation paths hardcode `status="pending"` instead:

| site | path |
|---|---|
| `QBdiscocmdshelper_cwl.py:1214` | cross-guild "orphaned elsewhere" mirror |
| `QBdiscocmdshelper_cwl.py:1282` | drag-and-drop placement (`_resolve_identity`) |
| `QBdiscocmdshelper_cwl.py:1498` | `auto_seeded` pool seed |
| `web_bridge.py:1586` | guest invite |

(A fifth, `:930`, is already correct — it carries the shared roster's real status.)

**Status: real in code, latent in data.** Checked the live DB: **0** rows currently have a local
`pending` against a non-`pending` global status. 31 of 116 global rows do hold a real response, so
the precondition exists — it just needs one of those players to be pooled afterwards by one of the
four paths above. Fixing it now is cheap; it is not firefighting.

### Fix

Seed from the global table at each of the four sites, mirroring what Start Enrollment already
does. `get_cwl_player_season_status_sync(player_tag, season)` is the single-player reader; the
bulk variant already exists for the seeding loop at `:1498`. Where the season isn't already in
scope, take it from the event row.

Prefer a small shared helper over four copies, so the "a new local row must never contradict a
response the player already gave" rule has one home — the same consolidation `_live_owners_or_sync`
got for ownership.

### Tests

Per path: a player with a global `confirmed`/`declined` gets that status on the newly created
local row, not `pending`; a player with no global row still gets `pending`; and an **existing**
local row is never overwritten by this seeding (all four sites are inside
`if get_cwl_signup_sync(...) is None`, and that must stay true).

---

## Sequencing

Item 2 first — small, self-contained, no schema risk, and it closes a real (if latent) correctness
gap. Then Item 1, which is mechanical but wide.

Each its own commit + `changelog.txt` entry + tests. `.\run_tests.ps1` must stay green after each;
report the real pass count.

## Verification

- `.\run_tests.ps1` after each step.
- For Item 1, re-run the schema check against a copy of `data/qapbot.db` to confirm the migration
  applies cleanly to real data and is a no-op on the second run — never against the file itself.
- Re-run the Item 2 drift query afterwards; it must still return 0 rows:
  ```sql
  SELECT COUNT(*) FROM cwl_signups s
  JOIN cwl_events e ON e.id = s.event_id
  JOIN cwl_player_season_status g ON g.player_tag = s.player_tag AND g.cwl_season = e.cwl_season
  WHERE s.status = 'pending' AND g.status != 'pending';
  ```

## Docs

Update `DATABASE_ARCHITECTURE.md` with the schema change and the rule the rename encodes:
**a snapshot table may record what happened (who we DMed, what they answered), never who
currently owns an account.** Update Pitfall 37 to note the column is now named for the one fact it
legitimately holds.
