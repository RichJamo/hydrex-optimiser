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
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, List, Optional

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
    gauges: Optional[List] = None,
    max_workers: int = 1,
    snapshot_timestamp: Optional[int] = None,
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
    
    # Get all gauges (or use provided subset)
    if gauges is None:
        gauges = db.get_all_gauges(alive_only=False)
    if not gauges:
        console.print("[yellow]No gauges in database; run gauge fetcher first[/yellow]")
        return 0
    
    kwargs = {"block_identifier": block_identifier} if block_identifier else {}
    
    def _fetch_weight(gauge):
        try:
            weight = voter.functions.weightsAt(
                Web3.to_checksum_address(gauge.pool or gauge.address),
                vote_epoch,
            ).call(**kwargs)
            return gauge.address, weight, None
        except Exception as err:
            return gauge.address, 0, err

    write_timestamp = int(snapshot_timestamp) if snapshot_timestamp is not None else int(datetime.utcnow().timestamp())
    rows_to_insert: List[tuple] = []
    workers = max(1, int(max_workers or 1))
    if workers == 1:
        for gauge in track(gauges, description="Fetching votes per gauge"):
            addr, weight, err = _fetch_weight(gauge)
            if err is not None:
                console.print(f"[yellow]Warning: Could not fetch weight for gauge {addr}: {err}[/yellow]")
                continue
            if weight > 0:
                rows_to_insert.append((int(epoch), str(addr), float(weight), write_timestamp))
    else:
        console.print(f"[cyan]Using parallel vote fetch with {workers} workers[/cyan]")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_fetch_weight, gauge) for gauge in gauges]
            for future in track(as_completed(futures), total=len(futures), description="Fetching votes in parallel"):
                addr, weight, err = future.result()
                if err is not None:
                    console.print(f"[yellow]Warning: Could not fetch weight for gauge {addr}: {err}[/yellow]")
                    continue
                if weight > 0:
                    rows_to_insert.append((int(epoch), str(addr), float(weight), write_timestamp))

    if not rows_to_insert:
        return 0

    # Batch write in one transaction to reduce SQLite lock contention.
    conn = sqlite3.connect(db.engine.url.database, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.executemany(
            """
            INSERT INTO votes(epoch, gauge, total_votes, indexed_at)
            VALUES (?, ?, ?, ?)
            """,
            rows_to_insert,
        )
        conn.commit()
    finally:
        conn.close()
    
    return len(rows_to_insert)


def main():
    """Main fetcher logic."""
    parser = argparse.ArgumentParser(description="Fetch vote distribution from VoterV5")
    parser.add_argument("--epoch", type=int, required=True, help="Epoch timestamp (when snapshot taken)")
    parser.add_argument("--vote-epoch", type=int, required=True, help="Vote epoch to query (usually earlier)")
    parser.add_argument("--block", type=int, help="Block number to query at (if not provided, uses latest)")
    parser.add_argument(
        "--max-gauges",
        type=int,
        default=0,
        help="Limit number of gauges queried (0 = all gauges)",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=8,
        help="Parallel workers for RPC calls (1 = sequential)",
    )
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
    
    gauges = db.get_all_gauges(alive_only=False)
    if args.max_gauges and args.max_gauges > 0:
        gauges = gauges[: args.max_gauges]
        console.print(f"[cyan]Gauge limit enabled: querying {len(gauges)} gauges[/cyan]")

    # Fetch votes
    added = fetch_votes(
        w3,
        db,
        args.epoch,
        args.vote_epoch,
        block_identifier=block,
        gauges=gauges,
        max_workers=args.max_workers,
        snapshot_timestamp=int(block_info["timestamp"]),
    )
    
    console.print(f"\n[green]✅ Fetched votes for epoch {args.epoch}[/green]")
    console.print(f"[cyan]Added {added} vote records to database[/cyan]")


if __name__ == "__main__":
    main()
