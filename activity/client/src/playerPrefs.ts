import type {
  PlayerPrefsAccount,
  PlayerPrefsChange,
  PlayerPrefsPayload,
  PlayerPrefsSeasonRow,
  PlayerPrefsStatusAction,
} from './types'
import { utcStringToLocalParts } from './timeFormat'
import { STATUS_ICON, isVisibleStatus, statusLabel } from './signupStatus'
import type { Translator } from './i18n'

// CoC's real league ladder — duplicated verbatim from qapbot/ui_cwl_roster.py:26-34 (see that
// list's own comment pointing back here) rather than fetched, since it is static and CoC-defined.
// If that list ever changes, update this one too.
const CWL_LEAGUE_RANKS: string[] = [
  'Champion League I', 'Champion League II', 'Champion League III',
  'Master League I', 'Master League II', 'Master League III',
  'Crystal League I', 'Crystal League II', 'Crystal League III',
  'Gold League I', 'Gold League II', 'Gold League III',
  'Silver League I', 'Silver League II', 'Silver League III',
  'Bronze League I', 'Bronze League II', 'Bronze League III',
  'Unranked',
]

type PrefsMode = 'none' | 'optin' | 'optout'

// POST /api/cwl/player-prefs/status's four possible failure codes (qapbot/web_bridge.py's
// _PLAYER_PREFS_STATUS_ERROR_HTTP_STATUS) — each one is also a real cwl.template.* key, reused
// as-is (Phase 6f). Anything else (e.g. "invalid request body") isn't a translation key at all
// and is shown verbatim.
const STATUS_ERROR_CODES = new Set(['db_unavailable', 'no_longer_valid', 'not_your_signup', 'signup_closed'])

function accountLabel(tag: string, name: string | null): string {
  return name ? `${name} (${tag})` : tag
}

/** One row's local, unsaved working state for block I — mirrors PlayerPrefsAccount's editable
 * fields (everything except `verified`, which is display-only). */
type AccountWorkingState = { mode: PrefsMode; sendDmAnyway: boolean; leagueRank: string | null }

function buildLeagueSelect(current: string | null, t: Translator): HTMLSelectElement {
  const select = document.createElement('select')
  const noPref = document.createElement('option')
  noPref.value = ''
  noPref.textContent = t('no_preference')
  select.appendChild(noPref)
  for (const rank of CWL_LEAGUE_RANKS) {
    const option = document.createElement('option')
    option.value = rank
    option.textContent = rank
    select.appendChild(option)
  }
  select.value = current ?? ''
  return select
}

function buildModeSelect(current: PrefsMode, t: Translator): HTMLSelectElement {
  const select = document.createElement('select')
  const options: [PrefsMode, string][] = [
    ['none', t('mode_none')],
    ['optin', t('mode_optin')],
    ['optout', t('mode_optout')],
  ]
  for (const [value, label] of options) {
    const option = document.createElement('option')
    option.value = value
    option.textContent = label
    select.appendChild(option)
  }
  select.value = current
  return select
}

/** Renders the Player CWL Settings Hub's Activity screen — block I (standing preferences, every
 * linked account plus a bulk "apply to all" row) and block II (this season's invitation status,
 * with interactive I'm in / I'm out buttons). See plans/cwl-personal-hub.md Phase 5e.
 *
 * Reuses clanConfigTable.ts's table/header/footer CSS and its "hold edits in a working copy,
 * only persist on Save, Save closes the Activity" discipline for block I. Block II's status
 * change is a separate, immediate action (like clanConfigTable's guest actions) — it POSTs the
 * moment a button is clicked, never batched with block I's Save.
 *
 * No optimistic updates anywhere: both onSave and onStatusChange return the freshly rebuilt
 * payload, and this function always re-renders itself from that fresh payload rather than
 * guessing what changed — a status click therefore also discards any not-yet-saved block I
 * edits, exactly like a real page reload would (an accepted, minor rough edge, not a bug).
 */
