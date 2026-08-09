import type { ClanConfig, ClanConfigPayload } from './types'

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
 */
export function renderClanConfigTable(
  container: HTMLElement,
  payload: ClanConfigPayload,
  onSave: (clans: ClanConfig[]) => Promise<void>,
  onClose: (reason: string) => void,
): void {
  const working: ClanConfig[] = payload.clans.map((c) => ({ ...c }))

  container.innerHTML = ''

  const header = document.createElement('div')
  header.className = 'header'
  header.textContent = `Season ${payload.season} — ${payload.event_status ?? 'draft'}`
  container.appendChild(header)

  const tzNote = document.createElement('div')
  tzNote.className = 'tz-note'
  const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone
  tzNote.textContent = `Times shown in your local timezone (${timeZone}) — saved as UTC automatically.`
  container.appendChild(tzNote)

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
  for (const clan of working) {
    tbody.appendChild(buildRow(clan))
  }
  table.appendChild(tbody)
  container.appendChild(table)

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
