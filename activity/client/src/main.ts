/**
 * Phase A proved the OAuth round-trip and the LAUNCH_ACTIVITY entry point work end to end.
 * Phase B proved the full bridge chain (Worker -> cloudflared tunnel -> QapBot) live. Phase C
 * (this file) renders the real table — see clanConfigTable.ts — and wires Save back to the
 * bridge. See CWL_CLAN_CONFIG_ACTIVITY_PLAN.md.
 */
import { DiscordSDK, RPCCloseCodes } from '@discord/embedded-app-sdk'
import { renderClanConfigTable } from './clanConfigTable'
import { renderEnrollmentBoard } from './enrollmentBoard'
import type { ClanConfig, ClanConfigPayload, EnrollmentPayload, GuestSearchResult, ScreenPayload } from './types'

const clientId = import.meta.env.VITE_CLIENT_ID as string | undefined

// Session-scoped access_token cache (2026-08-16, live-testing feedback: opening/closing the
// Activity repeatedly in a short span hit Discord's own OAuth token-endpoint rate limit —
// "You are being rate limited for requesting too many tokens" — because every single launch,
// even reopening the same view seconds later, did a completely fresh authorize()+/api/token
// round-trip. Confirmed via live wrangler tail: each launch really was exactly one legitimate
// authorize+exchange pair, not a bug duplicating calls — the limit is purely a function of launch
// *count* in a short window. authenticate() is documented to accept a previously-obtained
// access_token directly (it returns a fresh `expires` either way), so a cached token can skip
// authorize()+/api/token entirely on the next launch within the same browser session — falling
// back to the full flow the moment the cached token is ever rejected. sessionStorage (not
// module-level state) because Discord reloads this script fresh on every launch, but keeps the
// same top-level browsing context alive across repeated launches within one Discord session.
const CACHED_TOKEN_KEY = 'cwl-activity-access-token'

