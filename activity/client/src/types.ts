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

/** The subset of ClanConfig carried in `previous_clans` — no `name`/`tier`/`participating`
 * since those are always re-derived live (tier) or implied (participating=true, since
 * get_previous_cwl_event_clans_sync() only ever returns rows that were participating). */
export type PreviousClanConfig = {
  clan_tag: string
  roster_size: number
  cwl_start_at: string | null
}

export type ClanConfigPayload = {
  season: string
  event_status: string | null
  /** All seasons with their own saved event, plus the resolved "current" season and whichever
   * season was requested — always non-empty, sorted newest-first. Powers the season dropdown. */
  available_seasons: string[]
  /** True if `season` already has its own saved cwl_event_clans rows (`clans` below reflects
   * them). False means `clans` is showing plain defaults (Phase E.2) — see carry_over_*. */
  has_own_data: boolean
  /** True only when has_own_data is false AND a previous season has participating clans to
   * offer as a carry-over template (Phase E.4) — the frontend must ask, never auto-apply. */
  carry_over_available: boolean
  carry_over_season: string | null
  clans: ClanConfig[]
  previous_clans: PreviousClanConfig[] | null
}
