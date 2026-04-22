#!/usr/bin/env python3
"""
backtest_optimizer.py

4-way comparison per epoch: new+lv vs new (no late-vote) vs old vs oracle.
Scores each T-1 allocation against actual boundary votes/bribes.

Usage:
    venv/bin/python scripts/backtest_optimizer.py --voting-power 1774908
    venv/bin/python scripts/backtest_optimizer.py --voting-power 1774908 --recent-epochs 10
    venv/bin/python scripts/backtest_optimizer.py --voting-power 1774908 --csv analysis/backtest.csv

Optimizers compared:
  new+lv  — full current optimizer: ROI-ratio sort, ROI floor, denylist, competition mult,
             PLUS late-vote risk multipliers (Config.LATE_VOTE_RISK_MULTIPLIERS)
  new     — same as new+lv but with late-vote multipliers disabled (prior-session baseline)
  old     — raw-bribe sort, no ROI floor, no cap, 8-entry denylist
  oracle  — unconstrained optimizer on boundary truth (perfect information upper bound)
"""

import argparse
import csv
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from scipy.optimize import minimize

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS_DIR)

from config import Config
from config.settings import GAUGE_DENYLIST
from src.optimizer import VoteOptimizer, expected_return_usd
from analysis.recommender import _competition_multiplier
from preboundary_epoch_review import (
    load_target_epochs,
    load_preboundary_states,
    load_boundary_states,
    calculate_allocation_from_states,
)

logger = logging.getLogger(__name__)
console = Console()

# Old denylist = current 9-entry list minus the HYDX-USDC addition from the latest session
_HYDX_USDC_GAUGE = "0xac396cabf5832a49483b78225d902c0999829993"
OLD_DENYLIST: set = {g for g in GAUGE_DENYLIST if g.lower() != _HYDX_USDC_GAUGE}


@dataclass
class BacktestRow:
    epoch: int
    new_lv_scored: float        # new optimizer WITH late-vote multipliers
    new_scored: float           # new optimizer WITHOUT late-vote multipliers
    old_scored: float
    oracle_scored: float
    executed_scored: Optional[float]


# ── Data helpers ──────────────────────────────────────────────────────────────


def _compute_rolling_roi(
    live_conn: sqlite3.Connection,
    before_epoch: int,
    n_max: int = 7,
) -> Dict[str, float]:
    """Avg realized ROI/1k votes per gauge. Uses epochs strictly < before_epoch only."""
    runs = live_conn.execute(
        """
        SELECT ea.epoch, avr.id
        FROM executed_allocations ea
        JOIN auto_vote_runs avr
            ON ea.strategy_tag = 'auto_voter_run_' || avr.id
            AND avr.status = 'tx_success'
        WHERE ea.epoch < ?
        GROUP BY ea.epoch
        ORDER BY ea.epoch DESC
        LIMIT ?
        """,
        (int(before_epoch), int(n_max)),
    ).fetchall()

    roi_sum: Dict[str, float] = {}
    roi_count: Dict[str, int] = {}

    for epoch, run_id in runs:
        strategy_tag = f"auto_voter_run_{run_id}"
        exec_alloc = {
            r[0]: int(r[1])
            for r in live_conn.execute(
                "SELECT lower(gauge_address), executed_votes "
                "FROM executed_allocations "
                "WHERE epoch=? AND strategy_tag=? AND executed_votes > 0",
                (int(epoch), strategy_tag),
            ).fetchall()
        }
        bndry_votes = {
            r[0]: float(r[1] or 0.0)
            for r in live_conn.execute(
                "SELECT lower(gauge_address), votes_raw "
                "FROM boundary_gauge_values WHERE epoch=? AND active_only=1",
                (int(epoch),),
            ).fetchall()
        }
        bndry_bribes: Dict[str, float] = {}
        for gauge, usd in live_conn.execute(
            "SELECT lower(gauge_address), COALESCE(total_usd, 0.0) "
            "FROM boundary_reward_snapshots WHERE epoch=? AND active_only=1",
            (int(epoch),),
        ).fetchall():
            bndry_bribes[gauge] = bndry_bribes.get(gauge, 0.0) + float(usd or 0.0)

        for gauge, our_votes in exec_alloc.items():
            if our_votes <= 0:
                continue
            bribes = bndry_bribes.get(gauge, 0.0)
            if bribes <= 0:
                continue
            total_v = bndry_votes.get(gauge, 0.0)
            base_v = max(0.0, total_v - float(our_votes))
            ret = expected_return_usd(bribes, base_v, float(our_votes))
            roi_per_1k = ret / (our_votes / 1000.0)
            roi_sum[gauge] = roi_sum.get(gauge, 0.0) + roi_per_1k
            roi_count[gauge] = roi_count.get(gauge, 0) + 1

    return {g: roi_sum[g] / roi_count[g] for g in roi_sum}


