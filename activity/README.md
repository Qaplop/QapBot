# CWL Clan-Config Activity

Phase A skeleton of the Discord Activity described in `CWL_CLAN_CONFIG_ACTIVITY_PLAN.md` (repo
root). Two independent Node/TypeScript projects, deployed to Cloudflare:

- **`server/`** — Cloudflare Worker (Hono). OAuth2 code→token exchange, plus a thin proxy to
  QapBot's own bridge API (not wired up until Phase B).
- **`client/`** — Cloudflare Pages. Plain TypeScript, no framework (see the plan doc for why).
  Currently just proves the OAuth round-trip: shows "Hello, guild {id}" once launched from
  inside Discord.

Both currently install/typecheck/build clean with **zero `npm audit` findings** (verified
2026-08-09 against wrangler 4.x, vite 6.x, `@discord/embedded-app-sdk` 2.5.x — the versions
originally scaffolded pulled in known-vulnerable transitive deps via wrangler 3.x; bumped past
them rather than carrying that forward).

## One-time account setup (you, not me)

1. **Sign up for Cloudflare** at https://dash.cloudflare.com/sign-up — free, no credit card
   required for the tiers this plan uses (Workers: 100k requests/day free; Pages: unlimited
   requests, 500 builds/month free).
2. **Install and authenticate Wrangler** (already added as a dev dependency in both projects,
   so no global install needed):
   ```
   cd activity/server && npx wrangler login
   ```
   This opens a browser window to authorize the CLI against your new Cloudflare account —
   only needs to happen once per machine.
3. **Discord Developer Portal**, for *each* application (DEV and PROD both, per the plan):
   - This step needs a deployed URL first (chicken-and-egg) — see "First deploy" below, then
     come back here.
   - Developer Portal → your application → **Activities → Settings** → enable.
   - **URL Mapping**: root (`/`) → your `*.pages.dev` URL, `/api` → your `*.workers.dev` URL.
   - Note the **OAuth2 Client ID** (not secret, goes in `wrangler.toml`) and generate a
     **Client Secret** (real secret — goes in Wrangler secrets, never a file in this repo).

## First deploy (per environment: `dev` first, `prod` later)

```
cd activity/server
npm run deploy:dev          # first deploy — gives you the *.workers.dev URL
wrangler secret put CLIENT_SECRET --env dev   # paste the Client Secret from the Developer Portal

cd ../client
# copy .env.example to .env.local, fill in VITE_CLIENT_ID with the DEV application's Client ID
npm run deploy:dev          # gives you the *.pages.dev URL
```

Now go back to the Developer Portal step above and fill in the URL Mapping with the two URLs
you just got. Repeat everything in this section with `:prod` once DEV is validated (Phase D).

## Local development

Discord cannot embed `localhost` URLs directly — for local iteration once Phase A's spike is
validated, use a `cloudflared` tunnel to your Vite dev server and point a *second* URL Mapping
(or a temporary swap of the existing one) at the tunnel URL. Not needed to get the initial
skeleton deployed and working — `npm run dev` in each project is enough to catch build errors
before deploying.

## What's NOT built yet

- The bridge API on QapBot's side (`qapbot/web_bridge.py`) — Phase B.
- The real clan-config table UI — Phase C.
- The "Open Clan Config (Web)" button in the bot + the `LAUNCH_ACTIVITY` callback wiring —
  Phase A's own remaining risk item (see plan doc): whether Discord accepts a `LAUNCH_ACTIVITY`
  response from an arbitrary interaction, or requires a dedicated Entry Point command, needs to
  be spiked against the DEV application once it's deployed and URL-mapped.
