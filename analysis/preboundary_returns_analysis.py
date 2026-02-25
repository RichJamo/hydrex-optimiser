#!/usr/bin/env python3
"""
Analyze pre-boundary optimization potential by comparing:
1. Data visible at 20 blocks before boundary
2. Data visible at 1 block before boundary  
3. Actual boundary data

For each snapshot, we:
- Select top 5 pools by reward/vote ratio
- Calculate expected returns for YOUR_VOTING_POWER
- Compare to actual optimal returns at boundary
"""

import os
import sqlite3
from typing import Dict, List, Tuple
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

load_dotenv()
console = Console()

DATABASE_PATH = os.getenv("DATABASE_PATH", "data/db/data.db")
YOUR_VOTING_POWER = int(os.getenv("YOUR_VOTING_POWER", 1183272))


def get_gauge_data_at_boundary(conn: sqlite3.Connection, epoch: int) -> Dict[str, Tuple[float, float]]:
    """
    Get gauge votes and rewards at actual boundary.
    Returns: {gauge_address: (votes_raw, rewards_normalized_sum)}
    """
    # Get votes from boundary_gauge_values
    cur = conn.cursor()
    votes_data = {}
    cur.execute("""
        SELECT gauge_address, CAST(votes_raw AS REAL) 
        FROM boundary_gauge_values 
        WHERE epoch = ?
    """, (epoch,))
    for row in cur.fetchall():
        votes_data[row[0]] = row[1]
    
    # Get rewards from boundary_reward_snapshots with decimal normalization
    rewards_data = {}
    cur.execute("""
        SELECT 
            brs.gauge_address, 
            SUM(CAST(brs.rewards_raw AS REAL) / POWER(10, COALESCE(tm.decimals, 18))) as normalized_rewards
        FROM boundary_reward_snapshots brs
        LEFT JOIN token_metadata tm ON brs.reward_token = tm.token_address
        WHERE brs.epoch = ?
        GROUP BY brs.gauge_address
    """, (epoch,))
    for row in cur.fetchall():
        rewards_data[row[0]] = row[1] if row[1] else 0.0
    
    # Combine
    result = {}
    all_gauges = set(votes_data.keys()) | set(rewards_data.keys())
    for gauge in all_gauges:
        votes = votes_data.get(gauge, 0.0)
        rewards = rewards_data.get(gauge, 0.0)
        if votes > 0:  # Only include gauges with votes
            result[gauge] = (votes, rewards)
    
    return result


def get_gauge_data_at_offset(conn: sqlite3.Connection, epoch: int, blocks_before: int) -> Dict[str, Tuple[float, float]]:
    """
    Get gauge votes and rewards at N blocks before boundary.
    Returns: {gauge_address: (votes_raw, rewards_normalized_sum)}
    """
    # Get votes from boundary_vote_samples
    cur = conn.cursor()
    votes_data = {}
    cur.execute("""
        SELECT gauge_address, CAST(votes_raw AS REAL) 
        FROM boundary_vote_samples 
        WHERE epoch = ? AND blocks_before_boundary = ?
    """, (epoch, blocks_before))
    for row in cur.fetchall():
        votes_data[row[0]] = row[1]
    
    # Get rewards from boundary_reward_samples with decimal normalization
    rewards_data = {}
    cur.execute("""
        SELECT 
            brs.gauge_address, 
            SUM(CAST(brs.rewards_raw AS REAL) / POWER(10, COALESCE(tm.decimals, 18))) as normalized_rewards
        FROM boundary_reward_samples brs
        LEFT JOIN token_metadata tm ON brs.reward_token = tm.token_address
        WHERE brs.epoch = ? AND brs.blocks_before_boundary = ?
        GROUP BY brs.gauge_address
    """, (epoch, blocks_before))
    for row in cur.fetchall():
        rewards_data[row[0]] = row[1] if row[1] else 0.0
    
    # Combine
    result = {}
    all_gauges = set(votes_data.keys()) | set(rewards_data.keys())
    for gauge in all_gauges:
        votes = votes_data.get(gauge, 0.0)
        rewards = rewards_data.get(gauge, 0.0)
        if votes > 0:  # Only include gauges with votes
            result[gauge] = (votes, rewards)
    
    return result


def calculate_roi_and_select_top5(gauge_data: Dict[str, Tuple[float, float]]) -> List[Tuple[str, float, float, float]]:
    """
    Calculate ROI (rewards/votes) for each gauge and return top 5.
    Returns: [(gauge, votes, rewards, roi), ...]
    """
    MIN_VOTES_THRESHOLD = 1.0  # Require at least 1 vote to avoid division by near-zero
    
    gauge_roi = []
    for gauge, (votes, rewards) in gauge_data.items():
        if votes >= MIN_VOTES_THRESHOLD and rewards > 0:
            roi = rewards / votes
            gauge_roi.append((gauge, votes, rewards, roi))
    
    # Sort by ROI descending
    gauge_roi.sort(key=lambda x: x[3], reverse=True)
    return gauge_roi[:5]


