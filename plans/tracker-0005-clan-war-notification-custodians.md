# Feature #5: Per-clan war-notification custodians (@mention)

## Context

Tracker item #5 (open, filed 2026-08-21 by qaplop): the channel-wide war notification
(distinct from individual DM reminders) can go unnoticed. Request: let an admin configure,
per member clan in a guild, one or more Discord users ("custodians") who get `@mentioned` —
and therefore Discord-pushed — whenever that clan's war notification posts to the channel, so
someone is actively watching for people about to miss attacks.

Decisions already confirmed with the user:
- Cap at **5 custodians per clan**.
- Selectable from **any guild member** (not restricted to the clan's auto-role holders).
- **No separate enable/disable toggle** — an empty custodian list simply means no mention;
  configuring one or more custodians is itself "on".

## Data model

New table in `qapbot/db_manager.py`, added directly after the existing `guild_clan_roles`
table+index block (~line 2340), which is the closest existing precedent (per-clan config
scoped to a guild, FK'd to `guild_config`, not part of hot/history mirroring — Cardinal Rule 1
doesn't apply, it's pure config):

```sql
CREATE TABLE IF NOT EXISTS guild_clan_custodians (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    clan_tag TEXT NOT NULL,
    discord_user_id TEXT NOT NULL,
    FOREIGN KEY (guild_id) REFERENCES guild_config(guild_id) ON DELETE CASCADE,
    UNIQUE (guild_id, clan_tag, discord_user_id)
)
```
plus `CREATE INDEX IF NOT EXISTS idx_guild_clan_custodians_guild_id ON guild_clan_custodians(guild_id)`.

Unlike `guild_clan_roles` (one role per clan, `UNIQUE(guild_id, clan_tag)`), this needs multiple
rows per clan, hence the 3-column unique key — one row per custodian.

**DB methods** (`db_manager.py`, next to `save_guild_clan_role`/`delete_guild_clan_role` ~line
8780): a single `save_guild_clan_custodians(guild_id, clan_tag, user_ids: List[str])` that
replaces the whole set for that clan in one transaction (delete-then-insert, mirroring the
`guild_member_clans`/`guild_member_families` replace pattern already used inside
`save_guild_config` at db_manager.py:8723-8739):

```python
async def save_guild_clan_custodians(self, guild_id: str, clan_tag: str, user_ids: List[str]) -> None:
    await self._ensure_connection()
    async with self._write_lock:
        await self._conn.execute(
            "DELETE FROM guild_clan_custodians WHERE guild_id = ? AND clan_tag = ?",
            (guild_id, clan_tag)
        )
        if user_ids:
            await self._conn.executemany(
                "INSERT INTO guild_clan_custodians (guild_id, clan_tag, discord_user_id) VALUES (?, ?, ?)",
                [(guild_id, clan_tag, uid) for uid in user_ids]
            )
        await self._conn.commit()
```
An empty `user_ids` list naturally clears the clan's custodians — no separate delete method
needed (unlike `guild_clan_roles`, which only ever holds 0 or 1 row).

**Read path**: extend `get_guild_config()` (db_manager.py:8512) exactly like the existing
`clan_roles` block (lines 8561-8565), grouping rows by `clan_tag` into a `Dict[str, List[str]]`:

```python
custodians_cursor = await self._conn.execute(
    "SELECT clan_tag, discord_user_id FROM guild_clan_custodians WHERE guild_id = ?",
    (guild_id,)
)
clan_custodians: Dict[str, List[str]] = {}
for row in await custodians_cursor.fetchall():
    clan_custodians.setdefault(row["clan_tag"], []).append(row["discord_user_id"])
```
(named-column access per Rule 14, unlike the existing `clan_roles`/`families`/`clans` cursors in
this same function which use positional `f[0]`/`c[0]` — don't fix those in passing, out of scope).

Add `"clan_custodians": clan_custodians` to the dict returned by `get_guild_config()`. Because
`CACHE.load_server_config()` already calls `get_guild_config()` per guild at startup
(`cache_manager.py`), `CACHE.server_config[guild_id]["clan_custodians"]` is populated
automatically at boot with no separate loader — same as `clan_roles` today (Cardinal Rule 3:
CACHE-only reads at runtime).

## UI: `/clan management` → notifications mode → "Custodians" button

`qapbot/ui_clan_management.py`. The **"notifications" mode** of `ClanManagementView` already
keeps the clan-select dropdown (line 327's exclusion list omits it) and already has a
`_add_notification_management_buttons()` (line 615) with "Clan Settings"/"User Settings"
buttons that open ephemeral per-clan sub-views scoped to `self.clan_tag` — this is the natural
home, not a new top-level mode. Add a third button, "Custodians", row 4:

```python
custodians_button = discord.ui.Button(
    label=t('ui_components.clan_management.button_custodians', guild_id=guild_id),
    style=discord.ButtonStyle.primary,
    custom_id="clan_mgmt_custodians",
    row=4
)
custodians_button.callback = self._on_custodians
self.add_item(custodians_button)
```

`_on_custodians` mirrors `_on_clan_notification_settings` (line 1337-1360): admin-permission
check, `defer(thinking=True, ephemeral=True)`, then open a new `CustodianConfigurationView`,
`followup.send(header, view=..., ephemeral=True)`.

**New class `CustodianConfigurationView`** (add near `RoleConfigurationView`, ~line 3284, whose
shape it copies almost exactly — select(s) + clear + apply, state held on `self` and re-rendered
via `_rebuild_view()`/`edit_original_response()` on every change):

```python
class CustodianConfigurationView(discord.ui.View):
    def __init__(self, guild, clan_tag, current_custodian_ids, timeout=300):
        super().__init__(timeout=timeout)
        self.guild = guild
        self.clan_tag = clan_tag
        self.custodian_ids: List[str] = list(current_custodian_ids)
        self._rebuild_counter = 0
        self._add_user_select()
        self._add_clear_button()
        self._add_apply_button()
```

- `_add_user_select()`: `discord.ui.UserSelect(min_values=0, max_values=5,
  custom_id=f"custodian_select_{self._rebuild_counter}", row=0,
  default_values=[discord.Object(id=int(u)) for u in self.custodian_ids] or None)` — the
  dynamic `custom_id` suffix and `discord.Object(id=...)` default-values pattern are copied
  verbatim from `ClanManagementLinkAccountView._add_user_select()` (ui_clan_management.py:5658),
  which is the only existing `UserSelect` usage in the codebase and documents this exact
  stale-selection pitfall. `min_values=0` lets Discord's own picker clear the selection to
  empty; `max_values=5` enforces the agreed cap.
- Selection callback reads `interaction.data.get('values', [])` (same convention as
  `RoleConfigurationView._on_newbie_role_select`, line 3405-3413), stores it as
  `self.custodian_ids`, calls `self._rebuild_view()` (increment `_rebuild_counter`, clear+re-add
  items), and `interaction.edit_original_response(content=..., view=self)` showing the current
  picks as `<@id>` mentions (raw-mention convention already used elsewhere in the codebase, e.g.
  `QBdiscordcmds.py` bot-tester allowlist code).
- `_add_clear_button()`: secondary button, disabled when `self.custodian_ids` is empty, sets it
  to `[]` and rebuilds — same shape as `RoleConfigurationView._on_clear_newbie_role`.
- `_add_apply_button()` → `_on_apply`: `defer(thinking=False, ephemeral=True)`, then
  `await CACHE.db_manager.save_guild_clan_custodians(str(self.guild.id), self.clan_tag, self.custodian_ids)`,
  update `CACHE.server_config[guild_id].setdefault("clan_custodians", {})[self.clan_tag] =
  self.custodian_ids` (or `.pop(self.clan_tag, None)` if empty, to keep the in-memory dict tidy),
  then `followup.send` a confirmation. No need to touch/refresh the parent `ClanManagementView`'s
  public message — custodians aren't displayed on the main embed, matching how "Clan
  Settings"/"User Settings" already behave (they don't refresh the parent message either).

This view needs **no new `db_manager` read call at open time** beyond what's already cached:
`_on_custodians` reads the current list straight from
`CACHE.server_config[guild_id].get("clan_custodians", {}).get(self.clan_tag, [])`.

## Message wiring: the actual @mention

`qapbot/war_notifications.py`, `_send_channel_war_notification()`. `clan_tag` and `guild_id_int`
are already in scope at the send point (line 1457-1458). Change:

```python
custodian_ids = guild_config.get("clan_custodians", {}).get(clan_tag, [])
mention_content = None
if custodian_ids:
    mentions = " ".join(f"<@{uid}>" for uid in custodian_ids)
    mention_content = t('ui_components.basic_config.war_channel_notification_custodians_line',
                         guild_id=guild_id_int, mentions=mentions)

if isinstance(channel, (discord.TextChannel, discord.Thread)):
    await channel.send(
        content=mention_content,
        embed=embed,
        allowed_mentions=discord.AllowedMentions(users=True, everyone=False, roles=False)
    )
```

`AllowedMentions(users=True, everyone=False, roles=False)` is added explicitly and only here —
the rest of the codebase relies on discord.py's implicit `AllowedMentions.all()` default for
interactive replies, which is fine there, but this is the first time the bot deliberately mentions
someone from an unattended periodic post, so being explicit here guards against ever
accidentally turning a clan/opponent name containing `@everyone`-like text or a future
copy/paste bug into a mass-ping. `content=None` when there are no custodians is a no-op, so
guilds with none configured see byte-identical behavior to today.

Only `_send_channel_war_notification()` changes — the DM reminder path is untouched (feature
request is explicitly about the channel post, not DMs).

## i18n

Add to both `qapbot/translations/en.json` and `qapbot/translations/de.json`:
- `ui_components.clan_management.button_custodians` — button label.
- `ui_components.prompts.custodians_header` — opening ephemeral message (mirrors
  `clan_notifications_header`).
- `ui_components.custodian_configuration.*` — placeholder for the `UserSelect`, "updated"
  confirmation text (showing current mentions), clear-button label, save-button label (mirror
  the `role_configuration.*` key names 1:1, swapping "role" for "custodian").
- `ui_components.basic_config.war_channel_notification_custodians_line` — the mention line sent
  with the war notification, `{mentions}` placeholder, e.g. English: `"⚠️ {mentions} — please
  check in, attacks are still pending!"`.

## Docs (Cardinal Rule 15)

- `qapbot/docs/DATABASE_ARCHITECTURE.md`: add `guild_clan_custodians` to the core-tables list and
  the "Foreign Key Relationships" section, right next to the existing `guild_clan_roles` entry
  (line ~491), with the same one-line description style.
- `changelog.txt`: new top-of-file entry once implemented, dated 2026-08-21, per the project's
  changelog convention, including the test-suite pass count from `.\run_tests.ps1`.

## Tests

- `db_manager`: extend whichever existing test module covers `guild_clan_roles`
  save/get/delete (pattern-match in `tests/unit/`) with equivalent coverage for
  `save_guild_clan_custodians` — save populates `get_guild_config()`'s `clan_custodians`,
  overwriting with a new list replaces rather than appends, empty list clears the clan's rows
  without touching other clans/guilds.
- `tests/discord/` (new file `test_ui_clan_management_custodians.py`, alongside the existing
  `test_ui_clan_management_*` files): "Custodians" button appears only in `notifications` mode;
  opens `CustodianConfigurationView` seeded from `CACHE.server_config`; selecting via the
  `UserSelect` and hitting Apply calls `save_guild_clan_custodians` with the right args and
  updates `CACHE.server_config`; Clear button empties the list; `max_values` enforces the cap of 5.
- `tests/unit/test_war_notifications_*.py`: `_send_channel_war_notification` includes a
  `content=` mention line with the configured custodians' `<@id>`s and passes the restrictive
  `AllowedMentions` when `clan_custodians` is set for that guild+clan; omits `content` entirely
  (unchanged behavior) when not configured.

## Verification

1. `.\run_tests.ps1` — full suite must stay green, including the new/extended tests above.
2. Manual check on DEV: `/clan management` → notifications mode → Custodians button → select
   1-5 users → Apply → confirm `CACHE.server_config`/DB row reflects it. Full end-to-end channel
   ping can't be exercised on DEV (tracker doc notes DEV never gets live war-notification traffic
   the same way, but the *notification send path itself* — `_send_channel_war_notification` — is
   plain war-notification code, not tracker-gated, so it should still be triggerable on DEV with a
   test war if one is available; otherwise rely on the unit test asserting the `channel.send` call
   args).
3. After merge, use the tracker (`tracker_set_status` via MCP, or the bridge
   `POST /api/tracker/items/5/status {"status": "implemented"}`) to close out tracker item #5.