export function renderPlayerPrefs(
  container: HTMLElement,
  payload: PlayerPrefsPayload,
  t: Translator,
  onSave: (changes: PlayerPrefsChange[]) => Promise<PlayerPrefsPayload>,
  onStatusChange: (playerTag: string, action: PlayerPrefsStatusAction) => Promise<PlayerPrefsPayload>,
  onClose: (reason: string) => void,
): void {
  const render = (current: PlayerPrefsPayload): void => {
    container.innerHTML = ''

    if (current.accounts.length === 0) {
      const notice = document.createElement('div')
      notice.className = 'header'
      notice.textContent = t('no_linked_accounts')
      container.appendChild(notice)

      const footer = document.createElement('div')
      footer.className = 'footer sticky-footer'
      const closeButton = document.createElement('button')
      closeButton.textContent = t('close')
      closeButton.className = 'cancel-button'
      closeButton.addEventListener('click', () => onClose('Closed'))
      footer.appendChild(closeButton)
      container.appendChild(footer)
      return
    }

    renderBlockOne(container, current, t, onSave, onClose, render)
    renderBlockTwo(container, current, t, onStatusChange, render)
  }

  render(payload)
}

function renderBlockOne(
  container: HTMLElement,
  payload: PlayerPrefsPayload,
  t: Translator,
  onSave: (changes: PlayerPrefsChange[]) => Promise<PlayerPrefsPayload>,
  onClose: (reason: string) => void,
  rerender: (fresh: PlayerPrefsPayload) => void,
): void {
  const block = document.createElement('div')
  block.className = 'prefs-block'

  const header = document.createElement('div')
  header.className = 'header'
  header.textContent = t('prefs_title')
  block.appendChild(header)

  const description = document.createElement('div')
  description.className = 'prefs-description'
  description.textContent = t('prefs_description')
  block.appendChild(description)

  const scroll = document.createElement('div')
  scroll.className = 'table-scroll'
  const table = document.createElement('table')

  const thead = document.createElement('thead')
  const headRow = document.createElement('tr')
  for (const key of ['col_account', 'col_league', 'col_participation', 'col_dm_anyway']) {
    const th = document.createElement('th')
    th.textContent = t(key)
    headRow.appendChild(th)
  }
  thead.appendChild(headRow)
  table.appendChild(thead)

  const tbody = document.createElement('tbody')

  // Working state per account, edited in place and only sent on Save — same discipline as
  // clanConfigTable.ts's `working` copy.
  const working = new Map<string, AccountWorkingState>(
    payload.accounts.map((a) => [
      a.player_tag,
      { mode: a.mode, sendDmAnyway: a.send_dm_anyway, leagueRank: a.preferred_league_rank },
    ]),
  )

  // "Apply to all my accounts" — pinned above the per-account rows, its own immediate-action
  // Apply button (posts a single {player_tag: null} change right away, independent of the
  // footer's Save button below — see this function's own docstring).
  const applyRow = document.createElement('tr')
  applyRow.className = 'apply-row'

  const applyLabelCell = document.createElement('td')
  applyLabelCell.textContent = t('apply_to_all')
  applyRow.appendChild(applyLabelCell)

  const applyLeagueCell = document.createElement('td')
  const applyLeagueSelect = buildLeagueSelect(null, t)
  applyLeagueCell.appendChild(applyLeagueSelect)
  applyRow.appendChild(applyLeagueCell)

  const applyModeCell = document.createElement('td')
  const applyModeSelect = buildModeSelect('none', t)
  applyModeCell.appendChild(applyModeSelect)
  applyRow.appendChild(applyModeCell)

  const applyDmCell = document.createElement('td')
  const applyDmInner = document.createElement('div')
  applyDmInner.className = 'checkbox-cell-inner'
  const applyDmCheckbox = document.createElement('input')
  applyDmCheckbox.type = 'checkbox'
  applyDmCheckbox.disabled = applyModeSelect.value !== 'optout'
  const applyButton = document.createElement('button')
  applyButton.textContent = t('apply')
  applyButton.className = 'status-action-button'
  applyModeSelect.addEventListener('change', () => {
    applyDmCheckbox.disabled = applyModeSelect.value !== 'optout'
    if (applyDmCheckbox.disabled) applyDmCheckbox.checked = false
  })
  applyDmInner.appendChild(applyDmCheckbox)
  applyDmInner.appendChild(applyButton)
  applyDmCell.appendChild(applyDmInner)
  applyRow.appendChild(applyDmCell)

  tbody.appendChild(applyRow)

  const applyStatus = document.createElement('div')
  applyStatus.className = 'block-status'

  applyButton.addEventListener('click', async () => {
    applyButton.disabled = true
    applyStatus.textContent = t('saving')
    applyStatus.className = 'block-status'
    try {
      const fresh = await onSave([
        {
          player_tag: null,
          mode: applyModeSelect.value as PrefsMode,
          send_dm_anyway: applyDmCheckbox.checked,
          league_rank: applyLeagueSelect.value || null,
          rank_provided: true,
        },
      ])
      rerender(fresh)
    } catch (err) {
      console.error(err)
      applyStatus.textContent = (err as Error).message
      applyStatus.className = 'block-status error'
      applyButton.disabled = false
    }
  })

  for (const account of payload.accounts) {
    const state = working.get(account.player_tag)!
    const row = document.createElement('tr')

    const nameCell = document.createElement('td')
    nameCell.textContent = accountLabel(account.player_tag, account.player_name)
    row.appendChild(nameCell)

    const leagueCell = document.createElement('td')
    const leagueSelect = buildLeagueSelect(state.leagueRank, t)
    leagueSelect.addEventListener('change', () => {
      state.leagueRank = leagueSelect.value || null
    })
    leagueCell.appendChild(leagueSelect)
    row.appendChild(leagueCell)

    const modeCell = document.createElement('td')
    const modeSelect = buildModeSelect(state.mode, t)
    modeCell.appendChild(modeSelect)
    row.appendChild(modeCell)

    const dmCell = document.createElement('td')
    const dmInner = document.createElement('div')
    dmInner.className = 'checkbox-cell-inner'
    const dmCheckbox = document.createElement('input')
    dmCheckbox.type = 'checkbox'
    dmCheckbox.checked = state.sendDmAnyway
    dmCheckbox.disabled = state.mode !== 'optout'
    dmCheckbox.addEventListener('change', () => {
      state.sendDmAnyway = dmCheckbox.checked
    })
    modeSelect.addEventListener('change', () => {
      state.mode = modeSelect.value as PrefsMode
      dmCheckbox.disabled = state.mode !== 'optout'
      if (dmCheckbox.disabled) {
        dmCheckbox.checked = false
        state.sendDmAnyway = false
      }
    })
    dmInner.appendChild(dmCheckbox)
    dmCell.appendChild(dmInner)
    row.appendChild(dmCell)

    tbody.appendChild(row)
  }

  table.appendChild(tbody)
  scroll.appendChild(table)
  block.appendChild(scroll)
  block.appendChild(applyStatus)

  const status = document.createElement('span')
  status.className = 'save-status'

  const saveButton = document.createElement('button')
  saveButton.textContent = t('save')
  saveButton.className = 'save-button'

  const closeButton = document.createElement('button')
  closeButton.textContent = t('close')
  closeButton.className = 'cancel-button'
  closeButton.addEventListener('click', () => onClose('Cancelled'))

  saveButton.addEventListener('click', async () => {
    saveButton.disabled = true
    closeButton.disabled = true
    status.textContent = t('saving')
    status.className = 'save-status'
    try {
      const changes: PlayerPrefsChange[] = payload.accounts.map((a) => {
        const state = working.get(a.player_tag)!
        return {
          player_tag: a.player_tag,
          mode: state.mode,
          send_dm_anyway: state.sendDmAnyway,
          league_rank: state.leagueRank,
          rank_provided: true,
        }
      })
      await onSave(changes)
      onClose('Saved')
    } catch (err) {
      console.error(err)
      status.textContent = t('save_failed')
      status.className = 'save-status error'
      saveButton.disabled = false
      closeButton.disabled = false
    }
  })

  const footer = document.createElement('div')
  footer.className = 'footer sticky-footer'
  footer.appendChild(saveButton)
  footer.appendChild(closeButton)
  footer.appendChild(status)
  block.appendChild(footer)

  container.appendChild(block)
}

