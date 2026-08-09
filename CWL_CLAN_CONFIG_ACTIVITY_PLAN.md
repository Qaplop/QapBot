# CWL Clan-Config Activity — Implementation Plan

## Context

While building `CWL_ROSTER_PLANNING_PLAN.md` Phase 1's "Configure Participating Clans" screen, the project owner asked for a genuine table UI — a checkbox column, clan tag, league tier, a roster-size dropdown, and a date/time picker, one row per member clan, for at least 12 clans at once. Discord's native component API cannot render this (confirmed against the actual installed `discord.py==2.7.1` source, not guesses):

- **Modals** cap at 5 top-level items total, full stop. Even the best-fit new component (`CheckboxGroup`, discord.py 2.7) maxes at 10 options *in one field* with no room left for per-row dropdowns/date pickers, and Discord has no native date/time picker component at all.
- **Regular messages/Views** have no checkboxes and no table/grid layout primitive — just buttons (5/row, 25 total) and select menus (each eats a whole row).

So this plan replaces that one screen with a **Discord Activity**: a real web app (HTML/CSS/JS) that Discord renders in an `<iframe>` inside the client, launched from a button on the existing CWL Management Hub / `/clan management` screen. Everything else about CWL roster planning (data model, the rest of the `cwl_management`/`cwl_settings` screens, Phases 2-5) is unaffected and stays exactly as shipped.

**MVP scope, confirmed with the project owner:** only the participating-clans table (checkbox / tag / tier / roster size / start time, ~12-30 rows). The 50-*player* roster/sign-up/assignment screens from later phases are explicitly **out of scope** for this plan — that's a separate, bigger problem for a later round, not what triggered this.

Two prior research threads (Microsoft Enterprise Copilot chat log, and a separate research-agent report — both supplied by the project owner) independently converged on the same architecture: Cloudflare Pages (frontend) + Cloudflare Workers (backend, Hono, OAuth2 token exchange) + the official `@discord/embedded-app-sdk`. This plan reuses that conclusion and adapts it to QapBot's actual constraints (SQLite data living on the bot's own host, not in a hosted DB Cloudflare can reach directly).

---

## Decision: Cloudflare over Fly.io

**Cloudflare Pages + Workers**, not Fly.io. Reasoning:

- **No credit card required for the free tier.** Fly.io ended free-allowance signup for new orgs — it's usage-based billing with a card on file. Cloudflare's free tier (Workers: 100k requests/day; Pages: unlimited requests, 500 builds/month) needs no payment method at all, which matters given the explicit "for free" requirement.
- **No cold starts.** Workers run at the edge and are always warm — no 30-second wake-up penalty an admin would hit opening the config screen. Fly.io's free-tier-equivalent small VMs do sleep/scale to zero.
- **Best compatibility with Discord's own infrastructure.** Discord's Activity proxy (`discordsays.com`) is itself a Cloudflare Worker layer — both research threads flagged this as the reason Cloudflare has the smoothest integration path for Activities specifically (URL Mapping, CSP behavior, `/.proxy/*` routing are all Cloudflare-native concepts).
- **Matches what both independent research threads already recommended** — no point overriding a converged conclusion without a concrete reason to.

---

## Architecture

```
Discord Client (Desktop/Web/Mobile)
   |
   |  iframe, postMessage via @discord/embedded-app-sdk
   v
Cloudflare Pages  ──"clan-config.pages.dev"──  Frontend
   (static: HTML/CSS/TS, no framework — see "Frontend stack" below)
   |
   |  fetch('/api/...') — routed through Discord's proxy, arrives at:
   v
Cloudflare Worker  ──"clan-config.workers.dev"──  Backend (Hono)
   - OAuth2 code -> access_token exchange (holds CLIENT_SECRET)
   - Verifies caller is a real Discord user + reads guild_id from the SDK context
   - Proxies business calls to QapBot, attaching a shared bridge secret
   |
   |  HTTPS, shared-secret header
   v
Cloudflare Tunnel (cloudflared, free)  ──  runs alongside the QapBot process
   |
   v
QapBot bridge API (new: qapbot/web_bridge.py, aiohttp.web)
   - Runs IN-PROCESS with the bot (same asyncio loop, same CACHE/db_manager —
     no data duplication, no second source of truth)
   - Re-verifies admin status itself via the exact guild_permissions.administrator /
     _is_configured_admin() logic already in QBdiscocmdshelper.py (defense in depth —
     never trusts the Worker's claim alone)
   - Exposes exactly the 2 endpoints this MVP needs (see "Bridge API" below)
```

