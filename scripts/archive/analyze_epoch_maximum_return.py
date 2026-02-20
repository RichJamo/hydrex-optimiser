#!/usr/bin/env python3
"""
Compute theoretical max return for a target epoch.

Outputs:
1) Exact 1-pool maximum (all votes to one pool)
2) Constrained K-pool maximum (default K=5) with minimum votes per pool

Notes:
- Uses epoch bribe USD totals from `bribes` table.
- Uses vote base from `votes` table (exact epoch if present, else latest <= epoch),
  then falls back to `gauges.current_votes`.
- K-pool search is exhaustive over top-N candidate pools (configurable).
"""

import argparse
import itertools
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Tuple

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()


@dataclass
class GaugeRow:
    gauge: str
    pool: str
    total_usd: float
    base_votes: float
    votes_source: str


def expected_return(total_usd: float, base_votes: float, your_votes: float) -> float:
    if your_votes <= 0:
        return 0.0
    denom = base_votes + your_votes
    if denom <= 0:
        return 0.0
    return total_usd * (your_votes / denom)


def parse_votes(v) -> float:
    if v is None:
        return 0.0
    try:
        val = float(v)
        return max(val, 0.0)
    except Exception:
        return 0.0


def fetch_epoch_rows(conn: sqlite3.Connection, epoch: int) -> List[GaugeRow]:
    query = """
    WITH epoch_bribes AS (
        SELECT
            b.gauge_address AS gauge,
            SUM(COALESCE(b.usd_value, 0)) AS total_usd
        FROM bribes b
        WHERE b.epoch = ?
        GROUP BY b.gauge_address
        HAVING SUM(COALESCE(b.usd_value, 0)) > 0
    )
    SELECT
        eb.gauge,
        COALESCE(g.pool, eb.gauge) AS pool,
        eb.total_usd,
        v_exact.total_votes AS exact_votes,
        v_prev.total_votes AS prev_votes,
        g.current_votes AS current_votes
    FROM epoch_bribes eb
    LEFT JOIN gauges g
        ON lower(g.address) = lower(eb.gauge)
    LEFT JOIN votes v_exact
        ON lower(v_exact.gauge) = lower(eb.gauge)
       AND v_exact.epoch = ?
    LEFT JOIN votes v_prev
        ON lower(v_prev.gauge) = lower(eb.gauge)
       AND v_prev.epoch = (
            SELECT MAX(v2.epoch)
            FROM votes v2
            WHERE lower(v2.gauge) = lower(eb.gauge)
              AND v2.epoch <= ?
        )
    ORDER BY eb.total_usd DESC
    """

    cur = conn.cursor()
    cur.execute(query, (epoch, epoch, epoch))
    rows = []
    for gauge, pool, total_usd, exact_votes, prev_votes, current_votes in cur.fetchall():
        if exact_votes is not None:
            base_votes = parse_votes(exact_votes)
            source = "votes.exact"
        elif prev_votes is not None:
            base_votes = parse_votes(prev_votes)
            source = "votes.prev"
        elif current_votes is not None:
            base_votes = parse_votes(current_votes)
            source = "gauges.current_votes"
        else:
            base_votes = 0.0
            source = "zero"

        rows.append(
            GaugeRow(
                gauge=gauge,
                pool=pool,
                total_usd=float(total_usd or 0.0),
                base_votes=base_votes,
                votes_source=source,
            )
        )
    return rows


