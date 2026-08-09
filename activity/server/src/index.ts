/**
 * Cloudflare Worker backend for the CWL clan-config Discord Activity.
 * See CWL_CLAN_CONFIG_ACTIVITY_PLAN.md for the full architecture and phase plan.
 *
 * This Worker does two things, on purpose, and nothing else:
 *  1. OAuth2 code -> access_token exchange (CLIENT_SECRET must never reach the frontend).
 *  2. A thin proxy to QapBot's own bridge API (Phase B+), which is the actual source of
 *     truth for CWL data and re-verifies admin status itself — this Worker is a UX/session
 *     gate, not the security boundary. See "Auth & permission model" in the plan doc.
 */
import { Hono, type Context } from 'hono'

type Bindings = {
  CLIENT_ID: string
  CLIENT_SECRET: string
  // Phase B+: cloudflared tunnel URL to QapBot's bridge API, and the shared secret it expects.
  BRIDGE_URL?: string
  BRIDGE_SECRET?: string
}

type AppContext = Context<{ Bindings: Bindings }>

const app = new Hono<{ Bindings: Bindings }>()

app.get('/api/health', (c) => c.json({ ok: true }))

app.post('/api/token', async (c) => {
  let code: string | undefined
  try {
    ;({ code } = await c.req.json<{ code?: string }>())
  } catch {
    return c.json({ error: 'invalid JSON body' }, 400)
  }
  if (!code) {
    return c.json({ error: 'missing code' }, 400)
  }

  const response = await fetch('https://discord.com/api/oauth2/token', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      client_id: c.env.CLIENT_ID,
      client_secret: c.env.CLIENT_SECRET,
      grant_type: 'authorization_code',
      code,
    }),
  })

  if (!response.ok) {
    const detail = await response.text()
    return c.json({ error: 'token exchange failed', detail }, 502)
  }

  const { access_token } = await response.json<{ access_token: string }>()
  return c.json({ access_token })
})

function bridgeNotConfigured(c: AppContext) {
  return c.json(
    { error: 'bridge not configured yet — see CWL_CLAN_CONFIG_ACTIVITY_PLAN.md Phase B' },
    501,
  )
}

app.get('/api/cwl/clan-config', async (c) => {
  const guildId = c.req.query('guild_id')
  if (!guildId) return c.json({ error: 'missing guild_id' }, 400)
  if (!c.env.BRIDGE_URL || !c.env.BRIDGE_SECRET) return bridgeNotConfigured(c)

  const upstream = await fetch(
    `${c.env.BRIDGE_URL}/api/cwl/clan-config?guild_id=${encodeURIComponent(guildId)}`,
    { headers: { 'X-Bridge-Secret': c.env.BRIDGE_SECRET } },
  )
  return c.json(await upstream.json(), upstream.status as 200 | 400 | 403 | 500)
})

app.post('/api/cwl/clan-config', async (c) => {
  if (!c.env.BRIDGE_URL || !c.env.BRIDGE_SECRET) return bridgeNotConfigured(c)

  const body = await c.req.json()
  const upstream = await fetch(`${c.env.BRIDGE_URL}/api/cwl/clan-config`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Bridge-Secret': c.env.BRIDGE_SECRET },
    body: JSON.stringify(body),
  })
  return c.json(await upstream.json(), upstream.status as 200 | 400 | 403 | 500)
})

export default app
