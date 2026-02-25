#!/usr/bin/env python3
"""
Boundary Monitor: Continuously monitors blockchain to detect when to trigger auto-voting.

This script:
1. Monitors current block number
2. Calculates blocks until next epoch boundary
3. Triggers auto-voter at configured threshold (default: 20 blocks before boundary)
4. Handles errors and retries
5. Provides logging and alerting

Usage:
  python scripts/boundary_monitor.py --trigger-blocks-before 20 --dry-run
"""

import argparse
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from typing import Optional, Tuple

from dotenv import load_dotenv
from rich.console import Console
from rich.live import Live
from rich.table import Table
from web3 import Web3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import DATABASE_PATH, WEEK

load_dotenv()
console = Console()


def get_next_boundary(conn: sqlite3.Connection, current_epoch: int) -> Tuple[int, int]:
    """
    Get next boundary epoch and block.
    Returns (next_epoch, boundary_block).
    """
    cur = conn.cursor()
    
    # Try to find next epoch in database
    row = cur.execute(
        """
        SELECT epoch, boundary_block
        FROM epoch_boundaries
        WHERE epoch > ?
        ORDER BY epoch ASC
        LIMIT 1
        """,
        (current_epoch,),
    ).fetchone()
    
    if row:
        return int(row[0]), int(row[1])
    
    # If not found, estimate next boundary
    # Current epoch + 1 week
    next_epoch = current_epoch + WEEK
    
    # Try to estimate block based on average block time
    latest_row = cur.execute(
        """
        SELECT epoch, boundary_block
        FROM epoch_boundaries
        ORDER BY epoch DESC
        LIMIT 2
        """
    ).fetchall()
    
    if len(latest_row) >= 2:
        # Estimate blocks per epoch
        epoch_diff = latest_row[0][0] - latest_row[1][0]
        block_diff = latest_row[0][1] - latest_row[1][1]
        blocks_per_second = block_diff / epoch_diff if epoch_diff > 0 else 0.5  # Default to 2s per block
        
        # Estimate next boundary block
        time_until_boundary = next_epoch - current_epoch
        estimated_blocks = int(time_until_boundary * blocks_per_second)
        estimated_boundary_block = latest_row[0][1] + estimated_blocks
        
        return next_epoch, estimated_boundary_block
    
    # Fallback: assume 2 second block time
    latest_boundary = cur.execute("SELECT MAX(boundary_block) FROM epoch_boundaries").fetchone()
    if latest_boundary and latest_boundary[0]:
        time_until_boundary = next_epoch - current_epoch
        estimated_blocks = int(time_until_boundary / 2)  # 2s per block
        return next_epoch, int(latest_boundary[0]) + estimated_blocks
    
    raise ValueError("Cannot determine next boundary")


def get_current_epoch(conn: sqlite3.Connection, current_ts: int) -> int:
    """Get current epoch based on timestamp."""
    cur = conn.cursor()
    row = cur.execute(
        """
        SELECT MAX(epoch)
        FROM epoch_boundaries
        WHERE epoch <= ?
        """,
        (current_ts,),
    ).fetchone()
    
    if row and row[0]:
        return int(row[0])
    
    # Fallback to latest epoch
    fallback = cur.execute("SELECT MAX(epoch) FROM epoch_boundaries").fetchone()
    if fallback and fallback[0]:
        return int(fallback[0])
    
    raise ValueError("No epochs found in database")


