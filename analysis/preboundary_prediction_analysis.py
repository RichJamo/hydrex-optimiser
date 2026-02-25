#!/usr/bin/env python3
"""
Pre-Boundary Prediction Analysis

Analyzes how well we can predict optimal strategies using data from 1 and 20 blocks
before the epoch boundary, compared to actual optimal results.

Key Questions:
1. Which gauges are top-5 at pre-boundary vs actual boundary?
2. What are the ROI differences between prediction time and actual?
3. If we locked in our vote allocation early, how much return would we lose/gain?
4. Are there systematic patterns in how rewards/votes change?
"""

import argparse
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from rich.console import Console
from rich.table import Table

from config.settings import DATABASE_PATH

console = Console()


@dataclass
class GaugeSnapshot:
    """Snapshot of gauge state at a specific time."""
    gauge_address: str
    vote_epoch: int
    block: int
    votes_raw: float
    rewards_raw: float
    roi: float


@dataclass
class OptimizationResult:
    """Result of portfolio optimization at a specific time."""
    top_5_gauges: List[str]
    expected_roi: float
    total_expected_return: float
    gauge_allocations: Dict[str, float]  # gauge -> vote weight


@dataclass
class ComparisonResult:
    """Comparison between prediction and actual."""
    epoch: int
    blocks_before: int
    prediction: OptimizationResult
    actual: OptimizationResult
    overlap_count: int
    roi_error_pct: float
    return_if_locked_in: float
    return_loss_pct: float


def load_gauge_snapshots(
    conn: sqlite3.Connection,
    epoch: int,
    blocks_before: int,
    min_votes: float = 1000.0
) -> List[GaugeSnapshot]:
    """Load gauge data from boundary samples or actual boundary tables."""
    cur = conn.cursor()
    
    # Get vote_epoch and boundary_block
    boundary_info = cur.execute(
        "SELECT vote_epoch, boundary_block FROM epoch_boundaries WHERE epoch = ?",
        (epoch,)
    ).fetchone()
    
    if not boundary_info:
        return []
    
    vote_epoch, boundary_block = boundary_info
    
    # Load votes
    votes_by_gauge = {}
    if blocks_before == 0:
        # Use actual boundary data from boundary_gauge_values
        for row in cur.execute(
            """
            SELECT gauge_address, CAST(votes_raw AS REAL) as votes
            FROM boundary_gauge_values
            WHERE epoch = ?
            """,
            (epoch,)
        ).fetchall():
            gauge, votes = row
            if votes >= min_votes:
                votes_by_gauge[gauge] = votes
    else:
        # Use pre-boundary samples
        for row in cur.execute(
            """
            SELECT gauge_address, CAST(votes_raw AS REAL) as votes
            FROM boundary_vote_samples
            WHERE epoch = ? AND blocks_before_boundary = ?
            """,
            (epoch, blocks_before)
        ).fetchall():
            gauge, votes = row
            if votes >= min_votes:
                votes_by_gauge[gauge] = votes
    
    # Load rewards (sum across all tokens)
    rewards_by_gauge = defaultdict(float)
    if blocks_before == 0:
        # Use actual boundary data from bribes table
        for row in cur.execute(
            """
            SELECT gauge_address, CAST(amount_wei AS REAL) as rewards
            FROM bribes
            WHERE epoch = ?
            """,
            (epoch,)
        ).fetchall():
            gauge, rewards = row
            if gauge:  # Skip None gauge addresses
                rewards_by_gauge[gauge] += rewards
    else:
        # Use pre-boundary samples
        for row in cur.execute(
            """
            SELECT gauge_address, CAST(rewards_raw AS REAL) as rewards
            FROM boundary_reward_samples
            WHERE epoch = ? AND blocks_before_boundary = ? AND active_only = 1
            """,
            (epoch, blocks_before)
        ).fetchall():
            gauge, rewards = row
            rewards_by_gauge[gauge] += rewards
    
    # Combine into snapshots
    snapshots = []
    for gauge in votes_by_gauge:
        votes = votes_by_gauge[gauge]
        rewards = rewards_by_gauge.get(gauge, 0.0)
        
        # Calculate ROI (rewards per vote)
        roi = rewards / votes if votes > 0 else 0.0
        
        snapshots.append(GaugeSnapshot(
            gauge_address=gauge,
            vote_epoch=vote_epoch,
            block=boundary_block - blocks_before,
            votes_raw=votes,
            rewards_raw=rewards,
            roi=roi
        ))
    
    return snapshots


