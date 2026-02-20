#!/usr/bin/env python3
"""
Fetch vote data from VoterV5 contracts.

Queries gauge votes at a specific epoch and stores them in the database.
Run once per epoch to capture final vote distribution.

Usage:
    python -m data.fetchers.fetch_votes --epoch 1771372800 --vote-epoch 1770854400
    python -m data.fetchers.fetch_votes --epoch 1771372800 --vote-epoch 1770854400 --block 42291740
"""

import argparse
import os
from datetime import datetime
from typing import Dict, List

from web3 import Web3
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import track

from src.database import Database
from config.settings import DATABASE_PATH, VOTER_ADDRESS

load_dotenv()
console = Console()

RPC_URL = os.getenv("RPC_URL")

VOTER_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "_pool", "type": "address"},
            {"internalType": "uint256", "name": "_time", "type": "uint256"},
        ],
        "name": "weightsAt",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]


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


def fetch_votes(
    w3: Web3,
    db: Database,
    epoch: int,
    vote_epoch: int,
    block_identifier: int = None,
) -> int:
    """
    Fetch vote distribution for a specific epoch.
    
    Args:
        w3: Web3 instance
        db: Database instance
        epoch: Epoch timestamp (when this data snapshot was taken)
        vote_epoch: Vote epoch to query (usually earlier epoch)
        block_identifier: Block to query at (if None, uses latest)
    
    Returns:
        Number of vote records added to database
    """
    console.print(f"[cyan]Fetching votes for epoch {epoch} (vote_epoch={vote_epoch})[/cyan]")
    
    # Get voter contract
    voter = w3.eth.contract(address=Web3.to_checksum_address(VOTER_ADDRESS), abi=VOTER_ABI)
    
    # Get all gauges
    gauges = db.get_all_gauges(alive_only=False)
    if not gauges:
        console.print("[yellow]No gauges in database; run gauge fetcher first[/yellow]")
        return 0
    
    kwargs = {"block_identifier": block_identifier} if block_identifier else {}
    
    added_count = 0
    for gauge in track(gauges, description="Fetching votes per gauge"):
        try:
            # Get weight at vote_epoch
            weight = voter.functions.weightsAt(
                Web3.to_checksum_address(gauge.pool or gauge.address),
                vote_epoch
            ).call(**kwargs)
            
            if weight > 0:
                db.save_vote(epoch=epoch, gauge=gauge.address, total_votes=weight)
                added_count += 1
        
        except Exception as e:
            console.print(f"[yellow]Warning: Could not fetch weight for gauge {gauge.address}: {e}[/yellow]")
    
    return added_count


def main():
    """Main fetcher logic."""
    parser = argparse.ArgumentParser(description="Fetch vote distribution from VoterV5")
    parser.add_argument("--epoch", type=int, required=True, help="Epoch timestamp (when snapshot taken)")
    parser.add_argument("--vote-epoch", type=int, required=True, help="Vote epoch to query (usually earlier)")
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
    
    # Fetch votes
    added = fetch_votes(w3, db, args.epoch, args.vote_epoch, block_identifier=block)
    
    console.print(f"\n[green]✅ Fetched votes for epoch {args.epoch}[/green]")
    console.print(f"[cyan]Added {added} vote records to database[/cyan]")


if __name__ == "__main__":
    main()
