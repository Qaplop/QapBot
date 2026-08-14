import gcheckIconUrl from './assets/gcheck.svg'
import pendingIconUrl from './assets/pending.svg'
import redxIconUrl from './assets/redx.svg'
import unlinkedIconUrl from './assets/unlinked.svg'
import type { EnrollmentPayload, EnrollmentPlayer } from './types'

type SortOrder = 'th' | 'skill' | 'alpha'

type VisibleStatus = 'pending' | 'confirmed' | 'declined'

// Only the three statuses a member's own DM response can actually produce are ever shown — the
// board never lets the clan lead alter a signup status itself (live-testing feedback,
// 2026-08-14: assignment is drag-and-drop only, see renderEnrollmentBoard's docstring).
// Anything else (no signup row yet, or a legacy 'withdrawn' value) shows no icon at all.
const STATUS_ICON: Record<VisibleStatus, string> = {
  pending: pendingIconUrl,
  confirmed: gcheckIconUrl,
  declined: redxIconUrl,
}
const STATUS_LABEL: Record<VisibleStatus, string> = {
  pending: 'Pending',
  confirmed: 'Confirmed',
  declined: 'Declined',
}
const UNLINKED_LABEL = 'Not Linked'

const EVENT_STATUS_LABEL: Record<string, string> = {
  draft: 'Draft',
  signup_open: 'Signup Open',
  finalized: 'Finalized',
  announced: 'Announced',
  cancelled: 'Cancelled',
}

function formatEventStatus(status: string | null): string {
  if (!status) return EVENT_STATUS_LABEL.draft
  return EVENT_STATUS_LABEL[status] ?? status
}

function isVisibleStatus(status: EnrollmentPlayer['signup_status']): status is VisibleStatus {
  return status === 'pending' || status === 'confirmed' || status === 'declined'
}

function displayName(player: EnrollmentPlayer): string {
  return player.player_name || player.player_tag
}

function sortPlayers(players: EnrollmentPlayer[], order: SortOrder): EnrollmentPlayer[] {
  const byName = (a: EnrollmentPlayer, b: EnrollmentPlayer) => displayName(a).localeCompare(displayName(b))
  return [...players].sort((a, b) => {
    if (order === 'th') {
      const diff = (b.th_level ?? -1) - (a.th_level ?? -1)
      return diff !== 0 ? diff : byName(a, b)
    }
    if (order === 'skill') {
      const diff = (b.skill_score ?? -1) - (a.skill_score ?? -1)
      return diff !== 0 ? diff : byName(a, b)
    }
    return byName(a, b)
  })
}

const COLUMN_DRAG_TYPE = 'application/x-cwl-column-index'

/**
 * Renders the CWL "Manage Assignment" board — participating clans as drag-and-drop columns plus
 * an "Unassigned" pool, each player a compact card: TH icon + level, name, skill score, and a
 * read-only signup-status icon (CWL_ROSTER_PLANNING_PLAN.md "Manage Enrollment" slice 4).
 *
 * There is no Confirm/Withdraw control here — a signup's status is entirely the member's own
 * doing via their DM response, never something the clan lead sets on their behalf from this
 * screen. Two things are draggable: player cards (move a player between clans/Unassigned — the
 * bridge call is optimistic, reverting with an inline error if it fails) and column headers
 * (reorder the columns themselves — client-side only, so the Unassigned pool can be dragged next
 * to whichever clan is currently being filled instead of always sitting at the far end).
 *
 * The title/legend/sort-order block is a `position: sticky` header so it stays in view while a
 * long roster scrolls (each column also scrolls internally past a height cap).
 */
