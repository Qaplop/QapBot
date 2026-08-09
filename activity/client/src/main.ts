/**
 * Phase A proved the OAuth round-trip and the LAUNCH_ACTIVITY entry point work end to end.
 * Phase B adds a smoke test of the full bridge chain (Worker -> cloudflared tunnel -> QapBot),
 * fetching real clan-config data and rendering it as raw JSON — not the real table yet, that's
 * Phase C. See CWL_CLAN_CONFIG_ACTIVITY_PLAN.md.
 */
import { DiscordSDK } from '@discord/embedded-app-sdk'

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
    root.textContent = `Hello, guild ${guildId ?? '(no guild context)'} — OAuth round-trip OK.\n\nFetching clan-config...`

    if (guildId) {
      const configResponse = await fetch(`/api/cwl/clan-config?guild_id=${encodeURIComponent(guildId)}`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      })
      const configBody = await configResponse.json()
      root.textContent =
        `Hello, guild ${guildId} — OAuth round-trip OK.\n\n` +
        `GET /api/cwl/clan-config -> ${configResponse.status}\n` +
        JSON.stringify(configBody, null, 2)
    }
  } catch (err) {
    console.error(err)
    root.textContent = `Setup failed: ${(err as Error).message}`
  }
}

void setup()
