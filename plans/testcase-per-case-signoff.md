# Per-test-case sign-off for the bug/feature tracker

## Context

The tracker's test-case sign-off is currently **per-environment only**: the Discord `[✅ DEV
passed]`/`[✅ PROD passed]` buttons and the `tracker_mark_testcase_passed` MCP tool both sign off
*every* not-yet-passed case in one environment at once. There is no way — for an agent or a human
— to mark a single specific case passed or failed.

This surfaced as a real limitation: while testing tracker #0102, the operator had 6 PROD test
cases where 2 genuinely failed (real bugs) and 4 passed. The bulk `tracker_mark_testcase_passed`
tool would have incorrectly signed off the 2 failing cases too, so it couldn't be used at all —
the only options were "pass everything" (wrong) or "leave everything unchecked and explain in a
free-text comment" (loses the itemized pass/fail record). The ask: let an agent (and, for good
UX, a human operator in Discord) mark **one test case** as passed or failed, independent of the
others.

The DB layer already has an unused, unwired per-row method (`mark_tracker_testcase_passed(testcase_id, user_id)`,
`qapbot/db_manager.py:3247`) — evidence this was the original intent but never got a UI/agent
surface. There is no per-row "failed" state at all today (only the bulk `[❌ Failed]` button,
which reverts the whole item to `in_progress`).

## Design

### 1. DB layer (`qapbot/db_manager.py`)

- Migration (idempotent `_add_column_if_missing`, same pattern as `priority`/`access_grant_pending`):
  add `failed BOOLEAN NOT NULL DEFAULT 0`, `failed_by TEXT`, `failed_at TEXT`, `fail_note TEXT` to
  `tracker_testcases`. `passed` and `failed` are mutually exclusive per row.
- Update `mark_tracker_testcase_passed(testcase_id, user_id)` (existing, currently dead code) to
  also clear `failed`/`failed_by`/`failed_at`/`fail_note` — so re-marking a previously-failed case
  passed clears its failure, giving a free "undo" path.
- Add `mark_tracker_testcase_failed(testcase_id, user_id, note=None)`: sets
  `failed=1, failed_by, failed_at`, optional `fail_note`; clears `passed=0, passed_by=NULL, passed_at=NULL`.
- Add `get_tracker_testcase_by_id(testcase_id) -> Optional[Dict]` (single-row lookup by PK, used
  to validate the case belongs to the item before mutating, and to return the updated row after).
- `mark_tracker_environment_passed` (bulk) stays untouched — still backs the existing per-environment
  button and the 👍-reaction shortcut, which remain useful "sign off everything remaining" shortcuts.
- `set_tracker_testcases` (full replace) needs no change — it already wipes all prior state on
  every call, and the new columns naturally reset to defaults through the same DELETE+INSERT.

### 2. `qapbot/ui_tracker.py` business logic

New function, mirroring `mark_environment_passed_and_refresh`'s existing before/after
"fully-passed across every case" edge-trigger check (`ui_tracker.py:2464-2497`) rather than
refactoring it — same shape, lowest risk:

```python
async def mark_testcase_by_id_and_refresh(item_number, testcase_id, result, actor_id, note=None) -> Dict[str, Any]:
    # fetch + validate testcase.item_number == item_number (raise ValueError otherwise)
    # before = get_tracker_testcases(item_number); was_fully_passed = all(passed)
    # result == "passed" -> db.mark_tracker_testcase_passed; result == "failed" -> db.mark_tracker_testcase_failed(note)
    # _refresh_testcase_message(item_number)
    # after = get_tracker_testcases(item_number); now_fully_passed = all(passed)
    # if flipped True: finalize_testcases_move(item_number) (reuses existing helper, ui_tracker.py:2453)
    # returns {"testcase": <updated row>, "just_completed": bool, "moved": bool, "linked_item": ...}
```

A **failed** result never touches item status or archival — deliberately decoupled, matching the
project's existing "each object moves on its own trigger only" convention (tracker #0015
follow-up) and the operator's own stated workflow (file separate bug tickets for real failures;
don't force the whole item back to `in_progress` as a side effect of flagging one case).

**Rendering** (`_format_testcase_lines()`, `ui_tracker.py:2077`): three states instead of two —
`☑` passed, `☒` failed (new), `☐` pending — and append `— ❌ {fail_note}` inline when a failed
case has one.

**Discord UI — new persistent Select, not per-case buttons.** Discord caps a view at 5 rows / 25
buttons, and this codebase's own established pattern for "one interactive control across a
list of rows" is a `Select` keyed by id + an action, not one button per row (see
`ui_clan_management.py`'s `clan_select`/`family_select`, both explicitly capped at `[:25]`
"Discord limit"). New `TrackerTestCaseActionSelect` (`discord.ui.DynamicItem[discord.ui.Select]`,
custom_id template `tracker:test:case:(?P<item_number>\d+)`, mirroring `TrackerTestPassButton`'s
exact shape at `ui_tracker.py:2615-2670`):

- Options rebuilt fresh every render from the current case list: for each case, offer `✅ Pass —
  {ENV} #{seq}: {desc[:50]}` (value `pass:{id}`) unless already passed, and `❌ Fail — {ENV}
  #{seq}: {desc[:50]}` (value `fail:{id}`) unless already failed — so a resolved case still shows
  the one option that would reverse it (free undo), never the no-op one. Truncated to Discord's
  25-option cap if ever exceeded (same defensive `[:25]` idiom already used elsewhere).
