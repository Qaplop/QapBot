"""
Admin command helpers for Discord /admin subcommands.

This module provides business logic for all /admin subcommands:
- CHECK_LOGS: Scan and summarize QapBot logs
- CHECK_DATA: Check consistency between database and JSON archives (database-only, Phase 2+)
- LIST_ALL_SUBSCRIPTIONS, CLEANUP_MESSAGES, CLEANUP_MESSAGES_ALL, POST_REGISTRATION: 
  Logic is inlined in QBdiscordcmds.py using existing helper functions

Key Features:
- Log scanning: Parse log files, categorize errors/warnings/events, format summaries
- Database validation: Integrity checks, duplicate detection, missing records, archive comparison
- Discord formatting: Respects 2000 character limit with intelligent truncation
- Read-only operations: No data manipulation, only reading and reporting

Integration:
- Used by QBdiscordcmds.py /admin subcommands
- Independent of main bot logic for clean separation
"""
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false
import os
import re
import logging
from datetime import datetime
from collections import defaultdict
from typing import List, Tuple, Dict, Any, Optional, Sequence
from qapbot.config import CONFIG


def _n(v: int) -> str:
    """Format integer with European thousand separator (period)."""
    return f"{v:,}".replace(",", ".")


def _f(v: float, d: int = 1) -> str:
    """Float with European decimal comma and thousand period."""
    s = f"{v:,.{d}f}"
    return s.replace(".", "\x00").replace(",", ".").replace("\x00", ",")


# =============================================================================
# LOG SCANNING FUNCTIONS (for /admin check_logs)
# =============================================================================

# Log pattern for parsing timestamps
LOG_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}")


def parse_log_line(line: str) -> Tuple[Optional[datetime], str]:
    """
    Parse a log line to extract timestamp and content.
    
    Args:
        line: Raw log line
        
    Returns:
        Tuple of (datetime, line_content). datetime is None if no timestamp found.
    """
    if LOG_PATTERN.match(line):
        date_str = line[:23]
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S,%f")
        except Exception:
            dt = None
        return dt, line  # type: ignore[return-value]
    return None, line  # type: ignore[return-value]


