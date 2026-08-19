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
    // Logged (2026-08-16, live-testing feedback: repeated open/close of the Activity eventually
    // hit "token exchange failed: 502" with no way to tell why — this Worker already had
    // Discord's own real rejection reason in `detail`, it just never went anywhere visible).
    // `wrangler tail` / Cloudflare's dashboard Logs now show Discord's actual status + body —
    // almost certainly either `invalid_grant` (the authorization code was already exchanged
    // once, or expired — Discord codes are single-use and short-lived) or a 429 from Discord's
    // own OAuth rate limit on repeated token exchanges in a short window, both very plausible
    // for "opened and closed the Activity several times in a row."
    console.error(`[token] Discord OAuth token exchange failed: HTTP ${response.status} — ${detail}`)
    return c.json({ error: 'token exchange failed', status: response.status, detail }, 502)
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

// "Manage Enrollment" (CWL_ROSTER_PLANNING_PLAN.md, 2026-08-10) — four routes, same
// verify-identity-then-proxy pattern as /cwl/clan-config above. /cwl/screen deliberately skips
// nothing security-relevant by not checking BRIDGE_URL/BRIDGE_SECRET differently — it goes
// through the exact same proxy path, just to a cheaper bridge endpoint.

api.get('/cwl/screen', async (c) => {
  const guildId = c.req.query('guild_id')
  if (!guildId) return c.json({ error: 'missing guild_id' }, 400)

  const discordUserId = await verifiedDiscordUserId(c)
  if (!discordUserId) return c.json({ error: 'unauthorized' }, 401)

  if (!c.env.BRIDGE_URL || !c.env.BRIDGE_SECRET) return bridgeNotConfigured(c)

  const upstream = await fetch(
    `${c.env.BRIDGE_URL}/api/cwl/screen?guild_id=${encodeURIComponent(guildId)}&discord_user_id=${encodeURIComponent(discordUserId)}`,
    { headers: { 'X-Bridge-Secret': c.env.BRIDGE_SECRET } },
  )
  return c.json(await upstream.json(), upstream.status as 200 | 400 | 403 | 500)
})

api.get('/cwl/enrollment', async (c) => {
  const guildId = c.req.query('guild_id')
  if (!guildId) return c.json({ error: 'missing guild_id' }, 400)

  const discordUserId = await verifiedDiscordUserId(c)
  if (!discordUserId) return c.json({ error: 'unauthorized' }, 401)

  if (!c.env.BRIDGE_URL || !c.env.BRIDGE_SECRET) return bridgeNotConfigured(c)

  const upstream = await fetch(
    `${c.env.BRIDGE_URL}/api/cwl/enrollment?guild_id=${encodeURIComponent(guildId)}&discord_user_id=${encodeURIComponent(discordUserId)}`,
    { headers: { 'X-Bridge-Secret': c.env.BRIDGE_SECRET } },
  )
  return c.json(await upstream.json(), upstream.status as 200 | 400 | 403 | 500)
})

// Event-driven long-poll (2026-08-17, CWL_PROD_PERFORMANCE_FIX_PLAN.md P1 Step 8) replacing the
// client's old fixed 12s setInterval poll of /cwl/enrollment above. Same verify-identity-then-
// proxy shape as every other route here — the upstream fetch simply takes up to ~25s (the
// bridge's own hold duration) instead of returning immediately; Worker wall-clock time spent
// waiting on origin I/O like this is not billed CPU time, so this is free-plan safe.
api.get('/cwl/enrollment/wait', async (c) => {
  const guildId = c.req.query('guild_id')
  const knownVersion = c.req.query('known_version')
  if (!guildId || knownVersion === undefined) {
    return c.json({ error: 'missing guild_id or known_version' }, 400)
  }

  const discordUserId = await verifiedDiscordUserId(c)
  if (!discordUserId) return c.json({ error: 'unauthorized' }, 401)

  if (!c.env.BRIDGE_URL || !c.env.BRIDGE_SECRET) return bridgeNotConfigured(c)

  const upstream = await fetch(
    `${c.env.BRIDGE_URL}/api/cwl/enrollment/wait?guild_id=${encodeURIComponent(guildId)}` +
      `&discord_user_id=${encodeURIComponent(discordUserId)}&known_version=${encodeURIComponent(knownVersion)}`,
    { headers: { 'X-Bridge-Secret': c.env.BRIDGE_SECRET } },
  )
  return c.json(await upstream.json(), upstream.status as 200 | 400 | 403 | 500)
})

