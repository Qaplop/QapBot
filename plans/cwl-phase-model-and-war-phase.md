# CWL Phase Model, War Phase & Roster Freeze — Implementation Plan

Continuation of `CWL_ROSTER_PLANNING_PLAN.md` Phases 5+6 (shipped 2026-08-29, commit `c1e18d4`).
Project owner's spec, 2026-08-30, refining that work into a real four-phase model with a visible
step indicator, automatic war-phase detection, per-clan roster freezing, and batched update DMs.

## Confirmed decisions (project owner, 2026-08-30)

- **Phase vocabulary: `Setup → Enrollment → Preparation → War`.** The phase-3 button reads
  **"Announce Rosters"**, and **"Send Roster Updates"** once there are pending changes. Chosen over
  keeping "Start Preparation" for the button because the phase name should say *where you are* while
  the button says *what it does*. Step 1 is "Setup", not "Preparation", deliberately — two of four
  steps sharing a word defeats the point of a step indicator.
- **"Start CWL" is retired as a name entirely** — the real CWL start is a manual in-game action only
  a clan lead can take; the bot never starts anything. The old `start_cwl()` is renamed
  `announce_cwl_rosters()` to match, rather than left as a stale name (Pitfall risk: a name that
  lies about what a function does is how the next person builds the wrong thing on top of it).
