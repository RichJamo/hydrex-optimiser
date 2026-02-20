#!/usr/bin/env python3
"""
Investigate WHEN internal bribe rewards (fees) become visible.
Check if fees accumulate during the epoch or only appear at the flip.
"""

import json
from web3 import Web3
from datetime import datetime
from rich.console import Console
from rich.table import Table

console = Console()

# Setup
RPC_URL = "https://base-mainnet.g.alchemy.com/v2/oFfvEpXYjGo8Nj4QQIkU3kXd6Z0JvfJZ"
w3 = Web3(Web3.HTTPProvider(RPC_URL))

# Load BribeV2 ABI
with open("bribev2_abi.json", "r") as f:
    bribe_abi = json.load(f)

# Your internal bribe contracts that paid out
internal_bribes = [
    {
        "address": "0xdbd3DA2c3183a4db0d6a1E648a06B14b593dB7B5",
        "pool": "WETH/cbBTC",
        "paid": 246.75
    },
    {
        "address": "0x71aaE818Cd357f62C3aD25B5012cC27587442AaE",
        "pool": "USDC/cbBTC",
        "paid": 236.10
    },
    {
        "address": "0x7c02E7A38774317DFC72c2506FD642De2C55A7de",
        "pool": "kVCM/USDC",
        "paid": 10.71
    },
    {
        "address": "0xC96802e581c7B7ecC4ccFF37e0eE2b60bBe6741f",
        "pool": "BNKR/WETH",
        "paid": 225.97
    }
]

console.print("\n[bold cyan]Investigating When Internal Bribes (Fees) Become Visible[/bold cyan]")
console.print("=" * 100)

# Get current block for reference
current_block = w3.eth.block_number
current_time = datetime.now()

console.print(f"Current Block: {current_block}")
console.print(f"Current Time: {current_time}")

# Estimate epoch timing (Wednesday 00:00 UTC epochs)
# Last Wednesday would be 7 days ago from today (Tuesday)
# So the flip would have been ~6 days ago

console.print("\n[bold yellow]Key Question:[/bold yellow]")
console.print("Do trading fees accumulate in the internal bribe contract DURING the epoch,")
console.print("or are they only deposited AT the epoch flip (Wednesday 00:00 UTC)?")

console.print("\n[bold cyan]Checking for NotifyReward Events[/bold cyan]")
console.print("=" * 100)

# Look back ~7-14 days to capture last epoch flip
blocks_per_day = 43200  # Base: ~2 sec blocks = 43200 blocks/day
lookback_blocks = blocks_per_day * 14
from_block = current_block - lookback_blocks

console.print(f"Searching from block {from_block} to {current_block} (~14 days)")

table = Table(show_header=True, show_lines=True)
table.add_column("Pool", style="cyan", width=15)
table.add_column("Internal Bribe Contract", style="yellow", width=44)
table.add_column("Events Found", style="green", width=40)

