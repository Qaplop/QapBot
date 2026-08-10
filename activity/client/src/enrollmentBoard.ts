import type { EnrollmentPayload, EnrollmentPlayer } from './types'

type SignupAction = 'confirm' | 'withdraw'

// The board only ever offers a binary Confirm/Withdraw toggle (matching the bridge's
// POST /api/cwl/enrollment/signup, which only accepts those two actions) — "declined" only
// happens via a member's own DM response and isn't reachable from here.
const ENROLLED_STATUSES = new Set(['pending', 'confirmed'])

function displayName(player: EnrollmentPlayer): string {
  return player.player_name || player.player_tag
}

function statusLabel(status: EnrollmentPlayer['signup_status']): string {
  switch (status) {
    case 'pending':
      return 'Pending'
    case 'confirmed':
      return 'Confirmed'
    case 'declined':
      return 'Declined'
    case 'withdrawn':
      return 'Withdrawn'
    default:
      return 'Not signed up'
  }
}

/**
 * Renders the CWL "Manage Assignment" board — participating clans as drag-and-drop columns plus
 * an "Unassigned" pool, each player a card with a 1-click Confirm/Withdraw signup toggle
 * (CWL_ROSTER_PLANNING_PLAN.md "Manage Enrollment" slice 4).
 *
 * Unlike clanConfigTable's batched Save/Cancel, every action here is live: a signup toggle or a
 * drag-and-drop move calls the bridge immediately. The working copy is updated optimistically
 * before the network call resolves (instant visual feedback) and reverted with an inline error
 * if the call fails — there is no separate Save step to catch a failure at.
 */
export function renderEnrollmentBoard(
  container: HTMLElement,
  payload: EnrollmentPayload,
  onSignupAction: (playerTag: string, action: SignupAction) => Promise<void>,
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

  async function runAction(revert: () => void, action: () => Promise<void>): Promise<void> {
    status.textContent = ''
    status.className = 'save-status'
    renderBoard()
    try {
      await action()
    } catch (err) {
      console.error(err)
      revert()
      renderBoard()
      status.textContent = `Action failed: ${(err as Error).message}`
      status.className = 'save-status error'
    }
  }

  function handleSignupToggle(player: EnrollmentPlayer): void {
    const enrolled = ENROLLED_STATUSES.has(player.signup_status ?? '')
    const nextAction: SignupAction = enrolled ? 'withdraw' : 'confirm'
    const previousStatus = player.signup_status
    const previousAssignment = player.assigned_clan_tag
    player.signup_status = nextAction === 'confirm' ? 'confirmed' : 'withdrawn'
    // Mirrors the bridge's own cascade (handle_post_cwl_enrollment_signup): withdrawing also
    // clears any assignment, so a withdrawn player never lingers assigned to a clan column.
    if (nextAction === 'withdraw') {
      player.assigned_clan_tag = null
    }
    void runAction(
      () => {
        player.signup_status = previousStatus
        player.assigned_clan_tag = previousAssignment
      },
      () => onSignupAction(player.player_tag, nextAction),
    )
  }

  function handleDrop(player: EnrollmentPlayer, targetClanTag: string | null): void {
    if (player.assigned_clan_tag === targetClanTag) return
    const previousAssignment = player.assigned_clan_tag
    player.assigned_clan_tag = targetClanTag
    void runAction(
      () => {
        player.assigned_clan_tag = previousAssignment
      },
      () => onAssignAction(player.player_tag, targetClanTag),
    )
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

    const name = document.createElement('span')
    name.className = 'player-name'
    name.textContent = displayName(player)

    const badge = document.createElement('span')
    badge.className = `signup-badge signup-${player.signup_status ?? 'none'}`
    badge.textContent = statusLabel(player.signup_status)

    row.append(name, badge)

    const enrolled = ENROLLED_STATUSES.has(player.signup_status ?? '')
    const actionButton = document.createElement('button')
    actionButton.className = 'signup-toggle'
    actionButton.textContent = enrolled ? 'Withdraw' : 'Confirm'
    actionButton.addEventListener('click', () => handleSignupToggle(player))

    card.append(row, actionButton)
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
