#!/usr/bin/env python3
"""
Auto-Voter: Executes optimized votes automatically at the optimal time (N blocks before boundary).

This script:
1. Fetches fresh live snapshot data
2. Calculates optimal allocation
3. Builds and signs vote transaction
4. Executes vote (or dry-run for testing)

Safety Features:
- Dry-run mode (no actual transaction)
- Transaction simulation before sending
- Gas price limits
- Vote amount validation
- Comprehensive logging

Note: Vote proportions are relative weights (e.g., [10000, 10000, ...] for equal allocation).
The contract normalizes them - they don't need to sum to voting power.
VOTE_DELAY is currently 0, so you can re-vote multiple times per epoch (not twice in same block).
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
from eth_account import Account
from rich.console import Console
from web3 import Web3
from web3.exceptions import ContractLogicError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import DATABASE_PATH, VOTER_ADDRESS, ONE_E18, WEEK

load_dotenv()
console = Console()

# Load Voter ABI
VOTERV5_ABI_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "voterv5_abi.json")
with open(VOTERV5_ABI_PATH, "r") as f:
    VOTER_ABI = json.load(f)


def load_wallet(private_key_source: str) -> Account:
    """Load wallet from private key (env var or file path)."""
    if os.path.isfile(private_key_source):
        with open(private_key_source, "r") as f:
            private_key = f.read().strip()
    else:
        private_key = private_key_source
    
    # Remove 0x prefix if present
    if private_key.startswith("0x"):
        private_key = private_key[2:]
    
    return Account.from_key(private_key)


def fetch_fresh_snapshot(
    conn: sqlite3.Connection,
    w3: Web3,
    query_block: int,
    discover_missing_pairs: bool = False,
) -> Tuple[int, int, int]:
    """
    Fetch fresh live snapshot by calling the fetch_live_snapshot module.
    Returns (snapshot_ts, vote_epoch, query_block).
    """
    from data.fetchers.fetch_live_snapshot import (
        ensure_live_tables,
        fetch_live_snapshot,
        resolve_vote_epoch,
    )
    
    now_ts = int(time.time())
    vote_epoch = resolve_vote_epoch(conn, now_ts=now_ts, forced_vote_epoch=0)
    
    if query_block <= 0:
        query_block = int(w3.eth.block_number)
    
    console.print(f"[cyan]Fetching fresh snapshot at block {query_block}, vote_epoch={vote_epoch}...[/cyan]")
    
    snapshot_ts, token_rows, gauge_rows = fetch_live_snapshot(
        conn=conn,
        w3=w3,
        query_block=query_block,
        vote_epoch=vote_epoch,
        max_gauges=0,  # All gauges
        progress_every=100,
        progress_every_batches=3,
        discover_missing_pairs=discover_missing_pairs,
        pairs_cache_path=os.path.join(os.path.dirname(__file__), "..", "data", "fetchers", "discovered_pairs.json"),
    )
    
    console.print(f"[green]✓ Fresh snapshot saved: snapshot_ts={snapshot_ts}, gauge_rows={gauge_rows}[/green]")
    return snapshot_ts, vote_epoch, query_block


def calculate_optimal_allocation(
    conn: sqlite3.Connection,
    snapshot_ts: int,
    your_voting_power: int,
    top_k: int,
) -> List[Tuple[str, str, int]]:
    """
    Calculate optimal allocation using marginal ROI.
    Returns list of (gauge_addr, pool_addr, vote_amount).
    """
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT gauge_address, pool_address, votes_raw, rewards_normalized_total
        FROM live_gauge_snapshots
        WHERE snapshot_ts = ? AND is_alive = 1 AND rewards_normalized_total > 0
        ORDER BY rewards_normalized_total DESC
        """,
        (snapshot_ts,),
    ).fetchall()
    
    if not rows:
        console.print("[red]No live gauges with positive rewards found[/red]")
        return []
    
    # Equal allocation
    votes_per_pool = int(your_voting_power / max(1, top_k))
    
    # Calculate marginal ROI
    scored = []
    for gauge_addr, pool_addr, votes_raw, rewards_norm in rows:
        base_votes = float(votes_raw or 0.0)
        rewards_total = float(rewards_norm or 0.0)
        adjusted_roi = rewards_total / max(1.0, (base_votes + votes_per_pool))
        scored.append((gauge_addr, pool_addr, base_votes, rewards_total, adjusted_roi))
    
    # Sort by adjusted ROI
    scored.sort(key=lambda x: x[4], reverse=True)
    
    # Return top K with vote amounts
    return [(gauge, pool, votes_per_pool) for gauge, pool, _bv, _rn, _roi in scored[:top_k]]