Three deployable things, one new local process is *not* one of them — the bridge API runs inside the existing QapBot event loop, not as a separate service to babysit.

---

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Frontend hosting | Cloudflare Pages | Free, static, matches Discord's own proxy infra |
| Frontend code | Plain TypeScript + native `<table>`, no framework | ~12-30 rows, no search/sort/filter/pagination requirement for this MVP — React + TanStack Table (what the research docs recommended) is sized for the *50-player* problem we explicitly deferred. Adding a framework now is unjustified weight; revisit if/when the player-roster Activity is built. |
| Table interactivity | Native `<input type="checkbox">`, `<select>`, `<input type="datetime-local">` | Zero libraries — this is exactly the win over Discord's native components: real HTML form controls, no 25-option/5-component ceilings. |
| Backend (Worker) | Cloudflare Workers + Hono | Free, edge-native, tiny — matches both research threads' converged recommendation. |
| OAuth | Discord OAuth2, scopes `identify guilds` | `guilds` scope returns per-guild `permissions` bitfield directly — the Worker can read the `ADMINISTRATOR` bit without any extra API call. |
| Bot↔Worker bridge | `cloudflared` tunnel (free) + a static shared-secret header | No port-forwarding, no new domain purchase, no change to whatever network setup dev/prod already have. |
| Bridge API (bot side) | `aiohttp.web` (already a discord.py dependency — zero new package) | Runs in the bot's existing event loop; reads `CACHE`/`db_manager` directly, no duplicate data store. |
| Local Activity testing | `cloudflared` tunnel to `localhost` + `wrangler dev` | Discord cannot embed `localhost` URLs directly. |

---

## Auth & permission model

1. Activity launches inside a guild context — the Embedded App SDK exposes `discordSdk.guildId` directly, no need to derive it from anything else.
2. Frontend does the standard OAuth2 `authorize` → Worker exchanges `code` for an `access_token` (Client Secret never touches the frontend) → frontend calls `discordSdk.commands.authenticate()`.
3. Worker calls `GET /users/@me/guilds` with that token, finds the entry matching `discordSdk.guildId`, and reads its `permissions` field. If the `ADMINISTRATOR` bit isn't set, the Worker still forwards the request to the bridge (see next point) rather than rejecting outright — the bot-side check is authoritative.
4. Every bridge API call carries `{discord_user_id, guild_id}` (verified by the Worker's own OAuth check) plus the shared bridge secret. **QapBot's bridge API re-derives admin status itself**, reusing `check_admin_permissions()`'s exact logic (guild `administrator` permission **or** the configured single super-admin override) — this is what correctly covers the super-admin edge case without duplicating that logic in JavaScript, and means the Worker is never the sole authority on "is this person allowed."
5. Shared bridge secret: a random token, stored as a Cloudflare Worker secret (`wrangler secret put BRIDGE_SECRET`) and in QapBot's `.env` (gitignored, never committed — per the project's existing secrets-handling rule). Rotate by regenerating and updating both sides.

---

## Bridge API (QapBot side) — `qapbot/web_bridge.py`

New file, new `aiohttp.web.Application`, started from `QapBot.py`'s `_setup_hook()` alongside the bot (`web.AppRunner` + `web.TCPSite` bound to `127.0.0.1:<port>`, never bound to `0.0.0.0` — only `cloudflared` should ever reach it). Exactly two endpoints for MVP scope:

