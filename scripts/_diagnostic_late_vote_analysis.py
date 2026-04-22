#!/usr/bin/env python3
"""
Per-pool analysis of late-vote (T-60s → boundary) behaviour.

For each pool with >= 3 epochs of data, computes:
  - avg_late_pct:    average % vote increase in the final 60s
  - avg_dilution:    average % ROI/1k reduction caused by those late votes
  - median_votes:    median T-60s vote level (pool size proxy)
  - median_reward:   median boundary reward USD

Usage:
    venv/bin/python scripts/_diagnostic_late_vote_analysis.py
    venv/bin/python scripts/_diagnostic_late_vote_analysis.py --min-epochs 2 --min-votes 5000
"""

import argparse
import logging
import os
import sqlite3
import sys
from collections import defaultdict

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from rich.console import Console
from rich.table import Table

logger = logging.getLogger(__name__)
console = Console()


def roi_per_1k(reward: float, base_votes: float) -> float:
    denom = base_votes + 1000.0
    if denom <= 0:
        return 0.0
    return reward * (1000.0 / denom)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Per-pool late-vote (T-60s to boundary) dilution analysis."
    )
    parser.add_argument("--live-db", default="data/db/data.db")
    parser.add_argument("--pre-db", default="data/db/preboundary_dev.db")
    parser.add_argument("--min-epochs", type=int, default=3,
                        help="Minimum epochs of data required to include a pool (default: 3)")
    parser.add_argument("--min-votes", type=float, default=10_000,
                        help="Minimum T-60s votes to include an observation (default: 10000)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    live = sqlite3.connect(args.live_db)
    pre = sqlite3.connect(args.pre_db)

    # Pool names
    pool_names = {}
    try:
        for r in live.execute(
            "SELECT lower(pool_address), pool_name FROM pool_metadata WHERE pool_name IS NOT NULL"
        ).fetchall():
            pool_names[r[0]] = r[1]
    except sqlite3.OperationalError:
        pass

    # T-60s snapshots
    snap = {}
    for r in pre.execute(
        """
        SELECT lower(gauge_address), lower(COALESCE(pool_address, gauge_address)),
               epoch, votes_now_raw, rewards_now_usd
        FROM preboundary_snapshots
        WHERE decision_window = 'T-1'
        """
    ).fetchall():
        snap[(r[0], r[2])] = {
            "pool": r[1],
            "snap_v": float(r[3] or 0),
            "snap_r": float(r[4] or 0),
        }

    # Boundary votes
    bv = {}
    for r in live.execute(
        "SELECT lower(gauge_address), epoch, votes_raw FROM boundary_gauge_values WHERE active_only=1"
    ).fetchall():
        bv[(r[0], r[1])] = float(r[2] or 0)

    # Boundary rewards (proper aggregation)
    br = {}
    for r in live.execute(
        """
        SELECT lower(gauge_address), epoch, SUM(COALESCE(total_usd, 0))
        FROM boundary_reward_snapshots
        WHERE active_only = 1
        GROUP BY gauge_address, epoch
        """
    ).fetchall():
        br[(r[0], r[1])] = float(r[2] or 0)

    # Per-pool aggregation
    pool_data: dict = defaultdict(lambda: {
        "epochs": [],
        "late_vote_pcts": [],
        "dilution_pcts": [],
        "snap_votes": [],
        "rewards": [],
        "gauge": None,
    })

    for (gauge, epoch), s in snap.items():
        b_v = bv.get((gauge, epoch))
        if b_v is None or s["snap_v"] < args.min_votes:
            continue

        reward = br.get((gauge, epoch), 0.0)
        if reward <= 0:
            reward = s["snap_r"]  # fall back to snapshot value
        if reward <= 0:
            continue

        late_pct = (b_v - s["snap_v"]) / s["snap_v"] * 100.0
        roi_t60 = roi_per_1k(reward, s["snap_v"])
        roi_bnd = roi_per_1k(reward, b_v)
        dilution_pct = ((roi_bnd - roi_t60) / roi_t60 * 100.0) if roi_t60 > 0 else 0.0

        pool = s["pool"]
        pool_data[pool]["epochs"].append(epoch)
        pool_data[pool]["late_vote_pcts"].append(late_pct)
        pool_data[pool]["dilution_pcts"].append(dilution_pct)
        pool_data[pool]["snap_votes"].append(s["snap_v"])
        pool_data[pool]["rewards"].append(reward)
        pool_data[pool]["gauge"] = gauge

    # Build summary rows
    rows = []
    for pool, d in pool_data.items():
        n = len(d["epochs"])
        if n < args.min_epochs:
            continue

        avg_late = sum(d["late_vote_pcts"]) / n
        avg_dil = sum(d["dilution_pcts"]) / n
        worst_dil = min(d["dilution_pcts"])  # most negative = worst
        pct_epochs_moved = sum(1 for x in d["late_vote_pcts"] if x > 5.0) / n * 100
        sorted_v = sorted(d["snap_votes"])
        median_votes = sorted_v[n // 2]
        sorted_r = sorted(d["rewards"])
        median_reward = sorted_r[n // 2]
        name = pool_names.get(pool, pool[:16] + "...")
        rows.append((pool, name, n, avg_late, avg_dil, worst_dil,
                     pct_epochs_moved, median_votes, median_reward))

    # Sort by avg dilution (worst first)
    rows.sort(key=lambda x: x[4])

    # Rich table
    tbl = Table(
        show_header=True, header_style="bold cyan",
        title="Per-Pool Late-Vote Dilution  (T-60s → boundary)",
        caption=f"min_epochs={args.min_epochs}  min_votes={args.min_votes:,.0f}",
    )
    tbl.add_column("Pool", style="dim", width=18)
    tbl.add_column("Name", width=22)
    tbl.add_column("N", justify="right", width=3)
    tbl.add_column("Avg late%", justify="right", width=10)
    tbl.add_column("Avg dil%", justify="right", width=9)
    tbl.add_column("Worst dil%", justify="right", width=10)
    tbl.add_column(">5% late\nepochs", justify="right", width=8)
    tbl.add_column("Med votes", justify="right", width=12)
    tbl.add_column("Med rew$", justify="right", width=10)

    for pool, name, n, avg_late, avg_dil, worst_dil, pct_moved, med_v, med_r in rows:
        dil_str = f"{avg_dil:.1f}%"
        dil_style = "red" if avg_dil < -15 else ("yellow" if avg_dil < -5 else "green")
        tbl.add_row(
            pool[:16] + "...",
            name,
            str(n),
            f"{avg_late:+.1f}%",
            f"[{dil_style}]{dil_str}[/{dil_style}]",
            f"{worst_dil:.1f}%",
            f"{pct_moved:.0f}%",
            f"{med_v:>12,.0f}",
            f"${med_r:,.2f}",
        )

    console.print(tbl)

    # Summary
    high_risk = [(name, avg_dil, med_v) for _, name, n, _, avg_dil, _, pct_moved, med_v, _ in rows
                 if avg_dil < -10]
    if high_risk:
        console.print("\n[bold yellow]High-risk pools (avg dilution < -10%):[/bold yellow]")
        for name, avg_dil, med_v in sorted(high_risk, key=lambda x: x[1]):
            console.print(f"  {name:<24} avg dilution={avg_dil:.1f}%  med_votes={med_v:,.0f}")

    live.close()
    pre.close()


if __name__ == "__main__":
    main()
