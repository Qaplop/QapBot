import type { ClanConfig, ClanConfigPayload, GuestPlayerPoolEntry, GuestSearchResult } from './types'

const ROSTER_SIZES = [5, 15, 30] as const

// Mirrors qapbot/web_bridge.py's GUEST_SEARCH_MIN_NEEDLE_TAG/_TEXT exactly (2026-08-17,
// CWL_PROD_PERFORMANCE_FIX_PLAN.md P0 Step 5) — checking client-side too means a too-short query
// never even fires the debounced fetch, instead of round-tripping to the bridge just to get an
// empty-results response back. The server-side check is still the real guard (never trust the
// client) — this is purely to save a wasted request per keystroke below the minimum.
const GUEST_SEARCH_MIN_NEEDLE_TAG = 2
const GUEST_SEARCH_MIN_NEEDLE_TEXT = 3

/** Effective minimum for `query` as typed (including any `@`/`#` prefix) — mirrors the server's
 * needle-length check (see _search_cwl_guests_sync's docstring for the two-namespace-prefix
 * rules this needle length is checked against). */
function guestSearchMinNeedleLength(query: string): number {
  return query.startsWith('@') || query.startsWith('#') ? GUEST_SEARCH_MIN_NEEDLE_TAG : GUEST_SEARCH_MIN_NEEDLE_TEXT
}

function guestSearchNeedleLength(query: string): number {
  return query.startsWith('@') || query.startsWith('#') ? query.slice(1).trim().length : query.length
}

// datetime-local's `step` attribute only affects *validation*, never which minutes the native
// picker actually shows (confirmed: the spec ties step to validity checking, not UI presentation
// — Chromium's scrollable minute list always offers all 60 regardless of step). The only
// reliable way to truly restrict the *visible* options is to stop using the native minute
// picker for it: a plain date input (unaffected by this issue) plus a <select> pre-populated
// with only :00/:15/:30/:45 per hour.
const TIME_OF_DAY_OPTIONS: string[] = (() => {
  const options: string[] = []
  for (let h = 0; h < 24; h++) {
    for (const m of [0, 15, 30, 45]) {
      options.push(`${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`)
    }
  }
  return options
})()

function pad(n: number): string {
  return String(n).padStart(2, '0')
}

function snapToQuarterHour(hhmm: string): string {
  const [hStr, mStr] = hhmm.split(':')
  const h = Number(hStr) || 0
  const m = Number(mStr) || 0
  const snappedM = Math.round(m / 15) * 15
  const carry = snappedM === 60
  const finalH = (h + (carry ? 1 : 0)) % 24
  return `${pad(finalH)}:${pad(carry ? 0 : snappedM)}`
}

/** Bridge/DB storage is always UTC ("YYYY-MM-DDTHH:MMZ") — the user sees and sets everything in
 * their own browser timezone (Phase E.5); these two functions are the only place that
 * conversion happens, keeping it invisible everywhere else in this file. */
function utcStringToLocalParts(utc: string): { date: string; time: string } | null {
  const parsed = new Date(utc)
  if (Number.isNaN(parsed.getTime())) return null
  return {
    date: `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())}`,
    time: snapToQuarterHour(`${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`),
  }
}

function localPartsToUtcString(date: string, time: string): string {
  const [y, mo, d] = date.split('-').map(Number)
  const [h, mi] = time.split(':').map(Number)
  // Constructed from local (year, month, day, hour, minute) components — the Date constructor
  // interprets these as local time by default, which is exactly the "user's own timezone" input
  // Phase E.5 asks for. toISOString() converts it to UTC for us; slice off seconds/millis to
  // match the bridge's "YYYY-MM-DDTHH:MMZ" convention.
  const local = new Date(y, mo - 1, d, h, mi)
  return `${local.toISOString().slice(0, 16)}Z`
}

/** Renders the CWL clan-config table — checkbox / tag / tier / roster-size select / start-time
 * picker per row — into `container`, replacing whatever was there. This is the actual reason
 * this Activity exists: none of these columns fit together in any Discord-native component
 * (see CWL_CLAN_CONFIG_ACTIVITY_PLAN.md's "Context" section). Clans arrive pre-sorted by CWL
 * tier, highest first (the bridge does this — see web_bridge.py's _build_clan_config_payload).
 *
 * Season selection lives entirely on the Discord-side CWL Management screen (Phase E.2/E.3) —
 * this table always just shows/edits whichever season the bridge resolved; there's no season
 * picker or carry-over prompt here.
 *
 * Edits are held in a working copy and only sent anywhere when "Save" is clicked. Both buttons
 * close the Activity via `onClose` when done — Save persists first then closes, Cancel closes
 * immediately without saving anything. No intermediate confirmation screen; closing itself is
 * the confirmation.
 *
 * `onClose(reason)` calls discordSdk.close(RPCCloseCodes.CLOSE_NORMAL, reason) — a real,
 * documented top-level SDK method, confirmed working live (both from Save and Cancel).
 *
 * "Guests" section (2026-08-15, project owner's spec — invite a clan or individual player from
 * outside this guild's own family): a guest CLAN result is just appended to `working` and goes
 * through the exact same `onSave` path as every other row — nothing guest-specific about it
 * server-side (see qapbot/web_bridge.py's _search_cwl_guests docstring for why). A guest PLAYER
 * result is a genuinely separate, immediate action via `onGuestPlayerAdd` — it doesn't touch
 * `working` or `cwl_event_clans` at all, it writes straight to `cwl_signups`, so it can't be
 * batched into the same Save button; it applies the moment "Add" is clicked, independent of
 * whether the admin ever clicks Save on the clan table itself.
 */
