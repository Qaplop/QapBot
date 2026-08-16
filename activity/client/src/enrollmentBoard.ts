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
  return metric === 'avg_stars' ? 'Average stars/attack (last 3 CWL months)' : 'League-adjusted skill score'
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

// Progressively-fetched recent-CWL stats (2026-08-16, project owner's spec, verbatim: "get the
// number of missed cwl attacks from the last three season's" / "add the attack / defense ratio
// from the last three cwl seaons" / "Attacks: n (number of total attacks)") — matches GET
// /api/cwl/player-stats' response shape exactly. `seasons` is always 3 long once there's ANY
// data (a fixed trailing-3-calendar-month window, not adaptive); empty means no CWL history on
// record at all, in which case every stat field is null and the pop-up shows none of them.
type PlayerStats = {
  seasons: string[]
  attacks: number | null
  missed_attacks: number | null
  attack_defense_ratio: number | null
}

type TooltipLine = { text: string; kind: 'name' | 'header' | 'line' }

// Hover info pop-up (2026-08-16, project owner's spec — "a small pop-up shows the clan the
// player belongs to along with some other info on the user that we have in the DB", then
// follow-up: "show up the pop-up as soon as possible... and then fetch more data... as it comes
// in"). `resolveClanName` resolves a tag to its display name from whatever's known RIGHT NOW —
// either one of this board's own clans (payload.clans) or a tag already fetched in via
// onResolveClanNames — returning null (not the raw tag) when nothing's known yet, so the "Name
// (#TAG)" line can tell "not resolved yet" apart from "resolved, and the name happens to be the
// tag" and just show the bare tag until a real name lands (project owner's spec, verbatim: "If
// either name or tag is missing just leave it out for the time being and fetch it and add it as
// soon as the fetch is complete"). `playerStats` is undefined while its own fetch hasn't landed
// yet (or was never requested) — the CWL-stats section grows in place once it does, same
// "instant with what we have, patch in the rest" pattern as the clan name.
//
// Ordering (2026-08-16 follow-up, project owner's spec, verbatim — identity/status fields first,
// then one grouped CWL-stats section, Guest status always last): Name/TH/Current Clan/Assigned
// to/Discord/Response, then a "CWL Stats of last 3 Months:" header covering Attacks/Skill
// Score/Avg stars per attack/Attack-Defense ratio/Missed CWL attacks — Skill Score and Avg
// stars/attack moved here from their own standalone lines since they're now computed over the
// SAME trailing-3-month window as the other three (see compute_league_adjusted_skill_scores'
// own docstring for that consistency fix) — then Guest status, unconditionally last.
function buildTooltipLines(
  player: EnrollmentPlayer,
  resolveClanName: (tag: string) => string | null,
  playerStats: PlayerStats | undefined,
  statsFetchSettled: boolean,
): TooltipLine[] {
  const currentClanLine = ((): string => {
    if (player.current_clan_tag === null) return 'Current Clan: None on record'
    const name = resolveClanName(player.current_clan_tag)
    return name ? `Current Clan: ${name} (${player.current_clan_tag})` : `Current Clan: ${player.current_clan_tag}`
  })()

  const lines: TooltipLine[] = [{ text: `${displayName(player)} (${player.player_tag})`, kind: 'name' }]
  const pushLine = (text: string): void => {
    lines.push({ text, kind: 'line' })
  }
  if (player.th_level != null) pushLine(`Town Hall ${player.th_level}`)
  pushLine(currentClanLine)
  pushLine(
    `Assigned to: ${
      player.assigned_clan_tag !== null ? resolveClanName(player.assigned_clan_tag) ?? player.assigned_clan_tag : 'Unassigned'
    }`,
  )
  pushLine(`Discord: ${player.discord_id != null ? 'Linked' : 'Not linked'}`)
  pushLine(
    `Response: ${
      player.discord_id == null
        ? UNLINKED_LABEL
        : isVisibleStatus(player.signup_status) ? STATUS_LABEL[player.signup_status] : 'No response yet'
    }`,
  )

  const statLines: string[] = []
  if (playerStats && playerStats.attacks !== null) statLines.push(`Attacks: ${playerStats.attacks}`)
  if (player.skill_score != null) statLines.push(`Skill Score: ${player.skill_score.toFixed(1)}`)
  if (player.avg_stars != null) statLines.push(`Avg stars/attack: ${player.avg_stars.toFixed(1)}`)
  if (playerStats && playerStats.attack_defense_ratio !== null) {
    statLines.push(`Attack/Defense ratio: ${playerStats.attack_defense_ratio.toFixed(2)}`)
  }
  if (playerStats && playerStats.missed_attacks !== null) statLines.push(`Missed CWL attacks: ${playerStats.missed_attacks}`)
  if (statLines.length > 0) {
    lines.push({ text: 'CWL Stats of last 3 Months:', kind: 'header' })
    for (const text of statLines) pushLine(text)
  } else if (statsFetchSettled) {
    // Skill Score/Avg-stars are already known synchronously (part of `player`, never fetched),
    // so once the async playerStats fetch has also settled with nothing, every one of these
    // five stats is confirmed absent — not just "not fetched yet" — so it's safe to say so
    // explicitly instead of silently omitting the whole section (2026-08-16, project owner's
    // spec, verbatim: "for a player who has no cwl data available for the last three months it
    // should read: CWL stats of last 3 Months: No data available").
    lines.push({ text: 'CWL Stats of last 3 Months:', kind: 'header' })
    pushLine('No data available')
  }

  if (player.is_guest) pushLine('Guest (invited from another clan/guild)')

  return lines
}

