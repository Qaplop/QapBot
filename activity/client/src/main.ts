/**
 * Phase A proved the OAuth round-trip and the LAUNCH_ACTIVITY entry point work end to end.
 * Phase B proved the full bridge chain (Worker -> cloudflared tunnel -> QapBot) live. Phase C
 * (this file) renders the real table — see clanConfigTable.ts — and wires Save back to the
 * bridge. See CWL_CLAN_CONFIG_ACTIVITY_PLAN.md.
 */
import { DiscordSDK, RPCCloseCodes } from '@discord/embedded-app-sdk'
import { renderClanConfigTable } from './clanConfigTable'
import { renderEnrollmentBoard } from './enrollmentBoard'
import type {
  ClanConfig,
  ClanConfigPayload,
  EnrollmentPayload,
  GuestPlayerPoolEntry,
  GuestSearchResponse,
  ScreenPayload,
  WaitResponse,
} from './types'

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

// Every bridge endpoint that can fail returns `{"error": "human-readable message"}` — this turns
// that into an Error whose .message is just the human-readable text, not the raw response (status
// code + JSON) every one of this file's fetch call sites used to throw verbatim (2026-08-20, live
// bug report: an admin saw "Failed to add MasterBrainEro: 409:
// {"error":"MasterBrainEro is already placed in..."}" — the status code and JSON braces are
// noise the backend's own carefully-written message shouldn't have to fight through). Falls back
// to the raw `${status}: ${body}` shape only when the body isn't the expected JSON error shape at
// all (a proxy/gateway error page, an empty body, etc.) — better a rough fallback than swallowing
// a real failure silently.
function errorFromResponse(status: number, rawBody: string): Error {
  try {
    const parsed = JSON.parse(rawBody) as { error?: unknown }
    if (typeof parsed.error === 'string' && parsed.error.length > 0) return new Error(parsed.error)
  } catch {
    // Not JSON at all — fall through to the raw shape below.
  }
  return new Error(`${status}: ${rawBody}`)
}

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
            throw errorFromResponse(response.status, body)
          }
        },
        async (reason: string) => {
          waitLoopStopped = true
          waitController?.abort()
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
        // Right-click "Remove guest player" (2026-08-19, guest-player provenance feature) — same
        // endpoint the Configure Participating Clans panel above uses, single-tag. A guest-clan-
        // derived player comes back in `rejected` (this card offers the menu to every guest
        // player, clan-derived or not — see buildCard's own comment for why) — surfaced as a
        // thrown Error so the board's existing onRemoveGuestPlayer .catch() shows it inline via
        // the footer status text, same as every other action failure on this board.
        async (playerTag: string) => {
          const removeResponse = await fetch('/api/cwl/enrollment/guest-players/remove', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${accessToken}` },
            body: JSON.stringify({ guild_id: guildId, player_tags: [playerTag] }),
          })
          if (!removeResponse.ok) {
            const body = await removeResponse.text()
            throw errorFromResponse(removeResponse.status, body)
          }
          const { rejected } = (await removeResponse.json()) as {
            rejected: { player_tag: string; clan_name: string }[]
          }
          if (rejected.length > 0) {
            throw new Error(
              `This player was added by guest clan ${rejected[0].clan_name} — only removing the whole guest clan will remove them.`,
            )
          }
        },
      )

      // Event-driven live-update (2026-08-17, CWL_PROD_PERFORMANCE_FIX_PLAN.md P1 Step 8 —
      // replaces the old fixed 12s setInterval poll that answered live-testing feedback: "would
      // it be possible to auto-update this view whenever a user changes his confirmation
      // setting?"). Long-polls GET /api/cwl/enrollment/wait, which the bridge holds open for up
      // to ~25s and releases the instant any write bumps the guild's enrollment version — every
      // hop in between only ever sees an ordinary HTTP request, no streaming/protocol upgrade.
      // On `changed: true`, refetches the full payload and merges just the externally-changeable
      // fields (see enrollmentBoard.ts's applyPolledUpdate for exactly which, and why assignment
      // deliberately isn't one of them) via the SAME applyPolledUpdate the old poll used — the
      // mid-drag deferral there is reused as-is, unaware anything about the trigger changed.
      let knownVersion = payload.version
      let waitLoopStopped = false
      let waitController: AbortController | null = null

      const refetchEnrollmentAndApply = async (): Promise<void> => {
        const pollResponse = await fetch(`/api/cwl/enrollment?guild_id=${encodeURIComponent(guildId)}`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        })
        if (!pollResponse.ok) throw new Error(`enrollment refetch failed: ${pollResponse.status}`)
        const freshPayload = (await pollResponse.json()) as EnrollmentPayload
        knownVersion = freshPayload.version
        boardHandle.applyPolledUpdate(freshPayload.players)
      }

      // Tab hidden -> paused (no wait/refetch traffic at all while nobody can see the board);
      // resumed -> one immediate refetch (may have missed a change while paused, since a hidden
      // tab still has no wait in flight to be notified by) before the wait loop picks back up.
      const waitForVisible = (): Promise<void> =>
        new Promise((resolve) => {
          const onVisibilityChange = () => {
            if (document.visibilityState === 'visible') {
              document.removeEventListener('visibilitychange', onVisibilityChange)
              resolve()
            }
          }
          document.addEventListener('visibilitychange', onVisibilityChange)
        })

      void (async () => {
        let consecutiveFailures = 0
        while (!waitLoopStopped) {
          if (document.visibilityState === 'hidden') {
            await waitForVisible()
            if (waitLoopStopped) break
            try {
              await refetchEnrollmentAndApply()
            } catch (err) {
              console.error('[cwl-activity] enrollment refetch on resume failed:', err)
            }
            continue
          }

          try {
            waitController = new AbortController()
            const waitResponse = await fetch(
              `/api/cwl/enrollment/wait?guild_id=${encodeURIComponent(guildId)}&known_version=${encodeURIComponent(String(knownVersion))}`,
              { headers: { Authorization: `Bearer ${accessToken}` }, signal: waitController.signal },
            )
            if (!waitResponse.ok) throw new Error(`wait failed: ${waitResponse.status}`)
            const waitBody = (await waitResponse.json()) as WaitResponse
            consecutiveFailures = 0
            knownVersion = waitBody.version
            if (waitBody.changed) {
              await refetchEnrollmentAndApply()
            }
            // changed: false only happens after the bridge's own hold timeout with no write in
            // between — immediately re-issue the wait (next loop iteration) with the same
            // (still-current) knownVersion.
          } catch (err) {
            if ((err as Error).name === 'AbortError') return // waitLoopStopped — cleanup requested
            consecutiveFailures += 1
            console.error('[cwl-activity] enrollment wait failed:', err)
            // Safety net (bot restart, tunnel blip, any non-200): once backed off 3+ times in a
            // row, also refetch on every backoff cycle so the board doesn't go stale for the
            // whole backoff duration — a plain ~60s-cadence poll layered on top of the retry
            // loop below, not a separate code path, and it stops the moment a wait succeeds
            // again (consecutiveFailures resets to 0 above).
            if (consecutiveFailures >= 3) {
              try {
                await refetchEnrollmentAndApply()
              } catch (refetchErr) {
                console.error('[cwl-activity] degraded-mode enrollment refetch failed:', refetchErr)
              }
            }
            const backoffMs = Math.min(5000 * 2 ** (consecutiveFailures - 1), 60000)
            await new Promise((resolve) => setTimeout(resolve, backoffMs))
          }
        }
      })()
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

    // Aborts the previous guest-search fetch the instant a newer one fires (2026-08-17,
    // CWL_PROD_PERFORMANCE_FIX_PLAN.md P0 Step 5) — belt-and-suspenders alongside the bridge's
    // own single-flight guard (Step 3) and the searchRequestId stale-render guard just below:
    // this stops an obsolete request from even finishing the network round-trip, instead of only
    // discarding it after it lands.
    let guestSearchController: AbortController | null = null

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
          throw errorFromResponse(saveResponse.status, body)
        }
      },
      closeActivity,
      async (query: string) => {
        guestSearchController?.abort()
        const controller = new AbortController()
        guestSearchController = controller
        const searchResponse = await fetch(
          `/api/cwl/guest-search?guild_id=${encodeURIComponent(guildId)}&q=${encodeURIComponent(query)}`,
          { headers: { Authorization: `Bearer ${accessToken}` }, signal: controller.signal },
        )
        if (!searchResponse.ok) {
          const body = await searchResponse.text()
          throw errorFromResponse(searchResponse.status, body)
        }
        const body = (await searchResponse.json()) as GuestSearchResponse
        // A search superseded while queued behind the bridge's single-flight guard (Step 3) —
        // results is always [] here; the caller's own searchRequestId guard would already
        // discard this, but returning [] directly keeps that behavior explicit and avoids ever
        // rendering an empty-but-real-looking result set as if it were a genuine "no matches".
        if (body.stale) return []
        return body.results
      },
      async (result) => {
        const guestResponse = await fetch('/api/cwl/enrollment/guest', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${accessToken}` },
          body: JSON.stringify({
            guild_id: guildId,
            player_tag: result.player_tag,
            player_name: result.player_name,
            discord_id: result.discord_id,
          }),
        })
        if (!guestResponse.ok) {
          const body = await guestResponse.text()
          throw errorFromResponse(guestResponse.status, body)
        }
      },
      async (clanTag: string, targetGuildId: string) => {
        const evictResponse = await fetch('/api/cwl/shared-clan/evict', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${accessToken}` },
          body: JSON.stringify({ guild_id: guildId, clan_tag: clanTag, target_guild_id: targetGuildId }),
        })
        if (!evictResponse.ok) {
          const body = await evictResponse.text()
          throw errorFromResponse(evictResponse.status, body)
        }
      },
      async (clanTag: string) => {
        const removeResponse = await fetch('/api/cwl/enrollment/guest-clan/remove', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${accessToken}` },
          body: JSON.stringify({ guild_id: guildId, clan_tag: clanTag }),
        })
        if (!removeResponse.ok) {
          const body = await removeResponse.text()
          throw errorFromResponse(removeResponse.status, body)
        }
      },
      async () => {
        const listResponse = await fetch(
          `/api/cwl/enrollment/guest-players?guild_id=${encodeURIComponent(guildId)}`,
          { headers: { Authorization: `Bearer ${accessToken}` } },
        )
        if (!listResponse.ok) {
          const body = await listResponse.text()
          throw errorFromResponse(listResponse.status, body)
        }
        const body = (await listResponse.json()) as { players: GuestPlayerPoolEntry[] }
        return body.players
      },
      async (playerTags: string[]) => {
        const removeResponse = await fetch('/api/cwl/enrollment/guest-players/remove', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${accessToken}` },
          body: JSON.stringify({ guild_id: guildId, player_tags: playerTags }),
        })
        if (!removeResponse.ok) {
          const body = await removeResponse.text()
          throw errorFromResponse(removeResponse.status, body)
        }
        const { rejected } = (await removeResponse.json()) as {
          rejected: { player_tag: string; clan_name: string }[]
        }
        return { rejected }
      },
    )
  } catch (err) {
    console.error(err)
    root.textContent = `Setup failed: ${(err as Error).message}`
  }
}

void setup()