async function setup(): Promise<void> {
  const root = document.getElementById('app')
  if (!root) return

  if (!clientId) {
    root.textContent = 'Missing VITE_CLIENT_ID — copy .env.example to .env.local and fill it in.'
    return
  }

  const discordSdk = new DiscordSDK(clientId)

  try {
    await discordSdk.ready()

    let accessToken: string | null = null
    const cachedToken = sessionStorage.getItem(CACHED_TOKEN_KEY)
    if (cachedToken) {
      try {
        await discordSdk.commands.authenticate({ access_token: cachedToken })
        accessToken = cachedToken
      } catch (err) {
        // Cached token expired, revoked, or otherwise rejected — fall through to a full,
        // fresh authorize()+exchange below. Never treat this as fatal.
        console.log('[cwl-activity] cached access_token rejected, re-authorizing:', err)
        sessionStorage.removeItem(CACHED_TOKEN_KEY)
      }
    }

    if (!accessToken) {
      const { code } = await discordSdk.commands.authorize({
        client_id: clientId,
        response_type: 'code',
        state: '',
        prompt: 'none',
        scope: ['identify', 'guilds'],
      })

      // Runs through Discord's own proxy (/api/*) straight to our Worker — never call the
      // Worker's absolute URL directly from inside the iframe.
      const tokenResponse = await fetch('/api/token', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code }),
      })
      if (!tokenResponse.ok) {
        // Surface Discord's own rejection reason (2026-08-16, live-testing feedback: a bare
        // "token exchange failed: 502" gave no way to tell invalid_grant (code already used, or
        // expired) apart from Discord's own OAuth rate limit — the Worker always had this in its
        // response body, it just never reached the screen).
        let detail = ''
        try {
          const body = (await tokenResponse.json()) as { detail?: string }
          if (body.detail) detail = ` — ${body.detail}`
        } catch {
          // Body wasn't JSON (e.g. a raw Cloudflare error page) — fall back to the bare status.
        }
        throw new Error(`token exchange failed: ${tokenResponse.status}${detail}`)
      }
      ;({ access_token: accessToken } = (await tokenResponse.json()) as { access_token: string })

      await discordSdk.commands.authenticate({ access_token: accessToken })
      sessionStorage.setItem(CACHED_TOKEN_KEY, accessToken)
    }

    const guildId = discordSdk.guildId
    if (!guildId) {
      root.textContent = 'This Activity must be launched from inside a guild.'
      return
    }

    const closeActivity = async (reason: string): Promise<void> => {
      // Notify the bot BEFORE actually closing (2026-08-16, live-testing feedback: on iPad, the
      // Hub message's launch buttons stayed visibly greyed out/unresponsive after closing the
      // Activity — Discord's own client-side "an Activity was launched from this message" visual
      // state, which only the SAVE flow's existing Hub-message refresh happened to incidentally
      // clear, since that's a genuine new message edit, not a response to the now-stale original
      // interaction. Closing WITHOUT saving (a plain view, or Cancel) never triggered any refresh
      // at all. Best-effort with a short timeout — this must never meaningfully delay the actual
      // close the user is waiting on, and a failure here is never worth blocking it over.
      try {
        await Promise.race([
          fetch('/api/cwl/activity-closed', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${accessToken}` },
            body: JSON.stringify({ guild_id: guildId }),
          }),
          new Promise((resolve) => setTimeout(resolve, 1500)),
        ])
      } catch (err) {
        console.error('[cwl-activity] activity-closed notification failed:', err)
      }

      // Diagnostic logging (visible via Discord's own Activity dev console) in case this
      // silently no-ops for some environments — console output is routed to Discord's client
      // by the SDK itself (see Discord.d.ts's overrideConsoleLogging warning).
      console.log(`[cwl-activity] calling discordSdk.close(CLOSE_NORMAL, "${reason}")`)
      try {
        discordSdk.close(RPCCloseCodes.CLOSE_NORMAL, reason)
      } catch (err) {
        console.error('[cwl-activity] discordSdk.close() threw:', err)
      }
    }

    root.textContent = 'Loading…'

    // Which screen to render is decided by the bot, not by fetched event status — see
    // CWL_ROSTER_PLANNING_PLAN.md's "Manage Enrollment" architectural decision for why (the two
    // screens must stay independently reachable at any event status, not gated by it).
    const screenResponse = await fetch(`/api/cwl/screen?guild_id=${encodeURIComponent(guildId)}`, {
      headers: { Authorization: `Bearer ${accessToken}` },
    })
    if (!screenResponse.ok) {
      const body = await screenResponse.text()
      throw new Error(`failed to resolve screen (${screenResponse.status}): ${body}`)
    }
    const { screen } = (await screenResponse.json()) as ScreenPayload

    if (screen === 'enrollment') {
      root.textContent = 'Loading enrollment…'

      const enrollmentResponse = await fetch(`/api/cwl/enrollment?guild_id=${encodeURIComponent(guildId)}`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      })
      if (!enrollmentResponse.ok) {
        const body = await enrollmentResponse.text()
        throw new Error(`failed to load enrollment (${enrollmentResponse.status}): ${body}`)
      }
      const payload = (await enrollmentResponse.json()) as EnrollmentPayload

      const boardHandle = renderEnrollmentBoard(
        root,
        payload,
        async (playerTag: string, clanTag: string | null) => {
          const response = await fetch('/api/cwl/enrollment/assign', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${accessToken}` },
            body: JSON.stringify({ guild_id: guildId, player_tag: playerTag, clan_tag: clanTag }),
          })
          if (!response.ok) {
            const body = await response.text()
            throw new Error(`${response.status}: ${body}`)
          }
        },
        async (reason: string) => {
          clearInterval(pollTimer)
          await closeActivity(reason)
        },
        // Hover pop-up progressive fetch (2026-08-16, project owner's spec: show the pop-up
        // instantly with what's already loaded, then fill in a clan name it didn't already have).
        // A failed lookup is silently swallowed (logged only) — the pop-up just keeps showing the
        // raw tag, same as it would for a genuinely unknown clan.
        async (tags: string[]) => {
          const namesResponse = await fetch(
            `/api/cwl/clan-names?guild_id=${encodeURIComponent(guildId)}&tags=${encodeURIComponent(tags.join(','))}`,
            { headers: { Authorization: `Bearer ${accessToken}` } },
          )
          if (!namesResponse.ok) return {}
          const { names } = (await namesResponse.json()) as { names: Record<string, string> }
          return names
        },
        // Second half of the same progressive fetch (2026-08-16, project owner's spec: attacks/
        // missed CWL attacks/attack-defense ratio over a player's last 3 CWL months, computed
        // exactly as /leaderboard would). A failed lookup (or a player with no CWL history at
        // all) just means the pop-up's stats section never grows.
        async (playerTag: string) => {
          const statsResponse = await fetch(
            `/api/cwl/player-stats?guild_id=${encodeURIComponent(guildId)}&player_tag=${encodeURIComponent(playerTag)}`,
            { headers: { Authorization: `Bearer ${accessToken}` } },
          )
          if (!statsResponse.ok) return { seasons: [], attacks: null, missed_attacks: null, attack_defense_ratio: null }
          return (await statsResponse.json()) as {
            seasons: string[]
            attacks: number | null
            missed_attacks: number | null
            attack_defense_ratio: number | null
          }
        },
      )

      // Live-polling (2026-08-16, live-testing feedback: "would it be possible to auto-update
      // this view whenever a user changes his confirmation setting?") — re-fetches the same
      // enrollment payload on a timer and merges just the externally-changeable fields (see
      // enrollmentBoard.ts's applyPolledUpdate for exactly which, and why assignment isn't one of
      // them). 12s balances feeling reasonably live against not hammering the bridge/bot while
      // several admins could plausibly have this board open at once. A failed poll is silently
      // skipped (logged only) — the next tick tries again; it must never surface as a page error
      // the way the initial load's own fetch failures do.
      const POLL_INTERVAL_MS = 12000
      const pollTimer = setInterval(() => {
        void (async () => {
          try {
            const pollResponse = await fetch(`/api/cwl/enrollment?guild_id=${encodeURIComponent(guildId)}`, {
              headers: { Authorization: `Bearer ${accessToken}` },
            })
            if (!pollResponse.ok) return
            const freshPayload = (await pollResponse.json()) as EnrollmentPayload
            boardHandle.applyPolledUpdate(freshPayload.players)
          } catch (err) {
            console.error('[cwl-activity] enrollment poll failed:', err)
          }
        })()
      }, POLL_INTERVAL_MS)
      return
    }

    // No season query param — the bridge always resolves whichever season is currently
    // selected on the Discord-side CWL Management screen (CWL_CLAN_CONFIG_ACTIVITY_PLAN.md
    // Phase E.2/E.3); this Activity has no season picker of its own.
    const configResponse = await fetch(`/api/cwl/clan-config?guild_id=${encodeURIComponent(guildId)}`, {
      headers: { Authorization: `Bearer ${accessToken}` },
    })
    if (!configResponse.ok) {
      const body = await configResponse.text()
      throw new Error(`failed to load clan config (${configResponse.status}): ${body}`)
    }
    const payload = (await configResponse.json()) as ClanConfigPayload

    renderClanConfigTable(
      root,
      payload,
      async (clans: ClanConfig[]) => {
        const saveResponse = await fetch('/api/cwl/clan-config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${accessToken}` },
          body: JSON.stringify({ guild_id: guildId, clans }),
        })
        if (!saveResponse.ok) {
          const body = await saveResponse.text()
          throw new Error(`${saveResponse.status}: ${body}`)
        }
      },
      closeActivity,
      async (query: string) => {
        const searchResponse = await fetch(
          `/api/cwl/guest-search?guild_id=${encodeURIComponent(guildId)}&q=${encodeURIComponent(query)}`,
          { headers: { Authorization: `Bearer ${accessToken}` } },
        )
        if (!searchResponse.ok) {
          const body = await searchResponse.text()
          throw new Error(`${searchResponse.status}: ${body}`)
        }
        const { results } = (await searchResponse.json()) as { results: GuestSearchResult[] }
        return results
      },
      async (result, sendDmNow: boolean) => {
        const guestResponse = await fetch('/api/cwl/enrollment/guest', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${accessToken}` },
          body: JSON.stringify({
            guild_id: guildId,
            player_tag: result.player_tag,
            player_name: result.player_name,
            discord_id: result.discord_id,
            send_dm_on_save: sendDmNow,
          }),
        })
        if (!guestResponse.ok) {
          const body = await guestResponse.text()
          throw new Error(`${guestResponse.status}: ${body}`)
        }
        return (await guestResponse.json()) as { dm_sent: boolean }
      },
      async (clanTag: string, targetGuildId: string) => {
        const evictResponse = await fetch('/api/cwl/shared-clan/evict', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${accessToken}` },
          body: JSON.stringify({ guild_id: guildId, clan_tag: clanTag, target_guild_id: targetGuildId }),
        })
        if (!evictResponse.ok) {
          const body = await evictResponse.text()
          throw new Error(`${evictResponse.status}: ${body}`)
        }
      },
    )
  } catch (err) {
    console.error(err)
    root.textContent = `Setup failed: ${(err as Error).message}`
  }
}

void setup()