function renderTooltipLines(el: HTMLElement, lines: TooltipLine[]): void {
  el.innerHTML = ''
  const classForKind: Record<TooltipLine['kind'], string> = {
    name: 'hover-popup-name',
    header: 'hover-popup-header',
    line: 'hover-popup-line',
  }
  for (const line of lines) {
    const row = document.createElement('div')
    row.className = classForKind[line.kind]
    row.textContent = line.text
    el.appendChild(row)
  }
}

// Clamped to the viewport, not just "to the right of the card" — a card near the right or bottom
// edge of the window would otherwise push the pop-up (partially) off-screen. Measured AFTER
// appending to document.body (not the scrolling .board/.card-list containers — see
// showTooltip's own comment on why) so getBoundingClientRect() reflects its real rendered size.
function positionTooltip(el: HTMLElement, anchor: HTMLElement): void {
  const anchorRect = anchor.getBoundingClientRect()
  const popupRect = el.getBoundingClientRect()
  const margin = 8
  let left = anchorRect.right + margin
  if (left + popupRect.width > window.innerWidth) left = anchorRect.left - popupRect.width - margin
  left = Math.max(margin, left)
  let top = anchorRect.top
  if (top + popupRect.height > window.innerHeight) top = window.innerHeight - popupRect.height - margin
  top = Math.max(margin, top)
  el.style.left = `${left}px`
  el.style.top = `${top}px`
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
      if (diff !== 0) return diff
      const thDiff = (b.th_level ?? -1) - (a.th_level ?? -1)
      return thDiff !== 0 ? thDiff : byName(a, b)
    }
    return byName(a, b)
  })
}

const COLUMN_DRAG_TYPE = 'application/x-cwl-column-index'

// Orphaned-assignment column (2026-08-16, project owner's spec): a player can end up assigned to
// a clan_tag that's no longer one of this guild's participating columns at all — most commonly a
// shared clan the guild has since detached from (its column disappears, but the underlying
// assignment — local cwl_assignments for a plain guest clan, or a mirrored one for a former
// shared clan — was deliberately left in place rather than silently cleared, so the clan lead
// isn't left guessing where that player went). Before this fix such a player simply vanished
// from the board entirely (playersFor() only ever matched a REAL column's own tag, so an
// unrecognized assigned_clan_tag matched nothing, not even Unassigned) — this sentinel gives
// them a real, always-draggable-out home instead. Guaranteed never to collide with a real CoC
// clan tag (those always start with '#').
const ORPHANED_COLUMN_TAG = '__orphaned__'

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
export type EnrollmentBoardHandle = {
  /** Merges freshly-fetched player data into the live board (2026-08-16, live-testing feedback:
   * "would it be possible to auto-update this view whenever a user changes his confirmation
   * setting?") — see the function's own definition below for exactly which fields are safe to
   * live-update and why `assigned_clan_tag` deliberately isn't one of them. */
  applyPolledUpdate: (freshPlayers: EnrollmentPlayer[]) => void
}