def _score_allocation(
    alloc: Dict[str, int],
    boundary_lookup: Dict[str, Tuple[float, float]],
) -> float:
    """Score an allocation against boundary truth.

    Treats boundary_total_votes as the competition our votes face — consistent
    across new, old, oracle, and executed so they are all directly comparable.
    Formula: share_i = our_votes_i / (boundary_total_i + our_votes_i).
    """
    total = 0.0
    for gauge, our_votes in alloc.items():
        total_v, reward_usd = boundary_lookup.get(gauge.lower(), (0.0, 0.0))
        total += expected_return_usd(reward_usd, total_v, float(our_votes))
    return total


def _load_executed_allocation(
    live_conn: sqlite3.Connection,
    epoch: int,
) -> Optional[Dict[str, int]]:
    """Returns the executed gauge→votes dict for this epoch's tx_success run, or None."""
    vote_epoch = int(epoch) - 604800
    row = live_conn.execute(
        """
        SELECT id FROM auto_vote_runs
        WHERE vote_epoch = ? AND status = 'tx_success'
        ORDER BY completed_at DESC LIMIT 1
        """,
        (int(vote_epoch),),
    ).fetchone()
    if not row:
        return None
    run_id = row[0]
    strategy_tag = f"auto_voter_run_{run_id}"
    rows = live_conn.execute(
        """
        SELECT lower(gauge_address), SUM(executed_votes)
        FROM executed_allocations
        WHERE epoch = ? AND strategy_tag = ?
        GROUP BY lower(gauge_address)
        """,
        (int(epoch), strategy_tag),
    ).fetchall()
    if not rows:
        return None
    return {g: int(v or 0) for g, v in rows if v and int(v) > 0}


# ── Optimizer helpers ─────────────────────────────────────────────────────────


def _unconstrained_quadratic(
    candidates: List[Dict],
    voting_power: int,
) -> Dict[str, int]:
    """SLSQP with no per-pool cap. Candidates must be pre-filtered and pre-sliced."""
    if not candidates:
        return {}
    n = len(candidates)
    cv = np.array([g["current_votes"] for g in candidates], dtype=float)
    bv = np.array([g["bribes_usd"] for g in candidates], dtype=float)

    def objective(x: np.ndarray) -> float:
        denom = cv + x
        shares = np.where(denom > 0.0, x / denom, 0.0)
        return float(-np.dot(shares, bv))

    constraints = [{"type": "eq", "fun": lambda x: float(x.sum()) - float(voting_power)}]
    bounds = [(0.0, float(voting_power))] * n
    x0 = np.full(n, voting_power / n)
    try:
        res = minimize(
            objective,
            x0,
            method="SLSQP",
            bounds=bounds,
            constraints=constraints,
            options={"maxiter": 1000},
        )
        alloc = np.round(res.x).astype(int)
        diff = int(voting_power) - int(alloc.sum())
        if diff != 0:
            alloc[int(np.argmax(alloc))] += diff
        return {
            candidates[i]["address"]: int(alloc[i])
            for i in range(n)
            if int(alloc[i]) >= Config.MIN_VOTE_ALLOCATION
        }
    except Exception as exc:
        logger.warning("_unconstrained_quadratic failed: %s", exc)
        return {}


def _run_new_optimizer_no_lv(
    t1_states: List[Tuple],
    denylist_set: set,
    hist_roi: Dict[str, float],
    voting_power: int,
) -> Dict[str, int]:
    """New optimizer WITHOUT late-vote multipliers (prior-session baseline for comparison)."""
    orig_lv = Config.LATE_VOTE_RISK_MULTIPLIERS
    try:
        Config.LATE_VOTE_RISK_MULTIPLIERS = {}
        return _run_new_optimizer(t1_states, denylist_set, hist_roi, voting_power)
    finally:
        Config.LATE_VOTE_RISK_MULTIPLIERS = orig_lv


