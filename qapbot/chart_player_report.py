"""Charts for the /whois player_tag war performance report.

Two chart generators (both return JPEG bytes for Discord):
  make_season_chart_bytes  — monthly CW avg-stars trend (line) + CWL per-season bars
  make_last10_chart_bytes  — horizontal bar chart for the last 10 wars

Design notes:
  - Per-war star averages are capped at 3.0 to suppress archive-import dirty data
    (some early rows have stars > 3 due to a parser bug in the original import).
  - CWL seasons use the cwl_season field (YYYY-MM) as the x-label; CW uses date[:7].
  - Both charts follow the same dark palette as chart_clans_per_league.py.
"""
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false

import io
import calendar
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, DefaultDict, Dict, List, Optional

import matplotlib
matplotlib.use("Agg")  # non-interactive — no display needed
from matplotlib.axes import Axes
from matplotlib.container import BarContainer
from matplotlib.figure import Figure
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.ticker import MultipleLocator

# ── Dark theme palette (consistent with chart_clans_per_league.py) ───────────
_BG_OUTER = "#1a1a2e"
_BG_INNER = "#16213e"
_FG_TEXT  = "#e0e0e0"
_FG_MUTED = "#a0a0c0"
_GRID     = "#0f3460"
_WIN      = "#2ecc71"   # green
_LOSS     = "#e74c3c"   # red
_DRAW     = "#f1c40f"   # yellow
_CW       = "#4e9af1"   # blue  — CW attacks
_CWL      = "#f1a940"   # amber — CWL attacks
_MISS     = "#444466"   # dark  — 0-attack wars


@dataclass
class AttackAggregate:
    capped_stars: float = 0.0
    attacks: int = 0


def _result_color(result: str) -> str:
    r = (result or "").lower()
    return _WIN if r == "win" else (_LOSS if r == "lose" else _DRAW)


def _apply_dark(fig: Figure, ax: Axes) -> None:
    fig.patch.set_facecolor(_BG_OUTER)
    ax.set_facecolor(_BG_INNER)
    ax.tick_params(colors=_FG_MUTED, labelsize=8)
    for sp in ax.spines.values():
        sp.set_edgecolor(_GRID)


def _to_bytes(fig: Figure) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="jpeg", dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor(), pil_kwargs={"quality": 95})
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _capped_avg(stars: int, attacks: int) -> float:
    """Per-war avg stars per attack, capped at 3.0 to handle dirty archive data."""
    return min(stars / attacks, 3.0) if attacks > 0 else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Chart 1: Monthly/seasonal star performance trend
# ─────────────────────────────────────────────────────────────────────────────

