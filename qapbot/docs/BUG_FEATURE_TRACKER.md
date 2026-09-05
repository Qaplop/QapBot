# Bug/Feature Tracker

Implements `BUG_FEATURE_TRACKER_PLAN.md` (the full design doc — read it for the "why" behind
every decision below; this doc is the day-to-day reference for working on the shipped code).

## Architecture at a glance

```
/bug, /feature (QBdiscordcmds.py)
        │
        ▼
qapbot/ui_tracker.py        ── all Discord UI + item lifecycle logic
        │
        ▼
CACHE.db_manager (db_manager.py)   ── bot_settings / tracker_items / tracker_attachments /
        │                              tracker_testcases (WarHistoryDB._create_tracker_schema())
        │
        ▼
qapbot/web_bridge.py  /api/tracker/*   ── HTTP surface for the agent side, X-Bridge-Secret gated
        │
        ▼
qapbot/mcp/tracker_mcp.py   ── stdio MCP server, 11 tools, used by Copilot Chat / Claude Code
```

Only PROD ever owns the tracker: `CONFIG.tracker_enabled` is hard-coded to `not is_dev_mode`
in `qapbot/config.py`'s `load_config()` — there is no env var, and DEV can never register
`/bug`/`/feature` in any configuration. This is deliberate: PROD's DB (including
`bot_settings`' tracker channel IDs) is routinely copied to DEV for realistic-data testing, so
an independently-configurable toggle would let DEV post real-looking items into PROD's actual
channels. There is currently no way to live-test the Discord-side tracker commands on DEV —
use the unit/discord/integration test suites instead.

**This invariant has to be enforced at every entry point individually — slash-command
registration alone doesn't cover everything** (2026-08-22 live bug report: DEV reacted to a 👍
on a PROD-authored test-case message, marked environments passed in its own PROD-backup-seeded
copy of that tracker item, then failed with a 403 editing a message it doesn't own, and still
posted its own "mark item done too?" prompt into the thread). The reason slash-command gating
alone isn't enough: a raw gateway event like `on_raw_reaction_add` fires for **every bot present
in a channel**, regardless of which bot's message was reacted to — unlike a component
interaction (button click), which Discord delivers only to the application that owns the
message. So "DEV's copy of a DynamicItem button is safe because DEV never receives the
interaction" does NOT generalize to reaction listeners, where DEV very much does receive the
event and, since its DB is a PROD copy, finds a real matching row to act on.
`handle_tracker_test_reaction()` now checks `CONFIG.tracker_enabled` explicitly as its first
line for exactly this reason — any *future* raw event listener (reactions, message edits, etc.)
touching tracker state needs the same explicit check; do not rely on "DEV won't have a matching
row" or "DEV won't receive this event" without verifying which kind of Discord event it actually
is.

## Data model

Four tables, all created in `WarHistoryDB._create_tracker_schema()` (called from
`initialize_database()`), hot-only (no `history` mirror — Cardinal Rule 1 parity doesn't
apply, but Rule 14 named-column access still does):

- `bot_settings` — bot-wide key/value config (`tracker_*_channel_id`, `tracker_guild_id`,
  `tracker_enabled`). `PRIMARY KEY (guild_id, key)`, `guild_id=''` = global (the only scope
  used today). Cached on `CACHE.tracker_settings`, write-through via `CACHE.set_tracker_setting()`.
- `tracker_items` — one row per filed bug/feature. `item_number` (AUTOINCREMENT) is a single
  shared `#NNNN` pool across both `item_type` values. See `db_manager.py`'s
  `_TRACKER_ITEM_UPDATABLE_COLUMNS` whitelist for which columns `update_tracker_item(**fields)`
  is allowed to touch (never build a raw SQL identifier from caller input). `priority`
  (HIGH/MEDIUM/LOW, default MEDIUM) is set by the reporter in the modal.