// Hover pop-up progressive fetch (2026-08-16) — resolves clan_tag -> name for tags the board's
// initial payload didn't already carry a name for. Same verify-identity-then-proxy shape as
// /cwl/enrollment above (which this is a companion to); `tags` is a plain comma-joined list, not
// re-validated here — the bridge itself is tolerant of unknown/empty entries.
api.get('/cwl/clan-names', async (c) => {
  const guildId = c.req.query('guild_id')
  if (!guildId) return c.json({ error: 'missing guild_id' }, 400)
  const tags = c.req.query('tags') ?? ''

  const discordUserId = await verifiedDiscordUserId(c)
  if (!discordUserId) return c.json({ error: 'unauthorized' }, 401)

  if (!c.env.BRIDGE_URL || !c.env.BRIDGE_SECRET) return bridgeNotConfigured(c)

  const upstream = await fetch(
    `${c.env.BRIDGE_URL}/api/cwl/clan-names?guild_id=${encodeURIComponent(guildId)}&discord_user_id=${encodeURIComponent(discordUserId)}&tags=${encodeURIComponent(tags)}`,
    { headers: { 'X-Bridge-Secret': c.env.BRIDGE_SECRET } },
  )
  return c.json(await upstream.json(), upstream.status as 200 | 400 | 403 | 500)
})

// Second half of the hover pop-up's progressive fetch (2026-08-16) — missed CWL attacks +
// attack/defense ratio for a single player, over their last 3 CWL seasons. Same
// verify-identity-then-proxy shape as /cwl/clan-names above.
api.get('/cwl/player-stats', async (c) => {
  const guildId = c.req.query('guild_id')
  const playerTag = c.req.query('player_tag')
  if (!guildId || !playerTag) return c.json({ error: 'missing guild_id or player_tag' }, 400)

  const discordUserId = await verifiedDiscordUserId(c)
  if (!discordUserId) return c.json({ error: 'unauthorized' }, 401)

  if (!c.env.BRIDGE_URL || !c.env.BRIDGE_SECRET) return bridgeNotConfigured(c)

  const upstream = await fetch(
    `${c.env.BRIDGE_URL}/api/cwl/player-stats?guild_id=${encodeURIComponent(guildId)}&discord_user_id=${encodeURIComponent(discordUserId)}&player_tag=${encodeURIComponent(playerTag)}`,
    { headers: { 'X-Bridge-Secret': c.env.BRIDGE_SECRET } },
  )
  return c.json(await upstream.json(), upstream.status as 200 | 400 | 403 | 500 | 503)
})

api.post('/cwl/enrollment/assign', async (c) => {
  const discordUserId = await verifiedDiscordUserId(c)
  if (!discordUserId) return c.json({ error: 'unauthorized' }, 401)

  if (!c.env.BRIDGE_URL || !c.env.BRIDGE_SECRET) return bridgeNotConfigured(c)

  let body: Record<string, unknown>
  try {
    body = await c.req.json()
  } catch {
    return c.json({ error: 'invalid JSON body' }, 400)
  }

  const upstream = await fetch(`${c.env.BRIDGE_URL}/api/cwl/enrollment/assign`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Bridge-Secret': c.env.BRIDGE_SECRET },
    body: JSON.stringify({ ...body, discord_user_id: discordUserId }),
  })
  return c.json(await upstream.json(), upstream.status as 200 | 400 | 403 | 409 | 503)
})

// Activity-closed notification (2026-08-16, live-testing feedback: iPad's Hub message launch
// buttons stayed visibly disabled after closing the Activity) — same verify-then-proxy shape as
// every other route here, fired from main.ts's closeActivity() on every close, not just a save.
api.post('/cwl/activity-closed', async (c) => {
  const discordUserId = await verifiedDiscordUserId(c)
  if (!discordUserId) return c.json({ error: 'unauthorized' }, 401)

  if (!c.env.BRIDGE_URL || !c.env.BRIDGE_SECRET) return bridgeNotConfigured(c)

  let body: Record<string, unknown>
  try {
    body = await c.req.json()
  } catch {
    return c.json({ error: 'invalid JSON body' }, 400)
  }

  const upstream = await fetch(`${c.env.BRIDGE_URL}/api/cwl/activity-closed`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Bridge-Secret': c.env.BRIDGE_SECRET },
    body: JSON.stringify({ ...body, discord_user_id: discordUserId }),
  })
  return c.json(await upstream.json(), upstream.status as 200 | 400 | 403 | 503)
})

// Guests (2026-08-15) — invite a guest clan/player into Configure Participating Clans' Guests
// search. Same verify-identity-then-proxy pattern as everything above; guest CLANS never call
// the guest endpoint (they're just added into the same `clans` array POST /cwl/clan-config
// already saves) — only the search and the individual-guest-player add need new routes here.

api.get('/cwl/guest-search', async (c) => {
  const guildId = c.req.query('guild_id')
  if (!guildId) return c.json({ error: 'missing guild_id' }, 400)
  const q = c.req.query('q') ?? ''

  const discordUserId = await verifiedDiscordUserId(c)
  if (!discordUserId) return c.json({ error: 'unauthorized' }, 401)

  if (!c.env.BRIDGE_URL || !c.env.BRIDGE_SECRET) return bridgeNotConfigured(c)

  const upstream = await fetch(
    `${c.env.BRIDGE_URL}/api/cwl/guest-search?guild_id=${encodeURIComponent(guildId)}&discord_user_id=${encodeURIComponent(discordUserId)}&q=${encodeURIComponent(q)}`,
    { headers: { 'X-Bridge-Secret': c.env.BRIDGE_SECRET } },
  )
  return c.json(await upstream.json(), upstream.status as 200 | 400 | 403 | 500)
})

