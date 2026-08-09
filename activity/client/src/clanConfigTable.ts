import type { ClanConfig, ClanConfigPayload, PreviousClanConfig } from './types'

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

/** Renders the CWL clan-config table — season dropdown, optional carry-over prompt, then the
 * checkbox / tag / tier / roster-size select / start-time picker per row — into `container`,
 * replacing whatever was there. This is the actual reason this Activity exists: none of these
 * columns fit together in any Discord-native component (see CWL_CLAN_CONFIG_ACTIVITY_PLAN.md's
 * "Context" section).
 *
 * Edits are held in a working copy and only sent anywhere when "Save" is clicked. Both buttons
 * close the Activity via `onClose` when done — Save persists first then closes, Cancel closes
 * immediately without saving anything. No intermediate confirmation screen; closing itself is
 * the confirmation.
 *
 * `onClose(reason)` calls discordSdk.close(RPCCloseCodes.CLOSE_NORMAL, reason) — a real,
 * documented top-level SDK method, confirmed working live (both from Save and Cancel).
 *
 * `onSeasonChange(season)` is called when the season dropdown changes — the caller (main.ts)
 * re-fetches that season's payload and calls this function again; nothing here tries to patch
 * DOM state across a season switch in place.
 */
export function renderClanConfigTable(
  container: HTMLElement,
  payload: ClanConfigPayload,
  onSave: (clans: ClanConfig[]) => Promise<void>,
  onClose: (reason: string) => void,
  onSeasonChange: (season: string) => void,
): void {
  const working: ClanConfig[] = payload.clans.map((c) => ({ ...c }))

  container.innerHTML = ''

  const header = document.createElement('div')
  header.className = 'header'

  const seasonRow = document.createElement('div')
  seasonRow.className = 'season-row'

  const seasonSelect = document.createElement('select')
  seasonSelect.className = 'season-select'
  for (const season of payload.available_seasons) {
    const option = document.createElement('option')
    option.value = season
    option.textContent = season
    option.selected = season === payload.season
    seasonSelect.appendChild(option)
  }
  seasonSelect.addEventListener('change', () => {
    onSeasonChange(seasonSelect.value)
  })

  const statusBadge = document.createElement('span')
  statusBadge.className = 'status-badge'
  statusBadge.textContent = payload.event_status ?? 'draft'

  seasonRow.append(seasonSelect, statusBadge)
  header.appendChild(seasonRow)

  const tzNote = document.createElement('div')
  tzNote.className = 'tz-note'
  const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone
  tzNote.textContent = `Times shown in your local timezone (${timeZone}) — saved as UTC automatically.`
  header.appendChild(tzNote)

  container.appendChild(header)

  // Carry-over prompt (Phase E.4): only shown when this season has no saved config of its own
  // yet AND a previous season has one to offer — an explicit Yes/No, never auto-applied.
  if (payload.carry_over_available && payload.previous_clans) {
    container.appendChild(
      buildCarryOverBanner(payload.carry_over_season, payload.previous_clans, working, () => renderRows()),
    )
  }

  const table = document.createElement('table')
  const thead = document.createElement('thead')
  thead.innerHTML = `
    <tr>
      <th></th>
      <th>Clan</th>
      <th>Tier</th>
      <th>Roster Size</th>
      <th>Start Time (local)</th>
    </tr>
  `
  table.appendChild(thead)

  const tbody = document.createElement('tbody')
  table.appendChild(tbody)
  container.appendChild(table)

  function renderRows(): void {
    tbody.innerHTML = ''
    for (const clan of working) {
      tbody.appendChild(buildRow(clan))
    }
  }
  renderRows()

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
  footer.className = 'footer'
  footer.appendChild(saveButton)
  footer.appendChild(cancelButton)
  footer.appendChild(status)
  container.appendChild(footer)
}

function buildCarryOverBanner(
  carryOverSeason: string | null,
  previousClans: PreviousClanConfig[],
  working: ClanConfig[],
  onApplied: () => void,
): HTMLElement {
  const banner = document.createElement('div')
  banner.className = 'carry-over-banner'

  const text = document.createElement('span')
  text.textContent = `No settings saved yet for this season. Carry over configuration from ${carryOverSeason ?? 'the previous season'}?`
  banner.appendChild(text)

  const buttonRow = document.createElement('div')
  buttonRow.className = 'carry-over-buttons'

  const yesButton = document.createElement('button')
  yesButton.textContent = 'Yes, carry over'
  yesButton.className = 'carry-over-yes'
  yesButton.addEventListener('click', () => {
    const byTag = new Map(previousClans.map((c) => [c.clan_tag, c]))
    for (const clan of working) {
      const prev = byTag.get(clan.clan_tag)
      if (!prev) continue
      clan.participating = true
      clan.roster_size = prev.roster_size
      clan.cwl_start_at = prev.cwl_start_at
    }
    onApplied()
    banner.remove()
  })

  const noButton = document.createElement('button')
  noButton.textContent = 'No, start fresh'
  noButton.className = 'carry-over-no'
  noButton.addEventListener('click', () => banner.remove())

  buttonRow.append(yesButton, noButton)
  banner.appendChild(buttonRow)
  return banner
}

function buildRow(clan: ClanConfig): HTMLTableRowElement {
  const row = document.createElement('tr')

  const checkboxCell = document.createElement('td')
  const checkbox = document.createElement('input')
  checkbox.type = 'checkbox'
  checkbox.checked = clan.participating

  const nameCell = document.createElement('td')
  nameCell.textContent = `${clan.name} (${clan.clan_tag})`

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
    const disabled = !checkbox.checked
    rosterSelect.disabled = disabled
    dateInput.disabled = disabled
    timeSelect.disabled = disabled
    row.classList.toggle('inactive', disabled)
  }

  function updateStartValue(): void {
    clan.cwl_start_at = dateInput.value ? localPartsToUtcString(dateInput.value, timeSelect.value) : null
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
