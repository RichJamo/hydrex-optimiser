#!/usr/bin/env python3
"""
Weekly post-boundary review:
- Compare predicted vs executed vs optimal allocation returns
- Persist performance metrics to allocation_performance_metrics
"""

import argparse
import os
import sqlite3
import sys
import time
from itertools import combinations
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from web3 import Web3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from analyze_boundary_maximum_return import GaugeBoundaryState, expected_return, solve_alloc_for_set
from config.settings import DATABASE_PATH
from src.allocation_tracking import ensure_allocation_tracking_tables, save_performance_metrics

load_dotenv()
console = Console()

POOL_ABI = [
    {
        "inputs": [],
        "name": "token0",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "token1",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]

ERC20_SYMBOL_ABI = [
    {
        "inputs": [],
        "name": "symbol",
        "outputs": [{"internalType": "string", "name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    }
]


def short_address(value: str) -> str:
    value = str(value or "")
    if len(value) < 12:
        return value
    return f"{value[:10]}...{value[-6:]}"


def get_token_symbol(conn: sqlite3.Connection, token_address: str) -> Optional[str]:
    row = conn.cursor().execute(
        "SELECT symbol FROM token_metadata WHERE lower(token_address)=lower(?)",
        (str(token_address),),
    ).fetchone()
    if not row or not row[0]:
        return None
    symbol = str(row[0]).strip()
    if not symbol or "..." in symbol:
        return None
    return symbol


def resolve_token_symbol(conn: sqlite3.Connection, w3: Optional[Web3], token_address: str) -> Optional[str]:
    symbol = get_token_symbol(conn, token_address)
    if symbol:
        return symbol
    if not w3:
        return None
    try:
        token = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=ERC20_SYMBOL_ABI)
        symbol = token.functions.symbol().call()
        if isinstance(symbol, bytes):
            symbol = symbol.decode("utf-8", errors="ignore").rstrip("\x00")
        symbol = str(symbol).strip()
        if symbol:
            return symbol
    except Exception:
        return None
    return None


def resolve_pool_label(conn: sqlite3.Connection, w3: Optional[Web3], pool_address: str) -> str:
    if not pool_address:
        return "unknown"
    if not w3:
        return short_address(pool_address)

    try:
        pool = w3.eth.contract(address=Web3.to_checksum_address(pool_address), abi=POOL_ABI)
        token0 = pool.functions.token0().call()
        token1 = pool.functions.token1().call()
        sym0 = resolve_token_symbol(conn, w3, token0)
        sym1 = resolve_token_symbol(conn, w3, token1)
        if sym0 and sym1:
            return f"{sym0}/{sym1}"
    except Exception:
        pass
    return short_address(pool_address)


def n_choose_k(n: int, k: int) -> int:
    if k < 0 or k > n:
        return 0
    k = min(k, n - k)
    value = 1
    for i in range(1, k + 1):
        value = (value * (n - k + i)) // i
    return value


def resolve_epoch(conn: sqlite3.Connection, requested_epoch: int) -> int:
    if requested_epoch > 0:
        return int(requested_epoch)
    row = conn.cursor().execute("SELECT MAX(epoch) FROM epoch_boundaries").fetchone()
    if not row or row[0] is None:
        raise ValueError("No epoch boundaries found")
    return int(row[0])


def load_boundary_states(conn: sqlite3.Connection, epoch: int) -> List[GaugeBoundaryState]:
    rows = conn.cursor().execute(
        """
        SELECT lower(gauge_address), lower(pool_address),
               CAST(votes_raw AS REAL), CAST(total_usd AS REAL)
        FROM boundary_gauge_values
        WHERE epoch = ? AND COALESCE(active_only, 1) = 1
        """,
        (int(epoch),),
    ).fetchall()

    states: List[GaugeBoundaryState] = []
    for gauge, pool, votes_raw, total_usd in rows:
        states.append(
            GaugeBoundaryState(
                gauge=str(gauge),
                pool=str(pool),
                votes_raw=float(votes_raw or 0.0),
                total_usd=float(total_usd or 0.0),
            )
        )
    return states


def load_allocation(conn: sqlite3.Connection, table_name: str, epoch: int, strategy_tag: str, vote_column: str) -> Dict[str, int]:
    query = f"""
        SELECT lower(gauge_address), CAST({vote_column} AS INTEGER)
        FROM {table_name}
        WHERE epoch = ? AND strategy_tag = ?
        ORDER BY rank ASC
    """
    rows = conn.cursor().execute(query, (int(epoch), str(strategy_tag))).fetchall()
    return {str(g): int(v or 0) for g, v in rows if g}


def compute_portfolio_return(states_by_gauge: Dict[str, GaugeBoundaryState], allocation: Dict[str, int]) -> float:
    total = 0.0
    for gauge, votes in allocation.items():
        state = states_by_gauge.get(str(gauge).lower())
        if not state:
            continue
        total += expected_return(state.total_usd, state.votes_raw, float(votes))
    return float(total)


def render_allocation_table(
    title: str,
    conn: sqlite3.Connection,
    w3: Optional[Web3],
    states_by_gauge: Dict[str, GaugeBoundaryState],
    allocation: Dict[str, int],
) -> None:
    table = Table(title=title)
    table.add_column("Rank", justify="right", style="cyan")
    table.add_column("Pool", style="green")
    table.add_column("Gauge", style="cyan")
    table.add_column("Alloc Votes", justify="right", style="yellow")
    table.add_column("Total Votes", justify="right")
    table.add_column("Total Rewards", justify="right")
    table.add_column("Expected To Us", justify="right", style="bold green")
    table.add_column("Expected $/1k Votes", justify="right", style="bold green")

    rows_added = 0
    total_expected = 0.0
    total_alloc_votes = 0.0
    for rank, (gauge, alloc_votes) in enumerate(allocation.items(), start=1):
        state = states_by_gauge.get(str(gauge).lower())
        if not state:
            continue
        expected_usd = float(expected_return(state.total_usd, state.votes_raw, float(alloc_votes)))
        expected_per_1k_votes = (expected_usd * 1000.0) / max(1.0, float(alloc_votes))
        pool_label = resolve_pool_label(conn, w3, state.pool)
        table.add_row(
            str(rank),
            pool_label,
            short_address(state.gauge),
            f"{float(alloc_votes):,.0f}",
            f"{state.votes_raw:,.0f}",
            f"${state.total_usd:,.2f}",
            f"${expected_usd:,.2f}",
            f"${expected_per_1k_votes:,.2f}",
        )
        rows_added += 1
        total_expected += expected_usd
        total_alloc_votes += float(alloc_votes)

    if rows_added == 0:
        table.add_row("-", "(no rows)", "-", "0", "0", "$0.00", "$0.00", "$0.00")
    else:
        total_expected_per_1k = (total_expected * 1000.0) / max(1.0, total_alloc_votes)
        table.add_row(
            "",
            "[bold]TOTAL[/bold]",
            "",
            f"[bold]{total_alloc_votes:,.0f}[/bold]",
            "",
            "",
            f"[bold]${total_expected:,.2f}[/bold]",
            f"[bold]${total_expected_per_1k:,.2f}[/bold]",
        )

    console.print(table)


def compute_optimal_return(
    states: List[GaugeBoundaryState],
    voting_power: int,
    k: int,
    candidate_pools: int,
    min_votes_per_pool: int,
) -> Tuple[float, List[Tuple[GaugeBoundaryState, float, float]]]:
    if not states or voting_power <= 0:
        return 0.0, []

    ranked = sorted(
        states,
        key=lambda s: expected_return(s.total_usd, s.votes_raw, float(voting_power)),
        reverse=True,
    )
    candidates = ranked[: max(int(k), min(int(candidate_pools), len(ranked)))]

    effective_k = min(int(k), len(candidates))
    if effective_k <= 0:
        return 0.0, []

    best_return = -1.0
    best_combo = None
    best_alloc = None

    for combo in combinations(candidates, effective_k):
        alloc = solve_alloc_for_set(list(combo), int(voting_power), int(min_votes_per_pool))
        portfolio_return = sum(expected_return(s.total_usd, s.votes_raw, x) for s, x in zip(combo, alloc))
        if portfolio_return > best_return:
            best_return = float(portfolio_return)
            best_combo = list(combo)
            best_alloc = alloc

    if not best_combo or not best_alloc:
        return 0.0, []

    details = [
        (s, float(x), float(expected_return(s.total_usd, s.votes_raw, float(x))))
        for s, x in zip(best_combo, best_alloc)
    ]
    return float(best_return), details


def compute_optimal_return_for_k(
    candidates: List[GaugeBoundaryState],
    voting_power: int,
    k: int,
    min_votes_per_pool: int,
) -> Tuple[float, List[Tuple[GaugeBoundaryState, float, float]]]:
    if not candidates or voting_power <= 0 or k <= 0 or k > len(candidates):
        return 0.0, []

    best_return = -1.0
    best_combo = None
    best_alloc = None

    for combo in combinations(candidates, int(k)):
        alloc = solve_alloc_for_set(list(combo), int(voting_power), int(min_votes_per_pool))
        portfolio_return = sum(expected_return(s.total_usd, s.votes_raw, x) for s, x in zip(combo, alloc))
        if portfolio_return > best_return:
            best_return = float(portfolio_return)
            best_combo = list(combo)
            best_alloc = alloc

    if not best_combo or not best_alloc:
        return 0.0, []

    details = [
        (s, float(x), float(expected_return(s.total_usd, s.votes_raw, float(x))))
        for s, x in zip(best_combo, best_alloc)
    ]
    return float(best_return), details


def main() -> None:
    parser = argparse.ArgumentParser(description="Weekly allocation review: predicted vs executed vs optimal")
    parser.add_argument("--db-path", default=DATABASE_PATH, help="SQLite DB path")
    parser.add_argument("--epoch", type=int, default=0, help="Epoch to review (default: latest in epoch_boundaries)")
    parser.add_argument("--strategy-tag", default="manual", help="Strategy tag for predicted/executed rows")
    parser.add_argument("--voting-power", type=int, default=int(os.getenv("YOUR_VOTING_POWER", "0")), help="Voting power")
    parser.add_argument("--k", type=int, default=5, help="Max number of pools in optimal allocation")
    parser.add_argument("--candidate-pools", type=int, default=20, help="Candidate pool count for optimal search")
    parser.add_argument("--min-votes-per-pool", type=int, default=int(os.getenv("MIN_VOTE_ALLOCATION", "1000")))
    parser.add_argument(
        "--k-sweep-max",
        type=int,
        default=int(os.getenv("MAX_GAUGES_TO_VOTE", "10")),
        help="Max k to include in k-sweep table (default: MAX_GAUGES_TO_VOTE)",
    )
    parser.add_argument(
        "--k-sweep-max-combos",
        type=int,
        default=300000,
        help="Skip k values whose combination count exceeds this threshold (default: 300000)",
    )
    parser.add_argument(
        "--k-sweep-unbounded",
        action="store_true",
        help="Do not skip k values by combination-count threshold",
    )
    parser.add_argument(
        "--summary-k-mode",
        choices=["input-k", "best-sweep"],
        default="best-sweep",
        help="Which optimal value is written to summary/metrics (default: best-sweep)",
    )
    args = parser.parse_args()

    if args.voting_power <= 0:
        console.print("[red]--voting-power must be > 0[/red]")
        sys.exit(1)

    conn = sqlite3.connect(args.db_path)

    try:
        ensure_allocation_tracking_tables(conn)
        epoch = resolve_epoch(conn, args.epoch)

        states = load_boundary_states(conn, epoch)
        if not states:
            console.print(f"[red]No boundary_gauge_values rows found for epoch={epoch}[/red]")
            sys.exit(1)

        states_by_gauge = {s.gauge.lower(): s for s in states}

        predicted = load_allocation(
            conn=conn,
            table_name="predicted_allocations",
            epoch=epoch,
            strategy_tag=args.strategy_tag,
            vote_column="predicted_votes",
        )
        executed = load_allocation(
            conn=conn,
            table_name="executed_allocations",
            epoch=epoch,
            strategy_tag=args.strategy_tag,
            vote_column="executed_votes",
        )

        predicted_return = compute_portfolio_return(states_by_gauge, predicted)
        executed_return = compute_portfolio_return(states_by_gauge, executed)
        optimal_return, optimal_details = compute_optimal_return(
            states=states,
            voting_power=args.voting_power,
            k=args.k,
            candidate_pools=args.candidate_pools,
            min_votes_per_pool=args.min_votes_per_pool,
        )

        ranked = sorted(
            states,
            key=lambda s: expected_return(s.total_usd, s.votes_raw, float(args.voting_power)),
            reverse=True,
        )
        candidates = ranked[: max(int(args.k), min(int(args.candidate_pools), len(ranked)))]

        w3: Optional[Web3] = None
        rpc = os.getenv("RPC_URL", "").strip()
        if rpc:
            try:
                w3_candidate = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
                if w3_candidate.is_connected():
                    w3 = w3_candidate
            except Exception:
                w3 = None

        summary = Table(title=f"Weekly Allocation Review (epoch={epoch}, strategy={args.strategy_tag})")
        summary.add_column("Metric", style="cyan")
        summary.add_column("Value", justify="right", style="yellow")
        # Show allocations before summary metrics
        console.print()
        render_allocation_table(
            title="Predicted Allocation Detail",
            conn=conn,
            w3=w3,
            states_by_gauge=states_by_gauge,
            allocation=predicted,
        )
        console.print()
        render_allocation_table(
            title="Executed Allocation Detail",
            conn=conn,
            w3=w3,
            states_by_gauge=states_by_gauge,
            allocation=executed,
        )
        console.print()

        # Default k-sweep analysis (with combo safety cap)
        k_sweep_table = Table(title="Pool-Count Sweep (Optimal Return by k)")
        k_sweep_table.add_column("k", justify="right", style="cyan")
        k_sweep_table.add_column("Combos", justify="right")
        k_sweep_table.add_column("Runtime", justify="right")
        k_sweep_table.add_column("Combos/Sec", justify="right")
        k_sweep_table.add_column("Optimal Return", justify="right", style="yellow")
        k_sweep_table.add_column("Delta vs Executed", justify="right", style="green")
        k_sweep_table.add_column("Status")

        best_sweep_return = -1.0
        best_sweep_k = None
        best_sweep_details: List[Tuple[GaugeBoundaryState, float, float]] = []
        max_k_for_sweep = min(int(args.k_sweep_max), len(candidates))

        total_combos_planned = 0
        for k_value in range(1, max_k_for_sweep + 1):
            combos = n_choose_k(len(candidates), int(k_value))
            if args.k_sweep_unbounded or combos <= int(args.k_sweep_max_combos):
                total_combos_planned += combos

        console.print(
            "[cyan]Sweep feasibility:[/cyan] "
            f"candidates={len(candidates)}, k_max={max_k_for_sweep}, "
            f"planned_combos={total_combos_planned:,}, "
            f"combo_cap={'none' if args.k_sweep_unbounded else f'{args.k_sweep_max_combos:,}'}"
        )

        for k_value in range(1, max_k_for_sweep + 1):
            combos = n_choose_k(len(candidates), int(k_value))
            if (not args.k_sweep_unbounded) and combos > int(args.k_sweep_max_combos):
                k_sweep_table.add_row(
                    str(k_value),
                    f"{combos:,}",
                    "-",
                    "-",
                    "-",
                    "-",
                    f"skipped (>{args.k_sweep_max_combos:,} combos)",
                )
                continue

            sweep_started = time.perf_counter()
            sweep_return, _ = compute_optimal_return_for_k(
                candidates=candidates,
                voting_power=args.voting_power,
                k=int(k_value),
                min_votes_per_pool=args.min_votes_per_pool,
            )
            elapsed = max(time.perf_counter() - sweep_started, 1e-9)
            combos_per_sec = combos / elapsed
            delta_vs_executed = sweep_return - executed_return
            k_sweep_table.add_row(
                str(k_value),
                f"{combos:,}",
                f"{elapsed:.2f}s",
                f"{combos_per_sec:,.0f}",
                f"${sweep_return:,.2f}",
                f"${delta_vs_executed:,.2f}",
                "ok",
            )

            if sweep_return > best_sweep_return:
                best_sweep_return = sweep_return
                best_sweep_k = int(k_value)
                best_sweep_details = _

        console.print(k_sweep_table)
        if best_sweep_k is not None:
            console.print(
                f"[cyan]Best k in sweep: k={best_sweep_k} (return=${best_sweep_return:,.2f}, "
                f"delta_vs_executed=${(best_sweep_return - executed_return):,.2f})[/cyan]"
            )
        console.print()

        selected_optimal_return = float(optimal_return)
        selected_optimal_details = optimal_details
        selected_mode_label = f"input k={args.k}"
        if args.summary_k_mode == "best-sweep" and best_sweep_k is not None:
            selected_optimal_return = float(best_sweep_return)
            if best_sweep_details:
                selected_optimal_details = best_sweep_details
            selected_mode_label = f"best sweep k={best_sweep_k}"

        metrics = {
            "predicted_return_usd": float(predicted_return),
            "executed_return_usd": float(executed_return),
            "optimal_return_usd": float(selected_optimal_return),
            "opportunity_loss_executed_vs_optimal_usd": float(selected_optimal_return - executed_return),
            "opportunity_loss_predicted_vs_optimal_usd": float(selected_optimal_return - predicted_return),
            "prediction_gap_predicted_vs_executed_usd": float(predicted_return - executed_return),
            "prediction_count": float(len(predicted)),
            "executed_count": float(len(executed)),
        }

        save_performance_metrics(
            conn=conn,
            epoch=epoch,
            strategy_tag=args.strategy_tag,
            metrics=metrics,
            notes=f"weekly_allocation_review|summary_k_mode={args.summary_k_mode}",
        )

        summary.add_row("Predicted return (USD)", f"${predicted_return:,.2f}")
        summary.add_row("Predicted return ($/1k votes)", f"${((predicted_return * 1000.0) / max(1.0, float(args.voting_power))):,.2f}")
        summary.add_row("Executed return (USD)", f"${executed_return:,.2f}")
        summary.add_row("Executed return ($/1k votes)", f"${((executed_return * 1000.0) / max(1.0, float(args.voting_power))):,.2f}")
        summary.add_row("Optimal mode", selected_mode_label)
        summary.add_row("Optimal return (USD)", f"${selected_optimal_return:,.2f}")
        summary.add_row("Optimal return ($/1k votes)", f"${((selected_optimal_return * 1000.0) / max(1.0, float(args.voting_power))):,.2f}")
        summary.add_row("Executed opportunity loss", f"${(selected_optimal_return - executed_return):,.2f}")
        summary.add_row("Predicted opportunity loss", f"${(selected_optimal_return - predicted_return):,.2f}")
        summary.add_row("Predicted vs Executed gap", f"${(predicted_return - executed_return):,.2f}")
        summary.add_row("Predicted pools", f"{len(predicted)}")
        summary.add_row("Executed pools", f"{len(executed)}")
        console.print(summary)

        if selected_optimal_details:
            detail_table = Table(title="Optimal Allocation Detail")
            detail_table.add_column("Rank", justify="right", style="cyan")
            detail_table.add_column("Pool", style="green")
            detail_table.add_column("Gauge", style="cyan")
            detail_table.add_column("Alloc Votes", justify="right", style="yellow")
            detail_table.add_column("Total Votes", justify="right")
            detail_table.add_column("Total Rewards", justify="right")
            detail_table.add_column("Expected To Us", justify="right", style="bold green")
            detail_table.add_column("Expected $/1k Votes", justify="right", style="bold green")
            optimal_total_expected = 0.0
            optimal_total_alloc_votes = 0.0
            for idx, (state, alloc_votes, expected_usd) in enumerate(selected_optimal_details, start=1):
                expected_per_1k_votes = (float(expected_usd) * 1000.0) / max(1.0, float(alloc_votes))
                pool_label = resolve_pool_label(conn, w3, state.pool)
                detail_table.add_row(
                    str(idx),
                    pool_label,
                    short_address(state.gauge),
                    f"{alloc_votes:,.0f}",
                    f"{state.votes_raw:,.0f}",
                    f"${state.total_usd:,.2f}",
                    f"${expected_usd:,.2f}",
                    f"${expected_per_1k_votes:,.2f}",
                )
                optimal_total_expected += float(expected_usd)
                optimal_total_alloc_votes += float(alloc_votes)
            optimal_total_per_1k = (optimal_total_expected * 1000.0) / max(1.0, optimal_total_alloc_votes)
            detail_table.add_row(
                "",
                "[bold]TOTAL[/bold]",
                "",
                f"[bold]{optimal_total_alloc_votes:,.0f}[/bold]",
                "",
                "",
                f"[bold]${optimal_total_expected:,.2f}[/bold]",
                f"[bold]${optimal_total_per_1k:,.2f}[/bold]",
            )
            console.print(detail_table)

        console.print("[green]✓ Saved performance metrics to allocation_performance_metrics[/green]")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