api.post('/cwl/enrollment/guest', async (c) => {
  const discordUserId = await verifiedDiscordUserId(c)
  if (!discordUserId) return c.json({ error: 'unauthorized' }, 401)

  if (!c.env.BRIDGE_URL || !c.env.BRIDGE_SECRET) return bridgeNotConfigured(c)

  let body: Record<string, unknown>
  try {
    body = await c.req.json()
  } catch {
    return c.json({ error: 'invalid JSON body' }, 400)
  }

  const upstream = await fetch(`${c.env.BRIDGE_URL}/api/cwl/enrollment/guest`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Bridge-Secret': c.env.BRIDGE_SECRET },
    body: JSON.stringify({ ...body, discord_user_id: discordUserId }),
  })
  return c.json(await upstream.json(), upstream.status as 200 | 400 | 403 | 409 | 503)
})

// Guest clan/player removal (2026-08-18, CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md rules f/g)
// — same verify-identity-then-proxy pattern as every other route here.
api.post('/cwl/enrollment/guest-clan/remove', async (c) => {
  const discordUserId = await verifiedDiscordUserId(c)
  if (!discordUserId) return c.json({ error: 'unauthorized' }, 401)

  if (!c.env.BRIDGE_URL || !c.env.BRIDGE_SECRET) return bridgeNotConfigured(c)

  let body: Record<string, unknown>
  try {
    body = await c.req.json()
  } catch {
    return c.json({ error: 'invalid JSON body' }, 400)
  }

  const upstream = await fetch(`${c.env.BRIDGE_URL}/api/cwl/enrollment/guest-clan/remove`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Bridge-Secret': c.env.BRIDGE_SECRET },
    body: JSON.stringify({ ...body, discord_user_id: discordUserId }),
  })
  return c.json(await upstream.json(), upstream.status as 200 | 400 | 403 | 404 | 409 | 503)
})

api.get('/cwl/enrollment/guest-players', async (c) => {
  const guildId = c.req.query('guild_id')
  if (!guildId) return c.json({ error: 'missing guild_id' }, 400)

  const discordUserId = await verifiedDiscordUserId(c)
  if (!discordUserId) return c.json({ error: 'unauthorized' }, 401)

  if (!c.env.BRIDGE_URL || !c.env.BRIDGE_SECRET) return bridgeNotConfigured(c)

  const upstream = await fetch(
    `${c.env.BRIDGE_URL}/api/cwl/enrollment/guest-players?guild_id=${encodeURIComponent(guildId)}&discord_user_id=${encodeURIComponent(discordUserId)}`,
    { headers: { 'X-Bridge-Secret': c.env.BRIDGE_SECRET } },
  )
  return c.json(await upstream.json(), upstream.status as 200 | 400 | 403 | 503)
})

api.post('/cwl/enrollment/guest-players/remove', async (c) => {
  const discordUserId = await verifiedDiscordUserId(c)
  if (!discordUserId) return c.json({ error: 'unauthorized' }, 401)

  if (!c.env.BRIDGE_URL || !c.env.BRIDGE_SECRET) return bridgeNotConfigured(c)

  let body: Record<string, unknown>
  try {
    body = await c.req.json()
  } catch {
    return c.json({ error: 'invalid JSON body' }, 400)
  }

  const upstream = await fetch(`${c.env.BRIDGE_URL}/api/cwl/enrollment/guest-players/remove`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Bridge-Secret': c.env.BRIDGE_SECRET },
    body: JSON.stringify({ ...body, discord_user_id: discordUserId }),
  })
  return c.json(await upstream.json(), upstream.status as 200 | 400 | 403 | 409 | 503)
})

// Owner-only eviction (2026-08-15) — removes target_guild_id's participation in a shared clan.
// Same proxy pattern; the real ownership check happens bridge-side (evict_guild_from_shared_clan).
api.post('/cwl/shared-clan/evict', async (c) => {
  const discordUserId = await verifiedDiscordUserId(c)
  if (!discordUserId) return c.json({ error: 'unauthorized' }, 401)

  if (!c.env.BRIDGE_URL || !c.env.BRIDGE_SECRET) return bridgeNotConfigured(c)

  let body: Record<string, unknown>
  try {
    body = await c.req.json()
  } catch {
    return c.json({ error: 'invalid JSON body' }, 400)
  }

  const upstream = await fetch(`${c.env.BRIDGE_URL}/api/cwl/shared-clan/evict`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Bridge-Secret': c.env.BRIDGE_SECRET },
    body: JSON.stringify({ ...body, discord_user_id: discordUserId }),
  })
  return c.json(await upstream.json(), upstream.status as 200 | 400 | 403 | 503)
})

const app = new Hono<{ Bindings: Bindings }>()
app.route('/api', api)
app.route('/', api)

// If this still 404s, the response body tells us the exact path Cloudflare actually received —
// the real diagnostic we were missing before this change.
app.notFound((c) => c.json({ error: 'not found', path: c.req.path, method: c.req.method }, 404))

export default app
