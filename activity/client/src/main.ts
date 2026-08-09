/**
 * Phase A proved the OAuth round-trip and the LAUNCH_ACTIVITY entry point work end to end.
 * Phase B proved the full bridge chain (Worker -> cloudflared tunnel -> QapBot) live. Phase C
 * (this file) renders the real table — see clanConfigTable.ts — and wires Save back to the
 * bridge. See CWL_CLAN_CONFIG_ACTIVITY_PLAN.md.
 */
import { DiscordSDK } from '@discord/embedded-app-sdk'
import { renderClanConfigTable } from './clanConfigTable'
import type { ClanConfig, ClanConfigPayload } from './types'

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

    root.textContent = 'Loading clan configuration…'

    const configResponse = await fetch(`/api/cwl/clan-config?guild_id=${encodeURIComponent(guildId)}`, {
      headers: { Authorization: `Bearer ${accessToken}` },
    })
    if (!configResponse.ok) {
      const body = await configResponse.text()
      throw new Error(`failed to load clan config (${configResponse.status}): ${body}`)
    }
    const payload = (await configResponse.json()) as ClanConfigPayload

    renderClanConfigTable(root, payload, async (clans: ClanConfig[]) => {
      const saveResponse = await fetch('/api/cwl/clan-config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({ guild_id: guildId, clans }),
      })
      if (!saveResponse.ok) {
        const body = await saveResponse.text()
        throw new Error(`${saveResponse.status}: ${body}`)
      }
    })
  } catch (err) {
    console.error(err)
    root.textContent = `Setup failed: ${(err as Error).message}`
  }
}

void setup()
