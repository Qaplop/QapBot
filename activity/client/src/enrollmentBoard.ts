import type { EnrollmentPayload, EnrollmentPlayer } from './types'

function displayName(player: EnrollmentPlayer): string {
  return player.player_name || player.player_tag
}

// Only the three statuses a member's own DM response can actually produce are shown — the board
// never lets the clan lead alter a signup status itself (live-testing feedback, 2026-08-14:
// assignment is drag-and-drop only now, see renderEnrollmentBoard's docstring). Anything else
// (no signup row yet, or a legacy 'withdrawn' value) reads the same as "no status to show".
function statusLabel(status: EnrollmentPlayer['signup_status']): string | null {
  switch (status) {
    case 'pending':
      return 'Pending'
    case 'confirmed':
      return 'Confirmed'
    case 'declined':
      return 'Declined'
    default:
      return null
  }
}

/**
 * Renders the CWL "Manage Assignment" board — participating clans as drag-and-drop columns plus
 * an "Unassigned" pool, each player a compact card: TH icon + level, name, and a read-only
 * signup-status badge (CWL_ROSTER_PLANNING_PLAN.md "Manage Enrollment" slice 4).
 *
 * There is no Confirm/Withdraw control here — a signup's status is entirely the member's own
 * doing via their DM response, never something the clan lead sets on their behalf from this
 * screen. The only action available is moving a card between columns, which calls the bridge
 * immediately with an optimistic UI update that reverts (with an inline error) if the call fails
 * — there is no separate Save step to catch a failure at.
 */
export function renderEnrollmentBoard(
  container: HTMLElement,
  payload: EnrollmentPayload,
  onAssignAction: (playerTag: string, clanTag: string | null) => Promise<void>,
  onClose: (reason: string) => void,
): void {
  const working: EnrollmentPlayer[] = payload.players.map((p) => ({ ...p }))
  const byTag = new Map(working.map((p) => [p.player_tag, p]))

  container.innerHTML = ''

  const header = document.createElement('div')
  header.className = 'header'
  header.textContent = `Season ${payload.season} — ${payload.event_status ?? 'draft'}`
  container.appendChild(header)

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

  // Column order: participating clans (already tier-sorted by the bridge — see
  // _build_enrollment_payload), Unassigned pool last.
  const columnClanTags: (string | null)[] = [...payload.clans.map((c) => c.clan_tag), null]

  function columnTitle(clanTag: string | null): string {
    if (clanTag === null) return 'Unassigned'
    const clan = payload.clans.find((c) => c.clan_tag === clanTag)
    return clan ? `${clan.name ?? clan.clan_tag} (${clan.clan_tag})` : clanTag
  }

  function playersFor(clanTag: string | null): EnrollmentPlayer[] {
    return working
      .filter((p) => p.assigned_clan_tag === clanTag)
      .sort((a, b) => displayName(a).localeCompare(displayName(b)))
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

    const label = statusLabel(player.signup_status)
    if (label) {
      const badge = document.createElement('span')
      badge.className = `signup-badge signup-${player.signup_status}`
      badge.textContent = label
      row.appendChild(badge)
    }

    card.appendChild(row)
    return card
  }

  function buildColumn(clanTag: string | null): HTMLElement {
    const column = document.createElement('div')
    column.className = clanTag === null ? 'column column-unassigned' : 'column'

    const players = playersFor(clanTag)

    const columnHeader = document.createElement('div')
    columnHeader.className = 'column-header'
    columnHeader.textContent = `${columnTitle(clanTag)} (${players.length})`
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
      const playerTag = e.dataTransfer?.getData('text/plain')
      if (!playerTag) return
      const player = byTag.get(playerTag)
      if (player) handleDrop(player, clanTag)
    })

    return column
  }

  function renderBoard(): void {
    board.innerHTML = ''
    for (const clanTag of columnClanTags) {
      board.appendChild(buildColumn(clanTag))
    }
  }

  renderBoard()
}