function renderBlockTwo(
  container: HTMLElement,
  payload: PlayerPrefsPayload,
  t: Translator,
  onStatusChange: (playerTag: string, action: PlayerPrefsStatusAction) => Promise<PlayerPrefsPayload>,
  rerender: (fresh: PlayerPrefsPayload) => void,
): void {
  const block = document.createElement('div')
  block.className = 'prefs-block'

  if (payload.season === null) {
    const notice = document.createElement('div')
    notice.className = 'prefs-description'
    notice.textContent = t('no_season')
    block.appendChild(notice)
    container.appendChild(block)
    return
  }

  const header = document.createElement('div')
  header.className = 'header'
  header.textContent = t('season_title', { season: payload.season })
  block.appendChild(header)

  const tzNote = document.createElement('div')
  tzNote.className = 'tz-note'
  tzNote.textContent = t('tz_note')
  block.appendChild(tzNote)

  const enrollmentOpen = payload.event_status === 'signup_open'

  const scroll = document.createElement('div')
  scroll.className = 'table-scroll'
  const table = document.createElement('table')

  const thead = document.createElement('thead')
  const headRow = document.createElement('tr')
  for (const key of ['col_account', 'col_status', 'col_clan', 'col_tier', 'col_start']) {
    const th = document.createElement('th')
    th.textContent = t(key)
    headRow.appendChild(th)
  }
  thead.appendChild(headRow)
  table.appendChild(thead)

  const tbody = document.createElement('tbody')

  const blockStatus = document.createElement('div')
  blockStatus.className = 'block-status'

  const accountNames = new Map(payload.accounts.map((a) => [a.player_tag, a.player_name]))

  for (const row of payload.season_rows) {
    tbody.appendChild(buildSeasonRow(row, accountNames.get(row.player_tag) ?? null, t, enrollmentOpen, onStatusChange, rerender, blockStatus))
  }

  table.appendChild(tbody)
  scroll.appendChild(table)
  block.appendChild(scroll)

  if (!enrollmentOpen) {
    const note = document.createElement('div')
    note.className = 'block-status'
    note.textContent = t('enrollment_not_open')
    block.appendChild(note)
  }

  block.appendChild(blockStatus)
  container.appendChild(block)
}