def solve_alloc_for_set(gauges: List[GaugeRow], total_votes: int, min_per_pool: int) -> List[float]:
    """Continuous optimum for fixed set with x_i >= min_per_pool and sum x_i = total_votes."""
    k = len(gauges)
    if k == 0:
        return []
    if k * min_per_pool > total_votes:
        raise ValueError("Infeasible constraints: k * min_per_pool > total_votes")

    floors = [float(min_per_pool)] * k
    remaining = float(total_votes - k * min_per_pool)
    if remaining <= 0:
        return floors

    B = [g.total_usd for g in gauges]
    V = [max(g.base_votes, 0.0) for g in gauges]

    # Binary search lambda for KKT:
    # x_i = floor_i + max(0, sqrt(B_i*V_i/lambda) - V_i - floor_i)
    # Special case V_i == 0: marginal gain beyond floor is 0, so no extra allocation.
    def alloc_for_lambda(lmbd: float) -> List[float]:
        out = []
        for i in range(k):
            if V[i] <= 0 or B[i] <= 0:
                out.append(floors[i])
                continue
            x = math.sqrt((B[i] * V[i]) / lmbd) - V[i]
            if x < floors[i]:
                x = floors[i]
            out.append(x)
        return out

    # find hi such that sum alloc <= total_votes
    lo = 1e-18
    hi = 1.0
    for _ in range(120):
        alloc = alloc_for_lambda(hi)
        if sum(alloc) <= total_votes:
            break
        hi *= 2.0

    for _ in range(140):
        mid = (lo + hi) / 2.0
        alloc = alloc_for_lambda(mid)
        if sum(alloc) > total_votes:
            lo = mid
        else:
            hi = mid

    alloc = alloc_for_lambda(hi)
    s = sum(alloc)
    if s <= 0:
        return floors

    # Normalize tiny numeric drift to exact total
    if abs(s - total_votes) > 1e-9:
        active = [i for i in range(k) if alloc[i] > floors[i] + 1e-9]
        if active:
            factor = (total_votes - sum(floors[i] for i in range(k) if i not in active)) / sum(
                alloc[i] for i in active
            )
            for i in active:
                alloc[i] *= factor
        else:
            alloc = floors

    return alloc


def total_return(gauges: List[GaugeRow], alloc: List[float]) -> float:
    return sum(expected_return(g.total_usd, g.base_votes, x) for g, x in zip(gauges, alloc))


def best_one_pool(rows: List[GaugeRow], voting_power: int) -> Tuple[GaugeRow, float]:
    best = None
    best_ret = -1.0
    for r in rows:
        ret = expected_return(r.total_usd, r.base_votes, voting_power)
        if ret > best_ret:
            best = r
            best_ret = ret
    return best, best_ret


