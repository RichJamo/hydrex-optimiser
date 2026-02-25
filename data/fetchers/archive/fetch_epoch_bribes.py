#!/usr/bin/env python3
"""
Fetch historical bribe reward data for specified epochs.

This is the minimal-RPC bribe reward fetcher:
- Queries gauge_bribe_mapping to get all relevant bribe contracts
- For each epoch: fetches rewardData for all tokens in each bribe contract
- Stores in boundary_reward_snapshots table (pre-populated for boundary snapshot collector)
- Pre-populates rewards so boundary snapshot collector can run with minimal RPC

Usage:
  python -m data.fetchers.fetch_epoch_bribes --end-epoch 1770163200 --weeks 13
  python -m data.fetchers.fetch_epoch_bribes --end-epoch 1769040000 --weeks 1
"""

import argparse
import os
import sqlite3
import time
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv
from rich.console import Console
from web3 import Web3

from config.settings import DATABASE_PATH, ONE_E18, VOTER_ADDRESS, WEEK

load_dotenv()
console = Console()

RPC_URL = os.getenv("RPC_URL", "https://mainnet.base.org")


def _format_eta(seconds: float) -> str:
    """Format seconds into human-readable ETA string."""
    if seconds < 0:
        return "n/a"
    total = int(seconds)
    hrs = total // 3600
    mins = (total % 3600) // 60
    secs = total % 60
    if hrs > 0:
        return f"{hrs}h {mins}m {secs}s"
    if mins > 0:
        return f"{mins}m {secs}s"
    return f"{secs}s"


