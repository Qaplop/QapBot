# Tracker #0038 — "Remind Pending" CWL signup reminder

## Context

Tracker item #0038 (reporter `_its.lucas`) asks for a button that re-DMs everyone who hasn't
responded to their CWL sign-up invitation yet, ideally as fast as possible to get responses in.
While investigating, the user (project owner) noticed the CWL-Verwaltung overview's
"Ausstehend: 66" didn't match the much smaller number of ❓ pending cards visible in the
Teams-verwalten board for the same season, and asked for that to be explained and fixed.

Root cause (confirmed against the dev DB, event 18 / season 2026-09 / guild
`1224425088913248467`): `get_cwl_signup_status_counts_sync()` (`db_manager.py:4334`) counts every
`cwl_signups` row with `status='pending'`, regardless of whether the account currently has a
linked Discord user. The Teams-verwalten board (`activity/client/src/enrollmentBoard.ts:955-981`)
only shows the ❓ icon when a pending player *also* has a live Discord link — an unlinked pending
player shows "Not Linked" instead, by deliberate design (the two icons are mutually exclusive).
Of the 66 pending rows, 35 have no live link → 66 vs. 31 is exactly the gap observed. This is a
genuine, real discrepancy, not a bug in either individual query — just two different unreconciled
definitions of "pending" shown side by side. The user has confirmed the fix: split the one
"Ausstehend" line in the season overview into two — pending (linked, reachable) and unlinked
(pending, no Discord account, unreachable) — so the number an admin sees always matches what a
reminder/DM action can actually do.

The two pieces of work are naturally paired: the reminder feature can only ever reach the "linked"
half of "pending", so fixing the counter split and building the reminder button use the exact same
underlying resolution logic.

The user also specified a distinct DM shape for reminders, different from the existing one-DM-per-
account template: a verbose, personally-addressed intro DM ("Hallo **{display_name}**, ...")
explaining what to do and why, followed by a **separate** DM containing one confirm/decline button
pair **per pending CoC account** for that Discord user (not one DM per account) — and the old,
now-superseded invitation DM(s) must be retracted first.

## Existing infrastructure being reused

- `resolve_cwl_pool_dm_targets_sync()` / `_send_cwl_enrollment_dm_batch()` / `send_cwl_signup_template_dm()`
  (`qapbot/QBdiscocmdshelper_cwl.py`) — the existing DM pool-resolution + batch-send machinery.
  Not reusable as-is for this feature (its global dm_sent dedup would skip everyone already
  DMed — precisely the pending players we want to re-reach — and it sends one DM per account, not
  one combined DM per Discord user), but its sub-parts (retraction, dedup-clearing, DM-guard,
  live-link re-check) are the exact building blocks to reuse.
- `_reset_and_resend_enrollment_dm()` (`qapbot/web_bridge.py:1517`) — the existing single-player
  "retract old DM → clear dm_sent dedup → resend" sequence (used by the board's right-click
  "reset to pending" action). This reminder feature applies the same 3-step sequence, batched
  across many accounts and grouped by Discord user.
- `_retract_enrollment_dms_for_tags()` / `cleanup_stale_cwl_enrollment_dms()`
  (`qapbot/QBdiscocmdshelper_cwl.py:2813`, `:2848`) — best-effort DM deletion by tag list, reused
  unmodified for retracting old invitation DMs before sending reminders.
- `CwlNotifyNewMembersConfirmView` / `_make_cwl_management_notify_new_members_callback()`
  (`qapbot/ui_cwl_roster.py:1133-1256`) — the confirm/cancel dialog pattern (double-click guard:
  disable buttons + `edit_message()` before the long-running send) this feature's own confirm
  dialog will mirror exactly, including the row-4 "omit entirely rather than disable" button-gating
  convention used for "Notify New Pool Members" itself.
- `CwlSignupResponseButton` / `build_cwl_signup_response_view()` (`qapbot/ui_cwl_roster.py:1294-1442`)
  — the existing restart-safe `DynamicItem` confirm/opt-out button. Its response-recording logic
  (ownership checks, DB write, cross-guild propagation, board version bump) will be extracted into
  a shared helper so the new combined-message button reuses it exactly rather than duplicating it.
  The button's own final step (`interaction.response.edit_message(content=..., view=None)`) is
  **not** reused as-is — see Design note below.
