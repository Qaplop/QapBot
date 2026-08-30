# CWL Roster Planning

**Status: shipped, in production use.** QapBot owns the whole CWL roster workflow end to end —
pick participating clans → open sign-ups → drag-and-drop assignment on a Discord Activity board →
announce the roster with a DM to every player → automatic detection of when CWL actually starts
in-game → per-clan roster freeze → switch-verification alarms. This document describes the
**current implementation**, not its history. For the day-by-day design evolution (superseded
approaches, live-testing incidents, why particular bugs happened), see `changelog.txt` and
`.github/copilot-instructions.md`'s Pitfalls Cookbook — this file is reference documentation for
what exists today, kept current per Cardinal Rule 15 ("a stale doc is a bug").

Two companion plans carry implementation detail this document summarizes rather than repeats:
- `plans/implemented/cwl-personal-hub.md` — the player-facing preferences surface (`/cwl
  preferences`), fully shipped.
- `plans/cwl-phase-model-and-war-phase.md` — the four-phase model, war detection, roster freeze,
  and batched update DMs (2026-08-30), fully shipped; kept separately for its slice-by-slice
  build record.

---

## 1. Data model

All CWL tables live in the **hot DB only** (`qapbot/db_manager.py`'s schema-creation block,
alongside `clans`/`clan_families`) — no history-DB mirroring. This is short-lived per-season
operational data; deletion (via a configurable retention policy, currently the one unfinished
piece — see §11) is the intended lifecycle mechanism, not archival.

### `cwl_events` — one row per guild × season
```
id, guild_id, cwl_season, status, signup_deadline_at, template_season,
created_by_discord_id, created_at, updated_at
UNIQUE (guild_id, cwl_season)
```
`status` is the guild-level phase driver: `draft → signup_open → announced → war` (+`cancelled`).
There is no `finalized` status — an earlier design had one, but the native-Discord "Finalize"
screen that would have set it was superseded by the Activity board (§6), so it was retired rather
than left dead. Every transition has exactly one writer (see §3).

### `cwl_event_clans` — participating clans for one event
```
id, event_id, clan_tag, target_league_rank, roster_size, tier_order, cwl_start_at,
participating, locked_at, coordinator_reminder_sent_at, created_at
UNIQUE (event_id, clan_tag)
```
- `participating` — unchecking a clan must never delete its row (that would lose
  `roster_size`/`cwl_start_at`/`target_league_rank` on every toggle), so participation is this
  explicit column, never "row exists."
- `cwl_start_at` — per **clan**, not per event: clans in the same family start CWL at different
  times, since starting is a manual in-game action.
- `locked_at` — set once, never cleared, the moment this clan's CWL is observed to have actually
  started in-game (§10). Every freeze guard and the War-phase indicator key off this column alone.
- `coordinator_reminder_sent_at` — one-shot dedup for the 30-minutes-before roster status report
  to this clan's coordinators (§10).

### `cwl_signups` — one row per player per event
```
id, event_id, player_tag, player_name, dmed_discord_id, preferred_league_rank, source, status,
responded_at, created_at, origin_shared_clan_id
UNIQUE (event_id, player_tag)
```
- `dmed_discord_id` — who this event's enrollment DM went to. A **historical fact**, not an
  ownership field — never read as "who owns this account" (an account can be re-linked after the
  DM was sent). Live ownership always comes from `user_players` via `get_player_links_sync()`.
- `status` — `pending | confirmed | declined | auto_confirmed` (`withdrawn` is legacy, no longer
  written). `auto_confirmed` is seeded by a standing opt-in preference (§9 of
  `cwl-personal-hub.md`) and is distinguished from a genuine response everywhere it's read.
