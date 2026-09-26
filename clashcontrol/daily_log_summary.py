"""Daily error/warning summary DM for the bot admin (tracker #0144).

Shortly after midnight (00:05 server time — just after TimedRotatingFileHandler has rotated
yesterday's lines into clashcontrol.log.YYYY-MM-DD) the bot reads yesterday's ERROR/WARNING lines,
groups them by message pattern and DMs a short report to the bot admin (CONFIG.server_admin).

Design points:
  - Not part of nightly maintenance: it only reads log files, needs no DB lock, and runs as its
    own background task (start_daily_log_summary_scheduler()).
  - Bounded DM length whatever happened: identical messages are grouped (numbers, clan/player
    tags, ids normalized away), at most MAX_GROUPS_PER_LEVEL groups per level are listed with a
    count, and the whole report stays under MAX_REPORT_CHARS — a day with 50,000 errors produces
    the same-sized DM as a day with 5.
  - Sent once per day: the last reported day is kept in bot_metadata (METADATA_KEY). After a
    restart the bot catches up on yesterday only — never older days, so a long outage doesn't
    turn into a burst of DMs.
  - PROD by default; DEV only with DAILY_LOG_SUMMARY_ENABLED=true (CONFIG.daily_log_summary_enabled).
"""

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from typing import Dict, List, Optional

import discord

METADATA_KEY = "daily_log_summary_last_day"
RUN_AT = dtime(0, 5)            # local server time, like the log timestamps
MAX_GROUPS_PER_LEVEL = 8
MAX_SAMPLE_CHARS = 140
MAX_REPORT_CHARS = 3900         # embed description limit is 4096
_RETRY_SECONDS = 3600           # re-check at least hourly (e.g. after maintenance mode)

_ENTRY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}):\d{2},\d{3} \[(\w+)\] (.*)$")
_VERSION_RE = re.compile(r"Version:\s+\S+\s+build\s+(\d+)")
# Normalization for grouping — most specific first.
_NORMALIZERS = (
    (re.compile(r"#[0-9A-Z]{4,12}\b"), "#TAG"),           # clan / player tags
    (re.compile(r"\b\d{15,20}\b"), "<id>"),               # Discord snowflakes
    (re.compile(r"0x[0-9a-fA-F]+"), "<hex>"),
    (re.compile(r"\d+(?:[.,]\d+)?"), "N"),
    (re.compile(r"\s+"), " "),
)


@dataclass
class LogGroup:
    """All occurrences of one normalized message at one level."""
    sample: str
    count: int = 0
    first: str = ""
    last: str = ""
    detail: str = ""      # last line of an attached traceback (e.g. "RuntimeError: ...")


@dataclass
class DaySummary:
    day: date
    errors: Dict[str, LogGroup] = field(default_factory=dict)
    warnings: Dict[str, LogGroup] = field(default_factory=dict)
    starts: int = 0
    last_build: Optional[str] = None
    lines_seen: int = 0

    @property
    def error_count(self) -> int:
        return sum(g.count for g in self.errors.values())

    @property
    def warning_count(self) -> int:
        return sum(g.count for g in self.warnings.values())


def _normalize(message: str) -> str:
    for pattern, repl in _NORMALIZERS:
        message = pattern.sub(repl, message)
    return message.strip()[:200]


def _candidate_files(log_dir: str, day: date) -> List[str]:
    """Files that can hold `day`'s lines: its own rotation, the next day's (if the midnight
    rollover happened late) and the live file (if no rollover happened at all)."""
    from clashcontrol.QBdiscocmdshelper_admin_command import LOG_BASENAME
    names = [f"{LOG_BASENAME}.{day.isoformat()}", f"{LOG_BASENAME}.{(day + timedelta(days=1)).isoformat()}", LOG_BASENAME]
    return [os.path.join(log_dir, n) for n in names if os.path.isfile(os.path.join(log_dir, n))]