def trigger_auto_voter(
    db_path: str,
    your_voting_power: int,
    top_k: int,
    max_gas_price_gwei: float,
    private_key_source: str,
    dry_run: bool,
    query_block: int,
) -> Tuple[bool, str]:
    """
    Trigger the auto-voter script.
    Returns (success, output_or_error).
    """
    cmd = [
        sys.executable,
        os.path.join(os.path.dirname(__file__), "auto_voter.py"),
        "--db-path", db_path,
        "--your-voting-power", str(your_voting_power),
        "--top-k", str(top_k),
        "--max-gas-price-gwei", str(max_gas_price_gwei),
        "--query-block", str(query_block),
    ]
    
    if private_key_source:
        cmd.extend(["--private-key-source", private_key_source])
    
    if dry_run:
        cmd.append("--dry-run")
    
    try:
        console.print(f"[cyan]Triggering auto-voter: {' '.join(cmd)}[/cyan]")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,  # 10 minute timeout
        )
        
        if result.returncode == 0:
            console.print("[green]✓ Auto-voter completed successfully[/green]")
            console.print(result.stdout)
            return True, result.stdout
        else:
            console.print(f"[red]✗ Auto-voter failed with exit code {result.returncode}[/red]")
            console.print(result.stderr)
            return False, result.stderr
        
    except subprocess.TimeoutExpired:
        err = "Auto-voter timed out after 10 minutes"
        console.print(f"[red]✗ {err}[/red]")
        return False, err
    except Exception as e:
        err = f"Failed to trigger auto-voter: {e}"
        console.print(f"[red]✗ {err}[/red]")
        return False, err


