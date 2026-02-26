#!/usr/bin/env python3
"""
Generate clear voting instructions from the latest live snapshot.
Outputs in multiple formats:
1. Human-readable table
2. Contract call format (pool addresses + proportions)
3. CSV for record-keeping

Note: Vote proportions are relative weights (e.g., [10000, 10000, ...] for equal allocation).
The contract normalizes them - they don't need to sum to voting power.
VOTE_DELAY is currently 0, so you can re-vote multiple times per epoch (not twice in same block).
"""

import argparse
import os
import sqlite3
import sys
import time
from typing import List, Tuple

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import DATABASE_PATH
from src.allocation_tracking import save_predicted_allocation

load_dotenv()
console = Console()


def load_latest_snapshot(conn: sqlite3.Connection) -> Tuple[int, int, int]:
    """Return (snapshot_ts, vote_epoch, query_block) for latest snapshot."""
    cur = conn.cursor()
    row = cur.execute(
        """
        SELECT snapshot_ts, vote_epoch, query_block
        FROM live_gauge_snapshots
        WHERE snapshot_ts = (SELECT MAX(snapshot_ts) FROM live_gauge_snapshots)
        LIMIT 1
        """
    ).fetchone()
    
    if not row:
        raise ValueError("No live snapshot found in database")
    
    return int(row[0]), int(row[1]), int(row[2])


