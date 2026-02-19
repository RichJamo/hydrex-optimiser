#!/usr/bin/env python3
"""
Fetch final state of epoch 1771372800 (just closed) to reconcile actual rewards.
"""

import json
import sqlite3
from web3 import Web3
from web3.contract import Contract
import os
from dotenv import load_dotenv
from typing import Dict, List, Tuple
from rich.console import Console
from rich.progress import track

load_dotenv()

console = Console()

# Contract addresses
VOTER_V5 = "0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b"
USER_ADDRESS = "0x768a675B8542F23C428C6672738E380176E7635C"
BASE_RPC = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")

# Connect to Base
w3 = Web3(Web3.HTTPProvider(BASE_RPC))
if not w3.is_connected():
    console.print("[red]Failed to connect to Base RPC[/red]")
    exit(1)

console.print(f"[green]Connected to Base[/green]")

# Load VoterV5 ABI
with open("voterv5_abi.json", "r") as f:
    voter_abi = json.load(f)

voter_contract = w3.eth.contract(address=Web3.to_checksum_address(VOTER_V5), abi=voter_abi)

# Target epoch that just closed
CLOSED_EPOCH = 1771372800

console.print(f"\n[cyan]Querying final state of epoch {CLOSED_EPOCH}[/cyan]")

# Connected to database
DATABASE_PATH = "data.db"
conn = sqlite3.connect(DATABASE_PATH)
cursor = conn.cursor()

# Your votes in basis points
YOUR_VOTES_BP = {
    "0x51f0b932855986b0e621c9d4db6eee1f4644d3d2": 4200,    # HYDX/USDC - 42%
    "0xef96ec76eeb36584fc4922e9fa268e0780170f33": 2400,    # kVCM/USDC - 24%
    "0x82dbe18346a8656dbb5e76f74bf3ae279cc16b29": 3400,    # WETH/USDC - 34%
}

USER_VOTING_POWER = 1_530_896

# Convert BP to actual votes
YOUR_ACTUAL_VOTES = {}
for pool_addr, bp in YOUR_VOTES_BP.items():
    votes = int((bp / 10_000) * USER_VOTING_POWER)
    YOUR_ACTUAL_VOTES[pool_addr.lower()] = votes
    console.print(f"  {bp:,} BP = {votes:,} votes")

console.print(f"\n[cyan]Fetching final state for your 3 pools...[/cyan]")

# Get gauges for only our 3 pools
our_gauges = {}
placeholders = ",".join(["?"] * len(YOUR_VOTES_BP))
cursor.execute(f"SELECT pool, address FROM gauges WHERE pool IN ({placeholders})", 
               list(YOUR_VOTES_BP.keys()))

for pool_addr, gauge_addr in cursor.fetchall():
    our_gauges[pool_addr.lower()] = gauge_addr

console.print(f"[green]Found {len(our_gauges)} gauges for your voted pools[/green]")

# Query final weights
our_pools_final = {}
for pool_lower, votes_bp in YOUR_VOTES_BP.items():
    if pool_lower in our_gauges:
        gauge_addr = our_gauges[pool_lower]
        try:
            weight = voter_contract.functions.weights(
                Web3.to_checksum_address(gauge_addr),
                CLOSED_EPOCH
            ).call()
            
            our_pools_final[pool_lower] = {
                "gauge": gauge_addr,
                "final_weight": weight,
                "your_votes_bp": votes_bp,
                "your_votes_actual": YOUR_ACTUAL_VOTES[pool_lower]
            }
            console.print(f"  {pool_lower[:10]}...: {weight:,} total votes at epoch close")
        except Exception as e:
            console.print(f"  [yellow]{pool_lower[:10]}...: Query failed, will use DB data[/yellow]")
            our_pools_final[pool_lower] = {
                "gauge": gauge_addr,
                "final_weight": None,
                "your_votes_bp": votes_bp,
                "your_votes_actual": YOUR_ACTUAL_VOTES[pool_lower]
            }

# Now fetch latest bribe data from database for only our 3 pools
console.print(f"\n[cyan]Fetching final bribe data for your 3 pools...[/cyan]")

final_analysis = {}

# Get gauge addresses first
gauge_addrs = [our_gauges[pool] for pool in our_gauges.keys()]
placeholders = ",".join(["?"] * len(gauge_addrs))

cursor.execute(f"""
    SELECT g.pool, b.gauge_address,
           SUM(CASE WHEN b.bribe_type = 'internal' THEN b.usd_value ELSE 0 END) as internal_usd,
           SUM(CASE WHEN b.bribe_type = 'external' THEN b.usd_value ELSE 0 END) as external_usd,
           SUM(b.usd_value) as total_usd
    FROM bribes b
    JOIN gauges g ON b.gauge_address = g.address
    WHERE b.gauge_address IN ({placeholders}) AND b.epoch = ?
    GROUP BY b.gauge_address
""", gauge_addrs + [CLOSED_EPOCH])

for pool_addr, gauge_addr, internal_usd, external_usd, total_usd in cursor.fetchall():
    pool_lower = pool_addr.lower()
    final_analysis[pool_lower] = {
        "gauge": gauge_addr,
        "internal_usd": internal_usd or 0,
        "external_usd": external_usd or 0,
        "total_usd": total_usd or 0,
        "your_votes_bp": YOUR_VOTES_BP.get(pool_lower, 0),
        "your_votes_actual": YOUR_ACTUAL_VOTES.get(pool_lower, 0),
        "final_weight": our_pools_final.get(pool_lower, {}).get("final_weight")
    }

# Save results to file for analysis
with open("closed_epoch_data.json", "w") as f:
    json.dump({
        "epoch": CLOSED_EPOCH,
        "user_address": USER_ADDRESS,
        "user_voting_power": USER_VOTING_POWER,
        "your_votes": YOUR_ACTUAL_VOTES,
        "pools_analysis": {k: {**v, "gauge": str(v["gauge"])} for k, v in final_analysis.items()},
        "timestamp_utc": "2026-02-19T00:00:00Z"
    }, f, indent=2, default=str)

console.print(f"\n[green]Saved final epoch data to closed_epoch_data.json[/green]")
console.print(f"\n[cyan]Final bribe distributions (epoch {CLOSED_EPOCH}):[/cyan]")

for pool_lower in sorted(final_analysis.keys()):
    data = final_analysis[pool_lower]
    if pool_lower == "0x51f0b932855986b0e621c9d4db6eee1f4644d3d2":
        name = "HYDX/USDC"
    elif pool_lower == "0xef96ec76eeb36584fc4922e9fa268e0780170f33":
        name = "kVCM/USDC"
    else:
        name = "WETH/USDC"
    
    console.print(f"\n[bold]{name}[/bold]")
    console.print(f"  Your voting power: {data['your_votes_bp']} BP = {data['your_votes_actual']:,} votes")
    console.print(f"  Total bribes: ${data['total_usd']:,.2f}")
    console.print(f"    - Internal (fees): ${data['internal_usd']:,.2f}")
    console.print(f"    - External (bribes): ${data['external_usd']:,.2f}")

conn.close()