def make_season_chart_bytes(
    war_rows: List[Dict[str, Any]],
    player_name: str,
    player_tag: str,
) -> bytes:
    """
    Line/bar chart of average stars per attack, grouped by calendar month.

    - CW:  blue line chart, one data point per calendar month
    - CWL: amber bar chart, one bar per CWL season (YYYY-MM from cwl_season field)
    - Dashed reference line = overall avg across all wars
    - Dotted reference line at 3.0 (theoretical max)

    Stars are capped at 3.0 per attack to suppress dirty archive import data.
    """
    # ── Aggregate ──────────────────────────────────────────────────────────
    cw: DefaultDict[str, AttackAggregate] = defaultdict(AttackAggregate)
    cwl: DefaultDict[str, AttackAggregate] = defaultdict(AttackAggregate)

    for r in war_rows:
        if r["attacks"] == 0:
            continue
        month = (r["date"] or "")[:7]
        if not month:
            continue
        cap = _capped_avg(r["stars"], r["attacks"]) * r["attacks"]  # capped star total
        if r["is_cwl"]:
            season = r["cwl_season"] if r["cwl_season"] else month
            cwl[season].capped_stars += cap
            cwl[season].attacks += r["attacks"]
        else:
            cw[month].capped_stars += cap
            cw[month].attacks += r["attacks"]

    all_months: List[str] = sorted(set(cw) | set(cwl))
    if not all_months:
        fig, ax = plt.subplots(figsize=(6, 3))
        _apply_dark(fig, ax)
        ax.text(0.5, 0.5, "No data", ha="center", va="center",
                color=_FG_TEXT, transform=ax.transAxes)
        return _to_bytes(fig)

    n      = len(all_months)
    month_step = 1.28
    x      = [i * month_step for i in range(n)]
    # Strip "20" century prefix to save x-axis space (25-12 instead of 2025-12)
    labels = [m[2:] if len(m) == 7 else m for m in all_months]

    cw_avgs: List[float | None] = [
        cw[m].capped_stars / cw[m].attacks if cw[m].attacks > 0 else None
        for m in all_months
    ]
    cwl_avgs: List[float | None] = [
        cwl[m].capped_stars / cwl[m].attacks if cwl[m].attacks > 0 else None
        for m in all_months
    ]

    bar_w   = 0.24
    pair_offset = 0.15
    fig_w   = max(9, n * 1.15)
    fig, ax = plt.subplots(figsize=(fig_w, 5))
    _apply_dark(fig, ax)

    # CW dot is second within the same month group.
    cw_xi = [xi + pair_offset for xi, v in zip(x, cw_avgs) if v is not None]
    cw_yi = [v for v in cw_avgs if v is not None]
    if cw_xi:
        ax.plot(
            cw_xi, cw_yi, color=_CW, marker="o", markersize=6,
            linewidth=1.8, label="CW (per month)", zorder=2.5,
        )
        for xi, yi in zip(cw_xi, cw_yi):
            ax.text(
                xi, yi + 0.08, f"{yi:.2f}", ha="center", va="bottom",
                fontsize=7.5, color=_CW, fontweight="bold",
            )

    # Month groups are spaced wider than the two series inside each month.
    # This keeps the same-month CWL bar + CW dot visually paired.
    cwl_xi = [xi - pair_offset for xi, v in zip(x, cwl_avgs) if v is not None]
    cwl_yi = [v for v in cwl_avgs if v is not None]
    if cwl_xi:
        bars: BarContainer = ax.bar(
            cwl_xi, cwl_yi, width=bar_w, color=_CWL, alpha=0.85,
            label="CWL (per season)", zorder=3.5, edgecolor=_BG_OUTER, linewidth=0.5,
        )
        for b, v in zip(list(bars.patches), cwl_yi):
            ax.text(
                b.get_x() + b.get_width() / 2, v + 0.06,
                f"{v:.2f}", ha="center", va="bottom",
                fontsize=7.5, color=_CWL, fontweight="bold",
                zorder=5,
            )

    # Overall avg reference line (computed from capped values)
    all_cap_stars = sum(
        _capped_avg(r["stars"], r["attacks"]) * r["attacks"]
        for r in war_rows if r["attacks"] > 0
    )
    all_atk = sum(r["attacks"] for r in war_rows if r["attacks"] > 0)
    overall = all_cap_stars / all_atk if all_atk > 0 else 0.0
    ax.axhline(
        overall, color=_FG_MUTED, linestyle="--", linewidth=1.0, alpha=0.65,
        label=f"Overall ({overall:.2f} avg)", zorder=2,
    )
    ax.axhline(3.0, color="#555577", linestyle=":", linewidth=0.7, alpha=0.4, zorder=1)

    # ── Styling ────────────────────────────────────────────────────────────
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8, color=_FG_MUTED)
    ax.margins(x=0.04)
    ax.set_ylim(0, 3.4)
    ax.yaxis.set_major_locator(MultipleLocator(0.5))
    ax.yaxis.grid(True, color=_GRID, linestyle="--", linewidth=0.6, alpha=0.7)
    ax.set_axisbelow(True)

    ax.set_ylabel("Avg stars per attack", fontsize=10, color=_FG_MUTED, labelpad=8)
    ax.set_xlabel("Month", fontsize=10, color=_FG_MUTED, labelpad=8)
    ax.set_title(
        f"Attack Performance over Time  -  {player_name} ({player_tag})",
        fontsize=12, color=_FG_TEXT, pad=12,
    )
    ax.legend(
        loc="lower right", facecolor=_GRID, edgecolor="#555577",
        labelcolor=_FG_TEXT, fontsize=9,
    )

    plt.tight_layout()
    return _to_bytes(fig)