export function renderClanConfigTable(
  container: HTMLElement,
  payload: ClanConfigPayload,
  onSave: (clans: ClanConfig[]) => Promise<void>,
  onClose: (reason: string) => void,
  onGuestSearch: (query: string) => Promise<GuestSearchResult[]>,
  onGuestPlayerAdd: (result: Extract<GuestSearchResult, { type: 'player' }>) => Promise<void>,
  // Owner-only eviction (2026-08-15) — removes targetGuildId's participation in a shared clan.
  // Independent of Save/onSave, same immediate-action shape as onGuestPlayerAdd: it doesn't
  // touch `working` at all, so nothing here needs a page reload to reflect it beyond re-running
  // this same render with a fresh payload (see main.ts's call site).
  onEvict: (clanTag: string, targetGuildId: string) => Promise<void>,
  // Rule f (2026-08-18, CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md) — full removal of a guest
  // clan from the season (deletes its cwl_event_clans row AND purges its members from the
  // player pool), as opposed to just unchecking it (now purely cosmetic). Same immediate-action
  // shape as onEvict/onGuestPlayerAdd: independent of Save, applies the moment "Remove" +
  // confirm is clicked.
  onGuestClanRemove: (clanTag: string) => Promise<void>,
  // Rule g (2026-08-18) — "Remove Guest Players." Fetched fresh each time the panel opens
  // (immediate action, independent of Save, same shape as every other guest-management callback
  // here) rather than reusing `working`'s own player data, since guest PLAYERS aren't part of
  // `working` at all (that array is clans only).
  onGuestPlayersList: () => Promise<GuestPlayerPoolEntry[]>,
  onGuestPlayersRemove: (
    playerTags: string[],
  ) => Promise<{ rejected: { player_tag: string; clan_name: string }[] }>,
): void {
  const working: ClanConfig[] = payload.clans.map((c) => ({ ...c }))
  // Immutable snapshot of which clans already exist on the SAVED roster, taken once here before
  // any Add/Remove mutates `working` — removeGuestClanRow (below) uses this to tell "removing a
  // clan the backend has never heard of yet" (a freshly-added, still-unsaved row — nothing to
  // call the backend about, just drop it from `working`) apart from "removing a clan that's
  // actually persisted" (needs the real onGuestClanRemove network call) (2026-08-19, project
  // owner's spec: the Remove button must appear immediately on Add, not only after Save+reopen).
  const persistedClanTags = new Set(payload.clans.map((c) => c.clan_tag))

  container.innerHTML = ''

  // Sticky top block (mirrors enrollmentBoard.ts's board-topbar) so the season/status title and
  // timezone note stay in view while the clan table scrolls underneath — otherwise a long clan
  // list scrolls both the title and the Save/Cancel footer out of view with no way back short of
  // scrolling all the way up/down again.
  const topBar = document.createElement('div')
  topBar.className = 'board-topbar'
  container.appendChild(topBar)

  const header = document.createElement('div')
  header.className = 'header'
  header.textContent = `Season ${payload.season} — ${payload.event_status ?? 'draft'}`
  topBar.appendChild(header)

  const tzNote = document.createElement('div')
  tzNote.className = 'tz-note'
  const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone
  tzNote.textContent = `Times shown in your local timezone (${timeZone}) — saved as UTC automatically.`
  topBar.appendChild(tzNote)

  const table = document.createElement('table')
  const thead = document.createElement('thead')
  const theadRow = document.createElement('tr')

  const selectAllCheckbox = document.createElement('input')
  selectAllCheckbox.type = 'checkbox'
  selectAllCheckbox.title = 'Select/deselect all clans'
  selectAllCheckbox.setAttribute('aria-label', 'Select all clans')
  const selectAllTh = document.createElement('th')
  selectAllTh.appendChild(selectAllCheckbox)
  theadRow.appendChild(selectAllTh)

  for (const label of ['Clan', 'Tier', 'Roster Size', 'Start Time (local)']) {
    const th = document.createElement('th')
    th.textContent = label
    theadRow.appendChild(th)
  }
  thead.appendChild(theadRow)
  table.appendChild(thead)

  // `top: 0` — NOT the topbar's height (2026-08-16 regression fix; see `.table-scroll`'s own
  // comment below for the full root-cause). thead's sticky containing block is `.table-scroll`
  // itself (below), not the page, so `top` is relative to *its* box, which already starts right
  // where the table begins — offsetting by the topbar's height on top of that pushed the header
  // down into the body rows, hiding them behind its opaque background.
  thead.querySelectorAll('th').forEach((th) => {
    ;(th as HTMLElement).style.top = '0px'
  })

  // CWL itself never starts before the 1st of the season's month at 08:00 UTC (the game's
  // static schedule) — clamp every row's picker to that floor so an admin can't accidentally
  // schedule a clan's switch-over before CWL has even begun. The ceiling (+48h) exists because
  // a clan switching in later than that would miss too much of the war league to make sense —
  // both bounds are enforced identically (native min/max plus JS re-validation on every change).
  const seasonStartUtc = `${payload.season}-01T08:00Z`
  const seasonEndUtc = `${new Date(new Date(seasonStartUtc).getTime() + 48 * 60 * 60 * 1000).toISOString().slice(0, 16)}Z`

  // Rule f's Remove-button handler (2026-08-18) — network call (onGuestClanRemove, supplied by
  // the caller/main.ts) plus the outer-scope cleanup a successful removal needs: splice `working`
  // (so a later Save can't resurrect the removed clan), drop it from `existingClanTags`, and
  // refresh the select-all/read-only-notice state, exactly like the guest-clan Add flow's own
  // state updates below. Defined as a hoisted function declaration so it can safely reference
  // `existingClanTags`/`updateSelectAllState`/`updateReadOnlyNoticeVisibility` even though those
  // are declared further down in this function body — none of that matters until this actually
  // runs, long after the whole render has finished (a user click, not initial render).
  //
  // Skips the network call entirely for a clan NOT in persistedClanTags (2026-08-19) — a
  // freshly-added, still-unsaved row the backend has never heard of; onGuestClanRemove would
  // just 404 against a cwl_event_clans row that doesn't exist yet. "Remove" on a row like that is
  // exactly the same as it always silently was before Save existed: drop it from `working` and
  // move on.
  async function removeGuestClanRow(clan: ClanConfig, row: HTMLTableRowElement): Promise<void> {
    if (persistedClanTags.has(clan.clan_tag)) {
      await onGuestClanRemove(clan.clan_tag)
    }
    const index = working.indexOf(clan)
    if (index !== -1) working.splice(index, 1)
    existingClanTags.delete(clan.clan_tag)
    row.remove()
    updateSelectAllState()
    updateReadOnlyNoticeVisibility()
  }

  const tbody = document.createElement('tbody')
  for (const clan of working) {
    tbody.appendChild(buildRow(clan, seasonStartUtc, seasonEndUtc, onEvict, removeGuestClanRow))
  }
  table.appendChild(tbody)

  // Scrolls sideways instead of letting cell content wrap on a narrow window — see the
  // `.table-scroll`/`white-space: nowrap` comment in index.html for why wrapping is the thing
  // that actually breaks row alignment, not narrowness itself.
  //
  // Bounded `max-height` + its own internal vertical scroll (2026-08-16 regression fix,
  // discovered live-testing (34): giving `.table-scroll` any non-`visible` `overflow-x` makes
  // browsers force its computed `overflow-y` to `auto` too — CSS Overflow's "one axis visible,
  // one not" coupling rule, not a bug in this code — which silently turns `.table-scroll` itself
  // into the sticky *containing block* for the `th`s inside it, instead of the page. The `th`s'
  // `top: 0` above only works — i.e. only actually tracks scrolling and stays visible — if
  // `.table-scroll` is a REAL scroll container (content taller than its own box) rather than an
  // auto-height div that always fits its content exactly (which never scrolls internally, so a
  // sticky child inside it never has anything to react to and just sits at a fixed offset,
  // permanently — that's what hid the rows: `top: topBarHeight` used to be that fixed offset).
  // Mirrors enrollmentBoard.ts's own `resizeBoard()`/`.board` pattern (same underlying problem,
  // solved there first) — `max-height` rather than `.board`'s `height` since this table should
  // still shrink to fit a short clan list instead of always claiming the full available space.
  const tableScroll = document.createElement('div')
  tableScroll.className = 'table-scroll'
  tableScroll.appendChild(table)
  container.appendChild(tableScroll)

  function resizeTableScroll(): void {
    const available = window.innerHeight - topBar.getBoundingClientRect().height - 32
    tableScroll.style.maxHeight = `${Math.max(200, available)}px`
  }
  resizeTableScroll()
  window.addEventListener('resize', resizeTableScroll)

  // General read-only-settings notice (2026-08-15, live-testing feedback) — replaces the old
  // per-row explanation (moved out specifically so every row stays the same height regardless of
  // sharing status; see buildRow's own comment). Shown once, only while at least one row is
  // actually a read-only follower of another guild's shared clan.
  const readOnlyNotice = document.createElement('div')
  readOnlyNotice.className = 'shared-clan-readonly-notice'
  readOnlyNotice.textContent = '⚠️ For clans managed by another guild, roster size & start time are read-only!'
  container.appendChild(readOnlyNotice)

  function updateReadOnlyNoticeVisibility(): void {
    const anyReadOnly = working.some((c) => c.shared_with !== null && !c.shared_with.is_owner)
    readOnlyNotice.style.display = anyReadOnly ? '' : 'none'
  }
  updateReadOnlyNoticeVisibility()

  // Select-all header checkbox: reflects the row checkboxes' combined state (fully checked /
  // fully unchecked / native `.indeterminate` for a mixed selection — the standard convention
  // for this control) and, when clicked itself, drives every row checkbox to match by
  // dispatching a real 'change' event on each — reuses buildRow()'s own listener (updates
  // clan.participating + the row's disabled/inactive styling) rather than duplicating it here.
  const rowCheckboxes = () => tbody.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')

  function updateSelectAllState(): void {
    const total = working.length
    const checkedCount = working.filter((c) => c.participating).length
    selectAllCheckbox.checked = total > 0 && checkedCount === total
    selectAllCheckbox.indeterminate = checkedCount > 0 && checkedCount < total
  }

  selectAllCheckbox.addEventListener('change', () => {
    // Capture the target state ONCE — updateSelectAllState() (called via each row's own
    // 'change' listener as this loop dispatches events) mutates selectAllCheckbox.checked as a
    // side effect after every single row, so re-reading it live inside the loop turned this
    // into a feedback loop: after row 1 flipped to checked, the mid-loop recompute immediately
    // set selectAllCheckbox.checked back to false (not everything matched yet), so row 2 read
    // that stale false and got unchecked again — net effect, only the first row ever "stuck".
    const shouldCheck = selectAllCheckbox.checked
    rowCheckboxes().forEach((cb) => {
      if (cb.checked !== shouldCheck) {
        cb.checked = shouldCheck
        cb.dispatchEvent(new Event('change'))
      }
    })
    updateSelectAllState()
  })
  rowCheckboxes().forEach((cb) => cb.addEventListener('change', updateSelectAllState))
  updateSelectAllState()

  const existingClanTags = new Set(working.map((c) => c.clan_tag))
  const guestsSection = document.createElement('div')
  guestsSection.className = 'guests-section'

  const guestsTitle = document.createElement('div')
  guestsTitle.className = 'guests-title'
  guestsTitle.textContent = 'Guests'
  guestsSection.appendChild(guestsTitle)

  const guestsHint = document.createElement('div')
  guestsHint.className = 'guests-hint'
  guestsHint.textContent =
    'Invite a clan or an individual player who isn’t part of this guild’s own clan family. ' +
    'A guest clan is added to the table above (Save to persist); a guest player is added to the ' +
    'enrollment pool immediately.'
  guestsSection.appendChild(guestsHint)

  const guestsSearchRow = document.createElement('div')
  guestsSearchRow.className = 'guests-search-row'
  const guestsSearchInput = document.createElement('input')
  guestsSearchInput.type = 'text'
  guestsSearchInput.className = 'guests-search-input'
  guestsSearchInput.placeholder = 'Search name/tag, clan, @discord user, or #tag…'
  guestsSearchRow.appendChild(guestsSearchInput)
  guestsSection.appendChild(guestsSearchRow)

  const guestsResults = document.createElement('div')
  guestsResults.className = 'guests-results'
  guestsSection.appendChild(guestsResults)

  const guestsStatus = document.createElement('div')
  guestsStatus.className = 'guests-status'
  guestsSection.appendChild(guestsStatus)

  // Debounced (300ms) — a search fires on every keystroke otherwise, hammering the bridge for
  // no benefit since the admin is still mid-word almost every time.
  let searchDebounceHandle: ReturnType<typeof setTimeout> | undefined
  let searchRequestId = 0

  function renderGuestResults(results: GuestSearchResult[]): void {
    guestsResults.innerHTML = ''
    for (const result of results) {
      // A guest clan added but not yet Saved has no backend cwl_event_clans row yet, so the
      // server-side search can't exclude it itself (2026-08-20 fix, live bug report: an
      // already-invited guest kept reappearing in later searches) — existingClanTags is this
      // table's own live set of what's already on it, persisted or not.
      if (result.type === 'clan' && existingClanTags.has(result.clan_tag)) continue

      const row = document.createElement('div')
      row.className = 'guest-result'

      const badge = document.createElement('span')
      badge.className = `guest-result-badge ${result.type}`
      badge.textContent = result.type
      row.appendChild(badge)

      const label = document.createElement('span')
      label.className = 'guest-result-label'
      const addButton = document.createElement('button')
      addButton.className = 'guest-add-button'
      addButton.textContent = 'Add'

      if (result.type === 'clan') {
        const clanResult = result
        label.textContent = clanResult.already_shared_with
          ? `${clanResult.clan_name} (${clanResult.clan_tag}) — already on ${clanResult.already_shared_with}'s roster`
          : `${clanResult.clan_name} (${clanResult.clan_tag})`
        if (clanResult.already_shared_with) row.classList.add('guest-result-shared')
        row.appendChild(label)
        row.appendChild(addButton)

        const addClan = (): void => {
          const newClan: ClanConfig = {
            clan_tag: clanResult.clan_tag,
            name: clanResult.clan_name,
            tier: clanResult.clan_tier,
            participating: true,
            roster_size: 15,
            // Same default a never-configured clan already gets from the backend payload
            // (qapbot/web_bridge.py's _build_clan_config_payload: "1st of the season's month at
            // 08:00 UTC") — null here left the date picker showing empty placeholders instead of
            // a real default (live-testing feedback, 2026-08-15).
            cwl_start_at: seasonStartUtc,
            // Freshly added, not saved yet — no real cwl_shared_clans row exists for this
            // clan/guild pairing until Save actually runs, so is_owner/other_guild_ids can't be
            // known precisely yet (2026-08-15, project owner's spec: "no need to save first...
            // just fetch the info about the added clan when 'add' is selected" — the guest
            // search already told us this in already_shared_with, so show it immediately rather
            // than discarding it until after Save). is_owner is always false here — a clan
            // that's already someone else's roster can never make US the owner just by adding
            // it as a guest; other_guild_ids stays empty since buildRow only ever reads it to
            // build Evict buttons, which are is_owner-gated and so never render here anyway.
            shared_with: clanResult.already_shared_with
              ? { is_owner: false, other_guild_ids: [], other_guild_names: [clanResult.already_shared_with] }
              : null,
            // Always a guest by construction — the Guests search only ever offers clans outside
            // the guild's own family (2026-08-19, project owner's spec: the Remove button must
            // show up immediately on Add, not only after Save+reopen). removeGuestClanRow's own
            // persistedClanTags check is what keeps clicking it here from firing a network call
            // against a clan the backend has never heard of yet.
            is_guest: true,
          }
          working.push(newClan)
          existingClanTags.add(clanResult.clan_tag)
          const newRow = buildRow(newClan, seasonStartUtc, seasonEndUtc, onEvict, removeGuestClanRow)
          tbody.appendChild(newRow)
          newRow.querySelector<HTMLInputElement>('input[type="checkbox"]')?.addEventListener('change', updateSelectAllState)
          updateSelectAllState()
          updateReadOnlyNoticeVisibility()
          guestsStatus.textContent = clanResult.already_shared_with
            ? `Added ${clanResult.clan_name} — shared with ${clanResult.already_shared_with}. Click Save above to persist.`
            : `Added ${clanResult.clan_name} — click Save above to persist.`
          guestsStatus.className = 'guests-status success'
          guestsSearchInput.value = ''
          guestsResults.innerHTML = ''
        }

        addButton.addEventListener('click', () => {
          if (existingClanTags.has(clanResult.clan_tag)) {
            guestsStatus.textContent = `${clanResult.clan_name} is already on the list above.`
            guestsStatus.className = 'guests-status error'
            return
          }
          if (!clanResult.already_shared_with) {
            addClan()
            return
          }
          // Cross-guild shared-clan confirmation (2026-08-15, project owner's spec: "asked if he
          // would like to add the clan to the own guild's clan roster nevertheless") — no
          // window.confirm() (this runs in a sandboxed Activity iframe, and every other
          // confirmation in this codebase is an inline UI element, never a browser dialog):
          // swap the Add button for an inline Yes/Cancel pair instead.
          addButton.remove()
          const confirmLabel = document.createElement('span')
          confirmLabel.className = 'guest-result-note'
          confirmLabel.textContent = 'Add anyway?'
          const yesButton = document.createElement('button')
          yesButton.className = 'guest-add-button'
          yesButton.textContent = 'Yes'
          const cancelButton = document.createElement('button')
          cancelButton.className = 'guest-add-button'
          cancelButton.textContent = 'Cancel'
          yesButton.addEventListener('click', addClan)
          cancelButton.addEventListener('click', () => {
            confirmLabel.remove()
            yesButton.remove()
            cancelButton.remove()
            row.appendChild(addButton)
          })
          row.append(confirmLabel, yesButton, cancelButton)
        })
      } else {
        const dmNote = result.discord_id ? '' : ' — not linked, can’t DM yet'
        label.textContent = `${result.player_name} (${result.player_tag})${dmNote}`
        row.appendChild(label)
        row.appendChild(addButton)
        addButton.addEventListener('click', async () => {
          addButton.disabled = true
          guestsStatus.textContent = 'Adding…'
          guestsStatus.className = 'guests-status'
          try {
            await onGuestPlayerAdd(result)
            guestsStatus.textContent = `Added ${result.player_name} to the enrollment pool.`
            guestsStatus.className = 'guests-status success'
            guestsSearchInput.value = ''
            guestsResults.innerHTML = ''
          } catch (err) {
            console.error(err)
            guestsStatus.textContent = `Failed to add ${result.player_name}: ${(err as Error).message}`
            guestsStatus.className = 'guests-status error'
            addButton.disabled = false
          }
        })
      }

      guestsResults.appendChild(row)
    }
  }

  function handleSearchInput(): void {
    if (searchDebounceHandle !== undefined) clearTimeout(searchDebounceHandle)
    const query = guestsSearchInput.value.trim()
    if (!query) {
      guestsResults.innerHTML = ''
      guestsStatus.textContent = ''
      guestsStatus.className = 'guests-status'
      return
    }
    if (guestSearchNeedleLength(query) < guestSearchMinNeedleLength(query)) {
      // Below the server's own minimum (Step 5) — don't even fire the debounced fetch; clear any
      // stale results from a longer prior query and hint at how many more characters are needed.
      guestsResults.innerHTML = ''
      guestsStatus.textContent = `Type at least ${guestSearchMinNeedleLength(query)} characters…`
      guestsStatus.className = 'guests-status'
      return
    }
    guestsStatus.textContent = ''
    guestsStatus.className = 'guests-status'
    searchDebounceHandle = setTimeout(() => {
      const thisRequestId = ++searchRequestId
      onGuestSearch(query)
        .then((results) => {
          // A slower earlier request finishing after a faster later one would otherwise
          // overwrite the results list with stale data — only the most recently *fired* request
          // is allowed to render.
          if (thisRequestId === searchRequestId) renderGuestResults(results)
        })
        .catch((err) => {
          // AbortError (Step 5, main.ts's guestSearchController) means a newer keystroke's fetch
          // superseded this one — not a real failure, so it's silently dropped rather than
          // surfaced as "Search failed" (which would otherwise flash on every keystroke of a
          // still-being-typed query).
          if ((err as Error).name === 'AbortError') return
          console.error(err)
          if (thisRequestId === searchRequestId) {
            guestsStatus.textContent = `Search failed: ${(err as Error).message}`
            guestsStatus.className = 'guests-status error'
          }
        })
    }, 300)
  }

  guestsSearchInput.addEventListener('input', handleSearchInput)

  // Paste-specific fallback (2026-08-18, live-testing feedback: "when i pasted from the
  // clipboard to the search field it stopped working. i had to close and reopen the view to get
  // it back to work.") — inside a Discord Activity's sandboxed iframe, a clipboard paste can
  // apparently swallow the native 'input' event the browser normally fires right after (the
  // pasted text still visibly lands in the field — that part isn't JS-driven at all — but
  // nothing here ever heard about it, so the debounced search silently never fires and every
  // later keystroke inherits the same dead state). A dedicated 'paste' listener is a second,
  // independent way to notice the exact same change: at the moment 'paste' fires the browser
  // hasn't actually inserted the clipboard text into `.value` yet (that's the event's own
  // default action, which runs right after), so this defers one tick via setTimeout(…, 0) before
  // reading `.value` and re-running the identical search trigger — cheap and fully idempotent if
  // the native 'input' event *did* also fire normally (handleSearchInput's own debounce/
  // requestId guards already collapse any resulting duplicate call to a no-op).
  guestsSearchInput.addEventListener('paste', () => {
    setTimeout(handleSearchInput, 0)
  })

  // Rule g (2026-08-18) — "Remove Guest Players": a collapsed-by-default multi-select list of
  // every currently-pooled guest player, fetched fresh each time the panel opens so it always
  // reflects the latest pool state (including any orphaned-by-rule-f leftovers) rather than a
  // possibly-stale snapshot from page load.
  const guestPlayersSection = document.createElement('div')
  guestPlayersSection.className = 'guest-players-section'

  const guestPlayersToggle = document.createElement('button')
  guestPlayersToggle.className = 'guest-add-button'
  guestPlayersToggle.textContent = 'Remove Guest Players…'
  guestPlayersSection.appendChild(guestPlayersToggle)

  const guestPlayersPanel = document.createElement('div')
  guestPlayersPanel.className = 'guest-players-panel'
  guestPlayersPanel.style.display = 'none'
  guestPlayersSection.appendChild(guestPlayersPanel)

  guestPlayersToggle.addEventListener('click', async () => {
    // Toggle closed if already open.
    if (guestPlayersPanel.style.display !== 'none') {
      guestPlayersPanel.style.display = 'none'
      guestPlayersPanel.innerHTML = ''
      return
    }
    guestPlayersPanel.style.display = ''
    guestPlayersPanel.textContent = 'Loading…'
    let players: GuestPlayerPoolEntry[]
    try {
      players = await onGuestPlayersList()
    } catch (err) {
      console.error(err)
      guestPlayersPanel.textContent = `Failed to load: ${(err as Error).message}`
      return
    }
    guestPlayersPanel.innerHTML = ''
    if (players.length === 0) {
      const empty = document.createElement('div')
      empty.className = 'guests-status'
      empty.textContent = 'No guest players in the pool right now.'
      guestPlayersPanel.appendChild(empty)
      return
    }

    const list = document.createElement('div')
    list.className = 'guest-players-list'
    const checkboxes: HTMLInputElement[] = []
    for (const player of players) {
      const row = document.createElement('label')
      row.className = 'guest-players-row'
      const cb = document.createElement('input')
      cb.type = 'checkbox'
      cb.value = player.player_tag
      checkboxes.push(cb)
      row.appendChild(cb)
      row.append(` ${player.player_name ?? player.player_tag} (${player.player_tag})`)
      list.appendChild(row)
    }
    guestPlayersPanel.appendChild(list)

    const actionRow = document.createElement('div')
    actionRow.className = 'guest-players-actions'
    const removeSelectedButton = document.createElement('button')
    removeSelectedButton.className = 'guest-clan-remove-button'
    removeSelectedButton.textContent = 'Remove Selected'
    const cancelSelectionButton = document.createElement('button')
    cancelSelectionButton.className = 'guest-add-button'
    cancelSelectionButton.textContent = 'Cancel'
    const selectionStatus = document.createElement('span')
    selectionStatus.className = 'guests-status'
    actionRow.append(removeSelectedButton, cancelSelectionButton, selectionStatus)
    guestPlayersPanel.appendChild(actionRow)

    cancelSelectionButton.addEventListener('click', () => {
      guestPlayersPanel.style.display = 'none'
      guestPlayersPanel.innerHTML = ''
    })
    removeSelectedButton.addEventListener('click', async () => {
      const selected = checkboxes.filter((cb) => cb.checked).map((cb) => cb.value)
      if (selected.length === 0) {
        selectionStatus.textContent = 'Select at least one player.'
        selectionStatus.className = 'guests-status error'
        return
      }
      removeSelectedButton.disabled = true
      cancelSelectionButton.disabled = true
      selectionStatus.textContent = 'Removing…'
      selectionStatus.className = 'guests-status'
      try {
        const { rejected } = await onGuestPlayersRemove(selected)
        if (rejected.length === 0) {
          guestPlayersPanel.style.display = 'none'
          guestPlayersPanel.innerHTML = ''
          return
        }
        // Race condition (2026-08-19, guest-player provenance feature): the list above is already
        // pre-filtered to individually-invited players by onGuestPlayersList, so landing here
        // means one of them got reclassified as clan-derived (their clan was invited as a guest
        // clan) in the moment between that fetch and this click — rare, but the backend is the
        // final authority, not this stale-by-the-time-of-click snapshot. Any tags NOT in
        // `rejected` were still removed successfully.
        const rejectedNames = rejected.map((r) => {
          const player = players.find((p) => p.player_tag === r.player_tag)
          const label = player ? (player.player_name ?? player.player_tag) : r.player_tag
          return `${label} (added via guest clan ${r.clan_name})`
        })
        selectionStatus.textContent = `Could not remove: ${rejectedNames.join(', ')}. Remove the whole guest clan instead.`
        selectionStatus.className = 'guests-status error'
        removeSelectedButton.disabled = false
        cancelSelectionButton.disabled = false
        for (const cb of checkboxes) {
          if (!rejected.some((r) => r.player_tag === cb.value)) cb.checked = false
        }
      } catch (err) {
        console.error(err)
        selectionStatus.textContent = `Failed: ${(err as Error).message}`
        selectionStatus.className = 'guests-status error'
        removeSelectedButton.disabled = false
        cancelSelectionButton.disabled = false
      }
    })
  })

  guestsSection.appendChild(guestPlayersSection)

  container.appendChild(guestsSection)

  const status = document.createElement('span')
  status.className = 'save-status'

  const saveButton = document.createElement('button')
  saveButton.textContent = 'Save'
  saveButton.className = 'save-button'

  const cancelButton = document.createElement('button')
  cancelButton.textContent = 'Cancel'
  cancelButton.className = 'cancel-button'
  cancelButton.addEventListener('click', () => {
    onClose('Cancelled')
  })

  saveButton.addEventListener('click', async () => {
    saveButton.disabled = true
    cancelButton.disabled = true
    status.textContent = 'Saving…'
    status.className = 'save-status'
    try {
      await onSave(working)
      onClose('Saved')
    } catch (err) {
      console.error(err)
      status.textContent = `Save failed: ${(err as Error).message}`
      status.className = 'save-status error'
      saveButton.disabled = false
      cancelButton.disabled = false
    }
  })

  const footer = document.createElement('div')
  footer.className = 'footer sticky-footer'
  footer.appendChild(saveButton)
  footer.appendChild(cancelButton)
  footer.appendChild(status)
  container.appendChild(footer)
}