def _run_new_optimizer(
    t1_states: List[Tuple],
    denylist_set: set,
    hist_roi: Dict[str, float],
    voting_power: int,
) -> Dict[str, int]:
    """New optimizer WITH late-vote multipliers (current full configuration)."""
    gauge_data = [
        {
            "address": g,
            "pool": p,
            "current_votes": float(v) * _competition_multiplier(float(v)),
            "bribes_usd": float(b),
            "historical_roi_per_1k": hist_roi.get(g.lower(), 999.0),
        }
        for g, p, v, b in t1_states
        if g.lower() not in denylist_set and float(b) > 0
    ]
    if not gauge_data:
        logger.warning("_run_new_optimizer: no eligible gauges after denylist filter")
        return {}
    return VoteOptimizer(voting_power).quadratic_optimization(gauge_data)


def _run_old_optimizer(
    t1_states: List[Tuple],
    denylist_set: set,
    voting_power: int,
    top_k: int,
) -> Dict[str, int]:
    """Old optimizer: raw-bribe sort, no ROI floor, no per-pool cap, no competition multiplier."""
    candidates = [
        {"address": g, "pool": p, "current_votes": float(v), "bribes_usd": float(b)}
        for g, p, v, b in t1_states
        if g.lower() not in denylist_set and float(b) > 0
    ]
    candidates = sorted(candidates, key=lambda x: x["bribes_usd"], reverse=True)[:top_k]
    return _unconstrained_quadratic(candidates, voting_power)


