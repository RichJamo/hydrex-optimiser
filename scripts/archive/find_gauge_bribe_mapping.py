#!/usr/bin/env python3
"""
Find gauges for the voted pools by querying the Voter contract directly
"""

import json
from web3 import Web3
from rich.console import Console
from rich.table import Table

console = Console()

# Setup web3
RPC_URL = "https://base-mainnet.g.alchemy.com/v2/oFfvEpXYjGo8Nj4QQIkU3kXd6Z0JvfJZ"
w3 = Web3(Web3.HTTPProvider(RPC_URL))
console.print(f"Connected to Base: {w3.is_connected()}\n")

# Load VoterV5 ABI
with open("voterv5_abi.json", "r") as f:
    voter_abi = json.load(f)

VOTER_ADDRESS = "0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b"
voter = w3.eth.contract(address=Web3.to_checksum_address(VOTER_ADDRESS), abi=voter_abi)

# Your voted pools
pools_voted = [
    "0x3f9b863EF4B295d6Ba370215bcCa3785FCC44f44",  # WETH/cbBTC
    "0x0BA69825c4C033e72309F6AC0Bde0023b15Cc97c",  # USDC/cbBTC
    "0x680581725840958141Bb328666D8Fc185aC4FA49",  # BNKR/WETH
    "0xEf96Ec76eEB36584FC4922e9fA268e0780170f33",  # kVCM/USDC
]

# Bribe contracts that paid
bribe_contracts = [
    "0xdbd3DA2c3183a4db0d6a1E648a06B14b593dB7B5",  # $246.75
    "0x71aaE818Cd357f62C3aD25B5012cC27587442AaE",  # $236.10
    "0x7c02E7A38774317DFC72c2506FD642De2C55A7de",  # $10.71
    "0xC96802e581c7B7ecC4ccFF37e0eE2b60bBe6741f",  # $225.97
    "0x6b4e7d1752257cdc266b380b0F980cF75D3a2465",  # $235.00
]

console.print("[bold cyan]Looking up Gauge Information from Voter Contract[/bold cyan]")
console.print("=" * 100)

table = Table(show_header=True, show_lines=True)
table.add_column("Pool Address", style="cyan", width=44)
table.add_column("Gauge Address", style="yellow", width=44)

gauge_info = {}

for pool in pools_voted:
    pool_checksum = Web3.to_checksum_address(pool)
    
    try:
        # Get gauge address for this pool
        gauge_addr = voter.functions.gauges(pool_checksum).call()
        
        gauge_info[pool.lower()] = gauge_addr.lower()
        
        table.add_row(pool, gauge_addr)
    except Exception as e:
        table.add_row(pool, f"[red]Error: {e}[/red]")

console.print(table)

# Now query each gauge for its bribe contracts
console.print("\n[bold cyan]Querying Gauges for Bribe Contracts[/bold cyan]")
console.print("=" * 100)

bribe_table = Table(show_header=True, show_lines=True)
bribe_table.add_column("Pool", style="cyan", width=20)
bribe_table.add_column("Gauge", style="yellow", width=20)
bribe_table.add_column("Internal Bribe", style="green", width=20)
bribe_table.add_column("External Bribe", style="green", width=20)

gauge_bribe_map = {}

for pool, gauge_addr in gauge_info.items():
    gauge_checksum = Web3.to_checksum_address(gauge_addr)
    
    try:
        # Get internal bribe
        internal_bribe = voter.functions.internal_bribes(gauge_checksum).call()
        external_bribe = voter.functions.external_bribes(gauge_checksum).call()
        
        gauge_bribe_map[gauge_addr] = {
            'pool': pool,
            'internal': internal_bribe.lower(),
            'external': external_bribe.lower()
        }
        
        bribe_table.add_row(
            f"{pool[:8]}...{pool[-6:]}",
            f"{gauge_addr[:8]}...{gauge_addr[-6:]}",
            f"{internal_bribe[:8]}...{internal_bribe[-6:]}",
            f"{external_bribe[:8]}...{external_bribe[-6:]}",
        )
    except Exception as e:
        bribe_table.add_row(
            f"{pool[:8]}...{pool[-6:]}",
            f"{gauge_addr[:8]}...{gauge_addr[-6:]}",
            f"[red]Error[/red]",
            f"[red]Error[/red]"
        )

console.print(bribe_table)

# Match payments to pools
console.print("\n[bold cyan]Matching Payments to Pools[/bold cyan]")
console.print("=" * 100)

match_table = Table(show_header=True, show_lines=True)
match_table.add_column("Bribe Contract", style="cyan", width=44)
match_table.add_column("Type", style="yellow", width=10)
match_table.add_column("Pool Voted", style="green", width=44)

for bribe in bribe_contracts:
    bribe_lower = bribe.lower()
    matched = False
    
    for gauge_addr, info in gauge_bribe_map.items():
        if bribe_lower == info['internal']:
            match_table.add_row(bribe, "Internal", info['pool'])
            matched = True
            break
        elif bribe_lower == info['external']:
            match_table.add_row(bribe, "External", info['pool'])
            matched = True
            break
    
    if not matched:
        match_table.add_row(bribe, "[red]Unknown[/red]", "[red]No match[/red]")

console.print(match_table)

console.print("\n" + "=" * 100)