def optimize_portfolio(
    snapshots: List[GaugeSnapshot],
    voting_power: float = 1_183_272,
    top_n: int = 5
) -> OptimizationResult:
    """Optimize portfolio by selecting top N gauges by ROI and allocating votes equally."""
    # Sort by ROI descending
    sorted_snapshots = sorted(snapshots, key=lambda s: s.roi, reverse=True)
    
    # Select top N
    top_gauges = sorted_snapshots[:top_n]
    
    if not top_gauges:
        return OptimizationResult(
            top_5_gauges=[],
            expected_roi=0.0,
            total_expected_return=0.0,
            gauge_allocations={}
        )
    
    # Equal vote allocation
    votes_per_gauge = voting_power / len(top_gauges)
    
    # Calculate expected return
    total_return = sum(gauge.roi * votes_per_gauge for gauge in top_gauges)
    avg_roi = total_return / voting_power if voting_power > 0 else 0.0
    
    allocations = {gauge.gauge_address: votes_per_gauge for gauge in top_gauges}
    top_5_addrs = [gauge.gauge_address for gauge in top_gauges]
    
    return OptimizationResult(
        top_5_gauges=top_5_addrs,
        expected_roi=avg_roi,
        total_expected_return=total_return,
        gauge_allocations=allocations
    )


def calculate_locked_in_return(
    prediction: OptimizationResult,
    actual_snapshots: List[GaugeSnapshot]
) -> float:
    """Calculate what return we'd actually get if we locked in the prediction allocation."""
    actual_roi_by_gauge = {s.gauge_address: s.roi for s in actual_snapshots}
    
    total_return = 0.0
    for gauge, vote_allocation in prediction.gauge_allocations.items():
        actual_roi = actual_roi_by_gauge.get(gauge, 0.0)
        total_return += actual_roi * vote_allocation
    
    return total_return


def compare_predictions(
    conn: sqlite3.Connection,
    epoch: int,
    voting_power: float = 1_183_272
) -> Optional[ComparisonResult]:
    """Compare predictions at different time points for a single epoch."""
    
    # Load snapshots at different times
    pre1_snapshots = load_gauge_snapshots(conn, epoch, blocks_before=1)
    pre20_snapshots = load_gauge_snapshots(conn, epoch, blocks_before=20)
    actual_snapshots = load_gauge_snapshots(conn, epoch, blocks_before=0)
    
    if not actual_snapshots:
        return None
    
    # Optimize at each time point
    pre1_opt = optimize_portfolio(pre1_snapshots, voting_power)
    pre20_opt = optimize_portfolio(pre20_snapshots, voting_power)
    actual_opt = optimize_portfolio(actual_snapshots, voting_power)
    
    # Calculate overlaps
    pre1_overlap = len(set(pre1_opt.top_5_gauges) & set(actual_opt.top_5_gauges))
    pre20_overlap = len(set(pre20_opt.top_5_gauges) & set(actual_opt.top_5_gauges))
    
    # Calculate ROI prediction errors
    pre1_roi_error = abs(pre1_opt.expected_roi - actual_opt.expected_roi) / actual_opt.expected_roi * 100 if actual_opt.expected_roi > 0 else 0
    pre20_roi_error = abs(pre20_opt.expected_roi - actual_opt.expected_roi) / actual_opt.expected_roi * 100 if actual_opt.expected_roi > 0 else 0
    
    # Calculate actual returns if locked in early
    pre1_locked_return = calculate_locked_in_return(pre1_opt, actual_snapshots)
    pre20_locked_return = calculate_locked_in_return(pre20_opt, actual_snapshots)
    
    # Return loss vs optimal
    pre1_loss_pct = (actual_opt.total_expected_return - pre1_locked_return) / actual_opt.total_expected_return * 100 if actual_opt.total_expected_return > 0 else 0
    pre20_loss_pct = (actual_opt.total_expected_return - pre20_locked_return) / actual_opt.total_expected_return * 100 if actual_opt.total_expected_return > 0 else 0
    
    return {
        'pre1': ComparisonResult(
            epoch=epoch,
            blocks_before=1,
            prediction=pre1_opt,
            actual=actual_opt,
            overlap_count=pre1_overlap,
            roi_error_pct=pre1_roi_error,
            return_if_locked_in=pre1_locked_return,
            return_loss_pct=pre1_loss_pct
        ),
        'pre20': ComparisonResult(
            epoch=epoch,
            blocks_before=20,
            prediction=pre20_opt,
            actual=actual_opt,
            overlap_count=pre20_overlap,
            roi_error_pct=pre20_roi_error,
            return_if_locked_in=pre20_locked_return,
            return_loss_pct=pre20_loss_pct
        ),
        'actual': actual_opt
    }


