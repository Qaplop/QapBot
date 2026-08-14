/** Matches qapbot/web_bridge.py's GET/POST /api/cwl/clan-config payload shape exactly. */
export type ClanConfig = {
  clan_tag: string
  name: string
  /** CoC-defined (live war_league), never editable here — see CWL_ROSTER_PLANNING_PLAN.md's
   * tier fix. Null if the clan hasn't been synced by the CoC API yet. */
  tier: string | null
  participating: boolean
  roster_size: number
  /** UTC ISO-ish string ("YYYY-MM-DDTHH:MMZ", matching _parse_cwl_start_time()'s output format
   * on the bot side) or null if unset. */
  cwl_start_at: string | null
}

/** Season selection lives entirely on the Discord-side CWL Management screen (its own season
 * select + "Add New Season" button, CWL_CLAN_CONFIG_ACTIVITY_PLAN.md Phase E.2/E.3) — this
 * Activity has no season picker of its own; `season`/`clans` always just reflect whichever
 * season is currently selected there. */
export type ClanConfigPayload = {
  season: string
  event_status: string | null
  clans: ClanConfig[]
}

/** Which of the two screens to render — set by whichever Discord button fired LAUNCH_ACTIVITY
 * (CACHE.pending_cwl_activity_screen), read once via GET /api/cwl/screen. See
 * CWL_ROSTER_PLANNING_PLAN.md's "Manage Enrollment" architectural decision for why this exists
 * instead of routing by fetched event status. */
export type ScreenPayload = {
  screen: 'clan_config' | 'enrollment'
}

/** Matches qapbot/web_bridge.py's GET /api/cwl/enrollment payload shape exactly. */
export type EnrollmentClan = {
  clan_tag: string
  name: string | null
  tier: string | null
  roster_size: number
}

/** `signup_status` is null when the player is a current clan member who hasn't signed up (or
 * responded to anything) at all yet — still shown on the board, ready for a 1-click Confirm.
 * `assigned_clan_tag` is null for "Unassigned" — the `cwl_assignments` table has no row for
 * this player, never a nullable value on that row itself (see the plan doc's data model note). */
export type EnrollmentPlayer = {
  player_tag: string
  player_name: string | null
  discord_id: string | null
  // The board only ever displays pending/confirmed/declined — the true statuses a member's own
  // DM response can produce. 'withdrawn' remains a value the bridge can send (legacy data, or a
  // future non-board caller of POST /api/cwl/enrollment/signup) but the board treats it the same
  // as null/unknown rather than giving it its own icon — see enrollmentBoard.ts's isVisibleStatus().
  signup_status: 'pending' | 'confirmed' | 'declined' | 'withdrawn' | null
  assigned_clan_tag: string | null
  th_level: number | null
  th_icon_url: string | null
  // League-adjusted average stars/attack over the player's last 10 CWL attacks. Null until the
  // backend computes it (CWL_ROSTER_PLANNING_PLAN.md "Manage Enrollment" — player-skill sort).
  skill_score: number | null
  // Plain, unweighted average stars/attack over the same last <=10 CWL attacks skill_score uses
  // — the board's other number-display option (2026-08-14), null under the same "no CWL attack
  // history" condition as skill_score.
  avg_stars: number | null
  // user_players.cwl_permanent_optout — used to sort permanently-opted-out players to the
  // bottom of the Unassigned column (2026-08-14). Distinct from signup_status === 'declined'
  // (a one-season decline); both push a player to the bottom, see enrollmentBoard.ts's
  // isOptedOut().
  cwl_permanent_optout: boolean
  // user_players.current_clan_tag — null if unknown (e.g. a signed-up player who's since left
  // every guild clan). Compared against assigned_clan_tag to color a card green ("already in
  // their assigned clan") or amber ("assigned elsewhere, hasn't moved yet") — see
  // enrollmentBoard.ts's clanMatchClass().
  current_clan_tag: string | null
}

export type EnrollmentPayload = {
  season: string
  event_status: string | null
  clans: EnrollmentClan[]
  players: EnrollmentPlayer[]
}