- **A no-show dropped at lock time is DMed automatically.** They already received "move to X" plus
  two escalating alarms; leaving them silent means they turn up expecting to play. Automatic (not
  batched into the admin's update button) because the lock itself is automatic — there is no admin
  action to attach it to, and waiting for one could leave them uninformed for hours.

## Findings that changed the spec

### 1. An ephemeral on Activity close is impossible — it becomes a DM

Requirement 4b asked for an ephemeral when the admin closes the board with unsent updates. An
ephemeral requires a live interaction token; `discordSdk.close()` is a client-side call with no
interaction behind it, and the original `LAUNCH_ACTIVITY` both consumed its one response slot and
expires after 15 minutes.

`POST /api/cwl/activity-closed` already fires on every close (`main.ts`'s `closeActivity()`,
added 2026-08-16 for the stuck-launch-button fix), so the **trigger point already exists** — the
handler just gains a branch that DMs the closing admin a short message carrying a
"Send Roster Updates" button. Same outcome, delivered as a DM.

### 2. A `war` status must not blind the Phase 6 sweep

`get_announced_cwl_events_sync()` queries `status = 'announced'`. Adding `'war'` as a fifth status
without widening that query would stop switch monitoring at exactly the moment the no-show
reconciliation matters most. Renamed `get_active_cwl_events_sync()` and widened to
`IN ('announced', 'war')`.

### 3. Per-clan war state already exists — reuse `locked_at`

`cwl_event_clans.locked_at` is already written by `check_cwl_roster_switches()` and already means
"this clan's CWL roster locked in-game" (primary: an `is_cwl` `war_summary` row; fallback: a
`cwl_league_groups` row). Every freeze guard hangs off that column. No parallel concept.

### 4. `notified` (bool) cannot answer "does this player need an update DM?"

That question is *"is where they're assigned now different from what we last told them?"* — which
needs the clan we last announced, not a flag. Two additions:

- `cwl_assignments.notified_clan_tag TEXT` (+ the `cwl_shared_clan_players` counterpart), written
  only by the announcement sender.
- `cwl_dropped_notified_players` — a tombstone row per notified player who gets **unassigned**,
  because unassigning deletes the `cwl_assignments` row and the fact would otherwise vanish
  entirely. Cleared when they are re-assigned or once the removal DM goes out.

Pending updates are then *derived*, never queued: a player is pending iff
`assigned_clan_tag != notified_clan_tag` (moved), `notified_clan_tag IS NULL` (never told), or a
tombstone exists (dropped). A player dragged A→B→A therefore self-cancels with no DM — which is
exactly the avalanche protection the spec asks for, obtained structurally rather than by debouncing.

### 5. The eligible-member set must be snapshotted at lock time

The exception rule ("as long as the player was a member when the clan started, they can still
participate") cannot be answered later from `user_players.current_clan_tag` — that is live data and
drifts as people leave. `CACHE.get_league_group(clan_tag)` (`cache_manager.py:3323`, an existing
Cardinal-Rule-9 wrapper) returns the authoritative locked roster; it is called **once per clan per
season** at lock detection and persisted to a new `cwl_locked_clan_members` table. Falls back to
live membership if the call fails, so detection never blocks on the API.

Keyed by `(cwl_season, clan_tag)`, not by event — a roster lock is a real-world fact about the clan,
shared by every guild that configured it, exactly like `cwl_shared_clans`.

### 6. Coordinators are not permission holders

Verified: `_resolve_admin_or_leader()` gates on guild-admin or the Leader/Co-Leader Discord roles;
`cwl_clan_coordinators` grants no access at all. The 30-minute reminder therefore may reach someone
who cannot act on what it says, so it is written as an informational status report.

### 7. Gaps filled beyond the spec

- Deleting a season is **blocked** once any participating clan has locked — the in-game CWL is
  already running against that roster.
- The step indicator shows `War (2/8 started)` rather than a bare "War", since a guild reaches war
  phase when the *first* clan starts while the rest are still in Preparation.

---

## Slice 1 — Phase model, rename, step indicator

- `cwl_events.status` gains `'war'`. Full machine: `draft → signup_open → announced → war`
  (+ `cancelled`). `'finalized'` stays retired.
- `resolve_cwl_phase(event, clans) -> CwlPhase` (`QBdiscocmdshelper_cwl.py`) — the one place phase is
  derived, returning index (1-4), i18n label key, and the `(started, total)` clan counts for phase 4.
- Step indicator rendered at the top of `format_clan_management_cwl_management()`'s embed
  description — `✅ done · 🔵 **current** · ⚪ future`, joined by `──`. Shared content layer, so it
  appears identically in `/clan management` and the Hub.
- Renames (old name on the left; all applied 2026-08-30): `start_cwl()` → `announce_cwl_rosters()`,
  `has_cwl_assignments_to_start()` → `has_cwl_roster_announcements_pending()`,
  `resolve_cwl_start_targets_sync()` → `resolve_cwl_announcement_targets_sync()`,
  `send_cwl_start_dm_group()` → `send_cwl_roster_dm_group()`,
  `CwlStartCwlConfirmView` → `CwlAnnounceRostersConfirmView`, i18n `cwl.management.start_cwl_*` →
  `cwl.management.announce_rosters_*`, and every matching `custom_id`.

## Slice 2 — War transition, eligible snapshot, no-show reconciliation

- New `cwl_locked_clan_members(cwl_season, clan_tag, player_tag)`, `UNIQUE(cwl_season, clan_tag, player_tag)`.
- On lock detection in `check_cwl_roster_switches()`: snapshot the eligible set, then reconcile every
  assignment for that clan — assigned-but-not-eligible players are unassigned into the pool and DMed
  the no-show message once (deduped via the existing `alarm_stage_sent`, bumped past the last stage).
- First clan locking flips the event `announced → war`.

## Slice 3 — Freeze guards

- `assign_cwl_player_sync()` refuses any placement touching a locked clan, **except** placing a
  player who is in that clan's `cwl_locked_clan_members` into that same clan. Server-side is the real
  guard; the board is the convenience layer.
- `handle_post_clan_config` refuses to deactivate or re-configure a locked clan.
- `enrollmentBoard.ts`: a locked clan's column is drop-disabled and its cards drag-disabled, with a
  lock badge; eligible unassigned players remain draggable into it.
- Season deletion blocked while any participating clan is locked.

## Slice 4 — Batched update DMs (spec item 4)

- `notified_clan_tag` + `cwl_dropped_notified_players` (finding 4).
- `resolve_cwl_pending_roster_updates_sync()` → `{moved, dropped, new}`.
- Three triggers, one implementation: an "Send Roster Updates" button beside Close in the board; a
  DM-with-button from the `activity-closed` hook; the Hub button switching label and turning
  highlighted (`ButtonStyle.danger`) while anything is pending.

## Slice 5 — Clan add/remove during Preparation (spec item 5)

- Removing a participating clan moves its players to the pool and marks them dropped (pending a DM).
- Adding one runs the existing auto-assign, then classifies each resulting player:
  - already announced elsewhere → **moved** DM
  - enrolled but never announced → **new** DM
  - never contacted at all this season → a **combined** DM: the enrollment confirm/opt-out buttons
    *and* the assignment details in one message. Deliberately not two DMs — in Preparation phase,
    asking "do you want to play?" and immediately following it with "you're assigned to X, move
    there" is confusing and doubles the volume. One message answers both questions.

## Slice 6 — Roster completeness check (spec item 6)

Before announcing, compare each participating clan's assigned count against its `roster_size`; if any
is short, the confirm dialog names them and asks whether to proceed anyway.

## Slice 7 — 30-minute coordinator reminder (spec item 7)

New `cwl_event_clans.coordinator_reminder_sent_at`. When a clan's `cwl_start_at` is within 30
minutes and it is not yet locked, DM its coordinators a status report: roster filled vs open slots,
and which assigned players are still not in the clan.

---

## Test plan

Per slice, in `tests/integration/test_cwl_start_and_switch.py` and a new
`tests/integration/test_cwl_war_phase.py`: phase derivation for all five statuses and the mixed-clan
war case; the A→B→A self-cancelling pending computation; freeze guards accepting an eligible player
and refusing everyone else; the no-show reconciliation and its one-shot DM; the combined late-add
DM; the completeness check; and the 30-minute reminder firing once with correct roster status.