- `origin_shared_clan_id` — tags a local row created when a foreign shared-clan member is
  cross-assigned into one of this guild's own private clans, so it can be purged if the real
  owning guild later reassigns that player (see §7's cross-guild section).

### `cwl_assignments` — the player → clan placement for one event
```
id, event_id, player_tag, assigned_clan_tag, suggested_clan_tag, assignment_source, score,
score_breakdown_json, locked, notified, notified_clan_tag, switched_at, alarm_stage_sent,
updated_at
UNIQUE (event_id, player_tag)
```
"Unassigned" = no row (the column is `NOT NULL`), never a nullable value. `suggested_clan_tag`,
`score`, `score_breakdown_json` are **dead columns** — an earlier design had an algorithmic
scoring engine write them; it was superseded by human drag-and-drop (§7) and never built.
- `notified` / `notified_clan_tag` — written together as one fact ("we told them they play for
  X"), exclusively by the roster-announcement sender (§8). This pairing is what makes a later
  move *detectable*: "pending update" is derived by comparing `assigned_clan_tag` against
  `notified_clan_tag`, not by queuing anything (§9).
- `switched_at` / `alarm_stage_sent` — War-phase switch-verification state (§10).

### Cross-guild shared clans

A real-world clan can only actually play CWL for one roster, but nothing stops two independent
guilds each configuring the same `clan_tag` for the same season. Three dedicated tables
reconcile that, deliberately **not** by redirecting `cwl_signups`/`cwl_assignments`/
`cwl_event_clans` themselves (those stay scoped to one guild's whole event — redirecting
per-player inside them would mean every write path branches on "which clan is this," and a bug
there could leak a different guild's private roster data):

**`cwl_shared_clans`** — `id, clan_tag, cwl_season, owner_guild_id, owner_event_id,
owner_resolution_method, owner_resolved_at, created_at, updated_at`, `UNIQUE(clan_tag,
cwl_season)`. Ownership resolves by whoever holds the clan's real in-game Leader/Co-Leader rank
(verified beats unverified, Leader beats Co-Leader), falling back to whichever guild claimed it
first if nobody resolvable is found.

**`cwl_shared_clan_guilds`** — membership list, one row per attached guild.

**`cwl_shared_clan_players`** — the shared roster itself, `UNIQUE(shared_clan_id, player_tag)`,
visible identically from every attached guild's board. Carries the same
`notified`/`notified_clan_tag`/`switched_at`/`alarm_stage_sent` columns as `cwl_assignments`, plus:
```
status, assigned, dmed_discord_id, source, added_by_guild_id, responded_at, updated_at
```
`status` and `assigned` are **structurally independent columns**, each with its own single-purpose
writer whose `UPDATE` clause cannot mention the other. The original design conflated them ("one
clan per row, so `status='confirmed'` IS the assignment") — which meant every auto-assigned
placement had to fake a confirmation just to achieve placement, silently destroying whatever the
player's real response actually was. `status` (the player's genuine response) is written only by
`set_cwl_shared_clan_player_status_sync`; `assigned` (are they placed in *this* clan's column) only
by `set_cwl_shared_clan_player_assignment_sync`. This split is documented as a general lesson in
`.github/copilot-instructions.md`'s pitfall list: never let two logically-different facts share one
column just to avoid a second one.

### `cwl_locked_clan_members` — the roster a clan actually locked with

```
id, cwl_season, clan_tag, player_tag, player_name, source, created_at
UNIQUE (cwl_season, clan_tag, player_tag)
```
Snapshotted **once**, the instant a clan's lock is first detected (§10), because it cannot be
reconstructed afterward — live clan membership drifts as people leave. `source` is
`league_group` (the authoritative CoC league-group roster) or `live_membership` (fallback if that
call fails). Keyed by `(cwl_season, clan_tag)`, not by event — a lock is a real-world fact about
the clan, shared by every guild that configured it, same model as `cwl_shared_clans`. This is the
one permitted exception to the roster freeze: a player in this set may still be placed into the
locked clan even during War phase, because doing so records reality rather than changing it.

### `cwl_dropped_notified_players` — tombstone for a removed, already-announced player

```
id, event_id, player_tag, player_name, notified_clan_tag, created_at
UNIQUE (event_id, player_tag)
```
Every other "pending update" case is derivable by comparing live state against
`notified_clan_tag` — but unassigning a player **deletes** their `cwl_assignments` row, so the
fact that they're owed a "you're no longer on the roster" DM would vanish with it. This table is
that fact's only home. Cleared when the player is re-assigned (nothing to apologize for) or once
the removal DM actually goes out.

### `cwl_player_season_status` — global, cross-guild player truth

```
id, player_tag, cwl_season, player_name, dmed_discord_id, dm_sent, dm_sent_at,
dm_sent_via_event_id, dm_sent_via_guild_id, dm_sent_via_message_id, dm_sent_via_channel_id,
status, responded_at, responded_via_event_id, responded_via_guild_id, created_at, updated_at
UNIQUE (player_tag, cwl_season)
```
One row per real-world player per season, independent of which guild(s) currently pool them —
`cwl_signups.status`/`cwl_shared_clan_players.status` are read-optimized **mirrors** of this, not
independent truths. Two facts, two exclusive writers, same non-conflation discipline as above:
`dm_sent` (has *any* guild already invited this player this season — survives a guest being
removed and re-added) written only by `mark_cwl_player_dm_sent_sync`; `status`/`responded_at` (the
player's genuine response, fanned out to every guild's local mirror by
`propagate_cwl_player_response()`) written only by `set_cwl_player_response_status_sync`.

### `cwl_clan_coordinators` — standing per-clan leadership

```
id, guild_id, clan_tag, discord_user_id
UNIQUE (guild_id, clan_tag, discord_user_id)
```
Not season-scoped — carries forward automatically every CWL month. A distinct concept from
war-notification custodians (a different existing feature): coordinators exist specifically for
CWL roster-fill/switch reporting (§10) and, since 2026-08-30, for the third permission tier (§2).

### Extensions to existing tables

`user_players` gains four standing, per-CoC-account preferences (not per Discord user — a member
with several accounts may want different defaults per account):
```
cwl_permanent_optout, cwl_default_preferred_league_rank, cwl_permanent_optin,
cwl_optout_send_dm_anyway
```
Full semantics in `plans/implemented/cwl-personal-hub.md`.

`guild_config` gains two anchored-message triplets (channel/message/enabled/last-bump, mirroring
the registration message's own tracking) — one for the admin-facing **CWL Management Hub**, one
for the player-facing **Personal CWL Hub** (`cwl_management_*` / `cwl_player_hub_*`) — plus
`cwl_retention_months`, `cwl_selected_season`, and `cwl_enrollment_include_all_linked_accounts`
(§7's account-wide pool toggle).

---

## 2. Permission tiers

Three tiers, checked independently on both the Discord side and the web bridge (defense in depth
— the bridge never trusts a Discord-side gate alone):

1. **Guild admin** (Administrator permission or the configured bot admin) — every CWL action.
2. **Leader / Co-Leader** — whoever currently holds the guild's `coc_role_leader_id` /
   `coc_role_coleader_id` Discord role (the existing CoC-rank auto-role feature, one shared pair
   per guild). Grants the Manage Teams board only, not the settings/season-management actions.
3. **CWL Coordinator** (added 2026-08-30) — a standing coordinator (`cwl_clan_coordinators`) of
   **any clan currently participating in the guild's selected season**. Also grants the Manage
   Teams board, scoped the same way tier 2 is. Deliberately season-scoped even though the
   coordinator config itself is standing: a coordinator of a clan sitting out this season has no
   business on this season's board. Resolved by `is_cwl_coordinator_for_current_season()`
   (`QBdiscocmdshelper_cwl.py`).

Implementation: `check_admin_permissions()` / `check_admin_or_leader_permission()`
(`QBdiscocmdshelper.py`) on the Discord side, `_resolve_admin()` / `_resolve_admin_or_leader()`
(`web_bridge.py`) on the bridge side — the latter independently re-derives every tier rather than
trusting the Discord-side check that gated the original button click.

---

## 3. Phase model

Four phases — **Setup → Enrollment → Preparation → War** — derived in exactly one place,
`resolve_cwl_phase(event, clans)` (`QBdiscocmdshelper_cwl.py`), so the step indicator, every
button's gating, and the freeze guards can never disagree about where a guild is:

| Phase | `cwl_events.status` | Entered by |
| --- | --- | --- |
| 1 Setup | `draft` | "Add New Season" |
| 2 Enrollment | `signup_open` | "Start Enrollment" |
| 3 Preparation | `announced` | "Announce Rosters" (§8) |
| 4 War | `war` (or *any* participating clan `locked_at`) | Automatic, first clan lock (§10) |

Phase 4 is derived from **either** signal deliberately: the sweep that flips `status` to `'war'`
runs once per cycle, so there's a window where a clan is demonstrably locked before the event row
catches up — the indicator must show reality, not lag behind it.

A step indicator renders at the top of the CWL Management embed (shared by both entry points,
§4) — `✅ done  ──  🔵 **current**  ──  ⚪ upcoming`, four steps joined by connectors. The War
step carries its clan count, `War (2/8 started)`, because a guild reaches War the moment its
*first* clan starts while the rest may still be in Preparation — a bare "War" would misleadingly
imply the whole family is running.

---

## 4. Admin surfaces

**Two entry points, one shared implementation.** Beyond the `cwl_settings`/`cwl_management` modes
inside the existing `/clan management` command's mode dropdown (`ClanManagementView`,
`ui_clan_management.py`), there's a dedicated, permanently-anchored **CWL Management Hub**
message — its own configurable channel, own tracked message, reposted like the registration
message — because season prep is a recurring, multi-day workflow that benefits from persistent
visibility. The embed-building and button-adding logic (`format_clan_management_cwl_settings()` /
`format_clan_management_cwl_management()` / `add_cwl_management_components()`,
`QBdiscocmdshelper_cwl.py` + `ui_cwl_roster.py`) is written **once** and called from both shells —
this is the standing "shared content layer" invariant the feature has held since Phase 1, and
every later addition (the step indicator, the "Still Missing" section, coordinator access) went
through it rather than being special-cased per shell.

`cwl_settings` (rarely touched) holds: the two Hub channels, retention months, the CWL Coordinators
manager, the account-wide-pool-expansion toggle.

`cwl_management` (the season-by-season operational screen) holds the season select, and a row of
action buttons whose visibility/label is entirely phase-driven:

- **Configure Participating Clans** — always available regardless of phase (clans change plans
  last-minute even after enrollment starts); opens the Activity board's clan-config screen.
- **Manage CWL Coordinators** — standing config, never gated on season status.
- **Start Enrollment / Manage Assignment** — one dynamically-labeled button in the same slot:
  "Start Enrollment" while `draft`, "Manage Assignment" (opens the Activity's Teams Management
  board) from `signup_open` onward.
- **Delete Season** — disabled once any participating clan has locked (§10) — an in-game CWL is
  running against that roster.
- **Add New Season** — shown only when the current CWL month has no event yet (omitted entirely,
  not disabled, once it would be a no-op for the rest of the month).
- **Notify New Pool Members** / **Remind Pending** / **Announce Rosters** (or **Send Roster
  Updates** once something is pending) — row 4, each shown only when it would actually do
  something; see §6, §6, §8/§9 respectively.

---

## 5. Setup phase

**Season creation** ("Add New Season") defaults to carrying over the previous season's
`cwl_event_clans` — but "participating" is derived from **real war history** (which family clans
actually have an `is_cwl=1` war for the prior season), not from whatever the admin last happened
to toggle. A guild with no prior CWL history at all (or whose prior season was also empty)
auto-enables every family clan instead of handing the admin a blank slate. A clan newly added to
the family (no prior row) infers its `target_league_rank` from its most recent
`cwl_league_groups.league_rank`.

**Configure Participating Clans** is a Discord Activity screen (`activity/client`,
`CWL_CLAN_CONFIG_ACTIVITY_PLAN.md`) — a table of the guild's clan family plus any guest clans,
each row editable for tier (CoC-defined, read-only), roster size (5/15/30 preset), start time, and
the participating checkbox. **Guests**: an admin can search and add a clan tracked *anywhere* by
the bot (not just this guild's family) or an individual player, via one unified fuzzy search
(`_search_cwl_guests()`, `web_bridge.py`) supporting `@name`/`#tag` prefixes. A guest clan becomes
a full board column with its whole live roster; a guest player is a plain `cwl_signups` row
(`source='guest_invite'`) with no membership requirement. Adding a clan already claimed by another
guild triggers the cross-guild sharing flow above rather than creating a second independent
record.

**Coordinators** are managed per clan via a Discord select (`CWL_COORDINATOR_LIMIT`-capped),
independent of season status.

A **locked clan's row renders read-only** and any submitted change to it is silently reverted
server-side (`handle_post_clan_config`, `web_bridge.py`) — the game, not the admin, owns that
roster now (§10).

---

## 6. Enrollment phase

**Start Enrollment** is one confirmed action (`start_cwl_enrollment()`,
`QBdiscocmdshelper_cwl.py`) that does three things at once: seeds `cwl_signups` from the
**current live membership** of every guild-family clan (not last season's CWL attackers — a clan
new to CWL would otherwise seed zero signups despite having real members), runs an initial
auto-assignment seed (see §7), and sends the confirm/opt-out DM blast — because from the admin's
perspective this is one decision, not three.

**Signup DM**: a `discord.ui.DynamicItem`-based button pair (`CwlSignupResponseButton`,
`ui_cwl_roster.py`) — the first `add_dynamic_items()` registration in the codebase, chosen
specifically because a static `add_view()` view can't carry per-invocation data (which
player/event) without an extra lookup table, and this button must survive a bot restart
mid-signup-window without going dead. The callback re-reads live state on every click rather than
trusting closure state.

**Global cross-guild dedup**: a player pooled by two guilds (a shared clan, or an account-wide
expansion) gets exactly **one** enrollment DM for the whole season, tracked in
`cwl_player_season_status` — never one per guild. Their response fans out to every guild's local
mirror the instant it changes.

**Notify New Pool Members** re-runs the same pool resolution and DM-send machinery for anyone who
joined the pool *after* Start Enrollment already ran, sending to exactly
`resolve_cwl_pool_tags_missing_dm_sync()`'s set — the same function the season overview's "New
players without DM invitation" line counts, so the number shown and the number a click actually
reaches can never drift apart (tracker #0079's fix, 2026-08-30: they briefly could, when an
earlier settled-status exclusion was added to only one of the two).

**Remind Pending** (tracker #0038) DMs everyone still genuinely pending — its own resolver,
`resolve_cwl_pending_reminder_targets_sync()`, since unlike the "missing DM" set above it must
find people who *were* already DMed and haven't answered — grouped one message per Discord user
(not per account): a verbose personal intro plus a combined confirm/decline button set, after
retracting their stale original DM. The season overview's own "Ausstehend" line
(`split_cwl_pending_signups_by_link_sync()`) counts this same population — a pending, linked
signup counts only once its DM has actually gone out, matching exactly which players the board
renders with a ❓ icon (tracker #0078's fix, 2026-08-30).

**Player self-service** is entirely `/cwl preferences` (the Player CWL Hub Activity screen,
`plans/implemented/cwl-personal-hub.md`) plus the DM buttons — there is no standalone `/cwl
signup`/`/cwl withdraw` slash command; an earlier design specified them but the player-facing
surface was redesigned around the Hub before they were built.

**DM guard**: `CONFIG.cwl_dm_restrict_to_admin` (`_dm_guard_blocks()`) — hardcoded `False` on
PROD, defaults `True` on DEV with a PROD-tester allowlist. Every CWL bulk-DM sender in the
codebase calls this.

---

## 7. Manage Teams board

A Discord Activity (`activity/client/src/enrollmentBoard.ts`) — participating clans as columns, an
Unassigned pool, and an "Assigned to other Guild" pseudo-column for a placement that belongs to
another guild's event. This entirely replaced an original design for a native-Discord
skill/reliability-scored suggestion engine plus a separate override screen; the project owner
chose a human-driven drag-and-drop board instead. `skill_score` (league-weighted average
stars/attack, trailing 3 CWL months) exists purely as a **display/sort** field — it has never fed
an assignment decision.

**Initial auto-assignment**: each candidate player's own single most recent *real* CWL attack
(`attack_order > 0`, any clan they've ever played for) determines their suggested clan, if that
clan is participating this season. The candidate pool is **every current member of every guild
family clan** (not just participating ones) plus, if the guild's `cwl_enrollment_include_all_
linked_accounts` toggle is on, every other CoC account linked to the same Discord user. Every
placement — automatic or drag-and-drop — routes through one function,
`assign_cwl_player_sync()`, specifically so a race between an automatic guess and a real human
placement can't silently double-book a player: a `deliberate=False` (automatic) call never
overrides an existing claim, deliberate or not.

**Cross-guild conflict guards**: before writing a placement, `assign_cwl_player_sync()` purges the
player from every *other* shared clan they're currently placed in, and refuses a deliberate
placement that would double-book them into a **different** guild's private (non-shared) roster
for the same season — that conflict has to be resolved in the other guild's own board.

**Right-click menu** on a card offers admin-settable enrollment status
(Confirmed/Declined/Pending) — the last-action-wins rule runs both directions: an admin's later
override beats an earlier player DM response, and vice versa, since both paths write through the
same global `cwl_player_season_status` update function.

**Live polling** (`GET /api/cwl/enrollment/wait`, a long-poll bumped by any write) keeps the board
current across every open session without a fixed interval poll.

**Card coloring**: green when a player's live `current_clan_tag` already equals their
`assigned_clan_tag`, amber when it doesn't, no color when their current clan is unknown or
they're unassigned — this exact computation is reused verbatim by the roster-announcement DM
(§8) and the switch-monitoring sweep (§10), so the board, the DM and the alarms can never
disagree about the same fact.

---

## 8. Preparation phase — Announce Rosters

**"Announce Rosters"** (`announce_cwl_rosters()`, `QBdiscocmdshelper_cwl.py`) DMs every assigned
player where they play and when, tailored to the board's own green/amber split:

- **Green**: "you're already in **X**, CWL starts `<t:…:F>` (`<t:…:R>`)."
- **Amber**: "you're in **Y**, move to **X** *well before* `<t:…:F>` — here's the link
  (`coc_clan_profile_url()`)." An unknown current clan renders amber, deliberately — amber's text
  stays true regardless of where they actually are; green would assert something unverifiable.

Grouped **one DM per Discord user**, never per account, so a main and an alt in two different
clans starting at different times arrive as one coherent message. A shared clan is announced only
by its **owner** guild, to avoid two guilds double-announcing the same player.

Refuses outright — no partial send — if any participating clan still has no `cwl_start_at`,
naming the offending clans. The confirm dialog separately warns (not blocks) if any clan's roster
is short of its configured size, and asks whether to proceed anyway.

Marks each successful send `notified` + `notified_clan_tag`, which is what makes the action
idempotent and the button self-managing: it disappears once everyone announced is up to date, and
reappears the moment a late arrival is assigned. This is also the first and only writer of the
`signup_open → announced` transition.

---

## 9. Batched roster-update DMs

Changing the board *after* players were told where they play does not risk a DM per drag.
"Pending" is **derived**, never queued: a player is pending iff `assigned_clan_tag !=
notified_clan_tag` (moved), `notified_clan_tag IS NULL` (never told — a late add), or a
`cwl_dropped_notified_players` tombstone exists (removed entirely). A player dragged A→B→A
therefore self-cancels and owes nobody a DM — the avalanche protection is structural, not a
debounce timer.

`resolve_cwl_pending_roster_updates_sync()` computes the three buckets; `send_cwl_roster_updates()`
sends them, grouped per Discord user like every other CWL batch. A player whose clan was removed
entirely from the season, or who was **never** contacted at all this season (their clan joined
late), gets special handling: the latter receives **one** combined DM carrying both the roster
information *and* the confirm/opt-out buttons — never a separate enrollment DM followed by a
separate assignment DM.

**Three triggers, one implementation:**
1. A "Send Roster Updates" button on the board's own footer, beside Close.
2. A DM to whoever closes the board with updates still outstanding — the spec asked for an
   ephemeral here, which is impossible (no live interaction token survives `discordSdk.close()`);
   delivered as a DM instead, through the `POST /api/cwl/activity-closed` hook that already fires
   on every close.
3. The CWL Management Hub's own button, relabeled "Send Roster Updates" and highlighted
   (`ButtonStyle.danger`) whenever anything is pending — the same slot "Announce Rosters" occupied
   before Preparation began.

Removing a participating clan mid-Preparation tombstones its already-announced players (routed
into the same batch); adding one runs the auto-assignment seed against it, producing "moved" or
"new" entries depending on whether those players were already announced elsewhere this season.

---

## 10. War phase

**Detection costs zero extra CoC API calls.** A once-per-cycle sweep,
`check_cwl_roster_switches()` (hooked in next to `sweep_cwl_ended_flags()`, the established
pattern for cheap fleet-wide CWL housekeeping), watches for a clan's CWL roster locking:

- **Primary signal**: an `is_cwl=1` `war_summary` row for the clan's current season — read off
  the mandatory current-war fetch every actively-polled clan already gets.
- **Fallback**: a `cwl_league_groups` row now existing for `(season, clan_tag)` — covers
  private-warlog clans the primary signal can't observe directly, populated opportunistically by
  the existing CWL fallback path.

The **first** trigger to fire for a clan stamps `cwl_event_clans.locked_at` — write-once, the
single source of truth every consumer reads. An earlier design proposed force-promoting every
participating clan into the every-cycle fetch bucket while monitoring; that was **not built** —
it would recreate exactly the load pattern two prior fixes (tracker #0009's RSS plateau, #0017's
redundant mid-month fetches) removed, and buys nothing over the existing ~30-minute membership
staleness across a multi-*day* window.

**On lock**, two things happen immediately:
1. `snapshot_cwl_locked_clan_roster()` records who was actually in the clan — via
   `CACHE.get_league_group()` (authoritative), falling back to live membership — into
   `cwl_locked_clan_members` (write-once, see §1).
2. `reconcile_cwl_locked_clan_roster()` drops every assigned-but-ineligible player back to the
   unassigned pool and DMs them once, automatically, that they missed the roster — they already
   received the move instruction and two escalating alarms, so silence would leave them expecting
   to play.

The guild's own `cwl_events.status` flips to `war` the moment its *first* clan locks.

**Roster freeze**, enforced in `assign_cwl_player_sync()` — the single write path every placement
goes through — refuses any move into or out of a locked clan, **except** placing a player who was
actually in it at lock time (`cwl_locked_clan_members`). The board mirrors this: a locked column
shows a 🔒 badge, its cards are non-draggable, and it refuses drops except from an eligible
player. Season deletion is blocked entirely once any participating clan has locked.

**Switch-verification alarms**: for anyone assigned-but-not-yet-switched in a still-open clan, an
escalating DM fires at **T-24h** then **T-2h** before their clan's `cwl_start_at`
(`_due_cwl_alarm_stage()` always jumps to the most urgent still-due stage, so a bot outage across a
threshold never sends a now-pointless early nudge). A stage already sent is never re-sent, even
across many sweep cycles inside one window.

**Leadership**: a live-queried "Still Missing" section in the CWL Management embed (both entry
points, same shared content layer) lists who hasn't switched per clan, or "roster locked" once it
has. Separately, **30 minutes before** a clan's start, its coordinators get one consolidated
status DM — roster fill (X/roster_size) and who specifically hasn't transferred yet — deliberately
just the one message rather than an earlier design's *second* T-2h escalation, which said nearly
the same thing 90 minutes apart; players still get both of their own alarms, so no lead time is
lost.

---

## 11. Cross-cutting

**i18n**: `qapbot/translations/{en,de}.json`, top-level `cwl.*` namespace — `settings`,
`management`, `player_hub`, `activity`, `setup`, `template`, `phase`, `start`, `update`, `alarm`,
`reminder`. Every user-facing string in this feature goes through `t()`.

**Retention — the one unfinished piece.** `guild_config.cwl_retention_months` and its settings UI
exist and persist, but `nightly_db_maintenance()` has no CWL purge step — the setting is currently
**inert**. `announced`/`war` now have real writers (since 2026-08-29/30), so building the purge
(delete `cwl_events` in a terminal status older than the configured months, cascading through the
child tables) is the natural next piece of work on this feature.

**Docs**: this file. No separate `qapbot/docs/CWL_ROSTER_PLANNING.md` was ever written — this
remains the single design record, alongside the two companion plans listed at the top.

---

## 12. DM interaction foundation (Phase 0)

Generic bot infrastructure this feature needed and built first, not CWL-specific: a guild-context
resolver for DM-invoked commands (`resolve_guild_context()`, `QBdiscocmdshelper.py`), a DM
guild-picker for a caller linked into more than one guild, and an `on_message` DM handler —
fulfilling a long-standing backlog item. Shipped and stable; documented in
`qapbot/docs/CODE_STRUCTURE.md`, not repeated here.

---

## Verification

`.\run_tests.ps1` after any change — CWL-specific suites: `tests/unit/test_db_manager_cwl_roster.py`,
`tests/discord/test_ui_cwl_roster.py`, `tests/discord/test_web_bridge.py`,
`tests/integration/test_cwl_start_enrollment.py`, `tests/integration/test_cwl_start_and_switch.py`,
`tests/integration/test_cwl_war_phase.py`, `tests/unit/test_cwl_clan_ownership.py`,
`tests/unit/test_db_manager_cwl_shared_clans.py`, plus the Player Hub's own suite (see its plan).
`activity/client` and `activity/server` each have `npm run build` / `tsc --noEmit` as their
verification (no frontend test framework).

**Not yet verified live**: the 2026-08-30 phase-model/war-phase/batched-DM work
(`plans/cwl-phase-model-and-war-phase.md`) — needs a bot restart and a real click-through of
Preparation → War transition, the roster freeze, and all three update-DM triggers.