def analyze_all_epochs(
    conn: sqlite3.Connection,
    voting_power: float = 1_183_272,
    max_epochs: Optional[int] = None
) -> None:
    """Analyze all available epochs."""
    
    cur = conn.cursor()
    
    # Get epochs with pre-boundary data
    epochs = [
        row[0] for row in cur.execute(
            """
            SELECT DISTINCT epoch 
            FROM boundary_vote_samples 
            WHERE blocks_before_boundary IN (1, 20)
            ORDER BY epoch DESC
            """
        ).fetchall()
    ]
    
    if max_epochs:
        epochs = epochs[:max_epochs]
    
    console.print(f"\n[bold]Analyzing {len(epochs)} epochs with pre-boundary data[/bold]\n")
    
    # Summary statistics
    pre1_overlaps = []
    pre20_overlaps = []
    pre1_losses = []
    pre20_losses = []
    
    for epoch in epochs:
        comparison = compare_predictions(conn, epoch, voting_power)
        if not comparison:
            continue
        
        pre1 = comparison['pre1']
        pre20 = comparison['pre20']
        actual = comparison['actual']
        
        pre1_overlaps.append(pre1.overlap_count)
        pre20_overlaps.append(pre20.overlap_count)
        pre1_losses.append(pre1.return_loss_pct)
        pre20_losses.append(pre20.return_loss_pct)
        
        # Print individual epoch results
        console.print(f"[bold cyan]Epoch {epoch}[/bold cyan]")
        console.print(f"  Actual optimal return: {actual.total_expected_return / 1e18:.2f} tokens")
        console.print(f"  Actual optimal ROI: {actual.expected_roi / 1e18:.6f} tokens/vote")
        
        console.print(f"  [yellow]Pre-1 block:[/yellow]")
        console.print(f"    Top-5 overlap: {pre1.overlap_count}/5")
        console.print(f"    Locked-in return: {pre1.return_if_locked_in / 1e18:.2f} tokens ({pre1.return_loss_pct:.2f}% loss)")
        
        console.print(f"  [yellow]Pre-20 blocks:[/yellow]")
        console.print(f"    Top-5 overlap: {pre20.overlap_count}/5")
        console.print(f"    Locked-in return: {pre20.return_if_locked_in / 1e18:.2f} tokens ({pre20.return_loss_pct:.2f}% loss)")
        console.print()
    
    # Summary table
    if pre1_overlaps:
        table = Table(title="Pre-Boundary Prediction Summary")
        table.add_column("Metric", style="cyan")
        table.add_column("Pre-1 Block", style="yellow")
        table.add_column("Pre-20 Blocks", style="yellow")
        
        table.add_row(
            "Avg. Top-5 Overlap",
            f"{sum(pre1_overlaps) / len(pre1_overlaps):.2f} / 5",
            f"{sum(pre20_overlaps) / len(pre20_overlaps):.2f} / 5"
        )
        
        table.add_row(
            "Avg. Return Loss %",
            f"{sum(pre1_losses) / len(pre1_losses):.2f}%",
            f"{sum(pre20_losses) / len(pre20_losses):.2f}%"
        )
        
        table.add_row(
            "Max Return Loss %",
            f"{max(pre1_losses):.2f}%",
            f"{max(pre20_losses):.2f}%"
        )
        
        table.add_row(
            "Min Return Loss %",
            f"{min(pre1_losses):.2f}%",
            f"{min(pre20_losses):.2f}%"
        )
        
        console.print(table)


def main():
    parser = argparse.ArgumentParser(description="Analyze pre-boundary prediction accuracy")
    parser.add_argument("--db-path", default=DATABASE_PATH, help="Path to SQLite database")
    parser.add_argument("--voting-power", type=float, default=1_183_272, help="Your voting power")
    parser.add_argument("--max-epochs", type=int, help="Limit to N most recent epochs")
    parser.add_argument("--epoch", type=int, help="Analyze a specific epoch only")
    
    args = parser.parse_args()
    
    conn = sqlite3.connect(args.db_path)
    
    try:
        if args.epoch:
            comparison = compare_predictions(conn, args.epoch, args.voting_power)
            if comparison:
                pre1 = comparison['pre1']
                pre20 = comparison['pre20']
                actual = comparison['actual']
                
                console.print(f"\n[bold]Epoch {args.epoch} Analysis[/bold]\n")
                console.print(f"[green]Actual Optimal (at boundary):[/green]")
                console.print(f"  Top 5: {actual.top_5_gauges}")
                console.print(f"  Expected return: {actual.total_expected_return / 1e18:.2f} tokens")
                console.print(f"  Average ROI: {actual.expected_roi / 1e18:.6f} tokens/vote\n")
                
                console.print(f"[yellow]Pre-1 Block Prediction:[/yellow]")
                console.print(f"  Top 5: {pre1.prediction.top_5_gauges}")
                console.print(f"  Overlap: {pre1.overlap_count}/5")
                console.print(f"  Predicted return: {pre1.prediction.total_expected_return / 1e18:.2f} tokens")
                console.print(f"  Actual return if locked in: {pre1.return_if_locked_in / 1e18:.2f} tokens")
                console.print(f"  Loss: {pre1.return_loss_pct:.2f}%\n")
                
                console.print(f"[yellow]Pre-20 Blocks Prediction:[/yellow]")
                console.print(f"  Top 5: {pre20.prediction.top_5_gauges}")
                console.print(f"  Overlap: {pre20.overlap_count}/5")
                console.print(f"  Predicted return: {pre20.prediction.total_expected_return / 1e18:.2f} tokens")
                console.print(f"  Actual return if locked in: {pre20.return_if_locked_in / 1e18:.2f} tokens")
                console.print(f"  Loss: {pre20.return_loss_pct:.2f}%\n")
        else:
            analyze_all_epochs(conn, args.voting_power, args.max_epochs)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