# ─────────────────────────────────────────────────────────────────────────────
# Chart 2: Last-10-wars horizontal bar chart
# ─────────────────────────────────────────────────────────────────────────────

def make_last10_chart_bytes(
    war_rows: List[Dict[str, Any]],
    player_name: str,
    player_tag: str,
) -> bytes:
    """
    Horizontal bar chart for the last 10 wars.

    Bar fill  = avg stars per attack (capped at 3.0, 0 for missed wars).
    Background = max possible (3.0 scale).
    Color      = green/red/yellow for win/loss/draw; dark gray for 0-attack wars.
    Labels     = stars * attacks + capped avg, or "0/N atk (missed)".
    """
    wars = war_rows[:10]
    if not wars:
        fig, ax = plt.subplots(figsize=(8, 3))
        _apply_dark(fig, ax)
        ax.text(0.5, 0.5, "No data", ha="center", va="center",
                color=_FG_TEXT, transform=ax.transAxes)
        return _to_bytes(fig)

    # Reverse so newest war is at the top (matplotlib draws bottom-up)
    wars_rev = list(reversed(wars))

    y_labels: List[str] = []
    avgs:     List[float] = []
    colors:   List[str]  = []

    for r in wars_rev:
        date   = (r["date"] or "")[:10]
        type_s = "CWL" if r["is_cwl"] else " CW"
        result = (r["result"] or "").lower()
        res_s  = "W" if result == "win" else ("L" if result == "lose" else "D")
        y_labels.append(f"{type_s}  {date}  [{res_s}]")

        avg = _capped_avg(r["stars"], r["attacks"])
        avgs.append(avg)
        colors.append(_result_color(result) if r["attacks"] > 0 else _MISS)

    y       = list(range(len(wars_rev)))
    fig_h   = max(4, len(wars_rev) * 0.65)
    fig, ax = plt.subplots(figsize=(11, fig_h))
    _apply_dark(fig, ax)

    # Background bar representing the 0–3 scale
    ax.barh(
        y, [3.0] * len(y), color=_BG_INNER, edgecolor=_GRID,
        linewidth=0.7, height=0.55, zorder=1,
    )

    # Foreground bar: actual avg stars per attack
    bars: BarContainer = ax.barh(
        y, avgs, color=colors, alpha=0.85, height=0.55,
        edgecolor="#222244", linewidth=0.5, zorder=2,
    )

    # Annotation text
    for i, (bar, r) in enumerate(zip(list(bars.patches), wars_rev)):
        if r["attacks"] > 0:
            avg = avgs[i]
            ann = f"  {r['stars']}* / {r['attacks']}atk  ({avg:.2f} avg)"
        else:
            ann = f"  0 / {r['max_attacks']}atk  (missed all)"
        ax.text(
            max(avgs[i], 0.05),
            bar.get_y() + bar.get_height() / 2,
            ann, va="center", ha="left", fontsize=8.5, color=_FG_TEXT,
        )

    ax.set_yticks(y)
    ax.set_yticklabels(y_labels, fontsize=8.5, color=_FG_MUTED)
    ax.set_xlim(0, 3.4)
    ax.xaxis.set_major_locator(MultipleLocator(0.5))
    ax.xaxis.grid(True, color=_GRID, linestyle="--", linewidth=0.6, alpha=0.6)
    ax.set_axisbelow(True)

    ax.set_xlabel("Avg stars per attack", fontsize=10, color=_FG_MUTED, labelpad=6)
    ax.set_title(
        f"Last {len(wars)} Wars  -  {player_name} ({player_tag})",
        fontsize=11, color=_FG_TEXT, pad=10,
    )

    handles = [
        mpatches.Patch(color=_WIN,  label="Win"),
        mpatches.Patch(color=_LOSS, label="Loss"),
        mpatches.Patch(color=_DRAW, label="Draw / unknown"),
        mpatches.Patch(color=_MISS, label="Missed all attacks"),
    ]
    ax.legend(
        handles=handles, loc="lower right",
        facecolor=_GRID, edgecolor="#555577", labelcolor=_FG_TEXT, fontsize=9,
    )

    plt.tight_layout()
    return _to_bytes(fig)


