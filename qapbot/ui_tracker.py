"""Discord UI layer for the bug/feature tracker (BUG_FEATURE_TRACKER_PLAN.md).

Kept as its own module rather than growing the already ~7000-line ui_clan_management.py
(plan §5.1). Phase 2 implemented BotSetupView, the `/admin -> Bot Setup` screen. Phases 3-5
(this file's bulk) add the full item lifecycle:

- TrackerItemModal / TrackerDraftView / start_tracker_item() — `/bug` and `/feature`'s
  modal-then-draft-preview flow (plan §2.2).
- build_tracker_embed() / _post_tracker_item() — posting a submitted item + its discussion
  thread (plan §2.3).
- TrackerItemButton — the persistent Edit/Add files/Status/Test cases buttons on a posted
  item (restart-safe DynamicItem, plan §5.1).
- Attachment download/sanitize/store/re-upload helpers + the upload-window mechanism
  (_upload_windows) that lets a plain channel message (screenshots included, Ctrl+V works
  natively) attach files after the fact (plan §2.2 point 3, §5.4).
- apply_status_change() / TrackerStatusSelectView — the status lifecycle (plan §4.2, §2.4).
- post_test_cases() / TrackerTestPassButton / TrackerTestFailButton / handle_tracker_test_
  reaction() — the manual test-case sign-off loop, including the 👍-reaction shortcut
  (plan §2.4).

QapBot.py wires the two module-level hooks this file exposes for events it doesn't own:
handle_tracker_upload_message() (called first thing in the existing on_message handler) and
handle_tracker_test_reaction() (a new on_raw_reaction_add listener).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import discord

from qapbot.i18n import t

# bot_settings keys (plan §4.1) — global scope (guild_id='') only, the sole scope wired up today.
TRACKER_SETTING_GUILD_ID = "tracker_guild_id"
TRACKER_SETTING_BUG_CHANNEL = "tracker_bug_channel_id"
TRACKER_SETTING_TEST_CHANNEL = "tracker_test_channel_id"
# Items move out of the working channels once closed, so the reports/test channels don't
# accumulate closed work forever (see apply_status_change()'s move-on-`done` logic).
TRACKER_SETTING_IMPLEMENTED_CHANNEL = "tracker_implemented_channel_id"
TRACKER_SETTING_DONE_TESTING_CHANNEL = "tracker_done_testing_channel_id"
TRACKER_SETTING_ENABLED = "tracker_enabled"
# tracker_feature_channel_id (tracker item #0006, 2026-08-21) is retired — bugs and features now
# share TRACKER_SETTING_BUG_CHANNEL. A pre-existing bot_settings row under that old key, if any,
# is simply never read any more; nothing needs to delete it.

# Item/status metadata (plan §4.2)
ITEM_TYPE_EMOJI = {"bug": "🐞", "feature": "💡"}
STATUS_VALUES: Tuple[str, ...] = (
    "open", "triaged", "in_progress", "implemented", "testing", "done", "rejected", "duplicate",
)
STATUS_COLOR: Dict[str, discord.Color] = {
    "open": discord.Color.blue(),
    "triaged": discord.Color.blue(),
    "in_progress": discord.Color.gold(),
    "implemented": discord.Color.green(),
    "testing": discord.Color.gold(),
    "done": discord.Color.green(),
    "rejected": discord.Color.greyple(),
    "duplicate": discord.Color.greyple(),
}
# DM the reporter only on these key transitions (plan §8.4) — not on intermediate statuses.
DM_NOTIFY_STATUSES = {"implemented", "done", "rejected"}

# Environment selector (replaces the old freetext field, BUG_FEATURE_TRACKER.md workflow
# improvements) — PROD is the default per that request.
ENVIRONMENT_VALUES: Tuple[str, str, str] = ("PROD", "DEV", "BOTH")
_ENVIRONMENT_LABEL_KEYS = {
    "PROD": 'ui_components.tracker.environment_prod',
    "DEV": 'ui_components.tracker.environment_dev',
    "BOTH": 'ui_components.tracker.environment_both',
}


def _normalize_environment(value: Optional[str]) -> str:
    """Maps a legacy freetext Environment value (from before the PROD/DEV/BOTH selector) onto
    one of ENVIRONMENT_VALUES. Unset defaults to PROD (the new default); any other non-empty
    text defaults to BOTH — the more inclusive guess, since the reporter clearly meant
    *something* by it."""
    key = (value or "").strip().lower()
    if key in ("prod", "production"):
        return "PROD"
    if key in ("dev", "development"):
        return "DEV"
    return "BOTH" if key else "PROD"

# Attachment limits (plan §5.5)
MAX_ATTACHMENTS_PER_ITEM = 5
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 20 * 1024 * 1024
UPLOAD_WINDOW_SECONDS = 300
MAX_OPEN_ITEMS_PER_REPORTER = 10
_OPEN_STATUSES_EXCLUDED = {"done", "rejected", "duplicate"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _truncate(text: Optional[str], limit: int) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "… (full text in thread)"


def _lang_guild_id(item: Dict[str, Any]) -> Optional[int]:
    return int(item["guild_id"]) if item.get("guild_id") else None


class BotSetupView(discord.ui.View):
    """`/admin -> Bot Setup`: configure the two bot-wide tracker channels (bug/feature reports,
    manual test cases) plus the runtime enabled/disabled flag. Edits a single message in place,
    mirroring ChannelConfigurationView's idiom in ui_clan_management.py (plan §2.1). Short-lived,
    session-scoped — an ordinary timed view, not a DynamicItem (plan §5.1).

    Tracker item #0006 (2026-08-21): bugs and feature requests used to post to two separate
    channels (a "bug" slot and a "feature" slot); the project owner asked to unify them — both
    `/bug` and `/feature` now post to the single "bug" slot's channel below (the old
    tracker_feature_channel_id bot_settings key is retired, see the comment near the setting
    constants above).

    "implemented" and "done_testing" (2026-08-22) are the move-on-`done` destinations: once an
    item's status becomes `done`, apply_status_change() reposts its embed into "implemented"
    and (if a test-case message exists) its test-case message into "done_testing", deleting the
    old copies — keeps the "bug"/"test" working channels from accumulating closed items. Both
    are optional: unconfigured means the move is skipped and the item stays where it is (a
    setup that never configures them behaves exactly like before this feature existed).

    Row budget: 4 ChannelSelects (rows 0-3) + the Save/Toggle/Close button row (row 4) is
    Discord's 5-action-row cap exactly — no headroom for a 5th slot without restructuring.
    """

    # (slot_key, bot_settings key, i18n label key) — one ChannelSelect row per slot.
    _SLOTS: Tuple[Tuple[str, str, str], ...] = (
        ("bug", TRACKER_SETTING_BUG_CHANNEL, "ui_components.bot_setup.bug_label"),
        ("test", TRACKER_SETTING_TEST_CHANNEL, "ui_components.bot_setup.test_label"),
        ("implemented", TRACKER_SETTING_IMPLEMENTED_CHANNEL, "ui_components.bot_setup.implemented_label"),
        ("done_testing", TRACKER_SETTING_DONE_TESTING_CHANNEL, "ui_components.bot_setup.done_testing_label"),
    )

    def __init__(
        self,
        guild: discord.Guild,
        server_admin: str,
        current_channel_ids: Dict[str, Optional[str]],
        tracker_enabled: bool,
        user_id: str,
        timeout: int = 300,
    ):
        super().__init__(timeout=timeout)
        self.guild = guild
        self.server_admin = server_admin
        self.selected_channel_ids: Dict[str, Optional[str]] = dict(current_channel_ids)
        self.tracker_enabled = tracker_enabled
        self.user_id = user_id
        self.message: Optional[discord.Message] = None

        for row, (slot_key, _setting_key, label_key) in enumerate(self._SLOTS):
            self._add_channel_select(slot_key, label_key, row)
        self._add_buttons(row=len(self._SLOTS))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        from qapbot.QBdiscocmdshelper import check_bot_admin_only
        if check_bot_admin_only(interaction, self.server_admin):
            return True
        await interaction.response.send_message(
            t('commands.errors.bot_admin_only', user_id=self.user_id, guild_id=self.guild.id),
            ephemeral=True,
        )
        return False

    async def on_timeout(self) -> None:
        """Delete the message when the view times out (matches ManageTestersView)."""
        if self.message is not None:
            try:
                await self.message.delete()
            except discord.NotFound:
                pass
            except Exception as e:
                logging.warning(f"Failed to delete Bot Setup message on timeout: {e}")

    def _add_channel_select(self, slot_key: str, label_key: str, row: int) -> None:
        """Add a channel selector for one slot, pre-selected to the currently configured
        channel (if any) via ChannelSelect's default_values — otherwise every reopen of this
        view looks unconfigured even when a channel is already set (Cardinal Rule 8)."""
        label = t(label_key, user_id=self.user_id, guild_id=self.guild.id)
        current_id = self.selected_channel_ids.get(slot_key)
        current_channel = self.guild.get_channel(int(current_id)) if current_id else None
        select = discord.ui.ChannelSelect(
            placeholder=t('ui_components.bot_setup.select_placeholder', user_id=self.user_id, guild_id=self.guild.id, label=label),
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text],
            custom_id=f"tracker_setup_channel_{slot_key}",
            default_values=[current_channel] if current_channel else [],
            row=row,
        )
        select.callback = self._make_select_callback(slot_key)  # type: ignore[assignment]
        self.add_item(select)  # type: ignore[arg-type]

    def _make_select_callback(self, slot_key: str) -> Callable[[discord.Interaction], Any]:
        async def _callback(interaction: discord.Interaction) -> None:
            await interaction.response.defer(thinking=False, ephemeral=True)
            selected = interaction.data['resolved']['channels'][interaction.data['values'][0]]  # type: ignore[index]
            self.selected_channel_ids[slot_key] = str(selected['id'])
            await self._refresh_message()
        return _callback

    def _add_buttons(self, row: int) -> None:
        save_button = discord.ui.Button(
            label=t('ui_components.bot_setup.button_save', user_id=self.user_id, guild_id=self.guild.id),
            style=discord.ButtonStyle.success, custom_id="tracker_setup_save", row=row,
        )
        save_button.callback = self._on_save  # type: ignore[assignment]
        self.add_item(save_button)  # type: ignore[arg-type]

        self.toggle_button = discord.ui.Button(
            label=self._toggle_button_label(), style=self._toggle_button_style(),
            custom_id="tracker_setup_toggle", row=row,
        )
        self.toggle_button.callback = self._on_toggle_enabled  # type: ignore[assignment]
        self.add_item(self.toggle_button)  # type: ignore[arg-type]

        close_button = discord.ui.Button(
            label=t('ui_components.bot_setup.button_close', user_id=self.user_id, guild_id=self.guild.id),
            style=discord.ButtonStyle.secondary, custom_id="tracker_setup_close", row=row,
        )
        close_button.callback = self._on_close  # type: ignore[assignment]
        self.add_item(close_button)  # type: ignore[arg-type]

    def _toggle_button_label(self) -> str:
        key = 'ui_components.bot_setup.button_disable_tracker' if self.tracker_enabled else 'ui_components.bot_setup.button_enable_tracker'
        return t(key, user_id=self.user_id, guild_id=self.guild.id)

    def _toggle_button_style(self) -> discord.ButtonStyle:
        return discord.ButtonStyle.danger if self.tracker_enabled else discord.ButtonStyle.secondary

    def format_header(self) -> str:
        """Build the header message text reflecting the currently selected channels and the
        runtime enabled/disabled status."""
        def channel_text(slot_key: str) -> str:
            channel_id = self.selected_channel_ids.get(slot_key)
            channel = self.guild.get_channel(int(channel_id)) if channel_id else None
            return channel.mention if channel else t('ui_components.bot_setup.channel_not_set', user_id=self.user_id, guild_id=self.guild.id)

        status_key = 'ui_components.bot_setup.status_enabled' if self.tracker_enabled else 'ui_components.bot_setup.status_disabled'
        return t(
            'ui_components.bot_setup.header',
            user_id=self.user_id, guild_id=self.guild.id,
            bug_channel=channel_text("bug"),
            test_channel=channel_text("test"),
            implemented_channel=channel_text("implemented"),
            done_testing_channel=channel_text("done_testing"),
            status=t(status_key, user_id=self.user_id, guild_id=self.guild.id),
        )

    async def _refresh_message(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(content=self.format_header(), view=self)
            except Exception as e:
                logging.error(f"Failed to update Bot Setup message: {e}")

    async def _on_save(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=False, ephemeral=True)
        from qapbot.cache_manager import CACHE
        for slot_key, setting_key, _label_key in self._SLOTS:
            channel_id = self.selected_channel_ids.get(slot_key)
            if channel_id:
                await CACHE.set_tracker_setting(setting_key, channel_id)
        await CACHE.set_tracker_setting(TRACKER_SETTING_GUILD_ID, str(self.guild.id))
        logging.info(f"ADMIN ACTION: {interaction.user} saved tracker channel configuration")
        await self._refresh_message()

    async def _on_toggle_enabled(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=False, ephemeral=True)
        from qapbot.cache_manager import CACHE
        self.tracker_enabled = not self.tracker_enabled
        await CACHE.set_tracker_setting(TRACKER_SETTING_ENABLED, "1" if self.tracker_enabled else "0")
        self.toggle_button.label = self._toggle_button_label()
        self.toggle_button.style = self._toggle_button_style()
        logging.info(f"ADMIN ACTION: {interaction.user} set tracker_enabled={self.tracker_enabled}")
        await self._refresh_message()

    async def _on_close(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=False, ephemeral=True)
        self.stop()
        if self.message is not None:
            try:
                await self.message.delete()
            except discord.NotFound:
                pass
            except Exception as e:
                logging.warning(f"Failed to delete Bot Setup message: {e}")
            self.message = None


async def start_bot_setup(interaction: discord.Interaction) -> None:
    """Entry point for `/admin`'s BOT_SETUP action. Assumes the interaction has already been
    deferred (ephemeral) and that interaction.guild is not None — callers gate both before
    calling this (see QBdiscordcmds.py)."""
    from qapbot.cache_manager import CACHE
    from qapbot.config import CONFIG

    assert interaction.guild is not None
    guild = interaction.guild
    user_id = str(interaction.user.id)

    current_channel_ids = {
        "bug": CACHE.tracker_settings.get(TRACKER_SETTING_BUG_CHANNEL),
        "test": CACHE.tracker_settings.get(TRACKER_SETTING_TEST_CHANNEL),
        "implemented": CACHE.tracker_settings.get(TRACKER_SETTING_IMPLEMENTED_CHANNEL),
        "done_testing": CACHE.tracker_settings.get(TRACKER_SETTING_DONE_TESTING_CHANNEL),
    }
    tracker_enabled = CACHE.tracker_settings.get(TRACKER_SETTING_ENABLED) != "0"

    view = BotSetupView(
        guild=guild,
        server_admin=CONFIG.server_admin,
        current_channel_ids=current_channel_ids,
        tracker_enabled=tracker_enabled,
        user_id=user_id,
    )
    view.message = await interaction.followup.send(view.format_header(), view=view, ephemeral=True)


# ===========================================================================
# Attachment handling (plan §2.2 point 3, §3.3, §5.5)
# ===========================================================================

def _sanitize_attachment_filename(original_name: str, index: int) -> str:
    """`[A-Za-z0-9._-]{1,64}` + a numeric prefix — never trust attachment.filename for the
    on-disk path (plan §5.5). lstrip('.') defeats a pure-dots stem (e.g. "..") that would
    otherwise survive the character whitelist and let a filename escape its item directory
    via os.path.join."""
    stem = re.sub(r'[^A-Za-z0-9._-]', '_', original_name or "file")
    stem = stem.replace('..', '_')  # belt-and-suspenders: no ".." substring survives at all,
    # not just no path separator — matches plan §5.5's sanitizing spec verbatim.
    stem = stem.lstrip('.').rstrip('. ').strip('_')[:64] or "file"
    return f"{index}_{stem}"


async def _predownload_attachment(attachment: discord.Attachment) -> Dict[str, Any]:
    """Download one Discord attachment into memory. Called as early as possible (before/while
    the modal is still open) since the CDN URL on a user's own attachment is signed and
    expires (plan §2.2's ordering constraint, §3.3)."""
    if attachment.size > MAX_ATTACHMENT_BYTES:
        return {"error": "too_large", "original_name": attachment.filename}
    try:
        data = await attachment.read()
    except Exception as e:
        logging.warning(f"[TRACKER] Failed to download attachment {attachment.filename!r}: {e}")
        return {"error": "download_failed", "original_name": attachment.filename}
    return {
        "original_name": attachment.filename,
        "content_type": attachment.content_type or "application/octet-stream",
        "data": data,
        "size_bytes": len(data),
    }


async def _predownload_attachments(attachments: List[discord.Attachment]) -> List[Dict[str, Any]]:
    results = []
    for attachment in attachments[:MAX_ATTACHMENTS_PER_ITEM]:
        results.append(await _predownload_attachment(attachment))
    return results


async def _persist_attachment(item_number: int, index: int, pending: Dict[str, Any]) -> None:
    """Write a predownloaded attachment to CONFIG.tracker_data_dir and record it in
    tracker_attachments (plan §3.3 — the on-disk copy is what an agent reads locally, with no
    dependency on Discord's expiring CDN URLs)."""
    from qapbot.config import CONFIG
    from qapbot.cache_manager import CACHE

    filename = _sanitize_attachment_filename(pending["original_name"], index)
    item_dir = os.path.join(CONFIG.tracker_data_dir, f"{item_number:04d}")

    def _write() -> str:
        os.makedirs(item_dir, exist_ok=True)
        full_path = os.path.join(item_dir, filename)
        with open(full_path, "wb") as f:
            f.write(pending["data"])
        return full_path

    full_path = await asyncio.to_thread(_write)
    await CACHE.db_manager.add_tracker_attachment(
        item_number,
        filename=filename,
        original_name=pending["original_name"],
        size_bytes=pending["size_bytes"],
        local_path=full_path,
        content_type=pending["content_type"],
    )


async def _build_discord_files(item_number: int) -> List[discord.File]:
    """Bot-owned re-upload of every stored attachment (plan §2.3 — the user's original CDN
    link expires; only this copy is durable). Stops (rather than erroring) once the running
    total would exceed the 20 MB/item message cap; anything left over is simply omitted from
    the item message — the on-disk copy under CONFIG.tracker_data_dir is never lost."""
    from qapbot.cache_manager import CACHE

    attachments = await CACHE.db_manager.get_tracker_attachments(item_number)
    files: List[discord.File] = []
    total = 0
    for att in attachments:
        if total + att["size_bytes"] > MAX_TOTAL_ATTACHMENT_BYTES:
            break
        if not os.path.exists(att["local_path"]):
            continue
        files.append(discord.File(att["local_path"], filename=att["filename"]))
        total += att["size_bytes"]
    return files


@dataclass
class _UploadWindow:
    on_files: Callable[[List[Dict[str, Any]]], Awaitable[None]]
    expires_at: float


# Keyed by (discord_user_id, channel_id) — one open window per user per channel at a time.
# In-memory only (session-scoped, ≤5 min lifetime) — no restart-survival requirement.
_upload_windows: Dict[Tuple[int, int], _UploadWindow] = {}


def _register_upload_window(
    user_id: int, channel_id: int, on_files: Callable[[List[Dict[str, Any]]], Awaitable[None]]
) -> None:
    _upload_windows[(user_id, channel_id)] = _UploadWindow(
        on_files=on_files, expires_at=time.monotonic() + UPLOAD_WINDOW_SECONDS
    )


async def handle_tracker_upload_message(message: discord.Message) -> bool:
    """Called from QapBot.py's on_message handler, BEFORE the DM-fallback branch (plan §5.4) —
    a file uploaded in a DM during an open window must be consumed here, not answered with the
    generic "use /help" fallback text. Returns True if this message was consumed by an open
    upload window (caller should return immediately after); False lets it fall through
    unchanged (including: no open window, or an open window but this message carried no
    attachments — e.g. ordinary chat in a DM, left untouched so the window stays open)."""
    if message.author.bot:
        return False
    key = (message.author.id, message.channel.id)
    window = _upload_windows.get(key)
    if window is None:
        return False
    if time.monotonic() > window.expires_at:
        _upload_windows.pop(key, None)
        return False
    if not message.attachments:
        return False

    del _upload_windows[key]
    pending = []
    for result in await _predownload_attachments(list(message.attachments)):
        if "error" in result:
            logging.info(f"[TRACKER] Skipped upload-window attachment {result['original_name']!r}: {result['error']}")
        else:
            pending.append(result)

    try:
        await message.delete()
    except discord.NotFound:
        pass
    except Exception as e:
        logging.warning(f"[TRACKER] Failed to delete upload-window message: {e}")

    await window.on_files(pending)
    return True


# ===========================================================================
# Modal + draft preview (plan §2.2)
# ===========================================================================

class TrackerItemModal(discord.ui.Modal, title="Report an item"):
    """Title/Description/Details/Environment — type-aware labels, Environment removed
    entirely for features (plan §2.2). Serves three purposes selected by which optional
    constructor args are set: fresh submission (neither), editing a still-open draft
    (draft_view), or editing an already-posted item (item_number)."""

    # TextInput/Label MUST be class attributes for discord.py's Modal system (Cardinal Rule 10).
    title_input = discord.ui.TextInput(label="Title", required=True, max_length=100)
    description_input = discord.ui.TextInput(
        label="Description", style=discord.TextStyle.paragraph, required=True, max_length=4000
    )
    details_input = discord.ui.TextInput(
        label="Details", style=discord.TextStyle.paragraph, required=False, max_length=4000
    )
    # Label-wrapped Select (discord.py 2.6+ Components V2) — see CODE_STRUCTURE.md § Discord.py
    # Patterns "Select Menus Inside Modals" for why this replaces a plain TextInput here.
    environment_select = discord.ui.Label(
        text="Environment",
        component=discord.ui.Select(
            options=[discord.SelectOption(label=v, value=v, default=(v == "PROD")) for v in ENVIRONMENT_VALUES],
        ),
    )

    def __init__(
        self,
        item_type: str,
        guild_id: Optional[int],
        user_id: str,
        *,
        draft_view: Optional['TrackerDraftView'] = None,
        item_number: Optional[int] = None,
        initial_title: str = "",
        initial_description: str = "",
        initial_details: str = "",
        initial_environment: str = "",
    ):
        super().__init__()
        self.item_type = item_type
        self.guild_id = guild_id
        self.user_id = user_id
        self.draft_view = draft_view
        self.item_number = item_number
        self.predownload_task: Optional['asyncio.Task[List[Dict[str, Any]]]'] = None
        self._localize()
        self.title_input.default = initial_title
        self.description_input.default = initial_description
        self.details_input.default = initial_details
        if item_type == "bug":
            self._set_environment_options(initial_environment)
        else:
            self.remove_item(self.environment_select)

    def _localize(self) -> None:
        title_key = 'ui_components.tracker.modal_title_bug' if self.item_type == 'bug' else 'ui_components.tracker.modal_title_feature'
        details_key = 'ui_components.tracker.field_details_bug' if self.item_type == 'bug' else 'ui_components.tracker.field_details_feature'
        self.title = t(title_key, user_id=self.user_id, guild_id=self.guild_id)
        self.title_input.label = t('ui_components.tracker.field_title', user_id=self.user_id, guild_id=self.guild_id)
        self.description_input.label = t('ui_components.tracker.field_description', user_id=self.user_id, guild_id=self.guild_id)
        self.details_input.label = t(details_key, user_id=self.user_id, guild_id=self.guild_id)
        self.environment_select.text = t('ui_components.tracker.field_environment', user_id=self.user_id, guild_id=self.guild_id)

    def _set_environment_options(self, initial_environment: str) -> None:
        current = _normalize_environment(initial_environment)
        self.environment_select.component.options = [
            discord.SelectOption(
                label=t(_ENVIRONMENT_LABEL_KEYS[value], user_id=self.user_id, guild_id=self.guild_id),
                value=value, default=(value == current),
            )
            for value in ENVIRONMENT_VALUES
        ]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from qapbot.cache_manager import CACHE

        title_val = self.title_input.value.strip()
        description_val = self.description_input.value.strip()
        details_val = (self.details_input.value or "").strip()
        environment_val = self.environment_select.component.values[0] if self.item_type == "bug" else ""

        if self.item_number is not None:
            # Editing an already-posted item (Edit button, plan §2.3).
            await interaction.response.defer(thinking=False, ephemeral=True)
            await CACHE.db_manager.update_tracker_item(
                self.item_number,
                title=title_val, description=description_val,
                details=details_val or None,
                environment=(environment_val or None) if self.item_type == "bug" else None,
                last_edited_by=str(interaction.user.id), last_edited_at=_now_iso(),
            )
            item = await CACHE.db_manager.get_tracker_item(self.item_number)
            if item is not None:
                await _refresh_item_message(item)
            await interaction.followup.send(
                t('ui_components.tracker.edit_saved', user_id=self.user_id, guild_id=self.guild_id), ephemeral=True
            )
            return

        if self.draft_view is not None:
            # Editing a still-open draft (Edit button on the draft preview).
            await interaction.response.defer(thinking=False, ephemeral=True)
            self.draft_view.title_text = title_val
            self.draft_view.description_text = description_val
            self.draft_view.details_text = details_val
            self.draft_view.environment_text = environment_val
            await self.draft_view.refresh()
            return

        # Fresh submission -> build the draft preview.
        await interaction.response.defer(thinking=True, ephemeral=True)
        pending_attachments: List[Dict[str, Any]] = []
        if self.predownload_task is not None:
            try:
                results = await self.predownload_task
            except Exception as e:
                logging.warning(f"[TRACKER] Attachment predownload task failed: {e}")
                results = []
            for result in results:
                if "error" in result:
                    await interaction.followup.send(
                        t('ui_components.tracker.attachment_too_large', user_id=self.user_id, guild_id=self.guild_id, filename=result["original_name"]),
                        ephemeral=True,
                    )
                else:
                    pending_attachments.append(result)

        draft = TrackerDraftView(
            item_type=self.item_type,
            title=title_val, description=description_val, details=details_val, environment=environment_val,
            reporter_id=str(interaction.user.id), reporter_name=str(interaction.user),
            guild_id=self.guild_id, channel_id=interaction.channel.id if interaction.channel else 0,
            user_id=self.user_id, pending_attachments=pending_attachments,
        )
        draft.message = await interaction.followup.send(draft.format_preview(), view=draft, ephemeral=True)


class TrackerDraftView(discord.ui.View):
    """Ephemeral draft preview shown after modal submit — Edit / Add attachments / Submit /
    Discard (plan §2.2 point 2). Short-lived, session-scoped, not restart-safe by design (an
    abandoned draft simply expires — nothing was persisted to the DB yet)."""

    def __init__(
        self, item_type: str, title: str, description: str, details: str, environment: str,
        reporter_id: str, reporter_name: str, guild_id: Optional[int], channel_id: int, user_id: str,
        pending_attachments: Optional[List[Dict[str, Any]]] = None, timeout: int = 900,
    ):
        super().__init__(timeout=timeout)
        self.item_type = item_type
        self.title_text = title
        self.description_text = description
        self.details_text = details
        self.environment_text = environment
        self.reporter_id = reporter_id
        self.reporter_name = reporter_name
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.user_id = user_id
        self.pending_attachments: List[Dict[str, Any]] = list(pending_attachments or [])
        self.message: Optional[discord.Message] = None
        self.submitted = False
        self._build_buttons()

    def _build_buttons(self) -> None:
        edit_btn = discord.ui.Button(label=t('ui_components.tracker.button_edit', user_id=self.user_id, guild_id=self.guild_id), style=discord.ButtonStyle.secondary)
        edit_btn.callback = self._on_edit  # type: ignore[assignment]
        self.add_item(edit_btn)

        add_btn = discord.ui.Button(label=t('ui_components.tracker.button_add_attachments', user_id=self.user_id, guild_id=self.guild_id), style=discord.ButtonStyle.secondary)
        add_btn.callback = self._on_add_attachments  # type: ignore[assignment]
        self.add_item(add_btn)

        submit_btn = discord.ui.Button(label=t('ui_components.tracker.button_submit', user_id=self.user_id, guild_id=self.guild_id), style=discord.ButtonStyle.success)
        submit_btn.callback = self._on_submit  # type: ignore[assignment]
        self.add_item(submit_btn)

        discard_btn = discord.ui.Button(label=t('ui_components.tracker.button_discard', user_id=self.user_id, guild_id=self.guild_id), style=discord.ButtonStyle.danger)
        discard_btn.callback = self._on_discard  # type: ignore[assignment]
        self.add_item(discard_btn)

    def format_preview(self) -> str:
        emoji = ITEM_TYPE_EMOJI.get(self.item_type, "📝")
        details_key = 'ui_components.tracker.field_details_bug' if self.item_type == 'bug' else 'ui_components.tracker.field_details_feature'
        lines = [
            t('ui_components.tracker.draft_header', user_id=self.user_id, guild_id=self.guild_id),
            "", f"{emoji} **{self.title_text}**", "", self.description_text,
        ]
        if self.details_text:
            lines += ["", f"**{t(details_key, user_id=self.user_id, guild_id=self.guild_id)}**", self.details_text]
        if self.environment_text:
            lines += ["", f"**{t('ui_components.tracker.field_environment', user_id=self.user_id, guild_id=self.guild_id)}:** {self.environment_text}"]
        if self.pending_attachments:
            names = ", ".join(a["original_name"] for a in self.pending_attachments)
            lines += ["", t('ui_components.tracker.draft_attachments', user_id=self.user_id, guild_id=self.guild_id, list=names)]
        else:
            lines += ["", t('ui_components.tracker.draft_attachments_none', user_id=self.user_id, guild_id=self.guild_id)]
        return "\n".join(lines)

    async def refresh(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(content=self.format_preview(), view=self)
            except Exception as e:
                logging.error(f"[TRACKER] Failed to refresh draft message: {e}")

    async def _on_edit(self, interaction: discord.Interaction) -> None:
        modal = TrackerItemModal(
            self.item_type, self.guild_id, self.user_id, draft_view=self,
            initial_title=self.title_text, initial_description=self.description_text,
            initial_details=self.details_text, initial_environment=self.environment_text,
        )
        await interaction.response.send_modal(modal)

    async def _on_add_attachments(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            t('ui_components.tracker.upload_window_prompt', user_id=self.user_id, guild_id=self.guild_id), ephemeral=True
        )

        async def _on_files(pending: List[Dict[str, Any]]) -> None:
            room = MAX_ATTACHMENTS_PER_ITEM - len(self.pending_attachments)
            if room <= 0:
                await interaction.followup.send(
                    t('ui_components.tracker.attachment_limit_reached', user_id=self.user_id, guild_id=self.guild_id), ephemeral=True
                )
                return
            self.pending_attachments.extend(pending[:room])
            await self.refresh()

        _register_upload_window(interaction.user.id, self.channel_id, _on_files)

    async def _on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=False, ephemeral=True)
        from qapbot.cache_manager import CACHE

        db = CACHE.db_manager
        item_number = await db.create_tracker_item(
            item_type=self.item_type, title=self.title_text, description=self.description_text,
            reporter_id=self.reporter_id, reporter_name=self.reporter_name,
            details=self.details_text or None,
            environment=(self.environment_text or None) if self.item_type == "bug" else None,
            guild_id=str(self.guild_id) if self.guild_id else None,
        )

        for index, pending in enumerate(self.pending_attachments, start=1):
            await _persist_attachment(item_number, index, pending)

        # Bugs and features share one channel (tracker item #0006) — item_type no longer
        # selects between two settings, just which emoji/label the posted embed itself shows.
        channel_id_str = CACHE.tracker_settings.get(TRACKER_SETTING_BUG_CHANNEL)
        message = None
        if channel_id_str:
            message, _thread = await _post_tracker_item(item_number, int(channel_id_str))

        self.submitted = True
        self.stop()
        jump_link = message.jump_url if message else ""
        text = t(
            'ui_components.tracker.submitted', user_id=self.user_id, guild_id=self.guild_id,
            item_number=f"{item_number:04d}", jump_link=jump_link,
        )
        if self.message is not None:
            try:
                await self.message.edit(content=text, view=None)
            except Exception as e:
                logging.warning(f"[TRACKER] Failed to update draft message after submit: {e}")

    async def _on_discard(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=False, ephemeral=True)
        self.stop()
        if self.message is not None:
            try:
                await self.message.edit(
                    content=t('ui_components.tracker.discarded', user_id=self.user_id, guild_id=self.guild_id), view=None
                )
            except Exception as e:
                logging.warning(f"[TRACKER] Failed to update draft message after discard: {e}")


async def start_tracker_item(
    interaction: discord.Interaction,
    item_type: str,
    attachment1: Optional[discord.Attachment] = None,
    attachment2: Optional[discord.Attachment] = None,
    attachment3: Optional[discord.Attachment] = None,
) -> None:
    """Shared entry point for `/bug` and `/feature` (plan §5.2). Kicks off attachment
    pre-downloads (if any) as a background task immediately after send_modal() — the modal
    itself must be the interaction's first response (Cardinal Rule 7-adjacent Modal
    requirement), and the user may sit in it for minutes while the signed CDN URLs on their
    own command-param attachments keep ticking toward expiry (plan §2.2)."""
    from qapbot.cache_manager import CACHE

    guild_id = interaction.guild.id if interaction.guild else None
    user_id = str(interaction.user.id)

    if CACHE.tracker_settings.get(TRACKER_SETTING_ENABLED) == "0":
        await interaction.response.send_message(
            t('ui_components.tracker.tracker_disabled', user_id=user_id, guild_id=guild_id), ephemeral=True
        )
        return

    # Bugs and features share one channel (tracker item #0006).
    if not CACHE.tracker_settings.get(TRACKER_SETTING_BUG_CHANNEL):
        await interaction.response.send_message(
            t('ui_components.tracker.tracker_not_configured', user_id=user_id, guild_id=guild_id), ephemeral=True
        )
        return

    existing_items = await CACHE.db_manager.list_tracker_items(reporter_id=user_id, limit=100)
    open_count = sum(1 for it in existing_items if it["status"] not in _OPEN_STATUSES_EXCLUDED)
    if open_count >= MAX_OPEN_ITEMS_PER_REPORTER:
        await interaction.response.send_message(
            t('ui_components.tracker.too_many_open_items', user_id=user_id, guild_id=guild_id, count=open_count), ephemeral=True
        )
        return

    modal = TrackerItemModal(item_type, guild_id, user_id)
    await interaction.response.send_modal(modal)

    attachments = [a for a in (attachment1, attachment2, attachment3) if a is not None]
    if attachments:
        modal.predownload_task = asyncio.create_task(_predownload_attachments(attachments))


# ===========================================================================
# Posted item: embed, view, posting, refresh (plan §2.3)
# ===========================================================================

def build_tracker_embed(item: Dict[str, Any]) -> discord.Embed:
    emoji = ITEM_TYPE_EMOJI.get(item["item_type"], "📝")
    status = item["status"]
    color = STATUS_COLOR.get(status, discord.Color.blue())
    guild_id = _lang_guild_id(item)
    status_text = t(f'ui_components.tracker.status_{status}', guild_id=guild_id)

    embed = discord.Embed(
        title=f"{emoji} #{item['item_number']:04d} · {_truncate(item['title'], 180)}  [{status_text}]",
        color=color,
    )
    reporter_line = t(
        'ui_components.tracker.reported_by', guild_id=guild_id,
        reporter_id=item['reporter_id'], created_at=item['created_at'],
    )
    if item.get("environment"):
        reporter_line += f"  ·  {t('ui_components.tracker.field_environment', guild_id=guild_id)}: {item['environment']}"
    embed.description = (
        f"{reporter_line}\n\n**{t('ui_components.tracker.field_description', guild_id=guild_id)}**\n"
        f"{_truncate(item['description'], 1000)}"
    )

    if item.get("details"):
        details_key = 'ui_components.tracker.field_details_bug' if item['item_type'] == 'bug' else 'ui_components.tracker.field_details_feature'
        embed.add_field(name=t(details_key, guild_id=guild_id), value=_truncate(item["details"], 1000), inline=False)
    if item.get("implemented_note"):
        embed.add_field(name=t('ui_components.tracker.implemented_in', guild_id=guild_id), value=_truncate(item["implemented_note"], 500), inline=False)
    embed.set_footer(text=f"#{item['item_number']:04d}")
    return embed


def build_tracker_item_view(item_number: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(TrackerItemButton("edit", item_number))
    view.add_item(TrackerItemButton("addfiles", item_number))
    view.add_item(TrackerItemButton("status", item_number))
    view.add_item(TrackerItemButton("testcases", item_number))
    return view


async def _post_tracker_item(item_number: int, channel_id: int) -> Tuple[Optional[discord.Message], Optional[discord.Thread]]:
    """Post a freshly-created item to its configured channel, re-uploading attachments and
    creating the discussion thread (plan §2.3). Returns (message, thread); either may be None
    if the channel/thread step failed (logged, never raised — a failed post must not lose the
    already-persisted DB row)."""
    from qapbot.cache_manager import CACHE
    import QBcore

    item = await CACHE.db_manager.get_tracker_item(item_number)
    if item is None:
        return None, None

    channel = QBcore.bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await QBcore.bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            logging.error(f"[TRACKER] Could not resolve tracker channel {channel_id} for item #{item_number}: {e}")
            return None, None

    embed = build_tracker_embed(item)
    view = build_tracker_item_view(item_number)
    files = await _build_discord_files(item_number)
    try:
        message = await channel.send(embed=embed, view=view, files=files)  # type: ignore[union-attr]
    except Exception as e:
        logging.error(f"[TRACKER] Failed to post item #{item_number}: {e}")
        return None, None

    await CACHE.db_manager.set_tracker_item_message(item_number, channel_id=str(channel_id), message_id=str(message.id))

    thread = None
    try:
        thread = await message.create_thread(name=f"#{item_number:04d} — discussion")
        # Untruncated text as the first thread message (plan §2.3's embed-overflow strategy) —
        # posted unconditionally rather than only when the embed actually truncated, since
        # that keeps this simple and is never wrong either way.
        full_text = f"**{item['title']}**\n\n{item['description']}"
        if item.get("details"):
            full_text += f"\n\n{item['details']}"
        for i in range(0, len(full_text), 2000):
            await thread.send(full_text[i:i + 2000])
        await CACHE.db_manager.update_tracker_item(item_number, thread_id=str(thread.id))
    except Exception as e:
        logging.warning(f"[TRACKER] Failed to create discussion thread for item #{item_number}: {e}")

    return message, thread


def _build_archived_item_embed(item: Dict[str, Any]) -> discord.Embed:
    """The embed used once an item is sitting in the Implemented channel: the normal embed plus
    a "Discussion thread" jump-link field, since threads can't move channels — once the item's
    original message is deleted the thread becomes an orphan with no visible parent, so this
    keeps it reachable (same URL scheme as the "Test cases" jump link)."""
    embed = build_tracker_embed(item)
    if item.get("thread_id"):
        jump_link = f"https://discord.com/channels/{item['guild_id'] or '@me'}/{item['thread_id']}"
        embed.add_field(
            name=t('ui_components.tracker.discussion_thread_field', guild_id=_lang_guild_id(item)),
            value=f"[Jump to thread]({jump_link})", inline=False,
        )
    return embed


async def _refresh_item_message(item: Dict[str, Any], archived: bool = False) -> None:
    """Re-render the posted item embed/view in place. Never passes `attachments=` — doing so
    would replace (not preserve) the existing file set (plan §5.5). *archived* renders the item
    as it looks once it has moved to the Implemented channel — the thread jump-link field, and
    no buttons (`view=None`, nothing left to do on a closed item) — for the case where the item
    is already posted there and apply_status_change() re-enters `done` without moving it again."""
    if not item.get("channel_id") or not item.get("message_id"):
        return
    import QBcore

    channel = QBcore.bot.get_channel(int(item["channel_id"]))
    if channel is None:
        try:
            channel = await QBcore.bot.fetch_channel(int(item["channel_id"]))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
    try:
        message = await channel.fetch_message(int(item["message_id"]))  # type: ignore[union-attr]
    except discord.NotFound:
        return
    except Exception as e:
        logging.warning(f"[TRACKER] Failed to fetch item #{item['item_number']} message: {e}")
        return

    embed = _build_archived_item_embed(item) if archived else build_tracker_embed(item)
    view = None if archived else build_tracker_item_view(item["item_number"])
    try:
        await message.edit(embed=embed, view=view)
    except Exception as e:
        logging.warning(f"[TRACKER] Failed to update item #{item['item_number']} message: {e}")


async def _move_item_to_implemented_channel(item: Dict[str, Any]) -> bool:
    """Once an item reaches `done`, repost its embed (no buttons — nothing left to do on a
    closed item) into the configured Implemented channel and delete the old copy — keeps the
    working reports channel from accumulating closed items. Returns False (no-op, caller should
    fall back to an in-place refresh) if the Implemented channel isn't configured or the item is
    already posted there; never raises (a failed move must not lose the item's DB row, matching
    _post_tracker_item's convention)."""
    from qapbot.cache_manager import CACHE
    import QBcore

    item_number = item["item_number"]
    channel_id_str = CACHE.tracker_settings.get(TRACKER_SETTING_IMPLEMENTED_CHANNEL)
    if not channel_id_str or item.get("channel_id") == channel_id_str:
        return False

    new_channel = QBcore.bot.get_channel(int(channel_id_str))
    if new_channel is None:
        try:
            new_channel = await QBcore.bot.fetch_channel(int(channel_id_str))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            logging.error(f"[TRACKER] Could not resolve Implemented channel for item #{item_number}: {e}")
            return False

    embed = _build_archived_item_embed(item)
    files = await _build_discord_files(item_number)
    try:
        new_message = await new_channel.send(embed=embed, files=files)  # type: ignore[union-attr]  # no view= -> no buttons
    except Exception as e:
        logging.error(f"[TRACKER] Failed to post item #{item_number} to Implemented channel: {e}")
        return False

    old_channel_id, old_message_id = item.get("channel_id"), item.get("message_id")
    if old_channel_id and old_message_id:
        try:
            old_channel = QBcore.bot.get_channel(int(old_channel_id)) or await QBcore.bot.fetch_channel(int(old_channel_id))
            old_message = await old_channel.fetch_message(int(old_message_id))  # type: ignore[union-attr]
            await old_message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            logging.warning(f"[TRACKER] Failed to delete original item #{item_number} message after move: {e}")

    # update_tracker_item(), not set_tracker_item_message() — the latter always overwrites
    # thread_id (defaulting to None) and would wipe the jump link above's target.
    await CACHE.db_manager.update_tracker_item(item_number, channel_id=str(new_channel.id), message_id=str(new_message.id))
    return True


async def _dm_reporter_on_status_change(item: Dict[str, Any]) -> None:
    import QBcore

    key = {
        "implemented": 'ui_components.tracker.dm_status_implemented',
        "done": 'ui_components.tracker.dm_status_done',
        "rejected": 'ui_components.tracker.dm_status_rejected',
    }.get(item["status"])
    if key is None:
        return

    user_id = item["reporter_id"]
    type_key = 'ui_components.tracker.item_type_bug' if item["item_type"] == "bug" else 'ui_components.tracker.item_type_feature'
    message_text = t(
        key, user_id=user_id, type=t(type_key, user_id=user_id),
        item_number=f"{item['item_number']:04d}", title=item["title"],
    )
    try:
        user = QBcore.bot.get_user(int(user_id)) or await QBcore.bot.fetch_user(int(user_id))
        await user.send(message_text)
    except discord.Forbidden:
        logging.info(f"[TRACKER] Could not DM reporter {user_id} for item #{item['item_number']} (closed DMs)")
    except Exception as e:
        logging.warning(f"[TRACKER] Failed to DM reporter {user_id} for item #{item['item_number']}: {e}")


async def apply_status_change(
    item_number: int, new_status: str, note: Optional[str] = None, actor_id: Optional[str] = None
) -> Dict[str, Any]:
    """Update DB + the posted embed, and DM the reporter on key transitions (plan §2.4, §4.2,
    §8.4). Shared by the Discord status dropdown, the web bridge, and the MCP server — the
    one place status changes actually happen."""
    from qapbot.cache_manager import CACHE

    db = CACHE.db_manager
    item = await db.get_tracker_item(item_number)
    if item is None:
        raise ValueError(f"tracker item #{item_number} not found")

    fields: Dict[str, Any] = {"status": new_status}
    if actor_id:
        fields["last_edited_by"] = actor_id
        fields["last_edited_at"] = _now_iso()
    if new_status == "implemented":
        fields["implemented_at"] = _now_iso()
        if note:
            fields["implemented_note"] = note
    elif new_status in ("done", "rejected", "duplicate"):
        fields["closed_at"] = _now_iso()
    await db.update_tracker_item(item_number, **fields)
    item = await db.get_tracker_item(item_number)
    assert item is not None

    if new_status == "done":
        # Move-on-done: repost the item into the Implemented channel, and (if a test-case
        # message exists) the test-case message into Done Testing — both are single-choke-point
        # here so this covers the status dropdown, the bridge/MCP tool, AND the automatic
        # done-transition in mark_environment_passed_and_refresh() (the 👍 sign-off flow calls
        # this same function), with no changes needed anywhere else.
        moved = await _move_item_to_implemented_channel(item)
        if not moved:
            implemented_channel_id = CACHE.tracker_settings.get(TRACKER_SETTING_IMPLEMENTED_CHANNEL)
            already_archived = bool(implemented_channel_id) and item.get("channel_id") == implemented_channel_id
            await _refresh_item_message(item, archived=already_archived)
        await _move_test_message_to_done_testing_channel(item)
        item = await db.get_tracker_item(item_number)
        assert item is not None
    else:
        await _refresh_item_message(item)

    if new_status in DM_NOTIFY_STATUSES:
        await _dm_reporter_on_status_change(item)
    return item


async def post_comment(item_number: int, text: str, author_id: str) -> None:
    """Post into the item's discussion thread (plan §6.4's `/comment` endpoint)."""
    from qapbot.cache_manager import CACHE
    import QBcore

    item = await CACHE.db_manager.get_tracker_item(item_number)
    if item is None or not item.get("thread_id"):
        raise ValueError(f"tracker item #{item_number} has no discussion thread")

    thread = QBcore.bot.get_channel(int(item["thread_id"]))
    if thread is None:
        thread = await QBcore.bot.fetch_channel(int(item["thread_id"]))
    message = t('ui_components.tracker.comment_posted', guild_id=_lang_guild_id(item), author_id=author_id, text=text)
    await thread.send(message)  # type: ignore[union-attr]


async def _check_reporter_or_admin(interaction: discord.Interaction, item: Dict[str, Any]) -> bool:
    """Edit/Add files gate: the reporter themself, or a bot admin (plan §2.3)."""
    from qapbot.config import CONFIG
    from qapbot.QBdiscocmdshelper import check_bot_admin_only

    user_id = str(interaction.user.id)
    if user_id == item["reporter_id"] or check_bot_admin_only(interaction, CONFIG.server_admin):
        return True
    guild_id = interaction.guild.id if interaction.guild else None
    await interaction.response.send_message(
        t('ui_components.tracker.edit_denied', user_id=user_id, guild_id=guild_id), ephemeral=True
    )
    return False


def _startup_gate_message(guild_id: Optional[int]) -> str:
    return t('commands.errors.startup_in_progress', guild_id=guild_id)


TRACKER_ITEM_BUTTON_TEMPLATE = r'^tracker:(?P<action>edit|addfiles|status|testcases):(?P<item_number>\d+)$'


class TrackerItemButton(
    discord.ui.DynamicItem[discord.ui.Button],  # type: ignore[type-arg]
    template=TRACKER_ITEM_BUTTON_TEMPLATE,
):
    """Restart-safe buttons on a posted tracker item message: Edit, Add files, Status
    (admin), Test cases (plan §5.1). custom_id embeds action/item_number so from_custom_id()
    fully reconstructs this item's state on every dispatch, with no lookup table."""

    _LABEL_KEYS = {
        "edit": 'ui_components.tracker.button_edit',
        "addfiles": 'ui_components.tracker.button_add_files',
        "status": 'ui_components.tracker.button_status',
        "testcases": 'ui_components.tracker.button_test_cases',
    }
    _STYLES = {
        "edit": discord.ButtonStyle.secondary,
        "addfiles": discord.ButtonStyle.secondary,
        "status": discord.ButtonStyle.primary,
        "testcases": discord.ButtonStyle.secondary,
    }

    def __init__(self, action: str, item_number: int):
        self.action = action
        self.item_number = item_number
        super().__init__(
            discord.ui.Button(
                label=t(self._LABEL_KEYS[action]),
                style=self._STYLES[action],
                custom_id=f"tracker:{action}:{item_number}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: 're.Match[str]', /
    ) -> 'TrackerItemButton':
        return cls(action=match["action"], item_number=int(match["item_number"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Pitfall 20 gate — add_dynamic_items() registers this for dispatch before CACHE/DB
        finish loading post-restart; block clicks until startup is confirmed complete."""
        import QBcore
        if not getattr(QBcore.bot, 'fully_initialized', False):
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.response.send_message(_startup_gate_message(guild_id), ephemeral=True)
            return False
        return True

    async def callback(self, interaction: discord.Interaction) -> None:
        from qapbot.cache_manager import CACHE

        item = await CACHE.db_manager.get_tracker_item(self.item_number)
        user_id = str(interaction.user.id)
        guild_id = interaction.guild.id if interaction.guild else None
        if item is None:
            await interaction.response.send_message(
                t('ui_components.tracker.item_not_found', user_id=user_id, guild_id=guild_id, item_number=f"{self.item_number:04d}"),
                ephemeral=True,
            )
            return

        if self.action == "edit":
            await self._handle_edit(interaction, item)
        elif self.action == "addfiles":
            await self._handle_add_files(interaction, item)
        elif self.action == "status":
            await self._handle_status(interaction, item)
        elif self.action == "testcases":
            await self._handle_testcases(interaction, item)

    async def _handle_edit(self, interaction: discord.Interaction, item: Dict[str, Any]) -> None:
        if not await _check_reporter_or_admin(interaction, item):
            return
        guild_id = interaction.guild.id if interaction.guild else None
        modal = TrackerItemModal(
            item["item_type"], guild_id, str(interaction.user.id), item_number=self.item_number,
            initial_title=item["title"], initial_description=item["description"],
            initial_details=item.get("details") or "", initial_environment=item.get("environment") or "",
        )
        await interaction.response.send_modal(modal)

    async def _handle_add_files(self, interaction: discord.Interaction, item: Dict[str, Any]) -> None:
        if not await _check_reporter_or_admin(interaction, item):
            return
        user_id = str(interaction.user.id)
        guild_id = interaction.guild.id if interaction.guild else None
        await interaction.response.send_message(
            t('ui_components.tracker.upload_window_prompt', user_id=user_id, guild_id=guild_id), ephemeral=True
        )
        item_number = self.item_number

        async def _on_files(pending: List[Dict[str, Any]]) -> None:
            from qapbot.cache_manager import CACHE
            existing = await CACHE.db_manager.get_tracker_attachments(item_number)
            room = MAX_ATTACHMENTS_PER_ITEM - len(existing)
            total = sum(a["size_bytes"] for a in existing)
            next_index = len(existing) + 1
            for p in pending[:max(room, 0)]:
                if total + p["size_bytes"] > MAX_TOTAL_ATTACHMENT_BYTES:
                    break
                await _persist_attachment(item_number, next_index, p)
                total += p["size_bytes"]
                next_index += 1
            refreshed = await CACHE.db_manager.get_tracker_item(item_number)
            if refreshed is not None:
                await _refresh_item_message(refreshed)

        _register_upload_window(interaction.user.id, interaction.channel.id if interaction.channel else 0, _on_files)

    async def _handle_status(self, interaction: discord.Interaction, item: Dict[str, Any]) -> None:
        from qapbot.config import CONFIG
        from qapbot.QBdiscocmdshelper import check_bot_admin_only

        user_id = str(interaction.user.id)
        guild_id = interaction.guild.id if interaction.guild else None
        if not check_bot_admin_only(interaction, CONFIG.server_admin):
            await interaction.response.send_message(
                t('ui_components.tracker.status_denied', user_id=user_id, guild_id=guild_id), ephemeral=True
            )
            return
        view = TrackerStatusSelectView(self.item_number, item["status"], user_id, guild_id)
        await interaction.response.send_message(view=view, ephemeral=True)

    async def _handle_testcases(self, interaction: discord.Interaction, item: Dict[str, Any]) -> None:
        user_id = str(interaction.user.id)
        guild_id = interaction.guild.id if interaction.guild else None
        if not item.get("test_message_id") or not item.get("test_channel_id"):
            await interaction.response.send_message(
                t('ui_components.tracker.no_test_cases_yet', user_id=user_id, guild_id=guild_id), ephemeral=True
            )
            return
        jump_link = f"https://discord.com/channels/{item['guild_id'] or '@me'}/{item['test_channel_id']}/{item['test_message_id']}"
        await interaction.response.send_message(
            t('ui_components.tracker.test_cases_jump', user_id=user_id, guild_id=guild_id, jump_link=jump_link), ephemeral=True
        )


class TrackerStatusSelectView(discord.ui.View):
    """Admin-only status dropdown opened by the Status button (plan §2.3, §4.2)."""

    def __init__(self, item_number: int, current_status: str, user_id: str, guild_id: Optional[int]):
        super().__init__(timeout=120)
        self.item_number = item_number
        self.user_id = user_id
        self.guild_id = guild_id
        options = [
            discord.SelectOption(
                label=t(f'ui_components.tracker.status_{s}', user_id=user_id, guild_id=guild_id),
                value=s, default=(s == current_status),
            )
            for s in STATUS_VALUES
        ]
        select = discord.ui.Select(options=options, custom_id=f"tracker_status_select_{item_number}")
        select.callback = self._on_select  # type: ignore[assignment]
        self.add_item(select)  # type: ignore[arg-type]

    async def _on_select(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=False, ephemeral=True)
        new_status = interaction.data['values'][0]  # type: ignore[index]
        actor_id = str(interaction.user.id)
        await apply_status_change(self.item_number, new_status, actor_id=actor_id)
        await interaction.followup.send(
            t(
                'ui_components.tracker.status_changed_note', user_id=self.user_id, guild_id=self.guild_id,
                status=t(f'ui_components.tracker.status_{new_status}', user_id=self.user_id, guild_id=self.guild_id),
                actor_id=actor_id,
            ),
            ephemeral=True,
        )


# ===========================================================================
# Test-case loop (plan §2.4)
# ===========================================================================

def _format_testcase_message(item: Dict[str, Any], testcases: List[Dict[str, Any]]) -> str:
    guild_id = _lang_guild_id(item)
    # Same status badge (emoji + text) as build_tracker_embed()'s item title, so the test-case
    # message shows the same visible "done" indicator once the item is actually done — not just
    # a wall of checked boxes with no link back to the item's own status.
    status_text = t(f'ui_components.tracker.status_{item["status"]}', guild_id=guild_id)
    header = t('ui_components.tracker.testcase_message_header', guild_id=guild_id, item_number=f"{item['item_number']:04d}", title=item["title"])
    lines = [
        f"{header}  [{status_text}]",
        "",
    ]
    by_env: Dict[str, List[Dict[str, Any]]] = {}
    for case in testcases:
        by_env.setdefault(case["environment"], []).append(case)
    for env in sorted(by_env):
        lines.append(f"**{env}**")
        for case in by_env[env]:
            box = "☑" if case["passed"] else "☐"
            lines.append(f"{box} {case['seq']}. {case['description']}")
        lines.append("")
    lines.append(t('ui_components.tracker.testcase_signoff_hint', guild_id=guild_id))
    return "\n".join(lines)


def build_tracker_testcase_view(item_number: int, testcases: List[Dict[str, Any]]) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    environments = sorted({c["environment"] for c in testcases})
    for env in environments:
        view.add_item(TrackerTestPassButton(item_number, env))
    view.add_item(TrackerTestFailButton(item_number))
    return view


async def post_test_cases(item_number: int, cases: List[Dict[str, str]], actor_id: Optional[str] = None) -> Dict[str, Any]:
    """Persist *cases* ({environment, description}) and (re)post the test-case message to
    #qapbot-test, transitioning the item to 'testing' the moment it posts (plan §2.4 point 4)
    unless it's already testing/done. Shared by the Discord flow and the bridge/MCP
    `tracker_add_testcases` tool — there is no native Discord UI for *composing* test cases,
    only for signing them off (plan §2.3's "Test cases" button is a jump link, not an editor)."""
    from qapbot.cache_manager import CACHE
    import QBcore

    db = CACHE.db_manager
    item = await db.get_tracker_item(item_number)
    if item is None:
        raise ValueError(f"tracker item #{item_number} not found")

    await db.set_tracker_testcases(item_number, cases)
    testcases = await db.get_tracker_testcases(item_number)

    channel_id_str = item.get("test_channel_id") or CACHE.tracker_settings.get(TRACKER_SETTING_TEST_CHANNEL)
    if not channel_id_str:
        raise ValueError("no test channel configured — run /admin -> Bot Setup first")

    channel = QBcore.bot.get_channel(int(channel_id_str))
    if channel is None:
        channel = await QBcore.bot.fetch_channel(int(channel_id_str))

    content = _format_testcase_message(item, testcases)
    view = build_tracker_testcase_view(item_number, testcases)

    message = None
    if item.get("test_message_id"):
        try:
            message = await channel.fetch_message(int(item["test_message_id"]))  # type: ignore[union-attr]
            await message.edit(content=content, view=view)
        except discord.NotFound:
            message = None
    if message is None:
        message = await channel.send(content, view=view)  # type: ignore[union-attr]

    fields: Dict[str, Any] = {"test_channel_id": str(channel.id), "test_message_id": str(message.id)}
    if item["status"] not in ("testing", "done"):
        fields["status"] = "testing"
    if actor_id:
        fields["last_edited_by"] = actor_id
        fields["last_edited_at"] = _now_iso()
    await db.update_tracker_item(item_number, **fields)
    item = await db.get_tracker_item(item_number)
    assert item is not None
    await _refresh_item_message(item)
    return item


async def _move_test_message_to_done_testing_channel(item: Dict[str, Any]) -> bool:
    """Once an item reaches `done`, repost its test-case message into the configured Done
    Testing channel with no Pass/Fail buttons (nothing left to sign off) and delete the old
    copy. Returns False (no-op) if no test-case message exists yet, the Done Testing channel
    isn't configured, or it's already posted there; never raises."""
    from qapbot.cache_manager import CACHE
    import QBcore

    if not item.get("test_channel_id") or not item.get("test_message_id"):
        return False
    item_number = item["item_number"]
    channel_id_str = CACHE.tracker_settings.get(TRACKER_SETTING_DONE_TESTING_CHANNEL)
    if not channel_id_str or item.get("test_channel_id") == channel_id_str:
        return False

    new_channel = QBcore.bot.get_channel(int(channel_id_str))
    if new_channel is None:
        try:
            new_channel = await QBcore.bot.fetch_channel(int(channel_id_str))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            logging.error(f"[TRACKER] Could not resolve Done Testing channel for item #{item_number}: {e}")
            return False

    testcases = await CACHE.db_manager.get_tracker_testcases(item_number)
    content = _format_testcase_message(item, testcases)
    try:
        new_message = await new_channel.send(content)  # type: ignore[union-attr]  # no view= -> no buttons
    except Exception as e:
        logging.error(f"[TRACKER] Failed to post test cases for item #{item_number} to Done Testing channel: {e}")
        return False

    old_channel_id, old_message_id = item.get("test_channel_id"), item.get("test_message_id")
    try:
        old_channel = QBcore.bot.get_channel(int(old_channel_id)) or await QBcore.bot.fetch_channel(int(old_channel_id))
        old_message = await old_channel.fetch_message(int(old_message_id))  # type: ignore[union-attr]
        await old_message.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        logging.warning(f"[TRACKER] Failed to delete original test-case message for item #{item_number} after move: {e}")

    await CACHE.db_manager.update_tracker_item(
        item_number, test_channel_id=str(new_channel.id), test_message_id=str(new_message.id)
    )
    return True


async def _refresh_testcase_message(item_number: int) -> None:
    from qapbot.cache_manager import CACHE
    import QBcore

    db = CACHE.db_manager
    item = await db.get_tracker_item(item_number)
    if item is None or not item.get("test_channel_id") or not item.get("test_message_id"):
        return
    channel = QBcore.bot.get_channel(int(item["test_channel_id"]))
    if channel is None:
        try:
            channel = await QBcore.bot.fetch_channel(int(item["test_channel_id"]))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
    try:
        message = await channel.fetch_message(int(item["test_message_id"]))  # type: ignore[union-attr]
    except discord.NotFound:
        return
    testcases = await db.get_tracker_testcases(item_number)
    try:
        await message.edit(content=_format_testcase_message(item, testcases), view=build_tracker_testcase_view(item_number, testcases))
    except Exception as e:
        logging.warning(f"[TRACKER] Failed to refresh test-case message for item #{item_number}: {e}")


async def mark_environment_passed_and_refresh(item_number: int, environment: str, actor_id: str) -> None:
    """Sign off every pending test case for one environment; auto-transitions to 'done' the
    moment every environment that HAS test cases is fully passed (plan §2.4 point 4 — "required"
    = every environment with at least one row, so an item with only DEV cases never waits on
    PROD)."""
    from qapbot.cache_manager import CACHE

    db = CACHE.db_manager
    await db.mark_tracker_environment_passed(item_number, environment, actor_id)
    await _refresh_testcase_message(item_number)

    testcases = await db.get_tracker_testcases(item_number)
    if testcases and all(c["passed"] for c in testcases):
        item = await db.get_tracker_item(item_number)
        if item is not None and item["status"] != "done":
            await apply_status_change(item_number, "done", actor_id=actor_id)


async def mark_testing_failed(item_number: int, actor_id: str) -> None:
    """❌ Failed: revert to in_progress; already-passed environments keep their sign-off (never
    reset), and a note lands in the discussion thread (plan §8.10)."""
    item = await apply_status_change(item_number, "in_progress", actor_id=actor_id)
    await _refresh_testcase_message(item_number)
    if item.get("thread_id"):
        import QBcore
        try:
            thread = QBcore.bot.get_channel(int(item["thread_id"])) or await QBcore.bot.fetch_channel(int(item["thread_id"]))
            note = t('ui_components.tracker.testcase_fail_note', guild_id=_lang_guild_id(item), actor_id=actor_id)
            await thread.send(note)  # type: ignore[union-attr]
        except Exception as e:
            logging.warning(f"[TRACKER] Failed to post fail-note into thread for item #{item_number}: {e}")


TRACKER_TEST_PASS_TEMPLATE = r'^tracker:test:pass:(?P<item_number>\d+):(?P<environment>DEV|PROD)$'
TRACKER_TEST_FAIL_TEMPLATE = r'^tracker:test:fail:(?P<item_number>\d+)$'


class TrackerTestPassButton(
    discord.ui.DynamicItem[discord.ui.Button],  # type: ignore[type-arg]
    template=TRACKER_TEST_PASS_TEMPLATE,
):
    """`[ ✅ DEV passed ]` / `[ ✅ PROD passed ]` — bot-admin only (plan §2.4, §8.2)."""

    def __init__(self, item_number: int, environment: str):
        self.item_number = item_number
        self.environment = environment
        super().__init__(
            discord.ui.Button(
                label=t('ui_components.tracker.testcase_button_passed', environment=environment),
                style=discord.ButtonStyle.success,
                custom_id=f"tracker:test:pass:{item_number}:{environment}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: 're.Match[str]', /
    ) -> 'TrackerTestPassButton':
        return cls(item_number=int(match["item_number"]), environment=match["environment"])

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        import QBcore
        if not getattr(QBcore.bot, 'fully_initialized', False):
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.response.send_message(_startup_gate_message(guild_id), ephemeral=True)
            return False
        return True

    async def callback(self, interaction: discord.Interaction) -> None:
        from qapbot.config import CONFIG
        from qapbot.QBdiscocmdshelper import check_bot_admin_only

        user_id = str(interaction.user.id)
        guild_id = interaction.guild.id if interaction.guild else None
        if not check_bot_admin_only(interaction, CONFIG.server_admin):
            await interaction.response.send_message(
                t('ui_components.tracker.testcase_signoff_denied', user_id=user_id, guild_id=guild_id), ephemeral=True
            )
            return
        await interaction.response.defer(thinking=False, ephemeral=True)
        await mark_environment_passed_and_refresh(self.item_number, self.environment, user_id)


class TrackerTestFailButton(
    discord.ui.DynamicItem[discord.ui.Button],  # type: ignore[type-arg]
    template=TRACKER_TEST_FAIL_TEMPLATE,
):
    """`[ ❌ Failed ]` — bot-admin only (plan §2.4, §8.2, §8.10)."""

    def __init__(self, item_number: int):
        self.item_number = item_number
        super().__init__(
            discord.ui.Button(
                label=t('ui_components.tracker.testcase_button_failed'),
                style=discord.ButtonStyle.danger,
                custom_id=f"tracker:test:fail:{item_number}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: 're.Match[str]', /
    ) -> 'TrackerTestFailButton':
        return cls(item_number=int(match["item_number"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        import QBcore
        if not getattr(QBcore.bot, 'fully_initialized', False):
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.response.send_message(_startup_gate_message(guild_id), ephemeral=True)
            return False
        return True

    async def callback(self, interaction: discord.Interaction) -> None:
        from qapbot.config import CONFIG
        from qapbot.QBdiscocmdshelper import check_bot_admin_only

        user_id = str(interaction.user.id)
        guild_id = interaction.guild.id if interaction.guild else None
        if not check_bot_admin_only(interaction, CONFIG.server_admin):
            await interaction.response.send_message(
                t('ui_components.tracker.testcase_signoff_denied', user_id=user_id, guild_id=guild_id), ephemeral=True
            )
            return
        await interaction.response.defer(thinking=False, ephemeral=True)
        await mark_testing_failed(self.item_number, user_id)


async def handle_tracker_test_reaction(payload: discord.RawReactionActionEvent) -> None:
    """👍 on a test-case message marks every still-pending environment passed at once (plan
    §2.4 point 3) — a new on_raw_reaction_add listener (the bot had none before this, plan
    §5.4). Raw (not cached) so it works after restarts. Bot-admin only (plan §8.2); anyone
    else's reaction is silently skipped (never removed — that would need an extra permission
    the bot may not have, and isn't worth the noise)."""
    if payload.emoji.name != "👍" or payload.user_id is None:
        return
    from qapbot.cache_manager import CACHE
    from qapbot.config import CONFIG

    if not CONFIG.server_admin or str(payload.user_id) != CONFIG.server_admin:
        return
    db = CACHE.db_manager
    if db is None:
        return

    item = await db.get_tracker_item_by_test_message_id(str(payload.message_id))
    if item is None:
        return

    testcases = await db.get_tracker_testcases(item["item_number"])
    pending_envs = sorted({c["environment"] for c in testcases if not c["passed"]})
    actor_id = str(payload.user_id)
    for env in pending_envs:
        await db.mark_tracker_environment_passed(item["item_number"], env, actor_id)
    await _refresh_testcase_message(item["item_number"])

    testcases = await db.get_tracker_testcases(item["item_number"])
    if testcases and all(c["passed"] for c in testcases):
        refreshed = await db.get_tracker_item(item["item_number"])
        if refreshed is not None and refreshed["status"] != "done":
            await apply_status_change(item["item_number"], "done", actor_id=actor_id)
