/**
 * Cloudflare Worker backend for the CWL clan-config Discord Activity.
 * See CWL_CLAN_CONFIG_ACTIVITY_PLAN.md for the full architecture and phase plan.
 *
 * This Worker does two things, on purpose, and nothing else:
 *  1. OAuth2 code -> access_token exchange (CLIENT_SECRET must never reach the frontend).
 *  2. A thin proxy to QapBot's own bridge API, which is the actual source of truth for CWL
 *     data and re-verifies admin status itself — this Worker is a UX/session gate, not the
 *     security boundary. See "Auth & permission model" in the plan doc.
 *
 * Identity verification (Phase B): the discord_user_id forwarded to the bridge is NEVER taken
 * from anything the client sends directly — that would be trivially spoofable (anyone could
 * claim to be a known admin's Discord ID). Every /cwl/clan-config request must carry the
 * user's real OAuth access_token as a Bearer token, which this Worker independently exchanges
 * for the true user id via Discord's own GET /users/@me before forwarding anything to the bridge.
 */
import { Hono, type Context } from 'hono'

type Bindings = {
  CLIENT_ID: string
  CLIENT_SECRET: string
  // Must exactly match a URL registered under OAuth2 -> Redirects in the Developer Portal.
  // Nothing ever navigates here for an Activity — Discord's token endpoint just requires it
  // to be present and registered, per standard OAuth2 authorization_code grant rules.
  REDIRECT_URI: string
  // cloudflared tunnel URL to QapBot's bridge API, and the shared secret it expects.
  BRIDGE_URL?: string
  BRIDGE_SECRET?: string
}

type AppContext = Context<{ Bindings: Bindings }>

// Routes defined unprefixed, then mounted at both "/" and "/api" below — Discord's Activity
// proxy documentation doesn't specify whether a "/api" Proxy Path Mapping strips that prefix
// before forwarding to the target or preserves it, so we just answer to both until confirmed
// empirically (a 404 on first deploy told us the frontend's "/api/token" wasn't reaching a
// registered route — this makes it reach one regardless of which behavior Discord actually has).
const api = new Hono<{ Bindings: Bindings }>()

api.get('/health', (c) => c.json({ ok: true, path: c.req.path }))

api.post('/token', async (c) => {
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
      redirect_uri: c.env.REDIRECT_URI,
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

/** Independently verifies the caller's access_token against Discord itself and returns their
 * real user id — never trust a client-supplied discord_user_id for anything security-relevant. */
async function verifiedDiscordUserId(c: AppContext): Promise<string | null> {
  const auth = c.req.header('Authorization')
  if (!auth?.startsWith('Bearer ')) return null

  const response = await fetch('https://discord.com/api/users/@me', {
    headers: { Authorization: auth },
  })
  if (!response.ok) return null

  const user = await response.json<{ id: string }>()
  return user.id
}

api.get('/cwl/clan-config', async (c) => {
  const guildId = c.req.query('guild_id')
  if (!guildId) return c.json({ error: 'missing guild_id' }, 400)

  const discordUserId = await verifiedDiscordUserId(c)
  if (!discordUserId) return c.json({ error: 'unauthorized' }, 401)

  if (!c.env.BRIDGE_URL || !c.env.BRIDGE_SECRET) return bridgeNotConfigured(c)

  const upstream = await fetch(
    `${c.env.BRIDGE_URL}/api/cwl/clan-config?guild_id=${encodeURIComponent(guildId)}&discord_user_id=${encodeURIComponent(discordUserId)}`,
    { headers: { 'X-Bridge-Secret': c.env.BRIDGE_SECRET } },
  )
  return c.json(await upstream.json(), upstream.status as 200 | 400 | 403 | 500)
})

api.post('/cwl/clan-config', async (c) => {
  const discordUserId = await verifiedDiscordUserId(c)
  if (!discordUserId) return c.json({ error: 'unauthorized' }, 401)

  if (!c.env.BRIDGE_URL || !c.env.BRIDGE_SECRET) return bridgeNotConfigured(c)

  let body: Record<string, unknown>
  try {
    body = await c.req.json()
  } catch {
    return c.json({ error: 'invalid JSON body' }, 400)
  }

  const upstream = await fetch(`${c.env.BRIDGE_URL}/api/cwl/clan-config`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Bridge-Secret': c.env.BRIDGE_SECRET },
    // discord_user_id always comes from the server-verified value above, never from `body` —
    // spreading body first so a client-supplied discord_user_id field, if present, gets
    // overwritten rather than trusted.
    body: JSON.stringify({ ...body, discord_user_id: discordUserId }),
  })
  return c.json(await upstream.json(), upstream.status as 200 | 400 | 403 | 500)
})

const app = new Hono<{ Bindings: Bindings }>()
app.route('/api', api)
app.route('/', api)

// If this still 404s, the response body tells us the exact path Cloudflare actually received —
// the real diagnostic we were missing before this change.
app.notFound((c) => c.json({ error: 'not found', path: c.req.path, method: c.req.method }, 404))

export default app
