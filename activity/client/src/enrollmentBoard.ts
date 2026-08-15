import gcheckIconUrl from './assets/gcheck.svg'
import pendingIconUrl from './assets/pending.svg'
import redxIconUrl from './assets/redx.svg'
import unlinkedIconUrl from './assets/unlinked.svg'
import type { EnrollmentPayload, EnrollmentPlayer } from './types'

type SortOrder = 'th' | 'skill' | 'alpha'

// Which number shows next to each player's name — plain average stars/attack (default) or the
// league-adjusted skill score (2026-08-14, project owner's spec: a second, independent radio
// group from the Sort-by one above). Sorting by "Player Skill" always uses the actual
// league-adjusted score regardless of this choice — this only controls what's *displayed*.
type DisplayMetric = 'avg_stars' | 'skill'

function metricValue(player: EnrollmentPlayer, metric: DisplayMetric): number | null {
  return metric === 'avg_stars' ? player.avg_stars : player.skill_score
}

function metricLabel(metric: DisplayMetric): string {
  return metric === 'avg_stars' ? 'Average stars/attack (last ≤10 CWL attacks)' : 'League-adjusted skill score'
}

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

// Permanent opt-out (user_players.cwl_permanent_optout) or a this-season decline both mean "not
// playing" — either way they always sort to the bottom of the Unassigned pool (2026-08-14,
// project owner's spec), regardless of the chosen TH/skill/alpha sort order.
function isOptedOut(player: EnrollmentPlayer): boolean {
  return player.cwl_permanent_optout || player.signup_status === 'declined'
}