function buildSeasonRow(
  row: PlayerPrefsSeasonRow,
  accountName: string | null,
  t: Translator,
  enrollmentOpen: boolean,
  onStatusChange: (playerTag: string, action: PlayerPrefsStatusAction) => Promise<PlayerPrefsPayload>,
  rerender: (fresh: PlayerPrefsPayload) => void,
  blockStatus: HTMLElement,
): HTMLTableRowElement {
  const tr = document.createElement('tr')

  const nameCell = document.createElement('td')
  nameCell.textContent = accountLabel(row.player_tag, accountName ?? row.player_name)
  tr.appendChild(nameCell)

  const statusCell = document.createElement('td')
  const statusInner = document.createElement('div')
  statusInner.className = 'status-cell-inner'

  if (isVisibleStatus(row.signup_status)) {
    const icon = document.createElement('img')
    icon.className = 'status-icon'
    icon.src = STATUS_ICON[row.signup_status]
    icon.alt = statusLabel(row.signup_status, t)
    statusInner.appendChild(icon)
    const label = document.createElement('span')
    label.textContent = statusLabel(row.signup_status, t)
    statusInner.appendChild(label)
  }

  if (enrollmentOpen) {
    const currentStatus = row.signup_status
    const imInButton = document.createElement('button')
    imInButton.textContent = t('button_im_in')
    imInButton.className = 'status-action-button'
    imInButton.disabled = currentStatus === 'confirmed' || currentStatus === 'auto_confirmed'

    const imOutButton = document.createElement('button')
    imOutButton.textContent = t('button_im_out')
    imOutButton.className = 'status-action-button'
    imOutButton.disabled = currentStatus === 'declined'

    const fireStatusChange = async (action: PlayerPrefsStatusAction): Promise<void> => {
      imInButton.disabled = true
      imOutButton.disabled = true
      blockStatus.textContent = ''
      blockStatus.className = 'block-status'
      try {
        const fresh = await onStatusChange(row.player_tag, action)
        rerender(fresh)
      } catch (err) {
        console.error(err)
        const code = (err as Error).message
        blockStatus.textContent = STATUS_ERROR_CODES.has(code) ? t(code) : code
        blockStatus.className = 'block-status error'
        imInButton.disabled = currentStatus === 'confirmed' || currentStatus === 'auto_confirmed'
        imOutButton.disabled = currentStatus === 'declined'
      }
    }

    imInButton.addEventListener('click', () => void fireStatusChange('confirm'))
    imOutButton.addEventListener('click', () => void fireStatusChange('optout'))

    statusInner.appendChild(imInButton)
    statusInner.appendChild(imOutButton)
  }

  statusCell.appendChild(statusInner)
  tr.appendChild(statusCell)

  const clanCell = document.createElement('td')
  clanCell.textContent = row.assigned_clan_tag
    ? (row.assigned_clan_name ?? row.assigned_clan_tag)
    : t('unassigned')
  tr.appendChild(clanCell)

  const tierCell = document.createElement('td')
  tierCell.textContent = row.assigned_clan_tier ?? '—'
  tr.appendChild(tierCell)

  const startCell = document.createElement('td')
  const parts = row.assigned_clan_start_at ? utcStringToLocalParts(row.assigned_clan_start_at) : null
  startCell.textContent = parts ? `${parts.date} ${parts.time}` : '—'
  tr.appendChild(startCell)

  return tr
}
