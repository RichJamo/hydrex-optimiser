#!/usr/bin/env python3
"""
Query actual votes cast for the 3 pools in the epoch that just finished.
"""

import json
import os
from web3 import Web3
from web3.contract import Contract
from dotenv import load_dotenv
from datetime import datetime, timedelta
from rich.console import Console
from rich.table import Table

load_dotenv()

console = Console()

# Contract and user
VOTER_V5 = "0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b"
USER_ADDRESS = "0x768a675B8542F23C428C6672738E380176E7635C"
BASE_RPC = os.getenv("RPC_URL", "https://mainnet.base.org")

POOLS = {
    "0x51f0b932855986b0e621c9d4db6eee1f4644d3d2": "HYDX/USDC",
    "0xef96ec76eeb36584fc4922e9fa268e0780170f33": "kVCM/USDC",
    "0x82dbe18346a8656dbb5e76f74bf3ae279cc16b29": "WETH/USDC",
}

# Connect
w3 = Web3(Web3.HTTPProvider(BASE_RPC))
if not w3.is_connected():
    console.print("[red]Failed to connect to Base[/red]")
    exit(1)

console.print("[green]Connected to Base[/green]")

# Load VoterV5 ABI
with open("voterv5_abi.json", "r") as f:
    voter_abi = json.load(f)

voter = w3.eth.contract(
    address=Web3.to_checksum_address(VOTER_V5), 
    abi=voter_abi
)

console.print("\n[cyan]Querying VoterV5 contract...[/cyan]\n")

# Get current and previous epoch timestamps
try:
    current_epoch_ts = voter.functions._epochTimestamp().call()
    console.print(f"Current epoch: {current_epoch_ts} ({datetime.utcfromtimestamp(current_epoch_ts).isoformat()})")
    
    # Previous epoch is 7 days (604800 seconds) before
    previous_epoch_ts = current_epoch_ts - (7 * 24 * 60 * 60)
    console.print(f"Previous epoch (just finished): {previous_epoch_ts} ({datetime.utcfromtimestamp(previous_epoch_ts).isoformat()})")
except Exception as e:
    console.print(f"[red]Error getting epochs: {e}[/red]")
    exit(1)

console.print("\n[cyan]Actual votes cast on your 3 pools:[/cyan]\n")

# Create analysis table
table = Table(show_header=True, header_style="bold cyan")
table.add_column("Pool", width=15)
table.add_column("Your Votes", width=18, justify="right", style="yellow")
table.add_column("Total Votes", width=18, justify="right", style="cyan")
table.add_column("Your Share %", width=15, justify="right", style="green")

total_your_votes = 0
total_pool_votes = 0

results = {}

for pool_addr, pool_name in POOLS.items():
    try:
        # Your current votes for this pool
        your_votes_wei = voter.functions.votes(
            Web3.to_checksum_address(USER_ADDRESS),
            Web3.to_checksum_address(pool_addr)
        ).call()
        
        # Total votes for this pool in the previous (just-finished) epoch
        total_votes_wei = voter.functions.weightsAt(
            Web3.to_checksum_address(pool_addr),
            previous_epoch_ts
        ).call()
        
        # Convert from wei (18 decimals)
        your_votes = your_votes_wei / 1e18
        total_votes = total_votes_wei / 1e18
        
        your_share_pct = (your_votes / total_votes * 100) if total_votes > 0 else 0
        
        table.add_row(
            pool_name,
            f"{your_votes:,.0f}",
            f"{total_votes:,.0f}",
            f"{your_share_pct:.4f}%"
        )
        
        results[pool_name] = {
            "your_votes": your_votes,
            "total_votes": total_votes,
            "your_share_pct": your_share_pct
        }
        
        total_your_votes += your_votes
        total_pool_votes += total_votes
        
    except Exception as e:
        console.print(f"[red]Error querying {pool_name}: {e}[/red]")

console.print(table)

console.print(f"\n[bold cyan]Summary:[/bold cyan]")
console.print(f"Total votes you cast: {total_your_votes:,.0f}")
console.print(f"Sum of total votes in pools: {total_pool_votes:,.0f}")

# Calculate expected rewards based on actual share
console.print(f"\n[bold cyan]Expected reward calculation:[/bold cyan]\n")

# Load the closed epoch data for bribe amounts
with open("closed_epoch_data.json") as f:
    epoch_data = json.load(f)

for pool_name, actual_votes_info in results.items():
    # Find this pool in epoch_data
    pool_addr = None
    for addr, name in POOLS.items():
        if name == pool_name:
            pool_addr = addr
            break
    
    if pool_addr and pool_addr in epoch_data["pools_analysis"]:
        pool_info = epoch_data["pools_analysis"][pool_addr]
        total_bribes = pool_info["total_usd"]
        
        # Expected reward = total_bribes * your_share
        expected_reward = total_bribes * (actual_votes_info["your_share_pct"] / 100)
        
        console.print(f"[yellow]{pool_name}[/yellow]")
        console.print(f"  Your share: {actual_votes_info['your_share_pct']:.4f}%")
        console.print(f"  Total bribes: ${total_bribes:,.2f}")
        console.print(f"  Expected reward: ${expected_reward:,.2f}")
        console.print()

# Save results
with open("actual_votes_on_chain.json", "w") as f:
    json.dump({
        "epoch": previous_epoch_ts,
        "user_address": USER_ADDRESS,
        "pools": results,
        "total_your_votes": total_your_votes,
        "timestamp_utc": datetime.utcfromtimestamp(previous_epoch_ts).isoformat()
    }, f, indent=2)

console.print(f"[green]Saved on-chain vote data to actual_votes_on_chain.json[/green]")