# ─────────────────────────────────────────────────────────────────────────────
# Chart 3: Monthly Skill % trend
# ─────────────────────────────────────────────────────────────────────────────

def make_skill_chart_bytes(
    monthly_star_dist: List[Dict[str, Any]],
    player_name: str,
    player_tag: str,
) -> bytes:
    """
    Line/bar chart of Skill% per month.

    Skill = (3★ - 1★) / dist_attacks * 100 per month.
    CW:  blue line, one data point per calendar month.
    CWL: amber bars, one bar per CWL season (season_key from DB).
    Dashed reference line = overall skill across all wars.
    """
    from typing import Optional as _Opt

    @dataclass
    class _SkillAgg:
        three_star: int = 0
        one_star: int = 0
        dist_attacks: int = 0

    cw: DefaultDict[str, _SkillAgg] = defaultdict(_SkillAgg)
    cwl: DefaultDict[str, _SkillAgg] = defaultdict(_SkillAgg)

    for r in monthly_star_dist:
        if r["is_cwl"]:
            key = r["season_key"]
            cwl[key].three_star   += r["three_star"]
            cwl[key].one_star     += r["one_star"]
            cwl[key].dist_attacks += r["dist_attacks"]
        else:
            key = r["month"]
            cw[key].three_star    += r["three_star"]
            cw[key].one_star      += r["one_star"]
            cw[key].dist_attacks  += r["dist_attacks"]

    def _skill(d: _SkillAgg) -> "_Opt[float]":
        if d.dist_attacks == 0:
            return None
        return (d.three_star - d.one_star) / d.dist_attacks * 100

    all_months = sorted(set(cw) | set(cwl))
    if not all_months:
        fig, ax = plt.subplots(figsize=(6, 3))
        _apply_dark(fig, ax)
        ax.text(0.5, 0.5, "No data", ha="center", va="center",
                color=_FG_TEXT, transform=ax.transAxes)
        return _to_bytes(fig)

    n          = len(all_months)
    month_step = 1.28
    x          = [i * month_step for i in range(n)]
    labels     = [m[2:] if len(m) == 7 else m for m in all_months]

    cw_vals  = [_skill(cw[m])  if m in cw  else None for m in all_months]
    cwl_vals = [_skill(cwl[m]) if m in cwl else None for m in all_months]

    bar_w       = 0.24
    pair_offset = 0.15
    fig_w       = max(9, n * 1.15)
    fig, ax     = plt.subplots(figsize=(fig_w, 5))
    _apply_dark(fig, ax)

    cw_xi = [xi + pair_offset for xi, v in zip(x, cw_vals) if v is not None]
    cw_yi = [v for v in cw_vals if v is not None]
    if cw_xi:
        ax.plot(cw_xi, cw_yi, color=_CW, marker="o", markersize=6,
                linewidth=1.8, label="CW (per month)", zorder=2.5)
        for xi, yi in zip(cw_xi, cw_yi):
            ax.text(xi, yi + 2.5, f"{yi:.0f}%", ha="center", va="bottom",
                    fontsize=7.5, color=_CW, fontweight="bold")

    cwl_xi = [xi - pair_offset for xi, v in zip(x, cwl_vals) if v is not None]
    cwl_yi = [v for v in cwl_vals if v is not None]
    if cwl_xi:
        bars: BarContainer = ax.bar(
            cwl_xi, cwl_yi, width=bar_w, color=_CWL, alpha=0.85,
            label="CWL (per season)", zorder=3.5, edgecolor=_BG_OUTER, linewidth=0.5,
        )
        for b, v in zip(list(bars.patches), cwl_yi):
            ax.text(b.get_x() + b.get_width() / 2, v + 2.0,
                    f"{v:.0f}%", ha="center", va="bottom",
                    fontsize=7.5, color=_CWL, fontweight="bold", zorder=5)

    # Overall skill reference line
    all_three = sum(r["three_star"]   for r in monthly_star_dist)
    all_one   = sum(r["one_star"]     for r in monthly_star_dist)
    all_dist  = sum(r["dist_attacks"] for r in monthly_star_dist)
    overall_skill = (all_three - all_one) / all_dist * 100 if all_dist > 0 else 0.0
    ax.axhline(overall_skill, color=_FG_MUTED, linestyle="--", linewidth=1.0, alpha=0.65,
               label=f"Overall ({overall_skill:.0f}%)", zorder=2)
    ax.axhline(0, color="#555577", linestyle=":", linewidth=0.7, alpha=0.6, zorder=1)

    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8, color=_FG_MUTED)
    ax.margins(x=0.04)

    all_vals = cw_yi + cwl_yi
    y_max = max(105.0, (max(all_vals) + 10)) if all_vals else 105.0
    y_min = min(  0.0, (min(all_vals) - 10)) if all_vals else   0.0
    ax.set_ylim(y_min, y_max)
    ax.yaxis.set_major_locator(MultipleLocator(20))
    ax.yaxis.grid(True, color=_GRID, linestyle="--", linewidth=0.6, alpha=0.7)
    ax.set_axisbelow(True)

    ax.set_ylabel("Skill %  [(3★ − 1★) / attacks × 100]", fontsize=10, color=_FG_MUTED, labelpad=8)
    ax.set_xlabel("Month", fontsize=10, color=_FG_MUTED, labelpad=8)
    ax.set_title(
        f"Skill over Time  -  {player_name} ({player_tag})",
        fontsize=12, color=_FG_TEXT, pad=12,
    )
    ax.legend(loc="lower right", facecolor=_GRID, edgecolor="#555577",
              labelcolor=_FG_TEXT, fontsize=9)

    plt.tight_layout()
    return _to_bytes(fig)


