import type {
  PlayerPrefsChange,
  PlayerPrefsPayload,
  PlayerPrefsSeasonRow,
  PlayerPrefsStatusAction,
} from './types'
import { utcStringToLocalParts } from './timeFormat'
import { STATUS_ICON, isVisibleStatus, statusLabel } from './signupStatus'
import type { Translator } from './i18n'

// CoC's real league ladder — duplicated verbatim from qapbot/ui_cwl_roster.py's CWL_LEAGUE_RANKS
// (see that list's own comment pointing back here) rather than fetched, since it is static and
// CoC-defined. If that list ever changes (it did once already, tracker #0047 — CoC added the
// Legend/Titan tiers above Champion and this copy went stale), update this one too.
const CWL_LEAGUE_RANKS: string[] = [
  'Legend League',
  'Titan League I', 'Titan League II', 'Titan League III',
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

function buildLeagueSelect(current: string | null, t: Translator): HTMLSelectElement {
  const select = document.createElement('select')
  select.title = t('col_league_tooltip')
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
  select.title = t('col_participation_tooltip')
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
 * Reuses clanConfigTable.ts's table/header CSS. Every control on the screen auto-saves the
 * instant it changes (tracker #0052, live UX feedback: a separate Save button positioned under
 * only block I read as "block II auto-saves but block I doesn't," which was confusing — and
 * wasn't even accurate, since block II's status buttons always were immediate actions). There is
 * exactly one button on the whole screen, a single "Close" at the very bottom, common to both
 * blocks — its only job is closing the Activity; every change is already persisted by the time a
 * viewer could click it.
 *
 * No optimistic updates anywhere: onSave/onStatusChange always return the freshly rebuilt
 * payload, and this function re-renders the ENTIRE screen from that fresh payload after every
 * single successful change — never a local DOM patch — so what's on screen always matches the DB,
 * never a guess about what changed. A change to one row therefore also discards any other
 * not-yet-resolved edit still in flight elsewhere on the screen, same as a real page reload would
 * (an accepted, minor rough edge, not a bug).
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

    const closeButton = document.createElement('button')
    closeButton.textContent = t('close')
    closeButton.className = 'cancel-button'
    closeButton.addEventListener('click', () => onClose('Closed'))

    if (current.accounts.length === 0) {
      const notice = document.createElement('div')
      notice.className = 'header'
      notice.textContent = t('no_linked_accounts')
      container.appendChild(notice)

      const emptyFooter = document.createElement('div')
      emptyFooter.className = 'footer sticky-footer'
      emptyFooter.appendChild(closeButton)
      container.appendChild(emptyFooter)
      return
    }

    renderBlockOne(container, current, t, onSave, render)
    renderBlockTwo(container, current, t, onStatusChange, render)

    const footer = document.createElement('div')
    footer.className = 'footer sticky-footer'
    footer.appendChild(closeButton)
    container.appendChild(footer)
  }

  render(payload)
}

