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


# Priority selector — bugs, features, and (per test case row) test cases (tracker item request
# 2026-08-22). MEDIUM is the default; an unrecognized/legacy-empty value normalizes to it.
PRIORITY_VALUES: Tuple[str, str, str] = ("HIGH", "MEDIUM", "LOW")
DEFAULT_PRIORITY = "MEDIUM"
_PRIORITY_LABEL_KEYS = {
    "HIGH": 'ui_components.tracker.priority_high',
    "MEDIUM": 'ui_components.tracker.priority_medium',
    "LOW": 'ui_components.tracker.priority_low',
}
PRIORITY_EMOJI = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}


def _normalize_priority(value: Optional[str]) -> str:
    key = (value or "").strip().upper()
    return key if key in PRIORITY_VALUES else DEFAULT_PRIORITY

# Attachment limits (plan §5.5)
MAX_ATTACHMENTS_PER_ITEM = 5
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 20 * 1024 * 1024
UPLOAD_WINDOW_SECONDS = 300


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
    # Label-wrapped RadioGroup (discord.py 2.7+ Components V2) — see CODE_STRUCTURE.md §
    # Discord.py Patterns "Radio Groups Inside Modals" for why this replaces a plain
    # TextInput/Select here. RadioGroup renders as actual radio buttons (not a dropdown),
    # per the project owner's explicit ask (tracker item request 2026-08-22).
    environment_select = discord.ui.Label(
        text="Environment",
        component=discord.ui.RadioGroup(
            options=[discord.RadioGroupOption(label=v, value=v, default=(v == "PROD")) for v in ENVIRONMENT_VALUES],
        ),
    )
    priority_select = discord.ui.Label(
        text="Priority",
        component=discord.ui.RadioGroup(
            options=[
                discord.RadioGroupOption(label=v, value=v, default=(v == DEFAULT_PRIORITY))
                for v in PRIORITY_VALUES
            ],
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
        initial_priority: str = "",
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
        self._set_priority_options(initial_priority)
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
        self.priority_select.text = t('ui_components.tracker.field_priority', user_id=self.user_id, guild_id=self.guild_id)

    def _set_environment_options(self, initial_environment: str) -> None:
        current = _normalize_environment(initial_environment)
        self.environment_select.component.options = [
            discord.RadioGroupOption(
                label=t(_ENVIRONMENT_LABEL_KEYS[value], user_id=self.user_id, guild_id=self.guild_id),
                value=value, default=(value == current),
            )
            for value in ENVIRONMENT_VALUES
        ]

    def _set_priority_options(self, initial_priority: str) -> None:
        current = _normalize_priority(initial_priority)
        self.priority_select.component.options = [
            discord.RadioGroupOption(
                label=t(_PRIORITY_LABEL_KEYS[value], user_id=self.user_id, guild_id=self.guild_id),
                value=value, default=(value == current),
            )
            for value in PRIORITY_VALUES
        ]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from qapbot.cache_manager import CACHE

        title_val = self.title_input.value.strip()
        description_val = self.description_input.value.strip()
        details_val = (self.details_input.value or "").strip()
        environment_val = self.environment_select.component.value if self.item_type == "bug" else ""
        priority_val = _normalize_priority(self.priority_select.component.value)

        if self.item_number is not None:
            # Editing an already-posted item (Edit button, plan §2.3).
            await interaction.response.defer(thinking=False, ephemeral=True)
            await CACHE.db_manager.update_tracker_item(
                self.item_number,
                title=title_val, description=description_val,
                details=details_val or None,
                environment=(environment_val or None) if self.item_type == "bug" else None,
                priority=priority_val,
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
            self.draft_view.priority_text = priority_val
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
            priority=priority_val,
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
        priority: str = "",
        pending_attachments: Optional[List[Dict[str, Any]]] = None, timeout: int = 900,
    ):
        super().__init__(timeout=timeout)
        self.item_type = item_type
        self.title_text = title
        self.description_text = description
        self.details_text = details
        self.environment_text = environment
        self.priority_text = _normalize_priority(priority)
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
        priority_label = t(_PRIORITY_LABEL_KEYS[self.priority_text], user_id=self.user_id, guild_id=self.guild_id)
        lines += ["", f"**{t('ui_components.tracker.field_priority', user_id=self.user_id, guild_id=self.guild_id)}:** {PRIORITY_EMOJI[self.priority_text]} {priority_label}"]
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
            initial_priority=self.priority_text,
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
            priority=self.priority_text,
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

    modal = TrackerItemModal(item_type, guild_id, user_id)
    await interaction.response.send_modal(modal)

    attachments = [a for a in (attachment1, attachment2, attachment3) if a is not None]
    if attachments:
        modal.predownload_task = asyncio.create_task(_predownload_attachments(attachments))


async def create_tracker_item_for_agent(
    item_type: str,
    title: str,
    description: str,
    details: Optional[str] = None,
    environment: Optional[str] = None,
    priority: str = "MEDIUM",
    reporter_name: str = "agent",
) -> Dict[str, Any]:
    """Agent-facing equivalent of `TrackerDraftView._on_submit()` — no Discord interaction/modal
    involved, since the caller is the bridge (`/api/tracker/items` POST) or the MCP server's
    `tracker_create_item` tool, not a human in Discord (tracker item #0015). Creates the DB row
    and posts it to the configured reports channel exactly like a human-filed `/bug`/`/feature`
    would, just with no attachments and a non-numeric `reporter_id` (`agent:<label>` — never a
    real Discord snowflake, so it can't collide with one; `_dm_reporter_on_status_change()`'s
    `int(reporter_id)` already tolerates any non-numeric value by design, and Edit/status-DM
    features degrade gracefully — a human bot admin can still Edit the posted item in Discord).
    Raises ValueError if the tracker is disabled, unconfigured, or item_type is invalid."""
    from qapbot.cache_manager import CACHE

    if item_type not in ("bug", "feature"):
        raise ValueError(f"item_type must be 'bug' or 'feature', got {item_type!r}")
    if CACHE.tracker_settings.get(TRACKER_SETTING_ENABLED) == "0":
        raise ValueError("tracker is disabled")
    channel_id_str = CACHE.tracker_settings.get(TRACKER_SETTING_BUG_CHANNEL)
    if not channel_id_str:
        raise ValueError("tracker reports channel is not configured")

    normalized_priority = _normalize_priority(priority)
    normalized_environment = _normalize_environment(environment) if item_type == "bug" and environment else None

    db = CACHE.db_manager
    item_number = await db.create_tracker_item(
        item_type=item_type, title=title, description=description,
        reporter_id=f"agent:{reporter_name}", reporter_name=reporter_name,
        details=details or None, environment=normalized_environment, priority=normalized_priority,
    )

    message, _thread = await _post_tracker_item(item_number, int(channel_id_str))
    item = await db.get_tracker_item(item_number)
    assert item is not None
    return {"item": item, "jump_url": message.jump_url if message else None}


# ===========================================================================
# Posted item: embed, view, posting, refresh (plan §2.3)
# ===========================================================================

def build_tracker_embed(item: Dict[str, Any]) -> discord.Embed:
    emoji = ITEM_TYPE_EMOJI.get(item["item_type"], "📝")
    status = item["status"]
    color = STATUS_COLOR.get(status, discord.Color.blue())
    guild_id = _lang_guild_id(item)
    status_text = t(f'ui_components.tracker.status_{status}', guild_id=guild_id)
    priority = _normalize_priority(item.get("priority"))

    embed = discord.Embed(
        title=f"{PRIORITY_EMOJI[priority]} {emoji} #{item['item_number']:04d} · {_truncate(item['title'], 180)}  [{status_text}]",
        color=color,
    )
    reporter_line = t(
        'ui_components.tracker.reported_by', guild_id=guild_id,
        reporter_id=item['reporter_id'], created_at=item['created_at'],
    )
    reporter_line += (
        f"  ·  {t('ui_components.tracker.field_priority', guild_id=guild_id)}: "
        f"{t(_PRIORITY_LABEL_KEYS[priority], guild_id=guild_id)}"
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
    view.add_item(TrackerItemButton("grantaccess", item_number))
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


def _item_jump_link(item: Dict[str, Any]) -> str:
    """Discussion thread if one exists, else the item message itself — the target a reporter
    granted access should land on. Used by _handle_grant_access()'s success response and
    apply_pending_requestor_access()'s post-join DM."""
    if item.get("thread_id"):
        return f"https://discord.com/channels/{item['guild_id'] or '@me'}/{item['thread_id']}"
    return f"https://discord.com/channels/{item['guild_id'] or '@me'}/{item['channel_id']}/{item['message_id']}"


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
    """Once an item reaches a terminal status (`done`, `rejected`, or `duplicate`), repost its
    embed (no buttons — nothing left to do on a closed item) into the configured Implemented
    channel and delete the old copy — keeps the working reports channel from accumulating closed
    items regardless of how they closed. Returns False (no-op, caller should fall back to an
    in-place refresh) if the Implemented channel isn't configured or the item is already posted
    there; never raises (a failed move must not lose the item's DB row, matching
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


async def _revoke_requestor_access(item: Dict[str, Any], channel_id: str) -> None:
    """Undo TrackerItemButton._handle_grant_access()'s channel-wide overwrite once an item
    archives (moves to the Implemented channel) -- unless the same reporter still has another
    open item sitting in that same working channel, in which case revoking now would also cut
    them off from that still-open item. Best-effort: any missing/unresolvable Discord object is
    logged and skipped, never raised, matching this module's other archive-time side effects
    (_move_item_to_implemented_channel, _post_tracker_item)."""
    from qapbot.cache_manager import CACHE
    import QBcore

    reporter_id = item["reporter_id"]
    if not reporter_id.isdigit():
        return

    others = await CACHE.db_manager.list_tracker_items(reporter_id=reporter_id, guild_id=item.get("guild_id"))
    still_open = any(
        o["item_number"] != item["item_number"] and o.get("channel_id") == channel_id
        and o["status"] not in ("done", "rejected", "duplicate")
        for o in others
    )
    if still_open:
        return

    channel = QBcore.bot.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await QBcore.bot.fetch_channel(int(channel_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            logging.warning(f"[TRACKER] Could not resolve channel {channel_id} to revoke requestor access for item #{item['item_number']}: {e}")
            return

    guild_id = item.get("guild_id")
    if not guild_id:
        return
    guild = QBcore.bot.get_guild(int(guild_id))
    if guild is None:
        return
    member = guild.get_member(int(reporter_id))
    if member is None:
        return

    try:
        await channel.set_permissions(  # type: ignore[union-attr]
            member, overwrite=None, reason=f"Tracker #{item['item_number']} closed: revoke requestor access"
        )
    except (discord.Forbidden, discord.HTTPException) as e:
        logging.warning(f"[TRACKER] Failed to revoke requestor access for item #{item['item_number']}: {e}")


async def apply_pending_requestor_access(member: discord.Member) -> None:
    """Called from QapBot.py's on_member_join: finishes what _handle_grant_access()'s
    _invite_requestor() started for a reporter who wasn't a guild member yet — applies the same
    channel overwrite now that they've actually joined, for every still-open item where an admin
    already clicked "Reply to requestor" (access_grant_pending), no second click needed.

    Explicitly gated on CONFIG.tracker_enabled, same reasoning as handle_tracker_test_reaction()
    (BUG_FEATURE_TRACKER.md's architecture section / Pitfall 39): on_member_join is a raw gateway
    event, not a component interaction, so it fires on every bot present in the guild — including
    DEV, whose DB is a routine PROD-backup copy that can contain the exact same pending row."""
    from qapbot.config import CONFIG
    if not CONFIG.tracker_enabled:
        return
    from qapbot.cache_manager import CACHE

    items = await CACHE.db_manager.list_tracker_items(reporter_id=str(member.id), guild_id=str(member.guild.id))
    pending = [
        i for i in items
        if i.get("access_grant_pending") and i["status"] not in ("done", "rejected", "duplicate") and i.get("channel_id")
    ]
    if not pending:
        return

    overwrite = discord.PermissionOverwrite(
        view_channel=True, read_message_history=True, send_messages_in_threads=True,
    )
    for item in pending:
        channel = member.guild.get_channel(int(item["channel_id"]))
        if channel is None:
            try:
                channel = await member.guild.fetch_channel(int(item["channel_id"]))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                logging.warning(f"[TRACKER] Could not resolve channel to finish pending access grant for item #{item['item_number']}: {e}")
                continue
        try:
            await channel.set_permissions(  # type: ignore[union-attr]
                member, overwrite=overwrite,
                reason=f"Tracker #{item['item_number']}: requestor joined, applying pending access",
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            logging.warning(f"[TRACKER] Failed to apply pending requestor access for item #{item['item_number']}: {e}")
            continue

        await CACHE.db_manager.update_tracker_item(item["item_number"], access_grant_pending=0)
        try:
            await member.send(
                t(
                    'ui_components.tracker.grant_access_pending_applied', user_id=str(member.id), guild_id=member.guild.id,
                    item_number=f"{item['item_number']:04d}", jump_link=_item_jump_link(item),
                )
            )
        except discord.Forbidden:
            logging.info(f"[TRACKER] Could not DM {member.id} that pending access for item #{item['item_number']} was applied (closed DMs)")


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

    if new_status in ("done", "rejected", "duplicate"):
        # Move on any terminal status, item side only (tracker item #0015 follow-up, 2026-08-22,
        # widened 2026-08-22 to also cover rejected/duplicate — spec gap found live: those two
        # are just as terminal as done, but were falling into the plain _refresh_item_message()
        # branch below and lingering in the working reports channel forever instead of being
        # archived like everything else that's finished). The test-case message is archived
        # independently, via its OWN completion event (finalize_testcases_move(), called from
        # mark_environment_passed_and_refresh(), the 👍-reaction shortcut, or the manual "Move to
        # Done" action) — never as a side effect of the item reaching a terminal status, and vice
        # versa. Each object moves on its own trigger only.
        original_channel_id = item.get("channel_id")
        moved = await _move_item_to_implemented_channel(item)
        if not moved:
            implemented_channel_id = CACHE.tracker_settings.get(TRACKER_SETTING_IMPLEMENTED_CHANNEL)
            already_archived = bool(implemented_channel_id) and item.get("channel_id") == implemented_channel_id
            await _refresh_item_message(item, archived=already_archived)
        item = await db.get_tracker_item(item_number)
        assert item is not None
        if moved and original_channel_id:
            await _revoke_requestor_access(item, original_channel_id)
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


async def get_thread_messages(item_number: int, limit: int = 50) -> List[Dict[str, Any]]:
    """Fetch an item's discussion thread history, oldest first (2026-08-22 — until now
    `post_comment`/`tracker_comment` could only WRITE into the thread; nothing ever read it back,
    so an agent had no way to see a human's replies, clarifications, or the fail-note the testing
    loop posts there). `_post_tracker_item` seeds the thread with the item's own untruncated
    title/description/details as its very first message(s) (plan §2.3's embed-overflow strategy),
    so this also doubles as a way to read a long report the item embed itself truncated.

    Returns [] rather than raising when the item simply has no thread yet -- best-effort thread
    creation in _post_tracker_item can fail, or the item predates the thread feature -- matching
    the rest of this module's "missing optional Discord object" tolerance. Raises ValueError only
    when item_number itself doesn't exist, mirroring post_comment/apply_status_change.
    """
    from qapbot.cache_manager import CACHE
    import QBcore

    item = await CACHE.db_manager.get_tracker_item(item_number)
    if item is None:
        raise ValueError(f"tracker item #{item_number} not found")
    if not item.get("thread_id"):
        return []

    thread = QBcore.bot.get_channel(int(item["thread_id"]))
    if thread is None:
        try:
            thread = await QBcore.bot.fetch_channel(int(item["thread_id"]))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            logging.warning(f"[TRACKER] Could not resolve discussion thread for item #{item_number}: {e}")
            return []

    try:
        history = [msg async for msg in thread.history(limit=limit)]  # type: ignore[union-attr]
    except (discord.Forbidden, discord.HTTPException) as e:
        logging.warning(f"[TRACKER] Could not read discussion thread history for item #{item_number}: {e}")
        return []
    history.reverse()  # Discord's history() is newest-first; a conversation reads oldest-first.
    return [
        {
            "author_id": str(msg.author.id),
            "author_name": msg.author.display_name,
            "is_bot": bool(msg.author.bot),
            "content": msg.content,
            "created_at": msg.created_at.isoformat(),
        }
        for msg in history
    ]


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


TRACKER_ITEM_BUTTON_TEMPLATE = r'^tracker:(?P<action>edit|addfiles|status|testcases|grantaccess):(?P<item_number>\d+)$'


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
        "grantaccess": 'ui_components.tracker.button_grant_access',
    }
    _STYLES = {
        "edit": discord.ButtonStyle.secondary,
        "addfiles": discord.ButtonStyle.secondary,
        "status": discord.ButtonStyle.primary,
        "testcases": discord.ButtonStyle.secondary,
        "grantaccess": discord.ButtonStyle.secondary,
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
        elif self.action == "grantaccess":
            await self._handle_grant_access(interaction, item)

    async def _handle_edit(self, interaction: discord.Interaction, item: Dict[str, Any]) -> None:
        if not await _check_reporter_or_admin(interaction, item):
            return
        guild_id = interaction.guild.id if interaction.guild else None
        modal = TrackerItemModal(
            item["item_type"], guild_id, str(interaction.user.id), item_number=self.item_number,
            initial_title=item["title"], initial_description=item["description"],
            initial_details=item.get("details") or "", initial_environment=item.get("environment") or "",
            initial_priority=item.get("priority") or "",
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

    async def _handle_grant_access(self, interaction: discord.Interaction, item: Dict[str, Any]) -> None:
        """Admin-only: give the reporter a channel-wide view/read overwrite so they can see this
        item and its discussion thread, and reply inside threads — the trade-off the user chose
        over a per-thread-scoped private thread (BUG_FEATURE_TRACKER.md's grant/revoke section).
        Reverted by _revoke_requestor_access() once the item archives. If the reporter isn't a
        guild member (the common case — most reporters file via DM and never join), falls back
        to _invite_requestor() instead of failing outright."""
        from qapbot.config import CONFIG
        from qapbot.QBdiscocmdshelper import check_bot_admin_only

        user_id = str(interaction.user.id)
        guild_id = interaction.guild.id if interaction.guild else None
        if not check_bot_admin_only(interaction, CONFIG.server_admin):
            await interaction.response.send_message(
                t('ui_components.tracker.grant_access_denied', user_id=user_id, guild_id=guild_id), ephemeral=True
            )
            return

        reporter_id = item["reporter_id"]
        if not reporter_id.isdigit():
            await interaction.response.send_message(
                t('ui_components.tracker.grant_access_no_user', user_id=user_id, guild_id=guild_id), ephemeral=True
            )
            return

        member = interaction.guild.get_member(int(reporter_id)) if interaction.guild else None
        if member is None and interaction.guild is not None:
            try:
                member = await interaction.guild.fetch_member(int(reporter_id))
            except discord.NotFound:
                member = None
        if member is None:
            await self._invite_requestor(interaction, item, reporter_id)
            return

        overwrite = discord.PermissionOverwrite(
            view_channel=True, read_message_history=True, send_messages_in_threads=True,
        )
        try:
            await interaction.channel.set_permissions(  # type: ignore[union-attr]
                member, overwrite=overwrite, reason=f"Tracker #{self.item_number}: grant requestor access to reply"
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            logging.warning(f"[TRACKER] Failed to grant requestor access for item #{self.item_number}: {e}")
            await interaction.response.send_message(
                t('ui_components.tracker.grant_access_failed', user_id=user_id, guild_id=guild_id), ephemeral=True
            )
            return

        await interaction.response.send_message(
            t(
                'ui_components.tracker.grant_access_granted', user_id=user_id, guild_id=guild_id,
                reporter_id=reporter_id, jump_link=_item_jump_link(item),
            ),
            ephemeral=True,
        )

    async def _invite_requestor(self, interaction: discord.Interaction, item: Dict[str, Any], reporter_id: str) -> None:
        """Reporter isn't a guild member — DM them a single-use invite instead of just failing,
        and mark access_grant_pending so apply_pending_requestor_access() (on_member_join) can
        finish the grant automatically once they actually join, with no second click needed."""
        import QBcore

        user_id = str(interaction.user.id)
        guild_id = interaction.guild.id if interaction.guild else None

        reporter_user = QBcore.bot.get_user(int(reporter_id))
        if reporter_user is None:
            try:
                reporter_user = await QBcore.bot.fetch_user(int(reporter_id))
            except discord.NotFound:
                reporter_user = None
        if reporter_user is None:
            await interaction.response.send_message(
                t('ui_components.tracker.grant_access_member_not_found', user_id=user_id, guild_id=guild_id), ephemeral=True
            )
            return

        try:
            invite = await interaction.channel.create_invite(  # type: ignore[union-attr]
                max_age=604800, max_uses=1, unique=True,
                reason=f"Tracker #{self.item_number}: invite requestor to reply",
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            logging.warning(f"[TRACKER] Failed to create invite for item #{self.item_number}: {e}")
            await interaction.response.send_message(
                t('ui_components.tracker.grant_access_invite_failed', user_id=user_id, guild_id=guild_id), ephemeral=True
            )
            return

        from qapbot.cache_manager import CACHE
        await CACHE.db_manager.update_tracker_item(self.item_number, access_grant_pending=1)

        try:
            await reporter_user.send(
                t(
                    'ui_components.tracker.grant_access_invite_dm', user_id=reporter_id, guild_id=guild_id,
                    item_number=f"{self.item_number:04d}", invite_url=invite.url,
                )
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                t(
                    'ui_components.tracker.grant_access_invite_dm_failed', user_id=user_id, guild_id=guild_id,
                    invite_url=invite.url,
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            t('ui_components.tracker.grant_access_invited', user_id=user_id, guild_id=guild_id, invite_url=invite.url),
            ephemeral=True,
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
            priority = _normalize_priority(case.get("priority"))
            lines.append(f"{box} {PRIORITY_EMOJI[priority]} {case['seq']}. {case['description']}")
        lines.append("")
    lines.append(t('ui_components.tracker.testcase_signoff_hint', guild_id=guild_id))
    return "\n".join(lines)


def build_tracker_testcase_view(item_number: int, testcases: List[Dict[str, Any]]) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    environments = sorted({c["environment"] for c in testcases})
    for env in environments:
        view.add_item(TrackerTestPassButton(item_number, env))
    view.add_item(TrackerTestFailButton(item_number))
    view.add_item(TrackerTestMoveDoneButton(item_number))
    return view


async def post_test_cases(item_number: int, cases: List[Dict[str, str]], actor_id: Optional[str] = None) -> Dict[str, Any]:
    """Persist *cases* ({environment, description, priority?}) and (re)post the test-case message to
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
    """Re-render the posted test-case message in place. Skips rebuilding the Pass/Fail/Move-to-
    Done view once the message has been archived to the Done Testing channel (2026-08-22, live
    bug report: reacting 👍 a second time on an already-fully-passed, already-archived message
    resurrected its buttons) — _move_test_message_to_done_testing_channel() reposts with no view=
    specifically because there's nothing left to sign off, and updates test_channel_id to that
    channel when it does; unconditionally reattaching a fresh interactive view here on any later
    refresh (the 👍-reaction handler calls this even when nothing was actually pending) undid
    that. Mirrors _refresh_item_message's own `archived` handling for the item embed."""
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
    archived = item["test_channel_id"] == CACHE.tracker_settings.get(TRACKER_SETTING_DONE_TESTING_CHANNEL)
    view = None if archived else build_tracker_testcase_view(item_number, testcases)
    try:
        await message.edit(content=_format_testcase_message(item, testcases), view=view)
    except Exception as e:
        logging.warning(f"[TRACKER] Failed to refresh test-case message for item #{item_number}: {e}")


async def move_testcases_to_done_channel(item_number: int) -> bool:
    """Best-effort archive of one item's test-case message to the Done Testing channel —
    entirely independent of the linked item's own status (tracker item #0015 follow-up,
    2026-08-22: each object moves on its own trigger only). Thin wrapper so bridge/MCP callers
    don't need to fetch the item themselves first."""
    from qapbot.cache_manager import CACHE

    item = await CACHE.db_manager.get_tracker_item(item_number)
    if item is None:
        return False
    return await _move_test_message_to_done_testing_channel(item)


async def get_linked_item_if_eligible_for_done(item_number: int) -> Optional[Dict[str, Any]]:
    """The item linked to a test-case set, or None if it no longer exists or is already in a
    terminal status (done/rejected/duplicate) — the "should we even offer to mark it done too?"
    check shared by every completion path (tracker item #0015 follow-up, 2026-08-22)."""
    from qapbot.cache_manager import CACHE

    item = await CACHE.db_manager.get_tracker_item(item_number)
    if item is None or item["status"] in ("done", "rejected", "duplicate"):
        return None
    return item


async def finalize_testcases_move(item_number: int) -> Dict[str, Any]:
    """Archive the test-case message (independent of the item) and report whether the linked
    item is eligible for a "mark done too?" follow-up. Shared by natural full-completion
    (mark_environment_passed_and_refresh, the 👍-reaction shortcut) and the manual "Move to
    Done" action (TrackerTestMoveDoneButton, the bridge/MCP tool) — one place either path
    actually performs the move."""
    moved = await move_testcases_to_done_channel(item_number)
    linked_item = await get_linked_item_if_eligible_for_done(item_number)
    return {"moved": moved, "linked_item": linked_item}


async def mark_environment_passed_and_refresh(item_number: int, environment: str, actor_id: str) -> Dict[str, Any]:
    """Sign off every pending test case for one environment. No longer touches the linked
    item's status at all (tracker item #0015 follow-up, 2026-08-22 — the two objects are
    archived independently now). Returns {"just_completed", "moved", "linked_item"} — "just_
    completed" is edge-triggered (only True the call that flips the LAST pending row, not on
    every call against an already-fully-passed item) so callers can decide whether to offer
    moving the test-case message and/or prompting to mark the linked item done."""
    from qapbot.cache_manager import CACHE

    db = CACHE.db_manager
    before = await db.get_tracker_testcases(item_number)
    was_fully_passed = bool(before) and all(c["passed"] for c in before)

    await db.mark_tracker_environment_passed(item_number, environment, actor_id)
    await _refresh_testcase_message(item_number)

    after = await db.get_tracker_testcases(item_number)
    now_fully_passed = bool(after) and all(c["passed"] for c in after)

    if now_fully_passed and not was_fully_passed:
        result = await finalize_testcases_move(item_number)
        return {"just_completed": True, **result}
    return {"just_completed": False, "moved": False, "linked_item": None}


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


async def _post_item_done_confirmation_passive(item: Dict[str, Any], actor_id: str) -> None:
    """Non-interactive equivalent of the ephemeral "mark item done too?" prompt, for triggers
    with no live interaction to attach a followup to (the 👍-reaction shortcut). Posts to the
    item's discussion thread if one exists, else DMs the actor — never raises.

    Uses the `_passive` prompt variant, which opens with an `<@actor_id>` mention (2026-08-22,
    live bug report: a plain, unmentioned thread message doesn't highlight/notify anyone in
    Discord — the reactor had no way to know a decision was waiting on them, and the prompt for
    #0015 sat unnoticed). A mention also has the side effect of pulling a non-member actor into
    the thread, and Discord's own mention notification already jumps straight to this exact
    message when clicked — the closest a bot can get to "move the user's view there" since there
    is no API to steer a user's client directly."""
    import QBcore

    guild_id = _lang_guild_id(item)
    text = t(
        'ui_components.tracker.item_done_confirm_prompt_passive', guild_id=guild_id,
        item_number=f"{item['item_number']:04d}", title=item['title'],
        status=t(f'ui_components.tracker.status_{item["status"]}', guild_id=guild_id),
        actor_id=actor_id,
    )
    view = ConfirmItemDoneView(item['item_number'], guild_id)
    if item.get("thread_id"):
        try:
            thread = QBcore.bot.get_channel(int(item["thread_id"])) or await QBcore.bot.fetch_channel(int(item["thread_id"]))
            await thread.send(text, view=view)  # type: ignore[union-attr]
            return
        except Exception as e:
            logging.warning(f"[TRACKER] Failed to post item-done prompt into thread for item #{item['item_number']}: {e}")
    try:
        user = QBcore.bot.get_user(int(actor_id)) or await QBcore.bot.fetch_user(int(actor_id))
        await user.send(text, view=view)  # type: ignore[union-attr]
    except Exception as e:
        logging.warning(f"[TRACKER] Failed to DM item-done prompt to {actor_id} for item #{item['item_number']}: {e}")


class ConfirmItemDoneView(discord.ui.View):
    """Short-lived, session-scoped Yes/No prompt offered once a test-case set's message has
    been archived (naturally or via the manual "Move to Done" action) and the linked item isn't
    already terminal (tracker item #0015 follow-up, 2026-08-22). Not restart-safe by design —
    same convention as TrackerDraftView; a bot restart before anyone answers just lets it
    expire. Works both as an ephemeral followup (Pass button / Move-to-Done button) and as a
    regular channel/thread/DM message (the 👍-reaction shortcut has no interaction to attach an
    ephemeral message to)."""

    def __init__(self, item_number: int, guild_id: Optional[int], timeout: int = 600):
        super().__init__(timeout=timeout)
        self.item_number = item_number
        self.guild_id = guild_id
        yes_btn = discord.ui.Button(
            label=t('ui_components.tracker.item_done_confirm_yes', guild_id=guild_id), style=discord.ButtonStyle.success
        )
        yes_btn.callback = self._on_yes  # type: ignore[assignment]
        self.add_item(yes_btn)
        no_btn = discord.ui.Button(
            label=t('ui_components.tracker.item_done_confirm_no', guild_id=guild_id), style=discord.ButtonStyle.secondary
        )
        no_btn.callback = self._on_no  # type: ignore[assignment]
        self.add_item(no_btn)

    async def _on_yes(self, interaction: discord.Interaction) -> None:
        from qapbot.config import CONFIG
        from qapbot.QBdiscocmdshelper import check_bot_admin_only

        user_id = str(interaction.user.id)
        if not check_bot_admin_only(interaction, CONFIG.server_admin):
            await interaction.response.send_message(
                t('ui_components.tracker.testcase_signoff_denied', user_id=user_id, guild_id=self.guild_id), ephemeral=True
            )
            return
        await interaction.response.defer(thinking=False, ephemeral=True)
        try:
            await apply_status_change(self.item_number, "done", actor_id=user_id)
            await interaction.followup.send(
                t(
                    'ui_components.tracker.item_done_confirm_applied', guild_id=self.guild_id,
                    item_number=f"{self.item_number:04d}",
                ),
                ephemeral=True,
            )
        except ValueError as e:
            await interaction.followup.send(str(e), ephemeral=True)
        self.stop()

    async def _on_no(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=False, ephemeral=True)
        await interaction.followup.send(
            t('ui_components.tracker.item_done_confirm_cancelled', guild_id=self.guild_id), ephemeral=True
        )
        self.stop()


TRACKER_TEST_PASS_TEMPLATE = r'^tracker:test:pass:(?P<item_number>\d+):(?P<environment>DEV|PROD)$'
TRACKER_TEST_FAIL_TEMPLATE = r'^tracker:test:fail:(?P<item_number>\d+)$'
TRACKER_TEST_MOVEDONE_TEMPLATE = r'^tracker:test:movedone:(?P<item_number>\d+)$'



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
        result = await mark_environment_passed_and_refresh(self.item_number, self.environment, user_id)
        if result["just_completed"]:
            await _send_testcases_moved_followup(interaction, self.item_number, result, guild_id)


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


async def _send_testcases_moved_followup(
    interaction: discord.Interaction, item_number: int, result: Dict[str, Any], guild_id: Optional[int]
) -> None:
    """Shared ephemeral followup for both the natural-completion path (Pass button) and the
    manual Move-to-Done action, once a test-case message has just been archived — reports the
    move and, if the linked item isn't already terminal, offers the ConfirmItemDoneView
    (tracker item #0015 follow-up, 2026-08-22)."""
    text = t(
        'ui_components.tracker.testcase_moved_done_confirmed' if result["moved"] else 'ui_components.tracker.testcase_moved_done_no_channel',
        guild_id=guild_id,
    )
    linked_item = result.get("linked_item")
    if linked_item is not None:
        text += "\n" + t(
            'ui_components.tracker.item_done_confirm_prompt', guild_id=guild_id,
            item_number=f"{linked_item['item_number']:04d}", title=linked_item['title'],
            status=t(f'ui_components.tracker.status_{linked_item["status"]}', guild_id=guild_id),
        )
        await interaction.followup.send(text, view=ConfirmItemDoneView(item_number, guild_id), ephemeral=True)
    else:
        await interaction.followup.send(text, ephemeral=True)


class ConfirmForceMoveView(discord.ui.View):
    """Yes/No guard shown by TrackerTestMoveDoneButton when cases are still unchecked (tracker
    item #0015 follow-up, point 4, 2026-08-22). Not restart-safe by design, same as
    ConfirmItemDoneView."""

    def __init__(self, item_number: int, guild_id: Optional[int], timeout: int = 300):
        super().__init__(timeout=timeout)
        self.item_number = item_number
        self.guild_id = guild_id
        yes_btn = discord.ui.Button(
            label=t('ui_components.tracker.testcase_movedone_confirm_yes', guild_id=guild_id), style=discord.ButtonStyle.danger
        )
        yes_btn.callback = self._on_yes  # type: ignore[assignment]
        self.add_item(yes_btn)
        no_btn = discord.ui.Button(
            label=t('ui_components.tracker.testcase_movedone_confirm_no', guild_id=guild_id), style=discord.ButtonStyle.secondary
        )
        no_btn.callback = self._on_no  # type: ignore[assignment]
        self.add_item(no_btn)

    async def _on_yes(self, interaction: discord.Interaction) -> None:
        from qapbot.config import CONFIG
        from qapbot.QBdiscocmdshelper import check_bot_admin_only

        user_id = str(interaction.user.id)
        if not check_bot_admin_only(interaction, CONFIG.server_admin):
            await interaction.response.send_message(
                t('ui_components.tracker.testcase_signoff_denied', user_id=user_id, guild_id=self.guild_id), ephemeral=True
            )
            return
        await interaction.response.defer(thinking=False, ephemeral=True)
        result = await finalize_testcases_move(self.item_number)
        await _send_testcases_moved_followup(interaction, self.item_number, result, self.guild_id)
        self.stop()

    async def _on_no(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=False, ephemeral=True)
        await interaction.followup.send(
            t('ui_components.tracker.testcase_movedone_confirm_cancelled', guild_id=self.guild_id), ephemeral=True
        )
        self.stop()


class TrackerTestMoveDoneButton(
    discord.ui.DynamicItem[discord.ui.Button],  # type: ignore[type-arg]
    template=TRACKER_TEST_MOVEDONE_TEMPLATE,
):
    """`[ 📦 Move to Done ]` — manually archive the test-case message to the Done Testing
    channel, independent of whether every case is checked off or of the linked item's status
    (tracker item #0015 follow-up, point 4, 2026-08-22). Bot-admin only."""

    def __init__(self, item_number: int):
        self.item_number = item_number
        super().__init__(
            discord.ui.Button(
                label=t('ui_components.tracker.testcase_button_movedone'),
                style=discord.ButtonStyle.secondary,
                custom_id=f"tracker:test:movedone:{item_number}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: 're.Match[str]', /
    ) -> 'TrackerTestMoveDoneButton':
        return cls(item_number=int(match["item_number"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        import QBcore
        if not getattr(QBcore.bot, 'fully_initialized', False):
            guild_id = interaction.guild.id if interaction.guild else None
            await interaction.response.send_message(_startup_gate_message(guild_id), ephemeral=True)
            return False
        return True

    async def callback(self, interaction: discord.Interaction) -> None:
        from qapbot.cache_manager import CACHE
        from qapbot.config import CONFIG
        from qapbot.QBdiscocmdshelper import check_bot_admin_only

        user_id = str(interaction.user.id)
        guild_id = interaction.guild.id if interaction.guild else None
        if not check_bot_admin_only(interaction, CONFIG.server_admin):
            await interaction.response.send_message(
                t('ui_components.tracker.testcase_signoff_denied', user_id=user_id, guild_id=guild_id), ephemeral=True
            )
            return

        testcases = await CACHE.db_manager.get_tracker_testcases(self.item_number)
        unchecked = [c for c in testcases if not c["passed"]]
        if unchecked:
            await interaction.response.send_message(
                t(
                    'ui_components.tracker.testcase_movedone_confirm_incomplete', guild_id=guild_id,
                    count=len(unchecked),
                ),
                view=ConfirmForceMoveView(self.item_number, guild_id),
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=False, ephemeral=True)
        result = await finalize_testcases_move(self.item_number)
        await _send_testcases_moved_followup(interaction, self.item_number, result, guild_id)


async def handle_tracker_test_reaction(payload: discord.RawReactionActionEvent) -> None:
    """👍 on a test-case message marks every still-pending environment passed at once (plan
    §2.4 point 3) — a new on_raw_reaction_add listener (the bot had none before this, plan
    §5.4). Raw (not cached) so it works after restarts. Bot-admin only (plan §8.2); anyone
    else's reaction is silently skipped (never removed — that would need an extra permission
    the bot may not have, and isn't worth the noise).

    Explicitly gated on CONFIG.tracker_enabled (2026-08-22 live bug report: DEV reacted to a 👍
    on a PROD-authored test-case message — raw reaction events fire for every bot present in a
    channel, regardless of which bot's message was reacted to, unlike component interactions
    which Discord routes only to the owning application. QapBot.py's own `on_raw_reaction_add`
    registers this listener unconditionally on the wrong assumption that DEV's tracker DB has no
    matching rows to find; in practice DEV's DB is periodically seeded from a PROD backup and
    does have them, so DEV proceeded to mark environments passed in its own DB, fail with a 403
    ("Cannot edit a message authored by another user") refreshing PROD's test-case message, and
    still post its own "mark item done too?" prompt to the thread. tracker_enabled is already
    `not is_dev_mode` (qapbot/config.py) — this is the one function actually reachable from that
    listener, so the guard belongs here rather than relying on the registration site alone."""
    from qapbot.config import CONFIG

    if not CONFIG.tracker_enabled:
        return
    if payload.emoji.name != "👍" or payload.user_id is None:
        return
    from qapbot.cache_manager import CACHE

    if not CONFIG.server_admin or str(payload.user_id) != CONFIG.server_admin:
        return
    db = CACHE.db_manager
    if db is None:
        return

    item = await db.get_tracker_item_by_test_message_id(str(payload.message_id))
    if item is None:
        return
    item_number = item["item_number"]

    before = await db.get_tracker_testcases(item_number)
    was_fully_passed = bool(before) and all(c["passed"] for c in before)
    pending_envs = sorted({c["environment"] for c in before if not c["passed"]})
    if not pending_envs:
        # Nothing left to sign off (already fully passed, e.g. a redundant second 👍 on an
        # already-archived message) — a genuine no-op must not touch the message at all. Also
        # belt-and-suspenders alongside _refresh_testcase_message's own archived check below.
        return
    actor_id = str(payload.user_id)
    for env in pending_envs:
        await db.mark_tracker_environment_passed(item_number, env, actor_id)
    await _refresh_testcase_message(item_number)

    after = await db.get_tracker_testcases(item_number)
    now_fully_passed = bool(after) and all(c["passed"] for c in after)
    if now_fully_passed and not was_fully_passed:
        result = await finalize_testcases_move(item_number)
        linked_item = result.get("linked_item")
        if linked_item is not None:
            await _post_item_done_confirmation_passive(linked_item, actor_id)
