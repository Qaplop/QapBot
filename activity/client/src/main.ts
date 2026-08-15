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
      throw new Error(`token exchange failed: ${tokenResponse.status}`)
    }
    const { access_token: accessToken } = (await tokenResponse.json()) as { access_token: string }

    await discordSdk.commands.authenticate({ access_token: accessToken })

    const guildId = discordSdk.guildId
    if (!guildId) {
      root.textContent = 'This Activity must be launched from inside a guild.'
      return
    }

    const closeActivity = (reason: string): void => {
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

      renderEnrollmentBoard(
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
        closeActivity,
      )
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
