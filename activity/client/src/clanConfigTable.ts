import type { ClanConfig, ClanConfigPayload } from './types'

const ROSTER_SIZES = [5, 15, 30] as const
// datetime-local's `step` is in seconds; 900s = 15 minutes restricts the native picker's
// minute column to :00/:15/:30/:45 instead of every single minute.
const START_TIME_STEP_SECONDS = 900
// Long enough to actually read "Saved", short enough not to feel like a stuck screen.
const AUTO_CLOSE_DELAY_MS = 1200

/** Renders the real Phase C table — checkbox / tag / tier / roster-size select / start-time
 * picker per row — into `container`, replacing whatever was there. This is the actual reason
 * this Activity exists: none of these five columns fit together in any Discord-native
 * component (see CWL_CLAN_CONFIG_ACTIVITY_PLAN.md's "Context" section).
 *
 * Edits are held in a working copy and only sent anywhere when "Save" is clicked — same
 * working-copy-then-apply pattern as the Discord-side CwlEventSetupView. "Cancel" just
 * re-renders from the untouched original `payload` (no server round-trip needed, since nothing
 * was ever sent until Save).
 *
 * `onClose` calls discordSdk.close() (RPCCloseCodes.CLOSE_NORMAL) — a real SDK method, missed
 * on first pass because it isn't listed under commands/ like everything else; it's a top-level
 * DiscordSDK method that sends a raw CLOSE protocol frame instead of a request/response command.
 */
export function renderClanConfigTable(
  container: HTMLElement,
  payload: ClanConfigPayload,
  onSave: (clans: ClanConfig[]) => Promise<void>,
  onClose: () => void,
): void {
  const working: ClanConfig[] = payload.clans.map((c) => ({ ...c }))

  container.innerHTML = ''

  const header = document.createElement('div')
  header.className = 'header'
  header.textContent = `Season ${payload.season} — ${payload.event_status ?? 'draft'}`
  container.appendChild(header)

  const table = document.createElement('table')
  const thead = document.createElement('thead')
  thead.innerHTML = `
    <tr>
      <th></th>
      <th>Clan</th>
      <th>Tier</th>
      <th>Roster Size</th>
      <th>Start Time (UTC)</th>
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
    renderClanConfigTable(container, payload, onSave, onClose)
  })

  saveButton.addEventListener('click', async () => {
    saveButton.disabled = true
    cancelButton.disabled = true
    status.textContent = 'Saving…'
    status.className = 'save-status'
    try {
      await onSave(working)
      renderSavedState(container, payload, onSave, onClose)
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

/** Post-save confirmation, briefly shown before the Activity closes itself via onClose(). */
function renderSavedState(
  container: HTMLElement,
  payload: ClanConfigPayload,
  onSave: (clans: ClanConfig[]) => Promise<void>,
  onClose: () => void,
): void {
  container.innerHTML = ''

  const message = document.createElement('div')
  message.className = 'saved-message'
  message.textContent = '✓ Saved — closing…'
  container.appendChild(message)

  const editAgainButton = document.createElement('button')
  editAgainButton.textContent = 'Edit again'
  editAgainButton.className = 'cancel-button'

  const closeTimer = window.setTimeout(onClose, AUTO_CLOSE_DELAY_MS)
  editAgainButton.addEventListener('click', () => {
    window.clearTimeout(closeTimer)
    renderClanConfigTable(container, payload, onSave, onClose)
  })
  container.appendChild(editAgainButton)
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
  const startInput = document.createElement('input')
  startInput.type = 'datetime-local'
  startInput.step = String(START_TIME_STEP_SECONDS)
  // Bridge format is "YYYY-MM-DDTHH:MMZ" (always UTC, matching the Discord-side modal's own
  // convention) — datetime-local wants "YYYY-MM-DDTHH:MM" with no timezone suffix at all, and
  // is treated as a plain UTC value here too (never converted to/from the browser's locale).
  if (clan.cwl_start_at) {
    startInput.value = clan.cwl_start_at.replace(/Z$/, '')
  }
  startCell.appendChild(startInput)

  function syncDisabledState(): void {
    const disabled = !checkbox.checked
    rosterSelect.disabled = disabled
    startInput.disabled = disabled
    row.classList.toggle('inactive', disabled)
  }

  checkbox.addEventListener('change', () => {
    clan.participating = checkbox.checked
    syncDisabledState()
  })
  rosterSelect.addEventListener('change', () => {
    clan.roster_size = Number(rosterSelect.value)
  })
  startInput.addEventListener('change', () => {
    clan.cwl_start_at = startInput.value ? `${startInput.value}Z` : null
  })
  syncDisabledState()

  checkboxCell.appendChild(checkbox)
  row.append(checkboxCell, nameCell, tierCell, rosterCell, startCell)
  return row
}