def scan_logs(log_dir: str) -> Dict[str, Any]:
    """
    Scan all qapbot.log* files in the specified directory.
    
    Args:
        log_dir: Path to logs directory
        
    Returns:
        Dict with keys:
            - errors: List[str] - All error lines
            - warnings: List[str] - All warning lines
            - summary: List[str] - Categorized event lines
            - first_date: datetime - First log timestamp
            - last_date: datetime - Last log timestamp
            - active_clan_updates: int - Number of [ACTIVE] Fetching (active subscribed clans)
            - inactive_clan_updates: int - Number of [INACTIVE] Fetching (inactive clans checked periodically)
            - smart_timestamp_calcs: int - Number of smart timestamps set for ongoing wars
            - wars_finalized: int - Number of wars finalized via manage_war_files
            - wars_archived: int - Number of wars moved to archive
            - cwl_orphan_skips: int - Number of CWL orphans skipped for later refetch
            - late_attack_checks: int - Number of late attack checks performed
            - duplicate_wars_skipped: int - Number of duplicate wars skipped
            - coc_api_slow_count: int - Number of [COC-API-SLOW] events (INFO >=2s)
            - notification_failed_count: int - Number of war reminders that could not be sent (DMs disabled)
    """
    summary: List[str] = []
    errors: List[str] = []
    warnings: List[str] = []
    admin_actions: List[str] = []
    notification_sent_count = 0
    notification_failed_count = 0
    coc_api_slow_count = 0
    active_clan_updates = 0
    inactive_clan_updates = 0
    smart_timestamp_calcs = 0
    wars_finalized = 0
    wars_archived = 0
    cwl_orphan_skips = 0
    late_attack_checks = 0
    duplicate_wars_skipped = 0
    cycle_count = 0
    cycle_min: Optional[float] = None
    cycle_max: Optional[float] = None
    cycle_total = 0.0
    _cycle_re = re.compile(r"\[CYCLE-END\] Update cycle completed in ([\d.]+)s")
    throttle_backlog_cycles = 0  # cycles where [THROTTLE] fired (deferred > 0)
    throttle_backlog_min: Optional[int] = None
    throttle_backlog_max: Optional[int] = None
    throttle_backlog_total = 0
    _throttle_re = re.compile(r"\[THROTTLE\].*\(deferred (\d+) to next cycle\)")
    first_date: Optional[datetime] = None
    last_date: Optional[datetime] = None
    bot_start_date: Optional[datetime] = None
    bot_version_from_log: Optional[str] = None
    
    if not os.path.exists(log_dir):
        return {
            'errors': [f"Log directory not found: {log_dir}"],
            'warnings': [],
            'summary': [],
            'first_date': None,
            'last_date': None,
            'active_clan_updates': 0,
            'inactive_clan_updates': 0,
            'smart_timestamp_calcs': 0,
            'wars_finalized': 0,
            'wars_archived': 0,
            'cwl_orphan_skips': 0,
            'late_attack_checks': 0,
            'duplicate_wars_skipped': 0,
            'coc_api_slow_count': 0,
            'notification_failed_count': 0,
            'cycle_count': 0,
            'cycle_min': None,
            'cycle_max': None,
            'cycle_total': 0.0,
            'throttle_backlog_cycles': 0,
            'throttle_backlog_min': None,
            'throttle_backlog_max': None,
            'throttle_backlog_total': 0,
            'bot_start_date': None,
            'bot_version_from_log': None,
        }
    
    # Only scan the two most-recent files (today's qapbot.log + yesterday's rotation).
    # Reading older files causes the command to hang for long-running bots because the
    # startup marker that terminates the scan is buried deep in old rotations.
    _all_log_files = [f for f in os.listdir(log_dir) if f.startswith("qapbot.log")]
    log_files = sorted(
        _all_log_files,
        key=lambda f: "9999-99-99" if f == "qapbot.log" else f[len("qapbot.log."):],
        reverse=True,
    )[:2]

    if not log_files:
        return {
            'errors': [],
            'warnings': ["No log files found in directory"],
            'summary': [],
            'first_date': None,
            'last_date': None,
            'active_clan_updates': 0,
            'inactive_clan_updates': 0,
            'smart_timestamp_calcs': 0,
            'wars_finalized': 0,
            'wars_archived': 0,
            'cwl_orphan_skips': 0,
            'late_attack_checks': 0,
            'duplicate_wars_skipped': 0,
            'coc_api_slow_count': 0,
            'notification_failed_count': 0,
            'cycle_count': 0,
            'cycle_min': None,
            'cycle_max': None,
            'cycle_total': 0.0,
            'throttle_backlog_cycles': 0,
            'throttle_backlog_min': None,
            'throttle_backlog_max': None,
            'throttle_backlog_total': 0,
            'bot_start_date': None,
            'bot_version_from_log': None,
        }
    
    found_start = False
    for fname in log_files:
        if found_start:
            break
        fpath = os.path.join(log_dir, fname)
        try:
            with open(fpath, encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            for line in reversed(lines):
                    dt, l = parse_log_line(line)
                    if dt:
                        if not first_date or dt < first_date:
                            first_date = dt
                        if not last_date or dt > last_date:
                            last_date = dt
                    
                    # Categorize log lines by level
                    if "[ERROR]" in l:
                        errors.append(l.strip())
                    elif "[WARNING]" in l:
                        warnings.append(l.strip())
                    
                    # Track CoC API slow calls at any log level (INFO >=2s, WARNING >=5s)
                    if "[COC-API-SLOW]" in l:
                        coc_api_slow_count += 1
                    
                    # Track metrics from INFO level logs
                    if "[INFO]" in l:
                        # Track active/inactive clan updates (new format: [ACTIVE] Fetching / [INACTIVE] Fetching)
                        if "[ACTIVE] Fetching" in l:
                            active_clan_updates += 1
                        elif "[INACTIVE] Fetching" in l:
                            inactive_clan_updates += 1
                        elif "[INACTIVE]" in l and "Smart timestamp set" in l:
                            smart_timestamp_calcs += 1
                        elif "[CYCLE-END]" in l:
                            m = _cycle_re.search(l)
                            if m:
                                elapsed = float(m.group(1))
                                cycle_count += 1
                                cycle_total += elapsed
                                if cycle_min is None or elapsed < cycle_min:
                                    cycle_min = elapsed
                                if cycle_max is None or elapsed > cycle_max:
                                    cycle_max = elapsed
                        elif "[THROTTLE]" in l and "deferred" in l:
                            mt = _throttle_re.search(l)
                            if mt:
                                deferred = int(mt.group(1))
                                throttle_backlog_cycles += 1
                                throttle_backlog_total += deferred
                                if throttle_backlog_min is None or deferred < throttle_backlog_min:
                                    throttle_backlog_min = deferred
                                if throttle_backlog_max is None or deferred > throttle_backlog_max:
                                    throttle_backlog_max = deferred
                    
                    # Stop at the most recent bot start — everything accumulated so far
                    # is from after this startup (we are reading bottom-to-top).
                    if "[INFO] QapBot started" in l:
                        if dt:
                            bot_start_date = dt
                            first_date = dt
                        summary.append(f"Startup: {l.strip()}")
                        found_start = True
                        break  # Stop this file; outer loop will also stop

                    if "[INFO] Version: " in l:
                        m_ver = re.search(r"Version:\s+(\S+)", l)
                        if m_ver:
                            bot_version_from_log = m_ver.group(1)
                    
                    # Refactored war file management patterns (2026-01 refactoring)
                    # These can appear at INFO or DEBUG level, so check independently
                    if "[MANAGE-WAR-FILES]" in l:
                        if "Processing old war:" in l:
                            summary.append(f"War Processing: {l.strip()}")
                    elif "[CWL-ORPHAN-SKIP]" in l:
                        cwl_orphan_skips += 1
                        summary.append(f"CWL Orphan Skipped: {l.strip()}")
                    elif "[FINALIZE-OLD-WAR]" in l:
                        wars_finalized += 1
                        summary.append(f"War Finalized: {l.strip()}")
                    elif "[FINALIZE]" in l:
                        # New war being finalized
                        if "Processing new war" in l:
                            wars_finalized += 1
                    elif "[DUPLICATE-SKIP]" in l:
                        duplicate_wars_skipped += 1
                        summary.append(f"Duplicate War Skipped: {l.strip()}")
                    elif "[ARCHIVE]" in l:
                        # Track all archive operations
                        if "Moved" in l:
                            wars_archived += 1
                            summary.append(f"War Archived: {l.strip()}")
                    elif "[LATE-CHECK]" in l:
                        # [LATE-CHECK] appears at DEBUG level, just check if line contains it
                        late_attack_checks += 1
                    elif "leaderboard - content changed" in l:
                        summary.append(f"Leaderboard Updated: {l.strip()}")
                    elif "calculate_win_probability returned" in l:
                        summary.append(f"Win Probability: {l.strip()}")
                    elif "[INFO] ADMIN ACTION:" in l:
                        admin_actions.append(l.strip())
                    elif "✅ Sent war reminder" in l:
                        notification_sent_count += 1
                    elif "Could not send war reminder" in l:
                        notification_failed_count += 1
        except Exception as e:
            errors.append(f"Error reading log file {fname}: {e}")

    # Lines were read newest-first; restore chronological (oldest-first) order for formatters.
    errors.reverse()
    warnings.reverse()
    summary.reverse()
    admin_actions.reverse()

    return {
        'errors': errors,
        'warnings': warnings,
        'summary': summary,
        'admin_actions': admin_actions,
        'notification_sent_count': notification_sent_count,
        'notification_failed_count': notification_failed_count,
        'coc_api_slow_count': coc_api_slow_count,
        'active_clan_updates': active_clan_updates,
        'inactive_clan_updates': inactive_clan_updates,
        'smart_timestamp_calcs': smart_timestamp_calcs,
        'wars_finalized': wars_finalized,
        'wars_archived': wars_archived,
        'cwl_orphan_skips': cwl_orphan_skips,
        'late_attack_checks': late_attack_checks,
        'duplicate_wars_skipped': duplicate_wars_skipped,
        'cycle_count': cycle_count,
        'cycle_min': cycle_min,
        'cycle_max': cycle_max,
        'cycle_total': cycle_total,
        'throttle_backlog_cycles': throttle_backlog_cycles,
        'throttle_backlog_min': throttle_backlog_min,
        'throttle_backlog_max': throttle_backlog_max,
        'throttle_backlog_total': throttle_backlog_total,
        'first_date': first_date,
        'last_date': last_date,
        'bot_start_date': bot_start_date,
        'bot_version_from_log': bot_version_from_log,
    }


_NIGHTLY_MAINT_END_RE = re.compile(r"\[NIGHTLY-MAINTENANCE\] END — total duration ([\d.]+)s")


def _fmt_duration(seconds: float) -> str:
    """Format a duration in whole minutes/seconds, e.g. 783.8 -> '13m 4s', 45.2 -> '45s'."""
    total = round(seconds)
    minutes, secs = divmod(total, 60)
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


def find_last_nightly_maintenance_duration(log_dir: str) -> Optional[Tuple[datetime, float]]:
    """
    Find the most recently completed nightly-maintenance run by scanning
    qapbot.log* rotations newest-first, independent of this process's own
    startup marker.

    Unlike scan_logs() (which resets its counters at the last "QapBot started"
    line, so only reflects the current process), nightly maintenance runs at
    most once/night — the last completed run may well predate this process's
    start (e.g. right after a restart, before 03:00 UTC comes around again).
    This is the /status and /admin Check Logs fallback shown until
    QBcore.nightly_maintenance_durations has its first in-process entry.

    Returns:
        (timestamp, duration_seconds) of the most recent match, or None if no
        qapbot.log* files exist or none contain a "[NIGHTLY-MAINTENANCE] END" line.
    """
    if not os.path.exists(log_dir):
        return None
    all_log_files = [f for f in os.listdir(log_dir) if f.startswith("qapbot.log")]
    log_files = sorted(
        all_log_files,
        key=lambda f: "9999-99-99" if f == "qapbot.log" else f[len("qapbot.log."):],
        reverse=True,
    )
    for fname in log_files:
        fpath = os.path.join(log_dir, fname)
        try:
            with open(fpath, encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except Exception:
            continue
        for line in reversed(lines):
            if "[NIGHTLY-MAINTENANCE] END" not in line:
                continue
            m = _NIGHTLY_MAINT_END_RE.search(line)
            if not m:
                continue
            dt, _ = parse_log_line(line)
            if dt is None:
                continue
            return dt, float(m.group(1))
    return None


def format_nightly_maintenance_stats(durations: Sequence[float], log_dir: str) -> str:
    """
    Build the nightly-maintenance duration line shown by /status and
    /admin Check Logs: min/avg/max over up to the last 10 in-process runs
    (QBcore.nightly_maintenance_durations), or — until this process has
    completed its own first run — the most recent run read from the log
    file via find_last_nightly_maintenance_duration().
    """
    n = len(durations)
    if n > 0:
        d_min, d_max = min(durations), max(durations)
        d_avg = sum(durations) / n
        return (
            f"Last {n} run{'s' if n != 1 else ''} (this session) — "
            f"Min: {_fmt_duration(d_min)} | Avg: {_fmt_duration(d_avg)} | Max: {_fmt_duration(d_max)}"
        )
    last = find_last_nightly_maintenance_duration(log_dir)
    if last is None:
        return "No nightly maintenance run recorded yet"
    dt, secs = last
    return (
        f"No run yet this session — last recorded run: "
        f"{dt.strftime('%Y-%m-%d %H:%M:%S')} ({_fmt_duration(secs)})"
    )


def format_log_summary(
    scan_result: Dict[str, Any],
    max_length: int = 1900,
    bot_version: Optional[str] = None,
    nightly_maint_section: Optional[str] = None,
) -> str:
    """
    Format log scan results for Discord output with character limit.

    Args:
        scan_result: Dict from scan_logs()
        max_length: Maximum characters for output (default 1900, safe for Discord 2000 limit)
        bot_version: Current bot version string (e.g. from QBcore.BOT_VERSION)
        nightly_maint_section: Pre-formatted nightly-maintenance duration line
            (see format_nightly_maintenance_stats), shown near the top so it is
            never dropped by the errors/warnings truncation below.

    Returns:
        Formatted string ready for Discord
    """
    errors = scan_result['errors']
    warnings = scan_result['warnings']
    summary = scan_result['summary']
    admin_actions = scan_result.get('admin_actions', [])
    notification_sent_count = scan_result.get('notification_sent_count', 0)
    notification_failed_count = scan_result.get('notification_failed_count', 0)
    coc_api_slow_count = scan_result.get('coc_api_slow_count', 0)
    active_updates = scan_result.get('active_clan_updates', 0)
    inactive_updates = scan_result.get('inactive_clan_updates', 0)
    smart_ts = scan_result.get('smart_timestamp_calcs', 0)
    wars_finalized = scan_result.get('wars_finalized', 0)
    wars_archived = scan_result.get('wars_archived', 0)
    cwl_orphan_skips = scan_result.get('cwl_orphan_skips', 0)
    late_attack_checks = scan_result.get('late_attack_checks', 0)
    duplicate_wars_skipped = scan_result.get('duplicate_wars_skipped', 0)
    first_date = scan_result['first_date']
    last_date = scan_result['last_date']
    bot_start_date = scan_result.get('bot_start_date')
    bot_version_from_log = scan_result.get('bot_version_from_log')
    version_str = bot_version or bot_version_from_log
    
    lines = ["**=== QapBot Log Summary ===**\n"]
    
    if version_str:
        lines.append(f"**Bot version:** {version_str}")
    if bot_start_date:
        start_str = bot_start_date.strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"**Bot started:** {start_str}")
    
    if first_date and last_date:
        # Calculate time span for clarity
        time_span = last_date - first_date
        hours = time_span.total_seconds() / 3600
        last_str = last_date.strftime("%Y-%m-%d %H:%M:%S") + f".{last_date.microsecond // 1000:03d}"
        if bot_start_date:
            lines.append(f"**Coverage:** last {_f(hours)} hours (up to {last_str})\n")
        else:
            first_str = first_date.strftime("%Y-%m-%d %H:%M:%S") + f".{first_date.microsecond // 1000:03d}"
            lines.append(f"**Time period:** {first_str} to {last_str} ({_f(hours)} hours)\n")
    else:
        lines.append("")
    
    lines.append(f"**Total Errors:** {_n(len(errors))}")
    lines.append(f"**Total Warnings:** {_n(len(warnings))}\n")

    if nightly_maint_section:
        lines.append("**Nightly Maintenance:**")
        lines.append(f"  {nightly_maint_section}\n")

    # War Update Metrics section (refactored format - 2026-01)
    cycle_count = scan_result.get('cycle_count', 0)
    cycle_min = scan_result.get('cycle_min')
    cycle_max = scan_result.get('cycle_max')
    cycle_total = scan_result.get('cycle_total', 0.0)
    if active_updates or inactive_updates:
        lines.append(f"**War Update Cycles:**")
        lines.append(f"  • Active clans (subscribed): {_n(active_updates)}")
        lines.append(f"  • Inactive clans (periodic check): {_n(inactive_updates)}")
        if smart_ts:
            lines.append(f"  • Smart timestamps (ongoing wars): {_n(smart_ts)}")
        if cycle_count > 0 and cycle_min is not None and cycle_max is not None:
            cycle_avg = cycle_total / cycle_count
            lines.append(f"  • Cycles completed: {_n(cycle_count)}")
            lines.append(f"  • Runtime — Min: {_f(cycle_min)}s | Avg: {_f(cycle_avg)}s | Max: {_f(cycle_max)}s")
        throttle_backlog_cycles = scan_result.get('throttle_backlog_cycles', 0)
        throttle_backlog_min = scan_result.get('throttle_backlog_min')
        throttle_backlog_max = scan_result.get('throttle_backlog_max')
        throttle_backlog_total = scan_result.get('throttle_backlog_total', 0)
        if throttle_backlog_cycles > 0:
            throttle_backlog_avg = throttle_backlog_total / throttle_backlog_cycles
            throttle_backlog_min_i = int(throttle_backlog_min or 0)
            throttle_backlog_max_i = int(throttle_backlog_max or 0)
            lines.append(
                f"  • Throttle backlog: {_n(throttle_backlog_cycles)}/{_n(cycle_count)} cycles throttled "
                f"— Min: {_n(throttle_backlog_min_i)} | Avg: {_f(throttle_backlog_avg, 0)} | Max: {_n(throttle_backlog_max_i)}"
            )
        elif cycle_count > 0:
            lines.append(f"  • Throttle backlog: 0/{_n(cycle_count)} cycles throttled ✅")
        if coc_api_slow_count:
            lines.append(f"  • CoC API slow calls (≥2s): {_n(coc_api_slow_count)}")
        lines.append("")
    
    # Summarize non-critical events by category
    startup_count = sum(1 for s in summary if s.startswith("Startup:"))
    
    # Other event counts
    leaderboard_count = sum(1 for s in summary if s.startswith("Leaderboard Updated:"))
    winprob_count = sum(1 for s in summary if s.startswith("Win Probability:"))
    admin_action_count = len(admin_actions)
    
    lines.append("**Event Summary:**")
    lines.append(f"  Startups: {_n(startup_count)}")
    
    # War file management section (refactored 2026-01 format)
    if any([wars_finalized, wars_archived, cwl_orphan_skips, late_attack_checks, duplicate_wars_skipped]):
        lines.append(f"  **War File Management:**")
        if wars_finalized:
            lines.append(f"  • Wars Finalized: {_n(wars_finalized)}")
        if wars_archived:
            lines.append(f"  • Wars Archived: {_n(wars_archived)}")
        if cwl_orphan_skips:
            lines.append(f"  • CWL Orphans (skipped for refetch): {_n(cwl_orphan_skips)}")
        if late_attack_checks:
            lines.append(f"  • Late Attack Checks: {_n(late_attack_checks)}")
        if duplicate_wars_skipped:
            lines.append(f"  • Duplicate Wars Skipped: {_n(duplicate_wars_skipped)}")
    
    lines.append(f"  Leaderboard Updates: {_n(leaderboard_count)}")
    lines.append(f"  Win Probability Calculations: {_n(winprob_count)}")
    notif_line = f"  Notifications Sent: {_n(notification_sent_count)}"
    if notification_failed_count:
        notif_line += f" | Could not send (DMs disabled): {_n(notification_failed_count)}"
    lines.append(notif_line)
    lines.append(f"  ADMIN ACTIONS: {_n(admin_action_count)}\n")
    
    # Check current length before adding errors/warnings
    current_output = "\n".join(lines)
    remaining = max_length - len(current_output)
    
    # Add errors (truncate if needed) - Show newest first
    if errors:
        lines.append("**Errors:**")
        error_section: List[str] = []
        recent_errors = list(reversed(errors[-10:]))  # Last 10 errors, newest first
        for e in recent_errors:
            error_line = f"  {e[:200]}"  # Truncate long lines
            if len("\n".join(error_section)) + len(error_line) + 50 < remaining:
                error_section.append(error_line)
            else:
                error_section.append(f"  ... and {len(errors) - len(error_section)} older errors (check logs)")
                break
        lines.extend(error_section)
        lines.append("")
    
    # Add warnings (truncate if needed) - Show newest first
    if warnings:
        current_output = "\n".join(lines)
        remaining = max_length - len(current_output)
        
        lines.append("**Warnings:**")
        warning_section: List[str] = []
        recent_warnings = list(reversed(warnings[-10:]))  # Last 10 warnings, newest first
        for w in recent_warnings:
            warning_line = f"  {w[:200]}"  # Truncate long lines
            if len("\n".join(warning_section)) + len(warning_line) + 50 < remaining:
                warning_section.append(warning_line)
            else:
                warning_section.append(f"  ... and {len(warnings) - len(warning_section)} older warnings (check logs)")
                break
        lines.extend(warning_section)
    
    # Final status
    if not errors and not warnings:
        lines.append("\n✅ **No critical issues detected.**")
    
    return "\n".join(lines)


# =============================================================================
# DATABASE VALIDATION FUNCTIONS (for /admin check_data - Phase 2+ Database)
# =============================================================================

def extract_war_id_from_json_data(json_data: Dict[str, Any]) -> str:
    """Extract war ID from JSON war data."""
    opponent = json_data.get('opponent', {})
    opponent_tag = opponent.get('tag', 'UNK').lstrip('#') if opponent else 'UNK'
    
    start_time_str = json_data.get('start_time', '')
    start_time = None
    if start_time_str:
        m = re.search(r'datetime\.datetime\(([^)]+)\)', start_time_str)
        if m:
            dt_args = [int(x.strip()) for x in m.group(1).split(',')]
            if len(dt_args) >= 5:
                y, mo, d, h, mi = dt_args[:5]
                start_time = f"{y:04d}{mo:02d}{d:02d}{h:02d}{mi:02d}"
    
    if not start_time:
        start_time = datetime.now().strftime("%Y%m%d%H%M")
    
    return f"{opponent_tag}_{start_time}"


def check_current_wars_in_temp(data_dir: str) -> List[Dict[str, Any]]:
    """
    Return current active wars from CACHE.temp_war_metadata (no file I/O).

    Previously scanned all ~18k temp files on disk; now reads the in-memory
    metadata dict that was populated at startup and kept up-to-date by
    save_war_object() / set_temp_war_stats().  Filters to clans that are
    currently in war via CACHE.in_war_clan_tags so only the ~19 active entries
    are processed rather than 18,924.
    """
    from qapbot.cache_manager import CACHE

    current_wars = []
    active_tags = CACHE.in_war_clan_tags  # set of clan tags currently in war

    for clan_tag in active_tags:
        meta = CACHE.temp_war_metadata.get(clan_tag)
        if not meta:
            continue

        war_state = meta.get("state", "unknown")

        # Parse start_time / end_time strings stored in metadata
        start_time = None
        end_time = None
        for key, dst in (("start_time", "start"), ("end_time", "end")):
            time_str = meta.get(key, "")
            if time_str:
                m = re.search(r'datetime\.datetime\(([^)]+)\)', time_str)
                if m:
                    try:
                        dt_args = [int(x.strip()) for x in m.group(1).split(',')]
                        if len(dt_args) >= 5:
                            y, mo, d, h, mi = dt_args[:5]
                            from datetime import timezone
                            dt = datetime(y, mo, d, h, mi, tzinfo=timezone.utc)
                            if dst == "start":
                                start_time = dt
                            else:
                                end_time = dt
                    except Exception:
                        pass

        clan_name = CACHE.clan_name_cache.get(clan_tag, "Unknown")

        current_wars.append({
            'json_file': os.path.basename(meta.get("filepath", "")),
            'clan_tag': clan_tag,
            'clan_name': clan_name,
            'war_state': war_state,
            'start_time': start_time,
            'end_time': end_time,
        })

    return current_wars


def check_database_consistency(db_path: str, data_dir: str, include_integrity: bool = False, archive_dir: Optional[str] = None) -> Dict[str, Any]:
    """
    Perform comprehensive database validation checks.

    Uses CACHE.db_manager for all database operations (no direct sqlite3).

    Args:
        db_path: Path to SQLite database (e.g., "data/qapbot.db")
        data_dir: Path to data directory for archive comparison
        include_integrity: If True, run PRAGMA integrity_check (adds ~11s on a 584MB DB).
                           Defaults to False for the interactive /admin Check Data command.
        archive_dir: Path to archive directory. Defaults to CONFIG.archive_dir.

    Returns:
        Dict with keys:
            - integrity_ok: bool  (True = skipped or passed)
            - integrity_errors: List[str]
            - duplicate_wars: Dict[key, List[(war_id, date)]]
            - missing_from_db: List[(json_file, clan_tag, war_id)]
            - missing_from_archive: List[(clan_tag, war_id)]  — wars from last 14 days missing a JSON file
            - current_wars: List[Dict]
            - total_records: int
            - total_wars: int
            - total_clans: int
    """
    from qapbot.cache_manager import CACHE
    
    results = {
        'integrity_ok': False,
        'integrity_errors': [],
        'duplicate_wars': {},
        'missing_from_db': [],
        'missing_from_archive': [],
        'current_wars': [],
        'total_records': 0,
        'total_wars': 0,
        'total_clans': 0,
        'orphaned_summaries': 0,
        'corrupt_archive_files': 0,
    }
    
    # Verify db_manager is available
    if not CACHE.db_manager:
        results['integrity_errors'] = ["Database manager not initialized"]
        return results
    
    # 1. Database integrity check (optional — ~11s on a 584MB DB)
    if not os.path.exists(db_path):
        results['integrity_errors'] = [f"Database file not found: {db_path}"]
        return results

    try:
        if include_integrity:
            is_ok, errors = CACHE.db_manager.check_integrity_sync()
            results['integrity_ok'] = is_ok
            results['integrity_errors'] = errors
        else:
            results['integrity_ok'] = True  # skipped — assumed OK
        
        # 2. Get database statistics
        stats = CACHE.db_manager.get_global_db_statistics_sync()  # type: ignore[union-attr]
        results['total_records'] = stats['attacks_count']
        results['total_wars'] = stats['wars_count']
        results['total_clans'] = stats['clans_count']
        
        # 3. Find duplicate war IDs (same clan+opponent within 48 hours)
        all_wars = CACHE.db_manager.get_all_war_summaries_brief_sync()  # type: ignore[union-attr]
        
        wars_by_clan_opponent: Dict[str, Dict[str, Tuple[str, datetime]]] = defaultdict(dict)
        
        for war_id, clan_tag, date_str, is_cwl in all_wars:
            if '_' not in war_id:
                continue
            if is_cwl:
                # CWL clans legitimately fight the same opponent in consecutive
                # rounds within 48 h — skip CWL wars from duplicate detection.
                continue
            opponent_tag = war_id.split('_')[0]
            key = f"{clan_tag}|{opponent_tag}"
            
            try:
                war_date = datetime.fromisoformat(date_str)
            except:
                continue
            
            wars_by_clan_opponent[key][war_id] = (date_str, war_date)
        
        # Find potential duplicates (within 48 hours)
        for key, war_dict in wars_by_clan_opponent.items():
            if len(war_dict) < 2:
                continue
            
            sorted_wars = sorted(war_dict.items(), key=lambda x: x[1][1])
            potential_duplicates = []
            
            for i in range(len(sorted_wars)):
                for j in range(i + 1, len(sorted_wars)):
                    war_id_1, (date_str_1, date_1) = sorted_wars[i]
                    war_id_2, (date_str_2, date_2) = sorted_wars[j]
                    
                    time_diff_hours = abs((date_2 - date_1).total_seconds() / 3600)
                    if time_diff_hours <= 48:
                        potential_duplicates.append((war_id_1, date_str_1))
                        potential_duplicates.append((war_id_2, date_str_2))
            
            if potential_duplicates:
                unique_dupes = list(set(potential_duplicates))
                results['duplicate_wars'][key] = sorted(unique_dupes, key=lambda x: x[1])  # type: ignore[arg-type, return-value]
        
    except Exception as e:
        results['integrity_errors'] = [f"Database query failed: {e}"]
        return results
    
    # 4+5. Single parallel pass through the archive directory.
    #
    # One batch DB query loads all (clan_tag, war_id) pairs from war_summary.
    # war_summary is used instead of war_attacks because:
    #   - war_summary covers every war processed by the bot, including
    #     PrivateWarLog wars that have no attack rows in war_attacks.
    #   - war_attacks has 27M+ rows; loading its full key set into Python
    #     makes this check take 50+ seconds on the server-machine.
    archive_dir = archive_dir if archive_dir is not None else os.path.join(data_dir, 'archive')
    archived_war_ids: set[tuple[str, str]] = set()  # keyed on (clan_tag, war_id)

    wars_in_db = CACHE.db_manager.get_all_war_summary_keys_sync()  # type: ignore[union-attr]

    if os.path.exists(archive_dir):
        # Derive clan_tag and war_id directly from the filename — no file reads needed.
        # Archive filename formats:
        #   Regular war: CLANTAG_OPPONENTTAG_YYYYMMDDHHMM_war_data.json         (3-part)
        #   CWL war:     CLANTAG_OPPONENTTAG_YYYYMMDDHHMM_WARTAG_war_data.json  (4-part)
        # war_id = OPPONENTTAG_YYYYMMDDHHMM  (parts[1] + '_' + parts[2] for both formats)
        # Walk sharded subdirs; also handles any remaining flat files (backward compat).
        _archive_fnames: List[str] = []
        for _entry in os.scandir(archive_dir):
            if _entry.is_dir():
                _archive_fnames.extend(e.name for e in os.scandir(_entry.path))
            elif _entry.name.endswith('_war_data.json'):
                _archive_fnames.append(_entry.name)
        for archive_fname in _archive_fnames:
            if not archive_fname.endswith('_war_data.json'):
                continue
            parts = archive_fname.replace('_war_data.json', '').split('_')
            if len(parts) < 3:
                continue
            clan_tag = '#' + parts[0]
            war_id = parts[1] + '_' + parts[2]
            archived_war_ids.add((clan_tag, war_id))
            if (clan_tag, war_id) not in wars_in_db:
                results['missing_from_db'].append((archive_fname, clan_tag, war_id))

    # 5. Check for recent wars in database missing JSON archives (last 14 days).
    #    14 days covers the current CWL season + a few days buffer; the archive
    #    directory intentionally only retains recent files for backup purposes.
    #    Keyed on (clan_tag, war_id) to avoid masking a gap when two clans fight
    #    the same opponent at the same time and would share a war_id.
    try:
        # Use 7 days to match the move_old_archive_files.py age threshold (default 7 days).
        # Wars older than 7 days are moved to archive_old and would always show as
        # "missing from archive" if we look back further than the move threshold.
        recent_wars = CACHE.db_manager.get_recent_war_summaries_sync(days=7)  # type: ignore[union-attr]
        for clan_tag, war_id, _date in recent_wars:
            if (clan_tag, war_id) not in archived_war_ids:
                results['missing_from_archive'].append((clan_tag, war_id))
    except:
        pass

    # 6. Orphaned summaries: skipped — would require a full war_attacks key scan
    #    (27M+ rows on server-machine → 50+ seconds).  total_records in the stats above
    #    reflects the true attack count; the orphaned_summaries field is left at 0.
    # results['orphaned_summaries'] = ...
    
    # 6. Check current wars in temp directory
    results['current_wars'] = check_current_wars_in_temp(data_dir)
    
    return results


def format_database_check_results(results: Dict[str, Any], max_length: int = 1900) -> str:
    """
    Format database consistency check results for Discord output.
    
    Args:
        results: Dict from check_database_consistency()
        max_length: Maximum characters for output (default 1900)
        
    Returns:
        Formatted string ready for Discord
    """
    lines = ["**=== Database Validation Check ===**\n"]
    
    # Database integrity
    if results['integrity_ok']:
        lines.append("**Database Integrity:** ✅ OK")
    else:
        lines.append("**Database Integrity:** ❌ FAILED")
        for error in results['integrity_errors'][:5]:  # Show max 5 errors
            lines.append(f"  - {error}")
    
    lines.append("")
    
    # Statistics
    lines.append(f"**Database Statistics:**")
    lines.append(f"  Total Records: {_n(results['total_records'])}")
    lines.append(f"  Total Wars: {_n(results['total_wars'])}")
    lines.append(f"  Total Clans: {_n(results['total_clans'])}")
    lines.append("")
    
    # Duplicate wars
    duplicate_count = len(results['duplicate_wars'])
    status_icon = "✅" if duplicate_count == 0 else "⚠️"
    lines.append(f"**Duplicate Wars (same opponent, within 48 hours):** {_n(duplicate_count)} {status_icon}")
    
    if results['duplicate_wars']:
        lines.append("  Showing first 5 duplicate groups:")
        for i, (key, wars) in enumerate(list(results['duplicate_wars'].items())[:5]):
            clan_tag, opponent = key.split('|')
            lines.append(f"  {i+1}. {clan_tag} vs #{opponent}: {len(wars)} war IDs")
            for war_id, date in wars[:3]:  # Show max 3 per group
                lines.append(f"     - {war_id} ({date})")
        lines.append("")
    
    # Missing from database
    missing_db_count = len(results['missing_from_db'])
    status_icon = "✅" if missing_db_count == 0 else "❌"
    lines.append(f"**Wars in Archive but Missing from Database:** {_n(missing_db_count)} {status_icon}")
    
    if results['missing_from_db']:
        lines.append("  Showing first 5:")
        for _json_file, clan_tag, war_id in results['missing_from_db'][:5]:
            lines.append(f"  - {clan_tag}: {war_id}")
        lines.append("")
    
    # Missing from archive
    missing_archive_count = len(results['missing_from_archive'])
    status_icon = "✅" if missing_archive_count == 0 else "⚠️"
    lines.append(f"**Wars in Database but Missing JSON Archive (last 7 days):** {_n(missing_archive_count)} {status_icon}")
    
    if results['missing_from_archive']:
        lines.append("  Showing first 5:")
        for clan_tag, war_id in results['missing_from_archive'][:5]:
            lines.append(f"  - {clan_tag}: {war_id}")
        lines.append("")

    # Orphaned summaries
    orphaned_count = results.get('orphaned_summaries', 0)
    status_icon = "✅" if orphaned_count == 0 else "❌"
    lines.append(f"**Summaries Without Attack Data:** {_n(orphaned_count)} {status_icon}")
    if orphaned_count > 0:
        lines.append("  (war_summary rows with no matching war_attacks — partial import failures)")
        lines.append("")

    # Corrupt archive files
    corrupt_count = results.get('corrupt_archive_files', 0)
    status_icon = "✅" if corrupt_count == 0 else "⚠️"
    lines.append(f"**Corrupt Archive Files:** {_n(corrupt_count)} {status_icon}")
    if corrupt_count > 0:
        lines.append("  (archive JSON files that could not be parsed — manual inspection needed)")
        lines.append("")

    # Current wars with detailed phase breakdown
    current_wars_count = len(results['current_wars'])
    lines.append(f"**Current Wars (in temp directory):** {_n(current_wars_count)}")
    
    if results['current_wars']:
        from datetime import timezone
        
        # Group by phase
        preparation_wars = [w for w in results['current_wars'] if w.get('war_state') == 'preparation']
        in_war_wars = [w for w in results['current_wars'] if w.get('war_state') in ('in_war', 'war', 'inWar')]
        
        # Sort preparation wars by start time (earliest first - both past and future)
        preparation_wars.sort(key=lambda w: w.get('start_time') or datetime.max.replace(tzinfo=timezone.utc))
        
        # Sort war phase by end time (earliest first - both past and future)
        in_war_wars.sort(key=lambda w: w.get('end_time') or datetime.max.replace(tzinfo=timezone.utc))
        
        # Show preparation phase
        if preparation_wars:
            lines.append(f"\n  **Preparation Phase ({_n(len(preparation_wars))} wars):**")
            for war in preparation_wars[:5]:  # Show first 5
                clan_tag = war.get('clan_tag', 'Unknown')
                clan_name = war.get('clan_name', 'Unknown')
                start_time = war.get('start_time')
                
                if start_time:
                    now = datetime.now(timezone.utc)
                    time_until = start_time - now
                    total_seconds = time_until.total_seconds()
                    
                    if total_seconds > 0:
                        # Future time - war hasn't started yet
                        hours = int(total_seconds // 3600)
                        minutes = int((total_seconds % 3600) // 60)
                        
                        if hours > 0:
                            time_str = f"in {hours}h {minutes}m"
                        elif minutes > 0:
                            time_str = f"in {minutes}m"
                        else:
                            time_str = "starting now"
                    else:
                        # Past time - war already started (shouldn't be in preparation phase)
                        abs_seconds = abs(total_seconds)
                        hours = int(abs_seconds // 3600)
                        minutes = int((abs_seconds % 3600) // 60)
                        time_str = f"started {hours}h {minutes}m ago" if hours > 0 else f"started {minutes}m ago"
                    
                    # Convert UTC to local time for display
                    local_time = start_time.astimezone()
                    start_str = local_time.strftime("%Y-%m-%d %H:%M %Z")
                    lines.append(f"    {clan_tag} ({clan_name}): Starts {start_str} ({time_str})")
                else:
                    lines.append(f"    {clan_tag} ({clan_name}): Unknown start time")
            
            if len(preparation_wars) > 5:
                lines.append(f"    ... and {len(preparation_wars) - 5} more")
        
        # Show war phase
        if in_war_wars:
            lines.append(f"\n  **War Phase ({_n(len(in_war_wars))} wars):**")
            for war in in_war_wars[:5]:  # Show first 5
                clan_tag = war.get('clan_tag', 'Unknown')
                clan_name = war.get('clan_name', 'Unknown')
                end_time = war.get('end_time')
                
                if end_time:
                    now = datetime.now(timezone.utc)
                    time_until = end_time - now
                    total_seconds = time_until.total_seconds()
                    
                    if total_seconds > 0:
                        # Future time - war hasn't ended yet
                        hours = int(total_seconds // 3600)
                        minutes = int((total_seconds % 3600) // 60)
                        
                        if hours > 0:
                            time_str = f"in {hours}h {minutes}m"
                        elif minutes > 0:
                            time_str = f"in {minutes}m"
                        else:
                            time_str = "ending now"
                    else:
                        # Past time - war already ended (shouldn't be in war phase)
                        abs_seconds = abs(total_seconds)
                        hours = int(abs_seconds // 3600)
                        minutes = int((abs_seconds % 3600) // 60)
                        time_str = f"ended {hours}h {minutes}m ago" if hours > 0 else f"ended {minutes}m ago"
                    
                    # Convert UTC to local time for display
                    local_time = end_time.astimezone()
                    end_str = local_time.strftime("%Y-%m-%d %H:%M %Z")
                    lines.append(f"    {clan_tag} ({clan_name}): Ends {end_str} ({time_str})")
                else:
                    lines.append(f"    {clan_tag} ({clan_name}): Unknown end time")
            
            if len(in_war_wars) > 5:
                lines.append(f"    ... and {len(in_war_wars) - 5} more")
        
        lines.append("")
    
    # Final status
    if (results['integrity_ok'] and duplicate_count == 0 and
            missing_db_count == 0 and missing_archive_count == 0 and
            orphaned_count == 0 and corrupt_count == 0):
        lines.append("✅ **All checks passed! Database is healthy.**")
    else:
        lines.append("⚠️ **Issues found. Review the report above.**")
    
    # Truncate if needed, keeping as many lines as possible.
    # Only append the notice when lines are actually dropped — if the full output
    # fits, no notice is shown.
    _NOTICE = "\n... (truncated for Discord length limit) ..."
    _budget = max_length - len(_NOTICE) - 1  # reserve space for notice + joining \n
    kept: list[str] = []
    used = 0
    _truncated = False
    for line in lines:
        segment_len = len(line) + (1 if kept else 0)  # +1 for the joining \n
        if used + segment_len > _budget:
            _truncated = True
            break
        kept.append(line)
        used += segment_len

    if _truncated:
        final_output = "\n".join(kept) + _NOTICE
    else:
        final_output = "\n".join(kept)
    
    return final_output


# =============================================================================
# LIST ALL SUBSCRIPTIONS FUNCTION (for /admin list_all_subscriptions)
# =============================================================================

async def list_all_subscriptions(bot: Any, cache: Any) -> list[str]:
    """
    List all channel subscriptions across all guilds.
    
    Args:
        bot: Discord bot instance
        cache: CACHE instance with subscriptions data
        
    Returns:
        List of message strings (chunked for Discord 2000 char limit)
    """
    subs = cache.subscriptions
    if not subs:
        return ["No active subscriptions found."]

    # Build guilds as separate sections
    guild_sections = []
    subscribed_clans = set()
    
    for guild_id, guild_data in sorted(subs.items()):
        guild_lines = []
        guild = bot.get_guild(int(guild_id))
        guild_name = guild.name if guild else "Unknown Guild"
        
        guild_lines.append("```")
        guild_lines.append(f"**{guild_name}** ({guild_id})")
        guild_lines.append("")
        
        guild_subscriptions = []
        for channel_id, clan_tags in guild_data.items():
            channel = bot.get_channel(int(channel_id))
            channel_name = f"#{channel.name}" if channel else "Unknown"
            
            for tag_entry in clan_tags:
                tag = tag_entry.get('clan_tag')
                subscription_type = tag_entry.get('subscription_type', '')
                
                # Skip playerregistration pseudo-subscriptions (not real clans)
                if tag == 'PLAYERREGISTRATION' or subscription_type == 'playerregistration':
                    continue
                
                if tag in cache.clan_families:
                    entity_name = cache.clan_families[tag].get('name', "Unknown Family")
                    # Add all member clans of this family to subscribed_clans
                    family_clans = cache.clan_families[tag].get('clans', [])
                    subscribed_clans.update(family_clans)
                else:
                    # Use get_clan_name() to handle both string and dict formats
                    entity_name = cache.get_clan_name(tag, "Unknown Clan")
                
                guild_subscriptions.append({
                    'channel_name': channel_name,
                    'channel_id': channel_id,
                    'entity_name': entity_name,
                    'entity_tag': tag,
                    'subscription_type': subscription_type
                })
                subscribed_clans.add(tag)
        
        guild_lines.append(f"{'Channel Name':<20} {'Channel ID':<20} {'Clan/Family Name':<18} {'Tag':<12} {'Type':<10}")
        guild_lines.append(f"{'-'*20} {'-'*20} {'-'*18} {'-'*12} {'-'*10}")
        
        for sub in guild_subscriptions:
            channel_name = str(sub['channel_name'])[:19].rstrip('-')
            channel_id = str(sub['channel_id'])[:19].rstrip('-')
            entity_name = str(sub['entity_name'])[:17].rstrip('-')
            entity_tag = str(sub['entity_tag'])[:11].rstrip('-')
            sub_type = str(sub['subscription_type'])[:9].rstrip('-')
            
            guild_lines.append(f"{channel_name:<20} {channel_id:<20} {entity_name:<18} {entity_tag:<12} {sub_type:<10}")
        
        guild_lines.append("```")
        guild_lines.append("")
        guild_sections.append(guild_lines)

    unsubscribed_lines = []
    unsubscribed = [tag for tag in cache.clan_name_cache if tag not in subscribed_clans]
    if unsubscribed:
        unsubscribed_lines.append("**Unsubscribed clans:**")
        unsubscribed_lines.append("```")
        for tag in sorted(unsubscribed):
            # Use get_clan_name() to handle both string and dict formats
            clan_name = cache.get_clan_name(tag, "Unknown Clan")
            unsubscribed_lines.append(f"- {clan_name} ({tag})")
        unsubscribed_lines.append("```")

    # Smart chunking
    MAX_CHARS = 1900
    chunks = []
    current_chunk = ["**Active Subscriptions:**", ""]
    current_len = len("\n".join(current_chunk)) + 1
    
    for guild_lines in guild_sections:
        guild_text = "\n".join(guild_lines)
        guild_len = len(guild_text) + 1
        
        if current_len + guild_len > MAX_CHARS and len(current_chunk) > 2:
            # Chunk is full, save it and start new chunk
            chunks.append("\n".join(current_chunk))
            current_chunk = []
            current_len = 0
        
        current_chunk.extend(guild_lines)
        current_len += guild_len
    
    # Add current chunk before processing unsubscribed (if it has content)
    if len(current_chunk) > 0:
        chunks.append("\n".join(current_chunk))
    
    # Handle unsubscribed clans separately
    if unsubscribed_lines:
        unsubscribed_text = "\n".join(unsubscribed_lines)
        
        # If unsubscribed section fits in Discord limit, send as separate chunk
        if len(unsubscribed_text) <= MAX_CHARS:
            chunks.append(unsubscribed_text)
        else:
            # Unsubscribed list is too long, split it
            current_unsub_chunk = [unsubscribed_lines[0], unsubscribed_lines[1]]  # Header and opening ```
            current_unsub_len = len("\n".join(current_unsub_chunk))
            
            for line in unsubscribed_lines[2:-1]:  # Skip header, opening, and closing ```
                line_len = len(line) + 1
                if current_unsub_len + line_len + 4 > MAX_CHARS:  # +4 for closing ```
                    current_unsub_chunk.append("```")
                    chunks.append("\n".join(current_unsub_chunk))
                    current_unsub_chunk = ["```"]  # Start new chunk with opening ```
                    current_unsub_len = 3
                
                current_unsub_chunk.append(line)
                current_unsub_len += line_len
            
            # Add closing ``` to final chunk
            current_unsub_chunk.append("```")
            chunks.append("\n".join(current_unsub_chunk))

    return chunks


# =============================================================================
# CLEANUP MESSAGES FUNCTIONS (for /admin cleanup_messages actions)
# =============================================================================

async def handle_cleanup_messages_channel(interaction: Any, channel: Any, bot: Any, username: str) -> str:
    """
    Clean up orphaned bot messages and stale message IDs in a specific channel.
    
    Args:
        interaction: Discord interaction
        channel: Discord channel to clean up
        bot: Discord bot instance
        username: User who initiated the action
        
    Returns:
        Success message string
        
    Raises:
        Exception: If cleanup fails
    """
    import logging
    from qapbot.QBdiscocmdshelper import cleanup_channel_messages
    
    logging.warning(f"cleanup_messages (channel only) issued by user {username}")
    orphaned_deleted, stale_cleaned = await cleanup_channel_messages(channel, bot)
    logging.info(f"ADMIN ACTION: {username} CLEANUP_MESSAGES (channel)")
    
    return (
        f"Deleted {orphaned_deleted} orphaned bot messages in this channel.\n"
        f"Cleaned up {stale_cleaned} stale message IDs from cache for this channel."
    )


async def handle_cleanup_messages_all(bot: Any, cache: Any, username: str) -> str:
    """
    Clean up orphaned bot messages and stale message IDs across all channels.
    
    Loops through all channels referenced in the cache and applies unified cleanup
    to each one, removing both orphaned messages and stale cache entries.
    
    Args:
        bot: Discord bot instance
        cache: CACHE instance with message tracking
        username: User who initiated the action
        
    Returns:
        Success message string
        
    Raises:
        Exception: If cleanup fails
    """
    import asyncio
    import logging
    from qapbot.QBdiscocmdshelper import cleanup_channel_messages

    logging.warning(f"cleanup_messages scope=all issued by user {username}")

    # Collect all unique channel IDs from cache
    known_channel_ids = set()
    for entry in cache.leaderboard_messages.values():
        cid = entry.get('channel_id')
        if cid:
            known_channel_ids.add(int(cid))

    # Process all channels in parallel (max 5 concurrent)
    _cleanup_sem = asyncio.Semaphore(5)

    async def _cleanup_one(channel_id: int) -> tuple[int, int]:
        import discord
        from qapbot.config import CONFIG

        channel = bot.get_channel(channel_id)
        if channel:
            async with _cleanup_sem:
                try:
                    return await cleanup_channel_messages(channel, bot)
                except Exception as e:
                    logging.error(f"Error cleaning up channel {channel_id}: {e}")
                    return 0, 0

        if CONFIG.is_dev_mode:
            # Dev bot is only in dev guilds; channels from prod guilds don't resolve here.
            # Silently skip to avoid purging cache entries that are valid in prod.
            logging.debug(f"Channel {channel_id} not found in DEV mode - skipping (may be prod channel)")
            return 0, 0

        # get_channel() is a local-cache lookup, not proof of deletion — a cache miss can also be
        # a transient gateway/cache gap. Confirm with a live API call before purging anything;
        # a false purge here drops the tracked message ID, which then causes a *new* message to be
        # posted next time (e.g. duplicate registration messages) even though the old one is still live.
        async with _cleanup_sem:
            try:
                channel = await bot.fetch_channel(channel_id)
            except discord.NotFound:
                # Discord API confirms the channel no longer exists. Safe to purge.
                stale_keys = [k for k, v in list(cache.leaderboard_messages.items())
                              if v.get('channel_id') == str(channel_id)]
                for k in stale_keys:
                    await cache.delete_leaderboard_message(k)
                if stale_keys:
                    logging.info(f"Purged {len(stale_keys)} cache entries for confirmed-deleted channel {channel_id}")
                return 0, len(stale_keys)
            except Exception as e:
                # Forbidden (no access), HTTPException (API error/rate limit), network errors, etc.
                # None of these confirm deletion — leave the cache untouched and retry next run.
                logging.warning(f"Could not confirm channel {channel_id} is deleted ({e}); skipping this run.")
                return 0, 0

            try:
                return await cleanup_channel_messages(channel, bot)
            except Exception as e:
                logging.error(f"Error cleaning up channel {channel_id}: {e}")
                return 0, 0

    channel_results = await asyncio.gather(*[_cleanup_one(cid) for cid in known_channel_ids])
    total_orphaned_deleted = sum(r[0] for r in channel_results)
    total_stale_cleaned = sum(r[1] for r in channel_results)

    logging.info(
        f"ADMIN ACTION: {username} CLEANUP_MESSAGES_ALL "
        f"(orphaned={total_orphaned_deleted}, stale={total_stale_cleaned}, channels={len(known_channel_ids)})"
    )
    
    return (
        f"Deleted {total_orphaned_deleted} orphaned bot messages across all channels.\n"
        f"Cleaned up {total_stale_cleaned} stale message IDs from cache."
    )


# =============================================================================
# MEMORY PROFILE FUNCTION (for /admin memory_profile action)
# =============================================================================


def _build_cache_summary(cache: Any) -> list[str]:
    """Build human-readable cache size summary lines."""
    lines: list[str] = []
    try:
        coc_cache = cache.coc_clan_cache
        coc_entries = len(coc_cache.cache)
        try:
            coc_stats = coc_cache.get_stats()
            coc_mb = coc_stats.get("estimated_size_mb", 0)
        except Exception:
            coc_mb = 0.0
        lines.append(f"  coc_clan_cache      : {coc_entries} entries, ~{coc_mb:.1f} MB")
        lines.append(f"  clan_name_cache     : {len(cache.clan_name_cache)} clans")
        lines.append(f"  subscriptions       : {sum(len(v) for v in cache.subscriptions.values())} channels across {len(cache.subscriptions)} guilds")
        lines.append(f"  leaderboard_messages: {len(cache.leaderboard_messages)} entries")
        lines.append(f"  user_accounts       : {len(cache.user_accounts)} users")
        lines.append(f"  notification_state  : {len(cache.notification_state)} wars")
        lines.append(f"  clan_history        : {len(cache.clan_history)} clans loaded (lazy)")
        lines.append(f"  history_cache       : {len(cache.history_cache)} filtered slices")
        lines.append(f"  temp_war_stats      : {len(cache.temp_war_stats)} clans")
        from qapbot.cache_manager import MAX_TEMP_WAR_OBJECTS as _mwo_cap
        lines.append(f"  temp_war_objects    : {len(cache.temp_war_objects)} clans (cap={_mwo_cap:,})")
        lines.append(f"  server_config       : {len(cache.server_config)} guilds")
    except Exception as _ce:
        lines.append(f"  (error reading CACHE sizes: {_ce})")
    return lines


def _build_gc_type_counts(top_n: int = 15, max_scan: int = 200_000) -> list[tuple[str, int]]:
    """Return top-N GC object types by count.

    max_scan caps the number of objects iterated so this never hangs during
    CWL season when millions of cached objects are live (4+ GB RSS).
    The sample is still representative: object lists are ordered by generation
    so the most numerous types appear early.
    """
    import gc
    type_counts: dict[str, int] = {}
    for i, obj in enumerate(gc.get_objects()):
        if i >= max_scan:
            type_counts['<scan capped>'] = type_counts.get('<scan capped>', 0) + 1
            break
        t = type(obj).__name__
        type_counts[t] = type_counts.get(t, 0) + 1
    return sorted(type_counts.items(), key=lambda kv: kv[1], reverse=True)[:top_n]


def _get_process_memory_mb() -> tuple[float, float]:
    """Return (current RSS_MB, VMS_MB) for the current process.

    Priority:
    1. /proc/self/status  — Linux; reports *current* VmRSS (not high-water-mark like resource.ru_maxrss)
    2. psutil             — cross-platform fallback
    3. resource module    — last resort; note ru_maxrss is peak RSS, not current
    """
    # /proc/self/status gives the actual live RSS, unlike resource.ru_maxrss which
    # is the high-water mark since process start and never decreases.
    try:
        with open('/proc/self/status', 'r') as _f:
            _status = _f.read()
        _rss_kb = 0
        _vms_kb = 0
        for _line in _status.splitlines():
            if _line.startswith('VmRSS:'):
                _rss_kb = int(_line.split()[1])
            elif _line.startswith('VmSize:'):
                _vms_kb = int(_line.split()[1])
        if _rss_kb:
            return _rss_kb / 1024, _vms_kb / 1024
    except Exception:
        pass
    try:
        import psutil
        proc = psutil.Process()
        mem = proc.memory_info()
        return mem.rss / 1_048_576, mem.vms / 1_048_576
    except Exception:
        pass
    try:
        import resource  # Unix only; ru_maxrss = peak high-water, not current
        rusage = resource.getrusage(resource.RUSAGE_SELF)  # type: ignore[attr-defined]
        return rusage.ru_maxrss / 1024, 0.0  # Linux reports KB
    except Exception:
        return 0.0, 0.0


def save_memtrace_snapshot(cache: Any) -> str:
    """
    Take a tracemalloc snapshot, combine it with CACHE + GC + process stats,
    and write the full report to data/logs/memprofile_<timestamp>.txt.

    If QBcore.memtrace_baseline is set, also writes a differential section
    (what grew since the baseline was taken) using snapshot.compare_to().

    Called from:
    - handle_memory_profile() when tracemalloc is already active (DEV mode)
    - periodic_main() after an on-demand trace cycle completes (PROD mode)

    Returns:
        Path to the saved report file.
    """
    import tracemalloc
    import gc
    import os
    import time
    from datetime import datetime as _dt
    import QBcore

    gc.collect()
    snapshot = tracemalloc.take_snapshot()
    top_stats = snapshot.statistics("lineno")

    # Differential: compare against the baseline taken when tracing started
    baseline = QBcore.memtrace_baseline
    diff_stats = snapshot.compare_to(baseline, "lineno") if baseline is not None else None  # type: ignore[arg-type]

    cache_summary = _build_cache_summary(cache)
    top_types = _build_gc_type_counts(15)
    rss_mb, vms_mb = _get_process_memory_mb()

    # Uptime
    uptime_str = "unknown"
    if QBcore.bot_start_time:
        elapsed = time.time() - QBcore.bot_start_time
        h, rem = divmod(int(elapsed), 3600)
        m, s = divmod(rem, 60)
        uptime_str = f"{h}h {m}m {s}s"

    timestamp = _dt.now().strftime("%Y%m%d_%H%M%S")
    log_dir = os.path.join(CONFIG.data_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    report_path = os.path.join(log_dir, f"memprofile_{timestamp}.txt")

    lines: list[str] = []
    lines.append(f"QapBot Memory Profile — {_dt.now().isoformat()}")
    lines.append("=" * 70)
    nframes = tracemalloc.get_traceback_limit()
    lines.append(f"tracemalloc nframe={nframes}  |  RSS={rss_mb:.1f} MB  VMS={vms_mb:.1f} MB  |  uptime={uptime_str}")

    lines.append("\n[CACHE STRUCTURE SIZES]")
    lines.extend(cache_summary)

    lines.append("\n[GC OBJECT COUNTS — top 15 by type]")
    for tname, cnt in top_types:
        lines.append(f"  {tname:<40} {cnt:>10,}")

    lines.append(f"\n[TRACEMALLOC — top 50 allocation sites by cumulative size]")
    for i, stat in enumerate(top_stats[:50], 1):
        lines.append(f"  {i:>3}. {stat}")

    if diff_stats is not None:
        # Only show sites where memory GREW since baseline (size_diff > 0)
        grown = [s for s in diff_stats if s.size_diff > 0]
        grown_sorted = sorted(grown, key=lambda s: s.size_diff, reverse=True)
        baseline_time = getattr(QBcore, '_memtrace_baseline_time', None)
        since_str = f" since {baseline_time}" if baseline_time else ""
        lines.append(f"\n[GROWTH vs BASELINE{since_str} — top 30 by size increase]")
        if grown_sorted:
            for i, stat in enumerate(grown_sorted[:30], 1):
                size_diff_kb = stat.size_diff / 1024
                count_diff = stat.count_diff
                lines.append(f"  {i:>3}. +{size_diff_kb:>8.1f} KiB  ({count_diff:+d} allocs)  {stat.traceback[0]}")
        else:
            lines.append("  (no growth detected — memory footprint is stable)")

    report_text = "\n".join(lines)
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(report_text)

    # Clear the baseline so the next trace starts fresh
    QBcore.memtrace_baseline = None
    QBcore._memtrace_baseline_time = None  # type: ignore[attr-defined]

    logging.info(f"[ADMIN] MEMORY_PROFILE written to {report_path}")
    return report_path


async def handle_backfill_cwl_groups(cache: Any, season: str) -> str:
    """
    For every clan with <7 CWL war_summary rows for *season*, fetch its league
    group from the CoC API and write to cwl_league_groups + cwl_league_rounds.

    API calls are deduplicated: after fetching one clan's group, all group
    members are added to the ``seen`` set and skipped on subsequent iterations.

    Cycle safety:
    - Waits for any in-progress update cycle to finish before starting (up to
      10 min).  Refuses if the cycle does not complete in time.
    - Holds ``QBcore.backfill_idle_event`` cleared for the duration so that
      ``periodic_main()`` does not start a new update cycle while CoC API
      calls are in flight.

    After this runs, execute::

        python qapbot/scripts/analyze_cwl_rounds.py <season> --fetch-missing

    to generate the recovery file from the newly stored war_tags.

    Args:
        cache:  CacheManager instance (CACHE).
        season: CWL season in ``YYYY-MM`` format, e.g. ``'2026-05'``.

    Returns:
        Human-readable summary string for the Discord followup message.
    """
    import asyncio
    import QBcore as _qbcore

    # ── Cycle guard: only run during the sleep phase ──────────────────────────
    if not _qbcore.cycle_idle_event.is_set():
        logging.info("[BACKFILL-CWL-GROUPS] Update cycle running — waiting up to 10 min...")
        try:
            await asyncio.wait_for(_qbcore.cycle_idle_event.wait(), timeout=600.0)
        except asyncio.TimeoutError:
            return (
                "⚠️ Timed out waiting for the current update cycle to finish (10 min). "
                "Try again after the cycle completes."
            )

    # ── Signal: block new update cycles from starting while we run ───────────
    _qbcore.backfill_idle_event.clear()
    try:
        return await _handle_backfill_cwl_groups_inner(cache, season)
    finally:
        _qbcore.backfill_idle_event.set()


def _explicit_cols(conn: Any, table: str) -> str:
    """Comma-joined column names for `table`, read from `main`'s own schema — see
    WarHistoryDB._explicit_column_list_sync's docstring (qapbot/db_manager.py) for why every
    `UNION ALL` between `main.<table>` and `history.<table>` must name columns explicitly
    rather than using a bare `SELECT *` (2026-08-14 hot/history column-order-drift incident).
    Duplicated here (rather than routed through CACHE.db_manager) because this function
    deliberately uses its own short-lived, read-only, non-pooled connection — this helper only
    needs a `main` schema attached, which that connection already has via attach_history_db."""
    cur = conn.execute(f"PRAGMA main.table_info({table})")
    return ", ".join(row["name"] for row in cur.fetchall())


async def _handle_backfill_cwl_groups_inner(cache: Any, season: str) -> str:
    """Inner implementation of handle_backfill_cwl_groups (cycle guards handled by caller)."""
    import asyncio
    import sqlite3

    # ── Step 1: find all clan_tags with <7 rounds for this season ────────────
    conn = sqlite3.connect(f'file:{CONFIG.db_path}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    from qapbot.db_manager import attach_history_db
    attach_history_db(conn, CONFIG.db_path, getattr(CONFIG, "history_db_path", None), read_only=True)
    try:
        ws_cols = _explicit_cols(conn, "war_summary")
        short_clan_rows = conn.execute(
            f"""
            WITH ws AS (
                SELECT {ws_cols} FROM main.war_summary UNION ALL SELECT {ws_cols} FROM history.war_summary
            )
            SELECT clan_tag
            FROM   ws
            WHERE  is_cwl = 1 AND cwl_season = ?
            GROUP  BY clan_tag
            HAVING COUNT(*) < 7
            ORDER  BY clan_tag
            """,
            (season,),
        ).fetchall()
        rounds_before = (conn.execute(
            "SELECT (SELECT COUNT(*) FROM main.cwl_league_rounds WHERE cwl_season = ?) "
            "+ (SELECT COUNT(*) FROM history.cwl_league_rounds WHERE cwl_season = ?) AS cnt",
            (season, season),
        ).fetchone() or {})["cnt"] or 0
        groups_before = (conn.execute(
            "SELECT COUNT(DISTINCT league_group_id) AS cnt FROM ("
            "SELECT league_group_id FROM main.cwl_league_groups WHERE cwl_season = ? "
            "UNION ALL SELECT league_group_id FROM history.cwl_league_groups WHERE cwl_season = ?"
            ")",
            (season, season),
        ).fetchone() or {})["cnt"] or 0
    finally:
        conn.close()

    short_clans: list[str] = [r["clan_tag"] for r in short_clan_rows]
    if not short_clans:
        return f"✅ No clans with <7 rounds found for season **{season}**. Nothing to backfill."

    # ── Step 2: fetch league groups, deduping via seen set ───────────────────
    seen: set[str] = set()
    groups_fetched = 0
    api_errors = 0
    skipped = 0

    for clan_tag in short_clans:
        if clan_tag in seen:
            skipped += 1
            continue
        seen.add(clan_tag)
        try:
            lg = await cache.get_league_group(clan_tag)
            # Mark all group members as seen to avoid redundant API calls
            for c in getattr(lg, "clans", []):
                ct = getattr(c, "tag", None)
                if ct:
                    seen.add(str(ct))
            groups_fetched += 1
        except Exception as exc:
            logging.warning(f"[BACKFILL-CWL-GROUPS] get_league_group({clan_tag}) failed: {exc}")
            api_errors += 1
        # Small yield to keep event loop responsive
        await asyncio.sleep(0)

    # ── Step 3: post-analysis — compare war_summary counts vs group round counts ─
    conn2 = sqlite3.connect(f'file:{CONFIG.db_path}?mode=ro', uri=True)
    conn2.row_factory = sqlite3.Row
    attach_history_db(conn2, CONFIG.db_path, getattr(CONFIG, "history_db_path", None), read_only=True)
    try:
        rounds_after = (conn2.execute(
            "SELECT (SELECT COUNT(*) FROM main.cwl_league_rounds WHERE cwl_season = ?) "
            "+ (SELECT COUNT(*) FROM history.cwl_league_rounds WHERE cwl_season = ?) AS cnt",
            (season, season),
        ).fetchone() or {})["cnt"] or 0
        groups_after = (conn2.execute(
            "SELECT COUNT(DISTINCT league_group_id) AS cnt FROM ("
            "SELECT league_group_id FROM main.cwl_league_groups WHERE cwl_season = ? "
            "UNION ALL SELECT league_group_id FROM history.cwl_league_groups WHERE cwl_season = ?"
            ")",
            (season, season),
        ).fetchone() or {})["cnt"] or 0

        # Per-clan comparison: rounds in war_summary vs rounds stored in their CWL group.
        #   rounds_in_ws == rounds_in_group  → genuinely short group, nothing missing
        #   rounds_in_ws <  rounds_in_group  → some war_tags missing from war_summary
        #   rounds_in_group == 0             → API fetch failed, no group data
        ws_cols2 = _explicit_cols(conn2, "war_summary")
        clg_cols2 = _explicit_cols(conn2, "cwl_league_groups")
        clr_cols2 = _explicit_cols(conn2, "cwl_league_rounds")
        analysis_rows = conn2.execute(
            f"""
            WITH ws_all AS (
                SELECT {ws_cols2} FROM main.war_summary UNION ALL SELECT {ws_cols2} FROM history.war_summary
            ),
            clg_all AS (
                SELECT {clg_cols2} FROM main.cwl_league_groups UNION ALL SELECT {clg_cols2} FROM history.cwl_league_groups
            ),
            clr_all AS (
                SELECT {clr_cols2} FROM main.cwl_league_rounds UNION ALL SELECT {clr_cols2} FROM history.cwl_league_rounds
            ),
            ws AS (
                SELECT clan_tag, COUNT(*) AS rounds_in_ws
                FROM   ws_all
                WHERE  is_cwl = 1 AND cwl_season = ?
                GROUP  BY clan_tag
                HAVING COUNT(*) < 7
            ),
            grp AS (
                SELECT clg.clan_tag,
                       COUNT(DISTINCT clr.cwl_round) AS rounds_in_group
                FROM   clg_all clg
                JOIN   clr_all clr
                       ON  clr.league_group_id = clg.league_group_id
                       AND clr.cwl_season      = ?
                WHERE  clg.cwl_season = ?
                GROUP  BY clg.clan_tag
            )
            SELECT ws.clan_tag,
                   ws.rounds_in_ws,
                   COALESCE(grp.rounds_in_group, 0) AS rounds_in_group
            FROM   ws
            LEFT JOIN grp ON grp.clan_tag = ws.clan_tag
            """,
            (season, season, season),
        ).fetchall()
    finally:
        conn2.close()

    truly_short      = 0  # war_summary count == group round count  (short group, complete)
    actually_missing = 0  # war_summary count <  group round count  (real gaps)
    no_group_data    = 0  # group not in DB after fetch (api error)

    for r in analysis_rows:
        ws_cnt  = r["rounds_in_ws"]
        grp_cnt = r["rounds_in_group"]
        if grp_cnt == 0:
            no_group_data += 1
        elif ws_cnt < grp_cnt:
            actually_missing += 1
        else:
            truly_short += 1

    new_war_tags = rounds_after - rounds_before
    new_groups   = groups_after - groups_before

    logging.info(
        f"[BACKFILL-CWL-GROUPS] season={season}: {groups_fetched} groups fetched, "
        f"+{new_groups} new groups, +{new_war_tags} new war_tags, "
        f"{skipped} skipped, {api_errors} api_errors | "
        f"truly_short={truly_short}, actually_missing={actually_missing}, "
        f"no_group_data={no_group_data}"
    )

    lines = [
        f"✅ CWL group backfill complete for **{season}**",
        "",
        "**API fetch results:**",
        f"• Clans with <7 rounds (input): **{len(short_clans)}**",
        f"• Unique groups fetched via API: **{groups_fetched}**",
        f"• Clans skipped (same group, already fetched): **{skipped}**",
        f"• API errors: **{api_errors}**",
        f"• New `cwl_league_groups` entries: **{new_groups}**",
        f"• New `cwl_league_rounds` war_tags written: **{new_war_tags}**",
        "",
        "**Round-completeness analysis (after fetch):**",
        f"• ✅ Genuinely short groups (no missing data): **{truly_short}** clans",
        f"• ⚠️ Clans with missing war_tags in their group: **{actually_missing}** clans",
        f"• ❌ No group data (API fetch failed): **{no_group_data}** clans",
    ]
    if actually_missing > 0:
        lines.append(
            f"\nNext step: run `analyze_cwl_rounds.py {season} --fetch-missing` "
            f"to generate the recovery file for the **{actually_missing}** affected clans."
        )
    elif api_errors == 0 and no_group_data == 0:
        lines.append("\nAll clans are in short groups — no missing war data in the DB.")
    return "\n".join(lines)


async def handle_memory_profile(cache: Any) -> str:
    """
    Memory profiling with two modes:

    **DEV mode** (tracemalloc already active with 25 frames):
        Takes an immediate snapshot and returns the full report.

    **PROD mode** (tracemalloc inactive):
        1st call: Starts tracemalloc with nframe=1 (~2-3% CPU overhead),
                  sets QBcore.memtrace_pending=True.  After the next update
                  cycle completes, periodic_main() auto-takes the snapshot,
                  saves the report, and stops tracing.
        2nd call: If the report file exists, shows its summary.

    Args:
        cache: CacheManager instance (CACHE)

    Returns:
        Short summary string for the Discord followup message.
    """
    import tracemalloc
    import gc
    import os
    import glob as _glob

    # --- Already tracing (DEV mode or 2nd-cycle PROD) → immediate snapshot ---
    # NOTE: do NOT call gc.collect() here — it runs on the event loop and with
    # tracemalloc active every freed object's trace is deallocated synchronously,
    # making it extremely slow (seconds→minutes at 4+ GB RSS).  save_memtrace_snapshot
    # calls gc.collect() inside asyncio.to_thread() where it's safe.
    if tracemalloc.is_tracing():
        # Offload CPU-heavy work (gc.collect, tracemalloc.take_snapshot,
        # gc.get_objects loop) to a thread so the event loop stays free
        # for discord.py heartbeats.  save_memtrace_snapshot() already
        # takes its own snapshot internally.
        import asyncio as _asyncio
        report_path = await _asyncio.to_thread(save_memtrace_snapshot, cache)
        snapshot = tracemalloc.take_snapshot()
        top_stats = snapshot.statistics("lineno")
        cache_summary = _build_cache_summary(cache)
        rss_mb, _ = _get_process_memory_mb()

        total_top10_mb = sum(s.size for s in top_stats[:10]) / 1_048_576
        discord_lines: list[str] = [
            f"**Memory Profile** — `{os.path.basename(report_path)}`",
            f"Full report saved to `{report_path}`",
            f"RSS: {rss_mb:.1f} MB  |  nframe={tracemalloc.get_traceback_limit()}",
            "",
            "**CACHE sizes:**",
        ]
        discord_lines.extend(f"`{l.strip()}`" for l in cache_summary)
        discord_lines.append("")
        discord_lines.append(f"**Top 10 tracemalloc sites** ({total_top10_mb:.1f} MB combined):")
        for i, stat in enumerate(top_stats[:10], 1):
            size_mb = stat.size / 1_048_576
            frame = stat.traceback[0] if stat.traceback else None
            loc = f"{os.path.basename(frame.filename)}:{frame.lineno}" if frame else "?"
            discord_lines.append(f"`{i:>2}. {size_mb:6.2f} MB  {loc}`")
        return "\n".join(discord_lines)

    # --- Not tracing (PROD) → check for pending or start a new trace ---
    import QBcore  # type: ignore[import-untyped]

    if QBcore.memtrace_pending:
        return (
            "⏳ **Memory trace is already running.**\n"
            "A snapshot will be taken automatically after the current update cycle.\n"
            "Run this command again afterwards to see the results."
        )

    # Check for existing report from a previous on-demand trace
    log_dir = os.path.join(CONFIG.data_dir, "logs")
    existing = sorted(_glob.glob(os.path.join(log_dir, "memprofile_*.txt")), reverse=True)

    # Start on-demand trace: 1 frame = ~2-3% CPU for ONE cycle.
    # Take a baseline snapshot immediately so save_memtrace_snapshot() can
    # emit a differential section showing only what grew since tracing started.
    tracemalloc.start(1)
    gc.collect()
    from datetime import datetime as _dt2
    QBcore.memtrace_baseline = tracemalloc.take_snapshot()
    QBcore._memtrace_baseline_time = _dt2.now().strftime("%Y-%m-%dT%H:%M:%S")  # type: ignore[attr-defined]
    QBcore.memtrace_pending = True
    logging.info("[MEMTRACE] On-demand trace started (nframe=1) — baseline snapshot taken, will diff after next cycle")

    msg_lines = [
        "🔬 **Memory trace started** (1-frame, ~2-3% CPU overhead).",
        "A snapshot will be taken automatically after the next update cycle completes,",
        "then tracing will be stopped.",
        "",
        "Run `/admin Memory Profile` again after the cycle to see the full report.",
    ]
    if existing:
        msg_lines.append(f"\n📄 Previous report: `{existing[0]}`")
    return "\n".join(msg_lines)