def validate_allocation(allocation: List[Tuple[str, str, int]], your_voting_power: int) -> bool:
    """Validate allocation meets requirements."""
    total_votes = sum(votes for _, _, votes in allocation)
    
    if total_votes > your_voting_power:
        console.print(f"[red]✗ Total votes ({total_votes}) exceeds voting power ({your_voting_power})[/red]")
        return False
    
    if total_votes < your_voting_power * 0.95:  # Allow 5% tolerance
        console.print(f"[yellow]⚠ Total votes ({total_votes}) is less than 95% of voting power ({your_voting_power})[/yellow]")
    
    console.print(f"[green]✓ Allocation validated: {total_votes:,} / {your_voting_power:,} votes ({(total_votes/your_voting_power)*100:.1f}%)[/green]")
    return True


def simulate_vote_transaction(
    voter_contract,
    pool_addresses: List[str],
    vote_proportions: List[int],
    from_address: str,
    block_identifier: str = "latest",
) -> bool:
    """Simulate vote transaction using eth_call."""
    try:
        # This will revert if the vote would fail
        voter_contract.functions.vote(pool_addresses, vote_proportions).call(
            {"from": from_address},
            block_identifier=block_identifier,
        )
        console.print("[green]✓ Transaction simulation successful[/green]")
        return True
    except ContractLogicError as e:
        console.print(f"[red]✗ Transaction simulation failed: {e}[/red]")
        return False
    except Exception as e:
        console.print(f"[red]✗ Simulation error: {e}[/red]")
        return False


def build_and_send_vote_transaction(
    w3: Web3,
    voter_contract,
    wallet: Optional[Account],
    pool_addresses: List[str],
    vote_proportions: List[int],
    max_gas_price_gwei: float,
    dry_run: bool = True,
) -> Tuple[bool, str]:
    """
    Build, sign, and send vote transaction.
    Returns (success, tx_hash_or_error).
    """
    # Use zero address for dry-run if no wallet provided
    from_address = wallet.address if wallet else "0x0000000000000000000000000000000000000000"
    
    # Check current gas price
    current_gas_price = w3.eth.gas_price
    current_gas_price_gwei = float(current_gas_price) / 1e9
    
    console.print(f"[cyan]Current gas price: {current_gas_price_gwei:.2f} Gwei[/cyan]")
    
    if current_gas_price_gwei > max_gas_price_gwei:
        err = f"Gas price {current_gas_price_gwei:.2f} Gwei exceeds limit {max_gas_price_gwei} Gwei"
        console.print(f"[red]✗ {err}[/red]")
        return False, err
    
    console.print(f"[green]✓ Gas price acceptable (<= {max_gas_price_gwei} Gwei)[/green]")
    
    # Simulate first (skip if dry-run and no wallet)
    if not dry_run or wallet:
        console.print("[cyan]Simulating transaction...[/cyan]")
        if not simulate_vote_transaction(voter_contract, pool_addresses, vote_proportions, from_address):
            return False, "Simulation failed"
    else:
        console.print("[yellow]Skipping simulation (dry-run without wallet)[/yellow]")
    
    # Build transaction
    try:
        nonce = w3.eth.get_transaction_count(from_address) if wallet else 0
        
        tx = {
            "from": from_address,
            "nonce": nonce,
            "gas": 500000,  # Conservative estimate
            "gasPrice": current_gas_price,
            "chainId": w3.eth.chain_id if not dry_run else 8453,
        }
        
        # Only build full transaction if not dry-run or if we have a wallet
        if not dry_run or wallet:
            tx = voter_contract.functions.vote(pool_addresses, vote_proportions).build_transaction(tx)
            
            # Estimate gas
            try:
                estimated_gas = w3.eth.estimate_gas(tx)
                tx["gas"] = int(estimated_gas * 1.2)  # 20% buffer
                console.print(f"[cyan]Estimated gas: {estimated_gas:,} (using {tx['gas']:,} with buffer)[/cyan]")
            except Exception as e:
                console.print(f"[yellow]⚠ Gas estimation failed, using default: {e}[/yellow]")
        
        tx_cost_eth = (tx["gas"] * current_gas_price) / 1e18
        console.print(f"[cyan]Estimated transaction cost: {tx_cost_eth:.6f} ETH[/cyan]")
        
        if dry_run:
            console.print("\n[bold yellow]═══ DRY RUN MODE - NO TRANSACTION SENT ═══[/bold yellow]")
            console.print(f"[yellow]Would send transaction:[/yellow]")
            console.print(f"  From: {from_address}")
            console.print(f"  To: {voter_contract.address}")
            console.print(f"  Nonce: {nonce if wallet else 'N/A'}")
            console.print(f"  Gas: {tx.get('gas', 'N/A'):,}" if 'gas' in tx else "  Gas: (estimate)")
            console.print(f"  Gas Price: {current_gas_price_gwei:.2f} Gwei")
            console.print(f"  Estimated Cost: {tx_cost_eth:.6f} ETH" if 'gas' in tx else "  Estimated Cost: (unknown)")
            console.print(f"  Pools: {len(pool_addresses)}")
            console.print(f"  Vote Proportions (weights): {vote_proportions}")
            return True, "DRY_RUN_SUCCESS"
        
        if not wallet:
            return False, "No wallet provided for actual transaction"
        
        # Sign transaction
        console.print("[cyan]Signing transaction...[/cyan]")
        signed_tx = wallet.sign_transaction(tx)
        
        # Send transaction
        console.print("[cyan]Sending transaction...[/cyan]")
        tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
        tx_hash_hex = tx_hash.hex()
        
        console.print(f"[green]✓ Transaction sent: {tx_hash_hex}[/green]")
        console.print("[cyan]Waiting for transaction receipt...[/cyan]")
        
        # Wait for receipt (timeout after 5 minutes)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
        
        if receipt["status"] == 1:
            console.print(f"[bold green]✓ TRANSACTION SUCCESSFUL[/bold green]")
            console.print(f"  Block: {receipt['blockNumber']}")
            console.print(f"  Gas Used: {receipt['gasUsed']:,}")
            console.print(f"  Tx Hash: {tx_hash_hex}")
            return True, tx_hash_hex
        else:
            console.print(f"[bold red]✗ TRANSACTION FAILED[/bold red]")
            console.print(f"  Tx Hash: {tx_hash_hex}")
            return False, f"Transaction reverted: {tx_hash_hex}"
        
    except Exception as e:
        console.print(f"[red]✗ Transaction error: {e}[/red]")
        return False, str(e)


