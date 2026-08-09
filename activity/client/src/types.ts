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