def best_k_pool(
    rows: List[GaugeRow],
    voting_power: int,
    k: int,
    min_per_pool: int,
    candidate_count: int,
) -> Tuple[List[GaugeRow], List[float], float, int]:
    if k <= 0:
        raise ValueError("k must be positive")
    if k * min_per_pool > voting_power:
        raise ValueError("Infeasible constraints: k * min_per_pool > voting_power")

    scored = sorted(
        rows,
        key=lambda r: expected_return(r.total_usd, r.base_votes, voting_power),
        reverse=True,
    )
    candidates = scored[: max(k, min(candidate_count, len(scored)))]

    best_combo: List[GaugeRow] = []
    best_alloc: List[float] = []
    best_ret = -1.0
    combos = 0

    for combo in itertools.combinations(candidates, k):
        combos += 1
        combo_list = list(combo)
        alloc = solve_alloc_for_set(combo_list, voting_power, min_per_pool)
        ret = total_return(combo_list, alloc)
        if ret > best_ret:
            best_ret = ret
            best_combo = combo_list
            best_alloc = alloc

    return best_combo, best_alloc, best_ret, combos


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze theoretical max return for an epoch")
    parser.add_argument("--db", default="data.db", help="Path to sqlite DB")
    parser.add_argument("--epoch", type=int, default=None, help="Target epoch timestamp")
    parser.add_argument("--voting-power", type=int, default=1_183_272)
    parser.add_argument("--k", type=int, default=5, help="Number of pools for constrained allocation")
    parser.add_argument("--min-votes-per-pool", type=int, default=50_000)
    parser.add_argument(
        "--candidate-pools",
        type=int,
        default=30,
        help="Top-N candidates for exhaustive K-pool search",
    )
    parser.add_argument("--baseline-return", type=float, default=954.53)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    cur = conn.cursor()

    if args.epoch is None:
        cur.execute("SELECT MAX(epoch) FROM bribes")
        args.epoch = int(cur.fetchone()[0])

    rows = fetch_epoch_rows(conn, args.epoch)
    if not rows:
        console.print("[red]No bribe rows found for epoch[/red]")
        return

    source_counts: Dict[str, int] = {}
    for r in rows:
        source_counts[r.votes_source] = source_counts.get(r.votes_source, 0) + 1

    console.print(
        Panel.fit(
            "[bold cyan]Epoch Theoretical Maximum Return[/bold cyan]\n"
            f"Epoch: {args.epoch} ({datetime.utcfromtimestamp(args.epoch).isoformat()} UTC)\n"
            f"Voting Power: {args.voting_power:,} | Pools with bribes: {len(rows)}",
            border_style="cyan",
        )
    )

    src_str = ", ".join(f"{k}={v}" for k, v in sorted(source_counts.items()))
    console.print(f"[cyan]Vote base source usage:[/cyan] {src_str}")

    one_pool, one_pool_return = best_one_pool(rows, args.voting_power)

    combo, alloc, k_return, combos = best_k_pool(
        rows,
        args.voting_power,
        args.k,
        args.min_votes_per_pool,
        args.candidate_pools,
    )

    summary = Table(show_header=True, header_style="bold cyan")
    summary.add_column("Scenario", width=24)
    summary.add_column("Expected Return", justify="right", width=18)
    summary.add_column("vs Baseline", justify="right", width=14)
    summary.add_column("Notes", width=54)

    baseline = args.baseline_return
    one_vs = ((one_pool_return / baseline) - 1) * 100 if baseline > 0 else 0.0
    k_vs = ((k_return / baseline) - 1) * 100 if baseline > 0 else 0.0

    summary.add_row(
        "1-pool maximum (exact)",
        f"${one_pool_return:,.2f}",
        f"{one_vs:+.2f}%",
        f"All {args.voting_power:,} votes to best single pool",
    )
    summary.add_row(
        f"{args.k}-pool maximum",
        f"${k_return:,.2f}",
        f"{k_vs:+.2f}%",
        f"x_i >= {args.min_votes_per_pool:,}, searched {combos:,} combos from top {args.candidate_pools}",
    )

    console.print()
    console.print(summary)

    one_tbl = Table(show_header=True, header_style="bold yellow")
    one_tbl.add_column("Best Pool", width=44)
    one_tbl.add_column("Gauge", width=16)
    one_tbl.add_column("Base Votes", justify="right", width=16)
    one_tbl.add_column("Total Bribes", justify="right", width=14)
    one_tbl.add_column("Expected", justify="right", width=14)
    one_tbl.add_row(
        one_pool.pool,
        one_pool.gauge[:14] + "..",
        f"{one_pool.base_votes:,.0f}",
        f"${one_pool.total_usd:,.2f}",
        f"${one_pool_return:,.2f}",
    )

    console.print()
    console.print("[bold yellow]Best 1-pool allocation[/bold yellow]")
    console.print(one_tbl)

    k_tbl = Table(show_header=True, header_style="bold green")
    k_tbl.add_column("Pool", width=44)
    k_tbl.add_column("Gauge", width=16)
    k_tbl.add_column("Alloc Votes", justify="right", width=14)
    k_tbl.add_column("Base Votes", justify="right", width=16)
    k_tbl.add_column("Bribes", justify="right", width=12)
    k_tbl.add_column("Expected", justify="right", width=12)

    combo_returns = []
    for g, x in zip(combo, alloc):
        ret = expected_return(g.total_usd, g.base_votes, x)
        combo_returns.append(ret)
        k_tbl.add_row(
            g.pool,
            g.gauge[:14] + "..",
            f"{x:,.0f}",
            f"{g.base_votes:,.0f}",
            f"${g.total_usd:,.2f}",
            f"${ret:,.2f}",
        )

    console.print()
    console.print(f"[bold green]Best {args.k}-pool allocation (min {args.min_votes_per_pool:,} each)[/bold green]")
    console.print(k_tbl)
    console.print(f"[bold]Total expected ({args.k}-pool): ${sum(combo_returns):,.2f}[/bold]")

    console.print(
        "\n[dim]Assumptions: bribe totals fixed, no strategic reaction from other voters, "
        "and K-pool result is exhaustive within candidate set.[/dim]"
    )

    conn.close()


if __name__ == "__main__":
    main()