def main() -> None:
    parser = argparse.ArgumentParser(description="Automated voting executor with safety checks")
    parser.add_argument("--db-path", default=DATABASE_PATH, help="Database path")
    parser.add_argument("--rpc", default=os.getenv("RPC_URL", ""), help="RPC URL")
    parser.add_argument("--your-voting-power", type=int, default=int(os.getenv("YOUR_VOTING_POWER", "0")), help="Your total voting power")
    parser.add_argument("--top-k", type=int, default=int(os.getenv("MAX_GAUGES_TO_VOTE", "10")), help="Number of gauges to vote for")
    parser.add_argument("--query-block", type=int, default=0, help="Block to query (default: latest)")
    parser.add_argument("--discover-missing-pairs", action="store_true", help="On-chain enumerate missing reward tokens")
    parser.add_argument("--private-key-source", default=os.getenv("AUTO_VOTE_WALLET_KEYFILE", ""), help="Private key (env var value or file path)")
    parser.add_argument("--max-gas-price-gwei", type=float, default=float(os.getenv("AUTO_VOTE_MAX_GAS_PRICE_GWEI", "10")), help="Max gas price in Gwei")
    parser.add_argument("--dry-run", action="store_true", help="Dry run mode (no actual transaction)")
    parser.add_argument("--skip-fresh-fetch", action="store_true", help="Skip fetching fresh snapshot (use latest in DB)")
    args = parser.parse_args()
    
    # Validate inputs
    if not args.rpc:
        console.print("[red]Error: RPC_URL required[/red]")
        sys.exit(1)
    
    if args.your_voting_power <= 0:
        console.print("[red]Error: YOUR_VOTING_POWER must be > 0[/red]")
        sys.exit(1)
    
    if not args.private_key_source and not args.dry_run:
        console.print("[red]Error: --private-key-source required (or use --dry-run)[/red]")
        sys.exit(1)
    
    # Connect to blockchain
    w3 = Web3(Web3.HTTPProvider(args.rpc))
    if not w3.is_connected():
        console.print("[red]Failed to connect to RPC[/red]")
        sys.exit(1)
    
    console.print(f"[green]✓ Connected to {args.rpc}[/green]")
    console.print(f"[cyan]Chain ID: {w3.eth.chain_id}, Latest Block: {w3.eth.block_number}[/cyan]")
    
    # Load wallet
    wallet = None
    if args.private_key_source:
        try:
            wallet = load_wallet(args.private_key_source)
            console.print(f"[green]✓ Wallet loaded: {wallet.address}[/green]")
            
            # Check balance
            balance = w3.eth.get_balance(wallet.address)
            balance_eth = float(balance) / 1e18
            console.print(f"[cyan]Wallet balance: {balance_eth:.6f} ETH[/cyan]")
            
            if balance_eth < 0.001:
                console.print("[yellow]⚠ Low wallet balance, may not have enough gas[/yellow]")
        except Exception as e:
            console.print(f"[red]✗ Failed to load wallet: {e}[/red]")
            sys.exit(1)
    
    # Connect to database
    conn = sqlite3.connect(args.db_path)
    
    try:
        # Fetch fresh snapshot (unless skipped)
        if args.skip_fresh_fetch:
            console.print("[yellow]Skipping fresh snapshot fetch, using latest in DB...[/yellow]")
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
                console.print("[red]No snapshot found in DB[/red]")
                sys.exit(1)
            snapshot_ts, vote_epoch, query_block = int(row[0]), int(row[1]), int(row[2])
        else:
            snapshot_ts, vote_epoch, query_block = fetch_fresh_snapshot(
                conn=conn,
                w3=w3,
                query_block=args.query_block,
                discover_missing_pairs=args.discover_missing_pairs,
            )
        
        console.print(f"[cyan]Using snapshot: ts={snapshot_ts}, vote_epoch={vote_epoch}, block={query_block}[/cyan]")
        
        # Calculate optimal allocation
        console.print("[cyan]Calculating optimal allocation...[/cyan]")
        allocation = calculate_optimal_allocation(
            conn=conn,
            snapshot_ts=snapshot_ts,
            your_voting_power=args.your_voting_power,
            top_k=args.top_k,
        )
        
        if not allocation:
            console.print("[red]No allocation generated[/red]")
            sys.exit(1)
        
        console.print(f"[green]✓ Allocated to {len(allocation)} pools[/green]")
        
        # Display allocation
        from rich.table import Table
        table = Table(title="Auto-Voter Allocation")
        table.add_column("#", justify="right")
        table.add_column("Pool Address")
        table.add_column("Votes", justify="right")
        
        pool_addresses = []
        vote_proportions = []
        # Use constant proportions (relative weights) - contract normalizes them
        PROPORTION_PER_POOL = 10000  # Equal weight for equal allocation
        for idx, (gauge_addr, pool_addr, votes) in enumerate(allocation, start=1):
            table.add_row(str(idx), pool_addr, f"{votes:,}")
            pool_addresses.append(pool_addr)
            vote_proportions.append(PROPORTION_PER_POOL)  # Use proportion, not absolute votes
        
        console.print(table)
        
        # Validate allocation
        if not validate_allocation(allocation, args.your_voting_power):
            console.print("[red]Allocation validation failed[/red]")
            sys.exit(1)
        
        # Load voter contract
        voter_contract = w3.eth.contract(
            address=Web3.to_checksum_address(VOTER_ADDRESS),
            abi=VOTER_ABI,
        )
        
        # Build and send transaction
        console.print("\n[bold cyan]═══ EXECUTING VOTE ═══[/bold cyan]\n")
        
        success, result = build_and_send_vote_transaction(
            w3=w3,
            voter_contract=voter_contract,
            wallet=wallet,
            pool_addresses=[Web3.to_checksum_address(addr) for addr in pool_addresses],
            vote_proportions=vote_proportions,
            max_gas_price_gwei=args.max_gas_price_gwei,
            dry_run=args.dry_run,
        )
        
        if success:
            console.print(f"\n[bold green]✓ AUTO-VOTE COMPLETED SUCCESSFULLY[/bold green]")
            if not args.dry_run:
                console.print(f"[green]Transaction Hash: {result}[/green]")
        else:
            console.print(f"\n[bold red]✗ AUTO-VOTE FAILED: {result}[/bold red]")
            sys.exit(1)
        
    finally:
        conn.close()


if __name__ == "__main__":
    main()