def summarize_log_day(log_dir: str, day: date, files: Optional[List[str]] = None) -> DaySummary:
    """Group `day`'s ERROR/CRITICAL and WARNING lines (blocking — call via asyncio.to_thread()).

    Streams the files line by line (a PROD day is ~50 MB), keeps only lines stamped with `day`,
    and attaches a traceback's last line to the ERROR entry it follows.

    Args:
        log_dir: The logs directory (CONFIG.data_dir/logs).
        day: The calendar day (server time) to summarize.
        files: Explicit files to read instead of the rotation candidates (tests).
    """
    summary = DaySummary(day=day)
    wanted = day.isoformat()
    for path in files if files is not None else _candidate_files(log_dir, day):
        current: Optional[LogGroup] = None   # the ERROR group a following traceback belongs to
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    m = _ENTRY_RE.match(raw)
                    if m is None:
                        # Continuation line (traceback) — remember its last meaningful line.
                        if current is not None and raw.strip() and not raw.startswith((" ", "\t", "Traceback")):
                            current.detail = raw.strip()[:MAX_SAMPLE_CHARS]
                        continue
                    current = None
                    d, hhmm, level, message = m.groups()
                    if d != wanted:
                        continue
                    summary.lines_seen += 1
                    if level == "INFO" and "Version: " in message:
                        summary.starts += 1
                        vm = _VERSION_RE.search(message)
                        if vm:
                            summary.last_build = vm.group(1)
                        continue
                    if level in ("ERROR", "CRITICAL"):
                        bucket = summary.errors
                    elif level == "WARNING":
                        bucket = summary.warnings
                    else:
                        continue
                    key = _normalize(message)
                    group = bucket.get(key)
                    if group is None:
                        group = bucket[key] = LogGroup(sample=message.strip()[:MAX_SAMPLE_CHARS], first=hhmm)
                    group.count += 1
                    group.last = hhmm
                    if level != "WARNING":
                        current = group
        except OSError as exc:
            logging.warning(f"[DAILY-LOG-SUMMARY] could not read {path}: {exc}")
    return summary


def build_summary_embed(summary: DaySummary, env_label: str, user_id: Optional[str]) -> discord.Embed:
    """The DM: totals, restarts and the most frequent groups per level, always <= MAX_REPORT_CHARS.

    Args:
        summary: summarize_log_day() result.
        env_label: "PROD" or "DEV".
        user_id: Recipient (the bot admin) for the language lookup.
    """
    from clashcontrol.i18n import t  # type: ignore[attr-defined]

    def tr(key: str, **kwargs: object) -> str:
        return t(f'notifications.daily_log_summary.{key}', user_id=user_id, **kwargs)

    head: List[str] = []
    if summary.lines_seen == 0:
        head.append(tr('no_log', day=summary.day.isoformat()))
    else:
        head.append(tr('totals', errors=summary.error_count, error_groups=len(summary.errors),
                       warnings=summary.warning_count, warning_groups=len(summary.warnings)))
        if summary.starts:
            head.append(tr('restarts', count=summary.starts, build=summary.last_build or "?"))
        if not summary.errors and not summary.warnings:
            head.append(tr('clean_day'))

    def section(title_key: str, groups: Dict[str, LogGroup]) -> List[str]:
        if not groups:
            return []
        ranked = sorted(groups.values(), key=lambda g: (-g.count, g.first))
        lines = ["", f"**{tr(title_key)}**"]
        for g in ranked[:MAX_GROUPS_PER_LEVEL]:
            span = g.first if g.first == g.last else f"{g.first}–{g.last}"
            text = discord.utils.escape_markdown(g.sample)
            line = f"`{g.count}×` {span} {text}"
            if g.detail and g.detail not in g.sample:
                line += f"\n   ↳ {discord.utils.escape_markdown(g.detail)}"
            lines.append(line)
        if len(ranked) > MAX_GROUPS_PER_LEVEL:
            lines.append(tr('more_groups', count=len(ranked) - MAX_GROUPS_PER_LEVEL))
        return lines

    body = head + section('errors_title', summary.errors) + section('warnings_title', summary.warnings)
    # Hard cap: drop lines from the end (warnings go first) until it fits.
    text = "\n".join(body)
    while len(text) > MAX_REPORT_CHARS and len(body) > len(head):
        body.pop()
        text = "\n".join(body + ["…"])
    embed = discord.Embed(
        title=tr('title', day=summary.day.isoformat(), env=env_label)[:256],
        description=text[:4096],
        color=discord.Color.red() if summary.errors else (discord.Color.orange() if summary.warnings else discord.Color.green()),
    )
    embed.set_footer(text=tr('footer', file=f"clashcontrol.log.{summary.day.isoformat()}"))
    return embed


