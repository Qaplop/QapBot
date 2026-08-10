# CWL Clan-Config Activity — Implementation Plan

**Status**: Implemented (Phases A-E, including the same-day Phase E revision, all shipped and
verified live in DEV and PROD; small follow-up fixes continuing 2026-08-10 — see items 11-12)
**Session**: 2026-08-09 (2026-08-10 follow-ups)

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

### Phase B — Bridge API + tunnel ✅ shipped and verified live 2026-08-09 (DEV environment)
`qapbot/web_bridge.py`: an `aiohttp.web` app with `GET`/`POST /api/cwl/clan-config` (plus `/api/health`), started from `_setup_hook()` and stopped from `async_cleanup()` — no-ops entirely unless both port/secret config fields are set (new `BotConfig` fields, same "0/empty = disabled" convention as the rest of the config). Bound to `127.0.0.1` only, per the plan's security section — `cloudflared`, not this process, is what makes it externally reachable. 7 tests in `tests/discord/test_web_bridge.py` (secret gate, admin re-verification including the configured-super-admin bypass, GET payload shape, POST persistence + Hub-message refresh trigger) — 1618 total tests pass.

**Config correction**: `WEB_BRIDGE_PORT`/`WEB_BRIDGE_SECRET` shipped mode-agnostic at first, but this project runs DEV and PROD off one shared `.env` file — without a suffix they'd collide between the two. Fixed to `WEB_BRIDGE_PORT_DEV`/`WEB_BRIDGE_SECRET_DEV` (DEV mode) vs `WEB_BRIDGE_PORT`/`WEB_BRIDGE_SECRET` (PROD), mirroring `load_config()`'s existing `DISCORD_TOKEN`/`DISCORD_TOKEN_DEV` selection exactly.

Extracted `refresh_cwl_management_hub_message(guild_id, mode)` as a free function in `qapbot/ui_cwl_roster.py` (was inline in `CwlManagementHubView.refresh_cwl_view()`, which only ever used `interaction.guild.id` — nothing else about the interaction) — this is what lets the bridge trigger the same anchored-message refresh as the Discord-side screens after a web-side save, with no `discord.Interaction` available in an HTTP handler. `refresh_cwl_view()` now just delegates to it. Purely an extraction — the existing (unmodified) tests for it still pass unchanged, confirming no behavior change.

**A real security gap found and closed before this shipped, not after**: the plan's auth model always intended the bridge to independently re-verify admin status (not trust the Worker), but the Worker itself needed fixing too — its first-draft `/cwl/clan-config` proxy would have forwarded whatever `discord_user_id` the *client* claimed, which is trivially spoofable (anyone could claim to be a known admin's Discord ID, e.g. one visible in that guild's own message history). Fixed: the Worker now requires an `Authorization: Bearer <access_token>` header on every `/cwl/clan-config` request and independently calls Discord's own `GET /users/@me` with it to get the *real* user id before forwarding anything to the bridge — a client-supplied `discord_user_id` in the request body is now explicitly overwritten with the verified value, not merely ignored.

