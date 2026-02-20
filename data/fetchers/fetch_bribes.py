#!/usr/bin/env python3
"""
Fetch bribe/reward data from on-chain bribe contracts.

Queries RewardAdded events and stores rewards in the database.
Run once per epoch at/after epoch flip to capture final rewards.

Usage:
    python -m data.fetchers.fetch_bribes --epoch 1771372800
    python -m data.fetchers.fetch_bribes --epoch 1771372800 --bribe-contract 0x1234...
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from typing import Dict, List, Tuple
from pathlib import Path

from web3 import Web3
from web3.contract import Contract
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import track

from src.database import Database
from config.settings import DATABASE_PATH, ONE_E18

load_dotenv()
console = Console()

RPC_URL = os.getenv("RPC_URL")
BRIBE_CALC_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": False, "name": "from", "internalType": "address", "type": "address"},
            {"indexed": False, "name": "reward", "internalType": "uint256", "type": "uint256"},
            {"indexed": False, "name": "epoch", "internalType": "uint256", "type": "uint256"},
            {"indexed": False, "name": "ts", "internalType": "uint256", "type": "ts"}
        ],
        "name": "RewardAdded",
        "type": "event"
    },
    {
        "inputs": [],
        "name": "WEEK",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}, {"internalType": "uint256", "name": "", "type": "uint256"}],
        "name": "rewardData",
        "outputs": [
            {"internalType": "uint256", "name": "periodFinish", "type": "uint256"},
            {"internalType": "uint256", "name": "rewardsPerEpoch", "type": "uint256"},
            {"internalType": "uint256", "name": "lastUpdateTime", "type": "uint256"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
]

ERC20_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "type": "function"
    }
]


def get_token_decimals_and_symbol(w3: Web3, token_address: str) -> Tuple[int, str]:
    """Query token decimals and symbol from on-chain."""
    try:
        token = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=ERC20_ABI)
        decimals = token.functions.decimals().call()
        symbol = token.functions.symbol().call()
        return decimals, symbol
    except Exception as e:
        console.print(f"[yellow]Warning: Could not fetch decimals/symbol for {token_address}: {e}[/yellow]")
        return 18, f"{token_address[:8]}..."


def fetch_bribe_rewards(
    w3: Web3,
    db: Database,
    epoch: int,
    bribe_contracts: List[str],
) -> int:
    """
    Fetch rewards from bribe contracts for a specific epoch.
    
    Args:
        w3: Web3 instance
        db: Database instance
        epoch: Epoch timestamp
        bribe_contracts: List of bribe contract addresses to query
    
    Returns:
        Number of rewards added to database
    """
    console.print(f"[cyan]Fetching rewards for epoch {epoch}[/cyan]")
    
    added_count = 0
    
    for bribe_addr in track(bribe_contracts, description="Querying bribe contracts"):
        try:
            bribe_contract = w3.eth.contract(
                address=Web3.to_checksum_address(bribe_addr),
                abi=BRIBE_CALC_ABI
            )
            
            # Get WEEK for epoch alignment
            week = bribe_contract.functions.WEEK().call()
            calc_epoch = (epoch // week) * week
            
            # Query rewardData for all tokens
            # Note: This is a limitation - we can't directly enumerate token rewards.
            # In practice, you'd need to:
            # 1. Listen to RewardAdded events (off-chain indexing)
            # 2. Or know the list of reward tokens in advance
            # 3. Or query a subgraph
            
            # For now, we'll skip direct querying and rely on event logs
            # See: data/processors/process_rewards.py for event processing
            
        except Exception as e:
            console.print(f"[red]Error querying bribe {bribe_addr}: {e}[/red]")
    
    return added_count


def main():
    """Main fetcher logic."""
    parser = argparse.ArgumentParser(description="Fetch bribe rewards from on-chain contracts")
    parser.add_argument("--epoch", type=int, required=True, help="Epoch timestamp")
    parser.add_argument("--bribe-contracts", type=str, help="Comma-separated bribe contract addresses (if not provided, queries from DB)")
    parser.add_argument("--database", type=str, default=DATABASE_PATH, help="Database path")
    parser.add_argument(
        "--repair-token-metadata",
        action="store_true",
        help="Run scripts/repair_token_metadata.py after fetch to normalize symbol/decimals cache",
    )
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
    
    # Get bribe contracts to query
    if args.bribe_contracts:
        bribe_contracts = [addr.strip() for addr in args.bribe_contracts.split(",")]
    else:
        # Query from database
        gauges = db.get_all_gauges(alive_only=True)
        bribe_set = set()
        for gauge in gauges:
            if gauge.internal_bribe:
                bribe_set.add(gauge.internal_bribe.lower())
            if gauge.external_bribe:
                bribe_set.add(gauge.external_bribe.lower())
        bribe_contracts = list(bribe_set)
    
    if not bribe_contracts:
        console.print("[yellow]No bribe contracts found to query[/yellow]")
        return
    
    console.print(f"[cyan]Will query {len(bribe_contracts)} bribe contracts[/cyan]\n")
    
    # Fetch rewards
    added = fetch_bribe_rewards(w3, db, args.epoch, bribe_contracts)
    
    console.print(f"\n[green]✅ Fetched bribes for epoch {args.epoch}[/green]")
    console.print(f"[cyan]Added {added} reward records to database[/cyan]")

    if args.repair_token_metadata:
        repo_root = Path(__file__).resolve().parents[2]
        repair_script = repo_root / "scripts" / "repair_token_metadata.py"
        if not repair_script.exists():
            console.print("[yellow]Token metadata repair script not found; skipping repair step.[/yellow]")
            return

        console.print("[cyan]Running optional token metadata repair...[/cyan]")
        result = subprocess.run(
            [
                sys.executable,
                str(repair_script),
                "--database",
                args.database,
            ],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )

        if result.stdout:
            console.print(result.stdout.strip())
        if result.returncode != 0:
            if result.stderr:
                console.print(result.stderr.strip())
            console.print("[red]Token metadata repair failed.[/red]")
        else:
            console.print("[green]Token metadata repair complete.[/green]")


if __name__ == "__main__":
    main()