def create_status_table(
    current_block: int,
    next_boundary_epoch: int,
    boundary_block: int,
    blocks_until: int,
    trigger_threshold: int,
    triggered: bool,
    last_check_time: datetime,
    check_interval: int,
) -> Table:
    """Create rich table showing monitor status."""
    table = Table(title="🤖 Boundary Monitor Status", show_header=False, show_lines=True)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="yellow")
    
    table.add_row("Current Block", f"{current_block:,}")
    table.add_row("Next Boundary (Epoch)", f"{next_boundary_epoch} ({datetime.fromtimestamp(next_boundary_epoch).strftime('%Y-%m-%d %H:%M:%S UTC')})")
    table.add_row("Boundary Block", f"{boundary_block:,}")
    table.add_row("Blocks Until Boundary", f"{blocks_until:,}")
    table.add_row("Trigger Threshold", f"{trigger_threshold} blocks before")
    
    trigger_block = boundary_block - trigger_threshold
    blocks_until_trigger = trigger_block - current_block
    
    if triggered:
        status = "[bold green]TRIGGERED ✓[/bold green]"
    elif blocks_until_trigger <= 0:
        status = "[bold yellow]TRIGGER DUE[/bold yellow]"
    else:
        status = f"[bold cyan]MONITORING ({blocks_until_trigger:,} blocks until trigger)[/bold cyan]"
    
    table.add_row("Status", status)
    table.add_row("Trigger Block", f"{trigger_block:,}")
    table.add_row("Last Check", last_check_time.strftime('%H:%M:%S'))
    table.add_row("Check Interval", f"{check_interval}s")
    
    return table


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor blockchain and trigger auto-voting at optimal time")
    parser.add_argument("--db-path", default=DATABASE_PATH, help="Database path")
    parser.add_argument("--rpc", default=os.getenv("RPC_URL", ""), help="RPC URL")
    parser.add_argument("--trigger-blocks-before", type=int, default=int(os.getenv("AUTO_VOTE_TRIGGER_BLOCKS_BEFORE", "20")), help="Trigger N blocks before boundary")
    parser.add_argument("--check-interval", type=int, default=30, help="Check interval in seconds")
    parser.add_argument("--your-voting-power", type=int, default=int(os.getenv("YOUR_VOTING_POWER", "0")), help="Your total voting power")
    parser.add_argument("--top-k", type=int, default=int(os.getenv("MAX_GAUGES_TO_VOTE", "10")), help="Number of gauges to vote for")
    parser.add_argument("--max-gas-price-gwei", type=float, default=float(os.getenv("AUTO_VOTE_MAX_GAS_PRICE_GWEI", "10")), help="Max gas price in Gwei")
    parser.add_argument(
        "--private-key-source",
        default=os.getenv("AUTO_VOTE_WALLET_KEYFILE", ""),
        help="Private key source: raw key, file path, or 1Password reference (op://Vault/Item/field)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Dry run mode (no actual transaction)")
    parser.add_argument("--once", action="store_true", help="Check once and exit (don't monitor continuously)")
    args = parser.parse_args()
    
    # Validate inputs
    if not args.rpc:
        console.print("[red]Error: RPC_URL required[/red]")
        sys.exit(1)
    
    if args.your_voting_power <= 0:
        console.print("[red]Error: YOUR_VOTING_POWER must be > 0[/red]")
        sys.exit(1)
    
    # Connect to blockchain
    w3 = Web3(Web3.HTTPProvider(args.rpc))
    if not w3.is_connected():
        console.print("[red]Failed to connect to RPC[/red]")
        sys.exit(1)
    
    console.print(f"[green]✓ Connected to blockchain (Chain ID: {w3.eth.chain_id})[/green]")
    
    # Connect to database
    conn = sqlite3.connect(args.db_path)
    
    console.print("\n[bold cyan]═══════════════════════════════════════════════════════[/bold cyan]")
    console.print("[bold cyan]       HYDREX BOUNDARY MONITOR - PHASE 0.2              [/bold cyan]")
    console.print("[bold cyan]═══════════════════════════════════════════════════════[/bold cyan]\n")
    
    if args.dry_run:
        console.print("[bold yellow]⚠ DRY RUN MODE - No actual transactions will be sent[/bold yellow]\n")
    
    triggered = False
    
    try:
        while True:
            try:
                current_block = int(w3.eth.block_number)
                current_ts = int(time.time())
                last_check_time = datetime.now()
                
                # Get current epoch and next boundary
                current_epoch = get_current_epoch(conn, current_ts)
                next_boundary_epoch, boundary_block = get_next_boundary(conn, current_epoch)
                
                blocks_until = boundary_block - current_block
                trigger_block = boundary_block - args.trigger_blocks_before
                blocks_until_trigger = trigger_block - current_block
                
                # Create status table
                table = create_status_table(
                    current_block=current_block,
                    next_boundary_epoch=next_boundary_epoch,
                    boundary_block=boundary_block,
                    blocks_until=blocks_until,
                    trigger_threshold=args.trigger_blocks_before,
                    triggered=triggered,
                    last_check_time=last_check_time,
                    check_interval=args.check_interval,
                )
                
                console.clear()
                console.print(table)
                
                # Check if we should trigger
                if not triggered and blocks_until_trigger <= 0:
                    console.print("\n[bold yellow]🚨 TRIGGER THRESHOLD REACHED - Initiating auto-vote...[/bold yellow]\n")
                    
                    # Use current block for snapshot
                    query_block = current_block
                    
                    success, output = trigger_auto_voter(
                        db_path=args.db_path,
                        your_voting_power=args.your_voting_power,
                        top_k=args.top_k,
                        max_gas_price_gwei=args.max_gas_price_gwei,
                        private_key_source=args.private_key_source,
                        dry_run=args.dry_run,
                        query_block=query_block,
                    )
                    
                    if success:
                        triggered = True
                        console.print("\n[bold green]✓ AUTO-VOTE TRIGGERED SUCCESSFULLY[/bold green]")
                        console.print("[green]Monitor will continue running for visibility[/green]")
                    else:
                        console.print("\n[bold red]✗ AUTO-VOTE FAILED[/bold red]")
                        console.print("[yellow]Will retry on next check[/yellow]")
                
                # If triggered and we're past the boundary, we can exit
                if triggered and blocks_until < 0:
                    console.print("\n[green]Boundary has passed. Exiting monitor.[/green]")
                    break
                
                # Exit if --once flag
                if args.once:
                    console.print("\n[cyan]--once flag set, exiting after single check[/cyan]")
                    break
                
                # Wait for next check
                time.sleep(args.check_interval)
                
            except KeyboardInterrupt:
                console.print("\n[yellow]Monitor stopped by user[/yellow]")
                break
            except Exception as e:
                console.print(f"\n[red]Error during monitoring: {e}[/red]")
                console.print("[yellow]Retrying in 30 seconds...[/yellow]")
                time.sleep(30)
    
    finally:
        conn.close()
        console.print("\n[cyan]Boundary monitor shutdown complete.[/cyan]")


if __name__ == "__main__":
    main()
