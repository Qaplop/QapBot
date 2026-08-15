import type { ClanConfig, ClanConfigPayload, GuestSearchResult } from './types'

const ROSTER_SIZES = [5, 15, 30] as const

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
  onGuestPlayerAdd: (
    result: Extract<GuestSearchResult, { type: 'player' }>,
    sendDmNow: boolean,
  ) => Promise<{ dm_sent: boolean }>,
  // Owner-only eviction (2026-08-15) — removes targetGuildId's participation in a shared clan.
  // Independent of Save/onSave, same immediate-action shape as onGuestPlayerAdd: it doesn't
  // touch `working` at all, so nothing here needs a page reload to reflect it beyond re-running
  // this same render with a fresh payload (see main.ts's call site).
  onEvict: (clanTag: string, targetGuildId: string) => Promise<void>,
): void {
  const working: ClanConfig[] = payload.clans.map((c) => ({ ...c }))

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

  // Stick the column-header row directly beneath the topbar (not at the very top — that would
  // overlap/hide under it) so it stays visible too, not just the title. `topBar` is already in
  // the DOM at this point, so its rendered height is real, not a guessed/hardcoded pixel value
  // that would silently drift if the title text or font ever changes.
  const topBarHeight = topBar.getBoundingClientRect().height
  thead.querySelectorAll('th').forEach((th) => {
    ;(th as HTMLElement).style.top = `${topBarHeight}px`
  })

  // CWL itself never starts before the 1st of the season's month at 08:00 UTC (the game's
  // static schedule) — clamp every row's picker to that floor so an admin can't accidentally
  // schedule a clan's switch-over before CWL has even begun. The ceiling (+48h) exists because
  // a clan switching in later than that would miss too much of the war league to make sense —
  // both bounds are enforced identically (native min/max plus JS re-validation on every change).
  const seasonStartUtc = `${payload.season}-01T08:00Z`
  const seasonEndUtc = `${new Date(new Date(seasonStartUtc).getTime() + 48 * 60 * 60 * 1000).toISOString().slice(0, 16)}Z`

  const tbody = document.createElement('tbody')
  for (const clan of working) {
    tbody.appendChild(buildRow(clan, seasonStartUtc, seasonEndUtc, onEvict))
  }
  table.appendChild(tbody)
  container.appendChild(table)

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
  guestsSearchInput.placeholder = 'Search Discord user, player name/tag, or clan…'
  guestsSearchRow.appendChild(guestsSearchInput)
  const guestsDmLabel = document.createElement('label')
  guestsDmLabel.className = 'guests-dm-label'
  const guestsDmCheckbox = document.createElement('input')
  guestsDmCheckbox.type = 'checkbox'
  guestsDmLabel.appendChild(guestsDmCheckbox)
  guestsDmLabel.append('Send enrollment DM immediately (players only)')
  guestsSearchRow.appendChild(guestsDmLabel)
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
          }
          working.push(newClan)
          existingClanTags.add(clanResult.clan_tag)
          const newRow = buildRow(newClan, seasonStartUtc, seasonEndUtc, onEvict)
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
          const sendDmNow = guestsDmCheckbox.checked && !!result.discord_id
          addButton.disabled = true
          guestsStatus.textContent = 'Adding…'
          guestsStatus.className = 'guests-status'
          try {
            const { dm_sent } = await onGuestPlayerAdd(result, sendDmNow)
            guestsStatus.textContent = dm_sent
              ? `Added ${result.player_name} and sent the enrollment DM.`
              : `Added ${result.player_name} to the enrollment pool.`
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

  guestsSearchInput.addEventListener('input', () => {
    if (searchDebounceHandle !== undefined) clearTimeout(searchDebounceHandle)
    const query = guestsSearchInput.value.trim()
    if (!query) {
      guestsResults.innerHTML = ''
      return
    }
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
          console.error(err)
          if (thisRequestId === searchRequestId) {
            guestsStatus.textContent = `Search failed: ${(err as Error).message}`
            guestsStatus.className = 'guests-status error'
          }
        })
    }, 300)
  })

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

  checkboxCell.appendChild(checkbox)
  row.append(checkboxCell, nameCell, tierCell, rosterCell, startCell)
  return row
}
