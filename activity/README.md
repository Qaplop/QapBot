# CWL Clan-Config Activity

Phase A skeleton of the Discord Activity described in
`../qapbot/docs/CWL_CLAN_CONFIG_ACTIVITY_PLAN.md`. Two independent Node/TypeScript projects,
deployed to Cloudflare:

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

## This is two separately-deployed pieces — deploying one doesn't deploy the other

`client/` (Cloudflare Pages) and `server/` (Cloudflare Worker) ship independently via their own
`deploy:dev`/`deploy:prod` npm scripts. A request to "deploy the Activity" (or just "deploy the
frontend") means checking both, not whichever half the wording literally names — they're one
feature to the person using it.

**2026-08-23 incident**: the server's `/cwl/enrollment/status` route (tracker #0014) was committed
2026-08-22, but the PROD Worker's last deploy was 2026-08-19 — three days stale. A "deploy the
frontend to prod" request only redeployed `client/`, leaving the Worker still missing that route.
The right-click "Set enrollment status" admin action then failed live with `Action failed: not
found` (Hono's own 404 catch-all — the route genuinely didn't exist in the running Worker), with no
build error anywhere to catch it, since `client/`'s own `npm run build`/`typecheck` know nothing
about `server/`'s deployed state. **Before calling a deploy done, check `cd activity/server &&
npx wrangler deployments list --env <dev|prod>` for the last deploy time and compare against
`git log -1 -- activity/server/src` — if server source changed more recently than its last deploy,
redeploy it too, even if only the client was asked for.**

## Editing `client/src/*` does not ship anything by itself

The live Discord Activity iframe loads the built Cloudflare Pages bundle in `client/dist/`, not
the TypeScript source — `npm run dev`/`tsc --noEmit` only validate the source, they don't touch
what's deployed. After any change to `client/src/*.ts` (or its `index.html`/CSS) that needs to be
testable live, redeploy in the same step:

```
cd activity/client && npm run deploy:dev
```

(2026-08-19 incident: a same-session fix to `clanConfigTable.ts` was implemented and
typecheck-verified correctly, but the bundle was never rebuilt/redeployed. The user tested it live
hours later, saw the old broken behavior, and reasonably reported it as a fresh regression — it
was actually just an undeployed fix. `ls -la dist/assets` vs. `src/*.ts` mtimes is the fast way to
confirm/rule this out if a "regression" is reported for something that was supposedly just fixed.)

PROD (`npm run deploy:prod`) still needs explicit user confirmation before every deploy — this
redeploy-on-edit habit applies to DEV only.

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
   responds, then stop it and set up auto-start — on Synology DSM,
   **Control Panel → Task Scheduler → Create → Triggered Task → User-defined script**, trigger
   **Boot-up**, user **root** (must be root — a non-root user can't read `/root/.cloudflared/`'s
   credentials at all, confirmed the hard way). Run command:
   ```sh
   sleep 15
   HOME=/root /usr/local/bin/cloudflared tunnel --config /root/.cloudflared/config.yml run qapbot-prod-bridge >> /var/log/cloudflared-prod-bridge.log 2>&1
   ```
   The `sleep 15` and explicit `HOME`/`--config` aren't optional — DSM's Boot-up trigger can fire
   before the network is fully up, and `cloudflared`'s default `~/.cloudflared/` lookup isn't
   reliable that early either (see "Survive a DSM upgrade" below for a second, related timing
   gotcha this same design has to account for). Redirecting output to a file was necessary
   because DSM's own Task Scheduler "View Result" panel shows nothing useful unless you've
   separately configured an output-log folder in Task Scheduler's settings — the script logging
   itself sidesteps that entirely and is what actually revealed the underlying bug the first
   time this failed silently on a real reboot.
7. **Survive a DSM upgrade** (same reasoning as this project's own `Entware sichern` boot task —
   see `backlog.txt`): anything outside `/volume1` (the actual data volume) is *not* guaranteed
   to survive a DSM upgrade — that's a documented incident on this NAS already (a prior DSM
   upgrade wiped Entware's `/opt` install). `/usr/local/bin/cloudflared` and `/root/.cloudflared/`
   (cert, tunnel credentials, `config.yml`) are both in that same at-risk category. Move both
   onto `/volume1` and symlink back, mirroring Entware's exact `/opt` → `/volume1/@entware`
   pattern:
   ```sh
   mkdir -p /volume1/@cloudflared/bin
   mv /usr/local/bin/cloudflared /volume1/@cloudflared/bin/cloudflared
   ln -sf /volume1/@cloudflared/bin/cloudflared /usr/local/bin/cloudflared

   mv /root/.cloudflared /volume1/@cloudflared/dotcloudflared
   ln -sf /volume1/@cloudflared/dotcloudflared /root/.cloudflared
   ```
   The credentials directory is the more important half to protect — losing the binary just
   needs a re-download, but losing `/root/.cloudflared/` means redoing
   `tunnel login`/`create`/`route dns` and updating the Worker's `BRIDGE_URL` secret again. Then
   make the boot script self-healing (same `[ -L path ] || ln -sf ...` idiom as `Entware
   sichern`'s own script) so a future DSM upgrade that wipes the symlinks gets them back
   automatically on the next boot, before the tunnel start line from step 6:
   ```sh
   [ -L /usr/local/bin/cloudflared ] || ln -sf /volume1/@cloudflared/bin/cloudflared /usr/local/bin/cloudflared
   [ -L /root/.cloudflared ] || ln -sf /volume1/@cloudflared/dotcloudflared /root/.cloudflared
   sleep 15
   HOME=/root /usr/local/bin/cloudflared tunnel --config /root/.cloudflared/config.yml run qapbot-prod-bridge >> /var/log/cloudflared-prod-bridge.log 2>&1
   ```
   **Resolved by a real reboot test (2026-08-10)**: this migration added a new boot-time
   dependency the original script didn't have — `/volume1` must actually be mounted before these
   symlinks resolve to anything real, and DSM's Boot-up trigger firing before that happens was a
   documented possibility, not just a network-readiness question. The plain `sleep 15` above had
   only ever been validated against the network-timing bug (via a warm Task Scheduler
   "Ausführen" run, not a genuine cold boot); a full NAS restart with this exact script confirmed
   it covers the volume-mount timing too — both the bridge and the tunnel came up cleanly with no
   manual intervention, verified externally via `/api/health` immediately after boot. The
   wait-for-the-actual-dependency version below is therefore unnecessary in practice on this NAS,
   but is kept here as a more defensive fallback in case a slower/busier boot ever pushes past the
   `sleep 15` margin:
   ```sh
   for i in $(seq 1 30); do
     [ -e /volume1/@cloudflared/bin/cloudflared ] && break
     sleep 1
   done
   [ -L /usr/local/bin/cloudflared ] || ln -sf /volume1/@cloudflared/bin/cloudflared /usr/local/bin/cloudflared
   [ -L /root/.cloudflared ] || ln -sf /volume1/@cloudflared/dotcloudflared /root/.cloudflared
   sleep 15
   HOME=/root /usr/local/bin/cloudflared tunnel --config /root/.cloudflared/config.yml run qapbot-prod-bridge >> /var/log/cloudflared-prod-bridge.log 2>&1
   ```
   Entware's own boot task has the identical theoretical gap (no wait for `/volume1` before its
   `ln -sf`) but has never hit it in practice — its script only *restores a pointer*, it doesn't
   try to *use* anything through it in the same script, so a momentarily-dangling symlink at
   boot is harmless there (whatever eventually calls `zstd` runs much later, by which point
   `/volume1` is certainly mounted). `cloudflared`'s script is different because it tries to
   start a service through the symlink immediately, in the same script — that's what makes the
   timing actually matter here.
8. **Supervise it** (2026-08-14 incident + 2026-08-15 fix — see
   `../qapbot/docs/CWL_CLAN_CONFIG_ACTIVITY_PLAN.md`'s Phase D section for the full narrative):
   step 6's raw `cloudflared tunnel ... run ...` line dies silently whenever `cloudflared`'s own
   autoupdate replaces its binary and exits (on by default, ~daily) — DSM's Boot-up trigger
   doesn't notice or restart it, so a routine autoupdate meant 26h of downtime the one time it
   happened. Fixed by never running `cloudflared` directly from Task Scheduler again — instead
   run a small supervisor script that restarts it on any exit:
   ```sh
   sudo vim /volume1/@cloudflared/cloudflared-supervisor.sh
   ```
   ```sh
   #!/bin/bash
   # Supervises the qapbot-prod-bridge cloudflared tunnel: restarts it on ANY exit
   # (crash, OOM, or a routine "cloudflared has been updated" self-shutdown -- autoupdate
   # is deliberately left ON) so a silent exit no longer means ~26h of downtime like
   # 2026-08-14. Boot-up-triggered by DSM Task Scheduler; this loop is what actually keeps
   # it alive between exits, not just across reboots.
   #
   # To stop deliberately: kill THIS script's PID (written to $PIDFILE below), NOT
   # cloudflared's PID directly -- killing cloudflared alone just gets it restarted.

   LOG=/var/log/cloudflared-prod-bridge.log
   PIDFILE=/volume1/@cloudflared/supervisor.pid
   STOP=0
   CF_PID=

   echo $$ > "$PIDFILE"
   trap 'STOP=1; echo "$(date -Iseconds) INF supervisor got stop signal, killing cloudflared" >> "$LOG"; [ -n "$CF_PID" ] && kill "$CF_PID" 2>/dev/null' TERM INT

   for i in $(seq 1 30); do
     [ -e /volume1/@cloudflared/bin/cloudflared ] && break
     sleep 1
   done
   [ -L /usr/local/bin/cloudflared ] || ln -sf /volume1/@cloudflared/bin/cloudflared /usr/local/bin/cloudflared
   [ -L /root/.cloudflared ] || ln -sf /volume1/@cloudflared/dotcloudflared /root/.cloudflared
   sleep 15

   while [ "$STOP" -eq 0 ]; do
     echo "$(date -Iseconds) INF supervisor starting cloudflared" >> "$LOG"
     HOME=/root /usr/local/bin/cloudflared tunnel --config /root/.cloudflared/config.yml run qapbot-prod-bridge >> "$LOG" 2>&1 &
     CF_PID=$!
     wait "$CF_PID"
     if [ "$STOP" -eq 0 ]; then
       echo "$(date -Iseconds) WRN cloudflared exited unexpectedly -- restarting in 5s" >> "$LOG"
       sleep 5
     fi
   done

   rm -f "$PIDFILE"
   echo "$(date -Iseconds) INF supervisor stopped intentionally" >> "$LOG"
   ```
   ```sh
   sudo chmod +x /volume1/@cloudflared/cloudflared-supervisor.sh
   ```
   Kept in `/volume1/@cloudflared/` — same DSM-upgrade-protected location as the binary/
   credentials from step 7, not `/root`. Then edit the DSM Task Scheduler task from step 6
   (**Control Panel → Task Scheduler**, same task, still triggered on **Boot-up**) to run the
   supervisor instead of `cloudflared` directly:
   ```sh
   bash /volume1/@cloudflared/cloudflared-supervisor.sh
   ```
   **To stop the tunnel intentionally** (maintenance etc.): `kill $(cat
   /volume1/@cloudflared/supervisor.pid)` — killing `cloudflared`'s own PID directly does *not*
   count as intentional and just gets it restarted, by design (the supervisor only traps a
   signal sent to *itself*). **Verified live 2026-08-15**: `sudo kill -9 $(pgrep -f "cloudflared
   tunnel --config")` (simulating a hard crash, not just autoupdate's own graceful exit) →
   supervisor logged `WRN cloudflared exited unexpectedly -- restarting in 5s` and a fresh tunnel
   reconnected within seconds; a full NAS reboot afterward also came back up clean through the
   same Boot-up trigger, now pointing at the supervisor.
9. **Wire the Worker to it**: `cd activity/server && npx wrangler secret put BRIDGE_URL --env prod`
   → `https://bridge.<your-domain>`.
10. **Smoke test**: launch the Activity from a real PROD guild, confirm the clan-config table
    loads real data and Save round-trips through the whole chain.

## Status

All phases (A-E, skeleton through PROD rollout and the workflow redesign) are shipped and
verified live in both DEV and PROD — see `../qapbot/docs/CWL_CLAN_CONFIG_ACTIVITY_PLAN.md` for
the full phase-by-phase history. This file's "PROD rollout" section above is Phase D.