- **`GET /api/cwl/clan-config?guild_id=...`** → re-verify admin (see above) → returns: every guild member clan (tag, name, live `CACHE.get_clan_war_league()` tier), cross-referenced with the current CWL event's `cwl_event_clans` rows (participating flag, `roster_size`, `cwl_start_at`) — i.e. exactly the data `CwlEventSetupView` already assembles, just as JSON instead of Discord components.
- **`POST /api/cwl/clan-config`** → body: `{guild_id, discord_user_id, clans: [{clan_tag, participating, roster_size, cwl_start_at}]}` → re-verify admin → calls the *same* `create_cwl_event_sync()` + `set_cwl_event_clans_sync()` pair `CwlEventSetupView._on_apply()`/`_persist_detail_edit()` already call → triggers the same `refresh_cwl_view()`-style update of the anchored CWL Management Hub message afterward, so the Discord-side screen reflects web-side edits immediately.

No new persistence layer, no new tables — this is a second UI in front of the exact same `db_manager.py` functions already shipped in Phase 1. `_parse_cwl_start_time()`'s format (`YYYY-MM-DD HH:MM` UTC) is what the frontend's `datetime-local` input serializes to, so no new parsing logic either.

---

## Launch mechanism (Discord side)

**Resolved in Phase A, no longer a risk:** enabling Activities in the Developer Portal auto-creates a global `PRIMARY_ENTRY_POINT` command (type 4, name `launch`, `handler: 2` = `DISCORD_LAUNCH_ACTIVITY`) — confirmed by querying `GET /applications/{id}/commands` directly. Discord handles this command's interaction entirely itself; **zero bot code was needed** to make `/launch` (or the voice-channel "Choose Activity" picker) open the Activity. This also means the original plan's raw `bot.http.request(..., callback_type=12)` approach was solving a problem that, for the *default* entry point, doesn't need solving.

What's still planned for Phase C: a **"Open Clan Config (Web)"** button next to "Configure Participating Clans" in `add_cwl_management_components()` (`qapbot/ui_cwl_roster.py`), for in-context launch from the screen an admin is already on, rather than requiring them to discover `/launch` separately. discord.py has no high-level wrapper for responding to a component interaction with a `LAUNCH_ACTIVITY` callback (type 12), so that button's callback will still need a raw REST call through `QBcore.bot.http` — this part is **not yet verified**, only the auto-created entry point command is. Worth a quick spike at the start of Phase C before assuming it works identically.

---

## Discord Developer Portal setup (both applications)

Per the "set up both from day one" decision, this is done twice — once for the DEV application (`DISCORD_TOKEN_DEV`), once for PROD (`DISCORD_TOKEN`). Recommended: **one Cloudflare project with `wrangler.toml` environments** (`[env.dev]` / `[env.prod]`), not two separate Cloudflare projects — same codebase, separate secrets/URL mappings/deploy targets. Flag if you'd rather keep them fully separate.

For each application:
1. Enable **Activities → Settings**.
2. Set **URL Mapping**: root → `<project>-<env>.pages.dev`, `/api` → `<project>-<env>.workers.dev`.
3. Note the **OAuth2 Client ID** and generate a **Client Secret** (goes into that environment's Worker secrets, never the repo).
4. Add `identify guilds` to the OAuth2 scopes used by the Activity's `authorize()` call.

---

## Phased implementation

### Phase A — Skeleton + the LAUNCH_ACTIVITY spike ✅ shipped 2026-08-09 (DEV environment)
Bare Cloudflare Pages + Worker deploy, OAuth round-trip working end-to-end, launched for real from inside Discord (voice channel → "Choose Activity" → Qaps-CoC-Bot[DEV]) and confirmed showing `Hello, guild 1145641080621109312 — OAuth round-trip OK.` — the actual DEV test guild's ID, proving `discordSdk.guildId` resolves correctly through the whole chain.

Three real bugs found and fixed getting there, all now reflected in the scaffold itself:
1. **`redirect_uri` is required** in the server-side token exchange (`POST https://discord.com/api/oauth2/token`), even though the SDK's own `authorize()` call has no such parameter and nothing ever navigates to it — standard OAuth2 `authorization_code` grant behavior that the original reference code (from both research threads) had simply omitted. Fixed: added a `REDIRECT_URI` binding (any URL registered under the application's OAuth2 → Redirects; using the Pages root URL) and included it in the token-exchange request body.
2. **Discord's Proxy Path Mapping prefix behavior is undocumented** (whether a `/api` prefix mapping strips itself before forwarding to the target, or is preserved) — hit as a 404 on `/api/token`. Rather than guess, the Worker's routes are now defined once and mounted at both `/api/*` and unprefixed `/*`, so it answers correctly either way; a `notFound` handler that echoes back the exact received path was added as a standing diagnostic for any future path-mapping surprises.
3. **The Entry Point Command question is resolved, not just worked around**: enabling Activities auto-creates a global `type: 4` (`PRIMARY_ENTRY_POINT`) command named `launch` with `handler: 2` (`DISCORD_LAUNCH_ACTIVITY`) — confirmed by querying `GET /applications/{id}/commands` directly against the live DEV application. Discord handles the whole interaction itself; no bot code was needed for this path. See "Launch mechanism" above for what's still open (the custom in-context button planned for Phase C).