def calculate_expected_returns(
    top5: List[Tuple[str, float, float, float]], 
    your_votes: float,
    gauge_data: Dict[str, Tuple[float, float]]
) -> Tuple[float, List[Tuple[str, float, float]]]:
    """
    Given top 5 pools selected at a snapshot time, calculate expected returns
    if we split YOUR_VOTING_POWER equally across them, using the actual final
    boundary data for accurate returns calculation.
    
    Returns: (total_returns, [(gauge, allocated_votes, expected_rewards), ...])
    """
    votes_per_pool = your_votes / 5.0
    total_returns = 0.0
    details = []
    
    for gauge, snapshot_votes, snapshot_rewards, snapshot_roi in top5:
        # Get actual boundary data for this gauge
        if gauge in gauge_data:
            actual_votes, actual_rewards = gauge_data[gauge]
            # Calculate our share of rewards based on our contribution to total votes
            new_total_votes = actual_votes + votes_per_pool
            our_share = votes_per_pool / new_total_votes
            our_rewards = actual_rewards * our_share
            total_returns += our_rewards
            details.append((gauge, votes_per_pool, our_rewards))
        else:
            # Gauge not in boundary data (shouldn't happen)
            details.append((gauge, votes_per_pool, 0.0))
    
    return total_returns, details


def analyze_epoch(conn: sqlite3.Connection, epoch: int) -> None:
    """Analyze one epoch's pre-boundary optimization potential."""
    console.print(f"\n[bold cyan]═══ Analyzing Epoch {epoch} ═══[/bold cyan]")
    
    # Get data at all three time points
    boundary_data = get_gauge_data_at_boundary(conn, epoch)
    pre1_data = get_gauge_data_at_offset(conn, epoch, 1)
    pre20_data = get_gauge_data_at_offset(conn, epoch, 20)
    
    console.print(f"Boundary: {len(boundary_data)} gauges with votes")
    console.print(f"1 block before: {len(pre1_data)} gauges with votes")
    console.print(f"20 blocks before: {len(pre20_data)} gauges with votes")
    
    if not boundary_data:
        console.print("[yellow]No boundary data, skipping epoch[/yellow]")
        return
    
    # Calculate optimal selection at each time point
    boundary_top5 = calculate_roi_and_select_top5(boundary_data)
    pre1_top5 = calculate_roi_and_select_top5(pre1_data)
    pre20_top5 = calculate_roi_and_select_top5(pre20_data)
    
    # Calculate expected returns if we chose based on each snapshot
    optimal_returns, optimal_details = calculate_expected_returns(boundary_top5, YOUR_VOTING_POWER, boundary_data)
    pre1_returns, pre1_details = calculate_expected_returns(pre1_top5, YOUR_VOTING_POWER, boundary_data)
    pre20_returns, pre20_details = calculate_expected_returns(pre20_top5, YOUR_VOTING_POWER, boundary_data)
    
    # Display results
    table = Table(title=f"Epoch {epoch} - Optimization Comparison (Normalized Token Units)")
    table.add_column("Metric", style="cyan")
    table.add_column("At Boundary (Optimal)", style="green", justify="right")
    table.add_column("1 Block Before", style="yellow", justify="right")
    table.add_column("20 Blocks Before", style="magenta", justify="right")
    
    table.add_row("Total Expected Returns", f"{optimal_returns:,.4f}", f"{pre1_returns:,.4f}", f"{pre20_returns:,.4f}")
    table.add_row(
        "vs Optimal", 
        "100.00%",
        f"{(pre1_returns/optimal_returns*100):.2f}%" if optimal_returns > 0 else "N/A",
        f"{(pre20_returns/optimal_returns*100):.2f}%" if optimal_returns > 0 else "N/A"
    )
    
    console.print(table)
    
    # Show pool selections
    console.print("\n[bold]Pool Selections:[/bold]")
    
    for i, (label, top5) in enumerate([
        ("Boundary (Optimal)", boundary_top5),
        ("1 Block Before", pre1_top5),
        ("20 Blocks Before", pre20_top5)
    ]):
        console.print(f"\n[bold]{label}:[/bold]")
        for rank, (gauge, votes, rewards, roi) in enumerate(top5, 1):
            console.print(f"  {rank}. {gauge[:10]}... ROI: {roi:.8f} ({rewards:,.2f} / {votes:,.0f})")


def main():
    conn = sqlite3.connect(DATABASE_PATH)
    
    # Find epochs with data in all three snapshots
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT b.epoch
        FROM boundary_gauge_values b
        JOIN boundary_vote_samples v1 ON b.epoch = v1.epoch AND v1.blocks_before_boundary = 1
        JOIN boundary_vote_samples v20 ON b.epoch = v20.epoch AND v20.blocks_before_boundary = 20
        ORDER BY b.epoch DESC
        LIMIT 5
    """)
    epochs = [row[0] for row in cur.fetchall()]
    
    console.print(f"[green]Found {len(epochs)} epochs with complete data[/green]")
    
    for epoch in epochs:
        analyze_epoch(conn, epoch)
    
    conn.close()


if __name__ == "__main__":
    main()