def _run_oracle(
    boundary_states: List[Tuple],
    voting_power: int,
    top_k: int,
) -> Dict[str, int]:
    """Oracle: marginal allocation on boundary truth (perfect information upper bound).

    Uses calculate_allocation_from_states which sorts by expected return at a reference
    vote size then applies solve_marginal_allocation — same proven approach as
    preboundary_epoch_review.py's boundary-optimal baseline.
    """
    alloc_rows = calculate_allocation_from_states(
        states=boundary_states,
        voting_power=voting_power,
        top_k=top_k,
        candidate_pools=60,
        min_votes_per_pool=Config.MIN_VOTE_ALLOCATION,
    )
    return {row.gauge: row.alloc_votes for row in alloc_rows}


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Backtest new vs old optimizer vs oracle across recent epochs."
    )
    parser.add_argument(
        "--voting-power",
        type=int,
        default=int(os.getenv("YOUR_VOTING_POWER", "0") or "0"),
        help="Voting power (or set YOUR_VOTING_POWER env var)",
    )
    parser.add_argument(
        "--recent-epochs",
        type=int,
        default=10,
        help="Number of recent eligible epochs to analyze (default: 10)",
    )
    parser.add_argument(
        "--live-db",
        default="data/db/data.db",
        help="Main DB path (default: data/db/data.db)",
    )
    parser.add_argument(
        "--pre-db",
        default="data/db/preboundary_dev.db",
        help="Preboundary DB path (default: data/db/preboundary_dev.db)",
    )
    parser.add_argument(
        "--csv",
        default=None,
        metavar="PATH",
        help="Optional CSV output path",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    if args.voting_power <= 0:
        raise SystemExit(
            "Error: --voting-power must be > 0 (or set YOUR_VOTING_POWER env var)"
        )

    top_k = Config.MAX_GAUGES_TO_VOTE
    new_denylist = {g.lower() for g in GAUGE_DENYLIST}
    old_denylist = {g.lower() for g in OLD_DENYLIST}

    console.print(
        Panel.fit(
            "[bold cyan]Optimizer Backtest[/bold cyan]\n"
            f"Voting power: [green]{args.voting_power:,}[/green]  "
            f"Epochs: [green]{args.recent_epochs}[/green]  "
            f"Top-k: [green]{top_k}[/green]\n"
            f"New denylist: {len(new_denylist)} entries  |  "
            f"Old denylist: {len(old_denylist)} entries"
        )
    )

    live_conn = sqlite3.connect(args.live_db)
    pre_conn = sqlite3.connect(args.pre_db)

    try:
        epochs = load_target_epochs(
            conn=live_conn,
            pre_conn=pre_conn,
            recent_epochs=args.recent_epochs,
            explicit_epochs=None,
            decision_window="T-1",
            logger=logger,
        )
    except Exception as exc:
        live_conn.close()
        pre_conn.close()
        raise SystemExit(f"Failed to load eligible epochs: {exc}") from exc

    if not epochs:
        live_conn.close()
        pre_conn.close()
        raise SystemExit(
            "No eligible epochs found with both T-1 snapshot and boundary reward data."
        )

    console.print(f"[dim]Eligible epochs ({len(epochs)}): {epochs}[/dim]\n")

    rows: List[BacktestRow] = []

    for epoch in epochs:
        console.print(f"[dim]  Epoch {epoch}…[/dim]", end=" ")

        t1_states = load_preboundary_states(pre_conn, epoch, "T-1")
        boundary_states = load_boundary_states(live_conn, epoch)

        if not t1_states or not boundary_states:
            console.print("[yellow]skipped (missing data)[/yellow]")
            continue

        # boundary_lookup: gauge → (total_votes_raw, reward_usd)
        boundary_lookup: Dict[str, Tuple[float, float]] = {
            g: (v, b) for g, _p, v, b in boundary_states
        }

        hist_roi = _compute_rolling_roi(live_conn, before_epoch=int(epoch), n_max=7)
        logger.debug("Epoch %s: hist_roi populated for %d gauges", epoch, len(hist_roi))

        new_lv_alloc = _run_new_optimizer(t1_states, new_denylist, hist_roi, args.voting_power)
        new_alloc    = _run_new_optimizer_no_lv(t1_states, new_denylist, hist_roi, args.voting_power)
        old_alloc    = _run_old_optimizer(t1_states, old_denylist, args.voting_power, top_k)
        oracle_alloc = _run_oracle(boundary_states, args.voting_power, top_k)

        new_lv_scored = _score_allocation(new_lv_alloc, boundary_lookup)
        new_scored    = _score_allocation(new_alloc, boundary_lookup)
        old_scored    = _score_allocation(old_alloc, boundary_lookup)
        oracle_scored = _score_allocation(oracle_alloc, boundary_lookup)

        exec_alloc = _load_executed_allocation(live_conn, epoch)
        exec_scored: Optional[float] = (
            _score_allocation(exec_alloc, boundary_lookup) if exec_alloc else None
        )

        rows.append(
            BacktestRow(
                epoch=epoch,
                new_lv_scored=new_lv_scored,
                new_scored=new_scored,
                old_scored=old_scored,
                oracle_scored=oracle_scored,
                executed_scored=exec_scored,
            )
        )
        lv_delta = new_lv_scored - new_scored
        lv_str = f"[green]+{lv_delta:.2f}[/green]" if lv_delta >= 0 else f"[red]{lv_delta:.2f}[/red]"
        console.print(
            f"new+lv=[bold green]${new_lv_scored:.2f}[/bold green]  "
            f"new=[green]${new_scored:.2f}[/green]  Δlv={lv_str}  "
            f"old=[yellow]${old_scored:.2f}[/yellow]  "
            f"oracle=[cyan]${oracle_scored:.2f}[/cyan]"
        )

    live_conn.close()
    pre_conn.close()

    if not rows:
        raise SystemExit("No epochs processed — check DB paths and data availability.")

    # ── Rich results table ────────────────────────────────────────────────────
    console.print()
    tbl = Table(show_header=True, header_style="bold cyan", show_lines=False)
    tbl.add_column("Epoch", style="dim", width=12)
    tbl.add_column("Date (UTC)", width=12)
    tbl.add_column("New+LV ($)", justify="right", width=11)
    tbl.add_column("New ($)", justify="right", width=9)
    tbl.add_column("Old ($)", justify="right", width=9)
    tbl.add_column("Oracle ($)", justify="right", width=10)
    tbl.add_column("Δ LV/New", justify="right", width=10)
    tbl.add_column("Δ New/Old", justify="right", width=10)
    tbl.add_column("Regret", justify="right", width=9)

    for r in rows:
        dt = datetime.fromtimestamp(r.epoch, tz=timezone.utc).strftime("%Y-%m-%d")
        lv_delta = r.new_lv_scored - r.new_scored
        delta = r.new_scored - r.old_scored
        regret = r.oracle_scored - r.new_lv_scored
        lv_delta_str = (
            f"[bold green]+{lv_delta:.2f}[/bold green]" if lv_delta >= 0 else f"[bold red]{lv_delta:.2f}[/bold red]"
        )
        delta_str = (
            f"[green]+{delta:.2f}[/green]" if delta >= 0 else f"[red]{delta:.2f}[/red]"
        )
        exec_str = f"{r.executed_scored:.2f}" if r.executed_scored is not None else "—"
        tbl.add_row(
            str(r.epoch),
            dt,
            f"{r.new_lv_scored:.2f}",
            f"{r.new_scored:.2f}",
            f"{r.old_scored:.2f}",
            f"{r.oracle_scored:.2f}",
            lv_delta_str,
            delta_str,
            f"{regret:.2f}",
        )

    console.print(tbl)

    # ── Summary panel ─────────────────────────────────────────────────────────
    total_new_lv = sum(r.new_lv_scored for r in rows)
    total_new    = sum(r.new_scored for r in rows)
    total_old    = sum(r.old_scored for r in rows)
    total_oracle = sum(r.oracle_scored for r in rows)
    executed_rows = [r for r in rows if r.executed_scored is not None]
    total_exec = sum(r.executed_scored for r in executed_rows) if executed_rows else None

    lv_vs_new_pct = (
        ((total_new_lv - total_new) / total_new * 100.0) if total_new > 0 else 0.0
    )
    new_vs_old_pct = (
        ((total_new - total_old) / total_old * 100.0) if total_old > 0 else 0.0
    )
    lv_vs_old_pct = (
        ((total_new_lv - total_old) / total_old * 100.0) if total_old > 0 else 0.0
    )
    avg_lv_delta = (total_new_lv - total_new) / len(rows)
    avg_regret = (total_oracle - total_new_lv) / len(rows)

    exec_line = (
        f"  Executed total:        ${total_exec:>9.2f}  ({len(executed_rows)}/{len(rows)} epochs)\n"
        if total_exec is not None
        else ""
    )

    console.print(
        Panel(
            f"[bold]Summary — {len(rows)} epochs[/bold]\n\n"
            f"  New+LV optimizer total:  ${total_new_lv:>9.2f}\n"
            f"  New (no-LV) total:       ${total_new:>9.2f}\n"
            f"  Old optimizer total:     ${total_old:>9.2f}\n"
            f"  Oracle total:            ${total_oracle:>9.2f}\n"
            f"{exec_line}"
            f"\n"
            f"  Late-vote impact (LV vs New):  {lv_vs_new_pct:+.1f}%  (avg {avg_lv_delta:+.2f}/epoch)\n"
            f"  New+LV vs Old:                {lv_vs_old_pct:+.1f}%\n"
            f"  New (no-LV) vs Old:           {new_vs_old_pct:+.1f}%\n"
            f"  Avg regret (oracle gap):      ${avg_regret:.2f}/epoch",
            title="Backtest Results",
            border_style="cyan",
        )
    )

    # ── CSV export ────────────────────────────────────────────────────────────
    if args.csv:
        csv_dir = os.path.dirname(args.csv)
        if csv_dir:
            os.makedirs(csv_dir, exist_ok=True)
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "epoch",
                "date_utc",
                "new_lv_scored",
                "new_scored",
                "old_scored",
                "oracle_scored",
                "executed_scored",
                "delta_lv_new",
                "delta_new_old",
                "regret",
            ])
            for r in rows:
                dt = datetime.fromtimestamp(r.epoch, tz=timezone.utc).strftime("%Y-%m-%d")
                writer.writerow([
                    r.epoch,
                    dt,
                    f"{r.new_lv_scored:.6f}",
                    f"{r.new_scored:.6f}",
                    f"{r.old_scored:.6f}",
                    f"{r.oracle_scored:.6f}",
                    f"{r.executed_scored:.6f}" if r.executed_scored is not None else "",
                    f"{r.new_lv_scored - r.new_scored:.6f}",
                    f"{r.new_scored - r.old_scored:.6f}",
                    f"{r.oracle_scored - r.new_lv_scored:.6f}",
                ])
        console.print(f"\n[green]CSV written:[/green] {args.csv}")


if __name__ == "__main__":
    main()