Not yet done in Phase A: the PROD environment (deliberately deferred to Phase D per the phase plan — DEV validates the pipeline first).

### Phase B — Bridge API + tunnel
`qapbot/web_bridge.py` with the two endpoints, started from `_setup_hook()`. `cloudflared` tunnel running alongside the DEV bot process. Worker calls the bridge through the tunnel with the shared secret; verify a round-trip with a hardcoded test payload (no real UI yet).

### Phase C — Real table UI
Build the actual frontend table (checkbox/tag/tier/roster-size-select/start-time-picker per row), wired to the two bridge endpoints. Add the "Open Clan Config (Web)" button. End-to-end test in the DEV guild: open from Discord, edit, save, confirm the anchored CWL Management Hub message updates.

### Phase D — PROD rollout
Repeat the Developer Portal setup for the PROD application, deploy the `prod` Wrangler environment, add the PROD bridge (tunnel + `.env` secret on whatever host runs PROD), smoke-test in a real guild.

Each phase gets its own changelog entry and commit, per the project's established convention — same discipline as `CWL_ROSTER_PLANNING_PLAN.md`'s phases.

---

## Security considerations

- Client Secret and bridge shared-secret: Cloudflare Worker secrets (`wrangler secret put`) + QapBot `.env` only — never in `wrangler.toml`, never committed, never logged.
- Bridge API bound to `127.0.0.1` only; `cloudflared` is the only path in from outside.
- Every bridge request re-derives admin status server-side (bot-side) — the Worker's OAuth check is a UX gate (avoid showing the Activity to non-admins at all), not the security boundary.
- Every query/mutation is explicitly guild-scoped (`guild_id` from the verified session, never trusted from an arbitrary request body without cross-checking against the OAuth-verified guild).
- Discord's own Content-Security-Policy blocks any fetch to a domain not in URL Mapping — the frontend can only ever talk to the Worker, nothing else, by construction.
- Cloudflare's free-tier rate limits (100k Worker requests/day) are effectively unreachable for an admin-only tool with a handful of users — no additional rate limiting needed at this scale.

---

## Free-tier accounting

| Resource | Free tier | Expected usage |
|---|---|---|
| Cloudflare Pages | Unlimited requests, 500 builds/month | A handful of deploys per phase |
| Cloudflare Workers | 100,000 requests/day | Low tens per admin session, at most |
| `cloudflared` tunnel | Free, no request cap tied to the tunnel itself | N/A |
| Cloudflare account | No credit card required | — |

---

## Open items still to decide (non-blocking — can be settled during Phase A/B)

- Exact visual styling of the table (match Discord's dark theme colors vs. a neutral admin-tool look).
- Whether the bridge shared-secret is a single static token or a short-lived signed token per session (static token is simpler and adequate at this scale; flagging in case you'd rather not have a long-lived secret at all).
- Whether DEV and PROD bridges share one `cloudflared` tunnel config or get fully separate tunnels (separate tunnels is cleaner isolation, marginally more setup).

---

## Explicitly out of scope for this plan

- The 50-player roster/sign-up/assignment screens (Phases 2-4 of `CWL_ROSTER_PLANNING_PLAN.md`) — a future Activity extension, not this one.
- Removing or deprecating the existing native `CwlEventSetupView` flow — both stay available side by side.
- Any change to QapBot's core `db_manager.py`/`CACHE` architecture — the bridge API is purely a new *read/write client* of the existing layer, per Cardinal Rule 2 (CACHE-only data access).