- Same `check_bot_admin_or_tester` gate as the existing buttons.
- On select: parse `action, testcase_id = value.split(":", 1)`. `pass` → defer + call
  `mark_testcase_by_id_and_refresh(..., "passed")` directly, ack/completion-followup exactly like
  `TrackerTestPassButton.callback()` already does. `fail` → open a small new modal,
  `TrackerTestCaseFailNoteModal` (one optional paragraph `TextInput`, "Reason (optional)"),
  mirroring `TrackerReplyModal`'s just-added pattern (`ui_tracker.py`, tracker #0102) — on submit,
  calls `mark_testcase_by_id_and_refresh(..., "failed", note=...)`.
- Added in `build_tracker_testcase_view()` (`ui_tracker.py:2257`) only when `testcases` is
  non-empty (Discord requires ≥1 option). Total component footprint: existing ≤4 buttons (1 row)
  + 1 new select (1 row) = 2 rows, comfortably under the 5-row cap.
- Registered alongside the other tracker dynamic items in `QapBot.py`'s existing
  `add_dynamic_items(TrackerItemButton, TrackerTestPassButton, TrackerTestFailButton,
  TrackerTestMoveDoneButton)` call (`QapBot.py:3499`).

### 3. Agent-facing surface (bridge + MCP)

- New bridge endpoint `POST /api/tracker/items/{item_number}/testcases/{testcase_id}/result`
  (`qapbot/web_bridge.py`, modeled directly on `handle_post_tracker_testcase_pass` at
  `web_bridge.py:3336`), body `{"result": "passed"|"failed", "note"?: str}` → calls
  `mark_testcase_by_id_and_refresh`. Response: `{"ok": true, "testcase": {...},
  "testcases_just_completed": bool, "moved": bool, "linked_item": {...}|null}`.
- `tracker_bridge_client.py`: new `mark_testcase_result(item_number, testcase_id, result, note=None)`.
- New MCP tool `tracker_mark_testcase_result` (`tracker_mcp.py`): `{item_number, testcase_id,
  result: "passed"|"failed", note?}`. Description contrasts it with the existing bulk
  `tracker_mark_testcase_passed`/`tracker_mark_testcase_failed` (whole-environment sign-off /
  whole-item revert-to-`in_progress`), which are unchanged and still the right tool for "sign off
  everything remaining."
- `render_item_markdown()`'s testcase line (`tracker_mcp.py:322-323`) needs `id=` added (an agent
  must see each case's id to address it): `- [x] (id=42, DEV, HIGH) description`, plus a `!`
  marker and inline `— FAILED: {note}` for failed cases.

### 4. Tests

- DB (`tests/unit/test_tracker_db.py`): passed-clears-failed-fields; new
  `mark_tracker_testcase_failed` (sets fields, mutually exclusive); `get_tracker_testcase_by_id`
  (found / not found).
- `ui_tracker.py` (`tests/discord/test_ui_tracker_items.py`): `mark_testcase_by_id_and_refresh`
  passed path triggers completion only on the item-wide last-pending-case flip (mirroring the
  existing `test_mark_environment_passed_...` tests); failed path never touches item status;
  wrong item_number for testcase_id raises `ValueError`. `TrackerTestCaseActionSelect`: option
  list correctness/truncation, pass path (no modal), fail path (opens modal, permission gate).
  `TrackerTestCaseFailNoteModal.on_submit`. Rendering: `_format_testcase_lines` shows `☒` +
  note for a failed case.
- Bridge (`tests/integration/test_web_bridge_tracker.py`): new endpoint — success (passed/failed),
  404 for unknown testcase_id, 404 for a testcase_id belonging to a different item, validation of
  `result`.
- MCP (`tests/unit/test_tracker_mcp.py`): new tool schema present, `call_tool` dispatch, update
  `test_write_tools_are_exactly_eight` → nine.
- Update the `render_item_markdown` test to assert `id=` appears.

### 5. Docs / changelog / build

- `qapbot/docs/BUG_FEATURE_TRACKER.md`: new dated section next to the existing test-case sign-off
  writeup, explaining per-case sign-off is additive (bulk paths unchanged), the mutually-exclusive
  passed/failed row design, and the Select-not-per-row-buttons UI rationale.
- `changelog.txt` new entry + `QBcore.BOT_BUILD` bump (Cardinal Rule 17).

## Verification

- `.\run_tests.ps1` (full suite) after implementation — must stay green, in particular every
  existing test named in the Explore report (`test_mark_environment_passed_no_longer_touches_item_status`,
  the 👍-reaction suite, `test_refresh_testcase_message_strips_view_once_archived`, etc.) since
  none of the existing bulk pass/fail/reaction paths change behavior.
- `pyright` clean on every touched file (this repo's own convention of running it beyond just
  tests — it already caught a real bug earlier this session).
- No live Discord/PROD verification possible from here (tracker only runs on PROD, no DEV
  live-test path per `BUG_FEATURE_TRACKER.md`) — covered instead by the new unit/discord/bridge
  tests above, plus PROD test cases posted to the relevant tracker item once this ships, per this
  project's existing workflow.