function buildRow(
  clan: ClanConfig,
  seasonStartUtc: string,
  seasonEndUtc: string,
  onEvict: (clanTag: string, targetGuildId: string) => Promise<void>,
  // Rule f (2026-08-18) — full clan removal, offered only for a real (saved) guest clan row, see
  // ClanConfig.is_guest's own comment. The caller (renderClanConfigTable's removeGuestClanRow)
  // owns both the network call and the outer working[]/existingClanTags cleanup; this function
  // only owns the button/confirm UI and error display.
  onGuestClanRemove: (clan: ClanConfig, row: HTMLTableRowElement) => Promise<void>,
): HTMLTableRowElement {
  const row = document.createElement('tr')
  const seasonStartMs = new Date(seasonStartUtc).getTime()
  const seasonEndMs = new Date(seasonEndUtc).getTime()
  const seasonStartLocal = utcStringToLocalParts(seasonStartUtc)
  const seasonEndLocal = utcStringToLocalParts(seasonEndUtc)

  const checkboxCell = document.createElement('td')
  const checkbox = document.createElement('input')
  checkbox.type = 'checkbox'
  checkbox.checked = clan.participating

  const nameCell = document.createElement('td')
  nameCell.append(`${clan.name} (${clan.clan_tag})`)

  // Cross-guild shared-clan status (2026-08-15, live-testing feedback: a block-level badge
  // wrapping onto its own line under the name made this row taller than its neighbors — even
  // with every cell's vertical-align set to top, a taller row still visibly threw off its OWN
  // internal alignment. Fixed by keeping the status text INLINE, directly after the clan
  // name/tag on the very same line, so every row keeps identical height regardless of sharing
  // status; the "read-only" explanation moved to a single general notice below the whole table
  // (see renderClanConfigTable) instead of repeating per-row.
  // Cross-guild shared-clan settings lock (2026-08-15 follow-up, project owner's spec): "one
  // shared record," not two independently-edited copies — roster size & start time are the
  // OWNER guild's alone to set; a follower's copy of this row must be visibly and functionally
  // read-only, not just silently overridden server-side on save.
  const isLockedByOwner = clan.shared_with !== null && !clan.shared_with.is_owner

  if (clan.shared_with) {
    const sharedWith = clan.shared_with
    const label = document.createElement('span')
    label.className = 'shared-clan-inline'
    label.textContent = sharedWith.is_owner
      ? ` 🔗 Shared with: ${sharedWith.other_guild_names.join(', ')}`
      : ` 🔗 Managed by ${sharedWith.other_guild_names.join(', ')}`
    nameCell.appendChild(label)

    if (sharedWith.is_owner) {
      for (let i = 0; i < sharedWith.other_guild_ids.length; i++) {
        const targetGuildId = sharedWith.other_guild_ids[i]
        const targetGuildName = sharedWith.other_guild_names[i]
        const evictButton = document.createElement('button')
        evictButton.className = 'evict-button'
        evictButton.textContent = `Evict ${targetGuildName}`
        evictButton.addEventListener('click', async () => {
          evictButton.disabled = true
          try {
            await onEvict(clan.clan_tag, targetGuildId)
            label.textContent = ` 🔗 Evicted ${targetGuildName} — reload to refresh this row.`
            evictButton.remove()
          } catch (err) {
            console.error(err)
            evictButton.disabled = false
            evictButton.textContent = `Evict ${targetGuildName} (failed, retry?)`
          }
        })
        nameCell.appendChild(evictButton)
      }
    }
  }

  const tierCell = document.createElement('td')
  tierCell.textContent = clan.tier ?? '—'
  tierCell.className = 'tier-cell'

  const rosterCell = document.createElement('td')
  const rosterSelect = document.createElement('select')
  for (const size of ROSTER_SIZES) {
    const option = document.createElement('option')
    option.value = String(size)
    option.textContent = String(size)
    option.selected = clan.roster_size === size
    rosterSelect.appendChild(option)
  }
  rosterCell.appendChild(rosterSelect)

  const startCell = document.createElement('td')
  startCell.className = 'start-time-cell'

  const dateInput = document.createElement('input')
  dateInput.type = 'date'
  // Native first line of defense — most browsers refuse to pick an earlier date in the
  // calendar UI at all. Direct keyboard entry can still bypass it, so updateStartValue() below
  // re-checks and clamps on every change regardless of how the value got there.
  if (seasonStartLocal) {
    dateInput.min = seasonStartLocal.date
  }
  if (seasonEndLocal) {
    dateInput.max = seasonEndLocal.date
  }

  const timeSelect = document.createElement('select')
  for (const time of TIME_OF_DAY_OPTIONS) {
    const option = document.createElement('option')
    option.value = time
    option.textContent = time
    timeSelect.appendChild(option)
  }

  // Displayed/edited in the browser's local timezone; converted to/from the bridge's UTC
  // "YYYY-MM-DDTHH:MMZ" convention only here (Phase E.5) — everything else in this file works
  // purely in local terms.
  const localParts = clan.cwl_start_at ? utcStringToLocalParts(clan.cwl_start_at) : null
  if (localParts) {
    dateInput.value = localParts.date
    timeSelect.value = localParts.time
  } else {
    timeSelect.value = '08:00' // just a sane starting position — doesn't set anything until a date is chosen
  }
  startCell.append(dateInput, timeSelect)

  function syncDisabledState(): void {
    const disabled = !checkbox.checked || isLockedByOwner
    rosterSelect.disabled = disabled
    dateInput.disabled = disabled
    timeSelect.disabled = disabled
    row.classList.toggle('inactive', !checkbox.checked)
  }

  function updateStartValue(): void {
    if (!dateInput.value) {
      clan.cwl_start_at = null
      return
    }
    const candidateUtc = localPartsToUtcString(dateInput.value, timeSelect.value)
    const candidateMs = new Date(candidateUtc).getTime()
    // CWL never starts before the season's official 1st-of-month 08:00 UTC — clamp instead of
    // silently persisting an earlier value (the native `min` above stops most attempts, but
    // typed-in dates and the boundary day's earlier time-of-day options need this too).
    if (candidateMs < seasonStartMs && seasonStartLocal) {
      dateInput.value = seasonStartLocal.date
      timeSelect.value = seasonStartLocal.time
      clan.cwl_start_at = seasonStartUtc
      return
    }
    // A clan can't switch in more than 48h after the official start either — same clamp,
    // same reasoning, just the ceiling instead of the floor.
    if (candidateMs > seasonEndMs && seasonEndLocal) {
      dateInput.value = seasonEndLocal.date
      timeSelect.value = seasonEndLocal.time
      clan.cwl_start_at = seasonEndUtc
      return
    }
    clan.cwl_start_at = candidateUtc
  }

  checkbox.addEventListener('change', () => {
    clan.participating = checkbox.checked
    syncDisabledState()
  })
  rosterSelect.addEventListener('change', () => {
    clan.roster_size = Number(rosterSelect.value)
  })
  dateInput.addEventListener('change', updateStartValue)
  timeSelect.addEventListener('change', updateStartValue)
  syncDisabledState()

  // Inner flex row (2026-08-18, live-testing feedback: the checkbox and "Remove" button were
  // landing on visibly different lines/heights as plain inline siblings inside the `<td>`) — a
  // `<td>` must stay `display: table-cell` to keep participating correctly in the table's own
  // row/column layout, so the flex centering lives on this inner wrapper instead of the cell
  // itself; see `.checkbox-cell-inner` in index.html.
  const checkboxCellInner = document.createElement('div')
  checkboxCellInner.className = 'checkbox-cell-inner'
  checkboxCell.appendChild(checkboxCellInner)
  checkboxCellInner.appendChild(checkbox)

  // Rule f's "Remove" button — placed right of the checkbox (project owner's spec), vertically
  // centered against it (checkbox-cell-inner's own `align-items: center`), only for a real guest
  // clan (never a family clan — is_guest is false/undefined for those, and for a
  // freshly-added-but-unsaved guest clan, see ClanConfig.is_guest's own comment for why that's
  // deliberate too).
  if (clan.is_guest) {
    const removeButton = document.createElement('button')
    removeButton.className = 'guest-clan-remove-button'
    removeButton.textContent = 'Remove'
    removeButton.title = `Remove ${clan.name} from this season entirely — deletes it from the roster and removes its players from the enrollment pool`

    removeButton.addEventListener('click', () => {
      // Destructive action — no window.confirm() (this runs in a sandboxed Activity iframe, and
      // every other confirmation in this file is an inline UI element, never a browser dialog,
      // matching the guest-clan-add "already shared, add anyway?" pattern above).
      removeButton.remove()
      const confirmLabel = document.createElement('span')
      confirmLabel.className = 'guest-result-note'
      confirmLabel.textContent = 'Remove clan and its players from the pool?'
      const yesButton = document.createElement('button')
      yesButton.className = 'guest-clan-remove-button'
      yesButton.textContent = 'Yes'
      const cancelButton = document.createElement('button')
      cancelButton.className = 'guest-clan-remove-button'
      cancelButton.textContent = 'Cancel'
      yesButton.addEventListener('click', async () => {
        yesButton.disabled = true
        cancelButton.disabled = true
        try {
          await onGuestClanRemove(clan, row)
          // Success: the caller already removed `row` from the DOM — nothing left to update here.
        } catch (err) {
          console.error(err)
          confirmLabel.textContent = `Failed to remove: ${(err as Error).message}`
          yesButton.disabled = false
          cancelButton.disabled = false
        }
      })
      cancelButton.addEventListener('click', () => {
        confirmLabel.remove()
        yesButton.remove()
        cancelButton.remove()
        checkboxCellInner.appendChild(removeButton)
      })
      checkboxCellInner.append(confirmLabel, yesButton, cancelButton)
    })

    checkboxCellInner.appendChild(removeButton)
  }

  row.append(checkboxCell, nameCell, tierCell, rosterCell, startCell)
  return row
}
