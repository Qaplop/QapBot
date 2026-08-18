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
  /** Cross-guild shared CWL clan status (2026-08-15), GET-only (never sent back on save — the
   * backend derives it fresh, see qapbot/web_bridge.py's _build_clan_config_payload). Null when
   * this clan isn't shared with anyone. `is_owner` gates whether the Evict action is offered —
   * only the owner guild may remove another guild from a shared clan
   * (CWL_ROSTER_PLANNING_PLAN.md). */
  shared_with: { is_owner: boolean; other_guild_ids: string[]; other_guild_names: string[] } | null
  /** True when this clan is NOT part of the guild's own CWL family (2026-08-18,
   * CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md rule f) — drives the "Remove" button, which must
   * never be offered for a real family clan. GET-only, computed server-side from
   * resolve_guild_member_clan_tags(); a freshly-added guest clan (still unsaved, from the Guests
   * search "Add" click) is constructed client-side and deliberately does NOT set this field
   * (stays `undefined`/falsy) — Remove is only offered for a clan that's actually on the saved
   * roster, since removing an unsaved row is just "don't click Save." */
  is_guest?: boolean
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
  // cwl_signups.source === 'guest_invite' — set only by the Guests search's individual-player
  // invite (POST /api/cwl/enrollment/guest), never by any other signup path. Display-only badge,
  // doesn't change pool/eligibility logic (2026-08-15).
  is_guest: boolean
}

export type EnrollmentPayload = {
  season: string
  event_status: string | null
  clans: EnrollmentClan[]
  players: EnrollmentPlayer[]
  // The wait loop's starting point (2026-08-17, CWL_PROD_PERFORMANCE_FIX_PLAN.md P1 Step 8) —
  // every GET /api/cwl/enrollment response carries the guild's current enrollment version, so a
  // fresh page load (or a refetch triggered by the wait loop itself) always has a correct
  // known_version to hand the next GET /api/cwl/enrollment/wait call.
  version: number
}

/** GET /api/cwl/enrollment/wait's response shape (2026-08-17, Step 8) — the long-poll backing
 * the client's event-driven wait loop, replacing the old fixed 12s setInterval. `changed: true`
 * means the caller should refetch the full EnrollmentPayload (this response carries no payload
 * of its own); `changed: false` only ever happens after the bridge's hold timeout with no write
 * in between, and `version` always echoes the bridge's authoritative current value either way —
 * the wait loop uses it as the next call's known_version regardless of which branch happened. */
export type WaitResponse = { changed: boolean; version: number }

/** One flat result from GET /api/cwl/guest-search — the Guests invite search on Configure
 * Participating Clans (2026-08-15). A "clan" hit gets added straight into the same `clans` array
 * ClanConfig already edits (POST /api/cwl/clan-config persists it, participating=true, exactly
 * like any other clan — see qapbot/web_bridge.py's _search_cwl_guests docstring for why that
 * needs no separate endpoint). A "player" hit is added via POST /api/cwl/enrollment/guest — its
 * `discord_id` is null when the tag isn't linked to any Discord account yet, in which case
 * "send DM now" isn't offered (there's nobody to DM).
 *
 * `already_shared_with` (clan hits only, 2026-08-15, cross-guild shared CWL clans): the display
 * name of another guild already participating with this clan for the same season, or null if
 * none. Never hides the hit — the admin can still add it, sharing the clan's roster with that
 * other guild (CWL_ROSTER_PLANNING_PLAN.md) — this is just what drives the "already on X's
 * roster, add anyway?" confirmation before the add actually happens. */
export type GuestSearchResult =
  | { type: 'clan'; clan_tag: string; clan_name: string; clan_tier: string | null; already_shared_with: string | null }
  | { type: 'player'; player_tag: string; player_name: string; discord_id: string | null }

/** One row from GET /api/cwl/enrollment/guest-players (2026-08-18, rule g) — every currently
 * pooled guest player (individually invited, or an orphaned leftover from a rule-f guest-clan
 * removal), for the "Remove Guest Players" multi-select. */
export type GuestPlayerPoolEntry = {
  player_tag: string
  player_name: string | null
  current_clan_tag: string | null
  assigned_clan_tag: string | null
}

/** GET /api/cwl/guest-search's response shape (2026-08-17, CWL_PROD_PERFORMANCE_FIX_PLAN.md P0
 * Step 3). `stale` is only ever `true` — never present as `false` — set when a keystroke's search
 * was superseded by a newer one while still queued behind the bridge's per-admin single-flight
 * guard; `results` is always `[]` in that case (the request-id guard client-side already prevents
 * an out-of-order response from rendering, so a `stale: true` response is simply discarded). */
export type GuestSearchResponse = { results: GuestSearchResult[]; stale?: boolean }
