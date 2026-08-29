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
   * never be offered for a real family clan. GET-only from the backend, computed server-side from
   * resolve_guild_member_clan_tags(); a freshly-added guest clan (still unsaved, from the Guests
   * search "Add" click) is constructed client-side and sets this to `true` too (2026-08-19,
   * project owner's spec: Remove must appear immediately on Add, not only after Save+reopen —
   * every clan the Guests search can add is a guest by construction, so this is always correct
   * for that path). clanConfigTable.ts's removeGuestClanRow separately tracks which clan tags are
   * actually persisted (`persistedClanTags`, snapshotted from this same payload before any
   * client-side Add) so that clicking Remove on a still-unsaved row never fires a network call
   * against a clan the backend has never heard of yet. */
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

/** Which of the three screens to render — set by whichever Discord button fired LAUNCH_ACTIVITY
 * (CACHE.pending_cwl_activity_screen), read once via GET /api/cwl/screen. See
 * CWL_ROSTER_PLANNING_PLAN.md's "Manage Enrollment" architectural decision for why this exists
 * instead of routing by fetched event status. 'player_prefs' (2026-08-23, plans/
 * cwl-personal-hub.md Phase 5a) is CwlPlayerHubView's single button / the /cwl preferences
 * command — member-facing, no admin gate on the bridge side. */
export type ScreenPayload = {
  screen: 'clan_config' | 'enrollment' | 'player_prefs'
}

/** Matches qapbot/web_bridge.py's GET /api/cwl/enrollment payload shape exactly. */
export type EnrollmentClan = {
  clan_tag: string
  name: string | null
  tier: string | null
  roster_size: number
}

/** `signup_status` is null when the player is a current clan member who hasn't signed up (or
 * responded to anything) at all yet — still shown on the board, waiting on their own DM response.
 * `assigned_clan_tag` is null for "Unassigned" — the `cwl_assignments` table has no row for
 * this player, never a nullable value on that row itself (see the plan doc's data model note). */