export function renderEnrollmentBoard(
  container: HTMLElement,
  payload: EnrollmentPayload,
  onAssignAction: (playerTag: string, clanTag: string | null) => Promise<void>,
  onClose: (reason: string) => void,
): void {
  const working: EnrollmentPlayer[] = payload.players.map((p) => ({ ...p }))
  const byTag = new Map(working.map((p) => [p.player_tag, p]))
  let sortOrder: SortOrder = 'th'
  // Column order: participating clans (already tier-sorted by the bridge, highest league
  // first — see _build_enrollment_payload), Unassigned pool last. Mutable so column headers can
  // be dragged to reorder — a purely client-side arrangement, not persisted.
  let columnOrder: (string | null)[] = [...payload.clans.map((c) => c.clan_tag), null]

  container.innerHTML = ''

  const topBar = document.createElement('div')
  topBar.className = 'board-topbar'
  container.appendChild(topBar)

  const titleRow = document.createElement('div')
  titleRow.className = 'title-row'
  topBar.appendChild(titleRow)

  const header = document.createElement('div')
  header.className = 'header'
  header.textContent = `Season ${payload.season} — ${formatEventStatus(payload.event_status)}`
  titleRow.appendChild(header)

  const legend = document.createElement('div')
  legend.className = 'legend'
  legend.append(
    buildLegendItem(pendingIconUrl, STATUS_LABEL.pending),
    buildLegendItem(gcheckIconUrl, STATUS_LABEL.confirmed),
    buildLegendItem(redxIconUrl, STATUS_LABEL.declined),
    buildLegendItem(unlinkedIconUrl, UNLINKED_LABEL),
  )
  titleRow.appendChild(legend)

  const sortRow = document.createElement('div')
  sortRow.className = 'sort-row'
  sortRow.appendChild(
    buildSortSelector(sortOrder, (next) => {
      sortOrder = next
      renderBoard()
    }),
  )
  topBar.appendChild(sortRow)

  const board = document.createElement('div')
  board.className = 'board'
  container.appendChild(board)

  const status = document.createElement('span')
  status.className = 'save-status'

  const closeButton = document.createElement('button')
  closeButton.textContent = 'Close'
  closeButton.className = 'cancel-button'
  closeButton.addEventListener('click', () => onClose('Closed'))

  const footer = document.createElement('div')
  footer.className = 'footer'
  footer.append(closeButton, status)
  container.appendChild(footer)

  function columnTitle(clanTag: string | null): string {
    if (clanTag === null) return 'Unassigned'
    const clan = payload.clans.find((c) => c.clan_tag === clanTag)
    if (!clan) return clanTag
    const tierSuffix = clan.tier ? ` · ${clan.tier}` : ''
    return `${clan.name ?? clan.clan_tag} (${clan.clan_tag})${tierSuffix}`
  }

  function playersFor(clanTag: string | null): EnrollmentPlayer[] {
    return sortPlayers(
      working.filter((p) => p.assigned_clan_tag === clanTag),
      sortOrder,
    )
  }

  function handleDrop(player: EnrollmentPlayer, targetClanTag: string | null): void {
    if (player.assigned_clan_tag === targetClanTag) return
    const previousAssignment = player.assigned_clan_tag
    player.assigned_clan_tag = targetClanTag
    status.textContent = ''
    status.className = 'save-status'
    renderBoard()
    onAssignAction(player.player_tag, targetClanTag).catch((err: unknown) => {
      console.error(err)
      player.assigned_clan_tag = previousAssignment
      renderBoard()
      status.textContent = `Action failed: ${(err as Error).message}`
      status.className = 'save-status error'
    })
  }

  function handleColumnReorder(fromIndex: number, toIndex: number): void {
    if (fromIndex === toIndex || fromIndex < 0 || fromIndex >= columnOrder.length) return
    const [moved] = columnOrder.splice(fromIndex, 1)
    columnOrder.splice(toIndex, 0, moved)
    renderBoard()
  }

  function buildCard(player: EnrollmentPlayer): HTMLElement {
    const card = document.createElement('div')
    card.className = 'player-card'
    card.draggable = true
    card.addEventListener('dragstart', (e) => {
      e.dataTransfer?.setData('text/plain', player.player_tag)
      if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move'
      card.classList.add('dragging')
    })
    card.addEventListener('dragend', () => card.classList.remove('dragging'))

    const row = document.createElement('div')
    row.className = 'player-row'

    if (player.th_level != null) {
      const th = document.createElement('span')
      th.className = 'th-badge'
      if (player.th_icon_url) {
        const icon = document.createElement('img')
        icon.className = 'th-icon'
        icon.src = player.th_icon_url
        icon.alt = `TH${player.th_level}`
        // Some Activity CSPs may not allow cdn.discordapp.com images — degrade to the plain
        // number rather than showing a broken-image icon.
        icon.addEventListener('error', () => icon.remove())
        th.appendChild(icon)
      }
      th.append(String(player.th_level))
      row.appendChild(th)
    }

    const name = document.createElement('span')
    name.className = 'player-name'
    name.textContent = displayName(player)
    row.appendChild(name)

    if (player.skill_score != null) {
      const skill = document.createElement('span')
      skill.className = 'skill-score'
      skill.textContent = player.skill_score.toFixed(1)
      skill.title = 'Player skill score'
      row.appendChild(skill)
    }

    if (player.discord_id == null) {
      const icon = document.createElement('img')
      icon.className = 'status-icon'
      icon.src = unlinkedIconUrl
      icon.alt = UNLINKED_LABEL
      icon.title = UNLINKED_LABEL
      row.appendChild(icon)
    }

    if (isVisibleStatus(player.signup_status)) {
      const icon = document.createElement('img')
      icon.className = 'status-icon'
      icon.src = STATUS_ICON[player.signup_status]
      icon.alt = STATUS_LABEL[player.signup_status]
      icon.title = STATUS_LABEL[player.signup_status]
      row.appendChild(icon)
    }

    card.appendChild(row)
    return card
  }

  function buildColumn(clanTag: string | null, index: number): HTMLElement {
    const column = document.createElement('div')
    column.className = clanTag === null ? 'column column-unassigned' : 'column'

    const players = playersFor(clanTag)

    const columnHeader = document.createElement('div')
    columnHeader.className = 'column-header'
    columnHeader.textContent = `${columnTitle(clanTag)} (${players.length})`
    columnHeader.draggable = true
    columnHeader.addEventListener('dragstart', (e) => {
      e.dataTransfer?.setData(COLUMN_DRAG_TYPE, String(index))
      if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move'
      column.classList.add('dragging')
    })
    columnHeader.addEventListener('dragend', () => column.classList.remove('dragging'))
    column.appendChild(columnHeader)

    const cardList = document.createElement('div')
    cardList.className = 'card-list'
    for (const player of players) {
      cardList.appendChild(buildCard(player))
    }
    column.appendChild(cardList)

    column.addEventListener('dragover', (e) => {
      e.preventDefault()
      if (e.dataTransfer) e.dataTransfer.dropEffect = 'move'
      column.classList.add('drop-target')
    })
    column.addEventListener('dragleave', () => column.classList.remove('drop-target'))
    column.addEventListener('drop', (e) => {
      e.preventDefault()
      column.classList.remove('drop-target')
      if (e.dataTransfer?.types.includes(COLUMN_DRAG_TYPE)) {
        handleColumnReorder(Number(e.dataTransfer.getData(COLUMN_DRAG_TYPE)), index)
        return
      }
      const playerTag = e.dataTransfer?.getData('text/plain')
      if (!playerTag) return
      const player = byTag.get(playerTag)
      if (player) handleDrop(player, clanTag)
    })

    return column
  }

  function renderBoard(): void {
    board.innerHTML = ''
    columnOrder.forEach((clanTag, index) => {
      board.appendChild(buildColumn(clanTag, index))
    })
  }

  renderBoard()
}