- `CACHE.send_user_dm_detailed()` (`qapbot/cache_manager.py:1347`) for the actual DM sends, and
  `CACHE.user_accounts[discord_id]["display_name"]` for the personalized greeting name (the same
  source `war_notifications.py`'s `greeting = t('...display_name=display_name)` pattern uses).

## Design note: why the combined multi-account DM needs a new button class

`CwlSignupResponseButton.callback()` finishes by replacing the *entire* message: `interaction.
response.edit_message(content=<single-account confirmation text>, view=None)`. That's correct and
safe for every existing caller (one account = one DM = one view), but wrong for a message holding
buttons for *several* accounts at once — clicking one account's button would wipe the whole
message, including the still-pending buttons for the Discord user's other accounts.

Fix: a new `CwlReminderResponseButton` `DynamicItem` (own `custom_id` template,
`cwl:remind:(confirm|optout):{event_id}:{player_tag}`, so it can never collide with or interfere
with the original button) that, on success, **re-derives the live group from the DB** rather than
trying to parse/edit the existing message's component tree — it fetches this event's `cwl_signups`
rows still `status='pending'`, resolves each one's live Discord link, filters to the ones owned by
this same Discord user, and rebuilds the message content + button view from that fresh query. If
other accounts in the group are still pending, the message is re-rendered with just their buttons;
once none remain, the message becomes a plain "all done" confirmation with `view=None`. This
mirrors the codebase's established "re-derive from live state rather than trust a stored/prior
snapshot" convention (see Pitfall 24 in copilot-instructions.md) instead of introducing a new,
riskier technique (`discord.ui.View.from_message` component-tree surgery) to track group
membership.

Both button classes share one extracted helper, `_apply_cwl_signup_response()` (validation +
DB write + `propagate_cwl_player_response` + `bump_enrollment_version`, factored out of
`CwlSignupResponseButton.callback()`'s body), so the ownership/ ownership-transfer/cross-guild
propagation rules stay identical between the original and reminder buttons — no behavior fork.

Component-count safety: Discord caps a message at 5 action rows. One row per account (2 buttons:
confirm + decline) means up to 5 accounts per message; a Discord user with more than 5 pending
accounts in one event (unlikely, but not impossible for a big family) gets their reminder buttons
split across multiple sequential DMs of ≤5 accounts each, each tracked/retractable independently.

## Implementation

### 1. Season-overview counter split (`qapbot/QBdiscocmdshelper_cwl.py`, `qapbot/translations/*.json`)

- Add `split_cwl_pending_signups_by_link_sync(event_id) -> Tuple[int, int]` near
  `has_cwl_pool_members_missing_dm` (`QBdiscocmdshelper_cwl.py:546`): reads
  `db.get_cwl_signups_for_event_sync(event_id)`, filters `status == 'pending'`, resolves the tags
  in bulk via `db.get_player_links_sync()`, returns `(pending_linked, pending_unlinked)`. Plain
  sync function per Pitfall 26 — caller wraps in one `asyncio.to_thread()`.
- In `format_clan_management_cwl_management()` (`QBdiscocmdshelper_cwl.py:464-473`), replace the
  single `signup_status_pending` line with two lines built from the new split, keeping
  `confirmed`/`declined`/`withdrawn` unchanged.
- New i18n key `cwl.management.signup_status_unlinked` (de: "Nicht verknüpft (ausstehend)"; en:
  matching English) in both `qapbot/translations/de.json` (next to `signup_status_pending` at
  line 933) and `en.json`.

### 2. Pool resolution + grouped DM send (`qapbot/QBdiscocmdshelper_cwl.py`)

- `resolve_cwl_pending_reminder_targets_sync(guild_id, event_id, season) -> Dict[str, Any]`:
  pending `cwl_signups` rows for the event, live-link-resolved and opt-out-filtered (same
  `cwl_permanent_optout` honouring as `resolve_cwl_pool_dm_targets_sync`), grouped by
  `discord_id` → `{"groups": {discord_id: [{"player_tag","player_name"}, ...]}, "skipped_unlinked": int, "skipped_optout": int}`.
- Extract `_dm_guard_blocks(discord_id: str) -> bool` from the inline check inside
  `_send_cwl_enrollment_dm_batch` (`QBdiscocmdshelper_cwl.py:2686-2691`) — small, identical-logic
  refactor, used by both the existing batch function and the new one.
- `send_cwl_reminder_dm_group(event_id, guild_id, season, discord_id, accounts) -> Dict[str, Any]`:
  for one Discord user's account list — sends the intro DM (`cwl.reminder.dm_intro_body`, no
  view), then one or more buttons DMs (chunks of ≤5 accounts, `build_cwl_reminder_response_view`),
  and calls `db.mark_cwl_player_dm_sent_sync(...)` per account against its chunk's message id.
  Returns per-group outcome (`contacted`/`blocked`/`no_mutual_guild`/`failed`), same vocabulary as
  `_send_cwl_enrollment_dm_batch`'s result so the summary UI can reuse the existing i18n lines
  (`start_enrollment_dm_blocked_line` etc.).
- `has_cwl_pending_signups_to_remind(guild_id, season) -> bool`: button-gating check mirroring
  `has_cwl_pool_members_missing_dm`'s shape — true iff resolution above yields ≥1 group.

### 3. Orchestrator (`qapbot/web_bridge.py`)

- `remind_pending_cwl_players(guild_id, season) -> Dict[str, Any]`, placed next to
  `notify_new_cwl_pool_members` (`web_bridge.py:725`): resolves the event (bail on
  `no_database`/`no_event`/`not_open`, same error keys as the sibling action), resolves targets,
  then per group: retract old DMs (`_retract_enrollment_dms_for_tags`), clear each account's
  `dm_sent` dedup (`db.clear_cwl_player_dm_sent_sync`), send via `send_cwl_reminder_dm_group`.
  Aggregates into the same result shape `CwlNotifyNewMembersConfirmView` already knows how to
  render (`contacted`, `skipped_dm_guard`, `skipped_unlinked`, `blocked`, `no_mutual_guild`,
  `failed`), plus `contacted_users` (distinct Discord users reached) for the summary text.

### 4. Discord UI (`qapbot/ui_cwl_roster.py`)

- New row-4 button "Ausstehende erinnern" next to "Neue Pool-Mitglieder benachrichtigen"
  (`ui_cwl_roster.py:391-402`), gated by `has_cwl_pending_signups_to_remind` (omit entirely when
  false, matching the sibling button's own convention).
- `_make_cwl_management_remind_pending_callback()` + `CwlRemindPendingConfirmView` — copy of
  `CwlNotifyNewMembersConfirmView`'s shape (double-click guard, processing/summary messages,
  `_refresh_parent` + `bump_enrollment_version` on completion), calling
  `web_bridge.remind_pending_cwl_players`.
- `_apply_cwl_signup_response(db, event, signup, live_discord_id, user_id_str, action)` extracted
  from `CwlSignupResponseButton.callback()` (`ui_cwl_roster.py:1338-1427`): does the ownership
  checks, `upsert_cwl_signup_sync`, `propagate_cwl_player_response`, `bump_enrollment_version`, and
  returns an outcome code (`ok`/`no_longer_valid`/`not_your_signup`/`signup_closed`) plus
  `player_name`/`guild_id`. `CwlSignupResponseButton.callback()` is rewired to call it and keep its
  existing single-message `edit_message(view=None)` finish exactly as today.
- `build_cwl_reminder_response_view(event_id, accounts, guild_id)` + `CwlReminderResponseButton`
  (`DynamicItem`, template `cwl:remind:(confirm|optout):{event_id}:{player_tag}`): calls the shared
  helper above; on `ok`, re-derives the live remaining-pending group (per Design note) and either
  re-renders the trimmed multi-account view or finalizes with `view=None`; on any other outcome,
  reuses the existing `cwl.template.no_longer_valid`/`not_your_signup`/`signup_closed` i18n keys as
  an ephemeral `interaction.response.send_message(..., ephemeral=True)`, leaving the shared message
  untouched.
- Register the new dynamic item in `QapBot.py` (next to the existing
  `QBcore.bot.add_dynamic_items(CwlSignupResponseButton)` at line 2858).

### 5. i18n (`qapbot/translations/de.json` + `en.json`)

New keys under `cwl.management` (mirroring the `notify_new_members_*` family at
`de.json:967-974`): `button_remind_pending`, `button_confirm_remind_pending`,
`remind_pending_confirm_body`, `remind_pending_processing`, `remind_pending_summary`,
`remind_pending_error_no_database`, `remind_pending_error_no_event`,
`remind_pending_error_not_open`. New `cwl.reminder` section: `dm_intro_body` (verbose, personal,
explains why + asks for a fast response), `dm_buttons_intro`, `confirm_button_labeled` /
`optout_button_labeled` (both take `{player_name}`), `all_done_msg`. Ownership-error strings reuse
the existing `cwl.template.*` keys (no duplication).

### 6. Docs (Cardinal Rule 15)

Add a "Remind Pending CWL Players" section to
`qapbot/docs/REGISTRATION_MESSAGE_WORKFLOWS.md`, next to the existing "CWL Enrollment DM Re-Route"
section (`:898`), documenting: the grouped-by-Discord-user DM shape, why a second button class was
needed instead of reusing `CwlSignupResponseButton`, and the live-re-derivation approach for the
combined message instead of message-component surgery. Update `changelog.txt` per the standing
convention.

## Verification

- `.\run_tests.ps1` — full suite must stay green; add unit coverage for
  `split_cwl_pending_signups_by_link_sync`, `resolve_cwl_pending_reminder_targets_sync` (grouping +
  opt-out/unlinked filtering), and `_apply_cwl_signup_response`'s outcome codes.
- Manual DEV test (no user confirmation needed for DEV per standing project convention): open CWL
  Management for a season with several pending, linked signups (some accounts sharing one Discord
  user), click "Ausstehende erinnern", confirm, and check: old invitation DM(s) gone, a new
  personalized intro DM arrives, a second DM with one button pair per pending account arrives,
  clicking one account's button only resolves that account (siblings' buttons still work), and the
  season-overview embed now shows split Pending/Unlinked counts matching Teams-verwalten's ❓ tally.
- Post a manual test case via `tracker_add_testcases` before/alongside marking #0038 implemented
  (Cardinal Rule 15) — do not mark it implemented until this has actually been exercised in DEV,
  since it sends real Discord DMs.