def calculate_allocation(
    conn: sqlite3.Connection,
    snapshot_ts: int,
    your_voting_power: int,
    top_k: int,
    min_reward_usd: float = 0.0,
) -> List[Tuple[str, str, float, float, float, int]]:
    """
    Calculate optimal allocation using marginal ROI.
    
    Returns list of (gauge_addr, pool_addr, base_votes, rewards_norm, adj_roi, allocated_votes)
    """
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT gauge_address, pool_address, votes_raw, rewards_normalized_total
        FROM live_gauge_snapshots
        WHERE snapshot_ts = ? AND is_alive = 1 AND rewards_normalized_total > ?
        ORDER BY rewards_normalized_total DESC
        """,
        (snapshot_ts, min_reward_usd),
    ).fetchall()
    
    if not rows:
        console.print("[red]No live gauges with positive rewards found.[/red]")
        return []
    
    # Equal allocation for simplicity
    votes_per_pool = int(your_voting_power / max(1, top_k))
    
    # Calculate adjusted ROI (marginal)
    scored = []
    for gauge_addr, pool_addr, votes_raw, rewards_norm in rows:
        base_votes = float(votes_raw or 0.0)
        rewards_total = float(rewards_norm or 0.0)
        # Marginal ROI = rewards / (current_votes + new_votes)
        adjusted_roi = rewards_total / max(1.0, (base_votes + votes_per_pool))
        scored.append((gauge_addr, pool_addr, base_votes, rewards_total, adjusted_roi, votes_per_pool))
    
    # Sort by adjusted ROI descending
    scored.sort(key=lambda x: x[4], reverse=True)
    
    return scored[:top_k]


def print_voting_instructions(
    allocation: List[Tuple[str, str, float, float, float, int]],
    snapshot_ts: int,
    vote_epoch: int,
    query_block: int,
    your_voting_power: int,
    output_csv: str = "",
) -> None:
    """Print human-readable voting instructions."""
    
    if not allocation:
        console.print("[red]No allocation to display[/red]")
        return
    
    # Summary header
    console.print(f"\n[bold cyan]╔═══════════════════════════════════════════════════════╗[/bold cyan]")
    console.print(f"[bold cyan]║       HYDREX VOTING INSTRUCTIONS - PHASE 0.1          ║[/bold cyan]")
    console.print(f"[bold cyan]╚═══════════════════════════════════════════════════════╝[/bold cyan]\n")
    
    console.print(f"[yellow]Snapshot Timestamp:[/yellow] {snapshot_ts} ({time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(snapshot_ts))})")
    console.print(f"[yellow]Vote Epoch:[/yellow] {vote_epoch} ({time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(vote_epoch))})")
    console.print(f"[yellow]Query Block:[/yellow] {query_block}")
    console.print(f"[yellow]Your Voting Power:[/yellow] {your_voting_power:,}\n")
    
    # Allocation table
    table = Table(title="Recommended Allocation", show_header=True, header_style="bold magenta")
    table.add_column("#", justify="right", style="cyan")
    table.add_column("Gauge Address", style="green")
    table.add_column("Pool Address", style="blue")
    table.add_column("Current Votes", justify="right")
    table.add_column("Rewards (norm)", justify="right")
    table.add_column("Marginal ROI", justify="right")
    table.add_column("Your Votes", justify="right", style="bold yellow")
    table.add_column("% of Power", justify="right")
    
    total_allocated = 0
    total_expected_return = 0.0
    
    for idx, (gauge_addr, pool_addr, base_votes, rewards_norm, adj_roi, allocated_votes) in enumerate(allocation, start=1):
        total_allocated += allocated_votes
        expected_return = allocated_votes * adj_roi
        total_expected_return += expected_return
        pct = (allocated_votes / your_voting_power) * 100.0
        
        table.add_row(
            str(idx),
            gauge_addr,
            pool_addr,
            f"{base_votes:,.0f}",
            f"{rewards_norm:,.2f}",
            f"{adj_roi:.6f}",
            f"{allocated_votes:,}",
            f"{pct:.1f}%",
        )
    
    console.print(table)
    
    console.print(f"\n[bold]Total Allocated:[/bold] {total_allocated:,} / {your_voting_power:,} ({(total_allocated/your_voting_power)*100:.1f}%)")
    console.print(f"[bold green]Expected Total Return (normalized):[/bold green] {total_expected_return:,.2f}\n")
    
    # Contract call format
    console.print("[bold cyan]═══ CONTRACT CALL FORMAT ═══[/bold cyan]\n")
    console.print("[yellow]Function:[/yellow] vote(address[] _poolVote, uint256[] _voteProportions)")
    console.print(f"[yellow]Contract:[/yellow] {os.getenv('VOTER_ADDRESS', 'VOTER_ADDRESS_NOT_SET')}\n")
    
    pool_addresses = [alloc[1] for alloc in allocation]
    # Use constant proportions (relative weights) - contract normalizes them
    PROPORTION_PER_POOL = 10000  # Equal weight for equal allocation
    vote_proportions = [str(PROPORTION_PER_POOL) for _ in allocation]
    
    console.print("[bold]_poolVote (array of pool addresses):[/bold]")
    console.print("[" + ",\n ".join([f'"{addr}"' for addr in pool_addresses]) + "]")
    
    console.print("\n[bold]_voteProportions (array of relative weights - contract normalizes):[/bold]")
    console.print("[" + ", ".join(vote_proportions) + "]")
    console.print("[dim](Equal allocation: each pool gets weight 10000)[/dim]")
    
    # Python/web3.py format
    console.print("\n[bold cyan]═══ PYTHON/WEB3.PY FORMAT ═══[/bold cyan]\n")
    console.print("```python")
    console.print(f"pool_addresses = {pool_addresses}")
    console.print(f"vote_proportions = [{PROPORTION_PER_POOL}] * {len(allocation)}  # Equal weights")
    console.print("# OR: vote_proportions = [10000, 10000, ...]  # One per pool")
    console.print("tx = voter_contract.functions.vote(pool_addresses, vote_proportions).build_transaction({...})")
    console.print("```")
    
    # CSV format for copy-paste
    console.print("\n[bold cyan]═══ CSV FORMAT (for manual entry) ═══[/bold cyan]\n")
    console.print("Rank,GaugeAddress,PoolAddress,VoteAmount,PercentOfPower")
    for idx, (gauge_addr, pool_addr, _base_votes, _rewards_norm, _adj_roi, allocated_votes) in enumerate(allocation, start=1):
        pct = (allocated_votes / your_voting_power) * 100.0
        console.print(f"{idx},{gauge_addr},{pool_addr},{allocated_votes},{pct:.1f}%")
    
    # Save to CSV file if requested
    if output_csv:
        with open(output_csv, "w") as f:
            f.write("rank,gauge_address,pool_address,base_votes,rewards_normalized,marginal_roi,allocated_votes,percent_of_power\n")
            for idx, (gauge_addr, pool_addr, base_votes, rewards_norm, adj_roi, allocated_votes) in enumerate(allocation, start=1):
                pct = (allocated_votes / your_voting_power) * 100.0
                f.write(f"{idx},{gauge_addr},{pool_addr},{base_votes:.2f},{rewards_norm:.4f},{adj_roi:.8f},{allocated_votes},{pct:.2f}\n")
        console.print(f"\n[green]✓ CSV saved to: {output_csv}[/green]")
    
    # Warning
    console.print("\n[bold red]⚠ IMPORTANT REMINDERS:[/bold red]")
    console.print("[red]1. Verify pool addresses match gauges in your wallet/interface[/red]")
    console.print("[red]2. Ensure you have enough gas for the transaction[/red]")
    console.print("[red]3. Double-check vote amounts sum to your voting power[/red]")
    console.print("[red]4. This is an EQUAL ALLOCATION strategy (may not be optimal)[/red]")
    console.print("[red]5. Review transaction in block explorer before confirming[/red]\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate voting instructions from latest snapshot")
    parser.add_argument("--db-path", default=DATABASE_PATH, help="Database path")
    parser.add_argument("--your-voting-power", type=int, default=int(os.getenv("YOUR_VOTING_POWER", "0")), help="Your total voting power")
    parser.add_argument("--top-k", type=int, default=int(os.getenv("MAX_GAUGES_TO_VOTE", "10")), help="Number of gauges to vote for")
    parser.add_argument("--min-reward-usd", type=float, default=0.0, help="Minimum reward threshold (normalized)")
    parser.add_argument("--output-csv", default="", help="Optional CSV output file path")
    parser.add_argument("--snapshot-ts", type=int, default=0, help="Specific snapshot timestamp (default: latest)")
    parser.add_argument(
        "--save-prediction",
        action="store_true",
        help="Persist generated allocation to predicted_allocations table",
    )
    parser.add_argument(
        "--strategy-tag",
        default="preboundary_equal",
        help="Strategy tag used when saving prediction (default: preboundary_equal)",
    )
    args = parser.parse_args()
    
    if args.your_voting_power <= 0:
        console.print("[red]Error: YOUR_VOTING_POWER must be > 0 (set in .env or pass --your-voting-power)[/red]")
        sys.exit(1)
    
    conn = sqlite3.connect(args.db_path)
    
    try:
        # Load snapshot info
        if args.snapshot_ts > 0:
            snapshot_ts = args.snapshot_ts
            row = conn.cursor().execute(
                "SELECT vote_epoch, query_block FROM live_gauge_snapshots WHERE snapshot_ts = ? LIMIT 1",
                (snapshot_ts,)
            ).fetchone()
            if not row:
                console.print(f"[red]Snapshot {snapshot_ts} not found[/red]")
                sys.exit(1)
            vote_epoch, query_block = int(row[0]), int(row[1])
        else:
            snapshot_ts, vote_epoch, query_block = load_latest_snapshot(conn)
        
        # Calculate allocation
        allocation = calculate_allocation(
            conn=conn,
            snapshot_ts=snapshot_ts,
            your_voting_power=args.your_voting_power,
            top_k=args.top_k,
            min_reward_usd=args.min_reward_usd,
        )
        
        if not allocation:
            console.print("[red]No allocation generated[/red]")
            sys.exit(1)
        
        # Print instructions
        print_voting_instructions(
            allocation=allocation,
            snapshot_ts=snapshot_ts,
            vote_epoch=vote_epoch,
            query_block=query_block,
            your_voting_power=args.your_voting_power,
            output_csv=args.output_csv,
        )

        if args.save_prediction:
            rows = [
                (idx, gauge_addr, pool_addr, allocated_votes)
                for idx, (gauge_addr, pool_addr, _base_votes, _rewards_norm, _adj_roi, allocated_votes)
                in enumerate(allocation, start=1)
            ]
            inserted = save_predicted_allocation(
                conn=conn,
                vote_epoch=vote_epoch,
                snapshot_ts=snapshot_ts,
                query_block=query_block,
                strategy_tag=args.strategy_tag,
                rows=rows,
            )
            console.print(
                f"[green]✓ Saved {inserted} predicted allocation rows (strategy={args.strategy_tag})[/green]"
            )
        
    finally:
        conn.close()


if __name__ == "__main__":
    main()
