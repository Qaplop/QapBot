"""
Chart: Number of clans per war_league in the database, split by tracking status.

Source: clans table — actively tracked (track_war_updates=1) vs passively tracked (track_war_updates=0).
Output: qapbot/clans_per_league.jpg

Each bar shows the full clan count for a league, stacked into two segments:
  ■ solid fill   = actively tracked clans (subscribed or 22 h-polled, track_war_updates=1)
  ⊘ hatched fill = passively tracked clans (enemy-only, track_war_updates=0)
"""
import io
import os
import sqlite3
import sys
from typing import Union

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import matplotlib
matplotlib.use("Agg")  # non-interactive backend — no display needed
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
from datetime import datetime
from matplotlib.patches import Patch

from qapbot.config import CONFIG

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "clans_per_league.jpg")

# Canonical CWL league order (lowest → highest)
LEAGUE_ORDER = [
    "Bronze League III",
    "Bronze League II",
    "Bronze League I",
    "Silver League III",
    "Silver League II",
    "Silver League I",
    "Gold League III",
    "Gold League II",
    "Gold League I",
    "Crystal League III",
    "Crystal League II",
    "Crystal League I",
    "Master League III",
    "Master League II",
    "Master League I",
    "Champion League III",
    "Champion League II",
    "Champion League I",
    "Titan League III",
    "Titan League II",
    "Titan League I",
    "Legend League",
]

# Colour gradient from bronze → legend
LEAGUE_COLORS = {
    "Bronze League III":   "#cd7f32",
    "Bronze League II":    "#cd7f32",
    "Bronze League I":     "#cd7f32",
    "Silver League III":   "#a8a9ad",
    "Silver League II":    "#a8a9ad",
    "Silver League I":     "#a8a9ad",
    "Gold League III":     "#ffd700",
    "Gold League II":      "#ffd700",
    "Gold League I":       "#ffd700",
    "Crystal League III":  "#00d4ff",
    "Crystal League II":   "#00d4ff",
    "Crystal League I":    "#00d4ff",
    "Master League III":   "#b44be1",
    "Master League II":    "#b44be1",
    "Master League I":     "#b44be1",
    "Champion League III": "#e94560",
    "Champion League II":  "#e94560",
    "Champion League I":   "#e94560",
    "Titan League III":    "#ff8c00",
    "Titan League II":     "#ff8c00",
    "Titan League I":      "#ff8c00",
    "Legend League":       "#ffcc00",
}


def fetch_clans_per_league(db_path: str) -> dict[str, tuple[int, int]]:
    """
    Return {war_league: (actively_tracked_count, passively_tracked_count)} for every league in the DB.

    actively tracked   = track_war_updates = 1  (subscribed or 22 h-polled)
    passively tracked  = track_war_updates = 0  (enemy-only)
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT war_league,
                   SUM(CASE WHEN track_war_updates = 1 THEN 1 ELSE 0 END) AS tracked,
                   SUM(CASE WHEN track_war_updates = 0 THEN 1 ELSE 0 END) AS untracked
            FROM   clans
            WHERE  war_league IS NOT NULL
              AND  war_league != ''
            GROUP  BY war_league
            """
        ).fetchall()
        return {
            row["war_league"]: (int(row["tracked"]), int(row["untracked"]))
            for row in rows
        }
    finally:
        conn.close()


def _short_label(name: str) -> str:
    """Shorten league name for x-axis: 'Champion League I' → 'Champ I'."""
    parts = name.split()
    abbrevs = {"Bronze": "Bron", "Silver": "Silv", "Gold": "Gold",
               "Crystal": "Crys", "Master": "Mast", "Champion": "Champ"}
    if len(parts) >= 3:
        return f"{abbrevs.get(parts[0], parts[0])} {parts[2]}"
    return name