def _admin_user_id() -> Optional[str]:
    from clashcontrol.config import CONFIG
    admin = str(CONFIG.server_admin or "").strip()
    return admin if admin.isdigit() else None


def due_day(now: datetime) -> date:
    """The day a report is due for at `now` (server time): yesterday once RUN_AT has passed,
    otherwise the day before (yesterday's lines may still be rotating)."""
    today = now.date()
    return today - timedelta(days=1 if now.time() >= RUN_AT else 2)


async def send_due_summary(log_dir: str, now: Optional[datetime] = None) -> Optional[str]:
    """Send the report for due_day() unless it was already sent (bot_metadata marker).

    Returns the outcome for logging/tests: "sent", "already_sent", "no_admin", "maintenance",
    "dm_failed" — or None when the DB isn't available.
    """
    import QBcore
    from clashcontrol.cache_manager import CACHE
    from clashcontrol.config import CONFIG

    if getattr(QBcore, "maintenance_mode", False) or CACHE.db_manager is None:
        return "maintenance"
    day = due_day(now or datetime.now())
    last = await CACHE.db_manager.get_bot_metadata(METADATA_KEY)
    if last and last >= day.isoformat():
        return "already_sent"
    admin_id = _admin_user_id()
    if admin_id is None:
        logging.warning("[DAILY-LOG-SUMMARY] CONFIG.server_admin is not a numeric user id — no report sent")
        await CACHE.db_manager.set_bot_metadata(METADATA_KEY, day.isoformat())
        return "no_admin"

    summary = await asyncio.to_thread(summarize_log_day, log_dir, day)
    embed = build_summary_embed(summary, "DEV" if CONFIG.is_dev_mode else "PROD", admin_id)
    sent = await CACHE.send_user_dm(admin_id, "", embed=embed)
    # Marked as done even if the DM failed (admin blocks DMs): retrying every hour would only
    # repeat the same failure; the log line below records it.
    await CACHE.db_manager.set_bot_metadata(METADATA_KEY, day.isoformat())
    logging.info(
        f"[DAILY-LOG-SUMMARY] {day}: errors={summary.error_count} ({len(summary.errors)} distinct), "
        f"warnings={summary.warning_count} ({len(summary.warnings)} distinct), starts={summary.starts} "
        f"-> {'sent' if sent else 'DM failed'}"
    )
    return "sent" if sent else "dm_failed"


def _seconds_until_next_run(now: datetime) -> float:
    run_today = datetime.combine(now.date(), RUN_AT)
    next_run = run_today if now < run_today else run_today + timedelta(days=1)
    return max((next_run - now).total_seconds(), 1.0)


async def _scheduler_loop(log_dir: str) -> None:
    from clashcontrol.db_manager import DatabaseMaintenanceError
    while True:
        try:
            await send_due_summary(log_dir)
        except DatabaseMaintenanceError:
            pass  # maintenance started mid-run — retried on the next wake-up
        except Exception as exc:
            logging.error(f"[DAILY-LOG-SUMMARY] failed: {exc}", exc_info=True)
        await asyncio.sleep(min(_seconds_until_next_run(datetime.now()), _RETRY_SECONDS))


_scheduler_task: Optional["asyncio.Task[None]"] = None


def start_daily_log_summary_scheduler(log_dir: str) -> None:
    """Start the background task once per process (on_ready can run more than once)."""
    global _scheduler_task
    from clashcontrol.config import CONFIG
    if not CONFIG.daily_log_summary_enabled:
        logging.info("[DAILY-LOG-SUMMARY] disabled (DAILY_LOG_SUMMARY_ENABLED)")
        return
    if _scheduler_task is not None and not _scheduler_task.done():
        return
    import QBcore
    _scheduler_task = QBcore.spawn_tracked("daily-log-summary", _scheduler_loop(log_dir))
    logging.info(f"[DAILY-LOG-SUMMARY] scheduler started (daily at {RUN_AT.strftime('%H:%M')} server time)")
