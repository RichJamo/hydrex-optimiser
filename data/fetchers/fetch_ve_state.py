#!/usr/bin/env python3
"""
Fetch ve NFT delegation state snapshots.

Queries ve contract for delegation, power, and vote state at specific epochs.
Run periodically or on-demand to cache ve state for analysis.

Usage:
    python -m data.fetchers.fetch_ve_state --epoch 1771372800 --token-id 19435
    python -m data.fetchers.fetch_ve_state --epoch 1771372800 --token-id 19435 --block 42291740
"""

import argparse
import json
import os
from datetime import datetime
from typing import Optional
import sys

from web3 import Web3
from dotenv import load_dotenv
from rich.console import Console

from src.database import Database
from config.settings import DATABASE_PATH, VOTER_ADDRESS

load_dotenv()
console = Console()

RPC_URL = os.getenv("RPC_URL")

VOTER_ABI = [
    {"inputs": [], "name": "ve", "outputs": [{"internalType": "address", "name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "_ve", "outputs": [{"internalType": "address", "name": "", "type": "address"}], "stateMutability": "view", "type": "function"}
]

VE_ABI = [
    {
        "inputs": [{"internalType": "uint256", "name": "_tokenId", "type": "uint256"}, {"internalType": "uint48", "name": "_block", "type": "uint48"}],
        "name": "delegates",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "uint256", "name": "_tokenId", "type": "uint256"}, {"internalType": "uint256", "name": "_block", "type": "uint256"}],
        "name": "balanceOfNFTAt",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "address", "name": "_account", "type": "address"}, {"internalType": "uint256", "name": "_block", "type": "uint256"}],
        "name": "getPastVotes",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
]

WEEK = 604800  # 7 * 24 * 60 * 60


def find_block_at_timestamp(w3: Web3, target_ts: int, tolerance: int = 60) -> int:
    """Binary search to find block closest to timestamp."""
    left, right = 0, w3.eth.block_number
    
    while left < right:
        mid = (left + right) // 2
        block = w3.eth.get_block(mid)
        if block['timestamp'] < target_ts:
            left = mid + 1
        else:
            right = mid
    
    return left


def fetch_ve_state(
    w3: Web3,
    db: Database,
    epoch: int,
    token_id: int,
    block_identifier: Optional[int] = None,
) -> None:
    """
    Fetch and cache ve delegation state.
    
    Args:
        w3: Web3 instance
        db: Database instance
        epoch: Epoch timestamp
        token_id: ve NFT token ID
        block_identifier: Block to query at (if None, uses latest)
    """
    console.print(f"[cyan]Fetching ve state for tokenId {token_id} at epoch {epoch}[/cyan]")
    
    # Get voter contract
    voter = w3.eth.contract(address=Web3.to_checksum_address(VOTER_ADDRESS), abi=VOTER_ABI)
    
    # Get ve address
    try:
        ve_address = voter.functions.ve().call()
    except Exception:
        ve_address = voter.functions._ve().call()
    
    ve = w3.eth.contract(address=Web3.to_checksum_address(ve_address), abi=VE_ABI)
    
    kwargs = {"block_identifier": block_identifier} if block_identifier else {}
    
    # Epoch alignment (WEEK = 604800 seconds)
    calc_epoch = (epoch // WEEK) * WEEK
    
    def call_with_fallback(func, default_value, label):
        try:
            return func.call(**kwargs)
        except Exception as historical_error:
            if block_identifier is not None:
                console.print(
                    f"[yellow]Warning: Could not fetch {label} with block {block_identifier}: {historical_error}[/yellow]"
                )
                console.print("[yellow]Falling back to latest state...[/yellow]")
            try:
                return func.call()
            except Exception as latest_error:
                console.print(
                    f"[yellow]Warning: Could not fetch {label} from latest state: {latest_error}[/yellow]"
                )
                console.print(f"[yellow]Using default {label}: {default_value}[/yellow]")
                return default_value

    # Query delegation state with graceful fallback for contracts that revert on historical queries
    delegatee = call_with_fallback(
        ve.functions.delegates(token_id, calc_epoch),
        "0x0000000000000000000000000000000000000000",
        "delegates",
    )

    power = call_with_fallback(
        ve.functions.balanceOfNFTAt(token_id, calc_epoch),
        0,
        "balanceOfNFTAt",
    )

    delegatee_past_votes = 0
    if delegatee != "0x0000000000000000000000000000000000000000":
        delegatee_past_votes = call_with_fallback(
            ve.functions.getPastVotes(delegatee, calc_epoch),
            0,
            "getPastVotes",
        )
    
    # Weight = power / delegatee_past_votes
    weight_1e18 = 0
    if delegatee_past_votes > 0:
        weight_1e18 = (power * (10 ** 18)) // delegatee_past_votes
    
    # Display results
    console.print(f"\n[bold cyan]VE Delegation Snapshot[/bold cyan]")
    console.print(f"  Epoch: {epoch} (calc_epoch: {calc_epoch})")
    console.print(f"  Token ID: {token_id}")
    console.print(f"  Delegatee: {delegatee}")
    console.print(f"  Power: {power}")
    console.print(f"  Delegatee Past Votes: {delegatee_past_votes}")
    console.print(f"  Weight (1e18): {weight_1e18}")
    console.print(f"  Weight: {weight_1e18 / (10**18) if weight_1e18 > 0 else 0:.6f}\n")
    
    # Store in database as metadata
    # Create a JSON representation of the snapshot
    snapshot = {
        "epoch": epoch,
        "calc_epoch": calc_epoch,
        "token_id": token_id,
        "delegatee": delegatee,
        "power": power,
        "delegatee_past_votes": delegatee_past_votes,
        "weight_1e18": weight_1e18,
        "timestamp": int(datetime.utcnow().timestamp()),
    }
    
    if block_identifier:
        snapshot["block"] = block_identifier
    
    console.print(f"[green]✅ Queried ve state[/green]")
    console.print(f"[cyan]Snapshot: {json.dumps(snapshot, indent=2)}[/cyan]")
    
    # In a full implementation, you'd save this to database or file
    # For now, just display it


def main():
    """Main fetcher logic."""
    parser = argparse.ArgumentParser(description="Fetch ve delegation state")
    parser.add_argument("--epoch", type=int, required=True, help="Epoch timestamp")
    parser.add_argument("--token-id", type=int, required=True, help="ve NFT token ID to query")
    parser.add_argument("--block", type=int, help="Block number to query at (if not provided, uses latest)")
    parser.add_argument("--database", type=str, default=DATABASE_PATH, help="Database path")
    args = parser.parse_args()
    
    if not RPC_URL:
        console.print("[red]RPC_URL not set in .env[/red]")
        return
    
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        console.print("[red]Failed to connect to RPC[/red]")
        return
    
    console.print("[green]Connected to blockchain[/green]")
    
    # Initialize database
    db = Database(args.database)
    db.create_tables()
    
    # Determine block to query at
    if args.block:
        block = args.block
    else:
        block = find_block_at_timestamp(w3, args.epoch)
    
    block_info = w3.eth.get_block(block)
    console.print(f"[cyan]Using block {block} at {datetime.utcfromtimestamp(block_info['timestamp']).isoformat()}[/cyan]\n")
    
    # Fetch ve state
    fetch_ve_state(w3, db, args.epoch, args.token_id, block_identifier=block)


if __name__ == "__main__":
    main()