# ─────────────────────────────────────────────────────────────────────────────
# Chart 4: Monthly Reliability % trend
# ─────────────────────────────────────────────────────────────────────────────

def make_reliability_chart_bytes(
    war_rows: List[Dict[str, Any]],
    player_name: str,
    player_tag: str,
) -> bytes:
    """
    Line/bar chart of Reliability% per month.

    Reliability = attacks_used / max_attacks * 100 per month.
    CW:  blue line, one data point per calendar month.
    CWL: amber bars, one bar per CWL season.
    Includes sentinel rows (attacks=0) so missed wars count against reliability.
    Dashed reference line = overall reliability across all wars.
    """
    @dataclass
    class _RelAgg:
        used: int = 0
        max_a: int = 0

    cw:  DefaultDict[str, _RelAgg] = defaultdict(_RelAgg)
    cwl: DefaultDict[str, _RelAgg] = defaultdict(_RelAgg)

    for r in war_rows:
        month = (r["date"] or "")[:7]
        if not month:
            continue
        if r["is_cwl"]:
            key = r["cwl_season"] if r["cwl_season"] else month
            cwl[key].used  += r["attacks"]
            cwl[key].max_a += r["max_attacks"]
        else:
            cw[month].used  += r["attacks"]
            cw[month].max_a += r["max_attacks"]

    all_months = sorted(set(cw) | set(cwl))
    if not all_months:
        fig, ax = plt.subplots(figsize=(6, 3))
        _apply_dark(fig, ax)
        ax.text(0.5, 0.5, "No data", ha="center", va="center",
                color=_FG_TEXT, transform=ax.transAxes)
        return _to_bytes(fig)

    n          = len(all_months)
    month_step = 1.28
    x          = [i * month_step for i in range(n)]
    labels     = [m[2:] if len(m) == 7 else m for m in all_months]

    def _rel(d: _RelAgg) -> "float | None":
        return d.used / d.max_a * 100 if d.max_a > 0 else None

    cw_vals  = [_rel(cw[m])  if m in cw  else None for m in all_months]
    cwl_vals = [_rel(cwl[m]) if m in cwl else None for m in all_months]

    bar_w       = 0.24
    pair_offset = 0.15
    fig_w       = max(9, n * 1.15)
    fig, ax     = plt.subplots(figsize=(fig_w, 5))
    _apply_dark(fig, ax)

    cw_xi = [xi + pair_offset for xi, v in zip(x, cw_vals) if v is not None]
    cw_yi = [v for v in cw_vals if v is not None]
    if cw_xi:
        ax.plot(cw_xi, cw_yi, color=_CW, marker="o", markersize=6,
                linewidth=1.8, label="CW (per month)", zorder=2.5)
        for xi, yi in zip(cw_xi, cw_yi):
            ax.text(xi, yi + 2.0, f"{yi:.0f}%", ha="center", va="bottom",
                    fontsize=7.5, color=_CW, fontweight="bold")

    cwl_xi = [xi - pair_offset for xi, v in zip(x, cwl_vals) if v is not None]
    cwl_yi = [v for v in cwl_vals if v is not None]
    if cwl_xi:
        bars2: BarContainer = ax.bar(
            cwl_xi, cwl_yi, width=bar_w, color=_CWL, alpha=0.85,
            label="CWL (per season)", zorder=3.5, edgecolor=_BG_OUTER, linewidth=0.5,
        )
        for b, v in zip(list(bars2.patches), cwl_yi):
            ax.text(b.get_x() + b.get_width() / 2, v + 1.5,
                    f"{v:.0f}%", ha="center", va="bottom",
                    fontsize=7.5, color=_CWL, fontweight="bold", zorder=5)

    # Overall reliability reference line
    all_used = sum(r["attacks"]     for r in war_rows)
    all_max  = sum(r["max_attacks"] for r in war_rows)
    overall_rel = all_used / all_max * 100 if all_max > 0 else 0.0
    ax.axhline(overall_rel, color=_FG_MUTED, linestyle="--", linewidth=1.0, alpha=0.65,
               label=f"Overall ({overall_rel:.0f}%)", zorder=2)

    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8, color=_FG_MUTED)
    ax.margins(x=0.04)
    ax.set_ylim(0, 110)
    ax.yaxis.set_major_locator(MultipleLocator(20))
    ax.yaxis.grid(True, color=_GRID, linestyle="--", linewidth=0.6, alpha=0.7)
    ax.set_axisbelow(True)

    ax.set_ylabel("Reliability %", fontsize=10, color=_FG_MUTED, labelpad=8)
    ax.set_xlabel("Month", fontsize=10, color=_FG_MUTED, labelpad=8)
    ax.set_title(
        f"Attack Reliability over Time  -  {player_name} ({player_tag})",
        fontsize=12, color=_FG_TEXT, pad=12,
    )
    ax.legend(loc="lower right", facecolor=_GRID, edgecolor="#555577",
              labelcolor=_FG_TEXT, fontsize=9)

    plt.tight_layout()
    return _to_bytes(fig)