export function renderEnrollmentBoard(
  container: HTMLElement,
  payload: EnrollmentPayload,
  onAssignAction: (playerTag: string, clanTag: string | null) => Promise<void>,
  onClose: (reason: string) => void,
  // Progressive hover pop-up fetch (2026-08-16, project owner's spec: "show up the pop-up as
  // soon as possible... then fetch more data... starting with the clan name") — resolves any
  // clan tags the pop-up couldn't already label from payload.clans. Optional so tests/other
  // callers that don't care about the pop-up's async half can omit it; the pop-up still renders
  // fine with just the raw tag in that case.
  onResolveClanNames?: (tags: string[]) => Promise<Record<string, string>>,
  // Second half of the same progressive fetch (2026-08-16, project owner's spec: "get the number
  // of missed cwl attacks from the last three season's" / "add the attack / defense ratio from
  // the last three cwl seaons") — one player's own recent-CWL stats, fetched per-hover. Also
  // optional, same reasoning as onResolveClanNames above.
  onFetchPlayerStats?: (playerTag: string) => Promise<PlayerStats>,
): EnrollmentBoardHandle {
  const working: EnrollmentPlayer[] = payload.players.map((p) => ({ ...p }))
  const byTag = new Map(working.map((p) => [p.player_tag, p]))
  // Hover pop-up clan-name cache, shared across every card for the life of this board render —
  // `resolvedClanNames` holds names fetched in from outside payload.clans;
  // `attemptedClanTags` remembers every tag already asked for (successful or not) so a clan
  // CACHE genuinely doesn't know about isn't re-fetched on every single hover.
  const resolvedClanNames = new Map<string, string>()
  const attemptedClanTags = new Set<string>()
  // Same shape, keyed by player_tag instead of clan_tag — `playerStatsCache` only ever holds an
  // entry once its fetch actually resolves (with real data, seasons.length > 0), so a repeat
  // hover over a player with zero CWL history keeps re-showing the pop-up without the stat lines
  // rather than caching an empty result forever; `attemptedPlayerStatsTags` still prevents
  // re-fetching on every single hover regardless of whether that fetch found anything.
  const playerStatsCache = new Map<string, PlayerStats>()
  const attemptedPlayerStatsTags = new Set<string>()
  // Distinct from attemptedPlayerStatsTags (set synchronously the moment a fetch STARTS) — this
  // is set only once the fetch actually SETTLES, so buildTooltipLines can tell "still waiting"
  // apart from "confirmed no data in the window" and only show "No data available" once it's
  // actually true, never as a premature flash before the async fetch lands.
  const settledPlayerStatsTags = new Set<string>()
  // Set for the duration of any native HTML5 drag gesture (player card or column-header) — a
  // poll-triggered renderBoard() mid-drag would tear down the very DOM node the browser is
  // currently dragging, silently aborting the gesture (2026-08-16, live-testing feedback: polling
  // support). applyPolledUpdate() below checks this and simply defers to the next poll tick
  // instead of rendering mid-gesture.
  let isDragging = false
  let sortOrder: SortOrder = 'th'
  let displayMetric: DisplayMetric = 'avg_stars'
  const knownClanTags = new Set(payload.clans.map((c) => c.clan_tag))
  const clanNameByTag = new Map(payload.clans.map((c) => [c.clan_tag, c.name ?? c.clan_tag]))
  // Returns null (not the raw tag) when nothing's resolved yet — see buildTooltipLines' own
  // comment for why that distinction matters to the "Name (#TAG)" pop-up line.
  const resolveClanName = (tag: string): string | null => clanNameByTag.get(tag) ?? resolvedClanNames.get(tag) ?? null
  const hasOrphanedAssignments = working.some(
    (p) => p.assigned_clan_tag !== null && !knownClanTags.has(p.assigned_clan_tag),
  )
  // Column order: participating clans (already tier-sorted by the bridge, highest league
  // first — see _build_enrollment_payload), then the orphaned-assignment column (only when
  // actually needed — see ORPHANED_COLUMN_TAG), Unassigned pool last. Mutable so column headers
  // can be dragged to reorder — a purely client-side arrangement, not persisted. Computed once
  // at setup, same as every other column — if the clan lead reassigns every orphaned player away
  // during this session the column stays put (now empty) rather than disappearing mid-session,
  // matching how every other column already behaves once shown.
  let columnOrder: (string | null)[] = [
    ...payload.clans.map((c) => c.clan_tag),
    ...(hasOrphanedAssignments ? [ORPHANED_COLUMN_TAG] : []),
    null,
  ]
  // buildColumn()/renderBoard() hand-off for sizing each column's roster-band-bg overlay — see
  // buildColumn()'s comment on why this needs a second pass after DOM attachment.
  let pendingBandSizing: { bg: HTMLElement; lastCard: HTMLElement }[] = []

  // Hover pop-up (2026-08-16) — appended to document.body, not the card itself, so it isn't
  // clipped by .card-list's/.board's own `overflow: auto` (a popover living inside a scrolling
  // container gets cut off at that container's edge; document.body has no such boundary).
  // Tracked by player_tag (not just the element) so a slow-landing clan-name fetch can check
  // "is this still the card the user is hovering" before mutating anything — the user may have
  // already moved to a different card, or away entirely, by the time the fetch resolves.
  let activeTooltip: { el: HTMLElement; playerTag: string } | null = null

  function hideTooltip(): void {
    if (activeTooltip) {
      activeTooltip.el.remove()
      activeTooltip = null
    }
  }

  // Re-renders the pop-up in place IF the user is still hovering the same player it was opened
  // for — called after either progressive fetch below lands. A no-op once the user has moved to a
  // different card or away entirely, so a slow-landing response can never resurrect or overwrite
  // a pop-up that's already been dismissed or replaced by a different one.
  // No fetch capability at all means nothing to wait for — treat as settled so a caller that
  // never wires up onFetchPlayerStats (e.g. a test) still gets a definite answer instead of the
  // section silently vanishing forever.
  const statsSettled = (playerTag: string): boolean => !onFetchPlayerStats || settledPlayerStatsTags.has(playerTag)

  function refreshTooltipIfStillShowing(player: EnrollmentPlayer, anchor: HTMLElement): void {
    if (!activeTooltip || activeTooltip.playerTag !== player.player_tag) return
    renderTooltipLines(
      activeTooltip.el,
      buildTooltipLines(player, resolveClanName, playerStatsCache.get(player.player_tag), statsSettled(player.player_tag)),
    )
    positionTooltip(activeTooltip.el, anchor)
  }

  function showTooltip(player: EnrollmentPlayer, anchor: HTMLElement): void {
    hideTooltip()
    const el = document.createElement('div')
    el.className = 'player-hover-popup'
    renderTooltipLines(
      el,
      buildTooltipLines(player, resolveClanName, playerStatsCache.get(player.player_tag), statsSettled(player.player_tag)),
    )
    document.body.appendChild(el)
    positionTooltip(el, anchor)
    activeTooltip = { el, playerTag: player.player_tag }

    if (onResolveClanNames) {
      const tagsToResolve = [...new Set([player.current_clan_tag, player.assigned_clan_tag])].filter(
        (tag): tag is string => tag !== null && !clanNameByTag.has(tag) && !attemptedClanTags.has(tag),
      )
      if (tagsToResolve.length > 0) {
        tagsToResolve.forEach((tag) => attemptedClanTags.add(tag))
        onResolveClanNames(tagsToResolve)
          .then((names) => {
            for (const [tag, name] of Object.entries(names)) resolvedClanNames.set(tag, name)
            refreshTooltipIfStillShowing(player, anchor)
          })
          .catch((err: unknown) => console.error('[cwl-activity] clan-name lookup failed:', err))
      }
    }

    if (onFetchPlayerStats && !attemptedPlayerStatsTags.has(player.player_tag)) {
      attemptedPlayerStatsTags.add(player.player_tag)
      onFetchPlayerStats(player.player_tag)
        .then((stats) => {
          if (stats.seasons.length > 0) playerStatsCache.set(player.player_tag, stats)
          settledPlayerStatsTags.add(player.player_tag)
          refreshTooltipIfStillShowing(player, anchor)
        })
        .catch((err: unknown) => {
          console.error('[cwl-activity] player-stats lookup failed:', err)
          // A failed fetch still counts as "settled" — otherwise a persistent network error
          // would leave the pop-up silently missing the CWL-stats section forever instead of
          // ever showing "No data available".
          settledPlayerStatsTags.add(player.player_tag)
          refreshTooltipIfStillShowing(player, anchor)
        })
    }
  }

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
  const legendLabel = document.createElement('span')
  legendLabel.className = 'legend-label'
  legendLabel.textContent = 'User response:'
  legend.append(
    legendLabel,
    buildLegendItem(pendingIconUrl, STATUS_LABEL.pending),
    buildLegendItem(gcheckIconUrl, STATUS_LABEL.confirmed),
    buildLegendItem(redxIconUrl, STATUS_LABEL.declined),
    // Trailing comma appended to this item's own label text, not as a separate flex child
    // (2026-08-16, live-testing feedback: the guest swatch below doesn't belong to the "User
    // response:" group — a comma marks that boundary, but as a sibling flex item it'd pick up the
    // row's own gap on both sides and float away from "Not Linked" instead of reading as one
    // word).
    buildLegendItem(unlinkedIconUrl, UNLINKED_LABEL, ','),
    // Guest indicator (2026-08-16, live-testing feedback: "what is the meaning of the small
    // yellow band" — the .guest-card left-accent already had a hover tooltip, but that's not
    // discoverable without hovering every card; a legend entry is) — a small swatch reproducing
    // the exact same inset box-shadow .guest-card itself uses, so it reads as "this is what that
    // accent means" rather than an unrelated new symbol.
    buildGuestLegendItem(),
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
      ? 'Avg Stars/Attack = plain average stars per attack over each player’s CWL attacks in the last 3 months.'
      : 'Skill Score = league-weighted average stars/attack over each player’s CWL attacks in the last 3 months.'
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
    const matches =
      clanTag === ORPHANED_COLUMN_TAG
        ? (p: EnrollmentPlayer) => p.assigned_clan_tag !== null && !knownClanTags.has(p.assigned_clan_tag)
        : (p: EnrollmentPlayer) => p.assigned_clan_tag === clanTag
    const sorted = sortPlayers(working.filter(matches), sortOrder)
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
    // Subtle left-border accent only (no new element, no height impact) — see .guest-card in
    // index.html for why: this card grid's row-height parity across every column was hard-won
    // (2026-08-14 fix history above), so a guest marker can't risk perturbing it.
    if (player.is_guest) {
      card.classList.add('guest-card')
    }
    card.addEventListener('mouseenter', () => showTooltip(player, card))
    card.addEventListener('mouseleave', hideTooltip)
    card.draggable = true
    card.addEventListener('dragstart', (e) => {
      hideTooltip()
      e.dataTransfer?.setData('text/plain', player.player_tag)
      if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move'
      card.classList.add('dragging')
      isDragging = true
    })
    card.addEventListener('dragend', () => {
      card.classList.remove('dragging')
      isDragging = false
    })

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
    const isOrphanedColumn = clanTag === ORPHANED_COLUMN_TAG
    const column = document.createElement('div')
    column.className = clanTag === null ? 'column column-unassigned' : isOrphanedColumn ? 'column column-orphaned' : 'column'

    const players = playersFor(clanTag)
    const clan = clanTag === null || isOrphanedColumn ? null : payload.clans.find((c) => c.clan_tag === clanTag)
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
    nameSpan.textContent = clanTag === null ? 'Unassigned' : isOrphanedColumn ? 'Assigned to other Guild' : (clan?.name ?? clanTag)
    nameLine.appendChild(nameSpan)
    const countSpan = document.createElement('span')
    countSpan.className = 'column-count'
    countSpan.textContent = rosterSize !== null ? `(${players.length}/${rosterSize})` : `(${players.length})`
    // Roster-filled indicator (live-testing feedback, 2026-08-15) — green once the column has
    // reached or passed its target roster_size, amber while still short. Unassigned has no
    // rosterSize (no target to measure against), so it stays the plain default color.
    if (rosterSize !== null) {
      countSpan.classList.add(players.length >= rosterSize ? 'count-met' : 'count-under')
    }
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
      isDragging = true
    })
    columnHeader.addEventListener('dragend', () => {
      column.classList.remove('dragging')
      isDragging = false
    })
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
      // Not a real assignment target — there's no clan_tag to assign anyone TO here, only
      // players already stuck here to drag back OUT. Column reordering (above) still works.
      if (isOrphanedColumn) return
      const playerTag = e.dataTransfer?.getData('text/plain')
      if (!playerTag) return
      const player = byTag.get(playerTag)
      if (player) handleDrop(player, clanTag)
    })

    return column
  }

  function renderBoard(): void {
    // Every card gets torn down and rebuilt below — a pop-up still anchored to one of the old
    // (about to be removed) card elements would be left pointing at nothing. The next mouseenter
    // (once the user moves the mouse again) shows a fresh one against the new card, same as a
    // native browser tooltip would also disappear when its anchor element is replaced.
    hideTooltip()
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

  // Live-polling support (2026-08-16, live-testing feedback: "would it be possible to auto-update
  // this view whenever a user changes his confirmation setting?"). main.ts calls this on a
  // timer with a freshly-fetched payload's players. Deliberately merges only the fields another
  // person's action could actually change — a member's own DM response (signup_status), their
  // live CoC state (th_level/th_icon_url/current_clan_tag), or another admin's guest-invite
  // action (is_guest/discord_id/player_name) — never `assigned_clan_tag`. That field is this
  // board's own optimistic drag-and-drop state, confirmed or reverted by handleDrop()'s own
  // direct POST response; blindly overwriting it from a poll tick that raced an in-flight local
  // drag would visually snap a card back to its pre-drag column for a moment, then snap forward
  // again once the drag's own response lands — a strictly worse experience than just not touching
  // it here at all. Skips entirely while a native drag gesture is in progress (isDragging) since
  // tearing down the board mid-gesture would silently abort it; the next poll tick catches up.
  function applyPolledUpdate(freshPlayers: EnrollmentPlayer[]): void {
    if (isDragging) return
    let changed = false
    for (const fresh of freshPlayers) {
      const existing = byTag.get(fresh.player_tag)
      if (!existing) {
        // A genuinely new player (e.g. just invited as a guest, or freshly resolved from a
        // linked account) — add them so they show up without needing a manual reopen.
        const added: EnrollmentPlayer = { ...fresh }
        working.push(added)
        byTag.set(added.player_tag, added)
        changed = true
        continue
      }
      if (
        existing.signup_status !== fresh.signup_status ||
        existing.player_name !== fresh.player_name ||
        existing.discord_id !== fresh.discord_id ||
        existing.th_level !== fresh.th_level ||
        existing.th_icon_url !== fresh.th_icon_url ||
        existing.skill_score !== fresh.skill_score ||
        existing.avg_stars !== fresh.avg_stars ||
        existing.cwl_permanent_optout !== fresh.cwl_permanent_optout ||
        existing.current_clan_tag !== fresh.current_clan_tag ||
        existing.is_guest !== fresh.is_guest
      ) {
        existing.signup_status = fresh.signup_status
        existing.player_name = fresh.player_name
        existing.discord_id = fresh.discord_id
        existing.th_level = fresh.th_level
        existing.th_icon_url = fresh.th_icon_url
        existing.skill_score = fresh.skill_score
        existing.avg_stars = fresh.avg_stars
        existing.cwl_permanent_optout = fresh.cwl_permanent_optout
        existing.current_clan_tag = fresh.current_clan_tag
        existing.is_guest = fresh.is_guest
        changed = true
      }
    }
    if (changed) renderBoard()
  }

  renderBoard()
  return { applyPolledUpdate }
}

function buildLegendItem(iconUrl: string, label: string, suffix = ''): HTMLElement {
  const item = document.createElement('span')
  item.className = 'legend-item'
  const icon = document.createElement('img')
  icon.className = 'legend-icon'
  icon.src = iconUrl
  icon.alt = label
  item.append(icon, label + suffix)
  return item
}

function buildGuestLegendItem(): HTMLElement {
  const item = document.createElement('span')
  item.className = 'legend-item'
  const swatch = document.createElement('span')
  swatch.className = 'legend-guest-swatch'
  item.append(swatch, 'Guest (from another clan/guild)')
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
