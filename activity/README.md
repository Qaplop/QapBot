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
you just got.

**Client Client-ID note**: `deploy:dev`/`deploy:prod` (in `client/package.json`) build with
`vite --mode dev`/`--mode prod`, which load `.env.dev.local`/`.env.prod.local` respectively
(copy `.env.example` to each, gitignored) — so both environments' `VITE_CLIENT_ID` can coexist
without manually swapping one shared file before every deploy. Plain `npm run dev`/`npm run
build` (no mode flag) still fall back to `.env.local`.

## Local development

Discord cannot embed `localhost` URLs directly — for local iteration once Phase A's spike is
validated, use a `cloudflared` tunnel to your Vite dev server and point a *second* URL Mapping
(or a temporary swap of the existing one) at the tunnel URL. Not needed to get the initial
skeleton deployed and working — `npm run dev` in each project is enough to catch build errors
before deploying.

## PROD rollout — NAS bridge & named tunnel (Phase D)

Unlike DEV (bot runs on the dev Windows machine, `cloudflared`'s free **quick tunnel** is fine
since a dev restarts it by hand anyway), PROD runs unattended on the NAS
(`PROD_BOT_ROOT`/`PROD_SSD_UNC` in `.env`). A quick tunnel mints a brand-new random
`*.trycloudflare.com` URL on every restart with no notification — after any NAS reboot the
bridge silently goes dark until someone notices the Activity is broken and manually re-runs
`wrangler secret put BRIDGE_URL`. A **named tunnel** gets a permanent hostname that survives
restarts, at the cost of needing one domain in the same Cloudflare account.

1. **Get a domain** (skip if you already own one anywhere — you can add it to this Cloudflare
   account as a zone for free and just use a subdomain, no need to buy a second one):
   Cloudflare dashboard → **Domain Registration → Register a Domain** → search → buy (sold at
   cost, no markup; nameservers auto-configured, no manual DNS delegation step). Any cheap TLD
   is fine — nothing here is user-facing, only the Worker's `BRIDGE_URL` config ever uses it.
2. **Generate the bridge secret** (skip if `.env`'s `WEB_BRIDGE_SECRET` — PROD, no `_DEV` suffix
   — is already filled in): a random token, e.g. `openssl rand -base64 32`. Put the same value
   in two places and nowhere else:
   - This repo's `.env` → `WEB_BRIDGE_SECRET=` (already gitignored).
   - The Worker: `cd activity/server && npx wrangler secret put BRIDGE_SECRET --env prod`
     (paste at the prompt).
3. **On the NAS**, install `cloudflared` if not already present (Synology: SSH in, check
   `uname -m` for the CPU architecture, download the matching binary from
   `github.com/cloudflare/cloudflared/releases/latest`, `chmod +x`).
4. **Authenticate and create the named tunnel** (one-time, run on the NAS):
   ```
   cloudflared tunnel login                              # opens a browser auth against your Cloudflare account
   cloudflared tunnel create qapbot-prod-bridge           # writes a credentials JSON, prints a Tunnel ID
   cloudflared tunnel route dns qapbot-prod-bridge bridge.<your-domain>   # auto-creates the DNS record
   ```
5. **Config file** (e.g. `~/.cloudflared/config.yml` on the NAS):
   ```yaml
   tunnel: qapbot-prod-bridge
   credentials-file: /root/.cloudflared/<tunnel-id>.json
   ingress:
     - hostname: bridge.<your-domain>
       service: http://localhost:8789   # WEB_BRIDGE_PORT from .env
     - service: http_status:404
   ```
6. **Test it**: `cloudflared tunnel run qapbot-prod-bridge` (with the PROD bot already running
   and its bridge listening on `127.0.0.1:8789`), confirm `https://bridge.<your-domain>/api/health`
   responds, then stop it and set up auto-start so it survives NAS reboots — on Synology DSM,
   **Control Panel → Task Scheduler → Create → Triggered Task → Boot-up**, running
   `cloudflared tunnel run qapbot-prod-bridge` (or `cloudflared service install` if your DSM
   version supports installing it as a proper background service — check first, it's cleaner
   than a boot-trigger script when available).
7. **Wire the Worker to it**: `cd activity/server && npx wrangler secret put BRIDGE_URL --env prod`
   → `https://bridge.<your-domain>`.
8. **Smoke test**: launch the Activity from a real PROD guild, confirm the clan-config table
   loads real data and Save round-trips through the whole chain.

## Status

Phases A-C (skeleton, bridge API, real table UI) are shipped and verified live in DEV — see
`CWL_CLAN_CONFIG_ACTIVITY_PLAN.md` for the phase-by-phase history. This file's "PROD rollout"
section above is Phase D.