for bribe in internal_bribes:
    contract_addr = Web3.to_checksum_address(bribe["address"])
    contract = w3.eth.contract(address=contract_addr, abi=bribe_abi)
    
    console.print(f"\n[cyan]Checking {bribe['pool']}...[/cyan]")
    
    try:
        # Look for NotifyReward events - this is when rewards are added
        # Common event name in Bribe contracts
        event_filter = contract.events.NotifyReward.create_filter(
            fromBlock=from_block,
            toBlock='latest'
        )
        events = event_filter.get_all_entries()
        
        if events:
            event_info = []
            for event in events[-5:]:  # Last 5 events
                block = event['blockNumber']
                block_obj = w3.eth.get_block(block)
                timestamp = datetime.fromtimestamp(block_obj['timestamp'])
                
                # Try to get event data
                try:
                    reward_token = event['args'].get('rewardToken', 'Unknown')
                    amount = event['args'].get('amount', 0)
                    event_info.append(f"Block {block} ({timestamp.strftime('%Y-%m-%d %H:%M')})")
                except:
                    event_info.append(f"Block {block} ({timestamp.strftime('%Y-%m-%d %H:%M')})")
            
            table.add_row(
                bribe['pool'],
                f"{bribe['address'][:10]}...{bribe['address'][-6:]}",
                "\n".join(event_info) if event_info else "Unknown format"
            )
        else:
            table.add_row(
                bribe['pool'],
                f"{bribe['address'][:10]}...{bribe['address'][-6:]}",
                "[yellow]No NotifyReward events in last 14 days[/yellow]"
            )
            
    except Exception as e:
        # Try other potential event names
        try:
            # Some contracts use RewardAdded
            event_filter = contract.events.RewardAdded.create_filter(
                fromBlock=from_block,
                toBlock='latest'
            )
            events = event_filter.get_all_entries()
            
            if events:
                table.add_row(
                    bribe['pool'],
                    f"{bribe['address'][:10]}...{bribe['address'][-6:]}",
                    f"[green]Found {len(events)} RewardAdded events[/green]"
                )
            else:
                table.add_row(
                    bribe['pool'],
                    f"{bribe['address'][:10]}...{bribe['address'][-6:]}",
                    f"[red]No events found[/red]"
                )
        except:
            table.add_row(
                bribe['pool'],
                f"{bribe['address'][:10]}...{bribe['address'][-6:]}",
                f"[red]Error: {str(e)[:50]}[/red]"
            )

console.print(table)

console.print("\n[bold cyan]Checking Contract Balances[/bold cyan]")
console.print("=" * 100)

# Check what tokens are in the contracts now
balance_table = Table(show_header=True)
balance_table.add_column("Pool", style="cyan")
balance_table.add_column("Contract", style="yellow", width=20)
balance_table.add_column("Reward Tokens", style="green")

for bribe in internal_bribes:
    contract_addr = Web3.to_checksum_address(bribe["address"])
    contract = w3.eth.contract(address=contract_addr, abi=bribe_abi)
    
    try:
        # Get number of reward tokens
        reward_count = contract.functions.rewardsListLength().call()
        
        tokens = []
        for i in range(min(reward_count, 5)):  # Check first 5 tokens
            token_addr = contract.functions.rewardTokens(i).call()
            tokens.append(f"{token_addr[:8]}...{token_addr[-6:]}")
        
        balance_table.add_row(
            bribe['pool'],
            f"{bribe['address'][:8]}...{bribe['address'][-6:]}",
            f"{reward_count} tokens: " + ", ".join(tokens)
        )
    except Exception as e:
        balance_table.add_row(
            bribe['pool'],
            f"{bribe['address'][:8]}...{bribe['address'][-6:]}",
            f"[red]Error: {e}[/red]"
        )

console.print(balance_table)

console.print("\n[bold cyan]Analysis[/bold cyan]")
console.print("=" * 100)
console.print("""
[bold]Two Possible Scenarios:[/bold]

1. [green]Fees accumulate during the epoch[/green] (Good for us!)
   • Trading fees from the pool are sent to internal bribe continuously
   • We can query the contract balance BEFORE voting
   • We can optimize based on actual fee amounts

2. [yellow]Fees only deposited at epoch flip[/yellow] (Bad for us!)
   • Fees stay in the pool during the epoch
   • Only transferred to internal bribe at Wednesday 00:00 UTC flip
   • We'd have to vote BEFORE knowing the fee amounts
   • Would need to estimate based on historical data

[bold]What to check:[/bold]
- Look at BaseScan for these contracts and see when transfers happened
- Check if there are recent inbound transfers or only at weekly intervals
- Review the protocol's fee distribution mechanism in the contracts repo
""")

console.print("\n[bold yellow]Recommendation:[/bold yellow]")
console.print("Check BaseScan for one of these internal bribe contracts to see the")
console.print("transaction history and timing of incoming token transfers.")
console.print(f"\nExample: https://basescan.org/address/{internal_bribes[0]['address']}")

console.print("\n" + "=" * 100)