# ─────────────────────────────────────────────────────────────────────────────
# Chart 5: Monthly Activity % trend
# ─────────────────────────────────────────────────────────────────────────────

def make_activity_chart_bytes(
    war_rows: List[Dict[str, Any]],
    player_name: str,
    player_tag: str,
    cwl_max_rounds: Optional[Dict[str, int]] = None,
) -> bytes:
    """
    Line/bar chart of Activity% per month.

    CW activity%  = CW attacks used / normalized monthly max (20 × days_avail/total_days).
    CWL activity% = CWL attacks used / actual season max rounds (from cwl_league_rounds DB
                    data), only for seasons where the 10-day window applies:
                    first month → include iff first_day ≤ 10;
                    current month → include iff today.day > 10;
                    all other months → always include.
                    Falls back to 7 if no DB data is available for the season.
    CW:  blue line, one data point per calendar month.
    CWL: amber bars, one bar per CWL season (excluded seasons produce no bar).
    Dashed reference line = overall activity using the same logic as the embed Overview.
    """
    from datetime import datetime as _dt

    _MONTHLY_MAX_CW  = 20
    _cwl_max: Dict[str, int] = cwl_max_rounds or {}

    @dataclass
    class _ActAgg:
        used: int = 0

    cw:  DefaultDict[str, _ActAgg] = defaultdict(_ActAgg)
    cwl: DefaultDict[str, _ActAgg] = defaultdict(_ActAgg)

    for r in war_rows:
        month = (r["date"] or "")[:7]
        if not month:
            continue
        if r["is_cwl"]:
            key = r["cwl_season"] if r["cwl_season"] else month
            cwl[key].used += r["attacks"]
        else:
            cw[month].used += r["attacks"]

    all_months = sorted(set(cw) | set(cwl))
    if not all_months:
        fig, ax = plt.subplots(figsize=(6, 3))
        _apply_dark(fig, ax)
        ax.text(0.5, 0.5, "No data", ha="center", va="center",
                color=_FG_TEXT, transform=ax.transAxes)
        return _to_bytes(fig)

    # ── Derive first-day and today for normalization ───────────────────────────
    _first_date = war_rows[-1]["date"] if war_rows and war_rows[-1]["date"] else ""
    _fm_str     = _first_date[:7]                                          # "YYYY-MM"
    _fd         = int(_first_date[8:10]) if len(_first_date) >= 10 else 1
    _now        = _dt.today()
    _cy, _cm, _cd = _now.year, _now.month, _now.day
    _today_str  = f"{_cy:04d}-{_cm:02d}"

    def _cw_max_norm(month_str: str) -> float:
        """Normalized CW denominator for the given 'YYYY-MM' month."""
        y, m        = int(month_str[:4]), int(month_str[5:7])
        total_days  = calendar.monthrange(y, m)[1]
        is_first    = month_str == _fm_str
        is_cur      = month_str == _today_str
        if is_first and is_cur:
            days = _cd - _fd + 1
        elif is_first:
            days = total_days - _fd + 1
        elif is_cur:
            days = _cd
        else:
            days = total_days
        return _MONTHLY_MAX_CW * days / total_days

    def _cwl_included(season_str: str) -> bool:
        """Whether the CWL 10-day window is covered for the given season 'YYYY-MM'."""
        is_first = season_str == _fm_str
        is_cur   = season_str == _today_str
        if is_first and is_cur:
            return _fd <= 10 and _cd > 10
        if is_first:
            return _fd <= 10
        if is_cur:
            return _cd > 10
        return True

    # ── Per-month activity values ──────────────────────────────────────────────
    n          = len(all_months)
    month_step = 1.28
    x          = [i * month_step for i in range(n)]
    labels     = [m[2:] if len(m) == 7 else m for m in all_months]

    cw_vals: List[float | None] = []
    for m in all_months:
        if m in cw:
            norm = _cw_max_norm(m)
            cw_vals.append(min(cw[m].used / norm * 100, 100.0) if norm > 0 else None)
        else:
            cw_vals.append(None)

    cwl_vals: List[float | None] = []
    for m in all_months:
        if m in cwl and _cwl_included(m):
            _season_max = _cwl_max.get(m, 7)
            cwl_vals.append(min(cwl[m].used / _season_max * 100, 100.0))
        else:
            cwl_vals.append(None)

    bar_w       = 0.24
    pair_offset = 0.15
    fig_w       = max(9, n * 1.15)
    fig, ax     = plt.subplots(figsize=(fig_w, 5))
    _apply_dark(fig, ax)

    cw_xi = [xi + pair_offset for xi, v in zip(x, cw_vals) if v is not None]
    cw_yi = [v for v in cw_vals if v is not None]
    if cw_xi:
        ax.plot(cw_xi, cw_yi, color=_CW, marker="o", markersize=6,
                linewidth=1.8, label="CW (per month)", zorder=2.5)
        for xi, yi in zip(cw_xi, cw_yi):
            ax.text(xi, yi + 2.0, f"{yi:.0f}%", ha="center", va="bottom",
                    fontsize=7.5, color=_CW, fontweight="bold")

    cwl_xi = [xi - pair_offset for xi, v in zip(x, cwl_vals) if v is not None]
    cwl_yi = [v for v in cwl_vals if v is not None]
    if cwl_xi:
        bars_act: BarContainer = ax.bar(
            cwl_xi, cwl_yi, width=bar_w, color=_CWL, alpha=0.85,
            label="CWL (per season)", zorder=3.5, edgecolor=_BG_OUTER, linewidth=0.5,
        )
        for b, v in zip(list(bars_act.patches), cwl_yi):
            ax.text(b.get_x() + b.get_width() / 2, v + 1.5,
                    f"{v:.0f}%", ha="center", va="bottom",
                    fontsize=7.5, color=_CWL, fontweight="bold", zorder=5)

    # ── Overall activity reference line (same month-by-month logic as embed) ───
    _fy, _fm_num = int(_fm_str[:4]), int(_fm_str[5:7])
    _max_cw  = 0.0
    _max_cwl = 0.0
    _cur = (_fy, _fm_num)
    _end = (_cy, _cm)
    while _cur <= _end:
        _y, _m      = _cur
        _total_days = calendar.monthrange(_y, _m)[1]
        _cur_str    = f"{_y:04d}-{_m:02d}"
        _is_first   = _cur_str == _fm_str
        _is_cur_m   = _cur_str == _today_str
        if _is_first and _is_cur_m:
            _days        = _cd - _fd + 1
            _inc         = _fd <= 10 and _cd > 10
        elif _is_first:
            _days        = _total_days - _fd + 1
            _inc         = _fd <= 10
        elif _is_cur_m:
            _days        = _cd
            _inc         = _cd > 10
        else:
            _days        = _total_days
            _inc         = True
        _max_cw  += _MONTHLY_MAX_CW * _days / _total_days
        if _inc:
            _max_cwl += _cwl_max.get(_cur_str, 7)
        _cur = (_y + 1, 1) if _m == 12 else (_y, _m + 1)

    _all_used   = sum(r["attacks"] for r in war_rows)
    _max_total  = _max_cw + _max_cwl
    overall_act = min(_all_used / _max_total * 100, 100.0) if _max_total > 0 else 0.0
    ax.axhline(overall_act, color=_FG_MUTED, linestyle="--", linewidth=1.0, alpha=0.65,
               label=f"Overall ({overall_act:.0f}%)", zorder=2)

    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8, color=_FG_MUTED)
    ax.margins(x=0.04)
    ax.set_ylim(0, 115)
    ax.yaxis.set_major_locator(MultipleLocator(20))
    ax.yaxis.grid(True, color=_GRID, linestyle="--", linewidth=0.6, alpha=0.7)
    ax.set_axisbelow(True)

    ax.set_ylabel("Activity %", fontsize=10, color=_FG_MUTED, labelpad=8)
    ax.set_xlabel("Month", fontsize=10, color=_FG_MUTED, labelpad=8)
    ax.set_title(
        f"Activity over Time  -  {player_name} ({player_tag})",
        fontsize=12, color=_FG_TEXT, pad=12,
    )
    ax.legend(loc="lower right", facecolor=_GRID, edgecolor="#555577",
              labelcolor=_FG_TEXT, fontsize=9)

    plt.tight_layout()
    return _to_bytes(fig)