BRIBE_ABI = [
    {
        "inputs": [],
        "name": "rewardsListLength",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "name": "rewardTokens",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}],
        "name": "isRewardToken",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "", "type": "address"},
            {"internalType": "uint256", "name": "", "type": "uint256"},
        ],
        "name": "rewardData",
        "outputs": [
            {"internalType": "uint256", "name": "periodFinish", "type": "uint256"},
            {"internalType": "uint256", "name": "rewardsPerEpoch", "type": "uint256"},
            {"internalType": "uint256", "name": "lastUpdateTime", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]


def ensure_boundary_reward_snapshots(conn: sqlite3.Connection) -> None:
    """Ensure boundary_reward_snapshots table exists and has required columns."""
    cur = conn.cursor()
    
    # Check if table exists
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='boundary_reward_snapshots'")
    if not cur.fetchone():
        # Create table
        cur.execute(
            """
            CREATE TABLE boundary_reward_snapshots (
                epoch INTEGER NOT NULL,
                vote_epoch INTEGER NOT NULL,
                active_only INTEGER NOT NULL,
                boundary_block INTEGER,
                gauge_address TEXT NOT NULL,
                bribe_contract TEXT,
                reward_token TEXT NOT NULL,
                rewards_raw TEXT,
                token_decimals INTEGER,
                usd_price REAL,
                total_usd REAL NOT NULL,
                computed_at INTEGER NOT NULL,
                PRIMARY KEY (epoch, vote_epoch, active_only, gauge_address, bribe_contract, reward_token)
            )
            """
        )
    
    conn.commit()

def load_epoch_boundary(conn: sqlite3.Connection, epoch: int) -> Optional[Tuple[int, int]]:
    """Load (boundary_block, vote_epoch) from epoch_boundaries if available."""
    cur = conn.cursor()
    try:
        row = cur.execute(
            """
            SELECT boundary_block, vote_epoch
            FROM epoch_boundaries
            WHERE epoch = ?
            """,
            (int(epoch),),
        ).fetchone()
    except sqlite3.OperationalError:
        return None

    if not row or row[0] is None or row[1] is None:
        return None
    return int(row[0]), int(row[1])


def load_gauge_bribe_mapping(conn: sqlite3.Connection) -> Dict[str, Tuple[str, str]]:
    """Load gauge→(internal_bribe, external_bribe) from mapping table."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT gauge_address, internal_bribe, external_bribe FROM gauge_bribe_mapping")
        return {g: (ib, eb) for g, ib, eb in cur.fetchall()}
    except sqlite3.OperationalError:
        console.print("[bold red]ERROR: gauge_bribe_mapping table not found. Run fetch_gauge_bribe_mapping.py first.[/bold red]")
        raise


def find_block_at_timestamp(w3: Web3, target_timestamp: int, tolerance: int = 60) -> int:
    """Binary search to find block at target timestamp."""
    latest_block = w3.eth.block_number
    latest_ts = w3.eth.get_block(latest_block)["timestamp"]

    if target_timestamp > latest_ts:
        return latest_block

    blocks_back = int((latest_ts - target_timestamp) / 2)
    left = max(0, latest_block - blocks_back - 2000)
    right = latest_block
    best = left

    while left <= right:
        mid = (left + right) // 2
        blk = w3.eth.get_block(mid)
        ts = blk["timestamp"]

        if abs(ts - target_timestamp) <= tolerance:
            return mid

        if ts < target_timestamp:
            best = mid
            left = mid + 1
        else:
            right = mid - 1

    return best


def get_unique_bribe_contracts(mapping: Dict[str, Tuple[str, str]]) -> Set[str]:
    """Extract all unique bribe contracts from mapping."""
    bribes = set()
    for internal, external in mapping.values():
        if internal and internal != "0x0000000000000000000000000000000000000000":
            bribes.add(internal.lower())
        if external and external != "0x0000000000000000000000000000000000000000":
            bribes.add(external.lower())
    return bribes


def enumerate_approved_tokens(
    w3: Web3,
    bribe_contract,
    block_identifier: int,
) -> List[str]:
    """Get all approved reward tokens for a bribe contract."""
    try:
        length = int(bribe_contract.functions.rewardsListLength().call(block_identifier=block_identifier))
    except Exception:
        return []

    tokens = []
    for idx in range(min(length, 1000)):  # safety limit
        try:
            token_addr = bribe_contract.functions.rewardTokens(idx).call(block_identifier=block_identifier)
            if token_addr and token_addr != "0x" + "0" * 40:
                token_lower = Web3.to_checksum_address(token_addr).lower()
                is_approved = False
                try:
                    is_approved = bool(
                        bribe_contract.functions.isRewardToken(Web3.to_checksum_address(token_addr)).call(
                            block_identifier=block_identifier
                        )
                    )
                except Exception:
                    pass
                if is_approved:
                    tokens.append(token_lower)
        except Exception:
            pass

    return tokens


def fetch_reward_data(
    w3: Web3,
    bribe_contract,
    token_address: str,
    vote_epoch: int,
    block_identifier: int,
) -> Optional[Tuple[float, int, int]]:
    """Fetch rewardData for a token at an epoch."""
    try:
        period_finish, rewards_per_epoch, last_update = bribe_contract.functions.rewardData(
            Web3.to_checksum_address(token_address),
            vote_epoch,
        ).call(block_identifier=block_identifier)
        return (float(rewards_per_epoch) / ONE_E18, int(period_finish), int(last_update))
    except Exception:
        return None


def fetch_epoch_bribes(
    conn: sqlite3.Connection,
    w3: Web3,
    epoch: int,
    vote_epoch: int,
    mapping: Dict[str, Tuple[str, str]],
    unique_bribes: Set[str],
    boundary_block: int,
    progress_every: int,
) -> Tuple[int, int]:
    """Fetch all reward data for bribes at a given epoch."""
    cur = conn.cursor()
    
    # Delete stale rows for this epoch/vote_epoch first
    cur.execute(
        "DELETE FROM boundary_reward_snapshots WHERE epoch = ? AND vote_epoch = ? AND active_only = 1",
        (epoch, vote_epoch),
    )
    conn.commit()
    
    rows_inserted = 0
    tokens_total = 0
    bribes_processed = 0
    
    phase_start = time.time()
    now_ts = int(time.time())

    for bribe_idx, bribe_addr in enumerate(sorted(unique_bribes), start=1):
        try:
            bribe_contract = w3.eth.contract(address=Web3.to_checksum_address(bribe_addr), abi=BRIBE_ABI)
        except Exception:
            continue

        bribes_processed += 1
        approved_tokens = enumerate_approved_tokens(w3, bribe_contract, boundary_block)
        tokens_total += len(approved_tokens)

        # Find which gauges use this bribe
        gauges_for_bribe = [
            g for g, (ib, eb) in mapping.items()
            if (ib and ib.lower() == bribe_addr.lower()) or (eb and eb.lower() == bribe_addr.lower())
        ]

        for token_addr in approved_tokens:
            reward_data = fetch_reward_data(w3, bribe_contract, token_addr, vote_epoch, boundary_block)
            if reward_data:
                rewards_per_epoch, period_finish, last_update = reward_data
                # Insert for each gauge that uses this bribe
                for gauge in gauges_for_bribe:
                    cur.execute(
                        """
                        INSERT OR REPLACE INTO boundary_reward_snapshots 
                        (epoch, vote_epoch, active_only, boundary_block, gauge_address, bribe_contract, reward_token, 
                         rewards_raw, computed_at, total_usd)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            epoch,
                            vote_epoch,
                            1,
                            boundary_block,
                            gauge.lower(),
                            bribe_addr.lower(),
                            token_addr.lower(),
                            str(rewards_per_epoch),
                            now_ts,
                            0.0,  # total_usd computed later
                        ),
                    )
                    rows_inserted += 1

        if progress_every > 0 and bribe_idx % progress_every == 0:
            elapsed = max(time.time() - phase_start, 1e-9)
            rate = bribe_idx / elapsed
            remaining = max(0, len(unique_bribes) - bribe_idx)
            eta = remaining / rate if rate > 0 else -1
            console.print(
                f"[dim]Epoch {epoch}: bribe progress {bribe_idx}/{len(unique_bribes)} | {rate:.2f}/s | ETA {_format_eta(eta)}[/dim]"
            )

    conn.commit()
    return rows_inserted, tokens_total


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch historical bribe reward data")
    parser.add_argument("--end-epoch", type=int, required=True, help="End epoch timestamp")
    parser.add_argument("--weeks", type=int, default=1, help="Number of weeks to go back (default 1)")
    parser.add_argument("--vote-epoch-offset-weeks", type=int, default=1, help="Weeks prior to epoch for vote_epoch (default 1)")
    parser.add_argument("--progress-every", type=int, default=25, help="Progress log frequency (default 25)")
    parser.add_argument("--block-tolerance", type=int, default=60, help="Block timestamp tolerance in seconds")

    args = parser.parse_args()

    # Generate epoch range
    epochs = [int(args.end_epoch - i * WEEK) for i in range(max(1, args.weeks))]
    epochs.sort()

    console.print(f"[bold cyan]Fetching bribe reward data for {len(epochs)} epochs[/bold cyan]")

    conn = sqlite3.connect(DATABASE_PATH)
    ensure_boundary_reward_snapshots(conn)

    console.print("[cyan]Loading gauge→bribe mapping[/cyan]")
    mapping = load_gauge_bribe_mapping(conn)
    unique_bribes = get_unique_bribe_contracts(mapping)
    console.print(f"[green]Loaded {len(mapping)} gauges with {len(unique_bribes)} unique bribe contracts[/green]")

    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    console.print(f"[cyan]Connected to RPC[/cyan]")

    total_rows = 0
    total_tokens = 0

    for epoch_idx, epoch in enumerate(epochs, start=1):
        boundary_row = load_epoch_boundary(conn, epoch)
        if boundary_row:
            boundary_block, vote_epoch = boundary_row
        else:
            vote_epoch = int(epoch - max(0, args.vote_epoch_offset_weeks) * WEEK)
            boundary_block = find_block_at_timestamp(w3, epoch, args.block_tolerance)
        console.print(f"[bold cyan]Epoch {epoch_idx}/{len(epochs)}[/bold cyan] epoch={epoch} vote_epoch={vote_epoch}")
        console.print(f"[cyan]  boundary_block={boundary_block}[/cyan]")

        rows, tokens = fetch_epoch_bribes(
            conn,
            w3,
            epoch,
            vote_epoch,
            mapping,
            unique_bribes,
            boundary_block,
            progress_every=max(0, args.progress_every),
        )
        total_rows += rows
        total_tokens += tokens

        console.print(f"[green]  {rows} rows inserted, {tokens} tokens fetched[/green]")

    cur = conn.cursor()
    cur.execute("SELECT COUNT(DISTINCT epoch) FROM boundary_reward_snapshots WHERE active_only = 1")
    distinct_epochs = cur.fetchone()[0]

    console.print()
    console.print(f"[bold green]✅ Bribe reward data fetch complete[/bold green]")
    console.print(f"   {total_rows} total reward snapshots inserted")
    console.print(f"   {total_tokens} total unique tokens fetched")
    console.print(f"   {distinct_epochs} distinct epochs in boundary_reward_snapshots")

    conn.close()


if __name__ == "__main__":
    main()