**Verified live end-to-end in the DEV guild**: `cloudflared tunnel --url http://127.0.0.1:8788` (Cloudflare's free "quick tunnel," no named-tunnel/account setup needed for DEV) running alongside the DEV bot process, `BRIDGE_URL`/`BRIDGE_SECRET` set as Worker secrets, Activity relaunched from Discord — the full chain (Discord iframe → Cloudflare Pages → Worker → tunnel → QapBot bridge → real `cwl_events`/`cwl_event_clans` data → back through the same chain) returned the actual DEV guild's live CWL clan-config JSON (both participating clans, correct tiers, roster sizes, start times). Two real snags on the way, neither a code bug: `cloudflared` installed via `winget` wasn't on PATH in the already-open terminal (needed a fresh terminal, or the full `C:\Program Files (x86)\cloudflared\cloudflared.exe` path); the Activity's iframe kept showing the pre-fetch-code build after a redeploy (Discord/browser-side caching, not a failed deploy — confirmed by curling the live Pages bundle directly and finding the new code already there) — a full Discord client restart cleared it.

Named/production `cloudflared` tunnel setup (vs. the quick tunnel used for DEV) is a Phase D decision, not resolved here.

### Phase C — Real table UI ✅ shipped and verified live 2026-08-09 (DEV environment)
`activity/client/src/clanConfigTable.ts`: the actual reason this Activity exists — a real `<table>` with a checkbox/clan-name/tier(read-only)/roster-size-`<select>`/start-time-`<input type="datetime-local">` row per clan, editing a working copy that only reaches the bridge on "Save" (same working-copy-then-apply pattern as the Discord-side `CwlEventSetupView`). `main.ts` now loads this instead of Phase B's raw-JSON smoke test. Datetime handling is a plain string transform, never a timezone conversion: the bridge's `"YYYY-MM-DDTHH:MMZ"` and `datetime-local`'s `"YYYY-MM-DDTHH:MM"` differ only by the trailing `Z`, and both are always UTC by convention (matching the Discord-side modal) — never interpreted against the browser's local timezone.

Bot side: a 5th button, **"Open Clan Config (Web)"**, added to `add_cwl_management_components()` (`qapbot/ui_cwl_roster.py`) — exactly fills row 1's 5-button cap. Its callback resolves Phase A's flagged open question for real: whether a plain component-interaction (not the auto-created Entry Point command) can be answered with a `LAUNCH_ACTIVITY` (type 12) response. Implemented via a raw `interaction.client.http.request()` call (discord.py has no high-level wrapper), with a graceful ephemeral fallback message if Discord ever rejects it — so a "no" answer here degrades to a clear hint instead of a silently dead button. 2 new tests in `tests/discord/test_ui_cwl_roster.py` (correct callback-type-12 payload; fallback path when the raw call raises) — 1620 total tests pass.

**Verified live end-to-end in the DEV guild**: DEV bot restarted, "Open Clan Config (Web)" clicked from the Season Management screen — the Activity opened directly from the button (resolving the flagged `LAUNCH_ACTIVITY`-from-component-interaction question: **yes, it works**, not just from the auto-created Entry Point command). The table rendered real clan data, edits (roster size, start time via the native date/time picker) worked, and Save persisted correctly — confirmed against the same `cwl_events`/`cwl_event_clans` data the Discord-side screens read.

**Refinements from continued live testing, all same day — two of them needed a second pass after live testing showed the first fix didn't actually work:**
1. Start-time picker limited to `:00`/`:15`/`:30`/`:45`. First attempt used `datetime-local`'s `step="900"` (seconds) attribute — **didn't work**: live-tested, minute picker still showed all 60 values. Confirmed why: `step` on `datetime-local` only affects *validation*, never which values the native picker UI actually offers (the HTML spec ties it to validity-checking, not presentation; Chromium's scrollable minute list ignores it entirely). Actual fix: stopped using the native minute picker altogether — a plain `<input type="date">` (unaffected by this issue) plus a `<select>` pre-populated with only the 96 valid quarter-hour values (`00:00`, `00:15`, ... `23:45`), combined into the same `"YYYY-MM-DDTHH:MMZ"` value on change.
2. Added a Cancel button (re-renders from the untouched original payload — no server round-trip, since nothing is sent until Save).
3. Closing the Activity from Save/Cancel — **confirmed working after a few false starts, worth recording accurately since it took three passes to get right**: first conclusion ("no close/exit API exists at all") was wrong, based on only checking the SDK's `commands/` folder. Corrected to `discordSdk.close(RPCCloseCodes.CLOSE_NORMAL, reason)` — a real, exported top-level method whose actual runtime implementation (checked directly in the installed package's compiled source, not just its `.d.ts`) posts a genuine `CLOSE`-opcode `postMessage` to Discord's client frame, the same mechanism every other SDK command uses. First live test of *that* appeared to show no effect — but the only place calling it at the time was Save's confirmation screen, and Cancel wasn't wired to it at all (so "nothing happens" was partly just Cancel being a no-op, not necessarily `close()` failing). Wired both buttons to it and retested: **it works** — confirmed live via both Save and Cancel. Final UX, per project owner preference: no intermediate confirmation screen at all — Save persists then closes immediately, Cancel discards and closes immediately, closing itself is the confirmation.
4. **A real data-loss bug, not a platform limit**: deactivating a clan deleted its `cwl_event_clans` row entirely, silently discarding its `roster_size`/`cwl_start_at` — reactivating it (even within the same session) reset it to defaults. Root cause: "row exists" was the only signal for "participating" at all, so deactivating had no way to represent "off, but remember the settings." Added an explicit `participating` column (idempotent migration) — deactivating now sets `participating=0` and *keeps* the row instead of deleting it. Fixed at every layer that touches this data: `set_cwl_event_clans_sync()`/`get_previous_cwl_event_clans_sync()` (the latter still correctly excludes non-participating rows from next-season carry-over — that's a different, intentional filter), the web bridge's GET/POST, and the Discord-side `CwlEventSetupView` (toggling now flips `participating` in place instead of `del()`-ing the working-copy entry, which had the identical bug on the native side too, just never reported). Every *display* of "Participating Clans" now explicitly filters to `participating=1`, since the raw row set can contain deactivated-but-remembered clans. 6 new/updated tests across the DB layer, the bridge, and the Discord-side toggle — 1623 total tests pass.

### Phase D — PROD rollout ✅ shipped and verified live 2026-08-09
Repeat the Developer Portal setup for the PROD application, deploy the `prod` Wrangler environment, add the PROD bridge (tunnel + `.env` secret on whatever host runs PROD), smoke-test in a real guild.

**Done so far**: PROD Developer Portal setup (Activities enabled, URL Mapping, OAuth2 redirect —
`activity/README.md`'s deterministic-URL trick meant the mapping could be entered *before*
deploying, since Cloudflare Worker/Pages URLs are `<name>.<account-subdomain>.workers.dev` /
`<project-name>.pages.dev`, both fully known from `wrangler.toml`/`package.json` ahead of time).
PROD Worker + Pages both deployed (`cwl-clan-config-server-prod`, `cwl-clan-config-prod`).
`CLIENT_SECRET`/`BRIDGE_SECRET` set as Worker secrets for `--env prod`. Client build config
fixed to use real Vite modes (`vite build --mode dev|prod` loading `.env.dev.local`/
`.env.prod.local`) instead of one shared `.env.local` that had to be manually swapped before
every deploy — a footgun once two people/sessions might deploy either environment.

**A real PROD-startup crash found and fixed, not just a config gap**: restarting the PROD bot
after enabling Activities on the PROD Discord application (a Phase D step) made `setup_hook`'s
global command sync fail with HTTP 400 / error code 50240 ("You cannot remove this app's Entry
Point command in a bulk update operation"), an uncaught exception that exited the whole process
— PROD was down until fixed. Root cause: enabling Activities auto-creates a global
`PRIMARY_ENTRY_POINT` command that `discord.py` 2.7.1 has no model of at all (its
`AppCommandType` enum doesn't even define the value), so `tree.sync(guild=None)`'s bulk
overwrite always omits it — Discord rejects that outright rather than silently deleting it. The
same restriction silently broke `_clear_global_commands_after_ready`'s DEV-app cleanup too
(already wrapped in try/except there, so non-fatal, but not doing its job — the DEV app has had
an Entry Point command since Phase A). Fixed with `bulk_sync_global_commands()`
(`qapbot/discord_health.py`): fetches the app's current global commands via a raw HTTP call,
filters for an existing Entry Point command, and always splices it back into the bulk-upsert
payload — used at both call sites in `QapBot.py` in place of `tree.sync(guild=None)`. This is a
permanent fix, not a one-off: every future global sync on either app (DEV or PROD, both now
Activities-enabled) would otherwise hit this. 3 new tests in `tests/discord/test_discord_health.py`
— 1636 total tests pass.

**Named tunnel + auto-start, including a real boot-time bug**: bought `qapbot.uk` via
Cloudflare Registrar (DEV keeps its quick tunnel since a human restarts it by hand; PROD needed
a stable hostname since nobody's watching for a changed URL after an unattended reboot).
`cloudflared tunnel create`/`route dns` set up `bridge-prod.qapbot.uk` → `qapbot-prod-bridge` →
`http://localhost:8789`, `BRIDGE_URL` set as a Worker secret, confirmed reachable end-to-end.
The DSM Task Scheduler boot-up trigger for it **failed silently on the first real reboot test**
(`ps aux` showed no `cloudflared` process; the Activity surfaced a bare 500 since the Worker's
proxy fetch to a dead tunnel isn't handled as gracefully as the deliberate 501
`bridgeNotConfigured()` case) — DSM's boot-up-triggered scripts run before `HOME` is reliably
set to `/root` and before the network is necessarily up, so `cloudflared`'s default
`~/.cloudflared/` lookup and its initial connection attempt can both fail with no retry. Fixed
by making the task's script explicit rather than relying on defaults: `sleep 15` (network
readiness margin), `HOME=/root` and `--config /root/.cloudflared/config.yml` (removes any
ambiguity about which credentials/config to use), redirecting output to
`/var/log/cloudflared-prod-bridge.log` (DSM's own Task Scheduler "View Result" panel showed
nothing useful without a configured output-log folder — the script logging itself sidesteps
that entirely). Re-verified via **Action → Run** showing a clean tunnel startup and an external
`/api/health` check succeeding. **The follow-up cold-reboot double-check happened as part of the
persistent-storage hardening below** (2026-08-10) — confirmed the timing fix survives a genuine
reboot, not just a manual re-run of the same task.

**A second, related hardening pass, same day**: this NAS already has one documented incident
where a DSM upgrade wiped everything outside `/volume1` (Entware's `/opt` install — see
`instructions.txt`; the `Entware sichern` boot task exists specifically because of it).
`/usr/local/bin/cloudflared` and `/root/.cloudflared/` (credentials, `config.yml`) are both in
that same at-risk category and had no equivalent protection. Fixed by mirroring Entware's own
pattern exactly: moved both onto `/volume1/@cloudflared/` and symlinked back
(`/usr/local/bin/cloudflared` → `/volume1/@cloudflared/bin/cloudflared`, `/root/.cloudflared` →
`/volume1/@cloudflared/dotcloudflared`), then added the same `[ -L path ] || ln -sf ...`
self-healing idiom to the boot script so a future DSM upgrade that wipes the symlinks gets them
restored automatically on the next boot rather than requiring another manual fix. Verified live:
killed the running tunnel process, confirmed both symlinks resolved correctly, and a fresh
`cloudflared tunnel run` through the new paths connected cleanly. Full details and the exact
script in `activity/README.md`'s "Survive a DSM upgrade" step.

This migration introduced a boot-time dependency the original script didn't have — `/volume1`
must be mounted before the symlinks resolve to anything real, which is a documented DSM
possibility distinct from the network-readiness race the `sleep 15` was originally written for.
**Resolved by a real reboot test (2026-08-10)**: a full NAS restart brought both the bridge and
the tunnel up cleanly with the existing `sleep 15`, no manual intervention, verified externally
via `/api/health` right after boot — the same margin covers both timing risks in practice on
this NAS. `activity/README.md` still documents a more defensive wait-for-the-actual-dependency
version as a fallback, kept for a slower/busier future boot, but it isn't currently needed.

Full chain confirmed live in a real PROD guild: Activity launches, clan-config table loads real
data, Save round-trips through Worker → tunnel → bridge → `cwl_events`/`cwl_event_clans`.

### Phase E — Workflow redesign (web Activity becomes the sole clan-config entry point)
Five-part follow-up requested once Phase C was verified live, replacing the native/web dual-path design with the web Activity as the only way to configure CWL clans:

1. ✅ **Retired the native "Configure Participating Clans" flow entirely.** `add_cwl_management_components()`'s `configure_button` now opens the web Activity directly (the same `LAUNCH_ACTIVITY` callback the old separate "Open Clan Config (Web)" button used), reusing that button's original label/position; the standalone web button was removed since its job is now folded into `configure_button`. Deleted the entire now-dead `CwlEventSetupView`/`CwlStartTimeModal` classes and their only-used-there helpers (`_default_cwl_start_time()`, `_parse_cwl_start_time()`), the now-unused `TrackedView` import, the 7 tests in `tests/discord/test_ui_cwl_roster.py` that exercised them, and the now-orphaned `cwl.setup.*`/`cwl.management.button_open_web` i18n keys in both `en.json`/`de.json` (kept `cwl.setup.button_cancel` — still reused by `CwlDeleteSeasonConfirmView`'s Cancel button). 1616 tests pass after cleanup.
   - **Correction to this doc's own "Explicitly out of scope" section**: that section previously said *"Removing or deprecating the existing native `CwlEventSetupView` flow — both stay available side by side"* — this Phase E item does exactly that removal, superseding that line.
2. ✅ **Season-aware defaults.** `qapbot/web_bridge.py`'s `_build_clan_config_payload()` now defaults a clan with no saved row for the selected season to `roster_size=15`, `cwl_start_at=f"{season}-01T08:00Z"` (the 1st of that season's month at 08:00 UTC — the game's static schedule) instead of `roster_size=15`/`cwl_start_at=null`. Applies uniformly whenever a season has no own data yet — right after a season is deleted, or the first time a brand-new season is opened — with no special-casing needed for either trigger.
3. ⚠️ **Superseded same day, see the revision below** — originally shipped as a season-selection `<select>` *inside the web Activity itself*, driven by a `?season=` query param and an `available_seasons` list on the bridge.
4. ⚠️ **Superseded same day, see the revision below** — originally shipped as an in-Activity carry-over Yes/No banner, driven by `carry_over_available`/`carry_over_season`/`previous_clans` on the bridge.
5. ✅ **Timezone-aware date/time (per-clan start-time editing in the Activity).** `clanConfigTable.ts` converts the bridge's UTC `"YYYY-MM-DDTHH:MMZ"` to/from the browser's local timezone at exactly two functions (`utcStringToLocalParts`/`localPartsToUtcString`) — everywhere else in the file works purely in local terms, so the "invisible to the user" requirement is satisfied by construction rather than by discipline. The header shows the resolved IANA timezone name (`Intl.DateTimeFormat().resolvedOptions().timeZone`) so the user knows what "local" means without guessing. The "Start Time (UTC)" table header became "Start Time (local)". Unaffected by the revision below — this item is about the Activity's own date/time *inputs*, not the Discord embed's *display* (see item 9).

**Revision, same day — items 3/4 moved out of the Activity into Discord.** After using the shipped version, the project owner asked for season selection and "take over previous season" to move to the **Discord-side CWL Management message** instead of living in the web Activity, plus two more items: clans sorted by CWL tier (both surfaces) and the Discord embed showing start times in each viewer's own timezone. Implemented as:

6. ✅ **Clans sorted by CWL tier, highest first.** New `CWL_LEAGUE_ORDER` tuple in `qapbot/constants.py` (Bronze III → Legend, kept separate from `chart_clans_per_league.py`'s own copy since that module pulls in matplotlib) and a `cwl_league_rank(tier)` helper in `qapbot/QBdiscocmdshelper_cwl.py` (unknown/never-synced tiers rank last). Applied to both `web_bridge.py`'s `_build_clan_config_payload()` (the Activity's table) and `format_clan_management_cwl_management()` (the Discord embed) — both now sort the exact same way, name as the tiebreaker.
7. ✅ **Season select moved to the Discord-side CWL Management message.** New `guild_config.cwl_selected_season TEXT` column (idempotent migration, same pattern as `cwl_retention_months`) holds the persisted selection. `resolve_selected_cwl_season(guild_id)` in `QBdiscocmdshelper_cwl.py` is the single resolution path both the Discord embed and the bridge now share: persisted selection if set, else `get_current_cwl_event_sync()`'s season, else the calendar default — so both surfaces always agree on "which season is this" without either needing its own heuristic. `add_cwl_management_components()` (`qapbot/ui_cwl_roster.py`) adds a `discord.ui.Select` listing every season with a saved event, only once at least one exists; picking one persists `cwl_selected_season` via `CACHE.persist_server_config()` and refreshes the shared content layer in place. The bridge dropped its own `?season=`/`available_seasons` entirely — the Activity now has no season picker of its own, it just always reflects whichever season the Discord select last chose.
   - **Corrected same day**: initially placed the select on row 3, below the action buttons (row 1). Moved to row 1 (above the buttons, now on row 3) per direct feedback, and stopped marking any option `default=True` so the select's `placeholder` — "Select CWL season:" — always renders as a static caption instead of the currently-selected season's value (which is redundant anyway: the embed's own "Season **{season}** — {status}" header already shows it). Discord's classic message components have no separate label element to attach text to a select; a placeholder-only select is the idiomatic way bots achieve one.
8. ✅ **"Add New Season" button — the exclusive home for season creation and the carry-over prompt.** Last button in row 3 (Discord's per-row cap of 5), alongside configure/start/manage/delete. Computes the target season via the existing `resolve_current_cwl_season()` (next calendar month); if a `cwl_events` row already exists for it, sends an ephemeral "already exists — pick it from the dropdown" error rather than silently reusing it. Otherwise checks `get_previous_cwl_event_clans_sync()`: if a previous season has participating clans, opens a new ephemeral `CwlCarryOverPromptView` (Yes/No, mirrors `CwlDeleteSeasonConfirmView`'s shape) asking to carry over or start fresh; if there's nothing to offer, creates the season directly with plain defaults. Either path ends by persisting `cwl_selected_season` to the new season and refreshing the shared content layer. **"Configure Participating Clans" itself now carries zero season-resolution logic** — it only opens the web Activity for whichever season is already selected, and is `disabled` until that season actually has an event (mirroring the bridge's own POST refusal below), per the project owner's explicit instruction that season creation and carry-over belong exclusively to "Add New Season". The bridge's `POST /api/cwl/clan-config` matches this: it never calls `create_cwl_event_sync()` anymore, returning `409` with a "use \"Add New Season\" in Discord first" message if the resolved season has no event yet, instead of silently creating one (which is what the Activity's own carry-over-era POST used to do).
9. ✅ **Discord embed shows start times in each viewer's own timezone — no guild-wide timezone setting needed.** The project owner's ask offered a fallback ("if automatic isn't possible, add a server timezone setting near the language config") — it *is* possible, and better: Discord's native `<t:unix:style>` timestamp markup renders in each viewer's own Discord-client locale/timezone automatically, with no bot-side config at all. New `cwl_start_at_discord_timestamp(cwl_start_at, style="f")` helper in `QBdiscocmdshelper_cwl.py` converts the stored UTC string to this markup; `format_clan_management_cwl_management()` uses it for the "Start" field instead of the raw UTC string. A guild-wide setting would have been strictly worse here — every admin would see the *same* (guild-configured) time rather than their own.
10. ✅ **Start-time picker in the Activity clamped to the season's official start.** The project owner initially asked whether the picker should *allow* an earlier date; clarified they meant the opposite — the app should *prohibit* one, since CWL never starts before the 1st of the season's month at 08:00 UTC. `clanConfigTable.ts`'s date input now sets `min` to that floor (converted to local, the same conversion Phase E.5 already does) as a native first line of defense, and `updateStartValue()` independently re-validates and clamps on every change (covers typed-in dates bypassing `min`, and the boundary day's earlier time-of-day options, which `min` alone can't catch since it's date-only).
11. ✅ **Start-time picker also clamped to a 48h ceiling (2026-08-10).** Same mechanism as item 10, mirrored for the other bound: `seasonEndUtc = seasonStartUtc + 48h`, `dateInput.max` set to that (converted to local) as the native first line of defense, and `updateStartValue()` clamps to `seasonEndUtc`/`seasonEndLocal` if a candidate value exceeds it — a clan switching in later than 48h after the season's official start would miss too much of the war league to be sensible.
12. ✅ **Table row height tightened (2026-08-10).** `index.html`'s `th`/`td` vertical padding (8px→4px), select/date-input padding (6px→3px) and font-size (0.9rem→0.85rem), and the checkbox size (18px→16px) were all reduced so large clan families (12+ rows) fit on one screen without scrolling.
13. ✅ **Discord embed's "Participating Clans" list rebuilt as a monospaced code-block table (2026-08-10).** Project owner's explicit "clean table like design" ask, with column headers **Clan / League / Roster / CWL Start**. Clan tag dropped (name only, this view only — still shown elsewhere), tier shortened by dropping the word "League" (`"Champion League II"` → `"Champion II"`), roster shows the bare figure, and the start time is a fixed `"YY-MM-DD HH:MM"` (new `cwl_start_at_compact()` in `QBdiscocmdshelper_cwl.py`) instead of item 9's `<t:unix:f>` markup. **Corrects item 9's "no guild-wide timezone setting needed" conclusion — but only for this one table, not as a general reversal**: Discord does not parse `<t:...>` markup inside a fenced code block at all (renders as literal text), so a column-aligned table and native per-viewer timestamps are mutually exclusive — not a design preference, a hard Discord limitation, confirmed with the project owner via `AskUserQuestion` before implementing. Every other timestamp display in the codebase still prefers native per-viewer markup; this table is the sole deliberate exception.
14. ✅ **New per-guild "Select Timezone" setting, next to "Select Language" (2026-08-10)** — exactly the fallback the project owner originally proposed back when item 9 was first discussed, now actually needed because of item 13's code-block constraint. `guild_config.timezone_name TEXT NOT NULL DEFAULT 'UTC'` (idempotent migration, same pattern as `cwl_selected_season`). `cwl_start_at_compact()` converts via the stdlib `zoneinfo` module and shifts before formatting; the table's "CWL Start" header shows the configured zone name (`"CWL Start (Europe/Berlin)"`, or `"CWL Start (UTC)"` at the default) rather than a live offset/abbreviation — deliberately, since a multi-row table can straddle a DST transition and each row's own `HH:MM` is already individually correct regardless of what the header says.
    - **First cut (same day) used a free-text UTC-offset modal, then two follow-up corrections replaced it entirely**: (1) the project owner asked for "a real time zone picker" with "European daylight saving time shifts" maintained — a fixed offset can't do DST, so this needed an IANA zone (`zoneinfo.ZoneInfo`, DST-aware by construction) rather than arithmetic on a stored offset; (2) the project owner then asked for the picker to actually *be* a picker, not a typed field, and to be shown as a line in the Basic Config embed. Final shape: `TimezoneConfigurationView` in `ui_clan_management.py` (a `discord.ui.Select`, structurally identical to the existing `LanguageConfigurationView` — auto-applies on pick, no modal) offering `COMMON_TIMEZONES` (25 curated IANA zones, one global spread, exactly filling Discord's Select option cap; a RadioGroup's 10-option cap or the full ~400-zone IANA list, which would need pagination, were both rejected). `_format_clan_management_config()` (`QBdiscocmdshelper.py`) gained a `"🕒 Server Timezone: {name}"` field, placed between the language field and the registration-message field, matching the project owner's requested position. **Windows-specific gotcha caught before shipping**: `zoneinfo.ZoneInfo` raised `ZoneInfoNotFoundError` on the dev machine — Windows has no OS-level IANA tz database (unlike Linux/the NAS), so the first-party `tzdata` PyPI package is a hard runtime dependency here, not just on Windows dev machines but wherever the bot might run without system tzdata; added to `requirements.txt` and installed into `venv`. The earlier offset-based columns/helpers (`parse_utc_offset`/`format_utc_offset` in `QBhelperfunctions.py`, `TimezoneConfigurationModal`) were deleted outright rather than deprecated — never shipped to PROD, so a clean swap instead of a migration.
15. ✅ **Dashed header/data separator row added to the table (2026-08-10)**, matching the `/leaderboard` command's own text-table style — a row of `-` characters matching each column's width, inserted between the header row and the first data row.
16. ✅ **Header shows a short abbreviation, not the full IANA zone name (2026-08-10).** `"CWL Start (Europe/Berlin)"` reliably wrapped onto a second line inside Discord's code-block width, breaking the table's column alignment — replaced with `"CWL Start (CEST)"` via new `timezone_abbreviation(timezone_name, cwl_season)` in `QBdiscocmdshelper_cwl.py`, resolved against the season's own official start (1st of that month, 08:00 UTC) rather than "now" the embed happens to render at, so the abbreviation reflects the season being displayed. Per-clan start times are all within item 11's 48h window of that reference point, so in practice every row shares the header's abbreviation; on the rare table that straddles a DST transition, only the header label is approximate — each row's own `HH:MM` (`cwl_start_at_compact()`) is always individually correct regardless of what the header says.

Bridge-side: replaced the season-selection/carry-over test suite with tests for the simplified, persisted-selection-driven design (`test_clan_config_get_honors_persisted_selected_season`, `test_clan_config_get_sorts_clans_by_tier_highest_first`, `test_clan_config_post_targets_the_selected_season`, `test_clan_config_post_rejects_when_no_season_exists_yet`). Discord-side: new tests for `add_cwl_management_components()`'s season select and "Add New Season" button (creation, already-exists rejection, carry-over-offered, `CwlCarryOverPromptView` Yes/No), plus `cwl_league_rank`/`cwl_start_at_discord_timestamp`/`resolve_selected_cwl_season` unit tests and a `format_clan_management_cwl_management()` sorting/timestamp test. 1633 total tests pass. Frontend reverted to a single fetch/render with no season param and no in-Activity carry-over UI; `npm run typecheck`/`npm run build` both clean.

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

## Decisions made along the way (originally listed as open items)

- **Table visual styling**: left as a neutral admin-tool look — never revisited, no issue raised. Still open if you want to match Discord's dark theme colors, but non-blocking.
- **Bridge shared-secret**: resolved as a single static token (`WEB_BRIDGE_SECRET`/`_DEV`, `wrangler secret put BRIDGE_SECRET`) — shipped this way in Phase B and carried through Phase D unchanged, no short-lived signed-token scheme was needed at this scale.
- **DEV/PROD tunnel sharing**: resolved as fully separate tunnels — DEV keeps Cloudflare's free quick tunnel (a human restarts it by hand), PROD got a named tunnel (`bridge-prod.qapbot.uk`, Phase D) since it needs a stable hostname that survives unattended reboots.

---

## Explicitly out of scope for this plan

- The 50-player roster/sign-up/assignment screens (Phases 2-4 of `CWL_ROSTER_PLANNING_PLAN.md`) — a future Activity extension, not this one.
- Any change to QapBot's core `db_manager.py`/`CACHE` architecture — the bridge API is purely a new *read/write client* of the existing layer, per Cardinal Rule 2 (CACHE-only data access).

(Note: this section originally also listed "removing or deprecating the native `CwlEventSetupView` flow" as out of scope — Phase E superseded that and removed it entirely; see Phase E.1 above.)