// Green when an assigned player's real current clan already matches their assignment, amber
// when it doesn't (assigned but hasn't transferred yet) — null (no extra class, default card
// shade) for an Unassigned player or one whose current clan isn't on record at all, since
// neither case is a meaningful match/mismatch to flag.
function clanMatchClass(player: EnrollmentPlayer): 'same-clan' | 'different-clan' | null {
  if (player.assigned_clan_tag === null || player.current_clan_tag === null) return null
  return player.assigned_clan_tag === player.current_clan_tag ? 'same-clan' : 'different-clan'
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
  let displayMetric: DisplayMetric = 'avg_stars'
  // Column order: participating clans (already tier-sorted by the bridge, highest league
  // first — see _build_enrollment_payload), Unassigned pool last. Mutable so column headers can
  // be dragged to reorder — a purely client-side arrangement, not persisted.
  let columnOrder: (string | null)[] = [...payload.clans.map((c) => c.clan_tag), null]
  // buildColumn()/renderBoard() hand-off for sizing each column's roster-band-bg overlay — see
  // buildColumn()'s comment on why this needs a second pass after DOM attachment.
  let pendingBandSizing: { bg: HTMLElement; lastCard: HTMLElement }[] = []

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
  // Independent of the Sort-by group above — this only changes which number is *displayed* on
  // each card, not the sort order itself (2026-08-14, project owner's spec).
  sortRow.appendChild(
    buildMetricSelector(displayMetric, (next) => {
      displayMetric = next
      updateSkillExplainer()
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

  const skillExplainer = document.createElement('span')
  skillExplainer.className = 'skill-explainer'
  // Kept in sync with displayMetric (updateSkillExplainer(), called on init and whenever the
  // "Show:" selector changes) so it always describes whichever number the cards are currently
  // showing, not just the skill score.
  function updateSkillExplainer(): void {
    skillExplainer.textContent = displayMetric === 'avg_stars'
      ? 'Avg Stars/Attack = plain average stars per attack over each player’s last ≤10 CWL attacks.'
      : 'Skill Score = league-weighted average stars/attack over each player’s last ≤10 CWL attacks.'
  }
  updateSkillExplainer()

  // .footer-fixed (position: fixed, not sticky — see its CSS comment in index.html): plain
  // flow and sticky positioning both turned out to depend on the page's total content height
  // actually reaching the viewport height, which isn't reliably true here — position: fixed
  // sidesteps that entirely, always pinning this exactly to the viewport's bottom edge
  // regardless of content/window height.
  // Close stays first/left (unchanged position); skillExplainer moves last with margin-left:
  // auto so it's pushed to the right edge instead of sitting immediately after Close.
  const footer = document.createElement('div')
  footer.className = 'footer footer-fixed'
  footer.append(closeButton, status, skillExplainer)
  container.appendChild(footer)

  // Now that the footer is pinned independently of content height (position: fixed above), the
  // board itself is free to actually fill whatever room is really available instead of guessing
  // a constant — a fixed pixel cap wasted most of the window on anything taller than that guess
  // (live-testing feedback, 2026-08-14). Measures the two things that box in the available
  // height (the sticky topbar's real rendered height, the fixed footer's real rendered height)
  // and sets .board's own height to fill the rest — .column's default flex stretch then makes
  // every column, short or tall, span that full height, with each column's own internal
  // .card-list scroll (unchanged) handling whichever ones have more players than fit. Re-run on
  // resize so the board keeps matching the actual window, not just its size at first render.
  function resizeBoard(): void {
    const available = window.innerHeight
      - topBar.getBoundingClientRect().height
      - footer.getBoundingClientRect().height
      - 32 // breathing room: #app's own top+bottom padding
    board.style.height = `${Math.max(200, available)}px`
  }
  resizeBoard()
  window.addEventListener('resize', resizeBoard)

  // Backend tier strings are e.g. "Master League I" / "Champion League II" — drop the word
  // "League" so the header just reads "Master I" / "Champion II".
  function formatTier(tier: string | null): string | null {
    return tier ? tier.replace(' League', '') : null
  }

  function playersFor(clanTag: string | null): EnrollmentPlayer[] {
    const sorted = sortPlayers(
      working.filter((p) => p.assigned_clan_tag === clanTag),
      sortOrder,
    )
    if (clanTag !== null) return sorted
    // Unassigned only — opted-out players always sort last (Array.sort is stable, so each
    // partition keeps the order sortPlayers() already gave it).
    return [...sorted.filter((p) => !isOptedOut(p)), ...sorted.filter(isOptedOut)]
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
    const matchClass = clanMatchClass(player)
    if (matchClass) card.classList.add(matchClass)
    card.draggable = true
    card.addEventListener('dragstart', (e) => {
      e.dataTransfer?.setData('text/plain', player.player_tag)
      if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move'
      card.classList.add('dragging')
    })
    card.addEventListener('dragend', () => card.classList.remove('dragging'))

    const row = document.createElement('div')
    row.className = 'player-row'

    // TH-badge and status-icon slots are ALWAYS added, even when this particular player has
    // nothing to show there (invisible spacer instead) — they used to be omitted entirely,
    // which meant a player missing th_level, or a linked player with no visible signup status
    // (null/'withdrawn'), rendered a shorter row than everyone else. That's what actually broke
    // row-height/alignment across columns (live-testing feedback, 2026-08-14) — not the
    // green/amber/placeholder coloring itself, which was already identical. Reserving both
    // slots unconditionally means every card, real or placeholder, has the exact same DOM shape
    // and therefore the exact same height, full stop.
    const th = document.createElement('span')
    th.className = 'th-badge'
    if (player.th_level != null) {
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
    } else {
      // No real TH data — reserve the exact same shape (icon-sized box + 2-digit text) an
      // actual badge would take, just invisible, rather than an empty span with nothing to
      // give it a size (an empty .th-badge has no forced width/height of its own).
      const iconSpacer = document.createElement('span')
      iconSpacer.className = 'th-icon'
      th.appendChild(iconSpacer)
      th.append('00')
      th.style.visibility = 'hidden'
    }
    row.appendChild(th)

    const name = document.createElement('span')
    name.className = 'player-name'
    name.textContent = displayName(player)
    row.appendChild(name)

    // Always reserved, like th-badge/status-icon above (invisible placeholder text when this
    // player has no CWL-attack data for either metric) — Unassigned's pool skews heavily toward
    // players who've never played CWL at all, so leaving this slot out entirely for them was
    // the last remaining reason its cards didn't line up with clan-column cards row for row
    // (live-testing feedback, 2026-08-14).
    const metricScore = metricValue(player, displayMetric)
    const skill = document.createElement('span')
    skill.className = 'skill-score'
    if (metricScore != null) {
      skill.textContent = metricScore.toFixed(1)
      skill.title = metricLabel(displayMetric)
    } else {
      skill.textContent = '0.0'
      skill.style.visibility = 'hidden'
    }
    row.appendChild(skill)

    // Mutually exclusive: a player with no linked Discord account can never actually respond to
    // the template DM, so "Not Linked" replaces whatever signup-status icon would otherwise show
    // (almost always "Pending", since that's the only status such a player could ever be seeded
    // with) rather than displaying alongside it. A <span> (not an <img> with no src, which some
    // browsers render as a visible broken-image glyph even under visibility: hidden) reserves
    // the identical .status-icon-sized slot when there's nothing to actually show — same
    // approach buildPlaceholderCard() already uses for its own spacer.
    if (player.discord_id == null) {
      const icon = document.createElement('img')
      icon.className = 'status-icon'
      icon.src = unlinkedIconUrl
      icon.alt = UNLINKED_LABEL
      icon.title = UNLINKED_LABEL
      row.appendChild(icon)
    } else if (isVisibleStatus(player.signup_status)) {
      const icon = document.createElement('img')
      icon.className = 'status-icon'
      icon.src = STATUS_ICON[player.signup_status]
      icon.alt = STATUS_LABEL[player.signup_status]
      icon.title = STATUS_LABEL[player.signup_status]
      row.appendChild(icon)
    } else {
      const spacer = document.createElement('span')
      spacer.className = 'status-icon'
      spacer.style.visibility = 'hidden'
      row.appendChild(spacer)
    }

    card.appendChild(row)
    return card
  }

  // An empty starting-roster slot — not a real player, not draggable/droppable onto. Shown so
  // the roster band always reads as "roster_size tiles" (some filled, some still open) rather
  // than shrinking to however many starters happen to be assigned yet (2026-08-14, project
  // owner's spec). Contains an invisible .status-icon-sized spacer inside a real .player-row,
  // rather than a guessed min-height, so its height always matches an actual player card's
  // exactly — same markup/CSS driving both, nothing to keep in sync by hand.
  function buildPlaceholderCard(): HTMLElement {
    const card = document.createElement('div')
    card.className = 'player-card placeholder-card'
    const row = document.createElement('div')
    row.className = 'player-row'
    const spacer = document.createElement('span')
    spacer.className = 'status-icon'
    spacer.style.visibility = 'hidden'
    row.appendChild(spacer)
    card.appendChild(row)
    return card
  }

  function buildColumn(clanTag: string | null, index: number): HTMLElement {
    const column = document.createElement('div')
    column.className = clanTag === null ? 'column column-unassigned' : 'column'

    const players = playersFor(clanTag)
    const clan = clanTag === null ? null : payload.clans.find((c) => c.clan_tag === clanTag)
    const rosterSize = clan?.roster_size ?? null

    // Two lines: clan name (+ count) on top, league tier below — no clan tag, no "·" separator
    // (live-testing feedback, 2026-08-14: the old single-line "Name (#TAG) · Tier (count)" text
    // wrapped unpredictably depending on name/tag length, which is what made column-header
    // heights mismatch between columns in the first place). Neither name nor tier is forced to
    // uppercase (see the removed text-transform on .column-header in index.html) — shown exactly
    // as the data has them, tier already arrives "Title Case" from the backend.
    const columnHeader = document.createElement('div')
    columnHeader.className = 'column-header'

    // Name and count are separate flex children (space-between) so the count sits pinned to the
    // header's right edge instead of immediately trailing the name (live-testing feedback).
    const nameLine = document.createElement('div')
    nameLine.className = 'column-header-name'
    const nameSpan = document.createElement('span')
    nameSpan.className = 'column-header-name-text'
    nameSpan.textContent = clanTag === null ? 'Unassigned' : (clan?.name ?? clanTag)
    nameLine.appendChild(nameSpan)
    const countSpan = document.createElement('span')
    countSpan.className = 'column-count'
    countSpan.textContent = rosterSize !== null ? `(${players.length}/${rosterSize})` : `(${players.length})`
    nameLine.appendChild(countSpan)
    columnHeader.appendChild(nameLine)

    const tierText = formatTier(clan?.tier ?? null)
    if (tierText) {
      const tierLine = document.createElement('div')
      tierLine.className = 'column-header-tier'
      tierLine.textContent = tierText
      columnHeader.appendChild(tierLine)
    }

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
    if (rosterSize !== null) {
      // Starting-roster slots (positions 1..rosterSize, in whatever order this column is
      // currently sorted) get a visibly lighter background band; anything beyond that — backup/
      // reserve players — sits on the column's normal (current) shade. Slots the roster still
      // has open (fewer assigned starters than rosterSize) are padded out with placeholder
      // tiles, so the band always shows exactly rosterSize slots — some filled, some still
      // open — rather than shrinking to however many are assigned so far (2026-08-14, project
      // owner's spec).
      //
      // Starters/placeholders and backups are all plain, direct .card-list children — one flat
      // flex list, not a nested wrapper around just the starters — so every card (roster or
      // backup) gets its width from the exact same flex-stretch computation. Two earlier
      // attempts wrapped the starters in their own box and gave that box a background reaching
      // into .card-list's padding (first via width:calc()+negative margin, then via box-shadow);
      // both still left a hairline seam between the last roster card and the first backup card
      // in live testing (2026-08-15) because the wrapper was a second, independently-computed
      // width next to the backups' width, and the two could round a device-pixel apart. A flat
      // list has no second computation to drift from the first, so the seam can't recur.
      //
      // The band itself is a `.roster-band-bg` div, absolutely positioned within .card-list
      // (position: relative) and pinned with left/right/top: 0 — that's the padding-box edges,
      // not a width computed from a percentage, so it's flush with the column's inner border by
      // construction. Its height is set below, after this column is attached to the live board,
      // by measuring where the last roster-slot card actually ends (offsetTop + offsetHeight,
      // relative to .card-list since that's now the nearest positioned ancestor) — real pixels
      // from the real layout, not a guessed/derived card-height constant.
      const starters = players.slice(0, rosterSize)
      const backups = players.slice(rosterSize)
      const openSlots = rosterSize - starters.length
      if (starters.length > 0 || openSlots > 0) {
        const rosterBandBg = document.createElement('div')
        rosterBandBg.className = 'roster-band-bg'
        cardList.appendChild(rosterBandBg)
        let lastRosterCard: HTMLElement | null = null
        for (const player of starters) {
          lastRosterCard = buildCard(player)
          cardList.appendChild(lastRosterCard)
        }
        for (let i = 0; i < openSlots; i++) {
          lastRosterCard = buildPlaceholderCard()
          cardList.appendChild(lastRosterCard)
        }
        if (lastRosterCard) pendingBandSizing.push({ bg: rosterBandBg, lastCard: lastRosterCard })
      }
      for (const player of backups) {
        cardList.appendChild(buildCard(player))
      }
    } else {
      for (const player of players) {
        cardList.appendChild(buildCard(player))
      }
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
    // Populated by buildColumn() while it builds each column's cards (still detached from the
    // document at that point, so offsetTop/offsetHeight would read zero) — sized in a second
    // pass below, once every column is actually attached to `board` and has real layout.
    pendingBandSizing = []
    columnOrder.forEach((clanTag, index) => {
      board.appendChild(buildColumn(clanTag, index))
    })
    for (const { bg, lastCard } of pendingBandSizing) {
      // Ends halfway into the gap after the last roster card, not flush with its bottom edge
      // (live-testing feedback, 2026-08-15) — flush-with-the-card made the color change land
      // right on the card's own border line; centering it in the empty gap instead reads as a
      // clean break between the two zones. Reads the real .card-list gap (its parent) rather
      // than hardcoding half of the 4px in the CSS, so this can't drift out of sync with it.
      const parent = bg.parentElement as HTMLElement | null
      const gap = parent ? parseFloat(getComputedStyle(parent).rowGap) || 0 : 0
      bg.style.height = `${lastCard.offsetTop + lastCard.offsetHeight + gap / 2}px`
    }
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

function buildMetricSelector(initial: DisplayMetric, onChange: (metric: DisplayMetric) => void): HTMLElement {
  const wrap = document.createElement('div')
  wrap.className = 'sort-selector'

  const label = document.createElement('span')
  label.className = 'sort-label'
  label.textContent = 'Show:'
  wrap.appendChild(label)

  const options: { value: DisplayMetric; label: string }[] = [
    { value: 'avg_stars', label: 'Avg Stars/Attack' },
    { value: 'skill', label: 'Skill Score' },
  ]
  for (const option of options) {
    const optionLabel = document.createElement('label')
    optionLabel.className = 'sort-option'
    const radio = document.createElement('input')
    radio.type = 'radio'
    radio.name = 'cwl-display-metric'
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