- `tracker_attachments` — junction, `ON DELETE CASCADE`. `local_path` is the on-disk copy
  under `CONFIG.tracker_data_dir` (agent-readable, immune to Discord's expiring CDN URLs);
  `discord_url` is the bot's own re-upload.
- `tracker_testcases` — junction, `ON DELETE CASCADE`. `seq` orders rows within one
  `(item_number, environment)` pair. `priority` (HIGH/MEDIUM/LOW, default MEDIUM) is set
  per-row by whoever calls `tracker_add_testcases` (there is no Discord modal for composing
  test cases — see below) and shown next to each case in the posted test-case message.

There is no cap on how many open items one reporter can have — `/bug`/`/feature` always opens
the modal (removed 2026-08-22 per the project owner's request; see `BUG_FEATURE_TRACKER_PLAN.md`
for the original rationale).

Status lifecycle:

```
open ──▶ triaged ──▶ in_progress ──▶ implemented ──▶ testing ──▶ done
  └──────────────────────────────▶ rejected
  └──────────────────────────────▶ duplicate
```

`implemented → testing` happens automatically the moment `post_test_cases()` runs (Discord
button has no "compose test cases" UI — only the bridge/MCP `tracker_add_testcases` does that;
the Discord-side "Test cases" button is a jump link once one exists). `❌ Failed` reverts
`testing → in_progress`, keeping already-passed environments' sign-off (never resets them).

**`testing → done` is NOT automatic any more (decoupled 2026-08-22, tracker item #0015
follow-up).** Completing every environment's test cases used to force the item straight to
`done` as a side effect — by design now, the item and its test-case message are two
independent objects that only ever move on their OWN trigger:

- **The item** only reaches `done` via an explicit `apply_status_change(item_number, "done", ...)`
  call — the status dropdown, `tracker_set_status`, or someone accepting the "mark done too?"
  prompt described below. Reaching `done` never touches the test-case message.
- **The test-case message** only archives to the Done Testing channel via
  `move_testcases_to_done_channel()`/`finalize_testcases_move()` — triggered by its own
  completion (every environment with cases fully passed, detected as an edge-trigger in
  `mark_environment_passed_and_refresh()`/the 👍-reaction shortcut so it only fires once, not on
  every call against an already-fully-passed item) or by the manual `[ 📦 Move to Done ]`
  button/`tracker_move_testcases_done` MCP tool. The manual path works even with cases still
  unchecked — it asks for confirmation first (`ConfirmForceMoveView` / the bridge's
  `needs_confirmation`/`unchecked_count` response, force-able with `{"force": true}`) — and
  never silently marks the remaining cases passed; they stay visibly unchecked in the archived
  message. `_refresh_testcase_message()` (any later in-place refresh of that same message) checks
  whether `test_channel_id` already equals the configured Done Testing channel and, if so, passes
  `view=None` instead of rebuilding the interactive Pass/Fail/Move-to-Done view — mirroring
  `_refresh_item_message()`'s own `archived` handling for the item embed. Without this a later
  refresh (e.g. a redundant 👍 on an already-fully-passed message) would resurrect the buttons on
  a message that was deliberately stripped (2026-08-22 live bug report).
- Whenever a test-case set just finished (naturally or via the forced manual move),
  `get_linked_item_if_eligible_for_done()` checks whether the linked item is still open (not
  already `done`/`rejected`/`duplicate`) and, if so, offers a "mark item done too?" prompt:
  `ConfirmItemDoneView` as an ephemeral followup (Pass button, Move-to-Done button) or, for the
  👍-reaction shortcut (no interaction to attach an ephemeral to), a message with the same Yes/No
  buttons posted to the item's discussion thread, falling back to a DM to the reactor if there's
  no thread. The bridge/MCP surface never blocks on this — `tracker_mark_testcase_passed`/
  `tracker_move_testcases_done`'s response just names the linked item and status and suggests
  calling `tracker_set_status(..., "done")` if the agent wants to.
- The passive (thread/DM) prompt opens with an `<@actor_id>` mention (`item_done_confirm_prompt_
  passive`, 2026-08-22 live bug report: item #0015's prompt sat unnoticed in the thread because a
  plain, unmentioned message doesn't highlight or notify anyone in Discord). The ephemeral variant
  (`item_done_confirm_prompt`) has no mention — it's already private to the person who just
  clicked the button. There is no Discord API for a bot to move a user's client view to a
  channel/thread directly; a mention is the closest equivalent, since its notification is
  clickable and jumps straight to that exact message.

**Move-on-terminal-status (2026-08-22, item side only since the follow-up above; widened same
day to `rejected`/`duplicate`)**: the moment any path sets `status` to `done`, `rejected`, or
`duplicate` — the status dropdown, the bridge/MCP `tracker_set_status` tool, or accepting the
"mark done too?" prompt — `apply_status_change()` reposts the item's embed into the configured
**Implemented** channel, with its Edit/Add files/Status/Test cases buttons stripped (no `view=`
on the repost — nothing left to do on a closed item), and deletes the old copy from the reports
channel. All three are equally terminal: `rejected`/`duplicate` originally fell into the plain
in-place-refresh branch and lingered in the working reports channel indefinitely (live bug found
via tracker #0010's own closure) — an item that will never move again needs to leave the
unfinished-workload channel regardless of *why* it closed. The Implemented channel is optional —
unconfigured means the move is skipped and the item stays where it is. Discord threads can't
move channels, so the reposted item embed adds a "Discussion thread" jump-link field when one
exists (the old thread otherwise becomes unreachable once its parent message is deleted). See
`_move_item_to_implemented_channel()`.

**Grant/revoke requestor access (2026-08-22, ticket #0021; reply modal added 2026-09-05, ticket
#0102)**: reporters normally can't see the reports channel their item was posted in (it isn't
open to `@everyone`), so a staff `@mention` reply gets no push notification and the reporter
can't read the thread. The **"Reply to requestor"** button on `TrackerItemButton` (`grantaccess`
action, admin-only, `_handle_grant_access()`) opens `TrackerReplyModal` — a text box for an
actual reply, taken literally from the button's own name (live bug report: it used to only ever
grant access/send an invite and hand back a jump link for the admin to type into themselves,
never a compose box). The reply field is optional: submitting it blank preserves the original
grant-only behavior. On submit, `TrackerReplyModal.on_submit()` posts any reply text via
`post_comment()` — which always @-mentions the reporter (tracker item #0091) so Discord actually
notifies them the moment they can see the channel, not just a plain unmentioned message —
then gives the reporter a member-specific Discord permission overwrite on that channel
(`view_channel`/`read_message_history`/`send_messages_in_threads`) via
`_grant_or_invite_from_interaction()`/`_apply_requestor_grant()`, and finally replies (ephemeral,
to the admin) with a jump link to the item's discussion thread (or the item message itself if it
has no thread). A modal must be the interaction's very first response (Cardinal Rule 10), so
`_handle_grant_access()` itself does only the up-front admin/reporter gating and opening the
modal — none of the actual grant/invite/reply side effects, which all happen once the modal is
submitted.
This is deliberately **channel-wide**, not scoped to just their own message/thread — Discord's
narrower primitive for that would be a standalone **private thread** with `thread.add_user()`
(membership on a private thread grants access independent of the parent channel's own view
permission), but that would mean creating the discussion thread separately from the item message
instead of via `message.create_thread()`. The chosen trade-off accepts that a reporter granted
access can also see other tickets filed in the same channel, in exchange for keeping the existing
public-thread-off-the-message flow unchanged. `apply_status_change()` calls
`_revoke_requestor_access()` to undo the overwrite once the item actually moves to the
Implemented channel on a terminal status (not merely reaches one — an unconfigured Implemented
channel leaves the item, and the reporter's access, right where they are) — unless the same
reporter still has another open item sitting in that same working channel, checked via
`list_tracker_items(reporter_id=..., guild_id=...)`, in which case the overwrite is left in place
so it doesn't cut them off from that other item too.

**DM invite for reporters who aren't guild members (2026-08-22, ticket #0021 follow-up)**: most
reporters file via DM and never join the server, so `_grant_or_invite_from_interaction()`'s
member lookup usually fails. Instead of stopping there, it resolves the reporter as a plain
`discord.User` (`get_user`/`fetch_user`, same idiom `_dm_reporter_on_status_change()` already
uses) and calls `_invite_requestor_core()`, which creates a 7-day single-use invite to that
channel (`channel.create_invite()` — needs the bot to hold **Create Invite** on the tracker
channel(s), or this step fails with `grant_access_invite_failed`), DMs it to them, and sets
`tracker_items.access_grant_pending = 1` on the item as soon as the invite exists (independent of
whether the DM itself lands — a closed-DM failure still reports the invite URL back to the admin
ephemerally so it can be forwarded by hand). `apply_pending_requestor_access()`, called from
`QapBot.py`'s `on_member_join`, finishes the grant automatically once the reporter actually joins
— same overwrite `_apply_requestor_grant()` applies, for every still-open item with
`access_grant_pending` set for that member, then clears the flag and DMs a jump link. It's gated
on `CONFIG.tracker_enabled` as its first line, same reasoning as `handle_tracker_test_reaction()`
above and Pitfall 39: `on_member_join` is a raw gateway event, not a component interaction, so it
fires on every bot present in the guild — including DEV, whose DB is a routine PROD-backup copy
that can contain the exact same pending row.

**Four call sites, one grant/invite core (2026-09-05, ticket #0102)**: granting or inviting a
reporter can now be triggered four ways — the Discord button+modal
(`TrackerReplyModal.on_submit()` → `_grant_or_invite_from_interaction()`), a joining member
(`apply_pending_requestor_access()`), or an agent with no Discord identity at all
(`grant_access_for_agent()`, the bridge `/reply-and-invite` endpoint / MCP
`tracker_reply_and_invite` tool). All four ultimately call the same two side-effecting
primitives — `_apply_requestor_grant(item, channel, member)` (the permission overwrite) and
`_invite_requestor_core(item, channel, reporter_user)` (invite + DM + flag) — so none of them can
grant or invite something subtly different from the others. Only the *resolution* of
guild/channel/member differs per caller: the interaction-based callers read `interaction.guild`/
`interaction.channel`; the guild-less callers (`grant_access_for_agent()`,
`apply_pending_requestor_access()`) resolve the guild via `_tracker_home_guild_id()` (reads the
`tracker_guild_id` bot_setting) and the channel via the item's own `channel_id`.
`reply_and_invite_for_agent()` (the agent equivalent of "reply + make sure they can see it") is
`post_comment()` followed by `grant_access_for_agent()` — the same two steps
`TrackerReplyModal.on_submit()` runs, just resolved the agent's way.

**`post_comment()` always @-mentions the reporter (2026-09-05, ticket #0091)**: a duplicate-
closure comment posted via the plain `tracker_comment` bridge/MCP path showed only
`<@{author_id}>` — for an agent-driven comment, `author_id` is typically a non-numeric label
(e.g. the `X-Tracker-Admin` value, "Qaplop") rather than a real Discord snowflake, so it was
never a clickable mention at all, and the actual reporter was never notified their ticket had
moved (live bug report: "shouldn't the ticket creator have been mentioned in this post?").
`post_comment()` now prepends an `<@reporter_id>` mention unconditionally (no-op for a
non-numeric reporter_id — an agent-filed item) rather than taking an opt-in flag, since every
current call site (the plain `tracker_comment` endpoint, `reply_and_invite_for_agent()`,
`TrackerReplyModal.on_submit()`) wants it — there was no remaining case that didn't.

**`tracker_items.guild_id` must always be the tracker's home guild, never the reporting guild
(2026-09-05 live bug, ticket #0023)**: QapBot serves many Discord guilds, and `/bug`/`/feature`
are global commands — invocable from any of them, or via DM. Before this fix, `TrackerDraftView.
_on_submit()` persisted `interaction.guild.id` (or `None` from a DM) straight into the item's
`guild_id` column. That value is correct for exactly one thing — localizing the reporting
modal's own text into whichever guild's language the reporter happened to be sitting in
(`self.guild_id`, read live at report time, never stored) — and wrong for everything the stored
column is actually used for afterward: `_item_jump_link()` builds `https://discord.com/channels/
{guild_id}/{thread_id}`, and the thread only exists in the ONE guild the tracker's reports
channel lives in (`TRACKER_SETTING_GUILD_ID`, stamped by `BotSetupView._on_save()` alongside the
channel ids) — naming any other guild there is a dead link. Worse, `apply_pending_requestor_
access()`'s `list_tracker_items(reporter_id=..., guild_id=str(member.guild.id))` lookup runs
from `on_member_join`, which always fires for the tracker's home guild (that's the only guild
the DM'd invite ever points at) — a row stored with the reporter's OWN guild (or NULL, the DM
case) could never match, so the auto-grant-on-join step silently never fired. A reporter using an
alt account to test the DM-invite flow would join the server via the invite and still find the
discussion thread invisible, with no error anywhere. Fixed at both creation sites — `TrackerDraft
View._on_submit()` and `create_tracker_item_for_agent()` — to always resolve and store
`_tracker_home_guild_id()` (reads `TRACKER_SETTING_GUILD_ID`) instead of the reporting context.
`_create_tracker_schema()` also runs a one-time idempotent backfill correcting every existing
row's `guild_id` to match, so tickets filed before this fix (including #0023 itself) self-heal on
the next bot restart rather than staying permanently stuck with `access_grant_pending=1`.

## Discord surface (`qapbot/ui_tracker.py`)

**The posted RECORD is always English; the REPORTING interaction is translated
(2026-08-23, live bug report; boundary corrected 2026-08-29, project owner: "The modal should
be translated while the resulting channel message or at least its status labels should remain
english always")**: unlike the rest of the bot, the tracker item as POSTED to the tracker
channel — its embed, thread replies, comments, status-change notifications — is never localized.
The module shadows `qapbot.i18n.t` right after its import (`t = ` wrapper defined immediately
below `from qapbot.i18n import t as _t_localized`) with a version that drops any
`guild_id`/`user_id` kwargs before delegating — every remaining `t('ui_components.tracker...',
guild_id=..., user_id=...)` call site in this file keeps working unchanged, but `qapbot.i18n.t()`
then has nothing to resolve a non-default language from and falls back to English every time.
Reason: the tracker RECORD is a developer/triage tool, not end-user-facing — status labels like
"Implemented" were rendering as "Umgesetzt" or not depending on which guild's (or reporter's)
configured language happened to apply, confusing whoever's actually triaging tickets. If you add
a new tracker-RECORD UI string (embed field, thread message, status notification, comment), no
special handling is needed — just call `t(...)` as usual; the module-level shadow covers it
automatically.

The one exception, carved out 2026-08-29: `TrackerItemModal` (the `/bug`/`/feature` REPORTING
form itself — its title and every field label) and the two early-return "tracker
disabled"/"tracker not configured" messages in `start_tracker_item()` are personal/ephemeral,
seen only by the one person filing the report — the same category as any other DM/ephemeral UI
elsewhere in the bot (Cardinal Rule 6), which IS translated. Those specific call sites bypass the
module shadow and call `_t_localized(...)` directly instead (see `TrackerItemModal._localize()`,
`_set_environment_options()`, `_set_priority_options()`, and `start_tracker_item()`). If you add
a new string to the modal ITSELF (not the record it produces), use `_t_localized(...)`, not the
shadowed `t(...)` — the module-level shadow will silently force it to English otherwise, exactly
the bug this split was fixing. Everything else in this file (embeds, thread posts, comments,
status-change notifications, the draft-view preview/discard messages) stays on the shadowed
`t(...)` and stays English. This split is local to `ui_tracker.py` only — no other module renders
`ui_components.tracker.*` keys.

**Every side-effecting View callback must guard against double-clicks (2026-08-23, tracker
#0026/#0036)**: a rapid re-click landing while the first click's `defer()` (or any other
`await`) is still in flight re-runs the whole callback as a second, concurrent task — discord.py
dispatches each interaction as its own task, and none of the tracker's action functions
(`apply_status_change()` etc.) are written to tolerate being called twice for one logical click.
Session-scoped views (`TrackerDraftView`, `ConfirmItemDoneView`, `ConfirmForceMoveView`,
`TrackerStatusSelectView`) guard with `_consume_once(view, interaction)` — a flag set as the
literal first statement before any `await`, then `interaction.response.edit_message(view=view)`
with every child disabled, so the guard-set and the "buttons visibly gone" response are the same
atomic step. Persistent `DynamicItem` buttons (a fresh Python object per click, no `self` state
to flag) instead re-check persisted DB state before acting, e.g.
`TrackerItemButton._invite_requestor` checking `access_grant_pending`. See Pitfall 41,
`qapbot/docs/COPILOT_PITFALLS_COOKBOOK.md`, for the full writeup — apply the same pattern to any
new side-effecting callback added here.

- `/bug`, `/feature` (`QBdiscordcmds.py`) — thin wrappers around `start_tracker_item()`.
  Registered only when `CONFIG.tracker_enabled`; DM-invokable (no `guild_only()`).
- `TrackerItemModal` → `TrackerDraftView` (Edit/Add attachments/Submit/Discard, ephemeral,
  session-scoped) → posted item (embed + persistent `TrackerItemButton` row: Edit/Add
  files/Status/Test cases/Grant access).
- Attachments: `attachment1..3` command params are pre-downloaded as a background task
  **immediately after** `send_modal()` — the signed CDN URL on the user's own attachment
  expires while they're still filling in the modal. `Add attachments`/`Add files` instead open
  a 5-minute **upload window** (`_upload_windows`, consumed by `handle_tracker_upload_message()`,
  wired into `QapBot.py`'s existing `on_message` handler *before* its DM-fallback branch) —
  the only way to accept a pasted screenshot, since Discord modals can't contain file inputs.
- `TrackerItemButton` / `TrackerTestPassButton` / `TrackerTestFailButton` / `TrackerTestMoveDoneButton`
  are restart-safe `discord.ui.DynamicItem`s (custom_id scheme: `tracker:<action>:<item_number>`,
  `tracker:test:pass:<item_number>:<DEV|PROD>`, `tracker:test:fail:<item_number>`,
  `tracker:test:movedone:<item_number>`), registered once via `QBcore.bot.add_dynamic_items(...)`
  in `QapBot.py`'s `_setup_hook()`. Each implements `interaction_check()` gating on
  `QBcore.bot.fully_initialized` (Pitfall 20). `ConfirmItemDoneView`/`ConfirmForceMoveView` are
  short-lived, session-scoped Yes/No prompts (not restart-safe by design, same convention as
  `TrackerDraftView`) used by the decoupled done-linkage flow above.
- The 👍-reaction sign-off shortcut is a **new** `on_raw_reaction_add` listener in `QapBot.py`
  (the bot had none before) — bot-admin or configured tester only, raw (not cached) so it
  survives restarts. A redundant reaction with nothing left pending is a true no-op (returns
  before touching the message at all — see `_refresh_testcase_message()`'s archived handling
  above).

**Permission model (2026-08-23 revision)**: the Status button, the whole test-case sign-off loop
(`TrackerTestPassButton`/`TrackerTestFailButton`/`TrackerTestMoveDoneButton`,
`ConfirmItemDoneView`/`ConfirmForceMoveView`, and the 👍-reaction shortcut) all gate on
`check_bot_admin_or_tester()` (`qapbot/QBdiscocmdshelper.py`) — the bot admin **or** anyone in
`CACHE.testers` (the `/admin` → Manage Testers allowlist). This is deliberately *not*
DEV/PROD-gated, unlike the CWL enrollment DM guard's use of the same allowlist: tracker items are
tested in DEV first and PROD second by the same testers, so both environments must honor it.
Grant access (`_handle_grant_access()`) and the reporter/admin edit gate
(`_check_reporter_or_admin()`) are unchanged — still `check_bot_admin_only()` — since neither is
part of the test-case/status workflow testers were given.

**Single-use ephemeral prompts must edit themselves, not spawn a sibling (2026-08-23, live bug
report)**: `TrackerStatusSelectView._on_select`, `ConfirmItemDoneView`, and
`ConfirmForceMoveView` are one-shot dialogs — a dropdown pick or a Yes/No click is the dialog's
entire purpose, so the click must replace that same message (`interaction.response.defer()` +
`interaction.edit_original_response(..., view=None)`), never `interaction.followup.send(...)`,
which leaves the original prompt behind with its now-stale controls still visibly clickable while
a second, disconnected message appears below it (Pitfall 2,
`qapbot/docs/COPILOT_PITFALLS_COOKBOOK.md`). `ConfirmForceMoveView._on_yes` shares its result text
with the ordinary Pass-button/Move-to-Done completion path via `_build_testcases_moved_message()`
(pure text/view builder) — `_send_testcases_moved_followup()` (new message) is for those two,
which are layered on top of the *permanent* test-case list message and correctly stay a separate
toast, while `_edit_to_testcases_moved_message()` (in-place edit) is for the confirm-dialog case.
Not every "send a followup after defer" site is this bug: a followup layered on a persistent,
non-ephemeral message (the item card, the test-case checklist) is intentional and correct — only
a dialog whose sole content *is* the control being clicked must be edited away.
`CwlCarryOverPromptView._finish()` (`qapbot/ui_cwl_roster.py`) is a known, unfixable exception to
this rule: it must give `_launch_cwl_activity()` the interaction's one and only initial-response
slot, so its own ephemeral Yes/No prompt can't be edited or deleted first — documented in-place at
that call site.

**A Pass click must never be silent, even when it's a no-op (2026-08-23, live bug report)**: a
tester reported "the button did nothing" after clicking `[ ✅ DEV passed ]` — the checkbox update
in the persistent test-case message was the only feedback, easy to miss, and if the click didn't
happen to be the exact one that finished every environment (`just_completed=True` in
`mark_environment_passed_and_refresh()`), there was previously **no response at all**, not even an
acknowledgment. `TrackerTestPassButton.callback()` now sends an explicit
`testcase_pass_ack` ephemeral followup on the `just_completed=False` branch — covering both a
click that advances one environment while another is still pending, and a genuinely redundant
click on an environment that was already fully passed. `mark_environment_passed_and_refresh()`
also now logs `was_fully_passed`/`now_fully_passed` at INFO on every call, since this code path had
no logging at all before and a *genuine* miscount (a click that should have completed the item but
didn't) was previously undiagnosable after the fact.

**Agent-filed items never get a status-change DM — must no-op, not warn (2026-08-23, live bug
report)**: `create_tracker_item_for_agent()` gives an agent-filed item a non-numeric `reporter_id`
(`agent:<label>`, e.g. `agent:claude` — deliberately, so it can never collide with a real Discord
snowflake). `_dm_reporter_on_status_change()` used to fall straight into `int(reporter_id)` for
every status change, which raised on that non-numeric value and got logged as a misleading
`"Failed to DM reporter"` WARNING on every single status transition of every agent-filed item, even
though there was never a real reporter to DM in the first place. It now checks
`reporter_id.isdigit()` first and returns immediately — the same guard `_handle_grant_access()` and
`_revoke_requestor_access()` already used for the identical reason.

### Test-case message chunking (2026-08-23, tracker #0028)

`post_test_cases()`, `_refresh_testcase_message()` and `_move_test_message_to_done_testing_channel()`
all render their content through `_format_testcase_lines()` → `_chunk_lines_for_discord()` rather
than one unguarded `channel.send(content)`. Fixed a live incident: the old single-message send
raised `discord.HTTPException` (Discord's 2000-char cap) for any moderately detailed case list —
confirmed as low as 2 verbose cases (1221 chars) and routinely for a typical 8-case set (~2.4x
over) — which propagated uncaught all the way to the bridge as a bare `text/plain` 500, even
though `set_tracker_testcases()` (the DB write, earlier in the same function) had already
committed. The caller saw a failure; the cases were never actually posted. `post_comment()` had
the identical defect and the identical fix (chunking on `text.split("\n")` first, since a comment
has no test-case-style line structure of its own).

Behavior once content needs more than one message:

- The interactive view (Pass/Fail/Move-to-Done) lives on the **last** chunk only — that is also
  the id persisted as `test_message_id`, the one `get_tracker_item_by_test_message_id()` resolves
  for the 👍-reaction shortcut, regardless of which channel it's in.
- A new `test_overflow_message_ids` column (comma-joined) tracks any leading chunks.
- Edit-in-place is used **only** when the content still fits in exactly one message and there's
  no previously-tracked overflow — the common case, unchanged from before this fix (same message
  id, position, and reactions preserved). The moment a repost needs — or previously needed — more
  than one message, every previously-tracked message is deleted and the full set is reposted
  fresh, in order. This is deliberate: a newly-created overflow message would land
  chronologically *after* an existing message being edited in place, which would put the reader-
  order chunks out of sequence in the channel.
- `_move_test_message_to_done_testing_channel()` can't reuse that same delete-then-post helper
  directly, since its old and new messages live in **different channels** — it deletes the old
  ones from the source channel itself and posts fresh (view-less) chunks to the Done channel.

`web_bridge.py`'s `handle_post_tracker_comment`/`handle_post_tracker_testcases` additionally
catch `discord.HTTPException` and return a JSON `{"error": ...}` (502) instead of letting it
propagate — defense in depth for a genuine Discord-side failure (outage, permissions) that
chunking alone can't prevent, so the caller at least gets a parseable error instead of the
mimetype-confusion 500 that made the original bug hard to diagnose from the MCP side.

## Agent integration

### Bridge (`qapbot/web_bridge.py`, `/api/tracker/*`)

Reuses the existing `X-Bridge-Secret` (no second secret — accepted trade-off for a
single-admin setup: holding it grants tracker-admin *and* CWL-endpoint access).
`X-Tracker-Admin` is attribution-only (shows up as `last_edited_by`/comment author), never
authentication — it's self-asserted.

```
GET  /api/tracker/items?status=&type=&limit=
POST /api/tracker/items                {item_type, title, description, details?, environment?, priority?}
GET  /api/tracker/items/{n}
GET  /api/tracker/items/{n}/attachments/{aid}
GET  /api/tracker/items/{n}/thread?limit=
POST /api/tracker/items/{n}/status     {status, note}
POST /api/tracker/items/{n}/comment    {text}
POST /api/tracker/items/{n}/reply-and-invite  {text}
POST /api/tracker/items/{n}/testcases  {cases: [{environment, description, priority?}]}
POST /api/tracker/items/{n}/testcases/pass  {environment}
POST /api/tracker/items/{n}/testcases/fail  {}
POST /api/tracker/items/{n}/testcases/move-done  {force?}
```

All handlers delegate the actual DB+Discord work to `qapbot/ui_tracker.py`
(`create_tracker_item_for_agent()` / `apply_status_change()` / `post_comment()` /
`get_thread_messages()` / `post_test_cases()` / `mark_environment_passed_and_refresh()` /
`mark_testing_failed()` / `finalize_testcases_move()`) — there is exactly one place each of those
things happens, whether triggered from Discord or from an agent.

### MCP server (`qapbot/mcp/tracker_mcp.py`)

Stdio server, launched as `python -m qapbot.mcp.tracker_mcp` by both `.mcp.json` (Claude Code)
and `.vscode/mcp.json` (VS Code Copilot Chat). Hand-rolled JSON-RPC 2.0 — the `mcp` PyPI
package is **not** a project dependency; the wire surface needed (`initialize`, `tools/list`,
`tools/call`) is small enough that adding a new dependency wasn't worth it.

Eleven tools: `tracker_list_items`, `tracker_get_item`, `tracker_get_thread`, `tracker_create_item`,
`tracker_set_status`, `tracker_comment`, `tracker_reply_and_invite`, `tracker_add_testcases`,
`tracker_mark_testcase_passed`, `tracker_mark_testcase_failed`, `tracker_move_testcases_done`.
Only the first three are read-only — no filesystem/shell/git/deploy tool is exposed here
(plan §6.6).

`tracker_reply_and_invite` (2026-09-05, ticket #0102) is the agent-drivable equivalent of the
Discord "Reply to requestor" button + its reply modal: it posts `text` addressed directly to the
reporter (`<@reporter_id>` mention, so Discord notifies them), then grants them access if
they're already a member of the tracker's home guild or DMs them a one-time invite if not —
access applies automatically once they join either way, no follow-up call needed. Use this
instead of `tracker_comment` whenever the reply is meant for the reporter specifically (a
clarifying question, "please retest") — `tracker_comment` alone reaches nobody if the reporter
can't yet see the channel. Backed by `reply_and_invite_for_agent()`/`grant_access_for_agent()` in
`ui_tracker.py`, which share their actual grant/invite side effects with the Discord button (see
the "Four call sites, one grant/invite core" note above) via the bridge's
`POST /api/tracker/items/{n}/reply-and-invite` endpoint.

`tracker_get_thread` (2026-08-22) closes a one-way gap: `tracker_comment` could only ever WRITE
into an item's Discord discussion thread — there was no way for an agent to read a human's
replies, clarifications, or the automated notes (test-fail notes, other comments) already posted
there. Backed by a new `get_thread_messages()` in `ui_tracker.py`, which resolves the thread the
same way `post_comment()` already does and calls `Thread.history()`, reversed into chronological
(oldest-first) order. Returns `[]` rather than raising when the item simply has no thread yet
(best-effort thread creation in `_post_tracker_item()` can fail) — only an unknown `item_number`
raises. Since `_post_tracker_item()` seeds the thread with the item's own untruncated
title/description/details as its first message(s) (the embed-overflow strategy, §2.3), this tool
doubles as a way to read a long report that `tracker_get_item`'s embed truncated. Thread content
goes through the same `sanitize_field()`/`wrap_untrusted()` treatment as every other
reporter-supplied field — arbitrary Discord-user chat is exactly as untrusted as a report's
description/details field, just free-form rather than one structured value, so the whole
transcript is wrapped as a single block instead of per-message.

`tracker_create_item` (tracker item #0015, 2026-08-22) lets an agent file a new bug/feature
directly instead of asking a human to run `/bug`/`/feature` — it posts to the same reports
channel with the same embed/buttons a human-filed item gets. Its `reporter_id` is
`agent:<X-Tracker-Admin label>` (e.g. `agent:claude`), deliberately non-numeric so it can never
collide with a real Discord snowflake; this means the reporter-DM-on-status-change step silently
no-ops (nobody to DM) and only a bot admin — not "the reporter" — can Edit it from Discord. Both
are acceptable: there is no Discord user to notify or to grant reporter-only edit rights to.

`tracker_mark_testcase_passed`/`tracker_mark_testcase_failed` (2026-08-22) are the agent-drivable
equivalent of the Discord "✅ passed"/"❌ Failed" test-case buttons — previously the only way to
sign off a testing round. `mark_environment_passed_and_refresh()`/`mark_testing_failed()` never
actually depended on a Discord interaction (only on `QBcore.bot` for posting/refreshing
messages), so this was pure wiring: no new tracker-state logic, just a bridge
endpoint + MCP tool pointing at the same functions Discord's buttons already call. Marking an
environment passed no longer touches the item's status at all (decoupled in a follow-up the same
day, tracker item #0015 follow-up) — see the status-lifecycle section above for what happens
instead once every environment with cases is fully passed. Marking failed still reverts the item
to `in_progress`, keeping any already-passed environments' sign-off (that direction of coupling
was deliberately left alone).

`tracker_move_testcases_done` (2026-08-22 follow-up, point 4) is the agent-drivable equivalent of
the new `[ 📦 Move to Done ]` Discord button — archives the test-case message regardless of
whether every case is checked off. Without `force: true`, an incomplete set isn't moved; the
response's `needs_confirmation`/`unchecked_count` fields tell the caller how many cases remain so
it can decide whether to finish them first or retry with `force: true`. Never auto-marks the
remaining cases passed — they stay visibly unchecked in the archived message either way.

**Untrusted input (`qapbot/mcp/tracker_envelope.py`)**: bug/feature reports are arbitrary text
from arbitrary Discord users, fed straight into an agent's context — a textbook
prompt-injection surface. Every reporter-supplied field returned by `tracker_get_item` is
`sanitize_field()`'d (control chars stripped, ` ``` ` fences neutralized, length capped) then
`wrap_untrusted()`'d (`<user_report id="NNNN" field="..." trust="untrusted">…</user_report>`)
before it reaches the tool result.

**Local cache**: `tracker_get_item` downloads attachments and writes a rendered
`.tracker-cache/NNNN/item.md` under the workspace root (`.gitignore`d — never commit
reporter-uploaded content). `cache_path_for_item()` validates every write stays inside that
directory.

## Runbook

- **Enable the tracker**: nothing to set — `/bug`/`/feature` are registered automatically on
  PROD startup (`CONFIG.tracker_enabled = not is_dev_mode`). Just run `/admin` → *Bot Setup* on
  PROD to configure the channels: bug & feature reports, manual test cases, and (optional) the
  Implemented/Done Testing move-on-`done` archive channels above. `/bug` and `/feature` both
  post to the same reports channel (tracker item #0006, 2026-08-21 — they used to have separate
  channels; `item_type` still distinguishes them in the embed/emoji, just not in routing any
  more).
- **Disable at runtime without restarting**: `/admin` → *Bot Setup* → "Disable tracker" — this
  flips `bot_settings.tracker_enabled` (the *runtime* switch); `/bug`/`/feature` reply
  ephemerally that the tracker is off. There is no way to disable command *registration* itself
  short of running that instance in DEV mode.
- **MCP setup**: set `TRACKER_BRIDGE_URL`/`TRACKER_BRIDGE_SECRET`/`TRACKER_ADMIN_ID` in `.env`.
  `tracker_mcp.py` calls `load_dotenv(override=False)` itself (same convention as
  `qapbot/config.py`), so `.env` alone is sufficient — never put these as literals in
  `.mcp.json`/`.vscode/mcp.json` (Cardinal Rule 16). `.vscode/mcp.json` still passes them via
  `${env:...}`/`${workspaceFolder}`, which VS Code resolves against its own environment;
  `.mcp.json` (Claude Code CLI) does *not* reliably expand `${env:...}`/`${workspaceFolder}` —
  it uses a literal `command`/`cwd` path and relies on `.env` alone for the secrets.
- **After editing `.mcp.json`**: Claude Code only re-reads MCP server config at CLI startup —
  restart the session (`claude`) and approve the server (it shows "⏸ Pending approval" until
  then) before the tracker tools become available.
- **Attachments on disk**: `CONFIG.tracker_data_dir` (default `tracker`, project root — like
  `investigate_dir`, deliberately NOT under `data_dir`/`PROD_DATA_DIR`'s SSD, since attachment
  storage isn't performance-critical and benefits from the HDD's larger free space; override
  with `TRACKER_DATA_DIR`), one subdirectory per item (`0001/`, `0002/`, ...). Kept indefinitely
  — no automatic purge (plan §8.3).
- **Claude Code finishing a tracker-linked item**: never move an item to `implemented` without
  also posting at least one test case (`tracker_add_testcases` MCP tool, or the bridge's
  `POST /api/tracker/items/{n}/testcases` with `{"cases": [{"environment": "PROD"|"DEV"|"BOTH",
  "description": "...", "priority": "HIGH"|"MEDIUM"|"LOW"}]}` — priority is optional per case
  (defaults to MEDIUM), pick it based on how critical that specific case is to verify — when MCP
  isn't wired up in the session) — see
  `.github/copilot-instructions.md` Cardinal Rule 15. Posting the test case is what actually
  drives `post_test_cases()`'s automatic `implemented → testing` transition; skipping it leaves
  the item in a state the tracker's own lifecycle doesn't expect anyone to leave it in.

## See also

- `BUG_FEATURE_TRACKER_PLAN.md` — the full design doc (UX walkthrough, alternatives considered,
  every decision's rationale).
- `qapbot/docs/DATABASE_ARCHITECTURE.md` § Bug/Feature Tracker — table definitions.
- `qapbot/docs/COPILOT_PITFALLS_COOKBOOK.md` — the expiring-CDN-attachment-URL pitfall.