export type EnrollmentPlayer = {
  player_tag: string
  player_name: string | null
  discord_id: string | null
  // Populated from CACHE.user_accounts (the live gateway member cache), not a DB column — null
  // whenever discord_id is null, and also whenever discord_id is set but the account isn't in
  // that cache for any reason (2026-08-22, live-testing feedback: the tooltip showed a bare
  // "Linked" with no way to tell who — see enrollmentBoard.ts's buildTooltip).
  discord_display_name: string | null
  // The board only ever displays pending/confirmed/declined/auto_confirmed — the true statuses a
  // member's own DM response can produce, plus 'auto_confirmed' (2026-08-23, plans/
  // cwl-personal-hub.md Phase 4a): seeded automatically by a standing opt-in preference at Start
  // Enrollment, never written by an admin action or a member's own click — a real DM response
  // always overwrites it with 'confirmed'/'declined'. 'withdrawn' is legacy-only (2026-08-19: its
  // one writer, the board's now-removed 1-click admin confirm/withdraw control and its POST
  // /api/cwl/enrollment/signup endpoint, was deleted outright — dead code with no real caller,
  // per CWL_ROSTER_PLANNING_PLAN.md's own "known cleanup opportunity" note) — kept in the type
  // purely so any pre-existing DB row still carrying it renders without crashing; the board
  // treats it the same as null/unknown rather than giving it its own icon — see
  // enrollmentBoard.ts's isVisibleStatus().
  signup_status: 'pending' | 'confirmed' | 'declined' | 'auto_confirmed' | 'withdrawn' | null
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
  // The player's preferred CWL league tier (tracker #0057, e.g. "Master League I") — the LIVE
  // standing default (user_players.cwl_default_preferred_league_rank, the "Bevorzugte Liga"
  // dropdown in playerPrefs.ts) whenever known, falling back to the frozen enrollment-time
  // snapshot (cwl_signups.preferred_league_rank) only if the player has no user_players row at
  // all (tracker #0058 correction, 2026-08-29: the snapshot is NOT a genuine distinct per-invite
  // answer — no response flow ever asks that question — so showing it ahead of a live, possibly
  // since-changed standing default was stale-by-design; see web_bridge.py's build_enrollment_
  // payload for the exact precedence). Null when neither is set — display-only, shown in the
  // hover tooltip's identity block only when non-null (see enrollmentBoard.ts's buildTooltipLines).
  preferred_league_rank: string | null
  // True only once a DM has genuinely reached this player this season — the same dm_sent bulk
  // lookup _send_cwl_enrollment_dm_batch's own dedup check and the season overview's "New
  // players without DM invitation" line already use (2026-08-29, project owner's clarification).
  // Deliberately NOT derived from signup_status === null: a 'pending' cwl_signups row gets
  // seeded before a DM is even attempted (tracker #0016, so its buttons have something to
  // resolve against), so a player whose actual send never went out — DM guard, blocked, left
  // every mutual guild, a transient failure — still shows signup_status 'pending' despite never
  // having received anything. This is the one field the board actually needs to tell "sent,
  // awaiting response" apart from "never invited yet" (enrollmentBoard.ts's icon logic).
  dm_sent: boolean
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

/** The three statuses an admin may set from the board's right-click "Set enrollment status"
 * submenu (2026-08-22, tracker #0014). A strict subset of EnrollmentPlayer.signup_status:
 * `null` isn't settable (it means "no row yet", not a choice) and `withdrawn` is legacy-only.
 * Must stay in sync with ADMIN_SETTABLE_ENROLLMENT_STATUSES in qapbot/web_bridge.py, which
 * rejects anything else with a 400. */
export type AdminSettableStatus = 'confirmed' | 'declined' | 'pending'

/** POST /api/cwl/enrollment/status's response shape. `dm` is null for confirmed/declined (those
 * deliberately never touch the player's DM — see the bridge handler's docstring on why that is
 * what makes "last action wins" hold) and is populated only for `pending`, whose whole point is
 * retracting the old DM and sending a fresh one. `reason` is only meaningful when `sent` is
 * false: 'unlinked' (nobody owns the account), 'blocked' (DMs closed / bot blocked), 'dm_guard'
 * (CONFIG.cwl_dm_restrict_to_admin is on and this recipient isn't exempt), or 'failed'
 * (transient error, retries exhausted). */
export type SetStatusResult = {
  ok: boolean
  status: AdminSettableStatus
  dm: { sent: boolean; reason: 'unlinked' | 'blocked' | 'dm_guard' | 'failed' | null } | null
}

/** Matches qapbot/web_bridge.py's GET/POST /api/cwl/player-prefs payload shape exactly
 * (2026-08-23, plans/cwl-personal-hub.md Phase 5c). One account's standing CWL preference —
 * block I of the player_prefs screen. */
export type PlayerPrefsAccount = {
  player_tag: string
  player_name: string | null
  verified: boolean
  /** user_players.cwl_default_preferred_league_rank — null = no preference. */
  preferred_league_rank: string | null
  /** 'none' = ask each season (the pre-existing default), 'optin' = always play,
   * 'optout' = never play. Mutually exclusive by construction — see set_cwl_preferences_sync's
   * own docstring (db_manager.py) for why a single UPDATE enforces this server-side too. */
  mode: 'none' | 'optin' | 'optout'
  /** Only meaningful while mode === 'optout' — whether the invitation DM still goes out despite
   * the standing decline, so the member can override it for one season. */
  send_dm_anyway: boolean
}

/** One account's row in block II ("this season"). Every field is null when the account has no
 * cwl_signups row for the current event yet (still shown — "no row" reads as Unassigned/no
 * status, not omitted from the table). */
export type PlayerPrefsSeasonRow = {
  player_tag: string
  player_name: string | null
  /** Same status vocabulary as EnrollmentPlayer.signup_status, including 'auto_confirmed'. */
  signup_status: 'pending' | 'confirmed' | 'declined' | 'auto_confirmed' | 'withdrawn' | null
  assigned_clan_tag: string | null
  assigned_clan_name: string | null
  assigned_clan_tier: string | null
  /** Same UTC ISO-ish format as ClanConfig.cwl_start_at. */
  assigned_clan_start_at: string | null
}

/** GET /api/cwl/player-prefs's response shape, and also what POST /api/cwl/player-prefs
 * returns (the freshly rebuilt payload, so the client always re-renders from server truth —
 * see playerPrefs.ts's own "no optimistic update" discipline). `season`/`event_status` are both
 * null when the guild has no CWL event at all; `season_rows` is `[]` in that case too. */
export type PlayerPrefsPayload = {
  season: string | null
  event_status: string | null
  accounts: PlayerPrefsAccount[]
  season_rows: PlayerPrefsSeasonRow[]
}

/** One change in a POST /api/cwl/player-prefs request — player_tag: null means "apply to every
 * one of my linked accounts" (the bulk control). league_rank/rank_provided mirrors
 * set_cwl_preferences_sync's own disambiguation: league_rank=null is only a clear-to-no-
 * preference when rank_provided is true, never an accidental no-op. */
export type PlayerPrefsChange = {
  player_tag: string | null
  mode?: 'none' | 'optin' | 'optout'
  send_dm_anyway?: boolean
  league_rank?: string | null
  rank_provided?: boolean
}

/** POST /api/cwl/player-prefs/status's request shape — the member changing their own invitation
 * status from block II. Deliberately the same 'action' vocabulary as the DM button's own
 * custom_id (cwl:signup:confirm|optout), not 'status', because the whole contract of this
 * endpoint is "do exactly what that button does". */
export type PlayerPrefsStatusAction = 'confirm' | 'optout'

/** POST /api/cwl/player-prefs/status's response shape on success — the freshly rebuilt
 * PlayerPrefsPayload, same "no optimistic update" discipline as the preferences POST. On
 * failure the bridge returns {"error": "<cwl.template code>"} instead (see errorFromResponse in
 * main.ts and this screen's own footer-status-line handling). */
export type PlayerPrefsStatusResult = PlayerPrefsPayload