function renderBlockOne(
  container: HTMLElement,
  payload: PlayerPrefsPayload,
  t: Translator,
  onSave: (changes: PlayerPrefsChange[]) => Promise<PlayerPrefsPayload>,
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

  // tracker #0083: mouse-over help for the three settings whose purpose isn't obvious from the
  // column label alone — plain native `title` tooltips, the same lightweight pattern already used
  // for the status-action buttons below and throughout enrollmentBoard.ts.
  const COLUMN_TOOLTIPS: Partial<Record<string, string>> = {
    col_league: 'col_league_tooltip',
    col_participation: 'col_participation_tooltip',
    col_dm_anyway: 'col_dm_anyway_tooltip',
  }

  const thead = document.createElement('thead')
  const headRow = document.createElement('tr')
  for (const key of ['col_account', 'col_league', 'col_participation', 'col_dm_anyway']) {
    const th = document.createElement('th')
    th.textContent = t(key)
    const tooltipKey = COLUMN_TOOLTIPS[key]
    if (tooltipKey) th.title = t(tooltipKey)
    headRow.appendChild(th)
  }
  thead.appendChild(headRow)
  table.appendChild(thead)

  const tbody = document.createElement('tbody')

  // "Apply to all my accounts" — pinned above the per-account rows, its own Apply button (posts
  // a single {player_tag: null} change). Every other row auto-saves per control below; this one
  // keeps an explicit button since "apply" is a deliberate bulk overwrite of every account at
  // once, not a single field on one row — worth a distinct, separate confirmation click.
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
  applyDmCheckbox.title = t('col_dm_anyway_tooltip')
  applyDmCheckbox.disabled = applyModeSelect.value !== 'optout'
  const applyButton = document.createElement('button')
  applyButton.textContent = t('apply')
  applyButton.className = 'status-action-button'
  applyButton.title = t('apply_tooltip')
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

  const rowStatus = document.createElement('div')
  rowStatus.className = 'block-status'

  for (const account of payload.accounts) {
    const row = document.createElement('tr')

    const nameCell = document.createElement('td')
    nameCell.textContent = accountLabel(account.player_tag, account.player_name)
    row.appendChild(nameCell)

    const leagueCell = document.createElement('td')
    const leagueSelect = buildLeagueSelect(account.preferred_league_rank, t)
    leagueCell.appendChild(leagueSelect)
    row.appendChild(leagueCell)

    const modeCell = document.createElement('td')
    const modeSelect = buildModeSelect(account.mode, t)
    modeCell.appendChild(modeSelect)
    row.appendChild(modeCell)

    const dmCell = document.createElement('td')
    const dmInner = document.createElement('div')
    dmInner.className = 'checkbox-cell-inner'
    const dmCheckbox = document.createElement('input')
    dmCheckbox.type = 'checkbox'
    dmCheckbox.title = t('col_dm_anyway_tooltip')
    dmCheckbox.checked = account.send_dm_anyway
    dmCheckbox.disabled = account.mode !== 'optout'
    dmInner.appendChild(dmCheckbox)
    dmCell.appendChild(dmInner)
    row.appendChild(dmCell)

    // Auto-save (tracker #0052): every control in this row posts the row's full current state
    // the instant it changes — no separate Save click anywhere on this screen any more. Reads
    // straight off the DOM elements' live values rather than a separately-tracked working-state
    // object, since there's nothing left that needs to survive between two different controls'
    // changes (each change is independently, immediately persisted).
    const controls = [leagueSelect, modeSelect, dmCheckbox]
    const saveThisRow = async (): Promise<void> => {
      controls.forEach((c) => (c.disabled = true))
      rowStatus.textContent = t('saving')
      rowStatus.className = 'block-status'
      try {
        const fresh = await onSave([
          {
            player_tag: account.player_tag,
            mode: modeSelect.value as PrefsMode,
            send_dm_anyway: dmCheckbox.checked,
            league_rank: leagueSelect.value || null,
            rank_provided: true,
          },
        ])
        rerender(fresh)
      } catch (err) {
        console.error(err)
        rowStatus.textContent = t('save_failed')
        rowStatus.className = 'block-status error'
        controls.forEach((c) => (c.disabled = false))
        dmCheckbox.disabled = modeSelect.value !== 'optout'
      }
    }

    leagueSelect.addEventListener('change', () => void saveThisRow())
    dmCheckbox.addEventListener('change', () => void saveThisRow())
    modeSelect.addEventListener('change', () => {
      dmCheckbox.disabled = modeSelect.value !== 'optout'
      if (dmCheckbox.disabled) dmCheckbox.checked = false
      void saveThisRow()
    })

    tbody.appendChild(row)
  }

  table.appendChild(tbody)
  scroll.appendChild(table)
  block.appendChild(scroll)
  block.appendChild(applyStatus)
  block.appendChild(rowStatus)

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
    if (key === 'col_status') th.title = t('col_status_tooltip')
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
    // 'auto_confirmed' is the one status whose label alone doesn't say enough — see the "I'm in"
    // button's own confirm_tooltip_auto_confirmed for the same nuance from the action side.
    if (row.signup_status === 'auto_confirmed') icon.title = t('status_tooltip_auto_confirmed')
    statusInner.appendChild(icon)
    const label = document.createElement('span')
    label.textContent = statusLabel(row.signup_status, t)
    if (row.signup_status === 'auto_confirmed') label.title = t('status_tooltip_auto_confirmed')
    statusInner.appendChild(label)
  }

  if (enrollmentOpen) {
    const currentStatus = row.signup_status
    // null = no cwl_signups row for this event yet, i.e. the account was never invited
    // (tracker #0054, live bug report) — both buttons stay disabled until an invite creates
    // a row to confirm/decline against.
    const notInvited = currentStatus === null

    const imInButton = document.createElement('button')
    imInButton.textContent = t('button_im_in')
    imInButton.className = 'status-action-button'
    // auto_confirmed is left CLICKABLE, unlike a real 'confirmed' (tracker #0051, live bug
    // report): it was seeded automatically by a standing opt-in preference, not a genuine click,
    // so the member can still turn it into a real confirmation — which the tooltip below explains
    // is preferable, since it gives the clan leader more clarity than an automatic one.
    imInButton.disabled = notInvited || currentStatus === 'confirmed'
    if (currentStatus === 'auto_confirmed') {
      imInButton.title = t('confirm_tooltip_auto_confirmed')
    } else if (notInvited) {
      imInButton.title = t('status_action_tooltip_not_invited')
    } else if (currentStatus === 'confirmed') {
      imInButton.title = t('confirm_tooltip_already_confirmed')
    } else {
      imInButton.title = t('confirm_tooltip_default')
    }

    const imOutButton = document.createElement('button')
    imOutButton.textContent = t('button_im_out')
    imOutButton.className = 'status-action-button'
    imOutButton.disabled = notInvited || currentStatus === 'declined'
    if (notInvited) {
      imOutButton.title = t('status_action_tooltip_not_invited')
    } else if (currentStatus === 'declined') {
      imOutButton.title = t('optout_tooltip_already_declined')
    } else {
      imOutButton.title = t('optout_tooltip_default')
    }

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
        imInButton.disabled = currentStatus === 'confirmed'
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