def _render_chart(
    data: dict[str, tuple[int, int]],
    save_target: "Union[str, io.BytesIO]",
) -> None:
    """
    Core rendering function shared by make_chart() and make_chart_bytes().

    data keys   = league name
    data values = (actively_tracked_count, passively_tracked_count)
    save_target = file path string  →  write JPEG to disk
                  io.BytesIO object →  write JPEG into buffer (no disk I/O)
    """
    # ── Sort leagues ──────────────────────────────────────────────────────────
    leagues = [l for l in LEAGUE_ORDER if l in data]
    unknown = sorted(k for k in data if k not in LEAGUE_ORDER)
    leagues += unknown

    labels           = [_short_label(l) for l in leagues]
    tracked_counts   = [data[l][0] for l in leagues]
    untracked_counts = [data[l][1] for l in leagues]
    totals           = [t + u for t, u in zip(tracked_counts, untracked_counts)]
    colors           = [LEAGUE_COLORS.get(l, "#888888") for l in leagues]
    max_total        = max(totals) if totals else 1

    # ── Figure setup ─────────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(16, 6))  # type: ignore[misc]
    fig.patch.set_facecolor("#1a1a2e")
    ax.set_facecolor("#16213e")

    # ── Bottom stack: actively tracked — solid fill ─────────────────────────
    ax.bar(  # type: ignore[misc]
        labels, tracked_counts,
        color=colors, edgecolor="#0f3460", linewidth=0.7, width=0.7,
    )

    # ── Top stack: passively tracked — hatched, dimmed ──────────────────────
    ax.bar(  # type: ignore[misc]
        labels, untracked_counts,
        bottom=tracked_counts,
        color=colors, alpha=0.45,
        hatch="//", edgecolor="#aaaaaa", linewidth=0.5, width=0.7,
    )

    # ── Total count labels above each bar ────────────────────────────────────
    offset = max_total * 0.012
    for i, total in enumerate(totals):
        ax.text(  # type: ignore[misc]
            i, total + offset,
            f"{total:,}",
            ha="center", va="bottom",
            fontsize=7.5, color="#e0e0e0", fontweight="bold",
        )

    # ── Legend ────────────────────────────────────────────────────────────────
    legend_handles = [
        Patch(facecolor="#888888", edgecolor="#0f3460",
              label="Actively tracked (subscribed / 22 h-polled)"),
        Patch(facecolor="#888888", alpha=0.45, hatch="//", edgecolor="#aaaaaa",
              label="Passively tracked (enemy-only)"),
    ]
    ax.legend(  # type: ignore[misc]
        handles=legend_handles, loc="upper right",
        facecolor="#0f3460", edgecolor="#555577",
        labelcolor="#e0e0e0", fontsize=9,
    )

    # ── Title / axis labels ───────────────────────────────────────────────────
    total_all      = sum(totals)
    total_tracked  = sum(tracked_counts)
    total_untracked = total_all - total_tracked
    now_label      = datetime.now().strftime("%Y-%m-%d %H:%M")
    ax.set_title(  # type: ignore[misc]
        f"Clans per War League in QapBot DB  ({now_label})"
        f"  —  {total_all:,} total ({total_tracked:,} actively tracked, {total_untracked:,} passively tracked)",
        fontsize=13, color="#e0e0e0", pad=14,
    )
    ax.set_xlabel("War League", fontsize=11, color="#a0a0c0", labelpad=8)  # type: ignore[misc]
    ax.set_ylabel("Number of Clans", fontsize=11, color="#a0a0c0", labelpad=8)  # type: ignore[misc]

    ax.tick_params(colors="#a0a0c0", labelsize=8)  # type: ignore[misc]
    plt.xticks(rotation=45, ha="right")  # type: ignore[misc]
    ax.yaxis.set_major_formatter(  # type: ignore[misc]
        ticker.FuncFormatter(lambda x, _: f"{int(x):,}")  # type: ignore[arg-type]
    )
    ax.set_ylim(0, max_total * 1.18)  # extra headroom for legend

    for spine in ax.spines.values():
        spine.set_edgecolor("#0f3460")

    ax.yaxis.grid(True, color="#0f3460", linestyle="--", linewidth=0.6, alpha=0.7)  # type: ignore[misc]
    ax.set_axisbelow(True)

    plt.tight_layout()
    fig.savefig(save_target, format="jpeg", dpi=150, bbox_inches="tight")  # type: ignore[misc]
    plt.close(fig)


def make_chart(data: dict[str, tuple[int, int]], output_path: str) -> None:
    """Render chart and save to *output_path* on disk."""
    _render_chart(data, output_path)


def make_chart_bytes(data: dict[str, tuple[int, int]]) -> bytes:
    """Render chart to an in-memory JPEG and return raw bytes (no disk I/O)."""
    buf = io.BytesIO()
    _render_chart(data, buf)
    buf.seek(0)
    return buf.read()


def main() -> None:
    db_path = CONFIG.db_path
    print(f"Reading from: {db_path}")

    if not os.path.exists(db_path):
        print(f"Database not found at {db_path}")
        sys.exit(1)

    data = fetch_clans_per_league(db_path)
    if not data:
        print("No data found in clans table (no war_league values).")
        sys.exit(1)

    total_tracked   = sum(t for t, _ in data.values())
    total_untracked = sum(u for _, u in data.values())
    total_all       = total_tracked + total_untracked
    print(
        f"Found {len(data)} leagues, {total_all:,} clans total "
        f"({total_tracked:,} actively tracked, {total_untracked:,} passively tracked)"
    )
    for league in LEAGUE_ORDER:
        if league in data:
            t, u = data[league]
            print(f"  {league:<25}: {t + u:>6,}  ({t:,} actively tracked, {u:,} passively tracked)")
    for league in sorted(k for k in data if k not in LEAGUE_ORDER):
        t, u = data[league]
        print(f"  {league:<25}: {t + u:>6,}  ({t:,} actively tracked, {u:,} passively tracked)  (unknown order)")

    make_chart(data, OUTPUT_PATH)
    print(f"\nChart saved to: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
