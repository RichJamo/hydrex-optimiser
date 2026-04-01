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
from config.settings import (
    DATABASE_PATH,
    HYDREX_PRICE_REFRESH_MAX_FAILURES,
    VOTER_ADDRESS,
    WEEK,
)

load_dotenv()
console = Console()

VOTER_EPOCH_ABI = [
    {
        "inputs": [],
        "name": "_epochTimestamp",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "epochTimestamp",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


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
    candidate_pools: int,
    auto_top_k: bool,
    auto_top_k_min: int,
    auto_top_k_max: int,
    auto_top_k_step: int,
    min_votes_per_pool: int,
    max_gas_price_gwei: float,
    private_key_source: str,
    dry_run: bool,
    query_block: int,
    skip_fresh_fetch: bool,
    auto_top_k_return_tolerance_pct: float,
    phase_label: str,
    min_seconds_before_boundary: int,
    enforce_pre_boundary_guard: bool,
    votes_only_refresh: bool = False,
    price_max_age_hours: float = 0.0,
    allow_price_failures: int = 0,
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
        "--candidate-pools", str(candidate_pools),
        "--min-votes-per-pool", str(min_votes_per_pool),
        "--max-gas-price-gwei", str(max_gas_price_gwei),
        "--query-block", str(query_block),
        "--auto-top-k-return-tolerance-pct", str(auto_top_k_return_tolerance_pct),
        "--phase-label", str(phase_label),
        "--min-seconds-before-boundary", str(min_seconds_before_boundary),
    ]

    if enforce_pre_boundary_guard:
        cmd.append("--enforce-pre-boundary-guard")
    else:
        cmd.append("--no-enforce-pre-boundary-guard")

    if auto_top_k:
        cmd.extend([
            "--auto-top-k",
            "--auto-top-k-min", str(auto_top_k_min),
            "--auto-top-k-max", str(auto_top_k_max),
            "--auto-top-k-step", str(auto_top_k_step),
        ])
    
    if private_key_source:
        cmd.extend(["--private-key-source", private_key_source])
    
    if dry_run:
        cmd.append("--dry-run")

    if skip_fresh_fetch:
        cmd.append("--skip-fresh-fetch")

    if votes_only_refresh:
        cmd.append("--votes-only-refresh")

    if float(price_max_age_hours) > 0:
        cmd.extend(["--price-max-age-hours", str(float(price_max_age_hours))])

    cmd.extend(["--allow-price-failures", str(int(allow_price_failures))])

    display_cmd = list(cmd)
    if "--private-key-source" in display_cmd:
        key_idx = display_cmd.index("--private-key-source")
        if key_idx + 1 < len(display_cmd):
            display_cmd[key_idx + 1] = "***REDACTED***"
    
    try:
        console.print(f"[cyan]Triggering auto-voter ({phase_label}): {' '.join(display_cmd)}[/cyan]")
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
            combined_output = "\n".join(
                part for part in [result.stdout.strip(), result.stderr.strip()] if part
            )
            console.print(f"[red]✗ Auto-voter failed with exit code {result.returncode}[/red]")
            console.print(combined_output or "(no output captured)")
            return False, combined_output or f"exit_code={result.returncode}"
        
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
    latest_block_ts: int,
    onchain_epoch_ts: int,
    next_boundary_epoch: int,
    seconds_until_boundary: int,
    trigger_threshold_seconds: int,
    second_trigger_threshold_seconds: int,
    primary_triggered: bool,
    secondary_triggered: bool,
    last_check_time: datetime,
    check_interval: int,
    boundary_source: str,
) -> Table:
    """Create rich table showing monitor status."""
    table = Table(title="🤖 Boundary Monitor Status", show_header=False, show_lines=True)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="yellow")
    
    table.add_row("Current Block", f"{current_block:,}")
    table.add_row("Latest Block Time", f"{datetime.utcfromtimestamp(latest_block_ts).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    table.add_row("On-chain Epoch Start", f"{onchain_epoch_ts} ({datetime.utcfromtimestamp(onchain_epoch_ts).strftime('%Y-%m-%d %H:%M:%S UTC')})")
    table.add_row("Next Boundary (Epoch)", f"{next_boundary_epoch} ({datetime.utcfromtimestamp(next_boundary_epoch).strftime('%Y-%m-%d %H:%M:%S UTC')})")
    table.add_row("Seconds Until Boundary", f"{seconds_until_boundary:,}")
    table.add_row("Boundary Source", boundary_source)
    table.add_row("Trigger 1", f"{trigger_threshold_seconds}s before")
    table.add_row("Trigger 2", f"{second_trigger_threshold_seconds}s before")

    seconds_until_trigger = seconds_until_boundary - trigger_threshold_seconds
    seconds_until_second_trigger = seconds_until_boundary - second_trigger_threshold_seconds

    if primary_triggered and secondary_triggered:
        status = "[bold green]PHASE1+2 TRIGGERED ✓[/bold green]"
    elif secondary_triggered:
        status = "[bold green]PHASE2 TRIGGERED ✓[/bold green]"
    elif primary_triggered:
        status = f"[bold cyan]PHASE1 TRIGGERED ({seconds_until_second_trigger:,}s until phase2)[/bold cyan]"
    elif seconds_until_trigger <= 0:
        status = "[bold yellow]TRIGGER DUE[/bold yellow]"
    else:
        status = f"[bold cyan]MONITORING ({seconds_until_trigger:,}s until trigger)[/bold cyan]"
    
    table.add_row("Status", status)
    table.add_row("Guard Policy", "Abort if on-chain epoch advanced (mint/flip)")
    table.add_row("Last Check", last_check_time.strftime('%H:%M:%S'))
    table.add_row("Check Interval", f"{check_interval}s")
    
    return table


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor blockchain and trigger auto-voting at optimal time")
    auto_top_k_enabled_default = os.getenv("AUTO_TOP_K_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}
    parser.add_argument("--db-path", default=DATABASE_PATH, help="Database path")
    parser.add_argument("--rpc", default=os.getenv("RPC_URL", ""), help="RPC URL")
    parser.add_argument(
        "--trigger-seconds-before",
        type=int,
        default=int(os.getenv("AUTO_VOTE_TRIGGER_SECONDS_BEFORE", "90")),
        help="Trigger phase 1 when <= N seconds remain before boundary",
    )
    parser.add_argument(
        "--second-trigger-seconds-before",
        type=int,
        default=int(os.getenv("AUTO_VOTE_SECOND_TRIGGER_SECONDS_BEFORE", "20")),
        help="Trigger phase 2 when <= M seconds remain before boundary",
    )
    parser.add_argument(
        "--check-interval",
        type=int,
        default=int(os.getenv("AUTO_VOTE_CHECK_INTERVAL", "2")),
        help="Check interval in seconds",
    )
    parser.add_argument("--your-voting-power", type=int, default=int(os.getenv("YOUR_VOTING_POWER", "0")), help="Your total voting power")
    parser.add_argument("--top-k", type=int, default=int(os.getenv("MAX_GAUGES_TO_VOTE", "10")), help="Number of gauges to vote for")
    parser.add_argument(
        "--candidate-pools",
        type=int,
        default=int(os.getenv("AUTO_TOP_K_CANDIDATE_POOLS", "60")),
        help="Candidate pool count before chunked marginal allocation",
    )
    parser.add_argument(
        "--auto-top-k",
        action=argparse.BooleanOptionalAction,
        default=auto_top_k_enabled_default,
        help="Auto-select top-k by sweeping a configured range (default: enabled)",
    )
    parser.add_argument("--auto-top-k-min", type=int, default=1, help="Minimum k for auto top-k sweep")
    parser.add_argument(
        "--auto-top-k-max",
        type=int,
        default=int(os.getenv("AUTO_TOP_K_MAX", "50")),
        help="Maximum k for auto top-k sweep",
    )
    parser.add_argument("--auto-top-k-step", type=int, default=1, help="Step size for auto top-k sweep")
    parser.add_argument(
        "--min-votes-per-pool",
        type=int,
        default=int(os.getenv("MIN_VOTE_ALLOCATION", "1000")),
        help="Minimum votes per selected pool for allocator floor",
    )
    parser.add_argument("--max-gas-price-gwei", type=float, default=float(os.getenv("AUTO_VOTE_MAX_GAS_PRICE_GWEI", "10")), help="Max gas price in Gwei")
    parser.add_argument(
        "--second-max-gas-price-gwei",
        type=float,
        default=float(os.getenv("AUTO_VOTE_SECOND_MAX_GAS_PRICE_GWEI", "20")),
        help="Max gas price in Gwei for second-phase vote",
    )
    parser.add_argument(
        "--auto-top-k-return-tolerance-pct",
        type=float,
        default=float(os.getenv("AUTO_TOP_K_RETURN_TOLERANCE_PCT", "2.0")),
        help="Choose smallest k within this %% of best expected return",
    )
    parser.add_argument("--skip-fresh-fetch", action="store_true", help="Pass --skip-fresh-fetch to auto-voter")
    parser.add_argument(
        "--phase2-votes-only-refresh",
        action=argparse.BooleanOptionalAction,
        default=bool(os.getenv("BOUNDARY_MONITOR_PHASE2_VOTES_ONLY_REFRESH", "true").lower() not in ("0", "false", "no")),
        help="Phase 2: re-fetch only vote weights (skip bribe re-fetch and price refresh). Default: True",
    )
    parser.add_argument(
        "--phase2-price-max-age-hours",
        type=float,
        default=float(os.getenv("BOUNDARY_MONITOR_PHASE2_PRICE_MAX_AGE_HOURS", "1.0")),
        help="Price cache TTL for phase 2 (hours); if > 0, reuses phase 1's saved prices",
    )
    parser.add_argument(
        "--allow-price-failures",
        type=int,
        default=int(os.getenv("BOUNDARY_MONITOR_ALLOW_PRICE_FAILURES", str(HYDREX_PRICE_REFRESH_MAX_FAILURES))),
        help="Maximum token price refresh failures tolerated by auto_voter before abort",
    )
    parser.add_argument(
        "--private-key-source",
        default=os.getenv("TEST_WALLET_PK", ""),
        help="Private key source: raw key (default from TEST_WALLET_PK) or file path override",
    )
    parser.add_argument("--dry-run", action="store_true", help="Dry run mode (no actual transaction)")
    parser.add_argument(
        "--simulate-boundary-seconds-from-now",
        type=int,
        default=0,
        help="TEST ONLY: treat boundary as N seconds from now for trigger timing (still uses live on-chain data for voting)",
    )
    enforce_guard_default = os.getenv("AUTO_VOTE_ENFORCE_PRE_BOUNDARY_GUARD", "true").strip().lower() in {"1", "true", "yes", "on"}
    parser.add_argument(
        "--enforce-pre-boundary-guard",
        action=argparse.BooleanOptionalAction,
        default=enforce_guard_default,
        help="Pass hard pre-boundary guard to auto_voter (default: enabled)",
    )
    parser.add_argument("--once", action="store_true", help="Check once and exit (don't monitor continuously)")
    args = parser.parse_args()
    
    # Validate inputs
    if not args.rpc:
        console.print("[red]Error: RPC_URL required[/red]")
        sys.exit(1)
    
    if args.your_voting_power <= 0:
        console.print("[red]Error: YOUR_VOTING_POWER must be > 0[/red]")
        sys.exit(1)

    if args.second_trigger_seconds_before <= 0:
        console.print("[red]Error: --second-trigger-seconds-before must be > 0[/red]")
        sys.exit(1)

    if args.trigger_seconds_before <= 0:
        console.print("[red]Error: --trigger-seconds-before must be > 0[/red]")
        sys.exit(1)

    if args.second_trigger_seconds_before >= args.trigger_seconds_before:
        console.print("[red]Error: --second-trigger-seconds-before must be less than --trigger-seconds-before[/red]")
        sys.exit(1)

    if args.simulate_boundary_seconds_from_now < 0:
        console.print("[red]Error: --simulate-boundary-seconds-from-now must be >= 0[/red]")
        sys.exit(1)

    if args.allow_price_failures < 0:
        console.print("[red]Error: --allow-price-failures must be >= 0[/red]")
        sys.exit(1)
    
    # Connect to blockchain
    w3 = Web3(Web3.HTTPProvider(args.rpc))
    if not w3.is_connected():
        console.print("[red]Failed to connect to RPC[/red]")
        sys.exit(1)
    
    console.print(f"[green]✓ Connected to blockchain (Chain ID: {w3.eth.chain_id})[/green]")

    voter = w3.eth.contract(address=Web3.to_checksum_address(VOTER_ADDRESS), abi=VOTER_EPOCH_ABI)
    
    # Connect to database
    conn = sqlite3.connect(args.db_path)
    
    console.print("\n[bold cyan]═══════════════════════════════════════════════════════[/bold cyan]")
    console.print("[bold cyan]       HYDREX BOUNDARY MONITOR - PHASE 0.2              [/bold cyan]")
    console.print("[bold cyan]═══════════════════════════════════════════════════════[/bold cyan]\n")
    
    if args.dry_run:
        console.print("[bold yellow]⚠ DRY RUN MODE - No actual transactions will be sent[/bold yellow]\n")
    
    phase1_triggered = False
    phase2_triggered = False
    phase1_attempted = False
    phase2_attempted = False

    simulated_boundary_ts: Optional[int] = None
    
    try:
        while True:
            try:
                current_block = int(w3.eth.block_number)
                latest_block = w3.eth.get_block(current_block)
                latest_block_ts = int(latest_block["timestamp"])
                last_check_time = datetime.now()

                epoch_reader = getattr(voter.functions, "_epochTimestamp", None) or getattr(voter.functions, "epochTimestamp", None)
                if epoch_reader is None:
                    raise ValueError("Voter ABI missing _epochTimestamp/epochTimestamp")
                onchain_epoch_ts = int(epoch_reader().call(block_identifier=current_block))
                real_next_boundary_epoch = int(onchain_epoch_ts) + int(WEEK)

                boundary_source = "onchain"
                next_boundary_epoch = int(real_next_boundary_epoch)
                if int(args.simulate_boundary_seconds_from_now) > 0:
                    if simulated_boundary_ts is None:
                        simulated_boundary_ts = int(latest_block_ts) + int(args.simulate_boundary_seconds_from_now)
                    next_boundary_epoch = int(simulated_boundary_ts)
                    boundary_source = f"simulated(+{int(args.simulate_boundary_seconds_from_now)}s)"

                seconds_until_boundary = int(next_boundary_epoch) - int(latest_block_ts)
                seconds_until_trigger = int(seconds_until_boundary) - int(args.trigger_seconds_before)
                seconds_until_second_trigger = int(seconds_until_boundary) - int(args.second_trigger_seconds_before)

                if (not phase1_attempted) and (not phase2_attempted) and seconds_until_second_trigger <= 0:
                    phase1_triggered = True
                    phase1_attempted = True
                    console.print("[yellow]Phase 1 window already passed; skipping straight to phase 2[/yellow]")
                
                # Create status table
                table = create_status_table(
                    current_block=current_block,
                    latest_block_ts=latest_block_ts,
                    onchain_epoch_ts=onchain_epoch_ts,
                    next_boundary_epoch=next_boundary_epoch,
                    seconds_until_boundary=seconds_until_boundary,
                    trigger_threshold_seconds=args.trigger_seconds_before,
                    second_trigger_threshold_seconds=args.second_trigger_seconds_before,
                    primary_triggered=phase1_triggered,
                    secondary_triggered=phase2_triggered,
                    last_check_time=last_check_time,
                    check_interval=args.check_interval,
                    boundary_source=boundary_source,
                )
                
                console.clear()
                console.print(table)

                # Never execute a vote at/after boundary.
                if seconds_until_boundary <= 0:
                    if phase1_attempted or phase2_attempted:
                        console.print("\n[green]Boundary has passed. Exiting monitor.[/green]")
                    else:
                        console.print("\n[bold yellow]Boundary reached before any phase trigger could execute. Exiting without voting.[/bold yellow]")
                    break
                
                # Check if we should trigger
                if (not phase1_attempted) and seconds_until_trigger <= 0:
                    console.print("\n[bold yellow]🚨 PHASE 1 TRIGGER REACHED - Initiating anchor vote...[/bold yellow]\n")
                    
                    # Use current block for snapshot
                    query_block = current_block
                    
                    success, output = trigger_auto_voter(
                        db_path=args.db_path,
                        your_voting_power=args.your_voting_power,
                        top_k=args.top_k,
                        candidate_pools=args.candidate_pools,
                        auto_top_k=bool(args.auto_top_k),
                        auto_top_k_min=int(args.auto_top_k_min),
                        auto_top_k_max=int(args.auto_top_k_max),
                        auto_top_k_step=int(args.auto_top_k_step),
                        min_votes_per_pool=args.min_votes_per_pool,
                        max_gas_price_gwei=args.max_gas_price_gwei,
                        private_key_source=args.private_key_source,
                        dry_run=args.dry_run,
                        query_block=query_block,
                        skip_fresh_fetch=bool(args.skip_fresh_fetch),
                        auto_top_k_return_tolerance_pct=float(args.auto_top_k_return_tolerance_pct),
                        phase_label="phase1",
                        min_seconds_before_boundary=int(args.second_trigger_seconds_before),
                        enforce_pre_boundary_guard=bool(args.enforce_pre_boundary_guard),
                        allow_price_failures=int(args.allow_price_failures),
                    )
                    phase1_attempted = True
                    
                    if success:
                        phase1_triggered = True
                        console.print("\n[bold green]✓ PHASE 1 AUTO-VOTE COMPLETED[/bold green]")
                        console.print("[green]Monitor continues for phase 2 trigger[/green]")
                    else:
                        console.print("\n[bold red]✗ PHASE 1 AUTO-VOTE FAILED[/bold red]")
                        console.print("[yellow]Will retry on next check[/yellow]")

                if (not phase2_attempted) and seconds_until_second_trigger <= 0:
                    console.print("\n[bold yellow]🚨 PHASE 2 TRIGGER REACHED - Initiating final vote...[/bold yellow]\n")

                    query_block = current_block

                    success, output = trigger_auto_voter(
                        db_path=args.db_path,
                        your_voting_power=args.your_voting_power,
                        top_k=args.top_k,
                        candidate_pools=args.candidate_pools,
                        auto_top_k=bool(args.auto_top_k),
                        auto_top_k_min=int(args.auto_top_k_min),
                        auto_top_k_max=int(args.auto_top_k_max),
                        auto_top_k_step=int(args.auto_top_k_step),
                        min_votes_per_pool=args.min_votes_per_pool,
                        max_gas_price_gwei=args.second_max_gas_price_gwei,
                        private_key_source=args.private_key_source,
                        dry_run=args.dry_run,
                        query_block=query_block,
                        skip_fresh_fetch=bool(args.skip_fresh_fetch),
                        auto_top_k_return_tolerance_pct=float(args.auto_top_k_return_tolerance_pct),
                        phase_label="phase2",
                        min_seconds_before_boundary=0,
                        enforce_pre_boundary_guard=bool(args.enforce_pre_boundary_guard),
                        price_max_age_hours=float(args.phase2_price_max_age_hours),
                        allow_price_failures=int(args.allow_price_failures),
                        votes_only_refresh=bool(args.phase2_votes_only_refresh),
                    )
                    phase2_attempted = True

                    if success:
                        phase2_triggered = True
                        console.print("\n[bold green]✓ PHASE 2 AUTO-VOTE COMPLETED[/bold green]")
                        console.print("[green]Monitor will continue running for visibility[/green]")
                    else:
                        console.print("\n[bold red]✗ PHASE 2 AUTO-VOTE FAILED[/bold red]")
                        console.print("[yellow]Will retry on next check[/yellow]")
                
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
