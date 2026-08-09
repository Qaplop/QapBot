import type { ClanConfig, ClanConfigPayload } from './types'

const ROSTER_SIZES = [5, 15, 30] as const

/** Renders the real Phase C table — checkbox / tag / tier / roster-size select / start-time
 * picker per row — into `container`, replacing whatever was there. This is the actual reason
 * this Activity exists: none of these five columns fit together in any Discord-native
 * component (see CWL_CLAN_CONFIG_ACTIVITY_PLAN.md's "Context" section).
 *
 * Edits are held in a working copy (`working`) and only sent anywhere when "Save" is clicked —
 * same working-copy-then-apply pattern as the Discord-side CwlEventSetupView.
 */
export function renderClanConfigTable(
  container: HTMLElement,
  payload: ClanConfigPayload,
  onSave: (clans: ClanConfig[]) => Promise<void>,
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
  saveButton.addEventListener('click', async () => {
    saveButton.disabled = true
    status.textContent = 'Saving…'
    status.className = 'save-status'
    try {
      await onSave(working)
      status.textContent = 'Saved ✓'
      status.className = 'save-status success'
    } catch (err) {
      console.error(err)
      status.textContent = `Save failed: ${(err as Error).message}`
      status.className = 'save-status error'
    } finally {
      saveButton.disabled = false
    }
  })

  const footer = document.createElement('div')
  footer.className = 'footer'
  footer.appendChild(saveButton)
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
  const startInput = document.createElement('input')
  startInput.type = 'datetime-local'
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