function buildLegendItem(iconUrl: string, label: string): HTMLElement {
  const item = document.createElement('span')
  item.className = 'legend-item'
  const icon = document.createElement('img')
  icon.className = 'legend-icon'
  icon.src = iconUrl
  icon.alt = label
  item.append(icon, label)
  return item
}

function buildSortSelector(initial: SortOrder, onChange: (order: SortOrder) => void): HTMLElement {
  const wrap = document.createElement('div')
  wrap.className = 'sort-selector'

  const label = document.createElement('span')
  label.className = 'sort-label'
  label.textContent = 'Sort by:'
  wrap.appendChild(label)

  const options: { value: SortOrder; label: string }[] = [
    { value: 'th', label: 'TH Level' },
    { value: 'skill', label: 'Player Skill' },
    { value: 'alpha', label: 'Alphabetical' },
  ]
  for (const option of options) {
    const optionLabel = document.createElement('label')
    optionLabel.className = 'sort-option'
    const radio = document.createElement('input')
    radio.type = 'radio'
    radio.name = 'cwl-sort-order'
    radio.value = option.value
    radio.checked = option.value === initial
    radio.addEventListener('change', () => {
      if (radio.checked) onChange(option.value)
    })
    optionLabel.append(radio, option.label)
    wrap.appendChild(optionLabel)
  }
  return wrap
}
